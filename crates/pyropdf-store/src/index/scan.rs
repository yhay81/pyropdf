//! Rebuilding the index by scanning the bytes for object headers (DESIGN.md
//! section 4.3).
//!
//! The rules that decide the rebuilt index:
//!
//! - Every object header that begins a token is an object, except one in
//!   the data of a stream whose direct `/Length` `endstream` follows: the
//!   scan skips such data, so that a file embedded in a stream lends it no
//!   objects. Other data is scanned, as hayro-syntax scans it, so that a
//!   stream whose length is wrong hides no object after it.
//! - hayro-syntax parses each object within the bytes before the next
//!   header, so the scan takes time linear in the size of the input.
//! - Of the headers of one object number, the last whose value parses
//!   wins, since an update appends its objects; when none parses, the last.
//! - Each object stream that wins its number decodes, unless a trailer
//!   candidate names a security handler, and adds its members. The object
//!   later in the bytes wins a number, a member counting where its stream
//!   begins, and within one stream the later index wins. hayro-syntax 0.8
//!   prefers any uncompressed object to a member, which keeps the old copy
//!   of an object that an update moved into an object stream.
//! - The trailer is the last candidate in the order of the bytes whose
//!   `/Root` names a dictionary with `/Pages`: a dictionary after a
//!   `trailer` keyword, or the dictionary of a cross-reference stream.
//!   Without one, the trailer names the last object whose dictionary has
//!   `/Type /Catalog`; without that, it is the last candidate, and without
//!   any, it is empty. The index still locates the objects that the scan
//!   found, such as the pages of a file that lost its end.

use std::iter;
use std::ops::Range;

use hayro_syntax::object::{self as syntax, FromBytes, MaybeRef};

use super::lex::{self, Header, Tokens};
use super::objstm::{self, Decoded};
use super::{Entry, Location, Source, Ticker, find, guard, sorted};
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::value::{Bytes, Dict, Name, Reference, Value};
use crate::{Error, Result};

/// The index that a scan rebuilt.
pub(super) struct Rebuilt {
    /// The in-use entries, ascending by object number.
    pub(super) entries: Vec<(u32, Entry)>,
    /// Where the objects that the scan found begin, ascending.
    pub(super) bounds: Vec<usize>,
    pub(super) trailer: Dict,
    /// Where the trailer's dictionary begins, or `None` when the scan made
    /// the trailer: one that names the catalog that it found, or an empty
    /// one.
    pub(super) trailer_at: Option<usize>,
    /// The object streams that the scan decoded, with their object numbers.
    pub(super) streams: Vec<(u32, Decoded)>,
}

/// An object that the scan found.
#[derive(Clone, Copy, Debug)]
struct Found {
    number: u32,
    generation: u16,
    /// Where its header begins.
    pos: usize,
    /// Whether hayro-syntax parses a value after the header, within the
    /// bytes before the next header.
    valid: bool,
    kind: Kind,
    /// Where its dictionary begins and ends, if its value is a dictionary
    /// or a stream.
    dict: Option<(usize, usize)>,
    /// Whether its dictionary has `/Pages`, as a catalog's must.
    pages: bool,
}

/// What an object's dictionary says that it is.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    Other,
    Catalog,
    ObjectStream,
    XRefStream,
}

/// Rebuilds the index of `bytes` by scanning them for object headers.
///
/// Refuses with a malformed input error bytes that hold no object header,
/// and with the budget's limits.
pub(super) fn rebuild(bytes: &Bytes, budget: &Budget) -> Result<Rebuilt> {
    let Marks { headers, keywords } = marks(bytes, budget)?;
    if headers.is_empty() {
        return Err(Error::malformed(
            "no cross-reference data reads, and the bytes hold no object header",
        ));
    }
    let (found, skipped) = objects(bytes, &headers, budget)?;
    drop(headers);
    let candidates = candidates(bytes, &found, &keywords, &skipped, budget)?;
    drop((keywords, skipped));
    let mut bounds = BudgetVec::new(budget);
    bounds.try_reserve_exact(found.len())?;
    bounds.try_extend(found.iter().map(|object| object.pos))?;
    let bounds = sorted(bounds);
    let winners = winners(found, budget)?;
    let mut uncompressed = BudgetVec::new(budget);
    uncompressed.try_reserve_exact(winners.len())?;
    uncompressed.try_extend(winners.iter().map(|object| {
        let location = Location::Offset(object.pos);
        let entry = Entry {
            generation: object.generation,
            location,
        };
        (object.number, entry)
    }))?;
    let source = Source {
        bytes,
        entries: &uncompressed,
        bounds: &bounds,
    };
    let encrypted = candidates
        .iter()
        .any(|(_, trailer)| trailer.get("Encrypt").is_some());
    let streams = if encrypted {
        BudgetVec::new(budget)
    } else {
        decode_streams(&source, &winners, budget)?
    };
    let entries = entries(&uncompressed, &streams, budget)?;
    let mut by_number = BudgetVec::new(budget);
    by_number.try_reserve_exact(streams.len())?;
    by_number.try_extend(
        streams
            .iter()
            .enumerate()
            .map(|(index, &(_, number, _))| (number, index)),
    )?;
    by_number.sort_unstable();
    let choice = Choice {
        entries: &entries,
        winners: &winners,
        streams: &streams,
        by_number: &by_number,
        budget,
    };
    let (trailer, trailer_at) = choice.trailer(candidates)?;
    drop(by_number);
    let mut decoded = BudgetVec::new(budget);
    decoded.try_reserve_exact(streams.len())?;
    decoded.try_extend(
        streams
            .into_vec()
            .into_iter()
            .map(|(_, number, stream)| (number, stream)),
    )?;
    Ok(Rebuilt {
        entries,
        bounds,
        trailer,
        trailer_at,
        streams: decoded.into_vec(),
    })
}

/// The object headers and the `trailer` keywords of the bytes, in their
/// order.
struct Marks<'b> {
    /// Each header, and where it begins.
    headers: BudgetVec<'b, (usize, Header)>,
    /// Where the value after each `trailer` keyword begins.
    keywords: BudgetVec<'b, usize>,
}

/// Finds the object headers and the `trailer` keywords of `bytes`.
fn marks<'b>(bytes: &[u8], budget: &'b Budget) -> Result<Marks<'b>> {
    let mut marks = Marks {
        headers: BudgetVec::new(budget),
        keywords: BudgetVec::new(budget),
    };
    let mut ticker = Ticker::default();
    let mut tokens = Tokens::new(bytes, 0);
    while let Some(pos) = tokens.next() {
        ticker.tick(budget)?;
        if let Some(header) = lex::header(bytes, pos) {
            marks.headers.try_push((pos, header))?;
            tokens.skip_to(header.end);
        } else if let Some(end) = lex::keyword(bytes, pos, b"trailer") {
            marks.keywords.try_push(lex::skip_space(bytes, end))?;
        }
    }
    Ok(marks)
}

/// Reads the object after each of `headers` within the bytes before the
/// next header, and drops the headers inside the data of each stream whose
/// direct `/Length` `endstream` follows.
///
/// Returns the objects in the order of the bytes, and the ranges of the
/// data that the scan skipped, ascending.
fn objects<'b>(
    bytes: &[u8],
    headers: &[(usize, Header)],
    budget: &'b Budget,
) -> Result<(BudgetVec<'b, Found>, BudgetVec<'b, Range<usize>>)> {
    let mut found = BudgetVec::new(budget);
    let mut skipped = BudgetVec::new(budget);
    let mut ticker = Ticker::default();
    let mut next = 0;
    while let Some(&(pos, header)) = headers.get(next) {
        ticker.tick(budget)?;
        next += 1;
        let end = headers.get(next).map_or(bytes.len(), |&(next, _)| next);
        let value = &bytes[header.body..end.max(header.body)];
        let mut object = Found {
            number: header.number,
            generation: header.generation,
            pos,
            valid: false,
            kind: Kind::Other,
            dict: None,
            pages: false,
        };
        if value.starts_with(b"<<") {
            if let Some(dict) = guard(|| syntax::Dict::from_bytes(value)) {
                let dict_end = header.body + dict.data().len();
                object.valid = true;
                object.dict = Some((header.body, dict_end));
                object.kind = kind(&dict);
                object.pages = dict.contains_key(b"Pages");
                if let Some(data_end) = stream_end(bytes, dict_end, &dict) {
                    skipped.try_push(dict_end..data_end)?;
                    while headers.get(next).is_some_and(|&(next, _)| next < data_end) {
                        next += 1;
                    }
                }
            }
        } else {
            object.valid = guard(|| MaybeRef::<syntax::Object<'_>>::from_bytes(value)).is_some();
        }
        found.try_push(object)?;
    }
    Ok((found, skipped))
}

/// Returns what `dict` says that its object is, by its `/Type`.
fn kind(dict: &syntax::Dict<'_>) -> Kind {
    match dict.get::<syntax::Name<'_>>(b"Type").as_deref() {
        Some(b"Catalog") => Kind::Catalog,
        Some(b"ObjStm") => Kind::ObjectStream,
        Some(b"XRef") => Kind::XRefStream,
        _ => Kind::Other,
    }
}

/// Returns where the stream whose dictionary `dict` ends at `dict_end`
/// ends, after `endstream`, when the dictionary's `/Length` is a direct
/// length that `endstream` follows, as hayro-syntax reads such a stream.
fn stream_end(bytes: &[u8], dict_end: usize, dict: &syntax::Dict<'_>) -> Option<usize> {
    let keyword = lex::keyword(bytes, lex::skip_space(bytes, dict_end), b"stream")?;
    let start = lex::skip_eol(bytes, keyword);
    if start == keyword {
        return None;
    }
    let MaybeRef::NotRef(length) = dict.get_raw::<i64>(b"Length")? else {
        return None;
    };
    let end = start.checked_add(usize::try_from(length).ok()?)?;
    if end > bytes.len() {
        return None;
    }
    lex::keyword(bytes, lex::skip_white(bytes, end), b"endstream")
}

/// Returns the candidates for the trailer, in the order of the bytes: where
/// each dictionary begins, and the dictionary.
///
/// A candidate is the dictionary after a `trailer` keyword that is not in
/// skipped data, read within the bytes before the next object, or the
/// dictionary of a cross-reference stream.
fn candidates<'b>(
    bytes: &Bytes,
    found: &[Found],
    keywords: &[usize],
    skipped: &[Range<usize>],
    budget: &'b Budget,
) -> Result<BudgetVec<'b, (usize, Dict)>> {
    let mut candidates = BudgetVec::new(budget);
    for &start in keywords {
        let next = skipped.partition_point(|range| range.start <= start);
        if next > 0 && skipped[next - 1].contains(&start) {
            continue;
        }
        let next = found.partition_point(|object| object.pos <= start);
        let end = found.get(next).map_or(bytes.len(), |object| object.pos);
        if let Some(dict) = dictionary(bytes, start..end, budget)? {
            candidates.try_push((start, dict))?;
        }
    }
    for object in found
        .iter()
        .filter(|object| object.kind == Kind::XRefStream)
    {
        if let Some((start, end)) = object.dict
            && let Some(dict) = dictionary(bytes, start..end, budget)?
        {
            candidates.try_push((start, dict))?;
        }
    }
    candidates.sort_unstable_by_key(|&(start, _)| start);
    Ok(candidates)
}

/// Returns the dictionary that the bytes of `range` begin with, if they
/// begin with one.
fn dictionary(bytes: &Bytes, range: Range<usize>, budget: &Budget) -> Result<Option<Dict>> {
    let Some(source) = bytes.slice(range) else {
        return Ok(None);
    };
    match Value::parse(&source, budget) {
        Ok(Value::Dict(dict)) => Ok(Some(dict)),
        Err(error) if error.limit().is_some() => Err(error),
        _ => Ok(None),
    }
}

/// Returns, ascending by object number, the object that wins each number
/// among `found`: the last whose value parses, or the last when none does.
fn winners<'b>(
    mut found: BudgetVec<'b, Found>,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, Found>> {
    found.sort_unstable_by_key(|object| (object.number, object.valid, object.pos));
    let mut winners = BudgetVec::new(budget);
    for (index, object) in found.iter().enumerate() {
        if found
            .get(index + 1)
            .is_none_or(|next| next.number != object.number)
        {
            winners.try_push(*object)?;
        }
    }
    Ok(winners)
}

/// Decodes the object streams of generation 0 among `winners`, in the order
/// of the bytes: where each begins, its object number, and what it decodes
/// to. A stream that does not decode adds no members.
fn decode_streams<'b>(
    source: &Source<'_>,
    winners: &[Found],
    budget: &'b Budget,
) -> Result<BudgetVec<'b, (usize, u32, Decoded)>> {
    let mut order = BudgetVec::new(budget);
    for object in winners {
        if object.kind == Kind::ObjectStream && object.generation == 0 {
            order.try_push((object.pos, object.number))?;
        }
    }
    order.sort_unstable();
    let mut streams = BudgetVec::new(budget);
    for &(pos, number) in order.iter() {
        match objstm::decode(source, number, budget) {
            Ok(decoded) => streams.try_push((pos, number, decoded))?,
            Err(error) if error.limit().is_some() => return Err(error),
            Err(_) => {}
        }
    }
    Ok(streams)
}

/// Returns the entries of the rebuilt index, ascending by object number:
/// for each number, the uncompressed object of `uncompressed`, or the member
/// of `streams` when its stream begins later in the bytes.
fn entries(
    uncompressed: &[(u32, Entry)],
    streams: &[(usize, u32, Decoded)],
    budget: &Budget,
) -> Result<Vec<(u32, Entry)>> {
    let mut members = BudgetVec::new(budget);
    for (pos, stream, decoded) in streams {
        for (index, &(number, _)) in decoded.members().iter().enumerate() {
            let Ok(index) = u32::try_from(index) else {
                break;
            };
            let later = match find(uncompressed, number).map(|entry| entry.location) {
                Some(Location::Offset(header)) => *pos > header,
                _ => true,
            };
            if later {
                members.try_push((number, *pos, index, *stream))?;
            }
        }
    }
    members.sort_unstable();
    let mut compressed = members
        .iter()
        .enumerate()
        .filter(|&(index, &(number, ..))| {
            members
                .get(index + 1)
                .is_none_or(|&(next, ..)| next != number)
        })
        .map(|(_, &(number, _, index, stream))| {
            let location = Location::Member { stream, index };
            let entry = Entry {
                generation: 0,
                location,
            };
            (number, entry)
        })
        .peekable();
    let mut entries = BudgetVec::counting(budget, LimitCode::ObjectCount);
    for &(number, entry) in uncompressed {
        while let Some(&(member, _)) = compressed.peek()
            && member < number
        {
            entries.try_extend(compressed.next())?;
        }
        match compressed.next_if(|&(member, _)| member == number) {
            Some(member) => entries.try_push(member)?,
            None => entries.try_push((number, entry))?,
        }
    }
    entries.try_extend(compressed)?;
    Ok(entries.into_vec())
}

/// What choosing the trailer of a rebuilt index reads.
struct Choice<'a> {
    entries: &'a [(u32, Entry)],
    /// The objects that won their numbers, ascending by object number.
    winners: &'a [Found],
    /// The decoded object streams, in the order of the bytes.
    streams: &'a [(usize, u32, Decoded)],
    /// The object number of each of `streams` and its index there,
    /// ascending.
    by_number: &'a [(u32, usize)],
    budget: &'a Budget,
}

impl Choice<'_> {
    /// Returns the trailer among `candidates`, or one that the scan makes,
    /// and where its dictionary begins: see the module's rules.
    fn trailer(
        &self,
        mut candidates: BudgetVec<'_, (usize, Dict)>,
    ) -> Result<(Dict, Option<usize>)> {
        // Whether the object of each entry is a catalog, once checked.
        let mut checked = BudgetVec::new(self.budget);
        checked.try_reserve_exact(self.entries.len())?;
        checked.try_extend(iter::repeat_n(None, self.entries.len()))?;
        let mut chosen = None;
        for index in (0..candidates.len()).rev() {
            if let Some(&Value::Reference(root)) = candidates[index].1.get("Root")
                && self.is_catalog(root, &mut checked)?
            {
                chosen = Some(index);
                break;
            }
        }
        if chosen.is_none()
            && let Some(root) = self.catalog()?
        {
            #[expect(
                clippy::disallowed_methods,
                reason = "the trailer that the scan makes has one entry, its `/Root`"
            )]
            let trailer = Dict::from_iter([(Name::from("Root"), Value::Reference(root))]);
            return Ok((trailer, None));
        }
        candidates.truncate(chosen.map_or(candidates.len(), |index| index + 1));
        Ok(candidates.pop().map_or_else(
            || (Dict::new(), None),
            |(start, trailer)| (trailer, Some(start)),
        ))
    }

    /// Returns whether `root` names an object whose value is a dictionary
    /// with `/Pages`, keeping what it finds for each entry in `checked`.
    fn is_catalog(&self, root: Reference, checked: &mut [Option<bool>]) -> Result<bool> {
        let Ok(slot) = self
            .entries
            .binary_search_by_key(&root.number(), |&(number, _)| number)
        else {
            return Ok(false);
        };
        let entry = self.entries[slot].1;
        if entry.generation != root.generation() {
            return Ok(false);
        }
        if let Some(known) = checked[slot] {
            return Ok(known);
        }
        let catalog = match entry.location {
            Location::Offset(_) => self
                .winners
                .binary_search_by_key(&root.number(), |object| object.number)
                .is_ok_and(|index| self.winners[index].pages),
            Location::Member { stream, index } => {
                let decoded = self
                    .by_number
                    .binary_search_by_key(&stream, |&(number, _)| number)
                    .ok()
                    .map(|slot| &self.streams[self.by_number[slot].1].2);
                match decoded {
                    Some(decoded) => self
                        .member(decoded, index, root.number())?
                        .is_some_and(|dict| dict.get("Pages").is_some()),
                    None => false,
                }
            }
        };
        checked[slot] = Some(catalog);
        Ok(catalog)
    }

    /// Returns the last object in the order of the bytes whose dictionary
    /// has `/Type /Catalog`: an uncompressed one, or else a member of the
    /// latest object stream that holds one.
    fn catalog(&self) -> Result<Option<Reference>> {
        if let Some(object) = self
            .winners
            .iter()
            .filter(|object| {
                object.kind == Kind::Catalog
                    && find(self.entries, object.number)
                        .is_some_and(|entry| entry.location == Location::Offset(object.pos))
            })
            .max_by_key(|object| object.pos)
        {
            return Ok(Some(Reference::new(object.number, object.generation)));
        }
        let catalog = Value::Name(Name::from("Catalog"));
        for (_, stream, decoded) in self.streams.iter().rev() {
            for (index, &(number, _)) in decoded.members().iter().enumerate().rev() {
                let Ok(index) = u32::try_from(index) else {
                    continue;
                };
                let location = Location::Member {
                    stream: *stream,
                    index,
                };
                if find(self.entries, number).is_some_and(|entry| entry.location == location)
                    && self
                        .member(decoded, index, number)?
                        .is_some_and(|dict| dict.get("Type") == Some(&catalog))
                {
                    return Ok(Some(Reference::new(number, 0)));
                }
            }
        }
        Ok(None)
    }

    /// Returns the dictionary of member `index` of `decoded`, if its table
    /// lists object `number` there and the member's value is a dictionary.
    fn member(&self, decoded: &Decoded, index: u32, number: u32) -> Result<Option<Dict>> {
        let Some(bytes) = decoded.member(index, number) else {
            return Ok(None);
        };
        match Value::parse(&bytes, self.budget) {
            Ok(Value::Dict(dict)) => Ok(Some(dict)),
            Err(error) if error.limit().is_some() => Err(error),
            _ => Ok(None),
        }
    }
}
