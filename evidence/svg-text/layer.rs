//! Measures the text layer of the draft hayro-svg patch (#435).
//!
//! Usage: layer <out-dir> <pdf>... ; built against hayro-svg 0.8.0 with
//! `draft.patch` applied. For each page it writes the SVG with the layer
//! and the text that `Glyph::as_unicode` gives for its glyphs, which
//! `check_layer.py` compares after parsing the SVG, and prints the size
//! of the SVG without and with the layer.
use std::sync::Arc;

use hayro::hayro_interpret::font::GlyphRun;
use hayro::hayro_interpret::hayro_cmap::BfString;
use hayro::hayro_interpret::{
    BlendMode, ClipPath, Context, Device, DrawMode, DrawProps, Image, ImageDrawProps,
    InterpreterCache, InterpreterSettings, SoftMask, TransformExt, interpret_page,
};
use hayro::hayro_syntax::Pdf;
use kurbo::{BezPath, Rect};

#[derive(Default)]
struct Count {
    visible: usize,
    invisible: usize,
    mapped: usize,
    runs: usize,
    text: String,
}

impl<'a> Device<'a> for Count {
    fn draw_path(&mut self, _: &BezPath, _: DrawProps<'a>, _: &DrawMode) {}
    fn push_clip_path(&mut self, _: &ClipPath) {}
    fn push_transparency_group(&mut self, _: f32, _: Option<SoftMask<'a>>, _: BlendMode) {}
    fn draw_glyph_run(&mut self, run: &GlyphRun<'_, 'a>, _: DrawProps<'a>, mode: &DrawMode) {
        self.runs += 1;
        for glyph in run.glyphs() {
            match mode {
                DrawMode::Invisible => self.invisible += 1,
                _ => self.visible += 1,
            }
            if let Some(unicode) = glyph.as_unicode() {
                self.mapped += 1;
                let text = match unicode {
                    BfString::Char(c) => c.to_string(),
                    BfString::String(s) => s,
                };
                self.text.push_str(&text);
            }
        }
    }
    fn draw_image(&mut self, _: Image<'a, '_>, _: ImageDrawProps<'a>) {}
    fn pop_clip(&mut self) {}
    fn pop_transparency_group(&mut self) {}
}

fn main() {
    let out = std::path::PathBuf::from(std::env::args().nth(1).unwrap());
    println!("file\tpage\tglyphs\tunicode\tsvg_bytes\tlayer_bytes\ttext_elements");
    for path in std::env::args().skip(2) {
        let pdf = Pdf::new(Arc::new(std::fs::read(&path).unwrap())).unwrap();
        let name = path.rsplit('/').next().unwrap();
        for (at, page) in pdf.pages().iter().enumerate() {
            let settings = InterpreterSettings::default();
            let cache = InterpreterCache::new();
            let (w, h) = page.render_dimensions();
            let mut context = Context::new(
                page.initial_transform(true).to_kurbo(),
                Rect::new(0.0, 0.0, w as f64, h as f64),
                &cache,
                page.xref(),
                settings.clone(),
            );
            let mut count = Count::default();
            interpret_page(page, &mut context, &mut count);
            let convert = |text_layer| {
                let render = hayro_svg::SvgRenderSettings {
                    text_layer,
                    ..Default::default()
                };
                hayro_svg::convert(page, &hayro_svg::RenderCache::new(), &settings, &render)
            };
            let (plain, layered) = (convert(false), convert(true));
            let stem = out.join(format!("{}-{at}", name.trim_end_matches(".pdf")));
            std::fs::write(stem.with_extension("svg"), &layered).unwrap();
            std::fs::write(stem.with_extension("txt"), &count.text).unwrap();
            println!(
                "{name}\t{at}\t{}\t{}\t{}\t{}\t{}",
                count.visible + count.invisible,
                count.mapped,
                plain.len(),
                layered.len(),
                layered.matches("<text").count(),
            );
        }
    }
}
