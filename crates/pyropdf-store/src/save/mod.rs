//! Full and incremental saves of immutable versions (DESIGN.md section 4.4).
//!
//! Untouched values keep their original bytes; written values are copied as
//! written. Output goes directly to any [`Write`], without a second PDF in
//! memory. Metadata and output share the operation's [`Budget`]. Saving
//! never changes the version, its base, or its object allocation state.

mod base;
mod extensions;
mod hybrid;
mod integrity;
pub(crate) use base::encryption_dict;
mod linear;
mod merge;
mod pack;
mod prune;
mod raw;
mod recode;
mod renumber;

#[cfg(test)]
mod tests;

use std::borrow::Cow;
use std::fmt;
use std::io::{self, Write};

use md5::{Digest, Md5};

use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::index::{Index, Location, SectionKind};
use crate::security::{self, Encryption, Handler};
use crate::value::{Array, Bytes, Dict, PdfString, Reference, Stream, Value, encode};
use crate::version::{Slot, Version};
use crate::{Error as StoreError, ErrorKind, LimitExceeded, write};
use recode::StreamPolicy;
use renumber::Renumbering;

pub use base::FileBase;
pub use linear::is_linearized;
pub use recode::{StreamFilter, Streams, deflate};

/// A save either reports a typed store error, preserves the sink's I/O
/// error, or refuses a mode unsupported by this base.
#[derive(Debug)]
pub enum Error {
    /// A malformed value or an operation-budget refusal.
    Store(StoreError),
    /// The output writer failed. Partial output must be discarded.
    Io(io::Error),
    /// The requested save is not supported for this base.
    Refused(Refusal),
}

/// Why a save is refused before writing any bytes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Refusal {
    /// Incremental save requires the bytes and cross-reference offset of a file.
    NoOriginalFile,
    /// A repaired file's original offsets cannot be used by an update.
    RepairedBase,
    /// Encryption is implemented separately in issue #28.
    Encryption,
    /// A recipient must hold Table 24 bit 2 to change the handler.
    RecipientPermissions,
    /// A protected revision must retain its integrity protection.
    IntegrityProtection,
    /// Linearized byte-range signing is not implemented.
    MacLinearization,
    /// An attached MAC is bound to the existing signature's exact bytes.
    SignedMacRevision,
    /// Pruning, decompressing, explicit stream filters, ASCII/pretty output,
    /// another cross-reference form, object streams, and renumbering rewrite
    /// the original file, whose objects, numbers, and cross-reference form an
    /// incremental save keeps.
    FullSaveOption,
    /// A linearized save writes a cross-reference table, and neither
    /// object streams, merged objects, nor ASCII/pretty output.
    Linearization,
    /// Encrypted strings and streams are binary ciphertext (ISO 32000-2
    /// section 7.6.2), so encrypted saves cannot guarantee ASCII-only output.
    AsciiEncryption,
}

/// The result of saving to an output writer.
pub type Result<T> = std::result::Result<T, Error>;

impl Error {
    /// Adds `context` to a store error, as [`StoreError::context`] does.
    fn context(self, context: impl fmt::Display) -> Self {
        match self {
            Self::Store(error) => Self::Store(error.context(context)),
            error => error,
        }
    }
}

impl From<StoreError> for Error {
    fn from(error: StoreError) -> Self {
        Self::Store(error)
    }
}

impl From<LimitExceeded> for Error {
    fn from(error: LimitExceeded) -> Self {
        Self::Store(error.into())
    }
}

impl From<io::Error> for Error {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Store(error) => error.fmt(f),
            Self::Io(error) => error.fmt(f),
            Self::Refused(reason) => f.write_str(match reason {
                Refusal::NoOriginalFile => "incremental save requires an original PDF file",
                Refusal::RepairedBase => "incremental save is unavailable for a repaired base",
                Refusal::Encryption => "saving encrypted documents requires the security handler",
                Refusal::RecipientPermissions => "the recipient lacks permission to change encryption; keep the encryption or unlock with an authorized recipient",
                Refusal::IntegrityProtection => "a PDF MAC protected document requires retained encryption or a replacement with integrity protection",
                Refusal::SignedMacRevision => "use an incremental save to preserve the existing signature and its attached PDF MAC",
                Refusal::MacLinearization => "linearization of a PDF MAC protected document is not supported",
                Refusal::FullSaveOption => {
                    "prune, decompress, stream filters, ASCII/pretty formatting, a new cross-reference form, object streams, renumbering, and linearization require a full save"
                }
                Refusal::Linearization => {
                    "a linearized save writes a cross-reference table, without object streams, merged objects, or ASCII/pretty formatting"
                }
                Refusal::AsciiEncryption => {
                    "ASCII output is not available for encrypted saves"
                }
            }),
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Store(error) => Some(error),
            Self::Io(error) => Some(error),
            Self::Refused(_) => None,
        }
    }
}

/// A cross-reference entry, held in ascending object-number order.
struct Row {
    number: u32,
    generation: u16,
    /// `None` is a free entry; in-use offsets are set while writing objects.
    offset: Option<u64>,
    next_free: u32,
    /// The object stream that holds the object, and the object's index in
    /// it, for an object that the save packs into one.
    member: Option<(u32, u32)>,
}

/// Writes all live objects to `target`, with cross-reference data in the
/// form of the base's ([`CrossReference::Base`]) and a new trailer.
/// Object-stream members become ordinary objects;
/// their exact decoded member bytes are preserved. Existing object-stream
/// and xref-stream objects also retain their bytes.
///
/// Returns the number of bytes written. On an error, discard partial
/// output. The caller owns flushing, closing, and atomic file replacement.
pub fn full(version: &Version, target: &mut dyn Write, budget: &Budget) -> Result<u64> {
    full_with(version, target, budget, None, Options::default())
}

/// Writes all live objects to `target` as [`full`] does, encrypting every
/// string and stream with `encryption` (DESIGN.md section 4.5), whatever
/// the version's own encryption is.
pub fn full_encrypted(
    version: &Version,
    target: &mut dyn Write,
    budget: &Budget,
    encryption: &Encryption,
) -> Result<u64> {
    full_with(
        version,
        target,
        budget,
        Some(encryption),
        Options::default(),
    )
}

/// What a full save changes beyond writing every live object (DESIGN.md
/// section 4.4). Every option is off by default.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Options {
    /// Writes only the objects that the trailer reaches, and the numbers of
    /// the others as free entries.
    pub prune: bool,
    /// How the save writes the data of streams.
    pub streams: Streams,
    /// Per-object stream filter overrides, as `(object_number, filter)` pairs.
    /// Overrides `streams` for the named objects; unlisted objects follow
    /// `streams`.
    pub stream_filters: Vec<(u32, StreamFilter)>,
    /// Renews the file identifier (ISO 32000-2 section 14.4) with the MD5
    /// digest of what the save writes before it, so that the same version
    /// saves to the same bytes. A save of a version with changes keeps the
    /// first element of the trailer's `/ID` and writes the digest as the
    /// second, and a save of a trailer without `/ID` writes it as both.
    /// Otherwise, and without this option, `/ID` stays as the trailer has
    /// it.
    pub renew_id: bool,
    /// The form of the cross-reference data.
    pub cross_reference: CrossReference,
    /// Packs into object streams of up to 100 members the objects that ISO
    /// 32000-2 section 7.5.7 lets them hold: every object that is not a
    /// stream, has generation 0, and is not the `/Encrypt` dictionary. The
    /// save then writes a cross-reference stream, whatever
    /// `cross_reference` asks. Object streams are compressed with Flate at
    /// the level of [`Streams::Compress`], or 6, and with
    /// [`Streams::Decompress`] not at all.
    pub object_streams: bool,
    /// Numbers the objects that the save writes 1, 2, 3, and so on in the
    /// order of their numbers, with generation 0, and the objects that it
    /// adds after them. Each reference follows its object, and a reference
    /// to an object that the save does not write becomes null, as ISO
    /// 32000-2 section 7.3.10 reads it. The cross-reference data then lists
    /// no free number but 0.
    pub renumber: bool,
    /// Merges each set of identical objects into the one with the smallest
    /// number, and points every reference to it. Pages, annotations, form
    /// fields, structure elements, outline items, optional-content groups,
    /// the objects that the trailer names, and objects in cycles of
    /// references never merge. The merged objects' numbers become free
    /// entries, unless the save renumbers.
    pub dedupe: bool,
    /// Writes the file linearized (ISO 32000-2 Annex F), with hint tables,
    /// for viewers that show the first page before the rest has arrived.
    /// The save then writes only the objects that the pages, the trailer,
    /// and the catalog reach, numbered anew, moves the page tree's
    /// inherited attributes into the pages, and refuses object streams, a
    /// cross-reference stream, `dedupe`, `ascii`, and `pretty`.
    pub linearize: bool,
    /// Writes only printable ASCII bytes: omits the binary header comment,
    /// escapes binary strings and names, defaults `CrossReference::Base` to
    /// a classic table, and wraps streams containing non-ASCII bytes with
    /// `ASCII85Decode`.
    pub ascii: bool,
    /// Formats dictionaries and nested/long arrays deterministically with
    /// two-space indentation instead of preserving raw lexemes.
    pub pretty: bool,
}

/// Output formatting options for a save.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(super) struct Format {
    pub(super) ascii: bool,
    pub(super) pretty: bool,
}

impl Format {
    pub(super) const fn rewrites_syntax(self) -> bool {
        self.ascii || self.pretty
    }

    pub(super) fn write_value(
        self,
        out: &mut BudgetVec<'_, u8>,
        value: &Value,
    ) -> crate::Result<()> {
        if self.pretty {
            write::pretty(out, value)
        } else if self.ascii {
            write::canonical(out, value)
        } else {
            write::value(out, value)
        }
    }

    pub(super) fn write_trailer_value(
        self,
        out: &mut BudgetVec<'_, u8>,
        value: &Value,
    ) -> crate::Result<()> {
        if self.pretty {
            write::pretty_indented(out, value, 1)
        } else if self.ascii {
            write::canonical(out, value)
        } else {
            write::value(out, value)
        }
    }
}

/// The form of the cross-reference data that a save writes.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum CrossReference {
    /// The form of the section that the `startxref` of the base's file
    /// names: a stream for a cross-reference stream, and a table for a
    /// table, a hybrid-reference section, and a version without a file.
    #[default]
    Base,
    /// A table and its trailer (ISO 32000-2 sections 7.5.4 and 7.5.5).
    Table,
    /// A cross-reference stream (section 7.5.8). A full save raises the
    /// header's version to 1.5 when it is lower.
    Stream,
    /// A classic main section with a supplemental cross-reference stream in an update.
    Hybrid,
}

impl CrossReference {
    /// Returns whether a save of `version` writes a cross-reference stream.
    fn stream(self, version: &Version) -> bool {
        match self {
            Self::Table => false,
            Self::Stream | Self::Hybrid => true,
            Self::Base => version
                .base()
                .file()
                .and_then(|file| file.revisions().last())
                .and_then(|revision| revision.sections().first())
                .is_some_and(|section| section.kind() == SectionKind::Stream),
        }
    }
}

/// The level of the Flate data of the streams that a save adds: a
/// cross-reference stream and object streams.
const fn level(streams: Streams) -> Option<u8> {
    match streams {
        Streams::Keep => Some(6),
        Streams::Compress(level) | Streams::Filter(StreamFilter::Flate(level)) => Some(level),
        Streams::Decompress | Streams::Filter(StreamFilter::None) => None,
        Streams::Filter(
            StreamFilter::Keep
            | StreamFilter::Ascii85
            | StreamFilter::AsciiHex
            | StreamFilter::RunLength,
        ) => Some(6),
    }
}

/// Writes the objects of `version` to `target` as [`full`] does, encrypted
/// with `encryption` as [`full_encrypted`] does when it is given, and
/// changed as `options` asks.
pub fn full_with(
    version: &Version,
    target: &mut dyn Write,
    budget: &Budget,
    encryption: Option<&Encryption>,
    options: Options,
) -> Result<u64> {
    budget.poll()?;
    // Repair can recover an illegal live object zero. The mandatory free
    // row would discard it, even when renumbering or pruning is requested.
    if version.generation(0).is_some() {
        return Err(StoreError::malformed(
            "cannot save live object number 0: it is reserved for the free-list head",
        )
        .into());
    }
    let mut declared = None;
    if encryption.is_some_and(|config| config.method() == security::Method::AesGcm) {
        declared = Some(extensions::declare(
            version,
            32003,
            ":2023",
            "https://www.iso.org/standard/45876.html",
            budget,
        )?);
    }
    if matches!(
        encryption,
        Some(Encryption::PasswordWithMac(_) | Encryption::RecipientWithMac(_))
    ) {
        declared = Some(extensions::declare(
            declared.as_deref().unwrap_or(version),
            32004,
            ":2024",
            "https://www.iso.org/standard/45877.html",
            budget,
        )?);
    }
    let version = declared.as_deref().unwrap_or(version);
    let mut mode = Mode::of(version, encryption, budget)?;
    if options.ascii && !matches!(mode, Mode::Plain) {
        return Err(Error::Refused(Refusal::AsciiEncryption));
    }
    let mac = integrity::plan(version, &mode, true, options.linearize, budget)?;
    let policy = StreamPolicy::new(options.streams, &options.stream_filters, budget)?;
    let format = Format {
        ascii: options.ascii,
        pretty: options.pretty,
    };
    if options.linearize {
        if options.object_streams
            || options.dedupe
            || options.ascii
            || options.pretty
            || matches!(
                options.cross_reference,
                CrossReference::Stream | CrossReference::Hybrid
            )
        {
            return Err(Error::Refused(Refusal::Linearization));
        }
        return linear::write(version, target, budget, &mode, &policy, options.renew_id);
    }
    let mut numbers = BudgetVec::new(budget);
    version.base().numbers(&mut numbers)?;
    for (number, _) in version.slots() {
        budget.poll()?;
        numbers.try_push(number)?;
    }
    numbers.try_push(0)?;
    if let Ok(last) = u32::try_from(version.next_number().saturating_sub(1)) {
        // /Size may reserve numbers that have no entry, including a
        // producer's unlisted xref-stream object. Retain the bound with a
        // free entry without expanding a sparse range into all its gaps.
        numbers.try_push(last)?;
    }
    numbers.sort_unstable();
    let mut rows: BudgetVec<'_, Row> = BudgetVec::new(budget);
    for &number in &numbers {
        budget.poll()?;
        if rows.last().is_some_and(|row: &Row| row.number == number) {
            continue;
        }
        rows.try_push(row(version, number))?;
    }
    drop(numbers);
    if options.prune {
        let encrypt = !matches!(mode, Mode::New { .. });
        prune::free_unreachable(version, &mut rows, encrypt, budget)?;
    }
    let plan = if options.dedupe {
        merge::plan(version, &rows, budget)?
    } else {
        merge::Plan {
            merged: BudgetVec::new(budget),
            referrers: BudgetVec::new(budget),
        }
    };
    for &(number, _, _) in plan.merged.iter() {
        budget.poll()?;
        if let Ok(at) = rows.binary_search_by_key(&number, |row| row.number) {
            rows[at].offset = None;
            // A free entry names the generation that the number takes next.
            rows[at].generation = rows[at].generation.saturating_add(1);
        }
    }
    let renumbering = if options.renumber || options.dedupe {
        let renumbering = Renumbering::new(&rows, plan, options.renumber, budget)?;
        if options.renumber
            && let Mode::New { number, .. } = &mut mode
        {
            // The new /Encrypt dictionary follows the renumbered objects.
            let mut after = renumbering.len() + 1;
            *number = pack::take(&mut after)?;
        }
        Some(renumbering)
    } else {
        None
    };
    let renumbering = renumbering.as_ref();
    let hybrid = options.cross_reference == CrossReference::Hybrid;
    // In ASCII mode without an explicit xref stream or object-stream request,
    // default `CrossReference::Base` to a classic ASCII table for readability.
    let stream = options.object_streams
        || if format.ascii && options.cross_reference == CrossReference::Base {
            false
        } else {
            options.cross_reference.stream(version)
        };
    // Packing in a hybrid is optional. Keep everything reachable with a
    // classic reader, including the catalog, pages, resources and trailer.
    let visible = if hybrid && options.object_streams && !options.prune {
        Some(prune::reachable(version, &rows, true, budget)?)
    } else {
        None
    };
    let mut out = Output::new(target, budget, 0, options.renew_id);
    out.mac = mac.map(integrity::MacOutput::new);
    out.bytes(mode.header(version, stream, out.mac.is_some()))?;
    if format.ascii {
        out.bytes(b"\n")?;
    } else {
        out.bytes(b"\n%\xE2\xE3\xCF\xD3\n")?;
    }
    // A copied linearization dictionary must not remain the first object:
    // its old offsets describe the base, not this full save. Write the
    // catalog first, retaining every object's value and number unchanged.
    let root = match version.trailer().get("Root") {
        Some(Value::Reference(root)) => Some(root.number()),
        _ => None,
    };
    // The objects that the save adds take the numbers after the version's,
    // or the renumbered objects', and the new /Encrypt's.
    let size = match renumbering {
        Some(renumbering) if options.renumber => renumbering.len() + 1,
        _ => version.next_number(),
    };
    let next = size + u64::from(matches!(mode, Mode::New { .. }));
    let level = level(policy.default_streams());
    let mut packer = (options.object_streams && !(hybrid && options.prune))
        .then(|| pack::Packer::new(budget, level, next, format));
    let encrypt = match version.trailer().get("Encrypt") {
        Some(Value::Reference(encrypt)) => Some(encrypt.number()),
        _ => None,
    };
    let mut place = |at: usize, row: &mut Row, out: &mut Output<'_>| {
        let rewriting = renumbering.filter(|renumbering| renumbering.rewrites(row.number));
        if let Some(packer) = &mut packer
            && !visible.as_ref().is_some_and(|marked| marked[at])
            && packer.pack(version, row, encrypt, rewriting, out, &mode)?
        {
            return Ok(());
        }
        write_object(version, row, rewriting, out, &mode, &policy, format)
    };
    if let Some(at) = root.and_then(|root| rows.binary_search_by_key(&root, |row| row.number).ok())
    {
        place(at, &mut rows[at], &mut out)?;
    }
    for (at, row) in rows.iter_mut().enumerate() {
        if Some(row.number) != root {
            place(at, row, &mut out)?;
        }
    }
    if let Some(renumbering) = renumbering
        && options.renumber
    {
        rows = renumbering.rows(&rows, budget)?;
    }
    if let Mode::New { dict, number, .. } = &mode {
        // The new /Encrypt dictionary, which is never encrypted, takes the
        // number after every other.
        let mut row = Row {
            number: *number,
            generation: 0,
            offset: Some(out.offset()?),
            next_free: 0,
            member: None,
        };
        let mut bytes = BudgetVec::new(budget);
        format.write_value(&mut bytes, &Value::Dict(dict.clone()))?;
        out.display(format_args!("{number} 0 obj\n"))?;
        out.bytes(&bytes)?;
        out.bytes(b"\nendobj\n")?;
        row.next_free = 0;
        rows.try_push(row)?;
    }
    let mut next = next;
    if let Some(mut packer) = packer {
        packer.flush(&mut out, &mode)?;
        let (after, streams) = packer.finish();
        next = after;
        rows.try_extend(streams.into_vec())?;
    }
    let ending = Ending {
        previous: None,
        hybrid,
        size,
        renew_id: options.renew_id,
        format,
        stream: if stream {
            Some((pack::take(&mut next)?, level))
        } else {
            None
        },
        // The trailer names no merged object, as such objects never merge.
        renumbering: renumbering.filter(|_| options.renumber),
    };
    finish(version, &mut rows, &mut out, &mode, ending)?;
    if let Some(mac) = out.mac.take() {
        mac.finish(out.target, budget)?;
    }
    Ok(out.position)
}

/// Writes the original file byte for byte, followed only by the latest
/// slots written since the base and a cross-reference section with /Prev,
/// a table or a stream as the base's last section is.
/// No-edit saves (including undo to the initial version) reproduce the
/// original bytes exactly and do not parse any objects.
///
/// Refuses repaired and non-file bases and encryption before writing.
/// Returns bytes written; partial output after an error must be discarded.
pub fn incremental(version: &Version, target: &mut dyn Write, budget: &Budget) -> Result<u64> {
    incremental_with(version, target, budget, Options::default())
}

/// Writes the update as [`incremental`] does, compressing the streams that
/// it writes as [`Streams::Compress`] asks and renewing `/ID` as
/// [`Options::renew_id`] asks. The objects of the original file, their
/// numbers, and the form of its cross-reference data stay as they are, so
/// `prune`, [`Streams::Decompress`], another [`CrossReference`],
/// `object_streams`, and `renumber` are refused before writing.
pub fn incremental_with(
    version: &Version,
    target: &mut dyn Write,
    budget: &Budget,
    options: Options,
) -> Result<u64> {
    write_incremental(version, target, budget, options, None).map(|(size, _)| size)
}

/// Write an incremental signing revision and, for a MAC-protected source,
/// reserve the trailer link to the signature object. The caller embeds the
/// returned MAC in its signed CMS container before publishing the bytes.
pub fn incremental_signature(
    version: &Version,
    target: &mut dyn Write,
    budget: &Budget,
    signature: Reference,
) -> Result<Option<security::integrity::Writer>> {
    write_incremental(version, target, budget, Options::default(), Some(signature))
        .map(|(_, mac)| mac)
}

fn write_incremental(
    version: &Version,
    target: &mut dyn Write,
    budget: &Budget,
    options: Options,
    signature: Option<Reference>,
) -> Result<(u64, Option<security::integrity::Writer>)> {
    if options.prune
        || matches!(options.streams, Streams::Decompress | Streams::Filter(_))
        || !options.stream_filters.is_empty()
        || options.cross_reference != CrossReference::Base
        || options.object_streams
        || options.renumber
        || options.dedupe
        || options.linearize
        || options.ascii
        || options.pretty
    {
        return Err(Error::Refused(Refusal::FullSaveOption));
    }
    let file = version
        .base()
        .file()
        .ok_or(Error::Refused(Refusal::NoOriginalFile))?;
    if file.is_repaired() {
        return Err(Error::Refused(Refusal::RepairedBase));
    }
    let mode = Mode::of(version, None, budget)?;
    let mut mac = integrity::plan(version, &mode, false, false, budget)?;
    if signature.is_some() && file.is_encrypted() && mac.is_none() {
        return Err(Error::Refused(Refusal::IntegrityProtection));
    }
    let previous = file
        .revisions()
        .last()
        .and_then(|r| r.sections().first())
        .and_then(|s| s.offset().checked_sub(file.origin()))
        .ok_or_else(|| StoreError::malformed("the original file has no cross-reference offset"))?;
    let mut out = Output::new(target, budget, file.origin() as u64, options.renew_id);
    if version.has_changes() && mac.is_some() {
        if let Some(reference) = signature {
            out.attached = Some(reference);
        } else {
            out.mac = mac.take().map(integrity::MacOutput::new);
        }
    }
    out.bytes(file.bytes())?;
    if !version.has_changes() {
        return Ok((out.position, None));
    }
    out.bytes(b"\n")?;
    let mut rows = BudgetVec::new(budget);
    // Include the head of the newly freed entries. Retired entries are not
    // reusable. Older free entries remain defined by previous sections.
    rows.try_push(row(version, 0))?;
    let policy = StreamPolicy::new(options.streams, &[], budget)?;
    let format = Format::default();
    for (number, _) in version.slots() {
        budget.poll()?;
        if number != 0 {
            let mut row = row(version, number);
            write_object(version, &mut row, None, &mut out, &mode, &policy, format)?;
            rows.try_push(row)?;
        }
    }
    for &number in mode.changed_streams() {
        let mut affected = row(version, number);
        write_object(
            version,
            &mut affected,
            None,
            &mut out,
            &mode,
            &policy,
            format,
        )?;
        rows.try_push(affected)?;
    }
    rows.sort_unstable_by_key(|r| r.number);
    if let Ok(last) = u32::try_from(version.next_number().saturating_sub(1))
        && version.generation(last).is_none()
        && rows.last().is_some_and(|row| row.number < last)
    {
        rows.try_push(row(version, last))?;
    }
    // The update keeps the form of the base's cross-reference data.
    let mut next = version.next_number();
    let hybrid = file
        .revisions()
        .last()
        .and_then(|revision| revision.sections().first())
        .is_some_and(|section| matches!(section.kind(), SectionKind::Hybrid { .. }));
    let ending = Ending {
        previous: Some(previous as u64),
        hybrid,
        size: version.next_number(),
        renew_id: options.renew_id,
        format,
        stream: if hybrid || CrossReference::Base.stream(version) {
            Some((pack::take(&mut next)?, level(options.streams)))
        } else {
            None
        },
        renumbering: None,
    };
    finish(version, &mut rows, &mut out, &mode, ending)?;
    if let Some(mac) = out.mac.take() {
        mac.finish(out.target, budget)?;
    }
    Ok((out.position, mac))
}

/// How a save treats encryption.
enum Mode<'a> {
    /// No encryption: the version's trailer names no `/Encrypt`. An
    /// unlocked encrypted base reads decrypted.
    Plain,
    /// The base's own encryption: untouched objects of the file keep their
    /// encrypted bytes, and the rest are encrypted with its handler.
    Keep {
        handler: Cow<'a, Handler>,
        changed_streams: Vec<u32>,
        index: &'a Index,
        /// The object of the `/Encrypt` dictionary.
        encrypt: Option<u32>,
    },
    /// A new encryption, of every object.
    New {
        handler: Handler,
        dict: Dict,
        id: Vec<u8>,
        /// The number of the new `/Encrypt` dictionary.
        number: u32,
    },
}

impl<'a> Mode<'a> {
    fn of(version: &'a Version, encryption: Option<&Encryption>, budget: &Budget) -> Result<Self> {
        let source_encryption = version.base().trailer().get("Encrypt");
        let selected = version.trailer().get("Encrypt");
        if let Some(handler) = version
            .base()
            .file()
            .and_then(Index::security)
            .filter(|h| h.is_recipient())
        {
            let edited_dictionary = matches!(source_encryption, Some(Value::Reference(r)) if matches!(version.slot(r.number()), Some(Slot::Written(_))));
            if (encryption.is_some() || selected != source_encryption || edited_dictionary)
                && handler.access() != security::Access::Owner
            {
                return Err(Error::Refused(Refusal::RecipientPermissions));
            }
        }
        if let Some(encryption) = encryption {
            let id = match version.trailer().get("ID") {
                Some(Value::Array(id)) => match id.get(0) {
                    Some(Value::String(first)) => Some(first.as_bytes()),
                    _ => None,
                },
                _ => None,
            };
            let (dict, id, handler) = encryption.build(id, budget)?;
            let number = u32::try_from(version.next_number())
                .map_err(|_| StoreError::malformed("no object number is left for /Encrypt"))?;
            return Ok(Self::New {
                handler,
                dict,
                id,
                number,
            });
        }
        let Some(encrypt) = version.trailer().get("Encrypt") else {
            return Ok(Self::Plain);
        };
        let file = version.base().file();
        match file.and_then(|index| Some((index, index.security()?))) {
            Some((index, handler)) if version.base().trailer().get("Encrypt") == Some(encrypt) => {
                let original = handler;
                let (mut handler, selector_changed) = if let Value::Reference(reference) = encrypt
                    && let Some(Slot::Written(written)) = version.slot(reference.number())
                {
                    let Some(raw) = index.object(*reference, budget)? else {
                        return Err(Error::Refused(Refusal::Encryption));
                    };
                    let Value::Dict(mut old) = Value::parse(&raw, budget)? else {
                        return Err(Error::Refused(Refusal::Encryption));
                    };
                    let new_value = written.value(budget)?;
                    let Value::Dict(new) = &*new_value else {
                        return Err(Error::Refused(Refusal::Encryption));
                    };
                    let mut other = new.clone();
                    let old_eff = old.remove("EFF");
                    let new_eff = other.remove("EFF");
                    if old != other {
                        return Err(Error::Refused(Refusal::Encryption));
                    }
                    if old_eff == new_eff {
                        (Cow::Borrowed(handler), false)
                    } else {
                        let selected = handler.with_embedded_filter(new)?;
                        let changed = !selected.same_embedded_filter(handler);
                        (Cow::Owned(selected), changed)
                    }
                } else {
                    (Cow::Borrowed(handler), false)
                };
                if selector_changed
                    || handler.distinguishes_embedded_files() && version.has_changes()
                {
                    handler
                        .to_mut()
                        .remember_file_roles(security::embedded::discover(
                            index,
                            Some(version),
                            budget,
                        )?);
                }
                let changed_streams = if version.has_changes() {
                    handler.changed_streams(version, index, original, selector_changed, budget)?
                } else {
                    Vec::new()
                };
                Ok(Self::Keep {
                    handler,
                    changed_streams,
                    index,
                    encrypt: match encrypt {
                        Value::Reference(reference) => Some(reference.number()),
                        _ => None,
                    },
                })
            }
            _ => Err(Error::Refused(Refusal::Encryption)),
        }
    }

    fn removing_encryption(&self, version: &Version) -> bool {
        matches!(self, Self::Plain) && version.base().file().is_some_and(Index::is_encrypted)
    }

    fn changed_streams(&self) -> &[u32] {
        match self {
            Self::Keep {
                changed_streams, ..
            } => changed_streams,
            _ => &[],
        }
    }

    fn header<'v>(&self, version: &'v Version, xref_stream: bool, mac: bool) -> &'v [u8] {
        let original = version
            .base()
            .header_bytes()
            .and_then(|bytes| bytes.get(..8))
            .filter(|bytes| bytes.starts_with(b"%PDF-"))
            .unwrap_or(b"%PDF-1.7");
        if (mac || self.handler().is_some_and(Handler::uses_gcm)) && original < b"%PDF-2.0" {
            b"%PDF-2.0"
        } else if xref_stream && original < b"%PDF-1.5" {
            b"%PDF-1.5"
        } else {
            original
        }
    }

    fn handler(&self) -> Option<&Handler> {
        match self {
            Self::Plain => None,
            Self::Keep { handler, .. } => Some(handler),
            Self::New { handler, .. } => Some(handler),
        }
    }
}

fn plain_body(bytes: Bytes, version: &Version, budget: &Budget) -> Result<Bytes> {
    if version.base().file().is_some_and(Index::is_encrypted) {
        return Ok(security::filters::plain(
            bytes,
            &|r, b| version.filter_object(r, b),
            budget,
        )?);
    }
    Ok(bytes)
}

fn row(version: &Version, number: u32) -> Row {
    if number == 0 {
        return Row {
            number,
            generation: u16::MAX,
            offset: None,
            next_free: 0,
            member: None,
        };
    }
    let generation = version.generation(number);
    let free = match version.slot(number) {
        Some(Slot::Free(generation)) => *generation,
        _ => version.base().free_generation(number).unwrap_or(0),
    };
    Row {
        number,
        generation: generation.unwrap_or(free),
        offset: generation.map(|_| 0),
        next_free: 0,
        member: None,
    }
}

fn write_object(
    version: &Version,
    row: &mut Row,
    renumbering: Option<&Renumbering<'_>>,
    out: &mut Output<'_>,
    mode: &Mode<'_>,
    policy: &StreamPolicy<'_>,
    format: Format,
) -> Result<()> {
    out.budget.poll()?;
    if row.offset.is_none() {
        return Ok(());
    }
    if let Some(renumbering) = renumbering {
        let target = renumbering.number(row.number)?;
        let reference = Reference::new(row.number, row.generation);
        let plain = renumbering.bytes(
            version,
            reference,
            policy,
            format,
            mode.removing_encryption(version),
            out.budget,
        )?;
        // The keys of an encrypted object derive from its new reference.
        let bytes = match (mode, mode.handler()) {
            (Mode::Keep { encrypt, .. }, _) if Some(row.number) == *encrypt => plain,
            (_, Some(handler)) => security::encrypt_bytes_as(
                handler,
                reference,
                target,
                &plain,
                &|r, b| renumbering.lookup(version, r, b),
                out.budget,
            )?,
            (_, None) => plain,
        };
        row.offset = Some(out.offset()?);
        out.display(format_args!(
            "{} {} obj\n",
            target.number(),
            target.generation()
        ))?;
        out.bytes(&bytes)?;
        return out.bytes(b"\nendobj\n");
    }
    let reference = Reference::new(row.number, row.generation);
    let budget = out.budget;
    let context =
        |e: StoreError| e.context(format!("saving object {} {}", row.number, row.generation));
    let bytes = if format.rewrites_syntax() {
        formatted_object(version, reference, policy, format, mode, budget).map_err(|error| {
            error.context(format!("saving object {} {}", row.number, row.generation))
        })?
    } else {
        let bytes = match (version.slot(row.number), mode) {
            (Some(Slot::Written(object)), Mode::Plain) => {
                version.check_stream_filters(&*object.value(budget)?, budget)?;
                plain_body(object.bytes().clone(), version, budget)?
            }
            (Some(Slot::Written(object)), Mode::Keep { encrypt, .. })
                if Some(row.number) == *encrypt =>
            {
                object.bytes().clone()
            }
            (Some(Slot::Written(object)), _) => {
                let handler = mode.handler().expect("an encrypting mode has a handler");
                security::encrypt_bytes_as(
                    handler,
                    reference,
                    reference,
                    object.bytes(),
                    &|r, b| version.filter_object(r, b),
                    budget,
                )
                .map_err(context)?
            }
            (_, Mode::Keep { index, encrypt, .. })
                if Some(row.number) == *encrypt
                    || (mode.changed_streams().binary_search(&row.number).is_err()
                        && matches!(
                            index.get(row.number).map(|entry| entry.location()),
                            Some(Location::Offset(_))
                        )) =>
            {
                raw::file_body(index, reference, budget).map_err(context)?
            }
            (_, Mode::Plain) => plain_body(
                raw::body(&**version.base(), reference, budget).map_err(context)?,
                version,
                budget,
            )?,
            (_, _) => {
                let handler = mode.handler().expect("an encrypting mode has a handler");
                let plain = raw::body(&**version.base(), reference, budget).map_err(context)?;
                security::encrypt_bytes_as(
                    handler,
                    reference,
                    reference,
                    &plain,
                    &|r, b| version.filter_object(r, b),
                    budget,
                )
                .map_err(context)?
            }
        };
        match recode(version, reference, policy, format, mode, budget).map_err(|error| {
            error.context(format!("saving object {} {}", row.number, row.generation))
        })? {
            // `compress=True` replaces the data only where it is shorter.
            Some(recoded)
                if !policy.requires_shorter(row.number) || recoded.len() < bytes.len() =>
            {
                recoded
            }
            _ => bytes,
        }
    };
    row.offset = Some(out.offset()?);
    out.display(format_args!("{} {} obj\n", row.number, row.generation))?;
    out.bytes(&bytes)?;
    out.bytes(b"\nendobj\n")
}

/// Serializes `reference` through the object writer for `ascii`/`pretty`
/// output, recoding streams per `policy` and wrapping binary streams in
/// `ASCII85Decode` when `format.ascii` is set.
fn formatted_object(
    version: &Version,
    reference: Reference,
    policy: &StreamPolicy<'_>,
    format: Format,
    mode: &Mode<'_>,
    budget: &Budget,
) -> Result<Bytes> {
    let object = match version.get(reference.number(), budget) {
        Ok(Some(object)) => object,
        Ok(None) => return Ok(Bytes::default()),
        Err(error) if matches!(error.kind(), ErrorKind::Malformed(_)) => {
            let raw_bytes = match version.slot(reference.number()) {
                Some(Slot::Written(written)) => written.bytes().clone(),
                _ => raw::body(&**version.base(), reference, budget)?,
            };
            if format.ascii && !recode::is_printable_ascii(&raw_bytes) {
                return Err(error.into());
            }
            return Ok(raw_bytes);
        }
        Err(error) => return Err(error.into()),
    };
    let mut clear;
    let value = if mode.removing_encryption(version)
        && matches!(object.value().as_ref(), Value::Stream(_))
    {
        clear = crate::value::copy_for_edit(object.value(), budget)?;
        security::filters::remove_explicit(
            &mut clear,
            &|r, b| version.filter_object(r, b),
            budget,
        )?;
        &clear
    } else {
        if matches!(mode, Mode::Plain)
            && matches!(version.slot(reference.number()), Some(Slot::Written(_)))
        {
            version.check_stream_filters(object.value(), budget)?;
        }
        object.value().as_ref()
    };
    let recoded = recode::apply(
        value,
        reference.number(),
        policy,
        format.ascii,
        &|r, b| version.filter_object(r, b),
        budget,
    )?;
    let plain = match recoded {
        Some(recoded) if policy.requires_shorter(reference.number()) && !format.ascii => {
            let mut before = BudgetVec::new(budget);
            format.write_value(&mut before, value)?;
            let mut after = BudgetVec::new(budget);
            format.write_value(&mut after, &recoded)?;
            if after.len() < before.len() {
                Bytes::new(after.into_vec())
            } else {
                Bytes::new(before.into_vec())
            }
        }
        Some(recoded) => {
            let mut out = BudgetVec::new(budget);
            format.write_value(&mut out, &recoded)?;
            Bytes::new(out.into_vec())
        }
        None => {
            let mut out = BudgetVec::new(budget);
            format.write_value(&mut out, value)?;
            Bytes::new(out.into_vec())
        }
    };
    Ok(match (mode, mode.handler()) {
        (Mode::Keep { encrypt, .. }, _) if Some(reference.number()) == *encrypt => plain,
        (_, Some(handler)) => security::encrypt_bytes_as(
            handler,
            reference,
            reference,
            &plain,
            &|r, b| version.filter_object(r, b),
            budget,
        )?,
        (_, None) => plain,
    })
}

/// Returns the bytes of the object `reference` with its stream recoded as
/// `policy` asks, encrypted as `mode` encrypts, or `None` when it stays as
/// it is. An object that does not parse stays as it is.
fn recode(
    version: &Version,
    reference: Reference,
    policy: &StreamPolicy<'_>,
    format: Format,
    mode: &Mode<'_>,
    budget: &Budget,
) -> Result<Option<Bytes>> {
    if policy.is_keep(reference.number()) && !format.ascii {
        return Ok(None);
    }
    let object = match version.get(reference.number(), budget) {
        Ok(Some(object)) => object,
        Ok(None) => return Ok(None),
        Err(error) if matches!(error.kind(), ErrorKind::Malformed(_)) => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    let mut clear;
    let value = if mode.removing_encryption(version)
        && matches!(object.value().as_ref(), Value::Stream(_))
    {
        clear = crate::value::copy_for_edit(object.value(), budget)?;
        security::filters::remove_explicit(
            &mut clear,
            &|r, b| version.filter_object(r, b),
            budget,
        )?;
        &clear
    } else {
        object.value().as_ref()
    };
    let Some(value) = recode::apply(
        value,
        reference.number(),
        policy,
        format.ascii,
        &|r, b| version.filter_object(r, b),
        budget,
    )?
    else {
        return Ok(None);
    };
    let mut plain = BudgetVec::new(budget);
    format.write_value(&mut plain, &value)?;
    let plain = Bytes::new(plain.into_vec());
    Ok(Some(match mode.handler() {
        Some(handler) => security::encrypt_bytes_as(
            handler,
            reference,
            reference,
            &plain,
            &|r, b| version.filter_object(r, b),
            budget,
        )?,
        None => plain,
    }))
}

/// How [`finish`] ends a save.
struct Ending<'a> {
    /// The offset of the base's last cross-reference section, for an
    /// incremental save.
    previous: Option<u64>,
    /// The final table points to a supplemental cross-reference stream.
    hybrid: bool,
    /// The `/Size` of a save that writes no cross-reference stream and no
    /// new encryption.
    size: u64,
    renew_id: bool,
    format: Format,
    /// The object number of the cross-reference stream and the Flate level
    /// of its data, for a save that writes one instead of a table.
    stream: Option<(u32, Option<u8>)>,
    /// The renumbering of a save that renumbers, which the trailer's
    /// references follow.
    renumbering: Option<&'a Renumbering<'a>>,
}

fn finish(
    version: &Version,
    rows: &mut [Row],
    out: &mut Output<'_>,
    mode: &Mode<'_>,
    ending: Ending<'_>,
) -> Result<()> {
    let mut next_free = 0;
    for row in rows.iter_mut().rev() {
        out.budget.poll()?;
        if row.offset.is_none() {
            row.next_free = next_free;
            if row.generation != u16::MAX {
                next_free = row.number;
            }
        }
    }
    if ending.hybrid {
        return hybrid::finish(version, rows, out, mode, &ending);
    }
    let xref = out.offset()?;
    let mut size = match mode {
        Mode::New { number, .. } => u64::from(*number) + 1,
        _ => ending.size,
    };
    let sep = if ending.format.pretty { "\n  " } else { " " };
    let close = if ending.format.pretty { "\n" } else { " " };
    if let Some((number, _)) = ending.stream {
        // The cross-reference stream takes the last number.
        size = u64::from(number) + 1;
        out.display(format_args!("{number} 0 obj\n<<{sep}/Type /XRef"))?;
    } else {
        table(rows, out)?;
        out.bytes(b"trailer\n<<")?;
    }
    trailer_fields(version, mode, out, &ending, &mut None)?;
    out.display(format_args!("{sep}/Size {size}"))?;
    if let Some(previous) = ending.previous {
        out.display(format_args!("{sep}/Prev {previous}"))?;
    }
    out.authentication()?;
    if let Some((number, level)) = ending.stream {
        let own = Row {
            number,
            generation: 0,
            offset: Some(xref),
            next_free: 0,
            member: None,
        };
        stream(rows.iter(), &own, level, ending.format, out)?;
        out.display(format_args!("startxref\n{xref}\n%%EOF\n"))
    } else {
        out.display(format_args!("{close}>>\nstartxref\n{xref}\n%%EOF\n"))
    }
}

/// Writes the document trailer keys. A hybrid repeats the same identifier
/// in both its classic main trailer and its final update.
fn trailer_fields(
    version: &Version,
    mode: &Mode<'_>,
    out: &mut Output<'_>,
    ending: &Ending<'_>,
    digest: &mut Option<Value>,
) -> Result<()> {
    let sep = if ending.format.pretty { "\n  " } else { " " };
    if let Mode::New { id, number, .. } = mode {
        trailer(
            version.trailer(),
            out,
            &[b"Encrypt", b"ID"],
            ending.renumbering,
            ending.format,
        )?;
        out.display(format_args!("{sep}/Encrypt {number} 0 R"))?;
        // The keys derive from the first element.
        let first = Value::String(PdfString::hexadecimal(id.as_slice()));
        let second = digest
            .get_or_insert_with(|| out.digest().unwrap_or_else(|| first.clone()))
            .clone();
        identifier(out, first, second, ending.format)?;
    } else if let Some(first) = renews(version, ending.renew_id) {
        trailer(
            version.trailer(),
            out,
            &[b"ID"],
            ending.renumbering,
            ending.format,
        )?;
        let digest = digest
            .get_or_insert_with(|| out.digest().expect("a renewing save keeps a digest"))
            .clone();
        identifier(
            out,
            first.unwrap_or_else(|| digest.clone()),
            digest,
            ending.format,
        )?;
    } else {
        trailer(
            version.trailer(),
            out,
            &[],
            ending.renumbering,
            ending.format,
        )?;
    }
    Ok(())
}

/// Writes the rows as a classic cross-reference table.
fn table(rows: &[Row], out: &mut Output<'_>) -> Result<()> {
    out.bytes(b"xref\n")?;
    let mut start = 0;
    while start < rows.len() {
        let mut end = start + 1;
        while end < rows.len() && rows[end - 1].number.checked_add(1) == Some(rows[end].number) {
            end += 1;
        }
        out.display(format_args!("{} {}\n", rows[start].number, end - start))?;
        for row in &rows[start..end] {
            table_row(row, out)?;
        }
        start = end;
    }
    Ok(())
}

fn table_row(row: &Row, out: &mut Output<'_>) -> Result<()> {
    match row.offset {
        Some(offset) => {
            if offset > 9_999_999_999 {
                return Err(StoreError::malformed(
                    "an offset exceeds the classic xref table's ten digits",
                )
                .into());
            }
            out.display(format_args!("{offset:010} {:05} n \n", row.generation))?;
        }
        None => {
            let next = row.next_free;
            out.display(format_args!("{next:010} {:05} f \n", row.generation))?;
        }
    }
    Ok(())
}

/// Returns the fields of the cross-reference stream entry of `row` (ISO
/// 32000-2 Table 18): its type, then the two fields that the type defines.
fn fields(row: &Row) -> (u8, u64, u64) {
    match (row.offset, row.member) {
        (_, Some((stream, index))) => (2, u64::from(stream), u64::from(index)),
        (Some(offset), None) => (1, offset, u64::from(row.generation)),
        (None, _) => (0, u64::from(row.next_free), u64::from(row.generation)),
    }
}

/// Returns the fewest bytes (`1..=8`) required to store an xref stream field
/// whose largest value is `max`.
const fn xref_field_width(max: u64) -> usize {
    let bits = u64::BITS - max.leading_zeros();
    let bytes = bits.div_ceil(8);
    if bytes == 0 { 1 } else { bytes as usize }
}

/// Writes the entries of a cross-reference stream that describe its data,
/// which lists the rows and then the stream itself, `own`, and then the
/// data and the end of the stream's object.
fn stream<'a>(
    rows: impl Iterator<Item = &'a Row> + Clone,
    own: &'a Row,
    level: Option<u8>,
    format: Format,
    out: &mut Output<'_>,
) -> Result<()> {
    let budget = out.budget;
    let all = || rows.clone().chain(std::iter::once(own));
    let (second, third) = all().fold((0, 0), |(second, third), row| {
        let (_, two, three) = fields(row);
        (two.max(second), three.max(third))
    });
    let (second, third) = (xref_field_width(second), xref_field_width(third));
    let mut data = BudgetVec::new(budget);
    for row in all() {
        budget.poll()?;
        let (kind, two, three) = fields(row);
        data.try_push(kind)?;
        data.try_extend_from_slice(&two.to_be_bytes()[8 - second..])?;
        data.try_extend_from_slice(&three.to_be_bytes()[8 - third..])?;
    }
    let data = Stream::new(Dict::new(), Bytes::new(data.into_vec()));
    let compressed = match level {
        Some(level) => recode::compressed(&data, level, budget)?,
        None => None,
    };
    let sep = if format.pretty { "\n  " } else { " " };
    let close = if format.pretty { "\n" } else { " " };
    out.display(format_args!("{sep}/W [1 {second} {third}]{sep}/Index ["))?;
    let mut run: Option<(u32, u32)> = None;
    let mut first_run = true;
    for row in all() {
        budget.poll()?;
        match &mut run {
            Some((start, count)) if start.checked_add(*count) == Some(row.number) => *count += 1,
            _ => {
                if let Some((start, count)) = run {
                    if format.pretty {
                        if !first_run {
                            out.bytes(b" ")?;
                        }
                        out.display(format_args!("{start} {count}"))?;
                        first_run = false;
                    } else {
                        out.display(format_args!(" {start} {count}"))?;
                    }
                }
                run = Some((row.number, 1));
            }
        }
    }
    if let Some((start, count)) = run {
        if format.pretty {
            if !first_run {
                out.bytes(b" ")?;
            }
            out.display(format_args!("{start} {count}"))?;
        } else {
            out.display(format_args!(" {start} {count}"))?;
        }
    }
    let raw_data = compressed.as_ref().unwrap_or(&data).data();
    let ascii_wrapped = if format.ascii {
        Some(encode::ascii85(
            raw_data,
            Some(recode::ASCII_WRAP_COLUMNS),
            budget,
        )?)
    } else {
        None
    };
    let payload: &[u8] = ascii_wrapped.as_deref().unwrap_or(raw_data);
    let close_index = if format.pretty { "]" } else { " ]" };
    out.display(format_args!("{close_index}{sep}/Length {}", payload.len()))?;
    match (ascii_wrapped.is_some(), compressed.is_some()) {
        (true, true) => out.display(format_args!("{sep}/Filter [/ASCII85Decode /FlateDecode]"))?,
        (true, false) => out.display(format_args!("{sep}/Filter /ASCII85Decode"))?,
        (false, true) => out.display(format_args!("{sep}/Filter /FlateDecode"))?,
        (false, false) => {}
    }
    out.display(format_args!("{close}>>\nstream\n"))?;
    out.bytes(payload)?;
    out.bytes(b"\nendstream\nendobj\n")
}

/// Write each original trailer entry separately, avoiding a clone of its
/// input-derived dictionary. Layout-only entries are rebuilt for this save.
/// Returns whether a save that `renew_id` asks for writes a new `/ID` for
/// `version`, and then the first element that it keeps, if any: the
/// trailer's for a version with changes, none for a trailer without one.
fn renews(version: &Version, renew_id: bool) -> Option<Option<Value>> {
    if !renew_id {
        return None;
    }
    match version.trailer().get("ID") {
        Some(Value::Array(id)) => match id.get(0) {
            Some(first @ Value::String(_)) => version.has_changes().then(|| Some(first.clone())),
            _ => Some(None),
        },
        _ => Some(None),
    }
}

/// Writes the trailer entry `/ID [first second]`.
fn identifier(out: &mut Output<'_>, first: Value, second: Value, format: Format) -> Result<()> {
    let mut bytes = BudgetVec::new(out.budget);
    format.write_trailer_value(&mut bytes, &Value::Array(Array::from(vec![first, second])))?;
    if format.pretty {
        out.bytes(b"\n  /ID ")?;
    } else {
        out.bytes(b" /ID ")?;
    }
    out.bytes(&bytes)
}

/// Returns whether a save copies the trailer entry `name`: the entries of
/// cross-reference data are its own.
fn trailer_key(name: &[u8]) -> bool {
    !matches!(
        name,
        b"Size"
            | b"Prev"
            | b"XRefStm"
            | b"Type"
            | b"W"
            | b"Index"
            | b"Length"
            | b"Filter"
            | b"DecodeParms"
            | b"DL"
    )
}

fn trailer(
    dict: &Dict,
    out: &mut Output<'_>,
    replaced: &[&[u8]],
    renumbering: Option<&Renumbering<'_>>,
    format: Format,
) -> Result<()> {
    let sep: &[u8] = if format.pretty { b"\n  " } else { b" " };
    for (name, value) in dict.iter() {
        if replaced.contains(&name.as_bytes())
            || ((out.mac.is_some() || out.attached.is_some()) && name.as_bytes() == b"AuthCode")
        {
            continue;
        }
        if !trailer_key(name.as_bytes()) {
            continue;
        }
        out.budget.poll()?;
        let mut bytes = BudgetVec::new(out.budget);
        // Names are bounded by the same operation before their small clone.
        out.budget
            .charge(LimitCode::MemorySize, name.as_bytes().len() as u64)?;
        format.write_trailer_value(&mut bytes, &Value::Name(name.clone()))?;
        bytes.try_push(b' ')?;
        match renumbering {
            Some(renumbering) => {
                format.write_trailer_value(&mut bytes, &renumbering.copy(value, out.budget)?)?;
            }
            None => format.write_trailer_value(&mut bytes, value)?,
        }
        out.bytes(sep)?;
        out.bytes(&bytes)?;
    }
    Ok(())
}

/// A streaming sink that checks cancellation between bounded chunks, and
/// charges each chunk before sending it to the caller's writer.
struct Output<'a> {
    target: &'a mut dyn Write,
    budget: &'a Budget,
    position: u64,
    origin: u64,
    /// The digest of the bytes written, for a save that renews `/ID`.
    digest: Option<Md5>,
    /// Whether the bytes count against the output size: not those of a
    /// pass that only measures.
    charge: bool,
    mac: Option<integrity::MacOutput<'a>>,
    attached: Option<Reference>,
}

impl<'a> Output<'a> {
    fn new(target: &'a mut dyn Write, budget: &'a Budget, origin: u64, digest: bool) -> Self {
        Self {
            target,
            budget,
            position: 0,
            origin,
            digest: digest.then(Md5::new),
            charge: true,
            mac: None,
            attached: None,
        }
    }

    /// Returns the digest of the bytes written so far as a string, for a
    /// save that renews `/ID`.
    fn digest(&self) -> Option<Value> {
        let digest = self.digest.clone()?.finalize();
        Some(Value::String(PdfString::hexadecimal(digest.as_slice())))
    }

    fn offset(&self) -> Result<u64> {
        self.position
            .checked_sub(self.origin)
            .ok_or_else(|| StoreError::malformed("output precedes its PDF header").into())
    }

    fn bytes(&mut self, bytes: &[u8]) -> Result<()> {
        for chunk in bytes.chunks(64 * 1024) {
            self.budget.poll()?;
            if self.charge {
                self.budget
                    .charge(LimitCode::PdfOutputSize, chunk.len() as u64)?;
            }
            if let Some(mac) = &mut self.mac {
                mac.write(chunk, self.target)?;
            } else {
                self.target.write_all(chunk)?;
            }
            if let Some(digest) = &mut self.digest {
                digest.update(chunk);
            }
            self.position = self
                .position
                .checked_add(chunk.len() as u64)
                .ok_or_else(|| StoreError::malformed("PDF output size overflow"))?;
        }
        Ok(())
    }

    fn authentication(&mut self) -> Result<()> {
        if let Some(r) = self.attached {
            self.display(format_args!(
                " /AuthCode << /MACLocation /AttachedToSig /SigObjRef {} {} R >>",
                r.number(),
                r.generation()
            ))
        } else {
            integrity::MacOutput::reserve(self)
        }
    }

    fn display(&mut self, args: fmt::Arguments<'_>) -> Result<()> {
        // Call sites format fixed numbers and keywords only (under 128
        // bytes), never an input-sized string or object.
        self.bytes(fmt::format(args).as_bytes())
    }
}

#[cfg(kani)]
mod kani_proofs {
    use super::*;

    /// `xref_field_width` selects the minimal byte width in `1..=8` so that
    /// `&value.to_be_bytes()[8 - w..]` preserves every bit of `value` for all `u64`.
    #[kani::proof]
    #[kani::unwind(10)]
    fn kani_save_xref_field_width_roundtrip() {
        let value: u64 = kani::any();
        let w = xref_field_width(value);
        assert!((1..=8).contains(&w));
        let bytes = value.to_be_bytes();
        // Every dropped high byte must be zero.
        for &b in &bytes[..8 - w] {
            assert_eq!(b, 0);
        }
        // Width is minimal: if w > 1, the leading kept byte is non-zero.
        if w > 1 {
            assert_ne!(bytes[8 - w], 0);
        }
        // Big-endian decoding recovers `value` identically.
        let decoded = bytes[8 - w..]
            .iter()
            .fold(0_u64, |acc, &b| (acc << 8) | u64::from(b));
        assert_eq!(decoded, value);
    }
}
