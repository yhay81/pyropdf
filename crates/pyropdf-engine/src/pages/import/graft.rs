//! Grafting the structure elements of imported pages into the receiving
//! document's structure tree.
//!
//! A port of pylopdf's `rust/src/structure_import.rs` at ebb01d3c. The
//! caller's overrides already leave out the source's structure content
//! that lay only on pages that stay behind. The elements that remain join
//! the receiving tree under its single `Document` element, or its root, in
//! place of the source's own `Document` element when it had one: the walk
//! maps the source's root and that element to the receiving parent, so the
//! elements' `/P` follow. Their parent-tree keys, and the `/StructParents`
//! and `/StructParent` entries that name them, move past the receiving
//! tree's keys; role-map entries that the receiving tree maps otherwise
//! resolve to the source's standard type; clashing class names and element
//! IDs gain a suffix; and namespaces join the receiving list. The receiving
//! tree is read before the import writes anything.

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::pages::MAX_CHAIN;
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::value::{Array, Dict, Name, PdfString, Reference, Value};
use pyropdf_store::version::{Transaction, Version};
use pyropdf_store::{Error, LimitExceeded, Result};

use crate::pages::copy::{editable, extend_entry};

use super::{Charged, Keys, Walk};

/// Elements and tree nodes that one graft reads.
const MAX_NODES: usize = 1 << 20;
/// Role-map links that resolving one role follows.
const MAX_ROLE_CHAIN: usize = 16;
/// Levels of a number or name tree.
const MAX_TREE_DEPTH: usize = 32;

/// Reads objects by reference: the source through the walk, the receiving
/// document through its version.
trait Objects {
    fn budget(&self) -> &Budget;
    fn get(&self, reference: Reference) -> Result<Option<Value>>;
}

impl Objects for Walk<'_, '_> {
    fn budget(&self) -> &Budget {
        self.budget
    }
    fn get(&self, reference: Reference) -> Result<Option<Value>> {
        self.object(reference)
    }
}

struct Target<'v> {
    version: &'v Version,
    budget: &'v Budget,
}

impl Objects for Target<'_> {
    fn budget(&self) -> &Budget {
        self.budget
    }
    fn get(&self, reference: Reference) -> Result<Option<Value>> {
        self.version
            .get(reference.number(), self.budget)?
            .filter(|object| object.generation() == reference.generation())
            .map(|object| copy_for_edit(object.value(), self.budget))
            .transpose()
    }
}

/// The receiving structure tree, as read before the import.
struct Receiving {
    catalog: (Reference, Dict),
    /// The root object, or `None` when the catalog holds no root object.
    root: Option<Reference>,
    root_dict: Dict,
    tree: Option<Reference>,
    tree_dict: Dict,
    next_key: i64,
    first_key: Option<i64>,
    roles: Option<Dict>,
    classes: Option<Dict>,
    /// The single `Document` element under the root.
    document: Option<(Reference, Dict)>,
}

/// The source's elements planned into the receiving tree, written by
/// [`Graft::finish`].
pub(super) struct Graft<'b> {
    receiving: Receiving,
    root: Reference,
    /// The element or root that takes the source's top-level elements.
    top: Reference,
    /// The source's top-level kids, copied by [`Graft::copy`].
    kids: Array,
    /// The source's parent-tree pairs, shifted, by key ascending; their
    /// values are copied by [`Graft::copy`].
    pairs: BudgetVec<'b, (i64, Value)>,
    next_key: i64,
    /// The receiving role map with the source's roles that it lacked, when
    /// it gained any.
    roles: Option<Dict>,
    /// The receiving class map with the source's classes, when it gained
    /// any.
    classes: Option<Dict>,
    /// The receiving ID tree's pairs with the source's, when it gained any.
    ids: Option<BudgetVec<'b, (PdfString, Value)>>,
    namespaces: Option<Array>,
}

impl<'b> Graft<'b> {
    /// Plans the graft of the source's elements into the receiving tree,
    /// or returns `None` when the source has no elements to graft; the walk
    /// then strips the source's parent-tree keys.
    pub(super) fn plan(
        transaction: &mut Transaction,
        walk: &mut Walk<'_, 'b>,
        budget: &'b Budget,
    ) -> Result<Option<Self>> {
        let catalog = walk.source.trailer().get("Root");
        let Some(Value::Dict(catalog)) = follow(walk, catalog)? else {
            return Ok(None);
        };
        let original_root = match catalog.get("StructTreeRoot") {
            Some(Value::Reference(root)) => Some(*root),
            _ => None,
        };
        let (source_root, source_root_object) = match located(walk, catalog.get("StructTreeRoot"))?
        {
            Some((Value::Dict(root), object)) => (root, object),
            None | Some((Value::Null, _)) => return Ok(None),
            _ => {
                return Err(Error::malformed(
                    "the source structure root is not a dictionary",
                ));
            }
        };
        editable(&source_root)?;
        let source_roles = dict_of(walk, source_root.get("RoleMap"))?;
        let Some(mut receiving) = Receiving::read(transaction.version(), budget)? else {
            return Ok(None);
        };

        // The source's top-level elements, through its single Document
        // element when the receiving tree has one. Kids stay as written: a
        // lone reference is one kid, not its object.
        let root_kids = kids_of(walk, source_root.get("K"))?;
        let mut skipped = BudgetVec::new(budget);
        skipped.try_extend_from_slice(source_root_object.as_slice())?;
        if let Some(root) = original_root
            && !skipped.contains(&root)
        {
            skipped.try_push(root)?;
        }
        let mut kids = kids_of(walk, source_root.get("K"))?;
        if receiving.document.is_some()
            && let [Value::Reference(lone)] = root_kids.as_slice()
            && let Some((Value::Dict(element), Some(actual))) =
                located(walk, Some(&Value::Reference(*lone)))?
            && is_document(walk, source_roles.as_ref(), &element)?
        {
            if !skipped.contains(lone) {
                skipped.try_push(*lone)?;
            }
            if !skipped.contains(&actual) {
                skipped.try_push(actual)?;
            }
            kids = kids_of(walk, element.get("K"))?;
        }
        if kids.is_empty() {
            return Ok(None);
        }
        let root = match receiving.root {
            Some(root) => root,
            None => transaction.allocate()?,
        };
        let top = receiving
            .document
            .as_ref()
            .map_or(root, |&(element, _)| element);
        for skipped in skipped.iter() {
            walk.numbers.insert(*skipped, top, 0)?;
        }

        // Parent-tree keys move past the receiving tree's.
        let mut pairs = BudgetVec::new(budget);
        if let Some(tree) = source_root.get("ParentTree") {
            tree_pairs(walk, tree, "Nums", budget, |key, value| {
                if let Value::Integer(key) = key {
                    pairs.try_push((key, value))?;
                }
                Ok(())
            })?;
        }
        let smallest = pairs.iter().map(|&(key, _)| key).min().unwrap_or(0);
        let shift = receiving
            .next_key
            .checked_sub(smallest)
            .ok_or_else(|| LimitExceeded::new(LimitCode::StructureTreeCount))?;
        for (key, _) in pairs.iter_mut() {
            *key = key
                .checked_add(shift)
                .filter(|key| *key >= 0)
                .ok_or_else(|| LimitExceeded::new(LimitCode::StructureTreeCount))?;
        }
        budget.charge(
            LimitCode::MemorySize,
            pairs.len().saturating_mul(size_of::<(i64, Value)>()) as u64,
        )?;
        pairs.sort_by_key(|&(key, _)| key);
        let next_key = pairs
            .last()
            .map_or(receiving.next_key, |&(largest, _)| {
                largest.saturating_add(1)
            })
            .max(receiving.next_key);
        if next_key > i64::from(i32::MAX) {
            return Err(LimitExceeded::new(LimitCode::StructureTreeCount).into());
        }
        walk.keys = Keys::Shift(shift);

        // Classes that the receiving tree defines otherwise take a suffix.
        let mut classes = receiving.classes.take().unwrap_or_default();
        let mut classes_changed = false;
        let mut renames: Charged<'b, Name, Name> = Charged::new(budget);
        if let Some(source_classes) = dict_of(walk, source_root.get("ClassMap"))? {
            let mut taken = Charged::new(budget);
            for name in classes.keys() {
                taken.insert(copy_bytes(name.as_bytes(), budget)?, (), 0)?;
            }
            for (name, value) in source_classes.iter() {
                if classes.get(name.as_bytes()) == Some(value) {
                    continue;
                }
                let new = unused(name.as_bytes(), &taken, b'_', budget)?;
                taken.insert(copy_bytes(&new, budget)?, (), 0)?;
                let mut copied = pyropdf_store::refs::copy_for_edit(value, budget)?;
                walk.value(transaction, &mut copied, 1)?;
                budget.charge(
                    LimitCode::MemorySize,
                    (2 * size_of::<(Name, Value)>()) as u64,
                )?;
                classes.insert(Name::new(copy_bytes(&new, budget)?), copied);
                classes_changed = true;
                if new != name.as_bytes() {
                    budget.charge(LimitCode::MemorySize, name.as_bytes().len() as u64)?;
                    renames.insert(name.clone(), Name::new(new), 0)?;
                }
            }
        }

        // Elements: roles, classes, and IDs that the receiving tree holds
        // otherwise.
        let mut roles = receiving.roles.take().unwrap_or_default();
        let mut roles_changed = false;
        let mut ids = BudgetVec::new(budget);
        let mut taken_ids = Charged::new(budget);
        let target = Target {
            version: transaction.version(),
            budget,
        };
        if let Some(tree) = receiving.root_dict.get("IDTree") {
            tree_pairs(&target, tree, "Names", budget, |name, value| {
                if let Value::String(name) = name {
                    let bytes = copy_bytes(name.as_bytes(), budget)?;
                    taken_ids.insert(bytes, (), 0)?;
                    ids.try_push((name, value))?;
                }
                Ok(())
            })?;
        }
        let mut ids_changed = false;
        let mut visited: Charged<'b, Reference, ()> = Charged::new(budget);
        let mut stack = BudgetVec::new(budget);
        for kid in kids.iter().rev() {
            if let &Value::Reference(kid) = kid {
                stack.try_push(kid)?;
            }
        }
        while let Some(reference) = stack.pop() {
            budget.poll()?;
            if skipped.contains(&reference) {
                continue;
            }
            let Some((Value::Dict(mut element), Some(reference))) =
                located(walk, Some(&Value::Reference(reference)))?
            else {
                continue;
            };
            if skipped.contains(&reference) || visited.insert(reference, (), 0)?.is_some() {
                continue;
            }
            if visited.len() > MAX_NODES {
                return Err(LimitExceeded::over(
                    LimitCode::StructureTreeCount,
                    MAX_NODES as u64,
                    visited.len() as u64,
                )
                .into());
            }
            editable(&element)?;
            if !is_element(&element) {
                continue;
            }
            for kid in kids_of(walk, element.get("K"))?.iter().rev() {
                if let &Value::Reference(kid) = kid {
                    stack.try_push(kid)?;
                }
            }
            let mut changed = false;
            if let Some(Value::Name(role)) = element.get("S") {
                let ours = source_roles
                    .as_ref()
                    .and_then(|map| map.get(role.as_bytes()));
                let theirs = roles.get(role.as_bytes());
                match (ours, theirs) {
                    (Some(ours), Some(theirs)) if ours != theirs => {
                        let standard = resolved_role(walk, source_roles.as_ref(), role)?;
                        element.insert(Name::from("S"), Value::Name(standard));
                        changed = true;
                    }
                    (Some(ours), None) => {
                        budget.charge(LimitCode::MemorySize, role.as_bytes().len() as u64)?;
                        budget.charge(
                            LimitCode::MemorySize,
                            (2 * size_of::<(Name, Value)>()) as u64,
                        )?;
                        roles.insert(role.clone(), copy_for_edit(ours, budget)?);
                        roles_changed = true;
                    }
                    _ => {}
                }
            }
            if renames.len() > 0
                && let Some(classes) = element.get_mut("C")
            {
                let mut rename = |class: &mut Value| -> Result<()> {
                    budget.poll()?;
                    if let Value::Name(name) = class
                        && let Some(new) = renames.get(name)
                    {
                        budget.charge(LimitCode::MemorySize, new.as_bytes().len() as u64)?;
                        *name = new.clone();
                        changed = true;
                    }
                    Ok(())
                };
                match classes {
                    Value::Array(items) => {
                        for item in items.iter_mut() {
                            rename(item)?;
                        }
                    }
                    other => rename(other)?,
                }
            }
            if let Some(Value::String(identifier)) = element.get("ID") {
                let new = unused(identifier.as_bytes(), &taken_ids, b'+', budget)?;
                let string = PdfString::new(copy_bytes(&new, budget)?, identifier.form());
                taken_ids.insert(new, (), 0)?;
                budget.charge(LimitCode::MemorySize, string.as_bytes().len() as u64)?;
                element.insert(Name::from("ID"), Value::String(string.clone()));
                changed = true;
                if let Value::Reference(copied) = walk.reference(transaction, reference)? {
                    ids.try_push((string, Value::Reference(copied)))?;
                    ids_changed = true;
                }
            }
            if changed {
                walk.edited.try_push((reference, Value::Dict(element)))?;
            }
        }
        budget.charge(
            LimitCode::MemorySize,
            walk.edited
                .len()
                .saturating_mul(size_of::<(Reference, Value)>()) as u64,
        )?;
        walk.edited.sort_by_key(|(reference, _)| *reference);
        let namespaces = match follow(walk, source_root.get("Namespaces"))? {
            Some(Value::Array(items)) => Some(items),
            _ => None,
        };
        Ok(Some(Self {
            receiving,
            root,
            top,
            kids,
            pairs,
            next_key,
            roles: roles_changed.then_some(roles),
            classes: classes_changed.then_some(classes),
            ids: ids_changed.then_some(ids),
            namespaces,
        }))
    }

    /// Copies the top-level kids, the parent-tree values, and the
    /// namespaces through the walk, before it writes what they reach.
    pub(super) fn copy(
        &mut self,
        transaction: &mut Transaction,
        walk: &mut Walk<'_, '_>,
    ) -> Result<()> {
        for kid in self.kids.iter_mut() {
            walk.value(transaction, kid, 1)?;
            if let Value::Dict(element) = kid
                && is_element(element)
            {
                element.insert(Name::from("P"), Value::Reference(self.top));
            }
        }
        for (_, value) in self.pairs.iter_mut() {
            walk.value(transaction, value, 1)?;
        }
        if let Some(namespaces) = &mut self.namespaces {
            for item in namespaces.iter_mut() {
                walk.value(transaction, item, 1)?;
            }
        }
        Ok(())
    }

    /// Writes the receiving tree with the source's elements.
    pub(super) fn finish(self, transaction: &mut Transaction, budget: &Budget) -> Result<()> {
        let Receiving {
            catalog: (catalog, mut catalog_dict),
            root: old_root,
            mut root_dict,
            tree,
            mut tree_dict,
            document,
            first_key,
            ..
        } = self.receiving;
        let first_new = self.pairs.first().map(|(key, _)| *key);
        let last_new = self.pairs.last().map(|(key, _)| *key);
        let mut flat = BudgetVec::new(budget);
        flat.try_reserve_exact(2 * self.pairs.len())?;
        for (key, value) in self.pairs.into_vec() {
            flat.try_push(Value::Integer(key))?;
            flat.try_push(value)?;
        }
        let mut leaf = None;
        if !flat.is_empty() {
            if tree_dict.get("Nums").is_none() && tree_dict.get("Kids").is_some() {
                let limits = Array::from(vec![flat[0].clone(), flat[flat.len() - 2].clone()]);
                let mut node = Dict::new();
                node.insert(
                    Name::from("Nums"),
                    Value::Array(Array::from(flat.into_vec())),
                );
                node.insert(Name::from("Limits"), Value::Array(limits));
                leaf = Some(node);
            } else {
                extend_entry(
                    transaction,
                    &mut tree_dict,
                    "Nums",
                    flat.into_vec().into_iter(),
                    false,
                    budget,
                )?;
            }
            if tree_dict.get("Limits").is_some()
                && let (Some(first), Some(last)) =
                    (first_key.into_iter().chain(first_new).min(), last_new)
            {
                tree_dict.insert(
                    Name::from("Limits"),
                    Value::Array(Array::from(vec![
                        Value::Integer(first),
                        Value::Integer(last),
                    ])),
                );
            }
        }
        if let Some(namespaces) = &self.namespaces {
            let known = match follow(
                &Target {
                    version: transaction.version(),
                    budget,
                },
                root_dict.get("Namespaces"),
            )? {
                Some(Value::Array(items)) => items,
                None | Some(Value::Null) => Array::new(),
                _ => return Err(Error::malformed("the namespaces entry is not an array")),
            };
            let mut added = BudgetVec::new(budget);
            for item in namespaces.iter() {
                budget.poll()?;
                if !known.iter().any(|other| other == item)
                    && !added.iter().any(|other| other == item)
                {
                    added.try_push(copy_for_edit(item, budget)?)?;
                }
            }
            if !added.is_empty() {
                extend_entry(
                    transaction,
                    &mut root_dict,
                    "Namespaces",
                    added.into_vec().into_iter(),
                    false,
                    budget,
                )?;
            }
        }
        let mut appended = BudgetVec::new(budget);
        for kid in self.kids.iter() {
            appended.try_push(copy_for_edit(kid, budget)?)?;
        }
        let document = match document {
            Some((element, mut dict)) => {
                extend_entry(
                    transaction,
                    &mut dict,
                    "K",
                    appended.into_vec().into_iter(),
                    true,
                    budget,
                )?;
                Some((element, dict))
            }
            None => {
                extend_entry(
                    transaction,
                    &mut root_dict,
                    "K",
                    appended.into_vec().into_iter(),
                    true,
                    budget,
                )?;
                None
            }
        };
        if let Some(leaf) = leaf {
            let object = transaction.allocate()?;
            transaction.write(object.number(), &Value::Dict(leaf), budget)?;
            extend_entry(
                transaction,
                &mut tree_dict,
                "Kids",
                std::iter::once(Value::Reference(object)),
                false,
                budget,
            )?;
        }
        if let Some((element, dict)) = document {
            transaction.write(element.number(), &Value::Dict(dict), budget)?;
        }
        match tree {
            Some(object) => {
                transaction.write(object.number(), &Value::Dict(tree_dict), budget)?;
            }
            None => {
                root_dict.insert(Name::from("ParentTree"), Value::Dict(tree_dict));
            }
        }
        root_dict.insert(
            Name::from("ParentTreeNextKey"),
            Value::Integer(self.next_key),
        );
        if let Some(roles) = self.roles {
            root_dict.insert(Name::from("RoleMap"), Value::Dict(roles));
        }
        if let Some(classes) = self.classes {
            root_dict.insert(Name::from("ClassMap"), Value::Dict(classes));
        }
        if let Some(mut ids) = self.ids {
            budget.charge(
                LimitCode::MemorySize,
                ids.len().saturating_mul(size_of::<(PdfString, Value)>()) as u64,
            )?;
            ids.sort_by(|(left, _), (right, _)| left.as_bytes().cmp(right.as_bytes()));
            let mut names = BudgetVec::new(budget);
            names.try_reserve_exact(2 * ids.len())?;
            for (name, value) in ids.into_vec() {
                names.try_push(Value::String(name))?;
                names.try_push(value)?;
            }
            let mut node = Dict::new();
            node.insert(
                Name::from("Names"),
                Value::Array(Array::from(names.into_vec())),
            );
            root_dict.insert(Name::from("IDTree"), Value::Dict(node));
        }
        transaction.write(self.root.number(), &Value::Dict(root_dict), budget)?;
        if old_root.is_none() {
            catalog_dict.insert(Name::from("StructTreeRoot"), Value::Reference(self.root));
            transaction.write(catalog.number(), &Value::Dict(catalog_dict), budget)?;
        }
        Ok(())
    }
}

impl Receiving {
    /// Reads the receiving tree, or a new one for an untagged document;
    /// `None` when the document has no catalog object.
    fn read(version: &Version, budget: &Budget) -> Result<Option<Self>> {
        let target = Target { version, budget };
        let Some((Value::Dict(catalog_dict), Some(catalog))) =
            located(&target, version.trailer().get("Root"))?
        else {
            return Err(Error::malformed(
                "the receiving catalog is not an indirect dictionary",
            ));
        };
        let (root, root_dict) = match located(&target, catalog_dict.get("StructTreeRoot"))? {
            Some((Value::Dict(dict), object)) => {
                editable(&dict)?;
                (object, dict)
            }
            None | Some((Value::Null, _)) => (None, new_root()),
            _ => {
                return Err(Error::malformed(
                    "the receiving structure root is not a dictionary",
                ));
            }
        };
        let roles = dict_of(&target, root_dict.get("RoleMap"))?;
        let classes = dict_of(&target, root_dict.get("ClassMap"))?;
        let (tree, tree_dict) = match located(&target, root_dict.get("ParentTree"))? {
            Some((Value::Dict(dict), object)) => {
                editable(&dict)?;
                (object, dict)
            }
            None | Some((Value::Null, _)) => (None, Dict::new()),
            _ => {
                return Err(Error::malformed(
                    "the receiving parent tree is not a dictionary",
                ));
            }
        };
        let mut largest = None;
        let mut first_key = None;
        if let Some(tree) = root_dict.get("ParentTree") {
            tree_pairs(&target, tree, "Nums", budget, |key, _| {
                if let Value::Integer(key) = key {
                    largest = largest.max(Some(key));
                    first_key = first_key.min(Some(key)).or(Some(key));
                }
                Ok(())
            })?;
        }
        let next_key = match follow(&target, root_dict.get("ParentTreeNextKey"))? {
            Some(Value::Integer(next)) => next,
            _ => 0,
        }
        .max(largest.map_or(0, |largest: i64| largest.saturating_add(1)));
        let root_kids = kids_of(&target, root_dict.get("K"))?;
        let document = match root_kids.as_slice() {
            [Value::Reference(element)] => {
                match located(&target, Some(&Value::Reference(*element)))? {
                    Some((Value::Dict(dict), Some(element)))
                        if is_document(&target, roles.as_ref(), &dict)? =>
                    {
                        editable(&dict)?;
                        Some((element, dict))
                    }
                    _ => None,
                }
            }
            _ => None,
        };
        let document_object = document.as_ref().map(|(object, _)| *object);
        if root.is_some() && (root == tree || root == document_object)
            || tree.is_some() && tree == document_object
        {
            return Err(Error::malformed("a structure object has conflicting roles"));
        }
        Ok(Some(Self {
            catalog: (catalog, catalog_dict),
            root,
            root_dict,
            tree,
            tree_dict,
            next_key,
            first_key,
            roles,
            classes,
            document,
        }))
    }
}

fn new_root() -> Dict {
    let mut root = Dict::new();
    root.insert(
        Name::from("Type"),
        Value::Name(Name::from("StructTreeRoot")),
    );
    root
}

/// Resolve aliases while retaining the identity of the final owning object.
fn located(
    objects: &impl Objects,
    value: Option<&Value>,
) -> Result<Option<(Value, Option<Reference>)>> {
    let mut current = match value {
        None => return Ok(None),
        Some(Value::Reference(reference)) => *reference,
        Some(value) => {
            return copy_for_edit(value, objects.budget()).map(|value| Some((value, None)));
        }
    };
    for _ in 0..MAX_CHAIN {
        objects.budget().poll()?;
        match objects.get(current)? {
            Some(Value::Reference(next)) => current = next,
            Some(value) => return Ok(Some((value, Some(current)))),
            None => return Ok(None),
        }
    }
    Err(Error::malformed(
        "a structure reference cycle or overlong chain",
    ))
}

fn follow(objects: &impl Objects, value: Option<&Value>) -> Result<Option<Value>> {
    Ok(located(objects, value)?.map(|(value, _)| value))
}

fn dict_of(objects: &impl Objects, value: Option<&Value>) -> Result<Option<Dict>> {
    Ok(match follow(objects, value)? {
        Some(Value::Dict(dict)) => Some(dict),
        _ => None,
    })
}

fn kids_of(objects: &impl Objects, value: Option<&Value>) -> Result<Array> {
    Ok(match value {
        None | Some(Value::Null) => Array::new(),
        Some(value) => match located(objects, Some(value))? {
            Some((Value::Array(kids), _)) => kids,
            Some((Value::Null, _)) | None => Array::new(),
            Some(_) => Array::from(vec![copy_for_edit(value, objects.budget())?]),
        },
    })
}

fn is_element(dict: &Dict) -> bool {
    dict.get("S").is_some()
        && !matches!(dict.get("Type"), Some(Value::Name(kind))
            if matches!(kind.as_bytes(), b"MCR" | b"OBJR"))
}

fn is_document(objects: &impl Objects, roles: Option<&Dict>, element: &Dict) -> Result<bool> {
    Ok(match element.get("S") {
        Some(Value::Name(role)) => resolved_role(objects, roles, role)?.as_bytes() == b"Document",
        _ => false,
    })
}

/// Returns the standard type that `role` resolves to through `roles`.
fn resolved_role(objects: &impl Objects, roles: Option<&Dict>, role: &Name) -> Result<Name> {
    let budget = objects.budget();
    let mut current = Name::new(copy_bytes(role.as_bytes(), budget)?);
    let mut visited = BudgetVec::new(budget);
    for depth in 0..=MAX_ROLE_CHAIN {
        budget.poll()?;
        if visited.contains(&current) {
            return Err(Error::malformed("a structure role map contains a cycle"));
        }
        budget.charge(LimitCode::MemorySize, current.as_bytes().len() as u64)?;
        visited.try_push(current.clone())?;
        match follow(
            objects,
            roles.and_then(|roles| roles.get(current.as_bytes())),
        )? {
            Some(Value::Name(mapped)) => {
                if depth == MAX_ROLE_CHAIN {
                    return Err(LimitExceeded::new(LimitCode::StructureTreeCount).into());
                }
                current = mapped;
            }
            _ => return Ok(current),
        }
    }
    unreachable!("the bounded role loop always returns")
}

fn copy_bytes(bytes: &[u8], budget: &Budget) -> Result<Vec<u8>> {
    let mut out = BudgetVec::new(budget);
    out.try_extend_from_slice(bytes)?;
    Ok(out.into_vec())
}

fn unused(
    name: &[u8],
    taken: &Charged<'_, Vec<u8>, ()>,
    separator: u8,
    budget: &Budget,
) -> Result<Vec<u8>> {
    if taken.get(name).is_none() {
        return copy_bytes(name, budget);
    }
    let mut number = 1_u64;
    loop {
        budget.poll()?;
        let mut candidate = BudgetVec::new(budget);
        candidate.try_extend_from_slice(name)?;
        candidate.try_push(separator)?;
        candidate.try_extend_from_slice(number.to_string().as_bytes())?;
        if taken.get(candidate.as_ref()).is_none() {
            return Ok(candidate.into_vec());
        }
        number = number
            .checked_add(1)
            .ok_or_else(|| LimitExceeded::new(LimitCode::StructureTreeCount))?;
    }
}

/// Calls `each` with the pairs of the number or name tree `tree`, whose
/// nodes hold them at `key`, bounded and following each node once.
fn tree_pairs(
    objects: &impl Objects,
    tree: &Value,
    key: &str,
    budget: &Budget,
    mut each: impl FnMut(Value, Value) -> Result<()>,
) -> Result<()> {
    let mut visited: Charged<'_, Reference, ()> = Charged::new(budget);
    let mut pending = BudgetVec::new(budget);
    pending.try_push((copy_for_edit(tree, budget)?, 0_usize))?;
    let mut nodes = 0_usize;
    let mut pairs = 0_usize;
    while let Some((node, depth)) = pending.pop() {
        budget.poll()?;
        let (node, object) = match located(objects, Some(&node))? {
            None | Some((Value::Null, _)) => continue,
            Some((Value::Dict(node), object)) => (node, object),
            _ => {
                return Err(Error::malformed(
                    "a structure lookup tree node is not a dictionary",
                ));
            }
        };
        if let Some(reference) = object
            && visited.insert(reference, (), 0)?.is_some()
        {
            continue;
        }
        editable(&node)?;
        nodes += 1;
        if nodes > MAX_NODES || depth > MAX_TREE_DEPTH {
            return Err(LimitExceeded::new(LimitCode::StructureTreeCount).into());
        }
        if let Some(Value::Array(items)) = follow(objects, node.get(key))? {
            let mut items = items.iter();
            while let (Some(name), Some(value)) = (items.next(), items.next()) {
                budget.poll()?;
                pairs += 1;
                if pairs > MAX_NODES {
                    return Err(LimitExceeded::new(LimitCode::StructureTreeCount).into());
                }
                if let Some(name) = follow(objects, Some(name))? {
                    each(name, copy_for_edit(value, budget)?)?;
                }
            }
        }
        if let Some(Value::Array(kids)) = follow(objects, node.get("Kids"))? {
            for kid in kids.iter() {
                pending.try_push((copy_for_edit(kid, budget)?, depth + 1))?;
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use super::*;
    use pyropdf_store::value::Bytes;
    use pyropdf_store::version::{Document, MemoryBase};

    #[test]
    fn reading_a_cached_target_value_charges_owned_buffers() {
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
        let target = Target {
            version: &version,
            budget: &budget,
        };
        let error = match target.get(reference) {
            Ok(_) => panic!("reading a cached target value ignored the memory budget"),
            Err(error) => error,
        };
        assert_eq!(
            error.limit().map(LimitExceeded::code),
            Some(LimitCode::MemorySize)
        );
    }
}
