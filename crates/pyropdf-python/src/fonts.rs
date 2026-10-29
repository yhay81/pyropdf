//! The font programs of `python/pyropdf/_fonts.py`: the glyphs that the
//! pages draw with each font, as hayro resolves them, and the subset of a
//! program to those glyphs.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyropdf_engine::extract::{self, Error};
use pyropdf_engine::fonts::{self, Format};

use crate::document::_Document;
use crate::errors::{PdfError, limit_error};
use crate::pixmap::bridge_error;

/// Returns, for each font dictionary of `fonts`, by object number and
/// generation, the sorted glyph IDs that the pages and their annotation
/// appearances draw with it, interpreted with the GIL released.
#[pyfunction]
pub fn used_glyphs(
    py: Python<'_>,
    document: &_Document,
    fonts: Vec<(u32, u16)>,
) -> PyResult<Vec<Vec<u32>>> {
    let (version, index, budget) = crate::pages::index(document)?;
    py.detach(|| {
        let bridged = document
            .bridge
            .get(&version, &index, &budget)
            .map_err(|e| bridge_error(&e))?;
        extract::used_glyphs(&bridged, &fonts, &budget).map_err(|e| match e {
            Error::Limit(refusal) => limit_error(&refusal),
            Error::NoPage(_) | Error::Allocation(_) => PdfError::new_err(e.to_string()),
        })
    })
}

/// Returns font program `data`, of `format` `"sfnt"` or `"cff"`, subset to
/// the glyphs of `keep` with the glyphs that they draw, and the counts of
/// its glyphs and of those kept; `None` when the program is not one that
/// the subsetter reads.
#[pyfunction]
pub fn subset_font_program<'py>(
    py: Python<'py>,
    data: &[u8],
    format: &str,
    keep: Vec<u32>,
) -> PyResult<Option<(Bound<'py, PyBytes>, usize, usize)>> {
    let format = match format {
        "sfnt" => Format::Sfnt,
        "cff" => Format::Cff,
        _ => {
            return Err(PyValueError::new_err(format!(
                "unknown font program format {format:?}"
            )));
        }
    };
    let subset = py.detach(|| fonts::subset(data, format, &keep));
    Ok(subset.map(|subset| (PyBytes::new(py, &subset.data), subset.glyphs, subset.kept)))
}
