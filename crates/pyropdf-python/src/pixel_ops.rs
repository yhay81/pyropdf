//! The pixel operations of `Pixmap` (issue 398): new samples from old, on
//! 8-bit gray or RGB pixels with or without straight alpha, and decoding
//! PNG and JPEG data into them.

use std::io::Cursor;

use zune_jpeg::JpegDecoder;
use zune_jpeg::zune_core::bytestream::ZCursor;
use zune_jpeg::zune_core::colorspace::ColorSpace;
use zune_jpeg::zune_core::options::DecoderOptions;

/// The most pixels of a decoded image or a scaled pixmap, as rendering
/// bounds one raster.
pub const MAX_PIXELS: u64 = 64_000_000;

/// The ITU-R BT.601 luma weights, which rendering to gray uses.
const LUMA: [f64; 3] = [0.299, 0.587, 0.114];

/// The shape of a pixmap's samples.
#[derive(Clone, Copy, Debug)]
pub struct Layout {
    pub width: u32,
    pub height: u32,
    pub gray: bool,
    pub alpha: bool,
}

impl Layout {
    /// The color components of a pixel, without alpha.
    pub const fn colors(self) -> usize {
        if self.gray { 1 } else { 3 }
    }

    /// The bytes of a pixel.
    pub const fn channels(self) -> usize {
        self.colors() + self.alpha as usize
    }

    const fn stride(self) -> usize {
        self.width as usize * self.channels()
    }
}

/// A rectangle of whole pixels, `x0 <= x < x1` and `y0 <= y < y1`.
pub type Area = (u32, u32, u32, u32);

/// Returns a byte rounded from `value`, clamped to 0 through 255.
fn byte(value: f64) -> u8 {
    value.round().clamp(0.0, 255.0) as u8
}

/// Returns the luma of an RGB pixel.
pub fn luma(rgb: [u8; 3]) -> u8 {
    byte(LUMA[0] * f64::from(rgb[0]) + LUMA[1] * f64::from(rgb[1]) + LUMA[2] * f64::from(rgb[2]))
}

/// Inverts the colors of the pixels in `area`, keeping alpha.
pub fn invert(data: &[u8], layout: Layout, area: Area) -> Vec<u8> {
    let mut out = data.to_vec();
    let channels = layout.channels();
    let (x0, y0, x1, y1) = area;
    for y in y0..y1 {
        let row = y as usize * layout.stride();
        for x in x0..x1 {
            let at = row + x as usize * channels;
            for sample in &mut out[at..at + layout.colors()] {
                *sample = 255 - *sample;
            }
        }
    }
    out
}

/// Maps each color component through its table, keeping alpha; a gray
/// pixmap uses the first table.
pub fn map_colors(data: &[u8], layout: Layout, tables: &[[u8; 256]; 3]) -> Vec<u8> {
    let channels = layout.channels();
    let colors = layout.colors();
    let mut out = data.to_vec();
    for pixel in out.chunks_exact_mut(channels) {
        for (component, sample) in pixel[..colors].iter_mut().enumerate() {
            *sample = tables[component][usize::from(*sample)];
        }
    }
    out
}

/// Returns the table that raises each component, as a fraction of 255, to
/// the power `exponent`.
pub fn gamma_table(exponent: f64) -> [u8; 256] {
    std::array::from_fn(|value| byte(255.0 * (value as f64 / 255.0).powf(exponent)))
}

/// Returns the table that maps 0 to `black`, 255 to `white`, and the
/// values between linearly.
pub fn tint_table(black: u8, white: u8) -> [u8; 256] {
    let (black, white) = (f64::from(black), f64::from(white));
    std::array::from_fn(|value| byte(black + (white - black) * value as f64 / 255.0))
}

/// Returns the samples converted to gray or to RGB, keeping alpha.
pub fn convert(data: &[u8], layout: Layout, gray: bool) -> Vec<u8> {
    if layout.gray == gray {
        return data.to_vec();
    }
    let channels = layout.channels();
    let target = Layout { gray, ..layout };
    let mut out =
        Vec::with_capacity(layout.width as usize * layout.height as usize * target.channels());
    for pixel in data.chunks_exact(channels) {
        if gray {
            out.push(luma([pixel[0], pixel[1], pixel[2]]));
        } else {
            out.extend_from_slice(&[pixel[0]; 3]);
        }
        if layout.alpha {
            out.push(pixel[channels - 1]);
        }
    }
    out
}

/// Returns the colors of the pixels without alpha, in gray, RGB, or CMYK
/// by `components`. Gray and RGB are as `convert` makes them; CMYK takes
/// the gray of each pixel into black, the conversion of ISO 32000-2
/// 10.4.2.3 with full black generation and undercolor removal.
pub fn device_samples(data: &[u8], layout: Layout, components: usize) -> Vec<u8> {
    let channels = layout.channels();
    let mut out = Vec::with_capacity(layout.width as usize * layout.height as usize * components);
    for pixel in data.chunks_exact(channels) {
        let rgb = if layout.gray {
            [pixel[0]; 3]
        } else {
            [pixel[0], pixel[1], pixel[2]]
        };
        match components {
            1 => out.push(if layout.gray { pixel[0] } else { luma(rgb) }),
            3 => out.extend_from_slice(&rgb),
            _ => {
                let [c, m, y] = rgb.map(|value| 255 - value);
                let k = c.min(m).min(y);
                out.extend_from_slice(&[c - k, m - k, y - k, k]);
            }
        }
    }
    out
}

/// Returns the samples with an opaque alpha byte after each pixel.
pub fn add_alpha(data: &[u8], layout: Layout) -> Vec<u8> {
    let colors = layout.colors();
    let mut out = Vec::with_capacity(data.len() / colors * (colors + 1));
    for pixel in data.chunks_exact(colors) {
        out.extend_from_slice(pixel);
        out.push(255);
    }
    out
}

/// Returns the samples composited over `background` without alpha; a
/// gray pixmap uses the luma of `background`.
pub fn drop_alpha(data: &[u8], layout: Layout, background: [u8; 3]) -> Vec<u8> {
    let colors = layout.colors();
    let under: Vec<f64> = if layout.gray {
        vec![f64::from(luma(background))]
    } else {
        background.iter().map(|&value| f64::from(value)).collect()
    };
    let mut out = Vec::with_capacity(data.len() / (colors + 1) * colors);
    for pixel in data.chunks_exact(colors + 1) {
        let alpha = f64::from(pixel[colors]) / 255.0;
        for (component, &sample) in pixel[..colors].iter().enumerate() {
            out.push(byte(
                f64::from(sample) * alpha + under[component] * (1.0 - alpha),
            ));
        }
    }
    out
}

/// Returns the pixels of `area`.
pub fn crop(data: &[u8], layout: Layout, area: Area) -> Vec<u8> {
    let channels = layout.channels();
    let (x0, y0, x1, y1) = area;
    let row_bytes = (x1 - x0) as usize * channels;
    let mut out = Vec::with_capacity(row_bytes * (y1 - y0) as usize);
    for y in y0..y1 {
        let at = y as usize * layout.stride() + x0 as usize * channels;
        out.extend_from_slice(&data[at..at + row_bytes]);
    }
    out
}

/// The source pixels and their weights for each pixel of one axis.
struct Taps {
    starts: Vec<usize>,
    weights: Vec<Vec<f64>>,
}

/// Returns the taps of a triangle filter from `source` to `target`
/// pixels, widened by the reduction when shrinking, as Pillow's bilinear
/// resampling computes them.
fn taps(source: u32, target: u32) -> Taps {
    let scale = f64::from(source) / f64::from(target);
    let filter = scale.max(1.0);
    let support = filter;
    let mut starts = Vec::with_capacity(target as usize);
    let mut weights = Vec::with_capacity(target as usize);
    for index in 0..target {
        let centre = (f64::from(index) + 0.5) * scale;
        let first = (centre - support).floor().max(0.0) as usize;
        let last = ((centre + support).ceil() as usize).min(source as usize);
        let mut row: Vec<f64> = (first..last)
            .map(|pixel| (1.0 - ((pixel as f64 + 0.5 - centre) / filter).abs()).max(0.0))
            .collect();
        let total: f64 = row.iter().sum();
        if total > 0.0 {
            for weight in &mut row {
                *weight /= total;
            }
        }
        starts.push(first);
        weights.push(row);
    }
    Taps { starts, weights }
}

/// Resamples `width` by `height` pixels of `channels` bytes along rows,
/// to `target` pixels a row.
fn resample_rows(data: &[u8], width: u32, height: u32, channels: usize, target: u32) -> Vec<u8> {
    let taps = taps(width, target);
    let mut out = Vec::with_capacity(target as usize * height as usize * channels);
    let mut sums = vec![0.0; channels];
    for row in data.chunks_exact(width as usize * channels) {
        for (start, weights) in taps.starts.iter().zip(&taps.weights) {
            sums.fill(0.0);
            for (offset, weight) in weights.iter().enumerate() {
                let at = (start + offset) * channels;
                for (sum, &sample) in sums.iter_mut().zip(&row[at..at + channels]) {
                    *sum += weight * f64::from(sample);
                }
            }
            out.extend(sums.iter().map(|&sum| byte(sum)));
        }
    }
    out
}

/// Resamples `width` by `height` pixels of `channels` bytes along
/// columns, to `target` rows.
fn resample_columns(data: &[u8], width: u32, height: u32, channels: usize, target: u32) -> Vec<u8> {
    let taps = taps(height, target);
    let stride = width as usize * channels;
    let mut out = Vec::with_capacity(stride * target as usize);
    let mut sums = vec![0.0; stride];
    for (start, weights) in taps.starts.iter().zip(&taps.weights) {
        sums.fill(0.0);
        for (offset, weight) in weights.iter().enumerate() {
            let row = &data[(start + offset) * stride..][..stride];
            for (sum, &sample) in sums.iter_mut().zip(row) {
                *sum += weight * f64::from(sample);
            }
        }
        out.extend(sums.iter().map(|&sum| byte(sum)));
    }
    out
}

/// Returns the pixels resampled to `width` by `height` with a bilinear
/// filter, through premultiplied alpha, along the axis that keeps the
/// intermediate raster smaller first.
pub fn scale(data: &[u8], layout: Layout, width: u32, height: u32) -> Vec<u8> {
    let channels = layout.channels();
    let colors = layout.colors();
    let mut samples = data.to_vec();
    if layout.alpha {
        for pixel in samples.chunks_exact_mut(channels) {
            let alpha = f64::from(pixel[colors]);
            for sample in &mut pixel[..colors] {
                *sample = byte(f64::from(*sample) * alpha / 255.0);
            }
        }
    }
    let rows_first =
        u64::from(width) * u64::from(layout.height) <= u64::from(layout.width) * u64::from(height);
    let mut out = if rows_first {
        let rows = resample_rows(&samples, layout.width, layout.height, channels, width);
        resample_columns(&rows, width, layout.height, channels, height)
    } else {
        let columns = resample_columns(&samples, layout.width, layout.height, channels, height);
        resample_rows(&columns, layout.width, height, channels, width)
    };
    if layout.alpha {
        for pixel in out.chunks_exact_mut(channels) {
            let alpha = f64::from(pixel[colors]);
            for sample in &mut pixel[..colors] {
                *sample = if alpha == 0.0 {
                    0
                } else {
                    byte(f64::from(*sample) * 255.0 / alpha)
                };
            }
        }
    }
    out
}

/// A decoded image: its layout, samples, and resolution in dots per inch.
pub struct Decoded {
    pub layout: Layout,
    pub data: Vec<u8>,
    pub resolution: (u32, u32),
}

/// Decodes PNG or JPEG `data`, refusing more than [`MAX_PIXELS`] pixels.
pub fn decode(data: &[u8]) -> Result<Decoded, String> {
    if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        decode_png(data)
    } else if data.starts_with(b"\xff\xd8") {
        decode_jpeg(data)
    } else {
        Err("the data is neither PNG nor JPEG".to_owned())
    }
}

fn check_size(width: u32, height: u32) -> Result<(), String> {
    let pixels = u64::from(width) * u64::from(height);
    if pixels == 0 || pixels > MAX_PIXELS {
        return Err(format!(
            "the image has {pixels} pixels; a pixmap holds 1 to {MAX_PIXELS}"
        ));
    }
    Ok(())
}

fn decode_png(data: &[u8]) -> Result<Decoded, String> {
    let invalid = |error: png::DecodingError| format!("not a PNG image: {error}");
    let mut decoder = png::Decoder::new(Cursor::new(data));
    decoder.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
    let header = decoder.read_header_info().map_err(invalid)?;
    check_size(header.width, header.height)?;
    let resolution = header
        .pixel_dims
        .filter(|dims| dims.unit == png::Unit::Meter)
        .map_or((96, 96), |dims| {
            let dpi = |per_metre: u32| ((f64::from(per_metre) * 0.0254).round() as u32).max(1);
            (dpi(dims.xppu), dpi(dims.yppu))
        });
    let mut reader = decoder.read_info().map_err(invalid)?;
    let size = reader
        .output_buffer_size()
        .ok_or_else(|| "the PNG image is too large".to_owned())?;
    let mut buffer = vec![0; size];
    let info = reader.next_frame(&mut buffer).map_err(invalid)?;
    buffer.truncate(info.buffer_size());
    let (gray, alpha) = match info.color_type {
        png::ColorType::Grayscale => (true, false),
        png::ColorType::GrayscaleAlpha => (true, true),
        png::ColorType::Rgb | png::ColorType::Indexed => (false, false),
        png::ColorType::Rgba => (false, true),
    };
    let layout = Layout {
        width: info.width,
        height: info.height,
        gray,
        alpha,
    };
    Ok(Decoded {
        layout,
        data: buffer,
        resolution,
    })
}

fn decode_jpeg(data: &[u8]) -> Result<Decoded, String> {
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
    let gray = matches!(
        decoder.input_colorspace(),
        Some(ColorSpace::Luma | ColorSpace::LumaA)
    );
    let out = if gray {
        ColorSpace::Luma
    } else {
        ColorSpace::RGB
    };
    decoder.set_options(options.jpeg_set_out_colorspace(out));
    let samples = decoder.decode().map_err(invalid)?;
    let layout = Layout {
        width: width as u32,
        height: height as u32,
        gray,
        alpha: false,
    };
    if samples.len() != layout.stride() * height {
        return Err("the JPEG image decoded to the wrong number of samples".to_owned());
    }
    Ok(Decoded {
        layout,
        data: samples,
        resolution: (96, 96),
    })
}
