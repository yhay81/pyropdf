//! Subsetting an sfnt font program, TrueType or OpenType: the glyphs that
//! it drops keep their IDs with empty outlines, so every encoding, `cmap`,
//! and CIDToGIDMap that addresses the font stays valid.

use std::collections::BTreeSet;

use super::{Subset, be16, be32};

const GLYF: [u8; 4] = *b"glyf";
const LOCA: [u8; 4] = *b"loca";
const HEAD: [u8; 4] = *b"head";
const MAXP: [u8; 4] = *b"maxp";
const CFF: [u8; 4] = *b"CFF ";
/// The `head` table's `checkSumAdjustment` (OpenType `head`).
const ADJUSTMENT_AT: usize = 8;
/// The `head` table's `indexToLocFormat`: 0 for 16-bit offsets, 1 for 32-bit.
const LOCA_FORMAT_AT: usize = 50;
/// The constant from which `checkSumAdjustment` subtracts the font's sum.
const MAGIC: u32 = 0xB1B0_AFBA;
/// Composite glyph flags (OpenType `glyf`, composite glyph description).
const ARGS_ARE_WORDS: u16 = 0x0001;
const HAVE_SCALE: u16 = 0x0008;
const MORE_COMPONENTS: u16 = 0x0020;
const HAVE_XY_SCALE: u16 = 0x0040;
const HAVE_TWO_BY_TWO: u16 = 0x0080;

struct Table<'a> {
    tag: [u8; 4],
    data: &'a [u8],
}

fn tables(data: &[u8]) -> Option<Vec<Table<'_>>> {
    let count = usize::from(be16(data, 4)?);
    let mut tables = Vec::new();
    tables.try_reserve(count).ok()?;
    for at in 0..count {
        let record = 12 + 16 * at;
        let tag: [u8; 4] = data.get(record..record + 4)?.try_into().ok()?;
        let offset = usize::try_from(be32(data, record + 8)?).ok()?;
        let length = usize::try_from(be32(data, record + 12)?).ok()?;
        let table = data.get(offset..offset.checked_add(length)?)?;
        tables.push(Table { tag, data: table });
    }
    Some(tables)
}

fn find<'a>(tables: &'a [Table<'_>], tag: [u8; 4]) -> Option<&'a [u8]> {
    tables
        .iter()
        .find(|table| table.tag == tag)
        .map(|table| table.data)
}

fn checksum(data: &[u8]) -> u32 {
    data.chunks(4).fold(0u32, |sum, chunk| {
        let mut word = [0u8; 4];
        word[..chunk.len()].copy_from_slice(chunk);
        sum.wrapping_add(u32::from_be_bytes(word))
    })
}

/// Returns the glyph offsets of `loca`, one more than the glyphs.
fn offsets(loca: &[u8], long: bool, glyphs: usize, glyf: usize) -> Option<Vec<usize>> {
    let mut offsets = Vec::new();
    offsets.try_reserve(glyphs + 1).ok()?;
    for at in 0..=glyphs {
        let offset = if long {
            usize::try_from(be32(loca, 4 * at)?).ok()?
        } else {
            usize::from(be16(loca, 2 * at)?) * 2
        };
        offsets.push(offset.min(glyf));
    }
    Some(offsets)
}

/// Adds the components of the composite glyphs of `keep` to it, to any depth:
/// each glyph is read once, so the walk ends.
fn add_components(glyf: &[u8], offsets: &[usize], keep: &mut BTreeSet<u32>) {
    let glyphs = offsets.len() - 1;
    let mut pending: Vec<u32> = keep.iter().copied().collect();
    while let Some(glyph) = pending.pop() {
        let Ok(glyph) = usize::try_from(glyph) else {
            continue;
        };
        if glyph >= glyphs {
            continue;
        }
        let (start, end) = (offsets[glyph], offsets[glyph + 1]);
        let Some(data) = glyf.get(start..end.max(start)) else {
            continue;
        };
        // A negative contour count marks a composite glyph.
        if data.len() < 10 || be16(data, 0).is_none_or(|count| count & 0x8000 == 0) {
            continue;
        }
        let mut at = 10;
        while let (Some(flags), Some(component)) = (be16(data, at), be16(data, at + 2)) {
            if keep.insert(u32::from(component)) {
                pending.push(u32::from(component));
            }
            at += 4 + if flags & ARGS_ARE_WORDS != 0 { 4 } else { 2 };
            at += if flags & HAVE_SCALE != 0 {
                2
            } else if flags & HAVE_XY_SCALE != 0 {
                4
            } else if flags & HAVE_TWO_BY_TWO != 0 {
                8
            } else {
                0
            };
            if flags & MORE_COMPONENTS == 0 {
                break;
            }
        }
    }
}

/// Returns `glyf` with only the glyphs of `keep` and the matching long `loca`.
fn rebuild(glyf: &[u8], offsets: &[usize], keep: &BTreeSet<u32>) -> Option<(Vec<u8>, Vec<u8>)> {
    let glyphs = offsets.len() - 1;
    let mut new_glyf = Vec::new();
    let mut new_loca = Vec::new();
    new_loca.try_reserve(4 * (glyphs + 1)).ok()?;
    for glyph in 0..glyphs {
        new_loca.extend_from_slice(&u32::try_from(new_glyf.len()).ok()?.to_be_bytes());
        let id = u32::try_from(glyph).ok()?;
        let (start, end) = (offsets[glyph], offsets[glyph + 1]);
        if (glyph == 0 || keep.contains(&id)) && end > start {
            let data = glyf.get(start..end)?;
            new_glyf.try_reserve(data.len() + 3).ok()?;
            new_glyf.extend_from_slice(data);
            new_glyf.resize(new_glyf.len().next_multiple_of(4), 0);
        }
    }
    new_loca.extend_from_slice(&u32::try_from(new_glyf.len()).ok()?.to_be_bytes());
    Some((new_glyf, new_loca))
}

/// Writes an sfnt with `tables`, in their order, and its `head` adjustment.
pub(super) fn write(version: &[u8], tables: &[([u8; 4], Vec<u8>)]) -> Option<Vec<u8>> {
    let count = u16::try_from(tables.len()).ok()?;
    let power = if count == 0 {
        0
    } else {
        1u16 << (15 - count.leading_zeros())
    };
    let selector = u16::try_from(power.max(1).trailing_zeros()).ok()?;
    let mut out = Vec::new();
    out.extend_from_slice(version);
    out.extend_from_slice(&count.to_be_bytes());
    out.extend_from_slice(&(power * 16).to_be_bytes());
    out.extend_from_slice(&selector.to_be_bytes());
    out.extend_from_slice(&(count * 16 - power * 16).to_be_bytes());
    let mut offset = 12 + 16 * tables.len();
    let mut head_at = None;
    for (tag, data) in tables {
        out.extend_from_slice(tag);
        let mut sum_data = data.clone();
        if *tag == HEAD && sum_data.len() >= ADJUSTMENT_AT + 4 {
            sum_data[ADJUSTMENT_AT..ADJUSTMENT_AT + 4].fill(0);
            head_at = Some(offset);
        }
        out.extend_from_slice(&checksum(&sum_data).to_be_bytes());
        out.extend_from_slice(&u32::try_from(offset).ok()?.to_be_bytes());
        out.extend_from_slice(&u32::try_from(data.len()).ok()?.to_be_bytes());
        offset += data.len().next_multiple_of(4);
    }
    for (tag, data) in tables {
        let start = out.len();
        out.try_reserve(data.len() + 3).ok()?;
        out.extend_from_slice(data);
        if *tag == HEAD && data.len() >= ADJUSTMENT_AT + 4 {
            out[start + ADJUSTMENT_AT..start + ADJUSTMENT_AT + 4].fill(0);
        }
        out.resize(out.len().next_multiple_of(4), 0);
    }
    if let Some(at) = head_at {
        let adjustment = MAGIC.wrapping_sub(checksum(&out));
        out[at + ADJUSTMENT_AT..at + ADJUSTMENT_AT + 4].copy_from_slice(&adjustment.to_be_bytes());
    }
    Some(out)
}

/// Subsets the sfnt `data` to the glyphs of `keep`, with their components;
/// `None` when it is not an sfnt that this reads, such as a collection.
pub(super) fn subset(data: &[u8], keep: &BTreeSet<u32>) -> Option<Subset> {
    let version = data.get(0..4)?;
    if version != [0, 1, 0, 0] && version != b"true" && version != b"OTTO" {
        return None;
    }
    let tables = tables(data)?;
    let mut keep = keep.clone();
    let mut written: Vec<([u8; 4], Vec<u8>)> = Vec::new();
    written.try_reserve(tables.len()).ok()?;
    if let Some(cff) = find(&tables, CFF) {
        let new = super::cff::subset(cff, &keep)?;
        for table in &tables {
            let data = if table.tag == CFF {
                new.data.clone()
            } else {
                table.data.to_vec()
            };
            written.push((table.tag, data));
        }
        let data = write(version, &written)?;
        return Some(Subset {
            data,
            glyphs: new.glyphs,
            kept: new.kept,
        });
    }
    let glyf = find(&tables, GLYF)?;
    let loca = find(&tables, LOCA)?;
    let head = find(&tables, HEAD)?;
    let glyphs = usize::from(be16(find(&tables, MAXP)?, 4)?);
    let long = be16(head, LOCA_FORMAT_AT)? == 1;
    let offsets = offsets(loca, long, glyphs, glyf.len())?;
    add_components(glyf, &offsets, &mut keep);
    let (new_glyf, new_loca) = rebuild(glyf, &offsets, &keep)?;
    for table in &tables {
        let data = match table.tag {
            GLYF => new_glyf.clone(),
            LOCA => new_loca.clone(),
            HEAD => {
                let mut head = table.data.to_vec();
                head.get_mut(LOCA_FORMAT_AT..LOCA_FORMAT_AT + 2)?
                    .copy_from_slice(&1u16.to_be_bytes());
                head
            }
            _ => table.data.to_vec(),
        };
        written.push((table.tag, data));
    }
    let kept = (0..glyphs)
        .filter(|&glyph| glyph == 0 || u32::try_from(glyph).is_ok_and(|id| keep.contains(&id)))
        .count();
    Some(Subset {
        data: write(version, &written)?,
        glyphs,
        kept,
    })
}

const HHEA: [u8; 4] = *b"hhea";
const HMTX: [u8; 4] = *b"hmtx";
/// The tables that a TrueType program embedded for a CIDFont needs
/// (ISO 32000-2 9.9): the outlines, their metrics, and the instructions.
const EMBEDDED: [[u8; 4]; 9] = [
    *b"cvt ", *b"fpgm", GLYF, HEAD, HHEA, HMTX, LOCA, MAXP, *b"prep",
];
/// The `hhea` table's `numberOfHMetrics`.
const LONG_METRICS_AT: usize = 34;
/// The `maxp` table's `numGlyphs`.
const GLYPH_COUNT_AT: usize = 4;

/// Returns the TrueType sfnt `data` with only the glyphs of `keep`, their
/// components, and `.notdef`, renumbered in order, and the tables that
/// a CIDFont needs; with the old ID of each new glyph. `None` when it is
/// not a TrueType program that this reads.
pub(super) fn compact(data: &[u8], keep: &BTreeSet<u32>) -> Option<(Vec<u8>, Vec<u16>)> {
    let version = data.get(0..4)?;
    if version != [0, 1, 0, 0] && version != b"true" {
        return None;
    }
    let tables = tables(data)?;
    let glyf = find(&tables, GLYF)?;
    let head = find(&tables, HEAD)?;
    let hmtx = find(&tables, HMTX)?;
    let glyphs = usize::from(be16(find(&tables, MAXP)?, GLYPH_COUNT_AT)?);
    let long_metrics = usize::from(be16(find(&tables, HHEA)?, LONG_METRICS_AT)?);
    if glyphs == 0 || long_metrics == 0 || long_metrics > glyphs {
        return None;
    }
    let long = be16(head, LOCA_FORMAT_AT)? == 1;
    let offsets = offsets(find(&tables, LOCA)?, long, glyphs, glyf.len())?;
    let mut keep: BTreeSet<u32> = keep
        .iter()
        .copied()
        .filter(|&glyph| usize::try_from(glyph).is_ok_and(|glyph| glyph < glyphs))
        .collect();
    keep.insert(0);
    add_components(glyf, &offsets, &mut keep);
    keep.retain(|&glyph| usize::try_from(glyph).is_ok_and(|glyph| glyph < glyphs));
    let old: Vec<u16> = keep
        .iter()
        .filter_map(|&glyph| u16::try_from(glyph).ok())
        .collect();
    let new_id = |glyph: u16| old.binary_search(&glyph).ok();

    let mut new_glyf = Vec::new();
    let mut new_loca = Vec::new();
    let mut new_hmtx = Vec::new();
    new_loca.try_reserve(4 * (old.len() + 1)).ok()?;
    new_hmtx.try_reserve(4 * old.len()).ok()?;
    for &glyph in &old {
        let glyph = usize::from(glyph);
        new_loca.extend_from_slice(&u32::try_from(new_glyf.len()).ok()?.to_be_bytes());
        let (start, end) = (offsets[glyph], offsets[glyph + 1]);
        if end > start {
            let at = new_glyf.len();
            let outline = glyf.get(start..end)?;
            new_glyf.try_reserve(outline.len() + 3).ok()?;
            new_glyf.extend_from_slice(outline);
            renumber_components(&mut new_glyf[at..], new_id)?;
            new_glyf.resize(new_glyf.len().next_multiple_of(4), 0);
        }
        let metric = glyph.min(long_metrics - 1);
        new_hmtx.extend_from_slice(hmtx.get(4 * metric..4 * metric + 2)?);
        let bearing = if glyph < long_metrics {
            4 * glyph + 2
        } else {
            4 * long_metrics + 2 * (glyph - long_metrics)
        };
        new_hmtx.extend_from_slice(hmtx.get(bearing..bearing + 2)?);
    }
    new_loca.extend_from_slice(&u32::try_from(new_glyf.len()).ok()?.to_be_bytes());

    let count = u16::try_from(old.len()).ok()?.to_be_bytes();
    let mut written: Vec<([u8; 4], Vec<u8>)> = Vec::new();
    for table in &tables {
        if !EMBEDDED.contains(&table.tag) || written.iter().any(|(tag, _)| *tag == table.tag) {
            continue;
        }
        let data = match table.tag {
            GLYF => std::mem::take(&mut new_glyf),
            LOCA => std::mem::take(&mut new_loca),
            HMTX => std::mem::take(&mut new_hmtx),
            tag => {
                let mut data = table.data.to_vec();
                let (at, value) = match tag {
                    HEAD => (LOCA_FORMAT_AT, 1u16.to_be_bytes()),
                    HHEA => (LONG_METRICS_AT, count),
                    MAXP => (GLYPH_COUNT_AT, count),
                    _ => (0, [0; 2]),
                };
                if tag == HEAD || tag == HHEA || tag == MAXP {
                    data.get_mut(at..at + 2)?.copy_from_slice(&value);
                }
                data
            }
        };
        written.push((table.tag, data));
    }
    written.sort_by_key(|(tag, _)| *tag);
    Some((write(&[0, 1, 0, 0], &written)?, old))
}

/// Rewrites the component glyph IDs of the composite glyph `data` with
/// `new_id`; a simple glyph stays as it is.
fn renumber_components(data: &mut [u8], new_id: impl Fn(u16) -> Option<usize>) -> Option<()> {
    if data.len() < 10 || be16(data, 0)? & 0x8000 == 0 {
        return Some(());
    }
    let mut at = 10;
    while let (Some(flags), Some(component)) = (be16(data, at), be16(data, at + 2)) {
        let new = u16::try_from(new_id(component)?).ok()?;
        data.get_mut(at + 2..at + 4)?
            .copy_from_slice(&new.to_be_bytes());
        at += 4 + if flags & ARGS_ARE_WORDS != 0 { 4 } else { 2 };
        at += if flags & HAVE_SCALE != 0 {
            2
        } else if flags & HAVE_XY_SCALE != 0 {
            4
        } else if flags & HAVE_TWO_BY_TWO != 0 {
            8
        } else {
            0
        };
        if flags & MORE_COMPONENTS == 0 {
            break;
        }
    }
    Some(())
}
