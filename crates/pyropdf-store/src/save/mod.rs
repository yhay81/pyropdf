//! Full and incremental saves of immutable versions (DESIGN.md section 4.4).
//!
//! Untouched values keep their original bytes; written values are copied as
//! written. Output goes directly to any [`Write`], without a second PDF in
//! memory. Metadata and output share the operation's [`Budget`]. Saving
//! never changes the version, its base, or its object allocation state.

mod base;
mod prune;
mod raw;

#[cfg(test)]
mod tests;

use std::fmt;
use std::io::{self, Write};

use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::index::{Index, Location};
use crate::security::{self, Encryption, Handler};
use crate::value::{Dict, PdfString, Reference, Value};
use crate::version::{Slot, Version};
use crate::{Error as StoreError, LimitExceeded, write};

pub use base::FileBase;

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
}

/// The result of saving to an output writer.
pub type Result<T> = std::result::Result<T, Error>;

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
}

/// Writes all live objects to `target`, with a classic cross-reference
/// table and a new trailer. Object-stream members become ordinary objects;
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
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Options {
    /// Writes only the objects that the trailer reaches, and the numbers of
    /// the others as free entries.
    pub prune: bool,
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
    let mode = Mode::of(version, encryption, budget)?;
    budget.poll()?;
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
    let mut out = Output::new(target, budget, 0);
    let header = version
        .base()
        .file()
        .and_then(|file| file.bytes().get(file.origin()..file.origin() + 8))
        .filter(|bytes| bytes.starts_with(b"%PDF-"));
    out.bytes(header.unwrap_or(b"%PDF-1.7"))?;
    out.bytes(b"\n%\xE2\xE3\xCF\xD3\n")?;
    // A copied linearization dictionary must not remain the first object:
    // its old offsets describe the base, not this full save. Write the
    // catalog first, retaining every object's value and number unchanged.
    let root = match version.trailer().get("Root") {
        Some(Value::Reference(root)) => Some(root.number()),
        _ => None,
    };
    if let Some(at) = root.and_then(|root| rows.binary_search_by_key(&root, |row| row.number).ok())
    {
        write_object(version, &mut rows[at], &mut out, &mode)?;
    }
    for row in &mut rows {
        if Some(row.number) != root {
            write_object(version, row, &mut out, &mode)?;
        }
    }
    if let Mode::New { dict, number, .. } = &mode {
        // The new /Encrypt dictionary, which is never encrypted, takes the
        // number after every other.
        let mut row = Row {
            number: *number,
            generation: 0,
            offset: Some(out.offset()?),
            next_free: 0,
        };
        let mut bytes = BudgetVec::new(budget);
        write::value(&mut bytes, &Value::Dict(dict.clone()))?;
        out.display(format_args!("{number} 0 obj\n"))?;
        out.bytes(&bytes)?;
        out.bytes(b"\nendobj\n")?;
        row.next_free = 0;
        rows.try_push(row)?;
    }
    finish(version, &mut rows, None, &mut out, &mode)?;
    Ok(out.position)
}

/// Writes the original file byte for byte, followed only by the latest
/// slots written since the base and a cross-reference section with /Prev.
/// No-edit saves (including undo to the initial version) reproduce the
/// original bytes exactly and do not parse any objects.
///
/// Refuses repaired and non-file bases and encryption before writing.
/// Returns bytes written; partial output after an error must be discarded.
pub fn incremental(version: &Version, target: &mut dyn Write, budget: &Budget) -> Result<u64> {
    let file = version
        .base()
        .file()
        .ok_or(Error::Refused(Refusal::NoOriginalFile))?;
    if file.is_repaired() {
        return Err(Error::Refused(Refusal::RepairedBase));
    }
    let mode = Mode::of(version, None, budget)?;
    let previous = file
        .revisions()
        .last()
        .and_then(|r| r.sections().first())
        .and_then(|s| s.offset().checked_sub(file.origin()))
        .ok_or_else(|| StoreError::malformed("the original file has no cross-reference offset"))?;
    let mut out = Output::new(target, budget, file.origin() as u64);
    out.bytes(file.bytes())?;
    if !version.has_changes() {
        return Ok(out.position);
    }
    out.bytes(b"\n")?;
    let mut rows = BudgetVec::new(budget);
    // Include the head of the newly freed entries. Retired entries are not
    // reusable. Older free entries remain defined by previous sections.
    rows.try_push(row(version, 0))?;
    for (number, _) in version.slots() {
        budget.poll()?;
        if number != 0 {
            let mut row = row(version, number);
            write_object(version, &mut row, &mut out, &mode)?;
            rows.try_push(row)?;
        }
    }
    if let Ok(last) = u32::try_from(version.next_number().saturating_sub(1))
        && version.generation(last).is_none()
        && rows.last().is_some_and(|row| row.number < last)
    {
        rows.try_push(row(version, last))?;
    }
    finish(version, &mut rows, Some(previous as u64), &mut out, &mode)?;
    Ok(out.position)
}

/// How a save treats encryption.
enum Mode<'a> {
    /// No encryption: the version's trailer names no `/Encrypt`. An
    /// unlocked encrypted base reads decrypted.
    Plain,
    /// The base's own encryption: untouched objects of the file keep their
    /// encrypted bytes, and the rest are encrypted with its handler.
    Keep {
        handler: &'a Handler,
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
                Ok(Self::Keep {
                    handler,
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

    fn handler(&self) -> Option<&Handler> {
        match self {
            Self::Plain => None,
            Self::Keep { handler, .. } => Some(handler),
            Self::New { handler, .. } => Some(handler),
        }
    }
}

fn row(version: &Version, number: u32) -> Row {
    if number == 0 {
        return Row {
            number,
            generation: u16::MAX,
            offset: None,
            next_free: 0,
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
    }
}

fn write_object(
    version: &Version,
    row: &mut Row,
    out: &mut Output<'_>,
    mode: &Mode<'_>,
) -> Result<()> {
    out.budget.poll()?;
    if row.offset.is_none() {
        return Ok(());
    }
    let reference = Reference::new(row.number, row.generation);
    let budget = out.budget;
    let context =
        |e: StoreError| e.context(format!("saving object {} {}", row.number, row.generation));
    let bytes = match (version.slot(row.number), mode) {
        (Some(Slot::Written(object)), Mode::Plain) => object.bytes().clone(),
        (Some(Slot::Written(object)), _) => {
            let handler = mode.handler().expect("an encrypting mode has a handler");
            security::encrypt_bytes(handler, reference, object.bytes(), budget).map_err(context)?
        }
        (_, Mode::Keep { index, encrypt, .. })
            if Some(row.number) == *encrypt
                || matches!(
                    index.get(row.number).map(|entry| entry.location()),
                    Some(Location::Offset(_))
                ) =>
        {
            raw::file_body(index, reference, budget).map_err(context)?
        }
        (_, Mode::Plain) => raw::body(&**version.base(), reference, budget).map_err(context)?,
        (_, _) => {
            let handler = mode.handler().expect("an encrypting mode has a handler");
            let plain = raw::body(&**version.base(), reference, budget).map_err(context)?;
            security::encrypt_bytes(handler, reference, &plain, budget).map_err(context)?
        }
    };
    row.offset = Some(out.offset()?);
    out.display(format_args!("{} {} obj\n", row.number, row.generation))?;
    out.bytes(&bytes)?;
    out.bytes(b"\nendobj\n")
}

fn finish(
    version: &Version,
    rows: &mut [Row],
    previous: Option<u64>,
    out: &mut Output<'_>,
    mode: &Mode<'_>,
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
    let xref = out.offset()?;
    out.bytes(b"xref\n")?;
    let mut start = 0;
    while start < rows.len() {
        let mut end = start + 1;
        while end < rows.len() && rows[end - 1].number.checked_add(1) == Some(rows[end].number) {
            end += 1;
        }
        out.display(format_args!("{} {}\n", rows[start].number, end - start))?;
        for row in &rows[start..end] {
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
        }
        start = end;
    }
    out.bytes(b"trailer\n<<")?;
    let mut size = version.next_number();
    if let Mode::New { id, number, .. } = mode {
        trailer(version.trailer(), out, &[b"Encrypt", b"ID"])?;
        out.display(format_args!(" /Encrypt {number} 0 R /ID ["))?;
        for _ in 0..2 {
            let mut bytes = BudgetVec::new(out.budget);
            write::value(
                &mut bytes,
                &Value::String(PdfString::hexadecimal(id.as_slice())),
            )?;
            out.bytes(&bytes)?;
        }
        out.bytes(b"]")?;
        size = u64::from(*number) + 1;
    } else {
        trailer(version.trailer(), out, &[])?;
    }
    out.display(format_args!(" /Size {size}"))?;
    if let Some(previous) = previous {
        out.display(format_args!(" /Prev {previous}"))?;
    }
    out.display(format_args!(" >>\nstartxref\n{xref}\n%%EOF\n"))
}

/// Write each original trailer entry separately, avoiding a clone of its
/// input-derived dictionary. Layout-only entries are rebuilt for this save.
fn trailer(dict: &Dict, out: &mut Output<'_>, replaced: &[&[u8]]) -> Result<()> {
    for (name, value) in dict.iter() {
        if replaced.contains(&name.as_bytes()) {
            continue;
        }
        if matches!(
            name.as_bytes(),
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
        ) {
            continue;
        }
        out.budget.poll()?;
        let mut bytes = BudgetVec::new(out.budget);
        // Names are bounded by the same operation before their small clone.
        out.budget
            .charge(LimitCode::MemorySize, name.as_bytes().len() as u64)?;
        write::value(&mut bytes, &Value::Name(name.clone()))?;
        bytes.try_push(b' ')?;
        write::value(&mut bytes, value)?;
        out.bytes(b" ")?;
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
}

impl<'a> Output<'a> {
    fn new(target: &'a mut dyn Write, budget: &'a Budget, origin: u64) -> Self {
        Self {
            target,
            budget,
            position: 0,
            origin,
        }
    }

    fn offset(&self) -> Result<u64> {
        self.position
            .checked_sub(self.origin)
            .ok_or_else(|| StoreError::malformed("output precedes its PDF header").into())
    }

    fn bytes(&mut self, bytes: &[u8]) -> Result<()> {
        for chunk in bytes.chunks(64 * 1024) {
            self.budget.poll()?;
            self.budget
                .charge(LimitCode::PdfOutputSize, chunk.len() as u64)?;
            self.target.write_all(chunk)?;
            self.position = self
                .position
                .checked_add(chunk.len() as u64)
                .ok_or_else(|| StoreError::malformed("PDF output size overflow"))?;
        }
        Ok(())
    }

    fn display(&mut self, args: fmt::Arguments<'_>) -> Result<()> {
        // Call sites format fixed numbers and keywords only (under 128
        // bytes), never an input-sized string or object.
        self.bytes(fmt::format(args).as_bytes())
    }
}
