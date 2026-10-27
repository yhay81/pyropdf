//! Text in the Standard 14 fonts, which every viewer has without their
//! embedding: their metrics and WinAnsi encoding, the `/DA` strings that
//! select them, the layout of a text box, and the operators that draw it
//! (DESIGN.md section 7, W3, and section 10). Form fields draw their values
//! with them.
//!
//! Ported from pylopdf's `rust/src/layout.rs`, the text functions of
//! `rust/src/draw.rs`, and the `/DA` parser of
//! `rust/src/annotation_appearance.rs` at
//! ebb01d3c4c742046e8ce7fd72957cd15a2933321. Layouts borrow their lines
//! from the text and charge their collections to the operation's budget.

mod appearance;
mod font;
mod layout;
mod ops;

pub use appearance::{DA_FONTS, DefaultAppearance, MAX_DEFAULT_APPEARANCE, da_font};
pub use font::{
    char_metric, font, font_bbox, glyph_code, is_winansi, line_metrics, text_width,
    vertical_metrics, winansi_byte,
};
pub use layout::{
    LayoutError, Line, LineMetrics, MAX_LINES, Placement, TextBox, grapheme_count, graphemes,
    layout, single_line,
};
pub use ops::{Align, DeviceColor, Style, text_ops};
pub use pdf_base14_metrics::Base14Font;

use pyropdf_store::budget::Budget;

/// The distance between the baselines of a form field's lines, in em.
const FORM_LINE_HEIGHT: f64 = 1.2;
/// The largest size at which a field without a size fits its text.
pub(crate) const MAX_FIT_SIZE: f64 = 12.0;
/// The smallest size at which a field without a size fits its text.
pub(crate) const MIN_FIT_SIZE: f64 = 0.01;
/// The factor by which each attempt to fit shrinks the size.
pub(crate) const FIT_STEP: f64 = 0.85;

/// Lays out WinAnsi text in a box with Adobe's widths for `font`, its lines
/// placed by `placement`; see [`layout`].
pub fn textbox_layout<'t>(
    text: &'t str,
    box_size: (f64, f64),
    font: Base14Font,
    font_size: f64,
    placement: Placement,
    justify: bool,
    budget: &Budget,
) -> Result<TextBox<'t>, LayoutError> {
    if !is_winansi(text) {
        return Err(LayoutError::NotWinAnsi);
    }
    layout(
        text,
        box_size,
        font_size,
        placement,
        justify,
        budget,
        |line| text_width(line, font, font_size).unwrap_or(f64::INFINITY),
    )
}

/// Lays out WinAnsi text for a form appearance: lines `line_height` em
/// apart, fitted with the AFM font bounding box ([`vertical_metrics`]).
pub fn form_layout<'t>(
    text: &'t str,
    box_size: (f64, f64),
    font: Base14Font,
    font_size: f64,
    line_height: f64,
    budget: &Budget,
) -> Result<TextBox<'t>, LayoutError> {
    let (ascent, descent) = vertical_metrics(font);
    let placement = Placement {
        ascent,
        descent,
        line_height,
    };
    textbox_layout(text, box_size, font, font_size, placement, false, budget)
}

/// Lays out the WinAnsi value of a form field in a box of `box_size`, and
/// returns the layout with its size.
///
/// A positive `font_size` wraps the text to the box's width and lets the
/// box clip what does not fit its height. A `font_size` of 0 fits the text:
/// from the box's height or 12 points, whichever is smaller, the size
/// shrinks by 0.85 until the lines fit, on one line unless `multiline`, and
/// refuses below 0.01 points. A single-line field's text has no line breaks
/// ([`single_line`]).
pub fn fit<'t>(
    text: &'t str,
    box_size: (f64, f64),
    font: Base14Font,
    font_size: f64,
    multiline: bool,
    budget: &Budget,
) -> Result<(TextBox<'t>, f64), LayoutError> {
    if font_size > 0.0 {
        let unbounded = (box_size.0, f64::MAX / 4.0);
        let layout = form_layout(text, unbounded, font, font_size, FORM_LINE_HEIGHT, budget)?;
        return Ok((layout, font_size));
    }
    let mut size = box_size.1.min(MAX_FIT_SIZE);
    loop {
        let layout = form_layout(text, box_size, font, size, FORM_LINE_HEIGHT, budget)?;
        if layout.fits() && (multiline || layout.lines.len() <= 1) {
            return Ok((layout, size));
        }
        size *= FIT_STEP;
        if size < MIN_FIT_SIZE {
            return Err(LayoutError::Unfit);
        }
    }
}

#[cfg(test)]
mod tests;
