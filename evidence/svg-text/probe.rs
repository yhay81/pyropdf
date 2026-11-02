//! Records what hayro-svg emits for the text of each page (#435).
//!
//! Usage: probe <pdf>... ; prints one line per page: the glyphs that the
//! interpreter passes to a device, by draw mode and by whether
//! `Glyph::as_unicode` maps them, and what hayro-svg writes for the page.
use std::sync::Arc;

use hayro::hayro_interpret::font::GlyphRun;
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
            if glyph.as_unicode().is_some() {
                self.mapped += 1;
            }
        }
    }
    fn draw_image(&mut self, _: Image<'a, '_>, _: ImageDrawProps<'a>) {}
    fn pop_clip(&mut self) {}
    fn pop_transparency_group(&mut self) {}
}

fn main() {
    println!("file\tpage\truns\tvisible\tinvisible\tunicode\tsvg_bytes\tsvg_use\tsvg_text");
    for path in std::env::args().skip(1) {
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
            let svg = hayro_svg::convert(
                page,
                &hayro_svg::RenderCache::new(),
                &settings,
                &hayro_svg::SvgRenderSettings::default(),
            );
            println!(
                "{name}\t{at}\t{}\t{}\t{}\t{}\t{}\t{}\t{}",
                count.runs,
                count.visible,
                count.invisible,
                count.mapped,
                svg.len(),
                svg.matches("<use").count(),
                svg.matches("<text").count(),
            );
        }
    }
}
