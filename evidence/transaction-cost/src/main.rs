//! Measures what a transaction that writes one object costs in a document of
//! many objects (issue #25, DESIGN.md section 4.1), and prints Markdown
//! tables. `run.sh` runs it with the machine's description. An argument
//! writes every number to that CSV file as well.

mod alloc;
mod rng;

use std::collections::BTreeSet;
use std::fmt::Write as _;
use std::sync::Arc;
use std::time::Instant;

use pyropdf_store::budget::Budget;
use pyropdf_store::value::Value;
use pyropdf_store::version::{Document, MemoryBase};

use alloc::Counting;
use rng::Rng;

#[global_allocator]
static ALLOCATOR: Counting = Counting;

/// Objects in a document.
const SIZES: [usize; 3] = [10_000, 100_000, 1_000_000];

/// Transactions whose memory is measured, per document.
const MEMORY_ROUNDS: usize = 101;

/// Batches of transactions that are timed, per document.
const TIME_ROUNDS: usize = 1_001;

/// Transactions per timed batch, so that the clock's resolution does not
/// matter.
const BATCH: usize = 64;

/// Passes over the whole suite. The tables show each time's lowest pass.
const PASSES: usize = 3;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Distribution {
    /// Object numbers 1..=N, as most files number their objects.
    Dense,
    /// N numbers drawn uniformly from the 32-bit range: hostile input, which
    /// leaves one object in each chunk of the table and needs every level of
    /// branches.
    Scattered,
}

impl Distribution {
    const fn name(self) -> &'static str {
        match self {
            Self::Dense => "dense",
            Self::Scattered => "scattered",
        }
    }
}

/// Returns the object numbers of a document of `n` objects, ascending.
fn numbers(distribution: Distribution, n: usize) -> Vec<u32> {
    match distribution {
        Distribution::Dense => (1..=n as u32).collect(),
        Distribution::Scattered => {
            let mut rng = Rng::new(0x2500 + n as u64);
            let mut numbers = BTreeSet::new();
            while numbers.len() < n {
                numbers.insert(1 + rng.below(u64::from(u32::MAX)) as u32);
            }
            numbers.into_iter().collect()
        }
    }
}

/// Returns the levels of branches of a table that holds `number`: chunks of
/// 16 slots under 32-way branches, as crates/pyropdf-store/src/version/
/// table.rs builds them.
fn height(number: u32) -> u32 {
    let mut height = 1;
    while u64::from(number) >= 1 << (4 + 5 * height) {
        height += 1;
    }
    height
}

/// What one document measured.
struct Case {
    distribution: Distribution,
    objects: usize,
    height: u32,
    /// The bytes that the version before a transaction keeps alive in the
    /// history: what it holds and the transaction's version does not share.
    kept: u64,
    /// The bytes that a transaction allocates, less those that it frees,
    /// while the version before it stays alive.
    bytes: isize,
    /// The allocations that a transaction makes, less the blocks it frees.
    blocks: isize,
    /// The median time of a transaction, and its 90th percentile, over the
    /// timed batches.
    time: (f64, f64),
}

/// Measures transactions that each rewrite one object, drawn at random, in
/// a document of `n` objects that a first transaction wrote.
fn measure(distribution: Distribution, n: usize, pass: usize) -> Case {
    let numbers = numbers(distribution, n);
    let budget = Budget::default();
    let document = Document::new(Arc::new(MemoryBase::default()), None);
    let mut transaction = document.begin();
    for &number in &numbers {
        transaction
            .write(number, &Value::Integer(1), &budget)
            .unwrap();
    }
    transaction.commit(&budget).unwrap();
    let mut rng = Rng::new(0x5eed + n as u64 + pass as u64);
    let value = Value::Integer(2);
    let write = |number| {
        let mut transaction = document.begin();
        transaction.write(number, &value, &budget).unwrap();
        transaction.commit(&budget).unwrap();
    };

    // Memory, while the history keeps every version, so that the version
    // before each transaction stays alive.
    let (mut kept, mut bytes, mut blocks) = (Vec::new(), Vec::new(), Vec::new());
    for _ in 0..MEMORY_ROUNDS {
        let number = numbers[rng.below(n as u64) as usize];
        let before = document.history().size;
        let ((), usage) = alloc::measure(|| write(number));
        kept.push(document.history().size - before);
        bytes.push(usage.bytes);
        blocks.push(usage.blocks);
    }

    // Time, without a history, so that each commit frees the version before
    // it, as it does when nothing else holds that version.
    document.set_history_limit(Some(0));
    let mut times = Vec::with_capacity(TIME_ROUNDS);
    for _ in 0..TIME_ROUNDS {
        let batch: Vec<u32> = (0..BATCH)
            .map(|_| numbers[rng.below(n as u64) as usize])
            .collect();
        let start = Instant::now();
        for &number in &batch {
            write(number);
        }
        times.push(start.elapsed().as_nanos() as f64 / BATCH as f64);
    }

    // Every object reads as written, by the first transaction or a later one.
    let version = document.current();
    for &number in numbers.iter().step_by(97) {
        let object = version.get(number, &budget).unwrap().unwrap();
        assert!(matches!(**object.value(), Value::Integer(1 | 2)));
    }
    Case {
        distribution,
        objects: n,
        height: height(numbers[n - 1]),
        kept: median(kept),
        bytes: median(bytes),
        blocks: median(blocks),
        time: (percentile(&mut times, 50), percentile(&mut times, 90)),
    }
}

fn median<T: Ord + Copy>(mut samples: Vec<T>) -> T {
    samples.sort_unstable();
    samples[samples.len() / 2]
}

fn percentile(samples: &mut [f64], percent: usize) -> f64 {
    samples.sort_by(f64::total_cmp);
    samples[(samples.len() - 1) * percent / 100]
}

/// Asks macOS to run this thread on a performance core.
#[cfg(target_os = "macos")]
fn prefer_performance_cores() {
    unsafe extern "C" {
        fn pthread_set_qos_class_self_np(class: u32, relative_priority: i32) -> i32;
    }
    const QOS_CLASS_USER_INTERACTIVE: u32 = 0x21;
    // SAFETY: the call takes two integers and changes this thread's class.
    let status = unsafe { pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0) };
    assert_eq!(status, 0, "the thread's QoS class is settable");
}

#[cfg(not(target_os = "macos"))]
fn prefer_performance_cores() {}

fn main() {
    prefer_performance_cores();
    let csv = std::env::args().nth(1);
    let started = Instant::now();
    let mut passes: Vec<Vec<Case>> = Vec::new();
    // Whole passes, so that a burst of other work on the machine rarely
    // slows the same measurement in both.
    for pass in 0..PASSES {
        let mut cases = Vec::new();
        for distribution in [Distribution::Dense, Distribution::Scattered] {
            for n in SIZES {
                cases.push(measure(distribution, n, pass));
                eprintln!(
                    "pass {} {} {n}: {:.0} s",
                    pass + 1,
                    distribution.name(),
                    started.elapsed().as_secs_f64()
                );
            }
        }
        passes.push(cases);
    }
    print!("{}", report(&passes));
    if let Some(path) = csv {
        std::fs::write(path, csv_rows(&passes)).expect("the CSV file is writable");
    }
}

/// A duration in the largest unit that keeps it at 1 or more once rounded.
fn time(ns: f64) -> String {
    match ns {
        ns if ns < 999.5 => format!("{ns:.0} ns"),
        ns if ns < 999_950.0 => format!("{:.2} µs", ns / 1e3),
        ns => format!("{:.1} ms", ns / 1e6),
    }
}

fn thousands(n: usize) -> String {
    let digits = n.to_string();
    let mut out = String::new();
    for (i, digit) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(digit);
    }
    out
}

fn report(passes: &[Vec<Case>]) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "## One write per transaction\n");
    let _ = writeln!(
        out,
        "Each transaction rewrites one object drawn at random from a document whose objects a first transaction wrote. Memory is the median of {MEMORY_ROUNDS} transactions while the history keeps the version before each; time is the median and the 90th percentile of {} batches of {BATCH} transactions without a history, the lower of {PASSES} passes.\n",
        thousands(TIME_ROUNDS)
    );
    let _ = writeln!(
        out,
        "| Numbers | Objects | Height | Chunks and branches copied | Kept alive | Allocated | Allocations | Time, median | Time, 90th percentile |"
    );
    let _ = writeln!(
        out,
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    );
    for (index, case) in passes[0].iter().enumerate() {
        let lowest = |pick: fn(&Case) -> f64| {
            passes
                .iter()
                .map(|cases| pick(&cases[index]))
                .fold(f64::INFINITY, f64::min)
        };
        let _ = writeln!(
            out,
            "| {} | {} | {} | {} | {} B | {} B | {} | {} | {} |",
            case.distribution.name(),
            thousands(case.objects),
            case.height,
            case.height + 1,
            thousands(case.kept as usize),
            thousands(case.bytes.unsigned_abs()),
            case.blocks,
            time(lowest(|case| case.time.0)),
            time(lowest(|case| case.time.1)),
        );
    }
    let _ = writeln!(out);
    out
}

fn csv_rows(passes: &[Vec<Case>]) -> String {
    let mut out = String::from("pass,distribution,objects,metric,value\n");
    for (pass, cases) in passes.iter().enumerate() {
        for case in cases {
            for (metric, value) in [
                ("height", f64::from(case.height)),
                ("kept_bytes", case.kept as f64),
                ("allocated_bytes", case.bytes as f64),
                ("allocations", case.blocks as f64),
                ("median_ns", case.time.0),
                ("p90_ns", case.time.1),
            ] {
                let _ = writeln!(
                    out,
                    "{},{},{},{metric},{value}",
                    pass + 1,
                    case.distribution.name(),
                    case.objects
                );
            }
        }
    }
    out
}
