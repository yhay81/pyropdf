//! PDF image stream peeling, alpha interleaving, budgeted PNG splitting,
//! raw DCT/JPX sample extraction, and JPEG sample encoding.

use std::io::Cursor;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::value::{Array, Bytes, Stream, Value};
use pyropdf_store::version::Version;

use crate::extract::DecodedImage;

use super::{DecodeError, Layout, MAX_PIXELS, check_size};

/// The image filters whose encoded data an image keeps as stored
/// (ISO 32000-2 table 6).
const IMAGE_FILTERS: [&[u8]; 4] = [
    b"DCTDecode",
    b"JPXDecode",
    b"JBIG2Decode",
    b"CCITTFaxDecode",
];

/// Interleaves the optional alpha buffer of `image` after each pixel,
/// returning its [`Layout`] and sample buffer.
#[expect(
    clippy::disallowed_methods,
    reason = "one interleaved output sample per byte of the engine's budgeted color and alpha buffers"
)]
pub fn interleave_image(image: DecodedImage) -> Result<(Layout, Vec<u8>), String> {
    let DecodedImage {
        width,
        height,
        gray,
        color,
        alpha,
    } = image;
    let Some(alpha) = alpha else {
        return Ok((
            Layout {
                width,
                height,
                gray,
                alpha: false,
            },
            color,
        ));
    };
    let channels = if gray { 1 } else { 3 };
    let mut data = Vec::new();
    data.try_reserve_exact(color.len() + alpha.len())
        .map_err(|error| format!("cannot hold the image: {error}"))?;
    for (pixel, opacity) in color.chunks_exact(channels).zip(alpha) {
        data.extend_from_slice(pixel);
        data.push(opacity);
    }
    Ok((
        Layout {
            width,
            height,
            gray,
            alpha: true,
        },
        data,
    ))
}

/// Stored encoded image bytes, either shared directly from the stream or
/// decoded through its leading non-image filters under a [`Budget`].
pub enum StoredBytes {
    Raw(Bytes),
    Decoded(Vec<u8>),
}

impl AsRef<[u8]> for StoredBytes {
    fn as_ref(&self) -> &[u8] {
        match self {
            Self::Raw(bytes) => bytes.as_ref(),
            Self::Decoded(bytes) => bytes.as_slice(),
        }
    }
}

/// Returns the image filter and the data as stored of the image stream
/// `root`, its other filters decoded within `budget`; `None` when the last
/// filter is not an image filter or the object is not a stream.
#[expect(
    clippy::disallowed_methods,
    reason = "filter names and decode parameters copy only entries of the budget-parsed image dictionary"
)]
pub fn stored_image(
    version: &Version,
    root: (u32, u16),
    budget: &Budget,
) -> pyropdf_store::Result<Option<(String, StoredBytes)>> {
    let (number, generation) = root;
    let Some(object) = version
        .get(number, budget)?
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
        return Ok(Some((filter, StoredBytes::Raw(stream.data().clone()))));
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
    let data = Stream::new(dict, stream.data().clone()).decoded(budget)?;
    Ok(Some((filter, StoredBytes::Decoded(data))))
}

/// Width, height, whether gray, color samples, and separate alpha samples.
pub type DecodedPngBuffers<'b> = (u32, u32, bool, BudgetVec<'b, u8>, Option<BudgetVec<'b, u8>>);

/// Decodes PNG `data` to 8-bit color and optional alpha buffers under
/// `budget`, charging [`LimitCode::TotalDecompressedSize`] and polling
/// between scanlines and split chunks.
pub fn decode_png_buffers<'b>(
    data: &[u8],
    budget: &'b Budget,
) -> Result<DecodedPngBuffers<'b>, DecodeError> {
    let invalid =
        |error: png::DecodingError| DecodeError::Invalid(format!("not a PNG image: {error}"));
    let mut decoder = png::Decoder::new(Cursor::new(data));
    decoder.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
    let header = decoder.read_header_info().map_err(invalid)?;
    let pixels = u64::from(header.width) * u64::from(header.height);
    if pixels > MAX_PIXELS {
        return Err(DecodeError::Invalid(format!(
            "the PNG image has {pixels} pixels, more than the {MAX_PIXELS}-pixel limit"
        )));
    }
    budget.charge(LimitCode::TotalDecompressedSize, pixels * 4)?;
    let mut reader = decoder.read_info().map_err(invalid)?;
    let size = reader
        .output_buffer_size()
        .ok_or_else(|| DecodeError::Invalid("the PNG image is too large".to_owned()))?;
    let mut buffer = BudgetVec::new(budget);
    buffer.try_reserve_exact(size)?;
    // Initialize in small pieces so a cancelled operation does not have to
    // finish touching the entire decoded frame before noticing cancellation.
    const ZEROES: [u8; 16 * 1024] = [0; 16 * 1024];
    while buffer.len() < size {
        budget.poll()?;
        let len = (size - buffer.len()).min(ZEROES.len());
        buffer.try_extend_from_slice(&ZEROES[..len])?;
    }
    // Read row by row, polling between rows, rather than with
    // `next_frame`, which reads the whole frame without polling.
    let (width, height) = (reader.info().width, reader.info().height);
    let (color_type, _) = reader.output_color_type();
    let line = reader
        .output_line_size(width)
        .ok_or_else(|| DecodeError::Invalid("the PNG image is too large".to_owned()))?;
    let mut row = BudgetVec::new(budget);
    row.try_extend(std::iter::repeat_n(0u8, line))?;
    // After `EXPAND` and `STRIP_16`, every sample has 8 bits.
    let bits = u8::try_from(color_type.samples() * 8).unwrap_or(u8::MAX);
    let mut next = 0;
    while let Some(interlace) = reader.read_row(row.as_mut_slice()).map_err(invalid)? {
        budget.poll()?;
        match interlace {
            png::InterlaceInfo::Null(_) => {
                let start = next * line;
                if let Some(target) = buffer.as_mut_slice().get_mut(start..start + line) {
                    target.copy_from_slice(row.as_slice());
                }
                next += 1;
            }
            png::InterlaceInfo::Adam7(info) => {
                png::expand_interlaced_row(
                    buffer.as_mut_slice(),
                    line,
                    row.as_slice(),
                    &info,
                    bits,
                );
            }
        }
    }
    buffer.truncate(line * height as usize);
    let (gray, alpha) = match color_type {
        png::ColorType::Grayscale => (true, false),
        png::ColorType::GrayscaleAlpha => (true, true),
        png::ColorType::Rgb | png::ColorType::Indexed => (false, false),
        png::ColorType::Rgba => (false, true),
    };
    if !alpha {
        return Ok((width, height, gray, buffer, None));
    }
    let channels = if gray { 1 } else { 3 };
    let pixels = buffer.len() / (channels + 1);
    let mut color = BudgetVec::new(budget);
    let mut opacity = BudgetVec::new(budget);
    color.try_reserve_exact(pixels * channels)?;
    opacity.try_reserve_exact(pixels)?;
    for chunk in buffer.chunks((channels + 1) * 4096) {
        budget.poll()?;
        for pixel in chunk.chunks_exact(channels + 1) {
            color.try_extend_from_slice(&pixel[..channels])?;
            opacity.try_push(pixel[channels])?;
        }
    }
    Ok((width, height, gray, color, Some(opacity)))
}

/// Width, height, color components, 8-bit samples, and optional alpha.
pub type RawSamples = (u32, u32, u8, Vec<u8>, Option<Vec<u8>>);

/// Decodes JPEG (or, with `jpx`, JPEG 2000) `data` to the samples that
/// hayro-syntax's `DCTDecode` and `JPXDecode` filters give its color space,
/// without converting them to RGB: CMYK as stored, YCCK as pdf.js turns it
/// into CMYK, and JPEG 2000's palette indexes kept.
pub fn image_samples(data: &[u8], jpx: bool) -> Result<RawSamples, String> {
    if jpx {
        jpx_samples(data)
    } else {
        jpeg_samples(data)
    }
}

fn jpeg_samples(data: &[u8]) -> Result<RawSamples, String> {
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
    check_size(width as u32, height as u32)?;
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

#[expect(
    clippy::disallowed_methods,
    reason = "check_size bounds the raster to MAX_PIXELS; the output copies the decoded samples one-for-one with the u8 component count"
)]
fn jpx_samples(data: &[u8]) -> Result<RawSamples, String> {
    use hayro_jpeg2000::{DecodeSettings, DecoderContext, Image};

    let invalid = |error| format!("not a JPEG 2000 image the decoder reads: {error:?}");
    let settings = DecodeSettings {
        resolve_palette_indices: false,
        strict: false,
        target_resolution: None,
    };
    let image = Image::new(data, &settings).map_err(invalid)?;
    let (width, height) = (image.width(), image.height());
    check_size(width, height)?;
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
#[expect(
    clippy::disallowed_methods,
    reason = "the borrowed input is checked once against the nonzero u16 dimensions and 1, 3, or 4 components before its sole inverted copy"
)]
pub fn encode_jpeg_samples(
    data: &[u8],
    width: u16,
    height: u16,
    components: u8,
    quality: u8,
    adobe: bool,
) -> Result<Vec<u8>, String> {
    use jpeg_encoder::{ColorType, Encoder};

    let color = match components {
        1 => ColorType::Luma,
        3 => ColorType::Rgb,
        4 => ColorType::Cmyk,
        _ => return Err(format!("no {components}-component samples in JPEG")),
    };
    if width == 0
        || height == 0
        || data.len() != usize::from(width) * usize::from(height) * usize::from(components)
    {
        return Err(format!(
            "{} bytes are not the samples of a {width} by {height} image of {components} components",
            data.len()
        ));
    }
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
    Ok(out)
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
