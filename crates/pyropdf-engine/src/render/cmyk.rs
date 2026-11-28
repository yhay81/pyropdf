//! Native two-pass CMYK rasterization through `hayro_interpret::Device` and two
//! `vello_cpu::RenderContext` targets (DESIGN.md §6 workaround for #425 and #433).
//!
//! Interpreting a page once feeds two viewport-sized RGBA8 `RenderContext`
//! buffers simultaneously:
//!
//! - Pass 1 holds complemented `(1 - C, 1 - M, 1 - Y, alpha)`.
//! - Pass 2 holds complemented `(1 - K, 1 - K, 1 - K, alpha)`.
//!
//! `DeviceCMYK` paints, strokes, text, shadings, and images write directly into
//! the two targets without an sRGB round-trip; `DeviceGray` maps to pure K
//! `(0, 0, 0, 1 - gray)`; RGB, calibrated, Lab, and ICC-based colors convert to
//! CMYK with full undercolor removal and black generation. Because src-over
//! compositing, coverage, opacity, and PDF's separable blend modes operate
//! independently on complemented subtractive channels (ISO 32000-2), merging
//! the two rendered targets yields exact packed 4-channel `CMYK` (or 5-channel
//! straight-alpha `CMYKA`) samples.

use std::cell::RefCell;
use std::collections::HashMap;
use std::f64::consts::SQRT_2;
use std::rc::Rc;
use std::sync::Arc;

use hayro::hayro_interpret::color::{Color, ColorSpace};
use hayro::hayro_interpret::encode::texture_dimensions;
use hayro::hayro_interpret::font::{Glyph, GlyphRun, HintingCache, OutlineGlyph};
use hayro::hayro_interpret::hayro_cmap::WritingMode;
use hayro::hayro_interpret::pattern::{Pattern, ShadingPattern, TilingPattern};
use hayro::hayro_interpret::shading::{ShadingFunction, ShadingType};
use hayro::hayro_interpret::util::{RectExt, x_y_advances};
use hayro::hayro_interpret::{
    BlendMode, CacheKey, ClipPath, Context, Device, DrawMode, DrawProps, FillRule, ImageData,
    ImageDrawProps, InterpreterCache, InterpreterSettings, LumaData, MaskType, Paint, RasterImage,
    SoftMask, StrokeProps, interpret_page,
};
use hayro::hayro_syntax::bit_reader::BitReader;
use hayro::hayro_syntax::object::dict::keys::{
    BITS_PER_COMPONENT, BPC, CALCMYK, CMYK, COLORSPACE, CS, D, DECODE, DEVICE_CMYK, I, INTERPOLATE,
    MASK, MATTE, SMASK, SMASK_IN_DATA,
};
use hayro::hayro_syntax::object::stream::{ImageColorSpace, ImageDecodeParams};
use hayro::hayro_syntax::object::{Array, Name, Object, Stream};
use hayro::hayro_syntax::page::Page;
use hayro::kurbo::{Affine, BezPath, Cap, Join, PathEl, Point, Rect, Shape, Vec2};
use hayro::vello_cpu::color::{AlphaColor, Srgb};
use hayro::vello_cpu::peniko::{
    self, Compose, Fill, ImageAlphaType, ImageQuality, ImageSampler, Mix,
};
use hayro::vello_cpu::{
    Image as VelloImage, ImageSource, Mask, PaintType, PixelMetadata, Pixmap, RasterizerSettings,
    RenderContext, RenderSettings, Resources, TargetInit,
};
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};

use crate::display::Display;
use crate::pixmap::Colorspace;

use super::{ALIASED_THRESHOLD, Options, Raster, RasterError, extent, resolution};

/// Minimum ppem at which horizontal outline text is hinted, matching `hayro`.
const MIN_HINTING_PPEM: f64 = 4.0;
/// Maximum ppem at which horizontal outline text is hinted, matching `hayro`.
const MAX_HINTING_PPEM: f64 = 128.0;
/// Maximum cached glyph outlines retained across CMYK renders of one version.
const MAX_OUTLINE_CACHE: usize = 4_096;
/// Maximum cached soft masks retained during one CMYK render.
const MAX_SOFT_MASK_CACHE: usize = 64;
/// Maximum PDF function output components supported by `hayro-interpret`.
const MAX_FUNC_COMPONENTS: usize = 32;
/// How many drawing operations run between budget deadline/cancellation polls.
const POLL_INTERVAL: u32 = 256;
/// Estimated temporary memory per pixel for two `RenderContext`s and two RGBA8 `Pixmap`s.
const TEMP_BYTES_PER_PIXEL: u64 = 16;

/// Bounded glyph outline cache keyed by `(glyph_id, ppem_bits)`.
type OutlineCache = HashMap<(u128, u32), Option<Rc<BezPath>>>;

/// Per-version cache reused across CMYK renders of one bridged document.
pub(super) struct CmykCache<'a> {
    interpreter_cache: InterpreterCache<'a>,
    outline_cache: RefCell<OutlineCache>,
    hinting_cache: HintingCache,
}

impl<'a> CmykCache<'a> {
    pub(super) fn new() -> Self {
        Self {
            interpreter_cache: InterpreterCache::new(),
            outline_cache: RefCell::new(HashMap::new()),
            hinting_cache: HintingCache::new(),
        }
    }
}

/// Releases a temporary `LimitCode::MemorySize` charge when dropped.
struct MemoryGuard<'b> {
    budget: &'b Budget,
    bytes: u64,
}

impl<'b> MemoryGuard<'b> {
    fn charge(budget: &'b Budget, bytes: u64) -> Result<Self, LimitExceeded> {
        budget.charge(LimitCode::MemorySize, bytes)?;
        Ok(Self { budget, bytes })
    }
}

impl Drop for MemoryGuard<'_> {
    fn drop(&mut self) {
        if self.bytes > 0 {
            self.budget.release(LimitCode::MemorySize, self.bytes);
        }
    }
}

/// Renders `page` directly into packed 4-channel `CMYK` (or 5-channel `CMYKA`)
/// samples covering only the requested viewport.
pub(super) fn render_cmyk<'a>(
    page: &'a Page<'a>,
    display: Display,
    cache: &CmykCache<'a>,
    settings: &InterpreterSettings,
    options: &Options,
    budget: &Budget,
) -> Result<Raster, RasterError> {
    budget.poll()?;
    let (left, top, raster_width, raster_height) = extent(display, options)?;
    let pixels = u64::from(raster_width) * u64::from(raster_height);
    let out_channels = 4 + u64::from(options.alpha);
    let out_bytes = pixels
        .checked_mul(out_channels)
        .ok_or_else(|| LimitExceeded::new(LimitCode::MemorySize))?;
    let temp_bytes = pixels
        .checked_mul(TEMP_BYTES_PER_PIXEL)
        .ok_or_else(|| LimitExceeded::new(LimitCode::MemorySize))?;

    // Check peak memory (both RenderContexts + both Pixmaps + output buffer)
    // before allocating any raster buffer.
    let temp_guard = MemoryGuard::charge(budget, temp_bytes)?;
    let mut out = BudgetVec::new(budget);
    let out_len =
        usize::try_from(out_bytes).map_err(|_| LimitExceeded::new(LimitCode::MemorySize))?;
    out.try_reserve_exact(out_len)?;

    let (a, b, c, d) = options.matrix;
    let transform = Affine::translate((-left, -top))
        * Affine::new([a, b, c, d, 0.0, 0.0])
        * display.transform();
    let target = Rect::new(0.0, 0.0, f64::from(raster_width), f64::from(raster_height));
    let aliasing_threshold = (!options.antialias).then_some(ALIASED_THRESHOLD);

    let mut cmy_ctx = RenderContext::new(raster_width, raster_height);
    let mut k_ctx = RenderContext::new(raster_width, raster_height);
    cmy_ctx.set_aliasing_threshold(aliasing_threshold);
    k_ctx.set_aliasing_threshold(aliasing_threshold);

    let mut state = Context::new(
        transform,
        target,
        &cache.interpreter_cache,
        page.xref(),
        settings.clone(),
    );

    let mut device = CmykDevice {
        cache,
        cmy_ctx: &mut cmy_ctx,
        k_ctx: &mut k_ctx,
        budget,
        aliasing_threshold,
        hinting: options.hinting,
        inside_pattern: false,
        in_type3_glyph: false,
        soft_mask_cache: HashMap::new(),
        ops: 0,
        error: None,
    };

    let crop_box = page.intersected_crop_box().to_kurbo();
    let [ta, tb, tc, td, _, _] = transform.as_coeffs();
    let axis_aligned = (tb == 0.0 && tc == 0.0) || (ta == 0.0 && td == 0.0);
    let mapped_crop_box = transform
        .transform_rect_bbox(crop_box)
        .inflate(1.0 / 256.0, 1.0 / 256.0);
    let clip_crop_box = !axis_aligned || !mapped_crop_box.contains_rect(target);

    if clip_crop_box {
        device.cmy_ctx.set_transform(transform);
        device.cmy_ctx.push_clip_rect(&crop_box);
        device.k_ctx.set_transform(transform);
        device.k_ctx.push_clip_rect(&crop_box);
    }

    interpret_page(page, &mut state, &mut device);

    if clip_crop_box {
        device.pop_clip();
    }

    if let Some(limit_error) = device.error.take() {
        return Err(RasterError::Limit(limit_error));
    }
    drop(device);

    budget.poll()?;

    let (cmy_bg, k_bg) = background_to_lanes(options.background);

    cmy_ctx.flush();
    let mut cmy_pixmap = Pixmap::new(raster_width, raster_height);
    cmy_ctx.render_with(
        &mut cmy_pixmap,
        &mut Resources::default(),
        RasterizerSettings {
            target_init: TargetInit::Clear(cmy_bg),
            ..Default::default()
        },
    );
    drop(cmy_ctx);

    budget.poll()?;

    k_ctx.flush();
    let mut k_pixmap = Pixmap::new(raster_width, raster_height);
    k_ctx.render_with(
        &mut k_pixmap,
        &mut Resources::default(),
        RasterizerSettings {
            target_init: TargetInit::Clear(k_bg),
            ..Default::default()
        },
    );
    drop(k_ctx);

    budget.poll()?;

    let cmy_rgba = cmy_pixmap.take_rgba8(ImageAlphaType::Alpha);
    let k_rgba = k_pixmap.take_rgba8(ImageAlphaType::Alpha);

    merge_lanes(&cmy_rgba, &k_rgba, options.alpha, &mut out, budget)?;

    drop(cmy_rgba);
    drop(k_rgba);
    drop(temp_guard);

    Ok(Raster {
        width: raster_width,
        height: raster_height,
        alpha: options.alpha,
        colorspace: Colorspace::Cmyk,
        data: out.into_vec(),
        resolution: resolution(options.matrix),
    })
}

/// Merges straight-alpha complemented `(1-C, 1-M, 1-Y, A)` and `(1-K, _, _, A)`
/// buffers into packed `CMYK` or `CMYKA` samples.
fn merge_lanes(
    cmy_rgba: &[u8],
    k_rgba: &[u8],
    alpha: bool,
    out: &mut BudgetVec<'_, u8>,
    budget: &Budget,
) -> Result<(), LimitExceeded> {
    for (index, (cmy, k)) in cmy_rgba
        .as_chunks::<4>()
        .0
        .iter()
        .zip(k_rgba.as_chunks::<4>().0.iter())
        .enumerate()
    {
        if index % 65_536 == 0 {
            budget.poll()?;
        }
        if alpha {
            let a = cmy[3];
            if a == 0 {
                out.try_extend_from_slice(&[0, 0, 0, 0, 0])?;
            } else {
                out.try_extend_from_slice(&[
                    255 - cmy[0],
                    255 - cmy[1],
                    255 - cmy[2],
                    255 - k[0],
                    a,
                ])?;
            }
        } else {
            out.try_extend_from_slice(&[255 - cmy[0], 255 - cmy[1], 255 - cmy[2], 255 - k[0]])?;
        }
    }
    Ok(())
}

/// Converts straight RGBA background bytes to complemented CMY and K clear colors.
fn background_to_lanes([r, g, b, opacity]: [u8; 4]) -> (AlphaColor<Srgb>, AlphaColor<Srgb>) {
    if opacity == 0 {
        return (AlphaColor::TRANSPARENT, AlphaColor::TRANSPARENT);
    }
    let [c, m, y, k] = rgb_u8_to_cmyk([r, g, b]);
    (
        AlphaColor::from_rgba8(255 - c, 255 - m, 255 - y, opacity),
        AlphaColor::from_rgba8(255 - k, 255 - k, 255 - k, opacity),
    )
}

/// PDF color space family inspected in O(1) without heap allocation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ColorSpaceKind {
    DeviceCmyk,
    DeviceGray,
    DeviceRgb,
    CalGray,
    Other,
}

fn color_space_kind(space: &ColorSpace) -> ColorSpaceKind {
    use std::fmt::Write as _;

    struct Sniffer {
        buf: [u8; 32],
        len: usize,
        opens: u8,
    }

    impl std::fmt::Write for Sniffer {
        fn write_str(&mut self, s: &str) -> std::fmt::Result {
            for &byte in s.as_bytes() {
                if byte == b'(' {
                    self.opens += 1;
                    if self.opens >= 2 {
                        return Err(std::fmt::Error);
                    }
                }
                if self.len < self.buf.len() {
                    self.buf[self.len] = byte;
                    self.len += 1;
                } else {
                    return Err(std::fmt::Error);
                }
            }
            Ok(())
        }
    }

    let mut sniffer = Sniffer {
        buf: [0; 32],
        len: 0,
        opens: 0,
    };
    let _ = write!(&mut sniffer, "{space:?}");
    let prefix = std::str::from_utf8(&sniffer.buf[..sniffer.len]).unwrap_or("");
    match prefix.strip_prefix("ColorSpace(") {
        Some("DeviceCmyk") => ColorSpaceKind::DeviceCmyk,
        Some("DeviceGray") => ColorSpaceKind::DeviceGray,
        Some("DeviceRgb") => ColorSpaceKind::DeviceRgb,
        Some("CalGray") => ColorSpaceKind::CalGray,
        _ => ColorSpaceKind::Other,
    }
}

#[inline]
fn unit_to_u8(val: f32) -> u8 {
    (val.clamp(0.0, 1.0) * 255.0 + 0.5) as u8
}

/// Converts normalized RGB `[0.0, 1.0]` to CMYK `[0.0, 1.0]` with full
/// undercolor removal and black generation:
/// `k = 1 - max(r, g, b)` and `c = (1 - r - k) / (1 - k)` when `k < 1` else `0`.
/// Pure primaries map cleanly (e.g. `(1, 0, 0) -> (0, 1, 1, 0)`) and neutral
/// grays (`r == g == b`) map to pure K `(0, 0, 0, 1 - r)`.
#[inline]
pub(crate) fn rgb_f32_to_cmyk(r: f32, g: f32, b: f32) -> [f32; 4] {
    let r = r.clamp(0.0, 1.0);
    let g = g.clamp(0.0, 1.0);
    let b = b.clamp(0.0, 1.0);
    let max = r.max(g).max(b);
    let k = 1.0 - max;
    if max <= 0.0 {
        [0.0, 0.0, 0.0, 1.0]
    } else {
        let inv = 1.0 / max;
        [(max - r) * inv, (max - g) * inv, (max - b) * inv, k]
    }
}

/// Integer RGB8 -> CMYK8 matching [`rgb_f32_to_cmyk`].
#[inline]
pub(crate) fn rgb_u8_to_cmyk([r, g, b]: [u8; 3]) -> [u8; 4] {
    let max = r.max(g).max(b);
    let k = 255 - max;
    if max == 0 {
        [0, 0, 0, 255]
    } else {
        let m = u32::from(max);
        let half = m / 2;
        let c = ((u32::from(max - r) * 255 + half) / m) as u8;
        let mag = ((u32::from(max - g) * 255 + half) / m) as u8;
        let y = ((u32::from(max - b) * 255 + half) / m) as u8;
        [c, mag, y, k]
    }
}

/// Converts a PDF [`Color`] into complemented `(cmy_rgba, k_rgba)` lane colors.
fn color_to_lanes(color: &Color) -> ([u8; 4], [u8; 4]) {
    let rgba_f = color.to_rgba();
    let alpha = rgba_f.to_rgba8()[3];
    let comps = color.components();
    let [c, m, y, k] = match color_space_kind(color.color_space()) {
        ColorSpaceKind::DeviceCmyk => [
            unit_to_u8(comps.first().copied().unwrap_or(0.0)),
            unit_to_u8(comps.get(1).copied().unwrap_or(0.0)),
            unit_to_u8(comps.get(2).copied().unwrap_or(0.0)),
            unit_to_u8(comps.get(3).copied().unwrap_or(1.0)),
        ],
        ColorSpaceKind::DeviceGray => {
            let g = comps.first().copied().unwrap_or(0.0).clamp(0.0, 1.0);
            [0, 0, 0, unit_to_u8(1.0 - g)]
        }
        ColorSpaceKind::DeviceRgb => {
            let r = comps.first().copied().unwrap_or(0.0);
            let g = comps.get(1).copied().unwrap_or(0.0);
            let b = comps.get(2).copied().unwrap_or(0.0);
            rgb_f32_to_cmyk(r, g, b).map(unit_to_u8)
        }
        ColorSpaceKind::CalGray => {
            let g = rgba_f.components()[0].clamp(0.0, 1.0);
            [0, 0, 0, unit_to_u8(1.0 - g)]
        }
        ColorSpaceKind::Other => {
            let [r, g, b, _] = rgba_f.components();
            rgb_f32_to_cmyk(r, g, b).map(unit_to_u8)
        }
    };
    (
        [255 - c, 255 - m, 255 - y, alpha],
        [255 - k, 255 - k, 255 - k, alpha],
    )
}

/// Converts raw color-space component values (e.g. from a shading function)
/// into complemented `(cmy_rgba, k_rgba)` straight-alpha bytes.
fn components_to_lanes(
    space: &ColorSpace,
    kind: ColorSpaceKind,
    values: &[f32],
    opacity: f32,
) -> ([u8; 4], [u8; 4]) {
    let alpha = unit_to_u8(opacity);
    let [c, m, y, k] = match kind {
        ColorSpaceKind::DeviceCmyk => [
            unit_to_u8(values.first().copied().unwrap_or(0.0)),
            unit_to_u8(values.get(1).copied().unwrap_or(0.0)),
            unit_to_u8(values.get(2).copied().unwrap_or(0.0)),
            unit_to_u8(values.get(3).copied().unwrap_or(1.0)),
        ],
        ColorSpaceKind::DeviceGray => {
            let g = values.first().copied().unwrap_or(0.0).clamp(0.0, 1.0);
            [0, 0, 0, unit_to_u8(1.0 - g)]
        }
        ColorSpaceKind::DeviceRgb => {
            let r = values.first().copied().unwrap_or(0.0);
            let g = values.get(1).copied().unwrap_or(0.0);
            let b = values.get(2).copied().unwrap_or(0.0);
            rgb_f32_to_cmyk(r, g, b).map(unit_to_u8)
        }
        _ => {
            let [r, g, b, a] = space.to_rgba(values, opacity).components();
            let alpha = unit_to_u8(a);
            let [c, m, y, k] = rgb_f32_to_cmyk(r, g, b).map(unit_to_u8);
            return (
                [255 - c, 255 - m, 255 - y, alpha],
                [255 - k, 255 - k, 255 - k, alpha],
            );
        }
    };
    (
        [255 - c, 255 - m, 255 - y, alpha],
        [255 - k, 255 - k, 255 - k, alpha],
    )
}

/// Dual-target CMYK rasterizing `hayro_interpret::Device`.
struct CmykDevice<'doc, 'r, 'b> {
    cache: &'r CmykCache<'doc>,
    cmy_ctx: &'r mut RenderContext,
    k_ctx: &'r mut RenderContext,
    budget: &'b Budget,
    aliasing_threshold: Option<u8>,
    hinting: bool,
    inside_pattern: bool,
    in_type3_glyph: bool,
    soft_mask_cache: HashMap<u128, Mask>,
    ops: u32,
    error: Option<LimitExceeded>,
}

impl<'doc> CmykDevice<'doc, '_, '_> {
    fn poll_budget(&mut self) -> bool {
        if self.error.is_some() {
            return false;
        }
        self.ops = self.ops.wrapping_add(1);
        if self.ops.is_multiple_of(POLL_INTERVAL)
            && let Err(err) = self.budget.poll()
        {
            self.error = Some(err);
            return false;
        }
        true
    }

    fn record_limit(&mut self, err: LimitExceeded) {
        if self.error.is_none() {
            self.error = Some(err);
        }
    }

    fn misses_target(&self, bbox: Rect) -> bool {
        let target = Rect::new(
            0.0,
            0.0,
            f64::from(self.cmy_ctx.width()),
            f64::from(self.cmy_ctx.height()),
        );
        !bbox.overlaps(target)
    }

    fn child_context(&self, width: u16, height: u16) -> RenderContext {
        let settings = derive_settings(self.cmy_ctx.render_settings());
        let mut ctx = RenderContext::new_with(width, height, settings);
        ctx.set_aliasing_threshold(self.aliasing_threshold);
        ctx
    }

    fn resolve_soft_mask(&mut self, mask: Option<&SoftMask<'doc>>) -> Option<Mask> {
        let m = mask?;
        let key = m.cache_key();
        if let Some(cached) = self.soft_mask_cache.get(&key) {
            return Some(cached.clone());
        }
        let width = self.cmy_ctx.width();
        let height = self.cmy_ctx.height();
        let mask_bytes = u64::from(width) * u64::from(height) * 8;
        let Ok(_guard) = MemoryGuard::charge(self.budget, mask_bytes) else {
            self.record_limit(LimitExceeded::new(LimitCode::MemorySize));
            return None;
        };
        let rendered = draw_soft_mask(
            m,
            *self.cmy_ctx.render_settings(),
            width,
            height,
            self.cache,
            self.aliasing_threshold,
            self.hinting,
            self.budget,
        );
        let rendered = match rendered {
            Ok(mask) => mask,
            Err(err) => {
                self.record_limit(err);
                return None;
            }
        };
        if self.soft_mask_cache.len() < MAX_SOFT_MASK_CACHE {
            #[expect(
                clippy::disallowed_methods,
                reason = "bounded by MAX_SOFT_MASK_CACHE (64 entries)"
            )]
            self.soft_mask_cache.insert(key, rendered.clone());
        }
        Some(rendered)
    }

    fn apply_soft_mask(&mut self, mask: Option<&SoftMask<'doc>>) {
        if let Some(mask) = self.resolve_soft_mask(mask) {
            self.cmy_ctx.set_mask(mask.clone());
            self.k_ctx.set_mask(mask);
        } else {
            self.cmy_ctx.reset_mask();
            self.k_ctx.reset_mask();
        }
    }

    fn apply_draw_props(&mut self, props: &DrawProps<'doc>) {
        self.cmy_ctx.set_transform(props.transform);
        self.k_ctx.set_transform(props.transform);
        self.apply_soft_mask(props.soft_mask.as_ref());
        let blend = convert_blend_mode(props.blend_mode);
        self.cmy_ctx.set_blend_mode(blend);
        self.k_ctx.set_blend_mode(blend);
    }

    fn apply_image_props(&mut self, props: &ImageDrawProps<'doc>) {
        self.cmy_ctx.set_transform(props.transform);
        self.k_ctx.set_transform(props.transform);
        self.apply_soft_mask(props.soft_mask.as_ref());
        let blend = convert_blend_mode(props.blend_mode);
        self.cmy_ctx.set_blend_mode(blend);
        self.k_ctx.set_blend_mode(blend);
    }

    fn push_clip_path_inner(&mut self, clip_path: &BezPath, fill: FillRule) {
        let old_transform = *self.cmy_ctx.transform();
        let fill = convert_fill_rule(fill);

        self.cmy_ctx.set_fill_rule(fill);
        self.cmy_ctx.set_transform(Affine::IDENTITY);
        self.cmy_ctx.push_clip_path(clip_path);
        self.cmy_ctx.set_transform(old_transform);

        self.k_ctx.set_fill_rule(fill);
        self.k_ctx.set_transform(Affine::IDENTITY);
        self.k_ctx.push_clip_path(clip_path);
        self.k_ctx.set_transform(old_transform);
    }

    fn set_stroke_properties(&mut self, stroke_props: &StrokeProps, is_text: bool) {
        let stroke = make_stroke(
            self.cmy_ctx.transform(),
            stroke_props,
            is_text,
            self.inside_pattern,
            self.in_type3_glyph,
        );
        self.cmy_ctx.set_stroke(stroke.clone());
        self.k_ctx.set_stroke(stroke);
    }

    #[must_use]
    fn set_paint(
        &mut self,
        paint: &Paint<'doc>,
        path_bbox: impl Fn() -> Rect,
        is_stroke: bool,
    ) -> Option<BezPath> {
        let mut paint_transform = Affine::IDENTITY;
        let mut clip_path = None;

        let (cmy_paint, k_paint): (PaintType, PaintType) = match paint {
            Paint::Color(c) => {
                let (cmy, k) = color_to_lanes(c);
                (
                    AlphaColor::from_rgba8(cmy[0], cmy[1], cmy[2], cmy[3]).into(),
                    AlphaColor::from_rgba8(k[0], k[1], k[2], k[3]).into(),
                )
            }
            Paint::Pattern(p) => {
                let path_transform = *self.cmy_ctx.transform();
                let bbox = {
                    let mut bbox = path_transform.transform_rect_bbox(path_bbox());
                    if is_stroke {
                        let (a1, a2) = x_y_advances(&path_transform);
                        let factor = a1.length().max(a2.length()) * self.cmy_ctx.stroke().width;
                        bbox = bbox.inflate(factor, factor);
                    }
                    bbox.intersect(Rect::new(
                        0.0,
                        0.0,
                        f64::from(self.cmy_ctx.width()),
                        f64::from(self.cmy_ctx.height()),
                    ))
                };

                match p.as_ref() {
                    Pattern::Shading(s) => {
                        clip_path = s.shading.clip_path.clone();
                        match self.shading_paint(s, bbox) {
                            Ok(Some((cmy_img, k_img, transform))) => {
                                paint_transform = path_transform.inverse() * transform;
                                (PaintType::Image(cmy_img), PaintType::Image(k_img))
                            }
                            Ok(None) => (
                                AlphaColor::<Srgb>::TRANSPARENT.into(),
                                AlphaColor::<Srgb>::TRANSPARENT.into(),
                            ),
                            Err(err) => {
                                self.record_limit(err);
                                (
                                    AlphaColor::<Srgb>::TRANSPARENT.into(),
                                    AlphaColor::<Srgb>::TRANSPARENT.into(),
                                )
                            }
                        }
                    }
                    Pattern::Tiling(t) => match self.tiling_paint(t, bbox, is_stroke) {
                        Ok(Some((cmy_img, k_img, transform))) => {
                            paint_transform = path_transform.inverse() * transform;
                            (PaintType::Image(cmy_img), PaintType::Image(k_img))
                        }
                        Ok(None) => (
                            AlphaColor::<Srgb>::TRANSPARENT.into(),
                            AlphaColor::<Srgb>::TRANSPARENT.into(),
                        ),
                        Err(err) => {
                            self.record_limit(err);
                            (
                                AlphaColor::<Srgb>::TRANSPARENT.into(),
                                AlphaColor::<Srgb>::TRANSPARENT.into(),
                            )
                        }
                    },
                }
            }
        };

        self.cmy_ctx.set_paint_transform(paint_transform);
        self.cmy_ctx.set_paint(cmy_paint);
        self.k_ctx.set_paint_transform(paint_transform);
        self.k_ctx.set_paint(k_paint);

        clip_path
    }

    fn shading_paint(
        &self,
        s: &ShadingPattern,
        bbox: Rect,
    ) -> Result<Option<(VelloImage, VelloImage, Affine)>, LimitExceeded> {
        if bbox.is_zero_area() {
            return Ok(None);
        }
        let (width, height) = texture_dimensions(bbox, 1.0);
        if width == 0 || height == 0 || width > u32::from(u16::MAX) || height > u32::from(u16::MAX)
        {
            return Ok(None);
        }
        let pixel_count = usize::try_from(width)
            .ok()
            .and_then(|w| usize::try_from(height).ok().and_then(|h| w.checked_mul(h)))
            .ok_or_else(|| LimitExceeded::new(LimitCode::MemorySize))?;
        let byte_len = pixel_count
            .checked_mul(4)
            .ok_or_else(|| LimitExceeded::new(LimitCode::MemorySize))?;

        let mut cmy_buf = BudgetVec::new(self.budget);
        let mut k_buf = BudgetVec::new(self.budget);
        cmy_buf.try_reserve_exact(byte_len)?;
        k_buf.try_reserve_exact(byte_len)?;

        let mut may_have_transparency = false;
        let kind = color_space_kind(&s.shading.color_space);
        let native_kind = matches!(
            kind,
            ColorSpaceKind::DeviceCmyk | ColorSpaceKind::DeviceGray | ColorSpaceKind::DeviceRgb
        );

        let transform = if native_kind
            && let Some(transform) = sample_native_shading(
                s,
                kind,
                bbox,
                width,
                height,
                &mut cmy_buf,
                &mut k_buf,
                &mut may_have_transparency,
                self.budget,
            )? {
            transform
        } else {
            let encoded = s.encode();
            let mut failed: Option<LimitExceeded> = None;
            let mut index = 0usize;
            let (_, _, transform) = encoded.sample_texture(bbox, 1.0, |sample| {
                if failed.is_some() {
                    return;
                }
                if index.is_multiple_of(16_384)
                    && let Err(err) = self.budget.poll()
                {
                    failed = Some(err);
                    return;
                }
                index += 1;
                let a = unit_to_u8(sample[3]);
                may_have_transparency |= a != 255;
                let [c, m, y, k] = rgb_f32_to_cmyk(sample[0], sample[1], sample[2]).map(unit_to_u8);
                if let Err(err) = cmy_buf.try_extend_from_slice(&[255 - c, 255 - m, 255 - y, a]) {
                    failed = Some(err);
                    return;
                }
                if let Err(err) = k_buf.try_extend_from_slice(&[255 - k, 255 - k, 255 - k, a]) {
                    failed = Some(err);
                }
            });
            if let Some(err) = failed {
                return Err(err);
            }
            transform
        };

        let meta = PixelMetadata {
            may_have_transparency,
            alpha_type: ImageAlphaType::Alpha,
        };
        let cmy_pix = Pixmap::from_parts(cmy_buf.into_vec(), width as u16, height as u16, meta);
        let k_pix = Pixmap::from_parts(k_buf.into_vec(), width as u16, height as u16, meta);
        let sampler = ImageSampler {
            x_extend: peniko::Extend::Pad,
            y_extend: peniko::Extend::Pad,
            quality: ImageQuality::Medium,
            alpha: 1.0,
        };
        Ok(Some((
            VelloImage {
                image: ImageSource::Pixmap(Arc::new(cmy_pix)),
                sampler,
            },
            VelloImage {
                image: ImageSource::Pixmap(Arc::new(k_pix)),
                sampler,
            },
            transform,
        )))
    }

    fn tiling_paint(
        &self,
        t: &TilingPattern<'doc>,
        area: Rect,
        is_stroke: bool,
    ) -> Result<Option<(VelloImage, VelloImage, Affine)>, LimitExceeded> {
        if area.is_zero_area() {
            return Ok(None);
        }
        let area = t.matrix.inverse().transform_rect_bbox(area);
        let (x_advance, y_advance) = x_y_advances(&t.matrix);
        let x = TileAxis::new(
            x_advance.length(),
            t.bbox.x0..t.bbox.x1,
            t.x_step,
            area.x0..area.x1,
        );
        let y = TileAxis::new(
            y_advance.length(),
            t.bbox.y0..t.bbox.y1,
            t.y_step,
            area.y0..area.y1,
        );
        if x.size == 0 || y.size == 0 {
            return Ok(None);
        }

        let tile_pixels = u64::from(x.size) * u64::from(y.size);
        let _guard = MemoryGuard::charge(
            self.budget,
            tile_pixels.saturating_mul(TEMP_BYTES_PER_PIXEL),
        )?;

        let to_texture =
            Affine::scale_non_uniform(x.scale, y.scale) * Affine::translate((-x.start, -y.start));
        let mut child_cmy = self.child_context(x.size, y.size);
        let mut child_k = self.child_context(x.size, y.size);
        {
            let mut sub = CmykDevice {
                cache: self.cache,
                cmy_ctx: &mut child_cmy,
                k_ctx: &mut child_k,
                budget: self.budget,
                aliasing_threshold: self.aliasing_threshold,
                hinting: self.hinting,
                inside_pattern: true,
                in_type3_glyph: false,
                soft_mask_cache: HashMap::new(),
                ops: 0,
                error: None,
            };

            for j in y.copies.clone() {
                for i in x.copies.clone() {
                    if !sub.poll_budget() {
                        break;
                    }
                    let offset = Affine::translate((f64::from(i) * x.step, f64::from(j) * y.step));
                    t.interpret(&mut sub, to_texture * offset, is_stroke);
                }
                if sub.error.is_some() {
                    break;
                }
            }
            if let Some(err) = sub.error.take() {
                return Err(err);
            }
        }

        let mut cmy_pix = Pixmap::new(x.size, y.size);
        child_cmy.flush();
        child_cmy.render(&mut cmy_pix, &mut Resources::default());

        let mut k_pix = Pixmap::new(x.size, y.size);
        child_k.flush();
        child_k.render(&mut k_pix, &mut Resources::default());

        let sampler = ImageSampler {
            x_extend: x.extend,
            y_extend: y.extend,
            quality: if self.aliasing_threshold.is_some() {
                ImageQuality::Low
            } else {
                ImageQuality::Medium
            },
            alpha: t.opacity,
        };
        let transform = t.matrix
            * Affine::translate((x.start, y.start))
            * Affine::scale_non_uniform(1.0 / x.scale, 1.0 / y.scale);

        Ok(Some((
            VelloImage {
                image: ImageSource::Pixmap(Arc::new(cmy_pix)),
                sampler,
            },
            VelloImage {
                image: ImageSource::Pixmap(Arc::new(k_pix)),
                sampler,
            },
            transform,
        )))
    }

    fn stroke_path_inner(
        &mut self,
        path: &BezPath,
        props: DrawProps<'doc>,
        stroke_props: &StrokeProps,
        is_text: bool,
    ) {
        if !self.poll_budget() {
            return;
        }
        self.apply_draw_props(&props);
        self.set_stroke_properties(stroke_props, is_text);

        let stroke = self.cmy_ctx.stroke();
        let extent = stroke.width / 2.0 * stroke.miter_limit.max(SQRT_2);
        let bbox = path.control_box().inflate(extent, extent);
        if self.misses_target(props.transform.transform_rect_bbox(bbox)) {
            return;
        }

        let clip_path = self.set_paint(&props.paint, || path.bounding_box(), true);
        if let Some(clip_path) = clip_path.as_ref() {
            self.push_clip_path_inner(clip_path, FillRule::NonZero);
        }
        if let Some(outline) = single_segment_outline(path, self.cmy_ctx.stroke()) {
            self.cmy_ctx.set_fill_rule(Fill::NonZero);
            self.cmy_ctx.fill_path(&outline);
            self.k_ctx.set_fill_rule(Fill::NonZero);
            self.k_ctx.fill_path(&outline);
        } else {
            self.cmy_ctx.stroke_path(path);
            self.k_ctx.stroke_path(path);
        }
        if clip_path.is_some() {
            self.pop_clip();
        }
    }

    fn fill_path_inner(&mut self, path: &BezPath, props: DrawProps<'doc>, fill_rule: FillRule) {
        if !self.poll_budget() {
            return;
        }
        let fill = convert_fill_rule(fill_rule);
        self.cmy_ctx.set_fill_rule(fill);
        self.k_ctx.set_fill_rule(fill);
        self.apply_draw_props(&props);

        let clip_path = self.set_paint(&props.paint, || path.bounding_box(), false);
        if let Some(clip_path) = clip_path.as_ref() {
            self.push_clip_path_inner(clip_path, fill_rule);
        }

        self.cmy_ctx.fill_path(path);
        self.k_ctx.fill_path(path);

        if clip_path.is_some() {
            self.pop_clip();
        }
    }

    fn hint(&self, glyph_run: &GlyphRun<'_, '_>) -> bool {
        self.hinting && glyph_run.writing_mode() == WritingMode::Horizontal
    }

    fn fill_outline(
        &self,
        glyph: &OutlineGlyph,
        id: u128,
        transform: Affine,
        hint: bool,
    ) -> (Rc<BezPath>, Affine) {
        let [a, b, c, d, e, f] = transform.as_coeffs();
        let ppem = d.abs() * 1000.0;
        if hint
            && b == 0.0
            && (MIN_HINTING_PPEM..=MAX_HINTING_PPEM).contains(&ppem)
            && let Some(hinted) = self.cached_path(id, ppem as f32, || {
                glyph.hinted_outline(ppem as f32, &self.cache.hinting_cache)
            })
        {
            return (hinted, Affine::new([a, b, c, d, e, f.round()]));
        }
        (self.cached_outline(glyph, id), transform)
    }

    fn cached_outline(&self, glyph: &OutlineGlyph, id: u128) -> Rc<BezPath> {
        self.cached_path(id, 0.0, || Some(glyph.outline()))
            .unwrap_or_default()
    }

    fn cached_path(
        &self,
        id: u128,
        ppem: f32,
        build: impl FnOnce() -> Option<BezPath>,
    ) -> Option<Rc<BezPath>> {
        let key = (id, ppem.to_bits());
        if let Some(path) = self.cache.outline_cache.borrow().get(&key) {
            return path.clone();
        }
        let path = build().map(Rc::new);
        let mut cache = self.cache.outline_cache.borrow_mut();
        if cache.len() < MAX_OUTLINE_CACHE {
            #[expect(
                clippy::disallowed_methods,
                reason = "bounded by MAX_OUTLINE_CACHE (4096 entries)"
            )]
            cache.insert(key, path.clone());
        }
        path
    }

    fn fill_glyph(
        &mut self,
        glyph: &Glyph<'doc>,
        props: DrawProps<'doc>,
        transform: Affine,
        hint: bool,
    ) {
        match glyph {
            Glyph::Outline(outline) => {
                let id = outline.identifier().cache_key();
                let (path, transform) =
                    self.fill_outline(outline, id, props.transform * transform, hint);
                let props = DrawProps { transform, ..props };
                self.fill_path_inner(path.as_ref(), props, FillRule::NonZero);
            }
            Glyph::Type3(t3) => {
                self.in_type3_glyph = true;
                t3.interpret(self, props.transform, transform, &props.paint);
                self.in_type3_glyph = false;
            }
        }
    }

    fn stroke_glyph(
        &mut self,
        glyph: &Glyph<'doc>,
        props: DrawProps<'doc>,
        transform: Affine,
        stroke_props: &StrokeProps,
    ) {
        match glyph {
            Glyph::Outline(outline) => {
                let path = self.cached_outline(outline, outline.identifier().cache_key());
                self.stroke_path_inner(
                    &(transform * path.as_ref().clone()),
                    props,
                    stroke_props,
                    true,
                );
            }
            Glyph::Type3(t3) => {
                t3.interpret(self, props.transform, transform, &props.paint);
            }
        }
    }

    fn draw_dual_pixmaps(
        &mut self,
        cmy_pix: Arc<Pixmap>,
        k_pix: Arc<Pixmap>,
        quality: ImageQuality,
        transform: Affine,
    ) {
        let rect = Rect::new(
            0.0,
            0.0,
            f64::from(cmy_pix.width()),
            f64::from(cmy_pix.height()),
        );
        let sampler = ImageSampler {
            x_extend: peniko::Extend::Pad,
            y_extend: peniko::Extend::Pad,
            quality,
            alpha: 1.0,
        };
        self.cmy_ctx.set_transform(transform);
        self.cmy_ctx.set_paint(VelloImage {
            image: ImageSource::Pixmap(cmy_pix),
            sampler,
        });
        self.cmy_ctx.fill_rect(&rect);

        self.k_ctx.set_transform(transform);
        self.k_ctx.set_paint(VelloImage {
            image: ImageSource::Pixmap(k_pix),
            sampler,
        });
        self.k_ctx.fill_rect(&rect);
    }

    #[expect(
        clippy::too_many_arguments,
        reason = "internal dual-plane image helper"
    )]
    fn draw_cmyk_image_planes(
        &mut self,
        cmy_rgba: Vec<u8>,
        k_rgba: Vec<u8>,
        width: u32,
        height: u32,
        interpolate: bool,
        may_have_transparency: bool,
        transform: Affine,
    ) {
        let quality = if interpolate {
            ImageQuality::Medium
        } else {
            ImageQuality::Low
        };
        let (cmy_rgba, k_rgba, width, height, extra) = if self.in_type3_glyph {
            match (
                pad_type3_rgba(&cmy_rgba, width, height, self.budget),
                pad_type3_rgba(&k_rgba, width, height, self.budget),
            ) {
                (Ok(cmy), Ok(k)) => (
                    cmy,
                    k,
                    width + 4,
                    height + 4,
                    Affine::translate((-2.0, -2.0)),
                ),
                (Err(err), _) | (_, Err(err)) => {
                    self.record_limit(err);
                    return;
                }
            }
        } else {
            (cmy_rgba, k_rgba, width, height, Affine::IDENTITY)
        };
        let (Ok(w), Ok(h)) = (u16::try_from(width), u16::try_from(height)) else {
            return;
        };
        let meta = PixelMetadata {
            may_have_transparency: may_have_transparency || self.in_type3_glyph,
            alpha_type: ImageAlphaType::Alpha,
        };
        let cmy_pix = Arc::new(Pixmap::from_parts(cmy_rgba, w, h, meta));
        let k_pix = Arc::new(Pixmap::from_parts(k_rgba, w, h, meta));
        self.draw_dual_pixmaps(cmy_pix, k_pix, quality, transform * extra);
    }

    #[expect(
        clippy::too_many_arguments,
        reason = "internal dual-plane image helper"
    )]
    fn draw_cmyk_image_with_alpha(
        &mut self,
        cmy_bytes: &[u8],
        k_bytes: &[u8],
        width: u32,
        height: u32,
        interpolate: bool,
        alpha: Option<LumaData>,
        transform: Affine,
    ) {
        // When the alpha mask has different dimensions or interpolation,
        // composite it with DestIn in a temporary layer, matching `hayro`.
        if let Some(ref a) = alpha
            && (a.width != width || a.height != height || a.interpolate != interpolate)
        {
            let mask_transform = transform
                * Affine::scale_non_uniform(
                    f64::from(width) / f64::from(a.width),
                    f64::from(height) / f64::from(a.height),
                );
            self.cmy_ctx.push_layer(None, None, None, None, None);
            self.k_ctx.push_layer(None, None, None, None, None);
            self.draw_cmyk_image_with_alpha(
                cmy_bytes,
                k_bytes,
                width,
                height,
                interpolate,
                None,
                transform,
            );
            let dest_in = peniko::BlendMode::new(Mix::Normal, Compose::DestIn);
            self.cmy_ctx
                .push_layer(None, Some(dest_in), None, None, None);
            self.k_ctx.push_layer(None, Some(dest_in), None, None, None);
            if let Ok((mask_rgba, mw, mh, mscale)) =
                solid_rgba_with_alpha([0, 0, 0], &a.data, a.width, a.height, self.budget)
            {
                let (Ok(mw16), Ok(mh16)) = (u16::try_from(mw), u16::try_from(mh)) else {
                    self.cmy_ctx.pop_layer();
                    self.k_ctx.pop_layer();
                    self.cmy_ctx.pop_layer();
                    self.k_ctx.pop_layer();
                    return;
                };
                let pix = Arc::new(Pixmap::from_parts(
                    mask_rgba,
                    mw16,
                    mh16,
                    PixelMetadata {
                        may_have_transparency: true,
                        alpha_type: ImageAlphaType::Alpha,
                    },
                ));
                let quality = if a.interpolate {
                    ImageQuality::Medium
                } else {
                    ImageQuality::Low
                };
                self.draw_dual_pixmaps(pix.clone(), pix, quality, mask_transform * mscale);
            } else {
                self.record_limit(LimitExceeded::new(LimitCode::MemorySize));
            }
            self.cmy_ctx.pop_layer();
            self.k_ctx.pop_layer();
            self.cmy_ctx.pop_layer();
            self.k_ctx.pop_layer();
            self.cmy_ctx.set_transform(transform);
            self.k_ctx.set_transform(transform);
            return;
        }

        let alpha_slice = alpha.as_ref().map(|a| a.data.as_slice());
        let Ok((cmy_rgba, k_rgba, out_w, out_h, downscale)) =
            pack_dual_planes(cmy_bytes, k_bytes, alpha_slice, width, height, self.budget)
        else {
            self.record_limit(LimitExceeded::new(LimitCode::MemorySize));
            return;
        };
        self.draw_cmyk_image_planes(
            cmy_rgba,
            k_rgba,
            out_w,
            out_h,
            interpolate,
            alpha.is_some(),
            transform * downscale,
        );
    }

    fn draw_raster_image(&mut self, raster: RasterImage<'_>, mut transform: Affine) {
        let (x, y) = x_y_advances(&transform);
        let target_width = (x.length() * f64::from(raster.width())).ceil() as u32;
        let target_height = (y.length() * f64::from(raster.height())).ceil() as u32;
        let target_dim = Some((target_width.max(1), target_height.max(1)));

        // Fast native path for DeviceCMYK / CalCMYK images: bypass hayro's
        // sRGB ICC conversion completely so C/M/Y/K samples stay exact.
        if let Some(native) = decode_native_cmyk_image(&raster, target_dim, self.budget) {
            match native {
                Ok(decoded) => {
                    transform *= Affine::scale_non_uniform(
                        f64::from(decoded.scale_factors.0),
                        f64::from(decoded.scale_factors.1),
                    );
                    self.draw_cmyk_image_with_alpha(
                        &decoded.cmy,
                        &decoded.k,
                        decoded.width,
                        decoded.height,
                        decoded.interpolate,
                        decoded.alpha,
                        transform,
                    );
                    return;
                }
                Err(err) => {
                    self.record_limit(err);
                    return;
                }
            }
        }

        // Fallback path for DeviceGray (Luma -> pure K), DeviceRGB, ICC,
        // Lab, Indexed, Separation, and DeviceN images.
        raster.with_rgba(
            |image, alpha| {
                let (sx, sy) = image.scale_factors();
                transform *= Affine::scale_non_uniform(f64::from(sx), f64::from(sy));
                let interpolate = image.interpolate();
                match image {
                    ImageData::Luma(luma) => {
                        let Ok((cmy_buf, k_buf)) = luma_to_dual_planes(&luma.data, self.budget)
                        else {
                            self.record_limit(LimitExceeded::new(LimitCode::MemorySize));
                            return;
                        };
                        self.draw_cmyk_image_with_alpha(
                            &cmy_buf,
                            &k_buf,
                            luma.width,
                            luma.height,
                            interpolate,
                            alpha,
                            transform,
                        );
                    }
                    ImageData::Rgb(rgb) => {
                        let Ok((cmy_buf, k_buf)) = rgb_to_dual_planes(&rgb.data, self.budget)
                        else {
                            self.record_limit(LimitExceeded::new(LimitCode::MemorySize));
                            return;
                        };
                        self.draw_cmyk_image_with_alpha(
                            &cmy_buf,
                            &k_buf,
                            rgb.width,
                            rgb.height,
                            interpolate,
                            alpha,
                            transform,
                        );
                    }
                }
            },
            target_dim,
        );
    }
}

impl<'doc> Device<'doc> for CmykDevice<'doc, '_, '_> {
    fn draw_path(&mut self, path: &BezPath, props: DrawProps<'doc>, draw_mode: &DrawMode) {
        match draw_mode {
            DrawMode::Fill(f) => self.fill_path_inner(path, props, *f),
            DrawMode::Stroke(s) => self.stroke_path_inner(path, props, s, false),
            DrawMode::FillAndStroke(f, s) => {
                self.fill_path_inner(path, props.clone(), *f);
                self.stroke_path_inner(path, props, s, false);
            }
            DrawMode::Invisible => {}
        }
    }

    fn draw_rect(&mut self, rect: &Rect, props: DrawProps<'doc>, draw_mode: &DrawMode) {
        if !self.poll_budget() {
            return;
        }
        match draw_mode {
            DrawMode::Fill(fill_rule) => {
                let fill = convert_fill_rule(*fill_rule);
                self.cmy_ctx.set_fill_rule(fill);
                self.k_ctx.set_fill_rule(fill);
                self.apply_draw_props(&props);

                let clip_path = self.set_paint(&props.paint, || *rect, false);
                if let Some(clip_path) = clip_path.as_ref() {
                    self.push_clip_path_inner(clip_path, *fill_rule);
                }

                self.cmy_ctx.fill_rect(rect);
                self.k_ctx.fill_rect(rect);

                if clip_path.is_some() {
                    self.pop_clip();
                }
            }
            DrawMode::Stroke(s) => {
                let path = rect.to_path(0.1);
                self.stroke_path_inner(&path, props, s, false);
            }
            DrawMode::FillAndStroke(fill_rule, stroke_props) => {
                self.draw_rect(rect, props.clone(), &DrawMode::Fill(*fill_rule));
                let path = rect.to_path(0.1);
                self.stroke_path_inner(&path, props, stroke_props, false);
            }
            DrawMode::Invisible => {}
        }
    }

    fn push_clip_path(&mut self, clip_path: &ClipPath) {
        self.push_clip_path_inner(&clip_path.path, clip_path.fill);
    }

    fn push_clip_rect(&mut self, rect: &Rect) {
        self.cmy_ctx.set_fill_rule(Fill::NonZero);
        self.cmy_ctx.set_transform(Affine::IDENTITY);
        self.cmy_ctx.push_clip_rect(rect);

        self.k_ctx.set_fill_rule(Fill::NonZero);
        self.k_ctx.set_transform(Affine::IDENTITY);
        self.k_ctx.push_clip_rect(rect);
    }

    fn pop_clip(&mut self) {
        self.cmy_ctx.pop_clip();
        self.k_ctx.pop_clip();
    }

    fn push_transparency_group(
        &mut self,
        opacity: f32,
        mask: Option<SoftMask<'doc>>,
        blend_mode: BlendMode,
    ) {
        let mask = self.resolve_soft_mask(mask.as_ref());
        let blend = convert_blend_mode(blend_mode);
        self.cmy_ctx
            .push_layer(None, Some(blend), Some(opacity), mask.clone(), None);
        self.k_ctx
            .push_layer(None, Some(blend), Some(opacity), mask, None);
    }

    fn pop_transparency_group(&mut self) {
        self.cmy_ctx.pop_layer();
        self.k_ctx.pop_layer();
    }

    fn draw_glyph_run(
        &mut self,
        glyph_run: &GlyphRun<'_, 'doc>,
        props: DrawProps<'doc>,
        draw_mode: &DrawMode,
    ) {
        if !self.poll_budget() {
            return;
        }
        let hint = self.hint(glyph_run);
        for glyph in glyph_run.glyphs() {
            if self.error.is_some() {
                return;
            }
            let transform = glyph.transform();
            match draw_mode {
                DrawMode::Fill(_) => self.fill_glyph(glyph, props.clone(), transform, hint),
                DrawMode::Stroke(stroke) => {
                    self.stroke_glyph(glyph, props.clone(), transform, stroke);
                }
                DrawMode::FillAndStroke(_, stroke) => {
                    self.fill_glyph(glyph, props.clone(), transform, hint);
                    self.stroke_glyph(glyph, props.clone(), transform, stroke);
                }
                DrawMode::Invisible => {}
            }
        }
    }

    fn draw_image(
        &mut self,
        image: hayro::hayro_interpret::Image<'doc, '_>,
        props: ImageDrawProps<'doc>,
    ) {
        if !self.poll_budget() {
            return;
        }
        let local = Rect::new(
            0.0,
            0.0,
            f64::from(image.width()),
            f64::from(image.height()),
        );
        if self.misses_target(props.transform.transform_rect_bbox(local)) {
            return;
        }

        self.apply_image_props(&props);
        let mut transform = props.transform;
        self.cmy_ctx.set_paint_transform(Affine::IDENTITY);
        self.k_ctx.set_paint_transform(Affine::IDENTITY);
        self.cmy_ctx.set_aliasing_threshold(Some(1));
        self.k_ctx.set_aliasing_threshold(Some(1));

        match image {
            hayro::hayro_interpret::Image::Stencil(s) => {
                let (x, y) = x_y_advances(&transform);
                let target_width = (x.length() * f64::from(s.width())).ceil() as u32;
                let target_height = (y.length() * f64::from(s.height())).ceil() as u32;
                s.with_stencil(
                    |stencil, paint| {
                        transform *= Affine::scale_non_uniform(
                            f64::from(stencil.scale_factors.0),
                            f64::from(stencil.scale_factors.1),
                        );
                        match paint {
                            Paint::Color(c) => {
                                let (cmy, k) = color_to_lanes(c);
                                let alpha = cmy[3];
                                let blend = self.cmy_ctx.blend_mode();
                                let push_layer =
                                    alpha != 255 || blend != peniko::BlendMode::default();
                                if push_layer {
                                    let op = f32::from(alpha) / 255.0;
                                    self.cmy_ctx.push_layer(
                                        None,
                                        Some(blend),
                                        Some(op),
                                        None,
                                        None,
                                    );
                                    self.k_ctx
                                        .push_layer(None, Some(blend), Some(op), None, None);
                                }
                                let old_rule = *self.cmy_ctx.fill_rule();
                                self.cmy_ctx.set_fill_rule(Fill::NonZero);
                                self.k_ctx.set_fill_rule(Fill::NonZero);

                                if let (Ok((cmy_rgba, w, h, s1)), Ok((k_rgba, _, _, _))) = (
                                    solid_rgba_with_alpha(
                                        [cmy[0], cmy[1], cmy[2]],
                                        &stencil.data,
                                        stencil.width,
                                        stencil.height,
                                        self.budget,
                                    ),
                                    solid_rgba_with_alpha(
                                        [k[0], k[1], k[2]],
                                        &stencil.data,
                                        stencil.width,
                                        stencil.height,
                                        self.budget,
                                    ),
                                ) {
                                    self.draw_cmyk_image_planes(
                                        cmy_rgba,
                                        k_rgba,
                                        w,
                                        h,
                                        stencil.interpolate,
                                        true,
                                        transform * s1,
                                    );
                                } else {
                                    self.record_limit(LimitExceeded::new(LimitCode::MemorySize));
                                }

                                if push_layer {
                                    self.cmy_ctx.pop_layer();
                                    self.k_ctx.pop_layer();
                                }
                                self.cmy_ctx.set_fill_rule(old_rule);
                                self.k_ctx.set_fill_rule(old_rule);
                            }
                            Paint::Pattern(_) => {
                                let (width, height) = (self.cmy_ctx.width(), self.cmy_ctx.height());
                                let stencil_rect = Rect::new(
                                    0.0,
                                    0.0,
                                    f64::from(stencil.width),
                                    f64::from(stencil.height),
                                );
                                let Ok((white_rgba, sw, sh, s_scale)) = solid_rgba_with_alpha(
                                    [255, 255, 255],
                                    &stencil.data,
                                    stencil.width,
                                    stencil.height,
                                    self.budget,
                                ) else {
                                    self.record_limit(LimitExceeded::new(LimitCode::MemorySize));
                                    return;
                                };
                                let (Ok(sw16), Ok(sh16)) = (u16::try_from(sw), u16::try_from(sh))
                                else {
                                    return;
                                };
                                let mask_pix = {
                                    let mut sub_ctx = self.child_context(width, height);
                                    let src = Arc::new(Pixmap::from_parts(
                                        white_rgba,
                                        sw16,
                                        sh16,
                                        PixelMetadata {
                                            may_have_transparency: true,
                                            alpha_type: ImageAlphaType::Alpha,
                                        },
                                    ));
                                    sub_ctx.set_transform(transform * s_scale);
                                    sub_ctx.set_paint(VelloImage {
                                        image: ImageSource::Pixmap(src),
                                        sampler: ImageSampler {
                                            x_extend: peniko::Extend::Pad,
                                            y_extend: peniko::Extend::Pad,
                                            quality: if stencil.interpolate {
                                                ImageQuality::Medium
                                            } else {
                                                ImageQuality::Low
                                            },
                                            alpha: 1.0,
                                        },
                                    });
                                    sub_ctx.fill_rect(&Rect::new(
                                        0.0,
                                        0.0,
                                        f64::from(sw16),
                                        f64::from(sh16),
                                    ));
                                    let mut out_pix = Pixmap::new(width, height);
                                    sub_ctx.flush();
                                    sub_ctx.render(&mut out_pix, &mut Resources::default());
                                    out_pix
                                };
                                let mask = Mask::new_luminance(&mask_pix);
                                let blend = self.cmy_ctx.blend_mode();
                                self.cmy_ctx.push_layer(
                                    None,
                                    Some(blend),
                                    None,
                                    Some(mask.clone()),
                                    None,
                                );
                                self.k_ctx
                                    .push_layer(None, Some(blend), None, Some(mask), None);
                                self.cmy_ctx.set_transform(transform);
                                self.k_ctx.set_transform(transform);

                                let clip_path = self.set_paint(paint, || stencil_rect, false);
                                if let Some(clip_path) = clip_path.as_ref() {
                                    self.push_clip_path_inner(clip_path, FillRule::NonZero);
                                }
                                self.cmy_ctx.fill_rect(&stencil_rect);
                                self.k_ctx.fill_rect(&stencil_rect);
                                if clip_path.is_some() {
                                    self.pop_clip();
                                }

                                self.cmy_ctx.pop_layer();
                                self.k_ctx.pop_layer();
                            }
                        }
                    },
                    Some((target_width.max(1), target_height.max(1))),
                );
            }
            hayro::hayro_interpret::Image::Raster(r) => {
                self.draw_raster_image(r, transform);
            }
        }

        self.cmy_ctx.set_aliasing_threshold(self.aliasing_threshold);
        self.k_ctx.set_aliasing_threshold(self.aliasing_threshold);
    }
}

/// Decoded native CMYK image split into complemented `CMY` (3 BPP) and `K` (1 BPP) planes.
struct NativeCmykImage {
    cmy: Vec<u8>,
    k: Vec<u8>,
    width: u32,
    height: u32,
    interpolate: bool,
    scale_factors: (f32, f32),
    alpha: Option<LumaData>,
}

/// Decodes a `DeviceCMYK` / `CalCMYK` raster image directly from its PDF stream
/// without converting through sRGB. Returns `None` if the image is not native CMYK
/// or if stream decoding falls back to `hayro-interpret`.
fn decode_native_cmyk_image(
    raster: &RasterImage<'_>,
    target_dimension: Option<(u32, u32)>,
    budget: &Budget,
) -> Option<Result<NativeCmykImage, LimitExceeded>> {
    let stream = raster.stream();
    let dict = stream.dict();
    let cs_obj = dict
        .get::<Object<'_>>(CS)
        .or_else(|| dict.get::<Object<'_>>(COLORSPACE));
    let explicit_cmyk = cs_obj.as_ref().is_some_and(is_device_cmyk_object);
    let implicit_cs = cs_obj.is_none();
    if !explicit_cmyk && !implicit_cs {
        return None;
    }

    let dict_bpc = dict
        .get::<u8>(BPC)
        .or_else(|| dict.get::<u8>(BITS_PER_COMPONENT));
    let decode_params = ImageDecodeParams {
        is_indexed: false,
        bpc: dict_bpc,
        num_components: explicit_cmyk.then_some(4),
        target_dimension,
        width: raster.width(),
        height: raster.height(),
    };
    let mut decoded = stream.decoded_image(&decode_params).ok()?;
    if implicit_cs {
        let is_cmyk = decoded
            .image_data
            .as_ref()
            .and_then(|d| d.color_space)
            .is_some_and(|cs| matches!(cs, ImageColorSpace::Cmyk));
        if !is_cmyk {
            return None;
        }
    }

    let (mut scale_x, mut scale_y) = (1.0_f32, 1.0_f32);
    let (width, mut height) = decoded
        .image_data
        .as_ref()
        .map(|d| {
            scale_x = raster.width() as f32 / d.width as f32;
            scale_y = raster.height() as f32 / d.height as f32;
            (d.width, d.height)
        })
        .unwrap_or((raster.width(), raster.height()));
    if width == 0 || height == 0 {
        return None;
    }

    let bpc = decoded
        .image_data
        .as_ref()
        .map(|d| d.bits_per_component)
        .or(dict_bpc)
        .unwrap_or(8);

    let decode_arr: [(f32, f32); 4] = dict
        .get::<Array<'_>>(D)
        .or_else(|| dict.get::<Array<'_>>(DECODE))
        .and_then(|a| {
            let mut pairs = [(0.0_f32, 1.0_f32); 4];
            let mut count = 0;
            for pair in a.iter::<(f32, f32)>() {
                if count < 4 {
                    pairs[count] = pair;
                }
                count += 1;
            }
            (count >= 4).then_some(pairs)
        })
        .unwrap_or([(0.0, 1.0); 4]);

    let pixels = match usize::try_from(width)
        .ok()
        .and_then(|w| usize::try_from(height).ok().and_then(|h| w.checked_mul(h)))
    {
        Some(p) => p,
        None => return Some(Err(LimitExceeded::new(LimitCode::MemorySize))),
    };
    let expected_cmyk = match pixels.checked_mul(4) {
        Some(n) => n,
        None => return Some(Err(LimitExceeded::new(LimitCode::MemorySize))),
    };

    let mut cmyk = match unpack_cmyk_samples(
        &decoded.data,
        width,
        height,
        bpc,
        decode_arr,
        expected_cmyk,
        budget,
    ) {
        Ok(Some(buf)) => buf,
        Ok(None) => return None,
        Err(err) => return Some(Err(err)),
    };
    let row_len = (width as usize).saturating_mul(4);
    if row_len == 0 {
        return None;
    }
    if cmyk.len() > expected_cmyk {
        cmyk.truncate(expected_cmyk);
    } else if cmyk.len() < expected_cmyk {
        height = cmyk.len().div_ceil(row_len) as u32;
        if height == 0 {
            return None;
        }
        let rem = cmyk.len() % row_len;
        if rem != 0 {
            let pad = row_len - rem;
            if let Err(err) = cmyk.try_extend(std::iter::repeat_n(0, pad)) {
                return Some(Err(err));
            }
        }
    }

    let interpolate = dict
        .get::<bool>(I)
        .or_else(|| dict.get::<bool>(INTERPOLATE))
        .unwrap_or(false);

    // Extract optional alpha mask (SMaskInData, /SMask, /Mask, color-key).
    let has_mask =
        dict.contains_key(SMASK_IN_DATA) || dict.contains_key(SMASK) || dict.contains_key(MASK);
    let alpha = if let Some(1) = dict.get::<u8>(SMASK_IN_DATA) {
        decoded
            .image_data
            .as_mut()
            .and_then(|d| d.alpha.take())
            .map(|mut data| {
                let target_len = width as usize * height as usize;
                if data.len() >= target_len {
                    data.truncate(target_len);
                }
                LumaData {
                    data,
                    width,
                    height,
                    interpolate,
                    scale_factors: (scale_x, scale_y),
                }
            })
    } else if has_mask {
        let mut mask_out = None;
        raster.with_rgba(|_, alpha| mask_out = alpha, target_dimension);
        mask_out
    } else {
        None
    };

    // Unpremultiply CMYK /Matte directly in CMYK space when present on /SMask.
    if let Some(ref alpha_data) = alpha
        && alpha_data.width == width
        && alpha_data.height == height
        && let Some(smask) = dict.get::<Stream<'_>>(SMASK)
        && let Some(matte) = smask.dict().get::<Array<'_>>(MATTE)
    {
        let mut m = [0.0_f32; 4];
        let mut count = 0;
        for val in matte.iter::<f32>() {
            if count < 4 {
                m[count] = val;
            }
            count += 1;
        }
        if count == 4 {
            let m_u8 = m.map(unit_to_u8);
            for (pixel, &a) in cmyk
                .as_mut_slice()
                .as_chunks_mut::<4>()
                .0
                .iter_mut()
                .zip(alpha_data.data.iter())
            {
                if a == 0 {
                    continue;
                }
                let inv_a = 255.0 / f32::from(a);
                for (comp, &matte_byte) in pixel.iter_mut().zip(m_u8.iter()) {
                    let mv = f32::from(matte_byte);
                    *comp = (mv + (f32::from(*comp) - mv) * inv_a).clamp(0.0, 255.0) as u8;
                }
            }
        }
    }

    // Split CMYK into complemented CMY (3 BPP) and K (1 BPP) planes.
    let actual_pixels = cmyk.len() / 4;
    let mut cmy_plane = BudgetVec::new(budget);
    let mut k_plane = BudgetVec::new(budget);
    if let Err(err) = cmy_plane
        .try_reserve_exact(actual_pixels.saturating_mul(3))
        .and_then(|()| k_plane.try_reserve_exact(actual_pixels))
    {
        return Some(Err(err));
    }
    for (idx, px) in cmyk.as_slice().as_chunks::<4>().0.iter().enumerate() {
        if idx.is_multiple_of(65_536)
            && let Err(err) = budget.poll()
        {
            return Some(Err(err));
        }
        if let Err(err) = cmy_plane
            .try_extend_from_slice(&[255 - px[0], 255 - px[1], 255 - px[2]])
            .and_then(|()| k_plane.try_push(255 - px[3]))
        {
            return Some(Err(err));
        }
    }

    Some(Ok(NativeCmykImage {
        cmy: cmy_plane.into_vec(),
        k: k_plane.into_vec(),
        width,
        height,
        interpolate,
        scale_factors: (scale_x, scale_y),
        alpha,
    }))
}

fn is_device_cmyk_object(object: &Object<'_>) -> bool {
    match object {
        Object::Name(name) => matches!(name.as_ref(), DEVICE_CMYK | CMYK | CALCMYK),
        Object::Array(array) => array
            .flex_iter()
            .next::<Name<'_>>()
            .is_some_and(|n| matches!(n.as_ref(), DEVICE_CMYK | CMYK | CALCMYK)),
        _ => false,
    }
}

fn unpack_cmyk_samples<'b>(
    data: &[u8],
    width: u32,
    height: u32,
    bpc: u8,
    decode: [(f32, f32); 4],
    expected_bytes: usize,
    budget: &'b Budget,
) -> Result<Option<BudgetVec<'b, u8>>, LimitExceeded> {
    let mut out = BudgetVec::new(budget);
    out.try_reserve_exact(expected_bytes)?;

    if bpc == 8 && decode == [(0.0, 1.0); 4] {
        let len = data.len().min(expected_bytes);
        out.try_extend_from_slice(&data[..len])?;
        return Ok(Some(out));
    }
    if bpc == 8 && decode == [(1.0, 0.0); 4] {
        let len = data.len().min(expected_bytes);
        for (i, &b) in data[..len].iter().enumerate() {
            if i.is_multiple_of(65_536) {
                budget.poll()?;
            }
            out.try_push(255 - b)?;
        }
        return Ok(Some(out));
    }

    if !matches!(bpc, 1 | 2 | 4 | 8 | 12 | 16) {
        return Ok(None);
    }
    let max_val = ((1_u32 << bpc) - 1) as f32;
    let map_sample = |raw: u32, comp: usize| {
        let (dmin, dmax) = decode[comp];
        let v = dmin + (raw as f32 / max_val) * (dmax - dmin);
        unit_to_u8(v)
    };

    if bpc == 8 {
        let len = data.len().min(expected_bytes);
        for (i, &b) in data[..len].iter().enumerate() {
            if i.is_multiple_of(65_536) {
                budget.poll()?;
            }
            out.try_push(map_sample(u32::from(b), i % 4))?;
        }
        return Ok(Some(out));
    }

    if bpc == 16 {
        let max_samples = expected_bytes.min(data.len() / 2);
        for (i, pair) in data.as_chunks::<2>().0.iter().take(max_samples).enumerate() {
            if i.is_multiple_of(65_536) {
                budget.poll()?;
            }
            let raw = u32::from(u16::from_be_bytes([pair[0], pair[1]]));
            out.try_push(map_sample(raw, i % 4))?;
        }
        return Ok(Some(out));
    }

    let mut reader = BitReader::new(data);
    for row in 0..height {
        if row.is_multiple_of(256) {
            budget.poll()?;
        }
        for _ in 0..width {
            for comp in 0..4 {
                let raw = reader.read(bpc).unwrap_or(0);
                out.try_push(map_sample(raw, comp))?;
            }
        }
        reader.align();
    }
    Ok(Some(out))
}

fn luma_to_dual_planes(data: &[u8], budget: &Budget) -> Result<(Vec<u8>, Vec<u8>), LimitExceeded> {
    let mut cmy = BudgetVec::new(budget);
    let mut k = BudgetVec::new(budget);
    cmy.try_reserve_exact(data.len().saturating_mul(3))?;
    k.try_reserve_exact(data.len())?;
    for (idx, &g) in data.iter().enumerate() {
        if idx.is_multiple_of(65_536) {
            budget.poll()?;
        }
        // DeviceGray -> pure K: C=0, M=0, Y=0 (complement 255), K=1-g (complement g).
        cmy.try_extend_from_slice(&[255, 255, 255])?;
        k.try_push(g)?;
    }
    Ok((cmy.into_vec(), k.into_vec()))
}

fn rgb_to_dual_planes(data: &[u8], budget: &Budget) -> Result<(Vec<u8>, Vec<u8>), LimitExceeded> {
    let pixels = data.len() / 3;
    let mut cmy = BudgetVec::new(budget);
    let mut k = BudgetVec::new(budget);
    cmy.try_reserve_exact(pixels.saturating_mul(3))?;
    k.try_reserve_exact(pixels)?;
    for (idx, rgb) in data.as_chunks::<3>().0.iter().enumerate() {
        if idx.is_multiple_of(65_536) {
            budget.poll()?;
        }
        let [c, m, y, black] = rgb_u8_to_cmyk([rgb[0], rgb[1], rgb[2]]);
        cmy.try_extend_from_slice(&[255 - c, 255 - m, 255 - y])?;
        k.try_push(255 - black)?;
    }
    Ok((cmy.into_vec(), k.into_vec()))
}

/// Packed `(cmy_rgba, k_rgba, width, height, downsample_transform)` buffers.
type PackedDualPlanes = (Vec<u8>, Vec<u8>, u32, u32, Affine);

/// Packs complemented `cmy` (3 BPP) and `k` (1 BPP) planes with optional alpha
/// into two RGBA8 buffers, downsampling if either side exceeds `u16::MAX`.
fn pack_dual_planes(
    cmy: &[u8],
    k: &[u8],
    alpha: Option<&[u8]>,
    width: u32,
    height: u32,
    budget: &Budget,
) -> Result<PackedDualPlanes, LimitExceeded> {
    let step_x = width.div_ceil(u32::from(u16::MAX)).max(1);
    let step_y = height.div_ceil(u32::from(u16::MAX)).max(1);
    let out_w = width.div_ceil(step_x);
    let out_h = height.div_ceil(step_y);
    let out_pixels = usize::try_from(out_w)
        .ok()
        .and_then(|w| usize::try_from(out_h).ok().and_then(|h| w.checked_mul(h)))
        .ok_or_else(|| LimitExceeded::new(LimitCode::MemorySize))?;
    let out_bytes = out_pixels
        .checked_mul(4)
        .ok_or_else(|| LimitExceeded::new(LimitCode::MemorySize))?;

    let mut cmy_rgba = BudgetVec::new(budget);
    let mut k_rgba = BudgetVec::new(budget);
    cmy_rgba.try_reserve_exact(out_bytes)?;
    k_rgba.try_reserve_exact(out_bytes)?;

    if step_x == 1 && step_y == 1 {
        for idx in 0..out_pixels {
            if idx.is_multiple_of(65_536) {
                budget.poll()?;
            }
            let a = alpha.and_then(|a| a.get(idx).copied()).unwrap_or(255);
            let c_off = idx * 3;
            let kv = k.get(idx).copied().unwrap_or(255);
            if c_off + 3 <= cmy.len() {
                cmy_rgba.try_extend_from_slice(&[cmy[c_off], cmy[c_off + 1], cmy[c_off + 2], a])?;
            } else {
                cmy_rgba.try_extend_from_slice(&[255, 255, 255, a])?;
            }
            k_rgba.try_extend_from_slice(&[kv, kv, kv, a])?;
        }
        return Ok((
            cmy_rgba.into_vec(),
            k_rgba.into_vec(),
            out_w,
            out_h,
            Affine::IDENTITY,
        ));
    }

    for oy in 0..out_h {
        if oy.is_multiple_of(256) {
            budget.poll()?;
        }
        let sy = (oy * step_y).min(height - 1) as usize;
        for ox in 0..out_w {
            let sx = (ox * step_x).min(width - 1) as usize;
            let idx = sy * width as usize + sx;
            let a = alpha.and_then(|a| a.get(idx).copied()).unwrap_or(255);
            let c_off = idx * 3;
            let kv = k.get(idx).copied().unwrap_or(255);
            if c_off + 3 <= cmy.len() {
                cmy_rgba.try_extend_from_slice(&[cmy[c_off], cmy[c_off + 1], cmy[c_off + 2], a])?;
            } else {
                cmy_rgba.try_extend_from_slice(&[255, 255, 255, a])?;
            }
            k_rgba.try_extend_from_slice(&[kv, kv, kv, a])?;
        }
    }
    let scale = Affine::scale_non_uniform(
        f64::from(width) / f64::from(out_w),
        f64::from(height) / f64::from(out_h),
    );
    Ok((cmy_rgba.into_vec(), k_rgba.into_vec(), out_w, out_h, scale))
}

fn solid_rgba_with_alpha(
    rgb: [u8; 3],
    alpha: &[u8],
    width: u32,
    height: u32,
    budget: &Budget,
) -> Result<(Vec<u8>, u32, u32, Affine), LimitExceeded> {
    let step_x = width.div_ceil(u32::from(u16::MAX)).max(1);
    let step_y = height.div_ceil(u32::from(u16::MAX)).max(1);
    let out_w = width.div_ceil(step_x);
    let out_h = height.div_ceil(step_y);
    let out_pixels = usize::try_from(out_w)
        .ok()
        .and_then(|w| usize::try_from(out_h).ok().and_then(|h| w.checked_mul(h)))
        .ok_or_else(|| LimitExceeded::new(LimitCode::MemorySize))?;
    let mut out = BudgetVec::new(budget);
    out.try_reserve_exact(
        out_pixels
            .checked_mul(4)
            .ok_or_else(|| LimitExceeded::new(LimitCode::MemorySize))?,
    )?;
    if step_x == 1 && step_y == 1 {
        for (i, &a) in alpha.iter().take(out_pixels).enumerate() {
            if i.is_multiple_of(65_536) {
                budget.poll()?;
            }
            out.try_extend_from_slice(&[rgb[0], rgb[1], rgb[2], a])?;
        }
        return Ok((out.into_vec(), out_w, out_h, Affine::IDENTITY));
    }
    for oy in 0..out_h {
        let sy = (oy * step_y).min(height - 1) as usize;
        for ox in 0..out_w {
            let sx = (ox * step_x).min(width - 1) as usize;
            let a = alpha.get(sy * width as usize + sx).copied().unwrap_or(0);
            out.try_extend_from_slice(&[rgb[0], rgb[1], rgb[2], a])?;
        }
    }
    let scale = Affine::scale_non_uniform(
        f64::from(width) / f64::from(out_w),
        f64::from(height) / f64::from(out_h),
    );
    Ok((out.into_vec(), out_w, out_h, scale))
}

fn pad_type3_rgba(
    rgba: &[u8],
    width: u32,
    height: u32,
    budget: &Budget,
) -> Result<Vec<u8>, LimitExceeded> {
    let row_bytes = width as usize * 4;
    let padded_row = row_bytes + 16;
    let total = padded_row
        .checked_mul(height as usize + 4)
        .ok_or_else(|| LimitExceeded::new(LimitCode::MemorySize))?;
    let mut out = BudgetVec::new(budget);
    out.try_reserve_exact(total)?;
    out.try_extend(std::iter::repeat_n(0, padded_row * 2))?;
    for row in rgba.chunks_exact(row_bytes) {
        out.try_extend_from_slice(&[0; 8])?;
        out.try_extend_from_slice(row)?;
        out.try_extend_from_slice(&[0; 8])?;
    }
    out.try_extend(std::iter::repeat_n(0, padded_row * 2))?;
    Ok(out.into_vec())
}

/// Evaluates axial, radial, and function-based shadings natively in `DeviceCMYK`,
/// `DeviceGray`, or `DeviceRGB` without an intermediate sRGB conversion.
#[expect(
    clippy::too_many_arguments,
    reason = "internal shading rasterizer helper"
)]
fn sample_native_shading(
    s: &ShadingPattern,
    kind: ColorSpaceKind,
    bbox: Rect,
    width: u32,
    height: u32,
    cmy_buf: &mut BudgetVec<'_, u8>,
    k_buf: &mut BudgetVec<'_, u8>,
    may_have_transparency: &mut bool,
    budget: &Budget,
) -> Result<Option<Affine>, LimitExceeded> {
    let bg = s
        .shading
        .background
        .as_ref()
        .map(|vals| components_to_lanes(&s.shading.color_space, kind, vals, s.opacity))
        .unwrap_or(([0, 0, 0, 0], [0, 0, 0, 0]));

    let mut eval_and_push = |vals: Option<&[f32]>| -> Result<(), LimitExceeded> {
        let (cmy, k) = match vals {
            Some(v) => components_to_lanes(&s.shading.color_space, kind, v, s.opacity),
            None => bg,
        };
        *may_have_transparency |= cmy[3] != 255;
        cmy_buf.try_extend_from_slice(&cmy)?;
        k_buf.try_extend_from_slice(&k)?;
        Ok(())
    };

    match s.shading.shading_type.as_ref() {
        ShadingType::FunctionBased {
            domain,
            matrix,
            function,
        } => {
            let base_transform = (s.matrix * *matrix).inverse();
            let domain_rect = Rect::new(
                f64::from(domain[0]),
                f64::from(domain[2]),
                f64::from(domain[1]),
                f64::from(domain[3]),
            );
            let (x_adv, y_adv) = x_y_advances(&base_transform);
            let mut start =
                base_transform * Affine::translate((0.5, 0.5)) * Point::new(bbox.x0, bbox.y0);
            let mut out = [0.0_f32; MAX_FUNC_COMPONENTS];
            let n = function.num_outputs().min(MAX_FUNC_COMPONENTS);
            for y in 0..height {
                if y.is_multiple_of(16) {
                    budget.poll()?;
                }
                let mut pt = start;
                for _ in 0..width {
                    if domain_rect.contains(pt)
                        && function
                            .eval(&[pt.x as f32, pt.y as f32], &mut out[..n])
                            .is_some()
                    {
                        eval_and_push(Some(&out[..n]))?;
                    } else {
                        eval_and_push(None)?;
                    }
                    pt += x_adv;
                }
                start += y_adv;
            }
            Ok(Some(Affine::translate((bbox.x0, bbox.y0))))
        }
        ShadingType::RadialAxial {
            coords,
            domain,
            function,
            extend,
            axial,
        } => {
            let (initial_transform, radial) = if *axial {
                let [x0, y0, x1, y1, _, _] = *coords;
                (
                    ts_from_line_to_line(
                        Point::new(f64::from(x0), f64::from(y0)),
                        Point::new(f64::from(x1), f64::from(y1)),
                        Point::ZERO,
                        Point::new(1.0, 0.0),
                    ),
                    None,
                )
            } else {
                let [x0, y0, r0, x1, y1, r1] = *coords;
                (
                    Affine::translate((-f64::from(x0), -f64::from(y0))),
                    Some((
                        Point::new(f64::from(x1 - x0), f64::from(y1 - y0)),
                        Point::new(f64::from(r0), f64::from(r1)),
                    )),
                )
            };
            let base_transform = initial_transform * s.matrix.inverse();
            let (x_adv, y_adv) = x_y_advances(&base_transform);
            let mut start =
                base_transform * Affine::translate((0.5, 0.5)) * Point::new(bbox.x0, bbox.y0);
            let (t0, t1) = (domain[0], domain[1]);
            let mut out = [0.0_f32; MAX_FUNC_COMPONENTS];
            let n = function.num_outputs().min(MAX_FUNC_COMPONENTS);

            for y in 0..height {
                if y.is_multiple_of(16) {
                    budget.poll()?;
                }
                let mut pt = start;
                for _ in 0..width {
                    let t = match radial {
                        None => Some(pt.x as f32),
                        Some((p1, r)) => radial_pos(&pt, p1, r, extend[0], extend[1]),
                    };
                    let mapped = t.and_then(|mut t| {
                        if t < 0.0 {
                            if extend[0] {
                                t = 0.0;
                            } else {
                                return None;
                            }
                        } else if t > 1.0 {
                            if extend[1] {
                                t = 1.0;
                            } else {
                                return None;
                            }
                        }
                        Some(t0 + (t1 - t0) * t)
                    });
                    if let Some(u) = mapped
                        && eval_shading_fn(function, u, &mut out[..n])
                    {
                        eval_and_push(Some(&out[..n]))?;
                    } else {
                        eval_and_push(None)?;
                    }
                    pt += x_adv;
                }
                start += y_adv;
            }
            Ok(Some(Affine::translate((bbox.x0, bbox.y0))))
        }
        _ => Ok(None),
    }
}

fn eval_shading_fn(function: &ShadingFunction, t: f32, out: &mut [f32]) -> bool {
    function.eval(&[t], out).is_some()
}

fn ts_from_line_to_line(src1: Point, src2: Point, dst1: Point, dst2: Point) -> Affine {
    unit_to_line(dst1, dst2) * unit_to_line(src1, src2).inverse()
}

fn unit_to_line(p0: Point, p1: Point) -> Affine {
    Affine::new([
        p1.y - p0.y,
        p0.x - p1.x,
        p1.x - p0.x,
        p1.y - p0.y,
        p0.x,
        p0.y,
    ])
}

fn radial_pos(pos: &Point, p1: Point, r: Point, min_extend: bool, max_extend: bool) -> Option<f32> {
    let r0 = r.x as f32;
    let dx = p1.x as f32;
    let dy = p1.y as f32;
    let dr = r.y as f32 - r0;
    let px = pos.x as f32;
    let py = pos.y as f32;

    let a = dx * dx + dy * dy - dr * dr;
    let b = -2.0 * (px * dx + py * dy + r0 * dr);
    let c = px * px + py * py - r0 * r0;
    let discriminant = b * b - 4.0 * a * c;
    if discriminant < 0.0 {
        return None;
    }
    if a.abs() < 1e-6 {
        if b.abs() < 1e-6 {
            return None;
        }
        let t = -c / b;
        if (!min_extend && t < 0.0) || (!max_extend && t > 1.0) || r0 + dr * t < 0.0 {
            return None;
        }
        return Some(t);
    }
    let sqrt_d = discriminant.sqrt();
    let t1 = (-b - sqrt_d) / (2.0 * a);
    let t2 = (-b + sqrt_d) / (2.0 * a);
    let max = t1.max(t2);
    let min = t1.min(t2);
    let ok_max = (max_extend || max <= 1.0) && (min_extend || max >= 0.0) && r0 + dr * max >= 0.0;
    let ok_min = (min_extend || min >= 0.0) && (max_extend || min <= 1.0) && r0 + dr * min >= 0.0;
    match (ok_min, ok_max) {
        (_, true) => Some(max),
        (true, false) => Some(min),
        (false, false) => None,
    }
}

/// Renders a PDF [`SoftMask`] into a single-channel `vello_cpu::Mask` using an
/// RGB sub-device (soft masks are alpha/luminance only, independent of CMYK).
#[expect(
    clippy::too_many_arguments,
    reason = "internal soft-mask rasterizer helper"
)]
fn draw_soft_mask<'doc>(
    mask: &SoftMask<'doc>,
    settings: RenderSettings,
    width: u16,
    height: u16,
    cache: &CmykCache<'doc>,
    aliasing_threshold: Option<u8>,
    hinting: bool,
    budget: &Budget,
) -> Result<Mask, LimitExceeded> {
    budget.poll()?;
    let mut ctx = RenderContext::new_with(width, height, derive_settings(&settings));
    ctx.set_aliasing_threshold(aliasing_threshold);

    let bg_color = mask.background_color().to_rgba();
    let apply_bg = bg_color.to_rgba8() != [0, 0, 0, 255];
    if apply_bg {
        ctx.set_paint(AlphaColor::<Srgb>::new(bg_color.components()));
        ctx.fill_rect(&Rect::new(0.0, 0.0, f64::from(width), f64::from(height)));
        ctx.push_layer(None, None, None, None, None);
    }

    {
        let mut device = SoftMaskDevice {
            cache,
            ctx: &mut ctx,
            budget,
            aliasing_threshold,
            hinting,
            ops: 0,
            error: None,
        };
        mask.interpret(&mut device);
        if let Some(err) = device.error.take() {
            return Err(err);
        }
    }

    if apply_bg {
        ctx.pop_layer();
    }

    let mut pix = Pixmap::new(width, height);
    ctx.flush();
    ctx.render(&mut pix, &mut Resources::default());

    let mut rendered = match mask.mask_type() {
        MaskType::Luminosity => Mask::new_luminance(&pix),
        MaskType::Alpha => Mask::new_alpha(&pix),
    };

    if let Some(tf) = mask.transfer_function() {
        let len = usize::from(rendered.width()) * usize::from(rendered.height());
        let mut map = BudgetVec::new(budget);
        map.try_reserve_exact(len)?;
        for y in 0..rendered.height() {
            for x in 0..rendered.width() {
                map.try_push(rendered.sample(x, y))?;
            }
        }
        let mut raw = map.into_vec();
        tf.apply_to(&mut raw);
        rendered = Mask::from_parts(raw, rendered.width(), rendered.height());
    }

    Ok(rendered)
}

/// Minimal single-context RGB `Device` used exclusively for rendering `SoftMask`
/// luminosity/alpha channels.
struct SoftMaskDevice<'doc, 'r, 'b> {
    cache: &'r CmykCache<'doc>,
    ctx: &'r mut RenderContext,
    budget: &'b Budget,
    aliasing_threshold: Option<u8>,
    hinting: bool,
    ops: u32,
    error: Option<LimitExceeded>,
}

impl<'doc> SoftMaskDevice<'doc, '_, '_> {
    fn poll(&mut self) -> bool {
        if self.error.is_some() {
            return false;
        }
        self.ops = self.ops.wrapping_add(1);
        if self.ops.is_multiple_of(POLL_INTERVAL)
            && let Err(err) = self.budget.poll()
        {
            self.error = Some(err);
            return false;
        }
        true
    }

    fn set_rgb_paint(
        &mut self,
        paint: &Paint<'_>,
        path_bbox: impl Fn() -> Rect,
    ) -> Option<BezPath> {
        let mut paint_transform = Affine::IDENTITY;
        let mut clip_path = None;
        let vello_paint: PaintType = match paint {
            Paint::Color(c) => {
                let c = c.to_rgba().to_rgba8();
                AlphaColor::from_rgba8(c[0], c[1], c[2], c[3]).into()
            }
            Paint::Pattern(p) => {
                let path_transform = *self.ctx.transform();
                let bbox = path_transform
                    .transform_rect_bbox(path_bbox())
                    .intersect(Rect::new(
                        0.0,
                        0.0,
                        f64::from(self.ctx.width()),
                        f64::from(self.ctx.height()),
                    ));
                match p.as_ref() {
                    Pattern::Shading(s) => {
                        clip_path = s.shading.clip_path.clone();
                        if bbox.is_zero_area() {
                            AlphaColor::<Srgb>::TRANSPARENT.into()
                        } else {
                            let (w, h) = texture_dimensions(bbox, 1.0);
                            if w == 0
                                || h == 0
                                || w > u32::from(u16::MAX)
                                || h > u32::from(u16::MAX)
                            {
                                AlphaColor::<Srgb>::TRANSPARENT.into()
                            } else {
                                let mut buf = BudgetVec::new(self.budget);
                                if buf.try_reserve_exact(w as usize * h as usize * 4).is_err() {
                                    self.error = Some(LimitExceeded::new(LimitCode::MemorySize));
                                    AlphaColor::<Srgb>::TRANSPARENT.into()
                                } else {
                                    let encoded = s.encode();
                                    let mut has_alpha = false;
                                    let (_, _, tr) = encoded.sample_texture(bbox, 1.0, |sample| {
                                        let px = [
                                            unit_to_u8(sample[0]),
                                            unit_to_u8(sample[1]),
                                            unit_to_u8(sample[2]),
                                            unit_to_u8(sample[3]),
                                        ];
                                        has_alpha |= px[3] != 255;
                                        let _ = buf.try_extend_from_slice(&px);
                                    });
                                    paint_transform = path_transform.inverse() * tr;
                                    let pix = Pixmap::from_parts(
                                        buf.into_vec(),
                                        w as u16,
                                        h as u16,
                                        PixelMetadata {
                                            may_have_transparency: has_alpha,
                                            alpha_type: ImageAlphaType::Alpha,
                                        },
                                    );
                                    PaintType::Image(VelloImage {
                                        image: ImageSource::Pixmap(Arc::new(pix)),
                                        sampler: ImageSampler {
                                            x_extend: peniko::Extend::Pad,
                                            y_extend: peniko::Extend::Pad,
                                            quality: ImageQuality::Medium,
                                            alpha: 1.0,
                                        },
                                    })
                                }
                            }
                        }
                    }
                    Pattern::Tiling(_) => AlphaColor::<Srgb>::TRANSPARENT.into(),
                }
            }
        };
        self.ctx.set_paint_transform(paint_transform);
        self.ctx.set_paint(vello_paint);
        clip_path
    }
}

impl<'doc> Device<'doc> for SoftMaskDevice<'doc, '_, '_> {
    fn draw_path(&mut self, path: &BezPath, props: DrawProps<'doc>, draw_mode: &DrawMode) {
        if !self.poll() {
            return;
        }
        self.ctx.set_transform(props.transform);
        self.ctx
            .set_blend_mode(convert_blend_mode(props.blend_mode));
        match draw_mode {
            DrawMode::Fill(f) => {
                self.ctx.set_fill_rule(convert_fill_rule(*f));
                let clip = self.set_rgb_paint(&props.paint, || path.bounding_box());
                if let Some(cp) = clip.as_ref() {
                    let old = *self.ctx.transform();
                    self.ctx.set_transform(Affine::IDENTITY);
                    self.ctx.push_clip_path(cp);
                    self.ctx.set_transform(old);
                }
                self.ctx.fill_path(path);
                if clip.is_some() {
                    self.ctx.pop_clip();
                }
            }
            DrawMode::Stroke(s) => {
                self.ctx
                    .set_stroke(make_stroke(self.ctx.transform(), s, false, false, false));
                let clip = self.set_rgb_paint(&props.paint, || path.bounding_box());
                if let Some(cp) = clip.as_ref() {
                    let old = *self.ctx.transform();
                    self.ctx.set_transform(Affine::IDENTITY);
                    self.ctx.push_clip_path(cp);
                    self.ctx.set_transform(old);
                }
                self.ctx.stroke_path(path);
                if clip.is_some() {
                    self.ctx.pop_clip();
                }
            }
            DrawMode::FillAndStroke(f, s) => {
                self.draw_path(path, props.clone(), &DrawMode::Fill(*f));
                self.draw_path(path, props, &DrawMode::Stroke(s.clone()));
            }
            DrawMode::Invisible => {}
        }
    }

    fn draw_rect(&mut self, rect: &Rect, props: DrawProps<'doc>, draw_mode: &DrawMode) {
        self.draw_path(&rect.to_path(0.1), props, draw_mode);
    }

    fn push_clip_path(&mut self, clip_path: &ClipPath) {
        let old = *self.ctx.transform();
        self.ctx.set_fill_rule(convert_fill_rule(clip_path.fill));
        self.ctx.set_transform(Affine::IDENTITY);
        self.ctx.push_clip_path(&clip_path.path);
        self.ctx.set_transform(old);
    }

    fn push_clip_rect(&mut self, rect: &Rect) {
        self.ctx.set_fill_rule(Fill::NonZero);
        self.ctx.set_transform(Affine::IDENTITY);
        self.ctx.push_clip_rect(rect);
    }

    fn pop_clip(&mut self) {
        self.ctx.pop_clip();
    }

    fn push_transparency_group(
        &mut self,
        opacity: f32,
        _mask: Option<SoftMask<'doc>>,
        blend_mode: BlendMode,
    ) {
        self.ctx.push_layer(
            None,
            Some(convert_blend_mode(blend_mode)),
            Some(opacity),
            None,
            None,
        );
    }

    fn pop_transparency_group(&mut self) {
        self.ctx.pop_layer();
    }

    fn draw_glyph_run(
        &mut self,
        glyph_run: &GlyphRun<'_, 'doc>,
        props: DrawProps<'doc>,
        draw_mode: &DrawMode,
    ) {
        if !self.poll() {
            return;
        }
        let hint = self.hinting && glyph_run.writing_mode() == WritingMode::Horizontal;
        for glyph in glyph_run.glyphs() {
            match &**glyph {
                Glyph::Outline(outline) => {
                    let tr = props.transform * glyph.transform();
                    let [a, b, c, d, e, f] = tr.as_coeffs();
                    let ppem = d.abs() * 1000.0;
                    let (path, tr) = if hint
                        && b == 0.0
                        && (MIN_HINTING_PPEM..=MAX_HINTING_PPEM).contains(&ppem)
                        && let Some(h) =
                            outline.hinted_outline(ppem as f32, &self.cache.hinting_cache)
                    {
                        (h, Affine::new([a, b, c, d, e, f.round()]))
                    } else {
                        (outline.outline(), tr)
                    };
                    let props = DrawProps {
                        transform: tr,
                        ..props.clone()
                    };
                    self.draw_path(&path, props, draw_mode);
                }
                Glyph::Type3(t3) => {
                    t3.interpret(self, props.transform, glyph.transform(), &props.paint);
                }
            }
        }
    }

    fn draw_image(
        &mut self,
        image: hayro::hayro_interpret::Image<'doc, '_>,
        props: ImageDrawProps<'doc>,
    ) {
        if !self.poll() {
            return;
        }
        let mut transform = props.transform;
        self.ctx.set_aliasing_threshold(Some(1));
        if let hayro::hayro_interpret::Image::Raster(r) = image {
            r.with_rgba(
                |img, alpha| {
                    let (sx, sy) = img.scale_factors();
                    transform *= Affine::scale_non_uniform(f64::from(sx), f64::from(sy));
                    let (w, h) = (img.width(), img.height());
                    let (Ok(w16), Ok(h16)) = (u16::try_from(w), u16::try_from(h)) else {
                        return;
                    };
                    let mut rgba = BudgetVec::new(self.budget);
                    if rgba.try_reserve_exact(w as usize * h as usize * 4).is_err() {
                        self.error = Some(LimitExceeded::new(LimitCode::MemorySize));
                        return;
                    }
                    let alpha_slice = alpha.as_ref().map(|a| a.data.as_slice());
                    match img {
                        ImageData::Luma(l) => {
                            for (i, &g) in l.data.iter().enumerate() {
                                let a = alpha_slice.and_then(|s| s.get(i).copied()).unwrap_or(255);
                                let _ = rgba.try_extend_from_slice(&[g, g, g, a]);
                            }
                        }
                        ImageData::Rgb(r) => {
                            for (i, px) in r.data.as_chunks::<3>().0.iter().enumerate() {
                                let a = alpha_slice.and_then(|s| s.get(i).copied()).unwrap_or(255);
                                let _ = rgba.try_extend_from_slice(&[px[0], px[1], px[2], a]);
                            }
                        }
                    }
                    let pix = Arc::new(Pixmap::from_parts(
                        rgba.into_vec(),
                        w16,
                        h16,
                        PixelMetadata {
                            may_have_transparency: alpha.is_some(),
                            alpha_type: ImageAlphaType::Alpha,
                        },
                    ));
                    self.ctx.set_transform(transform);
                    self.ctx.set_paint(VelloImage {
                        image: ImageSource::Pixmap(pix),
                        sampler: ImageSampler {
                            x_extend: peniko::Extend::Pad,
                            y_extend: peniko::Extend::Pad,
                            quality: ImageQuality::Medium,
                            alpha: 1.0,
                        },
                    });
                    self.ctx
                        .fill_rect(&Rect::new(0.0, 0.0, f64::from(w16), f64::from(h16)));
                },
                None,
            );
        }
        self.ctx.set_aliasing_threshold(self.aliasing_threshold);
    }
}

struct TileAxis {
    start: f64,
    scale: f64,
    size: u16,
    step: f64,
    copies: std::ops::RangeInclusive<i32>,
    extend: peniko::Extend,
}

impl TileAxis {
    fn new(scale: f64, cell: std::ops::Range<f64>, step: f32, area: std::ops::Range<f64>) -> Self {
        let step = f64::from(step.abs());
        if step <= 0.0 || !step.is_finite() {
            return Self {
                start: cell.start,
                scale: 1.0,
                size: 0,
                step: 1.0,
                copies: std::ops::RangeInclusive::new(1, 0),
                extend: peniko::Extend::Pad,
            };
        }
        let width = cell.end - cell.start;
        let visible = (area.end - area.start).max(0.0);
        let repeat = visible >= step;
        let scale = (step * scale).round().max(1.0) / step;

        if repeat {
            Self {
                start: cell.start,
                scale,
                size: (step * scale).round().clamp(0.0, f64::from(u16::MAX)) as u16,
                step,
                copies: (-width / step).floor() as i32 + 1..=0,
                extend: peniko::Extend::Repeat,
            }
        } else {
            let start = cell.start + ((area.start - cell.start) * scale).floor() / scale;
            Self {
                start,
                scale,
                size: ((area.end - start) * scale)
                    .ceil()
                    .clamp(1.0, f64::from(u16::MAX)) as u16,
                step,
                copies: ((area.start - cell.end) / step).ceil() as i32
                    ..=((area.end - cell.start) / step).floor() as i32,
                extend: peniko::Extend::Pad,
            }
        }
    }
}

fn derive_settings(settings: &RenderSettings) -> RenderSettings {
    RenderSettings {
        num_threads: 0,
        ..*settings
    }
}

fn convert_fill_rule(fill_rule: FillRule) -> Fill {
    match fill_rule {
        FillRule::NonZero => Fill::NonZero,
        FillRule::EvenOdd => Fill::EvenOdd,
    }
}

fn convert_blend_mode(blend_mode: BlendMode) -> peniko::BlendMode {
    let mix = match blend_mode {
        BlendMode::Normal => Mix::Normal,
        BlendMode::Multiply => Mix::Multiply,
        BlendMode::Screen => Mix::Screen,
        BlendMode::Overlay => Mix::Overlay,
        BlendMode::Darken => Mix::Darken,
        BlendMode::Lighten => Mix::Lighten,
        BlendMode::ColorDodge => Mix::ColorDodge,
        BlendMode::ColorBurn => Mix::ColorBurn,
        BlendMode::HardLight => Mix::HardLight,
        BlendMode::SoftLight => Mix::SoftLight,
        BlendMode::Difference => Mix::Difference,
        BlendMode::Exclusion => Mix::Exclusion,
        BlendMode::Hue => Mix::Hue,
        BlendMode::Saturation => Mix::Saturation,
        BlendMode::Color => Mix::Color,
        BlendMode::Luminosity => Mix::Luminosity,
    };
    peniko::BlendMode::new(mix, Compose::SrcOver)
}

fn max_factor(transform: &Affine) -> f32 {
    let c = transform.as_coeffs();
    let linear = Affine::new([c[0], c[1], c[2], c[3], 0.0, 0.0]);
    let x_adv = linear * Point::new(1.0, 0.0);
    let y_adv = linear * Point::new(0.0, 1.0);
    x_adv.to_vec2().length().max(y_adv.to_vec2().length()) as f32
}

fn make_stroke(
    transform: &Affine,
    stroke_props: &StrokeProps,
    is_text: bool,
    inside_pattern: bool,
    in_type3_glyph: bool,
) -> hayro::kurbo::Stroke {
    let threshold = if is_text { 0.25 } else { 1.0 };
    let min_factor = max_factor(transform).max(1e-6);
    let mut line_width = stroke_props.line_width.max(0.01 / min_factor);
    let transformed_width = line_width * min_factor;
    if transformed_width < threshold && !inside_pattern && !in_type3_glyph {
        line_width /= transformed_width;
        line_width *= threshold;
    }
    let is_thin = transformed_width <= 1.0;
    let cap = match stroke_props.line_cap {
        Cap::Round if is_thin => Cap::Square,
        cap => cap,
    };
    let join = match stroke_props.line_join {
        Join::Round if is_thin => Join::Bevel,
        join => join,
    };
    let mut dashes = hayro::kurbo::Dashes::new();
    for &n in &stroke_props.dash_array {
        dashes.push(f64::from(n));
    }
    hayro::kurbo::Stroke {
        width: f64::from(line_width),
        join,
        miter_limit: f64::from(stroke_props.miter_limit),
        start_cap: cap,
        end_cap: cap,
        dash_pattern: dashes,
        dash_offset: f64::from(stroke_props.dash_offset),
    }
}

fn single_segment_outline(path: &BezPath, stroke: &hayro::kurbo::Stroke) -> Option<BezPath> {
    if !stroke.dash_pattern.is_empty() || stroke.start_cap != stroke.end_cap {
        return None;
    }
    let [PathEl::MoveTo(p0), PathEl::LineTo(p1)] = path.elements() else {
        return None;
    };
    let d = *p1 - *p0;
    let len = d.hypot();
    if len <= 0.0 || !len.is_finite() {
        return None;
    }
    let half = 0.5 * stroke.width;
    let dir = d / len;
    let n = Vec2::new(-dir.y, dir.x) * half;
    let (a, b) = match stroke.start_cap {
        Cap::Butt => (*p0, *p1),
        Cap::Square => (*p0 - dir * half, *p1 + dir * half),
        Cap::Round => return None,
    };
    let mut outline = BezPath::new();
    outline.move_to(a + n);
    outline.line_to(b + n);
    outline.line_to(b - n);
    outline.line_to(a - n);
    outline.close_path();
    Some(outline)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bridge::Bridged;
    use crate::render::{Fallback, Scale, raster_scaled};

    /// Builds a minimal valid PDF from numbered object bodies.
    fn build_pdf(objects: &[(usize, Vec<u8>)]) -> Vec<u8> {
        let mut out = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n".to_vec();
        let max_id = objects.iter().map(|(id, _)| *id).max().unwrap_or(0);
        let mut offsets = vec![0usize; max_id + 1];
        for (id, body) in objects {
            offsets[*id] = out.len();
            out.extend_from_slice(format!("{id} 0 obj\n").as_bytes());
            out.extend_from_slice(body);
            out.extend_from_slice(b"\nendobj\n");
        }
        let xref = out.len();
        out.extend_from_slice(format!("xref\n0 {}\n0000000000 65535 f \n", max_id + 1).as_bytes());
        for offset in &offsets[1..] {
            out.extend_from_slice(format!("{offset:010} 00000 n \n").as_bytes());
        }
        out.extend_from_slice(
            format!(
                "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n",
                max_id + 1
            )
            .as_bytes(),
        );
        out
    }

    fn single_page_pdf(
        width: u32,
        height: u32,
        resources: &str,
        content: &str,
        extra: &[(usize, Vec<u8>)],
    ) -> Arc<Bridged> {
        let mut objs = vec![
            (1, b"<< /Type /Catalog /Pages 2 0 R >>".to_vec()),
            (2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>".to_vec()),
            (
                3,
                format!(
                    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] \
                     /Resources << {resources} >> /Contents 4 0 R >>"
                )
                .into_bytes(),
            ),
            (
                4,
                format!(
                    "<< /Length {} >>\nstream\n{content}\nendstream",
                    content.len()
                )
                .into_bytes(),
            ),
        ];
        objs.extend_from_slice(extra);
        Arc::new(Bridged::of_bytes(build_pdf(&objs)))
    }

    fn cmyk_options(alpha: bool) -> Options {
        Options {
            colorspace: Colorspace::Cmyk,
            ..Options::new((1.0, 0.0, 0.0, 1.0), alpha)
        }
    }

    fn pixel_at(raster: &Raster, x: usize, y: usize) -> &[u8] {
        let channels = 4 + usize::from(raster.alpha);
        let off = (y * usize::from(raster.width) + x) * channels;
        &raster.data[off..off + channels]
    }

    #[test]
    fn device_cmyk_and_gray_and_rgb_and_blend_modes_are_exact() {
        let content = "\
            1 0 0 0 k 0 50 10 10 re f \
            0 1 0 0 k 10 50 10 10 re f \
            0 0 1 0 k 20 50 10 10 re f \
            0 0 0 1 k 30 50 10 10 re f \
            0.2 0.4 0.6 0.8 k 40 50 10 10 re f \
            0.5 g 50 50 10 10 re f \
            1 0 0 rg 60 50 10 10 re f \
            0.5 0.5 0.5 rg 70 50 10 10 re f \
            q /Half gs 0 0 0 1 k 0 30 10 10 re f Q \
            q 0 0 1 0 k 10 30 10 10 re f /Mul gs 0 0 0 1 k 10 30 10 10 re f Q";
        let bridged = single_page_pdf(
            80,
            60,
            "/ExtGState << /Half << /ca 0.5 >> /Mul << /BM /Multiply >> >>",
            content,
            &[],
        );
        let budget = Budget::default();
        let raster = raster_scaled(
            &bridged,
            None,
            &Fallback::default(),
            0,
            &cmyk_options(false),
            Scale::Matrix((1.0, 0.0, 0.0, 1.0)),
            &budget,
        )
        .unwrap();

        assert_eq!(raster.colorspace, Colorspace::Cmyk);
        assert!(!raster.alpha);
        assert_eq!(raster.data.len(), 80 * 60 * 4);
        // White background is zero ink.
        assert_eq!(pixel_at(&raster, 75, 55), &[0, 0, 0, 0]);
        // Top row (PDF y=50..60 -> raster y=0..10).
        assert_eq!(pixel_at(&raster, 5, 5), &[255, 0, 0, 0]);
        assert_eq!(pixel_at(&raster, 15, 5), &[0, 255, 0, 0]);
        assert_eq!(pixel_at(&raster, 25, 5), &[0, 0, 255, 0]);
        assert_eq!(pixel_at(&raster, 35, 5), &[0, 0, 0, 255]);
        assert_eq!(pixel_at(&raster, 45, 5), &[51, 102, 153, 204]);
        // DeviceGray 0.5 -> pure K.
        assert_eq!(pixel_at(&raster, 55, 5), &[0, 0, 0, 128]);
        // DeviceRGB red & 50% gray.
        assert_eq!(pixel_at(&raster, 65, 5), &[0, 255, 255, 0]);
        assert_eq!(pixel_at(&raster, 75, 5), &[0, 0, 0, 128]);
        // 50% alpha black over white -> (0, 0, 0, 128).
        assert_eq!(pixel_at(&raster, 5, 25), &[0, 0, 0, 128]);
        // Black multiplied over yellow -> (0, 0, 255, 255).
        assert_eq!(pixel_at(&raster, 15, 25), &[0, 0, 255, 255]);
    }

    #[test]
    fn device_cmyk_raster_image_avoids_rgb_round_trip() {
        let img_k = b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 \
            /ColorSpace /DeviceCMYK /BitsPerComponent 8 /Length 4 >>\nstream\n\x00\x00\x00\xff\nendstream"
            .to_vec();
        let img_tint = b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 \
            /ColorSpace /DeviceCMYK /BitsPerComponent 8 /Length 4 >>\nstream\n\x33\x66\x99\x1a\nendstream"
            .to_vec();
        let bridged = single_page_pdf(
            20,
            10,
            "/XObject << /Im0 5 0 R /Im1 6 0 R >>",
            "q 10 0 0 10 0 0 cm /Im0 Do Q q 10 0 0 10 10 0 cm /Im1 Do Q",
            &[(5, img_k), (6, img_tint)],
        );
        let budget = Budget::default();
        let raster = raster_scaled(
            &bridged,
            None,
            &Fallback::default(),
            0,
            &cmyk_options(false),
            Scale::Matrix((1.0, 0.0, 0.0, 1.0)),
            &budget,
        )
        .unwrap();

        assert_eq!(pixel_at(&raster, 5, 5), &[0, 0, 0, 255]);
        assert_eq!(pixel_at(&raster, 15, 5), &[51, 102, 153, 26]);
    }

    #[test]
    fn alpha_mode_produces_five_channel_cmyka() {
        let bridged = single_page_pdf(
            20,
            10,
            "/ExtGState << /Half << /ca 0.5 >> >>",
            "0 0 0 1 k 0 0 10 10 re f /Half gs 1 0 0 0 k 10 0 10 10 re f",
            &[],
        );
        let budget = Budget::default();
        let mut opts = cmyk_options(true);
        opts.clip = Some((0.0, 0.0, 20.0, 10.0));
        let raster = raster_scaled(
            &bridged,
            None,
            &Fallback::default(),
            0,
            &opts,
            Scale::Matrix((1.0, 0.0, 0.0, 1.0)),
            &budget,
        )
        .unwrap();

        assert_eq!(raster.data.len(), 20 * 10 * 5);
        assert_eq!(pixel_at(&raster, 5, 5), &[0, 0, 0, 255, 255]);
        assert_eq!(pixel_at(&raster, 15, 5), &[255, 0, 0, 0, 128]);

        // Unpainted transparent pixels hold all zeroes.
        let blank = single_page_pdf(10, 10, "", "", &[]);
        let empty = raster_scaled(
            &blank,
            None,
            &Fallback::default(),
            0,
            &cmyk_options(true),
            Scale::Matrix((1.0, 0.0, 0.0, 1.0)),
            &budget,
        )
        .unwrap();
        assert_eq!(pixel_at(&empty, 0, 0), &[0, 0, 0, 0, 0]);
    }

    #[test]
    fn clipped_viewport_on_huge_page_uses_viewport_memory_and_enforces_budget() {
        let bridged = single_page_pdf(10_000, 9_000, "", "0 0 0 1 k 4700 3073 43 27 re f", &[]);
        // Tight memory limit (256 KiB): full 10k*9k page would need >1.8 GiB,
        // but a 43x27 clipped viewport uses <30 KiB and succeeds.
        let small_budget = Budget::default().with_limit(LimitCode::MemorySize, Some(256 * 1024));
        let mut opts = cmyk_options(false);
        opts.clip = Some((4_700.0, 5_900.0, 4_743.0, 5_927.0));
        let raster = raster_scaled(
            &bridged,
            None,
            &Fallback::default(),
            0,
            &opts,
            Scale::Matrix((1.0, 0.0, 0.0, 1.0)),
            &small_budget,
        )
        .unwrap();
        assert_eq!((raster.width, raster.height), (43, 27));
        assert_eq!(pixel_at(&raster, 10, 10), &[0, 0, 0, 255]);

        // Tiny memory budget smaller than the viewport allocation is refused cleanly.
        let tiny_budget = Budget::default().with_limit(LimitCode::MemorySize, Some(1024));
        let err = raster_scaled(
            &bridged,
            None,
            &Fallback::default(),
            0,
            &opts,
            Scale::Matrix((1.0, 0.0, 0.0, 1.0)),
            &tiny_budget,
        )
        .unwrap_err();
        assert!(matches!(err, RasterError::Limit(l) if l.code() == LimitCode::MemorySize));
    }

    #[test]
    fn device_cmyk_axial_shading_stays_native() {
        let shading = b"<< /ShadingType 2 /ColorSpace /DeviceCMYK /Coords [0 0 100 0] \
            /Extend [true true] /Function << /FunctionType 2 /Domain [0 1] \
            /C0 [0 0 0 0] /C1 [0 0 0 1] /N 1 >> >>"
            .to_vec();
        let bridged = single_page_pdf(
            100,
            20,
            "/Shading << /Sh0 5 0 R >>",
            "/Sh0 sh",
            &[(5, shading)],
        );
        let budget = Budget::default();
        let raster = raster_scaled(
            &bridged,
            None,
            &Fallback::default(),
            0,
            &cmyk_options(false),
            Scale::Matrix((1.0, 0.0, 0.0, 1.0)),
            &budget,
        )
        .unwrap();
        // Every pixel across the K-only gradient has zero C, M, Y.
        for x in 0..100 {
            let px = pixel_at(&raster, x, 10);
            assert_eq!(&px[..3], &[0, 0, 0], "x={x} had non-zero CMY: {px:?}");
        }
        assert!(pixel_at(&raster, 0, 10)[3] <= 3);
        assert!(pixel_at(&raster, 99, 10)[3] >= 252);
    }
}
