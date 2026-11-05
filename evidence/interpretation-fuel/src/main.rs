//! The measurement behind issue #678: on the corpus, what option B's walk
//! costs per page, what it counts, and which pages candidate limits of
//! `interpretation_fuel` would refuse; beside it, the time that hayro spends
//! in `render::raster` at 72 dpi and in `extract::page_text`.
//!
//! Every page of each document is measured in order, each run on a fresh
//! bridge, so that nothing hayro or the walk memoizes survives from an
//! earlier run. A page's time is the best of the runs. The walk starts each
//! page with an empty memo, as one render of the page would.
//!
//! Usage: `interpretation-fuel OUT_DIR LABEL FILE...`, with `RUNS` in the
//! environment to change the default of 5. It writes `OUT_DIR/LABEL.json`
//! and `OUT_DIR/LABEL.md`.

mod walk;

use std::fmt::Write as _;
use std::path::Path;
use std::sync::Arc;
use std::time::Instant;

use pyropdf_engine::bridge::Bridged;
use pyropdf_engine::extract::{TextOptions, page_text};
use pyropdf_engine::render::{self, Fallback, Options};
use pyropdf_store::budget::Budget;
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::save::FileBase;
use pyropdf_store::value::Bytes;
use pyropdf_store::version::{Document, Version};

use walk::{Walk, Walker};

/// The candidate limits of `interpretation_fuel`, in operators.
const LIMITS: [u64; 6] = [1 << 14, 1 << 16, 1 << 18, 1 << 20, 1 << 22, 1 << 24];

struct Corpus {
    version: Arc<Version>,
    pages: PageIndex,
}

impl Corpus {
    fn open(bytes: Vec<u8>) -> Self {
        let budget = Budget::default();
        let base = FileBase::open(Bytes::new(bytes), &budget).expect("the store opens the file");
        let document = Document::new(Arc::new(base), None);
        let version = document.current();
        let pages = PageIndex::build(&mut Reads::new(&version, &budget)).expect("pages");
        Self { version, pages }
    }

    fn bridge(&self) -> Arc<Bridged> {
        Arc::new(Bridged::new(&self.version, &self.pages, &Budget::default()).expect("bridge"))
    }
}

#[derive(Clone)]
struct PageResult {
    walk: Walk,
    walk_ms: f64,
    /// hayro-syntax's tokenizer alone over the page's content, decoding
    /// included: what any walk of the page costs at least.
    tokens_ms: f64,
    render_ms: f64,
    text_ms: f64,
}

fn millis(started: Instant) -> f64 {
    started.elapsed().as_secs_f64() * 1e3
}

fn measure(corpus: &Corpus, runs: usize) -> Vec<PageResult> {
    let count = corpus.bridge().pdf().pages().len();
    let mut results: Vec<Option<PageResult>> = vec![None; count];
    let mut keep = |index: usize, walk: Option<Walk>, walk_ms, tokens_ms, render_ms, text_ms| {
        let entry = results[index].get_or_insert(PageResult {
            walk: Walk::default(),
            walk_ms: f64::INFINITY,
            tokens_ms: f64::INFINITY,
            render_ms: f64::INFINITY,
            text_ms: f64::INFINITY,
        });
        if let Some(walk) = walk {
            entry.walk = walk;
        }
        entry.walk_ms = entry.walk_ms.min(walk_ms);
        entry.tokens_ms = entry.tokens_ms.min(tokens_ms);
        entry.render_ms = entry.render_ms.min(render_ms);
        entry.text_ms = entry.text_ms.min(text_ms);
    };
    let options = Options::new((1.0, 0.0, 0.0, 1.0), false);
    for run in 0..runs {
        // The walk, cold.
        let bridged = corpus.bridge();
        let mut walks = Vec::with_capacity(count);
        for page in bridged.pdf().pages().iter() {
            let started = Instant::now();
            let walk = Walker::new().page(page);
            walks.push((walk, millis(started)));
        }
        // The tokenizer alone, cold.
        let bridged = corpus.bridge();
        let mut tokens = Vec::with_capacity(count);
        for page in bridged.pdf().pages().iter() {
            let started = Instant::now();
            let mut operations = page.operations();
            let mut count = 0u64;
            while operations.next().is_some() {
                count += 1;
            }
            std::hint::black_box(count);
            tokens.push(millis(started));
        }
        // Rendering, cold, with the thread's cache kept across the pages.
        let bridged = corpus.bridge();
        let mut renders = Vec::with_capacity(count);
        for index in 0..count {
            let started = Instant::now();
            let raster = render::raster(&bridged, &Fallback::default(), index, &options);
            renders.push(millis(started));
            std::hint::black_box(raster.ok());
        }
        // Text, cold.
        let bridged = corpus.bridge();
        let budget = Budget::default();
        for (index, (walk, walk_ms)) in walks.into_iter().enumerate() {
            let mut out = String::new();
            let started = Instant::now();
            let text = page_text(&bridged, index, &TextOptions::default(), &budget, &mut out);
            let text_ms = millis(started);
            std::hint::black_box(text.ok());
            keep(
                index,
                (run == 0).then_some(walk),
                walk_ms,
                tokens[index],
                renders[index],
                text_ms,
            );
        }
    }
    results
        .into_iter()
        .map(|result| result.expect("measured"))
        .collect()
}

fn percentile(sorted: &[u64], share: f64) -> u64 {
    if sorted.is_empty() {
        return 0;
    }
    let at = ((sorted.len() - 1) as f64 * share).round() as usize;
    sorted[at]
}

fn main() {
    let mut args = std::env::args().skip(1);
    let out = args.next().expect("OUT_DIR");
    let label = args.next().expect("LABEL");
    let files: Vec<String> = args.collect();
    let runs = std::env::var("RUNS").map_or(5, |value| value.parse().expect("RUNS"));

    let mut json = format!("{{\n  \"label\": \"{label}\",\n  \"runs\": {runs},\n  \"pages\": [\n");
    let mut md = String::new();
    let _ = writeln!(
        md,
        "## Per document\n\nTotals over the pages, best of {runs} runs per page, in milliseconds. The walk tokenizes every stream that interpreting the page runs, once per distinct stream and resources. Operators, shown bytes, and invocations are B's counts: each stream's own times the interpretations of it.\n\n| Document | Pages | Render, 72 dpi | Text | Walk | Walk / render | Walk / text | Page tokens alone | Streams | Decoded content (KiB) | Operators | Shown bytes | Invocations | Most operators on a page |\n| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    );
    let mut all: Vec<(String, usize, PageResult)> = Vec::new();
    let mut totals = (0.0f64, 0.0f64, 0.0f64, 0.0f64);
    for file in &files {
        let name = Path::new(file)
            .file_stem()
            .expect("a file name")
            .to_string_lossy()
            .into_owned();
        let corpus = Corpus::open(std::fs::read(file).expect("the file reads"));
        let results = measure(&corpus, runs);
        let render: f64 = results.iter().map(|page| page.render_ms).sum();
        let text: f64 = results.iter().map(|page| page.text_ms).sum();
        let walked: f64 = results.iter().map(|page| page.walk_ms).sum();
        let tokenized: f64 = results.iter().map(|page| page.tokens_ms).sum();
        totals.0 += render;
        totals.1 += text;
        totals.2 += walked;
        totals.3 += tokenized;
        let streams: u64 = results.iter().map(|page| page.walk.streams).sum();
        let bytes: u64 = results.iter().map(|page| page.walk.bytes).sum();
        let operators: u64 = results
            .iter()
            .map(|page| page.walk.count.operators)
            .fold(0, u64::saturating_add);
        let shown: u64 = results
            .iter()
            .map(|page| page.walk.count.shown)
            .fold(0, u64::saturating_add);
        let invocations: u64 = results
            .iter()
            .map(|page| page.walk.count.invocations)
            .fold(0, u64::saturating_add);
        let most = results
            .iter()
            .map(|page| page.walk.count.operators)
            .max()
            .unwrap_or(0);
        let _ = writeln!(
            md,
            "| {name} | {} | {render:.1} | {text:.1} | {walked:.2} | {:.1}% | {:.1}% | {tokenized:.2} | {streams} | {:.0} | {operators} | {shown} | {invocations} | {most} |",
            results.len(),
            100.0 * walked / render,
            100.0 * walked / text,
            bytes as f64 / 1024.0
        );
        eprintln!("{name}: {} pages", results.len());
        for (index, result) in results.into_iter().enumerate() {
            all.push((name.clone(), index + 1, result));
        }
    }
    let _ = writeln!(
        md,
        "| **Corpus** | {} | **{:.1}** | **{:.1}** | **{:.2}** | **{:.1}%** | **{:.1}%** | **{:.2}** | | | | | | |\n",
        all.len(),
        totals.0,
        totals.1,
        totals.2,
        100.0 * totals.2 / totals.0,
        100.0 * totals.2 / totals.1,
        totals.3
    );

    for (at, (name, page, result)) in all.iter().enumerate() {
        let walk = &result.walk;
        let _ = writeln!(
            json,
            "    {{\"document\": \"{name}\", \"page\": {page}, \"render_ms\": {:.4}, \"text_ms\": {:.4}, \"walk_ms\": {:.4}, \"tokens_ms\": {:.4}, \"operators\": {}, \"shown\": {}, \"invocations\": {}, \"streams\": {}, \"bytes\": {}, \"cycle\": {}, \"unmapped\": {}}}{}",
            result.render_ms,
            result.text_ms,
            result.walk_ms,
            result.tokens_ms,
            walk.count.operators,
            walk.count.shown,
            walk.count.invocations,
            walk.streams,
            walk.bytes,
            walk.cycle,
            walk.unmapped,
            if at + 1 < all.len() { "," } else { "" }
        );
    }
    json.push_str("  ]\n}\n");

    // The walk's cost against the work it bounds, per page.
    let mut ratios: Vec<f64> = all
        .iter()
        .map(|(_, _, result)| result.walk_ms / result.render_ms.max(1e-6))
        .collect();
    ratios.sort_by(f64::total_cmp);
    let worst = all
        .iter()
        .max_by(|a, b| {
            (a.2.walk_ms / a.2.render_ms.max(1e-6))
                .total_cmp(&(b.2.walk_ms / b.2.render_ms.max(1e-6)))
        })
        .expect("a page");
    let mut walk_times: Vec<f64> = all.iter().map(|(_, _, result)| result.walk_ms).collect();
    walk_times.sort_by(f64::total_cmp);
    let _ = writeln!(
        md,
        "## Per page\n\n| | Median | 90th percentile | Largest |\n| --- | ---: | ---: | ---: |\n| Walk (ms) | {:.3} | {:.3} | {:.3} |\n| Walk / render | {:.1}% | {:.1}% | {:.1}% ({} page {}) |",
        walk_times[walk_times.len() / 2],
        walk_times[(walk_times.len() - 1) * 9 / 10],
        walk_times[walk_times.len() - 1],
        100.0 * ratios[ratios.len() / 2],
        100.0 * ratios[(ratios.len() - 1) * 9 / 10],
        100.0 * ratios[ratios.len() - 1],
        worst.0,
        worst.1
    );
    let mut operators: Vec<u64> = all.iter().map(|(_, _, r)| r.walk.count.operators).collect();
    operators.sort_unstable();
    let mut invocations: Vec<u64> = all
        .iter()
        .map(|(_, _, r)| r.walk.count.invocations)
        .collect();
    invocations.sort_unstable();
    let _ = writeln!(
        md,
        "| Operators | {} | {} | {} |\n| Invocations | {} | {} | {} |\n",
        percentile(&operators, 0.5),
        percentile(&operators, 0.9),
        operators.last().copied().unwrap_or(0),
        percentile(&invocations, 0.5),
        percentile(&invocations, 0.9),
        invocations.last().copied().unwrap_or(0)
    );
    let cycles = all.iter().filter(|(_, _, r)| r.walk.cycle).count();
    let unmapped: u64 = all.iter().map(|(_, _, r)| r.walk.unmapped).sum();
    let _ = writeln!(
        md,
        "Pages with a stream that invokes itself: {cycles}. Type 3 codes that `/Differences` does not map: {unmapped}.\n\n## Candidate limits\n\nPages whose operators exceed a limit of `interpretation_fuel`, counted in operators.\n\n| Limit | Pages refused |\n| ---: | ---: |"
    );
    for limit in LIMITS {
        let refused = operators.iter().filter(|&&count| count > limit).count();
        let _ = writeln!(md, "| {limit} | {refused} |");
    }

    std::fs::create_dir_all(&out).expect("the output folder");
    std::fs::write(Path::new(&out).join(format!("{label}.json")), json).expect("json");
    std::fs::write(Path::new(&out).join(format!("{label}.md")), md).expect("markdown");
}
