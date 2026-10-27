//! Reading what hayro paints on a page (DESIGN.md sections 6 and 14), ported
//! from pylopdf's `rust/src/content_model.rs` and the extraction helpers of
//! `rust/src/extract.rs` at 036be111cafe77755db4bb2be17bc70a8f2f9d82.
//!
//! The content model interprets a page of a bridged version with one hayro
//! `Device` and keeps every text run, path, and image in paint order and
//! display space. The operation's budget counts its glyphs, path and clip
//! segments, dash lengths, and items with `content_element_count` (F6).

use hayro::hayro_interpret::{Context, InterpreterCache, InterpreterSettings, TransformExt};
use hayro::hayro_syntax::Pdf;
use hayro::hayro_syntax::object::{Array, Dict, Name, Object, Stream};
use hayro::hayro_syntax::page::Page;
use hayro::kurbo::Rect;
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::Budget;

use crate::bridge::Bridged;

mod model;
mod provenance;
mod reads;
#[cfg(test)]
mod tests;
mod text;

pub use model::{
    BBox, ClipTuple, ContentItem, ContentSink, FontTuple, GlyphStyle, GlyphsTuple, GroupTuple,
    ImageFormatTuple, ImageTuple, ItemContextTuple, ItemKinds, MarkTuple, PageContent, PaintTuple,
    PathTuple, RENDER_FILL, RENDER_FILL_STROKE, RENDER_INVISIBLE, RENDER_STROKE, SoftMaskTuple,
    StateTuple, StrokeTuple, TextRunTuple,
};
pub use reads::read_page;
pub use text::{
    BlockTuple, CharTuple, LineTuple, PageLayout, QuadTuple, SearchHits, SpanStyleTuple, SpanTuple,
    TextOptions, TextPeak, Unmapped, WordTuple, page_layout, page_search, page_text,
};

/// Why reading a page's content stopped.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Error {
    /// The document has no such page.
    NoPage(usize),
    /// The budget refused more glyphs, segments, dash lengths, and items.
    Limit(LimitExceeded),
    /// A collection could not grow.
    Allocation(String),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NoPage(index) => write!(f, "the document has no page {index}"),
            Self::Limit(refusal) => refusal.fmt(f),
            Self::Allocation(message) => f.write_str(message),
        }
    }
}

impl std::error::Error for Error {}

impl From<LimitExceeded> for Error {
    fn from(refusal: LimitExceeded) -> Self {
        Self::Limit(refusal)
    }
}

/// Returns the items of `kinds` that page `index`, counted from 0, paints,
/// with annotation appearances when `annotations` is set. `clip` keeps the
/// items whose box meets a display-space rectangle, after the budget has
/// counted them all.
pub fn page_content(
    bridged: &Bridged,
    index: usize,
    kinds: ItemKinds,
    annotations: bool,
    clip: Option<BBox>,
    budget: &Budget,
) -> Result<PageContent, Error> {
    let (content, model::NoSink) = page_content_into(
        bridged,
        index,
        model::NoSink,
        kinds,
        annotations,
        clip,
        budget,
    )?;
    Ok(content)
}

/// Returns what [`page_content`] returns, and `sink` after it has seen every
/// glyph and path of the page, whatever `kinds` records.
pub fn page_content_into<S: ContentSink>(
    bridged: &Bridged,
    index: usize,
    sink: S,
    kinds: ItemKinds,
    annotations: bool,
    clip: Option<BBox>,
    budget: &Budget,
) -> Result<(PageContent, S), Error> {
    let pdf = bridged.pdf();
    let pages = pdf.pages();
    let page = pages.get(index).ok_or(Error::NoPage(index))?;
    let settings = InterpreterSettings {
        render_annotations: annotations,
        ..InterpreterSettings::default()
    };
    let cache = InterpreterCache::new();
    model::collect_page_content(pdf, page, settings, &cache, sink, kinds, clip, budget)
}

/// The resolution of an image without a JFIF header, as pymupdf reports it.
pub(crate) const DEFAULT_IMAGE_RESOLUTION: u32 = 96;

/// Returns the dots-per-inch density of a JFIF header, or the default.
pub(crate) fn jpeg_resolution(data: &[u8]) -> (u32, u32) {
    // SOI, then an APP0 segment whose identifier is "JFIF\0".
    if data.len() >= 18 && data[..4] == [0xFF, 0xD8, 0xFF, 0xE0] && &data[6..11] == b"JFIF\0" {
        let units = data[13];
        let x = u32::from(u16::from_be_bytes([data[14], data[15]]));
        let y = u32::from(u16::from_be_bytes([data[16], data[17]]));
        let to_dpi = |value: u32| match units {
            1 => value,
            // Dots per centimetre.
            2 => (f64::from(value) * 2.54).round() as u32,
            _ => 0,
        };
        let (x, y) = (to_dpi(x), to_dpi(y));
        if x > 0 && y > 0 {
            return (x, y);
        }
    }
    (DEFAULT_IMAGE_RESOLUTION, DEFAULT_IMAGE_RESOLUTION)
}

/// Returns an image dictionary's color components and color space name.
///
/// Inline images may abbreviate the keys and the device names.
pub(crate) fn image_color_space(dict: &Dict<'_>) -> (u8, String) {
    let expand = |name: &str| -> String {
        match name {
            "G" => "DeviceGray",
            "RGB" => "DeviceRGB",
            "CMYK" => "DeviceCMYK",
            "I" => "Indexed",
            other => other,
        }
        .to_owned()
    };
    let device_components = |name: &str| match name {
        "DeviceGray" | "CalGray" | "Indexed" | "Separation" | "Pattern" => 1,
        "DeviceCMYK" => 4,
        _ => 3,
    };
    if dict
        .get::<bool>(b"ImageMask")
        .or_else(|| dict.get::<bool>(b"IM"))
        == Some(true)
    {
        return (1, String::new());
    }
    let key = if dict.contains_key(b"ColorSpace") {
        &b"ColorSpace"[..]
    } else {
        &b"CS"[..]
    };
    if let Some(name) = dict.get::<Name<'_>>(key) {
        let name = expand(name.as_str());
        return (device_components(&name), name);
    }
    if let Some(array) = dict.get::<Array<'_>>(key) {
        let mut items = array.iter::<Object<'_>>();
        let family = items
            .next()
            .and_then(|first| first.into_name())
            .map(|name| expand(name.as_str()))
            .unwrap_or_default();
        let mut operands = array.flex_iter();
        let _ = operands.next::<Name<'_>>();
        let components = match family.as_str() {
            "ICCBased" => operands
                .next::<Stream<'_>>()
                .and_then(|profile| profile.dict().get::<u8>(b"N"))
                .unwrap_or(3),
            "DeviceN" => operands.next::<Array<'_>>().map_or(1, |names| {
                u8::try_from(names.iter::<Name<'_>>().count()).unwrap_or(u8::MAX)
            }),
            other => device_components(other),
        };
        return (components, family);
    }
    (0, String::new())
}

/// Builds the interpretation context of a page, with the renderer's
/// `initial_transform(true)`, so that the device receives top-left-origin
/// display coordinates with the rotation applied.
pub(crate) fn extraction_context<'a>(
    pdf: &'a Pdf,
    page: &Page<'a>,
    cache: &InterpreterCache<'a>,
    settings: InterpreterSettings,
) -> Context<'a> {
    let (width, height) = page.render_dimensions();
    Context::new(
        page.initial_transform(true).to_kurbo(),
        Rect::new(0.0, 0.0, f64::from(width), f64::from(height)),
        cache,
        pdf.xref(),
        settings,
    )
}
