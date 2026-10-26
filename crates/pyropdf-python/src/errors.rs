//! The exception hierarchy of pylopdf, and the mapping of the store's typed
//! errors to it (DESIGN.md section 9).
//!
//! `python/pyropdf/_errors.py` re-exports these classes under the package's
//! name and gives `LimitError` its `code`.

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyropdf_store::{Error, ErrorKind, LimitExceeded, Password, save};

pyo3::create_exception!(
    _core,
    PdfError,
    PyException,
    "The base class of the errors that pyropdf raises for a document."
);
pyo3::create_exception!(
    _core,
    MalformedPdfError,
    PdfError,
    "Input that cannot be read as a PDF document, including empty input."
);
pyo3::create_exception!(
    _core,
    PasswordError,
    PdfError,
    "A password that matches neither the user password nor the owner password."
);
pyo3::create_exception!(
    _core,
    EncryptedDocumentError,
    PdfError,
    "An operation on an encrypted document that needs a password."
);
pyo3::create_exception!(
    _core,
    LimitError,
    PdfError,
    "A refusal by a limit, whose ``code`` names the limit."
);
pyo3::create_exception!(
    _core,
    StaleObjectError,
    PdfError,
    "Use of a handle whose object the current version no longer has."
);
pyo3::create_exception!(
    _core,
    DocumentClosedError,
    PdfError,
    "An operation on a closed document."
);

/// Returns the `LimitError` of a refusal, whose arguments are its stable code
/// and its message, as in pylopdf.
pub fn limit_error(refusal: &LimitExceeded) -> PyErr {
    LimitError::new_err((refusal.code().as_str(), refusal.to_string()))
}

/// Maps a typed store error to its class in the table of [`Error`].
pub fn store_error(error: &Error) -> PyErr {
    let message = error.to_string();
    match error.kind() {
        ErrorKind::Limit(refusal) => LimitError::new_err((refusal.code().as_str(), message)),
        ErrorKind::Malformed(_) => MalformedPdfError::new_err(message),
        ErrorKind::Password(Password::Incorrect) => PasswordError::new_err(message),
        ErrorKind::Password(_) => EncryptedDocumentError::new_err(format!(
            "{message}; open it with password= or call unlock()"
        )),
        ErrorKind::Stale(_) => StaleObjectError::new_err(message),
        _ => PdfError::new_err(message),
    }
}

/// Maps an error of a save; `target` names the output in the message of an
/// I/O error, as pylopdf names it.
pub fn save_error(error: &save::Error, target: &str) -> PyErr {
    match error {
        save::Error::Store(error) => store_error(error),
        save::Error::Io(error) => PdfError::new_err(format!("failed to save {target}: {error}")),
        save::Error::Refused(save::Refusal::Encryption) => {
            EncryptedDocumentError::new_err(error.to_string())
        }
        save::Error::Refused(_) => PdfError::new_err(error.to_string()),
    }
}
