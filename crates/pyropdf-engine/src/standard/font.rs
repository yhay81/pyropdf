//! The metrics of the Standard 14 fonts, and the WinAnsi encoding that
//! their text is written in.

use pdf_base14_metrics::{Base14Font, CharacterMetric};
use pyropdf_store::{LimitExceeded, budget::Budget};

use super::layout::LineMetrics;

/// The width, in thousandths of the em, of a character that a font has no
/// width for.
const MISSING_WIDTH: f32 = 250.0;

/// Returns the Standard 14 font of a PostScript name, such as
/// `Helvetica-Bold`.
pub fn font(name: &str) -> Option<Base14Font> {
    Base14Font::ALL
        .into_iter()
        .find(|font| font.pdf_base_name() == name)
}

/// Returns the width of `text` in `font` at `size`, by Adobe's metrics, or
/// `None` when it holds a character outside WinAnsi.
///
/// Symbol and ZapfDingbats read each WinAnsi byte as a code of their
/// built-in encoding, and a code without a glyph is 250 units wide.
pub fn text_width(text: &str, font: Base14Font, size: f64) -> Option<f64> {
    text_width_inner(text, font, size, None).ok().flatten()
}

pub(super) fn text_width_budgeted(
    text: &str,
    font: Base14Font,
    size: f64,
    budget: &Budget,
) -> Result<Option<f64>, LimitExceeded> {
    text_width_inner(text, font, size, Some(budget))
}

fn text_width_inner(
    text: &str,
    font: Base14Font,
    size: f64,
    budget: Option<&Budget>,
) -> Result<Option<f64>, LimitExceeded> {
    let symbolic = is_symbolic(font);
    let mut units = 0.0;
    for (index, character) in text.chars().enumerate() {
        if index % 1024 == 0
            && let Some(budget) = budget
        {
            budget.poll()?;
        }
        let Some(byte) = winansi_byte(character) else {
            return Ok(None);
        };
        let width = if symbolic {
            coded(font, byte).map(|metric| metric.width_x)
        } else {
            font.winansi_width(byte)
        };
        units += f64::from(width.unwrap_or(MISSING_WIDTH));
    }
    Ok(Some(units * size / 1000.0))
}

/// Returns the ascent and descent, as positive fractions of the em, that a
/// form appearance places lines with: the AFM `FontBBox`, which is
/// conservative because viewers substitute other outlines, else the
/// ascender and descender, else 0.8 and 0.2.
pub fn vertical_metrics(font: Base14Font) -> (f64, f64) {
    let metrics = font.metrics();
    let mut ascent = f64::from(metrics.font_bbox.ury.max(0.0)) / 1000.0;
    let mut descent = f64::from((-metrics.font_bbox.lly).max(0.0)) / 1000.0;
    if ascent + descent <= 0.0 {
        ascent = f64::from(metrics.ascender.max(0.0)) / 1000.0;
        descent = f64::from((-metrics.descender).max(0.0)) / 1000.0;
    }
    if ascent + descent <= 0.0 {
        (ascent, descent) = (0.8, 0.2);
    }
    (ascent, descent)
}

/// Returns the font bounding box as `[x0, y0, x1, y1]` in fractions of the
/// em, `y` upward: the box of the URW base-35 font that MuPDF and other
/// renderers draw it with, not Adobe's `FontBBox`, as pymupdf 1.28.2 and
/// 2.0 report it.
pub fn font_bbox(font: Base14Font) -> [f64; 4] {
    let units: [i16; 4] = match font {
        Base14Font::Courier => [-31, -317, 683, 932],
        Base14Font::CourierOblique => [-62, -317, 792, 920],
        Base14Font::CourierBold => [-82, -393, 698, 1007],
        Base14Font::CourierBoldOblique => [-93, -393, 844, 997],
        Base14Font::Helvetica => [-210, -299, 1032, 1075],
        Base14Font::HelveticaOblique => [-123, -284, 1154, 1070],
        Base14Font::HelveticaBold => [-188, -307, 1069, 1070],
        Base14Font::HelveticaBoldOblique => [-122, -309, 1196, 1073],
        Base14Font::TimesRoman => [-168, -281, 1000, 1053],
        Base14Font::TimesItalic => [-169, -270, 1085, 951],
        Base14Font::TimesBold => [-168, -341, 1079, 1044],
        Base14Font::TimesBoldItalic => [-200, -324, 1154, 972],
        Base14Font::Symbol => [-180, -293, 1090, 1010],
        Base14Font::ZapfDingbats => [-1, -144, 981, 819],
    };
    units.map(|value| f64::from(value) / 1000.0)
}

/// Returns the ascender and descender that canvas text places lines with:
/// the top and bottom of the font's [`font_bbox`].
pub fn line_metrics(font: Base14Font) -> LineMetrics {
    let [_, descender, _, ascender] = font_bbox(font);
    LineMetrics {
        ascender,
        descender,
    }
}

/// Returns the code of a character in the font's encoding when the font
/// has a glyph for it: WinAnsi for the text fonts, and the built-in
/// encoding, addressed through WinAnsi bytes, for Symbol and ZapfDingbats.
pub fn glyph_code(font: Base14Font, character: char) -> Option<u8> {
    let byte = winansi_byte(character)?;
    let present = if is_symbolic(font) {
        coded(font, byte).is_some()
    } else {
        font.winansi_width(byte).is_some()
    };
    present.then_some(byte)
}

/// Returns the AFM metrics of the glyph that the font draws for a
/// character, or `None` when [`glyph_code`] finds none: by its WinAnsi
/// glyph name in a text font, and by its code in Symbol and ZapfDingbats.
pub fn char_metric(font: Base14Font, character: char) -> Option<&'static CharacterMetric<'static>> {
    let byte = glyph_code(font, character)?;
    if is_symbolic(font) {
        return coded(font, byte);
    }
    let name = pdf_base14_metrics::winansi_glyph_name(byte)?;
    font.metrics()
        .character_metrics
        .iter()
        .find(|metric| metric.name == name)
}

fn is_symbolic(font: Base14Font) -> bool {
    matches!(font, Base14Font::Symbol | Base14Font::ZapfDingbats)
}

/// Returns the metrics of the glyph with code `byte` in the font's built-in
/// encoding.
fn coded(font: Base14Font, byte: u8) -> Option<&'static CharacterMetric<'static>> {
    font.metrics()
        .character_metrics
        .iter()
        .find(|metric| metric.code == i32::from(byte))
}

/// Returns whether every character of `text` has a WinAnsi byte.
pub fn is_winansi(text: &str) -> bool {
    text.chars()
        .all(|character| winansi_byte(character).is_some())
}

/// Returns the WinAnsi byte of a character, as Windows code page 1252
/// assigns them: the C0 controls and Latin-1 keep their code, and the
/// characters of `0x80` to `0x9f` their slot.
pub fn winansi_byte(character: char) -> Option<u8> {
    let code = u32::from(character);
    if let Ok(byte) = u8::try_from(code)
        && !(0x80..0xa0).contains(&byte)
    {
        return Some(byte);
    }
    Some(match code {
        0x20ac => 0x80,
        0x201a => 0x82,
        0x0192 => 0x83,
        0x201e => 0x84,
        0x2026 => 0x85,
        0x2020 => 0x86,
        0x2021 => 0x87,
        0x02c6 => 0x88,
        0x2030 => 0x89,
        0x0160 => 0x8a,
        0x2039 => 0x8b,
        0x0152 => 0x8c,
        0x017d => 0x8e,
        0x2018 => 0x91,
        0x2019 => 0x92,
        0x201c => 0x93,
        0x201d => 0x94,
        0x2022 => 0x95,
        0x2013 => 0x96,
        0x2014 => 0x97,
        0x02dc => 0x98,
        0x2122 => 0x99,
        0x0161 => 0x9a,
        0x203a => 0x9b,
        0x0153 => 0x9c,
        0x017e => 0x9e,
        0x0178 => 0x9f,
        _ => return None,
    })
}
