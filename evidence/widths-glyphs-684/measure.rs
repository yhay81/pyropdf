// run.py prefixes this harness with widths_glyphs extracted from the base
// and current source. Both use the actual store's Array and Value types.
use std::hint::black_box;
use std::time::Instant;

use pyropdf_store::value::{Array, Value};

fn array(values: Vec<Value>) -> Value {
    Value::Array(Array::from(values))
}

fn time(function: fn(&Value) -> Option<Vec<u32>>, value: &Value, repetitions: usize) -> f64 {
    let start = Instant::now();
    for _ in 0..repetitions {
        black_box(function(black_box(value)).unwrap());
    }
    start.elapsed().as_secs_f64() * 1_000_000.0 / repetitions as f64
}

fn median(values: &mut [f64]) -> f64 {
    values.sort_by(f64::total_cmp);
    values[values.len() / 2]
}

fn measure(
    name: &str,
    value: &Value,
    expected: &[u32],
    compare: bool,
    repetitions: usize,
    rounds: usize,
) {
    assert_eq!(after::widths_glyphs(value).unwrap(), expected, "{name}");
    if compare {
        assert_eq!(before::widths_glyphs(value).unwrap(), expected, "{name}");
    }
    let mut before = Vec::new();
    let mut after = Vec::new();
    for round in 0..rounds {
        // Reverse which implementation runs first on alternating rounds.
        if compare && round % 2 == 0 {
            before.push(time(before::widths_glyphs, value, repetitions));
        }
        after.push(time(after::widths_glyphs, value, repetitions));
        if compare && round % 2 != 0 {
            before.push(time(before::widths_glyphs, value, repetitions));
        }
    }
    let before = if compare {
        format!("{:.3}", median(&mut before))
    } else {
        "not run".to_string()
    };
    println!(
        "| {name} | {} | {before} | {:.3} |",
        expected.len(),
        median(&mut after)
    );
}

fn main() {
    let rounds: usize = std::env::args().nth(1).unwrap().parse().unwrap();
    println!("| Input | Glyphs | Base us | Current us |");
    println!("| --- | ---: | ---: | ---: |");
    measure("empty", &array(Vec::new()), &[], true, 1000, rounds);
    measure(
        "one listed glyph",
        &array(vec![Value::Integer(0), array(vec![Value::Integer(500)])]),
        &[0],
        true,
        1000,
        rounds,
    );
    let sparse: Vec<u32> = (0..16).map(|at| at * 4096).collect();
    let value = array(
        sparse
            .iter()
            .flat_map(|&glyph| {
                [
                    Value::Integer(i64::from(glyph)),
                    array(vec![Value::Integer(500)]),
                ]
            })
            .collect(),
    );
    measure(
        "16 sparse listed glyphs",
        &value,
        &sparse,
        true,
        1000,
        rounds,
    );
    let all: Vec<u32> = (0..=u32::from(u16::MAX)).collect();
    for count in [1, 10, 100, 1000, 10_000, 100_000] {
        let value = array(
            (0..count)
                .flat_map(|_| [0, 65535, 500].map(Value::Integer))
                .collect(),
        );
        measure(
            &format!("{count} full ranges"),
            &value,
            &all,
            count <= 1000,
            1,
            rounds,
        );
    }
    println!("\nEvery measured implementation returns the complete expected sorted vector.");
    println!(
        "Input construction and equality checks are outside timing; result allocation and drop are inside."
    );
    println!(
        "The base is deliberately not run above 1,000 full ranges; those cells are not estimates."
    );
}
