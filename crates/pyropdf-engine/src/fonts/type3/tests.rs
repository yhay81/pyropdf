use std::sync::Arc;

use proptest::prelude::*;
use pyropdf_store::budget::{Budget, DocumentLimits, LimitCode};
use pyropdf_store::save::FileBase;
use pyropdf_store::value::{Bytes, Dict, Name, Reference, Value};
use pyropdf_store::version::{Document, Version};

use super::{
    Code, Fonts, GLYPHS_PER_FONT, Glyph, MAX_MAPPED_UNITS, NO_TEXT, read_cmap, write_cmap,
};

const PDF: &[u8] = b"%PDF-1.7\n1 0 obj\n<</Type/Catalog/Pages 2 0 R>>\nendobj\n\
2 0 obj\n<</Type/Pages/Count 0/Kids[]>>\nendobj\n\
trailer\n<</Root 1 0 R/Size 3>>\n%%EOF\n";

fn document() -> Document {
    let budget = Budget::default();
    let base = FileBase::open(Bytes::new(PDF.to_vec()), &budget).unwrap();
    Document::new(Arc::new(base), None)
}

fn glyph(index: usize) -> Glyph {
    let name = format!("g{index}");
    Glyph {
        name: Name::from(name.as_str()),
        width: 500.0 + index as f64,
        ink: Some([0.0, -1.0, index as f64, 10.0]),
        procedure: format!("{} 0 d0", 500 + index).into_bytes(),
        resources: vec![(
            Name::from("XObject"),
            Name::from(name.as_str()),
            Value::Reference(Reference::new(1, 0)),
        )],
    }
}

/// Commits `glyphs` to the fonts `numbers`, and returns the fonts after it.
fn add(document: &Document, numbers: &[u32], glyphs: Vec<Glyph>) -> Vec<u32> {
    let budget = Budget::default();
    let mut transaction = document.begin();
    let version = transaction.version();
    let mut fonts = Fonts::read(version, numbers, 1000, &budget).unwrap();
    fonts.add(&mut transaction, glyphs).unwrap();
    fonts.write(&mut transaction).unwrap();
    let numbers = fonts.numbers().collect();
    drop(fonts);
    transaction.commit(&budget).unwrap();
    numbers
}

fn font(version: &Version, number: u32) -> Dict {
    let object = version.get(number, &Budget::default()).unwrap().unwrap();
    let Value::Dict(dict) = &**object.value() else {
        panic!("a font is a dictionary");
    };
    dict.clone()
}

fn count(dict: &Dict, key: &str) -> usize {
    match dict.get(key) {
        Some(Value::Array(array)) => array.len(),
        Some(Value::Dict(dict)) => dict.len(),
        _ => panic!("{key} is an array or a dictionary"),
    }
}

#[test]
fn a_font_holds_256_glyphs_and_a_new_one_takes_the_next() {
    let document = document();
    let glyphs = (0..GLYPHS_PER_FONT + 10).map(glyph).collect();
    let numbers = add(&document, &[], glyphs);
    assert_eq!(numbers.len(), 2);

    let version = document.current();
    let first = font(&version, numbers[0]);
    assert_eq!(count(&first, "CharProcs"), GLYPHS_PER_FONT);
    assert_eq!(count(&first, "Widths"), GLYPHS_PER_FONT);
    assert_eq!(first.get("LastChar"), Some(&Value::Integer(255)));
    assert_eq!(count(&font(&version, numbers[1]), "CharProcs"), 10);

    let budget = Budget::default();
    let held = Fonts::read(&version, &numbers, 1000, &budget).unwrap();
    assert_eq!(
        held.find(b"g255"),
        Some(Code {
            font: numbers[0],
            code: 255
        })
    );
    assert_eq!(
        held.find(b"g256"),
        Some(Code {
            font: numbers[1],
            code: 0
        })
    );
    assert_eq!(held.find(b"g9999"), None);
}

#[test]
fn a_later_drawing_fills_the_font_and_keeps_the_codes() {
    let document = document();
    let numbers = add(&document, &[], (0..10).map(glyph).collect());
    let again = add(&document, &numbers, (10..20).map(glyph).collect());
    assert_eq!(again, numbers);

    let version = document.current();
    let budget = Budget::default();
    let held = Fonts::read(&version, &numbers, 1000, &budget).unwrap();
    for index in 0..20 {
        let code = held.find(format!("g{index}").as_bytes()).unwrap();
        assert_eq!(usize::from(code.code), index);
    }
    let dict = font(&version, numbers[0]);
    let Some(Value::Array(bounds)) = dict.get("FontBBox") else {
        panic!("a font has a box");
    };
    assert_eq!(bounds.get(2), Some(&Value::Integer(19)));
    assert_eq!(bounds.get(1), Some(&Value::Integer(-1)));
}

#[test]
fn undo_forgets_the_glyphs_of_the_undone_step() {
    let document = document();
    let numbers = add(&document, &[], (0..10).map(glyph).collect());
    let budget = Budget::default();
    let before = document.current();
    add(&document, &numbers, (10..20).map(glyph).collect());

    let held = Fonts::read(&before, &numbers, 1000, &budget).unwrap();
    assert!(held.find(b"g9").is_some());
    assert!(held.find(b"g10").is_none());
}

#[test]
fn a_number_that_is_no_longer_a_font_of_this_shape_is_passed_over() {
    let document = document();
    let numbers = add(&document, &[], (0..2).map(glyph).collect());
    let budget = Budget::default();
    let mut transaction = document.begin();
    transaction
        .write(numbers[0], &Value::Integer(1), &budget)
        .unwrap();
    let version = transaction.version();
    let mut fonts = Fonts::read(version, &numbers, 1000, &budget).unwrap();
    assert_eq!(fonts.numbers().count(), 0);
    // A number that holds nothing is passed over as well.
    let unused = Fonts::read(version, &[4_000_000], 1000, &budget).unwrap();
    assert_eq!(unused.numbers().count(), 0);
    fonts.add(&mut transaction, vec![glyph(0)]).unwrap();
    assert_eq!(fonts.numbers().count(), 1);
    assert_ne!(fonts.numbers().next(), Some(numbers[0]));
}

#[test]
fn the_first_real_text_of_a_code_stays() {
    let document = document();
    let numbers = add(&document, &[], (0..2).map(glyph).collect());
    let budget = Budget::default();
    let mut transaction = document.begin();
    let version = transaction.version();
    let mut fonts = Fonts::read(version, &numbers, 1000, &budget).unwrap();
    let code = fonts.find(b"g0").unwrap();
    assert_eq!(fonts.text(code), None);
    fonts.map_text(code, NO_TEXT).unwrap();
    assert_eq!(fonts.text(code), Some(NO_TEXT));
    fonts.map_text(code, "first").unwrap();
    fonts.map_text(code, "second").unwrap();
    assert_eq!(fonts.text(code), Some("first"));
    let long = "x".repeat(MAX_MAPPED_UNITS + 1);
    let other = fonts.find(b"g1").unwrap();
    fonts.map_text(other, &long).unwrap();
    assert_eq!(fonts.text(other), None);
    fonts.write(&mut transaction).unwrap();

    let version = transaction.version();
    let read = Fonts::read(version, &numbers, 1000, &budget).unwrap();
    assert_eq!(read.text(code), Some("first"));
}

#[test]
fn a_limit_refuses_and_returns_what_was_charged() {
    let document = document();
    let limits = DocumentLimits {
        max_memory_size: Some(4096),
        ..DocumentLimits::default()
    };
    let budget = Budget::new(&limits);
    let mut transaction = document.begin();
    let version = transaction.version();
    let refused = {
        let mut fonts = Fonts::read(version, &[], 1000, &budget).unwrap();
        fonts
            .add(&mut transaction, (0..256).map(glyph).collect())
            .and_then(|()| fonts.write(&mut transaction))
    };
    assert!(refused.is_err());
    assert!(budget.used(LimitCode::MemorySize) <= 4096);
}

proptest! {
    #[test]
    fn a_cmap_reads_back_what_it_wrote(
        texts in prop::collection::vec("[a-z\u{3040}-\u{309f}\u{1f600}-\u{1f64f}]{0,8}", 0..256),
    ) {
        let budget = Budget::default();
        let cmap = write_cmap(&texts, &budget).unwrap();
        prop_assert_eq!(read_cmap(&cmap, texts.len()), Some(texts));
    }

    #[test]
    fn a_damaged_cmap_reads_as_none_or_as_text(data in prop::collection::vec(any::<u8>(), 0..256)) {
        let _ = read_cmap(&data, 4);
    }
}
