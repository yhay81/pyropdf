//! Remove fields and widgets and flatten their appearances into page content.

use std::collections::HashSet;
use std::sync::Arc;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::memo::{Memo, Reads};
use pyropdf_store::pages::{PageIndex, PageTree};
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::value::{Array, Dict, Reference, Value};
use pyropdf_store::version::{Transaction, Version};

use super::copy::copy_value;
use super::field_edit::form_object;
use super::fill::base;
use super::fill_index::FillCache;
use super::flatten::{self as flattening, Missing, Selection};
use super::traversal::{Form, Id, id, insert_set, object, stale, visit};
use super::tree_edit::edit_array;
use crate::annotations::{
    AnnotationError as Error, AnnotationResult as Result, PageAnnots, synthesized,
};

/// Removes the widgets that `handles` name from this page and its form. A widget that
/// the form knows leaves every page that lists it and its field's `/Kids`;
/// a field leaves with its last widget, as [`form_field_remove`] removes
/// it.
pub fn widget_remove(
    annots: &PageAnnots<'_>,
    transaction: &mut Transaction,
    handles: &[(u32, Id)],
    budget: &Budget,
    (cache, draft): (&FillCache, bool),
) -> Result<()> {
    budget.check(LimitCode::AnnotationCount, handles.len() as u64)?;
    let mut positions = BudgetVec::new(budget);
    let mut widgets = BudgetVec::new(budget);
    for &(index, handle) in handles {
        let (position, dict) = annots.find(index, handle)?;
        if annots.name(&dict, "Subtype")?.as_deref() != Some(b"Widget") {
            return Err(Error::Stale(format!(
                "annotation {} is no longer a widget",
                handle.0
            )));
        }
        if handle == (0, 0) {
            positions.try_push(position)
        } else {
            widgets.try_push(handle)
        }?;
    }
    let mut positions = positions.into_vec();
    let mut widgets = widgets.into_vec();
    positions.sort_unstable();
    positions.dedup();
    widgets.sort_unstable();
    widgets.dedup();
    if positions.is_empty() && widgets.is_empty() {
        return Ok(());
    }
    remove_widgets(
        transaction,
        annots,
        &positions,
        &widgets,
        budget,
        (cache, draft),
    )
}

/// Implements [`widget_remove`] in its transaction, for the sorted
/// `widgets` and the sorted `positions` of direct widgets on the page of
/// `annots`.
fn remove_widgets(
    transaction: &mut Transaction,
    annots: &PageAnnots<'_>,
    positions: &[usize],
    widgets: &[Id],
    budget: &Budget,
    (cache, draft): (&FillCache, bool),
) -> Result<()> {
    let before = base(transaction, draft);
    let index = cache.get(&before, budget)?;
    let (fields, detached, unlisted) = leaving(&index.topology.form, widgets, budget)?;
    let (version, pages, page) = annots.pages();
    unlist(
        transaction,
        (version, pages),
        &unlisted,
        (page, positions),
        budget,
    )?;
    unfield(transaction, &fields, &detached, budget)
}

/// The fields that leave the form with their last widget, the widgets that
/// only leave their field, and every widget that leaves its pages, sorted.
type Leaving = (Vec<Id>, Vec<Id>, Vec<Id>);

/// Returns what leaves form `form` when the sorted `widgets` leave it.
fn leaving(form: &Form, widgets: &[Id], budget: &Budget) -> Result<Leaving> {
    let mut fields = BudgetVec::new(budget);
    let mut seen = HashSet::new();
    let mut detached = BudgetVec::new(budget);
    let mut unlisted = BudgetVec::new(budget);
    for &widget in widgets {
        unlisted.try_push(widget)?;
        let Some(owner) = form.owner(widget) else {
            continue;
        };
        let field = form.field(owner).ok_or_else(|| stale(owner))?;
        let last = owner == widget
            || field
                .widgets
                .iter()
                .all(|kid| widgets.binary_search(kid).is_ok());
        if !last {
            detached.try_push(widget)?;
        } else if insert_set(&mut seen, owner, budget)? {
            fields.try_push(owner)?;
            for &kid in &field.widgets {
                unlisted.try_push(kid)?;
            }
        }
    }
    let mut unlisted = unlisted.into_vec();
    unlisted.sort_unstable();
    unlisted.dedup();
    Ok((fields.into_vec(), detached.into_vec(), unlisted))
}

/// Takes `fields` out of the form, with each named parent that one leaves
/// without kids, and the widgets `detached` out of their fields.
fn unfield(
    transaction: &mut Transaction,
    fields: &[Id],
    detached: &[Id],
    budget: &Budget,
) -> Result<()> {
    if fields.is_empty() && detached.is_empty() {
        return Ok(());
    }
    let holder = form_object(transaction, budget)?;
    for &field in fields {
        remove_field(transaction, holder, field, budget)?;
    }
    for &widget in detached {
        let widget = Reference::new(widget.0, widget.1);
        let parent = parent(transaction, widget, budget)?;
        detach(transaction, holder, parent, widget, budget)?;
    }
    Ok(())
}

/// Draws the appearances of the annotations and widgets that `annotations`
/// and `widgets` select into page content and takes them off, in one edit,
/// on page object `page`, or on every page for `None`; returns `false` when
/// `page` is no longer a page. The edit reads the pages after it takes the
/// writer lock and charges every page to its one budget. The caller rolls
/// back the transaction on refusal. Taking the widgets off every page removes the
/// interactive form; taking them off one page takes them out of their
/// fields, with each field that loses its last widget.
pub fn flatten(
    transaction: &mut Transaction,
    pages: &Memo<PageTree, PageIndex>,
    page: Option<Id>,
    selection: Selection,
    (cache, draft): (&FillCache, bool),
    budget: &Budget,
) -> Result<bool> {
    let Selection {
        annotations,
        widgets,
    } = selection;
    let before = base(transaction, draft);
    let version = transaction.snapshot();
    let index = PageIndex::memoized(pages, &version, budget)?;
    let targets = match page {
        Some((number, generation)) => match index.index_of(Reference::new(number, generation)) {
            Some(at) => at..at + 1,
            None => return Ok(false),
        },
        None => 0..index.pages().len(),
    };
    if !annotations && !widgets {
        return Ok(true);
    }
    // Annotations without an appearance are drawn as a render
    // draws them.
    let mut missing = if annotations {
        let objects = synthesized(&version, &index, budget)?;
        Missing::new(objects, budget)?
    } else {
        Missing::default()
    };
    let mut flattened = BudgetVec::new(budget);
    for at in targets {
        budget.poll()?;
        let annots = PageAnnots::at(Arc::clone(&version), Arc::clone(&index), budget, at)?;
        let Some(plan) = flattening::plan(&annots.reader(), &annots.items, selection)? else {
            continue;
        };
        let Some(object) = annots.page().object() else {
            return Err(Error::Pdf("the page is not an indirect object".into()));
        };
        if page.is_some() {
            for &widget in &plan.widgets {
                flattened.try_push(id(widget))?;
            }
        }
        flattening::draw(
            transaction,
            object.number(),
            annots.page(),
            plan,
            &mut missing,
            budget,
        )?;
    }
    if widgets && page.is_none() {
        remove_form(transaction, budget)?;
    } else if widgets {
        let mut flattened = flattened.into_vec();
        flattened.sort_unstable();
        flattened.dedup();
        let index = cache.get(&before, budget)?;
        let (fields, detached, _) = leaving(&index.topology.form, &flattened, budget)?;
        unfield(transaction, &fields, &detached, budget)?;
    }
    Ok(true)
}

/// Takes the interactive form out of the catalog.
fn remove_form(transaction: &mut Transaction, budget: &Budget) -> Result<()> {
    let version = transaction.snapshot();
    let mut reads = Reads::new(&version, budget);
    let Some(Value::Reference(root)) = reads.trailer().get("Root").cloned() else {
        return Ok(());
    };
    let Some(original) = object(&mut reads, id(root))? else {
        return Ok(());
    };
    let Value::Dict(mut catalog) = copy_for_edit(&original, budget)? else {
        return Ok(());
    };
    if catalog.remove("AcroForm").is_none() {
        return Ok(());
    }
    transaction.write(root.number(), &Value::Dict(catalog), budget)?;
    Ok(())
}

/// Takes field `field` out of the form with its widgets, in one edit that
/// reads the pages and the form after taking the writer lock: its widgets
/// leave every page that lists them, and the field leaves the field tree
/// and the calculation order with each named parent that it leaves without
/// kids.
pub fn form_field_remove(
    transaction: &mut Transaction,
    pages: &Memo<PageTree, PageIndex>,
    field: Id,
    (cache, draft): (&FillCache, bool),
    budget: &Budget,
) -> Result<()> {
    let before = base(transaction, draft);
    let index = cache.get(&before, budget)?;
    let found = index
        .topology
        .form
        .field(field)
        .ok_or_else(|| stale(field))?;
    let mut widgets = BudgetVec::new(budget);
    for &widget in &found.widgets {
        widgets.try_push(widget)?;
    }
    let mut widgets = widgets.into_vec();
    widgets.sort_unstable();
    widgets.dedup();
    let version = transaction.snapshot();
    let pages = PageIndex::memoized(pages, &version, budget)?;
    unlist(transaction, (&version, &pages), &widgets, (0, &[]), budget)?;
    let holder = form_object(transaction, budget)?;
    remove_field(transaction, holder, field, budget)
}

/// Takes the sorted `widgets` off every page of `pages`, a version and its
/// page index that the edit has not yet changed, and the direct annotations
/// at the sorted `positions` off its page `page`. A page whose `/Annots`
/// changes has its other direct annotations stored as objects, so that a
/// handle to one that moved goes stale rather than naming another.
fn unlist(
    transaction: &mut Transaction,
    (version, index): (&Arc<Version>, &Arc<PageIndex>),
    widgets: &[Id],
    (page, positions): (usize, &[usize]),
    budget: &Budget,
) -> Result<()> {
    for at in 0..index.pages().len() {
        let annots = PageAnnots::at(Arc::clone(version), Arc::clone(index), budget, at)?;
        let listed = |position: usize, item: &Value| match item {
            Value::Reference(reference) => widgets.binary_search(&id(*reference)).is_ok(),
            _ => at == page && positions.binary_search(&position).is_ok(),
        };
        if !annots
            .items
            .iter()
            .enumerate()
            .any(|(position, item)| listed(position, item))
        {
            continue;
        }
        let mut items = BudgetVec::new(budget);
        for (position, item) in annots.items.iter().enumerate() {
            if listed(position, item) {
                continue;
            }
            let item = match copy_value(item, budget)? {
                entry @ Value::Dict(_) => {
                    let reference = transaction.allocate()?;
                    transaction.write(reference.number(), &entry, budget)?;
                    Value::Reference(reference)
                }
                item => item,
            };
            items.try_push(item)?;
        }
        annots.write_items(transaction, Array::from(items.into_vec()), budget)?;
    }
    Ok(())
}

/// Returns the `/Parent` reference of object `node`.
fn parent(
    transaction: &Transaction,
    node: Reference,
    budget: &Budget,
) -> Result<Option<Reference>> {
    let object = transaction.version().get(node.number(), budget)?;
    Ok(match object.as_ref() {
        Some(object) if object.generation() == node.generation() => match &**object.value() {
            Value::Dict(dict) => match dict.get("Parent") {
                Some(Value::Reference(parent)) => Some(*parent),
                _ => None,
            },
            _ => None,
        },
        _ => None,
    })
}

/// Takes field `field` out of the field tree of form `holder` and its
/// calculation order, with each named parent that it leaves without kids.
fn remove_field(
    transaction: &mut Transaction,
    holder: Reference,
    field: Id,
    budget: &Budget,
) -> Result<()> {
    let mut node = Reference::new(field.0, field.1);
    let mut visited = HashSet::new();
    loop {
        if !visit(&mut visited, id(node), budget)? {
            return Err(Error::Pdf(
                "the field's parent chain contains a cycle".into(),
            ));
        }
        let parent = parent(transaction, node, budget)?;
        detach(transaction, holder, parent, node, budget)?;
        let Some(parent) = parent else {
            return Ok(());
        };
        let version = transaction.version();
        let object = version.get(parent.number(), budget)?;
        let Some(object) = object.filter(|object| object.generation() == parent.generation())
        else {
            return Ok(());
        };
        let Value::Dict(dict) = &**object.value() else {
            return Ok(());
        };
        let named = dict.get("T").is_some() || dict.get("FT").is_some();
        if !named || !childless(transaction, dict, budget)? {
            return Ok(());
        }
        node = parent;
    }
}

/// Returns whether the `/Kids` of field `dict` lists no kid.
fn childless(transaction: &Transaction, dict: &Dict, budget: &Budget) -> Result<bool> {
    let kids = match dict.get("Kids") {
        Some(Value::Reference(kids)) => *kids,
        Some(Value::Array(kids)) => return Ok(kids.is_empty()),
        _ => return Ok(true),
    };
    let object = transaction.version().get(kids.number(), budget)?;
    Ok(match object.as_ref() {
        Some(object) if object.generation() == kids.generation() => {
            !matches!(&**object.value(), Value::Array(kids) if !kids.is_empty())
        }
        _ => true,
    })
}

/// Takes `node` out of its parent's `/Kids`, or the form's `/Fields`, and
/// out of the calculation order.
fn detach(
    transaction: &mut Transaction,
    holder: Reference,
    parent: Option<Reference>,
    node: Reference,
    budget: &Budget,
) -> Result<()> {
    let gone = |items: &mut Array| items.retain(|item| *item != Value::Reference(node));
    edit_array(transaction, holder, "CO", budget, false, gone)?;
    match parent {
        Some(parent) => edit_array(transaction, parent, "Kids", budget, false, gone),
        None => edit_array(transaction, holder, "Fields", budget, false, gone),
    }
}
