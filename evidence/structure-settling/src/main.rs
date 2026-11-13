//! Microbenchmark harness for issue #765: compares current eager behavior,
//! Option A (`Settler` lazy coalescing), and Option B (immediate maintenance
//! via consolidated helpers) across `OutlineCount`, `ParentTree`, and
//! `TreeLimits` in both pure-write and interleaved-read transactions.

mod name_tree;
mod outline;
mod parent_tree;

use std::env;
use std::fs::File;
use std::hint::black_box;
use std::io::{BufWriter, Write};
use std::time::Instant;

use pyropdf_store::version::Document;

#[derive(Clone, Copy, Debug, Default)]
pub struct Stats {
    pub settler_runs: usize,
    pub reads: usize,
    pub writes: usize,
}

#[derive(Clone, Debug)]
pub struct BenchRow {
    pub structure: &'static str,
    pub workload: &'static str,
    pub size: usize,
    pub ops: usize,
    pub mode: &'static str,
    pub variant: &'static str,
    pub settler_runs: usize,
    pub object_reads: usize,
    pub object_writes: usize,
    pub changed_objects: usize,
    pub median_us: f64,
    pub p90_us: f64,
}

/// Times `samples` forward transactions on `doc`, calling `doc.undo()` outside
/// the timed span after each iteration so only the forward transaction + commit
/// cost is measured.
pub fn time_with_undo(samples: usize, doc: &Document, mut f: impl FnMut(&Document)) -> (f64, f64) {
    for _ in 0..3 {
        f(black_box(doc));
        doc.undo().expect("warmup undo");
    }
    let mut durations_us = Vec::with_capacity(samples);
    for _ in 0..samples {
        let start = Instant::now();
        f(black_box(doc));
        let elapsed = start.elapsed().as_secs_f64() * 1_000_000.0;
        durations_us.push(elapsed);
        doc.undo().expect("sample undo");
    }
    durations_us.sort_by(f64::total_cmp);
    let mid = durations_us[durations_us.len() / 2];
    let p90_idx = ((durations_us.len() - 1) * 9) / 10;
    let p90 = durations_us[p90_idx];
    (mid, p90)
}

fn write_csv(path: &str, rows: &[BenchRow]) {
    let file = File::create(path).expect("create CSV output");
    let mut out = BufWriter::new(file);
    writeln!(
        out,
        "structure,workload,size,ops,mode,variant,settler_runs,object_reads,object_writes,changed_objects,median_us,p90_us"
    )
    .unwrap();
    for r in rows {
        writeln!(
            out,
            "{},{},{},{},{},{},{},{},{},{},{:.2},{:.2}",
            r.structure,
            r.workload,
            r.size,
            r.ops,
            r.mode,
            r.variant,
            r.settler_runs,
            r.object_reads,
            r.object_writes,
            r.changed_objects,
            r.median_us,
            r.p90_us,
        )
        .unwrap();
    }
}

fn print_markdown(rows: &[BenchRow]) {
    println!("## Rust Store/Engine Microbenchmarks (`structure-settling-bench`)\n");
    for structure in ["OutlineCount", "ParentTree", "TreeLimits"] {
        println!("### `{structure}`\n");
        println!(
            "| Size | Ops | Mode | Variant | Settler runs | Store reads | Store writes | Changed objs | Median (µs) | p90 (µs) |"
        );
        println!(
            "| ---: | --: | :--- | :------ | -----------: | ----------: | -----------: | -----------: | ----------: | -------: |"
        );
        for r in rows.iter().filter(|r| r.structure == structure) {
            println!(
                "| {} | {} | `{}` | `{}` | {} | {} | {} | {} | {:.1} | {:.1} |",
                r.size,
                r.ops,
                r.mode,
                r.variant,
                r.settler_runs,
                r.object_reads,
                r.object_writes,
                r.changed_objects,
                r.median_us,
                r.p90_us,
            );
        }
        println!();
    }
}

fn main() {
    let csv_path = env::args().nth(1);
    let mut rows = Vec::new();
    rows.extend(outline::run_all());
    rows.extend(parent_tree::run_all());
    rows.extend(name_tree::run_all());
    if let Some(path) = csv_path {
        write_csv(&path, &rows);
    }
    print_markdown(&rows);
}
