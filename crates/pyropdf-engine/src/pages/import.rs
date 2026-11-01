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

/// Copy a value and the indirect objects it reaches from an immutable version.
///
/// Each live source reference is copied once, including cycles and shared
/// references. References to missing objects or a stale generation read as
/// null, as they do in the store. Unlike a page import, this keeps structure
/// keys and copies Page, Pages and Catalog objects without grafting anything.
/// All newly written objects are independent of subsequent source edits.
/// A stream whose Length would be rewritten by the store writer is refused.
/// On refusal the transaction is restored to its state before this call;
/// reserved object numbers may still be consumed.
pub fn copy_resource_graph(
    transaction: &mut Transaction,
    source: &Version,
    value: &Value,
    budget: &Budget,
) -> Result<Value> {
    let savepoint = transaction.savepoint();
    let result = (|| {
        let mut walk = Walk {
            source,
            overrides: &[],
            edited: BudgetVec::new(budget),
            keys: Keys::Preserve,
            numbers: Charged::new(budget),
            pending: BudgetVec::new(budget),
            budget,
        };
        let mut result = copy_for_edit(value, budget)?;
        walk.value(transaction, &mut result, 1)?;
        while let Some((reference, new)) = walk.pending.pop() {
            budget.poll()?;
            let mut object = walk.object(reference)?.unwrap_or(Value::Null);
            walk.value(transaction, &mut object, 1)?;
            transaction.write(new.number(), &object, budget)?;
        }
        budget.poll()?;
        Ok(result)
    })();
    if result.is_err() {
        transaction.rollback(savepoint);
    }
    result
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
    Preserve,
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
        let preserving = matches!(self.keys, Keys::Preserve);
        if preserving {
            self.budget.charge(LimitCode::ObjectCount, 1)?;
            let present = self.source.get(reference.number(), self.budget);
            match present {
                Ok(Some(object)) if object.generation() == reference.generation() => {}
                Ok(_) => {
                    self.budget.release(LimitCode::ObjectCount, 1);
                    return Ok(Value::Null);
                }
                Err(error) => {
                    self.budget.release(LimitCode::ObjectCount, 1);
                    return Err(error);
                }
            }
        } else {
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
                Keys::Preserve => {}
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
        if !needs_changes(value, self.budget, !matches!(self.keys, Keys::Preserve))? {
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
fn needs_changes(value: &Value, budget: &Budget, change_structure: bool) -> Result<bool> {
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
                if change_structure && STRUCTURE_KEYS.iter().any(|key| dict.get(key).is_some()) {
                    return Ok(true);
                }
                for (_, child) in dict.iter() {
                    pending.try_push((child, depth + 1))?;
                }
            }
            Value::Stream(stream) => {
                // The store writer replaces /Length with a direct integer.
                // Refuse rather than silently rewriting the source graph.
                if !change_structure
                    && stream.dict().get("Length")
                        != i64::try_from(stream.data().len())
                            .ok()
                            .map(Value::Integer)
                            .as_ref()
                {
                    return Err(Error::malformed(
                        "cannot faithfully copy a stream with a noncanonical Length",
                    ));
                }
                if change_structure
                    && STRUCTURE_KEYS
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
    use pyropdf_store::budget::CancellationToken;
    use pyropdf_store::value::{Array, Bytes, Stream};
    use pyropdf_store::version::{Document, MemoryBase};
    use std::sync::Arc;
    use std::time::Duration;

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

    fn base_with(objects: &[(u32, u16, &[u8])]) -> Arc<MemoryBase> {
        let mut base = MemoryBase::new(Dict::new());
        for &(number, generation, bytes) in objects {
            base.insert(
                Reference::new(number, generation),
                Bytes::new(bytes.to_vec()),
            );
        }
        Arc::new(base)
    }

    fn object(version: &Version, reference: Reference) -> Value {
        let object = version
            .get(reference.number(), &Budget::default())
            .unwrap()
            .unwrap();
        assert_eq!(object.generation(), reference.generation());
        (**object.value()).clone()
    }

    fn reference(value: &Value) -> Reference {
        let Value::Reference(reference) = value else {
            panic!("expected reference: {value:?}")
        };
        *reference
    }

    fn dict(value: &Value) -> &Dict {
        let Value::Dict(dict) = value else {
            panic!("expected dictionary: {value:?}")
        };
        dict
    }

    #[test]
    fn resource_graph_preserves_cycles_sharing_structure_and_old_version() {
        let base = base_with(&[
            (1, 0, b"<< /Type /Page /StructParents 9 /A 2 0 R /B 2 0 R /Catalog 3 0 R /Missing 99 0 R /Stale 2 1 R >>"),
            (2, 0, b"<< /Owner 1 0 R /Stream 4 0 R /Marker 1 /StructParent 7 >>"),
            (3, 0, b"<< /Type /Catalog /Pages 5 0 R >>"),
            (4, 0, b"<< /Length 4 /Owner 2 0 R >>\nstream\ntest\nendstream"),
            (5, 0, b"<< /Type /Pages /Count 1 /Kids [1 0 R] >>"),
        ]);
        let doc = Document::new(base, None);
        let source = doc.current();
        let budget = Budget::default();
        let mut transaction = doc.begin();
        // A write made after taking the source version must not alter the copy.
        let mut changed = Dict::new();
        changed.insert(Name::from("Marker"), Value::Integer(2));
        transaction
            .write(2, &Value::Dict(changed), &budget)
            .unwrap();
        let root = copy_resource_graph(
            &mut transaction,
            &source,
            &Value::Reference(Reference::new(1, 0)),
            &budget,
        )
        .unwrap();
        let new_root = reference(&root);
        let root_value = object(transaction.version(), new_root);
        let root_dict = dict(&root_value);
        assert_eq!(
            root_dict.get("Type"),
            Some(&Value::Name(Name::from("Page")))
        );
        assert_eq!(root_dict.get("StructParents"), Some(&Value::Integer(9)));
        assert_eq!(root_dict.get("Missing"), Some(&Value::Null));
        assert_eq!(root_dict.get("Stale"), Some(&Value::Null));
        let font = reference(root_dict.get("A").unwrap());
        assert_eq!(root_dict.get("A"), root_dict.get("B"));
        let catalog = reference(root_dict.get("Catalog").unwrap());
        let font_value = object(transaction.version(), font);
        let font_dict = dict(&font_value);
        assert_eq!(font_dict.get("Owner"), Some(&root));
        assert_eq!(font_dict.get("StructParent"), Some(&Value::Integer(7)));
        let stream_ref = reference(font_dict.get("Stream").unwrap());
        let stream_value = object(transaction.version(), stream_ref);
        let Value::Stream(stream) = stream_value else {
            panic!("expected stream")
        };
        assert_eq!(&**stream.data(), b"test");
        assert_eq!(stream.dict().get("Owner"), Some(&Value::Reference(font)));
        let catalog_value = object(transaction.version(), catalog);
        let pages = reference(dict(&catalog_value).get("Pages").unwrap());
        let pages_value = object(transaction.version(), pages);
        let Value::Array(kids) = dict(&pages_value).get("Kids").unwrap() else {
            panic!("expected Kids")
        };
        assert_eq!(kids.as_slice(), &[root]);

        // Modifying an original resource after copying must not change its copy.
        let mut changed = Dict::new();
        changed.insert(Name::from("Marker"), Value::Integer(3));
        transaction
            .write(2, &Value::Dict(changed), &budget)
            .unwrap();
        assert_eq!(
            dict(&object(transaction.version(), font)).get("Marker"),
            Some(&Value::Integer(1))
        );
        let committed = transaction.commit(&budget).unwrap();
        assert_eq!(
            dict(&object(&committed, font)).get("Marker"),
            Some(&Value::Integer(1))
        );
    }

    #[test]
    fn resource_graph_failure_rolls_back_partial_writes_and_obeys_limits() {
        let base = base_with(&[
            (1, 0, b"<< /A 2 0 R /B 3 0 R >>"),
            (2, 0, b"<< /Nested 4 0 R >>"),
            (3, 0, b"<< /Leaf 42 >>"),
            (4, 0, b"<< /Last 1 >>"),
        ]);
        let doc = Document::new(base, None);
        let source = doc.current();
        let mut transaction = doc.begin();
        let unlimited = Budget::default();
        transaction
            .write(3, &Value::Integer(123), &unlimited)
            .unwrap();
        let value = Value::Reference(Reference::new(1, 0));
        let budget = Budget::default().with_limit(LimitCode::ObjectCount, Some(3));
        let error = copy_resource_graph(&mut transaction, &source, &value, &budget).unwrap_err();
        assert_eq!(
            error.limit().map(LimitExceeded::code),
            Some(LimitCode::ObjectCount)
        );
        assert_eq!(
            object(transaction.version(), Reference::new(3, 0)),
            Value::Integer(123)
        );
        assert!(transaction.version().slots().all(|(number, _)| number == 3));

        let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(0));
        assert_eq!(
            copy_resource_graph(&mut transaction, &source, &value, &budget)
                .unwrap_err()
                .limit()
                .map(LimitExceeded::code),
            Some(LimitCode::MemorySize)
        );
        let budget = Budget::default()
            .with_limit(LimitCode::ObjectCount, Some(0))
            .with_limit(LimitCode::MemorySize, Some(256));
        assert_eq!(
            copy_resource_graph(&mut transaction, &source, &value, &budget)
                .unwrap_err()
                .limit()
                .map(LimitExceeded::code),
            Some(LimitCode::ObjectCount)
        );
        let token = CancellationToken::default();
        token.cancel();
        let budget = Budget::default().with_cancellation(token);
        assert_eq!(
            copy_resource_graph(&mut transaction, &source, &value, &budget)
                .unwrap_err()
                .limit()
                .map(LimitExceeded::code),
            Some(LimitCode::Cancelled)
        );
        let budget = Budget::default().with_timeout(Duration::ZERO);
        assert_eq!(
            copy_resource_graph(&mut transaction, &source, &value, &budget)
                .unwrap_err()
                .limit()
                .map(LimitExceeded::code),
            Some(LimitCode::Deadline)
        );
        assert!(transaction.version().slots().all(|(number, _)| number == 3));
    }

    #[test]
    fn resource_graph_isolates_same_number_in_different_documents() {
        let source_doc = Document::new(base_with(&[(1, 0, b"<< /Marker 1 >>")]), None);
        let target_doc = Document::new(base_with(&[(1, 0, b"<< /Marker 9 >>")]), None);
        let source = source_doc.current();
        let budget = Budget::default();
        let mut target = target_doc.begin();
        let result = copy_resource_graph(
            &mut target,
            &source,
            &Value::Reference(Reference::new(1, 0)),
            &budget,
        )
        .unwrap();
        let copied = reference(&result);
        assert_ne!(copied.number(), 1);
        assert_eq!(
            dict(&object(target.version(), Reference::new(1, 0))).get("Marker"),
            Some(&Value::Integer(9))
        );

        let mut source_edit = source_doc.begin();
        let mut changed = Dict::new();
        changed.insert(Name::from("Marker"), Value::Integer(2));
        source_edit
            .write(1, &Value::Dict(changed), &budget)
            .unwrap();
        source_edit.commit(&budget).unwrap();
        assert_eq!(
            dict(&object(target.version(), copied)).get("Marker"),
            Some(&Value::Integer(1))
        );

        let mut copied_edit = Dict::new();
        copied_edit.insert(Name::from("Marker"), Value::Integer(3));
        target
            .write(copied.number(), &Value::Dict(copied_edit), &budget)
            .unwrap();
        assert_eq!(
            dict(&object(&source_doc.current(), Reference::new(1, 0))).get("Marker"),
            Some(&Value::Integer(2))
        );
        assert_eq!(
            dict(&object(target.version(), Reference::new(1, 0))).get("Marker"),
            Some(&Value::Integer(9))
        );
    }

    #[test]
    fn resource_graph_respects_direct_object_depth() {
        let doc = Document::new(base_with(&[]), None);
        let source = doc.current();
        let mut transaction = doc.begin();
        let value = Value::Array(Array::from(vec![Value::Array(Array::new())]));
        let budget = Budget::default().with_limit(LimitCode::ObjectDepth, Some(1));
        assert_eq!(
            copy_resource_graph(&mut transaction, &source, &value, &budget)
                .unwrap_err()
                .limit()
                .map(LimitExceeded::code),
            Some(LimitCode::ObjectDepth)
        );
    }

    #[test]
    fn resource_graph_keeps_unmodified_structure_dictionary_lexemes() {
        let doc = Document::new(base_with(&[]), None);
        let source = doc.current();
        let mut transaction = doc.begin();
        let budget = Budget::default();
        let bytes = Bytes::new(b"<< /StructParents  17 /Precision 3.1400 >>".to_vec());
        let value = Value::parse(&bytes, &budget).unwrap();
        let copied = copy_resource_graph(&mut transaction, &source, &value, &budget).unwrap();
        assert_eq!(copied.lexeme(), value.lexeme());
        assert!(copied.lexeme().is_some());
    }

    #[test]
    fn resource_graph_refuses_a_stream_whose_length_would_be_rewritten() {
        let doc = Document::new(base_with(&[]), None);
        let source = doc.current();
        let mut transaction = doc.begin();
        let mut stream = Stream::new(Dict::new(), Bytes::new(b"abc".to_vec()));
        stream
            .dict_mut()
            .insert(Name::from("Length"), Value::Reference(Reference::new(9, 0)));
        // The preflight must also inspect a stream nested in a direct value.
        let value = Value::Array(Array::from(vec![Value::Stream(Box::new(stream))]));
        assert!(
            copy_resource_graph(&mut transaction, &source, &value, &Budget::default()).is_err()
        );
        assert_eq!(transaction.version().slots().count(), 0);
    }
}
