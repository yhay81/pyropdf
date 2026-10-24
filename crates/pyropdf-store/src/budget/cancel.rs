//! Cancellation of running operations (DESIGN.md section 9).

use std::sync::Arc;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::Ordering::Relaxed;

/// A flag that stops the operations whose budgets hold it.
///
/// Clones share one flag, so another thread can cancel an operation while it
/// runs. The operation refuses with `cancelled` at its next charge, check, or
/// poll. Cancelling cannot be undone.
#[derive(Clone, Debug, Default)]
pub struct CancellationToken(Arc<AtomicBool>);

impl CancellationToken {
    /// Returns a token that is not cancelled.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Cancels every operation whose budget holds the token or a clone of it.
    pub fn cancel(&self) {
        self.0.store(true, Relaxed);
    }

    /// Returns whether the token is cancelled.
    #[must_use]
    pub fn is_cancelled(&self) -> bool {
        self.0.load(Relaxed)
    }
}

/// Tokens are equal when they share one flag.
impl PartialEq for CancellationToken {
    fn eq(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.0, &other.0)
    }
}

impl Eq for CancellationToken {}
