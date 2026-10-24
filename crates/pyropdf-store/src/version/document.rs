//! A document's current version, its writer lock, and its history
//! (DESIGN.md sections 4.1 and 4.6).

use std::collections::VecDeque;
use std::fmt;
use std::mem;
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError, RwLock};

use super::base::Origin;
use super::{Base, Transaction, Version};

/// The versions of a document: the current one, the writer lock through
/// which transactions publish the next, and the history that undo and redo
/// move along (DESIGN.md sections 4.1 and 4.6).
///
/// [`Document::current`] returns the current version, which the caller then
/// reads without locks: the document holds the version behind a lock that
/// it takes only to copy or replace the pointer. Readers on any number of
/// threads see complete versions only, and a reader keeps the version it
/// took while transactions publish newer ones.
///
/// A transaction holds the writer lock from [`Document::begin`] until it
/// commits or is dropped, and undo and redo wait for it as another
/// transaction does. A thread that holds a transaction must not begin
/// another, undo, or redo on the same document: it would wait for itself.
pub struct Document {
    shared: Arc<Shared>,
}

/// What a document and its transactions share.
struct Shared {
    /// The current version.
    current: RwLock<Arc<Version>>,
    writer: Mutex<Writer>,
    /// Signals that a transaction released the writer lock.
    idle: Condvar,
}

/// What the writer lock guards.
struct Writer {
    /// Whether a transaction holds the writer lock.
    open: bool,
    history: History,
}

/// What a document's history holds: the steps that undo and redo can take,
/// and the bytes that their versions keep alive.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct HistoryStatus {
    /// How many steps undo can take.
    pub undo: usize,
    /// How many steps redo can take.
    pub redo: usize,
    /// The bytes that the versions of the steps keep alive: what each holds
    /// and its neighbor toward the current version does not share, including
    /// written trailers, version records, change sets, and history entries.
    /// Parsed-value caches and unused deque capacity are not counted.
    pub size: u64,
    /// The most bytes that the versions of the steps may keep alive, if the
    /// history has a limit.
    pub limit: Option<u64>,
}

impl Document {
    /// Returns a document over `base`, whose first version is the base
    /// itself, and whose history keeps at most `history_limit` bytes alive
    /// beyond the current version, or any amount for `None`.
    #[must_use]
    pub fn new(base: Arc<dyn Base>, history_limit: Option<u64>) -> Self {
        let first = Arc::new(Version::first(Arc::new(Origin::new(base))));
        Self {
            shared: Arc::new(Shared {
                current: RwLock::new(Arc::clone(&first)),
                writer: Mutex::new(Writer {
                    open: false,
                    history: History::new(first, history_limit),
                }),
                idle: Condvar::new(),
            }),
        }
    }

    /// Returns the current version.
    #[must_use]
    pub fn current(&self) -> Arc<Version> {
        Arc::clone(
            &self
                .shared
                .current
                .read()
                .unwrap_or_else(PoisonError::into_inner),
        )
    }

    /// Starts a transaction from the current version, after waiting until
    /// no other transaction holds the writer lock.
    #[must_use]
    pub fn begin(&self) -> Transaction {
        let writer = self.shared.idle_writer();
        self.start(writer)
    }

    /// Starts a transaction from the current version, or returns `None` if
    /// another transaction holds the writer lock.
    #[must_use]
    pub fn try_begin(&self) -> Option<Transaction> {
        let writer = self.shared.writer();
        if writer.open {
            return None;
        }
        Some(self.start(writer))
    }

    /// Starts a transaction, taking the writer lock that `writer` shows to be
    /// free.
    fn start(&self, mut writer: MutexGuard<'_, Writer>) -> Transaction {
        writer.open = true;
        let parent = Arc::clone(writer.history.current());
        drop(writer);
        let lock = WriterLock {
            shared: Arc::clone(&self.shared),
            held: true,
        };
        Transaction::new(lock, parent)
    }

    /// Publishes the version before the current one again, and returns it,
    /// or returns `None` if the history has none. Waits for the writer lock
    /// as [`Document::begin`] does.
    pub fn undo(&self) -> Option<Arc<Version>> {
        let mut writer = self.shared.idle_writer();
        let version = writer.history.undo()?;
        self.shared.publish(&version);
        Some(version)
    }

    /// Publishes the version that the last undo left again, and returns it,
    /// or returns `None` if there is none. Waits for the writer lock as
    /// [`Document::begin`] does.
    pub fn redo(&self) -> Option<Arc<Version>> {
        let mut writer = self.shared.idle_writer();
        let version = writer.history.redo()?;
        self.shared.publish(&version);
        Some(version)
    }

    /// Returns what the history holds.
    #[must_use]
    pub fn history(&self) -> HistoryStatus {
        self.shared.writer().history.status()
    }

    /// Sets the most bytes that the history may keep alive, or removes the
    /// limit, and drops steps until the history keeps no more.
    pub fn set_history_limit(&self, limit: Option<u64>) {
        self.shared.writer().history.set_limit(limit);
    }

    /// Returns the versions of the history, oldest first, and the index of
    /// the current one.
    #[cfg(test)]
    pub(super) fn history_versions(&self) -> (Vec<Arc<Version>>, usize) {
        let writer = self.shared.writer();
        let versions = writer
            .history
            .steps
            .iter()
            .map(|step| Arc::clone(&step.version))
            .collect();
        (versions, writer.history.position)
    }
}

impl fmt::Debug for Document {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Document")
            .field("current", &self.current())
            .field("history", &self.history())
            .finish()
    }
}

impl Shared {
    /// Locks what the writer lock guards.
    fn writer(&self) -> MutexGuard<'_, Writer> {
        // The guard is held only while the history and the flag change,
        // which leaves them consistent at every point that could panic.
        self.writer.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Locks what the writer lock guards once no transaction holds the
    /// writer lock.
    fn idle_writer(&self) -> MutexGuard<'_, Writer> {
        let mut writer = self.writer();
        while writer.open {
            writer = self
                .idle
                .wait(writer)
                .unwrap_or_else(PoisonError::into_inner);
        }
        writer
    }

    /// Makes `version` the current version.
    fn publish(&self, version: &Arc<Version>) {
        let mut current = self.current.write().unwrap_or_else(PoisonError::into_inner);
        let previous = mem::replace(&mut *current, Arc::clone(version));
        drop(current);
        // Readers do not wait while the previous version is dropped.
        drop(previous);
    }
}

/// The writer lock of a document, held by a transaction. Dropping it
/// releases the lock.
pub(super) struct WriterLock {
    shared: Arc<Shared>,
    held: bool,
}

impl WriterLock {
    /// Publishes `version`, which the transaction made, as the current
    /// version, records it in the history, and releases the lock.
    ///
    /// `added` is the bytes that the version holds and the current one does
    /// not share, and `released` the bytes that the current one holds and
    /// the version does not share.
    pub(super) fn publish(&mut self, version: Arc<Version>, added: u64, released: u64) {
        let mut writer = self.shared.writer();
        self.shared.publish(&version);
        writer.history.commit(version, added, released);
        writer.open = false;
        self.held = false;
        self.shared.idle.notify_all();
    }
}

impl Drop for WriterLock {
    fn drop(&mut self) {
        if self.held {
            self.shared.writer().open = false;
            self.shared.idle.notify_all();
        }
    }
}

/// The versions that undo and redo publish (DESIGN.md section 4.1).
///
/// The history is a line of versions, oldest first, each made by a
/// transaction from the one before it, and the current version is one of
/// them. Undo publishes the version before the current one and redo the one
/// after it. A commit drops every version after the current one, which an
/// edit after an undo replaces, and adds its own.
///
/// Each version but the current one keeps alive the bytes that it holds and
/// its neighbor toward the current version does not share: the chunks and
/// branches that the transaction between them copied, the objects and
/// trailers that it replaced or wrote, and each retained version's record,
/// change set, and history entry. Parsed-value caches and unused deque
/// capacity are not counted. When these bytes pass the limit, the
/// oldest versions go first, then the newest: pylopdf's `doc.history` drops
/// its steps so (yhay81/pyropdf#12 decides the limit and what it counts).
#[derive(Debug)]
struct History {
    steps: VecDeque<Step>,
    /// The index of the current version in `steps`.
    position: usize,
    /// The bytes that the versions keep alive.
    size: u64,
    limit: Option<u64>,
}

/// A version of the history, with what the transaction that made it from
/// the version before it changed in memory.
#[derive(Debug)]
struct Step {
    version: Arc<Version>,
    /// The bytes that the version holds and the version before it does not
    /// share, or 0 for the oldest version.
    added: u64,
    /// The bytes that the version before it holds and this one does not
    /// share, or 0 for the oldest version.
    released: u64,
}

impl History {
    /// Returns the history of a document whose first version is `first`.
    fn new(first: Arc<Version>, limit: Option<u64>) -> Self {
        Self {
            steps: VecDeque::from([Step {
                version: first,
                added: 0,
                released: 0,
            }]),
            position: 0,
            size: 0,
            limit,
        }
    }

    /// Returns the current version.
    fn current(&self) -> &Arc<Version> {
        &self.steps[self.position].version
    }

    /// Makes `version`, which a transaction made from the current version,
    /// current, after dropping the versions after the current one.
    fn commit(&mut self, version: Arc<Version>, added: u64, released: u64) {
        for step in self.steps.drain(self.position + 1..) {
            self.size = self.size.saturating_sub(step.added);
        }
        // Each non-current version owns its record and one history entry,
        // even when a transaction changed only the trailer.
        let added = added
            .saturating_add(version.record_size())
            .saturating_add(size_of::<Step>() as u64);
        let released = released
            .saturating_add(self.current().record_size())
            .saturating_add(size_of::<Step>() as u64);
        #[expect(
            clippy::disallowed_methods,
            reason = "each commit adds one history entry, counted with the version against the history limit"
        )]
        self.steps.push_back(Step {
            version,
            added,
            released,
        });
        self.position += 1;
        self.size = self.size.saturating_add(released);
        self.fit();
    }

    /// Makes the version before the current one current, and returns it.
    fn undo(&mut self) -> Option<Arc<Version>> {
        let current = &self.steps[self.position];
        if self.position == 0 {
            return None;
        }
        // The current version now keeps alive what it added, and the version
        // before it no longer what it replaced.
        self.size = self
            .size
            .saturating_sub(current.released)
            .saturating_add(current.added);
        self.position -= 1;
        let version = Arc::clone(self.current());
        self.fit();
        Some(version)
    }

    /// Makes the version after the current one current, and returns it.
    fn redo(&mut self) -> Option<Arc<Version>> {
        let next = self.steps.get(self.position + 1)?;
        self.size = self
            .size
            .saturating_sub(next.added)
            .saturating_add(next.released);
        self.position += 1;
        let version = Arc::clone(self.current());
        self.fit();
        Some(version)
    }

    /// Sets the limit, and drops versions until the history fits it.
    fn set_limit(&mut self, limit: Option<u64>) {
        self.limit = limit;
        self.fit();
    }

    /// Drops the oldest versions, then the newest, until the history keeps
    /// no more bytes alive than its limit allows.
    fn fit(&mut self) {
        let Some(limit) = self.limit else {
            return;
        };
        while self.size > limit {
            if self.position > 0 {
                self.steps.pop_front();
                self.position -= 1;
                // No version is before the new oldest one any more.
                if let Some(oldest) = self.steps.front_mut() {
                    self.size = self.size.saturating_sub(oldest.released);
                    oldest.added = 0;
                    oldest.released = 0;
                }
            } else if self.steps.len() > 1 {
                if let Some(newest) = self.steps.pop_back() {
                    self.size = self.size.saturating_sub(newest.added);
                }
            } else {
                break;
            }
        }
    }

    /// Returns what the history holds.
    fn status(&self) -> HistoryStatus {
        HistoryStatus {
            undo: self.position,
            redo: self.steps.len() - 1 - self.position,
            size: self.size,
            limit: self.limit,
        }
    }
}
