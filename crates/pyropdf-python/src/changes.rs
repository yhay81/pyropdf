//! The change report for `python/pyropdf/_changes.py` (DESIGN.md section
//! 10, as #157 decided): what the edits since opening invalidate before a
//! save.
//!
//! The edits are the change set between the version that reads see and
//! the first version of the document, so the report reads only the objects
//! that the edits wrote. Signatures come from the field tree of the opened
//! file, and an edit touches one when the object it wrote lies in the
//! signature's byte range of that file. Claims come from the XMP metadata
//! that reads see, and each written object is checked for what the claim
//! forbids; this is not a validation of the claim (Phase 3's structural
//! check is).

use pyo3::prelude::*;
use pyropdf_store::budget::Budget;
use pyropdf_store::index::{Index, Location};
use pyropdf_store::value::{Dict, Value};
use pyropdf_store::version::Version;

use crate::document::{_Document, text_string};
use crate::errors::{PdfError, store_error};

/// The most field dictionaries that the walk for signatures visits.
const MAX_NODES: usize = 4_096;
/// The most levels of the field tree.
const MAX_DEPTH: usize = 64;
/// The most nested containers of one written object that the claim checks
/// walk.
const MAX_NESTING: usize = 64;
/// The most written objects that one report names for one signature or
/// claim.
const MAX_NAMED: usize = 1_024;

/// A signature of the opened file: its field's full name and object, and
/// the byte ranges that it signs.
struct Signature {
    name: String,
    field: u32,
    /// The signature dictionary's object, when it is one.
    value: Option<u32>,
    ranges: [(u64, u64); 2],
}

/// What a report says about one signature or claim: its name, the field
/// or 0, the reason, and the written objects that break it.
type Finding = (String, u32, String, Vec<u32>);

/// Returns the signatures and the claims that the edits since opening, or
/// a save that is not `incremental`, invalidate.
#[pyfunction]
pub fn changes(
    py: Python<'_>,
    document: &_Document,
    incremental: bool,
) -> PyResult<(Vec<Finding>, Vec<Finding>)> {
    let version = document.reading()?;
    let budget = document.budget();
    py.detach(|| report(&version, incremental, &budget))
}

fn report(
    version: &Version,
    incremental: bool,
    budget: &Budget,
) -> PyResult<(Vec<Finding>, Vec<Finding>)> {
    let opened = version.opened();
    let written = version
        .changes(&opened, budget)
        .map_err(|e| store_error(&e))?
        .map_or_else(Vec::new, |changes| changes.objects().to_vec());
    let mut signatures = Vec::new();
    for signature in signatures_of(&opened, budget)? {
        if !incremental {
            let reason = "a save that is not incremental rewrites the signed bytes";
            signatures.push((
                signature.name,
                signature.field,
                reason.to_owned(),
                Vec::new(),
            ));
            continue;
        }
        let Some(index) = version.base().file() else {
            continue;
        };
        let touched: Vec<u32> = written
            .iter()
            .copied()
            .filter(|&number| signed(index, &signature, number))
            .take(MAX_NAMED)
            .collect();
        if !touched.is_empty() {
            let reason = "the edits change objects that the signature signs";
            signatures.push((signature.name, signature.field, reason.to_owned(), touched));
        }
    }
    let claims = claims(version, &written, budget)?;
    Ok((signatures, claims))
}

/// Returns whether object `number` of the opened file lies in a byte range
/// that `signature` signs: the object itself, or the object stream that
/// holds it.
fn signed(index: &Index, signature: &Signature, number: u32) -> bool {
    let offset = match index.get(number).map(|entry| entry.location()) {
        Some(Location::Offset(offset)) => offset,
        Some(Location::Member { stream, .. }) => match index.get(stream).map(|e| e.location()) {
            Some(Location::Offset(offset)) => offset,
            _ => return false,
        },
        None => return false,
    };
    let offset = offset as u64;
    signature
        .ranges
        .iter()
        .any(|&(start, length)| offset >= start && offset - start < length)
}

/// Returns object `number` of `version`, or `None` for one that is free or
/// that the store cannot read.
fn object(version: &Version, number: u32, budget: &Budget) -> PyResult<Option<Value>> {
    match version.get(number, budget) {
        Ok(object) => Ok(object.map(|object| (**object.value()).clone())),
        Err(error) if error.limit().is_some() => Err(store_error(&error)),
        Err(_) => Ok(None),
    }
}

/// Returns `value`, or the object that it refers to.
fn resolved(version: &Version, value: &Value, budget: &Budget) -> PyResult<Option<Value>> {
    match value {
        Value::Reference(reference) => object(version, reference.number(), budget),
        _ => Ok(Some(value.clone())),
    }
}

fn dict(value: Option<Value>) -> Option<Dict> {
    match value {
        Some(Value::Dict(dict)) => Some(dict),
        Some(Value::Stream(stream)) => Some(stream.dict().clone()),
        _ => None,
    }
}

/// Returns the catalog of `version`.
fn catalog(version: &Version, budget: &Budget) -> PyResult<Option<Dict>> {
    match version.trailer().get("Root") {
        Some(root) => Ok(dict(resolved(version, root, budget)?)),
        None => Ok(None),
    }
}

/// Returns the signatures of `opened`: its signed signature fields, then
/// the signatures of the catalog's `/Perms`, such as a usage rights
/// signature, that no field holds.
fn signatures_of(opened: &Version, budget: &Budget) -> PyResult<Vec<Signature>> {
    let Some(catalog) = catalog(opened, budget)? else {
        return Ok(Vec::new());
    };
    let mut out = signature_fields(opened, &catalog, budget)?;
    let Some(perms) = (match catalog.get("Perms") {
        Some(perms) => dict(resolved(opened, perms, budget)?),
        None => None,
    }) else {
        return Ok(out);
    };
    for (key, value) in perms.iter() {
        let number = match value {
            Value::Reference(reference) => Some(reference.number()),
            _ => None,
        };
        if number.is_some() && out.iter().any(|signature| signature.value == number) {
            continue;
        }
        if let Some(signature) = dict(resolved(opened, value, budget)?)
            && let Some(ranges) = byte_ranges(&signature)
        {
            out.push(Signature {
                name: format!("Perms/{}", String::from_utf8_lossy(key.as_bytes())),
                field: number.unwrap_or(0),
                value: number,
                ranges,
            });
        }
    }
    Ok(out)
}

/// Returns the signed signature fields of `opened`: those whose own or
/// inherited `/FT` is `/Sig` and whose `/V` has a `/ByteRange` of two
/// ranges. Refuses a field tree past [`MAX_NODES`] or [`MAX_DEPTH`].
fn signature_fields(opened: &Version, catalog: &Dict, budget: &Budget) -> PyResult<Vec<Signature>> {
    let mut out = Vec::new();
    let Some(form) = (match catalog.get("AcroForm") {
        Some(form) => dict(resolved(opened, form, budget)?),
        None => None,
    }) else {
        return Ok(out);
    };
    let Some(Value::Array(fields)) = (match form.get("Fields") {
        Some(fields) => resolved(opened, fields, budget)?,
        None => None,
    }) else {
        return Ok(out);
    };
    // A node: its object, the full name of its parent, its inherited
    // `/FT`, and its depth.
    let mut pending: Vec<(u32, String, bool, usize)> = fields
        .iter()
        .rev()
        .filter_map(|field| match field {
            Value::Reference(reference) => Some((reference.number(), String::new(), false, 1)),
            _ => None,
        })
        .collect();
    let mut visited = Vec::new();
    while let Some((number, parent, inherited, depth)) = pending.pop() {
        if visited.contains(&number) {
            continue;
        }
        visited.push(number);
        if visited.len() > MAX_NODES {
            return Err(PdfError::new_err(format!(
                "AcroForm field tree exceeds the {MAX_NODES}-node safety limit"
            )));
        }
        let Some(node) = dict(object(opened, number, budget)?) else {
            continue;
        };
        let name = match node.get("T") {
            Some(Value::String(partial)) if parent.is_empty() => text_string(partial.as_bytes()),
            Some(Value::String(partial)) => {
                format!("{parent}.{}", text_string(partial.as_bytes()))
            }
            _ => parent,
        };
        let signature = match node.get("FT") {
            Some(Value::Name(kind)) => kind.as_bytes() == b"Sig",
            _ => inherited,
        };
        if signature
            && let Some(value) = node.get("V")
            && let Some(signature) = dict(resolved(opened, value, budget)?)
            && let Some(ranges) = byte_ranges(&signature)
        {
            out.push(Signature {
                name: name.clone(),
                field: number,
                value: match value {
                    Value::Reference(reference) => Some(reference.number()),
                    _ => None,
                },
                ranges,
            });
        }
        if let Some(kids) = node.get("Kids")
            && let Some(Value::Array(kids)) = resolved(opened, kids, budget)?
        {
            if depth >= MAX_DEPTH {
                return Err(PdfError::new_err(format!(
                    "AcroForm field tree exceeds the {MAX_DEPTH}-level safety limit"
                )));
            }
            for kid in kids.iter().rev() {
                if let Value::Reference(reference) = kid {
                    pending.push((reference.number(), name.clone(), signature, depth + 1));
                }
            }
        }
    }
    Ok(out)
}

/// Returns the two byte ranges of a signature's `/ByteRange`.
fn byte_ranges(signature: &Dict) -> Option<[(u64, u64); 2]> {
    let Some(Value::Array(items)) = signature.get("ByteRange") else {
        return None;
    };
    let mut numbers = [0_u64; 4];
    if items.len() != 4 {
        return None;
    }
    for (slot, item) in numbers.iter_mut().zip(items.iter()) {
        let Value::Integer(number) = item else {
            return None;
        };
        *slot = u64::try_from(*number).ok()?;
    }
    Some([(numbers[0], numbers[1]), (numbers[2], numbers[3])])
}

/// A conformance claim of the XMP metadata.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Claim {
    /// PDF/A, its part and conformance level.
    A(u32, u8),
    /// PDF/UA, its part.
    Ua(u32),
}

impl Claim {
    fn name(self) -> String {
        match self {
            Self::A(part, 0) => format!("PDF/A-{part}"),
            Self::A(part, level) => format!("PDF/A-{part}{}", char::from(level)),
            Self::Ua(part) => format!("PDF/UA-{part}"),
        }
    }
}

/// Returns the claims of the metadata that reads see that the written
/// objects break.
fn claims(version: &Version, written: &[u32], budget: &Budget) -> PyResult<Vec<Finding>> {
    let mut out = Vec::new();
    if written.is_empty() {
        return Ok(out);
    }
    let metadata =
        match catalog(version, budget)?.and_then(|catalog| catalog.get("Metadata").cloned()) {
            Some(metadata) => resolved(version, &metadata, budget)?,
            None => None,
        };
    let Some(Value::Stream(metadata)) = metadata else {
        return Ok(out);
    };
    let xmp = metadata.decoded(budget).map_err(|e| store_error(&e))?;
    for claim in read_claims(&xmp) {
        let mut breaking = Vec::new();
        for &number in written {
            let Some(value) = object(version, number, budget)? else {
                continue;
            };
            if breaks(claim, &value) {
                breaking.push(number);
                if breaking.len() == MAX_NAMED {
                    break;
                }
            }
        }
        if !breaking.is_empty() {
            let reason = match claim {
                Claim::A(..) => "the edits add transparency, which PDF/A-1 forbids",
                Claim::Ua(_) => "the edits add an annotation that is not in the structure tree",
            };
            out.push((claim.name(), 0, reason.to_owned(), breaking));
        }
    }
    Ok(out)
}

/// Returns the PDF/A-1 and PDF/UA claims of XMP metadata. The claims of
/// later PDF/A parts are read but break on nothing that the report checks.
fn read_claims(xmp: &[u8]) -> Vec<Claim> {
    let mut out = Vec::new();
    if let Some(part) = property(xmp, b"pdfaid:part").and_then(number) {
        let level = property(xmp, b"pdfaid:conformance")
            .and_then(|level| level.first().copied())
            .map_or(0, |level| level.to_ascii_uppercase());
        if part == 1 {
            out.push(Claim::A(part, level));
        }
    }
    if let Some(part) = property(xmp, b"pdfuaid:part").and_then(number) {
        out.push(Claim::Ua(part));
    }
    out
}

/// Returns the value of an XMP property, written as an attribute
/// `name="value"` or as an element `<name>value</name>`, trimmed.
fn property<'a>(xmp: &'a [u8], name: &[u8]) -> Option<&'a [u8]> {
    let mut at = 0;
    while let Some(found) = find(&xmp[at..], name) {
        let start = at + found;
        at = start + name.len();
        // The name must not continue a longer name, such as `notpdfaid:part`.
        if start > 0 && (xmp[start - 1].is_ascii_alphanumeric() || xmp[start - 1] == b':') {
            continue;
        }
        let rest = trim_start(&xmp[at..]);
        if let Some(rest) = rest.strip_prefix(b"=") {
            let rest = trim_start(rest);
            let Some((&quote, rest)) = rest.split_first() else {
                continue;
            };
            if quote != b'"' && quote != b'\'' {
                continue;
            }
            if let Some(end) = rest.iter().position(|&byte| byte == quote) {
                return Some(trim(&rest[..end]));
            }
        } else if xmp[start - 1] == b'<'
            && let Some(rest) = rest.strip_prefix(b">")
            && let Some(end) = rest.iter().position(|&byte| byte == b'<')
        {
            return Some(trim(&rest[..end]));
        }
    }
    None
}

fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

fn trim_start(bytes: &[u8]) -> &[u8] {
    let start = bytes
        .iter()
        .position(|b| !b.is_ascii_whitespace())
        .unwrap_or(bytes.len());
    &bytes[start..]
}

fn trim(bytes: &[u8]) -> &[u8] {
    let bytes = trim_start(bytes);
    let end = bytes
        .iter()
        .rposition(|b| !b.is_ascii_whitespace())
        .map_or(0, |end| end + 1);
    &bytes[..end]
}

fn number(bytes: &[u8]) -> Option<u32> {
    std::str::from_utf8(bytes).ok()?.parse().ok()
}

/// Returns whether a written object breaks `claim`: transparency anywhere
/// in it for PDF/A-1, or an annotation without `/StructParent` for PDF/UA.
fn breaks(claim: Claim, value: &Value) -> bool {
    match claim {
        Claim::A(..) => has_transparency(value),
        Claim::Ua(_) => dict(Some(value.clone())).is_some_and(|dict| is_untagged_annotation(&dict)),
    }
}

/// Returns whether `value` holds, in itself or in its direct containers,
/// what PDF/A-1 forbids as transparency (ISO 19005-1 section 6.4): a soft
/// mask other than `/None`, a constant alpha below 1, a blend mode other
/// than `/Normal` or `/Compatible`, or a transparency group.
fn has_transparency(value: &Value) -> bool {
    let mut pending = vec![(value, 0)];
    while let Some((value, depth)) = pending.pop() {
        let dict = match value {
            Value::Dict(dict) => dict,
            Value::Stream(stream) => stream.dict(),
            Value::Array(items) => {
                if depth < MAX_NESTING {
                    pending.extend(items.iter().map(|item| (item, depth + 1)));
                }
                continue;
            }
            _ => continue,
        };
        for (key, entry) in dict.iter() {
            let forbidden = match (key.as_bytes(), entry) {
                (b"SMask", Value::Name(name)) => name.as_bytes() != b"None",
                (b"SMask", Value::Dict(_) | Value::Stream(_) | Value::Reference(_)) => true,
                (b"CA" | b"ca", Value::Integer(alpha)) => *alpha < 1,
                (b"CA" | b"ca", Value::Real(alpha)) => alpha.value() < 1.0,
                (b"BM", Value::Name(mode)) => !matches!(mode.as_bytes(), b"Normal" | b"Compatible"),
                (b"S", Value::Name(kind)) => kind.as_bytes() == b"Transparency",
                _ => false,
            };
            if forbidden {
                return true;
            }
            if depth < MAX_NESTING {
                pending.push((entry, depth + 1));
            }
        }
    }
    false
}

/// Returns whether `dict` is an annotation that PDF/UA requires in the
/// structure tree, without the `/StructParent` that would put it there.
fn is_untagged_annotation(dict: &Dict) -> bool {
    let Some(Value::Name(subtype)) = dict.get("Subtype") else {
        return false;
    };
    dict.get("Rect").is_some()
        && !matches!(subtype.as_bytes(), b"Popup")
        && dict.get("StructParent").is_none()
}
