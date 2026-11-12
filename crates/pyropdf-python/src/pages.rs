//! Python page handles, snapshots and transaction entry points.

use std::sync::{Arc, Mutex, PoisonError, Weak};

use pyo3::exceptions::{PyIndexError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyropdf_engine::display::Box4;
pub(crate) use pyropdf_engine::display::{display_box, rotation};
use pyropdf_engine::pages::{self, canvas, geometry, tree};
use pyropdf_engine::render::Fallback;
use pyropdf_store::budget::{Budget, LimitCode, SharedBudget};
use pyropdf_store::pages::{Page, PageIndex};
use pyropdf_store::value::{Reference, Value};
use pyropdf_store::version::{Document, Version};

use crate::annotations::annotation_error;
use crate::annotations::links::{destinations_memo, navigation_error};
use crate::document::_Document;
use crate::errors::{MalformedPdfError, PdfError, limit_error, store_error};
use crate::history::OperationBudget;

/// The view shared by every page and every pass of one document export.
#[pyclass(frozen, name = "_PageSnapshot", module = "pyropdf._core")]
pub struct PageSnapshot {
    document: Py<_Document>,
    version: Arc<Version>,
    index: Arc<PageIndex>,
    fallback: Fallback,
    budget: SharedBudget,
}

#[pyfunction]
pub fn snapshot_pages(document: Py<_Document>) -> PyResult<PageSnapshot> {
    crate::errors::boundary(|| {
        let (version, index, budget) = index(document.get())?;
        let fallback = document.get().fallback();
        Ok(PageSnapshot {
            document,
            version,
            index,
            fallback,
            budget,
        })
    })?
}

#[pymethods]
impl PageSnapshot {
    fn __len__(&self) -> PyResult<usize> {
        crate::errors::boundary(|| self.index.len())
    }

    fn page_object(&self, at: usize) -> PyResult<(u32, u16)> {
        crate::errors::boundary(|| {
            self.budget.poll().map_err(|error| limit_error(&error))?;
            object_at(&self.index, at)
        })?
    }
}

/// Page extraction uses the document's caches for both live and pinned reads.
#[derive(FromPyObject)]
pub enum PageSource<'py> {
    Live(PyRef<'py, _Document>),
    Pinned(PyRef<'py, PageSnapshot>),
}

impl PageSource<'_> {
    pub(crate) fn document(&self) -> &_Document {
        match self {
            Self::Live(document) => document,
            Self::Pinned(view) => view.document.get(),
        }
    }

    pub(crate) fn index(&self, budget: &Budget) -> PyResult<(Arc<Version>, Arc<PageIndex>)> {
        match self {
            Self::Live(document) => index_with(document, None, budget),
            Self::Pinned(view) => {
                view.budget.poll().map_err(|error| limit_error(&error))?;
                Ok((Arc::clone(&view.version), Arc::clone(&view.index)))
            }
        }
    }

    pub(crate) fn fallback(&self) -> Fallback {
        match self {
            Self::Live(document) => document.fallback(),
            Self::Pinned(view) => view.fallback.clone(),
        }
    }
}

pub(crate) fn index(
    document: &_Document,
) -> PyResult<(Arc<Version>, Arc<PageIndex>, SharedBudget)> {
    let budget = document.budget();
    let (version, index) = index_with(document, None, &budget)?;
    Ok((version, index, budget))
}

/// Returns the version that reads of `document` see and its page index,
/// charging `budget`; `operation`, if given, also settles the open
/// transaction's writes instead of the document's own operation.
pub(crate) fn index_with(
    document: &_Document,
    operation: Option<&Budget>,
    budget: &Budget,
) -> PyResult<(Arc<Version>, Arc<PageIndex>)> {
    let (version, parent) = document.reading_parent_with(operation)?;
    let index = match &parent {
        // An open transaction that has written nothing reads its parent.
        Some(parent) => PageIndex::memoized(&document.pages, parent, budget),
        None if document.in_transaction() => {
            PageIndex::memoized(&document.draft_pages, &version, budget)
        }
        None => PageIndex::memoized(&document.pages, &version, budget),
    }
    .map_err(|e| store_error(&e))?;
    Ok((version, index))
}

/// Resolves the page entries of a batch, such as the `pages` of
/// `doc.render`, to indexes: every page for `None`, negative entries counted
/// from the end.
#[expect(
    clippy::disallowed_methods,
    reason = "one selected index per already materialized Python index, or per page in the budgeted page index"
)]
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
    crate::errors::boundary(|| Ok(index(document)?.1.len()))?
}

/// Returns the object of page `at`, counted from 0, as a number and a
/// generation.
#[pyfunction]
pub fn page_object(document: &_Document, at: usize) -> PyResult<(u32, u16)> {
    crate::errors::boundary(|| {
        let (_, index, _) = index(document)?;
        object_at(&index, at)
    })?
}

fn object_at(index: &PageIndex, at: usize) -> PyResult<(u32, u16)> {
    let page = index
        .get(at)
        .ok_or_else(|| PyIndexError::new_err(format!("page {at} is out of range")))?;
    let object = pages::indirect(page, at).map_err(page_error)?;
    Ok((object.number(), object.generation()))
}

/// Returns the index of the page whose object is `number` and
/// `generation`, or `None` when the page tree no longer lists it.
#[pyfunction]
pub fn page_index(document: &_Document, number: u32, generation: u16) -> PyResult<Option<usize>> {
    crate::errors::boundary(|| {
        Ok(index(document)?
            .1
            .index_of(Reference::new(number, generation)))
    })?
}

/// Returns the objects of every page, in order.
#[pyfunction]
#[expect(
    clippy::disallowed_methods,
    reason = "one object identity per page in the budgeted page index"
)]
pub fn page_objects(document: &_Document) -> PyResult<Vec<(u32, u16)>> {
    crate::errors::boundary(|| {
        let (_, index, _) = index(document)?;
        index
            .pages()
            .iter()
            .enumerate()
            .map(|(at, page)| {
                pages::indirect(page, at)
                    .map(|r| (r.number(), r.generation()))
                    .map_err(page_error)
            })
            .collect()
    })?
}

fn read_page<T>(
    document: &_Document,
    reference: Reference,
    read: impl FnOnce(&Page) -> T,
) -> PyResult<Option<T>> {
    let (_, index, _) = index(document)?;
    Ok(index.index_of(reference).map(|at| read(&index.pages()[at])))
}

/// Normalized rotation and display dimensions in points, or None for a stale page.
#[pyfunction]
pub fn page_geometry(
    document: &_Document,
    number: u32,
    generation: u16,
) -> PyResult<Option<(i64, f64, f64)>> {
    crate::errors::boundary(|| {
        read_page(
            document,
            Reference::new(number, generation),
            geometry::geometry,
        )
    })?
}

/// Normalized rotation, unrotated display box and points per user-space unit.
#[pyfunction]
pub fn page_space(
    document: &_Document,
    number: u32,
    generation: u16,
) -> PyResult<Option<(i64, Box4, f64)>> {
    crate::errors::boundary(|| {
        read_page(
            document,
            Reference::new(number, generation),
            geometry::space,
        )
    })?
}

/// What a canvas reads of a source page, with the version it read.
type CanvasSourcePage<'py> = (
    i64,
    Box4,
    f64,
    f64,
    f64,
    Bound<'py, PyBytes>,
    Py<CanvasSourceSnapshot>,
);

/// One source page as a canvas operation read it: the immutable version
/// that its geometry and content came from, so that its resources come from
/// that version too, however the source changes meanwhile.
///
/// It belongs to one source document, page and operation, and holds the
/// version until [`copy_page_resources`] uses it or the canvas ends, after
/// which it refuses to be used. No cache holds it.
#[pyclass(frozen, name = "_CanvasSourceSnapshot", module = "pyropdf._core")]
pub(crate) struct CanvasSourceSnapshot {
    captured: Mutex<Option<canvas::Captured>>,
    page: Reference,
    source: Weak<Document>,
    operation: SharedBudget,
}

impl CanvasSourceSnapshot {
    /// Takes the version out of the snapshot, refusing a snapshot of another
    /// source document, page or operation, or one already used.
    fn take(
        &self,
        source: &Arc<Document>,
        page: Reference,
        operation: &OperationBudget,
    ) -> PyResult<canvas::Captured> {
        if self.page != page
            || !Weak::ptr_eq(&self.source, &Arc::downgrade(source))
            || !self.operation.ptr_eq(&operation.operation)
        {
            return Err(PyValueError::new_err(
                "the source snapshot does not belong to this page or canvas",
            ));
        }
        self.captured
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take()
            .ok_or_else(|| PyValueError::new_err("the source snapshot was already used"))
    }
}

#[pymethods]
impl CanvasSourceSnapshot {
    /// Drops the version the snapshot holds, as a canvas does when it ends
    /// without copying the page's resources.
    fn release(&self) -> PyResult<()> {
        crate::errors::boundary(|| {
            self.captured
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .take();
        })
    }

    /// Whether the snapshot still holds its version.
    #[getter]
    fn held(&self) -> PyResult<bool> {
        crate::errors::boundary(|| {
            self.captured
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .is_some()
        })
    }
}

/// Reads a page of `source` for the canvas of `operation`: its normalized
/// rotation, display box, user space unit, display width and height, as
/// [`page_space`] and [`page_geometry`] report them, and its content, all
/// from one version,
/// with a snapshot of that version for [`copy_page_resources`]. The read
/// charges the operation under the lower of its limits and the source's.
/// `None` for a stale page.
#[pyfunction]
pub fn canvas_source_page<'py>(
    py: Python<'py>,
    source: &_Document,
    number: u32,
    generation: u16,
    operation: &OperationBudget,
) -> PyResult<Option<CanvasSourcePage<'py>>> {
    crate::errors::boundary(|| {
        operation.ensure_active()?;
        let source_document = source.document()?;
        let budget = operation.for_source(source)?;
        let own = operation.is_active_on(source).then_some(&*budget);
        let (version, index) = index_with(source, own, &budget)?;
        let page = Reference::new(number, generation);
        let Some(source_page) = canvas::SourcePage::new(version, index, page) else {
            return Ok(None);
        };
        let captured = py
            .detach(|| source_page.capture(&budget))
            .map_err(page_error)?;
        // The source may have closed, been replaced by signing, or the canvas
        // ended on another call while the GIL was released.
        operation.ensure_active()?;
        if !Arc::ptr_eq(&source_document, &source.document()?) {
            return Err(PdfError::new_err(
                "the source document changed during the canvas read",
            ));
        }
        let (rotation, bbox, unit, (width, height)) = captured.geometry();
        let data = captured.content();
        let header = py
            .get_type::<PyBytes>()
            .getattr("__basicsize__")?
            .extract::<u64>()?;
        let size = u64::try_from(data.len()).unwrap_or(u64::MAX);
        budget
            .charge(
                LimitCode::MemorySize,
                size.saturating_add(header).saturating_add(1),
            )
            .map_err(|error| limit_error(&error))?;
        let bytes = PyBytes::new(py, data);
        let snapshot = Py::new(
            py,
            CanvasSourceSnapshot {
                captured: Mutex::new(Some(captured)),
                page,
                source: Arc::downgrade(&source_document),
                operation: operation.operation.clone(),
            },
        )?;
        Ok(Some((rotation, bbox, unit, width, height, bytes, snapshot)))
    })?
}

/// Writes the resources that a page of `source` uses, inherited or its
/// own, as a new dictionary object of `document`, in one transaction, and
/// returns its number. From another document the objects they reach are
/// copied too; within one document they are shared. `None` when `source`
/// no longer lists the page.
///
/// A canvas passes its `operation` and the `snapshot` that
/// [`canvas_source_page`] returned, together: the resources then come from
/// the snapshot's version, the operation is charged, and within one
/// document the objects they reach are copied too, so that a later edit of
/// the source's objects does not change them. The snapshot is then used up.
#[pyfunction]
#[pyo3(signature = (document, source, number, generation, *, operation=None, snapshot=None))]
pub fn copy_page_resources(
    py: Python<'_>,
    document: &_Document,
    source: &_Document,
    number: u32,
    generation: u16,
    operation: Option<&OperationBudget>,
    snapshot: Option<&CanvasSourceSnapshot>,
) -> PyResult<Option<u32>> {
    crate::errors::boundary(|| {
        let page = Reference::new(number, generation);
        let same = std::ptr::eq(document, source);
        let (source_page, budget) = match (operation, snapshot) {
            (None, None) => {
                let (version, index, budget) = self::index(source)?;
                let Some(source_page) = canvas::SourcePage::new(version, index, page) else {
                    return Ok(None);
                };
                (source_page, budget)
            }
            (Some(operation), Some(snapshot)) => {
                if !operation.is_active_on(document) {
                    return Err(PyValueError::new_err(
                        "the canvas operation is not active on this document",
                    ));
                }
                let captured = snapshot.take(&source.document()?, page, operation)?;
                let budget = operation.for_source(source)?;
                (captured.into_page(), budget)
            }
            _ => {
                return Err(PyValueError::new_err(
                    "a canvas operation and its source snapshot go together",
                ));
            }
        };
        let written = document.edit(py, |transaction, edit_budget| {
            // A canvas charges its operation, under the source's limits too.
            let budget: &Budget = if operation.is_some() {
                &budget
            } else {
                edit_budget
            };
            source_page
                .copy_resources(transaction, same, operation.is_some(), budget)
                .map_err(page_error)
        })?;
        Ok(Some(written.number()))
    })?
}

/// The reusable page Forms of one canvas destination.
#[pyclass(frozen, name = "_PageForms", module = "pyropdf._core")]
#[derive(Default)]
pub(crate) struct PageForms(canvas::PageForms);

#[pymethods]
impl PageForms {
    #[new]
    fn new() -> PyResult<Self> {
        crate::errors::boundary(Self::default)
    }

    /// The number of Forms the cache keeps.
    fn __len__(&self) -> PyResult<usize> {
        crate::errors::boundary(|| self.0.count())
    }
}

/// Returns a Form XObject of `document` that draws the page of `source`
/// that `snapshot` read, in its user space, with its display box as
/// `/BBox`, and uses the snapshot up.
///
/// The Form of an earlier placement is reused while the page's
/// `/Contents` and inherited `/Resources` values, its display box, every
/// source object that they reach, and the Form and its resources in
/// `document` are unchanged. Otherwise the page's resources are copied, as
/// [`copy_page_resources`] copies them for a canvas, a new Form is written
/// and `forms` remembers it. Everything charges the operation, under the
/// source's limits too. `None` when the snapshot no longer lists the page.
#[pyfunction]
#[expect(clippy::too_many_arguments, reason = "the private canvas binding")]
pub fn canvas_page_form(
    py: Python<'_>,
    forms: &PageForms,
    document: &_Document,
    source: &_Document,
    number: u32,
    generation: u16,
    operation: &OperationBudget,
    snapshot: &CanvasSourceSnapshot,
) -> PyResult<Option<u32>> {
    crate::errors::boundary(|| {
        if !operation.is_active_on(document) {
            return Err(PyValueError::new_err(
                "the canvas operation is not active on this document",
            ));
        }
        let page = Reference::new(number, generation);
        let captured = snapshot.take(&source.document()?, page, operation)?;
        let budget = operation.for_source(source)?;
        let target = document.reading()?;
        if let Some(number) = forms
            .0
            .find(&captured, &target, &budget)
            .map_err(page_error)?
        {
            return Ok(Some(number));
        }
        let same = std::ptr::eq(document, source);
        document.edit(py, |transaction, _| {
            forms
                .0
                .write(transaction, &captured, same, &budget)
                .map(Some)
                .map_err(page_error)
        })
    })?
}

/// Copies image object `number` of `source`, as it is in the version that
/// reads of `source` see, with the objects it reaches but pages, into
/// `document`'s canvas transaction, and returns the copy's number. Reading
/// and writing charge the canvas `operation`, under the source's limits
/// too. `None` when `source` has no object `number` or it is no image
/// XObject.
#[pyfunction]
pub fn canvas_copy_image(
    py: Python<'_>,
    document: &_Document,
    source: &_Document,
    number: u32,
    operation: &OperationBudget,
) -> PyResult<Option<u32>> {
    crate::errors::boundary(|| {
        if !operation.is_active_on(document) {
            return Err(PyValueError::new_err(
                "the canvas operation is not active on this document",
            ));
        }
        let budget = operation.for_source(source)?;
        let version = source.reading()?;
        document.edit(py, |transaction, _| {
            canvas::copy_image(transaction, &version, number, &budget).map_err(page_error)
        })
    })?
}

/// Sets a page's rotation. Returns false for a stale page.
#[pyfunction]
pub fn set_page_rotation(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    rotation: i64,
) -> PyResult<bool> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            geometry::set_rotation(
                transaction,
                &document.draft_pages,
                Reference::new(number, generation),
                rotation,
                budget,
            )
            .map_err(page_error)
        })
    })?
}

/// The five page boxes in display space, or None for a stale page.
#[pyfunction]
pub fn page_boxes(
    document: &_Document,
    number: u32,
    generation: u16,
) -> PyResult<Option<Vec<Box4>>> {
    crate::errors::boundary(|| {
        read_page(document, Reference::new(number, generation), |page| {
            Vec::from(geometry::boxes(page))
        })
    })?
}

/// Sets a box in the page's current display space. Returns false for a stale page.
#[pyfunction]
pub fn set_page_box(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    kind: usize,
    display: Box4,
) -> PyResult<bool> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            geometry::set_box(
                transaction,
                &document.draft_pages,
                Reference::new(number, generation),
                kind,
                display,
                budget,
            )
            .map_err(page_error)
        })
    })?
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
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            tree::set_pages(
                transaction,
                &document.draft_pages,
                &document.references,
                pages.into_iter().map(|(n, g)| Reference::new(n, g)),
                &copies,
                budget,
            )
            .map_err(page_error)
        })
    })?
}

/// Inserts copies of the pages of `source` at `at` in one transaction.
#[pyfunction]
pub fn import_pages(
    py: Python<'_>,
    document: &_Document,
    source: &_Document,
    pages: Vec<(u32, u16)>,
    at: usize,
) -> PyResult<()> {
    crate::errors::boundary(|| {
        // Pin the source before taking the destination's writer lock.
        // The import itself, including the source index, uses the edit budget.
        let (version, parent) = source.reading_parent()?;
        let memo = if parent.is_none() && source.in_transaction() {
            &source.draft_pages
        } else {
            &source.pages
        };
        let destinations = destinations_memo(source);
        document.edit(py, |transaction, budget| {
            let index = PageIndex::memoized(memo, parent.as_ref().unwrap_or(&version), budget)
                .map_err(|error| store_error(&error))?;
            let source = pages::import::Source {
                version: &version,
                index: &index,
                references: &source.references,
                destinations,
            };
            pages::import::import_pages(
                transaction,
                &document.draft_pages,
                &source,
                pages
                    .into_iter()
                    .map(|(number, generation)| Reference::new(number, generation)),
                at,
                budget,
            )
            .map_err(page_error)
        })
    })?
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
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            let page = tree::new_page(
                transaction,
                &document.draft_pages,
                at,
                width,
                height,
                budget,
            )
            .map_err(page_error)?;
            Ok((page.number(), page.generation()))
        })
    })?
}

pub(crate) fn resolve(
    version: &Version,
    value: Option<&Value>,
    budget: &Budget,
) -> PyResult<Option<Value>> {
    pages::resolve(version, value, budget).map_err(|error| store_error(&error))
}

fn page_error(error: pages::PageError) -> PyErr {
    match error {
        pages::PageError::Store(error) => store_error(&error),
        pages::PageError::Annotation(error) => annotation_error(error),
        pages::PageError::Navigation(error) => navigation_error(error),
        pages::PageError::Malformed(message) => MalformedPdfError::new_err(message),
        pages::PageError::Value(message) => PyValueError::new_err(message),
    }
}
