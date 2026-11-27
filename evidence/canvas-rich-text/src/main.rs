//! Side-by-side benchmark and correctness harness for issue #1198:
//! Option A (`parley 0.13.0`, minimal `std` feature set) vs
//! Option B (bounded `harfrust 0.16` + `skrifa 0.50` + `unicode-bidi 0.3.18`
//! + `unicode-linebreak` + `unicode-segmentation` + `BudgetVec`).
//!
//! Uses only the repository's committed test fonts under
//! `crates/pyropdf-engine/src/fonts/testdata/` so the benchmark reproduces
//! identically without system fonts.

mod bidi_rich;

use std::fmt::Write as _;
use std::path::Path;
use std::sync::Arc;
use std::time::Instant;

use bidi_rich::{Align, BaseDirection, RichFace, RichLayoutError, SpanInput, layout_rich_text};
use parley::{
    Alignment as ParleyAlign, AlignmentOptions, BaseDirection as ParleyDir, FontContext,
    FontFamily, LayoutContext, PositionedLayoutItem, StyleProperty,
};
use pyropdf_store::budget::{Budget, LimitCode};

const JP_FONT: &[u8] =
    include_bytes!("../../../crates/pyropdf-engine/src/fonts/testdata/NotoSansJP-subset.otf");
const HEBREW_FONT: &[u8] =
    include_bytes!("../../../crates/pyropdf-engine/src/fonts/testdata/NotoSansHebrew-subset.ttf");
const ARABIC_FONT: &[u8] = include_bytes!(
    "../../../crates/pyropdf-engine/src/fonts/testdata/NotoSansArabic-Variable-subset.ttf"
);
const LATIN_FONT: &[u8] =
    include_bytes!("../../../crates/pyropdf-engine/src/fonts/testdata/variable-latin.ttf");

type SpanDef = (&'static str, usize, f64, bool, bool, Option<u32>);

struct BenchCase {
    name: &'static str,
    width: f64,
    direction: BaseDirection,
    align: Align,
    spans: Vec<SpanDef>,
}

fn build_cases() -> Vec<BenchCase> {
    let long_latin = "office fit AVA ".repeat(80);
    let long_arabic = "مرحبا بالعالم ".repeat(50);
    // Leak for static slice convenience inside the benchmark harness.
    let long_latin: &'static str = Box::leak(long_latin.into_boxed_str());
    let long_arabic: &'static str = Box::leak(long_arabic.into_boxed_str());

    vec![
        BenchCase {
            name: "styled-latin-justified",
            width: 180.0,
            direction: BaseDirection::Ltr,
            align: Align::Justify,
            spans: vec![
                ("office ", 0, 12.0, false, false, None),
                ("fit ", 3, 14.0, true, false, Some(1)),
                (
                    "AVA office fit AVA office fit AVA",
                    0,
                    11.0,
                    false,
                    true,
                    None,
                ),
            ],
        },
        BenchCase {
            name: "bidi-ltr-with-hebrew-and-digits",
            width: 220.0,
            direction: BaseDirection::Ltr,
            align: Align::Start,
            spans: vec![
                ("AVA ", 0, 12.0, false, false, None),
                ("שָׁלוֹם עולם", 1, 12.0, true, false, Some(2)),
                (" 123 office", 0, 12.0, false, false, None),
            ],
        },
        BenchCase {
            name: "bidi-rtl-arabic-wrapped",
            width: 110.0,
            direction: BaseDirection::Rtl,
            align: Align::Start,
            spans: vec![
                ("مرحبا بالعالم ", 2, 14.0, false, false, None),
                ("AVA ", 0, 12.0, true, false, Some(3)),
                ("مرحبا بالعالم", 2, 14.0, false, false, None),
            ],
        },
        BenchCase {
            name: "multilingual-cjk-hebrew-arabic",
            width: 160.0,
            direction: BaseDirection::Auto,
            align: Align::Left,
            spans: vec![
                ("日本語のテキスト、かな。", 0, 11.0, false, false, None),
                (" office fit ", 3, 12.0, false, false, Some(4)),
                ("שלום ", 1, 12.0, true, false, None),
                ("مرحبا", 2, 12.0, false, true, None),
            ],
        },
        BenchCase {
            name: "long-bidi-500-tokens",
            width: 240.0,
            direction: BaseDirection::Auto,
            align: Align::Justify,
            spans: vec![
                (long_latin, 0, 10.0, false, false, None),
                (long_arabic, 2, 11.0, false, false, None),
            ],
        },
    ]
}

fn run_parley(
    font_cx: &mut FontContext,
    layout_cx: &mut LayoutContext<u32>,
    family_names: &[String],
    case: &BenchCase,
) -> (usize, usize, f32) {
    let mut full = String::new();
    let mut ranges = Vec::with_capacity(case.spans.len());
    for &(txt, face_idx, size, underline, strike, _) in &case.spans {
        let start = full.len();
        full.push_str(txt);
        ranges.push((start..full.len(), face_idx, size, underline, strike));
    }
    let mut builder = layout_cx.ranged_builder(font_cx, &full, 1.0, false);
    builder.set_base_direction(match case.direction {
        BaseDirection::Auto => ParleyDir::Auto,
        BaseDirection::Ltr => ParleyDir::Ltr,
        BaseDirection::Rtl => ParleyDir::Rtl,
    });
    for (range, face_idx, size, underline, strike) in ranges {
        builder.push(StyleProperty::FontSize(size as f32), range.clone());
        builder.push(
            StyleProperty::FontFamily(FontFamily::named(&family_names[face_idx])),
            range.clone(),
        );
        if underline {
            builder.push(StyleProperty::Underline(true), range.clone());
        }
        if strike {
            builder.push(StyleProperty::Strikethrough(true), range);
        }
    }
    let mut layout = builder.build(&full);
    layout.break_all_lines(Some(case.width as f32));
    layout.set_alignment(
        match case.align {
            Align::Left => ParleyAlign::Left,
            Align::Right => ParleyAlign::Right,
            Align::Center => ParleyAlign::Center,
            Align::Justify => ParleyAlign::Justify,
            Align::Start => ParleyAlign::Start,
            Align::End => ParleyAlign::End,
        },
        AlignmentOptions::default(),
    );
    let mut glyphs = 0usize;
    for line in layout.lines() {
        for item in line.items() {
            if let PositionedLayoutItem::GlyphRun(run) = item {
                glyphs += run.glyphs().count();
            }
        }
    }
    (layout.len(), glyphs, layout.height())
}

fn main() {
    let out_dir = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "evidence/canvas-rich-text/results".to_owned());
    let iters: usize = std::env::var("ITERS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(50);

    // Prepare Option B faces.
    let faces = vec![
        RichFace::from_opentype(Arc::new(JP_FONT.to_vec()), 0).expect("JP"),
        RichFace::from_opentype(Arc::new(HEBREW_FONT.to_vec()), 0).expect("Hebrew"),
        RichFace::from_opentype(Arc::new(ARABIC_FONT.to_vec()), 0).expect("Arabic"),
        RichFace::from_opentype(Arc::new(LATIN_FONT.to_vec()), 0).expect("Latin"),
    ];

    // Prepare Option A (parley) FontContext without system fonts.
    let mut font_cx = FontContext::new();
    let mut family_names = Vec::new();
    for bytes in [JP_FONT, HEBREW_FONT, ARABIC_FONT, LATIN_FONT] {
        let reg = font_cx
            .collection
            .register_fonts(bytes.to_vec().into(), None);
        let name = reg
            .first()
            .and_then(|(id, _)| font_cx.collection.family_name(*id))
            .unwrap_or("sans-serif")
            .to_owned();
        family_names.push(name);
    }
    let mut layout_cx: LayoutContext<u32> = LayoutContext::new();

    let cases = build_cases();

    let mut md = String::new();
    let _ = writeln!(
        md,
        "# Rich-text + BiDi Layout Prototype Benchmark (`macos-arm64`, {iters} runs, best)\n"
    );
    let _ = writeln!(
        md,
        "| Case | Option A `parley` (ms) | A lines / glyphs | Option B `harfrust+bidi` (ms) | B lines / glyphs | B peak F6 Budget (KiB) | B rules / links |"
    );
    let _ = writeln!(md, "| --- | ---: | ---: | ---: | ---: | ---: | ---: |");

    let mut json_rows = Vec::new();
    for case in &cases {
        let spans: Vec<SpanInput<'_>> = case
            .spans
            .iter()
            .map(
                |&(text, face_index, size, underline, strikethrough, link_id)| SpanInput {
                    text,
                    face_index,
                    size,
                    underline,
                    strikethrough,
                    link_id,
                },
            )
            .collect();

        // Warmup + best-of-N for Option A (parley, reusing FontContext & LayoutContext).
        let mut a_best_ms = f64::INFINITY;
        let mut a_res = (0, 0, 0.0f32);
        for _ in 0..iters {
            let t0 = Instant::now();
            a_res = run_parley(&mut font_cx, &mut layout_cx, &family_names, case);
            a_best_ms = a_best_ms.min(t0.elapsed().as_secs_f64() * 1e3);
        }

        // Warmup + best-of-N for Option B (fresh Budget per call).
        let mut b_best_ms = f64::INFINITY;
        let mut b_lines = 0usize;
        let mut b_glyphs = 0usize;
        let mut b_peak_bytes = 0u64;
        let mut b_rules = 0usize;
        let mut b_links = 0usize;
        for _ in 0..iters {
            let budget = Budget::default();
            let t0 = Instant::now();
            let res = layout_rich_text(
                &faces,
                &spans,
                (case.width, 2000.0),
                case.direction,
                case.align,
                None,
                &budget,
            )
            .expect("Option B layout must succeed");
            b_best_ms = b_best_ms.min(t0.elapsed().as_secs_f64() * 1e3);
            b_lines = res.lines.len();
            b_glyphs = res.total_glyphs;
            b_peak_bytes = b_peak_bytes.max(res.peak_budget_bytes);
            b_rules = res.decorations.len();
            b_links = res.links.len();
        }

        let _ = writeln!(
            md,
            "| `{}` | {:.3} | {} / {} | {:.3} | {} / {} | {:.1} | {} / {} |",
            case.name,
            a_best_ms,
            a_res.0,
            a_res.1,
            b_best_ms,
            b_lines,
            b_glyphs,
            b_peak_bytes as f64 / 1024.0,
            b_rules,
            b_links,
        );
        json_rows.push(format!(
            "  {{\"case\": \"{}\", \"parley_ms\": {:.4}, \"parley_lines\": {}, \"parley_glyphs\": {}, \"option_b_ms\": {:.4}, \"option_b_lines\": {}, \"option_b_glyphs\": {}, \"option_b_budget_peak_bytes\": {}, \"option_b_decorations\": {}, \"option_b_links\": {}}}",
            case.name,
            a_best_ms,
            a_res.0,
            a_res.1,
            b_best_ms,
            b_lines,
            b_glyphs,
            b_peak_bytes,
            b_rules,
            b_links,
        ));
    }

    // Verify F6 budget refusals on Option B (glyph count, line count, memory).
    let glyph_budget = Budget::default().with_limit(LimitCode::ShapedGlyphCount, Some(10));
    let glyph_refused = matches!(
        layout_rich_text(
            &faces,
            &[SpanInput {
                text: "office fit AVA office fit",
                face_index: 0,
                size: 12.0,
                underline: false,
                strikethrough: false,
                link_id: None,
            }],
            (200.0, 200.0),
            BaseDirection::Ltr,
            Align::Left,
            None,
            &glyph_budget,
        ),
        Err(RichLayoutError::Limit(ref e)) if e.code() == LimitCode::ShapedGlyphCount
    );
    let line_budget = Budget::default().with_limit(LimitCode::TextLineCount, Some(2));
    let line_refused = matches!(
        layout_rich_text(
            &faces,
            &[SpanInput {
                text: "office fit AVA office fit AVA office fit AVA",
                face_index: 0,
                size: 12.0,
                underline: false,
                strikethrough: false,
                link_id: None,
            }],
            (40.0, 400.0),
            BaseDirection::Ltr,
            Align::Left,
            None,
            &line_budget,
        ),
        Err(RichLayoutError::Limit(ref e)) if e.code() == LimitCode::TextLineCount
    );
    let mem_budget = Budget::default().with_limit(LimitCode::MemorySize, Some(256));
    let mem_refused = matches!(
        layout_rich_text(
            &faces,
            &[SpanInput {
                text: "office fit AVA ".repeat(40).as_str(),
                face_index: 0,
                size: 12.0,
                underline: false,
                strikethrough: false,
                link_id: None,
            }],
            (200.0, 400.0),
            BaseDirection::Ltr,
            Align::Left,
            None,
            &mem_budget,
        ),
        Err(RichLayoutError::Limit(ref e)) if e.code() == LimitCode::MemorySize
    );
    assert!(
        glyph_refused && line_refused && mem_refused,
        "F6 budget checks failed"
    );

    // Verify Option B also handles Standard 14 mixed with OpenType (which parley cannot do).
    let mixed_faces = vec![
        RichFace::standard(pdf_base14_metrics::Base14Font::Helvetica),
        RichFace::from_opentype(Arc::new(HEBREW_FONT.to_vec()), 0).expect("Hebrew"),
    ];
    let mixed_res = layout_rich_text(
        &mixed_faces,
        &[
            SpanInput {
                text: "Invoice #42: ",
                face_index: 0,
                size: 12.0,
                underline: false,
                strikethrough: false,
                link_id: None,
            },
            SpanInput {
                text: "שָׁלוֹם",
                face_index: 1,
                size: 12.0,
                underline: true,
                strikethrough: false,
                link_id: Some(99),
            },
        ],
        (300.0, 100.0),
        BaseDirection::Ltr,
        Align::Start,
        None,
        &Budget::default(),
    )
    .expect("mixed Standard14 + OpenType layout");
    assert_eq!(mixed_res.lines.len(), 1);
    assert_eq!(mixed_res.lines[0].runs.len(), 2);
    assert_eq!(mixed_res.links.len(), 1);

    let _ = writeln!(
        md,
        "\nF6 budget refusal verification (Option B): `ShapedGlyphCount`={glyph_refused}, `TextLineCount`={line_refused}, `MemorySize`={mem_refused}."
    );
    let _ = writeln!(
        md,
        "Mixed Standard-14 + OpenType verification (Option B): 1 line, 2 runs (WinAnsi Helvetica + RTL Hebrew), 1 link hit-box."
    );

    let out_path = Path::new(&out_dir);
    std::fs::create_dir_all(out_path).expect("create results dir");
    std::fs::write(out_path.join("benchmark.md"), &md).expect("write benchmark.md");
    let json = format!(
        "{{\n  \"iterations\": {iters},\n  \"f6_budget_refusals\": {{\"shaped_glyph_count\": {glyph_refused}, \"text_line_count\": {line_refused}, \"memory_size\": {mem_refused}}},\n  \"mixed_base14_opentype_runs\": {},\n  \"cases\": [\n{}\n  ]\n}}\n",
        mixed_res.lines[0].runs.len(),
        json_rows.join(",\n")
    );
    std::fs::write(out_path.join("benchmark.json"), json).expect("write benchmark.json");
    print!("{md}");
}
