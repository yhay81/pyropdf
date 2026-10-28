//! Merging: the identical objects that a full save writes once, on request
//! (DESIGN.md section 4.4).

use std::collections::hash_map::DefaultHasher;
use std::hash::Hasher;
use std::sync::Arc;

use super::{Result, Row};
use crate::ErrorKind;
use crate::budget::{Budget, BudgetVec};
use crate::value::{Dict, Reference, Value};
use crate::version::Version;

/// The keys whose objects have an identity that a reader relies on.
///
/// A page, an annotation, a form field, a structure element, an outline
/// item, an article bead, or an optional-content group is the same object
/// wherever it is named: merging two such objects that happen to be equal
/// would merge two pages, two layers, or two fields into one. The arrays
/// named under these keys are lists that belong to their owner (`/Annots`,
/// `/K`, `/Kids`), so they keep their identity as well.
const IDENTITY_KEYS: &[&[u8]] = &[
    b"AcroForm",
    b"Annots",
    b"B",
    b"Dests",
    b"Encrypt",
    b"Fields",
    b"First",
    b"IDTree",
    b"IRT",
    b"Info",
    b"K",
    b"Kids",
    b"Last",
    b"Locked",
    b"Names",
    b"Next",
    b"OC",
    b"OCGs",
    b"OCProperties",
    b"OFF",
    b"ON",
    b"Obj",
    b"Order",
    b"Outlines",
    b"P",
    b"Pages",
    b"Parent",
    b"ParentTree",
    b"Pg",
    b"Popup",
    b"Prev",
    b"Root",
    b"StructTreeRoot",
    b"Threads",
];

/// The `/Type` values of the dictionaries and streams that keep their
/// identity.
const IDENTITY_TYPES: &[&[u8]] = &[
    b"Annot",
    b"Bead",
    b"Catalog",
    b"MCR",
    b"OBJR",
    b"OCG",
    b"OCMD",
    b"ObjStm",
    b"Outlines",
    b"Page",
    b"Pages",
    b"Sig",
    b"StructElem",
    b"StructTreeRoot",
    b"Thread",
    b"XRef",
];

/// No node or class: an entry not yet given one.
const NONE: usize = usize::MAX;

/// The objects that a save merges.
pub(super) struct Plan<'b> {
    /// Each object that merges into an identical one, with the number of the
    /// object that it merges into, in ascending order.
    pub(super) merged: BudgetVec<'b, (u32, u32)>,
    /// The objects that the save keeps and that reference a merged one, in
    /// ascending order.
    pub(super) referrers: BudgetVec<'b, u32>,
}

/// Returns the plan that merges the identical objects of `rows`.
///
/// Two objects are identical when they have the same values, the same
/// dictionary keys in the same order, and references to objects that are
/// themselves identical; streams also need the same data. Each set of
/// identical objects merges into the one with the smallest number. An object
/// in a cycle of references, an object that the trailer names, an object
/// named under an identity key, and a dictionary or stream that looks like a
/// page, an annotation, a field, a structure element, or a layer never
/// merges, though references to it still count when its referrers are
/// compared. An object that does not parse never merges either.
pub(super) fn plan<'b>(version: &Version, rows: &[Row], budget: &'b Budget) -> Result<Plan<'b>> {
    let mut numbers = BudgetVec::new(budget);
    let mut values = BudgetVec::new(budget);
    for row in rows {
        budget.poll()?;
        if row.number == 0 || row.offset.is_none() {
            continue;
        }
        let value = match version.get(row.number, budget) {
            Ok(object) => object.map(|object| Arc::clone(object.value())),
            Err(error) if matches!(error.kind(), ErrorKind::Malformed(_)) => None,
            Err(error) => return Err(error.into()),
        };
        numbers.try_push(row.number)?;
        values.try_push(value)?;
    }
    let node_of = |reference: &Reference| numbers.binary_search(&reference.number()).ok();
    let graph = Graph::new(&values, &node_of, budget)?;
    let pinned = pinned(version, &values, &node_of, budget)?;
    let components = graph.components(budget)?;
    let count = values.len();
    // The class of each node, the first node of each class, and the hash of
    // each class that may take more nodes.
    let mut class_of = filled(count, NONE, budget)?;
    let mut first = BudgetVec::new(budget);
    let mut hashes = BudgetVec::new(budget);
    // An open-addressed table of the classes that may take more nodes: as it
    // has twice as many slots as nodes, it always has an empty one.
    let size = count.saturating_mul(2).next_power_of_two();
    let mut table = filled(size, NONE, budget)?;
    let mut walk = BudgetVec::new(budget);
    let mut pairs = BudgetVec::new(budget);
    // Children come before the components that reference them, so the
    // classes of a node's references are known when it is compared.
    for &(start, len) in components.list.iter() {
        budget.poll()?;
        let group = &components.members[start..start + len];
        let value = match group {
            [node] if !graph.loops(*node) && !pinned[*node] => {
                values[*node].as_deref().filter(|value| mergeable(value))
            }
            _ => None,
        };
        let Some(value) = value else {
            for &member in group {
                class_of[member] = first.len();
                first.try_push(member)?;
                hashes.try_push(0)?;
            }
            continue;
        };
        let node = group[0];
        let hash = hash(value, &node_of, &class_of, &mut walk)?;
        // The slot comes from the low bits of the hash.
        #[expect(clippy::cast_possible_truncation, reason = "the table masks the hash")]
        let mut slot = hash as usize & (size - 1);
        class_of[node] = loop {
            let class = table[slot];
            if class == NONE {
                let class = first.len();
                first.try_push(node)?;
                hashes.try_push(hash)?;
                table[slot] = class;
                break class;
            }
            if hashes[class] == hash {
                let other = values[first[class]]
                    .as_deref()
                    .expect("a merging class has a value");
                if equal(value, other, &node_of, &class_of, &mut pairs)? {
                    break class;
                }
            }
            slot = (slot + 1) & (size - 1);
        };
    }
    // Each class keeps its smallest number: the nodes ascend, so the first
    // node of a class in this scan is its smallest.
    let mut kept = filled(first.len(), NONE, budget)?;
    let mut merged = BudgetVec::new(budget);
    for (node, &class) in class_of.iter().enumerate() {
        if kept[class] == NONE {
            kept[class] = node;
        } else {
            merged.try_push((numbers[node], numbers[kept[class]]))?;
        }
    }
    let mut referrers = BudgetVec::new(budget);
    for node in 0..count {
        budget.poll()?;
        let stays = |node: usize| kept[class_of[node]] == node;
        if stays(node) && graph.edges(node).iter().any(|&target| !stays(target)) {
            referrers.try_push(numbers[node])?;
        }
    }
    Ok(Plan { merged, referrers })
}

/// Returns `count` copies of `value`.
fn filled<T: Clone>(count: usize, value: T, budget: &Budget) -> Result<BudgetVec<'_, T>> {
    let mut filled = BudgetVec::new(budget);
    filled.try_extend(std::iter::repeat_n(value, count))?;
    Ok(filled)
}

/// Returns whether a value may merge with an identical one by its own shape.
fn mergeable(value: &Value) -> bool {
    match value {
        Value::Stream(stream) => !has_identity(stream.dict()),
        Value::Dict(dict) => !has_identity(dict),
        _ => true,
    }
}

/// Returns whether a dictionary describes an object with an identity.
fn has_identity(dict: &Dict) -> bool {
    if let Some(Value::Name(kind)) = dict.get("Type")
        && IDENTITY_TYPES.contains(&kind.as_bytes())
    {
        return true;
    }
    // Pages, page-tree nodes, fields, outline items, annotations, and
    // structure elements name their parent or their kids; annotations
    // without /Type still carry /Subtype and /Rect, and fields carry /FT or
    // /T.
    ["Parent", "Kids", "P", "FT", "T"]
        .iter()
        .any(|key| dict.get(key).is_some())
        || (dict.get("Subtype").is_some() && dict.get("Rect").is_some())
}

/// Returns, for each node, whether the trailer names it or an object names
/// it under an identity key.
fn pinned<'b>(
    version: &Version,
    values: &[Option<Arc<Value>>],
    node: &dyn Fn(&Reference) -> Option<usize>,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, bool>> {
    let mut pinned = filled(values.len(), false, budget)?;
    let mut stack = BudgetVec::new(budget);
    stack.try_extend(version.trailer().iter().map(|(_, value)| (value, true)))?;
    stack.try_extend(values.iter().flatten().map(|value| (&**value, false)))?;
    while let Some((value, pin)) = stack.pop() {
        let dict = match value {
            Value::Reference(reference) => {
                if pin && let Some(node) = node(reference) {
                    pinned[node] = true;
                }
                continue;
            }
            Value::Array(array) => {
                stack.try_extend(array.iter().map(|item| (item, pin)))?;
                continue;
            }
            Value::Dict(dict) => dict,
            Value::Stream(stream) => stream.dict(),
            _ => continue,
        };
        stack.try_extend(
            dict.iter()
                .map(|(name, value)| (value, pin || IDENTITY_KEYS.contains(&name.as_bytes()))),
        )?;
    }
    Ok(pinned)
}

/// The strongly connected components of a [`Graph`], children before the
/// components that reference them.
struct Components<'b> {
    /// The members of all of them.
    members: BudgetVec<'b, usize>,
    /// The start and the length of each one's members.
    list: BudgetVec<'b, (usize, usize)>,
}

/// The references between the nodes, in compressed sparse row form.
struct Graph<'b> {
    offsets: BudgetVec<'b, usize>,
    targets: BudgetVec<'b, usize>,
}

impl<'b> Graph<'b> {
    fn new(
        values: &[Option<Arc<Value>>],
        node: &dyn Fn(&Reference) -> Option<usize>,
        budget: &'b Budget,
    ) -> Result<Self> {
        let mut offsets = BudgetVec::new(budget);
        let mut targets = BudgetVec::new(budget);
        let mut stack = BudgetVec::new(budget);
        offsets.try_push(0)?;
        for value in values {
            budget.poll()?;
            if let Some(value) = value {
                stack.try_push(&**value)?;
            }
            while let Some(value) = stack.pop() {
                match value {
                    Value::Reference(reference) => {
                        if let Some(node) = node(reference) {
                            targets.try_push(node)?;
                        }
                    }
                    Value::Array(array) => stack.try_extend(array.iter())?,
                    Value::Dict(dict) => stack.try_extend(dict.iter().map(|(_, value)| value))?,
                    Value::Stream(stream) => {
                        stack.try_extend(stream.dict().iter().map(|(_, value)| value))?;
                    }
                    _ => {}
                }
            }
            offsets.try_push(targets.len())?;
        }
        Ok(Self { offsets, targets })
    }

    fn edges(&self, node: usize) -> &[usize] {
        &self.targets[self.offsets[node]..self.offsets[node + 1]]
    }

    /// Returns whether `node` references itself.
    fn loops(&self, node: usize) -> bool {
        self.edges(node).contains(&node)
    }

    /// Returns the strongly connected components. Tarjan's algorithm runs
    /// from each node in ascending order, with a stack of frames for its
    /// depth.
    fn components(&self, budget: &'b Budget) -> Result<Components<'b>> {
        let count = self.offsets.len() - 1;
        let mut order = filled(count, NONE, budget)?;
        let mut low = filled(count, 0, budget)?;
        let mut on_stack = filled(count, false, budget)?;
        let mut stack = BudgetVec::new(budget);
        let mut frames: BudgetVec<'_, (usize, usize)> = BudgetVec::new(budget);
        let mut members = BudgetVec::new(budget);
        let mut list = BudgetVec::new(budget);
        let mut next = 0;
        for root in 0..count {
            if order[root] != NONE {
                continue;
            }
            budget.poll()?;
            frames.try_push((root, 0))?;
            while let Some(&mut (node, ref mut edge)) = frames.last_mut() {
                if *edge == 0 && order[node] == NONE {
                    order[node] = next;
                    low[node] = next;
                    next += 1;
                    stack.try_push(node)?;
                    on_stack[node] = true;
                }
                if let Some(&target) = self.edges(node).get(*edge) {
                    *edge += 1;
                    if order[target] == NONE {
                        frames.try_push((target, 0))?;
                    } else if on_stack[target] {
                        low[node] = low[node].min(order[target]);
                    }
                    continue;
                }
                frames.pop();
                if let Some(&(parent, _)) = frames.last() {
                    low[parent] = low[parent].min(low[node]);
                }
                if low[node] == order[node] {
                    let start = members.len();
                    while let Some(member) = stack.pop() {
                        on_stack[member] = false;
                        members.try_push(member)?;
                        if member == node {
                            break;
                        }
                    }
                    list.try_push((start, members.len() - start))?;
                }
            }
        }
        Ok(Components { members, list })
    }
}

/// Hashes `value` with each reference replaced by the class of its node, or
/// by its number and generation when it names no node.
fn hash<'v>(
    value: &'v Value,
    node: &dyn Fn(&Reference) -> Option<usize>,
    class_of: &[usize],
    walk: &mut BudgetVec<'_, &'v Value>,
) -> Result<u64> {
    let mut hasher = DefaultHasher::new();
    walk.clear();
    walk.try_push(value)?;
    while let Some(value) = walk.pop() {
        match value {
            Value::Null => hasher.write_u8(0),
            Value::Boolean(value) => {
                hasher.write_u8(1);
                hasher.write_u8(u8::from(*value));
            }
            Value::Integer(value) => {
                hasher.write_u8(2);
                hasher.write_i64(*value);
            }
            Value::Real(value) => {
                hasher.write_u8(3);
                hasher.write_u64(value.value().to_bits());
            }
            Value::Name(name) => {
                hasher.write_u8(4);
                bytes(&mut hasher, name.as_bytes());
            }
            Value::String(string) => {
                hasher.write_u8(5);
                bytes(&mut hasher, string.as_bytes());
            }
            Value::Array(array) => {
                hasher.write_u8(6);
                hasher.write_usize(array.len());
                walk.try_extend(array.iter())?;
            }
            Value::Dict(dict) => {
                hasher.write_u8(7);
                entries(&mut hasher, dict, walk)?;
            }
            Value::Stream(stream) => {
                hasher.write_u8(8);
                bytes(&mut hasher, stream.data());
                entries(&mut hasher, stream.dict(), walk)?;
            }
            Value::Reference(reference) => match node(reference) {
                Some(node) => {
                    hasher.write_u8(9);
                    hasher.write_usize(class_of[node]);
                }
                None => {
                    hasher.write_u8(10);
                    hasher.write_u32(reference.number());
                    hasher.write_u16(reference.generation());
                }
            },
        }
    }
    Ok(hasher.finish())
}

/// Hashes the keys of `dict`, and queues its values.
fn entries<'v>(
    hasher: &mut DefaultHasher,
    dict: &'v Dict,
    walk: &mut BudgetVec<'_, &'v Value>,
) -> Result<()> {
    hasher.write_usize(dict.len());
    for (name, _) in dict.iter() {
        bytes(hasher, name.as_bytes());
    }
    walk.try_extend(dict.iter().map(|(_, value)| value))?;
    Ok(())
}

fn bytes(hasher: &mut DefaultHasher, bytes: &[u8]) {
    hasher.write_usize(bytes.len());
    hasher.write(bytes);
}

/// Returns whether `left` and `right` are identical as [`hash`] hashes them.
fn equal<'v>(
    left: &'v Value,
    right: &'v Value,
    node: &dyn Fn(&Reference) -> Option<usize>,
    class_of: &[usize],
    pairs: &mut BudgetVec<'_, (&'v Value, &'v Value)>,
) -> Result<bool> {
    pairs.clear();
    pairs.try_push((left, right))?;
    while let Some(pair) = pairs.pop() {
        let equal = match pair {
            (Value::Array(left), Value::Array(right)) => {
                left.len() == right.len() && {
                    pairs.try_extend(left.iter().zip(right.iter()))?;
                    true
                }
            }
            (Value::Dict(left), Value::Dict(right)) => same_entries(left, right, pairs)?,
            (Value::Stream(left), Value::Stream(right)) => {
                left.data() == right.data() && same_entries(left.dict(), right.dict(), pairs)?
            }
            (Value::Reference(left), Value::Reference(right)) => match (node(left), node(right)) {
                (Some(left), Some(right)) => class_of[left] == class_of[right],
                (None, None) => left == right,
                _ => false,
            },
            (left, right) => left == right,
        };
        if !equal {
            return Ok(false);
        }
    }
    Ok(true)
}

/// Returns whether `left` and `right` have the same keys in the same order,
/// and queues their values in pairs.
fn same_entries<'v>(
    left: &'v Dict,
    right: &'v Dict,
    pairs: &mut BudgetVec<'_, (&'v Value, &'v Value)>,
) -> Result<bool> {
    if left.len() != right.len()
        || left
            .iter()
            .zip(right.iter())
            .any(|((left, _), (right, _))| left != right)
    {
        return Ok(false);
    }
    pairs.try_extend(
        left.iter()
            .zip(right.iter())
            .map(|((_, left), (_, right))| (left, right)),
    )?;
    Ok(true)
}
