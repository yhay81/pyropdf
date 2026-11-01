//! The interactive form (ISO 32000-2 section 12.7): the terminal fields
//! that `doc.form` maps by name, the widgets of pages and of fields, the
//! values that setting a field writes, and the repair of radio buttons
//! written as fields of their own (DESIGN.md section 10).
//!
//! The walk of the field tree is memoized with the objects that it read
//! (section 5), so reading every field of a form costs one walk of each
//! version. Every read shares pylopdf's field-tree limits. Ported from
//! pylopdf's `rust/src/document/forms.rs` at
//! ebb01d3c4c742046e8ce7fd72957cd15a2933321.

use std::borrow::Cow;
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex, PoisonError, Weak};

use pyo3::prelude::*;
use pyropdf_engine::annotations::{Reader, synthesized};
use pyropdf_engine::fonts::embed::Face;
use pyropdf_engine::forms::defaults::{self, Joined, Renames, Slot};
use pyropdf_engine::forms::face::{FaceFont, FaceText};
use pyropdf_engine::forms::flatten::{self as flattening, Missing, Selection};
use pyropdf_engine::forms::{self as widgets, WidgetError};
use pyropdf_engine::standard::{DA_FONTS, DefaultAppearance, winansi_byte};
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::memo::{DerivedMap, ReadDependencies, ReadSet, Reads};
use pyropdf_store::pages::{MAX_CHAIN, PageIndex};
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::settle::{Scope, Structure};
use pyropdf_store::value::{Array, Dict, Name, PdfString, Reference, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::annotations::{
    Annots, CopyPart, Rect, border_style, charge_copy, editing, encode_text, rgb,
};
use crate::document::{_Document, Failed, text_string};
use crate::errors::{
    LimitError, MalformedPdfError, PdfError, StaleObjectError, limit_error, store_error,
};
use crate::fonts::_Font;
use crate::pages::index;

/// The most terminal fields that a walk returns.
const MAX_FIELDS: usize = 4_096;
/// The most field dictionaries that a walk visits.
const MAX_NODES: usize = 4_096;
/// The most `/Fields` and `/Kids` entries that a walk reads.
const MAX_EDGES: usize = 8_192;
/// The most levels of the field tree.
const MAX_DEPTH: usize = 64;
/// The most widgets below the fields of a walk, and of one field.
const MAX_WIDGETS: usize = 4_096;
/// The bytes of the names and types that a walk reads and builds.
const MAX_NAME_BYTES: usize = 1024 * 1024;
/// The bytes of the values that a walk reads and returns, and of the
/// options of a choice field.
const MAX_VALUE_BYTES: usize = 1024 * 1024;
/// The most items of the array values of a walk, and options of a choice
/// field.
const MAX_VALUE_ITEMS: usize = 4_096;
/// The most fields of a `/Parent` chain that an inherited entry is read
/// along.
const MAX_PARENTS: usize = 64;
/// The bytes of the appearance states that one read of a page's widgets
/// returns.
const MAX_STATE_BYTES: usize = 1024 * 1024;
/// The UTF-8 bytes of the value that setting a field writes
/// (`form_field_input_size`).
const MAX_INPUT_BYTES: usize = 1024 * 1024;
/// The most appearance states of the widgets of one button field.
const MAX_BUTTON_STATES: usize = 8_192;
/// The most distinct appearance states that setting a check box to `True`
/// reads.
const MAX_BUTTON_NAMES: usize = 4_096;
/// The `/Ff` bit of radio buttons.
const RADIO: i64 = 1 << 15;
/// The keys of a radio button that a repaired group gives to its new
/// parent field.
const RADIO_KEYS: [&str; 7] = ["FT", "T", "TU", "TM", "Ff", "V", "DV"];
const FIELD_TRIGGERS: [&str; 4] = ["K", "F", "V", "C"];

/// An object number and generation.
type Id = (u32, u16);

/// A terminal field as the walk read it.
struct Field {
    /// The partial names of the field and its ancestors, joined by dots.
    name: String,
    id: Id,
    kind: &'static str,
    /// The inherited `/Ff`.
    flags: i64,
    value: Option<Arc<str>>,
    /// The widgets that show the field, or the field itself when it has
    /// none.
    widgets: Vec<Id>,
    /// Its node in a fill walk's ancestry, absent in an ordinary read.
    trace: Option<usize>,
}

#[derive(Clone, Copy)]
struct TraceNode {
    id: Id,
    parent: Option<usize>,
}

/// The terminal fields of a form, as one walk of its field tree read them.
#[derive(Default)]
pub(crate) struct Form {
    /// The fields by name and then object.
    fields: Vec<Field>,
    /// The position in `fields` of each field's object, by object.
    objects: Vec<(Id, usize)>,
    /// The field that owns each widget, the first in `fields` that lists
    /// it, by widget.
    owners: Vec<(Id, usize)>,
    /// Nodes visited by a fill walk, in parent-before-child order.
    trace: Vec<TraceNode>,
}

impl Form {
    fn field(&self, id: Id) -> Option<&Field> {
        let at = self.objects.binary_search_by_key(&id, |&(id, _)| id).ok()?;
        Some(&self.fields[self.objects[at].1])
    }

    fn owner(&self, widget: Id) -> Option<Id> {
        let at = self
            .owners
            .binary_search_by_key(&widget, |&(id, _)| id)
            .ok()?;
        Some(self.fields[self.owners[at].1].id)
    }
}

/// How a node of the walk takes part in it.
#[derive(Clone, Copy)]
enum Role {
    /// A `/Fields` entry or a named kid, which can be a terminal field.
    Field,
    /// A nameless intermediate field folded into the nearest field above
    /// it, whose reserved entry, if any, is at this position.
    Folded(Option<usize>),
}

/// A node that the walk has yet to visit, with what it inherits: the name
/// above it, `/FT`, `/Ff`, and `/V`.
///
/// The name and the inherited text are shared with the node that pushed
/// it: only the first visit of a node charges the text budgets, while
/// `/Kids` can push it once per edge.
struct Node {
    id: Id,
    prefix: Arc<String>,
    kind: Option<Arc<String>>,
    flags: i64,
    value: Option<Arc<str>>,
    depth: usize,
    role: Role,
    trace_parent: Option<usize>,
}

/// A terminal field that the walk found: its name, object, `/FT`, `/Ff`,
/// and value.
type Leaf = (
    String,
    Id,
    Arc<String>,
    i64,
    Option<Arc<str>>,
    Option<usize>,
);

/// What a `/Kids` entry is to the walk.
enum Kid {
    /// A kid with a partial name.
    Field(Id),
    /// A nameless kid without kids of its own.
    Widget(Id),
    /// A nameless kid with kids, folded into the field above it; it is a
    /// widget too when it looks like one.
    Intermediate { id: Id, kids: Array, widget: bool },
    /// A kid that is no dictionary.
    Unusable,
    /// A direct entry, a null, or a reference to a missing object.
    Ignored,
}

fn limit(message: String) -> PyErr {
    PdfError::new_err(message)
}

fn edges_error() -> PyErr {
    limit(format!(
        "AcroForm field tree exceeds the {MAX_EDGES}-edge safety limit"
    ))
}

fn stale((number, _): Id) -> PyErr {
    StaleObjectError::new_err(format!("object {number} is no longer a field of the form"))
}

/// Adds `amount` to `total`, refusing a total above `limit`.
fn charge(total: &mut usize, amount: usize, limit: usize, label: &str) -> PyResult<()> {
    *total = total.saturating_add(amount);
    if *total > limit {
        return Err(PdfError::new_err(format!(
            "AcroForm {label} text exceeds the {limit}-byte safety limit"
        )));
    }
    Ok(())
}

/// Returns object `id`, or `None` when it is missing.
fn object(reads: &mut Reads<'_>, (number, generation): Id) -> PyResult<Option<Arc<Value>>> {
    Ok(reads
        .get(number)
        .map_err(|e| store_error(&e))?
        .filter(|object| object.generation() == generation)
        .map(|object| Arc::clone(object.value())))
}

/// Follow object aliases while retaining the final identity and one snapshot.
fn object_location(
    reads: &mut Reads<'_>,
    reference: Reference,
) -> PyResult<Option<(Id, Arc<Value>)>> {
    let budget = reads.budget();
    let mut current = reference;
    let mut seen = [None; MAX_CHAIN];
    for depth in 0..MAX_CHAIN {
        budget.poll().map_err(|e| limit_error(&e))?;
        if seen[..depth].contains(&Some(current)) {
            return Ok(None);
        }
        seen[depth] = Some(current);
        let Some(value) = object(reads, id(current))? else {
            return Ok(None);
        };
        match &*value {
            Value::Null => return Ok(None),
            Value::Reference(next) => current = *next,
            _ => return Ok(Some((id(current), value))),
        }
    }
    if seen.contains(&Some(current)) {
        return Ok(None);
    }
    Err(MalformedPdfError::new_err(format!(
        "a form reference chain exceeds {MAX_CHAIN} references"
    )))
}

/// Resolve a value, charging owned buffers before copying them.
fn located(reads: &mut Reads<'_>, value: Option<&Value>) -> PyResult<Option<(Value, Option<Id>)>> {
    let budget = reads.budget();
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Reference(reference)) => match object_location(reads, *reference)? {
            Some((identity, value)) => copy_for_edit(&value, budget)
                .map(|value| Some((value, Some(identity))))
                .map_err(|e| store_error(&e)),
            None => Ok(None),
        },
        Some(value) => copy_for_edit(value, budget)
            .map(|value| Some((value, None)))
            .map_err(|e| store_error(&e)),
    }
}

fn read(reads: &mut Reads<'_>, value: Option<&Value>) -> PyResult<Option<Value>> {
    Ok(located(reads, value)?.map(|(value, _)| value))
}

/// Charge a fallible allocation before growing an owned input-sized vector.
fn push<T>(items: &mut Vec<T>, value: T, budget: &Budget) -> PyResult<()> {
    budget.poll().map_err(|e| limit_error(&e))?;
    if items.len() == items.capacity() {
        let count = items.len().max(4);
        budget
            .charge(
                LimitCode::MemorySize,
                count.saturating_mul(2 * size_of::<T>()) as u64,
            )
            .map_err(|e| limit_error(&e))?;
        items
            .try_reserve(count)
            .map_err(|_| limit_error(&pyropdf_store::LimitExceeded::new(LimitCode::MemorySize)))?;
    }
    items.push(value);
    Ok(())
}

fn reserve_map<K: Eq + std::hash::Hash, V>(
    map: &mut HashMap<K, V>,
    budget: &Budget,
) -> PyResult<()> {
    budget
        .charge(LimitCode::MemorySize, (64 + 2 * size_of::<(K, V)>()) as u64)
        .map_err(|e| limit_error(&e))?;
    map.try_reserve(1)
        .map_err(|_| limit_error(&pyropdf_store::LimitExceeded::new(LimitCode::MemorySize)))
}

const fn id(reference: Reference) -> Id {
    (reference.number(), reference.generation())
}

/// Returns whether `value` reads as an omitted entry: a null, or a
/// reference to a null or to a missing object (ISO 32000-2 sections 7.3.7
/// and 7.3.10).
fn omitted(reads: &mut Reads<'_>, value: &Value) -> PyResult<bool> {
    Ok(match value {
        Value::Null => true,
        Value::Reference(reference) => {
            object(reads, id(*reference))?.is_none_or(|value| matches!(*value, Value::Null))
        }
        _ => false,
    })
}

/// Returns entry `key` of field `dict` unless it reads as omitted, so that
/// the field inherits it.
fn own<'d>(reads: &mut Reads<'_>, dict: &'d Dict, key: &str) -> PyResult<Option<&'d Value>> {
    Ok(match dict.get(key) {
        Some(value) if !omitted(reads, value)? => Some(value),
        _ => None,
    })
}

/// Returns the `/Kids` array of field `dict`.
fn kids(reads: &mut Reads<'_>, dict: &Dict) -> PyResult<Option<Array>> {
    Ok(match read(reads, dict.get("Kids"))? {
        Some(Value::Array(kids)) => Some(kids),
        _ => None,
    })
}

/// Returns whether `dict` looks like a widget: its `/Subtype` is `Widget`,
/// or it has a `/Rect`.
fn looks_like_widget(dict: &Dict) -> bool {
    matches!(dict.get("Subtype"), Some(Value::Name(name)) if name.as_bytes() == b"Widget")
        || dict.get("Rect").is_some()
}

fn classify(reads: &mut Reads<'_>, kid: &Value) -> PyResult<Kid> {
    let Value::Reference(reference) = kid else {
        return Ok(Kid::Ignored);
    };
    let Some((kid, value)) = object_location(reads, *reference)? else {
        return Ok(Kid::Ignored);
    };
    let child = match &*value {
        Value::Dict(child) => child,
        Value::Null => return Ok(Kid::Ignored),
        _ => return Ok(Kid::Unusable),
    };
    if own(reads, child, "T")?.is_some() {
        return Ok(Kid::Field(kid));
    }
    Ok(match kids(reads, child)? {
        Some(kids) if !kids.is_empty() => Kid::Intermediate {
            id: kid,
            kids,
            widget: looks_like_widget(child),
        },
        _ => Kid::Widget(kid),
    })
}

/// Marks node `id` visited, refusing more than [`MAX_NODES`]; returns
/// whether it was new.
fn insert_set(visited: &mut HashSet<Id>, id: Id, budget: &Budget) -> PyResult<bool> {
    budget.poll().map_err(|e| limit_error(&e))?;
    if visited.contains(&id) {
        return Ok(false);
    }
    budget
        .charge(LimitCode::MemorySize, (64 + 2 * size_of::<Id>()) as u64)
        .map_err(|e| limit_error(&e))?;
    visited
        .try_reserve(1)
        .map_err(|_| limit_error(&pyropdf_store::LimitExceeded::new(LimitCode::MemorySize)))?;
    Ok(visited.insert(id))
}

fn visit(visited: &mut HashSet<Id>, id: Id, budget: &Budget) -> PyResult<bool> {
    if !insert_set(visited, id, budget)? {
        return Ok(false);
    }
    if visited.len() > MAX_NODES {
        return Err(limit(format!(
            "AcroForm field tree exceeds the {MAX_NODES}-node safety limit"
        )));
    }
    Ok(true)
}

fn add_edges(edges: &mut usize, count: usize) -> PyResult<()> {
    *edges = edges.saturating_add(count);
    if *edges > MAX_EDGES {
        return Err(edges_error());
    }
    Ok(())
}

/// Decodes text string `encoded`, charging its encoded and decoded bytes
/// to `totals`.
fn decode(
    encoded: &PdfString,
    totals: &mut (usize, usize),
    limit: usize,
    label: &str,
    budget: &Budget,
) -> PyResult<String> {
    charge(
        &mut totals.0,
        encoded.as_bytes().len(),
        limit,
        &format!("encoded {label}"),
    )?;
    budget
        .charge(
            LimitCode::MemorySize,
            encoded.as_bytes().len().saturating_mul(4) as u64,
        )
        .map_err(|e| limit_error(&e))?;
    let decoded = text_string(encoded.as_bytes());
    charge(
        &mut totals.1,
        decoded.len(),
        limit,
        &format!("decoded {label}"),
    )?;
    Ok(decoded)
}

/// Returns the text string that `value` holds, or `None` for anything
/// else.
fn text(
    reads: &mut Reads<'_>,
    value: &Value,
    totals: &mut (usize, usize),
    limit: usize,
    label: &str,
) -> PyResult<Option<String>> {
    match read(reads, Some(value))? {
        Some(Value::String(encoded)) => {
            decode(&encoded, totals, limit, label, reads.budget()).map(Some)
        }
        _ => Ok(None),
    }
}

/// Returns a field value as text: a name, a text string, or the text
/// strings of an array joined by commas.
fn field_value(
    reads: &mut Reads<'_>,
    value: &Value,
    totals: &mut (usize, usize),
    items: &mut usize,
) -> PyResult<Option<String>> {
    let label = "field-value";
    match read(reads, Some(value))? {
        Some(Value::Name(name)) => {
            let name = name.as_bytes();
            charge(
                &mut totals.0,
                name.len(),
                MAX_VALUE_BYTES,
                "encoded field-value",
            )?;
            reads
                .budget()
                .charge(LimitCode::MemorySize, name.len().saturating_mul(3) as u64)
                .map_err(|e| limit_error(&e))?;
            let decoded = String::from_utf8_lossy(name).into_owned();
            charge(
                &mut totals.1,
                decoded.len(),
                MAX_VALUE_BYTES,
                "decoded field-value",
            )?;
            Ok(Some(decoded))
        }
        Some(Value::String(encoded)) => {
            decode(&encoded, totals, MAX_VALUE_BYTES, label, reads.budget()).map(Some)
        }
        Some(Value::Array(array)) => {
            *items = items.saturating_add(array.len());
            if *items > MAX_VALUE_ITEMS {
                return Err(limit(format!(
                    "AcroForm field values exceed the {MAX_VALUE_ITEMS}-item safety limit"
                )));
            }
            let budget = reads.budget();
            let mut values = BudgetVec::new(budget);
            for item in array.iter() {
                if let Some(Value::String(encoded)) = read(reads, Some(item))? {
                    values
                        .try_push(decode(&encoded, totals, MAX_VALUE_BYTES, label, budget)?)
                        .map_err(|e| limit_error(&e))?;
                }
            }
            let separators = values.len().saturating_sub(1) * 2;
            if separators != 0 {
                charge(
                    &mut totals.1,
                    separators,
                    MAX_VALUE_BYTES,
                    "decoded field-value",
                )?;
            }
            let output = values
                .iter()
                .fold(separators, |total, value| total.saturating_add(value.len()));
            budget
                .charge(LimitCode::MemorySize, output as u64)
                .map_err(|e| limit_error(&e))?;
            Ok(Some(values.join(", ")))
        }
        _ => Ok(None),
    }
}

/// Returns the form's `/Fields` array, or `None` for a document without
/// one.
fn top_fields(reads: &mut Reads<'_>) -> PyResult<Option<Array>> {
    let Some(&Value::Reference(root)) = reads.trailer().get("Root") else {
        return Ok(None);
    };
    let Some(Value::Dict(catalog)) = read(reads, Some(&Value::Reference(root)))? else {
        return Ok(None);
    };
    let Some(Value::Dict(form)) = read(reads, catalog.get("AcroForm"))? else {
        return Ok(None);
    };
    Ok(match read(reads, form.get("Fields"))? {
        Some(Value::Array(fields)) => Some(fields),
        _ => None,
    })
}

/// The text that one walk reads and builds, against its limits.
#[derive(Default)]
struct Text {
    /// The encoded and decoded bytes of names and types.
    names: (usize, usize),
    /// The bytes of the full names.
    built: usize,
    /// The encoded and decoded bytes of values.
    values: (usize, usize),
    /// The items of array values.
    items: usize,
}

/// Returns the kind of a field from its `/FT` and `/Ff`.
fn kind(field_type: &str, flags: i64) -> &'static str {
    match field_type {
        "Tx" => "text",
        "Sig" => "signature",
        "Ch" if flags & (1 << 17) != 0 => "combobox",
        "Ch" => "listbox",
        "Btn" if flags & (1 << 16) != 0 => "pushbutton",
        "Btn" if flags & RADIO != 0 => "radio",
        "Btn" => "checkbox",
        _ => "unknown",
    }
}

/// A node's full name, `/FT`, `/Ff`, and `/V`.
type Entries = (Arc<String>, Option<Arc<String>>, i64, Option<Arc<str>>);

/// Reads node `node`: its name, `/FT`, `/Ff`, and `/V` after what it
/// inherits.
fn node_entries(
    reads: &mut Reads<'_>,
    dict: &Dict,
    node: &Node,
    text_budget: &mut Text,
    separate_values: bool,
) -> PyResult<Entries> {
    let component = match dict.get("T") {
        Some(name) => text(
            reads,
            name,
            &mut text_budget.names,
            MAX_NAME_BYTES,
            "field-name",
        )?,
        None => None,
    };
    let built_len = match &component {
        Some(component) if !node.prefix.is_empty() => node
            .prefix
            .len()
            .saturating_add(1)
            .saturating_add(component.len()),
        Some(component) => component.len(),
        None => node.prefix.len(),
    };
    charge(
        &mut text_budget.built,
        built_len,
        MAX_NAME_BYTES,
        "materialized field-name",
    )?;
    if component.is_some() {
        reads
            .budget()
            .charge(
                LimitCode::MemorySize,
                built_len.saturating_add(size_of::<String>() + 32) as u64,
            )
            .map_err(|e| limit_error(&e))?;
    }
    let name = match component {
        Some(component) if node.prefix.is_empty() => Arc::new(component),
        Some(component) => Arc::new(format!("{}.{component}", node.prefix)),
        None => Arc::clone(&node.prefix),
    };
    let field_type = match read(reads, dict.get("FT"))? {
        Some(Value::Name(field_type)) => {
            let field_type = field_type.as_bytes();
            let names = &mut text_budget.names;
            charge(
                &mut names.0,
                field_type.len(),
                MAX_NAME_BYTES,
                "encoded field-name/type",
            )?;
            reads
                .budget()
                .charge(
                    LimitCode::MemorySize,
                    field_type.len().saturating_mul(3).saturating_add(32) as u64,
                )
                .map_err(|e| limit_error(&e))?;
            let decoded = String::from_utf8_lossy(field_type).into_owned();
            charge(
                &mut names.1,
                decoded.len(),
                MAX_NAME_BYTES,
                "decoded field-name/type",
            )?;
            Some(Arc::new(decoded))
        }
        _ => node.kind.clone(),
    };
    let flags = match read(reads, dict.get("Ff"))? {
        Some(Value::Integer(flags)) => flags,
        _ => node.flags,
    };
    let value = if separate_values {
        let mut value_reads = Reads::new(reads.version(), reads.budget());
        node_value(&mut value_reads, dict, &node.value, text_budget)?
    } else {
        node_value(reads, dict, &node.value, text_budget)?
    };
    Ok((name, field_type, flags, value))
}

fn node_value(
    reads: &mut Reads<'_>,
    dict: &Dict,
    inherited: &Option<Arc<str>>,
    text_budget: &mut Text,
) -> PyResult<Option<Arc<str>>> {
    Ok(match own(reads, dict, "V")? {
        Some(value) => field_value(
            reads,
            value,
            &mut text_budget.values,
            &mut text_budget.items,
        )?
        .map(|value| {
            reads
                .budget()
                .charge(LimitCode::MemorySize, value.len().saturating_add(32) as u64)
                .map_err(|e| limit_error(&e))?;
            Ok::<Arc<str>, PyErr>(Arc::from(value))
        })
        .transpose()?,
        None => inherited.clone(),
    })
}

/// Walks the field tree and returns its terminal fields, by name and then
/// object.
///
/// `/FT`, `/Ff`, and `/V` inherit from the parent when a field omits them
/// or holds a null, an indirect null, or a reference to a missing object;
/// `/FT` and `/Ff` also inherit past any other unreadable value. A
/// terminal field has a type and no kid with a partial name; a nameless
/// kid with kids of its own folds into the field above it. The walk
/// refuses a tree past its limits instead of returning part of it.
fn terminal_fields(reads: &mut Reads<'_>, tracing: bool) -> PyResult<(Vec<Leaf>, Vec<TraceNode>)> {
    let Some(fields) = top_fields(reads)? else {
        return Ok((Vec::new(), Vec::new()));
    };
    if fields.len() > MAX_EDGES {
        return Err(edges_error());
    }
    // Each field with a type reserves its entry before its kids are read,
    // so that a nameless intermediate folded into it can withdraw the
    // entry when it holds a child field.
    let budget = reads.budget();
    let mut leaves: BudgetVec<'_, Option<Leaf>> = BudgetVec::new(budget);
    let mut stack = BudgetVec::new(budget);
    let mut trace = BudgetVec::new(budget);
    stack
        .try_reserve_exact(fields.len())
        .map_err(|e| limit_error(&e))?;
    let top = Arc::new(String::new());
    for field in fields.iter() {
        // As in `/Kids`, an omitted entry costs only its edge.
        if let Value::Reference(reference) = field
            && !omitted(reads, field)?
        {
            stack
                .try_push(Node {
                    id: id(*reference),
                    prefix: Arc::clone(&top),
                    kind: None,
                    flags: 0,
                    value: None,
                    depth: 1,
                    role: Role::Field,
                    trace_parent: None,
                })
                .map_err(|e| limit_error(&e))?;
        }
    }
    let mut visited = HashSet::new();
    let mut edges = fields.len();
    let mut widgets = 0usize;
    let mut text_budget = Text::default();
    while let Some(mut node) = stack.pop() {
        let reference = Reference::new(node.id.0, node.id.1);
        let Some((actual, object)) = object_location(reads, reference)? else {
            continue;
        };
        node.id = actual;
        if !visit(&mut visited, node.id, budget)? {
            continue;
        }
        let Value::Dict(dict) = &*object else {
            continue;
        };
        let trace_at = if tracing {
            let at = trace.len();
            trace
                .try_push(TraceNode {
                    id: node.id,
                    parent: node.trace_parent,
                })
                .map_err(|e| limit_error(&e))?;
            Some(at)
        } else {
            None
        };
        let (name, field_type, flags, value) =
            node_entries(reads, dict, &node, &mut text_budget, tracing)?;
        let leaf = match node.role {
            Role::Field if field_type.is_some() => {
                leaves.try_push(None).map_err(|e| limit_error(&e))?;
                Some(leaves.len() - 1)
            }
            Role::Field => None,
            Role::Folded(leaf) => leaf,
        };
        let mut has_child_fields = false;
        if let Some(kids) = kids(reads, dict)? {
            add_edges(&mut edges, kids.len())?;
            for kid in kids.iter() {
                // An ignored entry costs only its edge. Every other entry
                // that is neither a child field nor an intermediate counts
                // toward the widgets, as does an intermediate that looks
                // like a widget, so that the limit covers every widget
                // that `field_widgets` can return.
                let kid = classify(reads, kid)?;
                if matches!(
                    kid,
                    Kid::Widget(_) | Kid::Unusable | Kid::Intermediate { widget: true, .. }
                ) {
                    widgets += 1;
                    if widgets > MAX_WIDGETS {
                        return Err(limit(format!(
                            "AcroForm field tree exceeds the {MAX_WIDGETS}-widget safety limit"
                        )));
                    }
                }
                // An intermediate is walked like a child field, but it adds
                // no partial name and reports its child fields to the field
                // it folds into.
                let (kid, role) = match kid {
                    Kid::Field(kid) => {
                        has_child_fields = true;
                        (kid, Role::Field)
                    }
                    Kid::Intermediate { id, .. } => (id, Role::Folded(leaf)),
                    Kid::Widget(_) | Kid::Unusable | Kid::Ignored => continue,
                };
                if node.depth >= MAX_DEPTH {
                    if visited.contains(&kid) {
                        continue;
                    }
                    return Err(limit(format!(
                        "AcroForm field tree exceeds the {MAX_DEPTH}-level safety limit"
                    )));
                }
                stack
                    .try_push(Node {
                        id: kid,
                        prefix: Arc::clone(&name),
                        kind: field_type.clone(),
                        flags,
                        value: value.clone(),
                        depth: node.depth + 1,
                        role,
                        trace_parent: trace_at,
                    })
                    .map_err(|e| limit_error(&e))?;
            }
        }
        if let Some(at) = leaf {
            if has_child_fields {
                leaves[at] = None;
            } else if let (Role::Field, Some(field_type)) = (node.role, field_type) {
                budget
                    .charge(LimitCode::MemorySize, name.len() as u64)
                    .map_err(|e| limit_error(&e))?;
                leaves[at] = Some(((*name).clone(), node.id, field_type, flags, value, trace_at));
            }
        }
    }
    let mut out = BudgetVec::new(budget);
    for leaf in leaves.into_vec().into_iter().flatten() {
        out.try_push(leaf).map_err(|e| limit_error(&e))?;
    }
    if out.len() > MAX_FIELDS {
        return Err(limit(format!(
            "AcroForm field tree exceeds the {MAX_FIELDS}-entry safety limit"
        )));
    }
    let mut returned = 0usize;
    for (_, _, _, _, value, _) in &out {
        if let Some(value) = value {
            charge(
                &mut returned,
                value.len(),
                MAX_VALUE_BYTES,
                "returned field-value",
            )?;
        }
    }
    out.sort_unstable_by(|left, right| left.0.cmp(&right.0).then(left.1.cmp(&right.1)));
    Ok((out.into_vec(), trace.into_vec()))
}

/// Returns the widgets below field `field` in the order of its kids, or the
/// field itself when it has none.
///
/// The widgets of a nameless intermediate take its place among the kids,
/// and each intermediate is expanded once, so that a cycle ends. A walk that
/// returned the field visited every intermediate below it within the walk's
/// limits; the limits here keep this traversal bounded on its own.
fn field_widgets(reads: &mut Reads<'_>, field: Id) -> PyResult<Vec<Id>> {
    let budget = reads.budget();
    let mut widgets = BudgetVec::new(budget);
    let top = match object(reads, field)? {
        Some(value) => match &*value {
            Value::Dict(dict) => kids(reads, dict)?.unwrap_or_default(),
            _ => Array::new(),
        },
        None => Array::new(),
    };
    let mut edges = 0;
    add_edges(&mut edges, top.len())?;
    let mut frames = BudgetVec::new(budget);
    frames
        .try_push((top, 0usize))
        .map_err(|e| limit_error(&e))?;
    let mut expanded = HashSet::new();
    while let Some((kids, at)) = frames.last_mut() {
        let kid = match kids.get(*at) {
            Some(Value::Reference(reference)) => Value::Reference(*reference),
            Some(_) => {
                *at += 1;
                continue;
            }
            None => {
                frames.pop();
                continue;
            }
        };
        *at += 1;
        let (kid, widget, folded) = match classify(reads, &kid)? {
            Kid::Widget(kid) => (kid, true, None),
            Kid::Intermediate { id, kids, widget } => (id, widget, Some(kids)),
            Kid::Field(_) | Kid::Unusable | Kid::Ignored => continue,
        };
        if widget {
            if widgets.len() >= MAX_WIDGETS {
                return Err(limit(format!(
                    "AcroForm field exceeds the {MAX_WIDGETS}-widget safety limit"
                )));
            }
            widgets.try_push(kid).map_err(|e| limit_error(&e))?;
        }
        let Some(kids) = folded else {
            continue;
        };
        // The field is the first expanded node, so a cycle back to it ends.
        if expanded.is_empty() {
            visit(&mut expanded, field, budget)?;
        }
        if visit(&mut expanded, kid, budget)? {
            add_edges(&mut edges, kids.len())?;
            frames.try_push((kids, 0)).map_err(|e| limit_error(&e))?;
        }
    }
    if widgets.is_empty() {
        widgets.try_push(field).map_err(|e| limit_error(&e))?;
    }
    Ok(widgets.into_vec())
}

/// Walks the form of the version that `reads` reads.
fn walk(reads: &mut Reads<'_>) -> PyResult<Form> {
    walk_with_trace(reads, false)
}

fn walk_with_trace(reads: &mut Reads<'_>, tracing: bool) -> PyResult<Form> {
    let budget = reads.budget();
    let mut fields = BudgetVec::new(budget);
    let (leaves, trace) = terminal_fields(reads, tracing)?;
    for (name, id, field_type, flags, value, trace_at) in leaves {
        fields
            .try_push(Field {
                name,
                id,
                kind: kind(&field_type, flags),
                flags,
                value,
                widgets: field_widgets(reads, id)?,
                trace: trace_at,
            })
            .map_err(|e| limit_error(&e))?;
    }
    let mut objects = BudgetVec::new(budget);
    let mut owners = BudgetVec::new(budget);
    for (at, field) in fields.iter().enumerate() {
        budget.poll().map_err(|e| limit_error(&e))?;
        objects
            .try_push((field.id, at))
            .map_err(|e| limit_error(&e))?;
        for &widget in &field.widgets {
            owners.try_push((widget, at)).map_err(|e| limit_error(&e))?;
        }
    }
    objects.sort_unstable();
    owners.sort_unstable();
    let mut owners = owners.into_vec();
    owners.dedup_by_key(|&mut (widget, _)| widget);
    Ok(Form {
        fields: fields.into_vec(),
        objects: objects.into_vec(),
        owners,
        trace,
    })
}

/// Returns the version that reads see.
fn view(document: &_Document) -> PyResult<(Arc<Version>, Budget)> {
    Ok((document.reading_parent()?.0, document.budget()))
}

/// Returns the form of the version that reads see, memoized.
fn memoized_at(
    document: &_Document,
    version: &Arc<Version>,
    budget: &Budget,
) -> PyResult<Arc<Form>> {
    budget.poll().map_err(|e| limit_error(&e))?;
    let parent = if document.in_transaction() {
        Some(document.document()?.current())
    } else {
        None
    };
    let (memo, version) = match parent.as_ref() {
        Some(parent)
            if Arc::ptr_eq(parent, version)
                || parent
                    .changes(version, budget)
                    .map_err(|e| store_error(&e))?
                    .is_some_and(|changes| changes.is_empty()) =>
        {
            (&document.form, parent)
        }
        Some(_) => (&document.draft_form, version),
        None => (&document.form, version),
    };
    memo.get_or_compute(&(), version, budget, |reads| walk(reads).map_err(Failed))
        .map_err(|Failed(error)| error)
}

fn memoized(document: &_Document) -> PyResult<Arc<Form>> {
    let (version, budget) = view(document)?;
    memoized_at(document, &version, &budget)
}

/// Returns the full name and object of every terminal field, by name and
/// then object.
#[pyfunction]
pub fn form_fields(py: Python<'_>, document: &_Document) -> PyResult<Vec<(String, Id)>> {
    py.detach(|| {
        let (version, budget) = view(document)?;
        let form = memoized_at(document, &version, &budget)?;
        let mut out = BudgetVec::new(&budget);
        for field in &form.fields {
            budget
                .charge(LimitCode::MemorySize, field.name.len() as u64)
                .map_err(|e| limit_error(&e))?;
            out.try_push((field.name.clone(), field.id))
                .map_err(|e| limit_error(&e))?;
        }
        Ok(out.into_vec())
    })
}

/// Returns the object of the first terminal field named `name`, or `None`.
#[pyfunction]
pub fn form_field(py: Python<'_>, document: &_Document, name: &str) -> PyResult<Option<Id>> {
    let form = py.detach(|| memoized(document))?;
    let at = form
        .fields
        .partition_point(|field| field.name.as_str() < name);
    Ok(form
        .fields
        .get(at)
        .filter(|field| field.name == name)
        .map(|field| field.id))
}

/// Reads field `id` as a handle does: its full name, kind, inherited `/Ff`,
/// and value. Raises `StaleObjectError` once it is no terminal field.
#[pyfunction]
pub fn form_field_read(
    py: Python<'_>,
    document: &_Document,
    id: Id,
) -> PyResult<(String, &'static str, i64, Option<String>)> {
    py.detach(|| {
        let (version, budget) = view(document)?;
        let form = memoized_at(document, &version, &budget)?;
        let field = form.field(id).ok_or_else(|| stale(id))?;
        budget
            .charge(
                LimitCode::MemorySize,
                field
                    .name
                    .len()
                    .saturating_add(field.value.as_ref().map_or(0, |value| value.len()))
                    as u64,
            )
            .map_err(|e| limit_error(&e))?;
        Ok((
            field.name.clone(),
            field.kind,
            field.flags,
            field.value.as_deref().map(str::to_owned),
        ))
    })
}

/// Returns entry `key` of field `id` or of the nearest field above it that
/// has it, along at most [`MAX_PARENTS`] fields of the `/Parent` chain.
fn inherited(reads: &mut Reads<'_>, id: Id, key: &str) -> PyResult<Option<Value>> {
    let mut current = Some(id);
    let mut visited = HashSet::new();
    while let Some(id) = current {
        let Some((actual, value)) = object_location(reads, Reference::new(id.0, id.1))? else {
            return Ok(None);
        };
        let Value::Dict(dict) = &*value else {
            return Ok(None);
        };
        if !insert_set(&mut visited, actual, reads.budget())? {
            return Ok(None);
        }
        if visited.len() > MAX_PARENTS {
            return Err(limit(format!(
                "AcroForm parent chain exceeds the {MAX_PARENTS}-level safety limit"
            )));
        }
        if let Some(value) = own(reads, dict, key)? {
            return copy_for_edit(value, reads.budget())
                .map(Some)
                .map_err(|e| store_error(&e));
        }
        current = match dict.get("Parent") {
            Some(Value::Reference(parent)) => Some(self::id(*parent)),
            _ => None,
        };
    }
    Ok(None)
}

/// Returns the options of choice field `id` in `/Opt` order: each export
/// value and label. An option is a text string, both its export value and
/// its label, or an array of the two; other entries are skipped.
#[pyfunction]
pub fn form_field_choices(
    py: Python<'_>,
    document: &_Document,
    id: Id,
) -> PyResult<Vec<(String, String)>> {
    py.detach(|| {
        let (version, budget) = view(document)?;
        memoized_at(document, &version, &budget)?
            .field(id)
            .ok_or_else(|| stale(id))?;
        let mut reads = Reads::new(&version, &budget);
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
        let mut choices = BudgetVec::new(&budget);
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
                        budget
                            .charge(LimitCode::MemorySize, value.len() as u64)
                            .map_err(|e| limit_error(&e))?;
                        (value.clone(), value)
                    }
                    None => continue,
                },
            };
            choices.try_push(choice).map_err(|e| limit_error(&e))?;
        }
        Ok(choices.into_vec())
    })
}

/// Returns where the widgets of field `id` appear, in page order: the page
/// object, the widget's object, and its position in the page's `/Annots`.
#[pyfunction]
pub fn form_field_widgets(
    py: Python<'_>,
    document: &_Document,
    id: Id,
) -> PyResult<Vec<(Id, Id, u32)>> {
    py.detach(|| {
        let (version, index, budget) = index(document)?;
        let form = memoized_at(document, &version, &budget)?;
        let field = form.field(id).ok_or_else(|| stale(id))?;
        let mut widgets = HashSet::new();
        for &widget in &field.widgets {
            insert_set(&mut widgets, widget, &budget)?;
        }
        let mut reads = Reads::new(&version, &budget);
        let mut out = BudgetVec::new(&budget);
        for page in index.pages() {
            budget.poll().map_err(|e| limit_error(&e))?;
            let Some(object) = page.object() else {
                continue;
            };
            let Some(Value::Array(items)) = read(&mut reads, page.dict().get("Annots"))? else {
                continue;
            };
            budget
                .check(LimitCode::AnnotationCount, items.len() as u64)
                .map_err(|e| limit_error(&e))?;
            for (at, item) in items.iter().enumerate() {
                budget.poll().map_err(|e| limit_error(&e))?;
                if let Value::Reference(reference) = item
                    && let Some((actual, _)) = object_location(&mut reads, *reference)?
                    && widgets.contains(&actual)
                {
                    let at = u32::try_from(at).unwrap_or(u32::MAX);
                    out.try_push((self::id(object), actual, at))
                        .map_err(|e| limit_error(&e))?;
                }
            }
        }
        Ok(out.into_vec())
    })
}

/// A widget as a handle reads it: its position in the page's `/Annots`;
/// its object, `(0, 0)` for a dictionary written there; the terminal field
/// that owns it; its display rectangle; its `/F` flags; the states of its
/// normal appearance; and its `/AS` state.
type WidgetRecord = (u32, Id, Option<Id>, Rect, i64, Vec<String>, Option<String>);

/// Returns the record of annotation `dict`, or `None` when it is no widget
/// with a rectangle. `totals` holds the encoded and returned bytes of the
/// states that the read returned.
fn widget(
    annots: &Annots,
    form: &Form,
    index: usize,
    id: Id,
    dict: &Dict,
    totals: &mut (usize, usize),
    budget: &Budget,
) -> PyResult<Option<WidgetRecord>> {
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
    let mut state = |name: &Name| -> PyResult<String> {
        let name = name.as_bytes();
        charge(
            &mut totals.0,
            name.len(),
            MAX_STATE_BYTES,
            "encoded widget-state",
        )?;
        budget
            .charge(LimitCode::MemorySize, name.len().saturating_mul(3) as u64)
            .map_err(|e| limit_error(&e))?;
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
            states.try_push(state(name)?).map_err(|e| limit_error(&e))?;
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

/// Lists the widgets of page object `(number, generation)` in `/Annots`
/// order, or returns `None` for a page that is gone.
#[pyfunction]
pub fn widget_list(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
) -> PyResult<Option<Vec<WidgetRecord>>> {
    py.detach(|| {
        let (version, page_index, budget) = crate::pages::index(document)?;
        let Some(annots) = Annots::read(
            Arc::clone(&version),
            page_index,
            &budget,
            number,
            generation,
        )?
        else {
            return Ok(None);
        };
        let form = memoized_at(document, &version, &budget)?;
        let mut records = BudgetVec::new(&budget);
        let mut reads = Reads::new(&version, &budget);
        let mut totals = (0, 0);
        for (index, item) in annots.items.iter().enumerate() {
            let Some((id, dict)) = annots.entry(item)? else {
                continue;
            };
            let id = if id == (0, 0) {
                id
            } else {
                let Some((actual, _)) = object_location(&mut reads, Reference::new(id.0, id.1))?
                else {
                    continue;
                };
                actual
            };
            if let Some(record) = widget(&annots, &form, index, id, &dict, &mut totals, &budget)? {
                records.try_push(record).map_err(|e| limit_error(&e))?;
            }
        }
        Ok(Some(records.into_vec()))
    })
}

/// Locate the same widget after its annotation reference changes through an alias.
fn find_widget(
    annots: &Annots,
    index: u32,
    id: Id,
    reads: &mut Reads<'_>,
) -> PyResult<(usize, Dict)> {
    if id == (0, 0) {
        return annots.find(index, id);
    }
    let matches = |item: &Value, reads: &mut Reads<'_>| -> PyResult<bool> {
        let Value::Reference(reference) = item else {
            return Ok(false);
        };
        Ok(object_location(reads, *reference)?.is_some_and(|(actual, _)| actual == id))
    };
    let hint = index as usize;
    let mut position = None;
    if let Some(item) = annots.items.get(hint)
        && matches(item, reads)?
    {
        position = Some(hint);
    }
    if position.is_none() {
        for (at, item) in annots.items.iter().enumerate() {
            reads.budget().poll().map_err(|e| limit_error(&e))?;
            if at != hint && matches(item, reads)? {
                position = Some(at);
                break;
            }
        }
    }
    let Some(position) = position else {
        return Err(StaleObjectError::new_err(format!(
            "widget {} is no longer in the page's /Annots",
            id.0
        )));
    };
    match annots.entry(&annots.items.as_slice()[position])? {
        Some((_, dict)) => Ok((position, dict)),
        None => Err(StaleObjectError::new_err(format!(
            "widget {} is no longer a dictionary",
            id.0
        ))),
    }
}

/// Reads the widget that a handle names. Returns `None` for a page that is
/// gone, and raises `StaleObjectError` for a widget that is.
#[pyfunction]
pub fn widget_read(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: Id,
) -> PyResult<Option<WidgetRecord>> {
    py.detach(|| {
        let (version, page_index, budget) = crate::pages::index(document)?;
        let Some(annots) = Annots::read(
            Arc::clone(&version),
            page_index,
            &budget,
            number,
            generation,
        )?
        else {
            return Ok(None);
        };
        let mut reads = Reads::new(&version, &budget);
        let (position, dict) = find_widget(&annots, index, id, &mut reads)?;
        let form = memoized_at(document, &version, &budget)?;
        widget(&annots, &form, position, id, &dict, &mut (0, 0), &budget)?
            .map(Some)
            .ok_or_else(|| {
                StaleObjectError::new_err(format!("annotation {} is no longer a widget", id.0))
            })
    })
}

/// Where the form's `/Fields` array is written: as an object of its own,
/// in the form dictionary's object, or in the catalog's.
enum Holder {
    Array(Id),
    Form(Id),
    Catalog(Id),
}

/// Resolve aliases to the actual object that owns the form or fields array.
fn holder(reads: &mut Reads<'_>) -> PyResult<Option<(Holder, Array)>> {
    let Some(&Value::Reference(root)) = reads.trailer().get("Root") else {
        return Ok(None);
    };
    let Some((Value::Dict(catalog), Some(root))) = located(reads, Some(&Value::Reference(root)))?
    else {
        return Ok(None);
    };
    let Some((Value::Dict(form), form_object)) = located(reads, catalog.get("AcroForm"))? else {
        return Ok(None);
    };
    if matches!(form.get("Type"), Some(Value::Name(kind)) if matches!(kind.as_bytes(), b"Page" | b"Pages" | b"Catalog"))
    {
        return Err(MalformedPdfError::new_err(
            "the form aliases a page, page tree, or catalog",
        ));
    }
    let owner = form_object.map_or(Holder::Catalog(root), Holder::Form);
    Ok(match located(reads, form.get("Fields"))? {
        Some((Value::Array(fields), Some(object))) => Some((Holder::Array(object), fields)),
        Some((Value::Array(fields), None)) => Some((owner, fields)),
        _ => None,
    })
}

/// A radio group to repair: the name its buttons share and their objects.
type Group = (PdfString, Vec<(Reference, Reference)>);

/// Returns the top-level radio buttons that share a name, each a field
/// merged with its widget, by name in `/Fields` order.
fn radio_groups(reads: &mut Reads<'_>, fields: &Array) -> PyResult<Vec<Group>> {
    if fields.len() > MAX_EDGES {
        return Err(edges_error());
    }
    let budget = reads.budget();
    let mut groups: Vec<Group> = Vec::new();
    let mut positions: HashMap<String, usize> = HashMap::new();
    let mut totals = (0, 0);
    let mut seen = HashSet::new();
    for item in fields.iter() {
        budget.poll().map_err(|e| limit_error(&e))?;
        let Value::Reference(reference) = item else {
            continue;
        };
        let Some((actual, value)) = object_location(reads, *reference)? else {
            continue;
        };
        if !insert_set(&mut seen, actual, budget)? {
            continue;
        }
        let actual = Reference::new(actual.0, actual.1);
        let Value::Dict(field) = &*value else {
            continue;
        };
        let is_name = |value: Option<Value>, name: &[u8]| matches!(value, Some(Value::Name(value)) if value.as_bytes() == name);
        let radio = is_name(read(reads, field.get("FT"))?, b"Btn")
            && matches!(read(reads, field.get("Ff"))?, Some(Value::Integer(flags)) if flags & RADIO != 0);
        if !radio
            || !is_name(read(reads, field.get("Subtype"))?, b"Widget")
            || field.get("Kids").is_some()
        {
            continue;
        }
        let Some(Value::String(title)) = read(reads, field.get("T"))? else {
            continue;
        };
        charge(
            &mut totals.0,
            title.as_bytes().len(),
            MAX_NAME_BYTES,
            "encoded field name",
        )?;
        budget
            .charge(
                LimitCode::MemorySize,
                title.as_bytes().len().saturating_mul(4) as u64,
            )
            .map_err(|e| limit_error(&e))?;
        let decoded = text_string(title.as_bytes());
        charge(
            &mut totals.1,
            decoded.len(),
            MAX_NAME_BYTES,
            "decoded field name",
        )?;
        match positions.get(decoded.as_str()) {
            Some(&at) => push(&mut groups[at].1, (*reference, actual), budget)?,
            None => {
                reserve_map(&mut positions, budget)?;
                positions.insert(decoded, groups.len());
                let mut members = Vec::new();
                push(&mut members, (*reference, actual), budget)?;
                push(&mut groups, (title, members), budget)?;
            }
        }
    }
    groups.retain(|(_, members)| members.len() > 1);
    Ok(groups)
}

/// Gathers the top-level radio buttons that share a name, each a field
/// merged with its widget, under one new parent field of that name, which
/// takes the place of the first in `/Fields`; returns how many groups it
/// made. Viewers and the walk otherwise see only one of them.
#[pyfunction]
pub fn form_repair_radio_groups(py: Python<'_>, document: &_Document) -> PyResult<u32> {
    document.edit(py, |transaction, budget| {
        let savepoint = transaction.savepoint();
        let result = repair_radio_groups(transaction, budget);
        if result.is_err() {
            transaction.rollback(savepoint);
        }
        result
    })
}

fn repair_radio_groups(transaction: &mut Transaction, budget: &Budget) -> PyResult<u32> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let before = transaction.snapshot();
    let mut reads = Reads::new(&before, budget);
    let Some((owner, fields)) = holder(&mut reads)? else {
        return Ok(0);
    };
    let groups = radio_groups(&mut reads, &fields)?;
    if groups.is_empty() {
        return Ok(0);
    }
    let mut replaced: HashMap<Reference, Option<Reference>> = HashMap::new();
    for (title, members) in &groups {
        let first = object(&mut reads, id(members[0].1))?;
        let Some(Value::Dict(first)) = first.as_deref() else {
            continue;
        };
        budget
            .charge(
                LimitCode::MemorySize,
                (32 * size_of::<(Name, Value)>()) as u64,
            )
            .map_err(|e| limit_error(&e))?;
        let mut parent = Dict::new();
        parent.insert(Name::from("FT"), Value::Name(Name::from("Btn")));
        budget
            .charge(LimitCode::MemorySize, title.as_bytes().len() as u64)
            .map_err(|e| limit_error(&e))?;
        parent.insert(Name::from("T"), Value::String(title.clone()));
        let mut kids = BudgetVec::new(budget);
        for &(entry, _) in members {
            kids.try_push(Value::Reference(entry))
                .map_err(|e| limit_error(&e))?;
        }
        parent.insert(
            Name::from("Kids"),
            Value::Array(Array::from(kids.into_vec())),
        );
        for key in ["Ff", "TU", "TM", "DV"] {
            if let Some(value) = first.get(key) {
                parent.insert(
                    Name::from(key),
                    copy_for_edit(value, budget).map_err(store)?,
                );
            }
        }
        // The group's value is the first value that one of its buttons
        // holds.
        for &(_, member) in members {
            if let Some(Value::Dict(member)) = object(&mut reads, id(member))?.as_deref()
                && let Some(value) = member.get("V")
                && read(&mut reads, Some(value))?.is_some()
            {
                parent.insert(
                    Name::from("V"),
                    copy_for_edit(value, budget).map_err(store)?,
                );
                break;
            }
        }
        let mut field_actions = Dict::new();
        for &(_, member) in members {
            if let Some(Value::Dict(member)) = object(&mut reads, id(member))?.as_deref()
                && let Some(Value::Dict(mut actions)) = read(&mut reads, member.get("AA"))?
            {
                for trigger in FIELD_TRIGGERS {
                    if field_actions.get(trigger).is_none()
                        && let Some(action) = actions.remove(trigger)
                    {
                        field_actions.insert(Name::from(trigger), action);
                    }
                }
            }
        }
        if !field_actions.is_empty() {
            parent.insert(Name::from("AA"), Value::Dict(field_actions));
        }
        let reference = transaction.allocate().map_err(store)?;
        transaction
            .write(reference.number(), &Value::Dict(parent), budget)
            .map_err(store)?;
        for &(_, member) in members {
            let Some(value) = object(&mut reads, id(member))? else {
                continue;
            };
            let Value::Dict(mut dict) = copy_for_edit(&value, budget).map_err(store)? else {
                continue;
            };
            if matches!(dict.get("Type"), Some(Value::Name(kind)) if matches!(kind.as_bytes(), b"Page" | b"Pages" | b"Catalog"))
            {
                return Err(MalformedPdfError::new_err(
                    "a radio widget aliases a page, page tree, or catalog",
                ));
            }
            for key in RADIO_KEYS {
                dict.remove(key);
            }
            if let Some(Value::Dict(mut actions)) = read(&mut reads, dict.get("AA"))? {
                for trigger in FIELD_TRIGGERS {
                    actions.remove(trigger);
                }
                if actions.is_empty() {
                    dict.remove("AA");
                } else {
                    dict.insert(Name::from("AA"), Value::Dict(actions));
                }
            }
            dict.insert(Name::from("Parent"), Value::Reference(reference));
            transaction
                .write(member.number(), &Value::Dict(dict), budget)
                .map_err(store)?;
            reserve_map(&mut replaced, budget)?;
            replaced.insert(member, (member == members[0].1).then_some(reference));
        }
    }
    let mut changed_fields = BudgetVec::new(budget);
    let mut emitted = HashSet::new();
    for item in fields.iter() {
        let replacement = match item {
            Value::Reference(reference) => {
                let actual = object_location(&mut reads, *reference)?
                    .map(|(id, _)| Reference::new(id.0, id.1));
                match actual.and_then(|actual| {
                    replaced
                        .get(&actual)
                        .map(|replacement| (actual, replacement))
                }) {
                    Some((actual, Some(parent)))
                        if insert_set(&mut emitted, id(actual), budget)? =>
                    {
                        Some(Value::Reference(*parent))
                    }
                    Some(_) => None,
                    None => Some(copy_for_edit(item, budget).map_err(store)?),
                }
            }
            _ => Some(copy_for_edit(item, budget).map_err(store)?),
        };
        if let Some(value) = replacement {
            changed_fields
                .try_push(value)
                .map_err(|e| limit_error(&e))?;
        }
    }
    let fields = changed_fields.into_vec();
    let fields = Value::Array(Array::from(fields));
    let (number, value) = match owner {
        Holder::Array((number, _)) => (number, fields),
        Holder::Form(form) => {
            let Some(value) = object(&mut reads, form)? else {
                return Ok(0);
            };
            let Value::Dict(mut dict) = copy_for_edit(&value, budget).map_err(store)? else {
                return Ok(0);
            };
            dict.insert(Name::from("Fields"), fields);
            (form.0, Value::Dict(dict))
        }
        Holder::Catalog((number, generation)) => {
            let catalog = object(&mut reads, (number, generation))?;
            let Some(catalog) = catalog else {
                return Ok(0);
            };
            let Value::Dict(mut catalog) = copy_for_edit(&catalog, budget).map_err(store)? else {
                return Ok(0);
            };
            let Some(Value::Dict(form)) = catalog.get_mut("AcroForm") else {
                return Ok(0);
            };
            form.insert(Name::from("Fields"), fields);
            (number, Value::Dict(catalog))
        }
    };
    transaction.write(number, &value, budget).map_err(store)?;
    Ok(u32::try_from(groups.len()).unwrap_or(u32::MAX))
}

/// Returns `text` as a text string: ASCII without control characters as it
/// is, and else UTF-16BE, in which the line breaks and other controls that
/// PDFDocEncoding leaves unmapped read back.
fn encode_value(text: &str, budget: &Budget) -> PyResult<Value> {
    budget.poll().map_err(|e| limit_error(&e))?;
    let mut bytes = BudgetVec::new(budget);
    if text.is_ascii() && !text.chars().any(char::is_control) {
        bytes
            .try_extend_from_slice(text.as_bytes())
            .map_err(|e| limit_error(&e))?;
    } else {
        bytes
            .try_extend_from_slice(&[0xFE, 0xFF])
            .map_err(|e| limit_error(&e))?;
        for (index, unit) in text.encode_utf16().enumerate() {
            if index % 1024 == 0 {
                budget.poll().map_err(|e| limit_error(&e))?;
            }
            bytes
                .try_extend_from_slice(&unit.to_be_bytes())
                .map_err(|e| limit_error(&e))?;
        }
    }
    Ok(Value::String(PdfString::literal(bytes.into_vec())))
}

fn widget_error(error: WidgetError) -> PyErr {
    match error {
        WidgetError::Malformed(message) => PdfError::new_err(message.into_owned()),
        WidgetError::Lines => LimitError::new_err(("text_line_count", error.to_string())),
        WidgetError::Store(error) => store_error(&error),
    }
}

/// Returns widget `id` of `version` when it looks like a widget.
fn widget_of(
    version: &Version,
    (number, generation): Id,
    budget: &Budget,
) -> PyResult<Option<Arc<Value>>> {
    let object = version
        .get(number, budget)
        .map_err(|e| store_error(&e))?
        .filter(|object| object.generation() == generation);
    Ok(object
        .map(|object| Arc::clone(object.value()))
        .filter(|value| matches!(&**value, Value::Dict(dict) if looks_like_widget(dict))))
}

/// Returns whether a field of `kind` shows its value in text.
fn shows_text(kind: &str) -> bool {
    matches!(kind, "text" | "combobox" | "listbox")
}

fn is_button(kind: &str) -> bool {
    matches!(kind, "checkbox" | "radio")
}

fn is_winansi(text: &str, budget: &Budget) -> PyResult<bool> {
    for (index, character) in text.chars().enumerate() {
        if index % 1024 == 0 {
            budget.poll().map_err(|e| limit_error(&e))?;
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
#[derive(Clone, FromPyObject)]
pub enum FillValue {
    Flag(bool),
    Text(String),
}

/// Sets the value of field `id` to `value`, and draws its widgets through
/// the appearance settler, with those of the other fields that have no
/// appearance yet: a text or choice field shows its text, and a check box
/// or radio button selects the state that `value` names in each widget
/// that has it, or else in its first widget. `True` names a button's first
/// state that is not `Off`, or `Yes`, and `False` names `Off`. Then sets
/// the form's `/NeedAppearances` when a widget is left without an
/// appearance. Refuses what pylopdf refuses, leaving the document as it
/// was.
///
/// Without `draw`, the field's own widgets are left as they are, for
/// [`form_field_draw_face`] to draw.
#[pyfunction]
#[pyo3(signature = (document, id, value, draw=true))]
pub fn form_field_fill(
    py: Python<'_>,
    document: &_Document,
    id: Id,
    value: FillValue,
    draw: bool,
) -> PyResult<()> {
    if let FillValue::Text(text) = &value
        && text.len() > MAX_INPUT_BYTES
    {
        return Err(LimitError::new_err((
            "form_field_input_size",
            format!("form field value exceeds the {MAX_INPUT_BYTES}-byte UTF-8 limit"),
        )));
    }
    let draft = document.in_transaction();
    let cache = if draft {
        &document.draft_fill_index
    } else {
        &document.fill_index
    };
    document.edit(py, |transaction, budget| {
        let savepoint = transaction.savepoint();
        let result = fill(transaction, id, &value, budget, cache, draft, draw);
        if result.is_err() {
            transaction.rollback(savepoint);
        }
        result
    })
}

fn fill(
    transaction: &mut Transaction,
    id: Id,
    value: &FillValue,
    budget: &Budget,
    cache: &FillCache,
    draft: bool,
    draw: bool,
) -> PyResult<()> {
    let before = base(transaction, draft);
    change(
        transaction,
        id,
        budget,
        cache,
        before,
        |field, _, _| match field.kind {
            kind if shows_text(kind) && !draw => Ok(After::Set(Cow::Borrowed(value))),
            kind if shows_text(kind) || is_button(kind) => Ok(After::Fill(Cow::Borrowed(value))),
            "pushbutton" => Err(PdfError::new_err(
                "pushbutton fields do not have fillable values",
            )),
            "signature" => Err(PdfError::new_err(
                "filling signature fields is not supported (see the pyHanko integration for digital signatures)",
            )),
            _ => Err(PdfError::new_err("unsupported field type")),
        },
    )
}

/// Draws the widgets of text or choice field `id` in embedded `face`, as
/// [`form_field_fill`] draws them in a Standard 14 font, and returns the
/// glyphs that they show with their texts.
///
/// Without `font`, it only collects the glyphs and writes nothing. With
/// `font`, the Type0 font of the face that the appearances name and the
/// text that its ToUnicode CMap maps each glyph to, it writes the
/// appearances and then sets the form's `/NeedAppearances` while a widget
/// is left without an appearance. Refuses a character that the face has no
/// glyph for and what the fill refuses, leaving the document as it was.
#[pyfunction]
#[pyo3(signature = (document, id, face, font=None))]
pub fn form_field_draw_face(
    py: Python<'_>,
    document: &_Document,
    id: Id,
    face: &_Font,
    font: Option<(u32, HashMap<u32, String>)>,
) -> PyResult<Vec<(u32, String)>> {
    let Some(face) = face.embedded() else {
        return Err(PdfError::new_err(
            "form appearances draw an OpenType face, not a Standard 14 font",
        ));
    };
    let draft = document.in_transaction();
    let cache = if draft {
        &document.draft_fill_index
    } else {
        &document.fill_index
    };
    document.edit(py, |transaction, budget| {
        let savepoint = transaction.savepoint();
        let result = draw_face(transaction, id, face, font.as_ref(), budget, cache, draft);
        if result.is_err() {
            transaction.rollback(savepoint);
        }
        result
    })
}

fn draw_face(
    transaction: &mut Transaction,
    id: Id,
    face: &Face,
    font: Option<&(u32, HashMap<u32, String>)>,
    budget: &Budget,
    cache: &FillCache,
    draft: bool,
) -> PyResult<Vec<(u32, String)>> {
    let store = |e: pyropdf_store::Error| store_error(&e);
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
                .get(*number, budget)
                .map_err(store)?
                .map_or(0, |object| object.generation());
            Some(FaceFont {
                font: Reference::new(*number, generation),
                mapped,
            })
        }
        None => None,
    };
    let writing = font.is_some();
    let mut text = FaceText::new(face, font, budget).map_err(widget_error)?;
    for &widget in &field.widgets {
        let version = transaction.version();
        let reader = Reader { version, budget };
        let Some(dict) = widget_of(version, widget, budget)? else {
            continue;
        };
        let Value::Dict(dict) = &*dict else {
            continue;
        };
        let drawing = widgets::face_appearance(&reader, dict, &mut text).map_err(widget_error)?;
        if writing && let Some(drawing) = drawing {
            widgets::write(transaction, widget.0, dict, drawing, budget).map_err(store)?;
        }
    }
    if writing {
        let after = transaction.snapshot();
        let changes = before
            .changes(&after, budget)
            .map_err(store)?
            .ok_or_else(|| PdfError::new_err("form versions have different origins"))?;
        let mut appearances = index.appearances.clone();
        appearances.refresh(&after, changes.objects(), changes.trailer(), budget)?;
        let complete = appearances.missing.iter().next().is_none();
        set_need_appearances(transaction, !complete, budget)?;
        transaction.retain_derived_version(Arc::clone(&before));
    }
    Ok(text.into_drawn())
}

/// Returns the version that a form edit starts from: the open
/// transaction's draft, or else the committed version, whose fill index
/// the document keeps.
fn base(transaction: &Transaction, draft: bool) -> Arc<Version> {
    if draft {
        transaction.snapshot()
    } else {
        Arc::clone(transaction.parent())
    }
}

/// What follows an edit of a field's dictionary.
enum After<'v> {
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
/// says what follows. A value is then set as [`form_field_fill`] sets it,
/// and the widgets of the field are drawn or refused with the reason; those
/// of other fields that have no appearance are drawn where they can be.
fn change<'v>(
    transaction: &mut Transaction,
    id: Id,
    budget: &Budget,
    cache: &FillCache,
    before: Arc<Version>,
    edit: impl FnOnce(&Field, &mut Reads<'_>, &mut Dict) -> PyResult<After<'v>>,
) -> PyResult<()> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let index = cache.get(&before, budget)?;
    let mut reads = Reads::new(&before, budget);
    let form = &index.topology.form;
    let field = form.field(id).ok_or_else(|| stale(id))?;
    let Some(original) = object(&mut reads, id)? else {
        return Err(stale(id));
    };
    let Value::Dict(mut dict) = copy_for_edit(&original, budget).map_err(store)? else {
        return Err(stale(id));
    };
    let after = edit(field, &mut reads, &mut dict)?;
    let value = match &after {
        After::Keep => {
            transaction
                .write(id.0, &Value::Dict(dict), budget)
                .map_err(store)?;
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
            dict.insert(Name::from("V"), Value::Name(Name::from(text)));
        }
        Some(text) => {
            dict.insert(Name::from("V"), encode_value(text, budget)?);
        }
        None => {}
    }
    transaction
        .write(id.0, &Value::Dict(dict), budget)
        .map_err(store)?;
    if button && let Some(text) = text {
        select(transaction, &states, text, budget)?;
    }
    let after_write = transaction.snapshot();
    let changes = before
        .changes(&after_write, budget)
        .map_err(store)?
        .ok_or_else(|| PdfError::new_err("form versions have different origins"))?;
    let mut appearances = index.appearances.clone();
    appearances.refresh(&after_write, changes.objects(), changes.trailer(), budget)?;
    // The widgets of the field are drawn or refused with the reason; those
    // of other fields that have no appearance are drawn where they can be.
    let mut marks = BudgetVec::new(budget);
    let mut buttons = BudgetVec::new(budget);
    {
        let version = transaction.version();
        let reader = Reader { version, budget };
        for &widget in field.widgets.iter().filter(|_| own) {
            if let Some(dict) = widget_of(version, widget, budget)?
                && let Value::Dict(dict) = &*dict
            {
                widgets::appearance(&reader, dict).map_err(widget_error)?;
                marks.try_push(widget.0).map_err(|e| limit_error(&e))?;
            }
        }
        for (_, missing) in appearances.missing.iter() {
            let Some(other) = form.field(missing.field) else {
                return Err(PdfError::new_err("appearance index names a missing field"));
            };
            if other.name == field.name {
                continue;
            }
            if is_button(other.kind) {
                buttons.try_push(other.id).map_err(|e| limit_error(&e))?;
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
                && !widgets::has_normal_stream(&reader, dict).map_err(store)?
            {
                marks.try_push(widget.0).map_err(|e| limit_error(&e))?;
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
            return Err(PdfError::new_err("appearance index names a missing field"));
        };
        let states = button_states(transaction.version(), other, budget)?;
        let value = indexed_value(&index, form, other.id)?.unwrap_or(widgets::OFF);
        plan(&states, value).map_err(limit)?;
        for (widget, state) in select(transaction, &states, value, budget)? {
            let version = transaction.version();
            let reader = Reader { version, budget };
            if let Some(dict) = widget_of(version, widget, budget)?
                && let Value::Dict(dict) = &*dict
                && !(widgets::has_state(&reader, dict, widgets::OFF.as_bytes()).map_err(store)?
                    && widgets::has_state(&reader, dict, state.as_bytes()).map_err(store)?)
            {
                marks.try_push(widget.0).map_err(|e| limit_error(&e))?;
            }
        }
    }
    for number in marks.into_vec() {
        transaction
            .mark(Structure::Appearance, number, budget)
            .map_err(store)?;
    }
    transaction
        .settle(Scope::One(Structure::Appearance), budget)
        .map_err(store)?;
    let after_settle = transaction.snapshot();
    let changes = after_write
        .changes(&after_settle, budget)
        .map_err(store)?
        .ok_or_else(|| PdfError::new_err("form versions have different origins"))?;
    appearances.refresh(&after_settle, changes.objects(), changes.trailer(), budget)?;
    let complete = appearances.missing.iter().next().is_none();
    set_need_appearances(transaction, !complete, budget)?;
    transaction.retain_derived_version(Arc::clone(&before));
    Ok(())
}

/// Returns the value that the index records for field `id` of `form`.
fn indexed_value<'i>(index: &'i FillIndex, form: &Form, id: Id) -> PyResult<Option<&'i str>> {
    let Ok(at) = form.objects.binary_search_by_key(&id, |&(id, _)| id) else {
        return Err(PdfError::new_err("appearance index names a missing field"));
    };
    let key = u32::try_from(form.objects[at].1).map_err(|_| edges_error())?;
    let Some(value) = index.values.leaves.get(key) else {
        return Err(PdfError::new_err("value index names a missing field"));
    };
    Ok(value.as_deref())
}

/// Returns the appearance states of each widget of button `field`, charged
/// to `budget`, refusing more than 8,192 entries or 1 MiB of names in all.
fn button_states(version: &Version, field: &Field, budget: &Budget) -> PyResult<States> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let reader = Reader { version, budget };
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
                budget
                    .charge(LimitCode::MemorySize, name.as_bytes().len() as u64)
                    .map_err(|e| limit_error(&e))?;
                names.try_push(name.clone()).map_err(|e| limit_error(&e))?;
            }
            Ok(names.into_vec())
        })
        .map_err(store)?
        .transpose()?
        .unwrap_or_default();
        states
            .try_push((widget, names))
            .map_err(|e| limit_error(&e))?;
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
fn on_state(states: &States, budget: &Budget) -> PyResult<Option<String>> {
    let mut seen = HashSet::new();
    let mut first = None;
    for name in states.iter().flat_map(|(_, names)| names) {
        budget.poll().map_err(|e| limit_error(&e))?;
        if seen.contains(name) {
            continue;
        }
        if seen.len() == MAX_BUTTON_NAMES {
            return Err(limit(format!(
                "AcroForm button states exceed the {MAX_BUTTON_NAMES}-name safety limit"
            )));
        }
        budget
            .charge(LimitCode::MemorySize, (64 + 2 * size_of::<&Name>()) as u64)
            .map_err(|e| limit_error(&e))?;
        seen.try_reserve(1)
            .map_err(|_| limit_error(&pyropdf_store::LimitExceeded::new(LimitCode::MemorySize)))?;
        seen.insert(name);
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
) -> PyResult<Vec<(Id, &'s str)>> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let mut chosen = BudgetVec::new(budget);
    for (widget, state) in selected(states, value) {
        if let Some(original) = widget_of(transaction.version(), widget, budget)?
            && let Value::Dict(dict) = &*original
            && !matches!(dict.get("AS"), Some(Value::Name(current)) if current.as_bytes() == state.as_bytes())
        {
            let Value::Dict(mut dict) = copy_for_edit(&original, budget).map_err(store)? else {
                unreachable!("a copied dictionary remains a dictionary");
            };
            dict.insert(Name::from("AS"), Value::Name(Name::from(state)));
            transaction
                .write(widget.0, &Value::Dict(dict), budget)
                .map_err(store)?;
        }
        chosen
            .try_push((widget, state))
            .map_err(|e| limit_error(&e))?;
    }
    Ok(chosen.into_vec())
}

/// Sets the form's `/NeedAppearances` to `value` where the form dictionary
/// is written: as an object of its own, or in the catalog's. Returns
/// whether the document has a form dictionary.
fn set_need_appearances(
    transaction: &mut Transaction,
    value: bool,
    budget: &Budget,
) -> PyResult<bool> {
    let store = |e: pyropdf_store::Error| store_error(&e);
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
                let Value::Dict(mut form) = copy_for_edit(form, budget).map_err(store)? else {
                    unreachable!("a copied dictionary remains a dictionary");
                };
                form.insert(Name::from("NeedAppearances"), Value::Boolean(value));
                (reference.number(), Value::Dict(form))
            }
            _ => return Ok(false),
        },
        Some(Value::Dict(form)) => {
            if form.get("NeedAppearances") == Some(&Value::Boolean(value)) {
                return Ok(true);
            }
            let Value::Dict(mut catalog) = copy_for_edit(&catalog_value, budget).map_err(store)?
            else {
                unreachable!("a copied dictionary remains a dictionary");
            };
            let Some(Value::Dict(form)) = catalog.get_mut("AcroForm") else {
                unreachable!("the copied catalog retains its form dictionary");
            };
            form.insert(Name::from("NeedAppearances"), Value::Boolean(value));
            (root.number(), Value::Dict(catalog))
        }
        _ => return Ok(false),
    };
    transaction.write(number, &value, budget).map_err(store)?;
    Ok(true)
}

/// Sets the form's `/NeedAppearances`, which asks viewers to draw the
/// widgets again; a document without a form dictionary refuses.
#[pyfunction]
pub fn form_set_need_appearances(
    py: Python<'_>,
    document: &_Document,
    value: bool,
) -> PyResult<()> {
    document.edit(py, |transaction, budget| {
        if set_need_appearances(transaction, value, budget)? {
            Ok(())
        } else {
            Err(PdfError::new_err("the document has no interactive form"))
        }
    })
}

/// Reads field `id`'s label (`/TU`), maximum length (inherited `/MaxLen`,
/// unless negative), and default value (inherited `/DV`, a name or a text
/// string), all from one version.
#[pyfunction]
pub fn form_field_details(
    py: Python<'_>,
    document: &_Document,
    id: Id,
) -> PyResult<(Option<String>, Option<i64>, Option<String>)> {
    py.detach(|| {
        let (version, budget) = view(document)?;
        memoized_at(document, &version, &budget)?
            .field(id)
            .ok_or_else(|| stale(id))?;
        let mut reads = Reads::new(&version, &budget);
        let mut totals = (0, 0);
        let field = object(&mut reads, id)?;
        let label = match field.as_deref() {
            Some(Value::Dict(dict)) => match own(&mut reads, dict, "TU")? {
                Some(label) => text(
                    &mut reads,
                    label,
                    &mut totals,
                    MAX_VALUE_BYTES,
                    "field label",
                )?,
                None => None,
            },
            _ => None,
        };
        let length = inherited(&mut reads, id, "MaxLen")?;
        let max_length = match read(&mut reads, length.as_ref())? {
            Some(Value::Integer(length)) if length >= 0 => Some(length),
            _ => None,
        };
        let default = inherited(&mut reads, id, "DV")?;
        let default = match read(&mut reads, default.as_ref())? {
            Some(Value::Name(name)) => Some(String::from_utf8_lossy(name.as_bytes()).into_owned()),
            Some(Value::String(encoded)) => Some(decode(
                &encoded,
                &mut totals,
                MAX_VALUE_BYTES,
                "field default value",
                &budget,
            )?),
            _ => None,
        };
        Ok((label, max_length, default))
    })
}

/// The `/Ff` bits that decide a field's kind, which an edit keeps: radio,
/// pushbutton, and combo box.
const KIND_FLAGS: i64 = (1 << 15) | (1 << 16) | (1 << 17);

/// The properties that one edit of a field sets, as `Field._edit` passes
/// them, and the names of those that it removes.
#[derive(FromPyObject)]
#[pyo3(from_item_all)]
pub struct FieldEdit {
    #[pyo3(default)]
    label: Option<String>,
    #[pyo3(default)]
    max_length: Option<i64>,
    #[pyo3(default)]
    flags: Option<i64>,
    #[pyo3(default)]
    choices: Option<Vec<(String, String)>>,
    #[pyo3(default)]
    default_value: Option<String>,
    #[pyo3(default)]
    reset: bool,
    clear: Vec<String>,
}

/// Edits field `id`: sets or removes its label, maximum length, and
/// default value, sets its flags and choices, or resets its value to the
/// default value, and draws its widgets again as setting its value does.
/// A reset without a default value removes the value of a text or choice
/// field and turns a button off. Refuses flags that change the field's
/// kind, a maximum length outside a text field, and choices outside a
/// choice field, leaving the document as it was.
#[pyfunction]
pub fn form_field_edit(
    py: Python<'_>,
    document: &_Document,
    id: Id,
    edit: FieldEdit,
) -> PyResult<()> {
    let texts: usize = edit
        .label
        .iter()
        .chain(&edit.default_value)
        .map(String::len)
        .chain(
            edit.choices
                .iter()
                .flatten()
                .map(|(value, label)| value.len() + label.len()),
        )
        .sum();
    if texts > MAX_INPUT_BYTES {
        return Err(LimitError::new_err((
            "form_field_input_size",
            format!("field texts exceed the {MAX_INPUT_BYTES}-byte UTF-8 limit"),
        )));
    }
    if let Some(flags) = edit.flags
        && !(0..=i64::from(u32::MAX)).contains(&flags)
    {
        return Err(PdfError::new_err("field flags are a 32-bit value"));
    }
    if let Some(property) = edit
        .clear
        .iter()
        .find(|property| cleared(property).is_none())
    {
        return Err(PdfError::new_err(format!(
            "a field property that cannot be removed: {property:?}"
        )));
    }
    let draft = document.in_transaction();
    let cache = if draft {
        &document.draft_fill_index
    } else {
        &document.fill_index
    };
    document.edit(py, |transaction, budget| {
        let savepoint = transaction.savepoint();
        let result = edit_field(transaction, id, &edit, budget, cache, draft);
        if result.is_err() {
            transaction.rollback(savepoint);
        }
        result
    })
}

/// Returns the key of a field property that an edit can remove.
fn cleared(property: &str) -> Option<&'static str> {
    match property {
        "label" => Some("TU"),
        "max_length" => Some("MaxLen"),
        "default_value" => Some("DV"),
        _ => None,
    }
}

fn edit_field(
    transaction: &mut Transaction,
    id: Id,
    edit: &FieldEdit,
    budget: &Budget,
    cache: &FillCache,
    draft: bool,
) -> PyResult<()> {
    let before = base(transaction, draft);
    change(
        transaction,
        id,
        budget,
        cache,
        before,
        |field, reads, dict| {
            let buttons = is_button(field.kind);
            if let Some(flags) = edit.flags
                && flags & KIND_FLAGS != field.flags & KIND_FLAGS
            {
                return Err(PdfError::new_err(
                    "the radio, pushbutton, and combo box flags decide a field's kind; create a field of the other kind instead",
                ));
            }
            if let Some(length) = edit.max_length
                && (field.kind != "text" || !(0..=1_000_000).contains(&length))
            {
                return Err(PdfError::new_err(
                    "only a text field has a maximum length, from 0 to 1000000",
                ));
            }
            if let Some(choices) = &edit.choices
                && (!matches!(field.kind, "combobox" | "listbox")
                    || choices.len() > MAX_VALUE_ITEMS)
            {
                return Err(PdfError::new_err(format!(
                    "only a choice field has choices, at most {MAX_VALUE_ITEMS}"
                )));
            }
            // A reset fills the default value that the field had before the edit.
            let default = if edit.reset {
                inherited(reads, id, "DV")?
            } else {
                None
            };
            let default = match read(reads, default.as_ref())? {
                Some(Value::Name(state)) if buttons => Some(FillValue::Text(
                    String::from_utf8_lossy(state.as_bytes()).into_owned(),
                )),
                Some(Value::String(encoded)) => Some(FillValue::Text(decode(
                    &encoded,
                    &mut (0, 0),
                    MAX_VALUE_BYTES,
                    "field default value",
                    budget,
                )?)),
                _ if buttons && edit.reset => Some(FillValue::Flag(false)),
                _ => None,
            };
            if let Some(label) = &edit.label {
                dict.insert(Name::from("TU"), encode_value(label, budget)?);
            }
            if let Some(length) = edit.max_length {
                dict.insert(Name::from("MaxLen"), Value::Integer(length));
            }
            if let Some(flags) = edit.flags {
                dict.insert(Name::from("Ff"), Value::Integer(flags));
            }
            if let Some(value) = &edit.default_value {
                let value = if matches!(field.kind, "checkbox" | "radio" | "pushbutton") {
                    Value::Name(Name::from(value.as_str()))
                } else {
                    encode_value(value, budget)?
                };
                dict.insert(Name::from("DV"), value);
            }
            if let Some(choices) = &edit.choices {
                dict.insert(Name::from("Opt"), options(choices, budget)?);
            }
            for property in &edit.clear {
                if let Some(key) = cleared(property) {
                    dict.remove(key);
                }
            }
            let fillable = buttons || shows_text(field.kind);
            if edit.reset && fillable && default.is_none() {
                dict.remove("V");
            }
            Ok(match default {
                _ if !fillable => After::Keep,
                Some(value) => After::Fill(Cow::Owned(value)),
                None => After::Draw,
            })
        },
    )
}

/// A field to create with its first widget, or a widget to add to the
/// field of that name and kind, as `page.widgets.add` passes a field
/// specification. Colours are RGB from 0 to 1, the border is its width,
/// `/S` style, and dash lengths, `font_name` is a Standard 14 font, and
/// `align` is a `/Q` code.
#[derive(FromPyObject)]
#[pyo3(from_item_all)]
pub struct FieldSpec {
    /// `text`, `checkbox`, `radio`, `combobox`, `listbox`, `pushbutton`, or
    /// `signature`.
    kind: String,
    name: String,
    rect: Rect,
    #[pyo3(default)]
    value: Option<String>,
    #[pyo3(default)]
    default_value: Option<String>,
    #[pyo3(default)]
    flags: i64,
    #[pyo3(default)]
    label: Option<String>,
    #[pyo3(default)]
    max_length: Option<i64>,
    #[pyo3(default)]
    choices: Option<Vec<(String, String)>>,
    #[pyo3(default)]
    on_state: Option<String>,
    #[pyo3(default)]
    caption: Option<String>,
    #[pyo3(default)]
    border: Option<(f64, String, Vec<f64>)>,
    #[pyo3(default)]
    border_color: Option<(f64, f64, f64)>,
    #[pyo3(default)]
    background_color: Option<(f64, f64, f64)>,
    #[pyo3(default)]
    text_color: Option<(f64, f64, f64)>,
    #[pyo3(default)]
    font_name: Option<String>,
    #[pyo3(default)]
    font_size: Option<f64>,
    #[pyo3(default)]
    align: Option<u8>,
}

/// The `/Ff` bit of a radio group that keeps one button on.
const NO_TOGGLE_TO_OFF: i64 = 1 << 14;

/// The field entries that a field merged with its widget gives to the new
/// parent that it gets with a second widget.
const FIELD_KEYS: [&str; 11] = [
    "FT", "T", "TU", "TM", "Ff", "V", "DV", "Opt", "MaxLen", "TI", "I",
];

/// Returns the `/FT` and the `/Ff` bits of a field of `kind`.
fn field_type(kind: &str) -> PyResult<(&'static str, i64)> {
    Ok(match kind {
        "text" => ("Tx", 0),
        "checkbox" => ("Btn", 0),
        // Radio groups keep one button on, as Acrobat creates them.
        "radio" => ("Btn", RADIO | NO_TOGGLE_TO_OFF),
        "pushbutton" => ("Btn", 1 << 16),
        "combobox" => ("Ch", 1 << 17),
        "listbox" => ("Ch", 0),
        "signature" => ("Sig", 0),
        other => return Err(PdfError::new_err(format!("unknown field kind {other:?}"))),
    })
}

impl FieldSpec {
    /// Returns the `/DA` resource name of the specification's Standard 14
    /// font, Helvetica by default.
    fn font_resource(&self) -> PyResult<&'static str> {
        let Some(name) = &self.font_name else {
            return Ok("Helv");
        };
        DA_FONTS
            .iter()
            .find(|(_, font)| font == name)
            .map(|(resource, _)| *resource)
            .ok_or_else(|| PdfError::new_err(format!("unknown Standard 14 font {name:?}")))
    }

    /// Whether the widget draws text in the font of its `/DA`.
    fn shows_font(&self) -> bool {
        shows_text(&self.kind) || self.kind == "pushbutton"
    }

    /// Returns the `/DA` string that names font `resource`.
    fn default_appearance(&self, resource: &str) -> Value {
        let (red, green, blue) = self.text_color.unwrap_or((0.0, 0.0, 0.0));
        let size = self.font_size.unwrap_or(0.0);
        let appearance = format!("/{resource} {size} Tf {red} {green} {blue} rg");
        Value::String(PdfString::literal(appearance.into_bytes()))
    }

    /// Returns the on state of a check box or radio button.
    fn on_state(&self) -> &str {
        self.on_state.as_deref().unwrap_or("Yes")
    }

    /// Checks what the specification holds on its own, before the edit
    /// reads the document.
    fn check(&self) -> PyResult<()> {
        let (_, _) = field_type(&self.kind)?;
        let texts: usize = [
            Some(&self.name),
            self.value.as_ref(),
            self.default_value.as_ref(),
            self.label.as_ref(),
            self.on_state.as_ref(),
            self.caption.as_ref(),
        ]
        .into_iter()
        .flatten()
        .map(String::len)
        .chain(
            self.choices
                .iter()
                .flatten()
                .map(|(value, label)| value.len() + label.len()),
        )
        .sum();
        if texts > MAX_INPUT_BYTES {
            return Err(LimitError::new_err((
                "form_field_input_size",
                format!("field texts exceed the {MAX_INPUT_BYTES}-byte UTF-8 limit"),
            )));
        }
        if self.name.is_empty() {
            return Err(PdfError::new_err("a field has a name"));
        }
        if !(0..=i64::from(u32::MAX)).contains(&self.flags)
            || self.flags & (KIND_FLAGS | NO_TOGGLE_TO_OFF) != 0
        {
            return Err(PdfError::new_err(
                "field flags are a 32-bit value without the bits that the kind sets",
            ));
        }
        let (x0, y0, x1, y1) = self.rect;
        if ![x0, y0, x1, y1].iter().all(|value| value.is_finite()) || x1 <= x0 || y1 <= y0 {
            return Err(PdfError::new_err(
                "a widget rectangle is finite and not empty",
            ));
        }
        if is_button(&self.kind) && matches!(self.on_state(), "" | widgets::OFF) {
            return Err(PdfError::new_err(
                "a button's on state is a name other than Off",
            ));
        }
        if let Some(length) = self.max_length
            && (self.kind != "text" || !(0..=1_000_000).contains(&length))
        {
            return Err(PdfError::new_err(
                "only a text field has a maximum length, from 0 to 1000000",
            ));
        }
        if let Some(choices) = &self.choices {
            if !matches!(self.kind.as_str(), "combobox" | "listbox") {
                return Err(PdfError::new_err("only a choice field has choices"));
            }
            if choices.len() > MAX_VALUE_ITEMS {
                return Err(PdfError::new_err(format!(
                    "a choice field has at most {MAX_VALUE_ITEMS} choices"
                )));
            }
        }
        if shows_text(&self.kind) || self.kind == "pushbutton" {
            rgb(self.text_color.unwrap_or((0.0, 0.0, 0.0)), "text colour")?;
            self.font_resource()?;
            let size = self.font_size.unwrap_or(0.0);
            if !(size.is_finite() && (0.0..=10_000.0).contains(&size)) {
                return Err(PdfError::new_err(
                    "a widget font size is from 0, which fits the text, to 10000",
                ));
            }
            if self.align.is_some_and(|align| align > 2) {
                return Err(PdfError::new_err(
                    "a field aligns left (0), centre (1), or right (2)",
                ));
            }
        }
        Ok(())
    }

    /// Returns the widget dictionary and the field dictionary that the
    /// checked specification makes on the page of `annots`, before either
    /// joins the document.
    fn dictionaries(&self, annots: &Annots<'_>, budget: &Budget) -> PyResult<(Dict, Dict)> {
        let (field_type, kind_flags) = field_type(&self.kind)?;
        let mut widget: Dict = [
            ("Type", Value::Name(Name::from("Annot"))),
            ("Subtype", Value::Name(Name::from("Widget"))),
            ("Rect", annots.to_pdf(self.rect)),
            ("F", Value::Integer(4)),
        ]
        .into_iter()
        .map(|(key, value)| (Name::from(key), value))
        .collect();
        if let Some(page) = annots.page().object() {
            widget.insert(Name::from("P"), Value::Reference(page));
        }
        let mut mark = Dict::new();
        if let Some(color) = self.border_color {
            mark.insert(Name::from("BC"), rgb(color, "border colour")?);
        }
        if let Some(color) = self.background_color {
            mark.insert(Name::from("BG"), rgb(color, "background colour")?);
        }
        if let Some(caption) = &self.caption {
            mark.insert(Name::from("CA"), encode_value(caption, budget)?);
        }
        // A widget on a turned page shows upright.
        if annots.rotation != 0 {
            mark.insert(Name::from("R"), Value::Integer(annots.rotation));
        }
        widget.insert(Name::from("MK"), Value::Dict(mark));
        let solid = (1.0, "S".to_owned(), Vec::new());
        let border = self.border.as_ref().unwrap_or(&solid);
        widget.insert(Name::from("BS"), border_style(border, budget)?);
        if self.shows_font() {
            // `add_widget` sets `/DA` once the font has a name in `/DR`.
            if let Some(align) = self.align {
                widget.insert(Name::from("Q"), Value::Integer(i64::from(align)));
            }
        }
        let mut field: Dict = [
            ("FT", Value::Name(Name::from(field_type))),
            ("T", encode_value(&self.name, budget)?),
        ]
        .into_iter()
        .map(|(key, value)| (Name::from(key), value))
        .collect();
        if kind_flags | self.flags != 0 {
            field.insert(Name::from("Ff"), Value::Integer(kind_flags | self.flags));
        }
        if let Some(label) = &self.label {
            field.insert(Name::from("TU"), encode_value(label, budget)?);
        }
        if let Some(length) = self.max_length {
            field.insert(Name::from("MaxLen"), Value::Integer(length));
        }
        if let Some(choices) = &self.choices {
            field.insert(Name::from("Opt"), options(choices, budget)?);
        }
        if let Some(default) = &self.default_value {
            let default = if field_type == "Btn" {
                Value::Name(Name::from(default.as_str()))
            } else {
                encode_value(default, budget)?
            };
            field.insert(Name::from("DV"), default);
        }
        if shows_text(&self.kind)
            && let Some(value) = &self.value
        {
            field.insert(Name::from("V"), encode_value(value, budget)?);
        }
        Ok((widget, field))
    }
}

/// Returns the `/Opt` array of `choices`: a text string for an option whose
/// label is its export value, else an array of the two.
fn options(choices: &[(String, String)], budget: &Budget) -> PyResult<Value> {
    let mut options = BudgetVec::new(budget);
    for (value, label) in choices {
        let option = if value == label {
            encode_value(value, budget)?
        } else {
            let mut pair = BudgetVec::new(budget);
            pair.try_push(encode_value(value, budget)?)
                .map_err(|e| limit_error(&e))?;
            pair.try_push(encode_value(label, budget)?)
                .map_err(|e| limit_error(&e))?;
            Value::Array(Array::from(pair.into_vec()))
        };
        options.try_push(option).map_err(|e| limit_error(&e))?;
    }
    Ok(Value::Array(Array::from(options.into_vec())))
}

/// Adds a widget that `spec` describes to page object `(number,
/// generation)`: the first widget of a new field, or another widget of the
/// field of that name and kind. Draws the widget through the appearance
/// settler, as setting the field's value does, and returns the widget's
/// object and position in the page's `/Annots`; or `None` for a page that
/// is gone. The page and the form are read after the edit takes the
/// document's writer lock. Refuses a name with dots or one of a field that
/// has fields below it, a kind other than the existing field's, and what
/// pylopdf refuses in a specification, leaving the document as it was.
#[pyfunction]
pub fn widget_add(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    spec: FieldSpec,
) -> PyResult<Option<(Id, u32)>> {
    spec.check()?;
    let draft = document.in_transaction();
    let cache = if draft {
        &document.draft_fill_index
    } else {
        &document.fill_index
    };
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
            let (widget, field) = spec.dictionaries(annots, budget)?;
            let index = u32::try_from(annots.items.len()).unwrap_or(u32::MAX);
            let id = add_widget(
                transaction,
                annots,
                &spec,
                (widget, field),
                budget,
                (cache, draft),
            )?;
            Ok((id, index))
        },
    )
}

fn add_widget(
    transaction: &mut Transaction,
    annots: &Annots<'_>,
    spec: &FieldSpec,
    (mut widget, mut field): (Dict, Dict),
    budget: &Budget,
    (cache, draft): (&FillCache, bool),
) -> PyResult<Id> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let before = base(transaction, draft);
    let index = cache.get(&before, budget)?;
    let form = &index.topology.form;
    let mut existing = None;
    for (at, other) in form.fields.iter().enumerate() {
        if at % 1024 == 0 {
            budget.poll().map_err(|e| limit_error(&e))?;
        }
        if other.name == spec.name {
            existing = Some(other);
        } else if other
            .name
            .strip_prefix(&spec.name)
            .is_some_and(|suffix| suffix.starts_with('.'))
            && existing.is_none()
            && !spec.name.contains('.')
        {
            return Err(PdfError::new_err(
                "the form already has a non-terminal field of that name",
            ));
        }
    }
    match existing {
        Some(existing) if existing.kind != spec.kind => {
            return Err(PdfError::new_err(format!(
                "the form already has a {} field named {:?}",
                existing.kind, spec.name
            )));
        }
        None if spec.name.contains('.') => {
            return Err(PdfError::new_err(
                "a new field's name has no dots; dots join the names of a field's parents",
            ));
        }
        _ => {}
    }
    let existing_value = match existing {
        Some(existing) => indexed_value(&index, form, existing.id)?,
        None => None,
    };
    let holder = form_object(transaction, budget)?;
    if spec.shows_font() {
        let resource = register_font(transaction, holder, spec.font_resource()?, budget)?;
        widget.insert(Name::from("DA"), spec.default_appearance(resource.as_str()));
    }
    let reference = transaction.allocate().map_err(store)?;
    let buttons = is_button(&spec.kind);
    let field_id = match existing {
        Some(existing) => {
            let parent = join(transaction, holder, existing.id, budget)?;
            widget.insert(Name::from("Parent"), Value::Reference(parent));
            edit_array(transaction, parent, "Kids", budget, true, |kids| {
                kids.push(Value::Reference(reference));
            })?;
            id(parent)
        }
        None if spec.kind == "radio" => {
            let parent = transaction.allocate().map_err(store)?;
            let kids = Array::from(vec![Value::Reference(reference)]);
            field.insert(Name::from("Kids"), Value::Array(kids));
            transaction
                .write(parent.number(), &Value::Dict(field), budget)
                .map_err(store)?;
            widget.insert(Name::from("Parent"), Value::Reference(parent));
            edit_array(transaction, holder, "Fields", budget, true, |fields| {
                fields.push(Value::Reference(parent));
            })?;
            id(parent)
        }
        None => {
            for (key, value) in field.iter() {
                widget.insert(key.clone(), value.clone());
            }
            edit_array(transaction, holder, "Fields", budget, true, |fields| {
                fields.push(Value::Reference(reference));
            })?;
            id(reference)
        }
    };
    if buttons {
        // Settling draws the `Off` state and the state that `/AS` names.
        let state = Value::Name(Name::from(spec.on_state()));
        widget.insert(Name::from("AS"), state);
    }
    transaction
        .write(reference.number(), &Value::Dict(widget), budget)
        .map_err(store)?;
    charge_copy(CopyPart::Array(&annots.items), budget)?;
    let mut items = annots.items.clone();
    items.push(Value::Reference(reference));
    annots.write_items(transaction, items, budget)?;
    if buttons {
        transaction
            .mark(Structure::Appearance, reference.number(), budget)
            .map_err(store)?;
        transaction
            .settle(Scope::One(Structure::Appearance), budget)
            .map_err(store)?;
    }
    let value = if buttons {
        let selected = spec.value.as_deref() == Some(spec.on_state());
        Some(match existing_value {
            _ if selected => spec.on_state(),
            Some(value) => value,
            None => widgets::OFF,
        })
    } else if shows_text(&spec.kind) {
        spec.value.as_deref()
    } else {
        None
    };
    let value = match value {
        Some(value) => {
            budget
                .charge(LimitCode::MemorySize, value.len() as u64)
                .map_err(|e| limit_error(&e))?;
            After::Fill(Cow::Owned(FillValue::Text(value.to_owned())))
        }
        None => After::Draw,
    };
    let after = transaction.snapshot();
    change(transaction, field_id, budget, cache, after, |_, _, _| {
        Ok(value)
    })?;
    Ok(id(reference))
}

/// Returns the form dictionary's object, after moving one that the catalog
/// holds into an object of its own, or creating an empty one that names
/// Helvetica as `/Helv`.
fn form_object(transaction: &mut Transaction, budget: &Budget) -> PyResult<Reference> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let version = transaction.snapshot();
    let mut reads = Reads::new(&version, budget);
    let Some(Value::Reference(root)) = reads.trailer().get("Root").cloned() else {
        return Err(PdfError::new_err("the document has no catalog"));
    };
    let Some(catalog) = object(&mut reads, id(root))? else {
        return Err(PdfError::new_err("the document has no catalog"));
    };
    let Value::Dict(catalog) = &*catalog else {
        return Err(PdfError::new_err("the document has no catalog"));
    };
    let form = match catalog.get("AcroForm") {
        Some(Value::Reference(reference)) => {
            if let Some(Value::Dict(_)) = object(&mut reads, id(*reference))?.as_deref() {
                return Ok(*reference);
            }
            None
        }
        Some(form @ Value::Dict(_)) => Some(copy_for_edit(form, budget).map_err(store)?),
        _ => None,
    };
    let form = match form {
        Some(form) => form,
        None => {
            let font = transaction.allocate().map_err(store)?;
            let helvetica: Dict = [
                ("Type", "Font"),
                ("Subtype", "Type1"),
                ("BaseFont", "Helvetica"),
                ("Encoding", "WinAnsiEncoding"),
            ]
            .into_iter()
            .map(|(key, value)| (Name::from(key), Value::Name(Name::from(value))))
            .collect();
            transaction
                .write(font.number(), &Value::Dict(helvetica), budget)
                .map_err(store)?;
            let fonts: Dict = [(Name::from("Helv"), Value::Reference(font))]
                .into_iter()
                .collect();
            let resources: Dict = [(Name::from("Font"), Value::Dict(fonts))]
                .into_iter()
                .collect();
            let appearance = PdfString::literal(b"/Helv 0 Tf 0 g".to_vec());
            Value::Dict(
                [
                    (Name::from("Fields"), Value::Array(Array::new())),
                    (Name::from("DA"), Value::String(appearance)),
                    (Name::from("DR"), Value::Dict(resources)),
                ]
                .into_iter()
                .collect(),
            )
        }
    };
    let Value::Dict(mut catalog) =
        copy_for_edit(&Value::Dict(catalog.clone()), budget).map_err(store)?
    else {
        unreachable!("a copied dictionary remains a dictionary");
    };
    let reference = transaction.allocate().map_err(store)?;
    transaction
        .write(reference.number(), &form, budget)
        .map_err(store)?;
    catalog.insert(Name::from("AcroForm"), Value::Reference(reference));
    transaction
        .write(root.number(), &Value::Dict(catalog), budget)
        .map_err(store)?;
    Ok(reference)
}

/// Returns the name under which form `holder`'s `/DR /Font` holds the
/// Standard 14 font that `/DA` abbreviation `abbreviation` names, adding
/// the font when no entry holds it. The abbreviation itself serves unless
/// it names a different font; then the first of `abbreviation` followed by
/// 1, 2, ... that is free or holds the same font. A `/DR` or `/Font` that
/// is no dictionary is replaced by one.
fn register_font(
    transaction: &mut Transaction,
    holder: Reference,
    abbreviation: &str,
    budget: &Budget,
) -> PyResult<String> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let base_font = pyropdf_engine::standard::da_font(abbreviation);
    let resources = Name::from("DR");
    let font_key = Name::from("Font");
    let mut found = None;
    // Each pass adds what is missing: `/DR`, then its `/Font`.
    for _ in 0..3 {
        let version = transaction.snapshot();
        let mut reads = Reads::new(&version, budget);
        let Some(form) = dict_at(transaction, holder, budget)? else {
            return Err(PdfError::new_err("the interactive form is no dictionary"));
        };
        let Slot::Found(_, Value::Dict(dr)) =
            defaults::slot(&mut reads, &form, b"DR").map_err(store)?
        else {
            edit_dict(transaction, holder, &[], budget, |form| {
                form.insert(resources.clone(), Value::Dict(Dict::new()));
            })?;
            continue;
        };
        match defaults::slot(&mut reads, &dr, b"Font").map_err(store)? {
            Slot::Found(_, Value::Dict(fonts)) => {
                found = Some(fonts);
                break;
            }
            _ => edit_dict(transaction, holder, &[&resources], budget, |dr| {
                dr.insert(font_key.clone(), Value::Dict(Dict::new()));
            })?,
        }
    }
    let Some(fonts) = found else {
        return Err(PdfError::new_err("the form's /DR /Font cannot be edited"));
    };
    let version = transaction.snapshot();
    let mut reads = Reads::new(&version, budget);
    // At most one name per entry is taken, so one of the first
    // `fonts.len() + 1` names is free.
    for suffix in 0..=fonts.len() {
        if suffix % 1024 == 0 {
            budget.poll().map_err(|e| limit_error(&e))?;
        }
        let name = if suffix == 0 {
            abbreviation.to_owned()
        } else {
            format!("{abbreviation}{suffix}")
        };
        match defaults::slot(&mut reads, &fonts, name.as_bytes()).map_err(store)? {
            Slot::Absent => {
                let font = widgets::standard_font(transaction, base_font, budget).map_err(store)?;
                edit_dict(
                    transaction,
                    holder,
                    &[&resources, &font_key],
                    budget,
                    |fonts| {
                        fonts.insert(Name::from(name.as_str()), Value::Reference(font));
                    },
                )?;
                return Ok(name);
            }
            Slot::Found(_, Value::Dict(font))
                if matches!(font.get("BaseFont"),
                    Some(Value::Name(known)) if known.as_bytes() == base_font.as_bytes()) =>
            {
                return Ok(name);
            }
            _ => {}
        }
    }
    Err(PdfError::new_err("the form's /DR /Font has no free name"))
}

/// Returns a copy of the dictionary of object `reference` in the
/// transaction's version, charged to `budget`, or `None` when that object,
/// with that generation, is no dictionary.
fn dict_at(
    transaction: &Transaction,
    reference: Reference,
    budget: &Budget,
) -> PyResult<Option<Dict>> {
    let object = transaction
        .version()
        .get(reference.number(), budget)
        .map_err(|e| store_error(&e))?;
    match object.as_ref() {
        Some(object)
            if object.generation() == reference.generation()
                && matches!(**object.value(), Value::Dict(_)) =>
        {
            match copy_for_edit(object.value(), budget).map_err(|e| store_error(&e))? {
                Value::Dict(dict) => Ok(Some(dict)),
                _ => unreachable!("a copied dictionary remains a dictionary"),
            }
        }
        _ => Ok(None),
    }
}

/// The form defaults that an import of pages brings, planned against the
/// receiving form before the import copies the source values they name.
#[derive(Default)]
pub(crate) struct ImportDefaults {
    /// The source values for the import to copy, one per place.
    pub(crate) values: Vec<Value>,
    /// Where the copy of each value goes.
    places: Vec<Place>,
    /// The renames of the source default resources.
    renames: Renames,
    /// The source form's `/DA`, after the renames.
    appearance: Option<Vec<u8>>,
    /// The source form's `/Q`.
    quadding: i64,
    /// Whether the source form asks for appearances to be drawn again.
    need_appearances: bool,
    /// The receiving form's `/DA`, `/Q`, and `/NeedAppearances`, or `None`
    /// when the import creates the form.
    receiving: Option<(Option<Vec<u8>>, i64, bool)>,
}

impl ImportDefaults {
    /// Whether default appearance strings follow renamed resources, so
    /// that the import must return every object it copies.
    pub(crate) fn renames(&self) -> bool {
        !self.renames.is_empty()
    }
}

/// Where the copy of a source value of [`ImportDefaults`] goes.
enum Place {
    /// An entry of the form: `/DR`, `/DA`, or `/Q` of a new form, or the
    /// `/DR` of a form whose own is no dictionary.
    Form(Name),
    /// A category of the receiving `/DR`, or with a name a resource of it.
    Resource(Name, Option<Name>),
}

/// A form dictionary with the object that holds it, `None` for the
/// catalog.
type Located = (Option<Reference>, Dict);

/// Returns the catalog of `reads` with its reference, and its form.
fn located_form(
    reads: &mut Reads<'_>,
    budget: &Budget,
) -> PyResult<(Reference, Dict, Option<Located>)> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let Some(Value::Reference(root)) = reads.trailer().get("Root").cloned() else {
        return Err(PdfError::new_err("the document has no catalog"));
    };
    let catalog = match object(reads, id(root))?.as_deref() {
        Some(catalog @ Value::Dict(_)) => copy_for_edit(catalog, budget).map_err(store)?,
        _ => return Err(PdfError::new_err("the document has no catalog")),
    };
    let Value::Dict(catalog) = catalog else {
        unreachable!("a copied dictionary remains a dictionary");
    };
    let form = match catalog.get("AcroForm") {
        Some(&Value::Reference(reference)) => match object(reads, id(reference))?.as_deref() {
            Some(form @ Value::Dict(_)) => match copy_for_edit(form, budget).map_err(store)? {
                Value::Dict(form) => Some((Some(reference), form)),
                _ => unreachable!("a copied dictionary remains a dictionary"),
            },
            _ => None,
        },
        Some(form @ Value::Dict(_)) => match copy_for_edit(form, budget).map_err(store)? {
            Value::Dict(form) => Some((None, form)),
            _ => unreachable!("a copied dictionary remains a dictionary"),
        },
        _ => None,
    };
    Ok((root, catalog, form))
}

/// Returns the `/DA`, the `/Q`, and the `/NeedAppearances` of `form`: a
/// `/Q` other than 0, 1, or 2 is 0.
fn form_defaults(reads: &mut Reads<'_>, form: &Dict) -> PyResult<(Option<Vec<u8>>, i64, bool)> {
    let appearance = match read(reads, form.get("DA"))? {
        Some(Value::String(appearance)) => Some(appearance.as_bytes().to_vec()),
        _ => None,
    };
    let quadding = match read(reads, form.get("Q"))? {
        Some(Value::Integer(quadding @ 0..=2)) => quadding,
        _ => 0,
    };
    let need = read(reads, form.get("NeedAppearances"))? == Some(Value::Boolean(true));
    Ok((appearance, quadding, need))
}

/// Plans the form defaults that an import from `source` brings into the
/// form of `receiving`, as pylopdf's `AcroFormImport::prepare` does.
///
/// A new form takes the source `/DR`, `/DA`, and `/Q`. An existing form
/// whose `/DR` is no dictionary takes the source one; otherwise the
/// source resources join it ([`defaults::merge`]), and imported default
/// appearance strings follow their renames.
pub(crate) fn plan_imported_defaults(
    receiving: &Version,
    source: &Version,
    budget: &Budget,
) -> PyResult<ImportDefaults> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let mut plan = ImportDefaults::default();
    let mut sources = Reads::new(source, budget);
    let root = sources.trailer().get("Root").cloned();
    let Some(Value::Dict(catalog)) = read(&mut sources, root.as_ref())? else {
        return Ok(plan);
    };
    let Some(Value::Dict(source_form)) = read(&mut sources, catalog.get("AcroForm"))? else {
        return Ok(plan);
    };
    (plan.appearance, plan.quadding, plan.need_appearances) =
        form_defaults(&mut sources, &source_form)?;
    let mut reads = Reads::new(receiving, budget);
    let Some((_, form)) = located_form(&mut reads, budget)?.2 else {
        for key in ["DR", "DA", "Q"] {
            if let Some(value) = source_form.get(key)
                && !omitted(&mut sources, value)?
            {
                plan.values.push(value.clone());
                plan.places.push(Place::Form(Name::from(key)));
            }
        }
        return Ok(plan);
    };
    plan.receiving = Some(form_defaults(&mut reads, &form)?);
    if let Some(value) = source_form.get("DR")
        && !omitted(&mut sources, value)?
    {
        if let Slot::Found(_, Value::Dict(resources)) =
            defaults::slot(&mut reads, &form, b"DR").map_err(store)?
        {
            if let Some(Value::Dict(source_resources)) = read(&mut sources, Some(value))? {
                let (joined, renames) =
                    defaults::merge(&mut reads, &resources, &mut sources, &source_resources)
                        .map_err(store)?;
                for Joined {
                    category,
                    name,
                    value,
                } in joined
                {
                    plan.values.push(value);
                    plan.places.push(Place::Resource(category, name));
                }
                plan.renames = renames;
            }
        } else {
            plan.values.push(value.clone());
            plan.places.push(Place::Form(Name::from("DR")));
        }
    }
    if let Some(appearance) = &mut plan.appearance
        && let Some(renamed) = defaults::rewrite(appearance, &plan.renames)
    {
        *appearance = renamed;
    }
    Ok(plan)
}

/// Registers `fields`, the copies of the top-level fields that an import
/// of pages brought, in the receiving form, and `order` in its calculation
/// order, as pylopdf's `AcroFormImport` does. A form that is no dictionary
/// gives way to a new one. An imported partial name that a receiving
/// top-level field has takes the smallest `+N` suffix that no top-level
/// field of either has, shared by the imported fields of that name.
///
/// `values` are the copies of the values of `defaults`, and `copies`
/// every object that the import wrote when `defaults` renames resources.
/// A copied default appearance string follows the renames, and a copied
/// top-level field whose `/DA` or `/Q` the form would otherwise give it
/// differently takes the source form's.
pub(crate) fn register_imported(
    transaction: &mut Transaction,
    fields: &[Reference],
    order: &[Reference],
    defaults: &ImportDefaults,
    values: &[Value],
    copies: &[Reference],
    budget: &Budget,
) -> PyResult<()> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    if fields.is_empty() {
        return Ok(());
    }
    let version = transaction.snapshot();
    let mut reads = Reads::new(&version, budget);
    let (root, mut catalog, form) = located_form(&mut reads, budget)?;
    let references = |items: &[Reference]| -> PyResult<Array> {
        let mut out = Vec::new();
        for &item in items {
            push(&mut out, Value::Reference(item), budget)?;
        }
        Ok(Array::from(out))
    };
    let placed = || defaults.places.iter().zip(values);
    let Some((holder, form)) = form else {
        let mut form = Dict::new();
        form.insert(Name::from("Fields"), Value::Array(references(fields)?));
        for (place, value) in placed() {
            if let Place::Form(key) = place {
                form.insert(key.clone(), value.clone());
            }
        }
        if defaults.need_appearances {
            form.insert(Name::from("NeedAppearances"), Value::Boolean(true));
        }
        if !order.is_empty() {
            form.insert(Name::from("CO"), Value::Array(references(order)?));
        }
        let reference = transaction.allocate().map_err(store)?;
        transaction
            .write(reference.number(), &Value::Dict(form), budget)
            .map_err(store)?;
        catalog.insert(Name::from("AcroForm"), Value::Reference(reference));
        transaction
            .write(root.number(), &Value::Dict(catalog), budget)
            .map_err(store)?;
        return Ok(());
    };
    rename_imported(transaction, &mut reads, &form, fields, budget)?;
    let holder = match holder {
        Some(holder) => holder,
        None => {
            let reference = transaction.allocate().map_err(store)?;
            transaction
                .write(reference.number(), &Value::Dict(form), budget)
                .map_err(store)?;
            catalog.insert(Name::from("AcroForm"), Value::Reference(reference));
            transaction
                .write(root.number(), &Value::Dict(catalog), budget)
                .map_err(store)?;
            reference
        }
    };
    let dr = Name::from("DR");
    let placed = placed().collect::<Vec<_>>();
    // The resources of one category join it together.
    for group in placed.chunk_by(|(a, _), (b, _)| match (a, b) {
        (Place::Resource(a, Some(_)), Place::Resource(b, Some(_))) => a == b,
        _ => false,
    }) {
        let (path, entries) = match group {
            [(Place::Form(key), value)] => (vec![], vec![(key, *value)]),
            [(Place::Resource(category, None), value)] => (vec![&dr], vec![(category, *value)]),
            _ => {
                let mut entries = Vec::with_capacity(group.len());
                let mut category = &dr;
                for (place, value) in group {
                    if let Place::Resource(of, Some(name)) = place {
                        category = of;
                        entries.push((name, *value));
                    }
                }
                (vec![&dr, category], entries)
            }
        };
        edit_dict(transaction, holder, &path, budget, |dict| {
            for (key, value) in entries {
                dict.insert(key.clone(), value.clone());
            }
        })?;
    }
    if defaults.renames() {
        rename_appearances(transaction, copies, &defaults.renames, budget)?;
    }
    if let Some((appearance, quadding, need)) = &defaults.receiving {
        let version = transaction.snapshot();
        let mut reads = Reads::new(&version, budget);
        for &field in fields {
            let mut entries = Vec::new();
            if let Some(source) = &defaults.appearance
                && appearance.as_ref() != Some(source)
                && inherited(&mut reads, id(field), "DA")?.is_none()
            {
                entries.push(("DA", Value::String(PdfString::literal(source.clone()))));
            }
            if defaults.quadding != *quadding && inherited(&mut reads, id(field), "Q")?.is_none() {
                entries.push(("Q", Value::Integer(defaults.quadding)));
            }
            if entries.is_empty() {
                continue;
            }
            let Some(mut dict) = dict_at(transaction, field, budget)? else {
                continue;
            };
            for (key, value) in entries {
                dict.insert(Name::from(key), value);
            }
            transaction
                .write(field.number(), &Value::Dict(dict), budget)
                .map_err(store)?;
        }
        if defaults.need_appearances && !need {
            edit_dict(transaction, holder, &[], budget, |form| {
                form.insert(Name::from("NeedAppearances"), Value::Boolean(true));
            })?;
        }
    }
    edit_array(transaction, holder, "Fields", budget, true, |items| {
        for &field in fields {
            items.push(Value::Reference(field));
        }
    })?;
    if !order.is_empty() {
        edit_array(transaction, holder, "CO", budget, true, |items| {
            for &field in order {
                items.push(Value::Reference(field));
            }
        })?;
    }
    Ok(())
}

/// Edits the dictionary that `path` leads to from object `holder`, each
/// key naming a dictionary directly or through references; a path that
/// leads to no dictionary leaves everything as it is.
fn edit_dict(
    transaction: &mut Transaction,
    holder: Reference,
    path: &[&Name],
    budget: &Budget,
    edit: impl FnOnce(&mut Dict),
) -> PyResult<()> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let version = transaction.snapshot();
    let mut reads = Reads::new(&version, budget);
    let Some(mut current) = dict_at(transaction, holder, budget)? else {
        return Ok(());
    };
    // The object that holds the dictionary, and the keys to it from there.
    let mut number = holder;
    let mut direct = Vec::new();
    for &key in path {
        match defaults::slot(&mut reads, &current, key.as_bytes()).map_err(store)? {
            Slot::Found(Some(object), Value::Dict(next)) => {
                let Some(generation) = version.generation(object) else {
                    return Ok(());
                };
                number = Reference::new(object, generation);
                direct.clear();
                current = next;
            }
            Slot::Found(None, Value::Dict(next)) => {
                direct.push(key);
                current = next;
            }
            _ => return Ok(()),
        }
    }
    let Some(mut root) = dict_at(transaction, number, budget)? else {
        return Ok(());
    };
    let mut dict = &mut root;
    for key in direct {
        match dict.get_mut(key.as_bytes()) {
            Some(Value::Dict(next)) => dict = next,
            _ => return Ok(()),
        }
    }
    edit(dict);
    transaction
        .write(number.number(), &Value::Dict(root), budget)
        .map_err(store)?;
    Ok(())
}

/// Renames the resources that the default appearance strings of `copies`
/// name, directly or through a string object, after `renames`; a page,
/// page-tree node, or catalog keeps its own.
fn rename_appearances(
    transaction: &mut Transaction,
    copies: &[Reference],
    renames: &Renames,
    budget: &Budget,
) -> PyResult<()> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let read = |transaction: &Transaction, number: u32| {
        transaction
            .version()
            .get(number, budget)
            .map(|object| object.map(|object| Arc::clone(object.value())))
            .map_err(store)
    };
    let mut strings = HashSet::new();
    for copy in copies {
        let Some(value) = read(transaction, copy.number())? else {
            continue;
        };
        let dict = match &*value {
            Value::Dict(dict) => dict,
            Value::Stream(stream) => stream.dict(),
            _ => continue,
        };
        if let Some(Value::Name(kind)) = dict.get("Type")
            && matches!(kind.as_bytes(), b"Page" | b"Pages" | b"Catalog")
        {
            continue;
        }
        match dict.get("DA") {
            Some(Value::String(appearance)) => {
                let Some(renamed) = defaults::rewrite(appearance.as_bytes(), renames) else {
                    continue;
                };
                let mut value = (*value).clone();
                let dict = match &mut value {
                    Value::Dict(dict) => dict,
                    Value::Stream(stream) => stream.dict_mut(),
                    _ => continue,
                };
                dict.insert(Name::from("DA"), Value::String(PdfString::literal(renamed)));
                transaction
                    .write(copy.number(), &value, budget)
                    .map_err(store)?;
            }
            Some(&Value::Reference(string)) if strings.insert(string.number()) => {
                let object = read(transaction, string.number())?;
                let Some(Value::String(appearance)) = object.as_deref() else {
                    continue;
                };
                if let Some(renamed) = defaults::rewrite(appearance.as_bytes(), renames) {
                    transaction
                        .write(
                            string.number(),
                            &Value::String(PdfString::literal(renamed)),
                            budget,
                        )
                        .map_err(store)?;
                }
            }
            _ => {}
        }
    }
    Ok(())
}

/// Gives each of the imported top-level `fields` whose partial name a
/// top-level field of `form` has the smallest `+N` suffix that no
/// top-level field of either has; see [`register_imported`].
fn rename_imported(
    transaction: &mut Transaction,
    reads: &mut Reads<'_>,
    form: &Dict,
    fields: &[Reference],
    budget: &Budget,
) -> PyResult<()> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let mut receiving = HashSet::new();
    let mut totals = (0, 0);
    if let Some(Value::Array(items)) = read(reads, form.get("Fields"))? {
        for item in items.iter() {
            if let Some(name) = partial_name(reads, item, &mut totals)? {
                insert_name(&mut receiving, name, budget)?;
            }
        }
    }
    let mut imported = Vec::new();
    let mut totals = (0, 0);
    for &field in fields {
        if let Some(name) = partial_name(reads, &Value::Reference(field), &mut totals)? {
            push(&mut imported, (field, name), budget)?;
        }
    }
    if !imported.iter().any(|(_, name)| receiving.contains(name)) {
        return Ok(());
    }
    let mut reserved = HashSet::new();
    for name in receiving
        .iter()
        .chain(imported.iter().map(|(_, name)| name))
    {
        insert_name(&mut reserved, name.clone(), budget)?;
    }
    let mut renamed: HashMap<String, String> = HashMap::new();
    for (field, name) in &imported {
        budget.poll().map_err(|e| limit_error(&e))?;
        if !receiving.contains(name) {
            continue;
        }
        let new_name = match renamed.get(name) {
            Some(new_name) => new_name.clone(),
            None => {
                // Each name taken rules out at most one suffix, so the
                // search ends within the reserved names.
                let mut suffix = 1usize;
                let candidate = loop {
                    budget.poll().map_err(|e| limit_error(&e))?;
                    let candidate = format!("{name}+{suffix}");
                    if !reserved.contains(&candidate) {
                        break candidate;
                    }
                    suffix += 1;
                };
                insert_name(&mut reserved, candidate.clone(), budget)?;
                reserve_map(&mut renamed, budget)?;
                budget
                    .charge(
                        LimitCode::MemorySize,
                        name.len().saturating_add(candidate.len()) as u64,
                    )
                    .map_err(|e| limit_error(&e))?;
                renamed.insert(name.clone(), candidate.clone());
                candidate
            }
        };
        let Some(mut dict) = dict_at(transaction, *field, budget)? else {
            continue;
        };
        let partial = read(reads, dict.get("T"))?;
        let suffix = &new_name[name.len()..];
        dict.insert(
            Name::from("T"),
            renamed_partial(partial.as_ref(), &new_name, suffix),
        );
        transaction
            .write(field.number(), &Value::Dict(dict), budget)
            .map_err(store)?;
    }
    Ok(())
}

/// Adds `name` to `names`, charging the entry and its bytes to `budget`.
fn insert_name(names: &mut HashSet<String>, name: String, budget: &Budget) -> PyResult<()> {
    budget
        .charge(
            LimitCode::MemorySize,
            (64 + 2 * size_of::<String>()).saturating_add(name.len()) as u64,
        )
        .map_err(|e| limit_error(&e))?;
    names
        .try_reserve(1)
        .map_err(|_| limit_error(&pyropdf_store::LimitExceeded::new(LimitCode::MemorySize)))?;
    names.insert(name);
    Ok(())
}

/// Returns the non-empty partial name `/T` of the field that `entry`
/// names, charging it to `totals` under the field-name limit.
fn partial_name(
    reads: &mut Reads<'_>,
    entry: &Value,
    totals: &mut (usize, usize),
) -> PyResult<Option<String>> {
    let &Value::Reference(reference) = entry else {
        return Ok(None);
    };
    let Some(field) = object(reads, id(reference))? else {
        return Ok(None);
    };
    let Value::Dict(field) = &*field else {
        return Ok(None);
    };
    let Some(partial) = field.get("T") else {
        return Ok(None);
    };
    Ok(text(reads, partial, totals, MAX_NAME_BYTES, "field-name")?.filter(|name| !name.is_empty()))
}

/// Returns partial name `partial` with `suffix` appended in its own
/// encoding, UTF-16BE or single bytes, or `renamed` as a new text string
/// when `partial` is no string or odd UTF-16BE.
fn renamed_partial(partial: Option<&Value>, renamed: &str, suffix: &str) -> Value {
    let Some(Value::String(partial)) = partial else {
        return encode_text(renamed);
    };
    let bytes = partial.as_bytes();
    let utf16 = bytes.starts_with(&[0xfe, 0xff]);
    if utf16 && bytes.len() % 2 != 0 {
        return encode_text(renamed);
    }
    let mut encoded = bytes.to_vec();
    for byte in suffix.bytes() {
        if utf16 {
            encoded.push(0);
        }
        encoded.push(byte);
    }
    Value::String(PdfString::new(encoded, partial.form()))
}

/// Edits the array that entry `key` of object `holder` holds: an array
/// object that the entry names, or else the entry itself. An entry that is
/// no array starts empty with `create`, and stays as it is without. The
/// copies are charged to `budget`.
fn edit_array(
    transaction: &mut Transaction,
    holder: Reference,
    key: &str,
    budget: &Budget,
    create: bool,
    edit: impl FnOnce(&mut Array),
) -> PyResult<()> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let Some(mut dict) = dict_at(transaction, holder, budget)? else {
        return Err(PdfError::new_err("a form node is no dictionary"));
    };
    if let Some(Value::Reference(array)) = dict.get(key) {
        let array = *array;
        let object = transaction
            .version()
            .get(array.number(), budget)
            .map_err(store)?;
        if let Some(object) = object.as_ref()
            && object.generation() == array.generation()
            && let Value::Array(items) = &**object.value()
        {
            charge_copy(CopyPart::Array(items), budget)?;
            let mut items = items.clone();
            edit(&mut items);
            transaction
                .write(array.number(), &Value::Array(items), budget)
                .map_err(store)?;
            return Ok(());
        }
    }
    let mut items = match dict.remove(key) {
        Some(Value::Array(items)) => items,
        _ if create => Array::new(),
        _ => return Ok(()),
    };
    edit(&mut items);
    dict.insert(Name::from(key), Value::Array(items));
    transaction
        .write(holder.number(), &Value::Dict(dict), budget)
        .map_err(store)?;
    Ok(())
}

/// Returns the field that takes another widget of field `field`: the field
/// itself, or, for a field merged with its widget, a new parent that takes
/// its field entries and field triggers and its place in the field tree
/// and the calculation order of form `holder`.
fn join(
    transaction: &mut Transaction,
    holder: Reference,
    field: Id,
    budget: &Budget,
) -> PyResult<Reference> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let reference = Reference::new(field.0, field.1);
    let Some(mut kid) = dict_at(transaction, reference, budget)? else {
        return Err(stale(field));
    };
    if !matches!(kid.get("Subtype"), Some(Value::Name(name)) if name.as_bytes() == b"Widget") {
        return Ok(reference);
    }
    let parent = transaction.allocate().map_err(store)?;
    let mut dict: Dict = [(
        Name::from("Kids"),
        Value::Array(Array::from(vec![Value::Reference(reference)])),
    )]
    .into_iter()
    .collect();
    for key in FIELD_KEYS {
        if let Some(value) = kid.remove(key) {
            dict.insert(Name::from(key), value);
        }
    }
    // A merged dictionary holds the actions of both; only the field's
    // triggers move to the parent.
    let actions = match kid.get("AA") {
        Some(Value::Reference(actions)) => dict_at(transaction, *actions, budget)?,
        Some(Value::Dict(actions)) => Some(actions.clone()),
        _ => None,
    };
    if let Some(mut actions) = actions {
        let triggers: Dict = FIELD_TRIGGERS
            .iter()
            .filter_map(|trigger| Some((Name::from(*trigger), actions.remove(trigger)?)))
            .collect();
        if !triggers.is_empty() {
            dict.insert(Name::from("AA"), Value::Dict(triggers));
        }
        if actions.is_empty() {
            kid.remove("AA");
        } else {
            kid.insert(Name::from("AA"), Value::Dict(actions));
        }
    }
    let grand = match kid.get("Parent") {
        Some(Value::Reference(grand)) => Some(*grand),
        _ => None,
    };
    if let Some(grand) = grand {
        dict.insert(Name::from("Parent"), Value::Reference(grand));
    }
    kid.insert(Name::from("Parent"), Value::Reference(parent));
    transaction
        .write(parent.number(), &Value::Dict(dict), budget)
        .map_err(store)?;
    transaction
        .write(field.0, &Value::Dict(kid), budget)
        .map_err(store)?;
    let swap = |items: &mut Array| {
        for item in items.iter_mut() {
            if *item == Value::Reference(reference) {
                *item = Value::Reference(parent);
            }
        }
    };
    let (holder_of, key) = match grand {
        Some(grand) => (grand, "Kids"),
        None => (holder, "Fields"),
    };
    edit_array(transaction, holder_of, key, budget, true, swap)?;
    edit_array(transaction, holder, "CO", budget, false, swap)?;
    Ok(parent)
}

/// Takes the widgets that `handles`, each `(index, object)`, name off page
/// object `(number, generation)` and out of the form, in one edit that
/// reads the page and the form after taking the writer lock. A widget that
/// the form knows leaves every page that lists it and its field's `/Kids`;
/// a field leaves with its last widget, as [`form_field_remove`] removes
/// it. Returns `false` for a page that is gone.
#[pyfunction]
pub fn widget_remove(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    handles: Vec<(u32, Id)>,
) -> PyResult<bool> {
    let draft = document.in_transaction();
    let cache = if draft {
        &document.draft_fill_index
    } else {
        &document.fill_index
    };
    let removed = editing(
        py,
        document,
        number,
        generation,
        |annots, transaction, budget| {
            budget
                .check(LimitCode::AnnotationCount, handles.len() as u64)
                .map_err(|e| limit_error(&e))?;
            let mut positions = BudgetVec::new(budget);
            let mut widgets = BudgetVec::new(budget);
            for &(index, handle) in &handles {
                let (position, dict) = annots.find(index, handle)?;
                if annots.name(&dict, "Subtype")?.as_deref() != Some(b"Widget") {
                    return Err(StaleObjectError::new_err(format!(
                        "annotation {} is no longer a widget",
                        handle.0
                    )));
                }
                if handle == (0, 0) {
                    positions.try_push(position)
                } else {
                    widgets.try_push(handle)
                }
                .map_err(|e| limit_error(&e))?;
            }
            let mut positions = positions.into_vec();
            let mut widgets = widgets.into_vec();
            positions.sort_unstable();
            positions.dedup();
            widgets.sort_unstable();
            widgets.dedup();
            if positions.is_empty() && widgets.is_empty() {
                return Ok(());
            }
            remove_widgets(
                transaction,
                annots,
                &positions,
                &widgets,
                budget,
                (cache, draft),
            )
        },
    )?;
    Ok(removed.is_some())
}

/// Implements [`widget_remove`] in its transaction, for the sorted
/// `widgets` and the sorted `positions` of direct widgets on the page of
/// `annots`.
fn remove_widgets(
    transaction: &mut Transaction,
    annots: &Annots<'_>,
    positions: &[usize],
    widgets: &[Id],
    budget: &Budget,
    (cache, draft): (&FillCache, bool),
) -> PyResult<()> {
    let before = base(transaction, draft);
    let index = cache.get(&before, budget)?;
    let (fields, detached, unlisted) = leaving(&index.topology.form, widgets, budget)?;
    let (version, pages, page) = annots.pages();
    unlist(
        transaction,
        (version, pages),
        &unlisted,
        (page, positions),
        budget,
    )?;
    unfield(transaction, &fields, &detached, budget)
}

/// The fields that leave the form with their last widget, the widgets that
/// only leave their field, and every widget that leaves its pages, sorted.
type Leaving = (Vec<Id>, Vec<Id>, Vec<Id>);

/// Returns what leaves form `form` when the sorted `widgets` leave it.
fn leaving(form: &Form, widgets: &[Id], budget: &Budget) -> PyResult<Leaving> {
    let mut fields = BudgetVec::new(budget);
    let mut seen = HashSet::new();
    let mut detached = BudgetVec::new(budget);
    let mut unlisted = BudgetVec::new(budget);
    for &widget in widgets {
        unlisted.try_push(widget).map_err(|e| limit_error(&e))?;
        let Some(owner) = form.owner(widget) else {
            continue;
        };
        let field = form.field(owner).ok_or_else(|| stale(owner))?;
        let last = owner == widget
            || field
                .widgets
                .iter()
                .all(|kid| widgets.binary_search(kid).is_ok());
        if !last {
            detached.try_push(widget).map_err(|e| limit_error(&e))?;
        } else if insert_set(&mut seen, owner, budget)? {
            fields.try_push(owner).map_err(|e| limit_error(&e))?;
            for &kid in &field.widgets {
                unlisted.try_push(kid).map_err(|e| limit_error(&e))?;
            }
        }
    }
    let mut unlisted = unlisted.into_vec();
    unlisted.sort_unstable();
    unlisted.dedup();
    Ok((fields.into_vec(), detached.into_vec(), unlisted))
}

/// Takes `fields` out of the form, with each named parent that one leaves
/// without kids, and the widgets `detached` out of their fields.
fn unfield(
    transaction: &mut Transaction,
    fields: &[Id],
    detached: &[Id],
    budget: &Budget,
) -> PyResult<()> {
    if fields.is_empty() && detached.is_empty() {
        return Ok(());
    }
    let holder = form_object(transaction, budget)?;
    for &field in fields {
        remove_field(transaction, holder, field, budget)?;
    }
    for &widget in detached {
        let widget = Reference::new(widget.0, widget.1);
        let parent = parent(transaction, widget, budget)?;
        detach(transaction, holder, parent, widget, budget)?;
    }
    Ok(())
}

/// Draws the appearances of the annotations and widgets that `annotations`
/// and `widgets` select into page content and takes them off, in one edit,
/// on page object `page`, or on every page for `None`; returns `false` when
/// `page` is no longer a page. The edit reads the pages after it takes the
/// writer lock, and charges every page to its one budget; a refusal on any
/// page changes nothing. Taking the widgets off every page removes the
/// interactive form; taking them off one page takes them out of their
/// fields, with each field that loses its last widget.
#[pyfunction]
#[pyo3(signature = (document, page, annotations, widgets, /))]
pub fn flatten(
    py: Python<'_>,
    document: &_Document,
    page: Option<Id>,
    annotations: bool,
    widgets: bool,
) -> PyResult<bool> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let draft = document.in_transaction();
    let cache = if draft {
        &document.draft_fill_index
    } else {
        &document.fill_index
    };
    document.edit(py, |transaction, budget| {
        let savepoint = transaction.savepoint();
        let result = (|| {
            let before = base(transaction, draft);
            let version = transaction.snapshot();
            let index =
                PageIndex::memoized(&document.draft_pages, &version, budget).map_err(store)?;
            let targets = match page {
                Some((number, generation)) => {
                    match index.index_of(Reference::new(number, generation)) {
                        Some(at) => at..at + 1,
                        None => return Ok(false),
                    }
                }
                None => 0..index.pages().len(),
            };
            if !annotations && !widgets {
                return Ok(true);
            }
            let selection = Selection {
                annotations,
                widgets,
            };
            // Annotations without an appearance are drawn as a render
            // draws them.
            let mut missing = if annotations {
                let objects = synthesized(&version, &index, budget).map_err(store)?;
                Missing::new(objects, budget).map_err(store)?
            } else {
                Missing::default()
            };
            let mut flattened = BudgetVec::new(budget);
            for at in targets {
                budget.poll().map_err(|e| limit_error(&e))?;
                let annots = Annots::at(Arc::clone(&version), Arc::clone(&index), budget, at)?;
                let Some(plan) =
                    flattening::plan(&annots.reader(), &annots.items, selection).map_err(store)?
                else {
                    continue;
                };
                let Some(object) = annots.page().object() else {
                    return Err(PdfError::new_err("the page is not an indirect object"));
                };
                if page.is_some() {
                    for &widget in &plan.widgets {
                        flattened
                            .try_push(id(widget))
                            .map_err(|e| limit_error(&e))?;
                    }
                }
                flattening::draw(
                    transaction,
                    object.number(),
                    annots.page(),
                    plan,
                    &mut missing,
                    budget,
                )
                .map_err(store)?;
            }
            if !widgets {
            } else if page.is_none() {
                remove_form(transaction, budget)?;
            } else {
                let mut flattened = flattened.into_vec();
                flattened.sort_unstable();
                flattened.dedup();
                let index = cache.get(&before, budget)?;
                let (fields, detached, _) = leaving(&index.topology.form, &flattened, budget)?;
                unfield(transaction, &fields, &detached, budget)?;
            }
            Ok(true)
        })();
        if result.is_err() {
            transaction.rollback(savepoint);
        }
        result
    })
}

/// Takes the interactive form out of the catalog.
fn remove_form(transaction: &mut Transaction, budget: &Budget) -> PyResult<()> {
    let version = transaction.snapshot();
    let mut reads = Reads::new(&version, budget);
    let Some(Value::Reference(root)) = reads.trailer().get("Root").cloned() else {
        return Ok(());
    };
    let Some(original) = object(&mut reads, id(root))? else {
        return Ok(());
    };
    let Value::Dict(mut catalog) = copy_for_edit(&original, budget).map_err(|e| store_error(&e))?
    else {
        return Ok(());
    };
    if catalog.remove("AcroForm").is_none() {
        return Ok(());
    }
    transaction
        .write(root.number(), &Value::Dict(catalog), budget)
        .map_err(|e| store_error(&e))?;
    Ok(())
}

/// Takes field `field` out of the form with its widgets, in one edit that
/// reads the pages and the form after taking the writer lock: its widgets
/// leave every page that lists them, and the field leaves the field tree
/// and the calculation order with each named parent that it leaves without
/// kids.
#[pyfunction]
pub fn form_field_remove(py: Python<'_>, document: &_Document, field: Id) -> PyResult<()> {
    let draft = document.in_transaction();
    let cache = if draft {
        &document.draft_fill_index
    } else {
        &document.fill_index
    };
    document.edit(py, |transaction, budget| {
        let savepoint = transaction.savepoint();
        let result = (|| {
            let before = base(transaction, draft);
            let index = cache.get(&before, budget)?;
            let found = index
                .topology
                .form
                .field(field)
                .ok_or_else(|| stale(field))?;
            let mut widgets = BudgetVec::new(budget);
            for &widget in &found.widgets {
                widgets.try_push(widget).map_err(|e| limit_error(&e))?;
            }
            let mut widgets = widgets.into_vec();
            widgets.sort_unstable();
            widgets.dedup();
            let version = transaction.snapshot();
            let pages = PageIndex::memoized(&document.draft_pages, &version, budget)
                .map_err(|e| store_error(&e))?;
            unlist(transaction, (&version, &pages), &widgets, (0, &[]), budget)?;
            let holder = form_object(transaction, budget)?;
            remove_field(transaction, holder, field, budget)
        })();
        if result.is_err() {
            transaction.rollback(savepoint);
        }
        result
    })
}

/// Takes the sorted `widgets` off every page of `pages`, a version and its
/// page index that the edit has not yet changed, and the direct annotations
/// at the sorted `positions` off its page `page`. A page whose `/Annots`
/// changes has its other direct annotations stored as objects, so that a
/// handle to one that moved goes stale rather than naming another.
fn unlist(
    transaction: &mut Transaction,
    (version, index): (&Arc<Version>, &Arc<PageIndex>),
    widgets: &[Id],
    (page, positions): (usize, &[usize]),
    budget: &Budget,
) -> PyResult<()> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    for at in 0..index.pages().len() {
        let annots = Annots::at(Arc::clone(version), Arc::clone(index), budget, at)?;
        let listed = |position: usize, item: &Value| match item {
            Value::Reference(reference) => widgets.binary_search(&id(*reference)).is_ok(),
            _ => at == page && positions.binary_search(&position).is_ok(),
        };
        if !annots
            .items
            .iter()
            .enumerate()
            .any(|(position, item)| listed(position, item))
        {
            continue;
        }
        let mut items = BudgetVec::new(budget);
        for (position, item) in annots.items.iter().enumerate() {
            if listed(position, item) {
                continue;
            }
            charge_copy(CopyPart::Value(item), budget)?;
            let item = match item {
                Value::Dict(entry) => {
                    let reference = transaction.allocate().map_err(store)?;
                    transaction
                        .write(reference.number(), &Value::Dict(entry.clone()), budget)
                        .map_err(store)?;
                    Value::Reference(reference)
                }
                item => item.clone(),
            };
            items.try_push(item).map_err(|e| limit_error(&e))?;
        }
        annots.write_items(transaction, Array::from(items.into_vec()), budget)?;
    }
    Ok(())
}

/// Returns the `/Parent` reference of object `node`.
fn parent(
    transaction: &Transaction,
    node: Reference,
    budget: &Budget,
) -> PyResult<Option<Reference>> {
    let object = transaction
        .version()
        .get(node.number(), budget)
        .map_err(|e| store_error(&e))?;
    Ok(match object.as_ref() {
        Some(object) if object.generation() == node.generation() => match &**object.value() {
            Value::Dict(dict) => match dict.get("Parent") {
                Some(Value::Reference(parent)) => Some(*parent),
                _ => None,
            },
            _ => None,
        },
        _ => None,
    })
}

/// Takes field `field` out of the field tree of form `holder` and its
/// calculation order, with each named parent that it leaves without kids.
fn remove_field(
    transaction: &mut Transaction,
    holder: Reference,
    field: Id,
    budget: &Budget,
) -> PyResult<()> {
    let mut node = Reference::new(field.0, field.1);
    let mut visited = HashSet::new();
    loop {
        if !visit(&mut visited, id(node), budget)? {
            return Err(PdfError::new_err(
                "the field's parent chain contains a cycle",
            ));
        }
        let parent = parent(transaction, node, budget)?;
        detach(transaction, holder, parent, node, budget)?;
        let Some(parent) = parent else {
            return Ok(());
        };
        let version = transaction.version();
        let object = version
            .get(parent.number(), budget)
            .map_err(|e| store_error(&e))?;
        let Some(object) = object.filter(|object| object.generation() == parent.generation())
        else {
            return Ok(());
        };
        let Value::Dict(dict) = &**object.value() else {
            return Ok(());
        };
        let named = dict.get("T").is_some() || dict.get("FT").is_some();
        if !named || !childless(transaction, dict, budget)? {
            return Ok(());
        }
        node = parent;
    }
}

/// Returns whether the `/Kids` of field `dict` lists no kid.
fn childless(transaction: &Transaction, dict: &Dict, budget: &Budget) -> PyResult<bool> {
    let kids = match dict.get("Kids") {
        Some(Value::Reference(kids)) => *kids,
        Some(Value::Array(kids)) => return Ok(kids.is_empty()),
        _ => return Ok(true),
    };
    let object = transaction
        .version()
        .get(kids.number(), budget)
        .map_err(|e| store_error(&e))?;
    Ok(match object.as_ref() {
        Some(object) if object.generation() == kids.generation() => {
            !matches!(&**object.value(), Value::Array(kids) if !kids.is_empty())
        }
        _ => true,
    })
}

/// Takes `node` out of its parent's `/Kids`, or the form's `/Fields`, and
/// out of the calculation order.
fn detach(
    transaction: &mut Transaction,
    holder: Reference,
    parent: Option<Reference>,
    node: Reference,
    budget: &Budget,
) -> PyResult<()> {
    let gone = |items: &mut Array| items.retain(|item| *item != Value::Reference(node));
    edit_array(transaction, holder, "CO", budget, false, gone)?;
    match parent {
        Some(parent) => edit_array(transaction, parent, "Kids", budget, false, gone),
        None => edit_array(transaction, holder, "Fields", budget, false, gone),
    }
}

/// Returns how many form walks the document's memos reused and how many
/// they computed, for tests.
#[pyfunction]
pub fn form_walk_counts(document: &_Document) -> (u64, u64) {
    let (form, drafts) = (document.form.stats(), document.draft_form.stats());
    (
        form.hits + drafts.hits,
        form.computations + drafts.computations,
    )
}

/// Whether a widget shows a normal text appearance or a selected button
/// appearance. The resolver records every indirect object it examines.
#[derive(Clone, Copy)]
enum AppearanceKind {
    Normal,
    State,
}

/// A resolved value either borrows its container or shares an indirect
/// object. Looking at an appearance never copies its stream data.
enum AppearanceValue<'a> {
    Direct(&'a Value),
    Indirect(Arc<Value>),
}

impl std::ops::Deref for AppearanceValue<'_> {
    type Target = Value;

    fn deref(&self) -> &Value {
        match self {
            Self::Direct(value) => value,
            Self::Indirect(value) => value,
        }
    }
}

fn appearance_value<'a>(
    reads: &mut Reads<'_>,
    value: Option<&'a Value>,
) -> PyResult<Option<AppearanceValue<'a>>> {
    let Some(value) = value else {
        return Ok(None);
    };
    let Value::Reference(reference) = value else {
        return Ok(Some(AppearanceValue::Direct(value)));
    };
    let mut reference = *reference;
    for _ in 0..MAX_CHAIN {
        reads.budget().poll().map_err(|e| limit_error(&e))?;
        let Some(object) = object(reads, id(reference))? else {
            return Ok(None);
        };
        match &*object {
            Value::Reference(next) => reference = *next,
            _ => return Ok(Some(AppearanceValue::Indirect(object))),
        }
    }
    Err(MalformedPdfError::new_err(
        "an annotation appearance reference chain is too long",
    ))
}

/// Returns `None` for a missing or non-widget object, or whether the
/// widget has a nonempty appearance for its field's kind.
fn widget_appearance(
    reads: &mut Reads<'_>,
    widget: Id,
    kind: AppearanceKind,
) -> PyResult<Option<bool>> {
    let Some(widget) = object(reads, widget)? else {
        return Ok(None);
    };
    let Value::Dict(widget) = &*widget else {
        return Ok(None);
    };
    if !looks_like_widget(widget) {
        return Ok(None);
    }
    let state = match kind {
        AppearanceKind::State => match widget.get("AS") {
            Some(Value::Name(state)) => Some(state),
            _ => return Ok(Some(false)),
        },
        AppearanceKind::Normal => None,
    };
    let Some(appearances) = appearance_value(reads, widget.get("AP"))? else {
        return Ok(Some(false));
    };
    let Value::Dict(appearances) = &*appearances else {
        return Ok(Some(false));
    };
    let Some(normal) = appearance_value(reads, appearances.get("N"))? else {
        return Ok(Some(false));
    };
    let selected = match state {
        Some(state) => match &*normal {
            Value::Dict(states) => appearance_value(reads, states.get(state.as_bytes()))?,
            _ => None,
        },
        None => Some(normal),
    };
    Ok(Some(matches!(
        selected.as_deref(),
        Some(Value::Stream(stream)) if !stream.data().is_empty()
    )))
}

/// One field's use of a widget; a shared widget can have several entries.
#[derive(Clone, Copy)]
struct IndexedWidget {
    field: Id,
    widget: Id,
    kind: AppearanceKind,
}

/// Recorded widget appearance results and the reads that produced them.
/// The enclosing form index is responsible for discovering widget membership
/// and comparing versions before asking this index to refresh.
#[derive(Clone)]
struct AppearanceIndex {
    widgets: DerivedMap<IndexedWidget>,
    missing: DerivedMap<IndexedWidget>,
    dependencies: ReadDependencies,
}

impl Default for AppearanceIndex {
    fn default() -> Self {
        Self {
            widgets: DerivedMap::new(),
            missing: DerivedMap::new(),
            dependencies: ReadDependencies::new(),
        }
    }
}

impl AppearanceIndex {
    fn record(
        &mut self,
        key: u32,
        widget: IndexedWidget,
        version: &Version,
        budget: &Budget,
    ) -> PyResult<()> {
        let mut reads = Reads::new(version, budget);
        let present = widget_appearance(&mut reads, widget.widget, widget.kind)?;
        let dependencies = reads.finish().map_err(|e| store_error(&e))?;
        let mut next = self.clone();
        next.dependencies
            .replace(key, Some(dependencies), budget)
            .map_err(|e| store_error(&e))?;
        next.widgets
            .insert(key, widget, budget)
            .map_err(|e| store_error(&e))?;
        if present == Some(false) {
            next.missing
                .insert(key, widget, budget)
                .map_err(|e| store_error(&e))?;
        } else {
            next.missing
                .remove(key, budget)
                .map_err(|e| store_error(&e))?;
        }
        budget.poll().map_err(|e| limit_error(&e))?;
        *self = next;
        Ok(())
    }

    /// Refreshes only entries whose recorded reads intersect the changes.
    /// A failure leaves the whole index unchanged.
    fn refresh(
        &mut self,
        version: &Version,
        changed: &[u32],
        trailer_changed: bool,
        budget: &Budget,
    ) -> PyResult<()> {
        let affected = self
            .dependencies
            .affected(changed, trailer_changed, budget)
            .map_err(|e| store_error(&e))?;
        let mut next = self.clone();
        for &key in affected.iter() {
            budget.poll().map_err(|e| limit_error(&e))?;
            let Some(widget) = next.widgets.get(key).copied() else {
                return Err(MalformedPdfError::new_err(
                    "an appearance dependency names a missing widget",
                ));
            };
            next.record(key, widget, version, budget)?;
        }
        budget.poll().map_err(|e| limit_error(&e))?;
        *self = next;
        Ok(())
    }
}

/// Form membership without field values. Value and appearance changes are
/// checked separately from membership; callers must never use the discarded
/// values from this cached walk.
struct FormTopology {
    form: Form,
    reads: ReadSet,
    nodes: DerivedMap<u16>,
    /// Catalog and form holders can also be malformed field aliases. Their
    /// changes always require another walk.
    protected: [Option<u32>; 2],
}

impl FormTopology {
    fn build(version: &Version, budget: &Budget) -> PyResult<Self> {
        let mut reads = Reads::new(version, budget);
        let mut form = walk_with_trace(&mut reads, true)?;
        let mut nodes = DerivedMap::new();
        for node in &form.trace {
            nodes
                .insert(node.id.0, node.id.1, budget)
                .map_err(|e| store_error(&e))?;
        }
        for field in &mut form.fields {
            field.value = None;
        }
        let root = match reads.trailer().get("Root") {
            Some(Value::Reference(root)) => Some(*root),
            _ => None,
        };
        let mut protected = [root.map(|root| root.number()), None];
        if let Some(root) = root
            && let Some((root_id, catalog)) = object_location(&mut reads, root)?
        {
            protected[0] = Some(root_id.0);
            if let Value::Dict(catalog) = &*catalog
                && let Some(Value::Reference(form)) = catalog.get("AcroForm")
                && let Some((form_id, _)) = object_location(&mut reads, *form)?
            {
                protected[1] = Some(form_id.0);
            }
        }
        Ok(Self {
            form,
            reads: reads.finish().map_err(|e| store_error(&e))?,
            nodes,
            protected,
        })
    }

    /// Whether the recorded membership is still valid. A change to a known
    /// terminal field or widget is harmless only when its structural entries
    /// have the same bounded representation. Unknown roles and changes to a
    /// catalog or form holder conservatively require a fresh walk.
    fn unchanged(&self, old: &Version, new: &Version, budget: &Budget) -> PyResult<bool> {
        let Some(changes) = old.changes(new, budget).map_err(|e| store_error(&e))? else {
            return Ok(false);
        };
        if self.reads.is_disjoint(&changes) {
            return Ok(true);
        }
        if changes.trailer() && self.reads.trailer() {
            return Ok(false);
        }
        for &number in changes.objects() {
            budget.poll().map_err(|e| limit_error(&e))?;
            if self.reads.objects().binary_search(&number).is_err() {
                continue;
            }
            if self.protected.contains(&Some(number)) {
                return Ok(false);
            }
            let Some(before) = old.get(number, budget).map_err(|e| store_error(&e))? else {
                return Ok(false);
            };
            let Some(after) = new.get(number, budget).map_err(|e| store_error(&e))? else {
                return Ok(false);
            };
            if before.generation() != after.generation() {
                return Ok(false);
            }
            let identity = (number, before.generation());
            if self.nodes.get(number) != Some(&identity.1) && self.form.owner(identity).is_none() {
                return Ok(false);
            }
            let (Value::Dict(before), Value::Dict(after)) = (&**before.value(), &**after.value())
            else {
                return Ok(false);
            };
            if !same_field_structure(before, after, budget)? {
                return Ok(false);
            }
        }
        Ok(true)
    }
}

/// Compare the entries that the field-tree walk uses for membership and
/// inherited type. Values and appearances are independently recorded.
fn same_field_structure(before: &Dict, after: &Dict, budget: &Budget) -> PyResult<bool> {
    for key in ["T", "FT", "Ff", "Subtype"] {
        budget.poll().map_err(|e| limit_error(&e))?;
        if !same_structure_atom(before.get(key), after.get(key), budget)? {
            return Ok(false);
        }
    }
    if before.get("Rect").is_some() != after.get("Rect").is_some() {
        return Ok(false);
    }
    match (before.get("Kids"), after.get("Kids")) {
        (Some(Value::Array(left)), Some(Value::Array(right))) => {
            if left.len() != right.len() {
                return Ok(false);
            }
            for (left, right) in left.iter().zip(right.iter()) {
                budget.poll().map_err(|e| limit_error(&e))?;
                if !matches!((left, right), (Value::Reference(a), Value::Reference(b)) if a == b) {
                    return Ok(false);
                }
            }
            Ok(true)
        }
        (left, right) => same_structure_atom(left, right, budget),
    }
}

/// Compare only bounded scalar representations; an unusual direct value
/// conservatively forces a fresh walk instead of recursively comparing it.
fn same_structure_atom(
    left: Option<&Value>,
    right: Option<&Value>,
    budget: &Budget,
) -> PyResult<bool> {
    Ok(match (left, right) {
        (None, None) | (Some(Value::Null), Some(Value::Null)) => true,
        (Some(Value::Reference(a)), Some(Value::Reference(b))) => a == b,
        (Some(Value::Integer(a)), Some(Value::Integer(b))) => a == b,
        (Some(Value::Name(a)), Some(Value::Name(b))) => {
            same_structure_bytes(a.as_bytes(), b.as_bytes(), budget)?
        }
        (Some(Value::String(a)), Some(Value::String(b))) => {
            same_structure_bytes(a.as_bytes(), b.as_bytes(), budget)?
        }
        _ => false,
    })
}

fn same_structure_bytes(left: &[u8], right: &[u8], budget: &Budget) -> PyResult<bool> {
    if left.len() != right.len() {
        return Ok(false);
    }
    for (left, right) in left.chunks(4096).zip(right.chunks(4096)) {
        budget.poll().map_err(|e| limit_error(&e))?;
        if left != right {
            return Ok(false);
        }
    }
    Ok(true)
}

/// Per-node contributions to the original walk's value limits.
#[derive(Clone, Copy, Default)]
struct ValueMetric {
    encoded: usize,
    decoded: usize,
    items: usize,
}

#[derive(Clone, Default)]
struct ValueIndex {
    nodes: DerivedMap<ValueMetric>,
    node_reads: ReadDependencies,
    leaves: DerivedMap<Option<Arc<str>>>,
    leaf_reads: ReadDependencies,
    encoded: usize,
    decoded: usize,
    items: usize,
    returned: usize,
}

impl ValueIndex {
    fn build(topology: &FormTopology, version: &Version, budget: &Budget) -> PyResult<Self> {
        let mut index = Self::default();
        for (at, node) in topology.form.trace.iter().enumerate() {
            budget.poll().map_err(|e| limit_error(&e))?;
            let key = u32::try_from(at).map_err(|_| edges_error())?;
            let (metric, reads) = value_metric(version, node.id, budget)?;
            index.encoded = index.encoded.saturating_add(metric.encoded);
            index.decoded = index.decoded.saturating_add(metric.decoded);
            index.items = index.items.saturating_add(metric.items);
            index
                .nodes
                .insert(key, metric, budget)
                .map_err(|e| store_error(&e))?;
            index
                .node_reads
                .replace(key, Some(reads), budget)
                .map_err(|e| store_error(&e))?;
        }
        for (at, field) in topology.form.fields.iter().enumerate() {
            budget.poll().map_err(|e| limit_error(&e))?;
            let key = u32::try_from(at).map_err(|_| edges_error())?;
            let (value, reads) = traced_value(version, &topology.form.trace, field.trace, budget)?;
            index.returned = index
                .returned
                .saturating_add(value.as_ref().map_or(0, |value| value.len()));
            index
                .leaves
                .insert(key, value, budget)
                .map_err(|e| store_error(&e))?;
            index
                .leaf_reads
                .replace(key, Some(reads), budget)
                .map_err(|e| store_error(&e))?;
        }
        index.check_limits()?;
        Ok(index)
    }

    /// Recomputes only the node and leaf values whose recorded reads changed.
    fn refresh(
        &mut self,
        topology: &FormTopology,
        version: &Version,
        changed: &[u32],
        trailer_changed: bool,
        budget: &Budget,
    ) -> PyResult<()> {
        let nodes = self
            .node_reads
            .affected(changed, trailer_changed, budget)
            .map_err(|e| store_error(&e))?;
        let leaves = self
            .leaf_reads
            .affected(changed, trailer_changed, budget)
            .map_err(|e| store_error(&e))?;
        let mut next = self.clone();
        for &key in nodes.iter() {
            budget.poll().map_err(|e| limit_error(&e))?;
            let Some(old) = next.nodes.get(key).copied() else {
                return Err(MalformedPdfError::new_err(
                    "a value dependency names a missing node",
                ));
            };
            let Some(node) = topology.form.trace.get(key as usize) else {
                return Err(MalformedPdfError::new_err(
                    "a value dependency names a missing trace",
                ));
            };
            let (metric, reads) = value_metric(version, node.id, budget)?;
            next.encoded = next
                .encoded
                .saturating_sub(old.encoded)
                .saturating_add(metric.encoded);
            next.decoded = next
                .decoded
                .saturating_sub(old.decoded)
                .saturating_add(metric.decoded);
            next.items = next
                .items
                .saturating_sub(old.items)
                .saturating_add(metric.items);
            next.nodes
                .insert(key, metric, budget)
                .map_err(|e| store_error(&e))?;
            next.node_reads
                .replace(key, Some(reads), budget)
                .map_err(|e| store_error(&e))?;
        }
        for &key in leaves.iter() {
            budget.poll().map_err(|e| limit_error(&e))?;
            let Some(old) = next.leaves.get(key) else {
                return Err(MalformedPdfError::new_err(
                    "a value dependency names a missing value",
                ));
            };
            let Some(field) = topology.form.fields.get(key as usize) else {
                return Err(MalformedPdfError::new_err(
                    "a value dependency names a missing field",
                ));
            };
            let old_len = old.as_ref().map_or(0, |value| value.len());
            let (value, reads) = traced_value(version, &topology.form.trace, field.trace, budget)?;
            next.returned = next
                .returned
                .saturating_sub(old_len)
                .saturating_add(value.as_ref().map_or(0, |value| value.len()));
            next.leaves
                .insert(key, value, budget)
                .map_err(|e| store_error(&e))?;
            next.leaf_reads
                .replace(key, Some(reads), budget)
                .map_err(|e| store_error(&e))?;
        }
        next.check_limits()?;
        budget.poll().map_err(|e| limit_error(&e))?;
        *self = next;
        Ok(())
    }

    fn check_limits(&self) -> PyResult<()> {
        charge(&mut 0, self.encoded, MAX_VALUE_BYTES, "encoded field-value")?;
        charge(&mut 0, self.decoded, MAX_VALUE_BYTES, "decoded field-value")?;
        charge(
            &mut 0,
            self.returned,
            MAX_VALUE_BYTES,
            "returned field-value",
        )?;
        if self.items > MAX_VALUE_ITEMS {
            return Err(limit(format!(
                "AcroForm field values exceed the {MAX_VALUE_ITEMS}-item safety limit"
            )));
        }
        Ok(())
    }
}

fn value_metric(
    version: &Version,
    identity: Id,
    budget: &Budget,
) -> PyResult<(ValueMetric, ReadSet)> {
    let mut reads = Reads::new(version, budget);
    let mut text = Text::default();
    if let Some(value) = object(&mut reads, identity)?
        && let Value::Dict(dict) = &*value
    {
        let _ = node_value(&mut reads, dict, &None, &mut text)?;
    }
    Ok((
        ValueMetric {
            encoded: text.values.0,
            decoded: text.values.1,
            items: text.items,
        },
        reads.finish().map_err(|e| store_error(&e))?,
    ))
}

fn traced_value(
    version: &Version,
    trace: &[TraceNode],
    start: Option<usize>,
    budget: &Budget,
) -> PyResult<(Option<Arc<str>>, ReadSet)> {
    let mut reads = Reads::new(version, budget);
    let Some(start) = start else {
        return Err(MalformedPdfError::new_err("a field has no value trace"));
    };
    let mut at = Some(start);
    let mut result = None;
    let mut depth = 0;
    while let Some(index) = at {
        budget.poll().map_err(|e| limit_error(&e))?;
        if depth >= MAX_DEPTH {
            return Err(limit(format!(
                "AcroForm field tree exceeds the {MAX_DEPTH}-level safety limit"
            )));
        }
        let Some(node) = trace.get(index) else {
            return Err(MalformedPdfError::new_err(
                "a form value trace names a missing node",
            ));
        };
        depth += 1;
        if let Some(value) = object(&mut reads, node.id)?
            && let Value::Dict(dict) = &*value
            && let Some(value) = own(&mut reads, dict, "V")?
        {
            let mut text = Text::default();
            result = field_value(&mut reads, value, &mut text.values, &mut text.items)?
                .map(|value| {
                    budget
                        .charge(LimitCode::MemorySize, value.len().saturating_add(32) as u64)
                        .map_err(|e| limit_error(&e))?;
                    Ok::<Arc<str>, PyErr>(Arc::from(value))
                })
                .transpose()?;
            break;
        }
        at = node.parent;
    }
    Ok((result, reads.finish().map_err(|e| store_error(&e))?))
}

/// Form membership, values and widget appearances, with their recorded reads.
#[derive(Clone)]
struct FillIndex {
    topology: Arc<FormTopology>,
    appearances: AppearanceIndex,
    values: ValueIndex,
}

impl FillIndex {
    fn build(version: &Version, budget: &Budget) -> PyResult<Self> {
        let topology = FormTopology::build(version, budget)?;
        budget
            .charge(
                LimitCode::MemorySize,
                (size_of::<FormTopology>() + 2 * size_of::<usize>()) as u64,
            )
            .map_err(|e| limit_error(&e))?;
        let topology = Arc::new(topology);
        let mut appearances = AppearanceIndex::default();
        let mut key = 0_u32;
        for field in &topology.form.fields {
            let kind = match field.kind {
                "text" | "combobox" | "listbox" => AppearanceKind::Normal,
                "checkbox" | "radio" => AppearanceKind::State,
                _ => continue,
            };
            for &widget in &field.widgets {
                budget.poll().map_err(|e| limit_error(&e))?;
                appearances.record(
                    key,
                    IndexedWidget {
                        field: field.id,
                        widget,
                        kind,
                    },
                    version,
                    budget,
                )?;
                key = key.checked_add(1).ok_or_else(edges_error)?;
            }
        }
        let values = ValueIndex::build(&topology, version, budget)?;
        Ok(Self {
            topology,
            appearances,
            values,
        })
    }

    /// Derives the next index, or walks again when membership changed.
    #[cfg(test)]
    fn update(&self, old: &Version, new: &Version, budget: &Budget) -> PyResult<Self> {
        if !self.topology.unchanged(old, new, budget)? {
            return Self::build(new, budget);
        }
        let Some(changes) = old.changes(new, budget).map_err(|e| store_error(&e))? else {
            return Self::build(new, budget);
        };
        self.refresh(new, changes.objects(), changes.trailer(), budget)
    }

    fn refresh(
        &self,
        version: &Version,
        changed: &[u32],
        trailer_changed: bool,
        budget: &Budget,
    ) -> PyResult<Self> {
        let mut updated = self.clone();
        updated
            .appearances
            .refresh(version, changed, trailer_changed, budget)?;
        updated
            .values
            .refresh(&updated.topology, version, changed, trailer_changed, budget)?;
        Ok(updated)
    }

    /// A changed object matters only when the recorded topology, values, or
    /// widget appearances read it. Pending changes are filtered this way so
    /// unrelated edits cannot grow the cache without bound. A change that
    /// introduces a new dependency is itself recorded; refreshing it reads
    /// the dependency's final value, including any later edits.
    fn depends_on(&self, number: u32) -> bool {
        self.topology.reads.objects().binary_search(&number).is_ok()
            || self.values.node_reads.contains(number)
            || self.values.leaf_reads.contains(number)
            || self.appearances.dependencies.contains(number)
    }
}

#[derive(Clone)]
struct FillEntry {
    version: Weak<Version>,
    index: Arc<FillIndex>,
    /// Objects changed since `index` was computed, applied on the next read.
    pending: DerivedMap<()>,
    trailer: bool,
}

/// The last derived fill index. It never keeps a document version alive.
#[derive(Default)]
pub(crate) struct FillCache {
    entry: Mutex<Option<FillEntry>>,
}

impl FillCache {
    pub(crate) fn new() -> Self {
        Self::default()
    }

    pub(crate) fn clear(&self) {
        self.entry
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
    }

    fn get(&self, version: &Arc<Version>, budget: &Budget) -> PyResult<Arc<FillIndex>> {
        budget.poll().map_err(|e| limit_error(&e))?;
        let held = self
            .entry
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone();
        let held = held.and_then(|entry| Some((entry.version.upgrade()?, entry)));
        let index = match held {
            Some((old, entry)) => {
                let mut changed = BudgetVec::new(budget);
                for (number, ()) in entry.pending.iter() {
                    budget.poll().map_err(|e| limit_error(&e))?;
                    changed.try_push(number).map_err(|e| limit_error(&e))?;
                }
                if changed.is_empty() && !entry.trailer && Arc::ptr_eq(&old, version) {
                    return Ok(entry.index);
                }
                let mut trailer = entry.trailer;
                if !Arc::ptr_eq(&old, version) {
                    if !entry.index.topology.unchanged(&old, version, budget)? {
                        return self.install(version, FillIndex::build(version, budget)?, budget);
                    }
                    let Some(changes) =
                        old.changes(version, budget).map_err(|e| store_error(&e))?
                    else {
                        return self.install(version, FillIndex::build(version, budget)?, budget);
                    };
                    for &number in changes.objects() {
                        changed.try_push(number).map_err(|e| limit_error(&e))?;
                    }
                    trailer |= changes.trailer();
                }
                if changed.is_empty() && !trailer {
                    entry.index.as_ref().clone()
                } else {
                    entry.index.refresh(version, &changed, trailer, budget)?
                }
            }
            None => FillIndex::build(version, budget)?,
        };
        self.install(version, index, budget)
    }

    fn install(
        &self,
        version: &Arc<Version>,
        index: FillIndex,
        budget: &Budget,
    ) -> PyResult<Arc<FillIndex>> {
        budget
            .charge(
                LimitCode::MemorySize,
                (size_of::<FillIndex>() + 2 * size_of::<usize>()) as u64,
            )
            .map_err(|e| limit_error(&e))?;
        let index = Arc::new(index);
        *self.entry.lock().unwrap_or_else(PoisonError::into_inner) = Some(FillEntry {
            version: Arc::downgrade(version),
            index: Arc::clone(&index),
            pending: DerivedMap::new(),
            trailer: false,
        });
        Ok(index)
    }

    /// Carry a matching transaction result to the version that commit
    /// published. A failed optional refresh leaves the cache untouched.
    pub(crate) fn carry_from(
        &self,
        source: &Self,
        anchor: &Arc<Version>,
        committed: &Arc<Version>,
        budget: &Budget,
    ) {
        let held = source
            .entry
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone();
        let Some((held, entry)) = held.and_then(|entry| Some((entry.version.upgrade()?, entry)))
        else {
            return;
        };
        if !Arc::ptr_eq(&held, anchor) {
            return;
        }
        let Ok(true) = entry.index.topology.unchanged(anchor, committed, budget) else {
            return;
        };
        let Ok(Some(changes)) = anchor.changes(committed, budget) else {
            return;
        };
        let mut pending = entry.pending.clone();
        for &number in changes.objects() {
            if budget.poll().is_err() {
                return;
            }
            if entry.index.depends_on(number)
                && pending.get(number).is_none()
                && pending.insert(number, (), budget).is_err()
            {
                return;
            }
        }
        if budget.poll().is_err() {
            return;
        }
        *self.entry.lock().unwrap_or_else(PoisonError::into_inner) = Some(FillEntry {
            version: Arc::downgrade(committed),
            index: entry.index,
            pending,
            trailer: entry.trailer || changes.trailer(),
        });
    }
}

#[cfg(test)]
mod appearance_tests {
    use super::*;
    use pyropdf_store::value::Bytes;
    use pyropdf_store::version::{Document, MemoryBase};

    fn document(widget: &'static [u8], normal: &'static [u8]) -> Document {
        let mut base = MemoryBase::new(Dict::new());
        base.insert(Reference::new(4, 0), Bytes::new(widget));
        base.insert(Reference::new(8, 0), Bytes::new(widget));
        base.insert(Reference::new(5, 0), Bytes::new(&b"<< /N 6 0 R >>"[..]));
        base.insert(Reference::new(6, 0), Bytes::new(normal));
        base.insert(
            Reference::new(7, 0),
            Bytes::new(&b"<< /Length 3 >>\nstream\nq Q\nendstream"[..]),
        );
        Document::new(Arc::new(base), None)
    }

    fn version(widget: &'static [u8], normal: &'static [u8]) -> Arc<Version> {
        document(widget, normal).current()
    }

    #[test]
    fn selected_appearance_records_indirect_dependencies() {
        let version = version(
            b"<< /Subtype /Widget /AS /On /AP 5 0 R >>",
            b"<< /On 7 0 R >>",
        );
        let budget = Budget::default();
        let mut reads = Reads::new(&version, &budget);
        assert_eq!(
            widget_appearance(&mut reads, (4, 0), AppearanceKind::State).unwrap(),
            Some(true)
        );
        assert_eq!(reads.finish().unwrap().objects(), &[4, 5, 6, 7]);
    }

    #[test]
    fn missing_and_stale_appearances_are_recorded() {
        let version = version(b"<< /Subtype /Widget /AP 5 0 R >>", b"7 1 R");
        let budget = Budget::default();
        let mut reads = Reads::new(&version, &budget);
        assert_eq!(
            widget_appearance(&mut reads, (4, 0), AppearanceKind::Normal).unwrap(),
            Some(false)
        );
        assert_eq!(reads.finish().unwrap().objects(), &[4, 5, 6, 7]);
    }

    #[test]
    fn shared_appearance_changes_and_rollback_refresh_only_its_readers() {
        let document = document(b"<< /Subtype /Widget /AP 5 0 R >>", b"7 0 R");
        let before = document.current();
        let budget = Budget::default();
        let mut index = AppearanceIndex::default();
        for key in [1, 2, 3] {
            index
                .record(
                    key,
                    IndexedWidget {
                        field: (key, 0),
                        widget: (if key == 2 { 8 } else { 4 }, 0),
                        kind: AppearanceKind::Normal,
                    },
                    &before,
                    &budget,
                )
                .unwrap();
        }
        let saved = index.clone();
        assert_eq!(index.missing.iter().count(), 0);
        assert_eq!(
            index
                .dependencies
                .affected(&[7], false, &budget)
                .unwrap()
                .into_vec(),
            vec![1, 2, 3]
        );

        let mut transaction = document.begin();
        let savepoint = transaction.savepoint();
        transaction.write(7, &Value::Null, &budget).unwrap();
        let changed = transaction.snapshot();
        let changes = before.changes(&changed, &budget).unwrap().unwrap();
        index
            .refresh(&changed, changes.objects(), changes.trailer(), &budget)
            .unwrap();
        assert_eq!(
            index.missing.iter().map(|(key, _)| key).collect::<Vec<_>>(),
            vec![1, 2, 3]
        );
        assert_eq!(saved.missing.iter().count(), 0);

        transaction.rollback(savepoint);
        let restored = transaction.snapshot();
        let changes = changed.changes(&restored, &budget).unwrap().unwrap();
        index
            .refresh(&restored, changes.objects(), changes.trailer(), &budget)
            .unwrap();
        assert_eq!(index.missing.iter().count(), 0);
    }

    #[test]
    fn refused_appearance_refresh_leaves_the_prior_index_unchanged() {
        let document = document(b"<< /Subtype /Widget /AP 5 0 R >>", b"7 0 R");
        let before = document.current();
        let budget = Budget::default();
        let mut index = AppearanceIndex::default();
        index
            .record(
                1,
                IndexedWidget {
                    field: (1, 0),
                    widget: (4, 0),
                    kind: AppearanceKind::Normal,
                },
                &before,
                &budget,
            )
            .unwrap();
        let mut transaction = document.begin();
        transaction.write(7, &Value::Null, &budget).unwrap();
        let changed = transaction.snapshot();
        let limited = Budget::default().with_limit(LimitCode::MemorySize, Some(0));
        assert!(index.refresh(&changed, &[7], false, &limited).is_err());
        assert_eq!(index.missing.iter().count(), 0);
        index.refresh(&changed, &[7], false, &budget).unwrap();
        assert_eq!(index.missing.iter().count(), 1);
    }

    #[test]
    fn appearance_index_follows_undo_and_redo() {
        let document = document(b"<< /Subtype /Widget /AP 5 0 R >>", b"7 0 R");
        let before = document.current();
        let budget = Budget::default();
        let mut index = AppearanceIndex::default();
        index
            .record(
                1,
                IndexedWidget {
                    field: (1, 0),
                    widget: (4, 0),
                    kind: AppearanceKind::Normal,
                },
                &before,
                &budget,
            )
            .unwrap();
        let mut transaction = document.begin();
        transaction.write(7, &Value::Null, &budget).unwrap();
        let after = transaction.commit(&budget).unwrap();
        let changes = before.changes(&after, &budget).unwrap().unwrap();
        index
            .refresh(&after, changes.objects(), changes.trailer(), &budget)
            .unwrap();
        assert_eq!(index.missing.iter().count(), 1);

        let undone = document.undo().unwrap();
        let changes = after.changes(&undone, &budget).unwrap().unwrap();
        index
            .refresh(&undone, changes.objects(), changes.trailer(), &budget)
            .unwrap();
        assert_eq!(index.missing.iter().count(), 0);
        let redone = document.redo().unwrap();
        let changes = undone.changes(&redone, &budget).unwrap().unwrap();
        index
            .refresh(&redone, changes.objects(), changes.trailer(), &budget)
            .unwrap();
        assert_eq!(index.missing.iter().count(), 1);
    }

    #[test]
    fn topology_reuses_membership_for_value_and_appearance_changes_only() {
        let mut base = MemoryBase::new(
            [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
                .into_iter()
                .collect(),
        );
        for (number, bytes) in [
            (1, &b"<< /Type /Catalog /AcroForm 4 0 R >>"[..]),
            (4, &b"<< /Fields [5 0 R] >>"[..]),
            (
                5,
                &b"<< /FT /Tx /T (field) /V (old) /Subtype /Widget /Rect [0 0 10 10] >>"[..],
            ),
        ] {
            base.insert(Reference::new(number, 0), Bytes::new(bytes));
        }
        let document = Document::new(Arc::new(base), None);
        let budget = Budget::default();
        let before = document.current();
        let topology = FormTopology::build(&before, &budget).unwrap();
        assert_eq!(topology.form.fields.len(), 1);
        assert!(topology.form.fields[0].value.is_none());

        let mut transaction = document.begin();
        let changed_value = Value::parse(&Bytes::new(&b"<< /FT /Tx /T (field) /V (new) /Subtype /Widget /Rect [1 2 3 4] /AP << >> >>"[..]), &budget).unwrap();
        transaction.write(5, &changed_value, &budget).unwrap();
        let value_version = transaction.snapshot();
        assert!(
            topology
                .unchanged(&before, &value_version, &budget)
                .unwrap()
        );

        let changed_name = Value::parse(
            &Bytes::new(&b"<< /FT /Tx /T (other) /V (new) /Subtype /Widget /Rect [1 2 3 4] >>"[..]),
            &budget,
        )
        .unwrap();
        transaction.write(5, &changed_name, &budget).unwrap();
        let renamed = transaction.snapshot();
        assert!(!topology.unchanged(&before, &renamed, &budget).unwrap());
    }

    #[test]
    fn topology_rebuilds_when_a_form_holder_changes() {
        let mut base = MemoryBase::new(
            [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
                .into_iter()
                .collect(),
        );
        base.insert(
            Reference::new(1, 0),
            Bytes::new(&b"<< /AcroForm 4 0 R >>"[..]),
        );
        base.insert(
            Reference::new(4, 0),
            Bytes::new(&b"<< /Fields [5 0 R] >>"[..]),
        );
        base.insert(
            Reference::new(5, 0),
            Bytes::new(&b"<< /FT /Tx /T (field) >>"[..]),
        );
        let document = Document::new(Arc::new(base), None);
        let budget = Budget::default();
        let before = document.current();
        let topology = FormTopology::build(&before, &budget).unwrap();
        let mut transaction = document.begin();
        transaction.write(4, &Value::Null, &budget).unwrap();
        let after = transaction.snapshot();
        assert!(!topology.unchanged(&before, &after, &budget).unwrap());
    }

    #[test]
    fn topology_rebuilds_for_changed_kids_and_indirect_type() {
        let mut base = MemoryBase::new(
            [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
                .into_iter()
                .collect(),
        );
        for (number, bytes) in [
            (1, &b"<< /AcroForm 4 0 R >>"[..]),
            (4, &b"<< /Fields [5 0 R] >>"[..]),
            (5, &b"<< /FT 9 0 R /T (field) /Kids [6 0 R] >>"[..]),
            (6, &b"<< /Subtype /Widget >>"[..]),
            (8, &b"<< /Subtype /Widget >>"[..]),
            (9, &b"/Tx"[..]),
        ] {
            base.insert(Reference::new(number, 0), Bytes::new(bytes));
        }
        let document = Document::new(Arc::new(base), None);
        let budget = Budget::default();
        let before = document.current();
        let topology = FormTopology::build(&before, &budget).unwrap();
        let mut transaction = document.begin();
        let initial = transaction.savepoint();
        let field = Value::parse(
            &Bytes::new(&b"<< /FT 9 0 R /T (field) /Kids [8 0 R] >>"[..]),
            &budget,
        )
        .unwrap();
        transaction.write(5, &field, &budget).unwrap();
        assert!(
            !topology
                .unchanged(&before, &transaction.snapshot(), &budget)
                .unwrap()
        );
        transaction.rollback(initial);
        transaction
            .write(9, &Value::Name(Name::from("Btn")), &budget)
            .unwrap();
        assert!(
            !topology
                .unchanged(&before, &transaction.snapshot(), &budget)
                .unwrap()
        );
    }

    #[test]
    fn fill_index_updates_appearances_and_rebuilds_changed_membership() {
        let mut base = MemoryBase::new(
            [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
                .into_iter()
                .collect(),
        );
        for (number, bytes) in [
            (1, &b"<< /AcroForm 4 0 R >>"[..]),
            (4, &b"<< /Fields [5 0 R] >>"[..]),
            (
                5,
                &b"<< /FT /Tx /T (field) /V (old) /Subtype /Widget /AP 6 0 R >>"[..],
            ),
            (6, &b"<< /N 7 0 R >>"[..]),
            (7, &b"<< /Length 3 >>\nstream\nq Q\nendstream"[..]),
        ] {
            base.insert(Reference::new(number, 0), Bytes::new(bytes));
        }
        let document = Document::new(Arc::new(base), None);
        let budget = Budget::default();
        let before = document.current();
        let first = FillIndex::build(&before, &budget).unwrap();
        assert_eq!(first.appearances.missing.iter().count(), 0);
        let mut transaction = document.begin();
        transaction.write(7, &Value::Null, &budget).unwrap();
        let changed = transaction.snapshot();
        let second = first.update(&before, &changed, &budget).unwrap();
        assert!(Arc::ptr_eq(&first.topology, &second.topology));
        assert_eq!(second.appearances.missing.iter().count(), 1);
        assert_eq!(first.appearances.missing.iter().count(), 0);

        let renamed = Value::parse(
            &Bytes::new(&b"<< /FT /Tx /T (renamed) /V (old) /Subtype /Widget /AP 6 0 R >>"[..]),
            &budget,
        )
        .unwrap();
        transaction.write(5, &renamed, &budget).unwrap();
        let after = transaction.snapshot();
        let third = second.update(&changed, &after, &budget).unwrap();
        assert!(!Arc::ptr_eq(&second.topology, &third.topology));
        assert_eq!(third.topology.form.fields[0].name, "renamed");
        assert_eq!(third.appearances.missing.iter().count(), 1);
    }

    #[test]
    fn indexed_inherited_values_follow_indirect_and_parent_changes() {
        let mut base = MemoryBase::new(
            [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
                .into_iter()
                .collect(),
        );
        for (number, bytes) in [
            (1, &b"<< /AcroForm 4 0 R >>"[..]),
            (4, &b"<< /Fields [5 0 R] >>"[..]),
            (
                5,
                &b"<< /FT /Tx /T (parent) /V 9 0 R /Kids [6 0 R 7 0 R] >>"[..],
            ),
            (6, &b"<< /T (a) /Parent 5 0 R >>"[..]),
            (7, &b"<< /T (b) /Parent 5 0 R /V (local) >>"[..]),
            (9, &b"(inherited)"[..]),
        ] {
            base.insert(Reference::new(number, 0), Bytes::new(bytes));
        }
        let document = Document::new(Arc::new(base), None);
        let budget = Budget::default();
        let before = document.current();
        let first = FillIndex::build(&before, &budget).unwrap();
        assert_eq!(
            first.values.leaves.get(0).unwrap().as_deref(),
            Some("inherited")
        );
        assert_eq!(
            first.values.leaves.get(1).unwrap().as_deref(),
            Some("local")
        );
        let mut transaction = document.begin();
        transaction
            .write(
                9,
                &Value::String(PdfString::literal(&b"changed"[..])),
                &budget,
            )
            .unwrap();
        let changed = transaction.snapshot();
        let second = first.update(&before, &changed, &budget).unwrap();
        assert!(Arc::ptr_eq(&first.topology, &second.topology));
        assert_eq!(
            second.values.leaves.get(0).unwrap().as_deref(),
            Some("changed")
        );
        assert_eq!(
            second.values.leaves.get(1).unwrap().as_deref(),
            Some("local")
        );
        let fresh = walk(&mut Reads::new(&changed, &budget)).unwrap();
        for (at, field) in fresh.fields.iter().enumerate() {
            assert_eq!(
                second.values.leaves.get(at as u32).unwrap().as_deref(),
                field.value.as_deref()
            );
        }

        let parent = Value::parse(
            &Bytes::new(&b"<< /FT /Tx /T (parent) /V null /Kids [6 0 R 7 0 R] >>"[..]),
            &budget,
        )
        .unwrap();
        transaction.write(5, &parent, &budget).unwrap();
        let no_parent_value = transaction.snapshot();
        let third = second.update(&changed, &no_parent_value, &budget).unwrap();
        assert!(Arc::ptr_eq(&second.topology, &third.topology));
        assert_eq!(third.values.leaves.get(0).unwrap().as_deref(), None);
        assert_eq!(
            third.values.leaves.get(1).unwrap().as_deref(),
            Some("local")
        );
    }

    #[test]
    fn indexed_values_keep_the_whole_form_value_limit() {
        let mut base = MemoryBase::new(
            [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
                .into_iter()
                .collect(),
        );
        for (number, bytes) in [
            (1, &b"<< /AcroForm 4 0 R >>"[..]),
            (4, &b"<< /Fields [5 0 R 6 0 R] >>"[..]),
            (5, &b"<< /FT /Tx /T (a) /V 9 0 R >>"[..]),
            (6, &b"<< /FT /Tx /T (b) /V 10 0 R >>"[..]),
            (9, &b"(a)"[..]),
            (10, &b"(b)"[..]),
        ] {
            base.insert(Reference::new(number, 0), Bytes::new(bytes));
        }
        let document = Document::new(Arc::new(base), None);
        let budget = Budget::default();
        let mut transaction = document.begin();
        transaction
            .write(
                9,
                &Value::String(PdfString::literal(vec![b'a'; 600_000])),
                &budget,
            )
            .unwrap();
        transaction
            .write(
                10,
                &Value::String(PdfString::literal(vec![b'b'; 100_000])),
                &budget,
            )
            .unwrap();
        let before = transaction.snapshot();
        let index = FillIndex::build(&before, &budget).unwrap();
        transaction
            .write(
                10,
                &Value::String(PdfString::literal(vec![b'b'; 600_000])),
                &budget,
            )
            .unwrap();
        let after = transaction.snapshot();
        assert!(index.update(&before, &after, &budget).is_err());
        assert!(walk(&mut Reads::new(&after, &budget)).is_err());
        assert_eq!(
            index.values.leaves.get(1).unwrap().as_ref().unwrap().len(),
            100_000
        );
    }

    #[test]
    fn fill_cache_tracks_versions_without_reusing_stale_values() {
        let mut base = MemoryBase::new(
            [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
                .into_iter()
                .collect(),
        );
        for (number, bytes) in [
            (1, &b"<< /AcroForm 4 0 R >>"[..]),
            (4, &b"<< /Fields [5 0 R] >>"[..]),
            (5, &b"<< /FT /Tx /T (field) /V (old) >>"[..]),
        ] {
            base.insert(Reference::new(number, 0), Bytes::new(bytes));
        }
        let document = Document::new(Arc::new(base), None);
        let budget = Budget::default();
        let before = document.current();
        let cache = FillCache::new();
        let first = cache.get(&before, &budget).unwrap();
        let mut transaction = document.begin();
        let field = Value::parse(
            &Bytes::new(&b"<< /FT /Tx /T (field) /V (new) >>"[..]),
            &budget,
        )
        .unwrap();
        transaction.write(5, &field, &budget).unwrap();
        let after = transaction.commit(&budget).unwrap();
        let second = cache.get(&after, &budget).unwrap();
        assert!(Arc::ptr_eq(&first.topology, &second.topology));
        assert_eq!(first.values.leaves.get(0).unwrap().as_deref(), Some("old"));
        assert_eq!(second.values.leaves.get(0).unwrap().as_deref(), Some("new"));
        let undone = document.undo().unwrap();
        let undone = cache.get(&undone, &budget).unwrap();
        assert_eq!(undone.values.leaves.get(0).unwrap().as_deref(), Some("old"));
    }

    #[test]
    fn deferred_changes_follow_new_dependencies_without_old_versions() {
        let mut base = MemoryBase::new(
            [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
                .into_iter()
                .collect(),
        );
        for (number, bytes) in [
            (1, &b"<< /AcroForm 4 0 R >>"[..]),
            (4, &b"<< /Fields [5 0 R] >>"[..]),
            (5, &b"<< /FT /Tx /T (field) /V 9 0 R >>"[..]),
            (9, &b"(old)"[..]),
            (10, &b"(new)"[..]),
        ] {
            base.insert(Reference::new(number, 0), Bytes::new(bytes));
        }
        let document = Document::new(Arc::new(base), Some(0));
        let budget = Budget::default();
        let before = document.current();
        let cache = FillCache::new();
        let first = cache.get(&before, &budget).unwrap();
        let mut transaction = document.begin();
        let field = Value::parse(
            &Bytes::new(&b"<< /FT /Tx /T (field) /V 10 0 R >>"[..]),
            &budget,
        )
        .unwrap();
        transaction.write(5, &field, &budget).unwrap();
        let changed = transaction.commit(&budget).unwrap();
        cache.carry_from(&cache, &before, &changed, &budget);
        let mut transaction = document.begin();
        transaction
            .write(
                10,
                &Value::String(PdfString::literal(&b"latest"[..])),
                &budget,
            )
            .unwrap();
        let latest = transaction.commit(&budget).unwrap();
        cache.carry_from(&cache, &changed, &latest, &budget);
        drop(before);
        drop(changed);
        let result = cache.get(&latest, &budget).unwrap();
        assert!(Arc::ptr_eq(&first.topology, &result.topology));
        assert_eq!(
            result.values.leaves.get(0).unwrap().as_deref(),
            Some("latest")
        );
    }

    #[test]
    fn unrelated_edits_do_not_accumulate_in_the_fill_cache() {
        let mut base = MemoryBase::new(
            [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
                .into_iter()
                .collect(),
        );
        for (number, bytes) in [
            (1, &b"<< /AcroForm 4 0 R >>"[..]),
            (4, &b"<< /Fields [5 0 R] >>"[..]),
            (5, &b"<< /FT /Tx /T (field) /V (old) >>"[..]),
        ] {
            base.insert(Reference::new(number, 0), Bytes::new(bytes));
        }
        let document = Document::new(Arc::new(base), Some(0));
        let budget = Budget::default();
        let cache = FillCache::new();
        let original = cache.get(&document.current(), &budget).unwrap();
        for number in 1000..2000 {
            let before = document.current();
            let mut transaction = document.begin();
            transaction
                .write(number, &Value::Integer(i64::from(number)), &budget)
                .unwrap();
            let after = transaction.commit(&budget).unwrap();
            cache.carry_from(&cache, &before, &after, &budget);
        }
        let held = cache
            .entry
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
            .unwrap();
        assert!(held.pending.iter().next().is_none());
        assert!(Arc::ptr_eq(
            &original,
            &cache.get(&document.current(), &budget).unwrap()
        ));
    }

    #[test]
    fn indexed_values_match_a_fresh_walk_for_varied_inheritance() {
        let mut base = MemoryBase::new(
            [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
                .into_iter()
                .collect(),
        );
        for (number, bytes) in [
            (1, &b"<< /AcroForm 4 0 R >>"[..]),
            (4, &b"<< /Fields [5 0 R] >>"[..]),
            (
                5,
                &b"<< /FT /Tx /T (parent) /V (inherited) /Kids [6 0 R 7 0 R] >>"[..],
            ),
            (6, &b"<< /T (a) /Parent 5 0 R >>"[..]),
            (7, &b"<< /T (b) /Parent 5 0 R /V (other) >>"[..]),
            (9, &b"(indirect)"[..]),
        ] {
            base.insert(Reference::new(number, 0), Bytes::new(bytes));
        }
        let document = Document::new(Arc::new(base), None);
        let budget = Budget::default();
        let before = document.current();
        let index = FillIndex::build(&before, &budget).unwrap();
        let mut transaction = document.begin();
        let start = transaction.savepoint();
        for value in [
            "null",
            "(direct)",
            "9 0 R",
            "[(a) (b)]",
            "123",
            "<FEFF4E2D>",
        ] {
            let bytes = format!("<< /T (a) /Parent 5 0 R /V {value} >>").into_bytes();
            let field = Value::parse(&Bytes::new(bytes), &budget).unwrap();
            transaction.write(6, &field, &budget).unwrap();
            let version = transaction.snapshot();
            let updated = index.update(&before, &version, &budget).unwrap();
            assert!(Arc::ptr_eq(&index.topology, &updated.topology));
            let fresh = walk(&mut Reads::new(&version, &budget)).unwrap();
            for (at, field) in fresh.fields.iter().enumerate() {
                assert_eq!(
                    updated.values.leaves.get(at as u32).unwrap().as_deref(),
                    field.value.as_deref()
                );
            }
        }
        transaction.rollback(start);
    }

    #[test]
    fn indexed_forms_match_fresh_walks_for_varied_edits() {
        let mut base = MemoryBase::new(
            [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
                .into_iter()
                .collect(),
        );
        for (number, bytes) in [
            (1, &b"<< /AcroForm 4 0 R >>"[..]),
            (4, &b"<< /Fields [5 0 R 6 0 R] >>"[..]),
            (
                5,
                &b"<< /FT /Tx /T (a) /Ff 0 /V (old) /Kids [] /Subtype /Widget /Rect [0 0 1 1] /AP 10 0 R >>"[..],
            ),
            (6, &b"<< /FT /Tx /T (b) /V (other) >>"[..]),
            (8, &b"<< /Subtype /Widget >>"[..]),
            (9, &b"(indirect)"[..]),
            (10, &b"<< /N 11 0 R >>"[..]),
            (11, &b"<< /Length 3 >>\nstream\nq Q\nendstream"[..]),
        ] {
            base.insert(Reference::new(number, 0), Bytes::new(bytes));
        }
        let document = Document::new(Arc::new(base), None);
        let budget = Budget::default();
        let before = document.current();
        let index = FillIndex::build(&before, &budget).unwrap();
        let mut transaction = document.begin();
        let mut seed = 0x52a3_49d1_u64;
        let mut reused = 0;
        for _ in 0..2048 {
            seed = seed.wrapping_mul(6_364_136_223_846_793_005).wrapping_add(1);
            let name = if seed & 1 == 0 { "a" } else { "renamed" };
            let kind = if seed & 2 == 0 { "Tx" } else { "Btn" };
            let flags = if seed & 4 == 0 { 0 } else { 32_768 };
            let kids = if seed & 8 == 0 { "[]" } else { "[8 0 R]" };
            let subtype = if seed & 16 == 0 { "/Widget" } else { "/Link" };
            let rectangle = if seed & 32 == 0 {
                "/Rect [0 0 1 1]"
            } else {
                ""
            };
            let values = ["null", "(x)", "9 0 R", "[(a) (b)]", "123", "<FEFF4E2D>"];
            let value = values[((seed >> 8) as usize) % values.len()];
            let bytes = format!("<< /FT /{kind} /T ({name}) /Ff {flags} /V {value} /Kids {kids} /Subtype {subtype} {rectangle} /AP 10 0 R >>").into_bytes();
            let field = Value::parse(&Bytes::new(bytes), &budget).unwrap();
            let savepoint = transaction.savepoint();
            transaction.write(5, &field, &budget).unwrap();
            let after = transaction.snapshot();
            let updated = index.update(&before, &after, &budget).unwrap();
            reused += usize::from(Arc::ptr_eq(&index.topology, &updated.topology));
            let fresh = walk(&mut Reads::new(&after, &budget)).unwrap();
            assert_eq!(updated.topology.form.fields.len(), fresh.fields.len());
            for (at, (indexed, actual)) in updated
                .topology
                .form
                .fields
                .iter()
                .zip(&fresh.fields)
                .enumerate()
            {
                assert_eq!(
                    (
                        &indexed.name,
                        indexed.id,
                        indexed.kind,
                        indexed.flags,
                        &indexed.widgets
                    ),
                    (
                        &actual.name,
                        actual.id,
                        actual.kind,
                        actual.flags,
                        &actual.widgets
                    )
                );
                assert_eq!(
                    updated.values.leaves.get(at as u32).unwrap().as_deref(),
                    actual.value.as_deref()
                );
            }
            transaction.rollback(savepoint);
        }
        assert!(reused > 0 && reused < 2048);
    }

    #[test]
    #[ignore = "measurement run by evidence/form-fill-cost/measure_index.py"]
    fn measure_fill_index_updates() {
        use std::hint::black_box;
        use std::time::Instant;

        for count in [1_u32, 10, 100, 1_000, 4_000] {
            let mut base = MemoryBase::new(
                [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
                    .into_iter()
                    .collect(),
            );
            base.insert(
                Reference::new(1, 0),
                Bytes::new(&b"<< /AcroForm 4 0 R >>"[..]),
            );
            base.insert(
                Reference::new(3, 0),
                Bytes::new(&b"<< /Length 3 >>\nstream\nq Q\nendstream"[..]),
            );
            let references = (5..count + 5)
                .map(|n| format!("{n} 0 R"))
                .collect::<Vec<_>>()
                .join(" ");
            base.insert(
                Reference::new(4, 0),
                Bytes::new(format!("<< /Fields [{references}] >>").into_bytes()),
            );
            for number in 5..count + 5 {
                base.insert(Reference::new(number, 0), Bytes::new(format!("<< /FT /Tx /T (field{number}) /V (old) /Subtype /Widget /AP << /N 3 0 R >> >>").into_bytes()));
            }
            let document = Document::new(Arc::new(base), None);
            let before = document.current();
            let budget = Budget::default();
            let start = Instant::now();
            let index = FillIndex::build(&before, &budget).unwrap();
            let cold = start.elapsed().as_secs_f64() * 1000.0;
            let mut transaction = document.begin();
            let field = Value::parse(
                &Bytes::new(
                    &b"<< /FT /Tx /T (field5) /V (new) /Subtype /Widget /AP << /N 3 0 R >> >>"[..],
                ),
                &budget,
            )
            .unwrap();
            transaction.write(5, &field, &budget).unwrap();
            let after = transaction.snapshot();
            let mut batches = Vec::new();
            for _ in 0..7 {
                let start = Instant::now();
                for _ in 0..100 {
                    let budget = Budget::default();
                    black_box(index.update(&before, &after, &budget).unwrap());
                }
                batches.push(start.elapsed().as_secs_f64() * 10.0);
            }
            batches.sort_by(f64::total_cmp);
            println!("PYRO_FILL_INDEX {count} {cold:.3} {:.4}", batches[3]);
        }
    }
}

/// A widget's style: its `/MK` border color, background color, caption,
/// and rotation; its border, from `/BS` else `/Border`; and the font resource,
/// size, and RGB color of the `/DA` that it draws its text with, its own or
/// inherited.
type WidgetStyle = (
    (Vec<f64>, Vec<f64>, Option<String>, i64),
    Option<(f64, String, Vec<f64>)>,
    Option<(String, f64, (f64, f64, f64))>,
);

/// Reads the style of the widget that a handle names. Returns `None` for a
/// page that is gone, and raises `StaleObjectError` for a widget that is.
#[pyfunction]
pub fn widget_style(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: Id,
) -> PyResult<Option<WidgetStyle>> {
    let (version, pages, budget) = crate::pages::index(document)?;
    py.detach(|| {
        let Some(annots) = Annots::read(version, pages, &budget, number, generation)? else {
            return Ok(None);
        };
        let (_, dict) = annots.find(index, id)?;
        if annots.name(&dict, "Subtype")?.as_deref() != Some(b"Widget") {
            return Err(StaleObjectError::new_err(format!(
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
        let appearance = widgets::text_appearance(&annots.reader(), &dict)
            .map_err(widget_error)?
            .map(|da| (da.font, da.size, da.color));
        Ok(Some((colors, annots.border(&dict)?, appearance)))
    })
}

/// An edit of a widget's geometry and style, as the setters of `Widget`
/// pass it; `clear` names the properties to remove. The rotation is a
/// multiple of 90 degrees. Colors are RGB from 0
/// to 1, the border is `(width, style, dashes)`, and `font_name` is the
/// PostScript name of a Standard 14 font.
#[derive(FromPyObject)]
#[pyo3(from_item_all)]
pub struct WidgetEdit {
    #[pyo3(default)]
    rect: Option<Rect>,
    #[pyo3(default)]
    flags: Option<i64>,
    #[pyo3(default)]
    border: Option<(f64, String, Vec<f64>)>,
    #[pyo3(default)]
    border_color: Option<(f64, f64, f64)>,
    #[pyo3(default)]
    background_color: Option<(f64, f64, f64)>,
    #[pyo3(default)]
    caption: Option<String>,
    #[pyo3(default)]
    rotation: Option<i64>,
    #[pyo3(default)]
    text_color: Option<(f64, f64, f64)>,
    #[pyo3(default)]
    font_name: Option<String>,
    #[pyo3(default)]
    font_size: Option<f64>,
    /// Always given, so that the edit is not all defaults.
    clear: Vec<String>,
}

impl WidgetEdit {
    /// Applies the edit to `dict`, a widget on a page of `annots`.
    fn apply(&self, annots: &Annots, dict: &mut Dict, budget: &Budget) -> PyResult<()> {
        if self
            .caption
            .as_ref()
            .is_some_and(|caption| caption.len() > MAX_INPUT_BYTES)
        {
            return Err(LimitError::new_err((
                "annotation_input_size",
                format!("a widget caption exceeds the {MAX_INPUT_BYTES}-byte UTF-8 safety limit"),
            )));
        }
        let mut mark = match annots.resolved(dict.get("MK"))? {
            Some(Value::Dict(mark)) => mark,
            _ => Dict::new(),
        };
        let marked = !mark.is_empty();
        if let Some(caption) = &self.caption {
            mark.insert(Name::from("CA"), encode_text(caption));
        }
        if let Some(rotation) = self.rotation {
            if rotation % 90 != 0 {
                return Err(PdfError::new_err(format!(
                    "a widget rotation is a multiple of 90 degrees: {rotation}"
                )));
            }
            mark.insert(Name::from("R"), Value::Integer(rotation.rem_euclid(360)));
        }
        if let Some(color) = self.border_color {
            mark.insert(Name::from("BC"), rgb(color, "border color")?);
        }
        if let Some(color) = self.background_color {
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
                    return Err(PdfError::new_err(format!(
                        "a widget property that cannot be removed: {other:?}"
                    )));
                }
            }
        }
        if !mark.is_empty() {
            dict.insert(Name::from("MK"), Value::Dict(mark));
        } else if marked {
            dict.remove("MK");
        }
        if let Some(border) = &self.border {
            dict.remove("Border");
            dict.insert(Name::from("BS"), border_style(border, budget)?);
        }
        if let Some(flags) = self.flags {
            if !(0..=1023).contains(&flags) {
                return Err(PdfError::new_err("annotation flags are bits 1 to 10"));
            }
            dict.insert(Name::from("F"), Value::Integer(flags));
        }
        if let Some(area @ (x0, y0, x1, y1)) = self.rect {
            if ![x0, y0, x1, y1].iter().all(|value| value.is_finite()) || x1 <= x0 || y1 <= y0 {
                return Err(PdfError::new_err(
                    "a widget rectangle is finite and not empty",
                ));
            }
            dict.insert(Name::from("Rect"), annots.to_pdf(area));
        }
        if self.text_color.is_some() || self.font_name.is_some() || self.font_size.is_some() {
            let current =
                match widgets::text_appearance(&annots.reader(), dict).map_err(widget_error)? {
                    Some(current) => current,
                    None => DefaultAppearance::parse(b"")
                        .map_err(|_| PdfError::new_err("an empty /DA does not parse"))?,
                };
            let color = self.text_color.unwrap_or(current.color);
            rgb(color, "text color")?;
            let resource = match &self.font_name {
                Some(name) => DA_FONTS
                    .iter()
                    .find(|(_, font)| font == name)
                    .map(|(resource, _)| (*resource).to_owned())
                    .ok_or_else(|| {
                        PdfError::new_err(format!("unknown Standard 14 font {name:?}"))
                    })?,
                None => current.font,
            };
            let size = self.font_size.unwrap_or(current.size);
            if !(size.is_finite() && (0.0..=10_000.0).contains(&size)) {
                return Err(PdfError::new_err(
                    "a widget font size is from 0, which fits the text, to 10000",
                ));
            }
            let (red, green, blue) = color;
            let da = format!("/{resource} {size} Tf {red} {green} {blue} rg");
            dict.insert(
                Name::from("DA"),
                Value::String(PdfString::literal(da.into_bytes())),
            );
        }
        Ok(())
    }
}

/// Edits the widget that a handle names and draws it again for its
/// field's value: a text or choice value, every state of a check box or
/// radio button, and a pushbutton's caption; a signature widget keeps an
/// appearance that it has. The page and the widget are read after the edit
/// takes the writer lock. Returns the widget's object, which a direct
/// dictionary gains here, or `None` for a page that is gone.
#[pyfunction]
pub fn widget_edit(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: Id,
    edit: WidgetEdit,
) -> PyResult<Option<Id>> {
    let draft = document.in_transaction();
    let cache = if draft {
        &document.draft_fill_index
    } else {
        &document.fill_index
    };
    editing(
        py,
        document,
        number,
        generation,
        |annots, transaction, budget| {
            let before = base(transaction, draft);
            let (position, mut dict) = annots.find(index, id)?;
            if annots.name(&dict, "Subtype")?.as_deref() != Some(b"Widget") {
                return Err(StaleObjectError::new_err(format!(
                    "annotation {} is no longer a widget",
                    id.0
                )));
            }
            edit.apply(annots, &mut dict, budget)?;
            annots.write(
                transaction,
                position,
                false,
                dict,
                |transaction, reference, dict, _, budget| {
                    restyle(transaction, reference, dict, budget, (cache, before))?;
                    Ok(false)
                },
            )
        },
    )
}

/// Writes widget `reference`, `dict`, draws it again as [`widget_edit`]
/// does, leaves `dict` as written, and sets `/NeedAppearances` while a
/// widget of the form lacks an appearance, as the form index of `before`
/// refreshed with the edit finds.
fn restyle(
    transaction: &mut Transaction,
    reference: Reference,
    dict: &mut Dict,
    budget: &Budget,
    (cache, before): (&FillCache, Arc<Version>),
) -> PyResult<()> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let number = reference.number();
    let written = Value::Dict(std::mem::take(dict));
    transaction.write(number, &written, budget).map_err(store)?;
    let Value::Dict(written) = written else {
        unreachable!("a written dictionary remains a dictionary");
    };
    *dict = written;
    let version = transaction.version();
    let drawing = widgets::restyled(&Reader { version, budget }, dict).map_err(widget_error)?;
    if let Some(drawing) = drawing {
        widgets::write(transaction, number, dict, drawing, budget).map_err(store)?;
        if let Some(drawn) = dict_at(transaction, reference, budget)? {
            *dict = drawn;
        }
    }
    let index = cache.get(&before, budget)?;
    if !index.topology.form.fields.is_empty() {
        let after = transaction.snapshot();
        let changes = before
            .changes(&after, budget)
            .map_err(store)?
            .ok_or_else(|| PdfError::new_err("form versions have different origins"))?;
        let mut appearances = index.appearances.clone();
        appearances.refresh(&after, changes.objects(), changes.trailer(), budget)?;
        let missing = appearances.missing.iter().next().is_some();
        set_need_appearances(transaction, missing, budget)?;
    }
    transaction.retain_derived_version(before);
    Ok(())
}
