//! Transactions and history for `python/pyropdf/_history.py` (DESIGN.md
//! sections 4.1 and 10, as #12 decided).
//!
//! An open Python transaction is one store transaction of the document,
//! which belongs to the thread that opened it until its outermost
//! transaction ends. A nested transaction takes a savepoint, so that its
//! exception discards only its own writes. That thread's reads see what the
//! transaction has written; other threads read the current version, so
//! they see its writes only once it commits them all (DESIGN.md section
//! 4.6). Their edits, transactions, undo, and redo wait for the store's
//! writer lock, which the transaction holds. An edit outside a transaction
//! commits at once, as one step without a name.
//!
//! A canvas is one operation (DESIGN.md F6 and section 9): `begin_canvas`
//! opens a transaction, or nests one, with a [`SharedBudget`] that its
//! edits, its reads, its source reads and its commit all charge. The budget
//! belongs to the thread that opened it, and ends with its transaction.

use std::sync::{Arc, Mutex, MutexGuard, PoisonError, Weak};
use std::thread::{self, ThreadId};
use std::time::Duration;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyropdf_store::budget::{Budget, LimitCode, SharedBudget};
use pyropdf_store::settle::Scope;
use pyropdf_store::version::{Document, RestoredHistory, Savepoint, Transaction, Version};

use crate::document::_Document;
use crate::errors::{PdfError, limit_error, store_error};

pub(crate) mod testing;

/// The default history budget of Python documents, 256 MiB (DESIGN.md
/// section 4.1).
pub const DEFAULT_HISTORY_LIMIT: u64 = 256 * 1024 * 1024;

/// The transaction state and the step names of one document.
#[derive(Default)]
pub struct State {
    open: Mutex<Option<Open>>,
    steps: Mutex<Steps>,
}

/// An open transaction, with the savepoints of its nested transactions.
struct Open {
    /// The thread that opened it, the only one that writes into it.
    owner: ThreadId,
    transaction: Transaction,
    /// The savepoint of each nested transaction, with the operation that
    /// was active when it began.
    savepoints: Vec<(Savepoint, Option<SharedBudget>)>,
    /// The canvas operation of the innermost canvas, if one is open.
    operation: Option<SharedBudget>,
    name: String,
    /// What the transaction wrote, as reads see it, until the next write.
    snapshot: Option<Arc<Version>>,
}

impl Open {
    /// Bounds the whole nesting stack even when each transaction gets a fresh
    /// operation budget, and charges each new slot before allocating it.
    #[expect(
        clippy::disallowed_methods,
        reason = "the stack size is checked and each new slot is charged before fallible reservation and push"
    )]
    fn push_savepoint(&mut self, budget: &Budget) -> PyResult<()> {
        budget.poll().map_err(|error| limit_error(&error))?;
        let slot = size_of::<(Savepoint, Option<SharedBudget>)>() as u64;
        let size = (self.savepoints.len() as u64)
            .saturating_add(1)
            .saturating_mul(slot);
        budget
            .check(LimitCode::MemorySize, size)
            .map_err(|error| limit_error(&error))?;
        if self.savepoints.len() == self.savepoints.capacity() {
            budget
                .charge(LimitCode::MemorySize, slot)
                .map_err(|error| limit_error(&error))?;
            if self.savepoints.try_reserve_exact(1).is_err() {
                budget.release(LimitCode::MemorySize, slot);
                return Err(limit_error(&pyropdf_store::LimitExceeded::new(
                    LimitCode::MemorySize,
                )));
            }
        }
        let savepoint = self.transaction.savepoint();
        self.savepoints.push((savepoint, self.operation.clone()));
        Ok(())
    }

    /// Returns the budget of what runs in this transaction: its canvas
    /// operation, or a new operation of `document`.
    fn budget(&self, document: &_Document) -> SharedBudget {
        self.operation
            .clone()
            .unwrap_or_else(|| document.fresh_budget().share())
    }

    /// Returns what the transaction wrote, as reads see it. A read may
    /// depend on any structure that a write left stale, so the transaction
    /// first settles every structure that a commit settles.
    fn view(&mut self, budget: &Budget) -> PyResult<Arc<Version>> {
        if let Some(snapshot) = &self.snapshot {
            return Ok(Arc::clone(snapshot));
        }
        restore_on_error(&mut self.transaction, |transaction| {
            transaction
                .settle(Scope::Commit, budget)
                .map_err(|error| store_error(&error))
        })?;
        Ok(Arc::clone(
            self.snapshot.insert(self.transaction.snapshot()),
        ))
    }
}

/// The names of the history's steps, and how many of them undo can take.
#[derive(Default)]
struct Steps {
    names: Vec<String>,
    position: usize,
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// A borrowed transaction outlives this call. Restore its writes on any
/// failure, including a panic already translated by an inner boundary.
/// Python can catch the error inside its transaction and keep using it.
fn restore_on_error<T>(
    transaction: &mut Transaction,
    work: impl FnOnce(&mut Transaction) -> PyResult<T>,
) -> PyResult<T> {
    let savepoint = transaction.savepoint();
    let result = crate::errors::boundary(|| work(transaction)).and_then(std::convert::identity);
    if result.is_err() {
        transaction.rollback(savepoint);
    }
    result
}

/// Returns the open transaction if this thread opened it.
fn owned(open: &mut Option<Open>) -> Option<&mut Open> {
    open.as_mut()
        .filter(|open| open.owner == thread::current().id())
}

impl State {
    /// Forgets the open transaction and the step names.
    pub fn clear(&self) {
        lock(&self.open).take();
        *lock(&self.steps) = Steps::default();
    }

    /// Returns the canvas operation that this thread has open, if any.
    pub(crate) fn operation(&self) -> Option<SharedBudget> {
        owned(&mut lock(&self.open)).and_then(|open| open.operation.clone())
    }
}

/// The private handle of one canvas operation, for the canvas code in
/// Python and the calls it makes.
///
/// It refuses every use once its canvas has ended, or on another thread.
#[pyclass(frozen, name = "_OperationBudget", module = "pyropdf._core")]
pub(crate) struct OperationBudget {
    pub(crate) operation: SharedBudget,
    history: Weak<State>,
}

impl OperationBudget {
    /// Refuses unless this is the innermost canvas operation of this
    /// thread's open transaction.
    pub(crate) fn ensure_active(&self) -> PyResult<()> {
        let active = self
            .history
            .upgrade()
            .and_then(|history| history.operation())
            .is_some_and(|active| active.ptr_eq(&self.operation));
        if active {
            Ok(())
        } else {
            Err(PyValueError::new_err(
                "the canvas operation is not active on this thread",
            ))
        }
    }

    /// Returns whether this operation is the innermost canvas of `document`.
    pub(crate) fn is_active_on(&self, document: &_Document) -> bool {
        self.history
            .upgrade()
            .is_some_and(|history| Arc::ptr_eq(&history, &document.history))
            && self.ensure_active().is_ok()
    }

    /// Returns a view of the operation that also observes the limits,
    /// deadline and cancellation of `source`, for reading it.
    pub(crate) fn for_source(&self, source: &_Document) -> PyResult<SharedBudget> {
        self.operation
            .constrained_by(&source.fresh_budget())
            .map_err(|error| limit_error(&error))
    }
}

/// Returns the limit code named `code`.
fn budget_code(code: &str) -> PyResult<LimitCode> {
    LimitCode::ALL
        .iter()
        .copied()
        .find(|candidate| candidate.as_str() == code)
        .ok_or_else(|| PyValueError::new_err(format!("unknown limit code {code:?}")))
}

#[pymethods]
impl OperationBudget {
    /// Charges `amount` to the meter of `code`.
    fn charge(&self, code: &str, amount: u64) -> PyResult<()> {
        crate::errors::boundary(|| {
            self.ensure_active()?;
            self.operation
                .charge(budget_code(code)?, amount)
                .map_err(|error| limit_error(&error))
        })?
    }

    /// Returns `amount` to the meter of `code`.
    fn release(&self, code: &str, amount: u64) -> PyResult<()> {
        crate::errors::boundary(|| {
            self.ensure_active()?;
            self.operation.release(budget_code(code)?, amount);
            Ok(())
        })?
    }

    /// Checks one measurement against the limit of `code`.
    fn check(&self, code: &str, value: u64) -> PyResult<()> {
        crate::errors::boundary(|| {
            self.ensure_active()?;
            self.operation
                .check(budget_code(code)?, value)
                .map_err(|error| limit_error(&error))
        })?
    }

    /// Returns what has been charged to `code` and not released.
    fn used(&self, code: &str) -> PyResult<u64> {
        crate::errors::boundary(|| {
            self.ensure_active()?;
            Ok(self.operation.used(budget_code(code)?))
        })?
    }

    /// Returns the limit of `code`, or `None`.
    fn limit(&self, code: &str) -> PyResult<Option<u64>> {
        crate::errors::boundary(|| {
            self.ensure_active()?;
            Ok(self.operation.limit(budget_code(code)?))
        })?
    }

    /// Refuses once the operation is cancelled or past its deadline.
    fn poll(&self) -> PyResult<()> {
        crate::errors::boundary(|| {
            self.ensure_active()?;
            self.operation.poll().map_err(|error| limit_error(&error))
        })?
    }
}

impl _Document {
    /// Returns the version that reads see: what this thread's open
    /// transaction has written, or the current version.
    pub(crate) fn reading(&self) -> PyResult<Arc<Version>> {
        let store = self.document()?;
        let mut open = lock(&self.history.open);
        Ok(match owned(&mut open) {
            Some(open) => {
                let budget = open.budget(self);
                open.view(&budget)?
            }
            None => store.current(),
        })
    }

    /// Returns the version that reads see, as [`_Document::reading`] does,
    /// and the open transaction's parent when the transaction has written
    /// nothing yet, so that a reader may use what it memoized for the parent.
    pub(crate) fn reading_parent(&self) -> PyResult<(Arc<Version>, Option<Arc<Version>>)> {
        self.reading_parent_with(None)
    }

    /// Returns what [`_Document::reading_parent`] does, charging `budget`
    /// instead of this document's operation, as a canvas reading its source
    /// does.
    pub(crate) fn reading_parent_with(
        &self,
        budget: Option<&Budget>,
    ) -> PyResult<(Arc<Version>, Option<Arc<Version>>)> {
        let store = self.document()?;
        let mut open = lock(&self.history.open);
        let Some(open) = owned(&mut open) else {
            return Ok((store.current(), None));
        };
        let own = open.budget(self);
        let budget = budget.unwrap_or(&own);
        let view = open.view(budget)?;
        let parent = Arc::clone(open.transaction.parent());
        let unchanged = parent
            .changes(&view, budget)
            .map_err(|e| store_error(&e))?
            .is_some_and(|changes| changes.is_empty());
        Ok((view, unchanged.then_some(parent)))
    }

    /// Returns whether this thread has a transaction open.
    pub(crate) fn in_transaction(&self) -> bool {
        owned(&mut lock(&self.history.open)).is_some()
    }

    /// Runs `write` in this thread's open transaction, or in a transaction
    /// of its own that commits as one step without a name, once no other
    /// transaction holds the writer lock.
    pub(crate) fn edit<T>(
        &self,
        py: Python<'_>,
        write: impl FnOnce(&mut Transaction, &Budget) -> PyResult<T>,
    ) -> PyResult<T> {
        self.document()?;
        {
            let mut open = lock(&self.history.open);
            if let Some(open) = owned(&mut open) {
                open.snapshot = None;
                let budget = open.budget(self);
                return restore_on_error(&mut open.transaction, |transaction| {
                    write(transaction, &budget)
                });
            }
        }
        let budget = self.budget();
        let (store, mut transaction) = self.on_current(py, Document::begin)?;
        let value = write(&mut transaction, &budget)?;
        self.commit(&store, transaction, String::new(), &budget)?;
        Ok(value)
    }

    /// Runs `write` in a transaction whose writes are then discarded: in a
    /// savepoint of this thread's open transaction, or in a transaction of
    /// its own that is dropped. `write` returns what it read, such as a
    /// snapshot.
    pub(crate) fn scratch<T>(
        &self,
        py: Python<'_>,
        write: impl FnOnce(&mut Transaction, &Budget) -> PyResult<T>,
    ) -> PyResult<T> {
        self.document()?;
        {
            let mut open = lock(&self.history.open);
            if let Some(open) = owned(&mut open) {
                let budget = open.budget(self);
                let savepoint = open.transaction.savepoint();
                let result = crate::errors::boundary(|| write(&mut open.transaction, &budget));
                open.transaction.rollback(savepoint);
                open.snapshot = None;
                return result?;
            }
        }
        let budget = self.budget();
        let (_, mut transaction) = self.on_current(py, Document::begin)?;
        write(&mut transaction, &budget)
    }

    /// Runs `act` on the store's document with the GIL released, and returns
    /// the document it ran on. While `act` waits for the writer lock, signing
    /// may replace the document; `act` then runs again on the new one, so
    /// that what it writes is not lost with the old one.
    pub(crate) fn on_current<T: Send>(
        &self,
        py: Python<'_>,
        act: impl Fn(&Document) -> T + Sync,
    ) -> PyResult<(Arc<Document>, T)> {
        loop {
            let store = self.document()?;
            let value = py.detach(|| act(&store));
            if Arc::ptr_eq(&store, &self.document()?) {
                return Ok((store, value));
            }
        }
    }

    /// Returns whether this thread's open transaction is an outermost one,
    /// without nested transactions.
    pub(crate) fn in_outermost_transaction(&self) -> bool {
        owned(&mut lock(&self.history.open)).is_some_and(|open| open.savepoints.is_empty())
    }

    /// Commits `transaction`, and names the step it records.
    #[expect(
        clippy::disallowed_methods,
        reason = "one moved name per committed history step; sync trims the list to the store's budgeted undo and redo steps"
    )]
    fn commit(
        &self,
        store: &Document,
        transaction: Transaction,
        name: String,
        budget: &Budget,
    ) -> PyResult<()> {
        let parent = Arc::clone(transaction.parent());
        let anchor = transaction.derived_anchor().cloned();
        let committed = transaction.commit(budget).map_err(|e| store_error(&e))?;
        self.outline_counts.carry_to(&committed, budget);
        if let Some(anchor) = anchor {
            self.fill_index
                .carry_from(&self.draft_fill_index, &anchor, &committed, budget);
            self.fill_index
                .carry_from(&self.fill_index, &anchor, &committed, budget);
        }
        self.fill_index
            .carry_from(&self.fill_index, &parent, &committed, budget);
        let mut steps = lock(&self.history.steps);
        if !Arc::ptr_eq(&parent, &committed) {
            let position = steps.position;
            steps.names.truncate(position);
            steps.names.push(name);
            steps.position += 1;
        }
        sync(&mut steps, store);
        Ok(())
    }
}

/// Keeps the names of the steps that the store's history still holds: the
/// newest undo steps and the nearest redo steps.
fn sync(steps: &mut Steps, store: &Document) {
    let status = store.history();
    let position = steps.position;
    let undo = position.min(status.undo);
    let redo = (steps.names.len() - position).min(status.redo);
    steps.names.truncate(position + redo);
    steps.names.drain(..position - undo);
    steps.position = undo;
}

/// Opens a transaction named `name`, or nests one in this thread's open
/// transaction. Another thread's transaction is waited for, with the GIL
/// released, as the store's writer lock waits.
#[pyfunction]
pub fn begin_transaction(py: Python<'_>, document: &_Document, name: String) -> PyResult<()> {
    crate::errors::boundary(|| {
        document.document()?;
        let mut open = lock(&document.history.open);
        if let Some(open) = owned(&mut open) {
            open.push_savepoint(&open.budget(document))?;
            return Ok(());
        }
        drop(open);
        let (_, transaction) = document.on_current(py, Document::begin)?;
        // Holding the writer lock, this thread is the only one with an open
        // transaction: another thread's ended before its lock was released.
        *lock(&document.history.open) = Some(Open {
            owner: thread::current().id(),
            transaction,
            savepoints: Vec::new(),
            operation: None,
            name,
            snapshot: None,
        });
        Ok(())
    })?
}

/// The limits that a canvas's text calls set for each call, from their
/// `max_*` options, rather than for the whole operation.
const CANVAS_CALL_LIMITS: [LimitCode; 2] = [LimitCode::TextInputSize, LimitCode::TextLineCount];

/// Returns a canvas operation's budget from the document's limits. Its
/// calls set the limits of [`CANVAS_CALL_LIMITS`] themselves.
fn canvas_budget(document: &_Document) -> Budget {
    CANVAS_CALL_LIMITS
        .into_iter()
        .fold(document.fresh_budget(), |budget, code| {
            budget.with_limit(code, None)
        })
}

/// Opens a canvas operation: a transaction named `draw`, or one nested in
/// this thread's open transaction, whose work charges one budget from the
/// document's limits. A canvas nested in another operation is a view of the
/// outer operation, which it charges. Waiting for another thread's
/// transaction is part of the operation, so its deadline and cancellation
/// apply to the wait.
#[pyfunction]
pub fn begin_canvas(py: Python<'_>, document: &_Document) -> PyResult<OperationBudget> {
    crate::errors::boundary(|| begin_with(py, document, || canvas_budget(document), "draw"))?
}

/// Opens an operation named `name` as [`begin_canvas`] opens a canvas, but
/// with every limit of the document, for an edit of several core calls
/// such as a form fill in an embedded font. `abort_canvas` discards it.
#[pyfunction]
pub fn begin_operation(
    py: Python<'_>,
    document: &_Document,
    name: String,
) -> PyResult<OperationBudget> {
    crate::errors::boundary(|| begin_with(py, document, || document.fresh_budget(), &name))?
}

/// Opens an operation whose budget `limits` returns, named `name` when it
/// is the outermost transaction.
fn begin_with(
    py: Python<'_>,
    document: &_Document,
    limits: impl Fn() -> Budget,
    name: &str,
) -> PyResult<OperationBudget> {
    document.document()?;
    let handle = |operation: SharedBudget| OperationBudget {
        operation,
        history: Arc::downgrade(&document.history),
    };
    {
        let mut guard = lock(&document.history.open);
        if let Some(open) = owned(&mut guard) {
            let operation = match &open.operation {
                Some(outer) => outer.constrained_by(&limits()),
                None => {
                    let operation = limits().share();
                    operation.poll().map(|()| operation)
                }
            }
            .map_err(|error| limit_error(&error))?;
            open.push_savepoint(&operation)?;
            open.operation = Some(operation.clone());
            return Ok(handle(operation));
        }
    }
    let operation = limits().share();
    let (_, transaction) = document.on_current(py, |store| {
        loop {
            operation.poll()?;
            if let Some(transaction) = store.try_begin() {
                break Ok(transaction);
            }
            thread::sleep(Duration::from_millis(1));
        }
    })?;
    let transaction = transaction.map_err(|error| limit_error(&error))?;
    *lock(&document.history.open) = Some(Open {
        owner: thread::current().id(),
        transaction,
        savepoints: Vec::new(),
        operation: Some(operation.clone()),
        name: name.into(),
        snapshot: None,
    });
    Ok(handle(operation))
}

/// Ends the innermost transaction that this thread opened: with `commit`,
/// its writes stay, and the outermost one commits them as one step;
/// otherwise its writes are discarded.
#[pyfunction]
pub fn end_transaction(document: &_Document, commit: bool) -> PyResult<()> {
    crate::errors::boundary(|| {
        let store = document.document()?;
        let mut guard = lock(&document.history.open);
        let Some(open) = owned(&mut guard) else {
            return Err(PdfError::new_err("no transaction is open"));
        };
        // A cancelled or expired operation commits nothing.
        let refusal = if commit {
            open.operation
                .as_ref()
                .and_then(|operation| operation.poll().err())
        } else {
            None
        };
        if let Some((savepoint, outer)) = open.savepoints.pop() {
            if !commit || refusal.is_some() {
                open.transaction.rollback(savepoint);
                open.snapshot = None;
            }
            open.operation = outer;
            return refusal.map_or(Ok(()), |error| Err(limit_error(&error)));
        }
        let open = guard.take().expect("a transaction is open");
        drop(guard);
        if let Some(error) = refusal {
            return Err(limit_error(&error));
        }
        if commit {
            let budget = open.budget(document);
            document.commit(&store, open.transaction, open.name, &budget)?;
        }
        Ok(())
    })?
}

/// Discards the canvas of `operation` and what nested in it, if it is still
/// open on this thread. A canvas that already ended leaves its outer
/// transaction alone.
#[pyfunction]
pub fn abort_canvas(document: &_Document, operation: &OperationBudget) -> PyResult<()> {
    crate::errors::boundary(|| {
        let mut guard = lock(&document.history.open);
        let Some(open) = owned(&mut guard) else {
            return;
        };
        let ours = |active: &Option<SharedBudget>| {
            active
                .as_ref()
                .is_some_and(|active| active.ptr_eq(&operation.operation))
        };
        if !ours(&open.operation) {
            return;
        }
        // A transaction nested in the canvas keeps the canvas as its outer
        // operation; the canvas began at the savepoint after the last one
        // whose outer operation is not the canvas.
        let Some(boundary) = open.savepoints.iter().rposition(|(_, outer)| !ours(outer)) else {
            // The canvas is the outermost transaction: dropping it discards its
            // writes and releases the writer lock.
            guard.take();
            return;
        };
        open.savepoints.truncate(boundary + 1);
        let (savepoint, outer) = open.savepoints.pop().expect("the boundary is a savepoint");
        open.transaction.rollback(savepoint);
        open.operation = outer;
        open.snapshot = None;
    })
}

/// Returns the step names, how many undo can take, and the history's
/// limit and size in bytes.
#[pyfunction]
pub fn history_state(document: &_Document) -> PyResult<(Vec<String>, usize, Option<u64>, u64)> {
    crate::errors::boundary(|| {
        let store = document.document()?;
        let mut steps = lock(&document.history.steps);
        sync(&mut steps, &store);
        let status = store.history();
        Ok((
            steps.names.clone(),
            steps.position,
            status.limit,
            status.size,
        ))
    })?
}

pub(crate) fn outside_transaction(document: &_Document, what: &str) -> PyResult<()> {
    if document.in_transaction() {
        return Err(PdfError::new_err(format!(
            "cannot {what} inside a transaction"
        )));
    }
    Ok(())
}

/// Takes one undo step.
#[pyfunction]
pub fn history_undo(py: Python<'_>, document: &_Document) -> PyResult<()> {
    crate::errors::boundary(|| {
        outside_transaction(document, "undo")?;
        let (store, step) = document.on_current(py, Document::undo)?;
        if step.is_none() {
            return Err(PdfError::new_err("nothing to undo"));
        }
        let mut steps = lock(&document.history.steps);
        steps.position = steps.position.saturating_sub(1);
        sync(&mut steps, &store);
        Ok(())
    })?
}

/// Takes one redo step.
#[pyfunction]
pub fn history_redo(py: Python<'_>, document: &_Document) -> PyResult<()> {
    crate::errors::boundary(|| {
        outside_transaction(document, "redo")?;
        let (store, step) = document.on_current(py, Document::redo)?;
        if step.is_none() {
            return Err(PdfError::new_err("nothing to redo"));
        }
        let mut steps = lock(&document.history.steps);
        steps.position = (steps.position + 1).min(steps.names.len());
        sync(&mut steps, &store);
        Ok(())
    })?
}

/// Sets the history budget in bytes; `None` removes it, and 0 keeps no
/// step.
#[pyfunction]
pub fn set_history_limit(document: &_Document, limit: Option<u64>) -> PyResult<()> {
    crate::errors::boundary(|| {
        let store = document.document()?;
        store.set_history_limit(limit);
        sync(&mut lock(&document.history.steps), &store);
        Ok(())
    })?
}

/// Forgets every step, keeping the current version and the budget.
#[pyfunction]
pub fn history_clear(document: &_Document) -> PyResult<()> {
    crate::errors::boundary(|| {
        outside_transaction(document, "clear the history")?;
        let store = document.document()?;
        let limit = store.history().limit;
        store.set_history_limit(Some(0));
        store.set_history_limit(limit);
        sync(&mut lock(&document.history.steps), &store);
        Ok(())
    })?
}

/// Returns the bytes of the history, which `history_restore` restores into
/// the document as opened.
#[pyfunction]
pub fn history_save<'py>(py: Python<'py>, document: &_Document) -> PyResult<Bound<'py, PyBytes>> {
    crate::errors::boundary(|| {
        outside_transaction(document, "save the history")?;
        let store = document.document()?;
        let (names, position) = {
            let mut steps = lock(&document.history.steps);
            sync(&mut steps, &store);
            (steps.names.clone(), steps.position)
        };
        let budget = document.budget();
        let data = py
            .detach(|| store.save_history(&names, position, &budget))
            .map_err(|e| store_error(&e))?;
        Ok(PyBytes::new(py, &data))
    })?
}

/// Restores the history that `history_save` returned into the document as
/// opened, and makes its current version current.
#[pyfunction]
pub fn history_restore(py: Python<'_>, document: &_Document, data: &[u8]) -> PyResult<()> {
    crate::errors::boundary(|| {
        outside_transaction(document, "restore a history")?;
        let store = document.document()?;
        let budget = document.budget();
        match py
            .detach(|| store.restore_history(data, &budget))
            .map_err(|e| store_error(&e))?
        {
            RestoredHistory::Restored { labels, position } => {
                let mut steps = lock(&document.history.steps);
                *steps = Steps {
                    names: labels,
                    position,
                };
                sync(&mut steps, &store);
                Ok(())
            }
            RestoredHistory::OtherDocument => Err(PyValueError::new_err(
                "the history was saved from another document",
            )),
            RestoredHistory::Changed => Err(PdfError::new_err(
                "a history is restored only into a document as opened, before any edit",
            )),
        }
    })?
}
