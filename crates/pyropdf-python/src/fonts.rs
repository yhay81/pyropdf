//! The font programs of `python/pyropdf/_fonts.py`: the glyphs that the
//! pages draw with each font, as hayro resolves them, and the subset of a
//! program to those glyphs.

use std::sync::Arc;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyropdf_engine::extract::{self, Error};
use pyropdf_engine::fonts::embed::{Face, FaceError, GlyphId, Measure};
use pyropdf_engine::fonts::shape::{ShapeError, ShapedGlyph, ShapingFace};
use pyropdf_engine::fonts::{self, Format};
use pyropdf_engine::standard::{self, Base14Font, LayoutError, LineMetrics, Placement};
use pyropdf_store::budget::{Budget, LimitCode};
use pyropdf_store::settle::Structure;

use crate::document::_Document;
use crate::errors::{LimitError, PdfError, limit_error, store_error};
use crate::pixmap::bridge_error;

/// Returns, for each font dictionary of `fonts`, by object number and
/// generation, the sorted glyph IDs that the pages and their annotation
/// appearances draw with it, interpreted with the GIL released.
#[pyfunction]
pub fn used_glyphs(
    py: Python<'_>,
    document: &_Document,
    fonts: Vec<(u32, u16)>,
) -> PyResult<Vec<Vec<u32>>> {
    let (version, index, budget) = crate::pages::index(document)?;
    py.detach(|| {
        let bridged = document
            .bridge
            .get(&version, &index, &budget)
            .map_err(|e| bridge_error(&e))?;
        extract::used_glyphs(&bridged, &fonts, &budget).map_err(|e| match e {
            Error::Limit(refusal) => limit_error(&refusal),
            Error::NoPage(_) | Error::Allocation(_) => PdfError::new_err(e.to_string()),
        })
    })
}

/// Returns font program `data`, of `format` `"sfnt"` or `"cff"`, subset to
/// the glyphs of `keep` with the glyphs that they draw, and the counts of
/// its glyphs and of those kept; `None` when the program is not one that
/// the subsetter reads.
#[pyfunction]
pub fn subset_font_program<'py>(
    py: Python<'py>,
    data: &[u8],
    format: &str,
    keep: Vec<u32>,
) -> PyResult<Option<(Bound<'py, PyBytes>, usize, usize)>> {
    let format = match format {
        "sfnt" => Format::Sfnt,
        "cff" => Format::Cff,
        _ => {
            return Err(PyValueError::new_err(format!(
                "unknown font program format {format:?}"
            )));
        }
    };
    let subset = py.detach(|| fonts::subset(data, format, &keep));
    Ok(subset.map(|subset| (PyBytes::new(py, &subset.data), subset.glyphs, subset.kept)))
}

/// Marks the Type0 font `xref`, which `_fonts.py` wrote for an embedded
/// face, to be subset at save, in this thread's open transaction or in one
/// of its own (DESIGN.md section 7).
#[pyfunction]
pub fn mark_font_subset(py: Python<'_>, document: &_Document, xref: u32) -> PyResult<()> {
    document.edit(py, |transaction, budget| {
        transaction
            .mark(Structure::FontSubset, xref, budget)
            .map_err(|e| store_error(&e))
    })
}

/// The face of a `_Font`.
enum Kind {
    Standard(Base14Font),
    Embedded(Arc<Face>),
}

/// A glyph that shaping placed, as Python receives it: the glyph ID, its
/// advance and offsets in font units, the range of the characters that it
/// draws, and whether its run is right to left.
type Placed = (u32, i32, i32, i32, i32, usize, usize, bool);

/// The face of `python/pyropdf/_fonts.py`'s `Font`: a Standard 14 font,
/// measured with Adobe's metrics and drawn in WinAnsi, or an embedded
/// OpenType face that harfrust shapes (DESIGN.md section 7).
#[pyclass(frozen, module = "pyropdf._core")]
pub struct _Font {
    kind: Kind,
}

impl _Font {
    fn metric(
        font: Base14Font,
        codepoint: u32,
    ) -> PyResult<&'static standard::CharacterMetric<'static>> {
        char::from_u32(codepoint)
            .and_then(|character| standard::char_metric(font, character))
            .ok_or_else(|| missing_glyph(font, codepoint))
    }

    fn em(&self) -> f64 {
        match &self.kind {
            Kind::Standard(_) => 1000.0,
            Kind::Embedded(face) => f64::from(face.units_per_em),
        }
    }

    /// Returns the embedded face, `None` for a Standard 14 font.
    pub(crate) fn embedded(&self) -> Option<&Face> {
        match &self.kind {
            Kind::Standard(_) => None,
            Kind::Embedded(face) => Some(face),
        }
    }

    fn line_metrics(&self) -> LineMetrics {
        match &self.kind {
            Kind::Standard(font) => standard::line_metrics(*font),
            Kind::Embedded(face) => LineMetrics {
                ascender: face.ascender / self.em(),
                descender: face.descender / self.em(),
            },
        }
    }
}

/// Shapes `text` in `face` and refuses a character that it has no glyph for.
fn shaped(face: &Face, text: &str, budget: &Budget) -> PyResult<Vec<ShapedGlyph>> {
    let shaper = ShapingFace::new(&face.program, 0).map_err(|error| shape_error(&error))?;
    let glyphs = shaper
        .shape(text, budget)
        .map_err(|error| shape_error(&error))?
        .into_vec();
    if let Some(character) = glyphs
        .iter()
        .filter(|glyph| glyph.glyph == 0)
        .filter_map(|glyph| text[glyph.cluster.clone()].chars().next())
        .find(|character| !character.is_control())
    {
        return Err(PdfError::new_err(format!(
            "{} does not contain all glyphs of the text: no glyph for U+{:04X} ({character:?})",
            face.full_name,
            u32::from(character)
        )));
    }
    Ok(glyphs)
}

fn shape_error(error: &ShapeError) -> PyErr {
    match error {
        ShapeError::Font(message) => PdfError::new_err(format!("invalid font: {message}")),
        ShapeError::Limit(refusal) => limit_error(refusal),
    }
}

/// Returns the width of `glyphs` at `size` in a face of `em` units.
fn advance(glyphs: &[ShapedGlyph], size: f64, em: f64) -> f64 {
    glyphs
        .iter()
        .map(|glyph| f64::from(glyph.x_advance))
        .sum::<f64>()
        * size
        / em
}

#[pymethods]
impl _Font {
    /// Returns the Standard 14 font of a PostScript name.
    #[staticmethod]
    fn standard(name: &str) -> PyResult<Self> {
        standard::font(name)
            .map(|font| Self {
                kind: Kind::Standard(font),
            })
            .ok_or_else(|| PyValueError::new_err(format!("unknown Standard 14 font: {name:?}")))
    }

    /// Reads face `index` of an OpenType font file or collection.
    #[staticmethod]
    fn from_bytes(py: Python<'_>, data: &[u8], index: u32) -> PyResult<Self> {
        let face = py
            .detach(|| Face::read(data, index))
            .map_err(|error| match error {
                FaceError::Font(message) => PdfError::new_err(format!("invalid font: {message}")),
                FaceError::Unsupported(message) => {
                    PdfError::new_err(format!("unsupported font: {message}"))
                }
            })?;
        Ok(Self {
            kind: Kind::Embedded(Arc::new(face)),
        })
    }

    /// The PostScript name of a Standard 14 font, `None` for an embedded face.
    #[getter]
    fn standard_name(&self) -> Option<&'static str> {
        match &self.kind {
            Kind::Standard(font) => Some(font.pdf_base_name()),
            Kind::Embedded(_) => None,
        }
    }

    /// The name that a PDF font dictionary gives the face.
    #[getter]
    fn postscript_name(&self) -> &str {
        match &self.kind {
            Kind::Standard(font) => font.pdf_base_name(),
            Kind::Embedded(face) => &face.postscript_name,
        }
    }

    #[getter(name)]
    fn full_name(&self) -> &str {
        match &self.kind {
            Kind::Standard(font) => font.pdf_base_name(),
            Kind::Embedded(face) => &face.full_name,
        }
    }

    #[getter]
    fn family(&self) -> &str {
        match &self.kind {
            Kind::Standard(font) => {
                let name = font.pdf_base_name();
                name.split('-').next().unwrap_or(name)
            }
            Kind::Embedded(face) => &face.family,
        }
    }

    #[getter]
    fn bold(&self) -> bool {
        match &self.kind {
            Kind::Standard(font) => font.pdf_base_name().contains("Bold"),
            Kind::Embedded(face) => face.bold,
        }
    }

    #[getter]
    fn italic(&self) -> bool {
        match &self.kind {
            Kind::Standard(font) => {
                let name = font.pdf_base_name();
                name.contains("Italic") || name.contains("Oblique")
            }
            Kind::Embedded(face) => face.italic,
        }
    }

    #[getter]
    fn serif(&self) -> bool {
        match &self.kind {
            Kind::Standard(font) => font.pdf_base_name().starts_with("Times"),
            Kind::Embedded(face) => face.serif,
        }
    }

    #[getter]
    fn weight(&self) -> u16 {
        match &self.kind {
            Kind::Standard(_) if self.bold() => 700,
            Kind::Standard(_) => 400,
            Kind::Embedded(face) => face.weight,
        }
    }

    /// The minimum, default, and maximum of an embedded face's weight axis.
    #[getter]
    fn weight_axis(&self) -> Option<(f32, f32, f32)> {
        match &self.kind {
            Kind::Standard(_) => None,
            Kind::Embedded(face) => face
                .weight_axis
                .map(|[low, default, high]| (low, default, high)),
        }
    }

    #[getter]
    fn units_per_em(&self) -> u16 {
        match &self.kind {
            Kind::Standard(_) => 1000,
            Kind::Embedded(face) => face.units_per_em,
        }
    }

    #[getter]
    fn ascender(&self) -> f64 {
        self.line_metrics().ascender
    }

    #[getter]
    fn descender(&self) -> f64 {
        self.line_metrics().descender
    }

    /// The face's box in fractions of the em.
    #[getter]
    fn bbox(&self) -> (f64, f64, f64, f64) {
        match &self.kind {
            Kind::Standard(font) => {
                let [x0, y0, x1, y1] = standard::font_bbox(*font);
                (x0, y0, x1, y1)
            }
            Kind::Embedded(face) => {
                let em = self.em();
                let [x0, y0, x1, y1] = face.bbox;
                (x0 / em, y0 / em, x1 / em, y1 / em)
            }
        }
    }

    /// The cap height and the italic angle that a font descriptor records,
    /// in font units scaled to 1000 per em and in degrees.
    #[getter]
    fn descriptor_metrics(&self) -> (f64, f64) {
        match &self.kind {
            Kind::Standard(_) => (0.0, 0.0),
            Kind::Embedded(face) => (face.cap_height * 1000.0 / self.em(), face.italic_angle),
        }
    }

    #[getter]
    fn glyph_count(&self) -> usize {
        match &self.kind {
            Kind::Standard(font) => font.metrics().character_metrics.len(),
            Kind::Embedded(face) => face.glyph_count as usize,
        }
    }

    #[getter]
    fn monospaced(&self) -> bool {
        match &self.kind {
            Kind::Standard(font) => font.metrics().is_fixed_pitch,
            Kind::Embedded(face) => face.monospaced,
        }
    }

    /// Whether the embedded program has CFF outlines.
    #[getter]
    fn cff(&self) -> bool {
        matches!(&self.kind, Kind::Embedded(face) if face.cff)
    }

    /// The embedded program with all its glyphs, `None` for a Standard 14 font.
    fn program<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        match &self.kind {
            Kind::Standard(_) => None,
            Kind::Embedded(face) => Some(PyBytes::new(py, &face.program)),
        }
    }

    fn chars(&self) -> String {
        match &self.kind {
            Kind::Standard(font) => standard::chars(*font).collect(),
            Kind::Embedded(face) => Measure::new(face).chars().collect(),
        }
    }

    fn has_glyph(&self, codepoint: u32) -> bool {
        char::from_u32(codepoint).is_some_and(|character| match &self.kind {
            Kind::Standard(font) => standard::glyph_code(*font, character).is_some(),
            Kind::Embedded(face) => Measure::new(face).glyph(character).is_some(),
        })
    }

    /// Returns the advance of each character drawn alone, at `size`.
    fn char_widths(&self, text: &str, size: f64) -> PyResult<Vec<f64>> {
        match &self.kind {
            Kind::Standard(font) => text
                .chars()
                .map(|character| {
                    Ok(
                        f64::from(Self::metric(*font, u32::from(character))?.width_x) * size
                            / 1000.0,
                    )
                })
                .collect(),
            Kind::Embedded(face) => {
                let measure = Measure::new(face);
                text.chars()
                    .map(|character| {
                        let glyph = measure
                            .glyph(character)
                            .ok_or_else(|| embedded_missing(face, character))?;
                        Ok(measure.advance(glyph) * size / self.em())
                    })
                    .collect()
            }
        }
    }

    /// Returns the ink box of a character's glyph in fractions of the em.
    fn char_bbox(&self, codepoint: u32) -> PyResult<(f64, f64, f64, f64)> {
        match &self.kind {
            Kind::Standard(font) => {
                let metric = Self::metric(*font, codepoint)?;
                Ok(metric.bbox.as_ref().map_or((0.0, 0.0, 0.0, 0.0), |bbox| {
                    (
                        f64::from(bbox.llx) / 1000.0,
                        f64::from(bbox.lly) / 1000.0,
                        f64::from(bbox.urx) / 1000.0,
                        f64::from(bbox.ury) / 1000.0,
                    )
                }))
            }
            Kind::Embedded(face) => {
                let measure = Measure::new(face);
                let character = char::from_u32(codepoint).unwrap_or('\u{FFFD}');
                let glyph = measure
                    .glyph(character)
                    .ok_or_else(|| embedded_missing(face, character))?;
                let em = self.em();
                Ok(measure
                    .ink(glyph)
                    .map_or((0.0, 0.0, 0.0, 0.0), |[x0, y0, x1, y1]| {
                        (x0 / em, y0 / em, x1 / em, y1 / em)
                    }))
            }
        }
    }

    /// Returns the width of one line of `text` at `size`.
    fn text_width(&self, py: Python<'_>, text: &str, size: f64) -> PyResult<f64> {
        match &self.kind {
            Kind::Standard(font) => {
                if let Some(character) = text
                    .chars()
                    .find(|character| standard::glyph_code(*font, *character).is_none())
                {
                    return Err(missing_glyph(*font, u32::from(character)));
                }
                standard::text_width(text, *font, size)
                    .ok_or_else(|| PdfError::new_err(LayoutError::NotWinAnsi.to_string()))
            }
            Kind::Embedded(face) => {
                let em = self.em();
                py.detach(|| Ok(advance(&shaped(face, text, &Budget::default())?, size, em)))
            }
        }
    }

    /// Shapes one line of `text` in an embedded face, in visual order.
    fn shape(&self, py: Python<'_>, text: &str) -> PyResult<Vec<Placed>> {
        let Kind::Embedded(face) = &self.kind else {
            return Err(PyValueError::new_err("a Standard 14 font is not shaped"));
        };
        py.detach(|| {
            // Python indexes text by character, the shaper by UTF-8 byte.
            let mut characters = vec![0; text.len() + 1];
            for (index, (at, character)) in text.char_indices().enumerate() {
                characters[at..at + character.len_utf8()].fill(index);
            }
            characters[text.len()] = text.chars().count();
            Ok(shaped(face, text, &Budget::default())?
                .into_iter()
                .map(|glyph| {
                    (
                        glyph.glyph,
                        glyph.x_advance,
                        glyph.y_advance,
                        glyph.x_offset,
                        glyph.y_offset,
                        characters[glyph.text.start],
                        characters[glyph.text.end],
                        glyph.rtl,
                    )
                })
                .collect())
        })
    }

    /// Returns the advances of `glyphs` of an embedded face in thousandths
    /// of the em, as a CIDFont's `W` array records them.
    fn glyph_widths(&self, glyphs: Vec<u32>) -> PyResult<Vec<f64>> {
        let Kind::Embedded(face) = &self.kind else {
            return Err(PyValueError::new_err("a Standard 14 font has no glyph IDs"));
        };
        let measure = Measure::new(face);
        let em = self.em();
        Ok(glyphs
            .into_iter()
            .map(|glyph| measure.advance(GlyphId::new(glyph)) * 1000.0 / em)
            .collect())
    }

    /// Returns the distance between baselines at `size`: `line_height` em,
    /// by default the font's own line height.
    #[pyo3(signature = (size, line_height=None))]
    fn leading(&self, size: f64, line_height: Option<f64>) -> PyResult<f64> {
        self.line_metrics()
            .leading(size, line_height)
            .ok_or_else(|| PyValueError::new_err(LayoutError::InvalidGeometry.to_string()))
    }

    /// Returns the WinAnsi bytes that draw `text` in a Standard 14 font.
    fn encode<'py>(&self, py: Python<'py>, text: &str) -> PyResult<Bound<'py, PyBytes>> {
        let bytes = text
            .chars()
            .map(standard::winansi_byte)
            .collect::<Option<Vec<u8>>>()
            .ok_or_else(|| PdfError::new_err(LayoutError::NotWinAnsi.to_string()))?;
        Ok(PyBytes::new(py, &bytes))
    }

    /// Lays out `text` in a box `width` by `height`, its lines placed as
    /// canvas text places them, and returns the lines with their widths
    /// and whether each justifies, the height that they need, the spare
    /// height, the first baseline's depth in em, and the leading. At most
    /// `max_lines` lines, or any number for `None`.
    #[pyo3(signature = (text, width, height, size, line_height, justify, max_lines))]
    #[expect(clippy::too_many_arguments, reason = "the options of a text box")]
    #[expect(clippy::type_complexity, reason = "a tuple that Python unpacks")]
    fn layout(
        &self,
        py: Python<'_>,
        text: &str,
        width: f64,
        height: f64,
        size: f64,
        line_height: Option<f64>,
        justify: bool,
        max_lines: Option<u64>,
    ) -> PyResult<(Vec<(String, f64, bool)>, f64, f64, f64, f64)> {
        let placement = Placement::lines(self.line_metrics(), line_height);
        let em = self.em();
        py.detach(|| {
            let budget = Budget::default().with_limit(LimitCode::TextLineCount, max_lines);
            let layout = match &self.kind {
                Kind::Standard(font) => standard::textbox_layout(
                    text,
                    (width, height),
                    *font,
                    size,
                    placement,
                    justify,
                    &budget,
                ),
                Kind::Embedded(face) => {
                    for line in text.lines() {
                        shaped(face, line, &budget)?;
                    }
                    standard::layout(
                        text,
                        (width, height),
                        size,
                        placement,
                        justify,
                        &budget,
                        |line| {
                            shaped(face, line, &budget)
                                .map_or(f64::INFINITY, |glyphs| advance(&glyphs, size, em))
                        },
                    )
                }
            }
            .map_err(|error| layout_error(&error, max_lines))?;
            let lines = layout
                .lines
                .iter()
                .map(|line| (line.text.to_owned(), line.width, line.justify))
                .collect();
            Ok((
                lines,
                layout.height,
                layout.spare_height,
                layout.ascent,
                layout.leading,
            ))
        })
    }

    fn __repr__(&self) -> String {
        format!("_Font({:?})", self.full_name())
    }
}

/// Refuses a character that the font has no glyph for, naming it.
fn missing_glyph(font: Base14Font, codepoint: u32) -> PyErr {
    let shown =
        char::from_u32(codepoint).map_or_else(String::new, |character| format!("{character:?} "));
    PdfError::new_err(format!(
        "{} has no glyph for {shown}(U+{codepoint:04X}); a Standard 14 font draws WinAnsi text",
        font.pdf_base_name()
    ))
}

/// Refuses a character that an embedded face has no glyph for, naming it.
fn embedded_missing(face: &Face, character: char) -> PyErr {
    PdfError::new_err(format!(
        "{} does not contain all glyphs of the text: no glyph for U+{:04X} ({character:?})",
        face.full_name,
        u32::from(character)
    ))
}

/// Maps a refused layout: too many lines is the limit `text_line_count`.
fn layout_error(error: &LayoutError, max_lines: Option<u64>) -> PyErr {
    match error {
        LayoutError::Lines => LimitError::new_err((
            LimitCode::TextLineCount.as_str(),
            format!(
                "the text lays out in more than {} lines",
                max_lines.unwrap_or(u64::MAX)
            ),
        )),
        LayoutError::Limit(refusal) => limit_error(refusal),
        LayoutError::InvalidGeometry => PyValueError::new_err(error.to_string()),
        _ => PdfError::new_err(error.to_string()),
    }
}
