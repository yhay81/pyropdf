//! The PyO3 binding for CCITT Group 4 encoding (`pyropdf_engine::pixmap::fax`).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyropdf_engine::pixmap::fax;

/// Encodes 1-bit samples, rows padded to whole bytes with 1 for white, as
/// CCITT Group 4 data for `CCITTFaxDecode` with `/K -1` and `/Columns
/// width`, or returns `None` once the data would pass `limit` bytes.
#[pyfunction]
pub fn encode_ccitt_g4<'py>(
    py: Python<'py>,
    data: &[u8],
    width: usize,
    height: usize,
    limit: usize,
) -> PyResult<Option<Bound<'py, PyBytes>>> {
    crate::errors::boundary(|| {
        let encoded = py
            .detach(|| fax::encode_checked(data, width, height, limit))
            .map_err(PyValueError::new_err)?;
        Ok(encoded.map(|bytes| PyBytes::new(py, &bytes)))
    })?
}
