//! The content operators that draw a laid-out box of Standard 14 text.

use pyropdf_store::budget::BudgetVec;

use super::font::winansi_byte;
use super::layout::{LayoutError, TextBox};

/// A device colour with channels from 0 to 1.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum DeviceColor {
    /// A level of gray.
    Gray(f64),
    /// Red, green, and blue.
    Rgb(f64, f64, f64),
}

impl DeviceColor {
    /// Returns the operator that selects the colour for filling, such as
    /// `0.2 0.4 0.6 rg`.
    pub fn fill_operator(self) -> String {
        match self {
            Self::Gray(gray) => format!("{} g", fmt(gray)),
            Self::Rgb(red, green, blue) => {
                format!("{} {} {} rg", fmt(red), fmt(green), fmt(blue))
            }
        }
    }
}

/// How the lines of a box align.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Align {
    /// Lines start at the box's left edge.
    #[default]
    Left,
    /// Lines are centred.
    Center,
    /// Lines end at the box's right edge.
    Right,
}

impl Align {
    /// Returns the alignment of a field's `/Q` quadding: 1 centres, 2 aligns
    /// right, and any other value aligns left.
    pub fn from_quadding(quadding: i64) -> Self {
        match quadding {
            1 => Self::Center,
            2 => Self::Right,
            _ => Self::Left,
        }
    }
}

/// The font resource, size, and colour that text is drawn with.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Style<'a> {
    /// The name of the font in the drawing's resources, without its slash.
    pub resource: &'a str,
    /// The size in points.
    pub size: f64,
    /// The fill colour.
    pub color: DeviceColor,
}

/// Appends the operators that draw the lines of `layout` in `rect`, each
/// line placed by its own `Tm` and its spaces stretched by `Tw` when it
/// justifies, and its text encoded in WinAnsi.
///
/// `rect` is `[x0, y0, x1, y1]` in a space whose `y` axis points down from
/// the top of a drawing `height` tall, such as a form's bounding box, and
/// the operators flip it into the drawing's space. A line wider than the
/// box starts at its left edge.
pub fn text_ops(
    out: &mut BudgetVec<'_, u8>,
    height: f64,
    rect: [f64; 4],
    layout: &TextBox<'_>,
    align: Align,
    style: Style<'_>,
) -> Result<(), LayoutError> {
    out.budget().poll()?;
    let channels = match style.color {
        DeviceColor::Gray(gray) => [gray, gray, gray],
        DeviceColor::Rgb(red, green, blue) => [red, green, blue],
    };
    if !height.is_finite()
        || !rect.iter().all(|value| value.is_finite())
        || rect[2] < rect[0]
        || rect[3] < rect[1]
        || ![
            rect[2] - rect[0],
            rect[3] - rect[1],
            height - rect[3],
            style.size,
            layout.ascent,
            layout.leading,
        ]
        .iter()
        .all(|value| value.is_finite())
        || style.size < 0.0
        || !channels
            .iter()
            .all(|value| value.is_finite() && (0.0..=1.0).contains(value))
    {
        return Err(LayoutError::InvalidGeometry);
    }
    let clip = format!(
        "q\n{} {} {} {} re\nW\nn\nBT\n/",
        fmt(rect[0]),
        fmt(height - rect[3]),
        fmt(rect[2] - rect[0]),
        fmt(rect[3] - rect[1])
    );
    out.try_extend_from_slice(clip.as_bytes())?;
    push_name(out, style.resource)?;
    let header = format!(" {} Tf\n{}\n", fmt(style.size), style.color.fill_operator());
    out.try_extend_from_slice(header.as_bytes())?;
    let box_width = rect[2] - rect[0];
    for (number, line) in layout.lines.iter().enumerate() {
        let x_offset = match align {
            Align::Left => 0.0,
            Align::Center => (box_width - line.width) / 2.0,
            Align::Right => box_width - line.width,
        };
        #[expect(
            clippy::cast_precision_loss,
            reason = "a layout holds at most MAX_LINES lines"
        )]
        let baseline = rect[1] + layout.ascent * style.size + number as f64 * layout.leading;
        let spaces = line.text.bytes().filter(|&byte| byte == b' ').count();
        #[expect(
            clippy::cast_precision_loss,
            reason = "a line holds fewer spaces than f64 counts exactly"
        )]
        let word_space = if line.justify && spaces > 0 {
            (box_width - line.width).max(0.0) / spaces as f64
        } else {
            0.0
        };
        if ![
            line.width,
            baseline,
            word_space,
            rect[0] + x_offset.max(0.0),
            height - baseline,
        ]
        .iter()
        .all(|value| value.is_finite())
        {
            return Err(LayoutError::InvalidGeometry);
        }
        let positioning = format!(
            "{} Tw\n1 0 0 1 {} {} Tm\n",
            fmt(word_space),
            fmt(rect[0] + x_offset.max(0.0)),
            fmt(height - baseline),
        );
        out.try_extend_from_slice(positioning.as_bytes())?;
        push_string(out, line.text)?;
        out.try_extend_from_slice(b" Tj\n")?;
    }
    out.try_extend_from_slice(b"ET\nQ\n")?;
    Ok(())
}

/// Appends `text` as a literal string of WinAnsi bytes, with the bytes
/// outside printable ASCII in octal.
fn push_string(out: &mut BudgetVec<'_, u8>, text: &str) -> Result<(), LayoutError> {
    out.try_push(b'(')?;
    for character in text.chars() {
        let byte = winansi_byte(character).ok_or(LayoutError::NotWinAnsi)?;
        match byte {
            b'(' | b')' | b'\\' => out.try_extend_from_slice(&[b'\\', byte])?,
            0x20..=0x7e => out.try_push(byte)?,
            _ => out.try_extend_from_slice(&[
                b'\\',
                b'0' + ((byte >> 6) & 0x07),
                b'0' + ((byte >> 3) & 0x07),
                b'0' + (byte & 0x07),
            ])?,
        }
    }
    out.try_push(b')')?;
    Ok(())
}

/// Appends a name's bytes with whitespace and the delimiters other than
/// `#` as `#xx`, so that the name is one token; a `#` stays, since `/DA`
/// strings give the name as it is written.
fn push_name(out: &mut BudgetVec<'_, u8>, name: &str) -> Result<(), LayoutError> {
    const HEX: &[u8; 16] = b"0123456789ABCDEF";
    for byte in name.bytes() {
        match byte {
            b'(' | b')' | b'<' | b'>' | b'[' | b']' | b'{' | b'}' | b'/' | b'%' => {
                out.try_extend_from_slice(&[
                    b'#',
                    HEX[usize::from(byte >> 4)],
                    HEX[usize::from(byte & 15)],
                ])?;
            }
            0x21..=0x7e => out.try_push(byte)?,
            _ => out.try_extend_from_slice(&[
                b'#',
                HEX[usize::from(byte >> 4)],
                HEX[usize::from(byte & 15)],
            ])?,
        }
    }
    Ok(())
}

/// Formats a number for content with at most four decimals.
fn fmt(v: f64) -> String {
    let s = format!("{v:.4}");
    let s = s.trim_end_matches('0').trim_end_matches('.');
    if s.is_empty() || s == "-" || s == "-0" {
        "0".to_owned()
    } else {
        s.to_owned()
    }
}
