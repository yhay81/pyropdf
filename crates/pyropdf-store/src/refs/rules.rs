//! Referential rules (DESIGN.md section 8): what a structural edit does
//! with a reference to an object that it removes or moves.
//!
//! A structural edit plans its rules in [`Edits`], which reads nothing
//! that it changes, and writes them together, so a refusal while planning
//! leaves the version as it was. [`unreferenced`] then lists the objects
//! that the edit leaves unreferenced, for it to free.

use crate::budget::{Budget, BudgetVec};
use crate::memo::Reads;
use crate::value::{Reference, Value};
use crate::version::Transaction;
use crate::version::table::Table;
use crate::{Error, Result};

use super::{Found, References, Referrer, Role, Step, find};

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
}

impl<'b> Edits<'b> {
    /// Returns no edits, which grow through `budget`.
    #[must_use]
    pub const fn new(budget: &'b Budget) -> Self {
        Self {
            budget,
            edits: BudgetVec::new(budget),
            orphans: BudgetVec::new(budget),
        }
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
            Rule::RemoveEntry => self.push(referrer, path, None),
            Rule::Unresolve => self.push(referrer, &path[..path.len().min(1)], None),
            Rule::Retarget(to) => self.push(referrer, path, Some(to)),
            Rule::RemoveReferrer => {
                for &holder in index.referrers(referrer) {
                    for found in find(reads, Referrer::Object(holder), referrer)?.iter() {
                        if is_annotation_entry(reads, holder, found)? {
                            self.push(holder, &found.path, None)?;
                        }
                    }
                }
                self.orphans.try_push(referrer)?;
                Ok(())
            }
        }
    }

    /// Returns the referrers that the edits remove, ascending.
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
        let mut owned = BudgetVec::new(self.budget);
        owned.try_extend_from_slice(path)?;
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
        self.edits
            .sort_unstable_by(|a, b| a.number.cmp(&b.number).then_with(|| b.path.cmp(&a.path)));
        let mut at = 0;
        while at < self.edits.len() {
            budget.poll()?;
            let number = self.edits[at].number;
            let Some(object) = transaction.version().get(number, budget)? else {
                while at < self.edits.len() && self.edits[at].number == number {
                    at += 1;
                }
                continue;
            };
            let mut value = (**object.value()).clone();
            let mut last: Option<&[Step]> = None;
            while at < self.edits.len() && self.edits[at].number == number {
                let edit = &self.edits[at];
                if last != Some(&*edit.path) {
                    change(&mut value, &edit.path, edit.to);
                }
                last = Some(&edit.path);
                at += 1;
            }
            transaction.write(number, &value, budget)?;
        }
        self.edits.clear();
        Ok(())
    }
}

/// Returns whether `found`, a reference that `holder` holds, is an entry of
/// a page's annotations: of its `/Annots`, or of an array object that a
/// page names as them.
fn is_annotation_entry(reads: &mut Reads<'_>, holder: u32, found: &Found) -> Result<bool> {
    if found.role == Some(Role::PageAnnotation) {
        return Ok(true);
    }
    if !matches!(*found.path, [Step::Index(_)]) {
        return Ok(false);
    }
    Ok(match reads.get(holder) {
        Ok(Some(object)) => matches!(**object.value(), Value::Array(_)),
        Ok(None) => false,
        Err(error) if error.limit().is_some() => return Err(error),
        Err(_) => false,
    })
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

/// Returns the objects that a structural edit frees, ascending: `seeds`,
/// which leave whatever refers to them, and every object of `candidates`
/// or that a freed object refers to whose referrers are all freed. An
/// object that the trailer refers to stays. `index` is the index of the
/// version after the edit's other changes.
pub fn unreferenced<'b>(
    index: &References,
    seeds: &[u32],
    candidates: &[u32],
    budget: &'b Budget,
) -> Result<BudgetVec<'b, u32>> {
    let mut freed: Table<()> = Table::new();
    let mut out = BudgetVec::new(budget);
    let mut pending = BudgetVec::new(budget);
    for &seed in seeds {
        if freed.get(seed).is_none() {
            freed.insert(seed, (), budget)?;
            out.try_push(seed)?;
            pending.try_extend_from_slice(index.targets(seed))?;
        }
    }
    pending.try_extend_from_slice(candidates)?;
    while let Some(number) = pending.pop() {
        budget.poll()?;
        if freed.get(number).is_some()
            || index.trailer().binary_search(&number).is_ok()
            || !index
                .referrers(number)
                .iter()
                .all(|referrer| freed.get(*referrer).is_some())
        {
            continue;
        }
        freed.insert(number, (), budget)?;
        out.try_push(number)?;
        pending.try_extend_from_slice(index.targets(number))?;
    }
    out.sort_unstable();
    Ok(out)
}

/// Returns the refusal of an edit that would treat a page, a page tree
/// node, or the catalog as navigation.
pub(super) fn aliasing() -> Error {
    Error::malformed("navigation tree cannot alias a page or catalog dictionary")
}
