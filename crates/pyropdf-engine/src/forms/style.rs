//! Widget visual styling, decoration operators, and text/comb content layout.

use std::borrow::Cow;

use pyropdf_store::budget::{Budget, BudgetVec};
use pyropdf_store::value::{Dict, Value};
use pyropdf_store::{LimitExceeded, Result};

use crate::annotations::Reader;
use crate::standard::{
    self, Align, Base14Font, DefaultAppearance, DeviceColor, FIT_STEP, FORM_LINE_HEIGHT,
    LayoutError, MAX_FIT_SIZE, MIN_FIT_SIZE, Style, TextBox,
};

use super::appearance::integer;
use super::face::FaceText;
use super::{WidgetError, malformed};

/// A device colour of `/MK`, with channels from 0 to 1.
#[derive(Clone, Copy)]
enum Color {
    Gray(f64),
    Rgb(f64, f64, f64),
    Cmyk(f64, f64, f64, f64),
}

impl Color {
    /// Reads a colour array of one, three, or four numbers.
    fn read(reader: &Reader<'_>, value: Option<&Value>) -> Result<Option<Self>> {
        let Some(value) = value else {
            return Ok(None);
        };
        let Some(values) = reader.numbers_of(value, 4)? else {
            return Ok(None);
        };
        #[expect(
            clippy::disallowed_methods,
            reason = "`numbers_of` reads four numbers at most"
        )]
        let values: Vec<f64> = values.into_iter().map(|v| v.clamp(0.0, 1.0)).collect();
        Ok(match values[..] {
            [gray] => Some(Self::Gray(gray)),
            [red, green, blue] => Some(Self::Rgb(red, green, blue)),
            [cyan, magenta, yellow, black] => Some(Self::Cmyk(cyan, magenta, yellow, black)),
            _ => None,
        })
    }

    /// Returns the operator that selects the colour for filling or
    /// stroking.
    #[expect(
        clippy::disallowed_methods,
        reason = "four numbers and an operator at most"
    )]
    fn operator(self, stroke: bool) -> String {
        let (values, operator) = match self {
            Self::Gray(gray) => (vec![gray], "g"),
            Self::Rgb(red, green, blue) => (vec![red, green, blue], "rg"),
            Self::Cmyk(cyan, magenta, yellow, black) => (vec![cyan, magenta, yellow, black], "k"),
        };
        let mut out: Vec<String> = values.into_iter().map(fmt).collect();
        out.push(if stroke {
            operator.to_uppercase()
        } else {
            operator.to_owned()
        });
        out.join(" ") + "\n"
    }
}

/// The size and rotation of a widget, as its text is laid out, and its
/// background and border.
pub(super) struct WidgetStyle {
    pub(super) width: f64,
    pub(super) height: f64,
    pub(super) matrix: Option<[f64; 6]>,
    background: Option<Color>,
    border: Option<Color>,
    border_width: f64,
    pub(super) unit: f64,
}

impl WidgetStyle {
    /// Reads a widget's `/Rect`, its `/MK` rotation and colours, and its
    /// border width from `/BS` or the legacy `/Border`.
    pub(super) fn read(reader: &Reader<'_>, widget: &Dict) -> Result<Self, WidgetError> {
        let Some(rect) = widget.get("Rect") else {
            return Err(malformed("widget has no Rect"));
        };
        let resolved = reader.resolve(rect)?;
        let Value::Array(items) = &*resolved else {
            return Err(malformed("widget Rect is invalid"));
        };
        let values = if items.len() == 4 {
            reader.numbers_of(&resolved, 4)?
        } else {
            None
        };
        let Some(&[x0, y0, x1, y1]) = values.as_deref() else {
            return Err(malformed("widget Rect must contain four numbers"));
        };
        let (width, height) = ((x1 - x0).abs(), (y1 - y0).abs());
        if !(width > 0.0 && height > 0.0 && width.is_finite() && height.is_finite()) {
            return Err(malformed(
                "widget Rect must have positive finite dimensions",
            ));
        }
        let mark = reader.entry(widget, "MK")?;
        let mark = match mark.as_deref() {
            Some(Value::Dict(mark)) => Some(mark),
            _ => None,
        };
        let entry = |key| match mark {
            Some(mark) => reader.entry(mark, key),
            None => Ok(None),
        };
        let rotation = integer(entry("R")?.as_deref()).unwrap_or(0).rem_euclid(360);
        let background = Color::read(reader, entry("BG")?.as_deref())?;
        let border = Color::read(reader, entry("BC")?.as_deref())?.or(Some(Color::Gray(0.0)));
        let mut border_width = None;
        if let Some(Value::Dict(style)) = reader.entry(widget, "BS")?.as_deref()
            && let Some(width) = style.get("W")
        {
            border_width = reader.number(width)?;
        }
        if border_width.is_none()
            && let Some(Value::Array(border)) = reader.entry(widget, "Border")?.as_deref()
            && let Some(width) = border.get(2)
        {
            border_width = reader.number(width)?;
        }
        let border_width = border_width
            .unwrap_or(1.0)
            .clamp(0.0, width.min(height) / 2.0);
        let (layout_width, layout_height, matrix) = match rotation {
            90 => (height, width, Some([0.0, 1.0, -1.0, 0.0, width, 0.0])),
            180 => (width, height, Some([-1.0, 0.0, 0.0, -1.0, width, height])),
            270 => (height, width, Some([0.0, -1.0, 1.0, 0.0, 0.0, height])),
            _ => (width, height, None),
        };
        Ok(Self {
            width: layout_width,
            height: layout_height,
            matrix,
            background,
            border,
            border_width,
            unit: reader.unit,
        })
    }

    /// Returns the box that the text is drawn in, inside the border, with
    /// `y` downward from the top.
    pub(super) fn content_rect(&self) -> [f64; 4] {
        let pt = 1.0 / self.unit;
        let inset = (self.border_width + pt)
            .max(2.0 * pt)
            .min(self.width.min(self.height) / 3.0);
        [
            inset,
            inset,
            (self.width - inset).max(inset),
            (self.height - inset).max(inset),
        ]
    }

    /// Draws a check box, with a check mark when `on`, or a radio button,
    /// with a dot when `on`.
    pub(super) fn button(
        &self,
        on: bool,
        radio: bool,
        budget: &Budget,
    ) -> Result<Vec<u8>, LimitExceeded> {
        let mut out = BudgetVec::new(budget);
        if radio {
            self.round_decoration(&mut out)?;
        } else {
            self.decoration(&mut out)?;
        }
        let (width, height) = (self.width, self.height);
        if on && radio {
            let mut ops = circle(width / 2.0, height / 2.0, width.min(height) * 0.22);
            #[expect(
                clippy::disallowed_methods,
                reason = "a constant operator after the circle"
            )]
            ops.push_str("0 g\nf\n");
            out.try_extend_from_slice(ops.as_bytes())?;
        } else if on {
            let ops = format!(
                "q\n0 G\n{} w\n{} {} m\n{} {} l\n{} {} l\nS\nQ\n",
                fmt((width.min(height) * 0.10).max(1.0 / self.unit)),
                fmt(width * 0.20),
                fmt(height * 0.52),
                fmt(width * 0.42),
                fmt(height * 0.27),
                fmt(width * 0.82),
                fmt(height * 0.76),
            );
            out.try_extend_from_slice(ops.as_bytes())?;
        }
        Ok(out.into_vec())
    }

    /// Draws the background and the border of a radio button as circles.
    #[expect(
        clippy::disallowed_methods,
        reason = "a fixed number of operators, of numbers that `fmt` writes"
    )]
    fn round_decoration(&self, out: &mut BudgetVec<'_, u8>) -> Result<(), LimitExceeded> {
        let radius = ((self.width.min(self.height) - self.border_width) / 2.0).max(0.0);
        let (x, y) = (self.width / 2.0, self.height / 2.0);
        let mut ops = String::from("q\n");
        if let Some(color) = self.background {
            ops.push_str(&color.operator(false));
            ops.push_str(&circle(x, y, radius));
            ops.push_str("f\n");
        }
        if self.border_width > 0.0
            && let Some(color) = self.border
        {
            ops.push_str(&color.operator(true));
            ops.push_str(&format!("{} w\n", fmt(self.border_width)));
            ops.push_str(&circle(x, y, radius));
            ops.push_str("S\n");
        }
        ops.push_str("Q\n");
        out.try_extend_from_slice(ops.as_bytes())
    }

    /// Draws the background and the rectangular border.
    #[expect(
        clippy::disallowed_methods,
        reason = "a fixed number of operators, of numbers that `fmt` writes"
    )]
    pub(super) fn decoration(&self, out: &mut BudgetVec<'_, u8>) -> Result<(), LimitExceeded> {
        let mut ops = String::from("q\n");
        if let Some(color) = self.background {
            ops.push_str(&color.operator(false));
            ops.push_str(&format!(
                "0 0 {} {} re f\n",
                fmt(self.width),
                fmt(self.height)
            ));
        }
        if self.border_width > 0.0
            && let Some(color) = self.border
        {
            ops.push_str(&color.operator(true));
            let inset = self.border_width / 2.0;
            ops.push_str(&format!(
                "{} w\n{} {} {} {} re S\n",
                fmt(self.border_width),
                fmt(inset),
                fmt(inset),
                fmt((self.width - self.border_width).max(0.0)),
                fmt((self.height - self.border_width).max(0.0)),
            ));
        }
        ops.push_str("Q\n");
        out.try_extend_from_slice(ops.as_bytes())
    }
}

/// The text style of a widget's `/DA`.
pub(super) struct TextStyle {
    font: Base14Font,
    /// The font's resource name.
    pub(super) resource: String,
    pub(super) base_font: &'static str,
    /// The size in points; 0 fits the text to the widget.
    size: f64,
    color: (f64, f64, f64),
}

impl TextStyle {
    /// Returns the style of a `/DA`: Helvetica, as `Helv`, for a font that
    /// is no Standard 14 abbreviation, and Helvetica fitted in black without
    /// one.
    pub(super) fn new(da: Option<DefaultAppearance>, resolved_font: Option<&'static str>) -> Self {
        let (resource, size, color) = da.map_or_else(
            || ("Helv".to_owned(), 0.0, (0.0, 0.0, 0.0)),
            |da| (da.font, da.size, da.color),
        );
        let base_font = resolved_font.unwrap_or_else(|| standard::da_font(&resource));
        let resource = if resolved_font.is_none() && base_font == "Helvetica" && resource != "Helv"
        {
            "Helv".to_owned()
        } else {
            resource
        };
        Self {
            font: standard::font(base_font).unwrap_or(Base14Font::Helvetica),
            resource,
            base_font,
            size: if size.is_finite() && size > 0.0 {
                size
            } else {
                0.0
            },
            color,
        }
    }

    fn style(&self, size: f64) -> Style<'_> {
        let (red, green, blue) = self.color;
        Style {
            resource: &self.resource,
            size,
            color: DeviceColor::Rgb(red, green, blue),
        }
    }
}

/// Draws a text or choice value, fitted to the widget unless the `/DA`
/// sets a size.
pub(super) fn text_ops(
    out: &mut BudgetVec<'_, u8>,
    style: &WidgetStyle,
    text: &TextStyle,
    value: &str,
    multiline: bool,
    align: Align,
    face: Option<&mut FaceText<'_>>,
) -> Result<(), WidgetError> {
    if value.is_empty() {
        return Ok(());
    }
    let budget = out.budget();
    let value = if multiline {
        Cow::Borrowed(value)
    } else {
        standard::single_line(value, budget)?
    };
    let rect = style.content_rect();
    let size = (rect[2] - rect[0], rect[3] - rect[1]);
    if size.0 <= 0.0 || size.1 <= 0.0 {
        return Ok(());
    }
    let Some(face) = face else {
        let (layout, font_size) = fit_in_unit(
            &value,
            size,
            text.size,
            multiline,
            style.unit,
            budget,
            |value, size, font_size| {
                standard::form_layout(value, size, text.font, font_size, FORM_LINE_HEIGHT, budget)
            },
        )?;
        standard::text_ops(
            out,
            style.height,
            rect,
            &layout,
            align,
            text.style(font_size),
        )?;
        return Ok(());
    };
    let (layout, font_size) = fit_in_unit(
        &value,
        size,
        text.size,
        multiline,
        style.unit,
        budget,
        |value, size, font_size| face.layout(value, size, font_size, FORM_LINE_HEIGHT),
    )?;
    standard::text_ops_with(
        out,
        style.height,
        rect,
        &layout,
        align,
        text.style(font_size),
        |out, line| face.show(out, line, font_size),
    )?;
    Ok(())
}

/// Fits `text` in `box_size` as [`standard::fit_with`] does, scaling the
/// auto-fit bounds (`12` pt down to `0.01` pt) into the page's user space.
fn fit_in_unit<'t>(
    text: &'t str,
    box_size: (f64, f64),
    font_size: f64,
    multiline: bool,
    unit: f64,
    budget: &Budget,
    mut lay: impl FnMut(&'t str, (f64, f64), f64) -> std::result::Result<TextBox<'t>, LayoutError>,
) -> std::result::Result<(TextBox<'t>, f64), LayoutError> {
    budget.poll()?;
    if ![box_size.0, box_size.1, font_size]
        .iter()
        .all(|value| value.is_finite() && *value >= 0.0)
    {
        return Err(LayoutError::InvalidGeometry);
    }
    if font_size > 0.0 {
        let unbounded = (box_size.0, f64::MAX / 4.0);
        return Ok((lay(text, unbounded, font_size)?, font_size));
    }
    let min_size = (MIN_FIT_SIZE / unit).max(f64::MIN_POSITIVE);
    let mut size = box_size.1.min(MAX_FIT_SIZE / unit);
    if size < min_size {
        return Err(LayoutError::Unfit);
    }
    loop {
        let layout = lay(text, box_size, size)?;
        if layout.fits() && (multiline || layout.lines.len() <= 1) {
            return Ok((layout, size));
        }
        size *= FIT_STEP;
        if size < min_size {
            return Err(LayoutError::Unfit);
        }
    }
}

/// Draws a comb value one grapheme to a cell, from the cell that `align`
/// starts it at, at the largest size that fits every cell.
pub(super) fn comb_ops(
    out: &mut BudgetVec<'_, u8>,
    style: &WidgetStyle,
    text: &TextStyle,
    value: &str,
    max_len: usize,
    align: Align,
    mut face: Option<&mut FaceText<'_>>,
) -> Result<(), WidgetError> {
    let budget = out.budget();
    let value = standard::single_line(value, budget)?;
    let graphemes = standard::graphemes(&value, budget)?;
    if graphemes.is_empty() {
        return Ok(());
    }
    let rect = style.content_rect();
    let size = (rect[2] - rect[0], rect[3] - rect[1]);
    if size.0 <= 0.0 || size.1 <= 0.0 {
        return Ok(());
    }
    #[expect(
        clippy::cast_precision_loss,
        reason = "a comb has fewer cells than f64 counts exactly"
    )]
    let cell = size.0 / max_len as f64;
    let min_size = (MIN_FIT_SIZE / style.unit).max(f64::MIN_POSITIVE);
    let mut font_size = size.1.min(MAX_FIT_SIZE / style.unit);
    if font_size < min_size {
        return Err(malformed("comb field text cannot fit inside the widget"));
    }
    let layouts = loop {
        let mut layouts = BudgetVec::new(budget);
        layouts.try_reserve(graphemes.len())?;
        let mut fits = true;
        for grapheme in graphemes.iter() {
            let layout = match &face {
                Some(face) => face.layout(grapheme, (cell, size.1), font_size, 1.0)?,
                None => standard::form_layout(
                    grapheme,
                    (cell, size.1),
                    text.font,
                    font_size,
                    1.0,
                    budget,
                )?,
            };
            fits &= layout.fits() && layout.lines.len() == 1;
            layouts.try_push(layout)?;
        }
        if fits {
            break layouts;
        }
        font_size *= FIT_STEP;
        if font_size < min_size {
            return Err(malformed("comb field text cannot fit inside the widget"));
        }
    };
    let unused = max_len - graphemes.len();
    let start = match align {
        Align::Left => 0,
        Align::Center => unused / 2,
        Align::Right => unused,
    };
    for (index, layout) in layouts.iter().enumerate() {
        #[expect(
            clippy::cast_precision_loss,
            reason = "a comb has fewer cells than f64 counts exactly"
        )]
        let x0 = rect[0] + (start + index) as f64 * cell;
        let cell = [x0, rect[1], x0 + cell, rect[3]];
        match face.as_deref_mut() {
            Some(face) => standard::text_ops_with(
                out,
                style.height,
                cell,
                layout,
                Align::Center,
                text.style(font_size),
                |out, line| face.show(out, line, font_size),
            )?,
            None => standard::text_ops(
                out,
                style.height,
                cell,
                layout,
                Align::Center,
                text.style(font_size),
            )?,
        }
    }
    Ok(())
}

/// Returns a closed path of four Bézier curves around a circle.
#[expect(
    clippy::disallowed_methods,
    reason = "four curves of six numbers that `fmt` writes"
)]
fn circle(x: f64, y: f64, radius: f64) -> String {
    const KAPPA: f64 = 0.552_284_749_830_793_6;
    let control = radius * KAPPA;
    let points = [
        [
            x + radius,
            y + control,
            x + control,
            y + radius,
            x,
            y + radius,
        ],
        [
            x - control,
            y + radius,
            x - radius,
            y + control,
            x - radius,
            y,
        ],
        [
            x - radius,
            y - control,
            x - control,
            y - radius,
            x,
            y - radius,
        ],
        [
            x + control,
            y - radius,
            x + radius,
            y - control,
            x + radius,
            y,
        ],
    ];
    let mut ops = format!("{} {} m\n", fmt(x + radius), fmt(y));
    for curve in points {
        let numbers: Vec<String> = curve.into_iter().map(fmt).collect();
        ops.push_str(&numbers.join(" "));
        ops.push_str(" c\n");
    }
    ops.push_str("h\n");
    ops
}

/// Formats a number for content with at most six decimals, as pylopdf's
/// widget appearances do.
pub(super) fn fmt(value: f64) -> String {
    let value = if value.abs() < 1.0e-9 { 0.0 } else { value };
    let text = format!("{value:.6}");
    text.trim_end_matches('0').trim_end_matches('.').to_owned()
}
