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
    fn new() -> Self {
        Self(Token::new())
    }

    /// Cancels every operation whose limits carry this token.
    fn cancel(&self) {
        self.0.cancel();
    }

    /// Whether :meth:`cancel` was called.
    #[getter]
    fn cancelled(&self) -> bool {
        self.0.is_cancelled()
    }

    fn __repr__(&self) -> String {
        let state = if self.0.is_cancelled() {
            "cancelled "
        } else {
            ""
        };
        format!("<{state}CancellationToken>")
    }
}
