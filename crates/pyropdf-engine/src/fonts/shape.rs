//! Shaping the text of an embedded face (DESIGN.md section 7).
//!
//! harfrust shapes each run of one direction and one script. The text splits
//! into Unicode bidi runs (UAX #9) in visual order, and each bidi run into
//! script runs, so the glyphs come out in the order a line draws them. Every
//! glyph carries its cluster and the text that its ToUnicode entry maps to: a
//! glyph maps to its own character when each glyph of the cluster is the
//! nominal glyph of one of its characters, as a base and its marks are, and
//! otherwise the cluster's first glyph maps to the whole cluster, as a
//! ligature does.

use std::fmt;
use std::ops::Range;

use harfrust::{Direction, FontRef, Script, ShapeOptions, ShaperData, Tag, UnicodeBuffer};
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use skrifa::MetadataProvider;
use skrifa::charmap::Charmap;
use skrifa::raw::TableProvider;
use unicode_bidi::BidiInfo;
use unicode_script::{Script as UnicodeScript, UnicodeScript as _};

#[cfg(test)]
mod tests;

/// Why a face or its text could not be shaped.
#[derive(Clone, Debug, PartialEq)]
pub enum ShapeError {
    /// The program is not an OpenType font that this reads.
    Font(String),
    /// The operation's budget refused the text or its glyphs.
    Limit(LimitExceeded),
}

impl From<LimitExceeded> for ShapeError {
    fn from(refusal: LimitExceeded) -> Self {
        Self::Limit(refusal)
    }
}

impl fmt::Display for ShapeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Font(reason) => write!(formatter, "the font cannot be read: {reason}"),
            Self::Limit(refusal) => fmt::Display::fmt(refusal, formatter),
        }
    }
}

impl std::error::Error for ShapeError {}

/// One shaped glyph, in font units.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ShapedGlyph {
    /// The glyph ID in the face.
    pub glyph: u32,
    /// How far the pen moves after the glyph.
    pub x_advance: i32,
    /// How far the pen moves up after the glyph.
    pub y_advance: i32,
    /// How far the glyph is drawn right of the pen.
    pub x_offset: i32,
    /// How far the glyph is drawn above the pen.
    pub y_offset: i32,
    /// The byte range of the input that the glyph's cluster covers.
    pub cluster: Range<usize>,
    /// The byte range of the input that the glyph's ToUnicode entry maps
    /// to; empty for a glyph after the first of a cluster that maps whole.
    pub text: Range<usize>,
    /// Whether the glyph belongs to a right-to-left run.
    pub rtl: bool,
}

/// An OpenType face ready to shape, at its default instance.
pub struct ShapingFace<'a> {
    font: FontRef<'a>,
    data: ShaperData,
    charmap: Charmap<'a>,
}

impl<'a> ShapingFace<'a> {
    /// Reads the face at `index` of `program`, a TrueType, OpenType, or
    /// collection file.
    pub fn new(program: &'a [u8], index: u32) -> Result<Self, ShapeError> {
        let font = FontRef::from_index(program, index)
            .map_err(|error| ShapeError::Font(error.to_string()))?;
        if font.head().is_err() {
            return Err(ShapeError::Font("the face has no head table".into()));
        }
        let data = ShaperData::new(&font);
        let charmap = font.charmap();
        Ok(Self {
            font,
            data,
            charmap,
        })
    }

    /// Returns the face's units per em.
    #[must_use]
    pub fn units_per_em(&self) -> u16 {
        self.font.head().map_or(1000, |head| head.units_per_em())
    }

    /// Returns the glyphs of `text` in visual order.
    ///
    /// The text's characters and the glyphs count against
    /// [`LimitCode::ShapedGlyphCount`], and shaping polls the budget between
    /// runs.
    pub fn shape<'b>(
        &self,
        text: &str,
        budget: &'b Budget,
    ) -> Result<BudgetVec<'b, ShapedGlyph>, ShapeError> {
        let chars = u64::try_from(text.chars().count()).unwrap_or(u64::MAX);
        budget.check(LimitCode::ShapedGlyphCount, chars)?;
        budget.poll()?;
        let mut glyphs = BudgetVec::counting(budget, LimitCode::ShapedGlyphCount);
        if text.is_empty() {
            return Ok(glyphs);
        }
        let shaper = self.data.shaper(&self.font).build();
        let mut buffer = UnicodeBuffer::new();
        let bidi = BidiInfo::new(text, None);
        for paragraph in &bidi.paragraphs {
            let (levels, runs) = bidi.visual_runs(paragraph, paragraph.range.clone());
            for run in runs {
                let rtl = levels[run.start].is_rtl();
                let mut scripts = BudgetVec::new(budget);
                script_runs(text, run, &mut scripts)?;
                if rtl {
                    scripts.reverse();
                }
                for &(ref range, script) in scripts.iter() {
                    budget.poll()?;
                    buffer.push_str(&text[range.clone()]);
                    buffer.set_direction(if rtl {
                        Direction::RightToLeft
                    } else {
                        Direction::LeftToRight
                    });
                    if let Some(script) = script {
                        buffer.set_script(script);
                    }
                    buffer.guess_segment_properties();
                    let shaped = shaper.shape(buffer, ShapeOptions::new());
                    self.push_run(text, range.start..range.end, rtl, &shaped, &mut glyphs)?;
                    buffer = shaped.clear();
                }
            }
        }
        Ok(glyphs)
    }

    /// Appends one shaped run, whose clusters are byte offsets into
    /// `text[range]`.
    fn push_run(
        &self,
        text: &str,
        range: Range<usize>,
        rtl: bool,
        shaped: &harfrust::GlyphBuffer,
        glyphs: &mut BudgetVec<'_, ShapedGlyph>,
    ) -> Result<(), ShapeError> {
        let infos = shaped.glyph_infos();
        let positions = shaped.glyph_positions();
        let budget = glyphs.budget();
        // The clusters' starts, in logical order. A cluster runs to the next
        // larger start; the first starts at the run's start, so a character
        // whose glyph the shaper removed stays with a cluster.
        let mut starts = BudgetVec::new(budget);
        starts.try_reserve_exact(infos.len().saturating_add(1))?;
        for info in infos {
            starts.try_push(range.start + info.cluster as usize)?;
        }
        starts.try_push(range.end)?;
        starts.sort_unstable();
        let first_start = starts.first().copied().unwrap_or(range.start);
        glyphs.try_reserve(infos.len())?;
        let mut first = 0;
        while first < infos.len() {
            let start = range.start + infos[first].cluster as usize;
            let mut last = first;
            while last + 1 < infos.len() && range.start + infos[last + 1].cluster as usize == start
            {
                last += 1;
            }
            let end = starts[starts.partition_point(|&at| at <= start)];
            let cluster = if start == first_start {
                range.start..end
            } else {
                start..end
            };
            let own = self.own_characters(text, cluster.clone(), &infos[first..=last]);
            for at in first..=last {
                let mapped = match &own {
                    Some(own) => own[at - first].clone(),
                    // The glyph that a cluster draws first maps the whole
                    // cluster: the first in logical order, which is the last
                    // in visual order in a right-to-left run.
                    None if (if rtl { at == last } else { at == first }) => cluster.clone(),
                    None => cluster.start..cluster.start,
                };
                let position = &positions[at];
                glyphs.try_push(ShapedGlyph {
                    glyph: infos[at].glyph_id,
                    x_advance: position.x_advance,
                    y_advance: position.y_advance,
                    x_offset: position.x_offset,
                    y_offset: position.y_offset,
                    cluster: cluster.clone(),
                    text: mapped,
                    rtl,
                })?;
            }
            first = last + 1;
        }
        Ok(())
    }

    /// Maps each glyph of a cluster to its own character, when every glyph
    /// is the nominal glyph of a distinct character of the cluster and the
    /// counts match; `None` otherwise.
    fn own_characters(
        &self,
        text: &str,
        cluster: Range<usize>,
        infos: &[harfrust::GlyphInfo],
    ) -> Option<Vec<Range<usize>>> {
        let characters = text[cluster.clone()].char_indices();
        if infos.len() > 64 || characters.clone().count() != infos.len() {
            return None;
        }
        let mut used = 0_u64;
        let mut own = Vec::new();
        own.try_reserve_exact(infos.len()).ok()?;
        for info in infos {
            let (index, (offset, character)) =
                characters
                    .clone()
                    .enumerate()
                    .find(|&(index, (_, character))| {
                        used & (1 << index) == 0
                            && self.charmap.map(character).map(u32::from) == Some(info.glyph_id)
                    })?;
            used |= 1 << index;
            let start = cluster.start + offset;
            own.push(start..start + character.len_utf8());
        }
        Some(own)
    }
}

/// Splits `text[run]` into script runs in logical order. Common and
/// inherited characters join the run before them, or the first run when
/// they lead.
fn script_runs(
    text: &str,
    run: Range<usize>,
    scripts: &mut BudgetVec<'_, (Range<usize>, Option<Script>)>,
) -> Result<(), LimitExceeded> {
    let mut current: Option<(usize, UnicodeScript)> = None;
    for (offset, character) in text[run.clone()].char_indices() {
        let at = run.start + offset;
        let script = character.script();
        if matches!(
            script,
            UnicodeScript::Common | UnicodeScript::Inherited | UnicodeScript::Unknown
        ) {
            continue;
        }
        match current {
            None => current = Some((run.start, script)),
            Some((start, previous)) if previous != script => {
                scripts.try_push((start..at, harfrust_script(previous)))?;
                current = Some((at, script));
            }
            Some(_) => {}
        }
    }
    match current {
        Some((start, script)) => scripts.try_push((start..run.end, harfrust_script(script))),
        None => scripts.try_push((run, None)),
    }
}

fn harfrust_script(script: UnicodeScript) -> Option<Script> {
    let name = script.short_name().as_bytes();
    let tag: [u8; 4] = name.try_into().ok()?;
    Script::from_iso15924_tag(Tag::new(&tag))
}
