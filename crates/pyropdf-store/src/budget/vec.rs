//! A vector whose growth the budget pays for (DESIGN.md section 9).

use std::fmt;
use std::mem;
use std::ops::{Deref, DerefMut};

use super::{Budget, LimitCode};
use crate::LimitExceeded;

/// A vector whose growth is fallible and charged to a budget: the default
/// for collections that grow from input.
///
/// Before the buffer grows, the bytes of its new capacity are charged to
/// [`LimitCode::MemorySize`], and the allocation is fallible, so neither a
/// limit nor the allocator can abort the process. A vector made by
/// [`BudgetVec::counting`] also charges each element to the code it counts,
/// such as `object_count` for the entries of an index. A refused growth
/// leaves the vector and the budget as they were, and an extension that is
/// refused adds nothing.
///
/// Dropping the vector releases its charges, and removing an element
/// releases the element's count. [`BudgetVec::into_vec`] leaves both with
/// the budget for the rest of the operation. Only the vector's own buffer is
/// charged: elements that own memory charge it through collections of their
/// own.
pub struct BudgetVec<'b, T> {
    budget: &'b Budget,
    items: Vec<T>,
    /// The code that each element counts against, if any.
    counts: Option<LimitCode>,
    /// The bytes of capacity that the vector has charged.
    charged: u64,
}

impl<'b, T> BudgetVec<'b, T> {
    /// Returns an empty vector that charges its buffer to `budget`.
    #[must_use]
    pub const fn new(budget: &'b Budget) -> Self {
        Self {
            budget,
            items: Vec::new(),
            counts: None,
            charged: 0,
        }
    }

    /// Returns an empty vector that also charges each of its elements to
    /// `code`.
    #[must_use]
    pub const fn counting(budget: &'b Budget, code: LimitCode) -> Self {
        Self {
            budget,
            items: Vec::new(),
            counts: Some(code),
            charged: 0,
        }
    }

    /// Returns the budget that the vector charges.
    #[must_use]
    pub const fn budget(&self) -> &'b Budget {
        self.budget
    }

    /// Returns the number of elements.
    #[must_use]
    pub const fn len(&self) -> usize {
        self.items.len()
    }

    /// Returns whether the vector has no elements.
    #[must_use]
    pub const fn is_empty(&self) -> bool {
        self.items.is_empty()
    }

    /// Returns how many elements the buffer holds without growing.
    #[must_use]
    pub const fn capacity(&self) -> usize {
        self.items.capacity()
    }

    /// Returns the elements.
    #[must_use]
    pub const fn as_slice(&self) -> &[T] {
        self.items.as_slice()
    }

    /// Returns the elements, which may change but not their number.
    #[must_use]
    pub const fn as_mut_slice(&mut self) -> &mut [T] {
        self.items.as_mut_slice()
    }

    /// Makes room for at least `additional` more elements. When the buffer
    /// must grow, it at least doubles if the budget allows that, and grows
    /// exactly as much as needed otherwise.
    pub fn try_reserve(&mut self, additional: usize) -> Result<(), LimitExceeded> {
        self.grow(additional, true)
    }

    /// Makes room for exactly `additional` more elements.
    pub fn try_reserve_exact(&mut self, additional: usize) -> Result<(), LimitExceeded> {
        self.grow(additional, false)
    }

    /// Appends `item`, or drops it when the budget refuses.
    pub fn try_push(&mut self, item: T) -> Result<(), LimitExceeded> {
        self.count(1)?;
        if let Err(refusal) = self.grow(1, true) {
            self.uncount(1);
            return Err(refusal);
        }
        #[expect(
            clippy::disallowed_methods,
            reason = "`grow` has made room for the item and charged the budget for it"
        )]
        self.items.push(item);
        Ok(())
    }

    /// Appends clones of `items`: all of them, or none when the budget
    /// refuses.
    pub fn try_extend_from_slice(&mut self, items: &[T]) -> Result<(), LimitExceeded>
    where
        T: Clone,
    {
        self.count(items.len())?;
        if let Err(refusal) = self.grow(items.len(), true) {
            self.uncount(items.len());
            return Err(refusal);
        }
        #[expect(
            clippy::disallowed_methods,
            reason = "`grow` has made room for the items and charged the budget for them"
        )]
        self.items.extend_from_slice(items);
        Ok(())
    }

    /// Appends the items of `iter`: all of them, or none when the budget
    /// refuses one, since the items appended before it are removed again.
    pub fn try_extend(&mut self, iter: impl IntoIterator<Item = T>) -> Result<(), LimitExceeded> {
        let len = self.items.len();
        for item in iter {
            if let Err(refusal) = self.try_push(item) {
                self.truncate(len);
                return Err(refusal);
            }
        }
        Ok(())
    }

    /// Removes the last element and returns it, releasing its count.
    pub fn pop(&mut self) -> Option<T> {
        let item = self.items.pop()?;
        self.uncount(1);
        Some(item)
    }

    /// Keeps the first `len` elements and drops the rest, releasing their
    /// counts. The buffer keeps its capacity and its charge.
    pub fn truncate(&mut self, len: usize) {
        let removed = self.items.len().saturating_sub(len);
        self.items.truncate(len);
        self.uncount(removed);
    }

    /// Drops every element, releasing their counts. The buffer keeps its
    /// capacity and its charge.
    pub fn clear(&mut self) {
        self.truncate(0);
    }

    /// Returns the elements as a plain vector. The charges of its buffer and
    /// its elements stay with the budget for the rest of the operation.
    #[must_use]
    pub fn into_vec(mut self) -> Vec<T> {
        self.charged = 0;
        self.counts = None;
        mem::take(&mut self.items)
    }

    /// Charges `n` elements to the counted code, if there is one.
    fn count(&self, n: usize) -> Result<(), LimitExceeded> {
        match self.counts {
            Some(code) => self.budget.charge(code, to_u64(n)),
            None => Ok(()),
        }
    }

    /// Releases `n` elements from the counted code, if there is one.
    fn uncount(&self, n: usize) {
        if let Some(code) = self.counts {
            self.budget.release(code, to_u64(n));
        }
    }

    /// Grows the buffer to hold `additional` more elements, doubling it when
    /// `amortized` and the budget allow, and charging the new capacity first.
    fn grow(&mut self, additional: usize, amortized: bool) -> Result<(), LimitExceeded> {
        let capacity = self.items.capacity();
        let Some(needed) = self.items.len().checked_add(additional) else {
            return Err(LimitExceeded::new(LimitCode::MemorySize));
        };
        // A zero-sized element type never needs to grow: its capacity is the
        // largest `usize`.
        if needed <= capacity {
            return Ok(());
        }
        let target = if amortized {
            needed
                .max(capacity.saturating_mul(2))
                .max(min_capacity::<T>())
        } else {
            needed
        };
        let (target, extra) = match self.charge_for(target) {
            Ok(extra) => (target, extra),
            // Doubling could make the budget refuse what an exact growth fits.
            Err(_) if target > needed => (needed, self.charge_for(needed)?),
            Err(refusal) => return Err(refusal),
        };
        #[expect(
            clippy::disallowed_methods,
            reason = "the budget has been charged for the new capacity, and a failed reservation releases the charge"
        )]
        let reserved = self.items.try_reserve_exact(target - self.items.len());
        if reserved.is_err() {
            self.budget.release(LimitCode::MemorySize, extra);
            let used = self.budget.used(LimitCode::MemorySize);
            return Err(
                LimitExceeded::new(LimitCode::MemorySize).with_amount(used.saturating_add(extra))
            );
        }
        self.charged = self.charged.saturating_add(extra);
        Ok(())
    }

    /// Charges the budget for a buffer of `capacity` elements, beyond what
    /// the vector has charged, and returns the bytes charged.
    fn charge_for(&self, capacity: usize) -> Result<u64, LimitExceeded> {
        // No allocation holds more than `isize::MAX` bytes.
        let bytes = to_u64(capacity).saturating_mul(to_u64(mem::size_of::<T>()));
        if bytes > to_u64(isize::MAX.unsigned_abs()) {
            return Err(LimitExceeded::new(LimitCode::MemorySize).with_amount(bytes));
        }
        let extra = bytes.saturating_sub(self.charged);
        self.budget.charge(LimitCode::MemorySize, extra)?;
        Ok(extra)
    }
}

/// Returns the smallest capacity that a growing buffer takes, as the
/// standard library chooses it.
const fn min_capacity<T>() -> usize {
    match mem::size_of::<T>() {
        1 => 8,
        size if size <= 1024 => 4,
        _ => 1,
    }
}

/// Converts a count to the budget's unit, saturating where `usize` is wider.
fn to_u64(n: usize) -> u64 {
    u64::try_from(n).unwrap_or(u64::MAX)
}

impl<T> Drop for BudgetVec<'_, T> {
    fn drop(&mut self) {
        self.budget.release(LimitCode::MemorySize, self.charged);
        self.uncount(self.items.len());
    }
}

impl<T> Deref for BudgetVec<'_, T> {
    type Target = [T];

    fn deref(&self) -> &[T] {
        &self.items
    }
}

impl<T> DerefMut for BudgetVec<'_, T> {
    fn deref_mut(&mut self) -> &mut [T] {
        &mut self.items
    }
}

impl<'a, T> IntoIterator for &'a BudgetVec<'_, T> {
    type Item = &'a T;
    type IntoIter = std::slice::Iter<'a, T>;

    fn into_iter(self) -> Self::IntoIter {
        self.items.iter()
    }
}

impl<'a, T> IntoIterator for &'a mut BudgetVec<'_, T> {
    type Item = &'a mut T;
    type IntoIter = std::slice::IterMut<'a, T>;

    fn into_iter(self) -> Self::IntoIter {
        self.items.iter_mut()
    }
}

impl<T: fmt::Debug> fmt::Debug for BudgetVec<'_, T> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.debug_list().entries(&self.items).finish()
    }
}
