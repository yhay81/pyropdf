//! Width ranges keep their glyphs without work proportional to their overlap.

use std::collections::BTreeSet;

use proptest::prelude::*;
use pyropdf_store::value::{Array, Value};

use super::widths_glyphs;

fn array(values: Vec<Value>) -> Value {
    Value::Array(Array::from(values))
}

fn integers(values: &[i64]) -> Value {
    array(values.iter().copied().map(Value::Integer).collect())
}

#[test]
fn a_hundred_thousand_full_ranges_expand_each_glyph_once() {
    let values = (0..100_000)
        .flat_map(|_| [0, 65535, 500].map(Value::Integer))
        .collect();
    assert_eq!(
        widths_glyphs(&array(values)).unwrap(),
        (0..=u32::from(u16::MAX)).collect::<Vec<_>>()
    );
}

#[test]
fn full_coverage_does_not_skip_a_malformed_suffix() {
    assert!(widths_glyphs(&integers(&[0, 65535, 500, -1, 2, 500])).is_none());
    assert!(widths_glyphs(&integers(&[0, 65535, 500, 1])).is_none());
}

#[test]
fn mixed_ranges_keep_word_boundaries_gaps_and_sorted_unique_glyphs() {
    let widths = array(vec![
        Value::Integer(65535),
        integers(&[500, 500]), // Clip the array form to the 16-bit glyph domain.
        Value::Integer(128),
        Value::Integer(255),
        Value::Integer(500),
        Value::Integer(63),
        integers(&[500, 500]),
        Value::Integer(254),
        Value::Integer(257),
        Value::Integer(500),
        Value::Integer(192),
        Value::Integer(193),
        Value::Integer(500),
        Value::Integer(0),
        Value::Integer(0),
        Value::Integer(500),
        Value::Integer(10),
        integers(&[]),
        Value::Integer(9),
        Value::Integer(8),
        Value::Integer(500),
    ]);
    let expected: Vec<_> = [0, 63, 64]
        .into_iter()
        .chain(128..=257)
        .chain([65535])
        .collect();
    assert_eq!(widths_glyphs(&widths), Some(expected));
}

#[test]
fn empty_invalid_and_ignored_width_values_keep_the_existing_behavior() {
    assert_eq!(widths_glyphs(&integers(&[])), Some(Vec::new()));
    for value in [
        Value::Null,
        integers(&[0]),
        integers(&[-1, 2, 500]),
        integers(&[65536, 65536, 500]),
        integers(&[0, 65536, 500]),
        array(vec![Value::Integer(0), Value::Null, Value::Integer(500)]),
    ] {
        assert_eq!(widths_glyphs(&value), None);
    }
    // This helper extracts glyphs; it does not validate width values or
    // require the ignored third value of a range. Preserve that behavior.
    assert_eq!(widths_glyphs(&integers(&[1, 2])), Some(vec![1, 2]));
    assert_eq!(
        widths_glyphs(&array(vec![Value::Integer(1), array(vec![Value::Null])])),
        Some(vec![1])
    );
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(128))]
    #[test]
    fn ranges_match_individual_insertion(
        ranges in prop::collection::vec((any::<u16>(), 0_u16..512, any::<bool>(), any::<bool>()), 0..32)
    ) {
        let mut values = Vec::new();
        let mut expected = BTreeSet::new();
        for (first, count, listed, reversed) in ranges {
            let first = u32::from(first);
            let end = (first + u32::from(count)).min(u32::from(u16::MAX) + 1);
            if listed {
                values.push(Value::Integer(i64::from(first)));
                values.push(array(vec![Value::Integer(500); usize::from(count)]));
                expected.extend(first..end);
            } else {
                let last = end.saturating_sub(1).max(first);
                let (first, last) = if reversed { (last, first) } else { (first, last) };
                values.extend([i64::from(first), i64::from(last), 500].map(Value::Integer));
                expected.extend(first..=last);
            }
        }
        prop_assert_eq!(widths_glyphs(&array(values)), Some(expected.into_iter().collect()));
    }
}
