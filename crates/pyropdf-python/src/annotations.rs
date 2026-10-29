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

pub mod links;

use std::sync::Arc;

use pyo3::prelude::*;
use pyropdf_engine::annotations::{
    LINE_ENDINGS, MAX_BORDER_DASHES, MAX_QUADS, Reader, has_points, is_text_markup, valid_dashes,
    zigzag_points,
};
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::pages::{MAX_CHAIN, Page, PageIndex};
use pyropdf_store::settle::Structure;
use pyropdf_store::value::{Array, Dict, Name, PdfString, Reference, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::document::{_Document, text_string};
use crate::errors::{MalformedPdfError, PdfError, StaleObjectError, limit_error, store_error};
use crate::pages::{display_box, index as page_index, number, rect, rotation};

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
pub(crate) type Rect = (f64, f64, f64, f64);

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

/// A borrowed part of an object whose clone allocates owned values.
pub(crate) enum CopyPart<'a> {
    Value(&'a Value),
    Array(&'a Array),
    Dict(&'a Dict),
}

/// Charges owned buffers before a clone, including unknown nested fields.
/// The explicit work stack is budgeted; shared source spans and stream bytes
/// need no copy. Array and dictionary capacity includes room for an edit.
pub(crate) fn charge_copy(part: CopyPart<'_>, budget: &Budget) -> PyResult<()> {
    let mut pending = BudgetVec::new(budget);
    pending.try_push(part).map_err(|e| limit_error(&e))?;
    while let Some(part) = pending.pop() {
        budget.poll().map_err(|e| limit_error(&e))?;
        let bytes = match part {
            CopyPart::Value(value) => match value {
                Value::Name(name) => name.as_bytes().len(),
                Value::String(text) => text.as_bytes().len(),
                Value::Array(array) => {
                    pending
                        .try_push(CopyPart::Array(array))
                        .map_err(|e| limit_error(&e))?;
                    0
                }
                Value::Dict(dict) => {
                    pending
                        .try_push(CopyPart::Dict(dict))
                        .map_err(|e| limit_error(&e))?;
                    0
                }
                Value::Stream(stream) => {
                    pending
                        .try_push(CopyPart::Dict(stream.dict()))
                        .map_err(|e| limit_error(&e))?;
                    0
                }
                _ => 0,
            },
            CopyPart::Array(array) => {
                for value in array {
                    pending
                        .try_push(CopyPart::Value(value))
                        .map_err(|e| limit_error(&e))?;
                }
                array.len().saturating_mul(2 * size_of::<Value>())
            }
            CopyPart::Dict(dict) => {
                let mut bytes = dict.len().saturating_mul(2 * size_of::<(Name, Value)>());
                for (key, value) in dict.iter() {
                    bytes = bytes.saturating_add(key.as_bytes().len());
                    pending
                        .try_push(CopyPart::Value(value))
                        .map_err(|e| limit_error(&e))?;
                }
                bytes
            }
        };
        budget
            .charge(LimitCode::MemorySize, bytes as u64)
            .map_err(|e| limit_error(&e))?;
    }
    Ok(())
}

/// Resolves at most MAX_CHAIN references, charging the owned result before
/// copying it. Reading repeated references still charges each returned copy.
fn resolve(version: &Version, value: Option<&Value>, budget: &Budget) -> PyResult<Option<Value>> {
    let mut current = match value {
        None | Some(Value::Null) => return Ok(None),
        Some(Value::Reference(reference)) => *reference,
        Some(value) => {
            charge_copy(CopyPart::Value(value), budget)?;
            return Ok(Some(value.clone()));
        }
    };
    for _ in 0..MAX_CHAIN {
        let Some(object) = version
            .get(current.number(), budget)
            .map_err(|e| store_error(&e))?
            .filter(|object| object.generation() == current.generation())
        else {
            return Ok(None);
        };
        match &**object.value() {
            Value::Null => return Ok(None),
            Value::Reference(next) => current = *next,
            value => {
                charge_copy(CopyPart::Value(value), budget)?;
                return Ok(Some(value.clone()));
            }
        }
    }
    Err(MalformedPdfError::new_err(
        "a reference chain is longer than 16 references",
    ))
}

/// The annotations of one page in the version that reads see.
pub(crate) struct Annots<'b> {
    version: Arc<Version>,
    index: Arc<PageIndex>,
    at: usize,
    budget: &'b Budget,
    crop: [f64; 4],
    pub(crate) rotation: i64,
    /// The array object that the page's `/Annots` names, if it names one.
    array: Option<Reference>,
    pub(crate) items: Array,
}

impl<'b> Annots<'b> {
    /// Reads the annotations of page object `(number, generation)`, or
    /// returns `None` when it is not a page of the document.
    pub(crate) fn read(
        version: Arc<Version>,
        index: Arc<PageIndex>,
        budget: &'b Budget,
        number: u32,
        generation: u16,
    ) -> PyResult<Option<Self>> {
        let Some(at) = index.index_of(Reference::new(number, generation)) else {
            return Ok(None);
        };
        Self::at(version, index, budget, at).map(Some)
    }

    /// Reads the annotations of the page at `at` of `index`, a page index
    /// of `version`.
    pub(crate) fn at(
        version: Arc<Version>,
        index: Arc<PageIndex>,
        budget: &'b Budget,
        at: usize,
    ) -> PyResult<Self> {
        let page = &index.pages()[at];
        let crop = display_box(page);
        let rotation = rotation(page.rotate());
        let annots = page.dict().get("Annots");
        let array = match annots {
            Some(Value::Reference(reference)) => Some(*reference),
            _ => None,
        };
        let items = match resolve(&version, annots, budget)? {
            Some(Value::Array(items)) => items,
            _ => Array::new(),
        };
        budget
            .check(LimitCode::AnnotationCount, items.len() as u64)
            .map_err(|e| limit_error(&e))?;
        Ok(Self {
            version,
            index,
            at,
            budget,
            crop,
            rotation,
            array,
            items,
        })
    }

    pub(crate) fn page(&self) -> &Page {
        &self.index.pages()[self.at]
    }

    /// Returns the version that the annotations were read from, its page
    /// index, and the position of their page in it.
    pub(crate) fn pages(&self) -> (&Arc<Version>, &Arc<PageIndex>, usize) {
        (&self.version, &self.index, self.at)
    }

    /// Returns a reader of the version that the annotations were read in.
    pub(crate) fn reader(&self) -> Reader<'_> {
        Reader {
            version: &self.version,
            budget: self.budget,
        }
    }

    /// Returns the object and dictionary of the entry `item`, or `None`
    /// for an entry that is not an annotation dictionary.
    pub(crate) fn entry(&self, item: &Value) -> PyResult<Option<((u32, u16), Dict)>> {
        let id = match item {
            Value::Reference(reference) => (reference.number(), reference.generation()),
            Value::Dict(dict) => {
                charge_copy(CopyPart::Dict(dict), self.budget)?;
                return Ok(Some(((0, 0), dict.clone())));
            }
            _ => return Ok(None),
        };
        Ok(match resolve(&self.version, Some(item), self.budget)? {
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
    pub(crate) fn find(&self, index: u32, id: (u32, u16)) -> PyResult<(usize, Dict)> {
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
    pub(crate) fn to_display(&self, (x0, y0, x1, y1): Rect) -> Rect {
        let (a, b) = to_display(self.crop, self.rotation, x0, y0);
        let (c, d) = to_display(self.crop, self.rotation, x1, y1);
        (a.min(c), b.min(d), a.max(c), b.max(d))
    }

    /// Converts a display rectangle to a PDF `/Rect` array.
    pub(crate) fn to_pdf(&self, (x0, y0, x1, y1): Rect) -> Value {
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
            .map(|name| charge_text(&name, budget, self.budget))
            .transpose()?;
        let mut texts: [Option<String>; 8] = Default::default();
        for (slot, key) in texts.iter_mut().zip(TEXT_KEYS) {
            *slot = if key == "Name" || key == "IT" {
                self.name(dict, key)?
                    .map(|name| charge_text(&name, budget, self.budget))
                    .transpose()?
            } else {
                match self.resolved(dict.get(key))? {
                    Some(Value::String(text)) => {
                        Some(charge_decoded(text.as_bytes(), budget, self.budget)?)
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

    pub(crate) fn resolved(&self, value: Option<&Value>) -> PyResult<Option<Value>> {
        resolve(&self.version, value, self.budget)
    }

    pub(crate) fn name(&self, dict: &Dict, key: &str) -> PyResult<Option<Vec<u8>>> {
        Ok(match self.resolved(dict.get(key))? {
            Some(Value::Name(name)) => {
                self.budget
                    .check(LimitCode::AnnotationTextSize, name.as_bytes().len() as u64)
                    .map_err(|e| limit_error(&e))?;
                self.budget
                    .charge(LimitCode::MemorySize, name.as_bytes().len() as u64)
                    .map_err(|e| limit_error(&e))?;
                Some(name.as_bytes().to_vec())
            }
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
    pub(crate) fn rect(&self, value: Option<&Value>) -> PyResult<Option<Rect>> {
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
        if max == MAX_BORDER_DASHES {
            self.budget
                .check(LimitCode::AnnotationDashCount, items.len() as u64)
                .map_err(|e| limit_error(&e))?;
        }
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
    pub(crate) fn color(&self, dict: &Dict, key: &str) -> PyResult<Vec<f64>> {
        Ok(self.numbers(dict.get(key), 4)?.unwrap_or_default())
    }

    /// Reads the border that `/BS`, else `/Border`, describes.
    pub(crate) fn border(&self, dict: &Dict) -> PyResult<Option<(f64, String, Vec<f64>)>> {
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
    /// edit marks the annotation's appearance stale. A refused edit
    /// changes nothing. Returns the annotation's object.
    pub(crate) fn write(
        &self,
        transaction: &mut Transaction,
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
        let budget = self.budget;
        charge_copy(CopyPart::Array(&self.items), budget)?;
        let mut items = self.items.clone();
        let mut listed = false;
        // A handle finds a direct dictionary by its position alone.
        if matches!(self.items.get(position), Some(Value::Dict(_))) {
            for item in items.iter_mut() {
                if let Value::Dict(entry) = item {
                    charge_copy(CopyPart::Dict(entry), budget)?;
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
    }

    /// Writes the page's `/Annots` as `items`: into the array object that
    /// it names, unless another page shares that array, else into the page
    /// dictionary.
    pub(crate) fn write_items(
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
        charge_copy(CopyPart::Dict(page.dict()), budget)?;
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
pub(crate) fn to_display(crop: [f64; 4], rotation: i64, x: f64, y: f64) -> (f64, f64) {
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
pub(crate) fn to_pdf(crop: [f64; 4], rotation: i64, x: f64, y: f64) -> (f64, f64) {
    let [cx0, cy0, cx1, cy1] = crop;
    match rotation {
        90 => (cx0 + y, cy0 + x),
        180 => (cx1 - x, cy0 + y),
        270 => (cx1 - y, cy1 - x),
        _ => (cx0 + x, cy1 - y),
    }
}

/// Adds `amount` to one total of a read's text budget.
fn charge(total: &mut usize, amount: usize, budget: &Budget) -> PyResult<()> {
    *total = total.saturating_add(amount);
    budget
        .check(LimitCode::AnnotationTextSize, *total as u64)
        .map_err(|e| limit_error(&e))
}

/// Returns a name as text, charging before allocating its decoded copy.
fn charge_text(
    bytes: &[u8],
    (encoded, returned): &mut (usize, usize),
    budget: &Budget,
) -> PyResult<String> {
    charge(encoded, bytes.len(), budget)?;
    budget
        .charge(LimitCode::MemorySize, bytes.len().saturating_mul(3) as u64)
        .map_err(|e| limit_error(&e))?;
    let text = String::from_utf8_lossy(bytes).into_owned();
    charge(returned, text.len(), budget)?;
    Ok(text)
}

/// Decodes a text string within the operation's text and memory budgets.
fn charge_decoded(
    bytes: &[u8],
    (encoded, returned): &mut (usize, usize),
    budget: &Budget,
) -> PyResult<String> {
    charge(encoded, bytes.len(), budget)?;
    budget
        .charge(LimitCode::MemorySize, bytes.len().saturating_mul(3) as u64)
        .map_err(|e| limit_error(&e))?;
    let text = text_string(bytes);
    charge(returned, text.len(), budget)?;
    Ok(text)
}

/// Encodes a text string as `String.text` reads it: ASCII as is, anything
/// else as UTF-16BE.
pub(crate) fn encode_text(text: &str) -> Value {
    let bytes = if text.is_ascii() {
        text.as_bytes().to_vec()
    } else {
        let mut bytes = vec![0xFE, 0xFF];
        bytes.extend(text.encode_utf16().flat_map(u16::to_be_bytes));
        bytes
    };
    Value::String(PdfString::literal(bytes))
}

pub(crate) fn rgb(components: (f64, f64, f64), label: &str) -> PyResult<Value> {
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
    let (version, index_of_pages, budget) = page_index(document)?;
    let Some(annots) = Annots::read(version, index_of_pages, &budget, number, generation)? else {
        return Ok(None);
    };
    py.detach(|| {
        let mut records = BudgetVec::new(annots.budget);
        let mut budget = (0, 0);
        for (index, item) in annots.items.iter().enumerate() {
            annots.budget.poll().map_err(|e| limit_error(&e))?;
            let Some((id, dict)) = annots.entry(item)? else {
                continue;
            };
            if let Some(record) = annots.record(index, id, &dict, &mut budget)? {
                records.try_push(record).map_err(|e| limit_error(&e))?;
            }
        }
        Ok(Some(records.into_vec()))
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
    let (version, index_of_pages, budget) = page_index(document)?;
    let Some(annots) = Annots::read(version, index_of_pages, &budget, number, generation)? else {
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
    #[pyo3(default)]
    points: Option<Vec<Vec<(f64, f64)>>>,
    #[pyo3(default)]
    line_endings: Option<(String, String)>,
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
        annots
            .budget
            .charge(LimitCode::AnnotationInputSize, size as u64)
            .map_err(|e| limit_error(&e))?;
        annots
            .budget
            .charge(LimitCode::MemorySize, size.saturating_mul(3) as u64)
            .map_err(|e| limit_error(&e))?;
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
        if let Some(border) = &self.border {
            dict.remove("Border");
            dict.insert(Name::from("BS"), border_style(border, annots.budget)?);
        }
        if let Some(intensity) = self.clouds {
            dict.insert(Name::from("BE"), cloud_effect(subtype, intensity)?);
        }
        if let Some(lists) = &self.points {
            let (key, value) = point_entry(annots, subtype, lists)?;
            dict.insert(Name::from(key), value);
        }
        if let Some(endings) = &self.line_endings {
            if !matches!(subtype, "Line" | "PolyLine") {
                return Err(PdfError::new_err(
                    "only lines and polylines have line endings",
                ));
            }
            dict.insert(Name::from("LE"), line_endings(endings)?);
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

/// Returns the `/BS` dictionary of a border: its width, `/S` style, and
/// dash lengths, which a dashed border needs and no other takes.
pub(crate) fn border_style(
    (width, style, dashes): &(f64, String, Vec<f64>),
    budget: &Budget,
) -> PyResult<Value> {
    budget
        .check(LimitCode::AnnotationDashCount, dashes.len() as u64)
        .map_err(|e| limit_error(&e))?;
    if !(width.is_finite() && *width >= 0.0) {
        return Err(PdfError::new_err(
            "an annotation border width is finite and not negative",
        ));
    }
    if !matches!(style.as_str(), "S" | "D" | "B" | "I" | "U") {
        return Err(PdfError::new_err(format!(
            "unknown annotation border style {style:?}"
        )));
    }
    if dashes.len() > MAX_BORDER_DASHES
        || (!dashes.is_empty() && !valid_dashes(dashes))
        || (style == "D") == dashes.is_empty()
    {
        return Err(PdfError::new_err(format!(
            "a dashed border has 1 to {MAX_BORDER_DASHES} non-negative dash lengths, and only a dashed one"
        )));
    }
    let mut border = Dict::new();
    border.insert(Name::from("W"), number(*width));
    border.insert(Name::from("S"), Value::Name(Name::from(style.as_str())));
    if !dashes.is_empty() {
        let dashes = dashes.iter().map(|dash| number(*dash)).collect::<Vec<_>>();
        border.insert(Name::from("D"), Value::Array(Array::from(dashes)));
    }
    Ok(Value::Dict(border))
}

/// Returns the `/BE` dictionary of a cloudy border of `intensity` on an
/// annotation of `subtype`.
fn cloud_effect(subtype: &str, intensity: f64) -> PyResult<Value> {
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
    Ok(Value::Dict(effect))
}

/// Returns the `/LE` array of a line's start and end styles.
fn line_endings((start, end): &(String, String)) -> PyResult<Value> {
    for style in [start, end] {
        if !LINE_ENDINGS.contains(&style.as_str()) {
            return Err(PdfError::new_err(format!("unknown line ending {style:?}")));
        }
    }
    Ok(Value::Array(Array::from(vec![
        Value::Name(Name::from(start.as_str())),
        Value::Name(Name::from(end.as_str())),
    ])))
}

/// Returns the key and the user-space array of the display points of a
/// line (`/L`), polygon or polyline (`/Vertices`), or ink annotation
/// (`/InkList`) on a page of `annots`. Settling fits the rectangle to them.
fn point_entry(
    annots: &Annots,
    subtype: &str,
    lists: &[Vec<(f64, f64)>],
) -> PyResult<(&'static str, Value)> {
    annots
        .budget
        .check(LimitCode::AnnotationPointCount, lists.len() as u64)
        .map_err(|e| limit_error(&e))?;
    let mut count = 0usize;
    for points in lists {
        count = count.saturating_add(points.len());
        annots
            .budget
            .check(LimitCode::AnnotationPointCount, count as u64)
            .map_err(|e| limit_error(&e))?;
    }
    let fits = match subtype {
        "Line" => lists.len() == 1 && lists[0].len() == 2,
        "Polygon" | "PolyLine" => lists.len() == 1 && lists[0].len() >= 2,
        "Ink" => !lists.is_empty() && lists.iter().all(|points| !points.is_empty()),
        _ => {
            return Err(PdfError::new_err(
                "only lines, polygons, polylines, and ink have points",
            ));
        }
    };
    if !fits {
        return Err(PdfError::new_err(format!(
            "a {subtype} annotation has {}",
            match subtype {
                "Line" => "two points",
                "Ink" => "one or more strokes of one or more points",
                _ => "two or more vertices",
            }
        )));
    }
    let mut arrays = BudgetVec::new(annots.budget);
    for points in lists {
        let mut array = BudgetVec::new(annots.budget);
        for &(x, y) in points {
            if !(x.is_finite() && y.is_finite()) {
                return Err(PdfError::new_err("annotation points are finite"));
            }
            let (x, y) = to_pdf(annots.crop, annots.rotation, x, y);
            annots.budget.poll().map_err(|e| limit_error(&e))?;
            array
                .try_extend_from_slice(&[number(x), number(y)])
                .map_err(|e| limit_error(&e))?;
        }
        arrays
            .try_push(Value::Array(Array::from(array.into_vec())))
            .map_err(|e| limit_error(&e))?;
    }
    Ok(match subtype {
        "Line" => ("L", arrays.pop().expect("validated one list")),
        "Ink" => ("InkList", Value::Array(Array::from(arrays.into_vec()))),
        _ => ("Vertices", arrays.pop().expect("validated one list")),
    })
}

/// The display points of a line, polygon, polyline, or ink annotation,
/// `None` for another kind or a missing or malformed geometry, and the
/// `/LE` styles of a line or polyline.
type Geometry = (
    Option<Vec<Vec<(f64, f64)>>>,
    Option<(&'static str, &'static str)>,
);

/// Reads the points and line endings of the annotation that a handle
/// names, or returns `None` for a page that is gone. More than
/// `MAX_ANNOTATION_POINTS` points raise rather than read in part.
#[pyfunction]
pub fn annotation_geometry(
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
) -> PyResult<Option<Geometry>> {
    let (version, index_of_pages, budget) = page_index(document)?;
    let Some(annots) = Annots::read(version, index_of_pages, &budget, number, generation)? else {
        return Ok(None);
    };
    let (_, dict) = annots.find(index, id)?;
    let subtype = annots.name(&dict, "Subtype")?.unwrap_or_default();
    if !has_points(&subtype) {
        return Ok(Some((None, None)));
    }
    let reader = Reader {
        version: &annots.version,
        budget: annots.budget,
    };
    let mut points = reader
        .point_lists(&dict, &subtype)
        .map_err(|e| store_error(&e))?;
    if let Some(lists) = &mut points {
        for list in lists {
            for point in list {
                budget.poll().map_err(|e| limit_error(&e))?;
                *point = to_display(annots.crop, annots.rotation, point.0, point.1);
            }
        }
    }
    let endings = if matches!(&*subtype, b"Line" | b"PolyLine") {
        let [start, end] = reader.line_endings(&dict).map_err(|e| store_error(&e))?;
        Some((LINE_ENDINGS[start], LINE_ENDINGS[end]))
    } else {
        None
    };
    Ok(Some((points, endings)))
}

/// Reads the draft only after acquiring its writer lock. A refused edit
/// restores its own writes even when the caller catches it in a transaction.
pub(crate) fn editing<T>(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    write: impl FnOnce(&Annots<'_>, &mut Transaction, &Budget) -> PyResult<T>,
) -> PyResult<Option<T>> {
    document.edit(py, |transaction, budget| {
        let version = transaction.snapshot();
        let index = PageIndex::memoized(&document.draft_pages, &version, budget)
            .map_err(|error| store_error(&error))?;
        let Some(annots) = Annots::read(version, index, budget, number, generation)? else {
            return Ok(None);
        };
        let savepoint = transaction.savepoint();
        let result = write(&annots, transaction, budget);
        if result.is_err() {
            transaction.rollback(savepoint);
        }
        result.map(Some)
    })
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
    editing(
        py,
        document,
        number,
        generation,
        |annots, transaction, _budget| {
            let (position, mut dict) = annots.find(index, id)?;
            let subtype = annots.name(&dict, "Subtype")?.unwrap_or_default();
            edit.apply(annots, &mut dict, &String::from_utf8_lossy(&subtype))?;
            annots.write(transaction, position, true, dict, |_, _, _, _, _| Ok(false))
        },
    )
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
    editing(
        py,
        document,
        number,
        generation,
        |annots, transaction, _budget| {
            let (position, dict) = annots.find(index, id)?;
            if let Some((x0, y0, x1, y1)) = area
                && !([x0, y0, x1, y1].iter().all(|value| value.is_finite()) && x0 < x1 && y0 < y1)
            {
                return Err(PdfError::new_err(
                    "a popup rectangle is finite and not empty",
                ));
            }
            let existing = match dict.get("Popup") {
                Some(Value::Reference(popup)) => {
                    match annots.resolved(Some(&Value::Reference(*popup)))? {
                        Some(Value::Dict(popup_dict)) => Some((*popup, popup_dict)),
                        _ => None,
                    }
                }
                _ => None,
            };
            if area.is_some() && existing.is_none() {
                annots
                    .budget
                    .check(LimitCode::AnnotationCount, (annots.items.len() + 1) as u64)
                    .map_err(|e| limit_error(&e))?;
            }
            let area = area.map(|area| annots.to_pdf(area));
            let page = annots.page().object();
            let written = annots.write(
                transaction,
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
                        charge_copy(CopyPart::Array(items), budget)?;
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
            Ok(written)
        },
    )
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
    editing(
        py,
        document,
        number,
        generation,
        |annots, transaction, _budget| {
            let (position, dict) = annots.find(index, id)?;
            let popup = match dict.get("Popup") {
                Some(Value::Reference(popup)) => {
                    match annots.resolved(Some(&Value::Reference(*popup)))? {
                        Some(Value::Dict(popup_dict)) => Some((*popup, popup_dict)),
                        _ => None,
                    }
                }
                _ => None,
            };
            let is_note = annots.name(&dict, "Subtype")?.as_deref() == Some(b"Text");
            if popup.is_none() && !is_note {
                return Err(PdfError::new_err(
                    "the annotation has no popup to open; set popup_rect first",
                ));
            }
            let written = annots.write(
                transaction,
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
            Ok(written)
        },
    )
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
    editing(
        py,
        document,
        number,
        generation,
        |annots, transaction, _budget| {
            let (position, dict) = annots.find(index, id)?;
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
            let written =
                annots.write(transaction, position, false, dict, |_, _, dict, _, _| {
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
            Ok(written)
        },
    )
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
    editing(
        py,
        document,
        number,
        generation,
        |annots, transaction, budget| {
            budget
                .check(LimitCode::AnnotationCount, handles.len() as u64)
                .map_err(|e| limit_error(&e))?;
            let mut positions = BudgetVec::new(budget);
            for (index, id) in handles {
                let (position, dict) = annots.find(index, id)?;
                budget.poll().map_err(|e| limit_error(&e))?;
                positions.try_push(position).map_err(|e| limit_error(&e))?;
                if let Some(Value::Reference(popup)) = dict.get("Popup")
                    && let Some(position) = annots.items.iter().position(
                        |item| matches!(item, Value::Reference(listed) if listed == popup),
                    )
                {
                    positions.try_push(position).map_err(|e| limit_error(&e))?;
                }
            }
            if positions.is_empty() {
                return Ok(());
            }
            // Remove from the end, so that each removal keeps the earlier positions.
            let mut positions = positions.into_vec();
            positions.sort_unstable_by(|a, b| b.cmp(a));
            positions.dedup();
            charge_copy(CopyPart::Array(&annots.items), budget)?;
            let mut items = annots.items.clone();

            for item in items.iter_mut() {
                if let Value::Dict(entry) = item {
                    charge_copy(CopyPart::Dict(entry), budget)?;
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
        },
    )
    .map(|page| page.is_some())
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

/// The annotation that `page.annotations.add` adds, as a specification's
/// `_payload` gives it: its subtype; the display rectangles of text markup,
/// the display point lists of a line, polygon, polyline, or ink, or else
/// its display rectangle; its colors as RGB from 0 to 1, border, cloudy
/// border, opacity, blend mode, contents, and creation date; a line's
/// endings; and a note's icon and open state.
#[derive(FromPyObject)]
#[pyo3(from_item_all)]
pub struct Spec {
    subtype: String,
    #[pyo3(default)]
    rects: Option<Vec<Rect>>,
    #[pyo3(default)]
    rect: Option<Rect>,
    #[pyo3(default)]
    color: Option<(f64, f64, f64)>,
    #[pyo3(default)]
    opacity: Option<f64>,
    #[pyo3(default)]
    contents: Option<String>,
    #[pyo3(default)]
    created: Option<String>,
    #[pyo3(default)]
    icon: Option<String>,
    #[pyo3(default)]
    open: Option<bool>,
    #[pyo3(default)]
    points: Option<Vec<Vec<(f64, f64)>>>,
    #[pyo3(default)]
    line_endings: Option<(String, String)>,
    #[pyo3(default)]
    interior_color: Option<(f64, f64, f64)>,
    #[pyo3(default)]
    border: Option<(f64, String, Vec<f64>)>,
    #[pyo3(default)]
    clouds: Option<f64>,
    #[pyo3(default)]
    blend_mode: Option<String>,
}

/// The subtypes that `page.annotations.add` adds.
const ADDED_SUBTYPES: [&str; 11] = [
    "Highlight",
    "Underline",
    "StrikeOut",
    "Squiggly",
    "Text",
    "Square",
    "Circle",
    "Line",
    "Polygon",
    "PolyLine",
    "Ink",
];

impl Spec {
    /// Returns the dictionary of the annotation, without its page.
    fn dictionary(&self, annots: &Annots) -> PyResult<Dict> {
        let subtype = self.subtype.as_str();
        let created = self.created.as_deref().unwrap_or_default();
        let size = [
            subtype,
            self.contents.as_deref().unwrap_or_default(),
            created,
            created,
        ]
        .iter()
        .map(|text| text.len())
        .fold(0, usize::saturating_add);
        annots
            .budget
            .charge(LimitCode::AnnotationInputSize, size as u64)
            .map_err(|e| limit_error(&e))?;
        annots
            .budget
            .charge(LimitCode::MemorySize, size.saturating_mul(3) as u64)
            .map_err(|e| limit_error(&e))?;
        let markup = is_text_markup(subtype.as_bytes());
        if self.rects.is_some() != markup {
            return Err(PdfError::new_err(format!(
                "rectangles place text markup, not a {subtype} annotation"
            )));
        }
        if !ADDED_SUBTYPES.contains(&subtype) {
            return Err(PdfError::new_err(format!(
                "pyropdf does not add {subtype} annotations"
            )));
        }
        let mut dict = Dict::new();
        dict.insert(Name::from("Type"), Value::Name(Name::from("Annot")));
        dict.insert(Name::from("Subtype"), Value::Name(Name::from(subtype)));
        if let Some(rects) = &self.rects {
            if rects.is_empty() {
                return Err(PdfError::new_err(format!(
                    "text markup covers 1 to {MAX_QUADS} rectangles"
                )));
            }
            annots
                .budget
                .check(LimitCode::AnnotationQuadCount, rects.len() as u64)
                .map_err(|e| limit_error(&e))?;
            let mut quads = BudgetVec::new(annots.budget);
            quads
                .try_reserve_exact(rects.len())
                .map_err(|e| limit_error(&e))?;
            for &(x0, y0, x1, y1) in rects {
                if ![x0, y0, x1, y1].iter().all(|value| value.is_finite()) {
                    return Err(PdfError::new_err("text markup rectangles are finite"));
                }
                // Display corners TL, TR, BL, BR, as /QuadPoints lists them.
                quads
                    .try_push(
                        [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
                            .map(|(x, y)| to_pdf(annots.crop, annots.rotation, x, y)),
                    )
                    .map_err(|e| limit_error(&e))?;
            }
            if subtype == "Squiggly" {
                annots
                    .budget
                    .check(
                        LimitCode::AnnotationPointCount,
                        zigzag_points(&quads) as u64,
                    )
                    .map_err(|e| limit_error(&e))?;
            }
            let mut points = BudgetVec::new(annots.budget);
            points
                .try_extend(
                    quads
                        .iter()
                        .flatten()
                        .flat_map(|&(x, y)| [number(x), number(y)]),
                )
                .map_err(|e| limit_error(&e))?;
            dict.insert(
                Name::from("QuadPoints"),
                Value::Array(Array::from(points.into_vec())),
            );
            let bounds = quads.iter().flatten().fold(
                (
                    f64::INFINITY,
                    f64::INFINITY,
                    f64::NEG_INFINITY,
                    f64::NEG_INFINITY,
                ),
                |(x0, y0, x1, y1), &(x, y)| (x0.min(x), y0.min(y), x1.max(x), y1.max(y)),
            );
            let (x0, y0, x1, y1) = bounds;
            dict.insert(
                Name::from("Rect"),
                Value::Array(Array::from(vec![
                    number(x0),
                    number(y0),
                    number(x1),
                    number(y1),
                ])),
            );
        } else if has_points(subtype.as_bytes()) {
            let Some(lists) = self.points.as_ref().filter(|_| self.rect.is_none()) else {
                return Err(PdfError::new_err(format!(
                    "a {subtype} annotation takes points"
                )));
            };
            let (key, value) = point_entry(annots, subtype, lists)?;
            // The bounds of the points; settling widens them for the
            // stroke, the cloudy border, and the line endings.
            let mut bounds = (
                f64::INFINITY,
                f64::INFINITY,
                f64::NEG_INFINITY,
                f64::NEG_INFINITY,
            );
            for &(x, y) in lists.iter().flatten() {
                let (x, y) = to_pdf(annots.crop, annots.rotation, x, y);
                bounds = (
                    bounds.0.min(x),
                    bounds.1.min(y),
                    bounds.2.max(x),
                    bounds.3.max(y),
                );
            }
            dict.insert(Name::from(key), value);
            dict.insert(
                Name::from("Rect"),
                Value::Array(Array::from(vec![
                    number(bounds.0),
                    number(bounds.1),
                    number(bounds.2),
                    number(bounds.3),
                ])),
            );
            if let Some(endings) = &self.line_endings {
                if !matches!(subtype, "Line" | "PolyLine") {
                    return Err(PdfError::new_err(format!(
                        "a {subtype} annotation has no line endings"
                    )));
                }
                dict.insert(Name::from("LE"), line_endings(endings)?);
            }
        } else {
            let Some(area) = self.rect.filter(|_| self.points.is_none()) else {
                return Err(PdfError::new_err(format!(
                    "a {subtype} annotation takes a rectangle"
                )));
            };
            let (x0, y0, x1, y1) = area;
            if ![x0, y0, x1, y1].iter().all(|value| value.is_finite()) {
                return Err(PdfError::new_err("an annotation rectangle is finite"));
            }
            dict.insert(Name::from("Rect"), annots.to_pdf(area));
        }
        // Printable.
        dict.insert(Name::from("F"), Value::Integer(4));
        if let Some(color) = self.color {
            dict.insert(Name::from("C"), rgb(color, "color")?);
        }
        if let Some(color) = self.interior_color {
            dict.insert(Name::from("IC"), rgb(color, "interior color")?);
        }
        if let Some(border) = &self.border {
            dict.insert(Name::from("BS"), border_style(border, annots.budget)?);
        }
        if let Some(intensity) = self.clouds {
            dict.insert(Name::from("BE"), cloud_effect(subtype, intensity)?);
        }
        if let Some(opacity) = self.opacity {
            if !(opacity.is_finite() && opacity > 0.0 && opacity <= 1.0) {
                return Err(PdfError::new_err(
                    "an annotation opacity is greater than 0 and at most 1",
                ));
            }
            if opacity < 1.0 {
                dict.insert(Name::from("CA"), Value::from(opacity));
            }
        }
        if let Some(mode) = &self.blend_mode {
            if !BLEND_MODES.contains(&mode.as_str()) {
                return Err(PdfError::new_err(format!("unknown blend mode {mode:?}")));
            }
            dict.insert(Name::from("BM"), Value::Name(Name::from(mode.as_str())));
        }
        if let Some(contents) = &self.contents {
            dict.insert(Name::from("Contents"), encode_text(contents));
        }
        if self.open.is_some() && subtype != "Text" || self.icon.is_some() && subtype != "Text" {
            return Err(PdfError::new_err(format!(
                "an icon and an open state belong to a note, not a {subtype} annotation"
            )));
        }
        if subtype == "Text" {
            let icon = self.icon.as_deref().unwrap_or("Note");
            if !NOTE_ICONS.contains(&icon) {
                return Err(PdfError::new_err(format!("unknown note icon {icon:?}")));
            }
            dict.insert(Name::from("Name"), Value::Name(Name::from(icon)));
            dict.insert(
                Name::from("Open"),
                Value::Boolean(self.open.unwrap_or(false)),
            );
            // A note's icon neither zooms nor turns with the page (ISO
            // 32000-2 section 12.5.6.4); viewers that honor the flags keep
            // it upright.
            dict.insert(Name::from("F"), Value::Integer(4 | 8 | 16));
        }
        if let Some(created) = &self.created {
            if !created.starts_with("D:") || !created.is_ascii() || created.len() > 64 {
                return Err(PdfError::new_err("an annotation date is a PDF date string"));
            }
            let date = Value::String(PdfString::literal(created.as_bytes().to_vec()));
            dict.insert(Name::from("CreationDate"), date.clone());
            dict.insert(Name::from("M"), date);
        }
        Ok(dict)
    }
}

/// Adds the annotation that `spec` describes to page object `(number,
/// generation)`, marks its appearance stale so that settling draws it, and
/// returns its object; or `None` for a page that is gone.
#[pyfunction]
pub fn annotation_add(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    spec: Spec,
) -> PyResult<Option<(u32, u16)>> {
    editing(
        py,
        document,
        number,
        generation,
        |annots, transaction, budget| {
            budget
                .check(LimitCode::AnnotationCount, (annots.items.len() + 1) as u64)
                .map_err(|e| limit_error(&e))?;
            if !matches!(
                annots.resolved(annots.page().dict().get("Annots"))?,
                None | Some(Value::Array(_))
            ) {
                return Err(PdfError::new_err("the page's /Annots is not an array"));
            }
            let mut dict = spec.dictionary(annots)?;
            if let Some(page) = annots.page().object() {
                dict.insert(Name::from("P"), Value::Reference(page));
            }

            let reference = transaction.allocate().map_err(|e| store_error(&e))?;
            transaction
                .write(reference.number(), &Value::Dict(dict), budget)
                .map_err(|e| store_error(&e))?;
            charge_copy(CopyPart::Array(&annots.items), budget)?;
            let mut items = annots.items.clone();
            items.push(Value::Reference(reference));
            annots.write_items(transaction, items, budget)?;
            transaction
                .mark(Structure::Appearance, reference.number(), budget)
                .map_err(|e| store_error(&e))?;
            Ok((reference.number(), reference.generation()))
        },
    )
}
