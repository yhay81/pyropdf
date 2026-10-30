//! `Pixmap`, pylopdf's immutable raster with the buffer protocol and PNG
//! and JPEG output (pylopdf rust/src/pixmap.rs at 884af0db and 036be111),
//! the formats of `codecs` beyond them,
//! and `page.pixmap` and `doc.render` through the hayro bridge, with the
//! GIL released while rendering.

use std::ffi::{CString, c_int, c_void};
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufWriter, Write};
use std::path::{Path, PathBuf};
use std::ptr;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use jpeg_encoder::{ColorType, Encoder as JpegEncoder, PixelDensity, PixelDensityUnit};
use pyo3::buffer::PyBuffer;
use pyo3::exceptions::{PyBufferError, PyImportError, PyIndexError, PyTypeError, PyValueError};
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyInt, PyTuple};
use pyropdf_engine::bridge::Error as BridgeError;
use pyropdf_engine::ocr;
use pyropdf_engine::render::{self, Raster, RasterError};
use pyropdf_store::value::Reference;

use crate::codecs;
use crate::document::_Document;
use crate::errors::{EncryptedDocumentError, LimitError, PdfError, StaleObjectError, store_error};
use crate::pixel_ops::{self, Area, Layout};

/// The most distinct colors that `Pixmap.colors` returns by default.
const DEFAULT_MAX_COLORS: u64 = 65_536;

/// Metres per inch, for PNG's pixels per metre.
const METRES_PER_INCH: f64 = 0.0254;

/// A rendered page: immutable pixels, row by row from the top, RGB or gray,
/// each with or without straight alpha, 8 bits per channel.
#[pyclass(frozen, module = "pyropdf._core")]
pub struct Pixmap {
    width: u32,
    height: u32,
    alpha: bool,
    gray: bool,
    data: Arc<Vec<u8>>,
    resolution: (u32, u32),
}

impl Pixmap {
    /// Returns the pixmap of `data`, `width` by `height` pixels of gray or
    /// RGB, each followed by an alpha byte when `alpha`, at 96 dpi.
    pub(crate) fn from_samples(
        width: u32,
        height: u32,
        gray: bool,
        alpha: bool,
        data: Vec<u8>,
    ) -> Self {
        Self {
            width,
            height,
            alpha,
            gray,
            data: Arc::new(data),
            resolution: (96, 96),
        }
    }

    fn channels(&self) -> u32 {
        u32::from(!self.gray) * 2 + 1 + u32::from(self.alpha)
    }

    const fn layout(&self) -> Layout {
        Layout {
            width: self.width,
            height: self.height,
            gray: self.gray,
            alpha: self.alpha,
        }
    }

    /// Returns a pixmap of `data` in `layout` at this pixmap's resolution.
    fn derive(&self, layout: Layout, data: Vec<u8>) -> Self {
        Self {
            width: layout.width,
            height: layout.height,
            alpha: layout.alpha,
            gray: layout.gray,
            data: Arc::new(data),
            resolution: self.resolution,
        }
    }

    fn converted(&self, py: Python<'_>, gray: bool) -> Self {
        if self.gray == gray {
            return self.share();
        }
        let layout = self.layout();
        self.derive(
            Layout { gray, ..layout },
            py.detach(|| pixel_ops::convert(&self.data, layout, gray)),
        )
    }

    /// Returns a pixmap that shares this one's samples.
    fn share(&self) -> Self {
        Self {
            data: Arc::clone(&self.data),
            ..*self
        }
    }

    /// Returns the pixels of `rect`, `(x0, y0, x1, y1)` in whole pixels,
    /// cut to the pixmap; `None` when none is left.
    fn area(&self, rect: (i64, i64, i64, i64)) -> PyResult<Option<Area>> {
        let (x0, y0, x1, y1) = rect;
        if x0 >= x1 || y0 >= y1 {
            return Err(PyValueError::new_err(format!(
                "rect must have x0 < x1 and y0 < y1: {rect:?}"
            )));
        }
        let cut = |value: i64, limit: u32| value.clamp(0, i64::from(limit)) as u32;
        let area = (
            cut(x0, self.width),
            cut(y0, self.height),
            cut(x1, self.width),
            cut(y1, self.height),
        );
        Ok((area.0 < area.2 && area.1 < area.3).then_some(area))
    }

    /// Returns the pixels of `rect` as [`Pixmap::area`] cuts it, or every
    /// pixel for `None`.
    fn region(&self, rect: Option<(i64, i64, i64, i64)>) -> PyResult<Option<Area>> {
        match rect {
            None => Ok(Some((0, 0, self.width, self.height))),
            Some(rect) => self.area(rect),
        }
    }

    /// Returns the offset of the pixel at column `x` and row `y`, or
    /// raises `IndexError` outside the pixmap.
    fn position(&self, x: i64, y: i64) -> PyResult<usize> {
        if !(0..i64::from(self.width)).contains(&x) || !(0..i64::from(self.height)).contains(&y) {
            return Err(PyIndexError::new_err(format!(
                "pixel ({x}, {y}) is outside the {}x{} pixmap",
                self.width, self.height
            )));
        }
        Ok((y as usize * self.width as usize + x as usize) * self.channels() as usize)
    }

    /// Checks that `value` has one byte per channel.
    fn value(&self, value: Vec<u8>) -> PyResult<Vec<u8>> {
        if value.len() != self.channels() as usize {
            return Err(PyValueError::new_err(format!(
                "value must have {} components, one per channel: {value:?}",
                self.channels()
            )));
        }
        Ok(value)
    }

    /// Returns the samples and their layout, which OCR reads.
    pub fn ocr_raster(&self) -> (Arc<Vec<u8>>, ocr::Layout) {
        let layout = ocr::Layout {
            width: self.width as usize,
            height: self.height as usize,
            gray: self.gray,
            alpha: self.alpha,
        };
        (Arc::clone(&self.data), layout)
    }

    fn encode(&self, out: &mut dyn Write, encoding: Encoding) -> Result<(), String> {
        match encoding {
            Encoding::Png => {
                let mut encoder = png::Encoder::new(out, self.width, self.height);
                encoder.set_color(match (self.gray, self.alpha) {
                    (true, false) => png::ColorType::Grayscale,
                    (true, true) => png::ColorType::GrayscaleAlpha,
                    (false, false) => png::ColorType::Rgb,
                    (false, true) => png::ColorType::Rgba,
                });
                encoder.set_depth(png::BitDepth::Eight);
                encoder.set_compression(png::Compression::Fast);
                let per_metre = |dpi: u32| (f64::from(dpi) / METRES_PER_INCH).round() as u32;
                encoder.set_pixel_dims(Some(png::PixelDimensions {
                    xppu: per_metre(self.resolution.0),
                    yppu: per_metre(self.resolution.1),
                    unit: png::Unit::Meter,
                }));
                let mut writer = encoder.write_header().map_err(|e| e.to_string())?;
                writer
                    .write_image_data(&self.data)
                    .and_then(|()| writer.finish())
                    .map_err(|e| e.to_string())
            }
            Encoding::Jpeg(quality) => {
                let side = |length: u32| {
                    u16::try_from(length).map_err(|_| "JPEG sides end at 65535 pixels".to_owned())
                };
                let mut encoder = JpegEncoder::new(out, quality);
                // Resolutions never pass the 65,535 of JFIF's densities.
                encoder.set_density(PixelDensity {
                    density: (self.resolution.0 as u16, self.resolution.1 as u16),
                    unit: PixelDensityUnit::Inches,
                });
                let color = if self.gray {
                    ColorType::Luma
                } else {
                    ColorType::Rgb
                };
                encoder
                    .encode(&self.data, side(self.width)?, side(self.height)?, color)
                    .map_err(|e| e.to_string())
            }
            Encoding::Pam => {
                codecs::write_pam(out, self.layout(), &self.data).map_err(|e| e.to_string())
            }
            Encoding::Pbm => {
                codecs::write_pbm(out, self.layout(), &self.data).map_err(|e| e.to_string())
            }
            Encoding::Pnm => {
                codecs::write_pnm(out, self.layout(), &self.data).map_err(|e| e.to_string())
            }
            Encoding::Psd => codecs::write_psd(out, self.layout(), &self.data, self.resolution)
                .map_err(|e| e.to_string()),
            Encoding::Ps => codecs::write_ps(out, self.layout(), &self.data, self.resolution)
                .map_err(|e| e.to_string()),
        }
    }
}

impl From<Raster> for Pixmap {
    fn from(raster: Raster) -> Self {
        Self {
            width: u32::from(raster.width),
            height: u32::from(raster.height),
            alpha: raster.alpha,
            gray: raster.gray,
            data: Arc::new(raster.data),
            resolution: raster.resolution,
        }
    }
}

/// An image format that a pixmap encodes to.
#[derive(Clone, Copy)]
enum Encoding {
    Png,
    /// JPEG at a quality from 1 through 100.
    Jpeg(u8),
    Pam,
    /// PBM, gray thresholded at 128.
    Pbm,
    /// PGM for gray or PPM for RGB, as `"pgm"`, `"ppm"`, or `"pnm"` asks.
    Pnm,
    Psd,
    /// PostScript.
    Ps,
}

/// The formats that `Encoding::parse` reads.
const FORMATS: &str = "'png', 'jpeg', 'pam', 'pbm', 'pgm', 'ppm', 'pnm', 'psd', or 'ps'";

impl Encoding {
    /// Reads `format` at `quality` for samples in gray or RGB, with or
    /// without alpha: PNG, PAM, and PSD hold every kind; JPEG, PNM, and
    /// PostScript hold no alpha; PBM and PGM hold only gray, and PPM only
    /// RGB.
    fn parse(format: &str, quality: u8, gray: bool, alpha: bool) -> PyResult<Self> {
        let encoding = match format {
            "png" => Self::Png,
            "jpeg" => Self::Jpeg(quality),
            "pam" => Self::Pam,
            "pbm" => Self::Pbm,
            "pgm" | "ppm" | "pnm" => Self::Pnm,
            "psd" => Self::Psd,
            "ps" => Self::Ps,
            _ => {
                return Err(PyValueError::new_err(format!(
                    "format must be {FORMATS}: {format:?}"
                )));
            }
        };
        let name = format.to_ascii_uppercase();
        if alpha && !matches!(encoding, Self::Png | Self::Pam | Self::Psd) {
            return Err(PyValueError::new_err(format!(
                "{} has no alpha channel; drop alpha or write PNG, PAM, or PSD",
                encoding.name()
            )));
        }
        if !gray && matches!(format, "pbm" | "pgm") {
            return Err(PyValueError::new_err(format!(
                "{name} holds gray samples; convert the pixmap to gray or write PPM"
            )));
        }
        if gray && format == "ppm" {
            return Err(PyValueError::new_err(
                "PPM holds RGB samples; convert the pixmap to RGB or write PGM",
            ));
        }
        Ok(encoding)
    }

    const fn name(self) -> &'static str {
        match self {
            Self::Png => "PNG",
            Self::Jpeg(_) => "JPEG",
            Self::Pam => "PAM",
            Self::Pbm => "PBM",
            Self::Pnm => "PNM",
            Self::Psd => "PSD",
            Self::Ps => "PostScript",
        }
    }
}

/// A `Write` that counts what it keeps in `written`, which the writers of
/// one batch share, and refuses the write that would take the count past
/// `limit`.
struct Bounded<'a> {
    bytes: Vec<u8>,
    written: &'a AtomicUsize,
    limit: Option<usize>,
    exceeded: bool,
}

impl<'a> Bounded<'a> {
    const fn new(written: &'a AtomicUsize, limit: Option<usize>) -> Self {
        Self {
            bytes: Vec::new(),
            written,
            limit,
            exceeded: false,
        }
    }
}

impl Write for Bounded<'_> {
    fn write(&mut self, buffer: &[u8]) -> std::io::Result<usize> {
        let counted = self
            .written
            .try_update(Ordering::Relaxed, Ordering::Relaxed, |written| {
                written
                    .checked_add(buffer.len())
                    .filter(|&total| self.limit.is_none_or(|limit| total <= limit))
            });
        if counted.is_err() {
            self.exceeded = true;
            return Err(std::io::Error::other("the encoded image passes its limit"));
        }
        self.bytes.extend_from_slice(buffer);
        Ok(buffer.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

#[pymethods]
impl Pixmap {
    /// Width in pixels.
    #[getter]
    const fn width(&self) -> u32 {
        self.width
    }

    /// Height in pixels.
    #[getter]
    const fn height(&self) -> u32 {
        self.height
    }

    /// Bytes per pixel: 1 for gray, 3 for RGB, and one more with alpha.
    #[getter(channels)]
    fn get_channels(&self) -> u32 {
        self.channels()
    }

    /// Whether each pixel ends with a straight alpha byte.
    #[getter]
    const fn alpha(&self) -> bool {
        self.alpha
    }

    /// The color space of the samples, a `pyropdf.Colorspace`.
    #[getter]
    fn colorspace<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        py.import("pyropdf._pixmap")?
            .getattr("Colorspace")?
            .getattr(if self.gray { "GRAY" } else { "RGB" })
    }

    /// Bytes per row: width × channels.
    #[getter]
    fn stride(&self) -> u32 {
        self.width * self.channels()
    }

    /// The horizontal and vertical resolution in dots per inch, which PNG
    /// output records.
    #[getter]
    const fn resolution(&self) -> (u32, u32) {
        self.resolution
    }

    /// Returns a copy with the colors inside `rect` inverted, or all of
    /// them when `rect` is `None`; alpha is kept. `rect` is
    /// `(x0, y0, x1, y1)` in whole pixels, cut to the pixmap.
    #[pyo3(signature = (rect=None))]
    fn invert(&self, py: Python<'_>, rect: Option<(i64, i64, i64, i64)>) -> PyResult<Self> {
        let area = match rect {
            None => Some((0, 0, self.width, self.height)),
            Some(rect) => self.area(rect)?,
        };
        let Some(area) = area else {
            return Ok(self.share());
        };
        let layout = self.layout();
        Ok(self.derive(
            layout,
            py.detach(|| pixel_ops::invert(&self.data, layout, area)),
        ))
    }

    /// Returns a copy with each color component `c`, from 0 to 1, replaced
    /// by `c ** value`: a `value` above 1 darkens and one below lightens.
    fn gamma(&self, py: Python<'_>, value: f64) -> PyResult<Self> {
        if !(value.is_finite() && value > 0.0) {
            return Err(PyValueError::new_err(format!(
                "gamma must be a positive number: {value}"
            )));
        }
        let table = pixel_ops::gamma_table(value);
        let layout = self.layout();
        Ok(self.derive(
            layout,
            py.detach(|| pixel_ops::map_colors(&self.data, layout, &[table; 3])),
        ))
    }

    /// Returns a copy whose black becomes `black` and whose white becomes
    /// `white`, RGB colors of 0 to 255, with the values between mapped
    /// linearly; a gray pixmap uses their luma. Alpha is kept.
    #[pyo3(signature = (black=(0, 0, 0), white=(255, 255, 255)))]
    fn tint(&self, py: Python<'_>, black: (u8, u8, u8), white: (u8, u8, u8)) -> Self {
        let (black, white) = ([black.0, black.1, black.2], [white.0, white.1, white.2]);
        let tables = if self.gray {
            [pixel_ops::tint_table(pixel_ops::luma(black), pixel_ops::luma(white)); 3]
        } else {
            std::array::from_fn(|component| {
                pixel_ops::tint_table(black[component], white[component])
            })
        };
        let layout = self.layout();
        self.derive(
            layout,
            py.detach(|| pixel_ops::map_colors(&self.data, layout, &tables)),
        )
    }

    /// Returns the pixmap in gray, with ITU-R BT.601 luma weights, keeping alpha.
    fn to_gray(&self, py: Python<'_>) -> Self {
        self.converted(py, true)
    }

    /// Returns the pixmap in RGB, keeping alpha.
    fn to_rgb(&self, py: Python<'_>) -> Self {
        self.converted(py, false)
    }

    /// Returns the pixmap with an alpha channel, opaque where it had none.
    fn with_alpha(&self, py: Python<'_>) -> Self {
        if self.alpha {
            return self.share();
        }
        let layout = self.layout();
        self.derive(
            Layout {
                alpha: true,
                ..layout
            },
            py.detach(|| pixel_ops::add_alpha(&self.data, layout)),
        )
    }

    /// Returns the pixmap without alpha, composited over `background`, an
    /// RGB color of 0 to 255 whose luma a gray pixmap uses.
    #[pyo3(signature = (background=(255, 255, 255)))]
    fn without_alpha(&self, py: Python<'_>, background: (u8, u8, u8)) -> Self {
        if !self.alpha {
            return self.share();
        }
        let layout = self.layout();
        let background = [background.0, background.1, background.2];
        self.derive(
            Layout {
                alpha: false,
                ..layout
            },
            py.detach(|| pixel_ops::drop_alpha(&self.data, layout, background)),
        )
    }

    /// Returns the pixels of `rect`, `(x0, y0, x1, y1)` in whole pixels,
    /// cut to the pixmap; a rect outside it raises `ValueError`.
    fn crop(&self, py: Python<'_>, rect: (i64, i64, i64, i64)) -> PyResult<Self> {
        let Some(area) = self.area(rect)? else {
            return Err(PyValueError::new_err(format!(
                "rect {rect:?} does not overlap the {}x{} pixmap",
                self.width, self.height
            )));
        };
        let layout = self.layout();
        let (x0, y0, x1, y1) = area;
        Ok(self.derive(
            Layout {
                width: x1 - x0,
                height: y1 - y0,
                ..layout
            },
            py.detach(|| pixel_ops::crop(&self.data, layout, area)),
        ))
    }

    /// Returns the pixmap resampled to `width` by `height` pixels with a
    /// bilinear filter, as Pillow's `Image.resize` with `BILINEAR`; the
    /// resolution follows, so the image keeps its size in inches. At most
    /// 64,000,000 pixels.
    fn scale(&self, py: Python<'_>, width: u32, height: u32) -> PyResult<Self> {
        let pixels = u64::from(width) * u64::from(height);
        if width == 0 || height == 0 || pixels > pixel_ops::MAX_PIXELS {
            return Err(PyValueError::new_err(format!(
                "a scaled pixmap has 1 to {} pixels, not {width}x{height}",
                pixel_ops::MAX_PIXELS
            )));
        }
        let layout = self.layout();
        let mut scaled = self.derive(
            Layout {
                width,
                height,
                ..layout
            },
            py.detach(|| pixel_ops::scale(&self.data, layout, width, height)),
        );
        let follow = |dpi: u32, to: u32, from: u32| {
            (f64::from(dpi) * f64::from(to) / f64::from(from))
                .round()
                .clamp(1.0, 65_535.0) as u32
        };
        scaled.resolution = (
            follow(self.resolution.0, width, self.width),
            follow(self.resolution.1, height, self.height),
        );
        Ok(scaled)
    }

    /// Returns the pixmap turned clockwise by `degrees`, a multiple of 90;
    /// a quarter turn swaps the width, the height, and the resolution.
    fn rotate(&self, py: Python<'_>, degrees: i64) -> PyResult<Self> {
        if degrees % 90 != 0 {
            return Err(PyValueError::new_err(format!(
                "degrees must be a multiple of 90: {degrees}"
            )));
        }
        let quarters = degrees.rem_euclid(360) / 90;
        if quarters == 0 {
            return Ok(self.share());
        }
        let layout = self.layout();
        let mut rotated = self.derive(
            layout,
            py.detach(|| pixel_ops::rotate(&self.data, layout, quarters as u8)),
        );
        if quarters % 2 == 1 {
            rotated.width = self.height;
            rotated.height = self.width;
            rotated.resolution = (self.resolution.1, self.resolution.0);
        }
        Ok(rotated)
    }

    /// Returns the pixmap mirrored left to right for `"horizontal"`, or top
    /// to bottom for `"vertical"`.
    fn flip(&self, py: Python<'_>, axis: &str) -> PyResult<Self> {
        let vertical = match axis {
            "horizontal" => false,
            "vertical" => true,
            _ => {
                return Err(PyValueError::new_err(format!(
                    "axis must be 'horizontal' or 'vertical': {axis:?}"
                )));
            }
        };
        let layout = self.layout();
        Ok(self.derive(
            layout,
            py.detach(|| pixel_ops::flip(&self.data, layout, vertical)),
        ))
    }

    /// Returns `width` by `height` pixels whose corners are the corners
    /// `ul`, `ur`, `ll`, and `lr` of `quad`, in pixel coordinates from the
    /// top left: each pixel's center maps through the perspective map onto
    /// the quadrilateral, where the pixmap is sampled bilinearly between
    /// pixel centers and clamped to its edge pixels. The quadrilateral must
    /// be convex; at most 64,000,000 pixels.
    fn warp(
        &self,
        py: Python<'_>,
        quad: [(f64, f64); 4],
        width: u32,
        height: u32,
    ) -> PyResult<Self> {
        let pixels = u64::from(width) * u64::from(height);
        if width == 0 || height == 0 || pixels > pixel_ops::MAX_PIXELS {
            return Err(PyValueError::new_err(format!(
                "a warped pixmap has 1 to {} pixels, not {width}x{height}",
                pixel_ops::MAX_PIXELS
            )));
        }
        let projection = pixel_ops::Projection::new(quad).ok_or_else(|| {
            PyValueError::new_err(format!("quad must be finite and convex: {quad:?}"))
        })?;
        let layout = self.layout();
        Ok(self.derive(
            Layout {
                width,
                height,
                ..layout
            },
            py.detach(|| pixel_ops::warp(&self.data, layout, projection, width, height)),
        ))
    }

    /// Returns the channels of the pixel at column `x` and row `y`.
    fn pixel<'py>(&self, py: Python<'py>, x: i64, y: i64) -> PyResult<Bound<'py, PyTuple>> {
        let at = self.position(x, y)?;
        PyTuple::new(py, &self.data[at..at + self.channels() as usize])
    }

    /// Returns a copy with the pixel at column `x` and row `y` set to
    /// `value`, one integer of 0 to 255 per channel, alpha included.
    fn with_pixel(&self, py: Python<'_>, x: i64, y: i64, value: Vec<u8>) -> PyResult<Self> {
        self.position(x, y)?;
        let value = self.value(value)?;
        let layout = self.layout();
        let area = (x as u32, y as u32, x as u32 + 1, y as u32 + 1);
        Ok(self.derive(
            layout,
            py.detach(|| pixel_ops::fill(&self.data, layout, area, &value)),
        ))
    }

    /// Returns a copy with every pixel inside `rect` set to `value`, one
    /// integer of 0 to 255 per channel, alpha included, or every pixel when
    /// `rect` is `None`. `rect` is `(x0, y0, x1, y1)` in whole pixels, cut
    /// to the pixmap; one outside it changes nothing.
    #[pyo3(signature = (value, rect=None))]
    fn fill(
        &self,
        py: Python<'_>,
        value: Vec<u8>,
        rect: Option<(i64, i64, i64, i64)>,
    ) -> PyResult<Self> {
        let value = self.value(value)?;
        let area = match rect {
            None => Some((0, 0, self.width, self.height)),
            Some(rect) => self.area(rect)?,
        };
        let Some(area) = area else {
            return Ok(self.share());
        };
        let layout = self.layout();
        Ok(self.derive(
            layout,
            py.detach(|| pixel_ops::fill(&self.data, layout, area, &value)),
        ))
    }

    /// Returns the number of distinct pixels, alpha included, inside
    /// `rect`, or in the whole pixmap when `rect` is `None`; 0 for a rect
    /// outside it.
    #[pyo3(signature = (rect=None))]
    fn color_count(&self, py: Python<'_>, rect: Option<(i64, i64, i64, i64)>) -> PyResult<u64> {
        let Some(area) = self.region(rect)? else {
            return Ok(0);
        };
        let layout = self.layout();
        let mut count = 0;
        py.detach(|| {
            pixel_ops::color_runs(&self.data, layout, area, |_, _| {
                count += 1;
                true
            });
        });
        Ok(count)
    }

    /// Returns each distinct pixel, alpha included, inside `rect`, or in
    /// the whole pixmap when `rect` is `None`, with the number of pixels
    /// that have it, in ascending order of the channels. More than
    /// `max_colors` distinct pixels raise `ValueError`; `None` lifts it.
    #[pyo3(signature = (rect=None, *, max_colors=Some(DEFAULT_MAX_COLORS)))]
    fn colors<'py>(
        &self,
        py: Python<'py>,
        rect: Option<(i64, i64, i64, i64)>,
        max_colors: Option<u64>,
    ) -> PyResult<Bound<'py, PyDict>> {
        if max_colors == Some(0) {
            return Err(PyValueError::new_err("max_colors must be positive"));
        }
        let dict = PyDict::new(py);
        let Some(area) = self.region(rect)? else {
            return Ok(dict);
        };
        let layout = self.layout();
        let mut runs = Vec::new();
        let mut exceeded = false;
        py.detach(|| {
            pixel_ops::color_runs(&self.data, layout, area, |pixel, count| {
                if max_colors.is_some_and(|max| runs.len() as u64 >= max) {
                    exceeded = true;
                    return false;
                }
                runs.push((pixel, count));
                true
            });
        });
        if exceeded {
            return Err(PyValueError::new_err(format!(
                "the pixmap has more than {} distinct colors; pass a larger max_colors",
                max_colors.unwrap_or_default()
            )));
        }
        for (pixel, count) in runs {
            dict.set_item(PyTuple::new(py, pixel)?, count)?;
        }
        Ok(dict)
    }

    /// Returns the share of the pixels inside `rect`, or in the whole
    /// pixmap when `rect` is `None`, that the most frequent pixel has, and
    /// that pixel, alpha included; of pixels as frequent, the least in
    /// ascending order of the channels. `None` for a rect outside it.
    #[pyo3(signature = (rect=None))]
    fn top_color<'py>(
        &self,
        py: Python<'py>,
        rect: Option<(i64, i64, i64, i64)>,
    ) -> PyResult<Option<(f64, Bound<'py, PyTuple>)>> {
        let Some(area) = self.region(rect)? else {
            return Ok(None);
        };
        let layout = self.layout();
        let mut top: Option<(Vec<u8>, u64)> = None;
        py.detach(|| {
            pixel_ops::color_runs(&self.data, layout, area, |pixel, count| {
                if top.as_ref().is_none_or(|(_, most)| count > *most) {
                    top = Some((pixel, count));
                }
                true
            });
        });
        let (x0, y0, x1, y1) = area;
        let total = f64::from(x1 - x0) * f64::from(y1 - y0);
        top.map(|(pixel, count)| Ok((count as f64 / total, PyTuple::new(py, pixel)?)))
            .transpose()
    }

    /// Decodes PNG, JPEG, PBM, PGM, PPM (plain or raw), PAM, or
    /// uncompressed BMP `data`, any bytes-like object, into a pixmap: PNG
    /// keeps its alpha and resolution, BMP its resolution and bit-field
    /// alpha, and PAM its alpha; JPEG and the portable anymaps decode at
    /// 96 dpi. Samples of more than 8 bits scale to 8, and palettes of only
    /// gray decode to gray. CMYK is refused. At most 64,000,000 pixels.
    #[staticmethod]
    fn from_bytes(py: Python<'_>, data: &Bound<'_, PyAny>) -> PyResult<Self> {
        let bytes = PyBuffer::<u8>::get(data)?.to_vec(py)?;
        let decoded = py
            .detach(|| pixel_ops::decode(&bytes))
            .map_err(PyValueError::new_err)?;
        let layout = decoded.layout;
        Ok(Self {
            width: layout.width,
            height: layout.height,
            alpha: layout.alpha,
            gray: layout.gray,
            data: Arc::new(decoded.data),
            resolution: decoded.resolution,
        })
    }

    /// Reads an image file into a pixmap, as `from_bytes` decodes it.
    #[staticmethod]
    fn open(py: Python<'_>, path: PathBuf) -> PyResult<Self> {
        let bytes = py.detach(|| fs::read(&path))?;
        let decoded = py
            .detach(|| pixel_ops::decode(&bytes))
            .map_err(PyValueError::new_err)?;
        let layout = decoded.layout;
        Ok(Self {
            width: layout.width,
            height: layout.height,
            alpha: layout.alpha,
            gray: layout.gray,
            data: Arc::new(decoded.data),
            resolution: decoded.resolution,
        })
    }

    /// Returns the pixels as a Pillow image of mode `L`, `LA`, `RGB`, or
    /// `RGBA`, with the resolution in its `dpi` info; it needs Pillow.
    fn to_pil<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let module = py.import("PIL.Image").map_err(|error| {
            if error.is_instance_of::<PyImportError>(py) {
                PyImportError::new_err("Pixmap.to_pil needs Pillow: pip install pillow")
            } else {
                error
            }
        })?;
        let mode = match (self.gray, self.alpha) {
            (true, false) => "L",
            (true, true) => "LA",
            (false, false) => "RGB",
            (false, true) => "RGBA",
        };
        let image = module.call_method1(
            "frombytes",
            (
                mode,
                (self.width, self.height),
                PyBytes::new(py, &self.data),
            ),
        )?;
        image.getattr("info")?.set_item("dpi", self.resolution)?;
        Ok(image)
    }

    /// Returns the pixels encoded as `format` (`"png"`, `"jpeg"` at
    /// `quality`, 1 through 100, `"pam"`, `"pbm"`, `"pgm"`, `"ppm"`,
    /// `"pnm"`, `"psd"`, or `"ps"`). PNG, PAM, and PSD keep alpha; the
    /// others refuse it. PBM and PGM take gray, thresholding PBM at 128;
    /// PPM takes RGB; PNM writes PGM or PPM. PSD sides end at 30,000
    /// pixels, and PostScript is one Level 2 page at the resolution.
    ///
    /// `max_size` bounds the output, 64 MiB by default, and refuses the
    /// write that would pass it with `LimitError` code
    /// `pixmap_output_size`; `None` leaves it unbounded.
    #[pyo3(signature = (format="png", *, quality=DEFAULT_QUALITY, max_size=Some(DEFAULT_MAX_OUTPUT_SIZE)))]
    fn to_bytes<'py>(
        &self,
        py: Python<'py>,
        format: &str,
        #[pyo3(from_py_with = extract_quality)] quality: u8,
        #[pyo3(from_py_with = extract_max_size)] max_size: Option<usize>,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let encoding = Encoding::parse(format, quality, self.gray, self.alpha)?;
        let written = AtomicUsize::new(0);
        let mut out = Bounded::new(&written, max_size);
        let result = py.detach(|| self.encode(&mut out, encoding));
        if out.exceeded {
            let limit = max_size.unwrap_or_default();
            return Err(LimitError::new_err((
                "pixmap_output_size",
                format!(
                    "the encoded {} exceeds the {limit}-byte output limit",
                    encoding.name()
                ),
            )));
        }
        result
            .map_err(|e| PdfError::new_err(format!("failed to encode {}: {e}", encoding.name())))?;
        Ok(PyBytes::new(py, &out.bytes))
    }

    /// Writes the pixels as `format`, as `to_bytes` encodes it, or
    /// else the format that the extension of `path` names, and replaces
    /// `path` atomically after a complete write, keeping an existing file's
    /// permissions and a final symlink. A failed save leaves `path` as it
    /// was.
    #[pyo3(signature = (path, /, *, format=None, quality=DEFAULT_QUALITY))]
    fn save(
        &self,
        py: Python<'_>,
        path: PathBuf,
        format: Option<&str>,
        #[pyo3(from_py_with = extract_quality)] quality: u8,
    ) -> PyResult<()> {
        let format = match format {
            Some(format) => format,
            None => format_of(&path).ok_or_else(|| {
                PyValueError::new_err(format!(
                    "cannot tell the image format of {}; pass format as {FORMATS}",
                    path.display()
                ))
            })?,
        };
        let encoding = Encoding::parse(format, quality, self.gray, self.alpha)?;
        py.detach(|| {
            let error = |e: &dyn std::fmt::Display| {
                PdfError::new_err(format!(
                    "failed to save the image to {}: {e}",
                    path.display()
                ))
            };
            let target = final_target(&path).map_err(|e| error(&e))?;
            let permissions = fs::metadata(&target)
                .ok()
                .filter(fs::Metadata::is_file)
                .map(|metadata| metadata.permissions());
            let (temporary, file) = temporary_sibling(&target).map_err(|e| error(&e))?;
            let mut out = BufWriter::new(file);
            let result = self
                .encode(&mut out, encoding)
                .and_then(|()| out.flush().map_err(|e| e.to_string()))
                .and_then(|()| out.get_ref().sync_all().map_err(|e| e.to_string()))
                .map_err(|e| error(&e));
            drop(out);
            let result = result.and_then(|()| {
                if let Some(permissions) = permissions {
                    fs::set_permissions(&temporary, permissions).map_err(|e| error(&e))?;
                }
                fs::rename(&temporary, &target).map_err(|e| error(&e))
            });
            if result.is_err() {
                let _ = fs::remove_file(&temporary);
            }
            result
        })
    }

    fn __repr__(&self) -> String {
        let colorspace = if self.gray { "gray" } else { "rgb" };
        let alpha = if self.alpha { " alpha" } else { "" };
        format!(
            "<Pixmap {}x{} {colorspace}{alpha}>",
            self.width, self.height
        )
    }

    /// Exposes the pixels without a copy, read-only, shaped `(height,
    /// width, channels)`.
    unsafe fn __getbuffer__(
        slf: Bound<'_, Self>,
        view: *mut ffi::Py_buffer,
        flags: c_int,
    ) -> PyResult<()> {
        if view.is_null() {
            return Err(PyBufferError::new_err("buffer view is null"));
        }
        if (flags & ffi::PyBUF_WRITABLE) == ffi::PyBUF_WRITABLE {
            return Err(PyBufferError::new_err("Pixmap buffer is read-only"));
        }
        let owner = slf.clone().into_any();
        let pixmap = slf.get();
        let data = &pixmap.data;
        let length = isize::try_from(data.len())
            .map_err(|_| PyBufferError::new_err("Pixmap buffer is too large"))?;
        let channels = pixmap.channels() as isize;
        let width = pixmap.width as isize;
        let height = pixmap.height as isize;
        // Shape and strides live in one allocation that `internal` owns
        // until `__releasebuffer__`.
        let mut dimensions = Box::new([height, width, channels, width * channels, channels, 1]);
        let dimensions_ptr = dimensions.as_mut_ptr();
        let nd = (flags & ffi::PyBUF_ND) == ffi::PyBUF_ND;
        let strides = (flags & ffi::PyBUF_STRIDES) == ffi::PyBUF_STRIDES;
        // SAFETY: `view` was checked for null. The frozen Pixmap never moves
        // or changes `data`, and the owner reference keeps it alive. Shape
        // and strides point into the boxed array that `internal` owns.
        unsafe {
            (*view).obj = owner.into_ptr();
            (*view).buf = data.as_ptr().cast_mut().cast::<c_void>();
            (*view).len = length;
            (*view).readonly = 1;
            (*view).itemsize = 1;
            (*view).format = if (flags & ffi::PyBUF_FORMAT) == ffi::PyBUF_FORMAT {
                CString::new("B")
                    .expect("the format has no null byte")
                    .into_raw()
            } else {
                ptr::null_mut()
            };
            (*view).ndim = if nd { 3 } else { 1 };
            (*view).shape = if nd { dimensions_ptr } else { ptr::null_mut() };
            (*view).strides = if nd && strides {
                dimensions_ptr.add(3)
            } else {
                ptr::null_mut()
            };
            (*view).suboffsets = ptr::null_mut();
            (*view).internal = Box::into_raw(dimensions).cast::<c_void>();
        }
        Ok(())
    }

    /// Releases the format string and dimensions of one buffer view.
    unsafe fn __releasebuffer__(&self, view: *mut ffi::Py_buffer) {
        if view.is_null() {
            return;
        }
        // SAFETY: `__getbuffer__` made every non-null format with
        // `CString::into_raw` and stored a boxed `[isize; 6]` in
        // `internal`, once for this view.
        unsafe {
            let format = (*view).format;
            if !format.is_null() {
                drop(CString::from_raw(format));
            }
            let internal = (*view).internal;
            if !internal.is_null() {
                drop(Box::from_raw(internal.cast::<[isize; 6]>()));
            }
        }
    }
}

/// The default bound of encoded output, 64 MiB, as pylopdf bounds it.
const DEFAULT_MAX_OUTPUT_SIZE: usize = 64 * 1024 * 1024;

/// The symbolic links that a save follows from its path at most.
const MAX_SYMLINKS: usize = 40;

/// The default JPEG quality, as pylopdf sets it.
const DEFAULT_QUALITY: u8 = 95;

/// The image format that the extension of `path` names.
fn format_of(path: &Path) -> Option<&'static str> {
    let extension = path.extension()?.to_str()?.to_ascii_lowercase();
    match extension.as_str() {
        "png" => Some("png"),
        "jpg" | "jpeg" => Some("jpeg"),
        "pam" => Some("pam"),
        "pbm" => Some("pbm"),
        "pgm" => Some("pgm"),
        "ppm" => Some("ppm"),
        "pnm" => Some("pnm"),
        "psd" => Some("psd"),
        "ps" => Some("ps"),
        _ => None,
    }
}

fn extract_quality(value: &Bound<'_, PyAny>) -> PyResult<u8> {
    let message = "quality must be an integer from 1 through 100";
    if value.is_instance_of::<PyBool>() || !value.is_instance_of::<PyInt>() {
        return Err(PyTypeError::new_err(message));
    }
    value
        .extract::<u8>()
        .ok()
        .filter(|quality| (1..=100).contains(quality))
        .ok_or_else(|| PyValueError::new_err(message))
}

fn extract_max_size(value: &Bound<'_, PyAny>) -> PyResult<Option<usize>> {
    if value.is_none() {
        return Ok(None);
    }
    let message = "max_size must be a positive integer or None";
    if value.is_instance_of::<PyBool>() || !value.is_instance_of::<PyInt>() {
        return Err(PyTypeError::new_err(message));
    }
    match value.extract::<usize>() {
        Ok(size) if size > 0 => Ok(Some(size)),
        _ => Err(PyValueError::new_err(message)),
    }
}

/// Follows the symbolic links at the end of `path`, so that a save
/// replaces the file and keeps the link.
fn final_target(path: &Path) -> io::Result<PathBuf> {
    let mut resolved = path.to_path_buf();
    for _ in 0..MAX_SYMLINKS {
        match fs::symlink_metadata(&resolved) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                let destination = fs::read_link(&resolved)?;
                resolved = if destination.is_absolute() {
                    destination
                } else {
                    resolved
                        .parent()
                        .unwrap_or_else(|| Path::new("."))
                        .join(destination)
                };
            }
            Ok(_) => return Ok(resolved),
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(resolved),
            Err(error) => return Err(error),
        }
    }
    Err(io::Error::new(
        io::ErrorKind::InvalidInput,
        "too many levels of symbolic links",
    ))
}

/// Creates a new file beside `target`, named from the process, the time,
/// and a counter, so that two saves never share one.
fn temporary_sibling(target: &Path) -> io::Result<(PathBuf, File)> {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let parent = target.parent().unwrap_or_else(|| Path::new("."));
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_nanos());
    for _ in 0..100 {
        let count = COUNTER.fetch_add(1, Ordering::Relaxed);
        let path = parent.join(format!(
            ".pyropdf-{}-{nanos:x}-{count}.tmp",
            std::process::id()
        ));
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(file) => return Ok((path, file)),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error),
        }
    }
    Err(io::Error::new(
        io::ErrorKind::AlreadyExists,
        format!(
            "failed to create a unique temporary output beside {}",
            target.display()
        ),
    ))
}

/// Returns the colors of `pixmap` without its alpha, as the samples of an
/// image in 1 (gray), 3 (RGB), or 4 (CMYK) `components` (`page.recolor`),
/// or as a JPEG at `quality` when it is given, for gray or RGB.
#[pyfunction]
#[pyo3(signature = (pixmap, components, quality=None))]
pub fn recolor_samples<'py>(
    py: Python<'py>,
    pixmap: &Pixmap,
    components: usize,
    quality: Option<u8>,
) -> PyResult<Bound<'py, PyBytes>> {
    let color = match (components, quality) {
        (1 | 3 | 4, None) => None,
        (1, Some(_)) => Some(ColorType::Luma),
        (3, Some(_)) => Some(ColorType::Rgb),
        _ => {
            return Err(PyValueError::new_err(format!(
                "no {components}-component samples{}",
                if quality.is_some() { " in JPEG" } else { "" }
            )));
        }
    };
    let layout = pixmap.layout();
    let samples = py.detach(|| pixel_ops::device_samples(&pixmap.data, layout, components));
    let (Some(color), Some(quality)) = (color, quality) else {
        return Ok(PyBytes::new(py, &samples));
    };
    let side = |length: u32| {
        u16::try_from(length).map_err(|_| PyValueError::new_err("JPEG sides end at 65535 pixels"))
    };
    let (width, height) = (side(pixmap.width)?, side(pixmap.height)?);
    let mut out = Vec::new();
    py.detach(|| JpegEncoder::new(&mut out, quality).encode(&samples, width, height, color))
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(PyBytes::new(py, &out))
}

/// Renders the page whose object is `number` and `generation` through the
/// linear `matrix`, with the GIL released. The Python layer has checked the
/// options and resolved `background` to straight RGBA.
#[pyfunction]
#[expect(clippy::too_many_arguments, reason = "the options of Page.pixmap")]
pub fn render_page(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    matrix: (f64, f64, f64, f64),
    alpha: bool,
    gray: bool,
    background: (u8, u8, u8, u8),
    clip: Option<(f64, f64, f64, f64)>,
    annotations: bool,
) -> PyResult<Pixmap> {
    let (version, index, budget) = crate::pages::index(document)?;
    let (red, green, blue, opacity) = background;
    let options = render::Options {
        matrix,
        alpha,
        gray,
        background: [red, green, blue, opacity],
        clip,
        annotations,
    };
    let raster = py.detach(|| -> PyResult<Raster> {
        let at = index
            .index_of(Reference::new(number, generation))
            .ok_or_else(|| {
                StaleObjectError::new_err(format!(
                    "page object {number} {generation} is no longer a page of the document"
                ))
            })?;
        let bridged = document
            .bridge
            .get_for_render(&version, &index, &budget, annotations)
            .map_err(|e| bridge_error(&e))?;
        render::raster(&bridged, at, &options).map_err(raster_error)
    })?;
    Ok(Pixmap::from(raster))
}

pub(crate) fn raster_error(error: RasterError) -> PyErr {
    match error {
        RasterError::Geometry(message) => PyValueError::new_err(message),
        RasterError::NoPage(_) | RasterError::Clip(_) => PdfError::new_err(error.to_string()),
    }
}

/// Why a page of a batch failed: its raster or its encoding.
struct Failed(PyErr);

impl From<RasterError> for Failed {
    fn from(error: RasterError) -> Self {
        Self(raster_error(error))
    }
}

/// Renders the pages at `pages`, counted from 0 and from the end when
/// negative, or every page, all of one version, through the linear
/// `matrix`, and encodes each as `format` at `quality`, on up to `workers`
/// threads with the GIL released. `max_size` bounds the encoded bytes of
/// all the pages together. The Python layer has checked the options and
/// resolved `background` to straight RGBA.
#[pyfunction]
#[expect(clippy::too_many_arguments, reason = "the options of Document.render")]
pub fn render_pages(
    py: Python<'_>,
    document: &_Document,
    pages: Option<Vec<i64>>,
    matrix: (f64, f64, f64, f64),
    alpha: bool,
    gray: bool,
    background: (u8, u8, u8, u8),
    annotations: bool,
    format: &str,
    quality: u8,
    workers: usize,
    max_size: Option<usize>,
) -> PyResult<Vec<Py<PyBytes>>> {
    let encoding = Encoding::parse(format, quality, gray, alpha)?;
    let (version, index, budget) = crate::pages::index(document)?;
    let indexes = crate::pages::entries(pages, index.len())?;
    let (red, green, blue, opacity) = background;
    let options = render::Options {
        matrix,
        alpha,
        gray,
        background: [red, green, blue, opacity],
        clip: None,
        annotations,
    };
    let written = AtomicUsize::new(0);
    let encoded = py.detach(|| -> PyResult<Vec<Vec<u8>>> {
        let bridged = document
            .bridge
            .get_for_render(&version, &index, &budget, annotations)
            .map_err(|e| bridge_error(&e))?;
        render::batch(&bridged, &indexes, &options, workers, |raster| {
            let mut out = Bounded::new(&written, max_size);
            let result = Pixmap::from(raster).encode(&mut out, encoding);
            if out.exceeded {
                let limit = max_size.unwrap_or_default();
                return Err(Failed(LimitError::new_err((
                    "render_output_size",
                    format!("the rendered batch exceeds the {limit}-byte encoded-output limit"),
                ))));
            }
            result.map_err(|e| {
                Failed(PdfError::new_err(format!(
                    "failed to encode the rendered image: {e}"
                )))
            })?;
            Ok(out.bytes)
        })
        .map_err(|Failed(error)| error)
    })?;
    Ok(encoded
        .into_iter()
        .map(|bytes| PyBytes::new(py, &bytes).unbind())
        .collect())
}

pub fn bridge_error(error: &BridgeError) -> PyErr {
    match error {
        BridgeError::Store(error) => store_error(error),
        BridgeError::EncryptionChanged | BridgeError::PasswordNotUtf8 => {
            EncryptedDocumentError::new_err(error.to_string())
        }
        _ => PdfError::new_err(error.to_string()),
    }
}
