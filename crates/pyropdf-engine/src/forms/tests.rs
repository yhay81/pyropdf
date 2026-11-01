use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{DocumentLimits, LimitCode};
use pyropdf_store::index::Index;
use pyropdf_store::save::FileBase;
use pyropdf_store::version::Document;

use super::*;

fn open(objects: &[&str]) -> Document {
    let mut out = b"%PDF-1.7\n".to_vec();
    let mut offsets = Vec::new();
    for (number, object) in (1..).zip(objects) {
        offsets.push(out.len());
        out.extend_from_slice(format!("{number} 0 obj\n{object}\nendobj\n").as_bytes());
    }
    let xref = out.len();
    out.extend_from_slice(
        format!("xref\n0 {}\n0000000000 65535 f \n", offsets.len() + 1).as_bytes(),
    );
    for offset in offsets {
        out.extend_from_slice(format!("{offset:010} 00000 n \n").as_bytes());
    }
    out.extend_from_slice(
        format!(
            "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n",
            objects.len() + 1
        )
        .as_bytes(),
    );
    let budget = Budget::default();
    let index = Index::open(Bytes::new(out), &budget).unwrap();
    Document::new(Arc::new(FileBase::new(index, &budget).unwrap()), None)
}

/// A form whose field, object 3, has the widget `widget`, object 4, and
/// whose interactive form adds `form`.
fn form(form: &str, field: &str, widget: &str) -> Document {
    open(&[
        "<< /Type /Catalog /AcroForm 2 0 R >>",
        &format!("<< /Fields [3 0 R] {form} >>"),
        &format!("<< /T (f) /Kids [4 0 R] {field} >>"),
        &format!("<< /Parent 3 0 R /Subtype /Widget {widget} >>"),
        "<< /Length 1 >>\nstream\nx\nendstream",
    ])
}

fn draw(document: &Document) -> Result<Option<Drawing>, WidgetError> {
    let version = document.current();
    let budget = Budget::default();
    let widget = version.get(4, &budget).unwrap().unwrap();
    let Value::Dict(widget) = &**widget.value() else {
        panic!("the widget is a dictionary");
    };
    appearance(
        &Reader {
            version: &version,
            budget: &budget,
        },
        widget,
    )
}

fn content(document: &Document) -> String {
    match draw(document).unwrap() {
        Some(Drawing::Drawn(drawn)) => String::from_utf8(drawn.content).unwrap(),
        _ => panic!("the widget is drawn"),
    }
}

fn message(document: &Document) -> String {
    match draw(document) {
        Err(error) => error.to_string(),
        Ok(_) => panic!("the widget is refused"),
    }
}

#[test]
fn a_text_widget_draws_its_border_and_clipped_text() {
    let document = form(
        "/DA (/Helv 0 Tf 0 g)",
        "/FT /Tx /V (Hi)",
        "/Rect [0 0 100 20]",
    );
    let content = content(&document);
    assert!(content.starts_with("q\n0 G\n1 w\n0.5 0.5 99 19 re S\nQ\nq\n2 2 96 16 re W n\n"));
    assert!(content.contains("/Helv 12 Tf\n0 0 0 rg\n"));
    assert!(content.ends_with("ET\nQ\nQ\n"));
}

#[test]
fn a_widget_of_another_field_is_not_drawn() {
    let document = form("", "/FT /Xyz", "/Rect [0 0 20 20]");
    assert!(draw(&document).unwrap().is_none());
}

#[test]
fn a_value_outside_winansi_removes_the_appearance() {
    let document = form("", "/FT /Tx /V <FEFF65E5>", "/Rect [0 0 100 20]");
    assert!(matches!(draw(&document).unwrap(), Some(Drawing::Removed)));
}

#[test]
fn null_entries_inherit_and_the_form_quadding_is_the_default() {
    let document = form(
        "/Q 2",
        "/FT /Tx /V (Hi) /Q null",
        "/Rect [0 0 100 20] /MK << /R 90 >>",
    );
    let Some(Drawing::Drawn(drawn)) = draw(&document).unwrap() else {
        panic!("the widget is drawn");
    };
    assert_eq!(drawn.size, (20.0, 100.0));
    assert_eq!(drawn.matrix, Some([0.0, 1.0, -1.0, 0.0, 100.0, 0.0]));
    // Right alignment ends the text at the right of the content box.
    let content = String::from_utf8(drawn.content).unwrap();
    assert!(!content.contains("1 0 0 1 2 "), "{content}");
}

#[test]
fn comb_fields_refuse_what_they_cannot_hold() {
    let comb = "/FT /Tx /Ff 16777216";
    let rect = "/Rect [0 0 100 20]";
    assert_eq!(
        message(&form("", &format!("{comb} /MaxLen 2 /V (abc)"), rect)),
        "comb field value has 3 characters, exceeding MaxLen 2"
    );
    assert_eq!(
        message(&form("", &format!("{comb} /MaxLen 0 /V (a)"), rect)),
        "comb field requires a positive MaxLen"
    );
    assert_eq!(
        message(&form("", "/FT /Tx /Ff 16781312 /MaxLen 2 /V (a)", rect)),
        "comb fields cannot also be multiline, password, or file-select fields"
    );
    assert_eq!(
        message(&form("", "/FT /Tx /V (a)", "/Rect [0 0 0 20]")),
        "widget Rect must have positive finite dimensions"
    );
}

#[test]
fn settling_keeps_the_down_appearance_and_skips_what_it_cannot_draw() {
    let document = form(
        "",
        "/FT /Tx /V (Hi)",
        "/Rect [0 0 100 20] /AP << /N 5 0 R /D 5 0 R >>",
    );
    let budget = Budget::default();
    let mut transaction = document.begin();
    let widget = transaction.version().get(4, &budget).unwrap().unwrap();
    let Value::Dict(widget) = &**widget.value() else {
        panic!("the widget is a dictionary");
    };
    settle(&mut transaction, 4, widget, &budget).unwrap();
    let written = transaction.version().get(4, &budget).unwrap().unwrap();
    let Value::Dict(written) = &**written.value() else {
        panic!("the widget is a dictionary");
    };
    let Some(Value::Dict(appearances)) = written.get("AP") else {
        panic!("the widget has appearances");
    };
    assert_eq!(appearances.get("D"), widget_reference(5).as_ref());
    assert_ne!(appearances.get("N"), widget_reference(5).as_ref());

    let malformed = form("", "/FT /Tx /V (Hi)", "/Rect [0 0 0 20]");
    let mut transaction = malformed.begin();
    let widget = transaction.version().get(4, &budget).unwrap().unwrap();
    let Value::Dict(widget) = &**widget.value() else {
        panic!("the widget is a dictionary");
    };
    settle(&mut transaction, 4, widget, &budget).unwrap();
    assert_eq!(
        transaction
            .version()
            .get(4, &budget)
            .unwrap()
            .unwrap()
            .value(),
        malformed
            .current()
            .get(4, &budget)
            .unwrap()
            .unwrap()
            .value()
    );
}

fn widget_reference(number: u32) -> Option<Value> {
    Some(Value::Reference(Reference::new(number, 0)))
}

#[test]
fn settling_refuses_what_the_budget_refuses() {
    let document = form("", "/FT /Tx /Ff 4096 /V (Hi)", "/Rect [0 0 100 20]");
    let limits = DocumentLimits::default();
    let budget = Budget::new(&limits).with_limit(LimitCode::MemorySize, Some(0));
    let mut transaction = document.begin();
    let widget = transaction
        .version()
        .get(4, &Budget::default())
        .unwrap()
        .unwrap();
    let Value::Dict(widget) = &**widget.value() else {
        panic!("the widget is a dictionary");
    };
    let error = settle(&mut transaction, 4, widget, &budget).unwrap_err();
    let refusal: &LimitExceeded = error.limit().unwrap();
    assert_eq!(refusal.code(), LimitCode::MemorySize);
}

#[test]
fn an_oversized_default_appearance_refuses_settling() {
    let long = "/Helv 12 Tf ".repeat(400);
    let document = form(
        &format!("/DA ({long})"),
        "/FT /Tx /V (Hi)",
        "/Rect [0 0 100 20]",
    );
    let budget = Budget::default();
    let mut transaction = document.begin();
    let widget = transaction.version().get(4, &budget).unwrap().unwrap();
    let Value::Dict(widget) = &**widget.value() else {
        panic!("the widget is a dictionary")
    };
    let error = settle(&mut transaction, 4, widget, &budget).unwrap_err();
    assert_eq!(
        error.limit().unwrap().code(),
        LimitCode::FormDefaultAppearanceSize
    );
}

#[test]
fn parent_chains_refuse_depth_and_cycles_without_truncation() {
    let mut objects = Vec::new();
    for number in 1..=MAX_PARENTS + 2 {
        objects.push(format!("<< /Parent {} 0 R >>", number + 1));
    }
    let strings: Vec<_> = objects.iter().map(String::as_str).collect();
    let document = open(&strings);
    let version = document.current();
    let budget = Budget::default();
    let reader = Reader {
        version: &version,
        budget: &budget,
    };
    let mut value = Arc::clone(version.get(1, &budget).unwrap().unwrap().value());
    let mut visited = Vec::new();
    for _ in 0..MAX_PARENTS {
        let Value::Dict(dict) = &*value else {
            panic!("a parent is a dictionary")
        };
        value = parent(&reader, dict, &mut visited).unwrap().unwrap();
    }
    let Value::Dict(dict) = &*value else {
        panic!("a parent is a dictionary")
    };
    let error = parent(&reader, dict, &mut visited).unwrap_err();
    assert_eq!(
        error.limit().unwrap().code(),
        LimitCode::FormFieldParentDepth
    );

    let cyclic = form("", "/FT /Tx /Parent 4 0 R", "/Rect [0 0 100 20]");
    assert!(
        matches!(draw(&cyclic), Err(WidgetError::Store(error)) if error.to_string().contains("cycle"))
    );
}

#[test]
fn appearance_values_decode_with_budgeted_memory() {
    let examples: &[&[u8]] = &[
        b"hello\x80",
        b"\xfe\xff\x00A\xd8\x00",
        b"\xef\xbb\xbfA\xffB",
    ];
    for &encoded in examples {
        let budget = Budget::default();
        let mut output = BudgetVec::new(&budget);
        text::append_text(&mut output, encoded).unwrap();
        assert_eq!(std::str::from_utf8(&output).unwrap(), text_string(encoded));
    }
    let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(8));
    let mut output = BudgetVec::new(&budget);
    let error = text::append_text(&mut output, &[0x80; 100]).unwrap_err();
    assert_eq!(error.code(), LimitCode::MemorySize);
}

#[test]
fn stepped_utf8_decoding_matches_from_utf8_lossy() {
    let samples: &[&[u8]] = &[
        b"plain",
        "\u{e9}\u{4e00}\u{1f600}".as_bytes(),
        b"a\xffb\xe4\xb8c\xf0\x9f\x98",
        b"\xe0\x80\x80\xed\xa0\x80\xf4\x90\x80\x80",
        b"\xf0\x9f\x98\x80\xf0\x9f",
        b"\xc3",
    ];
    for &bytes in samples {
        // Repeat each sample so that steps cut its sequences everywhere.
        let bytes = bytes.repeat(5);
        for step in 1..12 {
            let budget = Budget::default();
            let mut output = BudgetVec::new(&budget);
            text::append_lossy_with(&mut output, &bytes, step, &mut || Ok(())).unwrap();
            assert_eq!(
                std::str::from_utf8(&output).unwrap(),
                String::from_utf8_lossy(&bytes),
                "{bytes:?} in steps of {step}"
            );
        }
    }
}

#[test]
fn utf8_decoding_stops_midway_for_cancellation_and_deadline() {
    use std::time::Instant;

    use pyropdf_store::budget::CancellationToken;

    let bytes = "\u{e9}".repeat(64 * 4096).into_bytes();
    let token = CancellationToken::new();
    token.cancel();
    let cancelled = Budget::default().with_cancellation(token);
    let expired = Budget::default().with_deadline(Instant::now());
    for (stopped, code) in [
        (cancelled, LimitCode::Cancelled),
        (expired, LimitCode::Deadline),
    ] {
        let budget = Budget::default();
        let mut output = BudgetVec::new(&budget);
        let mut polls = 0;
        let error = text::append_lossy_with(&mut output, &bytes, 4096, &mut || {
            polls += 1;
            if polls >= 32 { stopped.poll() } else { Ok(()) }
        })
        .unwrap_err();
        assert_eq!(error.code(), code);
        assert_eq!(polls, 32);
        // The polls came between steps, after part of the input.
        assert!(!output.is_empty() && output.len() < bytes.len());
    }
}

fn states(document: &Document) -> Vec<(String, String)> {
    match draw(document).unwrap() {
        Some(Drawing::States(states)) => states
            .into_iter()
            .map(|(state, drawn)| {
                (
                    String::from_utf8_lossy(state.as_bytes()).into_owned(),
                    String::from_utf8(drawn.content).unwrap(),
                )
            })
            .collect(),
        _ => panic!("the widget lacks states"),
    }
}

#[test]
fn a_check_box_lacks_its_off_and_selected_states() {
    let document = form(
        "",
        "/FT /Btn /V /Yes",
        "/Rect [0 0 20 20] /AS /Yes /AP << /N << /Yes 5 0 R >> >>",
    );
    // The selected state holds content and is kept.
    let drawn = states(&document);
    assert_eq!(drawn.len(), 1);
    assert_eq!(drawn[0].0, "Off");
    assert!(drawn[0].1.ends_with("re S\nQ\n"), "{}", drawn[0].1);

    let document = form("", "/FT /Btn /V /Yes", "/Rect [0 0 20 20] /AS /Yes");
    let drawn = states(&document);
    assert_eq!(
        drawn
            .iter()
            .map(|(state, _)| state.as_str())
            .collect::<Vec<_>>(),
        ["Off", "Yes"]
    );
    assert!(drawn[1].1.contains(" m\n") && drawn[1].1.ends_with("S\nQ\n"));
}

#[test]
fn a_radio_button_is_round_and_a_drawn_pushbutton_is_kept() {
    let document = form("", "/FT /Btn /Ff 32768 /V /A", "/Rect [0 0 20 20] /AS /A");
    let drawn = states(&document);
    assert!(drawn[0].1.contains(" c\n"));
    assert!(drawn[1].1.ends_with("0 g\nf\n"));

    let pushbutton = "/FT /Btn /Ff 65536";
    let document = form("", pushbutton, "/Rect [0 0 20 20] /AP << /N 5 0 R >>");
    assert!(draw(&document).unwrap().is_none());

    let document = form(
        "",
        "/FT /Btn /V /Off",
        "/Rect [0 0 20 20] /AS /Off /AP << /N << /Off 5 0 R >> >>",
    );
    assert!(draw(&document).unwrap().is_none());
}

#[test]
fn a_pushbutton_shows_its_caption_and_a_signature_its_border() {
    let document = form(
        "",
        "/FT /Btn /Ff 65536",
        "/Rect [0 0 60 20] /MK << /CA (Go) >>",
    );
    let content = content(&document);
    assert!(content.starts_with("q\n0 G\n1 w\n"), "{content}");
    assert!(content.contains("(Go) Tj"), "{content}");

    let document = form("", "/FT /Sig", "/Rect [0 0 60 20]");
    let Some(Drawing::Drawn(drawn)) = draw(&document).unwrap() else {
        panic!("the widget is drawn");
    };
    assert!(drawn.font.is_none());
    assert!(
        String::from_utf8(drawn.content)
            .unwrap()
            .ends_with("re S\nQ\n")
    );
}

/// Draws `/DA (/F1 10 Tf 0 g)` with `/F1` as `font` in the form's `/DR`,
/// and returns the resource and Standard 14 font of the appearance, or the
/// refusal's message.
fn form_font(font: &str) -> Result<(String, &'static str), String> {
    let document = form(
        &format!("/DA (/F1 10 Tf 0 g) /DR << /Font << /F1 {font} >> >>"),
        "/FT /Tx /V (A'`)",
        "/Rect [0 0 100 20]",
    );
    match draw(&document) {
        Ok(Some(Drawing::Drawn(drawn))) => match drawn.font {
            Some((resource, FontEntry::Standard(base_font))) => Ok((resource, base_font)),
            _ => panic!("the appearance names a Standard 14 font"),
        },
        Ok(_) => panic!("the widget is drawn"),
        Err(error) => Err(error.to_string()),
    }
}

#[test]
fn standard_form_fonts_resolve_by_alias_whatever_their_encoding_entry() {
    for font in [
        "<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
        "<< /Subtype /Type1 /BaseFont /Courier /Name /F1 /FirstChar 32 /LastChar 32 /Widths [600] >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /MacRomanEncoding >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding null >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding << /BaseEncoding /WinAnsiEncoding >> >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding << /Differences [] >> >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /FontDescriptor << /FontName /Courier >> >>",
    ] {
        assert_eq!(form_font(font), Ok(("F1".to_owned(), "Courier")), "{font}");
    }
    let symbol = "<< /Type /Font /Subtype /Type1 /BaseFont /ZapfDingbats >>";
    assert_eq!(form_font(symbol), Ok(("F1".to_owned(), "ZapfDingbats")));
}

#[test]
fn unsupported_form_font_encodings_refuse_the_drawing() {
    for font in [
        "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /MacExpertEncoding >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /Identity-H >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding 3 >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding << /Differences [39 /quoteright] >> >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding << /BaseEncoding /MacExpertEncoding >> >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Symbol /Encoding /WinAnsiEncoding >>",
    ] {
        let message = form_font(font).unwrap_err();
        assert!(message.contains("/Encoding"), "{font}: {message}");
    }
}

#[test]
fn other_form_fonts_fall_back_to_helvetica() {
    for font in [
        "<< /Type /Font /Subtype /TrueType /BaseFont /Courier >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /OtherFont >>",
        "<< /Type /Encoding /Subtype /Type1 /BaseFont /Courier >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /FontDescriptor << /FontFile 5 0 R >> >>",
    ] {
        assert_eq!(
            form_font(font),
            Ok(("Helv".to_owned(), "Helvetica")),
            "{font}"
        );
    }
}
