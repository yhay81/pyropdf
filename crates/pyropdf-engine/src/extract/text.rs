//! Plain text in reading order (DESIGN.md sections 6 and 14), ported from
//! pylopdf's `rust/src/extract.rs` at 036be111cafe77755db4bb2be17bc70a8f2f9d82.
//!
//! A `TextCollector` is the content model's sink: it keeps one record per
//! glyph with its Unicode text, origin, size, advance, and baseline
//! direction in display space. The records are clustered into lines, the
//! lines ordered for reading, and the text assembled with inferred spaces
//! and line breaks. The operation's budget counts the glyphs with
//! `text_glyph_count` and their UTF-8 with `text_size` (F6).

use std::collections::HashMap;

use hayro::hayro_interpret::DrawMode;
use hayro::hayro_interpret::font::Glyph;
use hayro::hayro_interpret::hayro_cmap::BfString;
use hayro::kurbo::{Affine, BezPath, Point, Rect};
use pyropdf_store::budget::{Budget, LimitCode};
use unicode_bidi::{BidiClass, bidi_class};

use super::{BBox, ContentSink, Error, GlyphStyle, ItemKinds, RENDER_FILL, RENDER_FILL_STROKE};
use crate::bridge::Bridged;

/// The line metrics of a font, which box its glyphs.
#[derive(Clone, Copy)]
struct FontInfo {
    /// The ascender and descender in em, as MuPDF reads them from the font
    /// program, or 0.8 and -0.2 without one.
    ascender: f64,
    descender: f64,
}

impl Default for FontInfo {
    fn default() -> Self {
        Self {
            ascender: DEFAULT_ASCENDER,
            descender: DEFAULT_DESCENDER,
        }
    }
}

/// The ascender and descender of a font without metrics, MuPDF's defaults.
const DEFAULT_ASCENDER: f64 = 0.8;
const DEFAULT_DESCENDER: f64 = -0.2;

/// Reads the line metrics of a glyph's font, once per font.
///
/// A standard font that is not embedded, for which hayro gives no font
/// data, and a bare CFF program, which OpenType tables do not describe,
/// keep the defaults.
fn font_info_of(glyph: &hayro::hayro_interpret::font::OutlineGlyph) -> FontInfo {
    glyph
        .font_data()
        .and_then(|data| line_metrics((*data.data).as_ref()))
        .unwrap_or_default()
}

/// Returns the ascender and descender of a font program, as pymupdf's
/// `Font` reports them (pylopdf's `generate::font_line_metrics`).
///
/// MuPDF takes them from FreeType, which reads the OS/2 typographic metrics
/// when the face sets `USE_TYPO_METRICS` and the `hhea` metrics otherwise,
/// falling back to the typographic and then the Windows metrics when both
/// `hhea` values are zero. MuPDF then replaces an ascender that is not
/// positive or exceeds 8 em with 0.8, a descender beyond 3 em either way with
/// -0.2, and a positive descender with its negation.
fn line_metrics(program: &[u8]) -> Option<FontInfo> {
    use skrifa::raw::TableProvider;
    use skrifa::raw::tables::os2::SelectionFlags;

    /// The largest ascender and descender, in em, that MuPDF accepts.
    const MAX_TRUSTED_ASCENDER: i32 = 8;
    const MAX_TRUSTED_DESCENDER: i32 = 3;

    let font = skrifa::FontRef::from_index(program, 0).ok()?;
    let units_per_em = font.head().ok()?.units_per_em();
    if units_per_em == 0 {
        return None;
    }
    let os2 = font.os2().ok();
    let use_typo_metrics = os2.as_ref().is_some_and(|os2| {
        os2.fs_selection()
            .contains(SelectionFlags::USE_TYPO_METRICS)
    });
    let hhea = font.hhea().map_or((0, 0), |hhea| {
        (hhea.ascender().to_i16(), hhea.descender().to_i16())
    });
    let (ascender, descender) = match &os2 {
        Some(os2) if use_typo_metrics => (os2.s_typo_ascender(), os2.s_typo_descender()),
        Some(os2) if hhea == (0, 0) => match (os2.s_typo_ascender(), os2.s_typo_descender()) {
            // FreeType reads the unsigned Windows metrics as signed values.
            (0, 0) => (
                os2.us_win_ascent().cast_signed(),
                os2.us_win_descent().cast_signed().wrapping_neg(),
            ),
            typo => typo,
        },
        _ => hhea,
    };
    let (ascender, descender) = (i32::from(ascender), i32::from(descender));
    let units_per_em = i32::from(units_per_em);
    let ascender = if ascender <= 0 || ascender > MAX_TRUSTED_ASCENDER * units_per_em {
        DEFAULT_ASCENDER
    } else {
        f64::from(ascender) / f64::from(units_per_em)
    };
    let descender = if descender.abs() > MAX_TRUSTED_DESCENDER * units_per_em {
        DEFAULT_DESCENDER
    } else {
        // A zero descender stays 0.0, as in MuPDF, rather than becoming -0.0.
        f64::from(-descender.abs()) / f64::from(units_per_em)
    };
    Some(FontInfo {
        ascender,
        descender,
    })
}

/// Unicode text of one glyph, mirroring hayro's `BfString` shape.
///
/// Single characters dominate real documents and stay inline, so collecting a
/// glyph does not require one heap string per glyph. Ligatures and
/// multi-character CMap mappings keep one owned heap string.
enum GlyphText {
    /// A single character, stored without heap allocation.
    Char(char),
    /// A multi-character mapping such as a ligature or CMap expansion.
    Heap(Box<str>),
}

impl GlyphText {
    /// Copy the text, failing instead of aborting when memory runs out.
    fn try_clone(&self) -> Result<Self, Error> {
        match self {
            Self::Char(ch) => Ok(Self::Char(*ch)),
            Self::Heap(text) => Self::try_from_str(text),
        }
    }

    /// Hold `text`, inline when it is one character.
    fn try_from_str(text: &str) -> Result<Self, Error> {
        let mut chars = text.chars();
        if let (Some(ch), None) = (chars.next(), chars.next()) {
            return Ok(Self::Char(ch));
        }
        let mut owned = String::new();
        owned
            .try_reserve_exact(text.len())
            .map_err(|error| Error::Allocation(format!("failed to copy glyph text: {error}")))?;
        owned.push_str(text);
        Ok(Self::Heap(owned.into_boxed_str()))
    }

    /// UTF-8 byte length.
    fn len(&self) -> usize {
        match self {
            Self::Char(c) => c.len_utf8(),
            Self::Heap(s) => s.len(),
        }
    }

    fn is_empty(&self) -> bool {
        match self {
            Self::Char(_) => false,
            Self::Heap(s) => s.is_empty(),
        }
    }

    fn chars(&self) -> GlyphTextChars<'_> {
        match self {
            Self::Char(c) => GlyphTextChars::Char(Some(*c)),
            Self::Heap(s) => GlyphTextChars::Str(s.chars()),
        }
    }

    fn ends_with(&self, needle: char) -> bool {
        match self {
            Self::Char(c) => *c == needle,
            Self::Heap(s) => s.ends_with(needle),
        }
    }

    fn starts_with(&self, needle: char) -> bool {
        match self {
            Self::Char(c) => *c == needle,
            Self::Heap(s) => s.starts_with(needle),
        }
    }

    /// Number of trailing ASCII space bytes.
    fn trailing_spaces(&self) -> usize {
        match self {
            Self::Char(c) => usize::from(*c == ' '),
            Self::Heap(s) => s.bytes().rev().take_while(|byte| *byte == b' ').count(),
        }
    }
}

/// Compare textual content, so a one-character CMap string equals its inline
/// character representation.
impl PartialEq for GlyphText {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Char(left), Self::Char(right)) => left == right,
            (Self::Heap(left), Self::Heap(right)) => left == right,
            (Self::Char(c), Self::Heap(s)) | (Self::Heap(s), Self::Char(c)) => {
                let mut encoded = [0u8; 4];
                c.encode_utf8(&mut encoded).as_bytes() == s.as_bytes()
            }
        }
    }
}

/// Character iterator over either glyph-text representation.
enum GlyphTextChars<'a> {
    Char(Option<char>),
    Str(std::str::Chars<'a>),
}

impl Iterator for GlyphTextChars<'_> {
    type Item = char;
    fn next(&mut self) -> Option<char> {
        match self {
            Self::Char(c) => c.take(),
            Self::Str(chars) => chars.next(),
        }
    }
}

/// What a glyph without a Unicode mapping becomes.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Default)]
pub enum Unmapped {
    /// Leave it out, counting it in [`PageText::skipped`].
    #[default]
    Skip,
    /// U+FFFD REPLACEMENT CHARACTER.
    Replacement,
    /// The character whose code point is the glyph ID, as pymupdf's
    /// `TEXT_USE_GID_FOR_UNKNOWN_UNICODE` gives it; U+FFFD for a Type 3 glyph
    /// or a glyph ID that is no character.
    GlyphId,
}

/// The options of the text of a page.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct TextOptions {
    /// Keep the glyphs whose box's center lies inside this display box.
    pub clip: Option<BBox>,
    /// Keep glyphs that the page boundary or the clip paths hide entirely.
    pub keep_clipped: bool,
    pub unmapped: Unmapped,
    /// Expand the ligatures of U+FB00 to U+FB06 to their letters.
    pub expand_ligatures: bool,
    /// Make every whitespace character a space.
    pub normalize_whitespace: bool,
    /// Join a word that a hyphen splits at the end of a line.
    pub dehyphenate: bool,
}

impl TextOptions {
    fn changes_lines(&self) -> bool {
        self.clip.is_some()
            || self.expand_ligatures
            || self.normalize_whitespace
            || self.dehyphenate
    }
}

/// One collected glyph in top-left-origin, downward-y display coordinates after
/// the renderer's `initial_transform`, with page rotation resolved.
struct GlyphRecord {
    /// Unicode representation; ligatures may contain multiple characters.
    text: GlyphText,
    /// Cached Unicode bidi properties used by logical-order assembly.
    bidi_flags: u8,
    /// Baseline-origin x.
    x: f64,
    /// Baseline-origin y, increasing downward.
    y: f64,
    /// Device-space font size used by line and word thresholds.
    size: f64,
    /// Device-space advance, estimated from size for unknown fonts.
    advance: f64,
    /// Unit baseline direction in display space.
    direction: (f64, f64),
    /// PDF writing mode: 0 for horizontal, 1 for vertical.
    ///
    /// Hayro currently does not expose WMode. This remains 0 for normal and
    /// rotated horizontal text, and is set to 1 only for conservative CJK
    /// vertical-layout detections during line assembly.
    writing_mode: u8,
    /// Font identity key for span splitting; Type 3 uses 0.
    font_key: u128,
    /// The line metrics of the glyph's font.
    font: FontInfo,
    /// The `RENDER_*` mode of the run that painted the glyph.
    render_mode: u8,
    /// Whether the clip paths leave no part of the glyph's box visible.
    is_clipped: bool,
    /// Whether the glyph continues the word of the line before, which
    /// dehyphenation removed the hyphen of.
    joins_previous: bool,
    /// Glyph callback order before geometric clustering.
    source_order: usize,
}

fn try_push_text<T>(values: &mut Vec<T>, value: T, label: &str) -> Result<(), Error> {
    if values.len() == values.capacity() {
        values
            .try_reserve(1)
            .map_err(|error| Error::Allocation(format!("failed to grow {label}: {error}")))?;
    }
    values.push(value);
    Ok(())
}

fn try_push_str_text(value: &mut String, text: &str, label: &str) -> Result<(), Error> {
    value
        .try_reserve(text.len())
        .map_err(|error| Error::Allocation(format!("failed to grow {label}: {error}")))?;
    value.push_str(text);
    Ok(())
}

fn try_push_glyph_text(value: &mut String, text: &GlyphText, label: &str) -> Result<(), Error> {
    match text {
        GlyphText::Char(c) => {
            value
                .try_reserve(c.len_utf8())
                .map_err(|error| Error::Allocation(format!("failed to grow {label}: {error}")))?;
            value.push(*c);
            Ok(())
        }
        GlyphText::Heap(s) => try_push_str_text(value, s, label),
    }
}

/// The content model's sink that collects the glyphs of a page.
struct TextCollector<'b> {
    glyphs: Vec<GlyphRecord>,
    /// font_key → FontInfo cache, resolving font_data only once per font.
    font_infos: HashMap<u128, FontInfo>,
    /// Most recent font lookup; consecutive glyphs usually share one font, so
    /// this skips two hash probes per glyph inside unbroken text runs.
    last_font: Option<(u128, FontInfo)>,
    /// Cumulative UTF-8 bytes in accepted glyph Unicode, charged to
    /// `text_size`.
    text_size: usize,
    budget: &'b Budget,
    /// The first refusal, which stops the collection.
    error: Option<Error>,
    /// Glyphs hayro delivered without any Unicode mapping, e.g. from a
    /// Type 3 font that has no ToUnicode CMap (LaurenzV/hayro#1331).
    skipped_no_unicode: usize,
    /// The render mode and clip of the glyphs being received.
    style: RunStyle,
    /// The index of the first glyph that the latest run recorded.
    run_start: usize,
    options: TextOptions,
    /// The visible page in display space, outside which glyphs are hidden.
    page_box: Rect,
}

/// The parts of a [`GlyphStyle`] that the collector keeps.
struct RunStyle {
    render_mode: u8,
    clip: Option<Rect>,
}

impl ContentSink for TextCollector<'_> {
    fn glyph_run(&mut self, style: &GlyphStyle) {
        self.style = RunStyle {
            render_mode: style.render_mode,
            clip: style.clip,
        };
        self.run_start = self.glyphs.len();
    }

    fn glyph(&mut self, glyph: &Glyph<'_>, transform: Affine, _: Option<Rect>) {
        if self.error.is_none()
            && let Err(error) = self.collect_glyph(glyph, transform)
        {
            self.error = Some(error);
        }
    }

    fn stroke_completes_fill(&mut self) {
        for glyph in &mut self.glyphs[self.run_start..] {
            if glyph.render_mode == RENDER_FILL {
                glyph.render_mode = RENDER_FILL_STROKE;
            }
        }
    }

    fn path(&mut self, _: &BezPath, _: Affine, _: &DrawMode) {}

    fn stopped(&self) -> bool {
        self.error.is_some()
    }
}

impl TextCollector<'_> {
    /// Record one glyph whose combined transform maps glyph space to display space.
    fn collect_glyph(&mut self, glyph: &Glyph<'_>, combined: Affine) -> Result<(), Error> {
        let unicode = match (glyph.as_unicode(), self.options.unmapped) {
            (Some(unicode), _) => unicode,
            (None, Unmapped::Skip) => {
                self.skipped_no_unicode = self.skipped_no_unicode.saturating_add(1);
                return Ok(());
            }
            (None, Unmapped::Replacement) => BfString::Char(char::REPLACEMENT_CHARACTER),
            (None, Unmapped::GlyphId) => BfString::Char(match glyph {
                Glyph::Outline(outline) => char::from_u32(outline.glyph_id().to_u32())
                    .unwrap_or(char::REPLACEMENT_CHARACTER),
                Glyph::Type3(_) => char::REPLACEMENT_CHARACTER,
            }),
        };
        let text = match unicode {
            BfString::Char(c) => GlyphText::Char(c),
            // Keep hayro's owned string, but never shrink it with an
            // infallible realloc: copy fallibly when capacity exceeds length.
            BfString::String(s) => {
                if s.capacity() == s.len() {
                    GlyphText::Heap(s.into_boxed_str())
                } else {
                    let mut exact = String::new();
                    exact.try_reserve_exact(s.len()).map_err(|error| {
                        Error::Allocation(format!("failed to allocate glyph text: {error}"))
                    })?;
                    exact.push_str(&s);
                    GlyphText::Heap(exact.into_boxed_str())
                }
            }
        };
        if text.is_empty() {
            return Ok(());
        }
        let origin = combined * Point::ZERO;
        if !origin.x.is_finite() || !origin.y.is_finite() {
            return Ok(());
        }
        // Font size is the transformed y-basis length × 1000. Hayro normalizes
        // glyph space to 1000 upem, so the transform factor is actual size / 1000.
        let [a, b, c, d, _, _] = combined.as_coeffs();
        let mut size = (c * c + d * d).sqrt() * 1000.0;
        if !size.is_finite() || size <= 0.0 {
            size = 12.0;
        }
        let direction_length = (a * a + b * b).sqrt();
        let direction = if direction_length.is_finite() && direction_length > f64::EPSILON {
            normalize_direction((a / direction_length, b / direction_length))
        } else {
            (1.0, 0.0)
        };
        // Advance is the glyph-space advance transformed by the x basis.
        // Approximate missing fonts such as Type 3 as half the font size.
        let (advance_width, font_key, font) = match glyph {
            Glyph::Outline(g) => {
                let key = g.font_cache_key();
                let info = if let Some((last_key, last_info)) = self.last_font
                    && last_key == key
                {
                    last_info
                } else {
                    if !self.font_infos.contains_key(&key) {
                        self.font_infos.try_reserve(1).map_err(|error| {
                            Error::Allocation(format!(
                                "failed to grow extraction font cache: {error}"
                            ))
                        })?;
                    }
                    let info = *self
                        .font_infos
                        .entry(key)
                        .or_insert_with(|| font_info_of(g));
                    self.last_font = Some((key, info));
                    info
                };
                (g.advance_width().map(f64::from), key, info)
            }
            Glyph::Type3(_) => (None, 0, FontInfo::default()),
        };
        let advance = advance_width
            .map(|adv| {
                let moved = combined * Point::new(adv, 0.0);
                ((moved.x - origin.x).powi(2) + (moved.y - origin.y).powi(2)).sqrt()
            })
            .filter(|a| a.is_finite() && *a > 0.0)
            .unwrap_or(size * 0.5);
        // Context initial_transform already flips y and applies rotation, so
        // transformed coordinates are directly in top-left-origin display space.
        let source_order = self.glyphs.len();
        let bidi_flags = glyph_bidi_flags(&text);
        let mut record = GlyphRecord {
            text,
            bidi_flags,
            x: origin.x,
            y: origin.y,
            size,
            advance,
            direction,
            writing_mode: 0,
            font_key,
            font,
            render_mode: self.style.render_mode,
            is_clipped: false,
            joins_previous: false,
            source_order,
        };
        // A glyph is clipped away when the page or the clip paths leave no
        // part of its box visible.
        let visible = self
            .style
            .clip
            .map_or(self.page_box, |clip| clip.intersect(self.page_box));
        let (x0, y0, x1, y1) = font_bbox(&record);
        record.is_clipped =
            x1 <= visible.x0 || y1 <= visible.y0 || x0 >= visible.x1 || y0 >= visible.y1;
        if record.is_clipped && !self.options.keep_clipped {
            return Ok(());
        }
        let bytes = record.text.len();
        self.budget.charge(LimitCode::TextGlyphCount, 1)?;
        if let Err(refusal) = self.budget.charge(LimitCode::TextSize, as_u64(bytes)) {
            self.budget.release(LimitCode::TextGlyphCount, 1);
            return Err(refusal.into());
        }
        self.text_size += bytes;
        if let Err(error) = try_push_text(&mut self.glyphs, record, "positioned-glyph collection") {
            self.release_glyph(bytes);
            return Err(error);
        }
        Ok(())
    }

    /// Returns one glyph's charges to the budget.
    fn release_glyph(&mut self, bytes: usize) {
        self.budget.release(LimitCode::TextGlyphCount, 1);
        self.budget.release(LimitCode::TextSize, as_u64(bytes));
        self.text_size -= bytes;
    }
}

/// Converts a length to a budget amount.
fn as_u64(value: usize) -> u64 {
    u64::try_from(value).unwrap_or(u64::MAX)
}

/// Line threshold: baselines within this factor × font size share a line.
/// This absorbs super/subscripts while separating normal leading of 1.0 or more.
const LINE_TOLERANCE: f64 = 0.5;

/// Backward movement above this factor starts another source-order paint run.
const PAINT_RUN_RESET_TOLERANCE: f64 = 0.05;

/// Inline overlap above this factor keeps paint runs on separate logical lines.
const PAINT_LAYER_OVERLAP_TOLERANCE: f64 = 0.05;

/// Same-origin repeated glyphs below this factor still form separate runs.
const PAINT_RUN_SAME_ORIGIN_TOLERANCE: f64 = 0.0001;

/// Cross-axis tolerance when joining glyphs on one vertical baseline.
const VERTICAL_LINE_TOLERANCE: f64 = 0.35;

/// Minimum glyph count before inferring a CJK vertical writing mode.
const MIN_VERTICAL_CJK_GLYPHS: usize = 3;

/// Bound the conservative CJK geometry pass on pathological pages.
const MAX_VERTICAL_CJK_CANDIDATES: usize = 4096;

/// Word threshold: synthesize a space above this factor × font size.
/// Typical word gaps are about 0.25 em and kerning about ±0.05 em.
const WORD_GAP: f64 = 0.15;

/// Block threshold: leading above this factor × font size starts a paragraph.
const BLOCK_GAP: f64 = 1.5;

/// Split one baseline into separate line segments above this horizontal gap.
const LINE_SEGMENT_GAP: f64 = 2.0;

/// Minimum whitespace gutter relative to the typical font size.
const COLUMN_GUTTER: f64 = 1.5;

/// Narrow gaps count only when they align across several physical lines.
const NARROW_COLUMN_GAP: f64 = 0.35;

/// Ignore full-width headings and footers while discovering column gutters.
const MAX_COLUMN_LINE_WIDTH_RATIO: f64 = 0.75;

/// Avoid treating an isolated side note or indentation as a separate column.
const MIN_COLUMN_LINES: usize = 2;

/// Require stronger repeated evidence before accepting a sub-em column gutter.
const MIN_NARROW_COLUMN_LINES: usize = 4;

/// Reject aligned labels, values, and dot leaders as column evidence.
const MIN_NARROW_COLUMN_SIDE_GLYPHS: usize = 16;

/// Require real text on both sides instead of punctuation-only leaders.
const MIN_NARROW_COLUMN_SIDE_TEXT: usize = 4;

/// Exclude dense table rows with several plausible vertical separators.
const MAX_NARROW_COLUMN_GAPS_PER_LINE: usize = 1;

/// Identify dense support runs before joining separated parts of one region.
const MAX_NARROW_COLUMN_LEADING: f64 = 2.0;

/// Absorb glyph-advance overlap around an otherwise established gutter.
const NARROW_COLUMN_OVERLAP_TOLERANCE: f64 = 0.3;

/// Follow small horizontal drift in a confirmed scan/OCR column boundary.
const ESTABLISHED_COLUMN_DRIFT: f64 = 0.5;

/// Require column candidates to coexist vertically over this fraction.
const MIN_COLUMN_VERTICAL_OVERLAP: f64 = 0.25;

/// Keep spreadsheet-like rows row-major when a wide gutter bisects baseline
/// bands into more segments than a small multi-column layout produces.
const MAX_COLUMN_BAND_SEGMENTS: usize = 4;

/// The top and bottom of a line from the baseline, in em, for reading order,
/// which does not depend on font metrics.
const ASCENT: f64 = 0.8;
const DESCENT: f64 = 0.2;

impl GlyphRecord {
    /// The distances of a glyph box's top above and bottom below the baseline.
    ///
    /// Like pymupdf, the box spans the font's ascender and descender, scaled
    /// to one em together when they span less, and 0.9 and -0.1 em when the
    /// ascender is not positive, as in a glyphless OCR font.
    fn vertical_extent(&self) -> (f64, f64) {
        let (mut ascender, mut descender) = (self.font.ascender, self.font.descender);
        if ascender < 1e-3 {
            (ascender, descender) = (0.9, -0.1);
        }
        let height = ascender - descender;
        if height < 1.0 {
            ascender /= height;
            descender /= height;
        }
        (self.size * ascender, -self.size * descender)
    }
}

/// Snap near-axis direction components so common horizontal text stays exact.
fn normalize_direction((x, y): (f64, f64)) -> (f64, f64) {
    const AXIS_EPSILON: f64 = 1e-9;
    (
        if x.abs() < AXIS_EPSILON { 0.0 } else { x },
        if y.abs() < AXIS_EPSILON { 0.0 } else { y },
    )
}

/// Return whether a baseline direction is predominantly vertical.
fn has_vertical_baseline(glyph: &GlyphRecord) -> bool {
    glyph.direction.1.abs() > glyph.direction.0.abs()
}

/// A baseline more than about one degree away from both axes, as in text that
/// a page sets at an angle; a slightly skewed scan stays on its axis.
fn has_oblique_baseline(glyph: &GlyphRecord) -> bool {
    glyph.direction.0.abs().min(glyph.direction.1.abs()) > OBLIQUE_BASELINE
}

/// The smaller direction component above which a baseline is oblique.
const OBLIQUE_BASELINE: f64 = 0.02;

/// Cluster glyphs on oblique baselines into lines: glyphs whose directions
/// agree to a thousandth of a radian and whose baselines lie within the
/// line tolerance of each other, measured across their direction, share a
/// line, which is then ordered along its direction.
fn cluster_oblique(
    arena: &[GlyphRecord],
    mut glyphs: Vec<usize>,
) -> Result<Vec<Vec<usize>>, Error> {
    let angle = |glyph: &GlyphRecord| (glyph.direction.1.atan2(glyph.direction.0) * 1000.0).round();
    let offset = |glyph: &GlyphRecord| glyph.y * glyph.direction.0 - glyph.x * glyph.direction.1;
    glyphs.sort_unstable_by(|&left, &right| {
        let (l, r) = (&arena[left], &arena[right]);
        angle(l)
            .total_cmp(&angle(r))
            .then(offset(l).total_cmp(&offset(r)))
            .then(left.cmp(&right))
    });
    let mut lines: Vec<Vec<usize>> = Vec::new();
    let mut current: Option<(f64, f64)> = None;
    for index in glyphs {
        let glyph = &arena[index];
        let tolerance = glyph.size.max(1.0) * LINE_TOLERANCE;
        let key = (angle(glyph), offset(glyph));
        match current {
            Some((line_angle, line_offset))
                if line_angle == key.0 && (key.1 - line_offset).abs() <= tolerance =>
            {
                try_push_text(
                    lines
                        .last_mut()
                        .expect("a line was created immediately before"),
                    index,
                    "oblique line glyphs",
                )?;
            }
            _ => {
                current = Some(key);
                let mut line = Vec::new();
                try_push_text(&mut line, index, "oblique line glyphs")?;
                try_push_text(&mut lines, line, "oblique lines")?;
            }
        }
    }
    let mut split_lines = Vec::new();
    for line in lines {
        for split in split_overlapping_paint_layers(arena, line)? {
            try_push_text(&mut split_lines, split, "split oblique lines")?;
        }
    }
    Ok(split_lines)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum AxisOrientation {
    Right,
    Down,
    Left,
    Up,
}

impl AxisOrientation {
    fn from_direction((x, y): (f64, f64)) -> Option<Self> {
        const NEAR_AXIS: f64 = 0.999;
        if x >= NEAR_AXIS {
            Some(Self::Right)
        } else if y >= NEAR_AXIS {
            Some(Self::Down)
        } else if x <= -NEAR_AXIS {
            Some(Self::Left)
        } else if y <= -NEAR_AXIS {
            Some(Self::Up)
        } else {
            None
        }
    }

    /// Map display coordinates into logical inline/block coordinates.
    fn logical_point(self, x: f64, y: f64) -> (f64, f64) {
        match self {
            Self::Right => (x, y),
            Self::Down => (y, -x),
            Self::Left => (-x, -y),
            Self::Up => (-y, x),
        }
    }
}

fn uniform_axis_orientation(lines: &[Vec<GlyphRecord>]) -> Option<AxisOrientation> {
    let first = AxisOrientation::from_direction(lines.first()?.first()?.direction)?;
    lines
        .iter()
        .all(|line| {
            line.first()
                .and_then(|glyph| AxisOrientation::from_direction(glyph.direction))
                == Some(first)
        })
        .then_some(first)
}

/// Return whether all visible characters are CJK or full-width punctuation.
fn is_cjk_text(text: &GlyphText) -> bool {
    let mut saw_character = false;
    for ch in text.chars().filter(|ch| !ch.is_whitespace()) {
        saw_character = true;
        if !matches!(
            ch,
            '\u{2E80}'..='\u{2FFF}'
                | '\u{3000}'..='\u{30FF}'
                | '\u{31F0}'..='\u{31FF}'
                | '\u{3400}'..='\u{4DBF}'
                | '\u{4E00}'..='\u{9FFF}'
                | '\u{AC00}'..='\u{D7AF}'
                | '\u{F900}'..='\u{FAFF}'
                | '\u{FF00}'..='\u{FFEF}'
                | '\u{20000}'..='\u{3134F}'
        ) {
            return false;
        }
    }
    saw_character
}

/// Identify CJK glyphs that advance vertically even though hayro reports the
/// font's transformed horizontal basis. Requiring a local vertical neighbour
/// and no local horizontal neighbour avoids reclassifying normal CJK rows.
///
/// `glyphs` selects candidates from `arena`; returned values are arena indexes.
fn inferred_vertical_cjk_indices(
    arena: &[GlyphRecord],
    glyphs: &[usize],
) -> Result<Vec<usize>, Error> {
    let mut cjk_indices = Vec::new();
    for &index in glyphs {
        let glyph = &arena[index];
        if has_vertical_baseline(glyph) || !is_cjk_text(&glyph.text) {
            continue;
        }
        if cjk_indices.len() >= MAX_VERTICAL_CJK_CANDIDATES {
            return Ok(Vec::new());
        }
        try_push_text(&mut cjk_indices, index, "vertical-CJK candidate collection")?;
    }
    if cjk_indices.len() < MIN_VERTICAL_CJK_GLYPHS {
        return Ok(Vec::new());
    }

    let mut eligible = Vec::new();
    for &index in &cjk_indices {
        let glyph = &arena[index];
        let mut has_vertical_neighbour = false;
        let mut has_horizontal_neighbour = false;
        for &other_index in &cjk_indices {
            if other_index == index {
                continue;
            }
            let other = &arena[other_index];
            if other.font_key != glyph.font_key {
                continue;
            }
            let scale = glyph.size.max(other.size).max(1.0);
            let dx = (other.x - glyph.x).abs();
            let dy = (other.y - glyph.y).abs();
            if dx <= scale * VERTICAL_LINE_TOLERANCE && dy >= scale * 0.35 && dy <= scale * 1.35 {
                has_vertical_neighbour = true;
            }
            if dy <= scale * LINE_TOLERANCE && dx >= scale * 0.2 && dx <= scale * 1.8 {
                has_horizontal_neighbour = true;
            }
            if has_vertical_neighbour && has_horizontal_neighbour {
                break;
            }
        }
        if has_vertical_neighbour && !has_horizontal_neighbour {
            try_push_text(&mut eligible, index, "vertical-CJK eligibility collection")?;
        }
    }
    Ok(eligible)
}

/// Extract conservative CJK vertical chains from otherwise horizontal glyphs.
///
/// Accepted chains rewrite their arena records to a vertical direction and
/// writing mode 1. Record positions never move, so arena indexes stay valid
/// for every other in-flight index list.
fn extract_inferred_vertical_cjk(
    arena: &mut [GlyphRecord],
    glyphs: Vec<usize>,
) -> Result<(Vec<usize>, Vec<Vec<usize>>), Error> {
    let eligible = inferred_vertical_cjk_indices(arena, &glyphs)?;
    if eligible.len() < MIN_VERTICAL_CJK_GLYPHS {
        return Ok((glyphs, Vec::new()));
    }

    let mut selected = Vec::new();
    selected.try_reserve_exact(arena.len()).map_err(|error| {
        Error::Allocation(format!(
            "failed to allocate vertical-CJK selection mask: {error}"
        ))
    })?;
    selected.resize(arena.len(), false);
    for index in eligible {
        selected[index] = true;
    }
    let mut candidates = Vec::new();
    let mut remaining = Vec::new();
    for index in glyphs {
        let target = if selected[index] {
            &mut candidates
        } else {
            &mut remaining
        };
        try_push_text(target, index, "vertical-CJK glyph partition")?;
    }
    // Arena indexes equal collection source order, so comparing indexes is the
    // explicit source-order tie-breaker.
    candidates.sort_unstable_by(|&left, &right| {
        let (l, r) = (&arena[left], &arena[right]);
        l.x.total_cmp(&r.x)
            .then(l.font_key.cmp(&r.font_key))
            .then(l.y.total_cmp(&r.y))
            .then(left.cmp(&right))
    });

    let mut lines: Vec<Vec<usize>> = Vec::new();
    for index in candidates {
        let (glyph_font_key, glyph_x, glyph_size) = {
            let glyph = &arena[index];
            (glyph.font_key, glyph.x, glyph.size)
        };
        let matching_line = lines.iter_mut().find(|line| {
            let first = &arena[line[0]];
            first.font_key == glyph_font_key
                && (first.x - glyph_x).abs()
                    <= first.size.max(glyph_size).max(1.0) * VERTICAL_LINE_TOLERANCE
        });
        if let Some(line) = matching_line {
            try_push_text(line, index, "vertical-CJK line")?;
        } else {
            let mut line = Vec::new();
            try_push_text(&mut line, index, "vertical-CJK line")?;
            try_push_text(&mut lines, line, "vertical-CJK lines")?;
        }
    }

    let mut accepted = Vec::new();
    for mut line in lines {
        line.sort_unstable_by(|&left, &right| {
            arena[left]
                .y
                .total_cmp(&arena[right].y)
                .then(left.cmp(&right))
        });
        let continuous = line.windows(2).all(|pair| {
            let (first, second) = (&arena[pair[0]], &arena[pair[1]]);
            let scale = first.size.max(second.size).max(1.0);
            let gap = second.y - first.y;
            gap >= scale * 0.35 && gap <= scale * 1.35
        });
        if line.len() >= MIN_VERTICAL_CJK_GLYPHS && continuous {
            for &index in &line {
                arena[index].direction = (0.0, 1.0);
                arena[index].writing_mode = 1;
            }
            try_push_text(&mut accepted, line, "accepted vertical-CJK lines")?;
        } else {
            for index in line {
                try_push_text(
                    &mut remaining,
                    index,
                    "remaining horizontal glyph collection",
                )?;
            }
        }
    }
    Ok((remaining, accepted))
}

/// Group glyphs whose transformed baseline itself is vertical.
fn cluster_explicit_vertical(
    arena: &[GlyphRecord],
    mut glyphs: Vec<usize>,
) -> Result<Vec<Vec<usize>>, Error> {
    glyphs.sort_unstable_by(|&left, &right| {
        let (l, r) = (&arena[left], &arena[right]);
        r.x.total_cmp(&l.x)
            .then(l.y.total_cmp(&r.y))
            .then(left.cmp(&right))
    });
    let mut lines: Vec<Vec<usize>> = Vec::new();
    for index in glyphs {
        let glyph = &arena[index];
        let matching_line = lines.iter_mut().find(|line| {
            let first = &arena[line[0]];
            let scale = first.size.max(glyph.size).max(1.0);
            (first.x - glyph.x).abs() <= scale * VERTICAL_LINE_TOLERANCE
                && first.direction.0 * glyph.direction.0 + first.direction.1 * glyph.direction.1
                    > 0.9
        });
        if let Some(line) = matching_line {
            try_push_text(line, index, "explicit vertical line")?;
        } else {
            let mut line = Vec::new();
            try_push_text(&mut line, index, "explicit vertical line")?;
            try_push_text(&mut lines, line, "explicit vertical lines")?;
        }
    }
    let mut split_lines = Vec::new();
    for line in lines {
        for split in split_overlapping_paint_layers(arena, line)? {
            try_push_text(&mut split_lines, split, "split explicit vertical lines")?;
        }
    }
    Ok(split_lines)
}

struct PaintLayer {
    glyphs: Vec<usize>,
    intervals: Vec<(f64, f64, f64)>,
}

fn sort_line_inline(arena: &[GlyphRecord], line: &mut [usize]) {
    line.sort_unstable_by(|&left, &right| {
        glyph_progress(&arena[left])
            .total_cmp(&glyph_progress(&arena[right]))
            .then(left.cmp(&right))
    });
}

/// Glyph bounding box over arena indexes without materializing the records.
fn indexed_glyphs_bbox(arena: &[GlyphRecord], indexes: &[usize]) -> BBox {
    let mut x0 = f64::INFINITY;
    let mut y0 = f64::INFINITY;
    let mut x1 = f64::NEG_INFINITY;
    let mut y1 = f64::NEG_INFINITY;
    for &index in indexes {
        let (glyph_x0, glyph_y0, glyph_x1, glyph_y1) = font_bbox(&arena[index]);
        x0 = x0.min(glyph_x0);
        y0 = y0.min(glyph_y0);
        x1 = x1.max(glyph_x1);
        y1 = y1.max(glyph_y1);
    }
    (x0, y0, x1, y1)
}

fn paint_run_interval(arena: &[GlyphRecord], run: &[usize]) -> (f64, f64, f64) {
    let (x0, y0, x1, y1) = indexed_glyphs_bbox(arena, run);
    let vertical = run
        .first()
        .is_some_and(|&index| has_vertical_baseline(&arena[index]));
    let (start, end) = if vertical { (y0, y1) } else { (x0, x1) };
    let scale = run
        .iter()
        .map(|&index| arena[index].size)
        .fold(1.0, f64::max);
    (start, end, scale)
}

fn paint_intervals_overlap(
    (left_start, left_end, left_scale): (f64, f64, f64),
    (right_start, right_end, right_scale): (f64, f64, f64),
) -> bool {
    let overlap = left_end.min(right_end) - left_start.max(right_start);
    overlap > left_scale.max(right_scale) * PAINT_LAYER_OVERLAP_TOLERANCE
}

/// Preserve source-order text runs when geometry sorting would interleave them.
///
/// A PDF may paint complete strings more than once at the same baseline. The
/// old x/y sort grouped equal-position glyphs (`A1, B1, A2, B2`) and destroyed
/// both strings. Detect backward source-order resets, then greedily place
/// non-overlapping runs on one geometry-sorted line while retaining overlapping
/// runs as separate lines. This preserves distinct overprints instead of
/// deleting them.
fn split_overlapping_paint_layers(
    arena: &[GlyphRecord],
    mut line: Vec<usize>,
) -> Result<Vec<Vec<usize>>, Error> {
    if line.len() < 2 {
        sort_line_inline(arena, &mut line);
        let mut lines = Vec::new();
        try_push_text(&mut lines, line, "text paint layers")?;
        return Ok(lines);
    }
    // Arena indexes equal collection source order.
    line.sort_unstable();

    let mut runs: Vec<Vec<usize>> = Vec::new();
    for index in line {
        let glyph = &arena[index];
        let progress = glyph_progress(glyph);
        let scale = glyph.size.max(1.0);
        let previous = runs
            .last()
            .and_then(|run| run.last())
            .map(|&previous| &arena[previous]);
        let direction_changed = previous.is_some_and(|previous| {
            previous.direction.0 * glyph.direction.0 + previous.direction.1 * glyph.direction.1
                < 0.9
        });
        let moved_backward = previous.is_some_and(|previous| {
            progress < glyph_progress(previous) - scale * PAINT_RUN_RESET_TOLERANCE
        });
        let repeated_at_same_origin = previous.is_some_and(|previous| {
            (glyph.x - previous.x).abs() <= scale * PAINT_RUN_SAME_ORIGIN_TOLERANCE
                && (glyph.y - previous.y).abs() <= scale * PAINT_RUN_SAME_ORIGIN_TOLERANCE
                && glyph.text == previous.text
        });
        if runs.is_empty() || moved_backward || repeated_at_same_origin || direction_changed {
            try_push_text(&mut runs, Vec::new(), "text paint runs")?;
        }
        try_push_text(
            runs.last_mut()
                .expect("a paint run was created immediately before"),
            index,
            "text paint-run glyphs",
        )?;
    }

    if runs.len() == 1 {
        let mut line = runs.pop().expect("one paint run exists");
        sort_line_inline(arena, &mut line);
        try_push_text(&mut runs, line, "text paint layers")?;
        return Ok(runs);
    }

    let mut layers: Vec<PaintLayer> = Vec::new();
    for run in runs {
        let interval = paint_run_interval(arena, &run);
        // Merge only into the latest layer. Searching older compatible layers
        // would move this run before an intervening overprint.
        let compatible = layers.last_mut().filter(|layer| {
            layer
                .intervals
                .iter()
                .all(|existing| !paint_intervals_overlap(*existing, interval))
        });
        if let Some(layer) = compatible {
            for index in run {
                try_push_text(&mut layer.glyphs, index, "text paint-layer glyphs")?;
            }
            try_push_text(&mut layer.intervals, interval, "text paint-layer intervals")?;
        } else {
            let mut intervals = Vec::new();
            try_push_text(&mut intervals, interval, "text paint-layer intervals")?;
            try_push_text(
                &mut layers,
                PaintLayer {
                    glyphs: run,
                    intervals,
                },
                "text paint layers",
            )?;
        }
    }
    let mut split = Vec::new();
    for mut layer in layers {
        sort_line_inline(arena, &mut layer.glyphs);
        try_push_text(&mut split, layer.glyphs, "split text paint layers")?;
    }
    Ok(split)
}

/// Group glyphs into physical text lines.
///
/// Partitioning, sorting, and run/layer splitting work on arena indexes so the
/// roughly 128-byte records are not moved through every intermediate
/// collection; `materialize_lines` then moves each record once into its final
/// clustered line.
fn cluster_lines(glyphs: Vec<GlyphRecord>) -> Result<Vec<Vec<GlyphRecord>>, Error> {
    let mut arena = glyphs;
    let mut explicit_vertical: Vec<usize> = Vec::new();
    let mut horizontal: Vec<usize> = Vec::new();
    let mut oblique: Vec<usize> = Vec::new();
    for (index, glyph) in arena.iter().enumerate() {
        if has_oblique_baseline(glyph) {
            try_push_text(&mut oblique, index, "oblique glyph partition")?;
        } else if has_vertical_baseline(glyph) {
            try_push_text(
                &mut explicit_vertical,
                index,
                "explicit vertical glyph partition",
            )?;
        } else {
            try_push_text(&mut horizontal, index, "horizontal glyph partition")?;
        }
    }
    let (mut horizontal, mut vertical_lines) =
        extract_inferred_vertical_cjk(&mut arena, horizontal)?;

    horizontal.sort_unstable_by(|&left, &right| {
        let (l, r) = (&arena[left], &arena[right]);
        l.y.total_cmp(&r.y)
            .then(l.x.total_cmp(&r.x))
            .then(left.cmp(&right))
    });
    let mut lines: Vec<Vec<usize>> = Vec::new();
    let mut current_baseline = f64::NEG_INFINITY;
    for index in horizontal {
        let glyph = &arena[index];
        let tolerance = glyph.size.max(1.0) * LINE_TOLERANCE;
        if (glyph.y - current_baseline).abs() <= tolerance {
            try_push_text(
                lines
                    .last_mut()
                    .expect("a line was created immediately before"),
                index,
                "physical text-line glyphs",
            )?;
        } else {
            current_baseline = glyph.y;
            let mut line = Vec::new();
            try_push_text(&mut line, index, "physical text-line glyphs")?;
            try_push_text(&mut lines, line, "physical text lines")?;
        }
    }
    let mut all_lines: Vec<Vec<usize>> = Vec::new();
    for line in lines {
        for split in split_overlapping_paint_layers(&arena, line)? {
            try_push_text(&mut all_lines, split, "split horizontal text lines")?;
        }
    }
    for line in cluster_explicit_vertical(&arena, explicit_vertical)? {
        try_push_text(&mut vertical_lines, line, "vertical text lines")?;
    }
    for line in cluster_oblique(&arena, oblique)? {
        try_push_text(&mut vertical_lines, line, "oblique text lines")?;
    }
    for line in vertical_lines {
        try_push_text(&mut all_lines, line, "clustered text lines")?;
    }
    materialize_lines(arena, all_lines)
}

/// Move each collected glyph once from the arena into its clustered line.
fn materialize_lines(
    arena: Vec<GlyphRecord>,
    lines: Vec<Vec<usize>>,
) -> Result<Vec<Vec<GlyphRecord>>, Error> {
    let mut slots = Vec::new();
    slots.try_reserve_exact(arena.len()).map_err(|error| {
        Error::Allocation(format!(
            "failed to allocate glyph materialization slots: {error}"
        ))
    })?;
    slots.extend(arena.into_iter().map(Some));
    let mut output = Vec::new();
    output.try_reserve_exact(lines.len()).map_err(|error| {
        Error::Allocation(format!("failed to allocate clustered text lines: {error}"))
    })?;
    for line in lines {
        let mut materialized = Vec::new();
        materialized
            .try_reserve_exact(line.len())
            .map_err(|error| {
                Error::Allocation(format!(
                    "failed to allocate clustered text-line glyphs: {error}"
                ))
            })?;
        for index in line {
            materialized.push(
                slots[index]
                    .take()
                    .expect("each glyph index is clustered into exactly one line"),
            );
        }
        output.push(materialized);
    }
    Ok(output)
}

/// Split one owned physical line without cloning its glyph strings or metadata.
fn split_line_segments_owned(line: Vec<GlyphRecord>) -> Result<Vec<Vec<GlyphRecord>>, Error> {
    let mut segments: Vec<Vec<GlyphRecord>> = Vec::new();
    let mut previous_end: Option<f64> = None;
    let mut previous_size = 0.0_f64;
    for glyph in line {
        let threshold = previous_size.max(glyph.size).max(1.0) * LINE_SEGMENT_GAP;
        let starts_segment = segments.is_empty()
            || previous_end.is_some_and(|end| glyph_progress(&glyph) - end > threshold);
        previous_end = Some(glyph_end(&glyph));
        previous_size = glyph.size;
        if starts_segment {
            try_push_text(&mut segments, Vec::new(), "owned text-line segments")?;
        }
        try_push_text(
            segments
                .last_mut()
                .expect("a segment was created immediately before"),
            glyph,
            "owned text-line segment glyphs",
        )?;
    }
    Ok(segments)
}

/// Count widely separated segments without cloning their glyphs.
fn line_segment_count(line: &[GlyphRecord]) -> usize {
    let mut count = usize::from(!line.is_empty());
    let mut previous_end: Option<f64> = None;
    let mut previous_size = 0.0_f64;
    for glyph in line {
        let threshold = previous_size.max(glyph.size).max(1.0) * LINE_SEGMENT_GAP;
        if previous_end.is_some_and(|end| glyph_progress(glyph) - end > threshold) {
            count += 1;
        }
        previous_end = Some(glyph_end(glyph));
        previous_size = glyph.size;
    }
    count
}

/// Split baseline bands only when a sustained page-level column gutter exists.
fn order_page_lines(clustered: Vec<Vec<GlyphRecord>>) -> Result<Vec<Vec<GlyphRecord>>, Error> {
    if let Some(orientation) = uniform_axis_orientation(&clustered) {
        return order_axis_page_lines(clustered, orientation);
    }
    if clustered
        .iter()
        .any(|line| line.first().is_some_and(has_vertical_baseline))
    {
        return order_vertical_page_lines(clustered);
    }
    order_axis_page_lines(clustered, AxisOrientation::Right)
}

/// Order a uniformly axis-aligned page in its logical inline/block space.
fn order_axis_page_lines(
    clustered: Vec<Vec<GlyphRecord>>,
    orientation: AxisOrientation,
) -> Result<Vec<Vec<GlyphRecord>>, Error> {
    // Precompute each line's geometry key once instead of rescanning both
    // lines' glyphs inside every sort comparison.
    let mut decorated = Vec::new();
    decorated
        .try_reserve_exact(clustered.len())
        .map_err(|error| {
            Error::Allocation(format!("failed to allocate line ordering keys: {error}"))
        })?;
    for line in clustered {
        let bbox = logical_line_bbox(&line, orientation);
        let source_order = line_source_order(&line);
        decorated.push((bbox.1, bbox.0, source_order, line));
    }
    decorated.sort_unstable_by(|left, right| {
        left.0
            .total_cmp(&right.0)
            .then(left.1.total_cmp(&right.1))
            .then(left.2.cmp(&right.2))
    });
    let mut clustered = Vec::new();
    clustered
        .try_reserve_exact(decorated.len())
        .map_err(|error| {
            Error::Allocation(format!("failed to allocate ordered text lines: {error}"))
        })?;
    clustered.extend(decorated.into_iter().map(|(_, _, _, line)| line));
    if let Some(boundary) = narrow_column_boundary(&clustered, orientation)? {
        return order_narrow_column_regions(clustered, orientation, boundary);
    }
    if clustered
        .iter()
        .filter(|line| line_segment_count(line) > 1)
        .take(MIN_COLUMN_LINES)
        .count()
        < MIN_COLUMN_LINES
    {
        return Ok(clustered);
    }
    let mut geometry = Vec::new();
    for line in &clustered {
        append_segment_geometry(line, orientation, &mut geometry)?;
    }
    let Some(boundary) = geometry_column_boundary(&geometry)? else {
        return Ok(clustered);
    };
    if !valid_geometry_column_split(&geometry, boundary)? {
        return Ok(clustered);
    }
    let mut segments = Vec::new();
    for line in clustered {
        for segment in split_line_segments_owned(line)? {
            try_push_text(&mut segments, segment, "page text-line segments")?;
        }
    }
    order_columns(segments, orientation)
}

/// Order vertical columns right-to-left, preserving horizontal headers and
/// footers outside the vertical text region.
fn order_vertical_page_lines(
    clustered: Vec<Vec<GlyphRecord>>,
) -> Result<Vec<Vec<GlyphRecord>>, Error> {
    let mut vertical_y0 = f64::INFINITY;
    let mut vertical_y1 = f64::NEG_INFINITY;
    let mut has_vertical = false;
    for line in &clustered {
        if line.first().is_some_and(has_vertical_baseline) {
            let (_, y0, _, y1) = line_bbox(line);
            vertical_y0 = vertical_y0.min(y0);
            vertical_y1 = vertical_y1.max(y1);
            has_vertical = true;
        }
    }
    if !has_vertical {
        vertical_y0 = f64::NEG_INFINITY;
        vertical_y1 = f64::INFINITY;
    }

    let mut top = Vec::new();
    let mut vertical = Vec::new();
    let mut middle = Vec::new();
    let mut bottom = Vec::new();
    for line in clustered {
        if line.first().is_some_and(has_vertical_baseline) {
            try_push_text(&mut vertical, line, "ordered vertical text lines")?;
            continue;
        }
        let (_, y0, _, y1) = line_bbox(&line);
        if y1 <= vertical_y0 {
            try_push_text(&mut top, line, "text lines above vertical content")?;
        } else if y0 >= vertical_y1 {
            try_push_text(&mut bottom, line, "text lines below vertical content")?;
        } else {
            try_push_text(&mut middle, line, "text lines beside vertical content")?;
        }
    }
    let mut top = sort_lines_by_bbox_key(top, |line| line_bbox(line).1, false)?;
    let vertical = sort_lines_by_bbox_key(vertical, |line| line_bbox(line).0, true)?;
    let middle = sort_lines_by_bbox_key(middle, |line| line_bbox(line).1, false)?;
    let bottom = sort_lines_by_bbox_key(bottom, |line| line_bbox(line).1, false)?;
    for line in vertical {
        try_push_text(&mut top, line, "ordered mixed-orientation text lines")?;
    }
    for line in middle {
        try_push_text(&mut top, line, "ordered mixed-orientation text lines")?;
    }
    for line in bottom {
        try_push_text(&mut top, line, "ordered mixed-orientation text lines")?;
    }
    Ok(top)
}

/// Return a line's bbox without exposing the internal glyph representation.
fn line_bbox(line: &[GlyphRecord]) -> BBox {
    let mut x0 = f64::INFINITY;
    let mut y0 = f64::INFINITY;
    let mut x1 = f64::NEG_INFINITY;
    let mut y1 = f64::NEG_INFINITY;
    for glyph in line {
        let (glyph_x0, glyph_y0, glyph_x1, glyph_y1) = font_bbox(glyph);
        x0 = x0.min(glyph_x0);
        y0 = y0.min(glyph_y0);
        x1 = x1.max(glyph_x1);
        y1 = y1.max(glyph_y1);
    }
    (x0, y0, x1, y1)
}

/// Sort whole lines by one precomputed geometry key with the explicit
/// source-order tie-breaker, scanning each line's glyphs once instead of on
/// every comparison.
fn sort_lines_by_bbox_key(
    lines: Vec<Vec<GlyphRecord>>,
    key: impl Fn(&[GlyphRecord]) -> f64,
    descending: bool,
) -> Result<Vec<Vec<GlyphRecord>>, Error> {
    let mut decorated = Vec::new();
    decorated.try_reserve_exact(lines.len()).map_err(|error| {
        Error::Allocation(format!("failed to allocate line ordering keys: {error}"))
    })?;
    for line in lines {
        let key_value = key(&line);
        let source_order = line_source_order(&line);
        decorated.push((key_value, source_order, line));
    }
    decorated.sort_unstable_by(|left, right| {
        let ordering = if descending {
            right.0.total_cmp(&left.0)
        } else {
            left.0.total_cmp(&right.0)
        };
        ordering.then(left.1.cmp(&right.1))
    });
    let mut lines = Vec::new();
    lines.try_reserve_exact(decorated.len()).map_err(|error| {
        Error::Allocation(format!("failed to allocate ordered text lines: {error}"))
    })?;
    lines.extend(decorated.into_iter().map(|(_, _, line)| line));
    Ok(lines)
}

/// Earliest callback order in one non-empty physical line.
fn line_source_order(line: &[GlyphRecord]) -> usize {
    line.iter()
        .map(|glyph| glyph.source_order)
        .min()
        .unwrap_or(usize::MAX)
}

/// Return a line bbox in logical inline/block coordinates.
fn logical_line_bbox(line: &[GlyphRecord], orientation: AxisOrientation) -> BBox {
    let mut u0 = f64::INFINITY;
    let mut v0 = f64::INFINITY;
    let mut u1 = f64::NEG_INFINITY;
    let mut v1 = f64::NEG_INFINITY;
    for glyph in line {
        let (u, v) = orientation.logical_point(glyph.x, glyph.y);
        u0 = u0.min(u);
        u1 = u1.max(u + glyph.advance);
        v0 = v0.min(v - glyph.size * ASCENT);
        v1 = v1.max(v + glyph.size * DESCENT);
    }
    (u0, v0, u1, v1)
}

#[derive(Clone, Copy)]
struct LineGeometry {
    bbox: BBox,
    size: f64,
}

#[derive(Clone, Copy)]
struct ColumnGapEvent {
    position: f64,
    starts: bool,
}

#[derive(Clone, Copy)]
struct NarrowColumnBoundary {
    position: f64,
    first_center: f64,
    last_center: f64,
}

#[derive(Clone, Copy)]
struct NarrowColumnSupport {
    center: f64,
    size: f64,
}

fn line_geometry(line: &[GlyphRecord], orientation: AxisOrientation) -> LineGeometry {
    LineGeometry {
        bbox: logical_line_bbox(line, orientation),
        size: line
            .iter()
            .map(|glyph| glyph.size)
            .filter(|size| size.is_finite() && *size > 0.0)
            .reduce(f64::max)
            .unwrap_or(12.0),
    }
}

fn append_segment_geometry(
    line: &[GlyphRecord],
    orientation: AxisOrientation,
    output: &mut Vec<LineGeometry>,
) -> Result<(), Error> {
    let mut segment_start = 0usize;
    let mut previous_end: Option<f64> = None;
    let mut previous_size = 0.0_f64;
    for (index, glyph) in line.iter().enumerate() {
        let threshold = previous_size.max(glyph.size).max(1.0) * LINE_SEGMENT_GAP;
        if previous_end.is_some_and(|end| glyph_progress(glyph) - end > threshold) {
            try_push_text(
                output,
                line_geometry(&line[segment_start..index], orientation),
                "text segment geometry",
            )?;
            segment_start = index;
        }
        previous_end = Some(glyph_end(glyph));
        previous_size = glyph.size;
    }
    if segment_start < line.len() {
        try_push_text(
            output,
            line_geometry(&line[segment_start..], orientation),
            "text segment geometry",
        )?;
    }
    Ok(())
}

fn narrow_gap_index(line: &[GlyphRecord], boundary: f64) -> Option<usize> {
    let gap_count = narrow_gap_count(line);
    if gap_count == 0 || gap_count > MAX_NARROW_COLUMN_GAPS_PER_LINE {
        return None;
    }
    line.windows(2).enumerate().find_map(|(index, pair)| {
        let (start, end) = narrow_gap_interval(line, index, pair)?;
        (start <= boundary && boundary <= end).then_some(index + 1)
    })
}

fn narrow_gap_count(line: &[GlyphRecord]) -> usize {
    line.windows(2)
        .enumerate()
        .filter(|(index, pair)| narrow_gap_interval(line, *index, pair).is_some())
        .take(MAX_NARROW_COLUMN_GAPS_PER_LINE + 1)
        .count()
}

fn narrow_gap_interval(
    line: &[GlyphRecord],
    index: usize,
    pair: &[GlyphRecord],
) -> Option<(f64, f64)> {
    let split_index = index + 1;
    if split_index < MIN_NARROW_COLUMN_SIDE_GLYPHS
        || line.len() - split_index < MIN_NARROW_COLUMN_SIDE_GLYPHS
        || !has_narrow_column_text(&line[..split_index])
        || !has_narrow_column_text(&line[split_index..])
    {
        return None;
    }
    let start = glyph_end(&pair[0]);
    let end = glyph_progress(&pair[1]);
    let minimum = pair[0].size.max(pair[1].size).max(1.0) * NARROW_COLUMN_GAP;
    (end - start >= minimum).then_some((start, end))
}

fn has_narrow_column_text(glyphs: &[GlyphRecord]) -> bool {
    glyphs
        .iter()
        .flat_map(|glyph| glyph.text.chars())
        .filter(|character| character.is_alphanumeric())
        .take(MIN_NARROW_COLUMN_SIDE_TEXT)
        .count()
        == MIN_NARROW_COLUMN_SIDE_TEXT
}

fn boundary_distance(start: f64, end: f64, boundary: f64) -> f64 {
    if boundary < start {
        start - boundary
    } else if boundary > end {
        boundary - end
    } else {
        0.0
    }
}

fn established_column_gap_index(line: &[GlyphRecord], boundary: f64) -> Option<usize> {
    let gaps = line.windows(2).enumerate().filter_map(|(index, pair)| {
        let split_index = index + 1;
        if split_index < 2 || line.len() - split_index < 2 {
            return None;
        }
        let start = glyph_end(&pair[0]);
        let end = glyph_progress(&pair[1]);
        let tolerance = pair[0].size.max(pair[1].size).max(1.0) * ESTABLISHED_COLUMN_DRIFT;
        let distance = boundary_distance(start, end, boundary);
        (end >= start && distance <= tolerance).then_some((distance, split_index))
    });
    let whitespace = line.iter().enumerate().filter_map(|(index, glyph)| {
        let split_index = index + 1;
        if split_index < 2 || line.len() - split_index < 2 {
            return None;
        }
        let whitespace = !glyph.text.is_empty() && glyph.text.chars().all(char::is_whitespace);
        let start = glyph_progress(glyph);
        let end = glyph_end(glyph);
        let tolerance = glyph.size.max(1.0) * ESTABLISHED_COLUMN_DRIFT;
        let distance = boundary_distance(start, end, boundary);
        (whitespace && distance <= tolerance).then_some((distance, split_index))
    });
    gaps.chain(whitespace)
        .min_by(|left, right| left.0.total_cmp(&right.0))
        .map(|(_, index)| index)
}

/// Find a sub-em gutter only when the same empty interval persists across
/// several merged physical lines. This distinguishes narrow newspaper-style
/// columns from ordinary word spaces without lowering the general line split.
fn narrow_column_boundary(
    lines: &[Vec<GlyphRecord>],
    orientation: AxisOrientation,
) -> Result<Option<NarrowColumnBoundary>, Error> {
    let mut events = Vec::new();
    for line in lines {
        let gap_count = narrow_gap_count(line);
        if gap_count == 0 || gap_count > MAX_NARROW_COLUMN_GAPS_PER_LINE {
            continue;
        }
        for (index, pair) in line.windows(2).enumerate() {
            let Some((start, end)) = narrow_gap_interval(line, index, pair) else {
                continue;
            };
            try_push_text(
                &mut events,
                ColumnGapEvent {
                    position: start,
                    starts: true,
                },
                "narrow-column gap events",
            )?;
            try_push_text(
                &mut events,
                ColumnGapEvent {
                    position: end,
                    starts: false,
                },
                "narrow-column gap events",
            )?;
        }
    }
    if events.len() < MIN_NARROW_COLUMN_LINES * 2 {
        return Ok(None);
    }
    events.sort_unstable_by(|left, right| {
        left.position
            .total_cmp(&right.position)
            .then(right.starts.cmp(&left.starts))
    });
    let mut active = 0usize;
    let mut strongest = 0usize;
    let mut position = 0.0;
    for event in events {
        if event.starts {
            active += 1;
            if active > strongest {
                strongest = active;
                position = event.position;
            }
        } else {
            active = active.saturating_sub(1);
        }
    }
    if strongest < MIN_NARROW_COLUMN_LINES {
        return Ok(None);
    }

    let mut supports = Vec::new();
    for line in lines {
        if let Some(index) = narrow_gap_index(line, position) {
            let left_geometry = line_geometry(&line[..index], orientation);
            let right_geometry = line_geometry(&line[index..], orientation);
            let center = (left_geometry.bbox.1
                + left_geometry.bbox.3
                + right_geometry.bbox.1
                + right_geometry.bbox.3)
                * 0.25;
            try_push_text(
                &mut supports,
                NarrowColumnSupport {
                    center,
                    size: left_geometry.size.max(right_geometry.size),
                },
                "narrow-column support lines",
            )?;
        }
    }
    if supports.len() < MIN_NARROW_COLUMN_LINES {
        return Ok(None);
    }
    supports.sort_unstable_by(|left, right| left.center.total_cmp(&right.center));
    let mut first_dense_start = None;
    let mut last_dense_end = None;
    let mut group_start = 0usize;
    for index in 1..=supports.len() {
        let group_ended = if index == supports.len() {
            true
        } else {
            let previous = supports[index - 1];
            let current = supports[index];
            let maximum_leading =
                previous.size.max(current.size).max(1.0) * MAX_NARROW_COLUMN_LEADING;
            current.center - previous.center > maximum_leading
        };
        if !group_ended {
            continue;
        }
        if index - group_start >= MIN_NARROW_COLUMN_LINES {
            first_dense_start.get_or_insert(group_start);
            last_dense_end = Some(index);
        }
        group_start = index;
    }
    let (Some(region_start), Some(region_end)) = (first_dense_start, last_dense_end) else {
        return Ok(None);
    };
    let mut first_center = supports[region_start].center;
    let mut first_size = supports[region_start].size;
    let mut last_center = supports[region_end - 1].center;
    let mut last_size = supports[region_end - 1].size;
    let mut established = Vec::new();
    for line in lines {
        if established_column_gap_index(line, position).is_some() {
            let geometry = line_geometry(line, orientation);
            try_push_text(
                &mut established,
                NarrowColumnSupport {
                    center: (geometry.bbox.1 + geometry.bbox.3) * 0.5,
                    size: geometry.size,
                },
                "established narrow-column lines",
            )?;
        }
    }
    established.sort_unstable_by(|left, right| left.center.total_cmp(&right.center));
    for candidate in established.iter().rev() {
        if candidate.center >= first_center {
            continue;
        }
        let maximum_leading = candidate.size.max(first_size).max(1.0) * MAX_NARROW_COLUMN_LEADING;
        if first_center - candidate.center > maximum_leading {
            break;
        }
        first_center = candidate.center;
        first_size = candidate.size;
    }
    for candidate in &established {
        if candidate.center <= last_center {
            continue;
        }
        let maximum_leading = candidate.size.max(last_size).max(1.0) * MAX_NARROW_COLUMN_LEADING;
        if candidate.center - last_center > maximum_leading {
            break;
        }
        last_center = candidate.center;
        last_size = candidate.size;
    }

    let mut support = 0usize;
    let mut geometry = Vec::new();
    for line in lines {
        let whole_geometry = line_geometry(line, orientation);
        let center = (whole_geometry.bbox.1 + whole_geometry.bbox.3) * 0.5;
        if center >= first_center
            && center <= last_center
            && let Some(index) = established_column_gap_index(line, position)
        {
            support += 1;
            try_push_text(
                &mut geometry,
                line_geometry(&line[..index], orientation),
                "narrow-column segment geometry",
            )?;
            try_push_text(
                &mut geometry,
                line_geometry(&line[index..], orientation),
                "narrow-column segment geometry",
            )?;
            continue;
        }
        try_push_text(
            &mut geometry,
            whole_geometry,
            "narrow-column segment geometry",
        )?;
    }
    if support < MIN_NARROW_COLUMN_LINES || !valid_geometry_column_split(&geometry, position)? {
        return Ok(None);
    }

    let has_middle_spanning = geometry.iter().any(|line| {
        let center = (line.bbox.1 + line.bbox.3) * 0.5;
        let tolerance = line.size.max(1.0) * NARROW_COLUMN_OVERLAP_TOLERANCE;
        line.bbox.0 < position - tolerance
            && line.bbox.2 > position + tolerance
            && center > first_center
            && center < last_center
    });
    if has_middle_spanning {
        return Ok(None);
    }
    Ok(Some(NarrowColumnBoundary {
        position,
        first_center,
        last_center,
    }))
}

fn split_owned_line_at(
    line: Vec<GlyphRecord>,
    index: usize,
) -> Result<(Vec<GlyphRecord>, Vec<GlyphRecord>), Error> {
    let mut first = Vec::new();
    first.try_reserve_exact(index).map_err(|error| {
        Error::Allocation(format!(
            "failed to allocate narrow-column line segment: {error}"
        ))
    })?;
    let mut second = Vec::new();
    second
        .try_reserve_exact(line.len() - index)
        .map_err(|error| {
            Error::Allocation(format!(
                "failed to allocate narrow-column line segment: {error}"
            ))
        })?;
    for (position, glyph) in line.into_iter().enumerate() {
        if position < index {
            first.push(glyph);
        } else {
            second.push(glyph);
        }
    }
    Ok((first, second))
}

fn order_narrow_column_regions(
    lines: Vec<Vec<GlyphRecord>>,
    orientation: AxisOrientation,
    boundary: NarrowColumnBoundary,
) -> Result<Vec<Vec<GlyphRecord>>, Error> {
    let mut top = Vec::new();
    let mut left = Vec::new();
    let mut right = Vec::new();
    let mut bottom = Vec::new();
    for line in lines {
        let geometry = line_geometry(&line, orientation);
        let center = (geometry.bbox.1 + geometry.bbox.3) * 0.5;
        if center >= boundary.first_center
            && center <= boundary.last_center
            && let Some(index) = established_column_gap_index(&line, boundary.position)
        {
            let (left_line, right_line) = split_owned_line_at(line, index)?;
            try_push_text(&mut left, left_line, "narrow left-column text lines")?;
            try_push_text(&mut right, right_line, "narrow right-column text lines")?;
            continue;
        }
        if center < boundary.first_center {
            try_push_text(&mut top, line, "narrow column-spanning headings")?;
        } else if center > boundary.last_center {
            try_push_text(&mut bottom, line, "narrow column-spanning footers")?;
        } else if geometry.bbox.2 <= boundary.position {
            try_push_text(&mut left, line, "narrow left-column text lines")?;
        } else if geometry.bbox.0 >= boundary.position {
            try_push_text(&mut right, line, "narrow right-column text lines")?;
        } else {
            try_push_text(&mut bottom, line, "narrow column-spanning footers")?;
        }
    }

    for line in order_axis_page_lines(left, orientation)? {
        try_push_text(&mut top, line, "ordered narrow-column text lines")?;
    }
    for line in order_axis_page_lines(right, orientation)? {
        try_push_text(&mut top, line, "ordered narrow-column text lines")?;
    }
    for line in bottom {
        try_push_text(&mut top, line, "ordered narrow-column text lines")?;
    }
    Ok(top)
}

fn typical_geometry_size(lines: &[LineGeometry]) -> Result<f64, Error> {
    let mut sizes = Vec::new();
    for line in lines {
        if line.size.is_finite() && line.size > 0.0 {
            try_push_text(&mut sizes, line.size, "text-line size sample")?;
        }
    }
    if sizes.is_empty() {
        return Ok(12.0);
    }
    sizes.sort_unstable_by(f64::total_cmp);
    Ok(sizes[sizes.len() / 2])
}

/// Find the strongest logical-inline whitespace gutter from lightweight bboxes.
fn geometry_column_boundary(lines: &[LineGeometry]) -> Result<Option<f64>, Error> {
    if lines.len() < MIN_COLUMN_LINES * 2 {
        return Ok(None);
    }
    let Some(region_x0) = lines.iter().map(|line| line.bbox.0).reduce(f64::min) else {
        return Ok(None);
    };
    let Some(region_x1) = lines.iter().map(|line| line.bbox.2).reduce(f64::max) else {
        return Ok(None);
    };
    let region_width = region_x1 - region_x0;
    if !region_width.is_finite() || region_width <= 0.0 {
        return Ok(None);
    }

    let mut intervals = Vec::new();
    for line in lines {
        let width = line.bbox.2 - line.bbox.0;
        if width <= region_width * MAX_COLUMN_LINE_WIDTH_RATIO {
            try_push_text(
                &mut intervals,
                (line.bbox.0, line.bbox.2),
                "text gutter intervals",
            )?;
        }
    }
    intervals.sort_unstable_by(|left, right| {
        left.0.total_cmp(&right.0).then(left.1.total_cmp(&right.1))
    });
    let Some((_, mut current_end)) = intervals.first().copied() else {
        return Ok(None);
    };
    let minimum_gap = (typical_geometry_size(lines)? * COLUMN_GUTTER).max(12.0);
    let mut best: Option<(f64, f64)> = None;
    for &(x0, x1) in intervals.iter().skip(1) {
        if x0 <= current_end {
            current_end = current_end.max(x1);
        } else {
            let gap = x0 - current_end;
            if gap >= minimum_gap && best.is_none_or(|(best_gap, _)| gap > best_gap) {
                best = Some((gap, (current_end + x0) * 0.5));
            }
            current_end = x1;
        }
    }
    Ok(best.map(|(_, boundary)| boundary))
}

fn geometry_side_extent(
    lines: &[LineGeometry],
    boundary: f64,
    left_side: bool,
) -> Option<(f64, f64, usize)> {
    let mut y0 = f64::INFINITY;
    let mut y1 = f64::NEG_INFINITY;
    let mut count = 0;
    for line in lines {
        let belongs = if left_side {
            line.bbox.2 <= boundary
        } else {
            line.bbox.0 >= boundary
        };
        if belongs {
            y0 = y0.min(line.bbox.1);
            y1 = y1.max(line.bbox.3);
            count += 1;
        }
    }
    (count > 0).then_some((y0, y1, count))
}

/// Median segment count of baseline bands with text on both sides of a
/// candidate gutter. Prose columns keep about one segment per side, while
/// spreadsheet rows fragment one baseline into many cell segments.
fn bisected_band_segment_median(
    lines: &[LineGeometry],
    boundary: f64,
) -> Result<Option<usize>, Error> {
    let mut segments = Vec::new();
    for line in lines {
        let on_left = line.bbox.2 <= boundary;
        let on_right = line.bbox.0 >= boundary;
        if on_left == on_right {
            continue;
        }
        let center = (line.bbox.1 + line.bbox.3) * 0.5;
        if center.is_finite() {
            try_push_text(
                &mut segments,
                (center, line.size, on_left),
                "column band segments",
            )?;
        }
    }
    segments.sort_unstable_by(|left, right| left.0.total_cmp(&right.0));
    let mut band_counts = Vec::new();
    let mut previous_center = f64::NEG_INFINITY;
    let mut count = 0_usize;
    let mut has_left = false;
    let mut has_right = false;
    for &(center, size, on_left) in &segments {
        if count > 0 && center - previous_center > size.max(1.0) * 0.5 {
            if has_left && has_right {
                try_push_text(&mut band_counts, count, "column band counts")?;
            }
            count = 0;
            has_left = false;
            has_right = false;
        }
        previous_center = center;
        count += 1;
        has_left |= on_left;
        has_right |= !on_left;
    }
    if count > 0 && has_left && has_right {
        try_push_text(&mut band_counts, count, "column band counts")?;
    }
    if band_counts.is_empty() {
        return Ok(None);
    }
    band_counts.sort_unstable();
    Ok(Some(band_counts[band_counts.len() / 2]))
}

fn valid_geometry_column_split(lines: &[LineGeometry], boundary: f64) -> Result<bool, Error> {
    let Some((left_y0, left_y1, left_count)) = geometry_side_extent(lines, boundary, true) else {
        return Ok(false);
    };
    let Some((right_y0, right_y1, right_count)) = geometry_side_extent(lines, boundary, false)
    else {
        return Ok(false);
    };
    if left_count < MIN_COLUMN_LINES || right_count < MIN_COLUMN_LINES {
        return Ok(false);
    }
    let overlap = left_y1.min(right_y1) - left_y0.max(right_y0);
    let shorter_height = (left_y1 - left_y0).min(right_y1 - right_y0);
    if !(overlap > 0.0
        && shorter_height > 0.0
        && overlap / shorter_height >= MIN_COLUMN_VERTICAL_OVERLAP)
    {
        return Ok(false);
    }
    if let Some(median) = bisected_band_segment_median(lines, boundary)?
        && median > MAX_COLUMN_BAND_SEGMENTS
    {
        return Ok(false);
    }
    Ok(true)
}

/// Recursively order column regions left-to-right while preserving spanning
/// headings above them and footers below them.
fn order_columns(
    lines: Vec<Vec<GlyphRecord>>,
    orientation: AxisOrientation,
) -> Result<Vec<Vec<GlyphRecord>>, Error> {
    let mut geometry = Vec::new();
    for line in &lines {
        try_push_text(
            &mut geometry,
            line_geometry(line, orientation),
            "column text-line geometry",
        )?;
    }
    let Some(boundary) = geometry_column_boundary(&geometry)? else {
        return Ok(lines);
    };
    if !valid_geometry_column_split(&geometry, boundary)? {
        return Ok(lines);
    }

    let mut first_center = f64::INFINITY;
    let mut last_center = f64::NEG_INFINITY;
    let mut has_side_line = false;
    for line in &geometry {
        if line.bbox.2 <= boundary || line.bbox.0 >= boundary {
            let center = (line.bbox.1 + line.bbox.3) * 0.5;
            first_center = first_center.min(center);
            last_center = last_center.max(center);
            has_side_line = true;
        }
    }
    if !has_side_line {
        return Ok(lines);
    }
    let has_middle_spanning = geometry.iter().any(|line| {
        let center = (line.bbox.1 + line.bbox.3) * 0.5;
        line.bbox.0 < boundary
            && line.bbox.2 > boundary
            && center > first_center
            && center < last_center
    });
    if has_middle_spanning {
        return Ok(lines);
    }

    let mut top = Vec::new();
    let mut left = Vec::new();
    let mut right = Vec::new();
    let mut bottom = Vec::new();
    for (line, line_geometry) in lines.into_iter().zip(geometry) {
        if line_geometry.bbox.2 <= boundary {
            try_push_text(&mut left, line, "left-column text lines")?;
        } else if line_geometry.bbox.0 >= boundary {
            try_push_text(&mut right, line, "right-column text lines")?;
        } else if (line_geometry.bbox.1 + line_geometry.bbox.3) * 0.5 <= first_center {
            try_push_text(&mut top, line, "column-spanning headings")?;
        } else {
            try_push_text(&mut bottom, line, "column-spanning footers")?;
        }
    }

    for line in order_columns(left, orientation)? {
        try_push_text(&mut top, line, "ordered column text lines")?;
    }
    for line in order_columns(right, orientation)? {
        try_push_text(&mut top, line, "ordered column text lines")?;
    }
    for line in bottom {
        try_push_text(&mut top, line, "ordered column text lines")?;
    }
    Ok(top)
}

/// Appends the text of page `index`, counted from 0, to `out` in reading
/// order, as pylopdf's `TextPage::text` assembles it, and returns the number
/// of glyphs without a Unicode mapping that [`Unmapped::Skip`] left out.
///
/// The page's glyphs count against `text_glyph_count` while it is read, and
/// its glyph text and then its assembled text against `text_size`, which
/// `out` keeps for the rest of the operation. A refusal leaves `out` as it
/// was.
pub fn page_text(
    bridged: &Bridged,
    index: usize,
    options: &TextOptions,
    budget: &Budget,
    out: &mut String,
) -> Result<usize, Error> {
    let page_box = {
        let pages = bridged.pdf().pages();
        let page = pages.get(index).ok_or(Error::NoPage(index))?;
        let (width, height) = page.render_dimensions();
        Rect::new(0.0, 0.0, f64::from(width), f64::from(height))
    };
    let collector = TextCollector {
        glyphs: Vec::new(),
        font_infos: HashMap::new(),
        last_font: None,
        text_size: 0,
        budget,
        error: None,
        skipped_no_unicode: 0,
        style: RunStyle {
            render_mode: RENDER_FILL,
            clip: None,
        },
        run_start: 0,
        options: *options,
        page_box,
    };
    let (_, mut collector) = super::page_content_into(
        bridged,
        index,
        collector,
        ItemKinds::NONE,
        true,
        None,
        budget,
    )?;
    let glyph_count = collector.glyphs.len();
    let text_size = collector.text_size;
    let release = || {
        budget.release(LimitCode::TextGlyphCount, as_u64(glyph_count));
        budget.release(LimitCode::TextSize, as_u64(text_size));
    };
    if let Some(error) = collector.error.take() {
        release();
        return Err(error);
    }
    let skipped = collector.skipped_no_unicode;
    let lines = page_lines(collector.glyphs).and_then(|lines| view_lines(lines, options));
    // The glyphs and their text give way to the text assembled from them.
    release();
    let lines = lines?;
    let size = assembled_text_size(&lines).ok_or_else(|| {
        Error::Allocation("the assembled text exceeds the address space".to_owned())
    })?;
    budget.charge(LimitCode::TextSize, as_u64(size))?;
    let appended = assemble_text(&lines, Some(size)).and_then(|text| {
        out.try_reserve(text.len())
            .map_err(|error| Error::Allocation(format!("failed to grow the text: {error}")))?;
        out.push_str(&text);
        Ok(())
    });
    if let Err(error) = appended {
        budget.release(LimitCode::TextSize, as_u64(size));
        return Err(error);
    }
    Ok(skipped)
}

/// Clusters glyphs into lines and orders them for reading, each line in
/// logical order.
fn page_lines(glyphs: Vec<GlyphRecord>) -> Result<Vec<Vec<GlyphRecord>>, Error> {
    let physical_lines = cluster_lines(glyphs)?;
    let mut lines = order_page_lines(physical_lines)?;
    for line in &mut lines {
        logicalize_rtl_line(line);
    }
    Ok(lines)
}

/// The lines as the view options make them: kept without options, else
/// the glyphs inside the clip with their text rewritten.
fn view_lines(
    lines: Vec<Vec<GlyphRecord>>,
    view: &TextOptions,
) -> Result<Vec<Vec<GlyphRecord>>, Error> {
    if !view.changes_lines() {
        return Ok(lines);
    }
    let mut viewed = Vec::new();
    for line in &lines {
        let mut kept = Vec::new();
        for glyph in line {
            if let Some((x0, y0, x1, y1)) = view.clip {
                let (gx0, gy0, gx1, gy1) = font_bbox(glyph);
                let (cx, cy) = ((gx0 + gx1) * 0.5, (gy0 + gy1) * 0.5);
                if !(cx >= x0 && cx <= x1 && cy >= y0 && cy <= y1) {
                    continue;
                }
            }
            try_push_text(&mut kept, view_glyph(glyph, view)?, "text view glyphs")?;
        }
        if !kept.is_empty() {
            try_push_text(&mut viewed, kept, "text view lines")?;
        }
    }
    if view.dehyphenate {
        dehyphenate_lines(&mut viewed);
    }
    Ok(viewed)
}

/// Copy one glyph with the text that the view options make of it.
fn view_glyph(glyph: &GlyphRecord, view: &TextOptions) -> Result<GlyphRecord, Error> {
    let text = if view.expand_ligatures || view.normalize_whitespace {
        let mut text = String::new();
        for ch in glyph.text.chars() {
            let expanded = match ch {
                '\u{FB00}' if view.expand_ligatures => "ff",
                '\u{FB01}' if view.expand_ligatures => "fi",
                '\u{FB02}' if view.expand_ligatures => "fl",
                '\u{FB03}' if view.expand_ligatures => "ffi",
                '\u{FB04}' if view.expand_ligatures => "ffl",
                '\u{FB05}' | '\u{FB06}' if view.expand_ligatures => "st",
                ch if view.normalize_whitespace && ch.is_whitespace() => " ",
                ch => {
                    let mut encoded = [0u8; 4];
                    try_push_str_text(
                        &mut text,
                        ch.encode_utf8(&mut encoded),
                        "text view glyph text",
                    )?;
                    continue;
                }
            };
            try_push_str_text(&mut text, expanded, "text view glyph text")?;
        }
        GlyphText::try_from_str(&text)?
    } else {
        glyph.text.try_clone()?
    };
    Ok(GlyphRecord {
        bidi_flags: glyph_bidi_flags(&text),
        text,
        x: glyph.x,
        y: glyph.y,
        size: glyph.size,
        advance: glyph.advance,
        direction: glyph.direction,
        writing_mode: glyph.writing_mode,
        font_key: glyph.font_key,
        font: glyph.font,
        render_mode: glyph.render_mode,
        is_clipped: glyph.is_clipped,
        joins_previous: glyph.joins_previous,
        source_order: glyph.source_order,
    })
}

/// Whether `line` starts a new block after `previous`: vertical lines stand
/// alone, and so does a line whose baseline is more than `BLOCK_GAP` sizes
/// below, or more than `LINE_TOLERANCE` sizes above, the one before.
fn starts_block(previous: &[GlyphRecord], line: &[GlyphRecord]) -> bool {
    let size = |line: &[GlyphRecord]| line.iter().map(|glyph| glyph.size).fold(0.0, f64::max);
    let vertical = has_vertical_baseline(&line[0]);
    let scale = size(previous).max(size(line)).max(1.0);
    let (prev, baseline) = (previous[0].y, line[0].y);
    vertical
        || has_vertical_baseline(&previous[0]) != vertical
        || baseline - prev > scale * BLOCK_GAP
        || prev - baseline > scale * LINE_TOLERANCE
}

/// Whether a glyph's text is one hyphen that can end a hyphenated line:
/// HYPHEN-MINUS, SOFT HYPHEN, or HYPHEN.
fn is_line_end_hyphen(glyph: &GlyphRecord) -> bool {
    matches!(glyph.text, GlyphText::Char('-' | '\u{00AD}' | '\u{2010}'))
}

/// Remove the hyphen that ends a line between two letters of one word and
/// mark the next line's first glyph as continuing that word.
///
/// The next line must belong to the same block, as `assemble_layout` groups
/// lines, and start with a lowercase letter, so that a hyphenated compound
/// before a capital, such as a name, keeps its hyphen.
fn dehyphenate_lines(lines: &mut [Vec<GlyphRecord>]) {
    for index in 1..lines.len() {
        let (before, after) = lines.split_at_mut(index);
        let line = &mut before[index - 1];
        let next = &mut after[0];
        if starts_block(line, next) || line.len() < 2 {
            continue;
        }
        let hyphen = &line[line.len() - 1];
        let letter = &line[line.len() - 2];
        let continues = next[0].text.chars().next().is_some_and(char::is_lowercase);
        let ends_word = letter.text.chars().last().is_some_and(char::is_alphabetic);
        if is_line_end_hyphen(hyphen) && ends_word && continues {
            line.pop();
            next[0].joins_previous = true;
        }
    }
}

/// Position along the line's baseline direction.
fn glyph_progress(glyph: &GlyphRecord) -> f64 {
    glyph.x * glyph.direction.0 + glyph.y * glyph.direction.1
}

/// Decide whether to insert a space from the gap between adjacent glyphs.
///
/// Logical RTL restoration can make adjacent records progress backwards along
/// the baseline. Compare their complete inline intervals so inferred word
/// spaces remain symmetric without changing geometry-based line discovery.
fn needs_gap(previous: Option<&GlyphRecord>, glyph: &GlyphRecord) -> bool {
    if glyph.writing_mode == 1 {
        return false;
    }
    previous.is_some_and(|previous| {
        if previous.text.ends_with(' ') || glyph.text.starts_with(' ') {
            return false;
        }
        if glyph_uses_no_space_script(previous) && glyph_uses_no_space_script(glyph) {
            return false;
        }
        if glyph_is_nonspacing_mark(glyph) {
            return false;
        }
        let previous_start = glyph_progress(previous);
        let previous_end = glyph_end(previous);
        let glyph_start = glyph_progress(glyph);
        let glyph_end = glyph_end(glyph);
        let gap = if glyph_start >= previous_start {
            glyph_start - previous_end
        } else {
            previous_start - glyph_end
        };
        gap > previous.size.max(glyph.size).max(1.0) * WORD_GAP
    })
}

fn glyph_uses_no_space_script(glyph: &GlyphRecord) -> bool {
    !glyph.text.is_empty() && glyph.text.chars().all(char_uses_no_space_script)
}

fn char_uses_no_space_script(ch: char) -> bool {
    matches!(
        ch as u32,
        0x0E00..=0x0E7F
            | 0x0E80..=0x0EFF
            | 0x1000..=0x109F
            | 0x1780..=0x17FF
            | 0x19E0..=0x19FF
            | 0xA9E0..=0xA9FF
            | 0xAA60..=0xAA7F
    )
}

/// End position of one glyph along its line's baseline.
fn glyph_end(glyph: &GlyphRecord) -> f64 {
    glyph_progress(glyph) + glyph.advance
}

fn glyph_is_nonspacing_mark(glyph: &GlyphRecord) -> bool {
    glyph.bidi_flags & BIDI_NSM_ONLY != 0
}

const BIDI_RTL_STRONG: u8 = 1;
const BIDI_FORWARD: u8 = 1 << 1;
const BIDI_NSM_ONLY: u8 = 1 << 2;
const BIDI_WHITESPACE: u8 = 1 << 3;
const BIDI_TOKEN_SEPARATOR: u8 = 1 << 4;
const BIDI_OTHER_NEUTRAL: u8 = 1 << 5;
const BIDI_UNSUPPORTED: u8 = 1 << 6;

#[derive(Clone, Copy, PartialEq, Eq)]
enum RtlLineOrder {
    None,
    Pure,
    EmbeddedForwardToken,
}

#[derive(Clone, Copy)]
enum MixedRtlState {
    BeforeToken,
    Token,
    AwaitingRtl,
    AfterToken,
}

/// Classify RTL text that can be restored without guessing paragraph layout.
///
/// A single forward token bracketed by RTL text is unambiguous: reverse the
/// RTL line, then restore that token's internal order. Whitespace-separated
/// forward runs, edge tokens, standalone marks, and general neutral punctuation
/// can map to multiple logical strings, so they remain in producer order.
fn glyphs_rtl_order<'a>(glyphs: impl IntoIterator<Item = &'a GlyphRecord>) -> RtlLineOrder {
    let mut state = MixedRtlState::BeforeToken;
    let mut saw_rtl = false;
    let mut saw_rtl_before = false;
    let mut saw_rtl_after = false;
    let mut saw_forward = false;
    let mut token_ends_forward = false;
    let mut mixed_invalid = false;

    for glyph in glyphs {
        let flags = glyph.bidi_flags;
        if flags & BIDI_UNSUPPORTED != 0
            || flags & BIDI_RTL_STRONG != 0 && flags & BIDI_FORWARD != 0
        {
            return RtlLineOrder::None;
        }
        if flags & (BIDI_NSM_ONLY | BIDI_OTHER_NEUTRAL) != 0 {
            mixed_invalid = true;
        }
        if flags & BIDI_RTL_STRONG != 0 {
            saw_rtl = true;
            if saw_forward {
                if !token_ends_forward {
                    mixed_invalid = true;
                }
                saw_rtl_after = true;
                state = MixedRtlState::AfterToken;
            } else {
                saw_rtl_before = true;
            }
            continue;
        }
        if flags & BIDI_FORWARD != 0 {
            match state {
                MixedRtlState::BeforeToken | MixedRtlState::Token => {
                    saw_forward = true;
                    token_ends_forward = true;
                    state = MixedRtlState::Token;
                }
                MixedRtlState::AwaitingRtl | MixedRtlState::AfterToken => {
                    mixed_invalid = true;
                }
            }
            continue;
        }
        if flags & BIDI_TOKEN_SEPARATOR != 0 {
            if matches!(state, MixedRtlState::Token) {
                token_ends_forward = false;
            } else {
                mixed_invalid = true;
            }
            continue;
        }
        if flags & BIDI_WHITESPACE != 0 && matches!(state, MixedRtlState::Token) {
            if !token_ends_forward {
                mixed_invalid = true;
            }
            state = MixedRtlState::AwaitingRtl;
        }
    }

    if !saw_rtl {
        RtlLineOrder::None
    } else if !saw_forward {
        RtlLineOrder::Pure
    } else if !mixed_invalid
        && saw_rtl_before
        && saw_rtl_after
        && token_ends_forward
        && matches!(state, MixedRtlState::AfterToken)
    {
        RtlLineOrder::EmbeddedForwardToken
    } else {
        RtlLineOrder::None
    }
}

fn glyph_is_forward_token_part(glyph: &GlyphRecord) -> bool {
    glyph.bidi_flags & (BIDI_FORWARD | BIDI_TOKEN_SEPARATOR) != 0
}

/// Summarize the bidi properties needed by extraction once at collection time.
fn glyph_bidi_flags(text: &GlyphText) -> u8 {
    match text {
        GlyphText::Char(ch) => char_bidi_flags(*ch),
        GlyphText::Heap(text) => {
            let mut flags = 0;
            let mut nsm_only = true;
            for ch in text.chars() {
                let char_flags = char_bidi_flags(ch);
                flags |= char_flags & !BIDI_NSM_ONLY;
                nsm_only &= char_flags & BIDI_NSM_ONLY != 0;
            }
            if nsm_only {
                flags |= BIDI_NSM_ONLY;
            }
            flags
        }
    }
}

fn char_bidi_flags(ch: char) -> u8 {
    // ASCII dominates common PDF text and never contains a nonspacing mark.
    // Classify its letters and digits without a Unicode table lookup.
    if ch.is_ascii_alphanumeric() {
        return BIDI_FORWARD;
    }
    match bidi_class(ch) {
        BidiClass::R | BidiClass::AL => BIDI_RTL_STRONG,
        BidiClass::L | BidiClass::EN | BidiClass::AN => BIDI_FORWARD,
        BidiClass::NSM => BIDI_NSM_ONLY,
        BidiClass::WS | BidiClass::B | BidiClass::S | BidiClass::BN => BIDI_WHITESPACE,
        BidiClass::CS | BidiClass::ES | BidiClass::ET => BIDI_TOKEN_SEPARATOR,
        BidiClass::ON => BIDI_OTHER_NEUTRAL,
        _ => BIDI_UNSUPPORTED,
    }
}

/// Restore one unambiguous RTL line from display order to logical Unicode order.
///
/// Reverse glyph records rather than scalar values so multi-character CMap
/// mappings stay intact. A shaped base followed by separate nonspacing-mark
/// records is reversed as one cluster without allocating an index vector. One
/// embedded forward token is then reversed back in place.
fn logicalize_rtl_line(line: &mut [GlyphRecord]) {
    let order = glyphs_rtl_order(line.iter());
    if order == RtlLineOrder::None {
        return;
    }
    line.reverse();
    let mut index = 0;
    while index < line.len() {
        if !glyph_is_nonspacing_mark(&line[index]) {
            index += 1;
            continue;
        }
        let start = index;
        while index < line.len() && glyph_is_nonspacing_mark(&line[index]) {
            index += 1;
        }
        if index < line.len() {
            line[start..=index].reverse();
            index += 1;
        }
    }
    if order == RtlLineOrder::EmbeddedForwardToken {
        let start = line
            .iter()
            .position(glyph_is_forward_token_part)
            .expect("embedded token classification requires a forward glyph");
        let end = line
            .iter()
            .rposition(glyph_is_forward_token_part)
            .expect("embedded token classification requires a forward glyph")
            + 1;
        line[start..end].reverse();
    }
}

/// Calculate the exact plain-text size before allocating the returned string.
fn assembled_text_size(lines: &[Vec<GlyphRecord>]) -> Option<usize> {
    let mut total = 0usize;
    for (line_index, line) in lines.iter().enumerate() {
        let mut line_size = 0usize;
        let mut trailing_spaces = 0usize;
        let mut ends_with_space = false;
        let mut ends_with_newline = line_index > 0;
        let mut previous: Option<&GlyphRecord> = None;
        for glyph in line {
            if needs_gap(previous, glyph) && !ends_with_space && !ends_with_newline {
                line_size = line_size.checked_add(1)?;
                trailing_spaces = trailing_spaces.checked_add(1)?;
            }
            line_size = line_size.checked_add(glyph.text.len())?;
            let glyph_trailing_spaces = glyph.text.trailing_spaces();
            if glyph_trailing_spaces == glyph.text.len() {
                trailing_spaces = trailing_spaces.checked_add(glyph_trailing_spaces)?;
            } else {
                trailing_spaces = glyph_trailing_spaces;
            }
            ends_with_space = glyph.text.ends_with(' ');
            ends_with_newline = glyph.text.ends_with('\n');
            if !glyph_is_nonspacing_mark(glyph) {
                previous = Some(glyph);
            }
        }
        line_size = line_size.checked_sub(trailing_spaces)?;
        total = total.checked_add(line_size)?;
        if !joins_next(lines, line_index) {
            total = total.checked_add(1)?;
        }
    }
    Some(total)
}

/// Whether dehyphenation joins line `index` to the line after it.
fn joins_next<L: AsRef<[GlyphRecord]>>(lines: &[L], index: usize) -> bool {
    lines
        .get(index + 1)
        .and_then(|line| line.as_ref().first())
        .is_some_and(|glyph| glyph.joins_previous)
}

/// Assemble glyphs into top-to-bottom, left-to-right plain text.
fn assemble_text<L: AsRef<[GlyphRecord]>>(
    lines: &[L],
    exact_size: Option<usize>,
) -> Result<String, Error> {
    let mut out = String::new();
    if let Some(size) = exact_size {
        out.try_reserve_exact(size).map_err(|error| {
            Error::Allocation(format!("failed to reserve plain text output: {error}"))
        })?;
    }
    for (index, line) in lines.iter().enumerate() {
        let mut previous: Option<&GlyphRecord> = None;
        for glyph in line.as_ref() {
            if needs_gap(previous, glyph) && !out.ends_with(' ') && !out.ends_with('\n') {
                try_push_str_text(&mut out, " ", "plain text output")?;
            }
            try_push_glyph_text(&mut out, &glyph.text, "plain text output")?;
            if !glyph_is_nonspacing_mark(glyph) {
                previous = Some(glyph);
            }
        }
        // Drop extra whitespace glyphs at line ends.
        while out.ends_with(' ') {
            out.pop();
        }
        // A line that dehyphenation joins to the next ends without a break.
        if !joins_next(lines, index) {
            try_push_str_text(&mut out, "\n", "plain text output")?;
        }
    }
    if let Some(size) = exact_size {
        debug_assert_eq!(out.len(), size);
    }
    Ok(out)
}

/// One glyph bbox from its advance and the font's ascender and descender,
/// which reading order uses whatever the box options.
fn font_bbox(glyph: &GlyphRecord) -> BBox {
    let (above, below) = glyph.vertical_extent();
    if glyph.writing_mode == 1
        || (glyph.direction.0 >= 1.0 - 1e-6 && glyph.direction.1.abs() <= 1e-6)
    {
        // Inferred mode-1 CJK retains upright glyph geometry; only its
        // conservative line ordering is vertical.
        return (
            glyph.x,
            glyph.y - above,
            glyph.x + glyph.advance,
            glyph.y + below,
        );
    }
    let block = (-glyph.direction.1, glyph.direction.0);
    let mut x0 = f64::INFINITY;
    let mut y0 = f64::INFINITY;
    let mut x1 = f64::NEG_INFINITY;
    let mut y1 = f64::NEG_INFINITY;
    for inline in [0.0, glyph.advance] {
        for cross in [-above, below] {
            let x = glyph.x + glyph.direction.0 * inline + block.0 * cross;
            let y = glyph.y + glyph.direction.1 * inline + block.1 * cross;
            x0 = x0.min(x);
            x1 = x1.max(x);
            y0 = y0.min(y);
            y1 = y1.max(y);
        }
    }
    (x0, y0, x1, y1)
}
