//! Appearance streams of annotations, and the settler that draws them
//! (DESIGN.md section 7).
//!
//! An edit that changes how an annotation looks marks its appearance stale;
//! [`AppearanceSettler`] then draws it once per transaction and writes the
//! drawing as a new form XObject that `/AP /N` names, so that a stream that
//! other annotations share is never overwritten. It draws the kinds that
//! [`draws`] accepts and leaves the others as they are.
//!
//! A drawing is made on a canvas over the annotation's `/Rect` whose `y`
//! axis points down, as pylopdf's appearance engine draws it, and the
//! content flips it into the form's space. Ported from pylopdf's
//! `rust/src/annotation_appearance.rs` at
//! ebb01d3c4c742046e8ce7fd72957cd15a2933321, without its canvas.

mod icons;

use std::fmt::Write as _;
use std::sync::Arc;

use pyropdf_store::Result;
use pyropdf_store::budget::{Budget, LimitCode};
use pyropdf_store::pages::MAX_CHAIN;
use pyropdf_store::settle::Settler;
use pyropdf_store::value::{Array, Bytes, Dict, Name, Stream, Value};
use pyropdf_store::version::{Transaction, Version};

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

/// A quadrilateral of text markup, `[TL, TR, BL, BR]`, as `/QuadPoints`
/// lists its corners.
pub type Quad = [(f64, f64); 4];

/// Returns whether the settler draws annotations of `subtype`.
#[must_use]
pub fn draws(subtype: &[u8]) -> bool {
    is_text_markup(subtype) || subtype == b"Text"
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
            let Some(drawn) = appearance(&reader, dict)? else {
                continue;
            };
            let mut dict = dict.clone();
            let form = drawn.form();
            budget.charge(LimitCode::MemorySize, form.data().len() as u64)?;
            let reference = transaction.allocate()?;
            transaction.write(reference.number(), &Value::from(form), budget)?;
            if drawn.markup {
                dict.insert(Name::from("Rect"), numbers_array(&drawn.rect));
            }
            let mut appearances = Dict::new();
            appearances.insert(Name::from("N"), Value::Reference(reference));
            dict.insert(Name::from("AP"), Value::Dict(appearances));
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

impl Reader<'_> {
    /// Follows `value` through at most [`MAX_CHAIN`] references; a missing
    /// object, or a longer chain, reads as null.
    fn resolve(&self, value: &Value) -> Result<Arc<Value>> {
        let &Value::Reference(mut reference) = value else {
            return Ok(Arc::new(value.clone()));
        };
        for _ in 0..MAX_CHAIN {
            let Some(object) = self
                .version
                .get(reference.number(), self.budget)?
                .filter(|object| object.generation() == reference.generation())
            else {
                break;
            };
            match &**object.value() {
                Value::Reference(next) => reference = *next,
                _ => return Ok(Arc::clone(object.value())),
            }
        }
        Ok(Arc::new(Value::Null))
    }

    fn entry(&self, dict: &Dict, key: &str) -> Result<Option<Arc<Value>>> {
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
            Some(Value::Name(name)) => Some(name.as_bytes().into()),
            _ => None,
        })
    }

    /// Reads up to `max` numbers of the array `value`, or `None` for
    /// anything else.
    fn numbers_of(&self, value: &Value, max: usize) -> Result<Option<Vec<f64>>> {
        let resolved = self.resolve(value)?;
        let Value::Array(items) = &*resolved else {
            return Ok(None);
        };
        if items.len() > max {
            return Ok(None);
        }
        let mut numbers = Vec::with_capacity(items.len());
        for item in items.iter() {
            match self.number(item)? {
                Some(number) => numbers.push(number),
                None => return Ok(None),
            }
        }
        Ok(Some(numbers))
    }

    fn numbers(&self, dict: &Dict, key: &str, max: usize) -> Result<Option<Vec<f64>>> {
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
        let Some(values) = self.numbers(dict, key, 4)? else {
            return Ok(None);
        };
        let values: Vec<f64> = values.into_iter().map(|v| v.clamp(0.0, 1.0)).collect();
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

    /// Reads the quadrilaterals of text markup, each `[TL, TR, BL, BR]` in
    /// user space, or `None` for malformed geometry or more than
    /// [`MAX_QUADS`] of them or, for a squiggly line, more than
    /// [`MAX_ANNOTATION_POINTS`] zigzag points.
    fn quads(&self, dict: &Dict, subtype: &[u8]) -> Result<Option<Vec<Quad>>> {
        let Some(values) = self.numbers(dict, "QuadPoints", 8 * MAX_QUADS)? else {
            return Ok(None);
        };
        if values.is_empty() || !values.len().is_multiple_of(8) {
            return Ok(None);
        }
        let quads: Vec<Quad> = values
            .as_chunks::<8>()
            .0
            .iter()
            .map(|q| [(q[0], q[1]), (q[2], q[3]), (q[4], q[5]), (q[6], q[7])])
            .collect();
        if subtype == b"Squiggly" && zigzag_points(&quads) > MAX_ANNOTATION_POINTS {
            return Ok(None);
        }
        Ok(Some(quads))
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
                0
            }
        })
        .fold(0, usize::saturating_add)
}

/// The paints of an appearance: the `/C` color, which strokes lines and
/// fills highlights and icons.
struct Paint {
    stroke: Option<(f64, f64, f64)>,
}

/// A drawn appearance: its content in the form's space, the form's size,
/// the annotation's rectangle in user space, and the graphics state that
/// paints it.
pub struct Appearance {
    content: Vec<u8>,
    size: (f64, f64),
    rect: [f64; 4],
    /// Whether the rectangle follows the quadrilaterals of text markup.
    markup: bool,
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
        Stream::new(dict, Bytes::new(self.content.clone()))
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
    let markup = is_text_markup(&subtype);
    let quads = if markup {
        match reader.quads(dict, &subtype)? {
            Some(quads) => quads,
            None => return Ok(None),
        }
    } else {
        Vec::new()
    };
    // Text markup covers its quadrilaterals, whatever /Rect says.
    let rect = if markup {
        quads.iter().flatten().fold(
            [
                f64::INFINITY,
                f64::INFINITY,
                f64::NEG_INFINITY,
                f64::NEG_INFINITY,
            ],
            |[x0, y0, x1, y1], &(x, y)| [x0.min(x), y0.min(y), x1.max(x), y1.max(y)],
        )
    } else {
        match reader.rect(dict)? {
            Some(rect) => rect,
            None => return Ok(None),
        }
    };
    let [x0, y0, x1, y1] = rect;
    if !(x1 >= x0 && y1 >= y0) {
        return Ok(None);
    }
    let size = (x1 - x0, y1 - y0);
    let Some(alpha) = reader.opacity(dict)? else {
        return Ok(None);
    };
    let paint = Paint {
        stroke: reader.color(dict, "C")?,
    };
    // A highlight multiplies unless /BM says otherwise.
    let blend = match reader.blend_mode(dict)? {
        Some(mode) => Some(mode),
        None if &*subtype == b"Highlight" && dict.get("BM").is_none() => Some("Multiply"),
        None => None,
    };
    let mut canvas = Canvas::new(size.1);
    if alpha < 1.0 || blend.is_some() {
        canvas.out.push_str("/G0 gs\n");
    }
    let to_canvas = |(x, y): (f64, f64)| (x - x0, y1 - y);
    match &*subtype {
        b"Text" => {
            let icon = reader.name(dict, "Name")?;
            icons::draw(
                &mut canvas,
                &paint,
                icon.as_deref().unwrap_or(b"Note"),
                size,
            );
        }
        b"Highlight" => {
            if let Some(color) = paint.stroke {
                for quad in &quads {
                    // QuadPoints list the corners TL, TR, BL, BR.
                    let corners = [quad[0], quad[1], quad[3], quad[2]].map(to_canvas);
                    let coordinates: Vec<f64> = corners.iter().flat_map(|&(x, y)| [x, y]).collect();
                    canvas.path("mlllh", &coordinates);
                    canvas.fill(color);
                }
            }
        }
        _ => {
            // Text-markup lines are one point wide, whatever the border.
            if let Some(color) = paint.stroke {
                for quad in &quads {
                    let Some(line) = markup_line(&subtype, *quad) else {
                        continue;
                    };
                    let mut operators = String::with_capacity(line.len());
                    let mut coordinates = Vec::with_capacity(line.len() * 2);
                    for (index, &point) in line.iter().enumerate() {
                        operators.push(if index == 0 { 'm' } else { 'l' });
                        let (x, y) = to_canvas(point);
                        coordinates.extend([x, y]);
                    }
                    canvas.path(&operators, &coordinates);
                    canvas.stroke(color, 1.0, 0, 0, &[]);
                }
            }
        }
    }
    Ok(Some(Appearance {
        content: canvas.out.into_bytes(),
        size,
        rect,
        markup,
        alpha,
        blend,
    }))
}

fn midpoint(a: (f64, f64), b: (f64, f64)) -> (f64, f64) {
    ((a.0 + b.0) / 2.0, (a.1 + b.1) / 2.0)
}

/// Returns the points of an underline, strikeout, or squiggly line under a
/// quadrilateral `[TL, TR, BL, BR]`, or `None` for one without length or
/// height.
fn markup_line(subtype: &[u8], quad: Quad) -> Option<Vec<(f64, f64)>> {
    let [upper_left, upper_right, lower_left, lower_right] = quad;
    let top = midpoint(upper_left, upper_right);
    let bottom = midpoint(lower_left, lower_right);
    let (dx, dy) = (top.0 - bottom.0, top.1 - bottom.1);
    let cross = dx.hypot(dy);
    let length = (lower_right.0 - lower_left.0).hypot(lower_right.1 - lower_left.1);
    if !(cross.is_finite() && cross > f64::EPSILON && length.is_finite() && length > f64::EPSILON) {
        return None;
    }
    // The normal points from the baseline into the quadrilateral.
    let normal = (dx / cross, dy / cross);
    let lifted =
        |point: (f64, f64), offset: f64| (point.0 + normal.0 * offset, point.1 + normal.1 * offset);
    match subtype {
        b"Underline" => {
            let inset = cross.min(1.0);
            Some(vec![lifted(lower_left, inset), lifted(lower_right, inset)])
        }
        b"StrikeOut" => Some(vec![
            midpoint(upper_left, lower_left),
            midpoint(upper_right, lower_right),
        ]),
        _ => {
            let segments = squiggly_segments(length);
            let inset = cross.min(0.5);
            let amplitude = (cross - inset).clamp(0.0, 2.0);
            let mut points = Vec::new();
            points.try_reserve_exact(segments + 1).ok()?;
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
                points.push(lifted(base, offset));
            }
            Some(points)
        }
    }
}

/// Content over a canvas whose `y` axis points down from the top of a
/// form `height` tall.
struct Canvas {
    out: String,
    height: f64,
}

impl Canvas {
    const fn new(height: f64) -> Self {
        Self {
            out: String::new(),
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
                let _ = write!(self.out, "{} {} ", fmt(x), fmt(self.height - y));
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
        let dashes: Vec<String> = dashes.iter().map(|dash| fmt(*dash)).collect();
        let _ = writeln!(
            self.out,
            "{} w {cap} J {join} j [{}] 0 d",
            fmt(width),
            dashes.join(" ")
        );
    }
}

/// Formats a number for content with at most four decimals.
fn fmt(v: f64) -> String {
    let s = format!("{v:.4}");
    let s = s.trim_end_matches('0').trim_end_matches('.');
    if s.is_empty() || s == "-" || s == "-0" {
        "0".to_owned()
    } else {
        s.to_owned()
    }
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
