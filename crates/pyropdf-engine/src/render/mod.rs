//! Rasterizing a page through hayro, as much as `page.pixmap`
//! (yhay81/pyropdf#35) needs, and converting it to SVG.

use hayro::hayro_interpret::InterpreterSettings;
use hayro::vello_cpu::color::AlphaColor;
use hayro::{PixmapSettings, RenderCache, RenderSettings};

use std::cell::RefCell;
use std::sync::Arc;

use hayro::hayro_syntax::Pdf;

use crate::bridge::{Bridged, Error, Result};

mod batch;
mod svg;
#[cfg(test)]
mod tests;

pub use batch::{MAX_WORKERS, batch};
pub use svg::svg;

/// A rendered page: its size in pixels and its pixels, row by row from the
/// top, as premultiplied RGBA with 8 bits per channel.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Rgba {
    /// The width in pixels.
    pub width: u16,
    /// The height in pixels.
    pub height: u16,
    /// Four bytes per pixel.
    pub data: Vec<u8>,
}

/// Renders page `index`, counted from 0, at `scale` pixels per point, on a
/// transparent background.
pub fn page(bridged: &Bridged, index: usize, scale: f32) -> Result<Rgba> {
    let pages = bridged.pdf().pages();
    let page = pages.get(index).ok_or(Error::NoPage(index))?;
    let pixmap = hayro::render(
        page,
        &RenderCache::new(),
        &InterpreterSettings::default(),
        &RenderSettings::default(),
        &PixmapSettings {
            x_scale: scale,
            y_scale: scale,
            bg_color: AlphaColor::TRANSPARENT,
        },
    );
    let data = pixmap
        .data()
        .iter()
        .flat_map(|pixel| [pixel.r, pixel.g, pixel.b, pixel.a])
        .collect();
    Ok(Rgba {
        width: pixmap.width(),
        height: pixmap.height(),
        data,
    })
}

/// The linear part `(a, b, c, d)` of a render matrix in the row-vector
/// convention of PDF: display point `(x, y)` maps to pixel
/// `(a x + c y, b x + d y)`, so the identity renders at 72 dpi.
pub type Matrix = (f64, f64, f64, f64);

/// The most pixels of one raster, as pylopdf bounds it.
pub const MAX_PIXELS: u64 = 64_000_000;

/// The widest raster vello_cpu 0.3 renders: a wider one overflows `u16`
/// when a filled span is rounded up to 128 pixels
/// (`TileAlignedSpan::from_tiles`). Raise it to 65,535 when a vello_cpu
/// release checks that overflow (issue 392).
pub const MAX_WIDTH: u16 = 65_408;

/// The tallest raster vello_cpu 0.3 renders: a taller one overflows `u16`
/// when `RenderContext::new` rounds it up to a whole tile of 4 pixels.
pub const MAX_HEIGHT: u16 = 65_532;

/// How far a page edge may fall short of a pixel boundary through
/// floating-point error and still cover that pixel, as MuPDF's
/// `fz_round_rect` allows.
const EDGE_TOLERANCE: f64 = 0.001;

/// A rendered page as pylopdf's `Pixmap` holds it: straight-alpha RGBA, or
/// RGB without alpha, or their gray counterparts, row by row from the top.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Raster {
    /// The width in pixels.
    pub width: u16,
    /// The height in pixels.
    pub height: u16,
    /// Whether each pixel ends with an alpha byte.
    pub alpha: bool,
    /// Whether each pixel holds one gray byte instead of three color bytes.
    pub gray: bool,
    /// One to four bytes per pixel.
    pub data: Vec<u8>,
    /// The dots per inch along each axis, which encoded images record.
    pub resolution: (u32, u32),
}

/// How [`raster`] renders a page: pylopdf's options of `page.pixmap`.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Options {
    /// The linear map from display points to pixels.
    pub matrix: Matrix,
    /// Whether the raster keeps a straight alpha channel.
    pub alpha: bool,
    /// Whether the raster holds the ITU-R BT.601 luma of each color.
    pub gray: bool,
    /// The straight RGBA color beneath the page, which must be opaque
    /// without `alpha`.
    pub background: [u8; 4],
    /// The display rectangle `(x0, y0, x1, y1)` to rasterize, or the whole
    /// page.
    pub clip: Option<(f64, f64, f64, f64)>,
    /// Whether annotation appearances are drawn.
    pub annotations: bool,
}

impl Options {
    /// Renders through `matrix` in RGB, on white without `alpha` and on
    /// transparency with it, with annotations and no clip.
    pub const fn new(matrix: Matrix, alpha: bool) -> Self {
        Self {
            matrix,
            alpha,
            gray: false,
            background: if alpha { [0; 4] } else { [255; 4] },
            clip: None,
            annotations: true,
        }
    }
}

/// Why a raster was refused.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RasterError {
    /// The document has no such page.
    NoPage(usize),
    /// The matrix or the page size gives no raster, or one above the
    /// limits: [`MAX_WIDTH`], [`MAX_HEIGHT`], and [`MAX_PIXELS`]; or the
    /// options are inconsistent.
    Geometry(String),
    /// The clip leaves no pixel of the page.
    Clip(String),
}

impl std::fmt::Display for RasterError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NoPage(index) => write!(f, "the document has no page {index}"),
            Self::Geometry(message) | Self::Clip(message) => f.write_str(message),
        }
    }
}

impl std::error::Error for RasterError {}

/// The render cache of the bridge that this thread rendered last: fonts,
/// images, and glyph outlines, which every page of one version shares, as
/// pylopdf keeps one cache per snapshot. hayro's cache is not `Send`, so
/// each thread keeps its own, and the bridge that it borrows from, until it
/// renders another version.
struct Cached {
    // Declared first, so that it drops before the bridge that it borrows.
    cache: RenderCache<'static>,
    bridged: Arc<Bridged>,
}

thread_local! {
    static CACHE: RefCell<Option<Cached>> = const { RefCell::new(None) };
}

/// Runs `render` with hayro's document of `bridged` and this thread's cache
/// for it.
fn with_cache<T>(
    bridged: &Arc<Bridged>,
    render: impl FnOnce(&'static Pdf, &RenderCache<'static>) -> T,
) -> T {
    CACHE.with(|cell| {
        let mut held = cell.borrow_mut();
        if !held
            .as_ref()
            .is_some_and(|cached| Arc::ptr_eq(&cached.bridged, bridged))
        {
            // Drop the old cache before its bridge, then start one for this bridge.
            *held = None;
            *held = Some(Cached {
                cache: RenderCache::new(),
                bridged: Arc::clone(bridged),
            });
        }
        let cached = held.as_ref().expect("the cache was just set");
        // SAFETY: the cache borrows from this `Pdf`, which `cached.bridged`
        // keeps alive and never moves or changes; the cache drops first.
        // Neither reference outlives this call.
        let pdf: &'static Pdf = unsafe { &*std::ptr::from_ref(cached.bridged.pdf()) };
        render(pdf, &cached.cache)
    })
}

/// Renders page `index` as pylopdf's `page.pixmap` renders it: the raster
/// covers the whole pixels inside the page transformed by the matrix, or
/// those of them inside the clip.
pub fn raster(
    bridged: &Arc<Bridged>,
    index: usize,
    options: &Options,
) -> std::result::Result<Raster, RasterError> {
    with_cache(bridged, |pdf, cache| draw(pdf, cache, index, options))
}

/// The smallest rectangle `(x0, y0, x1, y1)` holding `corners`.
fn bounds(corners: [hayro::kurbo::Point; 4]) -> (f64, f64, f64, f64) {
    let (mut x0, mut y0, mut x1, mut y1) = (f64::MAX, f64::MAX, f64::MIN, f64::MIN);
    for corner in corners {
        x0 = x0.min(corner.x);
        y0 = y0.min(corner.y);
        x1 = x1.max(corner.x);
        y1 = y1.max(corner.y);
    }
    (x0, y0, x1, y1)
}

/// The pixels of a `width` by `height` raster that the display rectangle
/// `clip` covers under `linear`: its bounding box rounded outward to pixel
/// boundaries and intersected with the raster, as an origin and a size.
fn clip_viewport(
    linear: hayro::kurbo::Affine,
    width: u16,
    height: u16,
    clip: (f64, f64, f64, f64),
) -> std::result::Result<(f64, f64, u16, u16), RasterError> {
    use hayro::kurbo::Point;

    let (cx0, cy0, cx1, cy1) = clip;
    if ![cx0, cy0, cx1, cy1].into_iter().all(f64::is_finite) || cx0 >= cx1 || cy0 >= cy1 {
        return Err(RasterError::Geometry(
            "clip must be a finite rectangle with x0 < x1 and y0 < y1".to_owned(),
        ));
    }
    let corners =
        [(cx0, cy0), (cx1, cy0), (cx0, cy1), (cx1, cy1)].map(|(x, y)| linear * Point::new(x, y));
    let (x0, y0, x1, y1) = bounds(corners);
    let (width, height) = (f64::from(width), f64::from(height));
    let x0 = x0.floor().clamp(0.0, width);
    let y0 = y0.floor().clamp(0.0, height);
    let x1 = x1.ceil().clamp(0.0, width);
    let y1 = y1.ceil().clamp(0.0, height);
    if x0 >= x1 || y0 >= y1 {
        return Err(RasterError::Clip(
            "clip does not intersect the rendered page".to_owned(),
        ));
    }
    // Both extents lie within the u16 raster, so the casts are exact.
    Ok((x0, y0, (x1 - x0) as u16, (y1 - y0) as u16))
}

/// The whole pixels that `page` covers through the matrix of `options`:
/// the raster's offset from the transformed page's origin and its size,
/// refused past the limits of one raster.
fn extent(
    page: &hayro::hayro_syntax::page::Page<'_>,
    options: &Options,
) -> std::result::Result<(f64, f64, f64, f64), RasterError> {
    use hayro::kurbo::{Affine, Point};

    let (a, b, c, d) = options.matrix;
    if ![a, b, c, d].into_iter().all(f64::is_finite) || a * d - b * c == 0.0 {
        return Err(RasterError::Geometry(
            "matrix must be finite and invertible".to_owned(),
        ));
    }
    if !options.alpha && options.background[3] != u8::MAX {
        return Err(RasterError::Geometry(
            "alpha=False needs an opaque background".to_owned(),
        ));
    }
    let (width, height) = page.render_dimensions();
    let linear = Affine::new([a, b, c, d, 0.0, 0.0]);
    let corners = [
        (0.0, 0.0),
        (f64::from(width), 0.0),
        (0.0, f64::from(height)),
        (f64::from(width), f64::from(height)),
    ]
    .map(|(x, y)| linear * Point::new(x, y));
    let (x0, y0, x1, y1) = bounds(corners);
    let (x0, y0) = ((x0 - EDGE_TOLERANCE).ceil(), (y0 - EDGE_TOLERANCE).ceil());
    let pixel_width = (x1 + EDGE_TOLERANCE).floor() - x0;
    let pixel_height = (y1 + EDGE_TOLERANCE).floor() - y0;
    if !pixel_width.is_finite()
        || !pixel_height.is_finite()
        || pixel_width < 1.0
        || pixel_height < 1.0
    {
        return Err(RasterError::Geometry(
            "scale is too small, or the PDF page size is invalid".to_owned(),
        ));
    }
    if pixel_width > f64::from(MAX_WIDTH) || pixel_height > f64::from(MAX_HEIGHT) {
        return Err(RasterError::Geometry(format!(
            "render size {pixel_width:.0}x{pixel_height:.0} exceeds the limit of \
             {MAX_WIDTH} pixels wide and {MAX_HEIGHT} pixels high"
        )));
    }
    let pixels = (pixel_width as u64) * (pixel_height as u64);
    if pixels > MAX_PIXELS {
        return Err(RasterError::Geometry(format!(
            "render size {pixel_width:.0}x{pixel_height:.0} ({pixels} pixels) exceeds the {MAX_PIXELS}-pixel limit"
        )));
    }
    Ok((x0, y0, pixel_width, pixel_height))
}

fn draw<'a>(
    pdf: &'a Pdf,
    cache: &RenderCache<'a>,
    index: usize,
    options: &Options,
) -> std::result::Result<Raster, RasterError> {
    use hayro::hayro_interpret::TransformExt;
    use hayro::kurbo::Affine;
    use hayro::vello_cpu::{RasterizerSettings, RenderContext, Resources, TargetInit};

    let pages = pdf.pages();
    let page = pages.get(index).ok_or(RasterError::NoPage(index))?;
    let (x0, y0, pixel_width, pixel_height) = extent(page, options)?;
    let (a, b, c, d) = options.matrix;
    let linear = Affine::new([a, b, c, d, 0.0, 0.0]);
    let page_to_raster = Affine::translate((-x0, -y0)) * linear;
    let (left, top, raster_width, raster_height) = match options.clip {
        Some(clip) => clip_viewport(
            page_to_raster,
            pixel_width as u16,
            pixel_height as u16,
            clip,
        )?,
        None => (0.0, 0.0, pixel_width as u16, pixel_height as u16),
    };
    let transform =
        Affine::translate((-left, -top)) * page_to_raster * page.initial_transform(true).to_kurbo();
    let settings = InterpreterSettings {
        render_annotations: options.annotations,
        ..InterpreterSettings::default()
    };
    let mut context = RenderContext::new(raster_width, raster_height);
    hayro::render_into(
        page,
        cache,
        &settings,
        &RenderSettings::default(),
        &mut context,
        transform,
    );
    context.flush();
    let [red, green, blue, opacity] = options.background;
    let mut pixmap = hayro::vello_cpu::Pixmap::new(raster_width, raster_height);
    context.render_with(
        &mut pixmap,
        &mut Resources::default(),
        RasterizerSettings {
            target_init: TargetInit::Clear(AlphaColor::from_rgba8(red, green, blue, opacity)),
            ..Default::default()
        },
    );
    let data = take_samples(pixmap, options.gray, options.alpha);
    Ok(Raster {
        width: raster_width,
        height: raster_height,
        alpha: options.alpha,
        gray: options.gray,
        data,
        resolution: resolution(options.matrix),
    })
}

/// Takes the straight-alpha samples of `pixmap` in the layout of `gray` and
/// `alpha`. RGB without alpha goes through vello_cpu's SIMD narrowing, which
/// returns RGB whenever the pixmap is opaque, as an opaque background leaves
/// it; anything else narrows through `to_layout`.
fn take_samples(pixmap: hayro::vello_cpu::Pixmap, gray: bool, alpha: bool) -> Vec<u8> {
    use hayro::vello_cpu::Pixels;
    use hayro::vello_cpu::peniko::ImageAlphaType;

    let mut data = if !gray && !alpha {
        match pixmap.try_take_rgb8(ImageAlphaType::Alpha) {
            Pixels::Rgb8(data) => return data,
            Pixels::Rgba8(data) => data,
        }
    } else {
        pixmap.take_rgba8(ImageAlphaType::Alpha)
    };
    to_layout(&mut data, gray, alpha);
    data
}

/// The ITU-R BT.601 luma of an RGBA pixel's color, rounded, as pylopdf
/// computes it.
fn luma(pixel: &[u8]) -> u8 {
    ((77 * u32::from(pixel[0]) + 150 * u32::from(pixel[1]) + 29 * u32::from(pixel[2]) + 128) >> 8)
        as u8
}

/// Narrows straight-alpha RGBA samples in place: without `alpha` the alpha
/// bytes go, and with `gray` the color components become their luma. Each
/// pixel is read before its narrower replacement is written, which never
/// reaches a later pixel.
fn to_layout(data: &mut Vec<u8>, gray: bool, alpha: bool) {
    let count = data.len() / 4;
    let channels = usize::from(!gray) * 2 + 1 + usize::from(alpha);
    if channels == 4 {
        return;
    }
    for at in 0..count {
        let pixel: [u8; 4] = data[at * 4..at * 4 + 4]
            .try_into()
            .expect("four bytes per pixel");
        let out = &mut data[at * channels..at * channels + channels];
        if gray {
            out[0] = luma(&pixel);
        } else {
            out[..3].copy_from_slice(&pixel[..3]);
        }
        if alpha {
            out[channels - 1] = pixel[3];
        }
    }
    data.truncate(count * channels);
}

/// The pixels per inch of the page along each raster axis, rounded, from 1
/// through 65,535: a diagonal matrix gives `72 a` by `72 d`, and a quarter
/// turn keeps the resolution of the upright page.
fn resolution((a, b, c, d): Matrix) -> (u32, u32) {
    let determinant = (a * d - b * c).abs();
    let per_inch = |length: f64| (72.0 * determinant / length).round().clamp(1.0, 65_535.0) as u32;
    (per_inch(b.hypot(d)), per_inch(a.hypot(c)))
}
