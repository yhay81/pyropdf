//! The images of `python/pyropdf/_images.py`: an image's pixels as hayro
//! decodes them, its data as stored, and the PNG input of a replacement.

use std::io::Cursor;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyropdf_engine::extract::{self, DecodedImage, Error};
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
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
    let budget = document.budget();
    // Decoding and splitting a frame can take long enough for another Python
    // thread to cancel it. Keep Python allocation outside the detached work.
    let (width, height, gray, color, alpha) = py.detach(|| decode_png_buffers(data, &budget))?;
    budget.poll().map_err(|e| limit_error(&e))?;
    Ok((
        width,
        height,
        gray,
        PyBytes::new(py, &color),
        alpha.map(|samples| PyBytes::new(py, &samples)),
    ))
}

type DecodedPngBuffers<'b> = (u32, u32, bool, BudgetVec<'b, u8>, Option<BudgetVec<'b, u8>>);

fn decode_png_buffers<'b>(data: &[u8], budget: &'b Budget) -> PyResult<DecodedPngBuffers<'b>> {
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
    budget
        .charge(LimitCode::TotalDecompressedSize, pixels * 4)
        .map_err(|e| limit_error(&e))?;
    let mut reader = decoder.read_info().map_err(invalid)?;
    let size = reader
        .output_buffer_size()
        .ok_or_else(|| PyValueError::new_err("the PNG image is too large"))?;
    let mut buffer = BudgetVec::new(budget);
    buffer
        .try_reserve_exact(size)
        .map_err(|e| limit_error(&e))?;
    // Initialize in small pieces so a cancelled operation does not have to
    // finish touching the entire decoded frame before noticing cancellation.
    const ZEROES: [u8; 16 * 1024] = [0; 16 * 1024];
    while buffer.len() < size {
        budget.poll().map_err(|e| limit_error(&e))?;
        let len = (size - buffer.len()).min(ZEROES.len());
        buffer
            .try_extend_from_slice(&ZEROES[..len])
            .map_err(|e| limit_error(&e))?;
    }
    let info = reader.next_frame(&mut buffer).map_err(invalid)?;
    buffer.truncate(info.buffer_size());
    let (gray, alpha) = match info.color_type {
        png::ColorType::Grayscale => (true, false),
        png::ColorType::GrayscaleAlpha => (true, true),
        png::ColorType::Rgb | png::ColorType::Indexed => (false, false),
        png::ColorType::Rgba => (false, true),
    };
    if !alpha {
        return Ok((info.width, info.height, gray, buffer, None));
    }
    let channels = if gray { 1 } else { 3 };
    let pixels = buffer.len() / (channels + 1);
    let mut color = BudgetVec::new(budget);
    let mut opacity = BudgetVec::new(budget);
    color
        .try_reserve_exact(pixels * channels)
        .map_err(|e| limit_error(&e))?;
    opacity
        .try_reserve_exact(pixels)
        .map_err(|e| limit_error(&e))?;
    for chunk in buffer.chunks((channels + 1) * 4096) {
        budget.poll().map_err(|e| limit_error(&e))?;
        for pixel in chunk.chunks_exact(channels + 1) {
            color
                .try_extend_from_slice(&pixel[..channels])
                .map_err(|e| limit_error(&e))?;
            opacity
                .try_push(pixel[channels])
                .map_err(|e| limit_error(&e))?;
        }
    }
    Ok((info.width, info.height, gray, color, Some(opacity)))
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
    let decoded = py.detach(|| {
        if jpx {
            jpx_samples(data)
        } else {
            jpeg_samples(data)
        }
    });
    let (width, height, components, color, alpha) = decoded.map_err(PyValueError::new_err)?;
    Ok((
        width,
        height,
        components,
        PyBytes::new(py, &color),
        alpha.map(|alpha| PyBytes::new(py, &alpha)),
    ))
}

type Decoded = (u32, u32, u8, Vec<u8>, Option<Vec<u8>>);

fn jpeg_samples(data: &[u8]) -> Result<Decoded, String> {
    use zune_jpeg::JpegDecoder;
    use zune_jpeg::zune_core::bytestream::ZCursor;
    use zune_jpeg::zune_core::colorspace::ColorSpace;
    use zune_jpeg::zune_core::options::DecoderOptions;

    let invalid = |error: zune_jpeg::errors::DecodeErrors| format!("not a JPEG image: {error}");
    let options = DecoderOptions::default()
        .set_max_width(usize::from(u16::MAX))
        .set_max_height(usize::from(u16::MAX));
    let mut decoder = JpegDecoder::new_with_options(ZCursor::new(data), options);
    decoder.decode_headers().map_err(invalid)?;
    let (width, height) = decoder
        .dimensions()
        .ok_or_else(|| "the JPEG image has no size".to_owned())?;
    crate::pixel_ops::check_size(width as u32, height as u32)?;
    let three = decoder.info().is_some_and(|info| info.components == 3);
    let (out, components) = match decoder.input_colorspace() {
        Some(ColorSpace::Luma | ColorSpace::LumaA) => (ColorSpace::Luma, 1),
        Some(ColorSpace::CMYK) if !three => (ColorSpace::CMYK, 4),
        Some(ColorSpace::YCCK) => (ColorSpace::YCCK, 4),
        _ => (ColorSpace::RGB, 3),
    };
    decoder.set_options(options.jpeg_set_out_colorspace(out));
    let mut samples = decoder.decode().map_err(invalid)?;
    if out == ColorSpace::YCCK {
        // As hayro-syntax, after pdf.js's jpg.js.
        for pixel in samples.as_chunks_mut::<4>().0 {
            let (y, cb, cr) = (
                f32::from(pixel[0]),
                f32::from(pixel[1]),
                f32::from(pixel[2]),
            );
            pixel[0] = (434.456 - y - 1.402 * cr) as u8;
            pixel[1] = (119.541 - y + 0.344 * cb + 0.714 * cr) as u8;
            pixel[2] = (481.816 - y - 1.772 * cb) as u8;
        }
    }
    if samples.len() != width * height * usize::from(components) {
        return Err("the JPEG image decoded to the wrong number of samples".to_owned());
    }
    Ok((width as u32, height as u32, components, samples, None))
}

fn jpx_samples(data: &[u8]) -> Result<Decoded, String> {
    use hayro_jpeg2000::{DecodeSettings, DecoderContext, Image};

    let invalid = |error| format!("not a JPEG 2000 image the decoder reads: {error:?}");
    let settings = DecodeSettings {
        resolve_palette_indices: false,
        strict: false,
        target_resolution: None,
    };
    let image = Image::new(data, &settings).map_err(invalid)?;
    let (width, height) = (image.width(), image.height());
    crate::pixel_ops::check_size(width, height)?;
    let components = image.color_space().num_channels();
    let alpha = image.has_alpha();
    let mut context = DecoderContext::default();
    let samples = image.decode(&mut context).map_err(invalid)?.data_u8();
    let channels = usize::from(components) + usize::from(alpha);
    if components == 0 || samples.len() != width as usize * height as usize * channels {
        return Err("the JPEG 2000 image decodes to an unexpected number of samples".to_owned());
    }
    if !alpha {
        return Ok((width, height, components, samples, None));
    }
    let pixels = width as usize * height as usize;
    let mut color = Vec::with_capacity(pixels * usize::from(components));
    let mut opacity = Vec::with_capacity(pixels);
    for pixel in samples.chunks_exact(channels) {
        let (last, rest) = pixel.split_last().expect("a pixel has channels");
        color.extend_from_slice(rest);
        opacity.push(*last);
    }
    Ok((width, height, components, color, Some(opacity)))
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
    use jpeg_encoder::{ColorType, Encoder};

    let color = match components {
        1 => ColorType::Luma,
        3 => ColorType::Rgb,
        4 => ColorType::Cmyk,
        _ => {
            return Err(PyValueError::new_err(format!(
                "no {components}-component samples in JPEG"
            )));
        }
    };
    if width == 0
        || height == 0
        || data.len() != usize::from(width) * usize::from(height) * usize::from(components)
    {
        return Err(PyValueError::new_err(format!(
            "{} bytes are not the samples of a {width} by {height} image of {components} components",
            data.len()
        )));
    }
    let out = py.detach(|| {
        // jpeg-encoder stores each CMYK sample inverted, as Adobe's
        // JPEGs do, so it is given the inverse of the sample to store.
        let inverted;
        let samples = if components == 4 {
            inverted = data.iter().map(|sample| 255 - sample).collect::<Vec<_>>();
            &inverted[..]
        } else {
            data
        };
        let mut out = Vec::new();
        Encoder::new(&mut out, quality.clamp(1, 100))
            .encode(samples, width, height, color)
            .map_err(|e| e.to_string())?;
        if components == 4 && !adobe {
            strip_app14(&mut out);
        }
        Ok::<_, String>(out)
    });
    Ok(PyBytes::new(py, &out.map_err(PyValueError::new_err)?))
}

/// Removes the Adobe APP14 segment before the first frame of JPEG `data`.
fn strip_app14(data: &mut Vec<u8>) {
    let mut at = 2;
    while at + 4 <= data.len() && data[at] == 0xFF {
        let marker = data[at + 1];
        let length = usize::from(u16::from_be_bytes([data[at + 2], data[at + 3]]));
        if marker == 0xEE && data.get(at + 4..at + 9) == Some(b"Adobe") {
            data.drain(at..at + 2 + length);
            return;
        }
        if (0xC0..=0xCF).contains(&marker) && !matches!(marker, 0xC4 | 0xC8 | 0xCC) {
            return;
        }
        at += 2 + length;
    }
}
