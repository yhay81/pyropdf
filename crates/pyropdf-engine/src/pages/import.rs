//! Importing pages of another document with only what they reach.
//!
//! A port of the object transplant of pylopdf's
//! `rust/src/document/page_tree.rs` at ebb01d3c. pylopdf renumbered the
//! whole source document and pruned what the pages did not reach; this
//! walk copies only the objects that the pages reach, each once, under a
//! number of the receiving document. A reference to a page that is not
//! imported, to a page-tree node, or to a catalog becomes null, so that
//! the source's page tree never comes along. The structure elements of
//! the pages come along too ([`graft`]).

mod graft;

use std::borrow::Borrow;
use std::collections::HashMap;
use std::hash::Hash;
use std::mem::size_of;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::value::{Array, Dict, Name, Reference, Value};
use pyropdf_store::version::{Transaction, Version};
use pyropdf_store::{Error, Result};

/// The deepest nesting of arrays and dictionaries that the walk copies.
const MAX_DEPTH: usize = 1024;
/// The keys that lead into the source's structure tree: the parent-tree
/// keys of pages and annotations.
const STRUCTURE_KEYS: [&str; 2] = ["StructParent", "StructParents"];

/// What an import copies besides its pages.
#[derive(Clone, Copy, Default)]
pub struct Along<'a> {
    /// The source objects to copy, such as the top-level fields that join
    /// the receiving form.
    pub objects: &'a [Reference],
    /// The source objects whose copies the caller wants when the import
    /// copies them anyway, such as the fields of a calculation order.
    pub found: &'a [Reference],
}

/// What an import copied.
pub struct Imported {
    /// The new pages, in order.
    pub pages: Vec<Reference>,
    /// The copy of each of [`Along::objects`], or null.
    pub objects: Vec<Value>,
    /// The copy of each of [`Along::found`], if the import copied it.
    pub found: Vec<Option<Reference>>,
}

/// Imports `pages`, each a source page with the dictionary that the
/// receiving document should hold for it, from `source` under the page-tree
/// node `root`, with the objects that `along` names.
///
/// `overrides`, sorted by number, replaces source objects, such as links
/// that now lead elsewhere: `Some` with the value to copy instead, `None`
/// with null. The pages' `/Parent` becomes `root`; the caller
/// adds the pages to `root`'s kids. A tagged source's elements join the
/// receiving document's structure tree, which `overrides` should leave
/// without the structure content of the source pages that stay behind.
pub fn import(
    transaction: &mut Transaction,
    source: &Version,
    pages: &[(Reference, Dict)],
    overrides: &[(u32, Option<Value>)],
    along: Along<'_>,
    root: Reference,
    budget: &Budget,
) -> Result<Imported> {
    let mut walk = Walk {
        source,
        overrides,
        edited: BudgetVec::new(budget),
        keys: Keys::Strip,
        numbers: Charged::new(budget),
        pending: BudgetVec::new(budget),
        budget,
    };
    let mut added = BudgetVec::new(budget);
    added.try_reserve_exact(pages.len())?;
    for (page, _) in pages {
        let new = transaction.allocate()?;
        walk.numbers.insert(page.number(), new, 0)?;
        added.try_push(new)?;
    }
    let mut graft = graft::Graft::plan(transaction, &mut walk, budget)?;
    for ((_, dict), &new) in pages.iter().zip(added.iter()) {
        let mut page = walk.dict(transaction, dict, 0)?;
        page.insert(Name::from("Parent"), Value::Reference(root));
        transaction.write(new.number(), &Value::Dict(page), budget)?;
    }
    if let Some(graft) = &mut graft {
        graft.copy(transaction, &mut walk)?;
    }
    let mut objects = BudgetVec::new(budget);
    objects.try_reserve_exact(along.objects.len())?;
    for &object in along.objects {
        objects.try_push(walk.reference(transaction, object)?)?;
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
    if let Some(graft) = graft {
        graft.finish(transaction, budget)?;
    }
    let mut found = BudgetVec::new(budget);
    found.try_reserve_exact(along.found.len())?;
    for object in along.found {
        found.try_push(walk.numbers.get(&object.number()).copied())?;
    }
    Ok(Imported {
        pages: added.into_vec(),
        objects: objects.into_vec(),
        found: found.into_vec(),
    })
}

/// A hash map charged to the budget as it grows.
struct Charged<'b, K, V> {
    map: HashMap<K, V>,
    budget: &'b Budget,
    charged: u64,
}

impl<'b, K: Eq + Hash, V> Charged<'b, K, V> {
    fn new(budget: &'b Budget) -> Self {
        Self {
            map: HashMap::new(),
            budget,
            charged: 0,
        }
    }

    fn get<Q: Eq + Hash + ?Sized>(&self, key: &Q) -> Option<&V>
    where
        K: Borrow<Q>,
    {
        self.map.get(key)
    }

    fn len(&self) -> usize {
        self.map.len()
    }

    /// Inserts `value` at `key`, charging the entry and `extra` bytes that
    /// it owns, and returns the value that `key` had.
    fn insert(&mut self, key: K, value: V, extra: u64) -> Result<Option<V>> {
        // A hash map holds about twice its entries.
        let amount = 2 * (size_of::<K>() + size_of::<V>()) as u64 + extra;
        self.budget.charge(LimitCode::MemorySize, amount)?;
        self.charged += amount;
        Ok(self.map.insert(key, value))
    }
}

impl<K, V> Drop for Charged<'_, K, V> {
    fn drop(&mut self) {
        self.budget.release(LimitCode::MemorySize, self.charged);
    }
}

/// What the walk does with the parent-tree keys that copied dictionaries
/// hold.
#[derive(Clone, Copy)]
enum Keys {
    /// Removes them: the source's structure tree stays behind.
    Strip,
    /// Adds this to them, past the receiving tree's keys.
    Shift(i64),
}

struct Walk<'s, 'b> {
    source: &'s Version,
    overrides: &'s [(u32, Option<Value>)],
    /// The source elements that the graft changes, by number ascending,
    /// which replace both the source and `overrides`.
    edited: BudgetVec<'b, (u32, Value)>,
    keys: Keys,
    /// The source numbers that the import gave numbers of the receiving
    /// document.
    numbers: Charged<'b, u32, Reference>,
    /// The source objects that have a new number and wait to be written.
    pending: BudgetVec<'b, (u32, Reference)>,
    budget: &'b Budget,
}

impl Walk<'_, '_> {
    /// Returns source object `number`, or what replaces it.
    fn object(&self, number: u32) -> Result<Option<Value>> {
        if let Ok(at) = self
            .edited
            .binary_search_by_key(&number, |(number, _)| *number)
        {
            return Ok(Some(self.edited[at].1.clone()));
        }
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
        if let Some(&new) = self.numbers.get(&reference.number()) {
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
        self.numbers.insert(reference.number(), new, 0)?;
        self.pending.try_push((reference.number(), new))?;
        Ok(Value::Reference(new))
    }

    fn dict(&mut self, transaction: &mut Transaction, dict: &Dict, depth: usize) -> Result<Dict> {
        if depth > MAX_DEPTH {
            return Err(Error::malformed("an imported object nests too deeply"));
        }
        let mut copy = dict.clone();
        for key in STRUCTURE_KEYS {
            match self.keys {
                Keys::Strip => {
                    copy.remove(key);
                }
                Keys::Shift(shift) => {
                    if let Some(Value::Integer(value)) = copy.get_mut(key) {
                        *value = value.saturating_add(shift);
                    }
                }
            }
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
