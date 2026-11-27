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
use skrifa::raw::ps::cff::CffFontRef;

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

/// One codespace range from a CMap stream.
#[derive(Clone, Copy, Debug)]
struct CodespaceRange {
    len: u8,
    low: u32,
    high: u32,
}

/// One CID mapping interval from a custom CMap stream.
#[derive(Clone, Copy, Debug)]
struct CidMapRange {
    len: u8,
    start: u32,
    end: u32,
    cid_start: u32,
}

/// Parsed tables from a raw PDF CMap stream.
#[derive(Clone, Debug, Default)]
struct ParsedCMapTables {
    codespaces: Vec<CodespaceRange>,
    cid_ranges: Vec<CidMapRange>,
    bf_entries: Vec<(u8, u32, String)>,
    wmode: Option<u8>,
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

/// Encoding classification of a Type 0 font.
#[derive(Clone, Debug)]
enum EncodingKind {
    Identity,
    PredefinedUtf16,
    PredefinedOther,
    CustomStream,
}

/// Parsed Type 0 font codec backed by the store.
pub struct Type0Codec {
    base_font: Option<String>,
    vertical: bool,
    encoding_kind: EncodingKind,
    encoding_chain: Vec<CMap>,
    custom_codespaces: Vec<CodespaceRange>,
    custom_cid_ranges: Vec<CidMapRange>,
    to_unicode: Option<CMap>,
    to_unicode_entries: Vec<(u8, u32, String)>,
    ucs2_cmap: Option<CMap>,
    dw: f64,
    widths: Vec<WidthRange>,
    dw2_w1y: f64,
    widths2: Vec<Width2Range>,
    cid_to_gid: CidToGid,
    is_subset_tagged: bool,
    program: ProgramState,
    can_extend: bool,
    existing_w_cids: BTreeSet<u32>,
    existing_w2_cids: BTreeSet<u32>,
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
    /// Inspects `font_dict` in `version`, returning `None` when it is not a
    /// well-formed Type 0 composite font.
    pub fn load(version: &Version, font_dict: &Dict, budget: &Budget) -> StoreResult<Option<Self>> {
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

        let character_collection = enc
            .chain
            .first()
            .and_then(|c| c.metadata().character_collection.clone())
            .filter(|cc| cc.family != CidFamily::AdobeIdentity)
            .or_else(|| {
                read_cid_system_info(version, descendant, budget)
                    .ok()
                    .flatten()
            });

        let dw = number_of(descendant.get("DW")).unwrap_or(1000.0);
        let (widths, existing_w_cids) = match resolve_entry(version, descendant, "W", budget)? {
            Some(val) => match &*val {
                Value::Array(arr) => parse_widths(arr, budget)?,
                _ => (Vec::new(), BTreeSet::new()),
            },
            None => (Vec::new(), BTreeSet::new()),
        };

        let dw2_w1y = match resolve_entry(version, descendant, "DW2", budget)? {
            Some(val) => match &*val {
                Value::Array(arr) if arr.len() >= 2 => number_of(arr.get(1)).unwrap_or(-1000.0),
                _ => -1000.0,
            },
            None => -1000.0,
        };
        let (widths2, existing_w2_cids) = match resolve_entry(version, descendant, "W2", budget)? {
            Some(val) => match &*val {
                Value::Array(arr) => parse_widths2(arr, budget)?,
                _ => (Vec::new(), BTreeSet::new()),
            },
            None => (Vec::new(), BTreeSet::new()),
        };

        let cid_to_gid = load_cid_to_gid(version, descendant, budget)?;

        let to_unicode_indirect = matches!(font_dict.get("ToUnicode"), Some(Value::Reference(_)));
        let (to_unicode, to_unicode_entries) =
            match resolve_entry(version, font_dict, "ToUnicode", budget)? {
                Some(val) => match &*val {
                    Value::Stream(stream) => {
                        let data = stream.decoded(budget)?;
                        let n = data.len() as u64;
                        budget.check(LimitCode::FontInputSize, n)?;
                        budget.charge(LimitCode::InterpretationFuel, n)?;
                        let cmap = CMap::parse(&data, load_embedded);
                        let tables = parse_cmap_tables(&data, budget)?;
                        (cmap, tables.bf_entries)
                    }
                    _ => (None, Vec::new()),
                },
                None => (None, Vec::new()),
            };

        let ucs2_cmap = character_collection
            .as_ref()
            .and_then(|cc| cc.family.ucs2_cmap())
            .and_then(load_embedded)
            .and_then(|data| CMap::parse(data, load_embedded));

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

        let (program, is_opentype_stream) = match descriptor.as_ref() {
            Some(desc) => load_program(version, desc, cid_is_truetype, budget)?,
            None => (ProgramState::None, false),
        };

        let cid_to_gid_is_identity = matches!(cid_to_gid, CidToGid::Identity);
        let cid_to_gid_explicit_identity = is_name(descendant.get("CIDToGIDMap"), b"Identity");
        let has_editable_face = matches!(
            &program,
            ProgramState::Sfnt {
                editable_face: Some(_),
                ..
            }
        );

        let can_extend = !is_subset_tagged
            && matches!(enc.kind, EncodingKind::Identity)
            && desc_is_indirect
            && to_unicode_indirect
            && has_editable_face
            && ((cid_is_truetype && cid_to_gid_explicit_identity)
                || (cid_is_cff && is_opentype_stream && cid_to_gid_is_identity));

        Ok(Some(Self {
            base_font,
            vertical: enc.vertical,
            encoding_kind: enc.kind,
            encoding_chain: enc.chain,
            custom_codespaces: enc.codespaces,
            custom_cid_ranges: enc.cid_ranges,
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
            existing_w_cids,
            existing_w2_cids,
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
            });
        }

        // The font is an editable/un-subsetted pyropdf-embedded OpenType face
        // and `text` requires glyphs not yet in ToUnicode/W/W2. Shape with
        // harfrust (applying `vert` for vertical mode and OpenType ligatures
        // for horizontal mode) and plan any needed font extensions.
        self.encode_with_extension(text, &mut segments, &mut extensions, budget)?;
        Ok(EncodePlan {
            vertical: self.vertical,
            segments: segments.into_vec(),
            extensions: extensions.into_vec(),
        })
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
        // Fallback: honor explicit codespace ranges when a code falls inside a
        // codespace range without a CID mapping (maps to CID 0).
        let mut fallback_code = 0_u32;
        for n in 0..max_len {
            fallback_code = (fallback_code << 8) | u32::from(bytes[offset + n]);
            let len = (n + 1) as u8;
            if self
                .custom_codespaces
                .iter()
                .any(|cs| cs.len == len && fallback_code >= cs.low && fallback_code <= cs.high)
            {
                return (fallback_code, n + 1, 0);
            }
        }
        (u32::from(bytes[offset]), 1, 0)
    }

    fn lookup_cid(&self, code: u32, byte_len: u8) -> Option<u32> {
        for cmap in &self.encoding_chain {
            if let Some(cid) = cmap.lookup_cid_code(code, byte_len) {
                return Some(cid);
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

    fn is_drawable(&self, cid: u32, text: &str) -> bool {
        let is_whitespace = !text.is_empty() && text.chars().all(char::is_whitespace);
        match &self.program {
            ProgramState::None => cid != 0,
            ProgramState::Other => cid != 0,
            ProgramState::Sfnt {
                data,
                is_cid_keyed_cff,
                num_glyphs,
                ..
            } => {
                let gid = if *is_cid_keyed_cff {
                    if matches!(self.cid_to_gid, CidToGid::Identity) {
                        sfnt_cff_glyph_by_cid(data, cid).unwrap_or(0)
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
                        return self.existing_w_cids.contains(&cid)
                            || self.existing_w2_cids.contains(&cid)
                            || self.to_unicode.is_some();
                    }
                    return true;
                }
                sfnt_glyph_has_ink(data, gid)
            }
            ProgramState::BareCff { data, is_cid_keyed } => {
                let gid = if *is_cid_keyed {
                    if matches!(self.cid_to_gid, CidToGid::Identity) {
                        bare_cff_glyph_by_cid(data, cid).unwrap_or(0)
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
                bare_cff_glyph_has_ink(data, gid)
            }
        }
    }

    fn build_reverse_map(
        &self,
        text: &str,
        budget: &Budget,
    ) -> StoreResult<BTreeMap<String, (Vec<u8>, u32)>> {
        let mut map: BTreeMap<String, (Vec<u8>, u32)> = BTreeMap::new();
        // 1. Existing `/ToUnicode` stream entries (covers Identity-H/V, ligatures,
        //    custom CMaps with ToUnicode, and predefined CMaps with ToUnicode).
        for (byte_len, code, unicode) in &self.to_unicode_entries {
            budget.poll()?;
            if unicode.is_empty() || unicode == "\0" {
                continue;
            }
            // Verify the code maps through the font's Encoding CMap to a drawable CID.
            let resolved = self
                .lookup_cid(*code, *byte_len)
                .map(|cid| (*byte_len, cid))
                .or_else(|| {
                    (1..=4_u8).find_map(|len| self.lookup_cid(*code, len).map(|cid| (len, cid)))
                });
            let Some((actual_len, cid)) = resolved else {
                continue;
            };
            if !self.is_drawable(cid, unicode) {
                continue;
            }
            let raw = encode_code_bytes(*code, actual_len);
            let replace = match map.get(unicode) {
                None => true,
                Some((existing, _)) => {
                    raw.len() < existing.len() || (raw.len() == existing.len() && raw < *existing)
                }
            };
            if replace {
                budget.charge(LimitCode::MemorySize, 64)?;
                #[expect(
                    clippy::disallowed_methods,
                    reason = "charged to MemorySize and bounded by ToUnicode entries"
                )]
                map.insert(unicode.clone(), (raw, cid));
            }
        }

        // If a `/ToUnicode` stream is present on a non-extensible font, only
        // codes mapped by `/ToUnicode` will extract back reliably in both
        // pyropdf and Poppler.
        if self.to_unicode.is_some() {
            return Ok(map);
        }

        // 2. No `/ToUnicode` stream: rely on Encoding CMap + predefined UCS2 CMap.
        let Some(ucs2) = &self.ucs2_cmap else {
            return Ok(map);
        };

        let mut needed: BTreeSet<char> = BTreeSet::new();
        for ch in text.chars() {
            #[expect(
                clippy::disallowed_methods,
                reason = "at most one entry per distinct char in the caller's replacement string"
            )]
            needed.insert(ch);
        }

        match self.encoding_kind {
            EncodingKind::PredefinedUtf16 => {
                for ch in needed {
                    budget.poll()?;
                    let mut utf16 = [0_u16; 2];
                    let units = ch.encode_utf16(&mut utf16);
                    let (code, len) = if units.len() == 1 {
                        (u32::from(units[0]), 2_u8)
                    } else {
                        ((u32::from(units[0]) << 16) | u32::from(units[1]), 4_u8)
                    };
                    if let Some(cid) = self.lookup_cid(code, len)
                        && self.is_drawable(cid, &ch.to_string())
                        && matches_ucs2(ucs2, cid, ch)
                    {
                        budget.charge(LimitCode::MemorySize, 64)?;
                        #[expect(
                            clippy::disallowed_methods,
                            reason = "at most one entry per distinct char in the replacement string"
                        )]
                        map.insert(ch.to_string(), (encode_code_bytes(code, len), cid));
                    }
                }
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
                        if let Some(BfString::Char(ch)) = ucs2.lookup_bf_string(cid)
                            && needed.contains(&ch)
                            && self.is_drawable(cid, &ch.to_string())
                        {
                            let raw = encode_code_bytes(code, range.len);
                            let key = ch.to_string();
                            let replace = match map.get(&key) {
                                None => true,
                                Some((existing, _)) => {
                                    raw.len() < existing.len()
                                        || (raw.len() == existing.len() && raw < *existing)
                                }
                            };
                            if replace {
                                budget.charge(LimitCode::MemorySize, 64)?;
                                #[expect(
                                    clippy::disallowed_methods,
                                    reason = "bounded by distinct chars of replacement string"
                                )]
                                map.insert(key, (raw, cid));
                            }
                        }
                    }
                }
            }
            EncodingKind::PredefinedOther => {
                // Scan 1-byte codes first (prefers 1-byte ASCII in RKSJ/EUC/GBK/Big5),
                // then 2-byte codes, stopping immediately once every needed char is found.
                let mut remaining = needed;
                for code in 0..=255_u32 {
                    if remaining.is_empty() {
                        break;
                    }
                    if let Some(cid) = self.lookup_cid(code, 1)
                        && let Some(BfString::Char(ch)) = ucs2.lookup_bf_string(cid)
                        && remaining.contains(&ch)
                        && self.is_drawable(cid, &ch.to_string())
                    {
                        budget.charge(LimitCode::MemorySize, 64)?;
                        #[expect(
                            clippy::disallowed_methods,
                            reason = "bounded by distinct chars of replacement string"
                        )]
                        map.insert(ch.to_string(), (encode_code_bytes(code, 1), cid));
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
                            && self.is_drawable(cid, &ch.to_string())
                        {
                            budget.charge(LimitCode::MemorySize, 64)?;
                            #[expect(
                                clippy::disallowed_methods,
                                reason = "bounded by distinct chars of replacement string"
                            )]
                            map.insert(ch.to_string(), (encode_code_bytes(code, 2), cid));
                            remaining.remove(&ch);
                        }
                    }
                }
            }
            EncodingKind::Identity => {}
        }

        Ok(map)
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
            data,
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

        let mut planned_gids: BTreeSet<u16> = BTreeSet::new();
        let mut missing = String::new();
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
                    && (is_ws || sfnt_glyph_has_ink(data, g.glyph))
            });

            if !cluster_ok {
                budget.charge(LimitCode::MemorySize, cluster_text.len() as u64)?;
                #[expect(
                    clippy::disallowed_methods,
                    reason = "bounded by input text length and charged to MemorySize"
                )]
                missing.push_str(cluster_text);
                idx = end;
                continue;
            }

            if !missing.is_empty() {
                segments.try_push(EncodedSegment::Missing {
                    text: std::mem::take(&mut missing),
                })?;
            }

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
                let needs_w = !self.existing_w_cids.contains(&cid);
                let needs_w2 = self.vertical && !self.existing_w2_cids.contains(&cid);
                let needs_tounicode =
                    !g_text.is_empty() && existing_unicode.as_deref() != Some(g_text);
                if needs_w || needs_w2 || needs_tounicode {
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "deduplicating shaped u16 glyph IDs bounded by ShapedGlyphCount"
                    )]
                    let inserted = planned_gids.insert(gid);
                    if inserted {
                        extensions.try_push(GlyphExtension {
                            gid,
                            width: h_width,
                            v_metrics: if self.vertical { v_entry } else { None },
                            text: g_text.to_owned(),
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
        if !missing.is_empty() {
            segments.try_push(EncodedSegment::Missing { text: missing })?;
        }
        Ok(())
    }
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

    // CharProcs dictionary.
    let mut char_procs: BTreeMap<Vec<u8>, Option<f64>> = BTreeMap::new();
    if let Some(val) = resolve_entry(version, font_dict, "CharProcs", budget)?
        && let Value::Dict(procs) = &*val
    {
        for (name, value) in procs.iter() {
            budget.poll()?;
            let d0_width = match resolve_value(version, value, budget)? {
                Some(stream_val) => match &*stream_val {
                    Value::Stream(stream) => stream
                        .decoded(budget)
                        .ok()
                        .and_then(|bytes| parse_type3_d0_width(&bytes)),
                    _ => None,
                },
                None => None,
            };
            budget.charge(LimitCode::MemorySize, 64)?;
            #[expect(
                clippy::disallowed_methods,
                reason = "bounded by entries of CharProcs dictionary"
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

    // Optional `/ToUnicode` stream.
    let mut to_unicode: BudgetVec<'_, (u8, String)> = BudgetVec::new(budget);
    if let Some(val) = resolve_entry(version, font_dict, "ToUnicode", budget)?
        && let Value::Stream(stream) = &*val
    {
        let data = stream.decoded(budget)?;
        let n = data.len() as u64;
        budget.check(LimitCode::FontInputSize, n)?;
        budget.charge(LimitCode::InterpretationFuel, n)?;
        let tables = parse_cmap_tables(&data, budget)?;
        for (_len, code, text) in tables.bf_entries {
            if code <= 255 && !text.is_empty() && text != "\0" {
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
    chain: Vec<CMap>,
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
                    chain: vec![CMap::identity_h()],
                    codespaces: vec![CodespaceRange {
                        len: 2,
                        low: 0,
                        high: 0xFFFF,
                    }],
                    cid_ranges: Vec::new(),
                })),
                CMapName::IdentityV => Ok(Some(LoadedEncoding {
                    vertical: true,
                    kind: EncodingKind::Identity,
                    chain: vec![CMap::identity_v()],
                    codespaces: vec![CodespaceRange {
                        len: 2,
                        low: 0,
                        high: 0xFFFF,
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
                        chain: vec![cmap],
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
            let tables = parse_cmap_tables(&data, budget)?;
            let parsed = CMap::parse(&data, load_embedded);
            let dict_wmode = int_of(stream.dict().get("WMode"));
            let mut vertical = dict_wmode == Some(1)
                || tables.wmode == Some(1)
                || parsed
                    .as_ref()
                    .is_some_and(|c| c.metadata().writing_mode == Some(WritingMode::Vertical));
            let mut chain: BudgetVec<'_, CMap> = BudgetVec::new(budget);
            if let Some(cmap) = parsed {
                chain.try_push(cmap)?;
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
                for cmap in base.chain {
                    chain.try_push(cmap)?;
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
                chain: chain.into_vec(),
                codespaces,
                cid_ranges,
            }))
        }
        _ => Ok(None),
    }
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
        let is_cid_keyed_cff = font
            .table_data(skrifa::Tag::new(b"CFF "))
            .and_then(|t| CffFontRef::new_cff(t.as_bytes(), 0, None).ok())
            .is_some_and(|cff| cff.is_cid());
        let editable_face = Face::read(&data, 0).ok().filter(|f| f.outlines);
        return Ok((
            ProgramState::Sfnt {
                data,
                is_cid_keyed_cff,
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

fn sfnt_cff_glyph_by_cid(data: &[u8], cid: u32) -> Option<u32> {
    let font = FontRef::new(data).ok()?;
    let cff_table = font.table_data(skrifa::Tag::new(b"CFF "))?;
    bare_cff_glyph_by_cid(cff_table.as_bytes(), cid)
}

fn bare_cff_glyph_by_cid(data: &[u8], cid: u32) -> Option<u32> {
    let cff = CffFontRef::new_cff(data, 0, None).ok()?;
    let target = u16::try_from(cid).ok()?;
    let charset = cff.charset()?;
    charset
        .iter()
        .find_map(|(gid, sid)| (sid.to_u16() == target).then(|| gid.to_u32()))
}

fn sfnt_glyph_has_ink(data: &[u8], gid: u32) -> bool {
    let Ok(font) = FontRef::new(data) else {
        return false;
    };
    let outlines = OutlineGlyphCollection::new(&font);
    let Some(outline) = outlines.get(GlyphId::new(gid)) else {
        return false;
    };
    let mut pen = HasContourPen::default();
    let settings = DrawSettings::unhinted(Size::unscaled(), LocationRef::default());
    outline.draw(settings, &mut pen).is_ok() && pen.has_ink
}

fn bare_cff_glyph_has_ink(data: &[u8], gid: u32) -> bool {
    let Ok(cff) = CffFontRef::new_cff(data, 0, None) else {
        return false;
    };
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

fn parse_widths(arr: &Array, budget: &Budget) -> StoreResult<(Vec<WidthRange>, BTreeSet<u32>)> {
    let mut ranges: BudgetVec<'_, WidthRange> = BudgetVec::new(budget);
    let mut explicit = BTreeSet::new();
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
                        if explicit.len() <= 65_536 {
                            #[expect(
                                clippy::disallowed_methods,
                                reason = "capped at 65536 CIDs (u16 range)"
                            )]
                            explicit.insert(cid);
                        }
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
                    if last - first <= 256 && explicit.len() <= 65_536 {
                        for cid in first..=last {
                            #[expect(
                                clippy::disallowed_methods,
                                reason = "small explicit range, capped at 65536 CIDs"
                            )]
                            explicit.insert(cid);
                        }
                    }
                }
            }
            None => break,
        }
    }
    Ok((ranges.into_vec(), explicit))
}

fn parse_widths2(arr: &Array, budget: &Budget) -> StoreResult<(Vec<Width2Range>, BTreeSet<u32>)> {
    let mut ranges: BudgetVec<'_, Width2Range> = BudgetVec::new(budget);
    let mut explicit = BTreeSet::new();
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
                        if explicit.len() <= 65_536 {
                            #[expect(
                                clippy::disallowed_methods,
                                reason = "capped at 65536 CIDs (u16 range)"
                            )]
                            explicit.insert(cid);
                        }
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
                    if last - first <= 256 && explicit.len() <= 65_536 {
                        for cid in first..=last {
                            #[expect(
                                clippy::disallowed_methods,
                                reason = "small explicit range, capped at 65536 CIDs"
                            )]
                            explicit.insert(cid);
                        }
                    }
                }
            }
            None => break,
        }
    }
    Ok((ranges.into_vec(), explicit))
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
/// `beginbfchar`, `beginbfrange`, and `/WMode` from a PDF CMap stream.
fn parse_cmap_tables(data: &[u8], budget: &Budget) -> StoreResult<ParsedCMapTables> {
    let mut codespaces: BudgetVec<'_, CodespaceRange> = BudgetVec::new(budget);
    let mut cid_ranges: BudgetVec<'_, CidMapRange> = BudgetVec::new(budget);
    let mut bf_entries: BudgetVec<'_, (u8, u32, String)> = BudgetVec::new(budget);
    let mut wmode = None;
    let mut expanded_bf = 0_u64;

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
            let Some(object) = input.read_without_context::<Object<'_>>() else {
                break;
            };
            if let Object::Name(name) = object
                && name.as_ref() == b"WMode"
            {
                input.skip_white_spaces_and_comments();
                if let Some(v) = input.read_without_context::<i32>()
                    && (v == 0 || v == 1)
                {
                    wmode = Some(v as u8);
                }
            }
            continue;
        }
        let start = input.offset();
        input.forward_while(|b| !b.is_ascii_whitespace() && !b"()<>[]{}/%\0".contains(&b));
        if input.offset() == start {
            input.forward();
            continue;
        }
        match &data[start..input.offset()] {
            b"begincodespacerange" => {
                parse_codespace_block(&mut input, data, &mut codespaces, budget)?;
            }
            b"begincidchar" => {
                parse_cidchar_block(&mut input, data, &mut cid_ranges, budget)?;
            }
            b"begincidrange" => {
                parse_cidrange_block(&mut input, data, &mut cid_ranges, budget)?;
            }
            b"beginbfchar" => {
                parse_bfchar_block(&mut input, data, &mut bf_entries, &mut expanded_bf, budget)?;
            }
            b"beginbfrange" => {
                parse_bfrange_block(&mut input, data, &mut bf_entries, &mut expanded_bf, budget)?;
            }
            _ => {}
        }
    }

    Ok(ParsedCMapTables {
        codespaces: codespaces.into_vec(),
        cid_ranges: cid_ranges.into_vec(),
        bf_entries: bf_entries.into_vec(),
        wmode,
    })
}

fn read_cmap_code(input: &mut SyntaxReader<'_>) -> Option<(u8, u32)> {
    input.skip_white_spaces_and_comments();
    let s = input.read_without_context::<SyntaxString>()?;
    let bytes = s.as_bytes();
    if !(1..=4).contains(&bytes.len()) {
        return None;
    }
    let val = bytes
        .iter()
        .fold(0_u32, |acc, &b| (acc << 8) | u32::from(b));
    Some((bytes.len() as u8, val))
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
        let Some((len1, low)) = read_cmap_code(input) else {
            break;
        };
        let Some((len2, high)) = read_cmap_code(input) else {
            break;
        };
        if len1 == len2 && low <= high {
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
        out.try_push(CidMapRange {
            len,
            start: code,
            end: code,
            cid_start: cid,
        })?;
    }
    Ok(())
}

fn parse_cidrange_block(
    input: &mut SyntaxReader<'_>,
    data: &[u8],
    out: &mut BudgetVec<'_, CidMapRange>,
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
            out.try_push(CidMapRange {
                len: len1,
                start,
                end,
                cid_start,
            })?;
        }
    }
    Ok(())
}

fn parse_bfchar_block(
    input: &mut SyntaxReader<'_>,
    data: &[u8],
    out: &mut BudgetVec<'_, (u8, u32, String)>,
    expanded: &mut u64,
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
        let Some(dst) = input.read_without_context::<SyntaxString>() else {
            break;
        };
        *expanded = expanded.saturating_add(1);
        if *expanded > MAX_CMAP_EXPAND {
            return Err(StoreError::from(
                LimitExceeded::new(LimitCode::InterpretationFuel)
                    .with_limit(MAX_CMAP_EXPAND)
                    .with_amount(*expanded),
            ));
        }
        if let Some(text) = decode_utf16be(dst.as_bytes()) {
            out.try_push((len, code, text))?;
        }
    }
    Ok(())
}

fn parse_bfrange_block(
    input: &mut SyntaxReader<'_>,
    data: &[u8],
    out: &mut BudgetVec<'_, (u8, u32, String)>,
    expanded: &mut u64,
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
            // Consume the destination operand before stopping.
            let _ = input.read_without_context::<Object<'_>>();
            continue;
        }
        let count = u64::from(end - start) + 1;
        *expanded = expanded.saturating_add(count);
        if *expanded > MAX_CMAP_EXPAND {
            return Err(StoreError::from(
                LimitExceeded::new(LimitCode::InterpretationFuel)
                    .with_limit(MAX_CMAP_EXPAND)
                    .with_amount(*expanded),
            ));
        }
        budget.charge(LimitCode::InterpretationFuel, count)?;
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
                if let Some(text) = decode_utf16be(s.as_bytes()) {
                    out.try_push((len1, start + i as u32, text))?;
                }
            }
        } else {
            let Some(dst) = input.read_without_context::<SyntaxString>() else {
                break;
            };
            let Some(mut units) = utf16be_units(dst.as_bytes()) else {
                continue;
            };
            if units.is_empty() {
                continue;
            }
            let base_last = *units.last().unwrap_or(&0);
            for offset in 0..count {
                if offset.is_multiple_of(256) {
                    budget.poll()?;
                }
                let Some(next_last) = u16::try_from(offset)
                    .ok()
                    .and_then(|o| base_last.checked_add(o))
                else {
                    break;
                };
                if let Some(last) = units.last_mut() {
                    *last = next_last;
                }
                if let Ok(text) = String::from_utf16(&units) {
                    out.try_push((len1, start + offset as u32, text))?;
                }
            }
        }
    }
    Ok(())
}

fn utf16be_units(bytes: &[u8]) -> Option<Vec<u16>> {
    if bytes.is_empty() || !bytes.len().is_multiple_of(2) {
        return None;
    }
    #[expect(
        clippy::disallowed_methods,
        reason = "one u16 per 2 bytes of a single CMap bf string"
    )]
    Some(
        bytes
            .as_chunks::<2>()
            .0
            .iter()
            .map(|&pair| u16::from_be_bytes(pair))
            .collect(),
    )
}

fn decode_utf16be(bytes: &[u8]) -> Option<String> {
    let units = utf16be_units(bytes)?;
    String::from_utf16(&units).ok()
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
            if let Some(Value::Name(base)) = dict.get("BaseEncoding") {
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

/// Populates standard PDF simple base encodings for Type 3 `/Encoding` lookup.
fn apply_base_encoding(base: &[u8], names: &mut [Option<Vec<u8>>; 256]) {
    if base == b"WinAnsiEncoding" {
        for (code, name) in WINANSI_NAMES.iter().enumerate() {
            if let Some(glyph) = name {
                #[expect(clippy::disallowed_methods, reason = "fixed 256-slot table")]
                {
                    names[code] = Some(glyph.as_bytes().to_vec());
                }
            }
        }
    } else if base == b"StandardEncoding" || base == b"MacRomanEncoding" {
        // Basic ASCII printable names shared by Standard/MacRoman; custom
        // Type 3 fonts almost always supply `/Differences`.
        for (code, name) in WINANSI_NAMES.iter().enumerate().take(127) {
            if let Some(glyph) = name {
                #[expect(clippy::disallowed_methods, reason = "fixed 256-slot table")]
                {
                    names[code] = Some(glyph.as_bytes().to_vec());
                }
            }
        }
    }
}

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
}
