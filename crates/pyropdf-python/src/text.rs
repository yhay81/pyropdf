//! `page.text`, `doc.text`, `page.words`, and `page.layout`: the text of
//! pages in reading order, ported from pylopdf's `extract_text` and
//! `extract_layout` in `rust/src/document/extraction.rs` at
//! 036be111cafe77755db4bb2be17bc70a8f2f9d82.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyropdf_engine::extract::{self, BBox, BlockTuple, Error, TextOptions, Unmapped};
use pyropdf_store::budget::LimitCode;
use pyropdf_store::value::Reference;

use crate::document::_Document;
use crate::errors::{PdfError, StaleObjectError, limit_error};
use crate::pixmap::bridge_error;

/// The text options as `python/pyropdf/_text.py` passes them: `(clip, keep
/// clipped glyphs, unmapped code, expand ligatures, normalize whitespace,
/// dehyphenate, exact boxes, styles)`, with the unmapped codes 0 to skip, 1
/// for U+FFFD, and 2 for the glyph ID.
type OptionsTuple = (Option<BBox>, bool, u8, bool, bool, bool, bool, bool);

fn text_options(options: OptionsTuple) -> PyResult<TextOptions> {
    let (
        clip,
        keep_clipped,
        unmapped,
        expand_ligatures,
        normalize_whitespace,
        dehyphenate,
        exact_bboxes,
        styles,
    ) = options;
    let unmapped = match unmapped {
        0 => Unmapped::Skip,
        1 => Unmapped::Replacement,
        2 => Unmapped::GlyphId,
        other => {
            return Err(PyValueError::new_err(format!(
                "unknown unmapped code {other}"
            )));
        }
    };
    Ok(TextOptions {
        clip,
        keep_clipped,
        unmapped,
        expand_ligatures,
        normalize_whitespace,
        dehyphenate,
        exact_bboxes,
        styles,
    })
}

fn text_error(error: Error) -> PyErr {
    match error {
        Error::Limit(refusal) => limit_error(&refusal),
        Error::NoPage(_) | Error::Allocation(_) => PdfError::new_err(error.to_string()),
    }
}

/// Returns the text of the page whose object is `number` and `generation`,
/// and the number of glyphs without Unicode that it left out, with the GIL
/// released. `max_glyphs` bounds the glyphs with `text_glyph_count` and
/// `max_size` the UTF-8 with `text_size`; `None` lifts either.
#[pyfunction]
pub fn page_text(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    options: OptionsTuple,
    max_glyphs: Option<u64>,
    max_size: Option<u64>,
) -> PyResult<(String, usize)> {
    let options = text_options(options)?;
    let (version, index, budget) = crate::pages::index(document)?;
    let budget = budget
        .with_limit(LimitCode::TextGlyphCount, max_glyphs)
        .with_limit(LimitCode::TextSize, max_size);
    py.detach(|| {
        let at = index
            .index_of(Reference::new(number, generation))
            .ok_or_else(|| {
                StaleObjectError::new_err(format!(
                    "page object {number} {generation} is no longer a page of the document"
                ))
            })?;
        let bridged = document
            .bridge
            .get(&version, &index, &budget)
            .map_err(|e| bridge_error(&e))?;
        let mut text = String::new();
        let skipped =
            extract::page_text(&bridged, at, &options, &budget, &mut text).map_err(text_error)?;
        Ok((text, skipped))
    })
}

/// Returns the text of `pages`, every page for `None`, joined in their
/// order from one version, and the glyphs without Unicode that it left out.
/// `max_glyphs` bounds each page, and `max_size` the whole text.
#[pyfunction]
pub fn document_text(
    py: Python<'_>,
    document: &_Document,
    pages: Option<Vec<i64>>,
    options: OptionsTuple,
    max_glyphs: Option<u64>,
    max_size: Option<u64>,
) -> PyResult<(String, usize)> {
    let options = text_options(options)?;
    let (version, index, budget) = crate::pages::index(document)?;
    let indexes = crate::pages::entries(pages, index.len())?;
    let budget = budget
        .with_limit(LimitCode::TextGlyphCount, max_glyphs)
        .with_limit(LimitCode::TextSize, max_size);
    py.detach(|| {
        let mut text = String::new();
        let mut skipped = 0usize;
        if indexes.is_empty() {
            return Ok((text, skipped));
        }
        let bridged = document
            .bridge
            .get(&version, &index, &budget)
            .map_err(|e| bridge_error(&e))?;
        for at in indexes {
            let page = extract::page_text(&bridged, at, &options, &budget, &mut text)
                .map_err(text_error)?;
            skipped = skipped.saturating_add(page);
        }
        Ok((text, skipped))
    })
}

/// Returns the layout of the page whose object is `number` and
/// `generation`: `(width, height, blocks, skipped)`, each span with its
/// characters when `chars` is set, with the GIL released. The budgets are
/// those of [`page_text`], and `max_size` also bounds the layout's strings.
#[pyfunction]
#[expect(
    clippy::too_many_arguments,
    reason = "the page, its options, and the two budgets of one call"
)]
pub fn page_layout(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    options: OptionsTuple,
    chars: bool,
    max_glyphs: Option<u64>,
    max_size: Option<u64>,
) -> PyResult<(f64, f64, Vec<BlockTuple>, usize)> {
    let options = text_options(options)?;
    let (version, index, budget) = crate::pages::index(document)?;
    let budget = budget
        .with_limit(LimitCode::TextGlyphCount, max_glyphs)
        .with_limit(LimitCode::TextSize, max_size);
    py.detach(|| {
        let at = index
            .index_of(Reference::new(number, generation))
            .ok_or_else(|| {
                StaleObjectError::new_err(format!(
                    "page object {number} {generation} is no longer a page of the document"
                ))
            })?;
        let bridged = document
            .bridge
            .get(&version, &index, &budget)
            .map_err(|e| bridge_error(&e))?;
        let layout =
            extract::page_layout(&bridged, at, &options, chars, &budget).map_err(text_error)?;
        Ok((layout.width, layout.height, layout.blocks, layout.skipped))
    })
}
