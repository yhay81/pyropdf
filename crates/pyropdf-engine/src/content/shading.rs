//! Axial and radial shadings and luminosity soft masks, written as document
//! objects (DESIGN.md section 7, yhay81/pyropdf#632).
//!
//! [`Shadings`] writes into a transaction under the budget of its operation
//! and shares the objects that it writes when they are equal. Each caller
//! keeps one table for as long as its objects are worth sharing: a canvas for
//! its block, a Type 3 font for its glyphs. [`Shadings::paint`] returns the
//! shading, and the soft mask or the uniform alpha that its stops need, and
//! the caller names them in its own resources and content:
//!
//! - a canvas paints through a shading pattern from [`Shadings::pattern`],
//!   because a stroke has no other way to take a shading, and its matrix
//!   carries the canvas's transforms, since pattern space ignores the CTM;
//! - a glyph procedure clips to an outline and paints the shading with `sh`,
//!   which follows the CTM.
//!
//! A soft mask reads the CTM in force when `gs` sets it, so the mask follows
//! both, and its box and the gradient's coordinates are in the same space.
//!
//! PDF has only the two ends' extension, so [`Extend::Repeat`] and
//! [`Extend::Reflect`] draw the cycles that a painted box needs, as a Type 3
//! function of the one cycle's function object. That keeps the writer within
//! function types 2 and 3, which every reader has.

use std::collections::HashMap;
use std::fmt;
use std::mem::size_of;
use std::slice;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::value::{Bytes, Dict, Name, Reference, Stream, Value};
use pyropdf_store::version::Transaction;
use pyropdf_store::{Error, LimitExceeded, write};

/// The most cycles that a repeating gradient draws over what it paints: past
/// this many, a cycle is thinner than a pixel of any page a reader shows.
pub const MAX_CYCLES: u64 = 4096;

/// A cycle's part of a painted box that is thinner than this is dropped, so
/// that a reader that holds reals as `f32` keeps the bounds apart.
const CYCLE_TOLERANCE: f64 = 1e-6;

/// The largest magnitude of a real that readers hold as a 32-bit float.
const REAL_MAX: f64 = f32::MAX as f64;

/// A bound on the memory of one exponential function's dictionary, while its
/// stitching function is built: five entries and their arrays.
const SEGMENT_SIZE: u64 = 16 * size_of::<Value>() as u64;

/// A bound on the memory of one cycle's entries of a stitching function.
const CYCLE_SIZE: u64 = 4 * size_of::<Value>() as u64;

/// One stop of a gradient: a place on its line, and the colour and alpha
/// there. Colour and alpha interpolate apart, so a colour is not premultiplied.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Stop {
    /// Where the stop is, from 0 at the line's start to 1 at its end.
    pub offset: f64,
    /// The `DeviceRGB` components, from 0 to 1.
    pub color: [f64; 3],
    /// The alpha, from 0 to 1.
    pub alpha: f64,
}

/// A gradient's line: an axis, or a cone of circles between two circles.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Geometry {
    /// A shading of type 2.
    Axial {
        /// Where the line starts.
        start: [f64; 2],
        /// Where the line ends.
        end: [f64; 2],
    },
    /// A shading of type 3.
    Radial {
        /// The centre of the circle where the line starts.
        start: [f64; 2],
        /// The radius of the circle where the line starts.
        start_radius: f64,
        /// The centre of the circle where the line ends.
        end: [f64; 2],
        /// The radius of the circle where the line ends.
        end_radius: f64,
    },
}

/// What a gradient paints beyond the two ends of its line.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Extend {
    /// Nothing.
    None,
    /// The colour at each end continues.
    Pad,
    /// Only the colour at the start continues.
    PadStart,
    /// Only the colour at the end continues.
    PadEnd,
    /// The line starts again.
    Repeat,
    /// The line runs back and forth.
    Reflect,
}

/// A gradient to paint.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Gradient<'a> {
    /// Its line.
    pub geometry: Geometry,
    /// Its stops, with offsets that do not decrease; equal offsets make a
    /// sharp change of colour.
    pub stops: &'a [Stop],
    /// What it paints beyond its line.
    pub extend: Extend,
}

/// Optional bounds and background retained when repainting an existing shading.
#[derive(Clone, Copy, Debug, Default)]
pub struct ShadingMetadata {
    /// The shading-space clip box.
    pub bbox: Option<[f64; 4]>,
    /// Background RGBA, already converted to sRGB by the reader.
    pub background: Option<[f64; 4]>,
    /// Preserve the reader's bounded approximation with a sampled function.
    pub sampled: bool,
}

/// What a caller names to paint a gradient.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Painted {
    /// The `DeviceRGB` shading.
    pub shading: Reference,
    /// The `ExtGState` whose soft mask holds the alpha of stops that differ
    /// in alpha, to set with `gs` around the painting.
    pub mask: Option<Reference>,
    /// The alpha that stops of one alpha share, to set as `ca` or `CA`; 1
    /// when there is a mask.
    pub alpha: f64,
}

/// Why a gradient was not written.
#[derive(Clone, Debug, PartialEq)]
#[non_exhaustive]
pub enum ShadingError {
    /// The line has no extent, so that the gradient paints nothing: its ends
    /// are one point, or its circles are one circle or both have no radius.
    Degenerate,
    /// A number is out of its range, with what is wrong.
    Invalid(&'static str),
    /// The store or the budget refused.
    Store(Error),
}

impl fmt::Display for ShadingError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Degenerate => formatter.write_str("the gradient's line has no extent"),
            Self::Invalid(reason) => write!(formatter, "the gradient is invalid: {reason}"),
            Self::Store(error) => write!(formatter, "{error}"),
        }
    }
}

impl std::error::Error for ShadingError {}

impl From<Error> for ShadingError {
    fn from(error: Error) -> Self {
        Self::Store(error)
    }
}

impl From<LimitExceeded> for ShadingError {
    fn from(refusal: LimitExceeded) -> Self {
        Self::Store(refusal.into())
    }
}

/// Memory charged while a value is built, returned when it has been written.
struct Scratch<'b> {
    budget: &'b Budget,
    amount: u64,
}

impl<'b> Scratch<'b> {
    fn new(budget: &'b Budget, amount: u64) -> Result<Self, LimitExceeded> {
        budget.charge(LimitCode::MemorySize, amount)?;
        Ok(Self { budget, amount })
    }
}

impl Drop for Scratch<'_> {
    fn drop(&mut self) {
        self.budget.release(LimitCode::MemorySize, self.amount);
    }
}

/// The objects that one owner has written, by what they hold, so that equal
/// ones are written once.
///
/// An object that a rollback or an undo removed is written again under a new
/// number, so a table can outlive a transaction of its owner.
#[derive(Debug, Default)]
pub struct Shadings {
    objects: HashMap<Box<[u8]>, Reference>,
}

impl Shadings {
    /// Returns an empty table.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Returns the objects of a table's gradient, written or shared.
    ///
    /// `area` is the box `[x0, y0, x1, y1]` that the painting covers, in the
    /// space of the gradient's coordinates: a soft mask hides what is outside
    /// it, and a repeating gradient draws the cycles that it needs. A gradient
    /// that needs neither does not depend on it.
    ///
    /// Refuses with [`ShadingError::Degenerate`] a line with no extent, with
    /// [`ShadingError::Invalid`] numbers that are not finite and within PDF
    /// real range, with offsets that are not from 0 to 1 in order, or with
    /// other components that are not from 0 to 1, and no stops at all. A
    /// repeating gradient that needs more than [`MAX_CYCLES`] over `area`
    /// refuses with `content_element_count`. What the budget refuses is
    /// counted before it is built or written.
    pub fn paint(
        &mut self,
        transaction: &mut Transaction,
        gradient: &Gradient<'_>,
        area: [f64; 4],
        budget: &Budget,
    ) -> Result<Painted, ShadingError> {
        self.paint_with_metadata(
            transaction,
            gradient,
            area,
            ShadingMetadata::default(),
            budget,
        )
    }

    /// Repaints a gradient with the optional clip box and background of a
    /// read shading. Metadata participates in sharing, just like its colours.
    pub fn paint_with_metadata(
        &mut self,
        transaction: &mut Transaction,
        gradient: &Gradient<'_>,
        area: [f64; 4],
        metadata: ShadingMetadata,
        budget: &Budget,
    ) -> Result<Painted, ShadingError> {
        check_gradient(gradient, area)?;
        if metadata
            .bbox
            .is_some_and(|bbox| bbox.iter().any(|x| !x.is_finite() || x.abs() > REAL_MAX))
            || metadata
                .background
                .is_some_and(|rgba| rgba.iter().any(|x| !(0.0..=1.0).contains(x)))
        {
            return Err(ShadingError::Invalid(
                "invalid shading bounds or background",
            ));
        }
        let span = Span::of(gradient, area)?;
        let geometry = span.map_or(gradient.geometry, |span| span.geometry(gradient.geometry));
        check_geometry(&geometry)?;
        let extended = match gradient.extend {
            Extend::None => [false, false],
            Extend::PadStart => [true, false],
            Extend::PadEnd => [false, true],
            _ => [true, true],
        };
        let color = self.line(
            transaction,
            gradient.stops,
            |stop| &stop.color,
            span.as_ref(),
            metadata.sampled,
            budget,
        )?;
        let shading = self.shading(
            transaction,
            &geometry,
            "DeviceRGB",
            color,
            extended,
            metadata,
            budget,
        )?;
        let alpha = gradient.stops[0].alpha;
        if gradient.stops.iter().all(|stop| stop.alpha == alpha)
            && metadata.background.is_none_or(|rgba| rgba[3] == alpha)
        {
            return Ok(Painted {
                shading,
                mask: None,
                alpha,
            });
        }
        let line = self.line(
            transaction,
            gradient.stops,
            |stop| slice::from_ref(&stop.alpha),
            span.as_ref(),
            metadata.sampled,
            budget,
        )?;
        let gray = self.shading(
            transaction,
            &geometry,
            "DeviceGray",
            line,
            extended,
            metadata,
            budget,
        )?;
        let shadings = dict([("S0", Value::Reference(gray))]);
        let resources = dict([("Shading", Value::Dict(shadings))]);
        let group = self.transparency_group(
            transaction,
            b"/S0 sh",
            area,
            resources,
            "DeviceGray",
            budget,
        )?;
        let mask = self.luminosity_mask(transaction, group, budget)?;
        Ok(Painted {
            shading,
            mask: Some(mask),
            alpha: 1.0,
        })
    }

    /// Returns the shading pattern that paints `shading` through `matrix`,
    /// from the gradient's space to the default space of the content stream
    /// that names the pattern.
    ///
    /// Refuses with [`ShadingError::Invalid`] a matrix that is not finite and
    /// within PDF real range.
    pub fn pattern(
        &mut self,
        transaction: &mut Transaction,
        shading: Reference,
        matrix: [f64; 6],
        budget: &Budget,
    ) -> Result<Reference, ShadingError> {
        if !matrix.iter().copied().all(in_real_range) {
            return Err(ShadingError::Invalid(
                "a pattern's matrix is not finite and within PDF real range",
            ));
        }
        let pattern = dict([
            ("Type", name("Pattern")),
            ("PatternType", Value::Integer(2)),
            ("Shading", Value::Reference(shading)),
            ("Matrix", reals(&matrix)),
        ]);
        Ok(self.shared(transaction, &Value::Dict(pattern), budget)?)
    }

    /// Returns the Form XObject of `content`, a transparency group in
    /// `color_space` over `bbox` with its own `resources`.
    pub fn transparency_group(
        &mut self,
        transaction: &mut Transaction,
        content: &[u8],
        bbox: [f64; 4],
        resources: Dict,
        color_space: &str,
        budget: &Budget,
    ) -> Result<Reference, ShadingError> {
        let group = dict([
            ("Type", name("Group")),
            ("S", name("Transparency")),
            ("CS", name(color_space)),
        ]);
        let form = dict([
            ("Type", name("XObject")),
            ("Subtype", name("Form")),
            ("BBox", reals(&bbox)),
            ("Group", Value::Dict(group)),
            ("Resources", Value::Dict(resources)),
        ]);
        let mut data = BudgetVec::new(budget);
        data.try_extend_from_slice(content)?;
        let data = Bytes::new(data.into_vec());
        let stream = Value::from(Stream::new(form, data));
        Ok(self.shared(transaction, &stream, budget)?)
    }

    /// Returns the `ExtGState` whose soft mask reads the luminosity of
    /// `group`, a transparency group with a colour space.
    pub fn luminosity_mask(
        &mut self,
        transaction: &mut Transaction,
        group: Reference,
        budget: &Budget,
    ) -> Result<Reference, ShadingError> {
        let mask = dict([
            ("Type", name("Mask")),
            ("S", name("Luminosity")),
            ("G", Value::Reference(group)),
        ]);
        let state = dict([("Type", name("ExtGState")), ("SMask", Value::Dict(mask))]);
        Ok(self.shared(transaction, &Value::Dict(state), budget)?)
    }

    /// Returns the function of `values` of the stops along the line, cycled
    /// over `span` if it has one.
    fn line<'s>(
        &mut self,
        transaction: &mut Transaction,
        stops: &'s [Stop],
        values: impl Fn(&'s Stop) -> &'s [f64],
        span: Option<&Span>,
        sampled: bool,
        budget: &Budget,
    ) -> Result<Reference, Error> {
        let function = if sampled {
            self.sampled_function(transaction, stops, values, budget)?
        } else {
            self.function(transaction, stops, values, budget)?
        };
        match span {
            Some(span) => self.cycles(transaction, function, span, budget),
            None => Ok(function),
        }
    }

    /// Keep read shadings on the reader's dyadic grid (maximum depth 10).
    /// Stitching every sampled interval duplicates its boundary stops when
    /// hayro renders it, which changes vello's LUT resolution at sharp edges.
    /// A Type 0 function avoids introducing those new stitching boundaries.
    fn sampled_function<'s>(
        &mut self,
        transaction: &mut Transaction,
        stops: &'s [Stop],
        values: impl Fn(&'s Stop) -> &'s [f64],
        budget: &Budget,
    ) -> Result<Reference, Error> {
        const INTERVALS: u32 = 1 << 10;
        let channels = values(&stops[0]).len();
        let mut samples = BudgetVec::new(budget);
        let mut index = 0;
        for sample in 0..=INTERVALS {
            let offset = f64::from(sample) / f64::from(INTERVALS);
            while index + 2 < stops.len() && stops[index + 1].offset < offset {
                index += 1;
            }
            let left = &stops[index];
            let right = &stops[index + 1];
            let width = right.offset - left.offset;
            let mix = if width > 0.0 {
                ((offset - left.offset) / width).clamp(0.0, 1.0)
            } else {
                1.0
            };
            for (&a, &b) in values(left).iter().zip(values(right)) {
                let value = ((a + (b - a) * mix) * f64::from(u16::MAX)).round() as u16;
                samples.try_extend_from_slice(&value.to_be_bytes())?;
            }
        }
        let ranges = [0.0, 1.0, 0.0, 1.0, 0.0, 1.0];
        let size = Value::Array(vec![Value::Integer(i64::from(INTERVALS + 1))].into());
        let dictionary = dict([
            ("FunctionType", Value::Integer(0)),
            ("Domain", reals(&[0.0, 1.0])),
            ("Range", reals(&ranges[..channels * 2])),
            ("Size", size),
            ("BitsPerSample", Value::Integer(16)),
            ("Encode", reals(&[0.0, f64::from(INTERVALS)])),
            ("Decode", reals(&ranges[..channels * 2])),
        ]);
        let function = Value::from(Stream::new(dictionary, Bytes::new(samples.into_vec())));
        self.shared(transaction, &function, budget)
    }

    /// Returns the function from 0 to 1 that interpolates `values` of the
    /// stops: exponential between two of them, and otherwise stitched.
    #[expect(
        clippy::disallowed_methods,
        reason = "one function and one bound per stop, and one more, which the scratch charges"
    )]
    fn function<'s>(
        &mut self,
        transaction: &mut Transaction,
        stops: &'s [Stop],
        values: impl Fn(&'s Stop) -> &'s [f64],
        budget: &Budget,
    ) -> Result<Reference, Error> {
        let size = (stops.len() as u64 + 1).saturating_mul(SEGMENT_SIZE);
        let _scratch = Scratch::new(budget, size)?;
        let mut functions = Vec::new();
        let mut bounds = Vec::new();
        // Readers hold reals as `f32`, and two bounds that they read as one
        // would leave a segment of no width, so offsets compare as they do.
        let mut reached = 0.0_f64;
        let mut from = values(&stops[0]);
        for stop in stops {
            let to = values(stop);
            if stop.offset as f32 > reached as f32 {
                if !functions.is_empty() {
                    bounds.push(reached);
                }
                functions.push(Value::Dict(exponential(from, to)));
                reached = stop.offset;
            }
            from = to;
        }
        if (reached as f32) < 1.0 {
            if !functions.is_empty() {
                bounds.push(reached);
            }
            functions.push(Value::Dict(exponential(from, from)));
        }
        let function = if functions.len() == 1 {
            functions.remove(0)
        } else {
            let encode: Vec<f64> = functions.iter().flat_map(|_| [0.0, 1.0]).collect();
            Value::Dict(stitching(functions, &bounds, &encode))
        };
        self.shared(transaction, &function, budget)
    }

    /// Returns the function that runs `one` over the cycles of `span`.
    fn cycles(
        &mut self,
        transaction: &mut Transaction,
        one: Reference,
        span: &Span,
        budget: &Budget,
    ) -> Result<Reference, Error> {
        let size = span.count().saturating_mul(CYCLE_SIZE);
        let _scratch = Scratch::new(budget, size)?;
        let (bounds, encode) = span.stitches();
        if bounds.is_empty() && encode == [0.0, 1.0] {
            return Ok(one);
        }
        #[expect(
            clippy::disallowed_methods,
            reason = "one function per cycle, which the scratch charges, and MAX_CYCLES at most"
        )]
        let functions = (0..span.count()).map(|_| Value::Reference(one)).collect();
        let function = Value::Dict(stitching(functions, &bounds, &encode));
        self.shared(transaction, &function, budget)
    }

    /// Returns the shading of `function` along `geometry`.
    #[expect(
        clippy::too_many_arguments,
        reason = "the fields of the emitted shading dictionary"
    )]
    fn shading(
        &mut self,
        transaction: &mut Transaction,
        geometry: &Geometry,
        color_space: &str,
        function: Reference,
        extended: [bool; 2],
        metadata: ShadingMetadata,
        budget: &Budget,
    ) -> Result<Reference, Error> {
        let (kind, coords) = match *geometry {
            Geometry::Axial { start, end } => (2, reals(&[start[0], start[1], end[0], end[1]])),
            Geometry::Radial {
                start,
                start_radius,
                end,
                end_radius,
            } => (
                3,
                reals(&[start[0], start[1], start_radius, end[0], end[1], end_radius]),
            ),
        };
        let extend =
            Value::Array(vec![Value::Boolean(extended[0]), Value::Boolean(extended[1])].into());
        let mut shading = dict([
            ("ShadingType", Value::Integer(kind)),
            ("ColorSpace", name(color_space)),
            ("Coords", coords),
            ("Function", Value::Reference(function)),
            ("Extend", extend),
        ]);
        #[expect(
            clippy::disallowed_methods,
            reason = "at most two optional entries in this fixed shading dictionary"
        )]
        {
            if let Some(bbox) = metadata.bbox {
                shading.insert(Name::from("BBox"), reals(&bbox));
            }
            if let Some(background) = metadata.background {
                let values = if color_space == "DeviceGray" {
                    &background[3..]
                } else {
                    &background[..3]
                };
                shading.insert(Name::from("Background"), reals(values));
            }
        }
        self.shared(transaction, &Value::Dict(shading), budget)
    }

    /// Returns the reference of an object that holds `value`, writing it
    /// unless the table has one that the transaction still holds.
    fn shared(
        &mut self,
        transaction: &mut Transaction,
        value: &Value,
        budget: &Budget,
    ) -> Result<Reference, Error> {
        budget.poll()?;
        let mut key = BudgetVec::new(budget);
        write::canonical(&mut key, value)?;
        if let Some(&known) = self.objects.get(key.as_slice())
            && transaction.version().generation(known.number()).is_some()
        {
            return Ok(known);
        }
        let reference = transaction.allocate()?;
        transaction.write(reference.number(), value, budget)?;
        budget.charge(
            LimitCode::MemorySize,
            (64 + 2 * size_of::<(Box<[u8]>, Reference)>()) as u64,
        )?;
        #[expect(
            clippy::disallowed_methods,
            reason = "the entry is charged to the budget above"
        )]
        self.objects
            .try_reserve(1)
            .map_err(|_| LimitExceeded::new(LimitCode::MemorySize))?;
        // The key's own charge stays with the operation, as the table keeps it.
        #[expect(
            clippy::disallowed_methods,
            reason = "the entry is charged to the budget above, and room is made for it"
        )]
        self.objects
            .insert(key.into_vec().into_boxed_slice(), reference);
        Ok(reference)
    }
}

/// The cycles of a repeating gradient that a painted box needs.
#[derive(Clone, Copy, Debug, PartialEq)]
struct Span {
    /// The place on the line, in cycles, where the drawn line starts.
    low: f64,
    /// The place on the line, in cycles, where the drawn line ends.
    high: f64,
    /// The first cycle that it runs through.
    first: f64,
    /// The cycle after the last one that it runs through.
    last: f64,
    /// Whether odd cycles run backward.
    reflect: bool,
}

impl Span {
    /// Returns the span of a repeating gradient over `area`, or `None` for a
    /// gradient that does not repeat.
    fn of(gradient: &Gradient<'_>, area: [f64; 4]) -> Result<Option<Self>, ShadingError> {
        let reflect = match gradient.extend {
            Extend::None | Extend::Pad | Extend::PadStart | Extend::PadEnd => return Ok(None),
            Extend::Repeat => false,
            Extend::Reflect => true,
        };
        let [x0, y0, x1, y1] = area;
        let corners = [[x0, y0], [x1, y0], [x0, y1], [x1, y1]];
        // Past the place where a radius reaches zero, there are no circles.
        let (needed, floor, ceiling) = match gradient.geometry {
            Geometry::Axial { start, end } => (
                axial_range(start, end, &corners),
                f64::NEG_INFINITY,
                f64::INFINITY,
            ),
            Geometry::Radial {
                start,
                start_radius,
                end,
                end_radius,
            } => {
                let step = end_radius - start_radius;
                let needed = radial_range(start, start_radius, end, step, &corners);
                let zero = -start_radius / step;
                if step > 0.0 {
                    (needed, zero, f64::INFINITY)
                } else if step < 0.0 {
                    (needed, f64::NEG_INFINITY, zero)
                } else {
                    (needed, f64::NEG_INFINITY, f64::INFINITY)
                }
            }
        };
        let first = (needed.0.max(floor) + CYCLE_TOLERANCE).floor();
        let mut last = (needed.1.min(ceiling) - CYCLE_TOLERANCE).ceil();
        if last <= first {
            last = first + 1.0;
        }
        // Past 2^53, adding 1 to a float no longer moves it.
        let count = last - first;
        if !(count <= MAX_CYCLES as f64 && first.abs() < 1e15 && last.abs() < 1e15) {
            let amount = if count.is_finite() {
                count as u64
            } else {
                u64::MAX
            };
            return Err(
                LimitExceeded::over(LimitCode::ContentElementCount, MAX_CYCLES, amount).into(),
            );
        }
        Ok(Some(Self {
            low: first.max(floor),
            high: last.min(ceiling),
            first,
            last,
            reflect,
        }))
    }

    /// Returns how many cycles it runs through.
    fn count(&self) -> u64 {
        (self.last - self.first) as u64
    }

    /// Returns the bounds and the encoding of a stitching function from 0 to
    /// 1 over the span, of one function from 0 to 1.
    #[expect(
        clippy::disallowed_methods,
        reason = "three numbers per cycle, and `Span::of` keeps MAX_CYCLES cycles at most"
    )]
    fn stitches(&self) -> (Vec<f64>, Vec<f64>) {
        let count = self.count();
        let length = self.high - self.low;
        let mut bounds = Vec::new();
        let mut encode = Vec::new();
        for index in 0..count {
            let cycle = self.first + index as f64;
            if index > 0 {
                bounds.push((cycle - self.low) / length);
            }
            let (from, to) = (
                self.low.max(cycle) - cycle,
                self.high.min(cycle + 1.0) - cycle,
            );
            if self.reflect && (cycle as i64).rem_euclid(2) == 1 {
                encode.extend([1.0 - from, 1.0 - to]);
            } else {
                encode.extend([from, to]);
            }
        }
        (bounds, encode)
    }

    /// Returns `geometry` drawn from the span's start to its end.
    fn geometry(&self, geometry: Geometry) -> Geometry {
        let along = |from: [f64; 2], to: [f64; 2], at: f64| {
            [
                from[0] + at * (to[0] - from[0]),
                from[1] + at * (to[1] - from[1]),
            ]
        };
        match geometry {
            Geometry::Axial { start, end } => Geometry::Axial {
                start: along(start, end, self.low),
                end: along(start, end, self.high),
            },
            Geometry::Radial {
                start,
                start_radius,
                end,
                end_radius,
            } => {
                // The zero that a rounding leaves below a radius is no radius.
                let radius =
                    |at: f64| (start_radius + at * (end_radius - start_radius)).max(0.0) + 0.0;
                Geometry::Radial {
                    start: along(start, end, self.low),
                    start_radius: radius(self.low),
                    end: along(start, end, self.high),
                    end_radius: radius(self.high),
                }
            }
        }
    }
}

/// Returns the range of places on an axis of `corners`.
fn axial_range(start: [f64; 2], end: [f64; 2], corners: &[[f64; 2]; 4]) -> (f64, f64) {
    let (dx, dy) = (end[0] - start[0], end[1] - start[1]);
    let length = dx * dx + dy * dy;
    let mut range = (f64::INFINITY, f64::NEG_INFINITY);
    for corner in corners {
        let place = ((corner[0] - start[0]) * dx + (corner[1] - start[1]) * dy) / length;
        range = (range.0.min(place), range.1.max(place));
    }
    range
}

/// Returns a range of the places on a line of circles that holds every circle
/// through `corners`, and so through the box of the corners.
///
/// A circle at place `t` has its centre at `start + t * (end - start)` and its
/// radius at `start_radius + t * radius_step`. It passes through a point `q`
/// from `start` when
/// `(d·d - radius_step²) t² - 2 (q·d + start_radius radius_step) t + (q·q - start_radius²) = 0`,
/// whose roots have a bound that holds over the box because its middle term
/// is linear in `q` and its last term convex, so that the corners bound both.
fn radial_range(
    start: [f64; 2],
    start_radius: f64,
    end: [f64; 2],
    radius_step: f64,
    corners: &[[f64; 2]; 4],
) -> (f64, f64) {
    let (dx, dy) = (end[0] - start[0], end[1] - start[1]);
    let squared = dx * dx + dy * dy;
    let square = radius_step * radius_step;
    let lead = squared - square;
    let mut middle = 0.0_f64;
    let mut last = start_radius * start_radius;
    let mut tangent = 0.0_f64;
    for corner in corners {
        let (qx, qy) = (corner[0] - start[0], corner[1] - start[1]);
        let linear = qx * dx + qy * dy + start_radius * radius_step;
        let constant = qx * qx + qy * qy - start_radius * start_radius;
        middle = middle.max(linear.abs());
        last = last.max(constant.abs());
        tangent = tangent.max((constant / (2.0 * linear)).abs());
    }
    // One circle inside the other and touching it has one root at each point.
    let bound = if lead.abs() <= 1e-9 * (squared + square) {
        tangent
    } else {
        (middle + (middle * middle + lead.abs() * last).sqrt()) / lead.abs()
    };
    (-bound, bound)
}

/// Refuses what a gradient cannot draw: see [`Shadings::paint`].
fn check_gradient(gradient: &Gradient<'_>, area: [f64; 4]) -> Result<(), ShadingError> {
    if gradient.stops.is_empty() {
        return Err(ShadingError::Invalid("a gradient needs a stop"));
    }
    let mut reached = 0.0;
    for stop in gradient.stops {
        if !(reached..=1.0).contains(&stop.offset) {
            return Err(ShadingError::Invalid(
                "stop offsets are numbers from 0 to 1 that do not decrease",
            ));
        }
        reached = stop.offset;
        let unit = |value: &f64| (0.0..=1.0).contains(value);
        if !stop.color.iter().all(unit) || !unit(&stop.alpha) {
            return Err(ShadingError::Invalid(
                "a stop's colour and alpha are numbers from 0 to 1",
            ));
        }
    }
    if !area.iter().copied().all(in_real_range) || !(area[0] <= area[2] && area[1] <= area[3]) {
        return Err(ShadingError::Invalid(
            "the painted box is not finite and ordered, within PDF real range",
        ));
    }
    check_geometry(&gradient.geometry)
}

/// Refuses a line that readers cannot draw, comparing numbers as they do.
fn check_geometry(geometry: &Geometry) -> Result<(), ShadingError> {
    let numbers: &[f64] = match geometry {
        Geometry::Axial { start, end } => &[start[0], start[1], end[0], end[1]],
        Geometry::Radial {
            start,
            start_radius,
            end,
            end_radius,
        } => &[
            start[0],
            start[1],
            *start_radius,
            end[0],
            end[1],
            *end_radius,
        ],
    };
    if !numbers.iter().copied().all(in_real_range) {
        return Err(ShadingError::Invalid(
            "the gradient's coordinates are not finite and within PDF real range",
        ));
    }
    let same = |from: [f64; 2], to: [f64; 2]| {
        from[0] as f32 == to[0] as f32 && from[1] as f32 == to[1] as f32
    };
    match *geometry {
        Geometry::Axial { start, end } if same(start, end) => Err(ShadingError::Degenerate),
        Geometry::Radial {
            start_radius,
            end_radius,
            ..
        } if start_radius < 0.0 || end_radius < 0.0 => {
            Err(ShadingError::Invalid("a radius is not negative"))
        }
        Geometry::Radial {
            start,
            start_radius,
            end,
            end_radius,
        } if (start_radius == 0.0 && end_radius == 0.0)
            || (same(start, end) && start_radius as f32 == end_radius as f32) =>
        {
            Err(ShadingError::Degenerate)
        }
        _ => Ok(()),
    }
}

/// Returns whether `value` is a number that readers hold as a real.
fn in_real_range(value: f64) -> bool {
    value.abs() <= REAL_MAX
}

/// Returns the dictionary of `entries`, in their order.
fn dict<const N: usize>(entries: [(&str, Value); N]) -> Dict {
    #[expect(
        clippy::disallowed_methods,
        reason = "the N entries of a fixed-size array"
    )]
    let dict = entries
        .into_iter()
        .map(|(key, value)| (Name::from(key), value))
        .collect();
    dict
}

/// Returns a name.
fn name(text: &str) -> Value {
    Value::Name(Name::from(text))
}

/// Returns an array of reals, in which negative zero is zero.
fn reals(values: &[f64]) -> Value {
    #[expect(
        clippy::disallowed_methods,
        reason = "a fixed array, a stop's colour, or a stitching's bounds and encoding, which a scratch charges"
    )]
    let array = values
        .iter()
        .map(|&value| Value::from(value + 0.0))
        .collect();
    Value::Array(array)
}

/// Returns the function that runs from `from` to `to` over 0 to 1.
fn exponential(from: &[f64], to: &[f64]) -> Dict {
    dict([
        ("FunctionType", Value::Integer(2)),
        ("Domain", reals(&[0.0, 1.0])),
        ("C0", reals(from)),
        ("C1", reals(to)),
        ("N", Value::Integer(1)),
    ])
}

/// Returns the function over 0 to 1 that joins `functions` at `bounds`.
fn stitching(functions: Vec<Value>, bounds: &[f64], encode: &[f64]) -> Dict {
    dict([
        ("FunctionType", Value::Integer(3)),
        ("Domain", reals(&[0.0, 1.0])),
        ("Functions", Value::Array(functions.into())),
        ("Bounds", reals(bounds)),
        ("Encode", reals(encode)),
    ])
}

#[cfg(test)]
mod tests;
