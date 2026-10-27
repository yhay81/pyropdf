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
use pyropdf_store::budget::{Budget, BudgetVec};
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::{MAX_CHAIN, Page, PageIndex};
use pyropdf_store::refs::{self, Edits, Rule};
use pyropdf_store::value::{Array, Dict, Name, Reference, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::annotations::MAX_PAGE_ANNOTATIONS;
use crate::annotations::links::{
    Destination, Lead, LeadReader, NamedDestinations, lead, named, read,
};
use crate::document::_Document;
use crate::errors::{MalformedPdfError, store_error};
use crate::forms;

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
    let (_, index, _) = index(document)?;
    let order = pages
        .into_iter()
        .map(|(number, generation)| Reference::new(number, generation))
        .collect::<Vec<_>>();
    let copied = |at: usize| copies.get(at).copied().unwrap_or(false);
    for (at, &page) in order.iter().enumerate() {
        if copied(at) && index.index_of(page).is_none() {
            return Err(not_a_page(page));
        }
    }
    let mut retained: Vec<u32> = order
        .iter()
        .enumerate()
        .filter(|&(at, _)| !copied(at))
        .map(|(_, page)| page.number())
        .collect();
    retained.sort_unstable();
    if retained.windows(2).any(|pair| pair[0] == pair[1]) {
        return Err(PyValueError::new_err(
            "a page stays at most once; copies repeat it",
        ));
    }
    let sources = order.iter().map(|page| page.number()).collect::<Vec<_>>();
    let before = current_order(&index)?
        .iter()
        .map(|page| page.number())
        .collect::<Vec<_>>();
    let mut removed = before
        .iter()
        .copied()
        .filter(|number| retained.binary_search(number).is_err())
        .collect::<Vec<_>>();
    removed.sort_unstable();
    removed.dedup();
    document.edit(py, |transaction, budget| {
        let savepoint = transaction.savepoint();
        let mut order = order.clone();
        let result =
            copy_pages(transaction, &index, &mut order, &copies, budget).and_then(|added| {
                let mut retained = retained.clone();
                retained.extend(added.iter().map(|page| page.number()));
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
    let store = |e: pyropdf_store::Error| store_error(&e);
    let (target, index, budget) = index(document)?;
    let (_, root_dict) = root(&target, &budget)?;
    let (version, source_index, _) = self::index(source)?;
    let pages = pages
        .into_iter()
        .map(|(number, generation)| Reference::new(number, generation))
        .collect::<Vec<_>>();
    let mut imported = Vec::with_capacity(pages.len());
    for &page in &pages {
        if source_index.index_of(page).is_none() {
            return Err(not_a_page(page));
        }
        imported.push(page.number());
    }
    imported.sort_unstable();
    if imported.windows(2).any(|pair| pair[0] == pair[1]) {
        return Err(PyValueError::new_err(
            "a page of another document can be inserted once per call",
        ));
    }
    let mut links = Links {
        source,
        index: &source_index,
        imported: &imported,
        reads: Reads::new(&version, &budget),
        named: None,
        overrides: BudgetVec::new(&budget),
    };
    let mut prepared = BudgetVec::new(&budget);
    prepared
        .try_reserve_exact(pages.len())
        .map_err(|e| store(e.into()))?;
    for &page in &pages {
        let entry = source_index
            .index_of(page)
            .and_then(|at| source_index.get(at))
            .ok_or_else(|| not_a_page(page))?;
        let mut dict = entry.dict().clone();
        for key in INHERITABLE {
            match entry.inherited(key) {
                Some(value) => {
                    dict.insert(Name::from(key), value.clone());
                }
                None => {
                    dict.remove(key);
                }
            }
        }
        // What the page would otherwise inherit from the receiving root.
        for key in INHERITABLE {
            if dict.get(key).is_some() || root_dict.get(key).is_none() {
                continue;
            }
            let default = match key {
                "CropBox" => dict.get("MediaBox").cloned().unwrap_or_else(a4),
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
        prepared
            .try_push((page, dict))
            .map_err(|e| store(e.into()))?;
    }
    let mut overrides = links.overrides.into_vec();
    // A link that two pages share is prepared once per page, alike.
    overrides.sort_by_key(|(number, _)| *number);
    overrides.dedup_by_key(|(number, _)| *number);
    let (fields, calculation) =
        refs::form_fields(&mut Reads::new(&version, &budget)).map_err(store)?;
    let (pruned, kept) = pruned(
        source,
        &version,
        &source_index,
        &imported,
        &overrides,
        !fields.is_empty(),
        &budget,
    )?;
    // A complete import registers every top-level field, and a partial one
    // those that keep a widget of its pages.
    let fields = match kept {
        Some(kept) => fields
            .into_iter()
            .filter(|field| kept.binary_search(&field.number()).is_ok())
            .collect(),
        None => fields,
    };
    if !pruned.is_empty() {
        // The links' own overrides come first and win.
        overrides.extend(pruned);
        overrides.sort_by_key(|(number, _)| *number);
        overrides.dedup_by_key(|(number, _)| *number);
    }
    let mut order = current_order(&index)?;
    document.edit(py, |transaction, budget| {
        let savepoint = transaction.savepoint();
        let result = (|| {
            let (root, _) = root(transaction.version(), budget)?;
            let along = import::Along {
                objects: &fields,
                found: &calculation,
            };
            let imported = import::import(
                transaction,
                &version,
                &prepared,
                &overrides,
                along,
                root,
                budget,
            )
            .map_err(store)?;
            let added = imported.pages;
            let at = at.min(order.len());
            order.splice(at..at, added.iter().copied());
            let view = transaction.snapshot();
            rewrite_with(transaction, &view, &index, &order, &added, budget)?;
            let fields = imported
                .objects
                .iter()
                .filter_map(|field| match field {
                    &Value::Reference(field) => Some(field),
                    _ => None,
                })
                .collect::<Vec<_>>();
            let calculation = imported.found.iter().flatten().copied().collect::<Vec<_>>();
            forms::register_imported(transaction, &fields, &calculation, budget)
        })();
        if result.is_err() {
            transaction.rollback(savepoint);
        }
        result
    })
}

/// Source objects that an import replaces, sorted by number: `Some` with
/// the value to copy instead, `None` with null.
type Overrides = Vec<(u32, Option<Value>)>;

/// Returns the overrides that leave the structure content of the source
/// pages other than `imported` out of a tagged `source`, and the widgets
/// and fields of those pages out of a source with a form (`form`), as
/// deleting those pages would, with the links that `links` removes. Empty
/// when neither applies or when every page comes along.
///
/// With a form and pages left behind, also returns the top-level fields
/// that keep a widget of the imported pages.
fn pruned(
    source: &_Document,
    version: &Arc<Version>,
    index: &PageIndex,
    imported: &[u32],
    links: &[(u32, Option<Value>)],
    form: bool,
    budget: &Budget,
) -> PyResult<(Overrides, Option<Vec<u32>>)> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let catalog = resolve(version, version.trailer().get("Root"), budget)?;
    let tagged =
        matches!(&catalog, Some(Value::Dict(catalog)) if catalog.get("StructTreeRoot").is_some());
    let mut removed = index
        .pages()
        .iter()
        .filter_map(Page::object)
        .map(|page| page.number())
        .filter(|number| imported.binary_search(number).is_err())
        .collect::<Vec<_>>();
    if !(tagged || form) || removed.is_empty() {
        return Ok((Vec::new(), None));
    }
    removed.sort_unstable();
    removed.dedup();
    let references = source.references.get(version, budget).map_err(store)?;
    let mut reads = Reads::new(version, budget);
    let mut edits = Edits::new(budget);
    for &(number, ref value) in links {
        if value.is_none() {
            edits
                .apply(&mut reads, &references, number, &[], Rule::RemoveReferrer)
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
    let mut out = edits
        .orphans()
        .iter()
        .map(|&number| (number, None))
        .collect::<Vec<_>>();
    for (number, value) in edits.values(version, budget).map_err(store)?.into_vec() {
        out.push((number, Some(value)));
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
    index: &'a PageIndex,
    /// The numbers of the imported pages, sorted.
    imported: &'a [u32],
    reads: Reads<'b>,
    named: Option<Arc<NamedDestinations>>,
    /// The link objects that lead elsewhere once imported, or go.
    overrides: BudgetVec<'b, (u32, Option<Value>)>,
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
        if items.len() > MAX_PAGE_ANNOTATIONS {
            return Err(MalformedPdfError::new_err(format!(
                "page annotations exceed the {MAX_PAGE_ANNOTATIONS}-entry safety limit"
            )));
        }
        let mut kept = Vec::with_capacity(items.len());
        for item in items.iter() {
            let Some(Value::Dict(annotation)) = self.resolved(Some(item))? else {
                kept.push(item.clone());
                continue;
            };
            let link = matches!(
                self.resolved(annotation.get("Subtype"))?,
                Some(Value::Name(subtype)) if subtype.as_bytes() == b"Link"
            );
            if !link {
                kept.push(item.clone());
                continue;
            }
            let destination = match lead(self, &annotation)? {
                Lead::Target(_) => {
                    kept.push(item.clone());
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
                        kept.push(item.clone());
                    }
                    self.overrides
                        .try_push((reference.number(), prepared.map(Value::Dict)))
                        .map_err(|e| store(e.into()))?;
                }
                _ => kept.extend(prepared.map(Value::Dict)),
            }
        }
        Ok(Some(Array::from(kept)))
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
            self.named = Some(named(self.source)?);
        }
        Ok(self
            .named
            .as_ref()
            .and_then(|named| named.get(name))
            .unwrap_or(Destination::UNRESOLVED))
    }
}

impl LeadReader for Links<'_, '_> {
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
    let mut added = Vec::new();
    if !copies.contains(&true) {
        return Ok(added);
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
        let copy = copier.copy(transaction, source, dict).map_err(store)?;
        added.push(copy);
        *entry = copy;
    }
    copier.finish(transaction).map_err(store)?;
    Ok(added)
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
    let mut candidates = edits.orphans().to_vec();
    if let Some(labels) = &labels {
        candidates.extend_from_slice(labels.nodes());
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
