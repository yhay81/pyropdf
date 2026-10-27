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
use pyropdf_store::value::{Array, Dict, Name, PdfString, Reference, Value};
use pyropdf_store::version::{Transaction, Version};
use pyropdf_store::{LimitExceeded, Result};

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
    fn get(&self, reference: Reference) -> Result<Option<Value>>;
}

impl Objects for Walk<'_, '_> {
    fn get(&self, reference: Reference) -> Result<Option<Value>> {
        self.object(reference.number())
    }
}

struct Target<'v> {
    version: &'v Version,
    budget: &'v Budget,
}

impl Objects for Target<'_> {
    fn get(&self, reference: Reference) -> Result<Option<Value>> {
        Ok(self
            .version
            .get(reference.number(), self.budget)?
            .filter(|object| object.generation() == reference.generation())
            .map(|object| (**object.value()).clone()))
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
        let source_root_object = match catalog.get("StructTreeRoot") {
            Some(&Value::Reference(root)) => Some(root),
            _ => None,
        };
        let Some(Value::Dict(source_root)) = follow(walk, catalog.get("StructTreeRoot"))? else {
            return Ok(None);
        };
        let source_roles = dict_of(walk, source_root.get("RoleMap"))?;
        let Some(receiving) = Receiving::read(transaction.version(), budget)? else {
            return Ok(None);
        };

        // The source's top-level elements, through its single Document
        // element when the receiving tree has one. Kids stay as written: a
        // lone reference is one kid, not its object.
        let root_kids = kids_of(walk, source_root.get("K"))?;
        let mut skipped = BudgetVec::new(budget);
        skipped.try_extend_from_slice(source_root_object.as_slice())?;
        let mut kids = root_kids.clone();
        if receiving.document.is_some()
            && let [Value::Reference(lone)] = root_kids.as_slice()
            && let Some(Value::Dict(element)) = walk.object(lone.number())?
            && is_document(walk, source_roles.as_ref(), &element)?
        {
            skipped.try_push(*lone)?;
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
            walk.numbers.insert(skipped.number(), top, 0)?;
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
        let shift = receiving.next_key.saturating_sub(smallest);
        for (key, _) in pairs.iter_mut() {
            *key = key.saturating_add(shift);
        }
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
        let mut classes = receiving.classes.clone().unwrap_or_default();
        let mut classes_changed = false;
        let mut renames: Charged<'b, Name, Name> = Charged::new(budget);
        if let Some(source_classes) = dict_of(walk, source_root.get("ClassMap"))? {
            let mut taken = Charged::new(budget);
            for name in classes.keys() {
                taken.insert(name.as_bytes().to_vec(), (), name.as_bytes().len() as u64)?;
            }
            for (name, value) in source_classes.iter() {
                if classes.get(name.as_bytes()) == Some(value) {
                    continue;
                }
                let new = unused(name.as_bytes(), &taken, b'_');
                taken.insert(new.clone(), (), new.len() as u64)?;
                let copied = walk.value(transaction, value, 1)?;
                classes.insert(Name::new(new.clone()), copied);
                classes_changed = true;
                if new != name.as_bytes() {
                    renames.insert(name.clone(), Name::new(new), 0)?;
                }
            }
        }

        // Elements: roles, classes, and IDs that the receiving tree holds
        // otherwise.
        let mut roles = receiving.roles.clone().unwrap_or_default();
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
                    let bytes = name.as_bytes().to_vec();
                    let size = bytes.len() as u64;
                    taken_ids.insert(bytes, (), size)?;
                    ids.try_push((name, value))?;
                }
                Ok(())
            })?;
        }
        let mut ids_changed = false;
        let mut visited: Charged<'b, u32, ()> = Charged::new(budget);
        let mut stack = BudgetVec::new(budget);
        for kid in kids.iter().rev() {
            if let &Value::Reference(kid) = kid {
                stack.try_push(kid)?;
            }
        }
        while let Some(reference) = stack.pop() {
            budget.poll()?;
            if skipped.contains(&reference) || visited.insert(reference.number(), (), 0)?.is_some()
            {
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
            let Some(Value::Dict(mut element)) = walk.object(reference.number())? else {
                continue;
            };
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
                        roles.insert(role.clone(), ours.clone());
                        roles_changed = true;
                    }
                    _ => {}
                }
            }
            if renames.len() > 0
                && let Some(classes) = element.get_mut("C")
            {
                let mut rename = |class: &mut Value| {
                    if let Value::Name(name) = class
                        && let Some(new) = renames.get(name)
                    {
                        *name = new.clone();
                        changed = true;
                    }
                };
                match classes {
                    Value::Array(items) => items.iter_mut().for_each(&mut rename),
                    other => rename(other),
                }
            }
            if let Some(Value::String(identifier)) = element.get("ID") {
                let new = unused(identifier.as_bytes(), &taken_ids, b'+');
                let size = new.len() as u64;
                let string = PdfString::new(new.clone(), identifier.form());
                taken_ids.insert(new, (), size)?;
                element.insert(Name::from("ID"), Value::String(string.clone()));
                changed = true;
                if let Value::Reference(copied) = walk.reference(transaction, reference)? {
                    ids.try_push((string, Value::Reference(copied)))?;
                    ids_changed = true;
                }
            }
            if changed {
                walk.edited
                    .try_push((reference.number(), Value::Dict(element)))?;
            }
        }
        walk.edited.sort_by_key(|&(number, _)| number);
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
            *kid = walk.value(transaction, kid, 1)?;
            if let Value::Dict(element) = kid
                && is_element(element)
            {
                element.insert(Name::from("P"), Value::Reference(self.top));
            }
        }
        for (_, value) in self.pairs.iter_mut() {
            *value = walk.value(transaction, value, 1)?;
        }
        if let Some(namespaces) = &mut self.namespaces {
            for item in namespaces.iter_mut() {
                *item = walk.value(transaction, item, 1)?;
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
            ..
        } = self.receiving;
        let target = Target {
            version: transaction.version(),
            budget,
        };
        // The receiving parent tree takes the shifted pairs after its own.
        let mut flat = BudgetVec::new(budget);
        flat.try_reserve_exact(2 * self.pairs.len())?;
        for (key, value) in self.pairs.into_vec() {
            flat.try_push(Value::Integer(key))?;
            flat.try_push(value)?;
        }
        let mut leaf = None;
        if !flat.is_empty() {
            if tree_dict.get("Nums").is_none()
                && matches!(tree_dict.get("Kids"), Some(Value::Array(_)))
            {
                let limits = Array::from(vec![flat[0].clone(), flat[flat.len() - 2].clone()]);
                let mut node = Dict::new();
                node.insert(
                    Name::from("Nums"),
                    Value::Array(Array::from(flat.into_vec())),
                );
                node.insert(Name::from("Limits"), Value::Array(limits));
                leaf = Some(node);
            } else {
                let mut nums = match follow(&target, tree_dict.get("Nums"))? {
                    Some(Value::Array(nums)) => nums,
                    _ => Array::new(),
                };
                for value in flat.into_vec() {
                    nums.push(value);
                }
                tree_dict.insert(Name::from("Nums"), Value::Array(nums));
            }
        }
        let mut known = match self.namespaces {
            Some(_) => match follow(&target, root_dict.get("Namespaces"))? {
                Some(Value::Array(items)) => items,
                _ => Array::new(),
            },
            None => Array::new(),
        };
        if let Some(namespaces) = &self.namespaces {
            for item in namespaces.iter() {
                if !known.iter().any(|known| known == item) {
                    known.push(item.clone());
                }
            }
        }
        // The top-level elements follow the receiving ones.
        let document = match document {
            Some((element, mut dict)) => {
                let mut kids = kids_of(&target, dict.get("K"))?;
                for kid in self.kids.iter() {
                    kids.push(kid.clone());
                }
                dict.insert(Name::from("K"), Value::Array(kids));
                Some((element, dict))
            }
            None => {
                let mut kids = kids_of(&target, root_dict.get("K"))?;
                for kid in self.kids.iter() {
                    kids.push(kid.clone());
                }
                root_dict.insert(Name::from("K"), Value::Array(kids));
                None
            }
        };
        if let Some(leaf) = leaf {
            let object = transaction.allocate()?;
            transaction.write(object.number(), &Value::Dict(leaf), budget)?;
            if let Some(Value::Array(kids)) = tree_dict.get_mut("Kids") {
                kids.push(Value::Reference(object));
            }
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
        if self.namespaces.is_some() {
            root_dict.insert(Name::from("Namespaces"), Value::Array(known));
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
        let Some(&Value::Reference(catalog)) = version.trailer().get("Root") else {
            return Ok(None);
        };
        let Some(Value::Dict(catalog_dict)) = target.get(catalog)? else {
            return Ok(None);
        };
        let (root, root_dict) = match catalog_dict.get("StructTreeRoot") {
            Some(&Value::Reference(root)) => match target.get(root)? {
                Some(Value::Dict(dict)) => (Some(root), dict),
                _ => (None, new_root()),
            },
            Some(Value::Dict(dict)) => (None, dict.clone()),
            _ => (None, new_root()),
        };
        let roles = dict_of(&target, root_dict.get("RoleMap"))?;
        let classes = dict_of(&target, root_dict.get("ClassMap"))?;
        let (tree, tree_dict) = match root_dict.get("ParentTree") {
            Some(&Value::Reference(tree)) => match target.get(tree)? {
                Some(Value::Dict(dict)) => (Some(tree), dict),
                _ => (None, Dict::new()),
            },
            Some(Value::Dict(dict)) => (None, dict.clone()),
            _ => (None, Dict::new()),
        };
        let mut largest = None;
        tree_pairs(
            &target,
            &Value::Dict(tree_dict.clone()),
            "Nums",
            budget,
            |key, _| {
                if let Value::Integer(key) = key {
                    largest = largest.max(Some(key));
                }
                Ok(())
            },
        )?;
        let next_key = match follow(&target, root_dict.get("ParentTreeNextKey"))? {
            Some(Value::Integer(next)) => next,
            _ => 0,
        }
        .max(largest.map_or(0, |largest: i64| largest.saturating_add(1)));
        let lone = match root_dict.get("K") {
            Some(Value::Array(items)) if items.len() == 1 => items.get(0).cloned(),
            Some(&Value::Reference(kids)) => match target.get(kids)? {
                Some(Value::Array(items)) if items.len() == 1 => items.get(0).cloned(),
                Some(Value::Dict(_)) => Some(Value::Reference(kids)),
                _ => None,
            },
            _ => None,
        };
        let document = match lone {
            Some(Value::Reference(element)) => match target.get(element)? {
                Some(Value::Dict(dict)) if is_document(&target, roles.as_ref(), &dict)? => {
                    Some((element, dict))
                }
                _ => None,
            },
            _ => None,
        };
        Ok(Some(Self {
            catalog: (catalog, catalog_dict),
            root,
            root_dict,
            tree,
            tree_dict,
            next_key,
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

/// Returns `value`, following references, or `None` at the end of a
/// broken or overlong chain.
fn follow(objects: &impl Objects, value: Option<&Value>) -> Result<Option<Value>> {
    let mut current = value.cloned();
    for _ in 0..MAX_CHAIN {
        match current {
            Some(Value::Reference(reference)) => current = objects.get(reference)?,
            other => return Ok(other),
        }
    }
    Ok(None)
}

fn dict_of(objects: &impl Objects, value: Option<&Value>) -> Result<Option<Dict>> {
    Ok(match follow(objects, value)? {
        Some(Value::Dict(dict)) => Some(dict),
        _ => None,
    })
}

/// Returns the kids of `/K`: an array's items, through a reference to an
/// array, or the one kid that it is.
fn kids_of(objects: &impl Objects, value: Option<&Value>) -> Result<Array> {
    Ok(match value {
        Some(Value::Array(kids)) => kids.clone(),
        Some(&Value::Reference(kids)) => match objects.get(kids)? {
            Some(Value::Array(kids)) => kids,
            Some(Value::Null) | None => Array::new(),
            Some(_) => Array::from(vec![Value::Reference(kids)]),
        },
        Some(Value::Null) | None => Array::new(),
        Some(kid) => Array::from(vec![kid.clone()]),
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
    let mut role = role.clone();
    for _ in 0..MAX_ROLE_CHAIN {
        match follow(objects, roles.and_then(|roles| roles.get(role.as_bytes())))? {
            Some(Value::Name(mapped)) if mapped != role => role = mapped,
            _ => break,
        }
    }
    Ok(role)
}

/// Returns a name that `taken` does not hold: `name`, else `name` with
/// `separator` and the smallest number that makes it so.
fn unused(name: &[u8], taken: &Charged<'_, Vec<u8>, ()>, separator: u8) -> Vec<u8> {
    if taken.get(name).is_none() {
        return name.to_vec();
    }
    let mut number = 1_u64;
    loop {
        let mut candidate = name.to_vec();
        candidate.push(separator);
        candidate.extend_from_slice(number.to_string().as_bytes());
        if taken.get(candidate.as_slice()).is_none() {
            return candidate;
        }
        number += 1;
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
    let mut visited: Charged<'_, u32, ()> = Charged::new(budget);
    let mut pending = BudgetVec::new(budget);
    pending.try_push((tree.clone(), 0_usize))?;
    let mut nodes = 0_usize;
    while let Some((node, depth)) = pending.pop() {
        budget.poll()?;
        nodes += 1;
        if nodes > MAX_NODES || depth > MAX_TREE_DEPTH {
            return Err(LimitExceeded::new(LimitCode::StructureTreeCount).into());
        }
        if let Value::Reference(reference) = node
            && visited.insert(reference.number(), (), 0)?.is_some()
        {
            continue;
        }
        let Some(Value::Dict(node)) = follow(objects, Some(&node))? else {
            continue;
        };
        if let Some(Value::Array(items)) = follow(objects, node.get(key))? {
            let mut items = items.iter();
            while let (Some(name), Some(value)) = (items.next(), items.next()) {
                if let Some(name) = follow(objects, Some(name))? {
                    each(name, value.clone())?;
                }
            }
        }
        if let Some(Value::Array(kids)) = follow(objects, node.get("Kids"))? {
            for kid in kids.iter() {
                pending.try_push((kid.clone(), depth + 1))?;
            }
        }
    }
    Ok(())
}
