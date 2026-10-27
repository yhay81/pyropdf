//! The core of `OcrEngine`: a PP-OCR model set of `pyropdf_engine::ocr`.

use std::path::PathBuf;

use pyo3::exceptions::{PyFileNotFoundError, PyValueError};
use pyo3::prelude::*;
use pyropdf_engine::ocr::{Engine, Error, OcrTuple, Options};
use pyropdf_store::budget::{Budget, DocumentLimits, LimitCode};

use crate::errors::{OcrError, limit_error};
use crate::pixmap::Pixmap;

/// Maps an OCR error to `FileNotFoundError`, `LimitError`, or `OcrError`.
fn ocr_error(error: Error) -> PyErr {
    match error {
        Error::NotFound(path) => PyFileNotFoundError::new_err((
            2,
            "No such file or directory",
            path.display().to_string(),
        )),
        Error::Limit(refusal) => limit_error(&refusal),
        Error::Failed(message) => OcrError::new_err(message),
    }
}

/// A loaded OCR model set. Python owns rendering and the conversion of boxes
/// to page coordinates.
#[pyclass(frozen, module = "pyropdf._core")]
pub struct _OcrEngine {
    engine: Engine,
}

#[pymethods]
impl _OcrEngine {
    /// Loads the model set at the paths with the GIL released.
    /// `max_model_size` bounds the bytes of the three files with
    /// `ocr_model_size`; `None` lifts it.
    #[new]
    fn new(
        py: Python<'_>,
        detector_path: PathBuf,
        recognizer_path: PathBuf,
        dictionary_path: PathBuf,
        threads: usize,
        max_model_size: Option<u64>,
    ) -> PyResult<Self> {
        if max_model_size == Some(0) {
            return Err(PyValueError::new_err(
                "max_model_size must be a positive integer or None",
            ));
        }
        let budget = Budget::new(&DocumentLimits::default())
            .with_limit(LimitCode::OcrModelSize, max_model_size);
        py.detach(|| {
            Engine::load(
                &detector_path,
                &recognizer_path,
                &dictionary_path,
                threads,
                &budget,
            )
        })
        .map(|engine| Self { engine })
        .map_err(ocr_error)
    }

    /// Recognizes the words of `pixmap` with the GIL released, as boxes in
    /// its pixel coordinates with their text and confidence.
    #[pyo3(signature = (pixmap, *, tile_size=1408, overlap=192, min_confidence=0.5, rotation=0))]
    fn recognize_pixmap(
        &self,
        py: Python<'_>,
        pixmap: PyRef<'_, Pixmap>,
        tile_size: usize,
        overlap: usize,
        min_confidence: f32,
        rotation: usize,
    ) -> PyResult<Vec<OcrTuple>> {
        let (pixels, layout) = pixmap.ocr_raster();
        drop(pixmap);
        let options = Options {
            tile_size,
            overlap,
            min_confidence,
            rotation,
        };
        py.detach(|| self.engine.recognize(&pixels, layout, &options))
            .map_err(ocr_error)
    }

    fn __repr__(&self) -> &'static str {
        "<pyropdf.OcrEngine native models>"
    }
}
