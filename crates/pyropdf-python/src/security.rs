//! Passwords and encryption for `python/pyropdf/_encryption.py`: unlocking,
//! the state of the security handler (#28), and the encryption of saves
//! (DESIGN.md section 4.5).

use std::sync::Arc;

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString, PyTuple};
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::save::FileBase;
use pyropdf_store::security::{Access, Encryption, Handler, RecipientEncryption};
use pyropdf_store::settle::Scope;
use pyropdf_store::value::Bytes;
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
    crate::errors::boundary(|| {
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
    })?
}

/// Verify the MAC over the opened revision, independently of the unlocking
/// and signature reports. The source bytes stay in the store.
#[pyfunction]
pub fn verify_integrity(
    py: Python<'_>,
    document: &_Document,
) -> PyResult<(&'static str, Option<&'static str>, Option<String>)> {
    use pyropdf_store::security::integrity::{self, Report};
    crate::errors::boundary(|| {
        let version = document.current()?;
        let budget = document.budget();
        let report = py
            .detach(|| match version.base().file() {
                Some(index) => integrity::verify(index, &budget),
                None => Ok(Report::Absent),
            })
            .map_err(|e| store_error(&e))?;
        Ok(match report {
            Report::Absent => ("absent", None, None),
            Report::Locked => ("locked", None, None),
            Report::Valid(location) => ("valid", Some(location), None),
            Report::Invalid(reason) => ("invalid", None, Some(reason)),
        })
    })?
}

/// Call the CMS provider only for envelopes validated by the store and
/// preserve a provider exception across the native operation.
pub(crate) fn recipient_base(
    bytes: Bytes,
    decrypt: &Py<PyAny>,
    budget: &Budget,
) -> PyResult<FileBase> {
    let mut failure = None;
    let result = FileBase::open_with_recipient(
        bytes,
        |der| {
            let plain = Python::attach(|py| -> PyResult<Option<[u8; 24]>> {
                decrypt.call1(py, (PyBytes::new(py, der),))?.extract(py)
            });
            plain.map_err(|e| {
                failure = Some(e);
                pyropdf_store::Error::from(pyropdf_store::Password::Recipient)
            })
        },
        budget,
    );
    result.map_err(|e| failure.unwrap_or_else(|| store_error(&e)))
}

/// Unlocks the document with `password`, and returns what it opened:
/// `"user"` or `"owner"`. A locked document is read again, so its history
/// starts over; an unlocked one is only checked. A password that matches
/// neither refuses with `PasswordError`.
#[pyfunction]
#[pyo3(signature = (document, password=None, recipient=None))]
pub fn unlock(
    py: Python<'_>,
    document: &_Document,
    password: Option<Vec<u8>>,
    recipient: Option<Py<PyAny>>,
) -> PyResult<&'static str> {
    crate::errors::boundary(|| {
        if password.is_some() == recipient.is_some() {
            return Err(PyTypeError::new_err(
                "unlock requires one password or recipient credential",
            ));
        }
        let version = document.current()?;
        let Some(index) = version.base().file().filter(|index| index.is_encrypted()) else {
            return Ok("owner");
        };
        let bytes = index.bytes().clone();
        let budget = document.budget();
        let base = py.detach(|| match recipient.as_ref() {
            Some(decrypt) => recipient_base(bytes, decrypt, &budget),
            None => FileBase::open_with_password(bytes, password.as_deref(), &budget)
                .map_err(|e| store_error(&e)),
        })?;
        let access = base.security().map_or(Access::Owner, Handler::access);
        if let Some(previous) = handler(&version) {
            if let Some(verified) = base.security() {
                previous.retain_recipient_access(verified);
            }
        } else {
            document.replace(Document::new(Arc::new(base), Some(DEFAULT_HISTORY_LIMIT)));
        }
        Ok(access_name(access))
    })?
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
            if value.len()? == 4 {
                type RecipientRequest<'py> =
                    ([u8; 20], [u8; 16], (bool, bool, bool), Bound<'py, PyAny>);
                let (seed, id, (encrypt_metadata, aes256, integrity), wrapped): RecipientRequest<
                    '_,
                > = value.extract()?;
                let budget = document.budget();
                let mut envelopes = BudgetVec::new(&budget);
                for item in wrapped.try_iter()? {
                    budget.poll().map_err(|e| crate::errors::limit_error(&e))?;
                    budget
                        .check(LimitCode::RecipientCount, envelopes.len() as u64 + 1)
                        .map_err(|e| crate::errors::limit_error(&e))?;
                    let item = item?;
                    let der = item.cast::<PyBytes>()?.as_bytes();
                    budget
                        .check(LimitCode::RecipientEnvelopeSize, der.len() as u64)
                        .map_err(|e| crate::errors::limit_error(&e))?;
                    budget
                        .charge(LimitCode::MemorySize, der.len() as u64)
                        .map_err(|e| crate::errors::limit_error(&e))?;
                    envelopes
                        .try_push(der.into())
                        .map_err(|e| crate::errors::limit_error(&e))?;
                }
                let request = RecipientEncryption {
                    seed,
                    id,
                    encrypt_metadata,
                    aes256,
                    envelopes: envelopes.into_vec(),
                };
                let encryption = if integrity {
                    Encryption::RecipientWithMac(request)
                } else {
                    Encryption::Recipient(request)
                };
                return Ok((saved(py, document, false)?, Some(encryption)));
            }
            let (user, owner, permissions, aes256, random, integrity): (
                Vec<u8>,
                Vec<u8>,
                u32,
                bool,
                Vec<u8>,
                bool,
            ) = value.extract()?;
            let random: [u8; 64] = random
                .try_into()
                .map_err(|_| PyValueError::new_err("encryption needs 64 random bytes"))?;
            let request = pyropdf_store::security::PasswordEncryption {
                user,
                owner,
                permissions,
                aes256,
                random,
            };
            let encryption = if integrity {
                Encryption::PasswordWithMac(request)
            } else {
                Encryption::Password(request)
            };
            return Ok((saved(py, document, false)?, Some(encryption)));
        }
        Some(_) => return Err(PyTypeError::new_err("encryption must be 'keep' or a tuple")),
    };
    Ok((saved(py, document, !keep)?, None))
}

/// Returns the version that a save writes: the version that reads see, with
/// every stale structure settled, font subsets included, and with no
/// /Encrypt in its trailer when `decrypt` asks for it. A transaction that is
/// then discarded makes those writes, so the current version keeps its
/// provisional fonts and the history gains no step.
fn saved(py: Python<'_>, document: &_Document, decrypt: bool) -> PyResult<Arc<Version>> {
    let version = document.reading()?;
    let decrypt = decrypt && version.trailer().get("Encrypt").is_some();
    if !decrypt && !version.has_stale() {
        return Ok(version);
    }
    document.scratch(py, |transaction, budget: &Budget| {
        transaction
            .settle(Scope::Save, budget)
            .map_err(|e| store_error(&e))?;
        if decrypt {
            let mut trailer = transaction.version().trailer().clone();
            trailer.remove("Encrypt");
            transaction
                .set_trailer(trailer, budget)
                .map_err(|e| store_error(&e))?;
        }
        Ok(transaction.snapshot())
    })
}
