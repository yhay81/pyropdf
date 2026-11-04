//! Field values drawn in an embedded OpenType face: the Type0 font of the
//! document's form font, whose CIDs are the face's glyph IDs (DESIGN.md
//! section 7). The face shapes each line with harfrust, and the lines are
//! laid out and fitted as the Standard 14 appearances are.
//!
//! The ToUnicode CMap of the font and its `W` array are written by the
//! caller: a first pass collects the glyphs that the appearance shows,
//! with their texts, and a second draws it once the font records them.

use std::collections::HashMap;
use std::iter::once;

use harfrust::unicode::{Decomposed, combining_class, decompose};
use pyropdf_store::budget::{Budget, BudgetVec};
use pyropdf_store::value::Reference;
use skrifa::GlyphId;

use super::{WidgetError, fmt};
use crate::fonts::colour::refused_colour_glyph;
use crate::fonts::embed::{Face, Measure};
use crate::fonts::shape::{ShapeError, ShapedGlyph, ShapingFace};
use crate::standard::{self, LayoutError, Placement, TextBox};

/// The name of the face's font in an appearance's resources.
pub(super) const RESOURCE: &str = "PyroForm";
/// The ToUnicode text of a glyph without text of its own, such as a dot that a
/// face draws apart from its letter: `_NO_TEXT` of `python/pyropdf/_fonts.py`.
const NO_TEXT: &str = "\u{2060}";
/// How far, in thousandths of the em, the last glyph of `/ActualText` may end
/// from its cluster's end: readers look for a word gap from about a tenth.
const CLUSTER_END_TOLERANCE: f64 = 50.0;

/// The Type0 font that an appearance names, and the text that its
/// ToUnicode CMap maps each glyph to.
pub struct FaceFont<'m> {
    pub font: Reference,
    pub mapped: &'m HashMap<u32, String>,
}

/// The first character of a value that a face cannot draw.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Undrawable {
    /// The face has no glyph for it.
    NoGlyph(char),
    /// Its glyph is a colour glyph with no outline, which would draw nothing
    /// under a value that reads back (DESIGN.md section 7).
    NoOutline(char),
}

/// An embedded face that draws field values, and the glyphs that it drew.
pub struct FaceText<'f> {
    face: &'f Face,
    shaper: ShapingFace,
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
        let shaper = ShapingFace::of(face)
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

    pub(crate) fn font(&self) -> Option<Reference> {
        self.font.as_ref().map(|font| font.font)
    }

    fn shape(&self, text: &str) -> Result<BudgetVec<'f, ShapedGlyph>, LayoutError> {
        self.shaper
            .shape(text, self.budget)
            .map_err(|error| match error {
                ShapeError::Limit(refusal) => refusal.into(),
                // A face that reads fails to shape only when its own
                // tables ask for more than the shaper allows.
                ShapeError::Font(_) => LayoutError::InvalidGeometry,
            })
    }

    /// Returns the first character of `value`, other than a control
    /// character, that the face has no glyph for, or else the first whose
    /// glyph is a colour glyph with no outline.
    pub(crate) fn undrawable(&self, value: &str) -> Result<Option<Undrawable>, ShapeError> {
        let glyphs = self.shaper.shape(value, self.budget)?;
        let missing = glyphs
            .iter()
            .filter(|glyph| glyph.glyph == 0)
            .filter_map(|glyph| value[glyph.cluster.clone()].chars().next())
            .find(|character| !character.is_control());
        if let Some(character) = missing {
            return Ok(Some(Undrawable::NoGlyph(character)));
        }
        let refused = refused_colour_glyph(self.face, value, &glyphs, self.budget)?;
        Ok(refused.map(Undrawable::NoOutline))
    }

    /// Refuses `value` when the face cannot draw one of its characters.
    pub(super) fn check(&self, value: &str) -> Result<(), WidgetError> {
        let undrawable = self.undrawable(value).map_err(|error| match error {
            ShapeError::Limit(refusal) => WidgetError::from(refusal),
            ShapeError::Font(reason) => {
                WidgetError::Malformed(format!("invalid font: {reason}").into())
            }
        })?;
        let message = match undrawable {
            None => return Ok(()),
            Some(Undrawable::NoGlyph(character)) => format!(
                "{} does not contain all glyphs of the text: no glyph for U+{:04X} ({character:?})",
                self.face.full_name,
                u32::from(character)
            ),
            Some(Undrawable::NoOutline(character)) => format!(
                "{} cannot draw U+{:04X} ({character:?}): its glyph is a colour glyph with no outline",
                self.face.full_name,
                u32::from(character)
            ),
        };
        Err(WidgetError::Malformed(message.into()))
    }

    /// Lays out `text` in a box at `font_size`, the lines `line_height` em
    /// apart and placed by the face's ascender and descender.
    pub(crate) fn layout<'t>(
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

    /// Appends the operators that show `line` at `size` as shaped glyphs, as
    /// `_GlyphRun` of `python/pyropdf/_canvas.py` draws canvas text: `TJ`
    /// adjustments move the pen from each glyph's width in `W` to its shaped
    /// advance and by its offset, `Ts` raises it, and a cluster whose glyphs
    /// do not read as its text in the ToUnicode CMap is shown inside one
    /// `/ActualText`.
    pub(crate) fn show(
        &mut self,
        out: &mut BudgetVec<'_, u8>,
        line: &str,
        size: f64,
    ) -> Result<(), LayoutError> {
        let glyphs = self.shape(line)?;
        let mut run = GlyphRun {
            out,
            items: BudgetVec::new(self.budget),
            pens: BudgetVec::new(self.budget),
            measure: &self.measure,
            size,
            em: self.em,
            pending: 0.0,
            rise: 0,
        };
        for cluster in glyphs.chunk_by(|a, b| a.cluster == b.cluster) {
            let text = &line[cluster[0].cluster.clone()];
            let spanned = match &self.font {
                Some(font) => !reads_as(cluster, text, font.mapped, self.budget)?,
                None => {
                    for glyph in cluster.iter().filter(|glyph| glyph.glyph != 0) {
                        let own = &line[glyph.text.clone()];
                        let own = if own.is_empty() { NO_TEXT } else { own };
                        self.drawn
                            .try_push((glyph.glyph, copy_text(own, self.budget)?))?;
                    }
                    false
                }
            };
            run.show_cluster(text, cluster, spanned)?;
        }
        flush(run.out, &mut run.items)?;
        if run.rise != 0 {
            run.out.try_extend_from_slice(b"0 Ts\n")?;
        }
        Ok(())
    }
}

/// Writes one line's shaped glyphs as `TJ` arrays.
struct GlyphRun<'r, 'o, 'f> {
    out: &'r mut BudgetVec<'o, u8>,
    items: BudgetVec<'f, u8>,
    /// The pen before each glyph of the current cluster and after its last,
    /// in thousandths of the em.
    pens: BudgetVec<'f, f64>,
    measure: &'r Measure<'f>,
    size: f64,
    em: f64,
    /// From the pen to the text position, in `TJ` units: positive moves left.
    pending: f64,
    rise: i32,
}

impl GlyphRun<'_, '_, '_> {
    fn show_cluster(
        &mut self,
        text: &str,
        cluster: &[ShapedGlyph],
        spanned: bool,
    ) -> Result<(), LayoutError> {
        let scale = 1000.0 / self.em;
        // Readers take the box of `/ActualText` from the glyphs inside: MuPDF
        // from the first glyph, Xpdf and poppler from the first glyph's origin
        // to the last one's end. So a glyph with a width comes first, not the
        // mark that a right-to-left run places before its base, and the last
        // glyph leaves the text position at the cluster's end. Each glyph still
        // draws where the shaper put it (`evidence/cluster-text`).
        let lead = if spanned {
            cluster
                .iter()
                .position(|glyph| glyph.glyph != 0 && glyph.x_advance != 0)
                .unwrap_or(0)
        } else {
            0
        };
        let last = (0..cluster.len())
            .rev()
            .find(|&index| index != lead)
            .unwrap_or(lead);
        let ends_span = spanned && cluster.iter().filter(|glyph| glyph.glyph != 0).count() > 1;
        self.pens.clear();
        self.pens.try_reserve_exact(cluster.len() + 1)?;
        let mut end = 0.0;
        self.pens.try_push(end)?;
        for glyph in cluster {
            end += f64::from(glyph.x_advance) * scale;
            self.pens.try_push(end)?;
        }
        if spanned {
            flush(self.out, &mut self.items)?;
            self.out
                .try_extend_from_slice(b"/Span <</ActualText <FEFF")?;
            for unit in text.encode_utf16() {
                self.out
                    .try_extend_from_slice(format!("{unit:04X}").as_bytes())?;
            }
            self.out.try_extend_from_slice(b">>> BDC\n")?;
        }
        let mut at = 0.0;
        for index in once(lead).chain((0..cluster.len()).filter(|&index| index != lead)) {
            let glyph = &cluster[index];
            self.pending -= self.pens[index] - at;
            at = self.pens[index];
            if glyph.glyph == 0 {
                // A control character without a glyph only advances.
                continue;
            }
            let offset = f64::from(glyph.x_offset) * scale;
            let width = self.measure.advance(GlyphId::new(glyph.glyph)) * scale;
            let follow = if ends_span && index == last {
                end - (at + offset + width)
            } else {
                0.0
            };
            let follow = if follow.abs() > CLUSTER_END_TOLERANCE {
                follow
            } else {
                0.0
            };
            self.show_glyph(glyph, offset, width, follow)?;
        }
        self.pending -= end - at;
        if spanned {
            flush(self.out, &mut self.items)?;
            self.out.try_extend_from_slice(b"EMC\n")?;
        }
        Ok(())
    }

    /// Shows one glyph at the pen, and moves the text position `follow`
    /// further through `Tc`.
    fn show_glyph(
        &mut self,
        glyph: &ShapedGlyph,
        offset: f64,
        width: f64,
        follow: f64,
    ) -> Result<(), LayoutError> {
        if glyph.y_offset != self.rise {
            flush(self.out, &mut self.items)?;
            self.rise = glyph.y_offset;
            let raise = format!("{} Ts\n", fmt(f64::from(self.rise) * self.size / self.em));
            self.out.try_extend_from_slice(raise.as_bytes())?;
        }
        if follow != 0.0 {
            flush(self.out, &mut self.items)?;
            let spacing = format!("{} Tc\n", fmt(follow * self.size / 1000.0));
            self.out.try_extend_from_slice(spacing.as_bytes())?;
        }
        if self.pending - offset != 0.0 {
            if !self.items.is_empty() {
                self.items.try_push(b' ')?;
            }
            self.items
                .try_extend_from_slice(fmt(self.pending - offset).as_bytes())?;
        }
        if !self.items.is_empty() {
            self.items.try_push(b' ')?;
        }
        self.items
            .try_extend_from_slice(format!("<{:04X}>", glyph.glyph).as_bytes())?;
        self.pending = width + offset + follow;
        if follow != 0.0 {
            flush(self.out, &mut self.items)?;
            self.out.try_extend_from_slice(b"0 Tc\n")?;
        }
        Ok(())
    }
}

/// Returns whether the CMap's texts of a cluster's glyphs read as its text, in
/// canonical order of its marks.
fn reads_as(
    cluster: &[ShapedGlyph],
    text: &str,
    mapped: &HashMap<u32, String>,
    budget: &Budget,
) -> Result<bool, pyropdf_store::LimitExceeded> {
    if cluster.iter().all(|glyph| glyph.glyph == 0) {
        return Ok(true);
    }
    let count = cluster.len();
    let rtl = cluster[0].rtl;
    let texts = (0..count)
        .map(|index| &cluster[if rtl { count - 1 - index } else { index }])
        .filter(|glyph| glyph.glyph != 0)
        .flat_map(|glyph| mapped.get(&glyph.glyph).map_or("", String::as_str).chars());
    if texts.clone().eq(text.chars()) {
        return Ok(true);
    }
    Ok(*nfd(texts, budget)? == *nfd(text.chars(), budget)?)
}

/// Returns `characters` in Unicode's canonical decomposition, NFD.
fn nfd<'b>(
    characters: impl Iterator<Item = char>,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, u32>, pyropdf_store::LimitExceeded> {
    let mut decomposed = BudgetVec::new(budget);
    for character in characters {
        push_decomposed(&mut decomposed, u32::from(character))?;
    }
    // Stable, so that marks of one class keep their order.
    for marks in
        decomposed.chunk_by_mut(|&a, &b| combining_class(a) != 0 && combining_class(b) != 0)
    {
        marks.sort_by_key(|&mark| combining_class(mark));
    }
    Ok(decomposed)
}

/// Appends the full canonical decomposition of `codepoint`. The recursion
/// follows Unicode's table, not the input, and ends within a few steps.
fn push_decomposed(
    decomposed: &mut BudgetVec<'_, u32>,
    codepoint: u32,
) -> Result<(), pyropdf_store::LimitExceeded> {
    match decompose(codepoint) {
        None => decomposed.try_push(codepoint),
        Some(Decomposed::Singleton(only)) => push_decomposed(decomposed, only),
        Some(Decomposed::Pair(first, second)) => {
            push_decomposed(decomposed, first)?;
            push_decomposed(decomposed, second)
        }
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

/// Copies one glyph's text with its bytes charged for the operation.
fn copy_text(text: &str, budget: &Budget) -> Result<String, pyropdf_store::LimitExceeded> {
    let mut bytes = BudgetVec::new(budget);
    bytes.try_reserve_exact(text.len())?;
    bytes.try_extend_from_slice(text.as_bytes())?;
    // The bytes came from a valid UTF-8 string and were copied unchanged.
    Ok(String::from_utf8(bytes.into_vec()).expect("copied text is UTF-8"))
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use pyropdf_store::budget::{Budget, LimitCode};

    use super::{ShapedGlyph, copy_text, nfd, reads_as};

    fn decomposed(text: &str) -> Vec<u32> {
        nfd(text.chars(), &Budget::default()).unwrap().into_vec()
    }

    #[test]
    fn nfd_decomposes_fully_and_orders_marks_by_class() {
        // A singleton, then a pair whose first part decomposes again.
        assert_eq!(decomposed("\u{212B}"), [0x41, 0x30A]);
        assert_eq!(decomposed("\u{1E69}"), [0x73, 0x323, 0x307]);
        // A pair of marks, and a Hangul syllable.
        assert_eq!(decomposed("\u{0344}"), [0x308, 0x301]);
        assert_eq!(decomposed("\u{D55C}"), [0x1112, 0x1161, 0x11AB]);
        // Marks of classes 230 and 220 swap, a base stops the run, and marks
        // of one class keep their order.
        assert_eq!(decomposed("s\u{307}\u{323}"), [0x73, 0x323, 0x307]);
        assert_eq!(
            decomposed("\u{307}a\u{301}\u{300}"),
            [0x307, 0x61, 0x301, 0x300]
        );
    }

    /// Returns whether glyphs 1, 2, ... of a cluster read as `text` with the
    /// CMap's `mapped` texts.
    fn cluster_reads_as(text: &str, glyphs: &[u32], rtl: bool, mapped: &[(u32, &str)]) -> bool {
        let cluster: Vec<_> = glyphs
            .iter()
            .map(|&glyph| ShapedGlyph {
                glyph,
                x_advance: 0,
                y_advance: 0,
                x_offset: 0,
                y_offset: 0,
                cluster: 0..text.len(),
                text: 0..0,
                rtl,
            })
            .collect();
        let mapped: HashMap<_, _> = mapped
            .iter()
            .map(|&(glyph, text)| (glyph, text.to_owned()))
            .collect();
        reads_as(&cluster, text, &mapped, &Budget::default()).unwrap()
    }

    #[test]
    fn a_cluster_reads_as_its_text_in_logical_order_and_canonical_form() {
        let mapped = [
            (1, "a"),
            (2, "\u{307}"),
            (3, "\u{323}"),
            (4, "\u{2060}"),
            (5, "b"),
        ];
        assert!(cluster_reads_as("a", &[1], false, &mapped));
        assert!(cluster_reads_as("a\u{307}", &[1, 2], false, &mapped));
        // A right-to-left run shows the glyphs in reverse.
        assert!(cluster_reads_as("a\u{307}", &[2, 1], true, &mapped));
        assert!(!cluster_reads_as("a\u{307}", &[2, 1], false, &mapped));
        // Marks in another canonical order, and a precomposed character.
        assert!(cluster_reads_as(
            "a\u{307}\u{323}",
            &[1, 3, 2],
            false,
            &mapped
        ));
        assert!(cluster_reads_as("\u{227}", &[1, 2], false, &mapped));
        // Bases in another order are other text, as is a mapped glyph's text
        // elsewhere, a glyph without text of its own, and a glyph unmapped.
        assert!(!cluster_reads_as("ab", &[5, 1], false, &mapped));
        assert!(!cluster_reads_as("b", &[1], false, &mapped));
        assert!(!cluster_reads_as("a", &[1, 4], false, &mapped));
        assert!(!cluster_reads_as("a", &[6], false, &mapped));
        // A cluster of control characters without glyphs draws nothing to read.
        assert!(cluster_reads_as("\u{200D}", &[0], false, &mapped));
    }

    #[test]
    fn nfd_counts_against_memory_limit() {
        let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(3));
        let refusal = nfd("\u{212B}".chars(), &budget).unwrap_err();
        assert_eq!(refusal.code(), LimitCode::MemorySize);
    }

    #[test]
    fn glyph_text_copies_count_together_against_memory_limit() {
        let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(11));
        let first = copy_text("日本", &budget).unwrap();
        assert_eq!(first, "日本");
        assert_eq!(budget.used(LimitCode::MemorySize), 6);
        let error = copy_text("日本", &budget).unwrap_err();
        assert_eq!(error.code(), LimitCode::MemorySize);
        assert_eq!(budget.used(LimitCode::MemorySize), 6);
        assert_eq!(first, "日本");
    }
}
