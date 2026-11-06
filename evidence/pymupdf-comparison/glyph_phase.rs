//! Count reuse of solid outline fills at their original device-space phase.
//! Run on the two fixed corpus fixtures as described in results/glyph-phase.md.

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::Path;

use hayro::hayro_interpret::font::{Glyph, GlyphRun};
use hayro::hayro_interpret::hayro_cmap::WritingMode;
use hayro::hayro_interpret::{
    BlendMode, ClipPath, Context, Device, DrawMode, DrawProps, Image, ImageDrawProps,
    InterpreterCache, InterpreterSettings, Paint, RenderContent, SoftMask, TransformExt,
    interpret_page,
};
use hayro::hayro_syntax::Pdf;
use hayro::kurbo::{Affine, BezPath, Rect, Shape};

const MAX_ENTRIES: usize = 4096;
const MAX_BYTES: usize = 4 * 1024 * 1024;

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
struct Key {
    glyph: u128,
    transform: [u64; 6],
}

#[derive(Default)]
struct Cache {
    entries: HashMap<Key, usize>,
    fifo: VecDeque<Key>,
    bytes: usize,
}

impl Cache {
    fn visit(&mut self, key: Key, bytes: usize) -> bool {
        if self.entries.contains_key(&key) {
            return true;
        }
        while self.entries.len() == MAX_ENTRIES || self.bytes + bytes > MAX_BYTES {
            let oldest = self.fifo.pop_front().unwrap();
            self.bytes -= self.entries.remove(&oldest).unwrap();
        }
        self.entries.insert(key, bytes);
        self.fifo.push_back(key);
        self.bytes += bytes;
        false
    }
}

#[derive(Default)]
struct Probe {
    document: Cache,
    history: History,
    page: Cache,
    seen: usize,
    eligible: usize,
    page_hits: usize,
    document_hits: usize,
    exact_repeats: usize,
    linear_repeats: usize,
}

// Count repeat opportunities even if a cache would have evicted the entry.
// Refuse larger inputs instead of publishing incomplete upper bounds.
#[derive(Default)]
struct History {
    exact: HashSet<Key>,
    linear: HashSet<Key>,
}

impl History {
    fn visit(&mut self, key: Key) -> (usize, usize) {
        let mut linear = key;
        linear.transform[4..].fill(0);
        let result = (!self.exact.insert(key), !self.linear.insert(linear));
        assert!(self.exact.len() <= 300_000 && self.linear.len() <= 300_000);
        (usize::from(result.0), usize::from(result.1))
    }
}

impl Device<'_> for Probe {
    fn draw_glyph_run(&mut self, run: &GlyphRun<'_, '_>, props: DrawProps<'_>, mode: &DrawMode) {
        self.seen += run.glyphs().len();
        if !matches!(props.paint, Paint::Color(_))
            || !matches!(mode, DrawMode::Fill(_) | DrawMode::FillAndStroke(_, _))
            || run.writing_mode() != WritingMode::Horizontal
            || !run
                .glyphs()
                .iter()
                .all(|g| matches!(&**g, Glyph::Outline(_)))
        {
            return;
        }
        for positioned in run.glyphs() {
            let Glyph::Outline(outline) = &**positioned else {
                unreachable!()
            };
            let [a, b, c, d, x, y] = (props.transform * positioned.transform()).as_coeffs();
            let (fx, fy) = (x - x.floor(), y - y.floor());
            if x.floor() + fx != x || y.floor() + fy != y {
                continue;
            }
            let matrix = [a, b, c, d, fx, fy];
            let bbox = (Affine::new(matrix) * outline.outline()).bounding_box();
            if bbox.is_zero_area() || bbox.width() > 128.0 || bbox.height() > 128.0 {
                continue;
            }
            let width = (bbox.x1.ceil() - bbox.x0.floor()) as usize;
            let height = (bbox.y1.ceil() - bbox.y0.floor()) as usize;
            let bytes = 4 * width * height;
            let key = Key {
                glyph: outline.identifier().cache_key(),
                transform: matrix.map(f64::to_bits),
            };
            self.eligible += 1;
            self.page_hits += usize::from(self.page.visit(key, bytes));
            self.document_hits += usize::from(self.document.visit(key, bytes));
            let (exact, linear) = self.history.visit(key);
            self.exact_repeats += exact;
            self.linear_repeats += linear;
        }
    }

    fn draw_path(&mut self, _: &BezPath, _: DrawProps<'_>, _: &DrawMode) {}
    fn push_clip_path(&mut self, _: &ClipPath) {}
    fn push_transparency_group(&mut self, _: f32, _: Option<SoftMask<'_>>, _: BlendMode) {}
    fn draw_image(&mut self, _: Image<'_, '_>, _: ImageDrawProps<'_>) {}
    fn pop_clip(&mut self) {}
    fn pop_transparency_group(&mut self) {}
}

fn main() {
    println!(
        "document,dpi,page,glyphs,eligible,page_hits,document_hits,exact_repeats,linear_repeats"
    );
    for name in ["bill-hr815", "usrguide"] {
        let path = Path::new("tests/assets/real_world").join(format!("{name}.pdf"));
        let pdf = Pdf::new(std::fs::read(path).unwrap()).unwrap();
        for dpi in [72, 144] {
            let scale = f64::from(dpi) / 72.0;
            let interpreter = InterpreterCache::new();
            let mut document = Cache::default();
            let mut history = History::default();
            for (index, page) in pdf.pages().iter().enumerate() {
                let (w, h) = page.render_dimensions();
                let target = Rect::new(
                    0.,
                    0.,
                    (f64::from(w) * scale + 0.001).floor(),
                    (f64::from(h) * scale + 0.001).floor(),
                );
                let transform = Affine::scale(scale) * page.initial_transform(true).to_kurbo();
                let settings = InterpreterSettings {
                    render_content: RenderContent::all(),
                    ..Default::default()
                };
                let mut ctx = Context::new(transform, target, &interpreter, page.xref(), settings);
                let mut probe = Probe {
                    document,
                    history,
                    ..Default::default()
                };
                interpret_page(page, &mut ctx, &mut probe);
                println!(
                    "{name},{dpi},{index},{},{},{},{},{},{}",
                    probe.seen,
                    probe.eligible,
                    probe.page_hits,
                    probe.document_hits,
                    probe.exact_repeats,
                    probe.linear_repeats
                );
                document = probe.document;
                history = probe.history;
            }
        }
    }
}
