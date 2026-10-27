//! Which operation of a page's own content streams showed each glyph run,
//! until hayro reports where each painted item comes from (I4,
//! yhay81/pyropdf#104).
//!
//! A second interpretation reads the page's content, as [`PageContent`]
//! joins it, with each text-showing operation wrapped in marked content
//! whose tag names the operation's index. Marked content changes nothing
//! that hayro paints, so that interpretation shows the glyph runs of the
//! page's content in the order of the first, those of its Form XObjects
//! included, and its device reads which operation, if any, each run comes
//! from. hayro shows the annotations after the content, so their runs come
//! last in the first interpretation and have no counterpart. Each run is
//! checked against its counterpart by its glyph count and the transform of
//! its first glyph, and a page whose runs disagree has no sources.

use hayro::hayro_interpret::font::GlyphRun;
use hayro::hayro_interpret::{
    BlendMode, ClipPath, Device, DrawMode, DrawProps, Image, ImageDrawProps, InterpreterCache,
    InterpreterSettings, SoftMask, interpret,
};
use hayro::hayro_syntax::Pdf;
use hayro::hayro_syntax::content::TypedIter;
use hayro::hayro_syntax::page::Page;
use hayro::kurbo::BezPath;
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec};
use pyropdf_store::value::Value;

use super::{Error, extraction_context};
use crate::content::PageContent;

/// The prefix of the marked-content tags that name operations.
const TAG: &[u8] = b"pyropdf.operation.";
/// A wrapper around each page-level `Do`; tags inside its Form cannot
/// identify a page operation, even if they use our prefix.
const FORM_TAG: &[u8] = b"pyropdf.operation.form";

/// The operators that show text.
const SHOWS_TEXT: [&[u8]; 4] = [b"Tj", b"TJ", b"'", b"\""];

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

/// Returns the source of each glyph run of the page's content, indexed by
/// the run's place, after checking `checks` against them. A run that a Form
/// XObject shows has no source, and the result is empty when the runs
/// disagree or when the content's own marked content uses the tags. `cache`
/// holds what the first interpretation read.
pub(crate) fn run_sources<'a>(
    pdf: &'a Pdf,
    page: &Page<'a>,
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
    let mut context = extraction_context(pdf, page, cache, settings);
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

    fn begin_marked_content(&mut self, tag: &[u8], _: Option<i32>) {
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
