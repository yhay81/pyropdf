//! The images of `python/pyropdf/_images.py`: an image's pixels as hayro
//! decodes them, its data as stored, and the PNG and JPEG helpers of image
//! replacement and rewriting.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyropdf_engine::extract::{self, DecodedImage, Error};
use pyropdf_engine::pixmap::images as engine_images;

use crate::document::_Document;
use crate::errors::{PdfError, StaleObjectError, limit_error, store_error};
use crate::pixmap::{Pixmap, bridge_error, decode_error};

/// Returns image `ordinal` of the page whose object is `number` and
/// `generation`, counted as `Page.content()` with `annotations` counts its
/// images, decoded by hayro with the GIL released; `None` when the page
/// paints fewer images or hayro cannot decode it.
#[pyfunction]
pub fn page_image(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    ordinal: usize,
    annotations: bool,
) -> PyResult<Option<Pixmap>> {
    crate::errors::boundary(|| {
        let (version, index, budget) = crate::pages::index(document)?;
        let image = py.detach(
            || -> PyResult<Option<(pyropdf_engine::pixmap::Layout, Vec<u8>)>> {
                let at = index
                    .index_of(pyropdf_store::value::Reference::new(number, generation))
                    .ok_or_else(|| {
                        StaleObjectError::new_err(format!(
                            "page object {number} {generation} is no longer a page of the document"
                        ))
                    })?;
                let bridged = document
                    .bridge
                    .get(&version, &index, &budget)
                    .map_err(|e| bridge_error(&e))?;
                let decoded: Option<DecodedImage> =
                    extract::page_image(&bridged, at, ordinal, annotations, &budget).map_err(
                        |e| match e {
                            Error::Limit(refusal) => limit_error(&refusal),
                            Error::NoPage(_) | Error::Allocation(_) => {
                                PdfError::new_err(e.to_string())
                            }
                        },
                    )?;
                decoded
                    .map(|img| engine_images::interleave_image(img).map_err(PdfError::new_err))
                    .transpose()
            },
        )?;
        Ok(image.map(|(layout, data)| {
            Pixmap::from_samples(layout.width, layout.height, layout.gray, layout.alpha, data)
        }))
    })?
}

/// Returns the image filter and the data as stored of the image stream
/// `root`, its other filters decoded within the document's limits; `None`
/// when the last filter is not an image filter or the stream is not one.
#[pyfunction]
pub fn image_stored<'py>(
    py: Python<'py>,
    document: &_Document,
    root: (u32, u16),
) -> PyResult<Option<(String, Bound<'py, PyBytes>)>> {
    crate::errors::boundary(|| {
        let version = document.reading()?;
        let budget = document.budget();
        let stored = py
            .detach(|| engine_images::stored_image(&version, root, &budget))
            .map_err(|e| store_error(&e))?;
        Ok(stored.map(|(filter, data)| (filter, PyBytes::new(py, data.as_ref()))))
    })?
}

/// A decoded PNG: width, height, whether gray, the color samples, and the
/// alpha samples, 8 bits each.
type DecodedPng<'py> = (
    u32,
    u32,
    bool,
    Bound<'py, PyBytes>,
    Option<Bound<'py, PyBytes>>,
);

/// Decodes PNG `data` to 8-bit samples, its palette and transparency
/// expanded, refusing more than 64,000,000 pixels with `LimitError`
/// `total_decompressed_size`.
#[pyfunction]
pub fn decode_png<'py>(
    py: Python<'py>,
    document: &_Document,
    data: &[u8],
) -> PyResult<DecodedPng<'py>> {
    crate::errors::boundary(|| {
        let budget = document.budget();
        // Decoding and splitting a frame can take long enough for another Python
        // thread to cancel it. Keep Python allocation outside the detached work.
        let (width, height, gray, color, alpha) = py
            .detach(|| engine_images::decode_png_buffers(data, &budget))
            .map_err(decode_error)?;
        budget.poll().map_err(|e| limit_error(&e))?;
        Ok((
            width,
            height,
            gray,
            PyBytes::new(py, &color),
            alpha.map(|samples| PyBytes::new(py, &samples)),
        ))
    })?
}

/// Width, height, color components, 8-bit samples, and the alpha that
/// JPEG 2000 data may carry.
type Samples<'py> = (
    u32,
    u32,
    u8,
    Bound<'py, PyBytes>,
    Option<Bound<'py, PyBytes>>,
);

/// Decodes the JPEG (or, with `jpx`, JPEG 2000) data of an image to the
/// samples that hayro-syntax's `DCTDecode` and `JPXDecode` filters give
/// its color space, without converting them to RGB: CMYK as stored, YCCK
/// as pdf.js turns it into CMYK, and JPEG 2000's palette indexes kept.
#[pyfunction]
pub fn image_samples<'py>(py: Python<'py>, data: &[u8], jpx: bool) -> PyResult<Samples<'py>> {
    crate::errors::boundary(|| {
        let (width, height, components, color, alpha) = py
            .detach(|| engine_images::image_samples(data, jpx))
            .map_err(PyValueError::new_err)?;
        Ok((
            width,
            height,
            components,
            PyBytes::new(py, &color),
            alpha.map(|alpha| PyBytes::new(py, &alpha)),
        ))
    })?
}

/// Encodes interleaved 8-bit samples of one, three, or four components as
/// JPEG data that decodes to them: four components as CMYK with an Adobe
/// APP14 marker of transform 0 when `adobe`, and without one otherwise.
#[pyfunction]
pub fn encode_jpeg_samples<'py>(
    py: Python<'py>,
    data: &[u8],
    width: u16,
    height: u16,
    components: u8,
    quality: u8,
    adobe: bool,
) -> PyResult<Bound<'py, PyBytes>> {
    crate::errors::boundary(|| {
        let out = py
            .detach(|| {
                engine_images::encode_jpeg_samples(data, width, height, components, quality, adobe)
            })
            .map_err(PyValueError::new_err)?;
        Ok(PyBytes::new(py, &out))
    })?
}
