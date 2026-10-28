//! Importing pages of another document with only what they reach.
//!
//! A port of the object transplant of pylopdf's
//! `rust/src/document/page_tree.rs` at ebb01d3c. pylopdf renumbered the
//! whole source document and pruned what the pages did not reach; this
//! walk copies only the objects that the pages reach, each once, under a
//! number of the receiving document. A reference to a page that is not
//! imported, to a page-tree node, or to a catalog becomes null, so that
//! the source's page tree never comes along.

use std::collections::HashMap;
use std::mem::size_of;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::value::{Dict, Name, Reference, Value};
use pyropdf_store::version::{Transaction, Version};
use pyropdf_store::{Error, LimitExceeded, Result};

/// The parser and budgeted value copies both bound direct nesting at 64.
const MAX_DEPTH: usize = 64;
const STRUCTURE_KEYS: [&str; 2] = ["StructParent", "StructParents"];

/// Imports pages and their reachable objects into one receiving transaction.
/// Overrides are sorted by the full source reference, including generation.
pub fn import(
    transaction: &mut Transaction,
    source: &Version,
    pages: Vec<(Reference, Dict)>,
    overrides: &[(Reference, Option<Value>)],
    root: Reference,
    budget: &Budget,
) -> Result<Vec<Reference>> {
    let mut walk = Walk {
        source,
        overrides,
        numbers: Numbers::new(budget),
        pending: BudgetVec::new(budget),
        budget,
    };
    let mut added = BudgetVec::new(budget);
    added.try_reserve_exact(pages.len())?;
    for (page, _) in &pages {
        budget.poll()?;
        if source.generation(page.number()) != Some(page.generation()) {
            return Err(Error::malformed("an imported page reference is stale"));
        }
        let new = transaction.allocate()?;
        walk.numbers.insert(*page, new)?;
        added.try_push(new)?;
    }
    for ((_, mut dict), &new) in pages.into_iter().zip(added.iter()) {
        walk.dict(transaction, &mut dict, 1)?;
        dict.insert(Name::from("Parent"), Value::Reference(root));
        transaction.write(new.number(), &Value::Dict(dict), budget)?;
    }
    while let Some((reference, new)) = walk.pending.pop() {
        budget.poll()?;
        let mut value = walk.object(reference)?.unwrap_or(Value::Null);
        walk.value(transaction, &mut value, 1)?;
        transaction.write(new.number(), &value, budget)?;
    }
    Ok(added.into_vec())
}

struct Numbers<'b> {
    map: HashMap<Reference, Reference>,
    budget: &'b Budget,
    charged: u64,
}

impl<'b> Numbers<'b> {
    fn new(budget: &'b Budget) -> Self {
        Self {
            map: HashMap::new(),
            budget,
            charged: 0,
        }
    }

    fn get(&self, reference: Reference) -> Option<Reference> {
        self.map.get(&reference).copied()
    }

    fn insert(&mut self, reference: Reference, new: Reference) -> Result<()> {
        self.budget.poll()?;
        if !self.map.contains_key(&reference) {
            let amount = (64 + 2 * size_of::<(Reference, Reference)>()) as u64;
            self.budget.charge(LimitCode::MemorySize, amount)?;
            self.charged += amount;
            self.map
                .try_reserve(1)
                .map_err(|_| LimitExceeded::new(LimitCode::MemorySize))?;
        }
        self.map.insert(reference, new);
        Ok(())
    }
}

impl Drop for Numbers<'_> {
    fn drop(&mut self) {
        self.budget.release(LimitCode::MemorySize, self.charged);
    }
}

struct Walk<'s, 'b> {
    source: &'s Version,
    overrides: &'s [(Reference, Option<Value>)],
    numbers: Numbers<'b>,
    pending: BudgetVec<'b, (Reference, Reference)>,
    budget: &'b Budget,
}

impl Walk<'_, '_> {
    /// A missing generation is never replaced by a different live object.
    fn object(&self, reference: Reference) -> Result<Option<Value>> {
        self.budget.poll()?;
        if self.source.generation(reference.number()) != Some(reference.generation()) {
            return Ok(None);
        }
        if let Ok(at) = self
            .overrides
            .binary_search_by_key(&reference, |(key, _)| *key)
        {
            return self.overrides[at]
                .1
                .as_ref()
                .map(|value| copy_for_edit(value, self.budget))
                .transpose();
        }
        self.source
            .get(reference.number(), self.budget)?
            .filter(|object| object.generation() == reference.generation())
            .map(|object| copy_for_edit(object.value(), self.budget))
            .transpose()
    }

    fn reference(&mut self, transaction: &mut Transaction, reference: Reference) -> Result<Value> {
        self.budget.poll()?;
        if self.source.generation(reference.number()) != Some(reference.generation()) {
            return Ok(Value::Null);
        }
        if let Some(new) = self.numbers.get(reference) {
            return Ok(Value::Reference(new));
        }
        let Some(value) = self.object(reference)? else {
            return Ok(Value::Null);
        };
        let dict = match &value {
            Value::Dict(dict) => Some(dict),
            Value::Stream(stream) => Some(stream.dict()),
            _ => None,
        };
        if let Some(Value::Name(kind)) = dict.and_then(|dict| dict.get("Type"))
            && matches!(kind.as_bytes(), b"Page" | b"Pages" | b"Catalog")
        {
            return Ok(Value::Null);
        }
        let new = transaction.allocate()?;
        self.numbers.insert(reference, new)?;
        self.pending.try_push((reference, new))?;
        Ok(Value::Reference(new))
    }

    fn dict(&mut self, transaction: &mut Transaction, dict: &mut Dict, depth: usize) -> Result<()> {
        self.budget.poll()?;
        if depth > MAX_DEPTH {
            return Err(LimitExceeded::new(LimitCode::ObjectDepth).into());
        }
        for key in STRUCTURE_KEYS {
            dict.remove(key);
        }
        for (_, value) in dict.iter_mut() {
            self.value(transaction, value, depth + 1)?;
        }
        Ok(())
    }

    fn value(
        &mut self,
        transaction: &mut Transaction,
        value: &mut Value,
        depth: usize,
    ) -> Result<()> {
        self.budget.poll()?;
        if depth > MAX_DEPTH && matches!(value, Value::Array(_) | Value::Dict(_) | Value::Stream(_))
        {
            return Err(LimitExceeded::new(LimitCode::ObjectDepth).into());
        }
        if !needs_changes(value, self.budget)? {
            return Ok(());
        }
        match value {
            Value::Reference(reference) => *value = self.reference(transaction, *reference)?,
            Value::Array(array) => {
                for child in array.iter_mut() {
                    self.value(transaction, child, depth + 1)?;
                }
            }
            Value::Dict(dict) => self.dict(transaction, dict, depth)?,
            Value::Stream(stream) => self.dict(transaction, stream.dict_mut(), depth)?,
            _ => {}
        }
        Ok(())
    }
}

/// An iterative budgeted preflight lets unchanged subtrees retain their lexemes.
fn needs_changes(value: &Value, budget: &Budget) -> Result<bool> {
    let mut pending = BudgetVec::new(budget);
    pending.try_push((value, 1_usize))?;
    while let Some((value, depth)) = pending.pop() {
        budget.poll()?;
        if depth > MAX_DEPTH && matches!(value, Value::Array(_) | Value::Dict(_) | Value::Stream(_))
        {
            return Err(LimitExceeded::new(LimitCode::ObjectDepth).into());
        }
        match value {
            Value::Reference(_) => return Ok(true),
            Value::Array(array) => {
                for child in array.iter() {
                    pending.try_push((child, depth + 1))?;
                }
            }
            Value::Dict(dict) => {
                if STRUCTURE_KEYS.iter().any(|key| dict.get(key).is_some()) {
                    return Ok(true);
                }
                for (_, child) in dict.iter() {
                    pending.try_push((child, depth + 1))?;
                }
            }
            Value::Stream(stream) => {
                if STRUCTURE_KEYS
                    .iter()
                    .any(|key| stream.dict().get(key).is_some())
                {
                    return Ok(true);
                }
                for (_, child) in stream.dict().iter() {
                    pending.try_push((child, depth + 1))?;
                }
            }
            _ => {}
        }
    }
    Ok(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyropdf_store::value::Bytes;
    use pyropdf_store::version::{Document, MemoryBase};
    use std::sync::Arc;

    #[test]
    fn importing_a_cached_value_charges_its_owned_buffers() {
        let mut base = MemoryBase::new(Dict::new());
        let reference = Reference::new(5, 0);
        base.insert(
            reference,
            Bytes::new(format!("<< /Unknown ({}) >>", "x".repeat(1024 * 1024)).into_bytes()),
        );
        let document = Document::new(Arc::new(base), None);
        let version = document.current();
        version.get(5, &Budget::default()).unwrap();
        let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(32 * 1024));
        let walk = Walk {
            source: &version,
            overrides: &[],
            numbers: Numbers::new(&budget),
            pending: BudgetVec::new(&budget),
            budget: &budget,
        };
        let error = match walk.object(reference) {
            Ok(_) => panic!("import ignored its memory budget"),
            Err(error) => error,
        };
        assert_eq!(
            error.limit().map(pyropdf_store::LimitExceeded::code),
            Some(LimitCode::MemorySize)
        );
    }
}
