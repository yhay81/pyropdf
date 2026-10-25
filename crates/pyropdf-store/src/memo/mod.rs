//! Derived results memoized with what computing them read (DESIGN.md
//! section 5 and F4).
//!
//! A computation reads a version through a [`Reads`], which records the
//! object numbers that it asked for, and whether it read the trailer, as a
//! [`ReadSet`]. A [`Memo`] keeps each result with its read set and the
//! version that it was computed on, and reuses it on a later version while
//! the change set between the two versions misses the read set. The key
//! names the kind of result and every setting that changes what it is, so
//! that results with different settings never stand in for one another.
//!
//! The memo remembers a version by a weak reference: it never keeps a
//! version alive (DESIGN.md section 10). A result whose version is gone is
//! computed again.

#[cfg(test)]
mod tests;

use std::collections::HashMap;
use std::hash::Hash;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, PoisonError, Weak};

use crate::budget::{Budget, BudgetVec};
use crate::value::Dict;
use crate::version::{ChangeSet, Object, Version};
use crate::{Error, Result};

/// A version read through a recorder: every object number asked for, found
/// or not, and whether the trailer was read.
pub struct Reads<'a> {
    version: &'a Version,
    budget: &'a Budget,
    objects: BudgetVec<'a, u32>,
    trailer: bool,
}

impl<'a> Reads<'a> {
    /// Returns a recorder of reads of `version`. The recorded numbers grow
    /// through `budget`, which the reads also use.
    #[must_use]
    pub const fn new(version: &'a Version, budget: &'a Budget) -> Self {
        Self {
            version,
            budget,
            objects: BudgetVec::new(budget),
            trailer: false,
        }
    }

    /// Returns the version that the reads read.
    #[must_use]
    pub const fn version(&self) -> &'a Version {
        self.version
    }

    /// Returns the budget of the reads.
    #[must_use]
    pub const fn budget(&self) -> &'a Budget {
        self.budget
    }

    /// Reads object `number`, as [`Version::get`] does, and records it.
    pub fn get(&mut self, number: u32) -> Result<Option<Object>> {
        self.record(number)?;
        self.version.get(number, self.budget)
    }

    /// Reads the generation of object `number`, as [`Version::generation`]
    /// does, and records it.
    pub fn generation(&mut self, number: u32) -> Result<Option<u16>> {
        self.record(number)?;
        Ok(self.version.generation(number))
    }

    /// Reads the trailer and records it.
    pub fn trailer(&mut self) -> &'a Dict {
        self.trailer = true;
        self.version.trailer()
    }

    /// Returns what was read.
    pub fn finish(self) -> Result<ReadSet> {
        let Self {
            mut objects,
            trailer,
            ..
        } = self;
        objects.sort_unstable();
        let mut kept = 0;
        for at in 0..objects.len() {
            if kept == 0 || objects[kept - 1] != objects[at] {
                objects[kept] = objects[at];
                kept += 1;
            }
        }
        objects.truncate(kept);
        Ok(ReadSet {
            objects: objects.into_vec().into_boxed_slice(),
            trailer,
        })
    }

    fn record(&mut self, number: u32) -> Result<()> {
        // Consecutive reads of one object, the common case, count once.
        if self.objects.last() != Some(&number) {
            self.objects.try_push(number)?;
        }
        Ok(())
    }
}

/// What a computation read: object numbers, ascending and distinct, and
/// whether it read the trailer.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ReadSet {
    objects: Box<[u32]>,
    trailer: bool,
}

impl ReadSet {
    /// Returns the object numbers read, ascending.
    #[must_use]
    pub const fn objects(&self) -> &[u32] {
        &self.objects
    }

    /// Returns whether the trailer was read.
    #[must_use]
    pub const fn trailer(&self) -> bool {
        self.trailer
    }

    /// Returns whether `changes` misses everything read, so that a result
    /// computed before the changes is still the result after them. The time
    /// grows with the smaller of the two sets, times the logarithm of the
    /// larger.
    #[must_use]
    pub fn is_disjoint(&self, changes: &ChangeSet) -> bool {
        if self.trailer && changes.trailer() {
            return false;
        }
        let (small, large) = if self.objects.len() <= changes.objects().len() {
            (&*self.objects, changes.objects())
        } else {
            (changes.objects(), &*self.objects)
        };
        small.iter().all(|n| large.binary_search(n).is_err())
    }
}

/// A result and what it depends on.
struct Entry<V> {
    version: Weak<Version>,
    reads: Arc<ReadSet>,
    value: Arc<V>,
    /// When the entry was last used, for eviction.
    used: u64,
}

/// Counts of a memo's lookups, for tests and measurements.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Stats {
    /// Lookups that reused a result.
    pub hits: u64,
    /// Lookups that computed one.
    pub computations: u64,
}

/// A bounded, thread-safe table of derived results, keyed by the kind of
/// result and its settings.
///
/// It holds at most `capacity` results and evicts the least recently used
/// one first. Two threads that miss the same key at once both compute; the
/// later result replaces the earlier.
pub struct Memo<K, V> {
    entries: Mutex<Table<K, V>>,
    capacity: usize,
    hits: AtomicU64,
    computations: AtomicU64,
}

struct Table<K, V> {
    map: HashMap<K, Entry<V>>,
    clock: u64,
}

impl<K: Eq + Hash + Clone, V> Memo<K, V> {
    /// Returns an empty memo of at most `capacity` results; a capacity of 0
    /// keeps none.
    #[must_use]
    pub fn new(capacity: usize) -> Self {
        Self {
            entries: Mutex::new(Table {
                map: HashMap::new(),
                clock: 0,
            }),
            capacity,
            hits: AtomicU64::new(0),
            computations: AtomicU64::new(0),
        }
    }

    /// Returns the result for `key` on `version`: a result computed on an
    /// earlier or later version of the same document whose read set the
    /// change set since misses, or else the result of `compute`, which reads
    /// `version` through the recorder that it is given.
    ///
    /// The lock is not held while computing or comparing versions. A
    /// failed computation keeps nothing.
    pub fn get_or_compute<E: From<Error>>(
        &self,
        key: &K,
        version: &Arc<Version>,
        budget: &Budget,
        compute: impl FnOnce(&mut Reads<'_>) -> std::result::Result<V, E>,
    ) -> std::result::Result<Arc<V>, E> {
        if let Some(value) = self.reuse(key, version, budget)? {
            self.hits.fetch_add(1, Ordering::Relaxed);
            return Ok(value);
        }
        self.computations.fetch_add(1, Ordering::Relaxed);
        let mut reads = Reads::new(version, budget);
        let value = Arc::new(compute(&mut reads)?);
        let reads = Arc::new(reads.finish()?);
        self.store(key, version, reads, Arc::clone(&value));
        Ok(value)
    }

    /// Returns the lookup counts so far.
    #[must_use]
    pub fn stats(&self) -> Stats {
        Stats {
            hits: self.hits.load(Ordering::Relaxed),
            computations: self.computations.load(Ordering::Relaxed),
        }
    }

    /// Returns how many results the memo holds.
    #[must_use]
    pub fn len(&self) -> usize {
        self.lock().map.len()
    }

    /// Returns whether the memo holds no result.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Drops every result.
    pub fn clear(&self) {
        self.lock().map.clear();
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Table<K, V>> {
        self.entries.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Returns a held result that is valid on `version`, and moves the
    /// entry to `version`, which it is now known to be valid on.
    fn reuse(&self, key: &K, version: &Arc<Version>, budget: &Budget) -> Result<Option<Arc<V>>> {
        let (held, reads, value) = {
            let mut table = self.lock();
            let clock = table.tick();
            let Some(entry) = table.map.get_mut(key) else {
                return Ok(None);
            };
            entry.used = clock;
            (
                entry.version.clone(),
                Arc::clone(&entry.reads),
                Arc::clone(&entry.value),
            )
        };
        let Some(held) = held.upgrade() else {
            return Ok(None);
        };
        if !Arc::ptr_eq(&held, version) {
            match held.changes(version, budget)? {
                Some(changes) if reads.is_disjoint(&changes) => {}
                _ => return Ok(None),
            }
            if let Some(entry) = self.lock().map.get_mut(key)
                && Arc::ptr_eq(&entry.value, &value)
            {
                entry.version = Arc::downgrade(version);
            }
        }
        Ok(Some(value))
    }

    fn store(&self, key: &K, version: &Arc<Version>, reads: Arc<ReadSet>, value: Arc<V>) {
        if self.capacity == 0 {
            return;
        }
        let mut table = self.lock();
        let used = table.tick();
        if !table.map.contains_key(key) && table.map.len() >= self.capacity {
            let oldest = table
                .map
                .iter()
                .min_by_key(|(_, entry)| entry.used)
                .map(|(key, _)| key.clone());
            if let Some(oldest) = oldest {
                table.map.remove(&oldest);
            }
        }
        let entry = Entry {
            version: Arc::downgrade(version),
            reads,
            value,
            used,
        };
        #[expect(
            clippy::disallowed_methods,
            reason = "the map holds at most `capacity` entries, which the caller sets"
        )]
        table.map.insert(key.clone(), entry);
    }
}

impl<K, V> Table<K, V> {
    const fn tick(&mut self) -> u64 {
        self.clock += 1;
        self.clock
    }
}
