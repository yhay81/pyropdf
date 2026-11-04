//! The settler of [`Structure::FontSubset`]: at save, the Type0 font of an
//! embedded face gets a program compacted to the glyphs of its `W` array,
//! which lists every glyph that the document drew with it, and its names
//! a subset tag (ISO 32000-2 9.9, 9.6.4). The save settles in a scratch
//! transaction, so the document keeps the whole program for later drawing.
//!
//! [`Structure::FontSubset`]: pyropdf_store::settle::Structure::FontSubset

use std::collections::BTreeSet;

use pyropdf_store::budget::Budget;
use pyropdf_store::settle::Settler;
use pyropdf_store::value::{Bytes, Dict, Name, Reference, Stream, Value};
use pyropdf_store::version::{Transaction, Version};
use pyropdf_store::{Error, Result};

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
        let mut maps = MapNumbers::of(transaction, budget)?;
        for &number in numbers {
            // A font that is no longer the shape that pyropdf wrote keeps
            // its program whole.
            if let Some(font) = Font::read(transaction.version(), number, budget)? {
                font.subset(transaction, &mut maps, budget)?;
            }
        }
        Ok(())
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
        if !is_name(font.get("Subtype"), b"Type0") || !is_name(font.get("Encoding"), b"Identity-H")
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
        let Some(glyphs) = cid_font.get("W").and_then(widths_glyphs) else {
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
        maps: &mut MapNumbers,
        budget: &Budget,
    ) -> Result<()> {
        let base = match self.font.get("BaseFont") {
            Some(Value::Name(name)) => name.as_bytes().to_vec(),
            _ => return Ok(()),
        };
        // A font that a save already tagged is a subset already.
        if has_tag(&base) {
            return Ok(());
        }
        let name = String::from_utf8_lossy(&base);
        let Some(compact) = compact(&self.program.1, &self.glyphs, &name) else {
            return Ok(());
        };
        if compact.data.len() >= self.program.1.len() {
            return Ok(());
        }
        let mut tagged = tag(&compact).into_bytes();
        tagged.extend_from_slice(&base);
        let tagged = Value::Name(Name::new(tagged));

        let (program, data) = (self.program.0, compact.data.clone());
        let mut program_dict = Dict::new();
        if compact.cff {
            program_dict.insert(
                Name::from("Subtype"),
                Value::Name(Name::from("CIDFontType0C")),
            );
        } else {
            program_dict.insert(
                Name::from("Length1"),
                Value::Integer(i64::try_from(data.len()).unwrap_or(i64::MAX)),
            );
        }
        let stream = Stream::new(program_dict, Bytes::from(data));
        transaction.write(program.number(), &Value::from(stream), budget)?;

        let (descriptor, mut descriptor_dict) = self.descriptor;
        descriptor_dict.insert(Name::from("FontName"), tagged.clone());
        transaction.write(descriptor.number(), &Value::Dict(descriptor_dict), budget)?;

        let (descendant, mut cid_font) = self.descendant;
        cid_font.insert(Name::from("BaseFont"), tagged.clone());
        if !compact.cff {
            let map = cid_to_gid(&compact.glyphs);
            let number = maps.next(transaction)?;
            let stream = Stream::new(Dict::new(), Bytes::from(map));
            let reference = transaction.write(number, &Value::from(stream), budget)?;
            cid_font.insert(Name::from("CIDToGIDMap"), Value::Reference(reference));
        }
        transaction.write(descendant.number(), &Value::Dict(cid_font), budget)?;

        let mut font = self.font;
        font.insert(Name::from("BaseFont"), tagged);
        transaction.write(self.number, &Value::Dict(font), budget)?;
        Ok(())
    }
}

/// Where one settling takes the numbers of the CIDToGIDMaps that it writes.
///
/// A save discards the transaction that it settles in, and the document never
/// issues a number twice, so allocating would write a map under a new number
/// at each save, and two saves with no edit between them would differ. A
/// transaction that has written nothing takes the numbers after its parent's
/// instead, which depend only on that version. The version refers to none of
/// them, since it was written above every number issued before it, and the
/// write raises the high-water mark above them, so no allocation issues them
/// later.
///
/// A transaction that has written something allocates: its writes may refer to
/// a number that a transaction nested in it discarded, above its parent's.
struct MapNumbers(Option<u64>);

impl MapNumbers {
    fn of(transaction: &Transaction, budget: &Budget) -> Result<Self> {
        let parent = transaction.parent();
        let written = transaction.version().changes(parent, budget)?;
        let unwritten = written.is_some_and(|changes| changes.is_empty());
        Ok(Self(unwritten.then(|| parent.next_number())))
    }

    fn next(&mut self, transaction: &mut Transaction) -> Result<u32> {
        let Some(next) = &mut self.0 else {
            return Ok(transaction.allocate()?.number());
        };
        let number = u32::try_from(*next).map_err(|_| {
            Error::malformed(
                "the document uses object number 4294967295, so no number is left for a new object",
            )
        })?;
        *next += 1;
        Ok(number)
    }
}

/// Returns the glyphs that the `W` array `value` gives widths, in its
/// `c [w1 w2 ...]` and `c_first c_last w` forms (ISO 32000-2 9.7.4.3),
/// or `None` when it is not an array of those.
fn widths_glyphs(value: &Value) -> Option<Vec<u32>> {
    let Value::Array(array) = value else {
        return None;
    };
    let glyph = |value: Option<&Value>| match value {
        Some(&Value::Integer(glyph)) => u16::try_from(glyph).ok().map(u32::from),
        _ => None,
    };
    let mut glyphs = BTreeSet::new();
    let mut at = 0;
    while at < array.len() {
        let first = glyph(array.get(at))?;
        match array.get(at + 1)? {
            Value::Array(widths) => {
                let last = first.checked_add(u32::try_from(widths.len()).ok()?)?;
                glyphs.extend((first..last).filter(|&glyph| glyph <= u32::from(u16::MAX)));
                at += 2;
            }
            _ => {
                let last = glyph(array.get(at + 1))?;
                glyphs.extend(first..=last);
                at += 3;
            }
        }
    }
    Some(glyphs.into_iter().collect())
}

/// Returns the CIDToGIDMap of a compacted TrueType program: for each CID,
/// the old glyph ID, its new glyph ID, two bytes each.
fn cid_to_gid(glyphs: &[u16]) -> Vec<u8> {
    let cids = glyphs.iter().max().map_or(0, |&last| usize::from(last) + 1);
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
