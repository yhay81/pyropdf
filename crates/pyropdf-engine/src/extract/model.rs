//! The content model: every text run, path, and image that hayro paints on a
//! page, in paint order and display space.
//!
//! `ContentCollector` is the one hayro `Device` that reads page content. It
//! records what each paint callback receives together with the clips,
//! transparency groups, and marked content that enclose it, and interns fonts
//! and graphics contexts so that items share them. Paths go into the columns
//! of one [`PathTable`], so that a page of many paths costs bytes per segment,
//! not allocations per path. A `ContentSink` sees the same paths and glyphs,
//! so text extraction reads what the items show; with items off, the
//! collector only feeds its sink. Glyph boxes come from the outlines that
//! hayro computes, and from interpreting Type 3 glyph procedures. A font that
//! a document does not embed is described on the fallback face that draws it,
//! which a second interpretation reads. The budget's `content_element_count`
//! bounds glyphs, path and clip segments, stroke dash lengths, and items of the
//! first, and every collection grows fallibly, so a refusal returns no partial
//! result.

use std::collections::HashMap;
use std::ops::Range;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use crate::display::Display;
use hayro::hayro_interpret::color::ColorSpace as HayroColorSpace;
use hayro::hayro_interpret::font::{Glyph, GlyphRun, OutlineGlyph, Type3Glyph};
use hayro::hayro_interpret::hayro_cmap::{BfString, WritingMode};
use hayro::hayro_interpret::pattern::{Pattern, ShadingPattern, TilingPattern};
use hayro::hayro_interpret::shading::ShadingType;
use hayro::hayro_interpret::{
    BlendMode, CacheKey, ClipPath, Device, DrawMode, DrawProps, FillRule, Image, ImageDrawProps,
    InterpreterCache, InterpreterSettings, MaskType, Paint, RenderContent, SoftMask, StrokeProps,
    interpret_page,
};
use hayro::hayro_syntax::Filter;
use hayro::hayro_syntax::object::dict::keys::{ACTUAL_TEXT, MCID};
use hayro::hayro_syntax::object::{Array, Dict, Name, Stream, String as PdfString};
use hayro::hayro_syntax::page::Page;
use hayro::hayro_syntax::xref::XRef;
use hayro::kurbo::{Affine, BezPath, Cap, Join, PathEl, Point, Rect, Shape};
use hayro_font::Font;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};

use super::images::{
    CapturedRaster, CapturedStencil, capture_raster_image, image_interpolate, pack_stencil_mask,
};
use super::stretch::{Stretches, without_stretch};
use super::{
    DEFAULT_IMAGE_RESOLUTION, Error, extraction_context, image_color_space, jpeg_resolution,
};
use crate::render::Fallback;

/// A display-space box: `x0`, `y0`, `x1`, `y1`.
pub type BBox = (f64, f64, f64, f64);
type Coefficients = [f64; 6];

/// Stored PDF color metadata: color space code (`0` when absent, `1..=11` for
/// PDF color space families) and original component values in that color space.
pub type StoredColorTuple = (u8, Vec<f64>);

/// A paint: kind 0 is a color (red, green, blue, alpha in sRGB, plus optional
/// stored PDF color space and original components), kind 1 a shading pattern
/// (shading type, or 0 for a shading that hayro could not read, the six matrix
/// coefficients, and opacity), and kind 2 a tiling pattern (the four
/// pattern-space bbox coordinates, x step, y step, the six matrix coefficients,
/// and opacity). A path pattern appends its stable identity within the original
/// table, so equal metadata never aliases two different colour functions or
/// tiling cells after crossing into Python.
pub type PaintTuple = (u8, Vec<f64>, Option<StoredColorTuple>);
/// A stroke: paint, width, cap, join, miter limit, dash lengths, dash offset.
pub type StrokeTuple = (PaintTuple, f64, u8, u8, f64, Vec<f64>, f64);
/// What every item carries: its graphics context, blend mode, and the type of
/// its soft mask, 0 for alpha and 1 for luminosity.
pub type ItemContextTuple = (u32, u8, Option<u8>);
/// Glyph IDs, Unicode, matrices (six coefficients per glyph), advances, and
/// ink boxes.
pub type GlyphsTuple = (
    Vec<Option<u32>>,
    Vec<Option<String>>,
    Vec<f64>,
    Vec<Option<f64>>,
    Vec<Option<BBox>>,
);
/// A text run: glyphs, font index, render mode, fill paint, stroke, bbox,
/// item context, redraw replay metadata, and writing mode (`0` horizontal,
/// `1` vertical).
pub type TextRunTuple = (
    GlyphsTuple,
    u32,
    u8,
    Option<PaintTuple>,
    Option<StrokeTuple>,
    BBox,
    ItemContextTuple,
    super::redraw::Replay,
    u8,
);
/// A path: segment kinds, coordinates, bbox, fill paint, even-odd fill rule,
/// stroke, and item context.
pub type PathTuple = (
    Vec<u8>,
    Vec<f64>,
    BBox,
    Option<PaintTuple>,
    bool,
    Option<StrokeTuple>,
    ItemContextTuple,
);
/// An image's pixel width and height, bits per component, color components,
/// and color space name.
pub type ImageFormatTuple = (u32, u32, u8, u8, String);
/// An image: xref, format, stencil flag, mask kind, encoded size, resolution,
/// unit-square matrix, bbox, item context, optional stencil fill paint, and
/// optional index into [`PathTable::inline_images`] for inline payloads.
pub type ImageTuple = (
    u32,
    ImageFormatTuple,
    bool,
    u8,
    usize,
    (u32, u32),
    Coefficients,
    BBox,
    ItemContextTuple,
    Option<PaintTuple>,
    Option<u32>,
);
/// A font: name, Type 3 flag, weight, italic, serif, monospace, PDF dictionary
/// object number (`None` when direct or absent), PDF `/Subtype`, and whether
/// the font embeds a font program stream.
pub type FontTuple = (
    String,
    bool,
    Option<u32>,
    bool,
    bool,
    bool,
    Option<u32>,
    String,
    bool,
);
/// A clip: segment kinds, coordinates, even-odd fill rule, and bbox.
pub type ClipTuple = (Vec<u8>, Vec<f64>, bool, BBox);
/// A transparency group: opacity, blend mode, and soft mask type, as an item's.
pub type GroupTuple = (f64, u8, Option<u8>);
/// A marked-content sequence: tag and MCID.
pub type MarkTuple = (String, Option<i32>);
/// A graphics context: clip, group, and marked-content indexes, outermost
/// first.
pub type StateTuple = (Vec<u32>, Vec<u32>, Vec<u32>);

/// Segment kinds, as the PDF path operators name them.
const MOVE: u8 = b'm';
const LINE: u8 = b'l';
const CURVE: u8 = b'c';
const CLOSE: u8 = b'h';

/// The kind of a color in a [`PaintTuple`].
const COLOR: u8 = 0;

/// Text render modes the Device API distinguishes.
pub const RENDER_FILL: u8 = 0;
pub const RENDER_STROKE: u8 = 1;
pub const RENDER_FILL_STROKE: u8 = 2;
pub const RENDER_INVISIBLE: u8 = 3;

/// Text run writing modes reported by `GlyphRun::writing_mode()`.
pub const WRITING_HORIZONTAL: u8 = 0;
pub const WRITING_VERTICAL: u8 = 1;

/// Stored PDF color space family codes reported from `Color::color_space()`.
const COLOR_SPACE_NONE: u8 = 0;
const COLOR_SPACE_DEVICE_GRAY: u8 = 1;
const COLOR_SPACE_DEVICE_RGB: u8 = 2;
const COLOR_SPACE_DEVICE_CMYK: u8 = 3;
const COLOR_SPACE_ICC_BASED: u8 = 4;
const COLOR_SPACE_CAL_GRAY: u8 = 5;
const COLOR_SPACE_CAL_RGB: u8 = 6;
const COLOR_SPACE_LAB: u8 = 7;
const COLOR_SPACE_INDEXED: u8 = 8;
const COLOR_SPACE_SEPARATION: u8 = 9;
const COLOR_SPACE_DEVICE_N: u8 = 10;
const COLOR_SPACE_PATTERN: u8 = 11;

/// Image mask kinds.
const MASK_NONE: u8 = 0;
const MASK_SOFT: u8 = 1;
const MASK_EXPLICIT: u8 = 2;
const MASK_COLOR_KEY: u8 = 3;

/// Glyph outlines kept per interpretation, so that a page reusing many
/// distinct glyphs cannot grow the cache without bound.
const MAX_CACHED_GLYPHS: usize = 65_536;

/// Maximum nesting depth of tiling patterns and Type 3 glyph procedures
/// captured inside a tiling pattern cell (DESIGN.md F6).
const MAX_TILING_DEPTH: u8 = 4;

/// Maximum clip or transparency group stack depth inside a tiling cell.
const MAX_TILING_STACK_DEPTH: usize = 64;

/// Owned image payload captured for an inline raster image or inline stencil
/// mask (`xref == 0`) so `Canvas.draw` can promote it to a PDF Image XObject.
#[derive(Clone, Debug)]
pub enum InlineImageData {
    Raster(CapturedRaster),
    Stencil(CapturedStencil),
}

/// Paint used by an operation inside a [`CapturedTilingCell`].
#[derive(Clone, Debug)]
pub enum CellPaint {
    /// Solid sRGB color with alpha `(r, g, b, a)`.
    Color([f64; 4]),
    /// Owned axial or radial shading pattern, sampled lazily on draw, with its
    /// pattern-space matrix.
    Shading {
        shading: Box<ShadingPattern>,
        matrix: Coefficients,
    },
    /// Nested tiling pattern cell (bounded by `MAX_TILING_DEPTH`), with its
    /// pattern-space matrix and overall opacity.
    Tiling {
        cell: Box<CapturedTilingCell>,
        matrix: Coefficients,
        opacity: f64,
    },
}

/// Stroke style and paint for a path inside a [`CapturedTilingCell`].
#[derive(Clone, Debug)]
pub struct CellStroke {
    pub paint: CellPaint,
    pub width: f64,
    pub cap: u8,
    pub join: u8,
    pub miter_limit: f64,
    pub dashes: Vec<f64>,
    pub dash_offset: f64,
}

/// One painted or graphics-state operation captured inside a tiling cell in
/// pattern space.
#[derive(Clone, Debug)]
pub enum TilingCellOp {
    PushClip {
        kinds: Vec<u8>,
        coords: Vec<f64>,
        even_odd: bool,
    },
    PopClip,
    PushGroup {
        opacity: f64,
        blend: u8,
    },
    PopGroup,
    Path {
        kinds: Vec<u8>,
        coords: Vec<f64>,
        fill: Option<CellPaint>,
        even_odd: bool,
        stroke: Option<CellStroke>,
        blend: u8,
    },
    RasterImage {
        image: CapturedRaster,
        /// Maps unit square `[0, 1]^2` (row 0 at y = 0) into pattern space.
        matrix: Coefficients,
        blend: u8,
    },
    StencilImage {
        stencil: CapturedStencil,
        fill: CellPaint,
        /// Maps unit square `[0, 1]^2` (row 0 at y = 0) into pattern space.
        matrix: Coefficients,
        blend: u8,
    },
}

/// Owned representation of a PDF tiling pattern cell captured via
/// `TilingPattern::interpret` in pattern space.
#[derive(Clone, Debug)]
pub struct CapturedTilingCell {
    pub bbox: BBox,
    pub x_step: f64,
    pub y_step: f64,
    pub ops: Vec<TilingCellOp>,
    /// Non-`None` when the cell contained a construct that `Canvas.draw`
    /// cannot faithfully reproduce (e.g. a soft mask, non-axial/radial
    /// shading, or excessive nesting); `Page.content()` still succeeds and
    /// `Canvas.draw` refuses atomically with this reason.
    pub refusal: Option<&'static str>,
}

/// One painted item: a text run or an image in the tuple form that crosses
/// into Python, or a path as its row in [`PageContent::paths`].
#[derive(Clone, Debug)]
pub enum ContentItem {
    Text(Box<TextRunTuple>),
    Path(u32),
    Image(Box<ImageTuple>),
}

/// A page's content: items and the tables they index. The paths and the
/// contexts are shared, so that what reads them without a copy can outlive
/// the content.
#[derive(Clone, Debug)]
pub struct PageContent {
    pub items: Vec<ContentItem>,
    pub paths: Arc<PathTable>,
    pub fonts: Vec<FontTuple>,
    pub contexts: Arc<Contexts>,
}

/// The graphics contexts of a page's items, with the clips, transparency
/// groups, and marked content that they index.
#[derive(Debug)]
pub struct Contexts {
    pub clips: Vec<ClipTuple>,
    pub groups: Vec<GroupTuple>,
    pub marks: Vec<MarkTuple>,
    pub states: Vec<StateTuple>,
}

/// Appends `value` to a table of the content model, fallibly.
#[expect(
    clippy::disallowed_methods,
    reason = "every caller grows the content model in step with the elements that the collector \
              charges to content_element_count"
)]
fn push<T>(values: &mut Vec<T>, value: T, label: &str) -> Result<(), Error> {
    if values.len() == values.capacity() {
        values
            .try_reserve(1)
            .map_err(|error| Error::Allocation(format!("failed to grow {label}: {error}")))?;
    }
    values.push(value);
    Ok(())
}

/// Makes room for `capacity` values in all, without spare room.
#[expect(
    clippy::disallowed_methods,
    reason = "every caller sizes the content model by what hayro paints, whose elements count \
              against content_element_count"
)]
fn reserve_exact<T>(values: &mut Vec<T>, capacity: usize, label: &str) -> Result<(), Error> {
    values
        .try_reserve_exact(capacity.saturating_sub(values.len()))
        .map_err(|error| Error::Allocation(format!("failed to allocate {label}: {error}")))
}

pub(super) fn with_capacity<T>(capacity: usize, label: &str) -> Result<Vec<T>, Error> {
    let mut values = Vec::new();
    reserve_exact(&mut values, capacity, label)?;
    Ok(values)
}

fn copy_values(values: &[f64], label: &str) -> Result<Vec<f64>, Error> {
    let mut copy = with_capacity(values.len(), label)?;
    #[expect(
        clippy::disallowed_methods,
        reason = "fills the capacity reserved above"
    )]
    copy.extend_from_slice(values);
    Ok(copy)
}

fn bbox_tuple(rect: Rect) -> BBox {
    (rect.x0, rect.y0, rect.x1, rect.y1)
}

fn rect_is_finite(rect: Rect) -> bool {
    rect.x0.is_finite() && rect.y0.is_finite() && rect.x1.is_finite() && rect.y1.is_finite()
}

fn affine_is_finite(transform: Affine) -> bool {
    transform.as_coeffs().iter().all(|value| value.is_finite())
}

/// The bounding box of a rectangle's four corners under `transform`.
fn transformed_rect(rect: Rect, transform: Affine) -> Rect {
    let corners = [
        transform * Point::new(rect.x0, rect.y0),
        transform * Point::new(rect.x1, rect.y0),
        transform * Point::new(rect.x0, rect.y1),
        transform * Point::new(rect.x1, rect.y1),
    ];
    let mut bbox = Rect::from_points(corners[0], corners[1]);
    bbox = bbox.union_pt(corners[2]);
    bbox.union_pt(corners[3])
}

/// Whether `transform` keeps rectangles axis-aligned, so that mapping a
/// box's corners gives the exact box of the mapped shape.
fn is_axis_aligned(transform: Affine) -> bool {
    let [a, b, c, d, _, _] = transform.as_coeffs();
    (b == 0.0 && c == 0.0) || (a == 0.0 && d == 0.0)
}

/// The exact bounding box of a path under `transform`, or `None` for a path
/// without segments or a non-finite transform/result.
fn path_bbox(path: &BezPath, transform: Affine) -> Option<Rect> {
    if !affine_is_finite(transform) {
        return None;
    }
    path.segments()
        .map(|segment| (transform * segment).bounding_box())
        .reduce(|bbox, segment| bbox.union(segment))
        .filter(|bbox| rect_is_finite(*bbox))
}

/// The largest scale that `transform` applies, used for stroke widths and
/// dashes in display space.
fn transform_scale(transform: Affine) -> f64 {
    let [a, b, c, d, _, _] = transform.as_coeffs();
    a.hypot(b).max(c.hypot(d))
}

/// Path segments in display space, as PDF operators and their coordinates.
#[derive(Default)]
struct Segments {
    kinds: Vec<u8>,
    coords: Vec<f64>,
}

impl Segments {
    fn equals(&self, other: &Self) -> bool {
        self.kinds == other.kinds && self.coords == other.coords
    }

    fn count(&self) -> usize {
        self.kinds.len()
    }

    /// Replace the segments with a path's elements mapped through `transform`.
    /// Quadratic curves become the cubic curves that draw them exactly.
    /// Returns whether every coordinate is finite.
    fn read(&mut self, path: &BezPath, transform: Affine) -> Result<bool, Error> {
        let elements = path.elements();
        let coordinate_count = elements
            .iter()
            .map(|element| match element {
                PathEl::MoveTo(_) | PathEl::LineTo(_) => 2,
                PathEl::QuadTo(..) | PathEl::CurveTo(..) => 6,
                PathEl::ClosePath => 0,
            })
            .sum();
        let Self { kinds, coords } = self;
        kinds.clear();
        coords.clear();
        reserve_exact(kinds, elements.len(), "path segment kinds")?;
        reserve_exact(coords, coordinate_count, "path coordinates")?;
        let mut current = Point::ZERO;
        let mut start = Point::ZERO;
        #[expect(
            clippy::disallowed_methods,
            reason = "fills the capacity reserved above: one kind per element and its coordinates"
        )]
        for element in elements {
            match *element {
                PathEl::MoveTo(point) => {
                    let point = transform * point;
                    kinds.push(MOVE);
                    coords.extend_from_slice(&[point.x, point.y]);
                    current = point;
                    start = point;
                }
                PathEl::LineTo(point) => {
                    let point = transform * point;
                    kinds.push(LINE);
                    coords.extend_from_slice(&[point.x, point.y]);
                    current = point;
                }
                PathEl::QuadTo(control, point) => {
                    let control = transform * control;
                    let point = transform * point;
                    let first = current + (control - current) * (2.0 / 3.0);
                    let second = point + (control - point) * (2.0 / 3.0);
                    kinds.push(CURVE);
                    coords.extend_from_slice(&[
                        first.x, first.y, second.x, second.y, point.x, point.y,
                    ]);
                    current = point;
                }
                PathEl::CurveTo(first, second, point) => {
                    let (first, second, point) =
                        (transform * first, transform * second, transform * point);
                    kinds.push(CURVE);
                    coords.extend_from_slice(&[
                        first.x, first.y, second.x, second.y, point.x, point.y,
                    ]);
                    current = point;
                }
                PathEl::ClosePath => {
                    kinds.push(CLOSE);
                    current = start;
                }
            }
        }
        Ok(coords.iter().all(|value| value.is_finite()))
    }
}

/// A path's elements as [`Segments::read`] maps them, or `None` when a
/// coordinate is not finite.
fn segments(path: &BezPath, transform: Affine) -> Result<Option<Segments>, Error> {
    let mut segments = Segments::default();
    Ok(segments.read(path, transform)?.then_some(segments))
}

/// Allocates a `String` copy of `text` charged to `LimitCode::MemorySize`.
fn fallible_string(text: &str, label: &str, budget: &Budget) -> Result<String, Error> {
    if text.is_empty() {
        return Ok(String::new());
    }
    budget.charge(LimitCode::MemorySize, text.len() as u64)?;
    let mut out = String::new();
    #[expect(
        clippy::disallowed_methods,
        reason = "charged to LimitCode::MemorySize above and reserved fallibly"
    )]
    out.try_reserve_exact(text.len())
        .map_err(|error| Error::Allocation(format!("failed to allocate {label}: {error}")))?;
    #[expect(
        clippy::disallowed_methods,
        reason = "fills the exact capacity reserved above"
    )]
    out.push_str(text);
    Ok(out)
}

/// Identifies the PDF color space family of `hayro_interpret::ColorSpace` in
/// O(1) time and zero heap allocations by capturing the outer variant prefix of
/// its `#[derive(Debug)]` representation before any inner payload is formatted.
fn color_space_code(space: &HayroColorSpace) -> u8 {
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
        Some("DeviceGray") => COLOR_SPACE_DEVICE_GRAY,
        Some("DeviceRgb") => COLOR_SPACE_DEVICE_RGB,
        Some("DeviceCmyk") => COLOR_SPACE_DEVICE_CMYK,
        Some("ICCBased") => COLOR_SPACE_ICC_BASED,
        Some("CalGray") => COLOR_SPACE_CAL_GRAY,
        Some("CalRgb") => COLOR_SPACE_CAL_RGB,
        Some("Lab") => COLOR_SPACE_LAB,
        Some("Indexed") => COLOR_SPACE_INDEXED,
        Some("Separation") => COLOR_SPACE_SEPARATION,
        Some("DeviceN") => COLOR_SPACE_DEVICE_N,
        Some("Pattern") => COLOR_SPACE_PATTERN,
        _ => COLOR_SPACE_NONE,
    }
}

/// Converts a color's stored PDF components to `Vec<f64>` under `budget`.
fn copy_color_components(components: &[f32], budget: &Budget) -> Result<Vec<f64>, Error> {
    let bytes = components.len().saturating_mul(std::mem::size_of::<f64>());
    budget.charge(LimitCode::MemorySize, bytes as u64)?;
    let mut copy = with_capacity(components.len(), "color components")?;
    #[expect(
        clippy::disallowed_methods,
        reason = "fills the exact capacity reserved above"
    )]
    copy.extend(components.iter().map(|&value| f64::from(value)));
    Ok(copy)
}

fn paint_tuple(paint: &Paint<'_>, budget: &Budget) -> Result<PaintTuple, Error> {
    match paint {
        Paint::Color(color) => {
            let [red, green, blue, alpha] = color.to_rgba().components();
            let values = [red, green, blue, alpha].map(f64::from);
            let space = color_space_code(color.color_space());
            let components = copy_color_components(color.components(), budget)?;
            Ok((
                COLOR,
                copy_values(&values, "paint values")?,
                Some((space, components)),
            ))
        }
        Paint::Pattern(pattern) => match pattern.as_ref() {
            Pattern::Shading(shading) => {
                let shading_type = match shading.shading.shading_type.as_ref() {
                    ShadingType::FunctionBased { .. } => 1.0,
                    ShadingType::RadialAxial { axial: true, .. } => 2.0,
                    ShadingType::RadialAxial { .. } => 3.0,
                    ShadingType::TriangleMesh { .. } => 4.0,
                    ShadingType::CoonsPatchMesh { .. } => 6.0,
                    ShadingType::TensorProductPatchMesh { .. } => 7.0,
                    ShadingType::Dummy => 0.0,
                };
                let [a, b, c, d, e, f] = shading.matrix.as_coeffs();
                let values = [shading_type, a, b, c, d, e, f, f64::from(shading.opacity)];
                Ok((1, copy_values(&values, "paint values")?, None))
            }
            Pattern::Tiling(tiling) => {
                let [a, b, c, d, e, f] = tiling.matrix.as_coeffs();
                let bbox = tiling.bbox;
                let values = [
                    bbox.x0,
                    bbox.y0,
                    bbox.x1,
                    bbox.y1,
                    f64::from(tiling.x_step),
                    f64::from(tiling.y_step),
                    a,
                    b,
                    c,
                    d,
                    e,
                    f,
                    f64::from(tiling.opacity),
                ];
                Ok((2, copy_values(&values, "paint values")?, None))
            }
        },
    }
}

/// Whether every value of a paint is finite, so that Python can hold its
/// matrix, box, and color components.
fn paint_is_finite(paint: &PaintTuple) -> bool {
    paint.1.iter().all(|value| value.is_finite())
        && paint
            .2
            .as_ref()
            .is_none_or(|(_, components)| components.iter().all(|value| value.is_finite()))
}

fn blend_code(mode: BlendMode) -> u8 {
    match mode {
        BlendMode::Normal => 0,
        BlendMode::Multiply => 1,
        BlendMode::Screen => 2,
        BlendMode::Overlay => 3,
        BlendMode::Darken => 4,
        BlendMode::Lighten => 5,
        BlendMode::ColorDodge => 6,
        BlendMode::ColorBurn => 7,
        BlendMode::HardLight => 8,
        BlendMode::SoftLight => 9,
        BlendMode::Difference => 10,
        BlendMode::Exclusion => 11,
        BlendMode::Hue => 12,
        BlendMode::Saturation => 13,
        BlendMode::Color => 14,
        BlendMode::Luminosity => 15,
    }
}

/// The type of a soft mask: 0 for alpha, 1 for luminosity.
fn soft_mask_code(mask: Option<&SoftMask<'_>>) -> Option<u8> {
    mask.map(|mask| match mask.mask_type() {
        MaskType::Alpha => 0,
        MaskType::Luminosity => 1,
    })
}

/// A stroke's style in display space.
struct StrokeStyle {
    width: f64,
    cap: u8,
    join: u8,
    miter_limit: f64,
    dashes: Vec<f64>,
    dash_offset: f64,
}

impl StrokeStyle {
    fn new(stroke: &StrokeProps, transform: Affine) -> Result<Self, Error> {
        let scale = transform_scale(transform);
        let mut dashes = with_capacity(stroke.dash_array.len(), "dash lengths")?;
        #[expect(
            clippy::disallowed_methods,
            reason = "fills the capacity reserved above"
        )]
        dashes.extend(
            stroke
                .dash_array
                .iter()
                .map(|value| f64::from(*value) * scale),
        );
        Ok(Self {
            width: f64::from(stroke.line_width) * scale,
            cap: match stroke.line_cap {
                Cap::Butt => 0,
                Cap::Round => 1,
                Cap::Square => 2,
            },
            join: match stroke.line_join {
                Join::Miter => 0,
                Join::Round => 1,
                Join::Bevel => 2,
            },
            miter_limit: f64::from(stroke.miter_limit),
            dashes,
            dash_offset: f64::from(stroke.dash_offset) * scale,
        })
    }

    /// The number of dash lengths, which count against the element budget.
    fn dash_count(&self) -> usize {
        self.dashes.len()
    }

    fn is_finite(&self) -> bool {
        self.width.is_finite()
            && self.miter_limit.is_finite()
            && self.dash_offset.is_finite()
            && self.dashes.iter().all(|value| value.is_finite())
    }
}

/// A text run's stroke: its paint and style.
struct StrokeRecord {
    paint: PaintTuple,
    style: StrokeStyle,
}

impl StrokeRecord {
    fn new(
        paint: &Paint<'_>,
        stroke: &StrokeProps,
        transform: Affine,
        budget: &Budget,
    ) -> Result<Self, Error> {
        Ok(Self {
            paint: paint_tuple(paint, budget)?,
            style: StrokeStyle::new(stroke, transform)?,
        })
    }

    fn dash_count(&self) -> usize {
        self.style.dash_count()
    }

    fn is_finite(&self) -> bool {
        paint_is_finite(&self.paint) && self.style.is_finite()
    }

    fn into_tuple(self) -> StrokeTuple {
        let style = self.style;
        (
            self.paint,
            style.width,
            style.cap,
            style.join,
            style.miter_limit,
            style.dashes,
            style.dash_offset,
        )
    }
}

/// The name of the path columns in an allocation error.
const PATH_COLUMNS: &str = "the path columns";

/// A count as an Arrow `list` offset.
fn offset(count: usize) -> Result<i32, Error> {
    i32::try_from(count)
        .map_err(|_| Error::Allocation("the paths have too many segments".to_owned()))
}

/// An Arrow bitmap of `rows` bits, least significant bit first, set where
/// `set` holds.
fn bitmap(rows: usize, set: impl Fn(usize) -> bool) -> Result<Vec<u8>, Error> {
    let mut bits = with_capacity(rows.div_ceil(8), "a path bitmap")?;
    #[expect(
        clippy::disallowed_methods,
        reason = "fills the capacity reserved above"
    )]
    bits.resize(rows.div_ceil(8), 0u8);
    for row in (0..rows).filter(|&row| set(row)) {
        bits[row / 8] |= 1 << (row % 8);
    }
    Ok(bits)
}

/// Keep the values of `column` where `keep` holds.
fn retain_rows<T>(column: &mut Vec<T>, keep: &[bool]) {
    let mut kept = keep.iter().copied();
    column.retain(|_| kept.next().unwrap_or(false));
}

/// Keep the rows of `offsets`, an Arrow `list` column, where `keep` holds, and
/// return which values of the column those rows index.
fn retain_offsets(
    offsets: &mut Vec<i32>,
    keep: &[bool],
    values: usize,
) -> Result<Vec<bool>, Error> {
    let mut kept_values = with_capacity(values, PATH_COLUMNS)?;
    let (mut start, mut total, mut rows) = (0, 0, 1);
    for (row, &kept) in keep.iter().enumerate() {
        let end = offsets[row + 1];
        let count = (end - start) as usize;
        #[expect(
            clippy::disallowed_methods,
            reason = "fills the capacity reserved above: the rows' counts sum to `values`"
        )]
        kept_values.extend(std::iter::repeat_n(kept, count));
        if kept {
            total += end - start;
            offsets[rows] = total;
            rows += 1;
        }
        start = end;
    }
    offsets.truncate(rows);
    Ok(kept_values)
}

/// A paint in the form of the path columns: a color's sRGB components, alpha,
/// and interned stored-color index, or a pattern's index in [`PathTable::patterns`].
#[derive(Clone, Copy)]
enum PathPaint {
    Color([f32; 4], i32),
    Pattern(i32),
}

/// One paint of every path, its fill or its stroke.
#[derive(Debug, Default)]
pub struct PaintColumns {
    /// Whether the path has the paint, 0 or 1.
    pub present: Vec<u8>,
    /// A color's sRGB components and alpha, NaN for a pattern or no paint.
    pub red: Vec<f32>,
    pub green: Vec<f32>,
    pub blue: Vec<f32>,
    pub alpha: Vec<f32>,
    /// A color's index in [`PathTable::stored_colors`], -1 for a pattern or no
    /// paint.
    pub stored_color: Vec<i32>,
    /// A pattern's index in [`PathTable::patterns`], -1 for a color or no
    /// paint.
    pub pattern: Vec<i32>,
    /// Arrow bitmaps: whether a row has the paint, whether it is a color, and
    /// whether it is a pattern.
    pub present_bits: Vec<u8>,
    pub color_bits: Vec<u8>,
    pub pattern_bits: Vec<u8>,
}

impl PaintColumns {
    /// Append a row without the paint.
    fn push_none(&mut self) -> Result<(), Error> {
        push(&mut self.present, 0, PATH_COLUMNS)?;
        push(&mut self.red, f32::NAN, PATH_COLUMNS)?;
        push(&mut self.green, f32::NAN, PATH_COLUMNS)?;
        push(&mut self.blue, f32::NAN, PATH_COLUMNS)?;
        push(&mut self.alpha, f32::NAN, PATH_COLUMNS)?;
        push(&mut self.stored_color, -1, PATH_COLUMNS)?;
        push(&mut self.pattern, -1, PATH_COLUMNS)
    }

    /// Give `row`, which has no paint, `paint`.
    fn set(&mut self, row: usize, paint: PathPaint) {
        self.present[row] = 1;
        match paint {
            PathPaint::Color([red, green, blue, alpha], stored) => {
                self.red[row] = red;
                self.green[row] = green;
                self.blue[row] = blue;
                self.alpha[row] = alpha;
                self.stored_color[row] = stored;
            }
            PathPaint::Pattern(pattern) => self.pattern[row] = pattern,
        }
    }

    /// The paint of `row` in the tuple form of the content model.
    fn tuple(
        &self,
        row: usize,
        patterns: &[PaintTuple],
        stored_colors: &[StoredColorTuple],
    ) -> Option<PaintTuple> {
        if self.present[row] == 0 {
            return None;
        }
        Some(match usize::try_from(self.pattern[row]) {
            Ok(pattern) => patterns[pattern].clone(),
            #[expect(clippy::disallowed_methods, reason = "a color's four components")]
            Err(_) => {
                let stored = usize::try_from(self.stored_color[row])
                    .ok()
                    .map(|index| stored_colors[index].clone());
                (
                    COLOR,
                    [
                        self.red[row],
                        self.green[row],
                        self.blue[row],
                        self.alpha[row],
                    ]
                    .map(f64::from)
                    .to_vec(),
                    stored,
                )
            }
        })
    }

    /// Keep the rows where `keep` holds.
    fn retain(&mut self, keep: &[bool]) {
        retain_rows(&mut self.present, keep);
        retain_rows(&mut self.red, keep);
        retain_rows(&mut self.green, keep);
        retain_rows(&mut self.blue, keep);
        retain_rows(&mut self.alpha, keep);
        retain_rows(&mut self.stored_color, keep);
        retain_rows(&mut self.pattern, keep);
    }

    /// Build the bitmaps and release the spare capacity of the columns.
    fn seal(&mut self) -> Result<(), Error> {
        let rows = self.present.len();
        let (present, pattern) = (&self.present, &self.pattern);
        self.present_bits = bitmap(rows, |row| present[row] != 0)?;
        self.color_bits = bitmap(rows, |row| present[row] != 0 && pattern[row] < 0)?;
        self.pattern_bits = bitmap(rows, |row| pattern[row] >= 0)?;
        self.present.shrink_to_fit();
        self.red.shrink_to_fit();
        self.green.shrink_to_fit();
        self.blue.shrink_to_fit();
        self.alpha.shrink_to_fit();
        self.stored_color.shrink_to_fit();
        self.pattern.shrink_to_fit();
        Ok(())
    }
}

/// The paths of a page's content, one row per path in paint order, each field
/// in its own column, with the segments of every path and the dash lengths of
/// every stroke in columns of their own, which offsets index.
#[derive(Debug, Default)]
pub struct PathTable {
    pub x0: Vec<f64>,
    pub y0: Vec<f64>,
    pub x1: Vec<f64>,
    pub y1: Vec<f64>,
    /// Arrow `list` offsets into the segment columns, one more than the rows.
    pub segment_offsets: Vec<i32>,
    pub fill: PaintColumns,
    /// 1 for the even-odd rule, 0 for the nonzero rule.
    pub fill_rule: Vec<u8>,
    pub stroke: PaintColumns,
    /// The stroke's style, NaN or 0 without a stroke.
    pub stroke_width: Vec<f64>,
    pub stroke_cap: Vec<u8>,
    pub stroke_join: Vec<u8>,
    pub stroke_miter_limit: Vec<f64>,
    /// Arrow `list` offsets into `stroke_dashes`, one more than the rows.
    pub stroke_dashes_offsets: Vec<i32>,
    pub stroke_dash_offset: Vec<f64>,
    /// The index of the path's graphics context in [`Contexts::states`].
    pub context: Vec<u32>,
    pub blend_mode: Vec<u8>,
    /// The type of the path's soft mask, 0 for alpha and 1 for luminosity, or
    /// -1.
    pub soft_mask: Vec<i8>,
    /// Arrow bitmap: whether a row has a soft mask.
    pub soft_mask_bits: Vec<u8>,
    /// Each segment's path, verb, end point, and a curve's control points,
    /// NaN for the other verbs; a closing segment ends where its subpath
    /// starts.
    pub path: Vec<u32>,
    pub verb: Vec<u8>,
    pub to_x: Vec<f64>,
    pub to_y: Vec<f64>,
    pub c1_x: Vec<f64>,
    pub c1_y: Vec<f64>,
    pub c2_x: Vec<f64>,
    pub c2_y: Vec<f64>,
    /// Arrow bitmap: whether a segment is a curve, and how many are.
    pub curve_bits: Vec<u8>,
    pub curves: usize,
    /// The dash lengths of every stroke.
    pub stroke_dashes: Vec<f64>,
    /// The distinct pattern paints that the pattern columns index, in the
    /// order in which the paths first use them.
    pub patterns: Vec<PaintTuple>,
    /// The distinct stored PDF color spaces and original components that the
    /// `stored_color` paint columns index.
    pub stored_colors: Vec<StoredColorTuple>,
    /// Owned hayro paints, shared by the rows, without evaluating their
    /// functions while extracting content. Non-shading patterns have no source.
    shadings: Vec<Option<ShadingPattern>>,
    /// Visible display size of the page `(width, height)` in points.
    pub page_size: (f64, f64),
    /// Owned tiling pattern cells captured in pattern space, parallel to
    /// `patterns`. Non-tiling patterns hold `None`.
    tilings: Vec<Option<CapturedTilingCell>>,
    /// Owned decoded payloads for inline raster images and inline stencil masks
    /// (`xref == 0`), indexed by `ImageTuple`'s trailing inline index.
    inline_images: Vec<InlineImageData>,
}

/// Whether the gradient is axial, its coordinates and extension flags, and
/// sampled stops (offset, red, green, blue, alpha) in its own coordinate space.
pub type SampledShadingTuple = (
    bool,
    [f64; 6],
    [bool; 2],
    Vec<[f64; 5]>,
    Option<BBox>,
    Option<[f64; 4]>,
);

/// Use the renderer's quarter/midpoint probes, RGB tolerance, and depth so
/// quantized colour spaces keep the approximation that the original shows.
const SHADING_DEPTH: u8 = 10;
const SHADING_TOLERANCE: f32 = 0.01;

/// Appends one interval's endpoint, subdividing at most SHADING_DEPTH times.
fn shading_interval(
    sample: &impl Fn(f32) -> Result<[f32; 4], Error>,
    stops: &mut BudgetVec<'_, [f64; 5]>,
    start: (f32, [f32; 4]),
    end: (f32, [f32; 4]),
    depth: u8,
) -> Result<(), Error> {
    let middle = (start.0 + end.0) * 0.5;
    let mid_color = sample(middle)?;
    let mut accurate = true;
    for (color, mix) in [
        (sample((start.0 + middle) * 0.5)?, 0.25),
        (mid_color, 0.5),
        (sample((middle + end.0) * 0.5)?, 0.75),
    ] {
        accurate &= color.iter().enumerate().all(|(channel, &value)| {
            (value - (start.1[channel] * (1.0 - mix) + end.1[channel] * mix)).abs()
                <= SHADING_TOLERANCE
        });
    }
    if accurate || depth == SHADING_DEPTH {
        let [r, g, b, a] = end.1.map(f64::from);
        stops.try_push([f64::from(end.0), r, g, b, a])?;
    } else {
        shading_interval(sample, stops, start, (middle, mid_color), depth + 1)?;
        shading_interval(sample, stops, (middle, mid_color), end, depth + 1)?;
    }
    Ok(())
}

/// Samples one owned axial/radial [`ShadingPattern`] under `budget`.
pub fn sample_shading_pattern(
    paint: &ShadingPattern,
    budget: &Budget,
) -> Result<Option<SampledShadingTuple>, Error> {
    let ShadingType::RadialAxial {
        coords,
        domain,
        function,
        extend,
        axial,
    } = paint.shading.shading_type.as_ref()
    else {
        return Ok(None);
    };
    // hayro's function reader bounds inputs and outputs to 32 components.
    if function.num_outputs() > 32 {
        return Ok(None);
    }
    let sample = |offset: f32| {
        budget.charge(LimitCode::InterpretationFuel, 1)?;
        let input = domain[0] + (domain[1] - domain[0]) * offset;
        let mut output = [0.0; 32];
        let output = &mut output[..function.num_outputs()];
        if function.eval(&[input], output).is_none() {
            return Ok([0.0; 4]);
        }
        let rgba = paint.shading.color_space.to_rgba(output, paint.opacity);
        let rgba = paint
            .transfer_function
            .as_ref()
            .map_or(rgba, |tf| tf.apply(&rgba));
        Ok(rgba.components())
    };
    let mut stops = BudgetVec::counting(budget, LimitCode::ContentElementCount);
    let start = (0.0, sample(0.0)?);
    let [r, g, b, a] = start.1.map(f64::from);
    stops.try_push([0.0, r, g, b, a])?;
    shading_interval(&sample, &mut stops, start, (1.0, sample(1.0)?), 0)?;
    // hayro has already transformed the clip into display space. Restore
    // the shading's box before writing it under the pattern matrix again.
    let bounds = paint
        .shading
        .clip_path
        .as_ref()
        .and_then(|path| {
            let det = paint.matrix.determinant();
            if !det.is_finite() || det == 0.0 {
                return None;
            }
            let inverse = paint.matrix.inverse();
            if !affine_is_finite(inverse) {
                return None;
            }
            path_bbox(path, inverse)
        })
        .map(bbox_tuple);
    let background = paint.shading.background.as_ref().map(|components| {
        let rgba = paint.shading.color_space.to_rgba(components, paint.opacity);
        paint
            .transfer_function
            .as_ref()
            .map_or(rgba, |tf| tf.apply(&rgba))
            .components()
            .map(f64::from)
    });
    Ok(Some((
        *axial,
        coords.map(f64::from),
        *extend,
        stops.into_vec(),
        bounds,
        background,
    )))
}

impl PathTable {
    /// Samples one axial/radial paint on demand, under the drawing operation's
    /// budget. The owned hayro functions and colour space outlive the source
    /// document and keep the version that extraction read.
    pub fn sample_shading(
        &self,
        pattern: usize,
        budget: &Budget,
    ) -> Result<Option<SampledShadingTuple>, Error> {
        let Some(Some(paint)) = self.shadings.get(pattern) else {
            return Ok(None);
        };
        sample_shading_pattern(paint, budget)
    }

    /// Returns the captured tiling pattern cell for `pattern`, if any.
    pub fn tiling_cell(&self, pattern: usize) -> Option<&CapturedTilingCell> {
        self.tilings.get(pattern).and_then(Option::as_ref)
    }

    /// Returns the captured inline raster or stencil payload at `index`.
    pub fn inline_image(&self, index: usize) -> Option<&InlineImageData> {
        self.inline_images.get(index)
    }

    fn new(page_size: (f64, f64)) -> Self {
        Self {
            segment_offsets: vec![0],
            stroke_dashes_offsets: vec![0],
            page_size,
            ..Self::default()
        }
    }

    /// The number of paths.
    pub fn rows(&self) -> usize {
        self.x0.len()
    }

    /// The segments of `row`, as a range of the segment columns.
    pub fn segments_of(&self, row: usize) -> Range<usize> {
        self.segment_offsets[row] as usize..self.segment_offsets[row + 1] as usize
    }

    /// The dash lengths of `row`, as a range of [`PathTable::stroke_dashes`].
    pub fn dashes_of(&self, row: usize) -> Range<usize> {
        self.stroke_dashes_offsets[row] as usize..self.stroke_dashes_offsets[row + 1] as usize
    }

    /// The coordinates of `row` in the form of the content model: the points
    /// that its verbs consume.
    #[expect(
        clippy::disallowed_methods,
        reason = "six coordinates per segment of one row at most, which the collector charged"
    )]
    pub fn coordinates(&self, row: usize) -> Vec<f64> {
        let mut coords = Vec::new();
        for segment in self.segments_of(row) {
            let to = [self.to_x[segment], self.to_y[segment]];
            match self.verb[segment] {
                MOVE | LINE => coords.extend(to),
                CURVE => {
                    coords.extend([
                        self.c1_x[segment],
                        self.c1_y[segment],
                        self.c2_x[segment],
                        self.c2_y[segment],
                    ]);
                    coords.extend(to);
                }
                _ => {}
            }
        }
        coords
    }

    /// `row` in the tuple form that crosses into Python.
    #[expect(
        clippy::disallowed_methods,
        reason = "copies the verbs and dash lengths of one row, which the collector charged"
    )]
    pub fn tuple(&self, row: usize) -> PathTuple {
        (
            self.verb[self.segments_of(row)].to_vec(),
            self.coordinates(row),
            (self.x0[row], self.y0[row], self.x1[row], self.y1[row]),
            self.fill.tuple(row, &self.patterns, &self.stored_colors),
            self.fill_rule[row] != 0,
            self.stroke
                .tuple(row, &self.patterns, &self.stored_colors)
                .map(|paint| {
                    (
                        paint,
                        self.stroke_width[row],
                        self.stroke_cap[row],
                        self.stroke_join[row],
                        self.stroke_miter_limit[row],
                        self.stroke_dashes[self.dashes_of(row)].to_vec(),
                        self.stroke_dash_offset[row],
                    )
                }),
            (
                self.context[row],
                self.blend_mode[row],
                u8::try_from(self.soft_mask[row]).ok(),
            ),
        )
    }

    fn bbox(&self, row: usize) -> Rect {
        Rect::new(self.x0[row], self.y0[row], self.x1[row], self.y1[row])
    }

    /// Append a path with `fill` and `stroke`, each painted with `paint`, and
    /// return its row.
    fn push(
        &mut self,
        segments: &Segments,
        bbox: Rect,
        paint: PathPaint,
        fill: Option<FillRule>,
        stroke: Option<&StrokeStyle>,
        context: ItemContext,
    ) -> Result<u32, Error> {
        let row = self.rows();
        let index = index_of(row)?;
        push(&mut self.x0, bbox.x0, PATH_COLUMNS)?;
        push(&mut self.y0, bbox.y0, PATH_COLUMNS)?;
        push(&mut self.x1, bbox.x1, PATH_COLUMNS)?;
        push(&mut self.y1, bbox.y1, PATH_COLUMNS)?;
        self.push_segments(index, segments)?;
        push(
            &mut self.segment_offsets,
            offset(self.verb.len())?,
            PATH_COLUMNS,
        )?;
        self.fill.push_none()?;
        if fill.is_some() {
            self.fill.set(row, paint);
        }
        let even_odd = fill == Some(FillRule::EvenOdd);
        push(&mut self.fill_rule, u8::from(even_odd), PATH_COLUMNS)?;
        self.stroke.push_none()?;
        push(&mut self.stroke_width, f64::NAN, PATH_COLUMNS)?;
        push(&mut self.stroke_cap, 0, PATH_COLUMNS)?;
        push(&mut self.stroke_join, 0, PATH_COLUMNS)?;
        push(&mut self.stroke_miter_limit, f64::NAN, PATH_COLUMNS)?;
        push(
            &mut self.stroke_dashes_offsets,
            offset(self.stroke_dashes.len())?,
            PATH_COLUMNS,
        )?;
        push(&mut self.stroke_dash_offset, f64::NAN, PATH_COLUMNS)?;
        push(&mut self.context, context.state, PATH_COLUMNS)?;
        push(&mut self.blend_mode, context.blend, PATH_COLUMNS)?;
        // The types are the positions in Python's `SoftMaskType`.
        let soft_mask = context.soft_mask.map_or(-1, |mask| mask as i8);
        push(&mut self.soft_mask, soft_mask, PATH_COLUMNS)?;
        if let Some(stroke) = stroke {
            self.set_stroke(row, paint, stroke)?;
        }
        Ok(index)
    }

    /// Append the segments of the path at row `path`.
    fn push_segments(&mut self, path: u32, segments: &Segments) -> Result<(), Error> {
        let none = [f64::NAN; 2];
        let mut points = segments.coords.as_chunks::<2>().0.iter().copied();
        let mut point = || points.next().unwrap_or(none);
        let mut start = none;
        for &verb in &segments.kinds {
            let (to, c1, c2) = match verb {
                MOVE => {
                    start = point();
                    (start, none, none)
                }
                LINE => (point(), none, none),
                CURVE => {
                    let (c1, c2) = (point(), point());
                    (point(), c1, c2)
                }
                _ => (start, none, none),
            };
            push(&mut self.path, path, PATH_COLUMNS)?;
            push(&mut self.verb, verb, PATH_COLUMNS)?;
            push(&mut self.to_x, to[0], PATH_COLUMNS)?;
            push(&mut self.to_y, to[1], PATH_COLUMNS)?;
            push(&mut self.c1_x, c1[0], PATH_COLUMNS)?;
            push(&mut self.c1_y, c1[1], PATH_COLUMNS)?;
            push(&mut self.c2_x, c2[0], PATH_COLUMNS)?;
            push(&mut self.c2_y, c2[1], PATH_COLUMNS)?;
        }
        Ok(())
    }

    /// Give `row`, the last row, a stroke.
    fn set_stroke(
        &mut self,
        row: usize,
        paint: PathPaint,
        stroke: &StrokeStyle,
    ) -> Result<(), Error> {
        self.stroke.set(row, paint);
        self.stroke_width[row] = stroke.width;
        self.stroke_cap[row] = stroke.cap;
        self.stroke_join[row] = stroke.join;
        self.stroke_miter_limit[row] = stroke.miter_limit;
        self.stroke_dash_offset[row] = stroke.dash_offset;
        for &dash in &stroke.dashes {
            push(&mut self.stroke_dashes, dash, PATH_COLUMNS)?;
        }
        self.stroke_dashes_offsets[row + 1] = offset(self.stroke_dashes.len())?;
        Ok(())
    }

    /// Keep the rows whose box meets `clip`, numbered in order, and the
    /// patterns and inline images that kept paths and images use.
    fn retain_meeting(
        &mut self,
        clip: BBox,
        image_patterns: &mut [Option<i32>],
        image_inlines: &mut [Option<u32>],
        dropped_images: bool,
    ) -> Result<(), Error> {
        let rows = self.rows();
        let mut keep = with_capacity(rows, PATH_COLUMNS)?;
        #[expect(
            clippy::disallowed_methods,
            reason = "fills the capacity reserved above"
        )]
        keep.extend((0..rows).map(|row| meets(self.bbox(row), clip)));
        if keep.iter().all(|&kept| kept) {
            if dropped_images {
                self.retain_patterns(image_patterns)?;
                self.retain_inline_images(image_inlines)?;
            }
            return Ok(());
        }
        retain_rows(&mut self.x0, &keep);
        retain_rows(&mut self.y0, &keep);
        retain_rows(&mut self.x1, &keep);
        retain_rows(&mut self.y1, &keep);
        self.fill.retain(&keep);
        retain_rows(&mut self.fill_rule, &keep);
        self.stroke.retain(&keep);
        retain_rows(&mut self.stroke_width, &keep);
        retain_rows(&mut self.stroke_cap, &keep);
        retain_rows(&mut self.stroke_join, &keep);
        retain_rows(&mut self.stroke_miter_limit, &keep);
        retain_rows(&mut self.stroke_dash_offset, &keep);
        retain_rows(&mut self.context, &keep);
        retain_rows(&mut self.blend_mode, &keep);
        retain_rows(&mut self.soft_mask, &keep);
        let segments = retain_offsets(&mut self.segment_offsets, &keep, self.verb.len())?;
        retain_rows(&mut self.path, &segments);
        retain_rows(&mut self.verb, &segments);
        retain_rows(&mut self.to_x, &segments);
        retain_rows(&mut self.to_y, &segments);
        retain_rows(&mut self.c1_x, &segments);
        retain_rows(&mut self.c1_y, &segments);
        retain_rows(&mut self.c2_x, &segments);
        retain_rows(&mut self.c2_y, &segments);
        let dashes = retain_offsets(
            &mut self.stroke_dashes_offsets,
            &keep,
            self.stroke_dashes.len(),
        )?;
        retain_rows(&mut self.stroke_dashes, &dashes);
        for row in 0..self.rows() {
            for segment in self.segments_of(row) {
                self.path[segment] = row as u32;
            }
        }
        self.retain_patterns(image_patterns)?;
        self.retain_inline_images(image_inlines)?;
        self.retain_stored_colors()
    }

    /// Keep the patterns that the kept path rows and kept image fills use, in
    /// the order in which they first use them.
    fn retain_patterns(&mut self, image_patterns: &mut [Option<i32>]) -> Result<(), Error> {
        let mut patterns = std::mem::take(&mut self.patterns);
        let mut shadings = std::mem::take(&mut self.shadings);
        let mut tilings = std::mem::take(&mut self.tilings);
        let mut indexes = with_capacity(patterns.len(), PATH_COLUMNS)?;
        #[expect(
            clippy::disallowed_methods,
            reason = "fills the capacity reserved above"
        )]
        indexes.resize(patterns.len(), -1);
        for row in 0..self.rows() {
            for column in [&mut self.fill.pattern, &mut self.stroke.pattern] {
                let Ok(pattern) = usize::try_from(column[row]) else {
                    continue;
                };
                if indexes[pattern] < 0 {
                    indexes[pattern] = offset(self.patterns.len())?;
                    let paint = std::mem::take(&mut patterns[pattern]);
                    push(&mut self.patterns, paint, PATH_COLUMNS)?;
                    push(&mut self.shadings, shadings[pattern].take(), PATH_COLUMNS)?;
                    push(&mut self.tilings, tilings[pattern].take(), PATH_COLUMNS)?;
                }
                column[row] = indexes[pattern];
            }
        }
        for slot in image_patterns.iter_mut() {
            let Some(pattern) = slot.and_then(|value| usize::try_from(value).ok()) else {
                continue;
            };
            if indexes[pattern] < 0 {
                indexes[pattern] = offset(self.patterns.len())?;
                let paint = std::mem::take(&mut patterns[pattern]);
                push(&mut self.patterns, paint, PATH_COLUMNS)?;
                push(&mut self.shadings, shadings[pattern].take(), PATH_COLUMNS)?;
                push(&mut self.tilings, tilings[pattern].take(), PATH_COLUMNS)?;
            }
            *slot = Some(indexes[pattern]);
        }
        Ok(())
    }

    /// Keep only the inline image payloads referenced by kept `Image` records.
    fn retain_inline_images(&mut self, image_inlines: &mut [Option<u32>]) -> Result<(), Error> {
        if self.inline_images.is_empty() {
            return Ok(());
        }
        let mut inline_images = std::mem::take(&mut self.inline_images);
        let mut moved = with_capacity(inline_images.len(), PATH_COLUMNS)?;
        #[expect(
            clippy::disallowed_methods,
            reason = "fills the capacity reserved above"
        )]
        moved.extend(inline_images.drain(..).map(Some));
        let mut indexes = with_capacity(moved.len(), PATH_COLUMNS)?;
        #[expect(
            clippy::disallowed_methods,
            reason = "fills the capacity reserved above"
        )]
        indexes.resize(moved.len(), None);
        for slot in image_inlines.iter_mut() {
            let Some(index) = slot.map(|value| value as usize) else {
                continue;
            };
            let new_index = match indexes[index] {
                Some(existing) => existing,
                None => {
                    let next = index_of(self.inline_images.len())?;
                    if let Some(payload) = moved[index].take() {
                        push(&mut self.inline_images, payload, PATH_COLUMNS)?;
                    }
                    indexes[index] = Some(next);
                    next
                }
            };
            *slot = Some(new_index);
        }
        Ok(())
    }

    /// Keep the stored colors that the rows use, in the order in which they
    /// first use them.
    fn retain_stored_colors(&mut self) -> Result<(), Error> {
        let mut stored_colors = std::mem::take(&mut self.stored_colors);
        let mut indexes = with_capacity(stored_colors.len(), PATH_COLUMNS)?;
        #[expect(
            clippy::disallowed_methods,
            reason = "fills the capacity reserved above"
        )]
        indexes.resize(stored_colors.len(), -1);
        for row in 0..self.rows() {
            for column in [&mut self.fill.stored_color, &mut self.stroke.stored_color] {
                let Ok(color) = usize::try_from(column[row]) else {
                    continue;
                };
                if indexes[color] < 0 {
                    indexes[color] = offset(self.stored_colors.len())?;
                    let entry = std::mem::take(&mut stored_colors[color]);
                    push(&mut self.stored_colors, entry, PATH_COLUMNS)?;
                }
                column[row] = indexes[color];
            }
        }
        Ok(())
    }

    /// Build the bitmaps and release the spare capacity of the columns, which
    /// never change again.
    fn seal(&mut self) -> Result<(), Error> {
        self.fill.seal()?;
        self.stroke.seal()?;
        let soft_mask = &self.soft_mask;
        self.soft_mask_bits = bitmap(soft_mask.len(), |row| soft_mask[row] >= 0)?;
        let verb = &self.verb;
        self.curve_bits = bitmap(verb.len(), |segment| verb[segment] == CURVE)?;
        self.curves = verb.iter().filter(|&&verb| verb == CURVE).count();
        self.x0.shrink_to_fit();
        self.y0.shrink_to_fit();
        self.x1.shrink_to_fit();
        self.y1.shrink_to_fit();
        self.segment_offsets.shrink_to_fit();
        self.fill_rule.shrink_to_fit();
        self.stroke_width.shrink_to_fit();
        self.stroke_cap.shrink_to_fit();
        self.stroke_join.shrink_to_fit();
        self.stroke_miter_limit.shrink_to_fit();
        self.stroke_dashes_offsets.shrink_to_fit();
        self.stroke_dash_offset.shrink_to_fit();
        self.context.shrink_to_fit();
        self.blend_mode.shrink_to_fit();
        self.soft_mask.shrink_to_fit();
        self.path.shrink_to_fit();
        self.verb.shrink_to_fit();
        self.to_x.shrink_to_fit();
        self.to_y.shrink_to_fit();
        self.c1_x.shrink_to_fit();
        self.c1_y.shrink_to_fit();
        self.c2_x.shrink_to_fit();
        self.c2_y.shrink_to_fit();
        self.stroke_dashes.shrink_to_fit();
        self.patterns.shrink_to_fit();
        self.stored_colors.shrink_to_fit();
        self.shadings.shrink_to_fit();
        self.tilings.shrink_to_fit();
        self.inline_images.shrink_to_fit();
        Ok(())
    }
}

/// Whether `bbox` meets the display-space rectangle `clip`.
fn meets(bbox: Rect, (x0, y0, x1, y1): BBox) -> bool {
    !(bbox.x0 > x1 || bbox.x1 < x0 || bbox.y0 > y1 || bbox.y1 < y0)
}

/// The context index, blend mode, and soft mask of one item.
#[derive(Clone, Copy, PartialEq)]
struct ItemContext {
    state: u32,
    blend: u8,
    soft_mask: Option<u8>,
}

impl ItemContext {
    fn into_tuple(self) -> ItemContextTuple {
        (self.state, self.blend, self.soft_mask)
    }
}

struct TextRunRecord {
    replay: super::redraw::Replay,
    ids: Vec<Option<u32>>,
    texts: Vec<Option<String>>,
    matrices: Vec<f64>,
    advances: Vec<Option<f64>>,
    boxes: Vec<Option<BBox>>,
    font: u32,
    render_mode: u8,
    fill: Option<PaintTuple>,
    stroke: Option<StrokeRecord>,
    bbox: Rect,
    /// The box of the glyphs' origins and advances, for a run without ink.
    extent: Rect,
    /// The place of the run's first glyph among those that hayro showed.
    first_shown: usize,
    context: ItemContext,
    writing_mode: u8,
}

impl TextRunRecord {
    /// Describes the run as the fallback faces draw it, from its first glyph
    /// on: the IDs and the ink of `faces`, and the box around that ink. The
    /// matrices and the advances stay, because a face's stretch in the glyph's
    /// transform would move them.
    fn describe_on(&mut self, faces: &[FaceGlyph]) {
        let mut ink: Option<Rect> = None;
        for ((id, bbox), face) in self.ids.iter_mut().zip(&mut self.boxes).zip(faces) {
            *id = face.id;
            *bbox = face.ink.map(bbox_tuple);
            if let Some(face) = face.ink {
                ink = Some(ink.map_or(face, |ink| ink.union(face)));
            }
        }
        self.bbox = ink.unwrap_or(self.extent);
    }
}

/// The fill paint of a stencil image mask: either a solid color tuple or an
/// index into [`PathTable::patterns`].
enum ImageFill {
    Color(PaintTuple),
    Pattern(i32),
}

struct ImageRecord {
    xref: u32,
    format: ImageFormatTuple,
    is_stencil: bool,
    mask: u8,
    encoded_size: usize,
    resolution: (u32, u32),
    matrix: Coefficients,
    bbox: Rect,
    context: ItemContext,
    fill: Option<ImageFill>,
    inline_index: Option<u32>,
}

/// An item in paint order; a path is a row of the collector's [`PathTable`].
/// The other kinds are boxed, so that a record per path costs little.
enum Record {
    Text(Box<TextRunRecord>),
    Path(u32),
    Image(Box<ImageRecord>),
}

impl Record {
    fn bbox(&self, paths: &PathTable) -> Rect {
        match self {
            Self::Text(run) => run.bbox,
            Self::Path(row) => paths.bbox(*row as usize),
            Self::Image(image) => image.bbox,
        }
    }

    /// The record as an item; a path is the row `path_row`.
    fn into_item(self, path_row: u32, patterns: &[PaintTuple]) -> ContentItem {
        match self {
            Self::Text(run) => {
                let run = *run;
                ContentItem::Text(Box::new((
                    (run.ids, run.texts, run.matrices, run.advances, run.boxes),
                    run.font,
                    run.render_mode,
                    run.fill,
                    run.stroke.map(StrokeRecord::into_tuple),
                    bbox_tuple(run.bbox),
                    run.context.into_tuple(),
                    run.replay,
                    run.writing_mode,
                )))
            }
            Self::Path(_) => ContentItem::Path(path_row),
            Self::Image(image) => {
                let image = *image;
                let fill = image.fill.and_then(|fill| match fill {
                    ImageFill::Color(tuple) => Some(tuple),
                    ImageFill::Pattern(index) => usize::try_from(index)
                        .ok()
                        .and_then(|idx| patterns.get(idx).cloned()),
                });
                ContentItem::Image(Box::new((
                    image.xref,
                    image.format,
                    image.is_stencil,
                    image.mask,
                    image.encoded_size,
                    image.resolution,
                    image.matrix,
                    bbox_tuple(image.bbox),
                    image.context.into_tuple(),
                    fill,
                    image.inline_index,
                )))
            }
        }
    }
}

/// The paint callback that a stroke may complete: hayro paints `B` and the
/// fill-and-stroke text render mode as a fill followed by a stroke of the
/// same geometry.
#[derive(Clone, Copy, PartialEq)]
enum Pending {
    Path,
    Text,
}

/// How the glyphs of one run are painted, as a sink receives it before them.
pub struct GlyphStyle {
    /// The sRGB approximation and alpha of a color paint; `None` for a
    /// pattern, or for invisible text.
    pub color: Option<[f32; 4]>,
    /// One of the `RENDER_*` modes.
    pub render_mode: u8,
    /// The intersection of the boxes of the clip paths in force, in display
    /// space, or `None` when no clip path is.
    pub clip: Option<Rect>,
    /// The run's place among the glyph runs that hayro showed on the page,
    /// counting from 0, a stroke that completes a fill included.
    pub run: u32,
    /// The key of the font the run was shown with: the cache key of its
    /// dictionary, as `Dict::cache_key` of a font object gives it, or a key
    /// of its own for hayro's fallback font.
    pub font: u128,
    /// The transform of the run's first glyph, as hayro gives it, from its
    /// 1000-unit glyph space to display space.
    pub first: Affine,
}

/// The font key of the runs that hayro shows with its fallback font, which
/// no font dictionary describes.
pub(super) const FALLBACK_FONT_KEY: u128 = 1;
/// The font key of a Type 3 run when hayro reports no font dictionary.
const FALLBACK_TYPE3_FONT_KEY: u128 = 2;

/// The [`GlyphStyle::font`] of a run. Fonts over one program have different
/// keys, although their glyphs' identifiers agree.
fn run_font_key(
    run: &GlyphRun<'_, '_>,
    recent: &mut [(usize, usize, u128); 8],
    next: &mut usize,
) -> u128 {
    let Some(dict) = run.font_dict() else {
        return FALLBACK_FONT_KEY;
    };
    let data = dict.data();
    let ptr = data.as_ptr() as usize;
    let len = data.len();
    for &(cached_ptr, cached_len, cached_key) in recent.iter() {
        if cached_ptr == ptr && cached_len == len && cached_key != 0 {
            return cached_key;
        }
    }
    let key = dict.cache_key();
    recent[*next & 7] = (ptr, len, key);
    *next = next.wrapping_add(1);
    key
}

/// What a reader built on the content model receives besides the items.
///
/// Every path paint callback reaches `path`. The glyphs of every text run
/// reach `glyph` once: hayro paints the fill-and-stroke text render mode as a
/// fill followed by a stroke of the same glyphs, and the stroke that completes
/// such a fill sends no glyphs again.
pub trait ContentSink {
    /// Receive the style of the glyphs that follow, once per text run.
    fn glyph_run(&mut self, style: &GlyphStyle);
    /// Receive one glyph with the transform from hayro's 1000-unit glyph space
    /// to display space, and its exact ink box when the sink wants it.
    fn glyph(&mut self, glyph: &Glyph<'_>, transform: Affine, ink: Option<Rect>);
    /// Whether [`ContentSink::glyph`] should receive exact ink boxes.
    fn wants_ink(&self) -> bool {
        false
    }
    /// Whether the transform of [`ContentSink::glyph`] should leave out the
    /// stretch of a substitute font's glyph, so that the glyph's advance through
    /// it is the PDF's width. The ink box keeps the stretch that a rendering
    /// draws.
    fn wants_advances(&self) -> bool {
        false
    }
    /// The stroke of the glyphs of the latest fill run, which completes that
    /// run as fill-and-stroke text; the glyphs are not sent again.
    fn stroke_completes_fill(&mut self);
    /// Receive one path paint callback.
    fn path(&mut self, path: &BezPath, transform: Affine, mode: &DrawMode);
    /// Whether the sink refused more input, so that the collector stops
    /// sending glyphs.
    fn stopped(&self) -> bool;
    /// Whether the sink decodes selected images itself, so the collector
    /// should skip capturing inline image payloads and stencil fill paints.
    fn handles_image_decode(&self) -> bool {
        false
    }
    /// Receive the image that the collector records as the page's image
    /// `ordinal`, counted from 0 in paint order, when it records images.
    fn image(&mut self, _ordinal: usize, _image: &Image<'_, '_>) -> Result<(), Error> {
        Ok(())
    }
    /// Receive the start of a marked-content sequence and its `/ActualText`.
    fn begin_marked_content(&mut self, _actual_text: Option<&[u8]>) {}
    /// Whether [`ContentSink::begin_marked_content`] should receive `/ActualText`.
    fn wants_actual_text(&self) -> bool {
        true
    }
    /// Receive the end of a marked-content sequence.
    fn end_marked_content(&mut self) {}
}

/// The sink of `Page.content()`, which reads only the items.
pub(crate) struct NoSink;

/// What a fallback face draws for one glyph that a page shows.
struct FaceGlyph {
    id: Option<u32>,
    ink: Option<Rect>,
}

/// Program-derived font metrics read from a fallback face: PostScript name,
/// weight, italic, serif, and monospace.
type FaceFontMetrics = (String, Option<u32>, bool, bool, bool);

/// The sink of an interpretation through the fallback faces, which reads the
/// glyph that each face draws for each glyph of the page, and the fonts as the
/// faces describe them, by the key of the font's dictionary.
struct FaceGlyphs<'b> {
    glyphs: BudgetVec<'b, FaceGlyph>,
    fonts: HashMap<u128, FaceFontMetrics>,
    /// The font of a run whose first glyph has yet to arrive.
    font: Option<u128>,
    error: Option<Error>,
}

impl FaceGlyphs<'_> {
    #[expect(
        clippy::disallowed_methods,
        reason = "one font per text run at most, whose first glyph `glyphs` charges to the budget"
    )]
    fn add_font(&mut self, key: u128, glyph: &OutlineGlyph) -> Result<(), Error> {
        if !self.fonts.contains_key(&key) {
            self.fonts.try_reserve(1).map_err(|error| {
                Error::Allocation(format!("failed to grow the fonts of the faces: {error}"))
            })?;
            self.fonts.insert(key, face_font_metrics(glyph));
        }
        Ok(())
    }

    fn add_glyph(&mut self, glyph: &Glyph<'_>, ink: Option<Rect>) -> Result<(), Error> {
        let font = self.font.take();
        let id = match glyph {
            Glyph::Outline(outline) => {
                if let Some(font) = font {
                    self.add_font(font, outline)?;
                }
                Some(outline.glyph_id().to_u32())
            }
            Glyph::Type3(_) => None,
        };
        let ink = ink.filter(|ink| rect_is_finite(*ink));
        Ok(self.glyphs.try_push(FaceGlyph { id, ink })?)
    }
}

impl ContentSink for FaceGlyphs<'_> {
    fn glyph_run(&mut self, style: &GlyphStyle) {
        self.font = Some(style.font);
    }

    fn glyph(&mut self, glyph: &Glyph<'_>, _: Affine, ink: Option<Rect>) {
        if self.error.is_none() {
            self.error = self.add_glyph(glyph, ink).err();
        }
    }

    fn wants_ink(&self) -> bool {
        true
    }

    fn stroke_completes_fill(&mut self) {}

    fn path(&mut self, _: &BezPath, _: Affine, _: &DrawMode) {}

    fn stopped(&self) -> bool {
        self.error.is_some()
    }
}

impl ContentSink for NoSink {
    fn glyph_run(&mut self, _: &GlyphStyle) {}
    fn glyph(&mut self, _: &Glyph<'_>, _: Affine, _: Option<Rect>) {}
    fn stroke_completes_fill(&mut self) {}
    fn path(&mut self, _: &BezPath, _: Affine, _: &DrawMode) {}
    fn stopped(&self) -> bool {
        false
    }
}

/// A glyph outline in hayro's 1000-unit glyph space, and its exact box.
struct GlyphInk {
    bbox: Rect,
    path: BezPath,
}

/// A Device that only measures where a Type 3 glyph procedure paints.
struct InkBounds {
    bbox: Option<Rect>,
}

impl InkBounds {
    fn add(&mut self, rect: Rect) {
        if rect_is_finite(rect) {
            self.bbox = Some(self.bbox.map_or(rect, |bbox| bbox.union(rect)));
        }
    }
}

impl<'a> Device<'a> for InkBounds {
    fn draw_path(&mut self, path: &BezPath, props: DrawProps<'a>, mode: &DrawMode) {
        let Some(bbox) = path_bbox(path, props.transform) else {
            return;
        };
        let half_width = match mode {
            DrawMode::Stroke(stroke) | DrawMode::FillAndStroke(_, stroke) => {
                f64::from(stroke.line_width) * transform_scale(props.transform) / 2.0
            }
            DrawMode::Fill(_) => 0.0,
            DrawMode::Invisible => return,
        };
        self.add(bbox.inflate(half_width, half_width));
    }
    fn push_clip_path(&mut self, _: &ClipPath) {}
    fn push_transparency_group(&mut self, _: f32, _: Option<SoftMask<'a>>, _: BlendMode) {}
    fn draw_glyph_run(&mut self, run: &GlyphRun<'_, 'a>, props: DrawProps<'a>, mode: &DrawMode) {
        if matches!(mode, DrawMode::Invisible) {
            return;
        }
        for glyph in run.glyphs() {
            match &**glyph {
                Glyph::Outline(outline) => {
                    if let Some(bbox) =
                        path_bbox(&outline.outline(), props.transform * glyph.transform())
                    {
                        self.add(bbox);
                    }
                }
                Glyph::Type3(type3) => {
                    type3.interpret(
                        &mut crate::render::DynDevice(self),
                        props.transform,
                        glyph.transform(),
                        &props.paint,
                    );
                }
            }
        }
    }
    fn draw_image(&mut self, image: Image<'a, '_>, props: ImageDrawProps<'a>) {
        let size = Rect::new(
            0.0,
            0.0,
            f64::from(image.width()),
            f64::from(image.height()),
        );
        self.add(transformed_rect(size, props.transform));
    }
    fn pop_clip(&mut self) {}
    fn pop_transparency_group(&mut self) {}
}

/// A `Device` that captures a [`TilingPattern`] cell's painted operations in
/// pattern space, strictly bounded by `budget` and `MAX_TILING_DEPTH`.
struct TilingCellCollector<'b> {
    ops: Vec<TilingCellOp>,
    /// Tracks raw `push_clip_path` depth so the synthetic outer `/BBox` clip
    /// that `TilingPattern::interpret` pushes at depth 0 is skipped.
    raw_clip_depth: usize,
    group_depth: usize,
    depth: u8,
    budget: &'b Budget,
    refusal: Option<&'static str>,
    error: Option<Error>,
}

impl<'b> TilingCellCollector<'b> {
    fn new(depth: u8, budget: &'b Budget) -> Self {
        Self {
            ops: Vec::new(),
            raw_clip_depth: 0,
            group_depth: 0,
            depth,
            budget,
            refusal: None,
            error: None,
        }
    }

    fn stopped(&self) -> bool {
        self.error.is_some() || self.refusal.is_some()
    }

    fn run(&mut self, callback: impl FnOnce(&mut Self) -> Result<(), Error>) {
        if !self.stopped()
            && let Err(error) = callback(self)
        {
            self.error = Some(error);
        }
    }

    fn push_op(&mut self, op: TilingCellOp) -> Result<(), Error> {
        self.budget.charge(LimitCode::ContentElementCount, 1)?;
        self.budget.charge(LimitCode::InterpretationFuel, 1)?;
        self.budget.charge(
            LimitCode::MemorySize,
            std::mem::size_of::<TilingCellOp>() as u64,
        )?;
        push(&mut self.ops, op, "tiling pattern cell operations")
    }

    fn cell_paint<'a>(
        &mut self,
        paint: &Paint<'a>,
        is_stroke: bool,
    ) -> Result<Option<CellPaint>, Error> {
        match paint {
            Paint::Color(color) => {
                let rgba = color.to_rgba().components().map(f64::from);
                if !rgba.iter().all(|value| value.is_finite()) {
                    return Ok(None);
                }
                Ok(Some(CellPaint::Color(rgba)))
            }
            Paint::Pattern(pattern) => match pattern.as_ref() {
                Pattern::Shading(shading) => {
                    if !matches!(
                        shading.shading.shading_type.as_ref(),
                        ShadingType::RadialAxial { .. }
                    ) {
                        self.refusal = Some(
                            "a tiling pattern cell with a non-axial/radial shading cannot be drawn",
                        );
                        return Ok(None);
                    }
                    let matrix = shading.matrix.as_coeffs();
                    if !matrix.iter().all(|value| value.is_finite()) || !shading.opacity.is_finite()
                    {
                        return Ok(None);
                    }
                    self.budget.charge(
                        LimitCode::MemorySize,
                        std::mem::size_of::<ShadingPattern>() as u64,
                    )?;
                    Ok(Some(CellPaint::Shading {
                        shading: Box::new(shading.clone()),
                        matrix,
                    }))
                }
                Pattern::Tiling(nested) => {
                    if self.depth >= MAX_TILING_DEPTH {
                        self.refusal = Some("a tiling pattern nests too deeply to draw");
                        return Ok(None);
                    }
                    let matrix = nested.matrix.as_coeffs();
                    let opacity = f64::from(nested.opacity);
                    if !matrix.iter().all(|value| value.is_finite())
                        || !opacity.is_finite()
                        || !nested.bbox.x0.is_finite()
                        || !nested.bbox.y0.is_finite()
                        || !nested.bbox.x1.is_finite()
                        || !nested.bbox.y1.is_finite()
                        || !nested.x_step.is_finite()
                        || !nested.y_step.is_finite()
                    {
                        return Ok(None);
                    }
                    let opacity = opacity.clamp(0.0, 1.0);
                    let cell = capture_tiling_cell(nested, is_stroke, self.depth + 1, self.budget)?;
                    if let Some(reason) = cell.refusal {
                        self.refusal = Some(reason);
                        return Ok(None);
                    }
                    Ok(Some(CellPaint::Tiling {
                        cell: Box::new(cell),
                        matrix,
                        opacity,
                    }))
                }
            },
        }
    }

    fn record_cell_path<'a>(
        &mut self,
        path: &BezPath,
        path_transform: Affine,
        stroke_transform: Affine,
        props: &DrawProps<'a>,
        mode: &DrawMode,
    ) -> Result<(), Error> {
        self.budget.charge(LimitCode::InterpretationFuel, 1)?;
        if matches!(mode, DrawMode::Invisible) {
            return Ok(());
        }
        if props.soft_mask.is_some() {
            self.refusal = Some("a tiling pattern cell with a soft mask cannot be drawn");
            return Ok(());
        }
        let Some(segs) = segments(path, path_transform)? else {
            return Ok(());
        };
        if segs.kinds.is_empty() {
            return Ok(());
        }
        let seg_bytes = segs
            .kinds
            .len()
            .saturating_add(segs.coords.len().saturating_mul(std::mem::size_of::<f64>()));
        self.budget
            .charge(LimitCode::ContentElementCount, segs.count() as u64)?;
        self.budget
            .charge(LimitCode::MemorySize, seg_bytes as u64)?;

        let (fill, even_odd, stroke) = match mode {
            DrawMode::Fill(rule) => {
                let Some(paint) = self.cell_paint(&props.paint, false)? else {
                    return Ok(());
                };
                (Some(paint), *rule == FillRule::EvenOdd, None)
            }
            DrawMode::Stroke(stroke_props) => {
                let style = StrokeStyle::new(stroke_props, stroke_transform)?;
                if !style.is_finite() {
                    return Ok(());
                }
                let Some(paint) = self.cell_paint(&props.paint, true)? else {
                    return Ok(());
                };
                self.budget
                    .charge(LimitCode::ContentElementCount, style.dash_count() as u64)?;
                self.budget.charge(
                    LimitCode::MemorySize,
                    (style
                        .dashes
                        .len()
                        .saturating_mul(std::mem::size_of::<f64>())) as u64,
                )?;
                (
                    None,
                    false,
                    Some(CellStroke {
                        paint,
                        width: style.width,
                        cap: style.cap,
                        join: style.join,
                        miter_limit: style.miter_limit,
                        dashes: style.dashes,
                        dash_offset: style.dash_offset,
                    }),
                )
            }
            DrawMode::FillAndStroke(rule, stroke_props) => {
                let style = StrokeStyle::new(stroke_props, stroke_transform)?;
                if !style.is_finite() {
                    return Ok(());
                }
                let Some(paint) = self.cell_paint(&props.paint, false)? else {
                    return Ok(());
                };
                self.budget
                    .charge(LimitCode::ContentElementCount, style.dash_count() as u64)?;
                self.budget.charge(
                    LimitCode::MemorySize,
                    (style
                        .dashes
                        .len()
                        .saturating_mul(std::mem::size_of::<f64>())) as u64,
                )?;
                (
                    Some(paint.clone()),
                    *rule == FillRule::EvenOdd,
                    Some(CellStroke {
                        paint,
                        width: style.width,
                        cap: style.cap,
                        join: style.join,
                        miter_limit: style.miter_limit,
                        dashes: style.dashes,
                        dash_offset: style.dash_offset,
                    }),
                )
            }
            DrawMode::Invisible => return Ok(()),
        };
        self.push_op(TilingCellOp::Path {
            kinds: segs.kinds,
            coords: segs.coords,
            fill,
            even_odd,
            stroke,
            blend: blend_code(props.blend_mode),
        })
    }
}

impl<'a> Device<'a> for TilingCellCollector<'_> {
    fn draw_path(&mut self, path: &BezPath, props: DrawProps<'a>, mode: &DrawMode) {
        self.run(|this| {
            this.record_cell_path(path, props.transform, props.transform, &props, mode)
        });
    }

    fn push_clip_path(&mut self, clip: &ClipPath) {
        let depth = self.raw_clip_depth;
        self.raw_clip_depth = self.raw_clip_depth.saturating_add(1);
        if depth == 0 {
            // `TilingPattern::interpret` pushes the pattern `/BBox` as the
            // outermost clip; the written PDF pattern's `/BBox` already clips.
            return;
        }
        self.run(|this| {
            if depth > MAX_TILING_STACK_DEPTH {
                this.refusal = Some("a tiling pattern cell clips too deeply to draw");
                return Ok(());
            }
            let segs = segments(&clip.path, Affine::IDENTITY)?.unwrap_or_default();
            let seg_bytes = segs
                .kinds
                .len()
                .saturating_add(segs.coords.len().saturating_mul(std::mem::size_of::<f64>()));
            this.budget
                .charge(LimitCode::ContentElementCount, segs.count() as u64)?;
            this.budget
                .charge(LimitCode::MemorySize, seg_bytes as u64)?;
            this.push_op(TilingCellOp::PushClip {
                kinds: segs.kinds,
                coords: segs.coords,
                even_odd: clip.fill == FillRule::EvenOdd,
            })
        });
    }

    fn pop_clip(&mut self) {
        if self.raw_clip_depth == 0 {
            return;
        }
        self.raw_clip_depth -= 1;
        if self.raw_clip_depth == 0 {
            return;
        }
        self.run(|this| this.push_op(TilingCellOp::PopClip));
    }

    fn push_transparency_group(
        &mut self,
        opacity: f32,
        mask: Option<SoftMask<'a>>,
        blend: BlendMode,
    ) {
        let depth = self.group_depth;
        self.group_depth = self.group_depth.saturating_add(1);
        self.run(|this| {
            if mask.is_some() {
                this.refusal = Some("a tiling pattern cell with a soft mask cannot be drawn");
                return Ok(());
            }
            if depth >= MAX_TILING_STACK_DEPTH {
                this.refusal = Some("a tiling pattern cell nests groups too deeply to draw");
                return Ok(());
            }
            let opacity = f64::from(opacity);
            // Always emit a balanced PushGroup when group_depth increments;
            // clamp finite values to [0.0, 1.0] and treat non-finite as 1.0.
            let clamped = if opacity.is_finite() {
                opacity.clamp(0.0, 1.0)
            } else {
                1.0
            };
            this.push_op(TilingCellOp::PushGroup {
                opacity: clamped,
                blend: blend_code(blend),
            })
        });
    }

    fn pop_transparency_group(&mut self) {
        if self.group_depth == 0 {
            return;
        }
        self.group_depth -= 1;
        self.run(|this| this.push_op(TilingCellOp::PopGroup));
    }

    fn draw_glyph_run(&mut self, run: &GlyphRun<'_, 'a>, props: DrawProps<'a>, mode: &DrawMode) {
        self.run(|this| Ok(this.budget.charge(LimitCode::InterpretationFuel, 1)?));
        if matches!(mode, DrawMode::Invisible) || self.stopped() {
            return;
        }
        if props.soft_mask.is_some() {
            self.refusal = Some("a tiling pattern cell with a soft mask cannot be drawn");
            return;
        }
        for glyph in run.glyphs() {
            if self.stopped() {
                break;
            }
            self.run(|this| Ok(this.budget.charge(LimitCode::InterpretationFuel, 1)?));
            if self.stopped() {
                break;
            }
            match &**glyph {
                Glyph::Outline(outline) => {
                    let outline_path = outline.outline();
                    let transform = props.transform * glyph.transform();
                    self.run(|this| {
                        this.record_cell_path(
                            &outline_path,
                            transform,
                            props.transform,
                            &props,
                            mode,
                        )
                    });
                }
                Glyph::Type3(type3) => {
                    if self.depth >= MAX_TILING_DEPTH {
                        self.refusal = Some("a tiling pattern nests too deeply to draw");
                        break;
                    }
                    self.depth += 1;
                    type3.interpret(
                        &mut crate::render::DynDevice(self),
                        props.transform,
                        glyph.transform(),
                        &props.paint,
                    );
                    self.depth -= 1;
                }
            }
        }
    }

    fn draw_image(&mut self, image: Image<'a, '_>, props: ImageDrawProps<'a>) {
        self.run(|this| {
            this.budget.charge(LimitCode::InterpretationFuel, 1)?;
            if props.soft_mask.is_some() {
                this.refusal = Some("a tiling pattern cell with a soft mask cannot be drawn");
                return Ok(());
            }
            let (width, height) = (image.width(), image.height());
            let matrix =
                props.transform * Affine::scale_non_uniform(f64::from(width), f64::from(height));
            if !affine_is_finite(matrix) {
                return Ok(());
            }
            let blend = blend_code(props.blend_mode);
            match image {
                Image::Raster(raster) => {
                    let Some(captured) = capture_raster_image(&raster, this.budget)? else {
                        this.refusal = Some(
                            "a raster image inside a tiling pattern cell could not be decoded",
                        );
                        return Ok(());
                    };
                    this.push_op(TilingCellOp::RasterImage {
                        image: captured,
                        matrix: matrix.as_coeffs(),
                        blend,
                    })
                }
                Image::Stencil(stencil) => {
                    let budget = this.budget;
                    let interpolate = image_interpolate(stencil.stream());
                    let pixels =
                        u64::from(stencil.width()).saturating_mul(u64::from(stencil.height()));
                    budget.charge(LimitCode::TotalDecompressedSize, pixels)?;
                    let mut captured = None;
                    stencil.with_stencil(
                        |luma, paint| {
                            captured = Some((|| -> Result<_, Error> {
                                let Some(fill) = this.cell_paint(paint, false)? else {
                                    return Ok(None);
                                };
                                let packed = pack_stencil_mask(&luma, interpolate, budget)?;
                                Ok(Some((packed, fill)))
                            })());
                        },
                        None,
                    );
                    let Some(result) = captured else {
                        this.refusal = Some(
                            "a stencil mask inside a tiling pattern cell could not be decoded",
                        );
                        return Ok(());
                    };
                    if let Some((stencil, fill)) = result? {
                        this.push_op(TilingCellOp::StencilImage {
                            stencil,
                            fill,
                            matrix: matrix.as_coeffs(),
                            blend,
                        })?;
                    }
                    Ok(())
                }
            }
        });
    }
}

/// Captures a `TilingPattern` cell's painted operations in pattern space.
fn capture_tiling_cell<'a>(
    tiling: &TilingPattern<'a>,
    is_stroke: bool,
    depth: u8,
    budget: &Budget,
) -> Result<CapturedTilingCell, Error> {
    budget.charge(LimitCode::InterpretationFuel, 1)?;
    let bbox = bbox_tuple(tiling.bbox);
    let x_step = f64::from(tiling.x_step);
    let y_step = f64::from(tiling.y_step);
    let mut collector = TilingCellCollector::new(depth, budget);
    let interpreted = tiling.interpret(
        &mut crate::render::DynDevice(&mut collector),
        Affine::IDENTITY,
        is_stroke,
    );
    if let Some(error) = collector.error {
        return Err(error);
    }
    let refusal = collector.refusal.or_else(|| {
        interpreted
            .is_none()
            .then_some("the tiling pattern stream could not be decoded")
    });
    if refusal.is_some() {
        collector.ops.clear();
    }
    collector.ops.shrink_to_fit();
    Ok(CapturedTilingCell {
        bbox,
        x_step,
        y_step,
        ops: collector.ops,
        refusal,
    })
}

/// The kinds of item that a collector records.
#[derive(Clone, Copy)]
pub struct ItemKinds {
    pub text_runs: bool,
    pub paths: bool,
    pub images: bool,
}

impl ItemKinds {
    /// Record no items, only feed the sink.
    pub const NONE: Self = Self {
        text_runs: false,
        paths: false,
        images: false,
    };

    fn any(self) -> bool {
        self.text_runs || self.paths || self.images
    }
}

/// Hash key for interning stored PDF colors without heap allocation for the
/// standard 1-to-4 component color spaces (`DeviceGray`, `DeviceRGB`,
/// `DeviceCMYK`, `CalGray`, `CalRGB`, `Lab`, `Indexed`, `Separation`).
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
enum StoredColorKey {
    Inline { space: u8, len: u8, bits: [u32; 4] },
    Heap { space: u8, bits: Vec<u32> },
}

impl StoredColorKey {
    fn from_components(space: u8, components: &[f32]) -> Result<Self, Error> {
        let norm_bits =
            |value: f32| -> u32 { if value == 0.0 { 0.0_f32 } else { value }.to_bits() };
        if components.len() <= 4 {
            let mut bits = [0u32; 4];
            for (slot, &value) in bits.iter_mut().zip(components) {
                *slot = norm_bits(value);
            }
            return Ok(Self::Inline {
                space,
                len: components.len() as u8,
                bits,
            });
        }
        let mut bits = with_capacity(components.len(), "stored color key")?;
        #[expect(
            clippy::disallowed_methods,
            reason = "fills the exact capacity reserved above"
        )]
        bits.extend(components.iter().map(|&value| norm_bits(value)));
        Ok(Self::Heap { space, bits })
    }

    fn heap_bytes(&self) -> usize {
        match self {
            Self::Inline { .. } => 0,
            Self::Heap { bits, .. } => bits.len().saturating_mul(std::mem::size_of::<u32>()),
        }
    }
}

pub(crate) struct ContentCollector<'a, 'b, 'c, 's> {
    sink: &'s mut dyn ContentSink,
    /// The items to record; with none, the collector only feeds the sink.
    kinds: ItemKinds,
    records: Vec<Record>,
    paths: PathTable,
    /// The index of each pattern by hayro's key (and stroke flag for tiling
    /// patterns, since uncolored tilings use separate fill and stroke colors).
    /// Repeated paints need no new tuple allocation.
    pattern_indexes: HashMap<(u128, bool), i32>,
    /// The index of each unique stored PDF color `(space_code, f32 bit patterns)`
    /// in [`PathTable::stored_colors`].
    stored_color_indexes: HashMap<StoredColorKey, i32>,
    /// The segments of the path being recorded, in a buffer that is reused.
    segments: Segments,
    /// The segments of the fill path that a stroke may complete.
    pending_segments: Segments,
    fonts: Vec<FontTuple>,
    font_indexes: HashMap<u128, u32>,
    clips: Vec<ClipTuple>,
    groups: Vec<GroupTuple>,
    marks: Vec<MarkTuple>,
    states: Vec<StateTuple>,
    clip_stack: Vec<u32>,
    /// The intersected box of the clip paths in force at each clip depth,
    /// kept for the sink whatever the item kinds.
    clip_boxes: Vec<Rect>,
    group_stack: Vec<u32>,
    mark_stack: Vec<u32>,
    actual_text_level: Option<usize>,
    /// The interned state of the current stacks, or `None` after a change.
    state: Option<u32>,
    pending: Option<Pending>,
    /// The glyph matrices of the fill run that a stroke may complete.
    pending_matrices: Vec<f64>,
    /// The glyph runs that hayro has shown.
    runs: u32,
    /// The glyphs of the runs that hayro has shown, without the strokes that
    /// complete a fill, which show the fill's glyphs again.
    shown: usize,
    /// The place among them of the first glyph of the latest such run.
    first_shown: usize,
    outlines: HashMap<u128, Option<GlyphInk>>,
    type3_boxes: HashMap<u128, Option<Rect>>,
    stretches: &'c Stretches<'a>,
    /// Recent `(dict_data_ptr, dict_data_len, cache_key)` lookups to avoid
    /// rehashing the same font dictionary on every text run.
    recent_font_keys: [(usize, usize, u128); 8],
    recent_font_next: usize,
    /// The images recorded so far, the ordinal of the next one.
    images: usize,
    xref: &'a XRef,
    budget: &'b Budget,
    error: Option<Error>,
}

impl<'a, 'b, 'c, 's> ContentCollector<'a, 'b, 'c, 's> {
    /// A collector that feeds `sink` and records the items of `kinds` within
    /// `budget`. `stretches` loads fonts as the interpretation does.
    pub(crate) fn new(
        sink: &'s mut dyn ContentSink,
        kinds: ItemKinds,
        stretches: &'c Stretches<'a>,
        xref: &'a XRef,
        page_size: (f64, f64),
        budget: &'b Budget,
    ) -> Self {
        Self {
            sink,
            kinds,
            records: Vec::new(),
            paths: PathTable::new(page_size),
            pattern_indexes: HashMap::new(),
            stored_color_indexes: HashMap::new(),
            segments: Segments::default(),
            pending_segments: Segments::default(),
            fonts: Vec::new(),
            font_indexes: HashMap::new(),
            clips: Vec::new(),
            groups: Vec::new(),
            marks: Vec::new(),
            states: Vec::new(),
            clip_stack: Vec::new(),
            clip_boxes: Vec::new(),
            group_stack: Vec::new(),
            mark_stack: Vec::new(),
            actual_text_level: None,
            state: None,
            pending: None,
            pending_matrices: Vec::new(),
            runs: 0,
            shown: 0,
            first_shown: 0,
            outlines: HashMap::new(),
            type3_boxes: HashMap::new(),
            stretches,
            recent_font_keys: [(0, 0, 0); 8],
            recent_font_next: 0,
            images: 0,
            xref,
            budget,
            error: None,
        }
    }

    /// Count `count` more elements against the budget.
    fn charge(&self, count: usize) -> Result<(), Error> {
        let count = u64::try_from(count).unwrap_or(u64::MAX);
        Ok(self.budget.charge(LimitCode::ContentElementCount, count)?)
    }

    /// Run one callback, keeping its first error and ignoring every later
    /// callback.
    fn run(&mut self, callback: impl FnOnce(&mut Self) -> Result<(), Error>) {
        if self.error.is_none()
            && let Err(error) = callback(self)
        {
            self.error = Some(error);
        }
    }

    fn intern_state(&mut self) -> Result<u32, Error> {
        if let Some(state) = self.state {
            return Ok(state);
        }
        let mut clips = with_capacity(self.clip_stack.len(), "context clips")?;
        #[expect(
            clippy::disallowed_methods,
            reason = "fills the capacity reserved above"
        )]
        clips.extend_from_slice(&self.clip_stack);
        let mut groups = with_capacity(self.group_stack.len(), "context groups")?;
        #[expect(
            clippy::disallowed_methods,
            reason = "fills the capacity reserved above"
        )]
        groups.extend_from_slice(&self.group_stack);
        let mut marks = with_capacity(self.mark_stack.len(), "context marked content")?;
        #[expect(
            clippy::disallowed_methods,
            reason = "fills the capacity reserved above"
        )]
        marks.extend_from_slice(&self.mark_stack);
        self.charge(1 + clips.len() + groups.len() + marks.len())?;
        let index = index_of(self.states.len())?;
        push(
            &mut self.states,
            (clips, groups, marks),
            "graphics contexts",
        )?;
        self.state = Some(index);
        Ok(index)
    }

    fn item_context(
        &mut self,
        blend: BlendMode,
        soft_mask: Option<&SoftMask<'_>>,
    ) -> Result<ItemContext, Error> {
        Ok(ItemContext {
            state: self.intern_state()?,
            blend: blend_code(blend),
            soft_mask: soft_mask_code(soft_mask),
        })
    }

    /// The index of the font with key `key` in the fonts table, adding it
    /// with what `glyph`'s program and `font_dict` say about it.
    #[expect(
        clippy::disallowed_methods,
        reason = "one font per text run at most, which the caller has charged to content_element_count"
    )]
    fn outline_font(
        &mut self,
        key: u128,
        glyph: &OutlineGlyph,
        font_dict: Option<&Dict<'_>>,
    ) -> Result<u32, Error> {
        if let Some(index) = self.font_indexes.get(&key) {
            return Ok(*index);
        }
        let font = outline_font_tuple(glyph, font_dict, self.xref, self.budget)?;
        let index = index_of(self.fonts.len())?;
        self.font_indexes.try_reserve(1).map_err(|error| {
            Error::Allocation(format!("failed to grow the font index: {error}"))
        })?;
        push(&mut self.fonts, font, "fonts")?;
        self.font_indexes.insert(key, index);
        Ok(index)
    }

    /// The index of a Type 3 font in the fonts table, keyed by its PDF font
    /// dictionary so distinct Type 3 fonts keep distinct identities.
    #[expect(
        clippy::disallowed_methods,
        reason = "one font per text run at most, which the caller has charged to content_element_count"
    )]
    fn type3_font(&mut self, font_dict: Option<&Dict<'_>>) -> Result<u32, Error> {
        let key = font_dict.map_or(FALLBACK_TYPE3_FONT_KEY, |dict| dict.cache_key());
        if let Some(index) = self.font_indexes.get(&key) {
            return Ok(*index);
        }
        let font = type3_font_tuple(font_dict, self.xref, self.budget)?;
        let index = index_of(self.fonts.len())?;
        self.font_indexes.try_reserve(1).map_err(|error| {
            Error::Allocation(format!("failed to grow the font index: {error}"))
        })?;
        push(&mut self.fonts, font, "fonts")?;
        self.font_indexes.insert(key, index);
        Ok(index)
    }

    /// The display-space ink box of an outline glyph.
    fn outline_box(
        &mut self,
        glyph: &OutlineGlyph,
        transform: Affine,
    ) -> Result<Option<Rect>, Error> {
        let key = glyph.identifier().cache_key();
        if !self.outlines.contains_key(&key) {
            let path = glyph.outline();
            let ink = path_bbox(&path, Affine::IDENTITY).map(|bbox| GlyphInk { bbox, path });
            if self.outlines.len() >= MAX_CACHED_GLYPHS {
                return Ok(ink.and_then(|ink| path_bbox(&ink.path, transform)));
            }
            #[expect(
                clippy::disallowed_methods,
                reason = "the cache keeps MAX_CACHED_GLYPHS outlines at most, checked above"
            )]
            self.outlines.try_reserve(1).map_err(|error| {
                Error::Allocation(format!("failed to grow the outline cache: {error}"))
            })?;
            #[expect(
                clippy::disallowed_methods,
                reason = "the cache keeps MAX_CACHED_GLYPHS outlines at most, checked above"
            )]
            self.outlines.insert(key, ink);
        }
        let Some(Some(ink)) = self.outlines.get(&key) else {
            return Ok(None);
        };
        if is_axis_aligned(transform) {
            return Ok(Some(transformed_rect(ink.bbox, transform)));
        }
        Ok(path_bbox(&ink.path, transform))
    }

    /// The display-space ink box of a Type 3 glyph. The procedure is measured
    /// once in glyph space; under a transform that rotates by other than a
    /// right angle, the box encloses the rotated glyph-space box.
    fn type3_box(
        &mut self,
        glyph: &Type3Glyph<'a>,
        paint: &Paint<'a>,
        transform: Affine,
    ) -> Result<Option<Rect>, Error> {
        let key = glyph.cache_key();
        let bbox = match self.type3_boxes.get(&key) {
            Some(bbox) => *bbox,
            None => {
                let mut ink = InkBounds { bbox: None };
                glyph.interpret(
                    &mut crate::render::DynDevice(&mut ink),
                    Affine::IDENTITY,
                    Affine::IDENTITY,
                    paint,
                );
                if self.type3_boxes.len() < MAX_CACHED_GLYPHS {
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "the cache keeps MAX_CACHED_GLYPHS boxes at most, checked above"
                    )]
                    self.type3_boxes.try_reserve(1).map_err(|error| {
                        Error::Allocation(format!("failed to grow the Type 3 cache: {error}"))
                    })?;
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "the cache keeps MAX_CACHED_GLYPHS boxes at most, checked above"
                    )]
                    self.type3_boxes.insert(key, ink.bbox);
                }
                ink.bbox
            }
        };
        Ok(bbox.map(|bbox| transformed_rect(bbox, transform)))
    }

    /// Interns a path color's stored PDF color space and original component
    /// values in [`PathTable::stored_colors`], charging new entries to
    /// `LimitCode::MemorySize`. Uses an inline stack key for 1-to-4 component
    /// color spaces so steady-state lookups allocate nothing on the heap.
    #[expect(
        clippy::disallowed_methods,
        reason = "interned stored color is bounded by path count (content_element_count) and \
                  charged to memory_size before allocation"
    )]
    fn intern_stored_color(&mut self, space: u8, components: &[f32]) -> Result<i32, Error> {
        let key = StoredColorKey::from_components(space, components)?;
        if let Some(&index) = self.stored_color_indexes.get(&key) {
            return Ok(index);
        }
        let stored_components = copy_color_components(components, self.budget)?;
        let entry_bytes = key
            .heap_bytes()
            .saturating_add(std::mem::size_of::<StoredColorKey>())
            .saturating_add(std::mem::size_of::<StoredColorTuple>());
        self.budget
            .charge(LimitCode::MemorySize, entry_bytes as u64)?;
        let index = offset(self.paths.stored_colors.len())?;
        self.stored_color_indexes.try_reserve(1).map_err(|error| {
            Error::Allocation(format!("failed to grow the stored color index: {error}"))
        })?;
        push(
            &mut self.paths.stored_colors,
            (space, stored_components),
            PATH_COLUMNS,
        )?;
        self.stored_color_indexes.insert(key, index);
        Ok(index)
    }

    /// The form of `paint` in the path columns, adding a pattern or stored
    /// color to the table when it is new, or `None` when a value of the paint
    /// is not finite.
    fn path_paint(
        &mut self,
        paint: &Paint<'a>,
        is_stroke: bool,
    ) -> Result<Option<PathPaint>, Error> {
        if let Paint::Color(color) = paint {
            let rgba = color.to_rgba().components();
            let raw_components = color.components();
            let finite = rgba.iter().all(|value| value.is_finite())
                && raw_components.iter().all(|value| value.is_finite());
            if !finite {
                return Ok(None);
            }
            let space = color_space_code(color.color_space());
            let stored = self.intern_stored_color(space, raw_components)?;
            return Ok(Some(PathPaint::Color(rgba, stored)));
        }
        let Paint::Pattern(pattern) = paint else {
            unreachable!("colors returned above");
        };
        let (shading, tiling) = match pattern.as_ref() {
            Pattern::Shading(shading) => (Some(shading), None),
            Pattern::Tiling(tiling) => (None, Some(tiling.as_ref())),
        };
        // hayro's cache key does not include a transfer function. Keep such
        // paints separate so that two transfers of one shading cannot alias.
        let cacheable = shading.is_none_or(|shading| shading.transfer_function.is_none());
        let key = (pattern.cache_key(), tiling.is_some() && is_stroke);
        if cacheable && let Some(&index) = self.pattern_indexes.get(&key) {
            return Ok(Some(PathPaint::Pattern(index)));
        }
        let mut paint = paint_tuple(paint, self.budget)?;
        if !paint_is_finite(&paint) {
            return Ok(None);
        }
        let captured_tiling = match tiling {
            Some(tiling) => Some(capture_tiling_cell(tiling, is_stroke, 0, self.budget)?),
            None => None,
        };
        let index = offset(self.paths.patterns.len())?;
        push(&mut paint.1, f64::from(index), "paint values")?;
        #[expect(
            clippy::disallowed_methods,
            reason = "one pattern per paint of a path or stencil, whose item the collector charges \
                      to content_element_count"
        )]
        self.pattern_indexes.try_reserve(1).map_err(|error| {
            Error::Allocation(format!("failed to grow the pattern index: {error}"))
        })?;
        push(&mut self.paths.patterns, paint, PATH_COLUMNS)?;
        let retained = shading.filter(|paint| {
            matches!(
                paint.shading.shading_type.as_ref(),
                ShadingType::RadialAxial { .. }
            )
        });
        push(&mut self.paths.shadings, retained.cloned(), PATH_COLUMNS)?;
        push(&mut self.paths.tilings, captured_tiling, PATH_COLUMNS)?;
        #[expect(
            clippy::disallowed_methods,
            reason = "one pattern per paint of a path or stencil, whose item the collector charges \
                      to content_element_count"
        )]
        if cacheable {
            self.pattern_indexes.insert(key, index);
        }
        Ok(Some(PathPaint::Pattern(index)))
    }

    /// Captures the fill paint of a stencil image mask, registering pattern
    /// paints in [`PathTable`] so they keep their shading/tiling sources.
    fn image_fill(&mut self, paint: &Paint<'a>) -> Result<Option<ImageFill>, Error> {
        match paint {
            Paint::Color(_) => {
                let tuple = paint_tuple(paint, self.budget)?;
                Ok(paint_is_finite(&tuple).then_some(ImageFill::Color(tuple)))
            }
            Paint::Pattern(_) => Ok(match self.path_paint(paint, false)? {
                Some(PathPaint::Pattern(index)) => Some(ImageFill::Pattern(index)),
                _ => None,
            }),
        }
    }

    fn record_path(
        &mut self,
        path: &BezPath,
        props: &DrawProps<'a>,
        fill: Option<FillRule>,
        stroke: Option<&StrokeProps>,
        pending: Option<Pending>,
    ) -> Result<(), Error> {
        if !self.segments.read(path, props.transform)? {
            return Ok(());
        }
        let Some(bbox) = path_bbox(path, props.transform).filter(|bbox| rect_is_finite(*bbox))
        else {
            return Ok(());
        };
        let stroke = stroke
            .map(|stroke| StrokeStyle::new(stroke, props.transform))
            .transpose()?;
        if !stroke.as_ref().is_none_or(StrokeStyle::is_finite) {
            return Ok(());
        }
        let is_stroke = fill.is_none() && stroke.is_some();
        let Some(paint) = self.path_paint(&props.paint, is_stroke)? else {
            return Ok(());
        };
        self.charge(stroke.as_ref().map_or(0, StrokeStyle::dash_count))?;
        let context = self.item_context(props.blend_mode, props.soft_mask.as_ref())?;
        if fill.is_none()
            && pending == Some(Pending::Path)
            && let Some(&Record::Path(row)) = self.records.last()
            && self.paths.stroke.present[row as usize] == 0
            && self.pending_segments.equals(&self.segments)
            && let Some(stroke) = &stroke
        {
            return self.paths.set_stroke(row as usize, paint, stroke);
        }
        self.charge(1 + self.segments.count())?;
        let row = self
            .paths
            .push(&self.segments, bbox, paint, fill, stroke.as_ref(), context)?;
        push(&mut self.records, Record::Path(row), "content items")?;
        if stroke.is_none() {
            self.pending = Some(Pending::Path);
            std::mem::swap(&mut self.segments, &mut self.pending_segments);
        }
        Ok(())
    }

    /// Send a run's glyphs to the sink, unless a stroke completes the fill
    /// that sent them, and record the run as an item.
    fn record_text(
        &mut self,
        run: &GlyphRun<'_, 'a>,
        props: &DrawProps<'a>,
        mode: &DrawMode,
        pending: Option<Pending>,
        ordinal: u32,
    ) -> Result<(), Error> {
        let glyphs = run.glyphs();
        if glyphs.is_empty() {
            return Ok(());
        }
        let font_key = run_font_key(run, &mut self.recent_font_keys, &mut self.recent_font_next);
        let wants_advances = self.sink.wants_advances();
        let stretching = if wants_advances || self.kinds.text_runs {
            self.stretches.font_of(run)
        } else {
            None
        };
        let is_fill = matches!(mode, DrawMode::Fill(_));
        // A stroke completes the preceding fill when it draws the same glyphs
        // at the same places. Equality fails for a transform that is not
        // finite, so such a run never completes a fill.
        let completes_fill = matches!(mode, DrawMode::Stroke(_))
            && pending == Some(Pending::Text)
            && self.pending_matrices.len() == glyphs.len().saturating_mul(6)
            && glyphs
                .iter()
                .zip(self.pending_matrices.as_chunks::<6>().0)
                .all(|(glyph, kept)| (props.transform * glyph.transform()).as_coeffs() == *kept);
        if is_fill {
            // The kept buffer is reused, so steady text allocates nothing here.
            self.pending_matrices.clear();
            #[expect(
                clippy::disallowed_methods,
                reason = "six coefficients per glyph of the run that hayro holds, in a buffer that \
                          each fill reuses"
            )]
            self.pending_matrices
                .try_reserve(glyphs.len().saturating_mul(6))
                .map_err(|error| {
                    Error::Allocation(format!("failed to keep glyph transforms: {error}"))
                })?;
        }
        if completes_fill {
            if !self.sink.stopped() {
                self.sink.stroke_completes_fill();
            }
        } else {
            self.first_shown = self.shown;
            self.shown += glyphs.len();
            if !self.sink.stopped() {
                self.sink.glyph_run(&GlyphStyle {
                    color: match (&props.paint, mode) {
                        (_, DrawMode::Invisible) | (Paint::Pattern(_), _) => None,
                        (Paint::Color(color), _) => Some(color.to_rgba().components()),
                    },
                    render_mode: match mode {
                        DrawMode::Fill(_) => RENDER_FILL,
                        DrawMode::Stroke(_) => RENDER_STROKE,
                        DrawMode::FillAndStroke(..) => RENDER_FILL_STROKE,
                        DrawMode::Invisible => RENDER_INVISIBLE,
                    },
                    clip: self.clip_boxes.last().copied(),
                    run: ordinal,
                    font: font_key,
                    first: props.transform * glyphs[0].transform(),
                });
            }
            for glyph in glyphs {
                let transform = props.transform * glyph.transform();
                if is_fill {
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "fills the capacity reserved above: six coefficients per glyph"
                    )]
                    self.pending_matrices
                        .extend_from_slice(&transform.as_coeffs());
                }
                if !self.sink.stopped() {
                    let ink = if self.sink.wants_ink() {
                        match &**glyph {
                            Glyph::Outline(outline) => self.outline_box(outline, transform)?,
                            Glyph::Type3(type3) => {
                                self.type3_box(type3, &props.paint, transform)?
                            }
                        }
                    } else {
                        None
                    };
                    let transform = if wants_advances {
                        without_stretch(transform, stretching.as_ref(), glyph)
                    } else {
                        transform
                    };
                    self.sink.glyph(glyph, transform, ink);
                }
            }
        }
        if self.kinds.text_runs {
            self.record_text_item(
                run,
                font_key,
                stretching.as_ref(),
                props,
                mode,
                completes_fill,
            )?;
        }
        if is_fill {
            self.pending = Some(Pending::Text);
        }
        Ok(())
    }

    fn record_text_item(
        &mut self,
        run: &GlyphRun<'_, 'a>,
        font_key: u128,
        stretching: Option<&Font<'a>>,
        props: &DrawProps<'a>,
        mode: &DrawMode,
        completes_fill: bool,
    ) -> Result<(), Error> {
        let glyphs = run.glyphs();
        let Some(first) = glyphs.first() else {
            return Ok(());
        };
        let mut matrices = with_capacity(glyphs.len().saturating_mul(6), "glyph matrices")?;
        for glyph in glyphs {
            let matrix = props.transform * glyph.transform() * Affine::scale(1000.0);
            #[expect(
                clippy::disallowed_methods,
                reason = "fills the capacity reserved above: six coefficients per glyph"
            )]
            matrices.extend_from_slice(&matrix.as_coeffs());
        }
        if !matrices.iter().all(|value| value.is_finite()) {
            return Ok(());
        }
        let (render_mode, fill, stroke) = match mode {
            DrawMode::Fill(_) => (
                RENDER_FILL,
                Some(paint_tuple(&props.paint, self.budget)?),
                None,
            ),
            DrawMode::Stroke(stroke) => (
                RENDER_STROKE,
                None,
                Some(StrokeRecord::new(
                    &props.paint,
                    stroke,
                    props.transform,
                    self.budget,
                )?),
            ),
            DrawMode::FillAndStroke(_, stroke) => (
                RENDER_FILL_STROKE,
                Some(paint_tuple(&props.paint, self.budget)?),
                Some(StrokeRecord::new(
                    &props.paint,
                    stroke,
                    props.transform,
                    self.budget,
                )?),
            ),
            DrawMode::Invisible => (RENDER_INVISIBLE, None, None),
        };
        if !stroke.as_ref().is_none_or(StrokeRecord::is_finite)
            || !fill.as_ref().is_none_or(paint_is_finite)
        {
            return Ok(());
        }
        self.charge(stroke.as_ref().map_or(0, StrokeRecord::dash_count))?;
        if completes_fill
            && let Some(Record::Text(previous)) = self.records.last_mut()
            && previous.render_mode == RENDER_FILL
            && previous.matrices == matrices
        {
            previous.render_mode = RENDER_FILL_STROKE;
            previous.stroke = stroke;
            return Ok(());
        }
        let context = self.item_context(props.blend_mode, props.soft_mask.as_ref())?;
        self.charge(1 + glyphs.len())?;
        let font = match &**first {
            Glyph::Outline(outline) => self.outline_font(font_key, outline, run.font_dict())?,
            Glyph::Type3(_) => self.type3_font(run.font_dict())?,
        };
        let writing_mode = match run.writing_mode() {
            WritingMode::Horizontal => WRITING_HORIZONTAL,
            WritingMode::Vertical => WRITING_VERTICAL,
        };
        let mut ids = with_capacity(glyphs.len(), "glyph IDs")?;
        let mut texts = with_capacity(glyphs.len(), "glyph text")?;
        let mut advances = with_capacity(glyphs.len(), "glyph advances")?;
        let mut boxes = with_capacity(glyphs.len(), "glyph boxes")?;
        let mut ink: Option<Rect> = None;
        let mut extent: Option<Rect> = None;
        #[expect(
            clippy::disallowed_methods,
            reason = "fills the capacity reserved above: one of each per glyph"
        )]
        for glyph in glyphs {
            let combined = props.transform * glyph.transform();
            let origin = combined * Point::ZERO;
            let (id, width, bbox) = match &**glyph {
                Glyph::Outline(outline) => (
                    Some(outline.glyph_id().to_u32()),
                    outline.advance_width().map(f64::from),
                    self.outline_box(outline, combined)?,
                ),
                Glyph::Type3(type3) => (None, None, self.type3_box(type3, &props.paint, combined)?),
            };
            // Advances are in hayro's 1000-unit glyph space, like outlines,
            // and are the PDF's widths, which the stretch of an outline is not.
            let unstretched = without_stretch(combined, stretching, glyph);
            let end = width
                .map(|width| unstretched * Point::new(width, 0.0))
                .filter(|end| end.x.is_finite() && end.y.is_finite());
            let advance = end.map(|end| (end - origin).hypot());
            let bbox = bbox.filter(|bbox| rect_is_finite(*bbox));
            let reach = Rect::from_points(origin, end.unwrap_or(origin));
            extent = Some(extent.map_or(reach, |extent| extent.union(reach)));
            if let Some(bbox) = bbox {
                ink = Some(ink.map_or(bbox, |ink| ink.union(bbox)));
            }
            ids.push(id);
            texts.push(glyph_text(glyph)?);
            advances.push(advance);
            boxes.push(bbox.map(bbox_tuple));
        }
        let Some(extent) = extent else {
            return Ok(());
        };
        let record = TextRunRecord {
            replay: super::redraw::capture(
                run,
                props.transform,
                self.stretches,
                self.actual_text_level.is_some(),
                self.budget,
            )?,
            ids,
            texts,
            matrices,
            advances,
            boxes,
            font,
            render_mode,
            fill,
            stroke,
            bbox: ink.unwrap_or(extent),
            extent,
            first_shown: self.first_shown,
            context,
            writing_mode,
        };
        push(
            &mut self.records,
            Record::Text(Box::new(record)),
            "content items",
        )
    }

    fn record_image(
        &mut self,
        image: &Image<'a, '_>,
        props: &ImageDrawProps<'a>,
    ) -> Result<(), Error> {
        let (width, height) = (image.width(), image.height());
        let matrix =
            props.transform * Affine::scale_non_uniform(f64::from(width), f64::from(height));
        let bbox = transformed_rect(Rect::new(0.0, 0.0, 1.0, 1.0), matrix);
        if !affine_is_finite(matrix) || !rect_is_finite(bbox) {
            return Ok(());
        }
        let context = self.item_context(props.blend_mode, props.soft_mask.as_ref())?;
        self.charge(1)?;
        self.sink.image(self.images, image)?;
        self.images += 1;
        let skip_decode = self.sink.handles_image_decode();
        let default_resolution = (DEFAULT_IMAGE_RESOLUTION, DEFAULT_IMAGE_RESOLUTION);
        let record = match image {
            Image::Stencil(stencil) => {
                let stream = stencil.stream();
                let xref = stream.obj_id().obj_number;
                let interpolate = image_interpolate(stream);
                let (fill, inline_index) = if skip_decode {
                    (None, None)
                } else {
                    let budget = self.budget;
                    let mut captured = None;
                    let pixels =
                        u64::from(stencil.width()).saturating_mul(u64::from(stencil.height()));
                    budget.charge(LimitCode::TotalDecompressedSize, pixels)?;
                    stencil.with_stencil(
                        |luma, paint| {
                            captured = Some((|| -> Result<_, Error> {
                                let fill = self.image_fill(paint)?;
                                let packed = if xref == 0 {
                                    Some(pack_stencil_mask(&luma, interpolate, budget)?)
                                } else {
                                    None
                                };
                                Ok((fill, packed))
                            })());
                        },
                        None,
                    );
                    match captured.transpose()? {
                        Some((fill, Some(packed))) => {
                            let idx = index_of(self.paths.inline_images.len())?;
                            push(
                                &mut self.paths.inline_images,
                                InlineImageData::Stencil(packed),
                                PATH_COLUMNS,
                            )?;
                            (fill, Some(idx))
                        }
                        Some((fill, None)) => (fill, None),
                        None => (None, None),
                    }
                };
                ImageRecord {
                    xref,
                    format: (width, height, 1, 1, String::new()),
                    is_stencil: true,
                    mask: MASK_NONE,
                    encoded_size: stream.raw_data().len(),
                    resolution: default_resolution,
                    matrix: matrix.as_coeffs(),
                    bbox,
                    context,
                    fill,
                    inline_index,
                }
            }
            Image::Raster(raster) => {
                let stream = raster.stream();
                let xref = stream.obj_id().obj_number;
                let dict = stream.dict();
                let (components, color_space) = image_color_space(dict);
                let bits = dict
                    .get::<u8>(b"BitsPerComponent")
                    .or_else(|| dict.get::<u8>(b"BPC"))
                    .unwrap_or(0);
                let mask = if dict.contains_key(b"SMask") {
                    MASK_SOFT
                } else if dict.get::<Stream<'_>>(b"Mask").is_some() {
                    MASK_EXPLICIT
                } else if dict.get::<Array<'_>>(b"Mask").is_some() {
                    MASK_COLOR_KEY
                } else {
                    MASK_NONE
                };
                let raw = stream.raw_data();
                let resolution = if stream.filters().last() == Some(&Filter::DctDecode) {
                    jpeg_resolution(&raw)
                } else {
                    default_resolution
                };
                let inline_index = if xref == 0 && !skip_decode {
                    match capture_raster_image(raster, self.budget)? {
                        Some(captured) => {
                            let idx = index_of(self.paths.inline_images.len())?;
                            push(
                                &mut self.paths.inline_images,
                                InlineImageData::Raster(captured),
                                PATH_COLUMNS,
                            )?;
                            Some(idx)
                        }
                        None => None,
                    }
                } else {
                    None
                };
                ImageRecord {
                    xref,
                    format: (width, height, bits, components, color_space),
                    is_stencil: false,
                    mask,
                    encoded_size: raw.len(),
                    resolution,
                    matrix: matrix.as_coeffs(),
                    bbox,
                    context,
                    fill: None,
                    inline_index,
                }
            }
        };
        push(
            &mut self.records,
            Record::Image(Box::new(record)),
            "content items",
        )
    }

    fn push_clip(&mut self, clip: &ClipPath) -> Result<(), Error> {
        let Some(segments) = segments(&clip.path, Affine::IDENTITY)? else {
            // A clip that no finite path describes leaves nothing visible.
            return self.push_clip_entry(Segments::default(), false, Rect::ZERO);
        };
        let bbox = path_bbox(&clip.path, Affine::IDENTITY)
            .filter(|bbox| rect_is_finite(*bbox))
            .unwrap_or(Rect::ZERO);
        self.push_clip_entry(segments, clip.fill == FillRule::EvenOdd, bbox)
    }

    /// Push the box of a clip path, intersected with the boxes in force.
    /// Text extraction records no items, so these boxes count no elements.
    #[expect(
        clippy::disallowed_methods,
        reason = "one box per clip in force, as hayro keeps one in its own stack, and pop_clip \
                  removes it"
    )]
    fn push_clip_box(&mut self, clip: &ClipPath) -> Result<(), Error> {
        // A clip that no finite path describes leaves nothing visible.
        let bbox = path_bbox(&clip.path, Affine::IDENTITY)
            .filter(|bbox| rect_is_finite(*bbox))
            .unwrap_or(Rect::ZERO);
        let bbox = self
            .clip_boxes
            .last()
            .map_or(bbox, |outer| outer.intersect(bbox));
        if self.clip_boxes.len() == self.clip_boxes.capacity() {
            self.clip_boxes.try_reserve(1).map_err(|error| {
                Error::Allocation(format!("failed to grow the clip boxes: {error}"))
            })?;
        }
        self.clip_boxes.push(bbox);
        Ok(())
    }

    fn push_clip_entry(
        &mut self,
        segments: Segments,
        even_odd: bool,
        bbox: Rect,
    ) -> Result<(), Error> {
        self.charge(1 + segments.count())?;
        let index = index_of(self.clips.len())?;
        push(
            &mut self.clips,
            (segments.kinds, segments.coords, even_odd, bbox_tuple(bbox)),
            "clips",
        )?;
        push(&mut self.clip_stack, index, "the clip stack")?;
        self.state = None;
        Ok(())
    }

    fn push_group(
        &mut self,
        opacity: f32,
        mask: Option<&SoftMask<'_>>,
        blend: BlendMode,
    ) -> Result<(), Error> {
        self.charge(1)?;
        let index = index_of(self.groups.len())?;
        push(
            &mut self.groups,
            (f64::from(opacity), blend_code(blend), soft_mask_code(mask)),
            "transparency groups",
        )?;
        push(&mut self.group_stack, index, "the group stack")?;
        self.state = None;
        Ok(())
    }

    fn begin_mark(&mut self, tag: &[u8], mcid: Option<i32>) -> Result<(), Error> {
        self.charge(1)?;
        let mut name = String::new();
        let text = String::from_utf8_lossy(tag);
        #[expect(
            clippy::disallowed_methods,
            reason = "the tag of one sequence, a name of the content, which is charged above"
        )]
        name.try_reserve_exact(text.len()).map_err(|error| {
            Error::Allocation(format!("failed to allocate a marked-content tag: {error}"))
        })?;
        #[expect(
            clippy::disallowed_methods,
            reason = "fills the capacity reserved above"
        )]
        name.push_str(&text);
        let index = index_of(self.marks.len())?;
        push(&mut self.marks, (name, mcid), "marked content")?;
        push(&mut self.mark_stack, index, "the marked-content stack")?;
        self.state = None;
        Ok(())
    }

    /// Return the items that meet `clip`, or all of them, and the sink.
    /// Describes the text runs and their fonts on `faces`. Faces that show
    /// other glyphs, as hayro does when one does not load, leave them as they
    /// are.
    fn describe_on(&mut self, faces: FaceGlyphs<'_>) -> Result<(), Error> {
        let FaceGlyphs {
            glyphs,
            mut fonts,
            error,
            ..
        } = faces;
        if let Some(error) = error {
            return Err(error);
        }
        if glyphs.len() != self.shown {
            return Ok(());
        }
        for record in &mut self.records {
            if let Record::Text(run) = record {
                run.describe_on(&glyphs[run.first_shown..]);
            }
        }
        for (key, index) in &self.font_indexes {
            if let Some((face_name, weight, is_italic, is_serif, is_monospace)) = fonts.remove(key)
            {
                let entry = &mut self.fonts[*index as usize];
                if entry.0.is_empty() && !face_name.is_empty() {
                    self.budget
                        .charge(LimitCode::MemorySize, face_name.len() as u64)?;
                    entry.0 = face_name;
                }
                entry.2 = weight;
                entry.3 = is_italic;
                entry.4 = is_serif;
                entry.5 = is_monospace;
            }
        }
        Ok(())
    }

    pub(crate) fn finish(mut self, clip: Option<BBox>) -> Result<PageContent, Error> {
        if let Some(error) = self.error {
            return Err(error);
        }
        let mut kept_records = with_capacity(self.records.len(), "content items")?;
        let mut dropped_images = false;
        for record in self.records {
            if clip.is_some_and(|clip| !meets(record.bbox(&self.paths), clip)) {
                if matches!(record, Record::Image(_)) {
                    dropped_images = true;
                }
                continue;
            }
            #[expect(
                clippy::disallowed_methods,
                reason = "fills the capacity reserved above: one item per record at most"
            )]
            kept_records.push(record);
        }
        if let Some(clip) = clip {
            let image_count = kept_records
                .iter()
                .filter(|record| matches!(record, Record::Image(_)))
                .count();
            let mut image_patterns = with_capacity(image_count, PATH_COLUMNS)?;
            let mut image_inlines = with_capacity(image_count, PATH_COLUMNS)?;
            for record in &kept_records {
                if let Record::Image(image) = record {
                    let pattern = match image.fill {
                        Some(ImageFill::Pattern(index)) => Some(index),
                        _ => None,
                    };
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "fills the capacity reserved above: one entry per kept image"
                    )]
                    image_patterns.push(pattern);
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "fills the capacity reserved above: one entry per kept image"
                    )]
                    image_inlines.push(image.inline_index);
                }
            }
            self.paths.retain_meeting(
                clip,
                &mut image_patterns,
                &mut image_inlines,
                dropped_images,
            )?;
            let mut image_cursor = 0usize;
            for record in &mut kept_records {
                if let Record::Image(image) = record {
                    if let Some(ImageFill::Pattern(index)) = &mut image.fill
                        && let Some(remapped) = image_patterns[image_cursor]
                    {
                        *index = remapped;
                    }
                    image.inline_index = image_inlines[image_cursor];
                    image_cursor += 1;
                }
            }
        }
        self.paths.seal()?;
        let mut items = with_capacity(kept_records.len(), "content items")?;
        let mut path_row = 0;
        for record in kept_records {
            let item = record.into_item(path_row, &self.paths.patterns);
            if matches!(item, ContentItem::Path(_)) {
                path_row += 1;
            }
            #[expect(
                clippy::disallowed_methods,
                reason = "fills the capacity reserved above: one item per kept record"
            )]
            items.push(item);
        }
        let content = PageContent {
            items,
            paths: Arc::new(self.paths),
            fonts: self.fonts,
            contexts: Arc::new(Contexts {
                clips: self.clips,
                groups: self.groups,
                marks: self.marks,
                states: self.states,
            }),
        };
        Ok(content)
    }
}

/// Strips a six-uppercase-letter subset tag (`ABCDEF+`, ISO 32000-2 §9.9.2).
fn strip_subset_prefix(name: &str) -> &str {
    let bytes = name.as_bytes();
    if bytes.len() > 7 && bytes[6] == b'+' && bytes[..6].iter().all(u8::is_ascii_uppercase) {
        &name[7..]
    } else {
        name
    }
}

/// Whether `font` has an embedded font program stream in its `/FontDescriptor`.
fn dict_has_font_file(font: &Dict<'_>) -> bool {
    font.get::<Dict<'_>>(b"FontDescriptor")
        .is_some_and(|descriptor| {
            [b"FontFile".as_slice(), b"FontFile2", b"FontFile3"]
                .iter()
                .any(|key| descriptor.get::<Stream<'_>>(key).is_some())
        })
}

/// Whether the PDF font dictionary embeds an external font program stream.
/// Type 3 fonts draw glyph procedures from `/CharProcs` and have no font file.
fn font_dict_is_embedded(dict: &Dict<'_>, subtype: &str) -> bool {
    match subtype {
        "Type0" => dict
            .get::<Array<'_>>(b"DescendantFonts")
            .and_then(|array| array.iter::<Dict<'_>>().next())
            .is_some_and(|descendant| dict_has_font_file(&descendant)),
        "Type3" => false,
        _ => dict_has_font_file(dict),
    }
}

/// Reads PDF object identity (`xref`, `/Subtype`, `/BaseFont` or Type 3 `/Name`,
/// and embedded status) from `font_dict`, charging allocated strings to `budget`.
///
/// `hayro_syntax::Dict::obj_id()` inherits the enclosing indirect object's ID
/// for direct sub-dictionaries, so `xref` is reported only when resolving that
/// object number yields the exact same dictionary bytes.
fn font_dict_identity(
    font_dict: Option<&Dict<'_>>,
    page_xref: &XRef,
    needs_base_name: bool,
    budget: &Budget,
) -> Result<(Option<u32>, String, String, bool), Error> {
    let Some(dict) = font_dict else {
        return Ok((None, String::new(), String::new(), false));
    };
    let xref = dict
        .obj_id()
        .filter(|&id| {
            page_xref
                .get::<Dict<'_>>(id)
                .is_some_and(|indirect| indirect.data() == dict.data())
        })
        .map(|id| id.obj_number);
    let subtype_name = dict.get::<Name<'_>>(b"Subtype");
    let subtype_str = subtype_name.as_ref().map_or("", Name::as_str);
    let subtype = fallible_string(subtype_str, "font subtype", budget)?;
    let base_name = if needs_base_name {
        let base_font = dict.get::<Name<'_>>(b"BaseFont").or_else(|| {
            (subtype_str == "Type3")
                .then(|| dict.get::<Name<'_>>(b"Name"))
                .flatten()
        });
        let base_str = base_font
            .as_ref()
            .map_or("", |name| strip_subset_prefix(name.as_str()));
        fallible_string(base_str, "font base name", budget)?
    } else {
        String::new()
    };
    let is_embedded = font_dict_is_embedded(dict, subtype_str);
    Ok((xref, subtype, base_name, is_embedded))
}

/// Program-derived metrics of an outline glyph.
fn face_font_metrics(glyph: &OutlineGlyph) -> FaceFontMetrics {
    match glyph.font_data() {
        Some(data) => (
            data.postscript_name.unwrap_or_default(),
            data.weight,
            data.is_italic,
            data.is_serif,
            data.is_monospace,
        ),
        None => (String::new(), None, false, false, false),
    }
}

/// The font of an outline glyph combined with its PDF font dictionary identity.
fn outline_font_tuple(
    glyph: &OutlineGlyph,
    font_dict: Option<&Dict<'_>>,
    page_xref: &XRef,
    budget: &Budget,
) -> Result<FontTuple, Error> {
    let (program_name, weight, is_italic, is_serif, is_monospace) = face_font_metrics(glyph);
    let (xref, subtype, base_name, is_embedded) =
        font_dict_identity(font_dict, page_xref, program_name.is_empty(), budget)?;
    let name = if program_name.is_empty() {
        base_name
    } else {
        budget.charge(LimitCode::MemorySize, program_name.len() as u64)?;
        program_name
    };
    Ok((
        name,
        false,
        weight,
        is_italic,
        is_serif,
        is_monospace,
        xref,
        subtype,
        is_embedded,
    ))
}

/// The font of a Type 3 glyph from its PDF font dictionary.
fn type3_font_tuple(
    font_dict: Option<&Dict<'_>>,
    page_xref: &XRef,
    budget: &Budget,
) -> Result<FontTuple, Error> {
    let (xref, subtype, base_name, _) = font_dict_identity(font_dict, page_xref, true, budget)?;
    Ok((
        base_name, true, None, false, false, false, xref, subtype, false,
    ))
}

fn index_of(length: usize) -> Result<u32, Error> {
    u32::try_from(length)
        .map_err(|_| Error::Allocation("content model index overflowed".to_owned()))
}

/// A glyph's Unicode text, or `None` when hayro finds no mapping.
#[expect(
    clippy::disallowed_methods,
    reason = "one character, four bytes at most"
)]
fn glyph_text(glyph: &Glyph<'_>) -> Result<Option<String>, Error> {
    Ok(match glyph.as_unicode() {
        None => None,
        Some(BfString::Char(character)) => {
            let mut text = String::new();
            text.try_reserve_exact(character.len_utf8())
                .map_err(|error| {
                    Error::Allocation(format!("failed to allocate glyph text: {error}"))
                })?;
            text.push(character);
            Some(text)
        }
        Some(BfString::String(text)) => Some(text),
    })
}

impl<'a> Device<'a> for ContentCollector<'a, '_, '_, '_> {
    fn draw_path(&mut self, path: &BezPath, props: DrawProps<'a>, mode: &DrawMode) {
        let pending = self.pending.take();
        self.sink.path(path, props.transform, mode);
        if !self.kinds.paths {
            return;
        }
        self.run(|this| match mode {
            DrawMode::Fill(rule) => this.record_path(path, &props, Some(*rule), None, None),
            DrawMode::Stroke(stroke) => this.record_path(path, &props, None, Some(stroke), pending),
            DrawMode::FillAndStroke(rule, stroke) => {
                this.record_path(path, &props, Some(*rule), Some(stroke), None)
            }
            DrawMode::Invisible => Ok(()),
        });
    }

    fn push_clip_path(&mut self, clip: &ClipPath) {
        self.pending = None;
        self.run(|this| this.push_clip_box(clip));
        if self.kinds.any() {
            self.run(|this| this.push_clip(clip));
        }
    }

    fn push_transparency_group(
        &mut self,
        opacity: f32,
        mask: Option<SoftMask<'a>>,
        blend: BlendMode,
    ) {
        self.pending = None;
        if self.kinds.any() {
            self.run(|this| this.push_group(opacity, mask.as_ref(), blend));
        }
    }

    fn draw_glyph_run(&mut self, run: &GlyphRun<'_, 'a>, props: DrawProps<'a>, mode: &DrawMode) {
        let pending = self.pending.take();
        let ordinal = self.runs;
        self.runs = self.runs.saturating_add(1);
        self.run(|this| this.record_text(run, &props, mode, pending, ordinal));
    }

    fn draw_image(&mut self, image: Image<'a, '_>, props: ImageDrawProps<'a>) {
        self.pending = None;
        if self.kinds.images {
            self.run(|this| this.record_image(&image, &props));
        }
    }

    fn pop_clip(&mut self) {
        self.pending = None;
        self.clip_boxes.pop();
        self.clip_stack.pop();
        self.state = None;
    }

    fn pop_transparency_group(&mut self) {
        self.pending = None;
        self.group_stack.pop();
        self.state = None;
    }

    fn begin_marked_content(&mut self, tag: &[u8], properties: Option<&Dict<'_>>) {
        self.pending = None;
        if self.kinds.any() {
            let mcid = properties.and_then(|properties| properties.get::<i32>(MCID));
            self.run(|this| this.begin_mark(tag, mcid));
        }
        let wants_actual = self.sink.wants_actual_text();
        let actual_text = if self.kinds.any() || wants_actual {
            properties.and_then(|properties| properties.get::<PdfString>(ACTUAL_TEXT))
        } else {
            None
        };
        if self.kinds.any() && actual_text.is_some() && self.actual_text_level.is_none() {
            self.actual_text_level = Some(self.mark_stack.len());
        }
        self.sink.begin_marked_content(
            wants_actual
                .then(|| actual_text.as_ref().map(PdfString::as_bytes))
                .flatten(),
        );
    }

    fn end_marked_content(&mut self) {
        self.pending = None;
        if self.actual_text_level == Some(self.mark_stack.len()) {
            self.actual_text_level = None;
        }
        if self.mark_stack.pop().is_some() {
            self.state = None;
        }
        self.sink.end_marked_content();
    }
}

/// Interprets one page, feeding `sink`, and collects its content. `cache`
/// keeps the fonts and objects that hayro reads for another interpretation
/// of the page.
#[expect(
    clippy::too_many_arguments,
    reason = "one interpretation and what it collects"
)]
pub(crate) fn collect_page_content<'a, S: ContentSink>(
    page: &Page<'a>,
    display: Display,
    settings: InterpreterSettings,
    cache: &InterpreterCache<'a>,
    sink: S,
    kinds: ItemKinds,
    clip: Option<BBox>,
    budget: &Budget,
) -> Result<(PageContent, S), Error> {
    let stretches = Stretches::new(&settings);
    collect_page_content_with_stretches(
        page, display, settings, cache, &stretches, sink, kinds, clip, budget,
    )
}

/// Interprets one page, feeding `sink`, reusing both `cache` and `stretches`.
#[expect(
    clippy::too_many_arguments,
    reason = "one interpretation and what it collects"
)]
pub(crate) fn collect_page_content_with_stretches<'a, S: ContentSink>(
    page: &Page<'a>,
    display: Display,
    settings: InterpreterSettings,
    cache: &InterpreterCache<'a>,
    stretches: &Stretches<'a>,
    mut sink: S,
    kinds: ItemKinds,
    clip: Option<BBox>,
    budget: &Budget,
) -> Result<(PageContent, S), Error> {
    let content = collect_page_content_with_stretches_dyn(
        page, display, settings, cache, stretches, &mut sink, kinds, clip, budget,
    )?;
    Ok((content, sink))
}

#[expect(
    clippy::too_many_arguments,
    reason = "one interpretation and what it collects"
)]
fn collect_page_content_with_stretches_dyn<'a>(
    page: &Page<'a>,
    display: Display,
    settings: InterpreterSettings,
    cache: &InterpreterCache<'a>,
    stretches: &Stretches<'a>,
    sink: &mut dyn ContentSink,
    kinds: ItemKinds,
    clip: Option<BBox>,
    budget: &Budget,
) -> Result<PageContent, Error> {
    let mut context = extraction_context(page, display, cache, settings);
    let mut collector =
        ContentCollector::new(sink, kinds, stretches, page.xref(), display.size(), budget);
    interpret_page(
        page,
        &mut context,
        &mut crate::render::DynDevice(&mut collector),
    );
    collector.finish(clip)
}

/// Returns what [`collect_page_content`] returns for what `render_content`
/// selects, with the text runs of a font that the document does not embed
/// described as the face of `fallback` that draws it: their glyph IDs, ink
/// boxes, and fonts.
///
/// Everything else comes from an interpretation without faces. hayro keeps the
/// stretch of a face's glyph to its PDF width in the glyph's transform, so the
/// matrices and the advances keep what the page gives without faces, as the
/// exact boxes of text do. Only text runs read the faces, and only when one of
/// them draws a font of the page, in an interpretation that records no items
/// and so counts no elements. A clip keeps the items by their boxes, so it
/// applies after the faces have moved them.
#[expect(clippy::too_many_arguments, reason = "the options of Page.content")]
pub(crate) fn collect_page_content_on_faces<'a, S: ContentSink>(
    page: &Page<'a>,
    display: Display,
    fallback: &Fallback,
    render_content: RenderContent,
    mut sink: S,
    kinds: ItemKinds,
    clip: Option<BBox>,
    budget: &Budget,
) -> Result<(PageContent, S), Error> {
    let content = collect_page_content_on_faces_dyn(
        page,
        display,
        fallback,
        render_content,
        &mut sink,
        kinds,
        clip,
        budget,
    )?;
    Ok((content, sink))
}

#[expect(clippy::too_many_arguments, reason = "the options of Page.content")]
fn collect_page_content_on_faces_dyn<'a>(
    page: &Page<'a>,
    display: Display,
    fallback: &Fallback,
    render_content: RenderContent,
    sink: &mut dyn ContentSink,
    kinds: ItemKinds,
    clip: Option<BBox>,
    budget: &Budget,
) -> Result<PageContent, Error> {
    let asked = Arc::new(AtomicBool::new(false));
    let settings = InterpreterSettings {
        render_content,
        ..if kinds.text_runs {
            fallback.watching(&asked)
        } else {
            InterpreterSettings::default()
        }
    };
    let cache = InterpreterCache::new();
    let stretches = Stretches::new(&settings);
    let mut context = extraction_context(page, display, &cache, settings);
    let mut collector =
        ContentCollector::new(sink, kinds, &stretches, page.xref(), display.size(), budget);
    interpret_page(
        page,
        &mut context,
        &mut crate::render::DynDevice(&mut collector),
    );
    if collector.error.is_none() && asked.load(Ordering::Relaxed) {
        let settings = InterpreterSettings {
            render_content,
            ..fallback.settings()
        };
        let faces = FaceGlyphs {
            glyphs: BudgetVec::new(budget),
            fonts: HashMap::new(),
            font: None,
            error: None,
        };
        let (_, faces) = collect_page_content(
            page,
            display,
            settings,
            &InterpreterCache::new(),
            faces,
            ItemKinds::NONE,
            None,
            budget,
        )?;
        collector.describe_on(faces)?;
    }
    collector.finish(clip)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bridge::Bridged;
    use crate::extract::{ItemKinds, page_content};
    use crate::render::Fallback;

    fn shading_pdf(pattern_matrix: &str) -> Vec<u8> {
        let content = "/Pattern cs /P1 scn 0 0 200 100 re f";
        let objects = [
            "<< /Type /Catalog /Pages 2 0 R >>".to_owned(),
            "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>".to_owned(),
            "<< /Type /Page /Parent 2 0 R /Contents 4 0 R \
             /Resources << /Pattern << /P1 5 0 R >> >> >>"
                .to_owned(),
            format!(
                "<< /Length {} >>\nstream\n{content}\nendstream",
                content.len()
            ),
            format!(
                "<< /Type /Pattern /PatternType 2 /Matrix [{pattern_matrix}] \
                 /Shading << /ShadingType 2 /ColorSpace /DeviceRGB /Coords [0 0 100 0] \
                 /BBox [10 10 90 90] /Function << /FunctionType 2 /Domain [0 1] \
                 /C0 [1 0 0] /C1 [0 0 1] /N 1 >> >> >>"
            ),
        ];
        let mut out = b"%PDF-1.7\n".to_vec();
        let mut offsets = Vec::new();
        for (at, body) in objects.iter().enumerate() {
            offsets.push(out.len());
            out.extend_from_slice(format!("{} 0 obj\n{body}\nendobj\n", at + 1).as_bytes());
        }
        let xref = out.len();
        out.extend_from_slice(
            format!("xref\n0 {}\n0000000000 65535 f \n", objects.len() + 1).as_bytes(),
        );
        for offset in offsets {
            out.extend_from_slice(format!("{offset:010} 00000 n \n").as_bytes());
        }
        out.extend_from_slice(
            format!(
                "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n",
                objects.len() + 1
            )
            .as_bytes(),
        );
        out
    }

    #[test]
    fn sample_shading_returns_none_bounds_for_singular_pattern_matrices() {
        let budget = Budget::default();
        let kinds = ItemKinds {
            text_runs: false,
            paths: true,
            images: false,
        };
        for singular in ["0 0 0 0 0 0", "1 0 0 0 10 20", "1 1 1 1 0 0"] {
            let bridged = Bridged::of_bytes(shading_pdf(singular));
            let content = page_content(
                &bridged,
                &Fallback::default(),
                0,
                kinds,
                true,
                None,
                &budget,
            )
            .unwrap();
            assert_eq!(content.paths.rows(), 1, "matrix={singular}");
            let sampled = content.paths.sample_shading(0, &budget).unwrap().unwrap();
            assert!(
                sampled.4.is_none(),
                "singular matrix {singular} produced non-None bounds: {:?}",
                sampled.4
            );
        }

        // An invertible matrix still recovers its finite shading-space BBox.
        let bridged = Bridged::of_bytes(shading_pdf("1 0 0 1 0 0"));
        let content = page_content(
            &bridged,
            &Fallback::default(),
            0,
            kinds,
            true,
            None,
            &budget,
        )
        .unwrap();
        let sampled = content.paths.sample_shading(0, &budget).unwrap().unwrap();
        assert_eq!(sampled.4, Some((10.0, 10.0, 90.0, 90.0)));

        // Direct path_bbox rejects non-finite inverses as well.
        let mut rect = BezPath::new();
        rect.move_to((0.0, 0.0));
        rect.line_to((10.0, 10.0));
        assert!(path_bbox(&rect, Affine::new([0.0; 6]).inverse()).is_none());
    }

    #[test]
    fn stored_color_spaces_and_memory_budget_are_reported() {
        let icc = include_bytes!("../../../../tests/assets/print-exchange/virtual-gray.icc");
        let mut icc_hex = String::with_capacity(icc.len() * 2 + 1);
        for &byte in icc {
            use std::fmt::Write as _;
            let _ = write!(&mut icc_hex, "{byte:02X}");
        }
        icc_hex.push('>');

        let content = concat!(
            "0.25 g 0 0 10 10 re f ",
            "0.2 0.4 0.6 rg 10 0 10 10 re f ",
            "0.1 0.2 0.3 0.4 k 20 0 10 10 re f ",
            "/CS_ICC cs 0.5 scn 30 0 10 10 re f ",
            "/CS_CALG cs 0.75 scn 40 0 10 10 re f ",
            "/CS_CALR cs 0.1 0.5 0.9 scn 50 0 10 10 re f ",
            "/CS_LAB cs 50 10 -20 scn 60 0 10 10 re f ",
            "/CS_IDX cs 1 scn 70 0 10 10 re f ",
            "/CS_SEP cs 0.6 scn 80 0 10 10 re f ",
            "/CS_DEVN cs 0.25 0.75 scn 90 0 10 10 re f",
        );
        let objects = [
            "<< /Type /Catalog /Pages 2 0 R >>".to_owned(),
            "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>".to_owned(),
            "<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << /ColorSpace << \
             /CS_ICC [/ICCBased 5 0 R] \
             /CS_CALG [/CalGray << /WhitePoint [0.9505 1.0 1.089] /Gamma 2.2 >>] \
             /CS_CALR [/CalRGB << /WhitePoint [0.9505 1.0 1.089] >>] \
             /CS_LAB [/Lab << /WhitePoint [0.9505 1.0 1.089] /Range [-100 100 -100 100] >>] \
             /CS_IDX [/Indexed /DeviceRGB 1 <FF000000FF00>] \
             /CS_SEP [/Separation /SpotRed /DeviceRGB 6 0 R] \
             /CS_DEVN [/DeviceN [/Cyan /Spot] /DeviceRGB 7 0 R] \
             >> >> >>"
                .to_owned(),
            format!(
                "<< /Length {} >>\nstream\n{content}\nendstream",
                content.len()
            ),
            format!(
                "<< /N 1 /Filter /ASCIIHexDecode /Length {} >>\nstream\n{icc_hex}\nendstream",
                icc_hex.len()
            ),
            "<< /FunctionType 2 /Domain [0 1] /C0 [1 1 1] /C1 [1 0 0] /N 1 >>".to_owned(),
            "<< /FunctionType 4 /Domain [0 1 0 1] /Range [0 1 0 1 0 1] /Length 17 >>\nstream\n{ 1 exch sub 0 } \nendstream"
                .to_owned(),
        ];
        let mut out = b"%PDF-1.7\n".to_vec();
        let mut offsets = Vec::new();
        for (at, body) in objects.iter().enumerate() {
            offsets.push(out.len());
            out.extend_from_slice(format!("{} 0 obj\n{body}\nendobj\n", at + 1).as_bytes());
        }
        let xref = out.len();
        out.extend_from_slice(
            format!("xref\n0 {}\n0000000000 65535 f \n", objects.len() + 1).as_bytes(),
        );
        for offset in offsets {
            out.extend_from_slice(format!("{offset:010} 00000 n \n").as_bytes());
        }
        out.extend_from_slice(
            format!(
                "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n",
                objects.len() + 1
            )
            .as_bytes(),
        );

        let bridged = Bridged::of_bytes(out);
        let kinds = ItemKinds {
            text_runs: false,
            paths: true,
            images: false,
        };
        let content = page_content(
            &bridged,
            &Fallback::default(),
            0,
            kinds,
            true,
            None,
            &Budget::default(),
        )
        .unwrap();
        assert_eq!(content.paths.rows(), 10);
        let expected_spaces = [
            COLOR_SPACE_DEVICE_GRAY,
            COLOR_SPACE_DEVICE_RGB,
            COLOR_SPACE_DEVICE_CMYK,
            COLOR_SPACE_ICC_BASED,
            COLOR_SPACE_CAL_GRAY,
            COLOR_SPACE_CAL_RGB,
            COLOR_SPACE_LAB,
            COLOR_SPACE_INDEXED,
            COLOR_SPACE_SEPARATION,
            COLOR_SPACE_DEVICE_N,
        ];
        let actual_spaces: Vec<u8> = (0..content.paths.rows())
            .map(|row| content.paths.tuple(row).3.unwrap().2.unwrap().0)
            .collect();
        assert_eq!(actual_spaces, expected_spaces);

        // A tight memory budget refuses stored color allocation cleanly.
        let limits = pyropdf_store::budget::DocumentLimits {
            max_memory_size: Some(16),
            ..Default::default()
        };
        let tight = Budget::new(&limits);
        let err =
            page_content(&bridged, &Fallback::default(), 0, kinds, true, None, &tight).unwrap_err();
        assert!(matches!(
            err,
            Error::Limit(refusal) if refusal.code() == LimitCode::MemorySize
        ));
    }

    #[test]
    fn tiling_cells_separate_fill_stroke_and_bound_budget() {
        let content = "/PCS cs 0 0 1 /P1 scn /PCS CS 1 0 0 /P1 SCN 4 w 10 10 80 40 re B";
        let pattern_body = "0 0 5 5 re f";
        let objects = [
            "<< /Type /Catalog /Pages 2 0 R >>".to_owned(),
            "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>".to_owned(),
            "<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << \
             /ColorSpace << /PCS [/Pattern /DeviceRGB] >> /Pattern << /P1 5 0 R >> >> >>"
                .to_owned(),
            format!(
                "<< /Length {} >>\nstream\n{content}\nendstream",
                content.len()
            ),
            format!(
                "<< /Type /Pattern /PatternType 1 /PaintType 2 /TilingType 1 \
                 /BBox [0 0 10 10] /XStep 10 /YStep 10 /Resources << >> /Length {} >>\nstream\n{pattern_body}\nendstream",
                pattern_body.len()
            ),
        ];
        let mut out = b"%PDF-1.7\n".to_vec();
        let mut offsets = Vec::new();
        for (at, body) in objects.iter().enumerate() {
            offsets.push(out.len());
            out.extend_from_slice(format!("{} 0 obj\n{body}\nendobj\n", at + 1).as_bytes());
        }
        let xref = out.len();
        out.extend_from_slice(
            format!("xref\n0 {}\n0000000000 65535 f \n", objects.len() + 1).as_bytes(),
        );
        for offset in offsets {
            out.extend_from_slice(format!("{offset:010} 00000 n \n").as_bytes());
        }
        out.extend_from_slice(
            format!(
                "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n",
                objects.len() + 1
            )
            .as_bytes(),
        );

        let bridged = Bridged::of_bytes(out);
        let kinds = ItemKinds {
            text_runs: false,
            paths: true,
            images: false,
        };
        let content = page_content(
            &bridged,
            &Fallback::default(),
            0,
            kinds,
            true,
            None,
            &Budget::default(),
        )
        .unwrap();
        // Uncolored tiling pattern used for fill (blue) and stroke (red) creates two distinct cells.
        assert_eq!(content.paths.patterns.len(), 2);
        let fill_cell = content.paths.tiling_cell(0).unwrap();
        let stroke_cell = content.paths.tiling_cell(1).unwrap();
        assert!(fill_cell.refusal.is_none());
        assert!(stroke_cell.refusal.is_none());
        let TilingCellOp::Path {
            fill: Some(CellPaint::Color(fill_rgba)),
            ..
        } = &fill_cell.ops[0]
        else {
            panic!("expected colored path in fill tiling cell");
        };
        let TilingCellOp::Path {
            fill: Some(CellPaint::Color(stroke_rgba)),
            ..
        } = &stroke_cell.ops[0]
        else {
            panic!("expected colored path in stroke tiling cell");
        };
        assert!((fill_rgba[2] - 1.0).abs() < 1e-4 && fill_rgba[0].abs() < 1e-4);
        assert!((stroke_rgba[0] - 1.0).abs() < 1e-4 && stroke_rgba[2].abs() < 1e-4);

        // Tight element count refuses cell capture cleanly.
        let tight = Budget::default().with_limit(LimitCode::ContentElementCount, Some(2));
        let err =
            page_content(&bridged, &Fallback::default(), 0, kinds, true, None, &tight).unwrap_err();
        assert!(matches!(
            err,
            Error::Limit(refusal) if refusal.code() == LimitCode::ContentElementCount
        ));
    }

    #[test]
    fn stored_color_key_uses_inline_storage_and_normalizes_negative_zero() {
        let pos = StoredColorKey::from_components(COLOR_SPACE_DEVICE_CMYK, &[0.0, 0.25, 0.5, 1.0])
            .unwrap();
        let neg = StoredColorKey::from_components(COLOR_SPACE_DEVICE_CMYK, &[-0.0, 0.25, 0.5, 1.0])
            .unwrap();
        assert_eq!(pos, neg);
        assert_eq!(pos.heap_bytes(), 0);
        assert!(matches!(pos, StoredColorKey::Inline { len: 4, .. }));

        // 5-component DeviceN color spills cleanly to heap storage.
        let devn =
            StoredColorKey::from_components(COLOR_SPACE_DEVICE_N, &[-0.0, 0.2, 0.4, 0.6, 0.8])
                .unwrap();
        let devn_pos =
            StoredColorKey::from_components(COLOR_SPACE_DEVICE_N, &[0.0, 0.2, 0.4, 0.6, 0.8])
                .unwrap();
        assert_eq!(devn, devn_pos);
        assert_eq!(devn.heap_bytes(), 5 * std::mem::size_of::<u32>());
        assert!(matches!(devn, StoredColorKey::Heap { .. }));
    }
}
