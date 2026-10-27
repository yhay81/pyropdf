//! The words, spans, characters, and blocks of a page's text (DESIGN.md
//! sections 6 and 14), ported from pylopdf's `rust/src/extract.rs` at
//! 036be111cafe77755db4bb2be17bc70a8f2f9d82.
//!
//! The layout groups the lines of [`super::page_text`] into blocks, splits
//! each line into spans where the font, the paint, or the baseline changes
//! and into words, and gives every line and span a quad that follows its
//! baseline and, on request, its characters. Every string that the layout
//! holds counts against `text_size` (F6). Its records number at most two
//! per glyph, which `text_glyph_count` bounds, and the rules of one page at
//! most [`MAX_RULES`].

use hayro::hayro_interpret::DrawMode;
use hayro::kurbo::{Affine, BezPath, PathSeg};
use pyropdf_store::budget::{Budget, LimitCode};

use super::{
    BBox, Error, GlyphRecord, TextOptions, as_u64, glyph_bbox, glyph_is_nonspacing_mark, needs_gap,
    starts_block, try_push_glyph_text, try_push_str_text, try_push_text, viewed_page_lines,
};
use crate::bridge::Bridged;

/// Four corners: upper left, upper right, lower left, and lower right.
pub type QuadTuple = ((f64, f64), (f64, f64), (f64, f64), (f64, f64));
/// One glyph of a span: `(whole Unicode mapping, bbox, baseline origin,
/// synthetic)`; a synthetic char is a space inferred from a gap.
pub type CharTuple = (String, BBox, (f64, f64), bool);
/// A span's paint, metrics, and decorations: `(sRGB color and alpha, render
/// mode, clipped, ascender, descender, (underlined, struck out,
/// superscript))`.
pub type SpanStyleTuple = (
    Option<(f32, f32, f32, f32)>,
    u8,
    bool,
    f64,
    f64,
    (bool, bool, bool),
);
/// Span: `(bbox, (text, chars), size, origin, font name, flags, style,
/// quad)`; the chars are empty unless the layout asks for them.
pub type SpanTuple = (
    BBox,
    (String, Vec<CharTuple>),
    f64,
    (f64, f64),
    String,
    i64,
    SpanStyleTuple,
    QuadTuple,
);
/// Word: `(bbox, text)`.
pub type WordTuple = (BBox, String);
/// Line: `(bbox, spans, words, baseline direction, writing mode, quad)`.
pub type LineTuple = (
    BBox,
    Vec<SpanTuple>,
    Vec<WordTuple>,
    (f64, f64),
    u8,
    QuadTuple,
);
/// Block: `(bbox, lines)`.
pub type BlockTuple = (BBox, Vec<LineTuple>);

/// The layout of one page's text.
pub struct PageLayout {
    /// The size of the page in display space, which every box is in.
    pub width: f64,
    pub height: f64,
    pub blocks: Vec<BlockTuple>,
    /// The glyphs without Unicode that [`super::Unmapped::Skip`] left out.
    pub skipped: usize,
}

/// Returns the layout of the page at `index`, each span with its characters
/// when `chars` is set.
///
/// The page's glyphs count against `text_glyph_count` and their text against
/// `text_size` while the layout is built, and the layout's strings against
/// `text_size` for the rest of the operation. A refusal keeps nothing.
pub fn page_layout(
    bridged: &Bridged,
    index: usize,
    options: &TextOptions,
    chars: bool,
    budget: &Budget,
) -> Result<PageLayout, Error> {
    let viewed = viewed_page_lines(bridged, index, options, budget)?;
    let mut meter = Meter { budget, charged: 0 };
    let blocks = assemble_layout(&viewed.lines, chars, &mut meter).and_then(|mut blocks| {
        merge_joined_words(&viewed.lines, &mut blocks)?;
        Ok(blocks)
    });
    // The glyphs give way to the layout built from them.
    viewed.charge.release(budget);
    let blocks = blocks.inspect_err(|_| meter.release())?;
    Ok(PageLayout {
        width: viewed.page_box.width(),
        height: viewed.page_box.height(),
        blocks,
        skipped: viewed.skipped,
    })
}

/// The `text_size` charges of the layout's strings.
struct Meter<'b> {
    budget: &'b Budget,
    charged: usize,
}

impl Meter<'_> {
    /// Charges one string of the layout.
    fn charge(&mut self, text: &str) -> Result<(), Error> {
        self.budget
            .charge(LimitCode::TextSize, as_u64(text.len()))?;
        self.charged += text.len();
        Ok(())
    }

    fn release(&self) {
        self.budget
            .release(LimitCode::TextSize, as_u64(self.charged));
    }
}

/// One axis-aligned rule in display space.
#[derive(Clone, Copy)]
pub(super) struct RuleSegment {
    x0: f64,
    y0: f64,
    x1: f64,
    #[expect(
        dead_code,
        reason = "styles read horizontal rules; the tables of #101 read the extent of vertical ones"
    )]
    y1: f64,
    horizontal: bool,
}

/// The most rules that one page collects.
const MAX_RULES: usize = 4096;

/// Treat transformed path segments this close to an axis as horizontal or
/// vertical.
const RULE_AXIS_TOLERANCE: f64 = 0.5;

/// Ignore tiny decorations and glyph-like path segments.
const MIN_RULE_LENGTH: f64 = 3.0;

/// Maximum short axis accepted as a filled rule.
const MAX_FILLED_RULE_THICKNESS: f64 = 4.0;

/// Reject compact filled decorations even when one axis is slightly longer.
const MIN_FILLED_RULE_ASPECT: f64 = 4.0;

/// Adds the rules that one path paint draws to `rules`, up to [`MAX_RULES`]:
/// a narrow filled rectangle, and each axis-aligned line of a stroke.
pub(super) fn collect_rules(
    path: &BezPath,
    transform: Affine,
    mode: &DrawMode,
    rules: &mut Vec<RuleSegment>,
) -> Result<(), Error> {
    if matches!(mode, DrawMode::Fill(_) | DrawMode::FillAndStroke(..))
        && let Some(rule) = filled_rule(path, transform)
        && !push_rule(rules, rule)?
    {
        return Ok(());
    }
    if !matches!(mode, DrawMode::Stroke(_) | DrawMode::FillAndStroke(..)) {
        return Ok(());
    }
    for segment in path.segments() {
        let PathSeg::Line(line) = segment else {
            continue;
        };
        let start = transform * line.p0;
        let end = transform * line.p1;
        if !start.x.is_finite() || !start.y.is_finite() || !end.x.is_finite() || !end.y.is_finite()
        {
            continue;
        }
        let dx = (end.x - start.x).abs();
        let dy = (end.y - start.y).abs();
        let rule = if dy <= RULE_AXIS_TOLERANCE && dx >= MIN_RULE_LENGTH {
            RuleSegment {
                x0: start.x.min(end.x),
                y0: (start.y + end.y) * 0.5,
                x1: start.x.max(end.x),
                y1: (start.y + end.y) * 0.5,
                horizontal: true,
            }
        } else if dx <= RULE_AXIS_TOLERANCE && dy >= MIN_RULE_LENGTH {
            RuleSegment {
                x0: (start.x + end.x) * 0.5,
                y0: start.y.min(end.y),
                x1: (start.x + end.x) * 0.5,
                y1: start.y.max(end.y),
                horizontal: false,
            }
        } else {
            continue;
        };
        if !push_rule(rules, rule)? {
            break;
        }
    }
    Ok(())
}

/// Pushes one rule, and returns whether there was room for it.
fn push_rule(rules: &mut Vec<RuleSegment>, rule: RuleSegment) -> Result<bool, Error> {
    if rules.len() >= MAX_RULES {
        return Ok(false);
    }
    try_push_text(rules, rule, "rule collection")?;
    Ok(true)
}

/// The rule of a narrow filled rectangle.
///
/// Generators often paint rules as narrow filled rectangles instead of
/// stroking paths. Curves and compact shapes are excluded so glyph outlines
/// and ordinary decorations do not become rules.
fn filled_rule(path: &BezPath, transform: Affine) -> Option<RuleSegment> {
    let mut x0 = f64::INFINITY;
    let mut y0 = f64::INFINITY;
    let mut x1 = f64::NEG_INFINITY;
    let mut y1 = f64::NEG_INFINITY;
    let mut line_count = 0;
    for segment in path.segments() {
        let PathSeg::Line(line) = segment else {
            return None;
        };
        for point in [transform * line.p0, transform * line.p1] {
            if !point.x.is_finite() || !point.y.is_finite() {
                return None;
            }
            x0 = x0.min(point.x);
            y0 = y0.min(point.y);
            x1 = x1.max(point.x);
            y1 = y1.max(point.y);
        }
        line_count += 1;
    }
    if line_count < 4 {
        return None;
    }
    let width = x1 - x0;
    let height = y1 - y0;
    if height <= MAX_FILLED_RULE_THICKNESS
        && width >= MIN_RULE_LENGTH
        && width >= height.max(f64::EPSILON) * MIN_FILLED_RULE_ASPECT
    {
        Some(RuleSegment {
            x0,
            y0: (y0 + y1) * 0.5,
            x1,
            y1: (y0 + y1) * 0.5,
            horizontal: true,
        })
    } else if width <= MAX_FILLED_RULE_THICKNESS
        && height >= MIN_RULE_LENGTH
        && height >= width.max(f64::EPSILON) * MIN_FILLED_RULE_ASPECT
    {
        Some(RuleSegment {
            x0: (x0 + x1) * 0.5,
            y0,
            x1: (x0 + x1) * 0.5,
            y1,
            horizontal: false,
        })
    } else {
        None
    }
}

/// Marks the glyphs of horizontal text that a horizontal rule underlines or
/// strikes out: a rule that covers at least half of a glyph's advance,
/// between a twentieth of its size above its baseline and three tenths below
/// it, underlines it, and one between a sixth and six tenths of its size
/// above its baseline strikes it out.
pub(super) fn mark_styles(glyphs: &mut [GlyphRecord], mut rules: Vec<RuleSegment>) {
    rules.retain(|rule| rule.horizontal);
    if rules.is_empty() {
        return;
    }
    rules.sort_unstable_by(|left, right| {
        left.y0
            .total_cmp(&right.y0)
            .then(left.x0.total_cmp(&right.x0))
    });
    for glyph in glyphs {
        if !is_horizontal(glyph) {
            continue;
        }
        let size = glyph.size.max(1.0);
        let (top, bottom) = (
            glyph.y - size * STRIKE_HIGHEST,
            glyph.y + size * UNDERLINE_LOWEST,
        );
        let start = rules.partition_point(|rule| rule.y0 < top);
        for rule in rules[start..].iter().take_while(|rule| rule.y0 <= bottom) {
            let overlap = rule.x1.min(glyph.x + glyph.advance) - rule.x0.max(glyph.x);
            if overlap < glyph.advance * 0.5 {
                continue;
            }
            let rise = glyph.y - rule.y0;
            if rise <= size * UNDERLINE_HIGHEST {
                glyph.is_underlined = true;
            } else if rise >= size * STRIKE_LOWEST {
                glyph.is_struck = true;
            }
        }
    }
}

/// The band of an underline below and above the baseline, and of a
/// strike-out above it, in sizes.
const UNDERLINE_LOWEST: f64 = 0.3;
const UNDERLINE_HIGHEST: f64 = 0.05;
const STRIKE_LOWEST: f64 = 1.0 / 6.0;
const STRIKE_HIGHEST: f64 = 0.6;

/// Whether a glyph is set in horizontal writing along the x axis.
fn is_horizontal(glyph: &GlyphRecord) -> bool {
    glyph.writing_mode == 0 && glyph.direction.0 >= 1.0 - 1e-6 && glyph.direction.1.abs() <= 1e-6
}

/// Glyph bounding box with vertical extents approximated from baseline and
/// size.
fn glyphs_bbox(glyphs: &[GlyphRecord]) -> BBox {
    let mut x0 = f64::INFINITY;
    let mut y0 = f64::INFINITY;
    let mut x1 = f64::NEG_INFINITY;
    let mut y1 = f64::NEG_INFINITY;
    for glyph in glyphs {
        let (glyph_x0, glyph_y0, glyph_x1, glyph_y1) = glyph_bbox(glyph);
        x0 = x0.min(glyph_x0);
        y0 = y0.min(glyph_y0);
        x1 = x1.max(glyph_x1);
        y1 = y1.max(glyph_y1);
    }
    (x0, y0, x1, y1)
}

/// Borrow contiguous spans sharing size, font, paint, render mode, baseline,
/// clipping, and decorations without allocating a partition.
fn span_glyph_slices(line: &[GlyphRecord]) -> impl Iterator<Item = &[GlyphRecord]> {
    let mut start = 0;
    std::iter::from_fn(move || {
        if start == line.len() {
            return None;
        }
        let mut end = start + 1;
        while end < line.len() {
            let (a, b) = (&line[end - 1], &line[end]);
            if b.font_key != a.font_key
                || (b.size - a.size).abs() > 0.1
                || b.color != a.color
                || b.render_mode != a.render_mode
                || b.is_clipped != a.is_clipped
                || b.is_underlined != a.is_underlined
                || b.is_struck != a.is_struck
                || baseline_shift(a, b) > a.size.max(b.size) * SPAN_BASELINE_TOLERANCE
            {
                break;
            }
            end += 1;
        }
        let glyphs = &line[start..end];
        start = end;
        Some(glyphs)
    })
}

/// Builds one span of `line`, with its characters when `chars` is set.
fn span_tuple(
    glyphs: &[GlyphRecord],
    line: &[GlyphRecord],
    chars: bool,
    meter: &mut Meter<'_>,
) -> Result<SpanTuple, Error> {
    let text = span_text(glyphs)?;
    meter.charge(&text)?;
    let chars = if chars {
        span_chars(glyphs, meter)?
    } else {
        Vec::new()
    };
    let first = &glyphs[0];
    let mut font_name = String::new();
    try_push_str_text(&mut font_name, &first.font.name, "layout span font name")?;
    meter.charge(&font_name)?;
    Ok((
        glyphs_bbox(glyphs),
        (text, chars),
        glyphs.iter().map(|g| g.size).fold(0.0, f64::max),
        (first.x, first.y),
        font_name,
        first.font.flags,
        (
            first
                .color
                .map(|[red, green, blue, alpha]| (red, green, blue, alpha)),
            first.render_mode,
            first.is_clipped,
            first.font.ascender,
            first.font.descender,
            (
                first.is_underlined,
                first.is_struck,
                is_superscript(glyphs, line),
            ),
        ),
        glyphs_quad(glyphs),
    ))
}

/// The characters of one span, one per mapped glyph, keeping
/// multi-character mappings intact, with a synthetic space for each gap
/// that [`span_text`] infers, so that the chars join to the span's text.
fn span_chars(glyphs: &[GlyphRecord], meter: &mut Meter<'_>) -> Result<Vec<CharTuple>, Error> {
    let mut chars = Vec::new();
    let mut previous: Option<&GlyphRecord> = None;
    let mut ends_with_space = false;
    for glyph in glyphs {
        if let Some(before) = previous
            && needs_gap(previous, glyph)
            && !ends_with_space
        {
            let (origin, bbox) = gap_geometry(before, glyph);
            meter.charge(" ")?;
            try_push_text(
                &mut chars,
                (" ".to_owned(), bbox, origin, true),
                "layout chars",
            )?;
        }
        let mut text = String::new();
        try_push_glyph_text(&mut text, &glyph.text, "layout char text")?;
        meter.charge(&text)?;
        ends_with_space = text.ends_with(' ');
        try_push_text(
            &mut chars,
            (text, glyph_bbox(glyph), (glyph.x, glyph.y), false),
            "layout chars",
        )?;
        if !glyph_is_nonspacing_mark(glyph) {
            previous = Some(glyph);
        }
    }
    Ok(chars)
}

/// Whether a span of a horizontal line is set smaller than the line's
/// largest glyphs and raised above their baseline by more than a sixth of
/// their size, as a superscript or an exponent is.
fn is_superscript(glyphs: &[GlyphRecord], line: &[GlyphRecord]) -> bool {
    let Some(main) = line
        .iter()
        .filter(|glyph| is_horizontal(glyph))
        .max_by(|left, right| left.size.total_cmp(&right.size))
    else {
        return false;
    };
    let span = &glyphs[0];
    is_horizontal(span)
        && span.size < main.size * SUPERSCRIPT_SIZE
        && main.y - span.y > main.size * SUPERSCRIPT_RISE
}

/// Glyphs whose baselines differ by more than this share of their size, as
/// a superscript's does, start another span.
const SPAN_BASELINE_TOLERANCE: f64 = 0.05;

/// How far the baseline of `b` lies from that of `a`, across the direction
/// of `a`; glyphs of inferred vertical writing stack along their line.
fn baseline_shift(a: &GlyphRecord, b: &GlyphRecord) -> f64 {
    if a.writing_mode == 1 || b.writing_mode == 1 {
        return 0.0;
    }
    ((b.y - a.y) * a.direction.0 - (b.x - a.x) * a.direction.1).abs()
}

/// A superscript is set below this share of the line's size and raised by
/// more than this share of it.
const SUPERSCRIPT_SIZE: f64 = 0.9;
const SUPERSCRIPT_RISE: f64 = 1.0 / 6.0;

/// The origin and box of the space inferred between two glyphs: from the end
/// of the advance of the one before to the start of the next, along the
/// baseline of the one before and across its ascender and descender.
fn gap_geometry(before: &GlyphRecord, after: &GlyphRecord) -> ((f64, f64), BBox) {
    let start = (
        before.x + before.direction.0 * before.advance,
        before.y + before.direction.1 * before.advance,
    );
    let length = ((after.x - start.0) * before.direction.0
        + (after.y - start.1) * before.direction.1)
        .max(0.0);
    let (above, below) = before.vertical_extent();
    let block = (-before.direction.1, before.direction.0);
    let mut x0 = f64::INFINITY;
    let mut y0 = f64::INFINITY;
    let mut x1 = f64::NEG_INFINITY;
    let mut y1 = f64::NEG_INFINITY;
    for inline in [0.0, length] {
        for cross in [-above, below] {
            let x = start.0 + before.direction.0 * inline + block.0 * cross;
            let y = start.1 + before.direction.1 * inline + block.1 * cross;
            x0 = x0.min(x);
            x1 = x1.max(x);
            y0 = y0.min(y);
            y1 = y1.max(y);
        }
    }
    (start, (x0, y0, x1, y1))
}

/// Assemble a span's text, with a space for each inferred gap.
fn span_text(glyphs: &[GlyphRecord]) -> Result<String, Error> {
    let mut text = String::new();
    let mut previous: Option<&GlyphRecord> = None;
    for glyph in glyphs {
        if needs_gap(previous, glyph) && !text.ends_with(' ') {
            try_push_str_text(&mut text, " ", "layout span text")?;
        }
        try_push_glyph_text(&mut text, &glyph.text, "layout span text")?;
        if !glyph_is_nonspacing_mark(glyph) {
            previous = Some(glyph);
        }
    }
    Ok(text)
}

/// Split a line into words delimited by whitespace and gaps.
fn split_words(line: &[GlyphRecord], meter: &mut Meter<'_>) -> Result<Vec<WordTuple>, Error> {
    let mut words = Vec::new();
    let mut word_start = None;
    let mut previous = None;
    for (index, glyph) in line.iter().enumerate() {
        let is_space = glyph.text.chars().all(char::is_whitespace);
        if (is_space || needs_gap(previous, glyph))
            && let Some(start) = word_start.take()
        {
            push_word(&mut words, &line[start..index], meter)?;
        }
        if !is_space && word_start.is_none() {
            word_start = Some(index);
        }
        if !glyph_is_nonspacing_mark(glyph) {
            previous = Some(glyph);
        }
    }
    if let Some(start) = word_start {
        push_word(&mut words, &line[start..], meter)?;
    }
    Ok(words)
}

fn push_word(
    words: &mut Vec<WordTuple>,
    glyphs: &[GlyphRecord],
    meter: &mut Meter<'_>,
) -> Result<(), Error> {
    let mut text = String::new();
    for glyph in glyphs {
        try_push_glyph_text(&mut text, &glyph.text, "layout word text")?;
    }
    meter.charge(&text)?;
    try_push_text(words, (glyphs_bbox(glyphs), text), "layout words")
}

fn line_direction(line: &[GlyphRecord]) -> ((f64, f64), u8) {
    line.first().map_or(((1.0, 0.0), 0), |glyph| {
        (glyph.direction, glyph.writing_mode)
    })
}

/// Groups the lines into blocks, as [`starts_block`] separates them, and
/// builds every line's spans and words.
fn assemble_layout(
    lines: &[Vec<GlyphRecord>],
    chars: bool,
    meter: &mut Meter<'_>,
) -> Result<Vec<BlockTuple>, Error> {
    let mut blocks: Vec<Vec<&[GlyphRecord]>> = Vec::new();
    let mut previous: Option<&[GlyphRecord]> = None;
    for line in lines {
        if previous.is_none_or(|previous| starts_block(previous, line)) {
            try_push_text(&mut blocks, Vec::new(), "layout block lines")?;
        }
        previous = Some(line);
        if let Some(block) = blocks.last_mut() {
            try_push_text(block, line.as_slice(), "layout block lines")?;
        }
    }

    let mut output = Vec::new();
    for block_lines in blocks {
        let mut line_tuples = Vec::new();
        let mut x0 = f64::INFINITY;
        let mut y0 = f64::INFINITY;
        let mut x1 = f64::NEG_INFINITY;
        let mut y1 = f64::NEG_INFINITY;
        for line in block_lines {
            let bbox = glyphs_bbox(line);
            let (direction, writing_mode) = line_direction(line);
            let mut spans = Vec::new();
            for glyphs in span_glyph_slices(line) {
                let span = span_tuple(glyphs, line, chars, meter)?;
                try_push_text(&mut spans, span, "layout spans")?;
            }
            let words = split_words(line, meter)?;
            try_push_text(
                &mut line_tuples,
                (
                    bbox,
                    spans,
                    words,
                    direction,
                    writing_mode,
                    glyphs_quad(line),
                ),
                "layout lines",
            )?;
            x0 = x0.min(bbox.0);
            y0 = y0.min(bbox.1);
            x1 = x1.max(bbox.2);
            y1 = y1.max(bbox.3);
        }
        try_push_text(
            &mut output,
            ((x0, y0, x1, y1), line_tuples),
            "layout blocks",
        )?;
    }
    Ok(output)
}

/// Merge the last word of each line that dehyphenation joined with the first
/// word of the next line, which drops that word: the merged word keeps its
/// line and spans both boxes.
fn merge_joined_words(lines: &[Vec<GlyphRecord>], blocks: &mut [BlockTuple]) -> Result<(), Error> {
    let mut tuples: Vec<&mut LineTuple> = Vec::new();
    for (_, block_lines) in blocks.iter_mut() {
        for line in block_lines.iter_mut() {
            try_push_text(&mut tuples, line, "joined words")?;
        }
    }
    for index in 1..lines.len().min(tuples.len()) {
        if !lines[index][0].joins_previous {
            continue;
        }
        let (before, after) = tuples.split_at_mut(index);
        let next_words = &mut after[0].2;
        if next_words.is_empty() {
            continue;
        }
        let (tail_box, tail) = next_words.remove(0);
        let Some((head_box, head)) = before[index - 1].2.last_mut() else {
            continue;
        };
        try_push_str_text(head, &tail, "joined words")?;
        *head_box = (
            head_box.0.min(tail_box.0),
            head_box.1.min(tail_box.1),
            head_box.2.max(tail_box.2),
            head_box.3.max(tail_box.3),
        );
    }
    Ok(())
}

/// The corners of one glyph's box: its ink box, or the parallelogram of its
/// advance along the baseline and its font extents across it.
fn glyph_corners(glyph: &GlyphRecord) -> [(f64, f64); 4] {
    if glyph.ink.is_some() || glyph.writing_mode == 1 {
        let (x0, y0, x1, y1) = glyph_bbox(glyph);
        return [(x0, y0), (x1, y0), (x0, y1), (x1, y1)];
    }
    let (above, below) = glyph.vertical_extent();
    let (dx, dy) = glyph.direction;
    let (bx, by) = (-dy, dx);
    let start = (glyph.x, glyph.y);
    let end = (glyph.x + dx * glyph.advance, glyph.y + dy * glyph.advance);
    [
        (start.0 - bx * above, start.1 - by * above),
        (end.0 - bx * above, end.1 - by * above),
        (start.0 + bx * below, start.1 + by * below),
        (end.0 + bx * below, end.1 + by * below),
    ]
}

/// The quad of glyphs that share a baseline: the smallest rectangle along
/// the first glyph's baseline direction that holds every glyph's box, so
/// that the quad of rotated text follows the text.
fn glyphs_quad(glyphs: &[GlyphRecord]) -> QuadTuple {
    let first = &glyphs[0];
    let (dx, dy) = if first.writing_mode == 1 {
        (1.0, 0.0)
    } else {
        first.direction
    };
    let (bx, by) = (-dy, dx);
    let (ox, oy) = (first.x, first.y);
    let (mut u0, mut u1, mut v0, mut v1) = (
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::INFINITY,
        f64::NEG_INFINITY,
    );
    for glyph in glyphs {
        for (x, y) in glyph_corners(glyph) {
            let u = (x - ox) * dx + (y - oy) * dy;
            let v = (x - ox) * bx + (y - oy) * by;
            u0 = u0.min(u);
            u1 = u1.max(u);
            v0 = v0.min(v);
            v1 = v1.max(v);
        }
    }
    let corner = |u: f64, v: f64| (ox + dx * u + bx * v, oy + dy * u + by * v);
    (
        corner(u0, v0),
        corner(u1, v0),
        corner(u0, v1),
        corner(u1, v1),
    )
}
