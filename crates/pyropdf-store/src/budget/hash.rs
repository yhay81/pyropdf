//! Fallible, budgeted reservation and insertion helpers for [`HashSet`] and
//! [`HashMap`] (DESIGN.md F6 and section 9).

use std::collections::{HashMap, HashSet};
use std::hash::{BuildHasher, Hash};

use super::{Budget, LimitCode};
use crate::LimitExceeded;

/// Per-entry charge for a hash table slot: control bytes, load-factor slack,
/// and transient rehash capacity for `Entry`.
const fn entry_bytes<Entry>() -> usize {
    64_usize.saturating_add(2_usize.saturating_mul(size_of::<Entry>()))
}

/// Total [`LimitCode::MemorySize`] charge for `additional` entries of `Entry`.
fn charge_for<Entry>(additional: usize) -> u64 {
    u64::try_from(additional)
        .unwrap_or(u64::MAX)
        .saturating_mul(u64::try_from(entry_bytes::<Entry>()).unwrap_or(u64::MAX))
}

/// Charges [`LimitCode::MemorySize`] for `additional` slots in a table of
/// length `len`, refusing impossible capacities before mutating an unlimited
/// meter.
fn charge_table<Entry>(
    len: usize,
    additional: usize,
    budget: &Budget,
) -> Result<u64, LimitExceeded> {
    budget.poll()?;
    let charge = charge_for::<Entry>(additional);
    let max_bytes = u64::try_from(isize::MAX.unsigned_abs()).unwrap_or(u64::MAX);
    if len.checked_add(additional).is_none() || charge > max_bytes {
        let total = budget.used(LimitCode::MemorySize).saturating_add(charge);
        budget.check(LimitCode::MemorySize, total)?;
        return Err(LimitExceeded::new(LimitCode::MemorySize).with_amount(total));
    }
    budget.charge(LimitCode::MemorySize, charge)?;
    Ok(charge)
}

/// Charges [`LimitCode::MemorySize`] for `additional` [`HashSet`] entries and
/// fallibly reserves capacity for them.
///
/// If the budget refuses, neither the meter nor `set` changes. If the
/// allocator refuses or `additional` overflows table capacity, the charge is
/// released before returning [`LimitCode::MemorySize`].
pub fn try_reserve_set<T, S>(
    set: &mut HashSet<T, S>,
    additional: usize,
    budget: &Budget,
) -> Result<(), LimitExceeded>
where
    T: Eq + Hash,
    S: BuildHasher,
{
    let charge = charge_table::<T>(set.len(), additional, budget)?;
    #[expect(
        clippy::disallowed_methods,
        reason = "`MemorySize` has been charged above for the requested set entries and table slack"
    )]
    if set.try_reserve(additional).is_err() {
        budget.release(LimitCode::MemorySize, charge);
        let amount = budget.used(LimitCode::MemorySize).saturating_add(charge);
        return Err(LimitExceeded::new(LimitCode::MemorySize).with_amount(amount));
    }
    Ok(())
}

/// Charges [`LimitCode::MemorySize`] for `additional` [`HashMap`] entries and
/// fallibly reserves capacity for them.
///
/// If the budget refuses, neither the meter nor `map` changes. If the
/// allocator refuses or `additional` overflows table capacity, the charge is
/// released before returning [`LimitCode::MemorySize`].
pub fn try_reserve_map<K, V, S>(
    map: &mut HashMap<K, V, S>,
    additional: usize,
    budget: &Budget,
) -> Result<(), LimitExceeded>
where
    K: Eq + Hash,
    S: BuildHasher,
{
    let charge = charge_table::<(K, V)>(map.len(), additional, budget)?;
    #[expect(
        clippy::disallowed_methods,
        reason = "`MemorySize` has been charged above for the requested map entries and table slack"
    )]
    if map.try_reserve(additional).is_err() {
        budget.release(LimitCode::MemorySize, charge);
        let amount = budget.used(LimitCode::MemorySize).saturating_add(charge);
        return Err(LimitExceeded::new(LimitCode::MemorySize).with_amount(amount));
    }
    Ok(())
}

/// Inserts `value` into `set`, charging [`LimitCode::MemorySize`] and fallibly
/// reserving space first when `value` is not yet present. Returns `Ok(true)`
/// when `value` was newly inserted.
pub fn try_insert_set<T, S>(
    set: &mut HashSet<T, S>,
    value: T,
    budget: &Budget,
) -> Result<bool, LimitExceeded>
where
    T: Eq + Hash,
    S: BuildHasher,
{
    budget.poll()?;
    if set.contains(&value) {
        return Ok(false);
    }
    try_reserve_set(set, 1, budget)?;
    #[expect(
        clippy::disallowed_methods,
        reason = "`try_reserve_set` charged `MemorySize` and reserved capacity for one new entry"
    )]
    Ok(set.insert(value))
}

/// Inserts `(key, value)` into `map`, charging [`LimitCode::MemorySize`] and
/// fallibly reserving space first when `key` is not yet present. Returns the
/// previous value associated with `key`, if any.
pub fn try_insert_map<K, V, S>(
    map: &mut HashMap<K, V, S>,
    key: K,
    value: V,
    budget: &Budget,
) -> Result<Option<V>, LimitExceeded>
where
    K: Eq + Hash,
    S: BuildHasher,
{
    budget.poll()?;
    if !map.contains_key(&key) {
        try_reserve_map(map, 1, budget)?;
    }
    #[expect(
        clippy::disallowed_methods,
        reason = "`try_reserve_map` charged `MemorySize` and reserved capacity when `key` was absent"
    )]
    Ok(map.insert(key, value))
}
