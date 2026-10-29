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
use pyropdf_engine::pages::copy::Copies;
use pyropdf_engine::pages::import;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::{MAX_CHAIN, Page, PageIndex};
use pyropdf_store::refs::{self, Edits, Rule};
use pyropdf_store::value::{Array, Dict, Name, Reference, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::annotations::links::{
    Destination, Lead, LeadReader, NamedDestinations, lead, named, read,
};
use crate::annotations::{CopyPart, charge_copy, to_display, to_pdf};
use crate::document::_Document;
use crate::errors::{MalformedPdfError, store_error};
use crate::forms;

/// The attributes that pages inherit (ISO 32000-2 table 31).
const INHERITABLE: [&str; 4] = ["Resources", "MediaBox", "CropBox", "Rotate"];

/// hayro's default page, A4 in points, for a page without a usable box.
const A4: (f64, f64) = (595.0, 842.0);

pub(crate) fn index(document: &_Document) -> PyResult<(Arc<Version>, Arc<PageIndex>, Budget)> {
    let (version, parent) = document.reading_parent()?;
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

/// The page boxes, in the order that [`page_boxes`] returns them.
const BOXES: [&str; 5] = ["MediaBox", "CropBox", "BleedBox", "TrimBox", "ArtBox"];

type Box4 = (f64, f64, f64, f64);

/// Returns the page boxes of `page` in unrotated user space: the inherited
/// media box, or hayro's A4, then the crop, bleed, trim, and art boxes,
/// each clipped to the media box and defaulting to the crop box
/// (ISO 32000-2 14.11.2).
fn user_boxes(page: &Page) -> [Box4; 5] {
    let media = page
        .media_box()
        .and_then(rect)
        .unwrap_or((0.0, 0.0, A4.0, A4.1));
    let clip = |b: Box4| {
        (
            b.0.max(media.0),
            b.1.max(media.1),
            b.2.min(media.2),
            b.3.min(media.3),
        )
    };
    let crop = page.crop_box().and_then(rect).map_or(media, clip);
    let own = |key: &str| page.dict().get(key).and_then(rect).map_or(crop, clip);
    [media, crop, own(BOXES[2]), own(BOXES[3]), own(BOXES[4])]
}

/// Converts a user-space rectangle of a page with display box `frame` and
/// normalized `rotation` to display space.
fn display_rect(frame: [f64; 4], rotation: i64, (x0, y0, x1, y1): Box4) -> Box4 {
    let (a, b) = to_display(frame, rotation, x0, y0);
    let (c, d) = to_display(frame, rotation, x1, y1);
    (a.min(c), b.min(d), a.max(c), b.max(d))
}

/// Returns the media, crop, bleed, trim, and art boxes of a page in display
/// space, as [`user_boxes`] resolves them, or `None` for a stale page.
#[pyfunction]
pub fn page_boxes(
    document: &_Document,
    number: u32,
    generation: u16,
) -> PyResult<Option<Vec<Box4>>> {
    let (_, index, _) = index(document)?;
    let Some(at) = index.index_of(Reference::new(number, generation)) else {
        return Ok(None);
    };
    let page = &index.pages()[at];
    let (frame, rotation) = (display_box(page), rotation(page.rotate()));
    Ok(Some(
        user_boxes(page)
            .into_iter()
            .map(|b| display_rect(frame, rotation, b))
            .collect(),
    ))
}

/// Sets box `kind`, an index of [`BOXES`], of a page to `display`, a
/// rectangle in the page's current display space, in one transaction.
/// A box other than the media box must lie inside the media box, and a new
/// media box must hold the boxes that the page sets; otherwise nothing is
/// written and `ValueError` is raised. Returns `false` for a stale page.
#[pyfunction]
pub fn set_page_box(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    kind: usize,
    display: Box4,
) -> PyResult<bool> {
    const SLACK: f64 = 1e-6;
    let Some(&key) = BOXES.get(kind) else {
        return Err(PyValueError::new_err(format!("no page box {kind}")));
    };
    let (_, index, _) = index(document)?;
    let Some(at) = index.index_of(Reference::new(number, generation)) else {
        return Ok(false);
    };
    let page = &index.pages()[at];
    let (frame, rotation) = (display_box(page), rotation(page.rotate()));
    let (a, b) = to_pdf(frame, rotation, display.0, display.1);
    let (c, d) = to_pdf(frame, rotation, display.2, display.3);
    let user = (a.min(c), b.min(d), a.max(c), b.max(d));
    if user.2 - user.0 < SLACK || user.3 - user.1 < SLACK {
        return Err(PyValueError::new_err(format!("the {key} is empty")));
    }
    let inside = |inner: Box4, outer: Box4| {
        inner.0 >= outer.0 - SLACK
            && inner.1 >= outer.1 - SLACK
            && inner.2 <= outer.2 + SLACK
            && inner.3 <= outer.3 + SLACK
    };
    let boxes = user_boxes(page);
    if kind == 0 {
        let set = [page.crop_box().and_then(rect)].into_iter().chain(
            BOXES[2..]
                .iter()
                .map(|key| page.dict().get(*key).and_then(rect)),
        );
        if set.flatten().any(|inner| !inside(inner, user)) {
            return Err(PyValueError::new_err(
                "the new MediaBox does not hold the page's other boxes; set them first",
            ));
        }
    } else if !inside(user, boxes[0]) {
        return Err(PyValueError::new_err(format!(
            "the {key} leaves the MediaBox"
        )));
    }
    let mut dict = page.dict().clone();
    let array = [user.0, user.1, user.2, user.3]
        .into_iter()
        .map(self::number)
        .collect::<Vec<_>>();
    dict.insert(Name::from(key), Value::Array(Array::from(array)));
    document.edit(py, |transaction, budget| {
        transaction
            .write(number, &Value::Dict(dict), budget)
            .map_err(|e| store_error(&e))
    })?;
    Ok(true)
}

/// Lists `pages`, objects of current pages, as the document's pages in
/// order, in one transaction. An entry that `copies` marks becomes a copy
/// of the page that it names, with annotations and structure of its own;
/// every other entry names a page that stays, at most once. The pages that
/// leave run the rules of the references to them (DESIGN.md section 8):
/// the navigation that leads to them goes, and they and the objects that
/// only they referred to are freed. A refusal leaves the document as it
/// was.
#[pyfunction]
#[pyo3(signature = (document, pages, copies=Vec::new()))]
pub fn set_pages(
    py: Python<'_>,
    document: &_Document,
    pages: Vec<(u32, u16)>,
    copies: Vec<bool>,
) -> PyResult<()> {
    if !copies.is_empty() && copies.len() != pages.len() {
        return Err(PyValueError::new_err("copies must mark every page or none"));
    }
    document.edit(py, |transaction, budget| {
        let store = |e: pyropdf_store::Error| store_error(&e);
        let limit = |e: pyropdf_store::LimitExceeded| store_error(&e.into());
        let view = transaction.snapshot();
        let index = PageIndex::memoized(&document.draft_pages, &view, budget).map_err(store)?;
        let mut order = BudgetVec::new(budget);
        let mut retained = BudgetVec::new(budget);
        let mut sources = BudgetVec::new(budget);
        for (at, (number, generation)) in pages.into_iter().enumerate() {
            let page = Reference::new(number, generation);
            if index.index_of(page).is_none() {
                return Err(not_a_page(page));
            }
            order.try_push(page).map_err(limit)?;
            sources.try_push(number).map_err(limit)?;
            if !copies.get(at).copied().unwrap_or(false) {
                retained.try_push(number).map_err(limit)?;
            }
        }
        retained.sort_unstable();
        if retained.windows(2).any(|pair| pair[0] == pair[1]) {
            return Err(PyValueError::new_err(
                "a page stays at most once; copies repeat it",
            ));
        }
        let mut before = BudgetVec::new(budget);
        let mut removed = BudgetVec::new(budget);
        for (at, page) in index.pages().iter().enumerate() {
            let number = indirect(page, at)?.number();
            before.try_push(number).map_err(limit)?;
            if retained.binary_search(&number).is_err() {
                removed.try_push(number).map_err(limit)?;
            }
        }
        let mut removed = removed.into_vec();
        removed.sort_unstable();
        removed.dedup();
        let savepoint = transaction.savepoint();
        let result =
            copy_pages(transaction, &index, &mut order, &copies, budget).and_then(|added| {
                for page in &added {
                    retained.try_push(page.number()).map_err(limit)?;
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
                remove_pages(document, transaction, &index, &pages, budget)
            });
        if result.is_err() {
            transaction.rollback(savepoint);
        }
        result
    })
}

/// Inserts copies of the pages of `source` at `at`, with what they reach,
/// in one transaction. Links between the imported pages keep leading to
/// them; other links within `source` go.
#[pyfunction]
pub fn import_pages(
    py: Python<'_>,
    document: &_Document,
    source: &_Document,
    pages: Vec<(u32, u16)>,
    at: usize,
) -> PyResult<()> {
    document.edit(py, |transaction, budget| {
        let store = |e: pyropdf_store::Error| store_error(&e);
        let limit = |e: pyropdf_store::LimitExceeded| store_error(&e.into());
        let view = transaction.snapshot();
        let index = PageIndex::memoized(&document.draft_pages, &view, budget).map_err(store)?;
        let (root, root_dict) = root(&view, budget)?;
        let (version, source_parent) = source.reading_parent()?;
        let source_index = match &source_parent {
            Some(parent) => PageIndex::memoized(&source.pages, parent, budget),
            None if source.in_transaction() => {
                PageIndex::memoized(&source.draft_pages, &version, budget)
            }
            None => PageIndex::memoized(&source.pages, &version, budget),
        }
        .map_err(store)?;
        let mut selected = BudgetVec::new(budget);
        let mut imported = BudgetVec::new(budget);
        for (number, generation) in pages {
            budget.poll().map_err(limit)?;
            let page = Reference::new(number, generation);
            if source_index.index_of(page).is_none() {
                return Err(not_a_page(page));
            }
            selected.try_push(page).map_err(limit)?;
            imported.try_push(number).map_err(limit)?;
        }
        imported.sort_unstable();
        if imported.windows(2).any(|pair| pair[0] == pair[1]) {
            return Err(PyValueError::new_err(
                "a page of another document can be inserted once per call",
            ));
        }
        let mut links = Links {
            source,
            version: &version,
            budget,
            index: &source_index,
            imported: &imported,
            reads: Reads::new(&version, budget),
            named: None,
            overrides: BudgetVec::new(budget),
        };
        let mut prepared = BudgetVec::new(budget);
        prepared.try_reserve_exact(selected.len()).map_err(limit)?;
        for &page in &selected {
            budget.poll().map_err(limit)?;
            let entry = source_index
                .index_of(page)
                .and_then(|at| source_index.get(at))
                .ok_or_else(|| not_a_page(page))?;
            charge_copy(CopyPart::Dict(entry.dict()), budget)?;
            let mut dict = entry.dict().clone();
            for key in INHERITABLE {
                match entry.inherited(key) {
                    Some(value) => {
                        dict.insert(
                            Name::from(key),
                            refs::copy_for_edit(value, budget).map_err(store)?,
                        );
                    }
                    None => {
                        dict.remove(key);
                    }
                }
            }
            for key in INHERITABLE {
                if dict.get(key).is_some() || root_dict.get(key).is_none() {
                    continue;
                }
                let default = match key {
                    "CropBox" => dict
                        .get("MediaBox")
                        .map(|value| refs::copy_for_edit(value, budget))
                        .transpose()
                        .map_err(store)?
                        .unwrap_or_else(a4),
                    "MediaBox" => a4(),
                    "Rotate" => Value::Integer(0),
                    _ => Value::Dict(Dict::new()),
                };
                dict.insert(Name::from(key), default);
            }
            match links.annots(&dict)? {
                Some(annots) if annots.is_empty() => {
                    dict.remove("Annots");
                }
                Some(annots) => {
                    dict.insert(Name::from("Annots"), Value::Array(annots));
                }
                None => {}
            }
            prepared.try_push((page, dict)).map_err(limit)?;
        }
        let mut overrides = links.overrides;
        let (fields, calculation) =
            refs::form_fields(&mut Reads::new(&version, budget)).map_err(store)?;
        let (pruned, kept) = pruned(
            source,
            &version,
            &source_index,
            &imported,
            &overrides,
            !fields.is_empty(),
            budget,
        )?;
        // A complete import registers every top-level field, and a partial
        // one those that keep a widget of its pages.
        let fields = match kept {
            Some(kept) => {
                let mut out = BudgetVec::new(budget);
                for field in fields {
                    if kept.binary_search(&field.number()).is_ok() {
                        out.try_push(field).map_err(limit)?;
                    }
                }
                out.into_vec()
            }
            None => fields,
        };
        for pair in pruned.into_vec() {
            overrides.try_push(pair).map_err(limit)?;
        }
        let mut overrides = overrides.into_vec();
        budget
            .charge(
                LimitCode::MemorySize,
                overrides
                    .len()
                    .saturating_mul(size_of::<(Reference, Option<Value>)>()) as u64,
            )
            .map_err(limit)?;
        overrides.sort_by_key(|(reference, _)| *reference);
        overrides.dedup_by_key(|(reference, _)| *reference);
        let mut before = BudgetVec::new(budget);
        for (at, page) in index.pages().iter().enumerate() {
            before.try_push(indirect(page, at)?).map_err(limit)?;
        }
        let savepoint = transaction.savepoint();
        let result = (|| {
            let defaults = if fields.is_empty() {
                forms::ImportDefaults::default()
            } else {
                forms::plan_imported_defaults(&transaction.snapshot(), &version, budget)?
            };
            let along = import::Along {
                objects: &fields,
                found: &calculation,
                values: &defaults.values,
                copies: defaults.renames(),
            };
            let imported = import::import(
                transaction,
                &version,
                prepared.into_vec(),
                &overrides,
                along,
                root,
                budget,
            )
            .map_err(store)?;
            let added = imported.pages;
            let at = at.min(before.len());
            let mut order = BudgetVec::new(budget);
            order
                .try_reserve_exact(before.len().saturating_add(added.len()))
                .map_err(limit)?;
            for &page in before[..at]
                .iter()
                .chain(added.iter())
                .chain(before[at..].iter())
            {
                order.try_push(page).map_err(limit)?;
            }
            let view = transaction.snapshot();
            rewrite_with(transaction, &view, &index, &order, &added, budget)?;
            let mut fields = BudgetVec::new(budget);
            for field in &imported.objects {
                if let &Value::Reference(field) = field {
                    fields.try_push(field).map_err(limit)?;
                }
            }
            let mut calculation = BudgetVec::new(budget);
            for &field in imported.found.iter().flatten() {
                calculation.try_push(field).map_err(limit)?;
            }
            forms::register_imported(
                transaction,
                &fields,
                &calculation,
                &defaults,
                &imported.values,
                &imported.copies,
                budget,
            )
        })();
        if result.is_err() {
            transaction.rollback(savepoint);
        }
        result
    })
}

/// Source objects that an import replaces: `Some` with the value to copy
/// instead, `None` with null.
type Overrides<'b> = BudgetVec<'b, (Reference, Option<Value>)>;

/// Returns the overrides that leave the structure content of the source
/// pages other than `imported` out of a tagged `source`, and the widgets
/// and fields of those pages out of a source with a form (`form`), as
/// deleting those pages would, with the links that `links` removes. Empty
/// when neither applies or when every page comes along.
///
/// With a form and pages left behind, also returns the top-level fields
/// that keep a widget of the imported pages, sorted by number.
fn pruned<'b>(
    source: &_Document,
    version: &Arc<Version>,
    index: &PageIndex,
    imported: &[u32],
    links: &[(Reference, Option<Value>)],
    form: bool,
    budget: &'b Budget,
) -> PyResult<(Overrides<'b>, Option<Vec<u32>>)> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let limit = |e: pyropdf_store::LimitExceeded| store_error(&e.into());
    let catalog = resolve(version, version.trailer().get("Root"), budget)?;
    let tagged =
        matches!(&catalog, Some(Value::Dict(catalog)) if catalog.get("StructTreeRoot").is_some());
    let mut removed = BudgetVec::new(budget);
    let mut out = BudgetVec::new(budget);
    if !(tagged || form) {
        return Ok((out, None));
    }
    for page in index.pages().iter().filter_map(Page::object) {
        if imported.binary_search(&page.number()).is_err() {
            removed.try_push(page.number()).map_err(limit)?;
        }
    }
    if removed.is_empty() {
        return Ok((out, None));
    }
    let mut removed = removed.into_vec();
    removed.sort_unstable();
    removed.dedup();
    let references = source.references.get(version, budget).map_err(store)?;
    let mut reads = Reads::new(version, budget);
    let mut edits = Edits::new(budget);
    for (reference, value) in links {
        if value.is_none() {
            edits
                .apply(
                    &mut reads,
                    &references,
                    reference.number(),
                    &[],
                    Rule::RemoveReferrer,
                )
                .map_err(store)?;
        }
    }
    if tagged {
        refs::prune_structure(&mut reads, &references, &removed, imported, &mut edits)
            .map_err(store)?;
    }
    let kept = if form {
        let mut kept = refs::prune_forms(&mut reads, &references, &removed, imported, &mut edits)
            .map_err(store)?;
        kept.sort_unstable();
        Some(kept)
    } else {
        None
    };
    // Computing the values removes the tree nodes that they empty.
    let values = edits.values(version, budget).map_err(store)?;
    for &number in edits.orphans() {
        if let Some(generation) = version.generation(number) {
            out.try_push((Reference::new(number, generation), None))
                .map_err(limit)?;
        }
    }
    for (number, value) in values.into_vec() {
        if let Some(generation) = version.generation(number) {
            out.try_push((Reference::new(number, generation), Some(value)))
                .map_err(limit)?;
        }
    }
    Ok((out, kept))
}

/// The media box that a page without one gets: A4, as the renderer draws it.
fn a4() -> Value {
    Value::Array(Array::from(vec![
        Value::Integer(0),
        Value::Integer(0),
        Value::Integer(595),
        Value::Integer(842),
    ]))
}

/// Prepares the links of pages that an import brings from `source`.
struct Links<'a, 'b> {
    source: &'a _Document,
    version: &'a Arc<Version>,
    budget: &'b Budget,
    index: &'a PageIndex,
    /// The numbers of the imported pages, sorted.
    imported: &'a [u32],
    reads: Reads<'b>,
    named: Option<Arc<NamedDestinations>>,
    /// The link objects that lead elsewhere once imported, or go.
    overrides: BudgetVec<'b, (Reference, Option<Value>)>,
}

impl Links<'_, '_> {
    /// Returns the annotations of `page` without the links within `source`
    /// that lead to a page left behind, and with explicit destinations for
    /// the others; `None` when the page has no annotation array.
    fn annots(&mut self, page: &Dict) -> PyResult<Option<Array>> {
        let store = |e: pyropdf_store::Error| store_error(&e);
        let Some(Value::Array(items)) = self.resolved(page.get("Annots"))? else {
            return Ok(None);
        };
        self.reads
            .budget()
            .check(LimitCode::AnnotationCount, items.len() as u64)
            .map_err(|e| store(e.into()))?;
        let budget = self.budget;
        let mut kept = BudgetVec::new(budget);
        kept.try_reserve_exact(items.len())
            .map_err(|e| store(e.into()))?;
        for item in items.iter() {
            let Some(Value::Dict(annotation)) = self.resolved(Some(item))? else {
                kept.try_push(refs::copy_for_edit(item, budget).map_err(store)?)
                    .map_err(|e| store(e.into()))?;
                continue;
            };
            let link = matches!(
                self.resolved(annotation.get("Subtype"))?,
                Some(Value::Name(subtype)) if subtype.as_bytes() == b"Link"
            );
            if !link {
                kept.try_push(refs::copy_for_edit(item, budget).map_err(store)?)
                    .map_err(|e| store(e.into()))?;
                continue;
            }
            let destination = match lead(self, &annotation)? {
                Lead::Target(_) => {
                    kept.try_push(refs::copy_for_edit(item, budget).map_err(store)?)
                        .map_err(|e| store(e.into()))?;
                    continue;
                }
                Lead::Destination(destination) => destination,
                Lead::Name(name) => self.lookup(&name)?,
            };
            let prepared = destination
                .page_in(self.index)
                .filter(|page| self.imported.binary_search(&page.number()).is_ok())
                .map(|page| self.retarget(&annotation, &destination, page))
                .transpose()?;
            match item {
                Value::Reference(reference) => {
                    if prepared.is_some() {
                        kept.try_push(refs::copy_for_edit(item, budget).map_err(store)?)
                            .map_err(|e| store(e.into()))?;
                    }
                    self.overrides
                        .try_push((*reference, prepared.map(Value::Dict)))
                        .map_err(|e| store(e.into()))?;
                }
                _ => {
                    if let Some(prepared) = prepared {
                        kept.try_push(Value::Dict(prepared))
                            .map_err(|e| store(e.into()))?;
                    }
                }
            }
        }
        Ok(Some(Array::from(kept.into_vec())))
    }

    /// Returns `link` leading to `destination` on `page` with an explicit
    /// array, in its `/GoTo` action if it has one.
    fn retarget(
        &mut self,
        link: &Dict,
        destination: &Destination,
        page: Reference,
    ) -> PyResult<Dict> {
        let array = Value::Array(destination.array(page));
        charge_copy(CopyPart::Dict(link), self.budget)?;
        let mut link = link.clone();
        if let Some(Value::Dict(mut action)) = self.resolved(link.get("A"))? {
            action.insert(Name::from("D"), array);
            link.insert(Name::from("A"), Value::Dict(action));
            link.remove("Dest");
        } else {
            link.insert(Name::from("Dest"), array);
            link.remove("A");
        }
        Ok(link)
    }

    fn lookup(&mut self, name: &[u8]) -> PyResult<Destination> {
        if self.named.is_none() {
            self.named = Some(named(self.source, self.version, self.reads.budget())?);
        }
        Ok(self
            .named
            .as_ref()
            .and_then(|named| named.get(name))
            .unwrap_or(Destination::UNRESOLVED))
    }
}

impl LeadReader for Links<'_, '_> {
    fn budget(&self) -> &Budget {
        self.reads.budget()
    }

    fn resolved(&mut self, value: Option<&Value>) -> PyResult<Option<Value>> {
        read(&mut self.reads, value)
    }

    /// The import reads no text of a link.
    fn text(&mut self, _: &[u8], _: &str, _: bool) -> PyResult<String> {
        Ok(String::new())
    }
}

fn not_a_page(page: Reference) -> PyErr {
    PyValueError::new_err(format!(
        "object {} {} is not a page of the document",
        page.number(),
        page.generation()
    ))
}

/// Writes a copy of each page of `order` that `copies` marks and puts it
/// in its place, and returns the copies.
fn copy_pages(
    transaction: &mut Transaction,
    index: &PageIndex,
    order: &mut [Reference],
    copies: &[bool],
    budget: &Budget,
) -> PyResult<Vec<Reference>> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let mut added = BudgetVec::new(budget);
    if !copies.contains(&true) {
        return Ok(added.into_vec());
    }
    let (root, _) = root(transaction.version(), budget)?;
    let mut copier = Copies::new(transaction, budget).map_err(store)?;
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
                return Err(MalformedPdfError::new_err(
                    "the page to copy is no longer a dictionary",
                ));
            }
        };
        dict.insert(Name::from("Parent"), Value::Reference(root));
        for key in INHERITABLE {
            match page.inherited(key) {
                Some(value) => {
                    dict.insert(
                        Name::from(key),
                        refs::copy_for_edit(value, budget).map_err(store)?,
                    );
                }
                None => {
                    dict.remove(key);
                }
            }
        }
        let copy = copier.copy(transaction, source, dict).map_err(store)?;
        added.try_push(copy).map_err(|e| store(e.into()))?;
        *entry = copy;
    }
    copier.finish(transaction).map_err(store)?;
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
    document: &_Document,
    transaction: &mut Transaction,
    index: &PageIndex,
    pages: &Pages<'_>,
    budget: &Budget,
) -> PyResult<()> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let Pages {
        before: old,
        order,
        sources,
        removed,
        retained,
        added,
    } = *pages;
    let before = transaction.snapshot();
    let limit = |e: pyropdf_store::LimitExceeded| store_error(&e.into());
    let labels = refs::relabel(&mut Reads::new(&before, budget), old, sources).map_err(store)?;
    if removed.is_empty()
        && labels
            .as_ref()
            .is_none_or(|labels| labels.nodes().is_empty())
    {
        if let Some(labels) = &labels {
            labels.write(transaction, budget).map_err(store)?;
        }
        let view = transaction.snapshot();
        return rewrite_with(transaction, &view, index, order, added, budget);
    }
    let parent = Arc::clone(transaction.parent());
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
    if !removed.is_empty() {
        refs::remove_pages(
            &mut Reads::new(&before, budget),
            &references,
            removed,
            retained,
            &mut edits,
        )
        .map_err(store)?;
    }
    edits.write(transaction, budget).map_err(store)?;
    if let Some(labels) = &labels {
        labels.write(transaction, budget).map_err(store)?;
    }
    let view = transaction.snapshot();
    rewrite_with(transaction, &view, index, order, added, budget)?;
    let after = transaction.snapshot();
    let references = match before.changes(&after, budget).map_err(store)? {
        Some(changes) => references
            .update(&mut Reads::new(&after, budget), &changes)
            .map_err(store)?,
        None => return Ok(()),
    };
    let mut candidates = BudgetVec::new(budget);
    candidates
        .try_extend_from_slice(edits.orphans())
        .map_err(limit)?;
    if let Some(labels) = &labels {
        candidates
            .try_extend_from_slice(labels.nodes())
            .map_err(limit)?;
    }
    let freed = refs::unreferenced(&references, removed, &candidates, budget).map_err(store)?;
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
        rewrite_with(transaction, &view, &index, &order, &[reference], budget)?;
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
) -> PyResult<Option<Reference>> {
    let mut node: Option<Arc<Value>> = None;
    for depth in 0.. {
        budget
            .check(LimitCode::ObjectDepth, depth)
            .map_err(|e| store_error(&e.into()))?;
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
        node = target(version, *parent, budget)?;
    }
    Ok(None)
}

/// Returns the value that `reference` names in `version`, through a chain
/// of references, without copying it; `None` for a missing object or null.
fn target(
    version: &Version,
    reference: Reference,
    budget: &Budget,
) -> PyResult<Option<Arc<Value>>> {
    let mut current = reference;
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
            _ => return Ok(Some(Arc::clone(object.value()))),
        }
    }
    Err(MalformedPdfError::new_err(
        "a reference chain is longer than 16 references",
    ))
}

/// Writes the root node with `order` as its kids, and each page that the
/// flattening changes. `added` are pages that the transaction wrote, which
/// the index does not list yet.
fn rewrite_with(
    transaction: &mut Transaction,
    version: &Version,
    index: &PageIndex,
    order: &[Reference],
    added: &[Reference],
    budget: &Budget,
) -> PyResult<()> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let (root, mut root_dict) = root(version, budget)?;
    let inherited_root: Vec<Option<Value>> = INHERITABLE
        .iter()
        .map(|key| resolve(version, root_dict.get(key), budget))
        .collect::<PyResult<_>>()?;
    for &reference in order {
        if added.contains(&reference) {
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
            _ => {
                return Err(MalformedPdfError::new_err(
                    "a retained page is no longer a dictionary",
                ));
            }
        };
        dict.insert(Name::from("Parent"), Value::Reference(root));
        for key in INHERITABLE {
            match page.inherited(key) {
                Some(value) => {
                    // A reference stays a reference, so that the pages keep
                    // sharing what they shared.
                    let value = match held(version, page.dict(), key, value, budget)? {
                        Some(held) => Value::Reference(held),
                        None => refs::copy_for_edit(value, budget).map_err(store)?,
                    };
                    dict.insert(Name::from(key), value);
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
    let mut kids = BudgetVec::new(budget);
    kids.try_extend(order.iter().copied().map(Value::Reference))
        .map_err(|e| store_error(&e.into()))?;
    root_dict.insert(
        Name::from("Kids"),
        Value::Array(Array::from(kids.into_vec())),
    );
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
        Some(value) => {
            return refs::copy_for_edit(value, budget)
                .map(Some)
                .map_err(|e| store_error(&e));
        }
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
            value => {
                return refs::copy_for_edit(value, budget)
                    .map(Some)
                    .map_err(|e| store_error(&e));
            }
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
