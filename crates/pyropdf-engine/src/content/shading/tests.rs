//! The writer of shadings: what it shares, what it refuses, and that nothing
//! it writes holds a number a reader cannot hold (F9).

use std::sync::Arc;

use proptest::prelude::*;
use pyropdf_store::ErrorKind;
use pyropdf_store::value::Array;
use pyropdf_store::version::{Document, MemoryBase};

use super::*;

const WHOLE: [f64; 4] = [0.0, 0.0, 100.0, 100.0];

fn stop(offset: f64, red: f64, alpha: f64) -> Stop {
    Stop {
        offset,
        color: [red, 0.0, 1.0 - red],
        alpha,
    }
}

const AXIS: Geometry = Geometry::Axial {
    start: [10.0, 0.0],
    end: [30.0, 0.0],
};

const RINGS: Geometry = Geometry::Radial {
    start: [50.0, 50.0],
    start_radius: 0.0,
    end: [50.0, 50.0],
    end_radius: 10.0,
};

fn document() -> Document {
    Document::new(Arc::new(MemoryBase::new(Dict::new())), None)
}

fn gradient(geometry: Geometry, stops: &[Stop], extend: Extend) -> Gradient<'_> {
    Gradient {
        geometry,
        stops,
        extend,
    }
}

/// Returns every written object's value.
fn written(transaction: &Transaction) -> Vec<Value> {
    let version = transaction.version();
    let budget = Budget::default();
    (1..1024)
        .filter(|&number| version.generation(number).is_some())
        .map(|number| Value::clone(version.get(number, &budget).unwrap().unwrap().value()))
        .collect()
}

fn count(objects: &[Value], key: &str) -> usize {
    objects
        .iter()
        .filter(|object| matches!(object, Value::Dict(dict) if dict.get(key).is_some()))
        .count()
}

fn numbers(value: &Value, out: &mut Vec<f64>) {
    match value {
        Value::Real(real) => out.push(real.value()),
        Value::Array(array) => array.iter().for_each(|item| numbers(item, out)),
        Value::Dict(dict) => dict.iter().for_each(|(_, item)| numbers(item, out)),
        Value::Stream(stream) => stream
            .dict()
            .iter()
            .for_each(|(_, item)| numbers(item, out)),
        _ => {}
    }
}

fn array_of<'v>(dict: &'v Dict, key: &str) -> &'v Array {
    match dict.get(key) {
        Some(Value::Array(array)) => array,
        other => panic!("{key} is {other:?}"),
    }
}

fn reals_of(array: &Array) -> Vec<f64> {
    array
        .iter()
        .map(|value| match value {
            Value::Real(real) => real.value(),
            Value::Integer(integer) => *integer as f64,
            other => panic!("not a number: {other:?}"),
        })
        .collect()
}

fn dict_of(transaction: &Transaction, reference: Reference) -> Dict {
    match transaction
        .version()
        .get(reference.number(), &Budget::default())
        .unwrap()
        .unwrap()
        .value()
        .as_ref()
    {
        Value::Dict(dict) => dict.clone(),
        other => panic!("not a dictionary: {other:?}"),
    }
}

#[test]
fn equal_objects_are_written_once() {
    let document = document();
    let mut transaction = document.begin();
    let mut table = Shadings::new();
    let budget = Budget::default();
    let stops = [stop(0.0, 0.0, 1.0), stop(1.0, 1.0, 1.0)];
    let first = table
        .paint(
            &mut transaction,
            &gradient(AXIS, &stops, Extend::Pad),
            WHOLE,
            &budget,
        )
        .unwrap();
    let again = table
        .paint(
            &mut transaction,
            &gradient(AXIS, &stops, Extend::Pad),
            [1.0, 2.0, 3.0, 4.0],
            &budget,
        )
        .unwrap();
    assert_eq!(first, again);
    // Another line shares the function.
    let other = table
        .paint(
            &mut transaction,
            &gradient(RINGS, &stops, Extend::Pad),
            WHOLE,
            &budget,
        )
        .unwrap();
    assert_ne!(first.shading, other.shading);
    let objects = written(&transaction);
    assert_eq!(count(&objects, "FunctionType"), 1);
    assert_eq!(count(&objects, "ShadingType"), 2);
}

#[test]
fn stops_of_one_alpha_need_no_mask() {
    let document = document();
    let mut transaction = document.begin();
    let mut table = Shadings::new();
    let budget = Budget::default();
    let stops = [stop(0.0, 0.0, 0.5), stop(1.0, 1.0, 0.5)];
    let painted = table
        .paint(
            &mut transaction,
            &gradient(AXIS, &stops, Extend::Pad),
            WHOLE,
            &budget,
        )
        .unwrap();
    assert_eq!((painted.mask, painted.alpha), (None, 0.5));
}

#[test]
fn stops_that_differ_in_alpha_paint_through_a_luminosity_mask() {
    let document = document();
    let mut transaction = document.begin();
    let mut table = Shadings::new();
    let budget = Budget::default();
    let stops = [stop(0.0, 0.0, 1.0), stop(1.0, 1.0, 0.0)];
    let painted = table
        .paint(
            &mut transaction,
            &gradient(AXIS, &stops, Extend::Pad),
            WHOLE,
            &budget,
        )
        .unwrap();
    assert_eq!(painted.alpha, 1.0);
    let state = dict_of(&transaction, painted.mask.unwrap());
    let Some(Value::Dict(mask)) = state.get("SMask") else {
        panic!("no soft mask: {state:?}");
    };
    assert_eq!(mask.get("S"), Some(&name("Luminosity")));
    let objects = written(&transaction);
    // The mask's own function and shading, in DeviceGray.
    assert_eq!(count(&objects, "FunctionType"), 2);
    assert_eq!(count(&objects, "ShadingType"), 2);
    assert!(objects.iter().any(|object| matches!(
        object,
        Value::Dict(dict) if dict.get("ColorSpace") == Some(&name("DeviceGray"))
    )));
}

#[test]
fn a_function_pads_to_the_ends_and_breaks_at_equal_offsets() {
    let document = document();
    let mut transaction = document.begin();
    let mut table = Shadings::new();
    let budget = Budget::default();
    let stops = [
        stop(0.25, 0.0, 1.0),
        stop(0.5, 1.0, 1.0),
        stop(0.5, 0.0, 1.0),
    ];
    let painted = table
        .paint(
            &mut transaction,
            &gradient(AXIS, &stops, Extend::Pad),
            WHOLE,
            &budget,
        )
        .unwrap();
    let shading = dict_of(&transaction, painted.shading);
    let Some(Value::Reference(function)) = shading.get("Function") else {
        panic!("no function: {shading:?}");
    };
    let function = dict_of(&transaction, *function);
    assert_eq!(function.get("FunctionType"), Some(&Value::Integer(3)));
    assert_eq!(reals_of(array_of(&function, "Bounds")), [0.25, 0.5]);
    assert_eq!(array_of(&function, "Functions").len(), 3);
    assert_eq!(array_of(&function, "Encode").len(), 6);
}

#[test]
fn a_repeating_gradient_draws_the_cycles_that_the_box_needs() {
    let document = document();
    let mut transaction = document.begin();
    let mut table = Shadings::new();
    let budget = Budget::default();
    let stops = [stop(0.0, 0.0, 1.0), stop(1.0, 1.0, 1.0)];
    let painted = table
        .paint(
            &mut transaction,
            &gradient(AXIS, &stops, Extend::Reflect),
            WHOLE,
            &budget,
        )
        .unwrap();
    let shading = dict_of(&transaction, painted.shading);
    // From x = -10 to 110, in cycles of 20 from 10.
    assert_eq!(
        reals_of(array_of(&shading, "Coords")),
        [-10.0, 0.0, 110.0, 0.0]
    );
    let Some(Value::Reference(function)) = shading.get("Function") else {
        panic!("no function: {shading:?}");
    };
    let function = dict_of(&transaction, *function);
    let encode = reals_of(array_of(&function, "Encode"));
    assert_eq!(array_of(&function, "Functions").len(), 6);
    // The cycle from -1 to 0 runs backward, and every other one after it.
    assert_eq!(encode[..4], [1.0, 0.0, 0.0, 1.0]);
}

#[test]
fn a_repeating_radial_gradient_stops_where_its_radius_reaches_zero() {
    let geometry = Geometry::Radial {
        start: [0.0, 0.0],
        start_radius: 5.0,
        end: [0.0, 0.0],
        end_radius: 10.0,
    };
    let span = Span::of(
        &gradient(geometry, &[stop(0.0, 0.0, 1.0)], Extend::Repeat),
        [0.0, 0.0, 30.0, 40.0],
    )
    .unwrap()
    .unwrap();
    // A radius of 0 at -1, and of 50 at the farthest corner, at 9.
    assert_eq!(span.low, -1.0);
    assert!(span.high >= 9.0);
    let Geometry::Radial {
        start_radius,
        end_radius,
        ..
    } = span.geometry(geometry)
    else {
        panic!("not radial");
    };
    assert_eq!(start_radius, 0.0);
    assert!(end_radius >= 50.0);
}

#[test]
fn a_partial_first_cycle_encodes_its_part_of_the_cycle() {
    let span = Span {
        low: -1.5,
        high: 1.0,
        first: -2.0,
        last: 1.0,
        reflect: true,
    };
    let (bounds, encode) = span.stitches();
    assert_eq!(bounds, [0.2, 0.6]);
    assert_eq!(encode, [0.5, 1.0, 1.0, 0.0, 0.0, 1.0]);
}

#[test]
fn it_refuses_what_it_cannot_draw() {
    let document = document();
    let mut transaction = document.begin();
    let mut table = Shadings::new();
    let budget = Budget::default();
    let good = [stop(0.0, 0.0, 1.0), stop(1.0, 1.0, 1.0)];
    let backward = [stop(0.5, 0.0, 1.0), stop(0.4, 0.0, 1.0)];
    let below = [stop(-0.5, 0.0, 1.0)];
    let above = [stop(1.5, 0.0, 1.0)];
    let bright = [stop(0.0, 2.0, 1.0)];
    let negative = [stop(0.0, 1.0, -1.0)];
    let unknown = [stop(f64::NAN, 1.0, 1.0)];
    let invalid: &[(Gradient<'_>, [f64; 4])] = &[
        (gradient(AXIS, &[], Extend::Pad), WHOLE),
        (gradient(AXIS, &below, Extend::Pad), WHOLE),
        (gradient(AXIS, &above, Extend::Pad), WHOLE),
        (gradient(AXIS, &backward, Extend::Pad), WHOLE),
        (gradient(AXIS, &bright, Extend::Pad), WHOLE),
        (gradient(AXIS, &negative, Extend::Pad), WHOLE),
        (gradient(AXIS, &unknown, Extend::Pad), WHOLE),
        (gradient(AXIS, &good, Extend::Pad), [f64::NAN; 4]),
        (gradient(AXIS, &good, Extend::Pad), [1.0, 1.0, 0.0, 0.0]),
        (
            gradient(
                Geometry::Axial {
                    start: [f64::INFINITY, 0.0],
                    end: [0.0, 0.0],
                },
                &good,
                Extend::Pad,
            ),
            WHOLE,
        ),
        (
            gradient(
                Geometry::Radial {
                    start: [0.0, 0.0],
                    start_radius: -1.0,
                    end: [0.0, 0.0],
                    end_radius: 1.0,
                },
                &good,
                Extend::Pad,
            ),
            WHOLE,
        ),
    ];
    for (gradient, area) in invalid {
        let result = table.paint(&mut transaction, gradient, *area, &budget);
        assert!(
            matches!(result, Err(ShadingError::Invalid(_))),
            "{gradient:?}: {result:?}"
        );
    }
    let degenerate = [
        Geometry::Axial {
            start: [1.0, 2.0],
            end: [1.0, 2.0],
        },
        Geometry::Radial {
            start: [1.0, 2.0],
            start_radius: 3.0,
            end: [1.0, 2.0],
            end_radius: 3.0,
        },
        Geometry::Radial {
            start: [1.0, 2.0],
            start_radius: 0.0,
            end: [3.0, 4.0],
            end_radius: 0.0,
        },
    ];
    for geometry in degenerate {
        let result = table.paint(
            &mut transaction,
            &gradient(geometry, &good, Extend::Pad),
            WHOLE,
            &budget,
        );
        assert_eq!(result, Err(ShadingError::Degenerate));
    }
    assert!(written(&transaction).is_empty());
    assert!(matches!(
        table.pattern(
            &mut transaction,
            Reference::new(1, 0),
            [f64::NAN; 6],
            &budget
        ),
        Err(ShadingError::Invalid(_))
    ));
}

#[test]
fn too_many_cycles_refuse_with_a_limit() {
    let document = document();
    let mut transaction = document.begin();
    let mut table = Shadings::new();
    let budget = Budget::default();
    let thin = Geometry::Axial {
        start: [0.0, 0.0],
        end: [1e-3, 0.0],
    };
    let stops = [stop(0.0, 0.0, 1.0), stop(1.0, 1.0, 1.0)];
    let result = table.paint(
        &mut transaction,
        &gradient(thin, &stops, Extend::Repeat),
        WHOLE,
        &budget,
    );
    let Err(ShadingError::Store(error)) = result else {
        panic!("not refused: {result:?}");
    };
    let ErrorKind::Limit(refusal) = error.kind() else {
        panic!("not a limit: {error}");
    };
    assert_eq!(refusal.code(), LimitCode::ContentElementCount);
    assert!(written(&transaction).is_empty());
}

#[test]
fn a_budget_refuses_before_anything_is_written() {
    let document = document();
    let mut transaction = document.begin();
    let mut table = Shadings::new();
    let stops: Vec<Stop> = (0..=1000)
        .map(|index| stop(f64::from(index) / 1000.0, 0.5, 1.0))
        .collect();
    let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(64 * 1024));
    let result = table.paint(
        &mut transaction,
        &gradient(AXIS, &stops, Extend::Pad),
        WHOLE,
        &budget,
    );
    let Err(ShadingError::Store(error)) = result else {
        panic!("not refused: {result:?}");
    };
    assert_eq!(
        error.limit().map(LimitExceeded::code),
        Some(LimitCode::MemorySize)
    );
    assert!(written(&transaction).is_empty());
    assert_eq!(budget.used(LimitCode::MemorySize), 0);
}

#[test]
fn an_object_that_a_rollback_removed_is_written_again() {
    let document = document();
    let mut transaction = document.begin();
    let mut table = Shadings::new();
    let budget = Budget::default();
    let stops = [stop(0.0, 0.0, 1.0), stop(1.0, 1.0, 1.0)];
    let wanted = gradient(AXIS, &stops, Extend::Pad);
    let savepoint = transaction.savepoint();
    let first = table
        .paint(&mut transaction, &wanted, WHOLE, &budget)
        .unwrap();
    transaction.rollback(savepoint);
    let again = table
        .paint(&mut transaction, &wanted, WHOLE, &budget)
        .unwrap();
    assert_ne!(first.shading, again.shading);
    assert!(
        transaction
            .version()
            .generation(again.shading.number())
            .is_some()
    );
}

/// Any 64 bits, biased to the numbers that a line or a colour may be.
fn number() -> impl Strategy<Value = f64> {
    prop_oneof![
        3 => 0.0..=1.0_f64,
        3 => -1e4..1e4_f64,
        1 => any::<f64>(),
        1 => prop::sample::select(vec![
            0.0, -0.0, 1.0, f64::NAN, f64::INFINITY, f64::NEG_INFINITY, f64::MIN_POSITIVE,
            REAL_MAX, -REAL_MAX, 1e-300, 1e300,
        ]),
    ]
}

fn arbitrary_stop() -> impl Strategy<Value = Stop> {
    (number(), number(), number(), number(), number()).prop_map(
        |(offset, red, green, blue, alpha)| Stop {
            offset,
            color: [red, green, blue],
            alpha,
        },
    )
}

fn arbitrary_geometry() -> impl Strategy<Value = Geometry> {
    prop_oneof![
        (number(), number(), number(), number()).prop_map(|(a, b, c, d)| Geometry::Axial {
            start: [a, b],
            end: [c, d],
        }),
        (number(), number(), number(), number(), number(), number()).prop_map(
            |(a, b, r, c, d, s)| Geometry::Radial {
                start: [a, b],
                start_radius: r,
                end: [c, d],
                end_radius: s,
            }
        ),
    ]
}

fn arbitrary_extend() -> impl Strategy<Value = Extend> {
    prop::sample::select(vec![
        Extend::None,
        Extend::Pad,
        Extend::Repeat,
        Extend::Reflect,
    ])
}

/// Repairs `stops` into what the writer takes, so that a run reaches the
/// arithmetic of the lines and the cycles.
fn in_order(stops: Vec<Stop>) -> Vec<Stop> {
    let unit = |value: f64| value.abs().fract();
    let mut stops: Vec<Stop> = stops
        .into_iter()
        .map(|stop| Stop {
            offset: unit(stop.offset),
            color: stop.color.map(unit),
            alpha: unit(stop.alpha),
        })
        .filter(|stop| stop.offset.is_finite() && stop.alpha.is_finite())
        .filter(|stop| stop.color.iter().all(|value| value.is_finite()))
        .collect();
    stops.sort_by(|a, b| a.offset.total_cmp(&b.offset));
    stops
}

proptest! {
    #[test]
    fn arbitrary_gradients_never_panic_or_write_a_number_a_reader_cannot_hold(
        geometry in arbitrary_geometry(),
        stops in prop::collection::vec(arbitrary_stop(), 0..8),
        extend in arbitrary_extend(),
        area in [number(), number(), number(), number()],
        repaired in any::<bool>(),
    ) {
        let document = document();
        let mut transaction = document.begin();
        let mut table = Shadings::new();
        let budget = Budget::default();
        let stops = if repaired { in_order(stops) } else { stops };
        let area = if repaired {
            let (x0, x1) = (area[0].min(area[2]), area[0].max(area[2]));
            let (y0, y1) = (area[1].min(area[3]), area[1].max(area[3]));
            [x0, y0, x1, y1]
        } else {
            area
        };
        let wanted = gradient(geometry, &stops, extend);
        if let Ok(painted) = table.paint(&mut transaction, &wanted, area, &budget) {
            let identity = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0];
            table.pattern(&mut transaction, painted.shading, identity, &budget).unwrap();
            for object in written(&transaction) {
                let mut found = Vec::new();
                numbers(&object, &mut found);
                for number in found {
                    prop_assert!(number.abs() <= REAL_MAX, "{number} in {object:?}");
                }
                let Value::Dict(dict) = &object else { continue };
                // A stitching function's bounds rise, as readers hold them.
                if let Some(Value::Array(bounds)) = dict.get("Bounds") {
                    let bounds = reals_of(bounds);
                    prop_assert!(
                        bounds.iter().all(|bound| (0.0..=1.0).contains(bound)),
                        "{bounds:?}"
                    );
                    prop_assert!(
                        bounds.windows(2).all(|pair| (pair[0] as f32) < pair[1] as f32),
                        "{bounds:?}"
                    );
                    let functions = array_of(dict, "Functions").len();
                    prop_assert_eq!(array_of(dict, "Encode").len(), 2 * functions);
                    prop_assert_eq!(functions, bounds.len() + 1);
                }
                // A radius is never negative.
                if dict.get("ShadingType") == Some(&Value::Integer(3)) {
                    let coords = reals_of(array_of(dict, "Coords"));
                    prop_assert!(coords[2] >= 0.0 && coords[5] >= 0.0, "{coords:?}");
                }
            }
        }
    }
}
