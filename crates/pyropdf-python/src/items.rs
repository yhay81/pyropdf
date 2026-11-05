//! The content model for `python/pyropdf/_content.py`: what hayro paints on
//! a page, ported from pylopdf's `page_content` in
//! `rust/src/document/extraction.rs` at
//! 036be111cafe77755db4bb2be17bc70a8f2f9d82.
//!
//! The content crosses the boundary as `(items, fonts, clips, groups, marks,
//! states)`: each item is `(kind, tuple)`, with kind 0 for a text run, 1 for
//! a path, and 2 for an image, in the tuple forms of
//! `pyropdf_engine::extract`, which index the other lists. The paths alone
//! also cross as `crate::buffers::PathColumns`, which hands the memoized
//! content's columns to Python without a copy or a Python object per path
//! (#606, #615).

use pyo3::IntoPyObjectExt;
use pyo3::prelude::*;
use pyropdf_engine::extract::{self, BBox, ContentItem, Error, ItemKinds, PageContent};
use pyropdf_engine::render::Fallback;
use pyropdf_store::budget::LimitCode;
use pyropdf_store::value::Reference;

use crate::buffers::PathColumns;
use crate::document::{_Document, PageKey, PageKind, PageResult};
use crate::errors::{PdfError, StaleObjectError, limit_error};
use crate::pixmap::bridge_error;
use crate::text::box_bits;

/// Returns the items of the selected kinds that the page whose object is
/// `number` and `generation` paints, with the GIL released while hayro
/// interprets it. `max_elements` bounds the glyphs, path and clip segments,
/// dash lengths, and items with `content_element_count`; `None` lifts it.
/// The content is memoized with what interpreting the page may read, and
/// with the fallback faces when it holds text runs, the only items that read
/// them.
#[pyfunction]
#[expect(clippy::too_many_arguments, reason = "the options of Page.content")]
pub fn page_content(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    kinds: (bool, bool, bool),
    annotations: bool,
    clip: Option<BBox>,
    max_elements: Option<u64>,
) -> PyResult<Py<PyAny>> {
    crate::errors::boundary(|| {
        let content = with_content(
            py,
            document,
            (number, generation),
            kinds,
            (annotations, clip, max_elements),
            |content| Ok(content.clone()),
        )?;
        let mut items = Vec::new();
        items
            .try_reserve_exact(content.items.len())
            .map_err(|error| {
                PdfError::new_err(format!("cannot hold the content items: {error}"))
            })?;
        for item in content.items {
            items.push(match item {
                ContentItem::Text(run) => (0u8, *run).into_py_any(py)?,
                ContentItem::Path(row) => {
                    (1u8, content.paths.tuple(row as usize)).into_py_any(py)?
                }
                ContentItem::Image(image) => (2u8, *image).into_py_any(py)?,
            });
        }
        let contexts = &*content.contexts;
        (
            items,
            content.fonts,
            &contexts.clips,
            &contexts.groups,
            &contexts.marks,
            &contexts.states,
        )
            .into_py_any(py)
    })?
}

/// Returns the columns of the paths that the page whose object is `number`
/// and `generation` paints: those of the content that [`page_content`]
/// memoizes for paths alone, read with the GIL released, so that
/// `max_elements` bounds what it bounds there.
#[pyfunction]
pub fn page_paths(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    generation: u16,
    annotations: bool,
    clip: Option<BBox>,
    max_elements: Option<u64>,
) -> PyResult<PathColumns> {
    crate::errors::boundary(|| {
        with_content(
            py,
            document,
            (number, generation),
            (false, true, false),
            (annotations, clip, max_elements),
            |content| Ok(PathColumns::new(content)),
        )
    })?
}

/// Returns what `then` makes of the memoized content of `kinds` of the page
/// `(number, generation)`, interpreting the page with `(annotations, clip,
/// max_elements)` where the memo holds no content that fits, with the GIL
/// released throughout.
fn with_content<T: Send>(
    py: Python<'_>,
    document: &_Document,
    (number, generation): (u32, u16),
    kinds: (bool, bool, bool),
    (annotations, clip, max_elements): (bool, Option<BBox>, Option<u64>),
    then: impl FnOnce(&PageContent) -> PyResult<T> + Send,
) -> PyResult<T> {
    let budget = document.call_budget(&[(LimitCode::ContentElementCount, max_elements)])?;
    let (version, index) = crate::pages::index_with(document, None, &budget)?;
    let (text_runs, paths, images) = kinds;
    let fallback = if text_runs {
        document.fallback()
    } else {
        Fallback::default()
    };
    let kinds = ItemKinds {
        text_runs,
        paths,
        images,
    };
    py.detach(|| {
        let reference = Reference::new(number, generation);
        let (at, page) = index
            .index_of(reference)
            .and_then(|at| Some((at, index.get(at)?)))
            .ok_or_else(|| {
                StaleObjectError::new_err(format!(
                    "page object {number} {generation} is no longer a page of the document"
                ))
            })?;
        let key = PageKey {
            page: reference,
            kind: PageKind::Content {
                kinds: [text_runs, paths, images],
                annotations,
                clip: clip.map(box_bits),
                fallback: fallback.clone(),
            },
        };
        let result = document.page_result(
            &key,
            &version,
            page,
            &budget,
            |result| {
                matches!(result, &PageResult::Content { elements, .. }
                    if budget
                        .remaining(LimitCode::ContentElementCount)
                        .is_none_or(|remaining| elements <= remaining))
            },
            || {
                let bridged = document
                    .bridge
                    .get(&version, &index, &budget)
                    .map_err(|e| bridge_error(&e))?;
                // The elements stay charged until the call ends.
                let before = budget.used(LimitCode::ContentElementCount);
                let content = extract::page_content(
                    &bridged,
                    &fallback,
                    at,
                    kinds,
                    annotations,
                    clip,
                    &budget,
                )
                .map_err(|e| match e {
                    Error::Limit(refusal) => limit_error(&refusal),
                    Error::NoPage(_) | Error::Allocation(_) => PdfError::new_err(e.to_string()),
                })?;
                Ok(PageResult::Content {
                    content,
                    elements: budget.used(LimitCode::ContentElementCount) - before,
                })
            },
        )?;
        let PageResult::Content { content, .. } = &*result else {
            return Err(PdfError::new_err(
                "the memo holds another kind of result for this page",
            ));
        };
        then(content)
    })
}
