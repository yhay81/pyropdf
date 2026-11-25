//! Pure-Rust column buffer-protocol layout, row-range validation, Arrow
//! validity bitmap helpers, and plain numeric byte reinterpretation shared
//! with the Python binding (DESIGN.md sections 9, 10, F6, and F9).
//!
//! Keeping these routines in `pyropdf-engine` allows exhaustive verification
//! with Kani and undefined-behavior checking with Miri without linking
//! CPython.

/// Plain numeric column element types with no padding bytes.
///
/// # Safety
/// Every byte of a valid value of an implementing type must be initialized.
pub unsafe trait PlainNumber: Copy {}

unsafe impl PlainNumber for u8 {}
unsafe impl PlainNumber for i8 {}
unsafe impl PlainNumber for i32 {}
unsafe impl PlainNumber for u32 {}
unsafe impl PlainNumber for f32 {}
unsafe impl PlainNumber for f64 {}

/// Returns the bytes of a slice of plain numbers without copying.
#[must_use]
pub fn plain_bytes<T: PlainNumber>(items: &[T]) -> &[u8] {
    // SAFETY: `PlainNumber` is implemented only for plain numeric types without
    // padding, so every byte of the slice is initialized and `items.as_ptr()` is
    // valid for `size_of_val(items)` bytes (even when empty, `as_ptr()` is non-null).
    unsafe { std::slice::from_raw_parts(items.as_ptr().cast(), size_of_val(items)) }
}

/// Returns whether `start..stop` is a valid row slice of a table with `rows`
/// rows.
#[must_use]
pub const fn row_range_valid(start: usize, stop: usize, rows: usize) -> bool {
    start <= stop && stop <= rows
}

/// Returns the item size in bytes of a PEP 3118 single-character column format
/// exported by `CharColumns` or `PathColumns`, or `None` if unsupported.
#[must_use]
pub const fn column_itemsize(format: &[u8]) -> Option<isize> {
    match format {
        b"d" => Some(8),
        b"f" | b"i" | b"I" => Some(4),
        b"B" | b"b" => Some(1),
        _ => None,
    }
}

/// Validated 1D read-only contiguous PEP 3118 buffer geometry for a column.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ColumnBufferView {
    /// Total length in bytes (`Py_buffer.len`).
    pub len: isize,
    /// Element size in bytes (`Py_buffer.itemsize`).
    pub itemsize: isize,
    /// `[shape_0, stride_0]` stored in `Py_buffer.internal`.
    pub shape_and_stride: [isize; 2],
}

/// Validates and computes the 1D buffer geometry for a column of `byte_len`
/// bytes and PEP 3118 `format`.
///
/// Returns `None` if `format` is unrecognized, `byte_len` does not fit in
/// `isize`, or `byte_len` is not a multiple of the element size.
#[must_use]
pub fn column_buffer_view(byte_len: usize, format: &[u8]) -> Option<ColumnBufferView> {
    let itemsize = column_itemsize(format)?;
    let len = isize::try_from(byte_len).ok()?;
    if len % itemsize != 0 {
        return None;
    }
    Some(ColumnBufferView {
        len,
        itemsize,
        shape_and_stride: [len / itemsize, itemsize],
    })
}

/// Sets bit `row` of an Arrow validity bitmap (`bits`, LSB first). Returns
/// `false` if `row / 8` is out of bounds.
pub fn set_bitmap_bit(bits: &mut [u8], row: usize) -> bool {
    let Some(byte) = bits.get_mut(row / 8) else {
        return false;
    };
    *byte |= 1_u8 << (row % 8);
    true
}

/// Returns how many bits in `start..stop` of an Arrow validity bitmap (`bits`,
/// LSB first) are clear (`0`), or `None` if `start > stop` or `bits` does not
/// cover `stop`.
#[must_use]
pub fn clear_bitmap_bits(bits: &[u8], start: usize, stop: usize) -> Option<usize> {
    if start > stop || stop.div_ceil(8) > bits.len() {
        return None;
    }
    Some(
        (start..stop)
            .filter(|&row| (bits[row / 8] >> (row % 8)) & 1 == 0)
            .count(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plain_bytes_covers_every_plain_numeric_type_and_empty_slices() {
        let u8s: [u8; 3] = [1, 2, 3];
        assert_eq!(plain_bytes(&u8s), &[1, 2, 3]);
        assert_eq!(plain_bytes::<u8>(&[]), &[]);

        let i8s: [i8; 2] = [-1, 127];
        assert_eq!(plain_bytes(&i8s), &[0xff, 0x7f]);
        assert_eq!(plain_bytes::<i8>(&[]), &[]);

        let i32s: [i32; 2] = [-42, 0x1234_5678];
        let mut expected_i32 = Vec::new();
        for v in i32s {
            expected_i32.extend_from_slice(&v.to_ne_bytes());
        }
        assert_eq!(plain_bytes(&i32s), expected_i32.as_slice());
        assert_eq!(plain_bytes::<i32>(&[]), &[]);

        let u32s: [u32; 2] = [0, u32::MAX];
        let mut expected_u32 = Vec::new();
        for v in u32s {
            expected_u32.extend_from_slice(&v.to_ne_bytes());
        }
        assert_eq!(plain_bytes(&u32s), expected_u32.as_slice());

        let f32s: [f32; 2] = [1.5, -0.25];
        let mut expected_f32 = Vec::new();
        for v in f32s {
            expected_f32.extend_from_slice(&v.to_ne_bytes());
        }
        assert_eq!(plain_bytes(&f32s), expected_f32.as_slice());

        let f64s: [f64; 2] = [std::f64::consts::PI, -0.0];
        let mut expected_f64 = Vec::new();
        for v in f64s {
            expected_f64.extend_from_slice(&v.to_ne_bytes());
        }
        assert_eq!(plain_bytes(&f64s), expected_f64.as_slice());
        assert_eq!(plain_bytes::<f64>(&[]), &[]);
    }

    #[test]
    fn column_buffer_view_validates_alignment_format_and_size() {
        assert_eq!(
            column_buffer_view(24, b"d"),
            Some(ColumnBufferView {
                len: 24,
                itemsize: 8,
                shape_and_stride: [3, 8],
            })
        );
        assert_eq!(
            column_buffer_view(0, b"I"),
            Some(ColumnBufferView {
                len: 0,
                itemsize: 4,
                shape_and_stride: [0, 4],
            })
        );
        // Misaligned byte length for 8-byte and 4-byte formats is rejected.
        assert_eq!(column_buffer_view(7, b"d"), None);
        assert_eq!(column_buffer_view(6, b"f"), None);
        // Overflow past isize::MAX is rejected.
        assert_eq!(column_buffer_view(usize::MAX, b"B"), None);
        // Unknown format is rejected.
        assert_eq!(column_buffer_view(8, b"q"), None);
    }

    #[test]
    fn bitmap_helpers_check_bounds_and_count_clear_bits() {
        let mut bits = [0u8; 2];
        assert!(set_bitmap_bit(&mut bits, 0));
        assert!(set_bitmap_bit(&mut bits, 7));
        assert!(set_bitmap_bit(&mut bits, 10));
        assert!(!set_bitmap_bit(&mut bits, 16));
        assert_eq!(clear_bitmap_bits(&bits, 0, 16), Some(13));
        assert_eq!(clear_bitmap_bits(&bits, 7, 11), Some(2));
        assert_eq!(clear_bitmap_bits(&bits, 5, 5), Some(0));
        assert_eq!(clear_bitmap_bits(&bits, 6, 5), None);
        assert_eq!(clear_bitmap_bits(&bits, 0, 17), None);
        assert!(row_range_valid(0, 5, 5));
        assert!(!row_range_valid(6, 5, 5));
        assert!(!row_range_valid(0, 6, 5));
    }
}

#[cfg(kani)]
mod kani_proofs {
    use super::*;

    /// `column_buffer_view` never panics for any `byte_len: usize` and format
    /// byte, rejects lengths exceeding `isize::MAX` or misaligned to `itemsize`,
    /// and satisfies `shape * stride == len`.
    #[kani::proof]
    fn kani_column_buffer_view_invariants() {
        let byte_len: usize = kani::any();
        let tag: u8 = kani::any();
        let format = [tag];
        match column_buffer_view(byte_len, &format) {
            Some(view) => {
                assert!(byte_len <= isize::MAX as usize);
                assert_eq!(view.len, byte_len as isize);
                assert!(matches!(view.itemsize, 1 | 4 | 8));
                assert_eq!(view.shape_and_stride[1], view.itemsize);
                assert_eq!(
                    view.shape_and_stride[0].checked_mul(view.shape_and_stride[1]),
                    Some(view.len)
                );
            }
            None => {
                let valid_fmt = column_itemsize(&format);
                assert!(
                    valid_fmt.is_none()
                        || byte_len > isize::MAX as usize
                        || (byte_len as isize) % valid_fmt.unwrap() != 0
                );
            }
        }
    }

    /// `set_bitmap_bit` and `clear_bitmap_bits` never panic for arbitrary
    /// indices/ranges and preserve exact bit semantics across a 4-byte bitmap.
    #[kani::proof]
    #[kani::unwind(34)]
    fn kani_bitmap_set_and_clear_exact() {
        let mut bits: [u8; 4] = kani::any();
        let row: usize = kani::any();
        let before = bits;
        let ok = set_bitmap_bit(&mut bits, row);
        if row < 32 {
            assert!(ok);
            assert_eq!((bits[row / 8] >> (row % 8)) & 1, 1);
            // Unrelated bytes are unchanged.
            for byte_idx in 0..4 {
                if byte_idx != row / 8 {
                    assert_eq!(bits[byte_idx], before[byte_idx]);
                }
            }
        } else {
            assert!(!ok);
            assert_eq!(bits, before);
        }

        let start: usize = kani::any();
        let stop: usize = kani::any();
        match clear_bitmap_bits(&bits, start, stop) {
            Some(clear) => {
                assert!(start <= stop && stop <= 32);
                assert!(clear <= stop - start);
                if row >= start && row < stop {
                    // Since `row` was just set, not all bits in `start..stop` can be clear.
                    assert!(clear < stop - start);
                }
            }
            None => {
                assert!(start > stop || stop > 32);
            }
        }
    }

    /// `plain_bytes` reinterprets `u32` and `f64` slices without UB and matches
    /// native-endian byte representations.
    #[kani::proof]
    #[kani::unwind(10)]
    fn kani_plain_bytes_unsafe_roundtrip() {
        let vals: [u32; 4] = kani::any();
        let len: usize = kani::any();
        kani::assume(len <= vals.len());
        let raw = plain_bytes(&vals[..len]);
        assert_eq!(raw.len(), len * size_of::<u32>());
        for i in 0..len {
            assert_eq!(&raw[i * 4..(i + 1) * 4], &vals[i].to_ne_bytes());
        }

        let floats: [f64; 2] = kani::any();
        let flen: usize = kani::any();
        kani::assume(flen <= floats.len());
        let fraw = plain_bytes(&floats[..flen]);
        assert_eq!(fraw.len(), flen * size_of::<f64>());
        for i in 0..flen {
            assert_eq!(&fraw[i * 8..(i + 1) * 8], &floats[i].to_ne_bytes());
        }
    }
}
