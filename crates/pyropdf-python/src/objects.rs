//! The low-level object model for `python/pyropdf/_objects.py` (DESIGN.md
//! section 10): reading and writing the objects of the version that reads
//! see, each write one transaction.
//!
//! A handle names a root, an indirect object or the trailer, and a path of
//! keys and indexes to a direct array or dictionary inside it. Every access
//! reads the root from the version, whose objects are parsed once and shared
//! by every later version, and walks the path, so its cost does not depend
//! on the size of the document or on what other edits wrote. A root that the
//! version no longer has, or a path that no longer leads to a direct array
//! or dictionary, raises `StaleObjectError`.
//!
//! Values cross the boundary in a tagged form. Rust returns `None`, `bool`,
//! `int`, `("f", value, lexeme)` for a real, `("n", bytes)` for a name,
//! `("s", bytes)` for a string, and `(tag, object)` for an array (`"a"`), a
//! dictionary (`"d"`), or a stream (`"t"`), where `object` is the number and
//! generation of an indirect one and `None` for a direct one. Python writes
//! the same scalars, `("a", items)`, `("d", entries)`, `("r", number,
//! generation)` for a reference, and `("c", root, path)` for a copy of what
//! a handle names.

use std::io::Write as _;
use std::sync::Arc;

use flate2::Compression;
use flate2::write::ZlibEncoder;
use pyo3::exceptions::{PyIndexError, PyKeyError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyList, PyTuple};
use pyo3::{IntoPyObjectExt, intern};
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::MAX_CHAIN;
use pyropdf_store::refs;
use pyropdf_store::value::{Array, Bytes, Dict, Name, PdfString, Real, Reference, Stream, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::document::_Document;
use crate::errors::{MalformedPdfError, StaleObjectError, limit_error, store_error};

/// How deeply a written value may nest arrays and dictionaries, as in
/// pylopdf; `python/pyropdf/_objects.py` checks it first.
const MAX_VALUE_DEPTH: usize = 32;

/// The object that a handle starts from: an indirect object, by number and
/// generation, or the trailer.
type Root = Option<(u32, u16)>;

/// A step of a handle's path: a dictionary key or an array index.
pub(crate) enum Step {
    Key(Vec<u8>),
    Index(usize),
}

impl<'py> FromPyObject<'_, 'py> for Step {
    type Error = PyErr;

    fn extract(step: Borrowed<'_, 'py, PyAny>) -> PyResult<Self> {
        if let Ok(key) = step.cast::<PyBytes>() {
            return Ok(Self::Key(key.as_bytes().to_vec()));
        }
        Ok(Self::Index(step.extract()?))
    }
}

/// Describes a handle for errors, as `12 0 R/Kids[3]` or `trailer`.
fn describe(root: Root, path: &[Step]) -> String {
    let mut text = match root {
        Some((number, generation)) => format!("{number} {generation} R"),
        None => "trailer".to_owned(),
    };
    for step in path {
        match step {
            Step::Key(key) => {
                text.push('/');
                text.push_str(&String::from_utf8_lossy(key));
            }
            Step::Index(index) => text.push_str(&format!("[{index}]")),
        }
    }
    text
}

fn stale(root: Root, path: &[Step]) -> PyErr {
    StaleObjectError::new_err(format!(
        "{} is no longer in the document; look it up again",
        describe(root, path)
    ))
}

/// Returns the value of `root` in `version`, or refuses a root that the
/// version no longer has.
fn root_value(
    version: &Version,
    root: Root,
    path: &[Step],
    budget: &Budget,
) -> PyResult<Arc<Value>> {
    match root {
        None => Ok(Arc::new(Value::Dict(version.trailer().clone()))),
        Some((number, generation)) => version
            .get(number, budget)
            .map_err(|e| store_error(&e))?
            .filter(|object| object.generation() == generation)
            .map(|object| Arc::clone(object.value()))
            .ok_or_else(|| stale(root, path)),
    }
}

/// Returns the child at `step` of an array, a dictionary, or a stream's
/// dictionary.
fn child<'v>(value: &'v Value, step: &Step) -> Option<&'v Value> {
    match (value, step) {
        (Value::Dict(dict), Step::Key(key)) => dict.get(key),
        (Value::Stream(stream), Step::Key(key)) => stream.dict().get(key),
        (Value::Array(array), Step::Index(index)) => array.get(*index),
        _ => None,
    }
}

fn child_mut<'v>(value: &'v mut Value, step: &Step) -> Option<&'v mut Value> {
    match (value, step) {
        (Value::Dict(dict), Step::Key(key)) => dict.get_mut(key),
        (Value::Stream(stream), Step::Key(key)) => stream.dict_mut().get_mut(key),
        (Value::Array(array), Step::Index(index)) => array.get_mut(*index),
        _ => None,
    }
}

/// Walks `path` from `value` to a direct array or dictionary, or refuses a
/// path that no longer leads to one.
fn walk<'v>(value: &'v Value, root: Root, path: &[Step]) -> PyResult<&'v Value> {
    let mut current = value;
    for step in path {
        current = child(current, step)
            .filter(|value| matches!(value, Value::Array(_) | Value::Dict(_)))
            .ok_or_else(|| stale(root, path))?;
    }
    Ok(current)
}

fn walk_mut<'v>(value: &'v mut Value, root: Root, path: &[Step]) -> PyResult<&'v mut Value> {
    let mut current = value;
    for step in path {
        current = child_mut(current, step)
            .filter(|value| matches!(value, Value::Array(_) | Value::Dict(_)))
            .ok_or_else(|| stale(root, path))?;
    }
    Ok(current)
}

/// Follows a reference, at most [`MAX_CHAIN`] of them, to the object it
/// names: `None` for a missing object or a null.
fn resolve(
    version: &Version,
    mut reference: Reference,
    budget: &Budget,
) -> PyResult<Option<(Reference, Arc<Value>)>> {
    for _ in 0..MAX_CHAIN {
        let Some(object) = version
            .get(reference.number(), budget)
            .map_err(|e| store_error(&e))?
            .filter(|object| object.generation() == reference.generation())
        else {
            return Ok(None);
        };
        match &**object.value() {
            Value::Null => return Ok(None),
            Value::Reference(next) => reference = *next,
            _ => return Ok(Some((reference, Arc::clone(object.value())))),
        }
    }
    Err(MalformedPdfError::new_err(
        "a reference chain is longer than 16 references",
    ))
}

/// Returns `value` in the tagged form, resolving a reference; `indirect`
/// names the object that holds a container, `None` for a direct one.
fn tagged<'py>(
    py: Python<'py>,
    version: &Version,
    value: &Value,
    indirect: Option<Reference>,
    budget: &Budget,
) -> PyResult<Bound<'py, PyAny>> {
    let object = indirect.map(|r| (r.number(), r.generation()));
    match value {
        Value::Null => Ok(py.None().into_bound(py)),
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
        Value::Array(_) => (intern!(py, "a"), object).into_bound_py_any(py),
        Value::Dict(_) => (intern!(py, "d"), object).into_bound_py_any(py),
        Value::Stream(_) => (intern!(py, "t"), object).into_bound_py_any(py),
        Value::Reference(reference) => match resolve(version, *reference, budget)? {
            Some((target, value)) => tagged(py, version, &value, Some(target), budget),
            None => Ok(py.None().into_bound(py)),
        },
    }
}

/// Returns the value that a tagged Python value writes, reading the
/// containers that it copies from `version`.
fn untagged(
    value: &Bound<'_, PyAny>,
    version: &Version,
    depth: usize,
    budget: &Budget,
) -> PyResult<Value> {
    if value.is_none() {
        return Ok(Value::Null);
    }
    if let Ok(value) = value.cast::<PyBool>() {
        return Ok(Value::Boolean(value.is_true()));
    }
    let Ok(tuple) = value.cast::<PyTuple>() else {
        return Ok(Value::Integer(value.extract()?));
    };
    let tag: String = tuple.get_item(0)?.extract()?;
    let item = |at: usize| tuple.get_item(at);
    match tag.as_str() {
        "f" => {
            let number: f64 = item(1)?.extract()?;
            let lexeme: Option<Vec<u8>> = item(2)?.extract()?;
            match lexeme {
                Some(lexeme) => Real::from_lexeme(Bytes::from(lexeme))
                    .map(Value::Real)
                    .ok_or_else(|| PyValueError::new_err("a real's lexeme is not a PDF real")),
                None if number.is_finite() => Ok(Value::from(number)),
                None => Err(PyValueError::new_err("a real must be finite")),
            }
        }
        "n" => Ok(Value::Name(Name::new(item(1)?.extract::<Vec<u8>>()?))),
        "s" => Ok(Value::String(PdfString::literal(
            item(1)?.extract::<Vec<u8>>()?,
        ))),
        "r" => Ok(Value::Reference(Reference::new(
            item(1)?.extract()?,
            item(2)?.extract()?,
        ))),
        "a" | "d" if depth >= MAX_VALUE_DEPTH => Err(limit_error(&LimitExceeded::over(
            LimitCode::ObjectValueDepth,
            MAX_VALUE_DEPTH as u64,
            depth as u64 + 1,
        ))),
        "a" => {
            let items = item(1)?;
            let items = items.cast::<PyList>()?;
            let mut array = Array::new();
            for element in items.iter() {
                array.push(untagged(&element, version, depth + 1, budget)?);
            }
            Ok(Value::Array(array))
        }
        "d" => {
            let entries = item(1)?;
            let entries = entries.cast::<PyList>()?;
            let mut dict = Dict::new();
            for entry in entries.iter() {
                let (key, element): (Vec<u8>, Bound<'_, PyAny>) = entry.extract()?;
                dict.insert(
                    Name::new(key),
                    untagged(&element, version, depth + 1, budget)?,
                );
            }
            Ok(Value::Dict(dict))
        }
        "c" => {
            let root: Root = item(1)?.extract()?;
            let path: Vec<Step> = item(2)?.extract()?;
            let value = root_value(version, root, &path, budget)?;
            refs::copy_for_edit(walk(&value, root, &path)?, budget)
                .map_err(|error| store_error(&error))
        }
        _ => Err(PyTypeError::new_err(format!("unknown value tag {tag:?}"))),
    }
}

/// Writes `value` as the root of a handle: the trailer, or an object that
/// must still have its generation.
fn write_root(
    transaction: &mut Transaction,
    root: Root,
    path: &[Step],
    value: Value,
    budget: &Budget,
) -> PyResult<()> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    match (root, value) {
        (None, Value::Dict(trailer)) => transaction.set_trailer(trailer, budget).map_err(store),
        (None, _) => Err(stale(root, path)),
        (Some((number, generation)), value) => {
            if transaction.version().generation(number) != Some(generation) {
                return Err(stale(root, path));
            }
            transaction.write(number, &value, budget).map_err(store)?;
            Ok(())
        }
    }
}

/// Changes the container that `root` and `path` name with `change`, in one
/// transaction. `change` receives the container and the version that the
/// transaction reads.
fn change(
    py: Python<'_>,
    document: &_Document,
    root: Root,
    path: &[Step],
    change: impl FnOnce(&mut Value, &Version, &Budget) -> PyResult<()>,
) -> PyResult<()> {
    document.edit(py, |transaction, budget| {
        let mut value = (*root_value(transaction.version(), root, path, budget)?).clone();
        change(
            walk_mut(&mut value, root, path)?,
            transaction.version(),
            budget,
        )?;
        write_root(transaction, root, path, value, budget)
    })
}

fn index_error(index: usize) -> PyErr {
    PyIndexError::new_err(format!("array index {index} out of range"))
}

/// Returns the object `number` as its generation and its tagged value, or
/// `None` when the version that reads see has no such object. A container
/// comes back as `(tag, None)`, for Python to name by the object.
#[pyfunction]
pub fn object_lookup<'py>(
    py: Python<'py>,
    document: &_Document,
    number: u32,
) -> PyResult<Option<(u16, Bound<'py, PyAny>)>> {
    let version = document.reading()?;
    let budget = document.budget();
    let Some(object) = version.get(number, &budget).map_err(|e| store_error(&e))? else {
        return Ok(None);
    };
    let value = tagged(py, &version, object.value(), None, &budget)?;
    Ok(Some((object.generation(), value)))
}

/// Returns the numbers of the objects in use, ascending.
#[pyfunction]
pub fn object_numbers(document: &_Document) -> PyResult<Vec<u32>> {
    let version = document.reading()?;
    let budget = document.budget();
    let store = |e: pyropdf_store::Error| store_error(&e);
    let mut numbers = BudgetVec::new(&budget);
    version.base().numbers(&mut numbers).map_err(store)?;
    for (number, _) in version.slots() {
        numbers.try_push(number).map_err(|e| limit_error(&e))?;
    }
    let mut numbers = numbers.into_vec();
    numbers.sort_unstable();
    numbers.dedup();
    numbers.retain(|&number| version.generation(number).is_some());
    Ok(numbers)
}

/// Returns the tagged child at `step` of the container that `root` and
/// `path` name, resolving a reference; `KeyError` or `IndexError` when it
/// has none.
#[pyfunction]
pub fn object_get<'py>(
    py: Python<'py>,
    document: &_Document,
    root: Root,
    path: Vec<Step>,
    step: Step,
) -> PyResult<Bound<'py, PyAny>> {
    let version = document.reading()?;
    let budget = document.budget();
    let value = root_value(&version, root, &path, &budget)?;
    let container = walk(&value, root, &path)?;
    let Some(found) = child(container, &step) else {
        return Err(match step {
            Step::Key(key) => PyKeyError::new_err(String::from_utf8_lossy(&key).into_owned()),
            Step::Index(index) => index_error(index),
        });
    };
    tagged(py, &version, found, None, &budget)
}

/// Returns the keys of the dictionary, or the stream's dictionary, that
/// `root` and `path` name, in order.
#[pyfunction]
pub fn object_keys<'py>(
    py: Python<'py>,
    document: &_Document,
    root: Root,
    path: Vec<Step>,
) -> PyResult<Vec<Bound<'py, PyBytes>>> {
    let version = document.reading()?;
    let value = root_value(&version, root, &path, &document.budget())?;
    let dict = match walk(&value, root, &path)? {
        Value::Dict(dict) => dict,
        Value::Stream(stream) => stream.dict(),
        _ => return Err(stale(root, &path)),
    };
    Ok(dict
        .keys()
        .map(|key| PyBytes::new(py, key.as_bytes()))
        .collect())
}

/// Returns the number of entries or elements of the container that `root`
/// and `path` name.
#[pyfunction]
pub fn object_len(document: &_Document, root: Root, path: Vec<Step>) -> PyResult<usize> {
    let version = document.reading()?;
    let value = root_value(&version, root, &path, &document.budget())?;
    Ok(match walk(&value, root, &path)? {
        Value::Dict(dict) => dict.len(),
        Value::Stream(stream) => stream.dict().len(),
        Value::Array(array) => array.len(),
        _ => return Err(stale(root, &path)),
    })
}

/// Writes the tagged `value` at `step` of the container that `root` and
/// `path` name, in one transaction.
#[pyfunction]
pub fn object_set(
    py: Python<'_>,
    document: &_Document,
    root: Root,
    path: Vec<Step>,
    step: Step,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    change(py, document, root, &path, |container, version, budget| {
        let value = untagged(value, version, 1, budget)?;
        match (container, step) {
            (Value::Dict(dict), Step::Key(key)) => {
                dict.insert(Name::new(key), value);
            }
            (Value::Stream(stream), Step::Key(key)) => {
                stream.dict_mut().insert(Name::new(key), value);
            }
            (Value::Array(array), Step::Index(index)) => {
                *array.get_mut(index).ok_or_else(|| index_error(index))? = value;
            }
            _ => return Err(PyTypeError::new_err("the step does not fit the container")),
        }
        Ok(())
    })
}

/// Removes `step` from the container that `root` and `path` name, in one
/// transaction; `KeyError` or `IndexError` when it has none.
#[pyfunction]
pub fn object_delete(
    py: Python<'_>,
    document: &_Document,
    root: Root,
    path: Vec<Step>,
    step: Step,
) -> PyResult<()> {
    change(py, document, root, &path, |container, _, _| {
        let removed = match (container, &step) {
            (Value::Dict(dict), Step::Key(key)) => dict.remove(key).is_some(),
            (Value::Stream(stream), Step::Key(key)) => stream.dict_mut().remove(key).is_some(),
            (Value::Array(array), Step::Index(index)) => array.remove(*index).is_some(),
            _ => return Err(PyTypeError::new_err("the step does not fit the container")),
        };
        match (removed, step) {
            (true, _) => Ok(()),
            (false, Step::Key(key)) => Err(PyKeyError::new_err(
                String::from_utf8_lossy(&key).into_owned(),
            )),
            (false, Step::Index(index)) => Err(index_error(index)),
        }
    })
}

/// Inserts the tagged `value` before `index`, at most the length, in the
/// array that `root` and `path` name, in one transaction.
#[pyfunction]
pub fn object_insert(
    py: Python<'_>,
    document: &_Document,
    root: Root,
    path: Vec<Step>,
    index: usize,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    change(py, document, root, &path, |container, version, budget| {
        let value = untagged(value, version, 1, budget)?;
        let Value::Array(array) = container else {
            return Err(PyTypeError::new_err("only an array takes an insertion"));
        };
        let at = index.min(array.len());
        array.insert(at, value).map_err(|_| index_error(index))
    })
}

/// Replaces object `number` with the tagged `value`, in one transaction;
/// `KeyError` when the object is not in use.
#[pyfunction]
pub fn object_replace(
    py: Python<'_>,
    document: &_Document,
    number: u32,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    document.edit(py, |transaction, budget| {
        if transaction.version().generation(number).is_none() {
            return Err(PyKeyError::new_err(number));
        }
        let value = untagged(value, transaction.version(), 0, budget)?;
        transaction
            .write(number, &value, budget)
            .map_err(|e| store_error(&e))?;
        Ok(())
    })
}

/// Adds the tagged `value` as a new object, in one transaction, and returns
/// its number and generation.
#[pyfunction]
pub fn object_add(
    py: Python<'_>,
    document: &_Document,
    value: &Bound<'_, PyAny>,
) -> PyResult<(u32, u16)> {
    document.edit(py, |transaction, budget| {
        let value = untagged(value, transaction.version(), 0, budget)?;
        add(transaction, &value, budget)
    })
}

fn add(transaction: &mut Transaction, value: &Value, budget: &Budget) -> PyResult<(u32, u16)> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    let reference = transaction.allocate().map_err(store)?;
    let written = transaction
        .write(reference.number(), value, budget)
        .map_err(store)?;
    Ok((written.number(), written.generation()))
}

/// Sets the data of `stream` to `data`, given decoded, compressed with
/// `/FlateDecode` when `compress`, and drops its previous filters.
pub(crate) fn set_data(stream: &mut Stream, data: &[u8], compress: bool) -> PyResult<()> {
    let dict = stream.dict_mut();
    dict.remove("Filter");
    dict.remove("DecodeParms");
    if !compress {
        stream.set_data(Bytes::from(data.to_vec()));
        return Ok(());
    }
    let mut encoder = ZlibEncoder::new(Vec::new(), Compression::default());
    encoder
        .write_all(data)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    let encoded = encoder
        .finish()
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    stream
        .dict_mut()
        .insert(Name::from("Filter"), Value::Name(Name::from("FlateDecode")));
    stream.set_data(Bytes::from(encoded));
    Ok(())
}

/// Adds a stream of the tagged dictionary `dict` and the decoded `data`,
/// in one transaction, and returns its number and generation.
#[pyfunction]
pub fn object_add_stream(
    py: Python<'_>,
    document: &_Document,
    dict: &Bound<'_, PyAny>,
    data: &[u8],
    compress: bool,
) -> PyResult<(u32, u16)> {
    document.edit(py, |transaction, budget| {
        let Value::Dict(dict) = untagged(dict, transaction.version(), 0, budget)? else {
            return Err(PyTypeError::new_err(
                "a stream's dictionary must be a mapping",
            ));
        };
        let mut stream = Stream::new(dict, Bytes::default());
        set_data(&mut stream, data, compress)?;
        add(transaction, &Value::Stream(Box::new(stream)), budget)
    })
}

/// Returns the stored size of a stream without copying or decoding its data.
#[pyfunction]
pub fn stream_raw_size(document: &_Document, root: (u32, u16)) -> PyResult<usize> {
    let version = document.reading()?;
    let budget = document.budget();
    let value = root_value(&version, Some(root), &[], &budget)?;
    let Value::Stream(stream) = &*value else {
        return Err(stale(Some(root), &[]));
    };
    let size = stream.data().len();
    budget
        .check(
            LimitCode::StreamDataSize,
            u64::try_from(size).unwrap_or(u64::MAX),
        )
        .map_err(|error| limit_error(&error))?;
    Ok(size)
}

/// Returns the data of the stream that `root` names, decoded through its
/// filters when `decode`. `max_size` bounds the result with the limit
/// `stream_data_size`.
#[pyfunction]
pub fn stream_read<'py>(
    py: Python<'py>,
    document: &_Document,
    root: (u32, u16),
    decode: bool,
    max_size: Option<u64>,
) -> PyResult<Bound<'py, PyBytes>> {
    let version = document.reading()?;
    let budget = document
        .budget()
        .with_limit(LimitCode::StreamDataSize, max_size);
    let value = root_value(&version, Some(root), &[], &budget)?;
    let Value::Stream(stream) = &*value else {
        return Err(stale(Some(root), &[]));
    };
    let limit = |len: usize| {
        budget
            .check(LimitCode::StreamDataSize, len as u64)
            .map_err(|e| limit_error(&e))
    };
    if !decode {
        limit(stream.data().len())?;
        return Ok(PyBytes::new(py, stream.data()));
    }
    let data = py
        .detach(|| stream.decoded(&budget))
        .map_err(|e| store_error(&e))?;
    limit(data.len())?;
    Ok(PyBytes::new(py, &data))
}

/// Replaces the data of the stream that `root` names with `data`, given
/// decoded, in one transaction.
#[pyfunction]
pub fn stream_write(
    py: Python<'_>,
    document: &_Document,
    root: (u32, u16),
    data: &[u8],
    compress: bool,
) -> PyResult<()> {
    change(py, document, Some(root), &[], |value, _, _| {
        let Value::Stream(stream) = value else {
            return Err(stale(Some(root), &[]));
        };
        set_data(stream, data, compress)
    })
}

/// Decodes a text string, as `String.text` does.
#[pyfunction]
pub fn decode_text_string(data: &[u8]) -> String {
    crate::document::text_string(data)
}

/// Frees the objects of `candidates` that nothing in the edit's version
/// refers to but freed objects, with every object that only freed objects
/// refer to, in one transaction, as the reverse-reference index finds them;
/// an object that anything else refers to stays. Returns the freed objects,
/// ascending.
#[pyfunction]
pub fn object_free_unreferenced(
    py: Python<'_>,
    document: &_Document,
    candidates: Vec<u32>,
) -> PyResult<Vec<u32>> {
    let store = |e: pyropdf_store::Error| store_error(&e);
    document.edit(py, |transaction, budget| {
        let parent = Arc::clone(transaction.parent());
        let before = transaction.snapshot();
        let references = document.references.get(&parent, budget).map_err(store)?;
        let references = match parent.changes(&before, budget).map_err(store)? {
            Some(changes) if !changes.is_empty() => Arc::new(
                references
                    .update(&mut Reads::new(&before, budget), &changes)
                    .map_err(store)?,
            ),
            _ => references,
        };
        let freed = refs::unreferenced(&references, &[], &candidates, budget).map_err(store)?;
        for &number in freed.iter() {
            transaction.free(number, budget).map_err(store)?;
        }
        Ok(freed.into_vec())
    })
}
