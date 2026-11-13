//! A recount is reusable only while every object it read is unchanged.

use std::sync::{Arc, Mutex, PoisonError, Weak};

use pyropdf_store::budget::Budget;
use pyropdf_store::memo::ReadSet;
use pyropdf_store::version::{Transaction, Version};

use super::Result;

pub(super) struct Verified {
    pub(super) reads: ReadSet,
    pub(super) members: Vec<(u32, u16)>,
}

struct Entry {
    version: Weak<Version>,
    verified: Arc<Verified>,
}

/// Verified outline counts, shared across edits without retaining old versions.
/// Raw writes, rollback, and history movement are checked against the recount's
/// recorded reads before the parent-chain shortcut can be used again.
#[derive(Default)]
pub struct CountCache {
    entry: Mutex<Option<Entry>>,
}

impl CountCache {
    /// Forgets the verified outline of a replaced or closed document.
    pub fn clear(&self) {
        *self.entry.lock().unwrap_or_else(PoisonError::into_inner) = None;
    }

    /// Shows or hides an item's children, recounting unverified outlines first.
    pub fn set_open(
        &self,
        transaction: &mut Transaction,
        budget: &Budget,
        id: (u32, u16),
        open: bool,
    ) -> Result<()> {
        super::set_open(self, transaction, budget, id, open)
    }

    pub(super) fn get(
        &self,
        version: &Arc<Version>,
        budget: &Budget,
    ) -> Result<Option<Arc<Verified>>> {
        budget.poll()?;
        let (old, verified) = {
            let entry = self.entry.lock().unwrap_or_else(PoisonError::into_inner);
            let Some(entry) = entry.as_ref() else {
                return Ok(None);
            };
            let Some(old) = entry.version.upgrade() else {
                return Ok(None);
            };
            (old, Arc::clone(&entry.verified))
        };
        if Arc::ptr_eq(&old, version)
            || old
                .changes(version, budget)?
                .is_some_and(|changes| verified.reads.is_disjoint(&changes))
        {
            Ok(Some(verified))
        } else {
            Ok(None)
        }
    }

    pub(super) fn put(&self, version: &Arc<Version>, verified: Arc<Verified>) {
        *self.entry.lock().unwrap_or_else(PoisonError::into_inner) = Some(Entry {
            version: Arc::downgrade(version),
            verified,
        });
    }

    /// Carries a verified draft to its committed version while the caller still
    /// holds the transaction's derived anchor. A refused comparison only loses
    /// the optimization; it cannot make an already published commit fail.
    pub fn carry_to(&self, version: &Arc<Version>, budget: &Budget) {
        if let Ok(Some(verified)) = self.get(version, budget) {
            self.put(version, verified);
        }
    }
}
