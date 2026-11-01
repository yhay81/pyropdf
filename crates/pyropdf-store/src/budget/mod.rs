//! One budget per operation (DESIGN.md F6 and section 9).
//!
//! An operation's [`Budget`] comes from the document's [`DocumentLimits`] and
//! the call's `max_*` options. It holds a meter and an optional limit for
//! every code of the registry, limits.toml: allocated bytes, elements,
//! decoded bytes, and interpreter fuel are each charged to the code that
//! bounds them. Every charge first checks the deadline and the cancellation
//! tokens. A refused charge changes nothing, so a refusal leaves no partial
//! result.
//!
//! Collections that grow from input are [`BudgetVec`]s, whose growth is
//! fallible and charged to the budget by default, and a stream's decoded
//! bytes go through a [`DecodeMeter`].

mod cancel;
mod code;
mod decode;
mod limits;
mod vec;

#[cfg(test)]
mod tests;

use std::fmt;
use std::sync::Arc;
use std::sync::atomic::AtomicU64;
use std::sync::atomic::Ordering::Relaxed;
use std::time::{Duration, Instant};

pub use cancel::CancellationToken;
pub use code::LimitCode;
pub use decode::{DecodeMeter, StreamKind};
pub use limits::DocumentLimits;
pub use vec::BudgetVec;

use crate::LimitExceeded;

/// The number of codes, which sizes the tables of a budget.
const CODES: usize = LimitCode::ALL.len();

impl LimitCode {
    /// The position of the code in [`LimitCode::ALL`], which indexes the
    /// tables of a budget.
    const fn index(self) -> usize {
        self as usize
    }
}

impl fmt::Display for LimitCode {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// The resources of one operation: a meter and an optional limit for each
/// limit code, a deadline, and cancellation tokens.
///
/// The threads of one operation share its budget by reference and charge the
/// same meters. [`Budget::charge`] counts against a code's limit,
/// [`Budget::release`] returns what a freed collection charged, and
/// [`Budget::check`] compares one measurement, such as the depth of an
/// object, with a limit without counting it.
#[derive(Debug)]
pub struct Budget {
    limits: [Option<u64>; CODES],
    used: Arc<[AtomicU64; CODES]>,
    deadline: Option<Deadline>,
    cancellation: Vec<CancellationToken>,
    /// A view can never relax its originating operation, even if it outlives it.
    shared: bool,
}

impl Clone for Budget {
    fn clone(&self) -> Self {
        Self {
            limits: self.limits,
            used: Arc::clone(&self.used),
            deadline: self.deadline,
            cancellation: self.cancellation.clone(),
            shared: true,
        }
    }
}

/// When an operation's deadline was set and when it passes.
#[derive(Clone, Copy, Debug)]
struct Deadline {
    start: Instant,
    end: Instant,
}

impl Budget {
    /// Returns the budget of an operation on a document with `limits`,
    /// starting now.
    ///
    /// Each code starts with its default limit from the registry, and each
    /// limit that `limits` sets replaces it.
    #[must_use]
    pub fn new(limits: &DocumentLimits) -> Self {
        let mut budget = Self {
            limits: LimitCode::ALL.map(LimitCode::default_limit),
            used: Arc::new(std::array::from_fn(|_| AtomicU64::new(0))),
            deadline: None,
            cancellation: Vec::new(),
            shared: false,
        };
        for (code, limit) in limits.code_limits() {
            if limit.is_some() {
                budget.limits[code.index()] = limit;
            }
        }
        if let Some(duration) = limits.max_duration {
            budget = budget.with_timeout(duration);
        }
        if let Some(token) = &limits.cancellation {
            budget = budget.with_cancellation(token.clone());
        }
        budget
    }

    /// Sets the limit of `code`, as a call's `max_*` option does. On a
    /// shared operation, an override can only tighten the existing limit.
    #[must_use]
    pub fn with_limit(mut self, code: LimitCode, limit: Option<u64>) -> Self {
        self.limits[code.index()] = if self.shared || Arc::strong_count(&self.used) > 1 {
            match (self.limits[code.index()], limit) {
                (Some(a), Some(b)) => Some(a.min(b)),
                (Some(a), None) => Some(a),
                (None, other) => other,
            }
        } else {
            limit
        };
        self
    }

    /// Returns a view of this operation with one call-specific limit.
    ///
    /// The returned view shares all meters, deadline, and cancellation with
    /// this budget. The view may tighten but cannot relax an existing limit;
    /// allocations and work still accumulate against the same operation.
    #[must_use]
    pub fn shared_with_limit(&self, code: LimitCode, limit: Option<u64>) -> Self {
        let mut limits = self.limits;
        let current = limits[code.index()];
        limits[code.index()] = match (current, limit) {
            (Some(a), Some(b)) => Some(a.min(b)),
            (Some(a), None) => Some(a),
            (None, other) => other,
        };
        Self {
            limits,
            used: Arc::clone(&self.used),
            deadline: self.deadline,
            cancellation: self.cancellation.clone(),
            shared: true,
        }
    }

    /// Returns a shared view constrained by another document's limits.
    ///
    /// The view charges this operation's meters while observing the more
    /// restrictive limits and earliest deadline, and both cancellation sets.
    #[must_use]
    pub fn constrained_by(&self, other: &Self) -> Self {
        let mut view = self.clone();
        for (limit, other) in view.limits.iter_mut().zip(other.limits) {
            *limit = match (*limit, other) {
                (Some(a), Some(b)) => Some(a.min(b)),
                (Some(a), None) | (None, Some(a)) => Some(a),
                (None, None) => None,
            };
        }
        view.deadline = match (view.deadline, other.deadline) {
            (Some(a), Some(b)) => Some(if a.end <= b.end { a } else { b }),
            (Some(a), None) | (None, Some(a)) => Some(a),
            (None, None) => None,
        };
        #[expect(
            clippy::disallowed_methods,
            reason = "tokens are supplied by the two operation configurations, not by PDF input"
        )]
        view.cancellation.extend(other.cancellation.iter().cloned());
        view
    }

    /// Adds a deadline. The earliest deadline of the budget applies.
    #[must_use]
    pub fn with_deadline(self, deadline: Instant) -> Self {
        self.add_deadline(Instant::now(), deadline)
    }

    /// Adds a deadline `duration` from now. A duration beyond the range of
    /// the clock adds none.
    #[must_use]
    pub fn with_timeout(self, duration: Duration) -> Self {
        let now = Instant::now();
        match now.checked_add(duration) {
            Some(deadline) => self.add_deadline(now, deadline),
            None => self,
        }
    }

    /// Adds a cancellation token. The operation stops once any of its tokens
    /// is cancelled.
    #[must_use]
    pub fn with_cancellation(mut self, token: CancellationToken) -> Self {
        #[expect(
            clippy::disallowed_methods,
            reason = "the caller adds each token, so the list grows with its calls, not with input"
        )]
        self.cancellation.push(token);
        self
    }

    /// Adds a deadline that passes at `end`, set at `now`.
    fn add_deadline(mut self, now: Instant, end: Instant) -> Self {
        self.deadline = Some(match self.deadline {
            Some(current) => Deadline {
                start: current.start,
                end: current.end.min(end),
            },
            None => Deadline { start: now, end },
        });
        self
    }

    /// Returns the limit of `code`, or `None` if it has none.
    #[must_use]
    pub const fn limit(&self, code: LimitCode) -> Option<u64> {
        self.limits[code.index()]
    }

    /// Returns what has been charged to `code` and not released.
    #[must_use]
    pub fn used(&self, code: LimitCode) -> u64 {
        self.used[code.index()].load(Relaxed)
    }

    /// Returns what `code` may still be charged, or `None` if it has no
    /// limit.
    #[must_use]
    pub fn remaining(&self, code: LimitCode) -> Option<u64> {
        self.limit(code)
            .map(|limit| limit.saturating_sub(self.used(code)))
    }

    /// Returns when the deadline passes, if the budget has one.
    #[must_use]
    pub fn deadline(&self) -> Option<Instant> {
        self.deadline.map(|deadline| deadline.end)
    }

    /// Charges `amount` to the meter of `code`.
    ///
    /// After [`Budget::poll`], refuses with `code` when the meter would pass
    /// the code's limit. A refused charge leaves the meter as it was. Without
    /// a limit the meter only counts, and saturates rather than wraps.
    pub fn charge(&self, code: LimitCode, amount: u64) -> Result<(), LimitExceeded> {
        self.poll()?;
        self.add(code, amount)
    }

    /// Charges `amount` to the meter of `code` without polling.
    fn add(&self, code: LimitCode, amount: u64) -> Result<(), LimitExceeded> {
        let meter = &self.used[code.index()];
        let Some(limit) = self.limit(code) else {
            meter.update(Relaxed, Relaxed, |used| used.saturating_add(amount));
            return Ok(());
        };
        meter
            .try_update(Relaxed, Relaxed, |used| {
                used.checked_add(amount).filter(|&total| total <= limit)
            })
            .map(drop)
            .map_err(|used| LimitExceeded::over(code, limit, used.saturating_add(amount)))
    }

    /// Returns `amount` to the meter of `code`, as a collection does when it
    /// frees what it charged.
    pub fn release(&self, code: LimitCode, amount: u64) {
        self.used[code.index()].update(Relaxed, Relaxed, |used| used.saturating_sub(amount));
    }

    /// Checks one measurement against the limit of `code` without charging
    /// it, for a limit on one item, such as the size of an input or the depth
    /// of an object, rather than on a total.
    ///
    /// After [`Budget::poll`], refuses with `code` when `value` is above the
    /// limit.
    pub fn check(&self, code: LimitCode, value: u64) -> Result<(), LimitExceeded> {
        self.poll()?;
        match self.limit(code) {
            Some(limit) if value > limit => Err(LimitExceeded::over(code, limit, value)),
            _ => Ok(()),
        }
    }

    /// Refuses with `cancelled` once a cancellation token of the budget is
    /// cancelled, and then with `deadline` once the deadline has passed.
    ///
    /// Every charge and check polls first. Work that runs long without
    /// charging anything polls on its own.
    pub fn poll(&self) -> Result<(), LimitExceeded> {
        if self
            .cancellation
            .iter()
            .any(CancellationToken::is_cancelled)
        {
            return Err(LimitExceeded::new(LimitCode::Cancelled));
        }
        if let Some(Deadline { start, end }) = self.deadline {
            let now = Instant::now();
            if now >= end {
                return Err(LimitExceeded::over(
                    LimitCode::Deadline,
                    floor_millis(end.saturating_duration_since(start)),
                    ceil_millis(now.saturating_duration_since(start)),
                ));
            }
        }
        Ok(())
    }

    /// Starts metering the decoded bytes of one stream of `kind`.
    #[must_use]
    pub const fn decode(&self, kind: StreamKind) -> DecodeMeter<'_> {
        DecodeMeter::new(self, kind)
    }
}

impl Default for Budget {
    /// Returns the budget of an operation on a document without limits: the
    /// registry's defaults apply.
    fn default() -> Self {
        Self::new(&DocumentLimits::default())
    }
}

/// Returns a duration in whole milliseconds, rounded down.
fn floor_millis(duration: Duration) -> u64 {
    u64::try_from(duration.as_millis()).unwrap_or(u64::MAX)
}

/// Returns a duration in whole milliseconds, rounded up.
fn ceil_millis(duration: Duration) -> u64 {
    u64::try_from(duration.as_nanos().div_ceil(1_000_000)).unwrap_or(u64::MAX)
}
