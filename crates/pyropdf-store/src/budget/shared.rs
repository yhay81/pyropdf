//! The frozen budget of an operation that several calls share.

use std::ops::Deref;
use std::sync::Arc;

use super::{Budget, CODES, Deadline, LimitCode};
use crate::LimitExceeded;

/// The budget of one operation, frozen so that several calls can share it.
///
/// [`Budget::share`] makes one. Its clones are the same operation: they
/// charge the same meters and observe the same limits, deadline and
/// cancellation tokens. Its configuration cannot change afterwards, so a
/// call that reads a limit once, as a content parser does, never misses a
/// later change. A call that needs a lower limit or another document's
/// limits takes a view with [`SharedBudget::tightened`] or
/// [`SharedBudget::constrained_by`]: the view charges the operation's
/// meters and can only be stricter than the operation.
#[derive(Clone, Debug)]
pub struct SharedBudget(Arc<Budget>);

impl Budget {
    /// Freezes this budget's configuration for an operation whose calls
    /// share it.
    #[must_use]
    pub fn share(self) -> SharedBudget {
        SharedBudget(Arc::new(self))
    }
}

impl Deref for SharedBudget {
    type Target = Budget;

    fn deref(&self) -> &Budget {
        &self.0
    }
}

impl SharedBudget {
    /// Returns whether `self` and `other` are the same clone family, rather
    /// than two views of an operation or two operations.
    #[must_use]
    pub fn ptr_eq(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.0, &other.0)
    }

    /// Returns whether `self` and `other` charge the same meters: views of
    /// one operation.
    #[must_use]
    pub fn same_operation(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.0.used, &other.0.used)
    }

    /// Returns a view of this operation whose limit of `code` is at most
    /// `limit`.
    ///
    /// Refuses with `code`, after [`Budget::poll`], when the operation has
    /// already charged more than the view allows.
    pub fn tightened(&self, code: LimitCode, limit: u64) -> Result<Self, LimitExceeded> {
        let mut limits = self.limits;
        limits[code.index()] = Some(limit);
        self.view(&limits, self.deadline, None)
    }

    /// Returns a view of this operation that also observes `other`'s
    /// limits, deadline and cancellation tokens, as an operation does when it
    /// reads another document.
    ///
    /// Each limit is the lower of the two, the deadline is the earlier, and
    /// the view stops when a token of either is cancelled. Refuses, after
    /// [`Budget::poll`], when the operation has already charged more than the
    /// view allows.
    pub fn constrained_by(&self, other: &Budget) -> Result<Self, LimitExceeded> {
        let deadline = match (self.deadline, other.deadline) {
            (Some(a), Some(b)) => Some(if a.end <= b.end { a } else { b }),
            (a, b) => a.or(b),
        };
        self.view(&other.limits, deadline, Some(&other.cancellation))
    }

    /// Returns a view with the lower of this operation's limits and
    /// `limits`, `deadline`, and the tokens of both.
    fn view(
        &self,
        limits: &[Option<u64>; CODES],
        deadline: Option<Deadline>,
        tokens: Option<&[super::CancellationToken]>,
    ) -> Result<Self, LimitExceeded> {
        let mut cancellation = self.cancellation.clone();
        if let Some(tokens) = tokens {
            #[expect(
                clippy::disallowed_methods,
                reason = "the tokens are the two operations' configurations, not PDF input"
            )]
            cancellation.extend_from_slice(tokens);
        }
        let mut view = Budget {
            limits: self.limits,
            used: Arc::clone(&self.used),
            deadline,
            cancellation,
            tightened: self.tightened,
        };
        for code in LimitCode::ALL {
            let index = code.index();
            if let Some(limit) = limits[index]
                && view.limits[index].is_none_or(|current| limit < current)
            {
                view.limits[index] = Some(limit);
                view.tightened[index] = true;
            }
        }
        view.poll()?;
        Ok(Self(Arc::new(view)))
    }
}
