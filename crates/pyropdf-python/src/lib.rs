//! The PyO3 binding, the only crate that depends on PyO3 (DESIGN.md section
//! 11). It builds the extension module `pyropdf._core`.

use pyo3::prelude::*;

mod annotations;
mod buffers;
mod cancel;
mod changes;
mod check;
mod content;
mod document;
mod errors;
mod forms;
mod history;
mod images;
mod items;
mod objects;
mod ocr;
mod outline;
mod pages;
mod pixmap;
mod render;
mod schema;
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
    use super::annotations::links::{
        link_add, link_edit, link_list, link_read, named_destinations,
    };
    #[pymodule_export]
    use super::annotations::{
        _stale_appearances, annotation_add, annotation_edit, annotation_geometry, annotation_list,
        annotation_open, annotation_popup, annotation_read, annotation_remove, annotation_reply,
    };
    #[pymodule_export]
    use super::buffers::{CharColumns, Column};
    #[pymodule_export]
    use super::cancel::CancellationToken;
    #[pymodule_export]
    use super::changes::changes;
    #[pymodule_export]
    use super::check::structural_check;
    #[pymodule_export]
    use super::content::{page_operations, parse_operations};
    #[pymodule_export]
    use super::document::{
        _Document, document_header_version, page_result_counts, probe, web_limits,
    };
    #[pymodule_export]
    use super::errors::{
        DocumentClosedError, EncryptedDocumentError, LimitError, MalformedPdfError, OcrError,
        PasswordError, PdfError, StaleObjectError,
    };
    #[pymodule_export]
    use super::forms::{
        flatten, form_field, form_field_choices, form_field_details, form_field_edit,
        form_field_fill, form_field_read, form_field_remove, form_field_widgets, form_fields,
        form_repair_radio_groups, form_set_need_appearances, form_walk_counts, widget_add,
        widget_edit, widget_list, widget_read, widget_remove, widget_style,
    };
    #[pymodule_export]
    use super::history::{
        begin_transaction, end_transaction, history_clear, history_redo, history_state,
        history_undo, set_history_limit,
    };
    #[pymodule_export]
    use super::images::{decode_png, image_stored, page_image};
    #[pymodule_export]
    use super::items::page_content;
    #[pymodule_export]
    use super::objects::{
        decode_text_string, object_add, object_add_stream, object_delete, object_free_unreferenced,
        object_get, object_insert, object_keys, object_len, object_lookup, object_numbers,
        object_replace, object_set, stream_read, stream_write,
    };
    #[pymodule_export]
    use super::ocr::{_OcrEngine, insert_ocr_layer};
    #[pymodule_export]
    use super::outline::{
        outline_children, outline_entries, outline_item, outline_set_open, outline_set_style,
        outline_set_target, outline_set_title, outline_walk_counts, outline_write,
    };
    #[pymodule_export]
    use super::pages::{
        import_pages, new_page, page_boxes, page_count, page_geometry, page_index, page_object,
        page_objects, set_page_box, set_page_rotation, set_pages,
    };
    #[pymodule_export]
    use super::pixmap::{Pixmap, render_page, render_pages};
    #[pymodule_export]
    use super::render::render_page_svg;
    #[pymodule_export]
    use super::schema::{schema_names, schema_object};
    #[pymodule_export]
    use super::security::{security_state, unlock};
    #[pymodule_export]
    use super::text::{
        document_text, page_chars, page_layout, page_search, page_selection, page_tables, page_text,
    };

    #[cfg(windows)]
    #[pymodule_export]
    use super::windows_permissions::_copy_windows_dacl;

    /// The version of pyropdf: the version of its crates.
    #[pymodule_export]
    #[expect(non_upper_case_globals)]
    const __version__: &str = env!("CARGO_PKG_VERSION");
}
