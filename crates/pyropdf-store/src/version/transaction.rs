//! Transactions (DESIGN.md section 4.1).

use std::fmt;
use std::sync::Arc;

use super::document::WriterLock;
use super::{Slot, Trailer, Version, Written, difference};
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::settle::{self, Mark, Scope, Settlers, Structure};
use crate::value::{Bytes, Dict, Reference, Value};
use crate::{Error, Result, write};

/// Writes that start from the current version of a document and publish the
/// next version when they commit (DESIGN.md section 4.1).
///
/// [`Document::begin`](super::Document::begin) starts a transaction, which
/// holds the document's writer lock until it commits or is dropped (section
/// 4.6). It starts from the current version without copying it, and
/// [`Transaction::version`] reads what it has written so far. A write
/// copies only the chunk of its slot and the branches above it that the
/// transaction does not hold alone yet. Dropping a transaction, as an error
/// does, discards what it wrote: nothing was cloned, and nothing needs
/// rolling back.
///
/// A refused allocation, write, or free changes nothing, so the transaction
/// may go on or be dropped.
///
/// A write that makes a dependent structure stale marks it with
/// [`Transaction::mark`], and the transaction settles it with the
/// document's settlers ([`crate::settle`]).
pub struct Transaction {
    lock: WriterLock,
    parent: Arc<Version>,
    draft: Version,
    settlers: Settlers,
    /// One transient snapshot anchoring a derived result while this
    /// transaction is open. It is discarded when the transaction ends.
    derived_anchor: Option<Arc<Version>>,
}

impl Transaction {
    /// Returns a transaction that holds `lock` and starts from `parent`.
    pub(super) fn new(lock: WriterLock, parent: Arc<Version>) -> Self {
        let mut draft = parent.draft();
        draft.next = draft.next.max(lock.next_number());
        let settlers = lock.settlers();
        Self {
            lock,
            parent,
            draft,
            settlers,
            derived_anchor: None,
        }
    }

    /// Returns the version that the transaction started from.
    #[must_use]
    pub const fn parent(&self) -> &Arc<Version> {
        &self.parent
    }

    /// Returns the version that the transaction has written so far, which
    /// its commit publishes. Its [`Version::written`] stays empty until then.
    #[must_use]
    pub const fn version(&self) -> &Version {
        &self.draft
    }

    /// Returns a reference to a new object: the next free object number,
    /// with generation 0. The number holds no object until the transaction
    /// writes one there. The document never issues this number again, even
    /// after undo, an empty commit, or dropping this transaction.
    ///
    /// Refuses with a malformed input error when the document uses the
    /// largest object number, 4294967295, so that no number is left.
    pub fn allocate(&mut self) -> Result<Reference> {
        let number = u32::try_from(self.draft.next).map_err(|_| {
            Error::malformed(
                "the document uses object number 4294967295, so no number is left for a new object",
            )
        })?;
        self.draft.next += 1;
        self.lock.reserve_through(self.draft.next);
        Ok(Reference::new(number, 0))
    }

    /// Writes `value` as object `number`, and returns the reference that
    /// names it.
    ///
    /// The writer serializes the value, writing what kept its lexeme as that
    /// lexeme ([`write::value`]), and the version keeps the bytes, which the
    /// object's first read parses. An object in use keeps its generation, a
    /// free number takes the generation that its free entry gives, and a
    /// number never used takes generation 0.
    ///
    /// Refuses with a malformed input error object number 0, which heads the
    /// list of free objects (ISO 32000-2 section 7.5.4), a number free with
    /// generation 65535, which PDF never uses again, and what the writer
    /// refuses. Refuses with a limit what the budget does not allow: the
    /// bytes, and the chunk and the branches that the write copies.
    pub fn write(&mut self, number: u32, value: &Value, budget: &Budget) -> Result<Reference> {
        if number == 0 {
            return Err(Error::malformed(
                "object number 0 heads the list of free objects and is never in use",
            ));
        }
        let base_free = self.draft.base().free_generation(number);
        let generation = match self.draft.table.get(number) {
            Some(Slot::Written(object)) => object.generation,
            Some(&Slot::Free(u16::MAX)) => {
                return Err(Error::malformed(format!(
                    "object {number} is free with generation 65535, which PDF never uses again"
                )));
            }
            Some(&Slot::Free(generation)) => generation,
            None => {
                if base_free == Some(u16::MAX) {
                    return Err(Error::malformed(format!(
                        "object {number} is free with generation 65535, which PDF never uses again"
                    )));
                }
                self.draft
                    .base()
                    .generation(number)
                    .or(base_free)
                    .unwrap_or(0)
            }
        };
        let mut out = BudgetVec::new(budget);
        write::value(&mut out, value)
            .map_err(|error| error.context(format!("object {number} {generation}")))?;
        budget.charge(LimitCode::MemorySize, Written::OVERHEAD)?;
        let bytes = Bytes::new(out.into_vec().into_boxed_slice());
        let object = Arc::new(Written::new(generation, bytes));
        self.draft
            .table
            .insert(number, Slot::Written(object), budget)?;
        self.draft.next = self.draft.next.max(u64::from(number) + 1);
        self.lock.reserve_through(self.draft.next);
        Ok(Reference::new(number, generation))
    }

    /// Frees object `number` if it is in use, and returns whether it was.
    ///
    /// The number's free entry takes the next generation, which stays at
    /// 65535 once there: PDF never uses such a number again. Refuses with
    /// `memory_size` when the budget does not allow the chunk and the
    /// branches that the free copies.
    pub fn free(&mut self, number: u32, budget: &Budget) -> Result<bool> {
        let generation = match self.draft.table.get(number) {
            Some(Slot::Written(object)) => object.generation,
            Some(Slot::Free(_)) => return Ok(false),
            None => match self.draft.base().generation(number) {
                Some(generation) => generation,
                None => return Ok(false),
            },
        };
        self.draft
            .table
            .insert(number, Slot::Free(generation.saturating_add(1)), budget)?;
        Ok(true)
    }

    /// Replaces the trailer, retaining its serialized bytes for lossless
    /// writing and history accounting. A refusal leaves the draft unchanged.
    pub fn set_trailer(&mut self, trailer: Dict, budget: &Budget) -> Result<()> {
        let mut out = BudgetVec::new(budget);
        write::value(&mut out, &Value::Dict(trailer))?;
        budget.charge(LimitCode::MemorySize, Trailer::OVERHEAD)?;
        let bytes = Bytes::new(out.into_vec().into_boxed_slice());
        let Value::Dict(value) = Value::parse(&bytes, budget)? else {
            return Err(Error::malformed("the written trailer is not a dictionary"));
        };
        self.draft.trailer = Some(Arc::new(Trailer { value, bytes }));
        Ok(())
    }

    /// Marks `structure` of object `number` stale, so that the transaction
    /// settles it. Marking it again before it settles adds nothing. Refuses
    /// with `memory_size` when the budget does not allow a new mark.
    pub fn mark(&mut self, structure: Structure, number: u32, budget: &Budget) -> Result<()> {
        self.draft.stale.insert(Mark { structure, number }, budget)
    }

    /// Returns whether `structure` of object `number` is stale in what the
    /// transaction has written so far.
    #[must_use]
    pub fn is_stale(&self, structure: Structure, number: u32) -> bool {
        self.draft.stale.contains(Mark { structure, number })
    }

    /// Settles the stale structures that `scope` covers and that a settler
    /// is registered for. A read that depends on a structure calls this
    /// with [`Scope::One`] first; a commit settles [`Scope::Commit`].
    ///
    /// Refuses with what a settler refuses, and with a malformed input error
    /// when the settlers keep marking one another's structures. The
    /// transaction restores its writes and marks to before the call on any
    /// error, so a read may retry without observing a partial settlement.
    pub fn settle(&mut self, scope: Scope, budget: &Budget) -> Result<()> {
        if !self.draft.stale.any(scope) {
            return Ok(());
        }
        let settlers = self.settlers.clone();
        let savepoint = self.savepoint();
        let result = settle::settle(self, &settlers, scope, budget);
        if result.is_err() {
            self.rollback(savepoint);
        }
        result
    }

    /// Removes the marks of `structure` for a settler, and returns the
    /// numbers they named.
    pub(crate) fn take_stale<'b>(
        &mut self,
        structure: Structure,
        budget: &'b Budget,
    ) -> Result<BudgetVec<'b, u32>> {
        self.draft.stale.take(structure, budget)
    }

    /// Returns what the transaction has written so far as a version of its
    /// own, sharing everything with the draft, for reads that need to own
    /// a version. It is no version of the document's history.
    #[must_use]
    pub fn snapshot(&self) -> Arc<Version> {
        Arc::new(self.draft.draft())
    }

    /// Retains at most one snapshot for a transaction-local derived result.
    pub fn retain_derived_version(&mut self, version: Arc<Version>) {
        self.derived_anchor = Some(version);
    }

    /// Returns the transient snapshot retained for derived results.
    pub fn derived_anchor(&self) -> Option<&Arc<Version>> {
        self.derived_anchor.as_ref()
    }

    /// Returns a point that [`Transaction::rollback`] returns the
    /// transaction to, discarding what it writes after it. Taking one
    /// copies nothing: it shares the draft's chunks and branches.
    #[must_use]
    pub fn savepoint(&self) -> Savepoint {
        Savepoint(self.draft.draft())
    }

    /// Discards what the transaction wrote since `savepoint`. Object
    /// numbers allocated since stay used: no number is issued twice.
    pub fn rollback(&mut self, savepoint: Savepoint) {
        let next = self.draft.next;
        self.draft = savepoint.0;
        self.draft.next = self.draft.next.max(next);
    }

    /// Publishes what the transaction wrote as the document's current
    /// version, and returns that version.
    ///
    /// The new version is current at once for every thread, and the history
    /// records it as one step. A transaction that changed no object, trailer,
    /// or stale mark returns the version it started from.
    ///
    /// The commit first settles every stale structure but font subsets
    /// ([`Scope::Commit`]), as part of the same step. It refuses with what
    /// settling refuses, and with a limit when the budget does not allow the
    /// change set, and then publishes nothing: the transaction is dropped.
    pub fn commit(mut self, budget: &Budget) -> Result<Arc<Version>> {
        self.settle(Scope::Commit, budget)?;
        let Self {
            mut lock,
            parent,
            mut draft,
            settlers: _,
            derived_anchor: _,
        } = self;
        let difference = difference(&parent, &draft, budget)?;
        if difference.changes.is_empty() && !difference.stale {
            return Ok(parent);
        }
        budget.charge(LimitCode::MemorySize, Version::OVERHEAD)?;
        draft.written = difference.changes;
        let version = Arc::new(draft);
        lock.publish(Arc::clone(&version), difference.second, difference.first);
        Ok(version)
    }
}

/// A point of a transaction to roll back to: [`Transaction::savepoint`].
pub struct Savepoint(Version);

impl fmt::Debug for Savepoint {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.debug_tuple("Savepoint").field(&self.0).finish()
    }
}

impl fmt::Debug for Transaction {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Transaction")
            .field("parent", &self.parent)
            .field("version", &self.draft)
            .finish_non_exhaustive()
    }
}
