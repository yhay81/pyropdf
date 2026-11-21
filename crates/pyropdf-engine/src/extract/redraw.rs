//! The PDF character codes and text matrices needed to paint an extracted run.
//! Glyph IDs alone cannot identify a code in a font's Encoding or ToUnicode.
use hayro::hayro_interpret::font::GlyphRun;
use hayro::hayro_syntax::object::{Array, Dict, Name, Stream};
use hayro::kurbo::Affine;

use super::Error;
use super::model::with_capacity;
use super::stretch::Stretches;

/// Font object and generation, code width, and the PDF text matrix of each code.
/// An unavailable source carries the reason the canvas cannot paint it.
pub type Replay = (
    Option<(u32, u16)>,
    u8,
    Vec<(u32, [f64; 6])>,
    Option<&'static str>,
    Option<[f64; 6]>,
);

fn refusal(reason: &'static str) -> Replay {
    (None, 0, Vec::new(), Some(reason), None)
}

fn embedded(font: &Dict<'_>) -> bool {
    font.get::<Dict<'_>>(b"FontDescriptor")
        .is_some_and(|descriptor| {
            [b"FontFile".as_slice(), b"FontFile2", b"FontFile3"]
                .iter()
                .any(|key| descriptor.get::<Stream<'_>>(key).is_some())
        })
}

fn standard_font(font: &Dict<'_>) -> bool {
    font.get::<Name<'_>>(b"BaseFont").is_some_and(|name| {
        matches!(
            name.as_ref(),
            b"Times-Roman"
                | b"Times-Bold"
                | b"Times-Italic"
                | b"Times-BoldItalic"
                | b"Helvetica"
                | b"Helvetica-Bold"
                | b"Helvetica-Oblique"
                | b"Helvetica-BoldOblique"
                | b"Courier"
                | b"Courier-Bold"
                | b"Courier-Oblique"
                | b"Courier-BoldOblique"
                | b"Symbol"
                | b"ZapfDingbats"
        )
    })
}

fn encoding(dict: &Dict<'_>) -> Result<(u8, bool), &'static str> {
    match dict.get::<Name<'_>>(b"Subtype").as_deref() {
        Some(b"Type0") => {
            let descendant = dict
                .get::<Array<'_>>(b"DescendantFonts")
                .and_then(|array| array.iter::<Dict<'_>>().next());
            if !descendant.as_ref().is_some_and(embedded) {
                return Err("the composite font relies on a fallback face");
            }
            match dict.get::<Name<'_>>(b"Encoding").as_deref() {
                Some(b"Identity-H" | b"Identity-V") => Ok((2, false)),
                _ => Err("the composite CMap does not report the original code byte lengths"),
            }
        }
        Some(b"Type1" | b"TrueType" | b"MMType1") if embedded(dict) => Ok((1, false)),
        Some(b"Type1" | b"TrueType") if standard_font(dict) => Ok((1, true)),
        Some(b"Type3") if dict.get::<Dict<'_>>(b"Resources").is_some() => Ok((1, false)),
        Some(b"Type3") => Err("the Type 3 glyph procedures have no font resource dictionary"),
        _ => Err("the font is neither embedded nor a standard 14 font"),
    }
}

pub(super) fn capture<'a>(
    run: &GlyphRun<'_, 'a>,
    transform: Affine,
    fonts: &Stretches<'a>,
    actual_text: bool,
    budget: &pyropdf_store::budget::Budget,
) -> Result<Replay, Error> {
    if actual_text {
        return Ok(refusal("a marked-content replacement text spans the run"));
    }
    let Some(dict) = run.font_dict() else {
        return Ok(refusal(
            "the interpreter used a fallback without a font dictionary",
        ));
    };
    let Some(reference) = dict
        .obj_id()
        .and_then(|id| Some((id.obj_number, u16::try_from(id.gen_number).ok()?)))
    else {
        return Ok(refusal("the source font dictionary is direct"));
    };
    let (width, standard) = match encoding(dict) {
        Ok(encoding) => encoding,
        Err(reason) => return Ok(refusal(reason)),
    };
    let selected = if standard {
        fonts.resolve(dict)
    } else {
        fonts.embedded_font(dict)
    };
    let Some(font) = selected else {
        return Ok(refusal(
            "the source font could not be loaded without a substitute",
        ));
    };
    let mut codes = with_capacity(run.glyphs().len(), "redrawn character codes and matrices")?;
    for glyph in run.glyphs() {
        budget.poll()?;
        let code = glyph.char_code();
        let stretch = font.glyph_stretch(font.map_code(code), code);
        if !stretch.is_finite() || stretch == 0.0 || code >= 1u32 << (u32::from(width) * 8) {
            return Ok(refusal(
                "the character code or font stretch cannot be reproduced",
            ));
        }
        // hayro already applied these font-dependent operations. The PDF reader
        // will apply them again when it reads our Tm and the original code.
        let matrix = transform
            * glyph.transform()
            * Affine::scale_non_uniform(stretch.recip(), 1.0)
            * Affine::translate(-font.origin_displacement(code))
            * Affine::scale(1000.0);
        if !matrix.is_finite() {
            return Ok(refusal("the glyph's text matrix is not finite"));
        }
        #[expect(
            clippy::disallowed_methods,
            reason = "one entry per budgeted glyph, capacity reserved above"
        )]
        codes.push((code, matrix.as_coeffs()));
    }
    // Public stroke widths and dash lengths are in display units. Retain the
    // shape of an anisotropic CTM so that its pen is still elliptical/skewed.
    let [a, b, c, d, _, _] = transform.as_coeffs();
    let (sx, sy) = (a.hypot(b), c.hypot(d));
    let scale = sx.max(sy);
    let pen = if (sx - sy).abs() > scale * 1e-12 || (a * c + b * d).abs() > scale * scale * 1e-12 {
        let pen = Affine::new([a / scale, b / scale, c / scale, d / scale, 0.0, 0.0]);
        if !pen.inverse().is_finite() {
            return Ok(refusal("the glyph transformation is singular"));
        }
        Some(pen.as_coeffs())
    } else {
        None
    };
    Ok((Some(reference), width, codes, None, pen))
}

impl super::PageContent {
    /// Move replay matrices from the interpreter's viewport to the exact display space
    /// the PDF writer uses. Native unit-one viewports round box coordinates to f32;
    /// copying that translation into the PDF applies the rounding a second time.
    pub fn align_redrawn_text(
        &mut self,
        bridge: &crate::bridge::Bridged,
        index: usize,
        page: &pyropdf_store::pages::Page,
        budget: &pyropdf_store::budget::Budget,
    ) -> Result<(), Error> {
        let (_, interpreter) = bridge.page(index).ok_or(Error::NoPage(index))?;
        let writer = crate::display::Display::from_page(page).transform();
        let correction = if writer == interpreter.transform() {
            Affine::IDENTITY
        } else {
            writer * interpreter.transform().inverse()
        };
        for item in &mut self.items {
            if let super::ContentItem::Text(run) = item {
                let (_, _, glyphs, _, pen) = &mut run.7;
                if run.4.is_none() {
                    *pen = None;
                }
                let transform = if let Some(pen) = pen {
                    Affine::new(*pen).inverse() * correction
                } else if correction == Affine::IDENTITY {
                    continue;
                } else {
                    correction
                };
                for (_, matrix) in glyphs {
                    budget.poll()?;
                    *matrix = (transform * Affine::new(*matrix)).as_coeffs();
                }
            }
        }
        Ok(())
    }
}
