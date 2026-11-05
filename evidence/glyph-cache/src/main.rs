//! The benchmark behind issue #640: hayro 0.9's glyph cache
//! (`RenderSettings::glyph_caching`, LaurenzV/hayro#1706) on and off, in
//! release mode, over the corpus.
//!
//! Each page is drawn as `pyropdf_engine::render::draw` draws it for
//! `page.pixmap(dpi=...)`: through the store's bridge, with hayro's default
//! interpreter settings and every annotation, unhinted, on white, and its
//! RGB samples taken. The two variants differ only in `glyph_caching`. The
//! product's own `render::raster`, which renders without the cache, is timed
//! beside them, and its samples must equal those of the replica without the
//! cache.
//!
//! - Time: every page of each document in order, with one `RenderCache` for
//!   the document, as `render::raster` keeps one per thread for the version
//!   that it rendered last. The variants take turns within each run, and a
//!   document's time is the best and the median of the runs.
//! - Memory: a counting allocator measures what the document's pages
//!   allocate at their peak and what the cache still holds after the last
//!   page.
//! - Pixels: at the quality resolutions, each page is drawn both ways and
//!   compared with the same page drawn without the cache at four times the
//!   resolution and averaged over each 4 by 4 block, which approximates the
//!   area that each shape covers in a pixel.
//! - Distinct sizes: a synthetic page of glyphs that each have a size of
//!   their own, which the cache cannot reuse, measures the cost of the
//!   cache where it only adds entries.
//!
//! Usage: `glyph-cache OUT_DIR LABEL FILE...`, with `RUNS`, `DPIS`,
//! `QUALITY_DPIS`, and `DISTINCT_SIZES` in the environment to change the
//! defaults. It writes `OUT_DIR/LABEL.json` and `OUT_DIR/LABEL.md`.

use std::alloc::{GlobalAlloc, Layout, System};
use std::fmt::Write as _;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicIsize, Ordering};
use std::time::Instant;

use hayro::hayro_interpret::{InterpreterSettings, RenderContent, TransformExt};
use hayro::hayro_syntax::Pdf;
use hayro::kurbo::Affine;
use hayro::vello_cpu::color::AlphaColor;
use hayro::vello_cpu::peniko::ImageAlphaType;
use hayro::vello_cpu::{Pixels, Pixmap, RasterizerSettings, RenderContext, Resources, TargetInit};
use hayro::{RenderCache, RenderSettings};
use pyropdf_engine::bridge::Bridged;
use pyropdf_engine::render::{self, Fallback, Options};
use pyropdf_store::budget::Budget;
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::save::FileBase;
use pyropdf_store::value::Bytes;
use pyropdf_store::version::Document;

/// `pyropdf_engine::render`'s tolerance at the edges of a page.
const EDGE_TOLERANCE: f64 = 0.001;
/// The factor of the reference resolution.
const FINE: usize = 4;
/// The channel difference beyond which a pixel counts as far, as
/// `evidence/phase1-parity` counts it.
const FAR: u8 = 16;

// ---------------------------------------------------------------------------
// A counting allocator.

struct Counting;

static COUNTING: AtomicBool = AtomicBool::new(false);
static CURRENT: AtomicIsize = AtomicIsize::new(0);
static PEAK: AtomicIsize = AtomicIsize::new(0);

fn grew(bytes: usize) {
    if COUNTING.load(Ordering::Relaxed) {
        let bytes = bytes as isize;
        let now = CURRENT.fetch_add(bytes, Ordering::Relaxed) + bytes;
        PEAK.fetch_max(now, Ordering::Relaxed);
    }
}

fn shrank(bytes: usize) {
    if COUNTING.load(Ordering::Relaxed) {
        CURRENT.fetch_sub(bytes as isize, Ordering::Relaxed);
    }
}

// SAFETY: every call forwards to `System` with the same arguments.
unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let pointer = unsafe { System.alloc(layout) };
        if !pointer.is_null() {
            grew(layout.size());
        }
        pointer
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        let pointer = unsafe { System.alloc_zeroed(layout) };
        if !pointer.is_null() {
            grew(layout.size());
        }
        pointer
    }

    unsafe fn dealloc(&self, pointer: *mut u8, layout: Layout) {
        unsafe { System.dealloc(pointer, layout) };
        shrank(layout.size());
    }

    unsafe fn realloc(&self, pointer: *mut u8, layout: Layout, size: usize) -> *mut u8 {
        let moved = unsafe { System.realloc(pointer, layout, size) };
        if !moved.is_null() {
            if size >= layout.size() {
                grew(size - layout.size());
            } else {
                shrank(layout.size() - size);
            }
        }
        moved
    }
}

#[global_allocator]
static ALLOCATOR: Counting = Counting;

/// Counts from zero what is allocated from now on.
fn start_counting() {
    CURRENT.store(0, Ordering::SeqCst);
    PEAK.store(0, Ordering::SeqCst);
    COUNTING.store(true, Ordering::SeqCst);
}

/// The bytes allocated and not freed since `start_counting`, and their peak.
fn counted() -> (isize, isize) {
    (CURRENT.load(Ordering::SeqCst), PEAK.load(Ordering::SeqCst))
}

fn stop_counting() {
    COUNTING.store(false, Ordering::SeqCst);
}

// ---------------------------------------------------------------------------
// Drawing.

struct Raster {
    width: usize,
    height: usize,
    rgb: Vec<u8>,
}

/// Draws page `index` at `scale` pixels per point as `render::draw` draws
/// it, with the glyph cache on or off.
fn draw<'a>(
    pdf: &'a Pdf,
    cache: &RenderCache<'a>,
    index: usize,
    scale: f64,
    caching: bool,
) -> Raster {
    let pages = pdf.pages();
    let page = pages.get(index).expect("the page exists");
    let (width, height) = page.render_dimensions();
    let width = (f64::from(width) * scale + EDGE_TOLERANCE).floor() as u16;
    let height = (f64::from(height) * scale + EDGE_TOLERANCE).floor() as u16;
    let transform =
        Affine::new([scale, 0.0, 0.0, scale, 0.0, 0.0]) * page.initial_transform(true).to_kurbo();
    let settings = InterpreterSettings {
        render_content: RenderContent::all(),
        ..InterpreterSettings::default()
    };
    let render_settings = RenderSettings {
        hinting: false,
        glyph_caching: caching,
        ..RenderSettings::default()
    };
    let mut context = RenderContext::new(width, height);
    hayro::render_into(
        page,
        cache,
        &settings,
        &render_settings,
        &mut context,
        transform,
    );
    context.flush();
    let mut pixmap = Pixmap::new(width, height);
    context.render_with(
        &mut pixmap,
        &mut Resources::default(),
        RasterizerSettings {
            target_init: TargetInit::Clear(AlphaColor::from_rgba8(255, 255, 255, 255)),
            ..Default::default()
        },
    );
    let rgb = match pixmap.try_take_rgb8(ImageAlphaType::Alpha) {
        Pixels::Rgb8(data) => data,
        Pixels::Rgba8(data) => data
            .chunks_exact(4)
            .flat_map(|pixel| [pixel[0], pixel[1], pixel[2]])
            .collect(),
    };
    Raster {
        width: usize::from(width),
        height: usize::from(height),
        rgb,
    }
}

/// Draws page `index` through the product's `render::raster`.
fn product(bridged: &Arc<Bridged>, index: usize, scale: f64) -> Vec<u8> {
    let options = Options::new((scale, 0.0, 0.0, scale), false);
    render::raster(bridged, &Fallback::default(), index, &options)
        .expect("the page renders")
        .data
}

// ---------------------------------------------------------------------------
// Documents.

struct Corpus {
    name: String,
    bridged: Arc<Bridged>,
    version: Arc<pyropdf_store::version::Version>,
    pages: PageIndex,
}

fn open(name: String, bytes: Vec<u8>) -> Corpus {
    let budget = Budget::default();
    let base = FileBase::open(Bytes::new(bytes), &budget).expect("the store opens the file");
    let document = Document::new(Arc::new(base), None);
    let version = document.current();
    let pages = PageIndex::build(&mut Reads::new(&version, &budget)).expect("pages");
    let bridged = Arc::new(Bridged::new(&version, &pages, &budget).expect("bridge"));
    Corpus {
        name,
        bridged,
        version,
        pages,
    }
}

impl Corpus {
    fn count(&self) -> usize {
        self.bridged.pdf().pages().len()
    }

    /// A fresh bridge of the same version, so that nothing hayro memoizes
    /// in its document survives from an earlier pass.
    fn fresh(&self) -> Arc<Bridged> {
        Arc::new(Bridged::new(&self.version, &self.pages, &Budget::default()).expect("bridge"))
    }
}

/// A one-page PDF of `content` in Helvetica, named `/F1`.
fn single_page(content: &str) -> Vec<u8> {
    let objects = [
        "<< /Type /Catalog /Pages 2 0 R >>".to_owned(),
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>".to_owned(),
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]          /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
            .to_owned(),
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>".to_owned(),
        format!(
            "<< /Length {} >>
stream
{content}endstream",
            content.len()
        ),
    ];
    let mut pdf = b"%PDF-1.7
"
    .to_vec();
    let mut offsets = Vec::new();
    for (at, object) in objects.iter().enumerate() {
        offsets.push(pdf.len());
        pdf.extend_from_slice(
            format!(
                "{} 0 obj
{object}
endobj
",
                at + 1
            )
            .as_bytes(),
        );
    }
    let xref = pdf.len();
    let mut table = format!(
        "xref
0 {}
0000000000 65535 f 
",
        objects.len() + 1
    );
    for offset in offsets {
        let _ = write!(
            table,
            "{offset:010} 00000 n 
"
        );
    }
    let _ = write!(
        table,
        "trailer
<< /Size {} /Root 1 0 R >>
startxref
{xref}
%%EOF
",
        objects.len() + 1
    );
    pdf.extend_from_slice(table.as_bytes());
    pdf
}

/// A page of `count` glyphs, each `W` of Helvetica at a size of its own
/// between 20 and 120 points, so that no glyph can reuse a cached bitmap.
fn distinct_sizes(count: usize) -> Vec<u8> {
    let mut content = String::from(
        "BT /F1 1 Tf
",
    );
    for at in 0..count {
        let size = 20.0 + 100.0 * at as f64 / count as f64;
        let x = 10 + (at % 50) * 11;
        let y = 10 + (at / 50 % 60) * 12;
        let _ = writeln!(content, "{size:.5} 0 0 {size:.5} {x} {y} Tm (W) Tj");
    }
    content.push_str(
        "ET
",
    );
    single_page(&content)
}

/// A page of 2,000 glyphs `e` of Helvetica at 10 points, whose origins lie
/// `dx` and `dy` points past a whole point; at 72 dpi a point is a pixel.
/// Successive columns step a further quarter of a point.
fn positions(dx: f64, dy: f64) -> Vec<u8> {
    let mut content = String::from(
        "BT /F1 10 Tf
",
    );
    for row in 0..50 {
        for column in 0..40 {
            let x = 20.0 + 14.0 * f64::from(column) + 0.25 * f64::from(column % 4) + dx;
            let y = 20.0 + 15.0 * f64::from(row) + dy;
            let _ = writeln!(content, "1 0 0 1 {x:.4} {y:.4} Tm (e) Tj");
        }
    }
    content.push_str(
        "ET
",
    );
    single_page(&content)
}

// ---------------------------------------------------------------------------
// Measurements.

#[derive(Clone, Copy, PartialEq, Eq)]
enum Variant {
    Off,
    On,
    Raster,
}

impl Variant {
    const ALL: [Self; 3] = [Self::Off, Self::On, Self::Raster];
}

/// Renders every page of `corpus` once and returns the milliseconds.
fn time_pass(corpus: &Corpus, scale: f64, variant: Variant) -> f64 {
    match variant {
        Variant::Raster => {
            // A bridge of its own, so that the thread's cache starts empty.
            let bridged = corpus.fresh();
            let started = Instant::now();
            for index in 0..corpus.count() {
                std::hint::black_box(product(&bridged, index, scale));
            }
            started.elapsed().as_secs_f64() * 1e3
        }
        Variant::Off | Variant::On => {
            let bridged = corpus.fresh();
            let cache = RenderCache::new();
            let started = Instant::now();
            for index in 0..corpus.count() {
                std::hint::black_box(draw(
                    bridged.pdf(),
                    &cache,
                    index,
                    scale,
                    variant == Variant::On,
                ));
            }
            started.elapsed().as_secs_f64() * 1e3
        }
    }
}

struct Memory {
    peak: isize,
    retained: isize,
}

/// Renders every page of `corpus` once while counting what it allocates.
fn memory_pass(corpus: &Corpus, scale: f64, caching: bool) -> Memory {
    let bridged = corpus.fresh();
    start_counting();
    let cache = RenderCache::new();
    for index in 0..corpus.count() {
        drop(draw(bridged.pdf(), &cache, index, scale, caching));
    }
    let (retained, peak) = counted();
    drop(cache);
    stop_counting();
    Memory { peak, retained }
}

#[derive(Default, Clone)]
struct Pixels2 {
    /// Pages whose two rasters are byte for byte equal.
    equal: usize,
    /// The largest share of a page's pixels beyond `FAR` in a channel
    /// between the two variants.
    worst_far: f64,
    /// Pixels beyond `FAR` between the variants, over all pages.
    far: u64,
    /// Pixels over all pages.
    pixels: u64,
    /// Pixels that differ at all between the variants.
    changed: u64,
    /// Sums of absolute channel errors against the reference, over all
    /// pixels and over the changed ones, for off and on.
    error_off: f64,
    error_on: f64,
    changed_error_off: f64,
    changed_error_on: f64,
    /// Pixels beyond `FAR` against the reference, for off and on.
    far_off: u64,
    far_on: u64,
    /// The worst page's mean channel error against the reference, for off
    /// and on, and that page.
    worst_page_error: (f64, f64, usize),
    /// Pages whose samples from `render::raster` differ from the replica's
    /// without the cache.
    product_mismatches: usize,
}

/// The reference: `fine` averaged over each `FINE` by `FINE` block.
fn reference(fine: &Raster, width: usize, height: usize) -> Vec<f32> {
    let mut out = vec![0.0f32; width * height * 3];
    for y in 0..height {
        for x in 0..width {
            for channel in 0..3 {
                let mut sum = 0u32;
                for dy in 0..FINE {
                    let row = (FINE * y + dy) * fine.width;
                    for dx in 0..FINE {
                        sum += u32::from(fine.rgb[(row + FINE * x + dx) * 3 + channel]);
                    }
                }
                out[(y * width + x) * 3 + channel] = sum as f32 / (FINE * FINE) as f32;
            }
        }
    }
    out
}

fn compare_pass(corpus: &Corpus, scale: f64) -> Pixels2 {
    let bridged = corpus.fresh();
    let off_cache = RenderCache::new();
    let on_cache = RenderCache::new();
    let fine_cache = RenderCache::new();
    let mut result = Pixels2::default();
    for index in 0..corpus.count() {
        let off = draw(bridged.pdf(), &off_cache, index, scale, false);
        let on = draw(bridged.pdf(), &on_cache, index, scale, true);
        if product(&bridged, index, scale) != off.rgb {
            result.product_mismatches += 1;
        }
        let fine = draw(
            bridged.pdf(),
            &fine_cache,
            index,
            scale * FINE as f64,
            false,
        );
        assert!(fine.width >= FINE * off.width && fine.height >= FINE * off.height);
        let truth = reference(&fine, off.width, off.height);
        let pixels = (off.width * off.height) as u64;
        let mut far = 0u64;
        let (mut error_off, mut error_on) = (0.0f64, 0.0f64);
        for pixel in 0..off.width * off.height {
            let a = &off.rgb[pixel * 3..pixel * 3 + 3];
            let b = &on.rgb[pixel * 3..pixel * 3 + 3];
            let t = &truth[pixel * 3..pixel * 3 + 3];
            let delta = (0..3).map(|c| a[c].abs_diff(b[c])).max().unwrap_or(0);
            if delta > FAR {
                far += 1;
            }
            let mut pixel_off = 0.0f64;
            let mut pixel_on = 0.0f64;
            let mut far_off = false;
            let mut far_on = false;
            for c in 0..3 {
                let e_off = (f64::from(a[c]) - f64::from(t[c])).abs();
                let e_on = (f64::from(b[c]) - f64::from(t[c])).abs();
                pixel_off += e_off;
                pixel_on += e_on;
                far_off |= e_off > f64::from(FAR);
                far_on |= e_on > f64::from(FAR);
            }
            error_off += pixel_off;
            error_on += pixel_on;
            result.far_off += u64::from(far_off);
            result.far_on += u64::from(far_on);
            if delta > 0 {
                result.changed += 1;
                result.changed_error_off += pixel_off;
                result.changed_error_on += pixel_on;
            }
        }
        if off.rgb == on.rgb {
            result.equal += 1;
        }
        result.far += far;
        result.pixels += pixels;
        result.worst_far = result.worst_far.max(far as f64 / pixels as f64);
        result.error_off += error_off;
        result.error_on += error_on;
        let page_off = error_off / (pixels * 3) as f64;
        let page_on = error_on / (pixels * 3) as f64;
        if page_on - page_off > result.worst_page_error.1 - result.worst_page_error.0 {
            result.worst_page_error = (page_off, page_on, index + 1);
        }
    }
    result
}

fn best(values: &[f64]) -> f64 {
    values.iter().copied().fold(f64::INFINITY, f64::min)
}

fn median(values: &[f64]) -> f64 {
    let mut sorted = values.to_vec();
    sorted.sort_by(f64::total_cmp);
    let middle = sorted.len() / 2;
    if sorted.len() % 2 == 0 {
        f64::midpoint(sorted[middle - 1], sorted[middle])
    } else {
        sorted[middle]
    }
}

fn env_list(name: &str, default: &[f64]) -> Vec<f64> {
    std::env::var(name).map_or_else(
        |_| default.to_vec(),
        |value| {
            value
                .split(',')
                .filter(|item| !item.is_empty())
                .map(|item| item.trim().parse().expect("a number"))
                .collect()
        },
    )
}

fn mib(bytes: isize) -> f64 {
    bytes as f64 / (1024.0 * 1024.0)
}

fn main() {
    let mut args = std::env::args().skip(1);
    let out = args.next().expect("OUT_DIR");
    let label = args.next().expect("LABEL");
    let files: Vec<String> = args.collect();
    let runs = std::env::var("RUNS").map_or(5, |value| value.parse().expect("RUNS"));
    let dpis = env_list("DPIS", &[72.0, 144.0, 300.0]);
    let quality_dpis = env_list("QUALITY_DPIS", &[72.0, 144.0]);
    let distinct = std::env::var("DISTINCT_SIZES")
        .map_or(20_000, |value| value.parse().expect("DISTINCT_SIZES"));

    let corpus: Vec<Corpus> = files
        .iter()
        .map(|file| {
            let name = Path::new(file)
                .file_stem()
                .expect("a file name")
                .to_string_lossy()
                .into_owned();
            open(name, std::fs::read(file).expect("the file reads"))
        })
        .collect();
    let pages: usize = corpus.iter().map(Corpus::count).sum();
    eprintln!("{} documents, {pages} pages", corpus.len());

    let mut json = String::new();
    let mut md = String::new();
    let _ = write!(
        json,
        "{{\n  \"label\": \"{label}\",\n  \"runs\": {runs},\n  \"documents\": {},\n  \"pages\": {pages},\n",
        corpus.len()
    );

    // Time.
    let _ = writeln!(json, "  \"time_ms\": {{");
    let _ = writeln!(
        md,
        "## Time\n\nEvery page of each document in order, best of {runs} runs, in milliseconds.\n"
    );
    for (dpi_at, &dpi) in dpis.iter().enumerate() {
        let scale = dpi / 72.0;
        // times[document][variant] = one value per run.
        let mut times = vec![[Vec::new(), Vec::new(), Vec::new()]; corpus.len()];
        for run in 0..runs {
            for (doc_at, doc) in corpus.iter().enumerate() {
                for turn in 0..Variant::ALL.len() {
                    let variant = Variant::ALL[(turn + run) % Variant::ALL.len()];
                    let ms = time_pass(doc, scale, variant);
                    let slot = Variant::ALL.iter().position(|v| *v == variant).unwrap();
                    times[doc_at][slot].push(ms);
                }
            }
            eprintln!("{dpi} dpi: run {} of {runs}", run + 1);
        }
        let _ = writeln!(
            md,
            "### {dpi} dpi\n\n| Document | Pages | Off | On | Off / on | Off, median | On, median | `render::raster` |\n| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
        );
        let _ = writeln!(json, "    \"{dpi}\": [");
        let mut totals = [0.0f64; 3];
        let mut median_totals = [0.0f64; 3];
        for (doc_at, doc) in corpus.iter().enumerate() {
            let [off, on, raster] = &times[doc_at];
            let (b_off, b_on, b_raster) = (best(off), best(on), best(raster));
            let (m_off, m_on, m_raster) = (median(off), median(on), median(raster));
            totals[0] += b_off;
            totals[1] += b_on;
            totals[2] += b_raster;
            median_totals[0] += m_off;
            median_totals[1] += m_on;
            median_totals[2] += m_raster;
            let _ = writeln!(
                md,
                "| {} | {} | {b_off:.1} | {b_on:.1} | {:.2} | {m_off:.1} | {m_on:.1} | {b_raster:.1} |",
                doc.name,
                doc.count(),
                b_off / b_on
            );
            let list = |values: &[f64]| {
                values
                    .iter()
                    .map(|value| format!("{value:.3}"))
                    .collect::<Vec<_>>()
                    .join(", ")
            };
            let _ = writeln!(
                json,
                "      {{\"document\": \"{}\", \"pages\": {}, \"off\": [{}], \"on\": [{}], \"raster\": [{}]}}{}",
                doc.name,
                doc.count(),
                list(off),
                list(on),
                list(raster),
                if doc_at + 1 < corpus.len() { "," } else { "" }
            );
        }
        let _ = writeln!(
            md,
            "| **Corpus** | {pages} | **{:.1}** | **{:.1}** | **{:.2}** | {:.1} | {:.1} | {:.1} |\n",
            totals[0],
            totals[1],
            totals[0] / totals[1],
            median_totals[0],
            median_totals[1],
            totals[2]
        );
        let _ = writeln!(
            json,
            "    ]{}",
            if dpi_at + 1 < dpis.len() { "," } else { "" }
        );
    }
    let _ = writeln!(json, "  }},");

    // Memory.
    let _ = writeln!(
        md,
        "## Memory\n\nWhat every page of a document allocates at its peak, and what one `RenderCache` still holds after its last page, in MiB.\n"
    );
    let _ = writeln!(json, "  \"memory_bytes\": {{");
    for (dpi_at, &dpi) in dpis.iter().enumerate() {
        let scale = dpi / 72.0;
        let _ = writeln!(
            md,
            "### {dpi} dpi\n\n| Document | Peak, off | Peak, on | Held, off | Held, on |\n| --- | ---: | ---: | ---: | ---: |"
        );
        let _ = writeln!(json, "    \"{dpi}\": [");
        let mut held = [0isize; 2];
        for (doc_at, doc) in corpus.iter().enumerate() {
            let off = memory_pass(doc, scale, false);
            let on = memory_pass(doc, scale, true);
            held[0] += off.retained;
            held[1] += on.retained;
            let _ = writeln!(
                md,
                "| {} | {:.1} | {:.1} | {:.1} | {:.1} |",
                doc.name,
                mib(off.peak),
                mib(on.peak),
                mib(off.retained),
                mib(on.retained)
            );
            let _ = writeln!(
                json,
                "      {{\"document\": \"{}\", \"peak_off\": {}, \"peak_on\": {}, \"held_off\": {}, \"held_on\": {}}}{}",
                doc.name,
                off.peak,
                on.peak,
                off.retained,
                on.retained,
                if doc_at + 1 < corpus.len() { "," } else { "" }
            );
        }
        let _ = writeln!(
            md,
            "| **Corpus, held** | | | **{:.1}** | **{:.1}** |\n",
            mib(held[0]),
            mib(held[1])
        );
        let _ = writeln!(
            json,
            "    ]{}",
            if dpi_at + 1 < dpis.len() { "," } else { "" }
        );
        eprintln!("{dpi} dpi: memory");
    }
    let _ = writeln!(json, "  }},");

    // Pixels.
    let _ = writeln!(
        md,
        "## Pixels\n\nEach page drawn both ways, and drawn without the cache at {FINE} times the resolution and averaged over each {FINE} by {FINE} block as the reference. A pixel is far when a channel differs by more than {FAR}. The errors are mean absolute channel errors against the reference, over all pixels and over the pixels that the cache changes.\n"
    );
    let _ = writeln!(json, "  \"pixels\": {{");
    for (dpi_at, &dpi) in quality_dpis.iter().enumerate() {
        let scale = dpi / 72.0;
        let _ = writeln!(
            md,
            "### {dpi} dpi\n\n| Document | Pages | Equal | Changed pixels | Far, on against off | Worst page far | Error, off | Error, on | Changed error, off | Changed error, on | Far from reference, off | Far from reference, on |\n| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
        );
        let _ = writeln!(json, "    \"{dpi}\": [");
        let mut all = Pixels2::default();
        let mut mismatches = 0;
        for (doc_at, doc) in corpus.iter().enumerate() {
            let result = compare_pass(doc, scale);
            mismatches += result.product_mismatches;
            let share = |count: u64| 100.0 * count as f64 / result.pixels as f64;
            let channels = (result.pixels * 3) as f64;
            let changed = (result.changed.max(1) * 3) as f64;
            let _ = writeln!(
                md,
                "| {} | {} | {} | {:.2}% | {:.3}% | {:.2}% | {:.3} | {:.3} | {:.2} | {:.2} | {:.3}% | {:.3}% |",
                doc.name,
                doc.count(),
                result.equal,
                share(result.changed),
                share(result.far),
                100.0 * result.worst_far,
                result.error_off / channels,
                result.error_on / channels,
                result.changed_error_off / changed,
                result.changed_error_on / changed,
                share(result.far_off),
                share(result.far_on)
            );
            let _ = writeln!(
                json,
                "      {{\"document\": \"{}\", \"pages\": {}, \"equal\": {}, \"pixels\": {}, \"changed\": {}, \"far\": {}, \"worst_far\": {:.6}, \"error_off\": {:.3}, \"error_on\": {:.3}, \"changed_error_off\": {:.3}, \"changed_error_on\": {:.3}, \"far_off\": {}, \"far_on\": {}, \"worst_page\": {}, \"worst_page_error_off\": {:.4}, \"worst_page_error_on\": {:.4}, \"product_mismatches\": {}}}{}",
                doc.name,
                doc.count(),
                result.equal,
                result.pixels,
                result.changed,
                result.far,
                result.worst_far,
                result.error_off,
                result.error_on,
                result.changed_error_off,
                result.changed_error_on,
                result.far_off,
                result.far_on,
                result.worst_page_error.2,
                result.worst_page_error.0,
                result.worst_page_error.1,
                result.product_mismatches,
                if doc_at + 1 < corpus.len() { "," } else { "" }
            );
            all.equal += result.equal;
            all.pixels += result.pixels;
            all.changed += result.changed;
            all.far += result.far;
            all.worst_far = all.worst_far.max(result.worst_far);
            all.error_off += result.error_off;
            all.error_on += result.error_on;
            all.changed_error_off += result.changed_error_off;
            all.changed_error_on += result.changed_error_on;
            all.far_off += result.far_off;
            all.far_on += result.far_on;
        }
        let share = |count: u64| 100.0 * count as f64 / all.pixels as f64;
        let channels = (all.pixels * 3) as f64;
        let changed = (all.changed.max(1) * 3) as f64;
        let _ = writeln!(
            md,
            "| **Corpus** | {pages} | **{}** | {:.2}% | {:.3}% | {:.2}% | **{:.3}** | **{:.3}** | **{:.2}** | **{:.2}** | {:.3}% | {:.3}% |\n\n`render::raster` and the replica without the cache differ on {mismatches} pages.\n",
            all.equal,
            share(all.changed),
            share(all.far),
            100.0 * all.worst_far,
            all.error_off / channels,
            all.error_on / channels,
            all.changed_error_off / changed,
            all.changed_error_on / changed,
            share(all.far_off),
            share(all.far_on)
        );
        let _ = writeln!(
            json,
            "    ]{}",
            if dpi_at + 1 < quality_dpis.len() {
                ","
            } else {
                ""
            }
        );
        eprintln!("{dpi} dpi: pixels");
    }
    let _ = writeln!(json, "  }},");

    // Distinct sizes.
    let synthetic = open("distinct-sizes".to_owned(), distinct_sizes(distinct));
    let mut off = Vec::new();
    let mut on = Vec::new();
    for run in 0..runs {
        if run % 2 == 0 {
            off.push(time_pass(&synthetic, 1.0, Variant::Off));
            on.push(time_pass(&synthetic, 1.0, Variant::On));
        } else {
            on.push(time_pass(&synthetic, 1.0, Variant::On));
            off.push(time_pass(&synthetic, 1.0, Variant::Off));
        }
    }
    let memory_off = memory_pass(&synthetic, 1.0, false);
    let memory_on = memory_pass(&synthetic, 1.0, true);
    let _ = writeln!(
        md,
        "## Distinct sizes\n\nOne page of {distinct} glyphs, `W` of Helvetica each at a size of its own between 20 and 120 points, at 72 dpi.\n\n| | Off | On |\n| --- | ---: | ---: |\n| Time, best of {runs} (ms) | {:.1} | {:.1} |\n| Peak (MiB) | {:.1} | {:.1} |\n| Held after the page (MiB) | {:.1} | {:.1} |",
        best(&off),
        best(&on),
        mib(memory_off.peak),
        mib(memory_on.peak),
        mib(memory_off.retained),
        mib(memory_on.retained)
    );
    let _ = writeln!(
        json,
        "  \"distinct_sizes\": {{\"glyphs\": {distinct}, \"off_ms\": {:.3}, \"on_ms\": {:.3}, \"peak_off\": {}, \"peak_on\": {}, \"held_off\": {}, \"held_on\": {}}},",
        best(&off),
        best(&on),
        memory_off.peak,
        memory_on.peak,
        memory_off.retained,
        memory_on.retained
    );

    // Positions.
    let _ = writeln!(
        md,
        "\n## Positions\n\nOne page of 2,000 glyphs `e` of Helvetica at 10 points, at 72 dpi, where a point is a pixel. Each column steps a further quarter of a pixel. Aligned: every origin on a whole pixel vertically and a quarter of a pixel horizontally, where the cache places glyphs. Offset: the same, an eighth of a pixel right and half a pixel up.\n\n| Page | Changed pixels | Far, on against off | Error, off | Error, on | Changed error, off | Changed error, on |\n| --- | ---: | ---: | ---: | ---: | ---: | ---: |"
    );
    let _ = writeln!(json, "  \"positions\": [");
    for (at, (name, dx, dy)) in [("aligned", 0.0, 0.0), ("offset", 0.125, 0.5)]
        .into_iter()
        .enumerate()
    {
        let page = open(name.to_owned(), positions(dx, dy));
        let result = compare_pass(&page, 1.0);
        let share = |count: u64| 100.0 * count as f64 / result.pixels as f64;
        let channels = (result.pixels * 3) as f64;
        let changed = (result.changed.max(1) * 3) as f64;
        let _ = writeln!(
            md,
            "| {name} | {:.3}% | {:.3}% | {:.3} | {:.3} | {:.2} | {:.2} |",
            share(result.changed),
            share(result.far),
            result.error_off / channels,
            result.error_on / channels,
            result.changed_error_off / changed,
            result.changed_error_on / changed
        );
        let _ = writeln!(
            json,
            "    {{\"page\": \"{name}\", \"pixels\": {}, \"changed\": {}, \"far\": {}, \"error_off\": {:.3}, \"error_on\": {:.3}, \"changed_error_off\": {:.3}, \"changed_error_on\": {:.3}}}{}",
            result.pixels,
            result.changed,
            result.far,
            result.error_off,
            result.error_on,
            result.changed_error_off,
            result.changed_error_on,
            if at == 0 { "," } else { "" }
        );
    }
    let _ = writeln!(json, "  ]\n}}");

    std::fs::create_dir_all(&out).expect("the output folder");
    std::fs::write(Path::new(&out).join(format!("{label}.json")), json).expect("json");
    std::fs::write(Path::new(&out).join(format!("{label}.md")), md).expect("markdown");
}
