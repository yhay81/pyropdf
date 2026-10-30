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

use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::thread::{self, ThreadId};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyropdf_store::budget::Budget;
use pyropdf_store::settle::Scope;
use pyropdf_store::version::{Document, RestoredHistory, Savepoint, Transaction, Version};

use crate::document::_Document;
use crate::errors::{PdfError, store_error};

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
    savepoints: Vec<Savepoint>,
    name: String,
    /// What the transaction wrote, as reads see it, until the next write.
    snapshot: Option<Arc<Version>>,
}

impl Open {
    /// Returns what the transaction wrote, as reads see it. A read may
    /// depend on any structure that a write left stale, so the transaction
    /// first settles every structure that a commit settles.
    fn view(&mut self, budget: &Budget) -> PyResult<Arc<Version>> {
        if let Some(snapshot) = &self.snapshot {
            return Ok(Arc::clone(snapshot));
        }
        self.transaction
            .settle(Scope::Commit, budget)
            .map_err(|e| store_error(&e))?;
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
}

impl _Document {
    /// Returns the version that reads see: what this thread's open
    /// transaction has written, or the current version.
    pub(crate) fn reading(&self) -> PyResult<Arc<Version>> {
        let store = self.document()?;
        let mut open = lock(&self.history.open);
        Ok(match owned(&mut open) {
            Some(open) => open.view(&self.budget())?,
            None => store.current(),
        })
    }

    /// Returns the version that reads see, as [`_Document::reading`] does,
    /// and the open transaction's parent when the transaction has written
    /// nothing yet, so that a reader may use what it memoized for the parent.
    pub(crate) fn reading_parent(&self) -> PyResult<(Arc<Version>, Option<Arc<Version>>)> {
        let store = self.document()?;
        let mut open = lock(&self.history.open);
        let Some(open) = owned(&mut open) else {
            return Ok((store.current(), None));
        };
        let budget = self.budget();
        let view = open.view(&budget)?;
        let parent = Arc::clone(open.transaction.parent());
        let unchanged = parent
            .changes(&view, &budget)
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
        let budget = self.budget();
        {
            let mut open = lock(&self.history.open);
            if let Some(open) = owned(&mut open) {
                open.snapshot = None;
                return write(&mut open.transaction, &budget);
            }
        }
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
        let budget = self.budget();
        {
            let mut open = lock(&self.history.open);
            if let Some(open) = owned(&mut open) {
                let savepoint = open.transaction.savepoint();
                let result = write(&mut open.transaction, &budget);
                open.transaction.rollback(savepoint);
                open.snapshot = None;
                return result;
            }
        }
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
    document.document()?;
    let mut open = lock(&document.history.open);
    if let Some(open) = owned(&mut open) {
        open.savepoints.push(open.transaction.savepoint());
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
        name,
        snapshot: None,
    });
    Ok(())
}

/// Ends the innermost transaction that this thread opened: with `commit`,
/// its writes stay, and the outermost one commits them as one step;
/// otherwise its writes are discarded.
#[pyfunction]
pub fn end_transaction(document: &_Document, commit: bool) -> PyResult<()> {
    let store = document.document()?;
    let mut guard = lock(&document.history.open);
    let Some(open) = owned(&mut guard) else {
        return Err(PdfError::new_err("no transaction is open"));
    };
    if let Some(savepoint) = open.savepoints.pop() {
        if !commit {
            open.transaction.rollback(savepoint);
            open.snapshot = None;
        }
        return Ok(());
    }
    let open = guard.take().expect("a transaction is open");
    drop(guard);
    if commit {
        document.commit(&store, open.transaction, open.name, &document.budget())?;
    }
    Ok(())
}

/// Returns the step names, how many undo can take, and the history's
/// limit and size in bytes.
#[pyfunction]
pub fn history_state(document: &_Document) -> PyResult<(Vec<String>, usize, Option<u64>, u64)> {
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
    outside_transaction(document, "undo")?;
    let (store, step) = document.on_current(py, Document::undo)?;
    if step.is_none() {
        return Err(PdfError::new_err("nothing to undo"));
    }
    let mut steps = lock(&document.history.steps);
    steps.position = steps.position.saturating_sub(1);
    sync(&mut steps, &store);
    Ok(())
}

/// Takes one redo step.
#[pyfunction]
pub fn history_redo(py: Python<'_>, document: &_Document) -> PyResult<()> {
    outside_transaction(document, "redo")?;
    let (store, step) = document.on_current(py, Document::redo)?;
    if step.is_none() {
        return Err(PdfError::new_err("nothing to redo"));
    }
    let mut steps = lock(&document.history.steps);
    steps.position = (steps.position + 1).min(steps.names.len());
    sync(&mut steps, &store);
    Ok(())
}

/// Sets the history budget in bytes; `None` removes it, and 0 keeps no
/// step.
#[pyfunction]
pub fn set_history_limit(document: &_Document, limit: Option<u64>) -> PyResult<()> {
    let store = document.document()?;
    store.set_history_limit(limit);
    sync(&mut lock(&document.history.steps), &store);
    Ok(())
}

/// Forgets every step, keeping the current version and the budget.
#[pyfunction]
pub fn history_clear(document: &_Document) -> PyResult<()> {
    outside_transaction(document, "clear the history")?;
    let store = document.document()?;
    let limit = store.history().limit;
    store.set_history_limit(Some(0));
    store.set_history_limit(limit);
    sync(&mut lock(&document.history.steps), &store);
    Ok(())
}

/// Returns the bytes of the history, which `history_restore` restores into
/// the document as opened.
#[pyfunction]
pub fn history_save<'py>(py: Python<'py>, document: &_Document) -> PyResult<Bound<'py, PyBytes>> {
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
}

/// Restores the history that `history_save` returned into the document as
/// opened, and makes its current version current.
#[pyfunction]
pub fn history_restore(py: Python<'_>, document: &_Document, data: &[u8]) -> PyResult<()> {
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
}
