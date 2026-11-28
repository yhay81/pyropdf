//! Referential rules (DESIGN.md section 8): what a structural edit does
//! with a reference to an object that it removes or moves.
//!
//! A structural edit plans its rules in [`Edits`], which reads nothing
//! that it changes, and writes them together, so a refusal while planning
//! leaves the version as it was. [`unreferenced`] then lists the objects
//! that the edit leaves unreferenced, for it to free.

use crate::budget::{Budget, BudgetVec};
use crate::memo::Reads;
use crate::value::{Dict, Name, Reference, Value};
use crate::version::table::Table;
use crate::version::{Transaction, Version};
use crate::{Error, Result};

use super::{Cache, Found, References, Referrer, Role, Step, copy_for_edit, find_where};

/// What a rule does with a reference to an object that leaves.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Rule {
    /// Removes the entry that holds the reference: the key of a
    /// dictionary, or the item of an array.
    RemoveEntry,
    /// Leaves the navigation that holds the reference without a
    /// destination: removes the referrer's entry that the path starts
    /// with, such as `/Dest` or `/A`.
    Unresolve,
    /// Removes the referrer: every entry of a page's annotations that
    /// refers to it goes, and the referrer is freed once nothing refers to
    /// it.
    RemoveReferrer,
    /// Points the reference at another object.
    Retarget(Reference),
}

/// One planned change of an object: the entry at a path goes, or the
/// reference at it points elsewhere.
struct Edit {
    number: u32,
    path: Box<[Step]>,
    to: Option<Reference>,
}

/// The rules that a structural edit plans, written together.
pub struct Edits<'b> {
    budget: &'b Budget,
    edits: BudgetVec<'b, Edit>,
    /// The referrers that [`Rule::RemoveReferrer`] removed.
    orphans: BudgetVec<'b, u32>,
    /// The references of each object that the rules searched, by the
    /// number that they name, so that each is read once: the position in
    /// `found` of each object's.
    held: Table<usize>,
    found: BudgetVec<'b, BudgetVec<'b, (usize, Found)>>,
    /// The edges, child then parent, from each name or number tree node
    /// whose pairs the edits remove up to its tree's root.
    parents: BudgetVec<'b, (u32, u32)>,
    climbed: Table<()>,
}

impl<'b> Edits<'b> {
    /// Returns no edits, which grow through `budget`.
    #[must_use]
    pub const fn new(budget: &'b Budget) -> Self {
        Self {
            budget,
            edits: BudgetVec::new(budget),
            orphans: BudgetVec::new(budget),
            held: Table::new(),
            found: BudgetVec::new(budget),
            parents: BudgetVec::new(budget),
            climbed: Table::new(),
        }
    }

    /// Returns every reference to object `number` that object `holder`
    /// holds in the version that `reads` reads, as [`super::find`] does,
    /// reading `holder` once however many of its references the rules ask
    /// for.
    pub(super) fn find(
        &mut self,
        reads: &mut Reads<'_>,
        holder: u32,
        number: u32,
    ) -> Result<BudgetVec<'b, Found>> {
        self.budget.poll()?;
        let at = if let Some(&at) = self.held.get(holder) {
            at
        } else {
            let mut found = BudgetVec::new(self.budget);
            for each in find_where(reads, Referrer::Object(holder), |_| true)?.into_vec() {
                found.try_push((found.len(), each))?;
            }
            found.sort_unstable_by_key(|(ordinal, found)| (found.reference.number(), *ordinal));
            self.budget.poll()?;
            self.found.try_push(found)?;
            self.held
                .insert(holder, self.found.len() - 1, self.budget)?;
            self.found.len() - 1
        };
        let mut out = BudgetVec::new(self.budget);
        if let Some(found) = self.found.get(at) {
            let start = found.partition_point(|(_, found)| found.reference.number() < number);
            for (_, found) in found[start..]
                .iter()
                .take_while(|(_, found)| found.reference.number() == number)
            {
                self.budget.poll()?;
                out.try_push(Found {
                    path: copy_path(&found.path, self.budget)?
                        .into_vec()
                        .into_boxed_slice(),
                    reference: found.reference,
                    role: found.role,
                })?;
            }
        }
        Ok(out)
    }

    /// Plans `rule` for the reference at `path` in object `referrer` of the
    /// version that `reads` reads, whose index is `index`.
    pub fn apply(
        &mut self,
        reads: &mut Reads<'_>,
        index: &References,
        referrer: u32,
        path: &[Step],
        rule: Rule,
    ) -> Result<()> {
        match rule {
            Rule::RemoveEntry => {
                if is_pair(path) {
                    self.climb(reads, index, referrer)?;
                }
                self.push(referrer, path, None)
            }
            Rule::Unresolve => self.push(referrer, &path[..path.len().min(1)], None),
            Rule::Retarget(to) => self.push(referrer, path, Some(to)),
            Rule::RemoveReferrer => {
                let generation = reads.generation(referrer)?;
                for &holder in index.referrers(referrer) {
                    for found in self.find(reads, holder, referrer)?.iter() {
                        if generation == Some(found.reference.generation())
                            && is_annotation_entry(reads, index, holder, found)?
                        {
                            self.push(holder, &found.path, None)?;
                        }
                    }
                }
                self.orphans.try_push(referrer)?;
                Ok(())
            }
        }
    }

    /// Records the edges from tree node `number` up to its tree's root: each
    /// node whose `/Kids` holds it, while the node has `/Limits`. Refuses a
    /// tree deeper than [`MAX_TREE_DEPTH`] levels.
    fn climb(&mut self, reads: &mut Reads<'_>, index: &References, number: u32) -> Result<()> {
        let mut pending = BudgetVec::new(self.budget);
        pending.try_push((number, 0))?;
        while let Some((child, depth)) = pending.pop() {
            self.budget.poll()?;
            if self.climbed.get(child).is_some() {
                continue;
            }
            self.climbed.insert(child, (), self.budget)?;
            if depth >= MAX_TREE_DEPTH {
                return Err(crate::LimitExceeded::over(
                    crate::budget::LimitCode::ObjectDepth,
                    MAX_TREE_DEPTH,
                    depth + 1,
                )
                .into());
            }
            let generation = reads.generation(child)?;
            for &parent in index.referrers(child) {
                self.budget.poll()?;
                let Some(object) = reads.get(parent)? else {
                    continue;
                };
                let Value::Dict(dict) = &**object.value() else {
                    continue;
                };
                let Some(Value::Array(kids)) = dict.get("Kids") else {
                    continue;
                };
                if !kids.iter().any(|kid| {
                    matches!(kid, Value::Reference(kid)
                        if kid.number() == child && Some(kid.generation()) == generation)
                }) {
                    continue;
                }
                self.parents.try_push((child, parent))?;
                if dict.get("Limits").is_some() {
                    pending.try_push((parent, depth + 1))?;
                }
            }
        }
        Ok(())
    }

    /// Records that the edits remove `number`, which is freed once nothing
    /// but other removed objects refers to it.
    pub(super) fn orphan(&mut self, number: u32) -> Result<()> {
        self.orphans.try_push(number)?;
        Ok(())
    }

    /// Adds the edits that `other` planned.
    pub(super) fn append(&mut self, other: Edits<'_>) -> Result<()> {
        for edit in other.edits.into_vec() {
            self.edits.try_push(edit)?;
        }
        self.orphans.try_extend_from_slice(&other.orphans)?;
        self.parents.try_extend_from_slice(&other.parents)?;
        Ok(())
    }

    /// Returns the objects that the edits remove, ascending: the referrers
    /// of [`Rule::RemoveReferrer`] and the structure elements that removed
    /// pages leave empty.
    pub fn orphans(&mut self) -> &[u32] {
        self.orphans.sort_unstable();
        &self.orphans
    }

    /// Returns whether no edit is planned.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.edits.is_empty() && self.orphans.is_empty()
    }

    fn push(&mut self, number: u32, path: &[Step], to: Option<Reference>) -> Result<()> {
        if path.is_empty() {
            return Ok(());
        }
        let owned = copy_path(path, self.budget)?;
        self.edits.try_push(Edit {
            number,
            path: owned.into_vec().into_boxed_slice(),
            to,
        })?;
        Ok(())
    }

    /// Writes the edits in `transaction`: each object once, with the
    /// deepest and last entries of a container removed first, so that the
    /// positions of the others hold.
    pub fn write(&mut self, transaction: &mut Transaction, budget: &Budget) -> Result<()> {
        let version = transaction.snapshot();
        self.process(&version, budget, |number, value| {
            transaction.write(number, &value, budget).map(|_| ())
        })
    }

    /// Computes edits without writing to the source version.
    pub fn values<'a>(
        &mut self,
        version: &Version,
        budget: &'a Budget,
    ) -> Result<BudgetVec<'a, (u32, Value)>> {
        let mut out = BudgetVec::new(budget);
        self.process(version, budget, |number, value| {
            out.try_push((number, value))?;
            Ok(())
        })?;
        Ok(out)
    }

    fn process(
        &mut self,
        version: &Version,
        budget: &Budget,
        mut emit: impl FnMut(u32, Value) -> Result<()>,
    ) -> Result<()> {
        budget.poll()?;
        self.edits.sort_unstable_by(|a, b| {
            a.number
                .cmp(&b.number)
                .then_with(|| b.path.cmp(&a.path))
                .then_with(|| b.to.is_some().cmp(&a.to.is_some()))
        });
        let mut out: BudgetVec<'_, (u32, Value)> = BudgetVec::new(budget);
        let mut positions: Table<usize> = Table::new();
        let mut settle = BudgetVec::new(budget);
        let mut at = 0;
        while at < self.edits.len() {
            budget.poll()?;
            let number = self.edits[at].number;
            let Some(object) = version.get(number, budget)? else {
                while at < self.edits.len() && self.edits[at].number == number {
                    at += 1;
                }
                continue;
            };
            let mut value = copy_for_edit(object.value(), budget)?;
            let mut last: Option<&[Step]> = None;
            let first = at;
            while at < self.edits.len() && self.edits[at].number == number {
                budget.poll()?;
                let edit = &self.edits[at];
                if last == Some(&*edit.path) {
                    at += 1;
                    continue;
                }
                let removals = removals(&self.edits[at..]);
                if removals > 1 {
                    remove_items(&mut value, &self.edits[at..at + removals]);
                    last = Some(&self.edits[at + removals - 1].path);
                    at += removals;
                    continue;
                }
                change(&mut value, &edit.path, edit.to);
                last = Some(&edit.path);
                at += 1;
            }
            let mut nodes = BudgetVec::new(budget);
            for edit in &self.edits[first..at] {
                if edit.to.is_none() && is_pair(&edit.path) {
                    let node = &edit.path[..edit.path.len() - 2];
                    if !nodes.contains(&node) {
                        nodes.try_push(node)?;
                    }
                }
            }
            for node in nodes.iter() {
                budget.poll()?;
                if let Some(Value::Dict(dict)) = at_mut(&mut value, node)
                    && let Some(state) = narrow(dict)
                    && node.is_empty()
                {
                    settle.try_push((number, state == Settled::Empty))?;
                }
            }
            positions.insert(number, out.len(), budget)?;
            out.try_push((number, value))?;
        }
        self.settle(version, budget, &mut out, &mut positions, settle)?;
        for (number, value) in out.into_vec() {
            emit(number, value)?;
        }
        self.edits.clear();
        self.parents = BudgetVec::new(self.budget);
        self.climbed = Table::new();
        self.held = Table::new();
        self.found.clear();
        budget.poll()?;
        Ok(())
    }
}

/// Carries the narrowed and emptied tree nodes of `settle`, with whether
/// each was emptied, up their trees: an emptied node leaves its parent's
/// `/Kids` and is removed, and a parent's `/Limits` follow its kids'.
impl Edits<'_> {
    fn settle(
        &mut self,
        version: &Version,
        budget: &Budget,
        out: &mut BudgetVec<'_, (u32, Value)>,
        positions: &mut Table<usize>,
        mut settle: BudgetVec<'_, (u32, bool)>,
    ) -> Result<()> {
        // Each step either empties a node, once, or changes a parent's
        // limits; a cycle of trees could change them forever.
        let mut steps = self.parents.len().saturating_mul(2) + settle.len();
        while let Some((child, emptied)) = settle.pop() {
            budget.poll()?;
            if emptied {
                self.orphans.try_push(child)?;
            }
            for at in 0..self.parents.len() {
                let (kid, parent) = self.parents[at];
                if kid != child || steps == 0 {
                    continue;
                }
                steps -= 1;
                let position = match positions.get(parent) {
                    Some(&position) => position,
                    None => {
                        let Some(object) = version.get(parent, budget)? else {
                            continue;
                        };
                        positions.insert(parent, out.len(), budget)?;
                        out.try_push((parent, copy_for_edit(object.value(), budget)?))?;
                        out.len() - 1
                    }
                };
                let mut value = std::mem::replace(&mut out[position].1, Value::Null);
                let state = if let Value::Dict(dict) = &mut value {
                    if emptied && let Some(Value::Array(kids)) = dict.get_mut("Kids") {
                        kids.retain(
                            |kid| !matches!(kid, Value::Reference(kid) if kid.number() == child),
                        );
                    }
                    self.gather(version, budget, out, positions, dict)?
                } else {
                    None
                };
                out[position].1 = value;
                if let Some(state) = state {
                    settle.try_push((parent, state == Settled::Empty))?;
                }
            }
        }
        Ok(())
    }

    /// Sets the `/Limits` of intermediate node `dict` from its kids':
    /// returns how they changed, or `None` when they hold or a kid has
    /// none.
    fn gather(
        &self,
        version: &Version,
        budget: &Budget,
        out: &BudgetVec<'_, (u32, Value)>,
        positions: &Table<usize>,
        dict: &mut Dict,
    ) -> Result<Option<Settled>> {
        if dict.get("Limits").is_none() {
            return Ok(None);
        }
        let Some(Value::Array(kids)) = dict.get("Kids") else {
            return Ok(None);
        };
        let mut ends = [None, None];
        for (end, kid) in [kids.as_slice().first(), kids.as_slice().last()]
            .into_iter()
            .enumerate()
        {
            let Some(Value::Reference(kid)) = kid else {
                break;
            };
            let held;
            let value = if let Some(&position) = positions.get(kid.number()) {
                &out[position].1
            } else if let Some(object) = version.get(kid.number(), budget)? {
                held = object;
                &**held.value()
            } else {
                return Ok(None);
            };
            let Value::Dict(kid) = value else {
                return Ok(None);
            };
            let Some(Value::Array(limits)) = kid.get("Limits") else {
                return Ok(None);
            };
            let Some(limit) = limits.get(end) else {
                return Ok(None);
            };
            ends[end] = Some(copy_for_edit(limit, budget)?);
        }
        Ok(match ends {
            [Some(least), Some(greatest)] => set_limits(dict, least, greatest),
            [None, None] if kids.is_empty() => Some(Settled::Empty),
            _ => None,
        })
    }
}

/// How a tree node's `/Limits` changed.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Settled {
    Narrowed,
    Empty,
}

/// The deepest name or number tree whose pruning reaches up to its root.
const MAX_TREE_DEPTH: u64 = 32;

/// Returns whether `path` leads to an item of a tree node's `/Names` or
/// `/Nums`.
fn is_pair(path: &[Step]) -> bool {
    matches!(path, [.., Step::Key(key), Step::Index(_)] if matches!(key.as_bytes(), b"Names" | b"Nums"))
}

/// Sets the `/Limits` of leaf `dict` from the keys that its pairs still
/// hold: returns how they changed, or `None` when it has no limits or
/// they hold.
fn narrow(dict: &mut Dict) -> Option<Settled> {
    dict.get("Limits")?;
    let pairs = ["Names", "Nums"]
        .into_iter()
        .find_map(|key| match dict.get(key) {
            Some(Value::Array(pairs)) => Some(pairs),
            _ => None,
        })?;
    if pairs.len() < 2 {
        return pairs.is_empty().then_some(Settled::Empty);
    }
    let least = pairs.as_slice()[0].clone();
    let greatest = pairs.as_slice()[(pairs.len() - 2) & !1].clone();
    set_limits(dict, least, greatest)
}

fn set_limits(dict: &mut Dict, least: Value, greatest: Value) -> Option<Settled> {
    let limits = Value::Array(vec![least, greatest].into());
    if dict.get("Limits") == Some(&limits) {
        return None;
    }
    #[expect(
        clippy::disallowed_methods,
        reason = "replaces one entry with two values that the node already held"
    )]
    dict.insert(Name::from("Limits"), limits);
    Some(Settled::Narrowed)
}

/// Copies the steps and every owned key through fallible budgeted buffers.
pub(super) fn copy_path<'b>(path: &[Step], budget: &'b Budget) -> Result<BudgetVec<'b, Step>> {
    let mut owned = BudgetVec::new(budget);
    for step in path {
        let step = match step {
            Step::Index(index) => Step::Index(*index),
            Step::Key(key) => {
                let mut bytes = BudgetVec::new(budget);
                bytes.try_extend_from_slice(key.as_bytes())?;
                Step::Key(Name::new(bytes.into_vec().into_boxed_slice()))
            }
        };
        owned.try_push(step)?;
    }
    Ok(owned)
}

/// Returns whether `found`, a reference that `holder` holds, is an entry of
/// a page's annotations: of its `/Annots`, or of an array object that a
/// page names as them.
fn is_annotation_entry(
    reads: &mut Reads<'_>,
    index: &References,
    holder: u32,
    found: &Found,
) -> Result<bool> {
    if found.role == Some(Role::PageAnnotation) {
        return Ok(true);
    }
    if !matches!(*found.path, [Step::Index(_)]) {
        return Ok(false);
    }
    annotation_array(reads, index, holder)
}

/// An array is annotations only when a page actually names it as /Annots.
pub(super) fn annotation_array(
    reads: &mut Reads<'_>,
    index: &References,
    number: u32,
) -> Result<bool> {
    for &owner in index.referrers(number) {
        reads.budget().poll()?;
        let Some(object) = reads.get(owner)? else {
            continue;
        };
        let Value::Dict(dict) = &**object.value() else {
            continue;
        };
        if matches!(dict.get("Type"), Some(Value::Name(name)) if name.as_bytes() == b"Page")
            && matches!(dict.get("Annots"), Some(Value::Reference(reference))
                if reference.number() == number && reads.generation(number)? == Some(reference.generation()))
        {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Returns how many of the first `edits`, sorted as [`Edits::write`] sorts
/// them, remove items of one array of one object: none when the first edit
/// does not remove an item.
fn removals(edits: &[Edit]) -> usize {
    fn removes(edit: &Edit) -> Option<(u32, &[Step])> {
        match edit.path.split_last() {
            Some((Step::Index(_), parent)) if edit.to.is_none() => Some((edit.number, parent)),
            _ => None,
        }
    }
    let Some(first) = edits.first().and_then(removes) else {
        return 0;
    };
    edits
        .iter()
        .take_while(|edit| removes(edit) == Some(first))
        .count()
}

/// Removes, in one pass over their array, the items that `edits` remove:
/// items of one array, in descending order of position, as [`removals`]
/// counts them.
fn remove_items(value: &mut Value, edits: &[Edit]) {
    let Some((_, parent)) = edits.first().and_then(|edit| edit.path.split_last()) else {
        return;
    };
    let Some(Value::Array(items)) = at_mut(value, parent) else {
        return;
    };
    let mut gone = edits
        .iter()
        .rev()
        .filter_map(|edit| match edit.path.last() {
            Some(Step::Index(index)) => Some(*index),
            _ => None,
        })
        .peekable();
    let mut position = 0;
    items.retain(|_| {
        let here = position;
        position += 1;
        let mut keep = true;
        while let Some(index) = gone.next_if(|&index| index <= here) {
            keep &= index != here;
        }
        keep
    });
}

/// Removes the entry at `path` in `value`, or points the reference there
/// at `to`; does nothing where the path leads nowhere.
fn change(value: &mut Value, path: &[Step], to: Option<Reference>) {
    let Some((last, parent)) = path.split_last() else {
        return;
    };
    let Some(container) = at_mut(value, parent) else {
        return;
    };
    let dict = match container {
        Value::Array(items) => {
            if let Step::Index(index) = last {
                match to {
                    Some(to) => {
                        if let Some(item) = items.get_mut(*index) {
                            *item = Value::Reference(to);
                        }
                    }
                    None => {
                        items.remove(*index);
                    }
                }
            }
            return;
        }
        Value::Dict(dict) => dict,
        Value::Stream(stream) => stream.dict_mut(),
        _ => return,
    };
    let Step::Key(key) = last else {
        return;
    };
    match to {
        Some(to) => {
            if let Some(item) = dict.get_mut(key.as_bytes()) {
                *item = Value::Reference(to);
            }
        }
        None => {
            dict.remove(key.as_bytes());
        }
    }
}

fn at_mut<'v>(value: &'v mut Value, path: &[Step]) -> Option<&'v mut Value> {
    path.iter()
        .try_fold(value, |value, step| match (value, step) {
            (Value::Array(items), Step::Index(index)) => items.get_mut(*index),
            (Value::Dict(dict), Step::Key(key)) => dict.get_mut(key.as_bytes()),
            (Value::Stream(stream), Step::Key(key)) => stream.dict_mut().get_mut(key.as_bytes()),
            _ => None,
        })
}

/// Execution context for [`nominated_unreferenced`] and [`unreferenced`]:
/// [`Budget`] with [`Error`] in production, and a zero-overhead infallible
/// context inside bounded Kani graph proofs.
trait GraphContext {
    type Error;
    fn poll(&self) -> std::result::Result<(), Self::Error>;
}

impl GraphContext for Budget {
    type Error = Error;

    fn poll(&self) -> Result<()> {
        Ok(Self::poll(self)?)
    }
}

/// Graph view consumed by [`nominated_unreferenced`] and [`unreferenced`].
trait RefGraph {
    fn trailer_contains(&self, number: u32) -> bool;
    fn for_each_target<E>(
        &self,
        number: u32,
        f: impl FnMut(u32) -> std::result::Result<(), E>,
    ) -> std::result::Result<(), E>;
    fn for_each_referrer<E>(
        &self,
        number: u32,
        f: impl FnMut(u32) -> std::result::Result<(), E>,
    ) -> std::result::Result<(), E>;
}

impl RefGraph for References {
    fn trailer_contains(&self, number: u32) -> bool {
        Self::trailer(self).binary_search(&number).is_ok()
    }

    fn for_each_target<E>(
        &self,
        number: u32,
        mut f: impl FnMut(u32) -> std::result::Result<(), E>,
    ) -> std::result::Result<(), E> {
        for &target in Self::targets(self, number) {
            f(target)?;
        }
        Ok(())
    }

    fn for_each_referrer<E>(
        &self,
        number: u32,
        mut f: impl FnMut(u32) -> std::result::Result<(), E>,
    ) -> std::result::Result<(), E> {
        for &referrer in Self::referrers(self, number) {
            f(referrer)?;
        }
        Ok(())
    }
}

/// Worklist / output storage used by [`nominated_unreferenced`] and
/// [`unreferenced`]: backed by `BudgetVec` in production and by a fixed
/// stack bitset in bounded Kani graph proofs.
trait NodeQueue<'c, C: GraphContext> {
    fn new_queue(ctx: &'c C) -> Self;
    fn push_node(&mut self, number: u32) -> std::result::Result<(), C::Error>;
    fn pop_node(&mut self) -> Option<u32>;
}

impl<'b> NodeQueue<'b, Budget> for BudgetVec<'b, u32> {
    fn new_queue(budget: &'b Budget) -> Self {
        Self::new(budget)
    }

    fn push_node(&mut self, number: u32) -> Result<()> {
        Ok(self.try_push(number)?)
    }

    fn pop_node(&mut self) -> Option<u32> {
        self.pop()
    }
}

/// Ascending integer set used by [`nominated_unreferenced`] and
/// [`unreferenced`]: backed by `Table<()>` in production and by a tiny
/// stack bitset inside bounded Kani graph proofs.
trait NodeSet<C: GraphContext>: Default {
    fn contains(&self, number: u32) -> bool;
    fn insert_node(&mut self, number: u32, ctx: &C) -> std::result::Result<(), C::Error>;
    fn for_each_ascending(
        &self,
        f: impl FnMut(u32) -> std::result::Result<(), C::Error>,
    ) -> std::result::Result<(), C::Error>;
}

impl NodeSet<Budget> for Table<()> {
    fn contains(&self, number: u32) -> bool {
        self.get(number).is_some()
    }

    fn insert_node(&mut self, number: u32, budget: &Budget) -> Result<()> {
        self.insert(number, (), budget)?;
        Ok(())
    }

    fn for_each_ascending(&self, mut f: impl FnMut(u32) -> Result<()>) -> Result<()> {
        for (number, ()) in self.iter() {
            f(number)?;
        }
        Ok(())
    }
}

/// Input list of object numbers consumed by [`nominated_unreferenced`] and
/// [`unreferenced`]: `&[u32]` in production and a stack mask in Kani.
trait NodeList {
    fn for_each_node<E>(
        &self,
        f: impl FnMut(u32) -> std::result::Result<(), E>,
    ) -> std::result::Result<(), E>;
}

impl NodeList for [u32] {
    fn for_each_node<E>(
        &self,
        mut f: impl FnMut(u32) -> std::result::Result<(), E>,
    ) -> std::result::Result<(), E> {
        for &number in self {
            f(number)?;
        }
        Ok(())
    }
}

/// Lists only nominated objects for which the trailer and every outside
/// object, including an otherwise unreachable owner, hold no reference.
/// References among nominees may form cycles. A retained nominee keeps
/// its nominated targets without traversing the rest of the document.
pub fn nominated_unreferenced<'b>(
    index: &References,
    candidates: &[u32],
    budget: &'b Budget,
) -> Result<BudgetVec<'b, u32>> {
    nominated_unreferenced_in::<References, [u32], Budget, Table<()>, BudgetVec<'b, u32>>(
        index, candidates, budget,
    )
}

fn nominated_unreferenced_in<
    'c,
    G: RefGraph,
    L: NodeList + ?Sized,
    C: GraphContext,
    S: NodeSet<C>,
    Q: NodeQueue<'c, C>,
>(
    index: &G,
    candidates: &L,
    ctx: &'c C,
) -> std::result::Result<Q, C::Error> {
    ctx.poll()?;
    let mut nominated = S::default();
    candidates.for_each_node(|number| {
        ctx.poll()?;
        nominated.insert_node(number, ctx)
    })?;
    let mut kept = S::default();
    let mut pending = Q::new_queue(ctx);
    nominated.for_each_ascending(|number| {
        ctx.poll()?;
        let mut outside = index.trailer_contains(number);
        if !outside {
            index.for_each_referrer(number, |referrer| {
                ctx.poll()?;
                if !nominated.contains(referrer) {
                    outside = true;
                }
                Ok(())
            })?;
        }
        if outside {
            kept.insert_node(number, ctx)?;
            pending.push_node(number)?;
        }
        Ok(())
    })?;
    while let Some(number) = pending.pop_node() {
        index.for_each_target(number, |target| {
            ctx.poll()?;
            if nominated.contains(target) && !kept.contains(target) {
                kept.insert_node(target, ctx)?;
                pending.push_node(target)?;
            }
            Ok(())
        })?;
    }
    let mut unused = Q::new_queue(ctx);
    nominated.for_each_ascending(|number| {
        ctx.poll()?;
        if !kept.contains(number) {
            unused.push_node(number)?;
        }
        Ok(())
    })?;
    Ok(unused)
}

/// Returns the objects that a structural edit frees, ascending: `seeds`,
/// which leave whatever refers to them; the objects of `candidates`, and
/// the objects that refer to them, that nothing refers to but freed objects
/// and other such objects, so that a cycle of a candidate and what refers
/// to it goes; and every object that a freed object refers to whose
/// referrers are all freed. An object that the trailer refers to stays.
/// `index` is the index of the version after the edit's other changes.
pub fn unreferenced<'b>(
    index: &References,
    seeds: &[u32],
    candidates: &[u32],
    budget: &'b Budget,
) -> Result<BudgetVec<'b, u32>> {
    unreferenced_in::<References, [u32], Budget, Table<()>, BudgetVec<'b, u32>>(
        index, seeds, candidates, budget,
    )
}

fn unreferenced_in<
    'c,
    G: RefGraph,
    L: NodeList + ?Sized,
    C: GraphContext,
    S: NodeSet<C>,
    Q: NodeQueue<'c, C>,
>(
    index: &G,
    seeds: &L,
    candidates: &L,
    ctx: &'c C,
) -> std::result::Result<Q, C::Error> {
    ctx.poll()?;
    let mut forced = S::default();
    let mut reachable = S::default();
    let mut kept = S::default();
    let mut out = Q::new_queue(ctx);
    let mut pending = Q::new_queue(ctx);
    seeds.for_each_node(|seed| {
        if !index.trailer_contains(seed) {
            forced.insert_node(seed, ctx)?;
        }
        pending.push_node(seed)
    })?;
    candidates.for_each_node(|candidate| {
        pending.push_node(candidate)?;
        // One level of referrers may leave with an orphaned structure element
        // or annotation (for example, its otherwise unreferenced parent).
        index.for_each_referrer(candidate, |referrer| pending.push_node(referrer))
    })?;
    // First collect the possible garbage, including strongly connected
    // components. Reference counting alone cannot collect an orphan cycle.
    while let Some(number) = pending.pop_node() {
        ctx.poll()?;
        if reachable.contains(number) {
            continue;
        }
        // Trailer roots and their directly referenced objects are proven
        // live. Keep them outside this closure: both the discovery walk
        // and later liveness propagation must avoid their retained subtrees.
        // Explicit removals still override a root's edge, except for the
        // trailer roots themselves.
        if index.trailer_contains(number)
            || (!forced.contains(number) && retained_by_root(index, number, ctx)?)
        {
            continue;
        }
        reachable.insert_node(number, ctx)?;
        index.for_each_target(number, |target| {
            if !reachable.contains(target) {
                pending.push_node(target)?;
            }
            Ok(())
        })?;
    }
    // Incoming edges from outside the candidate closure keep an object,
    // and everything it reaches, alive. Explicitly removed seeds leave.
    reachable.for_each_ascending(|number| {
        ctx.poll()?;
        if !forced.contains(number) {
            let mut keep = index.trailer_contains(number);
            if !keep {
                index.for_each_referrer(number, |referrer| {
                    if !reachable.contains(referrer) {
                        keep = true;
                    }
                    Ok(())
                })?;
            }
            if keep {
                pending.push_node(number)?;
            }
        }
        Ok(())
    })?;
    while let Some(number) = pending.pop_node() {
        ctx.poll()?;
        if forced.contains(number) || kept.contains(number) || !reachable.contains(number) {
            continue;
        }
        kept.insert_node(number, ctx)?;
        index.for_each_target(number, |target| {
            if reachable.contains(target) && !kept.contains(target) && !forced.contains(target) {
                pending.push_node(target)?;
            }
            Ok(())
        })?;
    }
    // `for_each_ascending` yields numbers in ascending order.
    reachable.for_each_ascending(|number| {
        ctx.poll()?;
        if !kept.contains(number) && !index.trailer_contains(number) {
            out.push_node(number)?;
        }
        Ok(())
    })?;
    ctx.poll()?;
    Ok(out)
}

/// Updates `cache`'s index across `transaction`'s uncommitted changes, finds
/// the objects that `seeds` and `candidates` leave unreferenced
/// ([`unreferenced`]), frees each in `transaction`, and returns the freed
/// object numbers, ascending.
pub fn free_unreferenced<'b>(
    cache: &Cache,
    transaction: &mut Transaction,
    seeds: &[u32],
    candidates: &[u32],
    budget: &'b Budget,
) -> Result<BudgetVec<'b, u32>> {
    budget.poll()?;
    if seeds.is_empty() && candidates.is_empty() {
        return Ok(BudgetVec::new(budget));
    }
    let index = cache.in_transaction(transaction, budget)?;
    let freed = unreferenced(&index, seeds, candidates, budget)?;
    for &number in freed.iter() {
        transaction.free(number, budget)?;
    }
    Ok(freed)
}

/// Returns the refusal of an edit that would treat a page, a page tree
/// node, or the catalog as navigation.
pub(super) fn aliasing() -> Error {
    Error::malformed("navigation tree cannot alias a page or catalog dictionary")
}

/// Whether a trailer root still refers to this object after the edit.
fn retained_by_root<G: RefGraph, C: GraphContext>(
    index: &G,
    number: u32,
    ctx: &C,
) -> std::result::Result<bool, C::Error> {
    let mut retained = false;
    index.for_each_referrer(number, |owner| {
        ctx.poll()?;
        if index.trailer_contains(owner) {
            retained = true;
        }
        Ok(())
    })?;
    Ok(retained)
}

#[cfg(kani)]
mod kani_proofs {
    use std::convert::Infallible;

    use super::*;

    const N: usize = 3;

    struct NoBudget;

    impl GraphContext for NoBudget {
        type Error = Infallible;

        fn poll(&self) -> std::result::Result<(), Infallible> {
            Ok(())
        }
    }

    /// Stack-allocated 3-node directed graph (nodes 1..=3) implementing
    /// `RefGraph` directly over its adjacency matrix so Kani verifies
    /// `nominated_unreferenced_in` and `unreferenced_in` across all 2^9 = 512
    /// directed graphs (self-loops, 2-cycles, 3-cycles, transitive chains,
    /// diamonds) and all trailer/seed/candidate subsets with zero heap or
    /// symbolic slice-pointer overhead.
    struct TinyGraph {
        edges: [[bool; N]; N],
        trailer_mask: [bool; N],
    }

    impl TinyGraph {
        fn symbolic() -> Self {
            Self {
                edges: kani::any(),
                trailer_mask: kani::any(),
            }
        }
    }

    impl RefGraph for TinyGraph {
        fn trailer_contains(&self, number: u32) -> bool {
            number >= 1 && (number as usize) <= N && self.trailer_mask[(number - 1) as usize]
        }

        fn for_each_target<E>(
            &self,
            number: u32,
            mut f: impl FnMut(u32) -> std::result::Result<(), E>,
        ) -> std::result::Result<(), E> {
            if number >= 1 && (number as usize) <= N {
                let u = (number - 1) as usize;
                for v in 0..N {
                    if self.edges[u][v] {
                        f((v + 1) as u32)?;
                    }
                }
            }
            Ok(())
        }

        fn for_each_referrer<E>(
            &self,
            number: u32,
            mut f: impl FnMut(u32) -> std::result::Result<(), E>,
        ) -> std::result::Result<(), E> {
            if number >= 1 && (number as usize) <= N {
                let v = (number - 1) as usize;
                for u in 0..N {
                    if self.edges[u][v] {
                        f((u + 1) as u32)?;
                    }
                }
            }
            Ok(())
        }
    }

    struct TinyMask([bool; N]);

    impl NodeList for TinyMask {
        fn for_each_node<E>(
            &self,
            mut f: impl FnMut(u32) -> std::result::Result<(), E>,
        ) -> std::result::Result<(), E> {
            for i in 0..N {
                if self.0[i] {
                    f((i + 1) as u32)?;
                }
            }
            Ok(())
        }
    }

    /// Deduplicating per-phase stack worklist / node collector (zero heap
    /// allocation). Both `nominated_unreferenced_in` and `unreferenced_in`
    /// compute monotone reachability fixpoints whose per-phase predicates
    /// (`forced`, `trailer`, `retained_by_root`, `reachable`) are static, so
    /// popping any node a second time in the same phase is a no-op; `seen`
    /// resets when the phase drains (`pop_node()` returns `None`).
    struct TinyWorklist {
        pending: [bool; N],
        seen: [bool; N],
    }

    impl<'c> NodeQueue<'c, NoBudget> for TinyWorklist {
        fn new_queue(_ctx: &'c NoBudget) -> Self {
            Self {
                pending: [false; N],
                seen: [false; N],
            }
        }

        fn push_node(&mut self, number: u32) -> std::result::Result<(), Infallible> {
            assert!((1..=N as u32).contains(&number));
            let idx = (number - 1) as usize;
            if !self.seen[idx] {
                self.pending[idx] = true;
                self.seen[idx] = true;
            }
            Ok(())
        }

        fn pop_node(&mut self) -> Option<u32> {
            for i in 0..N {
                if self.pending[i] {
                    self.pending[i] = false;
                    return Some((i + 1) as u32);
                }
            }
            self.seen = [false; N];
            None
        }
    }

    #[derive(Default)]
    struct TinySet([bool; N]);

    impl NodeSet<NoBudget> for TinySet {
        fn contains(&self, number: u32) -> bool {
            number >= 1 && (number as usize) <= N && self.0[(number - 1) as usize]
        }

        fn insert_node(
            &mut self,
            number: u32,
            _ctx: &NoBudget,
        ) -> std::result::Result<(), Infallible> {
            if number >= 1 && (number as usize) <= N {
                self.0[(number - 1) as usize] = true;
            }
            Ok(())
        }

        fn for_each_ascending(
            &self,
            mut f: impl FnMut(u32) -> std::result::Result<(), Infallible>,
        ) -> std::result::Result<(), Infallible> {
            for i in 0..N {
                if self.0[i] {
                    f((i + 1) as u32)?;
                }
            }
            Ok(())
        }
    }

    /// Computes exact transitive reachability from `roots` over `edges`,
    /// stopping at any node in `blocked`.
    fn reachable(edges: &[[bool; N]; N], roots: [bool; N], blocked: [bool; N]) -> [bool; N] {
        let mut vis = [false; N];
        for i in 0..N {
            if roots[i] && !blocked[i] {
                vis[i] = true;
            }
        }
        for _ in 0..N {
            for u in 0..N {
                if vis[u] {
                    for v in 0..N {
                        if edges[u][v] && !blocked[v] {
                            vis[v] = true;
                        }
                    }
                }
            }
        }
        vis
    }

    /// Proves `nominated_unreferenced` across all 3-node directed graphs and
    /// all trailer/candidate subsets:
    /// - Only nominated nodes are returned.
    /// - A nominated node is returned iff it is NOT reachable from the trailer
    ///   or from any non-nominated outside node (even across arbitrary cycles).
    #[kani::proof]
    #[kani::unwind(5)]
    fn kani_nominated_unreferenced_exact_on_all_3_node_graphs() {
        let graph = TinyGraph::symbolic();
        let cand_mask: [bool; N] = kani::any();
        let cands = TinyMask(cand_mask);
        let ctx = NoBudget;
        let Ok(unused) =
            nominated_unreferenced_in::<TinyGraph, TinyMask, NoBudget, TinySet, TinyWorklist>(
                &graph, &cands, &ctx,
            );

        let mut roots = [false; N];
        for i in 0..N {
            if graph.trailer_mask[i] || !cand_mask[i] {
                roots[i] = true;
            }
        }
        let live = reachable(&graph.edges, roots, [false; N]);

        for i in 0..N {
            assert_eq!(unused.pending[i], cand_mask[i] && !live[i]);
        }
    }

    /// Proves `unreferenced` safety across all 3-node directed graphs and all
    /// trailer/seed/candidate subsets:
    /// - No trailer root is ever freed.
    /// - No non-seed node reachable from the trailer without passing through a
    ///   forced seed is ever freed.
    /// - No freed node has a non-freed referrer (unless it is a forced seed).
    #[kani::proof]
    #[kani::unwind(5)]
    fn kani_unreferenced_never_frees_live_objects_on_all_3_node_graphs() {
        let graph = TinyGraph::symbolic();
        let seed_mask: [bool; N] = kani::any();
        let cand_mask: [bool; N] = kani::any();

        let mut forced_mask = [false; N];
        for i in 0..N {
            if seed_mask[i] && !graph.trailer_mask[i] {
                forced_mask[i] = true;
            }
        }

        let seeds = TinyMask(seed_mask);
        let cands = TinyMask(cand_mask);
        let ctx = NoBudget;
        let Ok(freed) = unreferenced_in::<TinyGraph, TinyMask, NoBudget, TinySet, TinyWorklist>(
            &graph, &seeds, &cands, &ctx,
        );

        let live_from_trailer = reachable(&graph.edges, graph.trailer_mask, forced_mask);
        for i in 0..N {
            // Trailer roots are never freed.
            if graph.trailer_mask[i] {
                assert!(!freed.pending[i]);
            }
            // Any non-forced object reachable from the trailer stays alive.
            if live_from_trailer[i] {
                assert!(!freed.pending[i]);
            }
            // Every non-forced freed object has all its referrers freed too.
            if freed.pending[i] && !forced_mask[i] {
                for u in 0..N {
                    if graph.edges[u][i] {
                        assert!(freed.pending[u]);
                    }
                }
            }
        }
    }
}
