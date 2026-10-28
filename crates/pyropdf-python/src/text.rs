//! `page.text`, `doc.text`, `page.words`, `page.layout`, `page.chars`,
//! `page.search`, and `page.selection`: the text of pages in reading order,
//! ported from pylopdf's `extract_text`, `extract_layout`, `search_page`,
//! and `select_text` in `rust/src/document/extraction.rs` at
//! 036be111cafe77755db4bb2be17bc70a8f2f9d82.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyropdf_engine::content::PageContent;
use pyropdf_engine::extract::{
    self, BBox, BlockTuple, Error, PageLayout, QuadTuple, SearchHits, TableTuple, TextOptions,
    TextPeak, Unmapped,
};
use pyropdf_store::ErrorKind;
use pyropdf_store::budget::{Budget, LimitCode};
use pyropdf_store::memo::Reads;
use pyropdf_store::value::Reference;

use crate::buffers::CharColumns;
use crate::document::{_Document, PageKey, PageKind, PageResult};
use crate::errors::{PdfError, StaleObjectError, limit_error, store_error};
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

/// The layout of a page as `page_layout` returns it: `(width, height,
/// blocks, skipped, columns)`, with the columns of the chars when the layout
/// has them.
type LayoutTuple = (f64, f64, Vec<BlockTuple>, usize, Option<CharColumns>);

/// Returns the layout of the page whose object is `number` and
/// `generation`, each span with its characters when `chars` is set, with
/// the GIL released, and the columns of those characters. The budgets are
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
) -> PyResult<LayoutTuple> {
    let limits = (max_glyphs, max_size);
    with_layout(
        py,
        document,
        (number, generation),
        options,
        chars,
        limits,
        |layout| {
            let columns = if chars {
                Some(CharColumns::new(&layout.blocks)?)
            } else {
                None
            };
            Ok((
                layout.width,
                layout.height,
                layout.blocks.clone(),
                layout.skipped,
                columns,
            ))
        },
    )
}

/// Returns the columns of the characters of the page whose object is
/// `number` and `generation`, in the order of [`page_layout`] with `chars`,
/// and the glyphs without Unicode that it left out, with the GIL released
/// and no Python object per character. It shares the memoized layout.
#[pyfunction]
pub fn page_chars(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    options: OptionsTuple,
    max_glyphs: Option<u64>,
    max_size: Option<u64>,
) -> PyResult<(CharColumns, usize)> {
    let limits = (max_glyphs, max_size);
    with_layout(
        py,
        document,
        (number, generation),
        options,
        true,
        limits,
        |layout| Ok((CharColumns::new(&layout.blocks)?, layout.skipped)),
    )
}

/// Calls `then` with the memoized layout of the page `(number,
/// generation)`, computing it with `options`, chars when `chars` is set, and
/// the budgets `(max_glyphs, max_size)` where the memo has none, with the
/// GIL released.
fn with_layout<T: Send>(
    py: Python<'_>,
    document: &_Document,
    (number, generation): (u32, u16),
    options: OptionsTuple,
    chars: bool,
    (max_glyphs, max_size): (Option<u64>, Option<u64>),
    then: impl FnOnce(&PageLayout) -> PyResult<T> + Send,
) -> PyResult<T> {
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
                // Malformed content may leave chars without sources, but a
                // budget refusal must stop the operation (F5 and F6).
                let content = if chars {
                    match PageContent::read(&mut Reads::new(&version, &budget), page) {
                        Ok(content) => Some(content),
                        Err(error) if matches!(error.kind(), ErrorKind::Limit(_)) => {
                            return Err(store_error(&error));
                        }
                        Err(_) => None,
                    }
                } else {
                    None
                };
                extract::page_layout(&bridged, at, &options, chars, content.as_ref(), &budget)
                    .map(PageResult::Layout)
                    .map_err(text_error)
            },
        )?;
        match &*result {
            PageResult::Layout(layout) => then(layout),
            _ => Err(mismatch()),
        }
    })
}

/// The matches of a search: a box per match, or a quad per line of each.
#[derive(IntoPyObject)]
pub enum Hits {
    Boxes(Vec<BBox>),
    Quads(Vec<QuadTuple>),
}

/// Returns the case-insensitive matches of `needle` on the page whose object
/// is `number` and `generation`, a box per match or with `quads` a quad per
/// line of each match, and the glyphs without Unicode that it left out, with
/// the GIL released. `max_hits` bounds the matches with `search_hit_count`,
/// and the other budgets are those of [`page_text`].
#[pyfunction]
#[expect(
    clippy::too_many_arguments,
    reason = "the page, the search, its options, and the three budgets of one call"
)]
pub fn page_search(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    needle: &str,
    quads: bool,
    options: OptionsTuple,
    max_hits: Option<u64>,
    max_glyphs: Option<u64>,
    max_size: Option<u64>,
) -> PyResult<(Hits, usize)> {
    let options = text_options(options)?;
    let (version, index, budget) = crate::pages::index(document)?;
    let budget = budget
        .with_limit(LimitCode::SearchHitCount, max_hits)
        .with_limit(LimitCode::TextGlyphCount, max_glyphs)
        .with_limit(LimitCode::TextSize, max_size);
    py.detach(|| {
        let reference = Reference::new(number, generation);
        let at = index
            .index_of(reference)
            .ok_or_else(|| stale(number, generation))?;
        let bridged = document
            .bridge
            .get(&version, &index, &budget)
            .map_err(|e| bridge_error(&e))?;
        let (hits, skipped) = extract::page_search(&bridged, at, &options, needle, quads, &budget)
            .map_err(text_error)?;
        let hits = match hits {
            SearchHits::Boxes(boxes) => Hits::Boxes(boxes),
            SearchHits::Quads(quads) => Hits::Quads(quads),
        };
        Ok((hits, skipped))
    })
}

/// Returns the text that a viewer selects by dragging from `start` to `end`
/// on the page whose object is `number` and `generation`, and the glyphs
/// without Unicode that it left out, with the GIL released. The points are
/// finite and in display space, and the budgets are those of [`page_text`].
#[pyfunction]
#[expect(
    clippy::too_many_arguments,
    reason = "the page, the two points, the options, and the two budgets of one call"
)]
pub fn page_selection(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    start: (f64, f64),
    end: (f64, f64),
    options: OptionsTuple,
    max_glyphs: Option<u64>,
    max_size: Option<u64>,
) -> PyResult<(String, usize)> {
    if ![start.0, start.1, end.0, end.1]
        .iter()
        .all(|value| value.is_finite())
    {
        return Err(PyValueError::new_err("selection points must be finite"));
    }
    let options = text_options(options)?;
    let (version, index, budget) = crate::pages::index(document)?;
    let budget = budget
        .with_limit(LimitCode::TextGlyphCount, max_glyphs)
        .with_limit(LimitCode::TextSize, max_size);
    py.detach(|| {
        let at = index
            .index_of(Reference::new(number, generation))
            .ok_or_else(|| stale(number, generation))?;
        let bridged = document
            .bridge
            .get(&version, &index, &budget)
            .map_err(|e| bridge_error(&e))?;
        extract::page_selection(&bridged, at, &options, start, end, &budget).map_err(text_error)
    })
}

/// Returns the tables on the page whose object is `number` and
/// `generation` whose whole box lies inside `clip`, from its vector rules or
/// with `text_strategy` from its aligned rows, and the glyphs without
/// Unicode that it left out, with the GIL released. The budgets are those of
/// [`page_text`].
#[pyfunction]
#[expect(
    clippy::too_many_arguments,
    reason = "the page, the strategy, the clip, and the two budgets of one call"
)]
pub fn page_tables(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    text_strategy: bool,
    clip: Option<BBox>,
    max_glyphs: Option<u64>,
    max_size: Option<u64>,
) -> PyResult<(Vec<TableTuple>, usize)> {
    let (version, index, budget) = crate::pages::index(document)?;
    let budget = budget
        .with_limit(LimitCode::TextGlyphCount, max_glyphs)
        .with_limit(LimitCode::TextSize, max_size);
    py.detach(|| {
        let at = index
            .index_of(Reference::new(number, generation))
            .ok_or_else(|| stale(number, generation))?;
        let bridged = document
            .bridge
            .get(&version, &index, &budget)
            .map_err(|e| bridge_error(&e))?;
        extract::page_tables(&bridged, at, text_strategy, clip, &budget).map_err(text_error)
    })
}
