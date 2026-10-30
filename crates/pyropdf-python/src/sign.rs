//! The core of `Signer` and `doc.sign`: the PAdES signatures of
//! `pyropdf_engine::sign`, whose callbacks call into Python.

use std::borrow::Cow;
use std::sync::Arc;

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyropdf_engine::sign::der::Encode;
use pyropdf_engine::sign::{self, Chain, DigestAlgorithm, Error, Failure, Key};
use pyropdf_store::save;
use pyropdf_store::value::Bytes;
use pyropdf_store::version::Document;

use crate::document::{_Document, open_base};
use crate::errors::{PdfError, save_error, store_error};
use crate::history::{begin_transaction, outside_transaction};

fn sign_error(error: Error) -> PyErr {
    match error {
        Error::Placeholder(message) => PdfError::new_err(message),
        other => PyValueError::new_err(other.to_string()),
    }
}

/// What makes the signature value.
enum Source {
    /// An ECDSA key inside pyropdf.
    Key(Key),
    /// A Python callable that signs the DER of the signed attributes.
    Callback(Py<PyAny>),
}

/// A signing key or callback, with its certificate chain.
#[pyclass(frozen, module = "pyropdf._core")]
pub struct _Signer {
    chain: Chain,
    source: Source,
}

#[pymethods]
impl _Signer {
    /// Reads a PEM private key and the PEM chain whose first certificate is
    /// the key's.
    #[staticmethod]
    #[pyo3(signature = (key, certificates, password=None))]
    fn from_pem(key: &[u8], certificates: &[u8], password: Option<&[u8]>) -> PyResult<Self> {
        let key = Key::from_pem(key, password).map_err(sign_error)?;
        let chain = Chain::from_pem(certificates).map_err(sign_error)?;
        if !key.matches(chain.signer()) {
            return Err(PyValueError::new_err(
                "the first certificate does not hold the key's public key",
            ));
        }
        Ok(Self {
            chain,
            source: Source::Key(key),
        })
    }

    /// Reads the key and the certificates of a PKCS#12 file.
    #[staticmethod]
    fn from_pkcs12(data: &[u8], password: &str) -> PyResult<Self> {
        let (key, certificates) = sign::parse_pkcs12(data, password).map_err(sign_error)?;
        Ok(Self {
            chain: Chain::new(certificates).map_err(sign_error)?,
            source: Source::Key(key),
        })
    }

    /// Wraps `sign`, which signs bytes with the key of the first of the DER
    /// `certificates`.
    #[staticmethod]
    fn callback(certificates: Vec<Vec<u8>>, sign: Py<PyAny>) -> PyResult<Self> {
        let certificates: Vec<&[u8]> = certificates.iter().map(Vec::as_slice).collect();
        Ok(Self {
            chain: Chain::from_der(&certificates).map_err(sign_error)?,
            source: Source::Callback(sign),
        })
    }

    /// Returns the DER certificates, the signer's first.
    #[getter]
    fn certificates(&self) -> PyResult<Vec<Cow<'static, [u8]>>> {
        self.chain
            .certificates()
            .iter()
            .map(|certificate| {
                Encode::to_der(certificate)
                    .map(Cow::Owned)
                    .map_err(|error| PyValueError::new_err(error.to_string()))
            })
            .collect()
    }

    /// Returns the name of the digest algorithm that the signature uses.
    #[getter]
    fn digest_algorithm(&self) -> &'static str {
        self.chain.algorithm().digest().name()
    }
}

/// Returns the bytes that a callback returned.
fn returned(value: &Bound<'_, PyAny>, what: &str) -> PyResult<Vec<u8>> {
    value
        .extract::<Cow<'_, [u8]>>()
        .map(Cow::into_owned)
        .map_err(|_| {
            let kind = value
                .get_type()
                .name()
                .map_or_else(|_| "?".to_owned(), |name| name.to_string());
            PyTypeError::new_err(format!("{what} must return bytes: {kind}"))
        })
}

/// Opens the transaction that [`sign_document`] signs: an outermost one,
/// which other threads' edits wait for.
#[pyfunction]
pub fn begin_signing(py: Python<'_>, document: &_Document) -> PyResult<()> {
    outside_transaction(document, "sign")?;
    begin_transaction(py, document, "sign".to_owned())
}

/// Saves `document` in an incremental revision with a placeholder of
/// `reserve` bytes, signs the saved bytes, and replaces the document with
/// them, which ends the transaction that [`begin_signing`] opened. The
/// caller discards that transaction when this refuses. The digest and the
/// save run without the GIL; the callbacks take it.
#[pyfunction]
#[pyo3(signature = (document, reserve, signer, timestamper=None))]
pub fn sign_document(
    py: Python<'_>,
    document: &_Document,
    reserve: usize,
    signer: &_Signer,
    timestamper: Option<Py<PyAny>>,
) -> PyResult<Py<PyBytes>> {
    if !document.in_outermost_transaction() {
        return Err(PdfError::new_err(
            "signing needs the transaction of begin_signing",
        ));
    }
    let (version, _) = crate::security::prepare(py, document, true, None)?;
    let before = document.reading()?;
    let budget = document.budget();
    let mut data = py.detach(|| {
        let mut out = Vec::new();
        save::incremental(&version, &mut out, &budget)
            .map_err(|error| save_error(&error, "PDF bytes"))?;
        Ok::<_, PyErr>(out)
    })?;
    let result = py.detach(|| {
        let sign_value = |attributes: &[u8]| -> PyResult<Vec<u8>> {
            match &signer.source {
                Source::Key(key) => Ok(key.sign(attributes)),
                Source::Callback(callable) => Python::attach(|py| {
                    let value = callable.call1(py, (PyBytes::new(py, attributes),))?;
                    returned(value.bind(py), "the signer's callback")
                }),
            }
        };
        let stamp = timestamper.as_ref().map(|callable| {
            move |digest: &[u8], algorithm: DigestAlgorithm| {
                Python::attach(|py| {
                    let value = callable.call1(py, (PyBytes::new(py, digest), algorithm.name()))?;
                    returned(value.bind(py), "the timestamper")
                })
            }
        });
        sign::sign(&mut data, reserve, &signer.chain, sign_value, stamp)
    });
    match result {
        Ok(()) => {}
        Err(Failure::Sign(error)) => return Err(sign_error(error)),
        Err(Failure::Callback(error)) => return Err(error),
    }
    // A callback that edited the document wrote into the transaction, which
    // the signed bytes do not hold.
    let after = document.reading()?;
    let unchanged = Arc::ptr_eq(&before, &after)
        || before
            .changes(&after, &budget)
            .map_err(|e| store_error(&e))?
            .is_some_and(|changes| changes.is_empty());
    if !unchanged {
        return Err(PdfError::new_err(
            "the document changed while it was being signed",
        ));
    }
    let limit = document.document()?.history().limit;
    let signed = Bytes::from(data);
    let base = py
        .detach(|| open_base(signed.clone(), None, &budget))
        .map_err(|e| store_error(&e))?;
    // Other threads' edits wait for the transaction's writer lock, which
    // the replaced document's transaction holds until it is dropped; then
    // they begin again on the signed document.
    document.replace(Document::new(Arc::new(base), limit));
    Ok(PyBytes::new(py, &signed).unbind())
}

/// A check of `sign::verify`: key, scheme, digest, signature, and message.
type CheckData<'py> = (
    Bound<'py, PyBytes>,
    String,
    String,
    Bound<'py, PyBytes>,
    Option<Bound<'py, PyBytes>>,
);

/// A SignedData of `sign::verify`: digest, message digest, signing time,
/// certificates, the signer's index, its check, the signature value, and
/// the time-stamp token as (signed data, TSTInfo, time, imprint matches).
type SignedTuple<'py> = (
    String,
    Option<Bound<'py, PyBytes>>,
    Option<u64>,
    Vec<Bound<'py, PyBytes>>,
    Option<usize>,
    Option<CheckData<'py>>,
    Bound<'py, PyBytes>,
);

type TimeStampTuple<'py> = (SignedTuple<'py>, Bound<'py, PyBytes>, u64, bool);

fn check_data<'py>(py: Python<'py>, check: &sign::verify::Check) -> CheckData<'py> {
    (
        PyBytes::new(py, &check.key),
        check.scheme.clone(),
        check.digest.clone(),
        PyBytes::new(py, &check.signature),
        check
            .message
            .as_ref()
            .map(|message| PyBytes::new(py, message)),
    )
}

fn signed_tuple<'py>(py: Python<'py>, signed: &sign::verify::Signed) -> SignedTuple<'py> {
    (
        signed.digest.clone(),
        signed
            .message_digest
            .as_ref()
            .map(|digest| PyBytes::new(py, digest)),
        signed.signing_time,
        signed
            .certificates
            .iter()
            .map(|certificate| PyBytes::new(py, certificate))
            .collect(),
        signed.signer,
        signed.check.as_ref().map(|check| check_data(py, check)),
        PyBytes::new(py, &signed.signature),
    )
}

/// Reads the CMS SignedData of a signature's `/Contents` for verification:
/// the SignedData, and its time-stamp token, if any.
#[pyfunction]
pub fn signature_read<'py>(
    py: Python<'py>,
    contents: &[u8],
) -> PyResult<(SignedTuple<'py>, Option<TimeStampTuple<'py>>)> {
    let signed = py
        .detach(|| sign::verify::read(contents))
        .map_err(|error| PdfError::new_err(error.to_string()))?;
    let timestamp = signed.timestamp.as_ref().map(|token| {
        (
            signed_tuple(py, &token.signed),
            PyBytes::new(py, &token.tst_info),
            token.time,
            token.imprint,
        )
    });
    Ok((signed_tuple(py, &signed), timestamp))
}

/// Reads a DER certificate: subject, DER subject and issuer, validity, its
/// check by the issuer (without the issuer's key), and its own key.
#[pyfunction]
#[expect(clippy::type_complexity, reason = "the parts of one certificate")]
pub fn certificate_read<'py>(
    py: Python<'py>,
    data: &[u8],
) -> PyResult<(
    String,
    Bound<'py, PyBytes>,
    Bound<'py, PyBytes>,
    (u64, u64),
    CheckData<'py>,
    Bound<'py, PyBytes>,
)> {
    let info = sign::verify::certificate(data).map_err(sign_error)?;
    Ok((
        info.subject.clone(),
        PyBytes::new(py, &info.subject_der),
        PyBytes::new(py, &info.issuer_der),
        info.validity,
        check_data(py, &info.check),
        PyBytes::new(py, &info.key),
    ))
}

/// Returns whether a DER ECDSA `signature` by the P-256 or P-384 key of the
/// DER SubjectPublicKeyInfo `key` signs the digest `prehash`.
#[pyfunction]
pub fn verify_ecdsa(key: &[u8], prehash: &[u8], signature: &[u8]) -> PyResult<bool> {
    sign::verify::verify_ecdsa(key, prehash, signature).map_err(sign_error)
}

/// The bytes of a file, its origin, and each revision's end and the
/// objects that its cross-reference sections list.
type FileData<'py> = (Bound<'py, PyBytes>, usize, Vec<(Option<usize>, Vec<u32>)>);

/// Returns the bytes of the opened file, where its offsets count from, and
/// where each of its revisions ends, with the objects that it lists, oldest
/// first; `None` for a document not read from a file or one whose index
/// was rebuilt.
#[pyfunction]
pub fn document_file<'py>(
    py: Python<'py>,
    document: &_Document,
) -> PyResult<Option<FileData<'py>>> {
    let version = document.reading()?;
    let opened = version.opened();
    let Some(index) = opened.base().file() else {
        return Ok(None);
    };
    if index.is_repaired() {
        return Ok(None);
    }
    let ends = index
        .revisions()
        .iter()
        .map(|revision| {
            let numbers = revision
                .sections()
                .iter()
                .flat_map(|section| section.listings().iter().map(|(number, _)| *number))
                .collect();
            (revision.end(), numbers)
        })
        .collect();
    Ok(Some((
        PyBytes::new(py, index.bytes()),
        index.origin(),
        ends,
    )))
}
