//! Subsetting a CFF font program (Adobe Technical Note 5176): the
//! CharStrings of the glyphs that the subset drops become a bare `endchar`,
//! and the rest of the font is copied, with its offsets rewritten.

use std::cell::RefCell;
use std::collections::{BTreeSet, HashMap};
use std::ops::Range;

use skrifa::raw::ps::cff::index::Index;
use skrifa::raw::ps::cs::{self, CharstringContext, CharstringKind, CommandSink};
use skrifa::raw::ps::error::Error as CsError;
use skrifa::raw::types::Fixed;

use super::{Subset, be16};

/// DICT operators whose operands are offsets (5176, tables 9, 10, and 23).
const CHARSET: u16 = 15;
const ENCODING: u16 = 16;
const CHARSTRINGS: u16 = 17;
const PRIVATE: u16 = 18;
const SUBRS: u16 = 19;
const ESCAPE: u16 = 12;
const CHARSTRING_TYPE: u16 = 1206;
const SYNTHETIC_BASE: u16 = 1220;
const ROS: u16 = 1230;
const FD_ARRAY: u16 = 1236;
const FD_SELECT: u16 = 1237;
/// The Type 2 charstring of an empty glyph.
const ENDCHAR: &[u8] = &[14];
/// The highest predefined charset and encoding offsets.
const PREDEFINED_CHARSET: i32 = 2;
const PREDEFINED_ENCODING: i32 = 1;

/// One DICT entry: its operator, the bytes of its operands, and their
/// values when they are integers.
struct Entry {
    op: u16,
    operands: Vec<u8>,
    values: Vec<Option<i32>>,
}

fn dict(data: &[u8]) -> Option<Vec<Entry>> {
    let mut entries = Vec::new();
    let (mut start, mut values, mut at) = (0, Vec::new(), 0);
    while let Some(&b0) = data.get(at) {
        let (value, size) = match b0 {
            0..=21 => {
                let op = if u16::from(b0) == ESCAPE {
                    at += 1;
                    1200 + u16::from(*data.get(at)?)
                } else {
                    u16::from(b0)
                };
                entries.push(Entry {
                    op,
                    operands: data
                        .get(start..at.checked_sub(usize::from(op >= 1200))?)?
                        .to_vec(),
                    values: std::mem::take(&mut values),
                });
                at += 1;
                start = at;
                continue;
            }
            28 => (
                Some(i32::from(i16::from_be_bytes(
                    data.get(at + 1..at + 3)?.try_into().ok()?,
                ))),
                3,
            ),
            29 => (
                Some(i32::from_be_bytes(
                    data.get(at + 1..at + 5)?.try_into().ok()?,
                )),
                5,
            ),
            30 => {
                let mut size = 1;
                while data
                    .get(at + size)
                    .is_some_and(|byte| byte & 0x0f != 0x0f && byte >> 4 != 0x0f)
                {
                    size += 1;
                }
                (None, size + 1)
            }
            32..=246 => (Some(i32::from(b0) - 139), 1),
            247..=250 => (
                Some((i32::from(b0) - 247) * 256 + i32::from(*data.get(at + 1)?) + 108),
                2,
            ),
            251..=254 => (
                Some(-(i32::from(b0) - 251) * 256 - i32::from(*data.get(at + 1)?) - 108),
                2,
            ),
            _ => return None,
        };
        // A DICT holds at most 48 operands per operator (5176, appendix B).
        if values.len() >= 48 {
            return None;
        }
        values.push(value);
        at += size;
    }
    Some(entries)
}

fn value(entries: &[Entry], op: u16, at: usize) -> Option<i32> {
    entries
        .iter()
        .find(|entry| entry.op == op)?
        .values
        .get(at)
        .copied()?
}

fn offset(entries: &[Entry], op: u16, at: usize) -> Option<usize> {
    usize::try_from(value(entries, op, at)?).ok()
}

/// Writes `entries`, with the operands of each operator in `replace`
/// written as 5-byte integers, which keeps the DICT's length whatever the
/// values.
fn write_dict(entries: &[Entry], replace: &[(u16, &[i32])]) -> Vec<u8> {
    let mut out = Vec::new();
    for entry in entries {
        match replace.iter().find(|(op, _)| *op == entry.op) {
            Some((_, values)) => {
                for value in *values {
                    out.push(29);
                    out.extend_from_slice(&value.to_be_bytes());
                }
            }
            None => out.extend_from_slice(&entry.operands),
        }
        if entry.op >= 1200 {
            out.extend_from_slice(&[12, u8::try_from(entry.op - 1200).unwrap_or(0)]);
        } else {
            out.push(u8::try_from(entry.op).unwrap_or(0));
        }
    }
    out
}

/// Returns the items of the INDEX at `at` and where it ends.
fn index(data: &[u8], at: usize) -> Option<(Vec<Range<usize>>, usize)> {
    let count = usize::from(be16(data, at)?);
    if count == 0 {
        return Some((Vec::new(), at + 2));
    }
    let size = usize::from(*data.get(at + 2)?);
    if !(1..=4).contains(&size) {
        return None;
    }
    let read = |item: usize| -> Option<usize> {
        let start = at + 3 + item * size;
        let bytes = data.get(start..start + size)?;
        Some(
            bytes
                .iter()
                .fold(0usize, |sum, &byte| (sum << 8) | usize::from(byte)),
        )
    };
    let base = at + 2 + (count + 1) * size;
    let mut items = Vec::new();
    items.try_reserve(count).ok()?;
    let mut previous = read(0)?;
    for item in 1..=count {
        let next = read(item)?;
        if next < previous || base + next > data.len() {
            return None;
        }
        items.push(base + previous..base + next);
        previous = next;
    }
    Some((items, base + previous))
}

fn write_index(items: &[&[u8]]) -> Option<Vec<u8>> {
    let count = u16::try_from(items.len()).ok()?;
    let mut out = count.to_be_bytes().to_vec();
    if count == 0 {
        return Some(out);
    }
    let total = items.iter().map(|item| item.len()).sum::<usize>() + 1;
    let size = (1..=4).find(|&size| total < 1 << (8 * size))?;
    out.push(u8::try_from(size).ok()?);
    out.try_reserve(total + (items.len() + 1) * size).ok()?;
    let mut offset = 1usize;
    for item in std::iter::once(&[][..]).chain(items.iter().copied()) {
        offset += item.len();
        out.extend_from_slice(&offset.to_be_bytes()[std::mem::size_of::<usize>() - size..]);
    }
    for item in items {
        out.extend_from_slice(item);
    }
    Some(out)
}

/// The length of the charset at `at` for `glyphs` glyphs (5176, section 13).
fn charset_len(data: &[u8], at: usize, glyphs: usize) -> Option<usize> {
    let format = *data.get(at)?;
    let named = glyphs.saturating_sub(1);
    if format == 0 {
        return Some(1 + 2 * named);
    }
    let left = match format {
        1 => 1,
        2 => 2,
        _ => return None,
    };
    let (mut covered, mut end) = (0, at + 1);
    while covered < named {
        let count = if left == 1 {
            usize::from(*data.get(end + 2)?)
        } else {
            usize::from(be16(data, end + 2)?)
        };
        covered += count + 1;
        end += 2 + left;
    }
    Some(end - at)
}

/// The length of the encoding at `at` (5176, section 12).
fn encoding_len(data: &[u8], at: usize) -> Option<usize> {
    let format = *data.get(at)?;
    let count = usize::from(*data.get(at + 1)?);
    let mut end = at
        + match format & 0x7f {
            0 => 2 + count,
            1 => 2 + 2 * count,
            _ => return None,
        };
    if format & 0x80 != 0 {
        end += 1 + 3 * usize::from(*data.get(end)?);
    }
    Some(end - at)
}

/// The length of the FDSelect at `at` for `glyphs` glyphs (5176, section 19).
fn fd_select_len(data: &[u8], at: usize, glyphs: usize) -> Option<usize> {
    match *data.get(at)? {
        0 => Some(1 + glyphs),
        3 => Some(5 + 3 * usize::from(be16(data, at + 1)?)),
        _ => None,
    }
}

/// A Private DICT and its local subroutines, ready to be written.
struct Private<'a> {
    entries: Vec<Entry>,
    subrs: Option<&'a [u8]>,
}

impl<'a> Private<'a> {
    fn read(data: &'a [u8], size: usize, at: usize) -> Option<Self> {
        let entries = dict(data.get(at..at.checked_add(size)?)?)?;
        let subrs = match offset(&entries, SUBRS, 0) {
            Some(relative) => {
                let start = at.checked_add(relative)?;
                let (_, end) = index(data, start)?;
                Some(data.get(start..end)?)
            }
            None => None,
        };
        Some(Self { entries, subrs })
    }

    fn dict_len(&self) -> usize {
        write_dict(&self.entries, &[(SUBRS, &[0])]).len()
    }

    fn len(&self) -> usize {
        self.dict_len() + self.subrs.map_or(0, <[u8]>::len)
    }

    fn write(&self, out: &mut Vec<u8>) -> Option<()> {
        let size = i32::try_from(self.dict_len()).ok()?;
        out.extend_from_slice(&write_dict(&self.entries, &[(SUBRS, &[size])]));
        out.extend_from_slice(self.subrs.unwrap_or_default());
        Some(())
    }
}

/// The context that evaluates a charstring and records the charstrings
/// that its `seac` draws.
struct Seac<'a> {
    inner: (&'a [u8], &'a Index<'a>, &'a Index<'a>, &'a Index<'a>),
    drawn: RefCell<Vec<usize>>,
}

impl CharstringContext for Seac<'_> {
    fn kind(&self) -> CharstringKind {
        CharstringKind::Type2
    }

    fn seac_components(&self, base: i32, accent: i32) -> Result<[&[u8]; 2], CsError> {
        let parts = self.inner.seac_components(base, accent)?;
        self.drawn
            .borrow_mut()
            .extend(parts.iter().map(|part| part.as_ptr() as usize));
        Ok(parts)
    }

    fn global_subr(&self, index: i32) -> Result<&[u8], CsError> {
        self.inner.global_subr(index)
    }

    fn subr(&self, index: i32) -> Result<&[u8], CsError> {
        self.inner.subr(index)
    }
}

struct Nothing;

impl CommandSink for Nothing {
    fn move_to(&mut self, _: Fixed, _: Fixed) {}
    fn line_to(&mut self, _: Fixed, _: Fixed) {}
    fn curve_to(&mut self, _: Fixed, _: Fixed, _: Fixed, _: Fixed, _: Fixed, _: Fixed) {}
    fn close(&mut self) {}
}

/// Adds the glyphs that the `seac` accents of `keep` draw to it.
fn add_accents(
    data: &[u8],
    charstrings: &[Range<usize>],
    charstrings_at: usize,
    global_subrs: Range<usize>,
    subrs: Option<&[u8]>,
    keep: &mut BTreeSet<u32>,
) -> Option<()> {
    let charstring_index = Index::new(data.get(charstrings_at..)?, false).ok()?;
    let global_index = Index::new(data.get(global_subrs)?, false).ok()?;
    let subr_index = match subrs {
        Some(subrs) => Index::new(subrs, false).ok()?,
        None => Index::Empty,
    };
    let context = Seac {
        inner: (data, &charstring_index, &global_index, &subr_index),
        drawn: RefCell::new(Vec::new()),
    };
    let glyphs: HashMap<usize, u32> = charstrings
        .iter()
        .enumerate()
        .filter(|(_, range)| !range.is_empty())
        .filter_map(|(glyph, range)| {
            Some((
                data.get(range.clone())?.as_ptr() as usize,
                u32::try_from(glyph).ok()?,
            ))
        })
        .collect();
    let used: Vec<u32> = keep.iter().copied().collect();
    for glyph in used {
        let Some(range) = usize::try_from(glyph)
            .ok()
            .and_then(|glyph| charstrings.get(glyph))
        else {
            continue;
        };
        let Some(charstring) = data.get(range.clone()) else {
            continue;
        };
        // A charstring that does not evaluate draws nothing, so it needs nothing.
        let _ = cs::evaluate(&context, None, charstring, &mut Nothing);
        for drawn in context.drawn.borrow_mut().drain(..) {
            if let Some(&component) = glyphs.get(&drawn) {
                keep.insert(component);
            }
        }
    }
    Some(())
}

/// Subsets the CFF `data` to the glyphs of `keep`, with their `seac`
/// accents; `None` when it is not a CFF that this rewrites.
pub(super) fn subset(data: &[u8], keep: &BTreeSet<u32>) -> Option<Subset> {
    if *data.first()? != 1 {
        return None;
    }
    let header = usize::from(*data.get(2)?);
    let (_, names_end) = index(data, header)?;
    let (tops, tops_end) = index(data, names_end)?;
    let [top] = tops.as_slice() else {
        return None;
    };
    let (_, strings_end) = index(data, tops_end)?;
    let (_, global_end) = index(data, strings_end)?;
    let top = dict(data.get(top.clone())?)?;
    if value(&top, CHARSTRING_TYPE, 0).is_some_and(|kind| kind != 2)
        || value(&top, SYNTHETIC_BASE, 0).is_some()
    {
        return None;
    }
    let cid = top.iter().any(|entry| entry.op == ROS);
    let charstrings_at = offset(&top, CHARSTRINGS, 0)?;
    let (charstrings, _) = index(data, charstrings_at)?;
    let glyphs = charstrings.len();

    let charset = value(&top, CHARSET, 0).unwrap_or(0);
    let charset_block = if charset > PREDEFINED_CHARSET {
        let at = usize::try_from(charset).ok()?;
        Some(data.get(at..at + charset_len(data, at, glyphs)?)?)
    } else {
        None
    };
    let encoding = value(&top, ENCODING, 0).unwrap_or(0);
    let encoding_block = if !cid && encoding > PREDEFINED_ENCODING {
        let at = usize::try_from(encoding).ok()?;
        Some(data.get(at..at + encoding_len(data, at)?)?)
    } else {
        None
    };
    let fd_select_block = if cid {
        let at = offset(&top, FD_SELECT, 0)?;
        Some(data.get(at..at + fd_select_len(data, at, glyphs)?)?)
    } else {
        None
    };
    let mut fonts: Vec<(Vec<Entry>, Private<'_>)> = Vec::new();
    let private = if cid {
        let (dicts, _) = index(data, offset(&top, FD_ARRAY, 0)?)?;
        // An FDArray holds at most 256 Font DICTs, as FDSelect's bytes address.
        if dicts.len() > 256 {
            return None;
        }
        for range in dicts {
            let entries = dict(data.get(range)?)?;
            let private = Private::read(
                data,
                offset(&entries, PRIVATE, 0)?,
                offset(&entries, PRIVATE, 1)?,
            )?;
            fonts.push((entries, private));
        }
        None
    } else {
        Some(Private::read(
            data,
            offset(&top, PRIVATE, 0)?,
            offset(&top, PRIVATE, 1)?,
        )?)
    };

    let mut keep = keep.clone();
    if let Some(private) = &private {
        add_accents(
            data,
            &charstrings,
            charstrings_at,
            strings_end..global_end,
            private.subrs,
            &mut keep,
        );
    }
    let mut new_charstrings: Vec<&[u8]> = Vec::new();
    new_charstrings.try_reserve(glyphs).ok()?;
    let mut kept = 0;
    for (glyph, range) in charstrings.iter().enumerate() {
        if glyph == 0 || u32::try_from(glyph).is_ok_and(|id| keep.contains(&id)) {
            new_charstrings.push(data.get(range.clone())?);
            kept += 1;
        } else {
            new_charstrings.push(ENDCHAR);
        }
    }
    let new_charstrings = write_index(&new_charstrings)?;

    // The layout: everything up to the global subroutines as it was, with the
    // new Top DICT, then the blocks that the Top DICT addresses.
    let replaced = |values: &[i32; 7]| -> Vec<u8> {
        let mut replace: Vec<(u16, &[i32])> = vec![(CHARSTRINGS, &values[3..4])];
        if charset_block.is_some() {
            replace.push((CHARSET, &values[0..1]));
        }
        if encoding_block.is_some() {
            replace.push((ENCODING, &values[1..2]));
        }
        if cid {
            replace.push((FD_SELECT, &values[2..3]));
            replace.push((FD_ARRAY, &values[4..5]));
        } else {
            replace.push((PRIVATE, &values[5..7]));
        }
        write_dict(&top, &replace)
    };
    let top_len = write_index(&[&replaced(&[0; 7])])?.len();
    let mut at = names_end + top_len + (global_end - tops_end);
    let mut place = |len: usize| -> Option<i32> {
        let here = i32::try_from(at).ok()?;
        at += len;
        Some(here)
    };
    let charset_at = place(charset_block.map_or(0, <[u8]>::len))?;
    let encoding_at = place(encoding_block.map_or(0, <[u8]>::len))?;
    let fd_select_at = place(fd_select_block.map_or(0, <[u8]>::len))?;
    let charstrings_new_at = place(new_charstrings.len())?;
    let font_dicts = |privates: &[i32]| -> Option<Vec<u8>> {
        let dicts: Vec<Vec<u8>> = fonts
            .iter()
            .zip(privates)
            .map(|((entries, private), &offset)| {
                let size = i32::try_from(private.dict_len()).unwrap_or(i32::MAX);
                write_dict(entries, &[(PRIVATE, &[size, offset])])
            })
            .collect();
        let dicts: Vec<&[u8]> = dicts.iter().map(Vec::as_slice).collect();
        write_index(&dicts)
    };
    let mut fd_array_at = 0;
    let mut privates = Vec::new();
    if cid {
        fd_array_at = place(font_dicts(&vec![0; fonts.len()])?.len())?;
        for (_, private) in &fonts {
            privates.push(place(private.len())?);
        }
    }
    let (private_size, private_at) = match &private {
        Some(private) => (
            i32::try_from(private.dict_len()).ok()?,
            place(private.len())?,
        ),
        None => (0, 0),
    };

    let mut out = Vec::new();
    out.try_reserve(at).ok()?;
    out.extend_from_slice(data.get(..names_end)?);
    out.extend_from_slice(&write_index(&[&replaced(&[
        charset_at,
        encoding_at,
        fd_select_at,
        charstrings_new_at,
        fd_array_at,
        private_size,
        private_at,
    ])])?);
    out.extend_from_slice(data.get(tops_end..global_end)?);
    for block in [charset_block, encoding_block, fd_select_block]
        .into_iter()
        .flatten()
    {
        out.extend_from_slice(block);
    }
    out.extend_from_slice(&new_charstrings);
    if cid {
        out.extend_from_slice(&font_dicts(&privates)?);
        for (_, private) in &fonts {
            private.write(&mut out)?;
        }
    }
    if let Some(private) = &private {
        private.write(&mut out)?;
    }
    Some(Subset {
        data: out,
        glyphs,
        kept,
    })
}
