//! A bounded paragraph layout: each line breaks greedily at the last break
//! opportunity of UAX #14 that fits, and inside a word, at a grapheme
//! boundary, only when the word alone is wider than the box.
//!
//! The layout measures text through a closure, so it serves any font whose
//! widths the caller knows.

use std::borrow::Cow;
use std::fmt;

use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};

use super::scan::{self, Poll};

/// How far a width or height may pass its box and still fit it.
const FIT_TOLERANCE: f64 = 1.0e-6;
/// The longest first word that is measured whole before a break inside it
/// is sought.
const DIRECT_BREAK_MEASURE_BYTES: usize = 64;
/// The most lines that one layout makes.
pub const MAX_LINES: usize = 4_096;

/// Why a layout refused its text.
#[derive(Clone, Debug, PartialEq)]
pub enum LayoutError {
    /// The default appearance exceeds its parsing bound.
    AppearanceTooLong,
    /// A size or coordinate cannot produce valid PDF numbers.
    InvalidGeometry,
    /// The text needs more than [`MAX_LINES`] lines.
    Lines,
    /// The box is narrower than one grapheme of the text.
    Narrow,
    /// A Standard 14 text holds a character outside WinAnsi.
    NotWinAnsi,
    /// The text does not fit its box at any size down to 0.01 points.
    Unfit,
    /// A paragraph holds more than
    /// [`MAX_BREAK_CONTEXT`](super::MAX_BREAK_CONTEXT) characters, past the
    /// runs of one line-breaking class, whose break opportunities depend on
    /// one another with no opportunity between them, such as combining
    /// marks alternating with zero-width joiners.
    BreakContext,
    /// The operation's budget refused the layout's memory.
    Limit(LimitExceeded),
}

impl From<LimitExceeded> for LayoutError {
    fn from(refusal: LimitExceeded) -> Self {
        Self::Limit(refusal)
    }
}

impl fmt::Display for LayoutError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::AppearanceTooLong => "default appearance exceeds the parsing limit",
            Self::InvalidGeometry => "text geometry must contain finite, nonnegative sizes",
            Self::Lines => "text layout exceeds the configured logical-line limit",
            Self::Narrow => "textbox is too narrow to fit one text grapheme",
            Self::NotWinAnsi => "Standard 14 text contains a character outside WinAnsi",
            Self::Unfit => "the text cannot fit inside its box",
            Self::BreakContext => {
                "text holds too long a run of characters whose line breaks depend on one another"
            }
            Self::Limit(refusal) => return fmt::Display::fmt(refusal, formatter),
        })
    }
}

impl std::error::Error for LayoutError {}

/// One laid-out line.
#[derive(Clone, Debug, PartialEq)]
pub struct Line<'t> {
    /// The text of the line, without the whitespace at its end.
    pub text: &'t str,
    /// The width that the measure gave the text.
    pub width: f64,
    /// Whether the line's spaces stretch to the box's width: only for a
    /// line that a wrap ended, in a justified layout.
    pub justify: bool,
}

/// The lines of a text in a box, laid out before anything is drawn.
#[derive(Clone, Debug, PartialEq)]
pub struct TextBox<'t> {
    /// The lines from the top.
    pub lines: Vec<Line<'t>>,
    /// The height that the lines need, as the placement defines it.
    pub height: f64,
    /// The box's height minus [`TextBox::height`].
    pub spare_height: f64,
    /// How far below the box's top the first baseline lies, in em.
    pub ascent: f64,
    /// The distance between baselines in points.
    pub leading: f64,
}

impl TextBox<'_> {
    /// Returns whether the lines fit the box's height.
    pub fn fits(&self) -> bool {
        self.spare_height >= -FIT_TOLERANCE
    }
}

/// The ascender and descender of a font as fractions of the em: the
/// ascender positive, and the descender, below the baseline, negative.
/// Canvas text and text boxes place lines with them.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct LineMetrics {
    /// The height above the baseline.
    pub ascender: f64,
    /// The depth below the baseline, negative.
    pub descender: f64,
}

impl LineMetrics {
    /// Returns the distance between baselines in em: `line_height` when
    /// given, else the ascender minus the descender, or 1.2 when that is at
    /// most 1, as pymupdf's `insert_text` spaces lines.
    ///
    /// pymupdf compares the single-precision metrics that MuPDF reports, in
    /// which 0.8 and -0.2 span 1.000000015 em, so such a face keeps its
    /// extent of 1 em.
    pub fn line_height(self, line_height: Option<f64>) -> f64 {
        line_height.unwrap_or_else(|| {
            #[expect(
                clippy::cast_possible_truncation,
                reason = "MuPDF reports the metrics in single precision"
            )]
            let (ascender, descender) = (self.ascender as f32, self.descender as f32);
            if f64::from(ascender) - f64::from(descender) <= 1.0 {
                1.2
            } else {
                self.ascender - self.descender
            }
        })
    }

    /// Returns the distance between baselines in points, or `None` when it
    /// is not finite, which content operators cannot write.
    pub fn leading(self, font_size: f64, line_height: Option<f64>) -> Option<f64> {
        Some(font_size * self.line_height(line_height)).filter(|leading| leading.is_finite())
    }
}

/// How a box places its lines and decides whether they fit: the first
/// baseline `ascent` em below the box's top, the others `line_height` em
/// apart, and room from the first line's ascent to the last line's descent.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Placement {
    /// The room above the first baseline, in em.
    pub ascent: f64,
    /// The room below the last baseline, in em, positive.
    pub descent: f64,
    /// The distance between baselines, in em.
    pub line_height: f64,
}

impl Placement {
    /// Places lines as canvas text spaces them: the first baseline one
    /// ascender below the box's top, the others `line_height` em apart, by
    /// default the font's ([`LineMetrics::line_height`]), and room down to
    /// the last line's descender.
    pub fn lines(metrics: LineMetrics, line_height: Option<f64>) -> Self {
        Self {
            ascent: metrics.ascender,
            descent: -metrics.descender,
            line_height: metrics.line_height(line_height),
        }
    }

    fn leading(self, font_size: f64) -> f64 {
        self.line_height * font_size
    }

    #[expect(
        clippy::cast_precision_loss,
        reason = "a layout holds at most MAX_LINES lines"
    )]
    fn required_height(self, lines: usize, font_size: f64) -> f64 {
        if lines == 0 {
            return 0.0;
        }
        (self.ascent + self.descent) * font_size + (lines - 1) as f64 * self.leading(font_size)
    }
}

/// Lays out `text` in a box of `box_size` at `font_size`: each `\n` starts
/// a paragraph, whose lines break greedily at the last break opportunity
/// that `measure` finds narrow enough, and inside a word only when the word
/// alone is too wide. Lines keep no whitespace at their end.
///
/// Refuses more than [`MAX_LINES`] lines, a box narrower than one
/// grapheme, and memory that `budget` does not allow. The text's height is
/// not refused: [`TextBox::fits`] tells whether it fits.
pub fn layout<'t>(
    text: &'t str,
    box_size: (f64, f64),
    font_size: f64,
    placement: Placement,
    justify: bool,
    budget: &Budget,
    mut measure: impl FnMut(&str) -> f64,
) -> Result<TextBox<'t>, LayoutError> {
    budget.poll()?;
    let (width, height) = box_size;
    if ![
        width,
        height,
        font_size,
        placement.ascent,
        placement.descent,
        placement.line_height,
    ]
    .iter()
    .all(|value| value.is_finite() && *value >= 0.0)
    {
        return Err(LayoutError::InvalidGeometry);
    }
    let mut lines = BudgetVec::new(budget);
    let poll = &mut || budget.poll();
    let mut start = 0;
    loop {
        let end = scan::find_ascii(text, start, |byte| byte == b'\n', poll)?;
        let paragraph = &text[start..end.unwrap_or(text.len())];
        wrap(paragraph, width, justify, &mut lines, &mut measure, poll)?;
        match end {
            Some(end) => start = end + 1,
            None => break,
        }
    }

    let required = placement.required_height(lines.len(), font_size);
    if !required.is_finite() || !placement.leading(font_size).is_finite() {
        return Err(LayoutError::InvalidGeometry);
    }
    let mut spare_height = height - required;
    if spare_height.abs() <= FIT_TOLERANCE {
        spare_height = 0.0;
    }
    Ok(TextBox {
        lines: lines.into_vec(),
        height: required,
        spare_height,
        ascent: placement.ascent,
        leading: placement.leading(font_size),
    })
}

/// Appends a line, or refuses it as the line after the budget's
/// `text_line_count`, [`MAX_LINES`] unless the caller lifts it.
fn push<'t>(lines: &mut BudgetVec<'_, Line<'t>>, line: Line<'t>) -> Result<(), LayoutError> {
    let count = u64::try_from(lines.len())
        .unwrap_or(u64::MAX)
        .saturating_add(1);
    match lines.budget().check(LimitCode::TextLineCount, count) {
        Err(refusal) if refusal.code() == LimitCode::TextLineCount => {
            return Err(LayoutError::Lines);
        }
        Err(refusal) => return Err(refusal.into()),
        Ok(()) => {}
    }
    Ok(lines.try_push(line)?)
}

/// Appends the lines of one paragraph.
fn wrap<'t>(
    paragraph: &'t str,
    max_width: f64,
    justify: bool,
    lines: &mut BudgetVec<'_, Line<'t>>,
    measure: &mut impl FnMut(&str) -> f64,
    poll: &mut Poll<'_>,
) -> Result<(), LayoutError> {
    // A paragraph that fits whole is measured once, so that a wide box
    // stays linear rather than measuring growing prefixes.
    let visible = scan::trim_end(paragraph, poll)?;
    let paragraph_width = measure(visible);
    if paragraph.is_empty() || paragraph_width <= max_width + FIT_TOLERANCE {
        return push(
            lines,
            Line {
                text: visible,
                width: if paragraph.is_empty() {
                    0.0
                } else {
                    paragraph_width
                },
                justify: false,
            },
        );
    }

    let budget = lines.budget();
    let break_ends = scan::break_ends(paragraph, budget, poll)?;
    let mut grapheme_ends = BudgetVec::new(budget);
    scan::grapheme_ends(paragraph, poll, |end| grapheme_ends.try_push(end))?;
    let mut start = 0;
    while start < paragraph.len() {
        let (consumed, visible_end, width) = greedy_break(
            paragraph,
            start,
            max_width,
            &break_ends,
            &grapheme_ends,
            measure,
            poll,
        )?;
        let soft_break = start + consumed < paragraph.len();
        push(
            lines,
            Line {
                text: &paragraph[start..start + visible_end],
                width,
                justify: justify && soft_break,
            },
        )?;
        start += consumed;
    }
    Ok(())
}

/// A line's break: the bytes that it consumes from its start, the bytes of
/// its visible text, and that text's width.
type Break = (usize, usize, f64);

fn measure_break(
    text: &str,
    start: usize,
    end: usize,
    measure: &mut impl FnMut(&str) -> f64,
    poll: &mut Poll<'_>,
) -> Result<Break, LimitExceeded> {
    let visible = scan::trim_end(&text[start..end], poll)?;
    Ok((end - start, visible.len(), measure(visible)))
}

/// Returns the longest line from `start` that ends at a break opportunity
/// and fits `max_width`, or the longest run of graphemes that fits when the
/// first word does not.
fn greedy_break(
    text: &str,
    start: usize,
    max_width: f64,
    break_ends: &[usize],
    grapheme_ends: &[usize],
    measure: &mut impl FnMut(&str) -> f64,
    poll: &mut Poll<'_>,
) -> Result<Break, LayoutError> {
    // The last break ends the text, so the first after `start` exists.
    let mut first = break_ends.partition_point(|end| *end <= start);
    // Leading whitespace does not make a blank line of its own. The text
    // from `start` is blank while each segment between opportunities is,
    // so each segment is read once.
    let mut segment_start = start;
    while first + 1 < break_ends.len() {
        poll()?;
        if !scan::trim_end(&text[segment_start..break_ends[first]], poll)?.is_empty() {
            break;
        }
        segment_start = break_ends[first];
        first += 1;
    }

    let first_end = break_ends[first];
    let first_visible = scan::trim_end(&text[start..first_end], poll)?;
    let first_visible_end = start + first_visible.len();
    let first_candidate = if first_visible.is_empty() {
        (first_end - start, 0, measure(first_visible))
    } else if first_visible.len() <= DIRECT_BREAK_MEASURE_BYTES {
        let candidate = measure_break(text, start, first_end, measure, poll)?;
        if candidate.2 > max_width + FIT_TOLERANCE {
            return emergency_break(
                text,
                start,
                first_visible_end,
                max_width,
                grapheme_ends,
                measure,
            );
        }
        candidate
    } else {
        let candidate = emergency_break(
            text,
            start,
            first_visible_end,
            max_width,
            grapheme_ends,
            measure,
        )?;
        if candidate.0 < first_visible.len() {
            return Ok(candidate);
        }
        (first_end - start, first_visible.len(), candidate.2)
    };

    // Probe ever more distant break opportunities, then search only the
    // interval where the line stops fitting, rather than measuring every
    // growing prefix.
    let last = break_ends.len() - 1;
    let mut best_index = first;
    let mut best = first_candidate;
    let mut offset = 1_usize;
    let failed_index = loop {
        if best_index == last {
            return Ok(best);
        }
        let probe = first.saturating_add(offset).min(last);
        let candidate = measure_break(text, start, break_ends[probe], measure, poll)?;
        if candidate.2 > max_width + FIT_TOLERANCE {
            break probe;
        }
        best_index = probe;
        best = candidate;
        offset = offset.saturating_mul(2);
    };

    let (mut low, mut high) = (best_index + 1, failed_index);
    while low < high {
        let middle = low + (high - low) / 2;
        let candidate = measure_break(text, start, break_ends[middle], measure, poll)?;
        if candidate.2 <= max_width + FIT_TOLERANCE {
            best = candidate;
            low = middle + 1;
        } else {
            high = middle;
        }
    }
    Ok(best)
}

/// Returns the longest run of graphemes from `start`, up to `max_end`, that
/// fits `max_width`; refuses a box narrower than the first grapheme.
fn emergency_break(
    text: &str,
    start: usize,
    max_end: usize,
    max_width: f64,
    grapheme_ends: &[usize],
    measure: &mut impl FnMut(&str) -> f64,
) -> Result<Break, LayoutError> {
    let first = grapheme_ends.partition_point(|end| *end <= start);
    // A grapheme may reach past the visible text; the first one is taken
    // whole then.
    let last = grapheme_ends
        .partition_point(|end| *end <= max_end)
        .saturating_sub(1)
        .max(first);
    let run = |index: usize| grapheme_ends[index] - start;
    let first_width = measure(&text[start..grapheme_ends[first]]);
    if first_width > max_width + FIT_TOLERANCE {
        return Err(LayoutError::Narrow);
    }

    // Galloping keeps a narrow line local, and the search that follows
    // avoids measuring every prefix of a long unbreakable run.
    let mut best_index = first;
    let mut best_width = first_width;
    let mut offset = 1_usize;
    let failed_index = loop {
        if best_index == last {
            return Ok((run(best_index), run(best_index), best_width));
        }
        let probe = first.saturating_add(offset).min(last);
        let width = measure(&text[start..grapheme_ends[probe]]);
        if width > max_width + FIT_TOLERANCE {
            break probe;
        }
        best_index = probe;
        best_width = width;
        offset = offset.saturating_mul(2);
    };

    let (mut low, mut high) = (best_index + 1, failed_index);
    while low < high {
        let middle = low + (high - low) / 2;
        let width = measure(&text[start..grapheme_ends[middle]]);
        if width <= max_width + FIT_TOLERANCE {
            best_index = middle;
            best_width = width;
            low = middle + 1;
        } else {
            high = middle;
        }
    }
    Ok((run(best_index), run(best_index), best_width))
}

/// Returns `text` with each CR and LF a space, as a single-line field shows
/// it. A copy is charged to `budget`.
pub fn single_line<'t>(text: &'t str, budget: &Budget) -> Result<Cow<'t, str>, LimitExceeded> {
    budget.poll()?;
    let poll = &mut || budget.poll();
    let is_line_end = |byte| byte == b'\r' || byte == b'\n';
    let Some(first) = scan::find_ascii(text, 0, is_line_end, poll)? else {
        return Ok(Cow::Borrowed(text));
    };
    budget.charge(LimitCode::MemorySize, text.len() as u64)?;
    let mut line = String::with_capacity(text.len());
    line.push_str(&text[..first]);
    let mut start = first;
    while start < text.len() {
        poll()?;
        let end = text.ceil_char_boundary(start.saturating_add(scan::STEP).min(text.len()));
        line.extend(text[start..end].chars().map(|character| {
            if matches!(character, '\r' | '\n') {
                ' '
            } else {
                character
            }
        }));
        start = end;
    }
    Ok(Cow::Owned(line))
}

/// Returns the number of extended graphemes of `text`, the characters that
/// a comb field gives a cell each.
pub fn grapheme_count(text: &str, budget: &Budget) -> Result<usize, LimitExceeded> {
    budget.poll()?;
    let mut count = 0_usize;
    scan::grapheme_ends(text, &mut || budget.poll(), |_| {
        count += 1;
        Ok(())
    })?;
    Ok(count)
}

/// Returns the extended graphemes of `text`.
pub fn graphemes<'t, 'b>(
    text: &'t str,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, &'t str>, LimitExceeded> {
    budget.poll()?;
    let mut graphemes = BudgetVec::new(budget);
    let mut start = 0;
    scan::grapheme_ends(text, &mut || budget.poll(), |end| {
        graphemes.try_push(&text[start..end])?;
        start = end;
        Ok(())
    })?;
    Ok(graphemes)
}
