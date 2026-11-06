//! Render a fixture with upstream hayro, outside pyropdf's patched dependency graph.

use hayro::hayro_interpret::InterpreterSettings;
use hayro::hayro_syntax::Pdf;
use hayro::vello_cpu::color::palette::css::WHITE;
use hayro::{PixmapSettings, RenderCache, RenderSettings, render};
use std::hint::black_box;
use std::time::Instant;

fn main() {
    let args: Vec<_> = std::env::args().collect();
    assert_eq!(
        args.len(),
        5,
        "usage: probe INPUT.pdf OUTPUT.png REPEATS SCALE"
    );
    let data = std::fs::read(&args[1]).expect("read PDF");
    let repeats: usize = args[3].parse().expect("positive repeat count");
    assert!(repeats > 0);
    let scale: f32 = args[4].parse().expect("positive scale");
    assert!(scale.is_finite() && scale > 0.0);
    let mut samples = Vec::with_capacity(repeats);
    let mut last = None;
    // One untimed warmup; each iteration has a fresh document and render cache.
    for iteration in 0..=repeats {
        let start = Instant::now();
        let pdf = Pdf::new(data.clone()).expect("parse PDF");
        let pixmap = render(
            &pdf.pages()[0],
            &RenderCache::new(),
            &InterpreterSettings::default(),
            &RenderSettings::default(),
            &PixmapSettings {
                x_scale: scale,
                y_scale: scale,
                bg_color: WHITE,
            },
        );
        black_box(&pixmap);
        let elapsed = start.elapsed().as_secs_f64();
        if iteration > 0 {
            samples.push(elapsed);
        }
        last = Some(pixmap);
    }
    let pixmap = last.expect("rendered at least once");
    std::fs::write(&args[2], pixmap.into_png().expect("encode PNG")).expect("write PNG");
    println!("{{\"seconds\": {samples:?}}}");
}
