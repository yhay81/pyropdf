//! The content model's sink and budget, which no public API of Python
//! exposes.

use hayro::hayro_interpret::DrawMode;
use hayro::hayro_interpret::font::Glyph;
use hayro::kurbo::{Affine, BezPath, Rect};
use std::sync::Arc;

use pyropdf_store::budget::{Budget, LimitCode};
use pyropdf_store::index::Index;
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::save::FileBase;
use pyropdf_store::value::Bytes;
use pyropdf_store::version::Document;

use super::*;
use crate::fonts::embed::Face;
use crate::render::{Fallback, FontFile, Language, Source};

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
    pdf_of(&objects)
}

/// Returns a PDF of `objects`, numbered from 1, whose catalog is object 1.
fn pdf_of(objects: &[String]) -> Vec<u8> {
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
    let faces = Fallback::default();
    let (all, with_items) = page_content_into(
        &bridged,
        &faces,
        0,
        Recording::default(),
        ALL,
        true,
        None,
        &budget,
    )
    .unwrap();
    let (none, without) = page_content_into(
        &bridged,
        &faces,
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
    assert_eq!(none.paths.rows(), 0);
    assert_eq!(all.items.len(), 3);
    assert_eq!(all.paths.rows(), 1);
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
        page_content(&bridged, &Fallback::default(), 0, ALL, true, None, &budget)
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

/// Returns a one-page PDF that paints `content`, with two shading patterns,
/// `/P1` and `/P2`.
fn pdf_with_patterns(content: &str) -> Vec<u8> {
    let pattern = |matrix: &str| {
        format!(
            "<< /PatternType 2 /Matrix [{matrix}] /Shading << /ShadingType 2 \
             /ColorSpace /DeviceRGB /Coords [0 0 1 1] /Function << /FunctionType 2 \
             /Domain [0 1] /C0 [1 0 0] /C1 [0 0 1] /N 1 >> >> >>"
        )
    };
    let objects = [
        "<< /Type /Catalog /Pages 2 0 R >>".to_owned(),
        "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>".to_owned(),
        "<< /Type /Page /Parent 2 0 R /Contents 4 0 R \
         /Resources << /Pattern << /P1 5 0 R /P2 6 0 R >> >> >>"
            .to_owned(),
        format!(
            "<< /Length {} >>\nstream\n{content}\nendstream",
            content.len()
        ),
        pattern("1 0 0 1 0 0"),
        pattern("1 0 0 1 5 5"),
    ];
    pdf_of(&objects)
}

/// Returns the rows that the path items of `content` name, in paint order.
fn path_rows(content: &PageContent) -> Vec<u32> {
    content
        .items
        .iter()
        .filter_map(|item| match item {
            ContentItem::Path(row) => Some(*row),
            ContentItem::Text(_) | ContentItem::Image(_) => None,
        })
        .collect()
}

#[test]
fn a_page_of_paths_costs_a_small_item_per_path() {
    // What keeps the memo of a CAD page to its columns (#615).
    assert!(size_of::<ContentItem>() <= 16);
}

#[test]
fn paths_are_rows_of_columns_in_paint_order() {
    let bridged = Bridged::of_bytes(pdf_with(
        "1 0 0 rg 10 20 m 30 40 l 50 60 70 80 90 90 c h f \
         BT /F1 12 Tf 10 50 Td (Hi) Tj ET \
         0 0 1 RG 2 w 5 5 m 9 9 l S",
    ));
    let content = page_content(
        &bridged,
        &Fallback::default(),
        0,
        ALL,
        true,
        None,
        &Budget::default(),
    )
    .unwrap();
    assert_eq!(content.items.len(), 3);
    assert_eq!(path_rows(&content), [0, 1]);
    let paths = &*content.paths;
    assert_eq!(paths.rows(), 2);
    assert_eq!(paths.segment_offsets, [0, 4, 6]);
    assert_eq!(paths.path, [0, 0, 0, 0, 1, 1]);
    assert_eq!(paths.verb, b"mlchml");
    // A closing segment ends where its subpath starts, in display space.
    assert_eq!(&paths.to_x[..4], [10.0, 30.0, 90.0, 10.0]);
    assert_eq!(&paths.to_y[..4], [80.0, 60.0, 10.0, 80.0]);
    assert_eq!((paths.c1_x[2], paths.c2_y[2]), (50.0, 20.0));
    assert!(paths.c1_x[0].is_nan() && paths.c2_y[3].is_nan());
    assert_eq!(paths.curve_bits, [0b0000_0100]);
    assert_eq!(paths.curves, 1);
    assert_eq!(paths.fill.present, [1, 0]);
    assert_eq!(paths.stroke.present, [0, 1]);
    assert_eq!(paths.fill.present_bits, [0b01]);
    assert_eq!(paths.fill.color_bits, [0b01]);
    assert_eq!(paths.fill.pattern_bits, [0]);
    assert_eq!(paths.stroke.present_bits, [0b10]);
    assert_eq!(paths.soft_mask, [-1, -1]);
    assert_eq!(paths.soft_mask_bits, [0]);

    let (verbs, coords, bbox, fill, even_odd, stroke, context) = paths.tuple(0);
    assert_eq!(verbs, b"mlch");
    assert_eq!(
        coords,
        [10.0, 80.0, 30.0, 60.0, 50.0, 40.0, 70.0, 20.0, 90.0, 10.0]
    );
    assert_eq!(bbox.0, 10.0);
    assert_eq!(fill, Some((0, vec![1.0, 0.0, 0.0, 1.0])));
    assert!(!even_odd);
    assert_eq!(stroke, None);
    assert_eq!(context, (0, 0, None));
    let (_, coords, _, fill, _, stroke, _) = paths.tuple(1);
    assert_eq!(coords, [5.0, 95.0, 9.0, 91.0]);
    assert_eq!(fill, None);
    assert_eq!(
        stroke,
        Some(((0, vec![0.0, 0.0, 1.0, 1.0]), 2.0, 0, 0, 10.0, vec![], 0.0))
    );
}

#[test]
fn a_page_without_paths_has_valid_offsets() {
    let bridged = Bridged::of_bytes(pdf_with(""));
    let content = page_content(
        &bridged,
        &Fallback::default(),
        0,
        ALL,
        true,
        None,
        &Budget::default(),
    )
    .unwrap();
    assert_eq!(content.paths.rows(), 0);
    assert_eq!(content.paths.segment_offsets, [0]);
    assert_eq!(content.paths.stroke_dashes_offsets, [0]);
}

#[test]
fn a_stroke_of_the_same_segments_completes_the_fill_row() {
    let elements = |content: &str| {
        let bridged = Bridged::of_bytes(pdf_with(content));
        let budget = Budget::default();
        let content =
            page_content(&bridged, &Fallback::default(), 0, ALL, true, None, &budget).unwrap();
        (content, budget.used(LimitCode::ContentElementCount))
    };
    // The fill costs its item and five segments, and the graphics context
    // one more, whether or not a stroke and its two dashes complete it.
    let (filled, filled_elements) = elements("10 10 20 20 re f");
    assert_eq!(filled_elements, 7);
    assert_eq!(filled.paths.stroke.present, [0]);
    let (both, both_elements) = elements("[3 1] 0 d 1 0 0 rg 0 1 0 RG 10 10 20 20 re B");
    assert_eq!(both_elements, 9);
    assert_eq!(path_rows(&both), [0]);
    let paths = &*both.paths;
    assert_eq!(paths.segment_offsets, [0, 5]);
    assert_eq!(paths.stroke_dashes_offsets, [0, 2]);
    assert_eq!(paths.stroke_dashes, [3.0, 1.0]);
    let (_, _, _, fill, _, stroke, _) = paths.tuple(0);
    assert_eq!(fill, Some((0, vec![1.0, 0.0, 0.0, 1.0])));
    let (paint, width, ..) = stroke.unwrap();
    assert_eq!((paint, width), ((0, vec![0.0, 1.0, 0.0, 1.0]), 1.0));

    // A stroke of other segments is a path of its own.
    let (apart, apart_elements) = elements("10 10 20 20 re f 30 30 20 20 re S");
    assert_eq!(apart_elements, 13);
    assert_eq!(path_rows(&apart), [0, 1]);
    assert_eq!(apart.paths.fill.present, [1, 0]);
    assert_eq!(apart.paths.stroke.present, [0, 1]);
    assert_eq!(apart.paths.segment_offsets, [0, 5, 10]);
}

#[test]
fn equal_patterns_share_an_index_in_the_order_of_first_use() {
    let bridged = Bridged::of_bytes(pdf_with_patterns(
        "/Pattern cs /P2 scn 10 10 20 20 re f /P1 scn 40 10 20 20 re f \
         /P2 scn 70 10 20 20 re f",
    ));
    let content = page_content(
        &bridged,
        &Fallback::default(),
        0,
        ALL,
        true,
        None,
        &Budget::default(),
    )
    .unwrap();
    let paths = &*content.paths;
    assert_eq!(paths.fill.pattern, [0, 1, 0]);
    assert_eq!(paths.fill.pattern_bits, [0b111]);
    assert_eq!(paths.fill.color_bits, [0]);
    assert!(paths.fill.red.iter().all(|value| value.is_nan()));
    assert_eq!(paths.patterns.len(), 2);
    assert_eq!(paths.tuple(2).3.unwrap(), paths.patterns[0]);
    assert_ne!(paths.patterns[0], paths.patterns[1]);
}

#[test]
fn a_clip_keeps_the_rows_that_meet_it_after_counting_them_all() {
    let bridged = Bridged::of_bytes(pdf_with_patterns(
        "/Pattern cs /Pattern CS \
         /P1 scn 10 10 20 20 re f \
         [3 1] 0 d /P2 scn /P2 SCN 100 10 20 20 re B \
         [] 0 d /P1 scn 130 10 20 20 re f \
         170 10 20 20 re f",
    ));
    let read = |clip| {
        let budget = Budget::default();
        let content =
            page_content(&bridged, &Fallback::default(), 0, ALL, true, clip, &budget).unwrap();
        (content, budget.used(LimitCode::ContentElementCount))
    };
    let (all, all_elements) = read(None);
    assert_eq!(all.paths.rows(), 4);
    assert_eq!(all.paths.fill.pattern, [0, 1, 0, 0]);

    let (clipped, clipped_elements) = read(Some((90.0, 0.0, 155.0, 100.0)));
    assert_eq!(clipped_elements, all_elements);
    assert_eq!(path_rows(&clipped), [0, 1]);
    let paths = &*clipped.paths;
    assert_eq!(paths.rows(), 2);
    assert_eq!(
        (paths.x0.as_slice(), paths.x1.as_slice()),
        ([100.0, 130.0].as_slice(), [120.0, 150.0].as_slice())
    );
    assert_eq!(paths.segment_offsets, [0, 5, 10]);
    assert_eq!(paths.path, [0, 0, 0, 0, 0, 1, 1, 1, 1, 1]);
    assert_eq!(paths.verb.len(), 10);
    assert_eq!(paths.to_x.len(), 10);
    assert_eq!(paths.stroke_dashes_offsets, [0, 2, 2]);
    assert_eq!(paths.stroke_dashes, [3.0, 1.0]);
    assert_eq!(paths.stroke.present, [1, 0]);
    assert_eq!(paths.fill.present_bits, [0b11]);
    assert_eq!(paths.stroke.present_bits, [0b01]);
    // The first pattern that the kept rows use comes first, and each row's
    // paint is still the pattern that it painted with.
    assert_eq!(paths.fill.pattern, [0, 1]);
    assert_eq!(paths.stroke.pattern, [0, -1]);
    assert_eq!(paths.patterns.len(), 2);
    assert_eq!(paths.patterns[0], all.paths.patterns[1]);
    assert_eq!(paths.patterns[1], all.paths.patterns[0]);
    assert_eq!(paths.tuple(1).3.unwrap(), all.paths.tuple(2).3.unwrap());

    // A clip that every path meets changes nothing, and one that none meets
    // leaves an empty table with valid offsets.
    let (met, _) = read(Some((0.0, 0.0, 200.0, 100.0)));
    assert_eq!(path_rows(&met), [0, 1, 2, 3]);
    assert_eq!(met.paths.fill.pattern, [0, 1, 0, 0]);
    let (missed, _) = read(Some((0.0, 200.0, 200.0, 300.0)));
    assert_eq!(path_rows(&missed), []);
    assert_eq!(missed.paths.rows(), 0);
    assert_eq!(missed.paths.segment_offsets, [0]);
    assert_eq!(missed.paths.stroke_dashes_offsets, [0]);
    assert!(missed.paths.patterns.is_empty());
}

#[test]
fn a_missing_page_is_refused() {
    let bridged = Bridged::of_bytes(pdf_with(""));
    let budget = Budget::default();
    assert_eq!(
        page_content(&bridged, &Fallback::default(), 1, ALL, true, None, &budget).unwrap_err(),
        Error::NoPage(1)
    );
}

#[test]
fn text_charges_its_glyphs_and_text_and_a_refusal_keeps_nothing() {
    let bridged = Bridged::of_bytes(pdf_with("BT /F1 12 Tf 10 50 Td (Hi there) Tj ET"));
    let options = TextOptions::default();
    let budget = Budget::default();
    let mut out = String::from("before ");
    let (skipped, peak) = page_text(&bridged, 0, &options, &budget, &mut out).unwrap();
    assert_eq!((out.as_str(), skipped), ("before Hi there\n", 0));
    // The most held at once is the eight glyphs, and the nine bytes of text.
    assert_eq!(peak, TextPeak { glyphs: 8, size: 9 });
    // The page's glyphs are released with it, and its text stays charged.
    assert_eq!(budget.used(LimitCode::TextGlyphCount), 0);
    assert_eq!(budget.used(LimitCode::TextSize), 9);

    // Eight glyphs are collected, and their eight bytes become nine.
    for (code, limit) in [(LimitCode::TextGlyphCount, 7), (LimitCode::TextSize, 8)] {
        let budget = Budget::default().with_limit(code, Some(limit));
        let mut out = String::from("before ");
        let Err(Error::Limit(refusal)) = page_text(&bridged, 0, &options, &budget, &mut out) else {
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

#[test]
fn a_page_reads_what_it_reaches_but_not_the_other_pages() {
    let content = "BT /F1 12 Tf 10 50 Td (Hi) Tj ET";
    let objects = [
        "<< /Type /Catalog /Pages 2 0 R /Outlines 10 0 R \
         /OCProperties << /OCGs [9 0 R] /D << /OFF [] >> >> >>"
            .to_owned(),
        "<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 /MediaBox [0 0 200 100] \
         /Resources 11 0 R >>"
            .to_owned(),
        "<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Annots [5 0 R] >>".to_owned(),
        format!(
            "<< /Length {} >>\nstream\n{content}\nendstream",
            content.len()
        ),
        "<< /Type /Annot /Subtype /Link /Rect [0 0 10 10] /P 3 0 R \
         /A << /S /GoTo /D [6 0 R /Fit] >> /AP << /N 12 0 R >> >>"
            .to_owned(),
        "<< /Type /Page /Parent 2 0 R /Contents 7 0 R >>".to_owned(),
        "<< /Length 0 >>\nstream\n\nendstream".to_owned(),
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>".to_owned(),
        "<< /Type /OCG /Name (Layer) >>".to_owned(),
        "<< /Type /Outlines >>".to_owned(),
        "<< /Font << /F1 8 0 R >> /XObject << /X 13 0 R >> >>".to_owned(),
        "<< /Type /XObject /Subtype /Form /BBox [0 0 10 10] /Length 0 >>\nstream\n\nendstream"
            .to_owned(),
        // A form whose resources are those that name it.
        "<< /Type /XObject /Subtype /Form /BBox [0 0 10 10] /Resources 11 0 R /Length 0 >>\n\
         stream\n\nendstream"
            .to_owned(),
        "(unrelated)".to_owned(),
    ];
    let budget = Budget::default();
    let index = Index::open(Bytes::new(pdf_of(&objects)), &budget).unwrap();
    let document = Document::new(Arc::new(FileBase::new(index, &budget).unwrap()), None);
    let version = document.current();
    let pages = PageIndex::build(&mut Reads::new(&version, &budget)).unwrap();

    let mut reads = Reads::new(&version, &budget);
    read_page(&mut reads, pages.get(0).unwrap()).unwrap();
    let read = reads.finish().unwrap();

    // The link's target page is read but not entered, so its content (7)
    // is not; neither are the outlines (10) nor the unrelated object (14).
    assert!(read.trailer());
    assert_eq!(read.objects(), [1, 2, 3, 4, 5, 6, 8, 9, 11, 12, 13]);
}

#[test]
fn a_page_read_set_fits_a_small_budget_with_sparse_object_numbers() {
    let budget = Budget::default();
    let index = Index::open(Bytes::new(pdf_with("")), &budget).unwrap();
    let document = Document::new(Arc::new(FileBase::new(index, &budget).unwrap()), None);
    let mut transaction = document.begin();
    transaction
        .write(u32::MAX - 1, &pyropdf_store::value::Value::Null, &budget)
        .unwrap();
    let version = transaction.commit(&budget).unwrap();
    let pages = PageIndex::build(&mut Reads::new(&version, &budget)).unwrap();

    let small = Budget::default().with_limit(LimitCode::MemorySize, Some(65_536));
    let mut reads = Reads::new(&version, &small);
    read_page(&mut reads, pages.get(0).unwrap()).unwrap();
    assert_eq!(reads.finish().unwrap().objects(), [1, 2, 3, 4, 5]);
}

/// The content that shows 日本語 at 40 points.
const SHOWN: &str = "BT /F1 40 Tf 20 30 Td <65E5672C8A9E> Tj ET";

/// Returns a page that shows 日本語 at 40 points in MS-Gothic, which the file
/// does not embed, with these entries in its descendant font.
fn nonembedded_japanese(descendant: &str) -> Vec<u8> {
    nonembedded_page(SHOWN, descendant)
}

/// Returns a page of `content` with MS-Gothic, which the file does not embed,
/// as `/F1`, with these entries in its descendant font.
fn nonembedded_page(content: &str, descendant: &str) -> Vec<u8> {
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
        "<< /Type /Font /Subtype /Type0 /BaseFont /MS-Gothic /Encoding /UniJIS-UCS2-H \
         /DescendantFonts [6 0 R] >>"
            .to_owned(),
        format!(
            "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /MS-Gothic /CIDSystemInfo \
             << /Registry (Adobe) /Ordering (Japan1) /Supplement 2 >> /DW 1000 {descendant} >>"
        ),
    ];
    pdf_of(&objects)
}

/// A subset of Noto Sans JP, which draws 日本語.
const SUBSET: &[u8] = include_bytes!("../fonts/testdata/NotoSansJP-subset.otf");

const EXACT: TextOptions = TextOptions {
    clip: None,
    keep_clipped: false,
    unmapped: Unmapped::Skip,
    expand_ligatures: false,
    normalize_whitespace: false,
    dehyphenate: false,
    exact_bboxes: true,
    styles: false,
};

/// Returns faces that draw Japanese in `source`.
fn japanese(source: Source) -> Fallback {
    let mut fallback = Fallback::default();
    fallback.set(Language::Japanese, false, Some(source));
    fallback
}

/// Returns the subset as the face of Japanese, as a program loads it.
fn loaded_japanese() -> Fallback {
    japanese(Source::Face(Arc::new(Face::read(SUBSET, 0).unwrap())))
}

/// Returns the box and the ascender of the first span of page 0, as the
/// text options view it, in a layout through `fallback`.
fn first_span(bridged: &Bridged, fallback: &Fallback, options: &TextOptions) -> (BBox, f64) {
    let layout = page_layout(
        bridged,
        fallback,
        0,
        options,
        false,
        None,
        &Budget::default(),
    )
    .unwrap();
    let span = &layout.blocks[0].1[0].1[0];
    (span.0, span.6.3)
}

#[test]
fn exact_boxes_measure_a_font_that_a_document_does_not_embed_on_its_face() {
    let bridged = Bridged::of_bytes(nonembedded_japanese(""));
    let none = Fallback::default();
    let face = loaded_japanese();
    let plain = TextOptions::default();

    // Without a face the glyphs have no outline, so they keep the font's box.
    let (font_box, ascender) = first_span(&bridged, &none, &plain);
    assert_eq!(first_span(&bridged, &none, &EXACT), (font_box, ascender));
    // A face moves the box to the outlines of the glyphs that it draws, inside
    // the three glyphs' 120 points. It describes no font of the PDF, so the
    // font keeps its metrics.
    let (ink, measured) = first_span(&bridged, &face, &EXACT);
    assert_ne!(ink, font_box);
    assert_eq!(measured, ascender);
    assert!(ink.0 > 20.0 && ink.2 < 140.0, "{ink:?}");
    // Other text is the same under any faces.
    assert_eq!(first_span(&bridged, &face, &plain), (font_box, ascender));
    // A search of exact boxes finds the same box.
    let (hits, _) = page_search(
        &bridged,
        &face,
        0,
        &EXACT,
        "日本語",
        false,
        &Budget::default(),
    )
    .unwrap();
    let SearchHits::Boxes(hits) = hits else {
        panic!("the search asked for boxes");
    };
    assert_eq!(hits, [ink]);
}

#[test]
fn only_exact_boxes_of_a_font_that_is_not_embedded_read_a_face() {
    let name = format!("pyropdf-extract-face-{}.otf", std::process::id());
    let path = std::env::temp_dir().join(name);
    std::fs::write(&path, SUBSET).unwrap();
    let file = || Source::File(Arc::new(FontFile::new(path.clone(), None)));
    let nonembedded = Bridged::of_bytes(nonembedded_japanese(""));
    let helvetica = Bridged::of_bytes(pdf_with("BT /F1 12 Tf 10 50 Td (Hi there) Tj ET"));

    // A face is read once, so one that these calls had read would still draw
    // after its file is gone.
    let unread = japanese(file());
    let read = japanese(file());
    first_span(&helvetica, &unread, &EXACT);
    first_span(&nonembedded, &unread, &TextOptions::default());
    let (ink, _) = first_span(&nonembedded, &read, &EXACT);
    std::fs::remove_file(&path).unwrap();

    let none = Fallback::default();
    assert_eq!(
        first_span(&nonembedded, &unread, &EXACT),
        first_span(&nonembedded, &none, &EXACT)
    );
    assert_eq!(first_span(&nonembedded, &read, &EXACT).0, ink);
    assert_ne!(ink, first_span(&nonembedded, &none, &EXACT).0);
}

#[test]
fn a_stretched_face_keeps_the_advances_and_the_words_of_the_page() {
    // Half the widths of the face's glyphs, which hayro draws at half their
    // width, by a transform that would halve their advances too.
    let bridged = Bridged::of_bytes(nonembedded_japanese("/W [0 65535 500]"));
    let layout = page_layout(
        &bridged,
        &loaded_japanese(),
        0,
        &EXACT,
        false,
        None,
        &Budget::default(),
    )
    .unwrap();
    let line = &layout.blocks[0].1[0];
    assert_eq!(line.2.len(), 1);
    assert_eq!(line.2[0].1, "日本語");
    // The ink of the three glyphs fills most of the 60 points of their widths.
    let ink = line.2[0].0;
    assert!(ink.0 > 20.0 && ink.2 < 80.0 && ink.2 > 70.0, "{ink:?}");
}

#[test]
fn actual_text_spans_the_ink_of_its_glyphs_on_the_face() {
    let plain = Bridged::of_bytes(nonembedded_japanese(""));
    let marked = format!("/Span << /ActualText (word) >> BDC {SHOWN} EMC");
    let marked = Bridged::of_bytes(nonembedded_page(&marked, ""));
    let face = loaded_japanese();

    let (whole, _) = first_span(&plain, &face, &EXACT);
    let layout = page_layout(&marked, &face, 0, &EXACT, true, None, &Budget::default()).unwrap();
    let span = &layout.blocks[0].1[0].1[0];
    assert_eq!(span.1.0, "word");
    assert_eq!(span.0, whole);
}

#[test]
fn a_face_that_does_not_load_leaves_the_boxes_that_the_page_gives() {
    let bridged = Bridged::of_bytes(nonembedded_japanese(""));
    // hayro then shows these codes in no font at all.
    let name = format!("pyropdf-extract-no-face-{}.otf", std::process::id());
    let path = std::env::temp_dir().join(name);
    std::fs::write(&path, b"no font").unwrap();
    let broken = japanese(Source::File(Arc::new(FontFile::new(path.clone(), None))));
    let measured = first_span(&bridged, &broken, &EXACT);
    std::fs::remove_file(&path).unwrap();
    assert_eq!(measured, first_span(&bridged, &Fallback::default(), &EXACT));
}

/// Returns a page that shows two glyphs at 40 points in a font named MS-Gothic
/// that the file embeds: the subset, in a stream of hexadecimal digits.
fn embedded_japanese() -> Vec<u8> {
    let content = "BT /F1 40 Tf 20 30 Td <002B002C> Tj ET";
    let program: String = SUBSET.iter().map(|byte| format!("{byte:02X}")).collect();
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
        "<< /Type /Font /Subtype /Type0 /BaseFont /MS-Gothic /Encoding /Identity-H \
         /DescendantFonts [6 0 R] >>"
            .to_owned(),
        "<< /Type /Font /Subtype /CIDFontType0 /BaseFont /MS-Gothic /CIDSystemInfo \
         << /Registry (Adobe) /Ordering (Japan1) /Supplement 2 >> /DW 1000 \
         /FontDescriptor 7 0 R >>"
            .to_owned(),
        "<< /Type /FontDescriptor /FontName /MS-Gothic /Flags 4 /FontBBox [0 -120 1000 880] \
         /ItalicAngle 0 /Ascent 880 /Descent -120 /CapHeight 700 /StemV 80 /FontFile3 8 0 R >>"
            .to_owned(),
        format!(
            "<< /Subtype /OpenType /Filter /ASCIIHexDecode /Length {} >>\nstream\n{program}>\nendstream",
            program.len() + 1
        ),
    ];
    pdf_of(&objects)
}

/// Returns the content of page 0 through `fallback`.
fn content_on(bridged: &Bridged, fallback: &Fallback, kinds: ItemKinds) -> PageContent {
    page_content(bridged, fallback, 0, kinds, true, None, &Budget::default()).unwrap()
}

/// Returns the text runs of `content`.
fn text_runs(content: &PageContent) -> Vec<&TextRunTuple> {
    content
        .items
        .iter()
        .filter_map(|item| match item {
            ContentItem::Text(run) => Some(&**run),
            _ => None,
        })
        .collect()
}

#[test]
fn text_runs_describe_a_font_that_a_document_does_not_embed_as_its_face() {
    let bridged = Bridged::of_bytes(nonembedded_japanese(""));
    let plain = content_on(&bridged, &Fallback::default(), ALL);
    let faced = content_on(&bridged, &loaded_japanese(), ALL);
    let (plain_run, faced_run) = (text_runs(&plain)[0], text_runs(&faced)[0]);

    // Without a face the glyphs have no outline, so the run lies on its
    // baseline, from its first origin to the end of its last advance.
    assert!(plain_run.0.4.iter().all(Option::is_none));
    assert_eq!(plain_run.5, (20.0, 70.0, 140.0, 70.0));
    assert_eq!(plain.fonts[plain_run.1 as usize].2, None);

    // A face gives each glyph the ID and the ink of what it draws, and the run
    // the box around that ink, inside the three glyphs' 120 points.
    assert_ne!(faced_run.0.0, plain_run.0.0);
    assert!(faced_run.0.0.iter().all(Option::is_some));
    let boxes: Vec<BBox> = faced_run.0.4.iter().map(|ink| ink.unwrap()).collect();
    let union = boxes.iter().fold(boxes[0], |all, one| {
        (
            all.0.min(one.0),
            all.1.min(one.1),
            all.2.max(one.2),
            all.3.max(one.3),
        )
    });
    assert_eq!(faced_run.5, union);
    assert!(union.0 > 20.0 && union.2 < 140.0, "{union:?}");
    // The font keeps its name, and reads the rest of its record from the face.
    let (plain_font, faced_font) = (
        &plain.fonts[plain_run.1 as usize],
        &faced.fonts[faced_run.1 as usize],
    );
    assert_eq!(faced_font.0, plain_font.0);
    assert!(faced_font.2.is_some());

    // The glyphs keep their text and places.
    assert_eq!(faced_run.0.1, plain_run.0.1);
    assert_eq!(faced_run.0.2, plain_run.0.2);
    assert_eq!(faced_run.0.3, plain_run.0.3);
}

#[test]
fn a_stretched_face_keeps_the_matrices_and_the_advances_of_a_text_run() {
    // Half the widths of the face's glyphs, which hayro draws at half their
    // width, by a transform that would halve their advances too.
    let bridged = Bridged::of_bytes(nonembedded_japanese("/W [0 65535 500]"));
    let plain = content_on(&bridged, &Fallback::default(), ALL);
    let faced = content_on(&bridged, &loaded_japanese(), ALL);
    let (plain_run, faced_run) = (text_runs(&plain)[0], text_runs(&faced)[0]);
    assert_eq!(faced_run.0.2, plain_run.0.2);
    assert_eq!(faced_run.0.3, plain_run.0.3);
    // The ink of the three glyphs fills most of the 60 points of their widths.
    let ink = faced_run.5;
    assert!(ink.0 > 20.0 && ink.2 < 80.0 && ink.2 > 70.0, "{ink:?}");
}

#[test]
fn every_text_run_reads_its_own_glyphs_on_the_faces() {
    // A run in fill and stroke, whose stroke shows its glyph again, between
    // two others: the places stay in step.
    let content = "BT /F1 40 Tf 20 30 Td <65E5> Tj ET \
                   BT 2 Tr /F1 40 Tf 60 30 Td <672C> Tj ET \
                   BT 0 Tr /F1 40 Tf 100 30 Td <8A9E> Tj ET";
    let bridged = Bridged::of_bytes(nonembedded_page(content, ""));
    let faced = content_on(&bridged, &loaded_japanese(), ALL);
    let runs = text_runs(&faced);
    assert_eq!(runs.len(), 3);
    assert_eq!(runs[1].2, RENDER_FILL_STROKE);

    // The three runs draw what one run of the three glyphs draws.
    let whole = Bridged::of_bytes(nonembedded_japanese(""));
    let whole = content_on(&whole, &loaded_japanese(), ALL);
    let whole = &text_runs(&whole)[0].0;
    for (at, run) in runs.iter().enumerate() {
        assert_eq!(run.0.0, [whole.0[at]]);
        assert_eq!(run.0.4, [whole.4[at]]);
    }
}

#[test]
fn a_clip_keeps_a_text_run_by_its_box_on_the_face() {
    let bridged = Bridged::of_bytes(nonembedded_japanese(""));
    let budget = Budget::default();
    // Above the baseline at 70, inside the ink.
    let clip = Some((20.0, 45.0, 140.0, 50.0));
    let without = page_content(&bridged, &Fallback::default(), 0, ALL, true, clip, &budget);
    assert!(without.unwrap().items.is_empty());
    let with = page_content(&bridged, &loaded_japanese(), 0, ALL, true, clip, &budget);
    assert_eq!(with.unwrap().items.len(), 1);
}

#[test]
fn only_text_runs_of_a_font_that_is_not_embedded_read_a_face() {
    let name = format!("pyropdf-content-face-{}.otf", std::process::id());
    let path = std::env::temp_dir().join(name);
    std::fs::write(&path, SUBSET).unwrap();
    let file = || Source::File(Arc::new(FontFile::new(path.clone(), None)));
    let nonembedded = Bridged::of_bytes(nonembedded_japanese(""));
    let helvetica = Bridged::of_bytes(pdf_with("BT /F1 12 Tf 10 50 Td (Hi there) Tj ET"));
    let embedded = Bridged::of_bytes(embedded_japanese());
    let without_runs = ItemKinds {
        text_runs: false,
        ..ALL
    };

    // A face is read once, so one that these calls had read would still draw
    // after its file is gone.
    let unread = japanese(file());
    let read = japanese(file());
    content_on(&helvetica, &unread, ALL);
    let drawn = content_on(&embedded, &unread, ALL);
    content_on(&nonembedded, &unread, without_runs);
    let measured = content_on(&nonembedded, &read, ALL);
    std::fs::remove_file(&path).unwrap();

    let none = Fallback::default();
    // The embedded font draws its own ink, the same under any faces.
    assert!(text_runs(&drawn)[0].0.4.iter().all(Option::is_some));
    let own = content_on(&embedded, &none, ALL);
    assert_eq!(text_runs(&drawn)[0], text_runs(&own)[0]);

    let (kept, plain) = (
        content_on(&nonembedded, &unread, ALL),
        content_on(&nonembedded, &none, ALL),
    );
    assert_eq!(text_runs(&kept)[0], text_runs(&plain)[0]);
    let again = content_on(&nonembedded, &read, ALL);
    assert_eq!(text_runs(&again)[0], text_runs(&measured)[0]);
    assert_ne!(text_runs(&measured)[0], text_runs(&plain)[0]);
}

#[test]
fn a_face_counts_no_elements_of_a_text_run() {
    let bridged = Bridged::of_bytes(nonembedded_japanese(""));
    let plain = Budget::default();
    page_content(&bridged, &Fallback::default(), 0, ALL, true, None, &plain).unwrap();
    let elements = plain.used(LimitCode::ContentElementCount);
    assert!(elements > 0);

    let faces = loaded_japanese();
    let limit = Some(elements);
    let budget = Budget::default().with_limit(LimitCode::ContentElementCount, limit);
    let content = page_content(&bridged, &faces, 0, ALL, true, None, &budget).unwrap();
    assert_eq!(budget.used(LimitCode::ContentElementCount), elements);
    assert!(text_runs(&content)[0].0.4.iter().all(Option::is_some));

    let limit = Some(elements - 1);
    let budget = Budget::default().with_limit(LimitCode::ContentElementCount, limit);
    assert!(matches!(
        page_content(&bridged, &faces, 0, ALL, true, None, &budget),
        Err(Error::Limit(_))
    ));
}

#[test]
fn a_face_that_does_not_load_leaves_the_text_runs_that_the_page_gives() {
    let bridged = Bridged::of_bytes(nonembedded_japanese(""));
    // hayro then shows these codes in no font at all.
    let name = format!("pyropdf-content-no-face-{}.otf", std::process::id());
    let path = std::env::temp_dir().join(name);
    std::fs::write(&path, b"no font").unwrap();
    let broken = japanese(Source::File(Arc::new(FontFile::new(path.clone(), None))));
    let measured = content_on(&bridged, &broken, ALL);
    std::fs::remove_file(&path).unwrap();
    let plain = content_on(&bridged, &Fallback::default(), ALL);
    assert_eq!(text_runs(&measured)[0], text_runs(&plain)[0]);
    assert_eq!(measured.fonts, plain.fonts);
}
