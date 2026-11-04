use skrifa::{FontRef, GlyphId, MetadataProvider};

use super::{Bitmaps, empty_procedure, procedure};
use crate::fonts::embed::{Face, FaceError};

const SBIX: &[u8] = include_bytes!("../testdata/colour-bitmaps-sbix.ttf");
const CBDT: &[u8] = include_bytes!("../testdata/colour-bitmaps-cbdt.ttf");
const SBIX_ONLY: &[u8] = include_bytes!("../testdata/colour-bitmaps-sbix-only.ttf");
const CBDT_ONLY: &[u8] = include_bytes!("../testdata/colour-bitmaps-cbdt-only.ttf");
const JPEG: &[u8] = include_bytes!("../testdata/colour-sbix-jpeg.ttf");
const RAW: &[u8] = include_bytes!("../testdata/colour-cbdt-raw.ttf");
const EBLC: &[u8] = include_bytes!("../testdata/colour-eblc.ttf");
const EBLC_ONLY: &[u8] = include_bytes!("../testdata/colour-eblc-only.ttf");
const COLR: &[u8] = include_bytes!("../testdata/colour-colr1.ttf");

fn glyph(font: &FontRef<'_>, character: char) -> GlyphId {
    font.charmap().map(character).unwrap()
}

#[test]
fn a_drawing_takes_the_smallest_strike_that_reaches_it() {
    for program in [SBIX, CBDT] {
        let font = FontRef::new(program).unwrap();
        let bitmaps = Bitmaps::read(&font).unwrap();
        let red = glyph(&font, 'R');
        assert_eq!(bitmaps.best(red, 10.0).unwrap().ppem, 32);
        assert_eq!(bitmaps.best(red, 32.0).unwrap().ppem, 32);
        assert_eq!(bitmaps.best(red, 33.0).unwrap().ppem, 120);
        assert_eq!(bitmaps.best(red, 500.0).unwrap().ppem, 120);
        // A glyph that one strike holds takes it at any size.
        let green = glyph(&font, 'G');
        assert_eq!(bitmaps.best(green, 10.0).unwrap().ppem, 120);
        let translucent = glyph(&font, 'T');
        assert_eq!(bitmaps.best(translucent, 500.0).unwrap().ppem, 32);
    }
}

#[test]
fn an_image_covers_one_em_from_the_baseline_in_font_units() {
    for program in [SBIX, CBDT, SBIX_ONLY, CBDT_ONLY] {
        let font = FontRef::new(program).unwrap();
        let bitmaps = Bitmaps::read(&font).unwrap();
        let red = glyph(&font, 'R');
        for ppem in [32.0, 120.0] {
            let bitmap = bitmaps.best(red, ppem).unwrap();
            assert_eq!(bitmap.rect, [0.0, 0.0, 1000.0, 1000.0]);
            assert!(bitmap.png.starts_with(b"\x89PNG"));
        }
    }
}

#[test]
fn a_glyph_with_no_png_has_no_bitmap() {
    for program in [SBIX, CBDT] {
        let font = FontRef::new(program).unwrap();
        let bitmaps = Bitmaps::read(&font).unwrap();
        assert!(bitmaps.has(glyph(&font, 'R')));
        assert!(!bitmaps.has(glyph(&font, 'O')));
        assert!(!bitmaps.has(glyph(&font, ' ')));
        assert!(!bitmaps.has(GlyphId::new(60_000)));
    }
    // A JPEG or raw strike, a monochrome table, and COLR hold no PNG.
    for program in [JPEG, RAW, EBLC, COLR] {
        let font = FontRef::new(program).unwrap();
        let bitmaps = Bitmaps::read(&font).unwrap();
        assert!(!bitmaps.has(glyph(&font, 'A')));
    }
}

#[test]
fn a_dupe_record_shows_the_glyph_that_it_names() {
    let font = FontRef::new(SBIX).unwrap();
    let bitmaps = Bitmaps::read(&font).unwrap();
    let (red, dupe) = (glyph(&font, 'R'), glyph(&font, 'D'));
    assert_ne!(red, dupe);
    assert_eq!(bitmaps.best(dupe, 120.0), bitmaps.best(red, 120.0));
    // The CBDT face has no such record.
    let font = FontRef::new(CBDT).unwrap();
    assert!(!Bitmaps::read(&font).unwrap().has(glyph(&font, 'D')));
}

#[test]
fn a_face_loads_with_outlines_or_colour_bitmaps() {
    assert!(Face::read(SBIX, 0).unwrap().outlines);
    assert!(Face::read(CBDT, 0).unwrap().outlines);
    assert!(!Face::read(SBIX_ONLY, 0).unwrap().outlines);
    assert!(!Face::read(CBDT_ONLY, 0).unwrap().outlines);
    assert!(matches!(
        Face::read(EBLC_ONLY, 0),
        Err(FaceError::Unsupported(_))
    ));
}

#[test]
fn a_procedure_sets_its_width_and_paints_the_image_over_its_box() {
    assert_eq!(
        procedure(1000.0, [10.0, -2.5, 900.0, 800.0], "g1s32"),
        b"1000 0 d0\nq 900 0 0 800 10 -2.5 cm /g1s32 Do Q"
    );
    assert_eq!(empty_procedure(300.0), b"300 0 d0");
}
