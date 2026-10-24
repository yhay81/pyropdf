//! Bytes that values share with the data they were read from.

use std::fmt;
use std::ops::{Deref, Range};
use std::sync::Arc;

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

impl Bytes {
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
