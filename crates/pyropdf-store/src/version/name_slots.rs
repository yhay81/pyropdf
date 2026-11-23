//! Incarnations of direct values in name-tree pairs. Replacements and array
//! shifts cover only surviving locations. Undo reuses the same written root.

use std::cmp::Ordering as Cmp;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use super::{Slot, Version, Written};
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

/// Relation to the previous written root. Most mutations need only the tag.
pub(super) enum SlotJournal<'b> {
    Keep,
    Replace,
    Mixed(BudgetVec<'b, u8>),
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
        // Only inspect the assigned subtree, or the suffix moved by this
        // insertion/removal. Ordinary scalar appends take constant work here.
        // Mutating an even key index in a /Names array replaces the identity of
        // its paired direct value slot (`at + 1`), matching NameEdit::Rebuild.
        let mut paired_path = [NameStep::Other; MAX_NESTING + 1];
        let mut paired_len = None;
        if suffix.is_none()
            && path.len() <= MAX_NESTING + 1
            && let Some((&NameStep::Index(at), parent)) = path.split_last()
            && at.is_multiple_of(2)
            && (parent.is_empty() || parent.last() == Some(&NameStep::Names))
            && let Some(Value::Array(array)) = at_path(value, parent)
            && array.get(at + 1).is_some_and(|item| {
                !matches!(item, Value::Reference(_))
                    && (pair_key(array.get(at)) || matches!(item, Value::Dict(_)))
            })
        {
            paired_path[..parent.len()].copy_from_slice(parent);
            paired_path[parent.len()] = NameStep::Index(at + 1);
            paired_len = Some(parent.len() + 1);
        }
        let effective_path = paired_len.map_or(path, |len| &paired_path[..len]);
        let keep = paired_len.is_some() || affected_slot(value, path, suffix, budget)?;
        if !keep {
            let Some(changes) = &self.changes else {
                return Ok(self.clone());
            };
            let start = changes.rules.partition_point(|r| r.path.as_ref() < path);
            let mut removed = false;
            for rule in &changes.rules[start..] {
                if !rule.path.starts_with(path) {
                    break;
                }
                budget.poll()?;
                if rule.covered(path, suffix) {
                    removed = true;
                    break;
                }
            }
            if !removed {
                return Ok(self.clone());
            }
        }
        let mut rules = BudgetVec::new(budget);
        if let Some(changes) = &self.changes {
            for (index, rule) in changes.rules.iter().enumerate() {
                if index.is_multiple_of(1024) {
                    budget.poll()?;
                }
                if !rule.covered(effective_path, suffix) {
                    Self::insert_rule(&mut rules, &rule.path, rule.suffix, rule.token, budget)?;
                }
            }
        }
        if keep {
            Self::insert_rule(&mut rules, effective_path, suffix, fresh()?, budget)?;
        }
        Self::with_rules(self.default, rules, budget)
    }

    /// Only a native dictionary rewrite may rebalance a name tree. All other
    /// mutations inspect only the assigned subtree or affected array suffix.
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
        visit_slots(
            value,
            &mut path,
            0,
            0,
            budget,
            &mut |location, key, item| {
                let before = match &old {
                    Some(v) => v,
                    None => old.insert(version.get(number, budget)?.map(|o| Arc::clone(o.value()))),
                };
                let Some((&NameStep::Index(at), parent)) = location.split_last() else {
                    unreachable!("a name slot is an array item");
                };
                let same = matches!(before.as_deref().and_then(|v| at_path(v, parent)),
                Some(Value::Array(a)) if a.get(at - 1) == Some(key) && a.get(at) == Some(item));
                let token = if same {
                    previous.token(location)
                } else {
                    fresh()?
                };
                if token != previous.default {
                    Self::insert_rule(&mut rules, location, None, token, budget)?;
                }
                Ok(false)
            },
        )?;
        Self::with_rules(previous.default, rules, budget)
    }

    /// One byte per direct slot in traversal order: 0 keeps the previous
    /// incarnation, 1 starts a new one. Process-local tokens never escape.
    pub(super) fn journal<'b>(
        &self,
        before: &Version,
        number: u32,
        object: &Written,
        budget: &'b Budget,
    ) -> Result<SlotJournal<'b>> {
        if !object.name_tree_candidate || before.generation(number).is_none() {
            return Ok(SlotJournal::Replace);
        }
        let previous = Self::inherited(before, number);
        let same = match (&previous.changes, &self.changes) {
            (None, None) if previous.default != self.default => return Ok(SlotJournal::Replace),
            (None, None) => true,
            (Some(a), Some(b)) => previous.default == self.default && Arc::ptr_eq(a, b),
            _ => false,
        };
        let value = object.value(budget)?;
        let mut path = [NameStep::Other; MAX_NESTING + 1];
        if same {
            // Format one is sufficient when the owner has no direct entry.
            let contains_slot =
                visit_slots(&value, &mut path, 0, 0, budget, &mut |_, _, _| Ok(true))?;
            return Ok(if contains_slot {
                SlotJournal::Keep
            } else {
                SlotJournal::Replace
            });
        }
        let mut flags = BudgetVec::new(budget);
        let mut replaced = 0;
        visit_slots(&value, &mut path, 0, 0, budget, &mut |location, _, _| {
            let changed = self.token(location) != previous.token(location);
            replaced += usize::from(changed);
            flags.try_push(u8::from(changed))?;
            Ok(false)
        })?;
        Ok(if replaced == flags.len() {
            SlotJournal::Replace
        } else if replaced == 0 {
            SlotJournal::Keep
        } else {
            SlotJournal::Mixed(flags)
        })
    }

    pub(super) fn inherited(before: &Version, number: u32) -> Self {
        before.name_slots(number).cloned().unwrap_or_default()
    }

    pub(super) fn restored(
        before: &Version,
        number: u32,
        value: &Value,
        flags: &[u8],
        budget: &Budget,
    ) -> Result<Self> {
        let invalid =
            || Error::malformed("the data is not a saved history: invalid name-slot flags");
        if flags.is_empty() {
            return Err(invalid());
        }
        let previous = before.name_slots(number).cloned().unwrap_or_default();
        let mut rules = BudgetVec::new(budget);
        let mut path = [NameStep::Other; MAX_NESTING + 1];
        let mut remaining = flags.iter();
        let replacement = fresh()?;
        let mut replaced = 0;
        visit_slots(value, &mut path, 0, 0, budget, &mut |location, _, _| {
            let token = match remaining.next() {
                Some(0) => previous.token(location),
                Some(1) => {
                    replaced += 1;
                    replacement
                }
                _ => return Err(invalid()),
            };
            if token != previous.default {
                Self::insert_rule(&mut rules, location, None, token, budget)?;
            }
            Ok(false)
        })?;
        if remaining.next().is_some() || replaced == 0 || replaced == flags.len() {
            return Err(invalid());
        }
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

fn pair_key(value: Option<&Value>) -> bool {
    matches!(value, Some(Value::String(_) | Value::Reference(_)))
}

fn affected_slot(
    root: &Value,
    path: &[NameStep],
    suffix: Option<usize>,
    budget: &Budget,
) -> Result<bool> {
    let Some(value) = at_path(root, path) else {
        return Ok(false);
    };
    if suffix.is_none()
        && let Some((&NameStep::Index(at), parent)) = path.split_last()
        && at % 2 == 1
        && (parent.is_empty() || parent.last() == Some(&NameStep::Names))
        && !matches!(value, Value::Reference(_))
        && matches!(at_path(root, parent), Some(Value::Array(a)) if pair_key(a.get(at - 1)))
    {
        return Ok(true);
    }
    if path.len() >= MAX_NESTING {
        return Ok(false);
    }
    let mut scratch = [NameStep::Other; MAX_NESTING + 1];
    scratch[..path.len()].copy_from_slice(path);
    visit_slots(
        value,
        &mut scratch,
        path.len(),
        suffix.unwrap_or(0),
        budget,
        &mut |_, _, _| Ok(true),
    )
}

/// Visit direct entries only. An indirect array root may hold either name
/// pairs or direct child dictionaries; a /Kids array can only hold children.
/// A visitor returning true stops at the first matching slot.
fn visit_slots(
    value: &Value,
    path: &mut [NameStep; MAX_NESTING + 1],
    depth: usize,
    first: usize,
    budget: &Budget,
    slot: &mut impl FnMut(&[NameStep], &Value, &Value) -> Result<bool>,
) -> Result<bool> {
    if depth >= MAX_NESTING {
        return Ok(false);
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
                    if visit_slots(child, path, depth + 1, 0, budget, slot)? {
                        return Ok(true);
                    }
                }
            }
        }
        Value::Array(array) => {
            let parent = depth.checked_sub(1).map(|i| path[i]);
            let pairs = parent.is_none() || parent == Some(NameStep::Names);
            let kids = parent.is_none() || parent == Some(NameStep::Kids);
            if !pairs && !kids {
                return Ok(false);
            }
            for (at, item) in array.iter().enumerate().skip(first) {
                if (at - first).is_multiple_of(1024) {
                    budget.poll()?;
                }
                path[depth] = NameStep::Index(at);
                if pairs
                    && at % 2 == 1
                    && !matches!(item, Value::Reference(_))
                    && pair_key(array.get(at - 1))
                    && slot(&path[..=depth], &array.as_slice()[at - 1], item)?
                {
                    return Ok(true);
                }
                if kids
                    && matches!(item, Value::Dict(_))
                    && visit_slots(item, path, depth + 1, 0, budget, slot)?
                {
                    return Ok(true);
                }
            }
        }
        _ => {}
    }
    Ok(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::value::{Array, Bytes, PdfString};

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
    fn ordinary_array_writes_allocate_no_identity_records_even_on_a_zero_memory_budget() {
        let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(0));
        let values = Value::Array(Array::from(vec![Value::Integer(1); 1000]));
        let previous = NameSlots::default();
        for (path, first) in [
            (&[NameStep::Index(999)][..], None),
            (&[][..], Some(999)),
            (&[][..], Some(0)),
        ] {
            let slots = previous.edited(&values, path, first, &budget).unwrap();
            assert_eq!(slots.size(), 0);
        }
        let kids = Value::parse(
            &Bytes::from(b"<< /Kids [1 0 R 2 0 R << /Type /Page >>] >>".as_slice()),
            &Budget::default(),
        )
        .unwrap();
        assert_eq!(
            previous
                .edited(&kids, &[NameStep::Kids], Some(0), &budget)
                .unwrap()
                .size(),
            0
        );
        let pairs = Value::parse(
            &Bytes::from(b"[(key) (value)]".as_slice()),
            &Budget::default(),
        )
        .unwrap();
        assert!(previous.edited(&pairs, &[], Some(0), &budget).is_err());
    }

    #[test]
    fn mutating_even_key_index_in_names_array_mints_paired_value_token() {
        let budget = Budget::default();
        let tree = Value::parse(
            &Bytes::from(b"<< /Names [(a) << /F (x) >> (b) << /F (y) >>] >>".as_slice()),
            &budget,
        )
        .unwrap();
        let slot0 = [NameStep::Names, NameStep::Index(1)];
        let slot1 = [NameStep::Names, NameStep::Index(3)];
        let slots = NameSlots::default()
            .edited(&tree, &[NameStep::Names, NameStep::Index(0)], None, &budget)
            .unwrap();
        let first = slots.token(&slot0);
        assert_ne!(first, 0);
        assert_eq!(slots.token(&slot1), 0);

        let slots = slots
            .edited(&tree, &[NameStep::Names, NameStep::Index(0)], None, &budget)
            .unwrap();
        assert_ne!(slots.token(&slot0), first);
        assert_eq!(slots.token(&slot1), 0);
    }
}
