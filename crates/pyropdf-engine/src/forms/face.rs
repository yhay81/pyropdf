//! Field values drawn in an embedded OpenType face: the Type0 font of the
//! document's form font, whose CIDs are the face's glyph IDs (DESIGN.md
//! section 7). The face shapes each line with harfrust, and the lines are
//! laid out and fitted as the Standard 14 appearances are.
//!
//! The ToUnicode CMap of the font and its `W` array are written by the
//! caller: a first pass collects the glyphs that the appearance shows,
//! with their texts, and a second draws it once the font records them.

use std::collections::HashMap;

use pyropdf_store::budget::{Budget, BudgetVec};
use pyropdf_store::value::Reference;
use skrifa::GlyphId;

use super::{WidgetError, fmt};
use crate::fonts::embed::{Face, Measure};
use crate::fonts::shape::{ShapeError, ShapedGlyph, ShapingFace};
use crate::standard::{self, LayoutError, Placement, TextBox};

/// The name of the face's font in an appearance's resources.
pub(super) const RESOURCE: &str = "PyroForm";

/// The Type0 font that an appearance names, and the text that its
/// ToUnicode CMap maps each glyph to.
pub struct FaceFont<'m> {
    pub font: Reference,
    pub mapped: &'m HashMap<u32, String>,
}

/// An embedded face that draws field values, and the glyphs that it drew.
pub struct FaceText<'f> {
    face: &'f Face,
    shaper: ShapingFace<'f>,
    measure: Measure<'f>,
    em: f64,
    budget: &'f Budget,
    /// The font that the appearance names; `None` while collecting.
    font: Option<FaceFont<'f>>,
    drawn: BudgetVec<'f, (u32, String)>,
}

impl<'f> FaceText<'f> {
    /// Returns `face` ready to draw, with `font` to name in appearances, or
    /// `None` to collect the glyphs that appearances would show.
    pub fn new(
        face: &'f Face,
        font: Option<FaceFont<'f>>,
        budget: &'f Budget,
    ) -> Result<Self, WidgetError> {
        let shaper = ShapingFace::new(&face.program, 0)
            .map_err(|error| WidgetError::Malformed(format!("invalid font: {error}").into()))?;
        Ok(Self {
            face,
            shaper,
            measure: Measure::new(face),
            em: f64::from(face.units_per_em),
            budget,
            font,
            drawn: BudgetVec::new(budget),
        })
    }

    /// Returns the glyphs that the appearances showed with their texts.
    pub fn into_drawn(self) -> Vec<(u32, String)> {
        self.drawn.into_vec()
    }

    pub(super) fn font(&self) -> Option<Reference> {
        self.font.as_ref().map(|font| font.font)
    }

    fn shape(&self, text: &str) -> Result<BudgetVec<'f, ShapedGlyph>, LayoutError> {
        self.shaper
            .shape(text, self.budget)
            .map_err(|error| match error {
                ShapeError::Limit(refusal) => refusal.into(),
                // A face that reads shapes text without font errors.
                ShapeError::Font(_) => LayoutError::InvalidGeometry,
            })
    }

    /// Refuses `value` when the face lacks a glyph for one of its
    /// characters other than a control character.
    pub(super) fn check(&self, value: &str) -> Result<(), WidgetError> {
        let glyphs = self.shape(value)?;
        if let Some(character) = glyphs
            .iter()
            .filter(|glyph| glyph.glyph == 0)
            .filter_map(|glyph| value[glyph.cluster.clone()].chars().next())
            .find(|character| !character.is_control())
        {
            return Err(WidgetError::Malformed(
                format!(
                    "{} does not contain all glyphs of the text: no glyph for U+{:04X} ({character:?})",
                    self.face.full_name,
                    u32::from(character)
                )
                .into(),
            ));
        }
        Ok(())
    }

    /// Lays out `text` in a box at `font_size`, the lines `line_height` em
    /// apart and placed by the face's ascender and descender.
    pub(super) fn layout<'t>(
        &self,
        text: &'t str,
        box_size: (f64, f64),
        font_size: f64,
        line_height: f64,
    ) -> Result<TextBox<'t>, LayoutError> {
        let placement = Placement {
            ascent: self.face.ascender / self.em,
            descent: -self.face.descender / self.em,
            line_height,
        };
        let mut refusal = None;
        let layout = standard::layout(
            text,
            box_size,
            font_size,
            placement,
            false,
            self.budget,
            |line| match self.shape(line) {
                Ok(glyphs) => {
                    let units: f64 = glyphs.iter().map(|glyph| f64::from(glyph.x_advance)).sum();
                    units * font_size / self.em
                }
                Err(error) => {
                    refusal = Some(error);
                    f64::INFINITY
                }
            },
        );
        match refusal {
            Some(error) => Err(error),
            None => layout,
        }
    }

    /// Appends the operators that show `line` at `size` as shaped glyphs:
    /// `TJ` adjustments move the pen from each glyph's width in `W` to its
    /// shaped advance and by its offset, `Ts` raises it, and a glyph whose
    /// text the ToUnicode CMap does not map is shown inside `/ActualText`,
    /// as canvas text draws it.
    pub(super) fn show(
        &mut self,
        out: &mut BudgetVec<'_, u8>,
        line: &str,
        size: f64,
    ) -> Result<(), LayoutError> {
        let glyphs = self.shape(line)?;
        let scale = 1000.0 / self.em;
        let mut items = BudgetVec::new(self.budget);
        // In TJ units: positive moves left.
        let mut pending = 0.0;
        let mut rise = 0;
        for glyph in glyphs.iter() {
            let text = &line[glyph.text.clone()];
            let offset = f64::from(glyph.x_offset) * scale;
            if glyph.glyph == 0 {
                // A control character without a glyph only advances.
                pending -= f64::from(glyph.x_advance) * scale;
                continue;
            }
            if glyph.y_offset != rise {
                flush(out, &mut items)?;
                rise = glyph.y_offset;
                let raise = format!("{} Ts\n", fmt(f64::from(rise) * size / self.em));
                out.try_extend_from_slice(raise.as_bytes())?;
            }
            if pending - offset != 0.0 {
                if !items.is_empty() {
                    items.try_push(b' ')?;
                }
                items.try_extend_from_slice(fmt(pending - offset).as_bytes())?;
            }
            let code = format!("<{:04X}>", glyph.glyph);
            let mapped = match &self.font {
                Some(font) => {
                    text.is_empty()
                        || font.mapped.get(&glyph.glyph).map(String::as_str) == Some(text)
                }
                None => {
                    self.drawn.try_push((glyph.glyph, text.to_owned()))?;
                    true
                }
            };
            if mapped {
                if !items.is_empty() {
                    items.try_push(b' ')?;
                }
                items.try_extend_from_slice(code.as_bytes())?;
            } else {
                flush(out, &mut items)?;
                out.try_extend_from_slice(b"/Span <</ActualText <FEFF")?;
                for unit in text.encode_utf16() {
                    out.try_extend_from_slice(format!("{unit:04X}").as_bytes())?;
                }
                out.try_extend_from_slice(format!(">>> BDC\n[{code}] TJ\nEMC\n").as_bytes())?;
            }
            let width = self.measure.advance(GlyphId::new(glyph.glyph)) * scale;
            pending = width - f64::from(glyph.x_advance) * scale + offset;
        }
        flush(out, &mut items)?;
        if rise != 0 {
            out.try_extend_from_slice(b"0 Ts\n")?;
        }
        Ok(())
    }
}

/// Appends the pending `TJ` array, if any.
fn flush(out: &mut BudgetVec<'_, u8>, items: &mut BudgetVec<'_, u8>) -> Result<(), LayoutError> {
    if !items.is_empty() {
        out.try_push(b'[')?;
        out.try_extend_from_slice(items)?;
        out.try_extend_from_slice(b"] TJ\n")?;
        items.clear();
    }
    Ok(())
}
