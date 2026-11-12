//! Shared PDF object resolution, text and field-value decoding, and budgeted
//! collection helpers for AcroForm operations.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::MAX_CHAIN;
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::value::{Dict, PdfString, Reference, Value};

use super::text_string;
use crate::annotations::{AnnotationError as Error, AnnotationResult as Result};

/// The bytes of the names and types that a walk reads and builds.
pub(super) const MAX_NAME_BYTES: usize = 1024 * 1024;
/// The bytes of the values that a walk reads and returns, and of the
/// options of a choice field.
pub(super) const MAX_VALUE_BYTES: usize = 1024 * 1024;
/// The most items of the array values of a walk, and options of a choice
/// field.
pub const MAX_VALUE_ITEMS: usize = 4_096;
/// The bytes of the appearance states that one read of a page's widgets
/// returns.
pub(super) const MAX_STATE_BYTES: usize = 1024 * 1024;
/// The UTF-8 bytes of the value that setting a field writes
/// (`form_field_input_size`).
pub(super) const MAX_INPUT_BYTES: usize = 1024 * 1024;
pub(super) const FIELD_TRIGGERS: [&str; 4] = ["K", "F", "V", "C"];

/// An object number and generation.
pub type Id = (u32, u16);

/// The text that one walk reads and builds, against its limits.
#[derive(Default)]
pub(super) struct Text {
    /// The encoded and decoded bytes of names and types.
    pub(super) names: (usize, usize),
    /// The bytes of the full names.
    pub(super) built: usize,
    /// The encoded and decoded bytes of values.
    pub(super) values: (usize, usize),
    /// The items of array values.
    pub(super) items: usize,
}

pub(super) fn limit(message: String) -> Error {
    Error::Pdf(message)
}

pub(super) fn stale((number, _): Id) -> Error {
    Error::Stale(format!("object {number} is no longer a field of the form"))
}

/// Adds `amount` to `total`, refusing a total above `limit`.
pub(super) fn charge(total: &mut usize, amount: usize, limit: usize, label: &str) -> Result<()> {
    *total = total.saturating_add(amount);
    if *total > limit {
        return Err(Error::Pdf(format!(
            "AcroForm {label} text exceeds the {limit}-byte safety limit"
        )));
    }
    Ok(())
}

/// Returns object `id`, or `None` when it is missing.
pub(super) fn object(
    reads: &mut Reads<'_>,
    (number, generation): Id,
) -> Result<Option<Arc<Value>>> {
    Ok(reads
        .get(number)?
        .filter(|object| object.generation() == generation)
        .map(|object| Arc::clone(object.value())))
}

/// Follow object aliases while retaining the final identity and one snapshot.
pub(super) fn object_location(
    reads: &mut Reads<'_>,
    reference: Reference,
) -> Result<Option<(Id, Arc<Value>)>> {
    let budget = reads.budget();
    let mut current = reference;
    let mut seen = [None; MAX_CHAIN];
    for depth in 0..MAX_CHAIN {
        budget.poll()?;
        if seen[..depth].contains(&Some(current)) {
            return Ok(None);
        }
        seen[depth] = Some(current);
        let Some(value) = object(reads, id(current))? else {
            return Ok(None);
        };
        match &*value {
            Value::Null => return Ok(None),
            Value::Reference(next) => current = *next,
            _ => return Ok(Some((id(current), value))),
        }
    }
    if seen.contains(&Some(current)) {
        return Ok(None);
    }
    Err(Error::Malformed(format!(
        "a form reference chain exceeds {MAX_CHAIN} references"
    )))
}

/// Resolve a value, charging owned buffers before copying them.
pub(super) fn located(
    reads: &mut Reads<'_>,
    value: Option<&Value>,
) -> Result<Option<(Value, Option<Id>)>> {
    let budget = reads.budget();
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Reference(reference)) => match object_location(reads, *reference)? {
            Some((identity, value)) => copy_for_edit(&value, budget)
                .map(|value| Some((value, Some(identity))))
                .map_err(Error::from),
            None => Ok(None),
        },
        Some(value) => copy_for_edit(value, budget)
            .map(|value| Some((value, None)))
            .map_err(Error::from),
    }
}

pub(super) fn read(reads: &mut Reads<'_>, value: Option<&Value>) -> Result<Option<Value>> {
    Ok(located(reads, value)?.map(|(value, _)| value))
}

/// Charge a fallible allocation before growing an owned input-sized vector.
#[expect(
    clippy::disallowed_methods,
    reason = "MemorySize is charged before fallible reservation; push uses the reserved capacity"
)]
pub(super) fn push<T>(items: &mut Vec<T>, value: T, budget: &Budget) -> Result<()> {
    budget.poll()?;
    if items.len() == items.capacity() {
        let count = items.len().max(4);
        budget.charge(
            LimitCode::MemorySize,
            count.saturating_mul(2 * size_of::<T>()) as u64,
        )?;
        items
            .try_reserve(count)
            .map_err(|_| pyropdf_store::LimitExceeded::new(LimitCode::MemorySize))?;
    }
    items.push(value);
    Ok(())
}

#[expect(
    clippy::disallowed_methods,
    reason = "MemorySize pays for an entry and table overhead before fallible reservation"
)]
pub(super) fn reserve_map<K: Eq + std::hash::Hash, V>(
    map: &mut HashMap<K, V>,
    budget: &Budget,
) -> Result<()> {
    budget.charge(LimitCode::MemorySize, (64 + 2 * size_of::<(K, V)>()) as u64)?;
    map.try_reserve(1)
        .map_err(|_| pyropdf_store::LimitExceeded::new(LimitCode::MemorySize).into())
}

pub(super) const fn id(reference: Reference) -> Id {
    (reference.number(), reference.generation())
}

/// Returns whether `value` reads as an omitted entry: a null, or a
/// reference to a null or to a missing object (ISO 32000-2 sections 7.3.7
/// and 7.3.10).
pub(super) fn omitted(reads: &mut Reads<'_>, value: &Value) -> Result<bool> {
    Ok(match value {
        Value::Null => true,
        Value::Reference(reference) => {
            object(reads, id(*reference))?.is_none_or(|value| matches!(*value, Value::Null))
        }
        _ => false,
    })
}

/// Returns entry `key` of field `dict` unless it reads as omitted, so that
/// the field inherits it.
pub(super) fn own<'d>(
    reads: &mut Reads<'_>,
    dict: &'d Dict,
    key: &str,
) -> Result<Option<&'d Value>> {
    Ok(match dict.get(key) {
        Some(value) if !omitted(reads, value)? => Some(value),
        _ => None,
    })
}

/// Returns whether `dict` looks like a widget: its `/Subtype` is `Widget`,
/// or it has a `/Rect`.
pub(super) fn looks_like_widget(dict: &Dict) -> bool {
    matches!(dict.get("Subtype"), Some(Value::Name(name)) if name.as_bytes() == b"Widget")
        || dict.get("Rect").is_some()
}

/// Marks node `id` visited in `visited`; returns whether it was new.
#[expect(
    clippy::disallowed_methods,
    reason = "MemorySize pays for an entry and table overhead before fallible reservation and insertion"
)]
pub(super) fn insert_set(visited: &mut HashSet<Id>, id: Id, budget: &Budget) -> Result<bool> {
    budget.poll()?;
    if visited.contains(&id) {
        return Ok(false);
    }
    budget.charge(LimitCode::MemorySize, (64 + 2 * size_of::<Id>()) as u64)?;
    visited
        .try_reserve(1)
        .map_err(|_| pyropdf_store::LimitExceeded::new(LimitCode::MemorySize))?;
    Ok(visited.insert(id))
}

/// Decodes text string `encoded`, charging its encoded and decoded bytes
/// to `totals`.
pub(super) fn decode(
    encoded: &PdfString,
    totals: &mut (usize, usize),
    limit: usize,
    label: &str,
    budget: &Budget,
) -> Result<String> {
    charge(
        &mut totals.0,
        encoded.as_bytes().len(),
        limit,
        &format!("encoded {label}"),
    )?;
    budget.charge(
        LimitCode::MemorySize,
        encoded.as_bytes().len().saturating_mul(4) as u64,
    )?;
    let decoded = text_string(encoded.as_bytes());
    charge(
        &mut totals.1,
        decoded.len(),
        limit,
        &format!("decoded {label}"),
    )?;
    Ok(decoded)
}

/// Returns the text string that `value` holds, or `None` for anything
/// else.
pub(super) fn text(
    reads: &mut Reads<'_>,
    value: &Value,
    totals: &mut (usize, usize),
    limit: usize,
    label: &str,
) -> Result<Option<String>> {
    match read(reads, Some(value))? {
        Some(Value::String(encoded)) => {
            decode(&encoded, totals, limit, label, reads.budget()).map(Some)
        }
        _ => Ok(None),
    }
}

/// Returns a field value as text: a name, a text string, or the text
/// strings of an array joined by commas.
pub(super) fn field_value(
    reads: &mut Reads<'_>,
    value: &Value,
    totals: &mut (usize, usize),
    items: &mut usize,
) -> Result<Option<String>> {
    let label = "field-value";
    match read(reads, Some(value))? {
        Some(Value::Name(name)) => {
            let name = name.as_bytes();
            charge(
                &mut totals.0,
                name.len(),
                MAX_VALUE_BYTES,
                "encoded field-value",
            )?;
            reads
                .budget()
                .charge(LimitCode::MemorySize, name.len().saturating_mul(3) as u64)?;
            let decoded = String::from_utf8_lossy(name).into_owned();
            charge(
                &mut totals.1,
                decoded.len(),
                MAX_VALUE_BYTES,
                "decoded field-value",
            )?;
            Ok(Some(decoded))
        }
        Some(Value::String(encoded)) => {
            decode(&encoded, totals, MAX_VALUE_BYTES, label, reads.budget()).map(Some)
        }
        Some(Value::Array(array)) => {
            *items = items.saturating_add(array.len());
            if *items > MAX_VALUE_ITEMS {
                return Err(limit(format!(
                    "AcroForm field values exceed the {MAX_VALUE_ITEMS}-item safety limit"
                )));
            }
            let budget = reads.budget();
            let mut values = BudgetVec::new(budget);
            for item in array.iter() {
                if let Some(Value::String(encoded)) = read(reads, Some(item))? {
                    values.try_push(decode(&encoded, totals, MAX_VALUE_BYTES, label, budget)?)?;
                }
            }
            let separators = values.len().saturating_sub(1) * 2;
            if separators != 0 {
                charge(
                    &mut totals.1,
                    separators,
                    MAX_VALUE_BYTES,
                    "decoded field-value",
                )?;
            }
            let output = values
                .iter()
                .fold(separators, |total, value| total.saturating_add(value.len()));
            budget.charge(LimitCode::MemorySize, output as u64)?;
            Ok(Some(values.join(", ")))
        }
        _ => Ok(None),
    }
}

pub(super) fn node_value(
    reads: &mut Reads<'_>,
    dict: &Dict,
    inherited: &Option<Arc<str>>,
    text_budget: &mut Text,
) -> Result<Option<Arc<str>>> {
    Ok(match own(reads, dict, "V")? {
        Some(value) => field_value(
            reads,
            value,
            &mut text_budget.values,
            &mut text_budget.items,
        )?
        .map(|value| {
            reads
                .budget()
                .charge(LimitCode::MemorySize, value.len().saturating_add(32) as u64)?;
            Ok::<Arc<str>, Error>(Arc::from(value))
        })
        .transpose()?,
        None => inherited.clone(),
    })
}
