//! Content-operation provenance for painted items on a page, until `hayro`
//! reports where each painted item comes from (`I4`, yhay81/pyropdf#104,
//! `LaurenzV/hayro#1742`).
//!
//! Temporary DESIGN.md §6 workaround tracked in yhay81/pyropdf#7 and #1273.
//! Removal condition: delete once pyropdf adopts a released `hayro-interpret`
//! containing `LaurenzV/hayro#1742` (#104).
//!
//! Two entry points coexist with zero overhead on the default non-provenance
//! extraction and rendering paths:
//!
//! 1. [`run_sources`] (used by text layout when `chars=True`): re-interprets
//!    the page's own joined [`PageContent`] with marked-content wrappers around
//!    text-showing operators to map page-level glyph runs to [`Source`].
//! 2. [`page_provenance`] / [`page_provenance_with_primary`] (generalized
//!    on-demand provenance): builds an ephemeral bridge overlay
//!    ([`crate::bridge::provenance_overlay`]) that wraps every painted operator
//!    (`Tj`, `TJ`, `'`, `"`, `S`, `s`, `f`, `F`, `f*`, `B`, `B*`, `b`, `b*`,
//!    `Do`, `BI..EI`, `sh`) across page `/Contents`, nested Form XObjects
//!    (bounded by `MAX_STREAM_DEPTH`, cycle-safe, deduplicated per object), and
//!    visible annotation `/AP /N` streams in collision-free marked content,
//!    never mutating [`Version`]. A clip-guarded [`ProvenanceDevice`] tracks
//!    nested Form/annotation call stacks immune to malformed `EMC` inside child
//!    streams, and cross-checks every painted item against the un-instrumented
//!    primary pass by count, kind, geometry, and display-space transform.

use hayro::hayro_interpret::font::GlyphRun;
use hayro::hayro_interpret::pattern::Pattern;
use hayro::hayro_interpret::{
    BlendMode, ClipPath, Device, DrawMode, DrawProps, Image, ImageDrawProps, InterpreterCache,
    InterpreterSettings, Paint, RenderContent, SoftMask, interpret, interpret_page,
};
use hayro::hayro_syntax::content::TypedIter;
use hayro::hayro_syntax::object::Dict;
use hayro::hayro_syntax::page::Page;
use hayro::kurbo::{BezPath, PathEl};
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::pages::PageIndex;
use pyropdf_store::value::Value;
use pyropdf_store::version::Version;

use super::{Error, extraction_context};
use crate::bridge::provenance_overlay::{ProvenanceOverlay, build_provenance_overlay};
use crate::bridge::{Bridged, Error as BridgeError, Result as BridgeResult};
use crate::content::PageContent;
use crate::display::Display;
use crate::render::Fallback;

/// The prefix of the marked-content tags that name page-level text operations
/// in [`run_sources`].
const TAG: &[u8] = b"pyropdf.operation.";
/// A wrapper around each page-level `Do`; tags inside its Form cannot
/// identify a page operation, even if they use our prefix.
const FORM_TAG: &[u8] = b"pyropdf.operation.form";

/// The operators that show text.
const SHOWS_TEXT: [&[u8]; 4] = [b"Tj", b"TJ", b"'", b"\""];

/// How many device callbacks occur between deadline/cancellation polls.
const CALLBACK_POLL_INTERVAL: usize = 64;

/// The operation of a page's own content that showed a glyph.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Source {
    /// The object number of the content stream that holds the operator.
    pub stream: u32,
    /// The operation's index in the page's joined content.
    pub operation: u32,
}

/// What the first interpretation collected of one glyph run.
#[derive(Clone, Copy)]
pub(crate) struct RunCheck {
    /// The run's place among the page's glyph runs.
    pub run: u32,
    pub glyphs: u32,
    /// The bits of the transform from the first glyph's space to display space.
    pub first: [u64; 6],
}

/// What the second interpretation saw of one glyph run.
struct Origin {
    operation: Option<u32>,
    glyphs: u32,
    first: [u64; 6],
}

/// One enclosing Form XObject invocation (`Do`) on the call stack from the
/// page or annotation root down to the stream that painted an item.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InvocationFrame {
    /// Object number of the content stream that held the `Do` operator.
    pub stream: u32,
    /// Zero-based operation index of the `Do` operator in that stream (or in
    /// the page's joined `/Contents` for page-level invocations).
    pub operation: u32,
    /// Resource name passed to `Do` (e.g. `"Fm1"`), when present.
    pub xobject_name: Option<String>,
}

/// The annotation whose normal appearance stream (`/AP /N`) painted an item.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AnnotationOrigin {
    /// Object number of the annotation dictionary (`0` when stored directly in
    /// the page's `/Annots` array).
    pub annotation: u32,
    /// Object number of the selected `/AP /N` appearance stream.
    pub appearance_stream: u32,
}

/// Which kind of painted operator produced a [`PaintedProvenance`] record.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ItemKind {
    /// A text glyph run (`Tj`, `TJ`, `'`, `"`).
    GlyphRun,
    /// A vector path (`S`, `s`, `f`, `F`, `f*`, `B`, `B*`, `b`, `b*`).
    Path,
    /// An Image XObject invoked via `Do`.
    Image,
    /// An inline image (`BI ... ID ... EI`).
    InlineImage,
    /// A shading painted via the `sh` operator.
    Shading,
}

/// Which `DrawMode` discriminant `hayro-interpret` passed for a glyph run or path.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DrawKind {
    Fill,
    Stroke,
    FillAndStroke,
    Invisible,
}

impl DrawKind {
    const fn from_mode(mode: &DrawMode) -> Self {
        match mode {
            DrawMode::Fill(_) => Self::Fill,
            DrawMode::Stroke(_) => Self::Stroke,
            DrawMode::FillAndStroke(..) => Self::FillAndStroke,
            DrawMode::Invisible => Self::Invisible,
        }
    }
}

/// Structural fingerprint of a painted item used to cross-check the
/// instrumented provenance pass against the un-instrumented primary pass.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ItemSignature {
    /// Glyph count, draw mode, and display-space transform bits of the first glyph.
    GlyphRun {
        glyphs: u32,
        draw: DrawKind,
        first: [u64; 6],
    },
    /// Element count, draw mode, shading-paint flag, first path point bits, and
    /// display-space transform bits.
    Path {
        segments: u32,
        draw: DrawKind,
        is_shading_paint: bool,
        first_point: [u64; 2],
        transform: [u64; 6],
    },
    /// Pixel dimensions, stored XObject number (`0` for inline images), stencil
    /// flag, and display-space transform bits.
    Image {
        width: u32,
        height: u32,
        xref: u32,
        is_stencil: bool,
        transform: [u64; 6],
    },
}

/// Complete provenance of one painted item on a page.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PaintedProvenance {
    /// Which kind of painted operator produced this item.
    pub kind: ItemKind,
    /// Object number of the innermost content stream holding the operator.
    pub stream: u32,
    /// Zero-based operation index in `stream` (or in the page's joined
    /// `/Contents` when painted directly by page content).
    pub operation: u32,
    /// Enclosing Form XObject `Do` frames, outermost first.
    pub call_stack: Vec<InvocationFrame>,
    /// Enclosing annotation origin when painted inside an annotation `/AP /N`.
    pub annotation: Option<AnnotationOrigin>,
    /// Resource name invoked by the painted operator: the Image XObject name
    /// for `Do` images, the shading name for `sh`, or `None` otherwise.
    pub resource_name: Option<String>,
    /// Cross-check signature observed by the provenance pass.
    pub signature: ItemSignature,
}

/// Paint-order provenance of every painted item on a page, verified against
/// the un-instrumented primary interpretation pass.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct PageProvenance {
    /// One entry per `draw_glyph_run` callback in paint order.
    pub glyph_runs: Vec<Option<PaintedProvenance>>,
    /// One entry per non-shading `draw_path` callback in paint order.
    pub paths: Vec<Option<PaintedProvenance>>,
    /// One entry per `draw_image` callback (both Image XObjects and inline
    /// images) in paint order.
    pub images: Vec<Option<PaintedProvenance>>,
    /// One entry per `sh`-painted shading callback in paint order.
    pub shadings: Vec<Option<PaintedProvenance>>,
    /// All painted items across all categories in exact paint order.
    pub items: Vec<Option<PaintedProvenance>>,
    /// Whether every item signature from the instrumented overlay matched the
    /// un-instrumented primary pass 1-for-1. When `false`, all item vectors are
    /// empty (fail-closed).
    pub verified: bool,
}

/// Primary un-instrumented item signatures for one page, used to cross-check
/// the instrumented provenance pass.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct PrimarySignatures {
    signatures: Vec<ItemSignature>,
}

impl PrimarySignatures {
    /// Returns the number of painted callbacks observed during the primary pass.
    #[must_use]
    pub const fn len(&self) -> usize {
        self.signatures.len()
    }

    /// Returns whether the primary pass observed no painted callbacks.
    #[must_use]
    pub const fn is_empty(&self) -> bool {
        self.signatures.is_empty()
    }

    /// Returns the recorded signatures in paint order.
    #[must_use]
    pub fn as_slice(&self) -> &[ItemSignature] {
        &self.signatures
    }
}

/// Collects un-instrumented primary painted-item signatures for page `index`
/// of `bridged`.
pub fn collect_primary_signatures(
    bridged: &Bridged,
    fallback: &Fallback,
    index: usize,
    annotations: bool,
    budget: &Budget,
) -> BridgeResult<PrimarySignatures> {
    let (page, display) = bridged.page(index).ok_or(BridgeError::NoPage(index))?;
    let settings = interpreter_settings(fallback, annotations);
    let cache = InterpreterCache::new();
    let mut context = extraction_context(page, display, &cache, settings);
    let mut device = SignatureDevice {
        signatures: BudgetVec::new(budget),
        callbacks: 0,
        error: None,
    };
    interpret_page(page, &mut context, &mut device);
    if let Some(refusal) = device.error {
        return Err(refusal.into());
    }
    Ok(PrimarySignatures {
        signatures: device.signatures.into_vec(),
    })
}

/// Computes verified on-demand provenance for every painted item on page
/// `index` of `version`, including nested Form XObjects (up to
/// `MAX_STREAM_DEPTH`), paths, Image XObjects, inline images, `sh` shadings,
/// and (when `annotations` is set) visible annotation `/AP /N` streams.
///
/// Builds a primary un-instrumented [`Bridged`] view for cross-checking and an
/// ephemeral instrumented overlay without mutating `version`. Callers that
/// already hold a cached [`Bridged`] for `version` should prefer
/// [`page_provenance_with_primary`] to avoid rebuilding the primary bridge.
pub fn page_provenance(
    version: &Version,
    pages: &PageIndex,
    index: usize,
    fallback: &Fallback,
    annotations: bool,
    budget: &Budget,
) -> BridgeResult<PageProvenance> {
    let primary = Bridged::new(version, pages, budget)?;
    page_provenance_with_primary(
        &primary,
        version,
        pages,
        index,
        fallback,
        annotations,
        budget,
    )
}

/// Like [`page_provenance`], reusing an existing un-instrumented `primary`
/// bridge of `version` for cross-checking.
pub fn page_provenance_with_primary(
    primary: &Bridged,
    version: &Version,
    pages: &PageIndex,
    index: usize,
    fallback: &Fallback,
    annotations: bool,
    budget: &Budget,
) -> BridgeResult<PageProvenance> {
    let primary_sigs = collect_primary_signatures(primary, fallback, index, annotations, budget)?;
    let overlay = build_provenance_overlay(version, pages, index, annotations, budget)?;
    collect_overlay_provenance(
        &overlay,
        &primary_sigs,
        fallback,
        index,
        annotations,
        budget,
    )
}

/// Runs [`ProvenanceDevice`] over `overlay` and cross-checks every item
/// against `primary_sigs`.
fn collect_overlay_provenance(
    overlay: &ProvenanceOverlay,
    primary_sigs: &PrimarySignatures,
    fallback: &Fallback,
    index: usize,
    annotations: bool,
    budget: &Budget,
) -> BridgeResult<PageProvenance> {
    let (page, display) = overlay
        .bridged
        .page(index)
        .ok_or(BridgeError::NoPage(index))?;
    let settings = interpreter_settings(fallback, annotations);
    let cache = InterpreterCache::new();
    let mut context = extraction_context(page, display, &cache, settings);
    let mut device = ProvenanceDevice {
        prefix: overlay.prefix.as_bytes(),
        budget,
        records: BudgetVec::new(budget),
        frames: BudgetVec::new(budget),
        mc_depth: 0,
        clip_depth: 0,
        callbacks: 0,
        error: None,
    };
    interpret_page(page, &mut context, &mut device);
    if let Some(refusal) = device.error {
        return Err(refusal.into());
    }

    let records = device.records;
    let verified = primary_sigs.signatures.len() == records.len()
        && primary_sigs
            .signatures
            .iter()
            .zip(records.iter())
            .all(|(expected, (observed, _))| expected == observed);
    if !verified {
        return Ok(PageProvenance {
            verified: false,
            ..PageProvenance::default()
        });
    }

    let mut glyph_runs = BudgetVec::new(budget);
    let mut paths = BudgetVec::new(budget);
    let mut images = BudgetVec::new(budget);
    let mut shadings = BudgetVec::new(budget);
    let mut items = BudgetVec::new(budget);
    items.try_reserve_exact(records.len())?;

    for (sig, prov) in records.into_vec() {
        let prov_clone = match &prov {
            Some(p) => Some(clone_provenance(p, budget)?),
            None => None,
        };
        match sig {
            ItemSignature::GlyphRun { .. } => glyph_runs.try_push(prov_clone)?,
            ItemSignature::Image { .. } => images.try_push(prov_clone)?,
            ItemSignature::Path {
                is_shading_paint, ..
            } => {
                let is_shading = match &prov_clone {
                    Some(p) => p.kind == ItemKind::Shading,
                    None => is_shading_paint,
                };
                if is_shading {
                    shadings.try_push(prov_clone)?;
                } else {
                    paths.try_push(prov_clone)?;
                }
            }
        }
        items.try_push(prov)?;
    }

    Ok(PageProvenance {
        glyph_runs: glyph_runs.into_vec(),
        paths: paths.into_vec(),
        images: images.into_vec(),
        shadings: shadings.into_vec(),
        items: items.into_vec(),
        verified: true,
    })
}

fn interpreter_settings(fallback: &Fallback, annotations: bool) -> InterpreterSettings {
    InterpreterSettings {
        render_content: if annotations {
            RenderContent::all()
        } else {
            RenderContent::PAGE
        },
        ..fallback.settings()
    }
}

fn glyph_run_signature(
    run: &GlyphRun<'_, '_>,
    props: &DrawProps<'_>,
    mode: &DrawMode,
) -> ItemSignature {
    let glyphs = run.glyphs();
    let first = glyphs.first().map_or([0; 6], |glyph| {
        (props.transform * glyph.transform())
            .as_coeffs()
            .map(f64::to_bits)
    });
    ItemSignature::GlyphRun {
        glyphs: u32::try_from(glyphs.len()).unwrap_or(u32::MAX),
        draw: DrawKind::from_mode(mode),
        first,
    }
}

fn path_signature(path: &BezPath, props: &DrawProps<'_>, mode: &DrawMode) -> ItemSignature {
    let first_point = match path.elements().first() {
        Some(PathEl::MoveTo(p) | PathEl::LineTo(p)) => [p.x.to_bits(), p.y.to_bits()],
        Some(PathEl::QuadTo(p, _) | PathEl::CurveTo(p, _, _)) => [p.x.to_bits(), p.y.to_bits()],
        Some(PathEl::ClosePath) | None => [0, 0],
    };
    let is_shading_paint = matches!(
        &props.paint,
        Paint::Pattern(pattern) if matches!(pattern.as_ref(), Pattern::Shading(_))
    );
    ItemSignature::Path {
        segments: u32::try_from(path.elements().len()).unwrap_or(u32::MAX),
        draw: DrawKind::from_mode(mode),
        is_shading_paint,
        first_point,
        transform: props.transform.as_coeffs().map(f64::to_bits),
    }
}

fn image_signature(image: &Image<'_, '_>, props: &ImageDrawProps<'_>) -> ItemSignature {
    let (xref, is_stencil) = match image {
        Image::Raster(r) => (r.stream().obj_id().obj_number, false),
        Image::Stencil(s) => (s.stream().obj_id().obj_number, true),
    };
    ItemSignature::Image {
        width: image.width(),
        height: image.height(),
        xref,
        is_stencil,
        transform: props.transform.as_coeffs().map(f64::to_bits),
    }
}

/// Lightweight primary device collecting only [`ItemSignature`]s in paint
/// order for cross-checking against the instrumented overlay pass.
struct SignatureDevice<'b> {
    signatures: BudgetVec<'b, ItemSignature>,
    callbacks: usize,
    error: Option<LimitExceeded>,
}

impl SignatureDevice<'_> {
    fn record(&mut self, sig: ItemSignature) {
        if self.error.is_some() {
            return;
        }
        self.callbacks = self.callbacks.wrapping_add(1);
        if self.callbacks.is_multiple_of(CALLBACK_POLL_INTERVAL)
            && let Err(refusal) = self.signatures.budget().poll()
        {
            self.error = Some(refusal);
            return;
        }
        if let Err(refusal) = self.signatures.try_push(sig) {
            self.error = Some(refusal);
        }
    }
}

impl<'a> Device<'a> for SignatureDevice<'_> {
    fn draw_path(&mut self, path: &BezPath, props: DrawProps<'a>, mode: &DrawMode) {
        self.record(path_signature(path, &props, mode));
    }

    fn push_clip_path(&mut self, _: &ClipPath) {}
    fn push_transparency_group(&mut self, _: f32, _: Option<SoftMask<'a>>, _: BlendMode) {}

    fn draw_glyph_run(&mut self, run: &GlyphRun<'_, 'a>, props: DrawProps<'a>, mode: &DrawMode) {
        self.record(glyph_run_signature(run, &props, mode));
    }

    fn draw_image(&mut self, image: Image<'a, '_>, props: ImageDrawProps<'a>) {
        self.record(image_signature(&image, &props));
    }

    fn pop_clip(&mut self) {}
    fn pop_transparency_group(&mut self) {}
}

enum FrameKind {
    Annotation {
        origin: AnnotationOrigin,
    },
    Do {
        stream: u32,
        operation: u32,
        name: Option<String>,
        entered_form: bool,
    },
    Op {
        stream: u32,
        operation: u32,
    },
    InlineImage {
        stream: u32,
        operation: u32,
    },
    Shading {
        stream: u32,
        operation: u32,
        name: Option<String>,
    },
}

struct OpenFrame {
    kind: FrameKind,
    mc_depth: usize,
    clip_depth: usize,
}

impl OpenFrame {
    const fn is_locked(&self) -> bool {
        matches!(
            self.kind,
            FrameKind::Annotation { .. }
                | FrameKind::Do {
                    entered_form: true,
                    ..
                }
        )
    }
}

/// Generalized clip-guarded provenance `Device` tracking nested Form XObjects,
/// annotation `/AP /N` streams, paths, glyph runs, images, inline images, and
/// `sh` shadings.
struct ProvenanceDevice<'p, 'b> {
    prefix: &'p [u8],
    budget: &'b Budget,
    records: BudgetVec<'b, (ItemSignature, Option<PaintedProvenance>)>,
    frames: BudgetVec<'b, OpenFrame>,
    mc_depth: usize,
    clip_depth: usize,
    callbacks: usize,
    error: Option<LimitExceeded>,
}

impl ProvenanceDevice<'_, '_> {
    fn poll_callback(&mut self) -> bool {
        if self.error.is_some() {
            return false;
        }
        self.callbacks = self.callbacks.wrapping_add(1);
        if self.callbacks.is_multiple_of(CALLBACK_POLL_INTERVAL)
            && let Err(refusal) = self.budget.poll()
        {
            self.error = Some(refusal);
            return false;
        }
        true
    }

    /// Smallest `mc_depth` below which an unmatched `EMC` inside a child Form
    /// XObject or annotation appearance cannot drop.
    fn locked_mc_floor(&self) -> usize {
        for frame in self.frames.iter().rev() {
            if frame.is_locked() {
                return frame.mc_depth;
            }
        }
        0
    }

    /// Collects the enclosing annotation origin and Form `Do` call stack from
    /// frames below the leaf operator frame.
    fn enclosing_context(
        &self,
        leaf_index: usize,
    ) -> Result<(Option<AnnotationOrigin>, Vec<InvocationFrame>), LimitExceeded> {
        let mut annotation = None;
        let mut call_stack = BudgetVec::new(self.budget);
        for frame in &self.frames[..leaf_index] {
            match &frame.kind {
                FrameKind::Annotation { origin } => annotation = Some(*origin),
                FrameKind::Do {
                    stream,
                    operation,
                    name,
                    entered_form: true,
                } => {
                    let cloned_name = match name {
                        Some(s) => Some(clone_budget_string(s, self.budget)?),
                        None => None,
                    };
                    call_stack.try_push(InvocationFrame {
                        stream: *stream,
                        operation: *operation,
                        xobject_name: cloned_name,
                    })?;
                }
                _ => {}
            }
        }
        Ok((annotation, call_stack.into_vec()))
    }

    fn record_item(&mut self, sig: ItemSignature, prov: Option<PaintedProvenance>) {
        if let Err(refusal) = self.records.try_push((sig, prov)) {
            self.error = Some(refusal);
        }
    }
}

impl<'a> Device<'a> for ProvenanceDevice<'_, '_> {
    fn draw_path(&mut self, path: &BezPath, props: DrawProps<'a>, mode: &DrawMode) {
        if !self.poll_callback() {
            return;
        }
        let sig = path_signature(path, &props, mode);
        let prov = match self.frames.last() {
            Some(top) if top.mc_depth == self.mc_depth => match &top.kind {
                FrameKind::Shading {
                    stream,
                    operation,
                    name,
                } => {
                    let leaf_idx = self.frames.len() - 1;
                    let (stream, operation) = (*stream, *operation);
                    let resource_name = match name {
                        Some(s) => match clone_budget_string(s, self.budget) {
                            Ok(s) => Some(s),
                            Err(refusal) => {
                                self.error = Some(refusal);
                                return;
                            }
                        },
                        None => None,
                    };
                    let (annotation, call_stack) = match self.enclosing_context(leaf_idx) {
                        Ok(ctx) => ctx,
                        Err(refusal) => {
                            self.error = Some(refusal);
                            return;
                        }
                    };
                    Some(PaintedProvenance {
                        kind: ItemKind::Shading,
                        stream,
                        operation,
                        call_stack,
                        annotation,
                        resource_name,
                        signature: sig,
                    })
                }
                FrameKind::Op { stream, operation } => {
                    let leaf_idx = self.frames.len() - 1;
                    let (stream, operation) = (*stream, *operation);
                    let (annotation, call_stack) = match self.enclosing_context(leaf_idx) {
                        Ok(ctx) => ctx,
                        Err(refusal) => {
                            self.error = Some(refusal);
                            return;
                        }
                    };
                    Some(PaintedProvenance {
                        kind: ItemKind::Path,
                        stream,
                        operation,
                        call_stack,
                        annotation,
                        resource_name: None,
                        signature: sig,
                    })
                }
                _ => None,
            },
            _ => None,
        };
        self.record_item(sig, prov);
    }

    fn push_clip_path(&mut self, _: &ClipPath) {
        if let Some(top) = self.frames.last_mut()
            && top.clip_depth == self.clip_depth
            && top.mc_depth == self.mc_depth
            && let FrameKind::Do {
                entered_form: entered @ false,
                ..
            } = &mut top.kind
        {
            *entered = true;
        }
        self.clip_depth = self.clip_depth.saturating_add(1);
    }

    fn push_transparency_group(&mut self, _: f32, _: Option<SoftMask<'a>>, _: BlendMode) {}

    fn draw_glyph_run(&mut self, run: &GlyphRun<'_, 'a>, props: DrawProps<'a>, mode: &DrawMode) {
        if !self.poll_callback() {
            return;
        }
        let sig = glyph_run_signature(run, &props, mode);
        let prov = match self.frames.last() {
            Some(top) if top.mc_depth == self.mc_depth => match top.kind {
                FrameKind::Op { stream, operation } => {
                    let leaf_idx = self.frames.len() - 1;
                    let (annotation, call_stack) = match self.enclosing_context(leaf_idx) {
                        Ok(ctx) => ctx,
                        Err(refusal) => {
                            self.error = Some(refusal);
                            return;
                        }
                    };
                    Some(PaintedProvenance {
                        kind: ItemKind::GlyphRun,
                        stream,
                        operation,
                        call_stack,
                        annotation,
                        resource_name: None,
                        signature: sig,
                    })
                }
                _ => None,
            },
            _ => None,
        };
        self.record_item(sig, prov);
    }

    fn draw_image(&mut self, image: Image<'a, '_>, props: ImageDrawProps<'a>) {
        if !self.poll_callback() {
            return;
        }
        let sig = image_signature(&image, &props);
        let prov = match self.frames.last() {
            Some(top) if top.mc_depth == self.mc_depth => match &top.kind {
                FrameKind::Do {
                    stream,
                    operation,
                    name,
                    entered_form: false,
                } => {
                    let leaf_idx = self.frames.len() - 1;
                    let (stream, operation) = (*stream, *operation);
                    let resource_name = match name {
                        Some(s) => match clone_budget_string(s, self.budget) {
                            Ok(s) => Some(s),
                            Err(refusal) => {
                                self.error = Some(refusal);
                                return;
                            }
                        },
                        None => None,
                    };
                    let (annotation, call_stack) = match self.enclosing_context(leaf_idx) {
                        Ok(ctx) => ctx,
                        Err(refusal) => {
                            self.error = Some(refusal);
                            return;
                        }
                    };
                    Some(PaintedProvenance {
                        kind: ItemKind::Image,
                        stream,
                        operation,
                        call_stack,
                        annotation,
                        resource_name,
                        signature: sig,
                    })
                }
                FrameKind::InlineImage { stream, operation } => {
                    let leaf_idx = self.frames.len() - 1;
                    let (stream, operation) = (*stream, *operation);
                    let (annotation, call_stack) = match self.enclosing_context(leaf_idx) {
                        Ok(ctx) => ctx,
                        Err(refusal) => {
                            self.error = Some(refusal);
                            return;
                        }
                    };
                    Some(PaintedProvenance {
                        kind: ItemKind::InlineImage,
                        stream,
                        operation,
                        call_stack,
                        annotation,
                        resource_name: None,
                        signature: sig,
                    })
                }
                _ => None,
            },
            _ => None,
        };
        self.record_item(sig, prov);
    }

    fn pop_clip(&mut self) {
        self.clip_depth = self.clip_depth.saturating_sub(1);
        while let Some(pos) = self.frames.iter().rposition(|frame| match frame.kind {
            FrameKind::Annotation { .. } => self.clip_depth < frame.clip_depth,
            FrameKind::Do {
                entered_form: true, ..
            } => self.clip_depth <= frame.clip_depth,
            _ => false,
        }) {
            let is_annot = matches!(self.frames[pos].kind, FrameKind::Annotation { .. });
            let frame_mc = self.frames[pos].mc_depth;
            if is_annot {
                self.frames.truncate(pos);
                self.mc_depth = frame_mc.saturating_sub(1);
            } else {
                self.frames.truncate(pos + 1);
                if let FrameKind::Do { entered_form, .. } = &mut self.frames[pos].kind {
                    *entered_form = false;
                }
                self.mc_depth = frame_mc;
            }
        }
    }

    fn pop_transparency_group(&mut self) {}

    fn begin_marked_content(&mut self, tag: &[u8], _: Option<&Dict<'_>>) {
        self.mc_depth = self.mc_depth.saturating_add(1);
        if self.error.is_some() {
            return;
        }
        let Some(rest) = tag.strip_prefix(self.prefix) else {
            return;
        };
        let kind = match parse_tag(rest, self.budget) {
            Ok(Some(kind)) => kind,
            Ok(None) => return,
            Err(refusal) => {
                self.error = Some(refusal);
                return;
            }
        };
        if let Err(refusal) = self.frames.try_push(OpenFrame {
            kind,
            mc_depth: self.mc_depth,
            clip_depth: self.clip_depth,
        }) {
            self.error = Some(refusal);
        }
    }

    fn end_marked_content(&mut self) {
        let floor = self.locked_mc_floor();
        if self.mc_depth <= floor {
            // Malformed unmatched `EMC` inside a Form XObject or annotation
            // appearance stream cannot pop or shift enclosing parent frames.
            return;
        }
        if let Some(top) = self.frames.last()
            && top.mc_depth == self.mc_depth
            && !top.is_locked()
        {
            self.frames.pop();
        }
        self.mc_depth -= 1;
    }
}

/// Parses the suffix after `<prefix>` of a provenance marked-content tag.
fn parse_tag(rest: &[u8], budget: &Budget) -> Result<Option<FrameKind>, LimitExceeded> {
    if let Some(body) = rest.strip_prefix(b"op.") {
        let Some((stream, operation)) = parse_u32_pair(body) else {
            return Ok(None);
        };
        return Ok(Some(FrameKind::Op { stream, operation }));
    }
    if let Some(body) = rest.strip_prefix(b"bi.") {
        let Some((stream, operation)) = parse_u32_pair(body) else {
            return Ok(None);
        };
        return Ok(Some(FrameKind::InlineImage { stream, operation }));
    }
    if let Some(body) = rest.strip_prefix(b"annot.") {
        let Some((annotation, appearance_stream)) = parse_u32_pair(body) else {
            return Ok(None);
        };
        return Ok(Some(FrameKind::Annotation {
            origin: AnnotationOrigin {
                annotation,
                appearance_stream,
            },
        }));
    }
    if let Some(body) = rest.strip_prefix(b"do.") {
        let Some((stream, operation, hex)) = parse_u32_pair_and_hex(body) else {
            return Ok(None);
        };
        let name = decode_hex_name(hex, budget)?;
        return Ok(Some(FrameKind::Do {
            stream,
            operation,
            name,
            entered_form: false,
        }));
    }
    if let Some(body) = rest.strip_prefix(b"sh.") {
        let Some((stream, operation, hex)) = parse_u32_pair_and_hex(body) else {
            return Ok(None);
        };
        let name = decode_hex_name(hex, budget)?;
        return Ok(Some(FrameKind::Shading {
            stream,
            operation,
            name,
        }));
    }
    Ok(None)
}

fn parse_u32_pair(bytes: &[u8]) -> Option<(u32, u32)> {
    let dot = bytes.iter().position(|&b| b == b'.')?;
    let a = std::str::from_utf8(&bytes[..dot])
        .ok()?
        .parse::<u32>()
        .ok()?;
    let b = std::str::from_utf8(&bytes[dot + 1..])
        .ok()?
        .parse::<u32>()
        .ok()?;
    Some((a, b))
}

fn parse_u32_pair_and_hex(bytes: &[u8]) -> Option<(u32, u32, &[u8])> {
    let dot1 = bytes.iter().position(|&b| b == b'.')?;
    let a = std::str::from_utf8(&bytes[..dot1])
        .ok()?
        .parse::<u32>()
        .ok()?;
    let after1 = &bytes[dot1 + 1..];
    let dot2 = after1.iter().position(|&b| b == b'.')?;
    let b = std::str::from_utf8(&after1[..dot2])
        .ok()?
        .parse::<u32>()
        .ok()?;
    Some((a, b, &after1[dot2 + 1..]))
}

/// Decodes lowercase hex-encoded PDF name bytes under `budget`, falling back
/// to Latin-1 -> UTF-8 for non-UTF-8 PDF names.
fn decode_hex_name(hex: &[u8], budget: &Budget) -> Result<Option<String>, LimitExceeded> {
    if hex.is_empty() || !hex.len().is_multiple_of(2) {
        return Ok(None);
    }
    let mut raw = BudgetVec::new(budget);
    raw.try_reserve_exact(hex.len() / 2)?;
    let mut i = 0;
    while i < hex.len() {
        let Some(hi) = hex_nibble(hex[i]) else {
            return Ok(None);
        };
        let Some(lo) = hex_nibble(hex[i + 1]) else {
            return Ok(None);
        };
        raw.try_push((hi << 4) | lo)?;
        i += 2;
    }
    let bytes = raw.into_vec();
    match String::from_utf8(bytes) {
        Ok(s) => Ok(Some(s)),
        Err(err) => {
            let raw_bytes = err.into_bytes();
            let mut utf8 = BudgetVec::new(budget);
            utf8.try_reserve(raw_bytes.len())?;
            let mut buf = [0u8; 4];
            for byte in raw_bytes {
                let encoded = char::from(byte).encode_utf8(&mut buf);
                utf8.try_extend_from_slice(encoded.as_bytes())?;
            }
            Ok(Some(
                String::from_utf8(utf8.into_vec()).expect("valid UTF-8 from char encoding"),
            ))
        }
    }
}

const fn hex_nibble(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

fn clone_budget_string(text: &str, budget: &Budget) -> Result<String, LimitExceeded> {
    if text.is_empty() {
        return Ok(String::new());
    }
    let mut bytes = BudgetVec::new(budget);
    bytes.try_extend_from_slice(text.as_bytes())?;
    Ok(String::from_utf8(bytes.into_vec()).expect("cloned from valid &str"))
}

fn clone_provenance(
    prov: &PaintedProvenance,
    budget: &Budget,
) -> Result<PaintedProvenance, LimitExceeded> {
    let mut call_stack = BudgetVec::new(budget);
    call_stack.try_reserve_exact(prov.call_stack.len())?;
    for frame in &prov.call_stack {
        let xobject_name = match &frame.xobject_name {
            Some(name) => Some(clone_budget_string(name, budget)?),
            None => None,
        };
        call_stack.try_push(InvocationFrame {
            stream: frame.stream,
            operation: frame.operation,
            xobject_name,
        })?;
    }
    let resource_name = match &prov.resource_name {
        Some(name) => Some(clone_budget_string(name, budget)?),
        None => None,
    };
    budget.charge(
        LimitCode::MemorySize,
        std::mem::size_of::<PaintedProvenance>() as u64,
    )?;
    Ok(PaintedProvenance {
        kind: prov.kind,
        stream: prov.stream,
        operation: prov.operation,
        call_stack: call_stack.into_vec(),
        annotation: prov.annotation,
        resource_name,
        signature: prov.signature,
    })
}

/// Returns the source of each glyph run of the page's content, indexed by
/// the run's place, after checking `checks` against them. A run that a Form
/// XObject shows has no source, and the result is empty when the runs
/// disagree or when the content's own marked content uses the tags. `cache`
/// holds what the first interpretation read.
pub(crate) fn run_sources<'a>(
    page: &Page<'a>,
    display: Display,
    settings: InterpreterSettings,
    cache: &InterpreterCache<'a>,
    content: &PageContent,
    checks: &[RunCheck],
    budget: &Budget,
) -> Result<Vec<Option<Source>>, Error> {
    let Some(last) = checks.last() else {
        return Ok(Vec::new());
    };
    let Some(data) = marked(content, budget)? else {
        return Ok(Vec::new());
    };
    let mut context = extraction_context(page, display, cache, settings);
    let mut device = OriginDevice {
        origins: BudgetVec::new(budget),
        // The runs after the last checked one cannot name a glyph.
        wanted: last.run.saturating_add(1),
        depth: 0,
        clip_depth: 0,
        form: None,
        open: None,
        error: None,
    };
    interpret(
        TypedIter::new(&data),
        page.resources(),
        &mut context,
        &mut device,
    );
    if let Some(refusal) = device.error {
        return Err(refusal.into());
    }
    let origins = device.origins;
    let agrees = checks.iter().all(|check| {
        origins
            .get(check.run as usize)
            .is_none_or(|origin| origin.glyphs == check.glyphs && origin.first == check.first)
    });
    if !agrees {
        return Ok(Vec::new());
    }
    let operations = content.operations().operations();
    let mut sources = BudgetVec::new(budget);
    sources.try_reserve_exact(origins.len())?;
    sources.try_extend(origins.iter().map(|origin| {
        let operation = origin.operation?;
        let stream = content.stream_of(operations.get(operation as usize)?)?;
        Some(Source {
            stream: stream.number(),
            operation,
        })
    }))?;
    Ok(sources.into_vec())
}

/// Returns the content with each text-showing operation tagged with its
/// index and each `Do` wrapped to exclude its Form's tags, or `None` when
/// the content's own marked content uses our tag prefix.
fn marked(content: &PageContent, budget: &Budget) -> Result<Option<Vec<u8>>, LimitExceeded> {
    let operations = content.operations();
    let data = operations.data();
    let mut wrapped = 0usize;
    for operation in operations.operations() {
        let operator = operations.operator(operation);
        if SHOWS_TEXT.contains(&operator) || operator == b"Do" {
            wrapped += 1;
        } else if matches!(operator, b"BMC" | b"BDC")
            && let Some(Value::Name(tag)) = operations.operands(operation).first()
            && tag.as_bytes().starts_with(TAG)
        {
            return Ok(None);
        }
    }
    // Each wrapped operation gains "\n/<TAG><index> BMC\n" and "\nEMC\n".
    let extra = wrapped.saturating_mul(FORM_TAG.len() + 20);
    let mut marked = BudgetVec::new(budget);
    marked.try_reserve_exact(data.len().saturating_add(extra))?;
    let mut copied = 0;
    for (index, operation) in operations.operations().iter().enumerate() {
        let operator = operations.operator(operation);
        if !SHOWS_TEXT.contains(&operator) && operator != b"Do" {
            continue;
        }
        let span = operation.span();
        marked.try_extend(data[copied..span.start].iter().copied())?;
        marked.try_extend(b"\n/".iter().copied())?;
        if operator == b"Do" {
            marked.try_extend(FORM_TAG.iter().copied())?;
        } else {
            marked.try_extend(TAG.iter().copied())?;
            marked.try_extend(index.to_string().bytes())?;
        }
        marked.try_extend(b" BMC\n".iter().copied())?;
        marked.try_extend(data[span.clone()].iter().copied())?;
        marked.try_extend(b"\nEMC\n".iter().copied())?;
        copied = span.end;
    }
    marked.try_extend(data[copied..].iter().copied())?;
    Ok(Some(marked.into_vec()))
}

/// A device that records, for each glyph run, the operation whose marked
/// content encloses it.
struct OriginDevice<'b> {
    origins: BudgetVec<'b, Origin>,
    /// The runs to record.
    wanted: u32,
    /// The depth of the marked content in force.
    depth: usize,
    /// Forms push a clip before interpreting their content. A malformed
    /// EMC inside a Form cannot close its wrapper while that clip is live.
    clip_depth: usize,
    /// The marked-content and clip depths of the page's enclosing `Do`.
    form: Option<(usize, usize)>,
    /// The operation whose marked content is open, and its depth.
    open: Option<(u32, usize)>,
    error: Option<LimitExceeded>,
}

impl<'a> Device<'a> for OriginDevice<'_> {
    fn draw_path(&mut self, _: &BezPath, _: DrawProps<'a>, _: &DrawMode) {}
    fn push_clip_path(&mut self, _: &ClipPath) {
        self.clip_depth = self.clip_depth.saturating_add(1);
    }
    fn push_transparency_group(&mut self, _: f32, _: Option<SoftMask<'a>>, _: BlendMode) {}

    fn draw_glyph_run(&mut self, run: &GlyphRun<'_, 'a>, props: DrawProps<'a>, _: &DrawMode) {
        if self.error.is_some() || self.origins.len() >= self.wanted as usize {
            return;
        }
        let glyphs = run.glyphs();
        let first = glyphs.first().map_or([0; 6], |glyph| {
            (props.transform * glyph.transform())
                .as_coeffs()
                .map(f64::to_bits)
        });
        let origin = Origin {
            // An operation's marked content holds only that operation, so a
            // run inside it, and not inside a Form XObject, is its own.
            operation: self
                .open
                .filter(|&(_, depth)| depth == self.depth && self.form.is_none())
                .map(|(operation, _)| operation),
            glyphs: u32::try_from(glyphs.len()).unwrap_or(u32::MAX),
            first,
        };
        if let Err(refusal) = self.origins.try_push(origin) {
            self.error = Some(refusal);
        }
    }

    fn draw_image(&mut self, _: Image<'a, '_>, _: ImageDrawProps<'a>) {}
    fn pop_clip(&mut self) {
        self.clip_depth = self.clip_depth.saturating_sub(1);
    }
    fn pop_transparency_group(&mut self) {}

    fn begin_marked_content(&mut self, tag: &[u8], _: Option<&Dict<'_>>) {
        self.depth = self.depth.saturating_add(1);
        if self.form.is_some() {
            return;
        }
        if tag == FORM_TAG {
            self.form = Some((self.depth, self.clip_depth));
            return;
        }
        if let Some(index) = tag.strip_prefix(TAG)
            && let Ok(index) = std::str::from_utf8(index)
            && let Ok(operation) = index.parse()
        {
            self.open = Some((operation, self.depth));
        }
    }

    fn end_marked_content(&mut self) {
        if self.form == Some((self.depth, self.clip_depth)) {
            self.form = None;
        }
        if self.open.is_some_and(|(_, depth)| depth == self.depth) {
            self.open = None;
        }
        self.depth = self.depth.saturating_sub(1);
    }
}

#[cfg(test)]
mod tests {
    use std::io::Write as _;
    use std::sync::Arc;

    use flate2::Compression;
    use flate2::write::ZlibEncoder;
    use pyropdf_store::budget::{Budget, CancellationToken, LimitCode};
    use pyropdf_store::index::Index;
    use pyropdf_store::memo::Reads;
    use pyropdf_store::pages::PageIndex;
    use pyropdf_store::save::FileBase;
    use pyropdf_store::value::Bytes;
    use pyropdf_store::version::{Document, Version};

    use super::*;
    use crate::bridge::preflight::MAX_STREAM_DEPTH;
    use crate::bridge::provenance_overlay::build_provenance_overlay;
    use crate::extract::{ItemKinds, page_content};

    /// Builds a valid PDF from raw object bodies (numbered 1..=objects.len()),
    /// where object 1 is the `/Catalog`.
    fn build_pdf(objects: &[Vec<u8>]) -> Vec<u8> {
        let mut out = b"%PDF-1.7\n%\xE2\xE3\xCF\xD3\n".to_vec();
        let mut offsets = Vec::with_capacity(objects.len());
        for (idx, body) in objects.iter().enumerate() {
            offsets.push(out.len());
            out.extend_from_slice(format!("{} 0 obj\n", idx + 1).as_bytes());
            out.extend_from_slice(body);
            out.extend_from_slice(b"\nendobj\n");
        }
        let xref = out.len();
        out.extend_from_slice(
            format!("xref\n0 {}\n0000000000 65535 f \n", objects.len() + 1).as_bytes(),
        );
        for offset in offsets {
            out.extend_from_slice(format!("{offset:010} 00000 n \n").as_bytes());
        }
        out.extend_from_slice(
            format!(
                "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n",
                objects.len() + 1
            )
            .as_bytes(),
        );
        out
    }

    fn stream_obj(extra_dict: &str, body: &[u8]) -> Vec<u8> {
        let mut out = Vec::new();
        if extra_dict.is_empty() {
            out.extend_from_slice(format!("<< /Length {} >>\nstream\n", body.len()).as_bytes());
        } else {
            out.extend_from_slice(
                format!("<< {extra_dict} /Length {} >>\nstream\n", body.len()).as_bytes(),
            );
        }
        out.extend_from_slice(body);
        out.extend_from_slice(b"\nendstream");
        out
    }

    fn flate_stream_obj(extra_dict: &str, plain: &[u8]) -> Vec<u8> {
        let mut encoder = ZlibEncoder::new(Vec::new(), Compression::default());
        encoder.write_all(plain).unwrap();
        let compressed = encoder.finish().unwrap();
        let mut dict = String::from("/Filter /FlateDecode");
        if !extra_dict.is_empty() {
            dict.push(' ');
            dict.push_str(extra_dict);
        }
        stream_obj(&dict, &compressed)
    }

    fn open_version(bytes: Vec<u8>) -> (Arc<Version>, PageIndex) {
        let budget = Budget::default();
        let bytes = Bytes::from(bytes);
        let index = Index::open(bytes, &budget).unwrap();
        let base = FileBase::new(index, &budget).unwrap();
        let doc = Document::new(Arc::new(base), None);
        let version = doc.current();
        let mut reads = Reads::new(&version, &budget);
        let pages = PageIndex::build(&mut reads).unwrap();
        drop(reads);
        (version, pages)
    }

    fn axial_shading_dict() -> Vec<u8> {
        b"<< /ShadingType 2 /ColorSpace /DeviceRGB /Coords [0 0 100 0] \
           /Function << /FunctionType 2 /Domain [0 1] /C0 [1 0 0] /C1 [0 0 1] /N 1 >> >>"
            .to_vec()
    }

    fn image_xobject_1x1_rgb() -> Vec<u8> {
        stream_obj(
            "/Type /XObject /Subtype /Image /Width 1 /Height 1 /BitsPerComponent 8 /ColorSpace /DeviceRGB",
            &[255, 0, 128],
        )
    }

    fn font_dict() -> Vec<u8> {
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>".to_vec()
    }

    #[test]
    fn page_content_reports_all_painted_operator_kinds_and_names() {
        // Object 1: Catalog, 2: Pages, 3: Page, 4: Contents, 5: Font, 6: Image XObject, 7: Shading
        let mut content = Vec::new();
        // Op 0..3: BT /F1 12 Tf 10 80 Td
        // Op 4: (Hello) Tj
        // Op 5: [(Wo) -50 (rld)] TJ
        // Op 6: ET
        content.extend_from_slice(b"BT /F1 12 Tf 10 80 Td (Hello) Tj [(Wo) -50 (rld)] TJ ET\n");
        // Op 7: 10 10 30 20 re
        // Op 8: W
        // Op 9: f  (tests clip push inside f!)
        // Op 10: 50 10 20 20 re
        // Op 11: B (fill + stroke: two callbacks, same op 11)
        content.extend_from_slice(b"q 10 10 30 20 re W f Q\n50 10 20 20 re B\n");
        // Op: /Im1 Do (XObject Image)
        content.extend_from_slice(b"q 10 0 0 10 0 0 cm /Im1 Do Q\n");
        // Op: inline image BI ... ID ... EI
        content.extend_from_slice(b"q 10 0 0 10 20 0 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x80 EI Q\n");
        // Op: /Sh1 sh
        content.extend_from_slice(b"/Sh1 sh\n");

        let pdf = build_pdf(&[
            b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>".to_vec(),
            b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R \
               /Resources << /Font << /F1 5 0 R >> /XObject << /Im1 6 0 R >> /Shading << /Sh1 7 0 R >> >> >>"
                .to_vec(),
            stream_obj("", &content),
            font_dict(),
            image_xobject_1x1_rgb(),
            axial_shading_dict(),
        ]);

        let (version, pages) = open_version(pdf);
        let budget = Budget::default();
        let prov = page_provenance(&version, &pages, 0, &Fallback::default(), false, &budget)
            .expect("provenance should succeed");
        assert!(prov.verified);

        // Verify glyph runs: (Hello) Tj at op 3, [(Wo) -50 (rld)] TJ at op 4.
        assert_eq!(prov.glyph_runs.len(), 2);
        let run0 = prov.glyph_runs[0].as_ref().unwrap();
        assert_eq!(run0.kind, ItemKind::GlyphRun);
        assert_eq!(run0.stream, 4);
        assert_eq!(run0.operation, 3);
        assert!(run0.call_stack.is_empty());
        assert_eq!(run0.annotation, None);
        assert_eq!(run0.resource_name, None);
        assert!(matches!(
            run0.signature,
            ItemSignature::GlyphRun { glyphs: 5, .. }
        ));

        let run1 = prov.glyph_runs[1].as_ref().unwrap();
        assert_eq!(run1.stream, 4);
        assert_eq!(run1.operation, 4);
        assert!(matches!(
            run1.signature,
            ItemSignature::GlyphRun { glyphs: 5, .. }
        ));

        // Verify paths: `W f` (op 9) + `B` (op 12, Fill and Stroke -> 2 callbacks).
        assert_eq!(prov.paths.len(), 3);
        let p0 = prov.paths[0].as_ref().unwrap();
        assert_eq!(p0.kind, ItemKind::Path);
        assert_eq!(p0.stream, 4);
        assert_eq!(p0.operation, 9);
        assert!(p0.call_stack.is_empty());
        assert_eq!(p0.resource_name, None);

        let p1 = prov.paths[1].as_ref().unwrap();
        let p2 = prov.paths[2].as_ref().unwrap();
        assert_eq!(p1.stream, 4);
        assert_eq!(p2.stream, 4);
        assert_eq!(p1.operation, 12);
        assert_eq!(p2.operation, 12);
        assert!(matches!(
            p1.signature,
            ItemSignature::Path {
                draw: DrawKind::Fill,
                ..
            }
        ));
        assert!(matches!(
            p2.signature,
            ItemSignature::Path {
                draw: DrawKind::Stroke,
                ..
            }
        ));

        // Verify images: XObject `/Im1 Do` (xref 6) and InlineImage (`BI..EI`, xref 0).
        assert_eq!(prov.images.len(), 2);
        let im_xobj = prov.images[0].as_ref().unwrap();
        assert_eq!(im_xobj.kind, ItemKind::Image);
        assert_eq!(im_xobj.stream, 4);
        assert_eq!(im_xobj.operation, 15);
        assert_eq!(im_xobj.resource_name.as_deref(), Some("Im1"));
        assert!(matches!(
            im_xobj.signature,
            ItemSignature::Image {
                width: 1,
                height: 1,
                xref: 6,
                ..
            }
        ));

        let im_inline = prov.images[1].as_ref().unwrap();
        assert_eq!(im_inline.kind, ItemKind::InlineImage);
        assert_eq!(im_inline.stream, 4);
        assert_eq!(im_inline.operation, 19);
        assert_eq!(im_inline.resource_name, None);
        assert!(matches!(
            im_inline.signature,
            ItemSignature::Image {
                width: 1,
                height: 1,
                xref: 0,
                ..
            }
        ));

        // Verify shading: `/Sh1 sh`.
        assert_eq!(prov.shadings.len(), 1);
        let sh = prov.shadings[0].as_ref().unwrap();
        assert_eq!(sh.kind, ItemKind::Shading);
        assert_eq!(sh.stream, 4);
        assert_eq!(sh.operation, 21);
        assert_eq!(sh.resource_name.as_deref(), Some("Sh1"));
        assert!(matches!(
            sh.signature,
            ItemSignature::Path {
                is_shading_paint: true,
                ..
            }
        ));

        // Total paint-order items equals sum of categories.
        assert_eq!(prov.items.len(), 2 + 3 + 2 + 1);
    }

    #[test]
    fn multi_stream_contents_reports_exact_stream_and_joined_op_index() {
        // Stream 4 has operands + first Tj (op 4), and operand of second Tj;
        // Stream 5 holds the `Tj` operator (op 5) and a path fill `f` (op 8).
        let pdf = build_pdf(&[
            b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>".to_vec(),
            b"<< /Type /Page /Parent 2 0 R /Contents [4 0 R 5 0 R] /Resources << /Font << /F1 6 0 R >> >> >>"
                .to_vec(),
            stream_obj("", b"BT /F1 12 Tf 10 50 Td (First) Tj (Split)"),
            stream_obj("", b"Tj ET 5 5 10 10 re f"),
            font_dict(),
        ]);

        let (version, pages) = open_version(pdf);
        let prov = page_provenance(
            &version,
            &pages,
            0,
            &Fallback::default(),
            false,
            &Budget::default(),
        )
        .unwrap();
        assert!(prov.verified);
        assert_eq!(prov.glyph_runs.len(), 2);
        let r0 = prov.glyph_runs[0].as_ref().unwrap();
        let r1 = prov.glyph_runs[1].as_ref().unwrap();
        assert_eq!((r0.stream, r0.operation), (4, 3));
        assert_eq!((r1.stream, r1.operation), (5, 4));
        assert_eq!(prov.paths.len(), 1);
        let p0 = prov.paths[0].as_ref().unwrap();
        assert_eq!((p0.stream, p0.operation), (5, 7));
    }

    #[test]
    fn nested_forms_three_levels_deep_track_full_call_stack() {
        // Page (stream 4, op 0 `/Fm1 Do`)
        //  -> Form1 (stream 6, op 0 `/Fm2 Do`, compressed with FlateDecode!)
        //   -> Form2 (stream 7, op 0 `/Fm3 Do`)
        //    -> Form3 (stream 8): glyph run, path, Image XObject `/Img#20One`, inline image, shading `/Sh#2FGrad`
        let form3_body = b"BT /F1 10 Tf 5 10 Td (Deep) Tj ET \
                           2 2 8 8 re S \
                           /Img#20One Do \
                           BI /W 1 /H 1 /CS /G /BPC 8 ID \xff EI \
                           /Sh#2FGrad sh";
        let pdf = build_pdf(&[
            b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>".to_vec(),
            b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R \
               /Resources << /XObject << /Fm1 6 0 R >> >> >>"
                .to_vec(),
            stream_obj("", b"/Fm1 Do"),
            font_dict(),
            flate_stream_obj(
                "/Type /XObject /Subtype /Form /BBox [0 0 200 100] /Resources << /XObject << /Fm2 7 0 R >> >>",
                b"q /Fm2 Do Q",
            ),
            stream_obj(
                "/Type /XObject /Subtype /Form /BBox [0 0 200 100] /Resources << /XObject << /Fm3 8 0 R >> >>",
                b"1 0 0 1 10 10 cm /Fm3 Do",
            ),
            stream_obj(
                "/Type /XObject /Subtype /Form /BBox [0 0 200 100] \
                 /Resources << /Font << /F1 5 0 R >> /XObject << /Img#20One 9 0 R >> /Shading << /Sh#2FGrad 10 0 R >> >>",
                form3_body,
            ),
            image_xobject_1x1_rgb(),
            axial_shading_dict(),
        ]);

        let (version, pages) = open_version(pdf);
        let prov = page_provenance(
            &version,
            &pages,
            0,
            &Fallback::default(),
            false,
            &Budget::default(),
        )
        .unwrap();
        assert!(prov.verified);

        let expected_stack = vec![
            InvocationFrame {
                stream: 4,
                operation: 0,
                xobject_name: Some("Fm1".to_owned()),
            },
            InvocationFrame {
                stream: 6,
                operation: 1,
                xobject_name: Some("Fm2".to_owned()),
            },
            InvocationFrame {
                stream: 7,
                operation: 1,
                xobject_name: Some("Fm3".to_owned()),
            },
        ];

        assert_eq!(prov.glyph_runs.len(), 1);
        let run = prov.glyph_runs[0].as_ref().unwrap();
        assert_eq!(run.stream, 8);
        assert_eq!(run.operation, 3);
        assert_eq!(run.call_stack, expected_stack);
        assert_eq!(run.annotation, None);

        assert_eq!(prov.paths.len(), 1);
        let path = prov.paths[0].as_ref().unwrap();
        assert_eq!(path.stream, 8);
        assert_eq!(path.operation, 6);
        assert_eq!(path.call_stack, expected_stack);

        assert_eq!(prov.images.len(), 2);
        let xobj_img = prov.images[0].as_ref().unwrap();
        assert_eq!(xobj_img.kind, ItemKind::Image);
        assert_eq!(xobj_img.stream, 8);
        assert_eq!(xobj_img.operation, 7);
        assert_eq!(xobj_img.resource_name.as_deref(), Some("Img One"));
        assert_eq!(xobj_img.call_stack, expected_stack);

        let inline_img = prov.images[1].as_ref().unwrap();
        assert_eq!(inline_img.kind, ItemKind::InlineImage);
        assert_eq!(inline_img.stream, 8);
        assert_eq!(inline_img.operation, 8);
        assert_eq!(inline_img.resource_name, None);
        assert_eq!(inline_img.call_stack, expected_stack);

        assert_eq!(prov.shadings.len(), 1);
        let sh = prov.shadings[0].as_ref().unwrap();
        assert_eq!(sh.kind, ItemKind::Shading);
        assert_eq!(sh.stream, 8);
        assert_eq!(sh.operation, 9);
        assert_eq!(sh.resource_name.as_deref(), Some("Sh/Grad"));
        assert_eq!(sh.call_stack, expected_stack);
    }

    #[test]
    fn reused_and_diamond_shared_form_reports_distinct_call_stacks_and_deduplicates_stream() {
        // Page invokes `/Shared Do` directly twice AND via `/BranchA Do` + `/BranchB Do`
        // (both referencing the same Form object 8, with no own `/Resources` on 8 so it
        // inherits `/F1` from the caller!).
        let pdf = build_pdf(&[
            b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>".to_vec(),
            b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R \
               /Resources << /Font << /F1 5 0 R >> /XObject << /BranchA 6 0 R /BranchB 7 0 R /Shared 8 0 R >> >> >>"
                .to_vec(),
            // op 0: /Shared Do, op 1: cm, op 2: /Shared Do, op 3: /BranchA Do, op 4: /BranchB Do
            stream_obj(
                "",
                b"/Shared Do 1 0 0 1 50 0 cm /Shared Do /BranchA Do /BranchB Do",
            ),
            font_dict(),
            stream_obj(
                "/Type /XObject /Subtype /Form /BBox [0 0 200 100] \
                 /Resources << /Font << /F1 5 0 R >> /XObject << /Leaf 8 0 R >> >>",
                b"/Leaf Do",
            ),
            stream_obj(
                "/Type /XObject /Subtype /Form /BBox [0 0 200 100] \
                 /Resources << /Font << /F1 5 0 R >> /XObject << /Leaf 8 0 R >> >>",
                b"1 0 0 1 0 20 cm /Leaf Do",
            ),
            // Shared Form 8 has NO `/Resources` of its own (inherits caller's `/Font /F1`).
            stream_obj(
                "/Type /XObject /Subtype /Form /BBox [0 0 200 100]",
                b"BT /F1 12 Tf 10 10 Td (Hit) Tj ET",
            ),
        ]);

        let (version, pages) = open_version(pdf);
        let budget = Budget::default();

        // Overlay must instrument only 4 distinct streams: page + BranchA + BranchB + Shared (once!).
        let overlay = build_provenance_overlay(&version, &pages, 0, false, &budget).unwrap();
        assert_eq!(overlay.instrumented_streams, 4);

        let prov =
            page_provenance(&version, &pages, 0, &Fallback::default(), false, &budget).unwrap();
        assert!(prov.verified);
        assert_eq!(prov.glyph_runs.len(), 4);

        let stacks: Vec<&[InvocationFrame]> = prov
            .glyph_runs
            .iter()
            .map(|r| {
                let r = r.as_ref().unwrap();
                assert_eq!((r.stream, r.operation), (8, 3));
                r.call_stack.as_slice()
            })
            .collect();

        assert_eq!(
            stacks[0],
            &[InvocationFrame {
                stream: 4,
                operation: 0,
                xobject_name: Some("Shared".to_owned()),
            }]
        );
        assert_eq!(
            stacks[1],
            &[InvocationFrame {
                stream: 4,
                operation: 2,
                xobject_name: Some("Shared".to_owned()),
            }]
        );
        assert_eq!(
            stacks[2],
            &[
                InvocationFrame {
                    stream: 4,
                    operation: 3,
                    xobject_name: Some("BranchA".to_owned()),
                },
                InvocationFrame {
                    stream: 6,
                    operation: 0,
                    xobject_name: Some("Leaf".to_owned()),
                },
            ]
        );
        assert_eq!(
            stacks[3],
            &[
                InvocationFrame {
                    stream: 4,
                    operation: 4,
                    xobject_name: Some("BranchB".to_owned()),
                },
                InvocationFrame {
                    stream: 7,
                    operation: 1,
                    xobject_name: Some("Leaf".to_owned()),
                },
            ]
        );
    }

    #[test]
    fn annotations_direct_state_subdict_shared_and_nested_forms() {
        // Object 6: Annot A with direct `/AP /N 7 0 R` (which paints text + calls nested Form 12)
        // Object 8: Annot B with `/AP /N << /Yes 9 0 R /Off 10 0 R >>` and `/AS /Yes`
        // Object 11: Annot C sharing `/AP /N 7 0 R` with Annot A!
        // Object 13: Hidden Annot D (`F = 2`) that must be skipped
        // Object 14: Annot E with state subdict and no `/AS` (falls back to `/Off` -> stream 10)
        let pdf = build_pdf(&[
            b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>".to_vec(),
            b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R \
               /Resources << /Font << /F1 5 0 R >> >> \
               /Annots [6 0 R 8 0 R 11 0 R 13 0 R 14 0 R] >>"
                .to_vec(),
            stream_obj("", b"BT /F1 12 Tf 10 80 Td (Page) Tj ET"),
            font_dict(),
            // 6: Annot A (direct /AP /N -> 7)
            b"<< /Type /Annot /Subtype /Stamp /Rect [10 10 60 30] /AP << /N 7 0 R >> >>".to_vec(),
            // 7: AP stream for Annot A & Annot C (draws text + invokes nested Form 12)
            stream_obj(
                "/Type /XObject /Subtype /Form /BBox [0 0 50 20] \
                 /Resources << /Font << /F1 5 0 R >> /XObject << /Sub 12 0 R >> >>",
                b"BT /F1 10 Tf 2 5 Td (Stamp) Tj ET /Sub Do",
            ),
            // 8: Annot B (/AS /Yes -> 9)
            b"<< /Type /Annot /Subtype /Widget /Rect [70 10 110 30] /AS /Yes \
               /AP << /N << /Yes 9 0 R /Off 10 0 R >> >> >>"
                .to_vec(),
            // 9: /Yes stream
            stream_obj(
                "/Type /XObject /Subtype /Form /BBox [0 0 40 20]",
                b"2 2 10 10 re f",
            ),
            // 10: /Off stream
            stream_obj(
                "/Type /XObject /Subtype /Form /BBox [0 0 40 20]",
                b"1 1 8 8 re S",
            ),
            // 11: Annot C sharing stream 7!
            b"<< /Type /Annot /Subtype /Stamp /Rect [120 10 170 30] /AP << /N 7 0 R >> >>".to_vec(),
            // 12: Nested Form inside AP stream 7
            stream_obj(
                "/Type /XObject /Subtype /Form /BBox [0 0 50 20]",
                b"0 0 5 5 re f",
            ),
            // 13: Hidden annotation (F = 2) -> skipped by hayro
            b"<< /Type /Annot /Subtype /Stamp /F 2 /Rect [0 0 20 20] /AP << /N 9 0 R >> >>"
                .to_vec(),
            // 14: Annot E without /AS -> falls back to /Off (stream 10)
            b"<< /Type /Annot /Subtype /Widget /Rect [10 40 50 60] \
               /AP << /N << /Yes 9 0 R /Off 10 0 R >> >> >>"
                .to_vec(),
        ]);

        let (version, pages) = open_version(pdf);
        let budget = Budget::default();

        // With annotations = false: only the page glyph run appears.
        let page_only =
            page_provenance(&version, &pages, 0, &Fallback::default(), false, &budget).unwrap();
        assert!(page_only.verified);
        assert_eq!(page_only.glyph_runs.len(), 1);
        assert!(page_only.paths.is_empty());

        // With annotations = true: page + Annot A (text + nested path) + Annot B (/Yes path)
        // + Annot C (shared AP stream 7: text + nested path) + Annot E (/Off fallback path).
        let with_annots =
            page_provenance(&version, &pages, 0, &Fallback::default(), true, &budget).unwrap();
        assert!(with_annots.verified);

        assert_eq!(with_annots.glyph_runs.len(), 3);
        let g_page = with_annots.glyph_runs[0].as_ref().unwrap();
        assert_eq!(g_page.annotation, None);
        assert_eq!((g_page.stream, g_page.operation), (4, 3));

        let g_annot_a = with_annots.glyph_runs[1].as_ref().unwrap();
        assert_eq!(
            g_annot_a.annotation,
            Some(AnnotationOrigin {
                annotation: 6,
                appearance_stream: 7,
            })
        );
        assert_eq!((g_annot_a.stream, g_annot_a.operation), (7, 3));
        assert!(g_annot_a.call_stack.is_empty());

        let g_annot_c = with_annots.glyph_runs[2].as_ref().unwrap();
        assert_eq!(
            g_annot_c.annotation,
            Some(AnnotationOrigin {
                annotation: 11,
                appearance_stream: 7,
            })
        );
        assert_eq!((g_annot_c.stream, g_annot_c.operation), (7, 3));

        // Paths: Annot A's nested Form 12, Annot B's /Yes (9), Annot C's nested Form 12, Annot E's /Off (10).
        assert_eq!(with_annots.paths.len(), 4);
        let p_a = with_annots.paths[0].as_ref().unwrap();
        assert_eq!(
            p_a.annotation,
            Some(AnnotationOrigin {
                annotation: 6,
                appearance_stream: 7,
            })
        );
        assert_eq!((p_a.stream, p_a.operation), (12, 1));
        assert_eq!(
            p_a.call_stack,
            vec![InvocationFrame {
                stream: 7,
                operation: 5,
                xobject_name: Some("Sub".to_owned()),
            }]
        );

        let p_b = with_annots.paths[1].as_ref().unwrap();
        assert_eq!(
            p_b.annotation,
            Some(AnnotationOrigin {
                annotation: 8,
                appearance_stream: 9,
            })
        );
        assert_eq!((p_b.stream, p_b.operation), (9, 1));
        assert!(p_b.call_stack.is_empty());

        let p_c = with_annots.paths[2].as_ref().unwrap();
        assert_eq!(
            p_c.annotation,
            Some(AnnotationOrigin {
                annotation: 11,
                appearance_stream: 7,
            })
        );
        assert_eq!((p_c.stream, p_c.operation), (12, 1));

        let p_e = with_annots.paths[3].as_ref().unwrap();
        assert_eq!(
            p_e.annotation,
            Some(AnnotationOrigin {
                annotation: 14,
                appearance_stream: 10,
            })
        );
        assert_eq!((p_e.stream, p_e.operation), (10, 1));
    }

    #[test]
    fn malformed_unmatched_emc_and_unclosed_bmc_inside_nested_form_and_annot_cannot_corrupt_parent()
    {
        // Form2 (obj 7) has 3 unmatched `EMC`s BEFORE text, 2 more AFTER text, and an unclosed
        // `/Unclosed BMC` at the end!
        // Form1 (obj 6) wraps `/Fm2 Do` in `/UserSpan BMC ... EMC` and paints text AFTER `/Fm2 Do`.
        // Page (obj 4) paints text AFTER `/Fm1 Do`.
        // Annotation (obj 8, AP stream 9) ALSO has unmatched `EMC EMC` before painting text!
        let pdf = build_pdf(&[
            b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>".to_vec(),
            b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R \
               /Resources << /Font << /F1 5 0 R >> /XObject << /Fm1 6 0 R >> >> \
               /Annots [8 0 R] >>"
                .to_vec(),
            stream_obj("", b"/Fm1 Do BT /F1 12 Tf 10 20 Td (PageAfter) Tj ET"),
            font_dict(),
            stream_obj(
                "/Type /XObject /Subtype /Form /BBox [0 0 200 100] \
                 /Resources << /Font << /F1 5 0 R >> /XObject << /Fm2 7 0 R >> >>",
                b"/UserSpan BMC /Fm2 Do EMC BT /F1 12 Tf 10 40 Td (Form1After) Tj ET",
            ),
            stream_obj(
                "/Type /XObject /Subtype /Form /BBox [0 0 200 100] \
                 /Resources << /Font << /F1 5 0 R >> >>",
                b"EMC EMC EMC BT /F1 12 Tf 10 60 Td (Form2Inner) Tj ET EMC EMC /Unclosed BMC",
            ),
            b"<< /Type /Annot /Subtype /Stamp /Rect [10 70 80 95] /AP << /N 9 0 R >> >>".to_vec(),
            stream_obj(
                "/Type /XObject /Subtype /Form /BBox [0 0 70 25] /Resources << /Font << /F1 5 0 R >> >>",
                b"EMC EMC BT /F1 10 Tf 2 5 Td (AnnotInner) Tj ET EMC",
            ),
        ]);

        let (version, pages) = open_version(pdf);
        let prov = page_provenance(
            &version,
            &pages,
            0,
            &Fallback::default(),
            true,
            &Budget::default(),
        )
        .unwrap();
        assert!(prov.verified);
        assert_eq!(prov.glyph_runs.len(), 4);

        // 0: Form2Inner (inside Form1 -> Form2)
        let r0 = prov.glyph_runs[0].as_ref().unwrap();
        assert_eq!((r0.stream, r0.operation), (7, 6));
        assert_eq!(
            r0.call_stack,
            vec![
                InvocationFrame {
                    stream: 4,
                    operation: 0,
                    xobject_name: Some("Fm1".to_owned()),
                },
                InvocationFrame {
                    stream: 6,
                    operation: 1,
                    xobject_name: Some("Fm2".to_owned()),
                },
            ]
        );
        assert_eq!(r0.annotation, None);

        // 1: Form1After (Form1 frame still intact after Form2's malformed EMC & unclosed BMC!)
        let r1 = prov.glyph_runs[1].as_ref().unwrap();
        assert_eq!((r1.stream, r1.operation), (6, 6));
        assert_eq!(
            r1.call_stack,
            vec![InvocationFrame {
                stream: 4,
                operation: 0,
                xobject_name: Some("Fm1".to_owned()),
            }]
        );
        assert_eq!(r1.annotation, None);

        // 2: PageAfter (Page root frame cleanly restored!)
        let r2 = prov.glyph_runs[2].as_ref().unwrap();
        assert_eq!((r2.stream, r2.operation), (4, 4));
        assert!(r2.call_stack.is_empty());
        assert_eq!(r2.annotation, None);

        // 3: AnnotInner (Annotation frame survived unmatched EMCs inside its own stream!)
        let r3 = prov.glyph_runs[3].as_ref().unwrap();
        assert_eq!((r3.stream, r3.operation), (9, 5));
        assert!(r3.call_stack.is_empty());
        assert_eq!(
            r3.annotation,
            Some(AnnotationOrigin {
                annotation: 8,
                appearance_stream: 9,
            })
        );
    }

    #[test]
    fn tag_prefix_collision_falls_back_to_alternate_prefix() {
        // Page uses `/pyropdf.prov.op.4.99 BMC` and Form uses `/pyropdf.prov.1.fake BMC`,
        // forcing the overlay to pick `"pyropdf.prov.2."`.
        let pdf = build_pdf(&[
            b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>".to_vec(),
            b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R \
               /Resources << /Font << /F1 5 0 R >> /XObject << /Fm1 6 0 R >> >> >>"
                .to_vec(),
            stream_obj(
                "",
                b"/pyropdf.prov.op.4.99 BMC BT /F1 12 Tf 10 50 Td (Collision) Tj ET EMC /Fm1 Do",
            ),
            font_dict(),
            stream_obj(
                "/Type /XObject /Subtype /Form /BBox [0 0 200 100] \
                 /Resources << /Font << /F1 5 0 R >> >>",
                b"/pyropdf.prov.1.fake BMC 10 10 20 20 re f EMC",
            ),
        ]);

        let (version, pages) = open_version(pdf);
        let budget = Budget::default();

        let overlay = build_provenance_overlay(&version, &pages, 0, false, &budget).unwrap();
        assert_eq!(overlay.prefix, "pyropdf.prov.2.");

        let prov =
            page_provenance(&version, &pages, 0, &Fallback::default(), false, &budget).unwrap();
        assert!(prov.verified);
        assert_eq!(prov.glyph_runs.len(), 1);
        let run = prov.glyph_runs[0].as_ref().unwrap();
        assert_eq!((run.stream, run.operation), (4, 4));
        assert!(run.call_stack.is_empty());

        assert_eq!(prov.paths.len(), 1);
        let path = prov.paths[0].as_ref().unwrap();
        assert_eq!((path.stream, path.operation), (6, 2));
        assert_eq!(
            path.call_stack,
            vec![InvocationFrame {
                stream: 4,
                operation: 7,
                xobject_name: Some("Fm1".to_owned()),
            }]
        );
    }

    #[test]
    fn cyclic_form_xobjects_refuse_without_infinite_recursion() {
        // Direct self-reference: Form 6 invokes `/Self Do` -> Form 6.
        let self_cycle_pdf = build_pdf(&[
            b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>".to_vec(),
            b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R \
               /Resources << /XObject << /Self 6 0 R >> >> >>"
                .to_vec(),
            stream_obj("", b"/Self Do"),
            font_dict(),
            stream_obj(
                "/Type /XObject /Subtype /Form /BBox [0 0 200 100] \
                 /Resources << /XObject << /Self 6 0 R >> >>",
                b"0 0 10 10 re f /Self Do",
            ),
        ]);
        let (version, pages) = open_version(self_cycle_pdf);
        let err = page_provenance(
            &version,
            &pages,
            0,
            &Fallback::default(),
            false,
            &Budget::default(),
        )
        .unwrap_err();
        assert!(
            matches!(
                err,
                BridgeError::Store(ref e) if e.limit().map(|l| l.code()) == Some(LimitCode::ObjectDepth)
            ),
            "expected ObjectDepth refusal for self-referencing Form, got {err:?}"
        );

        // Mutual cycle: Form 6 -> Form 7 -> Form 6.
        let mutual_cycle_pdf = build_pdf(&[
            b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>".to_vec(),
            b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R \
               /Resources << /XObject << /A 6 0 R >> >> >>"
                .to_vec(),
            stream_obj("", b"/A Do"),
            font_dict(),
            stream_obj(
                "/Type /XObject /Subtype /Form /BBox [0 0 200 100] \
                 /Resources << /XObject << /B 7 0 R >> >>",
                b"/B Do",
            ),
            stream_obj(
                "/Type /XObject /Subtype /Form /BBox [0 0 200 100] \
                 /Resources << /XObject << /A 6 0 R >> >>",
                b"/A Do",
            ),
        ]);
        let (version, pages) = open_version(mutual_cycle_pdf);
        let err = page_provenance(
            &version,
            &pages,
            0,
            &Fallback::default(),
            false,
            &Budget::default(),
        )
        .unwrap_err();
        assert!(
            matches!(
                err,
                BridgeError::Store(ref e) if e.limit().map(|l| l.code()) == Some(LimitCode::ObjectDepth)
            ),
            "expected ObjectDepth refusal for mutual Form cycle, got {err:?}"
        );
    }

    #[test]
    fn nested_forms_beyond_max_stream_depth_refuse_with_object_depth_limit() {
        // Build chain of MAX_STREAM_DEPTH + 1 nested Forms (depth 1 ..= 51).
        let chain_len = MAX_STREAM_DEPTH + 1;
        let mut objects = vec![
            b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>".to_vec(),
            b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R \
               /Resources << /XObject << /F0 5 0 R >> >> >>"
                .to_vec(),
            stream_obj("", b"/F0 Do"),
        ];
        for i in 0..chain_len {
            let next_obj = 5 + i + 1;
            if i + 1 < chain_len {
                objects.push(stream_obj(
                    &format!(
                        "/Type /XObject /Subtype /Form /BBox [0 0 200 100] \
                         /Resources << /XObject << /Next {next_obj} 0 R >> >>"
                    ),
                    b"/Next Do",
                ));
            } else {
                objects.push(stream_obj(
                    "/Type /XObject /Subtype /Form /BBox [0 0 200 100]",
                    b"0 0 5 5 re f",
                ));
            }
        }
        let (version, pages) = open_version(build_pdf(&objects));
        let err = page_provenance(
            &version,
            &pages,
            0,
            &Fallback::default(),
            false,
            &Budget::default(),
        )
        .unwrap_err();
        assert!(
            matches!(
                err,
                BridgeError::Store(ref e) if e.limit().map(|l| l.code()) == Some(LimitCode::ObjectDepth)
            ),
            "expected ObjectDepth refusal beyond MAX_STREAM_DEPTH, got {err:?}"
        );
    }

    #[test]
    fn budgets_and_cancellation_are_enforced_and_version_is_never_mutated() {
        let pdf = build_pdf(&[
            b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>".to_vec(),
            b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R \
               /Resources << /Font << /F1 5 0 R >> >> >>"
                .to_vec(),
            stream_obj(
                "",
                b"BT /F1 12 Tf 10 50 Td (BudgetCheck) Tj ET 0 0 10 10 re f",
            ),
            font_dict(),
        ]);

        let (version, pages) = open_version(pdf);
        let before_next = version.next_number();
        let before_changes = version.has_changes();

        // Tight InterpretationSize limit refuses cleanly.
        let tight_interp = Budget::default().with_limit(LimitCode::InterpretationSize, Some(32));
        let err = page_provenance(
            &version,
            &pages,
            0,
            &Fallback::default(),
            false,
            &tight_interp,
        )
        .unwrap_err();
        assert!(
            matches!(
                err,
                BridgeError::Store(ref e) if e.limit().map(|l| l.code()) == Some(LimitCode::InterpretationSize)
            ),
            "expected InterpretationSize refusal, got {err:?}"
        );

        // Tight MemorySize limit refuses cleanly.
        let tight_mem = Budget::default().with_limit(LimitCode::MemorySize, Some(16));
        let err = page_provenance(&version, &pages, 0, &Fallback::default(), false, &tight_mem)
            .unwrap_err();
        assert!(
            matches!(
                err,
                BridgeError::Store(ref e) if e.limit().map(|l| l.code()) == Some(LimitCode::MemorySize)
            ),
            "expected MemorySize refusal, got {err:?}"
        );

        // Cancelled token refuses cleanly.
        let token = CancellationToken::new();
        token.cancel();
        let cancelled = Budget::default().with_cancellation(token);
        let err = page_provenance(&version, &pages, 0, &Fallback::default(), false, &cancelled)
            .unwrap_err();
        assert!(
            matches!(
                err,
                BridgeError::Store(ref e) if e.limit().map(|l| l.code()) == Some(LimitCode::Cancelled)
            ),
            "expected Cancelled refusal, got {err:?}"
        );

        // Normal run with primary bridge reuse succeeds and leaves `version` untouched.
        let budget = Budget::default();
        let primary = Bridged::new(&version, &pages, &budget).unwrap();
        let prov = page_provenance_with_primary(
            &primary,
            &version,
            &pages,
            0,
            &Fallback::default(),
            false,
            &budget,
        )
        .unwrap();
        assert!(prov.verified);
        assert_eq!(prov.glyph_runs.len(), 1);
        assert_eq!(prov.paths.len(), 1);

        // `Version` was never mutated by any provenance pass.
        assert_eq!(version.next_number(), before_next);
        assert_eq!(version.has_changes(), before_changes);
        assert!(!version.has_changes());

        // Default non-provenance extraction works unchanged on the primary bridge.
        let content = page_content(
            &primary,
            &Fallback::default(),
            0,
            ItemKinds {
                text_runs: true,
                paths: true,
                images: true,
            },
            false,
            None,
            &budget,
        )
        .unwrap();
        assert_eq!(content.items.len(), 2);
    }

    #[test]
    fn text_quote_operators_fill_stroke_modes_all_path_variants_direct_annot_and_no_page() {
        // Exercises `'`, `"`, `2 Tr` (Fill+Stroke text), `s`, `F`, `f*`, `B*`, `b`, `b*`,
        // a direct annotation dict inside `/Annots`, a NoView (`F = 32`) annotation,
        // and out-of-range page error.
        let content = b"BT /F1 10 Tf 12 TL 10 80 Td (Q1) ' 1 2 (Q2) \" 2 Tr (FS) Tj ET \
                        0 0 10 10 re s \
                        15 0 10 10 re F \
                        30 0 10 10 re f* \
                        45 0 10 10 re B* \
                        60 0 10 10 re b \
                        75 0 10 10 re b*";
        let pdf = build_pdf(&[
            b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 100] >>".to_vec(),
            b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R \
               /Resources << /Font << /F1 5 0 R >> >> \
               /Annots [ << /Type /Annot /Subtype /Stamp /Rect [0 0 20 20] /AP << /N 6 0 R >> >> 7 0 R ] >>"
                .to_vec(),
            stream_obj("", content),
            font_dict(),
            stream_obj(
                "/Type /XObject /Subtype /Form /BBox [0 0 20 20]",
                b"1 1 5 5 re f",
            ),
            b"<< /Type /Annot /Subtype /Stamp /F 32 /Rect [30 0 50 20] /AP << /N 6 0 R >> >>"
                .to_vec(),
        ]);

        let (version, pages) = open_version(pdf);
        let budget = Budget::default();
        let prov =
            page_provenance(&version, &pages, 0, &Fallback::default(), true, &budget).unwrap();
        assert!(prov.verified);

        // Text: `'` (op 4), `"` (op 5), `2 Tr` `Tj` (op 7, Fill + Stroke -> 2 callbacks).
        assert_eq!(prov.glyph_runs.len(), 4);
        assert_eq!(prov.glyph_runs[0].as_ref().unwrap().operation, 4);
        assert_eq!(prov.glyph_runs[1].as_ref().unwrap().operation, 5);
        assert_eq!(prov.glyph_runs[2].as_ref().unwrap().operation, 7);
        assert_eq!(prov.glyph_runs[3].as_ref().unwrap().operation, 7);

        // Paths: s (1), F (1), f* (1), B* (2), b (2), b* (2) = 9 page callbacks + 1 direct annot callback.
        assert_eq!(prov.paths.len(), 10);
        let direct_annot_path = prov.paths[9].as_ref().unwrap();
        assert_eq!(
            direct_annot_path.annotation,
            Some(AnnotationOrigin {
                annotation: 0,
                appearance_stream: 6,
            })
        );
        assert_eq!(
            (direct_annot_path.stream, direct_annot_path.operation),
            (6, 1)
        );

        // Out-of-range page index returns BridgeError::NoPage(1).
        let err =
            page_provenance(&version, &pages, 1, &Fallback::default(), false, &budget).unwrap_err();
        assert_eq!(err, BridgeError::NoPage(1));
    }
}
