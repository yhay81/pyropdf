//! Persistent stale marks: the same budgeted chunk table as object slots.
//!
//! Each structure owns one table. Versions and savepoints share its nodes;
//! marking copies only the path to that object. Settling clears a whole
//! structure after collecting its numbers into a separately charged buffer.

use std::fmt;

use super::table::{Table, Unshared};
use crate::budget::{Budget, BudgetVec};
use crate::settle::{Mark, Scope, Structure};
use crate::{LimitExceeded, Result};

#[derive(Clone, Default)]
pub(crate) struct Stale([Table<()>; Structure::ALL.len()]);

impl Stale {
    #[cfg(test)]
    pub(crate) const FIRST_MARK_SIZE: u64 = Table::<()>::BRANCH_BYTES + Table::<()>::CHUNK_BYTES;

    pub(crate) fn insert(&mut self, mark: Mark, budget: &Budget) -> Result<()> {
        budget.poll()?;
        let table = &mut self.0[mark.structure.index()];
        if table.get(mark.number).is_none() {
            table.insert(mark.number, (), budget)?;
        }
        Ok(())
    }

    pub(crate) fn contains(&self, mark: Mark) -> bool {
        self.0[mark.structure.index()].get(mark.number).is_some()
    }

    pub(crate) fn any(&self, scope: Scope) -> bool {
        Structure::ALL
            .into_iter()
            .any(|kind| scope.covers(kind) && self.has(kind))
    }

    pub(crate) fn has(&self, structure: Structure) -> bool {
        self.0[structure.index()].iter().next().is_some()
    }

    pub(crate) fn take<'b>(
        &mut self,
        structure: Structure,
        budget: &'b Budget,
    ) -> Result<BudgetVec<'b, u32>> {
        let table = &mut self.0[structure.index()];
        let mut numbers = BudgetVec::new(budget);
        for (number, ()) in table.iter() {
            if numbers.len().is_multiple_of(super::POLL_INTERVAL as usize) {
                budget.poll()?;
            }
            numbers.try_push(number)?;
        }
        *table = Table::new();
        Ok(numbers)
    }

    pub(crate) fn marks(&self) -> impl Iterator<Item = Mark> + '_ {
        Structure::ALL.into_iter().flat_map(|structure| {
            self.0[structure.index()]
                .iter()
                .map(move |(number, ())| Mark { structure, number })
        })
    }

    pub(super) fn diff(
        &self,
        other: &Self,
        budget: &Budget,
    ) -> std::result::Result<(bool, Unshared), LimitExceeded> {
        let mut changed = false;
        let mut bytes = Unshared::default();
        for (a, b) in self.0.iter().zip(&other.0) {
            budget.poll()?;
            let unshared = a.diff(
                b,
                |(), ()| true,
                |_, _, _| {
                    changed = true;
                    budget.poll()
                },
            )?;
            bytes.first += unshared.first;
            bytes.second += unshared.second;
        }
        Ok((changed, bytes))
    }
}

impl fmt::Debug for Stale {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.debug_list().entries(self.marks()).finish()
    }
}
