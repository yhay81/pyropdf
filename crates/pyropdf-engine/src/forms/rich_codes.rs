//! Reverse an existing horizontal Type 0 encoding to the exact PDF character
//! codes selecting shaped outline IDs. A PDF character code, CID and GID are
//! three different quantities.
use super::{WidgetError, malformed};
use crate::annotations::Reader;
use crate::fonts::embed::Face;
use hayro::hayro_syntax::object::{Object, String as PdfString};
use hayro::hayro_syntax::reader::{Reader as SyntaxReader, ReaderExt};
use hayro_font::hayro_cmap::{BfString, CMap};
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::value::{Dict, Value};
use skrifa::Tag;
use skrifa::raw::ps::cff::CffFontRef;

#[derive(Clone, Copy, PartialOrd, Ord, PartialEq, Eq)]
pub(super) struct Code {
    pub(super) length: u8,
    pub(super) value: u32,
}

impl Code {
    fn read(input: &mut SyntaxReader<'_>) -> Option<Self> {
        input.skip_white_spaces_and_comments();
        let string = input.read_without_context::<PdfString>()?;
        let bytes = string.as_bytes();
        if !(1..=4).contains(&bytes.len()) {
            return None;
        }
        Some(Self {
            length: bytes.len() as u8,
            value: bytes.iter().fold(0, |n, b| n << 8 | u32::from(*b)),
        })
    }
}

pub(super) struct Codes {
    by_glyph: Vec<(u32, Code)>,
    unicode: Option<CMap>,
}

impl Codes {
    pub(super) fn read_unicode(
        &mut self,
        reader: &Reader<'_>,
        font: &Dict,
    ) -> Result<(), WidgetError> {
        let value = reader.entry(font, "ToUnicode")?;
        if let Some(Value::Stream(stream)) = value.as_deref() {
            let data = match stream.decoded(reader.budget) {
                Ok(data) => data,
                // A broken extraction map does not invalidate a drawable
                // outline. The appearance can supply its own ActualText.
                Err(error) if error.limit().is_none() => return Ok(()),
                Err(error) => return Err(error.into()),
            };
            let n = data.len() as u64;
            reader.budget.check(LimitCode::FontInputSize, n)?;
            reader
                .budget
                .charge(LimitCode::MemorySize, n.saturating_mul(16))?;
            reader.budget.charge(LimitCode::InterpretationFuel, n)?;
            self.unicode = CMap::parse(&data, |_| None);
        }
        Ok(())
    }

    /// Prefer the original code whose ToUnicode text matches this glyph's
    /// shaping cluster. Some encodings map multiple codes to the same outline.
    pub(super) fn select(
        &self,
        glyph: u32,
        text: &str,
        budget: &Budget,
    ) -> Result<Option<(Code, bool)>, WidgetError> {
        let index = self.by_glyph.partition_point(|(id, _)| *id < glyph);
        let mut first = None;
        for &(_, code) in self.by_glyph[index..]
            .iter()
            .take_while(|(id, _)| *id == glyph)
        {
            first.get_or_insert(code);
            let Some(unicode) = &self.unicode else { break };
            budget.charge(LimitCode::InterpretationFuel, 1)?;
            let equal = match unicode.lookup_bf_string(code.value) {
                Some(BfString::Char(ch)) => text == ch.encode_utf8(&mut [0; 4]),
                Some(BfString::String(string)) => string == text,
                None => false,
            };
            if equal {
                return Ok(Some((code, true)));
            }
        }
        Ok(first.map(|c| (c, false)))
    }
    pub(super) fn code(&self, glyph: u32) -> Option<Code> {
        let index = self.by_glyph.partition_point(|(id, _)| *id < glyph);
        self.by_glyph
            .get(index)
            .and_then(|&(id, code)| (id == glyph).then_some(code))
    }
}

enum GlyphMap<'a> {
    Identity(u32),
    Map(&'a [u8]),
    Cff(&'a [(u16, u32)]),
}

impl GlyphMap<'_> {
    fn glyph(&self, cid: u16) -> Option<u32> {
        match self {
            Self::Identity(count) => (u32::from(cid) < *count).then(|| u32::from(cid)),
            Self::Map(bytes) => {
                let p = usize::from(cid) * 2;
                let gid = u16::from_be_bytes(bytes.get(p..p + 2)?.try_into().ok()?);
                (gid != 0).then(|| u32::from(gid))
            }
            Self::Cff(entries) => entries
                .binary_search_by_key(&cid, |p| p.0)
                .ok()
                .map(|i| entries[i].1),
        }
    }
}

/// Validates a CMap `cidrange` interval `[start..=stop]` starting at `cid`,
/// returning the exact code count (`1..=65536`) if neither `Code::value` nor
/// `cid` overflows.
const fn checked_cid_range(start: Code, stop: Code, cid: u16) -> Option<u32> {
    if start.length != stop.length || stop.value < start.value {
        return None;
    }
    let span = stop.value - start.value;
    if span > (u16::MAX - cid) as u32 {
        return None;
    }
    Some(span + 1)
}

fn range(
    entries: &mut BudgetVec<'_, (u32, Code)>,
    glyphs: &GlyphMap<'_>,
    start: Code,
    stop: Code,
    cid: u16,
    expanded: &mut u64,
) -> Result<(), WidgetError> {
    let Some(count) = checked_cid_range(start, stop, cid) else {
        return Err(malformed(
            "invalid character range in the form font's encoding",
        ));
    };
    // A small CMap can repeat a 65,536-code range many times. Charge even
    // codes absent from the font, before considering the first element.
    const MAX_EXPANDED_CODES: u64 = 1 << 20;
    *expanded = expanded.saturating_add(u64::from(count));
    if *expanded > MAX_EXPANDED_CODES {
        return Err(LimitExceeded::new(LimitCode::InterpretationFuel)
            .with_limit(MAX_EXPANDED_CODES)
            .with_amount(*expanded)
            .into());
    }
    entries
        .budget()
        .charge(LimitCode::InterpretationFuel, u64::from(count))?;
    for i in 0..count {
        if i % 256 == 0 {
            entries.budget().poll()?;
        }
        if let Some(gid) = glyphs.glyph(cid + i as u16).filter(|gid| *gid != 0) {
            entries.try_push((
                gid,
                Code {
                    length: start.length,
                    value: start.value + i,
                },
            ))?;
        }
    }
    Ok(())
}

/// CMap data blocks can contain hundreds of operands per operator. Read
/// their PDF strings and numbers with hayro's object reader and process one
/// row at a time instead of treating the block as a page content operator.
fn stream_codes(
    data: &[u8],
    entries: &mut BudgetVec<'_, (u32, Code)>,
    glyphs: &GlyphMap<'_>,
    expanded: &mut u64,
) -> Result<bool, WidgetError> {
    let mut input = SyntaxReader::new(data);
    loop {
        entries.budget().poll()?;
        input.skip_white_spaces_and_comments();
        if input.at_end() {
            return Ok(true);
        }
        // Hayro recovers a bare content operator as Null. Distinguish its
        // initial token from real PDF operands before asking for an object.
        if matches!(
            input.peek_byte(),
            Some(b'/' | b'<' | b'(' | b'[' | b'.' | b'+' | b'-' | b'0'..=b'9')
        ) {
            let Some(object) = input.read_without_context::<Object<'_>>() else {
                return Ok(false);
            };
            if let Object::Name(name) = object
                && name.as_ref() == b"WMode"
            {
                input.skip_white_spaces_and_comments();
                if input.read_without_context::<i32>() != Some(0) {
                    return Ok(false);
                }
            }
            continue;
        }
        let start = input.offset();
        input.forward_while(|byte| !byte.is_ascii_whitespace() && !b"()<>[]{}/%\0".contains(&byte));
        if input.offset() == start {
            input.forward();
            continue;
        }
        let end = match &data[start..input.offset()] {
            b"begincidchar" => &b"endcidchar"[..],
            b"begincidrange" => &b"endcidrange"[..],
            b"usecmap" => return Ok(false),
            _ => continue,
        };
        loop {
            input.skip_white_spaces_and_comments();
            if input.forward_tag(end).is_some() {
                break;
            }
            let Some(first) = Code::read(&mut input) else {
                return Ok(false);
            };
            let last = if end == b"endcidrange" {
                let Some(last) = Code::read(&mut input) else {
                    return Ok(false);
                };
                last
            } else {
                first
            };
            input.skip_white_spaces_and_comments();
            let Some(cid) = input
                .read_without_context::<i32>()
                .and_then(|n| u16::try_from(n).ok())
            else {
                return Ok(false);
            };
            range(entries, glyphs, first, last, cid, expanded)?;
        }
    }
}

fn encoding(
    reader: &Reader<'_>,
    font: &Dict,
    glyphs: &GlyphMap<'_>,
) -> Result<Option<Codes>, WidgetError> {
    let mut result = BudgetVec::new(reader.budget);
    let mut expanded = 0;
    let value = reader.entry(font, "Encoding")?;
    match value.as_deref() {
        Some(Value::Name(n)) if n.as_bytes() == b"Identity-H" => {
            let stop = match glyphs {
                GlyphMap::Identity(count) => count.saturating_sub(1).min(65535),
                GlyphMap::Map(bytes) => (bytes.len() / 2).saturating_sub(1) as u32,
                GlyphMap::Cff(entries) => entries.last().map_or(0, |p| u32::from(p.0)),
            };
            range(
                &mut result,
                glyphs,
                Code {
                    length: 2,
                    value: 0,
                },
                Code {
                    length: 2,
                    value: stop,
                },
                0,
                &mut expanded,
            )?;
        }
        Some(Value::Stream(stream)) => {
            if stream.dict().get("UseCMap").is_some() {
                return Ok(None);
            }
            let data = stream.decoded(reader.budget)?;
            reader
                .budget
                .check(LimitCode::FontInputSize, data.len() as u64)?;
            reader
                .budget
                .charge(LimitCode::MemorySize, data.len() as u64)?;
            if !stream_codes(&data, &mut result, glyphs, &mut expanded)? {
                return Ok(None);
            }
        }
        _ => return Ok(None),
    }
    result.as_mut_slice().sort_unstable();
    Ok(Some(Codes {
        by_glyph: result.into_vec(),
        unicode: None,
    }))
}

pub(super) fn read(
    reader: &Reader<'_>,
    font: &Dict,
    descendant: &Dict,
    face: &Face,
) -> Result<Option<Codes>, WidgetError> {
    if face.cff {
        let font_ref = face.font();
        let Some(data) = font_ref.table_data(Tag::new(b"CFF ")) else {
            return Ok(None);
        };
        let Ok(cff) = CffFontRef::new_cff(data.as_bytes(), 0, None) else {
            return Ok(None);
        };
        if !cff.is_cid() {
            return encoding(reader, font, &GlyphMap::Identity(cff.num_glyphs()));
        }
        let Some(charset) = cff.charset() else {
            return Ok(None);
        };
        let mut points = BudgetVec::new(reader.budget);
        for (gid, cid) in charset.iter() {
            points.try_push((cid.to_u16(), gid.to_u32()))?;
        }
        points.as_mut_slice().sort_unstable();
        return encoding(reader, font, &GlyphMap::Cff(&points));
    }
    let map = reader.entry(descendant, "CIDToGIDMap")?;
    let data;
    let glyphs = match map.as_deref() {
        None => GlyphMap::Identity(face.glyph_count),
        Some(Value::Name(n)) if n.as_bytes() == b"Identity" => GlyphMap::Identity(face.glyph_count),
        Some(Value::Stream(s)) => {
            data = s.decoded(reader.budget)?;
            if data.len() > 131072 || data.len() % 2 != 0 {
                return Ok(None);
            }
            GlyphMap::Map(&data)
        }
        _ => return Ok(None),
    };
    encoding(reader, font, &glyphs)
}

/// Reverse the bounded byte alphabet of an embedded simple font. Differences
/// replace the original slot; a glyph name from the program wins over a
/// Unicode-derived guess for that name.
pub(super) fn simple(
    reader: &Reader<'_>,
    font: &Dict,
    face: &Face,
) -> Result<Option<Codes>, WidgetError> {
    use crate::fonts::embed::Measure;
    use pdf_base14_metrics::{winansi_char_map::WINANSI_CHAR_MAP, winansi_glyph_name};
    use skrifa::MetadataProvider;
    use skrifa::raw::ps::PredefinedEncoding;

    let encoding = reader.entry(font, "Encoding")?;
    let (base, differences) = match encoding.as_deref() {
        Some(Value::Name(n)) => (Some(n.as_bytes()), None),
        Some(Value::Dict(d)) => {
            let base = match d.get("BaseEncoding") {
                Some(Value::Name(n)) => Some(n.as_bytes()),
                None => None,
                _ => return Ok(None),
            };
            (base, reader.entry(d, "Differences")?)
        }
        _ => return Ok(None),
    };
    let mut names = [None; 256];
    let mut gids = [None; 256];
    let measure = Measure::new(face);
    match base {
        Some(b"WinAnsiEncoding") => {
            for code in u8::MIN..=u8::MAX {
                let i = usize::from(code);
                names[i] = winansi_glyph_name(code).map(str::as_bytes);
                gids[i] = WINANSI_CHAR_MAP[i]
                    .and_then(|c| measure.glyph(c))
                    .map(|id| id.to_u32());
            }
        }
        Some(b"StandardEncoding") => {
            for code in u8::MIN..=u8::MAX {
                let name = PredefinedEncoding::Standard.name(code);
                if name != ".notdef" && !name.is_empty() {
                    names[usize::from(code)] = Some(name.as_bytes());
                }
            }
        }
        // An unknown or implicit base can still define usable slots explicitly.
        _ => {}
    }
    if let Some(differences) = differences.as_deref() {
        let Value::Array(differences) = differences else {
            return Ok(None);
        };
        let mut next = None;
        for value in differences.iter() {
            reader.budget.charge(LimitCode::InterpretationFuel, 1)?;
            match value {
                Value::Integer(code) => next = u8::try_from(*code).ok(),
                Value::Name(name) => {
                    let Some(code) = next else { return Ok(None) };
                    let i = usize::from(code);
                    names[i] = Some(name.as_bytes());
                    gids[i] = None;
                    next = code.checked_add(1);
                }
                _ => return Ok(None),
            }
        }
    }
    let mut slots = BudgetVec::new(reader.budget);
    for (code, name) in (u8::MIN..=u8::MAX).zip(names) {
        if let Some(name) = name.filter(|name| *name != b".notdef") {
            slots.try_push((name, code))?;
            let c = match name {
                [byte] if byte.is_ascii_alphabetic() => Some(char::from(*byte)),
                _ => (u8::MIN..=u8::MAX).find_map(|b| {
                    (winansi_glyph_name(b).is_some_and(|s| s.as_bytes() == name))
                        .then(|| WINANSI_CHAR_MAP[usize::from(b)])
                        .flatten()
                }),
            };
            if gids[usize::from(code)].is_none() {
                gids[usize::from(code)] = c.and_then(|ch| measure.glyph(ch)).map(|id| id.to_u32());
            }
        }
    }
    slots.as_mut_slice().sort_unstable();
    let font_ref = face.font();
    let glyph_names = font_ref.glyph_names();
    reader.budget.charge(
        LimitCode::InterpretationFuel,
        u64::from(glyph_names.num_glyphs()),
    )?;
    for (gid, name) in glyph_names.iter() {
        reader.budget.poll()?;
        let start = slots.partition_point(|(n, _)| *n < name.as_bytes());
        for &(_, code) in slots[start..]
            .iter()
            .take_while(|(n, _)| *n == name.as_bytes())
        {
            gids[usize::from(code)] = Some(gid.to_u32());
        }
    }
    let mut result = BudgetVec::new(reader.budget);
    let bound = |key| match font.get(key) {
        Some(Value::Integer(n)) => u8::try_from(*n).ok(),
        _ => None,
    };
    let range = bound("FirstChar").unwrap_or(0)..=bound("LastChar").unwrap_or(255);
    for (code, gid) in (u8::MIN..=u8::MAX)
        .zip(gids)
        .filter(|(code, _)| range.contains(code))
    {
        if let Some(gid) = gid.filter(|gid| *gid != 0) {
            result.try_push((
                gid,
                Code {
                    length: 1,
                    value: u32::from(code),
                },
            ))?;
        }
    }
    result.as_mut_slice().sort_unstable();
    Ok(Some(Codes {
        by_glyph: result.into_vec(),
        unicode: None,
    }))
}

#[cfg(kani)]
mod kani_proofs {
    use super::*;

    /// `checked_cid_range` never overflows `u32` or `u16` across the full domain
    /// of `(start, stop, cid)` and guarantees every index `i in 0..count` yields
    /// in-range `cid + i as u16` and `start.value + i`.
    #[kani::proof]
    fn kani_cmap_checked_cid_range_bounds() {
        let start = Code {
            length: kani::any(),
            value: kani::any(),
        };
        let stop = Code {
            length: kani::any(),
            value: kani::any(),
        };
        let cid: u16 = kani::any();
        match checked_cid_range(start, stop, cid) {
            Some(count) => {
                assert_eq!(start.length, stop.length);
                assert!((1..=65536).contains(&count));
                let i: u32 = kani::any();
                kani::assume(i < count);
                assert!(i <= u32::from(u16::MAX));
                assert!(cid.checked_add(i as u16).is_some());
                assert!(start.value.checked_add(i).is_some_and(|v| v <= stop.value));
            }
            None => {
                assert!(
                    start.length != stop.length
                        || stop.value < start.value
                        || stop.value - start.value > u32::from(u16::MAX - cid)
                );
            }
        }
    }
}
