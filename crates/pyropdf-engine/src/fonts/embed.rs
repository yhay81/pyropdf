//! Embedding an OpenType face (DESIGN.md section 7): reading the face and
//! its metrics, and the program that a Type0 font embeds until a save.
//!
//! The program is the face alone, as an sfnt, even when it came from a
//! collection. Content codes are the face's glyph IDs, so a CID-keyed CFF
//! face whose charset maps glyphs to other CIDs gets an identity charset:
//! each glyph's CID becomes its original glyph ID.

use std::fmt;

pub use skrifa::GlyphId;
use skrifa::instance::{LocationRef, Size};
use skrifa::metrics::{BoundingBox, GlyphMetrics, Metrics};
use skrifa::outline::{DrawSettings, OutlinePen};
use skrifa::raw::types::Tag;
use skrifa::raw::{FileRef, TableProvider};
use skrifa::{FontRef, MetadataProvider, string::StringId};

use super::{cff, sfnt};

#[cfg(test)]
mod tests;

/// Why a face could not be embedded.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FaceError {
    /// The data is not an OpenType face that this reads.
    Font(String),
    /// The face is valid OpenType that PDF cannot embed as it is.
    Unsupported(&'static str),
}

impl fmt::Display for FaceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Font(reason) => write!(formatter, "the font cannot be read: {reason}"),
            Self::Unsupported(reason) => write!(formatter, "the font cannot be embedded: {reason}"),
        }
    }
}

impl std::error::Error for FaceError {}

/// An OpenType face read for embedding.
#[derive(Clone, Debug)]
pub struct Face {
    /// The face alone as an sfnt, with an identity charset when its CFF is
    /// CID-keyed: what `FontFile2` or `FontFile3` embeds.
    pub program: Vec<u8>,
    /// Whether the outlines are CFF, embedded as `CIDFontType0`; TrueType
    /// outlines are embedded as `CIDFontType2`.
    pub cff: bool,
    /// The PostScript name, or the family name with its spaces removed.
    pub postscript_name: String,
    /// The family name.
    pub family: String,
    /// The full name, such as "Noto Sans JP Regular".
    pub full_name: String,
    /// The OS/2 weight class.
    pub weight: u16,
    /// Whether the face is bold by its weight class or style flags.
    pub bold: bool,
    /// Whether the face is italic or oblique.
    pub italic: bool,
    /// Whether the face has serifs, by its PANOSE class or family name.
    pub serif: bool,
    /// Whether every glyph has one advance.
    pub monospaced: bool,
    /// The units per em.
    pub units_per_em: u16,
    /// The typographic ascender, in font units.
    pub ascender: f64,
    /// The typographic descender, in font units, negative.
    pub descender: f64,
    /// The font's box, in font units: `[x_min, y_min, x_max, y_max]`.
    pub bbox: [f64; 4],
    /// The number of glyphs.
    pub glyph_count: u32,
    /// The capital height, in font units, for the font descriptor.
    pub cap_height: f64,
    /// The italic angle in degrees, for the font descriptor.
    pub italic_angle: f64,
    /// The minimum, default, and maximum of a weight axis; the program is
    /// drawn at its default instance.
    pub weight_axis: Option<[f32; 3]>,
}

/// The tables that a face needs to measure and draw text.
const REQUIRED: [&[u8; 4]; 5] = [b"head", b"hhea", b"hmtx", b"maxp", b"cmap"];

impl Face {
    /// Reads the face at `index` of `data`, a font file or collection.
    pub fn read(data: &[u8], index: u32) -> Result<Self, FaceError> {
        let font = match FileRef::new(data).map_err(|error| FaceError::Font(error.to_string()))? {
            FileRef::Font(font) if index == 0 => font,
            FileRef::Font(_) => {
                return Err(FaceError::Font(format!(
                    "a single-face file has no face {index}"
                )));
            }
            FileRef::Collection(collection) => collection
                .get(index)
                .map_err(|error| FaceError::Font(format!("face {index}: {error}")))?,
        };
        for tag in REQUIRED {
            if font.table_data(Tag::new(tag)).is_none() {
                let name = String::from_utf8_lossy(tag);
                return Err(FaceError::Font(format!("the face has no {name} table")));
            }
        }
        if font.table_data(Tag::new(b"CFF2")).is_some() {
            return Err(FaceError::Unsupported(
                "CFF2 outlines have no PDF font type",
            ));
        }
        let cff = font.table_data(Tag::new(b"CFF ")).is_some();
        if !cff
            && (font.table_data(Tag::new(b"glyf")).is_none()
                || font.table_data(Tag::new(b"loca")).is_none())
        {
            return Err(FaceError::Unsupported("the face has no outlines"));
        }
        let head = font
            .head()
            .map_err(|error| FaceError::Font(format!("head: {error}")))?;
        let units_per_em = head.units_per_em();
        if !(16..=16384).contains(&units_per_em) {
            return Err(FaceError::Font(format!(
                "units per em {units_per_em} is outside 16 to 16384"
            )));
        }
        font.hhea()
            .map_err(|error| FaceError::Font(format!("hhea: {error}")))?;
        font.hmtx()
            .map_err(|error| FaceError::Font(format!("hmtx: {error}")))?;
        let glyph_count = u32::from(
            font.maxp()
                .map_err(|error| FaceError::Font(format!("maxp: {error}")))?
                .num_glyphs(),
        );
        if glyph_count == 0 {
            return Err(FaceError::Font("the face has no glyphs".into()));
        }
        font.cmap()
            .map_err(|error| FaceError::Font(format!("cmap: {error}")))?;

        let metrics = Metrics::new(&font, Size::unscaled(), LocationRef::default());
        let attributes = font.attributes();
        let os2 = font.os2().ok();
        let weight = os2
            .as_ref()
            .map_or(400, |os2| os2.us_weight_class())
            .clamp(1, 1000);
        let selection = os2.as_ref().map_or(0, |os2| os2.fs_selection().bits());
        let mac_style = head.mac_style().bits();
        let bold = weight >= 600 || selection & 0x20 != 0 || mac_style & 1 != 0;
        let italic = attributes.style != skrifa::attribute::Style::Normal
            || selection & 0x201 != 0
            || mac_style & 2 != 0;
        let family = name(&font, StringId::TYPOGRAPHIC_FAMILY_NAME)
            .or_else(|| name(&font, StringId::FAMILY_NAME))
            .unwrap_or_default();
        let full_name = name(&font, StringId::FULL_NAME).unwrap_or_else(|| family.clone());
        let postscript_name = name(&font, StringId::POSTSCRIPT_NAME)
            .map(|name| sanitized_name(&name))
            .filter(|name| !name.is_empty())
            .unwrap_or_else(|| sanitized_name(&family));
        let panose = os2.as_ref().map(|os2| os2.panose_10().to_vec());
        let serif = match panose.as_deref() {
            // A Latin text face: serif styles 2 to 10 have serifs, 11 to 13
            // are sans serif (PANOSE 2.0, bSerifStyle).
            Some([2, style, ..]) if (2..=10).contains(style) => true,
            Some([2, style, ..]) if (11..=13).contains(style) => false,
            _ => family.to_lowercase().contains("serif") && !family.to_lowercase().contains("sans"),
        };
        let bbox = metrics.bounds.map_or(
            [
                f64::from(head.x_min()),
                f64::from(head.y_min()),
                f64::from(head.x_max()),
                f64::from(head.y_max()),
            ],
            |bounds| {
                [
                    f64::from(bounds.x_min),
                    f64::from(bounds.y_min),
                    f64::from(bounds.x_max),
                    f64::from(bounds.y_max),
                ]
            },
        );
        let cap_height = metrics
            .cap_height
            .map_or(f64::from(metrics.ascent), f64::from);
        let weight_axis = font
            .axes()
            .iter()
            .find(|axis| axis.tag() == Tag::new(b"wght"))
            .map(|axis| [axis.min_value(), axis.default_value(), axis.max_value()]);
        let program = standalone(&font, cff, data.len())?;
        Ok(Self {
            program,
            cff,
            postscript_name,
            family,
            full_name,
            weight,
            bold,
            italic,
            serif,
            monospaced: metrics.is_monospace,
            units_per_em,
            ascender: f64::from(metrics.ascent),
            descender: f64::from(metrics.descent),
            bbox,
            glyph_count,
            cap_height,
            italic_angle: f64::from(metrics.italic_angle),
            weight_axis,
        })
    }

    /// Returns the face of [`Face::program`] to measure with.
    pub fn font(&self) -> FontRef<'_> {
        FontRef::new(&self.program).expect("the program was read when the face was")
    }
}

/// Measurements of one face, borrowed from its program.
pub struct Measure<'a> {
    font: FontRef<'a>,
    glyphs: GlyphMetrics<'a>,
}

impl<'a> Measure<'a> {
    /// Returns the measurements of `face`.
    #[must_use]
    pub fn new(face: &'a Face) -> Self {
        let font = face.font();
        let glyphs = font.glyph_metrics(Size::unscaled(), LocationRef::default());
        Self { font, glyphs }
    }

    /// Returns the nominal glyph of `character`, if the face has one.
    #[must_use]
    pub fn glyph(&self, character: char) -> Option<GlyphId> {
        self.font
            .charmap()
            .map(character)
            .filter(|glyph| glyph.to_u32() != 0)
    }

    /// Returns the advance of `glyph` in font units.
    #[must_use]
    pub fn advance(&self, glyph: GlyphId) -> f64 {
        self.glyphs.advance_width(glyph).map_or(0.0, f64::from)
    }

    /// Returns the ink box of `glyph` in font units, `None` for a glyph
    /// without ink.
    #[must_use]
    pub fn ink(&self, glyph: GlyphId) -> Option<[f64; 4]> {
        let outlines = self.font.outline_glyphs();
        let outline = outlines.get(glyph)?;
        let mut pen = BoundsPen::default();
        outline
            .draw(
                DrawSettings::unhinted(Size::unscaled(), LocationRef::default()),
                &mut pen,
            )
            .ok()?;
        pen.bounds.map(|bounds| {
            [
                f64::from(bounds.x_min),
                f64::from(bounds.y_min),
                f64::from(bounds.x_max),
                f64::from(bounds.y_max),
            ]
        })
    }

    /// Returns the characters that the face maps, in code point order.
    pub fn chars(&self) -> impl Iterator<Item = char> + '_ {
        self.font
            .charmap()
            .mappings()
            .filter(|(_, glyph)| glyph.to_u32() != 0)
            .filter_map(|(codepoint, _)| char::from_u32(codepoint))
    }
}

#[derive(Default)]
struct BoundsPen {
    bounds: Option<BoundingBox>,
}

impl BoundsPen {
    fn add(&mut self, x: f32, y: f32) {
        let bounds = self.bounds.get_or_insert(BoundingBox {
            x_min: x,
            y_min: y,
            x_max: x,
            y_max: y,
        });
        bounds.x_min = bounds.x_min.min(x);
        bounds.y_min = bounds.y_min.min(y);
        bounds.x_max = bounds.x_max.max(x);
        bounds.y_max = bounds.y_max.max(y);
    }
}

impl OutlinePen for BoundsPen {
    fn move_to(&mut self, x: f32, y: f32) {
        self.add(x, y);
    }
    fn line_to(&mut self, x: f32, y: f32) {
        self.add(x, y);
    }
    // Control points bound the curve; the ink box may be slightly larger
    // than the exact one, as a box from points is.
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

fn name(font: &FontRef<'_>, id: StringId) -> Option<String> {
    font.localized_strings(id)
        .english_or_first()
        .map(|name| name.to_string())
        .filter(|name| !name.trim().is_empty())
}

/// A PDF name for the font: printable ASCII without spaces or delimiters,
/// at most 63 bytes as a PostScript name is.
fn sanitized_name(name: &str) -> String {
    name.chars()
        .filter(|character| character.is_ascii_graphic() && !"()<>[]{}/%#".contains(*character))
        .take(63)
        .collect()
}

/// Writes the face's tables as one sfnt, rewriting a CID-keyed CFF's
/// charset to identity.
fn standalone(font: &FontRef<'_>, cff: bool, input_len: usize) -> Result<Vec<u8>, FaceError> {
    let records = font.table_directory.table_records();
    // The sfnt header stores the directory search fields as u16 values.
    // More than 4095 records cannot be represented without overflowing them.
    if records.len() > 4095 {
        return Err(FaceError::Font("the table directory is too large".into()));
    }
    // Table records can overlap, including thousands of records pointing to
    // the same bytes. Bound the total before copying any of them.
    let mut total = 0usize;
    for record in records {
        let tag = record.tag();
        let Some(data) = font.table_data(tag) else {
            return Err(FaceError::Font(format!("the {tag} table is out of bounds")));
        };
        total = total
            .checked_add(data.as_bytes().len())
            .filter(|&size| size <= input_len)
            .ok_or_else(|| FaceError::Font("the face's tables are too large".into()))?;
    }
    let mut tables = Vec::new();
    tables
        .try_reserve(records.len())
        .map_err(|_| FaceError::Font("the table directory is too large".into()))?;
    for record in records {
        let tag = record.tag();
        let Some(data) = font.table_data(tag) else {
            return Err(FaceError::Font(format!("the {tag} table is out of bounds")));
        };
        let mut data = data.as_bytes().to_vec();
        if tag == Tag::new(b"CFF ") {
            match cff::identity_charset(&data) {
                Ok(Some(rewritten)) => data = rewritten,
                Ok(None) => {}
                Err(()) => return Err(FaceError::Font("the CFF table cannot be read".into())),
            }
        }
        tables.push((tag.to_be_bytes(), data));
    }
    let version: &[u8] = if cff { b"OTTO" } else { &[0, 1, 0, 0] };
    sfnt::write(version, &tables).ok_or_else(|| FaceError::Font("the face is too large".into()))
}
