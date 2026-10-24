//! Versions read back what each transaction wrote, change sets are exact, a
//! dropped transaction changes nothing, undo and redo publish earlier
//! versions again within the history's limit, and readers on other threads
//! see only complete versions (DESIGN.md sections 4.1 and 4.6).

use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, mpsc};
use std::thread;
use std::time::Duration;

use proptest::prelude::*;

use super::table::Table;
use super::*;
use crate::budget::CancellationToken;
use crate::value::{Array, Name, PdfString, Reference, Stream};
use crate::{Error, ErrorKind, write};

/// The objects of the test base, with their generations. The value of each
/// is its number.
const BASE: [(u32, u16); 7] = [
    (1, 0),
    (2, 0),
    (3, 7),
    (5, 0),
    (8, 65_534),
    (9, 65_535),
    (17, 0),
];

/// The number that the first new object of the test base takes.
const BASE_NEXT: u64 = 18;

/// The object numbers that the property tests write and free: those of the
/// base, others in its chunks and beyond them, numbers at the edges of
/// chunks and branches, and numbers that need every level of branches.
const NUMBERS: [u32; 20] = [
    1,
    2,
    3,
    4,
    5,
    8,
    9,
    15,
    16,
    17,
    18,
    511,
    512,
    513,
    16_383,
    16_384,
    1_000_000,
    1 << 29,
    u32::MAX - 1,
    u32::MAX,
];

/// How many values [`values`] returns.
const VALUES: usize = 9;

/// Returns values of every kind, which the tests write.
fn values() -> [Value; VALUES] {
    [
        Value::Null,
        Value::Integer(-7),
        Value::from(0.1),
        Value::Name(Name::from("Pooled")),
        Value::String(PdfString::literal(b"(a) \\ b".to_vec())),
        Value::String(PdfString::hexadecimal(vec![0, 255, 16])),
        Value::Array(Array::from(vec![
            Value::Integer(1),
            Value::Reference(Reference::new(2, 0)),
            Value::Name(Name::from("A")),
        ])),
        Value::Dict(
            [
                (Name::from("Type"), Value::Name(Name::from("Test"))),
                (Name::from("Kids"), Value::Array(Array::new())),
            ]
            .into_iter()
            .collect(),
        ),
        Value::from(Stream::new(
            [(Name::from("Subtype"), Value::Name(Name::from("Plain")))]
                .into_iter()
                .collect(),
            Bytes::from(b"stream data\n".to_vec()),
        )),
    ]
}

/// Returns the bytes that the writer writes for `value`.
fn bytes_of(value: &Value) -> Bytes {
    let budget = Budget::default();
    let mut out = BudgetVec::new(&budget);
    write::value(&mut out, value).unwrap();
    Bytes::from(out.into_vec())
}

/// Returns a trailer that names the catalog and carries `marker`.
fn trailer(marker: i64) -> Dict {
    [
        (Name::from("Root"), Value::Reference(Reference::new(1, 0))),
        (Name::from("Marker"), Value::Integer(marker)),
    ]
    .into_iter()
    .collect()
}

/// Returns the test base, whose trailer carries the marker -1.
fn base() -> MemoryBase {
    let mut base = MemoryBase::new(trailer(-1));
    for (number, generation) in BASE {
        base.insert(
            Reference::new(number, generation),
            bytes_of(&Value::Integer(number.into())),
        );
    }
    base
}

/// Returns a document over the test base.
fn document(history_limit: Option<u64>) -> Document {
    Document::new(Arc::new(base()), history_limit)
}

/// Returns the value of object `number` in `version`, if it is in use.
fn value_of(version: &Version, number: u32) -> Option<Value> {
    version
        .get(number, &Budget::default())
        .unwrap()
        .map(|object| (**object.value()).clone())
}

/// Turns an error of the store into a failure of a property test.
fn fail(error: Error) -> TestCaseError {
    TestCaseError::fail(error.to_string())
}

/// What the model holds for an object number that a transaction wrote.
#[derive(Clone, Debug, PartialEq)]
enum Entry {
    /// An object, with the write that made it: no two writes are the same.
    Written {
        generation: u16,
        value: Value,
        write: u64,
    },
    /// A free entry, with the generation of the number's next use.
    Free(u16),
}

/// The model of a version.
#[derive(Clone, Debug)]
struct State {
    entries: BTreeMap<u32, Entry>,
    /// The trailer that a transaction wrote, with the write that did.
    trailer: Option<(u64, Dict)>,
    next: u64,
}

impl State {
    /// Returns the model of the first version over the test base.
    const fn first() -> Self {
        Self {
            entries: BTreeMap::new(),
            trailer: None,
            next: BASE_NEXT,
        }
    }

    /// Returns the generation of object `number` in the test base.
    fn base_generation(number: u32) -> Option<u16> {
        BASE.iter()
            .find(|&&(base, _)| base == number)
            .map(|&(_, generation)| generation)
    }

    fn generation(&self, number: u32) -> Option<u16> {
        match self.entries.get(&number) {
            Some(Entry::Written { generation, .. }) => Some(*generation),
            Some(Entry::Free(_)) => None,
            None => Self::base_generation(number),
        }
    }

    fn read(&self, number: u32) -> Option<(u16, Value)> {
        match self.entries.get(&number) {
            Some(Entry::Written {
                generation, value, ..
            }) => Some((*generation, value.clone())),
            Some(Entry::Free(_)) => None,
            None => Self::base_generation(number)
                .map(|generation| (generation, Value::Integer(number.into()))),
        }
    }

    /// Writes as a transaction does, or returns `None` where it refuses.
    fn write(&mut self, number: u32, value: &Value, write: u64) -> Option<Reference> {
        let generation = match self.entries.get(&number) {
            _ if number == 0 => return None,
            Some(Entry::Written { generation, .. }) => *generation,
            Some(Entry::Free(u16::MAX)) => return None,
            Some(Entry::Free(generation)) => *generation,
            None => Self::base_generation(number).unwrap_or(0),
        };
        self.entries.insert(
            number,
            Entry::Written {
                generation,
                value: value.clone(),
                write,
            },
        );
        self.next = self.next.max(u64::from(number) + 1);
        Some(Reference::new(number, generation))
    }

    fn free(&mut self, number: u32) -> bool {
        let Some(generation) = self.generation(number) else {
            return false;
        };
        self.entries
            .insert(number, Entry::Free(generation.saturating_add(1)));
        true
    }

    fn allocate(&mut self) -> Option<Reference> {
        let number = u32::try_from(self.next).ok()?;
        self.next += 1;
        Some(Reference::new(number, 0))
    }

    /// Returns the change set between two models, as versions compute it.
    fn changes(&self, other: &Self) -> (Vec<u32>, bool) {
        let numbers: BTreeSet<u32> = self
            .entries
            .keys()
            .chain(other.entries.keys())
            .copied()
            .collect();
        let objects = numbers
            .into_iter()
            .filter(|number| self.entries.get(number) != other.entries.get(number))
            .collect();
        let write = |state: &Self| state.trailer.as_ref().map(|&(write, _)| write);
        (objects, write(self) != write(other))
    }
}

/// Checks that `version` reads as `state` does: every object of `numbers`,
/// the slots, the trailer, and the next number.
fn check(version: &Version, state: &State, numbers: &BTreeSet<u32>) -> Result<(), TestCaseError> {
    let budget = Budget::default();
    for &number in numbers {
        let read = version.get(number, &budget).map_err(fail)?;
        prop_assert_eq!(
            read.map(|object| (object.generation(), (**object.value()).clone())),
            state.read(number),
            "object {}",
            number
        );
        prop_assert_eq!(
            version.generation(number),
            state.generation(number),
            "object {}",
            number
        );
    }
    let slots: Vec<(u32, bool, u16)> = version
        .slots()
        .map(|(number, slot)| match slot {
            Slot::Written(object) => (number, true, object.generation()),
            Slot::Free(generation) => (number, false, *generation),
        })
        .collect();
    let entries: Vec<(u32, bool, u16)> = state
        .entries
        .iter()
        .map(|(&number, entry)| match entry {
            Entry::Written { generation, .. } => (number, true, *generation),
            Entry::Free(generation) => (number, false, *generation),
        })
        .collect();
    prop_assert_eq!(slots, entries);
    let expected = state
        .trailer
        .as_ref()
        .map_or_else(|| trailer(-1), |(_, dict)| dict.clone());
    prop_assert_eq!(version.trailer(), &expected);
    prop_assert_eq!(version.next_number(), state.next);
    Ok(())
}

/// What a property test does next.
#[derive(Clone, Debug)]
enum Op {
    Begin,
    Write { number: usize, value: usize },
    WriteNew { value: usize },
    Free { number: usize },
    Trailer,
    Commit,
    Drop,
    Undo,
    Redo,
}

fn op() -> impl Strategy<Value = Op> {
    prop_oneof![
        3 => Just(Op::Begin),
        6 => (0..NUMBERS.len(), 0..VALUES).prop_map(|(number, value)| Op::Write { number, value }),
        2 => (0..VALUES).prop_map(|value| Op::WriteNew { value }),
        3 => (0..NUMBERS.len()).prop_map(|number| Op::Free { number }),
        1 => Just(Op::Trailer),
        3 => Just(Op::Commit),
        1 => Just(Op::Drop),
        2 => Just(Op::Undo),
        2 => Just(Op::Redo),
    ]
}

/// A transaction in progress, and the model of what it has written.
struct Open {
    transaction: Transaction,
    state: State,
}

/// Runs `ops` on a document over the test base, and checks every version
/// against its model after each of them.
fn run(ops: &[Op]) -> Result<(), TestCaseError> {
    let budget = Budget::default();
    let values = values();
    let document = document(None);
    // Every version that a commit published, the first included, with its
    // model.
    let mut published = vec![(document.current(), State::first())];
    // The history, as indexes into `published`, oldest first, and the
    // index of the current version.
    let mut history = vec![0];
    let mut position = 0;
    let mut open: Option<Open> = None;
    let mut writes = 0;
    let mut numbers: BTreeSet<u32> = NUMBERS.into_iter().chain([0, 6, 7, 10, 19]).collect();
    for op in ops {
        let current = history[position];
        match op {
            Op::Begin => {
                if open.is_some() {
                    prop_assert!(document.try_begin().is_none());
                } else {
                    let transaction = document
                        .try_begin()
                        .ok_or_else(|| TestCaseError::fail("the writer lock is held"))?;
                    prop_assert!(Arc::ptr_eq(transaction.parent(), &published[current].0));
                    let state = published[current].1.clone();
                    open = Some(Open { transaction, state });
                }
            }
            Op::Write { number, value } => {
                if let Some(open) = &mut open {
                    writes += 1;
                    let number = NUMBERS[*number];
                    let expected = open.state.write(number, &values[*value], writes);
                    match open.transaction.write(number, &values[*value], &budget) {
                        Ok(reference) => prop_assert_eq!(Some(reference), expected),
                        Err(error) => {
                            prop_assert!(matches!(error.kind(), ErrorKind::Malformed(_)));
                            prop_assert_eq!(expected, None);
                        }
                    }
                }
            }
            Op::WriteNew { value } => {
                if let Some(open) = &mut open {
                    writes += 1;
                    let expected = open.state.allocate();
                    let allocated = open.transaction.allocate().ok();
                    prop_assert_eq!(allocated, expected);
                    if let Some(reference) = allocated {
                        numbers.insert(reference.number());
                        let expected =
                            open.state
                                .write(reference.number(), &values[*value], writes);
                        let written = open
                            .transaction
                            .write(reference.number(), &values[*value], &budget)
                            .map_err(fail)?;
                        prop_assert_eq!(Some(written), expected);
                    }
                }
            }
            Op::Free { number } => {
                if let Some(open) = &mut open {
                    let number = NUMBERS[*number];
                    let freed = open.transaction.free(number, &budget).map_err(fail)?;
                    prop_assert_eq!(freed, open.state.free(number));
                }
            }
            Op::Trailer => {
                if let Some(open) = &mut open {
                    writes += 1;
                    let dict = trailer(i64::try_from(writes).unwrap());
                    open.state.trailer = Some((writes, dict.clone()));
                    open.transaction.set_trailer(dict);
                }
            }
            Op::Commit => {
                if let Some(Open { transaction, state }) = open.take() {
                    let parent = Arc::clone(transaction.parent());
                    let version = transaction.commit(&budget).map_err(fail)?;
                    let (objects, trailer) = published[current].1.changes(&state);
                    if objects.is_empty() && !trailer {
                        prop_assert!(Arc::ptr_eq(&version, &parent));
                    } else {
                        prop_assert_eq!(version.written().objects(), &objects[..]);
                        prop_assert_eq!(version.written().trailer(), trailer);
                        published.push((Arc::clone(&version), state));
                        history.truncate(position + 1);
                        history.push(published.len() - 1);
                        position += 1;
                    }
                }
            }
            Op::Drop => {
                if let Some(Open { transaction, .. }) = open.take() {
                    drop(transaction);
                    prop_assert!(document.try_begin().is_some());
                }
            }
            // Undo and redo would wait for an open transaction.
            Op::Undo if open.is_none() => {
                let undone = document.undo();
                if position > 0 {
                    position -= 1;
                    let expected = &published[history[position]].0;
                    prop_assert!(undone.is_some_and(|version| Arc::ptr_eq(&version, expected)));
                } else {
                    prop_assert!(undone.is_none());
                }
            }
            Op::Redo if open.is_none() => {
                let redone = document.redo();
                if position + 1 < history.len() {
                    position += 1;
                    let expected = &published[history[position]].0;
                    prop_assert!(redone.is_some_and(|version| Arc::ptr_eq(&version, expected)));
                } else {
                    prop_assert!(redone.is_none());
                }
            }
            Op::Undo | Op::Redo => {}
        }
        let (version, state) = &published[history[position]];
        prop_assert!(Arc::ptr_eq(&document.current(), version));
        check(version, state, &numbers)?;
        if let Some(open) = &open {
            check(open.transaction.version(), &open.state, &numbers)?;
        }
        let status = document.history();
        prop_assert_eq!(
            (status.undo, status.redo),
            (position, history.len() - 1 - position)
        );
    }
    // Every version still reads as it did, and the change set between any
    // two is exact.
    for (version, state) in &published {
        check(version, state, &numbers)?;
        for (other, other_state) in &published {
            let changes = version
                .changes(other, &budget)
                .map_err(fail)?
                .ok_or_else(|| TestCaseError::fail("the versions are of different documents"))?;
            let (objects, trailer) = state.changes(other_state);
            prop_assert_eq!(changes.objects(), &objects[..]);
            prop_assert_eq!(changes.trailer(), trailer);
            for number in &numbers {
                prop_assert_eq!(changes.contains(*number), objects.contains(number));
            }
        }
    }
    Ok(())
}

proptest! {
    #[test]
    fn random_transactions_keep_the_invariants_of_versions(
        ops in proptest::collection::vec(op(), 1..64),
    ) {
        run(&ops)?;
    }
}

/// What the history test does next.
#[derive(Clone, Debug)]
enum Step {
    /// A transaction that writes values of these lengths to these numbers.
    Commit(Vec<(u32, usize)>),
    Undo,
    Redo,
    Limit(Option<u64>),
}

fn step() -> impl Strategy<Value = Step> {
    let write = (
        prop_oneof![0..40_u32, Just(1_000), Just(u32::MAX)],
        0..200_usize,
    );
    let limit = prop_oneof![
        Just(None),
        Just(Some(0)),
        Just(Some(1_000)),
        Just(Some(4_000)),
        Just(Some(20_000)),
    ];
    prop_oneof![
        4 => proptest::collection::vec(write, 1..6).prop_map(Step::Commit),
        2 => Just(Step::Undo),
        2 => Just(Step::Redo),
        1 => limit.prop_map(Step::Limit),
    ]
}

/// The model of a history: its versions with the bytes that each holds and
/// the one before it does not share, and the bytes that the one before it
/// holds and it does not share; the index of the current version; and the
/// limit.
struct Line {
    versions: Vec<(Arc<Version>, u64, u64)>,
    position: usize,
    limit: Option<u64>,
}

impl Line {
    /// Returns the bytes that the versions but the current one keep alive.
    fn size(&self) -> u64 {
        let (older, newer) = self.versions.split_at(self.position + 1);
        older[1..]
            .iter()
            .map(|&(_, _, released)| released)
            .sum::<u64>()
            + newer.iter().map(|&(_, added, _)| added).sum::<u64>()
    }

    /// Drops the oldest versions, then the newest, until the size fits the
    /// limit.
    fn fit(&mut self) {
        while let Some(limit) = self.limit
            && self.size() > limit
        {
            if self.position > 0 {
                self.versions.remove(0);
                self.versions[0].1 = 0;
                self.versions[0].2 = 0;
                self.position -= 1;
            } else if self.versions.len() > 1 {
                self.versions.pop();
            } else {
                break;
            }
        }
    }
}

proptest! {
    #[test]
    fn the_history_keeps_what_its_limit_allows(
        limit in prop_oneof![
            Just(None),
            Just(Some(0)),
            Just(Some(2_000)),
            Just(Some(10_000)),
        ],
        steps in proptest::collection::vec(step(), 1..40),
    ) {
        let budget = Budget::default();
        let document = Document::new(Arc::new(MemoryBase::default()), limit);
        let mut line = Line {
            versions: vec![(document.current(), 0, 0)],
            position: 0,
            limit,
        };
        for step in &steps {
            match step {
                Step::Commit(writes) => {
                    let mut transaction = document.begin();
                    for &(number, length) in writes {
                        let value = Value::String(PdfString::literal(vec![b'x'; length]));
                        transaction.write(number.max(1), &value, &budget).map_err(fail)?;
                    }
                    let parent = Arc::clone(transaction.parent());
                    let version = transaction.commit(&budget).map_err(fail)?;
                    let difference = difference(&parent, &version, &budget)
                        .map_err(|refusal| fail(refusal.into()))?;
                    line.versions.truncate(line.position + 1);
                    line.versions.push((version, difference.second, difference.first));
                    line.position += 1;
                }
                Step::Undo => {
                    let undone = document.undo();
                    prop_assert_eq!(undone.is_some(), line.position > 0);
                    line.position = line.position.saturating_sub(1);
                }
                Step::Redo => {
                    let redone = document.redo();
                    prop_assert_eq!(redone.is_some(), line.position + 1 < line.versions.len());
                    line.position = (line.position + 1).min(line.versions.len() - 1);
                }
                Step::Limit(limit) => {
                    document.set_history_limit(*limit);
                    line.limit = *limit;
                }
            }
            line.fit();
            let (versions, position) = document.history_versions();
            prop_assert_eq!(position, line.position);
            prop_assert_eq!(versions.len(), line.versions.len());
            for (version, (expected, ..)) in versions.iter().zip(&line.versions) {
                prop_assert!(Arc::ptr_eq(version, expected));
            }
            prop_assert!(Arc::ptr_eq(&document.current(), &versions[position]));
            let status = document.history();
            prop_assert_eq!(status.size, line.size());
            prop_assert!(line.limit.is_none_or(|limit| status.size <= limit));
            prop_assert_eq!(
                (status.undo, status.redo, status.limit),
                (position, versions.len() - 1 - position, line.limit)
            );
        }
    }
}

#[test]
fn a_transaction_reads_its_writes_and_publishes_them_when_it_commits() {
    let budget = Budget::default();
    let document = document(None);
    let first = document.current();
    let mut transaction = document.begin();
    assert!(Arc::ptr_eq(transaction.parent(), &first));
    assert_eq!(
        transaction.write(2, &Value::Integer(20), &budget),
        Ok(Reference::new(2, 0))
    );
    assert_eq!(value_of(transaction.version(), 2), Some(Value::Integer(20)));
    // Nothing is published before the commit.
    assert_eq!(value_of(&document.current(), 2), Some(Value::Integer(2)));
    assert!(Arc::ptr_eq(&document.current(), &first));
    let version = transaction.commit(&budget).unwrap();
    assert!(Arc::ptr_eq(&document.current(), &version));
    assert_eq!(value_of(&version, 2), Some(Value::Integer(20)));
    assert_eq!(version.written().objects(), [2]);
    assert!(!version.written().trailer());
    // The version before keeps what it held.
    assert_eq!(value_of(&first, 2), Some(Value::Integer(2)));
    assert_eq!(
        first.changes(&version, &budget).unwrap(),
        Some(version.written().clone())
    );
}

#[test]
fn generations_follow_iso_32000_2() {
    let budget = Budget::default();
    let document = document(None);
    let mut transaction = document.begin();
    let value = Value::Integer(0);
    // An object in use keeps its generation.
    assert_eq!(
        transaction.write(3, &value, &budget),
        Ok(Reference::new(3, 7))
    );
    // A freed number takes the next generation when it is used again.
    assert_eq!(transaction.free(3, &budget), Ok(true));
    assert!(matches!(transaction.version().slot(3), Some(Slot::Free(8))));
    assert_eq!(transaction.version().generation(3), None);
    assert_eq!(transaction.version().get(3, &budget), Ok(None));
    assert_eq!(transaction.free(3, &budget), Ok(false));
    assert_eq!(
        transaction.write(3, &value, &budget),
        Ok(Reference::new(3, 8))
    );
    // A number free with generation 65535 is never used again.
    for number in [8, 9] {
        assert_eq!(transaction.free(number, &budget), Ok(true));
        assert!(matches!(
            transaction.version().slot(number),
            Some(Slot::Free(u16::MAX))
        ));
        let error = transaction.write(number, &value, &budget).unwrap_err();
        assert!(matches!(error.kind(), ErrorKind::Malformed(_)), "{error}");
    }
    // Object 0 heads the list of free objects.
    let error = transaction.write(0, &value, &budget).unwrap_err();
    assert!(matches!(error.kind(), ErrorKind::Malformed(_)), "{error}");
    assert_eq!(transaction.free(0, &budget), Ok(false));
    // A number that nothing uses takes generation 0 and is not freed.
    assert_eq!(transaction.free(6, &budget), Ok(false));
    assert_eq!(
        transaction.write(6, &value, &budget),
        Ok(Reference::new(6, 0))
    );
    // New objects take the numbers above every number used.
    assert_eq!(transaction.allocate(), Ok(Reference::new(18, 0)));
    assert_eq!(transaction.allocate(), Ok(Reference::new(19, 0)));
    assert_eq!(transaction.version().next_number(), 20);
    assert_eq!(
        transaction.write(100, &value, &budget),
        Ok(Reference::new(100, 0))
    );
    assert_eq!(transaction.allocate(), Ok(Reference::new(101, 0)));
    // The largest object number leaves none for a new object.
    assert_eq!(
        transaction.write(u32::MAX, &value, &budget),
        Ok(Reference::new(u32::MAX, 0))
    );
    assert_eq!(transaction.version().next_number(), 1 << 32);
    let error = transaction.allocate().unwrap_err();
    assert!(matches!(error.kind(), ErrorKind::Malformed(_)), "{error}");
}

#[test]
fn a_refused_write_changes_nothing() {
    let budget = Budget::default();
    let document = document(None);
    let mut transaction = document.begin();
    transaction.write(2, &Value::Integer(20), &budget).unwrap();
    let before: Vec<u32> = transaction
        .version()
        .slots()
        .map(|(number, _)| number)
        .collect();
    // The budget refuses the bytes of the value, then the chunk and the
    // branches that the write copies.
    let long = Value::String(PdfString::literal(vec![b'x'; 4_096]));
    for limit in [0, 1_024, 4_200] {
        let tight = Budget::default().with_limit(LimitCode::MemorySize, Some(limit));
        let error = transaction.write(600, &long, &tight).unwrap_err();
        assert_eq!(
            error.limit().map(LimitExceeded::code),
            Some(LimitCode::MemorySize)
        );
        let after: Vec<u32> = transaction
            .version()
            .slots()
            .map(|(number, _)| number)
            .collect();
        assert_eq!(after, before);
        assert_eq!(transaction.version().next_number(), BASE_NEXT);
    }
    let cancelled = CancellationToken::new();
    cancelled.cancel();
    let refused = Budget::default().with_cancellation(cancelled);
    let error = transaction.free(2, &refused).unwrap_err();
    assert_eq!(
        error.limit().map(LimitExceeded::code),
        Some(LimitCode::Cancelled)
    );
    assert_eq!(value_of(transaction.version(), 2), Some(Value::Integer(20)));
    // The transaction goes on after a refusal.
    let version = transaction.commit(&budget).unwrap();
    assert_eq!(version.written().objects(), [2]);
}

#[test]
fn a_dropped_or_refused_transaction_publishes_nothing() {
    let budget = Budget::default();
    let document = document(None);
    let first = document.current();
    let mut transaction = document.begin();
    transaction.write(1, &Value::Null, &budget).unwrap();
    transaction.set_trailer(trailer(5));
    drop(transaction);
    assert!(Arc::ptr_eq(&document.current(), &first));
    assert_eq!(document.history().undo, 0);
    // A commit that the budget refuses drops the transaction and releases
    // the writer lock.
    let mut transaction = document.begin();
    transaction.write(1, &Value::Null, &budget).unwrap();
    let tight = Budget::default().with_limit(LimitCode::MemorySize, Some(0));
    let error = transaction.commit(&tight).unwrap_err();
    assert_eq!(
        error.limit().map(LimitExceeded::code),
        Some(LimitCode::MemorySize)
    );
    assert!(Arc::ptr_eq(&document.current(), &first));
    assert_eq!(value_of(&document.current(), 1), Some(Value::Integer(1)));
    assert!(document.try_begin().is_some());
    // A transaction that writes nothing publishes nothing.
    let version = document.begin().commit(&budget).unwrap();
    assert!(Arc::ptr_eq(&version, &first));
    let mut transaction = document.begin();
    transaction.allocate().unwrap();
    assert!(Arc::ptr_eq(&transaction.commit(&budget).unwrap(), &first));
    assert_eq!(document.history().undo, 0);
    assert_eq!(document.current().next_number(), BASE_NEXT);
}

#[test]
fn undo_and_redo_publish_the_versions_of_the_history_again() {
    let budget = Budget::default();
    let document = document(None);
    let mut versions = vec![document.current()];
    for marker in 1..=3 {
        let mut transaction = document.begin();
        transaction
            .write(2, &Value::Integer(marker), &budget)
            .unwrap();
        versions.push(transaction.commit(&budget).unwrap());
    }
    assert!(Arc::ptr_eq(&document.undo().unwrap(), &versions[2]));
    assert!(Arc::ptr_eq(&document.undo().unwrap(), &versions[1]));
    assert!(Arc::ptr_eq(&document.current(), &versions[1]));
    assert_eq!(value_of(&document.current(), 2), Some(Value::Integer(1)));
    assert!(Arc::ptr_eq(&document.redo().unwrap(), &versions[2]));
    let status = document.history();
    assert_eq!((status.undo, status.redo), (2, 1));
    // An edit after an undo replaces what redo could publish.
    let mut transaction = document.begin();
    transaction.write(5, &Value::Null, &budget).unwrap();
    let edited = transaction.commit(&budget).unwrap();
    assert!(document.redo().is_none());
    let status = document.history();
    assert_eq!((status.undo, status.redo), (3, 0));
    assert_eq!(value_of(&edited, 2), Some(Value::Integer(2)));
    assert_eq!(value_of(&edited, 5), Some(Value::Null));
    for _ in 0..3 {
        document.undo().unwrap();
    }
    assert!(document.undo().is_none());
    assert!(Arc::ptr_eq(&document.current(), &versions[0]));
}

#[test]
fn the_history_counts_what_its_versions_keep_alive() {
    // Values of 1, 2, and 3 bytes in one chunk under one branch.
    let budget = Budget::default();
    let document = Document::new(Arc::new(MemoryBase::default()), None);
    let nodes = Table::<Slot>::BRANCH_BYTES + Table::<Slot>::CHUNK_BYTES;
    let object = |length: u64| Written::OVERHEAD + length;
    for (marker, size) in [
        (7, 0),
        (42, nodes + object(1)),
        (100, 2 * nodes + object(1) + object(2)),
    ] {
        let mut transaction = document.begin();
        transaction
            .write(1, &Value::Integer(marker), &budget)
            .unwrap();
        transaction.commit(&budget).unwrap();
        // What the commit replaced: nothing, then the copied chunk and
        // branch and the object before.
        assert_eq!(document.history().size, size, "{marker}");
    }
    // After an undo, the current version keeps what it replaced, and the
    // version after it keeps what it added.
    document.undo().unwrap();
    assert_eq!(
        document.history().size,
        (nodes + object(1)) + (nodes + object(3))
    );
    document.undo().unwrap();
    assert_eq!(document.history().size, 2 * nodes + object(2) + object(3));
    // A limit drops the oldest versions first, here the first, which keeps
    // nothing alive, and then the newest.
    document.set_history_limit(Some(nodes + object(2)));
    let status = document.history();
    assert_eq!((status.undo, status.redo), (0, 1));
    assert_eq!(status.size, nodes + object(2));
    assert!(document.undo().is_none());
    // A limit too small for any step keeps the current version only.
    document.set_history_limit(Some(0));
    let status = document.history();
    assert_eq!((status.undo, status.redo, status.size), (0, 0, 0));
    assert_eq!(value_of(&document.current(), 1), Some(Value::Integer(7)));
}

#[test]
fn one_write_keeps_one_chunk_and_the_branches_above_it_alive() {
    // The bytes that one rewrite keeps alive in the version before it are
    // the copied chunk and branches and the object it replaced, whatever
    // the number of objects; only the height of the table counts.
    let budget = Budget::default();
    let object = Written::OVERHEAD + 1;
    for (numbers, height) in [
        ((1..=10_000).collect::<Vec<u32>>(), 2_u64),
        ((1..=100_000).collect(), 3),
        ((0..10_000).map(|n| 1 + n * 429_496).collect(), 6),
    ] {
        let document = Document::new(Arc::new(MemoryBase::default()), None);
        let mut transaction = document.begin();
        for &number in &numbers {
            transaction
                .write(number, &Value::Integer(1), &budget)
                .unwrap();
        }
        transaction.commit(&budget).unwrap();
        assert_eq!(document.history().size, 0);
        for &number in numbers.iter().step_by(997) {
            let before = document.history().size;
            let mut transaction = document.begin();
            transaction
                .write(number, &Value::Integer(2), &budget)
                .unwrap();
            transaction.commit(&budget).unwrap();
            let kept = height * Table::<Slot>::BRANCH_BYTES + Table::<Slot>::CHUNK_BYTES + object;
            assert_eq!(document.history().size - before, kept, "{number}");
            document.undo().unwrap();
            document.redo().unwrap();
            document.undo().unwrap();
            // Undone, the version after keeps its own copies and object.
            assert_eq!(document.history().size - before, kept, "{number}");
            document.redo().unwrap();
        }
    }
}

#[test]
fn change_sets_are_the_union_of_what_the_transactions_between_wrote() {
    let budget = Budget::default();
    let document = document(None);
    let mut versions = vec![document.current()];
    let writes: [&[u32]; 5] = [&[1, 2], &[600], &[2, 3, 20_000], &[1], &[u32::MAX]];
    for numbers in writes {
        let mut transaction = document.begin();
        for &number in numbers {
            transaction.write(number, &Value::Null, &budget).unwrap();
        }
        versions.push(transaction.commit(&budget).unwrap());
    }
    for i in 0..versions.len() {
        for j in i..versions.len() {
            let union: BTreeSet<u32> = versions[i + 1..=j]
                .iter()
                .flat_map(|version| version.written().objects().iter().copied())
                .collect();
            let changes = versions[i].changes(&versions[j], &budget).unwrap().unwrap();
            assert_eq!(
                changes.objects(),
                union.into_iter().collect::<Vec<_>>(),
                "{i} {j}"
            );
            assert_eq!(
                versions[j].changes(&versions[i], &budget).unwrap().unwrap(),
                changes
            );
        }
    }
    let other = Document::new(Arc::new(base()), None);
    assert_eq!(versions[0].changes(&other.current(), &budget), Ok(None));
}

#[test]
fn a_read_parses_an_object_once_and_shares_its_value() {
    let budget = Budget::default();
    let mut base = base();
    base.insert(Reference::new(30, 0), Bytes::from(b"]".to_vec()));
    let document = Document::new(Arc::new(base), None);
    let first = document.current();
    let read = first.get(1, &budget).unwrap().unwrap();
    let mut transaction = document.begin();
    transaction.write(2, &Value::Integer(20), &budget).unwrap();
    let written = transaction.version().get(2, &budget).unwrap().unwrap();
    let version = transaction.commit(&budget).unwrap();
    // A base object that no transaction wrote, and a written object, read
    // in any version, share one value.
    for again in [first.get(1, &budget), version.get(1, &budget)] {
        assert!(Arc::ptr_eq(again.unwrap().unwrap().value(), read.value()));
    }
    assert!(Arc::ptr_eq(
        version.get(2, &budget).unwrap().unwrap().value(),
        written.value()
    ));
    let Some(Slot::Written(object)) = version.slot(2) else {
        panic!("object 2 is not written");
    };
    assert_eq!(object.bytes().as_slice(), b"20");
    assert!(Arc::ptr_eq(
        &object.value(&budget).unwrap(),
        written.value()
    ));
    // An object whose bytes do not parse refuses every read.
    for _ in 0..2 {
        let error = version.get(30, &budget).unwrap_err();
        assert!(matches!(error.kind(), ErrorKind::Malformed(_)));
        assert!(error.to_string().starts_with("object 30 0: "), "{error}");
    }
    // A refused read keeps nothing, so a later read with a larger budget
    // parses the object.
    let tight = Budget::default().with_limit(LimitCode::MemorySize, Some(0));
    let refused = version.get(5, &tight).unwrap_err();
    assert_eq!(
        refused.limit().map(LimitExceeded::code),
        Some(LimitCode::MemorySize)
    );
    assert_eq!(value_of(&version, 5), Some(Value::Integer(5)));
}

#[test]
fn every_kind_of_value_reads_back_as_written() {
    let budget = Budget::default();
    let document = Document::new(Arc::new(MemoryBase::default()), None);
    let mut transaction = document.begin();
    let values = values();
    for (number, value) in (1..).zip(&values) {
        transaction.write(number, value, &budget).unwrap();
    }
    let version = transaction.commit(&budget).unwrap();
    for (number, value) in (1..).zip(&values) {
        assert_eq!(value_of(&version, number).as_ref(), Some(value), "{number}");
        let Some(Slot::Written(object)) = version.slot(number) else {
            panic!("object {number} is not written");
        };
        assert_eq!(object.bytes(), &bytes_of(value));
    }
}

#[test]
fn versions_and_documents_are_shared_across_threads() {
    const fn shared<T: Send + Sync>() {}
    shared::<Version>();
    shared::<Document>();
    shared::<Transaction>();
    shared::<ChangeSet>();
    shared::<Object>();
}

#[test]
fn readers_on_other_threads_see_only_complete_versions() {
    // Every transaction writes one marker into every object and the trailer,
    // so a version that mixed two transactions would hold two markers.
    let numbers: Vec<u32> = (1..=48).map(|n| n * 97).collect();
    let mut base = MemoryBase::new(trailer(0));
    for &number in &numbers {
        base.insert(Reference::new(number, 0), bytes_of(&Value::Integer(0)));
    }
    let document = Document::new(Arc::new(base), Some(0));
    let done = AtomicBool::new(false);
    let markers = |version: &Version| -> Vec<i64> {
        let budget = Budget::default();
        let trailer = match version.trailer().get("Marker") {
            Some(Value::Integer(marker)) => *marker,
            other => panic!("the trailer's marker is {other:?}"),
        };
        let mut markers = vec![trailer];
        for &number in &numbers {
            match version
                .get(number, &budget)
                .unwrap()
                .map(|object| (**object.value()).clone())
            {
                Some(Value::Integer(marker)) => markers.push(marker),
                other => panic!("object {number} is {other:?}"),
            }
        }
        markers
    };
    thread::scope(|scope| {
        let readers: Vec<_> = (0..4)
            .map(|_| {
                scope.spawn(|| {
                    let kept = document.current();
                    let first = markers(&kept);
                    let mut last = first[0];
                    let mut reads = 0;
                    while !done.load(Ordering::Acquire) || reads == 0 {
                        let markers = markers(&document.current());
                        assert!(
                            markers.iter().all(|&marker| marker == markers[0]),
                            "{markers:?}"
                        );
                        // Versions appear in the order of their commits.
                        assert!(markers[0] >= last);
                        last = markers[0];
                        reads += 1;
                    }
                    // A version stays as it was while newer ones are published.
                    assert_eq!(markers(&kept), first);
                    reads
                })
            })
            .collect();
        let budget = Budget::default();
        for marker in 1..=300 {
            let mut transaction = document.begin();
            for &number in &numbers {
                transaction
                    .write(number, &Value::Integer(marker), &budget)
                    .unwrap();
            }
            transaction.set_trailer(trailer(marker));
            transaction.commit(&budget).unwrap();
        }
        done.store(true, Ordering::Release);
        for reader in readers {
            assert!(reader.join().unwrap() > 0);
        }
    });
    assert_eq!(markers(&document.current()), vec![300; numbers.len() + 1]);
}

#[test]
fn writers_on_other_threads_serialize_through_the_writer_lock() {
    // Each transaction adds one to a counter. A transaction that began from
    // a version that another had already replaced would lose an addition.
    const THREADS: i64 = 4;
    const ADDITIONS: i64 = 100;
    let document = Document::new(Arc::new(MemoryBase::default()), None);
    thread::scope(|scope| {
        for _ in 0..THREADS {
            scope.spawn(|| {
                let budget = Budget::default();
                for _ in 0..ADDITIONS {
                    let mut transaction = document.begin();
                    let count = match value_of(transaction.version(), 1) {
                        Some(Value::Integer(count)) => count,
                        _ => 0,
                    };
                    transaction
                        .write(1, &Value::Integer(count + 1), &budget)
                        .unwrap();
                    transaction.commit(&budget).unwrap();
                }
            });
        }
    });
    assert_eq!(
        value_of(&document.current(), 1),
        Some(Value::Integer(THREADS * ADDITIONS))
    );
    assert_eq!(
        document.history().undo,
        usize::try_from(THREADS * ADDITIONS).unwrap()
    );
}

#[test]
fn undo_waits_for_the_transaction_that_holds_the_writer_lock() {
    let budget = Budget::default();
    let document = document(None);
    let first = document.current();
    let mut transaction = document.begin();
    let (started, waiting) = mpsc::channel();
    thread::scope(|scope| {
        let undo = scope.spawn(|| {
            started.send(()).unwrap();
            document.undo()
        });
        waiting.recv().unwrap();
        thread::sleep(Duration::from_millis(20));
        transaction.write(1, &Value::Null, &budget).unwrap();
        transaction.commit(&budget).unwrap();
        // The undo waited for the commit, and undid it.
        let undone = undo.join().unwrap();
        assert!(undone.is_some_and(|version| Arc::ptr_eq(&version, &first)));
    });
    assert!(Arc::ptr_eq(&document.current(), &first));
    assert_eq!(document.history().redo, 1);
}
