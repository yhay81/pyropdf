//! `Page.search`: the case-insensitive matches of a text in a page's lines,
//! ported from pylopdf's `search_lines` and `line_search_index` in
//! `rust/src/extract.rs` at 036be111cafe77755db4bb2be17bc70a8f2f9d82.

use pyropdf_store::budget::{Budget, LimitCode};

use super::layout::{QuadTuple, glyphs_bbox, glyphs_quad};
use super::{
    BBox, Error, GlyphRecord, TextOptions, glyph_is_nonspacing_mark, needs_gap, try_push_str_text,
    try_push_text, viewed_page_lines,
};
use crate::bridge::Bridged;

/// The matches of one search.
pub enum SearchHits {
    /// One box per match, which spans the lines that a match across a
    /// dehyphenation join covers.
    Boxes(Vec<BBox>),
    /// The quad of every line of every match, in match order.
    Quads(Vec<QuadTuple>),
}

/// Returns the matches of `needle` in the text of the page at `index`, as
/// the text options view it, and the number of glyphs without Unicode that
/// it left out.
///
/// Search is line-based and finds no match across lines, but across the
/// lines that dehyphenation joins. Each match counts against
/// `search_hit_count`, and the page's glyphs against the budgets of
/// [`super::page_text`] while the search runs. A refusal keeps nothing.
pub fn page_search(
    bridged: &Bridged,
    index: usize,
    options: &TextOptions,
    needle: &str,
    quads: bool,
    budget: &Budget,
) -> Result<(SearchHits, usize), Error> {
    let viewed = viewed_page_lines(bridged, index, options, None, budget)?;
    let mut hits = if quads {
        SearchHits::Quads(Vec::new())
    } else {
        SearchHits::Boxes(Vec::new())
    };
    let mut count = 0u64;
    let searched = search_lines(&viewed.lines, needle, |segments| {
        budget.charge(LimitCode::SearchHitCount, 1)?;
        count += 1;
        match &mut hits {
            SearchHits::Boxes(boxes) => {
                let mut bbox = (
                    f64::INFINITY,
                    f64::INFINITY,
                    f64::NEG_INFINITY,
                    f64::NEG_INFINITY,
                );
                for segment in segments {
                    let (x0, y0, x1, y1) = glyphs_bbox(segment);
                    bbox = (
                        bbox.0.min(x0),
                        bbox.1.min(y0),
                        bbox.2.max(x1),
                        bbox.3.max(y1),
                    );
                }
                try_push_text(boxes, bbox, "search result geometry")
            }
            SearchHits::Quads(quads) => segments.iter().try_for_each(|segment| {
                try_push_text(quads, glyphs_quad(segment), "search result geometry")
            }),
        }
    });
    viewed.charge.release(budget);
    if let Err(error) = searched {
        budget.release(LimitCode::SearchHitCount, count);
        return Err(error);
    }
    Ok((hits, viewed.skipped))
}

/// Searches the lines case-insensitively for `needle` and hands `matched`
/// the glyphs of each match, one slice per line it covers.
fn search_lines(
    lines: &[Vec<GlyphRecord>],
    needle: &str,
    mut matched: impl FnMut(&[&[GlyphRecord]]) -> Result<(), Error>,
) -> Result<(), Error> {
    let mut lowered_needle = String::new();
    push_lowercase(&mut lowered_needle, needle.chars(), "lowercase search text")?;
    if lowered_needle.is_empty() {
        return Ok(());
    }
    let mut segments: Vec<&[GlyphRecord]> = Vec::new();
    let mut start_line = 0;
    while start_line < lines.len() {
        let mut end_line = start_line + 1;
        while lines
            .get(end_line)
            .and_then(|line| line.first())
            .is_some_and(|glyph| glyph.joins_previous)
        {
            end_line += 1;
        }
        let unit = &lines[start_line..end_line];
        start_line = end_line;
        let (haystack, map) = search_index(unit)?;
        let mut byte_cursor = 0;
        let mut char_cursor = 0;
        for (start, _) in haystack.match_indices(&lowered_needle) {
            let end = start + lowered_needle.len();
            // Byte position, then character position, then glyphs.
            let char_start = char_cursor + haystack[byte_cursor..start].chars().count();
            let char_len = haystack[start..end].chars().count();
            let glyph_map = &map[char_start..char_start + char_len];
            byte_cursor = end;
            char_cursor = char_start + char_len;
            let first = glyph_map.iter().find_map(|value| *value);
            let last = glyph_map.iter().rev().find_map(|value| *value);
            let (Some(first), Some(last)) = (first, last) else {
                continue;
            };
            segments.clear();
            for (offset, line) in unit[first.0..=last.0].iter().enumerate() {
                let at = first.0 + offset;
                let from = if at == first.0 { first.1 } else { 0 };
                let to = if at == last.0 {
                    last.1
                } else {
                    line.len().saturating_sub(1)
                };
                try_push_text(&mut segments, &line[from..=to], "search match lines")?;
            }
            matched(&segments)?;
        }
    }
    Ok(())
}

/// The `(line, glyph)` of each character of a search unit's text, `None` for
/// an inferred space.
type SearchMap = Vec<Option<(usize, usize)>>;

/// Builds the lowercase text of one search unit, a line or the lines that
/// dehyphenation joins, and the glyph of each of its characters.
///
/// A space with no glyph stands for each gap between words. When a ligature
/// or lowercasing yields several characters, each maps to the same glyph.
fn search_index(unit: &[Vec<GlyphRecord>]) -> Result<(String, SearchMap), Error> {
    let mut haystack = String::new();
    let mut map = Vec::new();
    for (line_index, line) in unit.iter().enumerate() {
        let mut previous: Option<&GlyphRecord> = None;
        for (index, glyph) in line.iter().enumerate() {
            if needs_gap(previous, glyph) && !haystack.ends_with(' ') {
                try_push_str_text(&mut haystack, " ", "search text")?;
                try_push_text(&mut map, None, "search glyph map")?;
            }
            let pushed = push_lowercase(&mut haystack, glyph.text.chars(), "search text")?;
            for _ in 0..pushed {
                try_push_text(&mut map, Some((line_index, index)), "search glyph map")?;
            }
            if !glyph_is_nonspacing_mark(glyph) {
                previous = Some(glyph);
            }
        }
    }
    Ok((haystack, map))
}

/// Appends the lowercase of `chars` to `text`, and returns how many
/// characters it appended.
fn push_lowercase(
    text: &mut String,
    chars: impl Iterator<Item = char>,
    label: &str,
) -> Result<usize, Error> {
    let mut pushed = 0;
    for ch in chars {
        for lowered in ch.to_lowercase() {
            let mut encoded = [0u8; 4];
            try_push_str_text(text, lowered.encode_utf8(&mut encoded), label)?;
            pushed += 1;
        }
    }
    Ok(pushed)
}
