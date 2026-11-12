//! Bounded AcroForm field traversal and the object identities it records.
//!
//! The store memoizes the walk with the objects that it read (DESIGN.md
//! section 5). Ported from pylopdf's `rust/src/document/forms.rs` at
//! ebb01d3c4c742046e8ce7fd72957cd15a2933321.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::MAX_CHAIN;
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::value::{Array, Dict, PdfString, Reference, Value};

use super::RADIO;
use super::{MAX_PARENTS, text_string};
use crate::annotations::{AnnotationError as Error, AnnotationResult as Result};

/// The most terminal fields that a walk returns.
const MAX_FIELDS: usize = 4_096;
/// The most field dictionaries that a walk visits.
const MAX_NODES: usize = 4_096;
/// The most `/Fields` and `/Kids` entries that a walk reads.
pub(super) const MAX_EDGES: usize = 8_192;
/// The most levels of the field tree.
pub(super) const MAX_DEPTH: usize = 64;
/// The most widgets below the fields of a walk, and of one field.
const MAX_WIDGETS: usize = 4_096;
/// The bytes of the names and types that a walk reads and builds.
pub(super) const MAX_NAME_BYTES: usize = 1024 * 1024;
/// The bytes of the values that a walk reads and returns, and of the
/// options of a choice field.
pub(super) const MAX_VALUE_BYTES: usize = 1024 * 1024;
/// The most items of the array values of a walk, and options of a choice
/// field.
pub const MAX_VALUE_ITEMS: usize = 4_096;
/// The bytes of the appearance states that one read of a page's widgets
/// returns.
pub(super) const MAX_STATE_BYTES: usize = 1024 * 1024;
/// The UTF-8 bytes of the value that setting a field writes
/// (`form_field_input_size`).
pub(super) const MAX_INPUT_BYTES: usize = 1024 * 1024;
pub(super) const FIELD_TRIGGERS: [&str; 4] = ["K", "F", "V", "C"];

/// An object number and generation.
pub type Id = (u32, u16);

/// A terminal field as the walk read it.
pub(super) struct Field {
    /// The partial names of the field and its ancestors, joined by dots.
    pub(super) name: String,
    pub(super) id: Id,
    pub(super) kind: &'static str,
    /// The inherited `/Ff`.
    pub(super) flags: i64,
    pub(super) value: Option<Arc<str>>,
    /// The widgets that show the field, or the field itself when it has
    /// none.
    pub(super) widgets: Vec<Id>,
    /// Its node in a fill walk's ancestry, absent in an ordinary read.
    pub(super) trace: Option<usize>,
}

#[derive(Clone, Copy)]
pub(super) struct TraceNode {
    pub(super) id: Id,
    pub(super) parent: Option<usize>,
}

/// The terminal fields of a form, as one walk of its field tree read them.
#[derive(Default)]
pub struct Form {
    /// The fields by name and then object.
    pub(super) fields: Vec<Field>,
    /// The position in `fields` of each field's object, by object.
    pub(super) objects: Vec<(Id, usize)>,
    /// The field that owns each widget, the first in `fields` that lists
    /// it, by widget.
    owners: Vec<(Id, usize)>,
    /// Nodes visited by a fill walk, in parent-before-child order.
    pub(super) trace: Vec<TraceNode>,
}

impl Form {
    pub(super) fn field(&self, id: Id) -> Option<&Field> {
        let at = self.objects.binary_search_by_key(&id, |&(id, _)| id).ok()?;
        Some(&self.fields[self.objects[at].1])
    }

    pub(super) fn owner(&self, widget: Id) -> Option<Id> {
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

pub(super) fn limit(message: String) -> Error {
    Error::Pdf(message)
}

pub(super) fn edges_error() -> Error {
    limit(format!(
        "AcroForm field tree exceeds the {MAX_EDGES}-edge safety limit"
    ))
}

pub(super) fn stale((number, _): Id) -> Error {
    Error::Stale(format!("object {number} is no longer a field of the form"))
}

/// Adds `amount` to `total`, refusing a total above `limit`.
pub(super) fn charge(total: &mut usize, amount: usize, limit: usize, label: &str) -> Result<()> {
    *total = total.saturating_add(amount);
    if *total > limit {
        return Err(Error::Pdf(format!(
            "AcroForm {label} text exceeds the {limit}-byte safety limit"
        )));
    }
    Ok(())
}

/// Returns object `id`, or `None` when it is missing.
pub(super) fn object(
    reads: &mut Reads<'_>,
    (number, generation): Id,
) -> Result<Option<Arc<Value>>> {
    Ok(reads
        .get(number)?
        .filter(|object| object.generation() == generation)
        .map(|object| Arc::clone(object.value())))
}

/// Follow object aliases while retaining the final identity and one snapshot.
pub(super) fn object_location(
    reads: &mut Reads<'_>,
    reference: Reference,
) -> Result<Option<(Id, Arc<Value>)>> {
    let budget = reads.budget();
    let mut current = reference;
    let mut seen = [None; MAX_CHAIN];
    for depth in 0..MAX_CHAIN {
        budget.poll()?;
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
    Err(Error::Malformed(format!(
        "a form reference chain exceeds {MAX_CHAIN} references"
    )))
}

/// Resolve a value, charging owned buffers before copying them.
pub(super) fn located(
    reads: &mut Reads<'_>,
    value: Option<&Value>,
) -> Result<Option<(Value, Option<Id>)>> {
    let budget = reads.budget();
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Reference(reference)) => match object_location(reads, *reference)? {
            Some((identity, value)) => copy_for_edit(&value, budget)
                .map(|value| Some((value, Some(identity))))
                .map_err(Error::from),
            None => Ok(None),
        },
        Some(value) => copy_for_edit(value, budget)
            .map(|value| Some((value, None)))
            .map_err(Error::from),
    }
}

pub(super) fn read(reads: &mut Reads<'_>, value: Option<&Value>) -> Result<Option<Value>> {
    Ok(located(reads, value)?.map(|(value, _)| value))
}

/// Charge a fallible allocation before growing an owned input-sized vector.
#[expect(
    clippy::disallowed_methods,
    reason = "MemorySize is charged before fallible reservation; push uses the reserved capacity"
)]
pub(super) fn push<T>(items: &mut Vec<T>, value: T, budget: &Budget) -> Result<()> {
    budget.poll()?;
    if items.len() == items.capacity() {
        let count = items.len().max(4);
        budget.charge(
            LimitCode::MemorySize,
            count.saturating_mul(2 * size_of::<T>()) as u64,
        )?;
        items
            .try_reserve(count)
            .map_err(|_| pyropdf_store::LimitExceeded::new(LimitCode::MemorySize))?;
    }
    items.push(value);
    Ok(())
}

#[expect(
    clippy::disallowed_methods,
    reason = "MemorySize pays for an entry and table overhead before fallible reservation"
)]
pub(super) fn reserve_map<K: Eq + std::hash::Hash, V>(
    map: &mut HashMap<K, V>,
    budget: &Budget,
) -> Result<()> {
    budget.charge(LimitCode::MemorySize, (64 + 2 * size_of::<(K, V)>()) as u64)?;
    map.try_reserve(1)
        .map_err(|_| pyropdf_store::LimitExceeded::new(LimitCode::MemorySize).into())
}

pub(super) const fn id(reference: Reference) -> Id {
    (reference.number(), reference.generation())
}

/// Returns whether `value` reads as an omitted entry: a null, or a
/// reference to a null or to a missing object (ISO 32000-2 sections 7.3.7
/// and 7.3.10).
pub(super) fn omitted(reads: &mut Reads<'_>, value: &Value) -> Result<bool> {
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
pub(super) fn own<'d>(
    reads: &mut Reads<'_>,
    dict: &'d Dict,
    key: &str,
) -> Result<Option<&'d Value>> {
    Ok(match dict.get(key) {
        Some(value) if !omitted(reads, value)? => Some(value),
        _ => None,
    })
}

/// Returns the `/Kids` array of field `dict`.
fn kids(reads: &mut Reads<'_>, dict: &Dict) -> Result<Option<Array>> {
    Ok(match read(reads, dict.get("Kids"))? {
        Some(Value::Array(kids)) => Some(kids),
        _ => None,
    })
}

/// Returns whether `dict` looks like a widget: its `/Subtype` is `Widget`,
/// or it has a `/Rect`.
pub(super) fn looks_like_widget(dict: &Dict) -> bool {
    matches!(dict.get("Subtype"), Some(Value::Name(name)) if name.as_bytes() == b"Widget")
        || dict.get("Rect").is_some()
}

fn classify(reads: &mut Reads<'_>, kid: &Value) -> Result<Kid> {
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
#[expect(
    clippy::disallowed_methods,
    reason = "MemorySize pays for an entry and table overhead before fallible reservation and insertion"
)]
pub(super) fn insert_set(visited: &mut HashSet<Id>, id: Id, budget: &Budget) -> Result<bool> {
    budget.poll()?;
    if visited.contains(&id) {
        return Ok(false);
    }
    budget.charge(LimitCode::MemorySize, (64 + 2 * size_of::<Id>()) as u64)?;
    visited
        .try_reserve(1)
        .map_err(|_| pyropdf_store::LimitExceeded::new(LimitCode::MemorySize))?;
    Ok(visited.insert(id))
}

pub(super) fn visit(visited: &mut HashSet<Id>, id: Id, budget: &Budget) -> Result<bool> {
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

fn add_edges(edges: &mut usize, count: usize) -> Result<()> {
    *edges = edges.saturating_add(count);
    if *edges > MAX_EDGES {
        return Err(edges_error());
    }
    Ok(())
}

/// Decodes text string `encoded`, charging its encoded and decoded bytes
/// to `totals`.
pub(super) fn decode(
    encoded: &PdfString,
    totals: &mut (usize, usize),
    limit: usize,
    label: &str,
    budget: &Budget,
) -> Result<String> {
    charge(
        &mut totals.0,
        encoded.as_bytes().len(),
        limit,
        &format!("encoded {label}"),
    )?;
    budget.charge(
        LimitCode::MemorySize,
        encoded.as_bytes().len().saturating_mul(4) as u64,
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
pub(super) fn text(
    reads: &mut Reads<'_>,
    value: &Value,
    totals: &mut (usize, usize),
    limit: usize,
    label: &str,
) -> Result<Option<String>> {
    match read(reads, Some(value))? {
        Some(Value::String(encoded)) => {
            decode(&encoded, totals, limit, label, reads.budget()).map(Some)
        }
        _ => Ok(None),
    }
}

/// Returns a field value as text: a name, a text string, or the text
/// strings of an array joined by commas.
pub(super) fn field_value(
    reads: &mut Reads<'_>,
    value: &Value,
    totals: &mut (usize, usize),
    items: &mut usize,
) -> Result<Option<String>> {
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
                .charge(LimitCode::MemorySize, name.len().saturating_mul(3) as u64)?;
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
                    values.try_push(decode(&encoded, totals, MAX_VALUE_BYTES, label, budget)?)?;
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
            budget.charge(LimitCode::MemorySize, output as u64)?;
            Ok(Some(values.join(", ")))
        }
        _ => Ok(None),
    }
}

/// Returns the form's `/Fields` array, or `None` for a document without
/// one.
fn top_fields(reads: &mut Reads<'_>) -> Result<Option<Array>> {
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
pub(super) struct Text {
    /// The encoded and decoded bytes of names and types.
    names: (usize, usize),
    /// The bytes of the full names.
    built: usize,
    /// The encoded and decoded bytes of values.
    pub(super) values: (usize, usize),
    /// The items of array values.
    pub(super) items: usize,
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
) -> Result<Entries> {
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
        reads.budget().charge(
            LimitCode::MemorySize,
            built_len.saturating_add(size_of::<String>() + 32) as u64,
        )?;
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
            reads.budget().charge(
                LimitCode::MemorySize,
                field_type.len().saturating_mul(3).saturating_add(32) as u64,
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
    let value = if separate_values {
        let mut value_reads = Reads::new(reads.version(), reads.budget());
        node_value(&mut value_reads, dict, &node.value, text_budget)?
    } else {
        node_value(reads, dict, &node.value, text_budget)?
    };
    Ok((name, field_type, flags, value))
}

pub(super) fn node_value(
    reads: &mut Reads<'_>,
    dict: &Dict,
    inherited: &Option<Arc<str>>,
    text_budget: &mut Text,
) -> Result<Option<Arc<str>>> {
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
                .charge(LimitCode::MemorySize, value.len().saturating_add(32) as u64)?;
            Ok::<Arc<str>, Error>(Arc::from(value))
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
fn terminal_fields(reads: &mut Reads<'_>, tracing: bool) -> Result<(Vec<Leaf>, Vec<TraceNode>)> {
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
    stack.try_reserve_exact(fields.len())?;
    let top = Arc::new(String::new());
    for field in fields.iter() {
        // As in `/Kids`, an omitted entry costs only its edge.
        if let Value::Reference(reference) = field
            && !omitted(reads, field)?
        {
            stack.try_push(Node {
                id: id(*reference),
                prefix: Arc::clone(&top),
                kind: None,
                flags: 0,
                value: None,
                depth: 1,
                role: Role::Field,
                trace_parent: None,
            })?;
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
            trace.try_push(TraceNode {
                id: node.id,
                parent: node.trace_parent,
            })?;
            Some(at)
        } else {
            None
        };
        let (name, field_type, flags, value) =
            node_entries(reads, dict, &node, &mut text_budget, tracing)?;
        let leaf = match node.role {
            Role::Field if field_type.is_some() => {
                leaves.try_push(None)?;
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
                stack.try_push(Node {
                    id: kid,
                    prefix: Arc::clone(&name),
                    kind: field_type.clone(),
                    flags,
                    value: value.clone(),
                    depth: node.depth + 1,
                    role,
                    trace_parent: trace_at,
                })?;
            }
        }
        if let Some(at) = leaf {
            if has_child_fields {
                leaves[at] = None;
            } else if let (Role::Field, Some(field_type)) = (node.role, field_type) {
                budget.charge(LimitCode::MemorySize, name.len() as u64)?;
                leaves[at] = Some(((*name).clone(), node.id, field_type, flags, value, trace_at));
            }
        }
    }
    let mut out = BudgetVec::new(budget);
    for leaf in leaves.into_vec().into_iter().flatten() {
        out.try_push(leaf)?;
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
fn field_widgets(reads: &mut Reads<'_>, field: Id) -> Result<Vec<Id>> {
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
    frames.try_push((top, 0usize))?;
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
            widgets.try_push(kid)?;
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
            frames.try_push((kids, 0))?;
        }
    }
    if widgets.is_empty() {
        widgets.try_push(field)?;
    }
    Ok(widgets.into_vec())
}

/// Walks the form of the version that `reads` reads.
pub fn walk(reads: &mut Reads<'_>) -> Result<Form> {
    walk_with_trace(reads, false)
}

pub(super) fn walk_with_trace(reads: &mut Reads<'_>, tracing: bool) -> Result<Form> {
    let budget = reads.budget();
    let mut fields = BudgetVec::new(budget);
    let (leaves, trace) = terminal_fields(reads, tracing)?;
    for (name, id, field_type, flags, value, trace_at) in leaves {
        fields.try_push(Field {
            name,
            id,
            kind: kind(&field_type, flags),
            flags,
            value,
            widgets: field_widgets(reads, id)?,
            trace: trace_at,
        })?;
    }
    let mut objects = BudgetVec::new(budget);
    let mut owners = BudgetVec::new(budget);
    for (at, field) in fields.iter().enumerate() {
        budget.poll()?;
        objects.try_push((field.id, at))?;
        for &widget in &field.widgets {
            owners.try_push((widget, at))?;
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

/// Returns entry `key` of field `id` or of the nearest field above it that
/// has it, along at most [`MAX_PARENTS`] fields of the `/Parent` chain.
pub(super) fn inherited(reads: &mut Reads<'_>, id: Id, key: &str) -> Result<Option<Value>> {
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
                .map_err(Error::from);
        }
        current = match dict.get("Parent") {
            Some(Value::Reference(parent)) => Some(self::id(*parent)),
            _ => None,
        };
    }
    Ok(None)
}
