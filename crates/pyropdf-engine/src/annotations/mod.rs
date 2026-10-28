//! Appearance streams of annotations, and the settler that draws them
//! (DESIGN.md section 7).
//!
//! An edit that changes how an annotation looks marks its appearance stale;
//! [`AppearanceSettler`] then draws it once per transaction and writes the
//! drawing as a new form XObject that `/AP /N` names, so that a stream that
//! other annotations share is never overwritten. It draws the kinds that
//! [`draws`] accepts and leaves the others as they are. The `/Rect` of text
//! markup, lines, polygons, and ink follows their geometry, so settling
//! fits it again too.
//!
//! A drawing is made on a canvas over the annotation's `/Rect` whose `y`
//! axis points down, as pylopdf's appearance engine draws it, and the
//! content flips it into the form's space. Ported from pylopdf's
//! `rust/src/annotation_appearance.rs` at
//! ebb01d3c4c742046e8ce7fd72957cd15a2933321, without its canvas.

mod icons;
mod shapes;

use std::fmt::Write as _;
use std::sync::Arc;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::pages::MAX_CHAIN;
use pyropdf_store::settle::Settler;
use pyropdf_store::value::{Array, Bytes, Dict, Name, Stream, Value};
use pyropdf_store::version::{Transaction, Version};
use pyropdf_store::{Error, Result};

/// The most points that one annotation's geometry holds, a squiggly line's
/// zigzag included.
pub const MAX_ANNOTATION_POINTS: usize = 65_536;

/// The most quadrilaterals that one text-markup annotation holds.
pub const MAX_QUADS: usize = 4_096;

/// The control-point distance of a quarter circle drawn as a Bézier curve.
const KAPPA: f64 = 0.552_284_749_830_793_4;

/// The blend modes of ISO 32000-2 Table 134, which `/BM` may name.
const BLEND_MODES: [&str; 16] = [
    "Normal",
    "Multiply",
    "Screen",
    "Overlay",
    "Darken",
    "Lighten",
    "ColorDodge",
    "ColorBurn",
    "HardLight",
    "SoftLight",
    "Difference",
    "Exclusion",
    "Hue",
    "Saturation",
    "Color",
    "Luminosity",
];

/// The most dash lengths that a border pattern holds.
pub const MAX_BORDER_DASHES: usize = 32;

pub use shapes::{LINE_ENDINGS, cloud_radius, ending_size};

/// A quadrilateral of text markup, `[TL, TR, BL, BR]`, as `/QuadPoints`
/// lists its corners.
pub type Quad = [(f64, f64); 4];

/// The point lists of a line, polygon, polyline, or ink annotation: one
/// list, or one per ink stroke.
pub type PointLists = Vec<Vec<(f64, f64)>>;

/// Returns whether the settler draws annotations of `subtype`.
#[must_use]
pub fn draws(subtype: &[u8]) -> bool {
    is_text_markup(subtype)
        || has_points(subtype)
        || matches!(subtype, b"Text" | b"Square" | b"Circle")
}

/// Returns whether `subtype` is a line, polygon, polyline, or ink
/// annotation, which `/L`, `/Vertices`, or `/InkList` place.
#[must_use]
pub fn has_points(subtype: &[u8]) -> bool {
    matches!(subtype, b"Line" | b"Polygon" | b"PolyLine" | b"Ink")
}

/// Returns whether `subtype` is text markup, which `/QuadPoints` place.
#[must_use]
pub fn is_text_markup(subtype: &[u8]) -> bool {
    matches!(
        subtype,
        b"Highlight" | b"Underline" | b"StrikeOut" | b"Squiggly"
    )
}

/// Returns the zigzag segments of a squiggly line `length` points long.
#[must_use]
pub fn squiggly_segments(length: f64) -> usize {
    // A float-to-integer cast saturates, and the length is finite.
    #[expect(
        clippy::cast_possible_truncation,
        clippy::cast_sign_loss,
        reason = "the count is at least one and saturates"
    )]
    let segments = (length / 2.0).ceil().max(1.0) as usize;
    segments
}

/// The settler of [`Structure::Appearance`](pyropdf_store::settle::Structure::Appearance).
#[derive(Clone, Copy, Debug, Default)]
pub struct AppearanceSettler;

impl Settler for AppearanceSettler {
    fn settle(
        &self,
        transaction: &mut Transaction,
        numbers: &[u32],
        budget: &Budget,
    ) -> Result<()> {
        for &number in numbers {
            let Some(object) = transaction.version().get(number, budget)? else {
                continue;
            };
            let Value::Dict(dict) = &**object.value() else {
                continue;
            };
            let reader = Reader {
                version: transaction.version(),
                budget,
            };
            // An annotation that cannot be drawn still has its rectangle
            // fitted to its geometry.
            let (form, rect) = match appearance(&reader, dict)? {
                Some(drawn) => (Some(drawn.form()), drawn.fitted.then_some(drawn.rect)),
                None => (None, fitted_rect(&reader, dict)?),
            };
            if form.is_none() && rect.is_none() {
                continue;
            }
            charge_copy(CopyPart::Dict(dict), budget)?;
            let mut dict = dict.clone();
            if let Some(form) = form {
                let reference = transaction.allocate()?;
                transaction.write(reference.number(), &Value::from(form), budget)?;
                let mut appearances = Dict::new();
                appearances.insert(Name::from("N"), Value::Reference(reference));
                dict.insert(Name::from("AP"), Value::Dict(appearances));
            }
            if let Some(rect) = rect {
                dict.insert(Name::from("Rect"), numbers_array(&rect));
            }
            transaction.write(number, &Value::Dict(dict), budget)?;
        }
        Ok(())
    }
}

/// Reads the values of one version, following references.
pub struct Reader<'a> {
    /// The version that the values belong to.
    pub version: &'a Version,
    /// The budget of the read.
    pub budget: &'a Budget,
}

/// Resolving a direct value borrows it; only indirect objects need an Arc.
enum Resolved<'a> {
    Borrowed(&'a Value),
    Shared(Arc<Value>),
}

impl std::ops::Deref for Resolved<'_> {
    type Target = Value;

    fn deref(&self) -> &Value {
        match self {
            Self::Borrowed(value) => value,
            Self::Shared(value) => value,
        }
    }
}

impl<'b> Reader<'b> {
    /// Follows at most MAX_CHAIN references. Missing objects read as null;
    /// exceeding the traversal bound is an error, not missing geometry.
    fn resolve<'v>(&self, value: &'v Value) -> Result<Resolved<'v>> {
        let &Value::Reference(mut reference) = value else {
            return Ok(Resolved::Borrowed(value));
        };
        for _ in 0..MAX_CHAIN {
            let Some(object) = self
                .version
                .get(reference.number(), self.budget)?
                .filter(|object| object.generation() == reference.generation())
            else {
                return Ok(Resolved::Borrowed(&Value::Null));
            };
            match &**object.value() {
                Value::Reference(next) => reference = *next,
                _ => return Ok(Resolved::Shared(Arc::clone(object.value()))),
            }
        }
        Err(Error::malformed(
            "an annotation appearance reference chain is too long",
        ))
    }

    fn entry<'v>(&self, dict: &'v Dict, key: &str) -> Result<Option<Resolved<'v>>> {
        dict.get(key)
            .map(|value| self.resolve(value))
            .transpose()
            .map(|value| value.filter(|value| !matches!(**value, Value::Null)))
    }

    fn number(&self, value: &Value) -> Result<Option<f64>> {
        Ok(match &*self.resolve(value)? {
            #[expect(
                clippy::cast_precision_loss,
                reason = "PDF numbers are reals, which an integer approximates"
            )]
            Value::Integer(value) => Some(*value as f64),
            Value::Real(value) => Some(value.value()),
            _ => None,
        }
        .filter(|value| value.is_finite()))
    }

    fn name(&self, dict: &Dict, key: &str) -> Result<Option<Box<[u8]>>> {
        Ok(match self.entry(dict, key)?.as_deref() {
            Some(Value::Name(name)) => {
                let mut bytes = BudgetVec::new(self.budget);
                bytes.try_extend_from_slice(name.as_bytes())?;
                Some(bytes.into_vec().into_boxed_slice())
            }
            _ => None,
        })
    }

    /// Reads up to `max` numbers of the array `value`, or `None` for
    /// anything else.
    fn numbers_of(&self, value: &Value, max: usize) -> Result<Option<BudgetVec<'b, f64>>> {
        let resolved = self.resolve(value)?;
        let Value::Array(items) = &*resolved else {
            return Ok(None);
        };
        if items.len() > max {
            return Ok(None);
        }
        let mut numbers = BudgetVec::new(self.budget);
        numbers.try_reserve_exact(items.len())?;
        for item in items.iter() {
            match self.number(item)? {
                Some(number) => numbers.try_push(number)?,
                None => return Ok(None),
            }
        }
        Ok(Some(numbers))
    }

    fn numbers(&self, dict: &Dict, key: &str, max: usize) -> Result<Option<BudgetVec<'b, f64>>> {
        match dict.get(key) {
            Some(value) => self.numbers_of(value, max),
            None => Ok(None),
        }
    }

    /// Reads the normalized `/Rect`, or `None` without a finite one.
    fn rect(&self, dict: &Dict) -> Result<Option<[f64; 4]>> {
        Ok(self.numbers(dict, "Rect", 4)?.and_then(|values| {
            let [x0, y0, x1, y1] = values[..] else {
                return None;
            };
            Some([x0.min(x1), y0.min(y1), x0.max(x1), y0.max(y1)])
        }))
    }

    /// Reads a `/C` or `/IC` array as RGB: gray, RGB, or CMYK converted
    /// naively, as MuPDF does; `None` for an empty or unusable array.
    fn color(&self, dict: &Dict, key: &str) -> Result<Option<(f64, f64, f64)>> {
        let Some(mut values) = self.numbers(dict, key, 4)? else {
            return Ok(None);
        };
        for value in values.as_mut_slice() {
            *value = value.clamp(0.0, 1.0);
        }
        Ok(match values[..] {
            [gray] => Some((gray, gray, gray)),
            [red, green, blue] => Some((red, green, blue)),
            [cyan, magenta, yellow, black] => Some((
                (1.0 - cyan) * (1.0 - black),
                (1.0 - magenta) * (1.0 - black),
                (1.0 - yellow) * (1.0 - black),
            )),
            _ => None,
        })
    }

    /// Reads the `/CA` opacity: 1 when absent, or `None` when it is no
    /// number from 0 to 1, since an appearance is not invented for an
    /// opacity that the author wrote but no reader can apply.
    fn opacity(&self, dict: &Dict) -> Result<Option<f64>> {
        match dict.get("CA") {
            None => Ok(Some(1.0)),
            Some(value) => Ok(self
                .number(value)?
                .filter(|value| (0.0..=1.0).contains(value))),
        }
    }

    /// Reads the `/BM` blend mode, `None` for Normal or an unknown one.
    fn blend_mode(&self, dict: &Dict) -> Result<Option<&'static str>> {
        let Some(name) = self.name(dict, "BM")? else {
            return Ok(None);
        };
        Ok(BLEND_MODES
            .iter()
            .skip(1)
            .find(|mode| mode.as_bytes() == &*name)
            .copied())
    }

    /// Reads text-markup quadrilaterals, rejecting excessive geometry.
    fn quads(&self, dict: &Dict, subtype: &[u8]) -> Result<Option<BudgetVec<'b, Quad>>> {
        let Some(value) = self.entry(dict, "QuadPoints")? else {
            return Ok(None);
        };
        if let Value::Array(items) = &*value {
            self.budget.check(
                LimitCode::AnnotationQuadCount,
                items.len().div_ceil(8) as u64,
            )?;
        }
        let Some(values) = self.numbers_of(&value, 8 * MAX_QUADS)? else {
            return Ok(None);
        };
        if values.is_empty() || !values.len().is_multiple_of(8) {
            return Ok(None);
        }
        let mut quads = BudgetVec::new(self.budget);
        for q in values.as_chunks::<8>().0 {
            quads.try_push([(q[0], q[1]), (q[2], q[3]), (q[4], q[5]), (q[6], q[7])])?;
        }
        if subtype == b"Squiggly" {
            self.budget.check(
                LimitCode::AnnotationPointCount,
                zigzag_points(&quads) as u64,
            )?;
        }
        Ok(Some(quads))
    }

    /// Reads the border that `/BS`, else `/Border`, describes, or `None`
    /// without either or with an unusable `/Border`.
    pub fn border(&self, dict: &Dict) -> Result<Option<Border>> {
        if let Some(style) = self.entry(dict, "BS")? {
            let Value::Dict(style) = &*style else {
                return self.legacy_border(dict);
            };
            let width = match style.get("W") {
                Some(value) => self.number(value)?.filter(|width| *width >= 0.0),
                None => None,
            }
            .unwrap_or(1.0);
            let dashed = self.name(style, "S")?.as_deref() == Some(b"D".as_slice());
            let dashes = if dashed {
                self.dashes(style.get("D"))?
                    .filter(|dashes| valid_dashes(dashes))
                    .map(BudgetVec::into_vec)
                    .unwrap_or_else(|| vec![3.0])
            } else {
                Vec::new()
            };
            return Ok(Some(Border { width, dashes }));
        }
        self.legacy_border(dict)
    }

    fn dashes(&self, value: Option<&Value>) -> Result<Option<BudgetVec<'b, f64>>> {
        let Some(value) = value else { return Ok(None) };
        let resolved = self.resolve(value)?;
        if let Value::Array(items) = &*resolved {
            self.budget
                .check(LimitCode::AnnotationDashCount, items.len() as u64)?;
        }
        self.numbers_of(&resolved, MAX_BORDER_DASHES)
    }

    /// Reads the `[h v width dashes]` array of `/Border`.
    fn legacy_border(&self, dict: &Dict) -> Result<Option<Border>> {
        let Some(items) = self.entry(dict, "Border")? else {
            return Ok(None);
        };
        let Value::Array(items) = &*items else {
            return Ok(None);
        };
        if !(3..=4).contains(&items.len()) {
            return Ok(None);
        }
        let items = items.as_slice();
        let Some(width) = self.number(&items[2])?.filter(|width| *width >= 0.0) else {
            return Ok(None);
        };
        let dashes = match items.get(3) {
            Some(pattern) => match self.dashes(Some(pattern))? {
                Some(dashes) if valid_dashes(&dashes) => dashes.into_vec(),
                _ => return Ok(None),
            },
            None => Vec::new(),
        };
        Ok(Some(Border { width, dashes }))
    }

    /// Reads the intensity of a cloudy border, `/BE` with `/S /C`, clamped
    /// from 0 to 2, or 0 without one (ISO 32000-2 Table 167).
    fn cloud_intensity(&self, dict: &Dict) -> Result<f64> {
        let Some(effect) = self.entry(dict, "BE")? else {
            return Ok(0.0);
        };
        let Value::Dict(effect) = &*effect else {
            return Ok(0.0);
        };
        if self.name(effect, "S")?.as_deref() != Some(b"C".as_slice()) {
            return Ok(0.0);
        }
        Ok(match effect.get("I") {
            Some(value) => self.number(value)?.unwrap_or(0.0),
            None => 0.0,
        }
        .clamp(0.0, 2.0))
    }

    /// Reads the start and end styles of `/LE`, by index into
    /// [`LINE_ENDINGS`]; an absent or unknown style is `None`, index 0.
    pub fn line_endings(&self, dict: &Dict) -> Result<[usize; 2]> {
        let Some(items) = self.entry(dict, "LE")? else {
            return Ok([0, 0]);
        };
        let Value::Array(items) = &*items else {
            return Ok([0, 0]);
        };
        let mut endings = [0; 2];
        for (ending, item) in endings.iter_mut().zip(items.iter()) {
            if let Value::Name(name) = &*self.resolve(item)? {
                *ending = LINE_ENDINGS
                    .iter()
                    .position(|known| known.as_bytes() == name.as_bytes())
                    .unwrap_or(0);
            }
        }
        Ok(endings)
    }

    /// Reads the `/RD` insets of a square or circle: left, top, right, and
    /// bottom, or zeros for unusable ones.
    fn rect_differences(&self, dict: &Dict, size: (f64, f64)) -> Result<[f64; 4]> {
        let Some(values) = self.numbers(dict, "RD", 4)? else {
            return Ok([0.0; 4]);
        };
        let [left, top, right, bottom] = values[..] else {
            return Ok([0.0; 4]);
        };
        if [left, top, right, bottom].iter().any(|value| *value < 0.0)
            || left + right >= size.0
            || top + bottom >= size.1
        {
            return Ok([0.0; 4]);
        }
        Ok([left, top, right, bottom])
    }

    /// Returns the array of numbers that holds the points of a line
    /// (`/L`), polygon or polyline (`/Vertices`), or ink annotation
    /// (`/InkList`), resolved, or `None` for another subtype or a missing
    /// array.
    fn point_array<'v>(&self, dict: &'v Dict, subtype: &[u8]) -> Result<Option<Resolved<'v>>> {
        let key = match subtype {
            b"Line" => "L",
            b"Polygon" | b"PolyLine" => "Vertices",
            b"Ink" => "InkList",
            _ => return Ok(None),
        };
        Ok(self
            .entry(dict, key)?
            .filter(|value| matches!(**value, Value::Array(_))))
    }

    /// Counts the points of a line, polygon, polyline, or ink annotation
    /// from the lengths of its arrays alone, as a read checks them against
    /// [`MAX_ANNOTATION_POINTS`] before reading them.
    pub fn point_count(&self, dict: &Dict, subtype: &[u8]) -> Result<usize> {
        let Some(array) = self.point_array(dict, subtype)? else {
            return Ok(0);
        };
        let Value::Array(items) = &*array else {
            return Ok(0);
        };
        if subtype != b"Ink" {
            return Ok(items.len().div_ceil(2));
        }
        // Empty or malformed strokes still count as visited containers.
        self.budget
            .check(LimitCode::AnnotationPointCount, items.len() as u64)?;
        let mut points = 0usize;
        for stroke in items.iter() {
            self.budget.poll()?;
            if let Value::Array(stroke) = &*self.resolve(stroke)? {
                points = points.saturating_add(stroke.len().div_ceil(2));
                self.budget
                    .check(LimitCode::AnnotationPointCount, points as u64)?;
            }
        }
        Ok(points)
    }

    /// Reads complete point lists. Malformed geometry is absent; excessive
    /// geometry refuses before allocation rather than appearing absent.
    pub fn point_lists(&self, dict: &Dict, subtype: &[u8]) -> Result<Option<PointLists>> {
        self.budget.check(
            LimitCode::AnnotationPointCount,
            self.point_count(dict, subtype)? as u64,
        )?;
        let Some(array) = self.point_array(dict, subtype)? else {
            return Ok(None);
        };
        let Value::Array(items) = &*array else {
            return Ok(None);
        };
        let pairs = |value: &Value| -> Result<Option<Vec<(f64, f64)>>> {
            let Some(numbers) = self.numbers_of(value, 2 * MAX_ANNOTATION_POINTS)? else {
                return Ok(None);
            };
            if !numbers.len().is_multiple_of(2) {
                return Ok(None);
            }
            let mut points = BudgetVec::new(self.budget);
            for &[x, y] in numbers.as_chunks::<2>().0 {
                self.budget.poll()?;
                points.try_push((x, y))?;
            }
            Ok(Some(points.into_vec()))
        };
        let mut strokes = BudgetVec::new(self.budget);
        if subtype != b"Ink" {
            if subtype == b"Line" && items.len() != 4 {
                return Ok(None);
            }
            let Some(points) = pairs(&array)? else {
                return Ok(None);
            };
            strokes.try_push(points)?;
        } else {
            for stroke in items.iter() {
                self.budget.poll()?;
                let Some(points) = pairs(stroke)? else {
                    return Ok(None);
                };
                strokes.try_push(points)?;
            }
        }
        Ok(Some(strokes.into_vec()))
    }
}

/// Returns the points that the zigzags of a squiggly line over `quads`
/// draw.
#[must_use]
pub fn zigzag_points(quads: &[Quad]) -> usize {
    quads
        .iter()
        .map(|quad| {
            let length = (quad[3].0 - quad[2].0).hypot(quad[3].1 - quad[2].1);
            if length.is_finite() {
                squiggly_segments(length).saturating_add(1)
            } else {
                usize::MAX
            }
        })
        .fold(0, usize::saturating_add)
}

/// The border of an annotation: its width and, for a dashed one, its dash
/// pattern.
#[derive(Clone, Debug, PartialEq)]
pub struct Border {
    /// The line width.
    pub width: f64,
    /// The dash and gap lengths, empty for a solid line.
    pub dashes: Vec<f64>,
}

/// Returns whether a dash array is one that a stroke can draw: finite,
/// non-negative lengths that do not sum to zero.
#[must_use]
pub fn valid_dashes(dashes: &[f64]) -> bool {
    !dashes.is_empty()
        && dashes.iter().all(|dash| dash.is_finite() && *dash >= 0.0)
        && dashes.iter().sum::<f64>() > 0.0
}

/// The paints of an appearance: the `/C` color, which strokes lines and
/// borders and fills highlights and icons; the `/IC` color, which fills
/// shapes and closed line endings; and the border's width and dashes.
struct Paint {
    stroke: Option<(f64, f64, f64)>,
    interior: Option<(f64, f64, f64)>,
    width: f64,
    dashes: Vec<f64>,
}

/// A stroke: its color, width, line cap and join, and dash pattern.
struct Stroke<'a> {
    color: (f64, f64, f64),
    width: f64,
    cap: u8,
    join: u8,
    dashes: &'a [f64],
}

impl Paint {
    /// Returns the border's stroke with `cap` and `join`, or `None` without
    /// a color or width.
    fn stroke(&self, cap: u8, join: u8) -> Option<Stroke<'_>> {
        self.stroke
            .filter(|_| self.width > 0.0)
            .map(|color| Stroke {
                color,
                width: self.width,
                cap,
                join,
                dashes: &self.dashes,
            })
    }

    /// Returns a solid stroke for line endings, which never dash.
    fn solid_stroke(&self) -> Option<Stroke<'_>> {
        self.stroke(0, 0).map(|stroke| Stroke {
            dashes: &[],
            ..stroke
        })
    }
}

/// A drawn appearance: its content in the form's space, the form's size,
/// the annotation's rectangle in user space, and the graphics state that
/// paints it.
pub struct Appearance {
    content: Bytes,
    size: (f64, f64),
    rect: [f64; 4],
    /// Whether the rectangle follows the geometry, as that of text markup,
    /// lines, polygons, and ink does.
    fitted: bool,
    alpha: f64,
    blend: Option<&'static str>,
}

impl Appearance {
    /// Returns the form XObject of the appearance, whose `/Matrix` maps its
    /// box onto the rectangle.
    #[must_use]
    pub fn form(&self) -> Stream {
        let (width, height) = self.size;
        let mut dict = Dict::new();
        dict.insert(Name::from("Type"), Value::Name(Name::from("XObject")));
        dict.insert(Name::from("Subtype"), Value::Name(Name::from("Form")));
        dict.insert(
            Name::from("BBox"),
            numbers_array(&[0.0, 0.0, width, height]),
        );
        dict.insert(
            Name::from("Matrix"),
            numbers_array(&[1.0, 0.0, 0.0, 1.0, self.rect[0], self.rect[1]]),
        );
        let mut resources = Dict::new();
        if self.alpha < 1.0 || self.blend.is_some() {
            let mut state = Dict::new();
            state.insert(Name::from("Type"), Value::Name(Name::from("ExtGState")));
            state.insert(Name::from("CA"), real(self.alpha));
            state.insert(Name::from("ca"), real(self.alpha));
            if let Some(blend) = self.blend {
                state.insert(Name::from("BM"), Value::Name(Name::from(blend)));
            }
            let mut states = Dict::new();
            states.insert(Name::from("G0"), Value::Dict(state));
            resources.insert(Name::from("ExtGState"), Value::Dict(states));
        }
        dict.insert(Name::from("Resources"), Value::Dict(resources));
        Stream::new(dict, self.content.clone())
    }
}

/// The geometry of an annotation: the quadrilaterals of text markup, the
/// point lists of a line, polygon, polyline, or ink, or none for a kind
/// that `/Rect` alone places.
enum Geometry {
    Quads(Vec<Quad>),
    Points(PointLists),
    Rect,
}

/// Reads the geometry of an annotation of `subtype`, or `None` when it is
/// missing or malformed.
fn geometry(reader: &Reader<'_>, dict: &Dict, subtype: &[u8]) -> Result<Option<Geometry>> {
    Ok(if is_text_markup(subtype) {
        reader
            .quads(dict, subtype)?
            .map(|quads| Geometry::Quads(quads.into_vec()))
    } else if has_points(subtype) {
        reader.point_lists(dict, subtype)?.map(Geometry::Points)
    } else {
        Some(Geometry::Rect)
    })
}

/// Returns the bounds of `points`, which are infinite for none.
fn bounds<'a>(points: impl Iterator<Item = &'a (f64, f64)>) -> [f64; 4] {
    points.fold(
        [
            f64::INFINITY,
            f64::INFINITY,
            f64::NEG_INFINITY,
            f64::NEG_INFINITY,
        ],
        |[x0, y0, x1, y1], &(x, y)| [x0.min(x), y0.min(y), x1.max(x), y1.max(y)],
    )
}

/// Returns the rectangle that `geometry` gives an annotation: the bounds of
/// the quadrilaterals of text markup, or the bounds of the points of a
/// line, polygon, polyline, or ink widened for the stroke, a polygon's
/// cloudy border, and the line endings; or `None` for another kind or
/// points without finite bounds.
fn fit(
    reader: &Reader<'_>,
    dict: &Dict,
    subtype: &[u8],
    geometry: &Geometry,
) -> Result<Option<[f64; 4]>> {
    let lists = match geometry {
        Geometry::Quads(quads) => return Ok(Some(bounds(quads.iter().flatten()))),
        Geometry::Points(lists) => lists,
        Geometry::Rect => return Ok(None),
    };
    let [x0, y0, x1, y1] = bounds(lists.iter().flatten());
    if ![x0, y0, x1, y1].iter().all(|value| value.is_finite()) {
        return Ok(None);
    }
    let width = reader.border(dict)?.map_or(1.0, |border| border.width);
    let mut pad = width / 2.0 + 1.0;
    let intensity = reader.cloud_intensity(dict)?;
    if intensity > 0.0 && subtype == b"Polygon" {
        pad += cloud_radius(intensity, width);
    }
    if reader.line_endings(dict)? != [0, 0] {
        pad += 2.0 * ending_size(width);
    }
    let rect = [x0 - pad, y0 - pad, x1 + pad, y1 + pad];
    if !rect.iter().all(|value| value.is_finite()) {
        return Err(Error::malformed(
            "an annotation fitted rectangle is nonfinite",
        ));
    }
    Ok(Some(rect))
}

/// Returns the rectangle that the geometry of the annotation `dict` gives
/// it, for text markup, lines, polygons, polylines, and ink, or `None` for
/// another kind or an unusable geometry.
pub fn fitted_rect(reader: &Reader<'_>, dict: &Dict) -> Result<Option<[f64; 4]>> {
    let Some(subtype) = reader.name(dict, "Subtype")? else {
        return Ok(None);
    };
    match geometry(reader, dict, &subtype)? {
        Some(geometry) => fit(reader, dict, &subtype, &geometry),
        None => Ok(None),
    }
}

/// Draws the normal appearance of the annotation `dict`, or returns `None`
/// for a kind that [`draws`] refuses, or for a dictionary without a usable
/// rectangle, geometry, or opacity.
pub fn appearance(reader: &Reader<'_>, dict: &Dict) -> Result<Option<Appearance>> {
    let Some(subtype) = reader.name(dict, "Subtype")? else {
        return Ok(None);
    };
    if !draws(&subtype) {
        return Ok(None);
    }
    let Some(geometry) = geometry(reader, dict, &subtype)? else {
        return Ok(None);
    };
    let fitted = fit(reader, dict, &subtype, &geometry)?;
    let rect = match (&geometry, fitted) {
        (_, Some(rect)) => rect,
        (Geometry::Rect, None) => match reader.rect(dict)? {
            Some(rect) => rect,
            None => return Ok(None),
        },
        _ => return Ok(None),
    };
    let [x0, y0, x1, y1] = rect;
    if !(x1 >= x0 && y1 >= y0) {
        return Ok(None);
    }
    let size = (x1 - x0, y1 - y0);
    if !(size.0.is_finite() && size.1.is_finite()) {
        return Err(Error::malformed(
            "an annotation appearance has nonfinite dimensions",
        ));
    }
    // The fixed-size form dictionary, resources, graphics state, and AP link.
    reader.budget.charge(LimitCode::MemorySize, 4096)?;
    let Some(alpha) = reader.opacity(dict)? else {
        return Ok(None);
    };
    let border = reader.border(dict)?;
    let paint = Paint {
        stroke: reader.color(dict, "C")?,
        interior: reader.color(dict, "IC")?,
        width: border.as_ref().map_or(1.0, |border| border.width),
        dashes: border.map(|border| border.dashes).unwrap_or_default(),
    };
    // A highlight multiplies unless /BM says otherwise.
    let blend = match reader.blend_mode(dict)? {
        Some(mode) => Some(mode),
        None if &*subtype == b"Highlight" && dict.get("BM").is_none() => Some("Multiply"),
        None => None,
    };
    let mut canvas = Canvas::new(size.1, reader.budget);
    if alpha < 1.0 || blend.is_some() {
        canvas.out.push_str("/G0 gs\n");
    }
    let to_canvas = |(x, y): (f64, f64)| (x - x0, y1 - y);
    match (&*subtype, &geometry) {
        (b"Text", _) => {
            let icon = reader.name(dict, "Name")?;
            icons::draw(
                &mut canvas,
                &paint,
                icon.as_deref().unwrap_or(b"Note"),
                size,
            );
        }
        (b"Square" | b"Circle", _) => {
            let insets = reader.rect_differences(dict, size)?;
            let intensity = reader.cloud_intensity(dict)?;
            shapes::boxed(
                &mut canvas,
                &paint,
                &*subtype == b"Circle",
                size,
                insets,
                intensity,
            )?;
        }
        (b"Highlight", Geometry::Quads(quads)) => {
            if let Some(color) = paint.stroke {
                for quad in quads {
                    // QuadPoints list the corners TL, TR, BL, BR.
                    let corners = [quad[0], quad[1], quad[3], quad[2]].map(to_canvas);
                    let coordinates = corners.map(|(x, y)| [x, y]).as_flattened().to_owned();
                    canvas.path("mlllh", &coordinates);
                    canvas.fill(color);
                }
            }
        }
        (_, Geometry::Quads(quads)) => {
            // Text-markup lines are one point wide, whatever the border.
            if let Some(color) = paint.stroke {
                for quad in quads {
                    let Some(line) = markup_line(&subtype, *quad, reader.budget)? else {
                        continue;
                    };
                    for (index, &point) in line.iter().enumerate() {
                        let (x, y) = to_canvas(point);
                        canvas.path(if index == 0 { "m" } else { "l" }, &[x, y]);
                    }
                    canvas.stroke(color, 1.0, 0, 0, &[]);
                }
            }
        }
        (_, Geometry::Points(lists)) => {
            let intensity = reader.cloud_intensity(dict)?;
            let endings = reader.line_endings(dict)?;
            if !shapes::lines(
                &mut canvas,
                &paint,
                &subtype,
                lists,
                intensity,
                endings,
                to_canvas,
            )? {
                return Ok(None);
            }
        }
        (_, Geometry::Rect) => return Ok(None),
    }
    Ok(Some(Appearance {
        content: Bytes::new(canvas.out.finish()?),
        size,
        rect,
        fitted: fitted.is_some(),
        alpha,
        blend,
    }))
}

fn midpoint(a: (f64, f64), b: (f64, f64)) -> (f64, f64) {
    (a.0 / 2.0 + b.0 / 2.0, a.1 / 2.0 + b.1 / 2.0)
}

/// Returns the points of an underline, strikeout, or squiggly line under a
/// quadrilateral `[TL, TR, BL, BR]`, or `None` for one without length or
/// height.
fn markup_line<'b>(
    subtype: &[u8],
    quad: Quad,
    budget: &'b Budget,
) -> Result<Option<BudgetVec<'b, (f64, f64)>>> {
    let [upper_left, upper_right, lower_left, lower_right] = quad;
    let top = midpoint(upper_left, upper_right);
    let bottom = midpoint(lower_left, lower_right);
    let (dx, dy) = (top.0 - bottom.0, top.1 - bottom.1);
    let cross = dx.hypot(dy);
    let length = (lower_right.0 - lower_left.0).hypot(lower_right.1 - lower_left.1);
    if !(cross.is_finite() && length.is_finite()) {
        return Err(Error::malformed(
            "an annotation line has nonfinite dimensions",
        ));
    }
    if cross <= f64::EPSILON || length <= f64::EPSILON {
        return Ok(None);
    }
    let mut points = BudgetVec::new(budget);
    // The normal points from the baseline into the quadrilateral.
    let normal = (dx / cross, dy / cross);
    let lifted =
        |point: (f64, f64), offset: f64| (point.0 + normal.0 * offset, point.1 + normal.1 * offset);
    match subtype {
        b"Underline" => {
            let inset = cross.min(1.0);
            points
                .try_extend_from_slice(&[lifted(lower_left, inset), lifted(lower_right, inset)])?;
        }
        b"StrikeOut" => points.try_extend_from_slice(&[
            midpoint(upper_left, lower_left),
            midpoint(upper_right, lower_right),
        ])?,
        _ => {
            let segments = squiggly_segments(length);
            let inset = cross.min(0.5);
            let amplitude = (cross - inset).clamp(0.0, 2.0);
            let count = segments.saturating_add(1);
            budget.check(LimitCode::AnnotationPointCount, count as u64)?;
            points.try_reserve_exact(count)?;
            for index in 0..=segments {
                #[expect(
                    clippy::cast_precision_loss,
                    reason = "the segments are at most MAX_ANNOTATION_POINTS"
                )]
                let t = index as f64 / segments as f64;
                let base = (
                    lower_left.0 + (lower_right.0 - lower_left.0) * t,
                    lower_left.1 + (lower_right.1 - lower_left.1) * t,
                );
                let offset = inset + if index % 2 == 0 { amplitude } else { 0.0 };
                points.try_push(lifted(base, offset))?;
            }
        }
    }
    Ok(Some(points))
}

/// Content over a canvas whose `y` axis points down from the top of a
/// form `height` tall.
struct Canvas<'b> {
    out: Output<'b>,
    height: f64,
}

impl<'b> Canvas<'b> {
    const fn new(height: f64, budget: &'b Budget) -> Self {
        Self {
            out: Output {
                bytes: BudgetVec::new(budget),
                refusal: None,
            },
            height,
        }
    }

    /// Appends a path: `m` and `l` take one point, `c` three, `h` none.
    fn path(&mut self, operators: &str, coordinates: &[f64]) {
        let mut points = coordinates.as_chunks::<2>().0.iter();
        for operator in operators.chars() {
            let count = match operator {
                'm' | 'l' => 1,
                'c' => 3,
                _ => 0,
            };
            for &[x, y] in points.by_ref().take(count) {
                let y = self.height - y;
                if !(x.is_finite() && y.is_finite()) {
                    self.out
                        .refusal
                        .get_or_insert_with(|| Error::malformed("an annotation path is nonfinite"));
                    return;
                }
                let _ = write!(self.out, "{} {} ", fmt(x), fmt(y));
            }
            self.out.push(operator);
            self.out.push('\n');
        }
    }

    fn color(&mut self, (red, green, blue): (f64, f64, f64), operator: &str) {
        let _ = writeln!(
            self.out,
            "{} {} {} {operator}",
            fmt(red),
            fmt(green),
            fmt(blue)
        );
    }

    /// Fills the path with nonzero winding.
    fn fill(&mut self, color: (f64, f64, f64)) {
        self.color(color, "rg");
        self.out.push_str("f\n");
    }

    /// Strokes the path with `width`, line `cap` and `join`, and `dashes`.
    fn stroke(&mut self, color: (f64, f64, f64), width: f64, cap: u8, join: u8, dashes: &[f64]) {
        self.stroke_state(color, width, cap, join, dashes);
        self.out.push_str("S\n");
    }

    /// Fills the path with `fill`, strokes it with `stroke`, or both; with
    /// neither it ends the path unpainted.
    fn draw(&mut self, fill: Option<(f64, f64, f64)>, stroke: Option<&Stroke<'_>>) {
        if let Some(fill) = fill {
            self.color(fill, "rg");
        }
        if let Some(stroke) = stroke {
            self.stroke_state(
                stroke.color,
                stroke.width,
                stroke.cap,
                stroke.join,
                stroke.dashes,
            );
        }
        self.out.push_str(match (fill, stroke) {
            (Some(_), Some(_)) => "B\n",
            (Some(_), None) => "f\n",
            (None, Some(_)) => "S\n",
            (None, None) => "n\n",
        });
    }

    /// Fills the path with `fill`, then strokes it with round caps and
    /// joins.
    fn fill_stroke(&mut self, fill: (f64, f64, f64), stroke: (f64, f64, f64), width: f64) {
        self.color(fill, "rg");
        self.stroke_state(stroke, width, 1, 1, &[]);
        self.out.push_str("B\n");
    }

    fn stroke_state(
        &mut self,
        color: (f64, f64, f64),
        width: f64,
        cap: u8,
        join: u8,
        dashes: &[f64],
    ) {
        self.color(color, "RG");
        let _ = write!(self.out, "{} w {cap} J {join} j [", fmt(width));
        for (index, dash) in dashes.iter().enumerate() {
            if index > 0 {
                self.out.push(' ');
            }
            let _ = write!(self.out, "{}", fmt(*dash));
        }
        self.out.push_str("] 0 d\n");
    }
}

/// Returns the coordinates of the ellipse inscribed in a rectangle, as a
/// move and four Béziers.
fn ellipse(x0: f64, y0: f64, x1: f64, y1: f64) -> [f64; 26] {
    let (cx, cy) = (x0 / 2.0 + x1 / 2.0, y0 / 2.0 + y1 / 2.0);
    let (rx, ry) = ((x1 - x0) / 2.0, (y1 - y0) / 2.0);
    let (kx, ky) = (rx * KAPPA, ry * KAPPA);
    [
        x1,
        cy,
        x1,
        cy + ky,
        cx + kx,
        y1,
        cx,
        y1,
        cx - kx,
        y1,
        x0,
        cy + ky,
        x0,
        cy,
        x0,
        cy - ky,
        cx - kx,
        y0,
        cx,
        y0,
        cx + kx,
        y0,
        x1,
        cy - ky,
        x1,
        cy,
    ]
}

/// Content output is charged before growth; retain the first refusal while
/// the fixed icon routines finish, then propagate it instead of partial bytes.
struct Output<'b> {
    bytes: BudgetVec<'b, u8>,
    refusal: Option<Error>,
}

impl std::fmt::Write for Output<'_> {
    fn write_str(&mut self, text: &str) -> std::fmt::Result {
        if self.refusal.is_some() {
            return Err(std::fmt::Error);
        }
        let result = self
            .bytes
            .budget()
            .check(
                LimitCode::AnnotationAppearanceSize,
                self.bytes.len().saturating_add(text.len()) as u64,
            )
            .and_then(|()| self.bytes.try_extend_from_slice(text.as_bytes()));
        if let Err(error) = result {
            self.refusal = Some(error.into());
            return Err(std::fmt::Error);
        }
        Ok(())
    }
}

impl Output<'_> {
    fn push_str(&mut self, text: &str) {
        let _ = self.write_str(text);
    }
    fn push(&mut self, ch: char) {
        let _ = self.write_char(ch);
    }
    fn finish(self) -> Result<Vec<u8>> {
        match self.refusal {
            Some(error) => Err(error),
            None => Ok(self.bytes.into_vec()),
        }
    }
}

/// Rust's display format preserves small coordinates without PDF-invalid
/// exponent notation. Normalize negative zero as well.
fn fmt(value: f64) -> String {
    if value == 0.0 {
        "0".to_owned()
    } else {
        value.to_string()
    }
}

/// A borrowed part of an object whose clone allocates owned values.
enum CopyPart<'a> {
    Value(&'a Value),
    Array(&'a Array),
    Dict(&'a Dict),
}

/// Charges owned buffers before a clone, including unknown nested fields.
/// The explicit work stack is budgeted; shared source spans and stream bytes
/// need no copy. Array and dictionary capacity includes room for an edit.
fn charge_copy(part: CopyPart<'_>, budget: &Budget) -> Result<()> {
    let mut pending = BudgetVec::new(budget);
    pending.try_push(part)?;
    while let Some(part) = pending.pop() {
        budget.poll()?;
        let bytes = match part {
            CopyPart::Value(value) => match value {
                Value::Name(name) => name.as_bytes().len(),
                Value::String(text) => text.as_bytes().len(),
                Value::Array(array) => {
                    pending.try_push(CopyPart::Array(array))?;
                    0
                }
                Value::Dict(dict) => {
                    pending.try_push(CopyPart::Dict(dict))?;
                    0
                }
                Value::Stream(stream) => {
                    pending.try_push(CopyPart::Dict(stream.dict()))?;
                    0
                }
                _ => 0,
            },
            CopyPart::Array(array) => {
                for value in array {
                    pending.try_push(CopyPart::Value(value))?;
                }
                array.len().saturating_mul(2 * size_of::<Value>())
            }
            CopyPart::Dict(dict) => {
                let mut bytes = dict.len().saturating_mul(2 * size_of::<(Name, Value)>());
                for (key, value) in dict.iter() {
                    bytes = bytes.saturating_add(key.as_bytes().len());
                    pending.try_push(CopyPart::Value(value))?;
                }
                bytes
            }
        };
        budget.charge(LimitCode::MemorySize, bytes as u64)?;
    }
    Ok(())
}

/// Returns a number as an integer when it is one, else a real.
fn real(value: f64) -> Value {
    #[expect(
        clippy::cast_possible_truncation,
        reason = "the value is integral and within the range of an i64"
    )]
    if value.fract() == 0.0 && value.abs() < 1e15 {
        Value::Integer(value as i64)
    } else {
        Value::from(value)
    }
}

fn numbers_array(values: &[f64]) -> Value {
    Value::Array(values.iter().map(|value| real(*value)).collect::<Array>())
}

#[cfg(test)]
mod tests;
