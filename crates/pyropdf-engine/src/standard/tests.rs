use std::borrow::Cow;

use pyropdf_store::budget::{Budget, BudgetVec, DocumentLimits, LimitCode};

use super::*;

/// Places lines 1.2 em apart with 0.8 em above and 0.2 em below.
const PLACEMENT: Placement = Placement {
    ascent: 0.8,
    descent: 0.2,
    line_height: 1.2,
};

/// Lays out `text` with every character 10 points wide.
fn mono(text: &str, width: f64, justify: bool) -> Result<TextBox<'_>, LayoutError> {
    let budget = Budget::default();
    layout(
        text,
        (width, 1000.0),
        10.0,
        PLACEMENT,
        justify,
        &budget,
        |line| 10.0 * line.chars().count() as f64,
    )
}

fn texts<'t>(layout: &TextBox<'t>) -> Vec<&'t str> {
    layout.lines.iter().map(|line| line.text).collect()
}

/// Draws `layout` and returns the operators as text.
fn draw(height: f64, rect: [f64; 4], layout: &TextBox<'_>, align: Align) -> String {
    let budget = Budget::default();
    let mut out = BudgetVec::new(&budget);
    let style = Style {
        resource: "Helv",
        size: 10.0,
        color: DeviceColor::Gray(0.0),
    };
    text_ops(&mut out, height, rect, layout, align, style).unwrap();
    String::from_utf8(out.into_vec()).unwrap()
}

#[test]
fn lines_break_at_the_last_opportunity_that_fits() {
    let layout = mono("aaa bbb ccc", 75.0, false).unwrap();
    assert_eq!(texts(&layout), ["aaa bbb", "ccc"]);
    assert_eq!(layout.lines[0].width, 70.0);
    assert!(layout.lines.iter().all(|line| !line.justify));

    // A hyphen is an opportunity too, and a line keeps it.
    let layout = mono("well-known words", 60.0, false).unwrap();
    assert_eq!(texts(&layout), ["well-", "known", "words"]);
}

#[test]
fn justified_layouts_stretch_only_wrapped_lines() {
    let layout = mono("aaa bbb ccc\nddd", 75.0, true).unwrap();
    let justify: Vec<_> = layout.lines.iter().map(|line| line.justify).collect();
    assert_eq!(justify, [true, false, false]);
}

#[test]
fn a_word_wider_than_the_box_breaks_between_graphemes() {
    let layout = mono("abcdefghij", 35.0, false).unwrap();
    assert_eq!(texts(&layout), ["abc", "def", "ghi", "j"]);

    // A first word longer than 64 bytes is broken without measuring it whole.
    let long = format!("{} y", "x".repeat(100));
    let layout = mono(&long, 500.0, false).unwrap();
    let half = "x".repeat(50);
    assert_eq!(texts(&layout), [half.as_str(), &half, "y"]);

    // A combining accent stays with its letter.
    let accented = "e\u{301}".repeat(3);
    let layout = mono(&accented, 25.0, false).unwrap();
    assert_eq!(texts(&layout), ["e\u{301}"; 3]);
}

#[test]
fn a_box_narrower_than_one_grapheme_is_refused() {
    assert_eq!(mono("abc", 5.0, false).unwrap_err(), LayoutError::Narrow);
    assert_eq!(
        LayoutError::Narrow.to_string(),
        "textbox is too narrow to fit one text grapheme"
    );
}

#[test]
fn paragraphs_keep_empty_lines_and_drop_trailing_whitespace() {
    let layout = mono("one  \n\n   two", 1000.0, false).unwrap();
    assert_eq!(texts(&layout), ["one", "", "   two"]);
    assert_eq!(layout.lines[1].width, 0.0);

    // Whitespace that a wrap leaves at the start of a paragraph makes no
    // blank line of its own.
    let layout = mono("   aaaa bbbb", 75.0, false).unwrap();
    assert_eq!(texts(&layout), ["   aaaa", "bbbb"]);
}

#[test]
fn the_height_decides_whether_lines_fit() {
    let budget = Budget::default();
    let fixed = |line: &str| 10.0 * line.len() as f64;
    // Two lines at 10 points need 8 + 12 + 2 points.
    let fits = layout(
        "a\nb",
        (100.0, 22.0),
        10.0,
        PLACEMENT,
        false,
        &budget,
        fixed,
    )
    .unwrap();
    assert_eq!(fits.height, 22.0);
    assert_eq!(fits.spare_height, 0.0);
    assert!(fits.fits());
    assert_eq!(fits.leading, 12.0);
    let tight = layout(
        "a\nb",
        (100.0, 21.9),
        10.0,
        PLACEMENT,
        false,
        &budget,
        fixed,
    )
    .unwrap();
    assert!(!tight.fits());
}

#[test]
fn layouts_refuse_more_than_the_line_limit() {
    let most = "\n".repeat(MAX_LINES - 1);
    assert_eq!(mono(&most, 100.0, false).unwrap().lines.len(), MAX_LINES);
    let more = "\n".repeat(MAX_LINES);
    assert_eq!(mono(&more, 100.0, false).unwrap_err(), LayoutError::Lines);
    // Lines that wrapping makes count as well.
    let words = "a ".repeat(MAX_LINES + 1);
    assert_eq!(mono(&words, 15.0, false).unwrap_err(), LayoutError::Lines);
}

#[test]
fn the_budget_sets_the_line_limit() {
    let more = "\n".repeat(MAX_LINES);
    let lifted = Budget::default().with_limit(LimitCode::TextLineCount, None);
    let layout = layout(
        &more,
        (100.0, 1000.0),
        10.0,
        PLACEMENT,
        false,
        &lifted,
        |_: &str| 0.0,
    )
    .unwrap();
    assert_eq!(layout.lines.len(), MAX_LINES + 1);
    let lowered = Budget::default().with_limit(LimitCode::TextLineCount, Some(2));
    let refusal = super::layout(
        "a\nb\nc",
        (100.0, 1000.0),
        10.0,
        PLACEMENT,
        false,
        &lowered,
        |_: &str| 0.0,
    );
    assert_eq!(refusal.unwrap_err(), LayoutError::Lines);
}

#[test]
fn layouts_charge_their_collections_to_the_budget() {
    let limits = DocumentLimits::default();
    let budget = Budget::new(&limits).with_limit(LimitCode::MemorySize, Some(64));
    let words = "a ".repeat(100);
    let refusal = layout(
        &words,
        (15.0, 1e6),
        10.0,
        PLACEMENT,
        false,
        &budget,
        |line| 10.0 * line.len() as f64,
    )
    .unwrap_err();
    let LayoutError::Limit(refusal) = refusal else {
        panic!("not a limit: {refusal:?}");
    };
    assert_eq!(refusal.code(), LimitCode::MemorySize);
}

#[test]
fn line_metrics_space_lines_as_pymupdf() {
    let compact = LineMetrics {
        ascender: 0.8,
        descender: -0.2,
    };
    // In single precision 0.8 and -0.2 span a little more than 1 em.
    assert_eq!(compact.line_height(None), 1.0);
    assert_eq!(compact.line_height(Some(2.0)), 2.0);
    let small = LineMetrics {
        ascender: 0.7,
        descender: -0.2,
    };
    assert_eq!(small.line_height(None), 1.2);
    let tall = line_metrics(Base14Font::Helvetica);
    assert!((tall.line_height(None) - 1.374).abs() < 1e-9);
    assert_eq!(small.leading(10.0, None), Some(12.0));
    assert_eq!(compact.leading(f64::MAX, Some(2.0)), None);
    let placed = Placement::lines(tall, None);
    assert_eq!((placed.ascent, placed.descent), (1.075, 0.299));
}

#[test]
fn standard_widths_and_metrics_are_adobes() {
    assert_eq!(font("Times-Bold"), Some(Base14Font::TimesBold));
    assert_eq!(font("Arial"), None);
    // H, e, l, l, o at 722, 556, 222, 222, 556 thousandths.
    let width = text_width("Hello", Base14Font::Helvetica, 10.0).unwrap();
    assert!((width - 22.78).abs() < 1e-9);
    assert_eq!(text_width("Ж", Base14Font::Helvetica, 10.0), None);
    // Symbol reads `a` as its code 0x61, alpha.
    assert_eq!(glyph_code(Base14Font::Symbol, 'a'), Some(0x61));
    assert_eq!(char_metric(Base14Font::Symbol, 'a').unwrap().name, "alpha");
    assert_eq!(
        char_metric(Base14Font::Helvetica, 'A').unwrap().width_x,
        667.0
    );
    assert_eq!(char_metric(Base14Font::Helvetica, 'Ж'), None);
    assert_eq!(vertical_metrics(Base14Font::Helvetica), (0.931, 0.225));
    assert_eq!(
        font_bbox(Base14Font::Courier),
        [-0.031, -0.317, 0.683, 0.932]
    );
}

#[test]
fn text_is_encoded_in_winansi() {
    assert_eq!(winansi_byte('A'), Some(b'A'));
    assert_eq!(winansi_byte('\t'), Some(b'\t'));
    assert_eq!(winansi_byte('€'), Some(0x80));
    assert_eq!(winansi_byte('Ÿ'), Some(0x9f));
    assert_eq!(winansi_byte('ÿ'), Some(0xff));
    assert_eq!(winansi_byte('\u{81}'), None);
    assert!(is_winansi("café – “quoted”"));
    assert!(!is_winansi("日本"));

    let budget = Budget::default();
    let text = "€ (é) \\";
    let layout = form_layout(
        text,
        (500.0, 50.0),
        Base14Font::Helvetica,
        10.0,
        1.2,
        &budget,
    )
    .unwrap();
    assert!(
        draw(50.0, [0.0, 0.0, 500.0, 50.0], &layout, Align::Left)
            .contains(r"(\200 \(\351\) \\) Tj")
    );
    assert_eq!(
        textbox_layout(
            "Ж",
            (500.0, 50.0),
            Base14Font::Helvetica,
            10.0,
            PLACEMENT,
            false,
            &budget
        )
        .unwrap_err(),
        LayoutError::NotWinAnsi
    );
}

#[test]
fn text_operators_place_and_align_each_line() {
    let budget = Budget::default();
    // "Hi" is 9.44 points wide at 10 points, and Helvetica's ascent 0.931 em
    // puts the baseline 9.31 points below the box's top.
    let layout = form_layout(
        "Hi",
        (100.0, 20.0),
        Base14Font::Helvetica,
        10.0,
        1.2,
        &budget,
    )
    .unwrap();
    let rect = [10.0, 5.0, 110.0, 25.0];
    assert_eq!(
        draw(30.0, rect, &layout, Align::Left),
        "q\n10 5 100 20 re\nW\nn\nBT\n/Helv 10 Tf\n0 g\n0 Tw\n1 0 0 1 10 15.69 Tm\n(Hi) Tj\nET\nQ\n"
    );
    assert!(draw(30.0, rect, &layout, Align::Center).contains("1 0 0 1 55.28 15.69 Tm"));
    assert!(draw(30.0, rect, &layout, Align::Right).contains("1 0 0 1 100.56 15.69 Tm"));
    assert_eq!(Align::from_quadding(1), Align::Center);
    assert_eq!(Align::from_quadding(2), Align::Right);
    assert_eq!(Align::from_quadding(7), Align::Left);

    // Later lines are one leading lower, and a justified line spreads its
    // spaces over the room that it leaves.
    let layout = mono("aaa bbb ccc", 75.0, true).unwrap();
    let ops = draw(1000.0, [0.0, 0.0, 75.0, 1000.0], &layout, Align::Left);
    assert!(ops.contains("5 Tw\n1 0 0 1 0 992 Tm\n(aaa bbb) Tj"));
    assert!(ops.contains("0 Tw\n1 0 0 1 0 980 Tm\n(ccc) Tj"));
}

#[test]
fn colours_and_names_are_written_as_content() {
    assert_eq!(
        DeviceColor::Rgb(1.0, 0.5, 0.0).fill_operator(),
        "1 0.5 0 rg"
    );
    assert_eq!(DeviceColor::Gray(-0.0).fill_operator(), "0 g");
    let budget = Budget::default();
    let layout = mono("a", 100.0, false).unwrap();
    let mut out = BudgetVec::new(&budget);
    let style = Style {
        resource: "F 1/x",
        size: 9.5,
        color: DeviceColor::Rgb(0.0, 0.0, 1.0),
    };
    text_ops(
        &mut out,
        10.0,
        [0.0, 0.0, 10.0, 10.0],
        &layout,
        Align::Left,
        style,
    )
    .unwrap();
    assert!(out.starts_with(b"q\n0 0 10 10 re\nW\nn\nBT\n/F#201#2Fx 9.5 Tf\n0 0 1 rg\n"));
}

#[test]
fn fields_without_a_size_shrink_until_their_text_fits() {
    let budget = Budget::default();
    let font = Base14Font::Helvetica;
    // A short value fits at 12 points, or at the box's height when lower.
    let (layout, size) = fit("Hello world", (200.0, 20.0), font, 0.0, false, &budget).unwrap();
    assert_eq!((layout.lines.len(), size), (1, 12.0));
    let (_, size) = fit("Hello world", (200.0, 8.0), font, 0.0, false, &budget).unwrap();
    assert!(size < 8.0 && size > 8.0 * 0.85 * 0.85 * 0.85 * 0.85);

    // A single-line field shrinks until its text is one line.
    let (layout, size) = fit("Hello world", (30.0, 20.0), font, 0.0, false, &budget).unwrap();
    assert_eq!(layout.lines.len(), 1);
    assert!(layout.lines[0].width <= 30.0 && size < 12.0);
    // A multiline field wraps first.
    let (layout, size) = fit("Hello world", (40.0, 40.0), font, 0.0, true, &budget).unwrap();
    assert_eq!(
        (texts(&layout).as_slice(), size),
        (["Hello", "world"].as_slice(), 12.0)
    );

    // A fixed size wraps to the width and leaves the height to the clip.
    let (layout, size) = fit("Hello world", (40.0, 5.0), font, 12.0, false, &budget).unwrap();
    assert_eq!((layout.lines.len(), size), (2, 12.0));

    // A box too low for any size is refused.
    let refusal = fit("Hello", (100.0, 0.001), font, 0.0, false, &budget).unwrap_err();
    assert_eq!(refusal, LayoutError::Unfit);
}

#[test]
fn default_appearances_read_their_last_font_and_colour() {
    let parse = |da: &str| DefaultAppearance::parse(da.as_bytes()).unwrap();
    assert_eq!(
        parse("/HeBo 9 Tf 0 0 1 rg"),
        DefaultAppearance {
            font: "HeBo".to_owned(),
            size: 9.0,
            color: (0.0, 0.0, 1.0)
        }
    );
    let gray = parse("0.5 g /Cour 0 Tf");
    assert_eq!(
        (gray.font.as_str(), gray.size, gray.color),
        ("Cour", 0.0, (0.5, 0.5, 0.5))
    );
    // The last `Tf` and the last colour win, and CMYK converts naively.
    let last = parse("1 0 0 0 k /TiRo 12 Tf 0 Tc /Helv 8 Tf");
    assert_eq!(
        (last.font.as_str(), last.size, last.color),
        ("Helv", 8.0, (0.0, 1.0, 1.0))
    );
    assert_eq!(parse("1 0 0 rg 0.25 g").color, (0.25, 0.25, 0.25));
    // A negative size keeps the default, colours clamp, and an operator
    // without its operands is ignored.
    let odd = parse("/F1 -3 Tf 2 g 12 Tf rg");
    assert_eq!(
        (odd.font.as_str(), odd.size, odd.color),
        ("F1", 0.0, (1.0, 1.0, 1.0))
    );
    let empty = DefaultAppearance {
        font: "Helv".to_owned(),
        size: 0.0,
        color: (0.0, 0.0, 0.0),
    };
    assert_eq!(parse(""), empty);
    let long = format!("/TiRo 12 Tf {}", " ".repeat(MAX_DEFAULT_APPEARANCE));
    assert_eq!(
        DefaultAppearance::parse(long.as_bytes()).unwrap_err(),
        LayoutError::AppearanceTooLong
    );

    assert_eq!(da_font("HeBo"), "Helvetica-Bold");
    assert_eq!(da_font("ZaDb"), "ZapfDingbats");
    assert_eq!(da_font("F1"), "Helvetica");
    assert!(DA_FONTS.iter().all(|(_, name)| font(name).is_some()));
}

#[test]
fn comb_fields_count_graphemes() {
    let text = "e\u{301}👨\u{200d}👩\u{200d}👧ab";
    assert_eq!(grapheme_count(text), 4);
    let budget = Budget::default();
    let cells = graphemes(text, &budget).unwrap();
    assert_eq!(&*cells, ["e\u{301}", "👨\u{200d}👩\u{200d}👧", "a", "b"]);
    assert_eq!(grapheme_count(""), 0);
}

#[test]
fn single_line_fields_show_line_breaks_as_spaces() {
    let budget = Budget::default();
    assert_eq!(single_line("a\r\nb\nc", &budget).unwrap(), "a  b c");
    assert!(matches!(
        single_line("abc", &budget).unwrap(),
        Cow::Borrowed("abc")
    ));
}

#[test]
fn text_operators_clip_to_the_supplied_box() {
    let layout = mono("long text", 100.0, false).unwrap();
    let ops = draw(30.0, [10.0, 5.0, 110.0, 25.0], &layout, Align::Left);
    assert!(ops.starts_with("q\n10 5 100 20 re\nW\nn\nBT\n"), "{ops}");
}

#[test]
fn fitting_refuses_zero_height() {
    let budget = Budget::default();
    assert_eq!(
        fit(
            "Hello",
            (100.0, 0.0),
            Base14Font::Helvetica,
            0.0,
            false,
            &budget
        )
        .unwrap_err(),
        LayoutError::Unfit
    );
}

#[test]
fn cancelled_fast_paths_refuse_before_returning() {
    use pyropdf_store::budget::CancellationToken;
    let token = CancellationToken::new();
    token.cancel();
    let budget = Budget::default().with_cancellation(token);
    assert_eq!(
        single_line("plain", &budget).unwrap_err().code(),
        LimitCode::Cancelled
    );
    let refusal = fit(
        "hello",
        (100.0, 20.0),
        Base14Font::Helvetica,
        0.0,
        false,
        &budget,
    )
    .unwrap_err();
    assert!(
        matches!(refusal, LayoutError::Limit(refusal) if refusal.code() == LimitCode::Cancelled)
    );
}

#[test]
fn invalid_geometry_cannot_emit_invalid_pdf_numbers() {
    let budget = Budget::default();
    assert_eq!(
        fit(
            "hello",
            (100.0, f64::NAN),
            Base14Font::Helvetica,
            0.0,
            false,
            &budget
        )
        .unwrap_err(),
        LayoutError::InvalidGeometry
    );
    let layout = mono("hello", 100.0, false).unwrap();
    let mut out = BudgetVec::new(&budget);
    let style = Style {
        resource: "Helv",
        size: 10.0,
        color: DeviceColor::Gray(f64::INFINITY),
    };
    assert_eq!(
        text_ops(
            &mut out,
            30.0,
            [0.0, 0.0, 10.0, 10.0],
            &layout,
            Align::Left,
            style
        )
        .unwrap_err(),
        LayoutError::InvalidGeometry
    );
    assert!(out.is_empty());
}

#[test]
fn resource_names_are_charged_to_the_output_budget() {
    let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(128));
    let layout = mono("hello", 100.0, false).unwrap();
    let mut out = BudgetVec::new(&budget);
    let resource = "long ".repeat(1000);
    let style = Style {
        resource: &resource,
        size: 10.0,
        color: DeviceColor::Gray(0.0),
    };
    let refusal = text_ops(
        &mut out,
        30.0,
        [0.0, 0.0, 10.0, 10.0],
        &layout,
        Align::Left,
        style,
    )
    .unwrap_err();
    assert!(
        matches!(refusal, LayoutError::Limit(refusal) if refusal.code() == LimitCode::MemorySize)
    );
}
