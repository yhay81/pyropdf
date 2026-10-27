//! The content model for `python/pyropdf/_content.py`: what hayro paints on
//! a page, ported from pylopdf's `page_content` in
//! `rust/src/document/extraction.rs` at
//! 036be111cafe77755db4bb2be17bc70a8f2f9d82.
//!
//! The content crosses the boundary as `(items, fonts, clips, groups, marks,
//! states)`: each item is `(kind, tuple)`, with kind 0 for a text run, 1 for
//! a path, and 2 for an image, in the tuple forms of
//! `pyropdf_engine::extract`, which index the other lists.

use pyo3::IntoPyObjectExt;
use pyo3::prelude::*;
use pyropdf_engine::extract::{self, BBox, ContentItem, Error, ItemKinds};
use pyropdf_store::budget::LimitCode;
use pyropdf_store::value::Reference;

use crate::document::{_Document, PageKey, PageKind, PageResult};
use crate::errors::{PdfError, StaleObjectError, limit_error};
use crate::pixmap::bridge_error;
use crate::text::box_bits;

/// Returns the items of the selected kinds that the page whose object is
/// `number` and `generation` paints, with the GIL released while hayro
/// interprets it. `max_elements` bounds the glyphs, path and clip segments,
/// dash lengths, and items with `content_element_count`; `None` lifts it.
/// The content is memoized with what interpreting the page may read.
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
    let (version, index, budget) = crate::pages::index(document)?;
    let budget = budget.with_limit(LimitCode::ContentElementCount, max_elements);
    let (text_runs, paths, images) = kinds;
    let kinds = ItemKinds {
        text_runs,
        paths,
        images,
    };
    let result = py.detach(|| {
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
            },
        };
        document.page_result(
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
                let content =
                    extract::page_content(&bridged, at, kinds, annotations, clip, &budget)
                        .map_err(|e| match e {
                            Error::Limit(refusal) => limit_error(&refusal),
                            Error::NoPage(_) | Error::Allocation(_) => {
                                PdfError::new_err(e.to_string())
                            }
                        })?;
                Ok(PageResult::Content {
                    content,
                    elements: budget.used(LimitCode::ContentElementCount) - before,
                })
            },
        )
    })?;
    let PageResult::Content { content, .. } = &*result else {
        return Err(PdfError::new_err(
            "the memo holds another kind of result for this page",
        ));
    };
    let content = content.clone();
    let mut items = Vec::new();
    items
        .try_reserve_exact(content.items.len())
        .map_err(|error| PdfError::new_err(format!("cannot hold the content items: {error}")))?;
    for item in content.items {
        items.push(match item {
            ContentItem::Text(run) => (0u8, run).into_py_any(py)?,
            ContentItem::Path(path) => (1u8, path).into_py_any(py)?,
            ContentItem::Image(image) => (2u8, image).into_py_any(py)?,
        });
    }
    (
        items,
        content.fonts,
        content.clips,
        content.groups,
        content.marks,
        content.states,
    )
        .into_py_any(py)
}
