//! Renders pairs of documents at 72 dpi, hinted and unhinted, and prints for
//! each page how many pixels differ between the two documents of a pair and
//! by how much.
//!
//! Usage: `hinted-subsets NAME BEFORE AFTER [NAME BEFORE AFTER]...`, which
//! prints a tab-separated line per pair, page, and hinting setting.

use std::env;
use std::fs;

use hayro::hayro_interpret::InterpreterSettings;
use hayro::hayro_syntax::Pdf;
use hayro::vello_cpu::color::AlphaColor;
use hayro::{PixmapSettings, RenderCache, RenderSettings};

/// The RGBA samples of every page of `pdf`, on white, as pyropdf renders
/// `page.pixmap(dpi=72)` with or without hinting.
fn render(pdf: &Pdf, hinting: bool) -> Vec<(u16, u16, Vec<u8>)> {
    let settings = RenderSettings {
        hinting,
        ..RenderSettings::default()
    };
    pdf.pages()
        .iter()
        .map(|page| {
            let pixmap = hayro::render(
                page,
                &RenderCache::new(),
                &InterpreterSettings::default(),
                &settings,
                &PixmapSettings {
                    x_scale: 1.0,
                    y_scale: 1.0,
                    bg_color: AlphaColor::WHITE,
                },
            );
            let samples = pixmap
                .data()
                .iter()
                .flat_map(|pixel| [pixel.r, pixel.g, pixel.b, pixel.a])
                .collect();
            (pixmap.width(), pixmap.height(), samples)
        })
        .collect()
}

fn main() {
    let arguments: Vec<String> = env::args().skip(1).collect();
    assert!(
        !arguments.is_empty() && arguments.len().is_multiple_of(3),
        "usage: hinted-subsets NAME BEFORE AFTER [NAME BEFORE AFTER]..."
    );
    println!("pair\tpage\thinting\tdiffering pixels\tpixels\tlargest difference");
    for pair in arguments.chunks(3) {
        let [name, before, after] = pair else {
            unreachable!()
        };
        let before = Pdf::new(fs::read(before).expect("the first document reads"))
            .expect("hayro reads the first document");
        let after = Pdf::new(fs::read(after).expect("the second document reads"))
            .expect("hayro reads the second document");
        for hinting in [true, false] {
            let ours = render(&before, hinting);
            let theirs = render(&after, hinting);
            assert_eq!(ours.len(), theirs.len(), "{name}: the page counts differ");
            for (page, ((width, height, a), (other_width, other_height, b))) in
                ours.iter().zip(&theirs).enumerate()
            {
                assert_eq!(
                    (width, height),
                    (other_width, other_height),
                    "{name} page {page}"
                );
                let mut differing = 0_u64;
                let mut largest = 0_u8;
                for (x, y) in a.as_chunks::<4>().0.iter().zip(b.as_chunks::<4>().0) {
                    if x != y {
                        differing += 1;
                        let delta = x.iter().zip(y).map(|(p, q)| p.abs_diff(*q)).max();
                        largest = largest.max(delta.unwrap_or(0));
                    }
                }
                let pixels = u64::from(*width) * u64::from(*height);
                println!("{name}\t{page}\t{hinting}\t{differing}\t{pixels}\t{largest}");
            }
        }
    }
}
