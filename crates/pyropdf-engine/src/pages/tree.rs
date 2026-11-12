//! Page-tree edits flatten the tree: the root lists every page, and a page
//! whose parent or inherited attributes would change keeps those attributes
//! under its new parent.

use std::sync::Arc;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::memo::{Memo, Reads};
use pyropdf_store::pages::{PageIndex, PageTree};
use pyropdf_store::refs::{self, Edits};
use pyropdf_store::settle::Scope;
use pyropdf_store::value::{Array, Dict, Name, Reference, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::display::number;

use super::copy::Copies;
use super::{PageError, Result, indirect, not_a_page, resolve, target, with_savepoint};

/// The attributes that pages inherit (ISO 32000-2 table 31).
pub(super) const INHERITABLE: [&str; 4] = ["Resources", "MediaBox", "CropBox", "Rotate"];

/// Lists `pages`, objects of current pages, as the document's pages in
/// order, in one transaction. An entry that `copies` marks becomes a copy
/// of the page that it names, with annotations and structure of its own;
/// every other entry names a page that stays, at most once. The pages that
/// leave run the rules of the references to them (DESIGN.md section 8):
/// the navigation that leads to them goes, and they and the objects that
/// only they referred to are freed. A refusal leaves the document as it
/// was.
pub fn set_pages(
    transaction: &mut Transaction,
    memo: &Memo<PageTree, PageIndex>,
    references: &refs::Cache,
    pages: impl ExactSizeIterator<Item = Reference>,
    copies: &[bool],
    budget: &Budget,
) -> Result<()> {
    if !copies.is_empty() && copies.len() != pages.len() {
        return Err(PageError::Value(
            "copies must mark every page or none".into(),
        ));
    }
    let index = PageIndex::memoized(memo, &transaction.snapshot(), budget)?;
    let mut order = BudgetVec::new(budget);
    let mut retained = BudgetVec::new(budget);
    let mut sources = BudgetVec::new(budget);
    for (at, page) in pages.enumerate() {
        budget.poll()?;
        let number = page.number();
        if index.index_of(page).is_none() {
            return Err(not_a_page(page));
        }
        order.try_push(page)?;
        sources.try_push(number)?;
        if !copies.get(at).copied().unwrap_or(false) {
            retained.try_push(number)?;
        }
    }
    retained.sort_unstable();
    if retained.windows(2).any(|pair| pair[0] == pair[1]) {
        return Err(PageError::Value(
            "a page stays at most once; copies repeat it".into(),
        ));
    }
    let mut before = BudgetVec::new(budget);
    let mut removed = BudgetVec::new(budget);
    for (at, page) in index.pages().iter().enumerate() {
        let number = indirect(page, at)?.number();
        before.try_push(number)?;
        if retained.binary_search(&number).is_err() {
            removed.try_push(number)?;
        }
    }
    let mut removed = removed.into_vec();
    removed.sort_unstable();
    removed.dedup();
    with_savepoint(transaction, |transaction| {
        let added = copy_pages(transaction, &index, &mut order, copies, budget)?;
        for page in &added {
            retained.try_push(page.number())?;
        }
        retained.sort_unstable();
        let pages = Pages {
            before: &before,
            order: &order,
            sources: &sources,
            removed: &removed,
            retained: &retained,
            added: &added,
        };
        remove_pages(references, transaction, &index, &pages, budget)
    })
}

/// Writes a copy of each page of `order` that `copies` marks and puts it
/// in its place, and returns the copies.
#[expect(
    clippy::disallowed_methods,
    reason = "only the fixed INHERITABLE keys are inserted, with values copied through the budget"
)]
fn copy_pages(
    transaction: &mut Transaction,
    index: &PageIndex,
    order: &mut [Reference],
    copies: &[bool],
    budget: &Budget,
) -> Result<Vec<Reference>> {
    let mut added = BudgetVec::new(budget);
    if !copies.contains(&true) {
        return Ok(added.into_vec());
    }
    let root = Root::read(transaction.version(), budget)?.reference;
    let mut copier = Copies::new(transaction, budget)?;
    for (entry, _) in order.iter_mut().zip(copies).filter(|(_, copy)| **copy) {
        let source = *entry;
        let at = index.index_of(source).ok_or_else(|| not_a_page(source))?;
        let page = &index.pages()[at];
        // The edits of the transaction so far, with what the page inherits.
        let mut dict = match resolve(
            transaction.version(),
            Some(&Value::Reference(source)),
            budget,
        )? {
            Some(Value::Dict(dict)) => dict,
            _ => {
                return Err(PageError::Malformed(
                    "the page to copy is no longer a dictionary".into(),
                ));
            }
        };
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /Parent key"
        )]
        dict.insert(Name::from("Parent"), Value::Reference(root));
        for key in INHERITABLE {
            match page.inherited(key) {
                Some(value) => {
                    dict.insert(Name::from(key), refs::copy_for_edit(value, budget)?);
                }
                None => {
                    dict.remove(key);
                }
            }
        }
        let copy = copier.copy(transaction, source, dict)?;
        added.try_push(copy)?;
        *entry = copy;
    }
    copier.finish(transaction)?;
    Ok(added.into_vec())
}

/// The pages of [`set_pages`]: in their order before, in the new order,
/// the pages that the new order copies its entries from, the numbers of
/// those that leave and of those that stay, ascending, and the copies.
struct Pages<'p> {
    before: &'p [u32],
    order: &'p [Reference],
    sources: &'p [u32],
    removed: &'p [u32],
    retained: &'p [u32],
    added: &'p [Reference],
}

/// Runs the rules for the removed pages, writes the page tree with the new
/// order and the page labels that follow the pages, and frees what the
/// removed pages and the labels before leave unreferenced.
fn remove_pages(
    references: &refs::Cache,
    transaction: &mut Transaction,
    index: &PageIndex,
    pages: &Pages<'_>,
    budget: &Budget,
) -> Result<()> {
    let Pages {
        before: old,
        order,
        sources,
        removed,
        retained,
        added,
    } = *pages;
    let before = transaction.snapshot();

    let labels = refs::relabel(&mut Reads::new(&before, budget), old, sources)?;
    if removed.is_empty()
        && labels
            .as_ref()
            .is_none_or(|labels| labels.nodes().is_empty())
    {
        if let Some(labels) = &labels {
            labels.write(transaction, budget)?;
        }
        let view = transaction.snapshot();
        return rewrite_with(transaction, &view, index, order, added, budget);
    }
    let parent = Arc::clone(transaction.parent());
    let references = references.get(&parent, budget)?;
    let references = match parent.changes(&before, budget)? {
        Some(changes) if !changes.is_empty() => {
            Arc::new(references.update(&mut Reads::new(&before, budget), &changes)?)
        }
        _ => references,
    };
    let mut edits = Edits::new(budget);
    if !removed.is_empty() {
        refs::remove_pages(
            &mut Reads::new(&before, budget),
            &references,
            removed,
            retained,
            &mut edits,
        )?;
    }
    edits.write(transaction, budget)?;
    if let Some(labels) = &labels {
        labels.write(transaction, budget)?;
    }
    let view = transaction.snapshot();
    rewrite_with(transaction, &view, index, order, added, budget)?;
    let after = transaction.snapshot();
    let references = match before.changes(&after, budget)? {
        Some(changes) => references.update(&mut Reads::new(&after, budget), &changes)?,
        None => return Ok(()),
    };
    let mut candidates = BudgetVec::new(budget);
    candidates.try_extend_from_slice(edits.orphans())?;
    if let Some(labels) = &labels {
        candidates.try_extend_from_slice(labels.nodes())?;
    }
    let freed = refs::unreferenced(&references, removed, &candidates, budget)?;
    for &number in freed.iter() {
        transaction.free(number, budget)?;
    }
    Ok(())
}

/// Adds a blank page of `width` by `height` points at `at`, counted from
/// 0 and at most the page count, in one transaction, and returns its
/// object.
#[expect(
    clippy::disallowed_methods,
    reason = "a blank page has a fixed set of dictionary keys"
)]
pub fn new_page(
    transaction: &mut Transaction,
    memo: &Memo<PageTree, PageIndex>,
    at: usize,
    width: f64,
    height: f64,
    budget: &Budget,
) -> Result<Reference> {
    with_savepoint(transaction, |transaction| {
        // Read earlier edits through the same settled view as the page handles.
        transaction.settle(Scope::Commit, budget)?;
        let version = transaction.snapshot();
        let index = PageIndex::memoized(memo, &version, budget)?;
        let root = Root::read(&version, budget)?;
        let root_dict = root.dict();
        let mut page = Dict::new();
        page.insert(Name::from("Type"), Value::Name(Name::from("Page")));
        page.insert(Name::from("Parent"), Value::Reference(root.reference));
        let media = Value::Array(Array::from(vec![
            Value::Integer(0),
            Value::Integer(0),
            number(width),
            number(height),
        ]));
        page.insert(Name::from("MediaBox"), media.clone());
        // What the blank page would otherwise inherit from the root.
        if root_dict.get("CropBox").is_some() {
            page.insert(Name::from("CropBox"), media);
        }
        if root_dict.get("Rotate").is_some() {
            page.insert(Name::from("Rotate"), Value::Integer(0));
        }
        page.insert(Name::from("Resources"), Value::Dict(Dict::new()));

        let mut order = BudgetVec::new(budget);
        order.try_reserve_exact(index.len().saturating_add(1))?;
        for (at, page) in index.pages().iter().enumerate() {
            budget.poll()?;
            order.try_push(indirect(page, at)?)?;
        }
        let at = at.min(order.len());
        let reference = transaction.allocate()?;
        transaction.write(reference.number(), &Value::Dict(page), budget)?;
        order.try_push(reference)?;
        order[at..].rotate_right(1);
        rewrite_with(transaction, &version, &index, &order, &[reference], budget)?;
        Ok(reference)
    })
}

/// Returns the reference through which page `page` inherits `inherited`,
/// its value of `key`: the entry of the page itself or of the nearest node
/// of its `/Parent` chain in `version` that holds the key, when that entry
/// is a reference to `inherited`. Returns `None` for a direct value, or
/// for a chain that the page walk did not follow.
fn held(
    version: &Version,
    page: &Dict,
    key: &str,
    inherited: &Value,
    budget: &Budget,
) -> Result<Option<Reference>> {
    let mut node: Option<Arc<Value>> = None;
    let mut checkpoint = None;
    for depth in 0.. {
        budget.check(LimitCode::ObjectDepth, depth)?;
        let dict = match node.as_deref() {
            None => page,
            Some(Value::Dict(dict)) => dict,
            Some(_) => return Ok(None),
        };
        match dict.get(key) {
            Some(Value::Reference(entry)) => {
                // The walk passes over an entry that names no object.
                if let Some(value) = target(version, *entry, budget)? {
                    let same = std::ptr::eq(&*value, inherited) || *value == *inherited;
                    return Ok(same.then_some(*entry));
                }
            }
            Some(Value::Null) | None => {}
            Some(_) => return Ok(None),
        }
        let Some(Value::Reference(parent)) = dict.get("Parent") else {
            return Ok(None);
        };
        // The /Parent chain can disagree with the /Kids walk. Sampling one
        // ancestor at doubling intervals detects any cycle in constant space.
        if checkpoint == Some(*parent) {
            // With a finite ceiling the old walk refused at its first level
            // above the limit. A cycle guarantees it would reach that level.
            if let Some(depth) = budget
                .limit(LimitCode::ObjectDepth)
                .and_then(|limit| limit.checked_add(1))
            {
                budget.check(LimitCode::ObjectDepth, depth)?;
            }
            return Ok(None);
        }
        if depth.is_power_of_two() || checkpoint.is_none() {
            checkpoint = Some(*parent);
        }
        node = target(version, *parent, budget)?;
    }
    Ok(None)
}

/// Writes the root node with `order` as its kids, and each page that the
/// flattening changes. `added` are pages that the transaction wrote, which
/// the index does not list yet, in allocation order.
#[expect(
    clippy::disallowed_methods,
    reason = "the resolved entries and inserted keys are bounded by the fixed INHERITABLE array"
)]
pub(super) fn rewrite_with(
    transaction: &mut Transaction,
    version: &Version,
    index: &PageIndex,
    order: &[Reference],
    added: &[Reference],
    budget: &Budget,
) -> Result<()> {
    let node = Root::read(version, budget)?;
    let root = node.reference;
    let Value::Dict(mut root_dict) = refs::copy_for_edit(&node.value, budget)? else {
        unreachable!("Root::read checks the dictionary");
    };
    let inherited_root: Vec<Option<Value>> = INHERITABLE
        .iter()
        .map(|key| resolve(version, root_dict.get(key), budget))
        .collect::<pyropdf_store::Result<_>>()?;
    for &reference in order {
        budget.poll()?;
        if added.binary_search(&reference).is_ok() {
            continue;
        }
        let at = index
            .index_of(reference)
            .ok_or_else(|| not_a_page(reference))?;
        let page = &index.pages()[at];
        let parent_is_root = page.dict().get("Parent") == Some(&Value::Reference(root));
        let mut changes = !parent_is_root;
        for (key, under_root) in INHERITABLE.iter().zip(&inherited_root) {
            let own = resolve(version, page.dict().get(key), budget)?;
            if own.is_none() && page.inherited(key) != under_root.as_ref() {
                changes = true;
            }
        }
        if !changes {
            continue;
        }
        // The rules may have edited the page in this transaction.
        let mut dict = match resolve(version, Some(&Value::Reference(reference)), budget)? {
            Some(Value::Dict(dict)) => dict,
            _ => {
                return Err(PageError::Malformed(
                    "a retained page is no longer a dictionary".into(),
                ));
            }
        };
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /Parent key"
        )]
        dict.insert(Name::from("Parent"), Value::Reference(root));
        for key in INHERITABLE {
            match page.inherited(key) {
                Some(value) => {
                    // A reference stays a reference, so that the pages keep
                    // sharing what they shared.
                    let value = match held(version, page.dict(), key, value, budget)? {
                        Some(held) => Value::Reference(held),
                        None => refs::copy_for_edit(value, budget)?,
                    };
                    dict.insert(Name::from(key), value);
                }
                None => {
                    dict.remove(key);
                }
            }
        }
        transaction.write(reference.number(), &Value::Dict(dict), budget)?;
    }
    let mut kids = BudgetVec::new(budget);
    kids.try_extend(order.iter().copied().map(Value::Reference))?;
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /Kids key"
    )]
    root_dict.insert(
        Name::from("Kids"),
        Value::Array(Array::from(kids.into_vec())),
    );
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /Count key"
    )]
    root_dict.insert(Name::from("Count"), Value::Integer(order.len() as i64));
    transaction.write(root.number(), &Value::Dict(root_dict), budget)?;
    Ok(())
}

/// The validated, indirect root of a page tree. Reads share its stored value.
pub(super) struct Root {
    pub(super) reference: Reference,
    value: Arc<Value>,
}

impl Root {
    pub(super) fn read(version: &Version, budget: &Budget) -> Result<Self> {
        let target_catalog;
        let catalog = match version.trailer().get("Root") {
            Some(Value::Reference(reference)) => {
                target_catalog = target(version, *reference, budget)?;
                target_catalog.as_deref()
            }
            direct => direct,
        };
        let Some(Value::Dict(catalog)) = catalog else {
            return Err(PageError::Malformed(
                "the trailer names no catalog dictionary".into(),
            ));
        };
        let Some(&Value::Reference(reference)) = catalog.get("Pages") else {
            return Err(PageError::Malformed(
                "the catalog's /Pages is not an indirect page tree".into(),
            ));
        };
        let value = target(version, reference, budget)?
            .filter(|value| matches!(&**value, Value::Dict(_)))
            .ok_or_else(|| PageError::Malformed("the catalog names no page tree".into()))?;
        Ok(Self { reference, value })
    }

    pub(super) fn dict(&self) -> &Dict {
        let Value::Dict(dict) = self.value.as_ref() else {
            unreachable!("Root::read checks the dictionary");
        };
        dict
    }
}

#[cfg(test)]
mod tests;
