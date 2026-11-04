use std::sync::Arc;

use proptest::prelude::*;
use pyropdf_store::budget::{Budget, CancellationToken, DocumentLimits, LimitCode};
use skrifa::FontRef;
use skrifa::raw::types::Tag;

use super::super::embed::Face;
use super::super::sfnt;
use super::super::shape::{ShapeError, ShapingFace};
use super::{MAX_LOOKUP_STEPS, refused_colour_glyph};

const COLR0: &[u8] = include_bytes!("../testdata/colour-colr0.ttf");
const COLR1: &[u8] = include_bytes!("../testdata/colour-colr1.ttf");
const SBIX: &[u8] = include_bytes!("../testdata/colour-sbix.ttf");
const CBDT: &[u8] = include_bytes!("../testdata/colour-cbdt.ttf");
const SVG: &[u8] = include_bytes!("../testdata/colour-svg.ttf");
const SVG_CFF: &[u8] = include_bytes!("../testdata/colour-svg-cff.otf");
const EBLC: &[u8] = include_bytes!("../testdata/colour-eblc.ttf");
const HEBREW: &[u8] = include_bytes!("../testdata/NotoSansHebrew-subset.ttf");

const COLOUR: [&[u8]; 6] = [COLR0, COLR1, SBIX, CBDT, SVG, SVG_CFF];
/// The glyph of `A`, of `space`, and how many a colour face has
/// (`colour_faces.py`).
const GLYPH_A: u16 = 1;
const GLYPH_SPACE: u16 = 6;
const GLYPHS: u16 = 9;

fn refused(program: &[u8], text: &str) -> Result<Option<char>, ShapeError> {
    let budget = Budget::default();
    let face = Face::read(program, 0).unwrap();
    let glyphs = ShapingFace::new(face.program.clone(), 0)
        .unwrap()
        .shape(text, &budget)
        .unwrap();
    refused_colour_glyph(&face, text, &glyphs, &budget)
}

/// Returns `program` with `tag` replaced by `table`, or added.
fn with_table(program: &[u8], tag: [u8; 4], table: Vec<u8>) -> Vec<u8> {
    let font = FontRef::new(program).unwrap();
    let mut tables: Vec<([u8; 4], Vec<u8>)> = font
        .table_directory
        .table_records()
        .iter()
        .map(|record| record.tag())
        .filter(|found| found.to_be_bytes() != tag)
        .map(|found| {
            let data = font.table_data(found).unwrap();
            (found.to_be_bytes(), data.as_bytes().to_vec())
        })
        .collect();
    tables.push((tag, table));
    tables.sort_by_key(|(tag, _)| Tag::new(tag));
    sfnt::write(&program[..4], &tables).unwrap()
}

/// Returns an `sbix` table of `strikes` offsets to one strike, which holds a
/// record for each glyph of `covered`.
fn sbix(strikes: usize, covered: &[u16]) -> Vec<u8> {
    let record = [&[0_u8, 0, 0, 0][..], b"png ", &[0; 4]].concat();
    let header = 4 + 4 * (usize::from(GLYPHS) + 1);
    let mut strike = [72_u16.to_be_bytes(), 72_u16.to_be_bytes()].concat();
    let mut at = header;
    for glyph in 0..=GLYPHS {
        strike.extend_from_slice(&u32::try_from(at).unwrap().to_be_bytes());
        if covered.contains(&glyph) {
            at += record.len();
        }
    }
    for _ in covered {
        strike.extend_from_slice(&record);
    }
    let mut table = [1_u16.to_be_bytes(), 1_u16.to_be_bytes()].concat();
    table.extend_from_slice(&u32::try_from(strikes).unwrap().to_be_bytes());
    let strike_at = 8 + 4 * strikes;
    for _ in 0..strikes {
        table.extend_from_slice(&u32::try_from(strike_at).unwrap().to_be_bytes());
    }
    table.extend_from_slice(&strike);
    table
}

#[test]
fn each_table_refuses_the_colour_glyphs_with_no_outline() {
    for program in COLOUR {
        assert_eq!(refused(program, "CA").unwrap(), Some('A'));
        assert_eq!(refused(program, "CD").unwrap(), Some('D'));
        assert_eq!(refused(program, "BCE \u{a0}").unwrap(), None);
    }
}

#[test]
fn a_face_with_no_colour_table_refuses_nothing() {
    assert_eq!(refused(HEBREW, "שלום").unwrap(), None);
    assert_eq!(refused(EBLC, "ABCDE").unwrap(), None);
}

#[test]
fn the_first_refused_glyph_of_the_text_names_its_character() {
    assert_eq!(refused(SBIX, "CCCCA").unwrap(), Some('A'));
    assert_eq!(refused(SBIX, "BDBA").unwrap(), Some('D'));
}

#[test]
fn a_record_with_no_data_covers_nothing() {
    let program = with_table(SBIX, *b"sbix", sbix(1, &[]));
    assert_eq!(refused(&program, "ABDE").unwrap(), None);
}

#[test]
fn any_graphic_type_of_a_record_covers_its_glyph() {
    let mut table = sbix(1, &[GLYPH_A]);
    let at = table.windows(4).position(|tag| tag == b"png ").unwrap();
    table[at..at + 4].copy_from_slice(b"emjc");
    let program = with_table(SBIX, *b"sbix", table);
    assert_eq!(refused(&program, "A").unwrap(), Some('A'));
}

#[test]
fn the_space_glyph_stands_for_the_characters_that_the_shaper_hides() {
    let program = with_table(SBIX, *b"sbix", sbix(1, &[GLYPH_SPACE]));
    assert_eq!(refused(&program, " ").unwrap(), None);
    // A zero width joiner is hidden as the space glyph.
    assert_eq!(refused(&program, "C\u{200d}C").unwrap(), None);
}

#[test]
fn a_lookup_that_walks_too_far_refuses_and_one_at_the_limit_does_not() {
    let at_limit = with_table(SBIX, *b"sbix", sbix(MAX_LOOKUP_STEPS, &[GLYPH_A]));
    assert_eq!(refused(&at_limit, "A").unwrap(), Some('A'));
    let over = with_table(SBIX, *b"sbix", sbix(MAX_LOOKUP_STEPS + 1, &[GLYPH_A]));
    assert!(matches!(refused(&over, "C"), Err(ShapeError::Font(_))));
}

#[test]
fn the_lookups_poll_the_budget() {
    let token = CancellationToken::new();
    let budget = Budget::new(&DocumentLimits::default()).with_cancellation(token.clone());
    let face = Face::read(COLR1, 0).unwrap();
    let glyphs = ShapingFace::new(face.program.clone(), 0)
        .unwrap()
        .shape("A", &budget)
        .unwrap();
    token.cancel();
    let Err(ShapeError::Limit(refusal)) = refused_colour_glyph(&face, "A", &glyphs, &budget) else {
        panic!("a cancelled budget refuses");
    };
    assert_eq!(refusal.code(), LimitCode::Cancelled);
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(64))]
    #[test]
    fn a_damaged_colour_face_never_panics(pick in any::<prop::sample::Index>(), cut in any::<prop::sample::Index>(), flips in prop::collection::vec((any::<prop::sample::Index>(), any::<u8>()), 0..16)) {
        let program = *pick.get(&COLOUR);
        let mut data = program[..cut.index(program.len() + 1)].to_vec();
        for (at, byte) in flips {
            if !data.is_empty() {
                let at = at.index(data.len());
                data[at] = byte;
            }
        }
        if let Ok(face) = Face::read(&data, 0) {
            let budget = Budget::default();
            let program = Arc::clone(&face.program);
            if let Ok(glyphs) = ShapingFace::new(program, 0).and_then(|shaper| shaper.shape("ABCDE \u{a0}", &budget)) {
                let _ = refused_colour_glyph(&face, "ABCDE \u{a0}", &glyphs, &budget);
            }
        }
    }
}
