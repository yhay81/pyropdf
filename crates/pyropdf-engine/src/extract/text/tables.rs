//! `Page.tables`: the tables that a page's vector rules bound or its text
//! aligns, ported from pylopdf's `TablePage` and table detection in
//! `rust/src/extract.rs` at 036be111cafe77755db4bb2be17bc70a8f2f9d82.

use pyropdf_store::budget::Budget;

use super::layout::{RuleSegment, glyphs_bbox};
use super::{
    BBox, Error, GlyphRecord, LINE_SEGMENT_GAP, LINE_TOLERANCE, RtlLineOrder, TextOptions,
    cluster_lines, collect_page_glyphs, glyph_end, glyph_is_nonspacing_mark, glyph_progress,
    glyphs_rtl_order, has_vertical_baseline, line_bbox, line_segment_count, needs_gap,
    try_push_glyph_text, try_push_str_text, try_push_text,
};
use crate::bridge::Bridged;

/// Most slots a single detected table can contain before detection skips it.
const MAX_TABLE_CELLS: usize = 4096;
/// Most cell-span candidates one grid examines before detection skips it.
const MAX_TABLE_SPAN_CANDIDATES: usize = 65_536;
/// Fewest rows that a borderless text table holds.
const MIN_TEXT_TABLE_ROWS: usize = 3;
/// Most columns that a borderless text table holds.
const MAX_TEXT_TABLE_COLUMNS: usize = 32;
/// How far, in ems, the column edges of a text table's rows may differ.
const TEXT_TABLE_ALIGNMENT_TOLERANCE: f64 = 1.0;
/// The widest gap, in ems, between two rows of one text table.
const TEXT_TABLE_ROW_GAP: f64 = 2.5;
/// Fewest evenly led text records that split a coarse grid span.
const MIN_HYBRID_GRID_ROWS: usize = 3;
/// The share of a grid's slots that the inferred records occupy.
const HYBRID_GRID_OCCUPANCY_RATIO: f64 = 0.5;
/// How alike the occupied slots of two inferred records are.
const HYBRID_GRID_SLOT_SIMILARITY: f64 = 0.8;
/// How much, in ems, the leading of inferred records may vary.
const HYBRID_GRID_LEADING_VARIATION: f64 = 0.75;
/// How far, in points, rules and boxes snap to each other.
const TABLE_SNAP_TOLERANCE: f64 = 1.0;

/// A table slot: its box and text, or `None` for a slot that a merged cell
/// covers.
pub type TableCell = Option<(BBox, String)>;
type MaterializedGrid = (Vec<TableCell>, Vec<u32>);
type BorrowedWord<'a> = (BBox, &'a [GlyphRecord]);
/// A table's confidence, and for a text table its alignment error, minimum
/// gutter, and row gap variation in ems.
pub type TableDiagnosticsTuple = (f64, Option<f64>, Option<f64>, Option<f64>);
/// A table: its box, row and column counts, row-major slots, the slot that
/// anchors the cell covering each slot, and its diagnostics.
pub type TableTuple = (
    BBox,
    u32,
    u32,
    Vec<TableCell>,
    Vec<u32>,
    TableDiagnosticsTuple,
);

/// Returns the tables of the page at `index` whose whole box lies inside
/// `clip`, from its vector rules or, with `text_strategy`, from its aligned
/// borderless rows; and the number of glyphs without Unicode left out.
///
/// The page's glyphs count against the budgets of [`super::page_text`]
/// while detection runs. A refusal keeps nothing.
pub fn page_tables(
    bridged: &Bridged,
    index: usize,
    text_strategy: bool,
    clip: Option<BBox>,
    budget: &Budget,
) -> Result<(Vec<TableTuple>, usize), Error> {
    let options = TextOptions {
        styles: !text_strategy,
        ..TextOptions::default()
    };
    let collected = collect_page_glyphs(bridged, index, &options, None, budget)?;
    let charge = collected.charge;
    let detected = cluster_lines(collected.glyphs).and_then(|lines| {
        if text_strategy {
            detect_text_tables(&lines)
        } else {
            detect_grid_tables(&lines, &collected.rules)
        }
    });
    charge.release(budget);
    let mut tables = detected?;
    tables.retain(|table| clip.is_none_or(|clip| bbox_is_inside(table.0, clip)));
    Ok((tables, collected.skipped))
}

/// Returns whether a box lies inside a display-space clip.
fn bbox_is_inside((x0, y0, x1, y1): BBox, (clip_x0, clip_y0, clip_x1, clip_y1): BBox) -> bool {
    x0 >= clip_x0 - TABLE_SNAP_TOLERANCE
        && y0 >= clip_y0 - TABLE_SNAP_TOLERANCE
        && x1 <= clip_x1 + TABLE_SNAP_TOLERANCE
        && y1 <= clip_y1 + TABLE_SNAP_TOLERANCE
}

fn try_filled_text<T: Clone>(len: usize, value: T, label: &str) -> Result<Vec<T>, Error> {
    let mut values = Vec::new();
    values
        .try_reserve_exact(len)
        .map_err(|error| Error::Allocation(format!("failed to reserve {label}: {error}")))?;
    values.resize(len, value);
    Ok(values)
}

/// Borrows the independently positioned columns or cells of one line.
fn split_line_segment_slices(line: &[GlyphRecord]) -> Result<Vec<&[GlyphRecord]>, Error> {
    let mut segments = Vec::new();
    let mut segment_start = 0;
    let mut previous_end: Option<f64> = None;
    let mut previous_size = 0.0_f64;
    for (index, glyph) in line.iter().enumerate() {
        let threshold = previous_size.max(glyph.size).max(1.0) * LINE_SEGMENT_GAP;
        if previous_end.is_some_and(|end| glyph_progress(glyph) - end > threshold) {
            try_push_text(
                &mut segments,
                &line[segment_start..index],
                "borrowed text-line segments",
            )?;
            segment_start = index;
        }
        previous_end = Some(glyph_end(glyph));
        previous_size = glyph.size;
    }
    if segment_start < line.len() {
        try_push_text(
            &mut segments,
            &line[segment_start..],
            "borrowed text-line segments",
        )?;
    }
    Ok(segments)
}

fn is_pure_rtl_line(line: &[GlyphRecord]) -> bool {
    glyphs_rtl_order(line) == RtlLineOrder::Pure
}

/// Appends the text of a glyph slice in logical order.
fn try_push_logical_glyphs(
    output: &mut String,
    glyphs: &[GlyphRecord],
    label: &str,
) -> Result<(), Error> {
    if !is_pure_rtl_line(glyphs) {
        for glyph in glyphs {
            try_push_glyph_text(output, &glyph.text, label)?;
        }
        return Ok(());
    }
    let mut end = glyphs.len();
    while end > 0 {
        let mut start = end - 1;
        while start > 0 && glyph_is_nonspacing_mark(&glyphs[start]) {
            start -= 1;
        }
        for glyph in &glyphs[start..end] {
            try_push_glyph_text(output, &glyph.text, label)?;
        }
        end = start;
    }
    Ok(())
}

/// Borrows the words of a line without copying their text.
fn split_word_slices(line: &[GlyphRecord]) -> Result<Vec<BorrowedWord<'_>>, Error> {
    let mut words = Vec::new();
    let mut word_start = None;
    let mut previous = None;
    for (index, glyph) in line.iter().enumerate() {
        let is_space = glyph.text.chars().all(char::is_whitespace);
        if (is_space || needs_gap(previous, glyph))
            && let Some(start) = word_start.take()
        {
            let glyphs = &line[start..index];
            try_push_text(
                &mut words,
                (glyphs_bbox(glyphs), glyphs),
                "borrowed text words",
            )?;
        }
        if !is_space && word_start.is_none() {
            word_start = Some(index);
        }
        if !glyph_is_nonspacing_mark(glyph) {
            previous = Some(glyph);
        }
    }
    if let Some(start) = word_start {
        let glyphs = &line[start..];
        try_push_text(
            &mut words,
            (glyphs_bbox(glyphs), glyphs),
            "borrowed text words",
        )?;
    }
    Ok(words)
}

struct RuleComponents {
    parent: Vec<usize>,
}

impl RuleComponents {
    fn new(len: usize) -> Result<Self, Error> {
        let mut parent = Vec::new();
        for index in 0..len {
            try_push_text(&mut parent, index, "table rule components")?;
        }
        Ok(Self { parent })
    }

    fn find(&mut self, index: usize) -> usize {
        let parent = self.parent[index];
        if parent != index {
            self.parent[index] = self.find(parent);
        }
        self.parent[index]
    }

    fn union(&mut self, left: usize, right: usize) {
        let left_root = self.find(left);
        let right_root = self.find(right);
        if left_root != right_root {
            self.parent[right_root] = left_root;
        }
    }
}

/// Return whether perpendicular rule segments meet within snap tolerance.
fn rules_intersect(horizontal: RuleSegment, vertical: RuleSegment) -> bool {
    vertical.x0 >= horizontal.x0 - TABLE_SNAP_TOLERANCE
        && vertical.x0 <= horizontal.x1 + TABLE_SNAP_TOLERANCE
        && horizontal.y0 >= vertical.y0 - TABLE_SNAP_TOLERANCE
        && horizontal.y0 <= vertical.y1 + TABLE_SNAP_TOLERANCE
}

/// Snap nearby line coordinates to one stable grid coordinate.
fn clustered_coordinates(mut values: Vec<f64>) -> Vec<f64> {
    values.sort_unstable_by(f64::total_cmp);
    if values.is_empty() {
        return values;
    }
    let mut write = 0;
    let mut sum = values[0];
    let mut count = 1usize;
    for read in 1..values.len() {
        let value = values[read];
        if (value - sum / count as f64).abs() <= TABLE_SNAP_TOLERANCE {
            sum += value;
            count += 1;
        } else {
            values[write] = sum / count as f64;
            write += 1;
            sum = value;
            count = 1;
        }
    }
    values[write] = sum / count as f64;
    values.truncate(write + 1);
    values
}

/// Return whether collinear rule fragments cover an entire cell edge.
fn rule_covers(
    rules: &[RuleSegment],
    horizontal: bool,
    fixed: f64,
    start: f64,
    end: f64,
) -> Result<bool, Error> {
    let mut intervals = Vec::new();
    for rule in rules {
        if rule.horizontal != horizontal {
            continue;
        }
        let rule_fixed = if horizontal { rule.y0 } else { rule.x0 };
        if (rule_fixed - fixed).abs() > TABLE_SNAP_TOLERANCE {
            continue;
        }
        let (rule_start, rule_end) = if horizontal {
            (rule.x0, rule.x1)
        } else {
            (rule.y0, rule.y1)
        };
        if rule_end >= start - TABLE_SNAP_TOLERANCE && rule_start <= end + TABLE_SNAP_TOLERANCE {
            try_push_text(
                &mut intervals,
                (rule_start, rule_end),
                "table edge intervals",
            )?;
        }
    }
    intervals.sort_unstable_by(|left, right| left.0.total_cmp(&right.0));

    let mut covered = start;
    for (interval_start, interval_end) in intervals {
        if interval_start > covered + TABLE_SNAP_TOLERANCE {
            break;
        }
        covered = covered.max(interval_end);
        if covered >= end - TABLE_SNAP_TOLERANCE {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Return whether a candidate cell has all four outer borders.
fn cell_is_bounded(rules: &[RuleSegment], (x0, y0, x1, y1): BBox) -> Result<bool, Error> {
    Ok(rule_covers(rules, true, y0, x0, x1)?
        && rule_covers(rules, true, y1, x0, x1)?
        && rule_covers(rules, false, x0, y0, y1)?
        && rule_covers(rules, false, x1, y0, y1)?)
}

/// Reject a spanning candidate if a complete internal rule splits it.
fn cell_has_internal_split(
    rules: &[RuleSegment],
    xs: &[f64],
    ys: &[f64],
    row_start: usize,
    row_end: usize,
    column_start: usize,
    column_end: usize,
) -> Result<bool, Error> {
    let (x0, x1) = (xs[column_start], xs[column_end]);
    let (y0, y1) = (ys[row_start], ys[row_end]);
    for &x in &xs[column_start + 1..column_end] {
        if rule_covers(rules, false, x, y0, y1)? {
            return Ok(true);
        }
    }
    for &y in &ys[row_start + 1..row_end] {
        if rule_covers(rules, true, y, x0, x1)? {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Tile a detected grid with the smallest bounded cells, allowing rectangular
/// row/column spans where an internal rule is absent.
fn materialize_grid_cells(
    rules: &[RuleSegment],
    xs: &[f64],
    ys: &[f64],
    word_lines: &[Vec<BorrowedWord<'_>>],
) -> Result<Option<MaterializedGrid>, Error> {
    let row_count = ys.len() - 1;
    let column_count = xs.len() - 1;
    let Some(slot_count) = row_count.checked_mul(column_count) else {
        return Ok(None);
    };
    if slot_count > MAX_TABLE_CELLS {
        return Ok(None);
    }

    let mut cells = try_filled_text(slot_count, None, "bordered table cells")?;
    let mut cell_anchors = try_filled_text(slot_count, 0, "bordered table cell anchors")?;
    let mut covered = try_filled_text(slot_count, false, "bordered table coverage map")?;
    let mut span_candidates = 0;
    for row_start in 0..row_count {
        for column_start in 0..column_count {
            let slot = row_start * column_count + column_start;
            if covered[slot] {
                continue;
            }

            let base_bbox = (
                xs[column_start],
                ys[row_start],
                xs[column_start + 1],
                ys[row_start + 1],
            );
            let mut best =
                cell_is_bounded(rules, base_bbox)?.then_some((1, row_start + 1, column_start + 1));
            if best.is_none() {
                for row_end in row_start + 1..=row_count {
                    for column_end in column_start + 1..=column_count {
                        span_candidates += 1;
                        if span_candidates > MAX_TABLE_SPAN_CANDIDATES {
                            return Ok(None);
                        }
                        let overlaps = (row_start..row_end).any(|row| {
                            (column_start..column_end)
                                .any(|column| covered[row * column_count + column])
                        });
                        if overlaps {
                            continue;
                        }
                        let bbox = (xs[column_start], ys[row_start], xs[column_end], ys[row_end]);
                        if !cell_is_bounded(rules, bbox)?
                            || cell_has_internal_split(
                                rules,
                                xs,
                                ys,
                                row_start,
                                row_end,
                                column_start,
                                column_end,
                            )?
                        {
                            continue;
                        }
                        let area = (row_end - row_start) * (column_end - column_start);
                        if best.is_none_or(|(best_area, _, _)| area < best_area) {
                            best = Some((area, row_end, column_end));
                        }
                    }
                }
            }

            let Some((_, row_end, column_end)) = best else {
                return Ok(None);
            };
            let bbox = (xs[column_start], ys[row_start], xs[column_end], ys[row_end]);
            cells[slot] = Some((bbox, cell_text(word_lines, bbox)?));
            let Ok(anchor) = u32::try_from(slot) else {
                return Ok(None);
            };
            for row in row_start..row_end {
                for column in column_start..column_end {
                    let covered_slot = row * column_count + column;
                    covered[covered_slot] = true;
                    cell_anchors[covered_slot] = anchor;
                }
            }
        }
    }
    Ok(covered
        .into_iter()
        .all(|slot| slot)
        .then_some((cells, cell_anchors)))
}

/// Return the interval containing one coordinate within snapped grid bounds.
fn grid_interval(bounds: &[f64], coordinate: f64) -> Option<usize> {
    bounds.windows(2).position(|pair| {
        coordinate >= pair[0] - TABLE_SNAP_TOLERANCE && coordinate <= pair[1] + TABLE_SNAP_TOLERANCE
    })
}

/// Return the cross-axis grid slots occupied by one physical text line.
fn line_grid_slots(
    line: &[GlyphRecord],
    bounds: &[f64],
    horizontal: bool,
) -> Result<Vec<bool>, Error> {
    let mut occupied = try_filled_text(
        bounds.len().saturating_sub(1),
        false,
        "hybrid-grid slot signature",
    )?;
    for ((x0, y0, x1, y1), _) in split_word_slices(line)? {
        let coordinate = if horizontal {
            (x0 + x1) * 0.5
        } else {
            (y0 + y1) * 0.5
        };
        if let Some(index) = grid_interval(bounds, coordinate) {
            occupied[index] = true;
        }
    }
    Ok(occupied)
}

/// Compare two slot signatures without rewarding jointly empty columns.
fn grid_slot_similarity(left: &[bool], right: &[bool]) -> f64 {
    let intersection = left
        .iter()
        .zip(right)
        .filter(|(left, right)| **left && **right)
        .count();
    let union = left
        .iter()
        .zip(right)
        .filter(|(left, right)| **left || **right)
        .count();
    if union == 0 {
        0.0
    } else {
        intersection as f64 / union as f64
    }
}

/// Infer missing row rules from three or more dense, evenly led text lines.
///
/// Some generators draw complete column borders but only one horizontal rule
/// every few data rows. Treating those gaps as row spans collapses several
/// records into one cell. Dense alignment across at least half the opposite
/// grid axis is strong enough to add synthetic dividers while rejecting
/// ordinary multiline headers and prose inside a wide merged cell.
fn infer_hybrid_grid_rules(
    physical_lines: &[Vec<GlyphRecord>],
    axis_bounds: &[f64],
    cross_bounds: &[f64],
    horizontal: bool,
) -> Result<Vec<RuleSegment>, Error> {
    let cross_count = cross_bounds.len().saturating_sub(1);
    if axis_bounds.len() < 2 || cross_count < 2 {
        return Ok(Vec::new());
    }
    let minimum_occupancy =
        ((cross_count as f64 * HYBRID_GRID_OCCUPANCY_RATIO).ceil() as usize).max(2);
    let mut inferred = Vec::new();
    for interval in axis_bounds.windows(2) {
        let mut candidates = Vec::new();
        for line in physical_lines {
            let Some(glyph) = line.first() else {
                continue;
            };
            let line_is_horizontal = glyph.direction.0.abs() >= glyph.direction.1.abs();
            if line_is_horizontal != horizontal {
                continue;
            }
            let (x0, y0, x1, y1) = line_bbox(line);
            let (start, end) = if horizontal { (y0, y1) } else { (x0, x1) };
            let center = (start + end) * 0.5;
            if center < interval[0] - TABLE_SNAP_TOLERANCE
                || center > interval[1] + TABLE_SNAP_TOLERANCE
            {
                continue;
            }
            let slots = line_grid_slots(line, cross_bounds, horizontal)?;
            if slots.iter().filter(|occupied| **occupied).count() < minimum_occupancy {
                continue;
            }
            let size = line.iter().map(|glyph| glyph.size).fold(0.0, f64::max);
            try_push_text(
                &mut candidates,
                (center, size, slots),
                "hybrid-grid row candidates",
            )?;
        }
        if candidates.len() < MIN_HYBRID_GRID_ROWS {
            continue;
        }
        candidates.sort_unstable_by(|left, right| left.0.total_cmp(&right.0));
        if !candidates
            .windows(2)
            .all(|pair| grid_slot_similarity(&pair[0].2, &pair[1].2) >= HYBRID_GRID_SLOT_SIMILARITY)
        {
            continue;
        }
        let mut minimum_gap = f64::INFINITY;
        let mut maximum_gap = f64::NEG_INFINITY;
        for pair in candidates.windows(2) {
            let gap = pair[1].0 - pair[0].0;
            minimum_gap = minimum_gap.min(gap);
            maximum_gap = maximum_gap.max(gap);
        }
        let maximum_size = candidates
            .iter()
            .map(|candidate| candidate.1)
            .reduce(f64::max)
            .unwrap_or(1.0)
            .max(1.0);
        if minimum_gap <= 0.0
            || maximum_gap > maximum_size * TEXT_TABLE_ROW_GAP
            || maximum_gap - minimum_gap > maximum_size * HYBRID_GRID_LEADING_VARIATION
        {
            continue;
        }
        for pair in candidates.windows(2) {
            let coordinate = (pair[0].0 + pair[1].0) * 0.5;
            let rule = if horizontal {
                RuleSegment {
                    x0: cross_bounds[0],
                    y0: coordinate,
                    x1: *cross_bounds
                        .last()
                        .expect("two cross-axis bounds were checked above"),
                    y1: coordinate,
                    horizontal: true,
                }
            } else {
                RuleSegment {
                    x0: coordinate,
                    y0: cross_bounds[0],
                    x1: coordinate,
                    y1: *cross_bounds
                        .last()
                        .expect("two cross-axis bounds were checked above"),
                    horizontal: false,
                }
            };
            try_push_text(&mut inferred, rule, "hybrid-grid inferred rules")?;
        }
    }
    Ok(inferred)
}

/// Extract physical-order text whose word centers fall inside a cell.
fn cell_text(lines: &[Vec<BorrowedWord<'_>>], (x0, y0, x1, y1): BBox) -> Result<String, Error> {
    let mut text = String::new();
    let mut has_row = false;
    for line in lines {
        let mut has_word = false;
        let inside_cell = |&&((word_x0, word_y0, word_x1, word_y1), _): &&BorrowedWord<'_>| {
            let center_x = (word_x0 + word_x1) * 0.5;
            let center_y = (word_y0 + word_y1) * 0.5;
            center_x >= x0 - TABLE_SNAP_TOLERANCE
                && center_x <= x1 + TABLE_SNAP_TOLERANCE
                && center_y >= y0 - TABLE_SNAP_TOLERANCE
                && center_y <= y1 + TABLE_SNAP_TOLERANCE
        };
        let logical_rtl = glyphs_rtl_order(
            line.iter()
                .filter(inside_cell)
                .flat_map(|(_, glyphs)| glyphs.iter()),
        ) != RtlLineOrder::None;
        let mut append_word =
            |&((word_x0, word_y0, word_x1, word_y1), glyphs): &BorrowedWord<'_>| {
                let center_x = (word_x0 + word_x1) * 0.5;
                let center_y = (word_y0 + word_y1) * 0.5;
                if center_x < x0 - TABLE_SNAP_TOLERANCE
                    || center_x > x1 + TABLE_SNAP_TOLERANCE
                    || center_y < y0 - TABLE_SNAP_TOLERANCE
                    || center_y > y1 + TABLE_SNAP_TOLERANCE
                {
                    return Ok(());
                }
                if !has_word && has_row {
                    try_push_str_text(&mut text, "\n", "bordered table cell text")?;
                } else if has_word {
                    try_push_str_text(&mut text, " ", "bordered table cell text")?;
                }
                try_push_logical_glyphs(&mut text, glyphs, "bordered table cell text")?;
                has_word = true;
                has_row = true;
                Ok::<(), Error>(())
            };
        if logical_rtl {
            for word in line.iter().rev() {
                append_word(word)?;
            }
        } else {
            for word in line {
                append_word(word)?;
            }
        }
    }
    Ok(text)
}

/// Return one cell's text from an already-separated line segment.
fn text_segment_value(segment: &[GlyphRecord]) -> Result<String, Error> {
    let words = split_word_slices(segment)?;
    let mut text = String::new();
    let logical_rtl = glyphs_rtl_order(segment) != RtlLineOrder::None;
    let mut append_word = |glyphs: &[GlyphRecord]| {
        if !text.is_empty() {
            try_push_str_text(&mut text, " ", "borderless table cell text")?;
        }
        try_push_logical_glyphs(&mut text, glyphs, "borderless table cell text")
    };
    if logical_rtl {
        for (_, glyphs) in words.iter().rev() {
            append_word(glyphs)?;
        }
    } else {
        for (_, glyphs) in &words {
            append_word(glyphs)?;
        }
    }
    Ok(text)
}

/// Bounding box around all segments in one inferred text-table row.
fn segmented_row_bbox(row: &[&[GlyphRecord]]) -> BBox {
    let mut x0 = f64::INFINITY;
    let mut y0 = f64::INFINITY;
    let mut x1 = f64::NEG_INFINITY;
    let mut y1 = f64::NEG_INFINITY;
    for segment in row {
        let (segment_x0, segment_y0, segment_x1, segment_y1) = line_bbox(segment);
        x0 = x0.min(segment_x0);
        y0 = y0.min(segment_y0);
        x1 = x1.max(segment_x1);
        y1 = y1.max(segment_y1);
    }
    (x0, y0, x1, y1)
}

/// Representative font size for one inferred text-table row.
fn segmented_row_size(row: &[&[GlyphRecord]]) -> f64 {
    row.iter()
        .flat_map(|segment| segment.iter())
        .map(|glyph| glyph.size)
        .fold(0.0, f64::max)
}

/// Return whether two physical rows can belong to one borderless table.
fn text_rows_compatible(
    first: &[&[GlyphRecord]],
    previous: &[&[GlyphRecord]],
    current: &[&[GlyphRecord]],
) -> bool {
    if first.len() != current.len() || previous.len() != current.len() {
        return false;
    }
    let scale = segmented_row_size(previous)
        .max(segmented_row_size(current))
        .max(1.0);
    let (_, _, _, previous_y1) = segmented_row_bbox(previous);
    let (_, current_y0, _, _) = segmented_row_bbox(current);
    let row_gap = current_y0 - previous_y1;
    if row_gap < -scale * LINE_TOLERANCE || row_gap > scale * TEXT_TABLE_ROW_GAP {
        return false;
    }

    first.iter().zip(current).all(|(anchor, candidate)| {
        let (anchor_x0, _, anchor_x1, _) = line_bbox(anchor);
        let (candidate_x0, _, candidate_x1, _) = line_bbox(candidate);
        (anchor_x0 - candidate_x0).abs() <= scale * TEXT_TABLE_ALIGNMENT_TOLERANCE
            || (anchor_x1 - candidate_x1).abs() <= scale * TEXT_TABLE_ALIGNMENT_TOLERANCE
    })
}

/// Summarize the geometric evidence behind one borderless-text table.
///
/// Confidence is a deterministic ranking heuristic, not a calibrated
/// probability. The component metrics stay public so callers can apply their
/// own thresholds.
fn text_table_diagnostics(rows: &[Vec<&[GlyphRecord]>]) -> TableDiagnosticsTuple {
    let anchor = &rows[0];
    let mut alignment_error_em = 0.0_f64;
    for row in rows.iter().skip(1) {
        let scale = segmented_row_size(anchor)
            .max(segmented_row_size(row))
            .max(1.0);
        for (anchor_segment, candidate_segment) in anchor.iter().zip(row) {
            let (anchor_x0, _, anchor_x1, _) = line_bbox(anchor_segment);
            let (candidate_x0, _, candidate_x1, _) = line_bbox(candidate_segment);
            let error = (anchor_x0 - candidate_x0)
                .abs()
                .min((anchor_x1 - candidate_x1).abs())
                / scale;
            alignment_error_em = alignment_error_em.max(error);
        }
    }

    let minimum_gutter_em = rows
        .iter()
        .flat_map(|row| {
            let scale = segmented_row_size(row).max(1.0);
            row.windows(2).map(move |pair| {
                let (_, _, left_x1, _) = line_bbox(pair[0]);
                let (right_x0, _, _, _) = line_bbox(pair[1]);
                (right_x0 - left_x1) / scale
            })
        })
        .reduce(f64::min)
        .unwrap_or(0.0);

    let mut minimum_row_gap = f64::INFINITY;
    let mut maximum_row_gap = f64::NEG_INFINITY;
    for pair in rows.windows(2) {
        let scale = segmented_row_size(&pair[0])
            .max(segmented_row_size(&pair[1]))
            .max(1.0);
        let gap = (segmented_row_bbox(&pair[1]).1 - segmented_row_bbox(&pair[0]).3) / scale;
        minimum_row_gap = minimum_row_gap.min(gap);
        maximum_row_gap = maximum_row_gap.max(gap);
    }
    if !minimum_row_gap.is_finite() {
        minimum_row_gap = 0.0;
        maximum_row_gap = 0.0;
    }
    let row_gap_variation_em = maximum_row_gap - minimum_row_gap;

    let row_depth = (rows.len().saturating_sub(MIN_TEXT_TABLE_ROWS) as f64 + 1.0) / 3.0;
    let alignment_quality =
        (1.0 - alignment_error_em / TEXT_TABLE_ALIGNMENT_TOLERANCE).clamp(0.0, 1.0);
    let spacing_quality = (1.0 - row_gap_variation_em / TEXT_TABLE_ROW_GAP).clamp(0.0, 1.0);
    let gutter_quality = (minimum_gutter_em / (LINE_SEGMENT_GAP * 2.0)).clamp(0.0, 1.0);
    let confidence = (0.65
        + 0.10 * row_depth.clamp(0.0, 1.0)
        + 0.10 * alignment_quality
        + 0.10 * spacing_quality
        + 0.05 * gutter_quality)
        .clamp(0.0, 1.0);
    (
        confidence,
        Some(alignment_error_em),
        Some(minimum_gutter_em),
        Some(row_gap_variation_em),
    )
}

/// Materialize one run of aligned borderless rows.
fn text_table_from_rows(rows: &[Vec<&[GlyphRecord]>]) -> Result<Option<TableTuple>, Error> {
    if rows.len() < MIN_TEXT_TABLE_ROWS {
        return Ok(None);
    }
    let row_count_usize = rows.len();
    let column_count_usize = rows[0].len();
    if !(2..=MAX_TEXT_TABLE_COLUMNS).contains(&column_count_usize) {
        return Ok(None);
    }
    let Some(slot_count) = row_count_usize.checked_mul(column_count_usize) else {
        return Ok(None);
    };
    if slot_count > MAX_TABLE_CELLS {
        return Ok(None);
    }

    let Some(first_x) = rows.iter().map(|row| line_bbox(row[0]).0).reduce(f64::min) else {
        return Ok(None);
    };
    let mut x_bounds = Vec::new();
    try_push_text(&mut x_bounds, first_x, "borderless table x bounds")?;
    for column in 1..column_count_usize {
        let Some(left_end) = rows
            .iter()
            .map(|row| line_bbox(row[column - 1]).2)
            .reduce(f64::max)
        else {
            return Ok(None);
        };
        let Some(right_start) = rows
            .iter()
            .map(|row| line_bbox(row[column]).0)
            .reduce(f64::min)
        else {
            return Ok(None);
        };
        if right_start <= left_end {
            return Ok(None);
        }
        try_push_text(
            &mut x_bounds,
            (left_end + right_start) * 0.5,
            "borderless table x bounds",
        )?;
    }
    let Some(last_x) = rows
        .iter()
        .map(|row| line_bbox(row[column_count_usize - 1]).2)
        .reduce(f64::max)
    else {
        return Ok(None);
    };
    try_push_text(&mut x_bounds, last_x, "borderless table x bounds")?;

    let mut row_bboxes = Vec::new();
    for row in rows {
        try_push_text(
            &mut row_bboxes,
            segmented_row_bbox(row),
            "borderless table row bounds",
        )?;
    }
    let mut y_bounds = Vec::new();
    try_push_text(&mut y_bounds, row_bboxes[0].1, "borderless table y bounds")?;
    for pair in row_bboxes.windows(2) {
        try_push_text(
            &mut y_bounds,
            (pair[0].3 + pair[1].1) * 0.5,
            "borderless table y bounds",
        )?;
    }
    try_push_text(
        &mut y_bounds,
        row_bboxes[row_count_usize - 1].3,
        "borderless table y bounds",
    )?;

    let mut cells = Vec::new();
    for (row_index, row) in rows.iter().enumerate() {
        for (column_index, segment) in row.iter().enumerate() {
            let cell = Some((
                (
                    x_bounds[column_index],
                    y_bounds[row_index],
                    x_bounds[column_index + 1],
                    y_bounds[row_index + 1],
                ),
                text_segment_value(segment)?,
            ));
            try_push_text(&mut cells, cell, "borderless table cells")?;
        }
    }
    let Ok(row_count) = u32::try_from(row_count_usize) else {
        return Ok(None);
    };
    let Ok(column_count) = u32::try_from(column_count_usize) else {
        return Ok(None);
    };
    let diagnostics = text_table_diagnostics(rows);
    let Ok(slot_count) = u32::try_from(slot_count) else {
        return Ok(None);
    };
    let mut cell_anchors = Vec::new();
    for anchor in 0..slot_count {
        try_push_text(&mut cell_anchors, anchor, "borderless table cell anchors")?;
    }
    Ok(Some((
        (
            x_bounds[0],
            y_bounds[0],
            x_bounds[column_count_usize],
            y_bounds[row_count_usize],
        ),
        row_count,
        column_count,
        cells,
        cell_anchors,
        diagnostics,
    )))
}

fn flush_text_table_run(
    run: &mut Vec<Vec<&[GlyphRecord]>>,
    tables: &mut Vec<TableTuple>,
) -> Result<(), Error> {
    if let Some(table) = text_table_from_rows(run)? {
        try_push_text(tables, table, "borderless tables")?;
    }
    run.clear();
    Ok(())
}

/// Detect opt-in borderless tables from sustained aligned text segments.
fn detect_text_tables(physical_lines: &[Vec<GlyphRecord>]) -> Result<Vec<TableTuple>, Error> {
    let mut tables = Vec::new();
    let mut run = Vec::new();

    for line in physical_lines {
        let segment_count = line_segment_count(line);
        let segments = if !line.is_empty()
            && !has_vertical_baseline(&line[0])
            && (2..=MAX_TEXT_TABLE_COLUMNS).contains(&segment_count)
        {
            let segments = split_line_segment_slices(line)?;
            (segments.len() == segment_count).then_some(segments)
        } else {
            None
        };
        let Some(segments) = segments else {
            flush_text_table_run(&mut run, &mut tables)?;
            continue;
        };
        if run.is_empty()
            || text_rows_compatible(
                run.first().expect("the run was checked as non-empty"),
                run.last().expect("the run was checked as non-empty"),
                &segments,
            )
        {
            try_push_text(&mut run, segments, "borderless table row run")?;
        } else {
            flush_text_table_run(&mut run, &mut tables)?;
            try_push_text(&mut run, segments, "borderless table row run")?;
        }
    }
    flush_text_table_run(&mut run, &mut tables)?;
    Ok(tables)
}

fn rule_coordinates(rules: &[RuleSegment], horizontal: bool) -> Result<Vec<f64>, Error> {
    let mut coordinates = Vec::new();
    for rule in rules {
        if rule.horizontal == horizontal {
            try_push_text(
                &mut coordinates,
                if horizontal { rule.y0 } else { rule.x0 },
                "table rule coordinates",
            )?;
        }
    }
    Ok(coordinates)
}

/// Detect high-confidence rectangular tables from connected vector-rule grids.
fn detect_grid_tables(
    physical_lines: &[Vec<GlyphRecord>],
    rules: &[RuleSegment],
) -> Result<Vec<TableTuple>, Error> {
    let mut word_lines = Vec::new();
    for line in physical_lines {
        try_push_text(
            &mut word_lines,
            split_word_slices(line)?,
            "bordered table word lines",
        )?;
    }
    let mut horizontal_indices = Vec::new();
    let mut vertical_indices = Vec::new();
    for (index, rule) in rules.iter().enumerate() {
        try_push_text(
            if rule.horizontal {
                &mut horizontal_indices
            } else {
                &mut vertical_indices
            },
            index,
            "table rule orientation indices",
        )?;
    }
    let mut components = RuleComponents::new(rules.len())?;
    for &horizontal_index in &horizontal_indices {
        for &vertical_index in &vertical_indices {
            if rules_intersect(rules[horizontal_index], rules[vertical_index]) {
                components.union(horizontal_index, vertical_index);
            }
        }
    }

    let mut grouped_rules = Vec::new();
    for (index, &rule) in rules.iter().enumerate() {
        let root = components.find(index);
        try_push_text(&mut grouped_rules, (root, rule), "connected table rules")?;
    }
    grouped_rules.sort_unstable_by_key(|(root, _)| *root);

    let mut tables = Vec::new();
    let mut group_start = 0;
    while group_start < grouped_rules.len() {
        let root = grouped_rules[group_start].0;
        let mut group_end = group_start + 1;
        while group_end < grouped_rules.len() && grouped_rules[group_end].0 == root {
            group_end += 1;
        }
        let mut component_rules = Vec::new();
        for &(_, rule) in &grouped_rules[group_start..group_end] {
            try_push_text(&mut component_rules, rule, "table component rules")?;
        }
        group_start = group_end;

        let original_xs = clustered_coordinates(rule_coordinates(&component_rules, false)?);
        let original_ys = clustered_coordinates(rule_coordinates(&component_rules, true)?);
        if original_xs.len() < 3 || original_ys.len() < 3 {
            continue;
        }
        let mut inferred_rules =
            infer_hybrid_grid_rules(physical_lines, &original_ys, &original_xs, true)?;
        for rule in infer_hybrid_grid_rules(physical_lines, &original_xs, &original_ys, false)? {
            try_push_text(&mut inferred_rules, rule, "hybrid-grid inferred rules")?;
        }
        let is_hybrid = !inferred_rules.is_empty();
        for rule in inferred_rules {
            try_push_text(&mut component_rules, rule, "table component rules")?;
        }
        let xs = clustered_coordinates(rule_coordinates(&component_rules, false)?);
        let ys = clustered_coordinates(rule_coordinates(&component_rules, true)?);
        let row_count_usize = ys.len() - 1;
        let column_count_usize = xs.len() - 1;
        let Some((cells, cell_anchors)) =
            materialize_grid_cells(&component_rules, &xs, &ys, &word_lines)?
        else {
            continue;
        };
        let Ok(row_count) = u32::try_from(row_count_usize) else {
            continue;
        };
        let Ok(column_count) = u32::try_from(column_count_usize) else {
            continue;
        };
        try_push_text(
            &mut tables,
            (
                (
                    xs[0],
                    ys[0],
                    *xs.last().expect("three coordinates were checked above"),
                    *ys.last().expect("three coordinates were checked above"),
                ),
                row_count,
                column_count,
                cells,
                cell_anchors,
                (if is_hybrid { 0.95 } else { 1.0 }, None, None, None),
            ),
            "bordered tables",
        )?;
    }
    tables.sort_unstable_by(|left, right| {
        left.0
            .1
            .total_cmp(&right.0.1)
            .then(left.0.0.total_cmp(&right.0.0))
            .then(left.1.cmp(&right.1))
            .then(left.2.cmp(&right.2))
    });
    Ok(tables)
}
