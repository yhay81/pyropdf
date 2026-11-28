//! Exhaustive concurrency model checking for `pyropdf-store` and the
//! `pyropdf-python` document lifecycle protocol (#1249).
//!
//! Run with:
//! ```bash
//! RUSTFLAGS="--cfg loom" cargo test --locked -p pyropdf-store --lib --release -- loom_tests
//! ```

use std::sync::Arc;

use loom::cell::UnsafeCell;
use loom::thread;

use crate::budget::{Budget, CancellationToken, DocumentLimits, LimitCode};
use crate::memo::Memo;
use crate::sync::atomic::{AtomicBool, Ordering};
use crate::sync::{Condvar, Mutex, PoisonError, RwLock};
use crate::value::{Bytes, Dict, Name, Reference, Value};
use crate::version::{Document, HistoryStatus, MemoryBase, RestoredHistory, Version};

fn memory_document(history_limit: Option<u64>) -> Document {
    let mut base = MemoryBase::new(
        [
            (Name::from("Root"), Value::from(Reference::new(1, 0))),
            (Name::from("Size"), Value::from(3)),
        ]
        .into_iter()
        .collect::<Dict>(),
    );
    base.insert(Reference::new(1, 0), Bytes::new(&b"1"[..]));
    base.insert(Reference::new(2, 0), Bytes::new(&b"2"[..]));
    Document::new(Arc::new(base), history_limit)
}

fn read_int(version: &Version, number: u32, budget: &Budget) -> Option<i64> {
    match version.get(number, budget).unwrap() {
        Some(obj) => match **obj.value() {
            Value::Integer(n) => Some(n),
            _ => panic!("expected integer object {number}"),
        },
        None => None,
    }
}

fn small_budget() -> Budget {
    Budget::new(&DocumentLimits::default())
}

#[test]
fn loom_document_concurrent_writes_and_reader() {
    loom::model(|| {
        let doc = Arc::new(memory_document(None));

        let d1 = Arc::clone(&doc);
        let t1 = thread::spawn(move || {
            let budget = small_budget();
            let mut tx = d1.begin();
            let allocated = tx.allocate().unwrap();
            tx.write(1, &Value::Integer(10), &budget).unwrap();
            tx.write(allocated.number(), &Value::Integer(100), &budget)
                .unwrap();
            tx.commit(&budget).unwrap();
            allocated.number()
        });

        let d2 = Arc::clone(&doc);
        let t2 = thread::spawn(move || {
            let budget = small_budget();
            let mut tx = d2.begin();
            let allocated = tx.allocate().unwrap();
            tx.write(2, &Value::Integer(20), &budget).unwrap();
            tx.commit(&budget).unwrap();
            allocated.number()
        });

        let d3 = Arc::clone(&doc);
        let t3 = thread::spawn(move || {
            let budget = small_budget();
            let snap = d3.current();
            let v1 = read_int(&snap, 1, &budget).unwrap();
            let v2 = read_int(&snap, 2, &budget).unwrap();
            assert!(v1 == 1 || v1 == 10, "unexpected v1={v1}");
            assert!(v2 == 2 || v2 == 20, "unexpected v2={v2}");
            // Readers see complete versions only: once v1 == 10 is published,
            // the object allocated in that same transaction is also visible.
            if v1 == 10 {
                let found_100 = (3..=5).any(|n| read_int(&snap, n, &budget) == Some(100));
                assert!(found_100, "v1=10 published without its allocated object");
            }
        });

        let n1 = t1.join().unwrap();
        let n2 = t2.join().unwrap();
        t3.join().unwrap();

        assert_ne!(n1, n2, "concurrent transactions reused an object number");
        assert!(n1 >= 3 && n2 >= 3);

        let budget = small_budget();
        let final_ver = doc.current();
        assert_eq!(read_int(&final_ver, 1, &budget), Some(10));
        assert_eq!(read_int(&final_ver, 2, &budget), Some(20));
        assert_eq!(read_int(&final_ver, n1, &budget), Some(100));
    });
}

#[test]
fn loom_document_dropped_transaction_wakes_waiter_and_preserves_hwm() {
    loom::model(|| {
        let doc = Arc::new(memory_document(None));

        let d1 = Arc::clone(&doc);
        let t1 = thread::spawn(move || {
            let budget = small_budget();
            let mut tx = d1.begin();
            let allocated = tx.allocate().unwrap();
            tx.write(1, &Value::Integer(99), &budget).unwrap();
            drop(tx);
            allocated.number()
        });

        let d2 = Arc::clone(&doc);
        let t2 = thread::spawn(move || {
            let budget = small_budget();
            let mut tx = d2.begin();
            let allocated = tx.allocate().unwrap();
            tx.write(2, &Value::Integer(22), &budget).unwrap();
            tx.commit(&budget).unwrap();
            allocated.number()
        });

        let n1 = t1.join().unwrap();
        let n2 = t2.join().unwrap();

        assert_ne!(
            n1, n2,
            "dropped transaction's reserved object number was reissued"
        );
        let budget = small_budget();
        let current = doc.current();
        assert_eq!(read_int(&current, 1, &budget), Some(1));
        assert_eq!(read_int(&current, 2, &budget), Some(22));
    });
}

#[test]
fn loom_document_undo_redo_and_reader_isolation() {
    loom::model(|| {
        let doc = Arc::new(memory_document(None));
        let budget = small_budget();
        let allocated_initial = {
            let mut tx = doc.begin();
            let r = tx.allocate().unwrap();
            tx.write(1, &Value::Integer(10), &budget).unwrap();
            tx.commit(&budget).unwrap();
            r.number()
        };
        let pinned = doc.current();

        let d1 = Arc::clone(&doc);
        let t1 = thread::spawn(move || {
            let _ = d1.undo();
            let _ = d1.redo();
        });

        let d2 = Arc::clone(&doc);
        let t2 = thread::spawn(move || {
            let budget = small_budget();
            let mut tx = d2.begin();
            let allocated = tx.allocate().unwrap();
            tx.write(2, &Value::Integer(20), &budget).unwrap();
            tx.commit(&budget).unwrap();
            allocated.number()
        });

        t1.join().unwrap();
        let allocated_after = t2.join().unwrap();

        assert!(
            allocated_after > allocated_initial,
            "undo lowered allocation high-water mark: initial={allocated_initial}, after={allocated_after}"
        );
        // Pinned version remains immutable across concurrent undo/redo/commit.
        assert_eq!(read_int(&pinned, 1, &budget), Some(10));
        assert_eq!(read_int(&pinned, 2, &budget), Some(2));
    });
}

#[test]
fn loom_memo_concurrent_reuse_and_invalidation() {
    loom::model(|| {
        let doc = memory_document(None);
        let budget = small_budget();
        let v0 = doc.current();
        let v1 = {
            let mut tx = doc.begin();
            tx.write(1, &Value::Integer(10), &budget).unwrap();
            tx.commit(&budget).unwrap()
        };
        let v2 = {
            let mut tx = doc.begin();
            // Disjoint from object 1: result computed on v1 is valid on v2,
            // while result computed on v0 is NOT valid on v1 or v2.
            tx.write(2, &Value::Integer(20), &budget).unwrap();
            tx.commit(&budget).unwrap()
        };

        let memo = Arc::new(Memo::<u32, i64>::new(2));
        // Seed the memo on v1 so both reuse-hit (v2) and invalidation (v0)
        // race concurrently.
        let seeded = memo
            .get_or_compute(&0, &v1, &budget, |reads| {
                Ok::<_, crate::Error>(match **reads.get(1)?.unwrap().value() {
                    Value::Integer(n) => n,
                    _ => unreachable!(),
                })
            })
            .unwrap();
        assert_eq!(*seeded, 10);

        let m1 = Arc::clone(&memo);
        let v2_clone = Arc::clone(&v2);
        let t1 = thread::spawn(move || {
            let budget = small_budget();
            let val = m1
                .get_or_compute(&0, &v2_clone, &budget, |reads| {
                    Ok::<_, crate::Error>(match **reads.get(1)?.unwrap().value() {
                        Value::Integer(n) => n,
                        _ => unreachable!(),
                    })
                })
                .unwrap();
            assert_eq!(*val, 10);
        });

        let m2 = Arc::clone(&memo);
        let v0_clone = Arc::clone(&v0);
        let t2 = thread::spawn(move || {
            let budget = small_budget();
            let val = m2
                .get_or_compute(&0, &v0_clone, &budget, |reads| {
                    Ok::<_, crate::Error>(match **reads.get(1)?.unwrap().value() {
                        Value::Integer(n) => n,
                        _ => unreachable!(),
                    })
                })
                .unwrap();
            assert_eq!(*val, 1);
        });

        t1.join().unwrap();
        t2.join().unwrap();

        // Re-check soundness on v2 after both concurrent updates settle.
        let check = memo
            .get_or_compute(&0, &v2, &budget, |reads| {
                Ok::<_, crate::Error>(match **reads.get(1)?.unwrap().value() {
                    Value::Integer(n) => n,
                    _ => unreachable!(),
                })
            })
            .unwrap();
        assert_eq!(*check, 10);
    });
}

#[test]
fn loom_memo_capacity_eviction_under_concurrency() {
    loom::model(|| {
        let doc = memory_document(None);
        let v0 = doc.current();
        let memo = Arc::new(Memo::<u32, i64>::new(1));

        let m1 = Arc::clone(&memo);
        let v_a = Arc::clone(&v0);
        let t1 = thread::spawn(move || {
            let budget = small_budget();
            let val = m1
                .get_or_compute(&1, &v_a, &budget, |reads| {
                    Ok::<_, crate::Error>(match **reads.get(1)?.unwrap().value() {
                        Value::Integer(n) => n,
                        _ => unreachable!(),
                    })
                })
                .unwrap();
            assert_eq!(*val, 1);
        });

        let m2 = Arc::clone(&memo);
        let v_b = Arc::clone(&v0);
        let t2 = thread::spawn(move || {
            let budget = small_budget();
            let val = m2
                .get_or_compute(&2, &v_b, &budget, |reads| {
                    Ok::<_, crate::Error>(match **reads.get(2)?.unwrap().value() {
                        Value::Integer(n) => n,
                        _ => unreachable!(),
                    })
                })
                .unwrap();
            assert_eq!(*val, 2);
        });

        t1.join().unwrap();
        t2.join().unwrap();
        assert!(memo.len() <= 1, "memo exceeded capacity: {}", memo.len());
    });
}

#[test]
fn loom_budget_shared_and_tightened_cas_never_exceeds_limit() {
    loom::model(|| {
        let shared = Budget::new(&DocumentLimits::default())
            .with_limit(LimitCode::MemorySize, Some(10))
            .share();
        let tightened = shared.tightened(LimitCode::MemorySize, 7).unwrap();

        let b1 = shared.clone();
        let t1 = thread::spawn(move || {
            if b1.charge(LimitCode::MemorySize, 8).is_ok() {
                assert!(b1.used(LimitCode::MemorySize) <= 10);
                b1.release(LimitCode::MemorySize, 8);
            }
        });

        let b2 = tightened.clone();
        let t2 = thread::spawn(move || match b2.charge(LimitCode::MemorySize, 4) {
            Ok(()) => {
                // Tightened view itself never charges beyond 7 when added alone;
                // combined with b1 it must never exceed the base limit 10.
                assert!(b2.used(LimitCode::MemorySize) <= 10);
                b2.release(LimitCode::MemorySize, 4);
            }
            Err(err) => {
                assert_eq!(err.code(), LimitCode::MemorySize);
                if let (Some(limit), Some(amount)) = (err.limit(), err.amount()) {
                    assert!(
                        amount > limit,
                        "torn LimitExceeded error: amount={amount} <= limit={limit}"
                    );
                }
            }
        });

        t1.join().unwrap();
        t2.join().unwrap();
        assert_eq!(
            shared.used(LimitCode::MemorySize),
            0,
            "charged memory leaked after releases"
        );
    });
}

#[test]
fn loom_budget_cancellation_is_monotonic() {
    loom::model(|| {
        let token = CancellationToken::new();
        let budget = Budget::new(&DocumentLimits::default())
            .with_cancellation(token.clone())
            .share();

        let tok = token.clone();
        let t1 = thread::spawn(move || {
            tok.cancel();
        });

        let b = budget.clone();
        let t2 = thread::spawn(move || {
            if let Err(err) = b.poll() {
                assert_eq!(err.code(), LimitCode::Cancelled);
                // Once observed cancelled, every later poll stays cancelled.
                assert_eq!(b.poll().unwrap_err().code(), LimitCode::Cancelled);
            }
        });

        t1.join().unwrap();
        t2.join().unwrap();
        assert_eq!(budget.poll().unwrap_err().code(), LimitCode::Cancelled);
    });
}

#[test]
fn loom_recipient_owner_acquire_release_visibility() {
    // Mirrors `RecipientAccess::owner` in `crates/pyropdf-store/src/security/recipient.rs`:
    // filter keys are published before `owner.store(true, Ordering::Release)` and
    // callers observe `owner.load(Ordering::Acquire)` before reading them.
    struct RecipientState {
        retained_key: UnsafeCell<u32>,
        owner: AtomicBool,
    }
    unsafe impl Sync for RecipientState {}

    loom::model(|| {
        let state = Arc::new(RecipientState {
            retained_key: UnsafeCell::new(0),
            owner: AtomicBool::new(false),
        });

        let s1 = Arc::clone(&state);
        let writer = thread::spawn(move || {
            s1.retained_key.with_mut(|ptr| unsafe { *ptr = 0xCAFE });
            s1.owner.store(true, Ordering::Release);
        });

        let s2 = Arc::clone(&state);
        let reader = thread::spawn(move || {
            if s2.owner.load(Ordering::Acquire) {
                let key = s2.retained_key.with(|ptr| unsafe { *ptr });
                assert_eq!(key, 0xCAFE);
            }
        });

        writer.join().unwrap();
        reader.join().unwrap();
    });
}

/// Mirrors the `pyropdf-python` `_Document` + `history::State` lock discipline
/// (`WriteGate` -> `document: RwLock<Option<Arc<Document>>>` -> `steps: Mutex<Steps>`
/// -> `store.writer`) over real `pyropdf_store::version::Document` instances.
struct LifecycleHarness {
    gate_held: Mutex<bool>,
    gate_changed: Condvar,
    document: RwLock<Option<Arc<Document>>>,
    steps: Mutex<Steps>,
}

#[derive(Default)]
struct Steps {
    names: Vec<&'static str>,
    position: usize,
}

struct GateGuard<'a>(&'a LifecycleHarness);

impl Drop for GateGuard<'_> {
    fn drop(&mut self) {
        let mut held = self
            .0
            .gate_held
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        *held = false;
        self.0.gate_changed.notify_all();
    }
}

impl LifecycleHarness {
    fn new(doc: Document) -> Self {
        Self {
            gate_held: Mutex::new(false),
            gate_changed: Condvar::new(),
            document: RwLock::new(Some(Arc::new(doc))),
            steps: Mutex::new(Steps::default()),
        }
    }

    fn write_lock(&self) -> GateGuard<'_> {
        let mut held = self
            .gate_held
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        while *held {
            held = self
                .gate_changed
                .wait(held)
                .unwrap_or_else(PoisonError::into_inner);
        }
        *held = true;
        GateGuard(self)
    }

    fn sync_steps(steps: &mut Steps, store: &Document) -> HistoryStatus {
        let status = store.history();
        let position = steps.position;
        let undo = position.min(status.undo);
        let redo = (steps.names.len() - position).min(status.redo);
        steps.names.truncate(position + redo);
        steps.names.drain(..position - undo);
        steps.position = undo;
        status
    }

    fn commit(&self, name: &'static str, value: i64) -> bool {
        let _gate = self.write_lock();
        let store = match self
            .document
            .read()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
        {
            Some(store) => store,
            None => return false,
        };
        let budget = small_budget();
        let mut tx = store.begin();
        tx.write(1, &Value::Integer(value), &budget).unwrap();
        let parent = Arc::clone(tx.parent());
        let mut steps = self.steps.lock().unwrap_or_else(PoisonError::into_inner);
        let committed = tx.commit(&budget).unwrap();
        if !Arc::ptr_eq(&parent, &committed) {
            let pos = steps.position;
            steps.names.truncate(pos);
            steps.names.push(name);
            steps.position += 1;
        }
        Self::sync_steps(&mut steps, &store);
        true
    }

    fn replace(&self, next: Document) -> bool {
        let _gate = self.write_lock();
        let mut guard = self
            .document
            .write()
            .unwrap_or_else(PoisonError::into_inner);
        if guard.is_none() {
            return false;
        }
        *guard = Some(Arc::new(next));
        *self.steps.lock().unwrap_or_else(PoisonError::into_inner) = Steps::default();
        true
    }

    fn close(&self) {
        let _gate = self.write_lock();
        let mut guard = self
            .document
            .write()
            .unwrap_or_else(PoisonError::into_inner);
        guard.take();
        *self.steps.lock().unwrap_or_else(PoisonError::into_inner) = Steps::default();
    }

    fn history_state(&self) -> Option<(Vec<&'static str>, usize, HistoryStatus)> {
        let guard = self.document.read().unwrap_or_else(PoisonError::into_inner);
        let store = guard.as_deref()?;
        let mut steps = self.steps.lock().unwrap_or_else(PoisonError::into_inner);
        let status = Self::sync_steps(&mut steps, store);
        assert_eq!(steps.position, status.undo);
        assert_eq!(steps.names.len() - steps.position, status.redo);
        Some((steps.names.clone(), steps.position, status))
    }

    fn set_history_limit(&self, limit: Option<u64>) -> bool {
        let guard = self.document.read().unwrap_or_else(PoisonError::into_inner);
        let Some(store) = guard.as_deref() else {
            return false;
        };
        let mut steps = self.steps.lock().unwrap_or_else(PoisonError::into_inner);
        store.set_history_limit(limit);
        Self::sync_steps(&mut steps, store);
        true
    }
}

#[test]
fn loom_lifecycle_replace_commit_and_history_state_never_corrupt_steps() {
    // Verifies the exact interleaving that previously let a stale `store` in
    // `history_state` / `set_history_limit` truncate the replacement document's
    // step names after `replace()` + `commit()`.
    loom::model(|| {
        let harness = Arc::new(LifecycleHarness::new(memory_document(None)));

        let h1 = Arc::clone(&harness);
        let t1 = thread::spawn(move || {
            assert!(h1.replace(memory_document(None)));
            assert!(h1.commit("after-replace", 42));
        });

        let h2 = Arc::clone(&harness);
        let t2 = thread::spawn(move || {
            let _ = h2.history_state();
            let _ = h2.set_history_limit(None);
        });

        t1.join().unwrap();
        t2.join().unwrap();

        let (names, pos, status) = harness.history_state().unwrap();
        assert_eq!(names, vec!["after-replace"]);
        assert_eq!(pos, 1);
        assert_eq!(status.undo, 1);
        assert_eq!(status.redo, 0);
    });
}

#[test]
fn loom_lifecycle_close_never_resurrects_under_concurrent_replace() {
    loom::model(|| {
        let harness = Arc::new(LifecycleHarness::new(memory_document(None)));

        let h1 = Arc::clone(&harness);
        let t1 = thread::spawn(move || {
            h1.close();
        });

        let h2 = Arc::clone(&harness);
        let t2 = thread::spawn(move || {
            let _ = h2.replace(memory_document(None));
        });

        let h3 = Arc::clone(&harness);
        let t3 = thread::spawn(move || {
            let _ = h3.history_state();
        });

        t1.join().unwrap();
        t2.join().unwrap();
        t3.join().unwrap();

        assert!(
            harness.history_state().is_none(),
            "document resurrected after close()"
        );
        let steps = harness.steps.lock().unwrap_or_else(PoisonError::into_inner);
        assert!(steps.names.is_empty());
        assert_eq!(steps.position, 0);
    });
}

#[test]
fn loom_document_restore_history_races_with_commit() {
    // Save a valid history once inside a single-threaded loom model, then
    // exhaustively interleave `restore_history` against a concurrent commit on
    // a fresh opened document sharing the same base.
    let saved = Arc::new(std::sync::OnceLock::<Vec<u8>>::new());
    let saved_init = Arc::clone(&saved);
    loom::model(move || {
        let _ = saved_init.get_or_init(|| {
            let doc = memory_document(None);
            let budget = small_budget();
            let mut tx = doc.begin();
            tx.write(1, &Value::Integer(77), &budget).unwrap();
            tx.commit(&budget).unwrap();
            doc.save_history(&["restored".into()], 1, &budget).unwrap()
        });
    });
    let saved = Arc::new(saved.get().unwrap().clone());

    loom::model(move || {
        let doc = Arc::new(memory_document(None));
        let data = Arc::clone(&saved);

        let d1 = Arc::clone(&doc);
        let t1 = thread::spawn(move || {
            let budget = small_budget();
            d1.restore_history(&data, &budget).unwrap()
        });

        let d2 = Arc::clone(&doc);
        let t2 = thread::spawn(move || {
            let budget = small_budget();
            let mut tx = d2.begin();
            tx.write(2, &Value::Integer(88), &budget).unwrap();
            tx.commit(&budget).unwrap();
        });

        let restored = t1.join().unwrap();
        t2.join().unwrap();

        let budget = small_budget();
        let current = doc.current();
        assert_eq!(read_int(&current, 2, &budget), Some(88));
        match restored {
            RestoredHistory::Restored { labels, position } => {
                assert_eq!(labels, vec!["restored".to_string()]);
                assert_eq!(position, 1);
                assert_eq!(read_int(&current, 1, &budget), Some(77));
            }
            RestoredHistory::Changed => {
                // Commit ran before restore_history acquired the writer lock;
                // restore must leave the document untouched.
                assert_eq!(read_int(&current, 1, &budget), Some(1));
            }
            RestoredHistory::OtherDocument => panic!("unexpected OtherDocument for identical base"),
        }
    });
}

#[test]
fn loom_memo_weak_version_drop_during_reuse() {
    loom::model(|| {
        let doc = memory_document(Some(0));
        let budget = small_budget();
        let v1 = {
            let mut tx = doc.begin();
            tx.write(1, &Value::Integer(10), &budget).unwrap();
            tx.commit(&budget).unwrap()
        };
        let v2 = {
            let mut tx = doc.begin();
            tx.write(2, &Value::Integer(20), &budget).unwrap();
            tx.commit(&budget).unwrap()
        };

        let memo = Arc::new(Memo::<u32, i64>::new(2));
        let seeded = memo
            .get_or_compute(&0, &v1, &budget, |reads| {
                Ok::<_, crate::Error>(match **reads.get(1)?.unwrap().value() {
                    Value::Integer(n) => n,
                    _ => unreachable!(),
                })
            })
            .unwrap();
        assert_eq!(*seeded, 10);

        // Thread 1 drops the sole strong reference to `v1` while Thread 2
        // looks up `v2` and attempts `Weak::upgrade()` in `Memo::reuse`.
        let t1 = thread::spawn(move || {
            drop(v1);
        });

        let m2 = Arc::clone(&memo);
        let v2_clone = Arc::clone(&v2);
        let t2 = thread::spawn(move || {
            let budget = small_budget();
            let val = m2
                .get_or_compute(&0, &v2_clone, &budget, |reads| {
                    Ok::<_, crate::Error>(match **reads.get(1)?.unwrap().value() {
                        Value::Integer(n) => n,
                        _ => unreachable!(),
                    })
                })
                .unwrap();
            assert_eq!(*val, 10);
        });

        t1.join().unwrap();
        t2.join().unwrap();
    });
}
