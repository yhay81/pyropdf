//! PyO3 entry point for removing a page's rotation.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyropdf_engine::pages::unrotate;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::value::Reference;

use crate::document::_Document;
use crate::errors::store_error;

fn unrotate_error(error: unrotate::Error) -> PyErr {
    match error {
        unrotate::Error::Store(error) => store_error(&error),
        unrotate::Error::SharedAnnotation(message) => PyValueError::new_err(message),
    }
}

/// Sets the rotation of page object `(number, generation)` to 0 while
/// keeping what it displays, in one transaction. An annotation or `/Annots`
/// array that another page lists too raises `ValueError` and changes nothing.
/// Returns `false` for a stale page.
#[pyfunction]
pub fn remove_page_rotation(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
) -> PyResult<bool> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            let version = transaction.snapshot();
            let index = PageIndex::memoized(&document.draft_pages, &version, budget)
                .map_err(|error| store_error(&error))?;
            unrotate::remove_page_rotation(
                transaction,
                &index,
                &document.references,
                budget,
                Reference::new(number, generation),
            )
            .map_err(unrotate_error)
        })
    })?
}
