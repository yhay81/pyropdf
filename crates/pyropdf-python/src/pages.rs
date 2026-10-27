//! The pages of a document for `python/pyropdf/_pages.py` and `_page.py`:
//! the page index (DESIGN.md section 10), page geometry, and the edits of
//! the page tree, each one transaction.
//!
//! A structural edit flattens the tree, as pylopdf's edits do: the root
//! node lists every page, and a page whose parent changes, or whose
//! inherited attributes would change under the root, is rewritten with
//! `/Parent` naming the root and its inherited attributes written out.

use std::sync::Arc;

use pyo3::exceptions::{PyIndexError, PyValueError};
use pyo3::prelude::*;
use pyropdf_store::budget::Budget;
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::{MAX_CHAIN, Page, PageIndex};
use pyropdf_store::refs::{self, Edits};
use pyropdf_store::value::{Array, Dict, Name, Reference, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::document::_Document;
use crate::errors::{MalformedPdfError, store_error};

/// The attributes that pages inherit (ISO 32000-2 table 31).
const INHERITABLE: [&str; 4] = ["Resources", "MediaBox", "CropBox", "Rotate"];

/// hayro's default page, A4 in points, for a page without a usable box.
const A4: (f64, f64) = (595.0, 842.0);

pub(crate) fn index(document: &_Document) -> PyResult<(Arc<Version>, Arc<PageIndex>, Budget)> {
    index_of(document, document.reading_parent()?)
}

/// Returns the page index of the version that an edit of annotations reads,
/// as [`_Document::editing_parent`] gives it.
pub(crate) fn editing_index(
    document: &_Document,
) -> PyResult<(Arc<Version>, Arc<PageIndex>, Budget)> {
    index_of(document, document.editing_parent()?)
}

fn index_of(
    document: &_Document,
    (version, parent): (Arc<Version>, Option<Arc<Version>>),
) -> PyResult<(Arc<Version>, Arc<PageIndex>, Budget)> {
    let budget = document.budget();
    let index = match &parent {
        // An open transaction that has written nothing reads its parent.
        Some(parent) => PageIndex::memoized(&document.pages, parent, &budget),
        None if document.in_transaction() => {
            PageIndex::memoized(&document.draft_pages, &version, &budget)
        }
        None => PageIndex::memoized(&document.pages, &version, &budget),
    }
    .map_err(|e| store_error(&e))?;
    Ok((version, index, budget))
}

/// Resolves the page entries of a batch, such as the `pages` of
/// `doc.render`, to indexes: every page for `None`, negative entries counted
/// from the end.
pub(crate) fn entries(pages: Option<Vec<i64>>, count: usize) -> PyResult<Vec<usize>> {
    let Some(pages) = pages else {
        return Ok((0..count).collect());
    };
    pages
        .into_iter()
        .map(|at| {
            let resolved = if at < 0 { at + count as i64 } else { at };
            usize::try_from(resolved)
                .ok()
                .filter(|&resolved| resolved < count)
                .ok_or_else(|| {
                    PyIndexError::new_err(format!(
                        "page index {at} is out of range for {count} pages"
                    ))
                })
        })
        .collect()
}

/// Returns the number of pages.
#[pyfunction]
pub fn page_count(document: &_Document) -> PyResult<usize> {
    Ok(index(document)?.1.len())
}

/// Returns the object of page `at`, counted from 0, as a number and a
/// generation.
#[pyfunction]
pub fn page_object(document: &_Document, at: usize) -> PyResult<(u32, u16)> {
    let (_, index, _) = index(document)?;
    let page = index
        .get(at)
        .ok_or_else(|| PyIndexError::new_err(format!("page {at} is out of range")))?;
    let object = indirect(page, at)?;
    Ok((object.number(), object.generation()))
}

/// Returns the index of the page whose object is `number` and
/// `generation`, or `None` when the page tree no longer lists it.
#[pyfunction]
pub fn page_index(document: &_Document, number: u32, generation: u16) -> PyResult<Option<usize>> {
    Ok(index(document)?
        .1
        .index_of(Reference::new(number, generation)))
}

/// Returns the objects of every page, in order.
#[pyfunction]
pub fn page_objects(document: &_Document) -> PyResult<Vec<(u32, u16)>> {
    let (_, index, _) = index(document)?;
    index
        .pages()
        .iter()
        .enumerate()
        .map(|(at, page)| indirect(page, at).map(|r| (r.number(), r.generation())))
        .collect()
}

/// Returns the rotation of a page, normalized to 0, 90, 180, or 270, and
/// its display width and height: the crop box clipped to the media box,
/// rotated, with hayro's A4 for an empty box. `None` for a stale page.
#[pyfunction]
pub fn page_geometry(
    document: &_Document,
    number: u32,
    generation: u16,
) -> PyResult<Option<(i64, f64, f64)>> {
    let (_, index, _) = index(document)?;
    let Some(at) = index.index_of(Reference::new(number, generation)) else {
        return Ok(None);
    };
    let page = &index.pages()[at];
    let rotation = rotation(page.rotate());
    let (mut width, mut height) = visible(page).map_or(A4, |(x0, y0, x1, y1)| {
        ((x1 - x0).max(1.0), (y1 - y0).max(1.0))
    });
    if rotation % 180 == 90 {
        std::mem::swap(&mut width, &mut height);
    }
    Ok(Some((rotation, width, height)))
}

/// Sets the `/Rotate` of a page, a multiple of 90 that the caller
/// normalized, in one transaction. Returns `false` for a stale page.
#[pyfunction]
pub fn set_page_rotation(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    rotation: i64,
) -> PyResult<bool> {
    let (_, index, _) = index(document)?;
    let reference = Reference::new(number, generation);
    let Some(at) = index.index_of(reference) else {
        return Ok(false);
    };
    let mut dict = index.pages()[at].dict().clone();
    dict.insert(Name::from("Rotate"), Value::Integer(rotation));
    document.edit(py, |transaction, budget| {
        transaction
            .write(number, &Value::Dict(dict), budget)
            .map_err(|e| store_error(&e))
    })?;
    Ok(true)
}

/// Lists `pages`, objects of current pages, as the document's pages in
/// order, in one transaction. The pages that leave run the rules of the
/// references to them (DESIGN.md section 8): the navigation that leads to
/// them goes, and they and the objects that only they referred to are
/// freed. A refusal leaves the document as it was.
#[pyfunction]
pub fn set_pages(py: Python<'_>, document: &_Document, pages: Vec<(u32, u16)>) -> PyResult<()> {
    let (_, index, _) = index(document)?;
    let order = pages
        .into_iter()
        .map(|(number, generation)| Reference::new(number, generation))
        .collect::<Vec<_>>();
    let mut retained: Vec<u32> = order.iter().map(|page| page.number()).collect();
    retained.sort_unstable();
    retained.dedup();
    let mut removed = current_order(&index)?
        .iter()
        .map(|page| page.number())
        .filter(|number| retained.binary_search(number).is_err())
        .collect::<Vec<_>>();
    removed.sort_unstable();
    removed.dedup();
    document.edit(py, |transaction, budget| {
        let savepoint = transaction.savepoint();
        let result = remove_pages(
            document,
            transaction,
            &index,
            &order,
            &removed,
            &retained,
            budget,
        );
        if result.is_err() {
            transaction.rollback(savepoint);
        }
        result
    })
}

/// Runs the rules for `removed`, writes the page tree with `order` as its
/// pages, and frees what the removed pages leave unreferenced.
fn remove_pages(
    document: &_Document,
    transaction: &mut Transaction,
    index: &PageIndex,
    order: &[Reference],
    removed: &[u32],
    retained: &[u32],
    budget: &Budget,
) -> PyResult<()> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    if removed.is_empty() {
        let view = transaction.snapshot();
        return rewrite_with(transaction, &view, index, order, None, budget);
    }
    let parent = Arc::clone(transaction.parent());
    let before = transaction.snapshot();
    let references = document.references.get(&parent, budget).map_err(store)?;
    let references = match parent.changes(&before, budget).map_err(store)? {
        Some(changes) if !changes.is_empty() => Arc::new(
            references
                .update(&mut Reads::new(&before, budget), &changes)
                .map_err(store)?,
        ),
        _ => references,
    };
    let mut edits = Edits::new(budget);
    refs::remove_pages(
        &mut Reads::new(&before, budget),
        &references,
        removed,
        retained,
        &mut edits,
    )
    .map_err(store)?;
    edits.write(transaction, budget).map_err(store)?;
    let view = transaction.snapshot();
    rewrite_with(transaction, &view, index, order, None, budget)?;
    let after = transaction.snapshot();
    let references = match before.changes(&after, budget).map_err(store)? {
        Some(changes) => references
            .update(&mut Reads::new(&after, budget), &changes)
            .map_err(store)?,
        None => return Ok(()),
    };
    let freed = refs::unreferenced(&references, removed, edits.orphans(), budget).map_err(store)?;
    for &number in freed.iter() {
        transaction.free(number, budget).map_err(store)?;
    }
    Ok(())
}

/// Adds a blank page of `width` by `height` points at `at`, counted from
/// 0 and at most the page count, in one transaction, and returns its
/// object.
#[pyfunction]
pub fn new_page(
    py: Python<'_>,
    document: &_Document,
    at: usize,
    width: f64,
    height: f64,
) -> PyResult<(u32, u16)> {
    let (version, index, budget) = index(document)?;
    let (root, root_dict) = root(&version, &budget)?;
    let mut page = Dict::new();
    page.insert(Name::from("Type"), Value::Name(Name::from("Page")));
    page.insert(Name::from("Parent"), Value::Reference(root));
    let media = Value::Array(Array::from(vec![
        Value::Integer(0),
        Value::Integer(0),
        number(width),
        number(height),
    ]));
    page.insert(Name::from("MediaBox"), media.clone());
    // What the page would otherwise inherit from the root.
    if root_dict.get("CropBox").is_some() {
        page.insert(Name::from("CropBox"), media);
    }
    if root_dict.get("Rotate").is_some() {
        page.insert(Name::from("Rotate"), Value::Integer(0));
    }
    page.insert(Name::from("Resources"), Value::Dict(Dict::new()));
    let mut order = current_order(&index)?;
    document.edit(py, |transaction, budget| {
        let store = |e: pyropdf_store::Error| store_error(&e);
        let reference = transaction.allocate().map_err(store)?;
        transaction
            .write(reference.number(), &Value::Dict(page), budget)
            .map_err(store)?;
        order.insert(at.min(order.len()), reference);
        let view = transaction.snapshot();
        rewrite_with(transaction, &view, &index, &order, Some(reference), budget)?;
        Ok((reference.number(), reference.generation()))
    })
}

fn current_order(index: &PageIndex) -> PyResult<Vec<Reference>> {
    index
        .pages()
        .iter()
        .enumerate()
        .map(|(at, page)| indirect(page, at))
        .collect()
}

/// Writes the root node with `order` as its kids, and each page that the
/// flattening changes. `added` is a page that the transaction wrote, which
/// the index does not list yet.
fn rewrite_with(
    transaction: &mut Transaction,
    version: &Version,
    index: &PageIndex,
    order: &[Reference],
    added: Option<Reference>,
    budget: &Budget,
) -> PyResult<()> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let (root, mut root_dict) = root(version, budget)?;
    let inherited_root: Vec<Option<Value>> = INHERITABLE
        .iter()
        .map(|key| resolve(version, root_dict.get(key), budget))
        .collect::<PyResult<_>>()?;
    for &reference in order {
        if Some(reference) == added {
            continue;
        }
        let at = index.index_of(reference).ok_or_else(|| {
            PyValueError::new_err(format!(
                "object {} {} is not a page of the document",
                reference.number(),
                reference.generation()
            ))
        })?;
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
            _ => page.dict().clone(),
        };
        dict.insert(Name::from("Parent"), Value::Reference(root));
        for key in INHERITABLE {
            match page.inherited(key) {
                Some(value) => {
                    dict.insert(Name::from(key), value.clone());
                }
                None => {
                    dict.remove(key);
                }
            }
        }
        transaction
            .write(reference.number(), &Value::Dict(dict), budget)
            .map_err(store)?;
    }
    let kids = order
        .iter()
        .map(|&r| Value::Reference(r))
        .collect::<Vec<_>>();
    root_dict.insert(Name::from("Kids"), Value::Array(Array::from(kids)));
    root_dict.insert(Name::from("Count"), Value::Integer(order.len() as i64));
    transaction
        .write(root.number(), &Value::Dict(root_dict), budget)
        .map_err(store)?;
    Ok(())
}

/// Returns the root node of the page tree, which must be indirect.
fn root(version: &Version, budget: &Budget) -> PyResult<(Reference, Dict)> {
    let catalog = resolve(version, version.trailer().get("Root"), budget)?;
    let Some(Value::Dict(catalog)) = catalog else {
        return Err(MalformedPdfError::new_err(
            "the trailer names no catalog dictionary",
        ));
    };
    let Some(&Value::Reference(root)) = catalog.get("Pages") else {
        return Err(MalformedPdfError::new_err(
            "the catalog's /Pages is not an indirect page tree",
        ));
    };
    match resolve(version, Some(&Value::Reference(root)), budget)? {
        Some(Value::Dict(dict)) => Ok((root, dict)),
        _ => Err(MalformedPdfError::new_err("the catalog names no page tree")),
    }
}

/// Follows references from `value`, at most [`MAX_CHAIN`]; a null, an
/// absent object, or a missing entry is `None`.
pub(crate) fn resolve(
    version: &Version,
    value: Option<&Value>,
    budget: &Budget,
) -> PyResult<Option<Value>> {
    let mut current = match value {
        None | Some(Value::Null) => return Ok(None),
        Some(Value::Reference(reference)) => *reference,
        Some(value) => return Ok(Some(value.clone())),
    };
    for _ in 0..MAX_CHAIN {
        let Some(object) = version
            .get(current.number(), budget)
            .map_err(|e| store_error(&e))?
            .filter(|object| object.generation() == current.generation())
        else {
            return Ok(None);
        };
        match &**object.value() {
            Value::Null => return Ok(None),
            Value::Reference(next) => current = *next,
            value => return Ok(Some(value.clone())),
        }
    }
    Err(MalformedPdfError::new_err(
        "a reference chain is longer than 16 references",
    ))
}

fn indirect(page: &Page, at: usize) -> PyResult<Reference> {
    page.object()
        .ok_or_else(|| MalformedPdfError::new_err(format!("page {at} is not an indirect object")))
}

/// Returns the crop box of `page` clipped to its media box, in unrotated
/// user space, or `None` when nothing is visible.
fn visible(page: &Page) -> Option<(f64, f64, f64, f64)> {
    let media = page
        .media_box()
        .and_then(rect)
        .unwrap_or((0.0, 0.0, A4.0, A4.1));
    let crop = page.crop_box().and_then(rect).unwrap_or(media);
    let (x0, y0) = (crop.0.max(media.0), crop.1.max(media.1));
    let (x1, y1) = (crop.2.min(media.2), crop.3.min(media.3));
    (x1 - x0 >= 1e-6 && y1 - y0 >= 1e-6).then_some((x0, y0, x1, y1))
}

/// Returns the display box of `page` in unrotated user space, whose size
/// [`page_geometry`] reports: the visible crop box, at least one point on a
/// side, or hayro's A4.
pub(crate) fn display_box(page: &Page) -> [f64; 4] {
    visible(page).map_or([0.0, 0.0, A4.0, A4.1], |(x0, y0, x1, y1)| {
        [x0, y0, x0 + (x1 - x0).max(1.0), y0 + (y1 - y0).max(1.0)]
    })
}

/// Normalizes `/Rotate` to 0, 90, 180, or 270, as hayro reads it.
pub(crate) fn rotation(value: Option<&Value>) -> i64 {
    match value {
        Some(&Value::Integer(rotate)) => match rotate.rem_euclid(360) {
            r @ (0 | 90 | 180 | 270) => r,
            _ => 0,
        },
        _ => 0,
    }
}

/// Reads a rectangle of four numbers, normalized.
pub(crate) fn rect(value: &Value) -> Option<(f64, f64, f64, f64)> {
    let Value::Array(array) = value else {
        return None;
    };
    let mut numbers = [0.0; 4];
    if array.len() != 4 {
        return None;
    }
    for (slot, item) in numbers.iter_mut().zip(array.iter()) {
        *slot = match item {
            Value::Integer(n) => *n as f64,
            Value::Real(r) => r.value(),
            _ => return None,
        };
    }
    let [a, b, c, d] = numbers;
    Some((a.min(c), b.min(d), a.max(c), b.max(d)))
}

/// Returns `value` as an integer when it is one, else as a real.
pub(crate) fn number(value: f64) -> Value {
    if value.fract() == 0.0 && value.abs() < 1e15 {
        Value::Integer(value as i64)
    } else {
        Value::from(value)
    }
}
