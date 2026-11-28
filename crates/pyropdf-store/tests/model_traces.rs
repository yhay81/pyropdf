//! Model-driven trace validation tests bridging the TLA+ specifications under
//! `evidence/formal-models/` to the real `pyropdf-store` implementation (#1255).
//!
//! Each test exercises the exact state transitions and invariants verified by
//! TLC (`HistoryAndSaveNumbers.tla`, `MemoInvalidation.tla`,
//! `SettlingProtocol.tla`) directly against `Document`, `Transaction`, `Memo`,
//! `ObjectStamps`, and `Settlers`.

#![allow(
    clippy::disallowed_methods,
    reason = "tests grow standard collections freely; F6 binds the store's own code"
)]

use std::collections::HashSet;
use std::sync::{Arc, Mutex};

use pyropdf_store::budget::{Budget, DocumentLimits};
use pyropdf_store::memo::Memo;
use pyropdf_store::settle::{Scope, Settler, Settlers, Structure};
use pyropdf_store::value::{Bytes, Dict, Name, PdfString, Reference, Value};
use pyropdf_store::version::{Document, MemoryBase, RestoredHistory, Transaction, Version};
use pyropdf_store::{Error, Result};

fn memory_base() -> Arc<MemoryBase> {
    let mut base = MemoryBase::new(
        [
            (Name::from("Root"), Value::from(Reference::new(1, 0))),
            (Name::from("Size"), Value::from(3)),
        ]
        .into_iter()
        .collect::<Dict>(),
    );
    base.insert(Reference::new(1, 0), Bytes::new(&b"0"[..]));
    base.insert(Reference::new(2, 0), Bytes::new(&b"0"[..]));
    Arc::new(base)
}

fn read_int(version: &Version, number: u32, budget: &Budget) -> Option<i64> {
    match version.get(number, budget).unwrap() {
        Some(obj) => match **obj.value() {
            Value::Integer(n) => Some(n),
            _ => panic!("expected integer at object {number}"),
        },
        None => None,
    }
}

/// Settler for `Structure::FontSubset` that allocates `count` save numbers via
/// `Transaction::number_for_save`, writes objects there, and records the
/// assigned numbers for determinism verification.
struct SaveNumberSettler {
    count: usize,
    recorded: Arc<Mutex<Vec<Vec<u32>>>>,
}

impl Settler for SaveNumberSettler {
    fn settle(
        &self,
        transaction: &mut Transaction,
        numbers: &[u32],
        budget: &Budget,
    ) -> Result<()> {
        let mut assigned = Vec::new();
        for &target in numbers {
            for _ in 0..self.count {
                let num = transaction.number_for_save()?;
                // Save numbers must never collide with an object already live
                // in the version being saved.
                assert!(
                    transaction.version().get(num, budget)?.is_none(),
                    "save number {num} collided with live object"
                );
                transaction.write(num, &Value::Integer(i64::from(target)), budget)?;
                assigned.push(num);
            }
        }
        self.recorded.lock().unwrap().push(assigned);
        Ok(())
    }
}

fn alloc_unique(tx: &mut Transaction, seen: &mut HashSet<u32>) -> u32 {
    let n = tx.allocate().unwrap().number();
    assert!(
        seen.insert(n),
        "object number {n} was reissued by allocate()"
    );
    n
}

/// Bridges `HistoryAndSaveNumbers.tla`: verifies lifetime allocation
/// monotonicity, savepoint rollback, deterministic `SaveNumbers` in both
/// unwritten and dirty transactions, `History::fit` eviction bounds, and exact
/// `restore_history` label alignment across a sweep of memory limits.
#[test]
fn trace_history_and_save_numbers_invariants() {
    let budget = Budget::new(&DocumentLimits::default());
    let base = memory_base();
    let doc = Document::new(Arc::clone(&base) as _, None);
    let save_log: Arc<Mutex<Vec<Vec<u32>>>> = Arc::new(Mutex::new(Vec::new()));
    doc.set_settlers(Settlers::default().with(
        Structure::FontSubset,
        Arc::new(SaveNumberSettler {
            count: 2,
            recorded: Arc::clone(&save_log),
        }),
    ));

    let mut allocated_ever = HashSet::new();

    // 1. Dropped transaction + savepoint rollback keep allocations reserved.
    {
        let mut tx = doc.begin();
        let n1 = alloc_unique(&mut tx, &mut allocated_ever);
        let sp = tx.savepoint();
        let n2 = alloc_unique(&mut tx, &mut allocated_ever);
        tx.write(1, &Value::Integer(999), &budget).unwrap();
        tx.rollback(sp);
        assert_eq!(read_int(tx.version(), 1, &budget), Some(0));
        let n3 = alloc_unique(&mut tx, &mut allocated_ever);
        assert!(n3 > n2 && n2 > n1);
        drop(tx);
    }

    // 2. Commit 4 labeled versions with distinct payloads (and a FontSubset
    // mark on v1) so we can exercise undo, redo, deterministic clean/dirty
    // saves, and restore_history under every tightening limit.
    let mut step_labels = Vec::new();
    let mut expected_vals_by_step = Vec::new();
    for step in 1..=4_i64 {
        let mut tx = doc.begin();
        let _ = alloc_unique(&mut tx, &mut allocated_ever);
        // Varying object payload sizes exercise asymmetric undo/redo weights.
        let payload = Value::String(PdfString::literal(vec![b'a'; (step as usize) * 200]));
        let holder = alloc_unique(&mut tx, &mut allocated_ever);
        tx.write(holder, &payload, &budget).unwrap();
        tx.write(1, &Value::Integer(step * 10), &budget).unwrap();
        if step == 1 {
            tx.mark(Structure::FontSubset, 1, &budget).unwrap();
        }
        tx.commit(&budget).unwrap();
        step_labels.push(format!("step-{step}"));
        expected_vals_by_step.push(step * 10);
    }

    // 3. Undo twice so both undo and redo queues are non-empty, then verify
    // allocate() after undo still never reuses any earlier number.
    doc.undo().unwrap();
    doc.undo().unwrap();
    assert_eq!(read_int(&doc.current(), 1, &budget), Some(20));
    {
        let mut tx = doc.begin();
        let prev_max = *allocated_ever.iter().max().unwrap();
        let after_undo = alloc_unique(&mut tx, &mut allocated_ever);
        assert!(after_undo > prev_max);
        drop(tx);
    }

    // 4. Deterministic clean-save numbering (`Settling::Unwritten`): two
    // consecutive save-settling passes on an unchanged transaction must choose
    // identical object numbers, even after the dropped allocations above.
    save_log.lock().unwrap().clear();
    for _ in 0..2 {
        let mut tx = doc.begin();
        tx.settle(Scope::Save, &budget).unwrap();
        drop(tx);
    }
    let clean_runs = save_log.lock().unwrap().clone();
    assert_eq!(clean_runs.len(), 2);
    assert_eq!(
        clean_runs[0], clean_runs[1],
        "repeated clean saves produced non-deterministic object numbers"
    );
    assert!(
        clean_runs[0]
            .iter()
            .all(|&n| u64::from(n) >= doc.current().next_number())
    );

    // 5. Deterministic dirty-save numbering (`Settling::Taken`): inside one
    // dirty transaction, settling for save, rolling back to a savepoint, and
    // settling again reuses the exact same `taken` range; allocating in
    // between shifts cleanly to fresh numbers above `hwm`.
    save_log.lock().unwrap().clear();
    {
        let mut tx = doc.begin();
        tx.write(2, &Value::Integer(55), &budget).unwrap();
        let sp = tx.savepoint();
        tx.settle(Scope::Save, &budget).unwrap();
        tx.rollback(sp);
        let sp2 = tx.savepoint();
        tx.settle(Scope::Save, &budget).unwrap();
        tx.rollback(sp2);
        let fresh = alloc_unique(&mut tx, &mut allocated_ever);
        let dirty_runs = save_log.lock().unwrap().clone();
        assert_eq!(dirty_runs.len(), 2);
        assert_eq!(
            dirty_runs[0], dirty_runs[1],
            "repeated dirty saves inside one transaction must reuse taken numbers"
        );
        assert!(
            dirty_runs[0].iter().all(|&n| n < fresh),
            "subsequent allocate() overlapped numbers reserved by save"
        );
        drop(tx);
    }

    // 6. Save history at position 2/4 and restore into freshly opened
    // documents across a sweep of memory limits: verify History::fit bounds
    // AND exact label-to-version alignment on every limit.
    let status = doc.history();
    assert_eq!((status.undo, status.redo), (2, 2));
    let saved = doc
        .save_history(&step_labels, status.undo, &budget)
        .unwrap();

    let mut test_limits = vec![None, Some(0), Some(1), Some(status.size + 1024)];
    for step_budget in (100..=status.size).step_by(150) {
        test_limits.push(Some(step_budget));
    }
    for lim in test_limits {
        let reopened = Document::new(Arc::clone(&base) as _, lim);
        let RestoredHistory::Restored { labels, position } =
            reopened.restore_history(&saved, &budget).unwrap()
        else {
            panic!("expected Restored for matching base");
        };
        let st = reopened.history();
        assert_eq!(position, st.undo);
        assert_eq!(labels.len(), st.undo + st.redo);
        if let Some(cap) = lim {
            assert!(
                st.size <= cap,
                "restored history size {} exceeded limit {cap}",
                st.size
            );
        }
        // Current version must still hold step-2's value (20).
        assert_eq!(read_int(&reopened.current(), 1, &budget), Some(20));

        // Lift the limit only after verifying `fit()` so walking redo/undo to
        // inspect every retained label cannot trigger additional evictions.
        reopened.set_history_limit(None);

        // Walk all kept redo steps and verify each label matches the exact
        // step value that produced it.
        for label in &labels[position..] {
            reopened.redo().unwrap();
            let step_idx: i64 = label.strip_prefix("step-").unwrap().parse().unwrap();
            assert_eq!(
                read_int(&reopened.current(), 1, &budget),
                Some(step_idx * 10),
                "redo label {label} misaligned with restored version"
            );
        }
        // Walk back to current, then walk all kept undo steps.
        for _ in position..labels.len() {
            reopened.undo().unwrap();
        }
        assert_eq!(read_int(&reopened.current(), 1, &budget), Some(20));
        for idx in (0..position).rev() {
            let label = &labels[idx];
            let step_idx: i64 = label.strip_prefix("step-").unwrap().parse().unwrap();
            assert_eq!(
                read_int(&reopened.current(), 1, &budget),
                Some(step_idx * 10),
                "undo label {label} misaligned before undo"
            );
            reopened.undo().unwrap();
            let prev_expected = if step_idx == 1 {
                0
            } else {
                expected_vals_by_step[(step_idx - 2) as usize]
            };
            assert_eq!(
                read_int(&reopened.current(), 1, &budget),
                Some(prev_expected)
            );
        }
    }
}

/// Bridges `MemoInvalidation.tla`: verifies F4 dynamic data-dependent read-set
/// memoization across edits, same-value rewrites, undo/redo, weak-ref expiry,
/// and `ObjectStamps` soundness.
#[test]
fn trace_memo_invalidation_and_stamps_invariants() {
    let budget = Budget::new(&DocumentLimits::default());
    let doc = Document::new(memory_base(), None);
    let memo = Memo::<u32, i64>::new(2);

    // Exact data-dependent query from `MemoInvalidation.tla`:
    // reads obj 1, and reads obj 2 only when obj 1 == 1.
    let eval_ground_truth = |v: &Version| -> i64 {
        let a = read_int(v, 1, &budget).unwrap();
        if a == 1 {
            let b = read_int(v, 2, &budget).unwrap();
            a * 10 + b
        } else {
            a
        }
    };
    let eval_memo = |v: &Arc<Version>| -> i64 {
        *memo
            .get_or_compute(&0, v, &budget, |reads| {
                let a = match **reads.get(1)?.unwrap().value() {
                    Value::Integer(n) => n,
                    _ => unreachable!(),
                };
                if a == 1 {
                    let b = match **reads.get(2)?.unwrap().value() {
                        Value::Integer(n) => n,
                        _ => unreachable!(),
                    };
                    Ok::<_, Error>(a * 10 + b)
                } else {
                    Ok(a)
                }
            })
            .unwrap()
    };

    // Initial state: (0, 0) -> reads {1}, result = 0.
    let v0 = doc.current();
    assert_eq!(eval_memo(&v0), eval_ground_truth(&v0));
    let stamps_v0 = v0.stamps(&[1], &budget).unwrap();

    // Edit obj 2 -> 5: disjoint from {1}, must be a memo hit AND stamps hold!
    let v1 = {
        let mut tx = doc.begin();
        tx.write(2, &Value::Integer(5), &budget).unwrap();
        tx.commit(&budget).unwrap()
    };
    let before_hits = memo.stats().hits;
    assert_eq!(eval_memo(&v1), eval_ground_truth(&v1));
    assert_eq!(memo.stats().hits, before_hits + 1);
    assert!(stamps_v0.unchanged(&v1, &budget).unwrap());

    // Edit obj 1 -> 1: invalidates memo, new read set becomes {1, 2}, result = 15.
    let v2 = {
        let mut tx = doc.begin();
        tx.write(1, &Value::Integer(1), &budget).unwrap();
        tx.commit(&budget).unwrap()
    };
    assert_eq!(eval_memo(&v2), 15);
    assert_eq!(eval_memo(&v2), eval_ground_truth(&v2));
    assert!(!stamps_v0.unchanged(&v2, &budget).unwrap());
    let stamps_v2 = v2.stamps(&[1, 2], &budget).unwrap();

    // Same-value rewrite of obj 2 -> 5 (#503): must invalidate stamps AND
    // recompute cleanly to the exact ground truth (15).
    let v3 = {
        let mut tx = doc.begin();
        tx.write(2, &Value::Integer(5), &budget).unwrap();
        tx.commit(&budget).unwrap()
    };
    assert!(!stamps_v2.unchanged(&v3, &budget).unwrap());
    assert_eq!(eval_memo(&v3), eval_ground_truth(&v3));

    // Edit obj 2 -> 9 while obj 1 == 1: read set is {1, 2}, so memo MUST
    // invalidate and return 19 (never stale 15!).
    let v4 = {
        let mut tx = doc.begin();
        tx.write(2, &Value::Integer(9), &budget).unwrap();
        tx.commit(&budget).unwrap()
    };
    assert_eq!(eval_memo(&v4), 19);

    // Undo back through v3, v2, v1, v0 and redo forward, verifying NoStaleRead
    // at every step.
    for _ in 0..4 {
        let ver = doc.undo().unwrap();
        assert_eq!(eval_memo(&ver), eval_ground_truth(&ver));
    }
    for _ in 0..4 {
        let ver = doc.redo().unwrap();
        assert_eq!(eval_memo(&ver), eval_ground_truth(&ver));
    }

    // Expire the memo's cached version (Weak::upgrade fails): clear history so
    // only a detached version remains, drop it, and verify fresh recomputation.
    doc.set_history_limit(Some(0));
    drop(v0);
    drop(v1);
    drop(v2);
    drop(v3);
    drop(v4);
    let latest = doc.current();
    assert_eq!(eval_memo(&latest), eval_ground_truth(&latest));
}

struct ScriptedSettler {
    mode: Mutex<SettlerMode>,
}

#[derive(Clone, Copy)]
enum SettlerMode {
    /// Write `val` to object 1 and optionally mark `cascade` stale.
    WriteAndMark {
        val: i64,
        cascade: Option<Structure>,
    },
    /// Allocate an object number, write a partial mutation, then fail.
    AllocateThenFail,
    /// Re-mark itself every round to trigger `MAX_ROUNDS` cycle refusal.
    SelfCycle,
}

impl Settler for ScriptedSettler {
    fn settle(
        &self,
        transaction: &mut Transaction,
        numbers: &[u32],
        budget: &Budget,
    ) -> Result<()> {
        let mode = *self.mode.lock().unwrap();
        match mode {
            SettlerMode::WriteAndMark { val, cascade } => {
                transaction.write(1, &Value::Integer(val), budget)?;
                if let Some(next) = cascade {
                    for &n in numbers {
                        transaction.mark(next, n, budget)?;
                    }
                }
                Ok(())
            }
            SettlerMode::AllocateThenFail => {
                let n = transaction.allocate()?;
                transaction.write(n.number(), &Value::Integer(404), budget)?;
                transaction.write(1, &Value::Integer(404), budget)?;
                Err(Error::malformed("injected settler refusal"))
            }
            SettlerMode::SelfCycle => {
                transaction.write(1, &Value::Integer(777), budget)?;
                for &n in numbers {
                    transaction.mark(Structure::Appearance, n, budget)?;
                }
                Ok(())
            }
        }
    }
}

/// Bridges `SettlingProtocol.tla`: verifies scope filtering (`FontSubset` never
/// cleared outside `Scope::Save`), cascade convergence, atomic rollback on
/// settler error with monotonic high-water mark, and cycle refusal at
/// `MAX_ROUNDS`.
#[test]
fn trace_settling_protocol_invariants() {
    let budget = Budget::new(&DocumentLimits::default());
    let appearance = Arc::new(ScriptedSettler {
        mode: Mutex::new(SettlerMode::WriteAndMark {
            val: 11,
            cascade: Some(Structure::FontSubset),
        }),
    });
    let font_subset = Arc::new(ScriptedSettler {
        mode: Mutex::new(SettlerMode::WriteAndMark {
            val: 22,
            cascade: None,
        }),
    });

    let doc = Document::new(memory_base(), None);
    doc.set_settlers(
        Settlers::default()
            .with(Structure::Appearance, Arc::clone(&appearance) as _)
            .with(Structure::FontSubset, Arc::clone(&font_subset) as _),
    );

    // 1. Appearance cascades to FontSubset; Scope::Commit settles Appearance
    //    but MUST preserve FontSubset stale mark.
    {
        let mut tx = doc.begin();
        tx.mark(Structure::Appearance, 1, &budget).unwrap();
        tx.settle(Scope::One(Structure::Appearance), &budget)
            .unwrap();
        assert!(!tx.is_stale(Structure::Appearance, 1));
        assert!(tx.is_stale(Structure::FontSubset, 1));
        assert_eq!(read_int(tx.version(), 1, &budget), Some(11));

        // Scope::Commit still leaves FontSubset stale.
        tx.settle(Scope::Commit, &budget).unwrap();
        assert!(tx.is_stale(Structure::FontSubset, 1));

        // Scope::Save settles FontSubset as well.
        tx.settle(Scope::Save, &budget).unwrap();
        assert!(!tx.is_stale(Structure::Appearance, 1));
        assert!(!tx.is_stale(Structure::FontSubset, 1));
        assert_eq!(read_int(tx.version(), 1, &budget), Some(22));
        drop(tx);
    }

    // 2. Settler failure rolls back writes and stale marks atomically while
    //    preserving the monotonic allocation high-water mark.
    *appearance.mode.lock().unwrap() = SettlerMode::AllocateThenFail;
    {
        let mut tx = doc.begin();
        tx.write(1, &Value::Integer(5), &budget).unwrap();
        tx.mark(Structure::Appearance, 1, &budget).unwrap();
        let next_before = tx.version().next_number();
        let err = tx.settle(Scope::Commit, &budget).unwrap_err();
        assert!(err.to_string().contains("injected settler refusal"));
        // Atomic rollback: draft value and stale mark restored to pre-settle state.
        assert_eq!(read_int(tx.version(), 1, &budget), Some(5));
        assert!(tx.is_stale(Structure::Appearance, 1));
        // High-water mark never decreases across rollback.
        assert!(tx.version().next_number() > next_before);
        drop(tx);
    }

    // 3. Self-marking cycle refuses after MAX_ROUNDS and rolls back atomically.
    *appearance.mode.lock().unwrap() = SettlerMode::SelfCycle;
    {
        let mut tx = doc.begin();
        tx.write(1, &Value::Integer(7), &budget).unwrap();
        tx.mark(Structure::Appearance, 1, &budget).unwrap();
        let err = tx.settle(Scope::Commit, &budget).unwrap_err();
        assert!(err.to_string().contains("settling did not finish"));
        assert_eq!(read_int(tx.version(), 1, &budget), Some(7));
        assert!(tx.is_stale(Structure::Appearance, 1));
        drop(tx);
    }
}
