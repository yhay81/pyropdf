use pyropdf_store::budget::{Budget, DocumentLimits, LimitCode};
use skrifa::FontRef;
use skrifa::instance::{LocationRef, Size};
use skrifa::metrics::Metrics;
use skrifa::outline::{DrawSettings, OutlinePen};
use skrifa::raw::tables::glyf::SimpleGlyph;
use skrifa::raw::types::Tag;
use skrifa::raw::{FontData, FontRead, TableProvider};
use skrifa::{GlyphId, MetadataProvider};

use super::super::embed::{Face, Measure};
use super::super::shape::ShapingFace;
use super::*;

const ARABIC: &[u8] = include_bytes!("../testdata/NotoSansArabic-Variable-subset.ttf");
const ORACLE: &str = include_str!("../testdata/NotoSansArabic-Variable-subset-instances.txt");
const LATIN: &[u8] = include_bytes!("../testdata/variable-latin.ttf");
const COMPOSITES: &[u8] = include_bytes!("../testdata/variable-composites.ttf");
const OVERFLOW: &[u8] = include_bytes!("../testdata/variable-overflow.ttf");
const HEBREW: &[u8] = include_bytes!("../testdata/NotoSansHebrew-subset.ttf");
const JAPANESE: &[u8] = include_bytes!("../testdata/NotoSansJP-subset.otf");

fn budget(limit: Option<u64>) -> Budget {
    Budget::new(&DocumentLimits::web()).with_limit(LimitCode::FontInputSize, limit)
}

fn instance(data: &[u8], weight: u16) -> Face {
    Face::read(data, 0)
        .unwrap()
        .at_weight(weight, &budget(None))
        .unwrap()
}

/// The signed area of a glyph's outline, which fontTools' `AreaPen` sums.
#[derive(Default)]
struct AreaPen {
    start: (f64, f64),
    last: (f64, f64),
    doubled: f64,
}

impl AreaPen {
    fn cross(a: (f64, f64), b: (f64, f64)) -> f64 {
        a.0 * b.1 - a.1 * b.0
    }
}

impl OutlinePen for AreaPen {
    fn move_to(&mut self, x: f32, y: f32) {
        self.start = (f64::from(x), f64::from(y));
        self.last = self.start;
    }
    fn line_to(&mut self, x: f32, y: f32) {
        let to = (f64::from(x), f64::from(y));
        self.doubled += Self::cross(self.last, to);
        self.last = to;
    }
    fn quad_to(&mut self, cx0: f32, cy0: f32, x: f32, y: f32) {
        let control = (f64::from(cx0), f64::from(cy0));
        let to = (f64::from(x), f64::from(y));
        self.doubled += (2.0 * Self::cross(self.last, control)
            + 2.0 * Self::cross(control, to)
            + Self::cross(self.last, to))
            / 3.0;
        self.last = to;
    }
    fn curve_to(&mut self, _: f32, _: f32, _: f32, _: f32, _: f32, _: f32) {
        panic!("a TrueType glyph has no cubic curves");
    }
    fn close(&mut self) {
        self.doubled += Self::cross(self.last, self.start);
        self.last = self.start;
    }
}

fn area(face: &Face, glyph: GlyphId) -> f64 {
    let font = face.font();
    let outlines = font.outline_glyphs();
    let mut pen = AreaPen::default();
    let Some(outline) = outlines.get(glyph) else {
        return 0.0;
    };
    outline
        .draw(
            DrawSettings::unhinted(Size::unscaled(), LocationRef::default()),
            &mut pen,
        )
        .unwrap();
    (pen.doubled / 2.0).abs()
}

#[test]
fn every_glyph_matches_fonttools_instancer() {
    let mut weights: Vec<(u16, Face)> = Vec::new();
    let mut checked = 0;
    for line in ORACLE.lines().filter(|line| !line.starts_with('#')) {
        let columns: Vec<f64> = line.split(' ').map(|c| c.parse().unwrap()).collect();
        let [
            weight,
            glyph,
            advance,
            x_min,
            y_min,
            x_max,
            y_max,
            oracle_area,
        ] = columns[..]
        else {
            panic!("an oracle line has eight columns: {line}");
        };
        let weight = weight as u16;
        if weights.last().is_none_or(|(last, _)| *last != weight) {
            weights.push((weight, instance(ARABIC, weight)));
        }
        let face = &weights.last().unwrap().1;
        let glyph = GlyphId::new(glyph as u32);
        let measure = Measure::new(face);
        assert!(
            (measure.advance(glyph) - advance).abs() <= 1.0,
            "the advance of {glyph} at {weight}: {} and {advance}",
            measure.advance(glyph),
        );
        let ink = measure.ink(glyph).unwrap_or([0.0; 4]);
        for (ours, theirs) in ink.into_iter().zip([x_min, y_min, x_max, y_max]) {
            assert!(
                (ours - theirs).abs() <= 1.0,
                "the box of {glyph} at {weight}: {ink:?} and {x_min} {y_min} {x_max} {y_max}"
            );
        }
        let ours = area(face, glyph);
        assert!(
            (ours - oracle_area).abs() <= (oracle_area * 0.01).max(1.0),
            "the area of {glyph} at {weight}: {ours} and {oracle_area}"
        );
        checked += 1;
    }
    assert_eq!(checked, 3 * 56);
}

#[test]
fn an_instance_keeps_every_glyph_id_and_character() {
    let source = Face::read(ARABIC, 0).unwrap();
    let face = instance(ARABIC, 700);
    assert_eq!(face.glyph_count, source.glyph_count);
    let (ours, theirs) = (Measure::new(&face), Measure::new(&source));
    assert_eq!(
        ours.chars().collect::<String>(),
        theirs.chars().collect::<String>()
    );
    for character in theirs.chars() {
        assert_eq!(ours.glyph(character), theirs.glyph(character));
    }
}

#[test]
fn a_static_program_has_no_variations_or_instructions() {
    let face = instance(ARABIC, 700);
    let font = face.font();
    for tag in [
        b"fvar", b"gvar", b"HVAR", b"MVAR", b"avar", b"STAT", b"GPOS", b"GSUB", b"GDEF", b"prep",
    ] {
        assert!(font.table_data(Tag::new(tag)).is_none(), "{tag:?}");
    }
    assert_eq!(face.weight_axis, None);
    assert!(!face.cff);
    let outlines = font.outline_glyphs();
    for id in 0..face.glyph_count {
        let Some(outline) = outlines.get(GlyphId::new(id)) else {
            continue;
        };
        assert_eq!(outline.has_hinting(), Some(false), "glyph {id}");
    }
}

#[test]
fn metrics_describe_the_instance() {
    let source = FontRef::new(ARABIC).unwrap();
    let location = source.axes().location([(WGHT, 700.0)]);
    let expected = Metrics::new(&source, Size::unscaled(), &location);
    let face = instance(ARABIC, 700);
    assert!((face.ascender - f64::from(expected.ascent)).abs() <= 0.5);
    assert!((face.descender - f64::from(expected.descent)).abs() <= 0.5);
    assert!((face.cap_height - f64::from(expected.cap_height.unwrap())).abs() <= 0.5);
    assert_eq!(face.weight, 700);
    assert!(face.bold);

    let latin = FontRef::new(LATIN).unwrap();
    let at_top = latin.axes().location([(WGHT, 900.0)]);
    let expected = Metrics::new(&latin, Size::unscaled(), &at_top);
    let default_latin = Metrics::new(&latin, Size::unscaled(), LocationRef::default());
    assert!(expected.cap_height.unwrap() > default_latin.cap_height.unwrap());
    let face = instance(LATIN, 900);
    assert_eq!(face.cap_height, f64::from(expected.cap_height.unwrap()));
    assert_eq!(face.ascender, f64::from(expected.ascent));
    assert_eq!(face.ascender, f64::from(default_latin.ascent) + 50.0);
    // The box comes from the instance's outlines, not the source's `head`.
    assert_eq!(face.bbox, [100.0, 0.0, 1000.0, 700.0]);
}

#[test]
fn a_weight_below_bold_does_not_read_as_bold() {
    let mut data = ARABIC.to_vec();
    let font = FontRef::new(ARABIC).unwrap();
    let start = font
        .table_data(Tag::new(b"OS/2"))
        .unwrap()
        .as_bytes()
        .as_ptr() as usize
        - ARABIC.as_ptr() as usize;
    data[start + OS2_SELECTION..start + OS2_SELECTION + 2]
        .copy_from_slice(&SELECTION_BOLD.to_be_bytes());
    assert!(Face::read(&data, 0).unwrap().bold);
    let face = instance(&data, 500);
    assert!(!face.bold);
    assert_eq!(face.weight, 500);
    let os2 = face.font().os2().unwrap();
    assert_eq!(os2.fs_selection().bits(), SELECTION_REGULAR);
    assert_eq!(os2.us_weight_class(), 500);
}

#[test]
fn names_describe_the_instance_and_differ_by_weight() {
    let source = Face::read(LATIN, 0).unwrap();
    assert_eq!(source.postscript_name, "VariableLatin-Regular");
    let named = instance(LATIN, 700);
    assert_eq!(named.family, "Variable Latin");
    assert_eq!(named.full_name, "Variable Latin Bold");
    assert_eq!(named.postscript_name, "VariableLatin_700wght");
    let unnamed = instance(LATIN, 650);
    assert_eq!(unnamed.full_name, "Variable Latin 650wght");
    assert_eq!(unnamed.postscript_name, "VariableLatin_650wght");
    let again = Face::read(&named.program, 0).unwrap();
    assert_eq!(again.full_name, named.full_name);
    assert_eq!(again.postscript_name, named.postscript_name);
}

#[test]
fn a_long_prefix_gives_way_to_the_weight() {
    let mut source = Face::read(LATIN, 0).unwrap();
    source.postscript_name = "P".repeat(63);
    let font = FontRef::new(LATIN).unwrap();
    let name = postscript_name(&source, &font, 700);
    assert_eq!(name.len(), 63);
    assert!(name.ends_with("_700wght"), "{name}");
    assert_ne!(name, postscript_name(&source, &font, 650));
}

#[test]
fn the_budget_holds_the_whole_program() {
    let source = Face::read(ARABIC, 0).unwrap();
    let program = instance(ARABIC, 700).program;
    let size = program.len() as u64;
    let exact = source.at_weight(700, &budget(Some(size))).unwrap();
    assert_eq!(exact.program, program);
    let refused = source.at_weight(700, &budget(Some(size - 1))).unwrap_err();
    let InstanceError::Limit(refusal) = refused else {
        panic!("not a limit: {refused:?}");
    };
    assert_eq!(refusal.code(), LimitCode::FontInputSize);
}

#[test]
fn composites_that_flatten_past_the_limit_refuse() {
    let source = Face::read(COMPOSITES, 0).unwrap();
    let whole = source.at_weight(700, &budget(None)).unwrap();
    assert!(whole.program.len() > 50 * COMPOSITES.len());
    let refused = source
        .at_weight(700, &budget(Some(COMPOSITES.len() as u64)))
        .unwrap_err();
    assert!(matches!(refused, InstanceError::Limit(_)), "{refused:?}");
}

#[test]
fn a_coordinate_outside_i16_refuses() {
    let source = Face::read(OVERFLOW, 0).unwrap();
    let inside = source.at_weight(500, &budget(None)).unwrap();
    assert_eq!(inside.bbox[2], 32400.0);
    let refused = source.at_weight(900, &budget(None)).unwrap_err();
    let InstanceError::Refused(reason) = refused else {
        panic!("not a refusal: {refused:?}");
    };
    assert!(reason.contains("glyph 1"), "{reason}");
    assert!(reason.contains("16 bits"), "{reason}");
}

#[test]
fn a_weight_needs_a_truetype_axis_that_holds_it() {
    let face = Face::read(ARABIC, 0).unwrap();
    for weight in [99, 901] {
        let refused = face.at_weight(weight, &budget(None)).unwrap_err();
        let InstanceError::Refused(reason) = refused else {
            panic!("not a refusal: {refused:?}");
        };
        assert!(reason.contains("outside"), "{reason}");
    }
    for data in [HEBREW, JAPANESE] {
        let face = Face::read(data, 0).unwrap();
        assert!(matches!(
            face.at_weight(700, &budget(None)),
            Err(InstanceError::Refused(_))
        ));
    }
    let mut cff = Face::read(JAPANESE, 0).unwrap();
    cff.weight_axis = Some([100.0, 400.0, 900.0]);
    assert!(matches!(
        cff.at_weight(700, &budget(None)),
        Err(InstanceError::Refused(_))
    ));
}

#[test]
fn the_source_shapes_at_the_instance() {
    let shaped = |face: &Face| -> Vec<i32> {
        ShapingFace::of(face)
            .unwrap()
            .shape("AV", &budget(None))
            .unwrap()
            .iter()
            .map(|glyph| glyph.x_advance)
            .collect()
    };
    let source = Face::read(LATIN, 0).unwrap();
    assert_eq!(shaped(&source), [900, 1000]);
    assert_eq!(shaped(&instance(LATIN, 900)), [800, 1000]);
    assert_eq!(shaped(&instance(LATIN, 100)), [950, 1000]);
    // Halfway up the axis: 50 of A's 100 units, and -200 of kerning.
    assert_eq!(shaped(&instance(LATIN, 650)), [850, 1000]);
    // A static program alone has no kerning to give.
    let face = instance(LATIN, 900);
    let alone = Face::read(&face.program, 0).unwrap();
    assert_eq!(shaped(&alone), [1100, 1000]);
}

fn drawn(points: &[(f32, f32, bool)]) -> Pen {
    let mut pen = Pen::default();
    let mut at = 0;
    while at < points.len() {
        let (x, y, on) = points[at];
        assert!(on, "a contour starts on the curve");
        pen.move_to(x, y);
        at += 1;
        while at < points.len() {
            let (x, y, on) = points[at];
            at += 1;
            if on {
                pen.line_to(x, y);
            } else {
                let (to_x, to_y, _) = points[at];
                at += 1;
                pen.quad_to(x, y, to_x, to_y);
            }
        }
        pen.close();
    }
    pen
}

fn read_back(data: &[u8]) -> Vec<(i32, i32, bool)> {
    let glyph = SimpleGlyph::read(FontData::new(data)).unwrap();
    glyph
        .points()
        .map(|point| (point.x.into(), point.y.into(), point.on_curve))
        .collect()
}

#[test]
fn a_glyph_reads_back_as_written() {
    // A flag repeats for more than one count byte can say, with deltas of
    // none, one, and two bytes.
    let mut points: Vec<(f32, f32, bool)> = (0..600).map(|x| (x as f32, 0.0, true)).collect();
    points.push((600.0, 300.0, true));
    points.push((-30000.0, 300.0, true));
    let pen = drawn(&points);
    let mut out = Vec::new();
    let encoded = encode(&pen, false, &mut out).unwrap().unwrap();
    assert_eq!(encoded.points, 602);
    assert_eq!(encoded.contours, 1);
    assert_eq!(encoded.bounds, [-30000, 0, 600, 300]);
    assert_eq!(out.len() % 4, 0);
    let expected: Vec<_> = points
        .iter()
        .map(|&(x, y, on)| (x as i32, y as i32, on))
        .collect();
    assert_eq!(read_back(&out), expected);
}

#[test]
fn an_implied_point_and_a_closing_point_are_not_written() {
    let pen = drawn(&[
        (0.0, 0.0, true),
        (0.0, 100.0, false),
        (50.0, 100.0, true),
        (100.0, 100.0, false),
        (100.0, 0.0, true),
        (0.0, 0.0, true),
    ]);
    let mut out = Vec::new();
    let encoded = encode(&pen, false, &mut out).unwrap().unwrap();
    assert_eq!(encoded.points, 4);
    assert_eq!(
        read_back(&out),
        [
            (0, 0, true),
            (0, 100, false),
            (100, 100, false),
            (100, 0, true),
        ]
    );
}

#[test]
fn an_overlap_flag_marks_the_first_point() {
    let pen = drawn(&[(0.0, 0.0, true), (0.0, 100.0, true), (100.0, 0.0, true)]);
    let mut out = Vec::new();
    encode(&pen, true, &mut out).unwrap();
    let glyph = SimpleGlyph::read(FontData::new(&out)).unwrap();
    assert!(glyph.has_overlapping_contours());
}

#[test]
fn an_empty_outline_writes_nothing() {
    let mut out = Vec::new();
    assert_eq!(encode(&Pen::default(), false, &mut out), Ok(None));
    assert!(out.is_empty());
}

#[test]
fn what_a_glyph_cannot_hold_refuses() {
    // Both points fit, the step between them does not.
    let pen = drawn(&[(-20000.0, 0.0, true), (20000.0, 0.0, true)]);
    assert_eq!(encode(&pen, false, &mut Vec::new()), Err(OUTSIDE_I16));

    let mut pen = Pen::default();
    pen.points = vec![
        Point {
            x: 0,
            y: 0,
            on: true
        };
        MAX_POINTS + 1
    ];
    pen.ends = vec![pen.points.len()];
    assert!(encode(&pen, false, &mut Vec::new()).is_err());

    assert_eq!(coordinate(32767.4), Some(32767));
    assert_eq!(coordinate(32767.6), None);
    assert_eq!(coordinate(-32768.4), Some(-32768));
    assert_eq!(coordinate(-32768.6), None);
    assert_eq!(coordinate(f32::NAN), None);
    let mut pen = Pen::default();
    pen.move_to(40000.0, 0.0);
    assert!(pen.outside);
    let mut pen = Pen::default();
    pen.curve_to(0.0, 0.0, 1.0, 1.0, 2.0, 2.0);
    assert!(pen.cubic);
}

#[test]
fn a_damaged_face_refuses_or_reads_as_a_face() {
    for (data, step) in [(LATIN, 3), (OVERFLOW, 3), (ARABIC, 211)] {
        for at in (0..data.len()).step_by(step) {
            for mask in [0x01, 0x80, 0xFF] {
                let mut damaged = data.to_vec();
                damaged[at] ^= mask;
                let Ok(face) = Face::read(&damaged, 0) else {
                    continue;
                };
                let limited = budget(Some(1 << 20));
                let Ok(instance) = face.at_weight(700, &limited) else {
                    continue;
                };
                assert!(instance.program.len() <= 1 << 20);
                let again = Face::read(&instance.program, 0).expect("an instance reads as a face");
                assert_eq!(again.glyph_count, face.glyph_count);
                let shaped = ShapingFace::of(&instance).unwrap().shape("AV", &limited);
                if let Ok(glyphs) = shaped {
                    let measure = Measure::new(&instance);
                    for glyph in glyphs.iter() {
                        let _ = measure.advance(GlyphId::new(glyph.glyph));
                        let _ = measure.ink(GlyphId::new(glyph.glyph));
                    }
                }
            }
        }
    }
}
