//! `Layout.chars` and `page.chars`: the characters of a layout as columns,
//! one buffer per field, that Python reads through the buffer protocol and
//! the Arrow PyCapsule interface without an object per glyph (DESIGN.md
//! section 10).

use std::ffi::{CStr, c_char, c_int, c_void};
use std::ptr;
use std::sync::Arc;

use pyo3::exceptions::{PyBufferError, PyIndexError, PyValueError};
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::PyCapsule;
use pyropdf_engine::extract::{BBox, BlockTuple};

/// One row as `python/pyropdf/_text.py` builds a `Char` from it: `(text,
/// bbox, origin, synthetic, (stream, operation) or None)`.
type RowTuple<'a> = (&'a str, BBox, (f64, f64), bool, Option<(u32, u32)>);

/// The characters of a layout, one row per char in layout order, each field
/// in its own column.
struct Table {
    x0: Vec<f64>,
    y0: Vec<f64>,
    x1: Vec<f64>,
    y1: Vec<f64>,
    origin_x: Vec<f64>,
    origin_y: Vec<f64>,
    /// Arrow `utf8` offsets into `text_data`, one more than the rows.
    text_offsets: Vec<i32>,
    text_data: Vec<u8>,
    block: Vec<u32>,
    line: Vec<u32>,
    span: Vec<u32>,
    is_synthetic: Vec<u8>,
    /// The object number of the char's content stream, or 0, which no
    /// object has, where the char has no source.
    stream: Vec<u32>,
    operation: Vec<u32>,
    /// Arrow bitmaps, least significant bit first: whether a row is
    /// synthetic, and whether it has a source.
    synthetic_bits: Vec<u8>,
    source_bits: Vec<u8>,
}

/// The columns that `Chars.column` names, in the order of `Chars.names`.
const COLUMNS: [&str; 14] = [
    "x0",
    "y0",
    "x1",
    "y1",
    "origin_x",
    "origin_y",
    "text_offsets",
    "text_data",
    "block",
    "line",
    "span",
    "is_synthetic",
    "stream",
    "operation",
];

/// Returns an index as the `u32` of a column.
fn index(index: usize) -> PyResult<u32> {
    u32::try_from(index).map_err(|_| PyValueError::new_err("the layout has too many parts"))
}

/// Sets bit `row` of `bits`, which holds a byte per eight rows.
fn set_bit(bits: &mut [u8], row: usize) {
    bits[row / 8] |= 1 << (row % 8);
}

impl Table {
    /// Returns the columns of the chars of `blocks`, a layout built with
    /// chars.
    fn new(blocks: &[BlockTuple]) -> PyResult<Self> {
        let rows: usize = blocks
            .iter()
            .flat_map(|(_, lines)| lines)
            .flat_map(|line| &line.1)
            .map(|span| span.1.1.len())
            .sum();
        let mut table = Self {
            x0: Vec::with_capacity(rows),
            y0: Vec::with_capacity(rows),
            x1: Vec::with_capacity(rows),
            y1: Vec::with_capacity(rows),
            origin_x: Vec::with_capacity(rows),
            origin_y: Vec::with_capacity(rows),
            text_offsets: Vec::with_capacity(rows + 1),
            text_data: Vec::new(),
            block: Vec::with_capacity(rows),
            line: Vec::with_capacity(rows),
            span: Vec::with_capacity(rows),
            is_synthetic: Vec::with_capacity(rows),
            stream: Vec::with_capacity(rows),
            operation: Vec::with_capacity(rows),
            synthetic_bits: vec![0; rows.div_ceil(8)],
            source_bits: vec![0; rows.div_ceil(8)],
        };
        table.text_offsets.push(0);
        for (block_index, (_, lines)) in blocks.iter().enumerate() {
            for (line_index, line) in lines.iter().enumerate() {
                for (span_index, span) in line.1.iter().enumerate() {
                    for (text, (x0, y0, x1, y1), (x, y), synthetic, source) in &span.1.1 {
                        let row = table.x0.len();
                        table.x0.push(*x0);
                        table.y0.push(*y0);
                        table.x1.push(*x1);
                        table.y1.push(*y1);
                        table.origin_x.push(*x);
                        table.origin_y.push(*y);
                        table.text_data.extend_from_slice(text.as_bytes());
                        table
                            .text_offsets
                            .push(i32::try_from(table.text_data.len()).map_err(|_| {
                                PyValueError::new_err("the text of the chars exceeds 2 GiB")
                            })?);
                        table.block.push(index(block_index)?);
                        table.line.push(index(line_index)?);
                        table.span.push(index(span_index)?);
                        table.is_synthetic.push(u8::from(*synthetic));
                        if *synthetic {
                            set_bit(&mut table.synthetic_bits, row);
                        }
                        let (stream, operation) = source.unwrap_or_default();
                        table.stream.push(stream);
                        table.operation.push(operation);
                        if source.is_some() {
                            set_bit(&mut table.source_bits, row);
                        }
                    }
                }
            }
        }
        Ok(table)
    }

    fn len(&self) -> usize {
        self.x0.len()
    }

    /// Returns the text of `row`.
    fn text(&self, row: usize) -> &str {
        let (start, end) = (self.text_offsets[row], self.text_offsets[row + 1]);
        let bytes = &self.text_data[start as usize..end as usize];
        // Each row's bytes are one `String` of the layout.
        std::str::from_utf8(bytes).unwrap_or_default()
    }

    fn row(&self, row: usize) -> RowTuple<'_> {
        (
            self.text(row),
            (self.x0[row], self.y0[row], self.x1[row], self.y1[row]),
            (self.origin_x[row], self.origin_y[row]),
            self.is_synthetic[row] != 0,
            (self.stream[row] != 0).then(|| (self.stream[row], self.operation[row])),
        )
    }

    /// Returns the bytes of the column `name` for the rows `start..stop`,
    /// with its item format: `text_offsets` holds one offset more, and
    /// `text_data` is the whole column, which the offsets index.
    fn column(&self, name: &str, start: usize, stop: usize) -> Option<(&[u8], &'static CStr)> {
        fn bytes<T>(items: &[T]) -> &[u8] {
            // SAFETY: the column types are plain numbers without padding, so
            // every byte of the slice is initialized.
            unsafe { std::slice::from_raw_parts(items.as_ptr().cast(), size_of_val(items)) }
        }
        Some(match name {
            "x0" => (bytes(&self.x0[start..stop]), c"d"),
            "y0" => (bytes(&self.y0[start..stop]), c"d"),
            "x1" => (bytes(&self.x1[start..stop]), c"d"),
            "y1" => (bytes(&self.y1[start..stop]), c"d"),
            "origin_x" => (bytes(&self.origin_x[start..stop]), c"d"),
            "origin_y" => (bytes(&self.origin_y[start..stop]), c"d"),
            "text_offsets" => (bytes(&self.text_offsets[start..=stop]), c"i"),
            "text_data" => (&self.text_data[..], c"B"),
            "block" => (bytes(&self.block[start..stop]), c"I"),
            "line" => (bytes(&self.line[start..stop]), c"I"),
            "span" => (bytes(&self.span[start..stop]), c"I"),
            "is_synthetic" => (&self.is_synthetic[start..stop], c"B"),
            "stream" => (bytes(&self.stream[start..stop]), c"I"),
            "operation" => (bytes(&self.operation[start..stop]), c"I"),
            _ => return None,
        })
    }
}

/// The columns of the chars of a layout, which `Chars` views.
#[pyclass(frozen, module = "pyropdf._core")]
pub struct CharColumns {
    table: Arc<Table>,
}

impl CharColumns {
    /// Returns the columns of the chars of `blocks`, a layout built with
    /// chars.
    pub(crate) fn new(blocks: &[BlockTuple]) -> PyResult<Self> {
        Ok(Self {
            table: Arc::new(Table::new(blocks)?),
        })
    }

    /// Checks that `start..stop` is a range of rows.
    fn rows_of(&self, start: usize, stop: usize) -> PyResult<()> {
        if start <= stop && stop <= self.table.len() {
            Ok(())
        } else {
            Err(PyIndexError::new_err("the rows are out of range"))
        }
    }
}

#[pymethods]
impl CharColumns {
    fn __len__(&self) -> usize {
        self.table.len()
    }

    /// Returns the rows `start..stop` as the tuples that `Char` records are
    /// built from.
    fn rows(&self, start: usize, stop: usize) -> PyResult<Vec<RowTuple<'_>>> {
        self.rows_of(start, stop)?;
        Ok((start..stop).map(|row| self.table.row(row)).collect())
    }

    /// Returns the column `name` for the rows `start..stop`, which
    /// `memoryview` reads without a copy.
    fn column(&self, name: &str, start: usize, stop: usize) -> PyResult<Column> {
        self.rows_of(start, stop)?;
        let Some(at) = COLUMNS.iter().position(|column| *column == name) else {
            return Err(PyValueError::new_err(format!(
                "Chars has no column {name:?}"
            )));
        };
        Ok(Column {
            table: Arc::clone(&self.table),
            name: COLUMNS[at],
            start,
            stop,
        })
    }

    /// Returns the Arrow schema of the rows as a PyCapsule.
    fn arrow_schema<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyCapsule>> {
        schema_capsule(py)
    }

    /// Returns the rows `start..stop` as the PyCapsules of an Arrow schema
    /// and an Arrow struct array, whose buffers are the columns.
    fn arrow_array<'py>(
        &self,
        py: Python<'py>,
        start: usize,
        stop: usize,
    ) -> PyResult<(Bound<'py, PyCapsule>, Bound<'py, PyCapsule>)> {
        self.rows_of(start, stop)?;
        let array = struct_array(&self.table, start, stop)?;
        let array = PyCapsule::new_with_value_and_destructor(
            py,
            Exported(array),
            c"arrow_array",
            |Exported(mut array), _| {
                if let Some(release) = array.release {
                    // SAFETY: a consumer that moved the array out set its
                    // release to null; otherwise this capsule still owns it.
                    unsafe { release(&raw mut array) };
                }
            },
        )?;
        Ok((schema_capsule(py)?, array))
    }
}

/// One column of [`CharColumns`] for some of its rows, which exposes its
/// items through the buffer protocol, read-only.
#[pyclass(frozen, module = "pyropdf._core")]
pub struct Column {
    table: Arc<Table>,
    name: &'static str,
    start: usize,
    stop: usize,
}

#[pymethods]
impl Column {
    /// Exposes the items without a copy, read-only and one-dimensional.
    unsafe fn __getbuffer__(
        slf: Bound<'_, Self>,
        view: *mut ffi::Py_buffer,
        flags: c_int,
    ) -> PyResult<()> {
        if view.is_null() {
            return Err(PyBufferError::new_err("buffer view is null"));
        }
        if (flags & ffi::PyBUF_WRITABLE) == ffi::PyBUF_WRITABLE {
            return Err(PyBufferError::new_err("Chars columns are read-only"));
        }
        let owner = slf.clone().into_any();
        let column = slf.get();
        let (bytes, format) = column
            .table
            .column(column.name, column.start, column.stop)
            .ok_or_else(|| PyBufferError::new_err("the column is unknown"))?;
        let itemsize: isize = match format.to_bytes() {
            b"d" => 8,
            b"i" | b"I" => 4,
            _ => 1,
        };
        let length = isize::try_from(bytes.len())
            .map_err(|_| PyBufferError::new_err("the column is too large"))?;
        // The shape lives in one allocation that `internal` owns until
        // `__releasebuffer__`; the strides are those of a contiguous view.
        let mut shape = Box::new([length / itemsize, itemsize]);
        let shape_ptr = shape.as_mut_ptr();
        let nd = (flags & ffi::PyBUF_ND) == ffi::PyBUF_ND;
        let strides = (flags & ffi::PyBUF_STRIDES) == ffi::PyBUF_STRIDES;
        // SAFETY: `view` was checked for null. The table never changes
        // after it is built, and the owner reference keeps it alive. The
        // shape and strides point into the boxed array that `internal` owns.
        unsafe {
            (*view).obj = owner.into_ptr();
            (*view).buf = bytes.as_ptr().cast_mut().cast::<c_void>();
            (*view).len = length;
            (*view).readonly = 1;
            (*view).itemsize = itemsize;
            (*view).format = if (flags & ffi::PyBUF_FORMAT) == ffi::PyBUF_FORMAT {
                format.as_ptr().cast_mut()
            } else {
                ptr::null_mut()
            };
            (*view).ndim = 1;
            (*view).shape = if nd { shape_ptr } else { ptr::null_mut() };
            (*view).strides = if nd && strides {
                shape_ptr.add(1)
            } else {
                ptr::null_mut()
            };
            (*view).suboffsets = ptr::null_mut();
            (*view).internal = Box::into_raw(shape).cast::<c_void>();
        }
        Ok(())
    }

    /// Releases the shape of one buffer view.
    unsafe fn __releasebuffer__(&self, view: *mut ffi::Py_buffer) {
        if view.is_null() {
            return;
        }
        // SAFETY: `__getbuffer__` stored a boxed `[isize; 2]` in `internal`
        // once for this view; the format is static.
        unsafe {
            let internal = (*view).internal;
            if !internal.is_null() {
                drop(Box::from_raw(internal.cast::<[isize; 2]>()));
            }
        }
    }
}

/// The Arrow C data interface's schema.
#[repr(C)]
struct ArrowSchema {
    format: *const c_char,
    name: *const c_char,
    metadata: *const c_char,
    flags: i64,
    n_children: i64,
    children: *mut *mut ArrowSchema,
    dictionary: *mut ArrowSchema,
    release: Option<unsafe extern "C" fn(*mut ArrowSchema)>,
    private_data: *mut c_void,
}

/// The Arrow C data interface's array.
#[repr(C)]
struct ArrowArray {
    length: i64,
    null_count: i64,
    offset: i64,
    n_buffers: i64,
    n_children: i64,
    buffers: *mut *const c_void,
    children: *mut *mut ArrowArray,
    dictionary: *mut ArrowArray,
    release: Option<unsafe extern "C" fn(*mut ArrowArray)>,
    private_data: *mut c_void,
}

/// An Arrow structure that a capsule owns until a consumer moves it out.
struct Exported<T>(T);

// SAFETY: the structure's pointers lead only to allocations that its own
// release frees and to the immutable columns of an `Arc<Table>`, so any
// thread may hold and release it, as the Arrow C data interface requires.
unsafe impl<T> Send for Exported<T> {}

/// The field that is nullable, from the Arrow C data interface.
const ARROW_FLAG_NULLABLE: i64 = 2;

/// The fields of the Arrow struct: `(name, format, nullable)`.
const FIELDS: [(&CStr, &CStr, bool); 13] = [
    (c"x0", c"g", false),
    (c"y0", c"g", false),
    (c"x1", c"g", false),
    (c"y1", c"g", false),
    (c"origin_x", c"g", false),
    (c"origin_y", c"g", false),
    (c"text", c"u", false),
    (c"block", c"I", false),
    (c"line", c"I", false),
    (c"span", c"I", false),
    (c"is_synthetic", c"b", false),
    (c"stream", c"I", true),
    (c"operation", c"I", true),
];

/// What a schema's release frees: its children, which own nothing.
struct SchemaChildren(Box<[*mut ArrowSchema]>);

/// Releases a field of the struct schema, whose strings are static.
unsafe extern "C" fn release_field(schema: *mut ArrowSchema) {
    // SAFETY: the consumer passes a schema that this module built.
    unsafe { (*schema).release = None };
}

/// Releases the struct schema and its fields.
unsafe extern "C" fn release_schema(schema: *mut ArrowSchema) {
    // SAFETY: the consumer passes the schema that `schema()` built, whose
    // private data is its boxed children, each a boxed field.
    unsafe {
        let children = Box::from_raw((*schema).private_data.cast::<SchemaChildren>());
        for &child in &children.0 {
            if let Some(release) = (*child).release {
                release(child);
            }
            drop(Box::from_raw(child));
        }
        (*schema).release = None;
    }
}

/// Returns the Arrow schema of the chars: a struct of [`FIELDS`].
fn schema() -> ArrowSchema {
    let children: Box<[*mut ArrowSchema]> = FIELDS
        .iter()
        .map(|&(name, format, nullable)| {
            Box::into_raw(Box::new(ArrowSchema {
                format: format.as_ptr(),
                name: name.as_ptr(),
                metadata: ptr::null(),
                flags: if nullable { ARROW_FLAG_NULLABLE } else { 0 },
                n_children: 0,
                children: ptr::null_mut(),
                dictionary: ptr::null_mut(),
                release: Some(release_field),
                private_data: ptr::null_mut(),
            }))
        })
        .collect();
    let mut children = Box::new(SchemaChildren(children));
    ArrowSchema {
        format: c"+s".as_ptr(),
        name: c"".as_ptr(),
        metadata: ptr::null(),
        flags: 0,
        n_children: FIELDS.len() as i64,
        children: children.0.as_mut_ptr(),
        dictionary: ptr::null_mut(),
        release: Some(release_schema),
        private_data: Box::into_raw(children).cast::<c_void>(),
    }
}

fn schema_capsule(py: Python<'_>) -> PyResult<Bound<'_, PyCapsule>> {
    PyCapsule::new_with_value_and_destructor(
        py,
        Exported(schema()),
        c"arrow_schema",
        |Exported(mut schema), _| {
            if let Some(release) = schema.release {
                // SAFETY: as for the array in `CharColumns::arrow_array`.
                unsafe { release(&raw mut schema) };
            }
        },
    )
}

/// What an array's release frees: its buffer pointers and children, and the
/// table that the buffers point into.
struct ArrayData {
    _table: Arc<Table>,
    _buffers: Box<[*const c_void]>,
    children: Box<[*mut ArrowArray]>,
}

/// Releases an array and its children.
unsafe extern "C" fn release_array(array: *mut ArrowArray) {
    // SAFETY: the consumer passes an array that `struct_array` built, whose
    // private data is its boxed `ArrayData`, with each child boxed.
    unsafe {
        let data = Box::from_raw((*array).private_data.cast::<ArrayData>());
        for &child in &data.children {
            if let Some(release) = (*child).release {
                release(child);
            }
            drop(Box::from_raw(child));
        }
        (*array).release = None;
    }
}

/// Returns an array of `length` rows from `offset`, with `buffers` and
/// `children`, that keeps `table` alive until it is released.
fn array(
    table: &Arc<Table>,
    offset: usize,
    length: usize,
    null_count: usize,
    buffers: Box<[*const c_void]>,
    children: Box<[*mut ArrowArray]>,
) -> ArrowArray {
    let mut data = Box::new(ArrayData {
        _table: Arc::clone(table),
        _buffers: buffers,
        children,
    });
    ArrowArray {
        length: length as i64,
        null_count: null_count as i64,
        offset: offset as i64,
        n_buffers: data._buffers.len() as i64,
        n_children: data.children.len() as i64,
        buffers: data._buffers.as_mut_ptr(),
        children: data.children.as_mut_ptr(),
        dictionary: ptr::null_mut(),
        release: Some(release_array),
        private_data: Box::into_raw(data).cast::<c_void>(),
    }
}

/// Returns the rows `start..stop` of `table` as an Arrow struct array of
/// [`FIELDS`], whose children read the columns from `start`.
fn struct_array(table: &Arc<Table>, start: usize, stop: usize) -> PyResult<ArrowArray> {
    let length = stop - start;
    let nulls = table.stream[start..stop]
        .iter()
        .filter(|&&stream| stream == 0)
        .count();
    let data =
        |pointer: *const u8| -> Box<[*const c_void]> { Box::new([ptr::null(), pointer.cast()]) };
    let buffers: [Box<[*const c_void]>; 13] = [
        data(table.x0.as_ptr().cast()),
        data(table.y0.as_ptr().cast()),
        data(table.x1.as_ptr().cast()),
        data(table.y1.as_ptr().cast()),
        data(table.origin_x.as_ptr().cast()),
        data(table.origin_y.as_ptr().cast()),
        Box::new([
            ptr::null(),
            table.text_offsets.as_ptr().cast(),
            table.text_data.as_ptr().cast(),
        ]),
        data(table.block.as_ptr().cast()),
        data(table.line.as_ptr().cast()),
        data(table.span.as_ptr().cast()),
        data(table.synthetic_bits.as_ptr()),
        Box::new([
            table.source_bits.as_ptr().cast(),
            table.stream.as_ptr().cast(),
        ]),
        Box::new([
            table.source_bits.as_ptr().cast(),
            table.operation.as_ptr().cast(),
        ]),
    ];
    let children: Box<[*mut ArrowArray]> = buffers
        .into_iter()
        .zip(FIELDS)
        .map(|(buffers, (_, _, nullable))| {
            let null_count = if nullable { nulls } else { 0 };
            let child = array(table, start, length, null_count, buffers, Box::new([]));
            Box::into_raw(Box::new(child))
        })
        .collect();
    Ok(array(
        table,
        0,
        length,
        0,
        Box::new([ptr::null()]),
        children,
    ))
}
