//! Times how long hayro-syntax's `TypedIter` takes over content streams
//! whose inline image is followed by many `EI`s, at doubling sizes. Linear
//! time doubles with the size; quadratic time quadruples.

use hayro_syntax::content::TypedIter;
use std::hint::black_box;
use std::time::Instant;

const RUNS: usize = 5;
/// A size is skipped once the previous one took longer than this.
const LIMIT_MS: f64 = 2_000.0;

const IMAGE: &[u8] = b"q BI /W 1 /H 1 /BPC 8 /CS /G ID ";

fn instructions(data: &[u8]) -> usize {
    let mut iter = TypedIter::new(data);
    let mut count = 0;
    while iter.next().is_some() {
        count += 1;
    }
    count
}

fn median_ms(data: &[u8]) -> (f64, usize) {
    let mut count = 0;
    let mut times: Vec<f64> = (0..RUNS)
        .map(|_| {
            let start = Instant::now();
            count = black_box(instructions(black_box(data)));
            start.elapsed().as_secs_f64() * 1e3
        })
        .collect();
    times.sort_by(f64::total_cmp);
    (times[RUNS / 2], count)
}

fn stream(prefix: &[u8], unit: &[u8], size: usize, suffix: &[u8]) -> Vec<u8> {
    let mut data = IMAGE.to_vec();
    data.extend_from_slice(prefix);
    while data.len() < size {
        data.extend_from_slice(unit);
    }
    data.extend_from_slice(suffix);
    data
}

fn main() {
    // (name, image data before the first EI, repeated unit, suffix)
    let cases: [(&str, &[u8], &[u8], &[u8]); 4] = [
        // The image ends at the first EI; text that shows "EI" follows.
        ("(EI) Tj after the image", b"x EI Q\n", b"(EI) Tj\n", b""),
        // Every EI is a candidate that a binary byte at the end rejects.
        ("EI before one binary byte", b"x ", b"EI ", b"\x80 EI Q"),
        // Each candidate opens a string that hides the next one.
        ("EI ( before one binary byte", b"x ", b"EI ( ", b"\x80 EI Q"),
        // Each candidate is rejected because no content follows it.
        ("EI ] (not content)", b"x ", b"EI ] ", b"EI Q"),
    ];

    for (name, prefix, unit, suffix) in cases {
        println!("{name}");
        println!("  {:>8}  {:>10}  {:>6}", "KiB", "ms", "ops");
        let mut size = 64 * 1024;
        while size <= 1024 * 1024 {
            let data = stream(prefix, unit, size, suffix);
            let (ms, count) = median_ms(&data);
            println!("  {:>8}  {:>10.3}  {:>6}", size / 1024, ms, count);
            if ms > LIMIT_MS {
                break;
            }
            size *= 2;
        }
    }
}
