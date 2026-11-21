//! PDF version requirements of annotation subtypes, entries, and flags, and
//! catalog `/Version` elevation for older documents.

use pyropdf_store::budget::Budget;
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::value::{Dict, Name, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::changes::pdfa_part;

use super::{AnnotationResult as Result, Reader};

/// A `(major, minor)` PDF version such as `(1, 4)` or `(2, 0)`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct PdfVersion(pub u8, pub u8);

/// The version assumed when a document was not opened from a file or its header
/// names no version, matching `save` and `check`.
pub const DEFAULT_VERSION: PdfVersion = PdfVersion(1, 7);

/// The earliest PDF version in which `/Catalog /Version` itself is defined.
pub const CATALOG_VERSION_MIN: PdfVersion = PdfVersion(1, 4);

/// Parses a `"<major>.<minor>"` version byte string.
#[must_use]
pub fn parse_version(bytes: &[u8]) -> Option<PdfVersion> {
    let text = std::str::from_utf8(bytes).ok()?;
    let (major, minor) = text.split_once('.')?;
    if major.is_empty() || minor.is_empty() || minor.contains('.') {
        return None;
    }
    Some(PdfVersion(major.parse().ok()?, minor.parse().ok()?))
}

/// Returns the version declared in `version`'s `%PDF-x.y` file header, if any.
#[must_use]
pub fn header_version(version: &Version) -> Option<PdfVersion> {
    let rest = version.base().header_bytes()?.strip_prefix(b"%PDF-")?;
    let end = rest
        .iter()
        .position(|&byte| !(byte.is_ascii_digit() || byte == b'.'))
        .unwrap_or(rest.len());
    parse_version(&rest[..end])
}

/// Returns the effective PDF version of `version`: the later of its file header
/// (or [`DEFAULT_VERSION`]) and its catalog `/Version`.
pub fn effective_version(version: &Version, budget: &Budget) -> Result<PdfVersion> {
    let mut effective = header_version(version).unwrap_or(DEFAULT_VERSION);
    let reader = Reader::new(version, budget);
    if let Some(root) = version.trailer().get("Root")
        && let Value::Dict(catalog) = &*reader.resolve(root)?
        && let Some(Value::Name(declared)) = reader.entry(catalog, "Version")?.as_deref()
        && let Some(parsed) = parse_version(declared.as_bytes())
    {
        effective = effective.max(parsed);
    }
    Ok(effective)
}

/// Returns the PDF version ceiling for automatically supplied entries during an
/// edit whose caller-requested minimum version is `minimum`.
///
/// When the document claims PDF/A-1 (PDF 1.4) or PDF/A-2/3 (PDF 1.7) and the
/// caller did not explicitly request features past that version, automatic
/// optional entries stay within the claimed part's PDF version.
pub fn automatic_version(
    version: &Version,
    effective: PdfVersion,
    minimum: PdfVersion,
    budget: &Budget,
) -> Result<PdfVersion> {
    let effective = effective.max(minimum);
    let cap = match pdfa_part(version, budget)? {
        Some(1) => Some(PdfVersion(1, 4)),
        Some(2 | 3) => Some(PdfVersion(1, 7)),
        _ => None,
    };
    Ok(match cap {
        Some(limit) if minimum <= limit => effective.min(limit),
        _ => effective,
    })
}

/// Raises the catalog's `/Version` to at least `minimum` when the document's
/// effective version is older, and returns the resulting effective version.
pub fn require_version(
    transaction: &mut Transaction,
    minimum: PdfVersion,
    budget: &Budget,
) -> Result<PdfVersion> {
    let current = effective_version(transaction.version(), budget)?;
    if current >= minimum {
        return Ok(current);
    }
    let target = minimum.max(CATALOG_VERSION_MIN);
    let catalog = {
        let version = transaction.version();
        let reader = Reader::new(version, budget);
        match version.trailer().get("Root") {
            Some(root) => reader.resolve_object(root)?,
            None => None,
        }
    };
    if let Some((reference, value)) = catalog
        && let Value::Dict(mut dict) = copy_for_edit(&value, budget)?
    {
        let label = format!("{}.{}", target.0, target.1);
        #[expect(
            clippy::disallowed_methods,
            reason = "inserts only the fixed /Version key on the catalog"
        )]
        dict.insert(
            Name::from("Version"),
            Value::Name(Name::from(label.as_str())),
        );
        transaction.write(reference.number(), &Value::Dict(dict), budget)?;
    }
    Ok(target)
}

/// Returns whether `subtype` is one of the 28 PDF annotation subtypes in the
/// Arlington model.
#[must_use]
pub fn is_known_annotation_subtype(subtype: &[u8]) -> bool {
    matches!(
        subtype,
        b"Text"
            | b"Link"
            | b"FreeText"
            | b"Line"
            | b"Square"
            | b"Circle"
            | b"Polygon"
            | b"PolyLine"
            | b"Highlight"
            | b"Underline"
            | b"Squiggly"
            | b"StrikeOut"
            | b"Redact"
            | b"Stamp"
            | b"Caret"
            | b"Ink"
            | b"Popup"
            | b"FileAttachment"
            | b"Sound"
            | b"Movie"
            | b"RichMedia"
            | b"Widget"
            | b"Screen"
            | b"PrinterMark"
            | b"TrapNet"
            | b"Watermark"
            | b"3D"
            | b"Projection"
    )
}

/// Returns whether ISO 19005-1 §6.5.2 forbids annotations of `subtype` in
/// PDF/A-1.
#[must_use]
pub fn is_pdfa_1_forbidden_subtype(subtype: &[u8]) -> bool {
    matches!(
        subtype,
        b"Sound"
            | b"Movie"
            | b"FileAttachment"
            | b"TrapNet"
            | b"Polygon"
            | b"PolyLine"
            | b"Caret"
            | b"Screen"
            | b"Watermark"
            | b"3D"
            | b"Redact"
            | b"Projection"
            | b"RichMedia"
    )
}

/// Returns the PDF version that introduced annotation `subtype`.
#[must_use]
pub fn subtype_version(subtype: &[u8]) -> PdfVersion {
    match subtype {
        b"Text" | b"Link" => PdfVersion(1, 0),
        b"Movie" | b"Sound" | b"Widget" => PdfVersion(1, 2),
        b"Circle" | b"FileAttachment" | b"FreeText" | b"Highlight" | b"Ink" | b"Line"
        | b"Popup" | b"Square" | b"Stamp" | b"StrikeOut" | b"TrapNet" | b"Underline" => {
            PdfVersion(1, 3)
        }
        b"PrinterMark" | b"Squiggly" => PdfVersion(1, 4),
        b"Caret" | b"PolyLine" | b"Polygon" | b"Screen" => PdfVersion(1, 5),
        b"3D" | b"Watermark" => PdfVersion(1, 6),
        b"Redact" => PdfVersion(1, 7),
        b"Projection" | b"RichMedia" => PdfVersion(2, 0),
        _ => PdfVersion(1, 0),
    }
}

/// Returns the PDF version required for `/CA` on an annotation of `subtype` in
/// the Arlington PDF Model.
#[must_use]
pub fn ca_version(subtype: &[u8]) -> PdfVersion {
    match subtype {
        b"Circle" | b"FileAttachment" | b"FreeText" | b"Highlight" | b"Ink" | b"Line"
        | b"Sound" | b"Squiggly" | b"Stamp" | b"StrikeOut" | b"Text" | b"Underline" => {
            PdfVersion(1, 4)
        }
        b"Caret" | b"PolyLine" | b"Polygon" => PdfVersion(1, 5),
        b"Redact" => PdfVersion(1, 7),
        _ => PdfVersion(2, 0),
    }
}

/// Returns the PDF version that introduced `/M` on an annotation of `subtype`.
#[must_use]
pub fn m_version(subtype: &[u8]) -> PdfVersion {
    subtype_version(subtype).max(PdfVersion(1, 1))
}

/// Returns the PDF version required by an `/F` bitmask value (ISO 32000-2
/// Table 167 and Arlington bitmask predicates).
#[must_use]
pub const fn flags_version(flags: i64) -> PdfVersion {
    if flags & (1 << 9) != 0 {
        PdfVersion(1, 7)
    } else if flags & (1 << 8) != 0 {
        PdfVersion(1, 5)
    } else if flags & (1 << 7) != 0 {
        PdfVersion(1, 4)
    } else if flags & 0b111_1000 != 0 {
        PdfVersion(1, 3)
    } else if flags & 0b000_0110 != 0 {
        PdfVersion(1, 2)
    } else {
        PdfVersion(1, 1)
    }
}

/// Returns whether an annotation of `subtype` admits `/BS` at `effective` in
/// the Arlington PDF Model.
#[must_use]
pub fn supports_bs(subtype: &[u8], effective: PdfVersion) -> bool {
    matches!(
        subtype,
        b"Link" | b"Widget" | b"Square" | b"Circle" | b"Line" | b"Ink" | b"Polygon" | b"PolyLine"
    ) || (subtype == b"FreeText" && effective >= PdfVersion(1, 7))
}

/// Returns whether `dict` is an annotation dictionary.
#[must_use]
pub fn is_annotation_dict(dict: &Dict) -> bool {
    if matches!(dict.get("Type"), Some(Value::Name(name)) if name.as_bytes() == b"Annot") {
        return true;
    }
    dict.get("Rect").is_some()
        && matches!(
            dict.get("Subtype"),
            Some(Value::Name(subtype)) if is_known_annotation_subtype(subtype.as_bytes())
        )
}

/// Returns the minimum PDF version required by the subtype, entries, and flags
/// of annotation `dict`, or `None` when `dict` is not an annotation.
#[must_use]
pub fn dict_required_version(dict: &Dict) -> Option<PdfVersion> {
    if !is_annotation_dict(dict) {
        return None;
    }
    let subtype = match dict.get("Subtype") {
        Some(Value::Name(name)) => name.as_bytes(),
        _ => b"",
    };
    let mut version = subtype_version(subtype);
    for (key, entry) in dict.iter() {
        if matches!(entry, Value::Null) {
            continue;
        }
        let needed = match key.as_bytes() {
            b"M" | b"C" => PdfVersion(1, 1),
            b"AP" | b"AS" => PdfVersion(1, 2),
            b"BS" => {
                if subtype == b"FreeText" {
                    PdfVersion(1, 7)
                } else {
                    PdfVersion(1, 2)
                }
            }
            b"P" | b"Popup" | b"Parent" | b"StructParent" => PdfVersion(1, 3),
            b"NM" | b"IC" | b"Q" => PdfVersion(1, 4),
            b"LE" => {
                if subtype == b"FreeText" {
                    PdfVersion(1, 7)
                } else {
                    PdfVersion(1, 4)
                }
            }
            b"CA" => ca_version(subtype),
            b"CreationDate" | b"Subj" | b"IRT" | b"RC" | b"OC" | b"DS" | b"State"
            | b"StateModel" => PdfVersion(1, 5),
            b"BE" | b"RD" => {
                if subtype == b"FreeText" {
                    PdfVersion(1, 7)
                } else {
                    PdfVersion(1, 5)
                }
            }
            b"RT" | b"CL" => PdfVersion(1, 6),
            b"IT" => {
                if subtype == b"Stamp" {
                    PdfVersion(2, 0)
                } else {
                    PdfVersion(1, 6)
                }
            }
            b"ExData" | b"OverlayText" | b"Repeat" | b"RO" | b"Measure" | b"LLO" | b"CP"
            | b"CO" => PdfVersion(1, 7),
            b"Lang" | b"BM" | b"ca" | b"Path" => PdfVersion(2, 0),
            b"F" => match entry {
                Value::Integer(flags) => flags_version(*flags),
                _ => PdfVersion(1, 1),
            },
            _ => continue,
        };
        version = version.max(needed);
    }
    Some(version)
}
