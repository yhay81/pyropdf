//! The rules for the navigation to pages that leave the document: links,
//! outline items, named destinations, the open action, and annotations'
//! `/P`. Ported from pylopdf's `rust/src/page_cleanup.rs` at
//! ebb01d3c4c742046e8ce7fd72957cd15a2933321, which walks every piece of
//! navigation; here the index finds the references to the pages, and only
//! a removed named destination makes a walk of the links and the outline
//! that name it.

use std::ops::Deref;
use std::sync::Arc;

use crate::Result;
use crate::budget::BudgetVec;
use crate::memo::Reads;
use crate::value::{Dict, Value};
use crate::version::table::Table;

use super::rules::{aliasing, annotation_array, copy_path};
use super::{Edits, References, Referrer, Role, Rule, Step, find};

/// Bounds recursive indirection through destinations and actions, including
/// malformed cycles. Direct dictionary paths are consumed iteratively.
const MAX_INDIRECTION: usize = crate::pages::MAX_CHAIN;

/// Plans the rules for the navigation to `removed`, the pages that leave
/// the version that `reads` reads, whose index is `index`. `retained`
/// lists the pages that stay. Both are ascending.
///
/// Links to a removed page leave the pages that stay, outline items to it
/// keep no destination, named destinations and the open action to it go,
/// and so does an annotation's `/P` to it. When nothing stays, the outline
/// goes. Refuses, before planning anything, navigation that is a page, a
/// page tree node, or the catalog.
pub fn remove_pages(
    reads: &mut Reads<'_>,
    index: &References,
    removed: &[u32],
    retained: &[u32],
    edits: &mut Edits<'_>,
) -> Result<()> {
    let budget = reads.budget();
    let Some(catalog) = catalog(reads)? else {
        return Ok(());
    };
    check_roots(reads, catalog)?;
    let mut planner = Planner {
        index,
        removed,
        catalog,
        legacy: readable(reads, catalog)?.and_then(|object| {
            match dict_of(&object).and_then(|dict| dict.get("Dests")) {
                Some(Value::Reference(reference)) => Some(reference.number()),
                _ => None,
            }
        }),
        names: BudgetVec::new(budget),
    };
    for &page in removed {
        let generation = reads.generation(page)?;
        for &referrer in index.referrers(page) {
            if is_in(removed, referrer) {
                continue;
            }
            for found in find(reads, Referrer::Object(referrer), page)?.iter() {
                if generation != Some(found.reference.generation()) {
                    continue;
                }
                match found.role {
                    Some(Role::Destination) => {
                        let holder = &found.path[..found.path.len() - 1];
                        planner.navigation(reads, edits, referrer, holder, 0)?;
                    }
                    Some(Role::AnnotationPage) => {
                        edits.apply(reads, index, referrer, &found.path, Rule::RemoveEntry)?;
                    }
                    _ => {}
                }
            }
        }
    }
    let names = planner.names;
    if !names.is_empty() {
        let mut names = names;
        names.sort_unstable();
        named(reads, index, catalog, retained, &names, edits)?;
    }
    if retained.is_empty() {
        let outlines = [Step::Key("Outlines".into())];
        edits.apply(reads, index, catalog, &outlines, Rule::RemoveEntry)?;
    }
    Ok(())
}

struct Planner<'a, 'b> {
    index: &'a References,
    removed: &'a [u32],
    catalog: u32,
    legacy: Option<u32>,
    /// The names of the named destinations that the rules remove.
    names: BudgetVec<'b, Box<[u8]>>,
}

impl Planner<'_, '_> {
    /// Plans the rule for navigation that leads to a removed page: the
    /// destination, destination dictionary, or action at `path` in object
    /// `referrer`, reached through `depth` indirect objects.
    fn navigation(
        &mut self,
        reads: &mut Reads<'_>,
        edits: &mut Edits<'_>,
        referrer: u32,
        path: &[Step],
        depth: usize,
    ) -> Result<()> {
        reads.budget().poll()?;
        // A legacy key may itself be named D, A, or Dest.
        let legacy_name = match path {
            [Step::Key(name)] if self.legacy == Some(referrer) => Some(name),
            [Step::Key(key), Step::Key(name)]
                if referrer == self.catalog && key.as_bytes() == b"Dests" =>
            {
                Some(name)
            }
            _ => None,
        };
        if let Some(name) = legacy_name {
            self.name(name.as_bytes())?;
            return edits.apply(reads, self.index, referrer, path, Rule::RemoveEntry);
        }
        // Strip direct destination dictionaries iteratively.
        let mut path = path;
        while let Some((Step::Key(key), parent)) = path.split_last()
            && key.as_bytes() == b"D"
        {
            reads.budget().poll()?;
            path = parent;
        }
        let Some(object) = readable(reads, referrer)? else {
            return Ok(());
        };
        if path.is_empty() {
            // An indirect destination or action: the rule follows whatever
            // holds it.
            if depth >= MAX_INDIRECTION {
                return Err(crate::Error::malformed(
                    "navigation reference chain is too deep or cyclic",
                ));
            }
            for &holder in self.index.referrers(referrer) {
                if is_in(self.removed, holder) {
                    continue;
                }
                for found in find(reads, Referrer::Object(holder), referrer)?.iter() {
                    if found.reference.generation() != object.generation() {
                        continue;
                    }
                    self.navigation(reads, edits, holder, &found.path, depth + 1)?;
                }
            }
            return Ok(());
        }
        let top = &**object.value();
        let index = self.index;
        if let Some((Step::Key(key), parent)) = path.split_last()
            && matches!(key.as_bytes(), b"Dest" | b"A")
            && let Some(Value::Dict(link)) = at(top, parent)
            && is_link(link)
        {
            let inline = match parent {
                [Step::Key(key), Step::Index(_)] if key.as_bytes() == b"Annots" => {
                    dict_of(&object).is_some_and(|dict| type_of(dict) == Some(b"Page"))
                }
                [Step::Index(_)] => annotation_array(reads, index, referrer)?,
                _ => false,
            };
            if inline {
                if is_aliasing(link) {
                    return Err(aliasing());
                }
                return edits.apply(reads, index, referrer, parent, Rule::RemoveEntry);
            }
        }
        // Name tree leaves may be direct dictionaries nested in a catalog.
        if let [prefix @ .., Step::Key(key), Step::Index(position)] = path
            && key.as_bytes() == b"Names"
            && position % 2 == 1
            && let Some(Value::Dict(node)) = at(top, prefix)
        {
            if is_aliasing(node) {
                return Err(aliasing());
            }
            if let Some(Value::Array(items)) = node.get("Names")
                && let Some(name) = items.get(position - 1)
                && let Some(held) = resolved(reads, name)?
                && let Value::String(name) = &*held
            {
                self.name(name.as_bytes())?;
            }
            edits.apply(reads, index, referrer, path, Rule::RemoveEntry)?;
            let mut key = copy_path(path, reads.budget())?;
            *key.last_mut().expect("nonempty name-tree path") = Step::Index(position - 1);
            return edits.apply(reads, index, referrer, &key, Rule::RemoveEntry);
        }
        let Some(dict) = dict_of(&object) else {
            return Ok(());
        };
        match path {
            [Step::Key(key)] if matches!(key.as_bytes(), b"Dest" | b"A") => {
                if is_aliasing(dict) {
                    return Err(aliasing());
                }
                let rule = if is_link(dict) {
                    Rule::RemoveReferrer
                } else {
                    Rule::Unresolve
                };
                edits.apply(reads, index, referrer, path, rule)
            }
            [Step::Key(key)] if key.as_bytes() == b"OpenAction" && referrer == self.catalog => {
                edits.apply(reads, index, referrer, path, Rule::RemoveEntry)
            }
            _ => Ok(()),
        }
    }

    fn name(&mut self, name: &[u8]) -> Result<()> {
        let mut owned = BudgetVec::new(self.names.budget());
        owned.try_extend_from_slice(name)?;
        self.names.try_push(owned.into_vec().into_boxed_slice())?;
        Ok(())
    }
}

/// Plans the rules for the links of `retained` and the outline items that
/// name a destination of `names`, which the rules removed.
fn named(
    reads: &mut Reads<'_>,
    index: &References,
    catalog: u32,
    retained: &[u32],
    names: &[Box<[u8]>],
    edits: &mut Edits<'_>,
) -> Result<()> {
    let budget = reads.budget();
    for &page in retained {
        budget.poll()?;
        let Some(object) = readable(reads, page)? else {
            continue;
        };
        let Some(annots) = dict_of(&object).and_then(|dict| dict.get("Annots")) else {
            continue;
        };
        let (holder, prefix) = match annots {
            Value::Reference(reference) => (reference.number(), None),
            _ => (page, Some(Step::Key("Annots".into()))),
        };
        let held = resolved(reads, annots)?;
        let Some(Value::Array(items)) = held.as_deref() else {
            continue;
        };
        for (at, item) in items.iter().enumerate() {
            budget.poll()?;
            let held = resolved(reads, item)?;
            let Some(Value::Dict(dict)) = held.as_deref() else {
                continue;
            };
            if !is_link(dict) || !names_one(reads, dict, names)? {
                continue;
            }
            if is_aliasing(dict) {
                return Err(aliasing());
            }
            if let Value::Reference(link) = item {
                edits.apply(reads, index, link.number(), &[], Rule::RemoveReferrer)?;
            } else {
                let mut entry = BudgetVec::new(budget);
                entry.try_extend(prefix.clone())?;
                entry.try_push(Step::Index(at))?;
                edits.apply(reads, index, holder, &entry, Rule::RemoveEntry)?;
            }
        }
    }
    outline(reads, index, catalog, names, edits)
}

/// Plans the rules for the outline items that name a destination of
/// `names`.
fn outline(
    reads: &mut Reads<'_>,
    index: &References,
    catalog: u32,
    names: &[Box<[u8]>],
    edits: &mut Edits<'_>,
) -> Result<()> {
    let budget = reads.budget();
    let Some(catalog_object) = readable(reads, catalog)? else {
        return Ok(());
    };
    let Some(root) = dict_of(&catalog_object).and_then(|dict| dict.get("Outlines")) else {
        return Ok(());
    };
    let Value::Reference(root) = root else {
        return Ok(());
    };
    let mut seen: Table<()> = Table::new();
    let mut pending = BudgetVec::new(budget);
    pending.try_push(root.number())?;
    while let Some(number) = pending.pop() {
        budget.poll()?;
        if seen.get(number).is_some() {
            continue;
        }
        seen.insert(number, (), budget)?;
        let Some(object) = readable(reads, number)? else {
            continue;
        };
        let Some(dict) = dict_of(&object) else {
            continue;
        };
        if is_aliasing(dict) {
            return Err(aliasing());
        }
        for key in ["Next", "First"] {
            if let Some(&Value::Reference(next)) = dict.get(key) {
                pending.try_push(next.number())?;
            }
        }
        if number == root.number() {
            continue;
        }
        for key in ["Dest", "A"] {
            let Some(value) = dict.get(key) else {
                continue;
            };
            let named = if key == "Dest" {
                is_named(reads, value, names)?
            } else {
                action_names(reads, value, names)?
            };
            if named {
                let path = [Step::Key(key.into())];
                edits.apply(reads, index, number, &path, Rule::Unresolve)?;
            }
        }
    }
    Ok(())
}

/// Returns whether the link or outline item `dict` names a destination of
/// `names`, in `/Dest` or in a GoTo action's `/D`.
fn names_one(reads: &mut Reads<'_>, dict: &Dict, names: &[Box<[u8]>]) -> Result<bool> {
    if let Some(destination) = dict.get("Dest")
        && is_named(reads, destination, names)?
    {
        return Ok(true);
    }
    match dict.get("A") {
        Some(action) => action_names(reads, action, names),
        None => Ok(false),
    }
}

/// Returns whether `action`, or the object it refers to, is a GoTo action
/// whose `/D` names a destination of `names`.
fn action_names(reads: &mut Reads<'_>, action: &Value, names: &[Box<[u8]>]) -> Result<bool> {
    let held = resolved(reads, action)?;
    let Some(Value::Dict(action)) = held.as_deref() else {
        return Ok(false);
    };
    if !matches!(action.get("S"), Some(Value::Name(kind)) if kind.as_bytes() == b"GoTo") {
        return Ok(false);
    }
    match action.get("D") {
        Some(destination) => is_named(reads, destination, names),
        None => Ok(false),
    }
}

/// Returns whether `value`, or the object it refers to, is a name or a
/// string of `names`.
fn is_named(reads: &mut Reads<'_>, value: &Value, names: &[Box<[u8]>]) -> Result<bool> {
    let held = resolved(reads, value)?;
    let bytes = match held.as_deref() {
        Some(Value::Name(name)) => name.as_bytes(),
        Some(Value::String(string)) => string.as_bytes(),
        _ => return Ok(false),
    };
    Ok(names
        .binary_search_by(|name| name.as_ref().cmp(bytes))
        .is_ok())
}

/// Returns the catalog's object number, if the trailer refers to one.
fn catalog(reads: &mut Reads<'_>) -> Result<Option<u32>> {
    Ok(match reads.trailer().get("Root") {
        Some(&Value::Reference(root)) => Some(root.number()),
        _ => None,
    })
}

/// Refuses an outline root, a names dictionary, or a destination tree or
/// dictionary that is a page, a page tree node, or the catalog.
fn check_roots(reads: &mut Reads<'_>, catalog: u32) -> Result<()> {
    let Some(object) = readable(reads, catalog)? else {
        return Ok(());
    };
    let Some(dict) = dict_of(&object) else {
        return Ok(());
    };
    for key in ["Outlines", "Names", "Dests"] {
        let Some(root) = dict.get(key) else {
            continue;
        };
        let held = resolved(reads, root)?;
        let Some(Value::Dict(root)) = held.as_deref() else {
            continue;
        };
        if is_aliasing(root) {
            return Err(aliasing());
        }
        if key == "Names"
            && let Some(dests) = root.get("Dests")
            && let Some(held) = resolved(reads, dests)?
            && let Value::Dict(dests) = &*held
            && is_aliasing(dests)
        {
            return Err(aliasing());
        }
    }
    Ok(())
}

/// A direct value is borrowed; an indirect value shares the store's parse.
enum Resolved<'a> {
    Borrowed(&'a Value),
    Shared(Arc<Value>),
}

impl Deref for Resolved<'_> {
    type Target = Value;
    fn deref(&self) -> &Value {
        match self {
            Self::Borrowed(value) => value,
            Self::Shared(value) => value,
        }
    }
}

fn resolved<'a>(reads: &mut Reads<'_>, value: &'a Value) -> Result<Option<Resolved<'a>>> {
    reads.budget().poll()?;
    match value {
        Value::Reference(reference) => Ok(readable(reads, reference.number())?
            .filter(|object| object.generation() == reference.generation())
            .map(|object| Resolved::Shared(Arc::clone(object.value())))),
        _ => Ok(Some(Resolved::Borrowed(value))),
    }
}

fn readable(reads: &mut Reads<'_>, number: u32) -> Result<Option<crate::version::Object>> {
    reads.get(number)
}

fn dict_of(object: &crate::version::Object) -> Option<&Dict> {
    match &**object.value() {
        Value::Dict(dict) => Some(dict),
        Value::Stream(stream) => Some(stream.dict()),
        _ => None,
    }
}

fn type_of(dict: &Dict) -> Option<&[u8]> {
    match dict.get("Type") {
        Some(Value::Name(name)) => Some(name.as_bytes()),
        _ => None,
    }
}

fn is_aliasing(dict: &Dict) -> bool {
    matches!(type_of(dict), Some(b"Page" | b"Pages" | b"Catalog"))
}

fn is_link(dict: &Dict) -> bool {
    matches!(dict.get("Subtype"), Some(Value::Name(kind)) if kind.as_bytes() == b"Link")
}

fn is_in(sorted: &[u32], number: u32) -> bool {
    sorted.binary_search(&number).is_ok()
}

/// Looks up a direct path without copying containers.
fn at<'a>(top: &'a Value, path: &[Step]) -> Option<&'a Value> {
    path.iter()
        .try_fold(top, |value, step| match (value, step) {
            (Value::Dict(dict), Step::Key(key)) => dict.get(key.as_bytes()),
            (Value::Stream(stream), Step::Key(key)) => stream.dict().get(key.as_bytes()),
            (Value::Array(array), Step::Index(index)) => array.get(*index),
            _ => None,
        })
}
