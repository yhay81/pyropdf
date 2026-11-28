//! Converting a page to SVG through hayro-svg, as pylopdf's `page.svg` does
//! (yhay81/pyropdf#120), with an optional invisible selectable Unicode text
//! layer (yhay81/pyropdf#1271, temporary DESIGN.md section 6 workaround for
//! #435 / LaurenzV/hayro#1739).

use std::fmt;

use hayro::hayro_interpret::font::{Glyph, GlyphRun, PositionedGlyph};
use hayro::hayro_interpret::hayro_cmap::{BfString, WritingMode};
use hayro::hayro_interpret::{
    BlendMode, ClipPath, Context, Device, DrawMode, DrawProps, Image, ImageDrawProps,
    InterpreterCache, InterpreterSettings, RenderContent, SoftMask, TransformExt, interpret_page,
};
use hayro::hayro_syntax::object::dict::keys::ACTUAL_TEXT;
use hayro::hayro_syntax::object::{Dict, String as PdfString};
use hayro::hayro_syntax::page::Page;
use hayro::kurbo::{Affine, BezPath, Point, Rect};
use hayro_font::Font;
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};

use super::{Fallback, RasterError};
use crate::bridge::Bridged;
use crate::display::Display;
use crate::extract::stretch::{Stretches, without_stretch};

/// The opening `<g>` element of the invisible selectable text overlay.
///
/// `fill="none" fill-opacity="0" stroke="none"` keeps every glyph invisible on
/// top of hayro-svg's paths; `pointer-events="all"` keeps character cells
/// hit-testable for pointer selection under SVG's `visiblePainted` default;
/// `xml:space="preserve"` preserves PDF whitespace verbatim.
const LAYER_OPEN: &str = r#"<g id="text-layer" fill="none" fill-opacity="0" stroke="none" pointer-events="all" xml:space="preserve">"#;
const LAYER_CLOSE: &str = "</g>\n";

/// The tolerance, in glyph units (1000 units per em), below which a glyph is
/// taken to sit on the baseline of its chunk.
const BASELINE_TOLERANCE: f64 = 0.5;

/// The horizontal advance assumed in glyph units when a multi-character glyph
/// has no width and no following glyph in the run.
const DEFAULT_ADVANCE: f64 = 500.0;

/// The vertical advance assumed in glyph units when a multi-character vertical
/// glyph has no following glyph in the run.
const VERTICAL_DEFAULT_ADVANCE: f64 = 1000.0;

/// Converts page `index` to an SVG document at 72 dpi, with text drawn as
/// paths and, unless `annotations` is false, annotation appearances;
/// `fallback` draws the CJK fonts that the document does not embed. Its
/// width and height are the page's display size in points.
///
/// hayro-svg builds the whole document before returning it, so a caller's
/// size limit applies to the result, not to the conversion's memory.
pub fn svg(
    bridged: &Bridged,
    fallback: &Fallback,
    index: usize,
    annotations: bool,
    budget: &Budget,
) -> Result<String, RasterError> {
    svg_with_text_layer(bridged, fallback, index, annotations, false, budget)
}

/// Converts page `index` to an SVG document at 72 dpi, optionally appending an
/// invisible selectable Unicode text layer above the painted paths.
///
/// When `text_layer` is `false`, no extra interpretation pass runs and the
/// output is byte-identical to [`svg`]. When `text_layer` is `true`, a
/// lightweight `hayro_interpret::Device` collects positioned Unicode text in
/// hayro-svg's native view coordinates (matching `page.initial_transform(true)`
/// so `UserUnit != 1.0` viewports remain aligned), respecting `/ActualText`,
/// horizontal and vertical writing modes, ligatures, transforms, and invisible
/// OCR text (`3 Tr` / `7 Tr`), and inserts it immediately before `</svg>`.
pub fn svg_with_text_layer(
    bridged: &Bridged,
    fallback: &Fallback,
    index: usize,
    annotations: bool,
    text_layer: bool,
    budget: &Budget,
) -> Result<String, RasterError> {
    let (page, display) = bridged.page(index).ok_or(RasterError::NoPage(index))?;
    let settings = InterpreterSettings {
        render_content: if annotations {
            RenderContent::all()
        } else {
            RenderContent::PAGE
        },
        ..fallback.settings()
    };
    let converted = hayro_svg::convert(
        page,
        &hayro_svg::RenderCache::new(),
        &settings,
        &hayro_svg::SvgRenderSettings::default(),
    );
    let mut svg = if display.unit == 1.0 {
        budget.check(LimitCode::SvgOutputSize, converted.len() as u64)?;
        converted
    } else {
        // hayro-svg paints in its native view. Locate the true top-left of the
        // crop there, including any offset from hayro's one-unit minimum.
        let native = Display::native(page);
        let origin = native.user_to_view * (display.user_to_view.inverse() * Point::ORIGIN);
        let (vw, vh) = display.view_size;
        let (width, height) = display.size();
        let attributes = format!(
            r#"viewBox="{} {} {vw} {vh}" width="{width}" height="{height}""#,
            origin.x, origin.y,
        );
        set_viewport(converted, &attributes, budget)?
    };
    if !text_layer {
        return Ok(svg);
    }
    let layer = build_text_layer(page, &settings, svg.len(), budget)?;
    if layer.is_empty() {
        return Ok(svg);
    }
    let layer_str = std::str::from_utf8(&layer).expect("text layer writes valid UTF-8");
    insert_text_layer(&mut svg, layer_str, budget)?;
    Ok(svg)
}

/// Replaces hayro-svg's three leading viewport attributes in the original
/// buffer. Check the final output length before reserving any extra bytes.
fn set_viewport(mut svg: String, attributes: &str, budget: &Budget) -> Result<String, RasterError> {
    let header = svg
        .split_once('>')
        .map_or(svg.as_str(), |(header, _)| header);
    let end = header
        .find(" xmlns=")
        .filter(|_| header.starts_with("<svg viewBox="))
        .ok_or_else(|| RasterError::Geometry("missing SVG viewport".to_owned()))?;
    let range = "<svg ".len()..end;
    let len = (svg.len() - range.len())
        .checked_add(attributes.len())
        .ok_or_else(|| LimitExceeded::new(LimitCode::MemorySize))?;
    budget.check(LimitCode::SvgOutputSize, len as u64)?;
    if len > svg.capacity() {
        budget.charge(LimitCode::MemorySize, (len - svg.capacity()) as u64)?;
        #[expect(
            clippy::disallowed_methods,
            reason = "only the charged increase over the converter's existing buffer is reserved"
        )]
        svg.try_reserve_exact(len - svg.len())
            .map_err(|_| LimitExceeded::new(LimitCode::MemorySize))?;
    }
    svg.replace_range(range, attributes);
    Ok(svg)
}

/// Inserts `layer` immediately before the closing `</svg>` tag. Check the
/// final output length and memory budget before reserving any extra bytes.
fn insert_text_layer(svg: &mut String, layer: &str, budget: &Budget) -> Result<(), RasterError> {
    let close = svg
        .rfind("</svg>")
        .ok_or_else(|| RasterError::Geometry("missing SVG closing tag".to_owned()))?;
    let len = svg
        .len()
        .checked_add(layer.len())
        .ok_or_else(|| LimitExceeded::new(LimitCode::MemorySize))?;
    budget.check(LimitCode::SvgOutputSize, len as u64)?;
    if len > svg.capacity() {
        budget.charge(LimitCode::MemorySize, (len - svg.capacity()) as u64)?;
        #[expect(
            clippy::disallowed_methods,
            reason = "only the charged increase over the converter's existing buffer is reserved"
        )]
        svg.try_reserve_exact(len - svg.len())
            .map_err(|_| LimitExceeded::new(LimitCode::MemorySize))?;
    }
    svg.replace_range(close..close, layer);
    Ok(())
}

/// Interprets `page` in hayro-svg's native view space and serializes its
/// invisible selectable `<g id="text-layer">...</g>` overlay into a budgeted
/// UTF-8 buffer.
fn build_text_layer<'a, 'b>(
    page: &'a Page<'a>,
    settings: &InterpreterSettings,
    base_len: usize,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, u8>, LimitExceeded> {
    let cache = InterpreterCache::new();
    let (width, height) = page.render_dimensions();
    let mut context = Context::new(
        page.initial_transform(true).to_kurbo(),
        Rect::new(0.0, 0.0, f64::from(width), f64::from(height)),
        &cache,
        page.xref(),
        settings.clone(),
    );
    let mut collector = TextLayerCollector::new(settings, base_len, budget);
    interpret_page(page, &mut context, &mut super::DynDevice(&mut collector));
    collector.finish()
}

/// Placement accumulated across all glyphs drawn inside an `/ActualText`
/// marked-content sequence.
struct ActualPlacement {
    transform: Affine,
    inverse: Affine,
    writing_mode: WritingMode,
    start: Point,
    end: Point,
}

/// An open marked-content sequence carrying `/ActualText` (ISO 32000-2 §14.9.4).
struct ActualText<'b> {
    depth: usize,
    text: BudgetVec<'b, u8>,
    placement: Option<ActualPlacement>,
}

/// Lightweight `hayro_interpret::Device` that records only top-level page and
/// annotation glyph runs (never masks, tiling patterns, or Type 3 inner
/// procedures) and serializes them directly to budgeted XML bytes.
struct TextLayerCollector<'a, 'b> {
    budget: &'b Budget,
    stretches: Stretches<'a>,
    base_len: usize,
    output: BudgetVec<'b, u8>,
    chunk_text: BudgetVec<'b, u8>,
    chunk_x: BudgetVec<'b, f32>,
    chunk_y: BudgetVec<'b, f32>,
    pending_fill: BudgetVec<'b, [f64; 6]>,
    marks: usize,
    actual_text: Option<ActualText<'b>>,
    error: Option<LimitExceeded>,
}

impl<'a, 'b> TextLayerCollector<'a, 'b> {
    fn new(settings: &InterpreterSettings, base_len: usize, budget: &'b Budget) -> Self {
        Self {
            budget,
            stretches: Stretches::new(settings),
            base_len,
            output: BudgetVec::new(budget),
            chunk_text: BudgetVec::new(budget),
            chunk_x: BudgetVec::new(budget),
            chunk_y: BudgetVec::new(budget),
            pending_fill: BudgetVec::new(budget),
            marks: 0,
            actual_text: None,
            error: None,
        }
    }

    fn record_glyph_run(
        &mut self,
        glyph_run: &GlyphRun<'_, 'a>,
        transform: Affine,
        draw_mode: &DrawMode,
    ) -> Result<(), LimitExceeded> {
        self.budget.poll()?;
        let glyphs = glyph_run.glyphs();
        if glyphs.is_empty() {
            self.pending_fill.clear();
            return Ok(());
        }

        // `hayro-interpret` emits `FillStroke` (`2 Tr`) and `FillAndStrokeAndClip`
        // (`6 Tr`) as a `Fill` call immediately followed by a `Stroke` call with
        // identical glyph transforms. Skip the completing stroke so text is not
        // duplicated in the overlay.
        let is_fill = matches!(draw_mode, DrawMode::Fill(_));
        let completes_fill = matches!(draw_mode, DrawMode::Stroke(_))
            && !self.pending_fill.is_empty()
            && self.pending_fill.len() == glyphs.len()
            && glyphs
                .iter()
                .zip(self.pending_fill.iter())
                .all(|(glyph, kept)| (transform * glyph.transform()).as_coeffs() == *kept);
        if completes_fill {
            self.pending_fill.clear();
            return Ok(());
        }
        self.pending_fill.clear();
        if is_fill {
            self.pending_fill.try_reserve(glyphs.len())?;
            for glyph in glyphs {
                self.pending_fill
                    .try_push((transform * glyph.transform()).as_coeffs())?;
            }
        }

        let stretching = self.stretches.font_of(glyph_run);
        let writing_mode = glyph_run.writing_mode();

        if self.actual_text.is_some() {
            self.record_actual_text_run(glyphs, transform, stretching.as_ref(), writing_mode);
            return Ok(());
        }

        let mut chunk_transform: Option<(Affine, Affine)> = None;
        self.chunk_text.clear();
        self.chunk_x.clear();
        self.chunk_y.clear();

        for (index, glyph) in glyphs.iter().enumerate() {
            if index % 256 == 0 {
                self.budget.poll()?;
            }
            let Some(unicode) = glyph.as_unicode() else {
                continue;
            };
            let has_xml_char = match &unicode {
                BfString::Char(c) => is_xml_char(*c),
                BfString::String(s) => s.chars().any(is_xml_char),
            };
            if !has_xml_char {
                continue;
            }
            let unstretched =
                without_stretch(transform * glyph.transform(), stretching.as_ref(), glyph);
            // SVG text is drawn y-down; PDF glyph space is y-up.
            let base = unstretched * Affine::scale_non_uniform(1.0, -1.0);
            let Some(base_inv) = finite_inverse(base) else {
                continue;
            };

            let same_basis = chunk_transform.is_some_and(|(_, inv)| {
                let relative = (inv * base).as_coeffs();
                (relative[0] - 1.0).abs() < 1e-3
                    && relative[1].abs() < 1e-3
                    && relative[2].abs() < 1e-3
                    && (relative[3] - 1.0).abs() < 1e-3
            });
            if !same_basis {
                if let Some((prev_transform, _)) = chunk_transform.take() {
                    self.flush_chunk(prev_transform)?;
                }
                chunk_transform = Some((base, base_inv));
            }
            let (_, chunk_inv) = chunk_transform.expect("chunk transform just initialized");
            let origin = chunk_inv * (base * Point::ORIGIN);
            if !origin.x.is_finite() || !origin.y.is_finite() {
                continue;
            }

            match unicode {
                BfString::Char(c) => {
                    self.push_char(c, origin)?;
                }
                BfString::String(s) => {
                    let count = s.chars().filter(|&c| is_xml_char(c)).count();
                    let next = next_glyph_origin(
                        glyphs,
                        index,
                        glyph,
                        origin,
                        chunk_inv,
                        transform,
                        stretching.as_ref(),
                        writing_mode,
                    );
                    for (at, c) in s.chars().filter(|&c| is_xml_char(c)).enumerate() {
                        let t = at as f64 / count as f64;
                        let point = origin.lerp(next, t);
                        if point.x.is_finite() && point.y.is_finite() {
                            self.push_char(c, point)?;
                        }
                    }
                }
            }
        }

        if let Some((prev_transform, _)) = chunk_transform {
            self.flush_chunk(prev_transform)?;
        }
        Ok(())
    }

    /// Accumulates glyph placement for an open `/ActualText` sequence without
    /// emitting individual glyph Unicode values.
    fn record_actual_text_run(
        &mut self,
        glyphs: &[PositionedGlyph<'a>],
        transform: Affine,
        stretching: Option<&Font<'a>>,
        writing_mode: WritingMode,
    ) {
        let Some(actual) = self.actual_text.as_mut() else {
            return;
        };
        for glyph in glyphs {
            let unstretched = without_stretch(transform * glyph.transform(), stretching, glyph);
            let base = unstretched * Affine::scale_non_uniform(1.0, -1.0);
            let Some(base_inv) = finite_inverse(base) else {
                continue;
            };
            let advance = glyph_advance_vec(glyph, writing_mode);
            match &mut actual.placement {
                None => {
                    actual.placement = Some(ActualPlacement {
                        transform: base,
                        inverse: base_inv,
                        writing_mode,
                        start: Point::ORIGIN,
                        end: Point::new(advance.0, advance.1),
                    });
                }
                Some(placement) => {
                    let origin = placement.inverse * (base * Point::ORIGIN);
                    if !origin.x.is_finite() || !origin.y.is_finite() {
                        continue;
                    }
                    match placement.writing_mode {
                        WritingMode::Horizontal => {
                            let end_x = origin.x + advance.0;
                            if end_x.is_finite() && end_x > placement.end.x {
                                placement.end.x = end_x;
                            }
                        }
                        WritingMode::Vertical => {
                            let end_y = origin.y + advance.1;
                            if end_y.is_finite() && end_y > placement.end.y {
                                placement.end.y = end_y;
                            }
                        }
                    }
                }
            }
        }
    }

    fn emit_actual_text(&mut self, actual: ActualText<'b>) -> Result<(), LimitExceeded> {
        let Some(placement) = actual.placement else {
            return Ok(());
        };
        let text = std::str::from_utf8(&actual.text).expect("append_text writes valid UTF-8");
        let count = text.chars().filter(|&c| is_xml_char(c)).count();
        if count == 0 {
            return Ok(());
        }
        self.chunk_text.clear();
        self.chunk_x.clear();
        self.chunk_y.clear();
        for (at, c) in text.chars().filter(|&c| is_xml_char(c)).enumerate() {
            if at % 256 == 0 {
                self.budget.poll()?;
            }
            let t = at as f64 / count as f64;
            let point = placement.start.lerp(placement.end, t);
            if point.x.is_finite() && point.y.is_finite() {
                self.push_char(c, point)?;
            }
        }
        self.flush_chunk(placement.transform)
    }

    fn push_char(&mut self, c: char, point: Point) -> Result<(), LimitExceeded> {
        let ch = if c.is_whitespace() { ' ' } else { c };
        let mut encoded = [0u8; 4];
        self.chunk_text
            .try_extend_from_slice(ch.encode_utf8(&mut encoded).as_bytes())?;
        self.chunk_x.try_push(round(point.x))?;
        self.chunk_y.try_push(round(point.y))?;
        Ok(())
    }

    fn flush_chunk(&mut self, transform: Affine) -> Result<(), LimitExceeded> {
        if self.chunk_text.is_empty() {
            self.chunk_x.clear();
            self.chunk_y.clear();
            return Ok(());
        }
        if self.output.is_empty() {
            self.output.try_extend_from_slice(LAYER_OPEN.as_bytes())?;
        }
        let mut writer = BudgetWriter::new(&mut self.output);
        let coeffs = transform.as_coeffs();
        writer.write_fmt(format_args!(
            r#"<text transform="matrix({} {} {} {} {} {})" font-size="1000" x=""#,
            clean_f32(coeffs[0] as f32),
            clean_f32(coeffs[1] as f32),
            clean_f32(coeffs[2] as f32),
            clean_f32(coeffs[3] as f32),
            clean_f32(coeffs[4] as f32),
            clean_f32(coeffs[5] as f32),
        ))?;
        write_f32_list(&mut writer, &self.chunk_x)?;
        writer.write_str("\"")?;
        if self
            .chunk_y
            .iter()
            .any(|y| f64::from(y.abs()) > BASELINE_TOLERANCE)
        {
            writer.write_str(" y=\"")?;
            write_f32_list(&mut writer, &self.chunk_y)?;
            writer.write_str("\"")?;
        }
        writer.write_str(">")?;
        let text = std::str::from_utf8(&self.chunk_text).expect("chunk text is valid UTF-8");
        write_xml_escaped(&mut writer, text)?;
        writer.write_str("</text>")?;
        writer.finish()?;

        self.chunk_text.clear();
        self.chunk_x.clear();
        self.chunk_y.clear();

        let projected = self
            .base_len
            .saturating_add(self.output.len())
            .saturating_add(LAYER_CLOSE.len());
        self.budget
            .check(LimitCode::SvgOutputSize, projected as u64)?;
        Ok(())
    }

    fn finish(mut self) -> Result<BudgetVec<'b, u8>, LimitExceeded> {
        if let Some(err) = self.error {
            return Err(err);
        }
        if let Some(actual) = self.actual_text.take() {
            self.emit_actual_text(actual)?;
        }
        if !self.output.is_empty() {
            self.output.try_extend_from_slice(LAYER_CLOSE.as_bytes())?;
            let projected = self.base_len.saturating_add(self.output.len());
            self.budget
                .check(LimitCode::SvgOutputSize, projected as u64)?;
        }
        Ok(self.output)
    }
}

impl<'a> Device<'a> for TextLayerCollector<'a, '_> {
    fn draw_path(&mut self, _: &BezPath, _: DrawProps<'a>, _: &DrawMode) {
        self.pending_fill.clear();
    }

    fn draw_rect(&mut self, _: &Rect, _: DrawProps<'a>, _: &DrawMode) {
        self.pending_fill.clear();
    }

    fn push_clip_path(&mut self, _: &ClipPath) {
        self.pending_fill.clear();
    }

    fn push_clip_rect(&mut self, _: &Rect) {
        self.pending_fill.clear();
    }

    fn push_transparency_group(&mut self, _: f32, _: Option<SoftMask<'a>>, _: BlendMode) {
        self.pending_fill.clear();
    }

    fn draw_glyph_run(
        &mut self,
        glyph_run: &GlyphRun<'_, 'a>,
        props: DrawProps<'a>,
        draw_mode: &DrawMode,
    ) {
        if self.error.is_some() {
            return;
        }
        if let Err(refusal) = self.record_glyph_run(glyph_run, props.transform, draw_mode) {
            self.error = Some(refusal);
        }
    }

    fn draw_image(&mut self, _: Image<'a, '_>, _: ImageDrawProps<'a>) {
        self.pending_fill.clear();
    }

    fn pop_clip(&mut self) {
        self.pending_fill.clear();
    }

    fn pop_transparency_group(&mut self) {
        self.pending_fill.clear();
    }

    fn begin_marked_content(&mut self, _tag: &[u8], properties: Option<&Dict<'_>>) {
        self.pending_fill.clear();
        if self.error.is_some() {
            return;
        }
        self.marks = self.marks.saturating_add(1);
        if self.actual_text.is_some() {
            return;
        }
        let Some(actual) = properties.and_then(|props| props.get::<PdfString>(ACTUAL_TEXT)) else {
            return;
        };
        let mut text = BudgetVec::new(self.budget);
        match crate::forms::append_text(&mut text, actual.as_bytes()) {
            Ok(()) => {
                self.actual_text = Some(ActualText {
                    depth: self.marks,
                    text,
                    placement: None,
                });
            }
            Err(refusal) => self.error = Some(refusal),
        }
    }

    fn end_marked_content(&mut self) {
        self.pending_fill.clear();
        if self
            .actual_text
            .as_ref()
            .is_some_and(|actual| actual.depth == self.marks)
        {
            let actual = self.actual_text.take().expect("actual_text just matched");
            if self.error.is_none()
                && let Err(refusal) = self.emit_actual_text(actual)
            {
                self.error = Some(refusal);
            }
        }
        self.marks = self.marks.saturating_sub(1);
    }
}

/// Computes the target end position for spreading the characters of a
/// multi-character glyph (such as a ligature) inside its chunk.
#[expect(
    clippy::too_many_arguments,
    reason = "glyph run context needed to resolve the following glyph's origin in chunk space"
)]
fn next_glyph_origin<'a>(
    glyphs: &[PositionedGlyph<'a>],
    index: usize,
    glyph: &Glyph<'a>,
    origin: Point,
    chunk_inv: Affine,
    transform: Affine,
    stretching: Option<&Font<'a>>,
    writing_mode: WritingMode,
) -> Point {
    let fallback = glyph_advance_vec(glyph, writing_mode);
    let default_end = Point::new(origin.x + fallback.0, origin.y + fallback.1);
    let Some(next_glyph) = glyphs.get(index + 1) else {
        return default_end;
    };
    let next_base = without_stretch(transform * next_glyph.transform(), stretching, next_glyph)
        * Affine::scale_non_uniform(1.0, -1.0);
    let next_origin = chunk_inv * (next_base * Point::ORIGIN);
    if !next_origin.x.is_finite() || !next_origin.y.is_finite() {
        return default_end;
    }
    if writing_mode == WritingMode::Horizontal
        && let Some(advance) = glyph_advance_width(glyph)
    {
        let dx = next_origin.x - origin.x;
        let dy = (next_origin.y - origin.y).abs();
        if dy <= BASELINE_TOLERANCE && dx > 0.0 && dx <= advance * 1.5 {
            return next_origin;
        }
        return Point::new(origin.x + advance, origin.y);
    }
    next_origin
}

/// Returns the glyph's positive finite advance width in 1000-unit glyph space.
fn glyph_advance_width(glyph: &Glyph<'_>) -> Option<f64> {
    let width = match glyph {
        Glyph::Outline(outline) => outline.advance_width(),
        Glyph::Type3(type3) => type3.advance_width(),
    }?;
    (width.is_finite() && width > 0.0).then_some(f64::from(width))
}

/// Returns a fallback glyph advance vector in SVG-flipped glyph space.
fn glyph_advance_vec(glyph: &Glyph<'_>, writing_mode: WritingMode) -> (f64, f64) {
    match writing_mode {
        WritingMode::Horizontal => (glyph_advance_width(glyph).unwrap_or(DEFAULT_ADVANCE), 0.0),
        WritingMode::Vertical => (0.0, VERTICAL_DEFAULT_ADVANCE),
    }
}

/// Returns `Some(transform.inverse())` when both `transform` (at `f32`
/// serialization precision) and its inverse are finite.
fn finite_inverse(transform: Affine) -> Option<Affine> {
    let coeffs = transform.as_coeffs();
    if !coeffs
        .iter()
        .all(|v| v.is_finite() && (*v as f32).is_finite())
    {
        return None;
    }
    let inverse = transform.inverse();
    if !inverse.as_coeffs().iter().all(|v| v.is_finite()) {
        return None;
    }
    Some(inverse)
}

fn clean_f32(value: f32) -> f32 {
    if value == 0.0 { 0.0 } else { value }
}

fn round(value: f64) -> f32 {
    clean_f32(((value * 10.0).round() / 10.0) as f32)
}

/// Whether XML 1.0 (Fifth Edition §2.2) allows `c` in character data.
fn is_xml_char(c: char) -> bool {
    matches!(
        c,
        '\u{9}' | '\u{A}' | '\u{D}' | '\u{20}'..='\u{D7FF}' | '\u{E000}'..='\u{FFFD}' | '\u{10000}'..='\u{10FFFF}'
    )
}

/// Adapter writing formatted UTF-8 text into a [`BudgetVec`] without temporary
/// `String` allocations.
struct BudgetWriter<'a, 'b> {
    buf: &'a mut BudgetVec<'b, u8>,
    err: Option<LimitExceeded>,
}

impl<'a, 'b> BudgetWriter<'a, 'b> {
    fn new(buf: &'a mut BudgetVec<'b, u8>) -> Self {
        Self { buf, err: None }
    }

    fn write_str(&mut self, s: &str) -> Result<(), LimitExceeded> {
        if let Some(err) = self.err.take() {
            return Err(err);
        }
        self.buf.try_extend_from_slice(s.as_bytes())
    }

    fn write_fmt(&mut self, args: fmt::Arguments<'_>) -> Result<(), LimitExceeded> {
        if let Some(err) = self.err.take() {
            return Err(err);
        }
        if fmt::Write::write_fmt(self, args).is_err() {
            return Err(self
                .err
                .take()
                .unwrap_or_else(|| LimitExceeded::new(LimitCode::MemorySize)));
        }
        Ok(())
    }

    fn finish(self) -> Result<(), LimitExceeded> {
        match self.err {
            Some(err) => Err(err),
            None => Ok(()),
        }
    }
}

impl fmt::Write for BudgetWriter<'_, '_> {
    fn write_str(&mut self, s: &str) -> fmt::Result {
        if self.err.is_some() {
            return Err(fmt::Error);
        }
        if let Err(err) = self.buf.try_extend_from_slice(s.as_bytes()) {
            self.err = Some(err);
            return Err(fmt::Error);
        }
        Ok(())
    }
}

fn write_f32_list(writer: &mut BudgetWriter<'_, '_>, values: &[f32]) -> Result<(), LimitExceeded> {
    for (at, value) in values.iter().enumerate() {
        if at > 0 {
            writer.write_str(" ")?;
        }
        writer.write_fmt(format_args!("{value}"))?;
    }
    Ok(())
}

fn write_xml_escaped(writer: &mut BudgetWriter<'_, '_>, text: &str) -> Result<(), LimitExceeded> {
    let mut start = 0;
    for (index, byte) in text.bytes().enumerate() {
        let escaped = match byte {
            b'&' => "&amp;",
            b'<' => "&lt;",
            b'>' => "&gt;",
            _ => continue,
        };
        if index > start {
            writer.write_str(&text[start..index])?;
        }
        writer.write_str(escaped)?;
        start = index + 1;
    }
    if start < text.len() {
        writer.write_str(&text[start..])?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::Path;

    use pyropdf_store::index::Index;
    use pyropdf_store::memo::Reads;
    use pyropdf_store::pages::PageIndex;
    use pyropdf_store::save::FileBase;
    use pyropdf_store::value::Bytes;
    use pyropdf_store::version::Document;

    use super::*;

    fn bridge_bytes(bytes: Vec<u8>, budget: &Budget) -> (Bridged, usize) {
        let index = Index::open(Bytes::new(bytes), budget).unwrap();
        let base = FileBase::new(index, budget).unwrap();
        let document = Document::new(std::sync::Arc::new(base), None);
        let version = document.current();
        let pages = PageIndex::build(&mut Reads::new(&version, budget)).unwrap();
        let count = pages.len();
        let bridged = Bridged::new(&version, &pages, budget).unwrap();
        (bridged, count)
    }

    #[test]
    fn viewport_edits_reuse_capacity_and_check_limits_before_growing() {
        let original = r#"<svg viewBox="0 0 1 1" width="1" height="1" xmlns="svg"><path/></svg>"#;
        let viewport = r#"viewBox="0 0 1 1" width="30000" height="30000""#;
        let expected =
            original.replace(r#"width="1" height="1""#, r#"width="30000" height="30000""#);
        let no_growth = Budget::default().with_limit(LimitCode::MemorySize, Some(0));
        let mut svg = String::with_capacity(expected.len());
        svg.push_str(original);
        let buffer = svg.as_ptr();
        let result = set_viewport(svg, viewport, &no_growth).unwrap();
        assert_eq!(result, expected);
        assert_eq!(result.as_ptr(), buffer);

        let exact = || original.to_owned().into_boxed_str().into_string();
        let error = set_viewport(exact(), viewport, &no_growth).unwrap_err();
        assert!(
            matches!(error, RasterError::Limit(limit) if limit.code() == LimitCode::MemorySize)
        );
        let capped =
            no_growth.with_limit(LimitCode::SvgOutputSize, Some(expected.len() as u64 - 1));
        let error = set_viewport(exact(), viewport, &capped).unwrap_err();
        assert!(
            matches!(error, RasterError::Limit(limit) if limit.code() == LimitCode::SvgOutputSize)
        );
    }

    #[test]
    fn insert_text_layer_reuses_capacity_and_checks_limits_before_growing() {
        let original = r#"<svg viewBox="0 0 1 1" width="1" height="1" xmlns="svg"><path/></svg>"#;
        let layer = r#"<g id="text-layer"><text x="0">Hi</text></g>"#;
        let expected = r#"<svg viewBox="0 0 1 1" width="1" height="1" xmlns="svg"><path/><g id="text-layer"><text x="0">Hi</text></g></svg>"#;
        let no_growth = Budget::default().with_limit(LimitCode::MemorySize, Some(0));
        let mut svg = String::with_capacity(expected.len());
        svg.push_str(original);
        let buffer = svg.as_ptr();
        insert_text_layer(&mut svg, layer, &no_growth).unwrap();
        assert_eq!(svg, expected);
        assert_eq!(svg.as_ptr(), buffer);

        let mut exact = original.to_owned().into_boxed_str().into_string();
        let error = insert_text_layer(&mut exact, layer, &no_growth).unwrap_err();
        assert!(
            matches!(error, RasterError::Limit(limit) if limit.code() == LimitCode::MemorySize)
        );
        let capped =
            no_growth.with_limit(LimitCode::SvgOutputSize, Some(expected.len() as u64 - 1));
        let mut exact = original.to_owned().into_boxed_str().into_string();
        let error = insert_text_layer(&mut exact, layer, &capped).unwrap_err();
        assert!(
            matches!(error, RasterError::Limit(limit) if limit.code() == LimitCode::SvgOutputSize)
        );
    }

    #[test]
    fn xml_escaping_and_control_char_filtering() {
        assert!(!is_xml_char('\u{0}'));
        assert!(!is_xml_char('\u{8}'));
        assert!(is_xml_char('\u{9}'));
        assert!(is_xml_char('\u{A}'));
        assert!(!is_xml_char('\u{B}'));
        assert!(!is_xml_char('\u{C}'));
        assert!(is_xml_char('\u{D}'));
        assert!(!is_xml_char('\u{E}'));
        assert!(!is_xml_char('\u{1F}'));
        assert!(is_xml_char(' '));
        assert!(is_xml_char('計'));
        assert!(is_xml_char('\u{1F600}'));
        assert!(!is_xml_char('\u{FFFE}'));
        assert!(!is_xml_char('\u{FFFF}'));

        let budget = Budget::default();
        let mut out = BudgetVec::new(&budget);
        let mut writer = BudgetWriter::new(&mut out);
        write_xml_escaped(&mut writer, "Fish & <Chips> 'ok' \"yes\" 計画地").unwrap();
        writer.finish().unwrap();
        assert_eq!(
            std::str::from_utf8(&out).unwrap(),
            "Fish &amp; &lt;Chips&gt; 'ok' \"yes\" 計画地"
        );
    }

    #[test]
    fn text_layer_enforces_memory_and_output_size_limits() {
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/assets/text/nonembedded-cjk.pdf");
        let bytes = fs::read(path).unwrap();
        let open_budget = Budget::default();
        let (bridged, _) = bridge_bytes(bytes, &open_budget);
        let none = Fallback::default();

        let full = svg_with_text_layer(&bridged, &none, 0, true, true, &open_budget).unwrap();
        assert!(full.contains("id=\"text-layer\""));
        assert!(full.contains("計画地"));

        let exact_budget =
            Budget::default().with_limit(LimitCode::SvgOutputSize, Some(full.len() as u64));
        assert_eq!(
            svg_with_text_layer(&bridged, &none, 0, true, true, &exact_budget).unwrap(),
            full
        );

        let short_budget =
            Budget::default().with_limit(LimitCode::SvgOutputSize, Some(full.len() as u64 - 1));
        let err = svg_with_text_layer(&bridged, &none, 0, true, true, &short_budget).unwrap_err();
        assert!(
            matches!(err, RasterError::Limit(limit) if limit.code() == LimitCode::SvgOutputSize)
        );

        let tight_memory = Budget::default().with_limit(LimitCode::MemorySize, Some(64));
        let err = svg_with_text_layer(&bridged, &none, 0, true, true, &tight_memory).unwrap_err();
        assert!(matches!(err, RasterError::Limit(limit) if limit.code() == LimitCode::MemorySize));
    }

    /// The bridge converts every corpus page as hayro-svg converts the
    /// original bytes, which is what pylopdf converts for an unedited
    /// document, and `text_layer=true` emits selectable text (including
    /// invisible OCR text) without altering default path-only output.
    #[test]
    fn corpus_pages_convert_as_the_original_bytes_do() {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/assets/real_world");
        let mut files: Vec<_> = fs::read_dir(&dir)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .filter(|path| path.extension().is_some_and(|e| e == "pdf"))
            .collect();
        files.sort();
        assert_eq!(files.len(), 15);
        std::thread::scope(|scope| {
            for path in &files {
                scope.spawn(move || {
                    let bytes = fs::read(path).unwrap();
                    let budget = Budget::default();
                    let (bridged, page_count) = bridge_bytes(bytes.clone(), &budget);
                    let original = Bridged::of_bytes(bytes);
                    let none = Fallback::default();
                    let is_ocr = path
                        .file_name()
                        .is_some_and(|name| name == "patent-us223898.pdf");
                    for at in 0..page_count {
                        for annotations in [true, false] {
                            let ours = svg(&bridged, &none, at, annotations, &budget).unwrap();
                            let theirs = svg(&original, &none, at, annotations, &budget).unwrap();
                            assert!(ours == theirs, "{} page {at}", path.display());
                            assert!(ours.starts_with("<svg"));
                        }
                        let layered =
                            svg_with_text_layer(&bridged, &none, at, true, true, &budget).unwrap();
                        assert!(layered.starts_with("<svg"));
                        assert!(layered.trim_end().ends_with("</svg>"));
                        if is_ocr {
                            assert!(
                                layered.contains("id=\"text-layer\""),
                                "{} page {at} should keep invisible OCR text",
                                path.display()
                            );
                        }
                    }
                    assert_eq!(
                        svg(&bridged, &none, page_count, true, &budget),
                        Err(RasterError::NoPage(page_count))
                    );
                });
            }
        });
    }
}
