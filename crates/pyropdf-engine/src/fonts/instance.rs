//! Other instances of a variable face (DESIGN.md section 7): a static
//! TrueType program for one weight, written at load, so that the canvas
//! draws, saves, and subsets it as it does any static face.
//!
//! Every glyph keeps its ID, because content codes are glyph IDs. Composites
//! are flattened and instructions go. The layout tables stay with the source,
//! which shapes the text at the instance
//! ([`ShapingFace::of`](super::shape::ShapingFace::of)), because kerning has
//! variation deltas that a static program cannot hold.

use std::fmt;
use std::sync::Arc;

use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, LimitCode};
use skrifa::instance::{Location, Size};
use skrifa::outline::{DrawSettings, OutlinePen};
use skrifa::raw::TableProvider;
use skrifa::raw::tables::mvar::tags as mvar;
use skrifa::raw::types::Tag;
use skrifa::{FontRef, GlyphId, MetadataProvider, string::StringId};

use super::embed::{Face, FaceError, name, sanitized_name};
use super::sfnt;

#[cfg(test)]
mod tests;

/// The tag of the weight axis, which also names the axis in a PostScript name.
const WGHT: Tag = Tag::new(b"wght");

/// The weight class from which [`Face::read`] calls a face bold.
const BOLD: u16 = 600;

/// The tables that a static program copies: `cmap`, and the colour tables,
/// which say which glyphs a drawing must refuse as it does for a static face.
const COPIED: [&[u8; 4]; 7] = [
    b"CBDT", b"CBLC", b"COLR", b"CPAL", b"SVG ", b"cmap", b"sbix",
];

/// The most points and contours that a simple glyph holds: `maxp` counts the
/// points in 16 bits, and the contours are a positive `i16`.
const MAX_POINTS: usize = u16::MAX as usize;
const MAX_CONTOURS: usize = i16::MAX as usize;

/// Why a coordinate cannot be written.
const OUTSIDE_I16: &str = "a coordinate outside the 16 bits of a TrueType glyph";

/// Simple glyph flags (OpenType `glyf`).
const ON_CURVE: u8 = 0x01;
const X_SHORT: u8 = 0x02;
const Y_SHORT: u8 = 0x04;
const REPEAT: u8 = 0x08;
const X_SAME_OR_POSITIVE: u8 = 0x10;
const Y_SAME_OR_POSITIVE: u8 = 0x20;
const OVERLAP: u8 = 0x40;

/// Offsets of the `head` fields that an instance rewrites.
const HEAD_BOUNDS: usize = 36;
const HEAD_MAC_STYLE: usize = 44;
const HEAD_LOCA_FORMAT: usize = 50;
/// Offsets of the `hhea` fields that an instance rewrites.
const HHEA_ADVANCE_MAX: usize = 10;
const HHEA_MIN_LEFT: usize = 12;
const HHEA_MIN_RIGHT: usize = 14;
const HHEA_EXTENT: usize = 16;
const HHEA_LONG_METRICS: usize = 34;
/// Offsets of the `OS/2` fields that an instance rewrites.
const OS2_WEIGHT: usize = 4;
const OS2_SELECTION: usize = 62;
/// `OS/2` selection bits: italic and oblique, bold, and regular.
const SELECTION_ITALIC: u16 = 0x0201;
const SELECTION_BOLD: u16 = 0x0020;
const SELECTION_REGULAR: u16 = 0x0040;
/// The length of a version 3 `post` header, which names no glyphs.
const POST_HEADER: usize = 32;

/// Where `MVAR` varies a signed field of `hhea`, as [`Metrics`] reads it.
///
/// [`Metrics`]: skrifa::metrics::Metrics
const HHEA_VARIED: [(Tag, usize); 6] = [
    (mvar::HASC, 4),
    (mvar::HDSC, 6),
    (mvar::HLGP, 8),
    (mvar::HCRS, 18),
    (mvar::HCRN, 20),
    (mvar::HCOF, 22),
];
/// Where `MVAR` varies a signed field of `OS/2`.
const OS2_VARIED: [(Tag, usize); 15] = [
    (mvar::SBXS, 10),
    (mvar::SBYS, 12),
    (mvar::SBXO, 14),
    (mvar::SBYO, 16),
    (mvar::SPXS, 18),
    (mvar::SPYS, 20),
    (mvar::SPXO, 22),
    (mvar::SPYO, 24),
    (mvar::STRS, 26),
    (mvar::STRO, 28),
    (mvar::HASC, 68),
    (mvar::HDSC, 70),
    (mvar::HLGP, 72),
    (mvar::XHGT, 86),
    (mvar::CPHT, 88),
];
/// Where `MVAR` varies an unsigned field of `OS/2`.
const OS2_VARIED_UNSIGNED: [(Tag, usize); 2] = [(mvar::HCLA, 74), (mvar::HCLD, 76)];
/// Where `MVAR` varies a signed field of `post`.
const POST_VARIED: [(Tag, usize); 2] = [(mvar::UNDO, 8), (mvar::UNDS, 10)];

/// Why a face has no instance to draw at a weight.
#[derive(Clone, Debug, PartialEq)]
pub enum InstanceError {
    /// The face has no such instance, or a static program cannot hold it.
    Refused(String),
    /// The face's data cannot be read, or the program does not read back.
    Font(FaceError),
    /// The program would pass the limit on font input.
    Limit(LimitExceeded),
}

impl From<LimitExceeded> for InstanceError {
    fn from(refusal: LimitExceeded) -> Self {
        Self::Limit(refusal)
    }
}

impl fmt::Display for InstanceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Refused(reason) => formatter.write_str(reason),
            Self::Font(error) => fmt::Display::fmt(error, formatter),
            Self::Limit(refusal) => fmt::Display::fmt(refusal, formatter),
        }
    }
}

impl std::error::Error for InstanceError {}

/// What shapes an instance's text: the variable source, at the instance.
#[derive(Clone, Debug)]
pub struct Instance {
    /// The source face alone, as [`Face::program`] holds it.
    pub source: Arc<Vec<u8>>,
    /// The location on the source's weight axis.
    pub weight: u16,
}

impl Face {
    /// Returns the instance of this face at `weight` on its weight axis, with
    /// a static program.
    ///
    /// The program charges [`LimitCode::FontInputSize`] for every byte it
    /// holds, before the bytes of a glyph join it, since flattening composites
    /// can amplify. A coordinate that the variations push outside `i16`, and
    /// a weight outside the axis, refuse, and so do CFF outlines: only `glyf`
    /// is written.
    pub fn at_weight(&self, weight: u16, budget: &Budget) -> Result<Self, InstanceError> {
        let Some([low, _, high]) = self.weight_axis else {
            return Err(InstanceError::Refused(
                "the font has no weight axis".to_owned(),
            ));
        };
        if !(low..=high).contains(&f32::from(weight)) {
            return Err(InstanceError::Refused(format!(
                "weight {weight} is outside the font's weight axis, which runs from {low} through {high}"
            )));
        }
        if self.cff {
            return Err(InstanceError::Refused(
                "a variable font with CFF outlines has no other instance".to_owned(),
            ));
        }
        let program = self.instance_program(weight, budget)?;
        let mut face = Self::read(&program, 0).map_err(InstanceError::Font)?;
        // A source may have no OS/2 table to rewrite.
        face.weight = weight;
        face.bold = weight >= BOLD;
        face.instance = Some(Instance {
            source: Arc::clone(&self.program),
            weight,
        });
        Ok(face)
    }

    /// Writes the static program of the instance at `weight`.
    fn instance_program(&self, weight: u16, budget: &Budget) -> Result<Vec<u8>, InstanceError> {
        let font = self.font();
        let location = font.axes().location([(WGHT, f32::from(weight))]);
        let outlines = Outlines::draw(&font, &location, budget)?;
        let mut tables = vec![
            (*b"head", head(&font, &outlines, weight)?),
            (*b"hhea", hhea(&font, &location, &outlines)?),
            (*b"maxp", maxp(&outlines)),
            (*b"name", names(self, &font, weight)?),
        ];
        if let Some(os2) = os2(&font, &location, weight)? {
            tables.push((*b"OS/2", os2));
        }
        if let Some(post) = post(&font, &location)? {
            tables.push((*b"post", post));
        }
        for tag in COPIED {
            if let Some(data) = font.table_data(Tag::new(tag)) {
                tables.push((*tag, data.as_bytes().to_vec()));
            }
        }
        let others = tables
            .iter()
            .map(|(_, data)| padded(data.len()))
            .sum::<u64>();
        budget.charge(LimitCode::FontInputSize, others)?;
        // The sfnt's header and directory; the outlines' three tables charged
        // themselves as they grew.
        let directory = 12 + 16 * (tables.len() as u64 + 3);
        budget.charge(LimitCode::FontInputSize, directory)?;
        tables.push((*b"glyf", outlines.glyf));
        tables.push((*b"hmtx", outlines.hmtx));
        tables.push((*b"loca", outlines.loca));
        tables.sort_by_key(|(tag, _)| *tag);
        sfnt::write(&[0, 1, 0, 0], &tables).ok_or_else(too_large)
    }
}

fn too_large() -> InstanceError {
    InstanceError::Refused("the instance is too large for one TrueType program".to_owned())
}

/// Returns the bytes that a table of `len` bytes takes in an sfnt.
fn padded(len: usize) -> u64 {
    len.next_multiple_of(4) as u64
}

/// Returns the table of `tag`, which [`Face::read`] made sure of.
fn table(font: &FontRef<'_>, tag: &[u8; 4]) -> Result<Vec<u8>, InstanceError> {
    font.table_data(Tag::new(tag))
        .map(|data| data.as_bytes().to_vec())
        .ok_or_else(|| {
            let name = String::from_utf8_lossy(tag);
            InstanceError::Font(FaceError::Font(format!("the face has no {name} table")))
        })
}

/// Returns the two bytes at `at`, when the table is long enough to hold them:
/// an old `OS/2` has no capital height.
fn field(table: &[u8], at: usize) -> Option<[u8; 2]> {
    table.get(at..at + 2)?.try_into().ok()
}

/// Writes `value` at `at`, when the table is long enough to hold it.
fn put(table: &mut [u8], at: usize, value: [u8; 2]) {
    if let Some(field) = table.get_mut(at..at + 2) {
        field.copy_from_slice(&value);
    }
}

/// Applies the `MVAR` deltas of `fields` to `table`, refusing a value that
/// no longer fits its 16 bits.
fn vary(
    font: &FontRef<'_>,
    location: &Location,
    table: &mut [u8],
    fields: &[(Tag, usize)],
    signed: bool,
) -> Result<(), InstanceError> {
    let Ok(variations) = font.mvar() else {
        return Ok(());
    };
    for &(tag, at) in fields {
        let Some(delta) = variations.metric_delta(tag, location.coords()) else {
            continue;
        };
        let Some(bytes) = field(table, at) else {
            continue;
        };
        let old = if signed {
            i32::from(i16::from_be_bytes(bytes))
        } else {
            i32::from(u16::from_be_bytes(bytes))
        };
        let value = i64::from(old) + delta.to_f64().round() as i64;
        let new = if signed {
            i16::try_from(value).ok().map(i16::to_be_bytes)
        } else {
            u16::try_from(value).ok().map(u16::to_be_bytes)
        };
        let new = new.ok_or_else(|| {
            InstanceError::Refused(format!(
                "the {tag} metric varies outside its 16 bits at this weight"
            ))
        })?;
        put(table, at, new);
    }
    Ok(())
}

fn head(font: &FontRef<'_>, outlines: &Outlines, weight: u16) -> Result<Vec<u8>, InstanceError> {
    let mut head = table(font, b"head")?;
    for (index, value) in outlines.bounds.unwrap_or_default().into_iter().enumerate() {
        put(&mut head, HEAD_BOUNDS + 2 * index, value.to_be_bytes());
    }
    let mac_style = field(&head, HEAD_MAC_STYLE).map_or(0, u16::from_be_bytes);
    let mac_style = (mac_style & !1) | u16::from(weight >= BOLD);
    put(&mut head, HEAD_MAC_STYLE, mac_style.to_be_bytes());
    put(&mut head, HEAD_LOCA_FORMAT, 1_u16.to_be_bytes());
    Ok(head)
}

fn hhea(
    font: &FontRef<'_>,
    location: &Location,
    outlines: &Outlines,
) -> Result<Vec<u8>, InstanceError> {
    let mut hhea = table(font, b"hhea")?;
    vary(font, location, &mut hhea, &HHEA_VARIED, true)?;
    put(
        &mut hhea,
        HHEA_ADVANCE_MAX,
        outlines.advance_max.to_be_bytes(),
    );
    put(&mut hhea, HHEA_MIN_LEFT, outlines.min_left.to_be_bytes());
    put(&mut hhea, HHEA_MIN_RIGHT, outlines.min_right.to_be_bytes());
    put(&mut hhea, HHEA_EXTENT, outlines.extent.to_be_bytes());
    put(&mut hhea, HHEA_LONG_METRICS, outlines.glyphs.to_be_bytes());
    Ok(hhea)
}

/// Returns a `maxp` that counts the flattened outlines. A static program has
/// no instructions, so the fields for them hold zero and the one zone.
fn maxp(outlines: &Outlines) -> Vec<u8> {
    let mut maxp = vec![0; 32];
    maxp[0..4].copy_from_slice(&0x0001_0000_u32.to_be_bytes());
    maxp[4..6].copy_from_slice(&outlines.glyphs.to_be_bytes());
    maxp[6..8].copy_from_slice(&outlines.max_points.to_be_bytes());
    maxp[8..10].copy_from_slice(&outlines.max_contours.to_be_bytes());
    maxp[14..16].copy_from_slice(&1_u16.to_be_bytes());
    maxp
}

fn os2(
    font: &FontRef<'_>,
    location: &Location,
    weight: u16,
) -> Result<Option<Vec<u8>>, InstanceError> {
    let Some(data) = font.table_data(Tag::new(b"OS/2")) else {
        return Ok(None);
    };
    let mut os2 = data.as_bytes().to_vec();
    vary(font, location, &mut os2, &OS2_VARIED, true)?;
    vary(font, location, &mut os2, &OS2_VARIED_UNSIGNED, false)?;
    put(&mut os2, OS2_WEIGHT, weight.to_be_bytes());
    if let Some(selection) = field(&os2, OS2_SELECTION) {
        let mut selection = u16::from_be_bytes(selection) & !(SELECTION_BOLD | SELECTION_REGULAR);
        if weight >= BOLD {
            selection |= SELECTION_BOLD;
        } else if selection & SELECTION_ITALIC == 0 {
            selection |= SELECTION_REGULAR;
        }
        put(&mut os2, OS2_SELECTION, selection.to_be_bytes());
    }
    Ok(Some(os2))
}

/// Returns the source's `post` header as version 3, which names no glyphs.
fn post(font: &FontRef<'_>, location: &Location) -> Result<Option<Vec<u8>>, InstanceError> {
    let Some(data) = font.table_data(Tag::new(b"post")) else {
        return Ok(None);
    };
    let Some(header) = data.as_bytes().get(..POST_HEADER) else {
        return Ok(None);
    };
    let mut post = header.to_vec();
    post[0..4].copy_from_slice(&0x0003_0000_u32.to_be_bytes());
    vary(font, location, &mut post, &POST_VARIED, true)?;
    Ok(Some(post))
}

/// Returns the style that the font names for the instance at `weight`: that
/// of a named instance there, with every other axis at its default.
fn named_style(font: &FontRef<'_>, weight: u16) -> Option<String> {
    let axes = font.axes();
    font.named_instances()
        .iter()
        .find(|instance| {
            instance
                .user_coords()
                .zip(axes.iter())
                .all(|(value, axis)| {
                    let expected = if axis.tag() == WGHT {
                        f32::from(weight)
                    } else {
                        axis.default_value()
                    };
                    value == expected
                })
        })
        .and_then(|instance| name(font, instance.subfamily_name_id()))
}

/// Returns the instance's PostScript name, with the weight as Adobe's
/// Technical Note 5902 writes an arbitrary instance's, so that two instances
/// never share a `/BaseFont`. A long prefix gives way to the weight.
fn postscript_name(face: &Face, font: &FontRef<'_>, weight: u16) -> String {
    let suffix = format!("_{weight}{WGHT}");
    let prefix = name(font, StringId::VARIATIONS_POSTSCRIPT_NAME_PREFIX)
        .map(|prefix| sanitized_name(&prefix))
        .filter(|prefix| !prefix.is_empty())
        .unwrap_or_else(|| {
            let source = &face.postscript_name;
            let stem = source
                .rsplit_once('-')
                .map_or(source.as_str(), |(stem, _)| stem);
            stem.to_owned()
        });
    let kept: String = prefix.chars().take(63 - suffix.len()).collect();
    format!("{kept}{suffix}")
}

/// Writes a `name` table that describes the instance, which [`Face::read`]
/// then reads as it reads any face's.
fn names(face: &Face, font: &FontRef<'_>, weight: u16) -> Result<Vec<u8>, InstanceError> {
    let full_name = match named_style(font, weight) {
        Some(style) => format!("{} {style}", face.family),
        None => format!("{} {weight}{WGHT}", face.family),
    };
    let postscript_name = postscript_name(face, font, weight);
    let linked_style = if weight >= BOLD { "Bold" } else { "Regular" };
    let records = [
        (StringId::FAMILY_NAME, face.family.as_str()),
        (StringId::SUBFAMILY_NAME, linked_style),
        (StringId::FULL_NAME, full_name.as_str()),
        (StringId::POSTSCRIPT_NAME, postscript_name.as_str()),
    ];
    name_table(&records).ok_or_else(|| {
        InstanceError::Refused("the font's names are too long for its name table".to_owned())
    })
}

/// Writes a format 0 `name` table with a Windows English record for each
/// entry that is not empty, `None` when the strings pass its 16-bit offsets.
fn name_table(records: &[(StringId, &str)]) -> Option<Vec<u8>> {
    const WINDOWS: u16 = 3;
    const UNICODE_BMP: u16 = 1;
    const ENGLISH_US: u16 = 0x0409;
    let records: Vec<_> = records
        .iter()
        .filter(|(_, text)| !text.is_empty())
        .collect();
    let count = u16::try_from(records.len()).ok()?;
    let storage = u16::try_from(6 + 12 * records.len()).ok()?;
    let mut table = Vec::new();
    let mut strings = Vec::new();
    for value in [0, count, storage] {
        table.extend_from_slice(&u16::to_be_bytes(value));
    }
    for (id, text) in records {
        let offset = u16::try_from(strings.len()).ok()?;
        strings.extend(text.encode_utf16().flat_map(u16::to_be_bytes));
        let length = u16::try_from(strings.len() - usize::from(offset)).ok()?;
        for value in [
            WINDOWS,
            UNICODE_BMP,
            ENGLISH_US,
            id.to_u16(),
            length,
            offset,
        ] {
            table.extend_from_slice(&u16::to_be_bytes(value));
        }
    }
    u16::try_from(strings.len()).ok()?;
    table.append(&mut strings);
    Some(table)
}

/// A point of an outline, in font units.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Point {
    x: i16,
    y: i16,
    on: bool,
}

/// Collects a glyph's contours as whole font units.
#[derive(Debug, Default)]
struct Pen {
    points: Vec<Point>,
    /// One past the last point of each contour.
    ends: Vec<usize>,
    /// Whether a coordinate fell outside `i16`.
    outside: bool,
    /// Whether a curve was cubic, which `glyf` does not hold.
    cubic: bool,
}

impl Pen {
    fn clear(&mut self) {
        self.points.clear();
        self.ends.clear();
        self.outside = false;
        self.cubic = false;
    }

    fn point(&mut self, x: f32, y: f32, on: bool) {
        match (coordinate(x), coordinate(y)) {
            (Some(x), Some(y)) => self.points.push(Point { x, y, on }),
            _ => self.outside = true,
        }
    }
}

/// Returns `value` rounded to a whole font unit, `None` outside `i16`.
fn coordinate(value: f32) -> Option<i16> {
    let value = value.round();
    (f32::from(i16::MIN)..=f32::from(i16::MAX))
        .contains(&value)
        .then_some(value as i16)
}

impl OutlinePen for Pen {
    fn move_to(&mut self, x: f32, y: f32) {
        self.point(x, y, true);
    }
    fn line_to(&mut self, x: f32, y: f32) {
        self.point(x, y, true);
    }
    fn quad_to(&mut self, cx0: f32, cy0: f32, x: f32, y: f32) {
        self.point(cx0, cy0, false);
        self.point(x, y, true);
    }
    fn curve_to(&mut self, _: f32, _: f32, _: f32, _: f32, _: f32, _: f32) {
        self.cubic = true;
    }
    fn close(&mut self) {
        // A contour that returns to its start closes without that point.
        let start = self.ends.last().copied().unwrap_or(0);
        if self.points.len() > start + 1
            && let (Some(first), Some(last)) = (self.points.get(start), self.points.last())
            && (first.x, first.y) == (last.x, last.y)
        {
            self.points.pop();
        }
        self.ends.push(self.points.len());
    }
}

/// Appends the points of `contour` that the file has to hold: an on-curve
/// point halfway between two off-curve points is implied.
fn keep_explicit(contour: &[Point], kept: &mut Vec<Point>) {
    let count = contour.len();
    for (index, &point) in contour.iter().enumerate() {
        let before = contour[(index + count - 1) % count];
        let after = contour[(index + 1) % count];
        let halfway = |low: i16, here: i16, high: i16| {
            (i32::from(low) + i32::from(high) - 2 * i32::from(here)).abs() <= 1
        };
        let implied = point.on
            && !before.on
            && !after.on
            && halfway(before.x, point.x, after.x)
            && halfway(before.y, point.y, after.y);
        if !implied {
            kept.push(point);
        }
    }
}

/// Writes one column of a simple glyph's coordinates as deltas from zero and
/// sets their flag bits.
fn encode_axis(
    values: impl Iterator<Item = i16>,
    short: u8,
    same_or_positive: u8,
    flags: &mut [u8],
    column: &mut Vec<u8>,
) -> Result<(), &'static str> {
    let mut last = 0_i32;
    for (value, flag) in values.zip(flags.iter_mut()) {
        let delta = i32::from(value) - last;
        last = i32::from(value);
        if delta == 0 {
            *flag |= same_or_positive;
        } else if let Ok(size) = u8::try_from(delta.unsigned_abs()) {
            *flag |= short;
            if delta > 0 {
                *flag |= same_or_positive;
            }
            column.push(size);
        } else {
            let delta = i16::try_from(delta).map_err(|_| OUTSIDE_I16)?;
            column.extend_from_slice(&delta.to_be_bytes());
        }
    }
    Ok(())
}

/// What [`encode`] wrote of one glyph.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Encoded {
    bounds: [i16; 4],
    points: u16,
    contours: u16,
}

/// Writes the simple glyph description of `pen`'s contours to `out`, padded
/// to four bytes; an empty outline writes nothing and returns `None`.
fn encode(pen: &Pen, overlaps: bool, out: &mut Vec<u8>) -> Result<Option<Encoded>, &'static str> {
    let mut points = Vec::with_capacity(pen.points.len());
    let mut ends = Vec::with_capacity(pen.ends.len());
    let mut start = 0;
    for &end in &pen.ends {
        keep_explicit(&pen.points[start..end], &mut points);
        if end > start {
            ends.push(points.len());
        }
        start = end;
    }
    if points.is_empty() {
        return Ok(None);
    }
    if points.len() > MAX_POINTS || ends.len() > MAX_CONTOURS {
        return Err("more points than a TrueType glyph holds");
    }
    let mut bounds = [i16::MAX, i16::MAX, i16::MIN, i16::MIN];
    for point in &points {
        bounds = [
            bounds[0].min(point.x),
            bounds[1].min(point.y),
            bounds[2].max(point.x),
            bounds[3].max(point.y),
        ];
    }
    let mut flags: Vec<u8> = points
        .iter()
        .map(|point| if point.on { ON_CURVE } else { 0 })
        .collect();
    if overlaps {
        flags[0] |= OVERLAP;
    }
    let (mut xs, mut ys) = (Vec::new(), Vec::new());
    encode_axis(
        points.iter().map(|point| point.x),
        X_SHORT,
        X_SAME_OR_POSITIVE,
        &mut flags,
        &mut xs,
    )?;
    encode_axis(
        points.iter().map(|point| point.y),
        Y_SHORT,
        Y_SAME_OR_POSITIVE,
        &mut flags,
        &mut ys,
    )?;
    // Both limits were checked above.
    let contours = ends.len() as u16;
    out.extend_from_slice(&contours.to_be_bytes());
    for value in bounds {
        out.extend_from_slice(&value.to_be_bytes());
    }
    for &end in &ends {
        out.extend_from_slice(&((end - 1) as u16).to_be_bytes());
    }
    // No instructions.
    out.extend_from_slice(&0_u16.to_be_bytes());
    let mut index = 0;
    while index < flags.len() {
        let flag = flags[index];
        let run = flags[index..]
            .iter()
            .take(256)
            .take_while(|&&other| other == flag)
            .count();
        if run > 1 {
            out.push(flag | REPEAT);
            out.push((run - 1) as u8);
        } else {
            out.push(flag);
        }
        index += run;
    }
    out.append(&mut xs);
    out.append(&mut ys);
    out.resize(out.len().next_multiple_of(4), 0);
    Ok(Some(Encoded {
        bounds,
        points: points.len() as u16,
        contours,
    }))
}

/// The three tables of the instance's glyphs, and what their other tables
/// record of them.
#[derive(Debug)]
struct Outlines {
    glyf: Vec<u8>,
    loca: Vec<u8>,
    hmtx: Vec<u8>,
    glyphs: u16,
    bounds: Option<[i16; 4]>,
    advance_max: u16,
    min_left: i16,
    min_right: i16,
    extent: i16,
    max_points: u16,
    max_contours: u16,
}

impl Outlines {
    /// Draws every glyph at `location`, charging each before it joins `glyf`.
    fn draw(
        font: &FontRef<'_>,
        location: &Location,
        budget: &Budget,
    ) -> Result<Self, InstanceError> {
        let glyphs = font
            .maxp()
            .map_err(|error| InstanceError::Font(FaceError::Font(format!("maxp: {error}"))))?
            .num_glyphs();
        let count = usize::from(glyphs);
        // `hmtx` and the long `loca`, which glyph IDs of 16 bits bound.
        budget.charge(LimitCode::FontInputSize, 8 * count as u64 + 4)?;
        let metrics = font.glyph_metrics(Size::unscaled(), location);
        let outlines = font.outline_glyphs();
        let settings = || DrawSettings::unhinted(Size::unscaled(), location);
        let mut result = Self {
            glyf: Vec::new(),
            loca: Vec::with_capacity(4 * (count + 1)),
            hmtx: Vec::with_capacity(4 * count),
            glyphs,
            bounds: None,
            advance_max: 0,
            min_left: i16::MAX,
            min_right: i16::MAX,
            extent: i16::MIN,
            max_points: 0,
            max_contours: 0,
        };
        let mut pen = Pen::default();
        let mut glyph = Vec::new();
        for id in 0..u32::from(glyphs) {
            let glyph_id = GlyphId::new(id);
            pen.clear();
            glyph.clear();
            let mut overlaps = false;
            if let Some(outline) = outlines.get(glyph_id) {
                outline.draw(settings(), &mut pen).map_err(|error| {
                    InstanceError::Font(FaceError::Font(format!("glyph {id}: {error}")))
                })?;
                overlaps = outline.has_overlaps().unwrap_or(false);
            }
            let refused = |reason: &str| {
                InstanceError::Refused(format!(
                    "glyph {id} cannot be written at this weight: {reason}"
                ))
            };
            if pen.outside {
                return Err(refused(OUTSIDE_I16));
            }
            if pen.cubic {
                return Err(refused("cubic curves, which a TrueType glyph cannot hold"));
            }
            let encoded = encode(&pen, overlaps, &mut glyph).map_err(refused)?;
            budget.charge(LimitCode::FontInputSize, glyph.len() as u64)?;
            let offset = u32::try_from(result.glyf.len()).map_err(|_| too_large())?;
            result.loca.extend_from_slice(&offset.to_be_bytes());
            result
                .glyf
                .try_reserve(glyph.len())
                .map_err(|_| too_large())?;
            result.glyf.extend_from_slice(&glyph);
            // A variation may take an advance below zero, as fontTools'
            // instancer finds too, and it ends there; no advance can pass
            // 16 bits in a program that holds it.
            let advance = metrics.advance_width(glyph_id).unwrap_or(0.0).round() as i32;
            let advance = u16::try_from(advance.max(0))
                .map_err(|_| refused("an advance outside the 16 bits of a TrueType font"))?;
            result.note(advance, encoded);
            let left = encoded.map_or(0, |encoded| encoded.bounds[0]);
            result.hmtx.extend_from_slice(&advance.to_be_bytes());
            result.hmtx.extend_from_slice(&left.to_be_bytes());
        }
        let offset = u32::try_from(result.glyf.len()).map_err(|_| too_large())?;
        result.loca.extend_from_slice(&offset.to_be_bytes());
        Ok(result)
    }

    /// Records what `hhea`, `head`, and `maxp` say of all the glyphs.
    fn note(&mut self, advance: u16, encoded: Option<Encoded>) {
        self.advance_max = self.advance_max.max(advance);
        let Some(encoded) = encoded else {
            return;
        };
        let [x_min, y_min, x_max, y_max] = encoded.bounds;
        let right = i32::from(advance) - i32::from(x_max);
        self.min_left = self.min_left.min(x_min);
        self.min_right = self
            .min_right
            .min(right.clamp(i16::MIN.into(), i16::MAX.into()) as i16);
        self.extent = self.extent.max(x_max);
        self.max_points = self.max_points.max(encoded.points);
        self.max_contours = self.max_contours.max(encoded.contours);
        self.bounds = Some(self.bounds.map_or(encoded.bounds, |bounds| {
            [
                bounds[0].min(x_min),
                bounds[1].min(y_min),
                bounds[2].max(x_max),
                bounds[3].max(y_max),
            ]
        }));
    }
}
