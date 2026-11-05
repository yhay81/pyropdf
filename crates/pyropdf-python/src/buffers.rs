//! `Layout.chars`, `page.chars`, and `page.paths`: the characters of a
//! layout and the paths of a page's content as columns, one buffer per
//! field, that Python reads through the buffer protocol and the Arrow
//! PyCapsule interface without an object per glyph, path, or segment
//! (DESIGN.md section 10).

use std::any::Any;
use std::ffi::{CStr, c_char, c_int, c_void};
use std::ptr;
use std::sync::Arc;

use pyo3::exceptions::{PyBufferError, PyIndexError, PyValueError};
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::PyCapsule;
use pyropdf_engine::extract::{
    BBox, BlockTuple, ClipTuple, Contexts, GroupTuple, MarkTuple, PageContent, PaintColumns,
    PaintTuple, PathTable, StateTuple,
};

/// A table whose columns a [`Column`] exposes.
trait Columns: Send + Sync {
    /// Returns the bytes of the column `name` for the rows `start..stop`,
    /// with its item format.
    fn column(&self, name: &str, start: usize, stop: usize) -> Option<(&[u8], &'static CStr)>;
}

/// Returns the bytes of a column of plain numbers.
fn bytes<T>(items: &[T]) -> &[u8] {
    // SAFETY: the column types are plain numbers without padding, so every
    // byte of the slice is initialized.
    unsafe { std::slice::from_raw_parts(items.as_ptr().cast(), size_of_val(items)) }
}

/// Sets bit `row` of `bits`, which holds a byte per eight rows.
fn set_bit(bits: &mut [u8], row: usize) {
    bits[row / 8] |= 1 << (row % 8);
}

/// Returns how many of the bits `start..stop` of `bits` are clear: the null
/// rows of an Arrow validity bitmap.
fn clear_bits(bits: &[u8], start: usize, stop: usize) -> usize {
    (start..stop)
        .filter(|&row| bits[row / 8] >> (row % 8) & 1 == 0)
        .count()
}

/// One row as `python/pyropdf/_text.py` builds a `Char` from it: `(text,
/// bbox, origin, synthetic, (stream, operation) or None)`.
type RowTuple<'a> = (&'a str, BBox, (f64, f64), bool, Option<(u32, u32)>);

/// The clip, group, mark, and graphics-state rows shared by a path table.
type ContextRows<'a> = (
    &'a [ClipTuple],
    &'a [GroupTuple],
    &'a [MarkTuple],
    &'a [StateTuple],
);

/// The characters of a layout, one row per char in layout order, each field
/// in its own column.
struct CharTable {
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

impl CharTable {
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
}

impl Columns for CharTable {
    /// Returns the bytes of the column `name` for the rows `start..stop`,
    /// with its item format: `text_offsets` holds one offset more, and
    /// `text_data` is the whole column, which the offsets index.
    fn column(&self, name: &str, start: usize, stop: usize) -> Option<(&[u8], &'static CStr)> {
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
    table: Arc<CharTable>,
}

impl CharColumns {
    /// Returns the columns of the chars of `blocks`, a layout built with
    /// chars.
    pub(crate) fn new(blocks: &[BlockTuple]) -> PyResult<Self> {
        Ok(Self {
            table: Arc::new(CharTable::new(blocks)?),
        })
    }
}

/// Checks that `start..stop` is a range of a table's `rows`.
fn rows_of(start: usize, stop: usize, rows: usize) -> PyResult<()> {
    if start <= stop && stop <= rows {
        Ok(())
    } else {
        Err(PyIndexError::new_err("the rows are out of range"))
    }
}

#[pymethods]
impl CharColumns {
    fn __len__(&self) -> PyResult<usize> {
        crate::errors::boundary(|| self.table.len())
    }

    /// Returns the rows `start..stop` as the tuples that `Char` records are
    /// built from.
    fn rows(&self, start: usize, stop: usize) -> PyResult<Vec<RowTuple<'_>>> {
        crate::errors::boundary(|| {
            rows_of(start, stop, self.table.len())?;
            Ok((start..stop).map(|row| self.table.row(row)).collect())
        })?
    }

    /// Returns the column `name` for the rows `start..stop`, which
    /// `memoryview` reads without a copy.
    fn column(&self, name: &str, start: usize, stop: usize) -> PyResult<Column> {
        crate::errors::boundary(|| {
            rows_of(start, stop, self.table.len())?;
            let Some(at) = COLUMNS.iter().position(|column| *column == name) else {
                return Err(PyValueError::new_err(format!(
                    "Chars has no column {name:?}"
                )));
            };
            Ok(Column {
                table: Arc::clone(&self.table) as Arc<dyn Columns>,
                name: COLUMNS[at],
                start,
                stop,
            })
        })?
    }

    /// Returns the Arrow schema of the rows as a PyCapsule.
    fn arrow_schema<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyCapsule>> {
        crate::errors::boundary(|| schema_capsule(py, &CHAR_STRUCT))?
    }

    /// Returns the rows `start..stop` as the PyCapsules of an Arrow schema
    /// and an Arrow struct array, whose buffers are the columns.
    fn arrow_array<'py>(
        &self,
        py: Python<'py>,
        start: usize,
        stop: usize,
    ) -> PyResult<(Bound<'py, PyCapsule>, Bound<'py, PyCapsule>)> {
        crate::errors::boundary(|| {
            rows_of(start, stop, self.table.len())?;
            let array = char_array(&self.table, start, stop);
            Ok((schema_capsule(py, &CHAR_STRUCT)?, array_capsule(py, array)?))
        })?
    }
}

/// One path as `python/pyropdf/_content.py` builds a `Path` from it:
/// `(verbs, coordinates, bbox, fill, even_odd, stroke, context, blend mode,
/// soft mask)`, with the verbs, the coordinates, and the type of the soft
/// mask in the forms of the content model.
type PathRow<'a> = (
    &'a [u8],
    Vec<f64>,
    BBox,
    Option<PaintRow>,
    bool,
    Option<StrokeRow<'a>>,
    u32,
    u8,
    Option<u8>,
);

/// A paint of a [`PathRow`]: its index in the patterns, or -1 for a color,
/// and the color's sRGB components and alpha.
type PaintRow = (i32, (f64, f64, f64, f64));

/// A stroke of a [`PathRow`]: paint, width, cap, join, miter limit, dash
/// lengths, and dash offset.
type StrokeRow<'a> = (PaintRow, f64, u8, u8, f64, &'a [f64], f64);

/// Returns the paint of `row` in the form of a [`PathRow`].
fn paint_row(paint: &PaintColumns, row: usize) -> PaintRow {
    (
        paint.pattern[row],
        (
            f64::from(paint.red[row]),
            f64::from(paint.green[row]),
            f64::from(paint.blue[row]),
            f64::from(paint.alpha[row]),
        ),
    )
}

/// Returns `row` of `table` in the tuple form that `Path` records are built
/// from.
fn path_row(table: &PathTable, row: usize) -> PathRow<'_> {
    (
        &table.verb[table.segments_of(row)],
        table.coordinates(row),
        (table.x0[row], table.y0[row], table.x1[row], table.y1[row]),
        (table.fill.present[row] != 0).then(|| paint_row(&table.fill, row)),
        table.fill_rule[row] != 0,
        (table.stroke.present[row] != 0).then(|| {
            (
                paint_row(&table.stroke, row),
                table.stroke_width[row],
                table.stroke_cap[row],
                table.stroke_join[row],
                table.stroke_miter_limit[row],
                &table.stroke_dashes[table.dashes_of(row)],
                table.stroke_dash_offset[row],
            )
        }),
        table.context[row],
        table.blend_mode[row],
        u8::try_from(table.soft_mask[row]).ok(),
    )
}

/// The columns that `Paths.column` names, in the order of `Paths.names`.
const PATH_COLUMNS: [&str; 36] = [
    "x0",
    "y0",
    "x1",
    "y1",
    "segment_offsets",
    "path",
    "verb",
    "to_x",
    "to_y",
    "c1_x",
    "c1_y",
    "c2_x",
    "c2_y",
    "has_fill",
    "fill_red",
    "fill_green",
    "fill_blue",
    "fill_alpha",
    "fill_pattern",
    "fill_rule",
    "has_stroke",
    "stroke_red",
    "stroke_green",
    "stroke_blue",
    "stroke_alpha",
    "stroke_pattern",
    "stroke_width",
    "stroke_cap",
    "stroke_join",
    "stroke_miter_limit",
    "stroke_dashes_offsets",
    "stroke_dashes",
    "stroke_dash_offset",
    "context",
    "blend_mode",
    "soft_mask",
];

impl Columns for PathTable {
    /// Returns the bytes of the column `name` for the rows `start..stop`,
    /// with its item format: the offsets hold one more, and the segment
    /// columns and `stroke_dashes` are whole, as the offsets index them.
    fn column(&self, name: &str, start: usize, stop: usize) -> Option<(&[u8], &'static CStr)> {
        let rows = start..stop;
        Some(match name {
            "x0" => (bytes(&self.x0[rows]), c"d"),
            "y0" => (bytes(&self.y0[rows]), c"d"),
            "x1" => (bytes(&self.x1[rows]), c"d"),
            "y1" => (bytes(&self.y1[rows]), c"d"),
            "segment_offsets" => (bytes(&self.segment_offsets[start..=stop]), c"i"),
            "path" => (bytes(&self.path), c"I"),
            "verb" => (&self.verb[..], c"B"),
            "to_x" => (bytes(&self.to_x), c"d"),
            "to_y" => (bytes(&self.to_y), c"d"),
            "c1_x" => (bytes(&self.c1_x), c"d"),
            "c1_y" => (bytes(&self.c1_y), c"d"),
            "c2_x" => (bytes(&self.c2_x), c"d"),
            "c2_y" => (bytes(&self.c2_y), c"d"),
            "has_fill" => (&self.fill.present[rows], c"B"),
            "fill_red" => (bytes(&self.fill.red[rows]), c"f"),
            "fill_green" => (bytes(&self.fill.green[rows]), c"f"),
            "fill_blue" => (bytes(&self.fill.blue[rows]), c"f"),
            "fill_alpha" => (bytes(&self.fill.alpha[rows]), c"f"),
            "fill_pattern" => (bytes(&self.fill.pattern[rows]), c"i"),
            "fill_rule" => (&self.fill_rule[rows], c"B"),
            "has_stroke" => (&self.stroke.present[rows], c"B"),
            "stroke_red" => (bytes(&self.stroke.red[rows]), c"f"),
            "stroke_green" => (bytes(&self.stroke.green[rows]), c"f"),
            "stroke_blue" => (bytes(&self.stroke.blue[rows]), c"f"),
            "stroke_alpha" => (bytes(&self.stroke.alpha[rows]), c"f"),
            "stroke_pattern" => (bytes(&self.stroke.pattern[rows]), c"i"),
            "stroke_width" => (bytes(&self.stroke_width[rows]), c"d"),
            "stroke_cap" => (&self.stroke_cap[rows], c"B"),
            "stroke_join" => (&self.stroke_join[rows], c"B"),
            "stroke_miter_limit" => (bytes(&self.stroke_miter_limit[rows]), c"d"),
            "stroke_dashes_offsets" => (bytes(&self.stroke_dashes_offsets[start..=stop]), c"i"),
            "stroke_dashes" => (bytes(&self.stroke_dashes), c"d"),
            "stroke_dash_offset" => (bytes(&self.stroke_dash_offset[rows]), c"d"),
            "context" => (bytes(&self.context[rows]), c"I"),
            "blend_mode" => (&self.blend_mode[rows], c"B"),
            "soft_mask" => (bytes(&self.soft_mask[rows]), c"b"),
            _ => return None,
        })
    }
}

/// The columns of the paths of a page's content, which `Paths` views: the
/// buffers that the content holds, without a copy.
#[pyclass(frozen, module = "pyropdf._core")]
pub struct PathColumns {
    table: Arc<PathTable>,
    contexts: Arc<Contexts>,
}

impl PathColumns {
    /// Returns the columns of the paths of `content`.
    pub(crate) fn new(content: &PageContent) -> Self {
        Self {
            table: Arc::clone(&content.paths),
            contexts: Arc::clone(&content.contexts),
        }
    }
}

#[pymethods]
impl PathColumns {
    fn __len__(&self) -> PyResult<usize> {
        crate::errors::boundary(|| self.table.rows())
    }

    /// Returns the rows `start..stop` as the tuples that `Path` records are
    /// built from.
    fn rows(&self, start: usize, stop: usize) -> PyResult<Vec<PathRow<'_>>> {
        crate::errors::boundary(|| {
            rows_of(start, stop, self.table.rows())?;
            Ok((start..stop)
                .map(|row| path_row(&self.table, row))
                .collect())
        })?
    }

    /// Returns the column `name` for the rows `start..stop`, which
    /// `memoryview` reads without a copy.
    fn column(&self, name: &str, start: usize, stop: usize) -> PyResult<Column> {
        crate::errors::boundary(|| {
            rows_of(start, stop, self.table.rows())?;
            let Some(at) = PATH_COLUMNS.iter().position(|column| *column == name) else {
                return Err(PyValueError::new_err(format!(
                    "Paths has no column {name:?}"
                )));
            };
            Ok(Column {
                table: Arc::clone(&self.table) as Arc<dyn Columns>,
                name: PATH_COLUMNS[at],
                start,
                stop,
            })
        })?
    }

    /// Returns the graphics contexts that the `context` column indexes, as
    /// `(clips, groups, marks, states)` in the tuple forms of `page_content`.
    fn contexts(&self) -> PyResult<ContextRows<'_>> {
        crate::errors::boundary(|| {
            let contexts = &*self.contexts;
            (
                contexts.clips.as_slice(),
                contexts.groups.as_slice(),
                contexts.marks.as_slice(),
                contexts.states.as_slice(),
            )
        })
    }

    /// Returns the pattern paints that the pattern columns index, in the
    /// tuple form of `page_content`.
    fn patterns(&self) -> PyResult<&[PaintTuple]> {
        crate::errors::boundary(|| self.table.patterns.as_slice())
    }

    /// Returns the Arrow schema of the rows as a PyCapsule.
    fn arrow_schema<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyCapsule>> {
        crate::errors::boundary(|| schema_capsule(py, &PATH_STRUCT))?
    }

    /// Returns the rows `start..stop` as the PyCapsules of an Arrow schema
    /// and an Arrow struct array, whose buffers are the columns.
    fn arrow_array<'py>(
        &self,
        py: Python<'py>,
        start: usize,
        stop: usize,
    ) -> PyResult<(Bound<'py, PyCapsule>, Bound<'py, PyCapsule>)> {
        crate::errors::boundary(|| {
            rows_of(start, stop, self.table.rows())?;
            let array = path_array(&self.table, start, stop);
            Ok((schema_capsule(py, &PATH_STRUCT)?, array_capsule(py, array)?))
        })?
    }
}

/// One column of [`CharColumns`] or [`PathColumns`] for some of its rows,
/// which exposes its items through the buffer protocol, read-only.
#[pyclass(frozen, module = "pyropdf._core")]
pub struct Column {
    table: Arc<dyn Columns>,
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
        crate::errors::boundary(|| {
            if view.is_null() {
                return Err(PyBufferError::new_err("buffer view is null"));
            }
            if (flags & ffi::PyBUF_WRITABLE) == ffi::PyBUF_WRITABLE {
                return Err(PyBufferError::new_err("the columns are read-only"));
            }
            let owner = slf.clone().into_any();
            let column = slf.get();
            let (bytes, format) = column
                .table
                .column(column.name, column.start, column.stop)
                .ok_or_else(|| PyBufferError::new_err("the column is unknown"))?;
            let itemsize: isize = match format.to_bytes() {
                b"d" => 8,
                b"f" | b"i" | b"I" => 4,
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
        })?
    }

    /// Releases the shape of one buffer view.
    unsafe fn __releasebuffer__(&self, view: *mut ffi::Py_buffer) -> PyResult<()> {
        crate::errors::boundary(|| {
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
        })
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
// release frees and to the immutable columns of the table that it keeps
// alive, so any thread may hold and release it, as the Arrow C data
// interface requires.
unsafe impl<T> Send for Exported<T> {}

/// The field that is nullable, from the Arrow C data interface.
const ARROW_FLAG_NULLABLE: i64 = 2;

/// An Arrow field: its name, format, whether it is nullable, and the fields
/// of a struct's children or a list's item.
struct Field {
    name: &'static CStr,
    format: &'static CStr,
    nullable: bool,
    children: &'static [Field],
}

impl Field {
    /// A field of a primitive type.
    const fn leaf(name: &'static CStr, format: &'static CStr, nullable: bool) -> Self {
        Self {
            name,
            format,
            nullable,
            children: &[],
        }
    }
}

/// The struct of the chars, whose fields are the columns, with one `text`
/// field for `text_offsets` and `text_data`.
const CHAR_STRUCT: Field = Field {
    name: c"",
    format: c"+s",
    nullable: false,
    children: &[
        Field::leaf(c"x0", c"g", false),
        Field::leaf(c"y0", c"g", false),
        Field::leaf(c"x1", c"g", false),
        Field::leaf(c"y1", c"g", false),
        Field::leaf(c"origin_x", c"g", false),
        Field::leaf(c"origin_y", c"g", false),
        Field::leaf(c"text", c"u", false),
        Field::leaf(c"block", c"I", false),
        Field::leaf(c"line", c"I", false),
        Field::leaf(c"span", c"I", false),
        Field::leaf(c"is_synthetic", c"b", false),
        Field::leaf(c"stream", c"I", true),
        Field::leaf(c"operation", c"I", true),
    ],
};

/// The struct of the paths, whose fields are the per-path columns, with a
/// `segments` list for `segment_offsets` and the segment columns, and a
/// `stroke_dashes` list for `stroke_dashes_offsets` and `stroke_dashes`.
const PATH_STRUCT: Field = Field {
    name: c"",
    format: c"+s",
    nullable: false,
    children: &[
        Field::leaf(c"x0", c"g", false),
        Field::leaf(c"y0", c"g", false),
        Field::leaf(c"x1", c"g", false),
        Field::leaf(c"y1", c"g", false),
        Field {
            name: c"segments",
            format: c"+l",
            nullable: false,
            children: &[Field {
                name: c"item",
                format: c"+s",
                nullable: false,
                children: &[
                    Field::leaf(c"path", c"I", false),
                    Field::leaf(c"verb", c"C", false),
                    Field::leaf(c"to_x", c"g", false),
                    Field::leaf(c"to_y", c"g", false),
                    Field::leaf(c"c1_x", c"g", true),
                    Field::leaf(c"c1_y", c"g", true),
                    Field::leaf(c"c2_x", c"g", true),
                    Field::leaf(c"c2_y", c"g", true),
                ],
            }],
        },
        Field::leaf(c"has_fill", c"b", false),
        Field::leaf(c"fill_red", c"f", true),
        Field::leaf(c"fill_green", c"f", true),
        Field::leaf(c"fill_blue", c"f", true),
        Field::leaf(c"fill_alpha", c"f", true),
        Field::leaf(c"fill_pattern", c"i", true),
        Field::leaf(c"fill_rule", c"C", false),
        Field::leaf(c"has_stroke", c"b", false),
        Field::leaf(c"stroke_red", c"f", true),
        Field::leaf(c"stroke_green", c"f", true),
        Field::leaf(c"stroke_blue", c"f", true),
        Field::leaf(c"stroke_alpha", c"f", true),
        Field::leaf(c"stroke_pattern", c"i", true),
        Field::leaf(c"stroke_width", c"g", true),
        Field::leaf(c"stroke_cap", c"C", true),
        Field::leaf(c"stroke_join", c"C", true),
        Field::leaf(c"stroke_miter_limit", c"g", true),
        Field {
            name: c"stroke_dashes",
            format: c"+l",
            nullable: true,
            children: &[Field::leaf(c"item", c"g", false)],
        },
        Field::leaf(c"stroke_dash_offset", c"g", true),
        Field::leaf(c"context", c"I", false),
        Field::leaf(c"blend_mode", c"C", false),
        Field::leaf(c"soft_mask", c"c", true),
    ],
};

/// What a schema's release frees: its children.
struct SchemaChildren(Box<[*mut ArrowSchema]>);

/// Releases a schema that [`schema`] built, and its children.
unsafe extern "C" fn release_schema(schema: *mut ArrowSchema) {
    // SAFETY: the consumer passes a schema that `schema()` built, whose
    // private data is null or its boxed children, each a boxed schema.
    unsafe {
        let children = (*schema).private_data;
        if !children.is_null() {
            let children = Box::from_raw(children.cast::<SchemaChildren>());
            for &child in &children.0 {
                if let Some(release) = (*child).release {
                    release(child);
                }
                drop(Box::from_raw(child));
            }
        }
        (*schema).release = None;
    }
}

/// Returns the Arrow schema of `field` and its children. The recursion
/// follows the static fields of this module, two levels deep at most.
fn schema(field: &Field) -> ArrowSchema {
    let (children, private_data) = if field.children.is_empty() {
        (ptr::null_mut(), ptr::null_mut())
    } else {
        let children: Box<[*mut ArrowSchema]> = field
            .children
            .iter()
            .map(|child| Box::into_raw(Box::new(schema(child))))
            .collect();
        let mut children = Box::new(SchemaChildren(children));
        (
            children.0.as_mut_ptr(),
            Box::into_raw(children).cast::<c_void>(),
        )
    };
    ArrowSchema {
        format: field.format.as_ptr(),
        name: field.name.as_ptr(),
        metadata: ptr::null(),
        flags: if field.nullable {
            ARROW_FLAG_NULLABLE
        } else {
            0
        },
        n_children: field.children.len() as i64,
        children,
        dictionary: ptr::null_mut(),
        release: Some(release_schema),
        private_data,
    }
}

fn schema_capsule<'py>(py: Python<'py>, field: &Field) -> PyResult<Bound<'py, PyCapsule>> {
    PyCapsule::new_with_value_and_destructor(
        py,
        Exported(schema(field)),
        c"arrow_schema",
        |Exported(mut schema), _| {
            if let Some(release) = schema.release {
                // SAFETY: as for the array in `array_capsule`.
                unsafe { release(&raw mut schema) };
            }
        },
    )
}

fn array_capsule(py: Python<'_>, array: ArrowArray) -> PyResult<Bound<'_, PyCapsule>> {
    PyCapsule::new_with_value_and_destructor(
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
    )
}

/// The table that an exported array's buffers point into.
type Owner = Arc<dyn Any + Send + Sync>;

/// What an array's release frees: its buffer pointers and children, and the
/// table that the buffers point into.
struct ArrayData {
    _table: Owner,
    _buffers: Box<[*const c_void]>,
    children: Box<[*mut ArrowArray]>,
}

/// Releases an array and its children.
unsafe extern "C" fn release_array(array: *mut ArrowArray) {
    // SAFETY: the consumer passes an array that `array` built, whose private
    // data is its boxed `ArrayData`, with each child boxed.
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
    table: &Owner,
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
/// [`CHAR_STRUCT`], whose children read the columns from `start`.
fn char_array(table: &Arc<CharTable>, start: usize, stop: usize) -> ArrowArray {
    let owner: Owner = table.clone();
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
        .zip(CHAR_STRUCT.children)
        .map(|(buffers, field)| {
            let null_count = if field.nullable { nulls } else { 0 };
            let child = array(&owner, start, length, null_count, buffers, Box::new([]));
            Box::into_raw(Box::new(child))
        })
        .collect();
    array(&owner, 0, length, 0, Box::new([ptr::null()]), children)
}

/// Returns the rows `start..stop` of `table` as an Arrow struct array of
/// [`PATH_STRUCT`], whose children read the per-path columns from `start`.
/// The items of the two lists are the whole segment and dash columns, which
/// the lists' offsets index.
fn path_array(table: &Arc<PathTable>, start: usize, stop: usize) -> ArrowArray {
    let owner: Owner = table.clone();
    let length = stop - start;
    let boxed = |array: ArrowArray| Box::into_raw(Box::new(array));
    // A child of `length` rows from `start`, null where `valid` has a clear
    // bit.
    let row = |valid: Option<&[u8]>, data: *const c_void| {
        let (bits, nulls) = valid.map_or((ptr::null(), 0), |bits| {
            (bits.as_ptr().cast(), clear_bits(bits, start, stop))
        });
        boxed(array(
            &owner,
            start,
            length,
            nulls,
            Box::new([bits, data]),
            Box::new([]),
        ))
    };
    // A child of every segment, null where the segment is not a curve when
    // `curve` is set.
    let total = table.verb.len();
    let segment = |curve: bool, data: *const c_void| {
        let (bits, nulls) = if curve {
            (table.curve_bits.as_ptr().cast(), total - table.curves)
        } else {
            (ptr::null(), 0)
        };
        boxed(array(
            &owner,
            0,
            total,
            nulls,
            Box::new([bits, data]),
            Box::new([]),
        ))
    };
    let item = array(
        &owner,
        0,
        total,
        0,
        Box::new([ptr::null()]),
        Box::new([
            segment(false, table.path.as_ptr().cast()),
            segment(false, table.verb.as_ptr().cast()),
            segment(false, table.to_x.as_ptr().cast()),
            segment(false, table.to_y.as_ptr().cast()),
            segment(true, table.c1_x.as_ptr().cast()),
            segment(true, table.c1_y.as_ptr().cast()),
            segment(true, table.c2_x.as_ptr().cast()),
            segment(true, table.c2_y.as_ptr().cast()),
        ]),
    );
    let segments = array(
        &owner,
        start,
        length,
        0,
        Box::new([ptr::null(), table.segment_offsets.as_ptr().cast()]),
        Box::new([boxed(item)]),
    );
    let dashes = array(
        &owner,
        0,
        table.stroke_dashes.len(),
        0,
        Box::new([ptr::null(), table.stroke_dashes.as_ptr().cast()]),
        Box::new([]),
    );
    let stroked = &table.stroke.present_bits;
    let dashes = array(
        &owner,
        start,
        length,
        clear_bits(stroked, start, stop),
        Box::new([
            stroked.as_ptr().cast(),
            table.stroke_dashes_offsets.as_ptr().cast(),
        ]),
        Box::new([boxed(dashes)]),
    );
    let (fill, stroke) = (&table.fill, &table.stroke);
    let children: [*mut ArrowArray; 27] = [
        row(None, table.x0.as_ptr().cast()),
        row(None, table.y0.as_ptr().cast()),
        row(None, table.x1.as_ptr().cast()),
        row(None, table.y1.as_ptr().cast()),
        boxed(segments),
        row(None, fill.present_bits.as_ptr().cast()),
        row(Some(&fill.color_bits), fill.red.as_ptr().cast()),
        row(Some(&fill.color_bits), fill.green.as_ptr().cast()),
        row(Some(&fill.color_bits), fill.blue.as_ptr().cast()),
        row(Some(&fill.color_bits), fill.alpha.as_ptr().cast()),
        row(Some(&fill.pattern_bits), fill.pattern.as_ptr().cast()),
        row(None, table.fill_rule.as_ptr().cast()),
        row(None, stroke.present_bits.as_ptr().cast()),
        row(Some(&stroke.color_bits), stroke.red.as_ptr().cast()),
        row(Some(&stroke.color_bits), stroke.green.as_ptr().cast()),
        row(Some(&stroke.color_bits), stroke.blue.as_ptr().cast()),
        row(Some(&stroke.color_bits), stroke.alpha.as_ptr().cast()),
        row(Some(&stroke.pattern_bits), stroke.pattern.as_ptr().cast()),
        row(Some(stroked), table.stroke_width.as_ptr().cast()),
        row(Some(stroked), table.stroke_cap.as_ptr().cast()),
        row(Some(stroked), table.stroke_join.as_ptr().cast()),
        row(Some(stroked), table.stroke_miter_limit.as_ptr().cast()),
        boxed(dashes),
        row(Some(stroked), table.stroke_dash_offset.as_ptr().cast()),
        row(None, table.context.as_ptr().cast()),
        row(None, table.blend_mode.as_ptr().cast()),
        row(Some(&table.soft_mask_bits), table.soft_mask.as_ptr().cast()),
    ];
    array(
        &owner,
        0,
        length,
        0,
        Box::new([ptr::null()]),
        Box::new(children),
    )
}
