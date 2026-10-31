use skrifa::instance::{LocationRef, Size};
use skrifa::outline::{DrawSettings, OutlinePen};
use skrifa::raw::TableProvider;
use skrifa::raw::types::Tag;
use skrifa::{FontRef, GlyphId, MetadataProvider};

use super::super::embed::Face;
use super::super::{cff, sfnt};
use super::compact;

const JAPANESE: &[u8] = include_bytes!("../testdata/NotoSansJP-subset.otf");
const HEBREW: &[u8] = include_bytes!("../testdata/NotoSansHebrew-subset.ttf");
const ARABIC: &[u8] = include_bytes!("../testdata/NotoSansArabic-Variable-subset.ttf");

/// Records the points that an outline draws.
#[derive(Default)]
struct Points(Vec<(f32, f32)>);

impl OutlinePen for Points {
    fn move_to(&mut self, x: f32, y: f32) {
        self.0.push((x, y));
    }

    fn line_to(&mut self, x: f32, y: f32) {
        self.0.push((x, y));
    }

    fn quad_to(&mut self, cx0: f32, cy0: f32, x: f32, y: f32) {
        self.0.extend([(cx0, cy0), (x, y)]);
    }

    fn curve_to(&mut self, cx0: f32, cy0: f32, cx1: f32, cy1: f32, x: f32, y: f32) {
        self.0.extend([(cx0, cy0), (cx1, cy1), (x, y)]);
    }

    fn close(&mut self) {}
}

/// The advance and the outline points of a glyph of `font`, in font units.
fn metrics(font: &FontRef<'_>, glyph: u32) -> (Option<f32>, Vec<(f32, f32)>) {
    let metrics = font.glyph_metrics(Size::unscaled(), LocationRef::default());
    let glyph = GlyphId::new(glyph);
    let mut points = Points::default();
    if let Some(outline) = font.outline_glyphs().get(glyph) {
        let settings = DrawSettings::unhinted(Size::unscaled(), LocationRef::default());
        outline.draw(settings, &mut points).unwrap();
    }
    (metrics.advance_width(glyph), points.0)
}

/// An OpenType wrapper of the bare CFF `cff` with `glyphs` glyphs, so that
/// skrifa draws it; the advances are zero, since the PDF's `W` gives them.
fn wrap(cff: &[u8], glyphs: usize, like: &FontRef<'_>) -> Vec<u8> {
    let table = |tag: &[u8; 4]| like.table_data(Tag::new(tag)).unwrap().as_bytes().to_vec();
    let count = u16::try_from(glyphs).unwrap().to_be_bytes();
    let mut hhea = table(b"hhea");
    hhea[34..36].copy_from_slice(&count);
    let mut maxp = table(b"maxp");
    maxp[4..6].copy_from_slice(&count);
    sfnt::write(
        b"OTTO",
        &[
            (*b"CFF ", cff.to_vec()),
            (*b"head", table(b"head")),
            (*b"hhea", hhea),
            (*b"hmtx", vec![0; 4 * glyphs]),
            (*b"maxp", maxp),
        ],
    )
    .unwrap()
}

#[test]
fn a_truetype_program_keeps_the_glyphs_it_is_given_renumbered() {
    for program in [HEBREW, ARABIC] {
        let face = Face::read(program, 0).unwrap();
        let before = FontRef::new(&face.program).unwrap();
        let count = u32::from(before.maxp().unwrap().num_glyphs());
        for glyph in 0..count {
            let one = compact(&face.program, &[glyph], "").unwrap();
            assert!(!one.cff);
            assert_eq!(one.glyphs[0], 0);
            assert!(one.glyphs.contains(&u16::try_from(glyph).unwrap()));
            let after = FontRef::new(&one.data).unwrap();
            assert_eq!(
                usize::from(after.maxp().unwrap().num_glyphs()),
                one.glyphs.len()
            );
            // Components come along, renumbered: every glyph draws as before.
            for (new, &old) in one.glyphs.iter().enumerate() {
                let new = u32::try_from(new).unwrap();
                assert_eq!(
                    metrics(&after, new),
                    metrics(&before, u32::from(old)),
                    "{glyph}"
                );
            }
            let tables: Vec<Tag> = after
                .table_directory
                .table_records()
                .iter()
                .map(|record| record.tag())
                .collect();
            assert!(!tables.contains(&Tag::new(b"cmap")));
            assert!(!tables.contains(&Tag::new(b"gvar")));
        }
    }
}

#[test]
fn a_cff_program_becomes_a_cid_keyed_cff_that_draws_the_same() {
    let face = Face::read(JAPANESE, 0).unwrap();
    let before = FontRef::new(&face.program).unwrap();
    let count = u32::from(before.maxp().unwrap().num_glyphs());
    let keep: Vec<u32> = (1..count).step_by(3).collect();
    let cid = compact(&face.program, &keep, "NotoSansJP-Regular").unwrap();

    assert!(cid.cff);
    assert_eq!(cid.glyphs.len(), keep.len() + 1);
    // The charset maps the new glyphs to the old IDs, so it is no identity.
    assert!(cff::identity_charset(&cid.data).unwrap().is_some());
    let wrapped = wrap(&cid.data, cid.glyphs.len(), &before);
    let after = FontRef::new(&wrapped).unwrap();
    for (new, &old) in cid.glyphs.iter().enumerate() {
        let new = u32::try_from(new).unwrap();
        assert_eq!(metrics(&after, new).1, metrics(&before, u32::from(old)).1);
    }
    assert!(cid.data.len() < face.program.len());
}

#[test]
fn glyphs_outside_the_program_are_ignored_and_notdef_stays() {
    let face = Face::read(HEBREW, 0).unwrap();
    let truetype = compact(&face.program, &[60_000, 1], "").unwrap();
    assert_eq!(truetype.glyphs, [0, 1]);
    let face = Face::read(JAPANESE, 0).unwrap();
    let empty = compact(&face.program, &[], "").unwrap();
    assert_eq!(empty.glyphs, [0]);
}

#[test]
fn bytes_that_are_no_program_do_not_compact() {
    for data in [&b""[..], b"not a font", &HEBREW[..40], &JAPANESE[..200]] {
        assert_eq!(compact(data, &[1, 2], ""), None);
    }
}
