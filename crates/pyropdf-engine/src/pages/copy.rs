//! Copies of pages within one document, with annotations and structure of
//! their own.
//!
//! A copy is a new page object that shares the original's content streams
//! and resources. Its annotations are new objects whose `/P` names the copy,
//! with `/Popup`, a popup's `/Parent`, and `/IRT` remapped among them; a
//! widget becomes another widget of its field, so both pages show and edit
//! one value. In a tagged document, the elements that own the page's marked
//! content and annotations, with their ancestors up to the root or its
//! `Document` element, are copied for the copy, which gets parent tree keys
//! of its own. Ported from pylopdf's `rust/src/document/page_copy.rs` at
//! ebb01d3c4c742046e8ce7fd72957cd15a2933321.

use std::collections::HashMap;
use std::hash::Hash;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::pages::MAX_CHAIN;
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::value::{Array, Dict, Name, Reference, Value};
use pyropdf_store::version::Transaction;
use pyropdf_store::{Error, LimitExceeded, Result};

/// Hash table keys, values, slack, and rehash space share the operation budget.
fn map_put<K: Eq + Hash, V>(
    map: &mut HashMap<K, V>,
    key: K,
    value: V,
    budget: &Budget,
) -> Result<Option<V>> {
    budget.poll()?;
    if !map.contains_key(&key) {
        budget.charge(LimitCode::MemorySize, (64 + 2 * size_of::<(K, V)>()) as u64)?;
        map.try_reserve(1)
            .map_err(|_| LimitExceeded::new(LimitCode::MemorySize))?;
    }
    Ok(map.insert(key, value))
}

/// Array growth includes transient allocation while the old buffer is live.
fn append(array: &mut Array, value: Value, budget: &Budget) -> Result<()> {
    budget.poll()?;
    budget.charge(
        LimitCode::MemorySize,
        (array
            .len()
            .saturating_add(1)
            .saturating_mul(2 * size_of::<Value>())) as u64,
    )?;
    array.push(value);
    Ok(())
}

/// A structure/field dictionary cannot also be another document identity.
fn editable(dict: &Dict) -> Result<()> {
    if matches!(dict.get("Type"), Some(Value::Name(kind)) if matches!(kind.as_bytes(), b"Page" | b"Pages" | b"Catalog"))
    {
        return Err(Error::malformed(
            "a page-copy structure or field aliases a page, page tree, or catalog",
        ));
    }
    Ok(())
}

/// Extends a stored array at its actual object, retaining reference aliases.
/// A structure /K may also be a single child, which becomes the first array item.
fn extend_entry(
    transaction: &mut Transaction,
    dict: &mut Dict,
    key: &str,
    values: impl ExactSizeIterator<Item = Value>,
    single: bool,
    budget: &Budget,
) -> Result<()> {
    let (mut array, object) = match located(transaction, dict.get(key), budget)? {
        Some((Value::Array(array), object)) => (array, object),
        None | Some((Value::Null, _)) => (Array::new(), None),
        Some(_) if single => {
            let mut array = Array::new();
            if let Some(value) = dict.get(key) {
                append(&mut array, copy_for_edit(value, budget)?, budget)?;
            }
            (array, None)
        }
        Some(_) => {
            return Err(Error::malformed(format!(
                "a page-copy /{key} is not an array"
            )));
        }
    };
    budget.charge(
        LimitCode::MemorySize,
        array
            .len()
            .saturating_add(values.len())
            .saturating_mul(2 * size_of::<Value>()) as u64,
    )?;
    for value in values {
        budget.poll()?;
        array.push(value);
    }
    match object {
        Some(object) => {
            transaction.write(object.number(), &Value::Array(array), budget)?;
        }
        None => {
            dict.insert(Name::from(key), Value::Array(array));
        }
    }
    Ok(())
}

/// The most annotations of a page that a copy copies.
const MAX_PAGE_ANNOTATIONS: usize = 4_096;
/// The deepest element chain that copying follows up to the root.
const MAX_ELEMENT_DEPTH: usize = 256;
/// The most elements that the copy of one page copies, and the most nodes
/// and pairs of the parent tree that reading it visits.
const MAX_TREE_NODES: usize = 1 << 20;
/// The deepest parent tree that reading it follows.
const MAX_TREE_DEPTH: usize = 32;
/// The field entries that a merged field and widget dictionary moves to a
/// new parent when the field gains a second widget.
const FIELD_KEYS: [&str; 11] = [
    "FT", "T", "TU", "TM", "Ff", "V", "DV", "Opt", "MaxLen", "TI", "I",
];
/// The triggers of `/AA` that belong to the field rather than its widget.
const FIELD_TRIGGERS: [&str; 4] = ["K", "F", "V", "C"];

/// A page's annotation and its copy, with the annotation's `/StructParent`.
type Copied = (Reference, Reference, Option<i64>);

/// A page copy's `/StructParents`, and the `/StructParent` of each of its
/// new annotations that has one.
type Keys = (Option<i64>, Vec<(Reference, i64)>);

/// Copies pages of the version that a transaction writes. Each copy writes
/// its page, annotations, and elements at once; [`Copies::finish`] writes
/// the parent tree entries and the kids that the copies add.
pub struct Copies<'b> {
    budget: &'b Budget,
    structure: Option<Structure<'b>>,
}

impl<'b> Copies<'b> {
    /// Reads the structure tree of the version that `transaction` writes.
    pub fn new(transaction: &Transaction, budget: &'b Budget) -> Result<Self> {
        Ok(Self {
            budget,
            structure: Structure::read(transaction, budget)?,
        })
    }

    /// Writes a copy of page `source`, whose dictionary is `page` with the
    /// attributes that it inherits and its new `/Parent`, and returns the
    /// copy.
    pub fn copy(
        &mut self,
        transaction: &mut Transaction,
        source: Reference,
        mut page: Dict,
    ) -> Result<Reference> {
        let budget = self.budget;
        let copy = transaction.allocate()?;
        let entries = match resolve(transaction, page.get("Annots"), budget)? {
            Some(Value::Array(entries)) => entries,
            _ => Array::new(),
        };
        if entries.len() > MAX_PAGE_ANNOTATIONS {
            return Err(LimitExceeded::over(
                LimitCode::AnnotationCount,
                MAX_PAGE_ANNOTATIONS as u64,
                entries.len() as u64,
            )
            .into());
        }
        // New numbers for the annotations other than widgets, which their
        // fields number as they take them.
        let mut numbers: HashMap<Reference, Reference> = HashMap::new();
        for entry in entries.iter() {
            if let Some((Value::Dict(annotation), Some(old))) =
                located(transaction, Some(entry), budget)?
                && !is_widget(&annotation)
                && !numbers.contains_key(&old)
            {
                map_put(&mut numbers, old, transaction.allocate()?, budget)?;
            }
        }
        let mut annots = BudgetVec::new(budget);
        annots.try_reserve_exact(entries.len())?;
        let mut copied: BudgetVec<'_, Copied> = BudgetVec::new(budget);
        for entry in entries.iter() {
            let (mut annotation, old) = match located(transaction, Some(entry), budget)? {
                Some((Value::Dict(annotation), old)) => (annotation, old),
                _ => {
                    annots.try_push(copy_for_edit(entry, budget)?)?;
                    continue;
                }
            };
            editable(&annotation)?;
            if old.is_none() && annotation.get("StructParent").is_some() {
                return Err(Error::malformed(
                    "a tagged annotation is not an indirect object",
                ));
            }
            let key = match annotation.get("StructParent") {
                Some(&Value::Integer(key)) => Some(key),
                _ => None,
            };
            annotation.remove("StructParent");
            let new = if is_widget(&annotation) {
                // A kid widget joins its field; a merged field and widget
                // becomes a field with both widgets.
                let merged = annotation.get("T").is_some() || annotation.get("Parent").is_none();
                let field = if merged {
                    old.ok_or_else(|| {
                        Error::malformed("a merged field/widget is not an indirect object")
                    })?
                } else {
                    match annotation.get("Parent") {
                        Some(Value::Reference(field)) => *field,
                        _ => return Err(Error::malformed("a widget's /Parent is not a reference")),
                    }
                };
                for key in FIELD_KEYS {
                    annotation.remove(key);
                }
                if let Some(Value::Dict(mut actions)) =
                    resolve(transaction, annotation.get("AA"), budget)?
                {
                    for trigger in FIELD_TRIGGERS {
                        actions.remove(trigger);
                    }
                    if actions.is_empty() {
                        annotation.remove("AA");
                    } else {
                        annotation.insert(Name::from("AA"), Value::Dict(actions));
                    }
                }
                annotation.remove("Parent");
                remap_annotation(transaction, &mut annotation, &numbers, copy, budget)?;
                join(transaction, field, annotation, budget)?
            } else {
                remap_annotation(transaction, &mut annotation, &numbers, copy, budget)?;
                let new = match old {
                    Some(old) => numbers[&old],
                    None => transaction.allocate()?,
                };
                transaction.write(new.number(), &Value::Dict(annotation), budget)?;
                new
            };
            annots.try_push(Value::Reference(new))?;
            if let Some(old) = old {
                copied.try_push((old, new, key))?;
            }
        }
        if !annots.is_empty() {
            page.insert(
                Name::from("Annots"),
                Value::Array(Array::from(annots.into_vec())),
            );
        }
        let page_key = match page.remove("StructParents") {
            Some(Value::Integer(key)) => Some(key),
            _ => None,
        };
        if let Some(structure) = &mut self.structure {
            let (page_key, keys) =
                structure.copy_page(transaction, source, copy, page_key, &copied)?;
            if let Some(key) = page_key {
                page.insert(Name::from("StructParents"), Value::Integer(key));
            }
            for (annotation, key) in keys {
                if let Some(mut dict) = dict_at(transaction, annotation, budget)? {
                    dict.insert(Name::from("StructParent"), Value::Integer(key));
                    transaction.write(annotation.number(), &Value::Dict(dict), budget)?;
                }
            }
        }
        transaction.write(copy.number(), &Value::Dict(page), budget)?;
        Ok(copy)
    }

    /// Writes the parent tree entries and the kids that the copies add.
    pub fn finish(self, transaction: &mut Transaction) -> Result<()> {
        match self.structure {
            Some(structure) => structure.finish(transaction, self.budget),
            None => Ok(()),
        }
    }
}

/// The structure tree that page copies extend, edited as copies are made
/// and written back by [`Structure::finish`].
struct Structure<'b> {
    budget: &'b Budget,
    root: Reference,
    /// The pairs of the parent tree before the copies, by key ascending.
    pairs: BudgetVec<'b, (i64, Value)>,
    next_key: i64,
    added: BudgetVec<'b, (i64, Value)>,
    /// Kids to append, by parent: the root or an element.
    appends: BudgetVec<'b, (Reference, Reference)>,
}

impl<'b> Structure<'b> {
    /// Reads a tagged document's tree, or `None` for an untagged one or one
    /// whose root is no object.
    fn read(transaction: &Transaction, budget: &'b Budget) -> Result<Option<Self>> {
        let catalog = transaction.version().trailer().get("Root");
        let Some(Value::Dict(catalog)) = resolve(transaction, catalog, budget)? else {
            return Ok(None);
        };
        let (root_dict, root) = match located(transaction, catalog.get("StructTreeRoot"), budget)? {
            None | Some((Value::Null, _)) => return Ok(None),
            Some((Value::Dict(dict), Some(root))) => (dict, root),
            _ => {
                return Err(Error::malformed(
                    "a structure root is not an indirect dictionary",
                ));
            }
        };
        editable(&root_dict)?;
        let mut pairs = BudgetVec::new(budget);
        if let Some(tree) = root_dict.get("ParentTree") {
            parent_tree_pairs(transaction, tree, &mut pairs, budget)?;
        }
        budget.charge(
            LimitCode::MemorySize,
            pairs.len().saturating_mul(size_of::<(i64, Value)>()) as u64,
        )?;
        pairs.sort_by_key(|&(key, _)| key);
        budget.poll()?;
        let largest = pairs.last().map(|&(key, _)| key);
        let next = match resolve(transaction, root_dict.get("ParentTreeNextKey"), budget)? {
            Some(Value::Integer(next)) => next,
            _ => 0,
        };
        Ok(Some(Self {
            budget,
            root,
            pairs,
            next_key: next.max(largest.map_or(0, |largest| largest.saturating_add(1))),
            added: BudgetVec::new(budget),
            appends: BudgetVec::new(budget),
        }))
    }

    fn pair(&self, key: i64) -> Option<&Value> {
        let at = self.pairs.partition_point(|&(other, _)| other < key);
        self.pairs
            .get(at)
            .filter(|&&(other, _)| other == key)
            .map(|(_, value)| value)
    }

    fn take_key(&mut self) -> Result<i64> {
        let key = self.next_key;
        if key >= i64::from(i32::MAX) {
            return Err(LimitExceeded::new(LimitCode::StructureTreeCount).into());
        }
        self.next_key += 1;
        Ok(key)
    }

    /// Copies the elements of page `source` for its copy `copy`, and returns
    /// the copy's `/StructParents` and, by new annotation, its
    /// `/StructParent`.
    fn copy_page(
        &mut self,
        transaction: &mut Transaction,
        source: Reference,
        copy: Reference,
        page_key: Option<i64>,
        annotations: &[Copied],
    ) -> Result<Keys> {
        let budget = self.budget;
        // The elements that own the page's content and annotations.
        let owners = match resolve(transaction, page_key.and_then(|key| self.pair(key)), budget)? {
            Some(Value::Array(items)) => items,
            _ => Array::new(),
        };
        let mut annotation_owners = BudgetVec::new(budget);
        for &(_, new, key) in annotations {
            if let Some(&Value::Reference(owner)) = key.and_then(|key| self.pair(key)) {
                annotation_owners.try_push((new, owner))?;
            }
        }
        // Those elements and their ancestors below the top, in discovery
        // order.
        let mut chain = BudgetVec::new(budget);
        let mut copies: HashMap<Reference, Reference> = HashMap::new();
        let starts = owners
            .iter()
            .filter_map(|owner| match owner {
                Value::Reference(owner) => Some(*owner),
                _ => None,
            })
            .chain(annotation_owners.iter().map(|&(_, owner)| owner));
        for start in starts {
            let mut element = start;
            for depth in 0..=MAX_ELEMENT_DEPTH {
                budget.poll()?;
                let (actual, dict) = structure_node(transaction, element, budget)?;
                element = actual;
                if copies.contains_key(&element)
                    || element == self.root
                    || matches!(dict.get("S"), Some(Value::Name(role)) if role.as_bytes() == b"Document")
                {
                    break;
                }
                if depth == MAX_ELEMENT_DEPTH {
                    return Err(LimitExceeded::over(
                        LimitCode::StructureTreeCount,
                        MAX_ELEMENT_DEPTH as u64,
                        depth as u64 + 1,
                    )
                    .into());
                }
                if chain.len() >= MAX_TREE_NODES {
                    return Err(LimitExceeded::over(
                        LimitCode::StructureTreeCount,
                        MAX_TREE_NODES as u64,
                        chain.len() as u64 + 1,
                    )
                    .into());
                }
                chain.try_push(element)?;
                map_put(&mut copies, element, element, budget)?;
                match dict.get("P") {
                    Some(&Value::Reference(parent)) => element = parent,
                    _ => break,
                }
            }
        }
        if chain.is_empty() {
            return Ok((None, Vec::new()));
        }
        for element in chain.iter() {
            map_put(&mut copies, *element, transaction.allocate()?, budget)?;
        }
        let mut annotation_map = HashMap::new();
        for &(old, new, _) in annotations {
            map_put(&mut annotation_map, old, new, budget)?;
        }
        for &element in chain.iter() {
            let Some(mut dict) = dict_at(transaction, element, budget)? else {
                continue;
            };
            let new = copies[&element];
            let on_source = dict.get("Pg") == Some(&Value::Reference(source));
            if let Some(&Value::Reference(parent)) = dict.get("P") {
                let (parent, _) = structure_node(transaction, parent, budget)?;
                match copies.get(&parent) {
                    Some(&copied) => {
                        dict.insert(Name::from("P"), Value::Reference(copied));
                    }
                    None => {
                        dict.insert(Name::from("P"), Value::Reference(parent));
                        self.appends.try_push((parent, new))?;
                    }
                }
            }
            if on_source {
                dict.insert(Name::from("Pg"), Value::Reference(copy));
            }
            dict.remove("ID");
            let kids = match resolve(transaction, dict.get("K"), budget)? {
                Some(Value::Array(kids)) => kids,
                None | Some(Value::Null) => Array::new(),
                Some(_) => {
                    let mut kids = Array::new();
                    if let Some(kid) = dict.get("K") {
                        append(&mut kids, copy_for_edit(kid, budget)?, budget)?;
                    }
                    kids
                }
            };
            let mut copied = BudgetVec::new(budget);
            copied.try_reserve_exact(kids.len())?;
            for kid in kids.iter() {
                budget.poll()?;
                let normalized;
                let kid = if let Value::Reference(reference) = kid {
                    normalized = match located(transaction, Some(kid), budget)? {
                        Some((Value::Dict(_), Some(actual))) if copies.contains_key(&actual) => {
                            Value::Reference(actual)
                        }
                        _ => Value::Reference(*reference),
                    };
                    &normalized
                } else {
                    kid
                };
                match kid {
                    Value::Integer(value) if on_source => {
                        copied.try_push(Value::Integer(*value))?
                    }
                    Value::Reference(child) if copies.contains_key(child) => {
                        copied.try_push(Value::Reference(copies[child]))?;
                    }
                    other => {
                        let Some(Value::Dict(mut reference)) =
                            resolve(transaction, Some(other), budget)?
                        else {
                            continue;
                        };
                        let here = match reference.get("Pg") {
                            Some(Value::Reference(page)) => *page == source,
                            _ => on_source,
                        };
                        match reference.get("Type") {
                            Some(Value::Name(kind))
                                if kind.as_bytes() == b"MCR"
                                    && here
                                    && reference.get("Stm").is_none() =>
                            {
                                reference.insert(Name::from("Pg"), Value::Reference(copy));
                                copied.try_push(Value::Dict(reference))?;
                            }
                            Some(Value::Name(kind)) if kind.as_bytes() == b"OBJR" => {
                                if let Some(Value::Reference(old)) = reference.get("Obj")
                                    && let Some(&new) = annotation_map.get(old)
                                {
                                    reference.insert(Name::from("Obj"), Value::Reference(new));
                                    reference.insert(Name::from("Pg"), Value::Reference(copy));
                                    copied.try_push(Value::Dict(reference))?;
                                }
                            }
                            _ => {}
                        }
                    }
                }
            }
            dict.insert(
                Name::from("K"),
                Value::Array(Array::from(copied.into_vec())),
            );
            transaction.write(new.number(), &Value::Dict(dict), budget)?;
        }
        let page_key = if owners.is_empty() {
            None
        } else {
            let key = self.take_key()?;
            let mut mapped = BudgetVec::new(budget);
            for owner in &owners {
                let value = match owner {
                    Value::Reference(element) => {
                        let (element, _) = structure_node(transaction, *element, budget)?;
                        Value::Reference(copies.get(&element).copied().unwrap_or(element))
                    }
                    other => copy_for_edit(other, budget)?,
                };
                mapped.try_push(value)?;
            }
            self.added
                .try_push((key, Value::Array(Array::from(mapped.into_vec()))))?;
            Some(key)
        };
        let mut keys = BudgetVec::new(budget);
        for &(annotation, owner) in annotation_owners.iter() {
            let (owner, _) = structure_node(transaction, owner, budget)?;
            if let Some(&copied) = copies.get(&owner) {
                let key = self.take_key()?;
                self.added.try_push((key, Value::Reference(copied)))?;
                keys.try_push((annotation, key))?;
            }
        }
        Ok((page_key, keys.into_vec()))
    }

    /// Writes the new parent tree pairs and the appended kids.
    fn finish(mut self, transaction: &mut Transaction, budget: &Budget) -> Result<()> {
        if self.added.is_empty() && self.appends.is_empty() {
            return Ok(());
        }
        budget.charge(
            LimitCode::MemorySize,
            self.appends
                .len()
                .saturating_mul(size_of::<(Reference, Reference)>()) as u64,
        )?;
        self.appends.sort_by_key(|(parent, _)| *parent);
        let mut at = 0;
        while at < self.appends.len() {
            let parent = self.appends[at].0;
            let end = at + self.appends[at..].partition_point(|(owner, _)| *owner == parent);
            let Some((Value::Dict(mut dict), Some(parent))) =
                located(transaction, Some(&Value::Reference(parent)), budget)?
            else {
                return Err(Error::malformed(
                    "a copied element's parent is not a dictionary",
                ));
            };
            editable(&dict)?;
            let values = self.appends[at..end]
                .iter()
                .map(|(_, kid)| Value::Reference(*kid));
            extend_entry(transaction, &mut dict, "K", values, true, budget)?;
            transaction.write(parent.number(), &Value::Dict(dict), budget)?;
            at = end;
        }
        let Some(mut root) = dict_at(transaction, self.root, budget)? else {
            return Err(Error::malformed("the structure root disappeared"));
        };
        editable(&root)?;
        let (tree_object, mut tree) = match located(transaction, root.get("ParentTree"), budget)? {
            Some((Value::Dict(tree), object)) => {
                editable(&tree)?;
                (object, tree)
            }
            None | Some((Value::Null, _)) => (None, Dict::new()),
            _ => return Err(Error::malformed("the parent tree is not a dictionary")),
        };
        let smallest = self
            .pairs
            .first()
            .map(|(key, _)| *key)
            .into_iter()
            .chain(self.added.first().map(|(key, _)| *key))
            .min();
        let largest = self
            .pairs
            .last()
            .map(|(key, _)| *key)
            .into_iter()
            .chain(self.added.last().map(|(key, _)| *key))
            .max();
        let mut flat = BudgetVec::new(budget);
        flat.try_reserve_exact(2 * self.added.len())?;
        for (key, value) in self.added.into_vec() {
            flat.try_push(Value::Integer(key))?;
            flat.try_push(value)?;
        }
        if !flat.is_empty() {
            if tree.get("Nums").is_none() && tree.get("Kids").is_some() {
                let limits = Array::from(vec![flat[0].clone(), flat[flat.len() - 2].clone()]);
                let mut leaf = Dict::new();
                leaf.insert(
                    Name::from("Nums"),
                    Value::Array(Array::from(flat.into_vec())),
                );
                leaf.insert(Name::from("Limits"), Value::Array(limits));
                let object = transaction.allocate()?;
                transaction.write(object.number(), &Value::Dict(leaf), budget)?;
                extend_entry(
                    transaction,
                    &mut tree,
                    "Kids",
                    std::iter::once(Value::Reference(object)),
                    false,
                    budget,
                )?;
            } else {
                extend_entry(
                    transaction,
                    &mut tree,
                    "Nums",
                    flat.into_vec().into_iter(),
                    false,
                    budget,
                )?;
            }
            if tree.get("Limits").is_some()
                && let (Some(first), Some(last)) = (smallest, largest)
            {
                tree.insert(
                    Name::from("Limits"),
                    Value::Array(Array::from(vec![
                        Value::Integer(first),
                        Value::Integer(last),
                    ])),
                );
            }
        }
        match tree_object {
            Some(object) => {
                transaction.write(object.number(), &Value::Dict(tree), budget)?;
            }
            None => {
                root.insert(Name::from("ParentTree"), Value::Dict(tree));
            }
        }
        root.insert(
            Name::from("ParentTreeNextKey"),
            Value::Integer(self.next_key),
        );
        transaction.write(self.root.number(), &Value::Dict(root), budget)?;
        Ok(())
    }
}

/// Adds the pairs of the parent tree `tree` to `pairs`, bounded and
/// following each node once.
fn parent_tree_pairs<'b>(
    transaction: &Transaction,
    tree: &Value,
    pairs: &mut BudgetVec<'b, (i64, Value)>,
    budget: &'b Budget,
) -> Result<()> {
    let mut visited: HashMap<Reference, ()> = HashMap::new();
    let mut pending = BudgetVec::new(budget);
    pending.try_push((copy_for_edit(tree, budget)?, 0_usize))?;
    let mut nodes = 0_usize;
    while let Some((node, depth)) = pending.pop() {
        budget.poll()?;
        let (node, object) = match located(transaction, Some(&node), budget)? {
            None | Some((Value::Null, _)) => continue,
            Some((Value::Dict(node), object)) => (node, object),
            _ => return Err(Error::malformed("a parent-tree node is not a dictionary")),
        };
        if let Some(reference) = object
            && map_put(&mut visited, reference, (), budget)?.is_some()
        {
            continue;
        }
        editable(&node)?;
        nodes += 1;
        if nodes > MAX_TREE_NODES || depth > MAX_TREE_DEPTH {
            return Err(LimitExceeded::new(LimitCode::StructureTreeCount).into());
        }
        if let Some(Value::Array(nums)) = resolve(transaction, node.get("Nums"), budget)? {
            let mut items = nums.iter();
            while let (Some(key), Some(value)) = (items.next(), items.next()) {
                if let Some(Value::Integer(key)) = resolve(transaction, Some(key), budget)? {
                    if pairs.len() >= MAX_TREE_NODES {
                        return Err(LimitExceeded::new(LimitCode::StructureTreeCount).into());
                    }
                    pairs.try_push((key, copy_for_edit(value, budget)?))?;
                }
            }
        }
        if let Some(Value::Array(kids)) = resolve(transaction, node.get("Kids"), budget)? {
            for kid in kids.iter() {
                pending.try_push((copy_for_edit(kid, budget)?, depth + 1))?;
            }
        }
    }
    Ok(())
}

/// Adds `widget` as another widget of field `field` and returns it. A
/// merged field and widget becomes a widget of a new parent field, which
/// takes its place in the form, so that both widgets are kids of one field.
fn join(
    transaction: &mut Transaction,
    field: Reference,
    mut widget: Dict,
    budget: &Budget,
) -> Result<Reference> {
    let Some((Value::Dict(old), Some(field))) =
        located(transaction, Some(&Value::Reference(field)), budget)?
    else {
        return Err(Error::malformed("a widget's field is not a dictionary"));
    };
    editable(&old)?;
    let parent = if is_widget(&old) {
        let mut parent = Dict::new();
        parent.insert(
            Name::from("Kids"),
            Value::Array(Array::from(vec![Value::Reference(field)])),
        );
        for key in FIELD_KEYS {
            if let Some(value) = old.get(key) {
                parent.insert(Name::from(key), copy_for_edit(value, budget)?);
            }
        }
        // A merged dictionary owns both field and widget actions. Only
        // field triggers move to the parent.
        let mut widget_actions = match resolve(transaction, old.get("AA"), budget)? {
            Some(Value::Dict(actions)) => Some(actions),
            _ => None,
        };
        if let Some(actions) = &mut widget_actions {
            let mut field_actions = Dict::new();
            for trigger in FIELD_TRIGGERS {
                if let Some(action) = actions.remove(trigger) {
                    field_actions.insert(Name::from(trigger), action);
                }
            }
            if !field_actions.is_empty() {
                parent.insert(Name::from("AA"), Value::Dict(field_actions));
            }
        }
        let grand = match old.get("Parent") {
            Some(&Value::Reference(grand)) => Some(grand),
            _ => None,
        };
        if let Some(grand) = grand {
            parent.insert(Name::from("Parent"), Value::Reference(grand));
        }
        let parent_object = transaction.allocate()?;
        transaction.write(parent_object.number(), &Value::Dict(parent), budget)?;
        let mut kid = old;
        for key in FIELD_KEYS {
            kid.remove(key);
        }
        match widget_actions {
            Some(actions) if actions.is_empty() => {
                kid.remove("AA");
            }
            Some(actions) => {
                kid.insert(Name::from("AA"), Value::Dict(actions));
            }
            None => {}
        }
        kid.insert(Name::from("Parent"), Value::Reference(parent_object));
        transaction.write(field.number(), &Value::Dict(kid), budget)?;
        replace_node(transaction, grand, field, parent_object, budget)?;
        parent_object
    } else {
        field
    };
    widget.insert(Name::from("Parent"), Value::Reference(parent));
    let new = transaction.allocate()?;
    transaction.write(new.number(), &Value::Dict(widget), budget)?;
    let Some(mut dict) = dict_at(transaction, parent, budget)? else {
        return Ok(new);
    };
    editable(&dict)?;
    extend_entry(
        transaction,
        &mut dict,
        "Kids",
        std::iter::once(Value::Reference(new)),
        false,
        budget,
    )?;
    transaction.write(parent.number(), &Value::Dict(dict), budget)?;
    Ok(new)
}

/// Puts field `new` where field `old` was: in the `/Kids` of `grand`, or
/// in the form's `/Fields` when `old` has no parent.
fn replace_node(
    transaction: &mut Transaction,
    grand: Option<Reference>,
    old: Reference,
    new: Reference,
    budget: &Budget,
) -> Result<()> {
    let (holder, key) = match grand {
        Some(grand) => (grand, "Kids"),
        None => {
            let root = transaction.version().trailer().get("Root");
            let Some((Value::Dict(mut catalog_dict), Some(catalog))) =
                located(transaction, root, budget)?
            else {
                return Err(Error::malformed(
                    "the catalog is not an indirect dictionary",
                ));
            };
            let Some((Value::Dict(mut form), form_object)) =
                located(transaction, catalog_dict.get("AcroForm"), budget)?
            else {
                return Err(Error::malformed("the form is not a dictionary"));
            };
            editable(&form)?;
            if replaced(&mut form, "Fields", old, new, transaction, budget)? {
                match form_object {
                    Some(object) => {
                        transaction.write(object.number(), &Value::Dict(form), budget)?;
                    }
                    None => {
                        catalog_dict.insert(Name::from("AcroForm"), Value::Dict(form));
                        transaction.write(catalog.number(), &Value::Dict(catalog_dict), budget)?;
                    }
                }
            }
            return Ok(());
        }
    };
    let Some((Value::Dict(mut dict), Some(holder))) =
        located(transaction, Some(&Value::Reference(holder)), budget)?
    else {
        return Err(Error::malformed("the parent field is not a dictionary"));
    };
    editable(&dict)?;
    if replaced(&mut dict, key, old, new, transaction, budget)? {
        transaction.write(holder.number(), &Value::Dict(dict), budget)?;
    }
    Ok(())
}

/// Replaces `old` with `new` in the array at `key` of `dict`, or appends
/// `new` when the array lacks `old`, and returns whether `dict` changed. An
/// array that is an object is written in place.
fn replaced(
    dict: &mut Dict,
    key: &str,
    old: Reference,
    new: Reference,
    transaction: &mut Transaction,
    budget: &Budget,
) -> Result<bool> {
    let swap = |array: &mut Array| -> Result<()> {
        let mut found = false;
        for item in array.iter_mut() {
            budget.poll()?;
            if matches!(item, Value::Reference(_))
                && let Some((_, Some(actual))) = located(transaction, Some(item), budget)?
                && actual == old
            {
                *item = Value::Reference(new);
                found = true;
            }
        }
        if !found {
            append(array, Value::Reference(new), budget)?;
        }
        Ok(())
    };
    let Some((Value::Array(mut array), object)) = located(transaction, dict.get(key), budget)?
    else {
        return Err(Error::malformed(format!(
            "the field /{key} is not an array"
        )));
    };
    swap(&mut array)?;
    match object {
        Some(object) => {
            transaction.write(object.number(), &Value::Array(array), budget)?;
            Ok(false)
        }
        None => {
            dict.insert(Name::from(key), Value::Array(array));
            Ok(true)
        }
    }
}

/// Preserve annotation relationships while resolving aliases to their actual identity.
fn remap_annotation(
    transaction: &Transaction,
    annotation: &mut Dict,
    numbers: &HashMap<Reference, Reference>,
    page: Reference,
    budget: &Budget,
) -> Result<()> {
    annotation.insert(Name::from("P"), Value::Reference(page));
    for key in ["Popup", "Parent", "IRT"] {
        if let Some(value @ Value::Reference(_)) = annotation.get(key)
            && let Some((_, Some(old))) = located(transaction, Some(value), budget)?
            && let Some(&new) = numbers.get(&old)
        {
            annotation.insert(Name::from(key), Value::Reference(new));
        }
    }
    Ok(())
}

/// A required structure owner must resolve to a distinct editable identity.
fn structure_node(
    transaction: &Transaction,
    reference: Reference,
    budget: &Budget,
) -> Result<(Reference, Dict)> {
    let Some((Value::Dict(dict), Some(actual))) =
        located(transaction, Some(&Value::Reference(reference)), budget)?
    else {
        return Err(Error::malformed(
            "a structure owner is not an indirect dictionary",
        ));
    };
    editable(&dict)?;
    Ok((actual, dict))
}

fn is_widget(annotation: &Dict) -> bool {
    matches!(annotation.get("Subtype"), Some(Value::Name(kind)) if kind.as_bytes() == b"Widget")
}

/// Returns the value of object `reference`, or `None` when it is gone.
fn value_at(
    transaction: &Transaction,
    reference: Reference,
    budget: &Budget,
) -> Result<Option<Value>> {
    transaction
        .version()
        .get(reference.number(), budget)?
        .filter(|object| object.generation() == reference.generation())
        .map(|object| copy_for_edit(object.value(), budget))
        .transpose()
}

/// Returns the dictionary of object `reference`, or `None`.
fn dict_at(
    transaction: &Transaction,
    reference: Reference,
    budget: &Budget,
) -> Result<Option<Dict>> {
    Ok(match value_at(transaction, reference, budget)? {
        Some(Value::Dict(dict)) => Some(dict),
        _ => None,
    })
}

/// Returns the final value and its owning object, retaining identity through aliases.
fn located(
    transaction: &Transaction,
    value: Option<&Value>,
    budget: &Budget,
) -> Result<Option<(Value, Option<Reference>)>> {
    let mut reference = match value {
        None => return Ok(None),
        Some(Value::Reference(reference)) => *reference,
        Some(value) => return Ok(Some((copy_for_edit(value, budget)?, None))),
    };
    for _ in 0..MAX_CHAIN {
        budget.poll()?;
        let Some(object) = transaction
            .version()
            .get(reference.number(), budget)?
            .filter(|object| object.generation() == reference.generation())
        else {
            return Ok(None);
        };
        match &**object.value() {
            Value::Reference(next) => reference = *next,
            value => return Ok(Some((copy_for_edit(value, budget)?, Some(reference)))),
        }
    }
    Err(Error::malformed(format!(
        "a page-copy reference cycle or a chain of more than {MAX_CHAIN} references"
    )))
}

fn resolve(
    transaction: &Transaction,
    value: Option<&Value>,
    budget: &Budget,
) -> Result<Option<Value>> {
    Ok(located(transaction, value, budget)?.map(|(value, _)| value))
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use super::*;
    use pyropdf_store::value::Bytes;
    use pyropdf_store::version::{Document, MemoryBase};

    #[test]
    fn copying_a_cached_value_charges_its_owned_buffers() {
        let mut base = MemoryBase::new(Dict::new());
        let source = format!("<< /Unknown ({}) >>", "x".repeat(1024 * 1024));
        let reference = Reference::new(5, 0);
        base.insert(reference, Bytes::new(source.into_bytes()));
        let document = Document::new(Arc::new(base), None);
        let transaction = document.begin();
        transaction.version().get(5, &Budget::default()).unwrap();
        let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(32 * 1024));
        let error = match value_at(&transaction, reference, &budget) {
            Err(error) => error,
            Ok(_) => panic!("copying a warm value ignored the operation memory budget"),
        };
        assert_eq!(
            error.limit().map(LimitExceeded::code),
            Some(LimitCode::MemorySize)
        );
    }

    #[test]
    fn resolving_an_overlong_reference_chain_refuses_instead_of_dropping_it() {
        let mut base = MemoryBase::new(Dict::new());
        for number in 1..=34 {
            let value = if number == 34 {
                "[]".to_owned()
            } else {
                format!("{} 0 R", number + 1)
            };
            base.insert(Reference::new(number, 0), Bytes::new(value.into_bytes()));
        }
        let document = Document::new(Arc::new(base), None);
        let transaction = document.begin();
        let budget = Budget::default();
        let value = Value::Reference(Reference::new(1, 0));
        assert!(resolve(&transaction, Some(&value), &budget).is_err());
    }
}
