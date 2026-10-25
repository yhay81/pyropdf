//! The PyO3 binding, the only crate that depends on PyO3 (DESIGN.md section
//! 11). It builds the extension module `pyropdf._core`.

use pyo3::prelude::*;

mod document;
mod errors;
mod source;

/// The extension module of pyropdf, which `python/pyropdf` imports.
#[pymodule]
mod _core {
    #[pymodule_export]
    use super::document::{_Document, probe, web_limits};
    #[pymodule_export]
    use super::errors::{
        DocumentClosedError, EncryptedDocumentError, LimitError, MalformedPdfError, PasswordError,
        PdfError, StaleObjectError,
    };

    /// The version of pyropdf: the version of its crates.
    #[pymodule_export]
    #[expect(non_upper_case_globals)]
    const __version__: &str = env!("CARGO_PKG_VERSION");
}
