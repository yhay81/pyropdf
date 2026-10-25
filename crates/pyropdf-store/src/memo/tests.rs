//! Tests of read sets and the memo: a memoized result always equals a fresh
//! computation, and a result whose read set the changes miss is reused.

use std::sync::Arc;
use std::thread;

use proptest::prelude::*;

use super::*;
use crate::budget::Budget;
use crate::value::{Array, Bytes, Name, Reference, Value};
use crate::version::{Document, MemoryBase};
use crate::write;

/// The object numbers of the test documents.
const NUMBERS: u32 = 8;

fn bytes_of(value: &Value) -> Bytes {
    let budget = Budget::default();
    let mut out = BudgetVec::new(&budget);
    write::value(&mut out, value).unwrap();
    Bytes::new(out.into_vec())
}

fn trailer(marker: i64) -> Dict {
    [(Name::from("Marker"), Value::Integer(marker))]
        .into_iter()
        .collect()
}

/// A value that the walk reads: an end, a link, or an amount and a link.
fn link(kind: u8, amount: i64, to: u32) -> Value {
    match kind % 3 {
        0 => Value::Integer(amount),
        1 => Value::Reference(Reference::new(to % NUMBERS + 1, 0)),
        _ => Value::Array(Array::from(vec![
            Value::Integer(amount),
            Value::Reference(Reference::new(to % NUMBERS + 1, 0)),
        ])),
    }
}

/// The derived computation: follows links from `start`, at most eight
/// steps, summing the amounts times `factor`; an odd factor adds the
/// trailer's marker. What it reads depends on the data.
fn walk(reads: &mut Reads<'_>, start: u32, factor: i64) -> Result<i64> {
    let mut sum = 0;
    if factor % 2 != 0
        && let Some(&Value::Integer(marker)) = reads.trailer().get("Marker")
    {
        sum += marker;
    }
    let mut number = start;
    for _ in 0..8 {
        let Some(object) = reads.get(number)? else {
            break;
        };
        match &**object.value() {
            Value::Integer(amount) => {
                sum += amount * factor;
                break;
            }
            Value::Reference(to) => number = to.number(),
            Value::Array(pair) => match pair.as_slice() {
                [Value::Integer(amount), Value::Reference(to)] => {
                    sum += amount * factor;
                    number = to.number();
                }
                _ => break,
            },
            _ => break,
        }
    }
    Ok(sum)
}

fn fresh(version: &Version, start: u32, factor: i64) -> i64 {
    let budget = Budget::default();
    walk(&mut Reads::new(version, &budget), start, factor).unwrap()
}

fn document(values: &[(u8, i64, u32)]) -> Document {
    let mut base = MemoryBase::new(trailer(0));
    for (number, &(kind, amount, to)) in (1..).zip(values) {
        base.insert(Reference::new(number, 0), bytes_of(&link(kind, amount, to)));
    }
    Document::new(Arc::new(base), None)
}

#[derive(Clone, Debug)]
enum Step {
    Write(u32, u8, i64, u32),
    Free(u32),
    Trailer(i64),
    Undo,
    Redo,
    Query(u32, i64),
}

fn step() -> impl Strategy<Value = Step> {
    prop_oneof![
        3 => (1..=NUMBERS, any::<u8>(), -9..9i64, 0..NUMBERS).prop_map(|(n, k, a, t)| Step::Write(n, k, a, t)),
        1 => (1..=NUMBERS).prop_map(Step::Free),
        1 => (-9..9i64).prop_map(Step::Trailer),
        1 => Just(Step::Undo),
        1 => Just(Step::Redo),
        6 => (1..=NUMBERS, 1..4i64).prop_map(|(s, f)| Step::Query(s, f)),
    ]
}

proptest! {
    #[test]
    fn a_memoized_result_equals_a_fresh_computation(
        values in proptest::collection::vec((any::<u8>(), -9..9i64, 0..NUMBERS), NUMBERS as usize),
        steps in proptest::collection::vec(step(), 1..64),
        capacity in 0..6usize,
    ) {
        let budget = Budget::default();
        let document = document(&values);
        let memo: Memo<(u32, i64), i64> = Memo::new(capacity);
        // Keep every version alive, so that the memo can compare with them.
        let mut versions = vec![document.current()];
        for step in steps {
            match step {
                Step::Write(number, kind, amount, to) => {
                    let mut transaction = document.begin();
                    transaction.write(number, &link(kind, amount, to), &budget).unwrap();
                    versions.push(transaction.commit(&budget).unwrap());
                }
                Step::Free(number) => {
                    let mut transaction = document.begin();
                    transaction.free(number, &budget).unwrap();
                    versions.push(transaction.commit(&budget).unwrap());
                }
                Step::Trailer(marker) => {
                    let mut transaction = document.begin();
                    transaction.set_trailer(trailer(marker), &budget).unwrap();
                    versions.push(transaction.commit(&budget).unwrap());
                }
                Step::Undo => versions.extend(document.undo()),
                Step::Redo => versions.extend(document.redo()),
                Step::Query(start, factor) => {
                    let current = document.current();
                    let memoized = memo
                        .get_or_compute::<Error>(&(start, factor), &current, &budget, |reads| {
                            walk(reads, start, factor)
                        })
                        .unwrap();
                    prop_assert_eq!(*memoized, fresh(&current, start, factor));
                }
            }
        }
        prop_assert!(memo.len() <= capacity);
    }
}

#[test]
fn a_result_is_reused_while_the_changes_miss_its_reads() {
    let budget = Budget::default();
    // 1 -> 2 -> 3 (an end); 4 is not read from 1.
    let document = document(&[(1, 0, 1), (2, 5, 2), (0, 7, 0), (0, 100, 0)]);
    let memo: Memo<(u32, i64), i64> = Memo::new(4);
    let mut computed = 0;
    let mut query = |version: &Arc<Version>, factor: i64| {
        *memo
            .get_or_compute::<Error>(&(1, factor), version, &budget, |reads| {
                computed += 1;
                walk(reads, 1, factor)
            })
            .unwrap()
    };
    let first = document.current();
    assert_eq!(query(&first, 2), 24);
    assert_eq!(query(&first, 2), 24);

    // Writing object 4 and the trailer misses the read set of factor 2.
    let mut transaction = document.begin();
    transaction.write(4, &Value::Integer(1), &budget).unwrap();
    transaction.set_trailer(trailer(1000), &budget).unwrap();
    let second = transaction.commit(&budget).unwrap();
    assert_eq!(query(&second, 2), 24);
    // A different setting is a different result, which reads the trailer.
    assert_eq!(query(&second, 3), 1036);

    // Writing object 3 changes the result.
    let mut transaction = document.begin();
    transaction.write(3, &Value::Integer(8), &budget).unwrap();
    let third = transaction.commit(&budget).unwrap();
    assert_eq!(query(&third, 2), 26);
    // Undo restores a version whose change set from the third version
    // contains object 3 again.
    let back = document.undo().unwrap();
    assert_eq!(query(&back, 2), 24);
    drop(query);

    assert_eq!(computed, 4);
    assert_eq!(
        memo.stats(),
        Stats {
            hits: 2,
            computations: 4
        }
    );
}

#[test]
fn reads_record_every_number_asked_for() {
    let budget = Budget::default();
    let document = document(&[(0, 1, 0)]);
    let version = document.current();
    let mut reads = Reads::new(&version, &budget);
    assert!(reads.get(5).unwrap().is_none());
    reads.get(1).unwrap();
    reads.get(5).unwrap();
    assert_eq!(reads.generation(3).unwrap(), None);
    let set = reads.finish().unwrap();
    assert_eq!(set.objects(), &[1, 3, 5]);
    assert!(!set.trailer());
}

#[test]
fn the_memo_does_not_keep_a_version_alive() {
    let budget = Budget::default();
    let document = document(&[(0, 1, 0)]);
    let memo: Memo<u8, i64> = Memo::new(1);
    let mut transaction = document.begin();
    transaction.write(2, &Value::Integer(2), &budget).unwrap();
    let version = transaction.commit(&budget).unwrap();
    memo.get_or_compute::<Error>(&0, &version, &budget, |reads| walk(reads, 1, 2))
        .unwrap();
    let weak = Arc::downgrade(&version);
    document.undo().unwrap();
    document.set_history_limit(Some(0));
    drop(version);
    assert!(weak.upgrade().is_none());
    // The result is computed again on a version that the memo cannot compare with.
    let current = document.current();
    memo.get_or_compute::<Error>(&0, &current, &budget, |reads| walk(reads, 1, 2))
        .unwrap();
    assert_eq!(memo.stats().computations, 2);
}

#[test]
fn the_capacity_evicts_the_least_recently_used_result() {
    let budget = Budget::default();
    let document = document(&[(0, 1, 0), (0, 2, 0), (0, 3, 0)]);
    let version = document.current();
    let memo: Memo<u32, i64> = Memo::new(2);
    let get = |start: u32| {
        memo.get_or_compute::<Error>(&start, &version, &budget, |reads| walk(reads, start, 2))
            .unwrap()
    };
    get(1);
    get(2);
    get(1);
    get(3);
    assert_eq!(memo.len(), 2);
    get(1);
    assert_eq!(
        memo.stats(),
        Stats {
            hits: 2,
            computations: 3
        }
    );
    get(2);
    assert_eq!(memo.stats().computations, 4);
}

#[test]
fn threads_share_a_memo() {
    let budget = Budget::default();
    let document = document(&[(0, 1, 0), (0, 2, 0), (0, 3, 0)]);
    let version = document.current();
    let memo: Memo<u32, i64> = Memo::new(8);
    thread::scope(|scope| {
        for _ in 0..4 {
            scope.spawn(|| {
                for start in 1..=3 {
                    let value = memo
                        .get_or_compute::<Error>(&start, &version, &budget, |reads| {
                            walk(reads, start, 2)
                        })
                        .unwrap();
                    assert_eq!(*value, i64::from(start) * 2);
                }
            });
        }
    });
    let stats = memo.stats();
    assert_eq!(stats.hits + stats.computations, 12);
    assert_eq!(memo.len(), 3);
}
