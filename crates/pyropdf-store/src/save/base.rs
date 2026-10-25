//! The original file index as the base of immutable versions.

use crate::Result;
use crate::budget::{Budget, BudgetVec};
use crate::index::{Index, Listing};
use crate::value::{Bytes, Dict, Reference, Value};
use crate::version::Base;

/// An indexed file shared by every version of an opened document.
/// Opening parses the index, not all the objects. Object streams remain
/// lazy and share the index's bounded decode cache.
#[derive(Debug)]
pub struct FileBase {
    index: Index,
    next: u64,
}

impl FileBase {
    /// Opens `bytes` and retains them without a second copy.
    pub fn open(bytes: Bytes, budget: &Budget) -> Result<Self> {
        Self::new(Index::open(bytes, budget)?, budget)
    }

    /// Connects an already-open index to versions. The high-water mark
    /// includes free entries and `/Size`, so allocation cannot reuse an
    /// object number that the original file reserved.
    pub fn new(index: Index, budget: &Budget) -> Result<Self> {
        let mut next = 1;
        for (number, _) in index.entries() {
            budget.poll()?;
            next = next.max(u64::from(number) + 1);
        }
        for section in index.revisions().iter().flat_map(|r| r.sections()) {
            budget.poll()?;
            if let Some(&(number, _)) = section.listings().last() {
                next = next.max(u64::from(number) + 1);
            }
        }
        if let Some(&Value::Integer(size)) = index.trailer().get("Size") {
            next = next.max(
                u64::try_from(size)
                    .unwrap_or(0)
                    .min(u64::from(u32::MAX) + 1),
            );
        }
        Ok(Self { index, next })
    }

    /// Returns the original index, including its repair report.
    #[must_use]
    pub const fn index(&self) -> &Index {
        &self.index
    }
}

impl Base for FileBase {
    fn generation(&self, number: u32) -> Option<u16> {
        self.index.get(number).map(|entry| entry.generation())
    }

    fn free_generation(&self, number: u32) -> Option<u16> {
        if self.generation(number).is_some() {
            return None;
        }
        for section in self
            .index
            .revisions()
            .iter()
            .rev()
            .flat_map(|r| r.sections())
        {
            if let Ok(at) = section
                .listings()
                .binary_search_by_key(&number, |&(n, _)| n)
            {
                return match section.listings()[at].1 {
                    Listing::Free { generation } => Some(generation),
                    Listing::InUse(_) => None,
                };
            }
        }
        None
    }

    fn object(&self, reference: Reference, budget: &Budget) -> Result<Option<Bytes>> {
        self.index.object(reference, budget)
    }

    fn trailer(&self) -> &Dict {
        self.index.trailer()
    }

    fn next_number(&self) -> u64 {
        self.next
    }

    fn numbers(&self, out: &mut BudgetVec<'_, u32>) -> Result<()> {
        for (number, _) in self.index.entries() {
            out.budget().poll()?;
            out.try_push(number)?;
        }
        for section in self.index.revisions().iter().flat_map(|r| r.sections()) {
            for &(number, _) in section.listings() {
                out.budget().poll()?;
                out.try_push(number)?;
            }
        }
        Ok(())
    }

    fn file(&self) -> Option<&Index> {
        Some(&self.index)
    }
}
