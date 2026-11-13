//! `Pixmap`, pylopdf's immutable raster with the buffer protocol and
//! file/bytes output through `pyropdf_engine::pixmap` (pylopdf
//! rust/src/pixmap.rs at 884af0db and 036be111), and `page.pixmap` and
//! `doc.render` through the hayro bridge, with the GIL released while
//! rendering.

use std::ffi::{CString, c_int, c_void};
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::ptr;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use pyo3::buffer::PyBuffer;
use pyo3::exceptions::{PyBufferError, PyImportError, PyIndexError, PyTypeError, PyValueError};
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyInt, PyTuple};
use pyropdf_engine::bridge::Error as BridgeError;
use pyropdf_engine::ocr;
use pyropdf_engine::pixmap::{
    self as pixel_ops, Area, Bounded, BudgetedPngOutput, DecodeError, Decoded, EncodeError,
    Encoding, FORMATS, Layout, encode, encode_png, format_of,
};
use pyropdf_engine::render::{self, Raster, RasterError};
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::value::Reference;

use crate::document::_Document;
use crate::errors::{
    EncryptedDocumentError, LimitError, PdfError, StaleObjectError, limit_error, store_error,
};

/// The most distinct colors that `Pixmap.colors` returns by default.
const DEFAULT_MAX_COLORS: u64 = 65_536;

/// The encoded-image limit from the registry, in bytes.
const DEFAULT_MAX_INPUT_SIZE: Option<usize> = match LimitCode::ImageInputSize.default_limit() {
    Some(limit) => Some(limit as usize),
    None => None,
};

fn encode_error(error: EncodeError, context: &str) -> PyErr {
    match error {
        EncodeError::Limit(refusal) => limit_error(&refusal),
        EncodeError::Other(message) => PdfError::new_err(format!("{context}: {message}")),
    }
}

pub(crate) fn decode_error(error: DecodeError) -> PyErr {
    match error {
        DecodeError::Invalid(message) => PyValueError::new_err(message),
        DecodeError::Limit(refusal) => limit_error(&refusal),
    }
}

/// A rendered page: immutable pixels, row by row from the top, RGB or gray,
/// each with or without straight alpha, 8 bits per channel.
///
/// Implements Python's buffer protocol without copying: `memoryview(pixmap)`
/// and `numpy.asarray(pixmap)` expose the samples read-only as `uint8` (`"B"`)
/// values shaped `(height, width, channels)`.
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

    fn encode(
        &self,
        out: &mut dyn Write,
        encoding: Encoding,
        budget: &Budget,
    ) -> Result<(), EncodeError> {
        encode(
            out,
            self.layout(),
            &self.data,
            self.resolution,
            encoding,
            budget,
        )
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

impl From<Decoded> for Pixmap {
    fn from(decoded: Decoded) -> Self {
        Self {
            width: decoded.layout.width,
            height: decoded.layout.height,
            alpha: decoded.layout.alpha,
            gray: decoded.layout.gray,
            data: Arc::new(decoded.data),
            resolution: decoded.resolution,
        }
    }
}

#[pymethods]
impl Pixmap {
    /// Width in pixels.
    #[getter]
    fn width(&self) -> PyResult<u32> {
        crate::errors::boundary(|| self.width)
    }

    /// Height in pixels.
    #[getter]
    fn height(&self) -> PyResult<u32> {
        crate::errors::boundary(|| self.height)
    }

    /// Bytes per pixel: 1 for gray, 3 for RGB, and one more with alpha.
    #[getter(channels)]
    fn get_channels(&self) -> PyResult<u32> {
        crate::errors::boundary(|| self.channels())
    }

    /// Whether each pixel ends with a straight alpha byte.
    #[getter]
    fn alpha(&self) -> PyResult<bool> {
        crate::errors::boundary(|| self.alpha)
    }

    /// The color space of the samples, a `pyropdf.Colorspace`.
    #[getter]
    fn colorspace<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        crate::errors::boundary(|| {
            py.import("pyropdf._pixmap")?
                .getattr("Colorspace")?
                .getattr(if self.gray { "GRAY" } else { "RGB" })
        })?
    }

    /// Bytes per row: width × channels.
    #[getter]
    fn stride(&self) -> PyResult<u32> {
        crate::errors::boundary(|| self.width * self.channels())
    }

    /// The horizontal and vertical resolution in dots per inch, which PNG
    /// output records.
    #[getter]
    fn resolution(&self) -> PyResult<(u32, u32)> {
        crate::errors::boundary(|| self.resolution)
    }

    /// Returns a copy with the colors inside `rect` inverted, or all of
    /// them when `rect` is `None`; alpha is kept. `rect` is
    /// `(x0, y0, x1, y1)` in whole pixels, cut to the pixmap.
    #[pyo3(signature = (rect=None))]
    fn invert(&self, py: Python<'_>, rect: Option<(i64, i64, i64, i64)>) -> PyResult<Self> {
        crate::errors::boundary(|| {
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
        })?
    }

    /// Returns a copy with each color component `c`, from 0 to 1, replaced
    /// by `c ** value`: a `value` above 1 darkens and one below lightens.
    fn gamma(&self, py: Python<'_>, value: f64) -> PyResult<Self> {
        crate::errors::boundary(|| {
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
        })?
    }

    /// Returns a copy whose black becomes `black` and whose white becomes
    /// `white`, RGB colors of 0 to 255, with the values between mapped
    /// linearly; a gray pixmap uses their luma. Alpha is kept.
    #[pyo3(signature = (black=(0, 0, 0), white=(255, 255, 255)))]
    fn tint(&self, py: Python<'_>, black: (u8, u8, u8), white: (u8, u8, u8)) -> PyResult<Self> {
        crate::errors::boundary(|| {
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
        })
    }

    /// Returns the pixmap in gray, with ITU-R BT.601 luma weights, keeping alpha.
    fn to_gray(&self, py: Python<'_>) -> PyResult<Self> {
        crate::errors::boundary(|| self.converted(py, true))
    }

    /// Returns the pixmap in RGB, keeping alpha.
    fn to_rgb(&self, py: Python<'_>) -> PyResult<Self> {
        crate::errors::boundary(|| self.converted(py, false))
    }

    /// Returns the pixmap with an alpha channel, opaque where it had none.
    fn with_alpha(&self, py: Python<'_>) -> PyResult<Self> {
        crate::errors::boundary(|| {
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
        })
    }

    /// Returns the pixmap without alpha, composited over `background`, an
    /// RGB color of 0 to 255 whose luma a gray pixmap uses.
    #[pyo3(signature = (background=(255, 255, 255)))]
    fn without_alpha(&self, py: Python<'_>, background: (u8, u8, u8)) -> PyResult<Self> {
        crate::errors::boundary(|| {
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
        })
    }

    /// Returns the pixels of `rect`, `(x0, y0, x1, y1)` in whole pixels,
    /// cut to the pixmap; a rect outside it raises `ValueError`.
    fn crop(&self, py: Python<'_>, rect: (i64, i64, i64, i64)) -> PyResult<Self> {
        crate::errors::boundary(|| {
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
        })?
    }

    /// Returns the pixmap resampled to `width` by `height` pixels with a
    /// bilinear filter, as Pillow's `Image.resize` with `BILINEAR`; the
    /// resolution follows, so the image keeps its size in inches. At most
    /// 64,000,000 pixels.
    fn scale(&self, py: Python<'_>, width: u32, height: u32) -> PyResult<Self> {
        crate::errors::boundary(|| {
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
        })?
    }

    /// Returns the pixmap turned clockwise by `degrees`, a multiple of 90;
    /// a quarter turn swaps the width, the height, and the resolution.
    fn rotate(&self, py: Python<'_>, degrees: i64) -> PyResult<Self> {
        crate::errors::boundary(|| {
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
        })?
    }

    /// Returns the pixmap mirrored left to right for `"horizontal"`, or top
    /// to bottom for `"vertical"`.
    fn flip(&self, py: Python<'_>, axis: &str) -> PyResult<Self> {
        crate::errors::boundary(|| {
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
        })?
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
        crate::errors::boundary(|| {
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
        })?
    }

    /// Returns the channels of the pixel at column `x` and row `y`.
    fn pixel<'py>(&self, py: Python<'py>, x: i64, y: i64) -> PyResult<Bound<'py, PyTuple>> {
        crate::errors::boundary(|| {
            let at = self.position(x, y)?;
            PyTuple::new(py, &self.data[at..at + self.channels() as usize])
        })?
    }

    /// Returns a copy with the pixel at column `x` and row `y` set to
    /// `value`, one integer of 0 to 255 per channel, alpha included.
    fn with_pixel(&self, py: Python<'_>, x: i64, y: i64, value: Vec<u8>) -> PyResult<Self> {
        crate::errors::boundary(|| {
            self.position(x, y)?;
            let value = self.value(value)?;
            let layout = self.layout();
            let area = (x as u32, y as u32, x as u32 + 1, y as u32 + 1);
            Ok(self.derive(
                layout,
                py.detach(|| pixel_ops::fill(&self.data, layout, area, &value)),
            ))
        })?
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
        crate::errors::boundary(|| {
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
        })?
    }

    /// Returns the number of distinct pixels, alpha included, inside
    /// `rect`, or in the whole pixmap when `rect` is `None`; 0 for a rect
    /// outside it.
    #[pyo3(signature = (rect=None))]
    fn color_count(&self, py: Python<'_>, rect: Option<(i64, i64, i64, i64)>) -> PyResult<u64> {
        crate::errors::boundary(|| {
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
        })?
    }

    /// Returns each distinct pixel, alpha included, inside `rect`, or in
    /// the whole pixmap when `rect` is `None`, with the number of pixels
    /// that have it, in ascending order of the channels. More than
    /// `max_colors` distinct pixels raise `ValueError`; `None` lifts it.
    #[pyo3(signature = (rect=None, *, max_colors=Some(DEFAULT_MAX_COLORS)))]
    #[expect(
        clippy::disallowed_methods,
        reason = "max_colors is checked before each push; even with no limit there are at most MAX_PIXELS colors"
    )]
    fn colors<'py>(
        &self,
        py: Python<'py>,
        rect: Option<(i64, i64, i64, i64)>,
        max_colors: Option<u64>,
    ) -> PyResult<Bound<'py, PyDict>> {
        crate::errors::boundary(|| {
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
        })?
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
        crate::errors::boundary(|| {
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
        })?
    }

    /// Decodes PNG, JPEG, PBM, PGM, PPM (plain or raw), PAM, BMP
    /// (uncompressed or RLE8 and RLE4), GIF, TIFF, or JPEG 2000 `data`, any
    /// bytes-like object, into a pixmap: PNG and TIFF keep their alpha and
    /// resolution, BMP its resolution and bit-field alpha, PAM and JPEG 2000
    /// their alpha, and GIF decodes its first frame, with alpha where it is
    /// transparent; JPEG, GIF, JPEG 2000, and the portable anymaps decode at
    /// 96 dpi. Samples of more than 8 bits scale to 8, and BMP palettes of
    /// only gray decode to gray. TIFF reads gray, gray with alpha, RGB,
    /// RGBA, and palettes, uncompressed or with PackBits, LZW, Deflate,
    /// JPEG, or CCITT Group 4. CMYK, CCITT Group 3 TIFF, and JPEG XR are
    /// refused. At most 64,000,000 pixels. `max_size` bounds encoded input
    /// bytes before copying or decoding (default 256 MiB); exceeding it
    /// raises `LimitError` with code `image_input_size`. Pass `None` to
    /// remove this input limit for trusted data; the pixel limit remains.
    #[staticmethod]
    #[pyo3(signature = (data, *, max_size=DEFAULT_MAX_INPUT_SIZE))]
    fn from_bytes(
        py: Python<'_>,
        data: &Bound<'_, PyAny>,
        #[pyo3(from_py_with = extract_max_size)] max_size: Option<usize>,
    ) -> PyResult<Self> {
        crate::errors::boundary(|| {
            let budget = Budget::default()
                .with_limit(LimitCode::ImageInputSize, max_size.map(|size| size as u64));
            let bytes = image_buffer(data, &budget)?;
            py.detach(|| pixel_ops::decode(&bytes, &budget))
                .map(Self::from)
                .map_err(decode_error)
        })?
    }

    /// Reads an image file into a pixmap, as `from_bytes` decodes it.
    /// `max_size` bounds encoded input bytes (default 256 MiB), both before
    /// and during reading, including a file that grows after its metadata
    /// is checked. Exceeding it raises `LimitError` with code
    /// `image_input_size`. Pass `None` to remove this input limit for trusted
    /// files; the 64,000,000-pixel limit remains. File errors are preserved.
    #[staticmethod]
    #[pyo3(signature = (path, *, max_size=DEFAULT_MAX_INPUT_SIZE))]
    fn open(
        py: Python<'_>,
        path: PathBuf,
        #[pyo3(from_py_with = extract_max_size)] max_size: Option<usize>,
    ) -> PyResult<Self> {
        crate::errors::boundary(|| {
            let budget = Budget::default()
                .with_limit(LimitCode::ImageInputSize, max_size.map(|size| size as u64));
            let bytes = py.detach(|| {
                let mut file = File::open(&path)?;
                let size = file.metadata()?.len();
                read_image(&mut file, size, &budget)
            })?;
            py.detach(|| pixel_ops::decode(&bytes, &budget))
                .map(Self::from)
                .map_err(decode_error)
        })?
    }

    /// Returns the pixels as a Pillow image of mode `L`, `LA`, `RGB`, or
    /// `RGBA`, with the resolution in its `dpi` info; it needs Pillow.
    fn to_pil<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        crate::errors::boundary(|| {
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
        })?
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
        crate::errors::boundary(|| {
            let encoding = Encoding::parse(format, quality, self.gray, self.alpha)
                .map_err(PyValueError::new_err)?;
            let written = AtomicUsize::new(0);
            let mut out = Bounded::new(&written, max_size);
            let result = py.detach(|| self.encode(&mut out, encoding, &Budget::default()));
            if out.exceeded() {
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
                .map_err(|e| encode_error(e, &format!("failed to encode {}", encoding.name())))?;
            Ok(PyBytes::new(py, out.as_slice()))
        })?
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
        crate::errors::boundary(|| {
            let format = match format {
                Some(format) => format,
                None => format_of(&path).ok_or_else(|| {
                    PyValueError::new_err(format!(
                        "cannot tell the image format of {}; pass format as {FORMATS}",
                        path.display()
                    ))
                })?,
            };
            let encoding = Encoding::parse(format, quality, self.gray, self.alpha)
                .map_err(PyValueError::new_err)?;
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
                    .encode(&mut out, encoding, &Budget::default())
                    .map_err(|e| match e {
                        EncodeError::Limit(refusal) => refusal.to_string(),
                        EncodeError::Other(message) => message,
                    })
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
        })?
    }

    fn __repr__(&self) -> PyResult<String> {
        crate::errors::boundary(|| {
            let colorspace = if self.gray { "gray" } else { "rgb" };
            let alpha = if self.alpha { " alpha" } else { "" };
            format!(
                "<Pixmap {}x{} {colorspace}{alpha}>",
                self.width, self.height
            )
        })
    }

    /// Exposes the pixels without a copy, read-only, shaped `(height,
    /// width, channels)`.
    unsafe fn __getbuffer__(
        slf: Bound<'_, Self>,
        view: *mut ffi::Py_buffer,
        flags: c_int,
    ) -> PyResult<()> {
        crate::errors::boundary(|| {
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
        })?
    }

    /// Releases the format string and dimensions of one buffer view.
    unsafe fn __releasebuffer__(&self, view: *mut ffi::Py_buffer) -> PyResult<()> {
        crate::errors::boundary(|| {
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
        })
    }
}

/// The default bound of encoded output: 64 MiB.
const DEFAULT_MAX_OUTPUT_SIZE: usize = 64 * 1024 * 1024;

/// The symbolic links that a save follows from its path at most.
const MAX_SYMLINKS: usize = 40;

/// The default JPEG quality: 95.
const DEFAULT_QUALITY: u8 = 95;

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

/// Copies a Python buffer only after checking and reserving its full size.
fn image_buffer(data: &Bound<'_, PyAny>, budget: &Budget) -> PyResult<Vec<u8>> {
    let buffer = PyBuffer::<u8>::get(data)?;
    let size = buffer.len_bytes();
    budget
        .check(LimitCode::ImageInputSize, size as u64)
        .map_err(|refusal| limit_error(&refusal))?;
    let mut bytes = BudgetVec::new(budget);
    bytes
        .try_reserve_exact(size)
        .map_err(|refusal| limit_error(&refusal))?;
    let mut bytes = bytes.into_vec();
    #[expect(
        clippy::disallowed_methods,
        reason = "BudgetVec reserved and charged all size bytes before initializing this buffer"
    )]
    bytes.resize(size, 0);
    buffer.copy_to_slice(data.py(), &mut bytes)?;
    Ok(bytes)
}

/// Reads a checked image, enforcing the actual byte count even when the
/// source grows or its metadata reports no size, as a pipe does.
fn read_image(
    input: &mut impl Read,
    metadata_size: u64,
    budget: &Budget,
) -> Result<Vec<u8>, crate::source::Error> {
    budget.check(LimitCode::ImageInputSize, metadata_size)?;
    let size =
        usize::try_from(metadata_size).map_err(|_| LimitExceeded::new(LimitCode::MemorySize))?;
    let mut bytes = BudgetVec::new(budget);
    bytes.try_reserve_exact(size)?;
    let mut chunk = [0; 16 * 1024];
    loop {
        budget.poll()?;
        // Probe at most one byte beyond the limit, without appending that
        // byte or making room for it in the retained input.
        let count = budget
            .limit(LimitCode::ImageInputSize)
            .map_or(chunk.len(), |limit| {
                limit
                    .saturating_sub(bytes.len() as u64)
                    .saturating_add(1)
                    .min(chunk.len() as u64) as usize
            });
        let read = match input.read(&mut chunk[..count]) {
            Ok(0) => break,
            Ok(read) => read,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error.into()),
        };
        budget.check(LimitCode::ImageInputSize, bytes.len() as u64 + read as u64)?;
        bytes.try_extend_from_slice(&chunk[..read])?;
    }
    Ok(bytes.into_vec())
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

/// Encodes `pixmap` as PNG for `document`, under a budget from the
/// document's limits: its memory limit, cancellation token, and deadline.
#[pyfunction]
pub fn pixmap_png<'py>(
    py: Python<'py>,
    document: &_Document,
    pixmap: &Pixmap,
) -> PyResult<Bound<'py, PyBytes>> {
    crate::errors::boundary(|| {
        let budget = document.budget();
        let mut out = BudgetedPngOutput::new(&budget);
        let layout = pixmap.layout();
        let result =
            py.detach(|| encode_png(&mut out, layout, &pixmap.data, pixmap.resolution, &budget));
        if let Some(refusal) = out.take_refusal() {
            return Err(limit_error(&refusal));
        }
        result.map_err(|e| encode_error(e, "failed to encode PNG"))?;
        // Python copies the encoded bytes while the Rust output remains live.
        // Charge both allocations and use the fallible Python allocation API.
        let encoded = out.as_slice();
        let len = encoded.len();
        budget
            .charge(
                LimitCode::MemorySize,
                u64::try_from(len).unwrap_or(u64::MAX),
            )
            .map_err(|e| limit_error(&e))?;
        let size = isize::try_from(len)
            .map_err(|_| PyValueError::new_err("the encoded PNG is too large"))?;
        // SAFETY: a null source requests an uninitialized bytes object. It is
        // owned locally and is never exposed to Python before it is filled.
        let raw = unsafe { ffi::PyBytes_FromStringAndSize(ptr::null(), size) };
        // SAFETY: PyBytes_FromStringAndSize returns a new reference or null with
        // a Python exception set. The owned handle also frees it on refusal.
        let bytes = unsafe { Bound::from_owned_ptr_or_err(py, raw)? };
        // SAFETY: `bytes` is a new, unshared bytes object and the result points
        // to its `len` bytes of writable storage until the object is published.
        let target = unsafe { ffi::PyBytes_AsString(bytes.as_ptr()).cast::<u8>() } as usize;
        // The object remains owned and inaccessible to Python while detached, so
        // another thread can cancel the operation while the bytes are copied.
        py.detach(|| -> Result<(), LimitExceeded> {
            for (index, source) in encoded.chunks(16 * 1024).enumerate() {
                budget.poll()?;
                // SAFETY: every chunk fits in the allocated, unshared object, and
                // the source is a distinct Rust buffer. No uninitialized bytes
                // are read. The owning handle remains alive until this finishes.
                unsafe {
                    ptr::copy_nonoverlapping(
                        source.as_ptr(),
                        (target as *mut u8).add(index * 16 * 1024),
                        source.len(),
                    );
                }
            }
            budget.poll()
        })
        .map_err(|e| limit_error(&e))?;
        bytes.cast_into::<PyBytes>().map_err(Into::into)
    })?
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
    crate::errors::boundary(|| {
        let layout = pixmap.layout();
        let out = py
            .detach(|| pixel_ops::recolor_samples(&pixmap.data, layout, components, quality))
            .map_err(PyValueError::new_err)?;
        Ok(PyBytes::new(py, &out))
    })?
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
    hinting: bool,
    glyph_cache: bool,
) -> PyResult<Pixmap> {
    crate::errors::boundary(|| {
        let (version, index, budget) = crate::pages::index(document)?;
        let (red, green, blue, opacity) = background;
        let options = render::Options {
            matrix,
            alpha,
            gray,
            background: [red, green, blue, opacity],
            clip,
            annotations,
            hinting,
            glyph_cache,
        };
        let fallback = document.fallback();
        let raster = py.detach(|| -> PyResult<Raster> {
            budget.poll().map_err(|e| limit_error(&e))?;
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
            let raster = render::raster(&bridged, &fallback, at, &options).map_err(raster_error)?;
            budget.poll().map_err(|e| limit_error(&e))?;
            Ok(raster)
        })?;
        Ok(Pixmap::from(raster))
    })?
}

pub(crate) fn raster_error(error: RasterError) -> PyErr {
    match error {
        RasterError::Limit(ref refusal) => {
            LimitError::new_err((refusal.code().as_str(), error.to_string()))
        }
        RasterError::Geometry(message) => PyValueError::new_err(message),
        RasterError::ClipPageSide(message) => {
            LimitError::new_err((LimitCode::ClipPageSide.as_str(), message))
        }
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
#[expect(
    clippy::disallowed_methods,
    reason = "one Python byte buffer per already budgeted and output-size-bounded encoded page"
)]
pub fn render_pages(
    py: Python<'_>,
    document: &_Document,
    pages: Option<Vec<i64>>,
    matrix: (f64, f64, f64, f64),
    alpha: bool,
    gray: bool,
    background: (u8, u8, u8, u8),
    annotations: bool,
    hinting: bool,
    glyph_cache: bool,
    format: &str,
    quality: u8,
    workers: usize,
    max_size: Option<usize>,
) -> PyResult<Vec<Py<PyBytes>>> {
    crate::errors::boundary(|| {
        let encoding =
            Encoding::parse(format, quality, gray, alpha).map_err(PyValueError::new_err)?;
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
            hinting,
            glyph_cache,
        };
        let written = AtomicUsize::new(0);
        let fallback = document.fallback();
        let encoded = py.detach(|| -> PyResult<Vec<Vec<u8>>> {
            let bridged = document
                .bridge
                .get_for_render(&version, &index, &budget, annotations)
                .map_err(|e| bridge_error(&e))?;
            render::batch(&bridged, &fallback, &indexes, &options, workers, |raster| {
                let mut out = Bounded::new(&written, max_size);
                let layout = Layout {
                    width: u32::from(raster.width),
                    height: u32::from(raster.height),
                    gray: raster.gray,
                    alpha: raster.alpha,
                };
                let result = encode(
                    &mut out,
                    layout,
                    &raster.data,
                    raster.resolution,
                    encoding,
                    &budget,
                );
                if out.exceeded() {
                    let limit = max_size.unwrap_or_default();
                    return Err(Failed(LimitError::new_err((
                        "render_output_size",
                        format!("the rendered batch exceeds the {limit}-byte encoded-output limit"),
                    ))));
                }
                result
                    .map_err(|e| Failed(encode_error(e, "failed to encode the rendered image")))?;
                Ok(out.into_bytes())
            })
            .map_err(|Failed(error)| error)
        })?;
        Ok(encoded
            .into_iter()
            .map(|bytes| PyBytes::new(py, &bytes).unbind())
            .collect())
    })?
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
