//! The icons of notes, drawn in a 20-unit square, as pylopdf draws them.

use super::{Canvas, Paint, ellipse};

const BLACK: (f64, f64, f64) = (0.0, 0.0, 0.0);

/// Draws the icon `/Name` of a note in a square scaled to `size`: a shape
/// filled with the annotation's color and outlined in black. A name that
/// is not one of ISO 32000-2's icons draws the note.
pub(super) fn draw(canvas: &mut Canvas, paint: &Paint, icon: &[u8], size: (f64, f64)) {
    let (sx, sy) = (size.0 / 20.0, size.1 / 20.0);
    let mut shape = |operators: &str, coordinates: &[f64], filled: bool, outline: Option<f64>| {
        let scaled: Vec<f64> = coordinates
            .iter()
            .enumerate()
            .map(|(index, value)| value * if index % 2 == 0 { sx } else { sy })
            .collect();
        canvas.path(operators, &scaled);
        let fill = if filled { paint.stroke } else { None };
        match (fill, outline) {
            (Some(fill), Some(width)) => canvas.fill_stroke(fill, BLACK, width * sx.min(sy)),
            (Some(fill), None) => canvas.fill(fill),
            (None, Some(width)) => canvas.stroke(BLACK, width * sx.min(sy), 1, 1, &[]),
            (None, None) => canvas.out.push_str("n\n"),
        }
    };
    match icon {
        b"Comment" => shape(
            "mllllllh",
            &[
                2.0, 2.0, 18.0, 2.0, 18.0, 14.0, 10.0, 14.0, 5.0, 18.5, 6.0, 14.0, 2.0, 14.0,
            ],
            true,
            Some(1.0),
        ),
        b"Key" => {
            shape("mcccch", &ellipse(2.0, 6.0, 10.0, 14.0), true, Some(1.0));
            shape(
                "mlmlml",
                &[
                    10.0, 10.0, 18.5, 10.0, 15.0, 10.0, 15.0, 13.5, 17.5, 10.0, 17.5, 13.5,
                ],
                false,
                Some(1.5),
            );
        }
        b"Help" => {
            shape("mcccch", &ellipse(1.5, 1.5, 18.5, 18.5), true, Some(1.0));
            shape(
                "mcc",
                &[
                    7.0, 7.5, 7.0, 4.0, 13.0, 4.0, 13.0, 7.5, 13.0, 10.0, 10.0, 10.0, 10.0, 12.5,
                ],
                false,
                Some(1.5),
            );
            // The dot is black, not the annotation's color.
            let dot: Vec<f64> = ellipse(9.0, 14.5, 11.0, 16.5)
                .iter()
                .enumerate()
                .map(|(index, value)| value * if index % 2 == 0 { sx } else { sy })
                .collect();
            canvas.path("mcccch", &dot);
            canvas.fill(BLACK);
        }
        b"Insert" => shape("mllh", &[2.0, 18.0, 10.0, 3.0, 18.0, 18.0], true, Some(1.0)),
        b"NewParagraph" => {
            shape("mllh", &[10.0, 2.0, 4.0, 11.0, 16.0, 11.0], true, Some(1.0));
            shape("ml", &[10.0, 11.0, 10.0, 18.5], false, Some(1.5));
        }
        b"Paragraph" => {
            shape("mcccch", &ellipse(4.0, 3.0, 12.0, 11.0), true, Some(1.0));
            shape(
                "mlmlml",
                &[
                    8.0, 3.0, 15.0, 3.0, 11.0, 3.0, 11.0, 18.0, 14.0, 3.0, 14.0, 18.0,
                ],
                false,
                Some(1.5),
            );
        }
        // Note, and any other name.
        _ => {
            shape(
                "mllllh",
                &[3.0, 1.5, 13.0, 1.5, 17.0, 5.5, 17.0, 18.5, 3.0, 18.5],
                true,
                Some(1.0),
            );
            shape(
                "mlmlml",
                &[
                    6.0, 8.0, 14.0, 8.0, 6.0, 11.5, 14.0, 11.5, 6.0, 15.0, 14.0, 15.0,
                ],
                false,
                Some(1.0),
            );
        }
    }
}

/// The caret upright in a unit square, top-down: a point at the top whose
/// sides curve in to a base along the bottom.
const CARET: [f64; 14] = [
    0.0, 1.0, 0.3, 0.8, 0.45, 0.35, 0.5, 0.0, 0.55, 0.35, 0.7, 0.8, 1.0, 1.0,
];

/// Draws a caret filled with the annotation's color in a box of `size`
/// less `insets` (`/RD`: left, top, right, bottom), its point turned
/// `turn` degrees counterclockwise from the top, as `/Rotate` gives it.
pub(super) fn caret(
    canvas: &mut Canvas,
    paint: &Paint,
    size: (f64, f64),
    insets: [f64; 4],
    turn: i64,
) {
    let Some(color) = paint.stroke else {
        return;
    };
    let [left, top, right, bottom] = insets;
    let (width, height) = (size.0 - left - right, size.1 - top - bottom);
    let mut coordinates = [0.0; 14];
    for (&[x, y], [u, v]) in CARET
        .as_chunks::<2>()
        .0
        .iter()
        .zip(coordinates.as_chunks_mut::<2>().0)
    {
        // Quarter turns counterclockwise, top-down: the top goes left.
        let (x, y) = match turn.rem_euclid(360) {
            90 => (y, 1.0 - x),
            180 => (1.0 - x, 1.0 - y),
            270 => (1.0 - y, x),
            _ => (x, y),
        };
        (*u, *v) = (left + x * width, top + y * height);
    }
    canvas.path("mcch", &coordinates);
    canvas.fill(color);
}
