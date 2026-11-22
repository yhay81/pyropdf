//! Incarnations of direct values in name-tree pairs. Replacements and array
//! shifts cover only surviving locations. Undo reuses the same written root.

use std::cmp::Ordering as Cmp;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use super::{Slot, Version};
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::value::{MAX_NESTING, Value};
use crate::{Error, Result};

/// A direct path in a name tree, starting at its nearest indirect owner.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum NameStep {
    /// A catalog or name-tree dictionary key.
    Names,
    /// The catalog's embedded file name tree.
    EmbeddedFiles,
    /// The children of an intermediate node.
    Kids,
    /// An element of a direct array.
    Index(usize),
    /// A step outside an embedded file tree.
    Other,
}

impl NameStep {
    /// Converts a dictionary key on a path to a name-tree entry.
    #[must_use]
    pub fn key(key: &[u8]) -> Self {
        match key {
            b"Names" => Self::Names,
            b"EmbeddedFiles" => Self::EmbeddedFiles,
            b"Kids" => Self::Kids,
            _ => Self::Other,
        }
    }
}

/// Which part of a root a low-level assignment replaces. A native name-tree
/// rebuild copies surviving key/value pairs in their prior locations.
#[derive(Clone, Copy, Default)]
pub enum NameEdit<'a> {
    /// A native operation that may rebuild or rebalance a tree.
    #[default]
    Rebuild,
    /// One assigned value, including assignment of a complete owner.
    Set(&'a [NameStep]),
    /// An insertion or removal moves the following siblings.
    Shift(&'a [NameStep], usize),
}

/// One replacement, or the suffix of an array whose entries moved.
/// Later replacements discard the earlier rules they completely cover.
struct Rule {
    path: Box<[NameStep]>,
    suffix: Option<usize>,
    token: u64,
}

impl Rule {
    fn cmp(&self, path: &[NameStep], suffix: Option<usize>) -> Cmp {
        self.path
            .as_ref()
            .cmp(path)
            .then_with(|| self.suffix.cmp(&suffix))
    }

    fn covered(&self, path: &[NameStep], suffix: Option<usize>) -> bool {
        let Some(tail) = self.path.strip_prefix(path) else {
            return false;
        };
        match (suffix, tail) {
            (None, _) => true,
            (Some(first), [NameStep::Index(at), ..]) => *at >= first,
            (Some(first), []) => self.suffix.is_some_and(|at| at >= first),
            _ => false,
        }
    }
}

struct Changes {
    rules: Box<[Rule]>,
    size: u64,
}

/// One default incarnation and sparse exceptions, shared across unrelated
/// edits. Only rules for surviving subtrees and array suffixes are retained.
#[derive(Clone, Default)]
pub(super) struct NameSlots {
    default: u64,
    changes: Option<Arc<Changes>>,
}

static NEXT_SLOT: AtomicU64 = AtomicU64::new(1);

impl NameSlots {
    fn token(&self, path: &[NameStep]) -> u64 {
        let Some(changes) = &self.changes else {
            return self.default;
        };
        let rules = &changes.rules;
        let mut token = self.default;
        for end in 0..=path.len() {
            let prefix = &path[..end];
            if let Ok(i) = rules.binary_search_by(|r| r.cmp(prefix, None)) {
                token = token.max(rules[i].token);
            }
            if let Some(NameStep::Index(at)) = path.get(end) {
                let i = rules.partition_point(|r| !r.cmp(prefix, Some(*at)).is_gt());
                if let Some(rule) = i.checked_sub(1).and_then(|i| rules.get(i))
                    && rule.path.as_ref() == prefix
                    && rule.suffix.is_some()
                {
                    token = token.max(rule.token);
                }
            }
        }
        token
    }

    pub(super) fn size(&self) -> u64 {
        self.changes.as_ref().map_or(0, |c| c.size)
    }

    pub(super) fn shared_size(&self, other: &Self) -> u64 {
        match (&self.changes, &other.changes) {
            (Some(a), Some(b)) if Arc::ptr_eq(a, b) => a.size,
            _ => 0,
        }
    }

    pub(super) fn replaced() -> Result<Self> {
        Ok(Self {
            default: fresh()?,
            changes: None,
        })
    }

    fn with_rules(default: u64, mut rules: BudgetVec<Rule>, budget: &Budget) -> Result<Self> {
        if rules.is_empty() {
            return Ok(Self {
                default,
                changes: None,
            });
        }
        rules.sort_unstable_by(|a, b| a.cmp(&b.path, b.suffix));
        let overhead = size_of::<Changes>() + 2 * size_of::<usize>();
        budget.charge(LimitCode::MemorySize, overhead as u64)?;
        let size = rules.iter().fold(overhead as u64, |n, rule| {
            n.saturating_add((size_of::<Rule>() + size_of_val(rule.path.as_ref())) as u64)
        });
        Ok(Self {
            default,
            changes: Some(Arc::new(Changes {
                rules: rules.into_vec().into_boxed_slice(),
                size,
            })),
        })
    }

    fn insert_rule(
        rules: &mut BudgetVec<Rule>,
        path: &[NameStep],
        suffix: Option<usize>,
        token: u64,
        budget: &Budget,
    ) -> Result<()> {
        let mut saved = BudgetVec::new(budget);
        saved.try_extend_from_slice(path)?;
        rules.try_push(Rule {
            path: saved.into_vec().into_boxed_slice(),
            suffix,
            token,
        })?;
        Ok(())
    }

    fn edited(
        &self,
        value: &Value,
        path: &[NameStep],
        suffix: Option<usize>,
        budget: &Budget,
    ) -> Result<Self> {
        if path.contains(&NameStep::Other) {
            return Ok(self.clone()); // An edited file property or an unrelated key.
        }
        if path.is_empty() && suffix.is_none() {
            return Self::replaced();
        }
        let mut rules = BudgetVec::new(budget);
        if let Some(changes) = &self.changes {
            for rule in &changes.rules {
                if !rule.covered(path, suffix) {
                    Self::insert_rule(&mut rules, &rule.path, rule.suffix, rule.token, budget)?;
                }
            }
        }
        // A removed value has no descendants. An insertion or later assignment
        // marks its replacement, so deleting a transient key retains no record.
        let keep = match (at_path(value, path), suffix) {
            (Some(Value::Array(array)), Some(first)) => first < array.len(),
            (Some(Value::Array(_) | Value::Dict(_)), None) => true,
            (Some(_), None) => path.split_last().is_some_and(|(last, parent)| {
                matches!(last, NameStep::Index(at) if at % 2 == 1 &&
                    matches!(at_path(value, parent), Some(Value::Array(a)) if matches!(a.get(at - 1), Some(Value::String(_)))))
            }),
            _ => false,
        };
        if keep {
            Self::insert_rule(&mut rules, path, suffix, fresh()?, budget)?;
        }
        Self::with_rules(self.default, rules, budget)
    }

    /// Only a native dictionary rewrite may rebalance a name tree. All other
    /// mutations have their exact path and never enumerate an array's entries.
    pub(super) fn written(
        version: &Version,
        number: u32,
        value: &Value,
        edit: NameEdit<'_>,
        budget: &Budget,
    ) -> Result<Self> {
        if version.generation(number).is_none() {
            return Ok(Self::default());
        }
        let previous = version.name_slots(number).cloned().unwrap_or_default();
        match edit {
            NameEdit::Set(path) => return previous.edited(value, path, None, budget),
            NameEdit::Shift(path, at) => return previous.edited(value, path, Some(at), budget),
            NameEdit::Rebuild if matches!(value, Value::Array(_)) => return Self::replaced(),
            NameEdit::Rebuild if !matches!(value, Value::Dict(_)) => return Ok(Self::default()),
            NameEdit::Rebuild => {}
        }
        let mut rules = BudgetVec::new(budget);
        let mut old: Option<Option<Arc<Value>>> = None;
        let mut path = [NameStep::Other; MAX_NESTING + 1];
        visit(value, &mut path, 0, budget, &mut |parent, names| {
            // Resolve the old array once per leaf, independent of pair count.
            let before = match &old {
                Some(v) => v,
                None => old.insert(version.get(number, budget)?.map(|o| Arc::clone(o.value()))),
            };
            let original = before
                .as_deref()
                .and_then(|root| at_path(root, parent))
                .and_then(|v| match v {
                    Value::Array(a) => Some(a),
                    _ => None,
                });
            let mut path = [NameStep::Other; MAX_NESTING + 1];
            path[..parent.len()].copy_from_slice(parent);
            for (pair, [key, item]) in names.as_slice().as_chunks::<2>().0.iter().enumerate() {
                if pair % 1024 == 0 {
                    budget.poll()?;
                }
                if !matches!(key, Value::String(_)) || matches!(item, Value::Reference(_)) {
                    continue;
                }
                let at = pair * 2 + 1;
                path[parent.len()] = NameStep::Index(at);
                let location = &path[..=parent.len()];
                let token = if original
                    .is_some_and(|a| a.get(at - 1) == Some(key) && a.get(at) == Some(item))
                {
                    previous.token(location)
                } else {
                    fresh()?
                };
                if token != previous.default {
                    Self::insert_rule(&mut rules, location, None, token, budget)?;
                }
            }
            Ok(())
        })?;
        Self::with_rules(previous.default, rules, budget)
    }
}

fn fresh() -> Result<u64> {
    NEXT_SLOT
        .try_update(Ordering::Relaxed, Ordering::Relaxed, |n| n.checked_add(1))
        .map_err(|_| Error::malformed("name-tree slot identities exhausted"))
}

impl Version {
    /// Returns the incarnation of the direct name-tree entry at this path.
    #[must_use]
    pub fn name_slot(&self, number: u32, path: &[NameStep]) -> u64 {
        self.name_slots(number).map_or(0, |s| s.token(path))
    }

    fn name_slots(&self, number: u32) -> Option<&NameSlots> {
        match self.slot(number) {
            Some(Slot::Written(object)) => Some(&object.name_slots),
            _ => None,
        }
    }
}

fn at_path<'a>(mut value: &'a Value, path: &[NameStep]) -> Option<&'a Value> {
    for step in path {
        value = match (value, step) {
            (Value::Dict(dict), NameStep::Names) => dict.get("Names")?,
            (Value::Dict(dict), NameStep::EmbeddedFiles) => dict.get("EmbeddedFiles")?,
            (Value::Dict(dict), NameStep::Kids) => dict.get("Kids")?,
            (Value::Array(array), NameStep::Index(i)) => array.get(*i)?,
            _ => return None,
        };
    }
    Some(value)
}

fn visit(
    value: &Value,
    path: &mut [NameStep; MAX_NESTING + 1],
    depth: usize,
    budget: &Budget,
    slot: &mut impl FnMut(&[NameStep], &crate::value::Array) -> Result<()>,
) -> Result<()> {
    if depth >= MAX_NESTING {
        return Ok(()); // Deeper input cannot be opened by the store.
    }
    match value {
        Value::Dict(dict) => {
            for (field, step) in [
                ("Names", NameStep::Names),
                ("EmbeddedFiles", NameStep::EmbeddedFiles),
                ("Kids", NameStep::Kids),
            ] {
                if let Some(child @ (Value::Dict(_) | Value::Array(_))) = dict.get(field) {
                    path[depth] = step;
                    visit(child, path, depth + 1, budget, slot)?;
                }
            }
        }
        Value::Array(array) => {
            let parent = depth.checked_sub(1).map(|i| path[i]);
            if parent == Some(NameStep::Names) {
                if array.as_slice().as_chunks::<2>().0.iter().any(|[k, v]| {
                    matches!(k, Value::String(_)) && !matches!(v, Value::Reference(_))
                }) {
                    slot(&path[..depth], array)?;
                }
                return Ok(());
            }
            for (at, item) in array.iter().enumerate() {
                if at % 1024 == 0 {
                    budget.poll()?;
                }
                path[depth] = NameStep::Index(at);
                if matches!(item, Value::Dict(_)) {
                    visit(item, path, depth + 1, budget, slot)?;
                }
            }
        }
        _ => {}
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::value::{Array, PdfString};

    #[test]
    fn repeated_slot_writes_and_array_shifts_keep_only_surviving_rules() {
        let budget = Budget::default();
        let array = Value::Array(Array::from(vec![
            Value::String(PdfString::literal(
                b"entry".to_vec()
            ));
            1000
        ]));
        let key = |i| [NameStep::Index(i)];
        let mut slots = NameSlots::default();
        for _ in 0..20 {
            slots = slots.edited(&array, &key(3), None, &budget).unwrap();
            assert_eq!(slots.changes.as_ref().unwrap().rules.len(), 1);
        }
        let first = slots.token(&key(3));
        slots = slots.edited(&array, &[], Some(20), &budget).unwrap();
        let shifted = slots.token(&key(21));
        assert_eq!(slots.token(&key(3)), first);
        assert_ne!(shifted, 0);
        slots = slots.edited(&array, &key(21), None, &budget).unwrap();
        assert_ne!(slots.token(&key(21)), shifted);
        assert_eq!(slots.token(&key(23)), shifted);
        slots = slots.edited(&array, &[], Some(2), &budget).unwrap();
        assert_eq!(slots.changes.as_ref().unwrap().rules.len(), 1);
        assert_eq!(slots.token(&key(1)), 0);
        assert_ne!(slots.token(&key(3)), first);
        let same = slots
            .edited(
                &array,
                &[NameStep::Index(3), NameStep::Other],
                None,
                &budget,
            )
            .unwrap();
        assert_eq!(same.shared_size(&slots), slots.size());
    }

    #[test]
    fn unrelated_numeric_array_assignment_allocates_no_name_records() {
        let values = Value::Array(Array::from(vec![Value::Integer(1); 1000]));
        let slots = NameSlots::default()
            .edited(&values, &[NameStep::Index(999)], None, &Budget::default())
            .unwrap();
        assert_eq!(slots.size(), 0);
    }
}
