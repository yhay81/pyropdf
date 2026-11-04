//! The COLR glyphs of an embedded face (DESIGN.md section 7): a glyph's paints
//! written as a transparency group, which a Type 3 glyph procedure
//! ([`type3`](super::type3)) shows.
//!
//! skrifa walks the paints: it resolves shared layers and `PaintColrGlyph`,
//! and bounds cycles, depth, and the number of paints. [`Colr::check`] walks
//! them first without writing, so a glyph paints whole or not at all.
//!
//! Everything is written in glyph space, in font units. A transform only
//! changes the points of what follows, and the matrix of a shading, so every
//! group of a glyph has one space and a soft mask needs nothing to follow.
//!
//! A glyph is one isolated group because a drawing's opacity, blend mode, and
//! soft mask then apply to it as a whole, and because its `gs` operators do
//! not replace them (ISO 32000-2 11.6.6). Its group also holds what it names,
//! so a Type 3 font's own resources hold one entry for each glyph.
//!
//! PDF's colours interpolate apart from alpha, and a font's in premultiplied
//! alpha, so [`premultiplied`] divides a segment of a gradient whose colour
//! and alpha both change.

use std::fmt;
use std::io::Write as _;

use pyropdf_store::Error;
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec};
use pyropdf_store::value::{Dict, Name, Reference, Value};
use pyropdf_store::version::Transaction;
use skrifa::color::{
    Brush, Color, ColorGlyphCollection, ColorPainter, ColorPalettes, ColorStop, CompositeMode,
    Extend as FontExtend, PaintError, Transform,
};
use skrifa::instance::{LocationRef, Size};
use skrifa::metrics::BoundingBox;
use skrifa::outline::{DrawSettings, OutlineGlyphCollection, OutlinePen};
use skrifa::raw::TableProvider;
use skrifa::{FontRef, GlyphId, MetadataProvider};

use super::type3::{Glyph, stream_with};
use crate::content::shading::{Extend, Geometry, Gradient, ShadingError, Shadings, Stop};

#[cfg(test)]
mod tests;

/// The palette index that stands for the text's colour.
const TEXT_COLOUR: u16 = 0xFFFF;

/// How far a colour between two stops may be from a font's, in premultiplied
/// components: one step of an 8-bit colour.
const COLOUR_TOLERANCE: f64 = 1.0 / 255.0;

/// The most pieces that a segment of a gradient is divided into.
const MAX_PIECES: u32 = 16;

/// The largest magnitude of a real that readers hold as a 32-bit float.
const REAL_MAX: f64 = f32::MAX as f64;

/// A matrix in PDF's order: `x' = a x + c y + e`, `y' = b x + d y + f`.
type Affine = [f64; 6];

const IDENTITY: Affine = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0];

/// A box: `[x0, y0, x1, y1]`.
type Bounds = [f64; 4];

/// What a face's colour table says of a glyph.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Verdict {
    /// The canvas paints it.
    Paints,
    /// It needs this paint, which PDF or the writer does not express; a
    /// fallback may draw instead.
    Cannot(&'static str),
    /// skrifa cannot walk its paints, for this reason. A bound refuses, so
    /// nothing draws instead.
    Damaged(&'static str),
}

/// Why a glyph was not painted.
#[derive(Clone, Debug, PartialEq)]
#[non_exhaustive]
pub enum ColrError {
    /// The glyph is not one that [`Colr::check`] passes, or its numbers are
    /// out of PDF's range, with what stands in the way.
    Refused(&'static str),
    /// The store or the budget refused.
    Store(Error),
}

impl fmt::Display for ColrError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Refused(reason) => formatter.write_str(reason),
            Self::Store(error) => write!(formatter, "{error}"),
        }
    }
}

impl std::error::Error for ColrError {}

impl From<Error> for ColrError {
    fn from(error: Error) -> Self {
        Self::Store(error)
    }
}

impl From<LimitExceeded> for ColrError {
    fn from(refusal: LimitExceeded) -> Self {
        Self::Store(refusal.into())
    }
}

impl From<ShadingError> for ColrError {
    fn from(error: ShadingError) -> Self {
        match error {
            ShadingError::Store(error) => Self::Store(error),
            ShadingError::Invalid(reason) => Self::Refused(reason),
            ShadingError::Degenerate => Self::Refused("the gradient's line has no extent"),
        }
    }
}

/// What a repeating gradient cannot cut where a radius reaches zero: its cycles
/// would no longer have one length.
const REPEATS_PAST_ZERO: &str = "PaintRadialGradient that repeats past a radius of zero";

/// Returns whether `brush` repeats a line that holds a radius of zero.
fn crosses_zero(brush: &Brush<'_>) -> bool {
    matches!(
        brush,
        Brush::RadialGradient { r0, r1, extend, .. }
            if (*r0 < 0.0) != (*r1 < 0.0) && *extend != FontExtend::Pad
    )
}

/// Says what skrifa's error means for the reader of a refusal.
fn damage(error: &PaintError) -> &'static str {
    match error {
        PaintError::PaintCycleDetected => "its paints form a cycle",
        PaintError::DepthLimitExceeded => "its paints nest too deep or are too many",
        PaintError::GlyphNotFound(_) => "it paints a glyph that the table does not hold",
        _ => "its paints do not read",
    }
}

/// The colour glyphs of a face that has a COLR table and outlines.
pub struct Colr<'a> {
    glyphs: ColorGlyphCollection<'a>,
    palettes: ColorPalettes<'a>,
    outlines: OutlineGlyphCollection<'a>,
    /// What a paint that no clip bounds covers: the face's box.
    face_box: Bounds,
}

impl<'a> Colr<'a> {
    /// Reads the table of `font`: `None` when it has none, or no outlines for
    /// its layers to take their shapes from.
    #[must_use]
    pub fn read(font: &FontRef<'a>) -> Option<Self> {
        font.colr().ok()?;
        let outlines = font.outline_glyphs();
        outlines.format()?;
        let face_box = font.head().map_or([0.0; 4], |head| {
            [
                f64::from(head.x_min()),
                f64::from(head.y_min()),
                f64::from(head.x_max()),
                f64::from(head.y_max()),
            ]
        });
        Some(Self {
            glyphs: font.color_glyphs(),
            palettes: font.color_palettes(),
            outlines,
            face_box,
        })
    }

    /// Returns what the table says of `glyph`, `None` for one that it does
    /// not hold. Writes nothing. Refuses with what the budget refuses.
    pub fn check(&self, glyph: GlyphId, budget: &Budget) -> Result<Option<Verdict>, LimitExceeded> {
        let Some(colour) = self.glyphs.get(glyph) else {
            return Ok(None);
        };
        let mut checker = Checker {
            colr: self,
            budget,
            cannot: None,
            limit: None,
        };
        let walked = colour.paint(LocationRef::default(), &mut checker);
        if let Some(refusal) = checker.limit {
            return Err(refusal);
        }
        Ok(Some(match walked {
            Err(error) => Verdict::Damaged(damage(&error)),
            Ok(()) => checker.cannot.map_or(Verdict::Paints, Verdict::Cannot),
        }))
    }

    /// Returns the Type 3 glyph `name` that paints `glyph` and advances by
    /// `width`, having written its groups and shadings into `transaction`.
    /// `shadings` shares the objects that are equal, among glyphs too.
    ///
    /// A failure may leave objects in `transaction`, so the caller's refusal
    /// must drop what it wrote. What the budget refuses is counted before it
    /// is built or written.
    pub fn paint(
        &self,
        transaction: &mut Transaction,
        shadings: &mut Shadings,
        glyph: GlyphId,
        name: Name,
        width: f64,
        budget: &Budget,
    ) -> Result<Glyph, ColrError> {
        let Some(colour) = self.glyphs.get(glyph) else {
            return Err(ColrError::Refused("the table does not hold the glyph"));
        };
        let mut frames = BudgetVec::new(budget);
        frames.try_push(Frame::new(budget))?;
        let mut transforms = BudgetVec::new(budget);
        transforms.try_push(IDENTITY)?;
        let mut clips = BudgetVec::new(budget);
        clips.try_push(Clip {
            bounds: None,
            nothing: false,
            written: false,
        })?;
        let mut painter = Painter {
            colr: self,
            transaction,
            shadings,
            budget,
            frames,
            transforms,
            clips,
            failed: None,
        };
        let walked = colour.paint(LocationRef::default(), &mut painter);
        if let Some(failure) = painter.failed {
            return Err(failure);
        }
        walked.map_err(|error| ColrError::Refused(damage(&error)))?;
        let root = painter
            .frames
            .pop()
            .ok_or(ColrError::Refused("its layers do not balance"))?;
        let Some(ink) = root.ink else {
            return Ok(Glyph {
                name,
                width,
                ink: None,
                procedure: super::bitmap::empty_procedure(width),
                resources: Vec::new(),
            });
        };
        let form = write_group(painter.transaction, root, outset(ink), budget)?;
        let procedure = procedure(width, &name);
        Ok(Glyph {
            resources: vec![(
                Name::from("XObject"),
                name.clone(),
                Value::Reference(form),
            )],
            name,
            width,
            ink: Some(ink),
            procedure,
        })
    }

    /// Returns the colour of a palette index, `None` for one that the default
    /// palette does not hold.
    fn colour(&self, index: u16) -> Option<Color> {
        self.palettes.get(0)?.colors().get(usize::from(index)).copied()
    }
}

/// Returns the procedure of a Type 3 glyph that shows the group `name`, and
/// advances by `width`. `d0` leaves the colour to the procedure (ISO 32000-2
/// 9.6.5).
fn procedure(width: f64, name: &Name) -> Vec<u8> {
    let mut out = format!("{width} 0 d0\n/").into_bytes();
    out.extend_from_slice(name.as_bytes());
    out.extend_from_slice(b" Do");
    out
}

/// Returns what a composite mode writes, `None` for one that blending and
/// soft masks do not express: `Plus` and `Xor` add, and `SrcAtop` and
/// `DestAtop` would keep a backdrop's alpha only where it is opaque.
fn composite_of(mode: CompositeMode) -> Option<Composite> {
    Some(match mode {
        CompositeMode::Clear => Composite::Clear,
        CompositeMode::Src => Composite::Source,
        CompositeMode::Dest => Composite::Backdrop,
        CompositeMode::SrcOver => Composite::Blend("Normal"),
        CompositeMode::DestOver => Composite::Under,
        CompositeMode::SrcIn => Composite::Masked { source: true, out: false },
        CompositeMode::DestIn => Composite::Masked { source: false, out: false },
        CompositeMode::SrcOut => Composite::Masked { source: true, out: true },
        CompositeMode::DestOut => Composite::Masked { source: false, out: true },
        CompositeMode::Screen => Composite::Blend("Screen"),
        CompositeMode::Overlay => Composite::Blend("Overlay"),
        CompositeMode::Darken => Composite::Blend("Darken"),
        CompositeMode::Lighten => Composite::Blend("Lighten"),
        CompositeMode::ColorDodge => Composite::Blend("ColorDodge"),
        CompositeMode::ColorBurn => Composite::Blend("ColorBurn"),
        CompositeMode::HardLight => Composite::Blend("HardLight"),
        CompositeMode::SoftLight => Composite::Blend("SoftLight"),
        CompositeMode::Difference => Composite::Blend("Difference"),
        CompositeMode::Exclusion => Composite::Blend("Exclusion"),
        CompositeMode::Multiply => Composite::Blend("Multiply"),
        CompositeMode::HslHue => Composite::Blend("Hue"),
        CompositeMode::HslSaturation => Composite::Blend("Saturation"),
        CompositeMode::HslColor => Composite::Blend("Color"),
        CompositeMode::HslLuminosity => Composite::Blend("Luminosity"),
        _ => return None,
    })
}

/// Returns the name that a refusal gives to a mode that it cannot write.
const fn mode_name(mode: CompositeMode) -> &'static str {
    match mode {
        CompositeMode::SrcAtop => "PaintComposite in the mode SrcAtop",
        CompositeMode::DestAtop => "PaintComposite in the mode DestAtop",
        CompositeMode::Xor => "PaintComposite in the mode Xor",
        CompositeMode::Plus => "PaintComposite in the mode Plus",
        _ => "PaintComposite in an unknown mode",
    }
}

/// How a layer joins what lies under it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Composite {
    /// Neither remains.
    Clear,
    /// Only the layer remains.
    Source,
    /// Only what lies under it remains.
    Backdrop,
    /// The layer goes over what lies under it, in this blend mode.
    Blend(&'static str),
    /// What lies under it goes over the layer.
    Under,
    /// One shows through the alpha of the other, or where the other has none:
    /// the layer when `source`, and otherwise what lies under it.
    Masked { source: bool, out: bool },
}

/// Walks a glyph's paints without writing, for what it cannot paint.
struct Checker<'c, 'a, 'b> {
    colr: &'c Colr<'a>,
    budget: &'b Budget,
    cannot: Option<&'static str>,
    limit: Option<LimitExceeded>,
}

impl Checker<'_, '_, '_> {
    fn poll(&mut self) {
        if self.limit.is_none()
            && let Err(refusal) = self.budget.poll()
        {
            self.limit = Some(refusal);
        }
    }

    fn cannot(&mut self, paint: &'static str) {
        self.cannot.get_or_insert(paint);
    }

    fn stops(&mut self, stops: &[ColorStop]) {
        for stop in stops {
            if stop.palette_index == TEXT_COLOUR {
                self.cannot("a gradient stop in the text's colour");
            } else if self.colr.colour(stop.palette_index).is_none() {
                self.cannot("a palette entry that the default palette does not hold");
            }
        }
    }
}

impl ColorPainter for Checker<'_, '_, '_> {
    fn push_transform(&mut self, _transform: Transform) {
        self.poll();
    }
    fn pop_transform(&mut self) {}
    fn push_clip_glyph(&mut self, _glyph: GlyphId) {
        self.poll();
    }
    fn push_clip_box(&mut self, _clip_box: BoundingBox) {
        self.poll();
    }
    fn pop_clip(&mut self) {}
    fn fill(&mut self, brush: Brush<'_>) {
        self.poll();
        match brush {
            Brush::Solid { palette_index, .. } => {
                if palette_index != TEXT_COLOUR && self.colr.colour(palette_index).is_none() {
                    self.cannot("a palette entry that the default palette does not hold");
                }
            }
            Brush::LinearGradient { color_stops, .. }
            | Brush::RadialGradient { color_stops, .. } => {
                self.stops(color_stops);
                if crosses_zero(&brush) {
                    self.cannot(REPEATS_PAST_ZERO);
                }
            }
            Brush::SweepGradient { .. } => self.cannot("PaintSweepGradient"),
        }
    }
    fn push_layer(&mut self, mode: CompositeMode) {
        self.poll();
        if composite_of(mode).is_none() {
            self.cannot(mode_name(mode));
        }
    }
}

/// One content stream of a glyph, and what it names.
struct Frame<'b> {
    content: BudgetVec<'b, u8>,
    resources: BudgetVec<'b, (Category, Name, Value)>,
    /// What it paints, in glyph space.
    ink: Option<Bounds>,
}

/// A category of a content stream's resources.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Category {
    XObject,
    Shading,
    ExtGState,
}

impl<'b> Frame<'b> {
    fn new(budget: &'b Budget) -> Self {
        Self {
            content: BudgetVec::new(budget),
            resources: BudgetVec::new(budget),
            ink: None,
        }
    }

    /// Names `value` in `category`, and returns the name, unique in the frame.
    fn name(&mut self, category: Category, value: Value) -> Result<Name, LimitExceeded> {
        let prefix = match category {
            Category::XObject => 'X',
            Category::Shading => 'S',
            Category::ExtGState => 'G',
        };
        let name = Name::from(format!("{prefix}{}", self.resources.len()).as_str());
        self.resources.try_push((category, name.clone(), value))?;
        Ok(name)
    }

    /// Appends `/name operator` to the content.
    fn show(&mut self, name: &Name, operator: &[u8]) -> Result<(), LimitExceeded> {
        self.content.try_push(b'/')?;
        self.content.try_extend_from_slice(name.as_bytes())?;
        self.content.try_push(b' ')?;
        self.content.try_extend_from_slice(operator)?;
        self.content.try_push(b'\n')
    }

    fn inked(&mut self, bounds: Bounds) {
        self.ink = union(self.ink, Some(bounds));
    }
}

/// The clip in force in one scope.
#[derive(Clone, Copy, Debug)]
struct Clip {
    /// What it lets through, in glyph space; `None` for no clip yet.
    bounds: Option<Bounds>,
    /// Whether it lets nothing through, so that nothing is written.
    nothing: bool,
    /// Whether its scope wrote `q`.
    written: bool,
}

/// Writes a glyph's paints.
struct Painter<'c, 'a, 'b> {
    colr: &'c Colr<'a>,
    transaction: &'c mut Transaction,
    shadings: &'c mut Shadings,
    budget: &'b Budget,
    frames: BudgetVec<'b, Frame<'b>>,
    transforms: BudgetVec<'b, Affine>,
    clips: BudgetVec<'b, Clip>,
    failed: Option<ColrError>,
}

impl<'b> Painter<'_, '_, 'b> {
    /// Runs `step` unless an earlier one failed, and keeps its failure, since
    /// skrifa's callbacks return nothing.
    fn attempt(&mut self, step: impl FnOnce(&mut Self) -> Result<(), ColrError>) {
        if self.failed.is_none() {
            let result = self
                .budget
                .poll()
                .map_err(ColrError::from)
                .and_then(|()| step(self));
            if let Err(failure) = result {
                self.failed = Some(failure);
            }
        }
    }

    fn transform(&self) -> Affine {
        self.transforms.last().copied().unwrap_or(IDENTITY)
    }

    fn clip(&self) -> Clip {
        self.clips.last().copied().unwrap_or(Clip {
            bounds: None,
            nothing: false,
            written: false,
        })
    }

    fn frame(&mut self) -> Result<&mut Frame<'b>, ColrError> {
        self.frames
            .last_mut()
            .ok_or(ColrError::Refused("its layers do not balance"))
    }

    /// Returns what a paint covers: the clip's bounds, else the face's box.
    fn covered(&self) -> Bounds {
        self.clip().bounds.unwrap_or(self.colr.face_box)
    }

    /// Appends the path of `glyph` through `matrix` to the current frame, and
    /// returns its bounds, `None` when it has no points.
    fn outline(&mut self, glyph: GlyphId, matrix: Affine) -> Result<Option<Bounds>, ColrError> {
        let Some(outline) = self.colr.outlines.get(glyph) else {
            return Ok(None);
        };
        let frame = self.frame()?;
        let start = frame.content.len();
        let mut pen = PathPen {
            out: &mut frame.content,
            matrix,
            current: (0.0, 0.0),
            bounds: None,
            failed: None,
        };
        let settings = DrawSettings::unhinted(Size::unscaled(), LocationRef::default());
        let drawn = outline.draw(settings, &mut pen);
        let (bounds, failed) = (pen.bounds, pen.failed);
        if let Some(failure) = failed {
            return Err(failure);
        }
        if drawn.is_err() {
            return Err(ColrError::Refused("a layer's outline does not read"));
        }
        if bounds.is_none() {
            frame.content.truncate(start);
        }
        Ok(bounds)
    }

    /// Paints `brush` over what the clip lets through, its shading through
    /// `brush_transform`. `path` holds the path to fill with a solid colour,
    /// which then needs no clip; `None` paints the clip's bounds.
    fn paint(
        &mut self,
        path: Option<GlyphId>,
        brush_transform: Option<Transform>,
        brush: &Brush<'_>,
    ) -> Result<(), ColrError> {
        if self.clip().nothing {
            return Ok(());
        }
        let matrix = self.transform();
        if let Brush::Solid { palette_index, alpha } = brush {
            let Some((colour, alpha)) = self.solid(*palette_index, *alpha)? else {
                return Ok(());
            };
            return self.fill_solid(path, matrix, colour, alpha);
        }
        // A clip to the glyph's outline, then the shading under it.
        let mut bounds = self.covered();
        let mark = self.frame()?.content.len();
        if let Some(glyph) = path {
            self.frame()?.content.try_extend_from_slice(b"q\n")?;
            let Some(found) = self.outline(glyph, matrix)? else {
                self.frame()?.content.truncate(mark);
                return Ok(());
            };
            self.frame()?.content.try_extend_from_slice(b"W n\n")?;
            bounds = match self.clip().bounds {
                Some(clip) => match intersection(clip, found) {
                    Some(both) => both,
                    None => {
                        self.frame()?.content.truncate(mark);
                        return Ok(());
                    }
                },
                None => found,
            };
        }
        let matrix = brush_transform.map_or(matrix, |transform| concat(matrix, of(transform)));
        let painted = self.gradient(matrix, bounds, brush)?;
        let frame = self.frame()?;
        if painted {
            frame.inked(bounds);
        } else if path.is_some() {
            frame.content.truncate(mark);
            return Ok(());
        }
        if path.is_some() {
            frame.content.try_extend_from_slice(b"Q\n")?;
        }
        Ok(())
    }

    /// Returns the components and alpha of a solid colour, `None` for the
    /// components of the text's colour, and no answer when it shows nothing.
    #[expect(
        clippy::type_complexity,
        reason = "a colour, of a palette or the text's, and an alpha, or nothing"
    )]
    fn solid(
        &self,
        palette_index: u16,
        alpha: f32,
    ) -> Result<Option<(Option<[f64; 3]>, f64)>, ColrError> {
        let scale = f64::from(alpha).clamp(0.0, 1.0);
        if palette_index == TEXT_COLOUR {
            return Ok((scale > 0.0).then_some((None, scale)));
        }
        let Some(colour) = self.colr.colour(palette_index) else {
            return Err(ColrError::Refused(
                "a palette entry that the default palette does not hold",
            ));
        };
        let (components, own) = components(colour);
        let alpha = own * scale;
        Ok((alpha > 0.0).then_some((Some(components), alpha)))
    }

    /// Fills `path`, or the clip's bounds, in one colour.
    fn fill_solid(
        &mut self,
        path: Option<GlyphId>,
        matrix: Affine,
        colour: Option<[f64; 3]>,
        alpha: f64,
    ) -> Result<(), ColrError> {
        let covered = self.covered();
        let clip = self.clip().bounds;
        let frame = self.frame()?;
        let mark = frame.content.len();
        frame.content.try_extend_from_slice(b"q\n")?;
        if let Some([red, green, blue]) = colour {
            number(&mut frame.content, red, 4)?;
            number(&mut frame.content, green, 4)?;
            number(&mut frame.content, blue, 4)?;
            frame.content.try_extend_from_slice(b"rg\n")?;
        }
        if alpha < 1.0 {
            state(frame, "ca", Value::from(alpha))?;
        }
        let bounds = match path {
            Some(glyph) => {
                let Some(found) = self.outline(glyph, matrix)? else {
                    self.frame()?.content.truncate(mark);
                    return Ok(());
                };
                match clip {
                    Some(clip) => intersection(clip, found),
                    None => Some(found),
                }
            }
            None => {
                let [x0, y0, x1, y1] = covered;
                let content = &mut self.frame()?.content;
                number(content, x0, 3)?;
                number(content, y0, 3)?;
                number(content, x1 - x0, 3)?;
                number(content, y1 - y0, 3)?;
                content.try_extend_from_slice(b"re\n")?;
                Some(covered)
            }
        };
        let Some(bounds) = bounds else {
            self.frame()?.content.truncate(mark);
            return Ok(());
        };
        let frame = self.frame()?;
        frame.content.try_extend_from_slice(b"f\nQ\n")?;
        frame.inked(bounds);
        Ok(())
    }

    /// Paints a gradient through `matrix` over `bounds`, in glyph space, and
    /// returns whether it paints anything: a line with no extent paints
    /// nothing, as a matrix that flattens the gradient does.
    fn gradient(
        &mut self,
        matrix: Affine,
        bounds: Bounds,
        brush: &Brush<'_>,
    ) -> Result<bool, ColrError> {
        let Some(inverse) = invert(matrix) else {
            return Ok(false);
        };
        let (geometry, font_stops, extend) = match *brush {
            Brush::LinearGradient { p0, p1, color_stops, extend } => (
                Geometry::Axial {
                    start: [f64::from(p0.x), f64::from(p0.y)],
                    end: [f64::from(p1.x), f64::from(p1.y)],
                },
                color_stops,
                extend,
            ),
            Brush::RadialGradient { c0, r0, c1, r1, color_stops, extend } => (
                Geometry::Radial {
                    start: [f64::from(c0.x), f64::from(c0.y)],
                    start_radius: f64::from(r0),
                    end: [f64::from(c1.x), f64::from(c1.y)],
                    end_radius: f64::from(r1),
                },
                color_stops,
                extend,
            ),
            _ => return Err(ColrError::Refused("PaintSweepGradient")),
        };
        let mut stops = BudgetVec::new(self.budget);
        self.stops(font_stops, &mut stops)?;
        let Some(first) = stops.first().copied() else {
            return Ok(false);
        };
        let extend = match extend {
            FontExtend::Repeat => Extend::Repeat,
            FontExtend::Reflect => Extend::Reflect,
            _ => Extend::Pad,
        };
        if crosses_zero(brush) {
            return Err(ColrError::Refused(REPEATS_PAST_ZERO));
        }
        let Some(geometry) = positive_radii(geometry, &mut stops)? else {
            return Ok(false);
        };
        if stops.iter().all(|stop| stop.alpha == 0.0) {
            return Ok(false);
        }
        // One stop, or one colour, is a solid fill of what the clip holds.
        if stops.len() == 1 {
            self.flood(first.color, first.alpha, bounds)?;
            return Ok(true);
        }
        let gradient = Gradient {
            geometry,
            stops: &stops,
            extend,
        };
        let area = mapped(inverse, bounds);
        let painted = match self.shadings.paint(self.transaction, &gradient, area, self.budget) {
            Ok(painted) => painted,
            Err(ShadingError::Degenerate) => return Ok(false),
            Err(error) => return Err(error.into()),
        };
        let frame = self.frame()?;
        frame.content.try_extend_from_slice(b"q\n")?;
        if matrix != IDENTITY {
            write_matrix(&mut frame.content, matrix)?;
        }
        if painted.alpha < 1.0 {
            state(frame, "ca", Value::from(painted.alpha))?;
        }
        if let Some(mask) = painted.mask {
            let name = frame.name(Category::ExtGState, Value::Reference(mask))?;
            frame.show(&name, b"gs")?;
        }
        let name = frame.name(Category::Shading, Value::Reference(painted.shading))?;
        frame.show(&name, b"sh")?;
        frame.content.try_extend_from_slice(b"Q\n")?;
        Ok(true)
    }

    /// Fills `bounds` in one colour.
    fn flood(&mut self, colour: [f64; 3], alpha: f64, bounds: Bounds) -> Result<(), ColrError> {
        let frame = self.frame()?;
        frame.content.try_extend_from_slice(b"q\n")?;
        let [red, green, blue] = colour;
        number(&mut frame.content, red, 4)?;
        number(&mut frame.content, green, 4)?;
        number(&mut frame.content, blue, 4)?;
        frame.content.try_extend_from_slice(b"rg\n")?;
        if alpha < 1.0 {
            state(frame, "ca", Value::from(alpha))?;
        }
        let [x0, y0, x1, y1] = bounds;
        number(&mut frame.content, x0, 3)?;
        number(&mut frame.content, y0, 3)?;
        number(&mut frame.content, x1 - x0, 3)?;
        number(&mut frame.content, y1 - y0, 3)?;
        frame.content.try_extend_from_slice(b"re\nf\nQ\n")?;
        Ok(())
    }

    /// Appends the writer's stops of a font's, in straight alpha.
    fn stops(
        &self,
        font_stops: &[ColorStop],
        out: &mut BudgetVec<'_, Stop>,
    ) -> Result<(), ColrError> {
        let mut last: Option<Stop> = None;
        for font_stop in font_stops {
            if font_stop.palette_index == TEXT_COLOUR {
                return Err(ColrError::Refused("a gradient stop in the text's colour"));
            }
            let Some(colour) = self.colr.colour(font_stop.palette_index) else {
                return Err(ColrError::Refused(
                    "a palette entry that the default palette does not hold",
                ));
            };
            let (color, own) = components(colour);
            let offset = f64::from(font_stop.offset).clamp(0.0, 1.0);
            let offset = last.map_or(offset, |last| offset.max(last.offset));
            let alpha = own * f64::from(font_stop.alpha).clamp(0.0, 1.0);
            let stop = Stop { offset, color, alpha };
            match last {
                Some(last) => premultiplied(last, stop, out)?,
                None => out.try_push(stop)?,
            }
            last = Some(stop);
        }
        Ok(())
    }

    /// Joins the layer `source` to the frame under it.
    fn join(&mut self, source: Frame<'b>, composite: Composite) -> Result<(), ColrError> {
        let budget = self.budget;
        let Some(backdrop) = self.frames.last_mut() else {
            return Err(ColrError::Refused("its layers do not balance"));
        };
        let transaction = &mut *self.transaction;
        match composite {
            Composite::Clear => {
                *backdrop = Frame::new(budget);
            }
            Composite::Source => *backdrop = source,
            Composite::Backdrop => {}
            Composite::Blend(mode) => {
                let Some(ink) = source.ink else {
                    return Ok(());
                };
                let form = write_group(transaction, source, outset(ink), budget)?;
                let name = backdrop.name(Category::XObject, Value::Reference(form))?;
                if mode == "Normal" {
                    backdrop.show(&name, b"Do")?;
                } else {
                    backdrop.content.try_extend_from_slice(b"q\n")?;
                    let blend = Value::Name(Name::from(mode));
                    state(backdrop, "BM", blend)?;
                    backdrop.show(&name, b"Do")?;
                    backdrop.content.try_extend_from_slice(b"Q\n")?;
                }
                backdrop.inked(ink);
            }
            Composite::Under => {
                let Some(ink) = source.ink else {
                    return Ok(());
                };
                let under = std::mem::replace(backdrop, Frame::new(budget));
                let over_ink = under.ink;
                let form = write_group(transaction, source, outset(ink), budget)?;
                let name = backdrop.name(Category::XObject, Value::Reference(form))?;
                backdrop.show(&name, b"Do")?;
                backdrop.inked(ink);
                if let Some(over_ink) = over_ink {
                    let form = write_group(transaction, under, outset(over_ink), budget)?;
                    let name = backdrop.name(Category::XObject, Value::Reference(form))?;
                    backdrop.show(&name, b"Do")?;
                    backdrop.inked(over_ink);
                }
            }
            Composite::Masked { source: shows_source, out } => {
                let under = std::mem::replace(backdrop, Frame::new(budget));
                let (shown, mask) = if shows_source {
                    (source, under)
                } else {
                    (under, source)
                };
                let Some(shown_ink) = shown.ink else {
                    return Ok(());
                };
                let Some(mask_ink) = mask.ink else {
                    // No alpha to read: everything shows through its absence.
                    if out {
                        *backdrop = shown;
                    }
                    return Ok(());
                };
                // An inverted mask must reach what shows, as its outside is
                // read before it is inverted.
                let reach = if out {
                    union(Some(mask_ink), Some(shown_ink)).unwrap_or(mask_ink)
                } else {
                    mask_ink
                };
                let ink = if out {
                    Some(shown_ink)
                } else {
                    intersection(shown_ink, mask_ink)
                };
                let Some(ink) = ink else {
                    return Ok(());
                };
                let mask = write_group(transaction, mask, outset(reach), budget)?;
                let shown = write_group(transaction, shown, outset(shown_ink), budget)?;
                let state = Value::Dict(mask_state(mask, out));
                let state = backdrop.name(Category::ExtGState, state)?;
                let name = backdrop.name(Category::XObject, Value::Reference(shown))?;
                backdrop.content.try_extend_from_slice(b"q\n")?;
                backdrop.show(&state, b"gs")?;
                backdrop.show(&name, b"Do")?;
                backdrop.content.try_extend_from_slice(b"Q\n")?;
                backdrop.inked(ink);
            }
        }
        Ok(())
    }
}

impl ColorPainter for Painter<'_, '_, '_> {
    fn push_transform(&mut self, transform: Transform) {
        self.attempt(|painter| {
            let matrix = concat(painter.transform(), of(transform));
            painter.transforms.try_push(matrix)?;
            Ok(())
        });
    }

    fn pop_transform(&mut self) {
        self.transforms.pop();
    }

    fn push_clip_glyph(&mut self, glyph: GlyphId) {
        self.attempt(|painter| {
            let outer = painter.clip();
            if outer.nothing {
                painter.clips.try_push(outer_nothing())?;
                return Ok(());
            }
            let matrix = painter.transform();
            let frame = painter.frame()?;
            let mark = frame.content.len();
            frame.content.try_extend_from_slice(b"q\n")?;
            let inner = painter.outline(glyph, matrix)?;
            let bounds = match (inner, outer.bounds) {
                (Some(inner), Some(outer)) => intersection(inner, outer),
                (inner, None) => inner,
                (None, Some(_)) => None,
            };
            let frame = painter.frame()?;
            let Some(bounds) = bounds else {
                frame.content.truncate(mark);
                painter.clips.try_push(outer_nothing())?;
                return Ok(());
            };
            frame.content.try_extend_from_slice(b"W n\n")?;
            painter.clips.try_push(Clip {
                bounds: Some(bounds),
                nothing: false,
                written: true,
            })?;
            Ok(())
        });
    }

    fn push_clip_box(&mut self, clip_box: BoundingBox) {
        self.attempt(|painter| {
            let outer = painter.clip();
            if outer.nothing {
                painter.clips.try_push(outer_nothing())?;
                return Ok(());
            }
            let matrix = painter.transform();
            let corners = [
                (clip_box.x_min, clip_box.y_min),
                (clip_box.x_max, clip_box.y_min),
                (clip_box.x_max, clip_box.y_max),
                (clip_box.x_min, clip_box.y_max),
            ]
            .map(|(x, y)| apply(matrix, f64::from(x), f64::from(y)));
            let inner = corners_box(&corners);
            let bounds = match outer.bounds {
                Some(outer) => intersection(inner, outer),
                None => Some(inner),
            };
            let Some(bounds) = bounds else {
                painter.clips.try_push(outer_nothing())?;
                return Ok(());
            };
            let content = &mut painter.frame()?.content;
            content.try_extend_from_slice(b"q\n")?;
            for (at, (x, y)) in corners.into_iter().enumerate() {
                number(content, x, 3)?;
                number(content, y, 3)?;
                content.try_extend_from_slice(if at == 0 { b"m\n" } else { b"l\n" })?;
            }
            content.try_extend_from_slice(b"h\nW n\n")?;
            painter.clips.try_push(Clip {
                bounds: Some(bounds),
                nothing: false,
                written: true,
            })?;
            Ok(())
        });
    }

    fn pop_clip(&mut self) {
        self.attempt(|painter| {
            if painter.clips.pop().is_some_and(|clip| clip.written) {
                painter.frame()?.content.try_extend_from_slice(b"Q\n")?;
            }
            Ok(())
        });
    }

    fn fill(&mut self, brush: Brush<'_>) {
        self.attempt(|painter| painter.paint(None, None, &brush));
    }

    fn fill_glyph(&mut self, glyph: GlyphId, brush_transform: Option<Transform>, brush: Brush<'_>) {
        self.attempt(|painter| painter.paint(Some(glyph), brush_transform, &brush));
    }

    fn push_layer(&mut self, _mode: CompositeMode) {
        self.attempt(|painter| {
            painter.frames.try_push(Frame::new(painter.budget))?;
            Ok(())
        });
    }

    fn pop_layer_with_mode(&mut self, mode: CompositeMode) {
        self.attempt(|painter| {
            let Some(composite) = composite_of(mode) else {
                return Err(ColrError::Refused(mode_name(mode)));
            };
            let Some(source) = painter.frames.pop() else {
                return Err(ColrError::Refused("its layers do not balance"));
            };
            painter.join(source, composite)
        });
    }
}

/// Returns a clip that lets nothing through.
const fn outer_nothing() -> Clip {
    Clip {
        bounds: None,
        nothing: true,
        written: false,
    }
}

/// Returns the state that shows through the alpha of `group`, or through its
/// absence when `out`.
fn mask_state(group: Reference, out: bool) -> Dict {
    let mut mask = Dict::new();
    mask.insert(Name::from("Type"), Value::Name(Name::from("Mask")));
    mask.insert(Name::from("S"), Value::Name(Name::from("Alpha")));
    mask.insert(Name::from("G"), Value::Reference(group));
    if out {
        mask.insert(Name::from("TR"), Value::Dict(inverted()));
    }
    let mut state = Dict::new();
    state.insert(Name::from("Type"), Value::Name(Name::from("ExtGState")));
    state.insert(Name::from("SMask"), Value::Dict(mask));
    state
}

/// Returns the function that takes `x` to `1 - x`.
fn inverted() -> Dict {
    let unit = |value: i64| Value::Array(vec![Value::Integer(value)].into());
    let mut function = Dict::new();
    function.insert(Name::from("FunctionType"), Value::Integer(2));
    function.insert(
        Name::from("Domain"),
        Value::Array(vec![Value::Integer(0), Value::Integer(1)].into()),
    );
    function.insert(Name::from("C0"), unit(1));
    function.insert(Name::from("C1"), unit(0));
    function.insert(Name::from("N"), Value::Integer(1));
    function
}

/// Sets one entry of the graphics state, naming it in the frame's resources.
fn state(frame: &mut Frame<'_>, key: &str, value: Value) -> Result<(), LimitExceeded> {
    let mut entry = Dict::new();
    entry.insert(Name::from(key), value);
    let name = frame.name(Category::ExtGState, Value::Dict(entry))?;
    frame.show(&name, b"gs")
}

/// Writes `frame` as an isolated transparency group over `bbox`, in glyph
/// space, and returns its reference.
fn write_group(
    transaction: &mut Transaction,
    frame: Frame<'_>,
    bbox: Bounds,
    budget: &Budget,
) -> Result<Reference, ColrError> {
    let mut resources = Dict::new();
    for category in [Category::XObject, Category::Shading, Category::ExtGState] {
        let mut named = Dict::new();
        for (held, name, value) in &frame.resources {
            if *held == category {
                budget.poll()?;
                named.insert(name.clone(), value.clone());
            }
        }
        if !named.is_empty() {
            let key = match category {
                Category::XObject => "XObject",
                Category::Shading => "Shading",
                Category::ExtGState => "ExtGState",
            };
            resources.insert(Name::from(key), Value::Dict(named));
        }
    }
    let mut group = Dict::new();
    group.insert(Name::from("Type"), Value::Name(Name::from("Group")));
    group.insert(Name::from("S"), Value::Name(Name::from("Transparency")));
    group.insert(Name::from("CS"), Value::Name(Name::from("DeviceRGB")));
    group.insert(Name::from("I"), Value::Boolean(true));
    let mut form = Dict::new();
    form.insert(Name::from("Type"), Value::Name(Name::from("XObject")));
    form.insert(Name::from("Subtype"), Value::Name(Name::from("Form")));
    form.insert(
        Name::from("BBox"),
        Value::Array(bbox.iter().copied().map(whole).collect()),
    );
    form.insert(Name::from("Group"), Value::Dict(group));
    form.insert(Name::from("Resources"), Value::Dict(resources));
    let stream = stream_with(form, &frame.content, budget)?;
    let reference = transaction.allocate()?;
    transaction.write(reference.number(), &Value::from(stream), budget)?;
    Ok(reference)
}

/// Returns `value`, a whole number of [`outset`], as an integer when it fits.
fn whole(value: f64) -> Value {
    if value.abs() < 1e15 {
        Value::Integer(value as i64)
    } else {
        Value::from(value)
    }
}

/// Returns the whole units around `bounds`, and one more, so that a group's
/// box does not cut what a reader smooths along its edge.
fn outset(bounds: Bounds) -> Bounds {
    [
        bounds[0].floor() - 1.0,
        bounds[1].floor() - 1.0,
        bounds[2].ceil() + 1.0,
        bounds[3].ceil() + 1.0,
    ]
}

/// Returns the components of a palette colour, from 0 to 1, and its alpha.
fn components(colour: Color) -> ([f64; 3], f64) {
    let unit = |value: u8| f64::from(value) / 255.0;
    (
        [unit(colour.red), unit(colour.green), unit(colour.blue)],
        unit(colour.alpha),
    )
}

/// Appends the stops after `from` up to `to` that hold a font's interpolation
/// in premultiplied alpha: `to` with what lies before it. `from` is the last
/// stop that `out` holds.
///
/// Where one end has no alpha, its colour never shows, so the other's holds
/// across the segment. Otherwise alpha stays linear, and the colour goes
/// along `w(t) = a1 t / (a0 (1 - t) + a1 t)`: the pieces are as many as that
/// needs to stay within [`COLOUR_TOLERANCE`], each one equal step of `w`.
fn premultiplied(from: Stop, to: Stop, out: &mut BudgetVec<'_, Stop>) -> Result<(), LimitExceeded> {
    let (first, second) = (from.alpha, to.alpha);
    if first == second || from.color == to.color || to.offset == from.offset {
        return out.try_push(to);
    }
    if first == 0.0 {
        out.try_push(Stop { color: to.color, ..from })?;
        return out.try_push(to);
    }
    if second == 0.0 {
        return out.try_push(Stop { color: from.color, ..to });
    }
    let change = from
        .color
        .iter()
        .zip(&to.color)
        .fold(0.0_f64, |most, (a, b)| most.max((b - a).abs()));
    let place = |w: f64| first * w / (second * (1.0 - w) + first * w);
    let colour_at = |t: f64| second * t / (first * (1.0 - t) + second * t);
    let pieces = (1..MAX_PIECES)
        .find(|&pieces| {
            (0..pieces).all(|piece| {
                let (low, high) = (
                    f64::from(piece) / f64::from(pieces),
                    f64::from(piece + 1) / f64::from(pieces),
                );
                let (start, end) = (place(low), place(high));
                let middle = f64::midpoint(start, end);
                let alpha = first + (second - first) * middle;
                let wrong = (colour_at(middle) - f64::midpoint(low, high)).abs();
                alpha * change * wrong <= COLOUR_TOLERANCE
            })
        })
        .unwrap_or(MAX_PIECES);
    for piece in 1..pieces {
        let w = f64::from(piece) / f64::from(pieces);
        let t = place(w);
        out.try_push(Stop {
            offset: from.offset + (to.offset - from.offset) * t,
            color: [
                from.color[0] + (to.color[0] - from.color[0]) * w,
                from.color[1] + (to.color[1] - from.color[1]) * w,
                from.color[2] + (to.color[2] - from.color[2]) * w,
            ],
            alpha: first + (second - first) * t,
        })?;
    }
    out.try_push(to)
}

/// Returns `geometry` with no negative radius: skrifa's normalized stops can
/// leave one, and there are no circles past the place where a radius reaches
/// zero, so the line stops there and `stops` start or end there too. `None`
/// when no circles remain.
fn positive_radii(
    geometry: Geometry,
    stops: &mut BudgetVec<'_, Stop>,
) -> Result<Option<Geometry>, LimitExceeded> {
    let Geometry::Radial { start, start_radius, end, end_radius } = geometry else {
        return Ok(Some(geometry));
    };
    if start_radius >= 0.0 && end_radius >= 0.0 {
        return Ok(Some(geometry));
    }
    if start_radius <= 0.0 && end_radius <= 0.0 {
        return Ok(None);
    }
    let zero = start_radius / (start_radius - end_radius);
    let along = |t: f64| {
        [
            start[0] + t * (end[0] - start[0]),
            start[1] + t * (end[1] - start[1]),
        ]
    };
    let at = |stops: &[Stop], t: f64| -> Stop {
        let after = stops.iter().position(|stop| stop.offset >= t);
        match after {
            None => Stop { offset: t, ..stops[stops.len() - 1] },
            Some(0) => Stop { offset: t, ..stops[0] },
            Some(after) => {
                let (low, high) = (stops[after - 1], stops[after]);
                let span = high.offset - low.offset;
                let part = if span > 0.0 { (t - low.offset) / span } else { 0.0 };
                let mix = |a: f64, b: f64| a + (b - a) * part;
                Stop {
                    offset: t,
                    color: [
                        mix(low.color[0], high.color[0]),
                        mix(low.color[1], high.color[1]),
                        mix(low.color[2], high.color[2]),
                    ],
                    alpha: mix(low.alpha, high.alpha),
                }
            }
        }
    };
    let cut = at(stops, zero);
    let keeps_end = end_radius > 0.0;
    let mut kept = BudgetVec::new(stops.budget());
    kept.try_reserve_exact(stops.len().saturating_add(1))?;
    if keeps_end {
        kept.try_push(Stop { offset: 0.0, ..cut })?;
        for stop in stops.iter().filter(|stop| stop.offset > zero) {
            kept.try_push(Stop {
                offset: (stop.offset - zero) / (1.0 - zero),
                ..*stop
            })?;
        }
    } else {
        for stop in stops.iter().filter(|stop| stop.offset < zero) {
            kept.try_push(Stop {
                offset: stop.offset / zero,
                ..*stop
            })?;
        }
        kept.try_push(Stop { offset: 1.0, ..cut })?;
    }
    stops.clear();
    stops.try_extend_from_slice(&kept)?;
    Ok(Some(if keeps_end {
        Geometry::Radial {
            start: along(zero),
            start_radius: 0.0,
            end,
            end_radius,
        }
    } else {
        Geometry::Radial {
            start,
            start_radius,
            end: along(zero),
            end_radius: 0.0,
        }
    }))
}

/// Writes outlines as path operators in glyph space, charging the budget.
struct PathPen<'p, 'b> {
    out: &'p mut BudgetVec<'b, u8>,
    matrix: Affine,
    current: (f64, f64),
    bounds: Option<Bounds>,
    failed: Option<ColrError>,
}

impl PathPen<'_, '_> {
    /// Writes the numbers of `points` through the matrix, and an operator.
    fn operator(&mut self, points: &[(f32, f32)], operator: &[u8]) {
        if self.failed.is_some() {
            return;
        }
        if let Err(failure) = self.write(points, operator) {
            self.failed = Some(failure);
        }
    }

    fn write(&mut self, points: &[(f32, f32)], operator: &[u8]) -> Result<(), ColrError> {
        for &(x, y) in points {
            let (x, y) = apply(self.matrix, f64::from(x), f64::from(y));
            number(self.out, x, 3)?;
            number(self.out, y, 3)?;
            self.bounds = union(self.bounds, Some([x, y, x, y]));
        }
        if let Some(&(x, y)) = points.last() {
            self.current = (f64::from(x), f64::from(y));
        }
        self.out.try_extend_from_slice(operator)?;
        Ok(())
    }
}

impl OutlinePen for PathPen<'_, '_> {
    fn move_to(&mut self, x: f32, y: f32) {
        self.operator(&[(x, y)], b"m\n");
    }
    fn line_to(&mut self, x: f32, y: f32) {
        self.operator(&[(x, y)], b"l\n");
    }
    fn quad_to(&mut self, cx0: f32, cy0: f32, x: f32, y: f32) {
        // A cubic of the same curve lies two thirds of the way to the control.
        let (x0, y0) = self.current;
        let (cx, cy) = (f64::from(cx0), f64::from(cy0));
        let (ex, ey) = (f64::from(x), f64::from(y));
        let first = ((x0 + 2.0 * cx) / 3.0, (y0 + 2.0 * cy) / 3.0);
        let second = ((ex + 2.0 * cx) / 3.0, (ey + 2.0 * cy) / 3.0);
        self.operator(
            &[
                (first.0 as f32, first.1 as f32),
                (second.0 as f32, second.1 as f32),
                (x, y),
            ],
            b"c\n",
        );
    }
    fn curve_to(&mut self, cx0: f32, cy0: f32, cx1: f32, cy1: f32, x: f32, y: f32) {
        self.operator(&[(cx0, cy0), (cx1, cy1), (x, y)], b"c\n");
    }
    fn close(&mut self) {
        self.operator(&[], b"h\n");
    }
}

/// Appends `value` and a space, with at most `decimals` digits after the
/// point, and refuses a number that readers do not hold.
fn number(out: &mut BudgetVec<'_, u8>, value: f64, decimals: usize) -> Result<(), ColrError> {
    if value.abs() > REAL_MAX || value.is_nan() {
        return Err(ColrError::Refused(
            "a coordinate is outside the range of a PDF real",
        ));
    }
    // The largest real has 39 digits before the point.
    let mut buffer = [0_u8; 64];
    let mut cursor = &mut buffer[..];
    let written = write!(cursor, "{value:.decimals$}");
    let free = cursor.len();
    if written.is_err() {
        return Err(ColrError::Refused(
            "a coordinate is outside the range of a PDF real",
        ));
    }
    let mut text = &buffer[..buffer.len() - free];
    if text.contains(&b'.') {
        while let [rest @ .., b'0'] = text {
            text = rest;
        }
        if let [rest @ .., b'.'] = text {
            text = rest;
        }
    }
    if text == b"-0" || text.is_empty() {
        text = b"0";
    }
    out.try_extend_from_slice(text)?;
    out.try_push(b' ')?;
    Ok(())
}

/// Appends `matrix` and `cm`.
fn write_matrix(out: &mut BudgetVec<'_, u8>, matrix: Affine) -> Result<(), ColrError> {
    for value in matrix {
        number(out, value, 6)?;
    }
    out.try_extend_from_slice(b"cm\n")?;
    Ok(())
}

/// Returns a font's matrix in PDF's order.
fn of(transform: Transform) -> Affine {
    [
        f64::from(transform.xx),
        f64::from(transform.yx),
        f64::from(transform.xy),
        f64::from(transform.yy),
        f64::from(transform.dx),
        f64::from(transform.dy),
    ]
}

/// Returns the matrix that applies `inner`, then `outer`.
fn concat(outer: Affine, inner: Affine) -> Affine {
    let [a, b, c, d, e, f] = outer;
    let [ia, ib, ic, id, ie, if_] = inner;
    [
        a * ia + c * ib,
        b * ia + d * ib,
        a * ic + c * id,
        b * ic + d * id,
        a * ie + c * if_ + e,
        b * ie + d * if_ + f,
    ]
}

/// Returns the point `(x, y)` through `matrix`.
fn apply(matrix: Affine, x: f64, y: f64) -> (f64, f64) {
    let [a, b, c, d, e, f] = matrix;
    (a * x + c * y + e, b * x + d * y + f)
}

/// Returns the inverse of `matrix`, `None` when it flattens the plane.
fn invert(matrix: Affine) -> Option<Affine> {
    let [a, b, c, d, e, f] = matrix;
    let det = a * d - b * c;
    if det == 0.0 || !det.is_finite() {
        return None;
    }
    let inverse = [
        d / det,
        -b / det,
        -c / det,
        a / det,
        (c * f - d * e) / det,
        (b * e - a * f) / det,
    ];
    inverse.iter().all(|value| value.is_finite()).then_some(inverse)
}

/// Returns the box around `bounds` through `matrix`.
fn mapped(matrix: Affine, bounds: Bounds) -> Bounds {
    let [x0, y0, x1, y1] = bounds;
    corners_box(&[
        apply(matrix, x0, y0),
        apply(matrix, x1, y0),
        apply(matrix, x1, y1),
        apply(matrix, x0, y1),
    ])
}

/// Returns the box around four points.
fn corners_box(corners: &[(f64, f64); 4]) -> Bounds {
    corners.iter().fold(
        [
            f64::INFINITY,
            f64::INFINITY,
            f64::NEG_INFINITY,
            f64::NEG_INFINITY,
        ],
        |[x0, y0, x1, y1], &(x, y)| [x0.min(x), y0.min(y), x1.max(x), y1.max(y)],
    )
}

fn union(a: Option<Bounds>, b: Option<Bounds>) -> Option<Bounds> {
    match (a, b) {
        (Some(a), Some(b)) => Some([
            a[0].min(b[0]),
            a[1].min(b[1]),
            a[2].max(b[2]),
            a[3].max(b[3]),
        ]),
        (a, b) => a.or(b),
    }
}

/// Returns what `a` and `b` share, `None` when that has no area to paint.
fn intersection(a: Bounds, b: Bounds) -> Option<Bounds> {
    let both = [
        a[0].max(b[0]),
        a[1].max(b[1]),
        a[2].min(b[2]),
        a[3].min(b[3]),
    ];
    (both[0] <= both[2] && both[1] <= both[3]).then_some(both)
}
