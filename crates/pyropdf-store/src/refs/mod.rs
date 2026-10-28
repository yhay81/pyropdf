//! The reverse-reference index of a version (DESIGN.md section 8): which
//! objects refer to an object.
//!
//! [`References`] records, for every object number, the objects whose
//! values hold a reference to it, and the numbers that each object's value
//! and the trailer refer to. Where a referrer holds a reference, and in
//! which role, is read from the referrer when a structural edit asks
//! ([`find`]): the referrer's value is what the index was built from, so
//! the index keeps two numbers per edge instead of paths.
//!
//! The index of a version derives from the index of another version of
//! the same document and the change set between them
//! ([`References::update`]), and reads only the objects that the change
//! set names. [`Cache`] keeps the index of the version asked for last, and
//! remembers that version by a weak reference: it never keeps a version
//! alive (DESIGN.md section 10).
//!
//! A reference counts whatever its generation, so a referrer that names a
//! number with another generation, which reads as null, is still listed;
//! [`find`] returns the generation that each reference names. A store read
//! error refuses the operation rather than hiding edges. The index records edges
//! and never follows them, so reference chains and cycles cost nothing.

mod roles;
#[cfg(test)]
mod tests;

use std::sync::{Arc, Mutex, MutexGuard, PoisonError, Weak};

use crate::LimitExceeded;
use crate::Result;
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::memo::Reads;
use crate::value::{Dict, Name, Reference, Value};
use crate::version::table::Table;
use crate::version::{ChangeSet, Version};

pub use roles::Role;

/// The references of one version, both ways.
#[derive(Clone, Default)]
pub struct References {
    /// The numbers that each object's value refers to, ascending and
    /// distinct; empty for an object that refers to none.
    targets: Table<Arc<[u32]>>,
    /// The objects whose values refer to each number, ascending and
    /// distinct; empty for a number that none refers to.
    referrers: Table<Arc<[u32]>>,
    /// The numbers that the trailer refers to, ascending and distinct.
    trailer: Arc<[u32]>,
}

impl References {
    /// Builds the index of the version that `reads` reads, from every
    /// object that it holds and from its trailer.
    pub fn build(reads: &mut Reads<'_>) -> Result<Self> {
        let budget = reads.budget();
        budget.poll()?;
        let version = reads.version();
        let mut numbers = BudgetVec::new(budget);
        version.base().numbers(&mut numbers)?;
        for (number, _) in version.slots() {
            budget.poll()?;
            numbers.try_push(number)?;
        }
        numbers.sort_unstable();
        dedup(&mut numbers);
        budget.poll()?;
        let mut index = Self::default();
        let mut edges: BudgetVec<'_, (u32, u32)> = BudgetVec::new(budget);
        let mut targets = BudgetVec::new(budget);
        for &number in numbers.iter() {
            object_targets(reads, number, &mut targets)?;
            if targets.is_empty() {
                continue;
            }
            index
                .targets
                .insert(number, list(&targets, budget)?, budget)?;
            for &target in targets.iter() {
                edges.try_push((target, number))?;
            }
        }
        drop(numbers);
        edges.sort_unstable();
        let mut referrers = BudgetVec::new(budget);
        let mut at = 0;
        while at < edges.len() {
            budget.poll()?;
            let target = edges[at].0;
            referrers.clear();
            while at < edges.len() && edges[at].0 == target {
                referrers.try_push(edges[at].1)?;
                at += 1;
            }
            index
                .referrers
                .insert(target, list(&referrers, budget)?, budget)?;
        }
        dict_targets(reads.trailer(), &mut targets)?;
        index.trailer = list(&targets, budget)?;
        Ok(index)
    }

    /// Returns the index of the version that `reads` reads, from this one,
    /// the index of a version whose change set to it is `changes`. Reads
    /// the objects that `changes` names, and the trailer if it changed.
    pub fn update(&self, reads: &mut Reads<'_>, changes: &ChangeSet) -> Result<Self> {
        let budget = reads.budget();
        budget.poll()?;
        let mut index = self.clone();
        let mut targets = BudgetVec::new(budget);
        for &referrer in changes.objects() {
            object_targets(reads, referrer, &mut targets)?;
            let old = index.targets.get(referrer).cloned().unwrap_or_default();
            if *old == *targets {
                continue;
            }
            index
                .targets
                .insert(referrer, list(&targets, budget)?, budget)?;
            let (mut a, mut b) = (0, 0);
            while a < old.len() || b < targets.len() {
                budget.poll()?;
                match (old.get(a), targets.get(b)) {
                    (Some(&gone), Some(&kept)) if gone == kept => {
                        a += 1;
                        b += 1;
                    }
                    (Some(&gone), Some(&added)) if gone > added => {
                        index.link(added, referrer, budget)?;
                        b += 1;
                    }
                    (Some(&gone), _) => {
                        index.unlink(gone, referrer, budget)?;
                        a += 1;
                    }
                    (None, Some(&added)) => {
                        index.link(added, referrer, budget)?;
                        b += 1;
                    }
                    (None, None) => break,
                }
            }
        }
        if changes.trailer() {
            dict_targets(reads.trailer(), &mut targets)?;
            index.trailer = list(&targets, budget)?;
        }
        Ok(index)
    }

    /// Returns the objects whose values refer to object `number`,
    /// ascending.
    #[must_use]
    pub fn referrers(&self, number: u32) -> &[u32] {
        self.referrers.get(number).map_or(&[], |list| list)
    }

    /// Returns the numbers that the value of object `number` refers to,
    /// ascending.
    #[must_use]
    pub fn targets(&self, number: u32) -> &[u32] {
        self.targets.get(number).map_or(&[], |list| list)
    }

    /// Returns the numbers that the trailer refers to, ascending.
    #[must_use]
    pub fn trailer(&self) -> &[u32] {
        &self.trailer
    }

    /// Adds `referrer` to the referrers of `target`.
    fn link(&mut self, target: u32, referrer: u32, budget: &Budget) -> Result<()> {
        let old = self.referrers(target);
        let at = old.partition_point(|&number| number < referrer);
        let mut new = BudgetVec::new(budget);
        new.try_reserve_exact(old.len() + 1)?;
        new.try_extend_from_slice(&old[..at])?;
        new.try_push(referrer)?;
        new.try_extend_from_slice(&old[at..])?;
        self.referrers.insert(target, list(&new, budget)?, budget)?;
        Ok(())
    }

    /// Removes `referrer` from the referrers of `target`.
    fn unlink(&mut self, target: u32, referrer: u32, budget: &Budget) -> Result<()> {
        let old = self.referrers(target);
        let Ok(at) = old.binary_search(&referrer) else {
            return Ok(());
        };
        let mut new = BudgetVec::new(budget);
        new.try_reserve_exact(old.len() - 1)?;
        new.try_extend_from_slice(&old[..at])?;
        new.try_extend_from_slice(&old[at + 1..])?;
        self.referrers.insert(target, list(&new, budget)?, budget)?;
        Ok(())
    }
}

impl PartialEq for References {
    /// Two indexes are equal when they record the same edges, whatever
    /// empty lists either keeps.
    fn eq(&self, other: &Self) -> bool {
        self.trailer == other.trailer
            && edges(&self.targets).eq(edges(&other.targets))
            && edges(&self.referrers).eq(edges(&other.referrers))
    }
}

impl std::fmt::Debug for References {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let count = |table| edges(table).count();
        f.debug_struct("References")
            .field("referrers", &count(&self.targets))
            .field("targets", &count(&self.referrers))
            .field("trailer", &self.trailer)
            .finish()
    }
}

/// Returns the numbers of `table` whose lists are not empty, with their
/// lists.
fn edges(table: &Table<Arc<[u32]>>) -> impl Iterator<Item = (u32, &[u32])> {
    table
        .iter()
        .filter(|(_, list)| !list.is_empty())
        .map(|(number, list)| (number, &**list))
}

/// Removes the repeats of a sorted vector.
fn dedup(numbers: &mut BudgetVec<'_, u32>) {
    let mut kept = 0;
    for at in 0..numbers.len() {
        if kept == 0 || numbers[kept - 1] != numbers[at] {
            numbers[kept] = numbers[at];
            kept += 1;
        }
    }
    numbers.truncate(kept);
}

/// Returns `numbers` as a shared list, charged to `budget`.
fn list(numbers: &[u32], budget: &Budget) -> Result<Arc<[u32]>, LimitExceeded> {
    budget.charge(
        LimitCode::MemorySize,
        (2 * size_of::<usize>() + size_of_val(numbers)) as u64,
    )?;
    Ok(Arc::from(numbers))
}

/// Sets `out` to the numbers that the value of object `number` refers to,
/// ascending and distinct: none for an absent object. Store errors propagate.
fn object_targets(reads: &mut Reads<'_>, number: u32, out: &mut BudgetVec<'_, u32>) -> Result<()> {
    out.clear();
    if let Some(object) = reads.get(number)? {
        walk(Start::Value(object.value()), out)?;
    }
    Ok(())
}

/// Sets `out` to the numbers that `dict` refers to, ascending and
/// distinct.
fn dict_targets(dict: &Dict, out: &mut BudgetVec<'_, u32>) -> Result<()> {
    out.clear();
    walk(Start::Entries(dict), out)
}

/// Where a walk starts: at a value, or at the entries of the trailer, which
/// is no value.
#[derive(Clone, Copy)]
enum Start<'v> {
    Value(&'v Value),
    Entries(&'v Dict),
}

/// One step of a walk: how a value is reached from its container.
#[derive(Clone, Copy)]
enum At<'v> {
    /// The value where the walk starts.
    Start,
    Key(&'v Name),
    Index(usize),
}

/// Walks the values that `start` holds, depth first with the last child of
/// a container first, and calls `each` with each reference and the steps
/// from `start` to it. Refuses with `object_depth` a container deeper than
/// the budget allows, the outermost being at depth 1.
fn visit<'v>(
    start: Start<'v>,
    budget: &Budget,
    mut each: impl FnMut(&[At<'v>], Reference) -> Result<()>,
) -> Result<()> {
    budget.poll()?;
    let mut stack: BudgetVec<'_, (usize, At<'v>, &'v Value)> = BudgetVec::new(budget);
    match start {
        Start::Value(value) => stack.try_push((0, At::Start, value))?,
        Start::Entries(dict) => {
            check_depth(budget, 0)?;
            push_entries(&mut stack, 0, dict)?;
        }
    }
    let mut path: BudgetVec<'_, At<'v>> = BudgetVec::new(budget);
    while let Some((depth, at, value)) = stack.pop() {
        budget.poll()?;
        path.truncate(depth);
        if !matches!(at, At::Start) {
            path.try_push(at)?;
        }
        let depth = path.len();
        let dict = match value {
            Value::Reference(reference) => {
                each(&path, *reference)?;
                continue;
            }
            Value::Array(items) => {
                check_depth(budget, depth)?;
                for (index, item) in items.iter().enumerate() {
                    budget.poll()?;
                    if holds_references(item) {
                        stack.try_push((depth, At::Index(index), item))?;
                    }
                }
                continue;
            }
            Value::Dict(dict) => dict,
            Value::Stream(stream) => stream.dict(),
            _ => continue,
        };
        check_depth(budget, depth)?;
        push_entries(&mut stack, depth, dict)?;
    }
    Ok(())
}

/// Refuses with `object_depth` a container whose path from the start has
/// `steps` steps, when it is deeper than `budget` allows.
fn check_depth(budget: &Budget, steps: usize) -> Result<(), LimitExceeded> {
    budget.check(
        LimitCode::ObjectDepth,
        u64::try_from(steps.saturating_add(1)).unwrap_or(u64::MAX),
    )
}

fn push_entries<'v>(
    stack: &mut BudgetVec<'_, (usize, At<'v>, &'v Value)>,
    depth: usize,
    dict: &'v Dict,
) -> Result<(), LimitExceeded> {
    for (key, item) in dict.iter() {
        stack.budget().poll()?;
        if holds_references(item) {
            stack.try_push((depth, At::Key(key), item))?;
        }
    }
    Ok(())
}

/// Returns whether `value` is a reference or may hold one.
const fn holds_references(value: &Value) -> bool {
    matches!(
        value,
        Value::Reference(_) | Value::Array(_) | Value::Dict(_) | Value::Stream(_)
    )
}

/// Sets `out` to the numbers that `start` refers to, ascending and
/// distinct. `out` is empty on entry.
fn walk(start: Start<'_>, out: &mut BudgetVec<'_, u32>) -> Result<()> {
    let budget = out.budget();
    visit(start, budget, |_, reference| {
        out.try_push(reference.number())?;
        Ok(())
    })?;
    out.sort_unstable();
    dedup(out);
    budget.poll()?;
    Ok(())
}

/// Who holds a reference: an object, or the trailer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Referrer {
    /// The object of this number.
    Object(u32),
    /// The trailer.
    Trailer,
}

/// A step of the path from a referrer's value to a reference in it: a key
/// of a dictionary, the dictionary of a stream included, or a position in
/// an array.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Step {
    /// The value of this key.
    Key(Name),
    /// The item at this position.
    Index(usize),
}

/// A reference that a referrer holds: where, what it names, and its role.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Found {
    /// The steps from the referrer's value, or from the trailer, to the
    /// reference.
    pub path: Box<[Step]>,
    /// The reference, with the generation that it names.
    pub reference: Reference,
    /// The role that a matcher gives the reference, if one knows it; the
    /// trailer's references have none.
    pub role: Option<Role>,
}

/// Returns every reference to object `number` that `referrer` holds in the
/// version that `reads` reads, in the order of a depth-first walk that
/// visits the last child of a container first.
pub fn find<'b>(
    reads: &mut Reads<'b>,
    referrer: Referrer,
    number: u32,
) -> Result<BudgetVec<'b, Found>> {
    let budget = reads.budget();
    budget.poll()?;
    let mut found = BudgetVec::new(budget);
    let held = match referrer {
        Referrer::Trailer => None,
        Referrer::Object(referrer) => match reads.get(referrer)? {
            Some(held) => Some(held),
            None => return Ok(found),
        },
    };
    let top = held.as_ref().map(|held| &**held.value());
    let start = top.map_or_else(|| Start::Entries(reads.trailer()), Start::Value);
    visit(start, budget, |at, reference| {
        if reference.number() != number {
            return Ok(());
        }
        let mut path = BudgetVec::new(budget);
        for step in at {
            path.try_push(match step {
                At::Start => continue,
                At::Key(key) => {
                    let mut bytes = BudgetVec::new(budget);
                    bytes.try_extend_from_slice(key.as_bytes())?;
                    Step::Key(Name::new(bytes.into_vec().into_boxed_slice()))
                }
                At::Index(index) => Step::Index(*index),
            })?;
        }
        let path = path.into_vec().into_boxed_slice();
        let role = top.and_then(|top| roles::role(top, &path));
        found.try_push(Found {
            path,
            reference,
            role,
        })?;
        Ok(())
    })?;
    Ok(found)
}

/// The index of the version asked for last. It remembers the version by a
/// weak reference, and derives the index of the next version asked for
/// from it while that version is alive.
#[derive(Default)]
pub struct Cache {
    entry: Mutex<Option<(Weak<Version>, Arc<References>)>>,
}

impl Cache {
    /// Returns an empty cache.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Returns the index of `version`: the one held, the one held updated
    /// with the change set from its version, or a new one when the held
    /// version is gone or belongs to another document.
    pub fn get(&self, version: &Arc<Version>, budget: &Budget) -> Result<Arc<References>> {
        budget.poll()?;
        let held = self.lock().clone();
        let held = held.and_then(|(held, index)| Some((held.upgrade()?, index)));
        let mut reads = Reads::new(version, budget);
        let index = match held {
            Some((held, index)) if Arc::ptr_eq(&held, version) => return Ok(index),
            Some((held, index)) => match held.changes(version, budget)? {
                Some(changes) => index.update(&mut reads, &changes)?,
                None => References::build(&mut reads)?,
            },
            None => References::build(&mut reads)?,
        };
        let index = Arc::new(index);
        *self.lock() = Some((Arc::downgrade(version), Arc::clone(&index)));
        Ok(index)
    }

    fn lock(&self) -> MutexGuard<'_, Option<(Weak<Version>, Arc<References>)>> {
        self.entry.lock().unwrap_or_else(PoisonError::into_inner)
    }
}
