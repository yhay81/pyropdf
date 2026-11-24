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
//!
//! An operation whose calls each need the budget, such as a canvas and the
//! edits and source reads it makes, freezes its budget with
//! [`Budget::share`]. A [`SharedBudget`] cannot be configured again, so a
//! limit, deadline or cancellation token that a call reads stays true for
//! every clone and view of the operation.

mod cancel;
mod code;
mod decode;
mod hash;
mod limits;
mod shared;
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
pub use hash::{try_insert_map, try_insert_set, try_reserve_map, try_reserve_set};
pub use limits::DocumentLimits;
pub use shared::SharedBudget;
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
///
/// A budget is configured by value and is not `Clone`: an operation that
/// hands its budget to several calls freezes it first with
/// [`Budget::share`].
#[derive(Debug)]
pub struct Budget {
    limits: [Option<u64>; CODES],
    /// The meters, which the views of a [`SharedBudget`] share.
    used: Arc<[AtomicU64; CODES]>,
    deadline: Option<Deadline>,
    cancellation: Vec<CancellationToken>,
    /// The codes whose limit a view of a shared operation tightened below
    /// the operation's own; [`Budget::poll`] refuses once their shared meter
    /// is above the view's limit.
    tightened: [bool; CODES],
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
            tightened: [false; CODES],
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

    /// Sets the limit of `code`, as a call's `max_*` option does, replacing
    /// its default or the document's limit. `None` removes the limit, for
    /// input that the caller trusts.
    #[must_use]
    pub fn with_limit(mut self, code: LimitCode, limit: Option<u64>) -> Self {
        self.limits[code.index()] = limit;
        self
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
    /// cancelled, and then with `deadline` once the deadline has passed. A
    /// view of a shared operation then refuses with a code whose limit it
    /// tightened once the operation's meter is above that limit.
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
        if self.tightened.contains(&true) {
            self.check_tightened()?;
        }
        Ok(())
    }

    /// Refuses with the first code whose limit this view tightened and whose
    /// meter is above it.
    fn check_tightened(&self) -> Result<(), LimitExceeded> {
        for code in LimitCode::ALL {
            if self.tightened[code.index()]
                && let Some(limit) = self.limit(code)
                && self.used(code) > limit
            {
                return Err(LimitExceeded::over(code, limit, self.used(code)));
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

#[cfg(kani)]
impl Budget {
    /// Constant-initialized budget with no limits, avoiding the 90-entry
    /// initialization loop of [`Budget::new`] inside bounded Kani harnesses.
    pub(crate) fn kani_empty() -> Self {
        Self {
            limits: [None; CODES],
            used: Arc::new([const { AtomicU64::new(0) }; CODES]),
            deadline: None,
            cancellation: Vec::new(),
            tightened: [false; CODES],
        }
    }

    /// Single-threaded stub for [`Budget::poll`] when no deadline,
    /// cancellation token, or tightened view is active.
    #[allow(dead_code, reason = "referenced by #[kani::stub] attributes")]
    pub(crate) fn kani_stub_poll(&self) -> Result<(), LimitExceeded> {
        Ok(())
    }

    /// Single-threaded stub for [`Budget::charge`] to avoid unwinding the
    /// `compare_exchange_weak` CAS loop in harnesses verifying callers.
    #[allow(dead_code, reason = "referenced by #[kani::stub] attributes")]
    pub(crate) fn kani_stub_charge(
        &self,
        code: LimitCode,
        amount: u64,
    ) -> Result<(), LimitExceeded> {
        self.kani_stub_add(code, amount)
    }

    /// Single-threaded stub for [`Budget::add`].
    #[allow(dead_code, reason = "referenced by #[kani::stub] attributes")]
    pub(crate) fn kani_stub_add(&self, code: LimitCode, amount: u64) -> Result<(), LimitExceeded> {
        let meter = &self.used[code.index()];
        let used = meter.load(Relaxed);
        let Some(limit) = self.limit(code) else {
            meter.store(used.saturating_add(amount), Relaxed);
            return Ok(());
        };
        match used.checked_add(amount).filter(|&total| total <= limit) {
            Some(total) => {
                meter.store(total, Relaxed);
                Ok(())
            }
            None => Err(LimitExceeded::over(
                code,
                limit,
                used.saturating_add(amount),
            )),
        }
    }

    /// Single-threaded stub for [`Budget::release`].
    #[allow(dead_code, reason = "referenced by #[kani::stub] attributes")]
    pub(crate) fn kani_stub_release(&self, code: LimitCode, amount: u64) {
        let meter = &self.used[code.index()];
        let used = meter.load(Relaxed);
        meter.store(used.saturating_sub(amount), Relaxed);
    }
}

#[cfg(kani)]
mod kani_proofs {
    use super::*;

    /// Verifies the real `AtomicU64::try_update`/`update` charge and release
    /// arithmetic across all `u64` limits and amounts.
    #[kani::proof]
    #[kani::unwind(3)]
    #[kani::stub(Budget::poll, Budget::kani_stub_poll)]
    fn kani_budget_charge_never_exceeds_limit() {
        let limit: u64 = kani::any();
        let a: u64 = kani::any();
        let b: u64 = kani::any();
        let rel: u64 = kani::any();
        let budget = Budget::kani_empty().with_limit(LimitCode::MemorySize, Some(limit));

        let r1 = budget.charge(LimitCode::MemorySize, a);
        assert_eq!(r1.is_ok(), a <= limit);
        let after1 = budget.used(LimitCode::MemorySize);
        assert!(after1 <= limit);
        assert_eq!(after1, if r1.is_ok() { a } else { 0 });

        budget.release(LimitCode::MemorySize, rel);
        let after_rel = budget.used(LimitCode::MemorySize);
        assert_eq!(after_rel, after1.saturating_sub(rel));

        let r2 = budget.charge(LimitCode::MemorySize, b);
        let fits = after_rel.checked_add(b).is_some_and(|t| t <= limit);
        assert_eq!(r2.is_ok(), fits);
        let after2 = budget.used(LimitCode::MemorySize);
        assert!(after2 <= limit);
        assert_eq!(after2, if fits { after_rel + b } else { after_rel });
    }

    /// Verifies `DecodeMeter::charge` obeys all three optional limits and
    /// leaves state unchanged on refusal.
    #[kani::proof]
    #[kani::unwind(4)]
    #[kani::stub(Budget::poll, Budget::kani_stub_poll)]
    #[kani::stub(Budget::add, Budget::kani_stub_add)]
    fn kani_decode_meter_multi_limit() {
        let stream_lim: Option<u64> = kani::any();
        let page_lim: Option<u64> = kani::any();
        let total_lim: Option<u64> = kani::any();
        let is_page: bool = kani::any();
        let c1: u64 = kani::any();
        let c2: u64 = kani::any();

        let budget = Budget::kani_empty()
            .with_limit(LimitCode::DecompressedSize, stream_lim)
            .with_limit(LimitCode::PageContentSize, page_lim)
            .with_limit(LimitCode::TotalDecompressedSize, total_lim);
        let kind = if is_page {
            StreamKind::PageContent
        } else {
            StreamKind::Any
        };
        let mut meter = budget.decode(kind);

        for chunk in [c1, c2] {
            let prev_size = meter.size();
            let prev_total = budget.used(LimitCode::TotalDecompressedSize);
            let res = meter.charge(chunk);
            if res.is_ok() {
                if let Some(lim) = stream_lim {
                    assert!(meter.size() <= lim);
                }
                if is_page && let Some(lim) = page_lim {
                    assert!(meter.size() <= lim);
                }
                if let Some(lim) = total_lim {
                    assert!(budget.used(LimitCode::TotalDecompressedSize) <= lim);
                }
            } else {
                assert_eq!(meter.size(), prev_size);
                assert_eq!(budget.used(LimitCode::TotalDecompressedSize), prev_total);
            }
        }
    }

    /// Verifies `BudgetVec::counting` obeys memory and element limits on all
    /// operations and releases every charge when dropped.
    #[kani::proof]
    #[kani::unwind(6)]
    #[kani::stub(Budget::poll, Budget::kani_stub_poll)]
    #[kani::stub(Budget::add, Budget::kani_stub_add)]
    #[kani::stub(Budget::release, Budget::kani_stub_release)]
    fn kani_budget_vec_charge_and_drop() {
        let mem_limit: u64 = kani::any();
        let count_limit: u64 = kani::any();
        let budget = Budget::kani_empty()
            .with_limit(LimitCode::MemorySize, Some(mem_limit))
            .with_limit(LimitCode::ObjectCount, Some(count_limit));

        {
            let mut v: BudgetVec<'_, u32> = BudgetVec::counting(&budget, LimitCode::ObjectCount);
            for _ in 0..3 {
                let op: u8 = kani::any();
                kani::assume(op < 5);
                match op {
                    0 => {
                        let x: u32 = kani::any();
                        let _ = v.try_push(x);
                    }
                    1 => {
                        let add: usize = kani::any();
                        kani::assume(add <= 4);
                        let _ = v.try_reserve(add);
                    }
                    2 => {
                        let add: usize = kani::any();
                        kani::assume(add <= 4);
                        let _ = v.try_reserve_exact(add);
                    }
                    3 => {
                        let _ = v.pop();
                    }
                    _ => {
                        let new_len: usize = kani::any();
                        kani::assume(new_len <= 4);
                        v.truncate(new_len);
                    }
                }
                assert!(budget.used(LimitCode::MemorySize) <= mem_limit);
                assert!(budget.used(LimitCode::ObjectCount) <= count_limit);
                assert_eq!(budget.used(LimitCode::ObjectCount), v.len() as u64);
            }
        }
        assert_eq!(budget.used(LimitCode::MemorySize), 0);
        assert_eq!(budget.used(LimitCode::ObjectCount), 0);
    }
}
