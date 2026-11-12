//! Page display geometry shared by the bindings, rendering and extraction.

use hayro::hayro_interpret::TransformExt;
use hayro::hayro_syntax::page::Page as HayroPage;
use hayro::kurbo::Affine;
use pyropdf_store::pages::Page;
use pyropdf_store::value::{PdfString, Value};

/// The default page dimensions when there is no usable box.
pub const A4: (f64, f64) = (595.0, 842.0);

/// The rotated visible page and its map from PDF user space. The view is
/// measured in unscaled units; each unit is `unit` physical points.
#[derive(Clone, Copy, Debug)]
pub struct Display {
    pub(crate) view_size: (f64, f64),
    pub(crate) user_to_view: Affine,
    pub(crate) unit: f64,
}

impl Display {
    /// Reads the unrounded boxes and the resolved unit of one store page.
    #[must_use]
    pub fn from_page(page: &Page) -> Self {
        let [x0, y0, x1, y1] = display_box(page);
        let width = x1 - x0;
        let height = y1 - y0;
        let (view_size, coefficients) = match rotation(page.rotate()) {
            90 => ((height, width), [0.0, 1.0, 1.0, 0.0, -y0, -x0]),
            180 => ((width, height), [-1.0, 0.0, 0.0, 1.0, x1, -y0]),
            270 => ((height, width), [0.0, -1.0, -1.0, 0.0, y1, x1]),
            _ => ((width, height), [1.0, 0.0, 0.0, -1.0, -x0, y1]),
        };
        Self {
            view_size,
            user_to_view: Affine::new(coefficients),
            unit: page.user_unit(),
        }
    }

    /// Keeps hayro's native viewport and transform on ordinary unit-one pages.
    pub(crate) fn native(page: &HayroPage<'_>) -> Self {
        let (width, height) = page.render_dimensions();
        Self {
            view_size: (f64::from(width), f64::from(height)),
            user_to_view: page.initial_transform(true).to_kurbo(),
            unit: 1.0,
        }
    }

    /// The visible width and height in points, after rotation.
    #[must_use]
    pub fn size(self) -> (f64, f64) {
        (self.view_size.0 * self.unit, self.view_size.1 * self.unit)
    }

    pub(crate) fn transform(self) -> Affine {
        Affine::scale(self.unit) * self.user_to_view
    }
}

/// The visible box in unrotated user space. On native pages retain hayro's
/// one-unit minimum. On scaled pages even a fraction of a user unit can
/// cover many points and the actual crop determines the display origin.
#[must_use]
pub fn display_box(page: &Page) -> [f64; 4] {
    let media = page
        .media_box()
        .and_then(rect)
        .unwrap_or((0.0, 0.0, A4.0, A4.1));
    let crop = page.crop_box().and_then(rect).unwrap_or(media);
    let (x0, y0) = (crop.0.max(media.0), crop.1.max(media.1));
    let (x1, y1) = (crop.2.min(media.2), crop.3.min(media.3));
    let (width, height) = (x1 - x0, y1 - y0);
    if page.user_unit() == 1.0 && width >= 1e-6 && height >= 1e-6 {
        [x0, y0, x0 + width.max(1.0), y0 + height.max(1.0)]
    } else if page.user_unit() != 1.0 && width > 0.0 && height > 0.0 {
        [x0, y0, x1, y1]
    } else {
        [0.0, 0.0, A4.0, A4.1]
    }
}

/// Normalizes `/Rotate` to 0, 90, 180, or 270, as hayro reads it.
#[must_use]
pub fn rotation(value: Option<&Value>) -> i64 {
    match value {
        Some(&Value::Integer(rotate)) => match rotate.rem_euclid(360) {
            r @ (0 | 90 | 180 | 270) => r,
            _ => 0,
        },
        _ => 0,
    }
}

/// The page boxes, in order from media box to art box.
pub const BOXES: [&str; 5] = ["MediaBox", "CropBox", "BleedBox", "TrimBox", "ArtBox"];

/// A normalized rectangle `(x0, y0, x1, y1)`.
pub type Box4 = (f64, f64, f64, f64);

/// Returns the page boxes of `page` in unrotated user space: the inherited
/// media box, or hayro's A4, then the crop, bleed, trim, and art boxes,
/// each clipped to the media box and defaulting to the crop box
/// (ISO 32000-2 14.11.2).
#[must_use]
pub fn user_boxes(page: &Page) -> [Box4; 5] {
    let media = page
        .media_box()
        .and_then(rect)
        .unwrap_or((0.0, 0.0, A4.0, A4.1));
    let clip = |b: Box4| {
        (
            b.0.max(media.0),
            b.1.max(media.1),
            b.2.min(media.2),
            b.3.min(media.3),
        )
    };
    let crop = page.crop_box().and_then(rect).map_or(media, clip);
    let own = |key: &str| page.dict().get(key).and_then(rect).map_or(crop, clip);
    [media, crop, own(BOXES[2]), own(BOXES[3]), own(BOXES[4])]
}

/// Reads a rectangle of four numbers, normalized.
#[must_use]
#[expect(clippy::cast_precision_loss, reason = "display coordinates are f64")]
pub fn rect(value: &Value) -> Option<Box4> {
    let Value::Array(array) = value else {
        return None;
    };
    let mut numbers = [0.0; 4];
    if array.len() != 4 {
        return None;
    }
    for (slot, item) in numbers.iter_mut().zip(array.iter()) {
        *slot = match item {
            Value::Integer(n) => *n as f64,
            Value::Real(r) => r.value(),
            _ => return None,
        };
    }
    let [a, b, c, d] = numbers;
    Some((a.min(c), b.min(d), a.max(c), b.max(d)))
}

/// Converts a point of PDF user space to display space, in points, on a
/// page whose display box is `crop`, whose normalized `/Rotate` is
/// `rotation`, and whose user space unit is `unit` points.
#[must_use]
pub fn to_display(crop: [f64; 4], rotation: i64, unit: f64, x: f64, y: f64) -> (f64, f64) {
    let [cx0, cy0, cx1, cy1] = crop;
    let (u, v) = match rotation {
        90 => (y - cy0, x - cx0),
        180 => (cx1 - x, y - cy0),
        270 => (cy1 - y, cx1 - x),
        _ => (x - cx0, cy1 - y),
    };
    (u * unit, v * unit)
}

/// Converts a point of display space to PDF user space, as the inverse of
/// [`to_display`].
#[must_use]
pub fn to_pdf(crop: [f64; 4], rotation: i64, unit: f64, x: f64, y: f64) -> (f64, f64) {
    let [cx0, cy0, cx1, cy1] = crop;
    let (x, y) = (x / unit, y / unit);
    match rotation {
        90 => (cx0 + y, cy0 + x),
        180 => (cx1 - x, cy0 + y),
        270 => (cx1 - y, cy1 - x),
        _ => (cx0 + x, cy1 - y),
    }
}

/// Encodes a text string as `String.text` reads it: ASCII as is, anything
/// else as UTF-16BE.
#[must_use]
#[expect(
    clippy::disallowed_methods,
    reason = "at most two bytes per input UTF-8 byte plus the two-byte BOM; callers bound or charge the text first"
)]
pub fn encode_text(text: &str) -> Value {
    let bytes = if text.is_ascii() {
        text.as_bytes().to_vec()
    } else {
        let mut bytes = vec![0xFE, 0xFF];
        bytes.extend(text.encode_utf16().flat_map(u16::to_be_bytes));
        bytes
    };
    Value::String(PdfString::literal(bytes))
}

/// Returns `value` as an integer when it is one, else as a real.
#[must_use]
pub fn number(value: f64) -> Value {
    if value.fract() == 0.0 && value.abs() < 1e15 {
        Value::Integer(value as i64)
    } else {
        Value::from(value)
    }
}
