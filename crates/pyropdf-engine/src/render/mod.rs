//! Rasterizing a page through hayro, as much as `page.pixmap`
//! (yhay81/pyropdf#35) needs.

use hayro::hayro_interpret::InterpreterSettings;
use hayro::vello_cpu::color::AlphaColor;
use hayro::{PixmapSettings, RenderCache, RenderSettings};

use crate::bridge::{Bridged, Error, Result};

/// A rendered page: its size in pixels and its pixels, row by row from the
/// top, as premultiplied RGBA with 8 bits per channel.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Rgba {
    /// The width in pixels.
    pub width: u16,
    /// The height in pixels.
    pub height: u16,
    /// Four bytes per pixel.
    pub data: Vec<u8>,
}

/// Renders page `index`, counted from 0, at `scale` pixels per point, on a
/// transparent background.
pub fn page(bridged: &Bridged, index: usize, scale: f32) -> Result<Rgba> {
    let pages = bridged.pdf().pages();
    let page = pages.get(index).ok_or(Error::NoPage(index))?;
    let pixmap = hayro::render(
        page,
        &RenderCache::new(),
        &InterpreterSettings::default(),
        &RenderSettings::default(),
        &PixmapSettings {
            x_scale: scale,
            y_scale: scale,
            bg_color: AlphaColor::TRANSPARENT,
        },
    );
    let data = pixmap
        .data()
        .iter()
        .flat_map(|pixel| [pixel.r, pixel.g, pixel.b, pixel.a])
        .collect();
    Ok(Rgba {
        width: pixmap.width(),
        height: pixmap.height(),
        data,
    })
}
