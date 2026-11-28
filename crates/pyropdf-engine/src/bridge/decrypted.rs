//! Bounded in-memory decrypted bridge fallback for encryption modes that
//! pinned `hayro-syntax` cannot decrypt directly (temporary DESIGN.md §6
//! workaround for yhay81/pyropdf#14 and #1043; tracked in #7 and #1272).
//!
//! Activated only after `pyropdf-store` has authenticated the document and
//! [`Handler::needs_decrypted_bridge`] reports one of the modes pinned
//! `hayro-syntax` cannot interpret through its UTF-8 Standard password API:
//!
//! 1. ISO/TS 32003 AES-GCM (`AESV4`, #968).
//! 2. Legacy pre-V5 public-key recipients (`adbe.pkcs7.s3`/`.s4`/V4 `.s5`, #937).
//! 3. Non-UTF-8 legacy R2–R4 byte passwords (#1043).
//! 4. Explicit legacy `/Crypt` stream filters (`V2`/`AESV2`), indirect `/Crypt`
//!    selectors, and independently keyed multi-recipient streams (#993).
//!
//! All other encrypted documents (Standard UTF-8 password PDFs without
//! explicit legacy `/Crypt` filters, and single-key V5 recipient PDFs) stay on
//! the zero-decrypt ciphertext fast path in [`super`].
//!
//! Every allocation is bounded by `LimitCode::InterpretationSize` and
//! `LimitCode::MemorySize` (F6), the store [`Version`] and saved bytes are
//! never mutated, unauthorized recipient streams are omitted as free (never
//! leaked) and refused with `Password::Recipient` whenever referenced by any
//! page's painting graph, and the ephemeral plaintext buffer is zeroed when
//! dropped.
//!
//! Removal condition (tracked in yhay81/pyropdf#7): delete this module once
//! pyropdf adopts a released `hayro-syntax` containing the I1 object-source
//! trait (`#14`, `LaurenzV/hayro#1659`) and byte passwords (`#1043`).

use std::collections::HashSet;
use std::sync::Arc;

use hayro::hayro_syntax::{LoadPdfError, Pdf, PdfData};
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode, try_insert_set};
use pyropdf_store::pages::PageIndex;
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::security;
use pyropdf_store::value::{Dict, Name, Reference, Value};
use pyropdf_store::version::{Slot, Version};
use pyropdf_store::{ErrorKind, Password, write};

use super::{Error, Result, WIDTHS, header, insert, row};

/// Maximum `/Parent` hops walked to find inherited page `/Resources`.
const MAX_PARENT_CHAIN: usize = 64;
/// Maximum nested container depth walked when scanning page references.
const MAX_WALK_DEPTH: usize = 64;
/// How often graph traversal loops poll the budget.
const POLL_INTERVAL: usize = 256;

/// Ephemeral plaintext PDF bytes handed to `hayro` and scrubbed on drop.
pub(super) struct EphemeralBytes(Vec<u8>);

impl EphemeralBytes {
    /// Opens the ephemeral unencrypted PDF in `hayro`.
    ///
    /// If opening fails, `self` is dropped and its plaintext buffer is zeroed.
    pub(super) fn open(self) -> std::result::Result<Pdf, LoadPdfError> {
        let data: PdfData = Arc::new(self).into();
        Pdf::new(data)
    }
}

impl AsRef<[u8]> for EphemeralBytes {
    fn as_ref(&self) -> &[u8] {
        &self.0
    }
}

impl Drop for EphemeralBytes {
    fn drop(&mut self) {
        self.0.fill(0);
        std::hint::black_box(&mut self.0);
    }
}

/// Write buffer whose bytes are zeroed if assembly exits early on an error.
struct ScrubbedOut<'b>(BudgetVec<'b, u8>);

impl<'b> ScrubbedOut<'b> {
    const fn new(budget: &'b Budget) -> Self {
        Self(BudgetVec::with_length_limit(
            budget,
            LimitCode::InterpretationSize,
        ))
    }

    fn into_ephemeral(mut self) -> EphemeralBytes {
        let mut owned = BudgetVec::new(self.0.budget());
        std::mem::swap(&mut self.0, &mut owned);
        EphemeralBytes(owned.into_vec())
    }
}

impl Drop for ScrubbedOut<'_> {
    fn drop(&mut self) {
        self.0.as_mut_slice().fill(0);
        std::hint::black_box(self.0.as_mut_slice());
    }
}

/// Builds an ephemeral unencrypted in-memory PDF for `version` (with `extra`
/// stand-in objects applied on top), bounded by `InterpretationSize` and
/// `MemorySize`.
pub(super) fn build(
    version: &Version,
    pages: &PageIndex,
    extra: &[(Reference, Value)],
    budget: &Budget,
) -> Result<EphemeralBytes> {
    let mut out = ScrubbedOut::new(budget);
    let header_line = version
        .base()
        .header_bytes()
        .and_then(|bytes| bytes.get(..8))
        .filter(|bytes| {
            matches!(
                *bytes,
                [b'%', b'P', b'D', b'F', b'-', b'1'..=b'9', b'.', b'0'..=b'9']
            )
        })
        .unwrap_or(b"%PDF-1.7");
    // Cross-reference streams require at least PDF 1.5 (ISO 32000-2 §7.5.8).
    let header_line = if header_line < b"%PDF-1.5" {
        b"%PDF-1.5"
    } else {
        header_line
    };
    out.0.try_extend_from_slice(header_line)?;
    out.0.try_extend_from_slice(b"\n%\xE2\xE3\xCF\xD3\n")?;

    let encrypt_number = match version.trailer().get("Encrypt") {
        Some(Value::Reference(reference)) => Some(reference.number()),
        _ => None,
    };

    let next = extra
        .last()
        .map_or(0, |(reference, _)| u64::from(reference.number()) + 1)
        .max(version.next_number());
    let size = u32::try_from(next).unwrap_or(u32::MAX);

    let mut numbers: BudgetVec<'_, u32> = BudgetVec::new(budget);
    version.base().numbers(&mut numbers)?;
    for (number, _) in version.slots() {
        budget.poll()?;
        numbers.try_push(number)?;
    }
    for (reference, _) in extra {
        budget.poll()?;
        numbers.try_push(reference.number())?;
    }
    numbers.try_push(0)?;
    numbers.sort_unstable();

    let mut rows: BudgetVec<'_, [u8; 13]> = BudgetVec::new(budget);
    let mut index: BudgetVec<'_, Value> = BudgetVec::new(budget);
    let mut unauthorized: BudgetVec<'_, u32> = BudgetVec::new(budget);
    let mut run: Option<(u32, u32)> = None;

    for &number in numbers.iter() {
        if number >= size || run.is_some_and(|(start, count)| number < start + count) {
            budget.poll()?;
            continue;
        }
        // Each pending row will add 13 bytes to the same input.
        let row_bytes = (rows.len() as u64 + 1) * 13;
        budget.check(
            LimitCode::InterpretationSize,
            (out.0.len() as u64).saturating_add(row_bytes),
        )?;
        run = match run {
            Some((start, count)) if start + count == number => Some((start, count + 1)),
            _ => {
                if let Some((start, count)) = run {
                    index.try_push(Value::Integer(i64::from(start)))?;
                    index.try_push(Value::Integer(i64::from(count)))?;
                }
                Some((number, 1))
            }
        };
        if number == 0 {
            rows.try_push(row(0, 0, 65535))?;
            continue;
        }
        if Some(number) == encrypt_number {
            rows.try_push(row(0, 0, 0))?;
            continue;
        }
        let stand_in = extra
            .binary_search_by_key(&number, |(reference, _)| reference.number())
            .ok()
            .map(|at| &extra[at]);
        let entry = if let Some((reference, value)) = stand_in {
            write_plain_object(&mut out.0, *reference, value, version, budget)?
        } else {
            match version.slot(number) {
                Some(Slot::Free(_)) => row(0, 0, 0),
                _ => {
                    // Active object-stream containers do not need to be emitted:
                    // their decrypted members are emitted individually above/below.
                    if version.slot(number).is_none()
                        && let Some(file) = version.base().file()
                    {
                        match file.object_stream(number, budget) {
                            Ok(Some(_)) => {
                                rows.try_push(row(0, 0, 0))?;
                                continue;
                            }
                            Ok(None) => {}
                            Err(error) if is_unauthorized_recipient(&error) => {
                                unauthorized.try_push(number)?;
                                rows.try_push(row(0, 0, 0))?;
                                continue;
                            }
                            Err(error) => return Err(error.into()),
                        }
                    }
                    match version.get(number, budget) {
                        Ok(Some(object)) => {
                            if is_structural_stream(object.value()) {
                                row(0, 0, 0)
                            } else {
                                let reference = Reference::new(number, object.generation());
                                write_plain_object(
                                    &mut out.0,
                                    reference,
                                    object.value(),
                                    version,
                                    budget,
                                )?
                            }
                        }
                        Ok(None) => row(0, 0, 0),
                        Err(error) if is_unauthorized_recipient(&error) => {
                            unauthorized.try_push(number)?;
                            row(0, 0, 0)
                        }
                        Err(error) => return Err(error.into()),
                    }
                }
            }
        };
        rows.try_push(entry)?;
    }
    drop(numbers);

    // Refuse with the store's typed recipient error if any page's painting
    // graph depends on a stream the authenticated recipient cannot decrypt.
    if !unauthorized.is_empty() {
        unauthorized.sort_unstable();
        check_pages_authorized(version, pages, extra, &unauthorized, budget)?;
    }
    drop(unauthorized);

    if let Some((start, count)) = run {
        index.try_push(Value::Integer(i64::from(start)))?;
        index.try_push(Value::Integer(i64::from(count)))?;
    }

    let offset = out.0.len() as u64;
    let mut trailer = Dict::new();
    for (key, value) in version.trailer().iter() {
        if !matches!(
            key.as_bytes(),
            b"Encrypt"
                | b"Prev"
                | b"XRefStm"
                | b"Type"
                | b"W"
                | b"Index"
                | b"Size"
                | b"Length"
                | b"Filter"
                | b"DecodeParms"
        ) {
            insert(&mut trailer, key.clone(), value.clone());
        }
    }
    insert(
        &mut trailer,
        Name::from("Type"),
        Value::Name(Name::from("XRef")),
    );
    insert(
        &mut trailer,
        Name::from("Size"),
        Value::Integer(i64::from(size)),
    );
    insert(
        &mut trailer,
        Name::from("Index"),
        Value::Array(index.into_vec().into()),
    );
    let widths = WIDTHS.map(|w| Value::Integer(w as i64));
    insert(
        &mut trailer,
        Name::from("W"),
        Value::Array(Vec::from(widths).into()),
    );
    let data_len = rows.len() * 13;
    insert(
        &mut trailer,
        Name::from("Length"),
        Value::Integer(data_len as i64),
    );
    header(&mut out.0, size, 0)?;
    write::value(&mut out.0, &Value::Dict(trailer))?;
    out.0.try_extend_from_slice(b"\nstream\n")?;
    for row in rows.iter() {
        out.0.try_extend_from_slice(row)?;
    }
    out.0
        .try_extend_from_slice(b"\nendstream\nendobj\nstartxref\n")?;
    out.0.try_extend_from_slice(offset.to_string().as_bytes())?;
    out.0.try_extend_from_slice(b"\n%%EOF\n")?;
    Ok(out.into_ephemeral())
}

fn is_unauthorized_recipient(error: &pyropdf_store::Error) -> bool {
    matches!(error.kind(), ErrorKind::Password(Password::Recipient))
}

fn is_structural_stream(value: &Value) -> bool {
    matches!(
        value,
        Value::Stream(stream)
            if matches!(
                stream.dict().get("Type"),
                Some(Value::Name(name)) if matches!(name.as_bytes(), b"ObjStm" | b"XRef")
            )
    )
}

fn write_plain_object(
    out: &mut BudgetVec<'_, u8>,
    reference: Reference,
    value: &Value,
    version: &Version,
    budget: &Budget,
) -> Result<[u8; 13]> {
    let offset = out.len() as u64;
    header(out, reference.number(), reference.generation())?;
    if let Value::Stream(stream) = value {
        let lookup = |r, b: &Budget| version.filter_object(r, b);
        if security::has_explicit_crypt(stream.dict(), &lookup, budget)? {
            let mut clean = copy_for_edit(value, budget)?;
            security::strip_explicit_crypt(&mut clean, &lookup, budget)?;
            write::value(out, &clean)?;
        } else {
            write::value(out, value)?;
        }
    } else {
        write::value(out, value)?;
    }
    out.try_extend_from_slice(b"\nendobj\n")?;
    Ok(row(1, offset, u32::from(reference.generation())))
}

/// Checks whether any page's painting graph (`/Contents`, `/Resources`,
/// `/Group`, and `/Annots` appearances) reaches an object in `unauthorized`.
///
/// Non-page streams such as private embedded files (`/Names /EmbeddedFiles`,
/// catalog entries, or FileAttachment `/FS`) are intentionally excluded so
/// restricted attachments do not block public pages.
fn check_pages_authorized(
    version: &Version,
    pages: &PageIndex,
    extra: &[(Reference, Value)],
    unauthorized: &[u32],
    budget: &Budget,
) -> Result<()> {
    let mut stack: BudgetVec<'_, Reference> = BudgetVec::new(budget);
    let mut visited: HashSet<u32> = HashSet::new();
    let mut steps: usize = 0;

    for page in pages.pages() {
        budget.poll()?;
        let page_value = match page.object() {
            Some(reference) => extra
                .binary_search_by_key(&reference.number(), |(r, _)| r.number())
                .ok()
                .map(|at| &extra[at].1),
            None => None,
        };
        let page_dict = match page_value {
            Some(Value::Dict(dict)) => dict,
            _ => page.dict(),
        };

        for key in ["Contents", "Group"] {
            if let Some(value) = page_dict.get(key) {
                collect_refs(value, &mut stack, budget)?;
            }
        }
        if let Some(resources) = page_dict.get("Resources") {
            collect_refs(resources, &mut stack, budget)?;
        } else {
            collect_inherited_resources(version, page_dict, &mut stack, budget)?;
        }
        if let Some(annots) = page_dict.get("Annots") {
            collect_annot_refs(version, extra, annots, unauthorized, &mut stack, budget)?;
        }
    }

    while let Some(reference) = stack.pop() {
        steps = steps.wrapping_add(1);
        if steps.is_multiple_of(POLL_INTERVAL) {
            budget.poll()?;
        }
        let number = reference.number();
        if unauthorized.binary_search(&number).is_ok() {
            return Err(Error::Store(pyropdf_store::Error::from(
                Password::Recipient,
            )));
        }
        if !try_insert_set(&mut visited, number, budget)? {
            continue;
        }
        if let Ok(at) = extra.binary_search_by_key(&number, |(r, _)| r.number()) {
            collect_refs(&extra[at].1, &mut stack, budget)?;
            continue;
        }
        match version.get(number, budget) {
            Ok(Some(object)) => collect_refs(object.value(), &mut stack, budget)?,
            Ok(None) => {}
            Err(error) if is_unauthorized_recipient(&error) => return Err(error.into()),
            Err(error) if error.limit().is_some() => return Err(error.into()),
            Err(_) => {}
        }
    }

    Ok(())
}

fn collect_inherited_resources(
    version: &Version,
    page_dict: &Dict,
    out: &mut BudgetVec<'_, Reference>,
    budget: &Budget,
) -> Result<()> {
    let mut parent = match page_dict.get("Parent") {
        Some(Value::Reference(reference)) => *reference,
        _ => return Ok(()),
    };
    for _ in 0..MAX_PARENT_CHAIN {
        budget.poll()?;
        let Some(object) = version.get(parent.number(), budget)? else {
            return Ok(());
        };
        let Value::Dict(dict) = object.value().as_ref() else {
            return Ok(());
        };
        if let Some(resources) = dict.get("Resources") {
            return collect_refs(resources, out, budget);
        }
        match dict.get("Parent") {
            Some(Value::Reference(next)) => parent = *next,
            _ => return Ok(()),
        }
    }
    Ok(())
}

fn collect_annot_refs(
    version: &Version,
    extra: &[(Reference, Value)],
    annots: &Value,
    unauthorized: &[u32],
    out: &mut BudgetVec<'_, Reference>,
    budget: &Budget,
) -> Result<()> {
    let resolved_array = match annots {
        Value::Reference(reference) => {
            if unauthorized.binary_search(&reference.number()).is_ok() {
                return Err(Error::Store(pyropdf_store::Error::from(
                    Password::Recipient,
                )));
            }
            if let Ok(at) = extra.binary_search_by_key(&reference.number(), |(r, _)| r.number()) {
                Some(CowValue::Borrowed(&extra[at].1))
            } else {
                version
                    .get(reference.number(), budget)?
                    .map(|object| CowValue::Owned(object.value().clone()))
            }
        }
        other => Some(CowValue::Borrowed(other)),
    };
    let Some(Value::Array(items)) = resolved_array.as_ref().map(CowValue::as_ref) else {
        return Ok(());
    };
    for item in items.iter() {
        budget.poll()?;
        let annot = match item {
            Value::Reference(reference) => {
                if unauthorized.binary_search(&reference.number()).is_ok() {
                    return Err(Error::Store(pyropdf_store::Error::from(
                        Password::Recipient,
                    )));
                }
                if let Ok(at) = extra.binary_search_by_key(&reference.number(), |(r, _)| r.number())
                {
                    Some(CowValue::Borrowed(&extra[at].1))
                } else {
                    version
                        .get(reference.number(), budget)?
                        .map(|object| CowValue::Owned(object.value().clone()))
                }
            }
            other => Some(CowValue::Borrowed(other)),
        };
        if let Some(Value::Dict(dict)) = annot.as_ref().map(CowValue::as_ref) {
            for key in ["AP", "OC"] {
                if let Some(value) = dict.get(key) {
                    collect_refs(value, out, budget)?;
                }
            }
        }
    }
    Ok(())
}

enum CowValue<'a> {
    Borrowed(&'a Value),
    Owned(Arc<Value>),
}

impl CowValue<'_> {
    fn as_ref(&self) -> &Value {
        match self {
            Self::Borrowed(value) => value,
            Self::Owned(value) => value.as_ref(),
        }
    }
}

fn collect_refs(root: &Value, out: &mut BudgetVec<'_, Reference>, budget: &Budget) -> Result<()> {
    let mut pending: BudgetVec<'_, (&Value, usize)> = BudgetVec::new(budget);
    pending.try_push((root, 0))?;
    let mut steps: usize = 0;
    while let Some((value, depth)) = pending.pop() {
        steps = steps.wrapping_add(1);
        if steps.is_multiple_of(POLL_INTERVAL) {
            budget.poll()?;
        }
        if depth > MAX_WALK_DEPTH {
            continue;
        }
        match value {
            Value::Reference(reference) => out.try_push(*reference)?,
            Value::Array(array) => {
                for item in array.iter() {
                    pending.try_push((item, depth + 1))?;
                }
            }
            Value::Dict(dict) => {
                for (key, item) in dict.iter() {
                    if !skip_page_key(key.as_bytes()) {
                        pending.try_push((item, depth + 1))?;
                    }
                }
            }
            Value::Stream(stream) => {
                for (key, item) in stream.dict().iter() {
                    if !skip_page_key(key.as_bytes()) {
                        pending.try_push((item, depth + 1))?;
                    }
                }
            }
            _ => {}
        }
    }
    Ok(())
}

/// Dictionary keys that point outside a page's painting graph (parent links,
/// navigation/actions, file attachments, metadata, and structure pointers).
fn skip_page_key(key: &[u8]) -> bool {
    matches!(
        key,
        b"Parent"
            | b"P"
            | b"Next"
            | b"Prev"
            | b"First"
            | b"Last"
            | b"FS"
            | b"EF"
            | b"RF"
            | b"A"
            | b"AA"
            | b"Dest"
            | b"Metadata"
            | b"StructParent"
            | b"StructParents"
            | b"PieceInfo"
            | b"LastModified"
            | b"B"
            | b"Beads"
            | b"Thumb"
    )
}
