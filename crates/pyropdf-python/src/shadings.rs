//! The gradients of `python/pyropdf/_canvas.py`: its calls of the engine's
//! shading writer, under the canvas's operation.

use std::sync::{Mutex, MutexGuard, PoisonError};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyropdf_engine::content::shading::{
    Extend, Geometry, Gradient, ShadingError, ShadingMetadata, Shadings, Stop,
};
use pyropdf_store::value::Reference;

use crate::document::_Document;
use crate::errors::store_error;
use crate::history::OperationBudget;

/// The shading objects that one canvas, or a document's colour glyphs, have
/// written, which later drawings share. The operation that wrote them charged
/// what it holds.
#[pyclass(frozen, name = "_Shadings", module = "pyropdf._core")]
#[derive(Default)]
pub(crate) struct CanvasShadings {
    table: Mutex<Shadings>,
}

#[pymethods]
impl CanvasShadings {
    #[new]
    fn new() -> PyResult<Self> {
        crate::errors::boundary(Self::default)
    }
}

impl CanvasShadings {
    /// Returns the table.
    pub(crate) fn lock(&self) -> MutexGuard<'_, Shadings> {
        self.table.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

/// Maps a refusal of the writer: a gradient that it cannot draw is the
/// caller's value.
fn shading_error(error: &ShadingError) -> PyErr {
    match error {
        ShadingError::Store(error) => store_error(error),
        _ => PyValueError::new_err(error.to_string()),
    }
}

/// Refuses an operation that is not this document's innermost canvas.
fn check_active(document: &_Document, operation: &OperationBudget) -> PyResult<()> {
    if operation.is_active_on(document) {
        Ok(())
    } else {
        Err(PyValueError::new_err(
            "the canvas operation is not active on this document",
        ))
    }
}

/// Writes or shares the objects of a gradient in `document`'s canvas
/// transaction, and returns the object numbers of its shading and of the
/// `ExtGState` of its soft mask, if it needs one, and its uniform alpha.
///
/// `radii` makes it radial. `stops` are `(offset, red, green, blue, alpha)`;
/// the canvas charged the memory they hold before it built them.
#[pyfunction]
#[allow(clippy::too_many_arguments)] // The fields of a gradient and where it goes.
#[expect(
    clippy::disallowed_methods,
    reason = "Canvas._gradient charges _STOP_SIZE per stop before conversion, including this native copy"
)]
pub fn canvas_gradient(
    py: Python<'_>,
    document: &_Document,
    shadings: &CanvasShadings,
    start: [f64; 2],
    end: [f64; 2],
    radii: Option<(f64, f64)>,
    stops: Vec<(f64, f64, f64, f64, f64)>,
    extend: &str,
    area: [f64; 4],
    bbox: Option<[f64; 4]>,
    background: Option<[f64; 4]>,
    sampled: bool,
    operation: &OperationBudget,
) -> PyResult<(u32, Option<u32>, f64)> {
    crate::errors::boundary(|| {
        check_active(document, operation)?;
        let extend = match extend {
            "none" => Extend::None,
            "pad" => Extend::Pad,
            "pad_start" => Extend::PadStart,
            "pad_end" => Extend::PadEnd,
            "repeat" => Extend::Repeat,
            "reflect" => Extend::Reflect,
            other => {
                return Err(PyValueError::new_err(format!(
                    "unknown gradient extension {other:?}"
                )));
            }
        };
        let geometry = match radii {
            None => Geometry::Axial { start, end },
            Some((start_radius, end_radius)) => Geometry::Radial {
                start,
                start_radius,
                end,
                end_radius,
            },
        };
        let stops: Vec<Stop> = stops
            .into_iter()
            .map(|(offset, red, green, blue, alpha)| Stop {
                offset,
                color: [red, green, blue],
                alpha,
            })
            .collect();
        let gradient = Gradient {
            geometry,
            stops: &stops,
            extend,
        };
        let mut table = shadings
            .table
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        let painted = document.edit(py, |transaction, budget| {
            table
                .paint_with_metadata(
                    transaction,
                    &gradient,
                    area,
                    ShadingMetadata {
                        bbox,
                        background,
                        sampled,
                    },
                    budget,
                )
                .map_err(|error| shading_error(&error))
        })?;
        Ok((
            painted.shading.number(),
            painted.mask.map(|mask| mask.number()),
            painted.alpha,
        ))
    })?
}

/// Writes or shares the shading pattern of object `shading` through `matrix`
/// in `document`'s canvas transaction, and returns its object number.
#[pyfunction]
pub fn canvas_gradient_pattern(
    py: Python<'_>,
    document: &_Document,
    shadings: &CanvasShadings,
    shading: u32,
    matrix: [f64; 6],
    operation: &OperationBudget,
) -> PyResult<u32> {
    crate::errors::boundary(|| {
        check_active(document, operation)?;
        let mut table = shadings
            .table
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        let pattern = document.edit(py, |transaction, budget| {
            table
                .pattern(transaction, Reference::new(shading, 0), matrix, budget)
                .map_err(|error| shading_error(&error))
        })?;
        Ok(pattern.number())
    })?
}
