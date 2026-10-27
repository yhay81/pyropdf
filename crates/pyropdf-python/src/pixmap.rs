//! `Pixmap`, pylopdf's immutable raster with the buffer protocol and PNG
//! and JPEG output (pylopdf rust/src/pixmap.rs at 884af0db and 036be111),
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
use pyo3::exceptions::{PyBufferError, PyTypeError, PyValueError};
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyInt};
use pyropdf_engine::bridge::Error as BridgeError;
use pyropdf_engine::ocr;
use pyropdf_engine::render::{self, Raster, RasterError};
use pyropdf_store::value::Reference;

use crate::document::_Document;
use crate::errors::{EncryptedDocumentError, LimitError, PdfError, StaleObjectError, store_error};

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
    fn channels(&self) -> u32 {
        u32::from(!self.gray) * 2 + 1 + u32::from(self.alpha)
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
}

impl Encoding {
    /// Reads `format`, `"png"` or `"jpeg"` at `quality`, for samples with
    /// or without `alpha`, which JPEG cannot hold.
    fn parse(format: &str, quality: u8, alpha: bool) -> PyResult<Self> {
        match format {
            "png" => Ok(Self::Png),
            "jpeg" if alpha => Err(PyValueError::new_err(
                "JPEG has no alpha channel; render without alpha or write PNG",
            )),
            "jpeg" => Ok(Self::Jpeg(quality)),
            _ => Err(PyValueError::new_err(format!(
                "format must be 'png' or 'jpeg': {format:?}"
            ))),
        }
    }

    const fn name(self) -> &'static str {
        match self {
            Self::Png => "PNG",
            Self::Jpeg(_) => "JPEG",
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

    /// Returns the pixels encoded as `format`, `"png"` or `"jpeg"` at
    /// `quality`, 1 through 100. JPEG has no alpha.
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
        let encoding = Encoding::parse(format, quality, self.alpha)?;
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

    /// Writes the pixels as `format`, `"png"` or `"jpeg"` at `quality`, or
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
                    "cannot tell the image format of {}; pass format='png' or format='jpeg'",
                    path.display()
                ))
            })?,
        };
        let encoding = Encoding::parse(format, quality, self.alpha)?;
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
            .get(&version, &index, &budget)
            .map_err(|e| bridge_error(&e))?;
        render::raster(&bridged, at, &options).map_err(raster_error)
    })?;
    Ok(Pixmap::from(raster))
}

fn raster_error(error: RasterError) -> PyErr {
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
    let encoding = Encoding::parse(format, quality, alpha)?;
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
            .get(&version, &index, &budget)
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
