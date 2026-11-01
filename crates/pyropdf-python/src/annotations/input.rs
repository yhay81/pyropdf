//! Bounded reads of the mapping that `annotation_add` and `annotation_edit`
//! take (DESIGN.md F6 and section 9).
//!
//! A direct `_core` call skips the Python wrappers' checks, so each string
//! and collection is measured in place and checked against the operation's
//! budget before it is copied: a text's length against its limit, a name
//! against the longest name the binding knows, and a list's length against
//! its count limit. Every copy charges `memory_size` first, and the reads
//! poll the budget, so a deadline or a cancellation stops a long one.

use pyo3::exceptions::{PyKeyError, PyTypeError};
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedBytes;
use pyo3::types::{PyBytes, PyList, PyString, PyTuple};
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};

use super::Rect;
use crate::errors::{PdfError, limit_error};

/// The display point lists of a line, polygon, polyline, or ink annotation.
type PointLists = Vec<Vec<(f64, f64)>>;

/// The longest PDF name that a specification names, in characters: a
/// subtype, an icon, a blend mode, a border style, a line ending, or a
/// property to clear. The longest that the binding knows has 19.
pub(crate) const MAX_NAME: u64 = 32;

/// The longest date string that an annotation takes, in characters.
pub(crate) const MAX_DATE: u64 = 64;

/// The bytes that copying a string of `chars` characters may allocate:
/// CPython's UTF-8 cache and the owned copy, each at most 4 bytes a
/// character.
const fn text_cost(chars: u64) -> u64 {
    chars.saturating_mul(8).saturating_add(1)
}

/// Admits `chars` more characters of a text counted toward `code`, of
/// which `total` were admitted before: refuses with `code` once the
/// characters alone pass the operation's limit, since each takes at least
/// one UTF-8 byte, and charges the copy to `memory_size` before it is made.
pub(crate) fn admit_text(
    budget: &Budget,
    code: LimitCode,
    total: &mut u64,
    chars: u64,
) -> Result<(), LimitExceeded> {
    budget.poll()?;
    *total = total.saturating_add(chars);
    budget.check(code, budget.used(code).saturating_add(*total))?;
    budget.charge(LimitCode::MemorySize, text_cost(chars))
}

/// Admits a list of `len` items counted toward `code`, of which `total`
/// were admitted before, refusing past the limit before any item is read.
pub(crate) fn admit_items(
    budget: &Budget,
    code: LimitCode,
    total: &mut u64,
    len: u64,
) -> Result<(), LimitExceeded> {
    budget.poll()?;
    *total = total.saturating_add(len);
    budget.check(code, *total)
}

/// A list or a tuple, read by its stored length so that a subclass's
/// `__len__` cannot misreport it.
enum Sequence<'py> {
    List(Bound<'py, PyList>),
    Tuple(Bound<'py, PyTuple>),
}

impl<'py> Sequence<'py> {
    fn new(value: &Bound<'py, PyAny>, what: &str) -> PyResult<Self> {
        if let Ok(list) = value.cast::<PyList>() {
            return Ok(Self::List(list.clone()));
        }
        if let Ok(tuple) = value.cast::<PyTuple>() {
            return Ok(Self::Tuple(tuple.clone()));
        }
        Err(PyTypeError::new_err(format!(
            "{what} is a list or a tuple, not {}",
            value.get_type().name()?
        )))
    }

    fn len(&self) -> usize {
        match self {
            Self::List(list) => list.len(),
            Self::Tuple(tuple) => tuple.len(),
        }
    }

    fn get(&self, index: usize) -> PyResult<Bound<'py, PyAny>> {
        match self {
            Self::List(list) => list.get_item(index),
            Self::Tuple(tuple) => tuple.get_item(index),
        }
    }
}

/// Returns the length of `text` in characters, through `str`'s own method,
/// which a subclass cannot override.
fn chars(text: &Bound<'_, PyString>) -> PyResult<u64> {
    text.py()
        .get_type::<PyString>()
        .getattr("__len__")?
        .call1((text,))?
        .extract::<u64>()
}

/// The fields of a specification mapping, read against one budget.
pub(crate) struct Fields<'a, 'py> {
    map: &'a Bound<'py, PyAny>,
    budget: &'a Budget,
}

impl<'a, 'py> Fields<'a, 'py> {
    pub(crate) fn new(map: &'a Bound<'py, PyAny>, budget: &'a Budget) -> Self {
        Self { map, budget }
    }

    /// Returns the value of `key`, or `None` for a missing key or `None`.
    fn get(&self, key: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
        match self.map.get_item(key) {
            Ok(value) if value.is_none() => Ok(None),
            Ok(value) => Ok(Some(value)),
            Err(error) if error.is_instance_of::<PyKeyError>(self.map.py()) => Ok(None),
            Err(error) => Err(error),
        }
    }

    /// Extracts a value of a fixed size, such as a number or a rectangle.
    pub(crate) fn value<T>(&self, key: &str) -> PyResult<Option<T>>
    where
        T: pyo3::conversion::FromPyObjectOwned<'py>,
        for<'b> <T as FromPyObject<'b, 'py>>::Error: Into<PyErr>,
    {
        self.get(key)?
            .map(|value| value.extract::<T>().map_err(Into::into))
            .transpose()
    }

    /// Copies the text of `key`, counted toward `code` with the texts of
    /// `total` before it, after admitting its length.
    pub(crate) fn text(
        &self,
        key: &str,
        code: LimitCode,
        total: &mut u64,
    ) -> PyResult<Option<String>> {
        let Some(value) = self.get(key)? else {
            return Ok(None);
        };
        let text = string(&value, key)?;
        admit_text(self.budget, code, total, chars(text)?).map_err(|e| limit_error(&e))?;
        Ok(Some(text.to_str()?.to_owned()))
    }

    /// Copies the name or date of `key`, refusing with `refuse` one longer
    /// than `max` characters before copying it.
    pub(crate) fn short(
        &self,
        key: &str,
        max: u64,
        refuse: impl FnOnce() -> PyErr,
    ) -> PyResult<Option<String>> {
        self.get(key)?
            .map(|value| short(self.budget, &value, key, max, refuse))
            .transpose()
    }

    /// Copies the text markup rectangles of `key`, refusing more than the
    /// quadrilateral limit before reading one.
    pub(crate) fn rects(&self, key: &str) -> PyResult<Option<Vec<Rect>>> {
        let Some(value) = self.get(key)? else {
            return Ok(None);
        };
        let items = Sequence::new(&value, key)?;
        admit_items(
            self.budget,
            LimitCode::AnnotationQuadCount,
            &mut 0,
            items.len() as u64,
        )
        .map_err(|e| limit_error(&e))?;
        self.items(&items, |item| item.extract()).map(Some)
    }

    /// Copies the point lists of `key`, refusing more points in all than
    /// the point limit before reading a list past it.
    pub(crate) fn points(&self, key: &str) -> PyResult<Option<PointLists>> {
        let Some(value) = self.get(key)? else {
            return Ok(None);
        };
        let lists = Sequence::new(&value, key)?;
        let code = LimitCode::AnnotationPointCount;
        admit_items(self.budget, code, &mut 0, lists.len() as u64).map_err(|e| limit_error(&e))?;
        let mut total = 0;
        self.items(&lists, |list| {
            let points = Sequence::new(list, "a point list")?;
            admit_items(self.budget, code, &mut total, points.len() as u64)
                .map_err(|e| limit_error(&e))?;
            self.items(&points, |point| point.extract())
        })
        .map(Some)
    }

    /// Copies a border, `(width, style, dashes)`, refusing more dashes than
    /// the dash limit before reading one.
    pub(crate) fn border(&self, key: &str) -> PyResult<Option<(f64, String, Vec<f64>)>> {
        let Some(value) = self.get(key)? else {
            return Ok(None);
        };
        let parts = Sequence::new(&value, key)?;
        if parts.len() != 3 {
            return Err(PyTypeError::new_err("a border is (width, style, dashes)"));
        }
        let width = parts.get(0)?.extract::<f64>()?;
        let style = short(
            self.budget,
            &parts.get(1)?,
            "a border style",
            MAX_NAME,
            || PdfError::new_err("unknown annotation border style"),
        )?;
        let dashes = Sequence::new(&parts.get(2)?, "border dashes")?;
        admit_items(
            self.budget,
            LimitCode::AnnotationDashCount,
            &mut 0,
            dashes.len() as u64,
        )
        .map_err(|e| limit_error(&e))?;
        let dashes = self.items(&dashes, |dash| dash.extract())?;
        Ok(Some((width, style, dashes)))
    }

    /// Copies a line's start and end styles.
    pub(crate) fn line_endings(&self, key: &str) -> PyResult<Option<(String, String)>> {
        let Some(value) = self.get(key)? else {
            return Ok(None);
        };
        let parts = Sequence::new(&value, key)?;
        if parts.len() != 2 {
            return Err(PyTypeError::new_err("line endings are (start, end)"));
        }
        let refuse = || PdfError::new_err("unknown line ending");
        Ok(Some((
            short(
                self.budget,
                &parts.get(0)?,
                "a line ending",
                MAX_NAME,
                refuse,
            )?,
            short(
                self.budget,
                &parts.get(1)?,
                "a line ending",
                MAX_NAME,
                refuse,
            )?,
        )))
    }

    /// Copies the names of `key`, a required list of at most `max` names.
    pub(crate) fn names(&self, key: &str, max: usize) -> PyResult<Vec<String>> {
        let Some(value) = self.get(key)? else {
            return Err(PyTypeError::new_err(format!("missing {key}")));
        };
        let names = Sequence::new(&value, key)?;
        if names.len() > max {
            return Err(PdfError::new_err(format!(
                "{key} names at most {max} properties"
            )));
        }
        self.items(&names, |name| {
            short(self.budget, name, key, MAX_NAME, || {
                PdfError::new_err("an annotation property that cannot be removed")
            })
        })
    }

    /// Borrows the bytes of `key`, refusing data longer than `max` before a
    /// copy and charging the copy that a buffer other than `bytes` needs.
    pub(crate) fn data(&self, key: &str, max: Option<u64>) -> PyResult<Option<PyBackedBytes>> {
        let Some(value) = self.get(key)? else {
            return Ok(None);
        };
        self.budget.poll().map_err(|e| limit_error(&e))?;
        if let Ok(bytes) = value.cast::<PyBytes>() {
            let len = bytes.as_bytes().len() as u64;
            check_data(max, len)?;
        } else if let Ok(len) = value.len() {
            // A bytearray's stored length; another buffer fails to extract.
            let len = len as u64;
            check_data(max, len)?;
            self.budget
                .charge(LimitCode::MemorySize, len)
                .map_err(|e| limit_error(&e))?;
        }
        Ok(Some(value.extract::<PyBackedBytes>()?))
    }

    /// Reads each item of `items` with `read` into a budgeted list, polling
    /// the budget between items.
    fn items<T>(
        &self,
        items: &Sequence<'py>,
        mut read: impl FnMut(&Bound<'py, PyAny>) -> PyResult<T>,
    ) -> PyResult<Vec<T>> {
        let mut out = BudgetVec::new(self.budget);
        out.try_reserve_exact(items.len())
            .map_err(|e| limit_error(&e))?;
        for index in 0..items.len() {
            self.budget.poll().map_err(|e| limit_error(&e))?;
            let item = read(&items.get(index)?)?;
            out.try_push(item).map_err(|e| limit_error(&e))?;
        }
        Ok(out.into_vec())
    }
}

/// Casts `value` to a string, or refuses it as `what`.
fn string<'b, 'py>(value: &'b Bound<'py, PyAny>, what: &str) -> PyResult<&'b Bound<'py, PyString>> {
    value.cast::<PyString>().map_err(|_| {
        PyTypeError::new_err(format!(
            "{what} is a str, not {}",
            value
                .get_type()
                .name()
                .map_or_else(|_| "another type".into(), |name| name.to_string())
        ))
    })
}

/// Copies a short string, refusing one longer than `max` characters with
/// `refuse` before copying it.
fn short(
    budget: &Budget,
    value: &Bound<'_, PyAny>,
    what: &str,
    max: u64,
    refuse: impl FnOnce() -> PyErr,
) -> PyResult<String> {
    let text = string(value, what)?;
    let chars = chars(text)?;
    if chars > max {
        return Err(refuse());
    }
    budget.poll().map_err(|e| limit_error(&e))?;
    budget
        .charge(LimitCode::MemorySize, text_cost(chars))
        .map_err(|e| limit_error(&e))?;
    Ok(text.to_str()?.to_owned())
}

/// Refuses data of `len` bytes past `max` (`embedded_file_size`).
fn check_data(max: Option<u64>, len: u64) -> PyResult<()> {
    match max {
        Some(limit) if len > limit => Err(limit_error(&LimitExceeded::over(
            LimitCode::EmbeddedFileSize,
            limit,
            len,
        ))),
        _ => Ok(()),
    }
}
