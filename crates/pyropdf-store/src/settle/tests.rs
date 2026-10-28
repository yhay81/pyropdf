//! Settling at the end of a transaction, before a read, and before a save.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use super::*;
use crate::budget::{DocumentLimits, LimitCode};
use crate::value::{Dict, Name, Reference, Value};
use crate::version::Stale;
use crate::version::{Document, MemoryBase, Version};
use crate::{ErrorKind, LimitExceeded};

/// Object that every test settler counts its calls in.
const COUNTS: u32 = 1;

fn document(settlers: Settlers) -> Document {
    let trailer: Dict = [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
        .into_iter()
        .collect();
    let document = Document::new(Arc::new(MemoryBase::new(trailer)), None);
    document.set_settlers(settlers);
    document
}

fn integer(version: &Version, number: u32) -> Option<i64> {
    match version
        .get(number, &Budget::default())
        .unwrap()?
        .value()
        .as_ref()
    {
        Value::Integer(value) => Some(*value),
        other => panic!("object {number} is {other:?}"),
    }
}

/// Settles a structure by writing, as each marked object's number plus
/// 1000, how many calls settled it, and records the numbers of each call.
/// It marks `then` of the same objects stale, if given.
#[derive(Default)]
struct Counting {
    calls: AtomicUsize,
    numbers: Mutex<Vec<Vec<u32>>>,
    then: Option<Structure>,
}

impl Counting {
    fn then(structure: Structure) -> Self {
        Self {
            then: Some(structure),
            ..Self::default()
        }
    }

    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }
}

impl Settler for Counting {
    fn settle(
        &self,
        transaction: &mut Transaction,
        numbers: &[u32],
        budget: &Budget,
    ) -> Result<()> {
        let calls = self.calls.fetch_add(1, Ordering::SeqCst) + 1;
        self.numbers.lock().unwrap().push(numbers.to_vec());
        for &number in numbers {
            transaction.write(number + 1000, &Value::Integer(calls as i64), budget)?;
            if let Some(then) = self.then {
                transaction.mark(then, number, budget)?;
            }
        }
        transaction.write(COUNTS, &Value::Integer(calls as i64), budget)?;
        Ok(())
    }
}

#[test]
fn three_writes_that_mark_one_structure_settle_it_once_at_the_commit() {
    let appearance = Arc::new(Counting::default());
    let document = document(Settlers::default().with(Structure::Appearance, appearance.clone()));
    let budget = Budget::default();
    let parent = document.current();

    let mut transaction = document.begin();
    for value in 1..=3 {
        transaction
            .write(7, &Value::Integer(value), &budget)
            .unwrap();
        transaction.mark(Structure::Appearance, 7, &budget).unwrap();
    }
    transaction.mark(Structure::Appearance, 5, &budget).unwrap();
    assert!(transaction.is_stale(Structure::Appearance, 7));
    assert_eq!(appearance.calls(), 0);
    let settled = transaction.commit(&budget).unwrap();

    assert_eq!(appearance.calls(), 1);
    assert_eq!(*appearance.numbers.lock().unwrap(), [vec![5, 7]]);
    assert_eq!(integer(&settled, 1007), Some(1));
    assert!(settled.stale().next().is_none());
    // Settling is part of the transaction's step.
    assert!(Arc::ptr_eq(&document.undo().unwrap(), &parent));
    assert!(Arc::ptr_eq(&document.redo().unwrap(), &settled));
    assert_eq!(document.history().undo, 1);
}

#[test]
fn a_read_inside_a_transaction_settles_what_it_depends_on_first() {
    let appearance = Arc::new(Counting::default());
    let limits = Arc::new(Counting::default());
    let document = document(
        Settlers::default()
            .with(Structure::Appearance, appearance.clone())
            .with(Structure::TreeLimits, limits.clone()),
    );
    let budget = Budget::default();

    let mut transaction = document.begin();
    transaction.write(7, &Value::Integer(1), &budget).unwrap();
    transaction.mark(Structure::Appearance, 7, &budget).unwrap();
    transaction.mark(Structure::TreeLimits, 8, &budget).unwrap();
    transaction
        .settle(Scope::One(Structure::Appearance), &budget)
        .unwrap();

    assert_eq!(integer(transaction.version(), 1007), Some(1));
    assert!(!transaction.is_stale(Structure::Appearance, 7));
    assert!(transaction.is_stale(Structure::TreeLimits, 8));
    transaction.commit(&budget).unwrap();
    // The commit settles only what is still stale.
    assert_eq!((appearance.calls(), limits.calls()), (1, 1));
}

#[test]
fn font_subsets_settle_only_at_a_save() {
    let subsets = Arc::new(Counting::default());
    let document = document(Settlers::default().with(Structure::FontSubset, subsets.clone()));
    let budget = Budget::default();

    let mut transaction = document.begin();
    transaction.write(9, &Value::Integer(1), &budget).unwrap();
    transaction.mark(Structure::FontSubset, 9, &budget).unwrap();
    transaction
        .settle(Scope::One(Structure::FontSubset), &budget)
        .unwrap();
    let current = transaction.commit(&budget).unwrap();

    assert_eq!(subsets.calls(), 0);
    assert!(current.is_stale(Structure::FontSubset, 9));
    // A save settles in a transaction that it drops: the current version
    // keeps its provisional font, and the history gains no step.
    let mut save = document.begin();
    save.settle(Scope::Save, &budget).unwrap();
    let saved = save.snapshot();
    drop(save);
    assert_eq!(subsets.calls(), 1);
    assert_eq!(integer(&saved, 1009), Some(1));
    assert!(saved.stale().next().is_none());
    assert!(Arc::ptr_eq(&document.current(), &current));
    assert_eq!(document.history().undo, 1);
    // Undo restores the version before the font, without its mark.
    assert!(document.undo().unwrap().stale().next().is_none());
    assert!(document.redo().unwrap().is_stale(Structure::FontSubset, 9));
}

#[test]
fn a_settler_that_marks_another_structure_settles_it_in_the_same_commit() {
    let appearance = Arc::new(Counting::then(Structure::TreeLimits));
    let limits = Arc::new(Counting::then(Structure::FontSubset));
    let document = document(
        Settlers::default()
            .with(Structure::Appearance, appearance.clone())
            .with(Structure::TreeLimits, limits.clone()),
    );
    let budget = Budget::default();

    let mut transaction = document.begin();
    transaction.write(7, &Value::Integer(1), &budget).unwrap();
    transaction.mark(Structure::TreeLimits, 6, &budget).unwrap();
    transaction.mark(Structure::Appearance, 7, &budget).unwrap();
    let settled = transaction.commit(&budget).unwrap();

    // Appearances settle before the limits that they mark, so one call
    // settles the limits that were stale and those the appearance marked.
    assert_eq!(*limits.numbers.lock().unwrap(), [vec![6, 7]]);
    assert_eq!(appearance.calls(), 1);
    // Font subsets wait for a save, and no settler is registered for them.
    let fonts: Vec<_> = settled
        .stale()
        .map(|mark| (mark.structure, mark.number))
        .collect();
    assert_eq!(
        fonts,
        [(Structure::FontSubset, 6), (Structure::FontSubset, 7)]
    );
}

#[test]
fn settlers_that_keep_marking_each_other_refuse_the_commit() {
    let appearance = Arc::new(Counting::then(Structure::TreeLimits));
    let limits = Arc::new(Counting::then(Structure::Appearance));
    let document = document(
        Settlers::default()
            .with(Structure::Appearance, appearance.clone())
            .with(Structure::TreeLimits, limits),
    );
    let budget = Budget::default();
    let parent = document.current();

    let mut transaction = document.begin();
    transaction.write(7, &Value::Integer(1), &budget).unwrap();
    transaction.mark(Structure::Appearance, 7, &budget).unwrap();
    let error = transaction.commit(&budget).unwrap_err();

    assert!(matches!(error.kind(), ErrorKind::Malformed(text) if text.contains("did not finish")));
    assert_eq!(appearance.calls(), MAX_ROUNDS);
    assert!(Arc::ptr_eq(&document.current(), &parent));
}

#[test]
fn a_mark_counts_against_the_memory_budget() {
    let document = document(Settlers::default());
    let budget = Budget::new(&DocumentLimits::default())
        .with_limit(LimitCode::MemorySize, Some(Stale::FIRST_MARK_SIZE));

    let mut transaction = document.begin();
    transaction.mark(Structure::Appearance, 1, &budget).unwrap();
    transaction.mark(Structure::Appearance, 1, &budget).unwrap();
    let error = transaction
        .mark(Structure::Appearance, 16, &budget)
        .unwrap_err();

    assert_eq!(
        error.limit().map(LimitExceeded::code),
        Some(LimitCode::MemorySize)
    );
    assert!(!transaction.is_stale(Structure::Appearance, 16));
}

#[test]
fn a_mark_only_commit_keeps_the_mark_in_history() {
    let document = document(Settlers::default());
    let parent = document.current();
    let budget = Budget::default();
    let mut transaction = document.begin();
    transaction.mark(Structure::FontSubset, 9, &budget).unwrap();
    let marked = transaction.commit(&budget).unwrap();
    assert!(marked.is_stale(Structure::FontSubset, 9));
    assert!(Arc::ptr_eq(&document.undo().unwrap(), &parent));
    assert!(Arc::ptr_eq(&document.redo().unwrap(), &marked));
    // A repeated mark is a no-op and preserves the same version.
    let mut transaction = document.begin();
    transaction.mark(Structure::FontSubset, 9, &budget).unwrap();
    assert!(Arc::ptr_eq(&transaction.commit(&budget).unwrap(), &marked));
}

struct FailsOnce(AtomicUsize);

impl Settler for FailsOnce {
    fn settle(
        &self,
        transaction: &mut Transaction,
        numbers: &[u32],
        budget: &Budget,
    ) -> Result<()> {
        transaction.write(1007, &Value::Integer(42), budget)?;
        transaction.mark(Structure::FontSubset, 7, budget)?;
        assert_eq!(numbers, [7]);
        if self.0.fetch_add(1, Ordering::SeqCst) == 0 {
            return Err(Error::malformed("a recoverable settler failure"));
        }
        Ok(())
    }
}

#[test]
fn a_failed_settle_restores_writes_and_marks_for_a_retry() {
    let document = document(Settlers::default().with(
        Structure::Appearance,
        Arc::new(FailsOnce(AtomicUsize::new(0))),
    ));
    let budget = Budget::default();
    let mut transaction = document.begin();
    transaction.write(7, &Value::Integer(1), &budget).unwrap();
    transaction.mark(Structure::Appearance, 7, &budget).unwrap();
    assert!(transaction.settle(Scope::Commit, &budget).is_err());
    assert_eq!(integer(transaction.version(), 7), Some(1));
    assert_eq!(integer(transaction.version(), 1007), None);
    assert!(transaction.is_stale(Structure::Appearance, 7));
    assert!(!transaction.is_stale(Structure::FontSubset, 7));
    transaction.settle(Scope::Commit, &budget).unwrap();
    assert_eq!(integer(transaction.version(), 1007), Some(42));
    assert!(!transaction.is_stale(Structure::Appearance, 7));
    assert!(transaction.is_stale(Structure::FontSubset, 7));
}

#[test]
fn a_refused_number_buffer_keeps_marks_without_calling_the_settler() {
    let appearance = Arc::new(Counting::default());
    let document = document(Settlers::default().with(Structure::Appearance, appearance.clone()));
    let mut transaction = document.begin();
    transaction
        .mark(Structure::Appearance, 7, &Budget::default())
        .unwrap();
    let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(0));
    let error = transaction.settle(Scope::Commit, &budget).unwrap_err();
    assert_eq!(
        error.limit().map(LimitExceeded::code),
        Some(LimitCode::MemorySize)
    );
    assert_eq!(appearance.calls(), 0);
    assert!(transaction.is_stale(Structure::Appearance, 7));
    transaction
        .settle(Scope::Commit, &Budget::default())
        .unwrap();
    assert_eq!(appearance.calls(), 1);
}

#[test]
fn snapshots_share_marks_and_new_marks_charge_only_their_table_path() {
    let document = document(Settlers::default());
    let mut transaction = document.begin();
    let budget = Budget::default();
    for number in 0..10_000 {
        transaction
            .mark(Structure::FontSubset, number, &budget)
            .unwrap();
    }
    let snapshot = transaction.snapshot();
    let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(4096));
    transaction
        .mark(Structure::FontSubset, 10_000, &budget)
        .unwrap();
    assert!(budget.used(LimitCode::MemorySize) >= Stale::FIRST_MARK_SIZE);
    assert!(!snapshot.is_stale(Structure::FontSubset, 10_000));
    assert!(snapshot.is_stale(Structure::FontSubset, 9999));
    assert!(transaction.is_stale(Structure::FontSubset, 10_000));
}

struct NoWrites;

impl Settler for NoWrites {
    fn settle(&self, _: &mut Transaction, _: &[u32], _: &Budget) -> Result<()> {
        Ok(())
    }
}

#[test]
fn clearing_only_marks_commits_and_history_counts_the_retained_tables() {
    let document = document(Settlers::default());
    let budget = Budget::default();
    let mut transaction = document.begin();
    for number in 0..1000 {
        transaction
            .mark(Structure::Appearance, number * 16, &budget)
            .unwrap();
    }
    let marked = transaction.commit(&budget).unwrap();
    document.set_settlers(Settlers::default().with(Structure::Appearance, Arc::new(NoWrites)));
    let settled = document.begin().commit(&budget).unwrap();
    assert!(!settled.has_stale());
    // The old marked version retains at least 1000 distinct chunks, even
    // though neither commit changed an object or the trailer.
    assert!(document.history().size > 16_000);
    assert!(Arc::ptr_eq(&document.undo().unwrap(), &marked));
    assert_eq!(document.current().stale().count(), 1000);
    assert!(Arc::ptr_eq(&document.redo().unwrap(), &settled));
}
