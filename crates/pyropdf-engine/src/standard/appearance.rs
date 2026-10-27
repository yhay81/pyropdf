//! The `/DA` strings that select a Standard 14 font, its size, and its
//! colour for the text of a form field or a free-text annotation.

/// The Standard 14 fonts by the resource names that `/DA` strings give
/// them, as Acrobat and MuPDF name them.
pub const DA_FONTS: [(&str, &str); 14] = [
    ("Helv", "Helvetica"),
    ("HeBo", "Helvetica-Bold"),
    ("HeOb", "Helvetica-Oblique"),
    ("HeBO", "Helvetica-BoldOblique"),
    ("TiRo", "Times-Roman"),
    ("TiBo", "Times-Bold"),
    ("TiIt", "Times-Italic"),
    ("TiBI", "Times-BoldItalic"),
    ("Cour", "Courier"),
    ("CoBo", "Courier-Bold"),
    ("CoOb", "Courier-Oblique"),
    ("CoBO", "Courier-BoldOblique"),
    ("Symb", "Symbol"),
    ("ZaDb", "ZapfDingbats"),
];

/// The longest `/DA` string that is read; a longer one reads as empty.
pub const MAX_DEFAULT_APPEARANCE: usize = 4_096;

/// The font resource name, size, and colour of a `/DA` string.
#[derive(Clone, Debug, PartialEq)]
pub struct DefaultAppearance {
    /// The name of the font resource, without its slash.
    pub font: String,
    /// The size in points; 0 fits the text to its box.
    pub size: f64,
    /// The RGB colour of the text, each channel from 0 to 1.
    pub color: (f64, f64, f64),
}

impl DefaultAppearance {
    /// Parses a `/DA` string: the last `Tf` and the last colour operator,
    /// `g`, `rg`, or `k`, whose CMYK converts naively to RGB. What the
    /// string does not set is Helvetica (`Helv`), size 0, and black, and so
    /// is everything of a string longer than [`MAX_DEFAULT_APPEARANCE`].
    pub fn parse(bytes: &[u8]) -> Self {
        let mut result = Self {
            font: "Helv".to_owned(),
            size: 0.0,
            color: (0.0, 0.0, 0.0),
        };
        if bytes.len() > MAX_DEFAULT_APPEARANCE {
            return result;
        }
        // The operators read at most four operands, so only the last four
        // are kept.
        let mut operands: [&[u8]; 4] = [b""; 4];
        let mut count = 0_usize;
        for token in bytes.split(u8::is_ascii_whitespace) {
            if token.is_empty() {
                continue;
            }
            // The `index`th operand from the end, from 1.
            let operand = |index: usize| (index <= count.min(4)).then(|| operands[4 - index]);
            let number = |index: usize| {
                std::str::from_utf8(operand(index)?)
                    .ok()?
                    .parse::<f64>()
                    .ok()
                    .filter(|value| value.is_finite())
            };
            match token {
                b"Tf" => {
                    if let (Some(name), Some(size)) = (operand(2), number(1))
                        && let Some(name) = name.strip_prefix(b"/")
                    {
                        result.font = String::from_utf8_lossy(name).into_owned();
                        if size >= 0.0 {
                            result.size = size;
                        }
                    }
                }
                b"g" => {
                    if let Some(gray) = number(1) {
                        let gray = gray.clamp(0.0, 1.0);
                        result.color = (gray, gray, gray);
                    }
                }
                b"rg" => {
                    if let (Some(red), Some(green), Some(blue)) = (number(3), number(2), number(1))
                    {
                        result.color = (
                            red.clamp(0.0, 1.0),
                            green.clamp(0.0, 1.0),
                            blue.clamp(0.0, 1.0),
                        );
                    }
                }
                b"k" => {
                    if let (Some(cyan), Some(magenta), Some(yellow), Some(black)) =
                        (number(4), number(3), number(2), number(1))
                    {
                        let [cyan, magenta, yellow, black] =
                            [cyan, magenta, yellow, black].map(|value| value.clamp(0.0, 1.0));
                        result.color = (
                            (1.0 - cyan) * (1.0 - black),
                            (1.0 - magenta) * (1.0 - black),
                            (1.0 - yellow) * (1.0 - black),
                        );
                    }
                }
                _ => {
                    operands.rotate_left(1);
                    operands[3] = token;
                    count = count.saturating_add(1);
                    continue;
                }
            }
            count = 0;
        }
        result
    }
}

/// Returns the PostScript name of the Standard 14 font that a `/DA`
/// resource name abbreviates, Helvetica for any other name.
pub fn da_font(resource: &str) -> &'static str {
    DA_FONTS
        .iter()
        .find(|(known, _)| *known == resource)
        .map_or("Helvetica", |(_, font)| font)
}
