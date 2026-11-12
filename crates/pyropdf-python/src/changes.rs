//! PyO3 binding for `Document.changes()` over [`pyropdf_engine::changes`].

use pyo3::prelude::*;
use pyo3::types::PyList;
use pyropdf_engine::changes::{Error, report};

use crate::document::_Document;
use crate::errors::{PdfError, store_error};

fn change_error(error: Error) -> PyErr {
    match error {
        Error::Store(error) => store_error(&error),
        Error::Pdf(message) => PdfError::new_err(message),
    }
}

/// Returns the signatures and the claims that the edits since opening, or
/// a save that is not `incremental`, invalidate.
#[pyfunction]
pub fn changes<'py>(
    py: Python<'py>,
    document: &_Document,
    incremental: bool,
) -> PyResult<(Bound<'py, PyList>, Bound<'py, PyList>)> {
    crate::errors::boundary(|| {
        let version = document.reading()?;
        let budget = document.budget();
        let found = py
            .detach(|| report(&version, incremental, &budget))
            .map_err(change_error)?;
        let signatures = PyList::new(
            py,
            found
                .signatures
                .into_iter()
                .map(|sig| (sig.field, sig.xref, sig.reason, sig.objects)),
        )?;
        let claims = PyList::new(
            py,
            found
                .claims
                .into_iter()
                .map(|claim| (claim.claim, 0_u32, claim.reason, claim.objects)),
        )?;
        Ok((signatures, claims))
    })?
}
