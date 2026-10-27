//! The PyO3 binding, the only crate that depends on PyO3 (DESIGN.md section
//! 11). It builds the extension module `pyropdf._core`.

use pyo3::prelude::*;

mod cancel;
mod content;
mod document;
mod errors;
mod history;
mod items;
mod objects;
mod ocr;
mod pages;
mod pixmap;
mod render;
mod security;
mod source;
mod text;
#[cfg(windows)]
mod windows_permissions;

/// The extension module of pyropdf, which `python/pyropdf` imports.
///
/// It runs without the GIL on free-threaded CPython: a document's state is
/// behind its own locks, and reads work on immutable versions (DESIGN.md
/// section 4.6; tests/test_threads.py).
#[pymodule(gil_used = false)]
mod _core {
    #[pymodule_export]
    use super::cancel::CancellationToken;
    #[pymodule_export]
    use super::content::{page_operations, parse_operations};
    #[pymodule_export]
    use super::document::{_Document, page_result_counts, probe, web_limits};
    #[pymodule_export]
    use super::errors::{
        DocumentClosedError, EncryptedDocumentError, LimitError, MalformedPdfError, OcrError,
        PasswordError, PdfError, StaleObjectError,
    };
    #[pymodule_export]
    use super::history::{
        begin_transaction, end_transaction, history_clear, history_redo, history_state,
        history_undo, set_history_limit,
    };
    #[pymodule_export]
    use super::items::page_content;
    #[pymodule_export]
    use super::objects::{
        decode_text_string, object_add, object_add_stream, object_delete, object_get,
        object_insert, object_keys, object_len, object_lookup, object_numbers, object_replace,
        object_set, stream_read, stream_write,
    };
    #[pymodule_export]
    use super::ocr::_OcrEngine;
    #[pymodule_export]
    use super::pages::{
        new_page, page_count, page_geometry, page_index, page_object, page_objects,
        set_page_rotation, set_pages,
    };
    #[pymodule_export]
    use super::pixmap::{Pixmap, render_page, render_pages};
    #[pymodule_export]
    use super::render::render_page_svg;
    #[pymodule_export]
    use super::security::{security_state, unlock};
    #[pymodule_export]
    use super::text::{
        document_text, page_layout, page_search, page_selection, page_tables, page_text,
    };

    #[cfg(windows)]
    #[pymodule_export]
    use super::windows_permissions::_copy_windows_dacl;

    /// The version of pyropdf: the version of its crates.
    #[pymodule_export]
    #[expect(non_upper_case_globals)]
    const __version__: &str = env!("CARGO_PKG_VERSION");
}
