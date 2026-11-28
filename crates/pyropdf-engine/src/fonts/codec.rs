//! Type 0 (composite / CID) and Type 3 font codecs for text editing
//! (ISO 32000-2 9.6.5, 9.7, 9.10).
//!
//! Reads font dictionaries, CMaps, width arrays (`/W`, `/DW`, `/W2`, `/DW2`),
//! `/CIDToGIDMap`, `/ToUnicode`, and embedded font programs directly through
//! `pyropdf-store` (DESIGN.md F1, F5, F6), so `Page.replace_text` can decode
//! multi-byte character codes, verify glyph drawability against subsetted
//! programs, compute horizontal and vertical advances, and extend un-subsetted
//! embedded faces without mutating the store during inspection.

use std::collections::{BTreeMap, BTreeSet};
use std::ops::Range;
use std::sync::Arc;

use hayro::hayro_syntax::object::{Object, String as SyntaxString};
use hayro::hayro_syntax::reader::{Reader as SyntaxReader, ReaderExt};
use hayro_font::hayro_cmap::{
    BfString, CMap, CMapName, CharacterCollection, CidFamily, WritingMode, load_embedded,
};
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::pages::MAX_CHAIN;
use pyropdf_store::value::{Array, Dict, Stream, Value};
use pyropdf_store::version::Version;
use pyropdf_store::{Error as StoreError, LimitExceeded, Result as StoreResult};
use skrifa::FontRef;
use skrifa::GlyphId;
use skrifa::instance::{LocationRef, Size};
use skrifa::outline::{DrawSettings, OutlineGlyphCollection, OutlinePen};
use skrifa::raw::TableProvider;
use skrifa::raw::ps::Sid;
use skrifa::raw::ps::cff::CffFontRef;
use skrifa::raw::ps::cff::charset::Charset as CffCharset;

use super::embed::{Face, Measure};
use super::shape::{ShapingFace, WritingDirection};

/// Maximum `usecmap` nesting depth when resolving PDF CMap streams.
const MAX_CMAP_DEPTH: usize = 16;

/// Maximum number of codes expanded from CMap ranges in one font inspection.
const MAX_CMAP_EXPAND: u64 = 1 << 18;

/// Maximum Unicode character length considered for ToUnicode ligature matching.
const MAX_LIGATURE_CHARS: usize = 8;

/// Maximum `CharProc` operators scanned when searching for `d0`/`d1` width.
const MAX_TYPE3_SCAN_BYTES: usize = 256;

/// One decoded character code from a PDF text string.
#[derive(Clone, Debug, PartialEq)]
pub struct DecodedCode {
    /// Raw PDF byte sequence of the character code (1 to 4 bytes).
    pub bytes: Vec<u8>,
    /// Mapped CID (0 when unmapped).
    pub cid: u32,
    /// Decoded Unicode text (`"\u{FFFD}"` when unmapped).
    pub text: String,
    /// Signed displacement along the writing axis in thousandths of text space
    /// (positive horizontal `w0` for `WMode = 0`, negative vertical `w1y` for
    /// `WMode = 1`).
    pub advance: f64,
}

/// One encoded segment produced by [`Type0Codec::encode_text`].
#[derive(Clone, Debug, PartialEq)]
pub enum EncodedSegment {
    /// A glyph encoded in this Type 0 font.
    Glyph {
        /// Raw PDF byte sequence to write in the content stream string.
        bytes: Vec<u8>,
        /// Unicode text represented by this glyph.
        text: String,
        /// Signed nominal PDF displacement in thousandths of text space.
        advance: f64,
        /// Trailing `TJ` kerning adjustment in thousandths of text space
        /// (non-zero when OpenType shaping adjusts advance relative to `/W` or
        /// `/W2`).
        kern_after: f64,
    },
    /// Contiguous text that this font cannot draw (for refusal or fallback).
    Missing {
        /// The unencodable substring.
        text: String,
    },
}

/// A newly used glyph to add to an un-subsetted embedded Type 0 font's `/W`,
/// `/W2` (when vertical), and `/ToUnicode`.
#[derive(Clone, Debug, PartialEq)]
pub struct GlyphExtension {
    /// Glyph ID / CID in the `Identity-H` or `Identity-V` font.
    pub gid: u16,
    /// Horizontal advance width in thousandths of the em (for `/W`).
    pub width: f64,
    /// Vertical metrics `(w1y, vx, vy)` in thousandths of the em (for `/W2`),
    /// present when `vertical` is true.
    pub v_metrics: Option<(f64, f64, f64)>,
    /// Unicode string to map `gid` to in `/ToUnicode`.
    pub text: String,
}

/// Encoding result for one replacement string.
#[derive(Clone, Debug, PartialEq)]
pub struct EncodePlan {
    /// Whether the font is vertical (`WMode = 1`).
    pub vertical: bool,
    /// Encoded glyphs and any missing text slices in drawing order.
    pub segments: Vec<EncodedSegment>,
    /// New glyphs to register in an extensible embedded font.
    pub extensions: Vec<GlyphExtension>,
    /// Existing `/ToUnicode` `(gid, unicode)` entries parsed through Rust's
    /// CMap reader, populated when `extensions` is non-empty so the caller can
    /// extend `/ToUnicode` without regex parsing.
    pub existing_tounicode: Vec<(u16, String)>,
}

/// Summary metadata of a Type 0 font for quick inspection.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Type0Summary {
    /// Whether the font's encoding has vertical writing mode (`WMode = 1`).
    pub vertical: bool,
    /// Whether missing glyphs can be added from the font's own un-subsetted
    /// embedded OpenType program.
    pub can_extend: bool,
    /// The font's `/BaseFont` name, if present.
    pub base_font: Option<String>,
}

/// Metric and drawability information for a Type 3 font.
#[derive(Clone, Debug, PartialEq)]
pub struct Type3Info {
    /// Exact PDF advance width in thousandths of text space for every byte
    /// code `0..256` (`w * FontMatrix[0] * 1000`).
    pub advances: [f64; 256],
    /// Whether each byte code `0..256` maps to a glyph present in `/CharProcs`
    /// (or is a space with a defined advance).
    pub drawable: [bool; 256],
    /// Explicit `/ToUnicode` mappings `(byte_code, unicode_text)`, when the
    /// font carries a `/ToUnicode` stream.
    pub to_unicode: Vec<(u8, String)>,
}

/// One inclusive width range from `/W`.
#[derive(Clone, Copy, Debug)]
struct WidthRange {
    start: u32,
    end: u32,
    width: f64,
}

/// One inclusive vertical-metric range from `/W2`.
#[derive(Clone, Copy, Debug)]
struct Width2Range {
    start: u32,
    end: u32,
    w1y: f64,
}

/// One codespace range from a CMap stream (ISO 32000-2 9.7.6.2).
///
/// Each byte position `i in 0..len` forms an independent inclusive bound
/// `low[i]..=high[i]` (an `n`-dimensional rectangle, not a scalar integer
/// interval).
#[derive(Clone, Copy, Debug)]
struct CodespaceRange {
    len: u8,
    low: [u8; 4],
    high: [u8; 4],
}

impl CodespaceRange {
    fn matches(&self, bytes: &[u8]) -> bool {
        let n = usize::from(self.len);
        bytes.len() >= n && (0..n).all(|i| bytes[i] >= self.low[i] && bytes[i] <= self.high[i])
    }
}

/// One CID mapping interval from a custom CMap stream.
#[derive(Clone, Copy, Debug)]
struct CidMapRange {
    len: u8,
    start: u32,
    end: u32,
    cid_start: u32,
}

/// Compact destination of a `beginbfchar` or `beginbfrange` entry.
#[derive(Clone, Debug)]
enum BfDest {
    One(u16),
    Two(u16, u16),
    Many(Vec<u16>),
}

impl BfDest {
    fn from_be_bytes(bytes: &[u8], budget: &Budget) -> StoreResult<Option<Self>> {
        if bytes.is_empty() {
            return Ok(None);
        }
        match bytes.len() {
            1 => Ok(Some(Self::One(u16::from(bytes[0])))),
            2 => Ok(Some(Self::One(u16::from_be_bytes([bytes[0], bytes[1]])))),
            3 => Ok(Some(Self::Two(
                u16::from(bytes[0]),
                u16::from_be_bytes([bytes[1], bytes[2]]),
            ))),
            4 => Ok(Some(Self::Two(
                u16::from_be_bytes([bytes[0], bytes[1]]),
                u16::from_be_bytes([bytes[2], bytes[3]]),
            ))),
            _ => {
                let Some(units) = utf16be_units(bytes, budget)? else {
                    return Ok(None);
                };
                Ok(Some(Self::Many(units)))
            }
        }
    }

    fn decode_at(&self, offset: u32) -> Option<String> {
        let off = u16::try_from(offset).ok()?;
        match self {
            Self::One(u) => {
                let val = u.checked_add(off)?;
                char::from_u32(u32::from(val)).map(|c| c.to_string())
            }
            Self::Two(hi, lo) => {
                let val = lo.checked_add(off)?;
                String::from_utf16(&[*hi, val]).ok()
            }
            Self::Many(units) => {
                let (&last, prefix) = units.split_last()?;
                let next_last = last.checked_add(off)?;
                let mut iter = prefix.iter().copied().chain(std::iter::once(next_last));
                let mut out = String::new();
                for decoded in char::decode_utf16(&mut iter) {
                    let ch = decoded.ok()?;
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "decoded string length is bounded by the CMap bf entry"
                    )]
                    out.push(ch);
                }
                Some(out)
            }
        }
    }
}

/// One compact `bfchar` or `bfrange` entry stored without eager expansion.
#[derive(Clone, Debug)]
struct BfRange {
    len: u8,
    start: u32,
    end: u32,
    dst: BfDest,
}

/// One entry in a `/ToUnicode` stream, preserving stream order across
/// interleaved `bfchar`, `bfrange`, `cidchar`, and `cidrange` blocks.
#[derive(Clone, Debug)]
enum ToUnicodeEntry {
    Bf(BfRange),
    Cid(CidMapRange),
}

/// Parsed tables from a raw PDF CMap stream.
#[derive(Clone, Debug, Default)]
struct ParsedCMapTables {
    codespaces: Vec<CodespaceRange>,
    cid_ranges: Vec<CidMapRange>,
    to_unicode_entries: Vec<ToUnicodeEntry>,
    wmode: Option<u8>,
    usecmap_name: Option<Range<usize>>,
}

/// How CIDs map to glyph IDs in the descendant CIDFont.
#[derive(Clone, Debug)]
enum CidToGid {
    Identity,
    Mapped(Vec<u16>),
}

impl CidToGid {
    fn map(&self, cid: u32) -> u32 {
        match self {
            Self::Identity => cid,
            Self::Mapped(table) => usize::try_from(cid)
                .ok()
                .and_then(|idx| table.get(idx).copied())
                .map_or(0, u32::from),
        }
    }

    fn inverse_map(&self, cid: u32) -> u32 {
        match self {
            Self::Identity => cid,
            Self::Mapped(table) => {
                for (index, &gid) in table.iter().enumerate() {
                    if u32::from(gid) == cid {
                        return u32::try_from(index).unwrap_or(0);
                    }
                }
                cid
            }
        }
    }
}

/// Loaded embedded font program used for drawability checks and extension.
enum ProgramState {
    /// Non-embedded predefined CID font (relies on viewer CJK font substitution).
    None,
    /// Embedded SFNT (TrueType or OpenType CFF) program.
    Sfnt {
        data: Arc<Vec<u8>>,
        is_cid_keyed_cff: bool,
        is_identity_cff_charset: bool,
        num_glyphs: u32,
        editable_face: Option<Face>,
    },
    /// Embedded bare CFF program (`CIDFontType0C` or `Type1C`).
    BareCff {
        data: Arc<Vec<u8>>,
        is_cid_keyed: bool,
    },
    /// Embedded program in another format (e.g. Type 1 / unrecognized).
    Other,
}

/// One layer in a resolved CMap `/UseCMap` chain.
#[derive(Clone, Debug)]
enum CMapLayer {
    Parsed(Box<CMap>),
    Tables(Vec<CidMapRange>),
}

/// Predefined base CMap class reachable through a custom CMap's `/UseCMap`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum PredefinedBase {
    Utf16,
    Other,
}

/// Encoding classification of a Type 0 font.
#[derive(Clone, Debug)]
enum EncodingKind {
    Identity,
    PredefinedUtf16,
    PredefinedOther,
    CustomStream,
}

/// How much of a Type 0 font to load.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum LoadMode {
    Inspect,
    Decode,
    Encode,
}

/// Parsed Type 0 font codec backed by the store.
pub struct Type0Codec {
    base_font: Option<String>,
    vertical: bool,
    encoding_kind: EncodingKind,
    base_predefined: Option<PredefinedBase>,
    encoding_chain: Vec<CMapLayer>,
    custom_codespaces: Vec<CodespaceRange>,
    custom_cid_ranges: Vec<CidMapRange>,
    has_to_unicode: bool,
    to_unicode: Option<CMap>,
    to_unicode_entries: Vec<ToUnicodeEntry>,
    ucs2_cmap: Option<CMap>,
    dw: f64,
    widths: Vec<WidthRange>,
    dw2_w1y: f64,
    widths2: Vec<Width2Range>,
    cid_to_gid: CidToGid,
    is_subset_tagged: bool,
    program: ProgramState,
    can_extend: bool,
}

/// Per-operation parsed font state for O(1) outline & CFF-charset drawability checks.
enum DrawChecker<'a> {
    AlwaysNonZero,
    Sfnt {
        outlines: Option<Box<OutlineGlyphCollection<'a>>>,
        cff_charset: Option<CffCharset<'a>>,
        is_cid_keyed_cff: bool,
        is_identity_cff_charset: bool,
        num_glyphs: u32,
    },
    BareCff {
        cff: Option<Box<CffFontRef<'a>>>,
        charset: Option<CffCharset<'a>>,
        is_cid_keyed: bool,
    },
}

impl<'a> DrawChecker<'a> {
    fn new(program: &'a ProgramState) -> Self {
        match program {
            ProgramState::None | ProgramState::Other => Self::AlwaysNonZero,
            ProgramState::Sfnt {
                data,
                is_cid_keyed_cff,
                is_identity_cff_charset,
                num_glyphs,
                ..
            } => {
                let font = FontRef::new(data).ok();
                let outlines = font
                    .as_ref()
                    .map(|f| Box::new(OutlineGlyphCollection::new(f)));
                let cff_charset = if *is_cid_keyed_cff && !*is_identity_cff_charset {
                    font.as_ref()
                        .and_then(|f| f.table_data(skrifa::Tag::new(b"CFF ")))
                        .and_then(|t| CffFontRef::new_cff(t.as_bytes(), 0, None).ok())
                        .and_then(|cff| cff.charset())
                } else {
                    None
                };
                Self::Sfnt {
                    outlines,
                    cff_charset,
                    is_cid_keyed_cff: *is_cid_keyed_cff,
                    is_identity_cff_charset: *is_identity_cff_charset,
                    num_glyphs: *num_glyphs,
                }
            }
            ProgramState::BareCff { data, is_cid_keyed } => {
                let cff = CffFontRef::new_cff(data, 0, None).ok().map(Box::new);
                let charset = if *is_cid_keyed {
                    cff.as_deref().and_then(CffFontRef::charset)
                } else {
                    None
                };
                Self::BareCff {
                    cff,
                    charset,
                    is_cid_keyed: *is_cid_keyed,
                }
            }
        }
    }

    fn sfnt_has_ink(&self, gid: u32) -> bool {
        let Self::Sfnt {
            outlines: Some(outlines),
            ..
        } = self
        else {
            return false;
        };
        let Some(outline) = outlines.get(GlyphId::new(gid)) else {
            return false;
        };
        let mut pen = HasContourPen::default();
        let settings = DrawSettings::unhinted(Size::unscaled(), LocationRef::default());
        outline.draw(settings, &mut pen).is_ok() && pen.has_ink
    }
}

/// Minimal pen that detects whether an outline draws any segment.
#[derive(Default)]
struct HasContourPen {
    has_ink: bool,
}

impl OutlinePen for HasContourPen {
    fn move_to(&mut self, _x: f32, _y: f32) {}

    fn line_to(&mut self, _x: f32, _y: f32) {
        self.has_ink = true;
    }

    fn quad_to(&mut self, _cx0: f32, _cy0: f32, _x: f32, _y: f32) {
        self.has_ink = true;
    }

    fn curve_to(&mut self, _cx0: f32, _cy0: f32, _cx1: f32, _cy1: f32, _x: f32, _y: f32) {
        self.has_ink = true;
    }

    fn close(&mut self) {}
}

impl Type0Codec {
    /// Inspects `font_dict` in `version` without loading widths, `/ToUnicode`,
    /// or the embedded font program.
    pub fn inspect(
        version: &Version,
        font_dict: &Dict,
        budget: &Budget,
    ) -> StoreResult<Option<Type0Summary>> {
        Ok(
            Self::load_with_mode(version, font_dict, LoadMode::Inspect, budget)?
                .map(|codec| codec.summary()),
        )
    }

    /// Loads `font_dict` for decoding strings without decompressing the
    /// embedded font program or `/CIDToGIDMap`.
    pub fn load_for_decode(
        version: &Version,
        font_dict: &Dict,
        budget: &Budget,
    ) -> StoreResult<Option<Self>> {
        Self::load_with_mode(version, font_dict, LoadMode::Decode, budget)
    }

    /// Loads `font_dict` in `version` including its embedded font program,
    /// returning `None` when it is not a well-formed Type 0 composite font.
    pub fn load(version: &Version, font_dict: &Dict, budget: &Budget) -> StoreResult<Option<Self>> {
        Self::load_with_mode(version, font_dict, LoadMode::Encode, budget)
    }

    fn load_with_mode(
        version: &Version,
        font_dict: &Dict,
        mode: LoadMode,
        budget: &Budget,
    ) -> StoreResult<Option<Self>> {
        if !is_name(font_dict.get("Subtype"), b"Type0") {
            return Ok(None);
        }
        let base_font = match font_dict.get("BaseFont") {
            Some(Value::Name(name)) => Some(String::from_utf8_lossy(name.as_bytes()).into_owned()),
            _ => None,
        };
        let Some(encoding_val) = resolve_entry(version, font_dict, "Encoding", budget)? else {
            return Ok(None);
        };
        let Some(enc) = load_encoding(version, &encoding_val, budget, 0)? else {
            return Ok(None);
        };

        let Some(descendants_val) = resolve_entry(version, font_dict, "DescendantFonts", budget)?
        else {
            return Ok(None);
        };
        let Value::Array(descendants) = &*descendants_val else {
            return Ok(None);
        };
        let Some(first_desc) = descendants.get(0) else {
            return Ok(None);
        };
        let Some(desc_val) = resolve_value(version, first_desc, budget)? else {
            return Ok(None);
        };
        let Value::Dict(descendant) = &*desc_val else {
            return Ok(None);
        };
        let desc_is_indirect = matches!(first_desc, Value::Reference(_));

        let cid_is_truetype = is_name(descendant.get("Subtype"), b"CIDFontType2");
        let cid_is_cff = is_name(descendant.get("Subtype"), b"CIDFontType0");
        if !cid_is_truetype && !cid_is_cff {
            return Ok(None);
        }

        if mode == LoadMode::Inspect {
            return Ok(Some(Self {
                base_font,
                vertical: enc.vertical,
                encoding_kind: enc.kind,
                base_predefined: enc.base_predefined,
                encoding_chain: enc.chain,
                custom_codespaces: enc.codespaces,
                custom_cid_ranges: enc.cid_ranges,
                has_to_unicode: false,
                to_unicode: None,
                to_unicode_entries: Vec::new(),
                ucs2_cmap: None,
                dw: 1000.0,
                widths: Vec::new(),
                dw2_w1y: -1000.0,
                widths2: Vec::new(),
                cid_to_gid: CidToGid::Identity,
                is_subset_tagged: false,
                program: ProgramState::None,
                can_extend: false,
            }));
        }

        let character_collection = enc
            .chain
            .iter()
            .find_map(|layer| match layer {
                CMapLayer::Parsed(c) => c
                    .metadata()
                    .character_collection
                    .clone()
                    .filter(|cc| cc.family != CidFamily::AdobeIdentity),
                CMapLayer::Tables(_) => None,
            })
            .or_else(|| {
                read_cid_system_info(version, descendant, budget)
                    .ok()
                    .flatten()
            });

        let dw = number_of(descendant.get("DW")).unwrap_or(1000.0);
        let widths = match resolve_entry(version, descendant, "W", budget)? {
            Some(val) => match &*val {
                Value::Array(arr) => parse_widths(arr, budget)?,
                _ => Vec::new(),
            },
            None => Vec::new(),
        };

        let dw2_w1y = match resolve_entry(version, descendant, "DW2", budget)? {
            Some(val) => match &*val {
                Value::Array(arr) if arr.len() >= 2 => number_of(arr.get(1)).unwrap_or(-1000.0),
                _ => -1000.0,
            },
            None => -1000.0,
        };
        let widths2 = match resolve_entry(version, descendant, "W2", budget)? {
            Some(val) => match &*val {
                Value::Array(arr) => parse_widths2(arr, budget)?,
                _ => Vec::new(),
            },
            None => Vec::new(),
        };

        let to_unicode_indirect = matches!(font_dict.get("ToUnicode"), Some(Value::Reference(_)));
        let (has_to_unicode, to_unicode, to_unicode_entries) =
            match resolve_entry(version, font_dict, "ToUnicode", budget)? {
                Some(val) => match &*val {
                    Value::Stream(stream) => {
                        let data = stream.decoded(budget)?;
                        let n = data.len() as u64;
                        budget.check(LimitCode::FontInputSize, n)?;
                        budget.charge(LimitCode::InterpretationFuel, n)?;
                        let cmap = CMap::parse(&data, load_embedded);
                        let tables = parse_cmap_tables(&data, true, budget)?;
                        (true, cmap, tables.to_unicode_entries)
                    }
                    _ => (false, None, Vec::new()),
                },
                None => (false, None, Vec::new()),
            };

        let ucs2_cmap = character_collection
            .as_ref()
            .and_then(|cc| cc.family.ucs2_cmap())
            .and_then(load_embedded)
            .and_then(|data| CMap::parse(data, load_embedded));

        if mode == LoadMode::Decode {
            return Ok(Some(Self {
                base_font,
                vertical: enc.vertical,
                encoding_kind: enc.kind,
                base_predefined: enc.base_predefined,
                encoding_chain: enc.chain,
                custom_codespaces: enc.codespaces,
                custom_cid_ranges: enc.cid_ranges,
                has_to_unicode,
                to_unicode,
                to_unicode_entries,
                ucs2_cmap,
                dw,
                widths,
                dw2_w1y,
                widths2,
                cid_to_gid: CidToGid::Identity,
                is_subset_tagged: false,
                program: ProgramState::None,
                can_extend: false,
            }));
        }

        let cid_to_gid = load_cid_to_gid(version, descendant, budget)?;

        let descriptor = match resolve_entry(version, descendant, "FontDescriptor", budget)? {
            Some(val) => match &*val {
                Value::Dict(dict) => Some(dict.clone()),
                _ => None,
            },
            None => None,
        };

        let is_subset_tagged = base_font
            .as_deref()
            .is_some_and(|n| has_subset_tag(n.as_bytes()))
            || match descendant.get("BaseFont") {
                Some(Value::Name(n)) => has_subset_tag(n.as_bytes()),
                _ => false,
            }
            || descriptor
                .as_ref()
                .and_then(|d| match d.get("FontName") {
                    Some(Value::Name(n)) => Some(has_subset_tag(n.as_bytes())),
                    _ => None,
                })
                .unwrap_or(false);

        let cid_to_gid_is_identity = matches!(cid_to_gid, CidToGid::Identity);
        let cid_to_gid_explicit_identity = is_name(descendant.get("CIDToGIDMap"), b"Identity");
        let may_extend_base = !is_subset_tagged
            && matches!(enc.kind, EncodingKind::Identity)
            && desc_is_indirect
            && to_unicode_indirect
            && ((cid_is_truetype && cid_to_gid_explicit_identity)
                || (cid_is_cff && cid_to_gid_is_identity));

        let (program, is_opentype_stream) = match descriptor.as_ref() {
            Some(desc) => load_program(version, desc, cid_is_truetype, may_extend_base, budget)?,
            None => (ProgramState::None, false),
        };

        let (has_editable_face, cff_charset_ok) = match &program {
            ProgramState::Sfnt {
                editable_face,
                is_cid_keyed_cff,
                is_identity_cff_charset,
                ..
            } => (
                editable_face.is_some(),
                !*is_cid_keyed_cff || *is_identity_cff_charset,
            ),
            _ => (false, false),
        };

        let can_extend = may_extend_base
            && has_editable_face
            && cff_charset_ok
            && (cid_is_truetype || (cid_is_cff && is_opentype_stream));

        Ok(Some(Self {
            base_font,
            vertical: enc.vertical,
            encoding_kind: enc.kind,
            base_predefined: enc.base_predefined,
            encoding_chain: enc.chain,
            custom_codespaces: enc.codespaces,
            custom_cid_ranges: enc.cid_ranges,
            has_to_unicode,
            to_unicode,
            to_unicode_entries,
            ucs2_cmap,
            dw,
            widths,
            dw2_w1y,
            widths2,
            cid_to_gid,
            is_subset_tagged,
            program,
            can_extend,
        }))
    }

    /// Returns a compact summary of this font.
    #[must_use]
    pub fn summary(&self) -> Type0Summary {
        Type0Summary {
            vertical: self.vertical,
            can_extend: self.can_extend,
            base_font: self.base_font.clone(),
        }
    }

    /// Returns whether this font uses vertical writing mode (`WMode = 1`).
    #[must_use]
    pub const fn is_vertical(&self) -> bool {
        self.vertical
    }

    /// Decodes a batch of raw PDF strings into character codes, Unicode text,
    /// and signed writing-axis advances.
    pub fn decode_strings(
        &self,
        strings: &[&[u8]],
        budget: &Budget,
    ) -> StoreResult<Vec<Vec<DecodedCode>>> {
        let mut results: BudgetVec<'_, Vec<DecodedCode>> = BudgetVec::new(budget);
        for &bytes in strings {
            budget.poll()?;
            let mut decoded: BudgetVec<'_, DecodedCode> = BudgetVec::new(budget);
            let mut offset = 0_usize;
            while offset < bytes.len() {
                if offset.is_multiple_of(256) {
                    budget.poll()?;
                }
                let (code, len, cid) = self.read_code(bytes, offset);
                let end = offset.saturating_add(len).min(bytes.len());
                #[expect(
                    clippy::disallowed_methods,
                    reason = "a character code is 1 to 4 bytes"
                )]
                let raw = bytes[offset..end].to_vec();
                let text = self
                    .code_to_unicode(code, cid)
                    .unwrap_or_else(|| "\u{FFFD}".to_owned());
                let advance = self.advance_for_cid(cid);
                decoded.try_push(DecodedCode {
                    bytes: raw,
                    cid,
                    text,
                    advance,
                })?;
                offset = end;
            }
            results.try_push(decoded.into_vec())?;
        }
        Ok(results.into_vec())
    }

    /// Encodes `text` through this Type 0 font, extending an un-subsetted
    /// embedded face when permitted, and marking any unencodable slices as
    /// [`EncodedSegment::Missing`].
    pub fn encode_text(&self, text: &str, budget: &Budget) -> StoreResult<EncodePlan> {
        let mut segments: BudgetVec<'_, EncodedSegment> = BudgetVec::new(budget);
        let mut extensions: BudgetVec<'_, GlyphExtension> = BudgetVec::new(budget);
        if text.is_empty() {
            return Ok(EncodePlan {
                vertical: self.vertical,
                segments: Vec::new(),
                extensions: Vec::new(),
                existing_tounicode: Vec::new(),
            });
        }

        // First build the existing drawable reverse map from ToUnicode (or
        // predefined/custom CMaps when no ToUnicode stream is present).
        let reverse = self.build_reverse_map(text, budget)?;

        // Check if every character of `text` can already be encoded using
        // existing drawable mappings (without needing face extension).
        if !self.can_extend || self.can_encode_with_map(text, &reverse) {
            self.encode_with_map(text, &reverse, &mut segments, budget)?;
            return Ok(EncodePlan {
                vertical: self.vertical,
                segments: segments.into_vec(),
                extensions: Vec::new(),
                existing_tounicode: Vec::new(),
            });
        }

        // The font is an editable/un-subsetted pyropdf-embedded OpenType face
        // and `text` requires glyphs not yet in ToUnicode/W/W2. Shape with
        // harfrust (applying `vert` for vertical mode and OpenType ligatures
        // for horizontal mode) and plan any needed font extensions.
        self.encode_with_extension(text, &mut segments, &mut extensions, budget)?;
        let extensions = extensions.into_vec();
        let existing_tounicode = if extensions.is_empty() {
            Vec::new()
        } else {
            self.collect_existing_tounicode(budget)?
        };
        Ok(EncodePlan {
            vertical: self.vertical,
            segments: segments.into_vec(),
            extensions,
            existing_tounicode,
        })
    }

    fn collect_existing_tounicode(&self, budget: &Budget) -> StoreResult<Vec<(u16, String)>> {
        let mut mapped: BTreeMap<u16, String> = BTreeMap::new();
        let mut expanded = 0_u64;
        for entry in &self.to_unicode_entries {
            budget.poll()?;
            match entry {
                ToUnicodeEntry::Bf(range) => {
                    let end = range.end.min(u32::from(u16::MAX));
                    if range.start > end {
                        continue;
                    }
                    let count = u64::from(end - range.start) + 1;
                    expanded = expanded.saturating_add(count);
                    if expanded > MAX_CMAP_EXPAND {
                        return Err(StoreError::from(
                            LimitExceeded::new(LimitCode::InterpretationFuel)
                                .with_limit(MAX_CMAP_EXPAND)
                                .with_amount(expanded),
                        ));
                    }
                    budget.charge(LimitCode::InterpretationFuel, count)?;
                    for offset in 0..count {
                        if offset.is_multiple_of(256) {
                            budget.poll()?;
                        }
                        let Some(text) = range.dst.decode_at(offset as u32) else {
                            continue;
                        };
                        if text.is_empty() || text == "\0" {
                            continue;
                        }
                        let code = (range.start + offset as u32) as u16;
                        let extra = match mapped.get(&code) {
                            None => 48 + text.len() as u64,
                            Some(prev) => text.len().saturating_sub(prev.len()) as u64,
                        };
                        if extra > 0 {
                            budget.charge(LimitCode::MemorySize, extra)?;
                        }
                        #[expect(
                            clippy::disallowed_methods,
                            reason = "bounded by u16 code space and charged to MemorySize"
                        )]
                        mapped.insert(code, text);
                    }
                }
                ToUnicodeEntry::Cid(range) => {
                    let end = range.end.min(u32::from(u16::MAX));
                    if range.start > end {
                        continue;
                    }
                    let count = u64::from(end - range.start) + 1;
                    expanded = expanded.saturating_add(count);
                    if expanded > MAX_CMAP_EXPAND {
                        return Err(StoreError::from(
                            LimitExceeded::new(LimitCode::InterpretationFuel)
                                .with_limit(MAX_CMAP_EXPAND)
                                .with_amount(expanded),
                        ));
                    }
                    budget.charge(LimitCode::InterpretationFuel, count)?;
                    for offset in 0..count {
                        if offset.is_multiple_of(256) {
                            budget.poll()?;
                        }
                        let Some(ch) = range
                            .cid_start
                            .checked_add(offset as u32)
                            .and_then(char::from_u32)
                        else {
                            continue;
                        };
                        if ch == '\0' {
                            continue;
                        }
                        let code = (range.start + offset as u32) as u16;
                        let text = ch.to_string();
                        let extra = match mapped.get(&code) {
                            None => 48 + text.len() as u64,
                            Some(prev) => text.len().saturating_sub(prev.len()) as u64,
                        };
                        if extra > 0 {
                            budget.charge(LimitCode::MemorySize, extra)?;
                        }
                        #[expect(
                            clippy::disallowed_methods,
                            reason = "bounded by u16 code space and charged to MemorySize"
                        )]
                        mapped.insert(code, text);
                    }
                }
            }
        }
        let mut out: BudgetVec<'_, (u16, String)> = BudgetVec::new(budget);
        for (code, text) in mapped {
            out.try_push((code, text))?;
        }
        Ok(out.into_vec())
    }

    fn read_code(&self, bytes: &[u8], offset: usize) -> (u32, usize, u32) {
        let remaining = bytes.len().saturating_sub(offset);
        let max_len = remaining.min(4);
        let mut code = 0_u32;
        for n in 0..max_len {
            code = (code << 8) | u32::from(bytes[offset + n]);
            let len = (n + 1) as u8;
            if let Some(cid) = self.lookup_cid(code, len) {
                return (code, n + 1, cid);
            }
        }
        // Fallback: honor explicit rectangular codespace ranges (ISO 32000-2 9.7.6.2)
        // when a code falls inside a codespace range without a CID mapping (maps to CID 0).
        let mut fallback_code = 0_u32;
        for n in 0..max_len {
            fallback_code = (fallback_code << 8) | u32::from(bytes[offset + n]);
            let len = (n + 1) as u8;
            let slice = &bytes[offset..offset + n + 1];
            if self
                .custom_codespaces
                .iter()
                .any(|cs| cs.len == len && cs.matches(slice))
            {
                return (fallback_code, n + 1, 0);
            }
        }
        (u32::from(bytes[offset]), 1, 0)
    }

    fn lookup_cid(&self, code: u32, byte_len: u8) -> Option<u32> {
        for layer in &self.encoding_chain {
            match layer {
                CMapLayer::Parsed(cmap) => {
                    if let Some(cid) = cmap.lookup_cid_code(code, byte_len) {
                        return Some(cid);
                    }
                }
                CMapLayer::Tables(ranges) => {
                    if let Some(cid) = lookup_cid_in_ranges(ranges, code, byte_len) {
                        return Some(cid);
                    }
                }
            }
        }
        None
    }

    fn code_to_unicode(&self, code: u32, cid: u32) -> Option<String> {
        if let Some(to_unicode) = &self.to_unicode {
            if let Some(bf) = to_unicode.lookup_bf_string(code) {
                let s = bf_to_string(bf);
                if s != "\0" {
                    return Some(s);
                }
            }
            for len in 1..=4_u8 {
                if let Some(cp) = to_unicode.lookup_cid_code(code, len)
                    && let Some(ch) = char::from_u32(cp)
                    && ch != '\0'
                {
                    return Some(ch.to_string());
                }
            }
            return None;
        }
        if self.has_to_unicode {
            for entry in self.to_unicode_entries.iter().rev() {
                match entry {
                    ToUnicodeEntry::Bf(r) if code >= r.start && code <= r.end => {
                        if let Some(s) = r.dst.decode_at(code - r.start)
                            && !s.is_empty()
                            && s != "\0"
                        {
                            return Some(s);
                        }
                    }
                    ToUnicodeEntry::Cid(r) if code >= r.start && code <= r.end => {
                        if let Some(ch) = r
                            .cid_start
                            .checked_add(code - r.start)
                            .and_then(char::from_u32)
                            && ch != '\0'
                        {
                            return Some(ch.to_string());
                        }
                    }
                    _ => {}
                }
            }
            return None;
        }
        if let Some(ucs2) = &self.ucs2_cmap
            && let Some(bf) = ucs2.lookup_bf_string(cid)
        {
            let s = bf_to_string(bf);
            if s != "\0" {
                return Some(s);
            }
        }
        None
    }

    fn advance_for_cid(&self, cid: u32) -> f64 {
        if self.vertical {
            lookup_width2(&self.widths2, cid).unwrap_or(self.dw2_w1y)
        } else {
            lookup_width(&self.widths, cid).unwrap_or(self.dw)
        }
    }

    fn is_drawable_with(&self, checker: &DrawChecker<'_>, cid: u32, text: &str) -> bool {
        let is_whitespace = !text.is_empty() && text.chars().all(char::is_whitespace);
        match checker {
            DrawChecker::AlwaysNonZero => cid != 0,
            DrawChecker::Sfnt {
                cff_charset,
                is_cid_keyed_cff,
                is_identity_cff_charset,
                num_glyphs,
                ..
            } => {
                let gid = if *is_cid_keyed_cff {
                    if matches!(self.cid_to_gid, CidToGid::Identity) {
                        if *is_identity_cff_charset {
                            cid
                        } else {
                            u16::try_from(cid)
                                .ok()
                                .and_then(|c| cff_charset.as_ref()?.glyph_id(Sid::new(c)))
                                .map_or(0, |g| g.to_u32())
                        }
                    } else {
                        self.cid_to_gid.inverse_map(cid)
                    }
                } else {
                    self.cid_to_gid.map(cid)
                };
                if gid == 0 || gid >= *num_glyphs {
                    return false;
                }
                if is_whitespace {
                    if self.is_subset_tagged {
                        return lookup_width(&self.widths, cid).is_some()
                            || lookup_width2(&self.widths2, cid).is_some()
                            || self.has_to_unicode;
                    }
                    return true;
                }
                checker.sfnt_has_ink(gid)
            }
            DrawChecker::BareCff {
                cff,
                charset,
                is_cid_keyed,
            } => {
                let gid = if *is_cid_keyed {
                    if matches!(self.cid_to_gid, CidToGid::Identity) {
                        u16::try_from(cid)
                            .ok()
                            .and_then(|c| charset.as_ref()?.glyph_id(Sid::new(c)))
                            .map_or(0, |g| g.to_u32())
                    } else {
                        self.cid_to_gid.inverse_map(cid)
                    }
                } else {
                    self.cid_to_gid.map(cid)
                };
                if gid == 0 {
                    return false;
                }
                if is_whitespace {
                    return true;
                }
                cff.as_deref()
                    .is_some_and(|c| bare_cff_ref_glyph_has_ink(c, gid))
            }
        }
    }

    #[expect(
        clippy::too_many_arguments,
        reason = "candidate validation context shared across reverse-map paths"
    )]
    fn insert_tounicode_candidate(
        &self,
        text: &str,
        needed: &BTreeSet<char>,
        checker: &DrawChecker<'_>,
        code: u32,
        byte_len: u8,
        unicode: String,
        map: &mut BTreeMap<String, (Vec<u8>, u32)>,
        budget: &Budget,
    ) -> StoreResult<()> {
        if unicode.is_empty() || unicode == "\0" {
            return Ok(());
        }
        // Pre-filter against `text` BEFORE CMap CID lookup, decode verification,
        // or font outline inspection.
        let Some(first_ch) = unicode.chars().next() else {
            return Ok(());
        };
        if !needed.contains(&first_ch) || !text.contains(unicode.as_str()) {
            return Ok(());
        }
        let resolved = self
            .lookup_cid(code, byte_len)
            .map(|cid| (byte_len, cid))
            .or_else(|| {
                (1..=4_u8).find_map(|len| self.lookup_cid(code, len).map(|cid| (len, cid)))
            });
        let Some((actual_len, cid)) = resolved else {
            return Ok(());
        };
        // Ensure round-trip symmetry with `code_to_unicode` (overridden earlier
        // bfchar/bfrange entries must never be selected).
        if self.code_to_unicode(code, cid).as_deref() != Some(unicode.as_str()) {
            return Ok(());
        }
        if !self.is_drawable_with(checker, cid, &unicode) {
            return Ok(());
        }
        let raw = encode_code_bytes(code, actual_len);
        let (replace, is_new) = match map.get(&unicode) {
            None => (true, true),
            Some((existing, _)) => (
                raw.len() < existing.len() || (raw.len() == existing.len() && raw < *existing),
                false,
            ),
        };
        if replace {
            if is_new {
                budget.charge(
                    LimitCode::MemorySize,
                    64 + unicode.len() as u64 + raw.len() as u64,
                )?;
            }
            #[expect(
                clippy::disallowed_methods,
                reason = "charged to MemorySize and bounded by substrings of `text`"
            )]
            map.insert(unicode, (raw, cid));
        }
        Ok(())
    }

    fn build_reverse_map(
        &self,
        text: &str,
        budget: &Budget,
    ) -> StoreResult<BTreeMap<String, (Vec<u8>, u32)>> {
        let mut map: BTreeMap<String, (Vec<u8>, u32)> = BTreeMap::new();
        let mut needed: BTreeSet<char> = BTreeSet::new();
        for ch in text.chars() {
            #[expect(
                clippy::disallowed_methods,
                reason = "at most one entry per distinct char in the caller's replacement string"
            )]
            needed.insert(ch);
        }
        let checker = DrawChecker::new(&self.program);

        // 1. Existing `/ToUnicode` stream entries (covers Identity-H/V, ligatures,
        //    custom CMaps with ToUnicode, and predefined CMaps with ToUnicode).
        for entry in &self.to_unicode_entries {
            budget.poll()?;
            match entry {
                ToUnicodeEntry::Bf(range) => {
                    if range.start == range.end {
                        // Fast pre-filter before allocating for single-unit entries.
                        if let BfDest::One(u) = range.dst {
                            if let Some(ch) = char::from_u32(u32::from(u))
                                && needed.contains(&ch)
                            {
                                self.insert_tounicode_candidate(
                                    text,
                                    &needed,
                                    &checker,
                                    range.start,
                                    range.len,
                                    ch.to_string(),
                                    &mut map,
                                    budget,
                                )?;
                            }
                            continue;
                        }
                        if let Some(unicode) = range.dst.decode_at(0) {
                            self.insert_tounicode_candidate(
                                text,
                                &needed,
                                &checker,
                                range.start,
                                range.len,
                                unicode,
                                &mut map,
                                budget,
                            )?;
                        }
                    } else {
                        self.match_bfrange_against_text(
                            range, text, &needed, &checker, &mut map, budget,
                        )?;
                    }
                }
                ToUnicodeEntry::Cid(range) => {
                    let span = range.end.saturating_sub(range.start);
                    for &ch in &needed {
                        let cp = u32::from(ch);
                        if cp >= range.cid_start {
                            let offset = cp - range.cid_start;
                            if offset <= span {
                                self.insert_tounicode_candidate(
                                    text,
                                    &needed,
                                    &checker,
                                    range.start + offset,
                                    range.len,
                                    ch.to_string(),
                                    &mut map,
                                    budget,
                                )?;
                            }
                        }
                    }
                }
            }
        }

        // If a `/ToUnicode` stream is present on a non-extensible font, only
        // codes mapped by `/ToUnicode` will extract back reliably in both
        // pyropdf and Poppler.
        if self.has_to_unicode {
            return Ok(map);
        }

        // 2. No `/ToUnicode` stream: rely on Encoding CMap + predefined UCS2 CMap.
        let Some(ucs2) = &self.ucs2_cmap else {
            return Ok(map);
        };

        match self.encoding_kind {
            EncodingKind::PredefinedUtf16 => {
                self.probe_predefined_utf16(&needed, ucs2, &checker, &mut map, budget)?;
            }
            EncodingKind::CustomStream => {
                let mut expanded = 0_u64;
                for range in &self.custom_cid_ranges {
                    if needed.is_empty() {
                        break;
                    }
                    let count = u64::from(range.end.saturating_sub(range.start)) + 1;
                    expanded = expanded.saturating_add(count);
                    if expanded > MAX_CMAP_EXPAND {
                        return Err(StoreError::from(
                            LimitExceeded::new(LimitCode::InterpretationFuel)
                                .with_limit(MAX_CMAP_EXPAND)
                                .with_amount(expanded),
                        ));
                    }
                    budget.charge(LimitCode::InterpretationFuel, count)?;
                    for offset in 0..count {
                        if offset.is_multiple_of(256) {
                            budget.poll()?;
                        }
                        let code = range.start + offset as u32;
                        let cid = range.cid_start.saturating_add(offset as u32);
                        // Honor overrides from higher layers in the `/UseCMap` chain.
                        if self.lookup_cid(code, range.len) != Some(cid) {
                            continue;
                        }
                        if let Some(BfString::Char(ch)) = ucs2.lookup_bf_string(cid)
                            && needed.contains(&ch)
                            && self.is_drawable_with(&checker, cid, &ch.to_string())
                        {
                            let raw = encode_code_bytes(code, range.len);
                            let key = ch.to_string();
                            let (replace, is_new) = match map.get(&key) {
                                None => (true, true),
                                Some((existing, _)) => (
                                    raw.len() < existing.len()
                                        || (raw.len() == existing.len() && raw < *existing),
                                    false,
                                ),
                            };
                            if replace {
                                if is_new {
                                    budget.charge(
                                        LimitCode::MemorySize,
                                        64 + key.len() as u64 + raw.len() as u64,
                                    )?;
                                }
                                #[expect(
                                    clippy::disallowed_methods,
                                    reason = "bounded by distinct chars of replacement string"
                                )]
                                map.insert(key, (raw, cid));
                            }
                        }
                    }
                }
                // Fall back to any predefined base CMap referenced by `/UseCMap`
                // for characters not already mapped by the custom stream.
                if let Some(base) = self.base_predefined {
                    let mut remaining: BTreeSet<char> = BTreeSet::new();
                    for &ch in &needed {
                        if !map.contains_key(&ch.to_string()) {
                            #[expect(
                                clippy::disallowed_methods,
                                reason = "subset of distinct chars in caller's replacement string"
                            )]
                            remaining.insert(ch);
                        }
                    }
                    if !remaining.is_empty() {
                        match base {
                            PredefinedBase::Utf16 => {
                                self.probe_predefined_utf16(
                                    &remaining, ucs2, &checker, &mut map, budget,
                                )?;
                            }
                            PredefinedBase::Other => {
                                self.probe_predefined_other(
                                    remaining, ucs2, &checker, &mut map, budget,
                                )?;
                            }
                        }
                    }
                }
            }
            EncodingKind::PredefinedOther => {
                self.probe_predefined_other(needed, ucs2, &checker, &mut map, budget)?;
            }
            EncodingKind::Identity => {}
        }

        Ok(map)
    }

    fn match_bfrange_against_text(
        &self,
        range: &BfRange,
        text: &str,
        needed: &BTreeSet<char>,
        checker: &DrawChecker<'_>,
        map: &mut BTreeMap<String, (Vec<u8>, u32)>,
        budget: &Budget,
    ) -> StoreResult<()> {
        let span = range.end.saturating_sub(range.start);
        match &range.dst {
            BfDest::One(base) => {
                for &ch in needed {
                    let cp = u32::from(ch);
                    if cp <= 0xFFFF && cp >= u32::from(*base) {
                        let offset = cp - u32::from(*base);
                        if offset <= span {
                            self.insert_tounicode_candidate(
                                text,
                                needed,
                                checker,
                                range.start + offset,
                                range.len,
                                ch.to_string(),
                                map,
                                budget,
                            )?;
                        }
                    }
                }
            }
            BfDest::Two(hi, lo) => {
                if (0xD800..=0xDBFF).contains(hi) {
                    for &ch in needed {
                        if u32::from(ch) > 0xFFFF {
                            let mut buf = [0_u16; 2];
                            let _ = ch.encode_utf16(&mut buf);
                            if buf[0] == *hi && buf[1] >= *lo {
                                let offset = u32::from(buf[1] - *lo);
                                if offset <= span {
                                    self.insert_tounicode_candidate(
                                        text,
                                        needed,
                                        checker,
                                        range.start + offset,
                                        range.len,
                                        ch.to_string(),
                                        map,
                                        budget,
                                    )?;
                                }
                            }
                        }
                    }
                } else if let Some(first_ch) = char::from_u32(u32::from(*hi))
                    && needed.contains(&first_ch)
                {
                    let mut chars = text.chars().peekable();
                    while let Some(c) = chars.next() {
                        if c == first_ch
                            && let Some(&next_ch) = chars.peek()
                            && u32::from(next_ch) <= 0xFFFF
                        {
                            let u2 = u32::from(next_ch);
                            if u2 >= u32::from(*lo) {
                                let offset = u2 - u32::from(*lo);
                                if offset <= span {
                                    let mut pair = String::new();
                                    #[expect(
                                        clippy::disallowed_methods,
                                        reason = "two-character string matching a bfrange"
                                    )]
                                    {
                                        pair.push(first_ch);
                                        pair.push(next_ch);
                                    }
                                    self.insert_tounicode_candidate(
                                        text,
                                        needed,
                                        checker,
                                        range.start + offset,
                                        range.len,
                                        pair,
                                        map,
                                        budget,
                                    )?;
                                }
                            }
                        }
                    }
                }
            }
            BfDest::Many(units) => {
                let Some((&base_last, prefix_units)) = units.split_last() else {
                    return Ok(());
                };
                if let Some(&penultimate) = prefix_units.last()
                    && (0xD800..=0xDBFF).contains(&penultimate)
                {
                    let Ok(prefix) = String::from_utf16(&prefix_units[..prefix_units.len() - 1])
                    else {
                        return Ok(());
                    };
                    if let Some(first_ch) = prefix.chars().next()
                        && !needed.contains(&first_ch)
                    {
                        return Ok(());
                    }
                    for (pos, _) in text.match_indices(&prefix) {
                        budget.poll()?;
                        let rest = &text[pos + prefix.len()..];
                        let Some(tail_ch) = rest.chars().next() else {
                            continue;
                        };
                        if u32::from(tail_ch) <= 0xFFFF {
                            continue;
                        }
                        let mut buf = [0_u16; 2];
                        let _ = tail_ch.encode_utf16(&mut buf);
                        if buf[0] == penultimate && buf[1] >= base_last {
                            let offset = u32::from(buf[1] - base_last);
                            if offset <= span {
                                let candidate = &text[pos..pos + prefix.len() + tail_ch.len_utf8()];
                                self.insert_tounicode_candidate(
                                    text,
                                    needed,
                                    checker,
                                    range.start + offset,
                                    range.len,
                                    candidate.to_owned(),
                                    map,
                                    budget,
                                )?;
                            }
                        }
                    }
                } else {
                    let Ok(prefix) = String::from_utf16(prefix_units) else {
                        return Ok(());
                    };
                    if prefix.is_empty()
                        || prefix.chars().next().is_none_or(|c| !needed.contains(&c))
                    {
                        return Ok(());
                    }
                    for (pos, _) in text.match_indices(&prefix) {
                        budget.poll()?;
                        let rest = &text[pos + prefix.len()..];
                        let Some(tail_ch) = rest.chars().next() else {
                            continue;
                        };
                        let cp = u32::from(tail_ch);
                        if cp <= 0xFFFF && cp >= u32::from(base_last) {
                            let offset = cp - u32::from(base_last);
                            if offset <= span {
                                let candidate = &text[pos..pos + prefix.len() + tail_ch.len_utf8()];
                                self.insert_tounicode_candidate(
                                    text,
                                    needed,
                                    checker,
                                    range.start + offset,
                                    range.len,
                                    candidate.to_owned(),
                                    map,
                                    budget,
                                )?;
                            }
                        }
                    }
                }
            }
        }
        Ok(())
    }

    fn probe_predefined_utf16(
        &self,
        needed: &BTreeSet<char>,
        ucs2: &CMap,
        checker: &DrawChecker<'_>,
        map: &mut BTreeMap<String, (Vec<u8>, u32)>,
        budget: &Budget,
    ) -> StoreResult<()> {
        for &ch in needed {
            budget.poll()?;
            let mut utf16 = [0_u16; 2];
            let units = ch.encode_utf16(&mut utf16);
            let (code, len) = if units.len() == 1 {
                (u32::from(units[0]), 2_u8)
            } else {
                ((u32::from(units[0]) << 16) | u32::from(units[1]), 4_u8)
            };
            if let Some(cid) = self.lookup_cid(code, len)
                && self.is_drawable_with(checker, cid, &ch.to_string())
                && matches_ucs2(ucs2, cid, ch)
            {
                let key = ch.to_string();
                let raw = encode_code_bytes(code, len);
                budget.charge(
                    LimitCode::MemorySize,
                    64 + key.len() as u64 + raw.len() as u64,
                )?;
                #[expect(
                    clippy::disallowed_methods,
                    reason = "at most one entry per distinct char in the replacement string"
                )]
                map.insert(key, (raw, cid));
            }
        }
        Ok(())
    }

    fn probe_predefined_other(
        &self,
        mut remaining: BTreeSet<char>,
        ucs2: &CMap,
        checker: &DrawChecker<'_>,
        map: &mut BTreeMap<String, (Vec<u8>, u32)>,
        budget: &Budget,
    ) -> StoreResult<()> {
        // Scan 1-byte codes first (prefers 1-byte ASCII in RKSJ/EUC/GBK/Big5),
        // then 2-byte codes, stopping immediately once every needed char is found.
        for code in 0..=255_u32 {
            if remaining.is_empty() {
                break;
            }
            if let Some(cid) = self.lookup_cid(code, 1)
                && let Some(BfString::Char(ch)) = ucs2.lookup_bf_string(cid)
                && remaining.contains(&ch)
                && self.is_drawable_with(checker, cid, &ch.to_string())
            {
                let key = ch.to_string();
                let raw = encode_code_bytes(code, 1);
                budget.charge(
                    LimitCode::MemorySize,
                    64 + key.len() as u64 + raw.len() as u64,
                )?;
                #[expect(
                    clippy::disallowed_methods,
                    reason = "bounded by distinct chars of replacement string"
                )]
                map.insert(key, (raw, cid));
                remaining.remove(&ch);
            }
        }
        if !remaining.is_empty() {
            budget.charge(LimitCode::InterpretationFuel, 65_536)?;
            for code in 0..=0xFFFF_u32 {
                if remaining.is_empty() {
                    break;
                }
                if code.is_multiple_of(512) {
                    budget.poll()?;
                }
                if let Some(cid) = self.lookup_cid(code, 2)
                    && let Some(BfString::Char(ch)) = ucs2.lookup_bf_string(cid)
                    && remaining.contains(&ch)
                    && self.is_drawable_with(checker, cid, &ch.to_string())
                {
                    let key = ch.to_string();
                    let raw = encode_code_bytes(code, 2);
                    budget.charge(
                        LimitCode::MemorySize,
                        64 + key.len() as u64 + raw.len() as u64,
                    )?;
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "bounded by distinct chars of replacement string"
                    )]
                    map.insert(key, (raw, cid));
                    remaining.remove(&ch);
                }
            }
        }
        Ok(())
    }

    fn can_encode_with_map(&self, text: &str, reverse: &BTreeMap<String, (Vec<u8>, u32)>) -> bool {
        let mut cursor = 0_usize;
        while cursor < text.len() {
            if let Some((step, _)) = longest_match(&text[cursor..], reverse) {
                cursor += step;
            } else {
                return false;
            }
        }
        true
    }

    fn encode_with_map(
        &self,
        text: &str,
        reverse: &BTreeMap<String, (Vec<u8>, u32)>,
        segments: &mut BudgetVec<'_, EncodedSegment>,
        budget: &Budget,
    ) -> StoreResult<()> {
        let mut cursor = 0_usize;
        let mut missing = String::new();
        while cursor < text.len() {
            budget.poll()?;
            if let Some((step, (bytes, cid))) = longest_match(&text[cursor..], reverse) {
                if !missing.is_empty() {
                    segments.try_push(EncodedSegment::Missing {
                        text: std::mem::take(&mut missing),
                    })?;
                }
                let matched = &text[cursor..cursor + step];
                segments.try_push(EncodedSegment::Glyph {
                    bytes: bytes.clone(),
                    text: matched.to_owned(),
                    advance: self.advance_for_cid(*cid),
                    kern_after: 0.0,
                })?;
                cursor += step;
            } else {
                let ch = text[cursor..].chars().next().unwrap_or('\u{FFFD}');
                budget.charge(LimitCode::MemorySize, ch.len_utf8() as u64)?;
                #[expect(
                    clippy::disallowed_methods,
                    reason = "bounded by input text length and charged to MemorySize"
                )]
                missing.push(ch);
                cursor += ch.len_utf8();
            }
        }
        if !missing.is_empty() {
            segments.try_push(EncodedSegment::Missing { text: missing })?;
        }
        Ok(())
    }

    fn encode_with_extension(
        &self,
        text: &str,
        segments: &mut BudgetVec<'_, EncodedSegment>,
        extensions: &mut BudgetVec<'_, GlyphExtension>,
        budget: &Budget,
    ) -> StoreResult<()> {
        let ProgramState::Sfnt {
            editable_face: Some(face),
            num_glyphs,
            ..
        } = &self.program
        else {
            segments.try_push(EncodedSegment::Missing {
                text: text.to_owned(),
            })?;
            return Ok(());
        };
        let Ok(shaper) = ShapingFace::of(face) else {
            segments.try_push(EncodedSegment::Missing {
                text: text.to_owned(),
            })?;
            return Ok(());
        };
        let direction = if self.vertical {
            WritingDirection::Vertical
        } else {
            WritingDirection::Horizontal
        };
        let shaped = match shaper.shape_direction(text, direction, budget) {
            Ok(glyphs) => glyphs,
            Err(super::shape::ShapeError::Limit(limit)) => return Err(StoreError::from(limit)),
            Err(super::shape::ShapeError::Font(_)) => {
                segments.try_push(EncodedSegment::Missing {
                    text: text.to_owned(),
                })?;
                return Ok(());
            }
        };
        let checker = DrawChecker::new(&self.program);
        let measure = Measure::new(face);
        let scale = 1000.0 / f64::from(face.units_per_em);

        // Collect all non-zero glyph IDs that need vertical metrics.
        let mut need_v_gids: BudgetVec<'_, u32> = BudgetVec::new(budget);
        if self.vertical {
            for glyph in shaped.iter() {
                if glyph.glyph != 0
                    && glyph.glyph <= u32::from(u16::MAX)
                    && !need_v_gids.contains(&glyph.glyph)
                {
                    need_v_gids.try_push(glyph.glyph)?;
                }
            }
        }
        let v_metrics_map: BTreeMap<u32, (f64, f64, f64)> = if self.vertical
            && !need_v_gids.is_empty()
        {
            match shaper.vertical_metrics(need_v_gids.as_slice(), budget) {
                Ok(list) => {
                    let mut map = BTreeMap::new();
                    for (&gid, &(v_adv, v_x, v_y)) in need_v_gids.iter().zip(list.iter()) {
                        budget.charge(LimitCode::MemorySize, 48)?;
                        #[expect(
                            clippy::disallowed_methods,
                            reason = "one entry per distinct shaped glyph, bounded by ShapedGlyphCount"
                        )]
                        map.insert(
                            gid,
                            (
                                f64::from(v_adv) * scale,
                                f64::from(v_x) * scale,
                                f64::from(v_y) * scale,
                            ),
                        );
                    }
                    map
                }
                Err(super::shape::ShapeError::Limit(limit)) => {
                    return Err(StoreError::from(limit));
                }
                Err(super::shape::ShapeError::Font(_)) => BTreeMap::new(),
            }
        } else {
            BTreeMap::new()
        };

        let mut planned_gids: BTreeMap<u16, usize> = BTreeMap::new();
        let mut missing_clusters: BudgetVec<'_, Range<usize>> = BudgetVec::new(budget);
        let mut idx = 0_usize;
        while idx < shaped.len() {
            budget.poll()?;
            let cluster = shaped[idx].cluster.clone();
            let mut end = idx + 1;
            while end < shaped.len() && shaped[end].cluster == cluster {
                end += 1;
            }
            let cluster_slice = &shaped[idx..end];
            let cluster_text = &text[cluster.clone()];
            let is_ws = !cluster_text.is_empty() && cluster_text.chars().all(char::is_whitespace);

            let cluster_ok = cluster_slice.iter().all(|g| {
                g.glyph != 0
                    && g.glyph <= u32::from(u16::MAX)
                    && g.glyph < *num_glyphs
                    && (is_ws || checker.sfnt_has_ink(g.glyph))
            });

            if !cluster_ok {
                missing_clusters.try_push(cluster)?;
                idx = end;
                continue;
            }

            flush_missing_clusters(text, &mut missing_clusters, segments, budget)?;

            for g in cluster_slice {
                let gid = g.glyph as u16;
                let cid = u32::from(gid);
                let g_text = &text[g.text.clone()];
                let h_width = measure.advance(GlyphId::new(cid)) * scale;
                let v_entry = v_metrics_map.get(&cid).copied();
                let nominal_advance = if self.vertical {
                    lookup_width2(&self.widths2, cid)
                        .or_else(|| v_entry.map(|(w1y, _, _)| w1y))
                        .unwrap_or(self.dw2_w1y)
                } else {
                    lookup_width(&self.widths, cid).unwrap_or(h_width)
                };
                let shaped_advance = if self.vertical {
                    f64::from(g.y_advance) * scale
                } else {
                    f64::from(g.x_advance) * scale
                };
                // PDF TJ adjustment shifts by -kern / 1000 * Tfs along the writing axis:
                // nominal_advance - kern_after == shaped_advance => kern_after = nominal_advance - shaped_advance.
                let diff = nominal_advance - shaped_advance;
                let kern_after = if diff.abs() > 0.01 {
                    (diff * 1000.0).round() / 1000.0
                } else {
                    0.0
                };

                let existing_unicode = self.code_to_unicode(cid, cid);
                let needs_w = lookup_width(&self.widths, cid).is_none();
                let needs_w2 = self.vertical && lookup_width2(&self.widths2, cid).is_none();
                // Never overwrite an existing non-NO_TEXT mapping for a shared GID.
                let needs_tounicode = !g_text.is_empty()
                    && existing_unicode
                        .as_deref()
                        .is_none_or(|existing| existing == "\u{2060}");
                let planned_text = if needs_tounicode { g_text } else { "" };
                if needs_w || needs_w2 || needs_tounicode {
                    if let Some(&ext_idx) = planned_gids.get(&gid) {
                        // Upgrade an earlier empty-text cluster glyph when a later
                        // occurrence gives this GID real text.
                        if extensions[ext_idx].text.is_empty() && !planned_text.is_empty() {
                            budget.charge(LimitCode::MemorySize, planned_text.len() as u64)?;
                            extensions[ext_idx].text = planned_text.to_owned();
                        }
                    } else {
                        let ext_idx = extensions.len();
                        budget.charge(LimitCode::MemorySize, 32 + planned_text.len() as u64)?;
                        #[expect(
                            clippy::disallowed_methods,
                            reason = "deduplicating shaped u16 glyph IDs bounded by ShapedGlyphCount"
                        )]
                        planned_gids.insert(gid, ext_idx);
                        extensions.try_push(GlyphExtension {
                            gid,
                            width: h_width,
                            v_metrics: if self.vertical { v_entry } else { None },
                            text: planned_text.to_owned(),
                        })?;
                    }
                }

                segments.try_push(EncodedSegment::Glyph {
                    bytes: encode_code_bytes(cid, 2),
                    text: g_text.to_owned(),
                    advance: nominal_advance,
                    kern_after,
                })?;
            }
            idx = end;
        }
        flush_missing_clusters(text, &mut missing_clusters, segments, budget)?;
        Ok(())
    }
}

/// Emits contiguous missing clusters restored to their original logical byte
/// order in `text` so fallback Bidi shaping does not double-reverse RTL runs.
fn flush_missing_clusters(
    text: &str,
    clusters: &mut BudgetVec<'_, Range<usize>>,
    segments: &mut BudgetVec<'_, EncodedSegment>,
    budget: &Budget,
) -> StoreResult<()> {
    if clusters.is_empty() {
        return Ok(());
    }
    clusters
        .as_mut_slice()
        .sort_unstable_by_key(|r| (r.start, r.end));
    let mut total = 0_usize;
    let mut end_seen = 0_usize;
    for r in clusters.iter() {
        let start = r.start.max(end_seen);
        if r.end > start {
            total = total.saturating_add(r.end - start);
            end_seen = r.end;
        }
    }
    budget.charge(LimitCode::MemorySize, total as u64)?;
    let mut out = String::new();
    let mut cursor = 0_usize;
    for r in clusters.iter() {
        let start = r.start.max(cursor);
        if r.end > start {
            #[expect(
                clippy::disallowed_methods,
                reason = "bounded by input text length and charged to MemorySize above"
            )]
            out.push_str(&text[start..r.end]);
            cursor = r.end;
        }
    }
    clusters.clear();
    if !out.is_empty() {
        segments.try_push(EncodedSegment::Missing { text: out })?;
    }
    Ok(())
}

/// Inspects a Type 3 font dictionary in `version`, returning exact PDF text-space
/// advances (`w * FontMatrix[0] * 1000`), per-byte drawability against `/CharProcs`,
/// and any `/ToUnicode` mappings.
pub fn inspect_type3(
    version: &Version,
    font_dict: &Dict,
    budget: &Budget,
) -> StoreResult<Option<Type3Info>> {
    if !is_name(font_dict.get("Subtype"), b"Type3") {
        return Ok(None);
    }

    // FontMatrix (ISO 32000-2 9.6.5, default [0.001 0 0 0.001 0 0]).
    let matrix_a = match resolve_entry(version, font_dict, "FontMatrix", budget)? {
        Some(val) => match &*val {
            Value::Array(arr) if !arr.is_empty() => number_of(arr.get(0)).unwrap_or(0.001),
            _ => 0.001,
        },
        None => 0.001,
    };
    let scale = matrix_a * 1000.0;

    // MissingWidth from FontDescriptor (optional) or 0.
    let missing_width = match resolve_entry(version, font_dict, "FontDescriptor", budget)? {
        Some(val) => match &*val {
            Value::Dict(desc) => number_of(desc.get("MissingWidth")).unwrap_or(0.0),
            _ => 0.0,
        },
        None => 0.0,
    };

    let first_char = int_of(font_dict.get("FirstChar")).unwrap_or(0).max(0) as usize;
    let last_char = int_of(font_dict.get("LastChar")).unwrap_or(-1).min(255);

    let mut widths = [missing_width; 256];
    let mut has_width = [false; 256];
    if let Some(val) = resolve_entry(version, font_dict, "Widths", budget)?
        && let Value::Array(arr) = &*val
    {
        for (i, item) in arr.iter().enumerate() {
            let code = first_char.saturating_add(i);
            if code > 255 || (last_char >= 0 && code > last_char as usize) {
                break;
            }
            if let Some(w) = number_of(Some(item)) {
                widths[code] = w;
                has_width[code] = true;
            }
        }
    }

    // CharProcs dictionary — only resolved Value::Stream entries are drawable.
    let mut char_procs: BTreeMap<Vec<u8>, Option<f64>> = BTreeMap::new();
    if let Some(val) = resolve_entry(version, font_dict, "CharProcs", budget)?
        && let Value::Dict(procs) = &*val
    {
        for (name, value) in procs.iter() {
            budget.poll()?;
            let Some(stream_val) = resolve_value(version, value, budget)? else {
                continue;
            };
            let Value::Stream(stream) = &*stream_val else {
                continue;
            };
            let d0_width = match stream.decoded(budget) {
                Ok(bytes) => parse_type3_d0_width(&bytes),
                Err(err) if err.limit().is_some() => return Err(err),
                Err(_) => None,
            };
            budget.charge(LimitCode::MemorySize, 64 + name.as_bytes().len() as u64)?;
            #[expect(
                clippy::disallowed_methods,
                reason = "bounded by entries of CharProcs dictionary and charged to MemorySize"
            )]
            char_procs.insert(name.as_bytes().to_vec(), d0_width);
        }
    }

    // Encoding -> glyph name per byte code.
    let code_names = resolve_simple_encoding(version, font_dict, budget)?;

    let mut advances = [0.0_f64; 256];
    let mut drawable = [false; 256];
    for code in 0..256_usize {
        let mut raw_width = widths[code];
        if let Some(name) = &code_names[code] {
            if let Some(d0_opt) = char_procs.get(name) {
                drawable[code] = true;
                if !has_width[code]
                    && let Some(d0_w) = d0_opt
                {
                    raw_width = *d0_w;
                }
            } else if name == b"space" && has_width[code] {
                drawable[code] = true;
            }
        }
        advances[code] = raw_width * scale;
    }

    // Optional `/ToUnicode` stream (only 1-byte codes 0..=255 apply to Type 3).
    let mut to_unicode: BudgetVec<'_, (u8, String)> = BudgetVec::new(budget);
    if let Some(val) = resolve_entry(version, font_dict, "ToUnicode", budget)?
        && let Value::Stream(stream) = &*val
    {
        let data = stream.decoded(budget)?;
        let n = data.len() as u64;
        budget.check(LimitCode::FontInputSize, n)?;
        budget.charge(LimitCode::InterpretationFuel, n)?;
        let tables = parse_cmap_tables(&data, true, budget)?;
        let mut slots: [Option<String>; 256] = std::array::from_fn(|_| None);
        for entry in tables.to_unicode_entries {
            budget.poll()?;
            match entry {
                ToUnicodeEntry::Bf(range) => {
                    let end = range.end.min(255);
                    if range.start > end {
                        continue;
                    }
                    for code in range.start..=end {
                        if let Some(text) = range.dst.decode_at(code - range.start)
                            && !text.is_empty()
                            && text != "\0"
                        {
                            let slot = &mut slots[code as usize];
                            let extra = match slot {
                                None => text.len() as u64,
                                Some(prev) => text.len().saturating_sub(prev.len()) as u64,
                            };
                            if extra > 0 {
                                budget.charge(LimitCode::MemorySize, extra)?;
                            }
                            *slot = Some(text);
                        }
                    }
                }
                ToUnicodeEntry::Cid(range) => {
                    let end = range.end.min(255);
                    if range.start > end {
                        continue;
                    }
                    for code in range.start..=end {
                        if let Some(ch) = range
                            .cid_start
                            .checked_add(code - range.start)
                            .and_then(char::from_u32)
                            && ch != '\0'
                        {
                            let text = ch.to_string();
                            let slot = &mut slots[code as usize];
                            let extra = match slot {
                                None => text.len() as u64,
                                Some(prev) => text.len().saturating_sub(prev.len()) as u64,
                            };
                            if extra > 0 {
                                budget.charge(LimitCode::MemorySize, extra)?;
                            }
                            *slot = Some(text);
                        }
                    }
                }
            }
        }
        for (code, slot) in slots.into_iter().enumerate() {
            if let Some(text) = slot {
                to_unicode.try_push((code as u8, text))?;
            }
        }
    }

    Ok(Some(Type3Info {
        advances,
        drawable,
        to_unicode: to_unicode.into_vec(),
    }))
}

// ── Internal helpers ────────────────────────────────────────────────────────

struct LoadedEncoding {
    vertical: bool,
    kind: EncodingKind,
    base_predefined: Option<PredefinedBase>,
    chain: Vec<CMapLayer>,
    codespaces: Vec<CodespaceRange>,
    cid_ranges: Vec<CidMapRange>,
}

fn load_encoding(
    version: &Version,
    val: &Value,
    budget: &Budget,
    depth: usize,
) -> StoreResult<Option<LoadedEncoding>> {
    if depth >= MAX_CMAP_DEPTH {
        return Ok(None);
    }
    match val {
        Value::Name(name) => {
            let cmap_name = CMapName::from_bytes(name.as_bytes());
            match cmap_name {
                CMapName::IdentityH => Ok(Some(LoadedEncoding {
                    vertical: false,
                    kind: EncodingKind::Identity,
                    base_predefined: None,
                    chain: vec![CMapLayer::Parsed(Box::new(CMap::identity_h()))],
                    codespaces: vec![CodespaceRange {
                        len: 2,
                        low: [0x00, 0x00, 0, 0],
                        high: [0xFF, 0xFF, 0, 0],
                    }],
                    cid_ranges: Vec::new(),
                })),
                CMapName::IdentityV => Ok(Some(LoadedEncoding {
                    vertical: true,
                    kind: EncodingKind::Identity,
                    base_predefined: None,
                    chain: vec![CMapLayer::Parsed(Box::new(CMap::identity_v()))],
                    codespaces: vec![CodespaceRange {
                        len: 2,
                        low: [0x00, 0x00, 0, 0],
                        high: [0xFF, 0xFF, 0, 0],
                    }],
                    cid_ranges: Vec::new(),
                })),
                other => {
                    let Some(data) = load_embedded(other) else {
                        return Ok(None);
                    };
                    let Some(cmap) = CMap::parse(data, load_embedded) else {
                        return Ok(None);
                    };
                    let vertical = cmap.metadata().writing_mode == Some(WritingMode::Vertical);
                    let kind = if is_utf16_predefined(other) {
                        EncodingKind::PredefinedUtf16
                    } else {
                        EncodingKind::PredefinedOther
                    };
                    Ok(Some(LoadedEncoding {
                        vertical,
                        kind,
                        base_predefined: None,
                        chain: vec![CMapLayer::Parsed(Box::new(cmap))],
                        codespaces: Vec::new(),
                        cid_ranges: Vec::new(),
                    }))
                }
            }
        }
        Value::Stream(stream) => {
            let data = stream.decoded(budget)?;
            let n = data.len() as u64;
            budget.check(LimitCode::FontInputSize, n)?;
            budget.charge(LimitCode::InterpretationFuel, n)?;
            let tables = parse_cmap_tables(&data, false, budget)?;
            let parsed = CMap::parse(&data, load_embedded);
            let dict_wmode = int_of(stream.dict().get("WMode"));
            let mut vertical = dict_wmode == Some(1)
                || tables.wmode == Some(1)
                || parsed
                    .as_ref()
                    .is_some_and(|c| c.metadata().writing_mode == Some(WritingMode::Vertical));
            let mut chain: BudgetVec<'_, CMapLayer> = BudgetVec::new(budget);
            let mut base_predefined = tables.usecmap_name.as_ref().and_then(|r| {
                let name = CMapName::from_bytes(&data[r.clone()]);
                load_embedded(name).map(|_| {
                    if is_utf16_predefined(name) {
                        PredefinedBase::Utf16
                    } else {
                        PredefinedBase::Other
                    }
                })
            });
            if let Some(cmap) = parsed {
                chain.try_push(CMapLayer::Parsed(Box::new(cmap)))?;
            } else if !tables.cid_ranges.is_empty() || !tables.codespaces.is_empty() {
                // `CMap::parse` returns `None` when a custom stream names a non-predefined
                // `usecmap` in its body; keep its own parsed CID ranges as the top layer.
                chain.try_push(CMapLayer::Tables(tables.cid_ranges.clone()))?;
            }
            let mut codespaces = tables.codespaces;
            let mut cid_ranges = tables.cid_ranges;

            // Follow `/UseCMap` dictionary entry if present (Name or Stream).
            if let Some(base_val) = resolve_entry(version, stream.dict(), "UseCMap", budget)?
                && let Some(base) = load_encoding(version, &base_val, budget, depth + 1)?
            {
                if dict_wmode.is_none() && tables.wmode.is_none() {
                    vertical = base.vertical;
                }
                base_predefined = base_predefined.or(match base.kind {
                    EncodingKind::PredefinedUtf16 => Some(PredefinedBase::Utf16),
                    EncodingKind::PredefinedOther => Some(PredefinedBase::Other),
                    EncodingKind::CustomStream => base.base_predefined,
                    EncodingKind::Identity => None,
                });
                for layer in base.chain {
                    chain.try_push(layer)?;
                }
                #[expect(
                    clippy::disallowed_methods,
                    reason = "bounded by MAX_CMAP_DEPTH and already-budgeted CMap tables"
                )]
                {
                    codespaces.extend(base.codespaces);
                    cid_ranges.extend(base.cid_ranges);
                }
            }
            if chain.is_empty() {
                return Ok(None);
            }
            Ok(Some(LoadedEncoding {
                vertical,
                kind: EncodingKind::CustomStream,
                base_predefined,
                chain: chain.into_vec(),
                codespaces,
                cid_ranges,
            }))
        }
        _ => Ok(None),
    }
}

fn lookup_cid_in_ranges(ranges: &[CidMapRange], code: u32, byte_len: u8) -> Option<u32> {
    ranges
        .iter()
        .rev()
        .find(|r| r.len == byte_len && code >= r.start && code <= r.end)
        .and_then(|r| r.cid_start.checked_add(code - r.start))
}

fn is_utf16_predefined(name: CMapName<'_>) -> bool {
    matches!(
        name,
        CMapName::UniJisUtf16H
            | CMapName::UniJisUtf16V
            | CMapName::UniJisUcs2H
            | CMapName::UniJisUcs2V
            | CMapName::UniJisUcs2HwH
            | CMapName::UniJisUcs2HwV
            | CMapName::UniGbUtf16H
            | CMapName::UniGbUtf16V
            | CMapName::UniGbUcs2H
            | CMapName::UniGbUcs2V
            | CMapName::UniCnsUtf16H
            | CMapName::UniCnsUtf16V
            | CMapName::UniCnsUcs2H
            | CMapName::UniCnsUcs2V
            | CMapName::UniKsUtf16H
            | CMapName::UniKsUtf16V
            | CMapName::UniKsUcs2H
            | CMapName::UniKsUcs2V
    )
}

fn read_cid_system_info(
    version: &Version,
    descendant: &Dict,
    budget: &Budget,
) -> StoreResult<Option<CharacterCollection>> {
    let Some(val) = resolve_entry(version, descendant, "CIDSystemInfo", budget)? else {
        return Ok(None);
    };
    let Value::Dict(info) = &*val else {
        return Ok(None);
    };
    let registry = match info.get("Registry") {
        Some(Value::String(s)) => s.as_bytes(),
        Some(Value::Name(n)) => n.as_bytes(),
        _ => return Ok(None),
    };
    let ordering = match info.get("Ordering") {
        Some(Value::String(s)) => s.as_bytes(),
        Some(Value::Name(n)) => n.as_bytes(),
        _ => return Ok(None),
    };
    let supplement = int_of(info.get("Supplement")).unwrap_or(0) as i32;
    Ok(Some(CharacterCollection {
        family: CidFamily::from_registry_ordering(registry, ordering),
        supplement,
    }))
}

fn load_cid_to_gid(version: &Version, descendant: &Dict, budget: &Budget) -> StoreResult<CidToGid> {
    let Some(val) = resolve_entry(version, descendant, "CIDToGIDMap", budget)? else {
        return Ok(CidToGid::Identity);
    };
    match &*val {
        Value::Name(n) if n.as_bytes() == b"Identity" => Ok(CidToGid::Identity),
        Value::Stream(stream) => {
            let decoded = stream.decoded(budget)?;
            let count = (decoded.len() / 2).min(usize::from(u16::MAX) + 1);
            let mut table: BudgetVec<'_, u16> = BudgetVec::new(budget);
            for &chunk in decoded.as_chunks::<2>().0.iter().take(count) {
                table.try_push(u16::from_be_bytes(chunk))?;
            }
            Ok(CidToGid::Mapped(table.into_vec()))
        }
        _ => Ok(CidToGid::Identity),
    }
}

fn load_program(
    version: &Version,
    descriptor: &Dict,
    cid_is_truetype: bool,
    may_extend: bool,
    budget: &Budget,
) -> StoreResult<(ProgramState, bool)> {
    let candidates = if cid_is_truetype {
        ["FontFile2", "FontFile3", "FontFile"]
    } else {
        ["FontFile3", "FontFile2", "FontFile"]
    };
    let mut found_stream: Option<(&str, Stream)> = None;
    for key in candidates {
        if let Some(val) = resolve_entry(version, descriptor, key, budget)?
            && let Value::Stream(stream) = &*val
        {
            found_stream = Some((key, (**stream).clone()));
            break;
        }
    }
    let Some((key, stream)) = found_stream else {
        return Ok((ProgramState::None, false));
    };
    let is_opentype_stream =
        key == "FontFile3" && is_name(stream.dict().get("Subtype"), b"OpenType");
    let raw = stream.decoded(budget)?;
    budget.check(LimitCode::FontInputSize, raw.len() as u64)?;
    let data = Arc::new(raw);

    // Try SFNT first (TrueType or OpenType CFF).
    if let Ok(font) = FontRef::new(&data)
        && let Ok(maxp) = font.maxp()
    {
        let num_glyphs = u32::from(maxp.num_glyphs());
        // Inspect the ORIGINAL embedded CFF charset BEFORE `Face::read`
        // (which rewrites CFF charsets to identity in memory).
        let (is_cid_keyed_cff, is_identity_cff_charset) = match font
            .table_data(skrifa::Tag::new(b"CFF "))
            .and_then(|t| CffFontRef::new_cff(t.as_bytes(), 0, None).ok())
        {
            Some(cff) if cff.is_cid() => {
                let is_id = check_identity_cff_charset(&cff, num_glyphs, budget)?;
                (true, is_id)
            }
            _ => (false, true),
        };
        let allow_extend = may_extend
            && (!is_cid_keyed_cff || is_identity_cff_charset)
            && (cid_is_truetype || is_opentype_stream);
        let editable_face = if allow_extend {
            Face::read(&data, 0).ok().filter(|f| f.outlines)
        } else {
            None
        };
        return Ok((
            ProgramState::Sfnt {
                data,
                is_cid_keyed_cff,
                is_identity_cff_charset,
                num_glyphs,
                editable_face,
            },
            is_opentype_stream,
        ));
    }

    // Try bare CFF (`CIDFontType0C` or `Type1C`).
    if let Ok(cff) = CffFontRef::new_cff(&data, 0, None) {
        let is_cid_keyed = cff.is_cid();
        return Ok((
            ProgramState::BareCff { data, is_cid_keyed },
            is_opentype_stream,
        ));
    }

    Ok((ProgramState::Other, is_opentype_stream))
}

fn check_identity_cff_charset(
    cff: &CffFontRef<'_>,
    num_glyphs: u32,
    budget: &Budget,
) -> StoreResult<bool> {
    let Some(charset) = cff.charset() else {
        return Ok(false);
    };
    let mut count = 0_u32;
    for (gid, sid) in charset.iter() {
        if count.is_multiple_of(512) {
            budget.poll()?;
        }
        if gid.to_u32() != u32::from(sid.to_u16()) {
            return Ok(false);
        }
        count = count.saturating_add(1);
    }
    Ok(num_glyphs > 0 && count == num_glyphs)
}

fn bare_cff_ref_glyph_has_ink(cff: &CffFontRef<'_>, gid: u32) -> bool {
    let Some(charstring) = cff.charstrings().get(gid as usize) else {
        return false;
    };
    // Empty or bare `endchar` (opcode 14) carries no ink.
    if charstring.is_empty() || charstring == [14] {
        return false;
    }
    let glyph = GlyphId::new(gid);
    let Some(subfont_index) = cff.subfont_index(glyph) else {
        return false;
    };
    let Ok(subfont) = cff.subfont(subfont_index, &[]) else {
        return false;
    };
    let mut pen = HasContourPen::default();
    cff.draw(&subfont, glyph, &[], None, &mut pen).is_ok() && pen.has_ink
}

fn parse_widths(arr: &Array, budget: &Budget) -> StoreResult<Vec<WidthRange>> {
    let mut ranges: BudgetVec<'_, WidthRange> = BudgetVec::new(budget);
    let mut i = 0_usize;
    while i < arr.len() {
        budget.poll()?;
        let Some(first) = int_of(arr.get(i)).and_then(|v| u32::try_from(v).ok()) else {
            break;
        };
        i += 1;
        match arr.get(i) {
            Some(Value::Array(list)) => {
                i += 1;
                for (offset, item) in list.iter().enumerate() {
                    if offset.is_multiple_of(256) {
                        budget.poll()?;
                    }
                    let Some(cid) = first.checked_add(offset as u32) else {
                        break;
                    };
                    if let Some(w) = number_of(Some(item)) {
                        ranges.try_push(WidthRange {
                            start: cid,
                            end: cid,
                            width: w,
                        })?;
                    }
                }
            }
            Some(second_val) => {
                let Some(last) = int_of(Some(second_val)).and_then(|v| u32::try_from(v).ok())
                else {
                    break;
                };
                let Some(w) = number_of(arr.get(i + 1)) else {
                    break;
                };
                i += 2;
                if first <= last {
                    ranges.try_push(WidthRange {
                        start: first,
                        end: last,
                        width: w,
                    })?;
                }
            }
            None => break,
        }
    }
    Ok(ranges.into_vec())
}

fn parse_widths2(arr: &Array, budget: &Budget) -> StoreResult<Vec<Width2Range>> {
    let mut ranges: BudgetVec<'_, Width2Range> = BudgetVec::new(budget);
    let mut i = 0_usize;
    while i < arr.len() {
        budget.poll()?;
        let Some(first) = int_of(arr.get(i)).and_then(|v| u32::try_from(v).ok()) else {
            break;
        };
        i += 1;
        match arr.get(i) {
            Some(Value::Array(list)) => {
                i += 1;
                let mut pos = 0_usize;
                let mut offset = 0_u32;
                while pos + 2 < list.len() {
                    if offset.is_multiple_of(256) {
                        budget.poll()?;
                    }
                    let Some(cid) = first.checked_add(offset) else {
                        break;
                    };
                    if let Some(w1y) = number_of(list.get(pos)) {
                        ranges.try_push(Width2Range {
                            start: cid,
                            end: cid,
                            w1y,
                        })?;
                    }
                    pos += 3;
                    offset += 1;
                }
            }
            Some(second_val) => {
                let Some(last) = int_of(Some(second_val)).and_then(|v| u32::try_from(v).ok())
                else {
                    break;
                };
                let Some(w1y) = number_of(arr.get(i + 1)) else {
                    break;
                };
                if arr.get(i + 3).is_none() {
                    break;
                }
                i += 4;
                if first <= last {
                    ranges.try_push(Width2Range {
                        start: first,
                        end: last,
                        w1y,
                    })?;
                }
            }
            None => break,
        }
    }
    Ok(ranges.into_vec())
}

fn lookup_width(ranges: &[WidthRange], cid: u32) -> Option<f64> {
    ranges
        .iter()
        .rev()
        .find(|r| cid >= r.start && cid <= r.end)
        .map(|r| r.width)
}

fn lookup_width2(ranges: &[Width2Range], cid: u32) -> Option<f64> {
    ranges
        .iter()
        .rev()
        .find(|r| cid >= r.start && cid <= r.end)
        .map(|r| r.w1y)
}

/// Parses `begincodespacerange`, `begincidchar`, `begincidrange`,
/// `beginbfchar`, `beginbfrange`, `/WMode`, and `usecmap` from a PDF CMap stream.
fn parse_cmap_tables(
    data: &[u8],
    collect_tounicode: bool,
    budget: &Budget,
) -> StoreResult<ParsedCMapTables> {
    let mut codespaces: BudgetVec<'_, CodespaceRange> = BudgetVec::new(budget);
    let mut cid_ranges: BudgetVec<'_, CidMapRange> = BudgetVec::new(budget);
    let mut to_unicode_entries: BudgetVec<'_, ToUnicodeEntry> = BudgetVec::new(budget);
    let mut wmode = None;
    let mut usecmap_name: Option<Range<usize>> = None;
    let mut last_name: Option<Range<usize>> = None;

    let mut input = SyntaxReader::new(data);
    while !input.at_end() {
        budget.poll()?;
        input.skip_white_spaces_and_comments();
        if input.at_end() {
            break;
        }
        if matches!(
            input.peek_byte(),
            Some(b'/' | b'<' | b'(' | b'[' | b'.' | b'+' | b'-' | b'0'..=b'9')
        ) {
            let before = input.offset();
            let Some(object) = input.read_without_context::<Object<'_>>() else {
                // Non-PDF PostScript token (e.g. `.notdef`, bare `/`, `+`):
                // advance one byte and keep scanning instead of truncating.
                if input.offset() == before {
                    input.forward();
                }
                last_name = None;
                continue;
            };
            if let Object::Name(name) = object {
                let raw_name = (before + 1)..input.offset();
                last_name = Some(raw_name);
                if name.as_ref() == b"WMode" {
                    input.skip_white_spaces_and_comments();
                    if let Some(v) = input.read_without_context::<i32>()
                        && (v == 0 || v == 1)
                    {
                        wmode = Some(v as u8);
                    }
                    last_name = None;
                }
            } else {
                last_name = None;
            }
            continue;
        }
        let start = input.offset();
        input.forward_while(|b| !b.is_ascii_whitespace() && !b"()<>[]{}/%\0".contains(&b));
        if input.offset() == start {
            input.forward();
            last_name = None;
            continue;
        }
        let keyword = &data[start..input.offset()];
        match keyword {
            b"usecmap" => {
                if let Some(name_range) = last_name.take() {
                    usecmap_name = Some(name_range);
                }
            }
            b"begincodespacerange" => {
                last_name = None;
                parse_codespace_block(&mut input, data, &mut codespaces, budget)?;
            }
            b"begincidchar" => {
                last_name = None;
                parse_cidchar_block(
                    &mut input,
                    data,
                    &mut cid_ranges,
                    collect_tounicode.then_some(&mut to_unicode_entries),
                    budget,
                )?;
            }
            b"begincidrange" => {
                last_name = None;
                parse_cidrange_block(
                    &mut input,
                    data,
                    &mut cid_ranges,
                    collect_tounicode.then_some(&mut to_unicode_entries),
                    budget,
                )?;
            }
            b"beginbfchar" => {
                last_name = None;
                parse_bfchar_block(&mut input, data, &mut to_unicode_entries, budget)?;
            }
            b"beginbfrange" => {
                last_name = None;
                parse_bfrange_block(&mut input, data, &mut to_unicode_entries, budget)?;
            }
            _ => {
                last_name = None;
            }
        }
    }

    Ok(ParsedCMapTables {
        codespaces: codespaces.into_vec(),
        cid_ranges: cid_ranges.into_vec(),
        to_unicode_entries: to_unicode_entries.into_vec(),
        wmode,
        usecmap_name,
    })
}

fn read_cmap_code_bytes(input: &mut SyntaxReader<'_>) -> Option<(u8, [u8; 4])> {
    input.skip_white_spaces_and_comments();
    let s = input.read_without_context::<SyntaxString>()?;
    let bytes = s.as_bytes();
    if !(1..=4).contains(&bytes.len()) {
        return None;
    }
    let mut buf = [0_u8; 4];
    buf[..bytes.len()].copy_from_slice(bytes);
    Some((bytes.len() as u8, buf))
}

fn read_cmap_code(input: &mut SyntaxReader<'_>) -> Option<(u8, u32)> {
    let (len, buf) = read_cmap_code_bytes(input)?;
    let val = buf[..usize::from(len)]
        .iter()
        .fold(0_u32, |acc, &b| (acc << 8) | u32::from(b));
    Some((len, val))
}

fn at_keyword(input: &mut SyntaxReader<'_>, data: &[u8], keyword: &[u8]) -> bool {
    input.skip_white_spaces_and_comments();
    let saved = input.clone();
    let start = input.offset();
    input.forward_while(|b| !b.is_ascii_whitespace() && !b"()<>[]{}/%\0".contains(&b));
    if &data[start..input.offset()] == keyword {
        true
    } else {
        *input = saved;
        false
    }
}

fn parse_codespace_block(
    input: &mut SyntaxReader<'_>,
    data: &[u8],
    out: &mut BudgetVec<'_, CodespaceRange>,
    budget: &Budget,
) -> StoreResult<()> {
    while !input.at_end() {
        budget.poll()?;
        if at_keyword(input, data, b"endcodespacerange") {
            break;
        }
        let Some((len1, low)) = read_cmap_code_bytes(input) else {
            break;
        };
        let Some((len2, high)) = read_cmap_code_bytes(input) else {
            break;
        };
        if len1 == len2 && (0..usize::from(len1)).all(|i| low[i] <= high[i]) {
            out.try_push(CodespaceRange {
                len: len1,
                low,
                high,
            })?;
        }
    }
    Ok(())
}

fn parse_cidchar_block(
    input: &mut SyntaxReader<'_>,
    data: &[u8],
    out: &mut BudgetVec<'_, CidMapRange>,
    mut to_unicode: Option<&mut BudgetVec<'_, ToUnicodeEntry>>,
    budget: &Budget,
) -> StoreResult<()> {
    while !input.at_end() {
        budget.poll()?;
        if at_keyword(input, data, b"endcidchar") {
            break;
        }
        let Some((len, code)) = read_cmap_code(input) else {
            break;
        };
        input.skip_white_spaces_and_comments();
        let Some(cid) = input
            .read_without_context::<i32>()
            .and_then(|v| u32::try_from(v).ok())
        else {
            break;
        };
        let range = CidMapRange {
            len,
            start: code,
            end: code,
            cid_start: cid,
        };
        out.try_push(range)?;
        if let Some(tu) = to_unicode.as_deref_mut() {
            tu.try_push(ToUnicodeEntry::Cid(range))?;
        }
    }
    Ok(())
}

fn parse_cidrange_block(
    input: &mut SyntaxReader<'_>,
    data: &[u8],
    out: &mut BudgetVec<'_, CidMapRange>,
    mut to_unicode: Option<&mut BudgetVec<'_, ToUnicodeEntry>>,
    budget: &Budget,
) -> StoreResult<()> {
    while !input.at_end() {
        budget.poll()?;
        if at_keyword(input, data, b"endcidrange") {
            break;
        }
        let Some((len1, start)) = read_cmap_code(input) else {
            break;
        };
        let Some((len2, end)) = read_cmap_code(input) else {
            break;
        };
        input.skip_white_spaces_and_comments();
        let Some(cid_start) = input
            .read_without_context::<i32>()
            .and_then(|v| u32::try_from(v).ok())
        else {
            break;
        };
        if len1 == len2 && start <= end {
            let range = CidMapRange {
                len: len1,
                start,
                end,
                cid_start,
            };
            out.try_push(range)?;
            if let Some(tu) = to_unicode.as_deref_mut() {
                tu.try_push(ToUnicodeEntry::Cid(range))?;
            }
        }
    }
    Ok(())
}

fn parse_bfchar_block(
    input: &mut SyntaxReader<'_>,
    data: &[u8],
    out: &mut BudgetVec<'_, ToUnicodeEntry>,
    budget: &Budget,
) -> StoreResult<()> {
    while !input.at_end() {
        budget.poll()?;
        if at_keyword(input, data, b"endbfchar") {
            break;
        }
        let Some((len, code)) = read_cmap_code(input) else {
            break;
        };
        input.skip_white_spaces_and_comments();
        let Some(dst_str) = input.read_without_context::<SyntaxString>() else {
            break;
        };
        if let Some(dst) = BfDest::from_be_bytes(dst_str.as_bytes(), budget)? {
            out.try_push(ToUnicodeEntry::Bf(BfRange {
                len,
                start: code,
                end: code,
                dst,
            }))?;
        }
    }
    Ok(())
}

fn parse_bfrange_block(
    input: &mut SyntaxReader<'_>,
    data: &[u8],
    out: &mut BudgetVec<'_, ToUnicodeEntry>,
    budget: &Budget,
) -> StoreResult<()> {
    while !input.at_end() {
        budget.poll()?;
        if at_keyword(input, data, b"endbfrange") {
            break;
        }
        let Some((len1, start)) = read_cmap_code(input) else {
            break;
        };
        let Some((len2, end)) = read_cmap_code(input) else {
            break;
        };
        input.skip_white_spaces_and_comments();
        if len1 != len2 || start > end {
            // Consume the destination operand before continuing.
            let _ = input.read_without_context::<Object<'_>>();
            continue;
        }
        let count = u64::from(end - start) + 1;
        if input.peek_byte() == Some(b'[') {
            let Some(Object::Array(arr)) = input.read_without_context::<Object<'_>>() else {
                break;
            };
            for (i, s) in arr.iter::<SyntaxString>().enumerate() {
                let offset = i as u64;
                if offset >= count {
                    break;
                }
                if i.is_multiple_of(256) {
                    budget.poll()?;
                }
                if let Some(dst) = BfDest::from_be_bytes(s.as_bytes(), budget)? {
                    out.try_push(ToUnicodeEntry::Bf(BfRange {
                        len: len1,
                        start: start + i as u32,
                        end: start + i as u32,
                        dst,
                    }))?;
                }
            }
        } else {
            let Some(dst_str) = input.read_without_context::<SyntaxString>() else {
                break;
            };
            // Store compact range without eager expansion (F6).
            if let Some(dst) = BfDest::from_be_bytes(dst_str.as_bytes(), budget)? {
                out.try_push(ToUnicodeEntry::Bf(BfRange {
                    len: len1,
                    start,
                    end,
                    dst,
                }))?;
            }
        }
    }
    Ok(())
}

/// Decodes big-endian UTF-16 code units from a CMap `bf` string, zero-padding
/// an odd leading byte to match `hayro-cmap`'s `decode_be`.
fn utf16be_units(bytes: &[u8], budget: &Budget) -> StoreResult<Option<Vec<u16>>> {
    if bytes.is_empty() {
        return Ok(None);
    }
    let mut out: BudgetVec<'_, u16> = BudgetVec::new(budget);
    out.try_reserve_exact(bytes.len().div_ceil(2))?;
    let mut i = 0_usize;
    if !bytes.len().is_multiple_of(2) {
        out.try_push(u16::from(bytes[0]))?;
        i = 1;
    }
    while i + 1 < bytes.len() {
        out.try_push(u16::from_be_bytes([bytes[i], bytes[i + 1]]))?;
        i += 2;
    }
    Ok(Some(out.into_vec()))
}

fn bf_to_string(bf: BfString) -> String {
    match bf {
        BfString::Char(ch) => ch.to_string(),
        BfString::String(s) => s,
    }
}

fn matches_ucs2(ucs2: &CMap, cid: u32, ch: char) -> bool {
    matches!(ucs2.lookup_bf_string(cid), Some(BfString::Char(c)) if c == ch)
}

fn encode_code_bytes(code: u32, byte_len: u8) -> Vec<u8> {
    let be = code.to_be_bytes();
    let len = usize::from(byte_len).clamp(1, 4);
    #[expect(clippy::disallowed_methods, reason = "1 to 4 bytes")]
    be[4 - len..].to_vec()
}

fn longest_match<'a, V>(text: &str, map: &'a BTreeMap<String, V>) -> Option<(usize, &'a V)> {
    // Collect byte boundaries of up to MAX_LIGATURE_CHARS characters.
    let mut ends = [0_usize; MAX_LIGATURE_CHARS];
    let mut count = 0_usize;
    for (byte_idx, ch) in text.char_indices() {
        if count >= MAX_LIGATURE_CHARS {
            break;
        }
        ends[count] = byte_idx + ch.len_utf8();
        count += 1;
    }
    for &end in ends[..count].iter().rev() {
        if let Some(val) = map.get(&text[..end]) {
            return Some((end, val));
        }
    }
    None
}

/// Returns whether `name` begins with an ISO 32000-2 9.6.4 subset tag:
/// six uppercase ASCII letters followed by `+`.
fn has_subset_tag(name: &[u8]) -> bool {
    name.len() > 7 && name[6] == b'+' && name[..6].iter().all(u8::is_ascii_uppercase)
}

/// Extracts the horizontal width (`wx`) from the leading `d0` or `d1` operator
/// of a Type 3 glyph `CharProc` stream (ISO 32000-2 9.6.5).
fn parse_type3_d0_width(data: &[u8]) -> Option<f64> {
    let slice = &data[..data.len().min(MAX_TYPE3_SCAN_BYTES)];
    let mut input = SyntaxReader::new(slice);
    let mut nums = [0.0_f64; 8];
    let mut count = 0_usize;
    while !input.at_end() {
        input.skip_white_spaces_and_comments();
        if input.at_end() {
            break;
        }
        if matches!(input.peek_byte(), Some(b'+' | b'-' | b'.' | b'0'..=b'9')) {
            let num = input.read_without_context::<f64>()?;
            if count < nums.len() {
                nums[count] = num;
                count += 1;
            }
            continue;
        }
        let start = input.offset();
        input.forward_while(|b| !b.is_ascii_whitespace() && !b"()<>[]{}/%\0".contains(&b));
        if input.offset() == start {
            return None;
        }
        let op = &slice[start..input.offset()];
        if op == b"d0" && count >= 2 {
            return Some(nums[count - 2]);
        }
        if op == b"d1" && count >= 6 {
            return Some(nums[count - 6]);
        }
        return None;
    }
    None
}

/// Resolves a simple/Type 3 `/Encoding` (Name or Dict with `/BaseEncoding` and
/// `/Differences`) to per-byte glyph names.
fn resolve_simple_encoding(
    version: &Version,
    font_dict: &Dict,
    budget: &Budget,
) -> StoreResult<[Option<Vec<u8>>; 256]> {
    let mut names: [Option<Vec<u8>>; 256] = std::array::from_fn(|_| None);
    let Some(enc_val) = resolve_entry(version, font_dict, "Encoding", budget)? else {
        return Ok(names);
    };
    match &*enc_val {
        Value::Name(base) => apply_base_encoding(base.as_bytes(), &mut names),
        Value::Dict(dict) => {
            if let Some(base_val) = resolve_entry(version, dict, "BaseEncoding", budget)?
                && let Value::Name(base) = &*base_val
            {
                apply_base_encoding(base.as_bytes(), &mut names);
            }
            if let Some(diffs_val) = resolve_entry(version, dict, "Differences", budget)?
                && let Value::Array(diffs) = &*diffs_val
            {
                let mut code = 0_usize;
                for item in diffs.iter() {
                    budget.poll()?;
                    match item {
                        Value::Integer(n) => {
                            if *n >= 0 {
                                code = (*n as usize).min(256);
                            }
                        }
                        Value::Name(n) if code < 256 => {
                            #[expect(
                                clippy::disallowed_methods,
                                reason = "at most 256 encoding slots"
                            )]
                            {
                                names[code] = Some(n.as_bytes().to_vec());
                            }
                            code += 1;
                        }
                        _ => {}
                    }
                }
            }
        }
        _ => {}
    }
    Ok(names)
}

/// Populates standard PDF simple base encodings (ISO 32000-2 Annex D) for
/// Type 3 `/Encoding` lookup.
fn apply_base_encoding(base: &[u8], names: &mut [Option<Vec<u8>>; 256]) {
    let table: Option<&[Option<&str>; 256]> = match base {
        b"WinAnsiEncoding" => Some(&WINANSI_NAMES),
        b"StandardEncoding" => Some(&STANDARD_NAMES),
        b"MacRomanEncoding" => Some(&MACROMAN_NAMES),
        _ => None,
    };
    if let Some(table) = table {
        for (code, name) in table.iter().enumerate() {
            if let Some(glyph) = name {
                #[expect(clippy::disallowed_methods, reason = "fixed 256-slot table")]
                {
                    names[code] = Some(glyph.as_bytes().to_vec());
                }
            }
        }
    }
}

/// StandardEncoding glyph names (ISO 32000-2 Annex D, Table D.2) for bytes 0..256.
const STANDARD_NAMES: [Option<&str>; 256] = [
    // 0x00..0x1F
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    // 0x20..0x3F
    Some("space"),
    Some("exclam"),
    Some("quotedbl"),
    Some("numbersign"),
    Some("dollar"),
    Some("percent"),
    Some("ampersand"),
    Some("quoteright"),
    Some("parenleft"),
    Some("parenright"),
    Some("asterisk"),
    Some("plus"),
    Some("comma"),
    Some("hyphen"),
    Some("period"),
    Some("slash"),
    Some("zero"),
    Some("one"),
    Some("two"),
    Some("three"),
    Some("four"),
    Some("five"),
    Some("six"),
    Some("seven"),
    Some("eight"),
    Some("nine"),
    Some("colon"),
    Some("semicolon"),
    Some("less"),
    Some("equal"),
    Some("greater"),
    Some("question"),
    // 0x40..0x5F
    Some("at"),
    Some("A"),
    Some("B"),
    Some("C"),
    Some("D"),
    Some("E"),
    Some("F"),
    Some("G"),
    Some("H"),
    Some("I"),
    Some("J"),
    Some("K"),
    Some("L"),
    Some("M"),
    Some("N"),
    Some("O"),
    Some("P"),
    Some("Q"),
    Some("R"),
    Some("S"),
    Some("T"),
    Some("U"),
    Some("V"),
    Some("W"),
    Some("X"),
    Some("Y"),
    Some("Z"),
    Some("bracketleft"),
    Some("backslash"),
    Some("bracketright"),
    Some("asciicircum"),
    Some("underscore"),
    // 0x60..0x7F
    Some("quoteleft"),
    Some("a"),
    Some("b"),
    Some("c"),
    Some("d"),
    Some("e"),
    Some("f"),
    Some("g"),
    Some("h"),
    Some("i"),
    Some("j"),
    Some("k"),
    Some("l"),
    Some("m"),
    Some("n"),
    Some("o"),
    Some("p"),
    Some("q"),
    Some("r"),
    Some("s"),
    Some("t"),
    Some("u"),
    Some("v"),
    Some("w"),
    Some("x"),
    Some("y"),
    Some("z"),
    Some("braceleft"),
    Some("bar"),
    Some("braceright"),
    Some("asciitilde"),
    None,
    // 0x80..0x9F
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    // 0xA0..0xBF
    None,
    Some("exclamdown"),
    Some("cent"),
    Some("sterling"),
    Some("fraction"),
    Some("yen"),
    Some("florin"),
    Some("section"),
    Some("currency"),
    Some("quotesingle"),
    Some("quotedblleft"),
    Some("guillemotleft"),
    Some("guilsinglleft"),
    Some("guilsinglright"),
    Some("fi"),
    Some("fl"),
    None,
    Some("endash"),
    Some("dagger"),
    Some("daggerdbl"),
    Some("periodcentered"),
    None,
    Some("paragraph"),
    Some("bullet"),
    Some("quotesinglbase"),
    Some("quotedblbase"),
    Some("quotedblright"),
    Some("guillemotright"),
    Some("ellipsis"),
    Some("perthousand"),
    None,
    Some("questiondown"),
    // 0xC0..0xDF
    None,
    Some("grave"),
    Some("acute"),
    Some("circumflex"),
    Some("tilde"),
    Some("macron"),
    Some("breve"),
    Some("dotaccent"),
    Some("dieresis"),
    None,
    Some("ring"),
    Some("cedilla"),
    None,
    Some("hungarumlaut"),
    Some("ogonek"),
    Some("caron"),
    Some("emdash"),
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    // 0xE0..0xFF
    None,
    Some("AE"),
    None,
    Some("ordfeminine"),
    None,
    None,
    None,
    None,
    Some("Lslash"),
    Some("Oslash"),
    Some("OE"),
    Some("ordmasculine"),
    None,
    None,
    None,
    None,
    None,
    Some("ae"),
    None,
    None,
    None,
    Some("dotlessi"),
    None,
    None,
    Some("lslash"),
    Some("oslash"),
    Some("oe"),
    Some("germandbls"),
    None,
    None,
    None,
    None,
];

/// MacRomanEncoding glyph names (ISO 32000-2 Annex D, Table D.2) for bytes 0..256.
const MACROMAN_NAMES: [Option<&str>; 256] = [
    // 0x00..0x1F
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    // 0x20..0x3F
    Some("space"),
    Some("exclam"),
    Some("quotedbl"),
    Some("numbersign"),
    Some("dollar"),
    Some("percent"),
    Some("ampersand"),
    Some("quotesingle"),
    Some("parenleft"),
    Some("parenright"),
    Some("asterisk"),
    Some("plus"),
    Some("comma"),
    Some("hyphen"),
    Some("period"),
    Some("slash"),
    Some("zero"),
    Some("one"),
    Some("two"),
    Some("three"),
    Some("four"),
    Some("five"),
    Some("six"),
    Some("seven"),
    Some("eight"),
    Some("nine"),
    Some("colon"),
    Some("semicolon"),
    Some("less"),
    Some("equal"),
    Some("greater"),
    Some("question"),
    // 0x40..0x5F
    Some("at"),
    Some("A"),
    Some("B"),
    Some("C"),
    Some("D"),
    Some("E"),
    Some("F"),
    Some("G"),
    Some("H"),
    Some("I"),
    Some("J"),
    Some("K"),
    Some("L"),
    Some("M"),
    Some("N"),
    Some("O"),
    Some("P"),
    Some("Q"),
    Some("R"),
    Some("S"),
    Some("T"),
    Some("U"),
    Some("V"),
    Some("W"),
    Some("X"),
    Some("Y"),
    Some("Z"),
    Some("bracketleft"),
    Some("backslash"),
    Some("bracketright"),
    Some("asciicircum"),
    Some("underscore"),
    // 0x60..0x7F
    Some("grave"),
    Some("a"),
    Some("b"),
    Some("c"),
    Some("d"),
    Some("e"),
    Some("f"),
    Some("g"),
    Some("h"),
    Some("i"),
    Some("j"),
    Some("k"),
    Some("l"),
    Some("m"),
    Some("n"),
    Some("o"),
    Some("p"),
    Some("q"),
    Some("r"),
    Some("s"),
    Some("t"),
    Some("u"),
    Some("v"),
    Some("w"),
    Some("x"),
    Some("y"),
    Some("z"),
    Some("braceleft"),
    Some("bar"),
    Some("braceright"),
    Some("asciitilde"),
    None,
    // 0x80..0x9F
    Some("Adieresis"),
    Some("Aring"),
    Some("Ccedilla"),
    Some("Eacute"),
    Some("Ntilde"),
    Some("Odieresis"),
    Some("Udieresis"),
    Some("aacute"),
    Some("agrave"),
    Some("acircumflex"),
    Some("adieresis"),
    Some("atilde"),
    Some("aring"),
    Some("ccedilla"),
    Some("eacute"),
    Some("egrave"),
    Some("ecircumflex"),
    Some("edieresis"),
    Some("iacute"),
    Some("igrave"),
    Some("icircumflex"),
    Some("idieresis"),
    Some("ntilde"),
    Some("oacute"),
    Some("ograve"),
    Some("ocircumflex"),
    Some("odieresis"),
    Some("otilde"),
    Some("uacute"),
    Some("ugrave"),
    Some("ucircumflex"),
    Some("udieresis"),
    // 0xA0..0xBF
    Some("dagger"),
    Some("degree"),
    Some("cent"),
    Some("sterling"),
    Some("section"),
    Some("bullet"),
    Some("paragraph"),
    Some("germandbls"),
    Some("registered"),
    Some("copyright"),
    Some("trademark"),
    Some("acute"),
    Some("dieresis"),
    None,
    Some("AE"),
    Some("Oslash"),
    None,
    Some("plusminus"),
    None,
    None,
    Some("yen"),
    Some("mu"),
    None,
    None,
    None,
    None,
    None,
    Some("ordfeminine"),
    Some("ordmasculine"),
    None,
    Some("ae"),
    Some("oslash"),
    // 0xC0..0xDF
    Some("questiondown"),
    Some("exclamdown"),
    Some("logicalnot"),
    None,
    Some("florin"),
    None,
    None,
    Some("guillemotleft"),
    Some("guillemotright"),
    Some("ellipsis"),
    Some("space"),
    Some("Agrave"),
    Some("Atilde"),
    Some("Otilde"),
    Some("OE"),
    Some("oe"),
    Some("endash"),
    Some("emdash"),
    Some("quotedblleft"),
    Some("quotedblright"),
    Some("quoteleft"),
    Some("quoteright"),
    Some("divide"),
    None,
    Some("ydieresis"),
    Some("Ydieresis"),
    Some("fraction"),
    Some("currency"),
    Some("guilsinglleft"),
    Some("guilsinglright"),
    Some("fi"),
    Some("fl"),
    // 0xE0..0xFF
    Some("daggerdbl"),
    Some("periodcentered"),
    Some("quotesinglbase"),
    Some("quotedblbase"),
    Some("perthousand"),
    Some("Acircumflex"),
    Some("Ecircumflex"),
    Some("Aacute"),
    Some("Edieresis"),
    Some("Egrave"),
    Some("Iacute"),
    Some("Icircumflex"),
    Some("Idieresis"),
    Some("Igrave"),
    Some("Oacute"),
    Some("Ocircumflex"),
    None,
    Some("Ograve"),
    Some("Uacute"),
    Some("Ucircumflex"),
    Some("Ugrave"),
    Some("dotlessi"),
    Some("circumflex"),
    Some("tilde"),
    Some("macron"),
    Some("breve"),
    Some("dotaccent"),
    Some("ring"),
    Some("cedilla"),
    Some("hungarumlaut"),
    Some("ogonek"),
    Some("caron"),
];

/// WinAnsiEncoding glyph names (ISO 32000-2 Annex D) for bytes 0..256.
const WINANSI_NAMES: [Option<&str>; 256] = [
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    Some("space"),
    Some("exclam"),
    Some("quotedbl"),
    Some("numbersign"),
    Some("dollar"),
    Some("percent"),
    Some("ampersand"),
    Some("quotesingle"),
    Some("parenleft"),
    Some("parenright"),
    Some("asterisk"),
    Some("plus"),
    Some("comma"),
    Some("hyphen"),
    Some("period"),
    Some("slash"),
    Some("zero"),
    Some("one"),
    Some("two"),
    Some("three"),
    Some("four"),
    Some("five"),
    Some("six"),
    Some("seven"),
    Some("eight"),
    Some("nine"),
    Some("colon"),
    Some("semicolon"),
    Some("less"),
    Some("equal"),
    Some("greater"),
    Some("question"),
    Some("at"),
    Some("A"),
    Some("B"),
    Some("C"),
    Some("D"),
    Some("E"),
    Some("F"),
    Some("G"),
    Some("H"),
    Some("I"),
    Some("J"),
    Some("K"),
    Some("L"),
    Some("M"),
    Some("N"),
    Some("O"),
    Some("P"),
    Some("Q"),
    Some("R"),
    Some("S"),
    Some("T"),
    Some("U"),
    Some("V"),
    Some("W"),
    Some("X"),
    Some("Y"),
    Some("Z"),
    Some("bracketleft"),
    Some("backslash"),
    Some("bracketright"),
    Some("asciicircum"),
    Some("underscore"),
    Some("grave"),
    Some("a"),
    Some("b"),
    Some("c"),
    Some("d"),
    Some("e"),
    Some("f"),
    Some("g"),
    Some("h"),
    Some("i"),
    Some("j"),
    Some("k"),
    Some("l"),
    Some("m"),
    Some("n"),
    Some("o"),
    Some("p"),
    Some("q"),
    Some("r"),
    Some("s"),
    Some("t"),
    Some("u"),
    Some("v"),
    Some("w"),
    Some("x"),
    Some("y"),
    Some("z"),
    Some("braceleft"),
    Some("bar"),
    Some("braceright"),
    Some("asciitilde"),
    None,
    Some("Euro"),
    None,
    Some("quotesinglbase"),
    Some("florin"),
    Some("quotedblbase"),
    Some("ellipsis"),
    Some("dagger"),
    Some("daggerdbl"),
    Some("circumflex"),
    Some("perthousand"),
    Some("Scaron"),
    Some("guilsinglleft"),
    Some("OE"),
    None,
    Some("Zcaron"),
    None,
    None,
    Some("quoteleft"),
    Some("quoteright"),
    Some("quotedblleft"),
    Some("quotedblright"),
    Some("bullet"),
    Some("endash"),
    Some("emdash"),
    Some("tilde"),
    Some("trademark"),
    Some("scaron"),
    Some("guilsinglright"),
    Some("oe"),
    None,
    Some("zcaron"),
    Some("Ydieresis"),
    Some("space"),
    Some("exclamdown"),
    Some("cent"),
    Some("sterling"),
    Some("currency"),
    Some("yen"),
    Some("brokenbar"),
    Some("section"),
    Some("dieresis"),
    Some("copyright"),
    Some("ordfeminine"),
    Some("guillemotleft"),
    Some("logicalnot"),
    Some("hyphen"),
    Some("registered"),
    Some("macron"),
    Some("degree"),
    Some("plusminus"),
    Some("twosuperior"),
    Some("threesuperior"),
    Some("acute"),
    Some("mu"),
    Some("paragraph"),
    Some("periodcentered"),
    Some("cedilla"),
    Some("onesuperior"),
    Some("ordmasculine"),
    Some("guillemotright"),
    Some("onequarter"),
    Some("onehalf"),
    Some("threequarters"),
    Some("questiondown"),
    Some("Agrave"),
    Some("Aacute"),
    Some("Acircumflex"),
    Some("Atilde"),
    Some("Adieresis"),
    Some("Aring"),
    Some("AE"),
    Some("Ccedilla"),
    Some("Egrave"),
    Some("Eacute"),
    Some("Ecircumflex"),
    Some("Edieresis"),
    Some("Igrave"),
    Some("Iacute"),
    Some("Icircumflex"),
    Some("Idieresis"),
    Some("Eth"),
    Some("Ntilde"),
    Some("Ograve"),
    Some("Oacute"),
    Some("Ocircumflex"),
    Some("Otilde"),
    Some("Odieresis"),
    Some("multiply"),
    Some("Oslash"),
    Some("Ugrave"),
    Some("Uacute"),
    Some("Ucircumflex"),
    Some("Udieresis"),
    Some("Yacute"),
    Some("Thorn"),
    Some("germandbls"),
    Some("agrave"),
    Some("aacute"),
    Some("acircumflex"),
    Some("atilde"),
    Some("adieresis"),
    Some("aring"),
    Some("ae"),
    Some("ccedilla"),
    Some("egrave"),
    Some("eacute"),
    Some("ecircumflex"),
    Some("edieresis"),
    Some("igrave"),
    Some("iacute"),
    Some("icircumflex"),
    Some("idieresis"),
    Some("eth"),
    Some("ntilde"),
    Some("ograve"),
    Some("oacute"),
    Some("ocircumflex"),
    Some("otilde"),
    Some("odieresis"),
    Some("divide"),
    Some("oslash"),
    Some("ugrave"),
    Some("uacute"),
    Some("ucircumflex"),
    Some("udieresis"),
    Some("yacute"),
    Some("thorn"),
    Some("ydieresis"),
];

fn is_name(value: Option<&Value>, expected: &[u8]) -> bool {
    matches!(value, Some(Value::Name(name)) if name.as_bytes() == expected)
}

fn number_of(value: Option<&Value>) -> Option<f64> {
    match value {
        Some(Value::Integer(n)) => Some(*n as f64),
        Some(Value::Real(r)) => Some(r.value()),
        _ => None,
    }
}

fn int_of(value: Option<&Value>) -> Option<i64> {
    match value {
        Some(Value::Integer(n)) => Some(*n),
        _ => None,
    }
}

fn resolve_entry(
    version: &Version,
    dict: &Dict,
    key: &str,
    budget: &Budget,
) -> StoreResult<Option<Arc<Value>>> {
    let Some(val) = dict.get(key) else {
        return Ok(None);
    };
    resolve_value(version, val, budget)
}

fn resolve_value(
    version: &Version,
    val: &Value,
    budget: &Budget,
) -> StoreResult<Option<Arc<Value>>> {
    let &Value::Reference(mut r) = val else {
        return Ok(Some(Arc::new(val.clone())));
    };
    for _ in 0..MAX_CHAIN {
        let Some(object) = version
            .get(r.number(), budget)?
            .filter(|obj| obj.generation() == r.generation())
        else {
            return Ok(None);
        };
        let value = Arc::clone(object.value());
        if let Value::Reference(next) = &*value {
            r = *next;
        } else {
            return Ok(Some(value));
        }
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyropdf_store::value::{Bytes, Name, PdfString, Real, Reference, StringForm};
    use pyropdf_store::version::{Document, MemoryBase};

    fn empty_doc() -> Document {
        Document::new(Arc::new(MemoryBase::new(Dict::new())), None)
    }

    fn add_obj(tx: &mut pyropdf_store::version::Transaction, val: &Value, budget: &Budget) -> u32 {
        let r = tx.allocate().unwrap();
        tx.write(r.number(), val, budget).unwrap();
        r.number()
    }

    fn make_predefined_type0(encoding: &str, dw: i64, dw2: Option<[i64; 2]>) -> (Document, u32) {
        let doc = empty_doc();
        let budget = Budget::default();
        let mut tx = doc.begin();
        let mut csi = Dict::new();
        csi.insert(
            Name::from("Registry"),
            Value::String(PdfString::new(b"Adobe".to_vec(), StringForm::Literal)),
        );
        csi.insert(
            Name::from("Ordering"),
            Value::String(PdfString::new(b"Japan1".to_vec(), StringForm::Literal)),
        );
        csi.insert(Name::from("Supplement"), Value::Integer(6));

        let mut desc = Dict::new();
        desc.insert(Name::from("Type"), Value::Name(Name::from("Font")));
        desc.insert(
            Name::from("Subtype"),
            Value::Name(Name::from("CIDFontType2")),
        );
        desc.insert(Name::from("BaseFont"), Value::Name(Name::from("MS-Mincho")));
        desc.insert(Name::from("CIDSystemInfo"), Value::Dict(csi));
        desc.insert(Name::from("DW"), Value::Integer(dw));
        if let Some([vy, w1y]) = dw2 {
            let mut arr = Array::new();
            arr.push(Value::Integer(vy));
            arr.push(Value::Integer(w1y));
            desc.insert(Name::from("DW2"), Value::Array(arr));
        }
        let desc_num = add_obj(&mut tx, &Value::Dict(desc), &budget);

        let mut desc_arr = Array::new();
        desc_arr.push(Value::Reference(Reference::new(desc_num, 0)));

        let mut font = Dict::new();
        font.insert(Name::from("Type"), Value::Name(Name::from("Font")));
        font.insert(Name::from("Subtype"), Value::Name(Name::from("Type0")));
        font.insert(Name::from("BaseFont"), Value::Name(Name::from("MS-Mincho")));
        font.insert(Name::from("Encoding"), Value::Name(Name::from(encoding)));
        font.insert(Name::from("DescendantFonts"), Value::Array(desc_arr));
        let font_num = add_obj(&mut tx, &Value::Dict(font), &budget);
        tx.commit(&budget).unwrap();
        (doc, font_num)
    }

    #[test]
    fn decodes_and_encodes_90ms_rksj_h() {
        let (doc, font_num) = make_predefined_type0("90ms-RKSJ-H", 1000, None);
        let budget = Budget::default();
        let version = doc.current();
        let obj = version.get(font_num, &budget).unwrap().unwrap();
        let Value::Dict(font_dict) = &**obj.value() else {
            panic!("expected font dict");
        };
        let codec = Type0Codec::load(&version, font_dict, &budget)
            .unwrap()
            .expect("valid Type0");
        assert!(!codec.is_vertical());
        // Shift-JIS bytes for "日本語"
        let sjis: &[u8] = &[0x93, 0xFA, 0x96, 0x7B, 0x8C, 0xEA];
        let decoded = codec.decode_strings(&[sjis], &budget).unwrap();
        let text: String = decoded[0].iter().map(|c| c.text.as_str()).collect();
        assert_eq!(text, "日本語");
        assert_eq!(decoded[0].len(), 3);
        assert_eq!(decoded[0][0].advance, 1000.0);

        let plan = codec.encode_text("東京A", &budget).unwrap();
        assert!(!plan.vertical);
        let mut encoded = Vec::new();
        for seg in &plan.segments {
            match seg {
                EncodedSegment::Glyph { bytes, .. } => encoded.extend_from_slice(bytes),
                EncodedSegment::Missing { text } => panic!("unexpected missing: {text}"),
            }
        }
        let roundtrip = codec.decode_strings(&[&encoded], &budget).unwrap();
        let rt_text: String = roundtrip[0].iter().map(|c| c.text.as_str()).collect();
        assert_eq!(rt_text, "東京A");
    }

    #[test]
    fn decodes_and_encodes_unijis_utf16_v() {
        let (doc, font_num) = make_predefined_type0("UniJIS-UTF16-V", 1000, Some([880, -1000]));
        let budget = Budget::default();
        let version = doc.current();
        let obj = version.get(font_num, &budget).unwrap().unwrap();
        let Value::Dict(font_dict) = &**obj.value() else {
            panic!("expected font dict");
        };
        let codec = Type0Codec::load(&version, font_dict, &budget)
            .unwrap()
            .expect("valid Type0");
        assert!(codec.is_vertical());
        let plan = codec.encode_text("日本語", &budget).unwrap();
        assert!(plan.vertical);
        let mut encoded = Vec::new();
        for seg in &plan.segments {
            match seg {
                EncodedSegment::Glyph { bytes, advance, .. } => {
                    assert_eq!(*advance, -1000.0);
                    encoded.extend_from_slice(bytes);
                }
                EncodedSegment::Missing { text } => panic!("unexpected missing: {text}"),
            }
        }
        let decoded = codec.decode_strings(&[&encoded], &budget).unwrap();
        let text: String = decoded[0].iter().map(|c| c.text.as_str()).collect();
        assert_eq!(text, "日本語");
    }

    #[test]
    fn custom_cmap_stream_with_tounicode_ligature() {
        let doc = empty_doc();
        let budget = Budget::default();
        let mut tx = doc.begin();

        let cmap_bytes = b"/CIDInit /ProcSet findresource begin\n\
            12 dict begin\n\
            begincmap\n\
            /CMapName /Custom-H def\n\
            /CMapType 1 def\n\
            /WMode 0 def\n\
            1 begincodespacerange\n<0001> <00FF>\nendcodespacerange\n\
            2 begincidchar\n<0001> 1\n<0002> 2\nendcidchar\n\
            endcmap\nend\nend\n";
        let cmap_stream = Stream::new(Dict::new(), Bytes::new(cmap_bytes.to_vec()));
        let cmap_num = add_obj(&mut tx, &Value::Stream(Box::new(cmap_stream)), &budget);

        let tounicode_bytes = b"/CIDInit /ProcSet findresource begin\n\
            12 dict begin\n\
            begincmap\n\
            1 begincodespacerange\n<0001> <00FF>\nendcodespacerange\n\
            2 beginbfchar\n<0001> <00660069>\n<0002> <0066006C>\nendbfchar\n\
            endcmap\nend\nend\n";
        let tu_stream = Stream::new(Dict::new(), Bytes::new(tounicode_bytes.to_vec()));
        let tu_num = add_obj(&mut tx, &Value::Stream(Box::new(tu_stream)), &budget);

        let mut desc = Dict::new();
        desc.insert(Name::from("Type"), Value::Name(Name::from("Font")));
        desc.insert(
            Name::from("Subtype"),
            Value::Name(Name::from("CIDFontType2")),
        );
        desc.insert(Name::from("BaseFont"), Value::Name(Name::from("Custom")));
        desc.insert(Name::from("DW"), Value::Integer(550));
        let desc_num = add_obj(&mut tx, &Value::Dict(desc), &budget);

        let mut desc_arr = Array::new();
        desc_arr.push(Value::Reference(Reference::new(desc_num, 0)));
        let mut font = Dict::new();
        font.insert(Name::from("Type"), Value::Name(Name::from("Font")));
        font.insert(Name::from("Subtype"), Value::Name(Name::from("Type0")));
        font.insert(Name::from("BaseFont"), Value::Name(Name::from("Custom")));
        font.insert(
            Name::from("Encoding"),
            Value::Reference(Reference::new(cmap_num, 0)),
        );
        font.insert(Name::from("DescendantFonts"), Value::Array(desc_arr));
        font.insert(
            Name::from("ToUnicode"),
            Value::Reference(Reference::new(tu_num, 0)),
        );
        let font_num = add_obj(&mut tx, &Value::Dict(font), &budget);
        tx.commit(&budget).unwrap();

        let version = doc.current();
        let obj = version.get(font_num, &budget).unwrap().unwrap();
        let Value::Dict(font_dict) = &**obj.value() else {
            panic!("expected font dict");
        };
        let codec = Type0Codec::load(&version, font_dict, &budget)
            .unwrap()
            .expect("valid custom Type0");
        let decoded = codec.decode_strings(&[&[0x00, 0x01]], &budget).unwrap();
        assert_eq!(decoded[0][0].text, "fi");
        assert_eq!(decoded[0][0].advance, 550.0);

        let plan = codec.encode_text("fl", &budget).unwrap();
        assert_eq!(plan.segments.len(), 1);
        match &plan.segments[0] {
            EncodedSegment::Glyph { bytes, text, .. } => {
                assert_eq!(bytes, &[0x00, 0x02]);
                assert_eq!(text, "fl");
            }
            EncodedSegment::Missing { .. } => panic!("expected ligature fl"),
        }
    }

    #[test]
    fn inspect_type3_scales_by_font_matrix_and_checks_charprocs() {
        let doc = empty_doc();
        let budget = Budget::default();
        let mut tx = doc.begin();

        let proc_a = Stream::new(Dict::new(), Bytes::new(b"25 0 d0 0 0 20 20 re f".to_vec()));
        let proc_a_num = add_obj(&mut tx, &Value::Stream(Box::new(proc_a)), &budget);

        let mut procs = Dict::new();
        procs.insert(
            Name::from("A"),
            Value::Reference(Reference::new(proc_a_num, 0)),
        );

        let mut diffs = Array::new();
        diffs.push(Value::Integer(65));
        diffs.push(Value::Name(Name::from("A")));
        diffs.push(Value::Name(Name::from("B")));
        let mut enc = Dict::new();
        enc.insert(Name::from("Type"), Value::Name(Name::from("Encoding")));
        enc.insert(Name::from("Differences"), Value::Array(diffs));

        // Non-standard FontMatrix [0.02 0 0 0.02 0 0]: width 25 * 0.02 * 1000 = 500.
        let mut matrix = Array::new();
        for v in [0.02, 0.0, 0.0, 0.02, 0.0, 0.0] {
            matrix.push(Value::Real(Real::new(v)));
        }
        let mut widths = Array::new();
        widths.push(Value::Integer(25));
        widths.push(Value::Integer(30));

        let mut font = Dict::new();
        font.insert(Name::from("Type"), Value::Name(Name::from("Font")));
        font.insert(Name::from("Subtype"), Value::Name(Name::from("Type3")));
        font.insert(Name::from("FontMatrix"), Value::Array(matrix));
        font.insert(Name::from("FirstChar"), Value::Integer(65));
        font.insert(Name::from("LastChar"), Value::Integer(66));
        font.insert(Name::from("Widths"), Value::Array(widths));
        font.insert(Name::from("CharProcs"), Value::Dict(procs));
        font.insert(Name::from("Encoding"), Value::Dict(enc));
        let font_num = add_obj(&mut tx, &Value::Dict(font), &budget);
        tx.commit(&budget).unwrap();

        let version = doc.current();
        let obj = version.get(font_num, &budget).unwrap().unwrap();
        let Value::Dict(font_dict) = &**obj.value() else {
            panic!("expected font dict");
        };
        let info = inspect_type3(&version, font_dict, &budget)
            .unwrap()
            .expect("valid Type3");
        assert!((info.advances[65] - 500.0).abs() < 1e-6);
        assert!(info.drawable[65]);
        assert!(!info.drawable[66]);
    }

    #[test]
    fn editable_identity_v_extends_and_subset_tagged_refuses() {
        let raw = include_bytes!("testdata/NotoSansJP-subset.otf");
        let face = Face::read(raw, 0).unwrap();
        let doc = empty_doc();
        let budget = Budget::default();
        let mut tx = doc.begin();

        let mut prog_dict = Dict::new();
        prog_dict.insert(Name::from("Subtype"), Value::Name(Name::from("OpenType")));
        let prog_num = add_obj(
            &mut tx,
            &Value::Stream(Box::new(Stream::new(
                prog_dict,
                Bytes::new((*face.program).clone()),
            ))),
            &budget,
        );

        let mut desc_dict = Dict::new();
        desc_dict.insert(
            Name::from("Type"),
            Value::Name(Name::from("FontDescriptor")),
        );
        desc_dict.insert(
            Name::from("FontName"),
            Value::Name(Name::from("NotoSansJP-Regular")),
        );
        desc_dict.insert(
            Name::from("FontFile3"),
            Value::Reference(Reference::new(prog_num, 0)),
        );
        let fd_num = add_obj(&mut tx, &Value::Dict(desc_dict), &budget);

        let mut cid = Dict::new();
        cid.insert(Name::from("Type"), Value::Name(Name::from("Font")));
        cid.insert(
            Name::from("Subtype"),
            Value::Name(Name::from("CIDFontType0")),
        );
        cid.insert(
            Name::from("BaseFont"),
            Value::Name(Name::from("NotoSansJP-Regular")),
        );
        cid.insert(
            Name::from("FontDescriptor"),
            Value::Reference(Reference::new(fd_num, 0)),
        );
        cid.insert(Name::from("W"), Value::Array(Array::new()));
        let mut dw2 = Array::new();
        dw2.push(Value::Integer(880));
        dw2.push(Value::Integer(-1000));
        cid.insert(Name::from("DW2"), Value::Array(dw2));
        cid.insert(Name::from("W2"), Value::Array(Array::new()));
        let cid_num = add_obj(&mut tx, &Value::Dict(cid), &budget);

        let tu_bytes = b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n\
            1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\nendcmap\nend\nend\n";
        let tu_num = add_obj(
            &mut tx,
            &Value::Stream(Box::new(Stream::new(
                Dict::new(),
                Bytes::new(tu_bytes.to_vec()),
            ))),
            &budget,
        );

        let mut desc_arr = Array::new();
        desc_arr.push(Value::Reference(Reference::new(cid_num, 0)));
        let mut font = Dict::new();
        font.insert(Name::from("Type"), Value::Name(Name::from("Font")));
        font.insert(Name::from("Subtype"), Value::Name(Name::from("Type0")));
        font.insert(
            Name::from("BaseFont"),
            Value::Name(Name::from("NotoSansJP-Regular")),
        );
        font.insert(
            Name::from("Encoding"),
            Value::Name(Name::from("Identity-V")),
        );
        font.insert(
            Name::from("DescendantFonts"),
            Value::Array(desc_arr.clone()),
        );
        font.insert(
            Name::from("ToUnicode"),
            Value::Reference(Reference::new(tu_num, 0)),
        );
        let editable_num = add_obj(&mut tx, &Value::Dict(font.clone()), &budget);

        // Subset-tagged version of the same font cannot extend when ToUnicode is empty.
        font.insert(
            Name::from("BaseFont"),
            Value::Name(Name::from("ABCDEF+NotoSansJP-Regular")),
        );
        let subset_num = add_obj(&mut tx, &Value::Dict(font), &budget);
        tx.commit(&budget).unwrap();

        let version = doc.current();
        let editable_obj = version.get(editable_num, &budget).unwrap().unwrap();
        let Value::Dict(editable_dict) = &**editable_obj.value() else {
            panic!("expected dict");
        };
        let codec = Type0Codec::load(&version, editable_dict, &budget)
            .unwrap()
            .expect("valid editable Type0");
        assert!(codec.is_vertical());
        assert!(codec.summary().can_extend);
        let plan = codec.encode_text("日本語", &budget).unwrap();
        assert!(plan.vertical);
        assert_eq!(plan.segments.len(), 3);
        assert_eq!(plan.extensions.len(), 3);
        for ext in &plan.extensions {
            assert!(ext.v_metrics.is_some());
            assert!(ext.width > 0.0);
        }

        let subset_obj = version.get(subset_num, &budget).unwrap().unwrap();
        let Value::Dict(subset_dict) = &**subset_obj.value() else {
            panic!("expected dict");
        };
        let subset_codec = Type0Codec::load(&version, subset_dict, &budget)
            .unwrap()
            .expect("valid subset Type0");
        assert!(!subset_codec.summary().can_extend);
        let subset_plan = subset_codec.encode_text("日本語", &budget).unwrap();
        assert!(subset_plan.extensions.is_empty());
        assert_eq!(
            subset_plan.segments,
            vec![EncodedSegment::Missing {
                text: "日本語".to_owned()
            }]
        );
    }

    fn make_embedded_otf_type0(
        program_bytes: Vec<u8>,
        tounicode_bytes: &[u8],
        w_array: Array,
        flate_compress: bool,
    ) -> (Document, u32) {
        let doc = empty_doc();
        let budget = Budget::default();
        let mut tx = doc.begin();

        let mut prog_dict = Dict::new();
        prog_dict.insert(Name::from("Subtype"), Value::Name(Name::from("OpenType")));
        let stream_bytes = if flate_compress {
            use flate2::Compression;
            use flate2::write::ZlibEncoder;
            use std::io::Write;
            prog_dict.insert(Name::from("Filter"), Value::Name(Name::from("FlateDecode")));
            let mut enc = ZlibEncoder::new(Vec::new(), Compression::default());
            enc.write_all(&program_bytes).unwrap();
            enc.finish().unwrap()
        } else {
            program_bytes
        };
        let prog_num = add_obj(
            &mut tx,
            &Value::Stream(Box::new(Stream::new(prog_dict, Bytes::new(stream_bytes)))),
            &budget,
        );

        let mut desc_dict = Dict::new();
        desc_dict.insert(
            Name::from("Type"),
            Value::Name(Name::from("FontDescriptor")),
        );
        desc_dict.insert(
            Name::from("FontName"),
            Value::Name(Name::from("NotoSansJP-Regular")),
        );
        desc_dict.insert(
            Name::from("FontFile3"),
            Value::Reference(Reference::new(prog_num, 0)),
        );
        let fd_num = add_obj(&mut tx, &Value::Dict(desc_dict), &budget);

        let mut cid = Dict::new();
        cid.insert(Name::from("Type"), Value::Name(Name::from("Font")));
        cid.insert(
            Name::from("Subtype"),
            Value::Name(Name::from("CIDFontType0")),
        );
        cid.insert(
            Name::from("BaseFont"),
            Value::Name(Name::from("NotoSansJP-Regular")),
        );
        cid.insert(
            Name::from("FontDescriptor"),
            Value::Reference(Reference::new(fd_num, 0)),
        );
        cid.insert(Name::from("W"), Value::Array(w_array));
        let cid_num = add_obj(&mut tx, &Value::Dict(cid), &budget);

        let tu_num = add_obj(
            &mut tx,
            &Value::Stream(Box::new(Stream::new(
                Dict::new(),
                Bytes::new(tounicode_bytes.to_vec()),
            ))),
            &budget,
        );

        let mut desc_arr = Array::new();
        desc_arr.push(Value::Reference(Reference::new(cid_num, 0)));
        let mut font = Dict::new();
        font.insert(Name::from("Type"), Value::Name(Name::from("Font")));
        font.insert(Name::from("Subtype"), Value::Name(Name::from("Type0")));
        font.insert(
            Name::from("BaseFont"),
            Value::Name(Name::from("NotoSansJP-Regular")),
        );
        font.insert(
            Name::from("Encoding"),
            Value::Name(Name::from("Identity-H")),
        );
        font.insert(Name::from("DescendantFonts"), Value::Array(desc_arr));
        font.insert(
            Name::from("ToUnicode"),
            Value::Reference(Reference::new(tu_num, 0)),
        );
        let font_num = add_obj(&mut tx, &Value::Dict(font), &budget);
        tx.commit(&budget).unwrap();
        (doc, font_num)
    }

    #[test]
    fn non_identity_cid_keyed_cff_refuses_extension() {
        // Raw NotoSansJP-subset.otf has a non-identity CID-keyed CFF charset
        // (before `Face::read` rewrites the charset to identity).
        let raw = include_bytes!("testdata/NotoSansJP-subset.otf");
        let font_ref = FontRef::new(raw).unwrap();
        let cff_table = font_ref.table_data(skrifa::Tag::new(b"CFF ")).unwrap();
        let cff = CffFontRef::new_cff(cff_table.as_bytes(), 0, None).unwrap();
        assert!(cff.is_cid());
        let num_glyphs = u32::from(font_ref.maxp().unwrap().num_glyphs());
        assert!(!check_identity_cff_charset(&cff, num_glyphs, &Budget::default()).unwrap());

        let tu_bytes = b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n\
            1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\nendcmap\nend\nend\n";
        let (doc, font_num) = make_embedded_otf_type0(raw.to_vec(), tu_bytes, Array::new(), false);
        let budget = Budget::default();
        let version = doc.current();
        let obj = version.get(font_num, &budget).unwrap().unwrap();
        let Value::Dict(font_dict) = &**obj.value() else {
            panic!("expected dict");
        };
        let codec = Type0Codec::load(&version, font_dict, &budget)
            .unwrap()
            .expect("valid Type0");
        assert!(!codec.summary().can_extend);
        let plan = codec.encode_text("日本語", &budget).unwrap();
        assert!(plan.extensions.is_empty());
        assert_eq!(
            plan.segments,
            vec![EncodedSegment::Missing {
                text: "日本語".to_owned()
            }]
        );
    }

    #[test]
    fn large_bfrange_stays_within_tight_memory_budget_and_w_ranges_exact() {
        let raw = include_bytes!("testdata/NotoSansJP-subset.otf");
        let face = Face::read(raw, 0).unwrap();
        let tu_bytes = b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n\
            1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n\
            1 beginbfrange\n<0000> <FFFF> <0020>\nendbfrange\n\
            endcmap\nend\nend\n";
        // Wide compact /W range [0 5000 600] (> 256 entries).
        let mut w = Array::new();
        w.push(Value::Integer(0));
        w.push(Value::Integer(5000));
        w.push(Value::Integer(600));
        let (doc, font_num) = make_embedded_otf_type0((*face.program).clone(), tu_bytes, w, false);

        // Tight 64 KiB memory budget would fail if the 65,536-entry bfrange were eagerly expanded.
        let limits = pyropdf_store::budget::DocumentLimits {
            max_memory_size: Some(64 * 1024),
            ..pyropdf_store::budget::DocumentLimits::default()
        };
        let budget = Budget::new(&limits);
        let version = doc.current();
        let obj = version.get(font_num, &budget).unwrap().unwrap();
        let Value::Dict(font_dict) = &**obj.value() else {
            panic!("expected dict");
        };
        let codec = Type0Codec::load(&version, font_dict, &budget)
            .unwrap()
            .expect("valid Type0");
        // Verify wide /W range lookup works without BTreeSet cap.
        assert_eq!(codec.advance_for_cid(1000), 600.0);
        // Encoding a single character present in the 65,536-entry range stays well under 64 KiB.
        let plan = codec.encode_text("!", &budget).unwrap();
        assert!(plan.extensions.is_empty());
        // Code 0x0001 maps to U+0021 ('!') in <0000> <FFFF> <0020>; check round-trip or missing.
        assert!(!plan.segments.is_empty());
    }

    #[test]
    fn inspect_and_decode_do_not_decompress_font_program() {
        let raw = include_bytes!("testdata/NotoSansJP-subset.otf");
        let face = Face::read(raw, 0).unwrap();
        let prog_len = face.program.len() as u64;
        assert!(prog_len > 8192);
        let tu_bytes = b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n\
            1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n\
            1 beginbfchar\n<0001> <0041>\nendbfchar\n\
            endcmap\nend\nend\n";
        let (doc, font_num) =
            make_embedded_otf_type0((*face.program).clone(), tu_bytes, Array::new(), true);

        // TotalDecompressedSize smaller than the compressed font program's decoded size.
        let limits = pyropdf_store::budget::DocumentLimits {
            max_total_decompressed_size: Some(4096),
            ..pyropdf_store::budget::DocumentLimits::default()
        };
        let budget = Budget::new(&limits);
        let version = doc.current();
        let obj = version.get(font_num, &budget).unwrap().unwrap();
        let Value::Dict(font_dict) = &**obj.value() else {
            panic!("expected dict");
        };
        let summary = Type0Codec::inspect(&version, font_dict, &budget)
            .unwrap()
            .expect("inspect succeeds without decompressing program");
        assert!(!summary.vertical);
        let dec_codec = Type0Codec::load_for_decode(&version, font_dict, &budget)
            .unwrap()
            .expect("decode load succeeds without decompressing program");
        let decoded = dec_codec.decode_strings(&[&[0x00, 0x01]], &budget).unwrap();
        assert_eq!(decoded[0][0].text, "A");
    }

    #[test]
    fn rtl_missing_runs_preserve_logical_order() {
        let raw = include_bytes!("testdata/variable-latin.ttf");
        let face = Face::read(raw, 0).unwrap();
        let doc = empty_doc();
        let budget = Budget::default();
        let mut tx = doc.begin();

        let prog_num = add_obj(
            &mut tx,
            &Value::Stream(Box::new(Stream::new(
                Dict::new(),
                Bytes::new((*face.program).clone()),
            ))),
            &budget,
        );
        let mut fd = Dict::new();
        fd.insert(
            Name::from("Type"),
            Value::Name(Name::from("FontDescriptor")),
        );
        fd.insert(
            Name::from("FontName"),
            Value::Name(Name::from("VariableLatin")),
        );
        fd.insert(
            Name::from("FontFile2"),
            Value::Reference(Reference::new(prog_num, 0)),
        );
        let fd_num = add_obj(&mut tx, &Value::Dict(fd), &budget);

        let mut cid = Dict::new();
        cid.insert(Name::from("Type"), Value::Name(Name::from("Font")));
        cid.insert(
            Name::from("Subtype"),
            Value::Name(Name::from("CIDFontType2")),
        );
        cid.insert(
            Name::from("BaseFont"),
            Value::Name(Name::from("VariableLatin")),
        );
        cid.insert(
            Name::from("CIDToGIDMap"),
            Value::Name(Name::from("Identity")),
        );
        cid.insert(
            Name::from("FontDescriptor"),
            Value::Reference(Reference::new(fd_num, 0)),
        );
        cid.insert(Name::from("W"), Value::Array(Array::new()));
        let cid_num = add_obj(&mut tx, &Value::Dict(cid), &budget);

        let tu_bytes = b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n\
            1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\nendcmap\nend\nend\n";
        let tu_num = add_obj(
            &mut tx,
            &Value::Stream(Box::new(Stream::new(
                Dict::new(),
                Bytes::new(tu_bytes.to_vec()),
            ))),
            &budget,
        );
        let mut desc_arr = Array::new();
        desc_arr.push(Value::Reference(Reference::new(cid_num, 0)));
        let mut font = Dict::new();
        font.insert(Name::from("Type"), Value::Name(Name::from("Font")));
        font.insert(Name::from("Subtype"), Value::Name(Name::from("Type0")));
        font.insert(
            Name::from("BaseFont"),
            Value::Name(Name::from("VariableLatin")),
        );
        font.insert(
            Name::from("Encoding"),
            Value::Name(Name::from("Identity-H")),
        );
        font.insert(Name::from("DescendantFonts"), Value::Array(desc_arr));
        font.insert(
            Name::from("ToUnicode"),
            Value::Reference(Reference::new(tu_num, 0)),
        );
        let font_num = add_obj(&mut tx, &Value::Dict(font), &budget);
        tx.commit(&budget).unwrap();

        let version = doc.current();
        let obj = version.get(font_num, &budget).unwrap().unwrap();
        let Value::Dict(font_dict) = &**obj.value() else {
            panic!("expected dict");
        };
        let codec = Type0Codec::load(&version, font_dict, &budget)
            .unwrap()
            .expect("valid editable TTF");
        assert!(codec.summary().can_extend);

        let pure_rtl = codec.encode_text("שלום", &budget).unwrap();
        assert_eq!(
            pure_rtl.segments,
            vec![EncodedSegment::Missing {
                text: "שלום".to_owned()
            }]
        );

        let mixed = codec.encode_text("AשלוםV", &budget).unwrap();
        assert_eq!(mixed.segments.len(), 3);
        assert!(matches!(&mixed.segments[0], EncodedSegment::Glyph { text, .. } if text == "A"));
        assert_eq!(
            mixed.segments[1],
            EncodedSegment::Missing {
                text: "שלום".to_owned()
            }
        );
        assert!(matches!(&mixed.segments[2], EncodedSegment::Glyph { text, .. } if text == "V"));
    }

    #[test]
    fn type3_standard_macroman_indirect_base_and_non_stream_charprocs() {
        // Verify STANDARD_NAMES matches skrifa::raw::ps::PredefinedEncoding::Standard for all 256 bytes.
        for code in 0..=255_u8 {
            let expected = skrifa::raw::ps::PredefinedEncoding::Standard.name(code);
            let actual = STANDARD_NAMES[usize::from(code)];
            if expected == ".notdef" || expected.is_empty() {
                assert_eq!(actual, None, "mismatch at StandardEncoding byte {code:#X}");
            } else {
                assert_eq!(
                    actual,
                    Some(expected),
                    "mismatch at StandardEncoding byte {code:#X}"
                );
            }
        }
        assert_eq!(STANDARD_NAMES[0x27], Some("quoteright"));
        assert_eq!(STANDARD_NAMES[0x60], Some("quoteleft"));
        assert_eq!(MACROMAN_NAMES[0x80], Some("Adieresis"));
        assert_eq!(MACROMAN_NAMES[0x8E], Some("eacute"));
        assert_eq!(MACROMAN_NAMES[0xA9], Some("copyright"));
        assert_eq!(MACROMAN_NAMES[0xFF], Some("caron"));

        let doc = empty_doc();
        let budget = Budget::default();
        let mut tx = doc.begin();

        let proc_stream = Stream::new(Dict::new(), Bytes::new(b"500 0 d0 0 0 10 10 re f".to_vec()));
        let proc_num = add_obj(&mut tx, &Value::Stream(Box::new(proc_stream)), &budget);
        let base_enc_num = add_obj(
            &mut tx,
            &Value::Name(Name::from("MacRomanEncoding")),
            &budget,
        );

        let mut procs = Dict::new();
        procs.insert(
            Name::from("Adieresis"),
            Value::Reference(Reference::new(proc_num, 0)),
        );
        // Non-stream /CharProcs entries must NOT be marked drawable.
        procs.insert(Name::from("Aring"), Value::Dict(Dict::new()));
        procs.insert(Name::from("Ccedilla"), Value::Null);
        procs.insert(
            Name::from("Eacute"),
            Value::Reference(Reference::new(999, 0)),
        );

        let mut enc = Dict::new();
        enc.insert(Name::from("Type"), Value::Name(Name::from("Encoding")));
        enc.insert(
            Name::from("BaseEncoding"),
            Value::Reference(Reference::new(base_enc_num, 0)),
        );

        let mut font = Dict::new();
        font.insert(Name::from("Type"), Value::Name(Name::from("Font")));
        font.insert(Name::from("Subtype"), Value::Name(Name::from("Type3")));
        font.insert(Name::from("CharProcs"), Value::Dict(procs));
        font.insert(Name::from("Encoding"), Value::Dict(enc));
        let font_num = add_obj(&mut tx, &Value::Dict(font), &budget);
        tx.commit(&budget).unwrap();

        let version = doc.current();
        let obj = version.get(font_num, &budget).unwrap().unwrap();
        let Value::Dict(font_dict) = &**obj.value() else {
            panic!("expected dict");
        };
        let info = inspect_type3(&version, font_dict, &budget)
            .unwrap()
            .expect("valid Type3");
        assert!(info.drawable[0x80], "Adieresis stream must be drawable");
        assert!(!info.drawable[0x81], "Dict CharProc must not be drawable");
        assert!(!info.drawable[0x82], "Null CharProc must not be drawable");
        assert!(
            !info.drawable[0x83],
            "Dangling ref CharProc must not be drawable"
        );
    }

    #[test]
    fn tounicode_overrides_one_byte_bf_cidrange_postscript_and_usecmap() {
        let doc = empty_doc();
        let budget = Budget::default();
        let mut tx = doc.begin();

        // Parent custom CMap: <0001> -> 1, <0002> -> 2, <0003> -> 3, <0004> -> 4.
        // Includes rectangular codespace <8140> <9FFC> for fallback check.
        let parent_cmap = b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n\
            /CMapName /ParentCustom-H def\n/WMode 0 def\n\
            2 begincodespacerange\n<0001> <0010>\n<8140> <9FFC>\nendcodespacerange\n\
            1 begincidrange\n<0001> <0004> 1\nendcidrange\nendcmap\nend\nend\n";
        let parent_num = add_obj(
            &mut tx,
            &Value::Stream(Box::new(Stream::new(
                Dict::new(),
                Bytes::new(parent_cmap.to_vec()),
            ))),
            &budget,
        );

        // Child custom CMap references unknown `/ParentCustom-H usecmap` in body AND
        // `/UseCMap` dict entry to `parent_num`, overriding <0001> -> 10.
        let child_cmap = b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n\
            /ParentCustom-H usecmap\n/CMapName /ChildCustom-H def\n\
            1 begincidchar\n<0001> 10\nendcidchar\nendcmap\nend\nend\n";
        let mut child_dict = Dict::new();
        child_dict.insert(
            Name::from("UseCMap"),
            Value::Reference(Reference::new(parent_num, 0)),
        );
        let child_num = add_obj(
            &mut tx,
            &Value::Stream(Box::new(Stream::new(
                child_dict,
                Bytes::new(child_cmap.to_vec()),
            ))),
            &budget,
        );

        // ToUnicode with:
        // - non-PDF PostScript token `.notdef` before blocks
        // - overridden entry <0001> <0041> then <0001> <0042>
        // - 1-byte bf destination <0002> <41> ('A')
        // - hex whitespace <00 03> <00 43> ('C')
        // - cidrange <0004> <0004> 68 ('D')
        let tu_bytes = b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n\
            .notdef\n\
            1 begincodespacerange\n<0001> <0010>\nendcodespacerange\n\
            4 beginbfchar\n\
            <0001> <0041>\n\
            <0001> <0042>\n\
            <0002> <41>\n\
            <00 03> <00 43>\n\
            endbfchar\n\
            1 begincidrange\n<0004> <0004> 68\nendcidrange\n\
            endcmap\nend\nend\n";
        let tu_num = add_obj(
            &mut tx,
            &Value::Stream(Box::new(Stream::new(
                Dict::new(),
                Bytes::new(tu_bytes.to_vec()),
            ))),
            &budget,
        );

        let mut desc = Dict::new();
        desc.insert(Name::from("Type"), Value::Name(Name::from("Font")));
        desc.insert(
            Name::from("Subtype"),
            Value::Name(Name::from("CIDFontType2")),
        );
        desc.insert(Name::from("BaseFont"), Value::Name(Name::from("Custom")));
        desc.insert(Name::from("DW"), Value::Integer(500));
        let desc_num = add_obj(&mut tx, &Value::Dict(desc), &budget);
        let mut desc_arr = Array::new();
        desc_arr.push(Value::Reference(Reference::new(desc_num, 0)));

        let mut font = Dict::new();
        font.insert(Name::from("Type"), Value::Name(Name::from("Font")));
        font.insert(Name::from("Subtype"), Value::Name(Name::from("Type0")));
        font.insert(Name::from("BaseFont"), Value::Name(Name::from("Custom")));
        font.insert(
            Name::from("Encoding"),
            Value::Reference(Reference::new(child_num, 0)),
        );
        font.insert(Name::from("DescendantFonts"), Value::Array(desc_arr));
        font.insert(
            Name::from("ToUnicode"),
            Value::Reference(Reference::new(tu_num, 0)),
        );
        let font_num = add_obj(&mut tx, &Value::Dict(font), &budget);
        tx.commit(&budget).unwrap();

        let version = doc.current();
        let obj = version.get(font_num, &budget).unwrap().unwrap();
        let Value::Dict(font_dict) = &**obj.value() else {
            panic!("expected dict");
        };
        let codec = Type0Codec::load(&version, font_dict, &budget)
            .unwrap()
            .expect("valid chained custom Type0");

        // Child override `<0001> -> CID 10` wins over parent `<0001> -> CID 1`.
        let dec1 = codec.decode_strings(&[&[0x00, 0x01]], &budget).unwrap();
        assert_eq!(dec1[0][0].cid, 10);

        // Encode "ABCD" -> must round-trip cleanly to "ABCD" through decode_strings.
        let plan = codec.encode_text("ABCD", &budget).unwrap();
        let mut encoded = Vec::new();
        for seg in &plan.segments {
            match seg {
                EncodedSegment::Glyph { bytes, .. } => encoded.extend_from_slice(bytes),
                EncodedSegment::Missing { text } => panic!("unexpected missing: {text}"),
            }
        }
        let rt = codec.decode_strings(&[&encoded], &budget).unwrap();
        let rt_text: String = rt[0].iter().map(|c| c.text.as_str()).collect();
        assert_eq!(rt_text, "ABCD");

        // Rectangular CodespaceRange check: <8200> has byte1=0x00 < 0x40 (outside <8140>..<9FFC>),
        // while <8240> is inside and consumes 2 bytes.
        let (_, len_outside, _) = codec.read_code(&[0x82, 0x00], 0);
        assert_eq!(len_outside, 1);
        let (_, len_inside, _) = codec.read_code(&[0x82, 0x40], 0);
        assert_eq!(len_inside, 2);
    }

    #[test]
    fn custom_cmap_with_predefined_usecmap_encodes_inherited_chars() {
        let doc = empty_doc();
        let budget = Budget::default();
        let mut tx = doc.begin();

        let child_cmap = b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n\
            /CMapName /CustomOverUniJIS-H def\n/WMode 0 def\n\
            1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n\
            endcmap\nend\nend\n";
        let mut cmap_dict = Dict::new();
        cmap_dict.insert(
            Name::from("UseCMap"),
            Value::Name(Name::from("UniJIS-UTF16-H")),
        );
        let cmap_num = add_obj(
            &mut tx,
            &Value::Stream(Box::new(Stream::new(
                cmap_dict,
                Bytes::new(child_cmap.to_vec()),
            ))),
            &budget,
        );

        let mut csi = Dict::new();
        csi.insert(
            Name::from("Registry"),
            Value::String(PdfString::new(b"Adobe".to_vec(), StringForm::Literal)),
        );
        csi.insert(
            Name::from("Ordering"),
            Value::String(PdfString::new(b"Japan1".to_vec(), StringForm::Literal)),
        );
        csi.insert(Name::from("Supplement"), Value::Integer(6));
        let mut desc = Dict::new();
        desc.insert(Name::from("Type"), Value::Name(Name::from("Font")));
        desc.insert(
            Name::from("Subtype"),
            Value::Name(Name::from("CIDFontType2")),
        );
        desc.insert(Name::from("BaseFont"), Value::Name(Name::from("MS-Mincho")));
        desc.insert(Name::from("CIDSystemInfo"), Value::Dict(csi));
        desc.insert(Name::from("DW"), Value::Integer(1000));
        let desc_num = add_obj(&mut tx, &Value::Dict(desc), &budget);
        let mut desc_arr = Array::new();
        desc_arr.push(Value::Reference(Reference::new(desc_num, 0)));

        let mut font = Dict::new();
        font.insert(Name::from("Type"), Value::Name(Name::from("Font")));
        font.insert(Name::from("Subtype"), Value::Name(Name::from("Type0")));
        font.insert(Name::from("BaseFont"), Value::Name(Name::from("MS-Mincho")));
        font.insert(
            Name::from("Encoding"),
            Value::Reference(Reference::new(cmap_num, 0)),
        );
        font.insert(Name::from("DescendantFonts"), Value::Array(desc_arr));
        let font_num = add_obj(&mut tx, &Value::Dict(font), &budget);
        tx.commit(&budget).unwrap();

        let version = doc.current();
        let obj = version.get(font_num, &budget).unwrap().unwrap();
        let Value::Dict(font_dict) = &**obj.value() else {
            panic!("expected dict");
        };
        let codec = Type0Codec::load(&version, font_dict, &budget)
            .unwrap()
            .expect("valid custom-over-predefined Type0");
        let plan = codec.encode_text("東京", &budget).unwrap();
        let mut encoded = Vec::new();
        for seg in &plan.segments {
            match seg {
                EncodedSegment::Glyph { bytes, .. } => encoded.extend_from_slice(bytes),
                EncodedSegment::Missing { text } => panic!("unexpected missing: {text}"),
            }
        }
        let rt = codec.decode_strings(&[&encoded], &budget).unwrap();
        let rt_text: String = rt[0].iter().map(|c| c.text.as_str()).collect();
        assert_eq!(rt_text, "東京");
    }
}
