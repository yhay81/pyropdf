//! The font programs of `python/pyropdf/_fonts.py`: the glyphs that the
//! pages draw with each font, as hayro resolves them, and the subset of a
//! program to those glyphs.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyropdf_engine::extract::{self, Error};
use pyropdf_engine::fonts::{self, Format};
use pyropdf_engine::standard::{self, Base14Font, LayoutError, Placement};
use pyropdf_store::budget::{Budget, LimitCode};

use crate::document::_Document;
use crate::errors::{LimitError, PdfError, limit_error};
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

/// The face of `python/pyropdf/_fonts.py`'s `Font`: a Standard 14 font,
/// measured with Adobe's metrics and drawn in WinAnsi (DESIGN.md section 7).
#[pyclass(frozen, module = "pyropdf._core")]
pub struct _Font {
    font: Base14Font,
}

impl _Font {
    fn metric(&self, codepoint: u32) -> PyResult<&'static standard::CharacterMetric<'static>> {
        char::from_u32(codepoint)
            .and_then(|character| standard::char_metric(self.font, character))
            .ok_or_else(|| missing_glyph(self.font, codepoint))
    }

    fn name(&self) -> &'static str {
        self.font.pdf_base_name()
    }
}

#[pymethods]
impl _Font {
    /// Returns the Standard 14 font of a PostScript name.
    #[staticmethod]
    fn standard(name: &str) -> PyResult<Self> {
        standard::font(name)
            .map(|font| Self { font })
            .ok_or_else(|| PyValueError::new_err(format!("unknown Standard 14 font: {name:?}")))
    }

    /// The PostScript name of a Standard 14 font.
    #[getter]
    fn standard_name(&self) -> &'static str {
        self.name()
    }

    #[getter(name)]
    fn full_name(&self) -> &'static str {
        self.name()
    }

    #[getter]
    fn family(&self) -> &'static str {
        let name = self.name();
        name.split('-').next().unwrap_or(name)
    }

    #[getter]
    fn bold(&self) -> bool {
        self.name().contains("Bold")
    }

    #[getter]
    fn italic(&self) -> bool {
        let name = self.name();
        name.contains("Italic") || name.contains("Oblique")
    }

    #[getter]
    fn serif(&self) -> bool {
        self.name().starts_with("Times")
    }

    #[getter]
    fn weight(&self) -> u16 {
        if self.bold() { 700 } else { 400 }
    }

    #[getter]
    fn units_per_em(&self) -> u16 {
        1000
    }

    #[getter]
    fn ascender(&self) -> f64 {
        standard::line_metrics(self.font).ascender
    }

    #[getter]
    fn descender(&self) -> f64 {
        standard::line_metrics(self.font).descender
    }

    #[getter]
    fn bbox(&self) -> (f64, f64, f64, f64) {
        let [x0, y0, x1, y1] = standard::font_bbox(self.font);
        (x0, y0, x1, y1)
    }

    #[getter]
    fn glyph_count(&self) -> usize {
        self.font.metrics().character_metrics.len()
    }

    #[getter]
    fn monospaced(&self) -> bool {
        self.font.metrics().is_fixed_pitch
    }

    fn chars(&self) -> String {
        standard::chars(self.font).collect()
    }

    fn has_glyph(&self, codepoint: u32) -> bool {
        char::from_u32(codepoint)
            .is_some_and(|character| standard::glyph_code(self.font, character).is_some())
    }

    /// Returns the advance of each character drawn alone, at `size`.
    fn char_widths(&self, text: &str, size: f64) -> PyResult<Vec<f64>> {
        text.chars()
            .map(|character| {
                Ok(f64::from(self.metric(u32::from(character))?.width_x) * size / 1000.0)
            })
            .collect()
    }

    /// Returns the AFM ink box of a character's glyph in fractions of the em.
    fn char_bbox(&self, codepoint: u32) -> PyResult<(f64, f64, f64, f64)> {
        let metric = self.metric(codepoint)?;
        Ok(metric.bbox.as_ref().map_or((0.0, 0.0, 0.0, 0.0), |bbox| {
            (
                f64::from(bbox.llx) / 1000.0,
                f64::from(bbox.lly) / 1000.0,
                f64::from(bbox.urx) / 1000.0,
                f64::from(bbox.ury) / 1000.0,
            )
        }))
    }

    /// Returns the width of one line of `text` at `size`.
    fn text_width(&self, text: &str, size: f64) -> PyResult<f64> {
        if let Some(character) = text
            .chars()
            .find(|character| standard::glyph_code(self.font, *character).is_none())
        {
            return Err(missing_glyph(self.font, u32::from(character)));
        }
        standard::text_width(text, self.font, size)
            .ok_or_else(|| PdfError::new_err(LayoutError::NotWinAnsi.to_string()))
    }

    /// Returns the distance between baselines at `size`: `line_height` em,
    /// by default the font's own line height.
    #[pyo3(signature = (size, line_height=None))]
    fn leading(&self, size: f64, line_height: Option<f64>) -> PyResult<f64> {
        standard::line_metrics(self.font)
            .leading(size, line_height)
            .ok_or_else(|| PyValueError::new_err(LayoutError::InvalidGeometry.to_string()))
    }

    /// Returns the WinAnsi bytes that draw `text`.
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
        let font = self.font;
        py.detach(|| {
            let budget = Budget::default().with_limit(LimitCode::TextLineCount, max_lines);
            let placement = Placement::lines(standard::line_metrics(font), line_height);
            let layout = standard::textbox_layout(
                text,
                (width, height),
                font,
                size,
                placement,
                justify,
                &budget,
            )
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
        format!("_Font({:?})", self.name())
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
