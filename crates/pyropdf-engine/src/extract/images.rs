//! The pixels of one image that a page paints or carries as its `/Thumb`
//! thumbnail, as hayro decodes them for rendering (ISO 32000-2 8.9 and
//! 12.3.4), with its soft mask or mask as alpha.

use hayro::hayro_interpret::font::{Glyph, GlyphRun};
use hayro::hayro_interpret::{
    BlendMode, ClipPath, Device, DrawMode, DrawProps, Image, ImageData, ImageDrawProps,
    InterpreterCache, InterpreterSettings, RenderContent, SoftMask, interpret,
};
use hayro::hayro_syntax::content::TypedIter;
use hayro::hayro_syntax::object::{Name, Stream};
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

/// Decodes one hayro `Image` after charging its RGBA byte footprint to
/// `budget`'s `total_decompressed_size`.
fn decode_hayro_image(
    image: &Image<'_, '_>,
    budget: &Budget,
) -> Result<Option<DecodedImage>, Error> {
    // The decoded color and alpha, charged before hayro allocates them.
    let pixels = u64::from(image.width()) * u64::from(image.height());
    budget.charge(LimitCode::TotalDecompressedSize, pixels.saturating_mul(4))?;
    let mut out = None;
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
                out = Some(DecodedImage {
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
                #[expect(
                    clippy::disallowed_methods,
                    reason = "as many bytes as the decoded mask that hayro holds"
                )]
                let color = vec![0; mask.data.len()];
                out = Some(DecodedImage {
                    width: mask.width,
                    height: mask.height,
                    gray: true,
                    color,
                    alpha: Some(mask.data),
                });
            },
            None,
        ),
    }
    Ok(out)
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
        self.image = decode_hayro_image(image, self.budget)?;
        Ok(())
    }
}

/// Device that captures the first image XObject painted during a thumbnail
/// interpretation.
struct ThumbnailGrab<'b> {
    budget: &'b Budget,
    done: bool,
    error: Option<Error>,
    image: Option<DecodedImage>,
}

impl Device<'_> for ThumbnailGrab<'_> {
    fn draw_path(&mut self, _: &BezPath, _: DrawProps<'_>, _: &DrawMode) {}
    fn push_clip_path(&mut self, _: &ClipPath) {}
    fn push_transparency_group(&mut self, _: f32, _: Option<SoftMask<'_>>, _: BlendMode) {}
    fn draw_glyph_run(&mut self, _: &GlyphRun<'_, '_>, _: DrawProps<'_>, _: &DrawMode) {}
    fn pop_clip(&mut self) {}
    fn pop_transparency_group(&mut self) {}

    fn draw_image(&mut self, image: Image<'_, '_>, _: ImageDrawProps<'_>) {
        if self.done {
            return;
        }
        self.done = true;
        match decode_hayro_image(&image, self.budget) {
            Ok(decoded) => self.image = decoded,
            Err(error) => self.error = Some(error),
        }
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
    let (page, display) = bridged.page(index).ok_or(Error::NoPage(index))?;
    let settings = InterpreterSettings {
        render_content: if annotations {
            RenderContent::all()
        } else {
            RenderContent::PAGE
        },
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
        model::collect_page_content(page, display, settings, &cache, grab, kinds, None, budget)?;
    Ok(grab.image)
}

/// Decodes the `/Thumb` thumbnail stream of page `index` (ISO 32000-2 12.3.4)
/// through hayro, assuming its stream dictionary carries `/Subtype /Image`.
/// Returns `None` when the page has no `/Thumb` stream or hayro cannot decode
/// it. The decoded bytes count against `total_decompressed_size`.
pub fn page_thumbnail(
    bridged: &Bridged,
    index: usize,
    budget: &Budget,
) -> Result<Option<DecodedImage>, Error> {
    let (page, display) = bridged.page(index).ok_or(Error::NoPage(index))?;
    let Some(stream) = page.raw().get::<Stream<'_>>(b"Thumb") else {
        return Ok(None);
    };
    if stream
        .dict()
        .get::<Name<'_>>(b"Subtype")
        .is_none_or(|subtype| subtype.as_ref() != b"Image")
    {
        return Ok(None);
    }
    let mut resources = page.resources().clone();
    resources.x_objects = page.raw().clone();
    let cache = InterpreterCache::new();
    let mut context =
        super::extraction_context(page, display, &cache, InterpreterSettings::default());
    let mut grab = ThumbnailGrab {
        budget,
        done: false,
        error: None,
        image: None,
    };
    interpret(
        TypedIter::new(b"/Thumb Do"),
        &resources,
        &mut context,
        &mut grab,
    );
    if let Some(error) = grab.error {
        return Err(error);
    }
    Ok(grab.image)
}
