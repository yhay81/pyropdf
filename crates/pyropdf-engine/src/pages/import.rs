//! Importing pages of another document with only what they reach.
//!
//! A port of the object transplant of pylopdf's
//! `rust/src/document/page_tree.rs` at ebb01d3c. pylopdf renumbered the
//! whole source document and pruned what the pages did not reach; this
//! walk copies only the objects that the pages reach, each once, under a
//! number of the receiving document. A reference to a page that is not
//! imported, to a page-tree node, or to a catalog becomes null, so that
//! the source's page tree never comes along.

mod graft;

use std::borrow::Borrow;
use std::collections::HashMap;
use std::hash::Hash;
use std::mem::size_of;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::value::{Dict, Name, Reference, Value};
use pyropdf_store::version::{Transaction, Version};
use pyropdf_store::{Error, LimitExceeded, Result};

/// The parser and budgeted value copies both bound direct nesting at 64.
const MAX_DEPTH: usize = 64;
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
    /// Source values to copy, such as the default resources of the source
    /// form.
    pub values: &'a [Value],
    /// Whether to return every object that the import copies.
    pub copies: bool,
}

/// What an import copied.
pub struct Imported {
    /// The new pages, in order.
    pub pages: Vec<Reference>,
    /// The copy of each of [`Along::objects`], or null.
    pub objects: Vec<Value>,
    /// The copy of each of [`Along::found`], if the import copied it.
    pub found: Vec<Option<Reference>>,
    /// The copy of each of [`Along::values`].
    pub values: Vec<Value>,
    /// With [`Along::copies`], every object that the import wrote, the
    /// pages among them, by number ascending; empty without.
    pub copies: Vec<Reference>,
}

/// Imports pages and their reachable objects, with the objects that `along`
/// names, into one receiving transaction.
/// Overrides are sorted by the full source reference, including generation.
pub fn import(
    transaction: &mut Transaction,
    source: &Version,
    pages: Vec<(Reference, Dict)>,
    overrides: &[(Reference, Option<Value>)],
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
    for (page, _) in &pages {
        budget.poll()?;
        if source.generation(page.number()) != Some(page.generation()) {
            return Err(Error::malformed("an imported page reference is stale"));
        }
        let new = transaction.allocate()?;
        walk.numbers.insert(*page, new, 0)?;
        added.try_push(new)?;
    }
    let mut graft = graft::Graft::plan(transaction, &mut walk, budget)?;
    for ((_, mut dict), &new) in pages.into_iter().zip(added.iter()) {
        walk.dict(transaction, &mut dict, 1)?;
        dict.insert(Name::from("Parent"), Value::Reference(root));
        transaction.write(new.number(), &Value::Dict(dict), budget)?;
    }
    if let Some(graft) = &mut graft {
        graft.copy(transaction, &mut walk)?;
    }
    let mut objects = BudgetVec::new(budget);
    objects.try_reserve_exact(along.objects.len())?;
    for &object in along.objects {
        budget.poll()?;
        objects.try_push(walk.reference(transaction, object)?)?;
    }
    let mut values = BudgetVec::new(budget);
    values.try_reserve_exact(along.values.len())?;
    for value in along.values {
        budget.poll()?;
        let mut value = copy_for_edit(value, budget)?;
        walk.value(transaction, &mut value, 1)?;
        values.try_push(value)?;
    }
    while let Some((reference, new)) = walk.pending.pop() {
        budget.poll()?;
        let mut value = walk.object(reference)?.unwrap_or(Value::Null);
        walk.value(transaction, &mut value, 1)?;
        transaction.write(new.number(), &value, budget)?;
    }
    if let Some(graft) = graft {
        graft.finish(transaction, budget)?;
    }
    let mut found = BudgetVec::new(budget);
    found.try_reserve_exact(along.found.len())?;
    for object in along.found {
        found.try_push(walk.numbers.get(object).copied())?;
    }
    let mut copies = BudgetVec::new(budget);
    if along.copies {
        copies.try_reserve_exact(walk.numbers.len())?;
        for &copy in walk.numbers.map.values() {
            copies.try_push(copy)?;
        }
    }
    let mut copies = copies.into_vec();
    copies.sort_unstable();
    Ok(Imported {
        pages: added.into_vec(),
        objects: objects.into_vec(),
        found: found.into_vec(),
        values: values.into_vec(),
        copies,
    })
}

/// A fallible map that charges entries and their owned keys before growth.
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
    fn insert(&mut self, key: K, value: V, extra: u64) -> Result<Option<V>> {
        self.budget.poll()?;
        if !self.map.contains_key(&key) {
            let amount = (64 + 2 * size_of::<(K, V)>()) as u64 + extra;
            self.budget.charge(LimitCode::MemorySize, amount)?;
            self.charged += amount;
            self.map
                .try_reserve(1)
                .map_err(|_| LimitExceeded::new(LimitCode::MemorySize))?;
        }
        Ok(self.map.insert(key, value))
    }
}
impl<K, V> Drop for Charged<'_, K, V> {
    fn drop(&mut self) {
        self.budget.release(LimitCode::MemorySize, self.charged);
    }
}
#[derive(Clone, Copy)]
enum Keys {
    Strip,
    Shift(i64),
}

struct Walk<'s, 'b> {
    source: &'s Version,
    overrides: &'s [(Reference, Option<Value>)],
    edited: BudgetVec<'b, (Reference, Value)>,
    keys: Keys,
    numbers: Charged<'b, Reference, Reference>,
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
            .edited
            .binary_search_by_key(&reference, |(key, _)| *key)
        {
            return copy_for_edit(&self.edited[at].1, self.budget).map(Some);
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
        if let Some(new) = self.numbers.get(&reference).copied() {
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
        self.numbers.insert(reference, new, 0)?;
        self.pending.try_push((reference, new))?;
        Ok(Value::Reference(new))
    }

    fn dict(&mut self, transaction: &mut Transaction, dict: &mut Dict, depth: usize) -> Result<()> {
        self.budget.poll()?;
        if depth > MAX_DEPTH {
            return Err(LimitExceeded::new(LimitCode::ObjectDepth).into());
        }
        for key in STRUCTURE_KEYS {
            match self.keys {
                Keys::Strip => {
                    dict.remove(key);
                }
                Keys::Shift(shift) => {
                    if let Some(Value::Integer(value)) = dict.get_mut(key) {
                        *value = value
                            .checked_add(shift)
                            .ok_or_else(|| LimitExceeded::new(LimitCode::StructureTreeCount))?;
                    }
                }
            }
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
            edited: BudgetVec::new(&budget),
            keys: Keys::Strip,
            numbers: Charged::new(&budget),
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
