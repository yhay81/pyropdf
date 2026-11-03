//! Opens documents with three versions of hayro-syntax in turn and prints the
//! median time of `Pdf::new`, and of `Pdf::new` followed by reading every
//! page's boxes, rotation, and resources: `before` builds every page with
//! them, `geometry` reads the boxes and the rotation on first use, and `after`
//! all three. Before timing a document, it checks that all versions read the
//! same pages.
//!
//! - `hayro-page-attributes DOCUMENT...` prints a row per document.
//! - `hayro-page-attributes --corpus DIRECTORY...` opens every PDF below the
//!   directories and prints the totals, and the five documents that took
//!   longest to open before.
//!
//! It prints Markdown.

use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

/// The timed runs per document, version, and workload, after a warmup.
const RUNS: usize = 101;
const WARMUP: usize = 5;
/// The same for each document of a corpus.
const CORPUS_RUNS: usize = 5;
const CORPUS_WARMUP: usize = 1;

/// What a page reads on first use since the change: its boxes, its rotation,
/// and the names in its resource dictionaries.
type Attributes = (Vec<f64>, String, Vec<Vec<Vec<u8>>>);

/// The time of `work`, without dropping what it returns.
fn timed<T>(work: impl FnOnce() -> T) -> Duration {
    let start = Instant::now();
    let result = work();
    let elapsed = start.elapsed();
    drop(std::hint::black_box(result));
    elapsed
}

macro_rules! version {
    ($name:ident) => {
        mod $name {
            use super::{Attributes, timed};
            use std::time::Duration;
            use $name::Pdf;
            use $name::object::Dict;

            fn open(data: Vec<u8>) -> Option<Pdf> {
                Pdf::new(data).ok()
            }

            pub fn time_open(data: Vec<u8>) -> Duration {
                timed(|| open(data).unwrap())
            }

            pub fn time_open_and_read(data: Vec<u8>) -> Duration {
                timed(|| {
                    let pdf = open(data).unwrap();
                    for page in pdf.pages().iter() {
                        std::hint::black_box((page.media_box(), page.crop_box(), page.rotation()));
                        std::hint::black_box(page.resources());
                    }
                    pdf
                })
            }

            pub fn objects(data: Vec<u8>) -> usize {
                open(data).unwrap().len()
            }

            /// Every page's attributes, or `None` if the document doesn't open.
            pub fn attributes(data: Vec<u8>) -> Option<Vec<Attributes>> {
                let keys = |dict: &Dict<'_>| {
                    let mut keys = dict.keys().map(|key| key.to_vec()).collect::<Vec<_>>();
                    keys.sort();
                    keys
                };
                let pdf = open(data)?;
                let pages = pdf
                    .pages()
                    .iter()
                    .map(|page| {
                        let (media, crop) = (page.media_box(), page.crop_box());
                        let boxes = vec![
                            media.x0, media.y0, media.x1, media.y1, crop.x0, crop.y0, crop.x1,
                            crop.y1,
                        ];
                        let resources = page.resources();
                        let names = vec![
                            keys(&resources.ext_g_states),
                            keys(&resources.fonts),
                            keys(&resources.properties),
                            keys(&resources.color_spaces),
                            keys(&resources.x_objects),
                            keys(&resources.patterns),
                            keys(&resources.shadings),
                        ];
                        (boxes, format!("{:?}", page.rotation()), names)
                    })
                    .collect();
                Some(pages)
            }
        }
    };
}

version!(before);
version!(geometry);
version!(after);

struct Version {
    time_open: fn(Vec<u8>) -> Duration,
    time_open_and_read: fn(Vec<u8>) -> Duration,
    objects: fn(Vec<u8>) -> usize,
    attributes: fn(Vec<u8>) -> Option<Vec<Attributes>>,
}

/// `before`, `geometry`, and `after`, in this order.
const VERSIONS: [Version; 3] = [
    Version {
        time_open: before::time_open,
        time_open_and_read: before::time_open_and_read,
        objects: before::objects,
        attributes: before::attributes,
    },
    Version {
        time_open: geometry::time_open,
        time_open_and_read: geometry::time_open_and_read,
        objects: geometry::objects,
        attributes: geometry::attributes,
    },
    Version {
        time_open: after::time_open,
        time_open_and_read: after::time_open_and_read,
        objects: after::objects,
        attributes: after::attributes,
    },
];

fn median_ms(mut times: Vec<Duration>) -> f64 {
    times.sort();
    times[times.len() / 2].as_secs_f64() * 1e3
}

/// The median times of `Pdf::new` and of `Pdf::new` followed by reading every
/// page's attributes, per version. The versions run in turn, and the one that
/// goes first changes with every run.
fn measure(bytes: &[u8], runs: usize, warmup: usize) -> [[f64; 3]; 2] {
    let mut times = [[(); 3], [(); 3]].map(|series| series.map(|()| Vec::with_capacity(runs)));
    for run in 0..warmup + runs {
        for (workload, series) in times.iter_mut().enumerate() {
            for turn in 0..VERSIONS.len() {
                let index = (run + turn) % VERSIONS.len();
                let version = &VERSIONS[index];
                let time = if workload == 0 {
                    (version.time_open)(bytes.to_vec())
                } else {
                    (version.time_open_and_read)(bytes.to_vec())
                };
                if run >= warmup {
                    series[index].push(time);
                }
            }
        }
    }
    times.map(|series| series.map(median_ms))
}

/// How a document compares between the versions.
enum Outcome {
    /// All read the same pages, of which there are this many.
    Same(usize),
    /// None opens it, or reading it panics in all.
    Unreadable,
    /// The versions disagree.
    Different,
}

fn compare(bytes: &[u8]) -> Outcome {
    let read = VERSIONS
        .iter()
        .map(|version| {
            catch_unwind(AssertUnwindSafe(|| (version.attributes)(bytes.to_vec())))
                .ok()
                .flatten()
        })
        .collect::<Vec<_>>();
    match read.as_slice() {
        [Some(first), rest @ ..] if rest.iter().all(|other| other.as_ref() == Some(first)) => {
            Outcome::Same(first.len())
        }
        all if all.iter().all(Option::is_none) => Outcome::Unreadable,
        _ => Outcome::Different,
    }
}

fn documents(paths: &[String]) {
    println!(
        "| Document | Pages | Objects | `Pdf::new`, before | geometry | after | Speedup | Then every page's attributes, before | geometry | after |"
    );
    println!("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |");
    for path in paths {
        let bytes = std::fs::read(path).unwrap();
        let Outcome::Same(pages) = compare(&bytes) else {
            panic!("{path}: the versions read different pages");
        };
        let objects = (VERSIONS[0].objects)(bytes.clone());
        assert!(
            VERSIONS
                .iter()
                .all(|version| (version.objects)(bytes.clone()) == objects)
        );
        let [
            [open_before, open_geometry, open_after],
            [all_before, all_geometry, all_after],
        ] = measure(&bytes, RUNS, WARMUP);
        let name = Path::new(path)
            .file_name()
            .unwrap()
            .to_string_lossy()
            .into_owned();
        println!(
            "| {name} | {pages} | {objects} | {open_before:.3} ms | {open_geometry:.3} ms | {open_after:.3} ms | {:.2}x | {all_before:.3} ms | {all_geometry:.3} ms | {all_after:.3} ms |",
            open_before / open_after
        );
    }
}

fn corpus(directories: &[String]) {
    let mut files = vec![];
    let mut pending: Vec<PathBuf> = directories.iter().map(PathBuf::from).collect();
    while let Some(directory) = pending.pop() {
        for entry in std::fs::read_dir(&directory).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                pending.push(path);
            } else if path
                .extension()
                .is_some_and(|extension| extension.eq_ignore_ascii_case("pdf"))
            {
                files.push(path);
            }
        }
    }
    files.sort();

    // Panics of hayro on unreadable documents are expected; keep them off the output.
    std::panic::set_hook(Box::new(|_| {}));
    let (mut same, mut unreadable, mut different, mut pages) = (0, 0, vec![], 0);
    let mut totals = [[0.0; 3]; 2];
    let mut slowest = vec![];
    for path in &files {
        let bytes = std::fs::read(path).unwrap();
        match compare(&bytes) {
            Outcome::Same(count) => {
                let Ok(times) = catch_unwind(|| measure(&bytes, CORPUS_RUNS, CORPUS_WARMUP)) else {
                    different.push(path);
                    continue;
                };
                same += 1;
                pages += count;
                for (total, time) in totals.iter_mut().flatten().zip(times.iter().flatten()) {
                    *total += time;
                }
                slowest.push((times[0], count, path));
            }
            Outcome::Unreadable => unreadable += 1,
            Outcome::Different => different.push(path),
        }
    }
    let _ = std::panic::take_hook();

    let [
        [open_before, open_geometry, open_after],
        [all_before, all_geometry, all_after],
    ] = totals;
    println!(
        "| PDFs | Read the same | Pages | Unreadable by all | Different | `Pdf::new` total, before | geometry | after | Speedup | Then every page's attributes, before | geometry | after |"
    );
    println!(
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    );
    println!(
        "| {} | {same} | {pages} | {unreadable} | {} | {open_before:.1} ms | {open_geometry:.1} ms | {open_after:.1} ms | {:.2}x | {all_before:.1} ms | {all_geometry:.1} ms | {all_after:.1} ms |",
        files.len(),
        different.len(),
        open_before / open_after
    );
    for path in &different {
        println!("\nDifferent: {}", path.display());
    }

    slowest.sort_by(|(a, ..), (b, ..)| b[0].total_cmp(&a[0]));
    println!();
    println!(
        "| Slowest to open before | Pages | `Pdf::new`, before | geometry | after | Speedup |"
    );
    println!("| --- | ---: | ---: | ---: | ---: | ---: |");
    for ([open_before, open_geometry, open_after], count, path) in slowest.iter().take(5) {
        let name = path.file_name().unwrap().to_string_lossy();
        println!(
            "| {name} | {count} | {open_before:.3} ms | {open_geometry:.3} ms | {open_after:.3} ms | {:.2}x |",
            open_before / open_after
        );
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.split_first() {
        Some((flag, directories)) if flag == "--corpus" => corpus(directories),
        _ => documents(&args),
    }
}
