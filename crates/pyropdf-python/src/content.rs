//! Content streams as operations for `python/pyropdf/_operations.py`: a
//! page's content as hayro reads it, and any decoded content, each operation
//! with its bytes (DESIGN.md sections 6 and 14).
//!
//! An operation crosses the boundary as `("o", operator, operands, bytes)`,
//! and an inline image as `("i", entries, data, bytes)`, followed by the
//! bytes after the last operation. Operands are plain values, not handles:
//! `None`, `bool`, `int`, `("f", value, lexeme)` for a real, `("n", bytes)`
//! for a name, `("s", bytes)` for a string, `("a", items)` for an array, and
//! `("d", entries)` for a dictionary, whose entries are `(key, value)`.

use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyo3::{IntoPyObjectExt, intern};
use pyropdf_engine::content::{Operations, PageContent};
use pyropdf_store::budget::{Budget, LimitCode};
use pyropdf_store::memo::Reads;
use pyropdf_store::value::{Bytes, Dict, Reference, Value};

use crate::document::_Document;
use crate::errors::{MalformedPdfError, limit_error, store_error};

/// The nesting of arrays and dictionaries that hayro-syntax reads in
/// content, which bounds the conversion of an operand.
const MAX_OPERAND_DEPTH: usize = 64;

/// The operations of content in the tagged form, and its tail.
type Content<'py> = (Vec<Bound<'py, PyAny>>, Bound<'py, PyBytes>);

/// Tokenizes decoded content with the GIL released. `max_size` bounds the
/// content with `content_input_size`, and `max_operations` the operations
/// with `content_operation_count`; `None` lifts either.
#[pyfunction]
pub fn parse_operations<'py>(
    py: Python<'py>,
    data: &[u8],
    max_operations: Option<u64>,
    max_size: Option<u64>,
) -> PyResult<Content<'py>> {
    let budget = Budget::default()
        .with_limit(LimitCode::ContentOperationCount, max_operations)
        .with_limit(LimitCode::ContentInputSize, max_size);
    budget
        .check(
            LimitCode::ContentInputSize,
            u64::try_from(data.len()).unwrap_or(u64::MAX),
        )
        .map_err(|e| limit_error(&e))?;
    let data = Bytes::new(data.to_vec());
    let operations = py
        .detach(|| Operations::parse(data, &budget))
        .map_err(|e| store_error(&e))?;
    tagged(py, &operations)
}

/// Returns the operations of the content of the page whose object is
/// `number` and `generation`, as hayro reads them, or `None` when the page
/// tree no longer lists it.
#[pyfunction]
pub fn page_operations<'py>(
    py: Python<'py>,
    document: &_Document,
    number: u32,
    generation: u16,
) -> PyResult<Option<Content<'py>>> {
    let (version, index, budget) = crate::pages::index(document)?;
    let Some(at) = index.index_of(Reference::new(number, generation)) else {
        return Ok(None);
    };
    let page = &index.pages()[at];
    let content = py
        .detach(|| PageContent::read(&mut Reads::new(&version, &budget), page))
        .map_err(|e| store_error(&e))?;
    tagged(py, content.operations()).map(Some)
}

/// Returns the operations in the tagged form.
fn tagged<'py>(py: Python<'py>, operations: &Operations) -> PyResult<Content<'py>> {
    let mut items = Vec::new();
    items.try_reserve_exact(operations.len()).map_err(|error| {
        MalformedPdfError::new_err(format!("cannot hold the content's operations: {error}"))
    })?;
    for operation in operations.operations() {
        let bytes = PyBytes::new(py, operations.bytes(operation));
        let item = match (
            operations.image_data(operation),
            operations.operands(operation),
        ) {
            (Some(data), [Value::Dict(dict)]) => (
                intern!(py, "i"),
                entries(py, dict, 1)?,
                PyBytes::new(py, data),
                bytes,
            )
                .into_bound_py_any(py)?,
            (Some(_), _) => {
                return Err(MalformedPdfError::new_err(
                    "an inline image is read without its dictionary",
                ));
            }
            (None, operands) => {
                let operands = operands
                    .iter()
                    .map(|value| operand(py, value, 0))
                    .collect::<PyResult<Vec<_>>>()?;
                (
                    intern!(py, "o"),
                    PyBytes::new(py, operations.operator(operation)),
                    operands,
                    bytes,
                )
                    .into_bound_py_any(py)?
            }
        };
        items.push(item);
    }
    Ok((items, PyBytes::new(py, operations.tail())))
}

/// Returns an operand in the tagged form; `depth` counts the arrays and
/// dictionaries around it.
fn operand<'py>(py: Python<'py>, value: &Value, depth: usize) -> PyResult<Bound<'py, PyAny>> {
    match value {
        // hayro-syntax reads no reference in content, and a stream only as
        // an inline image, which `tagged` reads.
        Value::Null | Value::Reference(_) => Ok(py.None().into_bound(py)),
        Value::Boolean(value) => value.into_bound_py_any(py),
        Value::Integer(value) => value.into_bound_py_any(py),
        Value::Real(real) => (
            intern!(py, "f"),
            real.value(),
            real.lexeme().map(|lexeme| PyBytes::new(py, lexeme)),
        )
            .into_bound_py_any(py),
        Value::Name(name) => {
            (intern!(py, "n"), PyBytes::new(py, name.as_bytes())).into_bound_py_any(py)
        }
        Value::String(string) => {
            (intern!(py, "s"), PyBytes::new(py, string.as_bytes())).into_bound_py_any(py)
        }
        Value::Array(array) => {
            check_depth(depth + 1)?;
            let items = array
                .iter()
                .map(|item| operand(py, item, depth + 1))
                .collect::<PyResult<Vec<_>>>()?;
            (intern!(py, "a"), items).into_bound_py_any(py)
        }
        Value::Dict(dict) => {
            (intern!(py, "d"), entries(py, dict, depth + 1)?).into_bound_py_any(py)
        }
        Value::Stream(stream) => {
            (intern!(py, "d"), entries(py, stream.dict(), depth + 1)?).into_bound_py_any(py)
        }
    }
}

/// Returns the entries of a dictionary at `depth` as `(key, value)` pairs.
fn entries<'py>(
    py: Python<'py>,
    dict: &Dict,
    depth: usize,
) -> PyResult<Vec<(Bound<'py, PyBytes>, Bound<'py, PyAny>)>> {
    check_depth(depth)?;
    dict.iter()
        .map(|(key, value)| Ok((PyBytes::new(py, key.as_bytes()), operand(py, value, depth)?)))
        .collect()
}

/// Refuses an array or a dictionary deeper than hayro-syntax reads.
fn check_depth(depth: usize) -> PyResult<()> {
    if depth > MAX_OPERAND_DEPTH {
        return Err(MalformedPdfError::new_err(format!(
            "an operand nests arrays and dictionaries deeper than {MAX_OPERAND_DEPTH} levels"
        )));
    }
    Ok(())
}
