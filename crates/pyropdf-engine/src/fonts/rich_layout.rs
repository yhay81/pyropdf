//! Bounded multi-style and UAX #9 BiDi paragraph layout (DESIGN.md sections 7 & 9).
//!
//! Lays out mixed Standard 14 (AFM) and embedded OpenType spans—including per-span
//! fallback font chains, mixed font sizes sharing a baseline, UAX #9 P2/P3 base
//! direction resolution, UAX #14 line breaking with UAX #29 emergency grapheme
//! breaks in logical order, per-line UAX #9 L1/L2 visual reordering, alignment
//! (`left`, `center`, `right`, `start`, `end`, `justify`), OpenType `post`/`OS/2`
//! underline and strikethrough rules, and per-visual-run hyperlink hit-boxes.
//!
//! Every input-sized collection uses [`BudgetVec`] and polls [`Budget::poll`],
//! enforcing [`LimitCode::TextInputSize`], [`LimitCode::ShapedGlyphCount`],
//! [`LimitCode::TextLineCount`], [`LimitCode::MemorySize`], deadline, and
//! cancellation under DESIGN.md F6.

use std::fmt;
use std::ops::Range;

use harfrust::{Buffer, Direction, Script, ShapeOptions, ShaperFont};
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use skrifa::raw::TableProvider;
use unicode_bidi::{BidiInfo, Level};
use unicode_script::{Script as UnicodeScript, UnicodeScript as _};

use super::embed::{Face, Measure};
use super::shape::{FallbackSource, ShapeError, ShapingFace, own_characters, script_runs};
use crate::standard::{self, Base14Font, LayoutError, LineMetrics, scan};

#[cfg(test)]
mod tests;

/// How far a width or height may pass its box and still fit it.
const FIT_TOLERANCE: f64 = 1.0e-6;
/// Poll interval for linear scans over input characters and spans.
const POLL_STEP: usize = 256;
/// Default underline offset below baseline in fractions of em (negative = below baseline).
const DEFAULT_UNDERLINE_POS: f64 = -0.1;
/// Default underline stroke thickness in fractions of em.
const DEFAULT_UNDERLINE_THICK: f64 = 0.05;
/// Default strikethrough offset above baseline in fractions of em (positive = above baseline).
const DEFAULT_STRIKEOUT_POS: f64 = 0.3;
/// Default strikethrough stroke thickness in fractions of em.
const DEFAULT_STRIKEOUT_THICK: f64 = 0.05;
/// Minimum decoration thickness in fractions of em so thin font tables remain visible.
const MIN_DECORATION_THICK: f64 = 0.04;

/// Paragraph base direction for UAX #9 bidirectional resolution.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum BaseDirection {
    /// Resolve per paragraph from its first strong directional character (UAX #9 P2/P3),
    /// defaulting to LTR if none is present.
    #[default]
    Auto,
    /// Force left-to-right base embedding level (0).
    Ltr,
    /// Force right-to-left base embedding level (1).
    Rtl,
}

/// Horizontal alignment inside a text box.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Align {
    /// Physical left edge.
    #[default]
    Left,
    /// Horizontally centered.
    Center,
    /// Physical right edge.
    Right,
    /// Stretch inter-word spaces on soft-wrapped lines; align last/hard-broken lines to `Start`.
    Justify,
    /// Direction-aware start edge: physical left for LTR paragraphs, physical right for RTL.
    Start,
    /// Direction-aware end edge: physical right for LTR paragraphs, physical left for RTL.
    End,
}

/// Options controlling rich-text paragraph layout.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct RichLayoutOptions {
    /// Wrapping width in points, or `None` for unwrapped explicit lines (as in `Canvas.text()`).
    pub max_width: Option<f64>,
    /// Available box height in points used to compute [`RichLayout::spare_height`].
    pub max_height: f64,
    /// UAX #9 base direction mode for paragraphs.
    pub base_direction: BaseDirection,
    /// Horizontal alignment within `max_width` (ignored when `max_width` is `None`).
    pub align: Align,
    /// Optional line-height multiplier in em; `None` uses each line's natural font line height.
    pub line_height: Option<f64>,
}

impl RichLayoutOptions {
    /// Options for a wrapped text box of `(width, height)` points.
    #[must_use]
    pub const fn textbox(width: f64, height: f64) -> Self {
        Self {
            max_width: Some(width),
            max_height: height,
            base_direction: BaseDirection::Auto,
            align: Align::Left,
            line_height: None,
        }
    }

    /// Options for unwrapped explicit lines (such as `Canvas.text()`).
    #[must_use]
    pub const fn unwrapped() -> Self {
        Self {
            max_width: None,
            max_height: 0.0,
            base_direction: BaseDirection::Auto,
            align: Align::Left,
            line_height: None,
        }
    }
}

/// One inline styled span inside a rich-text layout call.
#[derive(Clone, Debug)]
pub struct SpanInput<'a> {
    /// UTF-8 text of this span.
    pub text: &'a str,
    /// Primary face index into the caller's `faces` slice.
    pub face_index: usize,
    /// Ordered fallback face indices into `faces` consulted when `face_index`
    /// lacks a glyph for a grapheme cluster.
    pub fallback_faces: &'a [usize],
    /// Font size in points (must be finite and positive).
    pub size: f64,
    /// Whether to emit underline decoration rules beneath this span's visual runs.
    pub underline: bool,
    /// Whether to emit strikethrough decoration rules across this span's visual runs.
    pub strikethrough: bool,
    /// Optional hyperlink identifier emitted as per-visual-run hit-boxes.
    pub link_id: Option<u32>,
}

/// One shaped glyph emitted in visual left-to-right order inside a [`VisualRun`].
#[derive(Clone, Debug, PartialEq)]
pub struct PositionedGlyph {
    /// Resolved face index in the caller's `faces` slice (primary or fallback).
    pub face_index: usize,
    /// Glyph ID for an embedded OpenType face, or WinAnsi byte for a Standard 14 font.
    pub glyph_id: u32,
    /// Shaped horizontal advance in the resolved face's font units (before justification).
    pub x_advance_units: i32,
    /// Shaped vertical advance in the resolved face's font units.
    pub y_advance_units: i32,
    /// Shaped horizontal offset in the resolved face's font units.
    pub x_offset_units: i32,
    /// Shaped vertical offset in the resolved face's font units.
    pub y_offset_units: i32,
    /// Total horizontal advance in points (including any justification `extra_advance`).
    pub x_advance: f64,
    /// Horizontal placement offset in points.
    pub x_offset: f64,
    /// Vertical placement offset in points (positive upward in text space).
    pub y_offset: f64,
    /// Extra horizontal advance in points added by line justification (0.0 unless justified space).
    pub extra_advance: f64,
    /// Byte range in [`RichLayout::text`] covered by this glyph's shaping cluster (for `/ActualText`).
    pub cluster: Range<usize>,
    /// Byte range in [`RichLayout::text`] mapped by this glyph's `ToUnicode` entry
    /// (empty for non-leading glyphs of a multi-character ligature cluster).
    pub text: Range<usize>,
    /// Whether this glyph belongs to a right-to-left run.
    pub rtl: bool,
}

/// One contiguous visual run on a laid-out line (uniform span style and BiDi direction).
#[derive(Clone, Debug, PartialEq)]
pub struct VisualRun {
    /// Index into the caller's `spans` slice.
    pub span_index: usize,
    /// Primary face index of the span.
    pub face_index: usize,
    /// Font size in points.
    pub size: f64,
    /// Byte range inside [`RichLayout::text`] covered by this visual run.
    pub text_range: Range<usize>,
    /// Whether this run is right-to-left.
    pub rtl: bool,
    /// Left edge x-coordinate in box-local display space (points).
    pub x: f64,
    /// Visual advance width of this run in points.
    pub width: f64,
    /// Glyphs in visual left-to-right drawing order.
    pub glyphs: Vec<PositionedGlyph>,
}

/// A decoration stroke (underline or strikethrough) in box-local display coordinates.
#[derive(Clone, Debug, PartialEq)]
pub struct DecorationRule {
    /// Index into the caller's `spans` slice.
    pub span_index: usize,
    /// `false` for underline, `true` for strikethrough.
    pub is_strikethrough: bool,
    /// Left x-coordinate in box-local display space.
    pub x0: f64,
    /// Right x-coordinate in box-local display space.
    pub x1: f64,
    /// Center y-coordinate in box-local display space (y downward from box top).
    pub y: f64,
    /// Stroke thickness in points.
    pub thickness: f64,
}

/// A hyperlink hit-box in box-local display coordinates (y downward from box top).
#[derive(Clone, Debug, PartialEq)]
pub struct LinkBox {
    /// Caller-supplied link identifier from [`SpanInput::link_id`].
    pub link_id: u32,
    /// Left edge x-coordinate in points.
    pub x0: f64,
    /// Top edge y-coordinate in points (`baseline_y - ascent`).
    pub y0: f64,
    /// Right edge x-coordinate in points.
    pub x1: f64,
    /// Bottom edge y-coordinate in points (`baseline_y + descent`).
    pub y1: f64,
}

/// One laid-out line in visual left-to-right order.
#[derive(Clone, Debug, PartialEq)]
pub struct RichLine {
    /// Y coordinate of the shared baseline below the box top (display space, y downward).
    pub baseline_y: f64,
    /// Maximum ascent above the baseline across all runs on this line (points, >= 0).
    pub ascent: f64,
    /// Maximum descent below the baseline across all runs on this line (points, >= 0).
    pub descent: f64,
    /// Total visual width of the line in points.
    pub width: f64,
    /// Resolved UAX #9 paragraph base direction for this line's paragraph.
    pub is_rtl_paragraph: bool,
    /// Whether inter-word justification was applied to this line.
    pub justified: bool,
    /// Visual runs in left-to-right display order.
    pub runs: Vec<VisualRun>,
}

/// Complete rich-text layout result ready for PDF emission.
#[derive(Clone, Debug, PartialEq)]
pub struct RichLayout {
    /// Concatenated UTF-8 text of all input spans, indexed by every run's `text_range`
    /// and every glyph's `cluster` and `text` byte ranges.
    pub text: String,
    /// Laid-out lines from top to bottom.
    pub lines: Vec<RichLine>,
    /// Underline and strikethrough rules in box-local display coordinates.
    pub decorations: Vec<DecorationRule>,
    /// Hyperlink hit-boxes in box-local display coordinates.
    pub links: Vec<LinkBox>,
    /// Total height required by all lines in points.
    pub height: f64,
    /// Box height minus [`RichLayout::height`]; negative when the text overflows vertically.
    pub spare_height: f64,
    /// Total number of positioned glyphs emitted across all lines.
    pub total_glyphs: usize,
}

impl RichLayout {
    /// Returns whether the laid-out lines fit within `options.max_height`.
    #[must_use]
    pub fn fits(&self) -> bool {
        self.spare_height >= -FIT_TOLERANCE
    }
}

/// Why rich-text layout refused its input.
#[derive(Clone, Debug, PartialEq)]
pub enum RichLayoutError {
    /// Box width/height, font size, line height, or face index is invalid.
    InvalidGeometry,
    /// The box is narrower than a single extended grapheme cluster of the text.
    Narrow,
    /// A Standard 14 span (with no OpenType fallback) holds a character outside WinAnsi.
    NotWinAnsi(char),
    /// No face in a span's face chain has a glyph for `char`.
    MissingGlyph(char),
    /// A paragraph holds more than [`standard::MAX_BREAK_CONTEXT`] unresolved UAX #14 context chars.
    BreakContext,
    /// An embedded OpenType face could not be shaped.
    Font(String),
    /// The operation's budget refused memory, glyph count, line count, input size,
    /// deadline, or cancellation.
    Limit(LimitExceeded),
}

impl From<LimitExceeded> for RichLayoutError {
    fn from(refusal: LimitExceeded) -> Self {
        Self::Limit(refusal)
    }
}

impl From<ShapeError> for RichLayoutError {
    fn from(error: ShapeError) -> Self {
        match error {
            ShapeError::Font(reason) => Self::Font(reason),
            ShapeError::Limit(refusal) => Self::Limit(refusal),
        }
    }
}

impl From<LayoutError> for RichLayoutError {
    fn from(error: LayoutError) -> Self {
        match error {
            LayoutError::BreakContext => Self::BreakContext,
            LayoutError::InvalidGeometry => Self::InvalidGeometry,
            LayoutError::Narrow => Self::Narrow,
            LayoutError::Limit(refusal) => Self::Limit(refusal),
            LayoutError::NotWinAnsi => Self::NotWinAnsi('\u{FFFD}'),
            LayoutError::Lines | LayoutError::Unfit | LayoutError::AppearanceTooLong => {
                Self::InvalidGeometry
            }
        }
    }
}

impl fmt::Display for RichLayoutError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidGeometry => {
                formatter.write_str("rich text geometry and span sizes must be finite and positive")
            }
            Self::Narrow => formatter.write_str("textbox is too narrow to fit one text grapheme"),
            Self::NotWinAnsi(character) => write!(
                formatter,
                "Standard 14 text contains character U+{:04X} ({character:?}) outside WinAnsi",
                u32::from(*character)
            ),
            Self::MissingGlyph(character) => write!(
                formatter,
                "none of the configured fonts contain a glyph for U+{:04X} ({character:?})",
                u32::from(*character)
            ),
            Self::BreakContext => formatter.write_str(
                "text holds too long a run of characters whose line breaks depend on one another",
            ),
            Self::Font(reason) => write!(formatter, "the font cannot be shaped: {reason}"),
            Self::Limit(refusal) => fmt::Display::fmt(refusal, formatter),
        }
    }
}

impl std::error::Error for RichLayoutError {}

/// One atomic shaped cluster in logical paragraph order, referencing a slice
/// inside the paragraph's flat glyph cache so visual runs reuse shaped glyphs
/// without a second shaping pass.
#[derive(Clone, Debug)]
struct ShapedCluster {
    span_index: usize,
    /// Byte range within the paragraph (`0..para.len()`).
    byte_range: Range<usize>,
    /// Total horizontal advance of this cluster in points.
    advance: f64,
    /// Slice into the paragraph's flat glyph vector (glyphs already in visual order).
    glyph_range: Range<usize>,
    /// Whether this cluster was shaped in right-to-left direction.
    rtl: bool,
}

/// Cached decoration metrics for a face, in fractions of em.
#[derive(Clone, Copy, Debug)]
struct DecorationMetrics {
    underline_pos: f64,
    underline_thick: f64,
    strikeout_pos: f64,
    strikeout_thick: f64,
}

/// Reusable face shapers, metrics, and harfrust buffer for one layout call.
struct ShaperPool<'a, 'b> {
    faces: &'a [FallbackSource<'a>],
    shapers: BudgetVec<'b, Option<ShapingFace>>,
    line_metrics: BudgetVec<'b, LineMetrics>,
    decorations: BudgetVec<'b, DecorationMetrics>,
    buffer: Buffer,
    budget: &'b Budget,
    shaped_glyphs: u64,
}

impl<'a, 'b> ShaperPool<'a, 'b> {
    fn new(faces: &'a [FallbackSource<'a>], budget: &'b Budget) -> Result<Self, RichLayoutError> {
        let mut shapers = BudgetVec::new(budget);
        let mut line_metrics = BudgetVec::new(budget);
        let mut decorations = BudgetVec::new(budget);
        shapers.try_reserve_exact(faces.len())?;
        line_metrics.try_reserve_exact(faces.len())?;
        decorations.try_reserve_exact(faces.len())?;
        for &face in faces {
            shapers.try_push(None)?;
            line_metrics.try_push(face_line_metrics(face))?;
            decorations.try_push(face_decoration_metrics(face))?;
        }
        Ok(Self {
            faces,
            shapers,
            line_metrics,
            decorations,
            buffer: Buffer::new(),
            budget,
            shaped_glyphs: 0,
        })
    }

    fn ensure_shaper(&mut self, face_index: usize) -> Result<&ShapingFace, RichLayoutError> {
        if self.shapers[face_index].is_none() {
            let FallbackSource::Embedded(face) = self.faces[face_index] else {
                return Err(RichLayoutError::InvalidGeometry);
            };
            self.shapers[face_index] = Some(ShapingFace::of(face)?);
        }
        Ok(self.shapers[face_index]
            .as_ref()
            .expect("shaping face initialized above"))
    }

    fn check_glyph_budget(&mut self, added: usize) -> Result<(), RichLayoutError> {
        let added = u64::try_from(added).unwrap_or(u64::MAX);
        self.shaped_glyphs = self.shaped_glyphs.saturating_add(added);
        self.budget
            .check(LimitCode::ShapedGlyphCount, self.shaped_glyphs)?;
        Ok(())
    }

    /// Shapes one logical `(face_index, sub_range)` with `script` and `rtl`,
    /// appending its glyphs in visual order to `out_glyphs` and optionally
    /// recording logical-order [`ShapedCluster`] entries into `out_clusters`.
    #[expect(
        clippy::too_many_arguments,
        reason = "segment bounds, span metadata, and output caches"
    )]
    fn shape_face_subrun(
        &mut self,
        para: &str,
        para_offset: usize,
        sub_range: Range<usize>,
        span_index: usize,
        face_index: usize,
        size: f64,
        script: Option<Script>,
        rtl: bool,
        all_standard: bool,
        out_glyphs: &mut BudgetVec<'b, PositionedGlyph>,
        mut out_clusters: Option<&mut BudgetVec<'b, ShapedCluster>>,
    ) -> Result<(), RichLayoutError> {
        self.budget.poll()?;
        if sub_range.is_empty() {
            return Ok(());
        }
        match self.faces[face_index] {
            FallbackSource::Standard(font) => self.shape_standard_subrun(
                font,
                para,
                para_offset,
                sub_range,
                span_index,
                face_index,
                size,
                rtl,
                all_standard,
                out_glyphs,
                out_clusters.as_deref_mut(),
            ),
            FallbackSource::Embedded(face) => self.shape_embedded_subrun(
                face,
                para,
                para_offset,
                sub_range,
                span_index,
                face_index,
                size,
                script,
                rtl,
                out_glyphs,
                out_clusters,
            ),
        }
    }

    #[expect(
        clippy::too_many_arguments,
        reason = "Standard 14 shaping metadata and output caches"
    )]
    fn shape_standard_subrun(
        &mut self,
        font: Base14Font,
        para: &str,
        para_offset: usize,
        sub_range: Range<usize>,
        span_index: usize,
        face_index: usize,
        size: f64,
        rtl: bool,
        all_standard: bool,
        out_glyphs: &mut BudgetVec<'b, PositionedGlyph>,
        out_clusters: Option<&mut BudgetVec<'b, ShapedCluster>>,
    ) -> Result<(), RichLayoutError> {
        let mut entries = BudgetVec::new(self.budget);
        for (index, (offset, character)) in para[sub_range.clone()].char_indices().enumerate() {
            if index % POLL_STEP == 0 {
                self.budget.poll()?;
            }
            let start = sub_range.start + offset;
            let end = start + character.len_utf8();
            entries.try_push((character, start..end))?;
        }
        if rtl {
            entries.reverse();
        }
        self.check_glyph_budget(entries.len())?;
        out_glyphs.try_reserve(entries.len())?;

        let mut local_clusters = BudgetVec::new(self.budget);
        if out_clusters.is_some() {
            local_clusters.try_reserve_exact(entries.len())?;
        }

        for &(character, ref local_range) in entries.iter() {
            let Some(metric) = standard::char_metric(font, character) else {
                return Err(if all_standard {
                    RichLayoutError::NotWinAnsi(character)
                } else {
                    RichLayoutError::MissingGlyph(character)
                });
            };
            let Some(byte) = standard::winansi_byte(character) else {
                return Err(if all_standard {
                    RichLayoutError::NotWinAnsi(character)
                } else {
                    RichLayoutError::MissingGlyph(character)
                });
            };
            #[expect(
                clippy::cast_possible_truncation,
                reason = "Standard 14 AFM character widths are integer thousandths of an em"
            )]
            let x_advance_units = metric.width_x.round() as i32;
            let x_advance = f64::from(metric.width_x) * size / 1000.0;
            let full_range = (para_offset + local_range.start)..(para_offset + local_range.end);
            let glyph_start = out_glyphs.len();
            out_glyphs.try_push(PositionedGlyph {
                face_index,
                glyph_id: u32::from(byte),
                x_advance_units,
                y_advance_units: 0,
                x_offset_units: 0,
                y_offset_units: 0,
                x_advance,
                x_offset: 0.0,
                y_offset: 0.0,
                extra_advance: 0.0,
                cluster: full_range.clone(),
                text: full_range,
                rtl,
            })?;
            if out_clusters.is_some() {
                local_clusters.try_push(ShapedCluster {
                    span_index,
                    byte_range: local_range.clone(),
                    advance: x_advance,
                    glyph_range: glyph_start..(glyph_start + 1),
                    rtl,
                })?;
            }
        }

        if let Some(clusters) = out_clusters {
            if rtl {
                local_clusters.reverse();
            }
            clusters.try_reserve(local_clusters.len())?;
            for cluster in local_clusters.iter() {
                clusters.try_push(cluster.clone())?;
            }
        }
        Ok(())
    }

    #[expect(
        clippy::too_many_arguments,
        reason = "OpenType shaping metadata and output caches"
    )]
    fn shape_embedded_subrun(
        &mut self,
        face: &Face,
        para: &str,
        para_offset: usize,
        sub_range: Range<usize>,
        span_index: usize,
        face_index: usize,
        size: f64,
        script: Option<Script>,
        rtl: bool,
        out_glyphs: &mut BudgetVec<'b, PositionedGlyph>,
        out_clusters: Option<&mut BudgetVec<'b, ShapedCluster>>,
    ) -> Result<(), RichLayoutError> {
        let upem = f64::from(face.units_per_em);
        let scale = size / upem;
        self.ensure_shaper(face_index)?;
        let shaping_face = self.shapers[face_index]
            .as_ref()
            .expect("shaping face initialized above");
        let shaper = ShaperFont::new(shaping_face.harf_font());

        self.buffer.clear();
        self.buffer.push_str(&para[sub_range.clone()]);
        self.buffer.set_direction(if rtl {
            Direction::RightToLeft
        } else {
            Direction::LeftToRight
        });
        self.buffer.set_script(script);
        self.buffer.guess_segment_properties();
        harfrust::shape(&shaper, &mut self.buffer, ShapeOptions::new())
            .map_err(|error| RichLayoutError::Font(error.to_string()))?;
        if !self.buffer.allocation_successful() {
            return Err(RichLayoutError::Font(
                "the face asks for more work than the shaper allows".into(),
            ));
        }

        let infos = self.buffer.glyph_infos();
        let positions = self.buffer.glyph_positions();
        let added = infos.len();
        let new_total = self
            .shaped_glyphs
            .saturating_add(u64::try_from(added).unwrap_or(u64::MAX));
        self.budget.check(LimitCode::ShapedGlyphCount, new_total)?;
        self.shaped_glyphs = new_total;

        if infos.is_empty() {
            if let Some(clusters) = out_clusters {
                let end = out_glyphs.len();
                clusters.try_push(ShapedCluster {
                    span_index,
                    byte_range: sub_range,
                    advance: 0.0,
                    glyph_range: end..end,
                    rtl,
                })?;
            }
            return Ok(());
        }

        let mut starts = BudgetVec::new(self.budget);
        starts.try_reserve_exact(infos.len().saturating_add(1))?;
        for info in infos {
            starts.try_push(sub_range.start + info.cluster as usize)?;
        }
        starts.try_push(sub_range.end)?;
        starts.sort_unstable();
        let first_start = starts.first().copied().unwrap_or(sub_range.start);

        let glyph_base = out_glyphs.len();
        out_glyphs.try_reserve(infos.len())?;

        let mut local_clusters = BudgetVec::new(self.budget);
        let mut first = 0;
        while first < infos.len() {
            let start = sub_range.start + infos[first].cluster as usize;
            let mut last = first;
            while last + 1 < infos.len()
                && sub_range.start + infos[last + 1].cluster as usize == start
            {
                last += 1;
            }
            let end = starts[starts.partition_point(|&at| at <= start)];
            let local_cluster = if start == first_start {
                sub_range.start..end
            } else {
                start..end
            };
            let own = own_characters(&shaper, para, local_cluster.clone(), &infos[first..=last]);
            let full_cluster =
                (para_offset + local_cluster.start)..(para_offset + local_cluster.end);
            let cluster_glyph_start = out_glyphs.len();
            let mut cluster_advance = 0.0_f64;

            for at in first..=last {
                let mapped_local = match &own {
                    Some(own) => own[at - first].clone(),
                    None if (if rtl { at == last } else { at == first }) => local_cluster.clone(),
                    None => local_cluster.start..local_cluster.start,
                };
                let full_text_range =
                    (para_offset + mapped_local.start)..(para_offset + mapped_local.end);
                let position = &positions[at];
                let x_advance = f64::from(position.x_advance) * scale;
                let x_offset = f64::from(position.x_offset) * scale;
                let y_offset = f64::from(position.y_offset) * scale;
                cluster_advance += x_advance;
                out_glyphs.try_push(PositionedGlyph {
                    face_index,
                    glyph_id: infos[at].glyph_id,
                    x_advance_units: position.x_advance,
                    y_advance_units: position.y_advance,
                    x_offset_units: position.x_offset,
                    y_offset_units: position.y_offset,
                    x_advance,
                    x_offset,
                    y_offset,
                    extra_advance: 0.0,
                    cluster: full_cluster.clone(),
                    text: full_text_range,
                    rtl,
                })?;
            }

            if out_clusters.is_some() {
                local_clusters.try_push(ShapedCluster {
                    span_index,
                    byte_range: local_cluster,
                    advance: cluster_advance,
                    glyph_range: cluster_glyph_start..out_glyphs.len(),
                    rtl,
                })?;
            }
            first = last + 1;
        }

        // Refuse unexpected `.notdef` glyphs for visible non-ignorable characters.
        if let Some(character) = out_glyphs[glyph_base..]
            .iter()
            .filter(|glyph| glyph.glyph_id == 0)
            .filter_map(|glyph| {
                let local_s = glyph.cluster.start.saturating_sub(para_offset);
                let local_e = glyph.cluster.end.saturating_sub(para_offset);
                para.get(local_s..local_e)?.chars().next()
            })
            .find(|&character| !is_rich_ignorable(character))
        {
            return Err(RichLayoutError::MissingGlyph(character));
        }

        if let Some(clusters) = out_clusters {
            local_clusters.sort_by_key(|c| c.byte_range.start);
            clusters.try_reserve(local_clusters.len())?;
            for cluster in local_clusters.iter() {
                clusters.try_push(cluster.clone())?;
            }
        }
        Ok(())
    }

    /// Shapes a uniform-span, uniform-direction byte range `range` inside `para`
    /// in visual left-to-right order into `out_glyphs`. Used on rare cache misses
    /// (emergency mid-ligature / mid-cursive-word break or UAX #9 L1 direction flip)
    /// and for exact measurement of emergency sub-cluster breaks.
    #[expect(
        clippy::too_many_arguments,
        reason = "segment bounds, span reference, direction, and output buffer"
    )]
    fn shape_segment_visual(
        &mut self,
        para: &str,
        para_offset: usize,
        range: Range<usize>,
        span_index: usize,
        span: &SpanInput<'_>,
        rtl: bool,
        out_glyphs: &mut BudgetVec<'b, PositionedGlyph>,
    ) -> Result<(), RichLayoutError> {
        if range.is_empty() {
            return Ok(());
        }
        let all_standard = span_all_standard(self.faces, span);
        let mut scripts = BudgetVec::new(self.budget);
        script_runs(para, range, &mut scripts)?;
        if rtl {
            scripts.reverse();
        }
        for &(ref script_range, script) in scripts.iter() {
            let mut face_runs = BudgetVec::new(self.budget);
            split_face_runs(
                self.faces,
                para,
                script_range.clone(),
                span,
                all_standard,
                self.budget,
                &mut face_runs,
            )?;
            if rtl {
                face_runs.reverse();
            }
            for &(face_index, ref sub_range) in face_runs.iter() {
                self.shape_face_subrun(
                    para,
                    para_offset,
                    sub_range.clone(),
                    span_index,
                    face_index,
                    span.size,
                    script,
                    rtl,
                    all_standard,
                    out_glyphs,
                    None,
                )?;
            }
        }
        Ok(())
    }
}

/// Lays out styled spans according to `options` with UAX #9 BiDi resolution,
/// UAX #14 + UAX #29 wrapping in logical order, single-pass shaped cluster
/// caching, mixed font-size shared baselines, decorations, and hyperlink
/// hit-boxes under `budget` (DESIGN.md F6).
pub fn layout_rich_text(
    faces: &[FallbackSource<'_>],
    spans: &[SpanInput<'_>],
    options: RichLayoutOptions,
    budget: &Budget,
) -> Result<RichLayout, RichLayoutError> {
    budget.poll()?;
    validate_inputs(faces, spans, options)?;

    let mut total_bytes = 0usize;
    let mut total_chars = 0u64;
    for (index, span) in spans.iter().enumerate() {
        if index % POLL_STEP == 0 {
            budget.poll()?;
        }
        total_bytes = total_bytes.saturating_add(span.text.len());
        total_chars = total_chars.saturating_add(span.text.chars().count() as u64);
    }
    budget.check(LimitCode::TextInputSize, total_bytes as u64)?;
    budget.check(LimitCode::ShapedGlyphCount, total_chars)?;

    if total_bytes == 0 {
        return Ok(RichLayout {
            text: String::new(),
            lines: Vec::new(),
            decorations: Vec::new(),
            links: Vec::new(),
            height: 0.0,
            spare_height: options.max_height,
            total_glyphs: 0,
        });
    }

    let mut full_bytes = BudgetVec::<u8>::new(budget);
    full_bytes.try_reserve_exact(total_bytes)?;
    let mut span_of_byte = BudgetVec::<u32>::new(budget);
    span_of_byte.try_reserve_exact(total_bytes)?;

    for (idx, span) in spans.iter().enumerate() {
        budget.poll()?;
        let span_u32 = u32::try_from(idx).map_err(|_| RichLayoutError::InvalidGeometry)?;
        full_bytes.try_extend_from_slice(span.text.as_bytes())?;
        span_of_byte.try_reserve(span.text.len())?;
        for _ in 0..span.text.len() {
            span_of_byte.try_push(span_u32)?;
        }
    }

    let full_text =
        String::from_utf8(full_bytes.into_vec()).map_err(|_| RichLayoutError::InvalidGeometry)?;

    let forced_level = match options.base_direction {
        BaseDirection::Auto => None,
        BaseDirection::Ltr => Some(Level::ltr()),
        BaseDirection::Rtl => Some(Level::rtl()),
    };

    let mut pool = ShaperPool::new(faces, budget)?;
    let mut lines = BudgetVec::<RichLine>::new(budget);
    let mut decorations = BudgetVec::<DecorationRule>::new(budget);
    let mut links = BudgetVec::<LinkBox>::new(budget);
    let mut total_glyphs = 0usize;

    let mut cursor_y = 0.0_f64;
    let mut prev_descent = 0.0_f64;
    let mut prev_half_gap = 0.0_f64;

    let mut para_start = 0usize;
    loop {
        budget.poll()?;
        let next_newline =
            scan::find_ascii(&full_text, para_start, |byte| byte == b'\n', &mut || {
                budget.poll()
            })?;
        let para_end = next_newline.unwrap_or(full_text.len());
        let para = &full_text[para_start..para_end];

        if para.is_empty() {
            let line_count = u64::try_from(lines.len())
                .unwrap_or(u64::MAX)
                .saturating_add(1);
            budget.check(LimitCode::TextLineCount, line_count)?;

            let fallback_span_idx = if para_start < span_of_byte.len() {
                span_of_byte[para_start] as usize
            } else {
                span_of_byte.last().copied().unwrap_or(0) as usize
            };
            let sp = &spans[fallback_span_idx];
            let (ascent, descent, leading) = span_vertical_metrics(
                pool.line_metrics[sp.face_index],
                sp.size,
                options.line_height,
            );
            let half_gap = (leading - (ascent + descent)) * 0.5;
            let baseline_y = if lines.is_empty() {
                ascent
            } else {
                cursor_y + prev_descent + prev_half_gap + ascent + half_gap
            };
            if !baseline_y.is_finite() {
                return Err(RichLayoutError::InvalidGeometry);
            }
            cursor_y = baseline_y;
            prev_descent = descent;
            prev_half_gap = half_gap;

            lines.try_push(RichLine {
                baseline_y,
                ascent,
                descent,
                width: 0.0,
                is_rtl_paragraph: options.base_direction == BaseDirection::Rtl,
                justified: false,
                runs: Vec::new(),
            })?;
        } else {
            // Charge scratch estimate for unicode-bidi's internal paragraph vectors.
            let bidi_scratch = u64::try_from(para.len().saturating_mul(2)).unwrap_or(u64::MAX);
            budget.charge(LimitCode::MemorySize, bidi_scratch)?;
            let bidi = BidiInfo::new(para, forced_level);
            let para_result = layout_paragraph(
                &mut pool,
                &full_text,
                para,
                para_start,
                &span_of_byte,
                spans,
                &bidi,
                options,
                &mut lines,
                &mut decorations,
                &mut links,
                &mut total_glyphs,
                &mut cursor_y,
                &mut prev_descent,
                &mut prev_half_gap,
            );
            budget.release(LimitCode::MemorySize, bidi_scratch);
            para_result?;
        }

        match next_newline {
            Some(at) => para_start = at + 1,
            None => break,
        }
    }

    let total_height = if lines.is_empty() {
        0.0
    } else {
        cursor_y + prev_descent
    };
    if !total_height.is_finite() {
        return Err(RichLayoutError::InvalidGeometry);
    }
    let mut spare_height = options.max_height - total_height;
    if spare_height.abs() <= FIT_TOLERANCE {
        spare_height = 0.0;
    }

    Ok(RichLayout {
        text: full_text,
        lines: lines.into_vec(),
        decorations: decorations.into_vec(),
        links: links.into_vec(),
        height: total_height,
        spare_height,
        total_glyphs,
    })
}

#[expect(
    clippy::too_many_arguments,
    reason = "paragraph state, caches, and layout accumulators"
)]
fn layout_paragraph<'b>(
    pool: &mut ShaperPool<'_, 'b>,
    full_text: &str,
    para: &str,
    para_start: usize,
    span_of_byte: &[u32],
    spans: &[SpanInput<'_>],
    bidi: &BidiInfo<'_>,
    options: RichLayoutOptions,
    lines: &mut BudgetVec<'b, RichLine>,
    decorations: &mut BudgetVec<'b, DecorationRule>,
    links: &mut BudgetVec<'b, LinkBox>,
    total_glyphs: &mut usize,
    cursor_y: &mut f64,
    prev_descent: &mut f64,
    prev_half_gap: &mut f64,
) -> Result<(), RichLayoutError> {
    let budget = pool.budget;
    let para_info = &bidi.paragraphs[0];
    let para_is_rtl = para_info.level.is_rtl();

    // Pass 1: shape every logical (span × bidi-level × script × face) segment ONCE,
    // caching flat glyphs and logical cluster records with O(1) prefix-sum advances.
    let mut para_glyphs = BudgetVec::<PositionedGlyph>::new(budget);
    let mut clusters = BudgetVec::<ShapedCluster>::new(budget);
    shape_paragraph_clusters(
        pool,
        para,
        para_start,
        span_of_byte,
        spans,
        bidi,
        &mut para_glyphs,
        &mut clusters,
    )?;

    let mut prefix_adv = BudgetVec::<f64>::new(budget);
    prefix_adv.try_reserve_exact(clusters.len().saturating_add(1))?;
    let mut running = 0.0_f64;
    prefix_adv.try_push(0.0)?;
    for c in clusters.iter() {
        running += c.advance;
        prefix_adv.try_push(running)?;
    }

    // Wrap paragraph in logical order (or keep whole line when unwrapped).
    let wrapped_ranges = match options.max_width {
        Some(box_width) => wrap_paragraph_prefix(
            pool,
            para,
            para_start,
            span_of_byte,
            spans,
            bidi,
            &clusters,
            &prefix_adv,
            box_width,
        )?,
        None => {
            let mut single = BudgetVec::new(budget);
            single.try_push((0usize, para.len(), false))?;
            single
        }
    };

    // Pass 2: reorder each wrapped line with UAX #9 L1/L2 and slice cached glyphs.
    for &(line_start, visible_end, soft_break) in wrapped_ranges.iter() {
        budget.poll()?;
        let line_count = u64::try_from(lines.len())
            .unwrap_or(u64::MAX)
            .saturating_add(1);
        budget.check(LimitCode::TextLineCount, line_count)?;

        let visible_range = line_start..visible_end;
        if visible_range.is_empty() {
            let sp_idx = span_of_byte
                .get(para_start + line_start)
                .or_else(|| span_of_byte.get(para_start))
                .copied()
                .unwrap_or(0) as usize;
            let sp = &spans[sp_idx];
            let (ascent, descent, leading) = span_vertical_metrics(
                pool.line_metrics[sp.face_index],
                sp.size,
                options.line_height,
            );
            let half_gap = (leading - (ascent + descent)) * 0.5;
            let baseline_y = if lines.is_empty() {
                ascent
            } else {
                *cursor_y + *prev_descent + *prev_half_gap + ascent + half_gap
            };
            if !baseline_y.is_finite() {
                return Err(RichLayoutError::InvalidGeometry);
            }
            *cursor_y = baseline_y;
            *prev_descent = descent;
            *prev_half_gap = half_gap;
            lines.try_push(RichLine {
                baseline_y,
                ascent,
                descent,
                width: 0.0,
                is_rtl_paragraph: para_is_rtl,
                justified: false,
                runs: Vec::new(),
            })?;
            continue;
        }

        let vr_scratch = u64::try_from(visible_range.len()).unwrap_or(u64::MAX);
        budget.charge(LimitCode::MemorySize, vr_scratch)?;
        let (levels, bidi_runs) = bidi.visual_runs(para_info, visible_range);

        let mut visual_runs = BudgetVec::<VisualRun>::new(budget);
        let mut line_width = 0.0_f64;
        let mut line_ascent = 0.0_f64;
        let mut line_descent = 0.0_f64;
        let mut line_leading = 0.0_f64;
        let mut space_count = 0usize;

        let build_result: Result<(), RichLayoutError> = (|| {
            for bidi_run in bidi_runs {
                let rtl = levels[bidi_run.start].is_rtl();
                let sub_spans = split_by_span(
                    bidi_run.start,
                    bidi_run.end,
                    para_start,
                    span_of_byte,
                    budget,
                )?;
                for idx_pos in 0..sub_spans.len() {
                    let pick = if rtl {
                        sub_spans.len() - 1 - idx_pos
                    } else {
                        idx_pos
                    };
                    let (sub_start, sub_end, span_idx) = sub_spans[pick];
                    let sp = &spans[span_idx];
                    let mut run_glyphs = BudgetVec::<PositionedGlyph>::new(budget);

                    emit_visual_subrun(
                        pool,
                        para,
                        para_start,
                        sub_start..sub_end,
                        span_idx,
                        sp,
                        rtl,
                        &clusters,
                        &para_glyphs,
                        &mut run_glyphs,
                    )?;

                    // Accumulate vertical metrics from the span's primary face and any
                    // fallback faces actually used by glyphs in this run.
                    let (asc, desc, lead) = span_vertical_metrics(
                        pool.line_metrics[sp.face_index],
                        sp.size,
                        options.line_height,
                    );
                    line_ascent = line_ascent.max(asc);
                    line_descent = line_descent.max(desc);
                    line_leading = line_leading.max(lead);

                    let mut run_w = 0.0_f64;
                    for glyph in run_glyphs.iter() {
                        run_w += glyph.x_advance;
                        if glyph.face_index != sp.face_index {
                            let (fb_asc, fb_desc, fb_lead) = span_vertical_metrics(
                                pool.line_metrics[glyph.face_index],
                                sp.size,
                                options.line_height,
                            );
                            line_ascent = line_ascent.max(fb_asc);
                            line_descent = line_descent.max(fb_desc);
                            line_leading = line_leading.max(fb_lead);
                        }
                        if full_text.get(glyph.text.clone()) == Some(" ") {
                            space_count += 1;
                        }
                    }

                    *total_glyphs = total_glyphs.saturating_add(run_glyphs.len());
                    visual_runs.try_push(VisualRun {
                        span_index: span_idx,
                        face_index: sp.face_index,
                        size: sp.size,
                        text_range: (para_start + sub_start)..(para_start + sub_end),
                        rtl,
                        x: line_width,
                        width: run_w,
                        glyphs: run_glyphs.into_vec(),
                    })?;
                    line_width += run_w;
                }
            }
            Ok(())
        })();
        budget.release(LimitCode::MemorySize, vr_scratch);
        build_result?;

        let half_gap = (line_leading - (line_ascent + line_descent)) * 0.5;
        let baseline_y = if lines.is_empty() {
            line_ascent
        } else {
            *cursor_y + *prev_descent + *prev_half_gap + line_ascent + half_gap
        };
        if !baseline_y.is_finite() {
            return Err(RichLayoutError::InvalidGeometry);
        }
        *cursor_y = baseline_y;
        *prev_descent = line_descent;
        *prev_half_gap = half_gap;

        let (x_shift, justify, extra_per_space, final_width) = match options.max_width {
            Some(box_width) => {
                let free = (box_width - line_width).max(0.0);
                let justify =
                    options.align == Align::Justify && soft_break && space_count > 0 && free > 0.0;
                #[expect(
                    clippy::cast_precision_loss,
                    reason = "space_count is bounded by TextInputSize"
                )]
                let extra = if justify {
                    free / (space_count as f64)
                } else {
                    0.0
                };
                let shift = if justify {
                    0.0
                } else {
                    alignment_shift(options.align, para_is_rtl, free)
                };
                let width = if justify { box_width } else { line_width };
                (shift, justify, extra, width)
            }
            None => (0.0, false, 0.0, line_width),
        };

        let mut pen_x = x_shift;
        for run in visual_runs.iter_mut() {
            run.x = pen_x;
            if extra_per_space > 0.0 {
                let mut adjusted_w = 0.0_f64;
                for glyph in &mut run.glyphs {
                    if full_text.get(glyph.text.clone()) == Some(" ") {
                        glyph.x_advance += extra_per_space;
                        glyph.extra_advance += extra_per_space;
                    }
                    adjusted_w += glyph.x_advance;
                }
                run.width = adjusted_w;
            }

            let sp = &spans[run.span_index];
            let dec = pool.decorations[sp.face_index];
            if sp.underline && run.width > 0.0 {
                decorations.try_push(DecorationRule {
                    span_index: run.span_index,
                    is_strikethrough: false,
                    x0: run.x,
                    x1: run.x + run.width,
                    y: baseline_y - dec.underline_pos * sp.size,
                    thickness: dec.underline_thick * sp.size,
                })?;
            }
            if sp.strikethrough && run.width > 0.0 {
                decorations.try_push(DecorationRule {
                    span_index: run.span_index,
                    is_strikethrough: true,
                    x0: run.x,
                    x1: run.x + run.width,
                    y: baseline_y - dec.strikeout_pos * sp.size,
                    thickness: dec.strikeout_thick * sp.size,
                })?;
            }
            if let Some(link_id) = sp.link_id
                && run.width > 0.0
            {
                let mut run_asc = pool.line_metrics[sp.face_index].ascender.max(0.0) * sp.size;
                let mut run_desc = (-pool.line_metrics[sp.face_index].descender).max(0.0) * sp.size;
                for glyph in &run.glyphs {
                    if glyph.face_index != sp.face_index {
                        let m = pool.line_metrics[glyph.face_index];
                        run_asc = run_asc.max(m.ascender.max(0.0) * sp.size);
                        run_desc = run_desc.max((-m.descender).max(0.0) * sp.size);
                    }
                }
                links.try_push(LinkBox {
                    link_id,
                    x0: run.x,
                    y0: baseline_y - run_asc,
                    x1: run.x + run.width,
                    y1: baseline_y + run_desc,
                })?;
            }
            pen_x += run.width;
        }

        lines.try_push(RichLine {
            baseline_y,
            ascent: line_ascent,
            descent: line_descent,
            width: final_width,
            is_rtl_paragraph: para_is_rtl,
            justified: justify,
            runs: visual_runs.into_vec(),
        })?;
    }

    Ok(())
}

/// Pass 1: shape every logical `(span × bidi-level)` segment of `para` once,
/// filling `para_glyphs` and logical-order `clusters`.
#[expect(
    clippy::too_many_arguments,
    reason = "paragraph slice, bidi levels, and output glyph/cluster caches"
)]
fn shape_paragraph_clusters<'b>(
    pool: &mut ShaperPool<'_, 'b>,
    para: &str,
    para_offset: usize,
    span_of_byte: &[u32],
    spans: &[SpanInput<'_>],
    bidi: &BidiInfo<'_>,
    para_glyphs: &mut BudgetVec<'b, PositionedGlyph>,
    clusters: &mut BudgetVec<'b, ShapedCluster>,
) -> Result<(), RichLayoutError> {
    let levels = &bidi.levels;
    let mut at = 0usize;
    while at < para.len() {
        pool.budget.poll()?;
        let sp_idx = span_of_byte[para_offset + at] as usize;
        let lvl = levels[at];
        let mut end = at + para[at..].chars().next().map_or(1, char::len_utf8);
        while end < para.len()
            && span_of_byte[para_offset + end] as usize == sp_idx
            && levels[end] == lvl
        {
            end += para[end..].chars().next().map_or(1, char::len_utf8);
        }

        let sp = &spans[sp_idx];
        let rtl = lvl.is_rtl();
        let all_standard = span_all_standard(pool.faces, sp);

        let mut scripts = BudgetVec::new(pool.budget);
        script_runs(para, at..end, &mut scripts)?;
        for &(ref script_range, script) in scripts.iter() {
            let mut face_runs = BudgetVec::new(pool.budget);
            split_face_runs(
                pool.faces,
                para,
                script_range.clone(),
                sp,
                all_standard,
                pool.budget,
                &mut face_runs,
            )?;
            for &(face_index, ref sub_range) in face_runs.iter() {
                pool.shape_face_subrun(
                    para,
                    para_offset,
                    sub_range.clone(),
                    sp_idx,
                    face_index,
                    sp.size,
                    script,
                    rtl,
                    all_standard,
                    para_glyphs,
                    Some(clusters),
                )?;
            }
        }
        at = end;
    }
    Ok(())
}

/// Emits glyphs in visual order for `sub_range` by slicing the single-pass
/// `clusters`/`para_glyphs` cache on a hit, falling back to re-shaping only
/// when an emergency break split a ligature/cursive word or UAX #9 L1 flipped
/// the run's direction relative to paragraph shaping.
#[expect(
    clippy::too_many_arguments,
    reason = "visual subrun bounds, span metadata, glyph cache, and output buffer"
)]
fn emit_visual_subrun<'b>(
    pool: &mut ShaperPool<'_, 'b>,
    para: &str,
    para_offset: usize,
    sub_range: Range<usize>,
    span_index: usize,
    span: &SpanInput<'_>,
    rtl: bool,
    clusters: &[ShapedCluster],
    para_glyphs: &[PositionedGlyph],
    out_glyphs: &mut BudgetVec<'b, PositionedGlyph>,
) -> Result<(), RichLayoutError> {
    if sub_range.is_empty() {
        return Ok(());
    }
    let c0 = clusters.partition_point(|c| c.byte_range.end <= sub_range.start);
    let c1 = clusters.partition_point(|c| c.byte_range.start < sub_range.end);

    let cache_hit = c0 < c1
        && clusters[c0].byte_range.start == sub_range.start
        && clusters[c1 - 1].byte_range.end == sub_range.end
        && clusters[c0..c1]
            .iter()
            .all(|c| c.rtl == rtl && c.span_index == span_index)
        && !has_cursive_break_boundary(para, sub_range.start, sub_range.end);

    if cache_hit {
        let count: usize = clusters[c0..c1].iter().map(|c| c.glyph_range.len()).sum();
        out_glyphs.try_reserve_exact(count)?;
        if rtl {
            for cluster in clusters[c0..c1].iter().rev() {
                for glyph in &para_glyphs[cluster.glyph_range.clone()] {
                    out_glyphs.try_push(glyph.clone())?;
                }
            }
        } else {
            for cluster in &clusters[c0..c1] {
                for glyph in &para_glyphs[cluster.glyph_range.clone()] {
                    out_glyphs.try_push(glyph.clone())?;
                }
            }
        }
        Ok(())
    } else {
        pool.shape_segment_visual(
            para,
            para_offset,
            sub_range,
            span_index,
            span,
            rtl,
            out_glyphs,
        )
    }
}

/// Wraps `para` greedily in logical order using O(log C) prefix-sum cluster
/// widths and galloping search over UAX #14 break opportunities (with lazy
/// UAX #29 emergency grapheme breaking).
#[expect(
    clippy::too_many_arguments,
    reason = "paragraph text, span/bidi metadata, cluster prefix sums, and box width"
)]
fn wrap_paragraph_prefix<'b>(
    pool: &mut ShaperPool<'_, 'b>,
    para: &str,
    para_offset: usize,
    span_of_byte: &[u32],
    spans: &[SpanInput<'_>],
    bidi: &BidiInfo<'_>,
    clusters: &[ShapedCluster],
    prefix_adv: &[f64],
    max_width: f64,
) -> Result<BudgetVec<'b, (usize, usize, bool)>, RichLayoutError> {
    let budget = pool.budget;
    let mut wrapped = BudgetVec::new(budget);

    // Fast path: if the whole trimmed paragraph fits on one line, skip UAX #14 scanning.
    let whole_visible = scan::trim_end(para, &mut || budget.poll())?;
    let whole_width = range_width_prefix(clusters, prefix_adv, 0, whole_visible.len());
    if whole_width <= max_width + FIT_TOLERANCE {
        wrapped.try_push((0, whole_visible.len(), false))?;
        return Ok(wrapped);
    }

    let break_ends = scan::break_ends(para, budget, &mut || budget.poll())?;
    let mut grapheme_ends: Option<BudgetVec<'b, usize>> = None;

    let mut start_byte = 0usize;
    let mut break_cursor = 0usize;

    while start_byte < para.len() {
        budget.poll()?;
        // Advance break_cursor monotonically to the first break opportunity after start_byte.
        while break_cursor < break_ends.len() && break_ends[break_cursor] <= start_byte {
            break_cursor += 1;
        }
        // Skip leading whitespace-only break segments so they do not form a blank line.
        let mut first = break_cursor;
        let mut seg_start = start_byte;
        while first + 1 < break_ends.len() {
            budget.poll()?;
            if !scan::trim_end(&para[seg_start..break_ends[first]], &mut || budget.poll())?
                .is_empty()
            {
                break;
            }
            seg_start = break_ends[first];
            first += 1;
        }

        let first_end = break_ends[first];
        let first_vis_len =
            scan::trim_end(&para[start_byte..first_end], &mut || budget.poll())?.len();
        let first_vis_end = start_byte + first_vis_len;
        let first_w = range_width_prefix(clusters, prefix_adv, start_byte, first_vis_end);

        if first_vis_len > 0 && first_w > max_width + FIT_TOLERANCE {
            // Emergency UAX #29 grapheme break inside a word wider than max_width.
            if grapheme_ends.is_none() {
                let mut ends = BudgetVec::new(budget);
                scan::grapheme_ends(para, &mut || budget.poll(), |end| ends.try_push(end))?;
                grapheme_ends = Some(ends);
            }
            let g_ends = grapheme_ends
                .as_deref()
                .expect("grapheme ends populated above");
            let end_byte = emergency_grapheme_break(
                pool,
                para,
                para_offset,
                span_of_byte,
                spans,
                bidi,
                clusters,
                prefix_adv,
                start_byte,
                first_vis_end,
                g_ends,
                max_width,
            )?;
            let soft_break = end_byte < para.len();
            wrapped.try_push((start_byte, end_byte, soft_break))?;
            start_byte = end_byte;
            continue;
        }

        // Gallop forward from `first` over break_ends, then binary search the bracket.
        let last = break_ends.len() - 1;
        let mut best_index = first;
        let mut best = (first_end, first_vis_end);
        let mut step = 1usize;
        let failed_index = loop {
            if best_index == last {
                break None;
            }
            budget.poll()?;
            let probe = first.saturating_add(step).min(last);
            let brk = break_ends[probe];
            let vis_end =
                start_byte + scan::trim_end(&para[start_byte..brk], &mut || budget.poll())?.len();
            let width = range_width_prefix(clusters, prefix_adv, start_byte, vis_end);
            if width > max_width + FIT_TOLERANCE {
                break Some(probe);
            }
            best_index = probe;
            best = (brk, vis_end);
            step = step.saturating_mul(2);
        };

        if let Some(failed) = failed_index {
            let (mut low, mut high) = (best_index + 1, failed);
            while low < high {
                budget.poll()?;
                let mid = low + (high - low) / 2;
                let brk = break_ends[mid];
                let vis_end = start_byte
                    + scan::trim_end(&para[start_byte..brk], &mut || budget.poll())?.len();
                let width = range_width_prefix(clusters, prefix_adv, start_byte, vis_end);
                if width <= max_width + FIT_TOLERANCE {
                    best = (brk, vis_end);
                    best_index = mid;
                    low = mid + 1;
                } else {
                    high = mid;
                }
            }
        }

        let (brk, vis_end) = best;
        let soft_break = brk < para.len();
        wrapped.try_push((start_byte, vis_end, soft_break))?;
        start_byte = brk;
        break_cursor = best_index + 1;
    }

    Ok(wrapped)
}

/// Emergency grapheme break using prefix sums (with exact sub-cluster reshaping
/// if a grapheme boundary splits a multi-character ligature cluster).
#[expect(
    clippy::too_many_arguments,
    reason = "paragraph context, cluster prefix sums, grapheme table, and width bound"
)]
fn emergency_grapheme_break<'b>(
    pool: &mut ShaperPool<'_, 'b>,
    para: &str,
    para_offset: usize,
    span_of_byte: &[u32],
    spans: &[SpanInput<'_>],
    bidi: &BidiInfo<'_>,
    clusters: &[ShapedCluster],
    prefix_adv: &[f64],
    start_byte: usize,
    max_end: usize,
    grapheme_ends: &[usize],
    max_width: f64,
) -> Result<usize, RichLayoutError> {
    let first = grapheme_ends.partition_point(|&end| end <= start_byte);
    if first >= grapheme_ends.len() {
        return Err(RichLayoutError::Narrow);
    }
    let last = grapheme_ends
        .partition_point(|&end| end <= max_end)
        .saturating_sub(1)
        .max(first);

    let first_end = grapheme_ends[first];
    let first_w = measure_prefix_or_exact(
        pool,
        para,
        para_offset,
        span_of_byte,
        spans,
        bidi,
        clusters,
        prefix_adv,
        start_byte,
        first_end,
    )?;
    if first_w > max_width + FIT_TOLERANCE {
        return Err(RichLayoutError::Narrow);
    }

    let mut best_index = first;
    let mut step = 1usize;
    let failed_index = loop {
        if best_index == last {
            return Ok(grapheme_ends[best_index]);
        }
        pool.budget.poll()?;
        let probe = first.saturating_add(step).min(last);
        let width = measure_prefix_or_exact(
            pool,
            para,
            para_offset,
            span_of_byte,
            spans,
            bidi,
            clusters,
            prefix_adv,
            start_byte,
            grapheme_ends[probe],
        )?;
        if width > max_width + FIT_TOLERANCE {
            break probe;
        }
        best_index = probe;
        step = step.saturating_mul(2);
    };

    let (mut low, mut high) = (best_index + 1, failed_index);
    while low < high {
        pool.budget.poll()?;
        let mid = low + (high - low) / 2;
        let width = measure_prefix_or_exact(
            pool,
            para,
            para_offset,
            span_of_byte,
            spans,
            bidi,
            clusters,
            prefix_adv,
            start_byte,
            grapheme_ends[mid],
        )?;
        if width <= max_width + FIT_TOLERANCE {
            best_index = mid;
            low = mid + 1;
        } else {
            high = mid;
        }
    }
    Ok(grapheme_ends[best_index])
}

/// Returns O(log C) prefix-sum advance width when `start..end` aligns with cluster
/// boundaries, or shapes the exact slice when an emergency break splits a ligature.
#[expect(
    clippy::too_many_arguments,
    reason = "paragraph metadata, cluster prefix sums, and slice bounds"
)]
fn measure_prefix_or_exact<'b>(
    pool: &mut ShaperPool<'_, 'b>,
    para: &str,
    para_offset: usize,
    span_of_byte: &[u32],
    spans: &[SpanInput<'_>],
    bidi: &BidiInfo<'_>,
    clusters: &[ShapedCluster],
    prefix_adv: &[f64],
    start: usize,
    end: usize,
) -> Result<f64, RichLayoutError> {
    if start >= end {
        return Ok(0.0);
    }
    let c0 = clusters.partition_point(|c| c.byte_range.end <= start);
    let c1 = clusters.partition_point(|c| c.byte_range.start < end);
    if c0 < c1 && clusters[c0].byte_range.start == start && clusters[c1 - 1].byte_range.end == end {
        return Ok(prefix_adv[c1] - prefix_adv[c0]);
    }
    // Slice splits a multi-character ligature cluster; measure exact slice.
    let mut temp = BudgetVec::new(pool.budget);
    let mut at = start;
    while at < end {
        let sp_idx = span_of_byte[para_offset + at] as usize;
        let lvl = bidi.levels[at];
        let mut seg_end = at + para[at..end].chars().next().map_or(1, char::len_utf8);
        while seg_end < end
            && span_of_byte[para_offset + seg_end] as usize == sp_idx
            && bidi.levels[seg_end] == lvl
        {
            seg_end += para[seg_end..end].chars().next().map_or(1, char::len_utf8);
        }
        pool.shape_segment_visual(
            para,
            para_offset,
            at..seg_end,
            sp_idx,
            &spans[sp_idx],
            lvl.is_rtl(),
            &mut temp,
        )?;
        at = seg_end;
    }
    Ok(temp.iter().map(|g| g.x_advance).sum())
}

/// Computes the sum of cluster advances intersecting `start..end` in O(log C) time.
fn range_width_prefix(
    clusters: &[ShapedCluster],
    prefix_adv: &[f64],
    start: usize,
    end: usize,
) -> f64 {
    if start >= end || clusters.is_empty() {
        return 0.0;
    }
    let c0 = clusters.partition_point(|c| c.byte_range.end <= start);
    let c1 = clusters.partition_point(|c| c.byte_range.start < end);
    if c0 >= c1 {
        0.0
    } else {
        prefix_adv[c1] - prefix_adv[c0]
    }
}

fn validate_inputs(
    faces: &[FallbackSource<'_>],
    spans: &[SpanInput<'_>],
    options: RichLayoutOptions,
) -> Result<(), RichLayoutError> {
    if let Some(width) = options.max_width
        && (!width.is_finite() || width <= 0.0)
    {
        return Err(RichLayoutError::InvalidGeometry);
    }
    if !options.max_height.is_finite() || options.max_height < 0.0 {
        return Err(RichLayoutError::InvalidGeometry);
    }
    if let Some(lh) = options.line_height
        && (!lh.is_finite() || lh <= 0.0)
    {
        return Err(RichLayoutError::InvalidGeometry);
    }
    for span in spans {
        if !span.size.is_finite() || span.size <= 0.0 || span.face_index >= faces.len() {
            return Err(RichLayoutError::InvalidGeometry);
        }
        if span.fallback_faces.iter().any(|&fb| fb >= faces.len()) {
            return Err(RichLayoutError::InvalidGeometry);
        }
    }
    Ok(())
}

fn alignment_shift(align: Align, para_is_rtl: bool, free: f64) -> f64 {
    match align {
        Align::Left => 0.0,
        Align::Right => free,
        Align::Center => free * 0.5,
        Align::Start | Align::Justify => {
            if para_is_rtl {
                free
            } else {
                0.0
            }
        }
        Align::End => {
            if para_is_rtl {
                0.0
            } else {
                free
            }
        }
    }
}

fn face_line_metrics(face: FallbackSource<'_>) -> LineMetrics {
    match face {
        FallbackSource::Standard(font) => standard::line_metrics(font),
        FallbackSource::Embedded(face) => {
            let em = f64::from(face.units_per_em);
            LineMetrics {
                ascender: face.ascender / em,
                descender: face.descender / em,
            }
        }
    }
}

fn face_decoration_metrics(face: FallbackSource<'_>) -> DecorationMetrics {
    match face {
        FallbackSource::Standard(_) => DecorationMetrics {
            underline_pos: DEFAULT_UNDERLINE_POS,
            underline_thick: DEFAULT_UNDERLINE_THICK,
            strikeout_pos: DEFAULT_STRIKEOUT_POS,
            strikeout_thick: DEFAULT_STRIKEOUT_THICK,
        },
        FallbackSource::Embedded(face) => {
            let font = face.font();
            let em = f64::from(face.units_per_em);
            let (underline_pos, underline_thick) =
                font.post()
                    .map_or((DEFAULT_UNDERLINE_POS, DEFAULT_UNDERLINE_THICK), |post| {
                        let raw_pos = post.underline_position().to_i16();
                        let raw_thick = post.underline_thickness().to_i16();
                        if raw_pos == 0 && raw_thick <= 0 {
                            (DEFAULT_UNDERLINE_POS, DEFAULT_UNDERLINE_THICK)
                        } else {
                            let pos = f64::from(raw_pos) / em;
                            let thick = if raw_thick > 0 {
                                (f64::from(raw_thick) / em).max(MIN_DECORATION_THICK)
                            } else {
                                DEFAULT_UNDERLINE_THICK
                            };
                            (pos, thick)
                        }
                    });
            let (strikeout_pos, strikeout_thick) =
                font.os2()
                    .map_or((DEFAULT_STRIKEOUT_POS, DEFAULT_STRIKEOUT_THICK), |os2| {
                        let raw_pos = os2.y_strikeout_position();
                        let raw_size = os2.y_strikeout_size();
                        if raw_pos <= 0 || raw_size <= 0 {
                            (DEFAULT_STRIKEOUT_POS, DEFAULT_STRIKEOUT_THICK)
                        } else {
                            (
                                f64::from(raw_pos) / em,
                                (f64::from(raw_size) / em).max(MIN_DECORATION_THICK),
                            )
                        }
                    });
            DecorationMetrics {
                underline_pos,
                underline_thick,
                strikeout_pos,
                strikeout_thick,
            }
        }
    }
}

fn span_vertical_metrics(
    metrics: LineMetrics,
    size: f64,
    line_height_mult: Option<f64>,
) -> (f64, f64, f64) {
    let ascent = metrics.ascender.max(0.0) * size;
    let descent = (-metrics.descender).max(0.0) * size;
    let leading = metrics.line_height(line_height_mult) * size;
    (ascent, descent, leading)
}

fn split_by_span<'b>(
    start: usize,
    end: usize,
    para_offset: usize,
    span_of_byte: &[u32],
    budget: &'b Budget,
) -> Result<BudgetVec<'b, (usize, usize, usize)>, LimitExceeded> {
    let mut out = BudgetVec::new(budget);
    if start >= end {
        return Ok(out);
    }
    let mut seg_start = start;
    let mut cur = span_of_byte[para_offset + start] as usize;
    for (step, at) in ((start + 1)..end).enumerate() {
        if step % POLL_STEP == 0 {
            budget.poll()?;
        }
        let next = span_of_byte[para_offset + at] as usize;
        if next != cur {
            out.try_push((seg_start, at, cur))?;
            seg_start = at;
            cur = next;
        }
    }
    out.try_push((seg_start, end, cur))?;
    Ok(out)
}

fn span_all_standard(faces: &[FallbackSource<'_>], span: &SpanInput<'_>) -> bool {
    matches!(faces[span.face_index], FallbackSource::Standard(_))
        && span
            .fallback_faces
            .iter()
            .all(|&fb| matches!(faces[fb], FallbackSource::Standard(_)))
}

/// Splits `range` in `para` into maximal contiguous face runs by resolving each
/// extended grapheme cluster against `[span.face_index, ...span.fallback_faces]`.
fn split_face_runs<'b>(
    faces: &[FallbackSource<'_>],
    para: &str,
    range: Range<usize>,
    span: &SpanInput<'_>,
    all_standard: bool,
    budget: &'b Budget,
    out: &mut BudgetVec<'b, (usize, Range<usize>)>,
) -> Result<(), RichLayoutError> {
    if range.is_empty() {
        return Ok(());
    }
    if span.fallback_faces.is_empty() {
        // Fast path: single face for the span, validate characters during shaping
        // (or immediately for Standard 14 / missing embedded glyphs).
        if matches!(faces[span.face_index], FallbackSource::Embedded(_)) {
            let mut g_start = range.start;
            let slice = &para[range.clone()];
            let mut missing_err: Option<RichLayoutError> = None;
            scan::grapheme_ends(slice, &mut || budget.poll(), |rel_end| {
                if missing_err.is_none() {
                    let g_end = range.start + rel_end;
                    let cluster = &para[g_start..g_end];
                    if !cluster.chars().all(is_rich_ignorable)
                        && !face_supports_rich_cluster(faces[span.face_index], cluster)
                    {
                        let ch = first_non_ignorable(cluster);
                        missing_err = Some(RichLayoutError::MissingGlyph(ch));
                    }
                    g_start = g_end;
                }
                Ok(())
            })?;
            if let Some(err) = missing_err {
                return Err(err);
            }
        }
        out.try_push((span.face_index, range))?;
        return Ok(());
    }

    let mut last_face: Option<usize> = None;
    let mut g_start = range.start;
    let slice = &para[range.clone()];
    let mut pick_err: Option<RichLayoutError> = None;
    scan::grapheme_ends(slice, &mut || budget.poll(), |rel_end| {
        if pick_err.is_some() {
            return Ok(());
        }
        let g_end = range.start + rel_end;
        let cluster = &para[g_start..g_end];
        match pick_face_for_rich_cluster(faces, span, cluster, last_face, all_standard) {
            Ok(chosen) => {
                last_face = Some(chosen);
                if let Some(previous) = out.last_mut()
                    && previous.0 == chosen
                    && previous.1.end == g_start
                {
                    previous.1.end = g_end;
                } else {
                    out.try_push((chosen, g_start..g_end))?;
                }
            }
            Err(err) => {
                pick_err = Some(err);
            }
        }
        g_start = g_end;
        Ok(())
    })?;
    if let Some(err) = pick_err {
        return Err(err);
    }
    Ok(())
}

fn pick_face_for_rich_cluster(
    faces: &[FallbackSource<'_>],
    span: &SpanInput<'_>,
    cluster: &str,
    last_face: Option<usize>,
    all_standard: bool,
) -> Result<usize, RichLayoutError> {
    if !all_standard && cluster.chars().all(is_rich_ignorable) {
        if let Some(prev) = last_face
            && matches!(faces[prev], FallbackSource::Embedded(_))
        {
            return Ok(prev);
        }
        if matches!(faces[span.face_index], FallbackSource::Embedded(_)) {
            return Ok(span.face_index);
        }
        if let Some(&fb) = span
            .fallback_faces
            .iter()
            .find(|&&fb| matches!(faces[fb], FallbackSource::Embedded(_)))
        {
            return Ok(fb);
        }
        return Ok(last_face.unwrap_or(span.face_index));
    }

    if face_supports_rich_cluster(faces[span.face_index], cluster) {
        return Ok(span.face_index);
    }
    for &fb in span.fallback_faces {
        if face_supports_rich_cluster(faces[fb], cluster) {
            return Ok(fb);
        }
    }

    let missing = first_non_ignorable(cluster);
    if all_standard {
        Err(RichLayoutError::NotWinAnsi(missing))
    } else {
        Err(RichLayoutError::MissingGlyph(missing))
    }
}

fn face_supports_rich_cluster(face: FallbackSource<'_>, cluster: &str) -> bool {
    match face {
        FallbackSource::Standard(font) => cluster.chars().all(|character| {
            standard::glyph_code(font, character).is_some()
                && standard::winansi_byte(character).is_some()
        }),
        FallbackSource::Embedded(face) => {
            let measure = Measure::new(face);
            cluster
                .chars()
                .filter(|&character| !is_rich_ignorable(character))
                .all(|character| measure.glyph(character).is_some())
        }
    }
}

/// Characters that OpenType shapers handle as default-ignorable / format controls
/// (including UAX #9 BiDi formatting marks) without requiring a font `cmap` entry.
fn is_rich_ignorable(character: char) -> bool {
    character.is_control()
        || matches!(
            character,
            '\u{061C}'
                | '\u{200C}'..='\u{200F}'
                | '\u{202A}'..='\u{202E}'
                | '\u{2066}'..='\u{2069}'
                | '\u{FE00}'..='\u{FE0F}'
                | '\u{E0100}'..='\u{E01EF}'
        )
}

fn first_non_ignorable(cluster: &str) -> char {
    cluster
        .chars()
        .find(|&character| !is_rich_ignorable(character))
        .or_else(|| cluster.chars().next())
        .unwrap_or('\u{FFFD}')
}

/// Returns whether a line break at `start` or `end` cuts inside a cursive-joining
/// word (such as Arabic or Syriac), requiring contextual re-shaping at the break.
fn has_cursive_break_boundary(para: &str, start: usize, end: usize) -> bool {
    let split_left = start > 0
        && para[..start]
            .chars()
            .next_back()
            .is_some_and(is_cursive_script)
        && para[start..].chars().next().is_some_and(is_cursive_script);
    if split_left {
        return true;
    }
    end < para.len()
        && para[..end]
            .chars()
            .next_back()
            .is_some_and(is_cursive_script)
        && para[end..].chars().next().is_some_and(is_cursive_script)
}

fn is_cursive_script(character: char) -> bool {
    matches!(
        character.script(),
        UnicodeScript::Arabic
            | UnicodeScript::Syriac
            | UnicodeScript::Mandaic
            | UnicodeScript::Nko
            | UnicodeScript::Mongolian
            | UnicodeScript::Adlam
    )
}
