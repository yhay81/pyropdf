//! Budgeted copies for referential edits. Source lexemes and stream data
//! stay shared; owned containers, names, and strings are charged first.

use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::value::{Dict, Name, Value};
use crate::{LimitExceeded, Result};

/// Copies a value for editing without losing its unchanged lexemes.
///
/// The preflight walk is iterative and charges owned buffers before Clone
/// allocates them. Recursive cloning is limited to the parser's 64 levels,
/// including when a caller supplies a manually constructed value.
pub fn copy_for_edit(value: &Value, budget: &Budget) -> Result<Value> {
    let mut pending = BudgetVec::new(budget);
    pending.try_push((value, 1u64))?;
    preflight(pending)?;
    Ok(value.clone())
}

/// Copies a dictionary after charging its owned contents and edit headroom.
pub(super) fn copy_dict_for_edit(dict: &Dict, budget: &Budget) -> Result<Dict> {
    budget.check(LimitCode::ObjectDepth, 1)?;
    let mut pending = BudgetVec::new(budget);
    dictionary(dict, 1, &mut pending)?;
    preflight(pending)?;
    Ok(dict.clone())
}

fn preflight(mut pending: BudgetVec<'_, (&Value, u64)>) -> Result<()> {
    let budget = pending.budget();
    while let Some((value, depth)) = pending.pop() {
        budget.poll()?;
        let container = matches!(value, Value::Array(_) | Value::Dict(_) | Value::Stream(_));
        if container {
            budget.check(LimitCode::ObjectDepth, depth)?;
            if depth > 64 {
                return Err(LimitExceeded::over(LimitCode::ObjectDepth, 64, depth).into());
            }
        }
        match value {
            Value::Name(name) => {
                budget.charge(LimitCode::MemorySize, name.as_bytes().len() as u64)?
            }
            Value::String(string) => {
                budget.charge(LimitCode::MemorySize, string.as_bytes().len() as u64)?
            }
            Value::Array(items) => {
                budget.charge(
                    LimitCode::MemorySize,
                    (items.len().saturating_mul(size_of::<Value>())) as u64,
                )?;
                for child in items {
                    pending.try_push((child, depth + 1))?;
                }
            }
            Value::Dict(dict) => dictionary(dict, depth, &mut pending)?,
            Value::Stream(stream) => {
                budget.charge(LimitCode::MemorySize, size_of_val(&**stream) as u64)?;
                dictionary(stream.dict(), depth, &mut pending)?;
            }
            _ => {}
        }
    }
    Ok(())
}

fn dictionary<'v>(
    dict: &'v Dict,
    depth: u64,
    pending: &mut BudgetVec<'_, (&'v Value, u64)>,
) -> Result<()> {
    // Edits may add Parent or inherited attributes. Include allocation
    // growth and a few new keys before making the owned dictionary.
    let bytes = dict
        .len()
        .saturating_add(8)
        .saturating_mul(2 * size_of::<(Name, Value)>());
    pending
        .budget()
        .charge(LimitCode::MemorySize, bytes as u64)?;
    for (key, child) in dict.iter() {
        pending
            .budget()
            .charge(LimitCode::MemorySize, key.as_bytes().len() as u64)?;
        pending.try_push((child, depth + 1))?;
    }
    Ok(())
}
