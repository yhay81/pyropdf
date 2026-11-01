//! Free text and stamps: annotations whose appearance draws Standard 14
//! text, as pylopdf's `draw_free_text` and `draw_stamp` draw them.

use pyropdf_store::budget::{BudgetVec, LimitCode};
use pyropdf_store::value::{Dict, Name, Value};
use pyropdf_store::{Error, LimitExceeded, Result};

use super::shapes::{LINE_ENDINGS, draw_ending, polyline};
use super::{Canvas, KAPPA, Paint, Reader, Stroke, quarter_turn};
use crate::forms::text_string;
use crate::standard::{
    Align, Base14Font, DA_FONTS, DefaultAppearance, DeviceColor, LayoutError, Line,
    MAX_DEFAULT_APPEARANCE, Placement, Style, TextBox, da_font, font, line_metrics, text_ops,
    text_width, textbox_layout,
};

/// The space between a free text's border and its text.
const FREE_TEXT_PADDING: f64 = 2.0;

/// Whether the text of a free text could be drawn.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TextFit {
    /// The text is drawn, or there is none.
    Fits,
    /// The text does not fit its box, and is not drawn.
    Unfit,
    /// The text holds a character that no Standard 14 font draws, and is
    /// not drawn.
    NotWinAnsi,
}

/// The font that a drawing's text names: its resource name and the
/// PostScript name of the Standard 14 font.
pub(super) type FontResource = (&'static str, &'static str);

/// Returns the `/DA` of a free text: its last `Tf` and colour operator;
/// Helvetica, 12 points, and black for what it does not set, or for a
/// malformed string. An oversized string is refused.
pub fn default_appearance(reader: &Reader<'_>, dict: &Dict) -> Result<DefaultAppearance> {
    let parsed = match reader.entry(dict, "DA")?.as_deref() {
        Some(Value::String(text)) => {
            let size = u64::try_from(text.as_bytes().len()).unwrap_or(u64::MAX);
            reader
                .budget
                .check(LimitCode::FormDefaultAppearanceSize, size)?;
            if text.as_bytes().len() > MAX_DEFAULT_APPEARANCE {
                return Err(LimitExceeded::over(
                    LimitCode::FormDefaultAppearanceSize,
                    MAX_DEFAULT_APPEARANCE as u64,
                    size,
                )
                .into());
            }
            DefaultAppearance::parse(text.as_bytes()).ok()
        }
        _ => None,
    };
    let mut result = parsed
        .or_else(|| DefaultAppearance::parse(b"").ok())
        .ok_or_else(|| Error::malformed("an empty default appearance is refused"))?;
    if result.size <= 0.0 {
        result.size = 12.0;
    }
    Ok(result)
}

/// Returns the Standard 14 font that `name`, a PostScript name, selects
/// and the resource name that `/DA` strings give it.
fn resource(name: &str) -> Option<(Base14Font, FontResource)> {
    let (abbreviation, postscript) = DA_FONTS.iter().find(|(_, font)| *font == name)?;
    Some((font(postscript)?, (abbreviation, postscript)))
}

/// Draws a free text on `canvas`, a drawing of `size`: its box inside
/// `/RD`, filled with `/C` and outlined in the text colour when it has a
/// border, the callout line of `/CL` with the `/LE` ending at its first
/// point, and `/Contents` laid out in the box with the font, size, and
/// colour of `/DA`, aligned by `/Q` and turned counterclockwise by
/// `/Rotate`. Text that does not fit, or that no Standard 14 font draws, is
/// left out. Returns whether the text fit and the font that it names.
pub(super) fn free_text(
    canvas: &mut Canvas<'_>,
    reader: &Reader<'_>,
    dict: &Dict,
    paint: &Paint,
    rect: [f64; 4],
) -> Result<(TextFit, Option<FontResource>)> {
    let [x0, y0, x1, y1] = rect;
    let to_canvas = |(x, y): (f64, f64)| (x - x0, y1 - y);
    let size = (x1 - x0, y1 - y0);
    let da = default_appearance(reader, dict)?;
    let [left, top, right, bottom] = reader.rect_differences(dict, size)?;
    let inner = [left, top, size.0 - right, size.1 - bottom];
    // Free text without a border draws none.
    let width = reader.border(dict)?.map_or(0.0, |border| border.width);
    let stroke = (width > 0.0).then(|| Stroke {
        color: da.color,
        width,
        cap: 0,
        join: 0,
        dashes: &paint.dashes,
    });
    let half = if stroke.is_some() { width / 2.0 } else { 0.0 };
    let (a0, b0, a1, b1) = (
        inner[0] + half,
        inner[1] + half,
        inner[2] - half,
        inner[3] - half,
    );
    if (paint.stroke.is_some() || stroke.is_some()) && a1 > a0 && b1 > b0 {
        canvas.path("mlllh", &[a0, b0, a1, b0, a1, b1, a0, b1]);
        canvas.draw(paint.stroke, stroke.as_ref());
    }
    callout(canvas, reader, dict, &da, width, to_canvas)?;
    let text = match reader.entry(dict, "Contents")?.as_deref() {
        Some(Value::String(text)) => {
            // The decoded text, its line breaks replaced, and its layout.
            reader.budget.charge(
                LimitCode::MemorySize,
                (text.as_bytes().len() as u64).saturating_mul(12),
            )?;
            text_string(text.as_bytes())
        }
        _ => String::new(),
    };
    let text = text.replace("\r\n", "\n").replace('\r', "\n");
    let inset = width + FREE_TEXT_PADDING;
    let area = [
        inner[0] + inset,
        inner[1] + inset,
        inner[2] - inset,
        inner[3] - inset,
    ];
    let mut has_text = false;
    for (index, character) in text.chars().enumerate() {
        if index % 1024 == 0 {
            reader.budget.poll()?;
        }
        if !character.is_whitespace() {
            has_text = true;
            break;
        }
    }
    if !has_text {
        return Ok((TextFit::Fits, None));
    }
    if area[2] <= area[0] || area[3] <= area[1] {
        return Ok((TextFit::Unfit, None));
    }
    let align = match reader.entry(dict, "Q")?.as_deref() {
        Some(Value::Integer(1)) => Align::Center,
        Some(Value::Integer(2)) => Align::Right,
        _ => Align::Left,
    };
    let Some((face, font)) = resource(da_font(&da.font)) else {
        return Ok((TextFit::Unfit, None));
    };
    let turn = quarter_turn(reader, dict)?.rem_euclid(360);
    let turn = if turn % 90 == 0 { turn } else { 0 };
    let (width, height) = (area[2] - area[0], area[3] - area[1]);
    let turned = if turn % 180 == 0 {
        (width, height)
    } else {
        (height, width)
    };
    let placement = Placement::lines(line_metrics(face), None);
    let layout = match textbox_layout(
        &text,
        turned,
        face,
        da.size,
        placement,
        false,
        reader.budget,
    ) {
        Ok(layout) => layout,
        Err(LayoutError::NotWinAnsi) => return Ok((TextFit::NotWinAnsi, None)),
        Err(LayoutError::Limit(refusal)) => return Err(refusal.into()),
        Err(_) => return Ok((TextFit::Unfit, None)),
    };
    if !layout.fits() {
        return Ok((TextFit::Unfit, None));
    }
    // The box's lower-left corner in the form's space, whose y axis points
    // up.
    let origin = [area[0], canvas.height - area[3]];
    let style = Style {
        resource: font.0,
        size: da.size,
        color: DeviceColor::Rgb(da.color.0, da.color.1, da.color.2),
    };
    write_text(
        canvas,
        turned_matrix(turn, turned, origin),
        turned,
        &layout,
        align,
        style,
    )?;
    Ok((TextFit::Fits, Some(font)))
}

/// Draws the callout line of `/CL`, from the point it shows to the box,
/// at least one point wide, with the `/LE` ending at its first point.
fn callout(
    canvas: &mut Canvas<'_>,
    reader: &Reader<'_>,
    dict: &Dict,
    da: &DefaultAppearance,
    width: f64,
    to_canvas: impl Fn((f64, f64)) -> (f64, f64) + Copy,
) -> Result<()> {
    let Some(values) = reader.numbers(dict, "CL", 6)? else {
        return Ok(());
    };
    if !matches!(values.len(), 4 | 6) {
        return Ok(());
    }
    let points: Vec<(f64, f64)> = values
        .as_chunks::<2>()
        .0
        .iter()
        .map(|&[x, y]| (x, y))
        .collect();
    let line_width = width.max(1.0);
    polyline(canvas, &points, to_canvas);
    canvas.stroke(da.color, line_width, 0, 0, &[]);
    let ending_index = |name: &Name| {
        LINE_ENDINGS
            .iter()
            .position(|known| known.as_bytes() == name.as_bytes())
    };
    let ending = match reader.entry(dict, "LE")?.as_deref() {
        Some(Value::Name(name)) => ending_index(name),
        Some(Value::Array(items)) => match items.iter().next() {
            Some(item) => match &*reader.resolve(item)? {
                Value::Name(name) => ending_index(name),
                _ => None,
            },
            None => None,
        },
        _ => None,
    }
    .unwrap_or(0);
    let paint = Paint {
        stroke: Some(da.color),
        interior: reader.color(dict, "IC")?,
        width: line_width,
        dashes: Vec::new(),
    };
    draw_ending(canvas, &paint, ending, points[0], points[1], to_canvas)
}

/// Returns the matrix that turns a drawing of `size` counterclockwise by
/// `turn` degrees and moves its bounds to `origin`.
pub(super) fn turned_matrix(turn: i64, size: (f64, f64), origin: [f64; 2]) -> [f64; 6] {
    let (w, h) = size;
    let (a, b, c, d, e, f) = match turn.rem_euclid(360) {
        90 => (0.0, 1.0, -1.0, 0.0, h, 0.0),
        180 => (-1.0, 0.0, 0.0, -1.0, w, h),
        270 => (0.0, -1.0, 1.0, 0.0, 0.0, w),
        _ => (1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
    };
    [a, b, c, d, e + origin[0], f + origin[1]]
}

/// Appends the operators that draw `layout` in a box of `size` that
/// `matrix` places in the form's space.
fn write_text(
    canvas: &mut Canvas<'_>,
    matrix: [f64; 6],
    size: (f64, f64),
    layout: &TextBox<'_>,
    align: Align,
    style: Style<'_>,
) -> Result<()> {
    let mut out = BudgetVec::new(canvas.out.bytes.budget());
    text_ops(
        &mut out,
        size.1,
        [0.0, 0.0, size.0, size.1],
        layout,
        align,
        style,
    )
    .map_err(|error| match error {
        LayoutError::Limit(refusal) => refusal.into(),
        _ => Error::malformed("an annotation's text has unusable geometry"),
    })?;
    let ops = std::str::from_utf8(&out)
        .map_err(|_| Error::malformed("an annotation's text operators are not ASCII"))?;
    canvas.out.push_str("q\n");
    let [a, b, c, d, e, f] = matrix.map(super::fmt);
    canvas
        .out
        .push_str(&format!("{a} {b} {c} {d} {e} {f} cm\n"));
    canvas.out.push_str(ops);
    canvas.out.push_str("Q\n");
    Ok(())
}

/// The stamps of ISO 32000-2 section 12.5.6.12 by `/Name`, with the label
/// and the colour that each is drawn in: green for approval, red for
/// restriction, and blue for the rest.
pub const STAMPS: [(&str, &str, (f64, f64, f64)); 14] = [
    ("Approved", "APPROVED", GREEN),
    ("AsIs", "AS IS", BLUE),
    ("Confidential", "CONFIDENTIAL", RED),
    ("Departmental", "DEPARTMENTAL", BLUE),
    ("Experimental", "EXPERIMENTAL", BLUE),
    ("Expired", "EXPIRED", RED),
    ("Final", "FINAL", GREEN),
    ("ForComment", "FOR COMMENT", BLUE),
    ("ForPublicRelease", "FOR PUBLIC RELEASE", GREEN),
    ("NotApproved", "NOT APPROVED", RED),
    ("NotForPublicRelease", "NOT FOR PUBLIC RELEASE", RED),
    ("Sold", "SOLD", BLUE),
    ("TopSecret", "TOP SECRET", RED),
    ("Draft", "DRAFT", RED),
];
const GREEN: (f64, f64, f64) = (0.0, 0.5, 0.0);
const RED: (f64, f64, f64) = (0.8, 0.0, 0.0);
const BLUE: (f64, f64, f64) = (0.0, 0.0, 0.6);

/// The longest unknown stamp name that is drawn as its own label; longer
/// names are refused with `annotation_text_size`.
const MAX_STAMP_LABEL: usize = 256;

/// Draws a stamp on a canvas of `size`: its label in Helvetica-Bold, as
/// large as fits, centred in a rounded frame, both in `/C` or the stamp's
/// own colour. An unknown name draws itself as the label. Returns the font
/// that the label names, if one is drawn.
pub(super) fn stamp(
    canvas: &mut Canvas<'_>,
    paint: &Paint,
    icon: &[u8],
    size: (f64, f64),
) -> Result<Option<FontResource>> {
    let size_in_bytes = u64::try_from(icon.len()).unwrap_or(u64::MAX);
    canvas
        .out
        .bytes
        .budget()
        .check(LimitCode::AnnotationTextSize, size_in_bytes)?;
    if icon.len() > MAX_STAMP_LABEL {
        return Err(LimitExceeded::over(
            LimitCode::AnnotationTextSize,
            MAX_STAMP_LABEL as u64,
            size_in_bytes,
        )
        .into());
    }
    let mut unknown = BudgetVec::new(canvas.out.bytes.budget());
    let known = STAMPS.iter().find(|(known, _, _)| known.as_bytes() == icon);
    let (label, own) = match known {
        Some((_, label, color)) => (*label, *color),
        None => {
            for chunk in icon.utf8_chunks() {
                for character in chunk.valid().chars().flat_map(char::to_uppercase) {
                    let mut bytes = [0; 4];
                    unknown.try_extend_from_slice(character.encode_utf8(&mut bytes).as_bytes())?;
                }
                if !chunk.invalid().is_empty() {
                    unknown.try_extend_from_slice("\u{fffd}".as_bytes())?;
                }
            }
            (
                std::str::from_utf8(&unknown)
                    .map_err(|_| Error::malformed("a stamp label is not UTF-8"))?,
                RED,
            )
        }
    };
    let color = paint.stroke.unwrap_or(own);
    let (w, h) = size;
    let frame = (w.min(h) / 12.0).max(0.5);
    let radius = (w.min(h) / 6.0).max(frame);
    let (a0, b0, a1, b1) = (frame / 2.0, frame / 2.0, w - frame / 2.0, h - frame / 2.0);
    if a1 - a0 > 2.0 * radius && b1 - b0 > 2.0 * radius {
        let k = radius * (1.0 - KAPPA);
        canvas.path(
            "mlclclclch",
            &[
                a0 + radius,
                b0,
                a1 - radius,
                b0,
                a1 - k,
                b0,
                a1,
                b0 + k,
                a1,
                b0 + radius,
                a1,
                b1 - radius,
                a1,
                b1 - k,
                a1 - k,
                b1,
                a1 - radius,
                b1,
                a0 + radius,
                b1,
                a0 + k,
                b1,
                a0,
                b1 - k,
                a0,
                b1 - radius,
                a0,
                b0 + radius,
                a0,
                b0 + k,
                a0 + k,
                b0,
                a0 + radius,
                b0,
            ],
        );
        canvas.stroke(color, frame, 0, 0, &[]);
    }
    let Some((face, font)) = resource("Helvetica-Bold") else {
        return Ok(None);
    };
    let unit = text_width(label, face, 1.0).unwrap_or(0.0);
    let room = (w - 4.0 * frame, h - 4.0 * frame);
    if unit <= 0.0 || room.0 <= 0.0 || room.1 <= 0.0 {
        return Ok(None);
    }
    let text_size = (room.1 * 0.7).min(room.0 / unit);
    let width = unit * text_size;
    if !(text_size.is_finite() && text_size > 0.0) {
        return Ok(None);
    }
    // One centred line whose baseline lies 0.36 em below the middle.
    let layout = TextBox {
        lines: vec![Line {
            text: label,
            width,
            justify: false,
        }],
        height: h,
        spare_height: 0.0,
        ascent: (h / 2.0 + text_size * 0.36) / text_size,
        leading: text_size,
    };
    let style = Style {
        resource: font.0,
        size: text_size,
        color: DeviceColor::Rgb(color.0, color.1, color.2),
    };
    write_text(
        canvas,
        turned_matrix(0, size, [0.0, 0.0]),
        size,
        &layout,
        Align::Center,
        style,
    )?;
    Ok(Some(font))
}
