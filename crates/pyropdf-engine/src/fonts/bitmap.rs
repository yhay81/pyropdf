//! The colour bitmaps of an embedded face (DESIGN.md section 7): the PNG
//! strikes of its `sbix` and `CBDT` tables, and the glyph procedure that
//! paints one in a Type 3 font ([`type3`](super::type3)).
//!
//! A drawing takes the smallest strike that holds the glyph at
//! [`DRAWING_PPI`], else the largest that holds it. `sbix` comes first when a
//! face has both tables, as skrifa and Skia read them. JPEG, TIFF, and raw
//! strikes are not read: such a glyph stays a colour glyph that drawing
//! cannot paint ([`colour`](super::colour)).

use skrifa::FontRef;
use skrifa::GlyphId;
use skrifa::bitmap::{BitmapData, BitmapFormat, BitmapGlyph, BitmapStrike, BitmapStrikes, Origin};
use skrifa::raw::TableProvider;
use skrifa::raw::tables::sbix::Sbix;
use skrifa::raw::types::Tag;

use super::colour::check_lookups;
use super::shape::ShapeError;

#[cfg(test)]
mod tests;

/// The resolution, in pixels per inch, that a strike should reach at the
/// drawn size. A page may be printed, and a PDF has no resolution of its own.
pub const DRAWING_PPI: f64 = 300.0;

/// The signature that opens a PNG image.
const PNG_SIGNATURE: &[u8] = b"\x89PNG\r\n\x1a\n";

/// One PNG of a strike, placed in the glyph's space.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Bitmap<'a> {
    /// The PNG image.
    pub png: &'a [u8],
    /// The strike's pixels per em, which with the glyph names the image.
    pub ppem: u16,
    /// Where the image lies, in font units: `[x, y, width, height]`.
    pub rect: [f64; 4],
}

/// The strikes of a face's colour bitmap tables.
pub struct Bitmaps<'a> {
    em: f64,
    /// `sbix` needs its own table too, for the records that repeat a glyph.
    sbix: Option<(Sbix<'a>, BitmapStrikes<'a>)>,
    cbdt: Option<BitmapStrikes<'a>>,
}

impl<'a> Bitmaps<'a> {
    /// Reads the strikes of `font`, which may have none. A font error when a
    /// lookup would walk too far, as [`super::colour`] counts it.
    pub fn read(font: &FontRef<'a>) -> Result<Self, ShapeError> {
        check_lookups(font)?;
        let em = font
            .head()
            .map_or(0.0, |head| f64::from(head.units_per_em()));
        let sbix = font.sbix().ok().and_then(|table| {
            BitmapStrikes::with_format(font, BitmapFormat::Sbix).map(|strikes| (table, strikes))
        });
        Ok(Self {
            em,
            sbix,
            cbdt: BitmapStrikes::with_format(font, BitmapFormat::Cbdt),
        })
    }

    /// Returns whether a strike holds a PNG of `glyph`.
    #[must_use]
    pub fn has(&self, glyph: GlyphId) -> bool {
        self.best(glyph, 0.0).is_some()
    }

    /// Returns the bitmap of `glyph` for drawing at `ppem`: of the strikes
    /// that hold the glyph in the first table that does, the one of exactly
    /// `ppem`, else the nearest larger, else the nearest smaller. Strikes of
    /// one size keep the first, so one `ppem` always finds one bitmap.
    #[must_use]
    pub fn best(&self, glyph: GlyphId, ppem: f64) -> Option<Bitmap<'a>> {
        let mut best = None;
        if let Some((table, strikes)) = &self.sbix {
            for (at, strike) in strikes.iter().enumerate() {
                let repeated = sbix_repeated(table, at, glyph);
                best = self.better(best, &strike, repeated, ppem);
            }
        }
        if best.is_some() {
            return best;
        }
        if let Some(strikes) = &self.cbdt {
            for strike in strikes.iter() {
                best = self.better(best, &strike, glyph, ppem);
            }
        }
        best
    }

    /// Returns `best`, or the bitmap of `glyph` in `strike` when that is
    /// closer to `ppem`.
    fn better(
        &self,
        best: Option<Bitmap<'a>>,
        strike: &BitmapStrike<'a>,
        glyph: GlyphId,
        ppem: f64,
    ) -> Option<Bitmap<'a>> {
        let found = strike.get(glyph).and_then(|bitmap| self.placed(&bitmap));
        match (best, found) {
            (Some(best), Some(found)) => {
                let (size, known) = (f64::from(found.ppem), f64::from(best.ppem));
                let closer = if known >= ppem {
                    size >= ppem && size < known
                } else {
                    size > known
                };
                Some(if closer { found } else { best })
            }
            (best, found) => best.or(found),
        }
    }

    /// Returns `bitmap` in font units, when it is a PNG that covers an area.
    fn placed(&self, bitmap: &BitmapGlyph<'a>) -> Option<Bitmap<'a>> {
        let BitmapData::Png(png) = bitmap.data else {
            return None;
        };
        let ppem = bitmap.ppem_y;
        if !png.starts_with(PNG_SIGNATURE) || bitmap.ppem_x < 1.0 || ppem < 1.0 {
            return None;
        }
        if bitmap.width == 0 || bitmap.height == 0 {
            return None;
        }
        // Multiplying first keeps whole units whole.
        let across = |pixels: f32| f64::from(pixels) * self.em / f64::from(bitmap.ppem_x);
        let up = |pixels: f32| f64::from(pixels) * self.em / f64::from(ppem);
        let width = f64::from(bitmap.width) * self.em / f64::from(bitmap.ppem_x);
        let height = f64::from(bitmap.height) * self.em / f64::from(ppem);
        let x = f64::from(bitmap.bearing_x) + across(bitmap.inner_bearing_x);
        let mut y = f64::from(bitmap.bearing_y) + up(bitmap.inner_bearing_y);
        if bitmap.placement_origin == Origin::TopLeft {
            y -= height;
        }
        Some(Bitmap {
            png,
            // Both tables store a strike's size in 16 bits at most.
            ppem: ppem as u16,
            rect: [x, y, width, height],
        })
    }
}

/// Returns the glyph whose graphic `glyph` shows in the `sbix` strike `at`:
/// the one that a `dupe` record names, else `glyph`. A `dupe` of a `dupe`
/// finds no PNG, so that a lookup stays one step per strike.
fn sbix_repeated(table: &Sbix<'_>, at: usize, glyph: GlyphId) -> GlyphId {
    let record = table
        .strikes()
        .get(at)
        .ok()
        .and_then(|strike| strike.glyph_data(glyph).ok().flatten());
    match record {
        Some(record) if record.graphic_type() == Tag::new(b"dupe") => match record.data() {
            &[high, low, ..] => GlyphId::from(u16::from_be_bytes([high, low])),
            _ => glyph,
        },
        _ => glyph,
    }
}

/// Returns the procedure of a Type 3 glyph that paints the image named
/// `image` over `rect` of a [`Bitmap`], and advances by `width`, both in
/// glyph space. `d0` leaves the colour to the procedure (ISO 32000-2 9.6.5).
#[must_use]
pub fn procedure(width: f64, rect: [f64; 4], image: &str) -> Vec<u8> {
    let [x, y, across, up] = rect;
    format!("{width} 0 d0\nq {across} 0 0 {up} {x} {y} cm /{image} Do Q").into_bytes()
}

/// Returns the procedure of a Type 3 glyph that paints nothing, as a space
/// of a face with no outlines does, which still reads back and advances.
#[must_use]
pub fn empty_procedure(width: f64) -> Vec<u8> {
    format!("{width} 0 d0").into_bytes()
}
