//! Measures the candidates of issue #9 and prints Markdown tables. `run.sh`
//! runs it with the machine's description and the code-size probes. An
//! argument writes every number to that CSV file as well.

use std::collections::{BTreeSet, HashSet};
use std::fmt::Write as _;
use std::hint::black_box;
use std::time::Instant;

use persistent_map_bench::alloc::{self, Counting};
use persistent_map_bench::chunks::ChunkTable;
use persistent_map_bench::flat::FlatChunks;
use persistent_map_bench::maps::{ImHash, ImOrd, ImblHash, ImblOrd, Map, RpdsHash, RpdsTree};
use persistent_map_bench::rng::Rng;
use persistent_map_bench::slot::Slot;

#[global_allocator]
static ALLOCATOR: Counting = Counting;

/// Objects in a map.
const SIZES: [usize; 3] = [10_000, 100_000, 1_000_000];
/// Objects that one transaction writes.
const WRITES: [usize; 3] = [1, 100, 10_000];
/// Lookups per sample, whatever the size of the map.
const LOOKUPS: usize = 1 << 20;
/// The sparse distribution draws N object numbers from 1..=SPREAD*N.
const SPREAD: usize = 8;

/// The chunk table that the main tables show: among the variants that wrote
/// at the lowest cost in time and memory, at every size, in both trial
/// runs.
type Chunks = ChunkTable<Slot, 16, 32>;

/// Passes over the whole suite. The tables show each number's lowest pass.
const PASSES: usize = 2;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Distribution {
    /// Object numbers 1..=N, as most files number their objects.
    Dense,
    /// N numbers drawn uniformly from 1..=8N, as after incremental updates
    /// that freed objects, or a repair that found scattered numbers. At a
    /// million objects the largest number nears 8,388,607, the limit of
    /// PDF 1.7's Annex C.
    Sparse,
    /// N numbers drawn uniformly from the 32-bit range: hostile input, which
    /// leaves one object in each chunk of a chunk table.
    Scattered,
}

impl Distribution {
    const fn name(self) -> &'static str {
        match self {
            Self::Dense => "dense",
            Self::Sparse => "sparse",
            Self::Scattered => "scattered",
        }
    }
}

/// The numbers that the scattered distribution draws from, below the last
/// ten thousand plus margin that appends need.
const SCATTERED_RANGE: u64 = u32::MAX as u64 - (1 << 16);

#[derive(Clone, Copy, PartialEq, Eq)]
enum Pattern {
    /// Rewrite objects drawn uniformly from the map: edits.
    Update,
    /// Write new objects at the next free numbers: creation.
    Append,
}

impl Pattern {
    const fn name(self) -> &'static str {
        match self {
            Self::Update => "update",
            Self::Append => "append",
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Group {
    /// The candidates that issue #9 names, and imbl beside im.
    Candidates,
    /// Chunk and fan-out sizes of the chunk table, and its flat variant.
    Chunks,
}

/// The object numbers of one map and the objects they map to.
struct Objects {
    distribution: Distribution,
    /// Sorted.
    keys: Vec<u32>,
    values: Vec<Slot>,
    /// Numbers in the key range that no object has, for misses.
    absent: Vec<u32>,
}

impl Objects {
    fn new(distribution: Distribution, n: usize) -> Self {
        let mut rng = Rng::new(0x9000 + n as u64);
        let keys: Vec<u32> = match distribution {
            Distribution::Dense => (1..=n as u32).collect(),
            Distribution::Sparse => {
                // Selection sampling: exactly n numbers, in order.
                let range = SPREAD * n;
                let mut keys = Vec::with_capacity(n);
                for candidate in 1..=range {
                    let (needed, left) = (n - keys.len(), range - candidate + 1);
                    if (rng.below(left as u64) as usize) < needed {
                        keys.push(candidate as u32);
                    }
                }
                keys
            }
            Distribution::Scattered => {
                let mut keys = BTreeSet::new();
                while keys.len() < n {
                    keys.insert(1 + rng.below(SCATTERED_RANGE) as u32);
                }
                keys.into_iter().collect()
            }
        };
        let absent = match distribution {
            Distribution::Dense => Vec::new(),
            Distribution::Sparse | Distribution::Scattered => {
                let range = match distribution {
                    Distribution::Sparse => (SPREAD * n) as u64,
                    _ => SCATTERED_RANGE,
                };
                let mut absent = Vec::with_capacity(LOOKUPS);
                while absent.len() < LOOKUPS {
                    let key = 1 + rng.below(range) as u32;
                    if keys.binary_search(&key).is_err() {
                        absent.push(key);
                    }
                }
                absent
            }
        };
        // Objects are allocated in object-number order, as when a document
        // is written from its first object to its last.
        let values = keys.iter().map(|_| Slot::written(0)).collect();
        Self {
            distribution,
            keys,
            values,
            absent,
        }
    }

    fn next_free(&self) -> u32 {
        self.keys.last().map_or(1, |last| last + 1)
    }
}

/// One measured number.
struct Row {
    pass: usize,
    group: Group,
    map: String,
    distribution: Distribution,
    objects: usize,
    /// The write pattern and the writes of a transaction, for transactions.
    transaction: Option<(Pattern, usize)>,
    metric: &'static str,
    value: f64,
}

struct Results {
    rows: Vec<Row>,
    /// The pass that `run` records.
    pass: usize,
}

impl Results {
    /// The lowest value of all passes. Interference from other work only
    /// adds time, and memory is the same in every pass.
    fn find(
        &self,
        map: &str,
        distribution: Distribution,
        objects: usize,
        transaction: Option<(Pattern, usize)>,
        metric: &str,
    ) -> Option<f64> {
        self.rows
            .iter()
            .filter(|row| {
                row.map == map
                    && row.distribution == distribution
                    && row.objects == objects
                    && row.transaction == transaction
                    && row.metric == metric
            })
            .map(|row| row.value)
            .reduce(f64::min)
    }
}

fn median(mut samples: Vec<f64>) -> f64 {
    samples.sort_by(f64::total_cmp);
    samples[samples.len() / 2]
}

fn elapsed_ns(start: Instant) -> f64 {
    start.elapsed().as_nanos() as f64
}

/// Measures one map on one set of objects.
fn run<M: Map>(group: Group, objects: &Objects, results: &mut Results) {
    let name = M::name();
    let n = objects.keys.len();
    let mut rng = Rng::new(0x5eed + n as u64);
    let mut push = |transaction, metric, value| {
        results.rows.push(Row {
            pass: results.pass,
            group,
            map: name.clone(),
            distribution: objects.distribution,
            objects: n,
            transaction,
            metric,
            value,
        });
    };

    // Insert: build the map in place, one write after another, in random
    // object-number order.
    let mut order: Vec<usize> = (0..n).collect();
    rng.shuffle(&mut order);
    let rounds = (2_000_000 / n).clamp(3, 50);
    let mut samples = Vec::with_capacity(rounds);
    let mut map = M::new();
    for _ in 0..rounds {
        let mut entries: Vec<(u32, Slot)> = order
            .iter()
            .map(|&i| (objects.keys[i], objects.values[i].clone()))
            .collect();
        drop(map);
        let start = Instant::now();
        map = M::new();
        for (key, value) in entries.drain(..) {
            map.insert(key, value);
        }
        samples.push(elapsed_ns(start) / n as f64);
    }
    push(None, "insert_ns", median(samples));
    verify(&map, objects);

    // Lookups of present and absent object numbers.
    let present: Vec<u32> = (0..LOOKUPS)
        .map(|_| objects.keys[rng.below(n as u64) as usize])
        .collect();
    push(None, "hit_ns", time_lookups(&map, &present, true));
    if !objects.absent.is_empty() {
        push(None, "miss_ns", time_lookups(&map, &objects.absent, false));
    }

    // Every object in object-number order.
    let rounds = (2_000_000 / n).clamp(5, 100);
    let mut samples = Vec::with_capacity(rounds);
    for _ in 0..rounds {
        let start = Instant::now();
        let mut sum = 0u64;
        map.for_each_ordered(|key, value| sum = sum.wrapping_add(u64::from(key) ^ value.tag()));
        samples.push(elapsed_ns(start) / n as f64);
        black_box(sum);
    }
    push(None, "ordered_ns", median(samples));

    // Transactions on the map as the base version.
    for pattern in [Pattern::Update, Pattern::Append] {
        for k in WRITES {
            let transaction = Some((pattern, k));
            let [commit, drop_ns, bytes, blocks] =
                time_transactions(&map, objects, pattern, k, &mut rng);
            push(transaction, "commit_ns", commit);
            push(transaction, "drop_ns", drop_ns);
            push(transaction, "bytes", bytes);
            push(transaction, "blocks", blocks);
        }
    }

    // The memory of the whole version: what dropping it frees, since the
    // objects stay alive in `objects`.
    let start = Instant::now();
    let ((), usage) = alloc::measure(|| drop(map));
    push(None, "drop_all_ns", elapsed_ns(start));
    push(None, "bytes_per_object", -usage.bytes as f64 / n as f64);
    push(None, "blocks_per_object", -usage.blocks as f64 / n as f64);
}

/// Checks the map against the objects before anything is timed on it.
fn verify<M: Map>(map: &M, objects: &Objects) {
    assert_eq!(map.len(), objects.keys.len(), "{}", M::name());
    let mut i = 0;
    map.for_each_ordered(|key, value| {
        assert_eq!(key, objects.keys[i], "{}", M::name());
        assert_eq!(value.tag(), objects.values[i].tag(), "{}", M::name());
        i += 1;
    });
    assert_eq!(i, objects.keys.len(), "{}", M::name());
}

fn time_lookups<M: Map>(map: &M, keys: &[u32], present: bool) -> f64 {
    let mut samples = Vec::new();
    for _ in 0..3 {
        let start = Instant::now();
        let (mut sum, mut found) = (0u64, 0usize);
        for &key in keys {
            if let Some(value) = map.get(key) {
                sum = sum.wrapping_add(value.tag());
                found += 1;
            }
        }
        samples.push(elapsed_ns(start) / keys.len() as f64);
        black_box(sum);
        assert_eq!(found, if present { keys.len() } else { 0 }, "{}", M::name());
    }
    median(samples)
}

/// The writes of one transaction: k objects, each newly allocated.
fn writes(objects: &Objects, pattern: Pattern, k: usize, rng: &mut Rng) -> Vec<(u32, Slot)> {
    let keys: Vec<u32> = match pattern {
        Pattern::Update => {
            // Floyd's sampling of k distinct objects, then a seeded order.
            let n = objects.keys.len();
            let mut chosen = HashSet::with_capacity(k);
            for j in n - k..n {
                let t = rng.below(j as u64 + 1) as usize;
                if !chosen.insert(t) {
                    chosen.insert(j);
                }
            }
            let mut chosen: Vec<usize> = chosen.into_iter().collect();
            chosen.sort_unstable();
            rng.shuffle(&mut chosen);
            chosen.into_iter().map(|i| objects.keys[i]).collect()
        }
        Pattern::Append => (objects.next_free()..).take(k).collect(),
    };
    keys.into_iter()
        .map(|key| (key, Slot::written(1)))
        .collect()
}

/// A transaction: the next version is the base with the writes applied.
/// The caller allocates the writes, which take no part in the measurement.
fn transaction<M: Map>(base: &M, writes: &mut Vec<(u32, Slot)>) -> M {
    let mut next = base.clone();
    for (key, value) in writes.drain(..) {
        next.insert(key, value);
    }
    next
}

/// The median time to commit and to drop a transaction's version, and the
/// bytes and blocks that the version adds while the base stays alive.
fn time_transactions<M: Map>(
    base: &M,
    objects: &Objects,
    pattern: Pattern,
    k: usize,
    rng: &mut Rng,
) -> [f64; 4] {
    // Each transaction starts from the same base. Short ones run in
    // batches, so that the clock's resolution does not matter.
    let (batch, rounds) = match k {
        1 => (256, 31),
        100 => (16, 31),
        _ => (1, 11),
    };
    check_transaction(base, objects, pattern, k, rng);
    let (mut commit, mut dropped) = (Vec::new(), Vec::new());
    for _ in 0..rounds {
        let mut batches: Vec<_> = (0..batch)
            .map(|_| writes(objects, pattern, k, rng))
            .collect();
        let start = Instant::now();
        let versions: Vec<M> = batches
            .iter_mut()
            .map(|writes| transaction(base, writes))
            .collect();
        commit.push(elapsed_ns(start) / batch as f64);
        black_box(&versions);
        let start = Instant::now();
        drop(versions);
        dropped.push(elapsed_ns(start) / batch as f64);
    }
    let rounds = if k == 10_000 { 3 } else { 16 };
    let (mut bytes, mut blocks) = (0, 0);
    for _ in 0..rounds {
        let mut writes = writes(objects, pattern, k, rng);
        let (version, usage) = alloc::measure(|| transaction(base, &mut writes));
        bytes += usage.bytes;
        blocks += usage.blocks;
        drop(version);
    }
    let rounds = rounds as f64;
    [
        median(commit),
        median(dropped),
        bytes as f64 / rounds,
        blocks as f64 / rounds,
    ]
}

/// Checks that a transaction writes what it should and leaves its base
/// unchanged.
fn check_transaction<M: Map>(
    base: &M,
    objects: &Objects,
    pattern: Pattern,
    k: usize,
    rng: &mut Rng,
) {
    let mut writes = writes(objects, pattern, k, rng);
    let expected: Vec<(u32, u64)> = writes
        .iter()
        .map(|(key, value)| (*key, value.tag()))
        .collect();
    let next = transaction(base, &mut writes);
    let added = if pattern == Pattern::Append { k } else { 0 };
    assert_eq!(next.len(), base.len() + added, "{}", M::name());
    for (key, tag) in expected {
        assert_eq!(next.get(key).map(Slot::tag), Some(tag), "{}", M::name());
        let before = objects
            .keys
            .binary_search(&key)
            .ok()
            .map(|i| objects.values[i].tag());
        assert_eq!(base.get(key).map(Slot::tag), before, "{}", M::name());
    }
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

fn candidates(objects: &Objects, results: &mut Results) {
    run::<RpdsHash>(Group::Candidates, objects, results);
    run::<RpdsTree>(Group::Candidates, objects, results);
    run::<ImHash>(Group::Candidates, objects, results);
    run::<ImOrd>(Group::Candidates, objects, results);
    run::<ImblHash>(Group::Candidates, objects, results);
    run::<ImblOrd>(Group::Candidates, objects, results);
    run::<Chunks>(Group::Candidates, objects, results);
}

/// The chunk table at other chunk and fan-out sizes than `Chunks`, and
/// under a flat directory.
fn variants(objects: &Objects, results: &mut Results) {
    run::<ChunkTable<Slot, 16, 16>>(Group::Chunks, objects, results);
    run::<ChunkTable<Slot, 16, 64>>(Group::Chunks, objects, results);
    run::<ChunkTable<Slot, 32, 32>>(Group::Chunks, objects, results);
    run::<ChunkTable<Slot, 32, 64>>(Group::Chunks, objects, results);
    run::<ChunkTable<Slot, 64, 16>>(Group::Chunks, objects, results);
    run::<ChunkTable<Slot, 64, 64>>(Group::Chunks, objects, results);
    run::<ChunkTable<Slot, 128, 64>>(Group::Chunks, objects, results);
    run::<ChunkTable<Slot, 256, 64>>(Group::Chunks, objects, results);
    run::<ChunkTable<Slot, 64, 256>>(Group::Chunks, objects, results);
    run::<FlatChunks<Slot, 64>>(Group::Chunks, objects, results);
}

fn main() {
    prefer_performance_cores();
    let csv = std::env::args().nth(1);
    let mut results = Results {
        rows: Vec::new(),
        pass: 0,
    };
    let started = Instant::now();
    let runs: [(Distribution, &[usize]); 3] = [
        (Distribution::Dense, &SIZES),
        (Distribution::Sparse, &SIZES),
        // Hostile numbers measure what they cost, at one size, and not the
        // variants: the flat directory would need 2^26 pointers.
        (Distribution::Scattered, &[100_000]),
    ];
    // Whole passes, a minute or more apart, so that a burst of other work
    // on the machine rarely slows the same measurement in both.
    for pass in 0..PASSES {
        results.pass = pass;
        for (distribution, sizes) in runs {
            for &n in sizes {
                let objects = Objects::new(distribution, n);
                candidates(&objects, &mut results);
                if distribution != Distribution::Scattered {
                    variants(&objects, &mut results);
                }
                eprintln!(
                    "pass {} {} {n}: {:.0} s",
                    pass + 1,
                    distribution.name(),
                    started.elapsed().as_secs_f64()
                );
            }
        }
    }
    print!("{}", report(&results));
    if let Some(path) = csv {
        std::fs::write(path, csv_rows(&results)).expect("the CSV file is writable");
    }
}

/// A duration in the largest unit that keeps it at 1 or more once rounded.
fn time(ns: f64) -> String {
    match ns {
        ns if ns < 999.5 => format!("{ns:.0} ns"),
        ns if ns < 999_950.0 => format!("{:.1} µs", ns / 1e3),
        ns => format!("{:.1} ms", ns / 1e6),
    }
}

/// A size in the largest unit that keeps it at 1 or more once rounded.
fn size(bytes: f64) -> String {
    match bytes {
        b if b < 1023.5 => format!("{b:.0} B"),
        b if b < 1024.0 * 1023.95 => format!("{:.1} KiB", b / 1024.0),
        b => format!("{:.1} MiB", b / (1024.0 * 1024.0)),
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

fn maps(results: &Results, group: Group) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    for row in results
        .rows
        .iter()
        .filter(|row| row.group == group || (group == Group::Chunks && row.map == Chunks::name()))
    {
        if !names.contains(&row.map) {
            names.push(row.map.clone());
        }
    }
    names
}

fn report(results: &Results) -> String {
    let mut out = String::new();
    for (group, title) in [
        (Group::Candidates, "Candidates"),
        (Group::Chunks, "Chunk table variants"),
    ] {
        let names = maps(results, group);
        let _ = writeln!(out, "## {title}\n");
        let distributions: &[Distribution] = match group {
            Group::Candidates => &[
                Distribution::Dense,
                Distribution::Sparse,
                Distribution::Scattered,
            ],
            Group::Chunks => &[Distribution::Dense, Distribution::Sparse],
        };
        for &distribution in distributions {
            let d = distribution.name();
            let _ = writeln!(out, "### Operations, {d} object numbers\n");
            let _ = writeln!(
                out,
                "Median times per object, the lower of {PASSES} passes; the memory and the blocks of one whole version per object.\n"
            );
            let _ = writeln!(
                out,
                "| Map | Objects | Insert | Lookup, hit | Lookup, miss | Ordered visit | Memory | Blocks |"
            );
            let _ = writeln!(
                out,
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
            );
            for name in &names {
                for n in SIZES {
                    let get = |metric| results.find(name, distribution, n, None, metric);
                    let Some(insert) = get("insert_ns") else {
                        continue;
                    };
                    let miss = get("miss_ns").map_or_else(|| "—".to_owned(), time);
                    let _ = writeln!(
                        out,
                        "| {name} | {} | {} | {} | {miss} | {} | {} | {:.2} |",
                        thousands(n),
                        time(insert),
                        time(get("hit_ns").unwrap_or(f64::NAN)),
                        time(get("ordered_ns").unwrap_or(f64::NAN)),
                        size(get("bytes_per_object").unwrap_or(f64::NAN)),
                        get("blocks_per_object").unwrap_or(f64::NAN),
                    );
                }
            }
            let _ = writeln!(out);
            for pattern in [Pattern::Update, Pattern::Append] {
                let p = pattern.name();
                let _ = writeln!(out, "### Transactions, {d} object numbers, {p}\n");
                let _ = writeln!(
                    out,
                    "Median time to commit one transaction, the lower of {PASSES} passes, and the memory that its version adds while the base stays alive.\n"
                );
                let _ = writeln!(
                    out,
                    "| Map | Objects | 1 write | 100 writes | 10,000 writes |"
                );
                let _ = writeln!(out, "| --- | ---: | ---: | ---: | ---: |");
                for name in &names {
                    for n in SIZES {
                        let mut cells = Vec::new();
                        for k in WRITES {
                            let get = |metric| {
                                results.find(name, distribution, n, Some((pattern, k)), metric)
                            };
                            if let (Some(commit), Some(bytes)) = (get("commit_ns"), get("bytes")) {
                                cells.push(format!("{}, {}", time(commit), size(bytes)));
                            }
                        }
                        if !cells.is_empty() {
                            let _ = writeln!(
                                out,
                                "| {name} | {} | {} |",
                                thousands(n),
                                cells.join(" | ")
                            );
                        }
                    }
                }
                let _ = writeln!(out);
            }
        }
    }
    out
}

fn csv_rows(results: &Results) -> String {
    let mut out = String::from("pass,map,distribution,objects,pattern,writes,metric,value\n");
    for row in &results.rows {
        let (pattern, writes) = row
            .transaction
            .map_or(("", String::new()), |(p, k)| (p.name(), k.to_string()));
        let _ = writeln!(
            out,
            "{},{},{},{},{pattern},{writes},{},{}",
            row.pass + 1,
            row.map,
            row.distribution.name(),
            row.objects,
            row.metric,
            row.value
        );
    }
    out
}
