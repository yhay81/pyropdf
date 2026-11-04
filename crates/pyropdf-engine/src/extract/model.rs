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
//! hayro computes, and from interpreting Type 3 glyph procedures. The
//! budget's `content_element_count` bounds glyphs, path and clip segments,
//! stroke dash lengths, and items, and every collection grows fallibly, so a
//! refusal returns no partial result.

use std::collections::HashMap;
use std::ops::Range;
use std::sync::Arc;

use hayro::hayro_interpret::font::{Glyph, GlyphRun, OutlineGlyph, Type3Glyph};
use hayro::hayro_interpret::hayro_cmap::BfString;
use hayro::hayro_interpret::pattern::Pattern;
use hayro::hayro_interpret::shading::ShadingType;
use hayro::hayro_interpret::{
    BlendMode, CacheKey, ClipPath, Device, DrawMode, DrawProps, FillRule, Image, ImageDrawProps,
    InterpreterCache, InterpreterSettings, MaskType, Paint, SoftMask, StrokeProps, interpret_page,
};
use hayro::hayro_syntax::object::dict::keys::MCID;
use hayro::hayro_syntax::object::{Array, Dict, Stream};
use hayro::hayro_syntax::page::Page;
use hayro::hayro_syntax::{Filter, Pdf};
use hayro::kurbo::{Affine, BezPath, Cap, Join, PathEl, Point, Rect, Shape};
use pyropdf_store::budget::{Budget, LimitCode};

use super::{
    DEFAULT_IMAGE_RESOLUTION, Error, extraction_context, image_color_space, jpeg_resolution,
};

/// A display-space box: `x0`, `y0`, `x1`, `y1`.
pub type BBox = (f64, f64, f64, f64);
type Coefficients = [f64; 6];

/// A paint: kind 0 is a color (red, green, blue, alpha in sRGB), kind 1 a
/// shading pattern (shading type, or 0 for a shading that hayro could not
/// read, the six matrix coefficients, and opacity), and kind 2 a tiling
/// pattern (the four pattern-space bbox coordinates, x step, y step, the six
/// matrix coefficients, and opacity).
pub type PaintTuple = (u8, Vec<f64>);
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
/// A text run: glyphs, font index, render mode, fill paint, stroke, bbox, and
/// item context.
pub type TextRunTuple = (
    GlyphsTuple,
    u32,
    u8,
    Option<PaintTuple>,
    Option<StrokeTuple>,
    BBox,
    ItemContextTuple,
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
/// unit-square matrix, bbox, and item context.
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
);
/// A font: PostScript name, Type 3 flag, weight, italic, serif, monospace.
pub type FontTuple = (String, bool, Option<u32>, bool, bool, bool);
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

/// Image mask kinds.
const MASK_NONE: u8 = 0;
const MASK_SOFT: u8 = 1;
const MASK_EXPLICIT: u8 = 2;
const MASK_COLOR_KEY: u8 = 3;

/// Glyph outlines kept per interpretation, so that a page reusing many
/// distinct glyphs cannot grow the cache without bound.
const MAX_CACHED_GLYPHS: usize = 65_536;

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
fn reserve_exact<T>(values: &mut Vec<T>, capacity: usize, label: &str) -> Result<(), Error> {
    values
        .try_reserve_exact(capacity.saturating_sub(values.len()))
        .map_err(|error| Error::Allocation(format!("failed to allocate {label}: {error}")))
}

fn with_capacity<T>(capacity: usize, label: &str) -> Result<Vec<T>, Error> {
    let mut values = Vec::new();
    reserve_exact(&mut values, capacity, label)?;
    Ok(values)
}

fn copy_values(values: &[f64], label: &str) -> Result<Vec<f64>, Error> {
    let mut copy = with_capacity(values.len(), label)?;
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
/// without segments.
fn path_bbox(path: &BezPath, transform: Affine) -> Option<Rect> {
    path.segments()
        .map(|segment| (transform * segment).bounding_box())
        .reduce(|bbox, segment| bbox.union(segment))
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

fn paint_tuple(paint: &Paint<'_>) -> Result<PaintTuple, Error> {
    match paint {
        Paint::Color(color) => {
            let [red, green, blue, alpha] = color.to_rgba().components();
            let values = [red, green, blue, alpha].map(f64::from);
            Ok((COLOR, copy_values(&values, "paint values")?))
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
                Ok((1, copy_values(&values, "paint values")?))
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
                Ok((2, copy_values(&values, "paint values")?))
            }
        },
    }
}

/// Whether every value of a paint is finite, so that Python can hold its
/// matrix and box.
fn paint_is_finite(paint: &PaintTuple) -> bool {
    paint.1.iter().all(|value| value.is_finite())
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
    fn new(paint: &Paint<'_>, stroke: &StrokeProps, transform: Affine) -> Result<Self, Error> {
        Ok(Self {
            paint: paint_tuple(paint)?,
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

/// The bits of a pattern's value, with -0.0 read as 0.0, as Python compares
/// them.
fn value_bits(value: f64) -> u64 {
    if value == 0.0 { 0 } else { value.to_bits() }
}

/// A paint in the form of the path columns: a color's sRGB components and
/// alpha, or a pattern's index in [`PathTable::patterns`].
#[derive(Clone, Copy)]
enum PathPaint {
    Color([f32; 4]),
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
        push(&mut self.pattern, -1, PATH_COLUMNS)
    }

    /// Give `row`, which has no paint, `paint`.
    fn set(&mut self, row: usize, paint: PathPaint) {
        self.present[row] = 1;
        match paint {
            PathPaint::Color([red, green, blue, alpha]) => {
                self.red[row] = red;
                self.green[row] = green;
                self.blue[row] = blue;
                self.alpha[row] = alpha;
            }
            PathPaint::Pattern(pattern) => self.pattern[row] = pattern,
        }
    }

    /// The paint of `row` in the tuple form of the content model.
    fn tuple(&self, row: usize, patterns: &[PaintTuple]) -> Option<PaintTuple> {
        if self.present[row] == 0 {
            return None;
        }
        Some(match usize::try_from(self.pattern[row]) {
            Ok(pattern) => patterns[pattern].clone(),
            Err(_) => (
                COLOR,
                [
                    self.red[row],
                    self.green[row],
                    self.blue[row],
                    self.alpha[row],
                ]
                .map(f64::from)
                .to_vec(),
            ),
        })
    }

    /// Keep the rows where `keep` holds.
    fn retain(&mut self, keep: &[bool]) {
        retain_rows(&mut self.present, keep);
        retain_rows(&mut self.red, keep);
        retain_rows(&mut self.green, keep);
        retain_rows(&mut self.blue, keep);
        retain_rows(&mut self.alpha, keep);
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
}

impl PathTable {
    fn new() -> Self {
        Self {
            segment_offsets: vec![0],
            stroke_dashes_offsets: vec![0],
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
    pub fn tuple(&self, row: usize) -> PathTuple {
        (
            self.verb[self.segments_of(row)].to_vec(),
            self.coordinates(row),
            (self.x0[row], self.y0[row], self.x1[row], self.y1[row]),
            self.fill.tuple(row, &self.patterns),
            self.fill_rule[row] != 0,
            self.stroke.tuple(row, &self.patterns).map(|paint| {
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
    /// patterns that they use.
    fn retain_meeting(&mut self, clip: BBox) -> Result<(), Error> {
        let rows = self.rows();
        let mut keep = with_capacity(rows, PATH_COLUMNS)?;
        keep.extend((0..rows).map(|row| meets(self.bbox(row), clip)));
        if keep.iter().all(|&kept| kept) {
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
        self.retain_patterns()
    }

    /// Keep the patterns that the rows use, in the order in which they first
    /// use them.
    fn retain_patterns(&mut self) -> Result<(), Error> {
        let mut patterns = std::mem::take(&mut self.patterns);
        let mut indexes = with_capacity(patterns.len(), PATH_COLUMNS)?;
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
                }
                column[row] = indexes[pattern];
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
    context: ItemContext,
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
    fn into_item(self, path_row: u32) -> ContentItem {
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
                )))
            }
            Self::Path(_) => ContentItem::Path(path_row),
            Self::Image(image) => {
                let image = *image;
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
}

/// The font key of the runs that hayro shows with its fallback font, which
/// no font dictionary describes.
const FALLBACK_FONT_KEY: u128 = 1;

/// The [`GlyphStyle::font`] of a run. Fonts over one program have different
/// keys, although their glyphs' identifiers agree.
fn run_font_key(run: &GlyphRun<'_, '_>) -> u128 {
    run.font_dict()
        .map_or(FALLBACK_FONT_KEY, |dict| dict.cache_key())
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
    /// The stroke of the glyphs of the latest fill run, which completes that
    /// run as fill-and-stroke text; the glyphs are not sent again.
    fn stroke_completes_fill(&mut self);
    /// Receive one path paint callback.
    fn path(&mut self, path: &BezPath, transform: Affine, mode: &DrawMode);
    /// Whether the sink refused more input, so that the collector stops
    /// sending glyphs.
    fn stopped(&self) -> bool;
    /// Receive the image that the collector records as the page's image
    /// `ordinal`, counted from 0 in paint order, when it records images.
    fn image(&mut self, _ordinal: usize, _image: &Image<'_, '_>) -> Result<(), Error> {
        Ok(())
    }
}

/// The sink of `Page.content()`, which reads only the items.
pub(crate) struct NoSink;

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
                    type3.interpret(self, props.transform, glyph.transform(), &props.paint);
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

pub(crate) struct ContentCollector<'b, S> {
    sink: S,
    /// The items to record; with none, the collector only feeds the sink.
    kinds: ItemKinds,
    records: Vec<Record>,
    paths: PathTable,
    /// The index of each pattern in the table, by its kind and the bits of
    /// its values.
    pattern_indexes: HashMap<(u8, Vec<u64>), i32>,
    /// The segments of the path being recorded, in a buffer that is reused.
    segments: Segments,
    /// The segments of the fill path that a stroke may complete.
    pending_segments: Segments,
    fonts: Vec<FontTuple>,
    font_indexes: HashMap<u128, u32>,
    type3_font: Option<u32>,
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
    /// The interned state of the current stacks, or `None` after a change.
    state: Option<u32>,
    pending: Option<Pending>,
    /// The glyph matrices of the fill run that a stroke may complete.
    pending_matrices: Vec<f64>,
    /// The glyph runs that hayro has shown.
    runs: u32,
    outlines: HashMap<u128, Option<GlyphInk>>,
    type3_boxes: HashMap<u128, Option<Rect>>,
    /// The images recorded so far, the ordinal of the next one.
    images: usize,
    budget: &'b Budget,
    error: Option<Error>,
}

impl<'b, S: ContentSink> ContentCollector<'b, S> {
    /// A collector that feeds `sink` and records the items of `kinds` within
    /// `budget`.
    pub(crate) fn new(sink: S, kinds: ItemKinds, budget: &'b Budget) -> Self {
        Self {
            sink,
            kinds,
            records: Vec::new(),
            paths: PathTable::new(),
            pattern_indexes: HashMap::new(),
            segments: Segments::default(),
            pending_segments: Segments::default(),
            fonts: Vec::new(),
            font_indexes: HashMap::new(),
            type3_font: None,
            clips: Vec::new(),
            groups: Vec::new(),
            marks: Vec::new(),
            states: Vec::new(),
            clip_stack: Vec::new(),
            clip_boxes: Vec::new(),
            group_stack: Vec::new(),
            mark_stack: Vec::new(),
            state: None,
            pending: None,
            pending_matrices: Vec::new(),
            runs: 0,
            outlines: HashMap::new(),
            type3_boxes: HashMap::new(),
            images: 0,
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
        clips.extend_from_slice(&self.clip_stack);
        let mut groups = with_capacity(self.group_stack.len(), "context groups")?;
        groups.extend_from_slice(&self.group_stack);
        let mut marks = with_capacity(self.mark_stack.len(), "context marked content")?;
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
    /// with what `glyph`'s program says about it.
    fn outline_font(&mut self, key: u128, glyph: &OutlineGlyph) -> Result<u32, Error> {
        if let Some(index) = self.font_indexes.get(&key) {
            return Ok(*index);
        }
        let font = match glyph.font_data() {
            Some(data) => (
                data.postscript_name.unwrap_or_default(),
                false,
                data.weight,
                data.is_italic,
                data.is_serif,
                data.is_monospace,
            ),
            None => (String::new(), false, None, false, false, false),
        };
        let index = index_of(self.fonts.len())?;
        self.font_indexes.try_reserve(1).map_err(|error| {
            Error::Allocation(format!("failed to grow the font index: {error}"))
        })?;
        push(&mut self.fonts, font, "fonts")?;
        self.font_indexes.insert(key, index);
        Ok(index)
    }

    fn type3_font(&mut self) -> Result<u32, Error> {
        if let Some(index) = self.type3_font {
            return Ok(index);
        }
        let index = index_of(self.fonts.len())?;
        push(
            &mut self.fonts,
            (String::new(), true, None, false, false, false),
            "fonts",
        )?;
        self.type3_font = Some(index);
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
            self.outlines.try_reserve(1).map_err(|error| {
                Error::Allocation(format!("failed to grow the outline cache: {error}"))
            })?;
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
    fn type3_box<'a>(
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
                glyph.interpret(&mut ink, Affine::IDENTITY, Affine::IDENTITY, paint);
                if self.type3_boxes.len() < MAX_CACHED_GLYPHS {
                    self.type3_boxes.try_reserve(1).map_err(|error| {
                        Error::Allocation(format!("failed to grow the Type 3 cache: {error}"))
                    })?;
                    self.type3_boxes.insert(key, ink.bbox);
                }
                ink.bbox
            }
        };
        Ok(bbox.map(|bbox| transformed_rect(bbox, transform)))
    }

    /// The form of `paint` in the path columns, adding a pattern to the table
    /// when it is new, or `None` when a value of the paint is not finite.
    fn path_paint(&mut self, paint: &Paint<'_>) -> Result<Option<PathPaint>, Error> {
        if let Paint::Color(color) = paint {
            let rgba = color.to_rgba().components();
            let finite = rgba.iter().all(|value| value.is_finite());
            return Ok(finite.then_some(PathPaint::Color(rgba)));
        }
        let paint = paint_tuple(paint)?;
        if !paint_is_finite(&paint) {
            return Ok(None);
        }
        let mut bits = with_capacity(paint.1.len(), "paint values")?;
        bits.extend(paint.1.iter().map(|&value| value_bits(value)));
        let key = (paint.0, bits);
        if let Some(&index) = self.pattern_indexes.get(&key) {
            return Ok(Some(PathPaint::Pattern(index)));
        }
        let index = offset(self.paths.patterns.len())?;
        self.pattern_indexes.try_reserve(1).map_err(|error| {
            Error::Allocation(format!("failed to grow the pattern index: {error}"))
        })?;
        push(&mut self.paths.patterns, paint, PATH_COLUMNS)?;
        self.pattern_indexes.insert(key, index);
        Ok(Some(PathPaint::Pattern(index)))
    }

    fn record_path(
        &mut self,
        path: &BezPath,
        props: &DrawProps<'_>,
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
        let Some(paint) = self.path_paint(&props.paint)? else {
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
    fn record_text<'a>(
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
        let font_key = run_font_key(run);
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
                });
            }
            for glyph in glyphs {
                let transform = props.transform * glyph.transform();
                if is_fill {
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
                    self.sink.glyph(glyph, transform, ink);
                }
            }
        }
        if self.kinds.text_runs {
            self.record_text_item(run, font_key, props, mode, completes_fill)?;
        }
        if is_fill {
            self.pending = Some(Pending::Text);
        }
        Ok(())
    }

    fn record_text_item<'a>(
        &mut self,
        run: &GlyphRun<'_, 'a>,
        font_key: u128,
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
            matrices.extend_from_slice(&matrix.as_coeffs());
        }
        if !matrices.iter().all(|value| value.is_finite()) {
            return Ok(());
        }
        let (render_mode, fill, stroke) = match mode {
            DrawMode::Fill(_) => (RENDER_FILL, Some(paint_tuple(&props.paint)?), None),
            DrawMode::Stroke(stroke) => (
                RENDER_STROKE,
                None,
                Some(StrokeRecord::new(&props.paint, stroke, props.transform)?),
            ),
            DrawMode::FillAndStroke(_, stroke) => (
                RENDER_FILL_STROKE,
                Some(paint_tuple(&props.paint)?),
                Some(StrokeRecord::new(&props.paint, stroke, props.transform)?),
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
            Glyph::Outline(outline) => self.outline_font(font_key, outline)?,
            Glyph::Type3(_) => self.type3_font()?,
        };
        let mut ids = with_capacity(glyphs.len(), "glyph IDs")?;
        let mut texts = with_capacity(glyphs.len(), "glyph text")?;
        let mut advances = with_capacity(glyphs.len(), "glyph advances")?;
        let mut boxes = with_capacity(glyphs.len(), "glyph boxes")?;
        let mut ink: Option<Rect> = None;
        let mut extent: Option<Rect> = None;
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
            // Advances are in hayro's 1000-unit glyph space, like outlines.
            let end = width
                .map(|width| combined * Point::new(width, 0.0))
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
        let Some(bbox) = ink.or(extent) else {
            return Ok(());
        };
        let record = TextRunRecord {
            ids,
            texts,
            matrices,
            advances,
            boxes,
            font,
            render_mode,
            fill,
            stroke,
            bbox,
            context,
        };
        push(
            &mut self.records,
            Record::Text(Box::new(record)),
            "content items",
        )
    }

    fn record_image(
        &mut self,
        image: &Image<'_, '_>,
        props: &ImageDrawProps<'_>,
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
        let default_resolution = (DEFAULT_IMAGE_RESOLUTION, DEFAULT_IMAGE_RESOLUTION);
        let record = match image {
            // hayro does not expose the stream of an image mask.
            Image::Stencil(_) => ImageRecord {
                xref: 0,
                format: (width, height, 1, 1, String::new()),
                is_stencil: true,
                mask: MASK_NONE,
                encoded_size: 0,
                resolution: default_resolution,
                matrix: matrix.as_coeffs(),
                bbox,
                context,
            },
            Image::Raster(raster) => {
                let stream = raster.stream();
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
                ImageRecord {
                    xref: stream.obj_id().obj_number,
                    format: (width, height, bits, components, color_space),
                    is_stencil: false,
                    mask,
                    encoded_size: raw.len(),
                    resolution,
                    matrix: matrix.as_coeffs(),
                    bbox,
                    context,
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
    fn push_clip_box(&mut self, clip: &ClipPath) -> Result<(), Error> {
        // A clip that no finite path describes leaves nothing visible.
        let bbox = path_bbox(&clip.path, Affine::IDENTITY)
            .filter(|bbox| rect_is_finite(*bbox))
            .unwrap_or(Rect::ZERO);
        let bbox = self
            .clip_boxes
            .last()
            .map_or(bbox, |outer| outer.intersect(bbox));
        push(&mut self.clip_boxes, bbox, "the clip boxes")
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
        name.try_reserve_exact(text.len()).map_err(|error| {
            Error::Allocation(format!("failed to allocate a marked-content tag: {error}"))
        })?;
        name.push_str(&text);
        let index = index_of(self.marks.len())?;
        push(&mut self.marks, (name, mcid), "marked content")?;
        push(&mut self.mark_stack, index, "the marked-content stack")?;
        self.state = None;
        Ok(())
    }

    /// Return the items that meet `clip`, or all of them, and the sink.
    pub(crate) fn finish(mut self, clip: Option<BBox>) -> Result<(PageContent, S), Error> {
        if let Some(error) = self.error {
            return Err(error);
        }
        let mut items = with_capacity(self.records.len(), "content items")?;
        let mut path_row = 0;
        for record in self.records {
            if clip.is_some_and(|clip| !meets(record.bbox(&self.paths), clip)) {
                continue;
            }
            let item = record.into_item(path_row);
            if matches!(item, ContentItem::Path(_)) {
                path_row += 1;
            }
            items.push(item);
        }
        if let Some(clip) = clip {
            self.paths.retain_meeting(clip)?;
        }
        self.paths.seal()?;
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
        Ok((content, self.sink))
    }
}

fn index_of(length: usize) -> Result<u32, Error> {
    u32::try_from(length)
        .map_err(|_| Error::Allocation("content model index overflowed".to_owned()))
}

/// A glyph's Unicode text, or `None` when hayro finds no mapping.
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

impl<'a, S: ContentSink> Device<'a> for ContentCollector<'_, S> {
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
        let mcid = properties.and_then(|properties| properties.get::<i32>(MCID));
        if self.kinds.any() {
            self.run(|this| this.begin_mark(tag, mcid));
        }
    }

    fn end_marked_content(&mut self) {
        self.pending = None;
        if self.mark_stack.pop().is_some() {
            self.state = None;
        }
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
    pdf: &'a Pdf,
    page: &Page<'a>,
    settings: InterpreterSettings,
    cache: &InterpreterCache<'a>,
    sink: S,
    kinds: ItemKinds,
    clip: Option<BBox>,
    budget: &Budget,
) -> Result<(PageContent, S), Error> {
    let mut context = extraction_context(pdf, page, cache, settings);
    let mut collector = ContentCollector::new(sink, kinds, budget);
    interpret_page(page, &mut context, &mut collector);
    collector.finish(clip)
}
