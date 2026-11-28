//! Release-mode cold overhead and adversarial refusal benchmark for issue #1275:
//! `pyropdf_engine::bridge::preflight::validate_page` (nesting + DAG operator fuel
//! + inline-image EI candidate cap + Type 0 `/Size` + Type 4 operator bounds)
//! across all 15 `tests/assets/real_world/*.pdf` documents under `DocumentLimits::web()`.
//!
//! Usage:
//!   cargo run --release --locked --manifest-path evidence/interpretation-fuel/Cargo.toml \
//!     --bin preflight_fuel -- evidence/interpretation-fuel/results preflight-macos-arm64 \
//!     tests/assets/real_world/*.pdf

use std::fmt::{self, Write as _};
use std::path::Path;
use std::sync::Arc;
use std::time::Instant;

use pyropdf_engine::bridge::preflight::{validate_page, CheckedCache, Report};
use pyropdf_engine::bridge::Bridged;
use pyropdf_engine::extract::{page_text, TextOptions};
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
    fn open(bytes: Vec<u8>) -> Self {
        let budget = Budget::default();
        let base = FileBase::open(Bytes::new(bytes), &budget).expect("open pdf");
        let document = Document::new(Arc::new(base), None);
        let version = document.current();
        let pages = PageIndex::build(&mut Reads::new(&version, &budget)).expect("pages");
        Self { version, pages }
    }

    fn bridge(&self) -> Arc<Bridged> {
        Arc::new(Bridged::new(&self.version, &self.pages, &Budget::default()).expect("bridge"))
    }
}

fn millis(started: Instant) -> f64 {
    started.elapsed().as_secs_f64() * 1e3
}

struct ByteWriter<'a>(&'a mut Vec<u8>);
impl fmt::Write for ByteWriter<'_> {
    fn write_str(&mut self, s: &str) -> fmt::Result {
        self.0.extend_from_slice(s.as_bytes());
        Ok(())
    }
}

fn build_pdf(objects: &[Vec<u8>]) -> Vec<u8> {
    let mut out = Vec::from(&b"%PDF-1.7\n"[..]);
    let mut offsets = Vec::with_capacity(objects.len());
    for (idx, body) in objects.iter().enumerate() {
        offsets.push(out.len());
        let _ = writeln!(ByteWriter(&mut out), "{} 0 obj", idx + 1);
        out.extend_from_slice(body);
        out.extend_from_slice(b"\nendobj\n");
    }
    let xref = out.len();
    let _ = write!(
        ByteWriter(&mut out),
        "xref\n0 {}\n0000000000 65535 f \n",
        objects.len() + 1
    );
    for offset in offsets {
        let _ = writeln!(ByteWriter(&mut out), "{offset:010} 00000 n ");
    }
    let _ = write!(
        ByteWriter(&mut out),
        "trailer\n<</Size {}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n",
        objects.len() + 1
    );
    out
}

fn one_page(content: &[u8], resources: &[u8], extra: &[Vec<u8>]) -> Vec<u8> {
    let mut cobj = Vec::new();
    let _ = writeln!(ByteWriter(&mut cobj), "<</Length {}>>stream", content.len());
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

fn binary_form_bomb(levels: usize) -> Vec<u8> {
    let mut extra = Vec::with_capacity(levels + 1);
    for level in 0..=levels {
        let obj_num = 5 + level;
        let body = if level < levels {
            format!("/F{} Do /F{} Do", level + 1, level + 1)
        } else {
            "0 0 10 10 re f".to_string()
        };
        let res = if level < levels {
            format!(
                "/Resources<</XObject<</F{} {} 0 R>>>>",
                level + 1,
                obj_num + 1
            )
        } else {
            "/Resources<<>>".to_string()
        };
        let form = format!(
            "<</Type/XObject/Subtype/Form/BBox[0 0 100 100]{res}/Length {}>>stream\n{body}\nendstream",
            body.len()
        );
        extra.push(form.into_bytes());
    }
    one_page(b"/F0 Do", b"<</XObject<</F0 5 0 R>>>>", &extra)
}

fn synthetic_vectors() -> Vec<(&'static str, Vec<u8>)> {
    let ei_flood = {
        let mut s = b"BI /W 1 /H 1 ID ".to_vec();
        for _ in 0..500 {
            s.extend_from_slice(b"\xff EI ");
        }
        s.extend_from_slice(b"EI");
        one_page(&s, b"<<>>", &[])
    };
    let type0_huge = {
        let sh =
            b"<</ShadingType 2/ColorSpace/DeviceGray/Coords[0 0 100 0]/Function 6 0 R>>".to_vec();
        let t0 = b"<</FunctionType 0/Domain[0 1 0 1]/Range[0 1]/Size[2048 2048]/BitsPerSample 1/Length 1>>stream\n\x00\nendstream".to_vec();
        one_page(b"/Sh0 sh", b"<</Shading<</Sh0 5 0 R>>>>", &[sh, t0])
    };
    let type4_20k = {
        let mut prog = b"{".to_vec();
        for _ in 0..10_000 {
            prog.extend_from_slice(b" 1 pop");
        }
        prog.push(b'}');
        let mut func = Vec::new();
        let _ = writeln!(
            ByteWriter(&mut func),
            "<</FunctionType 4/Domain[0 1]/Range[0 1]/Length {}>>stream",
            prog.len()
        );
        func.extend_from_slice(&prog);
        func.extend_from_slice(b"\nendstream");
        let sh =
            b"<</ShadingType 2/ColorSpace/DeviceGray/Coords[0 0 100 0]/Function 6 0 R>>".to_vec();
        one_page(b"/Sh0 sh", b"<</Shading<</Sh0 5 0 R>>>>", &[sh, func])
    };
    let tiling_huge = {
        let cell = b"0 0 1 1 re f";
        let pat = format!(
            "<</Type/Pattern/PatternType 1/PaintType 1/TilingType 1/BBox[0 0 10000 10000]/XStep 1/YStep 1/Resources<<>>/Length {}>>stream\n{}\nendstream",
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
    vec![
        ("form-bomb-20", binary_form_bomb(20)),
        ("inline-ei-flood-500", ei_flood),
        ("type0-size-2048x2048", type0_huge),
        ("type4-20000-ops", type4_20k),
        ("tiling-10000x10000", tiling_huge),
    ]
}

struct PageBench {
    report: Report,
    cold_ms: f64,
    cached_ms: f64,
    render_ms: f64,
    text_ms: f64,
}

fn measure_doc(doc: &OpenedDoc, runs: usize) -> Vec<PageBench> {
    let count = doc.pages.len();
    let limits = DocumentLimits::web();
    let mut results: Vec<PageBench> = (0..count)
        .map(|_| PageBench {
            report: Report::default(),
            cold_ms: f64::INFINITY,
            cached_ms: f64::INFINITY,
            render_ms: f64::INFINITY,
            text_ms: f64::INFINITY,
        })
        .collect();
    let options = Options::new((1.0, 0.0, 0.0, 1.0), false);

    for run in 0..runs {
        for (idx, page) in doc.pages.pages().iter().enumerate() {
            let budget = Budget::new(&limits);
            let mut reads = Reads::new(&doc.version, &budget);
            let started = Instant::now();
            let report = validate_page(&mut reads, page, None).expect("corpus page passes");
            let ms = millis(started);
            if run == 0 {
                results[idx].report = report;
            }
            results[idx].cold_ms = results[idx].cold_ms.min(ms);
        }

        let mut cache = CheckedCache::new();
        for (idx, page) in doc.pages.pages().iter().enumerate() {
            let budget = Budget::new(&limits);
            let mut reads = Reads::new(&doc.version, &budget);
            let started = Instant::now();
            let _ = validate_page(&mut reads, page, Some(&mut cache)).expect("cached passes");
            results[idx].cached_ms = results[idx].cached_ms.min(millis(started));
        }

        let bridged = doc.bridge();
        for (idx, row) in results.iter_mut().enumerate().take(count) {
            let started = Instant::now();
            let raster = render::raster(&bridged, &Fallback::default(), idx, &options);
            let ms = millis(started);
            std::hint::black_box(raster.ok());
            row.render_ms = row.render_ms.min(ms);
        }

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

    let limits = DocumentLimits::web();
    let mut synth_md = String::from(
        "## Synthetic adversarial refusals (best of 5 runs)\n\n| Case | PDF bytes | Refusal time (ms) | Verdict |\n| --- | ---: | ---: | --- |\n",
    );
    let mut synth_json = String::from("  \"synthetic\": [\n");
    let vectors = synthetic_vectors();
    for (idx, (name, bytes)) in vectors.iter().enumerate() {
        let doc = OpenedDoc::open(bytes.clone());
        let mut best_ms = f64::INFINITY;
        let mut verdict = String::new();
        for _ in 0..runs {
            let budget = Budget::new(&limits);
            let mut reads = Reads::new(&doc.version, &budget);
            let started = Instant::now();
            let err = validate_page(&mut reads, &doc.pages.pages()[0], None)
                .expect_err("adversarial vector must refuse");
            let ms = millis(started);
            if ms < best_ms {
                best_ms = ms;
                verdict = err.to_string();
            }
        }
        eprintln!("synthetic {name:24} -> refused in {best_ms:.4} ms ({verdict})");
        let _ = writeln!(
            synth_md,
            "| `{name}` | {} | {best_ms:.4} | `{verdict}` |",
            bytes.len()
        );
        let escaped = verdict.replace('\\', "\\\\").replace('"', "\\\"");
        let _ = writeln!(
            synth_json,
            "    {{\"case\": \"{name}\", \"pdf_bytes\": {}, \"refusal_ms\": {best_ms:.4}, \"verdict\": \"{escaped}\"}}{}",
            bytes.len(),
            if idx + 1 < vectors.len() { "," } else { "" }
        );
    }
    synth_json.push_str("  ],\n");

    let mut md = format!(
        "# Pre-flight guard with DAG fuel, EI cap, and function bounds (`{label}`, best of {runs} runs)\n\n{synth_md}\n## 15-document real-world corpus under `DocumentLimits::web()`\n\nAll 15 documents under `tests/assets/real_world/` passed with **0 false positives**.\n\n| Document | Pages | Render 72 dpi (ms) | Text (ms) | Cold preflight (ms) | Cold / render | Cold / text | Cached preflight (ms) | Max page ops | Total ops | Max EI | Max T0 | Max T4 ops |\n| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |\n"
    );

    let mut all: Vec<(String, usize, PageBench)> = Vec::new();
    let mut tot_render = 0.0;
    let mut tot_text = 0.0;
    let mut tot_cold = 0.0;
    let mut tot_cached = 0.0;
    let mut max_ops = 0u64;
    let mut max_ei = 0usize;
    let mut max_t0 = 0u64;
    let mut max_t4_ops = 0usize;
    let mut tot_ops = 0u64;

    for file in &files {
        let name = Path::new(file)
            .file_stem()
            .expect("stem")
            .to_string_lossy()
            .into_owned();
        let doc = OpenedDoc::open(std::fs::read(file).expect("read pdf"));
        let results = measure_doc(&doc, runs);
        let render: f64 = results.iter().map(|p| p.render_ms).sum();
        let text: f64 = results.iter().map(|p| p.text_ms).sum();
        let cold: f64 = results.iter().map(|p| p.cold_ms).sum();
        let cached: f64 = results.iter().map(|p| p.cached_ms).sum();
        let doc_max_ops = results
            .iter()
            .map(|p| p.report.effective_operators)
            .max()
            .unwrap_or(0);
        let doc_tot_ops: u64 = results
            .iter()
            .map(|p| p.report.effective_operators)
            .fold(0, u64::saturating_add);
        let doc_max_ei = results
            .iter()
            .map(|p| p.report.max_inline_ei_candidates)
            .max()
            .unwrap_or(0);
        let doc_max_t0 = results
            .iter()
            .map(|p| p.report.max_type0_samples)
            .max()
            .unwrap_or(0);
        let doc_max_t4 = results
            .iter()
            .map(|p| p.report.max_type4_operators)
            .max()
            .unwrap_or(0);

        tot_render += render;
        tot_text += text;
        tot_cold += cold;
        tot_cached += cached;
        tot_ops = tot_ops.saturating_add(doc_tot_ops);
        max_ops = max_ops.max(doc_max_ops);
        max_ei = max_ei.max(doc_max_ei);
        max_t0 = max_t0.max(doc_max_t0);
        max_t4_ops = max_t4_ops.max(doc_max_t4);

        let _ = writeln!(
            md,
            "| `{name}` | {} | {render:.2} | {text:.2} | {cold:.2} | {:.2}% | {:.2}% | {cached:.2} | {doc_max_ops} | {doc_tot_ops} | {doc_max_ei} | {doc_max_t0} | {doc_max_t4} |",
            results.len(),
            100.0 * cold / render.max(1e-6),
            100.0 * cold / text.max(1e-6),
        );
        eprintln!(
            "{name:34} {:3}p  render={render:7.2}ms  text={text:6.2}ms  cold={cold:6.2}ms ({:5.2}%)  max_ops={doc_max_ops}",
            results.len(),
            100.0 * cold / render.max(1e-6),
        );
        for (idx, bench) in results.into_iter().enumerate() {
            all.push((name.clone(), idx + 1, bench));
        }
    }

    let _ = writeln!(
        md,
        "| **Corpus ({} docs)** | **{}** | **{tot_render:.2}** | **{tot_text:.2}** | **{tot_cold:.2}** | **{:.2}%** | **{:.2}%** | **{tot_cached:.2}** | **{max_ops}** | **{tot_ops}** | **{max_ei}** | **{max_t0}** | **{max_t4_ops}** |\n",
        files.len(),
        all.len(),
        100.0 * tot_cold / tot_render.max(1e-6),
        100.0 * tot_cold / tot_text.max(1e-6),
    );

    let mut cold_ms: Vec<f64> = all.iter().map(|(_, _, p)| p.cold_ms).collect();
    cold_ms.sort_by(f64::total_cmp);
    let mut cold_ratios: Vec<f64> = all
        .iter()
        .map(|(_, _, p)| p.cold_ms / p.render_ms.max(1e-6))
        .collect();
    cold_ratios.sort_by(f64::total_cmp);
    let n = all.len();
    let _ = writeln!(
        md,
        "## Per-page distribution ({n} pages)\n\n| Metric | Median | 90th percentile | Max |\n| --- | ---: | ---: | ---: |\n| Cold preflight (ms) | {:.3} | {:.3} | {:.3} |\n| Cold / render | {:.2}% | {:.2}% | {:.2}% |\n",
        cold_ms[n / 2],
        cold_ms[(n - 1) * 9 / 10],
        cold_ms[n - 1],
        100.0 * cold_ratios[n / 2],
        100.0 * cold_ratios[(n - 1) * 9 / 10],
        100.0 * cold_ratios[n - 1],
    );

    let mut pages_json = String::from("  \"pages\": [\n");
    for (idx, (doc, page, p)) in all.iter().enumerate() {
        let r = &p.report;
        let _ = writeln!(
            pages_json,
            "    {{\"document\": \"{doc}\", \"page\": {page}, \"render_ms\": {:.4}, \"text_ms\": {:.4}, \"cold_ms\": {:.4}, \"cached_ms\": {:.4}, \"effective_operators\": {}, \"max_ei_candidates\": {}, \"max_type0_samples\": {}, \"max_type4_operators\": {}}}{}",
            p.render_ms,
            p.text_ms,
            p.cold_ms,
            p.cached_ms,
            r.effective_operators,
            r.max_inline_ei_candidates,
            r.max_type0_samples,
            r.max_type4_operators,
            if idx + 1 < all.len() { "," } else { "" }
        );
    }
    pages_json.push_str("  ]\n");

    let json = format!(
        "{{\n  \"label\": \"{label}\",\n  \"runs\": {runs},\n  \"totals\": {{\"documents\": {}, \"pages\": {}, \"render_ms\": {tot_render:.4}, \"text_ms\": {tot_text:.4}, \"cold_ms\": {tot_cold:.4}, \"cached_ms\": {tot_cached:.4}, \"total_operators\": {tot_ops}, \"max_page_operators\": {max_ops}, \"max_ei_candidates\": {max_ei}, \"max_type0_samples\": {max_t0}, \"max_type4_operators\": {max_t4_ops}}},\n{synth_json}{pages_json}}}\n",
        files.len(),
        all.len(),
    );

    std::fs::create_dir_all(&out_dir).expect("create out_dir");
    std::fs::write(Path::new(&out_dir).join(format!("{label}.md")), md).expect("write md");
    std::fs::write(Path::new(&out_dir).join(format!("{label}.json")), json).expect("write json");
}
