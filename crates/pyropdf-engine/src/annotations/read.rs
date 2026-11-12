//! Reading page `/Annots` arrays, annotation records, embedded-face fonts,
//! stale appearance marks, and isolated single-annotation scratch pages.

use std::sync::Arc;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::{MAX_CHAIN, Page, PageIndex};
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::settle::{Scope, Structure};
use pyropdf_store::value::{Array, Dict, Name, Real, Reference, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::display::{display_box, number, rect as parse_rect, rotation, to_display, to_pdf};
use crate::forms::flatten::{self as flattening, Missing, Selection};
use crate::forms::text_string;

use super::geometry::{Rect, TEXT_KEYS};
use super::{
    AnnotationError, AnnotationResult as Result, BLEND_MODES, FACE_RESOURCE, MAX_BORDER_DASHES,
    Reader, synthesized_for, valid_dashes,
};

/// The 12-element tuple representation of an [`AnnotationRecord`] returned
/// across the Python boundary.
pub type RecordTuple = (
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

/// One non-link, non-widget, non-popup annotation listed in a page's `/Annots`.
#[derive(Clone, Debug, PartialEq)]
pub struct AnnotationRecord {
    /// Zero-based position in the page's `/Annots`.
    pub index: u32,
    /// Indirect object `(number, generation)`, or `(0, 0)` for a direct
    /// dictionary.
    pub id: (u32, u16),
    /// The `/Subtype` name, if present.
    pub subtype: Option<String>,
    /// Normalized display-space rectangle `(x0, y0, x1, y1)` in points.
    pub rect: Rect,
    /// `/F` annotation flags (0 when absent or non-integer).
    pub flags: i64,
    /// Decoded values of [`TEXT_KEYS`] (`Contents`, `Name`, `T`,
    /// `CreationDate`, `M`, `Subj`, `NM`, `IT`).
    pub texts: [Option<String>; 8],
    /// `/C` color components (up to 4 numbers, empty otherwise).
    pub color: Vec<f64>,
    /// `/IC` interior color components (up to 4 numbers, empty otherwise).
    pub interior_color: Vec<f64>,
    /// `/CA` opacity when finite.
    pub opacity: Option<f64>,
    /// Border `(width_pts, style, dashes_pts)` from `/BS` or `/Border`.
    pub border: Option<(f64, String, Vec<f64>)>,
    /// `/BM` blend mode when it names one of [`BLEND_MODES`].
    pub blend_mode: Option<&'static str>,
    /// `(reply_to_id, popup_display_rect, is_open)`.
    pub popup: (Option<(u32, u16)>, Option<Rect>, bool),
}

impl AnnotationRecord {
    /// Converts the record into its FFI tuple layout.
    #[must_use]
    pub fn into_tuple(self) -> RecordTuple {
        (
            self.index,
            self.id,
            self.subtype,
            self.rect,
            self.flags,
            self.texts,
            self.color,
            self.interior_color,
            self.opacity,
            self.border,
            self.blend_mode,
            self.popup,
        )
    }
}

/// A scratch version in which the target page draws nothing except a single
/// flattened annotation appearance.
pub struct IsolatedAnnotation {
    /// The settled scratch version with only the isolated annotation drawn.
    pub version: Arc<Version>,
    /// The page index of [`Self::version`].
    pub pages: Arc<PageIndex>,
    /// Zero-based page index of the target page in [`Self::pages`].
    pub page_index: usize,
    /// Display-space rectangle of the isolated annotation.
    pub rect: Rect,
}

/// Resolves at most [`MAX_CHAIN`] references, copying the resolved value
/// through [`copy_for_edit`] under `budget`.
pub(crate) fn resolve(
    version: &Version,
    value: Option<&Value>,
    budget: &Budget,
) -> Result<Option<Value>> {
    let mut current = match value {
        None | Some(Value::Null) => return Ok(None),
        Some(Value::Reference(reference)) => *reference,
        Some(value) => return Ok(Some(copy_for_edit(value, budget)?)),
    };
    for _ in 0..MAX_CHAIN {
        let Some(object) = version
            .get(current.number(), budget)?
            .filter(|object| object.generation() == current.generation())
        else {
            return Ok(None);
        };
        match &**object.value() {
            Value::Null => return Ok(None),
            Value::Reference(next) => current = *next,
            value => return Ok(Some(copy_for_edit(value, budget)?)),
        }
    }
    Err(AnnotationError::Malformed(
        "a reference chain is longer than 16 references".to_owned(),
    ))
}

/// The annotations of one page in an immutable version.
pub struct PageAnnots<'b> {
    version: Arc<Version>,
    index: Arc<PageIndex>,
    at: usize,
    /// The operation budget governing reads and edits on this page.
    pub budget: &'b Budget,
    /// Unrotated visible box of the page in PDF user space.
    pub crop: [f64; 4],
    /// Normalized `/Rotate` of the page (`0`, `90`, `180`, or `270`).
    pub rotation: i64,
    /// Size of the page's user-space unit in physical points.
    pub unit: f64,
    /// Indirect `/Annots` array object, if `/Annots` is an indirect reference.
    array: Option<Reference>,
    /// Resolved `/Annots` items (empty when `/Annots` is absent or not an
    /// array).
    pub items: Array,
}

impl<'b> PageAnnots<'b> {
    /// Reads the annotations of page object `(number, generation)`, or returns
    /// `None` when it is not a page of `index`.
    pub fn read(
        version: Arc<Version>,
        index: Arc<PageIndex>,
        budget: &'b Budget,
        number: u32,
        generation: u16,
    ) -> Result<Option<Self>> {
        let Some(at) = index.index_of(Reference::new(number, generation)) else {
            return Ok(None);
        };
        Self::at(version, index, budget, at).map(Some)
    }

    /// Reads the annotations of the page at `at` in `index`.
    pub fn at(
        version: Arc<Version>,
        index: Arc<PageIndex>,
        budget: &'b Budget,
        at: usize,
    ) -> Result<Self> {
        let page = &index.pages()[at];
        let crop = display_box(page);
        let rotation = rotation(page.rotate());
        let unit = page.user_unit();
        let annots = page.dict().get("Annots");
        let array = match annots {
            Some(Value::Reference(reference)) => Some(*reference),
            _ => None,
        };
        let items = match resolve(&version, annots, budget)? {
            Some(Value::Array(items)) => items,
            _ => Array::new(),
        };
        budget.check(LimitCode::AnnotationCount, items.len() as u64)?;
        Ok(Self {
            version,
            index,
            at,
            budget,
            crop,
            rotation,
            unit,
            array,
            items,
        })
    }

    /// Returns the store [`Page`] entry for this page.
    #[must_use]
    pub fn page(&self) -> &Page {
        &self.index.pages()[self.at]
    }

    /// Returns the version, page index, and zero-based page position.
    #[must_use]
    pub fn pages(&self) -> (&Arc<Version>, &Arc<PageIndex>, usize) {
        (&self.version, &self.index, self.at)
    }

    /// Returns an appearance [`Reader`] scaled to this page's `/UserUnit`.
    #[must_use]
    pub fn reader(&self) -> Reader<'_> {
        Reader::on_page(&self.version, self.budget, self.unit)
    }

    /// Resolves `/Annots` item `item` to its `(id, dict)`, where a direct
    /// dictionary has `id == (0, 0)`. Returns `None` for non-dictionary items.
    pub fn entry(&self, item: &Value) -> Result<Option<((u32, u16), Dict)>> {
        let id = match item {
            Value::Reference(reference) => (reference.number(), reference.generation()),
            Value::Dict(_) => {
                let Value::Dict(dict) = copy_for_edit(item, self.budget)? else {
                    unreachable!("checked dictionary variant");
                };
                return Ok(Some(((0, 0), dict)));
            }
            _ => return Ok(None),
        };
        Ok(match resolve(&self.version, Some(item), self.budget)? {
            Some(Value::Dict(dict)) => Some((id, dict)),
            _ => None,
        })
    }

    /// Finds the current `/Annots` position of handle `(index, id)`.
    pub fn position(&self, index: u32, id: (u32, u16)) -> Result<usize> {
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
            AnnotationError::Stale(format!(
                "annotation {} is no longer in the page's /Annots",
                id.0
            ))
        })
    }

    /// Returns the `/Annots` position and copied dictionary of handle
    /// `(index, id)`.
    pub fn find(&self, index: u32, id: (u32, u16)) -> Result<(usize, Dict)> {
        let position = self.position(index, id)?;
        match self.entry(&self.items.as_slice()[position])? {
            Some((_, dict)) => Ok((position, dict)),
            None => Err(AnnotationError::Stale(format!(
                "annotation {} is no longer a dictionary",
                id.0
            ))),
        }
    }

    /// Converts a PDF user-space rectangle to normalized display space in
    /// points.
    #[must_use]
    pub fn to_display(&self, (x0, y0, x1, y1): Rect) -> Rect {
        let (a, b) = to_display(self.crop, self.rotation, self.unit, x0, y0);
        let (c, d) = to_display(self.crop, self.rotation, self.unit, x1, y1);
        (a.min(c), b.min(d), a.max(c), b.max(d))
    }

    /// Converts a display-space rectangle in points to a normalized PDF `/Rect`
    /// array.
    #[must_use]
    pub fn to_pdf(&self, (x0, y0, x1, y1): Rect) -> Value {
        let (a, b) = to_pdf(self.crop, self.rotation, self.unit, x0, y0);
        let (c, d) = to_pdf(self.crop, self.rotation, self.unit, x1, y1);
        Value::Array(Array::from(vec![
            number(a.min(c)),
            number(b.min(d)),
            number(a.max(c)),
            number(b.max(d)),
        ]))
    }

    /// Resolves `value` in this page's version, copying the owned result
    /// through [`copy_for_edit`].
    pub fn resolved(&self, value: Option<&Value>) -> Result<Option<Value>> {
        resolve(&self.version, value, self.budget)
    }

    /// Reads a `/Name` entry from `dict`, checking `AnnotationTextSize` and
    /// charging `MemorySize` via [`BudgetVec`].
    pub fn name(&self, dict: &Dict, key: &str) -> Result<Option<Vec<u8>>> {
        Ok(match self.resolved(dict.get(key))? {
            Some(Value::Name(name)) => {
                self.budget
                    .check(LimitCode::AnnotationTextSize, name.as_bytes().len() as u64)?;
                let mut bytes = BudgetVec::new(self.budget);
                bytes.try_extend_from_slice(name.as_bytes())?;
                Some(bytes.into_vec())
            }
            _ => None,
        })
    }

    pub(crate) fn number(&self, value: Option<&Value>) -> Result<Option<f64>> {
        Ok(match self.resolved(value)? {
            #[expect(
                clippy::cast_precision_loss,
                reason = "PDF numbers are reals, which an integer approximates"
            )]
            Some(Value::Integer(value)) => Some(value as f64),
            Some(Value::Real(value)) => Some(value.value()),
            _ => None,
        })
    }

    /// Reads a normalized 4-number rectangle with resolved elements.
    pub fn rect(&self, value: Option<&Value>) -> Result<Option<Rect>> {
        let Some(Value::Array(items)) = self.resolved(value)? else {
            return Ok(None);
        };
        if items.len() != 4 {
            return Ok(None);
        }
        let mut resolved = [Value::Null, Value::Null, Value::Null, Value::Null];
        for (slot, item) in resolved.iter_mut().zip(items.iter()) {
            match self.number(Some(item))? {
                Some(n) => *slot = Value::Real(Real::new(n)),
                None => return Ok(None),
            }
        }
        Ok(parse_rect(&Value::Array(Array::from(Vec::from(resolved)))))
    }

    /// Reads up to `max` resolved numbers from an array, or `None` otherwise.
    fn numbers(&self, value: Option<&Value>, max: usize) -> Result<Option<Vec<f64>>> {
        let Some(Value::Array(items)) = self.resolved(value)? else {
            return Ok(None);
        };
        if max == MAX_BORDER_DASHES {
            self.budget
                .check(LimitCode::AnnotationDashCount, items.len() as u64)?;
        }
        if items.len() > max {
            return Ok(None);
        }
        let mut numbers = BudgetVec::new(self.budget);
        numbers.try_reserve_exact(items.len())?;
        for item in items.iter() {
            match self.number(Some(item))? {
                Some(n) => numbers.try_push(n)?,
                None => return Ok(None),
            }
        }
        Ok(Some(numbers.into_vec()))
    }

    /// Reads a `/C` or `/IC` color array of up to four numbers, or an empty
    /// vector otherwise.
    pub fn color(&self, dict: &Dict, key: &str) -> Result<Vec<f64>> {
        Ok(self.numbers(dict.get(key), 4)?.unwrap_or_default())
    }

    /// Reads the border described by `/BS`, falling back to legacy `/Border`,
    /// scaled to display points via `self.unit`.
    pub fn border(&self, dict: &Dict) -> Result<Option<(f64, String, Vec<f64>)>> {
        if let Some(Value::Dict(style)) = self.resolved(dict.get("BS"))? {
            let width = self
                .number(style.get("W"))?
                .filter(|width| width.is_finite() && *width >= 0.0)
                .unwrap_or(1.0)
                * self.unit;
            if !width.is_finite() {
                return Ok(None);
            }
            let style_name = self.name(&style, "S")?;
            let style_bytes: &[u8] = match style_name.as_deref() {
                Some(s @ (b"S" | b"D" | b"B" | b"I" | b"U")) => s,
                _ => b"S",
            };
            let dashes = if style_bytes == b"D" {
                let mut dashes = self
                    .numbers(style.get("D"), MAX_BORDER_DASHES)?
                    .filter(|dashes| valid_dashes(dashes))
                    .unwrap_or_else(|| vec![3.0]);
                for dash in &mut dashes {
                    *dash *= self.unit;
                }
                if !valid_dashes(&dashes) {
                    return Ok(None);
                }
                dashes
            } else {
                Vec::new()
            };
            return Ok(Some((
                width,
                String::from_utf8_lossy(style_bytes).into_owned(),
                dashes,
            )));
        }
        let Some(Value::Array(items)) = self.resolved(dict.get("Border"))? else {
            return Ok(None);
        };
        if !(3..=4).contains(&items.len()) {
            return Ok(None);
        }
        let Some(width) = self
            .number(items.get(2))?
            .filter(|width| width.is_finite() && *width >= 0.0)
        else {
            return Ok(None);
        };
        let width = width * self.unit;
        if !width.is_finite() {
            return Ok(None);
        }
        let dashes = match items.get(3) {
            Some(pattern) => match self.numbers(Some(pattern), MAX_BORDER_DASHES)? {
                Some(mut dashes) if valid_dashes(&dashes) => {
                    for dash in &mut dashes {
                        *dash *= self.unit;
                    }
                    if !valid_dashes(&dashes) {
                        return Ok(None);
                    }
                    dashes
                }
                _ => return Ok(None),
            },
            None => Vec::new(),
        };
        let style = if dashes.is_empty() { "S" } else { "D" };
        Ok(Some((width, style.to_owned(), dashes)))
    }

    /// Reads the [`AnnotationRecord`] of `dict` at `index`, returning `None`
    /// for `/Link`, `/Widget`, `/Popup`, or an annotation without a usable
    /// `/Rect`. `totals` accumulates `(encoded_bytes, returned_bytes)` across
    /// the operation.
    pub fn record(
        &self,
        index: usize,
        id: (u32, u16),
        dict: &Dict,
        totals: &mut (usize, usize),
    ) -> Result<Option<AnnotationRecord>> {
        let subtype = self.name(dict, "Subtype")?;
        if matches!(subtype.as_deref(), Some(b"Link" | b"Widget" | b"Popup")) {
            return Ok(None);
        }
        let Some(area) = self.rect(dict.get("Rect"))? else {
            return Ok(None);
        };
        let subtype = subtype
            .map(|name| charge_text(&name, totals, self.budget))
            .transpose()?;
        let mut texts: [Option<String>; 8] = Default::default();
        for (slot, key) in texts.iter_mut().zip(TEXT_KEYS) {
            *slot = if key == "Name" || key == "IT" {
                self.name(dict, key)?
                    .map(|name| charge_text(&name, totals, self.budget))
                    .transpose()?
            } else {
                match self.resolved(dict.get(key))? {
                    Some(Value::String(text)) => {
                        Some(charge_decoded(text.as_bytes(), totals, self.budget)?)
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
        let blend_mode = self.name(dict, "BM")?.and_then(|name| {
            BLEND_MODES
                .iter()
                .find(|mode| mode.as_bytes() == name.as_slice())
                .copied()
        });
        Ok(Some(AnnotationRecord {
            index: u32::try_from(index).unwrap_or(u32::MAX),
            id,
            subtype,
            rect: self.to_display(area),
            flags: match self.resolved(dict.get("F"))? {
                Some(Value::Integer(flags)) => flags,
                _ => 0,
            },
            texts,
            color: self.color(dict, "C")?,
            interior_color: self.color(dict, "IC")?,
            opacity: self
                .number(dict.get("CA"))?
                .filter(|opacity| opacity.is_finite()),
            border: self.border(dict)?,
            blend_mode,
            popup: (reply, popup_rect.map(|area| self.to_display(area)), open),
        }))
    }

    /// Returns whether another page in `self.index` shares the same indirect
    /// `/Annots` array object.
    #[must_use]
    pub fn shared(&self) -> bool {
        let Some(array) = self.array else {
            return false;
        };
        self.index.pages().iter().enumerate().any(|(at, page)| {
            at != self.at
                && matches!(page.dict().get("Annots"), Some(Value::Reference(other)) if *other == array)
        })
    }

    /// Edits the annotation at `position`, promoting all direct dictionaries in
    /// `/Annots` to indirect objects when the target is direct, applying
    /// `change`, writing the updated annotation (and `/Annots` if changed), and
    /// optionally marking [`Structure::Appearance`] stale.
    pub fn write_with<E>(
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
        ) -> std::result::Result<bool, E>,
    ) -> std::result::Result<(u32, u16), E>
    where
        E: From<AnnotationError>,
    {
        let budget = self.budget;
        let promote_direct = matches!(self.items.get(position), Some(Value::Dict(_)));
        let mut listed = false;
        let mut copied = BudgetVec::new(budget);
        copied
            .try_reserve_exact(self.items.len().saturating_add(1))
            .map_err(|e| E::from(AnnotationError::from(e)))?;
        for item in self.items.iter() {
            budget
                .poll()
                .map_err(|e| E::from(AnnotationError::from(e)))?;
            let next = if promote_direct && matches!(item, Value::Dict(_)) {
                let entry =
                    copy_for_edit(item, budget).map_err(|e| E::from(AnnotationError::from(e)))?;
                let reference = transaction
                    .allocate()
                    .map_err(|e| E::from(AnnotationError::from(e)))?;
                transaction
                    .write(reference.number(), &entry, budget)
                    .map_err(|e| E::from(AnnotationError::from(e)))?;
                listed = true;
                Value::Reference(reference)
            } else {
                copy_for_edit(item, budget).map_err(|e| E::from(AnnotationError::from(e)))?
            };
            copied
                .try_push(next)
                .map_err(|e| E::from(AnnotationError::from(e)))?;
        }
        let mut items = Array::from(copied.into_vec());
        let Some(&Value::Reference(reference)) = items.get(position) else {
            return Err(E::from(AnnotationError::Pdf(
                "the annotation changed while it was edited".to_owned(),
            )));
        };
        listed |= change(transaction, reference, &mut dict, &mut items, budget)?;
        transaction
            .write(reference.number(), &Value::Dict(dict), budget)
            .map_err(|e| E::from(AnnotationError::from(e)))?;
        if listed {
            self.write_items(transaction, items, budget)
                .map_err(E::from)?;
        }
        if look {
            transaction
                .mark(Structure::Appearance, reference.number(), budget)
                .map_err(|e| E::from(AnnotationError::from(e)))?;
        }
        Ok((reference.number(), reference.generation()))
    }

    /// Convenience wrapper around [`Self::write_with`] for engine callers.
    pub fn write(
        &self,
        transaction: &mut Transaction,
        position: usize,
        look: bool,
        dict: Dict,
        change: impl FnOnce(&mut Transaction, Reference, &mut Dict, &mut Array, &Budget) -> Result<bool>,
    ) -> Result<(u32, u16)> {
        self.write_with(transaction, position, look, dict, change)
    }

    /// Writes `items` as the page's `/Annots`: in place when `/Annots` is an
    /// unshared indirect array, or on the page dictionary otherwise.
    pub fn write_items(
        &self,
        transaction: &mut Transaction,
        items: Array,
        budget: &Budget,
    ) -> Result<()> {
        if let Some(array) = self.array
            && !self.shared()
        {
            transaction.write(array.number(), &Value::Array(items), budget)?;
            return Ok(());
        }
        let page = self.page();
        let Some(object) = page.object() else {
            return Err(AnnotationError::Pdf(
                "the page is not an indirect object".to_owned(),
            ));
        };
        let mut dict = copy_page_dict(transaction.version(), object, page.dict(), budget)?;
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /Annots key"
        )]
        dict.insert(Name::from("Annots"), Value::Array(items));
        transaction.write(object.number(), &Value::Dict(dict), budget)?;
        Ok(())
    }
}

pub(super) fn copy_page_dict(
    version: &Version,
    object: Reference,
    fallback: &Dict,
    budget: &Budget,
) -> Result<Dict> {
    if let Some(stored) = version
        .get(object.number(), budget)?
        .filter(|stored| stored.generation() == object.generation())
        && let Value::Dict(_) = &**stored.value()
    {
        let Value::Dict(dict) = copy_for_edit(stored.value(), budget)? else {
            unreachable!("checked dictionary variant");
        };
        return Ok(dict);
    }
    let mut entries = BudgetVec::new(budget);
    entries.try_reserve_exact(fallback.len() + 1)?;
    for (key, value) in fallback.iter() {
        budget.charge(LimitCode::MemorySize, key.as_bytes().len() as u64)?;
        entries.try_push((Name::from(key.as_bytes()), copy_for_edit(value, budget)?))?;
    }
    let mut dict = Dict::new();
    for (key, value) in entries.into_vec() {
        #[expect(
            clippy::disallowed_methods,
            reason = "each entry was copied and charged via BudgetVec above"
        )]
        dict.insert(key, value);
    }
    Ok(dict)
}

fn charge(total: &mut usize, amount: usize, budget: &Budget) -> Result<()> {
    *total = total.saturating_add(amount);
    budget.check(LimitCode::AnnotationTextSize, *total as u64)?;
    Ok(())
}

fn charge_text(
    bytes: &[u8],
    (encoded, returned): &mut (usize, usize),
    budget: &Budget,
) -> Result<String> {
    charge(encoded, bytes.len(), budget)?;
    budget.charge(LimitCode::MemorySize, bytes.len().saturating_mul(3) as u64)?;
    let text = String::from_utf8_lossy(bytes).into_owned();
    charge(returned, text.len(), budget)?;
    Ok(text)
}

fn charge_decoded(
    bytes: &[u8],
    (encoded, returned): &mut (usize, usize),
    budget: &Budget,
) -> Result<String> {
    charge(encoded, bytes.len(), budget)?;
    budget.charge(LimitCode::MemorySize, bytes.len().saturating_mul(3) as u64)?;
    let text = text_string(bytes);
    charge(returned, text.len(), budget)?;
    Ok(text)
}

/// Lists the annotations of page object `(number, generation)`, or returns
/// `None` when the page is gone.
pub fn annotation_list(
    version: Arc<Version>,
    pages: Arc<PageIndex>,
    budget: &Budget,
    number: u32,
    generation: u16,
) -> Result<Option<Vec<AnnotationRecord>>> {
    let Some(annots) = PageAnnots::read(version, pages, budget, number, generation)? else {
        return Ok(None);
    };
    let mut records = BudgetVec::new(annots.budget);
    let mut totals = (0, 0);
    for (index, item) in annots.items.iter().enumerate() {
        annots.budget.poll()?;
        let Some((id, dict)) = annots.entry(item)? else {
            continue;
        };
        if let Some(record) = annots.record(index, id, &dict, &mut totals)? {
            records.try_push(record)?;
        }
    }
    Ok(Some(records.into_vec()))
}

/// Reads the annotation that handle `(index, id)` names on page `(number,
/// generation)`, returning `None` when the page is gone or
/// [`AnnotationError::Stale`] when the annotation is gone or no longer listed.
pub fn annotation_read(
    version: Arc<Version>,
    pages: Arc<PageIndex>,
    budget: &Budget,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
) -> Result<Option<AnnotationRecord>> {
    let Some(annots) = PageAnnots::read(version, pages, budget, number, generation)? else {
        return Ok(None);
    };
    let (position, dict) = annots.find(index, id)?;
    annots
        .record(position, id, &dict, &mut (0, 0))?
        .map(Some)
        .ok_or_else(|| {
            AnnotationError::Stale(format!(
                "annotation {} is no longer one that page.annotations lists",
                id.0
            ))
        })
}

/// Returns the object number of the Type0 font named [`FACE_RESOURCE`] in a
/// free-text annotation's normal appearance resources, or `None` when none is
/// used or the page is gone.
pub fn annotation_face(
    version: Arc<Version>,
    pages: Arc<PageIndex>,
    budget: &Budget,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
) -> Result<Option<u32>> {
    let Some(annots) = PageAnnots::read(version, pages, budget, number, generation)? else {
        return Ok(None);
    };
    let (_, dict) = annots.find(index, id)?;
    if annots.name(&dict, "Subtype")?.as_deref() != Some(b"FreeText".as_slice()) {
        return Ok(None);
    }
    let Some(Value::Dict(appearances)) = annots.resolved(dict.get("AP"))? else {
        return Ok(None);
    };
    let Some(Value::Stream(normal)) = annots.resolved(appearances.get("N"))? else {
        return Ok(None);
    };
    let Some(Value::Dict(resources)) = annots.resolved(normal.dict().get("Resources"))? else {
        return Ok(None);
    };
    let Some(Value::Dict(fonts)) = annots.resolved(resources.get("Font"))? else {
        return Ok(None);
    };
    Ok(match fonts.get(FACE_RESOURCE) {
        Some(Value::Reference(font)) => Some(font.number()),
        _ => None,
    })
}

/// Returns the object numbers marked stale for [`Structure::Appearance`] in
/// `version`.
pub fn stale_appearances(version: &Version, budget: &Budget) -> Result<Vec<u32>> {
    let mut numbers = BudgetVec::new(budget);
    for mark in version.stale() {
        budget.poll()?;
        if mark.structure == Structure::Appearance {
            numbers.try_push(mark.number)?;
        }
    }
    Ok(numbers.into_vec())
}

/// Settles `transaction` and rewrites the target page in place (intended for a
/// scratch transaction whose writes are discarded) so that it draws nothing
/// except the flattened or synthesized appearance of handle `(index, id)`.
/// Returns `None` when the page is gone.
pub fn isolate_annotation(
    transaction: &mut Transaction,
    budget: &Budget,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
) -> Result<Option<IsolatedAnnotation>> {
    transaction.settle(Scope::Commit, budget)?;
    let version = transaction.snapshot();
    let pages = Arc::new(PageIndex::build(&mut Reads::new(&version, budget))?);
    let Some(annots) = PageAnnots::read(version, pages, budget, number, generation)? else {
        return Ok(None);
    };
    let (position, dict) = annots.find(index, id)?;
    let Some(record) = annots.record(position, id, &dict, &mut (0, 0))? else {
        return Err(AnnotationError::Stale(format!(
            "annotation {} is no longer one that page.annotations lists",
            id.0
        )));
    };
    let (_, _, at) = annots.pages();
    let page = annots.page();
    let Some(object) = page.object() else {
        return Err(AnnotationError::Pdf(
            "the page is not an indirect object".to_owned(),
        ));
    };
    let mut blank = copy_page_dict(transaction.version(), object, page.dict(), budget)?;
    blank.remove("Contents");
    blank.remove("Annots");
    transaction.write(object.number(), &Value::Dict(blank), budget)?;
    let item = copy_for_edit(&annots.items.as_slice()[position], budget)?;
    let version = transaction.snapshot();
    let pages = Arc::new(PageIndex::build(&mut Reads::new(&version, budget))?);
    let items = Array::from(vec![item]);
    let reader = Reader::on_page(&version, budget, pages.pages()[at].user_unit());
    let selection = Selection {
        annotations: true,
        widgets: false,
    };
    if let Some(plan) = flattening::plan(&reader, &items, selection)? {
        let objects = match items.as_slice() {
            [Value::Reference(reference)] => synthesized_for(&version, *reference, budget)?,
            _ => Vec::new(),
        };
        let mut missing = Missing::new(objects, budget)?;
        let page = &pages.pages()[at];
        flattening::draw(
            transaction,
            object.number(),
            page,
            plan,
            &mut missing,
            budget,
        )?;
    }
    Ok(Some(IsolatedAnnotation {
        version: transaction.snapshot(),
        pages,
        page_index: at,
        rect: record.rect,
    }))
}
