//! The pixel operations of `Pixmap` (issue 398): new samples from old, on
//! 8-bit gray or RGB pixels with or without straight alpha, and decoding
//! PNG and JPEG data into them, and through `codecs`, portable anymap, PAM,
//! and BMP data.

use std::io::Cursor;

use zune_jpeg::JpegDecoder;
use zune_jpeg::zune_core::bytestream::ZCursor;
use zune_jpeg::zune_core::colorspace::ColorSpace;
use zune_jpeg::zune_core::options::DecoderOptions;

use crate::codecs;

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

/// Returns the samples of `layout` turned clockwise by `quarters` quarter
/// turns, 1 through 3, whose layout swaps width and height for an odd
/// count.
pub fn rotate(data: &[u8], layout: Layout, quarters: u8) -> Vec<u8> {
    let (width, height) = (layout.width as usize, layout.height as usize);
    let channels = layout.channels();
    let mut out = Vec::with_capacity(data.len());
    let (out_width, out_height) = if quarters % 2 == 1 {
        (height, width)
    } else {
        (width, height)
    };
    for y in 0..out_height {
        for x in 0..out_width {
            let (from_x, from_y) = match quarters {
                1 => (y, height - 1 - x),
                2 => (width - 1 - x, height - 1 - y),
                _ => (width - 1 - y, x),
            };
            let at = (from_y * width + from_x) * channels;
            out.extend_from_slice(&data[at..at + channels]);
        }
    }
    out
}

/// Returns the samples mirrored left to right, or top to bottom when
/// `vertical`.
pub fn flip(data: &[u8], layout: Layout, vertical: bool) -> Vec<u8> {
    let stride = layout.stride();
    let channels = layout.channels();
    let mut out = Vec::with_capacity(data.len());
    for row in 0..layout.height as usize {
        let source = if vertical {
            layout.height as usize - 1 - row
        } else {
            row
        };
        let line = &data[source * stride..(source + 1) * stride];
        if vertical {
            out.extend_from_slice(line);
        } else {
            for pixel in line.chunks_exact(channels).rev() {
                out.extend_from_slice(pixel);
            }
        }
    }
    out
}

/// A projective map from the unit square onto a quadrilateral: `(u, v)`
/// goes to `((a u + b v + c) / w, (d u + e v + f) / w)` with
/// `w = g u + h v + 1`.
#[derive(Clone, Copy, Debug)]
pub struct Projection([f64; 8]);

impl Projection {
    /// Returns the map that sends the unit square's corners `(0, 0)`,
    /// `(1, 0)`, `(0, 1)`, and `(1, 1)` to `ul`, `ur`, `ll`, and `lr`, as
    /// Heckbert's square-to-quadrilateral mapping computes it; `None` for a
    /// quadrilateral with three corners on a line, or that is not convex.
    pub fn new(quad: [(f64, f64); 4]) -> Option<Self> {
        let [(x0, y0), (x1, y1), (x3, y3), (x2, y2)] = quad;
        if !quad.iter().all(|(x, y)| x.is_finite() && y.is_finite()) {
            return None;
        }
        // Each corner turns the same way, around the corners in order.
        let corners = [(x0, y0), (x1, y1), (x2, y2), (x3, y3)];
        let turns: [f64; 4] = std::array::from_fn(|at| {
            let (ax, ay) = corners[at];
            let (bx, by) = corners[(at + 1) % 4];
            let (cx, cy) = corners[(at + 2) % 4];
            (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        });
        if !(turns.iter().all(|turn| *turn > 0.0) || turns.iter().all(|turn| *turn < 0.0)) {
            return None;
        }
        let (dx1, dx2, dx3) = (x1 - x2, x3 - x2, x0 - x1 + x2 - x3);
        let (dy1, dy2, dy3) = (y1 - y2, y3 - y2, y0 - y1 + y2 - y3);
        let (g, h) = if dx3 == 0.0 && dy3 == 0.0 {
            (0.0, 0.0)
        } else {
            let denominator = dx1 * dy2 - dx2 * dy1;
            (
                (dx3 * dy2 - dx2 * dy3) / denominator,
                (dx1 * dy3 - dx3 * dy1) / denominator,
            )
        };
        Some(Self([
            x1 - x0 + g * x1,
            x3 - x0 + h * x3,
            x0,
            y1 - y0 + g * y1,
            y3 - y0 + h * y3,
            y0,
            g,
            h,
        ]))
    }

    fn apply(self, u: f64, v: f64) -> (f64, f64) {
        let [a, b, c, d, e, f, g, h] = self.0;
        let w = g * u + h * v + 1.0;
        ((a * u + b * v + c) / w, (d * u + e * v + f) / w)
    }
}

/// Returns `width` by `height` pixels whose corners are those of the
/// quadrilateral that `projection` maps the unit square onto: each pixel's
/// center maps into the source, which is sampled bilinearly between pixel
/// centers, with alpha weighting the colors, and clamped to its edge
/// pixels.
pub fn warp(
    data: &[u8],
    layout: Layout,
    projection: Projection,
    width: u32,
    height: u32,
) -> Vec<u8> {
    let channels = layout.channels();
    let colors = layout.colors();
    let (source_width, source_height) = (layout.width as usize, layout.height as usize);
    let mut out = Vec::with_capacity(width as usize * height as usize * channels);
    let sample = |x: usize, y: usize| &data[(y * source_width + x) * channels..][..channels];
    for row in 0..height {
        for column in 0..width {
            let u = (f64::from(column) + 0.5) / f64::from(width);
            let v = (f64::from(row) + 0.5) / f64::from(height);
            let (x, y) = projection.apply(u, v);
            let at = |value: f64, length: usize| {
                let value = (value - 0.5).clamp(0.0, (length - 1) as f64);
                let low = value.floor();
                (
                    low as usize,
                    ((low as usize) + 1).min(length - 1),
                    value - low,
                )
            };
            let (x0, x1, fx) = at(x, source_width);
            let (y0, y1, fy) = at(y, source_height);
            let (mut mixed, mut plain) = ([0.0; 4], [0.0; 3]);
            for (px, py, weight) in [
                (x0, y0, (1.0 - fx) * (1.0 - fy)),
                (x1, y0, fx * (1.0 - fy)),
                (x0, y1, (1.0 - fx) * fy),
                (x1, y1, fx * fy),
            ] {
                let pixel = sample(px, py);
                let alpha = if layout.alpha {
                    f64::from(pixel[colors]) / 255.0
                } else {
                    1.0
                };
                for component in 0..colors {
                    mixed[component] += weight * alpha * f64::from(pixel[component]);
                    plain[component] += weight * f64::from(pixel[component]);
                }
                if layout.alpha {
                    mixed[colors] += weight * f64::from(pixel[colors]);
                }
            }
            let alpha = if layout.alpha {
                mixed[colors] / 255.0
            } else {
                1.0
            };
            // Where every sample is transparent, their colors mix unweighted.
            for component in 0..colors {
                out.push(if alpha == 0.0 {
                    byte(plain[component])
                } else {
                    byte(mixed[component] / alpha)
                });
            }
            if layout.alpha {
                out.push(byte(mixed[colors]));
            }
        }
    }
    out
}

/// Returns the pixels of `area` as big-endian keys of their channels,
/// sorted, so that equal pixels lie together.
fn sorted_pixels(data: &[u8], layout: Layout, area: Area) -> Vec<u32> {
    let channels = layout.channels();
    let (x0, y0, x1, y1) = area;
    let mut keys = Vec::with_capacity((x1 - x0) as usize * (y1 - y0) as usize);
    for y in y0..y1 {
        let at = y as usize * layout.stride() + x0 as usize * channels;
        let row = &data[at..at + (x1 - x0) as usize * channels];
        keys.extend(row.chunks_exact(channels).map(|pixel| {
            pixel
                .iter()
                .fold(0_u32, |key, &byte| key << 8 | u32::from(byte))
        }));
    }
    keys.sort_unstable();
    keys
}

/// Calls `each` with every distinct pixel of `area`, as its channels, and
/// its count, in ascending order of the channels, until it returns `false`.
pub fn color_runs(
    data: &[u8],
    layout: Layout,
    area: Area,
    mut each: impl FnMut(Vec<u8>, u64) -> bool,
) {
    let channels = layout.channels();
    let keys = sorted_pixels(data, layout, area);
    let mut at = 0;
    while at < keys.len() {
        let key = keys[at];
        let end = at + keys[at..].partition_point(|&other| other == key);
        let pixel = key.to_be_bytes()[4 - channels..].to_vec();
        if !each(pixel, (end - at) as u64) {
            return;
        }
        at = end;
    }
}

/// Returns the samples with every pixel of `area` set to `pixel`, one byte
/// per channel.
pub fn fill(data: &[u8], layout: Layout, area: Area, pixel: &[u8]) -> Vec<u8> {
    let mut out = data.to_vec();
    let channels = layout.channels();
    let (x0, y0, x1, y1) = area;
    for y in y0..y1 {
        let at = y as usize * layout.stride() + x0 as usize * channels;
        for target in out[at..at + (x1 - x0) as usize * channels].chunks_exact_mut(channels) {
            target.copy_from_slice(pixel);
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

/// Decodes PNG, JPEG, portable anymap, PAM, or BMP `data`, refusing more
/// than [`MAX_PIXELS`] pixels.
pub fn decode(data: &[u8]) -> Result<Decoded, String> {
    if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        decode_png(data)
    } else if data.starts_with(b"\xff\xd8") {
        decode_jpeg(data)
    } else if codecs::is_pnm(data) {
        codecs::decode_pnm(data)
    } else if data.starts_with(b"BM") {
        codecs::decode_bmp(data)
    } else {
        Err("the data is not PNG, JPEG, a portable anymap, PAM, or BMP".to_owned())
    }
}

pub fn check_size(width: u32, height: u32) -> Result<(), String> {
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
    let mut reader = decoder.read_info().map_err(invalid)?;
    // pHYs follows the header, so only the full info holds it.
    let resolution = reader
        .info()
        .pixel_dims
        .filter(|dims| dims.unit == png::Unit::Meter)
        .map_or((96, 96), |dims| {
            let dpi = |per_metre: u32| ((f64::from(per_metre) * 0.0254).round() as u32).max(1);
            (dpi(dims.xppu), dpi(dims.yppu))
        });
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
