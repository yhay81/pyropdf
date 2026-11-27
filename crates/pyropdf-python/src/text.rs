//! `page.text`, `doc.text`, `page.words`, `page.layout`, `page.chars`,
//! `page.search`, and `page.selection`: the text of pages in reading order,
//! ported from pylopdf's `extract_text`, `extract_layout`, `search_page`,
//! and `select_text` in `rust/src/document/extraction.rs` at
//! 036be111cafe77755db4bb2be17bc70a8f2f9d82.

use std::ptr;
use std::sync::Arc;

use pyo3::exceptions::PyValueError;
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyList, PyString};
use pyropdf_engine::content::PageContent;
use pyropdf_engine::extract::{
    self, BBox, BlockTuple, Error, PageLayout, QuadTuple, RubyMode, SearchHits, TableOptions,
    TableTuple, TextOptions, TextPeak, Unmapped,
};
use pyropdf_engine::render::Fallback;
use pyropdf_store::ErrorKind;
use pyropdf_store::budget::{Budget, LimitCode, SharedBudget};
use pyropdf_store::memo::Reads;
use pyropdf_store::value::Reference;

use crate::buffers::CharColumns;
use crate::document::{_Document, PageKey, PageKind, PageResult};
use crate::errors::{PdfError, StaleObjectError, limit_error, store_error};
use crate::pages::PageSource;
use crate::pixmap::bridge_error;

/// The text options as `python/pyropdf/_text.py` passes them: `(clip, keep
/// clipped glyphs, unmapped code, expand ligatures, normalize whitespace,
/// dehyphenate, exact boxes, styles, ignore actual text, ruby mode)`, with the
/// unmapped codes 0 to skip, 1 for U+FFFD, 2 for the glyph ID, and 3 for Type
/// 3 char code, and ruby codes 0 to omit, 1 to parenthesize, and 2 to keep.
pub(crate) type OptionsTuple = (
    Option<BBox>,
    bool,
    u8,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    u8,
);

/// The text options as a memo key, with the clip's coordinates as bits, and
/// the fallback faces when the options ask for exact boxes, the only text
/// that reads them (DESIGN.md section 5).
pub(crate) type TextKey = (
    Option<[u64; 4]>,
    bool,
    u8,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    u8,
    Fallback,
);

fn text_key(options: OptionsTuple, fallback: &Fallback) -> TextKey {
    let (
        clip,
        keep_clipped,
        unmapped,
        ligatures,
        whitespace,
        dehyphenate,
        exact,
        styles,
        ignore_actual_text,
        ruby,
    ) = options;
    (
        clip.map(box_bits),
        keep_clipped,
        unmapped,
        ligatures,
        whitespace,
        dehyphenate,
        exact,
        styles,
        ignore_actual_text,
        ruby,
        if exact {
            fallback.clone()
        } else {
            Fallback::default()
        },
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

pub(crate) fn text_options(options: OptionsTuple) -> PyResult<TextOptions> {
    let (
        clip,
        keep_clipped,
        unmapped,
        expand_ligatures,
        normalize_whitespace,
        dehyphenate,
        exact_bboxes,
        styles,
        ignore_actual_text,
        ruby,
    ) = options;
    let unmapped = match unmapped {
        0 => Unmapped::Skip,
        1 => Unmapped::Replacement,
        2 => Unmapped::GlyphId,
        3 => Unmapped::Type3CharCode,
        other => {
            return Err(PyValueError::new_err(format!(
                "unknown unmapped code {other}"
            )));
        }
    };
    let ruby = match ruby {
        0 => RubyMode::Omit,
        1 => RubyMode::Parenthesize,
        2 => RubyMode::Keep,
        other => {
            return Err(PyValueError::new_err(format!("unknown ruby code {other}")));
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
        ignore_actual_text,
        ruby,
    })
}

pub(crate) fn text_error(error: Error) -> PyErr {
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
) -> PyResult<(Py<PyString>, usize)> {
    crate::errors::boundary(|| {
        let key = text_key(options, &Fallback::default());
        let options = text_options(options)?;
        let budget = document.call_budget(&[
            (LimitCode::TextGlyphCount, max_glyphs),
            (LimitCode::TextSize, max_size),
        ])?;
        let (version, index) = crate::pages::index_with(document, None, &budget)?;
        let result = py.detach(|| {
            let reference = Reference::new(number, generation);
            let (at, page) = index
                .index_of(reference)
                .and_then(|at| Some((at, index.get(at)?)))
                .ok_or_else(|| stale(number, generation))?;
            let key = PageKey {
                page: reference,
                kind: PageKind::Text(key),
            };
            document.page_result(
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
            )
        })?;
        match &*result {
            PageResult::Text { text, skipped, .. } => {
                Ok((PyString::new(py, text).unbind(), *skipped))
            }
            _ => Err(mismatch()),
        }
    })?
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
    crate::errors::boundary(|| {
        let options = text_options(options)?;
        let budget = document.call_budget(&[
            (LimitCode::TextGlyphCount, max_glyphs),
            (LimitCode::TextSize, max_size),
        ])?;
        let (version, index) = crate::pages::index_with(document, None, &budget)?;
        let indexes = crate::pages::entries(pages, index.len())?;
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
    })?
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
    document: PageSource<'_>,
    number: u32,
    generation: u16,
    options: OptionsTuple,
    chars: bool,
    max_glyphs: Option<u64>,
    max_size: Option<u64>,
) -> PyResult<LayoutTuple> {
    crate::errors::boundary(|| {
        let limits = (max_glyphs, max_size);
        with_layout(
            py,
            document,
            (number, generation),
            options,
            chars,
            limits,
            |layout, budget| {
                let columns = if chars {
                    Some(CharColumns::new(&layout.blocks, budget)?)
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
    })?
}

/// Returns the columns of the characters of the page whose object is
/// `number` and `generation`, in the order of [`page_layout`] with `chars`,
/// and the glyphs without Unicode that it left out, with the GIL released
/// and no Python object per character. It shares the memoized layout.
#[pyfunction]
pub fn page_chars(
    py: Python<'_>,
    document: PyRef<'_, _Document>,
    number: u32,
    generation: u16,
    options: OptionsTuple,
    max_glyphs: Option<u64>,
    max_size: Option<u64>,
) -> PyResult<(CharColumns, usize)> {
    crate::errors::boundary(|| {
        let limits = (max_glyphs, max_size);
        with_layout(
            py,
            PageSource::Live(document),
            (number, generation),
            options,
            true,
            limits,
            |layout, budget| Ok((CharColumns::new(&layout.blocks, budget)?, layout.skipped)),
        )
    })?
}

/// Cached Python constructors and slot setters for building `pyropdf.Word`
/// instances directly from the memoized layout without cloning spans or
/// crossing the Python loop boundary per word.
struct WordFactory {
    word_type: Py<PyAny>,
    rect_type: Py<PyAny>,
    new_object: Py<PyAny>,
    new_tuple: Py<PyAny>,
    set_text: Py<PyAny>,
    set_bbox: Py<PyAny>,
    set_block: Py<PyAny>,
    set_line: Py<PyAny>,
    set_index: Py<PyAny>,
}

static WORD_FACTORY: PyOnceLock<WordFactory> = PyOnceLock::new();

impl WordFactory {
    fn get(py: Python<'_>) -> PyResult<&'static Self> {
        WORD_FACTORY.get_or_try_init(py, || {
            let text_mod = py.import("pyropdf._text")?;
            let geom_mod = py.import("pyropdf._geometry")?;
            let builtins = py.import("builtins")?;
            let word_type = text_mod.getattr("Word")?;
            let rect_type = geom_mod.getattr("Rect")?;
            let new_object = builtins.getattr("object")?.getattr("__new__")?;
            let new_tuple = builtins.getattr("tuple")?.getattr("__new__")?;
            let set_text = word_type.getattr("text")?.getattr("__set__")?;
            let set_bbox = word_type.getattr("bbox")?.getattr("__set__")?;
            let set_block = word_type.getattr("block")?.getattr("__set__")?;
            let set_line = word_type.getattr("line")?.getattr("__set__")?;
            let set_index = word_type.getattr("index")?.getattr("__set__")?;
            Ok(Self {
                word_type: word_type.unbind(),
                rect_type: rect_type.unbind(),
                new_object: new_object.unbind(),
                new_tuple: new_tuple.unbind(),
                set_text: set_text.unbind(),
                set_bbox: set_bbox.unbind(),
                set_block: set_block.unbind(),
                set_line: set_line.unbind(),
                set_index: set_index.unbind(),
            })
        })
    }

    fn build_rect<'py>(
        &self,
        py: Python<'py>,
        coords_buf: *mut ffi::PyObject,
        bbox: BBox,
    ) -> PyResult<Bound<'py, PyAny>> {
        let (x0, y0, x1, y1) = bbox;
        if (x0 + y0 + x1 + y1).is_finite() {
            // SAFETY: Uses only Python 3.11+ Limited API calls with checked
            // non-null pointers. Reusing a single 4-element list avoids
            // allocating a temporary 4-tuple per word (`PyList_SetItem`
            // decrefs the previous float and steals the new reference).
            unsafe {
                for (slot, value) in [x0, y0, x1, y1].into_iter().enumerate() {
                    let float_obj = ffi::PyFloat_FromDouble(value);
                    if float_obj.is_null() {
                        return Err(PyErr::fetch(py));
                    }
                    if ffi::PyList_SetItem(coords_buf, slot as ffi::Py_ssize_t, float_obj) < 0 {
                        return Err(PyErr::fetch(py));
                    }
                }
                let rect = ffi::PyObject_CallFunctionObjArgs(
                    self.new_tuple.as_ptr(),
                    self.rect_type.as_ptr(),
                    coords_buf,
                    ptr::null_mut::<ffi::PyObject>(),
                );
                Bound::from_owned_ptr_or_err(py, rect)
            }
        } else {
            self.rect_type.bind(py).call1((x0, y0, x1, y1))
        }
    }

    fn set_slot(
        py: Python<'_>,
        setter: &Py<PyAny>,
        word: *mut ffi::PyObject,
        value: *mut ffi::PyObject,
    ) -> PyResult<()> {
        // SAFETY: `setter`, `word`, and `value` are valid Python object
        // pointers; the returned `None` reference is immediately decref'd.
        unsafe {
            let result = ffi::PyObject_CallFunctionObjArgs(
                setter.as_ptr(),
                word,
                value,
                ptr::null_mut::<ffi::PyObject>(),
            );
            if result.is_null() {
                return Err(PyErr::fetch(py));
            }
            ffi::Py_DECREF(result);
            Ok(())
        }
    }

    fn build_words<'py>(
        &self,
        py: Python<'py>,
        blocks: &[BlockTuple],
    ) -> PyResult<Bound<'py, PyList>> {
        let total: usize = blocks
            .iter()
            .flat_map(|(_, lines)| lines.iter())
            .map(|(_, _, words, _, _, _)| words.len())
            .sum();
        let list_len = ffi::Py_ssize_t::try_from(total)
            .map_err(|_| PyValueError::new_err("the page has too many words"))?;
        // SAFETY: `PyList_New` allocates a list of `list_len` null slots; each
        // slot is populated exactly once via `PyList_SetItem` (which steals the
        // reference), and `Bound::from_owned_ptr` cleans up cleanly on error.
        let list = unsafe {
            let ptr = ffi::PyList_New(list_len);
            if ptr.is_null() {
                return Err(PyErr::fetch(py));
            }
            Bound::from_owned_ptr(py, ptr).cast_into_unchecked::<PyList>()
        };
        if total == 0 {
            return Ok(list);
        }
        // Single reusable 4-element list backing `tuple.__new__(Rect, buf)`.
        let coords_buf = unsafe {
            let ptr = ffi::PyList_New(4);
            Bound::from_owned_ptr_or_err(py, ptr)?
        };
        let mut out_index: ffi::Py_ssize_t = 0;
        for (block_index, (_, lines)) in blocks.iter().enumerate() {
            let block_py = unsafe {
                let ptr = ffi::PyLong_FromSize_t(block_index);
                Bound::from_owned_ptr_or_err(py, ptr)?
            };
            for (line_index, (_, _, words, _, _, _)) in lines.iter().enumerate() {
                let line_py = unsafe {
                    let ptr = ffi::PyLong_FromSize_t(line_index);
                    Bound::from_owned_ptr_or_err(py, ptr)?
                };
                for (word_index, (bbox, text)) in words.iter().enumerate() {
                    let word = unsafe {
                        let ptr = ffi::PyObject_CallFunctionObjArgs(
                            self.new_object.as_ptr(),
                            self.word_type.as_ptr(),
                            ptr::null_mut::<ffi::PyObject>(),
                        );
                        Bound::from_owned_ptr_or_err(py, ptr)?
                    };
                    let text_py = PyString::new(py, text);
                    let rect_py = self.build_rect(py, coords_buf.as_ptr(), *bbox)?;
                    let index_py = unsafe {
                        let ptr = ffi::PyLong_FromSize_t(word_index);
                        Bound::from_owned_ptr_or_err(py, ptr)?
                    };
                    Self::set_slot(py, &self.set_text, word.as_ptr(), text_py.as_ptr())?;
                    Self::set_slot(py, &self.set_bbox, word.as_ptr(), rect_py.as_ptr())?;
                    Self::set_slot(py, &self.set_block, word.as_ptr(), block_py.as_ptr())?;
                    Self::set_slot(py, &self.set_line, word.as_ptr(), line_py.as_ptr())?;
                    Self::set_slot(py, &self.set_index, word.as_ptr(), index_py.as_ptr())?;
                    unsafe {
                        if ffi::PyList_SetItem(list.as_ptr(), out_index, word.into_ptr()) < 0 {
                            return Err(PyErr::fetch(py));
                        }
                    }
                    out_index += 1;
                }
            }
        }
        Ok(list)
    }
}

/// Returns the words of the page whose object is `number` and `generation` as
/// a list of `pyropdf.Word` records and the glyphs without Unicode that it
/// left out. Shares the memoized layout with [`page_layout`] (`chars=False`),
/// reading its words directly without cloning spans or converting them to
/// Python tuples.
#[pyfunction]
pub fn page_words(
    py: Python<'_>,
    document: PageSource<'_>,
    number: u32,
    generation: u16,
    options: OptionsTuple,
    max_glyphs: Option<u64>,
    max_size: Option<u64>,
) -> PyResult<(Py<PyList>, usize)> {
    crate::errors::boundary(|| {
        let factory = WordFactory::get(py)?;
        let (result, _) = layout_result(
            py,
            document,
            (number, generation),
            options,
            false,
            (max_glyphs, max_size),
        )?;
        match &*result {
            PageResult::Layout(layout) => {
                let words = factory.build_words(py, &layout.blocks)?;
                Ok((words.unbind(), layout.skipped))
            }
            _ => Err(mismatch()),
        }
    })?
}

/// Computes or looks up the memoized layout of the page `(number, generation)`
/// with the GIL released.
fn layout_result(
    py: Python<'_>,
    source: PageSource<'_>,
    (number, generation): (u32, u16),
    options: OptionsTuple,
    chars: bool,
    (max_glyphs, max_size): (Option<u64>, Option<u64>),
) -> PyResult<(Arc<PageResult>, SharedBudget)> {
    let document = source.document();
    let fallback = source.fallback();
    let key = text_key(options, &fallback);
    let options = text_options(options)?;
    let budget = document.call_budget(&[
        (LimitCode::TextGlyphCount, max_glyphs),
        (LimitCode::TextSize, max_size),
    ])?;
    let (version, index) = source.index(&budget)?;
    let result = py.detach(|| {
        let reference = Reference::new(number, generation);
        let (at, page) = index
            .index_of(reference)
            .and_then(|at| Some((at, index.get(at)?)))
            .ok_or_else(|| stale(number, generation))?;
        let key = PageKey {
            page: reference,
            kind: PageKind::Layout(key, chars),
        };
        document.page_result(
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
                extract::page_layout(
                    &bridged,
                    &fallback,
                    at,
                    &options,
                    chars,
                    content.as_ref(),
                    &budget,
                )
                .map(PageResult::Layout)
                .map_err(text_error)
            },
        )
    })?;
    Ok((result, budget))
}

/// Calls `then` with the memoized layout of the page `(number,
/// generation)`, computing it with `options`, chars when `chars` is set, and
/// the budgets `(max_glyphs, max_size)` where the memo has none, with the
/// GIL released.
fn with_layout<T: Send>(
    py: Python<'_>,
    source: PageSource<'_>,
    page_id: (u32, u16),
    options: OptionsTuple,
    chars: bool,
    limits: (Option<u64>, Option<u64>),
    then: impl FnOnce(&PageLayout, &Budget) -> PyResult<T> + Send,
) -> PyResult<T> {
    let (result, budget) = layout_result(py, source, page_id, options, chars, limits)?;
    py.detach(|| match &*result {
        PageResult::Layout(layout) => then(layout, &budget),
        _ => Err(mismatch()),
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
    crate::errors::boundary(|| {
        let options = text_options(options)?;
        let budget = document.call_budget(&[
            (LimitCode::SearchHitCount, max_hits),
            (LimitCode::TextGlyphCount, max_glyphs),
            (LimitCode::TextSize, max_size),
        ])?;
        let (version, index) = crate::pages::index_with(document, None, &budget)?;
        let fallback = document.fallback();
        py.detach(|| {
            let reference = Reference::new(number, generation);
            let at = index
                .index_of(reference)
                .ok_or_else(|| stale(number, generation))?;
            let bridged = document
                .bridge
                .get(&version, &index, &budget)
                .map_err(|e| bridge_error(&e))?;
            let (hits, skipped) =
                extract::page_search(&bridged, &fallback, at, &options, needle, quads, &budget)
                    .map_err(text_error)?;
            let hits = match hits {
                SearchHits::Boxes(boxes) => Hits::Boxes(boxes),
                SearchHits::Quads(quads) => Hits::Quads(quads),
            };
            Ok((hits, skipped))
        })
    })?
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
    crate::errors::boundary(|| {
        if ![start.0, start.1, end.0, end.1]
            .iter()
            .all(|value| value.is_finite())
        {
            return Err(PyValueError::new_err("selection points must be finite"));
        }
        let options = text_options(options)?;
        let budget = document.call_budget(&[
            (LimitCode::TextGlyphCount, max_glyphs),
            (LimitCode::TextSize, max_size),
        ])?;
        let (version, index) = crate::pages::index_with(document, None, &budget)?;
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
    })?
}

/// The table detection options as `python/pyropdf/_page.py` passes them:
/// `(text_strategy, clip, horizontal_lines, vertical_lines, snap_tolerance,
/// join_tolerance, min_gutter, min_words_vertical, min_words_horizontal)`.
pub(crate) type TableOptionsTuple = (
    bool,
    Option<BBox>,
    Vec<f64>,
    Vec<f64>,
    f64,
    f64,
    f64,
    usize,
    usize,
);

fn parse_table_options(options: TableOptionsTuple) -> PyResult<TableOptions> {
    let (
        text_strategy,
        clip,
        horizontal_lines,
        vertical_lines,
        snap_tolerance,
        join_tolerance,
        min_gutter,
        min_words_vertical,
        min_words_horizontal,
    ) = options;
    if !horizontal_lines.iter().all(|v| v.is_finite()) {
        return Err(PyValueError::new_err(
            "horizontal_lines coordinates must be finite",
        ));
    }
    if !vertical_lines.iter().all(|v| v.is_finite()) {
        return Err(PyValueError::new_err(
            "vertical_lines coordinates must be finite",
        ));
    }
    if !snap_tolerance.is_finite() || snap_tolerance < 0.0 {
        return Err(PyValueError::new_err(
            "snap_tolerance must be finite and non-negative",
        ));
    }
    if !join_tolerance.is_finite() || join_tolerance < 0.0 {
        return Err(PyValueError::new_err(
            "join_tolerance must be finite and non-negative",
        ));
    }
    if !min_gutter.is_finite() || min_gutter <= 0.0 {
        return Err(PyValueError::new_err(
            "min_gutter must be finite and positive",
        ));
    }
    if min_words_vertical == 0 {
        return Err(PyValueError::new_err(
            "min_words_vertical must be at least 1",
        ));
    }
    if min_words_horizontal == 0 {
        return Err(PyValueError::new_err(
            "min_words_horizontal must be at least 1",
        ));
    }
    Ok(TableOptions {
        text_strategy,
        clip,
        horizontal_lines,
        vertical_lines,
        snap_tolerance,
        join_tolerance,
        min_gutter,
        min_words_vertical,
        min_words_horizontal,
    })
}

/// Returns the tables on the page whose object is `number` and
/// `generation` according to `options`, and the glyphs without Unicode that it
/// left out, with the GIL released. The budgets are those of [`page_text`].
#[pyfunction]
pub fn page_tables(
    py: Python<'_>,
    document: PageSource<'_>,
    number: u32,
    generation: u16,
    options: TableOptionsTuple,
    max_glyphs: Option<u64>,
    max_size: Option<u64>,
) -> PyResult<(Vec<TableTuple>, usize)> {
    crate::errors::boundary(|| {
        let options = parse_table_options(options)?;
        let owner = document.document();
        let budget = owner.call_budget(&[
            (LimitCode::TextGlyphCount, max_glyphs),
            (LimitCode::TextSize, max_size),
        ])?;
        let (version, index) = document.index(&budget)?;
        py.detach(|| {
            let at = index
                .index_of(Reference::new(number, generation))
                .ok_or_else(|| stale(number, generation))?;
            let bridged = owner
                .bridge
                .get(&version, &index, &budget)
                .map_err(|e| bridge_error(&e))?;
            extract::page_tables(&bridged, at, &options, &budget).map_err(text_error)
        })
    })?
}
