//! Compacting an embedded face's program at save (DESIGN.md section 7):
//! the glyphs that the document drew, renumbered from zero, in the program
//! that a CIDFont embeds. A TrueType program keeps its outlines and
//! instructions; a CFF one becomes a bare CID-keyed CFF whose charset maps
//! each glyph back to its old ID, the CID that the content shows, and which
//! keeps each glyph's hints and the hint values of its Private DICT, so that
//! a viewer hints the text as it hints the face.

use std::collections::btree_map::Entry;
use std::collections::{BTreeMap, BTreeSet};

use skrifa::raw::TableProvider;
use skrifa::raw::ps::Transform;
use skrifa::raw::ps::cff::{CffFontRef, Subfont};
use skrifa::raw::ps::charstring::{CommandSink, TransformSink};
use skrifa::raw::types::{Fixed, Tag};
use skrifa::{FontRef, GlyphId};

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
    let table = font.table_data(Tag::new(b"CFF "))?;
    let table = table.as_bytes();
    let count = u32::from(font.maxp().ok()?.num_glyphs());
    let units_per_em = font.head().ok()?.units_per_em();
    let scale = 1000.0 / f64::from(units_per_em.max(1));
    let source = CffFontRef::new_cff(table, 0, Some(i32::from(units_per_em))).ok()?;
    let glyphs: Vec<u16> = std::iter::once(0)
        .chain(keep)
        .filter(|&glyph| glyph < count)
        .filter_map(|glyph| u16::try_from(glyph).ok())
        .collect::<BTreeSet<u16>>()
        .into_iter()
        .collect();
    let mut charstrings = Vec::new();
    charstrings.try_reserve(glyphs.len()).ok()?;
    let mut fds = Vec::new();
    fds.try_reserve(glyphs.len()).ok()?;
    // The hint values of the result's font DICTs, each only once, and for
    // each font DICT of the face, which of them it became and how it draws.
    let mut privates: Vec<Vec<u8>> = Vec::new();
    let mut subfonts: BTreeMap<u16, (u8, Option<(Subfont, Transform)>)> = BTreeMap::new();
    for &glyph in &glyphs {
        let glyph = GlyphId::new(u32::from(glyph));
        let index = source.subfont_index(glyph).unwrap_or(0);
        let (fd, subfont) = match subfonts.entry(index) {
            Entry::Occupied(entry) => *entry.get(),
            Entry::Vacant(entry) => {
                let subfont = source
                    .subfont(index, &[])
                    .ok()
                    .map(|subfont| (subfont, source.transform(&subfont, None)));
                let values = subfont
                    .and_then(|(_, transform)| hint_scale(&transform, scale))
                    .and_then(|scale| cff::hint_values(table, usize::from(index), scale))
                    .unwrap_or_default();
                let fd = privates
                    .iter()
                    .position(|private| *private == values)
                    .unwrap_or_else(|| {
                        privates.push(values);
                        privates.len() - 1
                    });
                *entry.insert((u8::try_from(fd).ok()?, subfont))
            }
        };
        fds.push(fd);
        // A glyph that does not draw is empty.
        charstrings.push(match subfont {
            Some((subfont, transform)) => charstring(&source, &subfont, glyph, transform, scale)?,
            None => vec![ENDCHAR],
        });
    }
    let name = if name.is_empty() { "Subset" } else { name };
    Some(Compact {
        data: cff::cid_keyed(name, &charstrings, &glyphs, &fds, &privates)?,
        cff: true,
        glyphs,
    })
}

/// Returns the scale from the charstrings of a subfont to the result's
/// units, given `scale` per font unit; `None` when its font matrix does more
/// than scale, since stems and hint values then no longer describe the
/// outline that the matrix draws.
fn hint_scale(transform: &Transform, scale: f64) -> Option<f64> {
    let matrix = transform.matrix;
    if [matrix.yx, matrix.xy, matrix.dx, matrix.dy] != [Fixed::ZERO; 4] || matrix.xx != matrix.yy {
        return None;
    }
    // The scale of a transform is in 26.6 per font unit.
    let scale =
        scale * matrix.yy.to_f64() * transform.scale.map_or(1.0, |scale| scale.to_f64() / 64.0);
    (scale.is_finite() && scale > 0.0).then_some(scale)
}

/// Returns the charstring of `glyph` for the result; `None` when a value is
/// out of the range of a charstring's numbers.
fn charstring(
    source: &CffFontRef<'_>,
    subfont: &Subfont,
    glyph: GlyphId,
    transform: Transform,
    scale: f64,
) -> Option<Vec<u8>> {
    let hinted = hint_scale(&transform, scale);
    let scale = hinted.unwrap_or(scale);
    let mut pen = Charstring::new(scale, hinted.is_some());
    // Stems describe the charstring's own units, so the outline stays in
    // them too, unless a font matrix has to draw it.
    let mut drawn = if hinted.is_some() {
        source.evaluate_charstring(subfont, glyph, &[], &mut pen)
    } else {
        let mut sink = TransformSink::new(&mut pen, transform);
        source.evaluate_charstring(subfont, glyph, &[], &mut sink)
    };
    // A viewer hints each component of a `seac` by its own stems, and a
    // charstring without the `seac` holds one set, so its outline goes alone.
    if pen.seac {
        pen = Charstring::new(scale, false);
        drawn = source.evaluate_charstring(subfont, glyph, &[], &mut pen);
    }
    if pen.overflow {
        return None;
    }
    Some(if drawn.is_ok() {
        pen.finish_glyph()
    } else {
        vec![ENDCHAR]
    })
}

const HSTEM: u8 = 1;
const VSTEM: u8 = 3;
const RLINETO: u8 = 5;
const RRCURVETO: u8 = 8;
const ENDCHAR: u8 = 14;
const HSTEMHM: u8 = 18;
const HINTMASK: u8 = 19;
const CNTRMASK: u8 = 20;
const RMOVETO: u8 = 21;
const VSTEMHM: u8 = 23;
/// The stems of one operator: a charstring's stack holds 48 numbers (5177,
/// appendix B).
const MAX_STEMS: usize = 24;
/// The width that marks a stem as the ghost of a top edge, and of a bottom
/// edge (5177, section 4.3), in 1/65536 units.
const GHOST_TOP: i32 = -20 * 65536;
const GHOST_BOTTOM: i32 = -21 * 65536;

/// A sink that writes a Type 2 charstring (5177), its values scaled to 1000
/// units per em and kept to 16.16 fixed.
struct Charstring {
    data: Vec<u8>,
    scale: f64,
    /// The current point, in 1/65536 units.
    at: (i64, i64),
    overflow: bool,
    /// Whether it writes stems and masks.
    hints: bool,
    /// Whether the charstring draws through a `seac`.
    seac: bool,
    /// How many stems wait for their operator, whether they are vertical,
    /// and where the last of them ends, in 1/65536 units.
    stems: usize,
    vertical: bool,
    stem_at: i64,
    /// Where the stem operators are, and whether a mask follows them, which
    /// asks for the operators that say so.
    stem_operators: Vec<usize>,
    masked: bool,
}

impl Charstring {
    fn new(scale: f64, hints: bool) -> Self {
        Self {
            data: Vec::new(),
            scale,
            at: (0, 0),
            overflow: false,
            hints,
            seac: false,
            stems: 0,
            vertical: false,
            stem_at: 0,
            stem_operators: Vec::new(),
            masked: false,
        }
    }

    fn fixed(&self, value: Fixed) -> i64 {
        // Rounding to 16.16 is the CFF's own precision; a coordinate
        // out of its range overflows instead of wrapping.
        let scaled = (f64::from(value.to_bits()) * self.scale).round();
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

    fn points(&mut self, points: &[(Fixed, Fixed)], op: u8) {
        self.end_stems();
        for &(x, y) in points {
            let (x, y) = (self.fixed(x), self.fixed(y));
            let (dx, dy) = (x.saturating_sub(self.at.0), y.saturating_sub(self.at.1));
            self.number(dx);
            self.number(dy);
            self.at = (x, y);
        }
        self.data.push(op);
    }

    fn stem(&mut self, vertical: bool, start: Fixed, end: Fixed) {
        if !self.hints {
            return;
        }
        if vertical != self.vertical || self.stems == MAX_STEMS {
            self.end_stems();
        }
        self.vertical = vertical;
        // The width of a ghost is a mark, which no scale changes; the edge
        // that it hints is its start at the top and its end at the bottom.
        let (start, end) = match end.to_bits().wrapping_sub(start.to_bits()) {
            GHOST_TOP => {
                let start = self.fixed(start);
                (start, start.saturating_add(i64::from(GHOST_TOP)))
            }
            GHOST_BOTTOM => {
                let end = self.fixed(end);
                (end.saturating_sub(i64::from(GHOST_BOTTOM)), end)
            }
            _ => (self.fixed(start), self.fixed(end)),
        };
        self.number(start.saturating_sub(self.stem_at));
        self.number(end.saturating_sub(start));
        self.stem_at = end;
        self.stems += 1;
    }

    /// Writes the operator of the stems that wait for one. Each operator
    /// starts counting from zero again.
    fn end_stems(&mut self) {
        if self.stems > 0 {
            self.stem_operators.push(self.data.len());
            self.data.push(if self.vertical { VSTEM } else { HSTEM });
            self.stems = 0;
            self.stem_at = 0;
        }
    }

    fn mask(&mut self, op: u8, mask: &[u8]) {
        if !self.hints {
            return;
        }
        // Vertical stems right before a mask need no operator of their own.
        if self.vertical {
            self.stems = 0;
            self.stem_at = 0;
        } else {
            self.end_stems();
        }
        self.masked = true;
        self.data.push(op);
        self.data.extend_from_slice(mask);
    }

    fn finish_glyph(mut self) -> Vec<u8> {
        self.end_stems();
        if self.masked {
            for at in self.stem_operators {
                self.data[at] = if self.data[at] == VSTEM {
                    VSTEMHM
                } else {
                    HSTEMHM
                };
            }
        }
        self.data.push(ENDCHAR);
        self.data
    }
}

impl CommandSink for Charstring {
    fn move_to(&mut self, x: Fixed, y: Fixed) {
        self.points(&[(x, y)], RMOVETO);
    }

    fn line_to(&mut self, x: Fixed, y: Fixed) {
        self.points(&[(x, y)], RLINETO);
    }

    fn curve_to(&mut self, cx0: Fixed, cy0: Fixed, cx1: Fixed, cy1: Fixed, x: Fixed, y: Fixed) {
        self.points(&[(cx0, cy0), (cx1, cy1), (x, y)], RRCURVETO);
    }

    fn close(&mut self) {}

    fn hstem(&mut self, y: Fixed, dy: Fixed) {
        self.stem(false, y, dy);
    }

    fn vstem(&mut self, x: Fixed, dx: Fixed) {
        self.stem(true, x, dx);
    }

    fn hint_mask(&mut self, mask: &[u8]) {
        self.mask(HINTMASK, mask);
    }

    fn counter_mask(&mut self, mask: &[u8]) {
        self.mask(CNTRMASK, mask);
    }

    fn clear_hints(&mut self) {
        self.seac = self.hints;
    }
}

#[cfg(test)]
mod tests;
