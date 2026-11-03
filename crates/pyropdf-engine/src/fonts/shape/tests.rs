use std::sync::Arc;

use harfrust::{Buffer, Font, ShapeOptions, ShaperFont};
use proptest::prelude::*;
use pyropdf_store::budget::{Budget, LimitCode};
use skrifa::raw::types::Tag;
use skrifa::{FontRef, MetadataProvider};

use super::super::sfnt;
use super::{ShapeError, ShapedGlyph, ShapingFace};

const JAPANESE: &[u8] = include_bytes!("../testdata/NotoSansJP-subset.otf");
const HEBREW: &[u8] = include_bytes!("../testdata/NotoSansHebrew-subset.ttf");
const ARABIC: &[u8] = include_bytes!("../testdata/NotoSansArabic-Variable-subset.ttf");

fn face(program: &[u8]) -> ShapingFace {
    ShapingFace::new(Arc::new(program.to_vec()), 0).unwrap()
}

fn shape(program: &[u8], text: &str) -> Vec<ShapedGlyph> {
    let budget = Budget::default();
    face(program).shape(text, &budget).unwrap().into_vec()
}

/// The glyph IDs and advances that harfrust gives the whole text as one run.
fn harfrust(program: &[u8], text: &str) -> Vec<(u32, i32)> {
    let font = Font::new(program.to_vec(), 0).unwrap();
    let shaper = ShaperFont::new(&font);
    let mut buffer = Buffer::new();
    buffer.push_str(text);
    buffer.guess_segment_properties();
    harfrust::shape(&shaper, &mut buffer, ShapeOptions::new()).unwrap();
    buffer
        .glyph_infos()
        .iter()
        .zip(buffer.glyph_positions())
        .map(|(info, position)| (info.glyph_id, position.x_advance))
        .collect()
}

fn mapped<'t>(text: &'t str, glyphs: &[ShapedGlyph]) -> Vec<&'t str> {
    glyphs
        .iter()
        .map(|glyph| &text[glyph.text.clone()])
        .collect()
}

/// The glyphs' texts in logical order, which concatenate to the input.
fn logical(text: &str, glyphs: &[ShapedGlyph]) -> String {
    let mut ranges: Vec<_> = glyphs.iter().map(|glyph| glyph.text.clone()).collect();
    ranges.sort_by_key(|range| range.start);
    ranges.into_iter().map(|range| &text[range]).collect()
}

#[test]
fn a_single_script_run_is_what_harfrust_gives() {
    for (program, text) in [
        (JAPANESE, "office fit AVA"),
        (JAPANESE, "日本語のテキスト、かな。"),
        (HEBREW, "שָׁלוֹם עולם"),
        (ARABIC, "مرحبا بالعالم"),
    ] {
        let glyphs = shape(program, text);
        let ours: Vec<_> = glyphs
            .iter()
            .map(|glyph| (glyph.glyph, glyph.x_advance))
            .collect();
        assert_eq!(ours, harfrust(program, text), "{text}");
        assert!(glyphs.iter().all(|glyph| glyph.glyph != 0), "{text}");
        assert_eq!(logical(text, &glyphs), text);
    }
}

#[test]
fn hebrew_comes_out_in_visual_order_with_each_point_on_its_own_character() {
    let text = "שָׁלוֹם";
    let glyphs = shape(HEBREW, text);

    assert!(glyphs.iter().all(|glyph| glyph.rtl));
    // Visual order runs from the final mem to the shin; the points keep
    // their own characters, so ToUnicode needs no ActualText.
    assert_eq!(mapped(text, &glyphs).first(), Some(&"ם"));
    assert_eq!(
        mapped(text, &glyphs)
            .last()
            .map(|text| text.chars().count()),
        Some(1)
    );
    assert!(
        mapped(text, &glyphs)
            .iter()
            .all(|text| text.chars().count() == 1)
    );
    let points = glyphs.iter().filter(|glyph| glyph.x_advance == 0).count();
    assert_eq!(points, 3);
}

#[test]
fn arabic_joining_forms_map_to_their_characters() {
    let text = "مرحبا";
    let glyphs = shape(ARABIC, text);

    assert!(glyphs.iter().all(|glyph| glyph.rtl));
    let mut letters = mapped(text, &glyphs);
    letters.retain(|text| !text.is_empty());
    letters.reverse();
    assert_eq!(letters.concat(), text);
}

#[test]
fn mixed_directions_split_into_runs_in_visual_order() {
    let text = "AVA שלום 123";
    let glyphs = shape(JAPANESE, text);

    // UAX #9 puts a number after right-to-left text inside its embedding,
    // with the space after the Hebrew in the Hebrew's run, so the line reads
    // "AVA 123 םולש" from left to right. The Japanese face has no Hebrew
    // glyphs; the order is still visual.
    let directions: Vec<bool> = glyphs.iter().map(|glyph| glyph.rtl).collect();
    assert_eq!(
        directions,
        [
            false, false, false, false, false, false, false, true, true, true, true, true
        ]
    );
    assert_eq!(mapped(text, &glyphs)[4..7], ["1", "2", "3"]);
    assert_eq!(mapped(text, &glyphs)[7..], [" ", "ם", "ו", "ל", "ש"]);
    assert_eq!(logical(text, &glyphs), text);
}

#[test]
fn a_right_to_left_paragraph_puts_its_number_on_the_left() {
    let text = "שלום 123";
    let glyphs = shape(HEBREW, text);

    assert_eq!(&text[glyphs[0].text.clone()], "1");
    assert!(!glyphs[0].rtl);
    assert_eq!(&text[glyphs.last().unwrap().text.clone()], "ש");
}

#[test]
fn script_runs_split_latin_from_japanese() {
    let text = "AVA日本";
    let glyphs = shape(JAPANESE, text);

    assert_eq!(mapped(text, &glyphs), ["A", "V", "A", "日", "本"]);
    assert_eq!(logical(text, &glyphs), text);
}

#[test]
fn the_budget_limits_the_characters_and_the_glyphs() {
    let face = face(JAPANESE);
    let budget = Budget::default().with_limit(LimitCode::ShapedGlyphCount, Some(3));

    let refusal = face.shape("Hello", &budget).unwrap_err();
    assert!(
        matches!(refusal, ShapeError::Limit(limit) if limit.code() == LimitCode::ShapedGlyphCount)
    );
    assert_eq!(face.shape("Hel", &budget).unwrap().len(), 3);
}

/// A face whose default feature multiplies the glyph of `A` by 255 four times
/// over, far beyond what harfrust lets a short text grow to.
fn expanding() -> Vec<u8> {
    const LOOKUPS: u16 = 4;
    const COPIES: u16 = 255;
    let font = FontRef::new(JAPANESE).unwrap();
    let glyph = u16::try_from(font.charmap().map('A').unwrap().to_u32()).unwrap();
    let mut gsub = vec![1, 0, 10, 30, 42 + 2 * LOOKUPS];
    // `DFLT`, whose default language system uses the one feature.
    gsub.extend([1, 0x4446, 0x4C54, 8, 4, 0, 0, 0xFFFF, 1, 0]);
    // `ccmp`, which runs every lookup.
    gsub.extend([1, 0x6363, 0x6D70, 8, 0, LOOKUPS]);
    gsub.extend(0..LOOKUPS);
    // The lookups, which share one multiple substitution of the glyph.
    gsub.push(LOOKUPS);
    gsub.extend((0..LOOKUPS).map(|_| 2 + 2 * LOOKUPS));
    gsub.extend([2, 0, 1, 8, 1, 10 + 2 * COPIES, 1, 8, COPIES]);
    gsub.extend((0..COPIES).map(|_| glyph));
    gsub.extend([1, 1, glyph]);
    let table = |tag: &[u8; 4]| font.table_data(Tag::new(tag)).unwrap().as_bytes().to_vec();
    sfnt::write(
        b"OTTO",
        &[
            (*b"CFF ", table(b"CFF ")),
            (
                *b"GSUB",
                gsub.iter().flat_map(|word| word.to_be_bytes()).collect(),
            ),
            (*b"cmap", table(b"cmap")),
            (*b"head", table(b"head")),
            (*b"hhea", table(b"hhea")),
            (*b"hmtx", table(b"hmtx")),
            (*b"maxp", table(b"maxp")),
        ],
    )
    .unwrap()
}

#[test]
fn a_face_that_asks_for_more_than_the_shaper_allows_is_a_font_error() {
    let face = face(&expanding());
    let budget = Budget::default();

    assert_eq!(face.shape("V", &budget).unwrap().len(), 1);
    assert!(matches!(face.shape("A", &budget), Err(ShapeError::Font(_))));
}

#[test]
fn an_invalid_program_is_a_font_error() {
    for program in [&b""[..], b"not a font", &JAPANESE[..12]] {
        assert!(matches!(
            ShapingFace::new(Arc::new(program.to_vec()), 0),
            Err(ShapeError::Font(_))
        ));
    }
    assert!(matches!(
        ShapingFace::new(Arc::new(HEBREW.to_vec()), 3),
        Err(ShapeError::Font(_))
    ));
}

#[test]
fn empty_text_shapes_to_nothing() {
    assert!(shape(HEBREW, "").is_empty());
}

const POOL: &[char] = &[
    'o', 'f', 'i', 't', 'A', 'V', ' ', ',', '1', '2', '3', '日', '本', '語', 'の', 'か', 'な',
    '。', 'ש', 'ל', 'ו', 'ם', 'ע', '\u{05B8}', '\u{05C1}', '\u{05B9}', 'م', 'ر', 'ح', 'ب', 'ا',
    'ل', 'ع', '\u{200E}', '\u{200F}', '\u{0301}',
];

proptest! {
    #[test]
    fn cluster_texts_concatenate_to_the_input(
        indexes in proptest::collection::vec(0..POOL.len(), 0..24),
        font in 0..3_usize,
    ) {
        let text: String = indexes.into_iter().map(|index| POOL[index]).collect();
        let program = [JAPANESE, HEBREW, ARABIC][font];
        let glyphs = shape(program, &text);

        prop_assert_eq!(logical(&text, &glyphs), text.clone());
        for glyph in &glyphs {
            prop_assert!(glyph.cluster.start <= glyph.text.start);
            prop_assert!(glyph.text.end <= glyph.cluster.end);
        }
    }
}
