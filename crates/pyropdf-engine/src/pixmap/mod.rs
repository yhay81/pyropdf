//! Raster pixel operations, image codecs, CCITT Group 4 encoding, and PDF
//! image sample and stream helpers (DESIGN.md section 11).

mod codecs;
mod curve;
pub use curve::{CurveStep, DeviceCurve, apply_black_tables};
mod encode;
pub mod fax;
pub mod images;
pub mod jpx;
mod ops;
#[cfg(test)]
mod tests;

use pyropdf_store::LimitExceeded;

pub use encode::{
    Bounded, BudgetedPngOutput, EncodeError, Encoding, FORMATS, encode, encode_png, format_of,
    recolor_samples,
};
pub use ops::{
    Projection, add_alpha, color_runs, convert, crop, decode, device_samples, drop_alpha, fill,
    flip, gamma_table, invert, luma, map_colors, rotate, scale, tint_table, warp,
};

/// The most pixels of a decoded image or a scaled pixmap, as rendering
/// bounds one raster.
pub const MAX_PIXELS: u64 = 64_000_000;

/// The shape of a pixmap's samples.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Layout {
    pub width: u32,
    pub height: u32,
    pub gray: bool,
    pub alpha: bool,
}

impl Layout {
    /// The color components of a pixel, without alpha.
    #[must_use]
    pub const fn colors(self) -> usize {
        if self.gray { 1 } else { 3 }
    }

    /// The bytes of a pixel.
    #[must_use]
    pub const fn channels(self) -> usize {
        self.colors() + self.alpha as usize
    }

    /// The bytes of a row.
    #[must_use]
    pub const fn stride(self) -> usize {
        self.width as usize * self.channels()
    }

    /// The bytes of a row in 64-bit arithmetic, which never overflows for any
    /// `u32` width.
    #[must_use]
    pub const fn stride_u64(self) -> u64 {
        self.width as u64 * self.channels() as u64
    }

    /// The bytes of a row, returning `None` on pointer-width overflow.
    #[must_use]
    pub fn stride_checked(self) -> Option<usize> {
        usize::try_from(self.width)
            .ok()?
            .checked_mul(self.channels())
    }

    /// The total bytes of the raster (`height * width * channels`), returning
    /// `None` on pointer-width overflow.
    #[must_use]
    pub fn byte_len_checked(self) -> Option<usize> {
        self.stride_checked()?
            .checked_mul(usize::try_from(self.height).ok()?)
    }

    /// The starting byte offset of pixel `(x, y)` in row-major order, returning
    /// `None` if `(x, y)` is outside the raster or the byte offsets overflow
    /// `usize`.
    #[must_use]
    pub fn pixel_offset(self, x: i64, y: i64) -> Option<usize> {
        if !(0..i64::from(self.width)).contains(&x) || !(0..i64::from(self.height)).contains(&y) {
            return None;
        }
        let channels = self.channels();
        let stride = self.stride_checked()?;
        let total = stride.checked_mul(usize::try_from(self.height).ok()?)?;
        let y_off = usize::try_from(y).ok()?.checked_mul(stride)?;
        let x_off = usize::try_from(x).ok()?.checked_mul(channels)?;
        let offset = y_off.checked_add(x_off)?;
        (offset.checked_add(channels)? <= total).then_some(offset)
    }

    /// Validates and computes the 3D `(height, width, channels)` PEP 3118
    /// buffer geometry for a backing buffer of `data_len` bytes.
    pub fn buffer_view(
        self,
        data_len: usize,
        require_f_contiguous: bool,
    ) -> Result<BufferView, BufferViewError> {
        let len = isize::try_from(data_len).map_err(|_| BufferViewError::TooLarge)?;
        let channels = isize::try_from(self.channels()).map_err(|_| BufferViewError::TooLarge)?;
        let width = isize::try_from(self.width).map_err(|_| BufferViewError::TooLarge)?;
        let height = isize::try_from(self.height).map_err(|_| BufferViewError::TooLarge)?;
        let row_stride = width
            .checked_mul(channels)
            .ok_or(BufferViewError::TooLarge)?;
        let expected = height
            .checked_mul(row_stride)
            .ok_or(BufferViewError::TooLarge)?;
        if expected != len {
            return Err(BufferViewError::TooLarge);
        }
        let f_contiguous = len == 0
            || [height, width, channels]
                .into_iter()
                .filter(|&dim| dim > 1)
                .count()
                <= 1;
        if require_f_contiguous && !f_contiguous {
            return Err(BufferViewError::NotFortranContiguous);
        }
        Ok(BufferView {
            len,
            dimensions: [height, width, channels, row_stride, channels, 1],
            f_contiguous,
        })
    }
}

/// Why a pixmap layout cannot be exported as a 3D PEP 3118 buffer view.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BufferViewError {
    /// Dimensions, stride, or byte length overflow `isize` or do not match `data_len`.
    TooLarge,
    /// The caller required Fortran-contiguous layout for a multi-axis raster.
    NotFortranContiguous,
}

/// Validated 3D `(height, width, channels)` PEP 3118 buffer geometry.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BufferView {
    /// Total length in bytes (`Py_buffer.len`).
    pub len: isize,
    /// `[height, width, channels, row_stride, channels, 1]`.
    pub dimensions: [isize; 6],
    /// Whether this C-contiguous 3D layout is also Fortran-contiguous.
    pub f_contiguous: bool,
}

/// A rectangle of whole pixels, `x0 <= x < x1` and `y0 <= y < y1`.
pub type Area = (u32, u32, u32, u32);

/// A decoded image: its layout, samples, and resolution in dots per inch.
#[derive(Debug)]
pub struct Decoded {
    pub layout: Layout,
    pub data: Vec<u8>,
    pub resolution: (u32, u32),
}

/// A malformed image or an operation budget that refused decoding.
#[derive(Debug)]
pub enum DecodeError {
    Invalid(String),
    Limit(LimitExceeded),
}

impl From<String> for DecodeError {
    fn from(message: String) -> Self {
        Self::Invalid(message)
    }
}

impl From<LimitExceeded> for DecodeError {
    fn from(refusal: LimitExceeded) -> Self {
        Self::Limit(refusal)
    }
}

/// Checks that `width` by `height` holds between 1 and [`MAX_PIXELS`] pixels.
pub fn check_size(width: u32, height: u32) -> Result<(), String> {
    let pixels = u64::from(width) * u64::from(height);
    if pixels == 0 || pixels > MAX_PIXELS {
        return Err(format!(
            "the image has {pixels} pixels; a pixmap holds 1 to {MAX_PIXELS}"
        ));
    }
    Ok(())
}

#[cfg(test)]
mod buffer_math_tests {
    use super::*;

    #[test]
    fn layout_pixel_offset_and_stride_reject_overflow_and_out_of_bounds() {
        let rgb = Layout {
            width: 10,
            height: 5,
            gray: false,
            alpha: false,
        };
        assert_eq!(rgb.stride_u64(), 30);
        assert_eq!(rgb.stride_checked(), Some(30));
        assert_eq!(rgb.byte_len_checked(), Some(150));
        assert_eq!(rgb.pixel_offset(0, 0), Some(0));
        assert_eq!(rgb.pixel_offset(9, 4), Some(147));
        assert_eq!(rgb.pixel_offset(-1, 0), None);
        assert_eq!(rgb.pixel_offset(10, 0), None);
        assert_eq!(rgb.pixel_offset(0, 5), None);

        // Huge dimensions that would overflow 32-bit stride/size are checked cleanly.
        let huge = Layout {
            width: u32::MAX,
            height: 2,
            gray: false,
            alpha: true,
        };
        assert_eq!(huge.stride_u64(), u64::from(u32::MAX) * 4);
        assert!(huge.buffer_view(0, false).is_err());
    }

    #[test]
    fn layout_buffer_view_checks_length_and_fortran_contiguity() {
        let gray_1d = Layout {
            width: 1,
            height: 4,
            gray: true,
            alpha: false,
        };
        let view = gray_1d.buffer_view(4, true).unwrap();
        assert_eq!(view.len, 4);
        assert_eq!(view.dimensions, [4, 1, 1, 1, 1, 1]);
        assert!(view.f_contiguous);

        // Mismatched backing buffer length is refused.
        assert_eq!(
            gray_1d.buffer_view(3, false),
            Err(BufferViewError::TooLarge)
        );
        assert_eq!(
            gray_1d.buffer_view(5, false),
            Err(BufferViewError::TooLarge)
        );

        // 2x1 RGB has two axes > 1 (height=2, channels=3), so not F-contiguous.
        let rgb_col = Layout {
            width: 1,
            height: 2,
            gray: false,
            alpha: false,
        };
        assert_eq!(
            rgb_col.buffer_view(6, true),
            Err(BufferViewError::NotFortranContiguous)
        );
        let view = rgb_col.buffer_view(6, false).unwrap();
        assert_eq!(view.dimensions, [2, 1, 3, 3, 3, 1]);
        assert!(!view.f_contiguous);
    }
}

#[cfg(kani)]
mod kani_proofs {
    use super::*;

    /// `Layout::pixel_offset` never panics or wraps across the full 32-bit
    /// dimension and 64-bit coordinate domain, and rejects all out-of-bounds
    /// coordinates.
    #[kani::proof]
    fn kani_pixmap_pixel_offset_no_panic() {
        let layout = Layout {
            width: kani::any(),
            height: kani::any(),
            gray: kani::any(),
            alpha: kani::any(),
        };
        let x: i64 = kani::any();
        let y: i64 = kani::any();
        if layout.pixel_offset(x, y).is_some() {
            assert!((0..i64::from(layout.width)).contains(&x));
            assert!((0..i64::from(layout.height)).contains(&y));
        }
    }

    /// Over all rasters up to 16x16, `Layout::pixel_offset` succeeds iff
    /// `(x, y)` is in bounds, equals `(y * width + x) * channels`, fits inside
    /// `byte_len_checked`, and maps distinct pixels to non-overlapping slices.
    #[kani::proof]
    fn kani_pixmap_pixel_offset_injective_and_bounded() {
        let width: u8 = kani::any();
        let height: u8 = kani::any();
        kani::assume(width <= 16 && height <= 16);
        let layout = Layout {
            width: u32::from(width),
            height: u32::from(height),
            gray: kani::any(),
            alpha: kani::any(),
        };
        let x1: i8 = kani::any();
        let y1: i8 = kani::any();
        kani::assume((-1..=16).contains(&x1) && (-1..=16).contains(&y1));
        let in_bounds = (0..i64::from(width)).contains(&i64::from(x1))
            && (0..i64::from(height)).contains(&i64::from(y1));
        let off1 = layout.pixel_offset(i64::from(x1), i64::from(y1));
        assert_eq!(off1.is_some(), in_bounds);
        if let Some(off1) = off1 {
            let channels = layout.channels();
            let total = layout.byte_len_checked().unwrap();
            assert!(off1 + channels <= total);
            assert_eq!(
                off1,
                (usize::from(y1 as u8) * usize::from(width) + usize::from(x1 as u8)) * channels
            );
            let x2: i8 = kani::any();
            let y2: i8 = kani::any();
            kani::assume((0..=15).contains(&x2) && (0..=15).contains(&y2));
            if (x1, y1) != (x2, y2)
                && let Some(off2) = layout.pixel_offset(i64::from(x2), i64::from(y2))
            {
                assert!(off1 + channels <= off2 || off2 + channels <= off1);
            }
        }
    }

    /// `Layout::buffer_view` never panics, validates exact 3D shape/stride
    /// products against `data_len`, and checks Fortran-contiguity accurately.
    #[kani::proof]
    fn kani_pixmap_buffer_view_invariants() {
        let layout = Layout {
            width: kani::any(),
            height: kani::any(),
            gray: kani::any(),
            alpha: kani::any(),
        };
        let data_len: usize = kani::any();
        let require_f: bool = kani::any();
        match layout.buffer_view(data_len, require_f) {
            Ok(view) => {
                let [h, w, c, row_stride, col_stride, chan_stride] = view.dimensions;
                assert_eq!(view.len, data_len as isize);
                assert_eq!(col_stride, c);
                assert_eq!(chan_stride, 1);
                assert_eq!(w.checked_mul(c), Some(row_stride));
                assert_eq!(h.checked_mul(row_stride), Some(view.len));
                if require_f {
                    assert!(view.f_contiguous);
                }
            }
            Err(BufferViewError::NotFortranContiguous) => {
                assert!(require_f);
                assert!(data_len > 0);
            }
            Err(BufferViewError::TooLarge) => {}
        }
    }

    /// For every non-empty small 3D raster `(h, w, c)`, `f_contiguous` holds
    /// iff C-order and F-order flat offsets agree at every valid index `(i, j, k)`.
    #[kani::proof]
    fn kani_pixmap_f_contiguous_iff_strides_agree() {
        let width: u32 = kani::any();
        let height: u32 = kani::any();
        kani::assume((1..=4).contains(&width) && (1..=4).contains(&height));
        let layout = Layout {
            width,
            height,
            gray: kani::any(),
            alpha: kani::any(),
        };
        let c = layout.channels();
        let h = height as usize;
        let w = width as usize;
        let view = layout.buffer_view(h * w * c, false).unwrap();

        let i: usize = kani::any();
        let j: usize = kani::any();
        let k: usize = kani::any();
        kani::assume(i < h && j < w && k < c);
        let c_offset = i * (w * c) + j * c + k;
        let f_offset = i + j * h + k * (h * w);
        if view.f_contiguous {
            assert_eq!(c_offset, f_offset);
        }
    }
}
