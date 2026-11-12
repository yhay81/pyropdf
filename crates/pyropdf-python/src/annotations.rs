//! PyO3 bindings for page annotations (`page.annotations`).

pub(crate) mod input;
pub mod links;

use std::collections::HashMap;
use std::sync::Arc;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
pub(crate) use pyropdf_engine::annotations::Rect;
use pyropdf_engine::annotations::{
    self, AnnotationError, AnnotationGeometry, AnnotationRecord, GeometryTuple, IsolatedAnnotation,
    PageAnnots, RecordTuple,
};
use pyropdf_engine::bridge::Bridged;
use pyropdf_engine::extract;
use pyropdf_engine::render::{self, Raster};
use pyropdf_store::budget::{Budget, LimitCode};
use pyropdf_store::pages::PageIndex;
use pyropdf_store::version::Transaction;

use crate::document::_Document;
use crate::errors::{
    LimitError, MalformedPdfError, PdfError, StaleObjectError, limit_error, store_error,
};
use crate::fonts::_Font;
use crate::pages::index as page_index;
use crate::pixmap::{Pixmap, bridge_error, raster_error};
use crate::text::{OptionsTuple, text_error, text_options};

/// Maps a typed annotation engine error to its Python exception class.
pub(crate) fn annotation_error(error: AnnotationError) -> PyErr {
    match error {
        AnnotationError::Store(error) => store_error(&error),
        AnnotationError::Limit { code, message } => LimitError::new_err((code.as_str(), message)),
        AnnotationError::Malformed(message) => MalformedPdfError::new_err(message),
        AnnotationError::Stale(message) => StaleObjectError::new_err(message),
        AnnotationError::Value(message) => PyValueError::new_err(message),
        AnnotationError::Pdf(message) => PdfError::new_err(message),
    }
}

/// Lists the annotations of page object `(number, generation)` that
/// `page.annotations` lists, or returns `None` for a page that is gone.
#[pyfunction]
#[expect(
    clippy::disallowed_methods,
    reason = "maps already-budgeted AnnotationRecord items one to one into PyO3 tuples"
)]
pub fn annotation_list(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
) -> PyResult<Option<Vec<RecordTuple>>> {
    crate::errors::boundary(|| {
        let (version, pages, budget) = page_index(document)?;
        py.detach(|| {
            annotations::annotation_list(version, pages, &budget, number, generation).map(
                |records| {
                    records.map(|records| {
                        records
                            .into_iter()
                            .map(AnnotationRecord::into_tuple)
                            .collect()
                    })
                },
            )
        })
        .map_err(annotation_error)
    })?
}

/// Reads the annotation that a handle names. Returns `None` for a page that
/// is gone, and raises `StaleObjectError` for an annotation that is.
#[pyfunction]
pub fn annotation_read(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
) -> PyResult<Option<RecordTuple>> {
    crate::errors::boundary(|| {
        let (version, pages, budget) = page_index(document)?;
        py.detach(|| {
            annotations::annotation_read(version, pages, &budget, number, generation, index, id)
                .map(|record| record.map(AnnotationRecord::into_tuple))
        })
        .map_err(annotation_error)
    })?
}

/// Reads the points and line endings of the annotation that a handle names,
/// or returns `None` for a page that is gone.
#[pyfunction]
pub fn annotation_geometry(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
) -> PyResult<Option<GeometryTuple>> {
    crate::errors::boundary(|| {
        let (version, pages, budget) = page_index(document)?;
        py.detach(|| {
            annotations::annotation_geometry(version, pages, &budget, number, generation, index, id)
                .map(|geometry| geometry.map(AnnotationGeometry::into_tuple))
        })
        .map_err(annotation_error)
    })?
}

/// Applies `edit` to the annotation that a handle names, marks its appearance
/// stale unless `face` is set, and returns its object, or `None` for a page
/// that is gone.
#[pyfunction]
#[pyo3(signature = (document, number, generation, index, id, edit, face=false))]
#[expect(
    clippy::too_many_arguments,
    reason = "a handle's location, the edit, and its flag"
)]
pub fn annotation_edit(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
    edit: &Bound<'_, PyAny>,
    face: bool,
) -> PyResult<Option<(u32, u16)>> {
    crate::errors::boundary(|| {
        editing(
            py,
            document,
            number,
            generation,
            |annots, transaction, budget| {
                let patch = input::read_patch(edit, budget)?;
                annots
                    .edit(transaction, index, id, &patch, face)
                    .map_err(annotation_error)
            },
        )
    })?
}

/// Returns the object number of the Type0 font that the normal appearance of
/// the free text that a handle names draws its text in, or `None`.
#[pyfunction]
pub fn annotation_face(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
) -> PyResult<Option<u32>> {
    crate::errors::boundary(|| {
        let (version, pages, budget) = page_index(document)?;
        py.detach(|| {
            annotations::annotation_face(version, pages, &budget, number, generation, index, id)
        })
        .map_err(annotation_error)
    })?
}

/// Draws the free text that a handle names in embedded OpenType `face` and
/// returns the shaped glyphs with their texts.
#[pyfunction]
#[pyo3(signature = (document, number, generation, index, id, face, font=None))]
#[expect(
    clippy::too_many_arguments,
    reason = "a handle's location, the face, and its font"
)]
pub fn annotation_draw_face(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
    face: &_Font,
    font: Option<(u32, HashMap<u32, String>)>,
) -> PyResult<Option<Vec<(u32, String)>>> {
    crate::errors::boundary(|| {
        let Some(face) = face.embedded() else {
            return Err(PdfError::new_err(
                "free text draws an OpenType face here, not a Standard 14 font",
            ));
        };
        let font_ref = font.as_ref().map(|(num, mapped)| (*num, mapped));
        document.edit(py, |transaction, budget| {
            annotations::annotation_draw_face(
                transaction,
                &document.draft_pages,
                budget,
                number,
                generation,
                index,
                id,
                face,
                font_ref,
            )
            .map_err(annotation_error)
        })
    })?
}

/// Moves, adds, or removes (`None`) the popup of the annotation that a handle
/// names.
#[pyfunction]
pub fn annotation_popup(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
    area: Option<Rect>,
) -> PyResult<Option<(u32, u16)>> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            annotations::annotation_popup(
                transaction,
                &document.draft_pages,
                budget,
                number,
                generation,
                index,
                id,
                area,
            )
            .map_err(annotation_error)
        })
    })?
}

/// Opens or closes the popup of the annotation that a handle names, and a
/// note's own `/Open`.
#[pyfunction]
pub fn annotation_open(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
    open: bool,
) -> PyResult<Option<(u32, u16)>> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            annotations::annotation_open(
                transaction,
                &document.draft_pages,
                budget,
                number,
                generation,
                index,
                id,
                open,
            )
            .map_err(annotation_error)
        })
    })?
}

/// Makes the annotation that a handle names a reply to `target` (`/IRT`), or
/// clears its reply state with `None`.
#[pyfunction]
pub fn annotation_reply(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
    target: Option<(u32, u16)>,
) -> PyResult<Option<(u32, u16)>> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            annotations::annotation_reply(
                transaction,
                &document.draft_pages,
                budget,
                number,
                generation,
                index,
                id,
                target,
            )
            .map_err(annotation_error)
        })
    })?
}

/// Removes the annotations that `handles` name (and any popups they name on
/// the page) from the page's `/Annots`.
#[pyfunction]
pub fn annotation_remove(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    handles: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    crate::errors::boundary(|| {
        editing(
            py,
            document,
            number,
            generation,
            |annots, transaction, budget| {
                let handles = input::read_handles(handles, budget)?;
                annots
                    .remove(transaction, &handles)
                    .map_err(annotation_error)
            },
        )
        .map(|page| page.is_some())
    })?
}

/// Returns the object numbers whose appearance the current readable version
/// leaves stale.
#[pyfunction]
pub fn _stale_appearances(py: Python<'_>, document: &_Document) -> PyResult<Vec<u32>> {
    crate::errors::boundary(|| {
        let version = document.reading()?;
        let budget = document.budget();
        py.detach(|| annotations::stale_appearances(&version, &budget))
            .map_err(annotation_error)
    })?
}

/// Adds the annotation that `spec` describes to page `(number, generation)`.
#[pyfunction]
#[pyo3(signature = (document, number, generation, spec, face=false))]
pub fn annotation_add(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    spec: &Bound<'_, PyAny>,
    face: bool,
) -> PyResult<Option<(u32, u16)>> {
    crate::errors::boundary(|| {
        editing(
            py,
            document,
            number,
            generation,
            |annots, transaction, budget| {
                let spec = input::SpecInput::read(spec, budget)?;
                annots
                    .add(transaction, &spec.as_spec(), face)
                    .map_err(annotation_error)
            },
        )
    })?
}

fn isolated(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
) -> PyResult<Option<IsolatedAnnotation>> {
    document.scratch(py, |transaction, budget| {
        annotations::isolate_annotation(transaction, budget, number, generation, index, id)
            .map_err(annotation_error)
    })
}

/// Renders the appearance of the annotation that a handle names alone over its
/// display rectangle with the GIL released.
#[pyfunction]
#[expect(
    clippy::too_many_arguments,
    reason = "the handle and the options of Annotation.pixmap"
)]
pub fn annotation_pixmap(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
    matrix: (f64, f64, f64, f64),
    alpha: bool,
    gray: bool,
    background: (u8, u8, u8, u8),
) -> PyResult<Option<Pixmap>> {
    crate::errors::boundary(|| {
        let Some(isolated) = isolated(py, document, number, generation, index, id)? else {
            return Ok(None);
        };
        let budget = document.budget();
        let (red, green, blue, opacity) = background;
        let options = render::Options {
            matrix,
            alpha,
            gray,
            background: [red, green, blue, opacity],
            clip: Some(isolated.rect),
            annotations: false,
            hinting: false,
            glyph_cache: false,
        };
        let fallback = document.fallback();
        let raster = py.detach(|| -> PyResult<Raster> {
            budget.poll().map_err(|e| limit_error(&e))?;
            let bridged = Arc::new(
                Bridged::new(&isolated.version, &isolated.pages, &budget)
                    .map_err(|e| bridge_error(&e))?,
            );
            let raster = render::raster(&bridged, &fallback, isolated.page_index, &options)
                .map_err(raster_error)?;
            budget.poll().map_err(|e| limit_error(&e))?;
            Ok(raster)
        })?;
        Ok(Some(Pixmap::from(raster)))
    })?
}

/// Extracts the text of the appearance of the annotation that a handle names
/// alone with the GIL released.
#[pyfunction]
#[expect(
    clippy::too_many_arguments,
    reason = "the handle and the options of Annotation.text"
)]
pub fn annotation_text(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
    options: OptionsTuple,
    max_glyphs: Option<u64>,
    max_size: Option<u64>,
) -> PyResult<Option<(String, usize)>> {
    crate::errors::boundary(|| {
        let options = text_options(options)?;
        let Some(isolated) = isolated(py, document, number, generation, index, id)? else {
            return Ok(None);
        };
        let budget = document.call_budget(&[
            (LimitCode::TextGlyphCount, max_glyphs),
            (LimitCode::TextSize, max_size),
        ])?;
        py.detach(|| {
            let bridged = Bridged::new(&isolated.version, &isolated.pages, &budget)
                .map_err(|e| bridge_error(&e))?;
            let mut text = String::new();
            let (skipped, _) =
                extract::page_text(&bridged, isolated.page_index, &options, &budget, &mut text)
                    .map_err(text_error)?;
            Ok(Some((text, skipped)))
        })
    })?
}

/// Looks up the page on the version held by the writer.
pub(crate) fn editing<T>(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    write: impl FnOnce(&PageAnnots<'_>, &mut Transaction, &Budget) -> PyResult<T>,
) -> PyResult<Option<T>> {
    document.edit(py, |transaction, budget| {
        let version = transaction.snapshot();
        let pages = PageIndex::memoized(&document.draft_pages, &version, budget)
            .map_err(|error| store_error(&error))?;
        let Some(annots) = PageAnnots::read(version, pages, budget, number, generation)
            .map_err(annotation_error)?
        else {
            return Ok(None);
        };
        write(&annots, transaction, budget).map(Some)
    })
}
