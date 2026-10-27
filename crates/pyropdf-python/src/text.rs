//! `page.text`, `doc.text`, `page.words`, and `page.layout`: the text of
//! pages in reading order, ported from pylopdf's `extract_text` and
//! `extract_layout` in `rust/src/document/extraction.rs` at
//! 036be111cafe77755db4bb2be17bc70a8f2f9d82.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyropdf_engine::extract::{self, BBox, BlockTuple, Error, TextOptions, TextPeak, Unmapped};
use pyropdf_store::budget::{Budget, LimitCode};
use pyropdf_store::value::Reference;

use crate::document::{_Document, PageKey, PageKind, PageResult};
use crate::errors::{PdfError, StaleObjectError, limit_error};
use crate::pixmap::bridge_error;

/// The text options as `python/pyropdf/_text.py` passes them: `(clip, keep
/// clipped glyphs, unmapped code, expand ligatures, normalize whitespace,
/// dehyphenate, exact boxes, styles)`, with the unmapped codes 0 to skip, 1
/// for U+FFFD, and 2 for the glyph ID.
type OptionsTuple = (Option<BBox>, bool, u8, bool, bool, bool, bool, bool);

/// The text options as a memo key, with the clip's coordinates as bits.
pub(crate) type TextKey = (Option<[u64; 4]>, bool, u8, bool, bool, bool, bool, bool);

fn text_key(options: OptionsTuple) -> TextKey {
    let (clip, keep_clipped, unmapped, ligatures, whitespace, dehyphenate, exact, styles) = options;
    (
        clip.map(box_bits),
        keep_clipped,
        unmapped,
        ligatures,
        whitespace,
        dehyphenate,
        exact,
        styles,
    )
}

/// Returns the bits of a box's coordinates, which a memo key compares.
pub(crate) fn box_bits((x0, y0, x1, y1): BBox) -> [u64; 4] {
    [x0.to_bits(), y0.to_bits(), x1.to_bits(), y1.to_bits()]
}

/// Whether a result whose reading held `peak` fits the rest of `budget`.
fn text_fits(budget: &Budget, peak: TextPeak) -> bool {
    budget
        .remaining(LimitCode::TextGlyphCount)
        .is_none_or(|remaining| peak.glyphs <= remaining)
        && budget
            .remaining(LimitCode::TextSize)
            .is_none_or(|remaining| peak.size <= remaining)
}

/// Refuses a result of another kind than its key's, which the memo never
/// holds.
fn mismatch() -> PyErr {
    PdfError::new_err("the memo holds another kind of result for this page")
}

fn stale(number: u32, generation: u16) -> PyErr {
    StaleObjectError::new_err(format!(
        "page object {number} {generation} is no longer a page of the document"
    ))
}

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
/// `max_size` the UTF-8 with `text_size`; `None` lifts either. The text is
/// memoized with what interpreting the page may read.
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
    let key = text_key(options);
    let options = text_options(options)?;
    let (version, index, budget) = crate::pages::index(document)?;
    let budget = budget
        .with_limit(LimitCode::TextGlyphCount, max_glyphs)
        .with_limit(LimitCode::TextSize, max_size);
    py.detach(|| {
        let reference = Reference::new(number, generation);
        let (at, page) = index
            .index_of(reference)
            .and_then(|at| Some((at, index.get(at)?)))
            .ok_or_else(|| stale(number, generation))?;
        let key = PageKey {
            page: reference,
            kind: PageKind::Text(key),
        };
        let result = document.page_result(
            &key,
            &version,
            page,
            &budget,
            |result| matches!(result, &PageResult::Text { peak, .. } if text_fits(&budget, peak)),
            || {
                let bridged = document
                    .bridge
                    .get(&version, &index, &budget)
                    .map_err(|e| bridge_error(&e))?;
                let mut text = String::new();
                let (skipped, peak) =
                    extract::page_text(&bridged, at, &options, &budget, &mut text)
                        .map_err(text_error)?;
                Ok(PageResult::Text {
                    text,
                    skipped,
                    peak,
                })
            },
        )?;
        match &*result {
            PageResult::Text { text, skipped, .. } => Ok((text.clone(), *skipped)),
            _ => Err(mismatch()),
        }
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
            let (page, _) = extract::page_text(&bridged, at, &options, &budget, &mut text)
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
/// The layout is memoized with what interpreting the page may read.
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
    let key = text_key(options);
    let options = text_options(options)?;
    let (version, index, budget) = crate::pages::index(document)?;
    let budget = budget
        .with_limit(LimitCode::TextGlyphCount, max_glyphs)
        .with_limit(LimitCode::TextSize, max_size);
    py.detach(|| {
        let reference = Reference::new(number, generation);
        let (at, page) = index
            .index_of(reference)
            .and_then(|at| Some((at, index.get(at)?)))
            .ok_or_else(|| stale(number, generation))?;
        let key = PageKey {
            page: reference,
            kind: PageKind::Layout(key, chars),
        };
        let result = document.page_result(
            &key,
            &version,
            page,
            &budget,
            |result| matches!(result, PageResult::Layout(layout) if text_fits(&budget, layout.peak)),
            || {
                let bridged = document
                    .bridge
                    .get(&version, &index, &budget)
                    .map_err(|e| bridge_error(&e))?;
                extract::page_layout(&bridged, at, &options, chars, &budget)
                    .map(PageResult::Layout)
                    .map_err(text_error)
            },
        )?;
        match &*result {
            PageResult::Layout(layout) => Ok((
                layout.width,
                layout.height,
                layout.blocks.clone(),
                layout.skipped,
            )),
            _ => Err(mismatch()),
        }
    })
}
