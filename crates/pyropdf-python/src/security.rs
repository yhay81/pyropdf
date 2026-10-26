//! Passwords and encryption for `python/pyropdf/_encryption.py`: unlocking,
//! the state of the security handler (#28), and the encryption of saves
//! (DESIGN.md section 4.5).

use std::sync::Arc;

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyString, PyTuple};
use pyropdf_store::budget::Budget;
use pyropdf_store::save::FileBase;
use pyropdf_store::security::{Access, Encryption, Handler};
use pyropdf_store::version::{Document, Version};

use crate::document::_Document;
use crate::errors::store_error;
use crate::history::DEFAULT_HISTORY_LIMIT;

fn access_name(access: Access) -> &'static str {
    match access {
        Access::User => "user",
        Access::Owner => "owner",
    }
}

fn handler(version: &Version) -> Option<&Handler> {
    version.base().file()?.security()
}

/// Whether a document is encrypted, whether it is unlocked, what opened
/// it, its permission flags, and its handler's description.
type State = (
    bool,
    bool,
    Option<&'static str>,
    Option<u32>,
    Option<String>,
);

/// Returns whether the document is encrypted, whether it is unlocked, what
/// opened it, its permission flags, and its handler's description.
#[pyfunction]
pub fn security_state(document: &_Document) -> PyResult<State> {
    let version = document.current()?;
    let encrypted = version
        .base()
        .file()
        .is_some_and(pyropdf_store::index::Index::is_encrypted);
    Ok(match handler(&version) {
        Some(handler) => (
            true,
            true,
            Some(access_name(handler.access())),
            Some(handler.permissions()),
            Some(handler.description()),
        ),
        None => (encrypted, !encrypted, None, None, None),
    })
}

/// Unlocks the document with `password`, and returns what it opened:
/// `"user"` or `"owner"`. A locked document is read again, so its history
/// starts over; an unlocked one is only checked. A password that matches
/// neither refuses with `PasswordError`.
#[pyfunction]
pub fn unlock(py: Python<'_>, document: &_Document, password: Vec<u8>) -> PyResult<&'static str> {
    let version = document.current()?;
    let Some(index) = version.base().file().filter(|index| index.is_encrypted()) else {
        return Ok("owner");
    };
    let bytes = index.bytes().clone();
    let budget = document.budget();
    let base = py
        .detach(|| FileBase::open_with_password(bytes, Some(&password), &budget))
        .map_err(|e| store_error(&e))?;
    let access = base.security().map_or(Access::Owner, Handler::access);
    if handler(&version).is_none() {
        document.replace(Document::new(Arc::new(base), Some(DEFAULT_HISTORY_LIMIT)));
    }
    Ok(access_name(access))
}

/// Returns the version that a save writes and the encryption of a full
/// save, from `encryption`: `None` decrypts in a full save and keeps the
/// encryption in an incremental one; `"keep"` keeps it; a tuple of the
/// user and owner passwords, the permission flags, whether AES-256, and 64
/// random bytes encrypts anew, in a full save only.
pub(crate) fn prepare(
    py: Python<'_>,
    document: &_Document,
    incremental: bool,
    encryption: Option<&Bound<'_, PyAny>>,
) -> PyResult<(Arc<Version>, Option<Encryption>)> {
    let keep = match encryption {
        None => incremental,
        Some(value) if value.is_instance_of::<PyString>() => {
            if value.extract::<String>()? != "keep" {
                return Err(PyValueError::new_err(
                    "encryption must be 'keep' or a tuple",
                ));
            }
            true
        }
        Some(value) if value.is_instance_of::<PyTuple>() => {
            if incremental {
                return Err(PyValueError::new_err(
                    "an incremental save keeps the document's encryption; save in full to encrypt anew",
                ));
            }
            let (user, owner, permissions, aes256, random): (Vec<u8>, Vec<u8>, u32, bool, Vec<u8>) =
                value.extract()?;
            let random: [u8; 64] = random
                .try_into()
                .map_err(|_| PyValueError::new_err("encryption needs 64 random bytes"))?;
            let encryption = Encryption {
                user,
                owner,
                permissions,
                aes256,
                random,
            };
            return Ok((document.reading()?, Some(encryption)));
        }
        Some(_) => return Err(PyTypeError::new_err("encryption must be 'keep' or a tuple")),
    };
    let version = document.reading()?;
    if keep || version.trailer().get("Encrypt").is_none() {
        return Ok((version, None));
    }
    // A full save without encryption writes the version decrypted: the
    // same version with no /Encrypt in its trailer, which a transaction
    // writes and then discards.
    let mut trailer = version.trailer().clone();
    trailer.remove("Encrypt");
    let plain = document.scratch(py, |transaction, budget: &Budget| {
        transaction
            .set_trailer(trailer, budget)
            .map_err(|e| store_error(&e))?;
        Ok(transaction.snapshot())
    })?;
    Ok((plain, None))
}
