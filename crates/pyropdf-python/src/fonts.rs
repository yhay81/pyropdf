//! The font programs of `python/pyropdf/_fonts.py`: the glyphs that the
//! pages draw with each font, as hayro resolves them, and the subset of a
//! program to those glyphs.

use std::ops::Deref;
use std::path::PathBuf;
use std::sync::Arc;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyFloat, PyList, PyString, PyTuple};
use pyropdf_engine::extract::{self, Error};
use pyropdf_engine::fonts::bitmap::{self, Bitmaps, DRAWING_PPI};
use pyropdf_engine::fonts::colour::{Painted, Refused, refused_canvas_glyph};
use pyropdf_engine::fonts::colr::{Colr, ColrError, Verdict};
use pyropdf_engine::fonts::embed::{Face, FaceError, GlyphId, Measure};
use pyropdf_engine::fonts::instance::InstanceError;
use pyropdf_engine::fonts::shape::{ShapeError, ShapedGlyph, ShapingFace};
use pyropdf_engine::fonts::type3::{Code, Fonts, Glyph};
use pyropdf_engine::fonts::{self, Format};
use pyropdf_engine::render::{Fallback, FontFile, Language, Source};
use pyropdf_engine::standard::{self, Base14Font, LayoutError, LineMetrics, Placement};
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode, SharedBudget};
use pyropdf_store::settle::Structure;
use pyropdf_store::value::{Name, Reference, Value};

use crate::document::_Document;
use crate::errors::{LimitError, PdfError, limit_error, store_error};
use crate::history::OperationBudget;
use crate::pixmap::bridge_error;
use crate::shadings::CanvasShadings;

/// Returns, for each font dictionary of `fonts`, by object number and
/// generation, the sorted glyph IDs that the pages and their annotation
/// appearances draw with it, interpreted with the GIL released.
#[pyfunction]
pub fn used_glyphs(
    py: Python<'_>,
    document: &_Document,
    fonts: Vec<(u32, u16)>,
) -> PyResult<Vec<Vec<u32>>> {
    crate::errors::boundary(|| {
        let (version, index, budget) = crate::pages::index(document)?;
        py.detach(|| {
            let bridged = document
                .bridge
                .get(&version, &index, &budget)
                .map_err(|e| bridge_error(&e))?;
            extract::used_glyphs(&bridged, &version, &index, &fonts, &budget).map_err(|e| match e {
                Error::Limit(refusal) => limit_error(&refusal),
                Error::NoPage(_) | Error::Allocation(_) => PdfError::new_err(e.to_string()),
            })
        })
    })?
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
    crate::errors::boundary(|| {
        let format = match format {
            "sfnt" => Format::Sfnt,
            "cff" => Format::Cff,
            _ => {
                return Err(PyValueError::new_err(format!(
                    "unknown font program format {format:?}"
                )));
            }
        };
        // An unknown glyph use means the program must stay whole, even when a
        // caller invokes this internal helper directly instead of subset_fonts.
        if keep.contains(&u32::MAX) {
            return Ok(None);
        }
        let subset = py.detach(|| fonts::subset(data, format, &keep));
        Ok(subset.map(|subset| (PyBytes::new(py, &subset.data), subset.glyphs, subset.kept)))
    })?
}

/// Marks the Type0 font `xref`, which `_fonts.py` wrote for an embedded
/// face, to be subset at save, in this thread's open transaction or in one
/// of its own (DESIGN.md section 7).
#[pyfunction]
pub fn mark_font_subset(py: Python<'_>, document: &_Document, xref: u32) -> PyResult<()> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            transaction
                .mark(Structure::FontSubset, xref, budget)
                .map_err(|e| store_error(&e))
        })
    })?
}

/// A new glyph of a Type 3 font, as `_canvas.py` gives it: its name, advance
/// in font units, the image's box in font units, and the image's object
/// number. A glyph with no image paints nothing.
type NewBitmap = (String, f64, Option<(f64, f64, f64, f64)>, Option<u32>);

/// Returns the font and code of each of `names` in the Type 3 fonts `fonts`
/// of a face of `units_per_em`, `None` for one that they do not hold.
#[pyfunction]
pub fn type3_codes(
    document: &_Document,
    fonts: Vec<u32>,
    units_per_em: u16,
    names: Vec<String>,
) -> PyResult<Vec<Option<(u32, u8)>>> {
    crate::errors::boundary(|| {
        let version = document.reading()?;
        let budget = document.budget();
        let held =
            Fonts::read(&version, &fonts, units_per_em, &budget).map_err(|e| store_error(&e))?;
        Ok(names
            .iter()
            .map(|name| {
                held.find(name.as_bytes())
                    .map(|code| (code.font, code.code))
            })
            .collect())
    })?
}

/// Adds the bitmap glyphs `glyphs` and the COLR glyphs `painted`, each a name,
/// an advance, and a glyph ID of `face`, to the Type 3 fonts `fonts` of a face
/// of `units_per_em`, in this thread's open transaction or in one of its own,
/// and maps the glyph of each of `drawn`, a name with a text, to that text.
/// Returns the font, the code, and the text that the CMap maps of each of
/// `drawn`. Writes only what changed (DESIGN.md section 7). `shadings` shares
/// the document's shading objects.
#[pyfunction]
#[pyo3(signature = (document, fonts, units_per_em, glyphs, drawn, *, face=None, shadings=None, painted=Vec::new()))]
#[expect(
    clippy::too_many_arguments,
    reason = "what a drawing adds to the fonts"
)]
pub fn type3_draw(
    py: Python<'_>,
    document: &_Document,
    fonts: Vec<u32>,
    units_per_em: u16,
    glyphs: Vec<NewBitmap>,
    drawn: Vec<(String, String)>,
    face: Option<PyRef<'_, _Font>>,
    shadings: Option<PyRef<'_, CanvasShadings>>,
    painted: Vec<(String, f64, u32)>,
) -> PyResult<Vec<(u32, u8, Option<String>)>> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            let mut held = Fonts::read(transaction.version(), &fonts, units_per_em, budget)
                .map_err(|e| store_error(&e))?;
            let mut new = BudgetVec::new(budget);
            new.try_reserve_exact(glyphs.len())
                .map_err(|e| limit_error(&e))?;
            for (name, width, rect, image) in &glyphs {
                new.try_push(bitmap_glyph(name, *width, *rect, *image))
                    .map_err(|e| limit_error(&e))?;
            }
            if !painted.is_empty() {
                let (Some(face), Some(shadings)) = (face.as_deref(), shadings.as_deref()) else {
                    return Err(PyValueError::new_err(
                        "a COLR glyph needs its face and shadings",
                    ));
                };
                let Kind::Embedded(face) = &face.kind else {
                    return Err(PyValueError::new_err("a Standard 14 font has no glyph IDs"));
                };
                new.try_reserve(painted.len())
                    .map_err(|e| limit_error(&e))?;
                Colr::of_face(face, |colr| {
                    let colr = colr
                        .ok_or_else(|| PdfError::new_err("the face has no COLR table to paint"))?;
                    let mut table = shadings.lock();
                    for (name, width, glyph) in &painted {
                        let glyph_id = GlyphId::new(*glyph);
                        let name = Name::from(name.as_str());
                        let glyph = colr
                            .paint(transaction, &mut table, glyph_id, name, *width, budget)
                            .map_err(|error| colr_error(face, *glyph, &error))?;
                        new.try_push(glyph).map_err(|e| limit_error(&e))?;
                    }
                    Ok::<_, PyErr>(())
                })?;
            }
            held.add(transaction, new.into_vec())
                .map_err(|e| store_error(&e))?;
            let mut codes = BudgetVec::<Code>::new(budget);
            codes
                .try_reserve_exact(drawn.len())
                .map_err(|e| limit_error(&e))?;
            for (name, text) in &drawn {
                let code = held.find(name.as_bytes()).ok_or_else(|| {
                    PdfError::new_err(format!("no Type 3 font holds the glyph {name}"))
                })?;
                held.map_text(code, text).map_err(|e| store_error(&e))?;
                codes.try_push(code).map_err(|e| limit_error(&e))?;
            }
            held.write(transaction).map_err(|e| store_error(&e))?;
            Ok(codes
                .iter()
                .map(|&code| (code.font, code.code, held.text(code).map(str::to_owned)))
                .collect())
        })
    })?
}

/// Returns the Type 3 glyph that paints the image `image` over `rect`,
/// whose resources name the image as the glyph itself.
fn bitmap_glyph(
    name: &str,
    width: f64,
    rect: Option<(f64, f64, f64, f64)>,
    image: Option<u32>,
) -> Glyph {
    let glyph = Name::from(name);
    match (rect, image) {
        (Some((x, y, across, up)), Some(image)) => Glyph {
            name: glyph.clone(),
            width,
            ink: Some([x, y, x + across, y + up]),
            procedure: bitmap::procedure(width, [x, y, across, up], name),
            resources: vec![(
                Name::from("XObject"),
                glyph,
                Value::Reference(Reference::new(image, 0)),
            )],
        },
        _ => Glyph {
            name: glyph,
            width,
            ink: None,
            procedure: bitmap::empty_procedure(width),
            resources: Vec::new(),
        },
    }
}

/// The face of a `_Font`.
enum Kind {
    Standard(Base14Font),
    Embedded(Arc<Face>),
}

/// How canvas text draws a glyph in a Type 3 font: the glyph's name there, its
/// advance in font units, and the pixels per em of its strike, 0 for a glyph
/// that paints nothing and `None` for one that COLR paints.
type ColourGlyph = (String, f64, Option<u16>);

/// A glyph that shaping placed, as Python receives it: the glyph ID, its
/// advance and offsets in font units, the range of the characters that it
/// draws, whether its run is right to left, and the range of its cluster.
type Placed = (u32, i32, i32, i32, i32, usize, usize, bool, usize, usize);

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
    let shaper = ShapingFace::of(face).map_err(|error| shape_error(&error))?;
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

fn face_error(error: &FaceError) -> PyErr {
    match error {
        FaceError::Font(message) => PdfError::new_err(format!("invalid font: {message}")),
        FaceError::Unsupported(message) => {
            PdfError::new_err(format!("unsupported font: {message}"))
        }
    }
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
        crate::errors::boundary(|| {
            standard::font(name)
                .map(|font| Self {
                    kind: Kind::Standard(font),
                })
                .ok_or_else(|| PyValueError::new_err(format!("unknown Standard 14 font: {name:?}")))
        })?
    }

    /// Reads face `index` of an OpenType font file or collection.
    #[staticmethod]
    fn from_bytes(py: Python<'_>, data: &[u8], index: u32) -> PyResult<Self> {
        crate::errors::boundary(|| {
            let face = py
                .detach(|| Face::read(data, index))
                .map_err(|error| face_error(&error))?;
            Ok(Self {
                kind: Kind::Embedded(Arc::new(face)),
            })
        })?
    }

    /// Returns the instance at `weight` on an embedded face's weight axis,
    /// whose static program stays within `max_size` bytes.
    fn at_weight(&self, py: Python<'_>, weight: u16, max_size: Option<u64>) -> PyResult<Self> {
        crate::errors::boundary(|| {
            let Kind::Embedded(face) = &self.kind else {
                return Err(PyValueError::new_err("a Standard 14 font has no instances"));
            };
            let budget = Budget::default().with_limit(LimitCode::FontInputSize, max_size);
            let instance = py
                .detach(|| face.at_weight(weight, &budget))
                .map_err(|error| match error {
                    InstanceError::Refused(message) => PdfError::new_err(message),
                    InstanceError::Font(error) => face_error(&error),
                    // The file passed its limit already; what it flattens to did not.
                    InstanceError::Limit(refusal) => LimitError::new_err((
                        refusal.code().as_str(),
                        format!(
                            "the program of this instance is over the {}-byte limit",
                            refusal.limit().unwrap_or_default()
                        ),
                    )),
                })?;
            Ok(Self {
                kind: Kind::Embedded(Arc::new(instance)),
            })
        })?
    }

    /// The PostScript name of a Standard 14 font, `None` for an embedded face.
    #[getter]
    fn standard_name(&self) -> PyResult<Option<&'static str>> {
        crate::errors::boundary(|| match &self.kind {
            Kind::Standard(font) => Some(font.pdf_base_name()),
            Kind::Embedded(_) => None,
        })
    }

    /// The name that a PDF font dictionary gives the face.
    #[getter]
    fn postscript_name(&self) -> PyResult<&str> {
        crate::errors::boundary(|| match &self.kind {
            Kind::Standard(font) => font.pdf_base_name(),
            Kind::Embedded(face) => &face.postscript_name,
        })
    }

    #[getter(name)]
    fn full_name(&self) -> PyResult<&str> {
        crate::errors::boundary(|| match &self.kind {
            Kind::Standard(font) => font.pdf_base_name(),
            Kind::Embedded(face) => &face.full_name,
        })
    }

    #[getter]
    fn family(&self) -> PyResult<&str> {
        crate::errors::boundary(|| match &self.kind {
            Kind::Standard(font) => {
                let name = font.pdf_base_name();
                name.split('-').next().unwrap_or(name)
            }
            Kind::Embedded(face) => &face.family,
        })
    }

    #[getter]
    fn bold(&self) -> PyResult<bool> {
        crate::errors::boundary(|| match &self.kind {
            Kind::Standard(font) => font.pdf_base_name().contains("Bold"),
            Kind::Embedded(face) => face.bold,
        })
    }

    #[getter]
    fn italic(&self) -> PyResult<bool> {
        crate::errors::boundary(|| match &self.kind {
            Kind::Standard(font) => {
                let name = font.pdf_base_name();
                name.contains("Italic") || name.contains("Oblique")
            }
            Kind::Embedded(face) => face.italic,
        })
    }

    #[getter]
    fn serif(&self) -> PyResult<bool> {
        crate::errors::boundary(|| match &self.kind {
            Kind::Standard(font) => font.pdf_base_name().starts_with("Times"),
            Kind::Embedded(face) => face.serif,
        })
    }

    #[getter]
    fn weight(&self) -> PyResult<u16> {
        crate::errors::boundary(|| {
            Ok(match &self.kind {
                Kind::Standard(_) if self.bold()? => 700,
                Kind::Standard(_) => 400,
                Kind::Embedded(face) => face.weight,
            })
        })?
    }

    /// The minimum, default, and maximum of an embedded face's weight axis;
    /// `None` for an instance, whose program has no axes.
    #[getter]
    fn weight_axis(&self) -> PyResult<Option<(f32, f32, f32)>> {
        crate::errors::boundary(|| match &self.kind {
            Kind::Standard(_) => None,
            Kind::Embedded(face) => face
                .weight_axis
                .map(|[low, default, high]| (low, default, high)),
        })
    }

    #[getter]
    fn units_per_em(&self) -> PyResult<u16> {
        crate::errors::boundary(|| match &self.kind {
            Kind::Standard(_) => 1000,
            Kind::Embedded(face) => face.units_per_em,
        })
    }

    #[getter]
    fn ascender(&self) -> PyResult<f64> {
        crate::errors::boundary(|| self.line_metrics().ascender)
    }

    #[getter]
    fn descender(&self) -> PyResult<f64> {
        crate::errors::boundary(|| self.line_metrics().descender)
    }

    /// The face's box in fractions of the em.
    #[getter]
    fn bbox(&self) -> PyResult<(f64, f64, f64, f64)> {
        crate::errors::boundary(|| match &self.kind {
            Kind::Standard(font) => {
                let [x0, y0, x1, y1] = standard::font_bbox(*font);
                (x0, y0, x1, y1)
            }
            Kind::Embedded(face) => {
                let em = self.em();
                let [x0, y0, x1, y1] = face.bbox;
                (x0 / em, y0 / em, x1 / em, y1 / em)
            }
        })
    }

    /// The cap height and the italic angle that a font descriptor records,
    /// in font units scaled to 1000 per em and in degrees.
    #[getter]
    fn descriptor_metrics(&self) -> PyResult<(f64, f64)> {
        crate::errors::boundary(|| match &self.kind {
            Kind::Standard(_) => (0.0, 0.0),
            Kind::Embedded(face) => (face.cap_height * 1000.0 / self.em(), face.italic_angle),
        })
    }

    #[getter]
    fn glyph_count(&self) -> PyResult<usize> {
        crate::errors::boundary(|| match &self.kind {
            Kind::Standard(font) => font.metrics().character_metrics.len(),
            Kind::Embedded(face) => face.glyph_count as usize,
        })
    }

    #[getter]
    fn monospaced(&self) -> PyResult<bool> {
        crate::errors::boundary(|| match &self.kind {
            Kind::Standard(font) => font.metrics().is_fixed_pitch,
            Kind::Embedded(face) => face.monospaced,
        })
    }

    /// Whether an embedded face has outlines, which a CIDFont needs.
    #[getter]
    fn outlines(&self) -> PyResult<bool> {
        crate::errors::boundary(|| !matches!(&self.kind, Kind::Embedded(face) if !face.outlines))
    }

    /// Whether the embedded program has CFF outlines.
    #[getter]
    fn cff(&self) -> PyResult<bool> {
        crate::errors::boundary(|| matches!(&self.kind, Kind::Embedded(face) if face.cff))
    }

    /// The embedded program with all its glyphs, `None` for a Standard 14 font.
    fn program<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyBytes>>> {
        crate::errors::boundary(|| match &self.kind {
            Kind::Standard(_) => None,
            Kind::Embedded(face) => Some(PyBytes::new(py, &face.program)),
        })
    }

    fn chars(&self) -> PyResult<String> {
        crate::errors::boundary(|| match &self.kind {
            Kind::Standard(font) => standard::chars(*font).collect(),
            Kind::Embedded(face) => Measure::new(face).chars().collect(),
        })
    }

    fn has_glyph(&self, codepoint: u32) -> PyResult<bool> {
        crate::errors::boundary(|| {
            char::from_u32(codepoint).is_some_and(|character| match &self.kind {
                Kind::Standard(font) => standard::glyph_code(*font, character).is_some(),
                Kind::Embedded(face) => Measure::new(face).glyph(character).is_some(),
            })
        })
    }

    /// Returns the advance of each character drawn alone, at `size`.
    #[pyo3(signature = (text, size, max_text_size=Some(1_048_576)))]
    fn char_widths<'py>(
        &self,
        py: Python<'py>,
        text: &Bound<'_, PyString>,
        size: f64,
        max_text_size: Option<u64>,
    ) -> PyResult<Bound<'py, PyTuple>> {
        crate::errors::boundary(|| {
            let (budget, text) =
                text_budget(text, None, &[(LimitCode::TextInputSize, max_text_size)])?;
            let mut widths = BudgetVec::new(&budget);
            for character in text.chars() {
                let width = match &self.kind {
                    Kind::Standard(font) => {
                        f64::from(Self::metric(*font, u32::from(character))?.width_x) * size
                            / 1000.0
                    }
                    Kind::Embedded(face) => {
                        let measure = Measure::new(face);
                        let glyph = measure
                            .glyph(character)
                            .ok_or_else(|| embedded_missing(face, character))?;
                        measure.advance(glyph) * size / self.em()
                    }
                };
                widths
                    .try_push(width)
                    .map_err(|error| limit_error(&error))?;
            }
            let count = u64::try_from(widths.len()).unwrap_or(u64::MAX);
            let tuple_size = py
                .get_type::<PyTuple>()
                .getattr("__basicsize__")?
                .extract::<u64>()?;
            let float_size = py
                .get_type::<PyFloat>()
                .getattr("__basicsize__")?
                .extract::<u64>()?;
            let slot_size = u64::try_from(std::mem::size_of::<usize>()).unwrap_or(u64::MAX);
            let output_size = tuple_size
                .saturating_add(count.saturating_mul(float_size.saturating_add(slot_size)));
            budget
                .charge(LimitCode::MemorySize, output_size)
                .map_err(|error| limit_error(&error))?;
            PyTuple::new(py, widths.iter().copied())
        })?
    }

    /// Returns the ink box of a character's glyph in fractions of the em.
    fn char_bbox(&self, codepoint: u32) -> PyResult<(f64, f64, f64, f64)> {
        crate::errors::boundary(|| {
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
                    // A strike's image is what a canvas paints; a damaged table
                    // measures as the outline, as it does when nothing is drawn.
                    let font = face.font();
                    let painted = Bitmaps::read(&font)
                        .ok()
                        .and_then(|bitmaps| bitmaps.best(glyph, f64::from(u16::MAX)))
                        .map(|found| {
                            let [x, y, width, height] = found.rect;
                            [x, y, x + width, y + height]
                        });
                    Ok(painted
                        .or_else(|| measure.ink(glyph))
                        .map_or((0.0, 0.0, 0.0, 0.0), |[x0, y0, x1, y1]| {
                            (x0 / em, y0 / em, x1 / em, y1 / em)
                        }))
                }
            }
        })?
    }

    /// Returns the width of one line of `text` at `size`.
    #[pyo3(signature = (text, size, max_text_size=Some(1_048_576)))]
    fn text_width(
        &self,
        py: Python<'_>,
        text: &Bound<'_, PyString>,
        size: f64,
        max_text_size: Option<u64>,
    ) -> PyResult<f64> {
        crate::errors::boundary(|| {
            let (budget, text) =
                text_budget(text, None, &[(LimitCode::TextInputSize, max_text_size)])?;
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
                    py.detach(|| Ok(advance(&shaped(face, text, &budget)?, size, em)))
                }
            }
        })?
    }

    /// Shapes one line of `text` to draw it on a canvas in an embedded face,
    /// in visual order, and refuses a colour glyph that it cannot paint and
    /// that has no outline, which would draw nothing under text that reads
    /// back. With `operation`, as for [`_Font::encode`].
    #[pyo3(signature = (text, *, operation=None))]
    fn shape(
        &self,
        py: Python<'_>,
        text: &str,
        operation: Option<PyRef<'_, OperationBudget>>,
    ) -> PyResult<Vec<Placed>> {
        crate::errors::boundary(|| {
            let Kind::Embedded(face) = &self.kind else {
                return Err(PyValueError::new_err("a Standard 14 font is not shaped"));
            };
            let budget = match operation.as_deref() {
                Some(operation) => {
                    operation.ensure_active()?;
                    TextBudget::shared(operation.operation.clone())
                }
                None => TextBudget {
                    budget: Budget::default().share(),
                    start: None,
                },
            };
            // The character index of each byte, and the glyphs that Python
            // receives.
            let index_size = u64::try_from(text.len().saturating_add(1))
                .unwrap_or(u64::MAX)
                .saturating_mul(u64::try_from(std::mem::size_of::<usize>()).unwrap_or(u64::MAX));
            budget
                .charge(LimitCode::MemorySize, index_size)
                .map_err(|error| limit_error(&error))?;
            let budget = &budget;
            py.detach(|| {
                // Python indexes text by character, the shaper by UTF-8 byte.
                let mut characters = vec![0; text.len() + 1];
                for (index, (at, character)) in text.char_indices().enumerate() {
                    characters[at..at + character.len_utf8()].fill(index);
                }
                characters[text.len()] = text.chars().count();
                let glyphs = shaped(face, text, budget)?;
                let font = face.font();
                let bitmaps = Bitmaps::read(&font).map_err(|error| shape_error(&error))?;
                let refused = Colr::of_face(face, |colr| {
                    let painted = Painted {
                        colr,
                        bitmaps: &bitmaps,
                    };
                    refused_canvas_glyph(face, text, &glyphs, painted, budget)
                })
                .map_err(|error| shape_error(&error))?;
                if let Some(refused) = refused {
                    return Err(colour_refused(face, &refused));
                }
                Ok(glyphs
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
                            characters[glyph.cluster.start],
                            characters[glyph.cluster.end],
                        )
                    })
                    .collect())
            })
        })?
    }

    /// Returns how a drawing at `size` points paints each of `glyphs` of an
    /// embedded face in a Type 3 font: `None` for a glyph that a CIDFont
    /// draws. COLR paints what it can, then a strike holds the glyph at
    /// [`DRAWING_PPI`] when it can, and a face with no outlines draws in
    /// Type 3 fonts alone. With `operation`, as for [`_Font::encode`].
    #[pyo3(signature = (glyphs, size, *, operation=None))]
    fn colour_glyphs(
        &self,
        py: Python<'_>,
        glyphs: Vec<u32>,
        size: f64,
        operation: Option<PyRef<'_, OperationBudget>>,
    ) -> PyResult<Vec<Option<ColourGlyph>>> {
        crate::errors::boundary(|| {
            let Kind::Embedded(face) = &self.kind else {
                return Err(PyValueError::new_err("a Standard 14 font has no glyph IDs"));
            };
            let budget = match operation.as_deref() {
                Some(operation) => {
                    operation.ensure_active()?;
                    TextBudget::shared(operation.operation.clone())
                }
                None => TextBudget {
                    budget: Budget::default().share(),
                    start: None,
                },
            };
            let budget = &budget;
            let ppem = size * DRAWING_PPI / 72.0;
            py.detach(|| {
                let font = face.font();
                let bitmaps = Bitmaps::read(&font).map_err(|error| shape_error(&error))?;
                let measure = Measure::new(face);
                let mut drawn = BudgetVec::new(budget);
                drawn
                    .try_reserve_exact(glyphs.len())
                    .map_err(|e| limit_error(&e))?;
                let verdicts = Colr::of_face(face, |colr| {
                    let mut verdicts = BudgetVec::new(budget);
                    verdicts.try_reserve_exact(glyphs.len())?;
                    for &glyph in &glyphs {
                        let verdict = match colr {
                            Some(colr) => colr.check(GlyphId::new(glyph), budget)?,
                            None => None,
                        };
                        verdicts.try_push(verdict)?;
                    }
                    Ok(verdicts)
                })
                .map_err(|e| limit_error(&e))?;
                for (&glyph, &verdict) in glyphs.iter().zip(&verdicts) {
                    budget.poll().map_err(|e| limit_error(&e))?;
                    let id = GlyphId::new(glyph);
                    let advance = measure.advance(id);
                    let colour = if verdict == Some(Verdict::Paints) {
                        Some((format!("g{glyph}c"), advance, None))
                    } else {
                        match bitmaps.best(id, ppem) {
                            Some(found) => {
                                let ppem = found.ppem;
                                Some((format!("g{glyph}s{ppem}"), advance, Some(ppem)))
                            }
                            None if face.outlines => None,
                            None => Some((format!("g{glyph}"), advance, Some(0))),
                        }
                    };
                    drawn.try_push(colour).map_err(|e| limit_error(&e))?;
                }
                Ok(drawn.into_vec())
            })
        })?
    }

    /// Returns the PNG of `glyph` in the strike of `ppem` that
    /// [`_Font::colour_glyphs`] named, and where its image lies in font units:
    /// `x`, `y`, the width, and the height.
    fn bitmap<'py>(
        &self,
        py: Python<'py>,
        glyph: u32,
        ppem: u16,
    ) -> PyResult<(Bound<'py, PyBytes>, f64, f64, f64, f64)> {
        crate::errors::boundary(|| {
            let Kind::Embedded(face) = &self.kind else {
                return Err(PyValueError::new_err("a Standard 14 font has no glyph IDs"));
            };
            let font = face.font();
            let found = Bitmaps::read(&font)
                .map_err(|error| shape_error(&error))?
                .best(GlyphId::new(glyph), f64::from(ppem))
                .ok_or_else(|| PdfError::new_err(format!("no strike holds the glyph {glyph}")))?;
            let [x, y, width, height] = found.rect;
            Ok((PyBytes::new(py, found.png), x, y, width, height))
        })?
    }

    /// Returns the advances of `glyphs` of an embedded face in thousandths
    /// of the em, as a CIDFont's `W` array records them.
    fn glyph_widths(&self, glyphs: Vec<u32>) -> PyResult<Vec<f64>> {
        crate::errors::boundary(|| {
            let Kind::Embedded(face) = &self.kind else {
                return Err(PyValueError::new_err("a Standard 14 font has no glyph IDs"));
            };
            let measure = Measure::new(face);
            let em = self.em();
            Ok(glyphs
                .into_iter()
                .map(|glyph| measure.advance(GlyphId::new(glyph)) * 1000.0 / em)
                .collect())
        })?
    }

    /// Returns the distance between baselines at `size`: `line_height` em,
    /// by default the font's own line height.
    #[pyo3(signature = (size, line_height=None))]
    fn leading(&self, size: f64, line_height: Option<f64>) -> PyResult<f64> {
        crate::errors::boundary(|| {
            self.line_metrics()
                .leading(size, line_height)
                .ok_or_else(|| PyValueError::new_err(LayoutError::InvalidGeometry.to_string()))
        })?
    }

    /// Returns the WinAnsi bytes that draw `text` in a Standard 14 font.
    ///
    /// With `operation`, a canvas's, the call charges the operation and
    /// observes its limits, deadline and cancellation.
    #[pyo3(signature = (text, max_text_size=Some(1_048_576), *, operation=None))]
    fn encode<'py>(
        &self,
        py: Python<'py>,
        text: &Bound<'_, PyString>,
        max_text_size: Option<u64>,
        operation: Option<PyRef<'_, OperationBudget>>,
    ) -> PyResult<Bound<'py, PyBytes>> {
        crate::errors::boundary(|| {
            let Kind::Standard(font) = &self.kind else {
                return Err(PyValueError::new_err(
                    "an embedded face is not encoded in WinAnsi",
                ));
            };
            let font = *font;
            let (budget, text) = text_budget(
                text,
                operation.as_deref(),
                &[(LimitCode::TextInputSize, max_text_size)],
            )?;
            let mut bytes = BudgetVec::new(&budget);
            for character in text.chars() {
                if standard::glyph_code(font, character).is_none() {
                    return Err(missing_glyph(font, u32::from(character)));
                }
                let byte = standard::winansi_byte(character)
                    .ok_or_else(|| PdfError::new_err(LayoutError::NotWinAnsi.to_string()))?;
                bytes.try_push(byte).map_err(|error| limit_error(&error))?;
            }
            let base_size = py
                .get_type::<PyBytes>()
                .getattr("__basicsize__")?
                .extract::<u64>()?;
            let output_size = u64::try_from(bytes.len())
                .unwrap_or(u64::MAX)
                .saturating_add(base_size)
                .saturating_add(1);
            budget
                .charge(LimitCode::MemorySize, output_size)
                .map_err(|error| limit_error(&error))?;
            PyBytes::new_with(py, bytes.len(), |output| {
                output.copy_from_slice(&bytes);
                Ok(())
            })
        })?
    }

    /// Lays out `text` in a box `width` by `height`, its lines placed as
    /// canvas text places them, and returns the lines with their widths
    /// and whether each justifies, the height that they need, the spare
    /// height, the first baseline's depth in em, and the leading. At most
    /// `max_lines` lines, or any number for `None`. With `operation`, as for
    /// [`_Font::encode`].
    #[pyo3(signature = (text, width, height, size, line_height, justify, max_lines, max_text_size=Some(1_048_576), normalize=false, *, operation=None))]
    #[expect(clippy::too_many_arguments, reason = "the options of a text box")]
    fn layout(
        &self,
        py: Python<'_>,
        text: &Bound<'_, PyString>,
        width: f64,
        height: f64,
        size: f64,
        line_height: Option<f64>,
        justify: bool,
        max_lines: Option<u64>,
        max_text_size: Option<u64>,
        normalize: bool,
        operation: Option<PyRef<'_, OperationBudget>>,
    ) -> PyResult<Py<PyTuple>> {
        crate::errors::boundary(|| {
            let (budget, text) = text_budget(
                text,
                operation.as_deref(),
                &[
                    (LimitCode::TextInputSize, max_text_size),
                    (LimitCode::TextLineCount, max_lines),
                ],
            )?;
            let placement = Placement::lines(self.line_metrics(), line_height);
            let em = self.em();
            py.detach(|| {
                let normalized = if normalize {
                    let mut bytes = BudgetVec::new(&budget);
                    let mut chars = text.chars().peekable();
                    while let Some(char) = chars.next() {
                        let char = match char {
                            '\r' => {
                                if chars.peek() == Some(&'\n') {
                                    chars.next();
                                }
                                '\n'
                            }
                            '\t' => ' ',
                            char => char,
                        };
                        let mut encoded = [0; 4];
                        bytes
                            .try_extend_from_slice(char.encode_utf8(&mut encoded).as_bytes())
                            .map_err(|error| limit_error(&error))?;
                    }
                    Some(String::from_utf8(bytes.into_vec()).expect("normalized text is UTF-8"))
                } else {
                    None
                };
                let text = normalized.as_deref().unwrap_or(text);
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
                let mut lines = BudgetVec::new(&budget);
                for line in &layout.lines {
                    let mut bytes = BudgetVec::new(&budget);
                    bytes
                        .try_extend_from_slice(line.text.as_bytes())
                        .map_err(|error| limit_error(&error))?;
                    // A line is a slice of the valid UTF-8 input.
                    let line_text = String::from_utf8(bytes.into_vec())
                        .expect("a layout line contains valid UTF-8");
                    lines
                        .try_push((line_text, line.width, line.justify))
                        .map_err(|error| limit_error(&error))?;
                }
                Python::attach(|py| {
                    let count = u64::try_from(lines.len()).unwrap_or(u64::MAX);
                    let tuple_size = py
                        .get_type::<PyTuple>()
                        .getattr("__basicsize__")?
                        .extract::<u64>()?;
                    let list_size = py
                        .get_type::<PyList>()
                        .getattr("__basicsize__")?
                        .extract::<u64>()?;
                    let string_size = py
                        .get_type::<PyString>()
                        .getattr("__basicsize__")?
                        .extract::<u64>()?;
                    let float_size = py
                        .get_type::<PyFloat>()
                        .getattr("__basicsize__")?
                        .extract::<u64>()?;
                    let slot = u64::try_from(std::mem::size_of::<usize>()).unwrap_or(u64::MAX);
                    let mut output_size = list_size
                        .saturating_add(count.saturating_mul(slot))
                        .saturating_add(tuple_size)
                        .saturating_add(5u64.saturating_mul(slot))
                        .saturating_add(count.saturating_add(4).saturating_mul(float_size));
                    for (text, _, _) in &lines {
                        let len = u64::try_from(text.len()).unwrap_or(u64::MAX);
                        output_size = output_size
                            .saturating_add(tuple_size)
                            .saturating_add(3u64.saturating_mul(slot))
                            .saturating_add(string_size)
                            .saturating_add(len.saturating_add(1).saturating_mul(4));
                    }
                    budget
                        .charge(LimitCode::MemorySize, output_size)
                        .map_err(|error| limit_error(&error))?;
                    let result = (
                        lines.into_vec(),
                        layout.height,
                        layout.spare_height,
                        layout.ascent,
                        layout.leading,
                    );
                    Ok(result.into_pyobject(py)?.unbind())
                })
            })
        })?
    }

    fn __repr__(&self) -> PyResult<String> {
        crate::errors::boundary(|| format!("_Font({:?})", self.full_name()))
    }
}

/// A font file of an installed font package, which rendering reads the
/// first time that a page needs it, at most `max_size` bytes; one object
/// serves every document that renders with it, so the file is read once.
#[pyclass(frozen, module = "pyropdf._core")]
pub struct _FontFile(Arc<FontFile>);

#[pymethods]
impl _FontFile {
    #[new]
    fn new(path: PathBuf, max_size: Option<u64>) -> PyResult<Self> {
        crate::errors::boundary(|| Self(Arc::new(FontFile::new(path, max_size))))
    }
}

/// A face that renders the CJK fonts of one language that a document does
/// not embed: a loaded OpenType face or an installed font file.
#[derive(FromPyObject)]
pub enum FallbackFace<'py> {
    Font(PyRef<'py, _Font>),
    File(PyRef<'py, _FontFile>),
}

/// Sets the faces that render the CJK fonts that `document` does not
/// embed, replacing all of them: each of `faces` holds a language tag,
/// `ja`, `ko`, `zh-CN`, or `zh-TW`, whether the face is the serif one, and
/// the face.
#[pyfunction]
pub fn set_fallback_faces(
    document: &_Document,
    faces: Vec<(String, bool, FallbackFace<'_>)>,
) -> PyResult<()> {
    crate::errors::boundary(|| {
        let mut fallback = Fallback::default();
        for (tag, serif, face) in faces {
            let language = Language::from_tag(&tag).ok_or_else(|| {
                PyValueError::new_err(format!(
                    "a fallback font language is 'ja', 'ko', 'zh-CN', or 'zh-TW': {tag:?}"
                ))
            })?;
            let source = match face {
                FallbackFace::Font(font) => match &font.kind {
                    Kind::Embedded(face) => Source::Face(Arc::clone(face)),
                    Kind::Standard(_) => {
                        return Err(PdfError::new_err(
                            "a Standard 14 font cannot render CJK text",
                        ));
                    }
                },
                FallbackFace::File(file) => Source::File(Arc::clone(&file.0)),
            };
            fallback.set(language, serif, Some(source));
        }
        document.set_fallback(fallback);
        Ok(())
    })?
}

/// Bounds a text operation before input-derived collections grow.
fn text_budget<'a>(
    text: &'a Bound<'_, PyString>,
    operation: Option<&OperationBudget>,
    limits: &[(LimitCode, Option<u64>)],
) -> PyResult<(TextBudget, &'a str)> {
    let budget = match operation {
        Some(operation) => {
            operation.ensure_active()?;
            let mut budget = operation.operation.clone();
            for &(code, limit) in limits {
                if let Some(limit) = limit {
                    budget = budget
                        .tightened(code, limit)
                        .map_err(|error| limit_error(&error))?;
                }
            }
            TextBudget::shared(budget)
        }
        None => TextBudget {
            budget: limits
                .iter()
                .fold(Budget::default(), |budget, &(code, limit)| {
                    budget.with_limit(code, limit)
                })
                .share(),
            start: None,
        },
    };
    // Call str's own length method: a subclass may override __len__.
    let chars = text
        .py()
        .get_type::<PyString>()
        .getattr("__len__")?
        .call1((text,))?
        .extract::<u64>()?;
    budget
        .check(LimitCode::TextInputSize, chars)
        .map_err(|error| limit_error(&error))?;
    // CPython may create a UTF-8 cache while converting a non-ASCII string.
    budget
        .charge(
            LimitCode::MemorySize,
            chars.saturating_mul(4).saturating_add(1),
        )
        .map_err(|error| limit_error(&error))?;
    let text = text.to_str()?;
    let len = u64::try_from(text.len()).unwrap_or(u64::MAX);
    budget
        .check(LimitCode::TextInputSize, len)
        .map_err(|error| limit_error(&error))?;
    Ok((budget, text))
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

/// Refuses a character whose glyph is a colour glyph with no outline, or whose
/// COLR paints do not read, naming it and the paint that stands in the way.
fn colour_refused(face: &Face, refused: &Refused) -> PyErr {
    let Refused {
        character,
        glyph,
        verdict,
    } = *refused;
    let name = &face.full_name;
    let code = u32::from(character);
    let no_outline = format!(
        "{name} cannot draw U+{code:04X} ({character:?}): its glyph is a colour glyph with no outline"
    );
    PdfError::new_err(match verdict {
        Some(Verdict::Damaged(reason)) => format!(
            "{name} cannot draw U+{code:04X} ({character:?}): its colour glyph {glyph} does not paint: {reason}"
        ),
        Some(Verdict::Cannot(paint)) => {
            format!(
                "{no_outline}, and colour glyph {glyph} needs {paint}, which the canvas does not paint"
            )
        }
        Some(Verdict::Paints) | None => no_outline,
    })
}

/// Maps a refused COLR glyph, naming it, or what the budget refused.
fn colr_error(face: &Face, glyph: u32, error: &ColrError) -> PyErr {
    match error {
        ColrError::Store(error) => store_error(error),
        _ => PdfError::new_err(format!(
            "{} cannot draw its colour glyph {glyph}: {error}",
            face.full_name,
        )),
    }
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

/// The budget of one text call: its own, or a view of a canvas operation.
///
/// A canvas charges the bytes that it keeps of a call's result itself, so
/// a call returns to the operation, when it ends, the memory that it
/// charged and did not release.
struct TextBudget {
    budget: SharedBudget,
    /// The operation's memory in use when the call began.
    start: Option<u64>,
}

impl TextBudget {
    fn shared(budget: SharedBudget) -> Self {
        let start = Some(budget.used(LimitCode::MemorySize));
        Self { budget, start }
    }
}

impl Deref for TextBudget {
    type Target = Budget;

    fn deref(&self) -> &Budget {
        &self.budget
    }
}

impl Drop for TextBudget {
    fn drop(&mut self) {
        if let Some(start) = self.start {
            let used = self.budget.used(LimitCode::MemorySize);
            self.budget
                .release(LimitCode::MemorySize, used.saturating_sub(start));
        }
    }
}
