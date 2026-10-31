use skrifa::raw::TableProvider;
use skrifa::raw::types::Tag;
use skrifa::{FontRef, GlyphId};

use super::super::cff;
use super::{Face, FaceError, Measure};

const JAPANESE: &[u8] = include_bytes!("../testdata/NotoSansJP-subset.otf");
const HEBREW: &[u8] = include_bytes!("../testdata/NotoSansHebrew-subset.ttf");
const ARABIC: &[u8] = include_bytes!("../testdata/NotoSansArabic-Variable-subset.ttf");

fn cff_table(program: &[u8]) -> Vec<u8> {
    FontRef::new(program)
        .unwrap()
        .table_data(Tag::new(b"CFF "))
        .unwrap()
        .as_bytes()
        .to_vec()
}

/// A collection of `faces`, each with its tables copied after the header.
fn collection(faces: &[&[u8]]) -> Vec<u8> {
    let header = 12 + 4 * faces.len();
    let mut out = Vec::new();
    out.extend_from_slice(b"ttcf");
    out.extend_from_slice(&[0, 1, 0, 0]);
    out.extend_from_slice(&u32::try_from(faces.len()).unwrap().to_be_bytes());
    out.resize(header, 0);
    for (at, face) in faces.iter().enumerate() {
        let start = out.len();
        out[12 + 4 * at..16 + 4 * at].copy_from_slice(&u32::try_from(start).unwrap().to_be_bytes());
        out.extend_from_slice(face);
        let count = usize::from(u16::from_be_bytes([face[4], face[5]]));
        for table in 0..count {
            let record = start + 12 + 16 * table + 8;
            let offset = u32::from_be_bytes(out[record..record + 4].try_into().unwrap());
            let moved = offset + u32::try_from(start).unwrap();
            out[record..record + 4].copy_from_slice(&moved.to_be_bytes());
        }
    }
    out
}

#[test]
fn faces_report_their_outlines_names_and_metrics() {
    let japanese = Face::read(JAPANESE, 0).unwrap();
    assert!(japanese.cff);
    assert_eq!(japanese.postscript_name, "NotoSansJP-Regular");
    assert_eq!(japanese.family, "Noto Sans JP");
    // The subset keeps no full name, so the family stands in.
    assert_eq!(japanese.full_name, "Noto Sans JP");
    assert_eq!(japanese.weight_axis, None);
    assert_eq!(
        (japanese.weight, japanese.bold, japanese.italic),
        (400, false, false)
    );
    assert!(!japanese.serif);
    assert_eq!(japanese.units_per_em, 1000);
    assert!(japanese.ascender > 0.0 && japanese.descender < 0.0);

    let hebrew = Face::read(HEBREW, 0).unwrap();
    assert!(!hebrew.cff);
    assert_eq!(hebrew.family, "Noto Sans Hebrew");
    assert_eq!(hebrew.full_name, "Noto Sans Hebrew Regular");
    let arabic = Face::read(ARABIC, 0).unwrap();
    assert!(!arabic.cff);
    assert!(arabic.family.starts_with("Noto Sans Arabic"));
    let [low, default, high] = arabic.weight_axis.unwrap();
    assert!(low <= default && default <= high && low < high);
}

#[test]
fn a_cid_keyed_charset_becomes_the_identity_with_the_same_outlines() {
    let original = cff_table(JAPANESE);
    assert!(
        cff::identity_charset(&original).unwrap().is_some(),
        "the subset keeps the source's CIDs"
    );
    let face = Face::read(JAPANESE, 0).unwrap();
    assert_eq!(cff::identity_charset(&cff_table(&face.program)), Ok(None));

    let before = FontRef::new(JAPANESE).unwrap();
    let measure = Measure::new(&face);
    let glyphs = before.maxp().unwrap().num_glyphs();
    assert_eq!(face.glyph_count, u32::from(glyphs));
    let ours = Face {
        program: JAPANESE.to_vec(),
        ..face.clone()
    };
    let theirs = Measure::new(&ours);
    for glyph in 0..u32::from(glyphs) {
        let glyph = GlyphId::new(glyph);
        assert_eq!(measure.ink(glyph), theirs.ink(glyph));
        assert_eq!(measure.advance(glyph), theirs.advance(glyph));
    }
}

#[test]
fn a_truetype_face_is_embedded_as_it_is() {
    let face = Face::read(HEBREW, 0).unwrap();
    let ours = FontRef::new(&face.program).unwrap();
    let theirs = FontRef::new(HEBREW).unwrap();
    // Every table but `head`, whose checksum adjustment covers the new file.
    for record in theirs.table_directory.table_records() {
        if record.tag() == Tag::new(b"head") {
            continue;
        }
        assert_eq!(
            ours.table_data(record.tag())
                .map(|data| data.as_bytes().to_vec()),
            theirs
                .table_data(record.tag())
                .map(|data| data.as_bytes().to_vec()),
        );
    }
}

#[test]
fn a_collection_embeds_the_selected_face_alone() {
    let data = collection(&[HEBREW, ARABIC]);
    let hebrew = Face::read(&data, 0).unwrap();
    let arabic = Face::read(&data, 1).unwrap();

    assert_eq!(hebrew.family, "Noto Sans Hebrew");
    assert!(arabic.family.starts_with("Noto Sans Arabic"));
    assert_eq!(&arabic.program[..4], &[0, 1, 0, 0]);
    assert!(matches!(Face::read(&data, 2), Err(FaceError::Font(_))));
}

#[test]
fn invalid_data_is_a_font_error() {
    let mut no_cmap = HEBREW.to_vec();
    let count = usize::from(u16::from_be_bytes([no_cmap[4], no_cmap[5]]));
    for table in 0..count {
        let record = 12 + 16 * table;
        if &no_cmap[record..record + 4] == b"cmap" {
            no_cmap[record..record + 4].copy_from_slice(b"xmap");
        }
    }
    for data in [&b""[..], b"not a font", &HEBREW[..40], &no_cmap] {
        assert!(
            matches!(Face::read(data, 0), Err(FaceError::Font(_))),
            "{:?}",
            data.get(..4)
        );
    }
    assert!(matches!(Face::read(HEBREW, 1), Err(FaceError::Font(_))));
}

#[test]
fn measure_maps_characters_to_advances_and_ink() {
    let face = Face::read(HEBREW, 0).unwrap();
    let measure = Measure::new(&face);
    let shin = measure.glyph('ש').unwrap();

    assert!(measure.advance(shin) > 0.0);
    let [x0, y0, x1, y1] = measure.ink(shin).unwrap();
    assert!(x0 < x1 && y0 < y1);
    assert_eq!(measure.glyph('A'), None);
    assert!(measure.chars().any(|character| character == 'ש'));
    let space = measure.glyph(' ').unwrap();
    assert_eq!(measure.ink(space), None);
}
