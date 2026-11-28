//! The core of `LayoutEngine`: a pure-Rust RTen document layout detector over
//! `pyropdf_engine::layout`.

use std::path::PathBuf;

use pyo3::exceptions::{PyFileNotFoundError, PyValueError};
use pyo3::prelude::*;
use pyropdf_engine::layout::{DEFAULT_INPUT_SIZE, Engine, Error, LayoutTuple};
use pyropdf_store::budget::{Budget, DocumentLimits, LimitCode};

use crate::document::_Document;
use crate::errors::{LayoutError, limit_error};
use crate::pixmap::Pixmap;

/// Maps a layout engine error to `FileNotFoundError`, `LimitError`, or `LayoutError`.
fn layout_error(error: Error) -> PyErr {
    match error {
        Error::NotFound(path) => PyFileNotFoundError::new_err((
            2,
            "No such file or directory",
            path.display().to_string(),
        )),
        Error::Limit(refusal) => limit_error(&refusal),
        Error::Failed(message) => LayoutError::new_err(message),
    }
}

/// A loaded RTen document layout model. Python owns rendering, label mapping,
/// concurrency admission, and converting boxes to display space.
#[pyclass(frozen, module = "pyropdf._core")]
pub struct _LayoutEngine {
    engine: Engine,
}

#[pymethods]
impl _LayoutEngine {
    /// Loads the `.rten` model at `model_path` with the GIL released.
    /// `max_model_size` bounds the file size with `layout_model_size`; `None`
    /// lifts it.
    #[new]
    #[pyo3(signature = (model_path, threads, max_model_size, input_size=DEFAULT_INPUT_SIZE))]
    fn new(
        py: Python<'_>,
        model_path: PathBuf,
        threads: usize,
        max_model_size: Option<u64>,
        input_size: usize,
    ) -> PyResult<Self> {
        crate::errors::boundary(|| {
            if max_model_size == Some(0) {
                return Err(PyValueError::new_err(
                    "max_model_size must be a positive integer or None",
                ));
            }
            let budget = Budget::new(&DocumentLimits::default())
                .with_limit(LimitCode::LayoutModelSize, max_model_size);
            py.detach(|| Engine::load(&model_path, threads, input_size, &budget))
                .map(|engine| Self { engine })
                .map_err(layout_error)
        })?
    }

    /// Detects layout regions in `pixmap` with the GIL released, returning
    /// `(class_id, confidence, x0, y0, x1, y1, reading_order)` in raster pixels.
    #[pyo3(signature = (pixmap, *, min_confidence=0.5, document=None))]
    fn detect_pixmap(
        &self,
        py: Python<'_>,
        pixmap: PyRef<'_, Pixmap>,
        min_confidence: f32,
        document: Option<PyRef<'_, _Document>>,
    ) -> PyResult<Vec<LayoutTuple>> {
        crate::errors::boundary(|| {
            let budget = document
                .as_ref()
                .map_or_else(|| Budget::default().share(), |document| document.budget());
            drop(document);
            let (pixels, layout) = pixmap.layout_raster();
            drop(pixmap);
            py.detach(|| self.engine.detect(&pixels, layout, min_confidence, &budget))
                .map_err(layout_error)
        })?
    }

    fn __repr__(&self) -> PyResult<&'static str> {
        crate::errors::boundary(|| "<pyropdf.LayoutEngine native model>")
    }
}
