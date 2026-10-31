//! Budgeted copies for referential edits. Source lexemes and stream data
//! stay shared; owned containers, names, and strings are allocated fallibly.

use crate::Result;
use crate::budget::Budget;
use crate::value::{self, Dict, Value};

/// Copies a value for editing without losing its unchanged lexemes.
pub fn copy_for_edit(value: &Value, budget: &Budget) -> Result<Value> {
    value::copy_for_edit(value, budget)
}

/// Copies a dictionary with reserved headroom for subsequent edits.
pub(super) fn copy_dict_for_edit(dict: &Dict, budget: &Budget) -> Result<Dict> {
    value::copy_dict_for_edit(dict, budget)
}
