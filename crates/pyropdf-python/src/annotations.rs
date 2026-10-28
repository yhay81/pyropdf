//! Annotation handles: the annotations that `page.annotations` lists, their
//! properties, and the edits of them (DESIGN.md section 10).
//!
//! A handle names its page, its annotation's object, and the position where
//! it last found it in the page's `/Annots`; an annotation stored there as a
//! direct dictionary has object `(0, 0)` and is found by its position alone.
//! An edit that could shift or rewrite such a dictionary first stores every
//! direct annotation of the page as an object of its own, so that a handle
//! made before then goes stale instead of naming another annotation.
//!
//! An edit that changes how an annotation looks marks its appearance stale,
//! and the appearance settler draws it again once per transaction (section
//! 7).

use std::sync::Arc;

use pyo3::prelude::*;
use pyropdf_store::budget::Budget;
use pyropdf_store::pages::{Page, PageIndex};
use pyropdf_store::settle::Structure;
use pyropdf_store::value::{Array, Dict, Name, PdfString, Reference, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::document::{_Document, text_string};
use crate::errors::{LimitError, PdfError, StaleObjectError, store_error};
use crate::pages::{display_box, index, number, rect, resolve, rotation};

/// The most entries of a page's `/Annots` that a read or an edit accepts.
const MAX_PAGE_ANNOTATIONS: usize = 4_096;
/// The most bytes of annotation text that one read encodes or returns, and
/// that one edit writes.
const MAX_ANNOTATION_TEXT_BYTES: usize = 1024 * 1024;
/// The most dash lengths of a border.
const MAX_BORDER_DASHES: usize = 32;

/// The text entries of a [`Record`], in its order.
const TEXT_KEYS: [&str; 8] = [
    "Contents",
    "Name",
    "T",
    "CreationDate",
    "M",
    "Subj",
    "NM",
    "IT",
];

/// The blend modes of ISO 32000-2 Table 134, which `/BM` may name.
const BLEND_MODES: [&str; 16] = [
    "Normal",
    "Multiply",
    "Screen",
    "Overlay",
    "Darken",
    "Lighten",
    "ColorDodge",
    "ColorBurn",
    "HardLight",
    "SoftLight",
    "Difference",
    "Exclusion",
    "Hue",
    "Saturation",
    "Color",
    "Luminosity",
];

/// The icons of a note (ISO 32000-2 section 12.5.6.4).
const NOTE_ICONS: [&str; 7] = [
    "Comment",
    "Help",
    "Insert",
    "Key",
    "NewParagraph",
    "Note",
    "Paragraph",
];

/// The stamps of ISO 32000-2 section 12.5.6.12.
const STAMP_ICONS: [&str; 14] = [
    "Approved",
    "AsIs",
    "Confidential",
    "Departmental",
    "Experimental",
    "Expired",
    "Final",
    "ForComment",
    "ForPublicRelease",
    "NotApproved",
    "NotForPublicRelease",
    "Sold",
    "TopSecret",
    "Draft",
];

/// The icons of a file attachment (ISO 32000-2 section 12.5.6.15).
const FILE_ICONS: [&str; 4] = ["Graph", "PushPin", "Paperclip", "Tag"];

/// The types whose `/Rect` an edit may move; the others follow their points.
const RECT_SUBTYPES: [&str; 7] = [
    "Square",
    "Circle",
    "Text",
    "FreeText",
    "Stamp",
    "Caret",
    "FileAttachment",
];

/// A display rectangle, `(x0, y0, x1, y1)`.
type Rect = (f64, f64, f64, f64);

/// One annotation that `page.annotations` lists: its position in `/Annots`;
/// its object, `(0, 0)` for a direct dictionary; its subtype; its display
/// rectangle; its `/F` flags; the texts of [`TEXT_KEYS`]; its `/C` and
/// `/IC` components; its `/CA` opacity; its border's width, `/S` style, and
/// dashes; its `/BM` blend mode; and the object that `/IRT` names, the
/// display rectangle of its popup, and whether the popup, or else the
/// annotation, is open.
type Record = (
    u32,
    (u32, u16),
    Option<String>,
    Rect,
    i64,
    [Option<String>; 8],
    Vec<f64>,
    Vec<f64>,
    Option<f64>,
    Option<(f64, String, Vec<f64>)>,
    Option<&'static str>,
    (Option<(u32, u16)>, Option<Rect>, bool),
);

/// The annotations of one page in the version that reads see.
struct Annots {
    version: Arc<Version>,
    index: Arc<PageIndex>,
    at: usize,
    budget: Budget,
    crop: [f64; 4],
    rotation: i64,
    /// The array object that the page's `/Annots` names, if it names one.
    array: Option<Reference>,
    items: Array,
}

impl Annots {
    /// Reads the annotations of page object `(number, generation)`, or
    /// returns `None` when it is not a page of the document.
    fn read(document: &_Document, number: u32, generation: u16) -> PyResult<Option<Self>> {
        let (version, index, budget) = index(document)?;
        let Some(at) = index.index_of(Reference::new(number, generation)) else {
            return Ok(None);
        };
        let page = &index.pages()[at];
        let crop = display_box(page);
        let rotation = rotation(page.rotate());
        let annots = page.dict().get("Annots");
        let array = match annots {
            Some(Value::Reference(reference)) => Some(*reference),
            _ => None,
        };
        let items = match resolve(&version, annots, &budget)? {
            Some(Value::Array(items)) => items,
            _ => Array::new(),
        };
        if items.len() > MAX_PAGE_ANNOTATIONS {
            return Err(PdfError::new_err(format!(
                "page annotations exceed the {MAX_PAGE_ANNOTATIONS}-entry safety limit"
            )));
        }
        Ok(Some(Self {
            version,
            index,
            at,
            budget,
            crop,
            rotation,
            array,
            items,
        }))
    }

    fn page(&self) -> &Page {
        &self.index.pages()[self.at]
    }

    /// Returns the object and dictionary of the entry `item`, or `None`
    /// for an entry that is not an annotation dictionary.
    fn entry(&self, item: &Value) -> PyResult<Option<((u32, u16), Dict)>> {
        let id = match item {
            Value::Reference(reference) => (reference.number(), reference.generation()),
            Value::Dict(dict) => return Ok(Some(((0, 0), dict.clone()))),
            _ => return Ok(None),
        };
        Ok(match resolve(&self.version, Some(item), &self.budget)? {
            Some(Value::Dict(dict)) => Some((id, dict)),
            _ => None,
        })
    }

    /// Returns the position of the annotation that a handle names: object
    /// `id`, found at `index` or, after an edit moved it, at its first
    /// position; or, for `(0, 0)`, the direct dictionary at `index`.
    fn position(&self, index: u32, id: (u32, u16)) -> PyResult<usize> {
        let names = |item: &Value| {
            matches!(item, Value::Reference(reference)
                if (reference.number(), reference.generation()) == id)
        };
        let index = index as usize;
        let position = match self.items.get(index) {
            Some(item) if id != (0, 0) && names(item) => Some(index),
            Some(Value::Dict(_)) if id == (0, 0) => Some(index),
            _ if id != (0, 0) => self.items.iter().position(names),
            _ => None,
        };
        position.ok_or_else(|| {
            StaleObjectError::new_err(format!(
                "annotation {} is no longer in the page's /Annots",
                id.0
            ))
        })
    }

    /// Returns the position and dictionary of the annotation that a handle
    /// names, as [`Annots::position`] finds it.
    fn find(&self, index: u32, id: (u32, u16)) -> PyResult<(usize, Dict)> {
        let position = self.position(index, id)?;
        match self.entry(&self.items.as_slice()[position])? {
            Some((_, dict)) => Ok((position, dict)),
            None => Err(StaleObjectError::new_err(format!(
                "annotation {} is no longer a dictionary",
                id.0
            ))),
        }
    }

    /// Converts a PDF rectangle to display space.
    fn to_display(&self, (x0, y0, x1, y1): Rect) -> Rect {
        let (a, b) = to_display(self.crop, self.rotation, x0, y0);
        let (c, d) = to_display(self.crop, self.rotation, x1, y1);
        (a.min(c), b.min(d), a.max(c), b.max(d))
    }

    /// Converts a display rectangle to a PDF `/Rect` array.
    fn to_pdf(&self, (x0, y0, x1, y1): Rect) -> Value {
        let (a, b) = to_pdf(self.crop, self.rotation, x0, y0);
        let (c, d) = to_pdf(self.crop, self.rotation, x1, y1);
        Value::Array(Array::from(vec![
            number(a.min(c)),
            number(b.min(d)),
            number(a.max(c)),
            number(b.max(d)),
        ]))
    }

    /// Returns the record of the annotation `dict` at `index`, or `None`
    /// for a link, a widget, a popup, or an annotation without a rectangle.
    /// `budget` holds the text bytes that the call encoded and returned.
    fn record(
        &self,
        index: usize,
        id: (u32, u16),
        dict: &Dict,
        budget: &mut (usize, usize),
    ) -> PyResult<Option<Record>> {
        let subtype = self.name(dict, "Subtype")?;
        // Links and widgets have sequences of their own, and a popup
        // belongs to the annotation that names it.
        if matches!(subtype.as_deref(), Some(b"Link" | b"Widget" | b"Popup")) {
            return Ok(None);
        }
        let Some(area) = self.rect(dict.get("Rect"))? else {
            return Ok(None);
        };
        let subtype = subtype
            .map(|name| charge_text(&name, budget, "subtype text"))
            .transpose()?;
        let mut texts: [Option<String>; 8] = Default::default();
        for (slot, key) in texts.iter_mut().zip(TEXT_KEYS) {
            *slot = if key == "Name" || key == "IT" {
                self.name(dict, key)?
                    .map(|name| charge_text(&name, budget, "icon name"))
                    .transpose()?
            } else {
                match self.resolved(dict.get(key))? {
                    Some(Value::String(text)) => {
                        Some(charge_decoded(text.as_bytes(), budget, key)?)
                    }
                    _ => None,
                }
            };
        }
        let popup = match self.resolved(dict.get("Popup"))? {
            Some(Value::Dict(popup)) => Some(popup),
            _ => None,
        };
        let popup_rect = match &popup {
            Some(popup) => self.rect(popup.get("Rect"))?,
            None => None,
        };
        let open = matches!(
            self.resolved(popup.as_ref().unwrap_or(dict).get("Open"))?,
            Some(Value::Boolean(true))
        );
        let reply = match dict.get("IRT") {
            Some(Value::Reference(reference)) => Some((reference.number(), reference.generation())),
            _ => None,
        };
        let blend = self.name(dict, "BM")?.and_then(|name| {
            BLEND_MODES
                .iter()
                .find(|mode| mode.as_bytes() == name.as_slice())
                .copied()
        });
        Ok(Some((
            u32::try_from(index).unwrap_or(u32::MAX),
            id,
            subtype,
            self.to_display(area),
            match self.resolved(dict.get("F"))? {
                Some(Value::Integer(flags)) => flags,
                _ => 0,
            },
            texts,
            self.color(dict, "C")?,
            self.color(dict, "IC")?,
            self.number(dict.get("CA"))?
                .filter(|opacity| opacity.is_finite()),
            self.border(dict)?,
            blend,
            (reply, popup_rect.map(|area| self.to_display(area)), open),
        )))
    }

    fn resolved(&self, value: Option<&Value>) -> PyResult<Option<Value>> {
        resolve(&self.version, value, &self.budget)
    }

    fn name(&self, dict: &Dict, key: &str) -> PyResult<Option<Vec<u8>>> {
        Ok(match self.resolved(dict.get(key))? {
            Some(Value::Name(name)) => Some(name.as_bytes().to_vec()),
            _ => None,
        })
    }

    fn number(&self, value: Option<&Value>) -> PyResult<Option<f64>> {
        Ok(match self.resolved(value)? {
            Some(Value::Integer(value)) => Some(value as f64),
            Some(Value::Real(value)) => Some(value.value()),
            _ => None,
        })
    }

    /// Reads a rectangle, its numbers resolved.
    fn rect(&self, value: Option<&Value>) -> PyResult<Option<Rect>> {
        let Some(Value::Array(items)) = self.resolved(value)? else {
            return Ok(None);
        };
        if items.len() != 4 {
            return Ok(None);
        }
        let mut numbers = Vec::with_capacity(4);
        for item in items.iter() {
            match self.number(Some(item))? {
                Some(number) => numbers.push(Value::from(number)),
                None => return Ok(None),
            }
        }
        Ok(rect(&Value::Array(Array::from(numbers))))
    }

    /// Reads up to `max` numbers of an array, or `None` for anything else.
    fn numbers(&self, value: Option<&Value>, max: usize) -> PyResult<Option<Vec<f64>>> {
        let Some(Value::Array(items)) = self.resolved(value)? else {
            return Ok(None);
        };
        if items.len() > max {
            return Ok(None);
        }
        let mut numbers = Vec::with_capacity(items.len());
        for item in items.iter() {
            match self.number(Some(item))? {
                Some(number) => numbers.push(number),
                None => return Ok(None),
            }
        }
        Ok(Some(numbers))
    }

    /// Reads a `/C` or `/IC` array of up to four numbers, empty otherwise.
    fn color(&self, dict: &Dict, key: &str) -> PyResult<Vec<f64>> {
        Ok(self.numbers(dict.get(key), 4)?.unwrap_or_default())
    }

    /// Reads the border that `/BS`, else `/Border`, describes.
    fn border(&self, dict: &Dict) -> PyResult<Option<(f64, String, Vec<f64>)>> {
        if let Some(Value::Dict(style)) = self.resolved(dict.get("BS"))? {
            let width = self
                .number(style.get("W"))?
                .filter(|width| *width >= 0.0)
                .unwrap_or(1.0);
            let name = self
                .name(&style, "S")?
                .filter(|name| matches!(name.as_slice(), b"S" | b"D" | b"B" | b"I" | b"U"))
                .unwrap_or_else(|| b"S".to_vec());
            let dashes = if name == b"D" {
                self.numbers(style.get("D"), MAX_BORDER_DASHES)?
                    .filter(|dashes| valid_dashes(dashes))
                    .unwrap_or_else(|| vec![3.0])
            } else {
                Vec::new()
            };
            return Ok(Some((
                width,
                String::from_utf8_lossy(&name).into_owned(),
                dashes,
            )));
        }
        let Some(Value::Array(items)) = self.resolved(dict.get("Border"))? else {
            return Ok(None);
        };
        if !(3..=4).contains(&items.len()) {
            return Ok(None);
        }
        let Some(width) = self.number(items.get(2))?.filter(|width| *width >= 0.0) else {
            return Ok(None);
        };
        let dashes = match items.get(3) {
            Some(pattern) => match self.numbers(Some(pattern), MAX_BORDER_DASHES)? {
                Some(dashes) if valid_dashes(&dashes) => dashes,
                _ => return Ok(None),
            },
            None => Vec::new(),
        };
        let style = if dashes.is_empty() { "S" } else { "D" };
        Ok(Some((width, style.to_owned(), dashes)))
    }

    /// Returns whether another page names the array object of `/Annots`.
    fn shared(&self) -> bool {
        let Some(array) = self.array else {
            return false;
        };
        self.index.pages().iter().enumerate().any(|(at, page)| {
            at != self.at
                && matches!(page.dict().get("Annots"), Some(Value::Reference(other)) if *other == array)
        })
    }

    /// Writes the edit of the annotation at `position`: stores the page's
    /// direct annotations as objects when that annotation is one, lets
    /// `change` edit the annotation's dictionary and the page's
    /// `/Annots`, and writes both. `change` gets the annotation's
    /// reference and returns whether it changed `/Annots`. With `look`, the
    /// edit marks the annotation's appearance stale. Returns the
    /// annotation's object.
    fn write(
        &self,
        py: Python<'_>,
        document: &_Document,
        position: usize,
        look: bool,
        mut dict: Dict,
        change: impl FnOnce(
            &mut Transaction,
            Reference,
            &mut Dict,
            &mut Array,
            &Budget,
        ) -> PyResult<bool>,
    ) -> PyResult<(u32, u16)> {
        let mut items = self.items.clone();
        document.edit(py, |transaction, budget| {
            let mut listed = false;
            // A handle finds a direct dictionary by its position alone.
            if matches!(self.items.get(position), Some(Value::Dict(_))) {
                for item in items.iter_mut() {
                    if let Value::Dict(entry) = item {
                        let reference = transaction.allocate().map_err(|e| store_error(&e))?;
                        transaction
                            .write(reference.number(), &Value::Dict(entry.clone()), budget)
                            .map_err(|e| store_error(&e))?;
                        *item = Value::Reference(reference);
                        listed = true;
                    }
                }
            }
            let Some(Value::Reference(reference)) = items.get(position).cloned() else {
                return Err(PdfError::new_err(
                    "the annotation changed while it was edited",
                ));
            };
            listed |= change(transaction, reference, &mut dict, &mut items, budget)?;
            transaction
                .write(reference.number(), &Value::Dict(dict), budget)
                .map_err(|e| store_error(&e))?;
            if listed {
                self.write_items(transaction, items, budget)?;
            }
            if look {
                transaction
                    .mark(Structure::Appearance, reference.number(), budget)
                    .map_err(|e| store_error(&e))?;
            }
            Ok((reference.number(), reference.generation()))
        })
    }

    /// Writes the page's `/Annots` as `items`: into the array object that
    /// it names, unless another page shares that array, else into the page
    /// dictionary.
    fn write_items(
        &self,
        transaction: &mut Transaction,
        items: Array,
        budget: &Budget,
    ) -> PyResult<()> {
        if let Some(array) = self.array
            && !self.shared()
        {
            transaction
                .write(array.number(), &Value::Array(items), budget)
                .map_err(|e| store_error(&e))?;
            return Ok(());
        }
        let page = self.page();
        let Some(object) = page.object() else {
            return Err(PdfError::new_err("the page is not an indirect object"));
        };
        let mut dict = page.dict().clone();
        dict.insert(Name::from("Annots"), Value::Array(items));
        transaction
            .write(object.number(), &Value::Dict(dict), budget)
            .map_err(|e| store_error(&e))?;
        Ok(())
    }
}

/// Converts a point of PDF user space to display space on a page whose
/// display box is `crop` and whose normalized `/Rotate` is `rotation`.
fn to_display(crop: [f64; 4], rotation: i64, x: f64, y: f64) -> (f64, f64) {
    let [cx0, cy0, cx1, cy1] = crop;
    match rotation {
        90 => (y - cy0, x - cx0),
        180 => (cx1 - x, y - cy0),
        270 => (cy1 - y, cx1 - x),
        _ => (x - cx0, cy1 - y),
    }
}

/// Converts a point of display space to PDF user space, as the inverse of
/// [`to_display`].
fn to_pdf(crop: [f64; 4], rotation: i64, x: f64, y: f64) -> (f64, f64) {
    let [cx0, cy0, cx1, cy1] = crop;
    match rotation {
        90 => (cx0 + y, cy0 + x),
        180 => (cx1 - x, cy0 + y),
        270 => (cx1 - y, cy1 - x),
        _ => (cx0 + x, cy1 - y),
    }
}

/// Returns whether dash lengths are a pattern that draws: finite, not
/// negative, and not all zero.
fn valid_dashes(dashes: &[f64]) -> bool {
    dashes.iter().all(|dash| dash.is_finite() && *dash >= 0.0) && dashes.iter().sum::<f64>() > 0.0
}

/// Adds `amount` to one total of a read's text budget.
fn charge(total: &mut usize, amount: usize, label: &str) -> PyResult<()> {
    *total = total.saturating_add(amount);
    if *total > MAX_ANNOTATION_TEXT_BYTES {
        return Err(PdfError::new_err(format!(
            "annotation {label} exceeds the {MAX_ANNOTATION_TEXT_BYTES}-byte safety limit"
        )));
    }
    Ok(())
}

/// Returns a name as text, after charging its bytes.
fn charge_text(
    bytes: &[u8],
    (encoded, returned): &mut (usize, usize),
    label: &str,
) -> PyResult<String> {
    charge(encoded, bytes.len(), &format!("encoded {label}"))?;
    let text = String::from_utf8_lossy(bytes).into_owned();
    charge(returned, text.len(), &format!("returned {label}"))?;
    Ok(text)
}

/// Decodes a text string, after charging its bytes.
fn charge_decoded(
    bytes: &[u8],
    (encoded, returned): &mut (usize, usize),
    key: &str,
) -> PyResult<String> {
    let label = if key == "Contents" {
        "Contents text"
    } else {
        "annotation text"
    };
    charge(encoded, bytes.len(), &format!("encoded {label}"))?;
    let text = text_string(bytes);
    charge(returned, text.len(), &format!("returned {label}"))?;
    Ok(text)
}

/// Encodes a text string as `String.text` reads it: ASCII as is, anything
/// else as UTF-16BE.
fn encode_text(text: &str) -> Value {
    let bytes = if text.is_ascii() {
        text.as_bytes().to_vec()
    } else {
        let mut bytes = vec![0xFE, 0xFF];
        bytes.extend(text.encode_utf16().flat_map(u16::to_be_bytes));
        bytes
    };
    Value::String(PdfString::literal(bytes))
}

fn rgb(components: (f64, f64, f64), label: &str) -> PyResult<Value> {
    let (red, green, blue) = components;
    if ![red, green, blue]
        .iter()
        .all(|c| c.is_finite() && (0.0..=1.0).contains(c))
    {
        return Err(PdfError::new_err(format!(
            "an annotation {label} has components from 0 to 1"
        )));
    }
    Ok(Value::Array(Array::from(vec![
        number(red),
        number(green),
        number(blue),
    ])))
}

/// Lists the annotations of page object `(number, generation)` that
/// `page.annotations` lists, or returns `None` for a page that is gone.
#[pyfunction]
pub fn annotation_list(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
) -> PyResult<Option<Vec<Record>>> {
    let Some(annots) = Annots::read(document, number, generation)? else {
        return Ok(None);
    };
    py.detach(|| {
        let mut records = Vec::new();
        let mut budget = (0, 0);
        for (index, item) in annots.items.iter().enumerate() {
            let Some((id, dict)) = annots.entry(item)? else {
                continue;
            };
            if let Some(record) = annots.record(index, id, &dict, &mut budget)? {
                records.push(record);
            }
        }
        Ok(Some(records))
    })
}

/// Reads the annotation that a handle names. Returns `None` for a page that
/// is gone, and raises `StaleObjectError` for an annotation that is.
#[pyfunction]
pub fn annotation_read(
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
) -> PyResult<Option<Record>> {
    let Some(annots) = Annots::read(document, number, generation)? else {
        return Ok(None);
    };
    let (position, dict) = annots.find(index, id)?;
    annots
        .record(position, id, &dict, &mut (0, 0))?
        .map(Some)
        .ok_or_else(|| {
            StaleObjectError::new_err(format!(
                "annotation {} is no longer one that page.annotations lists",
                id.0
            ))
        })
}

/// The properties that one edit of an annotation sets, as
/// `Annotation._set` passes them. Colors are RGB from 0 to 1, the border is
/// its width, `/S` style, and dash lengths, and the blend mode and the icon
/// are PDF names.
#[derive(FromPyObject)]
#[pyo3(from_item_all)]
pub struct Edit {
    #[pyo3(default)]
    rect: Option<Rect>,
    #[pyo3(default)]
    contents: Option<String>,
    #[pyo3(default)]
    author: Option<String>,
    #[pyo3(default)]
    subject: Option<String>,
    #[pyo3(default)]
    name: Option<String>,
    #[pyo3(default)]
    language: Option<String>,
    #[pyo3(default)]
    icon: Option<String>,
    #[pyo3(default)]
    color: Option<(f64, f64, f64)>,
    #[pyo3(default)]
    interior_color: Option<(f64, f64, f64)>,
    #[pyo3(default)]
    opacity: Option<f64>,
    #[pyo3(default)]
    flags: Option<i64>,
    #[pyo3(default)]
    border: Option<(f64, String, Vec<f64>)>,
    #[pyo3(default)]
    clouds: Option<f64>,
    #[pyo3(default)]
    blend_mode: Option<String>,
    #[pyo3(default)]
    rotate: Option<i64>,
    #[pyo3(default)]
    modified: Option<String>,
    clear: Vec<String>,
}

impl Edit {
    /// Applies the edit to `dict`, an annotation of `subtype` on a page of
    /// `annots`, refusing an edit that does not fit the annotation.
    fn apply(&self, annots: &Annots, dict: &mut Dict, subtype: &str) -> PyResult<()> {
        let texts = [
            (&self.contents, "Contents"),
            (&self.author, "T"),
            (&self.subject, "Subj"),
            (&self.name, "NM"),
            (&self.language, "Lang"),
        ];
        let size = texts
            .iter()
            .map(|(text, _)| text.as_deref().map_or(0, str::len))
            .fold(subtype.len(), usize::saturating_add);
        if size > MAX_ANNOTATION_TEXT_BYTES {
            return Err(LimitError::new_err((
                "annotation_input_size",
                format!(
                    "annotation subtype and texts input exceed the {MAX_ANNOTATION_TEXT_BYTES}-byte UTF-8 safety limit"
                ),
            )));
        }
        for (text, key) in texts {
            if let Some(text) = text {
                dict.insert(Name::from(key), encode_text(text));
            }
        }
        for property in &self.clear {
            let key = match property.as_str() {
                "contents" => "Contents",
                "author" => "T",
                "subject" => "Subj",
                "name" => "NM",
                "language" => "Lang",
                "color" => "C",
                "interior_color" => "IC",
                "blend_mode" => "BM",
                "border" => {
                    dict.remove("Border");
                    "BS"
                }
                "clouds" => "BE",
                other => {
                    return Err(PdfError::new_err(format!(
                        "an annotation property that cannot be removed: {other:?}"
                    )));
                }
            };
            dict.remove(key);
        }
        if let Some(icon) = &self.icon {
            let known: &[&str] = match subtype {
                "Text" => &NOTE_ICONS,
                "Stamp" => &STAMP_ICONS,
                "FileAttachment" => &FILE_ICONS,
                _ => {
                    return Err(PdfError::new_err(
                        "only a note, a stamp, or a file attachment has an icon",
                    ));
                }
            };
            if !known.contains(&icon.as_str()) {
                return Err(PdfError::new_err(format!("unknown icon {icon:?}")));
            }
            dict.insert(Name::from("Name"), Value::Name(Name::from(icon.as_str())));
        }
        if let Some(color) = self.color {
            dict.insert(Name::from("C"), rgb(color, "color")?);
        }
        if let Some(color) = self.interior_color {
            dict.insert(Name::from("IC"), rgb(color, "interior color")?);
        }
        if let Some(opacity) = self.opacity {
            if !(opacity.is_finite() && opacity > 0.0 && opacity <= 1.0) {
                return Err(PdfError::new_err(
                    "an annotation opacity is greater than 0 and at most 1",
                ));
            }
            if opacity < 1.0 {
                dict.insert(Name::from("CA"), Value::from(opacity));
            } else {
                dict.remove("CA");
            }
        }
        if let Some(flags) = self.flags {
            if !(0..=1023).contains(&flags) {
                return Err(PdfError::new_err("annotation flags are bits 1 to 10"));
            }
            dict.insert(Name::from("F"), Value::Integer(flags));
        }
        if let Some((width, style, dashes)) = &self.border {
            if !(width.is_finite() && *width >= 0.0)
                || !matches!(style.as_str(), "S" | "D" | "B" | "I" | "U")
                || dashes.len() > MAX_BORDER_DASHES
                || (!dashes.is_empty() && !valid_dashes(dashes))
                || (style == "D") == dashes.is_empty()
            {
                return Err(PdfError::new_err(
                    "the border is not one that pyropdf writes",
                ));
            }
            let mut border = Dict::new();
            border.insert(Name::from("W"), number(*width));
            border.insert(Name::from("S"), Value::Name(Name::from(style.as_str())));
            if !dashes.is_empty() {
                let dashes = dashes.iter().map(|dash| number(*dash)).collect::<Vec<_>>();
                border.insert(Name::from("D"), Value::Array(Array::from(dashes)));
            }
            dict.remove("Border");
            dict.insert(Name::from("BS"), Value::Dict(border));
        }
        if let Some(intensity) = self.clouds {
            if !matches!(subtype, "Square" | "Circle" | "Polygon") {
                return Err(PdfError::new_err(
                    "only a square, a circle, or a polygon has a cloudy border",
                ));
            }
            if !(intensity.is_finite() && intensity > 0.0 && intensity <= 2.0) {
                return Err(PdfError::new_err(
                    "a cloudy border's intensity is greater than 0 and at most 2",
                ));
            }
            let mut effect = Dict::new();
            effect.insert(Name::from("S"), Value::Name(Name::from("C")));
            effect.insert(Name::from("I"), number(intensity));
            dict.insert(Name::from("BE"), Value::Dict(effect));
        }
        if let Some(mode) = &self.blend_mode {
            if !BLEND_MODES.contains(&mode.as_str()) {
                return Err(PdfError::new_err(format!("unknown blend mode {mode:?}")));
            }
            dict.insert(Name::from("BM"), Value::Name(Name::from(mode.as_str())));
        }
        if let Some(turn) = self.rotate {
            if !matches!(subtype, "FreeText" | "Stamp" | "Caret" | "FileAttachment")
                || turn % 90 != 0
            {
                return Err(PdfError::new_err(
                    "free text, stamps, carets, and file attachments turn by multiples of 90 degrees",
                ));
            }
            let user_turn = (annots.rotation - turn).rem_euclid(360);
            if user_turn == 0 {
                dict.remove("Rotate");
            } else {
                dict.insert(Name::from("Rotate"), Value::Integer(user_turn));
            }
        }
        if let Some(area) = self.rect {
            if !RECT_SUBTYPES.contains(&subtype) || dict.get("CL").is_some() {
                return Err(PdfError::new_err(
                    "this annotation follows its points or callout; edit those instead of its rectangle",
                ));
            }
            let (x0, y0, x1, y1) = area;
            if ![x0, y0, x1, y1].iter().all(|value| value.is_finite()) {
                return Err(PdfError::new_err("an annotation rectangle is finite"));
            }
            dict.insert(Name::from("Rect"), annots.to_pdf(area));
        }
        if let Some(modified) = &self.modified {
            if !modified.starts_with("D:") || !modified.is_ascii() || modified.len() > 64 {
                return Err(PdfError::new_err("an annotation date is a PDF date string"));
            }
            dict.insert(
                Name::from("M"),
                Value::String(PdfString::literal(modified.as_bytes().to_vec())),
            );
        }
        Ok(())
    }
}

/// Reads the annotation that a handle names for an edit, or returns `None`
/// for a page that is gone.
fn edited(
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
) -> PyResult<Option<(Annots, usize, Dict)>> {
    let Some(annots) = Annots::read(document, number, generation)? else {
        return Ok(None);
    };
    let (position, dict) = annots.find(index, id)?;
    Ok(Some((annots, position, dict)))
}

/// Applies `edit` to the annotation that a handle names, marks its
/// appearance stale, and returns its object, which a direct dictionary
/// gains here.
#[pyfunction]
pub fn annotation_edit(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
    edit: Edit,
) -> PyResult<Option<(u32, u16)>> {
    let Some((annots, position, mut dict)) = edited(document, number, generation, index, id)?
    else {
        return Ok(None);
    };
    let subtype = annots.name(&dict, "Subtype")?.unwrap_or_default();
    edit.apply(&annots, &mut dict, &String::from_utf8_lossy(&subtype))?;
    annots
        .write(py, document, position, true, dict, |_, _, _, _, _| {
            Ok(false)
        })
        .map(Some)
}

/// Moves the popup of the annotation that a handle names to a display
/// rectangle, adding one that the page lists after it, or removes the popup
/// with `None`. Returns the annotation's object, or
/// `None` for a page that is gone.
#[pyfunction]
pub fn annotation_popup(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
    area: Option<Rect>,
) -> PyResult<Option<(u32, u16)>> {
    let Some((annots, position, dict)) = edited(document, number, generation, index, id)? else {
        return Ok(None);
    };
    if let Some((x0, y0, x1, y1)) = area
        && !([x0, y0, x1, y1].iter().all(|value| value.is_finite()) && x0 < x1 && y0 < y1)
    {
        return Err(PdfError::new_err(
            "a popup rectangle is finite and not empty",
        ));
    }
    let existing = match dict.get("Popup") {
        Some(Value::Reference(popup)) => match annots.resolved(Some(&Value::Reference(*popup)))? {
            Some(Value::Dict(popup_dict)) => Some((*popup, popup_dict)),
            _ => None,
        },
        _ => None,
    };
    if area.is_some() && existing.is_none() && annots.items.len() >= MAX_PAGE_ANNOTATIONS {
        return Err(PdfError::new_err(format!(
            "page annotations exceed the {MAX_PAGE_ANNOTATIONS}-entry safety limit"
        )));
    }
    let area = area.map(|area| annots.to_pdf(area));
    let page = annots.page().object();
    let written = annots.write(
        py,
        document,
        position,
        false,
        dict,
        |transaction, reference, dict, items, budget| match (area, existing) {
            (Some(area), Some((popup, mut popup_dict))) => {
                popup_dict.insert(Name::from("Rect"), area);
                transaction
                    .write(popup.number(), &Value::Dict(popup_dict), budget)
                    .map_err(|e| store_error(&e))?;
                Ok(false)
            }
            (Some(area), None) => {
                let popup = transaction.allocate().map_err(|e| store_error(&e))?;
                let mut popup_dict = Dict::new();
                popup_dict.insert(Name::from("Type"), Value::Name(Name::from("Annot")));
                popup_dict.insert(Name::from("Subtype"), Value::Name(Name::from("Popup")));
                popup_dict.insert(Name::from("Rect"), area);
                popup_dict.insert(Name::from("Parent"), Value::Reference(reference));
                if let Some(page) = page {
                    popup_dict.insert(Name::from("P"), Value::Reference(page));
                }
                popup_dict.insert(Name::from("Open"), Value::Boolean(false));
                transaction
                    .write(popup.number(), &Value::Dict(popup_dict), budget)
                    .map_err(|e| store_error(&e))?;
                dict.insert(Name::from("Popup"), Value::Reference(popup));
                items.push(Value::Reference(popup));
                Ok(true)
            }
            (None, existing) => {
                dict.remove("Popup");
                let Some((popup, _)) = existing else {
                    return Ok(false);
                };
                let kept = items
                    .iter()
                    .filter(|item| !matches!(item, Value::Reference(listed) if *listed == popup))
                    .cloned()
                    .collect::<Vec<_>>();
                let changed = kept.len() != items.len();
                *items = Array::from(kept);
                Ok(changed)
            }
        },
    )?;
    Ok(Some(written))
}

/// Opens or closes the popup of the annotation that a handle names, and a
/// note's own `/Open`. Returns the annotation's object, or
/// `None` for a page that is gone.
#[pyfunction]
pub fn annotation_open(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
    open: bool,
) -> PyResult<Option<(u32, u16)>> {
    let Some((annots, position, dict)) = edited(document, number, generation, index, id)? else {
        return Ok(None);
    };
    let popup = match dict.get("Popup") {
        Some(Value::Reference(popup)) => match annots.resolved(Some(&Value::Reference(*popup)))? {
            Some(Value::Dict(popup_dict)) => Some((*popup, popup_dict)),
            _ => None,
        },
        _ => None,
    };
    let is_note = annots.name(&dict, "Subtype")?.as_deref() == Some(b"Text");
    if popup.is_none() && !is_note {
        return Err(PdfError::new_err(
            "the annotation has no popup to open; set popup_rect first",
        ));
    }
    let written = annots.write(
        py,
        document,
        position,
        false,
        dict,
        |transaction, _, dict, _, budget| {
            if let Some((popup, mut popup_dict)) = popup {
                popup_dict.insert(Name::from("Open"), Value::Boolean(open));
                transaction
                    .write(popup.number(), &Value::Dict(popup_dict), budget)
                    .map_err(|e| store_error(&e))?;
            }
            if is_note {
                dict.insert(Name::from("Open"), Value::Boolean(open));
            }
            Ok(false)
        },
    )?;
    Ok(Some(written))
}

/// Makes the annotation that a handle names a reply to `target`, another
/// annotation object that the page lists (`/IRT`), or no reply with
/// `None`. Returns the annotation's object, or
/// `None` for a page that is gone.
#[pyfunction]
pub fn annotation_reply(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
    target: Option<(u32, u16)>,
) -> PyResult<Option<(u32, u16)>> {
    let Some((annots, position, dict)) = edited(document, number, generation, index, id)? else {
        return Ok(None);
    };
    let target = match target {
        Some(target) => {
            let target = Reference::new(target.0, target.1);
            let listed = annots
                .items
                .iter()
                .any(|item| matches!(item, Value::Reference(listed) if *listed == target));
            if !listed || (target.number(), target.generation()) == id {
                return Err(PdfError::new_err(
                    "an annotation replies to another annotation of its page",
                ));
            }
            Some(target)
        }
        None => None,
    };
    let written = annots.write(py, document, position, false, dict, |_, _, dict, _, _| {
        match target {
            Some(target) => {
                dict.insert(Name::from("IRT"), Value::Reference(target));
            }
            None => {
                dict.remove("IRT");
                dict.remove("RT");
            }
        }
        Ok(false)
    })?;
    Ok(Some(written))
}

/// Takes the annotations that `handles` name, each `(index, object)`, and
/// the popups they name when the page lists those too, out of the page's
/// `/Annots` in one edit, after storing the page's direct annotations as
/// objects. The objects stay in the document. Returns `false` for a page
/// that is gone.
#[pyfunction]
pub fn annotation_remove(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    handles: Vec<(u32, (u32, u16))>,
) -> PyResult<bool> {
    let Some(annots) = Annots::read(document, number, generation)? else {
        return Ok(false);
    };
    if handles.len() > MAX_PAGE_ANNOTATIONS {
        return Err(PdfError::new_err(format!(
            "page annotations exceed the {MAX_PAGE_ANNOTATIONS}-entry safety limit"
        )));
    }
    let mut positions = Vec::with_capacity(handles.len().saturating_mul(2));
    for (index, id) in handles {
        let (position, dict) = annots.find(index, id)?;
        positions.push(position);
        if let Some(Value::Reference(popup)) = dict.get("Popup") {
            positions.extend(
                annots
                    .items
                    .iter()
                    .position(|item| matches!(item, Value::Reference(listed) if listed == popup)),
            );
        }
    }
    if positions.is_empty() {
        return Ok(true);
    }
    // Remove from the end, so that each removal keeps the earlier positions.
    positions.sort_unstable_by(|a, b| b.cmp(a));
    positions.dedup();
    let mut items = annots.items.clone();
    document.edit(py, |transaction, budget| {
        for item in items.iter_mut() {
            if let Value::Dict(entry) = item {
                let reference = transaction.allocate().map_err(|e| store_error(&e))?;
                transaction
                    .write(reference.number(), &Value::Dict(entry.clone()), budget)
                    .map_err(|e| store_error(&e))?;
                *item = Value::Reference(reference);
            }
        }
        for position in positions {
            items.remove(position);
        }
        annots.write_items(transaction, items, budget)
    })?;
    Ok(true)
}

/// Returns the numbers of the annotations whose appearance the version that
/// reads see leaves stale. No public API exposes the marks, so tests read
/// them here (DESIGN.md section 7).
#[pyfunction]
pub fn _stale_appearances(document: &_Document) -> PyResult<Vec<u32>> {
    let version = document.reading()?;
    Ok(version
        .stale()
        .filter(|mark| mark.structure == Structure::Appearance)
        .map(|mark| mark.number)
        .collect())
}
