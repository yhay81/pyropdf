//! Print what hayro reports for every glyph on the first page of each PDF:
//! glyph kind, `as_unicode()`, and (with the `char_code` feature, hayro main)
//! `char_code()`.

use hayro_interpret::font::{Glyph, GlyphRun};
use hayro_interpret::hayro_syntax::Pdf;
use kurbo::{Affine, BezPath, Rect};
use hayro_interpret::{
    BlendMode, ClipPath, Context, Device, DrawMode, DrawProps, Image, ImageDrawProps,
    InterpreterCache, InterpreterSettings, SoftMask, interpret_page,
};

struct Probe;

impl Device<'_> for Probe {
    fn draw_path(&mut self, _: &BezPath, _: DrawProps<'_>, _: &DrawMode) {}
    fn push_clip_path(&mut self, _: &ClipPath) {}
    fn push_transparency_group(&mut self, _: f32, _: Option<SoftMask<'_>>, _: BlendMode) {}
    fn draw_glyph_run(&mut self, run: &GlyphRun<'_, '_>, _: DrawProps<'_>, _: &DrawMode) {
        for glyph in run.glyphs() {
            let kind = match &**glyph {
                Glyph::Outline(_) => "outline",
                Glyph::Type3(_) => "type3",
            };
            #[cfg(feature = "char_code")]
            let code = format!("{}", glyph.char_code());
            #[cfg(not(feature = "char_code"))]
            let code = "n/a".to_string();
            println!("{kind}\tchar_code={code}\tas_unicode={:?}", glyph.as_unicode());
        }
    }
    fn draw_image(&mut self, _: Image<'_, '_>, _: ImageDrawProps<'_>) {}
    fn pop_clip(&mut self) {}
    fn pop_transparency_group(&mut self) {}
}

fn main() {
    for path in std::env::args().skip(1) {
        println!("# {path}");
        let pdf = Pdf::new(std::fs::read(&path).unwrap()).unwrap();
        let cache = InterpreterCache::new();
        let mut context = Context::new(
            Affine::IDENTITY,
            Rect::new(0.0, 0.0, 1.0, 1.0),
            &cache,
            pdf.xref(),
            InterpreterSettings::default(),
        );
        interpret_page(&pdf.pages()[0], &mut context, &mut Probe);
    }
}
