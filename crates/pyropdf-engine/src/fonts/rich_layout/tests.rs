use proptest::prelude::*;
use pyropdf_store::budget::{Budget, CancellationToken, LimitCode};

use super::super::embed::Face;
use super::super::shape::FallbackSource;
use super::{
    Align, BaseDirection, RichLayout, RichLayoutError, RichLayoutOptions, SpanInput,
    layout_rich_text,
};
use crate::standard;

const JAPANESE: &[u8] = include_bytes!("../testdata/NotoSansJP-subset.otf");
const HEBREW: &[u8] = include_bytes!("../testdata/NotoSansHebrew-subset.ttf");
const ARABIC: &[u8] = include_bytes!("../testdata/NotoSansArabic-Variable-subset.ttf");
const LATIN_VAR: &[u8] = include_bytes!("../testdata/variable-latin.ttf");

fn load_faces() -> (Face, Face, Face, Face) {
    (
        Face::read(JAPANESE, 0).unwrap(),
        Face::read(HEBREW, 0).unwrap(),
        Face::read(ARABIC, 0).unwrap(),
        Face::read(LATIN_VAR, 0).unwrap(),
    )
}

fn span<'a>(text: &'a str, face_index: usize, size: f64) -> SpanInput<'a> {
    SpanInput {
        text,
        face_index,
        fallback_faces: &[],
        size,
        underline: false,
        strikethrough: false,
        link_id: None,
    }
}

/// Concatenates every glyph's `text` byte range across a line in logical byte order.
fn line_logical_text(layout: &RichLayout, line_idx: usize) -> String {
    let mut ranges: Vec<_> = layout.lines[line_idx]
        .runs
        .iter()
        .flat_map(|run| run.glyphs.iter().map(|g| g.text.clone()))
        .collect();
    ranges.sort_by_key(|r| (r.start, r.end));
    ranges
        .into_iter()
        .map(|r| &layout.text[r])
        .collect::<String>()
}

#[test]
fn ltr_paragraph_wraps_and_preserves_text() {
    let (jp, _, _, _) = load_faces();
    let faces = [FallbackSource::Embedded(&jp)];
    let budget = Budget::default();
    let spans = [span(
        "office fit AVA office fit AVA office fit AVA",
        0,
        12.0,
    )];
    let mut opts = RichLayoutOptions::textbox(120.0, 200.0);
    opts.base_direction = BaseDirection::Auto;
    opts.align = Align::Left;

    let layout = layout_rich_text(&faces, &spans, opts, &budget).unwrap();
    assert!(layout.lines.len() >= 2);
    assert!(layout.fits());
    assert!(layout.lines.iter().all(|l| !l.is_rtl_paragraph));
    assert!(layout.lines.iter().all(|l| l.width <= 120.0 + 1e-5));
    // Rejoined lines match the original words.
    let rebuilt: Vec<String> = (0..layout.lines.len())
        .map(|i| line_logical_text(&layout, i))
        .collect();
    assert_eq!(rebuilt.join(" "), spans[0].text);
}

#[test]
fn rtl_hebrew_and_arabic_resolve_direction_and_visual_order() {
    let (_, he, ar, _) = load_faces();
    let faces = [FallbackSource::Embedded(&he), FallbackSource::Embedded(&ar)];
    let budget = Budget::default();

    // Hebrew paragraph resolves to RTL under Auto, visual order starts with final mem.
    let he_spans = [span("שָׁלוֹם עולם", 0, 14.0)];
    let mut opts = RichLayoutOptions::textbox(300.0, 100.0);
    opts.base_direction = BaseDirection::Auto;
    opts.align = Align::Start;

    let he_layout = layout_rich_text(&faces, &he_spans, opts, &budget).unwrap();
    assert_eq!(he_layout.lines.len(), 1);
    let line = &he_layout.lines[0];
    assert!(line.is_rtl_paragraph);
    // Align::Start on RTL right-aligns the line to box_width = 300.0.
    let last_run = line.runs.last().unwrap();
    assert!((last_run.x + last_run.width - 300.0).abs() < 1e-5);
    assert!(line.runs.iter().all(|r| r.rtl));
    assert_eq!(line_logical_text(&he_layout, 0), "שָׁלוֹם עולם");

    // Arabic wrapped paragraph resolves to RTL and wraps cleanly across multiple lines.
    let ar_text = "مرحبا بالعالم مرحبا بالعالم مرحبا بالعالم";
    let ar_spans = [span(ar_text, 1, 14.0)];
    let ar_opts = RichLayoutOptions {
        max_width: Some(110.0),
        max_height: 200.0,
        base_direction: BaseDirection::Auto,
        align: Align::Start,
        line_height: None,
    };
    let ar_layout = layout_rich_text(&faces, &ar_spans, ar_opts, &budget).unwrap();
    assert!(ar_layout.lines.len() >= 2);
    assert!(ar_layout.lines.iter().all(|l| l.is_rtl_paragraph));
    for (idx, line) in ar_layout.lines.iter().enumerate() {
        let last = line.runs.last().unwrap();
        assert!((last.x + last.width - 110.0).abs() < 1e-4, "line {idx}");
        assert!(line.runs.iter().all(|r| r.rtl));
    }
    let rebuilt: Vec<String> = (0..ar_layout.lines.len())
        .map(|i| line_logical_text(&ar_layout, i))
        .collect();
    assert_eq!(rebuilt.join(" "), ar_text);
}

#[test]
fn mixed_bidi_with_digits_respects_base_direction_override() {
    let (_, he, _, _) = load_faces();
    let helvetica = standard::font("Helvetica").unwrap();
    let faces = [
        FallbackSource::Standard(helvetica),
        FallbackSource::Embedded(&he),
    ];
    let budget = Budget::default();

    // Paragraph starts with digits followed by Hebrew.
    // Under Auto, first strong char is Hebrew -> RTL base direction; digits appear visually left.
    // Under Ltr, base direction is forced LTR -> digits appear visually left of Hebrew,
    // while Start aligns left instead of right!
    let spans = [span("2026 ", 0, 12.0), span("שלום", 1, 12.0)];

    let mut auto_opts = RichLayoutOptions::textbox(250.0, 100.0);
    auto_opts.base_direction = BaseDirection::Auto;
    auto_opts.align = Align::Start;
    let auto_layout = layout_rich_text(&faces, &spans, auto_opts, &budget).unwrap();
    assert!(auto_layout.lines[0].is_rtl_paragraph);
    // In RTL paragraph, UAX #9 puts "2026" at level 2 (LTR, visually rightmost),
    // while " " (span 0) and "שלום" (span 1) share level 1 (RTL), producing
    // three visual runs left-to-right: "שלום" (RTL), " " (RTL), "2026" (LTR).
    let auto_runs = &auto_layout.lines[0].runs;
    assert_eq!(auto_runs.len(), 3);
    assert!(
        auto_runs[0].rtl,
        "Hebrew run should be visually leftmost in RTL"
    );
    assert!(
        auto_runs[1].rtl,
        "space in span 0 joins RTL level 1 between Hebrew and digits"
    );
    assert!(
        !auto_runs[2].rtl,
        "digits run should be visually rightmost in RTL"
    );
    assert!(auto_runs[0].x > 50.0, "RTL Start aligns to right edge");

    let mut ltr_opts = RichLayoutOptions::textbox(250.0, 100.0);
    ltr_opts.base_direction = BaseDirection::Ltr;
    ltr_opts.align = Align::Start;
    let ltr_layout = layout_rich_text(&faces, &spans, ltr_opts, &budget).unwrap();
    assert!(!ltr_layout.lines[0].is_rtl_paragraph);
    let ltr_runs = &ltr_layout.lines[0].runs;
    assert_eq!(ltr_runs.len(), 2);
    assert!(
        !ltr_runs[0].rtl,
        "digits run should be visually left in LTR"
    );
    assert!(
        ltr_runs[1].rtl,
        "Hebrew run should be visually right in LTR"
    );
    assert!(ltr_runs[0].x.abs() < 1e-6, "LTR Start aligns to left edge");

    // Forced RTL on a Latin-first mixed line.
    let mut rtl_opts = RichLayoutOptions::textbox(250.0, 100.0);
    rtl_opts.base_direction = BaseDirection::Rtl;
    rtl_opts.align = Align::Start;
    let mixed = [span("Hello ", 0, 12.0), span("שלום", 1, 12.0)];
    let rtl_layout = layout_rich_text(&faces, &mixed, rtl_opts, &budget).unwrap();
    assert!(rtl_layout.lines[0].is_rtl_paragraph);
    let last = rtl_layout.lines[0].runs.last().unwrap();
    assert!((last.x + last.width - 250.0).abs() < 1e-5);
}

#[test]
fn cjk_latin_and_fallback_fonts_mix_on_one_line() {
    let (jp, he, _, _) = load_faces();
    let helvetica = standard::font("Helvetica").unwrap();
    let faces = [
        FallbackSource::Standard(helvetica),
        FallbackSource::Embedded(&jp),
        FallbackSource::Embedded(&he),
    ];
    let budget = Budget::default();

    // Single span using Standard 14 Helvetica primary + [JP, Hebrew] fallbacks!
    let spans = [SpanInput {
        text: "Hello 日本 שלום",
        face_index: 0,
        fallback_faces: &[1, 2],
        size: 12.0,
        underline: false,
        strikethrough: false,
        link_id: None,
    }];
    let layout = layout_rich_text(
        &faces,
        &spans,
        RichLayoutOptions::textbox(300.0, 100.0),
        &budget,
    )
    .unwrap();
    assert_eq!(layout.lines.len(), 1);
    let all_glyphs: Vec<_> = layout.lines[0]
        .runs
        .iter()
        .flat_map(|r| r.glyphs.iter())
        .collect();
    assert!(all_glyphs.iter().any(|g| g.face_index == 0));
    assert!(all_glyphs.iter().any(|g| g.face_index == 1));
    assert!(all_glyphs.iter().any(|g| g.face_index == 2));
    assert_eq!(line_logical_text(&layout, 0), "Hello 日本 שלום");
}

#[test]
fn standard14_without_fallback_refuses_non_winansi() {
    let helvetica = standard::font("Helvetica").unwrap();
    let faces = [FallbackSource::Standard(helvetica)];
    let budget = Budget::default();
    let spans = [span("Hello 日本", 0, 12.0)];
    let err = layout_rich_text(
        &faces,
        &spans,
        RichLayoutOptions::textbox(200.0, 100.0),
        &budget,
    )
    .unwrap_err();
    assert_eq!(err, RichLayoutError::NotWinAnsi('日'));
}

#[test]
fn missing_glyph_in_opentype_chain_refuses_cleanly() {
    let (jp, _, _, _) = load_faces();
    let faces = [FallbackSource::Embedded(&jp)];
    let budget = Budget::default();
    let spans = [span("日本🦀", 0, 12.0)];
    let err = layout_rich_text(
        &faces,
        &spans,
        RichLayoutOptions::textbox(200.0, 100.0),
        &budget,
    )
    .unwrap_err();
    assert_eq!(err, RichLayoutError::MissingGlyph('🦀'));
}

#[test]
fn alignments_left_center_right_start_end_justify() {
    let helvetica = standard::font("Helvetica").unwrap();
    let (_, he, _, _) = load_faces();
    let faces = [
        FallbackSource::Standard(helvetica),
        FallbackSource::Embedded(&he),
    ];
    let budget = Budget::default();
    let box_w = 200.0;

    // Single-line LTR across Left, Center, Right, Start, End.
    let ltr_spans = [span("Hello world", 0, 12.0)];
    let mut shifts = Vec::new();
    for align in [
        Align::Left,
        Align::Center,
        Align::Right,
        Align::Start,
        Align::End,
    ] {
        let mut opts = RichLayoutOptions::textbox(box_w, 100.0);
        opts.align = align;
        let layout = layout_rich_text(&faces, &ltr_spans, opts, &budget).unwrap();
        let line = &layout.lines[0];
        shifts.push((align, line.runs[0].x, line.width));
    }
    let w = shifts[0].2;
    assert!(shifts[0].1.abs() < 1e-6); // Left
    assert!((shifts[1].1 - (box_w - w) * 0.5).abs() < 1e-6); // Center
    assert!((shifts[2].1 - (box_w - w)).abs() < 1e-6); // Right
    assert!(shifts[3].1.abs() < 1e-6); // Start (LTR == Left)
    assert!((shifts[4].1 - (box_w - w)).abs() < 1e-6); // End (LTR == Right)

    // Single-line RTL: Start == Right, End == Left.
    let rtl_spans = [span("שלום עולם", 1, 12.0)];
    let mut start_opts = RichLayoutOptions::textbox(box_w, 100.0);
    start_opts.align = Align::Start;
    let start_layout = layout_rich_text(&faces, &rtl_spans, start_opts, &budget).unwrap();
    let mut end_opts = RichLayoutOptions::textbox(box_w, 100.0);
    end_opts.align = Align::End;
    let end_layout = layout_rich_text(&faces, &rtl_spans, end_opts, &budget).unwrap();
    let rw = start_layout.lines[0].width;
    assert!((start_layout.lines[0].runs[0].x - (box_w - rw)).abs() < 1e-5);
    assert!(end_layout.lines[0].runs[0].x.abs() < 1e-5);

    // Justify stretches soft-wrapped lines to box_w, aligns last line to Start.
    let para_spans = [span(
        "alpha beta gamma delta epsilon zeta eta theta",
        0,
        12.0,
    )];
    let mut just_opts = RichLayoutOptions::textbox(120.0, 200.0);
    just_opts.align = Align::Justify;
    let just_layout = layout_rich_text(&faces, &para_spans, just_opts, &budget).unwrap();
    assert!(just_layout.lines.len() >= 2);
    for line in &just_layout.lines[..just_layout.lines.len() - 1] {
        assert!(line.justified);
        assert!((line.width - 120.0).abs() < 1e-5);
        let end_x = line.runs.last().map_or(0.0, |r| r.x + r.width);
        assert!((end_x - 120.0).abs() < 1e-5);
    }
    let last_line = just_layout.lines.last().unwrap();
    assert!(!last_line.justified);
    assert!(last_line.runs[0].x.abs() < 1e-6);
}

#[test]
fn mixed_font_sizes_decorations_and_link_hitboxes() {
    let (jp, he, _, _) = load_faces();
    let helvetica = standard::font("Helvetica").unwrap();
    let faces = [
        FallbackSource::Standard(helvetica),
        FallbackSource::Embedded(&jp),
        FallbackSource::Embedded(&he),
    ];
    let budget = Budget::default();

    let spans = [
        SpanInput {
            text: "Small ",
            face_index: 0,
            fallback_faces: &[],
            size: 10.0,
            underline: false,
            strikethrough: true,
            link_id: None,
        },
        SpanInput {
            text: "AVA ",
            face_index: 1,
            fallback_faces: &[],
            size: 24.0,
            underline: true,
            strikethrough: false,
            link_id: Some(42),
        },
        SpanInput {
            text: "שלום",
            face_index: 2,
            fallback_faces: &[],
            size: 14.0,
            underline: true,
            strikethrough: true,
            link_id: Some(99),
        },
    ];
    let layout = layout_rich_text(
        &faces,
        &spans,
        RichLayoutOptions::textbox(400.0, 200.0),
        &budget,
    )
    .unwrap();
    assert_eq!(layout.lines.len(), 1);
    let line = &layout.lines[0];
    assert_eq!(line.runs.len(), 3);
    // Shared baseline determined by the 24pt span's ascent.
    assert!(line.ascent > 18.0);
    assert!((line.baseline_y - line.ascent).abs() < 1e-6);

    // Decorations: 1 strikethrough (span 0) + 1 underline (span 1) + 2 both (span 2) = 4 rules.
    assert_eq!(layout.decorations.len(), 4);
    let strike_0 = layout
        .decorations
        .iter()
        .find(|d| d.span_index == 0 && d.is_strikethrough)
        .unwrap();
    assert!(
        strike_0.y < line.baseline_y,
        "strikethrough sits above baseline"
    );
    assert!(strike_0.thickness > 0.0);
    let under_1 = layout
        .decorations
        .iter()
        .find(|d| d.span_index == 1 && !d.is_strikethrough)
        .unwrap();
    assert!(under_1.y > line.baseline_y, "underline sits below baseline");
    assert!(under_1.thickness > 0.0);

    // Link hit-boxes: one for link 42 (BIG) and one for link 99 (שלום).
    assert_eq!(layout.links.len(), 2);
    let link42 = layout.links.iter().find(|l| l.link_id == 42).unwrap();
    assert!(link42.x1 > link42.x0);
    assert!(link42.y0 < line.baseline_y && link42.y1 > line.baseline_y);
    let link99 = layout.links.iter().find(|l| l.link_id == 99).unwrap();
    assert!(link99.x0 >= link42.x1 - 1e-6);
    assert!(link99.x1 > link99.x0);
}

#[test]
fn explicit_newlines_preserve_blank_lines_and_unwrapped_mode() {
    let helvetica = standard::font("Helvetica").unwrap();
    let faces = [FallbackSource::Standard(helvetica)];
    let budget = Budget::default();

    let spans = [span("First\n\nThird", 0, 12.0)];
    let layout = layout_rich_text(&faces, &spans, RichLayoutOptions::unwrapped(), &budget).unwrap();
    assert_eq!(layout.lines.len(), 3);
    assert_eq!(line_logical_text(&layout, 0), "First");
    assert!(layout.lines[1].runs.is_empty());
    assert_eq!(line_logical_text(&layout, 2), "Third");
    assert!(layout.lines[1].baseline_y > layout.lines[0].baseline_y);
    assert!(layout.lines[2].baseline_y > layout.lines[1].baseline_y);
}

#[test]
fn emergency_grapheme_break_and_narrow_refusal() {
    let helvetica = standard::font("Helvetica").unwrap();
    let faces = [FallbackSource::Standard(helvetica)];
    let budget = Budget::default();

    // Narrow box wider than one char (12pt), narrower than the whole word.
    let spans = [span("Supercalifragilistic", 0, 12.0)];
    let layout = layout_rich_text(
        &faces,
        &spans,
        RichLayoutOptions::textbox(25.0, 500.0),
        &budget,
    )
    .unwrap();
    assert!(layout.lines.len() >= 4);
    let rebuilt: String = (0..layout.lines.len())
        .map(|i| line_logical_text(&layout, i))
        .collect();
    assert_eq!(rebuilt, "Supercalifragilistic");

    // Box narrower than a single character refuses with Narrow.
    let err = layout_rich_text(
        &faces,
        &spans,
        RichLayoutOptions::textbox(1.0, 500.0),
        &budget,
    )
    .unwrap_err();
    assert_eq!(err, RichLayoutError::Narrow);
}

#[test]
fn deterministic_f6_budget_refusals() {
    let (jp, _, _, _) = load_faces();
    let faces = [FallbackSource::Embedded(&jp)];
    let spans = [span(
        "office fit AVA office fit AVA office fit AVA",
        0,
        12.0,
    )];
    let opts = RichLayoutOptions::textbox(80.0, 400.0);

    // 1. ShapedGlyphCount
    let glyph_budget = Budget::default().with_limit(LimitCode::ShapedGlyphCount, Some(5));
    let err = layout_rich_text(&faces, &spans, opts, &glyph_budget).unwrap_err();
    assert!(matches!(
        err,
        RichLayoutError::Limit(limit) if limit.code() == LimitCode::ShapedGlyphCount
    ));

    // 2. TextLineCount
    let line_budget = Budget::default().with_limit(LimitCode::TextLineCount, Some(1));
    let err = layout_rich_text(&faces, &spans, opts, &line_budget).unwrap_err();
    assert!(matches!(
        err,
        RichLayoutError::Limit(limit) if limit.code() == LimitCode::TextLineCount
    ));

    // 3. MemorySize
    let mem_budget = Budget::default().with_limit(LimitCode::MemorySize, Some(32));
    let err = layout_rich_text(&faces, &spans, opts, &mem_budget).unwrap_err();
    assert!(matches!(
        err,
        RichLayoutError::Limit(limit) if limit.code() == LimitCode::MemorySize
    ));

    // 4. TextInputSize
    let input_budget = Budget::default().with_limit(LimitCode::TextInputSize, Some(10));
    let err = layout_rich_text(&faces, &spans, opts, &input_budget).unwrap_err();
    assert!(matches!(
        err,
        RichLayoutError::Limit(limit) if limit.code() == LimitCode::TextInputSize
    ));

    // 5. Cancellation
    let token = CancellationToken::new();
    token.cancel();
    let cancel_budget = Budget::default().with_cancellation(token);
    let err = layout_rich_text(&faces, &spans, opts, &cancel_budget).unwrap_err();
    assert!(matches!(
        err,
        RichLayoutError::Limit(limit) if limit.code() == LimitCode::Cancelled
    ));
}

#[test]
fn invalid_geometry_refusals() {
    let helvetica = standard::font("Helvetica").unwrap();
    let faces = [FallbackSource::Standard(helvetica)];
    let budget = Budget::default();
    let spans = [span("Hello", 0, 12.0)];

    assert_eq!(
        layout_rich_text(
            &faces,
            &spans,
            RichLayoutOptions::textbox(0.0, 100.0),
            &budget
        )
        .unwrap_err(),
        RichLayoutError::InvalidGeometry
    );
    assert_eq!(
        layout_rich_text(
            &faces,
            &spans,
            RichLayoutOptions::textbox(f64::NAN, 100.0),
            &budget
        )
        .unwrap_err(),
        RichLayoutError::InvalidGeometry
    );
    assert_eq!(
        layout_rich_text(
            &faces,
            &spans,
            RichLayoutOptions::textbox(100.0, -1.0),
            &budget
        )
        .unwrap_err(),
        RichLayoutError::InvalidGeometry
    );
    let bad_span = [span("Hello", 0, 0.0)];
    assert_eq!(
        layout_rich_text(
            &faces,
            &bad_span,
            RichLayoutOptions::textbox(100.0, 100.0),
            &budget
        )
        .unwrap_err(),
        RichLayoutError::InvalidGeometry
    );
    let bad_face = [span("Hello", 99, 12.0)];
    assert_eq!(
        layout_rich_text(
            &faces,
            &bad_face,
            RichLayoutOptions::textbox(100.0, 100.0),
            &budget
        )
        .unwrap_err(),
        RichLayoutError::InvalidGeometry
    );
}

#[test]
fn variable_font_instance_works_in_rich_layout() {
    let (_, _, _, latin_var) = load_faces();
    let budget = Budget::default();
    let bold_instance = latin_var.at_weight(700, &budget).unwrap();
    let faces = [
        FallbackSource::Embedded(&latin_var),
        FallbackSource::Embedded(&bold_instance),
    ];
    let spans = [span("AV ", 0, 14.0), span("AV", 1, 14.0)];
    let layout = layout_rich_text(
        &faces,
        &spans,
        RichLayoutOptions::textbox(200.0, 100.0),
        &budget,
    )
    .unwrap();
    assert_eq!(layout.lines.len(), 1);
    assert_eq!(layout.lines[0].runs.len(), 2);
    assert!(layout.lines[0].width > 0.0);
}

#[test]
fn rtl_justify_stretches_wrapped_lines_and_right_aligns_last_line() {
    let (_, _, ar, _) = load_faces();
    let faces = [FallbackSource::Embedded(&ar)];
    let budget = Budget::default();
    let ar_text = "مرحبا بالعالم مرحبا بالعالم مرحبا بالعالم مرحبا";
    let spans = [span(ar_text, 0, 13.0)];
    let opts = RichLayoutOptions {
        max_width: Some(120.0),
        max_height: 300.0,
        base_direction: BaseDirection::Auto,
        align: Align::Justify,
        line_height: None,
    };
    let layout = layout_rich_text(&faces, &spans, opts, &budget).unwrap();
    assert!(layout.lines.len() >= 2);
    for line in &layout.lines[..layout.lines.len() - 1] {
        assert!(line.is_rtl_paragraph);
        assert!(line.justified);
        assert!((line.width - 120.0).abs() < 1e-4);
        assert!(line.runs[0].x.abs() < 1e-5);
        let end_x = line.runs.last().map_or(0.0, |r| r.x + r.width);
        assert!((end_x - 120.0).abs() < 1e-4);
    }
    let last = layout.lines.last().unwrap();
    assert!(!last.justified);
    let last_end = last.runs.last().map_or(0.0, |r| r.x + r.width);
    assert!(
        (last_end - 120.0).abs() < 1e-4,
        "last line of RTL justified paragraph aligns to physical right (Start)"
    );
}

#[test]
fn link_and_decoration_split_across_line_wrap_and_bidi_runs() {
    let (_, he, _, _) = load_faces();
    let helvetica = standard::font("Helvetica").unwrap();
    let faces = [
        FallbackSource::Standard(helvetica),
        FallbackSource::Embedded(&he),
    ];
    let budget = Budget::default();
    let spans = [SpanInput {
        text: "click here שלום click here again",
        face_index: 0,
        fallback_faces: &[1],
        size: 12.0,
        underline: true,
        strikethrough: false,
        link_id: Some(7),
    }];
    let layout = layout_rich_text(
        &faces,
        &spans,
        RichLayoutOptions::textbox(95.0, 200.0),
        &budget,
    )
    .unwrap();
    assert!(layout.lines.len() >= 2);
    let run_count: usize = layout.lines.iter().map(|l| l.runs.len()).sum();
    assert!(run_count >= 3);
    assert_eq!(layout.links.len(), run_count);
    assert_eq!(layout.decorations.len(), run_count);
    assert!(layout.links.iter().all(|l| l.link_id == 7));
}

#[test]
fn single_pass_caching_avoids_double_shaping_under_tight_glyph_budget() {
    let (_, he, _, _) = load_faces();
    let helvetica = standard::font("Helvetica").unwrap();
    let faces = [
        FallbackSource::Standard(helvetica),
        FallbackSource::Embedded(&he),
    ];
    let text_ltr = "alpha beta gamma delta epsilon";
    let spans_ltr = [span(text_ltr, 0, 12.0)];
    let exact_chars = text_ltr.chars().count() as u64;
    let tight_budget = Budget::default().with_limit(LimitCode::ShapedGlyphCount, Some(exact_chars));
    let layout = layout_rich_text(
        &faces,
        &spans_ltr,
        RichLayoutOptions::textbox(70.0, 300.0),
        &tight_budget,
    )
    .unwrap();
    assert!(layout.lines.len() >= 3);

    let text_rtl = "שלום עולם שלום עולם שלום עולם";
    let spans_rtl = [span(text_rtl, 1, 12.0)];
    let rtl_chars = text_rtl.chars().count() as u64;
    let tight_rtl_budget =
        Budget::default().with_limit(LimitCode::ShapedGlyphCount, Some(rtl_chars));
    let rtl_layout = layout_rich_text(
        &faces,
        &spans_rtl,
        RichLayoutOptions::textbox(70.0, 300.0),
        &tight_rtl_budget,
    )
    .unwrap();
    assert!(rtl_layout.lines.len() >= 2);
}

const PROPTEST_ATOMS: &[&str] = &[
    "A",
    "V",
    "fi",
    "office",
    "fit",
    " ",
    "  ",
    "123",
    "\n",
    "日",
    "本",
    "語",
    "の",
    "かな。",
    "שָׁלוֹם",
    "שלום",
    "עולם",
    "مرحبا",
    "بالعالم",
    "\u{200E}",
    "\u{200F}",
];

const PROPTEST_RAW_CHARS: &[char] = &[
    'A', 'V', 'f', 'i', ' ', '1', '2', '\n', '日', '本', 'ש', 'ל', 'ו', 'ם', '\u{05B8}',
    '\u{05C1}', 'م', 'ر', 'ح', 'ب', 'ا', '\u{200E}', '\u{200F}',
];

fn assert_layout_invariants(layout: &RichLayout) -> Result<(), TestCaseError> {
    // 1. Baselines strictly increase from line to line.
    let mut prev_y = -1.0_f64;
    for line in &layout.lines {
        prop_assert!(line.baseline_y.is_finite());
        prop_assert!(line.baseline_y > prev_y);
        prev_y = line.baseline_y;

        // 2. Monotone non-decreasing visual x coordinates across runs and glyphs.
        let mut pen_x = 0.0_f64;
        let mut run_ranges = Vec::new();
        let mut glyph_text_ranges = Vec::new();
        for (r_idx, run) in line.runs.iter().enumerate() {
            prop_assert!(run.x.is_finite() && run.width.is_finite());
            if r_idx > 0 {
                prop_assert!(run.x + 1e-6 >= pen_x);
            } else {
                prop_assert!(run.x >= -1e-6);
            }
            prop_assert!(layout.text.is_char_boundary(run.text_range.start));
            prop_assert!(layout.text.is_char_boundary(run.text_range.end));
            prop_assert!(run.text_range.start <= run.text_range.end);
            run_ranges.push(run.text_range.clone());

            let mut g_pen = run.x;
            for glyph in &run.glyphs {
                prop_assert!(glyph.x_advance.is_finite());
                prop_assert!(glyph.x_advance >= -1e-6);
                g_pen += glyph.x_advance;
                prop_assert!(layout.text.is_char_boundary(glyph.cluster.start));
                prop_assert!(layout.text.is_char_boundary(glyph.cluster.end));
                prop_assert!(layout.text.is_char_boundary(glyph.text.start));
                prop_assert!(layout.text.is_char_boundary(glyph.text.end));
                prop_assert!(run.text_range.start <= glyph.cluster.start);
                prop_assert!(glyph.cluster.start <= glyph.text.start);
                prop_assert!(glyph.text.start <= glyph.text.end);
                prop_assert!(glyph.text.end <= glyph.cluster.end);
                prop_assert!(glyph.cluster.end <= run.text_range.end);
                if !glyph.text.is_empty() {
                    glyph_text_ranges.push(glyph.text.clone());
                }
            }
            pen_x = run.x + run.width;
            prop_assert!((g_pen - pen_x).abs() < 1e-5);
        }

        // 3. Exact UTF-8 byte-range coverage of each line's runs without overlaps.
        run_ranges.sort_by_key(|r| r.start);
        for pair in run_ranges.windows(2) {
            prop_assert!(pair[0].end <= pair[1].start);
        }
        glyph_text_ranges.sort_by_key(|r| r.start);
        for pair in glyph_text_ranges.windows(2) {
            prop_assert!(pair[0].end <= pair[1].start);
        }
        let run_bytes: usize = run_ranges.iter().map(|r| r.len()).sum();
        let glyph_bytes: usize = glyph_text_ranges.iter().map(|r| r.len()).sum();
        if line.runs.iter().all(|r| !r.glyphs.is_empty()) {
            prop_assert_eq!(glyph_bytes, run_bytes);
        }
    }

    // 4. Decorations and links have valid finite positive extents.
    for dec in &layout.decorations {
        prop_assert!(dec.x1 > dec.x0 && dec.thickness > 0.0 && dec.y.is_finite());
    }
    for link in &layout.links {
        prop_assert!(link.x1 > link.x0 && link.y1 > link.y0);
    }
    Ok(())
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(96))]

    #[test]
    fn proptest_multilingual_invariants(
        tokens in proptest::collection::vec(
            (proptest::collection::vec(0..PROPTEST_ATOMS.len(), 0..6), 0..3_usize, 8..22_u32, any::<bool>(), any::<bool>(), any::<bool>()),
            1..6,
        ),
        box_width in 30.0..260.0_f64,
        dir_pick in 0..3_usize,
        align_pick in 0..6_usize,
    ) {
        let (jp, he, ar, _) = load_faces();
        let faces = [
            FallbackSource::Embedded(&jp),
            FallbackSource::Embedded(&he),
            FallbackSource::Embedded(&ar),
        ];
        let fallback_all = [0usize, 1, 2];
        let texts: Vec<String> = tokens
            .iter()
            .map(|(idxs, ..)| idxs.iter().map(|&i| PROPTEST_ATOMS[i]).collect())
            .collect();
        let spans: Vec<SpanInput<'_>> = tokens
            .iter()
            .zip(&texts)
            .map(|((_, face_idx, size, underline, strike, has_link), text)| SpanInput {
                text: text.as_str(),
                face_index: *face_idx,
                fallback_faces: &fallback_all,
                size: f64::from(*size),
                underline: *underline,
                strikethrough: *strike,
                link_id: if *has_link { Some(1) } else { None },
            })
            .collect();

        let base_direction = [BaseDirection::Auto, BaseDirection::Ltr, BaseDirection::Rtl][dir_pick];
        let align = [
            Align::Left,
            Align::Center,
            Align::Right,
            Align::Justify,
            Align::Start,
            Align::End,
        ][align_pick];
        let opts = RichLayoutOptions {
            max_width: Some(box_width),
            max_height: 1000.0,
            base_direction,
            align,
            line_height: None,
        };
        let budget = Budget::default();

        match layout_rich_text(&faces, &spans, opts, &budget) {
            Err(RichLayoutError::Narrow) => {}
            Err(other) => prop_assert!(false, "unexpected layout error: {other:?}"),
            Ok(layout) => assert_layout_invariants(&layout)?,
        }
    }

    #[test]
    fn proptest_arbitrary_combining_and_narrow_never_panics(
        idxs in proptest::collection::vec(0..PROPTEST_RAW_CHARS.len(), 0..24),
        box_width in 5.0..120.0_f64,
        dir_pick in 0..3_usize,
    ) {
        let (jp, he, ar, _) = load_faces();
        let faces = [
            FallbackSource::Embedded(&jp),
            FallbackSource::Embedded(&he),
            FallbackSource::Embedded(&ar),
        ];
        let fallback_all = [0usize, 1, 2];
        let text: String = idxs.into_iter().map(|i| PROPTEST_RAW_CHARS[i]).collect();
        let spans = [SpanInput {
            text: &text,
            face_index: 0,
            fallback_faces: &fallback_all,
            size: 12.0,
            underline: true,
            strikethrough: true,
            link_id: Some(7),
        }];
        let opts = RichLayoutOptions {
            max_width: Some(box_width),
            max_height: 500.0,
            base_direction: [BaseDirection::Auto, BaseDirection::Ltr, BaseDirection::Rtl][dir_pick],
            align: Align::Justify,
            line_height: None,
        };
        let budget = Budget::default();
        match layout_rich_text(&faces, &spans, opts, &budget) {
            Ok(layout) => assert_layout_invariants(&layout)?,
            Err(RichLayoutError::Narrow | RichLayoutError::MissingGlyph(_)) => {}
            Err(other) => prop_assert!(false, "unexpected layout error: {other:?}"),
        }
    }
}
