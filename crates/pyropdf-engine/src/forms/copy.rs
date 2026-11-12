//! Charge form and page-import copies before cloning their lossless PDF values.

use pyropdf_store::Result;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::value::{Array, Dict, Name, Value};

pub(super) fn copy_array(array: &Array, budget: &Budget) -> Result<Array> {
    charge_copy(CopyPart::Array(array), budget)?;
    Ok(array.clone())
}

pub(super) fn copy_value(value: &Value, budget: &Budget) -> Result<Value> {
    charge_copy(CopyPart::Value(value), budget)?;
    Ok(value.clone())
}

/// The borrowed PDF value, array, or dictionary about to be copied.
pub enum CopyPart<'a> {
    Value(&'a Value),
    Array(&'a Array),
    Dict(&'a Dict),
}

/// Charge each owned container, name, and string to the operation's memory budget.
pub fn charge_copy(part: CopyPart<'_>, budget: &Budget) -> Result<()> {
    let mut pending = BudgetVec::new(budget);
    pending.try_push(part)?;
    while let Some(part) = pending.pop() {
        budget.poll()?;
        let bytes = match part {
            CopyPart::Value(value) => match value {
                Value::Name(name) => name.as_bytes().len(),
                Value::String(text) => text.as_bytes().len(),
                Value::Array(array) => {
                    pending.try_push(CopyPart::Array(array))?;
                    0
                }
                Value::Dict(dict) => {
                    pending.try_push(CopyPart::Dict(dict))?;
                    0
                }
                Value::Stream(stream) => {
                    pending.try_push(CopyPart::Dict(stream.dict()))?;
                    0
                }
                _ => 0,
            },
            CopyPart::Array(array) => {
                for value in array {
                    pending.try_push(CopyPart::Value(value))?;
                }
                array.len().saturating_mul(2 * size_of::<Value>())
            }
            CopyPart::Dict(dict) => {
                let mut bytes = dict.len().saturating_mul(2 * size_of::<(Name, Value)>());
                for (key, value) in dict.iter() {
                    bytes = bytes.saturating_add(key.as_bytes().len());
                    pending.try_push(CopyPart::Value(value))?;
                }
                bytes
            }
        };
        budget.charge(LimitCode::MemorySize, bytes as u64)?;
    }
    Ok(())
}
