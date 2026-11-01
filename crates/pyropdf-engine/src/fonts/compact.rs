//! Compacting an embedded face's program at save (DESIGN.md section 7):
//! the glyphs that the document drew, renumbered from zero, in the program
//! that a CIDFont embeds. A TrueType program keeps its outlines and
//! instructions; a CFF one becomes a bare CID-keyed CFF whose charset maps
//! each glyph back to its old ID, the CID that the content shows.

use std::collections::BTreeSet;

use skrifa::instance::{LocationRef, Size};
use skrifa::outline::{DrawSettings, OutlinePen};
use skrifa::raw::TableProvider;
use skrifa::{FontRef, GlyphId, MetadataProvider};

use super::{cff, sfnt};

/// The program of a face compacted to some of its glyphs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Compact {
    /// A TrueType program (`FontFile2`), or a bare CID-keyed CFF
    /// (`FontFile3` with subtype `CIDFontType0C`).
    pub data: Vec<u8>,
    /// Whether `data` is CFF.
    pub cff: bool,
    /// The old glyph ID of each new glyph, `.notdef` first: the CIDToGIDMap
    /// of a TrueType program inverts it, and a CFF one's charset is it.
    pub glyphs: Vec<u16>,
}

/// Returns the face `program`, as [`super::embed::Face`] writes it, with
/// only `.notdef`, the glyphs of `keep`, and the glyphs that those draw;
/// `None` when it is not a program that this reads, which leaves it whole.
/// `name` names a CFF result.
#[must_use]
pub fn compact(program: &[u8], keep: &[u32], name: &str) -> Option<Compact> {
    let keep: BTreeSet<u32> = keep.iter().copied().collect();
    if let Some((data, glyphs)) = sfnt::compact(program, &keep) {
        return Some(Compact {
            data,
            cff: false,
            glyphs,
        });
    }
    let font = FontRef::new(program).ok()?;
    font.cff().ok()?;
    let count = u32::from(font.maxp().ok()?.num_glyphs());
    let scale = 1000.0 / f64::from(font.head().ok()?.units_per_em().max(1));
    let outlines = font.outline_glyphs();
    let glyphs: Vec<u16> = std::iter::once(0)
        .chain(keep)
        .filter(|&glyph| glyph < count)
        .filter_map(|glyph| u16::try_from(glyph).ok())
        .collect::<BTreeSet<u16>>()
        .into_iter()
        .collect();
    let mut charstrings = Vec::new();
    charstrings.try_reserve(glyphs.len()).ok()?;
    for &glyph in &glyphs {
        let mut pen = Charstring {
            scale,
            ..Charstring::default()
        };
        // A glyph that does not draw is empty.
        if let Some(outline) = outlines.get(GlyphId::new(u32::from(glyph))) {
            let settings = DrawSettings::unhinted(Size::unscaled(), LocationRef::default());
            if outline.draw(settings, &mut pen).is_err() {
                pen.data.clear();
            }
        }
        if pen.overflow {
            return None;
        }
        pen.data.push(ENDCHAR);
        charstrings.push(pen.data);
    }
    let name = if name.is_empty() { "Subset" } else { name };
    Some(Compact {
        data: cff::cid_keyed(name, &charstrings, &glyphs)?,
        cff: true,
        glyphs,
    })
}

const RLINETO: u8 = 5;
const RRCURVETO: u8 = 8;
const ENDCHAR: u8 = 14;
const RMOVETO: u8 = 21;

/// A pen that writes a Type 2 charstring without hints (5177), its
/// coordinates scaled to 1000 units per em and kept to 16.16 fixed.
#[derive(Default)]
struct Charstring {
    data: Vec<u8>,
    scale: f64,
    /// The current point, in 1/65536 units.
    at: (i64, i64),
    overflow: bool,
}

impl Charstring {
    fn fixed(&self, value: f32) -> i64 {
        // Rounding to 16.16 is the CFF's own precision; a coordinate
        // out of its range overflows instead of wrapping.
        let scaled = (f64::from(value) * self.scale * 65536.0).round();
        if scaled.is_finite() && scaled.abs() < 2_147_483_648.0 {
            #[expect(
                clippy::cast_possible_truncation,
                reason = "the value is finite and inside i32's range"
            )]
            let fixed = scaled as i64;
            fixed
        } else {
            i64::MAX
        }
    }

    fn number(&mut self, value: i64) {
        let Ok(value) = i32::try_from(value) else {
            self.overflow = true;
            return;
        };
        if value % 65536 != 0 {
            self.data.push(255);
            self.data.extend_from_slice(&value.to_be_bytes());
            return;
        }
        let integer = value / 65536;
        match integer {
            -107..=107 => self.data.push(u8::try_from(integer + 139).unwrap_or(139)),
            108..=1131 => {
                let rest = integer - 108;
                self.data
                    .push(u8::try_from(rest / 256 + 247).unwrap_or(247));
                self.data.push(u8::try_from(rest % 256).unwrap_or(0));
            }
            -1131..=-108 => {
                let rest = -integer - 108;
                self.data
                    .push(u8::try_from(rest / 256 + 251).unwrap_or(251));
                self.data.push(u8::try_from(rest % 256).unwrap_or(0));
            }
            _ => {
                self.data.push(28);
                self.data
                    .extend_from_slice(&i16::try_from(integer).unwrap_or(0).to_be_bytes());
            }
        }
    }

    fn points(&mut self, points: &[(f32, f32)], op: u8) {
        for &(x, y) in points {
            let (x, y) = (self.fixed(x), self.fixed(y));
            let (dx, dy) = (x.saturating_sub(self.at.0), y.saturating_sub(self.at.1));
            self.number(dx);
            self.number(dy);
            self.at = (x, y);
        }
        self.data.push(op);
    }
}

impl OutlinePen for Charstring {
    fn move_to(&mut self, x: f32, y: f32) {
        self.points(&[(x, y)], RMOVETO);
    }

    fn line_to(&mut self, x: f32, y: f32) {
        self.points(&[(x, y)], RLINETO);
    }

    fn quad_to(&mut self, cx0: f32, cy0: f32, x: f32, y: f32) {
        // A CFF program draws cubics; the conversion is exact.
        let (x0, y0) = self.current();
        let c1 = (x0 + 2.0 / 3.0 * (cx0 - x0), y0 + 2.0 / 3.0 * (cy0 - y0));
        let c2 = (x + 2.0 / 3.0 * (cx0 - x), y + 2.0 / 3.0 * (cy0 - y));
        self.points(&[c1, c2, (x, y)], RRCURVETO);
    }

    fn curve_to(&mut self, cx0: f32, cy0: f32, cx1: f32, cy1: f32, x: f32, y: f32) {
        self.points(&[(cx0, cy0), (cx1, cy1), (x, y)], RRCURVETO);
    }

    fn close(&mut self) {}
}

impl Charstring {
    /// The current point in font units.
    #[expect(
        clippy::cast_precision_loss,
        clippy::cast_possible_truncation,
        reason = "a 16.16 coordinate fits f32's use here"
    )]
    fn current(&self) -> (f32, f32) {
        let unscale = |value: i64| (value as f64 / 65536.0 / self.scale) as f32;
        (unscale(self.at.0), unscale(self.at.1))
    }
}

#[cfg(test)]
mod tests;
