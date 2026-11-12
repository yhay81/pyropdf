//! PyO3 bindings for the document outline.

use pyo3::prelude::*;
pub(crate) use pyropdf_engine::navigation::Outline;
use pyropdf_engine::navigation::{self, Color, EntryRecord, ItemRecord, Spec, Target};

use crate::annotations::links::{destinations_memo, navigation_error};
use crate::document::_Document;
use crate::pages::index;

/// Returns the top-level items of the outline, or the children of item
/// `parent`.
#[pyfunction]
#[pyo3(signature = (document, parent=None))]
pub fn outline_children(
    py: Python<'_>,
    document: &_Document,
    parent: Option<(u32, u16)>,
) -> PyResult<Vec<(u32, u16)>> {
    crate::errors::boundary(|| {
        let (version, _) = document.reading_parent()?;
        let budget = document.budget();
        py.detach(|| navigation::outline_children(&version, &budget, parent))
            .map_err(navigation_error)
    })?
}

/// Returns item `id` as a handle reads it.
#[pyfunction]
pub fn outline_item(py: Python<'_>, document: &_Document, id: (u32, u16)) -> PyResult<ItemRecord> {
    crate::errors::boundary(|| {
        let (version, index, budget) = index(document)?;
        let destinations = destinations_memo(document);
        py.detach(|| navigation::outline_item(&version, &index, destinations, &budget, id))
            .map_err(navigation_error)
    })?
}

/// Returns every item of the outline in document order.
#[pyfunction]
pub fn outline_entries(py: Python<'_>, document: &_Document) -> PyResult<Vec<EntryRecord>> {
    crate::errors::boundary(|| {
        let (version, parent) = document.reading_parent()?;
        let budget = document.budget();
        let in_transaction = document.in_transaction();
        let destinations = if in_transaction {
            &document.draft_destinations
        } else {
            &document.destinations
        };
        let (outline, pages, version) = match parent {
            Some(parent) => (&document.outline, &document.pages, parent),
            None if in_transaction => (&document.draft_outline, &document.draft_pages, version),
            None => (&document.outline, &document.pages, version),
        };
        py.detach(|| navigation::outline_entries(outline, pages, destinations, &version, &budget))
            .map_err(navigation_error)
    })?
}

/// Returns how many outline walks the document's memos reused and how many
/// they computed, for tests.
#[pyfunction]
pub fn outline_walk_counts(document: &_Document) -> PyResult<(u64, u64)> {
    crate::errors::boundary(|| {
        navigation::outline_walk_counts(&document.outline, &document.draft_outline)
    })
}

/// Replaces the outline with the items of `specs`, all checked before any
/// change, and frees the objects of the outline before that nothing else
/// refers to.
#[pyfunction]
pub fn outline_write(py: Python<'_>, document: &_Document, specs: Vec<Spec>) -> PyResult<()> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            navigation::outline_write(
                transaction,
                &document.draft_pages,
                &document.references,
                budget,
                specs,
            )
            .map_err(navigation_error)
        })
    })?
}

/// Sets the title of item `id`.
#[pyfunction]
pub fn outline_set_title(
    py: Python<'_>,
    document: &_Document,
    id: (u32, u16),
    title: &str,
) -> PyResult<()> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            navigation::outline_set_title(transaction, budget, id, title).map_err(navigation_error)
        })
    })?
}

/// Makes item `id` lead to `target`.
#[pyfunction]
pub fn outline_set_target(
    py: Python<'_>,
    document: &_Document,
    id: (u32, u16),
    target: Target,
) -> PyResult<()> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            navigation::outline_set_target(transaction, &document.draft_pages, budget, id, &target)
                .map_err(navigation_error)
        })
    })?
}

/// Sets the color and the `/F` flags of item `id`.
#[pyfunction]
#[pyo3(signature = (document, id, color, flags=None, flag=None))]
pub fn outline_set_style(
    py: Python<'_>,
    document: &_Document,
    id: (u32, u16),
    color: Option<Color>,
    flags: Option<u32>,
    flag: Option<(u32, bool)>,
) -> PyResult<()> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            navigation::outline_set_style(transaction, budget, id, color, flags, flag)
                .map_err(navigation_error)
        })
    })?
}

/// Shows or hides the children of item `id`, and counts every `/Count` of
/// the outline again (ISO 32000-2 Table 151).
#[pyfunction]
pub fn outline_set_open(
    py: Python<'_>,
    document: &_Document,
    id: (u32, u16),
    open: bool,
) -> PyResult<()> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            navigation::outline_set_open(transaction, budget, id, open).map_err(navigation_error)
        })
    })?
}
