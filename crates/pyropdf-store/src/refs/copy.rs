//! Budgeted copies for referential edits. Source lexemes and stream data
//! stay shared; owned containers, names, and strings are allocated fallibly.

use crate::Result;
use crate::budget::Budget;
use crate::value::{self, Array, Dict, Value};

/// Copies a value for editing without losing its unchanged lexemes.
pub fn copy_for_edit(value: &Value, budget: &Budget) -> Result<Value> {
    value::copy_for_edit(value, budget)
}

/// Copies a dictionary with reserved headroom for subsequent edits.
pub fn copy_dict_for_edit(dict: &Dict, budget: &Budget) -> Result<Dict> {
    value::copy_dict_for_edit(dict, budget)
}

/// Copies an array with reserved headroom for subsequent edits.
pub fn copy_array_for_edit(array: &Array, budget: &Budget) -> Result<Array> {
    value::copy_array_for_edit(array, budget)
}
