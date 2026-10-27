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
use pyropdf_store::value::{Array, Dict, Name, Reference, Value};
use pyropdf_store::version::{Transaction, Version};
use pyropdf_store::{Error, Result};

/// The deepest nesting of arrays and dictionaries that the walk copies.
const MAX_DEPTH: usize = 1024;
/// The keys that lead into the source's structure tree, which the imported
/// objects leave behind until their elements are grafted.
const STRUCTURE_KEYS: [&str; 2] = ["StructParent", "StructParents"];

/// Imports `pages`, each a source page with the dictionary that the
/// receiving document should hold for it, from `source` under the page-tree
/// node `root`, and returns the new pages in order.
///
/// `overrides`, sorted by number, replaces source objects, such as links
/// that now lead elsewhere: `Some` with the value to copy instead, `None`
/// with null. The pages' `/Parent` becomes `root`; the caller
/// adds the pages to `root`'s kids.
pub fn import(
    transaction: &mut Transaction,
    source: &Version,
    pages: &[(Reference, Dict)],
    overrides: &[(u32, Option<Value>)],
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
    for (page, _) in pages {
        let new = transaction.allocate()?;
        walk.numbers.insert(page.number(), new)?;
        added.try_push(new)?;
    }
    for ((_, dict), &new) in pages.iter().zip(added.iter()) {
        let mut page = walk.dict(transaction, dict, 0)?;
        page.insert(Name::from("Parent"), Value::Reference(root));
        transaction.write(new.number(), &Value::Dict(page), budget)?;
    }
    while let Some((number, new)) = walk.pending.pop() {
        let value = match walk.object(number)? {
            Some(Value::Stream(mut stream)) => {
                let dict = walk.dict(transaction, stream.dict(), 0)?;
                *stream.dict_mut() = dict;
                Value::Stream(stream)
            }
            Some(Value::Dict(dict)) => Value::Dict(walk.dict(transaction, &dict, 0)?),
            Some(value) => walk.value(transaction, &value, 0)?,
            None => Value::Null,
        };
        transaction.write(new.number(), &value, budget)?;
    }
    Ok(added.into_vec())
}

/// The source numbers that the import gave numbers of the receiving
/// document, charged to the budget as they grow.
struct Numbers<'b> {
    map: HashMap<u32, Reference>,
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

    fn get(&self, number: u32) -> Option<Reference> {
        self.map.get(&number).copied()
    }

    fn insert(&mut self, number: u32, new: Reference) -> Result<()> {
        // A hash map holds about twice its entries.
        let amount = 2 * (size_of::<u32>() + size_of::<Reference>()) as u64;
        self.budget.charge(LimitCode::MemorySize, amount)?;
        self.charged += amount;
        self.map.insert(number, new);
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
    overrides: &'s [(u32, Option<Value>)],
    numbers: Numbers<'b>,
    /// The source objects that have a new number and wait to be written.
    pending: BudgetVec<'b, (u32, Reference)>,
    budget: &'b Budget,
}

impl Walk<'_, '_> {
    /// Returns source object `number`, or what replaces it.
    fn object(&self, number: u32) -> Result<Option<Value>> {
        if let Ok(at) = self
            .overrides
            .binary_search_by_key(&number, |(number, _)| *number)
        {
            return Ok(self.overrides[at].1.clone());
        }
        Ok(self
            .source
            .get(number, self.budget)?
            .map(|object| Value::clone(object.value())))
    }

    /// Returns what `reference` becomes: the new number of an object that
    /// the import copies, or null.
    fn reference(&mut self, transaction: &mut Transaction, reference: Reference) -> Result<Value> {
        if let Some(new) = self.numbers.get(reference.number()) {
            return Ok(Value::Reference(new));
        }
        let Some(value) = self.object(reference.number())? else {
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
        self.numbers.insert(reference.number(), new)?;
        self.pending.try_push((reference.number(), new))?;
        Ok(Value::Reference(new))
    }

    fn dict(&mut self, transaction: &mut Transaction, dict: &Dict, depth: usize) -> Result<Dict> {
        if depth > MAX_DEPTH {
            return Err(Error::malformed("an imported object nests too deeply"));
        }
        let mut copy = dict.clone();
        for key in STRUCTURE_KEYS {
            copy.remove(key);
        }
        if !holds_references(dict, depth)? {
            return Ok(copy);
        }
        for (_, value) in copy.iter_mut() {
            *value = self.value(transaction, value, depth + 1)?;
        }
        Ok(copy)
    }

    fn value(
        &mut self,
        transaction: &mut Transaction,
        value: &Value,
        depth: usize,
    ) -> Result<Value> {
        if depth > MAX_DEPTH {
            return Err(Error::malformed("an imported object nests too deeply"));
        }
        Ok(match value {
            &Value::Reference(reference) => self.reference(transaction, reference)?,
            Value::Array(array) if holds(value, depth)? => {
                let mut items = BudgetVec::new(self.budget);
                items.try_reserve_exact(array.len())?;
                for item in array.iter() {
                    items.try_push(self.value(transaction, item, depth + 1)?)?;
                }
                Value::Array(Array::from(items.into_vec()))
            }
            Value::Dict(dict) => Value::Dict(self.dict(transaction, dict, depth)?),
            value => value.clone(),
        })
    }
}

/// Returns whether `value` holds a reference, so that copying it keeps the
/// bytes it was read from when it holds none.
fn holds(value: &Value, depth: usize) -> Result<bool> {
    if depth > MAX_DEPTH {
        return Err(Error::malformed("an imported object nests too deeply"));
    }
    Ok(match value {
        Value::Reference(_) => true,
        Value::Array(array) => {
            for item in array.iter() {
                if holds(item, depth + 1)? {
                    return Ok(true);
                }
            }
            false
        }
        Value::Dict(dict) => holds_references(dict, depth)?,
        _ => false,
    })
}

fn holds_references(dict: &Dict, depth: usize) -> Result<bool> {
    for (_, value) in dict.iter() {
        if holds(value, depth + 1)? {
            return Ok(true);
        }
    }
    Ok(false)
}
