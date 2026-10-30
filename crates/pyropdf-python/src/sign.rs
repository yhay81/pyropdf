//! The core of `Signer` and `doc.sign`: the PAdES signatures of
//! `pyropdf_engine::sign`, whose callbacks call into Python.

use std::borrow::Cow;

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyropdf_engine::sign::der::Encode;
use pyropdf_engine::sign::{self, Chain, DigestAlgorithm, Error, Failure, Key};

use crate::errors::PdfError;

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

/// Signs `data`, a document saved with a placeholder of `reserve` bytes,
/// and returns the signed bytes. The digest runs without the GIL; the
/// callbacks take it.
#[pyfunction]
#[pyo3(signature = (data, reserve, signer, timestamper=None))]
pub fn sign_document(
    py: Python<'_>,
    data: Vec<u8>,
    reserve: usize,
    signer: &_Signer,
    timestamper: Option<Py<PyAny>>,
) -> PyResult<Py<PyBytes>> {
    let mut data = data;
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
        Ok(()) => Ok(PyBytes::new(py, &data).unbind()),
        Err(Failure::Sign(error)) => Err(sign_error(error)),
        Err(Failure::Callback(error)) => Err(error),
    }
}
