//! The stretch of a substitute font's glyphs.
//!
//! hayro draws a font in a substitute when a document does not embed its
//! program or when the program does not load, and stretches each glyph to the
//! width that the PDF gives it. It keeps the stretch in the glyph's transform,
//! where an advance taken through the transform is stretched too, so a reader
//! of advances takes it out again. Ink boxes keep it, because a rendering
//! draws it.
//!
//! `hayro_interpret` keeps the stretch to itself, and only its font crate tells
//! it, of a font that it loads. Loading a font again costs as much as the first
//! time, and which program hayro read does not show in a font's dictionary, so
//! a run's glyphs decide: most text in an embedded program has none that a
//! stretch could have touched, and loads nothing.

use hayro::hayro_interpret::InterpreterSettings;
use hayro::hayro_interpret::font::{Glyph, GlyphRun};
use hayro::kurbo::Affine;
use hayro_font::{CMapResolverFn, Font, FontCache, FontResolverFn};

/// The largest difference, relative to the length of a glyph's y basis, that
/// leaves its x basis the same length: rounding, or a stretch of no effect.
const SAME_LENGTH: f64 = 1e-9;

/// Loads the fonts of the runs that hayro may have stretched, with the
/// resolvers that it uses.
pub(super) struct Stretches<'a> {
    cache: FontCache<'a>,
    font_resolver: FontResolverFn,
    cmap_resolver: CMapResolverFn,
}

impl<'a> Stretches<'a> {
    pub(super) fn new(settings: &InterpreterSettings) -> Self {
        Self {
            cache: FontCache::new(),
            font_resolver: settings.font_resolver.clone(),
            cmap_resolver: settings.cmap_resolver.clone(),
        }
    }

    /// The font that tells the stretches of `run`'s glyphs, or `None` when
    /// they have none. A run without a font dictionary shows hayro's fallback
    /// font, which has no PDF widths to stretch to.
    pub(super) fn font_of(&self, run: &GlyphRun<'_, 'a>) -> Option<Font<'a>> {
        let dict = run.font_dict()?;
        run.glyphs()
            .iter()
            .any(|glyph| {
                matches!(&**glyph, Glyph::Outline(_)) && may_be_stretched(glyph.transform())
            })
            .then(|| {
                self.cache
                    .font(dict, &self.font_resolver, &self.cmap_resolver)
            })?
    }
}

/// Whether a glyph's transform from its glyph space to text space may hold a
/// stretch, which leaves its x basis another length than its y basis. So do
/// `Tz` and a text matrix that is no similarity, for which the font tells a
/// stretch of 1. Only a stretch that `Tz` cancels exactly goes unseen.
fn may_be_stretched(transform: Affine) -> bool {
    let [a, b, c, d, _, _] = transform.as_coeffs();
    let (x, y) = (a.hypot(b), c.hypot(d));
    (x - y).abs() > y * SAME_LENGTH
}

/// `transform` of a glyph of `font` without the stretch that hayro keeps in
/// it, so that the glyph's advance through it is the PDF's width. A width of 0
/// stretches the glyph to nothing, which leaves no x basis to scale back: the
/// y basis then gives its direction, in display space, whose y axis points
/// down.
pub(super) fn without_stretch(
    transform: Affine,
    font: Option<&Font<'_>>,
    glyph: &Glyph<'_>,
) -> Affine {
    let (Some(font), Glyph::Outline(outline)) = (font, glyph) else {
        return transform;
    };
    let stretch = font.glyph_stretch(outline.glyph_id(), outline.char_code());
    if stretch == 0.0 {
        let [_, _, c, d, e, f] = transform.as_coeffs();
        return Affine::new([-d, c, c, d, e, f]);
    }
    if stretch == 1.0 || !stretch.is_finite() {
        return transform;
    }
    transform * Affine::scale_non_uniform(stretch.recip(), 1.0)
}
