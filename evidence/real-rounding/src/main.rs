//! Times hayro-syntax's number parsing and counts the reals that don't read
//! back as the `f64` they were written from.

use hayro_syntax::content::UntypedIter;
use hayro_syntax::object::{FromBytes, Number};
use std::hint::black_box;
use std::time::Instant;

const COUNT: usize = 1_000_000;
const RUNS: usize = 15;

struct XorShift(u64);

impl XorShift {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }

    fn unit(&mut self) -> f64 {
        (self.next() >> 11) as f64 / (1u64 << 53) as f64
    }
}

fn median_ns_per_number(mut run: impl FnMut()) -> f64 {
    let mut times: Vec<f64> = (0..RUNS)
        .map(|_| {
            let start = Instant::now();
            run();
            start.elapsed().as_nanos() as f64 / COUNT as f64
        })
        .collect();
    times.sort_by(f64::total_cmp);
    times[RUNS / 2]
}

fn bench(name: &str, texts: &[String], values: Option<&[f64]>) {
    let numbers = median_ns_per_number(|| {
        for text in texts {
            black_box(Number::from_bytes(black_box(text.as_bytes())));
        }
    });

    // The same numbers as operands in a content stream, two per `Td`.
    let mut content = String::new();
    for pair in texts.chunks(2) {
        for text in pair {
            content.push_str(text);
            content.push(' ');
        }
        content.push_str("Td\n");
    }
    let stream = median_ns_per_number(|| {
        let mut ops = UntypedIter::new(black_box(content.as_bytes()));
        while let Some(op) = ops.next() {
            black_box(op.operands().count());
        }
    });

    let wrong = values.map_or(String::from("-"), |values| {
        let wrong = texts
            .iter()
            .zip(values)
            .filter(|(text, value)| {
                Number::from_bytes(text.as_bytes()).unwrap().as_f64().to_bits() != value.to_bits()
            })
            .count();
        format!("{:.2}%", wrong as f64 * 100.0 / COUNT as f64)
    });

    println!("{name:<28} {numbers:>8.2} {stream:>8.2} {wrong:>9}");
}

fn main() {
    let mut rng = XorShift(0x9e37_79b9_7f4a_7c15);

    // What PDF writers usually emit: coordinates with a few decimals.
    let short: Vec<String> = (0..COUNT)
        .map(|_| format!("{:.3}", rng.unit() * 2000.0 - 1000.0))
        .collect();
    let integers: Vec<String> = (0..COUNT)
        .map(|_| (rng.next() % 2000).to_string())
        .collect();
    // The shortest decimal that round-trips each `f64`, as pyropdf writes it.
    let uniform: Vec<f64> = (0..COUNT).map(|_| rng.unit() * 2e6 - 1e6).collect();
    let bits: Vec<f64> = std::iter::repeat_with(|| f64::from_bits(rng.next()))
        .filter(|value| value.is_finite())
        .take(COUNT)
        .collect();
    let shortest = |values: &[f64]| values.iter().map(|v| format!("{v}")).collect::<Vec<_>>();

    println!("{:<28} {:>8} {:>8} {:>9}", "input", "ns/num", "ns/op'd", "misread");
    bench("integers below 2000", &integers, None);
    bench("{:.3} in +-1000", &short, None);
    bench("shortest, uniform in +-1e6", &shortest(&uniform), Some(&uniform));
    bench("shortest, random bits", &shortest(&bits), Some(&bits));
}
