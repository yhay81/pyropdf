//! `Page.tables`: the tables that a page's vector rules bound or its text
//! aligns, ported from pylopdf's `TablePage` and table detection in
//! `rust/src/extract.rs` at 036be111cafe77755db4bb2be17bc70a8f2f9d82.

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};

use super::layout::{RuleSegment, glyphs_bbox};
use super::{
    BBox, Error, GlyphRecord, LINE_SEGMENT_GAP, LINE_TOLERANCE, RtlLineOrder, TextOptions, as_u64,
    cluster_lines, collect_page_glyphs, glyph_end, glyph_is_nonspacing_mark, glyph_progress,
    glyphs_rtl_order, has_vertical_baseline, line_bbox, line_segment_count, needs_gap,
    try_push_glyph_text, try_push_str_text, try_push_text,
};
use crate::bridge::Bridged;
use crate::render::Fallback;

/// Fewest rows that a borderless text table holds.
const MIN_TEXT_TABLE_ROWS: usize = 3;
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
/// Widest double-rule gap, in points, between a bordered header grid and an
/// adjacent outer-bordered body box sharing the same cross-axis span.
const MAX_DOUBLE_RULE_GAP: f64 = 4.0;
/// Minimum whitespace gap, in ems, across a projected header column boundary
/// before splitting a borderless body line into separate column phrases.
const BODY_COLUMN_RULE_GAP: f64 = 0.85;
/// Fewest aligned multi-column records required to treat an outer-bordered
/// body box as a table body rather than prose or a summary note.
const MIN_BORDERLESS_BODY_RECORDS: usize = 2;
/// Minimum Jaccard similarity between adjacent multi-column records' occupied
/// column masks in a borderless body.
const BORDERLESS_BODY_SLOT_SIMILARITY: f64 = 0.6;
/// Widest inter-line edge gap, in ems, for a wrapped continuation line to
/// attach to the preceding record.
const CONTINUATION_MAX_EDGE_GAP: f64 = 1.25;

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
/// while detection runs, and the text of the cells of a ruled table against
/// `text_size` for the rest of the operation. A refusal keeps nothing.
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
    let collected =
        collect_page_glyphs(bridged, &Fallback::default(), index, &options, None, budget)?;
    let charge = collected.charge;
    let detected = cluster_lines(collected.glyphs).and_then(|lines| {
        if text_strategy {
            detect_text_tables(&lines, budget)
        } else {
            detect_grid_tables(&lines, &collected.rules, budget)
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

#[expect(
    clippy::disallowed_methods,
    reason = "every caller sizes a grid whose slots table_slots checks against table_cell_count"
)]
fn try_filled_text<T: Clone>(len: usize, value: T, label: &str) -> Result<Vec<T>, Error> {
    let mut values = Vec::new();
    values
        .try_reserve_exact(len)
        .map_err(|error| Error::Allocation(format!("failed to reserve {label}: {error}")))?;
    values.resize(len, value);
    Ok(values)
}

/// Checks one candidate before allocating its grid or inferring dividers.
fn table_slots(rows: usize, columns: usize, budget: &Budget) -> Result<usize, Error> {
    budget.check(
        LimitCode::TableCellCount,
        as_u64(rows).saturating_mul(as_u64(columns)),
    )?;
    rows.checked_mul(columns)
        .ok_or_else(|| Error::Allocation("table slots exceed the address space".to_owned()))
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
        let mut root = index;
        while self.parent[root] != root {
            root = self.parent[root];
        }
        let mut at = index;
        while self.parent[at] != at {
            let parent = self.parent[at];
            self.parent[at] = root;
            at = parent;
        }
        root
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
///
/// The text of the cells counts against `text_size` for the rest of the
/// operation, as the strings of a layout do, unless the grid is discarded.
fn materialize_grid_cells(
    rules: &[RuleSegment],
    xs: &[f64],
    ys: &[f64],
    word_lines: &[Vec<BorrowedWord<'_>>],
    budget: &Budget,
) -> Result<Option<MaterializedGrid>, Error> {
    let mut charged = 0;
    let grid = tile_grid_cells(rules, xs, ys, word_lines, budget, &mut charged);
    if !matches!(grid, Ok(Some(_))) {
        budget.release(LimitCode::TextSize, as_u64(charged));
    }
    grid
}

/// [`materialize_grid_cells`], adding the `text_size` that the cells' text
/// charges to `charged`.
fn tile_grid_cells(
    rules: &[RuleSegment],
    xs: &[f64],
    ys: &[f64],
    word_lines: &[Vec<BorrowedWord<'_>>],
    budget: &Budget,
    charged: &mut usize,
) -> Result<Option<MaterializedGrid>, Error> {
    let row_count = ys.len() - 1;
    let column_count = xs.len() - 1;
    let slot_count = table_slots(row_count, column_count, budget)?;

    let mut cells = try_filled_text(slot_count, None, "bordered table cells")?;
    let mut cell_anchors = try_filled_text(slot_count, 0, "bordered table cell anchors")?;
    let mut covered = try_filled_text(slot_count, false, "bordered table coverage map")?;
    let mut span_candidates = 0;
    for row_start in 0..row_count {
        budget.poll()?;
        for column_start in 0..column_count {
            budget.poll()?;
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
                        budget.check(LimitCode::TableSpanCandidateCount, span_candidates)?;
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
            cells[slot] = Some((bbox, cell_text(word_lines, bbox, budget, charged)?));
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
fn line_grid_slots<'b>(
    line: &[GlyphRecord],
    bounds: &[f64],
    horizontal: bool,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, bool>, Error> {
    let mut occupied = BudgetVec::new(budget);
    occupied.try_extend(std::iter::repeat_n(false, bounds.len().saturating_sub(1)))?;
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
    budget: &Budget,
) -> Result<Vec<RuleSegment>, Error> {
    let cross_count = cross_bounds.len().saturating_sub(1);
    if axis_bounds.len() < 2 || cross_count < 2 {
        return Ok(Vec::new());
    }
    let minimum_occupancy =
        ((cross_count as f64 * HYBRID_GRID_OCCUPANCY_RATIO).ceil() as usize).max(2);
    let mut inferred = Vec::new();
    for interval in axis_bounds.windows(2) {
        budget.poll()?;
        let mut candidates = BudgetVec::new(budget);
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
            let slots = line_grid_slots(line, cross_bounds, horizontal, budget)?;
            if slots.iter().filter(|occupied| **occupied).count() < minimum_occupancy {
                continue;
            }
            let size = line.iter().map(|glyph| glyph.size).fold(0.0, f64::max);
            candidates.try_push((center, size, slots))?;
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
            budget.check(
                LimitCode::TextRuleCount,
                as_u64(inferred.len()).saturating_add(1),
            )?;
            try_push_text(&mut inferred, rule, "hybrid-grid inferred rules")?;
        }
    }
    Ok(inferred)
}

/// Extract physical-order text whose word centers fall inside a cell, and
/// charge it to `text_size`, adding it to `charged`.
///
/// A cell holds at most the text of the page's words, which `text_size`
/// counts already, but the cells of a grid can hold more than the page:
/// a word near an edge falls in the cells on both sides, and the grids of
/// a page may overlap. The charge bounds what they hold together.
fn cell_text(
    lines: &[Vec<BorrowedWord<'_>>],
    bbox: BBox,
    budget: &Budget,
    charged: &mut usize,
) -> Result<String, Error> {
    let text = physical_cell_text(lines, bbox, budget)?;
    budget.charge(LimitCode::TextSize, as_u64(text.len()))?;
    *charged += text.len();
    Ok(text)
}

/// The text of the words of `lines` whose centers fall inside a cell, in
/// physical order.
fn physical_cell_text(
    lines: &[Vec<BorrowedWord<'_>>],
    (x0, y0, x1, y1): BBox,
    budget: &Budget,
) -> Result<String, Error> {
    let mut text = String::new();
    let mut has_row = false;
    for line in lines {
        budget.poll()?;
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
fn text_table_from_rows(
    rows: &[Vec<&[GlyphRecord]>],
    budget: &Budget,
) -> Result<Option<TableTuple>, Error> {
    if rows.len() < MIN_TEXT_TABLE_ROWS {
        return Ok(None);
    }
    let row_count_usize = rows.len();
    let column_count_usize = rows[0].len();
    if column_count_usize < 2 {
        return Ok(None);
    }
    budget.check(LimitCode::TableColumnCount, as_u64(column_count_usize))?;
    let slot_count = table_slots(row_count_usize, column_count_usize, budget)?;

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
        budget.poll()?;
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
    budget: &Budget,
) -> Result<(), Error> {
    if let Some(table) = text_table_from_rows(run, budget)? {
        try_push_text(tables, table, "borderless tables")?;
    }
    run.clear();
    Ok(())
}

/// Detect opt-in borderless tables from sustained aligned text segments.
fn detect_text_tables(
    physical_lines: &[Vec<GlyphRecord>],
    budget: &Budget,
) -> Result<Vec<TableTuple>, Error> {
    let mut tables = Vec::new();
    let mut run = Vec::new();

    for line in physical_lines {
        budget.poll()?;
        let segment_count = line_segment_count(line);
        let segments = if !line.is_empty() && !has_vertical_baseline(&line[0]) && segment_count >= 2
        {
            budget.check(LimitCode::TableColumnCount, as_u64(segment_count))?;
            let segments = split_line_segment_slices(line)?;
            (segments.len() == segment_count).then_some(segments)
        } else {
            None
        };
        let Some(segments) = segments else {
            flush_text_table_run(&mut run, &mut tables, budget)?;
            continue;
        };
        if run.is_empty()
            || text_rows_compatible(
                run.first().expect("the run was checked as non-empty"),
                run.last().expect("the run was checked as non-empty"),
                &segments,
            )
        {
            table_slots(run.len() + 1, segment_count, budget)?;
            try_push_text(&mut run, segments, "borderless table row run")?;
        } else {
            flush_text_table_run(&mut run, &mut tables, budget)?;
            try_push_text(&mut run, segments, "borderless table row run")?;
        }
    }
    flush_text_table_run(&mut run, &mut tables, budget)?;
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

/// One connected component of vector rules with its clustered axis bounds.
struct RuleComponent {
    rules: Vec<RuleSegment>,
    xs: Vec<f64>,
    ys: Vec<f64>,
    bounded: bool,
}

impl RuleComponent {
    fn axis_bounds(&self, horizontal: bool) -> &[f64] {
        if horizontal { &self.ys } else { &self.xs }
    }

    fn cross_bounds(&self, horizontal: bool) -> &[f64] {
        if horizontal { &self.xs } else { &self.ys }
    }

    fn bbox(&self) -> Option<BBox> {
        let (&x0, &x1) = (self.xs.first()?, self.xs.last()?);
        let (&y0, &y1) = (self.ys.first()?, self.ys.last()?);
        Some((x0, y0, x1, y1))
    }
}

/// Text reading orientation inside a borderless table body in display space.
#[derive(Clone, Copy)]
struct BodyOrientation {
    /// `true` when lines run horizontally (rows stack along `y`); `false` when
    /// lines run vertically (rows stack along `x`).
    horizontal: bool,
    /// `+1.0` along ascending column-axis coordinates, `-1.0` along descending.
    inline_sign: f64,
    /// `+1.0` along ascending row-axis coordinates, `-1.0` along descending.
    block_sign: f64,
}

/// One column-aligned text phrase on a physical body line.
struct BodyPhrase {
    column: usize,
    word_start: usize,
    word_end: usize,
    cross_start: f64,
    cross_end: f64,
}

/// One physical text line parsed against projected header column boundaries.
struct ParsedBodyLine<'b> {
    line_index: usize,
    axis_start: f64,
    axis_end: f64,
    axis_center: f64,
    font_size: f64,
    phrases: BudgetVec<'b, BodyPhrase>,
    slots: BudgetVec<'b, bool>,
    occupied_cols: usize,
}

impl ParsedBodyLine<'_> {
    /// Cross-axis extent of all phrases mapped to `column` on this line.
    fn column_span(&self, column: usize) -> Option<(f64, f64)> {
        let mut start = f64::INFINITY;
        let mut end = f64::NEG_INFINITY;
        for phrase in self.phrases.iter() {
            if phrase.column == column {
                start = start.min(phrase.cross_start);
                end = end.max(phrase.cross_end);
            }
        }
        (start <= end).then_some((start, end))
    }

    /// Logical-first occupied column index (the leading key column).
    fn first_occupied_column(&self, inline_sign: f64) -> Option<usize> {
        if inline_sign >= 0.0 {
            self.slots.iter().position(|&occupied| occupied)
        } else {
            self.slots.iter().rposition(|&occupied| occupied)
        }
    }
}

/// One logical data record (its start line plus any wrapped continuation lines).
struct BodyRecord<'b> {
    lines: BudgetVec<'b, ParsedBodyLine<'b>>,
    display_axis_start: f64,
    display_axis_end: f64,
}

/// One row-axis band of a borderless body with its grouped data records.
struct BodyBandPlan<'b> {
    axis_start: f64,
    axis_end: f64,
    /// Records in ascending display row-axis order.
    records: BudgetVec<'b, BodyRecord<'b>>,
}

/// Validated plan pairing a bordered header grid with a borderless-column body.
struct AttachedBodyPlan<'b> {
    orientation: BodyOrientation,
    header_xs: Vec<f64>,
    header_ys: Vec<f64>,
    body_component_indices: BudgetVec<'b, usize>,
    /// Body bands in ascending display row-axis order.
    bands: BudgetVec<'b, BodyBandPlan<'b>>,
    total_body_records: usize,
    total_rule_count: usize,
}

/// Return the 1D span of `bbox` along the row axis (`y` if horizontal, `x` if
/// vertical) or cross/column axis (`x` if horizontal, `y` if vertical).
fn bbox_axis_span((x0, y0, x1, y1): BBox, horizontal: bool) -> (f64, f64) {
    if horizontal { (y0, y1) } else { (x0, x1) }
}

fn bbox_cross_span((x0, y0, x1, y1): BBox, horizontal: bool) -> (f64, f64) {
    if horizontal { (x0, x1) } else { (y0, y1) }
}

/// Determine the dominant text orientation inside one display-space box along
/// `horizontal`, returning `None` if no matching lines lie inside.
fn body_text_orientation(
    physical_lines: &[Vec<GlyphRecord>],
    bbox: BBox,
    horizontal: bool,
) -> Option<BodyOrientation> {
    let (axis_lo, axis_hi) = bbox_axis_span(bbox, horizontal);
    let (cross_lo, cross_hi) = bbox_cross_span(bbox, horizontal);
    let mut positive = 0usize;
    let mut negative = 0usize;
    for line in physical_lines {
        let Some(first) = line.first() else {
            continue;
        };
        let line_is_horizontal = first.direction.0.abs() >= first.direction.1.abs();
        if line_is_horizontal != horizontal {
            continue;
        }
        let line_box = line_bbox(line);
        let (line_a0, line_a1) = bbox_axis_span(line_box, horizontal);
        let (line_c0, line_c1) = bbox_cross_span(line_box, horizontal);
        let axis_center = (line_a0 + line_a1) * 0.5;
        let cross_center = (line_c0 + line_c1) * 0.5;
        if axis_center < axis_lo - TABLE_SNAP_TOLERANCE
            || axis_center > axis_hi + TABLE_SNAP_TOLERANCE
            || cross_center < cross_lo - TABLE_SNAP_TOLERANCE
            || cross_center > cross_hi + TABLE_SNAP_TOLERANCE
        {
            continue;
        }
        let inline_component = if horizontal {
            first.direction.0
        } else {
            first.direction.1
        };
        if inline_component >= 0.0 {
            positive += 1;
        } else {
            negative += 1;
        }
    }
    if positive == 0 && negative == 0 {
        return None;
    }
    let inline_sign = if positive >= negative { 1.0 } else { -1.0 };
    // Block progression vector is `(-dy, dx)`:
    // horizontal `(s, 0)` -> `(0, s)` (+y for 0 deg, -y for 180 deg);
    // vertical `(0, s)` -> `(-s, 0)` (-x for 90 deg, +x for 270 deg).
    let block_sign = if horizontal {
        inline_sign
    } else {
        -inline_sign
    };
    Some(BodyOrientation {
        horizontal,
        inline_sign,
        block_sign,
    })
}

/// Return the column index owning `words[word_start..word_end]` within
/// `col_bounds`, using the logical-first non-tiny word so overflowing wrapped
/// descriptions stay in their starting column while slight edge overhangs snap
/// cleanly.
fn phrase_column(
    words: &[BorrowedWord<'_>],
    word_start: usize,
    word_end: usize,
    col_bounds: &[f64],
    horizontal: bool,
    is_rtl: bool,
) -> Option<usize> {
    let col_count = col_bounds.len().saturating_sub(1);
    if col_count == 0 || word_start >= word_end {
        return None;
    }
    let anchor_span = |range: std::ops::Range<usize>| -> (f64, f64) {
        let mut lo = f64::INFINITY;
        let mut hi = f64::NEG_INFINITY;
        for &(word_box, _) in &words[range] {
            let (s, e) = bbox_cross_span(word_box, horizontal);
            lo = lo.min(s);
            hi = hi.max(e);
        }
        (lo, hi)
    };
    let best_overlap_col = |lo: f64, hi: f64| -> Option<usize> {
        let mut best: Option<(usize, f64)> = None;
        for col in 0..col_count {
            let overlap = (hi.min(col_bounds[col + 1]) - lo.max(col_bounds[col])).max(0.0);
            if overlap > 0.0 && best.is_none_or(|(_, best_overlap)| overlap > best_overlap) {
                best = Some((col, overlap));
            }
        }
        if let Some((col, _)) = best {
            return Some(col);
        }
        grid_interval(col_bounds, (lo + hi) * 0.5)
    };

    // Logical-first word: `words` is in `glyph_progress` order, so index
    // `word_start` is logical-first for LTR and `word_end - 1` for pure RTL.
    let min_anchor_width = TABLE_SNAP_TOLERANCE * 2.0;
    let anchor_index = if is_rtl {
        (word_start..word_end).rev().find(|&idx| {
            let (s, e) = bbox_cross_span(words[idx].0, horizontal);
            e - s > min_anchor_width
        })
    } else {
        (word_start..word_end).find(|&idx| {
            let (s, e) = bbox_cross_span(words[idx].0, horizontal);
            e - s > min_anchor_width
        })
    };
    let (lo, hi) = if let Some(idx) = anchor_index {
        bbox_cross_span(words[idx].0, horizontal)
    } else {
        anchor_span(word_start..word_end)
    };
    best_overlap_col(lo, hi)
}

/// Parse one physical text line inside a borderless body band into column
/// phrases aligned against `col_bounds`.
fn parse_body_line<'b>(
    line_index: usize,
    line: &[GlyphRecord],
    words: &[BorrowedWord<'_>],
    col_bounds: &[f64],
    orientation: BodyOrientation,
    budget: &'b Budget,
) -> Result<Option<ParsedBodyLine<'b>>, Error> {
    let col_count = col_bounds.len().saturating_sub(1);
    if col_count < 2 || line.is_empty() || words.is_empty() {
        return Ok(None);
    }
    let cross_min = col_bounds[0] - TABLE_SNAP_TOLERANCE;
    let cross_max = col_bounds[col_count] + TABLE_SNAP_TOLERANCE;

    // Collect indices into `words` whose center lies inside the table's
    // cross-axis span. `words` is already in `glyph_progress` order.
    let mut kept_indices: BudgetVec<'b, usize> = BudgetVec::new(budget);
    for (idx, &(word_box, _)) in words.iter().enumerate() {
        let (w0, w1) = bbox_cross_span(word_box, orientation.horizontal);
        let center = (w0 + w1) * 0.5;
        if center >= cross_min && center <= cross_max {
            kept_indices.try_push(idx)?;
        }
    }
    if kept_indices.is_empty() {
        return Ok(None);
    }

    // Check that kept words form a contiguous slice or filter them cleanly.
    // To stay borrow-only without copying words, split runs along `kept_indices`.
    let word_inline_span = |word_box: BBox| -> (f64, f64) {
        let (s, e) = bbox_cross_span(word_box, orientation.horizontal);
        if orientation.inline_sign >= 0.0 {
            (s, e)
        } else {
            (-e, -s)
        }
    };
    let word_size =
        |glyphs: &[GlyphRecord]| -> f64 { glyphs.iter().map(|g| g.size).fold(0.0_f64, f64::max) };

    let is_rtl = is_pure_rtl_line(line);
    let mut phrases: BudgetVec<'b, BodyPhrase> = BudgetVec::new(budget);
    let mut slots: BudgetVec<'b, bool> = BudgetVec::new(budget);
    slots.try_extend(std::iter::repeat_n(false, col_count))?;

    let mut run_start = 0usize;
    while run_start < kept_indices.len() {
        let mut run_end = run_start + 1;
        while run_end < kept_indices.len() {
            let prev_idx = kept_indices[run_end - 1];
            let next_idx = kept_indices[run_end];
            // Non-contiguous word index means an excluded outside word sat between them.
            if next_idx != prev_idx + 1 {
                break;
            }
            let (prev_box, prev_glyphs) = words[prev_idx];
            let (next_box, next_glyphs) = words[next_idx];
            let (_, prev_inline_end) = word_inline_span(prev_box);
            let (next_inline_start, _) = word_inline_span(next_box);
            let gap = next_inline_start - prev_inline_end;
            let scale = word_size(prev_glyphs).max(word_size(next_glyphs)).max(1.0);
            let wide_gutter = gap > scale * LINE_SEGMENT_GAP;
            let crosses_divider = col_bounds[1..col_count].iter().any(|&divider| {
                let d = divider * orientation.inline_sign;
                prev_inline_end <= d + TABLE_SNAP_TOLERANCE
                    && next_inline_start >= d - TABLE_SNAP_TOLERANCE
            });
            if wide_gutter || (crosses_divider && gap > scale * BODY_COLUMN_RULE_GAP) {
                break;
            }
            run_end += 1;
        }
        let w_start = kept_indices[run_start];
        let w_end = kept_indices[run_end - 1] + 1;
        if let Some(column) = phrase_column(
            words,
            w_start,
            w_end,
            col_bounds,
            orientation.horizontal,
            is_rtl,
        ) {
            let mut cross_start = f64::INFINITY;
            let mut cross_end = f64::NEG_INFINITY;
            for &(word_box, _) in &words[w_start..w_end] {
                let (s, e) = bbox_cross_span(word_box, orientation.horizontal);
                cross_start = cross_start.min(s);
                cross_end = cross_end.max(e);
            }
            slots[column] = true;
            phrases.try_push(BodyPhrase {
                column,
                word_start: w_start,
                word_end: w_end,
                cross_start,
                cross_end,
            })?;
        }
        run_start = run_end;
    }

    let occupied_cols = slots.iter().filter(|&&occupied| occupied).count();
    if occupied_cols == 0 {
        return Ok(None);
    }
    let mut axis_start = f64::INFINITY;
    let mut axis_end = f64::NEG_INFINITY;
    let mut font_size = 0.0_f64;
    for &idx in kept_indices.iter() {
        let (word_box, glyphs) = words[idx];
        let (a0, a1) = bbox_axis_span(word_box, orientation.horizontal);
        axis_start = axis_start.min(a0);
        axis_end = axis_end.max(a1);
        font_size = font_size.max(word_size(glyphs));
    }
    Ok(Some(ParsedBodyLine {
        line_index,
        axis_start,
        axis_end,
        axis_center: (axis_start + axis_end) * 0.5,
        font_size: font_size.max(1.0),
        phrases,
        slots,
        occupied_cols,
    }))
}

/// Return whether `curr` is a wrapped continuation of `record` rather than the
/// start of a new data record.
fn is_wrapped_continuation(
    record: &BodyRecord<'_>,
    curr: &ParsedBodyLine<'_>,
    next: Option<&ParsedBodyLine<'_>>,
    orientation: BodyOrientation,
) -> bool {
    let start = &record.lines[0];
    let prev = &record.lines[record.lines.len() - 1];
    if start.occupied_cols < 2 || curr.occupied_cols >= start.occupied_cols {
        return false;
    }
    // Every occupied column on a continuation line must already belong to the
    // record's start line.
    if !curr
        .slots
        .iter()
        .zip(start.slots.iter())
        .all(|(&c, &s)| !c || s)
    {
        return false;
    }
    // Wrapped lines follow tightly in the block reading direction.
    let scale = prev.font_size.max(curr.font_size).max(1.0);
    let center_step = (curr.axis_center - prev.axis_center) * orientation.block_sign;
    let edge_gap = if orientation.block_sign >= 0.0 {
        curr.axis_start - prev.axis_end
    } else {
        prev.axis_start - curr.axis_end
    };
    if center_step <= 0.0
        || center_step > scale * TEXT_TABLE_ROW_GAP
        || edge_gap > scale * CONTINUATION_MAX_EDGE_GAP
    {
        return false;
    }
    // A continuation phrase must not physically intrude into the cross-axis
    // span of another occupied column of the parent record (guards against
    // full-width prose paragraphs beneath a multi-column heading line).
    for phrase in curr.phrases.iter() {
        for (other_col, &occupied) in start.slots.iter().enumerate() {
            if !occupied || other_col == phrase.column {
                continue;
            }
            if let Some((other_start, other_end)) = start.column_span(other_col) {
                let overlap = phrase.cross_end.min(other_end) - phrase.cross_start.max(other_start);
                if overlap > TABLE_SNAP_TOLERANCE {
                    return false;
                }
            }
        }
    }
    if curr.occupied_cols == 1 {
        return true;
    }
    // Multi-column continuations (when 2+ text columns wrap together) must not
    // re-occupy the record's leading key column or begin a repeating section of
    // sparser multi-column rows.
    if let Some(lead_col) = start.first_occupied_column(orientation.inline_sign)
        && curr.slots[lead_col]
    {
        return false;
    }
    if let Some(next_line) = next
        && next_line.occupied_cols >= 2
        && grid_slot_similarity(&curr.slots, &next_line.slots) >= HYBRID_GRID_SLOT_SIMILARITY
    {
        return false;
    }
    edge_gap <= scale
}

/// Return whether two multi-column records share similar occupied columns and
/// consistent cross-axis alignment (left, right, or center) in every shared
/// column.
fn records_column_aligned(left: &ParsedBodyLine<'_>, right: &ParsedBodyLine<'_>) -> bool {
    if grid_slot_similarity(&left.slots, &right.slots) < BORDERLESS_BODY_SLOT_SIMILARITY {
        return false;
    }
    let scale = left.font_size.max(right.font_size).max(1.0);
    let tolerance = scale * TEXT_TABLE_ALIGNMENT_TOLERANCE;
    let mut shared = 0usize;
    for col in 0..left.slots.len() {
        if !left.slots[col] || !right.slots[col] {
            continue;
        }
        let (Some((l0, l1)), Some((r0, r1))) = (left.column_span(col), right.column_span(col))
        else {
            continue;
        };
        shared += 1;
        let left_aligned = (l0 - r0).abs() <= tolerance;
        let right_aligned = (l1 - r1).abs() <= tolerance;
        let center_aligned = ((l0 + l1) * 0.5 - (r0 + r1) * 0.5).abs() <= tolerance;
        if !left_aligned && !right_aligned && !center_aligned {
            return false;
        }
    }
    shared >= 2
}

/// Group all matching physical lines in one body band into logical records.
fn group_band_records<'b>(
    physical_lines: &[Vec<GlyphRecord>],
    word_lines: &[Vec<BorrowedWord<'_>>],
    (band_start, band_end): (f64, f64),
    col_bounds: &[f64],
    orientation: BodyOrientation,
    budget: &'b Budget,
) -> Result<BodyBandPlan<'b>, Error> {
    let mut parsed: BudgetVec<'b, ParsedBodyLine<'b>> = BudgetVec::new(budget);
    for (line_index, (line, words)) in physical_lines.iter().zip(word_lines).enumerate() {
        budget.poll()?;
        let Some(first) = line.first() else {
            continue;
        };
        let line_is_horizontal = first.direction.0.abs() >= first.direction.1.abs();
        if line_is_horizontal != orientation.horizontal {
            continue;
        }
        let inline_component = if orientation.horizontal {
            first.direction.0
        } else {
            first.direction.1
        };
        if (inline_component >= 0.0) != (orientation.inline_sign >= 0.0) {
            continue;
        }
        let (line_a0, line_a1) = bbox_axis_span(line_bbox(line), orientation.horizontal);
        let axis_center = (line_a0 + line_a1) * 0.5;
        if axis_center < band_start - TABLE_SNAP_TOLERANCE
            || axis_center > band_end + TABLE_SNAP_TOLERANCE
        {
            continue;
        }
        if let Some(entry) =
            parse_body_line(line_index, line, words, col_bounds, orientation, budget)?
        {
            parsed.try_push(entry)?;
        }
    }
    // Sort in reverse logical reading order so `pop` yields lines in reading
    // order while `last` peeks the next line without releasing budget tracking.
    parsed.sort_unstable_by(|left, right| {
        (right.axis_center * orientation.block_sign)
            .total_cmp(&(left.axis_center * orientation.block_sign))
    });

    let mut records: BudgetVec<'b, BodyRecord<'b>> = BudgetVec::new(budget);
    while let Some(line) = parsed.pop() {
        budget.poll()?;
        let attaches = records.last().is_some_and(|record| {
            is_wrapped_continuation(record, &line, parsed.last(), orientation)
        });
        if attaches {
            let record = records
                .last_mut()
                .expect("last record was checked immediately above");
            record.display_axis_start = record.display_axis_start.min(line.axis_start);
            record.display_axis_end = record.display_axis_end.max(line.axis_end);
            record.lines.try_push(line)?;
        } else {
            let display_axis_start = line.axis_start;
            let display_axis_end = line.axis_end;
            let mut rec_lines = BudgetVec::new(budget);
            rec_lines.try_push(line)?;
            records.try_push(BodyRecord {
                lines: rec_lines,
                display_axis_start,
                display_axis_end,
            })?;
        }
    }

    // Store records in ascending display row-axis order for grid tiling.
    if orientation.block_sign < 0.0 {
        records.reverse();
    }
    Ok(BodyBandPlan {
        axis_start: band_start,
        axis_end: band_end,
        records,
    })
}

/// Validate that the grouped body bands exhibit repeated aligned tabular
/// records rather than prose or an unrelated summary box.
fn body_plan_is_tabular(bands: &[BodyBandPlan<'_>], budget: &Budget) -> Result<bool, Error> {
    let mut total_records = 0usize;
    let mut multi_col_records = 0usize;
    let mut standalone_single_col = 0usize;
    let mut total_adjacent_pairs = 0usize;
    let mut aligned_adjacent_pairs = 0usize;

    let mut all_multi_starts: BudgetVec<'_, &ParsedBodyLine<'_>> = BudgetVec::new(budget);
    for band in bands {
        let mut prev_multi: Option<&ParsedBodyLine<'_>> = None;
        for record in band.records.iter() {
            total_records += 1;
            let start = &record.lines[0];
            if start.occupied_cols >= 2 {
                multi_col_records += 1;
                all_multi_starts.try_push(start)?;
                if let Some(previous) = prev_multi {
                    total_adjacent_pairs += 1;
                    if records_column_aligned(previous, start) {
                        aligned_adjacent_pairs += 1;
                    }
                }
                prev_multi = Some(start);
            } else {
                standalone_single_col += 1;
            }
        }
    }
    if multi_col_records < MIN_BORDERLESS_BODY_RECORDS {
        return Ok(false);
    }
    // Reject boxes dominated by unattached single-column prose lines.
    if standalone_single_col > 0 && standalone_single_col * 3 >= total_records {
        return Ok(false);
    }
    // When every band holds a single record separated by horizontal rules,
    // verify column alignment across consecutive bands instead.
    if total_adjacent_pairs == 0 {
        for pair in all_multi_starts.windows(2) {
            total_adjacent_pairs += 1;
            if records_column_aligned(pair[0], pair[1]) {
                aligned_adjacent_pairs += 1;
            }
        }
    }
    Ok(total_adjacent_pairs > 0 && aligned_adjacent_pairs * 4 >= total_adjacent_pairs * 3)
}

/// Return full-width row-axis coordinates of `rules` spanning
/// `[cross_start, cross_end]`.
fn full_width_axis_bounds(
    rules: &[RuleSegment],
    axis_bounds: &[f64],
    cross_start: f64,
    cross_end: f64,
    horizontal: bool,
    budget: &Budget,
) -> Result<Vec<f64>, Error> {
    let mut kept = Vec::new();
    for &coord in axis_bounds {
        budget.poll()?;
        if rule_covers(rules, horizontal, coord, cross_start, cross_end)? {
            try_push_text(&mut kept, coord, "body band axis bounds")?;
        }
    }
    Ok(kept)
}

/// Try to pair header component `header_index` with one or more adjacent
/// outer-bordered body components across a narrow double-rule gap along
/// `horizontal`.
fn plan_double_rule_body<'b>(
    components: &[RuleComponent],
    consumed: &[bool],
    header_index: usize,
    horizontal: bool,
    physical_lines: &[Vec<GlyphRecord>],
    word_lines: &[Vec<BorrowedWord<'_>>],
    budget: &'b Budget,
) -> Result<Option<AttachedBodyPlan<'b>>, Error> {
    let header = &components[header_index];
    let header_cross = header.cross_bounds(horizontal);
    let header_axis = header.axis_bounds(horizontal);
    if !header.bounded || header_cross.len() < 3 || header_axis.len() < 2 {
        return Ok(None);
    }
    let cross_lo = header_cross[0];
    let cross_hi = header_cross[header_cross.len() - 1];
    let header_axis_lo = header_axis[0];
    let header_axis_hi = header_axis[header_axis.len() - 1];

    // Try attaching on either side (+block or -block), letting the body box's
    // text orientation decide which side is logically "after" the header.
    for side in [1.0_f64, -1.0_f64] {
        let mut chain_indices: BudgetVec<'b, usize> = BudgetVec::new(budget);
        let mut frontier = if side > 0.0 {
            header_axis_hi
        } else {
            header_axis_lo
        };
        let mut orientation: Option<BodyOrientation> = None;

        loop {
            budget.poll()?;
            let mut next_match: Option<(usize, f64, BodyOrientation)> = None;
            for (idx, candidate) in components.iter().enumerate() {
                if idx == header_index
                    || consumed[idx]
                    || !candidate.bounded
                    || chain_indices.contains(&idx)
                {
                    continue;
                }
                let cand_cross = candidate.cross_bounds(horizontal);
                let cand_axis = candidate.axis_bounds(horizontal);
                if cand_cross.len() != 2 || cand_axis.len() < 2 {
                    continue;
                }
                if (cand_cross[0] - cross_lo).abs() > TABLE_SNAP_TOLERANCE
                    || (cand_cross[1] - cross_hi).abs() > TABLE_SNAP_TOLERANCE
                {
                    continue;
                }
                let gap = if side > 0.0 {
                    cand_axis[0] - frontier
                } else {
                    frontier - cand_axis[cand_axis.len() - 1]
                };
                if gap < -TABLE_SNAP_TOLERANCE || gap > MAX_DOUBLE_RULE_GAP {
                    continue;
                }
                let Some(cand_bbox) = candidate.bbox() else {
                    continue;
                };
                let Some(cand_orientation) =
                    body_text_orientation(physical_lines, cand_bbox, horizontal)
                else {
                    continue;
                };
                if cand_orientation.block_sign != side {
                    continue;
                }
                if let Some(existing) = orientation
                    && (existing.inline_sign != cand_orientation.inline_sign
                        || existing.block_sign != cand_orientation.block_sign)
                {
                    continue;
                }
                if next_match.is_none_or(|(_, best_gap, _)| gap < best_gap) {
                    next_match = Some((idx, gap, cand_orientation));
                }
            }
            let Some((matched_idx, _, matched_orientation)) = next_match else {
                break;
            };
            orientation = Some(matched_orientation);
            chain_indices.try_push(matched_idx)?;
            let matched_axis = components[matched_idx].axis_bounds(horizontal);
            frontier = if side > 0.0 {
                matched_axis[matched_axis.len() - 1]
            } else {
                matched_axis[0]
            };
        }

        let Some(orientation) = orientation else {
            continue;
        };

        let header_axis_rows = header_axis.len() - 1;
        let col_count = header_cross.len() - 1;
        let mut bands: BudgetVec<'b, BodyBandPlan<'b>> = BudgetVec::new(budget);
        let mut total_body_records = 0usize;
        let mut total_real_rules = header.rules.len();
        let mut inferred_row_dividers = 0usize;

        for &body_idx in chain_indices.iter() {
            let body = &components[body_idx];
            total_real_rules = total_real_rules.saturating_add(body.rules.len());
            let body_axis = full_width_axis_bounds(
                &body.rules,
                body.axis_bounds(horizontal),
                cross_lo,
                cross_hi,
                horizontal,
                budget,
            )?;
            if body_axis.len() < 2 {
                continue;
            }
            for pair in body_axis.windows(2) {
                let plan = group_band_records(
                    physical_lines,
                    word_lines,
                    (pair[0], pair[1]),
                    header_cross,
                    orientation,
                    budget,
                )?;
                if plan.records.is_empty() && pair[1] - pair[0] <= MAX_DOUBLE_RULE_GAP {
                    continue;
                }
                if !plan.records.is_empty() {
                    total_body_records = total_body_records.saturating_add(plan.records.len());
                    inferred_row_dividers =
                        inferred_row_dividers.saturating_add(plan.records.len().saturating_sub(1));
                    bands.try_push(plan)?;
                }
            }
        }

        if bands.is_empty() || !body_plan_is_tabular(&bands, budget)? {
            continue;
        }
        // Sort bands in ascending display row-axis order.
        bands.sort_unstable_by(|left, right| left.axis_start.total_cmp(&right.axis_start));

        let projected_col_rules = col_count.saturating_sub(1);
        let total_rule_count = total_real_rules
            .saturating_add(projected_col_rules)
            .saturating_add(inferred_row_dividers);
        budget.check(LimitCode::TextRuleCount, as_u64(total_rule_count))?;
        table_slots(
            header_axis_rows.saturating_add(total_body_records),
            col_count,
            budget,
        )?;

        let mut header_xs = Vec::new();
        for &x in &header.xs {
            try_push_text(&mut header_xs, x, "hybrid header x bounds")?;
        }
        let mut header_ys = Vec::new();
        for &y in &header.ys {
            try_push_text(&mut header_ys, y, "hybrid header y bounds")?;
        }
        return Ok(Some(AttachedBodyPlan {
            orientation,
            header_xs,
            header_ys,
            body_component_indices: chain_indices,
            bands,
            total_body_records,
            total_rule_count,
        }));
    }
    Ok(None)
}

/// Try to split one connected component whose ruled header touches (0 pt gap)
/// one or more trailing full-width unsplit body bands along `horizontal`.
fn plan_connected_borderless_body<'b>(
    component: &RuleComponent,
    horizontal: bool,
    physical_lines: &[Vec<GlyphRecord>],
    word_lines: &[Vec<BorrowedWord<'_>>],
    budget: &'b Budget,
) -> Result<Option<AttachedBodyPlan<'b>>, Error> {
    let cross = component.cross_bounds(horizontal);
    let axis = component.axis_bounds(horizontal);
    if !component.bounded || cross.len() < 3 || axis.len() < 3 {
        return Ok(None);
    }
    let cross_lo = cross[0];
    let cross_hi = cross[cross.len() - 1];
    let full_axis = full_width_axis_bounds(
        &component.rules,
        axis,
        cross_lo,
        cross_hi,
        horizontal,
        budget,
    )?;
    if full_axis.len() < 3 {
        return Ok(None);
    }

    // Check which full-width axis bands lack ALL internal column dividers.
    let band_count = full_axis.len() - 1;
    let mut band_has_internal_cols: BudgetVec<'b, bool> = BudgetVec::new(budget);
    for pair in full_axis.windows(2) {
        let mut has_col = false;
        for &divider in &cross[1..cross.len() - 1] {
            if rule_covers(&component.rules, !horizontal, divider, pair[0], pair[1])? {
                has_col = true;
                break;
            }
        }
        band_has_internal_cols.try_push(has_col)?;
    }

    for side in [1.0_f64, -1.0_f64] {
        // Count trailing unsplit bands on `side`.
        let unsplit_count = if side > 0.0 {
            band_has_internal_cols
                .iter()
                .rev()
                .take_while(|&&has_col| !has_col)
                .count()
        } else {
            band_has_internal_cols
                .iter()
                .take_while(|&&has_col| !has_col)
                .count()
        };
        if unsplit_count == 0 || unsplit_count == band_count {
            continue;
        }
        let split_at = if side > 0.0 {
            band_count - unsplit_count
        } else {
            unsplit_count
        };
        let split_coord = full_axis[split_at];
        let body_axis_lo = if side > 0.0 {
            split_coord
        } else {
            full_axis[0]
        };
        let body_axis_hi = if side > 0.0 {
            full_axis[band_count]
        } else {
            split_coord
        };
        let body_bbox = if horizontal {
            (cross_lo, body_axis_lo, cross_hi, body_axis_hi)
        } else {
            (body_axis_lo, cross_lo, body_axis_hi, cross_hi)
        };
        let Some(orientation) = body_text_orientation(physical_lines, body_bbox, horizontal) else {
            continue;
        };
        if orientation.block_sign != side {
            continue;
        }

        // Keep header axis coordinates up to `split_coord` (inclusive).
        let mut header_axis_coords = Vec::new();
        for &coord in axis {
            let in_header = if side > 0.0 {
                coord <= split_coord + TABLE_SNAP_TOLERANCE
            } else {
                coord >= split_coord - TABLE_SNAP_TOLERANCE
            };
            if in_header {
                try_push_text(
                    &mut header_axis_coords,
                    coord,
                    "connected header axis bounds",
                )?;
            }
        }
        if header_axis_coords.len() < 2 {
            continue;
        }
        let header_axis_rows = header_axis_coords.len() - 1;
        let col_count = cross.len() - 1;

        let body_band_range = if side > 0.0 {
            split_at..band_count
        } else {
            0..split_at
        };
        let mut bands: BudgetVec<'b, BodyBandPlan<'b>> = BudgetVec::new(budget);
        let mut total_body_records = 0usize;
        let mut inferred_row_dividers = 0usize;
        for b_idx in body_band_range {
            let (b0, b1) = (full_axis[b_idx], full_axis[b_idx + 1]);
            let plan = group_band_records(
                physical_lines,
                word_lines,
                (b0, b1),
                cross,
                orientation,
                budget,
            )?;
            if plan.records.is_empty() && b1 - b0 <= MAX_DOUBLE_RULE_GAP {
                continue;
            }
            if !plan.records.is_empty() {
                total_body_records = total_body_records.saturating_add(plan.records.len());
                inferred_row_dividers =
                    inferred_row_dividers.saturating_add(plan.records.len().saturating_sub(1));
                bands.try_push(plan)?;
            }
        }
        if bands.is_empty() || !body_plan_is_tabular(&bands, budget)? {
            continue;
        }
        bands.sort_unstable_by(|left, right| left.axis_start.total_cmp(&right.axis_start));

        let projected_col_rules = col_count.saturating_sub(1);
        let total_rule_count = component
            .rules
            .len()
            .saturating_add(projected_col_rules)
            .saturating_add(inferred_row_dividers);
        budget.check(LimitCode::TextRuleCount, as_u64(total_rule_count))?;
        table_slots(
            header_axis_rows.saturating_add(total_body_records),
            col_count,
            budget,
        )?;

        let mut cross_copy = Vec::new();
        for &c in cross {
            try_push_text(&mut cross_copy, c, "connected header cross bounds")?;
        }
        let (header_xs, header_ys) = if horizontal {
            (cross_copy, header_axis_coords)
        } else {
            (header_axis_coords, cross_copy)
        };
        return Ok(Some(AttachedBodyPlan {
            orientation,
            header_xs,
            header_ys,
            body_component_indices: BudgetVec::new(budget),
            bands,
            total_body_records,
            total_rule_count,
        }));
    }
    Ok(None)
}

/// Assemble the text of one borderless body cell across all lines of `record`
/// mapped to `column`, charging its UTF-8 bytes against `text_size`.
fn body_record_cell_text(
    record: &BodyRecord<'_>,
    column: usize,
    word_lines: &[Vec<BorrowedWord<'_>>],
    budget: &Budget,
    charged: &mut usize,
) -> Result<String, Error> {
    let mut text = String::new();
    let mut has_row = false;
    for line in record.lines.iter() {
        budget.poll()?;
        let words = &word_lines[line.line_index];
        let logical_rtl = glyphs_rtl_order(
            line.phrases
                .iter()
                .filter(|phrase| phrase.column == column)
                .flat_map(|phrase| words[phrase.word_start..phrase.word_end].iter())
                .flat_map(|(_, glyphs)| glyphs.iter()),
        ) != RtlLineOrder::None;
        let mut has_word = false;
        let mut append_word = |glyphs: &[GlyphRecord]| -> Result<(), Error> {
            if !has_word && has_row {
                try_push_str_text(&mut text, "\n", "borderless body cell text")?;
            } else if has_word {
                try_push_str_text(&mut text, " ", "borderless body cell text")?;
            }
            try_push_logical_glyphs(&mut text, glyphs, "borderless body cell text")?;
            has_word = true;
            has_row = true;
            Ok(())
        };
        if logical_rtl {
            for phrase in line.phrases.iter().rev() {
                if phrase.column != column {
                    continue;
                }
                for (_, glyphs) in words[phrase.word_start..phrase.word_end].iter().rev() {
                    append_word(glyphs)?;
                }
            }
        } else {
            for phrase in line.phrases.iter() {
                if phrase.column != column {
                    continue;
                }
                for (_, glyphs) in &words[phrase.word_start..phrase.word_end] {
                    append_word(glyphs)?;
                }
            }
        }
    }
    budget.charge(LimitCode::TextSize, as_u64(text.len()))?;
    *charged += text.len();
    Ok(text)
}

/// Compute monotonic display-space row boundaries tiling `[band.axis_start,
/// band.axis_end]` across `band.records` (already in ascending display order).
fn body_band_row_bounds<'b>(
    band: &BodyBandPlan<'_>,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, f64>, Error> {
    let mut bounds = BudgetVec::new(budget);
    bounds.try_push(band.axis_start)?;
    for pair in band.records.windows(2) {
        let raw_mid = if pair[0].display_axis_end <= pair[1].display_axis_start {
            (pair[0].display_axis_end + pair[1].display_axis_start) * 0.5
        } else {
            let c0 = (pair[0].display_axis_start + pair[0].display_axis_end) * 0.5;
            let c1 = (pair[1].display_axis_start + pair[1].display_axis_end) * 0.5;
            (c0 + c1) * 0.5
        };
        let prev = *bounds.last().expect("band start was pushed");
        bounds.try_push(raw_mid.clamp(prev, band.axis_end))?;
    }
    bounds.try_push(band.axis_end)?;
    Ok(bounds)
}

/// Materialize one combined header + borderless body table, releasing all
/// `text_size` charges if header tiling fails or any limit refuses.
fn materialize_attached_body_table(
    header_rules: &[RuleSegment],
    plan: &AttachedBodyPlan<'_>,
    word_lines: &[Vec<BorrowedWord<'_>>],
    budget: &Budget,
) -> Result<Option<TableTuple>, Error> {
    budget.check(LimitCode::TextRuleCount, as_u64(plan.total_rule_count))?;
    let mut charged = 0usize;
    let result = build_attached_body_table(header_rules, plan, word_lines, budget, &mut charged);
    if !matches!(result, Ok(Some(_))) {
        budget.release(LimitCode::TextSize, as_u64(charged));
    }
    result
}

fn build_attached_body_table(
    header_rules: &[RuleSegment],
    plan: &AttachedBodyPlan<'_>,
    word_lines: &[Vec<BorrowedWord<'_>>],
    budget: &Budget,
    charged: &mut usize,
) -> Result<Option<TableTuple>, Error> {
    let horizontal = plan.orientation.horizontal;
    let header_rows = plan.header_ys.len() - 1;
    let header_cols = plan.header_xs.len() - 1;
    let (total_rows, total_cols) = if horizontal {
        (header_rows + plan.total_body_records, header_cols)
    } else {
        (header_rows, header_cols + plan.total_body_records)
    };
    let slot_count = table_slots(total_rows, total_cols, budget)?;
    let Ok(row_count_u32) = u32::try_from(total_rows) else {
        return Ok(None);
    };
    let Ok(col_count_u32) = u32::try_from(total_cols) else {
        return Ok(None);
    };

    let Some((mut header_cells, header_anchors)) = tile_grid_cells(
        header_rules,
        &plan.header_xs,
        &plan.header_ys,
        word_lines,
        budget,
        charged,
    )?
    else {
        return Ok(None);
    };

    let mut cells = try_filled_text(slot_count, None, "hybrid borderless table cells")?;
    let mut anchors = try_filled_text(slot_count, 0u32, "hybrid borderless table anchors")?;

    // Header sits before body in display order when `block_sign > 0`, and after
    // body when `block_sign < 0`.
    let (header_row_offset, header_col_offset, body_row_offset, body_col_offset) =
        match (horizontal, plan.orientation.block_sign >= 0.0) {
            (true, true) => (0, 0, header_rows, 0),
            (true, false) => (plan.total_body_records, 0, 0, 0),
            (false, true) => (0, 0, 0, header_cols),
            (false, false) => (0, plan.total_body_records, 0, 0),
        };

    for r in 0..header_rows {
        for c in 0..header_cols {
            let old_slot = r * header_cols + c;
            let new_slot = (r + header_row_offset) * total_cols + (c + header_col_offset);
            let old_anchor = header_anchors[old_slot] as usize;
            let anchor_r = old_anchor / header_cols + header_row_offset;
            let anchor_c = old_anchor % header_cols + header_col_offset;
            let Ok(new_anchor) = u32::try_from(anchor_r * total_cols + anchor_c) else {
                return Ok(None);
            };
            anchors[new_slot] = new_anchor;
            cells[new_slot] = header_cells[old_slot].take();
        }
    }

    let col_bounds: &[f64] = if horizontal {
        &plan.header_xs
    } else {
        &plan.header_ys
    };
    let col_count = col_bounds.len() - 1;
    let mut record_offset = 0usize;
    for band in plan.bands.iter() {
        let row_bounds = body_band_row_bounds(band, budget)?;
        for (idx, record) in band.records.iter().enumerate() {
            budget.poll()?;
            let a0 = row_bounds[idx];
            let a1 = row_bounds[idx + 1];
            for col in 0..col_count {
                let text = body_record_cell_text(record, col, word_lines, budget, charged)?;
                let cell_bbox = if horizontal {
                    (col_bounds[col], a0, col_bounds[col + 1], a1)
                } else {
                    (a0, col_bounds[col], a1, col_bounds[col + 1])
                };
                let (dest_r, dest_c) = if horizontal {
                    (body_row_offset + record_offset, col)
                } else {
                    (col, body_col_offset + record_offset)
                };
                let slot = dest_r * total_cols + dest_c;
                let Ok(anchor) = u32::try_from(slot) else {
                    return Ok(None);
                };
                anchors[slot] = anchor;
                cells[slot] = Some((cell_bbox, text));
            }
            record_offset += 1;
        }
    }

    let first_band = &plan.bands[0];
    let last_band = &plan.bands[plan.bands.len() - 1];
    let (x0, y0, x1, y1) = if horizontal {
        (
            plan.header_xs[0],
            plan.header_ys[0].min(first_band.axis_start),
            plan.header_xs[header_cols],
            plan.header_ys[header_rows].max(last_band.axis_end),
        )
    } else {
        (
            plan.header_xs[0].min(first_band.axis_start),
            plan.header_ys[0],
            plan.header_xs[header_cols].max(last_band.axis_end),
            plan.header_ys[header_rows],
        )
    };

    Ok(Some((
        (x0, y0, x1, y1),
        row_count_u32,
        col_count_u32,
        cells,
        anchors,
        (0.95, None, None, None),
    )))
}

/// Detect high-confidence rectangular tables from connected vector-rule grids.
fn detect_grid_tables(
    physical_lines: &[Vec<GlyphRecord>],
    rules: &[RuleSegment],
    budget: &Budget,
) -> Result<Vec<TableTuple>, Error> {
    budget.check(LimitCode::TextRuleCount, as_u64(rules.len()))?;
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
        budget.poll()?;
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

    let mut rule_components = Vec::new();
    let mut group_start = 0;
    while group_start < grouped_rules.len() {
        budget.poll()?;
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
        let xs = clustered_coordinates(rule_coordinates(&component_rules, false)?);
        let ys = clustered_coordinates(rule_coordinates(&component_rules, true)?);
        let bounded = xs.len() >= 2
            && ys.len() >= 2
            && cell_is_bounded(
                &component_rules,
                (xs[0], ys[0], xs[xs.len() - 1], ys[ys.len() - 1]),
            )?;
        try_push_text(
            &mut rule_components,
            RuleComponent {
                rules: component_rules,
                xs,
                ys,
                bounded,
            },
            "table rule components",
        )?;
    }

    let mut consumed = try_filled_text(
        rule_components.len(),
        false,
        "consumed table rule components",
    )?;
    let mut tables = Vec::new();

    // First associate bordered header grids with adjacent outer-bordered
    // borderless-column bodies across a narrow double-rule gap (or attached
    // directly with no gap).
    for index in 0..rule_components.len() {
        budget.poll()?;
        if consumed[index] || !rule_components[index].bounded {
            continue;
        }
        let mut plan = None;
        for horizontal in [true, false] {
            if let Some(found) = plan_double_rule_body(
                &rule_components,
                &consumed,
                index,
                horizontal,
                physical_lines,
                &word_lines,
                budget,
            )? {
                plan = Some(found);
                break;
            }
        }
        if plan.is_none() {
            for horizontal in [true, false] {
                if let Some(found) = plan_connected_borderless_body(
                    &rule_components[index],
                    horizontal,
                    physical_lines,
                    &word_lines,
                    budget,
                )? {
                    plan = Some(found);
                    break;
                }
            }
        }
        if let Some(plan) = plan
            && let Some(table) = materialize_attached_body_table(
                &rule_components[index].rules,
                &plan,
                &word_lines,
                budget,
            )?
        {
            consumed[index] = true;
            for &body_idx in plan.body_component_indices.iter() {
                consumed[body_idx] = true;
            }
            try_push_text(&mut tables, table, "bordered tables")?;
        }
    }

    for (index, component) in rule_components.into_iter().enumerate() {
        budget.poll()?;
        if consumed[index] {
            continue;
        }
        let mut component_rules = component.rules;
        let original_xs = component.xs;
        let original_ys = component.ys;
        if original_xs.len() < 3 || original_ys.len() < 3 {
            continue;
        }
        table_slots(original_ys.len() - 1, original_xs.len() - 1, budget)?;
        let mut inferred_rules =
            infer_hybrid_grid_rules(physical_lines, &original_ys, &original_xs, true, budget)?;
        for rule in
            infer_hybrid_grid_rules(physical_lines, &original_xs, &original_ys, false, budget)?
        {
            budget.check(
                LimitCode::TextRuleCount,
                as_u64(inferred_rules.len()).saturating_add(1),
            )?;
            try_push_text(&mut inferred_rules, rule, "hybrid-grid inferred rules")?;
        }
        let is_hybrid = !inferred_rules.is_empty();
        budget.check(
            LimitCode::TextRuleCount,
            as_u64(component_rules.len()).saturating_add(as_u64(inferred_rules.len())),
        )?;
        for rule in inferred_rules {
            try_push_text(&mut component_rules, rule, "table component rules")?;
        }
        let xs = clustered_coordinates(rule_coordinates(&component_rules, false)?);
        let ys = clustered_coordinates(rule_coordinates(&component_rules, true)?);
        let row_count_usize = ys.len() - 1;
        let column_count_usize = xs.len() - 1;
        let Some((cells, cell_anchors)) =
            materialize_grid_cells(&component_rules, &xs, &ys, &word_lines, budget)?
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rule_components_compress_a_long_chain_without_recursion() {
        let mut components = RuleComponents::new(65_536).unwrap();
        for index in 1..components.parent.len() {
            components.parent[index] = index - 1;
        }
        assert_eq!(components.find(components.parent.len() - 1), 0);
        assert!(components.parent.iter().all(|parent| *parent == 0));
    }

    #[test]
    fn candidate_limits_refuse_instead_of_discarding_a_grid() {
        let bounds = [0.0, 10.0, 20.0];
        for (code, maximum) in [
            (LimitCode::TableCellCount, 3),
            (LimitCode::TableSpanCandidateCount, 1),
        ] {
            let budget = Budget::default().with_limit(code, Some(maximum));
            let error = materialize_grid_cells(&[], &bounds, &bounds, &[], &budget).unwrap_err();
            assert!(matches!(error, Error::Limit(refusal) if refusal.code() == code));
        }
    }

    #[test]
    fn hybrid_slot_signatures_charge_memory_and_release_on_refusal() {
        let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(1));
        let error = line_grid_slots(&[], &[0.0, 10.0, 20.0], true, &budget).unwrap_err();
        assert!(matches!(error, Error::Limit(refusal) if refusal.code() == LimitCode::MemorySize));
        assert_eq!(budget.used(LimitCode::MemorySize), 0);
    }

    /// A glyph of `ch` with its origin at `x` on the baseline y = 5.
    fn glyph(ch: char, x: f64) -> GlyphRecord {
        let text = super::super::GlyphText::Char(ch);
        GlyphRecord {
            bidi_flags: super::super::glyph_bidi_flags(&text),
            text,
            x,
            y: 5.0,
            size: 1.0,
            advance: 1.0,
            zero_advance: false,
            direction: (1.0, 0.0),
            writing_mode: 0,
            font_key: 0,
            font: super::super::FontInfo::default(),
            color: None,
            render_mode: 0,
            is_clipped: false,
            ink: None,
            is_underlined: false,
            is_struck: false,
            joins_previous: false,
            source_order: 0,
            shown_until: 1,
            run: 0,
            source: None,
        }
    }

    #[test]
    fn cell_text_counts_against_text_size() {
        let glyphs = [glyph('a', 4.0), glyph('b', 5.0)];
        let lines = vec![vec![((4.0, 4.0, 6.0, 6.0), &glyphs[..])]];
        let cell = (0.0, 0.0, 10.0, 10.0);
        let budget = Budget::default();
        let mut charged = 0;
        assert_eq!(
            cell_text(&lines, cell, &budget, &mut charged).unwrap(),
            "ab"
        );
        assert_eq!((charged, budget.used(LimitCode::TextSize)), (2, 2));

        let budget = Budget::default().with_limit(LimitCode::TextSize, Some(1));
        let mut charged = 0;
        let error = cell_text(&lines, cell, &budget, &mut charged).unwrap_err();
        assert!(matches!(error, Error::Limit(refusal) if refusal.code() == LimitCode::TextSize));
        assert_eq!((charged, budget.used(LimitCode::TextSize)), (0, 0));
    }

    #[test]
    fn a_discarded_grid_releases_the_text_of_its_cells() {
        let glyphs = [glyph('a', 4.0), glyph('b', 5.0)];
        let lines = vec![vec![((4.0, 4.0, 6.0, 6.0), &glyphs[..])]];
        let rule = |x0, y0, x1, y1, horizontal| RuleSegment {
            x0,
            y0,
            x1,
            y1,
            horizontal,
        };
        // The rules bound the first cell, which holds the text, and leave
        // the second open, so the grid is discarded after the first.
        let rules = [
            rule(0.0, 0.0, 10.0, 0.0, true),
            rule(0.0, 10.0, 10.0, 10.0, true),
            rule(0.0, 0.0, 0.0, 10.0, false),
            rule(10.0, 0.0, 10.0, 10.0, false),
        ];
        let (xs, ys) = ([0.0, 10.0, 20.0], [0.0, 10.0]);
        let budget = Budget::default();
        assert!(
            materialize_grid_cells(&rules, &xs, &ys, &lines, &budget)
                .unwrap()
                .is_none()
        );
        assert_eq!(budget.used(LimitCode::TextSize), 0);

        let (xs, ys) = ([0.0, 10.0], [0.0, 10.0]);
        let (cells, _) = materialize_grid_cells(&rules, &xs, &ys, &lines, &budget)
            .unwrap()
            .unwrap();
        assert_eq!(cells[0].as_ref().map(|(_, text)| text.as_str()), Some("ab"));
        assert_eq!(budget.used(LimitCode::TextSize), 2);
    }

    #[test]
    fn detection_polls_the_operation_deadline() {
        let budget = Budget::default().with_timeout(std::time::Duration::ZERO);
        let error = detect_grid_tables(&[], &[], &budget).unwrap_err();
        assert!(matches!(error, Error::Limit(refusal) if refusal.code() == LimitCode::Deadline));
    }

    fn text_glyphs(text: &str, x: f64, y: f64, size: f64) -> Vec<GlyphRecord> {
        let advance = size * 0.55;
        text.chars()
            .enumerate()
            .map(|(idx, ch)| {
                let mut g = glyph(ch, x + idx as f64 * advance);
                g.y = y;
                g.size = size;
                g.advance = advance;
                g
            })
            .collect()
    }

    fn rule_seg(x0: f64, y0: f64, x1: f64, y1: f64, horizontal: bool) -> RuleSegment {
        RuleSegment {
            x0,
            y0,
            x1,
            y1,
            horizontal,
        }
    }

    /// A 2-row, 3-column bordered header (`y = 20..50`) separated by a 2 pt
    /// double-rule gap from an outer-bordered body box (`y = 52..150`).
    fn double_rule_rules() -> Vec<RuleSegment> {
        vec![
            // Header outer frame + internal row/column rules
            rule_seg(20.0, 20.0, 260.0, 20.0, true),
            rule_seg(20.0, 35.0, 260.0, 35.0, true),
            rule_seg(20.0, 50.0, 260.0, 50.0, true),
            rule_seg(20.0, 20.0, 20.0, 50.0, false),
            rule_seg(80.0, 20.0, 80.0, 50.0, false),
            rule_seg(200.0, 20.0, 200.0, 50.0, false),
            rule_seg(260.0, 20.0, 260.0, 50.0, false),
            // Body outer frame (2 pt below header, no internal vertical rules)
            rule_seg(20.0, 52.0, 260.0, 52.0, true),
            rule_seg(20.0, 150.0, 260.0, 150.0, true),
            rule_seg(20.0, 52.0, 20.0, 150.0, false),
            rule_seg(260.0, 52.0, 260.0, 150.0, false),
        ]
    }

    fn double_rule_body_lines() -> Vec<Vec<GlyphRecord>> {
        let mut line0 = text_glyphs("ID", 25.0, 30.0, 6.0);
        line0.extend(text_glyphs("DESC", 85.0, 30.0, 6.0));
        line0.extend(text_glyphs("AMT", 230.0, 30.0, 6.0));

        let mut row1 = text_glyphs("R1", 25.0, 62.0, 6.0);
        row1.extend(text_glyphs("FIRST ITEM", 85.0, 62.0, 6.0));
        row1.extend(text_glyphs("10.00", 235.0, 62.0, 6.0));
        // Continuation line overflowing slightly past the x=200 header divider
        // while staying strictly left of the right-aligned amount at x=235.
        let row1_cont = text_glyphs("CONTINUATION OVERFLOWING PAST DIVIDER", 85.0, 69.0, 6.0);
        let row1_cont2 = text_glyphs("FINAL LINE", 85.0, 76.0, 6.0);

        let mut row2 = text_glyphs("R2", 25.0, 84.0, 6.0);
        row2.extend(text_glyphs("SECOND ITEM", 85.0, 84.0, 6.0));
        row2.extend(text_glyphs("20.00", 235.0, 84.0, 6.0));
        let row2_cont = text_glyphs("SECOND CONTINUATION", 85.0, 91.0, 6.0);

        vec![line0, row1, row1_cont, row1_cont2, row2, row2_cont]
    }

    #[test]
    fn double_rule_header_attaches_borderless_body_with_wrapped_continuations() {
        let rules = double_rule_rules();
        let lines = double_rule_body_lines();
        let budget = Budget::default();

        let tables = detect_grid_tables(&lines, &rules, &budget).unwrap();
        assert_eq!(tables.len(), 1);
        let (bbox, row_count, col_count, cells, _, diagnostics) = &tables[0];
        assert_eq!((*row_count, *col_count), (4, 3));
        assert_eq!(*bbox, (20.0, 20.0, 260.0, 150.0));
        assert_eq!(diagnostics.0, 0.95);
        let text_at = |r: usize, c: usize| {
            cells[r * 3 + c]
                .as_ref()
                .map(|(_, text)| text.as_str())
                .unwrap_or("")
        };
        assert_eq!(text_at(2, 0), "R1");
        assert_eq!(
            text_at(2, 1),
            "FIRST ITEM\nCONTINUATION OVERFLOWING PAST DIVIDER\nFINAL LINE"
        );
        assert_eq!(text_at(2, 2), "10.00");
        assert_eq!(text_at(3, 0), "R2");
        assert_eq!(text_at(3, 1), "SECOND ITEM\nSECOND CONTINUATION");
        assert_eq!(text_at(3, 2), "20.00");
    }

    #[test]
    fn double_rule_prose_box_is_not_attached_and_does_not_trip_cell_budget() {
        let rules = double_rule_rules();
        let mut lines = Vec::new();
        let mut header = text_glyphs("ID", 25.0, 30.0, 6.0);
        header.extend(text_glyphs("DESC", 85.0, 30.0, 6.0));
        header.extend(text_glyphs("AMT", 230.0, 30.0, 6.0));
        lines.push(header);
        // 12 single-phrase prose lines inside the lower box.
        for i in 0..12 {
            lines.push(text_glyphs(
                "Ordinary narrative sentence explaining the table above.",
                25.0,
                60.0 + i as f64 * 7.0,
                6.0,
            ));
        }
        // Tight cell budget that allows the 2x3 header (6 cells) but would fail
        // if the 12 prose lines were mistakenly counted as 12x3 table rows.
        let budget = Budget::default().with_limit(LimitCode::TableCellCount, Some(10));
        let tables = detect_grid_tables(&lines, &rules, &budget).unwrap();
        assert_eq!(tables.len(), 1);
        assert_eq!((tables[0].1, tables[0].2), (2, 3));
        assert_eq!(tables[0].5.0, 1.0);
    }

    #[test]
    fn double_rule_body_enforces_table_cell_rule_and_text_size_budgets() {
        let rules = double_rule_rules();
        let lines = double_rule_body_lines();

        // 4x3 = 12 cells exceeds a limit of 10.
        let cell_budget = Budget::default().with_limit(LimitCode::TableCellCount, Some(10));
        let err = detect_grid_tables(&lines, &rules, &cell_budget).unwrap_err();
        assert!(
            matches!(err, Error::Limit(refusal) if refusal.code() == LimitCode::TableCellCount)
        );

        // 11 real rules + 2 projected column dividers + 1 inferred row divider = 14 rules.
        let rule_budget = Budget::default().with_limit(LimitCode::TextRuleCount, Some(13));
        let err = detect_grid_tables(&lines, &rules, &rule_budget).unwrap_err();
        assert!(matches!(err, Error::Limit(refusal) if refusal.code() == LimitCode::TextRuleCount));

        // TextSize refusal mid-body releases every byte charged for header and
        // earlier body cells.
        let text_budget = Budget::default().with_limit(LimitCode::TextSize, Some(20));
        let err = detect_grid_tables(&lines, &rules, &text_budget).unwrap_err();
        assert!(matches!(err, Error::Limit(refusal) if refusal.code() == LimitCode::TextSize));
        assert_eq!(text_budget.used(LimitCode::TextSize), 0);
    }
}
