use proptest::prelude::*;
use skrifa::instance::{LocationRef, Size};
use skrifa::outline::{DrawSettings, OutlinePen};
use skrifa::raw::TableProvider;
use skrifa::raw::model::glyph::outline::PathElement;
use skrifa::raw::ps::cff::CffFontRef;
use skrifa::raw::ps::cff::index::Index;
use skrifa::raw::ps::charstring::{self as cs, CommandSink};
use skrifa::raw::ps::{FontMatrix, HintingParams, Transform};
use skrifa::raw::types::{Fixed, Tag};
use skrifa::{
    DrawMemory, Font, FontRef, GlyphId, HintOptions, MetadataProvider, RasterTarget, ScalerFont,
};

use super::super::embed::Face;
use super::super::{cff, sfnt};
use super::{
    CNTRMASK, Charstring, ENDCHAR, HINTMASK, HSTEM, HSTEMHM, RMOVETO, VSTEM, compact, hint_scale,
};

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

/// An OpenType wrapper of the bare CFF `cff` with `glyphs` glyphs and zero
/// `hmtx` advances, so that `skrifa` draws its outlines without adding widths.
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

/// One command of a charstring, with its coordinates in 1/65536 units.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Command {
    HStem(i32, i32),
    VStem(i32, i32),
    HintMask(Vec<u8>),
    CounterMask(Vec<u8>),
    Move(i32, i32),
    Line(i32, i32),
    Curve([i32; 6]),
}

/// Records what a charstring evaluates to, hints included.
#[derive(Default)]
struct Commands(Vec<Command>);

impl CommandSink for Commands {
    fn move_to(&mut self, x: Fixed, y: Fixed) {
        self.0.push(Command::Move(x.to_bits(), y.to_bits()));
    }

    fn line_to(&mut self, x: Fixed, y: Fixed) {
        self.0.push(Command::Line(x.to_bits(), y.to_bits()));
    }

    fn curve_to(&mut self, cx0: Fixed, cy0: Fixed, cx1: Fixed, cy1: Fixed, x: Fixed, y: Fixed) {
        self.0.push(Command::Curve(
            [cx0, cy0, cx1, cy1, x, y].map(Fixed::to_bits),
        ));
    }

    fn close(&mut self) {}

    fn hstem(&mut self, y: Fixed, dy: Fixed) {
        self.0.push(Command::HStem(y.to_bits(), dy.to_bits()));
    }

    fn vstem(&mut self, x: Fixed, dx: Fixed) {
        self.0.push(Command::VStem(x.to_bits(), dx.to_bits()));
    }

    fn hint_mask(&mut self, mask: &[u8]) {
        self.0.push(Command::HintMask(mask.to_vec()));
    }

    fn counter_mask(&mut self, mask: &[u8]) {
        self.0.push(Command::CounterMask(mask.to_vec()));
    }
}

/// The `CFF ` table of the sfnt `program`.
fn cff_table(program: &[u8]) -> &[u8] {
    FontRef::new(program)
        .unwrap()
        .table_data(Tag::new(b"CFF "))
        .unwrap()
        .as_bytes()
}

/// The commands that `glyph` of the CFF `font` evaluates to.
fn commands(font: &CffFontRef<'_>, glyph: u32) -> Vec<Command> {
    let glyph = GlyphId::new(glyph);
    let subfont = font
        .subfont(font.subfont_index(glyph).unwrap(), &[])
        .unwrap();
    let mut sink = Commands::default();
    font.evaluate_charstring(&subfont, glyph, &[], &mut sink)
        .unwrap();
    sink.0
}

/// The hinting parameters that a reader takes from the Private DICT of `glyph`.
fn hint_params(font: &CffFontRef<'_>, glyph: u32) -> HintingParams {
    let index = font.subfont_index(GlyphId::new(glyph)).unwrap();
    font.subfont_hinted(index, &[]).unwrap().1
}

/// A scaler of `program` that hints at `ppem` as hayro hints a CFF program
/// (`hayro-font/src/program/mod.rs`, `hinted_outline`).
fn hinting(program: &[u8], ppem: f32) -> ScalerFont {
    let font = Font::new(program.to_vec(), 0).unwrap();
    let options = HintOptions::interpreter().with_target(RasterTarget::Light);
    let scaler = ScalerFont::hinted(font, ppem, options);
    assert!(scaler.is_hinted());
    scaler
}

fn hinted(scaler: &ScalerFont, glyph: u32) -> Vec<PathElement> {
    let mut path = Vec::new();
    scaler
        .outline(GlyphId::new(glyph))
        .unwrap()
        .draw(DrawMemory::Scratch, &mut path)
        .unwrap();
    path
}

/// The sizes to compare hinted outlines at, across the 39.6 ppem where the
/// default `BlueScale` stops suppressing overshoots.
const PPEMS: [f32; 8] = [8.0, 11.0, 12.0, 16.5, 24.0, 33.0, 64.0, 200.0];

/// The Japanese face's program, and every other glyph of it compacted.
fn japanese_compacted() -> (Vec<u8>, super::Compact) {
    let face = Face::read(JAPANESE, 0).unwrap();
    let count = u32::from(
        FontRef::new(&face.program)
            .unwrap()
            .maxp()
            .unwrap()
            .num_glyphs(),
    );
    let keep: Vec<u32> = (1..count).step_by(2).collect();
    let cid = compact(&face.program, &keep, "Subset").unwrap();
    (face.program.to_vec(), cid)
}

#[test]
fn a_compacted_cff_keeps_the_stems_and_masks_of_each_glyph() {
    let (program, cid) = japanese_compacted();
    let before = CffFontRef::new_cff(cff_table(&program), 0, None).unwrap();
    let after = CffFontRef::new_cff(&cid.data, 0, None).unwrap();

    let (mut stems, mut masks) = (0, 0);
    for (new, &old) in cid.glyphs.iter().enumerate() {
        let drawn = commands(&before, u32::from(old));
        stems += drawn
            .iter()
            .filter(|command| matches!(command, Command::HStem(..) | Command::VStem(..)))
            .count();
        masks += drawn
            .iter()
            .filter(|command| matches!(command, Command::HintMask(_) | Command::CounterMask(_)))
            .count();
        assert_eq!(
            commands(&after, u32::try_from(new).unwrap()),
            drawn,
            "{old}"
        );
    }
    assert!(stems > 0 && masks > 0, "{stems} stems and {masks} masks");
}

#[test]
fn a_compacted_cff_keeps_the_hint_values_of_each_glyph() {
    let (program, cid) = japanese_compacted();
    let table = cff_table(&program);
    let before = CffFontRef::new_cff(table, 0, None).unwrap();
    let after = CffFontRef::new_cff(&cid.data, 0, None).unwrap();
    let fd = |font: &CffFontRef<'_>, glyph| {
        usize::from(font.subfont_index(GlyphId::new(glyph)).unwrap())
    };

    for (new, &old) in cid.glyphs.iter().enumerate() {
        let (new, old) = (u32::try_from(new).unwrap(), u32::from(old));
        let params = hint_params(&before, old);
        assert!(!params.blues.values().is_empty());
        assert_eq!(hint_params(&after, new), params, "{old}");
        // Byte for byte, for the values that skrifa does not read as well.
        assert_eq!(
            cff::hint_values(&cid.data, fd(&after, new), 1.0).unwrap(),
            cff::hint_values(table, fd(&before, old), 1.0).unwrap(),
            "{old}"
        );
    }
}

#[test]
fn a_compacted_cff_hints_as_the_face_does() {
    let (program, cid) = japanese_compacted();
    for ppem in PPEMS {
        let (before, after) = (hinting(&program, ppem), hinting(&cid.data, ppem));
        for (new, &old) in cid.glyphs.iter().enumerate() {
            assert_eq!(
                hinted(&after, u32::try_from(new).unwrap()),
                hinted(&before, u32::from(old)),
                "glyph {old} at {ppem} ppem"
            );
        }
    }
}

/// The coordinates of a path, in order.
fn coordinates(path: &[PathElement]) -> Vec<f32> {
    path.iter()
        .flat_map(|element| match *element {
            PathElement::MoveTo { x, y } | PathElement::LineTo { x, y } => vec![x, y],
            PathElement::QuadTo { cx0, cy0, x, y } => vec![cx0, cy0, x, y],
            PathElement::CurveTo {
                cx0,
                cy0,
                cx1,
                cy1,
                x,
                y,
            } => vec![cx0, cy0, cx1, cy1, x, y],
            PathElement::Close => vec![],
        })
        .collect()
}

#[test]
fn a_face_of_another_scale_hints_as_it_did() {
    let mut program = Face::read(JAPANESE, 0).unwrap().program.to_vec();
    let head = FontRef::new(&program)
        .unwrap()
        .table_directory
        .table_records()
        .iter()
        .find(|record| record.tag() == Tag::new(b"head"))
        .unwrap()
        .offset() as usize;
    // At 2000 units per em, the 1000 that compaction writes halve every value.
    program[head + 18..head + 20].copy_from_slice(&2000u16.to_be_bytes());
    let count = u32::from(FontRef::new(&program).unwrap().maxp().unwrap().num_glyphs());
    let keep: Vec<u32> = (1..count).step_by(5).collect();
    let cid = compact(&program, &keep, "Subset").unwrap();

    let before = CffFontRef::new_cff(cff_table(&program), 0, Some(2000)).unwrap();
    let after = CffFontRef::new_cff(&cid.data, 0, None).unwrap();
    let half = |value: Fixed| value / Fixed::from_i32(2);
    for (new, &old) in cid.glyphs.iter().enumerate() {
        let (new, old) = (u32::try_from(new).unwrap(), u32::from(old));
        let (params, scaled) = (hint_params(&before, old), hint_params(&after, new));
        let blues: Vec<(Fixed, Fixed)> = params
            .blues
            .values()
            .iter()
            .map(|&(low, high)| (half(low), half(high)))
            .collect();
        assert_eq!(scaled.blues.values(), blues.as_slice(), "{old}");
        assert_eq!(scaled.blue_shift, half(params.blue_shift), "{old}");
        assert_eq!(scaled.language_group, params.language_group, "{old}");
        // The size where overshoots stop being suppressed stays where it was.
        let ratio = scaled.blue_scale.to_f64() / params.blue_scale.to_f64();
        assert!((ratio - 2.0).abs() < 0.01, "{old}: {ratio}");
    }
    // A `LanguageGroup` of 1 hints by an em box at fixed coordinates of the
    // character space, which assume 1000 units per em whatever the font's.
    let latin: Vec<(u32, u32)> = cid
        .glyphs
        .iter()
        .enumerate()
        .map(|(new, &old)| (u32::try_from(new).unwrap(), u32::from(old)))
        .filter(|&(_, old)| hint_params(&before, old).language_group == 0)
        .collect();
    assert!(!latin.is_empty());
    // Hinting rounds an edge to the pixel, where a scale off by 1/65536 can
    // move it, so these are sizes whose scales are exact at both units per em.
    for ppem in [7.8125, 15.625, 31.25, 62.5, 125.0, 250.0] {
        let (face, compacted) = (hinting(&program, ppem), hinting(&cid.data, ppem));
        for &(new, old) in &latin {
            let drawn = coordinates(&hinted(&compacted, new));
            let wanted = coordinates(&hinted(&face, old));
            assert_eq!(drawn.len(), wanted.len(), "glyph {old} at {ppem} ppem");
            for (drawn, wanted) in drawn.iter().zip(&wanted) {
                // A hinted coordinate is truncated to 1/64 of a pixel, after
                // the halved values rounded at 1/65536 of a unit.
                assert!(
                    (drawn - wanted).abs() <= 2.0 / 64.0,
                    "glyph {old} at {ppem} ppem: {drawn} and {wanted}"
                );
            }
        }
    }
}

/// The commands that the charstring `data`, without subroutines, evaluates to.
fn evaluated(data: &[u8]) -> Vec<Command> {
    let empty = Index::Empty;
    let context = (&[][..], &empty, &empty, &empty);
    let mut sink = Commands::default();
    cs::evaluate(&context, None, data, &mut sink).unwrap();
    sink.0
}

fn units(value: i32) -> Fixed {
    Fixed::from_i32(value)
}

fn hstem(start: i32, end: i32) -> Command {
    Command::HStem(units(start).to_bits(), units(end).to_bits())
}

#[test]
fn a_ghost_hint_keeps_its_marker_at_any_scale() {
    let mut pen = Charstring::new(0.5, true, usize::MAX);
    // The ghost of a top edge has a width of -20, that of a bottom edge -21.
    pen.hstem(units(100), units(80));
    pen.hstem(units(221), units(200));
    pen.hstem(units(300), units(340));
    assert_eq!(
        evaluated(&pen.finish_glyph().unwrap()),
        [hstem(50, 30), hstem(121, 100), hstem(150, 170)]
    );
}

#[test]
fn stems_past_the_operand_limit_take_another_operator() {
    let mut pen = Charstring::new(1.0, true, usize::MAX);
    let mut stems = Vec::new();
    for stem in 0..30 {
        pen.hstem(units(stem * 10), units(stem * 10 + 5));
        stems.push(hstem(stem * 10, stem * 10 + 5));
    }
    let data = pen.finish_glyph().unwrap();
    assert_eq!(evaluated(&data), stems);
    // No byte of these numbers reads as the operator.
    assert_eq!(data.iter().filter(|&&byte| byte == HSTEM).count(), 2);
}

#[test]
fn stems_take_the_operators_of_masks_only_when_a_mask_follows() {
    let mut pen = Charstring::new(1.0, true, usize::MAX);
    pen.hstem(units(0), units(10));
    pen.vstem(units(5), units(15));
    pen.move_to(units(1), units(2));
    assert_eq!(
        pen.finish_glyph().unwrap(),
        [139, 149, HSTEM, 144, 149, VSTEM, 140, 141, RMOVETO, ENDCHAR]
    );

    let mut pen = Charstring::new(1.0, true, usize::MAX);
    pen.hstem(units(0), units(10));
    pen.vstem(units(5), units(15));
    pen.hint_mask(&[0b1100_0000]);
    pen.move_to(units(1), units(2));
    pen.vstem(units(6), units(7));
    pen.counter_mask(&[0b1010_0000]);
    // Vertical stems right before a mask need no operator of their own.
    assert_eq!(
        pen.finish_glyph().unwrap(),
        [
            139,
            149,
            HSTEMHM,
            144,
            149,
            HINTMASK,
            0b1100_0000,
            140,
            141,
            RMOVETO,
            145,
            140,
            CNTRMASK,
            0b1010_0000,
            ENDCHAR
        ]
    );
}

#[test]
fn a_font_matrix_that_does_more_than_scale_gives_no_hint_scale() {
    let matrix = |xy: Fixed, yy: Fixed| {
        FontMatrix::from_elements([Fixed::ONE, Fixed::ZERO, xy, yy, Fixed::ZERO, Fixed::ZERO])
    };
    let scale = |matrix, scale| hint_scale(&Transform { matrix, scale }, 0.5);
    assert_eq!(scale(matrix(Fixed::ZERO, Fixed::ONE), None), Some(0.5));
    // A scale is in 26.6 per font unit, so 128 doubles.
    assert_eq!(
        scale(matrix(Fixed::ZERO, Fixed::ONE), Some(units(128))),
        Some(1.0)
    );
    assert_eq!(scale(matrix(units(1), Fixed::ONE), None), None);
    assert_eq!(scale(matrix(Fixed::ZERO, units(2)), None), None);
}

/// A Private DICT with only these `BlueValues`.
fn blues(values: &[i32]) -> Vec<u8> {
    let mut out = Vec::new();
    for value in values {
        out.push(29);
        out.extend_from_slice(&value.to_be_bytes());
    }
    out.push(6);
    out
}

/// A charstring with a stem at `y` and a line from there.
fn stem_and_line(y: i32) -> Vec<u8> {
    let mut pen = Charstring::new(1.0, true, usize::MAX);
    pen.hstem(units(y), units(y + 10));
    pen.move_to(units(0), units(y));
    pen.line_to(units(10), units(y));
    pen.finish_glyph().unwrap()
}

#[test]
fn each_glyph_keeps_the_private_dict_of_its_font_dict() {
    let charstrings: Vec<Vec<u8>> = (0..6).map(|glyph| stem_and_line(glyph * 100)).collect();
    // The third font DICT holds the same values as the first.
    let source = cff::cid_keyed(
        "Source",
        &charstrings,
        &[0, 1, 2, 3, 4, 5],
        &[0, 1, 2, 0, 1, 2],
        &[blues(&[0, 10]), blues(&[100, 20]), blues(&[0, 10])],
    )
    .unwrap();
    let program = wrap(&source, 6, &FontRef::new(JAPANESE).unwrap());

    let cid = compact(&program, &[1, 2, 3, 4, 5], "Subset").unwrap();
    let after = CffFontRef::new_cff(&cid.data, 0, None).unwrap();
    assert_eq!(after.num_subfonts(), 2);
    let before = CffFontRef::new_cff(&source, 0, None).unwrap();
    assert_eq!(before.num_subfonts(), 3);
    for glyph in 0..6 {
        assert_eq!(hint_params(&after, glyph), hint_params(&before, glyph));
        assert_eq!(commands(&after, glyph), commands(&before, glyph));
    }
}

#[test]
fn a_seac_glyph_keeps_its_outline_and_no_hints() {
    // `adx ady bchar achar endchar`, of "A" and "acute" in StandardEncoding.
    let mut accented = Charstring::new(1.0, true, usize::MAX);
    for value in [5, 20, 65, 194] {
        accented.number(i64::from(value) << 16);
    }
    // skrifa finds the components of a CID-keyed font's `seac` through the
    // ISOAdobe charset, whose glyph IDs are the SIDs of those two.
    let mut charstrings = vec![vec![ENDCHAR]; 127];
    charstrings[34] = stem_and_line(0);
    charstrings[125] = stem_and_line(100);
    charstrings[126] = accented.finish_glyph().unwrap();
    let cids: Vec<u16> = (0..127).collect();
    let source =
        cff::cid_keyed("Source", &charstrings, &cids, &[0; 127], &[blues(&[0, 10])]).unwrap();
    let program = wrap(&source, 127, &FontRef::new(JAPANESE).unwrap());

    let cid = compact(&program, &[126], "Subset").unwrap();
    assert_eq!(cid.glyphs, [0, 126]);
    let before = CffFontRef::new_cff(&source, 0, None).unwrap();
    let after = CffFontRef::new_cff(&cid.data, 0, None).unwrap();
    let outline: Vec<Command> = commands(&before, 126)
        .into_iter()
        .filter(|command| !matches!(command, Command::HStem(..)))
        .collect();
    assert_eq!(outline.len(), 4, "a move and a line of each component");
    assert_eq!(commands(&after, 1), outline);
}

#[test]
fn a_charstring_overflows_at_its_limit() {
    // A move and a line take three bytes each, and the end one more.
    let mut pen = Charstring::new(1.0, false, 7);
    pen.move_to(units(1), units(2));
    pen.line_to(units(3), units(4));
    assert_eq!(pen.finish_glyph().map(|data| data.len()), Some(7));

    let mut pen = Charstring::new(1.0, false, 8);
    pen.move_to(units(1), units(2));
    pen.line_to(units(3), units(4));
    assert!(!pen.overflow);
    pen.line_to(units(5), units(6));
    assert!(pen.overflow);
    assert!(pen.data.len() <= 8);
    assert_eq!(pen.finish_glyph(), None);
}

#[test]
fn an_overflowing_charstring_stops_recording_stem_operators() {
    // Two stem operands fit, but their operator does not. Later callbacks
    // must not grow the bookkeeping after the byte limit has refused it.
    let mut pen = Charstring::new(1.0, true, 2);
    for _ in 0..100 {
        pen.hstem(units(0), units(1));
        pen.move_to(units(0), units(0));
    }
    assert!(pen.overflow);
    assert!(pen.stem_operators.is_empty());
    assert_eq!(pen.finish_glyph(), None);
}

#[test]
fn charstrings_that_outgrow_the_program_leave_it_whole() {
    // Each `seac` glyph draws the long outline of "A" again, so a few bytes
    // of the program become thousands in the result.
    let mut base = Charstring::new(1.0, false, usize::MAX);
    base.move_to(units(0), units(0));
    for at in 1..2000 {
        base.line_to(units(at % 50), units(at / 50));
    }
    let mut accented = Charstring::new(1.0, false, usize::MAX);
    for value in [5, 20, 65, 194] {
        accented.number(i64::from(value) << 16);
    }
    let accented = accented.finish_glyph().unwrap();
    let mut charstrings = vec![vec![ENDCHAR]; 127];
    charstrings[34] = base.finish_glyph().unwrap();
    charstrings[125] = stem_and_line(100);
    charstrings.extend(std::iter::repeat_n(accented, 100));
    let cids: Vec<u16> = (0..227).collect();
    let source =
        cff::cid_keyed("Source", &charstrings, &cids, &[0; 227], &[blues(&[0, 10])]).unwrap();
    let program = wrap(&source, 227, &FontRef::new(JAPANESE).unwrap());
    let seacs: Vec<u32> = (127..227).collect();
    assert!(seacs.len() * charstrings[34].len() > program.len());

    assert_eq!(compact(&program, &seacs, "Subset"), None);
    let one = compact(&program, &[127], "Subset").unwrap();
    assert!(one.data.len() < program.len());
}

/// Parses the leading width operand of a subroutine-free Type 2 charstring
/// (Adobe TN 5177, section 4.2), returning `None` when the charstring leaves
/// `defaultWidthX` in effect.
fn charstring_width(data: &[u8]) -> Option<f64> {
    let mut stack = Vec::new();
    let mut at = 0;
    while let Some(&b0) = data.get(at) {
        match b0 {
            // hstem, vstem, hstemhm, hintmask, cntrmask, vstemhm: an odd
            // operand count means the first stack entry is the width.
            1 | 3 | 18 | 19 | 20 | 23 => {
                assert!(
                    stack.len() <= 48,
                    "Type 2 operand stack exceeded 48 items: {}",
                    stack.len()
                );
                return (stack.len() % 2 == 1).then(|| stack[0]);
            }
            // vmoveto, hmoveto: 2 operands when width is present.
            4 | 22 => return (stack.len() > 1).then(|| stack[0]),
            // rmoveto: 3 operands when width is present.
            21 => return (stack.len() > 2).then(|| stack[0]),
            // endchar: 1 operand when width is present (4 was seac).
            14 => return (!stack.is_empty() && stack.len() != 4).then(|| stack[0]),
            28 => {
                let value = i16::from_be_bytes([data[at + 1], data[at + 2]]);
                stack.push(f64::from(value));
                at += 3;
            }
            32..=246 => {
                stack.push(f64::from(i32::from(b0) - 139));
                at += 1;
            }
            247..=250 => {
                let value = (i32::from(b0) - 247) * 256 + i32::from(data[at + 1]) + 108;
                stack.push(f64::from(value));
                at += 2;
            }
            251..=254 => {
                let value = -(i32::from(b0) - 251) * 256 - i32::from(data[at + 1]) - 108;
                stack.push(f64::from(value));
                at += 2;
            }
            255 => {
                let fixed =
                    i32::from_be_bytes([data[at + 1], data[at + 2], data[at + 3], data[at + 4]]);
                stack.push(f64::from(fixed) / 65536.0);
                at += 5;
            }
            _ => return None,
        }
    }
    None
}

/// Returns the Top DICT bytes and the CharStrings INDEX of a bare CFF.
fn cff_top_and_charstrings(data: &[u8]) -> (&[u8], Index<'_>) {
    let header_len = usize::from(data[2]);
    let names = Index::new(&data[header_len..], false).unwrap();
    let tops_at = header_len + names.size_in_bytes().unwrap();
    let tops = Index::new(&data[tops_at..], false).unwrap();
    let top = tops.get(0).unwrap();
    // Our `cid_keyed` Top DICT ends with `CHARSTRINGS` (op 17) then `FD_ARRAY` (12 36).
    let cs_at = top
        .windows(6)
        .find_map(|window| {
            (window[0] == 29 && window[5] == 17).then(|| {
                usize::try_from(i32::from_be_bytes([
                    window[1], window[2], window[3], window[4],
                ]))
                .unwrap()
            })
        })
        .unwrap();
    (top, Index::new(&data[cs_at..], false).unwrap())
}

#[test]
fn a_compacted_cff_encodes_canonical_ros_and_exact_glyph_widths() {
    let face = Face::read(JAPANESE, 0).unwrap();
    let before = FontRef::new(&face.program).unwrap();
    let count = u32::from(before.maxp().unwrap().num_glyphs());
    let keep: Vec<u32> = (0..count).collect();
    let cid = compact(&face.program, &keep, "NotoSansJP-Regular").unwrap();
    let (top, charstrings) = cff_top_and_charstrings(&cid.data);

    // Top DICT starts with compact SIDs 391, 392 and supplement 0 before ROS (12 30).
    assert_eq!(&top[..7], &[248, 27, 248, 28, 139, 12, 30]);
    for (new, &old) in cid.glyphs.iter().enumerate() {
        let cs_bytes = charstrings.get(new).unwrap();
        let program_width = charstring_width(cs_bytes).unwrap_or(0.0);
        let hmtx_width = f64::from(metrics(&before, u32::from(old)).0.unwrap_or(0.0));
        assert!(
            (program_width - hmtx_width).abs() <= 1.0 / 65536.0,
            "glyph {old}: CFF width {program_width} != hmtx {hmtx_width}"
        );
    }

    // At 2048 units per em, non-integer scaled widths round-trip in 16.16 fixed-point.
    let mut scaled_program = face.program.to_vec();
    let head = before
        .table_directory
        .table_records()
        .iter()
        .find(|record| record.tag() == Tag::new(b"head"))
        .unwrap()
        .offset() as usize;
    scaled_program[head + 18..head + 20].copy_from_slice(&2048_u16.to_be_bytes());
    let scaled_cid = compact(&scaled_program, &[1, 6, 8, 9, 11, 15, 49], "Scaled").unwrap();
    let (_, scaled_cs) = cff_top_and_charstrings(&scaled_cid.data);
    for (new, &old) in scaled_cid.glyphs.iter().enumerate() {
        let program_width = charstring_width(scaled_cs.get(new).unwrap()).unwrap_or(0.0);
        let expected =
            f64::from(metrics(&before, u32::from(old)).0.unwrap_or(0.0)) * 1000.0 / 2048.0;
        assert!(
            (program_width - expected).abs() <= 1.0 / 65536.0,
            "scaled glyph {old}: CFF width {program_width} != expected {expected}"
        );
    }
}

#[test]
fn charstring_widths_precede_every_first_operator_within_the_stack_limit() {
    // Empty outline (space): `w endchar`.
    let empty = Charstring::new(1.0, false, usize::MAX)
        .with_width(224 << 16)
        .finish_glyph()
        .unwrap();
    assert_eq!(charstring_width(&empty), Some(224.0));
    assert_eq!(evaluated(&empty), []);

    // Unhinted outline: `w dx dy rmoveto ...`.
    let mut unhinted = Charstring::new(1.0, false, usize::MAX).with_width(575 << 16);
    unhinted.move_to(units(10), units(20));
    unhinted.line_to(units(30), units(40));
    let unhinted = unhinted.finish_glyph().unwrap();
    assert_eq!(charstring_width(&unhinted), Some(575.0));

    // Implicit vstem right before hintmask: `w x dx hintmask ...`.
    let mut vmasked = Charstring::new(1.0, true, usize::MAX).with_width(608 << 16);
    vmasked.vstem(units(10), units(30));
    vmasked.hint_mask(&[0b1000_0000]);
    vmasked.move_to(units(10), units(20));
    let vmasked = vmasked.finish_glyph().unwrap();
    assert_eq!(charstring_width(&vmasked), Some(608.0));

    // Hintmask with no preceding stems: `w hintmask ...`.
    let mut bare_mask = Charstring::new(1.0, true, usize::MAX).with_width(510 << 16);
    bare_mask.hint_mask(&[0]);
    bare_mask.move_to(units(1), units(2));
    let bare_mask = bare_mask.finish_glyph().unwrap();
    assert_eq!(charstring_width(&bare_mask), Some(510.0));

    // 30 stems + width: the first operator carries 1 + 2*23 = 47 <= 48 operands,
    // and the second carries the remaining 7 stems (14 operands, even).
    let mut many = Charstring::new(1.0, true, usize::MAX).with_width(918 << 16);
    let mut expected_stems = Vec::new();
    for stem in 0..30 {
        many.hstem(units(stem * 10), units(stem * 10 + 5));
        expected_stems.push(hstem(stem * 10, stem * 10 + 5));
    }
    let many = many.finish_glyph().unwrap();
    assert_eq!(charstring_width(&many), Some(918.0));
    assert_eq!(evaluated(&many), expected_stems);
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(64))]
    #[test]
    fn a_damaged_cff_face_compacts_or_refuses(flips in prop::collection::vec((any::<prop::sample::Index>(), any::<u8>()), 1..16)) {
        let table = cff_table(JAPANESE);
        let start = table.as_ptr() as usize - JAPANESE.as_ptr() as usize;
        let mut program = JAPANESE.to_vec();
        for (at, byte) in flips {
            program[start + at.index(table.len())] = byte;
        }
        let _ = compact(&program, &[1, 2, 3, 10, 100], "Damaged");
    }
}
