//! The bytes of a document from a Python buffer or a file, checked against
//! `file_size` before any copy (DESIGN.md section 9).

use std::fs::File;
use std::io::Read;
use std::path::Path;

use pyo3::buffer::PyBuffer;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyMemoryView};
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::value::Bytes;

use crate::errors::limit_error;

/// A contiguous buffer of a `bytes` object that the store reads without a
/// copy. The export keeps the bytes alive, and `bytes` never change.
struct Borrowed(PyBuffer<u8>);

impl AsRef<[u8]> for Borrowed {
    fn as_ref(&self) -> &[u8] {
        let len = self.0.len_bytes();
        if len == 0 {
            return &[];
        }
        // SAFETY: `len > 0`, the buffer is read-only and C-contiguous, `len`
        // bytes long from `buf_ptr`, and exported by an exact immutable `bytes`
        // object that the export keeps alive as long as `self`.
        unsafe { std::slice::from_raw_parts(self.0.buf_ptr().cast(), len) }
    }
}

/// Returns the bytes of `source`, which supports the buffer protocol.
///
/// An exact `bytes`, and a contiguous read-only `memoryview` of exact `bytes`,
/// are borrowed. Any other buffer, such as a `bytearray`, a read-only view of
/// one, or a `bytes` subclass (which may override `__buffer__` under PEP 688),
/// is copied, since Python code may change it while the document reads it.
/// Both are checked against `file_size` first.
pub fn buffer(source: &Bound<'_, PyAny>, budget: &Budget) -> PyResult<Bytes> {
    let buffer = PyBuffer::<u8>::get(source)?;
    check_size(buffer.len_bytes() as u64, budget)?;
    if buffer.readonly() && buffer.is_c_contiguous() && exported_by_bytes(source)? {
        return Ok(Bytes::new(Borrowed(buffer)));
    }
    Ok(Bytes::new(buffer.to_vec(source.py())?))
}

/// Reads the file at `path`, after checking its size against `file_size`.
/// The GIL is not needed.
pub fn file(path: &Path, budget: &Budget) -> Result<Bytes, Error> {
    let mut file = File::open(path)?;
    let size = file.metadata()?.len();
    budget.check(LimitCode::FileSize, size)?;
    let mut data = BudgetVec::new(budget);
    let mut chunk = [0_u8; 64 * 1024];
    // Read one byte past the size checked, so that a file that grows
    // meanwhile still meets the limit.
    let cap = budget
        .limit(LimitCode::FileSize)
        .map_or(u64::MAX, |l| l.saturating_add(1));
    let mut reader = file.by_ref().take(cap);
    loop {
        budget.poll()?;
        let read = reader.read(&mut chunk)?;
        if read == 0 {
            break;
        }
        let next = (data.len() as u64).saturating_add(read as u64);
        budget.check(LimitCode::FileSize, next)?;
        data.try_extend_from_slice(&chunk[..read])?;
    }
    Ok(Bytes::new(data.into_vec()))
}

/// Why reading a file failed.
pub enum Error {
    Io(std::io::Error),
    Limit(LimitExceeded),
}

impl From<std::io::Error> for Error {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}

impl From<LimitExceeded> for Error {
    fn from(refusal: LimitExceeded) -> Self {
        Self::Limit(refusal)
    }
}

impl From<Error> for PyErr {
    fn from(error: Error) -> Self {
        match error {
            Error::Io(error) => error.into(),
            Error::Limit(refusal) => limit_error(&refusal),
        }
    }
}

/// Returns whether `source` is an exact `bytes` or an exact `memoryview` of
/// an exact `bytes`.
fn exported_by_bytes(source: &Bound<'_, PyAny>) -> PyResult<bool> {
    if source.is_exact_instance_of::<PyBytes>() {
        return Ok(true);
    }
    Ok(source.is_exact_instance_of::<PyMemoryView>()
        && source.getattr("obj")?.is_exact_instance_of::<PyBytes>())
}

fn check_size(size: u64, budget: &Budget) -> PyResult<()> {
    budget
        .check(LimitCode::FileSize, size)
        .map_err(|refusal| limit_error(&refusal))
}
