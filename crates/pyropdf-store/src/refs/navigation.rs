//! The rules for the navigation to pages that leave the document: links,
//! outline items, named destinations, the open action, and annotations'
//! `/P`. Ported from pylopdf's `rust/src/page_cleanup.rs` at
//! ebb01d3c4c742046e8ce7fd72957cd15a2933321, which walks every piece of
//! navigation; here the index finds the references to the pages, and only
//! a removed named destination makes a walk of the links and the outline
//! that name it.

use crate::Result;
use crate::budget::BudgetVec;
use crate::memo::Reads;
use crate::value::{Dict, Value};
use crate::version::table::Table;

use super::rules::aliasing;
use super::{Edits, References, Referrer, Role, Rule, Step, find};

/// The most steps from a reference to a page up to the navigation that
/// holds it: an indirect destination, then an indirect action.
const MAX_INDIRECTION: usize = 2;

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
        names: BudgetVec::new(budget),
    };
    for &page in removed {
        for &referrer in index.referrers(page) {
            if is_in(removed, referrer) {
                continue;
            }
            for found in find(reads, Referrer::Object(referrer), page)?.iter() {
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
        // A destination dictionary's or a GoTo action's `/D`.
        if let Some((Step::Key(key), parent)) = path.split_last()
            && key.as_bytes() == b"D"
        {
            return self.navigation(reads, edits, referrer, parent, depth);
        }
        let Some(object) = readable(reads, referrer)? else {
            return Ok(());
        };
        if path.is_empty() {
            // An indirect destination or action: the rule follows whatever
            // holds it.
            if depth >= MAX_INDIRECTION {
                return Ok(());
            }
            for &holder in self.index.referrers(referrer) {
                if is_in(self.removed, holder) {
                    continue;
                }
                for found in find(reads, Referrer::Object(holder), referrer)?.iter() {
                    self.navigation(reads, edits, holder, &found.path, depth + 1)?;
                }
            }
            return Ok(());
        }
        let Some(dict) = dict_of(&object) else {
            return Ok(());
        };
        let index = self.index;
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
            [Step::Key(key), Step::Key(name)]
                if key.as_bytes() == b"Dests" && referrer == self.catalog =>
            {
                self.name(name.as_bytes())?;
                edits.apply(reads, index, referrer, path, Rule::RemoveEntry)
            }
            [Step::Key(key), Step::Index(at)] if key.as_bytes() == b"Names" && at % 2 == 1 => {
                if is_aliasing(dict) {
                    return Err(aliasing());
                }
                let name = match dict.get("Names") {
                    Some(Value::Array(items)) => items.get(at - 1).cloned(),
                    _ => None,
                };
                if let Some(name) = name
                    && let Some(Value::String(name)) = resolved(reads, &name)?
                {
                    self.name(name.as_bytes())?;
                }
                edits.apply(reads, index, referrer, path, Rule::RemoveEntry)?;
                let key = [Step::Key("Names".into()), Step::Index(at - 1)];
                edits.apply(reads, index, referrer, &key, Rule::RemoveEntry)
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
        let (holder, prefix, items) = match dict_of(&object).and_then(|dict| dict.get("Annots")) {
            Some(Value::Array(items)) => (page, Some(Step::Key("Annots".into())), items.clone()),
            Some(&Value::Reference(annots)) => match readable(reads, annots.number())? {
                Some(array) => match &**array.value() {
                    Value::Array(items) => (annots.number(), None, items.clone()),
                    _ => continue,
                },
                None => continue,
            },
            _ => continue,
        };
        for (at, item) in items.iter().enumerate() {
            budget.poll()?;
            let Some(Value::Dict(dict)) = resolved(reads, item)? else {
                continue;
            };
            if !is_link(&dict) || !names_one(reads, &dict, names)? {
                continue;
            }
            if is_aliasing(&dict) {
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
    let Some(root) = dict_of(&catalog_object)
        .and_then(|dict| dict.get("Outlines"))
        .cloned()
    else {
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
    let Some(Value::Dict(action)) = resolved(reads, action)? else {
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
    let bytes = match resolved(reads, value)? {
        Some(Value::Name(name)) => Box::<[u8]>::from(name.as_bytes()),
        Some(Value::String(string)) => Box::<[u8]>::from(string.as_bytes()),
        _ => return Ok(false),
    };
    Ok(names.binary_search(&bytes).is_ok())
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
        let Some(Value::Dict(root)) = resolved(reads, root)? else {
            continue;
        };
        if is_aliasing(&root) {
            return Err(aliasing());
        }
        if key == "Names"
            && let Some(dests) = root.get("Dests")
            && let Some(Value::Dict(dests)) = resolved(reads, dests)?
            && is_aliasing(&dests)
        {
            return Err(aliasing());
        }
    }
    Ok(())
}

/// Returns `value`, or the object that it refers to; `None` for a
/// reference to an object that the store cannot read.
fn resolved(reads: &mut Reads<'_>, value: &Value) -> Result<Option<Value>> {
    match value {
        Value::Reference(reference) => Ok(readable(reads, reference.number())?
            .filter(|object| object.generation() == reference.generation())
            .map(|object| (**object.value()).clone())),
        _ => Ok(Some(value.clone())),
    }
}

fn readable(reads: &mut Reads<'_>, number: u32) -> Result<Option<crate::version::Object>> {
    match reads.get(number) {
        Ok(object) => Ok(object),
        Err(error) if error.limit().is_some() => Err(error),
        Err(_) => Ok(None),
    }
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
