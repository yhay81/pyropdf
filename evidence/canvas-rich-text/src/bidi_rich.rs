//! Prototype of Option B for issue #1198: bounded multi-style rich-text and
//! UAX #9 BiDi paragraph layout built strictly on pyropdf's existing crates
//! (`harfrust 0.16`, `skrifa 0.50`, `unicode-bidi 0.3.18`,
//! `unicode-linebreak 0.1.5`, `unicode-segmentation 1.13.3`,
//! `unicode-script 0.5.9`, `pdf-base14-metrics 0.0.2`, and
//! `pyropdf-store::budget::{Budget, BudgetVec, LimitCode}`).
//!
//! Every input-sized collection uses `BudgetVec` and polls `Budget::poll()`
//! so memory, shaped glyph count, line count, deadline, and cancellation are
//! enforced under DESIGN.md F6.

#![allow(dead_code)]

use std::ops::Range;
use std::sync::Arc;

use harfrust::{Buffer, Direction, Font as HarfFont, Script, ShapeOptions, ShaperFont, Tag};
use pdf_base14_metrics::Base14Font;
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use skrifa::raw::TableProvider;
use unicode_bidi::{BidiInfo, Level};
use unicode_linebreak::linebreaks;
use unicode_script::{Script as UnicodeScript, UnicodeScript as _};
use unicode_segmentation::UnicodeSegmentation;

const FIT_TOLERANCE: f64 = 1.0e-6;
const POLL_STEP: usize = 256;

/// Base direction of a paragraph.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BaseDirection {
    /// Resolve per paragraph from its first strong directional character (UAX #9 P2/P3).
    Auto,
    /// Force left-to-right base embedding level (0).
    Ltr,
    /// Force right-to-left base embedding level (1).
    Rtl,
}

/// Horizontal alignment inside a text box.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Align {
    Left,
    Center,
    Right,
    Justify,
    /// Physical left for LTR paragraphs, physical right for RTL paragraphs.
    Start,
    /// Physical right for LTR paragraphs, physical left for RTL paragraphs.
    End,
}

/// Either a Standard 14 font (AFM metrics, WinAnsi) or an embedded OpenType face.
pub enum RichFace {
    Standard {
        font: Base14Font,
        ascender: f64,
        descender: f64,
    },
    OpenType {
        font: HarfFont,
        units_per_em: f64,
        ascender: f64,
        descender: f64,
        underline_position: f64,
        underline_thickness: f64,
        strikeout_position: f64,
        strikeout_thickness: f64,
    },
}

impl RichFace {
    pub fn standard(font: Base14Font) -> Self {
        Self::Standard {
            font,
            ascender: 0.8,
            descender: -0.2,
        }
    }

    pub fn from_opentype(program: Arc<Vec<u8>>, index: u32) -> Result<Self, String> {
        let source: Arc<dyn AsRef<[u8]> + Send + Sync> = program;
        let font =
            HarfFont::new(source, index).ok_or_else(|| "cannot read OpenType face".to_owned())?;
        let tables = font.tables();
        let head = tables.head().map_err(|e| e.to_string())?;
        let upem = f64::from(head.units_per_em());
        let (ascender, descender) = tables.hhea().map_or((0.8, -0.2), |hhea| {
            (
                f64::from(hhea.ascender().to_i16()) / upem,
                f64::from(hhea.descender().to_i16()) / upem,
            )
        });
        let (underline_position, underline_thickness) =
            tables.post().map_or((-0.1, 0.05), |post| {
                (
                    f64::from(post.underline_position().to_i16()) / upem,
                    (f64::from(post.underline_thickness().to_i16()) / upem).max(0.04),
                )
            });
        let (strikeout_position, strikeout_thickness) = tables.os2().map_or((0.3, 0.05), |os2| {
            (
                f64::from(os2.y_strikeout_position()) / upem,
                (f64::from(os2.y_strikeout_size()) / upem).max(0.04),
            )
        });
        Ok(Self::OpenType {
            font,
            units_per_em: upem,
            ascender,
            descender,
            underline_position,
            underline_thickness,
            strikeout_position,
            strikeout_thickness,
        })
    }

    fn metrics(&self) -> (f64, f64) {
        match self {
            Self::Standard {
                ascender,
                descender,
                ..
            } => (*ascender, *descender),
            Self::OpenType {
                ascender,
                descender,
                ..
            } => (*ascender, *descender),
        }
    }

    fn decorations(&self) -> (f64, f64, f64, f64) {
        match self {
            Self::Standard { .. } => (-0.1, 0.05, 0.3, 0.05),
            Self::OpenType {
                underline_position,
                underline_thickness,
                strikeout_position,
                strikeout_thickness,
                ..
            } => (
                *underline_position,
                *underline_thickness,
                *strikeout_position,
                *strikeout_thickness,
            ),
        }
    }
}

/// One inline styled span inside a rich-text call.
#[derive(Clone, Debug)]
pub struct SpanInput<'a> {
    pub text: &'a str,
    pub face_index: usize,
    pub size: f64,
    pub underline: bool,
    pub strikethrough: bool,
    pub link_id: Option<u32>,
}

/// One shaped glyph emitted in visual order inside a run.
#[derive(Clone, Debug)]
pub struct PositionedGlyph {
    pub glyph_id: u32,
    pub x_advance: f64,
    pub x_offset: f64,
    pub y_offset: f64,
    pub text_range: Range<usize>,
    pub rtl: bool,
}

/// One contiguous visual run on a laid-out line (uniform style, font, and BiDi level).
#[derive(Clone, Debug)]
pub struct VisualRun {
    pub span_index: usize,
    pub face_index: usize,
    pub size: f64,
    pub text_range: Range<usize>,
    pub rtl: bool,
    pub x: f64,
    pub width: f64,
    pub glyphs: Vec<PositionedGlyph>,
}

/// A decoration stroke (underline or strikethrough) in display coordinates.
#[derive(Clone, Debug)]
pub struct DecorationRule {
    pub span_index: usize,
    pub is_strikethrough: bool,
    pub x0: f64,
    pub x1: f64,
    pub y: f64,
    pub thickness: f64,
}

/// A hyperlink hit-box in display coordinates.
#[derive(Clone, Debug)]
pub struct LinkBox {
    pub link_id: u32,
    pub x0: f64,
    pub y0: f64,
    pub x1: f64,
    pub y1: f64,
}

/// One laid-out line in visual order.
#[derive(Clone, Debug)]
pub struct RichLine {
    pub baseline_y: f64,
    pub ascent: f64,
    pub descent: f64,
    pub width: f64,
    pub is_rtl_paragraph: bool,
    pub justified: bool,
    pub runs: Vec<VisualRun>,
}

/// Complete rich-text layout result ready for PDF emission.
#[derive(Clone, Debug)]
pub struct RichLayout {
    pub lines: Vec<RichLine>,
    pub decorations: Vec<DecorationRule>,
    pub links: Vec<LinkBox>,
    pub height: f64,
    pub spare_height: f64,
    pub total_glyphs: usize,
    pub peak_budget_bytes: u64,
}

/// Why rich-text layout refused its input.
#[derive(Clone, Debug)]
pub enum RichLayoutError {
    InvalidGeometry,
    Narrow,
    Font(String),
    Limit(LimitExceeded),
}

impl From<LimitExceeded> for RichLayoutError {
    fn from(value: LimitExceeded) -> Self {
        Self::Limit(value)
    }
}

/// Internal atomic cluster (grapheme/shaping cluster) in logical paragraph order,
/// pre-measured so line breaking runs in O(N) without re-shaping every prefix.
struct ClusterMeasure {
    span_index: usize,
    byte_range: Range<usize>,
    advance: f64,
    is_space: bool,
}

/// Lays out styled spans inside a `(box_width, box_height)` box with full UAX #9
/// BiDi reordering per wrapped line, UAX #14 line breaking, mixed font sizes,
/// underline/strikethrough rules, and link hit-boxes under `budget` (DESIGN.md F6).
pub fn layout_rich_text(
    faces: &[RichFace],
    spans: &[SpanInput<'_>],
    box_size: (f64, f64),
    base_direction: BaseDirection,
    align: Align,
    line_height_mult: Option<f64>,
    budget: &Budget,
) -> Result<RichLayout, RichLayoutError> {
    budget.poll()?;
    let (box_width, box_height) = box_size;
    if !box_width.is_finite() || box_width <= 0.0 || !box_height.is_finite() || box_height < 0.0 {
        return Err(RichLayoutError::InvalidGeometry);
    }

    // Concatenate spans into one budgeted buffer + byte->span map.
    let mut total_bytes = 0usize;
    let mut total_chars = 0u64;
    for span in spans {
        if !span.size.is_finite() || span.size <= 0.0 || span.face_index >= faces.len() {
            return Err(RichLayoutError::InvalidGeometry);
        }
        total_bytes = total_bytes.saturating_add(span.text.len());
        total_chars = total_chars.saturating_add(span.text.chars().count() as u64);
    }
    budget.check(LimitCode::ShapedGlyphCount, total_chars)?;
    budget.charge(LimitCode::MemorySize, total_bytes as u64)?;

    let mut full_bytes = BudgetVec::<u8>::new(budget);
    full_bytes.try_reserve_exact(total_bytes)?;
    let mut span_of_byte = BudgetVec::<u32>::new(budget);
    span_of_byte.try_reserve_exact(total_bytes)?;

    for (idx, span) in spans.iter().enumerate() {
        budget.poll()?;
        for &b in span.text.as_bytes() {
            full_bytes.try_push(b)?;
            span_of_byte.try_push(idx as u32)?;
        }
    }
    let full_text =
        std::str::from_utf8(full_bytes.as_slice()).map_err(|_| RichLayoutError::InvalidGeometry)?;

    if full_text.is_empty() {
        return Ok(RichLayout {
            lines: Vec::new(),
            decorations: Vec::new(),
            links: Vec::new(),
            height: 0.0,
            spare_height: box_height,
            total_glyphs: 0,
            peak_budget_bytes: budget.used(LimitCode::MemorySize),
        });
    }

    let forced_level = match base_direction {
        BaseDirection::Auto => None,
        BaseDirection::Ltr => Some(Level::ltr()),
        BaseDirection::Rtl => Some(Level::rtl()),
    };

    let mut lines = BudgetVec::<RichLine>::new(budget);
    let mut decorations = BudgetVec::<DecorationRule>::new(budget);
    let mut links = BudgetVec::<LinkBox>::new(budget);
    let mut total_glyphs = 0usize;
    let mut peak_budget_bytes = budget.used(LimitCode::MemorySize);
    let mut cursor_y = 0.0_f64;
    let mut prev_descent = 0.0_f64;

    // Split into explicit paragraphs on '\n'.
    let mut para_start = 0usize;
    loop {
        budget.poll()?;
        let para_end = full_text[para_start..]
            .find('\n')
            .map_or(full_text.len(), |off| para_start + off);
        let para = &full_text[para_start..para_end];

        if !para.is_empty() {
            let bidi = BidiInfo::new(para, forced_level);
            let para_info = &bidi.paragraphs[0];
            let para_is_rtl = para_info.level.is_rtl();

            // Pre-shape logical style × bidi × script items once to get exact cluster advances.
            let clusters = measure_paragraph_clusters(
                faces,
                spans,
                para,
                para_start,
                &span_of_byte,
                &bidi,
                budget,
            )?;

            // Collect UAX #14 break ends inside `para`.
            let mut break_ends = BudgetVec::<usize>::new(budget);
            for (i, (off, _)) in linebreaks(para).enumerate() {
                if i % POLL_STEP == 0 {
                    budget.poll()?;
                }
                break_ends.try_push(off)?;
            }

            // Greedy wrapping in logical order using exact cluster advances.
            let wrapped_ranges = wrap_clusters(para, &clusters, &break_ends, box_width, budget)?;

            for (line_idx, &(line_start, visible_end, soft_break)) in
                wrapped_ranges.iter().enumerate()
            {
                budget.poll()?;
                let line_count = (lines.len() + 1) as u64;
                budget.check(LimitCode::TextLineCount, line_count)?;

                let visible_range = line_start..visible_end;
                let (levels, bidi_runs) = if visible_range.is_empty() {
                    (Vec::new(), Vec::new())
                } else {
                    bidi.visual_runs(para_info, visible_range.clone())
                };

                // Determine per-line vertical metrics across all spans touching this line.
                let mut line_ascent = 0.0_f64;
                let mut line_descent = 0.0_f64;
                for &span_idx in &span_of_byte[para_start + line_start..para_start + visible_end] {
                    let sp = &spans[span_idx as usize];
                    let (asc, desc) = faces[sp.face_index].metrics();
                    line_ascent = line_ascent.max(asc * sp.size);
                    line_descent = line_descent.max((-desc) * sp.size);
                }
                if line_ascent == 0.0 {
                    line_ascent = spans[0].size * 0.8;
                    line_descent = spans[0].size * 0.2;
                }
                let mult = line_height_mult.unwrap_or(1.2);
                let baseline_y = if lines.is_empty() {
                    line_ascent
                } else {
                    cursor_y
                        + prev_descent
                        + (line_ascent + line_descent) * (mult - 1.0)
                        + line_ascent
                };
                cursor_y = baseline_y;
                prev_descent = line_descent;

                // Build visual runs: within each UAX #9 visual run, split by style span
                // (reversing sub-runs inside an RTL bidi run so visual left-to-right order holds).
                let mut visual_runs = BudgetVec::<VisualRun>::new(budget);
                let mut line_width = 0.0_f64;
                let mut space_count = 0usize;

                for bidi_run in bidi_runs {
                    let rtl = levels[bidi_run.start].is_rtl();
                    let sub_spans = split_by_span(
                        bidi_run.start,
                        bidi_run.end,
                        para_start,
                        &span_of_byte,
                        budget,
                    )?;
                    let iter_range = 0..sub_spans.len();
                    for idx_pos in iter_range {
                        let pick = if rtl {
                            sub_spans.len() - 1 - idx_pos
                        } else {
                            idx_pos
                        };
                        let (sub_start, sub_end, span_idx) = sub_spans[pick];
                        let sp = &spans[span_idx];
                        let sub_text = &para[sub_start..sub_end];
                        let shaped = shape_segment(
                            &faces[sp.face_index],
                            sub_text,
                            para_start + sub_start,
                            sp.size,
                            rtl,
                            budget,
                        )?;
                        peak_budget_bytes =
                            peak_budget_bytes.max(budget.used(LimitCode::MemorySize));
                        let run_w: f64 = shaped.iter().map(|g| g.x_advance).sum();
                        space_count += sub_text.chars().filter(|&c| c == ' ').count();
                        total_glyphs += shaped.len();
                        budget.check(LimitCode::ShapedGlyphCount, total_glyphs as u64)?;
                        visual_runs.try_push(VisualRun {
                            span_index: span_idx,
                            face_index: sp.face_index,
                            size: sp.size,
                            text_range: (para_start + sub_start)..(para_start + sub_end),
                            rtl,
                            x: line_width,
                            width: run_w,
                            glyphs: shaped.into_vec(),
                        })?;
                        line_width += run_w;
                    }
                }

                // Resolve horizontal alignment & justification.
                let free = (box_width - line_width).max(0.0);
                let justify =
                    align == Align::Justify && soft_break && space_count > 0 && free > 0.0;
                let extra_per_space = if justify {
                    free / (space_count as f64)
                } else {
                    0.0
                };
                let x_shift = if justify {
                    0.0
                } else {
                    match align {
                        Align::Left => 0.0,
                        Align::Right => free,
                        Align::Center => free * 0.5,
                        Align::Start => {
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
                        Align::Justify => {
                            if para_is_rtl {
                                free
                            } else {
                                0.0
                            }
                        }
                    }
                };

                // Apply horizontal offset, justification, decorations, and link hit-boxes.
                let mut pen_x = x_shift;
                for run in visual_runs.iter_mut() {
                    run.x = pen_x;
                    if extra_per_space > 0.0 {
                        let mut adjusted_w = 0.0;
                        for g in &mut run.glyphs {
                            if full_text[g.text_range.clone()] == *" " {
                                g.x_advance += extra_per_space;
                            }
                            adjusted_w += g.x_advance;
                        }
                        run.width = adjusted_w;
                    }
                    let sp = &spans[run.span_index];
                    let (u_pos, u_thick, s_pos, s_thick) = faces[sp.face_index].decorations();
                    if sp.underline && run.width > 0.0 {
                        decorations.try_push(DecorationRule {
                            span_index: run.span_index,
                            is_strikethrough: false,
                            x0: run.x,
                            x1: run.x + run.width,
                            y: baseline_y - u_pos * sp.size,
                            thickness: u_thick * sp.size,
                        })?;
                    }
                    if sp.strikethrough && run.width > 0.0 {
                        decorations.try_push(DecorationRule {
                            span_index: run.span_index,
                            is_strikethrough: true,
                            x0: run.x,
                            x1: run.x + run.width,
                            y: baseline_y - s_pos * sp.size,
                            thickness: s_thick * sp.size,
                        })?;
                    }
                    if let Some(link_id) = sp.link_id
                        && run.width > 0.0
                    {
                        let (asc, desc) = faces[sp.face_index].metrics();
                        links.try_push(LinkBox {
                            link_id,
                            x0: run.x,
                            y0: baseline_y - asc * sp.size,
                            x1: run.x + run.width,
                            y1: baseline_y + (-desc) * sp.size,
                        })?;
                    }
                    pen_x += run.width;
                }

                let _ = line_idx;
                lines.try_push(RichLine {
                    baseline_y,
                    ascent: line_ascent,
                    descent: line_descent,
                    width: if justify { box_width } else { line_width },
                    is_rtl_paragraph: para_is_rtl,
                    justified: justify,
                    runs: visual_runs.into_vec(),
                })?;
            }
        }

        if para_end == full_text.len() {
            break;
        }
        para_start = para_end + 1;
    }

    let total_height = if lines.is_empty() {
        0.0
    } else {
        cursor_y + prev_descent
    };
    let spare_height = box_height - total_height;

    peak_budget_bytes = peak_budget_bytes.max(budget.used(LimitCode::MemorySize));
    Ok(RichLayout {
        lines: lines.into_vec(),
        decorations: decorations.into_vec(),
        links: links.into_vec(),
        height: total_height,
        spare_height,
        total_glyphs,
        peak_budget_bytes,
    })
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
    for at in (start + 1)..end {
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

fn measure_paragraph_clusters<'b>(
    faces: &[RichFace],
    spans: &[SpanInput<'_>],
    para: &str,
    para_offset: usize,
    span_of_byte: &[u32],
    bidi: &BidiInfo<'_>,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, ClusterMeasure>, RichLayoutError> {
    let mut clusters = BudgetVec::new(budget);
    let levels = &bidi.levels;
    let mut at = 0usize;
    while at < para.len() {
        budget.poll()?;
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
        let shaped = shape_segment(
            &faces[sp.face_index],
            &para[at..end],
            at,
            sp.size,
            rtl,
            budget,
        )?;
        // Aggregate shaped glyph advances per logical cluster byte range.
        let mut range_adv = BudgetVec::<(usize, usize, f64)>::new(budget);
        for g in shaped.iter() {
            let (s, e) = (g.text_range.start, g.text_range.end);
            if let Some(last) = range_adv.iter_mut().find(|x| x.0 == s && x.1 == e) {
                last.2 += g.x_advance;
            } else {
                range_adv.try_push((s, e, g.x_advance))?;
            }
        }
        range_adv.sort_by_key(|x| x.0);
        for &(s, e, adv) in range_adv.iter() {
            clusters.try_push(ClusterMeasure {
                span_index: sp_idx,
                byte_range: s..e,
                advance: adv,
                is_space: para[s..e].chars().all(char::is_whitespace),
            })?;
        }
        at = end;
    }
    Ok(clusters)
}

fn wrap_clusters<'b>(
    para: &str,
    clusters: &[ClusterMeasure],
    break_ends: &[usize],
    max_width: f64,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, (usize, usize, bool)>, RichLayoutError> {
    let mut wrapped = BudgetVec::new(budget);
    // Prefix sums of cluster advances by byte offset.
    let mut start_byte = 0usize;
    while start_byte < para.len() {
        budget.poll()?;
        // Find longest break opportunity from start_byte whose trimmed width fits max_width.
        let mut best_break: Option<(usize, usize)> = None;
        for &brk in break_ends {
            if brk <= start_byte {
                continue;
            }
            let vis_end = para[start_byte..brk].trim_end().len() + start_byte;
            let w = range_width(clusters, start_byte, vis_end);
            if w <= max_width + FIT_TOLERANCE {
                best_break = Some((brk, vis_end));
            } else {
                break;
            }
        }
        if let Some((brk, vis_end)) = best_break {
            let soft_break = brk < para.len();
            wrapped.try_push((start_byte, vis_end, soft_break))?;
            start_byte = brk;
        } else {
            // Emergency break at extended grapheme boundary (UAX #29).
            let mut end_byte = start_byte;
            let mut acc_w = 0.0;
            for g in para[start_byte..].graphemes(true) {
                let next_end = end_byte + g.len();
                let next_w = range_width(clusters, start_byte, next_end);
                if next_w > max_width + FIT_TOLERANCE {
                    if end_byte == start_byte {
                        return Err(RichLayoutError::Narrow);
                    }
                    break;
                }
                end_byte = next_end;
                acc_w = next_w;
            }
            let _ = acc_w;
            let soft_break = end_byte < para.len();
            wrapped.try_push((start_byte, end_byte, soft_break))?;
            start_byte = end_byte;
        }
    }
    Ok(wrapped)
}

fn range_width(clusters: &[ClusterMeasure], start: usize, end: usize) -> f64 {
    let mut w = 0.0;
    for c in clusters {
        if c.byte_range.end <= start {
            continue;
        }
        if c.byte_range.start >= end {
            break;
        }
        let _ = c.is_space;
        w += c.advance;
    }
    w
}

fn shape_segment<'b>(
    face: &RichFace,
    text: &str,
    base_offset: usize,
    size: f64,
    rtl: bool,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, PositionedGlyph>, RichLayoutError> {
    let mut out = BudgetVec::new(budget);
    if text.is_empty() {
        return Ok(out);
    }
    match face {
        RichFace::Standard { font, .. } => {
            let mut chars = BudgetVec::<(usize, char)>::new(budget);
            for (off, ch) in text.char_indices() {
                chars.try_push((off, ch))?;
            }
            if rtl {
                chars.reverse();
            }
            for &(off, ch) in chars.iter() {
                let code = ch as u32;
                let byte = u8::try_from(code).unwrap_or(b'?');
                let width_1000 = f64::from(font.winansi_width(byte).unwrap_or(500.0));
                out.try_push(PositionedGlyph {
                    glyph_id: code,
                    x_advance: width_1000 * size / 1000.0,
                    x_offset: 0.0,
                    y_offset: 0.0,
                    text_range: (base_offset + off)..(base_offset + off + ch.len_utf8()),
                    rtl,
                })?;
            }
            Ok(out)
        }
        RichFace::OpenType {
            font, units_per_em, ..
        } => {
            let shaper = ShaperFont::new(font);
            let mut buffer = Buffer::new();
            let mut scripts = BudgetVec::new(budget);
            script_runs(text, &mut scripts)?;
            if rtl {
                scripts.reverse();
            }
            let scale = size / *units_per_em;
            for &(ref range, script) in scripts.iter() {
                budget.poll()?;
                buffer.clear();
                buffer.push_str(&text[range.clone()]);
                buffer.set_direction(if rtl {
                    Direction::RightToLeft
                } else {
                    Direction::LeftToRight
                });
                buffer.set_script(script);
                buffer.guess_segment_properties();
                harfrust::shape(&shaper, &mut buffer, ShapeOptions::new())
                    .map_err(|e| RichLayoutError::Font(e.to_string()))?;
                let infos = buffer.glyph_infos();
                let positions = buffer.glyph_positions();
                let mut starts = BudgetVec::new(budget);
                for info in infos {
                    starts.try_push(range.start + info.cluster as usize)?;
                }
                starts.try_push(range.end)?;
                starts.sort_unstable();
                for (info, pos) in infos.iter().zip(positions.iter()) {
                    let s = range.start + info.cluster as usize;
                    let e = starts[starts.partition_point(|&at| at <= s)];
                    out.try_push(PositionedGlyph {
                        glyph_id: info.glyph_id,
                        x_advance: f64::from(pos.x_advance) * scale,
                        x_offset: f64::from(pos.x_offset) * scale,
                        y_offset: f64::from(pos.y_offset) * scale,
                        text_range: (base_offset + s)..(base_offset + e),
                        rtl,
                    })?;
                }
            }
            Ok(out)
        }
    }
}

fn script_runs<'b>(
    text: &str,
    scripts: &mut BudgetVec<'b, (Range<usize>, Option<Script>)>,
) -> Result<(), LimitExceeded> {
    let mut current: Option<(usize, UnicodeScript)> = None;
    for (at, ch) in text.char_indices() {
        let sc = ch.script();
        if matches!(
            sc,
            UnicodeScript::Common | UnicodeScript::Inherited | UnicodeScript::Unknown
        ) {
            continue;
        }
        match current {
            None => current = Some((0, sc)),
            Some((start, prev)) if prev != sc => {
                scripts.try_push((start..at, harfrust_script(prev)))?;
                current = Some((at, sc));
            }
            Some(_) => {}
        }
    }
    match current {
        Some((start, sc)) => scripts.try_push((start..text.len(), harfrust_script(sc))),
        None => scripts.try_push((0..text.len(), None)),
    }
}

fn harfrust_script(script: UnicodeScript) -> Option<Script> {
    let tag: [u8; 4] = script.short_name().as_bytes().try_into().ok()?;
    Script::from_iso15924_tag(Tag::new(&tag))
}
