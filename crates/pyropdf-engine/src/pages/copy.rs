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

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::pages::MAX_CHAIN;
use pyropdf_store::value::{Array, Dict, Name, Reference, Value};
use pyropdf_store::version::Transaction;
use pyropdf_store::{Error, LimitExceeded, Result};

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
            return Err(Error::malformed(format!(
                "page annotations exceed the {MAX_PAGE_ANNOTATIONS}-entry safety limit"
            )));
        }
        // New numbers for the annotations other than widgets, which their
        // fields number as they take them.
        let mut numbers: HashMap<u32, Reference> = HashMap::new();
        for entry in entries.iter() {
            if let Value::Reference(old) = entry
                && let Some(annotation) = dict_at(transaction, *old, budget)?
                && !is_widget(&annotation)
                && !numbers.contains_key(&old.number())
            {
                numbers.insert(old.number(), transaction.allocate()?);
            }
        }
        let remap = |annotation: &mut Dict| {
            annotation.insert(Name::from("P"), Value::Reference(copy));
            for key in ["Popup", "Parent", "IRT"] {
                if let Some(Value::Reference(old)) = annotation.get(key)
                    && let Some(&new) = numbers.get(&old.number())
                {
                    annotation.insert(Name::from(key), Value::Reference(new));
                }
            }
        };
        let mut annots = BudgetVec::new(budget);
        annots.try_reserve_exact(entries.len())?;
        let mut copied: BudgetVec<'_, Copied> = BudgetVec::new(budget);
        for entry in entries.iter() {
            let Value::Reference(old) = entry else {
                let mut entry = entry.clone();
                if let Value::Dict(annotation) = &mut entry {
                    remap(annotation);
                    annotation.remove("StructParent");
                }
                annots.try_push(entry)?;
                continue;
            };
            let Some(mut annotation) = dict_at(transaction, *old, budget)? else {
                annots.try_push(entry.clone())?;
                continue;
            };
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
                    *old
                } else {
                    match annotation.get("Parent") {
                        Some(Value::Reference(field)) => *field,
                        _ => return Err(Error::malformed("a widget's /Parent is not a reference")),
                    }
                };
                for key in FIELD_KEYS {
                    annotation.remove(key);
                }
                if let Some(Value::Dict(actions)) = annotation.get_mut("AA") {
                    for trigger in FIELD_TRIGGERS {
                        actions.remove(trigger);
                    }
                }
                annotation.remove("Parent");
                remap(&mut annotation);
                join(transaction, field, annotation, budget)?
            } else {
                remap(&mut annotation);
                let new = numbers[&old.number()];
                transaction.write(new.number(), &Value::Dict(annotation), budget)?;
                new
            };
            annots.try_push(Value::Reference(new))?;
            copied.try_push((*old, new, key))?;
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
        let catalog = transaction.version().trailer().get("Root").cloned();
        let Some(Value::Dict(catalog)) = resolve(transaction, catalog.as_ref(), budget)? else {
            return Ok(None);
        };
        let Some(&Value::Reference(root)) = catalog.get("StructTreeRoot") else {
            return Ok(None);
        };
        let Some(root_dict) = dict_at(transaction, root, budget)? else {
            return Ok(None);
        };
        let mut pairs = BudgetVec::new(budget);
        if let Some(tree) = root_dict.get("ParentTree") {
            parent_tree_pairs(transaction, tree, &mut pairs, budget)?;
        }
        pairs.sort_by_key(|&(key, _)| key);
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

    /// Returns whether `element` is where copied chains stop: the root, or
    /// an element whose role is `Document`.
    fn is_top(
        &self,
        transaction: &Transaction,
        element: Reference,
        budget: &Budget,
    ) -> Result<bool> {
        if element == self.root {
            return Ok(true);
        }
        Ok(dict_at(transaction, element, budget)?.is_some_and(
            |element| matches!(element.get("S"), Some(Value::Name(role)) if role.as_bytes() == b"Document"),
        ))
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
        let owners = match page_key.and_then(|key| self.pair(key)) {
            Some(Value::Array(items)) => items.clone(),
            Some(value @ Value::Reference(_)) => match resolve(transaction, Some(value), budget)? {
                Some(Value::Array(items)) => items,
                _ => Array::new(),
            },
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
            for _ in 0..MAX_ELEMENT_DEPTH {
                budget.poll()?;
                if copies.contains_key(&element) || self.is_top(transaction, element, budget)? {
                    break;
                }
                let Some(dict) = dict_at(transaction, element, budget)? else {
                    break;
                };
                if chain.len() >= MAX_TREE_NODES {
                    return Err(LimitExceeded::over(
                        LimitCode::StructureTreeCount,
                        MAX_TREE_NODES as u64,
                        chain.len() as u64 + 1,
                    )
                    .into());
                }
                chain.try_push(element)?;
                copies.insert(element, element);
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
            copies.insert(*element, transaction.allocate()?);
        }
        let annotation_map: HashMap<Reference, Reference> = annotations
            .iter()
            .map(|&(old, new, _)| (old, new))
            .collect();
        for &element in chain.iter() {
            let Some(mut dict) = dict_at(transaction, element, budget)? else {
                continue;
            };
            let new = copies[&element];
            let on_source = dict.get("Pg") == Some(&Value::Reference(source));
            if let Some(&Value::Reference(parent)) = dict.get("P") {
                match copies.get(&parent) {
                    Some(&copied) => {
                        dict.insert(Name::from("P"), Value::Reference(copied));
                    }
                    None => self.appends.try_push((parent, new))?,
                }
            }
            if on_source {
                dict.insert(Name::from("Pg"), Value::Reference(copy));
            }
            dict.remove("ID");
            let kids = match dict.get("K") {
                Some(Value::Array(kids)) => kids.clone(),
                None | Some(Value::Null) => Array::new(),
                Some(kid) => Array::from(vec![kid.clone()]),
            };
            let mut copied = BudgetVec::new(budget);
            copied.try_reserve_exact(kids.len())?;
            for kid in kids.iter() {
                budget.poll()?;
                match kid {
                    Value::Integer(_) if on_source => copied.try_push(kid.clone())?,
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
            let mapped = owners
                .iter()
                .map(|owner| match owner {
                    Value::Reference(element) => {
                        Value::Reference(copies.get(element).copied().unwrap_or(*element))
                    }
                    other => other.clone(),
                })
                .collect::<Vec<_>>();
            self.added
                .try_push((key, Value::Array(Array::from(mapped))))?;
            Some(key)
        };
        let mut keys = Vec::new();
        for &(annotation, owner) in annotation_owners.iter() {
            if let Some(&copied) = copies.get(&owner) {
                let key = self.take_key()?;
                self.added.try_push((key, Value::Reference(copied)))?;
                keys.push((annotation, key));
            }
        }
        Ok((page_key, keys))
    }

    /// Writes the new parent tree pairs and the appended kids.
    fn finish(self, transaction: &mut Transaction, budget: &Budget) -> Result<()> {
        if self.added.is_empty() && self.appends.is_empty() {
            return Ok(());
        }
        for &(parent, kid) in self.appends.iter() {
            let Some(mut dict) = dict_at(transaction, parent, budget)? else {
                continue;
            };
            let kids = match dict.remove("K") {
                Some(Value::Array(mut kids)) => {
                    kids.push(Value::Reference(kid));
                    kids
                }
                None | Some(Value::Null) => Array::from(vec![Value::Reference(kid)]),
                Some(other) => Array::from(vec![other, Value::Reference(kid)]),
            };
            dict.insert(Name::from("K"), Value::Array(kids));
            transaction.write(parent.number(), &Value::Dict(dict), budget)?;
        }
        let Some(mut root) = dict_at(transaction, self.root, budget)? else {
            return Ok(());
        };
        let (tree_object, mut tree) = match root.get("ParentTree") {
            Some(&Value::Reference(tree)) => match dict_at(transaction, tree, budget)? {
                Some(dict) => (Some(tree), dict),
                None => (None, Dict::new()),
            },
            Some(Value::Dict(tree)) => (None, tree.clone()),
            _ => (None, Dict::new()),
        };
        let mut flat = BudgetVec::new(budget);
        flat.try_reserve_exact(2 * self.added.len())?;
        for (key, value) in self.added.into_vec() {
            flat.try_push(Value::Integer(key))?;
            flat.try_push(value)?;
        }
        if !flat.is_empty() {
            let leaf =
                tree.get("Nums").is_none() && matches!(tree.get("Kids"), Some(Value::Array(_)));
            if leaf {
                let limits = Array::from(vec![flat[0].clone(), flat[flat.len() - 2].clone()]);
                let mut leaf = Dict::new();
                leaf.insert(
                    Name::from("Nums"),
                    Value::Array(Array::from(flat.into_vec())),
                );
                leaf.insert(Name::from("Limits"), Value::Array(limits));
                let leaf_object = transaction.allocate()?;
                transaction.write(leaf_object.number(), &Value::Dict(leaf), budget)?;
                if let Some(Value::Array(kids)) = tree.get_mut("Kids") {
                    kids.push(Value::Reference(leaf_object));
                }
            } else {
                let mut nums = match tree.remove("Nums") {
                    Some(Value::Array(nums)) => nums,
                    _ => Array::new(),
                };
                for value in flat.into_vec() {
                    nums.push(value);
                }
                tree.insert(Name::from("Nums"), Value::Array(nums));
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
    pending.try_push((tree.clone(), 0_usize))?;
    let mut nodes = 0_usize;
    while let Some((node, depth)) = pending.pop() {
        budget.poll()?;
        nodes += 1;
        if nodes > MAX_TREE_NODES || depth > MAX_TREE_DEPTH || pairs.len() > MAX_TREE_NODES {
            return Err(LimitExceeded::new(LimitCode::StructureTreeCount).into());
        }
        if let Value::Reference(reference) = node
            && visited.insert(reference, ()).is_some()
        {
            continue;
        }
        let Some(Value::Dict(node)) = resolve(transaction, Some(&node), budget)? else {
            continue;
        };
        if let Some(Value::Array(nums)) = resolve(transaction, node.get("Nums"), budget)? {
            let mut items = nums.iter();
            while let (Some(key), Some(value)) = (items.next(), items.next()) {
                if let Some(Value::Integer(key)) = resolve(transaction, Some(key), budget)? {
                    pairs.try_push((key, value.clone()))?;
                }
            }
        }
        if let Some(Value::Array(kids)) = resolve(transaction, node.get("Kids"), budget)? {
            for kid in kids.iter() {
                pending.try_push((kid.clone(), depth + 1))?;
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
    let Some(old) = dict_at(transaction, field, budget)? else {
        return Err(Error::malformed("a widget's field is not a dictionary"));
    };
    let parent = if is_widget(&old) {
        let mut parent = Dict::new();
        parent.insert(
            Name::from("Kids"),
            Value::Array(Array::from(vec![Value::Reference(field)])),
        );
        for key in FIELD_KEYS {
            if let Some(value) = old.get(key) {
                parent.insert(Name::from(key), value.clone());
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
    match dict.get("Kids") {
        Some(&Value::Reference(kids)) => {
            if let Some(Value::Array(mut array)) = value_at(transaction, kids, budget)? {
                array.push(Value::Reference(new));
                transaction.write(kids.number(), &Value::Array(array), budget)?;
                return Ok(new);
            }
            dict.insert(
                Name::from("Kids"),
                Value::Array(Array::from(vec![Value::Reference(new)])),
            );
        }
        Some(Value::Array(_)) => {
            if let Some(Value::Array(kids)) = dict.get_mut("Kids") {
                kids.push(Value::Reference(new));
            }
        }
        _ => {
            dict.insert(
                Name::from("Kids"),
                Value::Array(Array::from(vec![Value::Reference(new)])),
            );
        }
    }
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
            let Some(&Value::Reference(catalog)) = transaction.version().trailer().get("Root")
            else {
                return Ok(());
            };
            let Some(mut catalog_dict) = dict_at(transaction, catalog, budget)? else {
                return Ok(());
            };
            match catalog_dict.get_mut("AcroForm") {
                Some(&mut Value::Reference(form)) => (form, "Fields"),
                Some(Value::Dict(form)) => {
                    if replaced(form, "Fields", old, new, transaction, budget)? {
                        transaction.write(catalog.number(), &Value::Dict(catalog_dict), budget)?;
                    }
                    return Ok(());
                }
                _ => return Ok(()),
            }
        }
    };
    if let Some(mut dict) = dict_at(transaction, holder, budget)?
        && replaced(&mut dict, key, old, new, transaction, budget)?
    {
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
    let swap = |array: &mut Array| match array
        .iter_mut()
        .find(|item| **item == Value::Reference(old))
    {
        Some(item) => *item = Value::Reference(new),
        None => array.push(Value::Reference(new)),
    };
    match dict.get_mut(key) {
        Some(Value::Array(array)) => {
            swap(array);
            Ok(true)
        }
        Some(&mut Value::Reference(object)) => {
            if let Some(Value::Array(mut array)) = value_at(transaction, object, budget)? {
                swap(&mut array);
                transaction.write(object.number(), &Value::Array(array), budget)?;
            }
            Ok(false)
        }
        _ => Ok(false),
    }
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
    Ok(transaction
        .version()
        .get(reference.number(), budget)?
        .filter(|object| object.generation() == reference.generation())
        .map(|object| (**object.value()).clone()))
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

/// Returns `value`, following references, or `None` at the end of a
/// broken or overlong chain.
fn resolve(
    transaction: &Transaction,
    value: Option<&Value>,
    budget: &Budget,
) -> Result<Option<Value>> {
    let mut current = value.cloned();
    for _ in 0..MAX_CHAIN {
        match current {
            Some(Value::Reference(reference)) => {
                current = value_at(transaction, reference, budget)?
            }
            other => return Ok(other),
        }
    }
    Ok(None)
}
