//! A reverse index for independently computed, recorded read sets.

use std::sync::Arc;

use crate::Result;
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::version::table::Table;

use super::ReadSet;

/// Maps the objects read by individually keyed results back to those keys.
///
/// The caller compares the versions and recomputes every returned result;
/// this index does not decide whether a result is reusable by itself.
/// Updates copy only paths for the changed associations and are atomic if
/// the operation budget refuses them. Deleted associations are pruned so
/// old dependencies do not accumulate over repeated edits.
#[derive(Clone, Default)]
pub struct ReadDependencies {
    objects: Table<Table<()>>,
    trailer: Table<()>,
    reads: Table<Arc<ReadSet>>,
}

impl ReadDependencies {
    /// Returns an empty index.
    #[must_use]
    pub const fn new() -> Self {
        Self {
            objects: Table::new(),
            trailer: Table::new(),
            reads: Table::new(),
        }
    }

    /// Replaces the recorded reads of `key`, or removes them with `None`.
    /// On refusal the index is unchanged.
    pub fn replace(&mut self, key: u32, reads: Option<ReadSet>, budget: &Budget) -> Result<()> {
        budget.poll()?;
        let old = self.reads.get(key).cloned();
        let mut objects = self.objects.clone();
        let mut trailer = self.trailer.clone();
        let mut recorded = self.reads.clone();

        if let Some(old) = &old {
            for &number in old.objects() {
                budget.poll()?;
                if reads
                    .as_ref()
                    .is_none_or(|new| new.objects().binary_search(&number).is_err())
                {
                    Self::unlink(&mut objects, number, key, budget)?;
                }
            }
            if old.trailer() && reads.as_ref().is_none_or(|new| !new.trailer()) {
                trailer.remove(key, budget)?;
            }
        }
        if let Some(new) = &reads {
            for &number in new.objects() {
                budget.poll()?;
                if old
                    .as_ref()
                    .is_none_or(|old| old.objects().binary_search(&number).is_err())
                {
                    let mut keys = objects.get(number).cloned().unwrap_or_default();
                    keys.insert(key, (), budget)?;
                    objects.insert(number, keys, budget)?;
                }
            }
            if new.trailer() && old.as_ref().is_none_or(|old| !old.trailer()) {
                trailer.insert(key, (), budget)?;
            }
        }
        if let Some(reads) = reads {
            budget.charge(
                LimitCode::MemorySize,
                (size_of::<ReadSet>() + 2 * size_of::<usize>()) as u64,
            )?;
            recorded.insert(key, Arc::new(reads), budget)?;
        } else {
            recorded.remove(key, budget)?;
        }
        budget.poll()?;
        self.objects = objects;
        self.trailer = trailer;
        self.reads = recorded;
        Ok(())
    }

    fn unlink(
        objects: &mut Table<Table<()>>,
        number: u32,
        key: u32,
        budget: &Budget,
    ) -> Result<()> {
        if let Some(mut keys) = objects.get(number).cloned() {
            keys.remove(key, budget)?;
            if keys.iter().next().is_none() {
                objects.remove(number, budget)?;
            } else {
                objects.insert(number, keys, budget)?;
            }
        }
        Ok(())
    }

    /// Returns the distinct keys whose reads include a changed object or
    /// trailer, sorted. Duplicate changed numbers do not duplicate keys.
    pub fn affected<'b>(
        &self,
        changed: &[u32],
        trailer_changed: bool,
        budget: &'b Budget,
    ) -> Result<BudgetVec<'b, u32>> {
        let mut keys = BudgetVec::new(budget);
        for &number in changed {
            budget.poll()?;
            if let Some(dependents) = self.objects.get(number) {
                for (key, ()) in dependents.iter() {
                    budget.poll()?;
                    keys.try_push(key)?;
                }
            }
        }
        if trailer_changed {
            for (key, ()) in self.trailer.iter() {
                budget.poll()?;
                keys.try_push(key)?;
            }
        }
        keys.sort_unstable();
        let mut kept = 0;
        for at in 0..keys.len() {
            if kept == 0 || keys[kept - 1] != keys[at] {
                keys[kept] = keys[at];
                kept += 1;
            }
        }
        keys.truncate(kept);
        budget.poll()?;
        Ok(keys)
    }
}

#[cfg(test)]
mod tests {
    use std::collections::{BTreeMap, BTreeSet};

    use proptest::prelude::*;

    use super::*;

    fn reads(objects: &[u32], trailer: bool) -> ReadSet {
        let mut objects = objects.to_vec();
        objects.sort_unstable();
        objects.dedup();
        ReadSet {
            objects: objects.into_boxed_slice(),
            trailer,
        }
    }

    proptest! {
        #[test]
        fn updated_dependencies_match_an_independent_map(
            updates in proptest::collection::vec((0..40_u32, proptest::collection::vec(0..40_u32, 0..8), any::<bool>(), any::<bool>()), 0..100),
            changed in proptest::collection::vec(0..40_u32, 0..20),
            trailer in any::<bool>(),
        ) {
            let budget = Budget::default();
            let mut index = ReadDependencies::new();
            let mut model = BTreeMap::new();
            for (key, objects, uses_trailer, remove) in updates {
                let previous = index.clone();
                let before = index.affected(&changed, trailer, &budget).unwrap().into_vec();
                if remove {
                    index.replace(key, None, &budget).unwrap();
                    model.remove(&key);
                } else {
                    index.replace(key, Some(reads(&objects, uses_trailer)), &budget).unwrap();
                    model.insert(key, (objects, uses_trailer));
                }
                prop_assert_eq!(previous.affected(&changed, trailer, &budget).unwrap().into_vec(), before);
            }
            let expected: BTreeSet<u32> = model.iter().filter_map(|(&key, (objects, uses_trailer))| {
                ((trailer && *uses_trailer) || objects.iter().any(|number| changed.contains(number))).then_some(key)
            }).collect();
            prop_assert_eq!(index.affected(&changed, trailer, &budget).unwrap().into_vec(), expected.into_iter().collect::<Vec<_>>());
        }
    }

    #[test]
    fn refused_replacement_keeps_the_old_dependencies() {
        let budget = Budget::default();
        let mut index = ReadDependencies::new();
        index
            .replace(3, Some(reads(&[7, 9], true)), &budget)
            .unwrap();
        let limited = Budget::default().with_limit(LimitCode::MemorySize, Some(0));
        assert!(
            index
                .replace(3, Some(reads(&[8], false)), &limited)
                .is_err()
        );
        assert_eq!(
            index
                .affected(&[7, 8, 9], true, &budget)
                .unwrap()
                .into_vec(),
            vec![3]
        );
        assert_eq!(
            index.affected(&[8], false, &budget).unwrap().into_vec(),
            Vec::<u32>::new()
        );
        index.replace(3, None, &budget).unwrap();
        assert!(index.affected(&[7, 9], true, &budget).unwrap().is_empty());
        assert!(index.objects.iter().next().is_none());
    }

    #[test]
    fn removing_one_shared_dependency_does_not_copy_its_other_readers() {
        let budget = Budget::default();
        let mut index = ReadDependencies::new();
        for key in 0..4_000 {
            index
                .replace(key, Some(reads(&[99, key + 10_000], false)), &budget)
                .unwrap();
        }
        let limited = Budget::default().with_limit(LimitCode::MemorySize, Some(64 * 1024));
        index
            .replace(7, Some(reads(&[10_007], false)), &limited)
            .unwrap();
        let found = index.affected(&[99], false, &budget).unwrap();
        assert_eq!(found.len(), 3_999);
        assert!(!found.contains(&7));
    }

    #[test]
    fn partial_update_refusal_and_cancellation_keep_the_index_unchanged() {
        let budget = Budget::default();
        let mut index = ReadDependencies::new();
        let objects: Vec<u32> = (0..100).map(|number| number * 1024).collect();
        index
            .replace(8, Some(reads(&objects, true)), &budget)
            .unwrap();
        let limited = Budget::default().with_limit(LimitCode::MemorySize, Some(8 * 1024));
        assert!(
            index
                .replace(8, Some(reads(&[200_000], false)), &limited)
                .is_err()
        );
        assert!(limited.used(LimitCode::MemorySize) > 0);
        assert_eq!(
            index.affected(&objects, true, &budget).unwrap().into_vec(),
            vec![8]
        );
        assert!(
            index
                .affected(&[200_000], false, &budget)
                .unwrap()
                .is_empty()
        );

        let token = crate::budget::CancellationToken::new();
        token.cancel();
        let cancelled = Budget::default().with_cancellation(token);
        assert!(index.replace(8, None, &cancelled).is_err());
        assert_eq!(
            index.affected(&objects, true, &budget).unwrap().into_vec(),
            vec![8]
        );
    }
}
