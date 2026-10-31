//! Renders page 1 of the PDF at 1 pixel per point on white, and prints
//! the number of red and of black pixels in the annotation's box.

use hayro::hayro_interpret::InterpreterSettings;
use hayro::hayro_syntax::Pdf;
use hayro::vello_cpu::color::AlphaColor;
use hayro::{PixmapSettings, RenderCache, RenderSettings};

fn main() {
    let path = std::env::args()
        .nth(1)
        .expect("usage: hayro-annotation-state FILE.pdf");
    let pdf = Pdf::new(std::fs::read(&path).expect("readable file")).expect("a PDF");
    let page = &pdf.pages()[0];
    let pixmap = hayro::render(
        page,
        &RenderCache::new(),
        &InterpreterSettings::default(),
        &RenderSettings::default(),
        &PixmapSettings {
            x_scale: 1.0,
            y_scale: 1.0,
            bg_color: AlphaColor::WHITE,
        },
    );
    let width = usize::from(pixmap.width());
    // The page is 200 by 200 points; device y runs down from the top.
    let mut counts = [0usize; 4];
    for y in 140..160 {
        for x in 20..60 {
            let pixel = pixmap.data()[y * width + x];
            let (r, g, b) = (pixel.r, pixel.g, pixel.b);
            if r > 200 && g < 60 && b < 60 {
                counts[0] += 1;
            } else if g > 200 && r < 60 && b < 60 {
                counts[1] += 1;
            } else if b > 200 && r < 60 && g < 60 {
                counts[2] += 1;
            } else if r < 60 && g < 60 && b < 60 {
                counts[3] += 1;
            }
        }
    }
    println!(
        "annotation box: {} red, {} green, {} blue, {} black pixels",
        counts[0], counts[1], counts[2], counts[3]
    );
}
