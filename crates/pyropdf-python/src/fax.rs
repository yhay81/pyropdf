//! The PyO3 binding for CCITT Group 4 encoding (`pyropdf_engine::pixmap::fax`).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyropdf_engine::pixmap::fax;
use pyropdf_store::budget::LimitCode;

use crate::document::_Document;
use crate::errors::limit_error;

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

/// Encodes the selected lossless image representation from its sample layout.
#[pyfunction]
pub fn encode_image_samples<'py>(
    py: Python<'py>,
    document: &_Document,
    form: &str,
    data: &[u8],
    size: (u32, u32),
    components: u16,
    limit: usize,
) -> PyResult<Option<Bound<'py, PyBytes>>> {
    crate::errors::boundary(|| {
        let (width, height) = size;
        let budget = document.budget();
        let input = data.len() as u64;
        // Reserve for the materialized samples, codec scratch and copies of
        // the bounded output. JP2 processes at most a 256 by 256 tile at a
        // time. Fax uses two rows and at most one code per sample/change.
        let scratch = if form == "jpx" {
            let tile =
                u64::from(width.min(256)) * u64::from(height.min(256)) * u64::from(components);
            input
                .saturating_mul(8)
                .saturating_add(tile.saturating_mul(64))
                .saturating_add(65_536)
        } else {
            u64::from(width) * 2
        };
        let output = (limit as u64).min(
            input
                .saturating_mul(16)
                .saturating_add(u64::from(height) * 16)
                .saturating_add(512),
        );
        let reservation = scratch
            .saturating_add(input)
            .saturating_add(output.saturating_mul(3));
        budget
            .charge(LimitCode::MemorySize, reservation)
            .map_err(|error| limit_error(&error))?;
        let encoded = py
            .detach(|| match form {
                "jpx" => {
                    pyropdf_engine::pixmap::jpx::encode_jp2(data, width, height, components, limit)
                }
                "fax-g3" | "fax-g3-2d" if components == 1 => fax::encode_g3_checked(
                    data,
                    width as usize,
                    height as usize,
                    limit,
                    form == "fax-g3-2d",
                ),
                "jbig2" if components == 1 => {
                    fax::encode_jbig2_checked(data, width as usize, height as usize, limit)
                }
                _ => Err("invalid image encoder or component count".into()),
            })
            .map_err(PyValueError::new_err);
        let result = encoded.map(|data| data.map(|bytes| PyBytes::new(py, &bytes)));
        budget.release(LimitCode::MemorySize, reservation);
        budget.poll().map_err(|error| limit_error(&error))?;
        result
    })?
}
