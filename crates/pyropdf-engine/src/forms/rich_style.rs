//! XHTML text and scoped CSS used by a variable text field's appearance.

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::value::Value;

use crate::standard::{self, Align, Base14Font};

use super::rich_text::{self, Element, Run};
use super::{WidgetError, malformed, text};

pub(super) const RICH_TEXT: i64 = 1 << 25;

/// The PDF text string or XML text stream containing /RV.
pub(super) fn read(value: &Value, budget: &Budget) -> Result<Vec<Run>, WidgetError> {
    let xml = match value {
        Value::String(bytes) => {
            budget.check(LimitCode::FormFieldInputSize, bytes.as_bytes().len() as u64)?;
            let mut utf8 = BudgetVec::new(budget);
            text::append_text(&mut utf8, bytes.as_bytes())?;
            String::from_utf8(utf8.into_vec()).map_err(|_| malformed("invalid rich text string"))?
        }
        Value::Stream(stream) => {
            let bytes = stream.decoded(budget)?;
            budget.check(LimitCode::FormFieldInputSize, bytes.len() as u64)?;
            if bytes.starts_with(&[0xfe, 0xff])
                || bytes.starts_with(&[0xff, 0xfe])
                || bytes.starts_with(&[0, b'<'])
                || bytes.starts_with(&[b'<', 0])
            {
                let le = bytes.starts_with(&[0xff, 0xfe]) || bytes.starts_with(&[b'<', 0]);
                let mark = usize::from(matches!(bytes.first(), Some(0xff | 0xfe))) * 2;
                let (pairs, rest) = bytes[mark..].as_chunks::<2>();
                if !rest.is_empty() {
                    return Err(malformed("invalid UTF-16 rich text stream"));
                }
                let mut utf8 = BudgetVec::new(budget);
                for unit in char::decode_utf16(pairs.iter().map(|p| {
                    if le {
                        u16::from_le_bytes(*p)
                    } else {
                        u16::from_be_bytes(*p)
                    }
                })) {
                    let c = unit.map_err(|_| malformed("invalid UTF-16 rich text stream"))?;
                    utf8.try_extend_from_slice(c.encode_utf8(&mut [0; 4]).as_bytes())?;
                }
                String::from_utf8(utf8.into_vec())
                    .map_err(|_| malformed("invalid rich text stream"))?
            } else {
                String::from_utf8(bytes).map_err(|_| malformed("invalid UTF-8 rich text stream"))?
            }
        }
        _ => {
            return Err(malformed(
                "a rich text field's /RV must be a text string or stream",
            ));
        }
    };
    rich_text::parse(xml.trim_start_matches('\u{feff}'), budget)
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub(super) struct Style {
    pub(super) family: Base14Font,
    bold: bool,
    italic: bool,
    pub(super) size: f64,
    pub(super) auto_size: bool,
    pub(super) user_unit: f64,
    pub(super) color: [f64; 3],
    pub(super) align: Align,
    pub(super) rise: f64,
    pub(super) decoration: Option<bool>, // true for underline, false for strike-through
    pub(super) stretch: f64,
}

impl Style {
    pub(super) fn new(font: Base14Font, size: f64, color: [f64; 3], align: Align) -> Self {
        let mut s = Self {
            family: font,
            auto_size: false,
            user_unit: 1.,
            bold: false,
            italic: false,
            size,
            color,
            align,
            rise: 0.,
            decoration: None,
            stretch: 1.,
        };
        s.face(font);
        s
    }

    fn face(&mut self, font: Base14Font) {
        self.family = font;
        let name = font.pdf_base_name();
        self.bold = name.contains("Bold");
        self.italic = name.contains("Italic") || name.contains("Oblique");
    }

    pub(super) fn font(self) -> Base14Font {
        let name = self.family.pdf_base_name();
        let faces = if name.starts_with("Times") {
            &Base14Font::ALL[4..8]
        } else if name.starts_with("Courier") {
            &Base14Font::ALL[8..12]
        } else if name.starts_with("Helvetica") {
            &Base14Font::ALL[..4]
        } else {
            return self.family;
        };
        faces[usize::from(self.bold) + 2 * usize::from(self.italic)]
    }

    fn families(&mut self, value: &str) {
        for name in value.split(',').map(|s| s.trim().trim_matches(['\'', '"'])) {
            let face = standard::font(name).or(match name {
                "sans-serif" | "Arial" => Some(Base14Font::Helvetica),
                "serif" | "Times New Roman" | "Times" => Some(Base14Font::TimesRoman),
                "monospace" | "Courier New" => Some(Base14Font::Courier),
                _ => None,
            });
            if let Some(face) = face {
                self.family = face;
                break;
            }
        }
    }

    fn property(&mut self, key: &str, val: &str) {
        match key {
            "text-align" => match val {
                "left" => self.align = Align::Left,
                "right" => self.align = Align::Right,
                "center" => self.align = Align::Center,
                _ => {}
            },
            "font-size" => {
                if let Some(pt) = points(val).filter(|v| *v > 0.) {
                    self.size = pt / self.user_unit;
                    self.auto_size = false;
                }
            }
            "vertical-align" => {
                if let Some(pt) = points(val) {
                    self.rise = pt / self.user_unit;
                }
            }
            "font-style" => match val {
                "normal" => self.italic = false,
                "italic" | "oblique" => self.italic = true,
                _ => {}
            },
            "font-weight" => match val {
                "bold" | "600" | "700" | "800" | "900" => self.bold = true,
                "normal" | "100" | "200" | "300" | "400" | "500" => self.bold = false,
                _ => {}
            },
            "font-family" => self.families(val),
            "font" => {
                let mut rest = val;
                while let Some(word) = rest.split_ascii_whitespace().next() {
                    rest = rest
                        .trim_start()
                        .strip_prefix(word)
                        .unwrap_or_default()
                        .trim_start();
                    match word {
                        "italic" | "oblique" => self.italic = true,
                        "bold" | "600" | "700" | "800" | "900" => self.bold = true,
                        "normal" | "100" | "200" | "300" | "400" | "500" => {}
                        _ => {
                            if let Some(pt) = points(word).filter(|v| *v > 0.) {
                                self.size = pt / self.user_unit;
                                self.auto_size = false;
                                self.families(rest);
                                break;
                            } else {
                                break;
                            }
                        }
                    }
                }
            }
            "color" => {
                if let Some(rgb) = color(val) {
                    self.color = rgb;
                }
            }
            "text-decoration" => {
                self.decoration = match val {
                    "underline" => Some(true),
                    "line-through" => Some(false),
                    _ => None,
                }
            }
            "font-stretch" => {
                self.stretch = match val {
                    "ultra-condensed" => 0.5,
                    "extra-condensed" => 0.625,
                    "condensed" => 0.75,
                    "semi-condensed" => 0.875,
                    "semi-expanded" => 1.125,
                    "expanded" => 1.25,
                    "extra-expanded" => 1.5,
                    "ultra-expanded" => 2.,
                    _ => 1.,
                }
            }
            _ => {}
        }
    }

    pub(super) fn css(&mut self, declaration: &str) {
        for property in declaration.split(';') {
            if let Some((key, value)) = property.split_once(':') {
                self.property(key.trim(), value.trim());
            }
        }
    }
}

fn points(value: &str) -> Option<f64> {
    value
        .strip_suffix("pt")?
        .trim()
        .parse::<f64>()
        .ok()
        .filter(|v| v.is_finite() && v.abs() <= 1_000_000.)
}

fn color(value: &str) -> Option<[f64; 3]> {
    let bytes = if let Some(hex) = value
        .strip_prefix('#')
        .filter(|s| s.is_ascii() && s.len() == 6)
    {
        [
            u8::from_str_radix(&hex[..2], 16).ok()?,
            u8::from_str_radix(&hex[2..4], 16).ok()?,
            u8::from_str_radix(&hex[4..], 16).ok()?,
        ]
    } else {
        let mut parts = value
            .strip_prefix("rgb(")?
            .strip_suffix(')')?
            .split(',')
            .map(|s| s.trim().parse::<u8>().ok());
        let rgb = [parts.next()??, parts.next()??, parts.next()??];
        if parts.next().is_some() {
            return None;
        }
        rgb
    };
    Some(bytes.map(|b| f64::from(b) / 255.))
}

/// Emits styled characters, folding XHTML whitespace across inline scopes but
/// preserving explicit breaks, empty paragraphs, and nonbreaking spaces.
pub(super) fn walk(
    runs: &[Run],
    initial: Style,
    budget: &Budget,
    mut emit: impl FnMut(char, Style) -> Result<(), WidgetError>,
) -> Result<(), WidgetError> {
    let mut styles = BudgetVec::new(budget);
    let mut style = initial;
    let mut paragraph = false;
    let mut pending = None;
    let mut line_start = true;
    for run in runs {
        budget.poll()?;
        match run {
            Run::Open(el, css) => {
                styles.try_push(style)?;
                match el {
                    Element::Paragraph => {
                        if paragraph {
                            emit('\n', style)?;
                        }
                        paragraph = true;
                        pending = None;
                        line_start = true;
                    }
                    Element::Break => {
                        emit('\n', style)?;
                        pending = None;
                        line_start = true;
                    }
                    Element::Bold => style.bold = true,
                    Element::Italic => style.italic = true,
                    Element::Underline => style.decoration = Some(true),
                    Element::Superscript => {
                        style.rise += style.size * 0.35;
                        style.size *= 0.75;
                    }
                    Element::Subscript => {
                        style.rise -= style.size * 0.2;
                        style.size *= 0.75;
                    }
                    _ => {}
                }
                if let Some(css) = css {
                    style.css(css);
                }
            }
            Run::Close(_) => {
                style = styles
                    .pop()
                    .ok_or_else(|| malformed("unbalanced rich text scope"))?
            }
            Run::Text(text) => {
                for (i, c) in text.char_indices() {
                    if i % 1024 == 0 {
                        budget.poll()?;
                    }
                    if matches!(c, ' ' | '\r' | '\t' | '\n') {
                        if !line_start {
                            pending = Some(style);
                        }
                    } else {
                        if let Some(space) = pending.take() {
                            emit(' ', space)?;
                        }
                        emit(c, style)?;
                        line_start = false;
                    }
                }
            }
        }
    }
    Ok(())
}

pub(super) fn plain(runs: &[Run], budget: &Budget) -> Result<String, WidgetError> {
    let mut text = BudgetVec::new(budget);
    walk(
        runs,
        Style::new(Base14Font::Helvetica, 12., [0.; 3], Align::Left),
        budget,
        |c, _| {
            text.try_extend_from_slice(c.encode_utf8(&mut [0; 4]).as_bytes())?;
            Ok(())
        },
    )?;
    String::from_utf8(text.into_vec()).map_err(|_| malformed("invalid decoded XHTML text"))
}
