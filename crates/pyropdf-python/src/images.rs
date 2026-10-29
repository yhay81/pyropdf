//! The images of `python/pyropdf/_images.py`: an image's pixels as hayro
//! decodes them, its data as stored, and the PNG input of a replacement.

use std::io::Cursor;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyropdf_engine::extract::{self, DecodedImage, Error};
use pyropdf_store::budget::LimitCode;
use pyropdf_store::value::{Array, Stream, Value};

use crate::document::_Document;
use crate::errors::{PdfError, StaleObjectError, limit_error, store_error};
use crate::pixmap::{Pixmap, bridge_error};

/// The image filters whose encoded data an image keeps as stored
/// (ISO 32000-2 table 6).
const IMAGE_FILTERS: [&[u8]; 4] = [
    b"DCTDecode",
    b"JPXDecode",
    b"JBIG2Decode",
    b"CCITTFaxDecode",
];

/// The most pixels of a PNG that `decode_png` reads, as rendering bounds
/// one raster.
const MAX_PNG_PIXELS: u64 = 64_000_000;

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
    let (version, index, budget) = crate::pages::index(document)?;
    let image = py.detach(|| -> PyResult<Option<DecodedImage>> {
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
        extract::page_image(&bridged, at, ordinal, annotations, &budget).map_err(|e| match e {
            Error::Limit(refusal) => limit_error(&refusal),
            Error::NoPage(_) | Error::Allocation(_) => PdfError::new_err(e.to_string()),
        })
    })?;
    image.map(pixmap).transpose()
}

/// Returns the pixmap of `image`, its alpha interleaved after each pixel.
fn pixmap(image: DecodedImage) -> PyResult<Pixmap> {
    let DecodedImage {
        width,
        height,
        gray,
        color,
        alpha,
    } = image;
    let Some(alpha) = alpha else {
        return Ok(Pixmap::from_samples(width, height, gray, false, color));
    };
    let channels = if gray { 1 } else { 3 };
    let mut data = Vec::new();
    data.try_reserve_exact(color.len() + alpha.len())
        .map_err(|error| PdfError::new_err(format!("cannot hold the image: {error}")))?;
    for (pixel, opacity) in color.chunks_exact(channels).zip(alpha) {
        data.extend_from_slice(pixel);
        data.push(opacity);
    }
    Ok(Pixmap::from_samples(width, height, gray, true, data))
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
    let version = document.reading()?;
    let budget = document.budget();
    let (number, generation) = root;
    let Some(object) = version
        .get(number, &budget)
        .map_err(|e| store_error(&e))?
        .filter(|object| object.generation() == generation)
    else {
        return Ok(None);
    };
    let Value::Stream(stream) = object.value().as_ref() else {
        return Ok(None);
    };
    let names = |value: Option<&Value>| -> Option<Vec<Vec<u8>>> {
        match value? {
            Value::Name(name) => Some(vec![name.as_bytes().to_vec()]),
            Value::Array(array) => array
                .iter()
                .map(|value| match value {
                    Value::Name(name) => Some(name.as_bytes().to_vec()),
                    _ => None,
                })
                .collect(),
            _ => None,
        }
    };
    let Some(filters) = names(stream.dict().get("Filter")) else {
        return Ok(None);
    };
    let Some(last) = filters
        .last()
        .filter(|last| IMAGE_FILTERS.contains(&last.as_slice()))
    else {
        return Ok(None);
    };
    let filter = String::from_utf8_lossy(last).into_owned();
    if filters.len() == 1 {
        return Ok(Some((filter, PyBytes::new(py, stream.data()))));
    }
    // The stream with its image filter left out decodes to the stored data.
    let keep = filters.len() - 1;
    let mut dict = stream.dict().clone();
    let Some(Value::Array(array)) = dict.get("Filter") else {
        return Ok(None);
    };
    let leading: Array = array.iter().take(keep).cloned().collect();
    dict.insert("Filter".into(), Value::Array(leading));
    if let Some(Value::Array(parameters)) = dict.get("DecodeParms") {
        let leading: Array = parameters.iter().take(keep).cloned().collect();
        dict.insert("DecodeParms".into(), Value::Array(leading));
    }
    let data = py
        .detach(|| Stream::new(dict, stream.data().clone()).decoded(&budget))
        .map_err(|e| store_error(&e))?;
    Ok(Some((filter, PyBytes::new(py, &data))))
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
    let invalid =
        |error: png::DecodingError| PyValueError::new_err(format!("not a PNG image: {error}"));
    let mut decoder = png::Decoder::new(Cursor::new(data));
    decoder.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
    let header = decoder.read_header_info().map_err(invalid)?;
    let pixels = u64::from(header.width) * u64::from(header.height);
    if pixels > MAX_PNG_PIXELS {
        return Err(PyValueError::new_err(format!(
            "the PNG image has {pixels} pixels, more than the {MAX_PNG_PIXELS}-pixel limit"
        )));
    }
    document
        .budget()
        .charge(LimitCode::TotalDecompressedSize, pixels * 4)
        .map_err(|e| limit_error(&e))?;
    let mut reader = decoder.read_info().map_err(invalid)?;
    let size = reader
        .output_buffer_size()
        .ok_or_else(|| PyValueError::new_err("the PNG image is too large"))?;
    let mut buffer = vec![0; size];
    let info = reader.next_frame(&mut buffer).map_err(invalid)?;
    buffer.truncate(info.buffer_size());
    let (gray, alpha) = match info.color_type {
        png::ColorType::Grayscale => (true, false),
        png::ColorType::GrayscaleAlpha => (true, true),
        png::ColorType::Rgb | png::ColorType::Indexed => (false, false),
        png::ColorType::Rgba => (false, true),
    };
    if !alpha {
        return Ok((
            info.width,
            info.height,
            gray,
            PyBytes::new(py, &buffer),
            None,
        ));
    }
    let channels = if gray { 1 } else { 3 };
    let mut color = Vec::with_capacity(buffer.len() / (channels + 1) * channels);
    let mut opacity = Vec::with_capacity(buffer.len() / (channels + 1));
    for pixel in buffer.chunks_exact(channels + 1) {
        color.extend_from_slice(&pixel[..channels]);
        opacity.push(pixel[channels]);
    }
    Ok((
        info.width,
        info.height,
        gray,
        PyBytes::new(py, &color),
        Some(PyBytes::new(py, &opacity)),
    ))
}
