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
    let total = color
        .len()
        .checked_add(alpha.len())
        .ok_or_else(|| "cannot hold the image: byte length overflow".to_owned())?;
    let mut data = Vec::new();
    data.try_reserve_exact(total)
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
    buffer.truncate(line.saturating_mul(height as usize));
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
pub fn image_samples(data: &[u8], jpx: bool, budget: &Budget) -> Result<RawSamples, DecodeError> {
    budget.poll()?;
    if jpx {
        jpx_samples(data, budget)
    } else {
        jpeg_samples(data, budget)
    }
}

fn jpeg_samples(data: &[u8], budget: &Budget) -> Result<RawSamples, DecodeError> {
    use zune_jpeg::JpegDecoder;
    use zune_jpeg::zune_core::bytestream::ZCursor;
    use zune_jpeg::zune_core::colorspace::ColorSpace;
    use zune_jpeg::zune_core::options::DecoderOptions;

    budget.poll()?;
    let invalid = |error: zune_jpeg::errors::DecodeErrors| {
        DecodeError::Invalid(format!("not a JPEG image: {error}"))
    };
    let options = DecoderOptions::default()
        .set_max_width(usize::from(u16::MAX))
        .set_max_height(usize::from(u16::MAX));
    let mut decoder = JpegDecoder::new_with_options(ZCursor::new(data), options);
    decoder.decode_headers().map_err(invalid)?;
    let (width, height) = decoder
        .dimensions()
        .ok_or_else(|| DecodeError::Invalid("the JPEG image has no size".to_owned()))?;
    check_size(width as u32, height as u32)?;
    let three = decoder.info().is_some_and(|info| info.components == 3);
    let (out, components) = match decoder.input_colorspace() {
        Some(ColorSpace::Luma | ColorSpace::LumaA) => (ColorSpace::Luma, 1),
        Some(ColorSpace::CMYK) if !three => (ColorSpace::CMYK, 4),
        Some(ColorSpace::YCCK) => (ColorSpace::YCCK, 4),
        _ => (ColorSpace::RGB, 3),
    };
    let expected = u64::from(width as u32) * u64::from(height as u32) * u64::from(components);
    budget.charge(LimitCode::TotalDecompressedSize, expected)?;
    budget.charge(LimitCode::MemorySize, expected)?;
    decoder.set_options(options.jpeg_set_out_colorspace(out));
    let decoded = (|| -> Result<Vec<u8>, DecodeError> {
        let mut samples = decoder.decode().map_err(invalid)?;
        if out == ColorSpace::YCCK {
            // As hayro-syntax, after pdf.js's jpg.js.
            for (index, pixel) in samples.as_chunks_mut::<4>().0.iter_mut().enumerate() {
                if index.is_multiple_of(16_384) {
                    budget.poll()?;
                }
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
        if samples.len() as u64 != expected {
            return Err(DecodeError::Invalid(
                "the JPEG image decoded to the wrong number of samples".to_owned(),
            ));
        }
        Ok(samples)
    })();
    match decoded {
        Ok(samples) => Ok((width as u32, height as u32, components, samples, None)),
        Err(error) => {
            budget.release(LimitCode::MemorySize, expected);
            Err(error)
        }
    }
}

fn jpx_samples(data: &[u8], budget: &Budget) -> Result<RawSamples, DecodeError> {
    use hayro_jpeg2000::{DecodeSettings, DecoderContext, Image};

    budget.poll()?;
    let invalid = |error| {
        DecodeError::Invalid(format!(
            "not a JPEG 2000 image the decoder reads: {error:?}"
        ))
    };
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
    let channels = u64::from(components) + u64::from(alpha);
    let unexpected = || {
        DecodeError::Invalid(
            "the JPEG 2000 image decodes to an unexpected number of samples".to_owned(),
        )
    };
    let expected = (u64::from(width) * u64::from(height))
        .checked_mul(channels)
        .ok_or_else(unexpected)?;
    if components == 0 {
        return Err(unexpected());
    }
    budget.charge(LimitCode::TotalDecompressedSize, expected)?;
    budget.charge(LimitCode::MemorySize, expected)?;
    let decoded = (|| -> Result<RawSamples, DecodeError> {
        let mut context = DecoderContext::default();
        let samples = image.decode(&mut context).map_err(invalid)?.data_u8();
        budget.poll()?;
        if samples.len() as u64 != expected {
            return Err(unexpected());
        }
        if !alpha {
            return Ok((width, height, components, samples, None));
        }
        let channels = usize::try_from(channels).map_err(|_| unexpected())?;
        let pixels = usize::try_from(width)
            .ok()
            .and_then(|w| usize::try_from(height).ok().and_then(|h| w.checked_mul(h)))
            .ok_or_else(unexpected)?;
        let color_len = pixels
            .checked_mul(usize::from(components))
            .ok_or_else(unexpected)?;
        let mut color = BudgetVec::new(budget);
        let mut opacity = BudgetVec::new(budget);
        color.try_reserve_exact(color_len)?;
        opacity.try_reserve_exact(pixels)?;
        for chunk in samples.chunks(channels * 4096) {
            budget.poll()?;
            for pixel in chunk.chunks_exact(channels) {
                let (last, rest) = pixel.split_last().expect("a pixel has channels");
                color.try_extend_from_slice(rest)?;
                opacity.try_push(*last)?;
            }
        }
        drop(samples);
        Ok((
            width,
            height,
            components,
            color.into_vec(),
            Some(opacity.into_vec()),
        ))
    })();
    match decoded {
        Ok((width, height, components, color, opacity)) => {
            if opacity.is_some() {
                budget.release(LimitCode::MemorySize, expected);
            }
            Ok((width, height, components, color, opacity))
        }
        Err(error) => {
            budget.release(LimitCode::MemorySize, expected);
            Err(error)
        }
    }
}

/// Encodes interleaved 8-bit samples of one, three, or four components as
/// JPEG data that decodes to them: four components as CMYK with an Adobe
/// APP14 marker of transform 0 when `adobe`, and without one otherwise.
pub fn encode_jpeg_samples(
    data: &[u8],
    width: u16,
    height: u16,
    components: u8,
    quality: u8,
    adobe: bool,
    budget: &Budget,
) -> Result<Vec<u8>, super::EncodeError> {
    use jpeg_encoder::{ColorType, Encoder};

    budget.poll().map_err(super::EncodeError::Limit)?;
    let color = match components {
        1 => ColorType::Luma,
        3 => ColorType::Rgb,
        4 => ColorType::Cmyk,
        _ => {
            return Err(super::EncodeError::Other(format!(
                "no {components}-component samples in JPEG"
            )));
        }
    };
    let expected = u64::from(width) * u64::from(height) * u64::from(components);
    if width == 0 || height == 0 || data.len() as u64 != expected {
        return Err(super::EncodeError::Other(format!(
            "{} bytes are not the samples of a {width} by {height} image of {components} components",
            data.len()
        )));
    }
    // jpeg-encoder stores each CMYK sample inverted, as Adobe's
    // JPEGs do, so it is given the inverse of the sample to store.
    let mut inverted = BudgetVec::new(budget);
    let samples = if components == 4 {
        inverted
            .try_reserve_exact(data.len())
            .map_err(super::EncodeError::Limit)?;
        for (index, &sample) in data.iter().enumerate() {
            if index % 65_536 == 0 {
                budget.poll().map_err(super::EncodeError::Limit)?;
            }
            inverted
                .try_push(255 - sample)
                .map_err(super::EncodeError::Limit)?;
        }
        inverted.as_slice()
    } else {
        data
    };
    let mut out = super::BudgetedPngOutput::new(budget);
    if let Err(error) =
        Encoder::new(&mut out, quality.clamp(1, 100)).encode(samples, width, height, color)
    {
        if let Some(refusal) = out.take_refusal() {
            return Err(super::EncodeError::Limit(refusal));
        }
        return Err(super::EncodeError::Other(error.to_string()));
    }
    let mut out = out.into_vec();
    if components == 4 && !adobe {
        strip_app14(&mut out);
    }
    Ok(out)
}

/// Removes the Adobe APP14 segment before the first frame of JPEG `data`.
fn strip_app14(data: &mut Vec<u8>) {
    let mut at = 2_usize;
    while at
        .checked_add(4)
        .is_some_and(|header_end| header_end <= data.len())
        && data[at] == 0xFF
    {
        let marker = data[at + 1];
        let length = usize::from(u16::from_be_bytes([data[at + 2], data[at + 3]]));
        let Some(end) = at
            .checked_add(2)
            .and_then(|start| start.checked_add(length))
            .filter(|&end| length >= 2 && end <= data.len())
        else {
            return;
        };
        if marker == 0xEE && data.get(at + 4..at + 9) == Some(b"Adobe") {
            data.drain(at..end);
            return;
        }
        if (0xC0..=0xCF).contains(&marker) && !matches!(marker, 0xC4 | 0xC8 | 0xCC) {
            return;
        }
        at = end;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encode_jpeg_samples_checks_expected_length_in_u64_without_32_bit_overflow() {
        // 65535 * 65535 * 4 = 17_179_344_900 > u32::MAX; must return an error
        // rather than overflowing 32-bit usize multiplication.
        let err = encode_jpeg_samples(&[], u16::MAX, u16::MAX, 4, 90, true, &Budget::default())
            .unwrap_err();
        assert!(matches!(err, super::super::EncodeError::Other(_)));
    }

    #[test]
    fn strip_app14_handles_truncated_and_zero_length_segments_without_panicking() {
        // Truncated APP14 segment whose declared length passes EOF.
        let mut truncated = vec![
            0xff, 0xd8, 0xff, 0xee, 0x00, 0x20, b'A', b'd', b'o', b'b', b'e',
        ];
        let before = truncated.clone();
        strip_app14(&mut truncated);
        assert_eq!(truncated, before);

        // Zero-length segment would loop or underflow if not checked for length >= 2.
        let mut zero_len = vec![0xff, 0xd8, 0xff, 0xe0, 0x00, 0x00, 0xff, 0xd9];
        let before = zero_len.clone();
        strip_app14(&mut zero_len);
        assert_eq!(zero_len, before);

        // Well-formed APP14 segment is stripped cleanly.
        let mut valid = vec![
            0xff, 0xd8, 0xff, 0xee, 0x00, 0x0e, b'A', b'd', b'o', b'b', b'e', 0, 100, 0, 0, 0, 0,
            0, 0xff, 0xd9,
        ];
        strip_app14(&mut valid);
        assert_eq!(valid, vec![0xff, 0xd8, 0xff, 0xd9]);
    }
}
