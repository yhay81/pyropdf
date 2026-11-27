//! Benchmark and verification harness for issue #1197:
//! iterative, budget-charged pre-flight nesting & inline-image guard before hayro.
//!
//! 1. Verifies that every synthetic crash/DoS/truncation PDF (#15, #740,
//!    tiling-pattern cycle, Type 3 glyph cycle, content operand nesting > 64)
//!    is refused cleanly by `guard::validate_page` under `DocumentLimits::web()`
//!    in sub-millisecond time without invoking `hayro`.
//! 2. Benchmarks the pre-flight guard across the 15-document real-world corpus
//!    (156 pages), comparing:
//!    - per-page cold preflight (`guard_cold_ms`, no cross-page cache)
//!    - document-cached preflight (`guard_cached_ms`, shared `CheckedCache` per version)
//!    - `render::raster` at 72 dpi
//!    - `extract::page_text`
//!
//!    Also records peak F6 `Budget` memory charged and maximum nesting depths
//!    observed across all 156 pages (confirming 0 false positives).
//!
//! Usage:
//!   cargo run --release --manifest-path evidence/preflight-guard/Cargo.toml -- \
//!     evidence/preflight-guard/results macos-arm64 tests/assets/real_world/*.pdf

mod guard;

use std::fmt::Write as _;
use std::path::Path;
use std::sync::Arc;
use std::time::Instant;

use guard::{CheckedCache, Report, validate_page};
use pyropdf_engine::bridge::Bridged;
use pyropdf_engine::extract::{TextOptions, page_text};
use pyropdf_engine::render::{self, Fallback, Options};
use pyropdf_store::budget::{Budget, DocumentLimits};
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::save::FileBase;
use pyropdf_store::value::Bytes;
use pyropdf_store::version::{Document, Version};

struct OpenedDoc {
    version: Arc<Version>,
    pages: PageIndex,
}

impl OpenedDoc {
    fn open(bytes: Vec<u8>, limits: &DocumentLimits) -> Result<Self, String> {
        let budget = Budget::new(limits);
        let base = FileBase::open(Bytes::new(bytes), &budget).map_err(|e| e.to_string())?;
        let document = Document::new(Arc::new(base), None);
        let version = document.current();
        let pages =
            PageIndex::build(&mut Reads::new(&version, &budget)).map_err(|e| e.to_string())?;
        Ok(Self { version, pages })
    }

    fn bridge(&self) -> Arc<Bridged> {
        Arc::new(Bridged::new(&self.version, &self.pages, &Budget::default()).expect("bridge"))
    }
}

fn millis(started: Instant) -> f64 {
    started.elapsed().as_secs_f64() * 1e3
}

// --- Synthetic PDF builders for verifying guard refusal ---

fn build_pdf(objects: &[Vec<u8>]) -> Vec<u8> {
    let mut out = Vec::from(&b"%PDF-1.7\n"[..]);
    let mut offsets = Vec::with_capacity(objects.len());
    for (idx, body) in objects.iter().enumerate() {
        offsets.push(out.len());
        let _ = writeln!(out_str(&mut out), "{} 0 obj", idx + 1);
        out.extend_from_slice(body);
        out.extend_from_slice(b"\nendobj\n");
    }
    let xref = out.len();
    let _ = write!(
        out_str(&mut out),
        "xref\n0 {}\n0000000000 65535 f \n",
        objects.len() + 1
    );
    for offset in offsets {
        let _ = writeln!(out_str(&mut out), "{offset:010} 00000 n ");
    }
    let _ = write!(
        out_str(&mut out),
        "trailer\n<</Size {}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n",
        objects.len() + 1
    );
    out
}

struct ByteWriter<'a>(&'a mut Vec<u8>);
impl fmt::Write for ByteWriter<'_> {
    fn write_str(&mut self, s: &str) -> fmt::Result {
        self.0.extend_from_slice(s.as_bytes());
        Ok(())
    }
}
use std::fmt;
fn out_str(buf: &mut Vec<u8>) -> ByteWriter<'_> {
    ByteWriter(buf)
}

fn one_page(content: &[u8], resources: &[u8], extra: &[Vec<u8>]) -> Vec<u8> {
    let mut cobj = Vec::new();
    let _ = writeln!(out_str(&mut cobj), "<</Length {}>>stream", content.len());
    cobj.extend_from_slice(content);
    cobj.extend_from_slice(b"\nendstream");

    let mut page_dict =
        Vec::from(&b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Resources "[..]);
    page_dict.extend_from_slice(resources);
    page_dict.extend_from_slice(b"/Contents 4 0 R>>");

    let mut objects = vec![
        b"<</Type/Catalog/Pages 2 0 R>>".to_vec(),
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>".to_vec(),
        page_dict,
        cobj,
    ];
    objects.extend_from_slice(extra);
    build_pdf(&objects)
}

fn synthetic_cases() -> Vec<(&'static str, Vec<u8>)> {
    let type4_50k = {
        let mut prog = vec![b'{'; 50_000];
        prog.extend(std::iter::repeat_n(b'}', 50_000));
        let mut func = Vec::new();
        let _ = writeln!(
            out_str(&mut func),
            "<</FunctionType 4/Domain[0 1]/Range[0 1]/Length {}>>stream",
            prog.len()
        );
        func.extend_from_slice(&prog);
        func.extend_from_slice(b"\nendstream");
        let sh =
            b"<</ShadingType 2/ColorSpace/DeviceGray/Coords[0 0 200 0]/Function 6 0 R>>".to_vec();
        one_page(b"/Sh0 sh", b"<</Shading<</Sh0 5 0 R>>>>", &[sh, func])
    };

    let type3_cycle = {
        let sh =
            b"<</ShadingType 2/ColorSpace/DeviceGray/Coords[0 0 200 0]/Function 6 0 R>>".to_vec();
        let func =
            b"<</FunctionType 3/Domain[0 1]/Bounds[]/Encode[0 1]/Functions[6 0 R]>>".to_vec();
        one_page(b"/Sh0 sh", b"<</Shading<</Sh0 5 0 R>>>>", &[sh, func])
    };

    let type3_chain_5k = {
        let mut extra = vec![
            b"<</ShadingType 2/ColorSpace/DeviceGray/Coords[0 0 200 0]/Function 6 0 R>>".to_vec(),
        ];
        for i in 0..5_000usize {
            extra.push(
                format!(
                    "<</FunctionType 3/Domain[0 1]/Bounds[]/Encode[0 1]/Functions[{} 0 R]>>",
                    7 + i
                )
                .into_bytes(),
            );
        }
        extra.push(b"<</FunctionType 2/Domain[0 1]/C0[0]/C1[1]/N 1>>".to_vec());
        one_page(b"/Sh0 sh", b"<</Shading<</Sh0 5 0 R>>>>", &extra)
    };

    let cs_indexed_cycle = one_page(
        b"/CS0 cs 0 scn 0 0 50 50 re f",
        b"<</ColorSpace<</CS0 5 0 R>>>>",
        &[b"[/Indexed 5 0 R 0 <00>]".to_vec()],
    );

    let cs_icc_cycle = {
        let icc = b"<</N 3/Alternate 5 0 R/Length 4>>stream\nxxxx\nendstream".to_vec();
        one_page(
            b"/CS0 cs 0 0 0 scn 0 0 50 50 re f",
            b"<</ColorSpace<</CS0 5 0 R>>>>",
            &[b"[/ICCBased 6 0 R]".to_vec(), icc],
        )
    };

    let cs_chain_5k = {
        let mut extra = Vec::new();
        for i in 0..5_000usize {
            extra.push(format!("[/Indexed {} 0 R 0 <00>]", 6 + i).into_bytes());
        }
        extra.push(b"/DeviceGray".to_vec());
        one_page(
            b"/CS0 cs 0 scn 0 0 50 50 re f",
            b"<</ColorSpace<</CS0 5 0 R>>>>",
            &extra,
        )
    };

    let inline_200k = {
        let unit = b"BI /W 1 /H 1 ID x EI EI ";
        let mut body = Vec::with_capacity(unit.len() * 200_000);
        for _ in 0..200_000 {
            body.extend_from_slice(unit);
        }
        one_page(&body, b"<<>>", &[])
    };

    let tiling_cycle = {
        let cell = b"/Pattern cs /P0 scn 0 0 10 10 re f";
        let pat = format!(
            "<</Type/Pattern/PatternType 1/PaintType 1/TilingType 1/BBox[0 0 10 10]/XStep 10/YStep 10/Resources<</Pattern<</P0 5 0 R>>>>/Length {}>>stream\n{}\nendstream",
            cell.len(),
            std::str::from_utf8(cell).unwrap()
        )
        .into_bytes();
        one_page(
            b"/Pattern cs /P0 scn 0 0 50 50 re f",
            b"<</Pattern<</P0 5 0 R>>>>",
            &[pat],
        )
    };

    let type3_glyph_cycle = {
        let proc = b"100 0 d0 BT /F0 10 Tf (a) Tj ET";
        let cp = format!(
            "<</Length {}>>stream\n{}\nendstream",
            proc.len(),
            std::str::from_utf8(proc).unwrap()
        )
        .into_bytes();
        let font = b"<</Type/Font/Subtype/Type3/FontBBox[0 0 100 100]/FontMatrix[0.01 0 0 0.01 0 0]/CharProcs<</a 6 0 R>>/Encoding<</Type/Encoding/Differences[97 /a]>>/FirstChar 97/LastChar 97/Widths[100]/Resources<</Font<</F0 5 0 R>>>>>>".to_vec();
        one_page(
            b"BT /F0 10 Tf (a) Tj ET",
            b"<</Font<</F0 5 0 R>>>>",
            &[font, cp],
        )
    };

    let tiling_chain_2k = {
        let mut extra = Vec::with_capacity(2_000);
        for i in 0..2_000usize {
            let obj_num = 5 + i;
            let (cell, res) = if i + 1 < 2_000 {
                (
                    format!("/Pattern cs /P{} scn 0 0 10 10 re f", i + 1),
                    format!("<</Pattern<</P{} {} 0 R>>>>", i + 1, obj_num + 1),
                )
            } else {
                ("1 0 0 rg 0 0 10 10 re f".to_string(), "<<>>".to_string())
            };
            let pat = format!(
                "<</Type/Pattern/PatternType 1/PaintType 1/TilingType 1/BBox[0 0 10 10]/XStep 10/YStep 10/Resources {res}/Length {}>>stream\n{cell}\nendstream",
                cell.len()
            )
            .into_bytes();
            extra.push(pat);
        }
        one_page(
            b"/Pattern cs /P0 scn 0 0 50 50 re f",
            b"<</Pattern<</P0 5 0 R>>>>",
            &extra,
        )
    };

    let type3_chain_2k = {
        let mut extra = Vec::with_capacity(4_000);
        for i in 0..2_000usize {
            let font_num = 5 + 2 * i;
            let cp_num = font_num + 1;
            let (proc, res) = if i + 1 < 2_000 {
                let next_font = 5 + 2 * (i + 1);
                (
                    format!("100 0 d0 BT /F{} 10 Tf (a) Tj ET", i + 1),
                    format!("<</Font<</F{} {next_font} 0 R>>>>", i + 1),
                )
            } else {
                ("100 0 d0 0 0 50 50 re f".to_string(), "<<>>".to_string())
            };
            let font = format!(
                "<</Type/Font/Subtype/Type3/FontBBox[0 0 100 100]/FontMatrix[0.01 0 0 0.01 0 0]/CharProcs<</a {cp_num} 0 R>>/Encoding<</Type/Encoding/Differences[97 /a]>>/FirstChar 97/LastChar 97/Widths[100]/Resources {res}>>"
            )
            .into_bytes();
            let cp = format!("<</Length {}>>stream\n{proc}\nendstream", proc.len()).into_bytes();
            extra.push(font);
            extra.push(cp);
        }
        one_page(b"BT /F0 10 Tf (a) Tj ET", b"<</Font<</F0 5 0 R>>>>", &extra)
    };

    let form_cycle = {
        let body = b"/Fm0 Do";
        let form = format!(
            "<</Type/XObject/Subtype/Form/BBox[0 0 100 100]/Resources<</XObject<</Fm0 5 0 R>>>>/Length {}>>stream\n/Fm0 Do\nendstream",
            body.len()
        )
        .into_bytes();
        one_page(b"/Fm0 Do", b"<</XObject<</Fm0 5 0 R>>>>", &[form])
    };

    let content_600 = {
        let mut body = vec![b'['; 600];
        body.extend(std::iter::repeat_n(b']', 600));
        body.extend_from_slice(b" pop");
        one_page(&body, b"<<>>", &[])
    };

    vec![
        ("type4-braces-50000", type4_50k),
        ("type3-function-cycle", type3_cycle),
        ("type3-function-chain-5000", type3_chain_5k),
        ("cs-indexed-cycle", cs_indexed_cycle),
        ("cs-icc-alternate-cycle", cs_icc_cycle),
        ("cs-indexed-chain-5000", cs_chain_5k),
        ("inline-image-nested-200000", inline_200k),
        ("tiling-pattern-cycle", tiling_cycle),
        ("tiling-pattern-chain-2000", tiling_chain_2k),
        ("type3-glyph-cycle", type3_glyph_cycle),
        ("type3-glyph-chain-2000", type3_chain_2k),
        ("form-xobject-cycle", form_cycle),
        ("content-operand-depth-600", content_600),
    ]
}

struct PageBench {
    report: Report,
    guard_cold_ms: f64,
    guard_cached_ms: f64,
    render_ms: f64,
    text_ms: f64,
}

fn measure_doc(doc: &OpenedDoc, runs: usize) -> Vec<PageBench> {
    let count = doc.pages.len();
    let limits = DocumentLimits::web();
    let mut results: Vec<PageBench> = (0..count)
        .map(|_| PageBench {
            report: Report::default(),
            guard_cold_ms: f64::INFINITY,
            guard_cached_ms: f64::INFINITY,
            render_ms: f64::INFINITY,
            text_ms: f64::INFINITY,
        })
        .collect();
    let options = Options::new((1.0, 0.0, 0.0, 1.0), false);

    for run in 0..runs {
        // 1. Pre-flight guard (per-page cold, empty cache).
        for (idx, page) in doc.pages.pages().iter().enumerate() {
            let budget = Budget::new(&limits);
            let mut reads = Reads::new(&doc.version, &budget);
            let started = Instant::now();
            let report =
                validate_page(&mut reads, page, None).expect("corpus page must pass guard");
            let ms = millis(started);
            if run == 0 {
                results[idx].report = report;
            }
            results[idx].guard_cold_ms = results[idx].guard_cold_ms.min(ms);
        }

        // 2. Pre-flight guard (shared CheckedCache across pages of the same Version).
        let mut cache = CheckedCache::new();
        for (idx, page) in doc.pages.pages().iter().enumerate() {
            let budget = Budget::new(&limits);
            let mut reads = Reads::new(&doc.version, &budget);
            let started = Instant::now();
            let _ = validate_page(&mut reads, page, Some(&mut cache))
                .expect("corpus page must pass cached guard");
            let ms = millis(started);
            results[idx].guard_cached_ms = results[idx].guard_cached_ms.min(ms);
        }

        // 3. Render 72 dpi, fresh bridge per run.
        let bridged = doc.bridge();
        for (idx, row) in results.iter_mut().enumerate().take(count) {
            let started = Instant::now();
            let raster = render::raster(&bridged, &Fallback::default(), idx, &options);
            let ms = millis(started);
            std::hint::black_box(raster.ok());
            row.render_ms = row.render_ms.min(ms);
        }

        // 4. Text extraction, fresh bridge per run.
        let bridged = doc.bridge();
        let budget = Budget::default();
        for (idx, row) in results.iter_mut().enumerate().take(count) {
            let mut out = String::new();
            let started = Instant::now();
            let text = page_text(&bridged, idx, &TextOptions::default(), &budget, &mut out);
            let ms = millis(started);
            std::hint::black_box(text.ok());
            row.text_ms = row.text_ms.min(ms);
        }
    }
    results
}

fn main() {
    let mut args = std::env::args().skip(1);
    let out_dir = args.next().expect("OUT_DIR");
    let label = args.next().expect("LABEL");
    let files: Vec<String> = args.collect();
    let runs = std::env::var("RUNS").map_or(5usize, |v| v.parse().expect("RUNS"));

    // Part A: verify synthetic adversarial corpus refusal.
    let limits = DocumentLimits::web();
    let mut synthetic_md = String::from(
        "## Synthetic adversarial verification (guard before hayro)\n\n| Case | PDF bytes | Guard verdict | Guard time (ms) |\n| --- | ---: | --- | ---: |\n",
    );
    let mut synthetic_json = String::from("  \"synthetic\": [\n");
    let synth = synthetic_cases();
    for (idx, (name, bytes)) in synth.iter().enumerate() {
        let started = Instant::now();
        let verdict = match OpenedDoc::open(bytes.clone(), &limits) {
            Ok(doc) => {
                let budget = Budget::new(&limits);
                let mut reads = Reads::new(&doc.version, &budget);
                match validate_page(&mut reads, &doc.pages.pages()[0], None) {
                    Ok(_) => panic!("BUG: guard failed to refuse adversarial case {name}"),
                    Err(refusal) => refusal.to_string(),
                }
            }
            Err(err) => format!("store_open_refused: {err}"),
        };
        let ms = millis(started);
        eprintln!("synthetic {name:28} -> refused in {ms:.3} ms ({verdict})");
        let _ = writeln!(
            synthetic_md,
            "| `{name}` | {} | `{verdict}` | {ms:.3} |",
            bytes.len()
        );
        let escaped = verdict.replace('\\', "\\\\").replace('"', "\\\"");
        let _ = writeln!(
            synthetic_json,
            "    {{\"case\": \"{name}\", \"pdf_bytes\": {}, \"refused\": true, \"verdict\": \"{escaped}\", \"elapsed_ms\": {ms:.4}}}{}",
            bytes.len(),
            if idx + 1 < synth.len() { "," } else { "" }
        );
    }
    synthetic_json.push_str("  ],\n");

    // Part B: 15-document real-world corpus benchmark.
    let mut md = format!(
        "# Pre-flight nesting guard benchmark (`{label}`, best of {runs} runs)\n\n{synthetic_md}\n## 15-document corpus overhead\n\nAll 15 documents under `tests/assets/real_world/` validated under `DocumentLimits::web()` with **0 false positives**.\n\n| Document | Pages | Render 72 dpi (ms) | Text (ms) | Guard cold (ms) | Cold / render | Cold / text | Guard cached (ms) | Cached / render | Streams | Decoded (KiB) | Peak Budget mem (KiB) | Max CS | Max Fn | Max T4 | Max Stream | Max OpDepth |\n| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |\n"
    );
    let mut pages_json = String::from("  \"pages\": [\n");

    let mut all: Vec<(String, usize, PageBench)> = Vec::new();
    let mut corpus_report = Report::default();
    let mut tot_render = 0.0;
    let mut tot_text = 0.0;
    let mut tot_cold = 0.0;
    let mut tot_cached = 0.0;

    for file in &files {
        let name = Path::new(file)
            .file_stem()
            .expect("stem")
            .to_string_lossy()
            .into_owned();
        let bytes = std::fs::read(file).expect("read corpus pdf");
        let doc = OpenedDoc::open(bytes, &DocumentLimits::default()).expect("open corpus pdf");
        let results = measure_doc(&doc, runs);
        let render: f64 = results.iter().map(|p| p.render_ms).sum();
        let text: f64 = results.iter().map(|p| p.text_ms).sum();
        let cold: f64 = results.iter().map(|p| p.guard_cold_ms).sum();
        let cached: f64 = results.iter().map(|p| p.guard_cached_ms).sum();
        tot_render += render;
        tot_text += text;
        tot_cold += cold;
        tot_cached += cached;

        let mut doc_rep = Report::default();
        for r in &results {
            doc_rep.merge(r.report);
        }
        corpus_report.merge(doc_rep);

        let _ = writeln!(
            md,
            "| `{name}` | {} | {render:.2} | {text:.2} | {cold:.2} | {:.2}% | {:.2}% | {cached:.2} | {:.2}% | {} | {:.1} | {:.1} | {} | {} | {} | {} | {} |",
            results.len(),
            100.0 * cold / render,
            100.0 * cold / text,
            100.0 * cached / render,
            doc_rep.streams_scanned,
            doc_rep.content_bytes_scanned as f64 / 1024.0,
            doc_rep.peak_budget_memory_bytes as f64 / 1024.0,
            doc_rep.max_cs_depth,
            doc_rep.max_fn_depth,
            doc_rep.max_type4_braces,
            doc_rep.max_stream_depth,
            doc_rep.max_content_depth,
        );
        eprintln!(
            "{name:34} {:3}p  render={render:7.2}ms  text={text:6.2}ms  cold={cold:6.2}ms ({:5.2}%)  cached={cached:6.2}ms ({:5.2}%)",
            results.len(),
            100.0 * cold / render,
            100.0 * cached / render
        );
        for (idx, bench) in results.into_iter().enumerate() {
            all.push((name.clone(), idx + 1, bench));
        }
    }

    let _ = writeln!(
        md,
        "| **Corpus ({} docs)** | **{}** | **{tot_render:.2}** | **{tot_text:.2}** | **{tot_cold:.2}** | **{:.2}%** | **{:.2}%** | **{tot_cached:.2}** | **{:.2}%** | **{}** | **{:.1}** | **{:.1}** | **{}** | **{}** | **{}** | **{}** | **{}** |\n",
        files.len(),
        all.len(),
        100.0 * tot_cold / tot_render,
        100.0 * tot_cold / tot_text,
        100.0 * tot_cached / tot_render,
        corpus_report.streams_scanned,
        corpus_report.content_bytes_scanned as f64 / 1024.0,
        corpus_report.peak_budget_memory_bytes as f64 / 1024.0,
        corpus_report.max_cs_depth,
        corpus_report.max_fn_depth,
        corpus_report.max_type4_braces,
        corpus_report.max_stream_depth,
        corpus_report.max_content_depth,
    );

    let mut cold_ms: Vec<f64> = all.iter().map(|(_, _, p)| p.guard_cold_ms).collect();
    cold_ms.sort_by(f64::total_cmp);
    let mut cached_ms: Vec<f64> = all.iter().map(|(_, _, p)| p.guard_cached_ms).collect();
    cached_ms.sort_by(f64::total_cmp);
    let mut cold_ratios: Vec<f64> = all
        .iter()
        .map(|(_, _, p)| p.guard_cold_ms / p.render_ms.max(1e-6))
        .collect();
    cold_ratios.sort_by(f64::total_cmp);
    let mut cached_ratios: Vec<f64> = all
        .iter()
        .map(|(_, _, p)| p.guard_cached_ms / p.render_ms.max(1e-6))
        .collect();
    cached_ratios.sort_by(f64::total_cmp);
    let worst = all
        .iter()
        .max_by(|a, b| {
            (a.2.guard_cold_ms / a.2.render_ms.max(1e-6))
                .total_cmp(&(b.2.guard_cold_ms / b.2.render_ms.max(1e-6)))
        })
        .expect("at least one page");

    let n = all.len();
    let _ = writeln!(
        md,
        "## Per-page distribution ({n} pages)\n\n| Metric | Median | 90th percentile | Max |\n| --- | ---: | ---: | ---: |\n| Guard cold (ms) | {:.3} | {:.3} | {:.3} |\n| Guard cached (ms) | {:.3} | {:.3} | {:.3} |\n| Guard cold / render | {:.2}% | {:.2}% | {:.2}% (`{}` p.{}) |\n| Guard cached / render | {:.2}% | {:.2}% | {:.2}% |\n",
        cold_ms[n / 2],
        cold_ms[(n - 1) * 9 / 10],
        cold_ms[n - 1],
        cached_ms[n / 2],
        cached_ms[(n - 1) * 9 / 10],
        cached_ms[n - 1],
        100.0 * cold_ratios[n / 2],
        100.0 * cold_ratios[(n - 1) * 9 / 10],
        100.0 * cold_ratios[n - 1],
        worst.0,
        worst.1,
        100.0 * cached_ratios[n / 2],
        100.0 * cached_ratios[(n - 1) * 9 / 10],
        100.0 * cached_ratios[n - 1],
    );

    for (idx, (doc, page, p)) in all.iter().enumerate() {
        let r = &p.report;
        let _ = writeln!(
            pages_json,
            "    {{\"document\": \"{doc}\", \"page\": {page}, \"render_ms\": {:.4}, \"text_ms\": {:.4}, \"guard_cold_ms\": {:.4}, \"guard_cached_ms\": {:.4}, \"streams\": {}, \"content_bytes\": {}, \"type4_streams\": {}, \"objects_visited\": {}, \"max_cs_depth\": {}, \"max_fn_depth\": {}, \"max_type4_braces\": {}, \"max_stream_depth\": {}, \"max_content_depth\": {}, \"inline_images\": {}, \"max_inline_lookahead\": {}, \"peak_budget_memory_bytes\": {}}}{}",
            p.render_ms,
            p.text_ms,
            p.guard_cold_ms,
            p.guard_cached_ms,
            r.streams_scanned,
            r.content_bytes_scanned,
            r.type4_streams_scanned,
            r.objects_visited,
            r.max_cs_depth,
            r.max_fn_depth,
            r.max_type4_braces,
            r.max_stream_depth,
            r.max_content_depth,
            r.inline_images_seen,
            r.max_inline_lookahead_chain,
            r.peak_budget_memory_bytes,
            if idx + 1 < all.len() { "," } else { "" }
        );
    }
    pages_json.push_str("  ]\n");

    let full_json = format!(
        "{{\n  \"label\": \"{label}\",\n  \"runs\": {runs},\n  \"totals\": {{\"documents\": {}, \"pages\": {}, \"render_ms\": {tot_render:.4}, \"text_ms\": {tot_text:.4}, \"guard_cold_ms\": {tot_cold:.4}, \"guard_cached_ms\": {tot_cached:.4}, \"max_cs_depth\": {}, \"max_fn_depth\": {}, \"max_type4_braces\": {}, \"max_stream_depth\": {}, \"max_content_depth\": {}, \"inline_images_seen\": {}, \"peak_budget_memory_bytes\": {}}},\n{synthetic_json}{pages_json}}}\n",
        files.len(),
        all.len(),
        corpus_report.max_cs_depth,
        corpus_report.max_fn_depth,
        corpus_report.max_type4_braces,
        corpus_report.max_stream_depth,
        corpus_report.max_content_depth,
        corpus_report.inline_images_seen,
        corpus_report.peak_budget_memory_bytes,
    );

    std::fs::create_dir_all(&out_dir).expect("create out dir");
    std::fs::write(Path::new(&out_dir).join(format!("{label}.json")), full_json)
        .expect("write json");
    std::fs::write(Path::new(&out_dir).join(format!("{label}.md")), md).expect("write md");
}
