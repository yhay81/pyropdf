//! Scans of input-sized text that poll the operation's budget between
//! bounded steps (DESIGN.md F6 and section 9): the search for a line end,
//! trailing whitespace, break opportunities, and grapheme boundaries. A
//! single step of `str::split`, `trim_end_matches`, `linebreaks`, or a
//! grapheme iterator can read a whole paragraph, so these scans never hand
//! one more than a step of text.

use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec};
use unicode_linebreak::{BreakClass, break_property, linebreaks, split_at_safe};
use unicode_segmentation::{GraphemeCursor, GraphemeIncomplete};

use super::layout::LayoutError;

/// The bytes, or characters, that a scan reads between two polls.
pub(super) const STEP: usize = 4_096;
/// The most characters of context that line-break discovery carries from
/// one step to the next; see [`LayoutError::BreakContext`].
pub const MAX_BREAK_CONTEXT: usize = 1_024;
/// The characters that the carried context keeps at each end of a run of
/// one line-breaking class.
const RUN_ENDS: usize = 2;

/// Polls the operation's deadline and cancellation.
pub(super) type Poll<'p> = dyn FnMut() -> Result<(), LimitExceeded> + 'p;

/// Returns the end of the step of `text` from `start`: `step` bytes on,
/// moved forward to a character boundary.
fn step_end(text: &str, start: usize, step: usize) -> usize {
    text.ceil_char_boundary(start.saturating_add(step).min(text.len()))
}

/// Returns the offset of the first byte from `start` that `matches`, which
/// only ASCII bytes may match.
pub(super) fn find_ascii(
    text: &str,
    start: usize,
    matches: impl Fn(u8) -> bool,
    poll: &mut Poll<'_>,
) -> Result<Option<usize>, LimitExceeded> {
    let bytes = text.as_bytes();
    let mut from = start;
    while from < bytes.len() {
        poll()?;
        let end = from.saturating_add(STEP).min(bytes.len());
        if let Some(index) = bytes[from..end].iter().position(|&byte| matches(byte)) {
            return Ok(Some(from + index));
        }
        from = end;
    }
    Ok(None)
}

/// Returns `text` without the whitespace at its end.
pub(super) fn trim_end<'t>(text: &'t str, poll: &mut Poll<'_>) -> Result<&'t str, LimitExceeded> {
    for (index, (offset, character)) in text.char_indices().rev().enumerate() {
        if index > 0 && index % STEP == 0 {
            poll()?;
        }
        if !character.is_whitespace() {
            return Ok(&text[..offset + character.len_utf8()]);
        }
    }
    Ok("")
}

/// Returns the ends of the break opportunities of `text` by UAX #14, as
/// `unicode_linebreak::linebreaks` finds them, the end of the text last.
///
/// `linebreaks` reads up to the next opportunity in one step, so it reads
/// each step of the text after a context that leaves its state machine as
/// the text before left it. The context starts at the last opportunity, or
/// at the last pair of characters after which no break depends on what
/// precedes them (`split_at_safe`), and a run of one line-breaking class in
/// it keeps only its ends, which decide the breaks that follow it. A context
/// still longer than [`MAX_BREAK_CONTEXT`] characters is refused rather than
/// read whole.
pub(super) fn break_ends<'b>(
    text: &str,
    budget: &'b Budget,
    poll: &mut Poll<'_>,
) -> Result<BudgetVec<'b, usize>, LayoutError> {
    break_ends_with(text, budget, STEP, MAX_BREAK_CONTEXT, poll)
}

pub(super) fn break_ends_with<'b>(
    text: &str,
    budget: &'b Budget,
    step: usize,
    max_context: usize,
    poll: &mut Poll<'_>,
) -> Result<BudgetVec<'b, usize>, LayoutError> {
    let mut ends = BudgetVec::new(budget);
    // Both strings stay below `step` bytes and `max_context` characters.
    let mut context = String::new();
    let mut window = String::new();
    let mut start = 0;
    while start < text.len() {
        poll()?;
        let end = step_end(text, start, step);
        window.clear();
        window.push_str(&context);
        window.push_str(&text[start..end]);
        let base = context.len();
        let mut restart = 0;
        for (offset, _) in linebreaks(&window) {
            if offset == window.len() {
                // The window's end is the text's end only in the last step.
                if end == text.len() {
                    ends.try_push(text.len())?;
                }
            } else {
                if offset >= base {
                    ends.try_push(start + offset - base)?;
                }
                restart = offset;
            }
        }
        let (head, _) = split_at_safe(&window);
        if !head.is_empty()
            && let Some(character) = window[head.len()..].chars().next()
        {
            restart = restart.max(head.len() + character.len_utf8());
        }
        if keep_run_ends(&window[restart..], &mut context) > max_context {
            return Err(LayoutError::BreakContext);
        }
        start = end;
    }
    Ok(ends)
}

/// Writes `text` to `out` with each run of characters of one line-breaking
/// class cut to its first and last [`RUN_ENDS`] characters, and returns the
/// characters written.
fn keep_run_ends(text: &str, out: &mut String) -> usize {
    out.clear();
    let mut written = 0;
    let mut class: Option<BreakClass> = None;
    let mut run = 0_usize;
    // The last characters of the run past its first `RUN_ENDS`, as a ring.
    let mut tail = ['\0'; RUN_ENDS];
    for character in text.chars() {
        let current = break_property(u32::from(character));
        if class != Some(current) {
            written += push_run_end(out, run, &tail);
            class = Some(current);
            run = 0;
        }
        if run < RUN_ENDS {
            out.push(character);
            written += 1;
        } else {
            tail[(run - RUN_ENDS) % RUN_ENDS] = character;
        }
        run += 1;
    }
    written + push_run_end(out, run, &tail)
}

/// Writes the last characters of a run of `run` characters from its ring.
fn push_run_end(out: &mut String, run: usize, tail: &[char; RUN_ENDS]) -> usize {
    let past = run.saturating_sub(RUN_ENDS);
    let kept = past.min(RUN_ENDS);
    for index in past - kept..past {
        out.push(tail[index % RUN_ENDS]);
    }
    kept
}

/// Calls `found` with the end of each extended grapheme of `text`, in
/// order, as `unicode_segmentation`'s grapheme iterator finds them, while
/// letting its cursor read only a step further at a time.
///
/// The chunks that the cursor reads all start at the text's start, so it
/// never asks for the text before a chunk: its answers to such requests
/// lose the parity of regional indicators. Its own look back for a
/// conjunct, an emoji sequence, or a regional indicator still reads a run
/// of characters that precede the decision in one step.
pub(super) fn grapheme_ends(
    text: &str,
    poll: &mut Poll<'_>,
    mut found: impl FnMut(usize) -> Result<(), LimitExceeded>,
) -> Result<(), LimitExceeded> {
    grapheme_ends_with(text, STEP, poll, &mut found)
}

pub(super) fn grapheme_ends_with(
    text: &str,
    step: usize,
    poll: &mut Poll<'_>,
    found: &mut dyn FnMut(usize) -> Result<(), LimitExceeded>,
) -> Result<(), LimitExceeded> {
    let mut cursor = GraphemeCursor::new(0, text.len(), true);
    let mut end = step_end(text, 0, step);
    let mut boundaries = 0_usize;
    loop {
        match cursor.next_boundary(&text[..end], 0) {
            Ok(None) => return Ok(()),
            Ok(Some(boundary)) => {
                boundaries += 1;
                if boundaries.is_multiple_of(step) {
                    poll()?;
                }
                found(boundary)?;
            }
            Err(GraphemeIncomplete::NextChunk) => {
                poll()?;
                end = step_end(text, end, step);
            }
            // A chunk from the text's start holds all the context there is,
            // and the cursor only moves forward inside the text.
            Err(
                GraphemeIncomplete::PreContext(_)
                | GraphemeIncomplete::PrevChunk
                | GraphemeIncomplete::InvalidOffset,
            ) => unreachable!("a forward grapheme cursor asked for earlier text"),
        }
    }
}
