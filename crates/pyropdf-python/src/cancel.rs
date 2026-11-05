//! `CancellationToken`: the Python side of the store's token (DESIGN.md
//! sections 9 and 10), which `DocumentLimits.cancellation` carries.

use pyo3::prelude::*;
use pyropdf_store::budget::CancellationToken as Token;

/// A flag that stops the operations of every document whose limits carry it.
///
/// Any thread may call :meth:`cancel`, also while another thread's
/// operation runs with the GIL released: the operation then refuses with
/// :class:`LimitError` code ``cancelled`` at its next check. Cancelling
/// cannot be undone, so a document whose token is cancelled refuses every
/// later operation too; open it again with another token to go on.
#[pyclass(frozen, module = "pyropdf._core")]
pub struct CancellationToken(pub(crate) Token);

#[pymethods]
impl CancellationToken {
    #[new]
    fn new() -> PyResult<Self> {
        crate::errors::boundary(|| Self(Token::new()))
    }

    /// Cancels every operation whose limits carry this token.
    fn cancel(&self) -> PyResult<()> {
        crate::errors::boundary(|| {
            self.0.cancel();
        })
    }

    /// Whether :meth:`cancel` was called.
    #[getter]
    fn cancelled(&self) -> PyResult<bool> {
        crate::errors::boundary(|| self.0.is_cancelled())
    }

    fn __repr__(&self) -> PyResult<String> {
        crate::errors::boundary(|| {
            let state = if self.0.is_cancelled() {
                "cancelled "
            } else {
                ""
            };
            format!("<{state}CancellationToken>")
        })
    }
}
