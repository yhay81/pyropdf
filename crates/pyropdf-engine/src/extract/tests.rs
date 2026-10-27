//! The content model's sink and budget, which no public API of Python
//! exposes.

use hayro::hayro_interpret::DrawMode;
use hayro::hayro_interpret::font::Glyph;
use hayro::kurbo::{Affine, BezPath, Rect};
use pyropdf_store::budget::{Budget, LimitCode};

use super::*;

/// Returns a one-page PDF that paints `content`, with Helvetica as `/F1`.
fn pdf_with(content: &str) -> Vec<u8> {
    let objects = [
        "<< /Type /Catalog /Pages 2 0 R >>".to_owned(),
        "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>".to_owned(),
        "<< /Type /Page /Parent 2 0 R /Contents 4 0 R \
         /Resources << /Font << /F1 5 0 R >> >> >>"
            .to_owned(),
        format!(
            "<< /Length {} >>\nstream\n{content}\nendstream",
            content.len()
        ),
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>".to_owned(),
    ];
    let mut out = b"%PDF-1.7\n".to_vec();
    let mut offsets = Vec::new();
    for (at, body) in objects.iter().enumerate() {
        offsets.push(out.len());
        out.extend_from_slice(format!("{} 0 obj\n{body}\nendobj\n", at + 1).as_bytes());
    }
    let xref = out.len();
    out.extend_from_slice(
        format!("xref\n0 {}\n0000000000 65535 f \n", objects.len() + 1).as_bytes(),
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
    out
}

const ALL: ItemKinds = ItemKinds {
    text_runs: true,
    paths: true,
    images: true,
};

/// A sink that records what it receives.
#[derive(Default, PartialEq, Debug)]
struct Recording {
    runs: Vec<(u8, Option<Rect>)>,
    glyphs: Vec<(Option<char>, [f64; 6], Option<Rect>)>,
    completions: usize,
    paths: Vec<([f64; 6], &'static str)>,
}

impl ContentSink for Recording {
    fn glyph_run(&mut self, style: &GlyphStyle) {
        self.runs.push((style.render_mode, style.clip));
    }

    fn glyph(&mut self, glyph: &Glyph<'_>, transform: Affine, ink: Option<Rect>) {
        let text = match glyph.as_unicode() {
            Some(hayro::hayro_interpret::hayro_cmap::BfString::Char(c)) => Some(c),
            _ => None,
        };
        self.glyphs.push((text, transform.as_coeffs(), ink));
    }

    fn wants_ink(&self) -> bool {
        true
    }

    fn stroke_completes_fill(&mut self) {
        self.completions += 1;
    }

    fn path(&mut self, _: &BezPath, transform: Affine, mode: &DrawMode) {
        let mode = match mode {
            DrawMode::Fill(_) => "fill",
            DrawMode::Stroke(_) => "stroke",
            DrawMode::FillAndStroke(..) => "fill and stroke",
            DrawMode::Invisible => "invisible",
        };
        self.paths.push((transform.as_coeffs(), mode));
    }

    fn stopped(&self) -> bool {
        false
    }
}

#[test]
fn a_sink_sees_the_same_glyphs_and_paths_with_items_off() {
    let bridged = Bridged::of_bytes(pdf_with(
        "q 10 10 80 80 re W n 1 0 0 rg 20 20 30 30 re B Q \
         BT /F1 12 Tf 10 50 Td 2 Tr (Hi) Tj 0 Tr ( there) Tj ET",
    ));
    let budget = Budget::default();
    let (all, with_items) =
        page_content_into(&bridged, 0, Recording::default(), ALL, true, None, &budget).unwrap();
    let (none, without) = page_content_into(
        &bridged,
        0,
        Recording::default(),
        ItemKinds::NONE,
        true,
        None,
        &budget,
    )
    .unwrap();

    assert_eq!(with_items, without);
    assert!(none.items.is_empty());
    assert_eq!(all.items.len(), 3);
    // The stroke of the fill-and-stroke run sends no glyphs again.
    assert_eq!(with_items.completions, 1);
    let text: String = with_items
        .glyphs
        .iter()
        .filter_map(|glyph| glyph.0)
        .collect();
    assert_eq!(text, "Hi there");
    assert!(with_items.glyphs.iter().any(|glyph| glyph.2.is_some()));
    // hayro sends the fill of the fill-and-stroke run first, and `Q` ends
    // the clip before the text.
    assert_eq!(with_items.runs, [(RENDER_FILL, None), (RENDER_FILL, None)]);
}

#[test]
fn the_budget_counts_elements_and_a_refusal_keeps_nothing() {
    let bridged = Bridged::of_bytes(pdf_with("10 10 20 20 re f"));
    // One path of five segments, and its graphics context.
    let fits = |limit| {
        let budget = Budget::default().with_limit(LimitCode::ContentElementCount, Some(limit));
        page_content(&bridged, 0, ALL, true, None, &budget)
    };
    let content = fits(7).unwrap();
    assert_eq!(content.items.len(), 1);
    let Err(Error::Limit(refusal)) = fits(6) else {
        panic!("six elements should not hold the path");
    };
    assert_eq!(refusal.code(), LimitCode::ContentElementCount);
    assert_eq!(
        Budget::default().limit(LimitCode::ContentElementCount),
        Some(262_144)
    );
}

#[test]
fn a_missing_page_is_refused() {
    let bridged = Bridged::of_bytes(pdf_with(""));
    let budget = Budget::default();
    assert_eq!(
        page_content(&bridged, 1, ALL, true, None, &budget).unwrap_err(),
        Error::NoPage(1)
    );
}

#[test]
fn text_charges_its_glyphs_and_text_and_a_refusal_keeps_nothing() {
    let bridged = Bridged::of_bytes(pdf_with("BT /F1 12 Tf 10 50 Td (Hi there) Tj ET"));
    let options = TextOptions::default();
    let budget = Budget::default();
    let mut out = String::from("before ");
    let skipped = page_text(&bridged, 0, &options, &budget, &mut out).unwrap();
    assert_eq!((out.as_str(), skipped), ("before Hi there\n", 0));
    // The page's glyphs are released with it, and its text stays charged.
    assert_eq!(budget.used(LimitCode::TextGlyphCount), 0);
    assert_eq!(budget.used(LimitCode::TextSize), 9);

    // Eight glyphs are collected, and their eight bytes become nine.
    for (code, limit) in [(LimitCode::TextGlyphCount, 7), (LimitCode::TextSize, 8)] {
        let budget = Budget::default().with_limit(code, Some(limit));
        let mut out = String::from("before ");
        let Err(Error::Limit(refusal)) = page_text(&bridged, 0, &options, &budget, &mut out)
        else {
            panic!("{limit} should not hold the text");
        };
        assert_eq!(refusal.code(), code);
        assert_eq!(out, "before ");
        assert_eq!(budget.used(LimitCode::TextGlyphCount), 0);
        assert_eq!(budget.used(LimitCode::TextSize), 0);
    }
    assert_eq!(
        Budget::default().limit(LimitCode::TextGlyphCount),
        Some(262_144)
    );
    assert_eq!(
        Budget::default().limit(LimitCode::TextSize),
        Some(64 * 1024 * 1024)
    );
}

#[test]
fn text_of_a_missing_page_is_refused() {
    let bridged = Bridged::of_bytes(pdf_with(""));
    let mut out = String::new();
    assert_eq!(
        page_text(
            &bridged,
            1,
            &TextOptions::default(),
            &Budget::default(),
            &mut out
        ),
        Err(Error::NoPage(1))
    );
}
