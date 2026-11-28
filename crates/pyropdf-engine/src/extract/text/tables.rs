//! `Page.tables`: the tables that a page's vector rules bound or its text
//! aligns, ported from pylopdf's `TablePage` and table detection in
//! `rust/src/extract.rs` at 036be111cafe77755db4bb2be17bc70a8f2f9d82.

use std::sync::Arc;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};

use super::layout::{RuleSegment, glyphs_bbox};
use super::{
    BBox, Error, FontInfo, GlyphRecord, LINE_SEGMENT_GAP, LINE_TOLERANCE, RtlLineOrder,
    TextOptions, as_u64, cluster_lines, collect_page_glyphs, glyph_end, glyph_is_nonspacing_mark,
    glyph_progress, glyphs_rtl_order, has_vertical_baseline, line_bbox, needs_gap,
    try_push_glyph_text, try_push_str_text, try_push_text, updates_gap_anchor,
};
use crate::bridge::Bridged;
use crate::render::Fallback;

/// Fewest rows that a borderless text table holds.
const MIN_TEXT_TABLE_ROWS: usize = 3;
/// How far, in ems, the column edges of a text table's rows may differ.
const TEXT_TABLE_ALIGNMENT_TOLERANCE: f64 = 1.0;
/// The widest gap, in ems, between two rows of one text table.
const TEXT_TABLE_ROW_GAP: f64 = 2.5;
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

/// Caller-supplied geometry and detection tuning for [`page_tables`].
#[derive(Clone, Debug)]
pub struct TableOptions {
    pub text_strategy: bool,
    pub clip: Option<BBox>,
    pub horizontal_lines: Vec<f64>,
    pub vertical_lines: Vec<f64>,
    pub snap_tolerance: f64,
    pub join_tolerance: f64,
    pub min_gutter: f64,
    pub min_words_vertical: usize,
    pub min_words_horizontal: usize,
}

impl Default for TableOptions {
    fn default() -> Self {
        Self {
            text_strategy: false,
            clip: None,
            horizontal_lines: Vec::new(),
            vertical_lines: Vec::new(),
            snap_tolerance: TABLE_SNAP_TOLERANCE,
            join_tolerance: TABLE_SNAP_TOLERANCE,
            min_gutter: LINE_SEGMENT_GAP,
            min_words_vertical: MIN_TEXT_TABLE_ROWS,
            min_words_horizontal: 2,
        }
    }
}

/// Scalar geometric tolerances copied from [`TableOptions`].
#[derive(Clone, Copy, Debug)]
struct TableTolerances {
    snap: f64,
    join: f64,
    min_gutter: f64,
    min_words_vertical: usize,
    min_words_horizontal: usize,
}

impl Default for TableTolerances {
    fn default() -> Self {
        Self {
            snap: TABLE_SNAP_TOLERANCE,
            join: TABLE_SNAP_TOLERANCE,
            min_gutter: LINE_SEGMENT_GAP,
            min_words_vertical: MIN_TEXT_TABLE_ROWS,
            min_words_horizontal: 2,
        }
    }
}

impl From<&TableOptions> for TableTolerances {
    fn from(options: &TableOptions) -> Self {
        Self {
            snap: options.snap_tolerance,
            join: options.join_tolerance,
            min_gutter: options.min_gutter,
            min_words_vertical: options.min_words_vertical,
            min_words_horizontal: options.min_words_horizontal,
        }
    }
}

/// A table slot: its box and text, or `None` for a slot that a merged cell
/// covers.
pub type TableCell = Option<(BBox, String)>;
type MaterializedGrid = (Vec<TableCell>, Vec<u32>);
type BorrowedWord<'a> = (BBox, &'a [GlyphRecord]);
/// A table's confidence, and for a text table its alignment error, minimum
/// gutter, and row gap variation in ems.
pub type TableDiagnosticsTuple = (f64, Option<f64>, Option<f64>, Option<f64>);
/// A table: its box, row and column counts, row-major slots, the slot that
/// anchors the cell covering each slot, its diagnostics, and the detected
/// leading header row count.
pub type TableTuple = (
    BBox,
    u32,
    u32,
    Vec<TableCell>,
    Vec<u32>,
    TableDiagnosticsTuple,
    u32,
);

/// Returns the tables of the page at `index` according to `options`, and the
/// number of glyphs without Unicode left out.
///
/// The page's glyphs count against the budgets of [`super::page_text`]
/// while detection runs, and the text of the cells of a ruled table against
/// `text_size` for the rest of the operation. A refusal keeps nothing.
pub fn page_tables(
    bridged: &Arc<Bridged>,
    index: usize,
    options: &TableOptions,
    budget: &Budget,
) -> Result<(Vec<TableTuple>, usize), Error> {
    let tol = TableTolerances::from(options);
    let explicit_xs = clustered_coordinates(
        copy_f64_slice(&options.vertical_lines, "explicit vertical table lines")?,
        tol.snap,
    );
    let explicit_ys = clustered_coordinates(
        copy_f64_slice(&options.horizontal_lines, "explicit horizontal table lines")?,
        tol.snap,
    );
    let full_explicit = explicit_xs.len() >= 2 && explicit_ys.len() >= 2;
    let text_options = TextOptions {
        styles: !options.text_strategy && !full_explicit,
        ..TextOptions::default()
    };
    let collected = collect_page_glyphs(
        bridged,
        &Fallback::default(),
        index,
        &text_options,
        None,
        budget,
    )?;
    let charge = collected.charge;
    let fonts = collected.fonts;
    let page_box = (
        collected.page_box.x0,
        collected.page_box.y0,
        collected.page_box.x1,
        collected.page_box.y1,
    );
    let rules = collected.rules;
    let detected = cluster_lines(collected.glyphs).and_then(|lines| {
        budget.check(
            LimitCode::TextRuleCount,
            as_u64(rules.len())
                .saturating_add(as_u64(options.horizontal_lines.len()))
                .saturating_add(as_u64(options.vertical_lines.len())),
        )?;
        if full_explicit {
            let word_lines = build_word_lines(&lines)?;
            let mut tables = Vec::new();
            if let Some(table) = materialize_explicit_grid(
                &explicit_xs,
                &explicit_ys,
                &word_lines,
                tol,
                &fonts,
                budget,
            )? {
                try_push_text(&mut tables, table, "explicit grid tables")?;
            }
            return Ok(tables);
        }
        if options.text_strategy {
            if !explicit_xs.is_empty() || !explicit_ys.is_empty() {
                let word_lines = build_word_lines(&lines)?;
                let mut tables = Vec::new();
                if let Some(table) = detect_single_axis_explicit_table(
                    &lines,
                    &word_lines,
                    &explicit_xs,
                    &explicit_ys,
                    options.clip,
                    tol,
                    &fonts,
                    budget,
                )? {
                    try_push_text(&mut tables, table, "explicit guide tables")?;
                }
                return Ok(tables);
            }
            return detect_text_tables(&lines, tol, &fonts, budget);
        }
        if !explicit_xs.is_empty() || !explicit_ys.is_empty() {
            let combined_rules = augment_rules_with_guides(
                &rules,
                &lines,
                &explicit_xs,
                &explicit_ys,
                options.clip,
                page_box,
                budget,
            )?;
            let tables = detect_grid_tables(&lines, &combined_rules, tol, &fonts, true, budget)?;
            if !tables.is_empty() {
                return Ok(tables);
            }
            let word_lines = build_word_lines(&lines)?;
            let mut fallback_tables = Vec::new();
            if let Some(table) = detect_single_axis_explicit_table(
                &lines,
                &word_lines,
                &explicit_xs,
                &explicit_ys,
                options.clip,
                tol,
                &fonts,
                budget,
            )? {
                try_push_text(&mut fallback_tables, table, "explicit guide tables")?;
            }
            return Ok(fallback_tables);
        }
        detect_grid_tables(&lines, &rules, tol, &fonts, false, budget)
    });
    charge.release(budget);
    let mut tables = detected?;
    tables.retain(|table| {
        options
            .clip
            .is_none_or(|clip| bbox_is_inside(table.0, clip, tol.snap))
    });
    Ok((tables, collected.skipped))
}

/// Copies a slice of `f64` values through fallible allocation.
fn copy_f64_slice(values: &[f64], label: &str) -> Result<Vec<f64>, Error> {
    let mut out = Vec::new();
    for &value in values {
        try_push_text(&mut out, value, label)?;
    }
    Ok(out)
}

/// Splits every clustered physical line into borrowed words once.
fn build_word_lines<'a>(
    physical_lines: &'a [Vec<GlyphRecord>],
) -> Result<Vec<Vec<BorrowedWord<'a>>>, Error> {
    let mut word_lines = Vec::new();
    for line in physical_lines {
        try_push_text(
            &mut word_lines,
            split_word_slices(line)?,
            "bordered table word lines",
        )?;
    }
    Ok(word_lines)
}

/// Returns whether a box lies inside a display-space clip.
fn bbox_is_inside(
    (x0, y0, x1, y1): BBox,
    (clip_x0, clip_y0, clip_x1, clip_y1): BBox,
    snap: f64,
) -> bool {
    x0 >= clip_x0 - snap && y0 >= clip_y0 - snap && x1 <= clip_x1 + snap && y1 <= clip_y1 + snap
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

/// Count widely separated segments along one line using `min_gutter` ems.
fn line_segment_count_with(line: &[GlyphRecord], min_gutter: f64) -> usize {
    let mut count = usize::from(!line.is_empty());
    let mut previous_end: Option<f64> = None;
    let mut previous_size = 0.0_f64;
    for glyph in line {
        let threshold = previous_size.max(glyph.size).max(1.0) * min_gutter;
        if previous_end.is_some_and(|end| glyph_progress(glyph) - end > threshold) {
            count += 1;
        }
        previous_end = Some(glyph_end(glyph));
        previous_size = glyph.size;
    }
    count
}

/// Borrows the independently positioned columns or cells of one line.
fn split_line_segment_slices(
    line: &[GlyphRecord],
    min_gutter: f64,
) -> Result<Vec<&[GlyphRecord]>, Error> {
    let mut segments = Vec::new();
    let mut segment_start = 0;
    let mut previous_end: Option<f64> = None;
    let mut previous_size = 0.0_f64;
    for (index, glyph) in line.iter().enumerate() {
        let threshold = previous_size.max(glyph.size).max(1.0) * min_gutter;
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
        if updates_gap_anchor(glyph) {
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

/// Return whether perpendicular rule segments meet within join tolerance.
fn rules_intersect(horizontal: RuleSegment, vertical: RuleSegment, join: f64) -> bool {
    vertical.x0 >= horizontal.x0 - join
        && vertical.x0 <= horizontal.x1 + join
        && horizontal.y0 >= vertical.y0 - join
        && horizontal.y0 <= vertical.y1 + join
}

/// Snap nearby line coordinates to one stable grid coordinate.
fn clustered_coordinates(mut values: Vec<f64>, snap: f64) -> Vec<f64> {
    values.sort_unstable_by(f64::total_cmp);
    if values.is_empty() {
        return values;
    }
    let mut write = 0;
    let mut sum = values[0];
    let mut count = 1usize;
    for read in 1..values.len() {
        let value = values[read];
        if (value - sum / count as f64).abs() <= snap {
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
    tol: TableTolerances,
) -> Result<bool, Error> {
    let mut intervals = Vec::new();
    for rule in rules {
        if rule.horizontal != horizontal {
            continue;
        }
        let rule_fixed = if horizontal { rule.y0 } else { rule.x0 };
        if (rule_fixed - fixed).abs() > tol.snap {
            continue;
        }
        let (rule_start, rule_end) = if horizontal {
            (rule.x0, rule.x1)
        } else {
            (rule.y0, rule.y1)
        };
        if rule_end >= start - tol.join && rule_start <= end + tol.join {
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
        if interval_start > covered + tol.join {
            break;
        }
        covered = covered.max(interval_end);
        if covered >= end - tol.join {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Return whether a candidate cell has all four outer borders.
fn cell_is_bounded(
    rules: &[RuleSegment],
    (x0, y0, x1, y1): BBox,
    tol: TableTolerances,
) -> Result<bool, Error> {
    Ok(rule_covers(rules, true, y0, x0, x1, tol)?
        && rule_covers(rules, true, y1, x0, x1, tol)?
        && rule_covers(rules, false, x0, y0, y1, tol)?
        && rule_covers(rules, false, x1, y0, y1, tol)?)
}

/// Reject a spanning candidate if a complete internal rule splits it.
#[expect(
    clippy::too_many_arguments,
    reason = "grid rules, bounds, candidate cell span, and tolerances"
)]
fn cell_has_internal_split(
    rules: &[RuleSegment],
    xs: &[f64],
    ys: &[f64],
    row_start: usize,
    row_end: usize,
    column_start: usize,
    column_end: usize,
    tol: TableTolerances,
) -> Result<bool, Error> {
    let (x0, x1) = (xs[column_start], xs[column_end]);
    let (y0, y1) = (ys[row_start], ys[row_end]);
    for &x in &xs[column_start + 1..column_end] {
        if rule_covers(rules, false, x, y0, y1, tol)? {
            return Ok(true);
        }
    }
    for &y in &ys[row_start + 1..row_end] {
        if rule_covers(rules, true, y, x0, x1, tol)? {
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
    tol: TableTolerances,
    budget: &Budget,
) -> Result<Option<MaterializedGrid>, Error> {
    let mut charged = 0;
    let grid = tile_grid_cells(rules, xs, ys, word_lines, tol, budget, &mut charged);
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
    tol: TableTolerances,
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
            let mut best = cell_is_bounded(rules, base_bbox, tol)?.then_some((
                1,
                row_start + 1,
                column_start + 1,
            ));
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
                        if !cell_is_bounded(rules, bbox, tol)?
                            || cell_has_internal_split(
                                rules,
                                xs,
                                ys,
                                row_start,
                                row_end,
                                column_start,
                                column_end,
                                tol,
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
            cells[slot] = Some((
                bbox,
                cell_text(word_lines, bbox, tol.snap, budget, charged)?,
            ));
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
fn grid_interval(bounds: &[f64], coordinate: f64, snap: f64) -> Option<usize> {
    bounds
        .windows(2)
        .position(|pair| coordinate >= pair[0] - snap && coordinate <= pair[1] + snap)
}

/// Return the cross-axis grid slots occupied by one physical text line.
fn line_grid_slots<'b>(
    line: &[GlyphRecord],
    bounds: &[f64],
    horizontal: bool,
    snap: f64,
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
        if let Some(index) = grid_interval(bounds, coordinate, snap) {
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
    tol: TableTolerances,
    budget: &Budget,
) -> Result<Vec<RuleSegment>, Error> {
    let cross_count = cross_bounds.len().saturating_sub(1);
    if axis_bounds.len() < 2 || cross_count < 2 {
        return Ok(Vec::new());
    }
    let minimum_occupancy =
        ((cross_count as f64 * HYBRID_GRID_OCCUPANCY_RATIO).ceil() as usize).max(2);
    let min_rows = tol.min_words_vertical.max(2);
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
            if center < interval[0] - tol.snap || center > interval[1] + tol.snap {
                continue;
            }
            let slots = line_grid_slots(line, cross_bounds, horizontal, tol.snap, budget)?;
            if slots.iter().filter(|occupied| **occupied).count() < minimum_occupancy {
                continue;
            }
            let size = line.iter().map(|glyph| glyph.size).fold(0.0, f64::max);
            candidates.try_push((center, size, slots))?;
        }
        if candidates.len() < min_rows {
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
    snap: f64,
    budget: &Budget,
    charged: &mut usize,
) -> Result<String, Error> {
    let text = physical_cell_text(lines, bbox, snap, budget)?;
    budget.charge(LimitCode::TextSize, as_u64(text.len()))?;
    *charged += text.len();
    Ok(text)
}

/// The text of the words of `lines` whose centers fall inside a cell, in
/// physical order.
fn physical_cell_text(
    lines: &[Vec<BorrowedWord<'_>>],
    (x0, y0, x1, y1): BBox,
    snap: f64,
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
            center_x >= x0 - snap
                && center_x <= x1 + snap
                && center_y >= y0 - snap
                && center_y <= y1 + snap
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
                if center_x < x0 - snap
                    || center_x > x1 + snap
                    || center_y < y0 - snap
                    || center_y > y1 + snap
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
fn text_table_diagnostics(
    rows: &[Vec<&[GlyphRecord]>],
    tol: TableTolerances,
) -> TableDiagnosticsTuple {
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

    let row_depth = (rows.len().saturating_sub(tol.min_words_vertical) as f64 + 1.0) / 3.0;
    let alignment_quality =
        (1.0 - alignment_error_em / TEXT_TABLE_ALIGNMENT_TOLERANCE).clamp(0.0, 1.0);
    let spacing_quality = (1.0 - row_gap_variation_em / TEXT_TABLE_ROW_GAP).clamp(0.0, 1.0);
    let gutter_quality = (minimum_gutter_em / (tol.min_gutter * 2.0)).clamp(0.0, 1.0);
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
    tol: TableTolerances,
    fonts: &[FontInfo],
    budget: &Budget,
) -> Result<Option<TableTuple>, Error> {
    if rows.len() < tol.min_words_vertical {
        return Ok(None);
    }
    let row_count_usize = rows.len();
    let column_count_usize = rows[0].len();
    if column_count_usize < tol.min_words_horizontal {
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
    let mut row_styles = Vec::new();
    for (row_index, row) in rows.iter().enumerate() {
        budget.poll()?;
        try_push_text(
            &mut row_styles,
            segmented_row_style(row, fonts),
            "borderless table row styles",
        )?;
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
    let diagnostics = text_table_diagnostics(rows, tol);
    let Ok(slot_count) = u32::try_from(slot_count) else {
        return Ok(None);
    };
    let mut cell_anchors = Vec::new();
    for anchor in 0..slot_count {
        try_push_text(&mut cell_anchors, anchor, "borderless table cell anchors")?;
    }
    let header_rows = detect_header_rows(
        row_count_usize,
        column_count_usize,
        &cells,
        &cell_anchors,
        &row_styles,
        None,
    );
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
        header_rows,
    )))
}

fn flush_text_table_run(
    run: &mut Vec<Vec<&[GlyphRecord]>>,
    tables: &mut Vec<TableTuple>,
    tol: TableTolerances,
    fonts: &[FontInfo],
    budget: &Budget,
) -> Result<(), Error> {
    if let Some(table) = text_table_from_rows(run, tol, fonts, budget)? {
        try_push_text(tables, table, "borderless tables")?;
    }
    run.clear();
    Ok(())
}

/// Detect opt-in borderless tables from sustained aligned text segments.
fn detect_text_tables(
    physical_lines: &[Vec<GlyphRecord>],
    tol: TableTolerances,
    fonts: &[FontInfo],
    budget: &Budget,
) -> Result<Vec<TableTuple>, Error> {
    let mut tables = Vec::new();
    let mut run = Vec::new();

    for line in physical_lines {
        budget.poll()?;
        let segment_count = line_segment_count_with(line, tol.min_gutter);
        let segments = if !line.is_empty()
            && !has_vertical_baseline(&line[0])
            && segment_count >= tol.min_words_horizontal
        {
            budget.check(LimitCode::TableColumnCount, as_u64(segment_count))?;
            let segments = split_line_segment_slices(line, tol.min_gutter)?;
            (segments.len() == segment_count).then_some(segments)
        } else {
            None
        };
        let Some(segments) = segments else {
            flush_text_table_run(&mut run, &mut tables, tol, fonts, budget)?;
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
            flush_text_table_run(&mut run, &mut tables, tol, fonts, budget)?;
            try_push_text(&mut run, segments, "borderless table row run")?;
        }
    }
    flush_text_table_run(&mut run, &mut tables, tol, fonts, budget)?;
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
    snap: f64,
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
        if axis_center < axis_lo - snap
            || axis_center > axis_hi + snap
            || cross_center < cross_lo - snap
            || cross_center > cross_hi + snap
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
    snap: f64,
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
        grid_interval(col_bounds, (lo + hi) * 0.5, snap)
    };

    // Logical-first word: `words` is in `glyph_progress` order, so index
    // `word_start` is logical-first for LTR and `word_end - 1` for pure RTL.
    let min_anchor_width = snap * 2.0;
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
    tol: TableTolerances,
    budget: &'b Budget,
) -> Result<Option<ParsedBodyLine<'b>>, Error> {
    let col_count = col_bounds.len().saturating_sub(1);
    if col_count < 2 || line.is_empty() || words.is_empty() {
        return Ok(None);
    }
    let cross_min = col_bounds[0] - tol.snap;
    let cross_max = col_bounds[col_count] + tol.snap;

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
            let wide_gutter = gap > scale * tol.min_gutter;
            let crosses_divider = col_bounds[1..col_count].iter().any(|&divider| {
                let d = divider * orientation.inline_sign;
                prev_inline_end <= d + tol.snap && next_inline_start >= d - tol.snap
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
            tol.snap,
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
    snap: f64,
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
                if overlap > snap {
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
    tol: TableTolerances,
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
        if axis_center < band_start - tol.snap || axis_center > band_end + tol.snap {
            continue;
        }
        if let Some(entry) = parse_body_line(
            line_index,
            line,
            words,
            col_bounds,
            orientation,
            tol,
            budget,
        )? {
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
            is_wrapped_continuation(record, &line, parsed.last(), orientation, tol.snap)
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
    tol: TableTolerances,
    budget: &Budget,
) -> Result<Vec<f64>, Error> {
    let mut kept = Vec::new();
    for &coord in axis_bounds {
        budget.poll()?;
        if rule_covers(rules, horizontal, coord, cross_start, cross_end, tol)? {
            try_push_text(&mut kept, coord, "body band axis bounds")?;
        }
    }
    Ok(kept)
}

/// Try to pair header component `header_index` with one or more adjacent
/// outer-bordered body components across a narrow double-rule gap along
/// `horizontal`.
#[expect(
    clippy::too_many_arguments,
    reason = "components, state, geometry, tolerances, and budget"
)]
fn plan_double_rule_body<'b>(
    components: &[RuleComponent],
    consumed: &[bool],
    header_index: usize,
    horizontal: bool,
    physical_lines: &[Vec<GlyphRecord>],
    word_lines: &[Vec<BorrowedWord<'_>>],
    tol: TableTolerances,
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
                if (cand_cross[0] - cross_lo).abs() > tol.snap
                    || (cand_cross[1] - cross_hi).abs() > tol.snap
                {
                    continue;
                }
                let gap = if side > 0.0 {
                    cand_axis[0] - frontier
                } else {
                    frontier - cand_axis[cand_axis.len() - 1]
                };
                if gap < -tol.snap || gap > MAX_DOUBLE_RULE_GAP {
                    continue;
                }
                let Some(cand_bbox) = candidate.bbox() else {
                    continue;
                };
                let Some(cand_orientation) =
                    body_text_orientation(physical_lines, cand_bbox, horizontal, tol.snap)
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
                tol,
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
                    tol,
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
    tol: TableTolerances,
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
        tol,
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
            if rule_covers(
                &component.rules,
                !horizontal,
                divider,
                pair[0],
                pair[1],
                tol,
            )? {
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
        let Some(orientation) =
            body_text_orientation(physical_lines, body_bbox, horizontal, tol.snap)
        else {
            continue;
        };
        if orientation.block_sign != side {
            continue;
        }

        // Keep header axis coordinates up to `split_coord` (inclusive).
        let mut header_axis_coords = Vec::new();
        for &coord in axis {
            let in_header = if side > 0.0 {
                coord <= split_coord + tol.snap
            } else {
                coord >= split_coord - tol.snap
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
                tol,
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
    tol: TableTolerances,
    fonts: &[FontInfo],
    budget: &Budget,
) -> Result<Option<TableTuple>, Error> {
    budget.check(LimitCode::TextRuleCount, as_u64(plan.total_rule_count))?;
    let mut charged = 0usize;
    let result = build_attached_body_table(
        header_rules,
        plan,
        word_lines,
        tol,
        fonts,
        budget,
        &mut charged,
    );
    if !matches!(result, Ok(Some(_))) {
        budget.release(LimitCode::TextSize, as_u64(charged));
    }
    result
}

fn build_attached_body_table(
    header_rules: &[RuleSegment],
    plan: &AttachedBodyPlan<'_>,
    word_lines: &[Vec<BorrowedWord<'_>>],
    tol: TableTolerances,
    fonts: &[FontInfo],
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
        tol,
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

    let structural_headers =
        (horizontal && plan.orientation.block_sign >= 0.0).then_some(header_rows);
    let row_styles = grid_row_styles(
        total_rows, total_cols, &cells, &anchors, word_lines, fonts, tol.snap, budget,
    )?;
    let detected_headers = detect_header_rows(
        total_rows,
        total_cols,
        &cells,
        &anchors,
        &row_styles,
        structural_headers,
    );

    Ok(Some((
        (x0, y0, x1, y1),
        row_count_u32,
        col_count_u32,
        cells,
        anchors,
        (0.95, None, None, None),
        detected_headers,
    )))
}

/// Detect high-confidence rectangular tables from connected vector-rule grids.
fn detect_grid_tables(
    physical_lines: &[Vec<GlyphRecord>],
    rules: &[RuleSegment],
    tol: TableTolerances,
    fonts: &[FontInfo],
    allow_small_explicit: bool,
    budget: &Budget,
) -> Result<Vec<TableTuple>, Error> {
    budget.check(LimitCode::TextRuleCount, as_u64(rules.len()))?;
    let word_lines = build_word_lines(physical_lines)?;
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
            if rules_intersect(rules[horizontal_index], rules[vertical_index], tol.join) {
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
        let xs = clustered_coordinates(rule_coordinates(&component_rules, false)?, tol.snap);
        let ys = clustered_coordinates(rule_coordinates(&component_rules, true)?, tol.snap);
        let bounded = xs.len() >= 2
            && ys.len() >= 2
            && cell_is_bounded(
                &component_rules,
                (xs[0], ys[0], xs[xs.len() - 1], ys[ys.len() - 1]),
                tol,
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
                tol,
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
                    tol,
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
                tol,
                fonts,
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

    let min_grid_lines = if allow_small_explicit { 2 } else { 3 };
    for (index, component) in rule_components.into_iter().enumerate() {
        budget.poll()?;
        if consumed[index] {
            continue;
        }
        let mut component_rules = component.rules;
        let original_xs = component.xs;
        let original_ys = component.ys;
        if original_xs.len() < min_grid_lines || original_ys.len() < min_grid_lines {
            continue;
        }
        table_slots(original_ys.len() - 1, original_xs.len() - 1, budget)?;
        let horizontal_inferred = infer_hybrid_grid_rules(
            physical_lines,
            &original_ys,
            &original_xs,
            true,
            tol,
            budget,
        )?;
        let structural_headers = horizontal_inferred
            .iter()
            .map(|r| r.y0)
            .reduce(f64::min)
            .and_then(|first_inferred_y| {
                let top_bands = original_ys
                    .windows(2)
                    .take_while(|pair| pair[1] <= first_inferred_y + tol.snap)
                    .count();
                (top_bands > 0).then_some(top_bands)
            });
        let mut inferred_rules = horizontal_inferred;
        for rule in infer_hybrid_grid_rules(
            physical_lines,
            &original_xs,
            &original_ys,
            false,
            tol,
            budget,
        )? {
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
        let xs = clustered_coordinates(rule_coordinates(&component_rules, false)?, tol.snap);
        let ys = clustered_coordinates(rule_coordinates(&component_rules, true)?, tol.snap);
        let row_count_usize = ys.len() - 1;
        let column_count_usize = xs.len() - 1;
        let Some((cells, cell_anchors)) =
            materialize_grid_cells(&component_rules, &xs, &ys, &word_lines, tol, budget)?
        else {
            continue;
        };
        let Ok(row_count) = u32::try_from(row_count_usize) else {
            continue;
        };
        let Ok(column_count) = u32::try_from(column_count_usize) else {
            continue;
        };
        let row_styles = grid_row_styles(
            row_count_usize,
            column_count_usize,
            &cells,
            &cell_anchors,
            &word_lines,
            fonts,
            tol.snap,
            budget,
        )?;
        let header_rows = detect_header_rows(
            row_count_usize,
            column_count_usize,
            &cells,
            &cell_anchors,
            &row_styles,
            structural_headers,
        );
        try_push_text(
            &mut tables,
            (
                (
                    xs[0],
                    ys[0],
                    *xs.last().expect("at least two coordinates were checked"),
                    *ys.last().expect("at least two coordinates were checked"),
                ),
                row_count,
                column_count,
                cells,
                cell_anchors,
                (if is_hybrid { 0.95 } else { 1.0 }, None, None, None),
                header_rows,
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

/// Materialize one table directly from caller-specified display-space `xs` and
/// `ys` grid coordinates.
fn materialize_explicit_grid(
    xs: &[f64],
    ys: &[f64],
    word_lines: &[Vec<BorrowedWord<'_>>],
    tol: TableTolerances,
    fonts: &[FontInfo],
    budget: &Budget,
) -> Result<Option<TableTuple>, Error> {
    if xs.len() < 2 || ys.len() < 2 {
        return Ok(None);
    }
    let row_count_usize = ys.len() - 1;
    let column_count_usize = xs.len() - 1;
    table_slots(row_count_usize, column_count_usize, budget)?;
    budget.check(
        LimitCode::TextRuleCount,
        as_u64(xs.len()).saturating_add(as_u64(ys.len())),
    )?;
    let (x0, x1) = (xs[0], xs[xs.len() - 1]);
    let (y0, y1) = (ys[0], ys[ys.len() - 1]);
    let mut rules = Vec::new();
    for &y in ys {
        try_push_text(
            &mut rules,
            RuleSegment {
                x0,
                y0: y,
                x1,
                y1: y,
                horizontal: true,
            },
            "explicit horizontal table rules",
        )?;
    }
    for &x in xs {
        try_push_text(
            &mut rules,
            RuleSegment {
                x0: x,
                y0,
                x1: x,
                y1,
                horizontal: false,
            },
            "explicit vertical table rules",
        )?;
    }
    let Some((cells, cell_anchors)) =
        materialize_grid_cells(&rules, xs, ys, word_lines, tol, budget)?
    else {
        return Ok(None);
    };
    let Ok(row_count) = u32::try_from(row_count_usize) else {
        return Ok(None);
    };
    let Ok(column_count) = u32::try_from(column_count_usize) else {
        return Ok(None);
    };
    let row_styles = grid_row_styles(
        row_count_usize,
        column_count_usize,
        &cells,
        &cell_anchors,
        word_lines,
        fonts,
        tol.snap,
        budget,
    )?;
    let header_rows = detect_header_rows(
        row_count_usize,
        column_count_usize,
        &cells,
        &cell_anchors,
        &row_styles,
        None,
    );
    Ok(Some((
        (x0, y0, x1, y1),
        row_count,
        column_count,
        cells,
        cell_anchors,
        (1.0, None, None, None),
        header_rows,
    )))
}

/// Augment detected page rules with synthetic spanning rules for caller guides.
fn augment_rules_with_guides(
    rules: &[RuleSegment],
    physical_lines: &[Vec<GlyphRecord>],
    explicit_xs: &[f64],
    explicit_ys: &[f64],
    clip: Option<BBox>,
    page_box: BBox,
    budget: &Budget,
) -> Result<Vec<RuleSegment>, Error> {
    let mut x_lo = f64::INFINITY;
    let mut x_hi = f64::NEG_INFINITY;
    let mut y_lo = f64::INFINITY;
    let mut y_hi = f64::NEG_INFINITY;
    if let Some((cx0, cy0, cx1, cy1)) = clip {
        x_lo = cx0;
        x_hi = cx1;
        y_lo = cy0;
        y_hi = cy1;
    } else {
        for rule in rules {
            x_lo = x_lo.min(rule.x0).min(rule.x1);
            x_hi = x_hi.max(rule.x0).max(rule.x1);
            y_lo = y_lo.min(rule.y0).min(rule.y1);
            y_hi = y_hi.max(rule.y0).max(rule.y1);
        }
        for line in physical_lines {
            if line.is_empty() {
                continue;
            }
            let (lx0, ly0, lx1, ly1) = line_bbox(line);
            x_lo = x_lo.min(lx0);
            x_hi = x_hi.max(lx1);
            y_lo = y_lo.min(ly0);
            y_hi = y_hi.max(ly1);
        }
    }
    if explicit_xs.len() >= 2 {
        x_lo = explicit_xs[0];
        x_hi = explicit_xs[explicit_xs.len() - 1];
    }
    if explicit_ys.len() >= 2 {
        y_lo = explicit_ys[0];
        y_hi = explicit_ys[explicit_ys.len() - 1];
    }
    if !x_lo.is_finite() || !x_hi.is_finite() || x_hi <= x_lo {
        x_lo = page_box.0;
        x_hi = page_box.2;
    }
    if !y_lo.is_finite() || !y_hi.is_finite() || y_hi <= y_lo {
        y_lo = page_box.1;
        y_hi = page_box.3;
    }

    let total = rules
        .len()
        .saturating_add(explicit_xs.len())
        .saturating_add(explicit_ys.len());
    budget.check(LimitCode::TextRuleCount, as_u64(total))?;
    let mut combined = Vec::new();
    for &rule in rules {
        try_push_text(&mut combined, rule, "augmented table rules")?;
    }
    for &y in explicit_ys {
        try_push_text(
            &mut combined,
            RuleSegment {
                x0: x_lo,
                y0: y,
                x1: x_hi,
                y1: y,
                horizontal: true,
            },
            "augmented horizontal table rules",
        )?;
    }
    for &x in explicit_xs {
        try_push_text(
            &mut combined,
            RuleSegment {
                x0: x,
                y0: y_lo,
                x1: x,
                y1: y_hi,
                horizontal: false,
            },
            "augmented vertical table rules",
        )?;
    }
    Ok(combined)
}

/// Derive the missing axis of a borderless table when the caller supplied
/// explicit guides on exactly one axis (`vertical_lines` or `horizontal_lines`).
#[expect(
    clippy::too_many_arguments,
    reason = "lines, words, single-axis guides, clip, tolerances, fonts, and budget"
)]
fn detect_single_axis_explicit_table(
    physical_lines: &[Vec<GlyphRecord>],
    word_lines: &[Vec<BorrowedWord<'_>>],
    explicit_xs: &[f64],
    explicit_ys: &[f64],
    clip: Option<BBox>,
    tol: TableTolerances,
    fonts: &[FontInfo],
    budget: &Budget,
) -> Result<Option<TableTuple>, Error> {
    if explicit_xs.len() >= 2 && explicit_ys.is_empty() {
        let (x_lo, x_hi) = (explicit_xs[0], explicit_xs[explicit_xs.len() - 1]);
        let mut row_spans = Vec::new();
        for words in word_lines {
            budget.poll()?;
            let mut y0 = f64::INFINITY;
            let mut y1 = f64::NEG_INFINITY;
            for &((wx0, wy0, wx1, wy1), _) in words {
                let cx = (wx0 + wx1) * 0.5;
                let cy = (wy0 + wy1) * 0.5;
                if cx < x_lo - tol.snap || cx > x_hi + tol.snap {
                    continue;
                }
                if let Some((_, cy0, _, cy1)) = clip
                    && (cy < cy0 - tol.snap || cy > cy1 + tol.snap)
                {
                    continue;
                }
                y0 = y0.min(wy0);
                y1 = y1.max(wy1);
            }
            if y0 <= y1 {
                try_push_text(
                    &mut row_spans,
                    (y0, y1),
                    "explicit vertical guide row spans",
                )?;
            }
        }
        if row_spans.is_empty() {
            return Ok(None);
        }
        row_spans.sort_unstable_by(|left, right| left.0.total_cmp(&right.0));
        let mut merged_rows: Vec<(f64, f64)> = Vec::new();
        for (start, end) in row_spans {
            if let Some(last) = merged_rows.last_mut()
                && start <= last.1 + tol.snap
            {
                last.1 = last.1.max(end);
            } else {
                try_push_text(
                    &mut merged_rows,
                    (start, end),
                    "explicit vertical guide row spans",
                )?;
            }
        }
        let mut ys = Vec::new();
        try_push_text(
            &mut ys,
            merged_rows[0].0,
            "explicit vertical guide row bounds",
        )?;
        for pair in merged_rows.windows(2) {
            let mid = (pair[0].1 + pair[1].0) * 0.5;
            let prev = *ys.last().expect("start bound pushed");
            if mid > prev + tol.snap {
                try_push_text(&mut ys, mid, "explicit vertical guide row bounds")?;
            }
        }
        let last_y = merged_rows[merged_rows.len() - 1].1;
        if last_y > *ys.last().expect("start bound pushed") {
            try_push_text(&mut ys, last_y, "explicit vertical guide row bounds")?;
        }
        if ys.len() < 2 {
            return Ok(None);
        }
        return materialize_explicit_grid(explicit_xs, &ys, word_lines, tol, fonts, budget);
    }

    if explicit_ys.len() >= 2 && explicit_xs.is_empty() {
        let (y_lo, y_hi) = (explicit_ys[0], explicit_ys[explicit_ys.len() - 1]);
        let mut intervals = Vec::new();
        for line in physical_lines {
            budget.poll()?;
            if line.is_empty() {
                continue;
            }
            let (_, ly0, _, ly1) = line_bbox(line);
            let cy = (ly0 + ly1) * 0.5;
            if cy < y_lo - tol.snap || cy > y_hi + tol.snap {
                continue;
            }
            for segment in split_line_segment_slices(line, tol.min_gutter)? {
                let (sx0, _, sx1, _) = line_bbox(segment);
                let cx = (sx0 + sx1) * 0.5;
                if let Some((cx0, _, cx1, _)) = clip
                    && (cx < cx0 - tol.snap || cx > cx1 + tol.snap)
                {
                    continue;
                }
                try_push_text(&mut intervals, (sx0, sx1), "explicit horizontal guide cols")?;
            }
        }
        if intervals.is_empty() {
            return Ok(None);
        }
        intervals.sort_unstable_by(|left, right| left.0.total_cmp(&right.0));
        let mut merged: Vec<(f64, f64)> = Vec::new();
        for (start, end) in intervals {
            if let Some(last) = merged.last_mut()
                && start <= last.1 + tol.snap
            {
                last.1 = last.1.max(end);
            } else {
                try_push_text(&mut merged, (start, end), "explicit horizontal guide cols")?;
            }
        }
        budget.check(LimitCode::TableColumnCount, as_u64(merged.len()))?;
        let mut xs = Vec::new();
        try_push_text(&mut xs, merged[0].0, "explicit horizontal guide x bounds")?;
        for pair in merged.windows(2) {
            try_push_text(
                &mut xs,
                (pair[0].1 + pair[1].0) * 0.5,
                "explicit horizontal guide x bounds",
            )?;
        }
        try_push_text(
            &mut xs,
            merged[merged.len() - 1].1,
            "explicit horizontal guide x bounds",
        )?;
        if xs.len() < 2 {
            return Ok(None);
        }
        return materialize_explicit_grid(&xs, explicit_ys, word_lines, tol, fonts, budget);
    }

    Ok(None)
}

/// Returns whether a cell string represents a numeric/formatted-number value.
fn is_numeric_like(text: &str) -> bool {
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return false;
    }
    let mut has_digit = false;
    for ch in trimmed.chars() {
        if ch.is_ascii_digit() {
            has_digit = true;
        } else if matches!(
            ch,
            '$' | '€' | '£' | '¥' | '%' | ',' | '.' | '+' | '-' | '−' | '(' | ')' | '/' | ':' | ' '
        ) {
            continue;
        } else {
            return false;
        }
    }
    has_digit
}

#[derive(Clone, Copy)]
struct RowStyle {
    is_bold: bool,
    max_size: f64,
    uniform_font_id: Option<u32>,
}

/// Summarize style flags for one text-strategy row.
fn segmented_row_style(row: &[&[GlyphRecord]], fonts: &[FontInfo]) -> RowStyle {
    let mut bold = 0usize;
    let mut total = 0usize;
    let mut max_size = 0.0_f64;
    let mut first_font: Option<u32> = None;
    let mut mixed_font = false;
    for segment in row {
        for glyph in *segment {
            if glyph.text.chars().all(char::is_whitespace) {
                continue;
            }
            total += 1;
            max_size = max_size.max(glyph.size);
            if let Some(prev) = first_font {
                if prev != glyph.font_id {
                    mixed_font = true;
                }
            } else {
                first_font = Some(glyph.font_id);
            }
            if fonts
                .get(glyph.font_id as usize)
                .is_some_and(|font| font.flags & 16 != 0)
            {
                bold += 1;
            }
        }
    }
    RowStyle {
        is_bold: total > 0 && bold * 2 >= total,
        max_size,
        uniform_font_id: if mixed_font { None } else { first_font },
    }
}

/// Summarize style flags for each row of a grid table.
#[expect(
    clippy::too_many_arguments,
    reason = "grid dimensions, cells, anchors, borrowed words, fonts, snap tolerance, and budget"
)]
fn grid_row_styles(
    row_count: usize,
    col_count: usize,
    cells: &[TableCell],
    anchors: &[u32],
    word_lines: &[Vec<BorrowedWord<'_>>],
    fonts: &[FontInfo],
    snap: f64,
    budget: &Budget,
) -> Result<Vec<RowStyle>, Error> {
    let mut styles = Vec::new();
    for row in 0..row_count {
        budget.poll()?;
        let mut row_y0 = f64::INFINITY;
        let mut row_y1 = f64::NEG_INFINITY;
        let mut single_row_y0 = f64::INFINITY;
        let mut single_row_y1 = f64::NEG_INFINITY;
        let mut row_x0 = f64::INFINITY;
        let mut row_x1 = f64::NEG_INFINITY;
        for col in 0..col_count {
            let slot = row * col_count + col;
            if let Some(((x0, y0, x1, y1), _)) = &cells[slot] {
                row_x0 = row_x0.min(*x0);
                row_y0 = row_y0.min(*y0);
                row_x1 = row_x1.max(*x1);
                row_y1 = row_y1.max(*y1);
                let spans_below = row + 1 < row_count
                    && anchors
                        .get((row + 1) * col_count + col)
                        .is_some_and(|&below_anchor| below_anchor as usize == slot);
                if !spans_below {
                    single_row_y0 = single_row_y0.min(*y0);
                    single_row_y1 = single_row_y1.max(*y1);
                }
            }
        }
        if single_row_y0 <= single_row_y1 {
            row_y0 = single_row_y0;
            row_y1 = single_row_y1;
        }
        let mut bold = 0usize;
        let mut total = 0usize;
        let mut max_size = 0.0_f64;
        let mut first_font: Option<u32> = None;
        let mut mixed_font = false;
        if row_x0 <= row_x1 && row_y0 <= row_y1 {
            for line in word_lines {
                for &((wx0, wy0, wx1, wy1), glyphs) in line {
                    let cx = (wx0 + wx1) * 0.5;
                    let cy = (wy0 + wy1) * 0.5;
                    if cx < row_x0 - snap
                        || cx > row_x1 + snap
                        || cy < row_y0 - snap
                        || cy > row_y1 + snap
                    {
                        continue;
                    }
                    for glyph in glyphs {
                        if glyph.text.chars().all(char::is_whitespace) {
                            continue;
                        }
                        total += 1;
                        max_size = max_size.max(glyph.size);
                        if let Some(prev) = first_font {
                            if prev != glyph.font_id {
                                mixed_font = true;
                            }
                        } else {
                            first_font = Some(glyph.font_id);
                        }
                        if fonts
                            .get(glyph.font_id as usize)
                            .is_some_and(|font| font.flags & 16 != 0)
                        {
                            bold += 1;
                        }
                    }
                }
            }
        }
        try_push_text(
            &mut styles,
            RowStyle {
                is_bold: total > 0 && bold * 2 >= total,
                max_size,
                uniform_font_id: if mixed_font { None } else { first_font },
            },
            "table header row styles",
        )?;
    }
    Ok(styles)
}

/// Detect the number of leading header rows (`0` for headerless tables, `1`
/// for a standard single-row header, `>= 2` for multi-row grouped headers).
fn detect_header_rows(
    row_count: usize,
    col_count: usize,
    cells: &[TableCell],
    anchors: &[u32],
    row_styles: &[RowStyle],
    structural_header_rows: Option<usize>,
) -> u32 {
    if row_count < 2 || col_count == 0 {
        return 0;
    }
    if let Some(structural) = structural_header_rows
        && structural > 0
        && structural < row_count
    {
        return u32::try_from(structural).unwrap_or(0);
    }

    let cell_str = |row: usize, col: usize| -> &str {
        let slot = row * col_count + col;
        let anchor = anchors.get(slot).copied().unwrap_or(slot as u32) as usize;
        cells
            .get(anchor)
            .and_then(|cell| cell.as_ref())
            .map_or("", |(_, text)| text.as_str())
            .trim()
    };
    let row_has_colspan = |row: usize| -> bool {
        (1..col_count).any(|col| {
            let prev = anchors[row * col_count + col - 1] as usize;
            let curr = anchors[row * col_count + col] as usize;
            curr == prev && curr / col_count == row
        })
    };
    let row_continues_rowspan = |row: usize| -> bool {
        row > 0
            && (0..col_count).any(|col| (anchors[row * col_count + col] as usize) / col_count < row)
    };
    let body_col_is_numeric = |start_row: usize, col: usize| -> bool {
        let mut non_empty = 0usize;
        let mut numeric = 0usize;
        for row in start_row..row_count {
            let value = cell_str(row, col);
            if !value.is_empty() {
                non_empty += 1;
                if is_numeric_like(value) {
                    numeric += 1;
                }
            }
        }
        non_empty > 0 && numeric * 2 >= non_empty
    };
    let row_has_numeric =
        |row: usize| -> bool { (0..col_count).any(|col| is_numeric_like(cell_str(row, col))) };

    let max_header = (row_count - 1).min(4);

    // 1. Typographical header distinction (bold rows, notably larger font, or a
    // distinct uniform font such as unembedded Standard 14 bold headers).
    if row_styles.len() == row_count {
        if row_styles[0].is_bold {
            let bold_run = row_styles[..max_header]
                .iter()
                .take_while(|style| style.is_bold)
                .count();
            if bold_run > 0 && bold_run < row_count {
                let body_rows = row_count - bold_run;
                let non_bold_body = row_styles[bold_run..]
                    .iter()
                    .filter(|style| !style.is_bold)
                    .count();
                if non_bold_body * 2 > body_rows {
                    return bold_run as u32;
                }
            }
        }
        let body_max_size = row_styles[1..]
            .iter()
            .map(|style| style.max_size)
            .fold(0.0_f64, f64::max);
        if body_max_size > 0.0 && row_styles[0].max_size > body_max_size * 1.12 {
            let large_run = row_styles[..max_header]
                .iter()
                .take_while(|style| style.max_size > body_max_size * 1.12)
                .count();
            if large_run > 0 && large_run < row_count {
                return large_run as u32;
            }
        }
        if let Some(head_font) = row_styles[0].uniform_font_id
            && !row_has_numeric(0)
        {
            let font_run = row_styles[..max_header]
                .iter()
                .take_while(|style| style.uniform_font_id == Some(head_font))
                .count();
            if font_run > 0
                && font_run < row_count
                && (0..font_run).all(|row| !row_has_numeric(row))
                && let Some(body_font) = row_styles[font_run..]
                    .iter()
                    .find_map(|style| style.uniform_font_id)
                && body_font != head_font
            {
                let body_rows = row_count - font_run;
                let uniform_body_matches = row_styles[font_run..]
                    .iter()
                    .filter(|style| style.uniform_font_id == Some(body_font))
                    .count();
                let body_reuses_head = row_styles[font_run..]
                    .iter()
                    .any(|style| style.uniform_font_id == Some(head_font));
                if !body_reuses_head && uniform_body_matches * 2 > body_rows {
                    return font_run as u32;
                }
            }
        }
    }

    // 2. Multi-row merged group header (column spans or vertical row spans in
    // leading rows above data rows).
    if row_count >= 3 && (row_has_colspan(0) || row_continues_rowspan(1)) {
        let mut h = 1usize;
        while h < max_header {
            let spans_from_above = row_continues_rowspan(h);
            let subheader_under_span = row_has_colspan(h - 1)
                && !row_has_numeric(h)
                && (0..col_count).any(|col| body_col_is_numeric(h + 1, col));
            if spans_from_above || subheader_under_span {
                h += 1;
            } else {
                break;
            }
        }
        if h >= 2 && !row_has_numeric(h - 1) {
            let has_numeric_body = (0..col_count).any(|col| body_col_is_numeric(h, col));
            let distinct_subheaders = (0..col_count).any(|col| {
                let slot = (h - 1) * col_count + col;
                (anchors[slot] as usize) / col_count == h - 1 && !cell_str(h - 1, col).is_empty()
            });
            if has_numeric_body || distinct_subheaders {
                return h as u32;
            }
        }
    }

    // 3. Single-row header by text-to-numeric column transition.
    let mut transition_cols = 0usize;
    let mut row0_numeric_in_numeric_col = 0usize;
    for col in 0..col_count {
        if !body_col_is_numeric(1, col) {
            continue;
        }
        let head = cell_str(0, col);
        if is_numeric_like(head) {
            row0_numeric_in_numeric_col += 1;
        } else if !head.is_empty() {
            transition_cols += 1;
        }
    }
    if transition_cols > 0 && row0_numeric_in_numeric_col == 0 {
        return 1;
    }

    0
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
        let tol = TableTolerances::default();
        for (code, maximum) in [
            (LimitCode::TableCellCount, 3),
            (LimitCode::TableSpanCandidateCount, 1),
        ] {
            let budget = Budget::default().with_limit(code, Some(maximum));
            let error =
                materialize_grid_cells(&[], &bounds, &bounds, &[], tol, &budget).unwrap_err();
            assert!(matches!(error, Error::Limit(refusal) if refusal.code() == code));
        }
    }

    #[test]
    fn hybrid_slot_signatures_charge_memory_and_release_on_refusal() {
        let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(1));
        let error = line_grid_slots(&[], &[0.0, 10.0, 20.0], true, TABLE_SNAP_TOLERANCE, &budget)
            .unwrap_err();
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
            font_id: 0,
            extent_asc: super::super::DEFAULT_ASCENDER,
            extent_desc: -super::super::DEFAULT_DESCENDER,
            color: None,
            render_mode: 0,
            is_clipped: false,
            ink: None,
            is_underlined: false,
            is_struck: false,
            joins_previous: false,
            is_ruby: false,
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
            cell_text(&lines, cell, TABLE_SNAP_TOLERANCE, &budget, &mut charged).unwrap(),
            "ab"
        );
        assert_eq!((charged, budget.used(LimitCode::TextSize)), (2, 2));

        let budget = Budget::default().with_limit(LimitCode::TextSize, Some(1));
        let mut charged = 0;
        let error =
            cell_text(&lines, cell, TABLE_SNAP_TOLERANCE, &budget, &mut charged).unwrap_err();
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
        let tol = TableTolerances::default();
        let (xs, ys) = ([0.0, 10.0, 20.0], [0.0, 10.0]);
        let budget = Budget::default();
        assert!(
            materialize_grid_cells(&rules, &xs, &ys, &lines, tol, &budget)
                .unwrap()
                .is_none()
        );
        assert_eq!(budget.used(LimitCode::TextSize), 0);

        let (xs, ys) = ([0.0, 10.0], [0.0, 10.0]);
        let (cells, _) = materialize_grid_cells(&rules, &xs, &ys, &lines, tol, &budget)
            .unwrap()
            .unwrap();
        assert_eq!(cells[0].as_ref().map(|(_, text)| text.as_str()), Some("ab"));
        assert_eq!(budget.used(LimitCode::TextSize), 2);
    }

    #[test]
    fn detection_polls_the_operation_deadline() {
        let budget = Budget::default().with_timeout(std::time::Duration::ZERO);
        let error = detect_grid_tables(&[], &[], TableTolerances::default(), &[], false, &budget)
            .unwrap_err();
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

        let tables = detect_grid_tables(
            &lines,
            &rules,
            TableTolerances::default(),
            &[],
            false,
            &budget,
        )
        .unwrap();
        assert_eq!(tables.len(), 1);
        let (bbox, row_count, col_count, cells, _, diagnostics, header_rows) = &tables[0];
        assert_eq!((*row_count, *col_count), (4, 3));
        assert_eq!(*header_rows, 2);
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
        let tables = detect_grid_tables(
            &lines,
            &rules,
            TableTolerances::default(),
            &[],
            false,
            &budget,
        )
        .unwrap();
        assert_eq!(tables.len(), 1);
        assert_eq!((tables[0].1, tables[0].2), (2, 3));
        assert_eq!(tables[0].5.0, 1.0);
    }

    #[test]
    fn double_rule_body_enforces_table_cell_rule_and_text_size_budgets() {
        let rules = double_rule_rules();
        let lines = double_rule_body_lines();
        let tol = TableTolerances::default();

        // 4x3 = 12 cells exceeds a limit of 10.
        let cell_budget = Budget::default().with_limit(LimitCode::TableCellCount, Some(10));
        let err = detect_grid_tables(&lines, &rules, tol, &[], false, &cell_budget).unwrap_err();
        assert!(
            matches!(err, Error::Limit(refusal) if refusal.code() == LimitCode::TableCellCount)
        );

        // 11 real rules + 2 projected column dividers + 1 inferred row divider = 14 rules.
        let rule_budget = Budget::default().with_limit(LimitCode::TextRuleCount, Some(13));
        let err = detect_grid_tables(&lines, &rules, tol, &[], false, &rule_budget).unwrap_err();
        assert!(matches!(err, Error::Limit(refusal) if refusal.code() == LimitCode::TextRuleCount));

        // TextSize refusal mid-body releases every byte charged for header and
        // earlier body cells.
        let text_budget = Budget::default().with_limit(LimitCode::TextSize, Some(20));
        let err = detect_grid_tables(&lines, &rules, tol, &[], false, &text_budget).unwrap_err();
        assert!(matches!(err, Error::Limit(refusal) if refusal.code() == LimitCode::TextSize));
        assert_eq!(text_budget.used(LimitCode::TextSize), 0);
    }

    #[test]
    fn grid_row_styles_ignore_rowspan_bleed_from_body_rows() {
        let regular_font = FontInfo {
            name: std::sync::Arc::from("Regular"),
            flags: 0,
            ascender: 0.8,
            descender: -0.2,
        };
        let bold_font = FontInfo {
            name: std::sync::Arc::from("Bold"),
            flags: 16,
            ascender: 0.8,
            descender: -0.2,
        };
        let fonts = [regular_font, bold_font];
        // Row 0 has Col 0 spanning rows 0..2 (y=0..40) and Col 1 single-row (y=0..20), both bold.
        // Row 1 Col 1 (y=20..40) has a long non-bold body string that must not dilute Row 0's bold style.
        let cells: Vec<TableCell> = vec![
            Some(((0.0, 0.0, 50.0, 40.0), "ID".to_owned())),
            Some(((50.0, 0.0, 150.0, 20.0), "Name".to_owned())),
            None,
            Some(((50.0, 20.0, 150.0, 40.0), "OrdinaryBodyText".to_owned())),
        ];
        let anchors = vec![0u32, 1, 0, 3];
        let mut h0 = text_glyphs("ID", 10.0, 10.0, 10.0);
        for g in &mut h0 {
            g.font_id = 1;
        }
        let mut h1 = text_glyphs("Name", 60.0, 10.0, 10.0);
        for g in &mut h1 {
            g.font_id = 1;
        }
        let body = text_glyphs("OrdinaryBodyText", 60.0, 30.0, 10.0);
        let word_lines = vec![
            vec![(glyphs_bbox(&h0), &h0[..]), (glyphs_bbox(&h1), &h1[..])],
            vec![(glyphs_bbox(&body), &body[..])],
        ];
        let budget = Budget::default();
        let styles =
            grid_row_styles(2, 2, &cells, &anchors, &word_lines, &fonts, 1.0, &budget).unwrap();
        assert!(styles[0].is_bold);
        assert!(!styles[1].is_bold);
    }

    #[test]
    fn single_axis_vertical_guides_merge_overlapping_row_spans() {
        // Two overlapping physical lines on the same visual row (y=20..32 and y=22..30),
        // followed by a cleanly separated second visual row (y=48..60).
        let line_a = text_glyphs("Alpha", 20.0, 30.0, 12.0);
        let line_b = text_glyphs("100", 120.0, 29.0, 8.0);
        let line_c = text_glyphs("Beta", 20.0, 58.0, 12.0);
        let line_d = text_glyphs("200", 120.0, 58.0, 12.0);
        let physical_lines = vec![line_a, line_b, line_c, line_d];
        let word_lines = build_word_lines(&physical_lines).unwrap();
        let budget = Budget::default();
        let table = detect_single_axis_explicit_table(
            &physical_lines,
            &word_lines,
            &[10.0, 100.0, 200.0],
            &[],
            None,
            TableTolerances::default(),
            &[],
            &budget,
        )
        .unwrap()
        .expect("single-axis table should be detected");
        assert_eq!((table.1, table.2), (2, 2));
        let text_at = |r: usize, c: usize| table.3[r * 2 + c].as_ref().unwrap().1.as_str();
        assert_eq!(text_at(0, 0), "Alpha");
        assert_eq!(text_at(0, 1), "100");
        assert_eq!(text_at(1, 0), "Beta");
        assert_eq!(text_at(1, 1), "200");
    }
}
