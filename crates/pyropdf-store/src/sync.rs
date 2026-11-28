//! Synchronization primitives switched to `loom::sync` under
//! `#[cfg(all(test, loom))]` for exhaustive concurrency model checking (#1249).
//!
//! Normal builds re-export `std::sync` directly with zero overhead. `Arc` and
//! `Weak` stay `std::sync` so the store's public API signatures are unchanged.

#[cfg(not(all(test, loom)))]
pub(crate) use std::sync::{Condvar, Mutex, MutexGuard, PoisonError, RwLock, atomic};

#[cfg(all(test, loom))]
pub(crate) use loom::sync::{Condvar, Mutex, MutexGuard, RwLock, atomic};
#[cfg(all(test, loom))]
pub(crate) use std::sync::PoisonError;
