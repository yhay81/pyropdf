//! Annotation geometry, coordinate conversion, and attribute validation.

use std::sync::Arc;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::pages::PageIndex;
use pyropdf_store::value::{Array, Dict, Name, Value};
use pyropdf_store::version::Version;

use crate::display::{number, to_display, to_pdf};

use super::read::PageAnnots;
use super::{
    AnnotationError, AnnotationResult as Result, LINE_ENDINGS, MAX_BORDER_DASHES, PointLists,
    has_points, valid_dashes,
};

/// A display rectangle, `(x0, y0, x1, y1)`.
pub type Rect = (f64, f64, f64, f64);

/// The text entries of an [`AnnotationRecord`](super::AnnotationRecord), in its
/// tuple order.
pub const TEXT_KEYS: [&str; 8] = [
    "Contents",
    "Name",
    "T",
    "CreationDate",
    "M",
    "Subj",
    "NM",
    "IT",
];

/// The icons of a note (ISO 32000-2 section 12.5.6.4).
pub const NOTE_ICONS: [&str; 7] = [
    "Comment",
    "Help",
    "Insert",
    "Key",
    "NewParagraph",
    "Note",
    "Paragraph",
];

/// The stamps of ISO 32000-2 section 12.5.6.12.
pub const STAMP_ICONS: [&str; 14] = [
    "Approved",
    "AsIs",
    "Confidential",
    "Departmental",
    "Experimental",
    "Expired",
    "Final",
    "ForComment",
    "ForPublicRelease",
    "NotApproved",
    "NotForPublicRelease",
    "Sold",
    "TopSecret",
    "Draft",
];

/// The icons of a file attachment (ISO 32000-2 section 12.5.6.15).
pub const FILE_ICONS: [&str; 4] = ["Graph", "PushPin", "Paperclip", "Tag"];

/// The types whose `/Rect` an edit may move; the others follow their points.
pub const RECT_SUBTYPES: [&str; 7] = [
    "Square",
    "Circle",
    "Text",
    "FreeText",
    "Stamp",
    "Caret",
    "FileAttachment",
];

/// The subtypes that `page.annotations.add` adds.
pub const ADDED_SUBTYPES: [&str; 15] = [
    "Highlight",
    "Underline",
    "StrikeOut",
    "Squiggly",
    "Text",
    "FreeText",
    "Stamp",
    "Square",
    "Circle",
    "Line",
    "Polygon",
    "PolyLine",
    "Ink",
    "Caret",
    "FileAttachment",
];

/// The maximum number of properties that an edit may clear at once.
pub const CLEARABLE: usize = 10;

/// At most three callout points, stored inline without heap allocation.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct CalloutPoints {
    points: [(f64, f64); 3],
    len: usize,
    too_many: bool,
}

impl CalloutPoints {
    /// Creates callout points from up to three points and an overflow flag.
    #[must_use]
    pub const fn new(points: [(f64, f64); 3], len: usize, too_many: bool) -> Self {
        Self {
            points,
            len,
            too_many,
        }
    }

    /// Creates two-point callout geometry `[tip, box_point]`.
    #[must_use]
    pub const fn two(tip: (f64, f64), edge: (f64, f64)) -> Self {
        Self {
            points: [tip, edge, (0.0, 0.0)],
            len: 2,
            too_many: false,
        }
    }

    /// Creates three-point callout geometry `[tip, knee, box_point]`.
    #[must_use]
    pub const fn three(tip: (f64, f64), knee: (f64, f64), edge: (f64, f64)) -> Self {
        Self {
            points: [tip, knee, edge],
            len: 3,
            too_many: false,
        }
    }

    /// Returns whether the input supplied more than three points.
    #[must_use]
    pub const fn is_too_many(&self) -> bool {
        self.too_many
    }
}

impl std::ops::Deref for CalloutPoints {
    type Target = [(f64, f64)];

    fn deref(&self) -> &Self::Target {
        &self.points[..self.len]
    }
}

/// The display points of a line, polygon, polyline, or ink annotation, `None`
/// for another kind or missing/malformed geometry, and the `/LE` styles of a
/// line or polyline.
#[derive(Clone, Debug, PartialEq)]
pub struct AnnotationGeometry {
    /// Display-space point lists (`None` for non-point annotations or missing
    /// geometry).
    pub points: Option<PointLists>,
    /// Start and end `/LE` style names for `/Line` and `/PolyLine`.
    pub line_endings: Option<(&'static str, &'static str)>,
}

/// Tuple representation of [`AnnotationGeometry`] returned across FFI.
pub type GeometryTuple = (Option<PointLists>, Option<(&'static str, &'static str)>);

impl AnnotationGeometry {
    /// Converts into the `(points, line_endings)` tuple returned across FFI.
    #[must_use]
    pub fn into_tuple(self) -> GeometryTuple {
        (self.points, self.line_endings)
    }
}

/// Reads the display-space points and line endings of the annotation that a
/// handle names, or returns `None` when the page is gone.
pub fn annotation_geometry(
    version: Arc<Version>,
    pages: Arc<PageIndex>,
    budget: &Budget,
    number: u32,
    generation: u16,
    index: u32,
    id: (u32, u16),
) -> Result<Option<AnnotationGeometry>> {
    let Some(annots) = PageAnnots::read(version, pages, budget, number, generation)? else {
        return Ok(None);
    };
    let (_, dict) = annots.find(index, id)?;
    let subtype = annots.name(&dict, "Subtype")?.unwrap_or_default();
    if !has_points(&subtype) {
        return Ok(Some(AnnotationGeometry {
            points: None,
            line_endings: None,
        }));
    }
    let reader = annots.reader();
    let mut points = reader.point_lists(&dict, &subtype)?;
    if let Some(lists) = &mut points {
        for list in lists {
            for point in list {
                budget.poll()?;
                *point = to_display(annots.crop, annots.rotation, annots.unit, point.0, point.1);
            }
        }
    }
    let line_endings = if matches!(&*subtype, b"Line" | b"PolyLine") {
        let [start, end] = reader.line_endings(&dict)?;
        Some((LINE_ENDINGS[start], LINE_ENDINGS[end]))
    } else {
        None
    };
    Ok(Some(AnnotationGeometry {
        points,
        line_endings,
    }))
}

/// Validates an RGB triple in `[0.0, 1.0]` and encodes it as a 3-number PDF
/// array.
pub fn rgb(components: (f64, f64, f64), label: &str) -> Result<Value> {
    let (red, green, blue) = components;
    if ![red, green, blue]
        .iter()
        .all(|c| c.is_finite() && (0.0..=1.0).contains(c))
    {
        return Err(AnnotationError::Pdf(format!(
            "an annotation {label} has components from 0 to 1"
        )));
    }
    Ok(Value::Array(Array::from(vec![
        number(red),
        number(green),
        number(blue),
    ])))
}

/// Validates and builds the `/BS` dictionary of a border, converting display
/// points into the page's user space (`unit`).
pub fn border_style(
    (width, style, dashes): &(f64, String, Vec<f64>),
    unit: f64,
    budget: &Budget,
) -> Result<Value> {
    budget.check(LimitCode::AnnotationDashCount, dashes.len() as u64)?;
    if !(width.is_finite() && *width >= 0.0) {
        return Err(AnnotationError::Pdf(
            "an annotation border width is finite and not negative".to_owned(),
        ));
    }
    if !matches!(style.as_str(), "S" | "D" | "B" | "I" | "U") {
        return Err(AnnotationError::Pdf(format!(
            "unknown annotation border style {style:?}"
        )));
    }
    if dashes.len() > MAX_BORDER_DASHES
        || (!dashes.is_empty() && !valid_dashes(dashes))
        || (style == "D") == dashes.is_empty()
    {
        return Err(AnnotationError::Pdf(format!(
            "a dashed border has 1 to {MAX_BORDER_DASHES} non-negative dash lengths, and only a dashed one"
        )));
    }
    let pdf_width = if *width == 0.0 { 0.0 } else { *width / unit };
    if !pdf_width.is_finite() {
        return Err(AnnotationError::Pdf(
            "an annotation border width is not representable in user space".to_owned(),
        ));
    }
    let mut border = Dict::new();
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /W key"
    )]
    border.insert(Name::from("W"), number(pdf_width));
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /S key"
    )]
    border.insert(Name::from("S"), Value::Name(Name::from(style.as_str())));
    if !dashes.is_empty() {
        let mut pdf_dashes = BudgetVec::new(budget);
        pdf_dashes.try_reserve_exact(dashes.len())?;
        for &dash in dashes {
            pdf_dashes.try_push(if dash == 0.0 { 0.0 } else { dash / unit })?;
        }
        if !valid_dashes(&pdf_dashes) {
            return Err(AnnotationError::Pdf(
                "annotation border dashes are not representable in user space".to_owned(),
            ));
        }
        let mut dash_values = BudgetVec::new(budget);
        dash_values.try_reserve_exact(pdf_dashes.len())?;
        for &dash in &pdf_dashes {
            dash_values.try_push(number(dash))?;
        }
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /D key"
        )]
        border.insert(
            Name::from("D"),
            Value::Array(Array::from(dash_values.into_vec())),
        );
    }
    Ok(Value::Dict(border))
}

/// Validates and builds the `/BE` dictionary of a cloudy border of `intensity`
/// on an annotation of `subtype`.
pub(super) fn cloud_effect(subtype: &str, intensity: f64) -> Result<Value> {
    if !matches!(subtype, "Square" | "Circle" | "Polygon") {
        return Err(AnnotationError::Pdf(
            "only a square, a circle, or a polygon has a cloudy border".to_owned(),
        ));
    }
    if !(intensity.is_finite() && intensity > 0.0 && intensity <= 2.0) {
        return Err(AnnotationError::Pdf(
            "a cloudy border's intensity is greater than 0 and at most 2".to_owned(),
        ));
    }
    let mut effect = Dict::new();
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /S key"
    )]
    effect.insert(Name::from("S"), Value::Name(Name::from("C")));
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /I key"
    )]
    effect.insert(Name::from("I"), number(intensity));
    Ok(Value::Dict(effect))
}

/// Validates and builds the `/LE` array of a line's start and end styles.
pub(super) fn line_endings_value((start, end): &(String, String)) -> Result<Value> {
    for style in [start, end] {
        if !LINE_ENDINGS.contains(&style.as_str()) {
            return Err(AnnotationError::Pdf(format!(
                "unknown line ending {style:?}"
            )));
        }
    }
    Ok(Value::Array(Array::from(vec![
        Value::Name(Name::from(start.as_str())),
        Value::Name(Name::from(end.as_str())),
    ])))
}

/// Converts display-space point lists into the PDF user-space entry of a line
/// (`/L`), polygon or polyline (`/Vertices`), or ink annotation (`/InkList`).
/// Settling fits `/Rect` to them.
pub(super) fn point_entry(
    annots: &PageAnnots<'_>,
    subtype: &str,
    lists: &[Vec<(f64, f64)>],
) -> Result<(&'static str, Value)> {
    annots
        .budget
        .check(LimitCode::AnnotationPointCount, lists.len() as u64)?;
    let mut count = 0usize;
    for points in lists {
        count = count.saturating_add(points.len());
        annots
            .budget
            .check(LimitCode::AnnotationPointCount, count as u64)?;
    }
    let fits = match subtype {
        "Line" => lists.len() == 1 && lists[0].len() == 2,
        "Polygon" | "PolyLine" => lists.len() == 1 && lists[0].len() >= 2,
        "Ink" => !lists.is_empty() && lists.iter().all(|points| !points.is_empty()),
        _ => {
            return Err(AnnotationError::Pdf(
                "only lines, polygons, polylines, and ink have points".to_owned(),
            ));
        }
    };
    if !fits {
        return Err(AnnotationError::Pdf(format!(
            "a {subtype} annotation has {}",
            match subtype {
                "Line" => "two points",
                "Ink" => "one or more strokes of one or more points",
                _ => "two or more vertices",
            }
        )));
    }
    let mut arrays = BudgetVec::new(annots.budget);
    arrays.try_reserve_exact(lists.len())?;
    for points in lists {
        let mut array = BudgetVec::new(annots.budget);
        array.try_reserve_exact(points.len().saturating_mul(2))?;
        for &(x, y) in points {
            if !(x.is_finite() && y.is_finite()) {
                return Err(AnnotationError::Pdf(
                    "annotation points are finite".to_owned(),
                ));
            }
            let (x, y) = to_pdf(annots.crop, annots.rotation, annots.unit, x, y);
            annots.budget.poll()?;
            array.try_extend_from_slice(&[number(x), number(y)])?;
        }
        arrays.try_push(Value::Array(Array::from(array.into_vec())))?;
    }
    Ok(match subtype {
        "Line" => ("L", arrays.pop().expect("validated one list")),
        "Ink" => ("InkList", Value::Array(Array::from(arrays.into_vec()))),
        _ => ("Vertices", arrays.pop().expect("validated one list")),
    })
}
