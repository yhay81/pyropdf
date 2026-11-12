//! Read terminal fields and locate the widgets that display them.

use std::collections::HashSet;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::value::{Dict, Name, Reference, Value};
use pyropdf_store::version::Version;

use super::traversal::{Form, inherited};
use super::values::{
    Id, MAX_STATE_BYTES, MAX_VALUE_BYTES, MAX_VALUE_ITEMS, charge, id, insert_set, limit,
    object_location, read, stale, text,
};
use crate::annotations::{AnnotationError as Error, AnnotationResult as Result, PageAnnots, Rect};

/// Returns the full name and object of every terminal field, by name and
/// then object.
pub fn form_fields(form: &Form, budget: &Budget) -> Result<Vec<(String, Id)>> {
    let mut out = BudgetVec::new(budget);
    for field in &form.fields {
        budget.charge(LimitCode::MemorySize, field.name.len() as u64)?;
        out.try_push((field.name.clone(), field.id))?;
    }
    Ok(out.into_vec())
}

/// Returns the object of the first terminal field named `name`, or `None`.
#[must_use]
pub fn form_field(form: &Form, name: &str) -> Option<Id> {
    let at = form
        .fields
        .partition_point(|field| field.name.as_str() < name);
    form.fields
        .get(at)
        .filter(|field| field.name == name)
        .map(|field| field.id)
}

/// Reads field `id` as a handle does: its full name, kind, inherited `/Ff`,
/// and value. Reports a stale handle once the object is no terminal field.
pub fn form_field_read(
    form: &Form,
    id: Id,
    budget: &Budget,
) -> Result<(String, &'static str, i64, Option<String>)> {
    let field = form.field(id).ok_or_else(|| stale(id))?;
    budget.charge(
        LimitCode::MemorySize,
        field
            .name
            .len()
            .saturating_add(field.value.as_ref().map_or(0, |value| value.len())) as u64,
    )?;
    Ok((
        field.name.clone(),
        field.kind,
        field.flags,
        field.value.as_deref().map(str::to_owned),
    ))
}

/// Returns the options of choice field `id` in `/Opt` order: each export
/// value and label. An option is a text string, both its export value and
/// its label, or an array of the two; other entries are skipped.
pub fn form_field_choices(
    form: &Form,
    version: &Version,
    id: Id,
    budget: &Budget,
) -> Result<Vec<(String, String)>> {
    form.field(id).ok_or_else(|| stale(id))?;
    let mut reads = Reads::new(version, budget);
    let Some(options) = inherited(&mut reads, id, "Opt")? else {
        return Ok(Vec::new());
    };
    let Some(Value::Array(options)) = read(&mut reads, Some(&options))? else {
        return Ok(Vec::new());
    };
    if options.len() > MAX_VALUE_ITEMS {
        return Err(limit(format!(
            "AcroForm choice options exceed the {MAX_VALUE_ITEMS}-item safety limit"
        )));
    }
    let mut totals = (0, 0);
    let mut choices = BudgetVec::new(budget);
    for option in options.iter() {
        let mut text = |value: &Value| {
            text(
                &mut reads,
                value,
                &mut totals,
                MAX_VALUE_BYTES,
                "choice-option",
            )
        };
        let choice = match option {
            Value::Array(pair) if pair.len() == 2 => {
                match (text(&pair.as_slice()[0])?, text(&pair.as_slice()[1])?) {
                    (Some(value), Some(label)) => (value, label),
                    _ => continue,
                }
            }
            option => match text(option)? {
                Some(value) => {
                    budget.charge(LimitCode::MemorySize, value.len() as u64)?;
                    (value.clone(), value)
                }
                None => continue,
            },
        };
        choices.try_push(choice)?;
    }
    Ok(choices.into_vec())
}

/// Returns where the widgets of field `id` appear, in page order: the page
/// object, the widget's object, and its position in the page's `/Annots`.
pub fn form_field_widgets(
    form: &Form,
    version: &Version,
    index: &PageIndex,
    id: Id,
    budget: &Budget,
) -> Result<Vec<(Id, Id, u32)>> {
    let field = form.field(id).ok_or_else(|| stale(id))?;
    let mut widgets = HashSet::new();
    for &widget in &field.widgets {
        insert_set(&mut widgets, widget, budget)?;
    }
    let mut reads = Reads::new(version, budget);
    let mut out = BudgetVec::new(budget);
    for page in index.pages() {
        budget.poll()?;
        let Some(object) = page.object() else {
            continue;
        };
        let Some(Value::Array(items)) = read(&mut reads, page.dict().get("Annots"))? else {
            continue;
        };
        budget.check(LimitCode::AnnotationCount, items.len() as u64)?;
        for (at, item) in items.iter().enumerate() {
            budget.poll()?;
            if let Value::Reference(reference) = item
                && let Some((actual, _)) = object_location(&mut reads, *reference)?
                && widgets.contains(&actual)
            {
                let at = u32::try_from(at).unwrap_or(u32::MAX);
                out.try_push((self::id(object), actual, at))?;
            }
        }
    }
    Ok(out.into_vec())
}

/// A widget as a handle reads it: its position in the page's `/Annots`;
/// its object, `(0, 0)` for a dictionary written there; the terminal field
/// that owns it; its display rectangle; its `/F` flags; the states of its
/// normal appearance; and its `/AS` state.
pub type WidgetRecord = (u32, Id, Option<Id>, Rect, i64, Vec<String>, Option<String>);

/// Returns the record of annotation `dict`, or `None` when it is no widget
/// with a rectangle. `totals` holds the encoded and returned bytes of the
/// states that the read returned.
fn widget(
    annots: &PageAnnots,
    form: &Form,
    index: usize,
    id: Id,
    dict: &Dict,
    totals: &mut (usize, usize),
    budget: &Budget,
) -> Result<Option<WidgetRecord>> {
    if annots.name(dict, "Subtype")?.as_deref() != Some(b"Widget") {
        return Ok(None);
    }
    let Some(area) = annots.rect(dict.get("Rect"))? else {
        return Ok(None);
    };
    let flags = match annots.resolved(dict.get("F"))? {
        Some(Value::Integer(flags)) => flags,
        _ => 0,
    };
    let mut state = |name: &Name| -> Result<String> {
        let name = name.as_bytes();
        charge(
            &mut totals.0,
            name.len(),
            MAX_STATE_BYTES,
            "encoded widget-state",
        )?;
        budget.charge(LimitCode::MemorySize, name.len().saturating_mul(3) as u64)?;
        let state = String::from_utf8_lossy(name).into_owned();
        charge(
            &mut totals.1,
            state.len(),
            MAX_STATE_BYTES,
            "returned widget-state",
        )?;
        Ok(state)
    };
    let mut states = BudgetVec::new(budget);
    if let Some(Value::Dict(appearance)) = annots.resolved(dict.get("AP"))?
        && let Some(Value::Dict(normal)) = annots.resolved(appearance.get("N"))?
    {
        for name in normal.keys() {
            states.try_push(state(name)?)?;
        }
    }
    let current = match annots.resolved(dict.get("AS"))? {
        Some(Value::Name(name)) => Some(state(&name)?),
        _ => None,
    };
    let field = if id == (0, 0) { None } else { form.owner(id) };
    let index = u32::try_from(index).unwrap_or(u32::MAX);
    Ok(Some((
        index,
        id,
        field,
        annots.to_display(area),
        flags,
        states.into_vec(),
        current,
    )))
}

/// Lists the widgets of a page in its `/Annots` order.
pub fn widget_list(annots: &PageAnnots, form: &Form) -> Result<Vec<WidgetRecord>> {
    let (version, _, _) = annots.pages();
    let budget = annots.budget;
    let mut records = BudgetVec::new(budget);
    let mut reads = Reads::new(version, budget);
    let mut totals = (0, 0);
    for (index, item) in annots.items.iter().enumerate() {
        let Some((id, dict)) = annots.entry(item)? else {
            continue;
        };
        let id = if id == (0, 0) {
            id
        } else {
            let Some((actual, _)) = object_location(&mut reads, Reference::new(id.0, id.1))? else {
                continue;
            };
            actual
        };
        if let Some(record) = widget(annots, form, index, id, &dict, &mut totals, budget)? {
            records.try_push(record)?;
        }
    }
    Ok(records.into_vec())
}

/// Locate the same widget after its annotation reference changes through an alias.
pub fn find_widget(annots: &PageAnnots, index: u32, id: Id) -> Result<(usize, Dict)> {
    if id == (0, 0) {
        return annots.find(index, id);
    }
    let matches = |item: &Value, reads: &mut Reads<'_>| -> Result<bool> {
        let Value::Reference(reference) = item else {
            return Ok(false);
        };
        Ok(object_location(reads, *reference)?.is_some_and(|(actual, _)| actual == id))
    };
    let mut reads = Reads::new(annots.pages().0, annots.budget);
    let hint = index as usize;
    let mut position = None;
    if let Some(item) = annots.items.get(hint)
        && matches(item, &mut reads)?
    {
        position = Some(hint);
    }
    if position.is_none() {
        for (at, item) in annots.items.iter().enumerate() {
            reads.budget().poll()?;
            if at != hint && matches(item, &mut reads)? {
                position = Some(at);
                break;
            }
        }
    }
    let Some(position) = position else {
        return Err(Error::Stale(format!(
            "widget {} is no longer in the page's /Annots",
            id.0
        )));
    };
    match annots.entry(&annots.items.as_slice()[position])? {
        Some((_, dict)) => Ok((position, dict)),
        None => Err(Error::Stale(format!(
            "widget {} is no longer a dictionary",
            id.0
        ))),
    }
}

/// Reads a located widget and its owning field.
pub fn widget_read(
    annots: &PageAnnots,
    form: &Form,
    position: usize,
    id: Id,
    dict: &Dict,
) -> Result<WidgetRecord> {
    widget(annots, form, position, id, dict, &mut (0, 0), annots.budget)?
        .ok_or_else(|| Error::Stale(format!("annotation {} is no longer a widget", id.0)))
}
