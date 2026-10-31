//! Subsetting embedded font programs (ISO 32000-2 9.9): a subset keeps
//! every glyph ID and empties the outlines of the glyphs that no page
//! draws, so the content strings, encodings, `cmap`, CIDToGIDMap, widths,
//! and ToUnicode that address the font stay valid unchanged.

use std::collections::BTreeSet;

mod cff;
mod sfnt;
pub mod shape;
#[cfg(test)]
mod tests;

/// A subset font program.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Subset {
    /// The program, in the format of the program it came from.
    pub data: Vec<u8>,
    /// The glyphs of the program.
    pub glyphs: usize,
    /// The glyphs whose outlines the subset keeps, with `.notdef`.
    pub kept: usize,
}

/// The formats of embedded font programs that this subsets.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    /// A TrueType or OpenType program (`FontFile2`, or `FontFile3` with
    /// subtype `OpenType`).
    Sfnt,
    /// A bare CFF program (`FontFile3` with subtype `Type1C` or
    /// `CIDFontType0C`).
    Cff,
}

/// Returns `data` subset to the glyphs of `keep`, together with the glyphs
/// that those draw as components or accents; `None` when the program is not
/// one that this reads, which leaves it as it is.
pub fn subset(data: &[u8], format: Format, keep: &[u32]) -> Option<Subset> {
    let keep: BTreeSet<u32> = keep.iter().copied().collect();
    match format {
        Format::Sfnt => sfnt::subset(data, &keep),
        Format::Cff => cff::subset(data, &keep),
    }
}

fn be16(data: &[u8], at: usize) -> Option<u16> {
    Some(u16::from_be_bytes(
        data.get(at..at.checked_add(2)?)?.try_into().ok()?,
    ))
}

fn be32(data: &[u8], at: usize) -> Option<u32> {
    Some(u32::from_be_bytes(
        data.get(at..at.checked_add(4)?)?.try_into().ok()?,
    ))
}
