//! Pruning: the objects of a full save that the trailer reaches (DESIGN.md
//! section 4.4).

use super::{Result, Row};
use crate::ErrorKind;
use crate::budget::{Budget, BudgetVec};
use crate::value::Value;
use crate::version::Version;

/// Frees each row of `rows`, which holds the numbers of a full save in
/// ascending order, whose object the trailer of `version` does not reach.
/// The trailer's `/Encrypt` counts only when `encrypt` is true, which it is
/// not when the save writes a new one.
///
/// The walk keeps the numbers still to visit and one flag per row, so it
/// reads each object once, whatever the depth and cycles of the references.
/// An object that does not parse is kept, as a full save copies it, with
/// nothing that it refers to.
pub(super) fn free_unreachable(
    version: &Version,
    rows: &mut [Row],
    encrypt: bool,
    budget: &Budget,
) -> Result<()> {
    let mut reached = BudgetVec::new(budget);
    reached.try_extend(rows.iter().map(|_| false))?;
    let mut pending = BudgetVec::new(budget);
    for (name, value) in version.trailer().iter() {
        if encrypt || name.as_bytes() != b"Encrypt" {
            references(value, &mut pending, budget)?;
        }
    }
    while let Some(number) = pending.pop() {
        budget.poll()?;
        let Ok(at) = rows.binary_search_by_key(&number, |row| row.number) else {
            continue;
        };
        if reached[at] || rows[at].offset.is_none() {
            continue;
        }
        reached[at] = true;
        match version.get(number, budget) {
            Ok(Some(object)) => references(object.value(), &mut pending, budget)?,
            Ok(None) => {}
            Err(error) if matches!(error.kind(), ErrorKind::Malformed(_)) => {}
            Err(error) => return Err(error.into()),
        }
    }
    for (row, &reached) in rows.iter_mut().zip(reached.iter()) {
        if !reached && row.offset.is_some() {
            row.offset = None;
            // A free entry names the generation that the number takes next.
            row.generation = row.generation.saturating_add(1);
        }
    }
    Ok(())
}

/// Pushes the number of every reference inside `value` onto `out`, walking
/// arrays, dictionaries, and stream dictionaries with a stack.
fn references(value: &Value, out: &mut BudgetVec<'_, u32>, budget: &Budget) -> Result<()> {
    let mut stack = BudgetVec::new(budget);
    stack.try_push(value)?;
    while let Some(value) = stack.pop() {
        match value {
            Value::Reference(reference) => out.try_push(reference.number())?,
            Value::Array(array) => stack.try_extend(array.iter())?,
            Value::Dict(dict) => stack.try_extend(dict.iter().map(|(_, item)| item))?,
            Value::Stream(stream) => {
                stack.try_extend(stream.dict().iter().map(|(_, item)| item))?;
            }
            _ => {}
        }
    }
    Ok(())
}
