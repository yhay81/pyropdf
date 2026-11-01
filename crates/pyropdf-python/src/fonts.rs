//! The font programs of `python/pyropdf/_fonts.py`: the glyphs that the
//! pages draw with each font, as hayro resolves them, and the subset of a
//! program to those glyphs.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyFloat, PyList, PyString, PyTuple};
use pyropdf_engine::extract::{self, Error};
use pyropdf_engine::fonts::{self, Format};
use pyropdf_engine::standard::{self, Base14Font, LayoutError, Placement};
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};

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
        extract::used_glyphs(&bridged, &version, &index, &fonts, &budget).map_err(|e| match e {
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
    // An unknown glyph use means the program must stay whole, even when a
    // caller invokes this internal helper directly instead of subset_fonts.
    if keep.contains(&u32::MAX) {
        return Ok(None);
    }
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
    #[pyo3(signature = (text, size, max_text_size=Some(1_048_576)))]
    fn char_widths<'py>(
        &self,
        py: Python<'py>,
        text: &Bound<'_, PyString>,
        size: f64,
        max_text_size: Option<u64>,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let (budget, text) = text_budget(text, max_text_size)?;
        let mut widths = BudgetVec::new(&budget);
        for character in text.chars() {
            let width = f64::from(self.metric(u32::from(character))?.width_x) * size / 1000.0;
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
        let output_size =
            tuple_size.saturating_add(count.saturating_mul(float_size.saturating_add(slot_size)));
        budget
            .charge(LimitCode::MemorySize, output_size)
            .map_err(|error| limit_error(&error))?;
        PyTuple::new(py, widths.iter().copied())
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
    #[pyo3(signature = (text, size, max_text_size=Some(1_048_576)))]
    fn text_width(
        &self,
        text: &Bound<'_, PyString>,
        size: f64,
        max_text_size: Option<u64>,
    ) -> PyResult<f64> {
        let (_budget, text) = text_budget(text, max_text_size)?;
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
    #[pyo3(signature = (text, max_text_size=Some(1_048_576)))]
    fn encode<'py>(
        &self,
        py: Python<'py>,
        text: &Bound<'_, PyString>,
        max_text_size: Option<u64>,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let (budget, text) = text_budget(text, max_text_size)?;
        let mut bytes = BudgetVec::new(&budget);
        for character in text.chars() {
            if standard::glyph_code(self.font, character).is_none() {
                return Err(missing_glyph(self.font, u32::from(character)));
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
    }

    /// Lays out `text` in a box `width` by `height`, its lines placed as
    /// canvas text places them, and returns the lines with their widths
    /// and whether each justifies, the height that they need, the spare
    /// height, the first baseline's depth in em, and the leading. At most
    /// `max_lines` lines, or any number for `None`.
    #[pyo3(signature = (text, width, height, size, line_height, justify, max_lines, max_text_size=Some(1_048_576), normalize=false))]
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
    ) -> PyResult<Py<PyTuple>> {
        let (budget, text) = text_budget(text, max_text_size)?;
        let budget = budget.with_limit(LimitCode::TextLineCount, max_lines);
        let font = self.font;
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
    }

    fn __repr__(&self) -> String {
        format!("_Font({:?})", self.name())
    }
}

/// Bounds a text operation before input-derived collections grow.
fn text_budget<'a>(
    text: &'a Bound<'_, PyString>,
    max_text_size: Option<u64>,
) -> PyResult<(Budget, &'a str)> {
    let budget = Budget::default().with_limit(LimitCode::TextInputSize, max_text_size);
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
