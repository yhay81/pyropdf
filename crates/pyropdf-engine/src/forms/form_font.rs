//! `/AcroForm` `/DR` standard-font lookup, `/DA` default-appearance parsing,
//! and bounded `/Parent` chain resolution for widgets.

use std::sync::Arc;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::value::{Array, Dict, Name, Reference, Value};
use pyropdf_store::version::{Transaction, Version};
use pyropdf_store::{Error, LimitExceeded, Result};

use crate::annotations::{Reader, Resolved, numbers_array};
use crate::display::number;
use crate::standard::{self, DefaultAppearance, MAX_DEFAULT_APPEARANCE};

use super::WidgetError;

/// The most fields of a `/Parent` chain that a widget reads entries from.
pub const MAX_PARENTS: usize = 64;

/// What a form's `/DR /Font` holds under the font name of a `/DA`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum FormFont {
    /// No unembedded Standard 14 Type1 font: the name falls back to its
    /// Standard 14 abbreviation, else to Helvetica.
    Absent,
    /// A Standard 14 font whose glyphs the appearance's own font
    /// dictionary reaches: WinAnsi for a text font, the built-in encoding
    /// for Symbol and ZapfDingbats.
    Standard(&'static str),
    /// A Standard 14 font whose `/Encoding` selects glyphs that the
    /// appearance's own font dictionary cannot reproduce.
    Unsupported,
}

/// Resolves the font that the form's `/DR /Font` holds under `/DA` name
/// `resource`: a collision-free name assigned when a widget is created, or
/// an arbitrary alias such as `F1`. Entries beyond those that select the
/// face and its encoding, such as `/Widths` or `/Name`, do not matter.
///
/// The appearance writes its own font dictionary, so a text font's
/// `/Encoding` matters only for the glyphs that it selects: the built-in
/// `StandardEncoding` of an omitted entry, `WinAnsiEncoding`,
/// `MacRomanEncoding`, and a dictionary over one of these without
/// `/Differences` all select Latin text glyphs that WinAnsi reaches, so the
/// text is drawn in WinAnsi with the same glyphs. Omission is not read as
/// WinAnsi: the appearance's dictionary names WinAnsi itself. Any other
/// encoding, and any `/Encoding` on Symbol or ZapfDingbats, is
/// [`FormFont::Unsupported`] rather than drawn with other glyphs.
pub(super) fn form_standard_font(
    reader: &Reader<'_>,
    form: &Dict,
    resource: &str,
) -> Result<FormFont> {
    let Some(resources) = reader.entry(form, "DR")? else {
        return Ok(FormFont::Absent);
    };
    let Value::Dict(resources) = &*resources else {
        return Ok(FormFont::Absent);
    };
    let Some(fonts) = reader.entry(resources, "Font")? else {
        return Ok(FormFont::Absent);
    };
    let Value::Dict(fonts) = &*fonts else {
        return Ok(FormFont::Absent);
    };
    let Some(font) = reader.entry(fonts, resource)? else {
        return Ok(FormFont::Absent);
    };
    let Value::Dict(font) = &*font else {
        return Ok(FormFont::Absent);
    };
    if !matches!(reader.entry(font, "Subtype")?.as_deref(), Some(Value::Name(name)) if name.as_bytes() == b"Type1")
        || !match reader.entry(font, "Type")?.as_deref() {
            None => true,
            Some(Value::Name(name)) => name.as_bytes() == b"Font",
            Some(_) => false,
        }
    {
        return Ok(FormFont::Absent);
    }
    let Some(base) = reader.entry(font, "BaseFont")? else {
        return Ok(FormFont::Absent);
    };
    let Value::Name(base) = &*base else {
        return Ok(FormFont::Absent);
    };
    let Some(base) = standard::DA_FONTS
        .iter()
        .map(|(_, base)| *base)
        .find(|name| name.as_bytes() == base.as_bytes())
    else {
        return Ok(FormFont::Absent);
    };
    if let Some(descriptor) = reader.entry(font, "FontDescriptor")? {
        let Value::Dict(descriptor) = &*descriptor else {
            return Ok(FormFont::Absent);
        };
        for key in ["FontFile", "FontFile2", "FontFile3"] {
            if reader.entry(descriptor, key)?.is_some() {
                // An embedded program draws other outlines than the
                // Standard 14 font that it is named after.
                return Ok(FormFont::Absent);
            }
        }
    }
    let supported = match reader.entry(font, "Encoding")?.as_deref() {
        None => true,
        Some(_) if matches!(base, "Symbol" | "ZapfDingbats") => false,
        Some(Value::Name(name)) => latin_encoding(name.as_bytes()),
        Some(Value::Dict(encoding)) => {
            let base_supported = match reader.entry(encoding, "BaseEncoding")?.as_deref() {
                None => true,
                Some(Value::Name(name)) => latin_encoding(name.as_bytes()),
                Some(_) => false,
            };
            let differences = match reader.entry(encoding, "Differences")?.as_deref() {
                None => false,
                Some(Value::Array(differences)) => !differences.is_empty(),
                Some(_) => true,
            };
            base_supported && !differences
        }
        Some(_) => false,
    };
    Ok(if supported {
        FormFont::Standard(base)
    } else {
        FormFont::Unsupported
    })
}

/// Returns whether a named encoding of a text font selects only the Latin
/// text glyphs that WinAnsi reaches.
fn latin_encoding(name: &[u8]) -> bool {
    matches!(
        name,
        b"StandardEncoding" | b"WinAnsiEncoding" | b"MacRomanEncoding"
    )
}

/// Returns the field referred to by `/Parent`, refusing cycles and depth overflow.
pub(super) fn parent(
    reader: &Reader<'_>,
    dict: &Dict,
    visited: &mut Vec<Reference>,
) -> Result<Option<Arc<Value>>> {
    let Some(parent @ Value::Reference(reference)) = dict.get("Parent") else {
        return Ok(None);
    };
    if visited.contains(reference) {
        return Err(Error::malformed("a form field parent chain has a cycle"));
    }
    let parent = reader.resolve(parent)?;
    if matches!(*parent, Value::Dict(_)) {
        let depth = visited.len() + 1;
        reader
            .budget
            .check(LimitCode::FormFieldParentDepth, depth as u64)?;
        if depth > MAX_PARENTS {
            return Err(LimitExceeded::over(
                LimitCode::FormFieldParentDepth,
                MAX_PARENTS as u64,
                depth as u64,
            )
            .into());
        }
        #[expect(
            clippy::disallowed_methods,
            reason = "the chain holds MAX_PARENTS fields at most, checked above"
        )]
        visited.push(*reference);
        Ok(Some(owned(parent, reader.budget)?))
    } else {
        Ok(None)
    }
}

pub(super) fn owned(value: Resolved<'_>, budget: &Budget) -> Result<Arc<Value>> {
    match value {
        Resolved::Shared(value) => Ok(value),
        Resolved::Borrowed(value) => {
            budget.charge(LimitCode::MemorySize, size_of::<Value>() as u64)?;
            Ok(Arc::new(copy_for_edit(value, budget)?))
        }
    }
}

/// Returns the document's interactive form dictionary.
pub(super) fn interactive_form(reader: &Reader<'_>) -> Result<Option<Arc<Value>>> {
    let Some(root) = reader.version.trailer().get("Root") else {
        return Ok(None);
    };
    let catalog = reader.resolve(root)?;
    let Value::Dict(catalog) = &*catalog else {
        return Ok(None);
    };
    reader
        .entry(catalog, "AcroForm")?
        .map(|value| owned(value, reader.budget))
        .transpose()
}

/// Returns the `/DA` that `widget` draws its text with, as
/// [`default_appearance`] finds it with the document's interactive form.
pub fn text_appearance(
    reader: &Reader<'_>,
    widget: &Dict,
) -> std::result::Result<Option<DefaultAppearance>, WidgetError> {
    let form = interactive_form(reader)?;
    let form = match form.as_deref() {
        Some(Value::Dict(form)) => Some(form),
        _ => None,
    };
    default_appearance(reader, widget, form)
}

/// Returns the `/DA` of a widget: its own, else the nearest of its
/// `/Parent` chain, else the interactive form's.
pub(super) fn default_appearance(
    reader: &Reader<'_>,
    widget: &Dict,
    form: Option<&Dict>,
) -> std::result::Result<Option<DefaultAppearance>, WidgetError> {
    let mut visited = Vec::new();
    let mut held = None;
    loop {
        let dict = match held.as_deref() {
            None => widget,
            Some(Value::Dict(dict)) => dict,
            Some(_) => break,
        };
        if let Some(Value::String(da)) = reader.entry(dict, "DA")?.as_deref() {
            return Ok(Some(parse_default_appearance(
                da.as_bytes(),
                reader.budget,
            )?));
        }
        match parent(reader, dict, &mut visited)? {
            Some(parent) => held = Some(parent),
            None => break,
        }
    }
    let Some(form) = form else {
        return Ok(None);
    };
    Ok(match reader.entry(form, "DA")?.as_deref() {
        Some(Value::String(da)) => Some(parse_default_appearance(da.as_bytes(), reader.budget)?),
        _ => None,
    })
}

fn parse_default_appearance(
    bytes: &[u8],
    budget: &Budget,
) -> std::result::Result<DefaultAppearance, WidgetError> {
    budget.check(LimitCode::FormDefaultAppearanceSize, bytes.len() as u64)?;
    if bytes.len() > MAX_DEFAULT_APPEARANCE {
        return Err(LimitExceeded::over(
            LimitCode::FormDefaultAppearanceSize,
            MAX_DEFAULT_APPEARANCE as u64,
            bytes.len() as u64,
        )
        .into());
    }
    Ok(DefaultAppearance::parse(bytes)?)
}

fn parse_pdf_version(bytes: &[u8]) -> Option<(u8, u8)> {
    let end = bytes
        .iter()
        .position(|&byte| !(byte.is_ascii_digit() || byte == b'.'))
        .unwrap_or(bytes.len());
    let text = std::str::from_utf8(&bytes[..end]).ok()?;
    let (major, minor) = text.split_once('.')?;
    Some((major.parse().ok()?, minor.parse().ok()?))
}

/// Returns whether the effective PDF version of `version` (the later of its
/// `%PDF-x.y` header and its catalog `/Version`) is PDF 2.0 or later.
pub(in crate::forms) fn is_pdf_2_0(version: &Version, budget: &Budget) -> Result<bool> {
    let mut effective = version
        .base()
        .header_bytes()
        .and_then(|bytes| bytes.strip_prefix(b"%PDF-"))
        .and_then(parse_pdf_version)
        .unwrap_or((1, 7));
    let reader = Reader::new(version, budget);
    if let Some(root) = version.trailer().get("Root")
        && let Value::Dict(catalog) = &*reader.resolve(root)?
        && let Some(Value::Name(declared)) = reader.entry(catalog, "Version")?.as_deref()
        && let Some(parsed) = parse_pdf_version(declared.as_bytes())
    {
        effective = effective.max(parsed);
    }
    Ok(effective >= (2, 0))
}

/// Writes an unembedded Standard 14 `Type1` font dictionary for `base`,
/// supplying `/FirstChar`, `/LastChar`, `/Widths`, and an indirect
/// `/FontDescriptor` when `pdf_2_0` is true (ISO 32000-2 §9.6.2.2).
#[expect(
    clippy::disallowed_methods,
    reason = "each font and descriptor dictionary holds fixed keys only"
)]
pub(in crate::forms) fn write_standard_type1(
    transaction: &mut Transaction,
    base: &str,
    pdf_2_0: bool,
    budget: &Budget,
) -> Result<Reference> {
    let mut font = Dict::new();
    font.insert(Name::from("Type"), Value::Name(Name::from("Font")));
    font.insert(Name::from("Subtype"), Value::Name(Name::from("Type1")));
    font.insert(Name::from("BaseFont"), Value::Name(Name::from(base)));
    let symbolic = matches!(base, "Symbol" | "ZapfDingbats");
    if !symbolic {
        font.insert(
            Name::from("Encoding"),
            Value::Name(Name::from("WinAnsiEncoding")),
        );
    }
    if pdf_2_0 && let Some(b14) = standard::font(base) {
        let first_char = 32_u8;
        let last_char = 255_u8;
        let count = usize::from(last_char - first_char) + 1;
        let mut widths = BudgetVec::new(budget);
        widths.try_reserve_exact(count)?;
        let metrics = b14.metrics();
        for code in first_char..=last_char {
            let width = if symbolic {
                metrics
                    .character_metrics
                    .iter()
                    .find(|item| item.code == i32::from(code))
                    .map_or(0.0, |item| item.width_x)
            } else {
                b14.winansi_width(code).unwrap_or(0.0)
            };
            widths.try_push(number(f64::from(width)))?;
        }
        let mut flags = if symbolic { 4_i64 } else { 32_i64 };
        if !symbolic {
            if metrics.is_fixed_pitch {
                flags |= 1;
            }
            if base.starts_with("Times-") {
                flags |= 2;
            }
            if metrics.italic_angle != 0.0 {
                flags |= 64;
            }
        }
        let ascent = if metrics.ascender != 0.0 {
            metrics.ascender
        } else {
            metrics.font_bbox.ury
        };
        let descent = if metrics.descender != 0.0 {
            metrics.descender.min(0.0)
        } else {
            metrics.font_bbox.lly.min(0.0)
        };
        let cap_height = if metrics.cap_height != 0.0 {
            metrics.cap_height
        } else {
            ascent
        };
        let mut descriptor = Dict::new();
        descriptor.insert(
            Name::from("Type"),
            Value::Name(Name::from("FontDescriptor")),
        );
        descriptor.insert(Name::from("FontName"), Value::Name(Name::from(base)));
        descriptor.insert(Name::from("Flags"), Value::Integer(flags));
        descriptor.insert(
            Name::from("FontBBox"),
            numbers_array(&[
                f64::from(metrics.font_bbox.llx),
                f64::from(metrics.font_bbox.lly),
                f64::from(metrics.font_bbox.urx),
                f64::from(metrics.font_bbox.ury),
            ]),
        );
        descriptor.insert(
            Name::from("ItalicAngle"),
            number(f64::from(metrics.italic_angle)),
        );
        descriptor.insert(Name::from("Ascent"), number(f64::from(ascent)));
        descriptor.insert(Name::from("Descent"), number(f64::from(descent)));
        descriptor.insert(Name::from("CapHeight"), number(f64::from(cap_height)));
        descriptor.insert(
            Name::from("StemV"),
            Value::Integer(if base.contains("Bold") { 120 } else { 80 }),
        );
        let descriptor_ref = transaction.allocate()?;
        transaction.write(descriptor_ref.number(), &Value::Dict(descriptor), budget)?;

        font.insert(
            Name::from("FirstChar"),
            Value::Integer(i64::from(first_char)),
        );
        font.insert(Name::from("LastChar"), Value::Integer(i64::from(last_char)));
        font.insert(
            Name::from("Widths"),
            Value::Array(Array::from(widths.into_vec())),
        );
        font.insert(
            Name::from("FontDescriptor"),
            Value::Reference(descriptor_ref),
        );
    }
    let reference = transaction.allocate()?;
    transaction.write(reference.number(), &Value::Dict(font), budget)?;
    Ok(reference)
}
