//! `Page.selection`: the text that a viewer selects between two points,
//! ported from pylopdf's `TextPage::selection` and `nearest_glyph` in
//! `rust/src/extract.rs` at 036be111cafe77755db4bb2be17bc70a8f2f9d82.

use pyropdf_store::budget::{Budget, LimitCode};

use super::{
    Error, GlyphRecord, TextOptions, as_u64, assemble_text, assembled_text_size, glyph_bbox,
    try_push_text, viewed_page_lines,
};
use crate::bridge::Bridged;

/// Returns the text of the page at `index` in reading order from the glyph
/// nearest `start` to the one nearest `end`, as a viewer selects it: the
/// first and last lines in part and the lines between whole, without a final
/// line break. Also returns the number of glyphs without Unicode that it
/// left out.
///
/// The points are in display space, and the text options view the glyphs.
/// The budgets are those of [`super::page_text`]. A refusal keeps nothing.
pub fn page_selection(
    bridged: &Bridged,
    index: usize,
    options: &TextOptions,
    start: (f64, f64),
    end: (f64, f64),
    budget: &Budget,
) -> Result<(String, usize), Error> {
    let viewed = viewed_page_lines(bridged, index, options, None, budget)?;
    // The glyphs and their text give way to the text assembled from them.
    viewed.charge.release(budget);
    let lines = &viewed.lines;
    let (Some(first), Some(last)) = (nearest_glyph(lines, start), nearest_glyph(lines, end)) else {
        return Ok((String::new(), viewed.skipped));
    };
    let (first, last) = if first <= last {
        (first, last)
    } else {
        (last, first)
    };
    let mut selected: Vec<&[GlyphRecord]> = Vec::new();
    for (at, line) in lines.iter().enumerate().take(last.0 + 1).skip(first.0) {
        let from = if at == first.0 { first.1 } else { 0 };
        let to = if at == last.0 { last.1 + 1 } else { line.len() };
        try_push_text(&mut selected, &line[from..to], "selected lines")?;
    }
    let size = assembled_text_size(&selected).ok_or_else(|| {
        Error::Allocation("the selected text exceeds the address space".to_owned())
    })?;
    budget.charge(LimitCode::TextSize, as_u64(size))?;
    let mut text = assemble_text(&selected, Some(size))
        .inspect_err(|_| budget.release(LimitCode::TextSize, as_u64(size)))?;
    if text.ends_with('\n') {
        text.pop();
    }
    Ok((text, viewed.skipped))
}

/// The `(line, glyph)` of the glyph whose box lies nearest to a point, the
/// first in reading order among equally near ones, or `None` without glyphs.
fn nearest_glyph(lines: &[Vec<GlyphRecord>], (x, y): (f64, f64)) -> Option<(usize, usize)> {
    let mut nearest = None;
    let mut best = f64::INFINITY;
    for (line_index, line) in lines.iter().enumerate() {
        for (index, glyph) in line.iter().enumerate() {
            let (x0, y0, x1, y1) = glyph_bbox(glyph);
            let dx = (x0 - x).max(x - x1).max(0.0);
            let dy = (y0 - y).max(y - y1).max(0.0);
            let distance = dx.hypot(dy);
            if distance < best {
                best = distance;
                nearest = Some((line_index, index));
            }
        }
    }
    nearest
}
