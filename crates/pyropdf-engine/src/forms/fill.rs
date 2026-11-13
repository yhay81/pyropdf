//! Set a field's value and regenerate its widgets' appearances.

use std::borrow::Cow;
use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode, try_insert_set};
use pyropdf_store::memo::Reads;
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::settle::{Scope, Structure};
use pyropdf_store::value::{Dict, Name, PdfString, Reference, Value};
use pyropdf_store::version::{Transaction, Version};

use super as widgets;
use super::face::{FaceFont, FaceText};
use super::fill_index::{FillCache, indexed_value};
use super::traversal::Field;
use super::values::{
    Id, MAX_INPUT_BYTES, MAX_STATE_BYTES, id, limit, looks_like_widget, object, stale,
};
use crate::annotations::{AnnotationError as Error, AnnotationResult as Result, PageUnits, Reader};
use crate::fonts::embed::Face;
use crate::standard::winansi_byte;

/// The most appearance states of the widgets of one button field.
const MAX_BUTTON_STATES: usize = 8_192;
/// The most distinct states that selecting a check box reads.
const MAX_BUTTON_NAMES: usize = 4_096;

/// Returns `text` as a text string: ASCII without control characters as it
/// is, and else UTF-16BE, in which the line breaks and other controls that
/// PDFDocEncoding leaves unmapped read back.
pub(super) fn encode_value(text: &str, budget: &Budget) -> Result<Value> {
    budget.poll()?;
    let mut bytes = BudgetVec::new(budget);
    if text.is_ascii() && !text.chars().any(char::is_control) {
        bytes.try_extend_from_slice(text.as_bytes())?;
    } else {
        bytes.try_extend_from_slice(&[0xFE, 0xFF])?;
        for (index, unit) in text.encode_utf16().enumerate() {
            if index % 1024 == 0 {
                budget.poll()?;
            }
            bytes.try_extend_from_slice(&unit.to_be_bytes())?;
        }
    }
    Ok(Value::String(PdfString::literal(bytes.into_vec())))
}

/// Returns widget `id` of `version` when it looks like a widget.
fn widget_of(
    version: &Version,
    (number, generation): Id,
    budget: &Budget,
) -> Result<Option<Arc<Value>>> {
    let object = version
        .get(number, budget)?
        .filter(|object| object.generation() == generation);
    Ok(object
        .map(|object| Arc::clone(object.value()))
        .filter(|value| matches!(&**value, Value::Dict(dict) if looks_like_widget(dict))))
}

/// Returns whether a field of `kind` shows its value in text.
pub(super) fn shows_text(kind: &str) -> bool {
    matches!(kind, "text" | "combobox" | "listbox")
}

pub(super) fn is_button(kind: &str) -> bool {
    matches!(kind, "checkbox" | "radio")
}

fn is_winansi(text: &str, budget: &Budget) -> Result<bool> {
    for (index, character) in text.chars().enumerate() {
        if index % 1024 == 0 {
            budget.poll()?;
        }
        if winansi_byte(character).is_none() {
            return Ok(false);
        }
    }
    Ok(true)
}

/// The appearance states of each widget of a button field, by widget.
type States = Vec<(Id, Vec<Name>)>;

/// A value that setting a field writes: text, or a check box's state.
#[derive(Clone)]
pub enum FillValue {
    /// A check box state.
    Flag(bool),
    /// Text or a named button state.
    Text(String),
}

/// Sets the terminal field's value and updates widget appearances when requested.
pub fn fill(
    transaction: &mut Transaction,
    id: Id,
    value: &FillValue,
    budget: &Budget,
    cache: &FillCache,
    draft: bool,
    draw: bool,
) -> Result<()> {
    if let FillValue::Text(text) = value
        && text.len() > MAX_INPUT_BYTES
    {
        return Err(Error::Limit {
            code: LimitCode::FormFieldInputSize,
            message: format!("form field value exceeds the {MAX_INPUT_BYTES}-byte UTF-8 limit"),
        });
    }
    let before = base(transaction, draft);
    change(transaction, id, budget, cache, before, |field, _, _| {
        match field.kind {
            kind if shows_text(kind) && !draw => Ok(After::Set(Cow::Borrowed(value))),
            kind if shows_text(kind) || is_button(kind) => Ok(After::Fill(Cow::Borrowed(value))),
            "pushbutton" => Err(Error::Pdf("pushbutton fields do not have fillable values".into())),
            "signature" => Err(Error::Pdf("filling signature fields is not supported (see the pyHanko integration for digital signatures)".into())),
            _ => Err(Error::Pdf("unsupported field type".into())),
        }
    })
}

/// Collects the field's glyphs or draws its widgets with the supplied font.
pub fn draw_face(
    transaction: &mut Transaction,
    id: Id,
    face: &Face,
    font: Option<&(u32, HashMap<u32, String>)>,
    budget: &Budget,
    cache: &FillCache,
    draft: bool,
) -> Result<Vec<(u32, String)>> {
    let before = base(transaction, draft);
    let index = cache.get(&before, budget)?;
    let field = index.topology.form.field(id).ok_or_else(|| stale(id))?;
    if !shows_text(field.kind) {
        return Ok(Vec::new());
    }
    let font = match font {
        Some((number, mapped)) => {
            // Appearances that show no glyph name no font.
            let generation = before
                .get(*number, budget)?
                .map_or(0, |object| object.generation());
            Some(FaceFont {
                font: Reference::new(*number, generation),
                mapped,
            })
        }
        None => None,
    };
    let writing = font.is_some();
    let mut text = FaceText::new(face, font, budget)?;
    let snapshot = transaction.snapshot();
    let mut units = PageUnits::new(&snapshot, budget);
    for &widget in &field.widgets {
        let version = transaction.version();
        let Some(dict) = widget_of(version, widget, budget)? else {
            continue;
        };
        let Value::Dict(dict) = &*dict else {
            continue;
        };
        let unit = units.unit_of(Some(Reference::new(widget.0, widget.1)), dict)?;
        let reader = Reader::on_page(version, budget, unit);
        let drawing = widgets::face_appearance(&reader, dict, &mut text)?;
        if writing && let Some(drawing) = drawing {
            widgets::write(transaction, widget.0, dict, drawing, budget)?;
        }
    }
    if writing {
        let after = transaction.snapshot();
        let changes = before
            .changes(&after, budget)?
            .ok_or_else(|| Error::Pdf("form versions have different origins".into()))?;
        let mut appearances = index.appearances.clone();
        appearances.refresh(&after, changes.objects(), changes.trailer(), budget)?;
        let complete = appearances.missing.iter().next().is_none();
        update_need_appearances(transaction, !complete, budget)?;
        transaction.retain_derived_version(Arc::clone(&before));
    }
    Ok(text.into_drawn())
}

/// Returns the version that a form edit starts from: the open
/// transaction's draft, or else the committed version, whose fill index
/// the document keeps.
pub(super) fn base(transaction: &Transaction, draft: bool) -> Arc<Version> {
    if draft {
        transaction.snapshot()
    } else {
        Arc::clone(transaction.parent())
    }
}

/// What follows an edit of a field's dictionary.
pub(super) enum After<'v> {
    /// Nothing: the field shows no value.
    Keep,
    /// The field's widgets are drawn again.
    Draw,
    /// The value is set and the field's widgets are drawn again.
    Fill(Cow<'v, FillValue>),
    /// The value is set and the field's widgets are left to the caller.
    Set(Cow<'v, FillValue>),
}

/// Edits field `id` in one write: `edit` checks the field and changes a
/// copy of its dictionary, read from the version before the edit, and
/// says what follows. A value is then set as [`fill`] sets it,
/// and the widgets of the field are drawn or refused with the reason; those
/// of other fields that have no appearance are drawn where they can be.
pub(super) fn change<'v>(
    transaction: &mut Transaction,
    id: Id,
    budget: &Budget,
    cache: &FillCache,
    before: Arc<Version>,
    edit: impl FnOnce(&Field, &mut Reads<'_>, &mut Dict) -> Result<After<'v>>,
) -> Result<()> {
    let index = cache.get(&before, budget)?;
    let mut reads = Reads::new(&before, budget);
    let form = &index.topology.form;
    let field = form.field(id).ok_or_else(|| stale(id))?;
    let Some(original) = object(&mut reads, id)? else {
        return Err(stale(id));
    };
    let Value::Dict(mut dict) = copy_for_edit(&original, budget)? else {
        return Err(stale(id));
    };
    let after = edit(field, &mut reads, &mut dict)?;
    let value = match &after {
        After::Keep => {
            transaction.write(id.0, &Value::Dict(dict), budget)?;
            return Ok(());
        }
        After::Draw => None,
        After::Fill(value) | After::Set(value) => Some(&**value),
    };
    let own = !matches!(after, After::Set(_));
    let button = is_button(field.kind);
    let states = if button && value.is_some() {
        button_states(&before, field, budget)?
    } else {
        Vec::new()
    };
    let on = match value {
        Some(FillValue::Flag(true)) => on_state(&states, budget)?,
        _ => None,
    };
    let text = value.map(|value| match value {
        FillValue::Text(text) => text.as_str(),
        FillValue::Flag(false) => widgets::OFF,
        FillValue::Flag(true) => on.as_deref().unwrap_or("Yes"),
    });
    match text {
        Some(text) if button => {
            plan(&states, text).map_err(limit)?;
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /V key"
            )]
            dict.insert(Name::from("V"), Value::Name(Name::from(text)));
        }
        Some(text) => {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /V key"
            )]
            dict.insert(Name::from("V"), encode_value(text, budget)?);
        }
        None => {}
    }
    transaction.write(id.0, &Value::Dict(dict), budget)?;
    if button && let Some(text) = text {
        select(transaction, &states, text, budget)?;
    }
    let after_write = transaction.snapshot();
    let changes = before
        .changes(&after_write, budget)?
        .ok_or_else(|| Error::Pdf("form versions have different origins".into()))?;
    let mut appearances = index.appearances.clone();
    appearances.refresh(&after_write, changes.objects(), changes.trailer(), budget)?;
    // The widgets of the field are drawn or refused with the reason; those
    // of other fields that have no appearance are drawn where they can be.
    let mut marks = BudgetVec::new(budget);
    let mut buttons = BudgetVec::new(budget);
    {
        let version = transaction.version();
        let mut units = PageUnits::new(version, budget);
        let reader = Reader::new(version, budget);
        for &widget in field.widgets.iter().filter(|_| own) {
            if let Some(dict) = widget_of(version, widget, budget)?
                && let Value::Dict(dict) = &*dict
            {
                let unit = units.unit_of(Some(Reference::new(widget.0, widget.1)), dict)?;
                widgets::appearance(&Reader::on_page(version, budget, unit), dict)?;
                marks.try_push(widget.0)?;
            }
        }
        for (_, missing) in appearances.missing.iter() {
            let Some(other) = form.field(missing.field) else {
                return Err(Error::Pdf("appearance index names a missing field".into()));
            };
            if other.name == field.name {
                continue;
            }
            if is_button(other.kind) {
                buttons.try_push(other.id)?;
                continue;
            }
            if !shows_text(other.kind) {
                continue;
            }
            let value = indexed_value(&index, form, other.id)?;
            if let Some(value) = value
                && !is_winansi(value, budget)?
            {
                continue;
            }
            let widget = missing.widget;
            if let Some(dict) = widget_of(version, widget, budget)?
                && let Value::Dict(dict) = &*dict
                && !widgets::has_normal_stream(&reader, dict)?
            {
                marks.try_push(widget.0)?;
            }
        }
    }
    // A button field without an appearance selects the state of its value
    // in each widget, which then draws the states that it lacks; a field
    // whose states exceed a limit refuses the fill, as pylopdf does.
    let mut buttons = buttons.into_vec();
    buttons.sort_unstable();
    buttons.dedup();
    for other in buttons {
        let Some(other) = form.field(other) else {
            return Err(Error::Pdf("appearance index names a missing field".into()));
        };
        let states = button_states(transaction.version(), other, budget)?;
        let value = indexed_value(&index, form, other.id)?.unwrap_or(widgets::OFF);
        plan(&states, value).map_err(limit)?;
        for (widget, state) in select(transaction, &states, value, budget)? {
            let version = transaction.version();
            let reader = Reader::new(version, budget);
            if let Some(dict) = widget_of(version, widget, budget)?
                && let Value::Dict(dict) = &*dict
                && !(widgets::has_state(&reader, dict, widgets::OFF.as_bytes())?
                    && widgets::has_state(&reader, dict, state.as_bytes())?)
            {
                marks.try_push(widget.0)?;
            }
        }
    }
    for number in marks.into_vec() {
        transaction.mark(Structure::Appearance, number, budget)?;
    }
    transaction.settle(Scope::One(Structure::Appearance), budget)?;
    let after_settle = transaction.snapshot();
    let changes = after_write
        .changes(&after_settle, budget)?
        .ok_or_else(|| Error::Pdf("form versions have different origins".into()))?;
    appearances.refresh(&after_settle, changes.objects(), changes.trailer(), budget)?;
    let complete = appearances.missing.iter().next().is_none();
    update_need_appearances(transaction, !complete, budget)?;
    transaction.retain_derived_version(Arc::clone(&before));
    Ok(())
}
/// Returns the appearance states of each widget of button `field`, charged
/// to `budget`, refusing more than 8,192 entries or 1 MiB of names in all.
fn button_states(version: &Version, field: &Field, budget: &Budget) -> Result<States> {
    let reader = Reader::new(version, budget);
    let (mut entries, mut bytes) = (0, 0);
    let mut states = BudgetVec::new(budget);
    for &widget in &field.widgets {
        let Some(dict) = widget_of(version, widget, budget)? else {
            continue;
        };
        let Value::Dict(dict) = &*dict else {
            continue;
        };
        let names = widgets::with_normal_states(&reader, dict, |normal| {
            let mut names = BudgetVec::new(budget);
            entries += normal.len();
            if entries > MAX_BUTTON_STATES {
                return Err(limit(format!(
                    "AcroForm button states exceed the {MAX_BUTTON_STATES}-entry safety limit"
                )));
            }
            for name in normal.keys() {
                bytes += name.as_bytes().len();
                if bytes > MAX_STATE_BYTES {
                    return Err(limit(state_bytes_error()));
                }
                budget.charge(LimitCode::MemorySize, name.as_bytes().len() as u64)?;
                names.try_push(name.clone())?;
            }
            Ok(names.into_vec())
        })?
        .transpose()?
        .unwrap_or_default();
        states.try_push((widget, names))?;
    }
    Ok(states.into_vec())
}

fn state_bytes_error() -> String {
    format!(
        "AcroForm encoded button-state name text exceeds the {MAX_STATE_BYTES}-byte safety limit"
    )
}

/// Returns the first state of `states` that is not `Off`, refusing more
/// than 4,096 names.
fn on_state(states: &States, budget: &Budget) -> Result<Option<String>> {
    let mut seen = HashSet::new();
    let mut first = None;
    for name in states.iter().flat_map(|(_, names)| names) {
        budget.poll()?;
        if seen.contains(name) {
            continue;
        }
        if seen.len() == MAX_BUTTON_NAMES {
            return Err(limit(format!(
                "AcroForm button states exceed the {MAX_BUTTON_NAMES}-name safety limit"
            )));
        }
        try_insert_set(&mut seen, name, budget)?;
        if first.is_none() && name.as_bytes() != widgets::OFF.as_bytes() {
            first = Some(String::from_utf8_lossy(name.as_bytes()).into_owned());
        }
    }
    Ok(first)
}

/// Returns the state that each widget of `states` selects for `value`.
fn selected<'s>(states: &'s States, value: &'s str) -> impl Iterator<Item = (Id, &'s str)> {
    let known = states
        .iter()
        .any(|(_, names)| names.iter().any(|name| name.as_bytes() == value.as_bytes()));
    states
        .iter()
        .enumerate()
        .map(move |(index, (widget, names))| {
            let chosen = value != widgets::OFF
                && (names.iter().any(|name| name.as_bytes() == value.as_bytes())
                    || (!known && index == 0));
            (*widget, if chosen { value } else { widgets::OFF })
        })
}

/// Refuses a selection of `value` that would give the widgets more than
/// 8,192 states, or 1 MiB of names, with the states that it draws.
fn plan(states: &States, value: &str) -> Result<(), String> {
    let mut entries: usize = states.iter().map(|(_, names)| names.len()).sum();
    let mut bytes: usize = states
        .iter()
        .flat_map(|(_, names)| names)
        .map(|name| name.as_bytes().len())
        .sum();
    for ((_, names), (_, state)) in states.iter().zip(selected(states, value)) {
        let on = (state != widgets::OFF).then_some(state);
        for state in std::iter::once(widgets::OFF).chain(on) {
            if names.iter().any(|name| name.as_bytes() == state.as_bytes()) {
                continue;
            }
            entries += 1;
            if entries > MAX_BUTTON_STATES {
                return Err(format!(
                    "AcroForm button state update exceeds the {MAX_BUTTON_STATES}-entry safety limit"
                ));
            }
            bytes += state.len();
            if bytes > MAX_STATE_BYTES {
                return Err(state_bytes_error());
            }
        }
    }
    Ok(())
}

/// Sets each widget's `/AS` to the state that it selects for `value`, and
/// returns the states.
fn select<'s>(
    transaction: &mut Transaction,
    states: &'s States,
    value: &'s str,
    budget: &Budget,
) -> Result<Vec<(Id, &'s str)>> {
    let mut chosen = BudgetVec::new(budget);
    for (widget, state) in selected(states, value) {
        if let Some(original) = widget_of(transaction.version(), widget, budget)?
            && let Value::Dict(dict) = &*original
            && !matches!(dict.get("AS"), Some(Value::Name(current)) if current.as_bytes() == state.as_bytes())
        {
            let Value::Dict(mut dict) = copy_for_edit(&original, budget)? else {
                unreachable!("a copied dictionary remains a dictionary");
            };
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /AS key"
            )]
            dict.insert(Name::from("AS"), Value::Name(Name::from(state)));
            transaction.write(widget.0, &Value::Dict(dict), budget)?;
        }
        chosen.try_push((widget, state))?;
    }
    Ok(chosen.into_vec())
}

/// Sets the form's `/NeedAppearances` to `value` where the form dictionary
/// is written: as an object of its own, or in the catalog's. Returns
/// whether the document has a form dictionary.
pub(super) fn update_need_appearances(
    transaction: &mut Transaction,
    value: bool,
    budget: &Budget,
) -> Result<bool> {
    let version = transaction.snapshot();
    let mut reads = Reads::new(&version, budget);
    let Some(Value::Reference(root)) = reads.trailer().get("Root").cloned() else {
        return Ok(false);
    };
    let Some(catalog_value) = object(&mut reads, id(root))? else {
        return Ok(false);
    };
    let Value::Dict(catalog) = &*catalog_value else {
        return Ok(false);
    };
    let (number, value) = match catalog.get("AcroForm") {
        Some(Value::Reference(reference)) => match object(&mut reads, id(*reference))?.as_deref() {
            Some(form @ Value::Dict(dict)) => {
                if dict.get("NeedAppearances") == Some(&Value::Boolean(value)) {
                    return Ok(true);
                }
                let Value::Dict(mut form) = copy_for_edit(form, budget)? else {
                    unreachable!("a copied dictionary remains a dictionary");
                };
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /NeedAppearances key"
                )]
                form.insert(Name::from("NeedAppearances"), Value::Boolean(value));
                (reference.number(), Value::Dict(form))
            }
            _ => return Ok(false),
        },
        Some(Value::Dict(form)) => {
            if form.get("NeedAppearances") == Some(&Value::Boolean(value)) {
                return Ok(true);
            }
            let Value::Dict(mut catalog) = copy_for_edit(&catalog_value, budget)? else {
                unreachable!("a copied dictionary remains a dictionary");
            };
            let Some(Value::Dict(form)) = catalog.get_mut("AcroForm") else {
                unreachable!("the copied catalog retains its form dictionary");
            };
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /NeedAppearances key"
            )]
            form.insert(Name::from("NeedAppearances"), Value::Boolean(value));
            (root.number(), Value::Dict(catalog))
        }
        _ => return Ok(false),
    };
    transaction.write(number, &value, budget)?;
    Ok(true)
}

/// Set the interactive form's appearance flag, refusing a document without a form.
pub fn set_need_appearances(
    transaction: &mut Transaction,
    value: bool,
    budget: &Budget,
) -> Result<()> {
    if update_need_appearances(transaction, value, budget)? {
        Ok(())
    } else {
        Err(Error::Pdf("the document has no interactive form".into()))
    }
}
