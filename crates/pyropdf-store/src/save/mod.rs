//! Full and incremental saves of immutable versions (DESIGN.md section 4.4).
//!
//! Untouched values keep their original bytes; written values are copied as
//! written. Output goes directly to any [`Write`], without a second PDF in
//! memory. Metadata and output share the operation's [`Budget`]. Saving
//! never changes the version, its base, or its object allocation state.

mod base;
mod raw;

#[cfg(test)]
mod tests;

use std::fmt;
use std::io::{self, Write};

use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::value::{Dict, Reference, Value};
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
    check_encryption(version)?;
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
    let mut rows = BudgetVec::new(budget);
    for &number in &numbers {
        budget.poll()?;
        if rows.last().is_some_and(|row: &Row| row.number == number) {
            continue;
        }
        rows.try_push(row(version, number))?;
    }
    drop(numbers);
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
        write_object(version, &mut rows[at], &mut out)?;
    }
    for row in &mut rows {
        if Some(row.number) != root {
            write_object(version, row, &mut out)?;
        }
    }
    finish(version, &mut rows, None, &mut out)?;
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
    check_encryption(version)?;
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
            write_object(version, &mut row, &mut out)?;
            rows.try_push(row)?;
        }
    }
    if let Ok(last) = u32::try_from(version.next_number().saturating_sub(1))
        && version.generation(last).is_none()
        && rows.last().is_some_and(|row| row.number < last)
    {
        rows.try_push(row(version, last))?;
    }
    finish(version, &mut rows, Some(previous as u64), &mut out)?;
    Ok(out.position)
}

fn check_encryption(version: &Version) -> Result<()> {
    if version.trailer().get("Encrypt").is_some()
        || version.base().trailer().get("Encrypt").is_some()
    {
        return Err(Error::Refused(Refusal::Encryption));
    }
    Ok(())
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

fn write_object(version: &Version, row: &mut Row, out: &mut Output<'_>) -> Result<()> {
    out.budget.poll()?;
    if row.offset.is_none() {
        return Ok(());
    }
    let bytes = match version.slot(row.number) {
        Some(Slot::Written(object)) => object.bytes().clone(),
        _ => raw::body(
            &**version.base(),
            Reference::new(row.number, row.generation),
            out.budget,
        )
        .map_err(|e| e.context(format!("saving object {} {}", row.number, row.generation)))?,
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
    trailer(version.trailer(), out)?;
    out.display(format_args!(" /Size {}", version.next_number()))?;
    if let Some(previous) = previous {
        out.display(format_args!(" /Prev {previous}"))?;
    }
    out.display(format_args!(" >>\nstartxref\n{xref}\n%%EOF\n"))
}

/// Write each original trailer entry separately, avoiding a clone of its
/// input-derived dictionary. Layout-only entries are rebuilt for this save.
fn trailer(dict: &Dict, out: &mut Output<'_>) -> Result<()> {
    for (name, value) in dict.iter() {
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
