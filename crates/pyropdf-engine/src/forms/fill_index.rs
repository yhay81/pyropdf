//! The field topology, values, and widget appearances recorded by a fill.

use std::sync::{Arc, Mutex, PoisonError, Weak};

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::memo::{DerivedMap, ReadDependencies, ReadSet, Reads};
use pyropdf_store::pages::MAX_CHAIN;
use pyropdf_store::value::{Dict, Value};
use pyropdf_store::version::Version;

use super::traversal::{Form, MAX_DEPTH, TraceNode, edges_error, walk_with_trace};
use super::values::{
    Id, MAX_VALUE_BYTES, MAX_VALUE_ITEMS, Text, charge, field_value, id, limit, looks_like_widget,
    node_value, object, object_location, own,
};
use crate::annotations::{AnnotationError as Error, AnnotationResult as Result};

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
) -> Result<Option<AppearanceValue<'a>>> {
    let Some(value) = value else {
        return Ok(None);
    };
    let Value::Reference(reference) = value else {
        return Ok(Some(AppearanceValue::Direct(value)));
    };
    let mut reference = *reference;
    for _ in 0..MAX_CHAIN {
        reads.budget().poll()?;
        let Some(object) = object(reads, id(reference))? else {
            return Ok(None);
        };
        match &*object {
            Value::Reference(next) => reference = *next,
            _ => return Ok(Some(AppearanceValue::Indirect(object))),
        }
    }
    Err(Error::Malformed(
        "an annotation appearance reference chain is too long".into(),
    ))
}

/// Returns `None` for a missing or non-widget object, or whether the
/// widget has a nonempty appearance for its field's kind.
fn widget_appearance(
    reads: &mut Reads<'_>,
    widget: Id,
    kind: AppearanceKind,
) -> Result<Option<bool>> {
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
pub(super) struct IndexedWidget {
    pub(super) field: Id,
    pub(super) widget: Id,
    kind: AppearanceKind,
}

/// Recorded widget appearance results and the reads that produced them.
/// The enclosing form index is responsible for discovering widget membership
/// and comparing versions before asking this index to refresh.
#[derive(Clone)]
pub(super) struct AppearanceIndex {
    widgets: DerivedMap<IndexedWidget>,
    pub(super) missing: DerivedMap<IndexedWidget>,
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
    ) -> Result<()> {
        let mut reads = Reads::new(version, budget);
        let present = widget_appearance(&mut reads, widget.widget, widget.kind)?;
        let dependencies = reads.finish()?;
        let mut next = self.clone();
        next.dependencies.replace(key, Some(dependencies), budget)?;
        next.widgets.insert(key, widget, budget)?;
        if present == Some(false) {
            next.missing.insert(key, widget, budget)?;
        } else {
            next.missing.remove(key, budget)?;
        }
        budget.poll()?;
        *self = next;
        Ok(())
    }

    /// Refreshes only entries whose recorded reads intersect the changes.
    /// A failure leaves the whole index unchanged.
    pub(super) fn refresh(
        &mut self,
        version: &Version,
        changed: &[u32],
        trailer_changed: bool,
        budget: &Budget,
    ) -> Result<()> {
        let affected = self
            .dependencies
            .affected(changed, trailer_changed, budget)?;
        let mut next = self.clone();
        for &key in affected.iter() {
            budget.poll()?;
            let Some(widget) = next.widgets.get(key).copied() else {
                return Err(Error::Malformed(
                    "an appearance dependency names a missing widget".into(),
                ));
            };
            next.record(key, widget, version, budget)?;
        }
        budget.poll()?;
        *self = next;
        Ok(())
    }
}

/// Form membership without field values. Value and appearance changes are
/// checked separately from membership; callers must never use the discarded
/// values from this cached walk.
pub(super) struct FormTopology {
    pub(super) form: Form,
    reads: ReadSet,
    nodes: DerivedMap<u16>,
    /// Catalog and form holders can also be malformed field aliases. Their
    /// changes always require another walk.
    protected: [Option<u32>; 2],
}

impl FormTopology {
    fn build(version: &Version, budget: &Budget) -> Result<Self> {
        let mut reads = Reads::new(version, budget);
        let mut form = walk_with_trace(&mut reads, true)?;
        let mut nodes = DerivedMap::new();
        for node in &form.trace {
            nodes.insert(node.id.0, node.id.1, budget)?;
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
            reads: reads.finish()?,
            nodes,
            protected,
        })
    }

    /// Whether the recorded membership is still valid. A change to a known
    /// terminal field or widget is harmless only when its structural entries
    /// have the same bounded representation. Unknown roles and changes to a
    /// catalog or form holder conservatively require a fresh walk.
    fn unchanged(&self, old: &Version, new: &Version, budget: &Budget) -> Result<bool> {
        let Some(changes) = old.changes(new, budget)? else {
            return Ok(false);
        };
        if self.reads.is_disjoint(&changes) {
            return Ok(true);
        }
        if changes.trailer() && self.reads.trailer() {
            return Ok(false);
        }
        for &number in changes.objects() {
            budget.poll()?;
            if self.reads.objects().binary_search(&number).is_err() {
                continue;
            }
            if self.protected.contains(&Some(number)) {
                return Ok(false);
            }
            let Some(before) = old.get(number, budget)? else {
                return Ok(false);
            };
            let Some(after) = new.get(number, budget)? else {
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
fn same_field_structure(before: &Dict, after: &Dict, budget: &Budget) -> Result<bool> {
    for key in ["T", "FT", "Ff", "Subtype"] {
        budget.poll()?;
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
                budget.poll()?;
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
) -> Result<bool> {
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

fn same_structure_bytes(left: &[u8], right: &[u8], budget: &Budget) -> Result<bool> {
    if left.len() != right.len() {
        return Ok(false);
    }
    for (left, right) in left.chunks(4096).zip(right.chunks(4096)) {
        budget.poll()?;
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
    fn build(topology: &FormTopology, version: &Version, budget: &Budget) -> Result<Self> {
        let mut index = Self::default();
        for (at, node) in topology.form.trace.iter().enumerate() {
            budget.poll()?;
            let key = u32::try_from(at).map_err(|_| edges_error())?;
            let (metric, reads) = value_metric(version, node.id, budget)?;
            index.encoded = index.encoded.saturating_add(metric.encoded);
            index.decoded = index.decoded.saturating_add(metric.decoded);
            index.items = index.items.saturating_add(metric.items);
            index.nodes.insert(key, metric, budget)?;
            index.node_reads.replace(key, Some(reads), budget)?;
        }
        for (at, field) in topology.form.fields.iter().enumerate() {
            budget.poll()?;
            let key = u32::try_from(at).map_err(|_| edges_error())?;
            let (value, reads) = traced_value(version, &topology.form.trace, field.trace, budget)?;
            index.returned = index
                .returned
                .saturating_add(value.as_ref().map_or(0, |value| value.len()));
            index.leaves.insert(key, value, budget)?;
            index.leaf_reads.replace(key, Some(reads), budget)?;
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
    ) -> Result<()> {
        let nodes = self.node_reads.affected(changed, trailer_changed, budget)?;
        let leaves = self.leaf_reads.affected(changed, trailer_changed, budget)?;
        let mut next = self.clone();
        for &key in nodes.iter() {
            budget.poll()?;
            let Some(old) = next.nodes.get(key).copied() else {
                return Err(Error::Malformed(
                    "a value dependency names a missing node".into(),
                ));
            };
            let Some(node) = topology.form.trace.get(key as usize) else {
                return Err(Error::Malformed(
                    "a value dependency names a missing trace".into(),
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
            next.nodes.insert(key, metric, budget)?;
            next.node_reads.replace(key, Some(reads), budget)?;
        }
        for &key in leaves.iter() {
            budget.poll()?;
            let Some(old) = next.leaves.get(key) else {
                return Err(Error::Malformed(
                    "a value dependency names a missing value".into(),
                ));
            };
            let Some(field) = topology.form.fields.get(key as usize) else {
                return Err(Error::Malformed(
                    "a value dependency names a missing field".into(),
                ));
            };
            let old_len = old.as_ref().map_or(0, |value| value.len());
            let (value, reads) = traced_value(version, &topology.form.trace, field.trace, budget)?;
            next.returned = next
                .returned
                .saturating_sub(old_len)
                .saturating_add(value.as_ref().map_or(0, |value| value.len()));
            next.leaves.insert(key, value, budget)?;
            next.leaf_reads.replace(key, Some(reads), budget)?;
        }
        next.check_limits()?;
        budget.poll()?;
        *self = next;
        Ok(())
    }

    fn check_limits(&self) -> Result<()> {
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
) -> Result<(ValueMetric, ReadSet)> {
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
        reads.finish()?,
    ))
}

fn traced_value(
    version: &Version,
    trace: &[TraceNode],
    start: Option<usize>,
    budget: &Budget,
) -> Result<(Option<Arc<str>>, ReadSet)> {
    let mut reads = Reads::new(version, budget);
    let Some(start) = start else {
        return Err(Error::Malformed("a field has no value trace".into()));
    };
    let mut at = Some(start);
    let mut result = None;
    let mut depth = 0;
    while let Some(index) = at {
        budget.poll()?;
        if depth >= MAX_DEPTH {
            return Err(limit(format!(
                "AcroForm field tree exceeds the {MAX_DEPTH}-level safety limit"
            )));
        }
        let Some(node) = trace.get(index) else {
            return Err(Error::Malformed(
                "a form value trace names a missing node".into(),
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
                    budget.charge(LimitCode::MemorySize, value.len().saturating_add(32) as u64)?;
                    Ok::<Arc<str>, Error>(Arc::from(value))
                })
                .transpose()?;
            break;
        }
        at = node.parent;
    }
    Ok((result, reads.finish()?))
}

/// Form membership, values and widget appearances, with their recorded reads.
#[derive(Clone)]
pub(super) struct FillIndex {
    pub(super) topology: Arc<FormTopology>,
    pub(super) appearances: AppearanceIndex,
    values: ValueIndex,
}

impl FillIndex {
    fn build(version: &Version, budget: &Budget) -> Result<Self> {
        let topology = FormTopology::build(version, budget)?;
        budget.charge(
            LimitCode::MemorySize,
            (size_of::<FormTopology>() + 2 * size_of::<usize>()) as u64,
        )?;
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
                budget.poll()?;
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
    fn update(&self, old: &Version, new: &Version, budget: &Budget) -> Result<Self> {
        if !self.topology.unchanged(old, new, budget)? {
            return Self::build(new, budget);
        }
        let Some(changes) = old.changes(new, budget)? else {
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
    ) -> Result<Self> {
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
pub struct FillCache {
    entry: Mutex<Option<FillEntry>>,
}

impl FillCache {
    /// Creates an empty fill cache.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Drops the cached index and its pending changes.
    pub fn clear(&self) {
        self.entry
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
    }

    pub(super) fn get(&self, version: &Arc<Version>, budget: &Budget) -> Result<Arc<FillIndex>> {
        budget.poll()?;
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
                    budget.poll()?;
                    changed.try_push(number)?;
                }
                if changed.is_empty() && !entry.trailer && Arc::ptr_eq(&old, version) {
                    return Ok(entry.index);
                }
                let mut trailer = entry.trailer;
                if !Arc::ptr_eq(&old, version) {
                    if !entry.index.topology.unchanged(&old, version, budget)? {
                        return self.install(version, FillIndex::build(version, budget)?, budget);
                    }
                    let Some(changes) = old.changes(version, budget)? else {
                        return self.install(version, FillIndex::build(version, budget)?, budget);
                    };
                    for &number in changes.objects() {
                        changed.try_push(number)?;
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
    ) -> Result<Arc<FillIndex>> {
        budget.charge(
            LimitCode::MemorySize,
            (size_of::<FillIndex>() + 2 * size_of::<usize>()) as u64,
        )?;
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
    pub fn carry_from(
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

/// Returns the value that the index records for field `id` of `form`.
pub(super) fn indexed_value<'i>(
    index: &'i FillIndex,
    form: &Form,
    id: Id,
) -> Result<Option<&'i str>> {
    let Ok(at) = form.objects.binary_search_by_key(&id, |&(id, _)| id) else {
        return Err(Error::Pdf("appearance index names a missing field".into()));
    };
    let key = u32::try_from(form.objects[at].1).map_err(|_| edges_error())?;
    let Some(value) = index.values.leaves.get(key) else {
        return Err(Error::Pdf("value index names a missing field".into()));
    };
    Ok(value.as_deref())
}

#[cfg(test)]
mod appearance_tests;
