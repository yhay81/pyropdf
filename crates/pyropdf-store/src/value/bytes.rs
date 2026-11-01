//! Bytes that values share with the data they were read from.

use std::alloc::{Layout, alloc};
use std::fmt;
use std::mem::MaybeUninit;
use std::ops::{Deref, Range};
use std::ptr::{self, NonNull};
use std::sync::Arc;

use crate::budget::{Budget, LimitCode};
use crate::{LimitExceeded, Result};

/// Immutable bytes that clones share: all of an owner's bytes, or a range of
/// them.
///
/// A value read from bytes keeps its lexeme as `Bytes` over them, and a
/// stream keeps its data so, without a copy. The owner may be anything that
/// holds bytes, such as a file's contents or a decoded object stream, and it
/// must return the same bytes every time.
#[derive(Clone)]
pub struct Bytes {
    owner: Arc<dyn AsRef<[u8]> + Send + Sync>,
    start: usize,
    end: usize,
}

/// Exact-size serialized bytes whose charge is released if publication fails.
///
/// The original output vector remains charged while the copy is made. The
/// allocation has no spare capacity, so history counts all retained bytes.
pub(crate) struct WrittenBytes<'b> {
    bytes: Bytes,
    budget: &'b Budget,
    charged: u64,
}

impl<'b> WrittenBytes<'b> {
    pub(crate) fn copy(source: &[u8], budget: &'b Budget) -> Result<Self> {
        budget.poll()?;
        let charged = u64::try_from(source.len()).unwrap_or(u64::MAX);
        if source.is_empty() {
            return Ok(Self {
                bytes: Bytes::default(),
                budget,
                charged: 0,
            });
        }
        let layout = Layout::array::<u8>(source.len())
            .map_err(|_| LimitExceeded::new(LimitCode::MemorySize))?;
        budget.charge(LimitCode::MemorySize, charged)?;
        // SAFETY: layout is nonzero and valid; null is handled without invoking
        // the infallible allocation handler. The exact layout is handed to Box.
        let Some(pointer) = NonNull::new(unsafe { alloc(layout) }) else {
            budget.release(LimitCode::MemorySize, charged);
            return Err(LimitExceeded::new(LimitCode::MemorySize).into());
        };
        let slice =
            ptr::slice_from_raw_parts_mut(pointer.as_ptr().cast::<MaybeUninit<u8>>(), source.len());
        // SAFETY: the allocation has exactly this layout, and uninitialized
        // bytes are valid MaybeUninit values. Box also cleans up on refusal.
        let mut output = unsafe { Box::from_raw(slice) };
        const CHUNK: usize = 16 * 1024;
        for (index, chunk) in source.chunks(CHUNK).enumerate() {
            if let Err(error) = budget.poll() {
                drop(output);
                budget.release(LimitCode::MemorySize, charged);
                return Err(error.into());
            }
            // SAFETY: the source and fresh allocation do not overlap, and the
            // chunk is wholly within the allocated slice.
            unsafe {
                ptr::copy_nonoverlapping(
                    chunk.as_ptr(),
                    output.as_mut_ptr().add(index * CHUNK).cast::<u8>(),
                    chunk.len(),
                );
            }
            #[cfg(test)]
            test_hook::after_chunk();
        }
        if let Err(error) = budget.poll() {
            drop(output);
            budget.release(LimitCode::MemorySize, charged);
            return Err(error.into());
        }
        // SAFETY: every byte has been initialized by the chunks above.
        let bytes = Bytes::new(unsafe { output.assume_init() });
        Ok(Self {
            bytes,
            budget,
            charged,
        })
    }

    pub(crate) fn bytes(&self) -> &Bytes {
        &self.bytes
    }

    /// Retain the copy's charge for the rest of the successful operation.
    pub(crate) fn published(mut self) {
        self.charged = 0;
    }
}

impl Drop for WrittenBytes<'_> {
    fn drop(&mut self) {
        self.budget.release(LimitCode::MemorySize, self.charged);
    }
}

#[cfg(test)]
mod test_hook {
    use crate::budget::CancellationToken;
    use std::cell::RefCell;
    thread_local! { static CANCEL: RefCell<Option<CancellationToken>> = const { RefCell::new(None) }; }
    pub(super) fn after_chunk() {
        CANCEL.with(|slot| {
            if let Some(token) = slot.borrow_mut().take() {
                token.cancel();
            }
        });
    }
    pub(super) fn cancel_after_chunk(token: CancellationToken) {
        CANCEL.with(|slot| *slot.borrow_mut() = Some(token));
    }
}

impl Bytes {
    #[cfg(test)]
    pub(crate) fn cancel_written_copy_after_chunk(token: crate::budget::CancellationToken) {
        test_hook::cancel_after_chunk(token);
    }

    pub(crate) fn written_copy<'b>(source: &[u8], budget: &'b Budget) -> Result<WrittenBytes<'b>> {
        WrittenBytes::copy(source, budget)
    }

    /// Returns all the bytes of `owner`, which they keep without a copy.
    #[must_use]
    pub fn new(owner: impl AsRef<[u8]> + Send + Sync + 'static) -> Self {
        let end = owner.as_ref().len();
        Self {
            owner: Arc::new(owner),
            start: 0,
            end,
        }
    }

    /// Returns the bytes.
    #[must_use]
    pub fn as_slice(&self) -> &[u8] {
        &AsRef::<[u8]>::as_ref(&*self.owner)[self.start..self.end]
    }

    /// Returns the number of bytes.
    #[must_use]
    pub const fn len(&self) -> usize {
        self.end - self.start
    }

    /// Returns whether there are no bytes.
    #[must_use]
    pub const fn is_empty(&self) -> bool {
        self.start == self.end
    }

    /// Returns the bytes in `range`, counted from the start of these, which
    /// share them, or `None` if the range does not lie within them.
    #[must_use]
    pub fn slice(&self, range: Range<usize>) -> Option<Self> {
        if range.start > range.end || range.end > self.len() {
            return None;
        }
        Some(Self {
            owner: Arc::clone(&self.owner),
            start: self.start + range.start,
            end: self.start + range.end,
        })
    }

    /// Returns where `part` starts in these bytes, if it is a slice of them.
    pub(crate) fn offset_of(&self, part: &[u8]) -> Option<usize> {
        offset_in(self.as_slice(), part)
    }

    /// Returns `part`, a slice of these bytes, as bytes that share them, or
    /// `None` if it is not a slice of them.
    pub(crate) fn share(&self, part: &[u8]) -> Option<Self> {
        let start = self.offset_of(part)?;
        self.slice(start..start + part.len())
    }
}

/// Returns where `part` starts in `whole`, if it is a slice of it.
///
/// hayro-syntax parses objects from a byte slice and borrows from it, so the
/// place of a slice that it returns is the place of what it read.
pub(crate) fn offset_in(whole: &[u8], part: &[u8]) -> Option<usize> {
    let start = part.as_ptr().addr().checked_sub(whole.as_ptr().addr())?;
    (start.checked_add(part.len())? <= whole.len()).then_some(start)
}

impl Default for Bytes {
    /// Returns no bytes.
    fn default() -> Self {
        Self::new(<&[u8]>::default())
    }
}

impl Deref for Bytes {
    type Target = [u8];

    fn deref(&self) -> &[u8] {
        self.as_slice()
    }
}

impl AsRef<[u8]> for Bytes {
    fn as_ref(&self) -> &[u8] {
        self.as_slice()
    }
}

impl From<Vec<u8>> for Bytes {
    fn from(bytes: Vec<u8>) -> Self {
        Self::new(bytes)
    }
}

impl From<&'static [u8]> for Bytes {
    fn from(bytes: &'static [u8]) -> Self {
        Self::new(bytes)
    }
}

impl PartialEq for Bytes {
    /// Bytes are equal when their contents are, whatever owns them.
    fn eq(&self, other: &Self) -> bool {
        self.as_slice() == other.as_slice()
    }
}

impl Eq for Bytes {}

impl fmt::Debug for Bytes {
    /// Writes the number of bytes, not the bytes, which may be a whole file.
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "Bytes({} bytes)", self.len())
    }
}

#[cfg(test)]
mod written_tests {
    use super::*;
    use crate::budget::{BudgetVec, CancellationToken};

    #[test]
    fn exact_copy_charges_both_buffers_and_releases_a_refused_copy() {
        let budget = Budget::default();
        let mut source = BudgetVec::new(&budget);
        source.try_extend_from_slice(&vec![42; 50_001]).unwrap();
        let source_charge = budget.used(LimitCode::MemorySize);
        let copied = Bytes::written_copy(source.as_slice(), &budget).unwrap();
        assert_eq!(copied.bytes().as_slice(), source.as_slice());
        assert_eq!(budget.used(LimitCode::MemorySize), source_charge + 50_001);
        let shared = copied.bytes().clone();
        drop(shared);
        drop(copied);
        assert_eq!(budget.used(LimitCode::MemorySize), source_charge);
        drop(source);
        assert_eq!(budget.used(LimitCode::MemorySize), 0);
    }

    #[test]
    fn cancellation_during_exact_copy_releases_its_allocation() {
        let token = CancellationToken::new();
        let budget = Budget::default().with_cancellation(token.clone());
        test_hook::cancel_after_chunk(token);
        let error = match Bytes::written_copy(&vec![1; 50_001], &budget) {
            Ok(_) => panic!("copy should have been cancelled"),
            Err(error) => error,
        };
        assert_eq!(
            error.limit().map(LimitExceeded::code),
            Some(LimitCode::Cancelled)
        );
        assert_eq!(budget.used(LimitCode::MemorySize), 0);
    }

    #[test]
    fn published_shared_bytes_keep_the_exact_charge_for_the_operation() {
        let budget = Budget::default();
        let source = vec![9; 50_001];
        let copied = Bytes::written_copy(&source, &budget).unwrap();
        let shared = copied.bytes().clone();
        copied.published();
        assert_eq!(budget.used(LimitCode::MemorySize), 50_001);
        assert_eq!(shared.as_slice(), source);
        drop(shared);
        // Successful owned values retain their charge until the operation ends,
        // matching BudgetVec::into_vec, even after a local clone is dropped.
        assert_eq!(budget.used(LimitCode::MemorySize), 50_001);
    }
}
