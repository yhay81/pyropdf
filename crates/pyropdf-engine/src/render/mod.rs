//! Rasterizing a page through hayro, as much as `page.pixmap`
//! (yhay81/pyropdf#35) needs.

use hayro::hayro_interpret::InterpreterSettings;
use hayro::vello_cpu::color::AlphaColor;
use hayro::{PixmapSettings, RenderCache, RenderSettings};

use std::cell::RefCell;
use std::sync::Arc;

use hayro::hayro_syntax::Pdf;

use crate::bridge::{Bridged, Error, Result};

#[cfg(test)]
mod tests;

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

/// How far a page edge may fall short of a pixel boundary through
/// floating-point error and still cover that pixel, as MuPDF's
/// `fz_round_rect` allows.
const EDGE_TOLERANCE: f64 = 0.001;

/// A rendered page as pylopdf's `Pixmap` holds it: straight-alpha RGBA, or
/// RGB on white without alpha, row by row from the top.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Raster {
    /// The width in pixels.
    pub width: u16,
    /// The height in pixels.
    pub height: u16,
    /// Whether each pixel ends with an alpha byte.
    pub alpha: bool,
    /// Three or four bytes per pixel.
    pub data: Vec<u8>,
    /// The dots per inch along each axis, which encoded images record.
    pub resolution: (u32, u32),
}

/// Why a raster was refused.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RasterError {
    /// The document has no such page.
    NoPage(usize),
    /// The matrix or the page size gives no raster, or one above the
    /// limits: 65,535 pixels a side and [`MAX_PIXELS`] in all.
    Geometry(String),
}

impl std::fmt::Display for RasterError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NoPage(index) => write!(f, "the document has no page {index}"),
            Self::Geometry(message) => f.write_str(message),
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

/// Renders page `index` through `matrix`, as pylopdf's `page.pixmap`
/// renders it: the raster covers the whole pixels inside the transformed
/// page, on white without `alpha` and on transparency with it.
pub fn raster(
    bridged: &Arc<Bridged>,
    index: usize,
    matrix: Matrix,
    alpha: bool,
) -> std::result::Result<Raster, RasterError> {
    with_cache(bridged, |pdf, cache| draw(pdf, cache, index, matrix, alpha))
}

fn draw<'a>(
    pdf: &'a Pdf,
    cache: &RenderCache<'a>,
    index: usize,
    matrix: Matrix,
    alpha: bool,
) -> std::result::Result<Raster, RasterError> {
    use hayro::hayro_interpret::TransformExt;
    use hayro::kurbo::{Affine, Point};
    use hayro::vello_cpu::peniko::ImageAlphaType;
    use hayro::vello_cpu::{RasterizerSettings, RenderContext, Resources, TargetInit};

    let pages = pdf.pages();
    let page = pages.get(index).ok_or(RasterError::NoPage(index))?;
    let (a, b, c, d) = matrix;
    if ![a, b, c, d].into_iter().all(f64::is_finite) || a * d - b * c == 0.0 {
        return Err(RasterError::Geometry(
            "matrix must be finite and invertible".to_owned(),
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
    let (mut x0, mut y0, mut x1, mut y1) = (f64::MAX, f64::MAX, f64::MIN, f64::MIN);
    for corner in corners {
        x0 = x0.min(corner.x);
        y0 = y0.min(corner.y);
        x1 = x1.max(corner.x);
        y1 = y1.max(corner.y);
    }
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
    if pixel_width > f64::from(u16::MAX) || pixel_height > f64::from(u16::MAX) {
        return Err(RasterError::Geometry(format!(
            "render size {pixel_width:.0}x{pixel_height:.0} exceeds the 65535-pixel limit per side"
        )));
    }
    let pixels = (pixel_width as u64) * (pixel_height as u64);
    if pixels > MAX_PIXELS {
        return Err(RasterError::Geometry(format!(
            "render size {pixel_width:.0}x{pixel_height:.0} ({pixels} pixels) exceeds the {MAX_PIXELS}-pixel limit"
        )));
    }
    let (raster_width, raster_height) = (pixel_width as u16, pixel_height as u16);
    let transform =
        Affine::translate((-x0, -y0)) * linear * page.initial_transform(true).to_kurbo();
    let mut context = RenderContext::new(raster_width, raster_height);
    hayro::render_into(
        page,
        cache,
        &InterpreterSettings::default(),
        &RenderSettings::default(),
        &mut context,
        transform,
    );
    context.flush();
    let background = if alpha {
        AlphaColor::TRANSPARENT
    } else {
        AlphaColor::from_rgba8(255, 255, 255, 255)
    };
    let mut pixmap = hayro::vello_cpu::Pixmap::new(raster_width, raster_height);
    context.render_with(
        &mut pixmap,
        &mut Resources::default(),
        RasterizerSettings {
            target_init: TargetInit::Clear(background),
            ..Default::default()
        },
    );
    let mut data = pixmap.take_rgba8(ImageAlphaType::Alpha);
    if !alpha {
        let count = data.len() / 4;
        for at in 0..count {
            data.copy_within(at * 4..at * 4 + 3, at * 3);
        }
        data.truncate(count * 3);
    }
    Ok(Raster {
        width: raster_width,
        height: raster_height,
        alpha,
        data,
        resolution: resolution(matrix),
    })
}

/// The pixels per inch of the page along each raster axis, rounded, from 1
/// through 65,535: a diagonal matrix gives `72 a` by `72 d`, and a quarter
/// turn keeps the resolution of the upright page.
fn resolution((a, b, c, d): Matrix) -> (u32, u32) {
    let determinant = (a * d - b * c).abs();
    let per_inch = |length: f64| (72.0 * determinant / length).round().clamp(1.0, 65_535.0) as u32;
    (per_inch(b.hypot(d)), per_inch(a.hypot(c)))
}
