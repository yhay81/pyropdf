//! Tests of the COLR painter.

use std::sync::Arc;

use proptest::prelude::*;
use pyropdf_store::budget::{Budget, BudgetVec, DocumentLimits, LimitCode};
use pyropdf_store::save::FileBase;
use pyropdf_store::value::{Bytes, Dict, Name, Value};
use pyropdf_store::version::{Document, Transaction};
use skrifa::color::CompositeMode;
use skrifa::raw::TableProvider;
use skrifa::{FontRef, GlyphId, MetadataProvider};

use super::{
    COLOUR_TOLERANCE, Colr, ColrError, Composite, Verdict, composite_of, mode_name, number,
    positive_radii, premultiplied,
};
use crate::content::shading::{Geometry, Shadings, Stop};

const PAINTS: &[u8] = include_bytes!("../testdata/colour-colr-paints.ttf");
const COLR0: &[u8] = include_bytes!("../testdata/colour-colr0.ttf");
const COLR1: &[u8] = include_bytes!("../testdata/colour-colr1.ttf");
const SBIX: &[u8] = include_bytes!("../testdata/colour-bitmaps-sbix.ttf");

const PDF: &[u8] = b"%PDF-1.7\n1 0 obj\n<</Type/Catalog/Pages 2 0 R>>\nendobj\n\
2 0 obj\n<</Type/Pages/Count 0/Kids[]>>\nendobj\n\
trailer\n<</Root 1 0 R/Size 3>>\n%%EOF\n";

fn document() -> Document {
    let budget = Budget::default();
    let base = FileBase::open(Bytes::new(PDF.to_vec()), &budget).unwrap();
    Document::new(Arc::new(base), None)
}

fn glyph(font: &FontRef<'_>, character: char) -> GlyphId {
    font.charmap().map(character).unwrap()
}

fn check(colr: &Colr<'_>, font: &FontRef<'_>, character: char) -> Option<Verdict> {
    colr.check(glyph(font, character), &Budget::default())
        .unwrap()
}

/// Returns how many objects `transaction` holds.
fn written(transaction: &Transaction) -> usize {
    let version = transaction.version();
    (1..4096)
        .filter(|&number| version.generation(number).is_some())
        .count()
}

fn dict_of(transaction: &Transaction, value: &Value) -> Dict {
    let Value::Reference(reference) = value else {
        panic!("a group is named by reference");
    };
    let object = transaction
        .version()
        .get(reference.number(), &Budget::default())
        .unwrap()
        .unwrap();
    match &**object.value() {
        Value::Stream(stream) => stream.dict().clone(),
        Value::Dict(dict) => dict.clone(),
        _ => panic!("a group is a stream"),
    }
}

#[test]
fn a_check_says_what_each_glyph_needs() {
    let font = FontRef::new(PAINTS).unwrap();
    let colr = Colr::read(&font).unwrap();
    for character in "LRTKMIOGYJAFNH".chars() {
        assert_eq!(
            check(&colr, &font, character),
            Some(Verdict::Paints),
            "{character}"
        );
    }
    let cannot = [
        ('P', "PaintComposite in the mode Xor"),
        ('Q', "a gradient stop in the text's colour"),
        ('W', "PaintSweepGradient"),
        ('V', "PaintSweepGradient"),
    ];
    for (character, paint) in cannot {
        assert_eq!(check(&colr, &font, character), Some(Verdict::Cannot(paint)));
    }
    assert_eq!(
        check(&colr, &font, 'X'),
        Some(Verdict::Damaged("its paints form a cycle"))
    );
    assert_eq!(
        check(&colr, &font, 'Z'),
        Some(Verdict::Damaged("its paints nest too deep or are too many"))
    );
    // A glyph that the table does not hold.
    assert_eq!(check(&colr, &font, ' '), None);
}

#[test]
fn colr_v0_and_v1_paint_their_layers() {
    for program in [COLR0, COLR1] {
        let font = FontRef::new(program).unwrap();
        let colr = Colr::read(&font).unwrap();
        for character in ['A', 'B', 'D', '\u{a0}'] {
            assert_eq!(check(&colr, &font, character), Some(Verdict::Paints));
        }
        assert_eq!(check(&colr, &font, 'C'), None);
    }
}

#[test]
fn a_face_with_no_colr_table_reads_as_none() {
    let font = FontRef::new(SBIX).unwrap();
    assert!(Colr::read(&font).is_none());
}

#[test]
fn every_glyph_paints_whole_as_the_check_says_or_refuses() {
    let font = FontRef::new(PAINTS).unwrap();
    let colr = Colr::read(&font).unwrap();
    let document = document();
    let budget = Budget::default();
    let mut shadings = Shadings::new();
    for id in 0..u32::from(font.maxp().unwrap().num_glyphs()) {
        let glyph = GlyphId::new(id);
        let mut transaction = document.begin();
        let name = Name::from(format!("g{id}c").as_str());
        let painted = colr.paint(
            &mut transaction,
            &mut shadings,
            glyph,
            name,
            1000.0,
            &budget,
        );
        match colr.check(glyph, &budget).unwrap() {
            Some(Verdict::Paints) => {
                let painted = painted.unwrap();
                assert!(painted.ink.is_some(), "glyph {id} paints");
            }
            Some(Verdict::Cannot(paint) | Verdict::Damaged(paint)) => {
                assert!(
                    matches!(painted, Err(ColrError::Refused(_))),
                    "glyph {id}, which needs {paint}, refuses"
                );
            }
            None => assert!(matches!(painted, Err(ColrError::Refused(_)))),
        }
    }
}

#[test]
fn a_glyph_is_one_isolated_group_that_its_procedure_shows() {
    let font = FontRef::new(PAINTS).unwrap();
    let colr = Colr::read(&font).unwrap();
    let document = document();
    let budget = Budget::default();
    let mut transaction = document.begin();
    let mut shadings = Shadings::new();
    let name = Name::from("g1c");
    let painted = colr
        .paint(
            &mut transaction,
            &mut shadings,
            glyph(&font, 'L'),
            name.clone(),
            1000.0,
            &budget,
        )
        .unwrap();

    assert_eq!(painted.procedure, b"1000 0 d0\n/g1c Do");
    assert_eq!(painted.width, 1000.0);
    let [x0, y0, x1, y1] = painted.ink.unwrap();
    assert!(x0 <= 100.0 && y0 <= 0.0 && x1 >= 900.0 && y1 >= 800.0);
    let [(category, named, form)] = &painted.resources[..] else {
        panic!("one group");
    };
    assert_eq!((category.as_bytes(), named), (&b"XObject"[..], &name));
    let form = dict_of(&transaction, form);
    let Some(Value::Dict(group)) = form.get(b"Group") else {
        panic!("a form with a group");
    };
    assert_eq!(
        group.get(b"S"),
        Some(&Value::Name(Name::from("Transparency")))
    );
    assert_eq!(group.get(b"I"), Some(&Value::Boolean(true)));
}

#[test]
fn glyphs_share_the_shadings_that_are_equal() {
    let font = FontRef::new(PAINTS).unwrap();
    let colr = Colr::read(&font).unwrap();
    let document = document();
    let budget = Budget::default();
    let mut transaction = document.begin();
    let mut shadings = Shadings::new();
    let paint = |transaction: &mut Transaction, shadings: &mut Shadings, character, name: &str| {
        let glyph = glyph(&font, character);
        colr.paint(
            transaction,
            shadings,
            glyph,
            Name::from(name),
            1000.0,
            &budget,
        )
        .unwrap();
    };
    paint(&mut transaction, &mut shadings, 'L', "g1c");
    let after_one = written(&transaction);
    // G paints L through PaintColrGlyph: its own group, and L's shading.
    paint(&mut transaction, &mut shadings, 'G', "g9c");
    assert_eq!(written(&transaction), after_one + 1);
}

#[test]
fn a_limit_refuses_a_glyph_before_it_writes_past_the_limit() {
    let font = FontRef::new(PAINTS).unwrap();
    let colr = Colr::read(&font).unwrap();
    let document = document();
    let limits = DocumentLimits {
        max_memory_size: Some(65_536),
        ..DocumentLimits::default()
    };
    let budget = Budget::new(&limits);
    let mut transaction = document.begin();
    let mut shadings = Shadings::new();
    let heavy = glyph(&font, 'H');
    let refused = colr.paint(
        &mut transaction,
        &mut shadings,
        heavy,
        Name::from("h"),
        1000.0,
        &budget,
    );
    assert!(matches!(refused, Err(ColrError::Store(_))), "{refused:?}");
    assert!(budget.used(LimitCode::MemorySize) <= 65_536);
}

#[test]
fn the_modes_that_pdf_does_not_express_are_named() {
    let unwritten = [
        CompositeMode::SrcAtop,
        CompositeMode::DestAtop,
        CompositeMode::Xor,
        CompositeMode::Plus,
    ];
    for mode in unwritten {
        assert_eq!(composite_of(mode), None);
        assert!(mode_name(mode).starts_with("PaintComposite in the mode "));
    }
    assert_eq!(
        composite_of(CompositeMode::SrcOver),
        Some(Composite::Blend("Normal"))
    );
    assert_eq!(
        composite_of(CompositeMode::HslLuminosity),
        Some(Composite::Blend("Luminosity"))
    );
}

#[test]
fn a_number_is_short_and_refuses_outside_the_range_of_a_real() {
    let budget = Budget::default();
    let written = |value: f64, decimals: usize| {
        let mut out = BudgetVec::new(&budget);
        number(&mut out, value, decimals).map(|()| String::from_utf8(out.into_vec()).unwrap())
    };
    assert_eq!(written(0.5, 3).unwrap(), "0.5 ");
    assert_eq!(written(100.0, 3).unwrap(), "100 ");
    assert_eq!(written(-0.0001, 3).unwrap(), "0 ");
    assert_eq!(written(1.0 / 3.0, 4).unwrap(), "0.3333 ");
    assert!(written(1e39, 3).is_err());
    assert!(written(f64::NAN, 3).is_err());
    assert!(written(f64::INFINITY, 3).is_err());
}

#[test]
fn a_radius_that_reaches_zero_cuts_the_line_there() {
    let budget = Budget::default();
    let stop = |offset: f64, red: f64| Stop {
        offset,
        color: [red, 0.0, 0.0],
        alpha: 1.0,
    };
    let mut stops = BudgetVec::new(&budget);
    stops
        .try_extend_from_slice(&[stop(0.0, 0.0), stop(1.0, 1.0)])
        .unwrap();
    // From a radius of -100 to one of 100: the circles start halfway.
    let geometry = Geometry::Radial {
        start: [0.0, 0.0],
        start_radius: -100.0,
        end: [200.0, 0.0],
        end_radius: 100.0,
    };
    let Some(Geometry::Radial {
        start,
        start_radius,
        end_radius,
        ..
    }) = positive_radii(geometry, &mut stops).unwrap()
    else {
        panic!("circles remain");
    };
    assert_eq!(
        (start, start_radius, end_radius),
        ([100.0, 0.0], 0.0, 100.0)
    );
    assert_eq!(stops.len(), 2);
    assert_eq!((stops[0].offset, stops[0].color[0]), (0.0, 0.5));
    assert_eq!((stops[1].offset, stops[1].color[0]), (1.0, 1.0));

    let none = Geometry::Radial {
        start: [0.0, 0.0],
        start_radius: -1.0,
        end: [1.0, 0.0],
        end_radius: 0.0,
    };
    assert!(positive_radii(none, &mut stops).unwrap().is_none());
}

/// Returns the colour that PDF draws at `t` of `stops`, premultiplied: it
/// interpolates colour and alpha apart.
fn drawn(stops: &[Stop], t: f64) -> [f64; 3] {
    let after = stops.iter().position(|stop| stop.offset >= t).unwrap();
    let (low, high) = (stops[after.saturating_sub(1)], stops[after]);
    let span = high.offset - low.offset;
    let part = if span > 0.0 {
        (t - low.offset) / span
    } else {
        0.0
    };
    let alpha = low.alpha + (high.alpha - low.alpha) * part;
    [0, 1, 2].map(|channel| {
        let colour = low.color[channel] + (high.color[channel] - low.color[channel]) * part;
        colour * alpha
    })
}

proptest! {
    #[test]
    fn premultiplied_stops_stay_within_the_tolerance_of_the_font(
        from in prop::array::uniform3(0.0..=1.0_f64),
        to in prop::array::uniform3(0.0..=1.0_f64),
        alphas in (0.0..=1.0_f64, 0.0..=1.0_f64),
    ) {
        let budget = Budget::default();
        let first = Stop { offset: 0.0, color: from, alpha: alphas.0 };
        let last = Stop { offset: 1.0, color: to, alpha: alphas.1 };
        let mut stops = BudgetVec::new(&budget);
        stops.try_push(first).unwrap();
        premultiplied(first, last, &mut stops).unwrap();
        prop_assert!(stops.len() <= 1 + super::MAX_PIECES as usize + 1);
        prop_assert!(stops.windows(2).all(|pair| pair[0].offset <= pair[1].offset));
        for step in 0..=200 {
            let t = f64::from(step) / 200.0;
            let font = [0, 1, 2].map(|channel| {
                from[channel] * alphas.0 * (1.0 - t) + to[channel] * alphas.1 * t
            });
            let pdf = drawn(&stops, t);
            for channel in 0..3 {
                // Sixteen pieces bound the error, not the tolerance, for an
                // alpha that falls to almost nothing.
                prop_assert!(
                    (pdf[channel] - font[channel]).abs() <= COLOUR_TOLERANCE + 1e-9,
                    "{t}: {pdf:?} against {font:?}",
                );
            }
        }
    }
}
