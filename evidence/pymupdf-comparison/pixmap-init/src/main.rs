//! Compare repeated-pixel construction with zeroed byte allocation.
//! This is a constructor probe, not evidence of a full rendering speedup.
use std::hint::black_box;
use std::time::Instant;
use vello_common::peniko::ImageAlphaType;
use vello_common::pixmap::{PixelMetadata, Pixmap};

fn make(width: u16, height: u16, zeroed: bool) -> Pixmap {
    if zeroed {
        let bytes = vec![0_u8; usize::from(width) * usize::from(height) * 4];
        Pixmap::from_parts(
            bytes,
            width,
            height,
            PixelMetadata::new(ImageAlphaType::AlphaPremultiplied, true),
        )
    } else {
        Pixmap::new(width, height)
    }
}

fn main() {
    println!("width,height,round,zeroed,touch,milliseconds");
    for (width, height) in [(612, 792), (1224, 1584), (2480, 3508)] {
        for touch in [false, true] {
            for round in 0..10 {
                let order = if round % 2 == 0 {
                    [false, true]
                } else {
                    [true, false]
                };
                for zeroed in order {
                    let start = Instant::now();
                    let mut pixmap = black_box(make(black_box(width), black_box(height), zeroed));
                    if touch {
                        // Force every page to be touched: zeroed allocation alone
                        // can defer physical-page costs to the eventual renderer.
                        pixmap.data_as_u8_slice_mut().fill(black_box(255));
                    }
                    black_box(&pixmap);
                    drop(pixmap);
                    let elapsed = start.elapsed().as_secs_f64() * 1000.0;
                    println!("{width},{height},{round},{zeroed},{touch},{elapsed:.6}");
                }
            }
        }
        let pixmap = make(width, height, true);
        assert_eq!(pixmap.width(), width);
        assert_eq!(pixmap.height(), height);
        assert!(pixmap.data_as_u8_slice().iter().all(|&byte| byte == 0));
    }
}
