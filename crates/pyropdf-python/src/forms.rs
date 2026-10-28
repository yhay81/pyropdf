//! The interactive form (ISO 32000-2 section 12.7): the terminal fields
//! that `doc.form` maps by name, the widgets of pages and of fields, and
//! the repair of radio buttons written as fields of their own (DESIGN.md
//! section 10).
//!
//! The walk of the field tree is memoized with the objects that it read
//! (section 5), so reading every field of a form costs one walk of each
//! version. Every read shares pylopdf's field-tree limits. Ported from
//! pylopdf's `rust/src/document/forms.rs` at
//! ebb01d3c4c742046e8ce7fd72957cd15a2933321.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use pyo3::prelude::*;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::memo::{DerivedMap, ReadDependencies, Reads};
use pyropdf_store::pages::MAX_CHAIN;
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::value::{Array, Dict, Name, PdfString, Reference, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::annotations::{Annots, Rect};
use crate::document::{_Document, Failed, text_string};
use crate::errors::{MalformedPdfError, PdfError, StaleObjectError, limit_error, store_error};
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
}

/// A terminal field that the walk found: its name, object, `/FT`, `/Ff`,
/// and value.
type Leaf = (String, Id, Arc<String>, i64, Option<Arc<str>>);

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
    let value = match own(reads, dict, "V")? {
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
        None => node.value.clone(),
    };
    Ok((name, field_type, flags, value))
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
fn terminal_fields(reads: &mut Reads<'_>) -> PyResult<Vec<Leaf>> {
    let Some(fields) = top_fields(reads)? else {
        return Ok(Vec::new());
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
        let (name, field_type, flags, value) = node_entries(reads, dict, &node, &mut text_budget)?;
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
                leaves[at] = Some(((*name).clone(), node.id, field_type, flags, value));
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
    for (_, _, _, _, value) in &out {
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
    Ok(out.into_vec())
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
    let budget = reads.budget();
    let mut fields = BudgetVec::new(budget);
    for (name, id, field_type, flags, value) in terminal_fields(reads)? {
        fields
            .try_push(Field {
                name,
                id,
                kind: kind(&field_type, flags),
                flags,
                value,
                widgets: field_widgets(reads, id)?,
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
#[allow(dead_code)]
enum AppearanceKind {
    Normal,
    State,
}

/// A resolved value either borrows its container or shares an indirect
/// object. Looking at an appearance never copies its stream data.
#[allow(dead_code)]
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

#[allow(dead_code)]
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
#[allow(dead_code)]
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
#[allow(dead_code)]
struct IndexedWidget {
    field: Id,
    widget: Id,
    kind: AppearanceKind,
}

/// Recorded widget appearance results and the reads that produced them.
/// The enclosing form index is responsible for discovering widget membership
/// and comparing versions before asking this index to refresh.
#[derive(Clone)]
#[allow(dead_code)]
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

#[allow(dead_code)]
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
            if let Some(widget) = next.widgets.get(key).copied() {
                next.record(key, widget, version, budget)?;
            }
        }
        budget.poll().map_err(|e| limit_error(&e))?;
        *self = next;
        Ok(())
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
}
