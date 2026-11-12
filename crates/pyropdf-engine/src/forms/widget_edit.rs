//! Reading widget styles and applying geometry and appearance edits.

use std::sync::Arc;

use pyropdf_store::budget::{Budget, LimitCode};
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::value::{Dict, Name, PdfString, Reference, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::annotations::{
    AnnotationError as Error, AnnotationResult as Result, PageAnnots, Reader, Rect, border_style,
    rgb,
};
use crate::display::encode_text;
use crate::forms as widgets;
use crate::standard::{DA_FONTS, DefaultAppearance};

use super::field_edit::{dict_at, form_object, register_standard_font};
use super::fill::{base, update_need_appearances};
use super::fill_index::FillCache;
use super::values::{Id, MAX_INPUT_BYTES, limit, object_location};

/// A widget's style: its `/MK` border color, background color, caption,
/// and rotation; its border, from `/BS` else `/Border`; and the font resource,
/// size, and RGB color of the `/DA` that it draws its text with, its own or
/// inherited, with the Standard 14 font that the resource resolves to.
pub type WidgetStyle = (
    (Vec<f64>, Vec<f64>, Option<String>, i64),
    Option<(f64, String, Vec<f64>)>,
    Option<(String, f64, (f64, f64, f64), Option<&'static str>)>,
);

/// Returns the style of the widget on this page, or None if the page is gone.
pub fn widget_style(
    version: Arc<Version>,
    pages: Arc<PageIndex>,
    budget: &Budget,
    number: u32,
    generation: u16,
    index: u32,
    id: Id,
) -> Result<Option<WidgetStyle>> {
    let Some(annots) = PageAnnots::read(version, pages, budget, number, generation)? else {
        return Ok(None);
    };
    let (_, dict) = annots.find(index, id)?;
    if annots.name(&dict, "Subtype")?.as_deref() != Some(b"Widget") {
        return Err(Error::Stale(format!(
            "annotation {} is no longer a widget",
            id.0
        )));
    }
    let mark = match annots.resolved(dict.get("MK"))? {
        Some(Value::Dict(mark)) => mark,
        _ => Dict::new(),
    };
    let caption = match annots.resolved(mark.get("CA"))? {
        Some(Value::String(caption)) => {
            let caption = widgets::text_string(caption.as_bytes());
            if caption.len() > MAX_INPUT_BYTES {
                return Err(limit(format!(
                    "a widget caption exceeds the {MAX_INPUT_BYTES}-byte safety limit"
                )));
            }
            Some(caption)
        }
        _ => None,
    };
    let rotation = match annots.resolved(mark.get("R"))? {
        Some(Value::Integer(rotation)) => rotation.rem_euclid(360),
        _ => 0,
    };
    let colors = (
        annots.color(&mark, "BC")?,
        annots.color(&mark, "BG")?,
        caption,
        rotation,
    );
    let reader = annots.reader();
    let appearance = match widgets::text_appearance(&reader, &dict)? {
        Some(da) => {
            let size = if da.size == 0.0 {
                0.0
            } else {
                da.size * annots.unit
            };
            if !size.is_finite() {
                return Err(Error::Malformed(
                    "a widget font size overflows display space".into(),
                ));
            }
            let font = widget_text_font(&annots, &da.font)?;
            Some((da.font, size, da.color, font))
        }
        None => None,
    };
    Ok(Some((colors, annots.border(&dict)?, appearance)))
}

/// A value read without copying its input-sized contents.
enum WidgetFontValue<'a> {
    Direct(&'a Value),
    Indirect(Arc<Value>),
}

impl std::ops::Deref for WidgetFontValue<'_> {
    type Target = Value;

    fn deref(&self) -> &Value {
        match self {
            Self::Direct(value) => value,
            Self::Indirect(value) => value,
        }
    }
}

/// Resolves an optional resource value without cloning a dictionary. The
/// reference chain is bounded and charged through the operation's reads.
fn widget_font_value<'a>(
    reads: &mut Reads<'_>,
    value: Option<&'a Value>,
) -> Result<Option<WidgetFontValue<'a>>> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Reference(reference)) => {
            Ok(object_location(reads, *reference)?
                .map(|(_, value)| WidgetFontValue::Indirect(value)))
        }
        Some(value) => Ok(Some(WidgetFontValue::Direct(value))),
    }
}

/// Returns `None` for a missing resource, `Some(None)` for an unsupported
/// one, or `Some(Some(name))` for a Standard 14 font.
fn widget_font_resource(
    reads: &mut Reads<'_>,
    root: Option<&Value>,
    resource: &str,
) -> Result<Option<Option<&'static str>>> {
    let Some(catalog) = widget_font_value(reads, root)? else {
        return Ok(None);
    };
    let Value::Dict(catalog) = &*catalog else {
        return Ok(Some(None));
    };
    let Some(form) = widget_font_value(reads, catalog.get("AcroForm"))? else {
        return Ok(None);
    };
    let Value::Dict(form) = &*form else {
        return Ok(Some(None));
    };
    let Some(resources) = widget_font_value(reads, form.get("DR"))? else {
        return Ok(None);
    };
    let Value::Dict(resources) = &*resources else {
        return Ok(Some(None));
    };
    let Some(fonts) = widget_font_value(reads, resources.get("Font"))? else {
        return Ok(None);
    };
    let Value::Dict(fonts) = &*fonts else {
        return Ok(Some(None));
    };
    let Some(font) = widget_font_value(reads, fonts.get(resource))? else {
        return Ok(None);
    };
    let Value::Dict(font) = &*font else {
        return Ok(Some(None));
    };
    if !matches!(widget_font_value(reads, font.get("Subtype"))?.as_deref(), Some(Value::Name(name)) if name.as_bytes() == b"Type1")
    {
        return Ok(Some(None));
    }
    if let Some(descriptor) = widget_font_value(reads, font.get("FontDescriptor"))? {
        let Value::Dict(descriptor) = &*descriptor else {
            return Ok(Some(None));
        };
        for key in ["FontFile", "FontFile2", "FontFile3"] {
            if widget_font_value(reads, descriptor.get(key))?.is_some() {
                return Ok(Some(None));
            }
        }
    }
    let Some(base) = widget_font_value(reads, font.get("BaseFont"))? else {
        return Ok(Some(None));
    };
    let Value::Name(base) = &*base else {
        return Ok(Some(None));
    };
    Ok(Some(
        DA_FONTS
            .iter()
            .map(|(_, font)| *font)
            .find(|name| name.as_bytes() == base.as_bytes()),
    ))
}

/// Resolves the font named by a widget's /DA through the form's /DR. A
/// missing resource can use a conventional Standard 14 abbreviation; a
/// present but unsupported font must not be confused with that abbreviation.
fn widget_text_font(annots: &PageAnnots<'_>, resource: &str) -> Result<Option<&'static str>> {
    let reader = annots.reader();
    let mut reads = Reads::new(reader.version, reader.budget);
    if let Some(font) =
        widget_font_resource(&mut reads, reader.version.trailer().get("Root"), resource)?
    {
        return Ok(font);
    }
    Ok(DA_FONTS
        .iter()
        .find(|(known, _)| *known == resource)
        .map(|(_, font)| *font))
}

/// An edit of a widget's geometry and style, as the setters of `Widget`
/// pass it; `clear` names the properties to remove. The rotation is a
/// multiple of 90 degrees. Colors are RGB from 0
/// to 1, the border is `(width, style, dashes)`, and `font_name` is the
/// PostScript name of a Standard 14 font.
pub struct WidgetEdit {
    pub rect: Option<Rect>,
    pub flags: Option<i64>,
    pub border: Option<(f64, String, Vec<f64>)>,
    pub border_color: Option<(f64, f64, f64)>,
    pub background_color: Option<(f64, f64, f64)>,
    pub caption: Option<String>,
    pub rotation: Option<i64>,
    pub text_color: Option<(f64, f64, f64)>,
    pub font_name: Option<String>,
    pub font_size: Option<f64>,
    /// Always given, so that the edit is not all defaults.
    pub clear: Vec<String>,
}

impl WidgetEdit {
    /// Returns the `/DA` abbreviation of the edit's Standard 14 font.
    fn font_resource(&self) -> Result<Option<(&'static str, &'static str)>> {
        let Some(name) = &self.font_name else {
            return Ok(None);
        };
        DA_FONTS
            .iter()
            .find(|(_, font)| font == name)
            .map(|&(resource, font)| Some((resource, font)))
            .ok_or_else(|| Error::Pdf(format!("unknown Standard 14 font {name:?}")))
    }

    /// Applies the edit to `dict`, a widget on a page of `annots`;
    /// `font_resource` is the `/DR` name of the edit's font.
    fn apply(
        &self,
        annots: &PageAnnots,
        dict: &mut Dict,
        font_resource: Option<String>,
        budget: &Budget,
    ) -> Result<()> {
        if self
            .caption
            .as_ref()
            .is_some_and(|caption| caption.len() > MAX_INPUT_BYTES)
        {
            return Err(Error::Limit {
                code: LimitCode::AnnotationInputSize,
                message: format!(
                    "a widget caption exceeds the {MAX_INPUT_BYTES}-byte UTF-8 safety limit"
                ),
            });
        }
        let mut mark = match annots.resolved(dict.get("MK"))? {
            Some(Value::Dict(mark)) => mark,
            _ => Dict::new(),
        };
        let marked = !mark.is_empty();
        if let Some(caption) = &self.caption {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /CA key"
            )]
            mark.insert(Name::from("CA"), encode_text(caption));
        }
        if let Some(rotation) = self.rotation {
            if rotation % 90 != 0 {
                return Err(Error::Pdf(format!(
                    "a widget rotation is a multiple of 90 degrees: {rotation}"
                )));
            }
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /R key"
            )]
            mark.insert(Name::from("R"), Value::Integer(rotation.rem_euclid(360)));
        }
        if let Some(color) = self.border_color {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /BC key"
            )]
            mark.insert(Name::from("BC"), rgb(color, "border color")?);
        }
        if let Some(color) = self.background_color {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /BG key"
            )]
            mark.insert(Name::from("BG"), rgb(color, "background color")?);
        }
        for property in &self.clear {
            match property.as_str() {
                "border_color" => drop(mark.remove("BC")),
                "background_color" => drop(mark.remove("BG")),
                "caption" => drop(mark.remove("CA")),
                "rotation" => drop(mark.remove("R")),
                "border" => {
                    dict.remove("BS");
                    dict.remove("Border");
                }
                other => {
                    return Err(Error::Pdf(format!(
                        "a widget property that cannot be removed: {other:?}"
                    )));
                }
            }
        }
        if !mark.is_empty() {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /MK key"
            )]
            dict.insert(Name::from("MK"), Value::Dict(mark));
        } else if marked {
            dict.remove("MK");
        }
        if let Some(border) = &self.border {
            dict.remove("Border");
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /BS key"
            )]
            dict.insert(Name::from("BS"), border_style(border, annots.unit, budget)?);
        }
        if let Some(flags) = self.flags {
            if !(0..=1023).contains(&flags) {
                return Err(Error::Pdf("annotation flags are bits 1 to 10".into()));
            }
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /F key"
            )]
            dict.insert(Name::from("F"), Value::Integer(flags));
        }
        if let Some(area @ (x0, y0, x1, y1)) = self.rect {
            if ![x0, y0, x1, y1].iter().all(|value| value.is_finite()) || x1 <= x0 || y1 <= y0 {
                return Err(Error::Pdf(
                    "a widget rectangle is finite and not empty".into(),
                ));
            }
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /Rect key"
            )]
            dict.insert(Name::from("Rect"), annots.to_pdf(area));
        }
        if self.text_color.is_some() || self.font_name.is_some() || self.font_size.is_some() {
            let current = match widgets::text_appearance(&annots.reader(), dict)? {
                Some(current) => current,
                None => DefaultAppearance::parse(b"")
                    .map_err(|_| Error::Pdf("an empty /DA does not parse".into()))?,
            };
            let color = self.text_color.unwrap_or(current.color);
            rgb(color, "text color")?;
            let resource = font_resource.unwrap_or(current.font);
            let size = match self.font_size {
                Some(size) => {
                    if !(size.is_finite() && (0.0..=10_000.0).contains(&size)) {
                        return Err(Error::Pdf(
                            "a widget font size is from 0, which fits the text, to 10000".into(),
                        ));
                    }
                    pdf_font_size(size, annots.unit)?
                }
                None => {
                    let points = current.size * annots.unit;
                    if !(points.is_finite() && (0.0..=10_000.0).contains(&points)) {
                        return Err(Error::Pdf(
                            "a widget font size is from 0, which fits the text, to 10000".into(),
                        ));
                    }
                    if current.size == 0.0 {
                        0.0
                    } else {
                        current.size
                    }
                }
            };
            let (red, green, blue) = color;
            let da = format!("/{resource} {size} Tf {red} {green} {blue} rg");
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /DA key"
            )]
            dict.insert(
                Name::from("DA"),
                Value::String(PdfString::literal(da.into_bytes())),
            );
        }
        Ok(())
    }
}

pub(super) fn pdf_font_size(size: f64, unit: f64) -> Result<f64> {
    if size == 0.0 {
        return Ok(0.0);
    }
    let pdf = size / unit;
    if !(pdf.is_finite() && pdf > 0.0) {
        return Err(Error::Pdf(
            "a widget font size is not representable in user space".into(),
        ));
    }
    Ok(pdf)
}

/// Changes the widget's geometry and style and redraws it for its field value.
pub fn edit_widget(
    transaction: &mut Transaction,
    annots: &PageAnnots<'_>,
    index: u32,
    id: Id,
    edit: &WidgetEdit,
    budget: &Budget,
    state: (&FillCache, bool),
) -> Result<Id> {
    let (cache, draft) = state;
    let before = base(transaction, draft);
    let (position, mut dict) = annots.find(index, id)?;
    if annots.name(&dict, "Subtype")?.as_deref() != Some(b"Widget") {
        return Err(Error::Stale(format!(
            "annotation {} is no longer a widget",
            id.0
        )));
    }
    // The font resolves through the form's /DR, as a created
    // field's does, so that a name that another font holds there
    // is not drawn in that font.
    let font_resource = match edit.font_resource()? {
        Some((preferred, base)) => {
            let holder = form_object(transaction, budget)?;
            Some(register_standard_font(
                transaction,
                holder,
                preferred,
                base,
                budget,
            )?)
        }
        None => None,
    };
    edit.apply(annots, &mut dict, font_resource, budget)?;
    let unit = annots.unit;
    annots.write(
        transaction,
        position,
        false,
        dict,
        |transaction, reference, dict, _, budget| {
            restyle(transaction, reference, dict, unit, budget, (cache, before))?;
            Ok(false)
        },
    )
}

/// Writes widget `reference`, `dict`, draws it again as [`edit_widget`]
/// does, leaves `dict` as written, and sets `/NeedAppearances` while a
/// widget of the form lacks an appearance, as the form index of `before`
/// refreshed with the edit finds.
fn restyle(
    transaction: &mut Transaction,
    reference: Reference,
    dict: &mut Dict,
    unit: f64,
    budget: &Budget,
    (cache, before): (&FillCache, Arc<Version>),
) -> Result<()> {
    let number = reference.number();
    let written = Value::Dict(std::mem::take(dict));
    transaction.write(number, &written, budget)?;
    let Value::Dict(written) = written else {
        unreachable!("a written dictionary remains a dictionary");
    };
    *dict = written;
    let version = transaction.version();
    let drawing = widgets::restyled(&Reader::on_page(version, budget, unit), dict)?;
    if let Some(drawing) = drawing {
        widgets::write(transaction, number, dict, drawing, budget)?;
        if let Some(drawn) = dict_at(transaction, reference, budget)? {
            *dict = drawn;
        }
    }
    let index = cache.get(&before, budget)?;
    if !index.topology.form.fields.is_empty() {
        let after = transaction.snapshot();
        let changes = before
            .changes(&after, budget)?
            .ok_or_else(|| Error::Pdf("form versions have different origins".into()))?;
        let mut appearances = index.appearances.clone();
        appearances.refresh(&after, changes.objects(), changes.trailer(), budget)?;
        let missing = appearances.missing.iter().next().is_some();
        update_need_appearances(transaction, missing, budget)?;
    }
    transaction.retain_derived_version(before);
    Ok(())
}
