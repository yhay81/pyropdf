//! Raster pixel operations, image codecs, CCITT Group 4 encoding, and PDF
//! image sample and stream helpers (DESIGN.md section 11).

mod codecs;
mod encode;
pub mod fax;
pub mod images;
mod ops;
#[cfg(test)]
mod tests;

use pyropdf_store::LimitExceeded;

pub use encode::{
    Bounded, BudgetedPngOutput, EncodeError, Encoding, FORMATS, encode, encode_png, format_of,
    recolor_samples,
};
pub use ops::{
    Projection, add_alpha, color_runs, convert, crop, decode, device_samples, drop_alpha, fill,
    flip, gamma_table, invert, luma, map_colors, rotate, scale, tint_table, warp,
};

/// The most pixels of a decoded image or a scaled pixmap, as rendering
/// bounds one raster.
pub const MAX_PIXELS: u64 = 64_000_000;

/// The shape of a pixmap's samples.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Layout {
    pub width: u32,
    pub height: u32,
    pub gray: bool,
    pub alpha: bool,
}

impl Layout {
    /// The color components of a pixel, without alpha.
    #[must_use]
    pub const fn colors(self) -> usize {
        if self.gray { 1 } else { 3 }
    }

    /// The bytes of a pixel.
    #[must_use]
    pub const fn channels(self) -> usize {
        self.colors() + self.alpha as usize
    }

    /// The bytes of a row.
    #[must_use]
    pub const fn stride(self) -> usize {
        self.width as usize * self.channels()
    }
}

/// A rectangle of whole pixels, `x0 <= x < x1` and `y0 <= y < y1`.
pub type Area = (u32, u32, u32, u32);

/// A decoded image: its layout, samples, and resolution in dots per inch.
#[derive(Debug)]
pub struct Decoded {
    pub layout: Layout,
    pub data: Vec<u8>,
    pub resolution: (u32, u32),
}

/// A malformed image or an operation budget that refused decoding.
#[derive(Debug)]
pub enum DecodeError {
    Invalid(String),
    Limit(LimitExceeded),
}

impl From<String> for DecodeError {
    fn from(message: String) -> Self {
        Self::Invalid(message)
    }
}

impl From<LimitExceeded> for DecodeError {
    fn from(refusal: LimitExceeded) -> Self {
        Self::Limit(refusal)
    }
}

/// Checks that `width` by `height` holds between 1 and [`MAX_PIXELS`] pixels.
pub fn check_size(width: u32, height: u32) -> Result<(), String> {
    let pixels = u64::from(width) * u64::from(height);
    if pixels == 0 || pixels > MAX_PIXELS {
        return Err(format!(
            "the image has {pixels} pixels; a pixmap holds 1 to {MAX_PIXELS}"
        ));
    }
    Ok(())
}
