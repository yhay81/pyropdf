//! The text strings of field values (ISO 32000-2 section 7.9.2.2).

use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::BudgetVec;

pub(super) fn append_text(out: &mut BudgetVec<'_, u8>, bytes: &[u8]) -> Result<(), LimitExceeded> {
    out.budget().poll()?;
    if let Some(rest) = bytes.strip_prefix(&[0xFE, 0xFF]) {
        let units = rest
            .as_chunks::<2>()
            .0
            .iter()
            .map(|&pair| u16::from_be_bytes(pair));
        for (index, character) in char::decode_utf16(units).enumerate() {
            if index % 1024 == 0 {
                out.budget().poll()?;
            }
            append_char(out, character.unwrap_or(char::REPLACEMENT_CHARACTER))?;
        }
    } else if let Some(rest) = bytes.strip_prefix(&[0xEF, 0xBB, 0xBF]) {
        append_lossy(out, rest)?;
    } else {
        for (index, &byte) in bytes.iter().enumerate() {
            if index % 1024 == 0 {
                out.budget().poll()?;
            }
            append_char(out, pdf_doc_char(byte))?;
        }
    }
    Ok(())
}

pub(super) fn append_lossy(out: &mut BudgetVec<'_, u8>, bytes: &[u8]) -> Result<(), LimitExceeded> {
    out.budget().poll()?;
    for chunk in bytes.utf8_chunks() {
        for part in chunk.valid().as_bytes().chunks(4096) {
            out.budget().poll()?;
            out.try_extend_from_slice(part)?;
        }
        if !chunk.invalid().is_empty() {
            append_char(out, char::REPLACEMENT_CHARACTER)?;
        }
    }
    Ok(())
}

fn append_char(out: &mut BudgetVec<'_, u8>, character: char) -> Result<(), LimitExceeded> {
    let mut bytes = [0; 4];
    out.try_extend_from_slice(character.encode_utf8(&mut bytes).as_bytes())
}

/// Decodes a text string: UTF-16BE or UTF-8 after their byte order marks,
/// and PDFDocEncoding otherwise (ISO 32000-2 section 7.9.2.2).
#[must_use]
pub fn text_string(bytes: &[u8]) -> String {
    if let Some(rest) = bytes.strip_prefix(&[0xFE, 0xFF]) {
        let units = rest
            .as_chunks::<2>()
            .0
            .iter()
            .map(|&pair| u16::from_be_bytes(pair));
        return char::decode_utf16(units)
            .map(|c| c.unwrap_or(char::REPLACEMENT_CHARACTER))
            .collect();
    }
    if let Some(rest) = bytes.strip_prefix(&[0xEF, 0xBB, 0xBF]) {
        return String::from_utf8_lossy(rest).into_owned();
    }
    bytes.iter().map(|&b| pdf_doc_char(b)).collect()
}

/// The characters of PDFDocEncoding from 0x80 to 0xA0 (ISO 32000-2 table
/// D.2); 0x9F is undefined.
const PDF_DOC_HIGH: [char; 33] = [
    '\u{2022}', '\u{2020}', '\u{2021}', '\u{2026}', '\u{2014}', '\u{2013}', '\u{0192}', '\u{2044}',
    '\u{2039}', '\u{203A}', '\u{2212}', '\u{2030}', '\u{201E}', '\u{201C}', '\u{201D}', '\u{2018}',
    '\u{2019}', '\u{201A}', '\u{2122}', '\u{FB01}', '\u{FB02}', '\u{0141}', '\u{0152}', '\u{0160}',
    '\u{0178}', '\u{017D}', '\u{0131}', '\u{0142}', '\u{0153}', '\u{0161}', '\u{017E}', '\u{FFFD}',
    '\u{20AC}',
];

/// The characters of PDFDocEncoding from 0x18 to 0x1F.
const PDF_DOC_ACCENTS: [char; 8] = [
    '\u{02D8}', '\u{02C7}', '\u{02C6}', '\u{02D9}', '\u{02DD}', '\u{02DB}', '\u{02DA}', '\u{02DC}',
];

fn pdf_doc_char(byte: u8) -> char {
    match byte {
        0x18..=0x1F => PDF_DOC_ACCENTS[usize::from(byte - 0x18)],
        0x7F => char::REPLACEMENT_CHARACTER,
        0x80..=0xA0 => PDF_DOC_HIGH[usize::from(byte - 0x80)],
        _ => char::from(byte),
    }
}
