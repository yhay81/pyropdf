//! Times pyropdf-engine's font subsetter (`fonts::subset`) and compactor
//! (`fonts::compact::compact`) on each face that the arguments name, built
//! against the checkout that this crate sits in. The README describes the
//! workload. It prints one tab-separated line per face and operation: the
//! face, the operation, its glyphs, the glyphs kept, the bytes of the face
//! and of the result, and the median, minimum, and count of the timed runs
//! in microseconds, then the result's SHA-256 (or `none`).

use std::hint::black_box;
use std::time::{Duration, Instant};

use pyropdf_engine::fonts::{self, Format, compact};

/// Each operation runs at least this often, and for at least this long.
const RUNS: usize = 15;
const TIME: Duration = Duration::from_millis(500);

/// The glyph count of the sfnt `data`, from its `maxp` table.
fn glyphs(data: &[u8]) -> Option<u32> {
    let be16 = |at: usize| Some(u16::from_be_bytes(data.get(at..at + 2)?.try_into().ok()?));
    let be32 = |at: usize| Some(u32::from_be_bytes(data.get(at..at + 4)?.try_into().ok()?));
    for record in 0..usize::from(be16(4)?) {
        let at = 12 + 16 * record;
        if data.get(at..at + 4)? == b"maxp" {
            return be16(usize::try_from(be32(at + 8)?).ok()? + 4).map(u32::from);
        }
    }
    None
}

/// Runs `operation` until it has run [`RUNS`] times and for [`TIME`], and
/// returns the median and the minimum of the runs, and their count.
fn time(mut operation: impl FnMut()) -> (Duration, Duration, usize) {
    operation();
    let mut runs = Vec::new();
    let start = Instant::now();
    while runs.len() < RUNS || start.elapsed() < TIME {
        let run = Instant::now();
        operation();
        runs.push(run.elapsed());
    }
    runs.sort();
    (runs[runs.len() / 2], runs[0], runs.len())
}

/// Identify the result outside the timed loop, including absence.
fn digest(data: Option<&[u8]>) -> String {
    data.map_or_else(
        || "none".to_owned(),
        |data| {
            pyropdf_engine::sign::digest_named("sha256", &[data])
                .unwrap()
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect()
        },
    )
}

fn main() {
    for path in std::env::args().skip(1) {
        let data = std::fs::read(&path).unwrap_or_else(|error| panic!("{path}: {error}"));
        let face = std::path::Path::new(&path)
            .file_name()
            .map_or(path.clone(), |name| name.to_string_lossy().into_owned());
        let count = glyphs(&data).unwrap_or_else(|| panic!("{path}: no maxp"));
        // The first 256 glyphs, as Latin text draws, and every 16th one after.
        let keep: Vec<u32> = (0..count.min(256))
            .chain((256..count).step_by(16))
            .collect();
        let subset = fonts::subset(&data, Format::Sfnt, &keep);
        let (median, min, runs) = time(|| {
            black_box(fonts::subset(black_box(&data), Format::Sfnt, black_box(&keep)));
        });
        let hash = digest(subset.as_ref().map(|subset| subset.data.as_slice()));
        let out = subset.map_or(0, |subset| subset.data.len());
        println!(
            "{face}\tsubset\t{count}\t{}\t{}\t{out}\t{}\t{}\t{runs}\t{hash}",
            keep.len(),
            data.len(),
            median.as_micros(),
            min.as_micros()
        );
        let compacted = compact::compact(&data, &keep, "Face");
        let (median, min, runs) = time(|| {
            black_box(compact::compact(black_box(&data), black_box(&keep), "Face"));
        });
        let hash = digest(compacted.as_ref().map(|compacted| compacted.data.as_slice()));
        let out = compacted.map_or(0, |compacted| compacted.data.len());
        println!(
            "{face}\tcompact\t{count}\t{}\t{}\t{out}\t{}\t{}\t{runs}\t{hash}",
            keep.len(),
            data.len(),
            median.as_micros(),
            min.as_micros()
        );
    }
}
