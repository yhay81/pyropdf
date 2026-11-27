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
use std::sync::Arc;

use harfrust::{
    Buffer, Direction, Font, GlyphId as HarfGlyphId, Script, ShapeOptions, ShaperFont, Tag,
};
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use skrifa::raw::TableProvider;
use unicode_bidi::BidiInfo;
use unicode_script::{Script as UnicodeScript, UnicodeScript as _};
use unicode_segmentation::UnicodeSegmentation;

use super::embed::{Face, Measure};
use crate::standard::{self, Base14Font};

#[cfg(test)]
mod tests;

/// Why a face or its text could not be shaped.
#[derive(Clone, Debug, PartialEq)]
pub enum ShapeError {
    /// The program is not an OpenType font that this reads, or its tables
    /// ask for more than the shaper allows.
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

/// Text progression direction for shaping (horizontal or top-to-bottom vertical).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum WritingDirection {
    /// Standard horizontal writing (bidi LTR/RTL runs).
    #[default]
    Horizontal,
    /// Vertical top-to-bottom writing (Identity-V / `vert` GSUB).
    Vertical,
}

/// One face in an ordered fallback chain: a Standard 14 font (horizontal only)
/// or an embedded OpenType face.
#[derive(Clone, Copy)]
pub enum FallbackSource<'a> {
    /// A Standard 14 font encoded in WinAnsi (horizontal writing only).
    Standard(Base14Font),
    /// An embedded OpenType face shaped by harfrust.
    Embedded(&'a Face),
}

/// One shaped glyph from a fallback chain, carrying the index of the face that
/// shaped it alongside its placement in that face's font units.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FallbackShapedGlyph {
    /// Index into the caller's face slice.
    pub face_index: usize,
    /// Glyph ID for an embedded face, or WinAnsi byte for a Standard 14 font.
    pub glyph: u32,
    /// How far the pen moves right after the glyph, in the face's font units.
    pub x_advance: i32,
    /// How far the pen moves up after the glyph, in the face's font units.
    pub y_advance: i32,
    /// How far the glyph is drawn right of the pen, in the face's font units.
    pub x_offset: i32,
    /// How far the glyph is drawn above the pen, in the face's font units.
    pub y_offset: i32,
    /// The byte range of the input that the glyph's cluster covers.
    pub cluster: Range<usize>,
    /// The byte range of the input that the glyph's ToUnicode entry maps to;
    /// empty for a glyph after the first of a cluster that maps whole.
    pub text: Range<usize>,
    /// Whether the glyph belongs to a right-to-left run.
    pub rtl: bool,
}

/// Why shaping across a fallback font chain failed.
#[derive(Clone, Debug, PartialEq)]
pub enum FallbackShapeError {
    /// Underlying shaper or budget refusal.
    Shape(ShapeError),
    /// Vertical writing was requested with a Standard 14 font in the chain.
    VerticalStandard,
    /// Every face in the chain was Standard 14 and a character was outside WinAnsi.
    NotWinAnsi(char),
    /// No face in the chain had a glyph for `character`.
    MissingGlyph(char),
}

impl From<ShapeError> for FallbackShapeError {
    fn from(error: ShapeError) -> Self {
        Self::Shape(error)
    }
}

impl From<LimitExceeded> for FallbackShapeError {
    fn from(refusal: LimitExceeded) -> Self {
        Self::Shape(ShapeError::Limit(refusal))
    }
}

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

/// An OpenType face ready to shape, at its default instance or, read with
/// [`ShapingFace::of`], at the instance of a [`Face`].
pub struct ShapingFace {
    font: Font,
}

impl ShapingFace {
    /// Reads the face at `index` of `program`, a TrueType, OpenType, or
    /// collection file, which harfrust's font shares rather than copies.
    pub fn new(program: Arc<Vec<u8>>, index: u32) -> Result<Self, ShapeError> {
        let source: Arc<dyn AsRef<[u8]> + Send + Sync> = program;
        let font = Font::new(source, index)
            .ok_or_else(|| ShapeError::Font(format!("the program has no face {index}")))?;
        if font.tables().head().is_err() {
            return Err(ShapeError::Font("the face has no head table".into()));
        }
        Ok(Self { font })
    }

    /// Reads `face` to shape: its own program, or for an instance the source
    /// at the instance's weight, since kerning has variation deltas that a
    /// static program cannot hold (DESIGN.md section 7).
    pub fn of(face: &Face) -> Result<Self, ShapeError> {
        let Some(instance) = &face.instance else {
            return Self::new(Arc::clone(&face.program), 0);
        };
        let Self { font } = Self::new(Arc::clone(&instance.source), 0)?;
        let font = font
            .instance_builder()
            .variations([(Tag::new(b"wght"), f32::from(instance.weight))])
            .build();
        Ok(Self { font })
    }

    /// Returns the face's units per em.
    #[must_use]
    pub fn units_per_em(&self) -> u16 {
        self.font.units_per_em()
    }

    /// Returns the glyphs of `text` in visual order (horizontal writing).
    ///
    /// The text's characters and the glyphs count against
    /// [`LimitCode::ShapedGlyphCount`], and shaping polls the budget between
    /// runs. A run that needs more work or room than harfrust allows it is a
    /// font error, since only the face's own tables can ask for that.
    pub fn shape<'b>(
        &self,
        text: &str,
        budget: &'b Budget,
    ) -> Result<BudgetVec<'b, ShapedGlyph>, ShapeError> {
        self.shape_direction(text, WritingDirection::Horizontal, budget)
    }

    /// Returns the glyphs of `text` shaped in `direction` (`Horizontal` visual
    /// order or `Vertical` top-to-bottom order).
    pub fn shape_direction<'b>(
        &self,
        text: &str,
        direction: WritingDirection,
        budget: &'b Budget,
    ) -> Result<BudgetVec<'b, ShapedGlyph>, ShapeError> {
        let chars = u64::try_from(text.chars().count()).unwrap_or(u64::MAX);
        budget.check(LimitCode::ShapedGlyphCount, chars)?;
        budget.poll()?;
        let mut glyphs = BudgetVec::counting(budget, LimitCode::ShapedGlyphCount);
        if text.is_empty() {
            return Ok(glyphs);
        }
        let shaper = ShaperFont::new(&self.font);
        let mut buffer = Buffer::new();
        match direction {
            WritingDirection::Horizontal => {
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
                            shape_segment(
                                &shaper,
                                &mut buffer,
                                text,
                                range.clone(),
                                if rtl {
                                    Direction::RightToLeft
                                } else {
                                    Direction::LeftToRight
                                },
                                script,
                                rtl,
                                &mut glyphs,
                            )?;
                        }
                    }
                }
            }
            WritingDirection::Vertical => {
                let mut scripts = BudgetVec::new(budget);
                script_runs(text, 0..text.len(), &mut scripts)?;
                for &(ref range, script) in scripts.iter() {
                    budget.poll()?;
                    shape_segment(
                        &shaper,
                        &mut buffer,
                        text,
                        range.clone(),
                        Direction::TopToBottom,
                        script,
                        false,
                        &mut glyphs,
                    )?;
                }
            }
        }
        Ok(glyphs)
    }

    /// Returns `(v_advance, v_origin_x, v_origin_y)` in font units for each
    /// glyph ID in `glyphs`, matching what harfrust uses for vertical shaping
    /// (`v_advance` is negative for downward progression).
    pub fn vertical_metrics<'b>(
        &self,
        glyphs: &[u32],
        budget: &'b Budget,
    ) -> Result<BudgetVec<'b, (i32, i32, i32)>, ShapeError> {
        let shaper = ShaperFont::new(&self.font);
        let mut out = BudgetVec::new(budget);
        out.try_reserve_exact(glyphs.len())?;
        for &glyph in glyphs {
            budget.poll()?;
            let id = HarfGlyphId::new(glyph);
            let v_advance = shaper.glyph_v_advance(id);
            let (v_x, v_y) = shaper.glyph_v_origin(id);
            out.try_push((v_advance, v_x, v_y))?;
        }
        Ok(out)
    }
}

#[expect(
    clippy::too_many_arguments,
    reason = "shaping state and segment metadata"
)]
fn shape_segment(
    shaper: &ShaperFont<'_, '_>,
    buffer: &mut Buffer,
    text: &str,
    range: Range<usize>,
    direction: Direction,
    script: Option<Script>,
    rtl: bool,
    glyphs: &mut BudgetVec<'_, ShapedGlyph>,
) -> Result<(), ShapeError> {
    buffer.push_str(&text[range.clone()]);
    buffer.set_direction(direction);
    buffer.set_script(script);
    buffer.guess_segment_properties();
    harfrust::shape(shaper, buffer, ShapeOptions::new())
        .map_err(|error| ShapeError::Font(error.to_string()))?;
    if !buffer.allocation_successful() {
        return Err(ShapeError::Font(
            "the face asks for more work than the shaper allows".into(),
        ));
    }
    push_run(shaper, text, range, rtl, buffer, glyphs)?;
    buffer.clear();
    Ok(())
}

/// Shapes `text` across an ordered fallback sequence of `faces`, resolving each
/// extended grapheme cluster deterministically to the first face that supports
/// it, coalescing adjacent clusters on the same face so kerning and ligatures
/// stay intact.
pub fn shape_with_fallback<'b>(
    faces: &[FallbackSource<'_>],
    text: &str,
    direction: WritingDirection,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, FallbackShapedGlyph>, FallbackShapeError> {
    if faces.is_empty() {
        return Err(ShapeError::Font("at least one font face is required".into()).into());
    }
    if direction == WritingDirection::Vertical
        && faces
            .iter()
            .any(|face| matches!(face, FallbackSource::Standard(_)))
    {
        return Err(FallbackShapeError::VerticalStandard);
    }
    let chars = u64::try_from(text.chars().count()).unwrap_or(u64::MAX);
    budget.check(LimitCode::ShapedGlyphCount, chars)?;
    budget.poll()?;
    let mut glyphs = BudgetVec::counting(budget, LimitCode::ShapedGlyphCount);
    if text.is_empty() {
        return Ok(glyphs);
    }
    let all_standard = faces
        .iter()
        .all(|face| matches!(face, FallbackSource::Standard(_)));
    let mut shapers: BudgetVec<'_, Option<ShapingFace>> = BudgetVec::new(budget);
    shapers.try_reserve_exact(faces.len())?;
    for _ in 0..faces.len() {
        shapers.try_push(None)?;
    }
    let mut buffer = Buffer::new();
    let mut shape_script_run = |range: Range<usize>,
                                script: Option<Script>,
                                rtl: bool,
                                glyphs: &mut BudgetVec<'b, FallbackShapedGlyph>|
     -> Result<(), FallbackShapeError> {
        let mut face_runs: BudgetVec<'_, (usize, Range<usize>)> = BudgetVec::new(budget);
        let mut last_face: Option<usize> = None;
        let slice = &text[range.clone()];
        for (index, (offset, cluster)) in slice.grapheme_indices(true).enumerate() {
            if index % 256 == 0 {
                budget.poll()?;
            }
            let c_start = range.start + offset;
            let c_end = c_start + cluster.len();
            let chosen = pick_face_for_cluster(faces, cluster, last_face, all_standard)?;
            last_face = Some(chosen);
            if let Some(previous) = face_runs.last_mut()
                && previous.0 == chosen
                && previous.1.end == c_start
            {
                previous.1.end = c_end;
            } else {
                face_runs.try_push((chosen, c_start..c_end))?;
            }
        }
        if rtl {
            face_runs.reverse();
        }
        for &(face_index, ref sub_range) in face_runs.iter() {
            budget.poll()?;
            match faces[face_index] {
                FallbackSource::Standard(font) => {
                    push_standard_run(
                        font,
                        face_index,
                        text,
                        sub_range.clone(),
                        rtl,
                        all_standard,
                        glyphs,
                    )?;
                }
                FallbackSource::Embedded(face) => {
                    if shapers[face_index].is_none() {
                        shapers[face_index] = Some(ShapingFace::of(face)?);
                    }
                    let shaping_face = shapers[face_index]
                        .as_ref()
                        .expect("shaping face initialized above");
                    let shaper = ShaperFont::new(&shaping_face.font);
                    let harf_dir = match direction {
                        WritingDirection::Vertical => Direction::TopToBottom,
                        WritingDirection::Horizontal if rtl => Direction::RightToLeft,
                        WritingDirection::Horizontal => Direction::LeftToRight,
                    };
                    buffer.push_str(&text[sub_range.clone()]);
                    buffer.set_direction(harf_dir);
                    buffer.set_script(script);
                    buffer.guess_segment_properties();
                    harfrust::shape(&shaper, &mut buffer, ShapeOptions::new())
                        .map_err(|error| ShapeError::Font(error.to_string()))?;
                    if !buffer.allocation_successful() {
                        return Err(ShapeError::Font(
                            "the face asks for more work than the shaper allows".into(),
                        )
                        .into());
                    }
                    let start_len = glyphs.len();
                    push_fallback_run(
                        &shaper,
                        face_index,
                        text,
                        sub_range.clone(),
                        rtl,
                        &buffer,
                        glyphs,
                    )?;
                    buffer.clear();
                    if let Some(character) = glyphs[start_len..]
                        .iter()
                        .filter(|glyph| glyph.glyph == 0)
                        .filter_map(|glyph| text[glyph.cluster.clone()].chars().next())
                        .find(|character| !character.is_control())
                    {
                        return Err(FallbackShapeError::MissingGlyph(character));
                    }
                }
            }
        }
        Ok(())
    };

    match direction {
        WritingDirection::Horizontal => {
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
                        shape_script_run(range.clone(), script, rtl, &mut glyphs)?;
                    }
                }
            }
        }
        WritingDirection::Vertical => {
            let mut scripts = BudgetVec::new(budget);
            script_runs(text, 0..text.len(), &mut scripts)?;
            for &(ref range, script) in scripts.iter() {
                budget.poll()?;
                shape_script_run(range.clone(), script, false, &mut glyphs)?;
            }
        }
    }
    Ok(glyphs)
}

fn is_cluster_ignorable(character: char) -> bool {
    character.is_control()
        || matches!(character, '\u{200C}' | '\u{200D}' | '\u{FE00}'..='\u{FE0F}' | '\u{E0100}'..='\u{E01EF}')
}

fn face_supports_cluster(face: FallbackSource<'_>, cluster: &str) -> bool {
    match face {
        FallbackSource::Standard(font) => cluster.chars().all(|character| {
            standard::glyph_code(font, character).is_some()
                && standard::winansi_byte(character).is_some()
        }),
        FallbackSource::Embedded(face) => {
            let measure = Measure::new(face);
            cluster
                .chars()
                .filter(|&character| !is_cluster_ignorable(character))
                .all(|character| measure.glyph(character).is_some())
        }
    }
}

fn pick_face_for_cluster(
    faces: &[FallbackSource<'_>],
    cluster: &str,
    last_face: Option<usize>,
    all_standard: bool,
) -> Result<usize, FallbackShapeError> {
    if cluster.chars().all(is_cluster_ignorable) {
        return Ok(last_face.unwrap_or(0));
    }
    if let Some(index) = faces
        .iter()
        .position(|&face| face_supports_cluster(face, cluster))
    {
        return Ok(index);
    }
    let missing = cluster
        .chars()
        .find(|&character| !is_cluster_ignorable(character))
        .or_else(|| cluster.chars().next())
        .unwrap_or('\u{FFFD}');
    if all_standard {
        Err(FallbackShapeError::NotWinAnsi(missing))
    } else {
        Err(FallbackShapeError::MissingGlyph(missing))
    }
}

fn push_standard_run(
    font: Base14Font,
    face_index: usize,
    text: &str,
    range: Range<usize>,
    rtl: bool,
    all_standard: bool,
    glyphs: &mut BudgetVec<'_, FallbackShapedGlyph>,
) -> Result<(), FallbackShapeError> {
    let budget = glyphs.budget();
    let mut entries = BudgetVec::new(budget);
    for (offset, character) in text[range.clone()].char_indices() {
        let start = range.start + offset;
        let end = start + character.len_utf8();
        entries.try_push((character, start..end))?;
    }
    if rtl {
        entries.reverse();
    }
    glyphs.try_reserve(entries.len())?;
    for &(character, ref char_range) in entries.iter() {
        let Some(metric) = standard::char_metric(font, character) else {
            return Err(if all_standard {
                FallbackShapeError::NotWinAnsi(character)
            } else {
                FallbackShapeError::MissingGlyph(character)
            });
        };
        let Some(byte) = standard::winansi_byte(character) else {
            return Err(if all_standard {
                FallbackShapeError::NotWinAnsi(character)
            } else {
                FallbackShapeError::MissingGlyph(character)
            });
        };
        #[expect(
            clippy::cast_possible_truncation,
            reason = "Standard 14 AFM character widths are integer thousandths of an em"
        )]
        let x_advance = metric.width_x.round() as i32;
        glyphs.try_push(FallbackShapedGlyph {
            face_index,
            glyph: u32::from(byte),
            x_advance,
            y_advance: 0,
            x_offset: 0,
            y_offset: 0,
            cluster: char_range.clone(),
            text: char_range.clone(),
            rtl,
        })?;
    }
    Ok(())
}

/// Appends one shaped fallback run directly into `glyphs` so each glyph counts
/// once against `ShapedGlyphCount`.
fn push_fallback_run(
    shaper: &ShaperFont<'_, '_>,
    face_index: usize,
    text: &str,
    range: Range<usize>,
    rtl: bool,
    shaped: &Buffer,
    glyphs: &mut BudgetVec<'_, FallbackShapedGlyph>,
) -> Result<(), ShapeError> {
    let infos = shaped.glyph_infos();
    let positions = shaped.glyph_positions();
    let budget = glyphs.budget();
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
        while last + 1 < infos.len() && range.start + infos[last + 1].cluster as usize == start {
            last += 1;
        }
        let end = starts[starts.partition_point(|&at| at <= start)];
        let cluster = if start == first_start {
            range.start..end
        } else {
            start..end
        };
        let own = own_characters(shaper, text, cluster.clone(), &infos[first..=last]);
        for at in first..=last {
            let mapped = match &own {
                Some(own) => own[at - first].clone(),
                None if (if rtl { at == last } else { at == first }) => cluster.clone(),
                None => cluster.start..cluster.start,
            };
            let position = &positions[at];
            glyphs.try_push(FallbackShapedGlyph {
                face_index,
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

/// Appends one shaped run, whose clusters are byte offsets into
/// `text[range]`.
fn push_run(
    shaper: &ShaperFont<'_, '_>,
    text: &str,
    range: Range<usize>,
    rtl: bool,
    shaped: &Buffer,
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
        while last + 1 < infos.len() && range.start + infos[last + 1].cluster as usize == start {
            last += 1;
        }
        let end = starts[starts.partition_point(|&at| at <= start)];
        let cluster = if start == first_start {
            range.start..end
        } else {
            start..end
        };
        let own = own_characters(shaper, text, cluster.clone(), &infos[first..=last]);
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
#[expect(
    clippy::disallowed_methods,
    reason = "one range per glyph of a cluster, 64 at most, checked below"
)]
fn own_characters(
    shaper: &ShaperFont<'_, '_>,
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
                        && shaper.nominal_glyph(u32::from(character)).map(u32::from)
                            == Some(info.glyph_id)
                })?;
        used |= 1 << index;
        let start = cluster.start + offset;
        own.push(start..start + character.len_utf8());
    }
    Some(own)
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
