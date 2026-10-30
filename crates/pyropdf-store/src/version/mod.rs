//! Versions, transactions, change sets, and undo (DESIGN.md sections 4.1
//! and 4.6).
//!
//! A document is a base and a line of versions. A [`Version`] is immutable:
//! it maps each object number that transactions wrote to the object they
//! wrote, kept as serialized bytes, or to a free entry, and it reads every
//! other object from the [`Base`]. Its map is pyropdf's own chunk table
//! (decided in yhay81/pyropdf#9), so versions share structure: a
//! [`Transaction`] that writes one object copies one chunk and the branches
//! above it, at most six with 32-bit object numbers, however many objects
//! the document has, and it charges them to the operation's budget before
//! it allocates them.
//!
//! A [`Document`] holds its current version, which any thread takes and
//! then reads without locks, and a writer lock, which a transaction holds
//! from its start until it commits or is dropped, so that writes serialize.
//! A commit publishes the next version at once. Dropping a transaction, as
//! an error does, publishes nothing, and nothing needs rolling back. Undo
//! and redo publish earlier and later versions again, within a memory limit
//! that the document is given.
//!
//! The [`ChangeSet`] between two versions is what the transactions between
//! them wrote. Saving, invalidation, and reports read it.
//!
//! hayro-syntax parses every object from its bytes, written or in the base
//! ([`Value::parse`], F5), on the object's first read, and every later read
//! of the same object, in any version, shares the value.

mod base;
mod document;
mod journal;
mod stale;
pub(crate) mod table;
mod transaction;

#[cfg(test)]
mod tests;

use std::fmt;
use std::sync::{Arc, OnceLock};

use base::{Origin, VALUE_BYTES};
use table::Table;

use crate::settle::{Mark, Structure};
pub(crate) use stale::Stale;

pub use base::{Base, MemoryBase};
pub use document::{Document, HistoryStatus, RestoredHistory};
pub use transaction::{Savepoint, Transaction};

use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::value::{Bytes, Dict, Value};
use crate::{LimitExceeded, Result};

/// How many numbers a diff visits between two polls of the budget.
const POLL_INTERVAL: u32 = 1024;

/// A version of a document (DESIGN.md section 4.1): the base, what
/// transactions wrote since, the trailer, and the next free object number.
///
/// A version reads each object from what a transaction wrote for its number
/// and from the base otherwise. Versions are immutable, so several threads
/// may read one at once, and a reader keeps the version it took while
/// transactions publish newer ones.
pub struct Version {
    origin: Arc<Origin>,
    /// What transactions wrote, by object number.
    table: Table<Slot>,
    /// The trailer that a transaction wrote, if one did.
    trailer: Option<Arc<Trailer>>,
    /// The number that the next new object takes.
    next: u64,
    /// What the transaction that made the version wrote.
    written: ChangeSet,
    /// The structures that the version leaves stale (DESIGN.md section 7).
    pub(crate) stale: Stale,
}

/// A written trailer, retained as bytes and a parsed dictionary.
struct Trailer {
    value: Dict,
    bytes: Bytes,
}

impl Trailer {
    const OVERHEAD: u64 =
        (size_of::<Self>() + 4 * size_of::<usize>() + size_of::<Box<[u8]>>()) as u64;

    fn size(&self) -> u64 {
        Self::OVERHEAD.saturating_add(self.bytes.len() as u64)
    }
}

/// An object that a version reads: its generation and its value.
#[derive(Clone, Debug, PartialEq)]
pub struct Object {
    generation: u16,
    value: Arc<Value>,
}

/// What a version holds for an object number that a transaction wrote.
#[derive(Clone, Debug)]
pub enum Slot {
    /// An object, kept as the bytes of its value.
    Written(Arc<Written>),
    /// A free entry, with the generation that the number's next use takes. A
    /// number free with generation 65535 is never used again (ISO 32000-2
    /// section 7.5.4).
    Free(u16),
}

/// An object that a transaction wrote: its generation and the bytes of its
/// value, which its first read parses.
pub struct Written {
    generation: u16,
    bytes: Bytes,
    /// The value parsed from the bytes, once a read has parsed it.
    value: OnceLock<Arc<Value>>,
}

/// The change set between two versions (DESIGN.md section 4.1): the object
/// numbers whose slots differ, ascending, and whether the trailers differ.
///
/// Every write makes a slot of its own, so the change set between a version
/// and the version that a transaction made from it is what the transaction
/// wrote. Between versions further apart, it is what the transactions
/// between them wrote, less the numbers that both versions leave free with
/// the same generation.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ChangeSet {
    objects: Box<[u32]>,
    trailer: bool,
}

impl Version {
    const OVERHEAD: u64 = (size_of::<Self>() + 2 * size_of::<usize>()) as u64;

    /// The version record and its change set, excluding shared objects.
    fn record_size(&self) -> u64 {
        Self::OVERHEAD + self.written.objects.len() as u64 * size_of::<u32>() as u64
    }

    /// Returns the first version of a document from `origin`: its base,
    /// before any transaction.
    fn first(origin: Arc<Origin>) -> Self {
        let next = origin.base().next_number().max(1);
        Self {
            origin,
            table: Table::new(),
            trailer: None,
            next,
            written: ChangeSet::default(),
            stale: Stale::default(),
        }
    }

    /// Returns a version that shares everything with this one, for a
    /// transaction to write into.
    fn draft(&self) -> Self {
        Self {
            origin: Arc::clone(&self.origin),
            table: self.table.clone(),
            trailer: self.trailer.clone(),
            next: self.next,
            written: ChangeSet::default(),
            stale: self.stale.clone(),
        }
    }

    /// Returns the base that the version reads every object from that no
    /// transaction wrote.
    #[must_use]
    pub fn base(&self) -> &Arc<dyn Base> {
        self.origin.base()
    }

    /// Returns object `number`, if it is in use.
    ///
    /// The first read of an object parses it from its bytes, and every later
    /// read shares the value. Refuses with a malformed input error an object
    /// whose bytes hayro-syntax does not read as a value, and with a limit
    /// what the budget does not allow, such as `memory_size` for what the
    /// value holds. A refusal or an error is not kept, so a later read tries
    /// again.
    pub fn get(&self, number: u32, budget: &Budget) -> Result<Option<Object>> {
        budget.poll()?;
        match self.table.get(number) {
            Some(Slot::Written(object)) => {
                let value = object.value(budget).map_err(|error| {
                    error.context(format!("object {number} {}", object.generation))
                })?;
                Ok(Some(Object {
                    generation: object.generation,
                    value,
                }))
            }
            Some(Slot::Free(_)) => Ok(None),
            None => self.origin.object(number, budget),
        }
    }

    /// Returns the generation of object `number`, if it is in use.
    #[must_use]
    pub fn generation(&self, number: u32) -> Option<u16> {
        match self.table.get(number) {
            Some(Slot::Written(object)) => Some(object.generation),
            Some(Slot::Free(_)) => None,
            None => self.base().generation(number),
        }
    }

    /// Returns what transactions wrote for object `number`, or `None` if no
    /// transaction did, and the version reads it from the base.
    #[must_use]
    pub fn slot(&self, number: u32) -> Option<&Slot> {
        self.table.get(number)
    }

    /// Returns every object number that transactions wrote, ascending, with
    /// what they wrote: the objects and free entries that a save writes
    /// after the base.
    pub fn slots(&self) -> impl Iterator<Item = (u32, &Slot)> {
        self.table.iter()
    }

    /// Whether any object or trailer was written since the base. Undo to
    /// the initial version makes this false again, permitting an exact
    /// no-edit incremental save without reading any objects.
    #[must_use]
    pub fn has_changes(&self) -> bool {
        self.trailer.is_some() || self.slots().next().is_some()
    }

    /// Returns whether the version leaves `structure` of object `number`
    /// stale: a font subset that only a save settles, or a structure that no
    /// settler is registered for.
    #[must_use]
    pub fn is_stale(&self, structure: Structure, number: u32) -> bool {
        self.stale.contains(Mark { structure, number })
    }

    /// Returns the structures that the version leaves stale, sorted.
    pub fn stale(&self) -> impl Iterator<Item = Mark> + '_ {
        self.stale.marks()
    }

    /// Returns whether this version leaves any dependent structure stale.
    #[must_use]
    pub fn has_stale(&self) -> bool {
        self.stale.any(crate::settle::Scope::Save)
    }

    /// Returns the trailer: the one that a transaction wrote last, or the
    /// base's.
    #[must_use]
    pub fn trailer(&self) -> &Dict {
        self.trailer
            .as_ref()
            .map_or_else(|| self.base().trailer(), |trailer| &trailer.value)
    }

    /// Returns the next-number bound recorded in this version, above every
    /// number of the base and every number that this version reserved or
    /// wrote. A new transaction also respects the document's lifetime
    /// allocation high-water mark, which undo never lowers.
    ///
    /// The largest object number is 4294967295, so the result is at most
    /// 4294967296, when no number is left.
    #[must_use]
    pub const fn next_number(&self) -> u64 {
        self.next
    }

    /// Returns what the transaction that made the version wrote: the change
    /// set from the version before it.
    ///
    /// The first version of a document has an empty one, and so does the
    /// version of a transaction until it commits.
    #[must_use]
    pub const fn written(&self) -> &ChangeSet {
        &self.written
    }

    /// Returns the first version of this version's document: its base,
    /// before any transaction. [`Version::changes`] against it gives what
    /// every transaction since opening wrote, at the cost of what they
    /// wrote, not of the document.
    #[must_use]
    pub fn opened(&self) -> Self {
        Self::first(Arc::clone(&self.origin))
    }

    /// Returns the change set between this version and `other`, or `None`
    /// if they are versions of different documents.
    ///
    /// The change set is the same in both directions. Its time grows with
    /// the chunks and branches that the versions do not share: with what
    /// the transactions between them wrote, not with the document. Refuses
    /// with `memory_size` when the budget does not allow the change set.
    pub fn changes(&self, other: &Self, budget: &Budget) -> Result<Option<ChangeSet>> {
        if !Arc::ptr_eq(&self.origin, &other.origin) {
            return Ok(None);
        }
        Ok(Some(difference(self, other, budget)?.changes))
    }
}

impl fmt::Debug for Version {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Version")
            .field("next", &self.next)
            .field("written", &self.written)
            .finish_non_exhaustive()
    }
}

impl Object {
    /// Returns the generation.
    #[must_use]
    pub const fn generation(&self) -> u16 {
        self.generation
    }

    /// Returns the value, which every read of the same object shares.
    #[must_use]
    pub const fn value(&self) -> &Arc<Value> {
        &self.value
    }
}

impl Slot {
    /// Returns whether two slots are the same: the same written object, or
    /// free entries of the same generation.
    fn same(a: &Self, b: &Self) -> bool {
        match (a, b) {
            (Self::Written(a), Self::Written(b)) => Arc::ptr_eq(a, b),
            (Self::Free(a), Self::Free(b)) => a == b,
            _ => false,
        }
    }
}

impl Written {
    /// The bytes that a written object takes besides the bytes of its
    /// value: the object and the counts of its `Arc`, and the `Arc` that
    /// owns the bytes.
    const OVERHEAD: u64 =
        (4 * size_of::<usize>() + size_of::<Self>() + size_of::<Box<[u8]>>()) as u64;

    /// Returns an object of `generation` whose value `bytes` hold.
    const fn new(generation: u16, bytes: Bytes) -> Self {
        Self {
            generation,
            bytes,
            value: OnceLock::new(),
        }
    }

    /// Returns the generation.
    #[must_use]
    pub const fn generation(&self) -> u16 {
        self.generation
    }

    /// Returns the bytes of the value, as the writer wrote them: what a save
    /// writes between `obj` and `endobj`.
    #[must_use]
    pub const fn bytes(&self) -> &Bytes {
        &self.bytes
    }

    /// Returns the value, which the first call parses from the bytes and
    /// every later call shares.
    ///
    /// Refuses as [`Value::parse`] does. A refusal or an error is not kept.
    pub fn value(&self, budget: &Budget) -> Result<Arc<Value>> {
        budget.poll()?;
        if let Some(value) = self.value.get() {
            return Ok(Arc::clone(value));
        }
        let value = Value::parse(&self.bytes, budget)?;
        budget.charge(LimitCode::MemorySize, VALUE_BYTES)?;
        Ok(Arc::clone(self.value.get_or_init(|| Arc::new(value))))
    }

    /// Returns the bytes that the object holds, which the history counts.
    /// The value parsed from them is not counted.
    fn size(&self) -> u64 {
        Self::OVERHEAD.saturating_add(u64::try_from(self.bytes.len()).unwrap_or(u64::MAX))
    }
}

impl fmt::Debug for Written {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Written")
            .field("generation", &self.generation)
            .field("bytes", &self.bytes)
            .finish_non_exhaustive()
    }
}

impl ChangeSet {
    /// Returns the object numbers whose slots differ, ascending.
    #[must_use]
    pub const fn objects(&self) -> &[u32] {
        &self.objects
    }

    /// Returns whether the trailers differ.
    #[must_use]
    pub const fn trailer(&self) -> bool {
        self.trailer
    }

    /// Returns whether nothing differs.
    #[must_use]
    pub const fn is_empty(&self) -> bool {
        self.objects.is_empty() && !self.trailer
    }

    /// Returns whether the slots of object `number` differ.
    #[must_use]
    pub fn contains(&self, number: u32) -> bool {
        self.objects.binary_search(&number).is_ok()
    }
}

/// What two versions of one document hold differently.
struct Difference {
    changes: ChangeSet,
    stale: bool,
    /// The bytes of the chunks, the branches, and the written objects that
    /// only the first version holds.
    first: u64,
    /// The bytes of those that only the second version holds.
    second: u64,
}

/// Returns what `first` and `second`, versions of one document, hold
/// differently. Refuses with `memory_size` when the budget does not allow
/// the change set.
fn difference(
    first: &Version,
    second: &Version,
    budget: &Budget,
) -> Result<Difference, LimitExceeded> {
    budget.poll()?;
    let mut objects = BudgetVec::new(budget);
    let (mut first_bytes, mut second_bytes) = (0_u64, 0_u64);
    let mut steps = 0_u32;
    let unshared = first
        .table
        .diff(&second.table, Slot::same, |number, a, b| {
            if let Some(Slot::Written(object)) = a {
                first_bytes = first_bytes.saturating_add(object.size());
            }
            if let Some(Slot::Written(object)) = b {
                second_bytes = second_bytes.saturating_add(object.size());
            }
            steps = steps.wrapping_add(1);
            if steps.is_multiple_of(POLL_INTERVAL) {
                budget.poll()?;
            }
            objects.try_push(number)
        })?;
    let trailer = match (&first.trailer, &second.trailer) {
        (None, None) => false,
        (Some(a), Some(b)) => !Arc::ptr_eq(a, b),
        _ => true,
    };
    if trailer {
        first_bytes = first_bytes.saturating_add(first.trailer.as_ref().map_or(0, |t| t.size()));
        second_bytes = second_bytes.saturating_add(second.trailer.as_ref().map_or(0, |t| t.size()));
    }
    let (stale, stale_bytes) = first.stale.diff(&second.stale, budget)?;
    Ok(Difference {
        stale,
        changes: ChangeSet {
            objects: objects.into_vec().into_boxed_slice(),
            trailer,
        },
        first: first_bytes
            .saturating_add(unshared.first)
            .saturating_add(stale_bytes.first),
        second: second_bytes
            .saturating_add(unshared.second)
            .saturating_add(stale_bytes.second),
    })
}
