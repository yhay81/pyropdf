//! What a version holds for some object numbers, remembered without keeping
//! the version (DESIGN.md F4 and section 10).
//!
//! A derived result that depends on a few objects, such as a placed page's
//! Form, records their [`ObjectStamps`] and is still valid on a later
//! version while every one of them holds the same slot: the same written
//! object, the same free entry, or the base's object. That is the identity
//! that [`Version::changes`] compares, so a write of an object changes its
//! stamp even when it writes the same bytes. Stamps hold weak references:
//! they keep neither the version nor the bytes of its objects alive.

use std::sync::{Arc, Weak};

use super::base::Origin;
use super::{Slot, Version, Written};
use crate::LimitExceeded;
use crate::budget::{Budget, BudgetVec};

/// How often a check polls the budget, in objects.
const POLL_INTERVAL: usize = 1024;

/// What a version holds for one object number.
#[derive(Clone, Debug)]
enum Held {
    /// The base's object, or nothing: no transaction wrote the number.
    Base,
    /// An object that a transaction wrote.
    Written(Weak<Written>),
    /// A free entry with its next generation.
    Free(u16),
}

impl Held {
    fn of(version: &Version, number: u32) -> Self {
        match version.slot(number) {
            None => Self::Base,
            Some(Slot::Written(object)) => Self::Written(Arc::downgrade(object)),
            Some(Slot::Free(generation)) => Self::Free(*generation),
        }
    }

    fn holds(&self, version: &Version, number: u32) -> bool {
        match (self, version.slot(number)) {
            (Self::Base, None) => true,
            (Self::Written(held), Some(Slot::Written(object))) => {
                std::ptr::eq(held.as_ptr(), Arc::as_ptr(object))
            }
            (Self::Free(held), Some(Slot::Free(generation))) => held == generation,
            _ => false,
        }
    }
}

/// The stamps of some object numbers of one document's version.
#[derive(Clone, Debug)]
pub struct ObjectStamps {
    origin: Weak<Origin>,
    stamps: Box<[(u32, Held)]>,
}

impl Version {
    /// Returns the stamps of `numbers` in this version. Refuses with
    /// `memory_size` when the budget does not allow them.
    pub fn stamps(&self, numbers: &[u32], budget: &Budget) -> Result<ObjectStamps, LimitExceeded> {
        budget.poll()?;
        let mut stamps = BudgetVec::new(budget);
        stamps.try_reserve_exact(numbers.len())?;
        for &number in numbers {
            stamps.try_push((number, Held::of(self, number)))?;
        }
        Ok(ObjectStamps {
            origin: Arc::downgrade(&self.origin),
            stamps: stamps.into_vec().into_boxed_slice(),
        })
    }
}

impl ObjectStamps {
    /// Returns whether `version`, of the same document, holds what the
    /// stamped version held for every stamped number. A version of another
    /// document, such as one that signing replaced, never does.
    pub fn unchanged(&self, version: &Version, budget: &Budget) -> Result<bool, LimitExceeded> {
        budget.poll()?;
        if !std::ptr::eq(self.origin.as_ptr(), Arc::as_ptr(&version.origin)) {
            return Ok(false);
        }
        for (at, (number, held)) in self.stamps.iter().enumerate() {
            if at % POLL_INTERVAL == POLL_INTERVAL - 1 {
                budget.poll()?;
            }
            if !held.holds(version, *number) {
                return Ok(false);
            }
        }
        Ok(true)
    }

    /// Returns how many numbers are stamped.
    #[must_use]
    pub fn len(&self) -> usize {
        self.stamps.len()
    }

    /// Returns whether no number is stamped.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.stamps.is_empty()
    }

    /// Returns the bytes that the stamps hold, for a cache that bounds what
    /// it keeps.
    #[must_use]
    pub fn size(&self) -> u64 {
        (size_of::<Self>() + self.stamps.len() * size_of::<(u32, Held)>()) as u64
    }
}
