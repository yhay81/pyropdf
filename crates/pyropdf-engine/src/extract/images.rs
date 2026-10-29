//! The pixels of one image that a page paints, as hayro decodes them for
//! rendering (ISO 32000-2 8.9), with its soft mask or mask as alpha.

use hayro::hayro_interpret::font::Glyph;
use hayro::hayro_interpret::{DrawMode, Image, ImageData, InterpreterCache, InterpreterSettings};
use hayro::kurbo::{Affine, BezPath, Rect};
use pyropdf_store::budget::{Budget, LimitCode};

use super::Error;
use super::model::{self, ContentSink, GlyphStyle, ItemKinds};
use crate::bridge::Bridged;

/// An image's pixels, row by row from the top, 8 bits per channel.
pub struct DecodedImage {
    pub width: u32,
    pub height: u32,
    /// Whether `color` holds one gray byte per pixel instead of three.
    pub gray: bool,
    pub color: Vec<u8>,
    /// One alpha byte per pixel, for an image with a mask or a stencil.
    pub alpha: Option<Vec<u8>>,
}

/// The sink that decodes the page's image `target`.
struct Grab<'b> {
    target: usize,
    budget: &'b Budget,
    found: bool,
    image: Option<DecodedImage>,
}

impl ContentSink for Grab<'_> {
    fn glyph_run(&mut self, _: &GlyphStyle) {}
    fn glyph(&mut self, _: &Glyph<'_>, _: Affine, _: Option<Rect>) {}
    fn stroke_completes_fill(&mut self) {}
    fn path(&mut self, _: &BezPath, _: Affine, _: &DrawMode) {}
    fn stopped(&self) -> bool {
        self.found
    }

    fn image(&mut self, ordinal: usize, image: &Image<'_, '_>) -> Result<(), Error> {
        if self.found || ordinal != self.target {
            return Ok(());
        }
        self.found = true;
        // The decoded color and alpha, charged before hayro allocates them.
        let pixels = u64::from(image.width()) * u64::from(image.height());
        self.budget
            .charge(LimitCode::TotalDecompressedSize, pixels.saturating_mul(4))?;
        match image {
            Image::Raster(raster) => raster.with_rgba(
                |data, alpha| {
                    let (width, height, gray, color) = match data {
                        ImageData::Rgb(rgb) => (rgb.width, rgb.height, false, rgb.data),
                        ImageData::Luma(luma) => (luma.width, luma.height, true, luma.data),
                    };
                    // hayro may decode a mask at another size than the image.
                    let alpha = alpha
                        .filter(|alpha| alpha.width == width && alpha.height == height)
                        .map(|alpha| alpha.data);
                    self.image = Some(DecodedImage {
                        width,
                        height,
                        gray,
                        color,
                        alpha,
                    });
                },
                None,
            ),
            Image::Stencil(stencil) => stencil.with_stencil(
                |mask, _| {
                    let size = mask.data.len();
                    self.image = Some(DecodedImage {
                        width: mask.width,
                        height: mask.height,
                        gray: true,
                        color: vec![0; size],
                        alpha: Some(mask.data),
                    });
                },
                None,
            ),
        }
        Ok(())
    }
}

/// Decodes image `ordinal`, counted from 0 in the paint order of
/// `Page.content()`, of page `index`, painted with annotation appearances
/// when `annotations` is set. `None` when the page paints fewer images or
/// hayro cannot decode that one. The decoded bytes count against the
/// budget's `total_decompressed_size`.
pub fn page_image(
    bridged: &Bridged,
    index: usize,
    ordinal: usize,
    annotations: bool,
    budget: &Budget,
) -> Result<Option<DecodedImage>, Error> {
    let pdf = bridged.pdf();
    let pages = pdf.pages();
    let page = pages.get(index).ok_or(Error::NoPage(index))?;
    let settings = InterpreterSettings {
        render_annotations: annotations,
        ..InterpreterSettings::default()
    };
    let cache = InterpreterCache::new();
    let kinds = ItemKinds {
        text_runs: false,
        paths: false,
        images: true,
    };
    let grab = Grab {
        target: ordinal,
        budget,
        found: false,
        image: None,
    };
    let (_, grab) =
        model::collect_page_content(pdf, page, settings, &cache, grab, kinds, None, budget)?;
    Ok(grab.image)
}
