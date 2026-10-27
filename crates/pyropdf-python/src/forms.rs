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

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use pyo3::exceptions::PyNotImplementedError;
use pyo3::prelude::*;
use pyropdf_engine::annotations::Reader;
use pyropdf_engine::forms::{self as widgets, WidgetError};
use pyropdf_engine::standard::is_winansi;
use pyropdf_store::budget::Budget;
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::MAX_CHAIN;
use pyropdf_store::settle::{Scope, Structure};
use pyropdf_store::value::{Array, Dict, Name, PdfString, Reference, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::annotations::{Annots, Rect};
use crate::document::{_Document, Failed, text_string};
use crate::errors::{LimitError, PdfError, StaleObjectError, store_error};
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
/// The `/Ff` bit of radio buttons.
const RADIO: i64 = 1 << 15;
/// The keys of a radio button that a repaired group gives to its new
/// parent field.
const RADIO_KEYS: [&str; 7] = ["FT", "T", "TU", "TM", "Ff", "V", "DV"];

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

/// Follows references from `value` through `reads`, at most [`MAX_CHAIN`];
/// a null, an absent object, a missing entry, a reference cycle, or a
/// longer chain is `None`, so a malformed entry reads as unusable.
fn read(reads: &mut Reads<'_>, value: Option<&Value>) -> PyResult<Option<Value>> {
    let mut current = match value {
        None | Some(Value::Null) => return Ok(None),
        Some(Value::Reference(reference)) => *reference,
        Some(value) => return Ok(Some(value.clone())),
    };
    for _ in 0..MAX_CHAIN {
        let Some(value) = object(reads, id(current))? else {
            return Ok(None);
        };
        match &*value {
            Value::Null => return Ok(None),
            Value::Reference(next) => current = *next,
            value => return Ok(Some(value.clone())),
        }
    }
    Ok(None)
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
    let kid = id(*reference);
    let Some(value) = object(reads, kid)? else {
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
fn visit(visited: &mut HashSet<Id>, id: Id) -> PyResult<bool> {
    if !visited.insert(id) {
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
) -> PyResult<String> {
    charge(
        &mut totals.0,
        encoded.as_bytes().len(),
        limit,
        &format!("encoded {label}"),
    )?;
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
        Some(Value::String(encoded)) => decode(&encoded, totals, limit, label).map(Some),
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
            let decoded = String::from_utf8_lossy(name).into_owned();
            charge(
                &mut totals.1,
                decoded.len(),
                MAX_VALUE_BYTES,
                "decoded field-value",
            )?;
            Ok(Some(decoded))
        }
        Some(Value::String(encoded)) => decode(&encoded, totals, MAX_VALUE_BYTES, label).map(Some),
        Some(Value::Array(array)) => {
            *items = items.saturating_add(array.len());
            if *items > MAX_VALUE_ITEMS {
                return Err(limit(format!(
                    "AcroForm field values exceed the {MAX_VALUE_ITEMS}-item safety limit"
                )));
            }
            let mut values = Vec::new();
            for item in array.iter() {
                if let Some(Value::String(encoded)) = read(reads, Some(item))? {
                    values.push(decode(&encoded, totals, MAX_VALUE_BYTES, label)?);
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
            Ok(Some(values.join(", ")))
        }
        _ => Ok(None),
    }
}

/// Returns the form's `/Fields` array, or `None` for a document without
/// one.
fn top_fields(reads: &mut Reads<'_>) -> PyResult<Option<Array>> {
    let root = reads.trailer().get("Root").cloned();
    let Some(Value::Dict(catalog)) = read(reads, root.as_ref())? else {
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
    // A node without a partial name shares the name above it.
    let name = match component {
        Some(component) if node.prefix.is_empty() => Arc::new(component),
        Some(component) => Arc::new(format!("{}.{component}", node.prefix)),
        None => Arc::clone(&node.prefix),
    };
    charge(
        &mut text_budget.built,
        name.len(),
        MAX_NAME_BYTES,
        "materialized field-name",
    )?;
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
        .map(Arc::from),
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
    let mut leaves: Vec<Option<Leaf>> = Vec::new();
    let mut stack = Vec::with_capacity(fields.len());
    let top = Arc::new(String::new());
    for field in fields.iter() {
        // As in `/Kids`, an omitted entry costs only its edge.
        if let Value::Reference(reference) = field
            && !omitted(reads, field)?
        {
            stack.push(Node {
                id: id(*reference),
                prefix: Arc::clone(&top),
                kind: None,
                flags: 0,
                value: None,
                depth: 1,
                role: Role::Field,
            });
        }
    }
    let mut visited = HashSet::new();
    let mut edges = fields.len();
    let mut widgets = 0usize;
    let mut text_budget = Text::default();
    while let Some(node) = stack.pop() {
        if !visit(&mut visited, node.id)? {
            continue;
        }
        let Some(value) = object(reads, node.id)? else {
            continue;
        };
        let Value::Dict(dict) = &*value else {
            continue;
        };
        let (name, field_type, flags, value) = node_entries(reads, dict, &node, &mut text_budget)?;
        let leaf = match node.role {
            Role::Field if field_type.is_some() => {
                leaves.push(None);
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
                    return Err(limit(format!(
                        "AcroForm field tree exceeds the {MAX_DEPTH}-level safety limit"
                    )));
                }
                stack.push(Node {
                    id: kid,
                    prefix: Arc::clone(&name),
                    kind: field_type.clone(),
                    flags,
                    value: value.clone(),
                    depth: node.depth + 1,
                    role,
                });
            }
        }
        if let Some(at) = leaf {
            if has_child_fields {
                leaves[at] = None;
            } else if let (Role::Field, Some(field_type)) = (node.role, field_type) {
                leaves[at] = Some(((*name).clone(), node.id, field_type, flags, value));
            }
        }
    }
    let mut out: Vec<Leaf> = leaves.into_iter().flatten().collect();
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
    Ok(out)
}

/// Returns the widgets below field `field` in the order of its kids, or the
/// field itself when it has none.
///
/// The widgets of a nameless intermediate take its place among the kids,
/// and each intermediate is expanded once, so that a cycle ends. A walk that
/// returned the field visited every intermediate below it within the walk's
/// limits; the limits here keep this traversal bounded on its own.
fn field_widgets(reads: &mut Reads<'_>, field: Id) -> PyResult<Vec<Id>> {
    let mut widgets = Vec::new();
    let top = match object(reads, field)? {
        Some(value) => match &*value {
            Value::Dict(dict) => kids(reads, dict)?.unwrap_or_default(),
            _ => Array::new(),
        },
        None => Array::new(),
    };
    let mut edges = 0;
    add_edges(&mut edges, top.len())?;
    let mut frames = vec![(top, 0usize)];
    let mut expanded = HashSet::new();
    while let Some((kids, at)) = frames.last_mut() {
        let Some(kid) = kids.get(*at).cloned() else {
            frames.pop();
            continue;
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
            widgets.push(kid);
        }
        let Some(kids) = folded else {
            continue;
        };
        // The field is the first expanded node, so a cycle back to it ends.
        if expanded.is_empty() {
            visit(&mut expanded, field)?;
        }
        if visit(&mut expanded, kid)? {
            add_edges(&mut edges, kids.len())?;
            frames.push((kids, 0));
        }
    }
    if widgets.is_empty() {
        widgets.push(field);
    }
    Ok(widgets)
}

/// Walks the form of the version that `reads` reads.
fn walk(reads: &mut Reads<'_>) -> PyResult<Form> {
    let mut fields = Vec::new();
    for (name, id, field_type, flags, value) in terminal_fields(reads)? {
        fields.push(Field {
            name,
            id,
            kind: kind(&field_type, flags),
            flags,
            value,
            widgets: field_widgets(reads, id)?,
        });
    }
    let mut objects: Vec<(Id, usize)> = fields
        .iter()
        .enumerate()
        .map(|(at, field)| (field.id, at))
        .collect();
    objects.sort_unstable();
    let mut owners: Vec<(Id, usize)> = fields
        .iter()
        .enumerate()
        .flat_map(|(at, field)| field.widgets.iter().map(move |&widget| (widget, at)))
        .collect();
    owners.sort_unstable();
    owners.dedup_by_key(|&mut (widget, _)| widget);
    Ok(Form {
        fields,
        objects,
        owners,
    })
}

/// Returns the version that reads see.
fn view(document: &_Document) -> PyResult<(Arc<Version>, Budget)> {
    Ok((document.reading_parent()?.0, document.budget()))
}

/// Returns the form of the version that reads see, memoized.
fn memoized(document: &_Document) -> PyResult<Arc<Form>> {
    let (version, parent) = document.reading_parent()?;
    let budget = document.budget();
    let (memo, version) = match &parent {
        // An open transaction that has written nothing reads its parent.
        Some(parent) => (&document.form, parent),
        None if document.in_transaction() => (&document.draft_form, &version),
        None => (&document.form, &version),
    };
    memo.get_or_compute(&(), version, &budget, |reads| walk(reads).map_err(Failed))
        .map_err(|Failed(error)| error)
}

/// Returns the full name and object of every terminal field, by name and
/// then object.
#[pyfunction]
pub fn form_fields(py: Python<'_>, document: &_Document) -> PyResult<Vec<(String, Id)>> {
    let form = py.detach(|| memoized(document))?;
    Ok(form
        .fields
        .iter()
        .map(|field| (field.name.clone(), field.id))
        .collect())
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
    let form = py.detach(|| memoized(document))?;
    let field = form.field(id).ok_or_else(|| stale(id))?;
    Ok((
        field.name.clone(),
        field.kind,
        field.flags,
        field.value.as_deref().map(str::to_owned),
    ))
}

/// Returns entry `key` of field `id` or of the nearest field above it that
/// has it, along at most [`MAX_PARENTS`] fields of the `/Parent` chain.
fn inherited(reads: &mut Reads<'_>, id: Id, key: &str) -> PyResult<Option<Value>> {
    let mut current = Some(id);
    let mut visited = HashSet::new();
    while let Some(id) = current {
        if !visited.insert(id) || visited.len() > MAX_PARENTS {
            return Ok(None);
        }
        let Some(value) = object(reads, id)? else {
            return Ok(None);
        };
        let Value::Dict(dict) = &*value else {
            return Ok(None);
        };
        if let Some(value) = own(reads, dict, key)? {
            return Ok(Some(value.clone()));
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
        memoized(document)?.field(id).ok_or_else(|| stale(id))?;
        let (version, budget) = view(document)?;
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
        let mut choices = Vec::new();
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
                    Some(value) => (value.clone(), value),
                    None => continue,
                },
            };
            choices.push(choice);
        }
        Ok(choices)
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
        let form = memoized(document)?;
        let field = form.field(id).ok_or_else(|| stale(id))?;
        let widgets: HashSet<Id> = field.widgets.iter().copied().collect();
        let (version, index, budget) = index(document)?;
        let mut reads = Reads::new(&version, &budget);
        let mut out = Vec::new();
        for page in index.pages() {
            let Some(object) = page.object() else {
                continue;
            };
            let Some(Value::Array(items)) = read(&mut reads, page.dict().get("Annots"))? else {
                continue;
            };
            for (at, item) in items.iter().enumerate() {
                if let Value::Reference(reference) = item
                    && widgets.contains(&self::id(*reference))
                {
                    let at = u32::try_from(at).unwrap_or(u32::MAX);
                    out.push((self::id(object), self::id(*reference), at));
                }
            }
        }
        Ok(out)
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
        let state = String::from_utf8_lossy(name).into_owned();
        charge(
            &mut totals.1,
            state.len(),
            MAX_STATE_BYTES,
            "returned widget-state",
        )?;
        Ok(state)
    };
    let mut states = Vec::new();
    if let Some(Value::Dict(appearance)) = annots.resolved(dict.get("AP"))?
        && let Some(Value::Dict(normal)) = annots.resolved(appearance.get("N"))?
    {
        for name in normal.keys() {
            states.push(state(name)?);
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
        states,
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
        let Some(annots) = Annots::read(document, number, generation)? else {
            return Ok(None);
        };
        let form = memoized(document)?;
        let mut records = Vec::new();
        let mut totals = (0, 0);
        for (index, item) in annots.items.iter().enumerate() {
            let Some((id, dict)) = annots.entry(item)? else {
                continue;
            };
            if let Some(record) = widget(&annots, &form, index, id, &dict, &mut totals)? {
                records.push(record);
            }
        }
        Ok(Some(records))
    })
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
        let Some(annots) = Annots::read(document, number, generation)? else {
            return Ok(None);
        };
        let (position, dict) = annots.find(index, id)?;
        let form = memoized(document)?;
        widget(&annots, &form, position, id, &dict, &mut (0, 0))?
            .map(Some)
            .ok_or_else(|| {
                StaleObjectError::new_err(format!("annotation {} is no longer a widget", id.0))
            })
    })
}

/// Where the form's `/Fields` array is written: as an object of its own,
/// in the form dictionary's object, or in the catalog's.
enum Holder {
    Array(u32),
    Form(Id),
    Catalog(Id),
}

/// Returns where the form's `/Fields` array is written, and the array.
fn holder(reads: &mut Reads<'_>) -> PyResult<Option<(Holder, Array)>> {
    let Some(Value::Reference(root)) = reads.trailer().get("Root").cloned() else {
        return Ok(None);
    };
    let Some(catalog) = object(reads, id(root))? else {
        return Ok(None);
    };
    let Value::Dict(catalog) = &*catalog else {
        return Ok(None);
    };
    let (form, owner) = match catalog.get("AcroForm") {
        Some(Value::Reference(reference)) => match object(reads, id(*reference))? {
            Some(form) => match &*form {
                Value::Dict(form) => (form.clone(), Holder::Form(id(*reference))),
                _ => return Ok(None),
            },
            None => return Ok(None),
        },
        Some(Value::Dict(form)) => (form.clone(), Holder::Catalog(id(root))),
        _ => return Ok(None),
    };
    let owner = match form.get("Fields") {
        Some(Value::Reference(reference)) => Holder::Array(reference.number()),
        _ => owner,
    };
    Ok(match read(reads, form.get("Fields"))? {
        Some(Value::Array(fields)) => Some((owner, fields)),
        _ => None,
    })
}

/// A radio group to repair: the name its buttons share and their objects.
type Group = (PdfString, Vec<Reference>);

/// Returns the top-level radio buttons that share a name, each a field
/// merged with its widget, by name in `/Fields` order.
fn radio_groups(reads: &mut Reads<'_>, fields: &Array) -> PyResult<Vec<Group>> {
    if fields.len() > MAX_EDGES {
        return Err(edges_error());
    }
    let mut groups: Vec<Group> = Vec::new();
    let mut positions: HashMap<Vec<u8>, usize> = HashMap::new();
    for item in fields.iter() {
        let Value::Reference(reference) = item else {
            continue;
        };
        let Some(value) = object(reads, id(*reference))? else {
            continue;
        };
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
        match positions.get(title.as_bytes()) {
            Some(&at) => groups[at].1.push(*reference),
            None => {
                positions.insert(title.as_bytes().to_vec(), groups.len());
                groups.push((title, vec![*reference]));
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
    let groups = py.detach(|| {
        let (version, budget) = view(document)?;
        let mut reads = Reads::new(&version, &budget);
        match holder(&mut reads)? {
            Some((_, fields)) => radio_groups(&mut reads, &fields),
            None => Ok(Vec::new()),
        }
    })?;
    if groups.is_empty() {
        return Ok(0);
    }
    document.edit(py, |transaction, budget| {
        let store = |e: pyropdf_store::Error| store_error(&e);
        let before = transaction.snapshot();
        let mut reads = Reads::new(&before, budget);
        let Some((owner, fields)) = holder(&mut reads)? else {
            return Ok(0);
        };
        let groups = radio_groups(&mut reads, &fields)?;
        let mut replaced: HashMap<Reference, Option<Reference>> = HashMap::new();
        for (title, members) in &groups {
            let first = object(&mut reads, id(members[0]))?;
            let Some(Value::Dict(first)) = first.as_deref() else {
                continue;
            };
            let mut parent = Dict::new();
            parent.insert(Name::from("FT"), Value::Name(Name::from("Btn")));
            parent.insert(Name::from("T"), Value::String(title.clone()));
            let kids = members.iter().map(|&member| Value::Reference(member));
            parent.insert(
                Name::from("Kids"),
                Value::Array(Array::from(kids.collect::<Vec<_>>())),
            );
            for key in ["Ff", "TU", "TM", "DV"] {
                if let Some(value) = first.get(key) {
                    parent.insert(Name::from(key), value.clone());
                }
            }
            // The group's value is the first value that one of its buttons
            // holds.
            for &member in members {
                if let Some(Value::Dict(member)) = object(&mut reads, id(member))?.as_deref()
                    && let Some(value) = member.get("V")
                    && read(&mut reads, Some(value))?.is_some()
                {
                    parent.insert(Name::from("V"), value.clone());
                    break;
                }
            }
            let reference = transaction.allocate().map_err(store)?;
            transaction
                .write(reference.number(), &Value::Dict(parent), budget)
                .map_err(store)?;
            for &member in members {
                let Some(Value::Dict(dict)) = object(&mut reads, id(member))?.as_deref().cloned()
                else {
                    continue;
                };
                let mut dict = dict;
                for key in RADIO_KEYS {
                    dict.remove(key);
                }
                dict.insert(Name::from("Parent"), Value::Reference(reference));
                transaction
                    .write(member.number(), &Value::Dict(dict), budget)
                    .map_err(store)?;
                replaced.insert(member, (member == members[0]).then_some(reference));
            }
        }
        let fields: Vec<Value> = fields
            .iter()
            .filter_map(|item| match item {
                Value::Reference(reference) => match replaced.get(reference) {
                    Some(Some(parent)) => Some(Value::Reference(*parent)),
                    Some(None) => None,
                    None => Some(item.clone()),
                },
                _ => Some(item.clone()),
            })
            .collect();
        let fields = Value::Array(Array::from(fields));
        let (number, value) = match owner {
            Holder::Array(number) => (number, fields),
            Holder::Form(form) => {
                let Some(Value::Dict(mut dict)) = object(&mut reads, form)?.as_deref().cloned()
                else {
                    return Ok(0);
                };
                dict.insert(Name::from("Fields"), fields);
                (form.0, Value::Dict(dict))
            }
            Holder::Catalog((number, generation)) => {
                let catalog = object(&mut reads, (number, generation))?;
                let Some(Value::Dict(mut catalog)) = catalog.as_deref().cloned() else {
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
    })
}

/// Returns `text` as a text string: ASCII without control characters as it
/// is, and else UTF-16BE, in which the line breaks and other controls that
/// PDFDocEncoding leaves unmapped read back.
fn encode_value(text: &str) -> Value {
    let bytes = if text.is_ascii() && !text.chars().any(char::is_control) {
        text.as_bytes().to_vec()
    } else {
        let mut bytes = vec![0xFE, 0xFF];
        bytes.extend(text.encode_utf16().flat_map(u16::to_be_bytes));
        bytes
    };
    Value::String(PdfString::literal(bytes))
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

/// Sets the value of field `id`, a text or choice field, to `value`, and
/// draws its widgets through the appearance settler, with those of the
/// other text and choice fields that have no appearance yet; then sets the
/// form's `/NeedAppearances` when a widget is left without one. Refuses
/// what pylopdf refuses, leaving the document as it was.
#[pyfunction]
pub fn form_field_fill(py: Python<'_>, document: &_Document, id: Id, value: &str) -> PyResult<()> {
    if value.len() > MAX_INPUT_BYTES {
        return Err(LimitError::new_err((
            "form_field_input_size",
            format!("form field value exceeds the {MAX_INPUT_BYTES}-byte UTF-8 limit"),
        )));
    }
    document.edit(py, |transaction, budget| {
        let savepoint = transaction.savepoint();
        let result = fill(transaction, id, value, budget);
        if result.is_err() {
            transaction.rollback(savepoint);
        }
        result
    })
}

fn fill(transaction: &mut Transaction, id: Id, value: &str, budget: &Budget) -> PyResult<()> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let before = transaction.snapshot();
    let mut reads = Reads::new(&before, budget);
    let form = walk(&mut reads)?;
    let field = form.field(id).ok_or_else(|| stale(id))?;
    match field.kind {
        kind if shows_text(kind) => {}
        "pushbutton" => {
            return Err(PdfError::new_err(
                "pushbutton fields do not have fillable values",
            ));
        }
        "checkbox" | "radio" => {
            return Err(PyNotImplementedError::new_err(
                "setting the value of a check box or radio button is not supported yet",
            ));
        }
        "signature" => {
            return Err(PdfError::new_err(
                "filling signature fields is not supported (see the pyHanko integration for digital signatures)",
            ));
        }
        _ => return Err(PdfError::new_err("unsupported field type")),
    }
    let Some(Value::Dict(mut dict)) = object(&mut reads, id)?.as_deref().cloned() else {
        return Err(stale(id));
    };
    dict.insert(Name::from("V"), encode_value(value));
    transaction
        .write(id.0, &Value::Dict(dict), budget)
        .map_err(store)?;
    // The widgets of the field are drawn or refused with the reason; those
    // of other fields that have no appearance are drawn where they can be.
    let mut marks = Vec::new();
    {
        let version = transaction.version();
        let reader = Reader { version, budget };
        for &widget in &field.widgets {
            if let Some(dict) = widget_of(version, widget, budget)?
                && let Value::Dict(dict) = &*dict
            {
                widgets::appearance(&reader, dict).map_err(widget_error)?;
                marks.push(widget.0);
            }
        }
        for other in &form.fields {
            if other.name == field.name
                || !shows_text(other.kind)
                || !other.value.as_deref().is_none_or(is_winansi)
            {
                continue;
            }
            for &widget in &other.widgets {
                if let Some(dict) = widget_of(version, widget, budget)?
                    && let Value::Dict(dict) = &*dict
                    && !widgets::has_normal_stream(&reader, dict).map_err(store)?
                {
                    marks.push(widget.0);
                }
            }
        }
    }
    for number in marks {
        transaction
            .mark(Structure::Appearance, number, budget)
            .map_err(store)?;
    }
    transaction
        .settle(Scope::One(Structure::Appearance), budget)
        .map_err(store)?;
    let complete = complete(transaction.version(), &form, budget)?;
    set_need_appearances(transaction, !complete, budget)
}

/// Returns whether every widget of a text, choice, check box, or radio
/// field of `form` has an appearance to show.
fn complete(version: &Version, form: &Form, budget: &Budget) -> PyResult<bool> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let reader = Reader { version, budget };
    for field in &form.fields {
        let buttons = matches!(field.kind, "checkbox" | "radio");
        if !buttons && !shows_text(field.kind) {
            continue;
        }
        for &widget in &field.widgets {
            let Some(dict) = widget_of(version, widget, budget)? else {
                continue;
            };
            let Value::Dict(dict) = &*dict else {
                continue;
            };
            let shown = if buttons {
                widgets::has_state_stream(&reader, dict)
            } else {
                widgets::has_normal_stream(&reader, dict)
            };
            if !shown.map_err(store)? {
                return Ok(false);
            }
        }
    }
    Ok(true)
}

/// Sets the form's `/NeedAppearances` to `value` where the form dictionary
/// is written: as an object of its own, or in the catalog's.
fn set_need_appearances(
    transaction: &mut Transaction,
    value: bool,
    budget: &Budget,
) -> PyResult<()> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let version = transaction.snapshot();
    let mut reads = Reads::new(&version, budget);
    let Some(Value::Reference(root)) = reads.trailer().get("Root").cloned() else {
        return Ok(());
    };
    let Some(Value::Dict(mut catalog)) = object(&mut reads, id(root))?.as_deref().cloned() else {
        return Ok(());
    };
    let (number, mut form) = match catalog.get("AcroForm") {
        Some(Value::Reference(reference)) => match object(&mut reads, id(*reference))?.as_deref() {
            Some(Value::Dict(form)) => (Some(reference.number()), form.clone()),
            _ => return Ok(()),
        },
        Some(Value::Dict(form)) => (None, form.clone()),
        _ => return Ok(()),
    };
    if form.get("NeedAppearances") == Some(&Value::Boolean(value)) {
        return Ok(());
    }
    form.insert(Name::from("NeedAppearances"), Value::Boolean(value));
    let (number, value) = match number {
        Some(number) => (number, Value::Dict(form)),
        None => {
            catalog.insert(Name::from("AcroForm"), Value::Dict(form));
            (root.number(), Value::Dict(catalog))
        }
    };
    transaction.write(number, &value, budget).map_err(store)?;
    Ok(())
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
