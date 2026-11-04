//! Colour glyphs of an embedded face (DESIGN.md section 7).
//!
//! A CIDFont draws a glyph's outline, so a glyph that a colour table covers
//! draws its monochrome fallback outline. One with no outline would draw
//! nothing under text that reads back, so drawing refuses it. Measuring does
//! not: a face may hold glyphs with outlines too, and the text may never be
//! drawn. `ColourTables::covers` is the one place that says which tables
//! count. Canvas text paints COLR ([`colr`](super::colr)) and the PNG strikes
//! ([`bitmap`](super::bitmap)) as a Type 3 font, so it refuses only what is
//! left; a form field or an annotation has no Type 3 path, writes a CIDFont,
//! and refuses those glyphs too, so `covers` stays wider than what the canvas
//! paints.

use skrifa::color::ColorGlyphCollection;
use skrifa::instance::{LocationRef, Size};
use skrifa::outline::{DrawSettings, OutlineGlyphCollection, OutlinePen};
use skrifa::raw::TableProvider;
use skrifa::raw::tables::{cblc::Cblc, sbix::Sbix, svg::Svg};
use skrifa::{FontRef, GlyphId, MetadataProvider};

use pyropdf_store::budget::Budget;

use super::bitmap::Bitmaps;
use super::colr::{Colr, Verdict};
use super::embed::Face;
use super::shape::{ShapeError, ShapedGlyph};

#[cfg(test)]
mod tests;

/// The most strikes and index subtables that a lookup may walk for one
/// glyph. Glyph IDs have 16 bits, so a face that needs more only asks for
/// work, and each distinct glyph of a text pays for it.
const MAX_LOOKUP_STEPS: usize = 1 << 16;

/// The words of a set of 16-bit glyph IDs.
const GLYPH_SET_WORDS: usize = (u16::MAX as usize + 1) / 64;

/// The colour tables of a face that cover glyphs. `CBDT` needs `CBLC` to
/// find its bitmaps, and `EBDT` holds no colour, so it is not read.
struct ColourTables<'a> {
    colr: Option<ColorGlyphCollection<'a>>,
    sbix: Option<Sbix<'a>>,
    cblc: Option<Cblc<'a>>,
    svg: Option<Svg<'a>>,
}

impl<'a> ColourTables<'a> {
    /// Reads the tables of `font`: `None` when it has none that read, as
    /// most faces do, and a font error when a lookup would walk too far.
    fn read(font: &FontRef<'a>) -> Result<Option<Self>, ShapeError> {
        let tables = Self {
            colr: font.colr().ok().map(|_| font.color_glyphs()),
            sbix: font.sbix().ok(),
            cblc: font.cbdt().ok().and_then(|_| font.cblc().ok()),
            svg: font.svg().ok(),
        };
        if tables.colr.is_none()
            && tables.sbix.is_none()
            && tables.cblc.is_none()
            && tables.svg.is_none()
        {
            return Ok(None);
        }
        if tables.lookup_steps() > MAX_LOOKUP_STEPS {
            return Err(ShapeError::Font(
                "the face's colour tables ask for more work than drawing allows".into(),
            ));
        }
        Ok(Some(tables))
    }

    /// Returns how many strikes and index subtables a lookup walks at most,
    /// stopping once that is over the limit.
    fn lookup_steps(&self) -> usize {
        let mut steps = self.sbix.as_ref().map_or(0, |sbix| sbix.strikes().len());
        if let Some(cblc) = &self.cblc {
            for size in cblc.bitmap_sizes() {
                let subtables = size
                    .index_subtable_list(cblc.offset_data())
                    .map_or(0, |list| list.index_subtable_records().len());
                steps = steps.saturating_add(subtables).saturating_add(1);
                if steps > MAX_LOOKUP_STEPS {
                    break;
                }
            }
        }
        steps
    }

    /// Returns whether a table holds a colour glyph for `glyph`. A record
    /// with no data, as a bitmap face keeps for a space, covers nothing.
    fn covers(&self, glyph: GlyphId) -> bool {
        self.colr
            .as_ref()
            .is_some_and(|colr| colr.get(glyph).is_some())
            || self.sbix.as_ref().is_some_and(|sbix| {
                sbix.strikes().iter().any(|strike| {
                    strike.is_ok_and(|strike| matches!(strike.glyph_data(glyph), Ok(Some(_))))
                })
            })
            || self.cblc.as_ref().is_some_and(|cblc| {
                cblc.bitmap_sizes().iter().any(|size| {
                    size.location(cblc.offset_data(), glyph)
                        .is_ok_and(|location| !location.is_empty())
                })
            })
            || self
                .svg
                .as_ref()
                .is_some_and(|svg| svg.glyph_data(glyph).is_some_and(|data| !data.is_empty()))
    }
}

/// Refuses a font whose colour tables would make a lookup walk too far.
pub(super) fn check_lookups(font: &FontRef<'_>) -> Result<(), ShapeError> {
    ColourTables::read(font).map(|_| ())
}

/// What a drawing refuses of a text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Refused {
    /// The first character that the glyph stands for.
    pub character: char,
    /// The glyph.
    pub glyph: u32,
    /// The paint that it needs and that nothing draws, or what is wrong with
    /// its paints; `None` for a table that the drawing does not read.
    pub paint: Option<&'static str>,
}

/// What a canvas paints in a Type 3 font.
#[derive(Clone, Copy)]
pub struct Painted<'p, 'a> {
    /// The face's COLR table, if it has one that can paint.
    pub colr: Option<&'p Colr<'a>>,
    /// The face's strikes.
    pub bitmaps: &'p Bitmaps<'a>,
}

/// Returns the first character, in the order of `glyphs`, whose glyph is a
/// colour glyph with no outline: what a drawing of `text` in a CIDFont must
/// refuse.
///
/// A glyph that stands only for white space or control characters asks for
/// no ink, and neither does the face's space glyph, which also stands for
/// the characters that the shaper hides. Each distinct glyph is looked up
/// once, and the lookups poll the budget.
pub fn refused_colour_glyph(
    face: &Face,
    text: &str,
    glyphs: &[ShapedGlyph],
    budget: &Budget,
) -> Result<Option<char>, ShapeError> {
    let refusal = refused(face, text, glyphs, budget, None)?;
    Ok(refusal.map(|refusal| refusal.character))
}

/// Returns what [`refused_colour_glyph`] does for a drawing on the canvas,
/// which paints what `painted` can. A COLR glyph whose paints skrifa cannot
/// walk refuses even with an outline, since the font is damaged there.
pub fn refused_canvas_glyph(
    face: &Face,
    text: &str,
    glyphs: &[ShapedGlyph],
    painted: Painted<'_, '_>,
    budget: &Budget,
) -> Result<Option<Refused>, ShapeError> {
    refused(face, text, glyphs, budget, Some(painted))
}

fn refused(
    face: &Face,
    text: &str,
    glyphs: &[ShapedGlyph],
    budget: &Budget,
    painted: Option<Painted<'_, '_>>,
) -> Result<Option<Refused>, ShapeError> {
    let font = face.font();
    let Some(tables) = ColourTables::read(&font)? else {
        return Ok(None);
    };
    let outlines = font.outline_glyphs();
    let space = font.charmap().map(' ');
    let mut passed = [0_u64; GLYPH_SET_WORDS];
    for shaped in glyphs {
        // A colour table indexes glyphs with 16 bits.
        let Ok(id) = u16::try_from(shaped.glyph) else {
            continue;
        };
        let (word, bit) = (usize::from(id / 64), 1_u64 << (id % 64));
        if passed[word] & bit != 0 {
            continue;
        }
        budget.poll()?;
        let glyph = GlyphId::from(id);
        if id == 0 || space == Some(glyph) || !tables.covers(glyph) {
            passed[word] |= bit;
            continue;
        }
        let verdict = match painted.and_then(|painted| painted.colr) {
            Some(colr) => colr.check(glyph, budget)?,
            None => None,
        };
        let damaged = matches!(verdict, Some(Verdict::Damaged(_)));
        if !damaged
            && (verdict == Some(Verdict::Paints)
                || painted.is_some_and(|painted| painted.bitmaps.has(glyph))
                || paints(&outlines, glyph))
        {
            passed[word] |= bit;
            continue;
        }
        let stands_for = if shaped.text.is_empty() {
            &shaped.cluster
        } else {
            &shaped.text
        };
        let character = text[stands_for.clone()]
            .chars()
            .find(|character| !character.is_whitespace() && !character.is_control());
        if let Some(character) = character {
            let paint = match verdict {
                Some(Verdict::Cannot(paint) | Verdict::Damaged(paint)) => Some(paint),
                _ => None,
            };
            return Ok(Some(Refused {
                character,
                glyph: u32::from(id),
                paint,
            }));
        }
    }
    Ok(None)
}

/// Returns whether the outline of `glyph` paints anything.
fn paints(outlines: &OutlineGlyphCollection<'_>, glyph: GlyphId) -> bool {
    let Some(outline) = outlines.get(glyph) else {
        return false;
    };
    let mut pen = PaintPen::default();
    let settings = DrawSettings::unhinted(Size::unscaled(), LocationRef::default());
    outline.draw(settings, &mut pen).is_ok() && pen.paints
}

/// Finds a contour that encloses something. One whose points all lie on a
/// line paints nothing, as the two points that a bitmap face may keep for a
/// glyph's box do.
#[derive(Default)]
struct PaintPen {
    start: Option<(f32, f32)>,
    towards: Option<(f32, f32)>,
    paints: bool,
}

impl PaintPen {
    fn add(&mut self, x: f32, y: f32) {
        let Some(start) = self.start else {
            self.start = Some((x, y));
            return;
        };
        let (dx, dy) = (x - start.0, y - start.1);
        match self.towards {
            None if dx != 0.0 || dy != 0.0 => self.towards = Some((dx, dy)),
            Some(towards) if towards.0 * dy != towards.1 * dx => self.paints = true,
            _ => {}
        }
    }
}

impl OutlinePen for PaintPen {
    fn move_to(&mut self, x: f32, y: f32) {
        self.start = Some((x, y));
        self.towards = None;
    }
    fn line_to(&mut self, x: f32, y: f32) {
        self.add(x, y);
    }
    fn quad_to(&mut self, cx0: f32, cy0: f32, x: f32, y: f32) {
        self.add(cx0, cy0);
        self.add(x, y);
    }
    fn curve_to(&mut self, cx0: f32, cy0: f32, cx1: f32, cy1: f32, x: f32, y: f32) {
        self.add(cx0, cy0);
        self.add(cx1, cy1);
        self.add(x, y);
    }
    fn close(&mut self) {}
}
