//! PyO3 binding for `Document.revisions` and `Revision.open()`.

use std::sync::Arc;

use pyo3::exceptions::PyIndexError;
use pyo3::prelude::*;
use pyropdf_store::budget::Budget;
use pyropdf_store::save::FileBase;
use pyropdf_store::version::Document;

use crate::document::{_Document, check_every_object, complete_checks, document_limits};
use crate::errors::store_error;
use crate::history::DEFAULT_HISTORY_LIMIT;

/// One file revision: its `%%EOF` end offset, and the object numbers that
/// it added, updated, and freed.
type RevisionTuple = (Option<usize>, Vec<u32>, Vec<u32>, Vec<u32>);

/// Returns the incremental revisions of the opened file, oldest first, or
/// an empty vector for an in-memory or repaired document.
#[pyfunction]
#[expect(
    clippy::disallowed_methods,
    reason = "one tuple per revision, whose change lists the store built under the operation's budget"
)]
pub fn document_revisions(py: Python<'_>, document: &_Document) -> PyResult<Vec<RevisionTuple>> {
    crate::errors::boundary(|| {
        let version = document.reading()?;
        let budget = document.budget();
        py.detach(|| {
            let opened = version.opened();
            let Some(index) = opened.base().file() else {
                return Ok(Vec::new());
            };
            if index.is_repaired() {
                return Ok(Vec::new());
            }
            let changes = index
                .revision_changes(&budget)
                .map_err(|e| store_error(&e))?;
            Ok(index
                .revisions()
                .iter()
                .zip(changes)
                .map(|(revision, change)| {
                    let (added, updated, freed) = change.into_parts();
                    (revision.end(), added, updated, freed)
                })
                .collect())
        })
    })?
}

/// Opens revision `index` (oldest = `0`) of `document`'s opened file as an
/// independent `_Document` sharing its byte prefix.
#[pyfunction]
pub fn open_revision(
    py: Python<'_>,
    document: &_Document,
    index: usize,
    limits: &Bound<'_, PyAny>,
) -> PyResult<_Document> {
    crate::errors::boundary(|| {
        let (limits, complete) = (document_limits(limits)?, complete_checks(limits)?);
        let version = document.reading()?;
        py.detach(|| {
            let budget = Budget::new(&limits);
            let opened = version.opened();
            let Some(file_index) = opened.base().file() else {
                return Err(PyIndexError::new_err(format!(
                    "revision index {index} out of range"
                )));
            };
            let Some(base) =
                FileBase::open_revision(file_index, index, &budget).map_err(|e| store_error(&e))?
            else {
                return Err(PyIndexError::new_err(format!(
                    "revision index {index} out of range"
                )));
            };
            let locked = base.index().is_encrypted() && base.security().is_none();
            let doc = Document::new(Arc::new(base), Some(DEFAULT_HISTORY_LIMIT));
            if complete && !locked {
                check_every_object(&doc.current(), &budget).map_err(|e| store_error(&e))?;
            }
            Ok(_Document::new_checked(doc, limits, complete))
        })
    })?
}
