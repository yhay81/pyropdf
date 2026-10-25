//! The cross-reference index of a base, and its deterministic repair
//! (DESIGN.md sections 4.1 and 4.3).
//!
//! [`Index::open`] reads the cross-reference data that the file's last
//! `startxref` names: classic tables (ISO 32000-2 section 7.5.4),
//! cross-reference streams (7.5.8), the `/Prev` chain of an incrementally
//! updated file (7.5.6), and the `/XRefStm` streams of hybrid files
//! (7.5.8.4). hayro-syntax 0.8 keeps its own reader of this data
//! crate-private (DESIGN.md section 6), so the index reads the syntax around
//! the values itself, and hayro-syntax parses every value, such as a
//! trailer, through its public `FromBytes` (F5).
//!
//! Opening then checks the entries that locate objects: every uncompressed
//! in-use entry against the object header it points to, every compressed
//! entry for an object stream that an uncompressed entry locates, and the
//! trailer's `/Root` against the entries. When the data cannot be read or
//! the check fails, opening rebuilds the index once by scanning the bytes
//! for object headers, and [`Index::repair`] reports why. A repaired index
//! refuses incremental save, because its locations are not the file's
//! offsets.
//!
//! An object stream decodes once, when one of its members is first read,
//! and its member table is checked against the entries that name the stream
//! by one fixed rule: an entry names the member that the table lists at the
//! entry's index when the table gives that member the entry's object
//! number, and no object otherwise. Reading changes nothing else, so every
//! object resolves the same whatever order objects are read in, and no
//! consumer repairs the index on its own.
//!
//! Every collection that opening grows from the input charges the
//! operation's budget (F6): the entries count against `object_count`, and
//! the decoded bytes of cross-reference streams and object streams against
//! the limits of decoded streams.

mod lex;
mod objstm;
mod read;
mod scan;

#[cfg(test)]
pub(crate) mod tests;

use std::borrow::Cow;
use std::fmt;
use std::panic::{self, AssertUnwindSafe};

use hayro_syntax::object::{self as syntax, FromBytes};

use crate::budget::{Budget, BudgetVec, LimitCode, StreamKind};
use crate::value::{Bytes, Dict, Reference};
use crate::{Error, LimitExceeded, Password, Result};

pub use objstm::ObjectStream;

/// How many bytes may come before the `%PDF-` header from which the file's
/// offsets count, as pdf.js searches for it.
const HEADER_WINDOW: usize = 1024;

/// How many steps a long loop takes between two polls of the budget.
const POLL_INTERVAL: u32 = 1024;

/// The index of a base: the original bytes, where each object is in them,
/// the trailer, the revisions, and the repair report (DESIGN.md section
/// 4.1).
///
/// The index locates every in-use object by its number: at a position of
/// the bytes, or as a member of an object stream. [`Index::object`] returns
/// the bytes that an object's value begins with, which
/// [`Value::parse`](crate::value::Value::parse) reads. The index is
/// immutable but for the object streams that it decodes once each, and
/// several threads may read it at once.
#[derive(Debug)]
pub struct Index {
    bytes: Bytes,
    origin: usize,
    /// The in-use entries, ascending by object number.
    entries: Vec<(u32, Entry)>,
    /// Where objects and cross-reference sections begin, ascending: an
    /// object's bytes end where the next of these begins.
    bounds: Vec<usize>,
    /// The trailer that the scan of a repair chose. An index that read the
    /// file's sections keeps none, since its newest section holds it.
    rebuilt: Dict,
    /// The revisions, oldest first.
    revisions: Vec<Revision>,
    repair: Option<Repair>,
    streams: objstm::Streams,
}

/// Where an in-use object is, and its generation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Entry {
    generation: u16,
    location: Location,
}

/// Where an in-use object is.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Location {
    /// An uncompressed object, whose header begins at this position of the
    /// bytes.
    Offset(usize),
    /// A member of an object stream: the stream's object number, and the
    /// member's index in the table of the stream.
    Member {
        /// The object number of the object stream.
        stream: u32,
        /// The index of the member in the stream's table.
        index: u32,
    },
}

/// What a cross-reference section lists for one object number.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Listing {
    /// The object is in use, at the entry's location.
    InUse(Entry),
    /// The object is free, with the generation that its next use takes.
    ///
    /// ISO 32000-2 Table 18 reads an entry of a cross-reference stream whose
    /// type it does not define as a reference to the null object, which this
    /// lists as free with generation 0.
    Free {
        /// The generation that the object's next use takes.
        generation: u16,
    },
}

/// One revision of the file: the bytes that one save wrote, which end with
/// the cross-reference sections of the revision (ISO 32000-2 section 7.5.6).
///
/// A revision usually has one section. A linearized file's revision has
/// two, its first-page section and its main section (Annex F).
#[derive(Debug)]
pub struct Revision {
    sections: Vec<Section>,
    end: Option<usize>,
}

/// A cross-reference section: what it lists, and its trailer.
#[derive(Debug)]
pub struct Section {
    offset: usize,
    kind: SectionKind,
    trailer: Dict,
    listings: Vec<(u32, Listing)>,
}

/// The form of a cross-reference section.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SectionKind {
    /// A table and its trailer (ISO 32000-2 sections 7.5.4 and 7.5.5).
    Table,
    /// A cross-reference stream (section 7.5.8).
    Stream,
    /// A table whose trailer's `/XRefStm` names a cross-reference stream of
    /// the same section (section 7.5.8.4).
    Hybrid {
        /// The position where the stream named by `/XRefStm` begins.
        stream: usize,
    },
}

/// Why opening rebuilt the index, and what the rebuilt index's trailer is.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Repair {
    damage: Damage,
    trailer: Option<usize>,
}

/// What made opening rebuild the index.
#[derive(Clone, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum Damage {
    /// The cross-reference data cannot be read, with what is wrong.
    Unreadable(Box<str>),
    /// In-use entries do not locate the objects that they name: the first
    /// such object number, and how many entries fail.
    Entries {
        /// The lowest object number whose entry fails the check.
        first: u32,
        /// How many entries fail the check.
        count: usize,
    },
    /// The trailer names no catalog in `/Root` that the entries locate.
    Root,
}

impl Index {
    /// Reads the cross-reference data of `bytes`, and builds the index.
    ///
    /// Opening reads the data that the last `startxref` names, and checks
    /// the entries that it gives. When the data cannot be read or the check
    /// fails, opening scans the bytes for object headers and builds the
    /// index from what it finds, and [`Index::repair`] reports why.
    ///
    /// Refuses with `file_size` input larger than the budget allows, with
    /// `object_count` input with more objects, with the limits of decoded
    /// streams cross-reference and object streams that decode to more bytes,
    /// and with a malformed input error input whose cross-reference data
    /// cannot be read and in which the scan finds no object header.
    pub fn open(bytes: Bytes, budget: &Budget) -> Result<Self> {
        budget.check(LimitCode::FileSize, count(bytes.len()))?;
        let origin = origin(&bytes);
        let damage = match read::read(&bytes, origin, budget) {
            Ok(chain) => match chain.check(&bytes, budget)? {
                None => return Self::from_chain(bytes, origin, chain, budget),
                Some(damage) => damage,
            },
            Err(error) if error.limit().is_some() => return Err(error),
            Err(error) => Damage::Unreadable(error.to_string().into_boxed_str()),
        };
        let rebuilt = scan::rebuild(&bytes, budget)?;
        Self::from_rebuilt(bytes, origin, rebuilt, damage, budget)
    }

    /// Returns the index of the cross-reference data that `chain` read.
    fn from_chain(
        bytes: Bytes,
        origin: usize,
        chain: read::Chain,
        budget: &Budget,
    ) -> Result<Self> {
        let mut bounds = BudgetVec::new(budget);
        for (_, entry) in &chain.entries {
            if let Location::Offset(pos) = entry.location {
                bounds.try_push(pos)?;
            }
        }
        for section in chain
            .revisions
            .iter()
            .flat_map(|revision| &revision.sections)
        {
            bounds.try_push(section.offset)?;
            if let SectionKind::Hybrid { stream } = section.kind {
                bounds.try_push(stream)?;
            }
        }
        let streams = objstm::Streams::new(&chain.entries, budget)?;
        Ok(Self {
            bytes,
            origin,
            entries: chain.entries,
            bounds: sorted(bounds),
            rebuilt: Dict::new(),
            revisions: chain.revisions,
            repair: None,
            streams,
        })
    }

    /// Returns the index that a scan rebuilt after `damage`.
    fn from_rebuilt(
        bytes: Bytes,
        origin: usize,
        rebuilt: scan::Rebuilt,
        damage: Damage,
        budget: &Budget,
    ) -> Result<Self> {
        let streams = objstm::Streams::new(&rebuilt.entries, budget)?;
        for (number, decoded) in rebuilt.streams {
            streams.fill(number, decoded, budget)?;
        }
        Ok(Self {
            bytes,
            origin,
            entries: rebuilt.entries,
            bounds: rebuilt.bounds,
            rebuilt: rebuilt.trailer,
            revisions: Vec::new(),
            repair: Some(Repair {
                damage,
                trailer: rebuilt.trailer_at,
            }),
            streams,
        })
    }

    /// Returns the bytes of the file.
    #[must_use]
    pub const fn bytes(&self) -> &Bytes {
        &self.bytes
    }

    /// Returns where the file's offsets count from: the start of its `%PDF-`
    /// header when the header begins within the first 1024 bytes, as
    /// Acrobat and pdf.js read such a file, and the start of the bytes
    /// otherwise.
    ///
    /// The locations of the index are positions of the bytes. An offset
    /// that a save writes into the file is a position less the origin.
    #[must_use]
    pub const fn origin(&self) -> usize {
        self.origin
    }

    /// Returns the number of in-use objects.
    #[must_use]
    pub const fn len(&self) -> usize {
        self.entries.len()
    }

    /// Returns whether the index locates no object.
    #[must_use]
    pub const fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// Returns the entry of object `number`, if it is in use.
    #[must_use]
    pub fn get(&self, number: u32) -> Option<Entry> {
        find(&self.entries, number)
    }

    /// Returns the in-use entries, ascending by object number.
    pub fn entries(&self) -> impl ExactSizeIterator<Item = (u32, Entry)> + '_ {
        self.entries.iter().copied()
    }

    /// Returns the trailer: the newest section's, or the one that the scan
    /// of a repair chose.
    ///
    /// A cross-reference stream's trailer is its dictionary, with the
    /// stream's own entries. The trailer of a repaired index may hold
    /// `/Prev` and `/XRefStm` entries, which no longer name its sections.
    #[must_use]
    pub fn trailer(&self) -> &Dict {
        let newest = self
            .revisions
            .last()
            .and_then(|revision| revision.sections.first());
        newest.map_or(&self.rebuilt, |section| &section.trailer)
    }

    /// Returns the revisions of the file, oldest first.
    ///
    /// A repaired index has none: its locations are not the file's.
    #[must_use]
    pub fn revisions(&self) -> &[Revision] {
        &self.revisions
    }

    /// Returns why opening rebuilt the index, if it did.
    #[must_use]
    pub const fn repair(&self) -> Option<&Repair> {
        self.repair.as_ref()
    }

    /// Returns whether opening rebuilt the index, which an incremental save
    /// must refuse: the file's own offsets do not locate its objects.
    #[must_use]
    pub const fn is_repaired(&self) -> bool {
        self.repair.is_some()
    }

    /// Returns whether the trailer names a security handler in `/Encrypt`.
    #[must_use]
    pub fn is_encrypted(&self) -> bool {
        self.trailer().get("Encrypt").is_some()
    }

    /// Returns the bytes that the value of the object that `reference`
    /// names begins with, or `None` if the index locates no such object.
    ///
    /// The bytes of an uncompressed object run from its value to where the
    /// next object or cross-reference section of the file begins. A member
    /// of an object stream reads from its place in the stream's decoded data
    /// to the end of the data. A reference whose generation differs from the
    /// entry's names no object, and neither does an entry whose member the
    /// stream's table does not confirm.
    ///
    /// Refuses as [`Index::object_stream`] does when the object is a member
    /// of an object stream.
    pub fn object(&self, reference: Reference, budget: &Budget) -> Result<Option<Bytes>> {
        let Some(entry) = self.get(reference.number()) else {
            return Ok(None);
        };
        if entry.generation != reference.generation() {
            return Ok(None);
        }
        Ok(match entry.location {
            Location::Offset(_) => self.source().uncompressed(reference),
            Location::Member { stream, index } => self
                .object_stream(stream, budget)?
                .and_then(|objects| objects.member(index, reference.number())),
        })
    }

    /// Returns object stream `number`, decoded, if an entry names it as the
    /// stream of a member.
    ///
    /// The first read decodes the stream and checks its table against the
    /// entries that name it, and every later read returns what it found, a
    /// malformed input error included. A refusal by the budget is not kept,
    /// so a later read with a larger budget decodes the stream. Refuses with
    /// the limits of decoded streams a stream that decodes to more bytes
    /// than the budget allows, and with [`Password::Required`] the object
    /// streams of an encrypted document, which the store cannot decrypt yet
    /// (yhay81/pyropdf#28).
    pub fn object_stream(&self, number: u32, budget: &Budget) -> Result<Option<&ObjectStream>> {
        let Some(container) = self.streams.container(number) else {
            return Ok(None);
        };
        if let Some(outcome) = container.outcome() {
            return outcome.as_ref().map(Some).map_err(Clone::clone);
        }
        if self.is_encrypted() {
            return Err(Error::from(Password::Required).context(format!("object stream {number}")));
        }
        let decoded = objstm::decode(&self.source(), number, budget)
            .and_then(|decoded| ObjectStream::new(decoded, self.streams.named(container), budget));
        let computed = match decoded {
            Err(error) if error.limit().is_some() => return Err(error),
            decoded => decoded.map_err(|error| error.context(format!("object stream {number}"))),
        };
        container
            .keep(computed)
            .as_ref()
            .map(Some)
            .map_err(Clone::clone)
    }

    /// Returns the bytes and the entries that locate the index's
    /// uncompressed objects.
    fn source(&self) -> Source<'_> {
        Source {
            bytes: &self.bytes,
            entries: &self.entries,
            bounds: &self.bounds,
        }
    }
}

impl Entry {
    /// Returns the generation.
    #[must_use]
    pub const fn generation(&self) -> u16 {
        self.generation
    }

    /// Returns where the object is.
    #[must_use]
    pub const fn location(&self) -> Location {
        self.location
    }
}

impl Revision {
    /// Returns the cross-reference sections of the revision, in the order
    /// that the `/Prev` chain links them: the one that the revision's
    /// `startxref` names first.
    #[must_use]
    pub fn sections(&self) -> &[Section] {
        &self.sections
    }

    /// Returns where the revision's bytes end: after the `%%EOF` marker
    /// that its `startxref` line ends with, and after the end-of-line marker
    /// that follows it, if the marker is there.
    #[must_use]
    pub const fn end(&self) -> Option<usize> {
        self.end
    }
}

impl Section {
    /// Returns the position that the `startxref` or `/Prev` naming the
    /// section gives: where it begins, or the white space and comments
    /// before it.
    #[must_use]
    pub const fn offset(&self) -> usize {
        self.offset
    }

    /// Returns the form of the section.
    #[must_use]
    pub const fn kind(&self) -> SectionKind {
        self.kind
    }

    /// Returns the section's trailer: the dictionary after its table's
    /// `trailer` keyword, or its cross-reference stream's dictionary.
    #[must_use]
    pub const fn trailer(&self) -> &Dict {
        &self.trailer
    }

    /// Returns what the section lists for each object number, ascending.
    ///
    /// A number that a section lists twice keeps its last listing, and a
    /// hybrid section lists an in-use entry of its table before what its
    /// stream lists, and that before a free entry of its table, as ISO
    /// 32000-2 section 7.5.8.4 reads such a section.
    #[must_use]
    pub fn listings(&self) -> &[(u32, Listing)] {
        &self.listings
    }
}

impl Repair {
    /// Returns what made opening rebuild the index.
    #[must_use]
    pub const fn damage(&self) -> &Damage {
        &self.damage
    }

    /// Returns where the dictionary that the rebuilt index took as its
    /// trailer begins, or `None` when the scan found no trailer that names
    /// a catalog: the trailer then names the last catalog that the scan
    /// found, or is empty when it found none.
    #[must_use]
    pub const fn trailer(&self) -> Option<usize> {
        self.trailer
    }
}

impl fmt::Display for Repair {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "rebuilt the cross-reference index by scanning object headers: {}",
            self.damage
        )
    }
}

impl fmt::Display for Damage {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unreadable(problem) => formatter.write_str(problem),
            Self::Entries { first, count: 1 } => write!(
                formatter,
                "the entry of object {first} does not locate its object"
            ),
            Self::Entries { first, count } => write!(
                formatter,
                "{count} entries do not locate their objects, the first that of object {first}"
            ),
            Self::Root => {
                formatter.write_str("the trailer names no catalog that the entries locate")
            }
        }
    }
}

/// The bytes, and the entries that locate the uncompressed objects in them.
#[derive(Clone, Copy)]
struct Source<'a> {
    bytes: &'a Bytes,
    /// Entries ascending by object number, of which only the uncompressed
    /// ones are read.
    entries: &'a [(u32, Entry)],
    /// Where objects and sections begin, ascending.
    bounds: &'a [usize],
}

impl Source<'_> {
    /// Returns the bytes of the uncompressed object that `reference` names:
    /// from its value to where the next object or section begins.
    fn uncompressed(&self, reference: Reference) -> Option<Bytes> {
        let entry = find(self.entries, reference.number())?;
        let (Location::Offset(pos), true) =
            (entry.location, entry.generation == reference.generation())
        else {
            return None;
        };
        let header = lex::header(self.bytes, pos)?;
        let next = self.bounds.partition_point(|&bound| bound <= pos);
        let end = self.bounds.get(next).copied().unwrap_or(self.bytes.len());
        self.bytes.slice(header.body..end.max(header.body))
    }
}

/// Returns the entry of object `number` among `entries`, which ascend by
/// object number.
fn find(entries: &[(u32, Entry)], number: u32) -> Option<Entry> {
    entries
        .binary_search_by_key(&number, |&(entry, _)| entry)
        .ok()
        .map(|index| entries[index].1)
}

/// Returns where the offsets of `bytes` count from: see [`Index::origin`].
fn origin(bytes: &[u8]) -> usize {
    bytes[..bytes.len().min(HEADER_WINDOW)]
        .windows(b"%PDF-".len())
        .position(|window| window == b"%PDF-")
        .unwrap_or(0)
}

/// Returns `positions`, sorted, with each position once.
fn sorted(mut positions: BudgetVec<'_, usize>) -> Vec<usize> {
    positions.sort_unstable();
    let mut kept = 0;
    for index in 0..positions.len() {
        if kept == 0 || positions[kept - 1] != positions[index] {
            positions[kept] = positions[index];
            kept += 1;
        }
    }
    positions.truncate(kept);
    positions.into_vec()
}

/// Converts a length to the budget's unit, saturating where `usize` is
/// wider.
fn count(len: usize) -> u64 {
    u64::try_from(len).unwrap_or(u64::MAX)
}

/// Runs a call into hayro-syntax, and reads a panic as no result.
///
/// hayro-syntax 0.8 panics on some malformed input that it skips but cannot
/// read (I3, yhay81/pyropdf#19).
fn guard<T>(call: impl FnOnce() -> Option<T>) -> Option<T> {
    panic::catch_unwind(AssertUnwindSafe(call)).ok().flatten()
}

/// Parses the stream that `bytes` begin with, through hayro-syntax.
///
/// hayro-syntax's `Stream` reads a dictionary that no `stream` keyword
/// follows as a stream too, when the keyword comes anywhere later in the
/// bytes; an `Object` is a stream only when the keyword follows the
/// dictionary.
fn stream(bytes: &[u8]) -> Option<syntax::Stream<'_>> {
    match guard(|| syntax::Object::from_bytes(bytes))? {
        syntax::Object::Stream(stream) => Some(stream),
        _ => None,
    }
}

/// Decodes `stream`, which hayro-syntax parsed, and charges its decoded size
/// to `budget` as one stream of any kind.
///
/// hayro-syntax 0.8 decodes a stream whole and without a bound, so the
/// budget counts the decoded bytes once they are decoded.
fn decoded<'a>(stream: &syntax::Stream<'a>, budget: &Budget) -> Result<Cow<'a, [u8]>> {
    let data = guard(|| stream.decoded().ok())
        .ok_or_else(|| Error::malformed("the stream's data cannot be decoded"))?;
    budget.decode(StreamKind::Any).charge(count(data.len()))?;
    Ok(data)
}

/// Counts the steps of a long loop, and polls the budget every
/// [`POLL_INTERVAL`] steps, so that the loop stops at the operation's
/// deadline or cancellation.
#[derive(Default)]
struct Ticker(u32);

impl Ticker {
    /// Counts one step.
    fn tick(&mut self, budget: &Budget) -> Result<(), LimitExceeded> {
        self.0 = self.0.wrapping_add(1);
        if self.0.is_multiple_of(POLL_INTERVAL) {
            budget.poll()?;
        }
        Ok(())
    }
}
