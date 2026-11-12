//! PyO3 bindings for link annotations and named destinations.

use pyo3::prelude::*;
pub(crate) use pyropdf_engine::navigation::NamedDestinations;
use pyropdf_engine::navigation::{self, LinkRecord, Named, Rect, Target};
use pyropdf_store::memo::Memo;

use crate::document::_Document;
use crate::errors::{LimitError, MalformedPdfError, PdfError, StaleObjectError, store_error};
use crate::pages::index as page_index;

/// Returns the named-destinations memo for the version that reads of
/// `document` see.
pub(crate) fn destinations_memo(document: &_Document) -> &Memo<(), NamedDestinations> {
    if document.in_transaction() {
        &document.draft_destinations
    } else {
        &document.destinations
    }
}

/// Maps a typed navigation or outline error to its Python exception class.
pub(crate) fn navigation_error(error: navigation::Error) -> PyErr {
    match error {
        navigation::Error::Store(error) => store_error(&error),
        navigation::Error::Limit { code, message } => LimitError::new_err((code.as_str(), message)),
        navigation::Error::Malformed(message) => MalformedPdfError::new_err(message),
        navigation::Error::Stale(message) => StaleObjectError::new_err(message),
        navigation::Error::Pdf(message) => PdfError::new_err(message),
    }
}

/// Returns the links of page object `(number, generation)` in `/Annots`
/// order, or `None` for a page that is gone.
#[pyfunction]
pub fn link_list(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
) -> PyResult<Option<Vec<LinkRecord>>> {
    crate::errors::boundary(|| {
        let (version, pages, budget) = page_index(document)?;
        let destinations = destinations_memo(document);
        py.detach(|| {
            navigation::link_list(&version, &pages, destinations, &budget, number, generation)
        })
        .map_err(navigation_error)
    })?
}

/// Returns the link that a handle names, or `None` for a page that is gone.
#[pyfunction]
pub fn link_read(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
) -> PyResult<Option<LinkRecord>> {
    crate::errors::boundary(|| {
        let (version, pages, budget) = page_index(document)?;
        let destinations = destinations_memo(document);
        py.detach(|| {
            navigation::link_read(
                &version,
                &pages,
                destinations,
                &budget,
                number,
                generation,
                index,
                id,
            )
        })
        .map_err(navigation_error)
    })?
}

/// Returns the named destinations of the document in the order that
/// `doc.destinations` lists them.
#[pyfunction]
pub fn named_destinations(py: Python<'_>, document: &_Document) -> PyResult<Vec<Named>> {
    crate::errors::boundary(|| {
        let (version, index, budget) = page_index(document)?;
        let destinations = destinations_memo(document);
        py.detach(|| navigation::named_destinations(destinations, &version, &index, &budget))
            .map_err(navigation_error)
    })?
}

/// Adds a link over a display rectangle of page object `(number,
/// generation)` that leads to `target`, and returns its object, or `None`
/// for a page that is gone.
#[pyfunction]
pub fn link_add(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    area: Rect,
    target: Target,
) -> PyResult<Option<(u32, u16)>> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            navigation::link_add(
                transaction,
                &document.draft_pages,
                budget,
                number,
                generation,
                area,
                &target,
            )
            .map_err(navigation_error)
        })
    })?
}

/// Moves the link that a handle names to a display rectangle, makes it lead
/// to `target`, or both, and returns its object, which a direct dictionary
/// gains here; or `None` for a page that is gone.
#[pyfunction]
#[pyo3(signature = (document, number, generation, index, id, area, target))]
#[expect(
    clippy::too_many_arguments,
    reason = "a handle's page and annotation, and the two things a link edit changes"
)]
pub fn link_edit(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
    area: Option<Rect>,
    target: Option<Target>,
) -> PyResult<Option<(u32, u16)>> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            navigation::link_edit(
                transaction,
                &document.draft_pages,
                budget,
                number,
                generation,
                index,
                id,
                area,
                target.as_ref(),
            )
            .map_err(navigation_error)
        })
    })?
}
