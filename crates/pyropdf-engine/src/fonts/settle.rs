//! The settler of [`Structure::FontSubset`]: at save, the Type0 font of an
//! embedded face gets a program compacted to the glyphs of its `W` array,
//! which lists every glyph that the document drew with it, and its names
//! a subset tag (ISO 32000-2 9.9, 9.6.4). The save settles in a scratch
//! transaction, so the document keeps the whole program for later drawing.
//!
//! [`Structure::FontSubset`]: pyropdf_store::settle::Structure::FontSubset

use pyropdf_store::Result;
use pyropdf_store::budget::{Budget, BudgetVec};
use pyropdf_store::settle::Settler;
use pyropdf_store::value::{Bytes, Dict, Name, Reference, Stream, Value};
use pyropdf_store::version::{Transaction, Version};

use super::compact::{Compact, compact};

/// The settler of [`Structure::FontSubset`](pyropdf_store::settle::Structure::FontSubset).
#[derive(Clone, Copy, Debug, Default)]
pub struct FontSubsetSettler;

impl Settler for FontSubsetSettler {
    fn settle(
        &self,
        transaction: &mut Transaction,
        numbers: &[u32],
        budget: &Budget,
    ) -> Result<()> {
        if numbers.is_empty() {
            return Ok(());
        }
        let target = Target::of(transaction.version(), budget)?;
        // Multiple Type0 fonts (for instance `Identity-H` and `Identity-V` of
        // the same face) can share one CIDFont descendant and program. Group
        // them by descendant reference so the program is compacted once and
        // every Type0 dictionary receives the same subset tag.
        let mut groups: BudgetVec<'_, FontGroup> = BudgetVec::new(budget);
        for &number in numbers {
            let Some(font) = Font::read(transaction.version(), number, budget)? else {
                continue;
            };
            if let Some(group) = groups
                .iter_mut()
                .find(|group| group.primary.descendant.0 == font.descendant.0)
            {
                if group.primary.number != font.number
                    && group
                        .extra_type0s
                        .iter()
                        .all(|(existing, _)| *existing != font.number)
                {
                    group.extra_type0s.try_push((font.number, font.font))?;
                }
            } else {
                groups.try_push(FontGroup {
                    primary: font,
                    extra_type0s: BudgetVec::new(budget),
                })?;
            }
        }
        for group in groups.into_vec() {
            group.subset(transaction, target, budget)?;
        }
        Ok(())
    }
}

/// One or more Type0 fonts sharing a single CIDFont descendant and program.
struct FontGroup<'b> {
    primary: Font,
    extra_type0s: BudgetVec<'b, (u32, Dict)>,
}

impl FontGroup<'_> {
    fn subset(self, transaction: &mut Transaction, target: Target, budget: &Budget) -> Result<()> {
        let tagged = self.primary.subset(transaction, target, budget)?;
        if let Some(tagged) = tagged {
            for (number, mut font) in self.extra_type0s.into_vec() {
                if let Some(Value::Name(base)) = font.get("BaseFont")
                    && has_tag(base.as_bytes())
                {
                    continue;
                }
                #[expect(
                    clippy::disallowed_methods,
                    reason = "the dictionary grows by one entry at most"
                )]
                font.insert(Name::from("BaseFont"), tagged.clone());
                transaction.write(number, &Value::Dict(font), budget)?;
            }
        }
        Ok(())
    }
}

/// Version and conformance facts of the saved document that decide subset
/// metadata.
#[derive(Clone, Copy)]
struct Target {
    /// Whether a CIDFont subset descriptor needs `/CIDSet` (ISO 19005-1
    /// 6.3.5); PDF 2.0 deprecates `/CIDSet`, so PDF 2.0+ never writes it.
    cid_set: bool,
    /// Whether `FontFile3` allows `/Subtype /OpenType` (PDF 1.6+).
    open_type: bool,
}

impl Target {
    fn of(version: &Version, budget: &Budget) -> Result<Self> {
        let mut pdf = version
            .base()
            .header_bytes()
            .and_then(|bytes| bytes.strip_prefix(b"%PDF-"))
            .and_then(parse_version)
            .unwrap_or((1, 7));
        let catalog = match reference(version.trailer().get("Root")) {
            Some(root) => dict_of(version, root, budget)?,
            None => None,
        };
        if let Some(Value::Name(override_version)) =
            catalog.as_ref().and_then(|dict| dict.get("Version"))
            && let Some(parsed) = parse_version(override_version.as_bytes())
        {
            pdf = pdf.max(parsed);
        }
        let cid_set = if pdf < (2, 0) && catalog.is_some() {
            crate::changes::pdfa_part(version, budget)? == Some(1)
        } else {
            false
        };
        Ok(Self {
            cid_set,
            open_type: pdf >= (1, 6),
        })
    }
}

/// The objects of a Type0 font that pyropdf wrote for an embedded face.
struct Font {
    number: u32,
    font: Dict,
    descendant: (Reference, Dict),
    descriptor: (Reference, Dict),
    program: (Reference, Vec<u8>),
    glyphs: Vec<u32>,
}

/// Returns the dictionary of the indirect object `reference`.
fn dict_of(version: &Version, reference: Reference, budget: &Budget) -> Result<Option<Dict>> {
    let Some(object) = version
        .get(reference.number(), budget)?
        .filter(|object| object.generation() == reference.generation())
    else {
        return Ok(None);
    };
    Ok(match &**object.value() {
        Value::Dict(dict) => Some(dict.clone()),
        _ => None,
    })
}

fn reference(value: Option<&Value>) -> Option<Reference> {
    match value {
        Some(Value::Reference(reference)) => Some(*reference),
        _ => None,
    }
}

fn is_name(value: Option<&Value>, name: &[u8]) -> bool {
    matches!(value, Some(Value::Name(value)) if value.as_bytes() == name)
}

impl Font {
    fn read(version: &Version, number: u32, budget: &Budget) -> Result<Option<Self>> {
        let Some(object) = version.get(number, budget)? else {
            return Ok(None);
        };
        let Value::Dict(font) = &**object.value() else {
            return Ok(None);
        };
        if !is_name(font.get("Subtype"), b"Type0")
            || !(is_name(font.get("Encoding"), b"Identity-H")
                || is_name(font.get("Encoding"), b"Identity-V"))
        {
            return Ok(None);
        }
        let descendant = match font.get("DescendantFonts") {
            Some(Value::Array(array)) if array.len() == 1 => reference(array.get(0)),
            _ => None,
        };
        let Some(descendant) = descendant else {
            return Ok(None);
        };
        let Some(cid_font) = dict_of(version, descendant, budget)? else {
            return Ok(None);
        };
        let truetype = is_name(cid_font.get("Subtype"), b"CIDFontType2");
        if truetype && !is_name(cid_font.get("CIDToGIDMap"), b"Identity")
            || !truetype && !is_name(cid_font.get("Subtype"), b"CIDFontType0")
        {
            return Ok(None);
        }
        let Some(descriptor) = reference(cid_font.get("FontDescriptor")) else {
            return Ok(None);
        };
        let Some(descriptor_dict) = dict_of(version, descriptor, budget)? else {
            return Ok(None);
        };
        let key = if truetype { "FontFile2" } else { "FontFile3" };
        let Some(program) = reference(descriptor_dict.get(key)) else {
            return Ok(None);
        };
        let Some(stream) = version
            .get(program.number(), budget)?
            .filter(|object| object.generation() == program.generation())
        else {
            return Ok(None);
        };
        let Value::Stream(stream) = &**stream.value() else {
            return Ok(None);
        };
        if !truetype && !is_name(stream.dict().get("Subtype"), b"OpenType") {
            return Ok(None);
        }
        let data = stream.decoded(budget)?;
        let Some(glyphs) = cid_font_glyphs(&cid_font) else {
            return Ok(None);
        };
        Ok(Some(Self {
            number,
            font: font.clone(),
            descendant: (descendant, cid_font),
            descriptor: (descriptor, descriptor_dict),
            program: (program, data),
            glyphs,
        }))
    }

    fn subset(
        self,
        transaction: &mut Transaction,
        target: Target,
        budget: &Budget,
    ) -> Result<Option<Value>> {
        let base = match self.font.get("BaseFont") {
            #[expect(
                clippy::disallowed_methods,
                reason = "a copy of the font's BaseFont, which the version holds"
            )]
            Some(Value::Name(name)) => name.as_bytes().to_vec(),
            _ => return Ok(None),
        };
        // A font that a save already tagged is a subset already.
        if has_tag(&base) {
            return Ok(None);
        }
        let name = String::from_utf8_lossy(&base);
        let Some(compact) = compact(&self.program.1, &self.glyphs, &name) else {
            return Ok(None);
        };
        // Before PDF 1.6, a CFF face's provisional `/OpenType` stream has no
        // valid subtype, so it still converts to `CIDFontType0C`; PDF/A-1 also
        // needs `/CIDSet` on the subset descriptor.
        if (!compact.cff || target.open_type)
            && !target.cid_set
            && compact.data.len() >= self.program.1.len()
        {
            return Ok(None);
        }
        let mut tagged = tag(&compact).into_bytes();
        #[expect(
            clippy::disallowed_methods,
            reason = "the tag's seven bytes, then the BaseFont copied above"
        )]
        tagged.extend_from_slice(&base);
        let tagged = Value::Name(Name::new(tagged));

        let (program, data) = (self.program.0, compact.data.clone());
        let mut program_dict = Dict::new();
        if compact.cff {
            #[expect(
                clippy::disallowed_methods,
                reason = "the dictionary grows by one entry at most"
            )]
            program_dict.insert(
                Name::from("Subtype"),
                Value::Name(Name::from("CIDFontType0C")),
            );
        } else {
            #[expect(
                clippy::disallowed_methods,
                reason = "the dictionary grows by one entry at most"
            )]
            program_dict.insert(
                Name::from("Length1"),
                Value::Integer(i64::try_from(data.len()).unwrap_or(i64::MAX)),
            );
        }
        let stream = Stream::new(program_dict, Bytes::from(data));
        transaction.write(program.number(), &Value::from(stream), budget)?;

        let (descriptor, mut descriptor_dict) = self.descriptor;
        #[expect(
            clippy::disallowed_methods,
            reason = "the dictionary grows by one entry at most"
        )]
        descriptor_dict.insert(Name::from("FontName"), tagged.clone());
        if target.cid_set {
            let set = cid_set(&compact.glyphs);
            let number = transaction.number_for_save()?;
            let stream = Stream::new(Dict::new(), Bytes::from(set));
            let reference = transaction.write(number, &Value::from(stream), budget)?;
            #[expect(
                clippy::disallowed_methods,
                reason = "the dictionary grows by one entry at most"
            )]
            descriptor_dict.insert(Name::from("CIDSet"), Value::Reference(reference));
        }
        transaction.write(descriptor.number(), &Value::Dict(descriptor_dict), budget)?;

        let (descendant, mut cid_font) = self.descendant;
        #[expect(
            clippy::disallowed_methods,
            reason = "the dictionary grows by one entry at most"
        )]
        cid_font.insert(Name::from("BaseFont"), tagged.clone());
        if !compact.cff {
            let map = cid_to_gid(&compact.glyphs);
            // The same number at each save of one version, so that two saves
            // with no edit between them write the same bytes.
            let number = transaction.number_for_save()?;
            let stream = Stream::new(Dict::new(), Bytes::from(map));
            let reference = transaction.write(number, &Value::from(stream), budget)?;
            #[expect(
                clippy::disallowed_methods,
                reason = "the dictionary grows by one entry at most"
            )]
            cid_font.insert(Name::from("CIDToGIDMap"), Value::Reference(reference));
        }
        transaction.write(descendant.number(), &Value::Dict(cid_font), budget)?;

        let mut font = self.font;
        #[expect(
            clippy::disallowed_methods,
            reason = "the dictionary grows by one entry at most"
        )]
        font.insert(Name::from("BaseFont"), tagged.clone());
        transaction.write(self.number, &Value::Dict(font), budget)?;
        Ok(Some(tagged))
    }
}

struct GlyphSweep {
    edges: [u64; 1024],
    full_ends: [u16; 1024],
    first_used: usize,
    end_used: usize,
}

impl GlyphSweep {
    fn new() -> Self {
        Self {
            edges: [0_u64; 1024],
            full_ends: [0_u16; 1024],
            first_used: 1024,
            end_used: 0,
        }
    }

    fn mark(&mut self, first: u32, end: u32) {
        if first >= end {
            return;
        }
        let last = end - 1;
        let (first_word, last_word) =
            mark_glyph_range(&mut self.edges, &mut self.full_ends, first, last);
        self.first_used = self.first_used.min(first_word);
        self.end_used = self.end_used.max(last_word + 1);
    }

    #[expect(
        clippy::disallowed_methods,
        reason = "16-bit glyph IDs, so the result holds 65,536 glyphs at most"
    )]
    fn finish(self) -> Vec<u32> {
        let mut glyphs = Vec::new();
        let mut full_end = 0;
        for word in self.first_used..self.end_used {
            full_end = full_end.max(usize::from(self.full_ends[word]));
            let mut bits = if word < full_end {
                u64::MAX
            } else {
                self.edges[word]
            };
            while bits != 0 {
                glyphs.push(word as u32 * 64 + bits.trailing_zeros());
                bits &= bits - 1;
            }
        }
        glyphs
    }
}

fn glyph_id(value: Option<&Value>) -> Option<u32> {
    match value {
        Some(&Value::Integer(glyph)) => u16::try_from(glyph).ok().map(u32::from),
        _ => None,
    }
}

fn sweep_w(value: &Value, sweep: &mut GlyphSweep) -> Option<()> {
    let Value::Array(array) = value else {
        return None;
    };
    let mut at = 0;
    while at < array.len() {
        let first = glyph_id(array.get(at))?;
        let end = match array.get(at + 1)? {
            Value::Array(widths) => {
                let last = first.checked_add(u32::try_from(widths.len()).ok()?)?;
                at += 2;
                last.min(u32::from(u16::MAX) + 1)
            }
            _ => {
                let last = glyph_id(array.get(at + 1))?;
                at += 3;
                last + 1
            }
        };
        sweep.mark(first, end);
    }
    Some(())
}

fn sweep_w2(value: &Value, sweep: &mut GlyphSweep) -> Option<()> {
    let Value::Array(array) = value else {
        return None;
    };
    let mut at = 0;
    while at < array.len() {
        let first = glyph_id(array.get(at))?;
        let end = match array.get(at + 1)? {
            Value::Array(metrics) => {
                if !metrics.len().is_multiple_of(3) {
                    return None;
                }
                let count = u32::try_from(metrics.len() / 3).ok()?;
                let last = first.checked_add(count)?;
                at += 2;
                last.min(u32::from(u16::MAX) + 1)
            }
            _ => {
                let last = glyph_id(array.get(at + 1))?;
                at += 5;
                last + 1
            }
        };
        sweep.mark(first, end);
    }
    Some(())
}

/// Returns the union of glyphs named by a CIDFont's `W` and/or `W2` arrays,
/// or `None` when neither is present or either present entry is malformed.
fn cid_font_glyphs(cid_font: &Dict) -> Option<Vec<u32>> {
    let w = cid_font.get("W");
    let w2 = cid_font.get("W2");
    if w.is_none() && w2.is_none() {
        return None;
    }
    let mut sweep = GlyphSweep::new();
    if let Some(value) = w {
        sweep_w(value, &mut sweep)?;
    }
    if let Some(value) = w2 {
        sweep_w2(value, &mut sweep)?;
    }
    Some(sweep.finish())
}

/// Returns the glyphs that the `W` array `value` gives widths, in its
/// `c [w1 w2 ...]` and `c_first c_last w` forms (ISO 32000-2 9.7.4.3),
/// or `None` when it is not an array of those.
#[cfg(test)]
fn widths_glyphs(value: &Value) -> Option<Vec<u32>> {
    let mut sweep = GlyphSweep::new();
    sweep_w(value, &mut sweep)?;
    Some(sweep.finish())
}

/// Returns the glyphs that the `W2` array `value` gives vertical metrics, in
/// its `c [w1y vx vy ...]` and `c_first c_last w1y vx vy` forms (ISO 32000-2
/// 9.7.4.3), or `None` when it is not an array of those.
#[cfg(test)]
fn widths2_glyphs(value: &Value) -> Option<Vec<u32>> {
    let mut sweep = GlyphSweep::new();
    sweep_w2(value, &mut sweep)?;
    Some(sweep.finish())
}

/// Returns the `CIDSet` bitmap of a subset whose CIDs are `glyphs`
/// (PDF 1.4 Reference, table 5.20): high-order bit first, CID 0 at the
/// most significant bit of byte 0.
fn cid_set(glyphs: &[u16]) -> Vec<u8> {
    let bytes = glyphs
        .iter()
        .max()
        .map_or(0, |&last| usize::from(last) / 8 + 1);
    #[expect(
        clippy::disallowed_methods,
        reason = "one bit per 16-bit CID, 8,192 bytes at most"
    )]
    let mut set = vec![0_u8; bytes];
    for &cid in glyphs {
        let cid = usize::from(cid);
        set[cid / 8] |= 0x80 >> (cid % 8);
    }
    set
}

fn parse_version(bytes: &[u8]) -> Option<(u8, u8)> {
    let end = bytes
        .iter()
        .position(|&byte| !(byte.is_ascii_digit() || byte == b'.'))
        .unwrap_or(bytes.len());
    let text = std::str::from_utf8(&bytes[..end]).ok()?;
    let (major, minor) = text.split_once('.')?;
    Some((major.parse().ok()?, minor.parse().ok()?))
}

/// Returns the CIDToGIDMap of a compacted TrueType program: for each CID,
/// the old glyph ID, its new glyph ID, two bytes each.
fn cid_to_gid(glyphs: &[u16]) -> Vec<u8> {
    let cids = glyphs.iter().max().map_or(0, |&last| usize::from(last) + 1);
    #[expect(
        clippy::disallowed_methods,
        reason = "two bytes per 16-bit glyph ID, 128 KiB at most"
    )]
    let mut map = vec![0; 2 * cids];
    for (new, &old) in glyphs.iter().enumerate() {
        let at = 2 * usize::from(old);
        map[at..at + 2].copy_from_slice(&u16::try_from(new).unwrap_or(0).to_be_bytes());
    }
    map
}

fn has_tag(name: &[u8]) -> bool {
    name.len() > 7 && name[6] == b'+' && name[..6].iter().all(u8::is_ascii_uppercase)
}

/// Returns the subset tag of `compact`: six capital letters from a hash of
/// the program, so that the same subset gets the same tag.
#[expect(clippy::disallowed_methods, reason = "the tag's seven characters")]
fn tag(compact: &Compact) -> String {
    // FNV-1a, whose value does not change between builds.
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for &byte in &compact.data {
        hash = (hash ^ u64::from(byte)).wrapping_mul(0x0100_0000_01b3);
    }
    let mut tag = String::with_capacity(7);
    for _ in 0..6 {
        tag.push(char::from(b'A' + u8::try_from(hash % 26).unwrap_or(0)));
        hash /= 26;
    }
    tag.push('+');
    tag
}

/// Marks the inclusive glyph interval `first..=last` in the interval-sweep
/// bitset (`edges`, `full_ends`), returning `(first_word, last_word)`.
fn mark_glyph_range(
    edges: &mut [u64],
    full_ends: &mut [u16],
    first: u32,
    last: u32,
) -> (usize, usize) {
    let first_word = (first / 64) as usize;
    let last_word = (last / 64) as usize;
    let first_bits = u64::MAX << (first % 64);
    let last_bits = u64::MAX >> (63 - last % 64);
    if first_word == last_word {
        edges[first_word] |= first_bits & last_bits;
    } else {
        edges[first_word] |= first_bits;
        edges[last_word] |= last_bits;
        full_ends[first_word + 1] = full_ends[first_word + 1].max(last_word as u16);
    }
    (first_word, last_word)
}

#[cfg(test)]
mod tests;

#[cfg(kani)]
mod kani_proofs {
    use super::*;

    /// The interval-sweep bitset used by `widths_glyphs` produces the exact
    /// same bitmask as setting each glyph bit individually for any pair of
    /// overlapping ranges across four 64-bit words (256 glyphs).
    #[kani::proof]
    #[kani::unwind(6)]
    fn kani_widths_range_sweep_matches_naive() {
        let mut edges = [0_u64; 4];
        let mut full_ends = [0_u16; 4];
        let mut naive = [0_u64; 4];

        let f1: u32 = kani::any();
        let l1: u32 = kani::any();
        let f2: u32 = kani::any();
        let l2: u32 = kani::any();
        kani::assume(f1 <= l1 && l1 < 256);
        kani::assume(f2 <= l2 && l2 < 256);

        mark_glyph_range(&mut edges, &mut full_ends, f1, l1);
        mark_glyph_range(&mut edges, &mut full_ends, f2, l2);

        for word in 0..4_u32 {
            let base = word * 64;
            let top = base + 63;
            for (f, l) in [(f1, l1), (f2, l2)] {
                if l >= base && f <= top {
                    let lo = f.max(base) - base;
                    let hi = l.min(top) - base;
                    naive[word as usize] |= (u64::MAX << lo) & (u64::MAX >> (63 - hi));
                }
            }
        }

        let mut full_end = 0_usize;
        for word in 0..4_usize {
            full_end = full_end.max(usize::from(full_ends[word]));
            let bits = if word < full_end {
                u64::MAX
            } else {
                edges[word]
            };
            assert_eq!(bits, naive[word]);
        }
    }
}
