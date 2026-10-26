//! The PyO3 binding, the only crate that depends on PyO3 (DESIGN.md section
//! 11). It builds the extension module `pyropdf._core`.

use pyo3::prelude::*;

mod cancel;
mod document;
mod errors;
mod history;
mod objects;
mod pages;
mod pixmap;
mod security;
mod source;
#[cfg(windows)]
mod windows_permissions;

/// The extension module of pyropdf, which `python/pyropdf` imports.
#[pymodule]
mod _core {
    #[pymodule_export]
    use super::cancel::CancellationToken;
    #[pymodule_export]
    use super::document::{_Document, probe, web_limits};
    #[pymodule_export]
    use super::errors::{
        DocumentClosedError, EncryptedDocumentError, LimitError, MalformedPdfError, PasswordError,
        PdfError, StaleObjectError,
    };
    #[pymodule_export]
    use super::history::{
        begin_transaction, end_transaction, history_clear, history_redo, history_state,
        history_undo, set_history_limit,
    };
    #[pymodule_export]
    use super::objects::{
        decode_text_string, object_add, object_add_stream, object_delete, object_get,
        object_insert, object_keys, object_len, object_lookup, object_numbers, object_replace,
        object_set, stream_read, stream_write,
    };
    #[pymodule_export]
    use super::pages::{
        new_page, page_count, page_geometry, page_index, page_object, page_objects,
        set_page_rotation, set_pages,
    };
    #[pymodule_export]
    use super::pixmap::{Pixmap, render_page};
    #[pymodule_export]
    use super::security::{security_state, unlock};

    #[cfg(windows)]
    #[pymodule_export]
    use super::windows_permissions::_copy_windows_dacl;

    /// The version of pyropdf: the version of its crates.
    #[pymodule_export]
    #[expect(non_upper_case_globals)]
    const __version__: &str = env!("CARGO_PKG_VERSION");
}
