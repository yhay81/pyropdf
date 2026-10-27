//! Squares, circles, lines, polygons, polylines, and ink, with their line
//! endings and cloudy borders, as pylopdf draws them.

use super::{Canvas, KAPPA, Paint, ellipse};

/// The line-ending styles of `/LE` (ISO 32000-2 Table 179), in order.
pub const LINE_ENDINGS: [&str; 10] = [
    "None",
    "Square",
    "Circle",
    "Diamond",
    "OpenArrow",
    "ClosedArrow",
    "Butt",
    "ROpenArrow",
    "RClosedArrow",
    "Slash",
];

/// The most bumps that a cloudy border draws; a longer outline spaces them
/// further apart.
const MAX_CLOUD_BUMPS: usize = 1_024;

/// Returns the half size of a line ending drawn with stroke `width`.
#[must_use]
pub fn ending_size(width: f64) -> f64 {
    3.0 * width.max(1.0)
}

/// Returns how far the bumps of a cloudy border of `intensity`, stroked
/// `width` wide, reach beyond its outline.
#[must_use]
pub fn cloud_radius(intensity: f64, width: f64) -> f64 {
    4.0f64.mul_add(intensity, width).max(2.0)
}

/// Returns the path operators and canvas coordinates of a polyline.
pub(super) fn polyline(
    points: &[(f64, f64)],
    to_canvas: impl Fn((f64, f64)) -> (f64, f64),
) -> (String, Vec<f64>) {
    let mut operators = String::with_capacity(points.len() + 1);
    let mut coordinates = Vec::with_capacity(points.len() * 2 + 2);
    for (index, &point) in points.iter().enumerate() {
        operators.push(if index == 0 { 'm' } else { 'l' });
        let (x, y) = to_canvas(point);
        coordinates.extend([x, y]);
    }
    (operators, coordinates)
}

/// Draws a square, or the ellipse inscribed in it for a circle, inside the
/// `/RD` `insets` of a canvas of `size`, with the stroke and the bumps of a
/// cloudy border of `intensity` inside it too.
pub(super) fn boxed(
    canvas: &mut Canvas,
    paint: &Paint,
    circle: bool,
    size: (f64, f64),
    insets: [f64; 4],
    intensity: f64,
) {
    let stroke = paint.stroke(0, 0);
    let fill = paint.interior;
    if stroke.is_none() && fill.is_none() {
        return;
    }
    let [left, top, right, bottom] = insets;
    let bumps = if intensity > 0.0 {
        cloud_radius(intensity, paint.width)
    } else {
        0.0
    };
    let inset = bumps
        + if stroke.is_some() {
            paint.width / 2.0
        } else {
            0.0
        };
    let (a0, b0) = (left + inset, top + inset);
    let (a1, b1) = (size.0 - right - inset, size.1 - bottom - inset);
    if !(a1 > a0 && b1 > b0) {
        return;
    }
    let (operators, coordinates) = match (circle, bumps > 0.0) {
        (true, true) => cloud(&ellipse_vertices(a0, b0, a1, b1, bumps), bumps),
        (true, false) => ("mcccch".to_owned(), ellipse(a0, b0, a1, b1).to_vec()),
        (false, true) => cloud(&[(a0, b0), (a1, b0), (a1, b1), (a0, b1)], bumps),
        (false, false) => ("mlllh".to_owned(), vec![a0, b0, a1, b0, a1, b1, a0, b1]),
    };
    canvas.path(&operators, &coordinates);
    canvas.draw(fill, stroke.as_ref());
}

/// Draws a line, polygon, polyline, or ink annotation of `subtype` through
/// `lists` of user-space points, with a polygon's cloudy border of
/// `intensity` and a line's `endings`. Returns `false` for a line,
/// polygon, or polyline of fewer than two points, which draws nothing.
pub(super) fn lines(
    canvas: &mut Canvas,
    paint: &Paint,
    subtype: &[u8],
    lists: &[Vec<(f64, f64)>],
    intensity: f64,
    endings: [usize; 2],
    to_canvas: impl Fn((f64, f64)) -> (f64, f64) + Copy,
) -> bool {
    if subtype == b"Ink" {
        let Some(stroke) = paint.stroke(1, 1) else {
            return true;
        };
        for line in lists.iter().filter(|line| !line.is_empty()) {
            let (mut operators, mut coordinates) = polyline(line, to_canvas);
            if line.len() == 1 {
                // A single point draws a dot with the round cap.
                operators.push('l');
                coordinates.extend_from_within(..2);
            }
            canvas.path(&operators, &coordinates);
            canvas.draw(None, Some(&stroke));
        }
        return true;
    }
    let Some(vertices) = lists.first().filter(|vertices| vertices.len() >= 2) else {
        return false;
    };
    if subtype == b"Polygon" {
        let stroke = paint.stroke(0, 1);
        if stroke.is_none() && paint.interior.is_none() {
            return true;
        }
        let (operators, coordinates) = if intensity > 0.0 && vertices.len() >= 3 {
            let points: Vec<(f64, f64)> = vertices.iter().copied().map(to_canvas).collect();
            cloud(&points, cloud_radius(intensity, paint.width))
        } else {
            let (mut operators, coordinates) = polyline(vertices, to_canvas);
            operators.push('h');
            (operators, coordinates)
        };
        canvas.path(&operators, &coordinates);
        canvas.draw(paint.interior, stroke.as_ref());
        return true;
    }
    // A line or polyline, and its two endings.
    if let Some(stroke) = paint.stroke(0, 1) {
        let (operators, coordinates) = polyline(vertices, to_canvas);
        canvas.path(&operators, &coordinates);
        canvas.draw(None, Some(&stroke));
    }
    let last = vertices.len() - 1;
    for (ending, tip, from) in [
        (endings[0], vertices[0], vertices[1]),
        (endings[1], vertices[last], vertices[last - 1]),
    ] {
        draw_ending(canvas, paint, ending, tip, from, to_canvas);
    }
    true
}

/// Draws the line ending `ending` at `tip` of the segment that comes from
/// `from`, in user space.
fn draw_ending(
    canvas: &mut Canvas,
    paint: &Paint,
    ending: usize,
    tip: (f64, f64),
    from: (f64, f64),
    to_canvas: impl Fn((f64, f64)) -> (f64, f64),
) {
    let (dx, dy) = (tip.0 - from.0, tip.1 - from.1);
    let length = dx.hypot(dy);
    if ending == 0 || !(length.is_finite() && length > 0.0) {
        return;
    }
    let size = ending_size(paint.width);
    // u points out of the line at the tip, n across it.
    let u = (dx / length, dy / length);
    let n = (-u.1, u.0);
    let at = |along: f64, across: f64| {
        to_canvas((
            n.0.mul_add(across, u.0.mul_add(along, tip.0)),
            n.1.mul_add(across, u.1.mul_add(along, tip.1)),
        ))
    };
    let stroke = paint.solid_stroke();
    let back = size * 3f64.sqrt();
    let (closed, points): (bool, Vec<(f64, f64)>) = match LINE_ENDINGS[ending] {
        "Square" => (
            true,
            vec![
                at(size, size),
                at(size, -size),
                at(-size, -size),
                at(-size, size),
            ],
        ),
        "Diamond" => (
            true,
            vec![at(size, 0.0), at(0.0, -size), at(-size, 0.0), at(0.0, size)],
        ),
        "OpenArrow" => (false, vec![at(-back, size), at(0.0, 0.0), at(-back, -size)]),
        "ClosedArrow" => (true, vec![at(-back, size), at(0.0, 0.0), at(-back, -size)]),
        "ROpenArrow" => (false, vec![at(back, size), at(0.0, 0.0), at(back, -size)]),
        "RClosedArrow" => (true, vec![at(back, size), at(0.0, 0.0), at(back, -size)]),
        "Butt" => (false, vec![at(0.0, size), at(0.0, -size)]),
        "Slash" => {
            // 30 degrees clockwise from the perpendicular.
            let (along, across) = (size * 0.5, size * 3f64.sqrt() / 2.0);
            (false, vec![at(along, -across), at(-along, across)])
        }
        // Circle.
        _ => {
            if paint.interior.is_some() || stroke.is_some() {
                let (cx, cy) = at(0.0, 0.0);
                canvas.path(
                    "mcccch",
                    &ellipse(cx - size, cy - size, cx + size, cy + size),
                );
                canvas.draw(paint.interior, stroke.as_ref());
            }
            return;
        }
    };
    let fill = if closed { paint.interior } else { None };
    if fill.is_none() && stroke.is_none() {
        return;
    }
    let (mut operators, coordinates) = polyline(&points, |point| point);
    if closed {
        operators.push('h');
    }
    canvas.path(&operators, &coordinates);
    canvas.draw(fill, stroke.as_ref());
}

/// Returns a closed outline through canvas `vertices` drawn as a cloud:
/// each edge split into chords about twice `radius` long, each chord a
/// semicircle bulging outward.
fn cloud(vertices: &[(f64, f64)], radius: f64) -> (String, Vec<f64>) {
    let n = vertices.len();
    let edge = |i: usize| (vertices[i], vertices[(i + 1) % n]);
    let length = |((x0, y0), (x1, y1)): ((f64, f64), (f64, f64))| (x1 - x0).hypot(y1 - y0);
    let perimeter: f64 = (0..n).map(|i| length(edge(i))).sum();
    #[expect(
        clippy::cast_precision_loss,
        reason = "the bump limit is small and exact"
    )]
    let spacing = (2.0 * radius).max(perimeter / MAX_CLOUD_BUMPS as f64);
    // The shoelace sign tells which side of each edge is outside.
    let area: f64 = (0..n)
        .map(|i| {
            let ((x0, y0), (x1, y1)) = edge(i);
            x0.mul_add(y1, -(x1 * y0))
        })
        .sum();
    let side = if area >= 0.0 { 1.0 } else { -1.0 };
    let mut operators = String::from("m");
    let mut coordinates = vec![vertices[0].0, vertices[0].1];
    for i in 0..n {
        let ((x0, y0), (x1, y1)) = edge(i);
        let total = length(edge(i));
        if total <= 0.0 {
            continue;
        }
        let (tx, ty) = ((x1 - x0) / total, (y1 - y0) / total);
        let (nx, ny) = (side * ty, -side * tx);
        // Rounding up keeps every chord, and so every bump, within `radius`;
        // the spacing keeps the bumps of one outline within the limit.
        #[expect(
            clippy::cast_possible_truncation,
            clippy::cast_sign_loss,
            reason = "the count is at least one and at most the bump limit per edge"
        )]
        let bumps = (total / spacing).ceil().max(1.0) as usize;
        #[expect(clippy::cast_precision_loss, reason = "the count is small")]
        let chord = total / bumps as f64;
        let (r, k) = (chord / 2.0, chord / 2.0 * KAPPA);
        for j in 0..bumps {
            #[expect(clippy::cast_precision_loss, reason = "the index is small")]
            let along = chord * j as f64;
            let (px, py) = (tx.mul_add(along, x0), ty.mul_add(along, y0));
            let (qx, qy) = (tx.mul_add(chord, px), ty.mul_add(chord, py));
            let (ax, ay) = (
                nx.mul_add(r, (px + qx) / 2.0),
                ny.mul_add(r, (py + qy) / 2.0),
            );
            operators.push_str("cc");
            coordinates.extend_from_slice(&[
                nx.mul_add(k, px),
                ny.mul_add(k, py),
                (-tx).mul_add(k, ax),
                (-ty).mul_add(k, ay),
                ax,
                ay,
                tx.mul_add(k, ax),
                ty.mul_add(k, ay),
                nx.mul_add(k, qx),
                ny.mul_add(k, qy),
                qx,
                qy,
            ]);
        }
    }
    operators.push('h');
    (operators, coordinates)
}

/// Returns the vertices of an ellipse inside a box, spaced about twice
/// `radius` apart along its outline, so that every bump of its cloud is
/// alike.
fn ellipse_vertices(x0: f64, y0: f64, x1: f64, y1: f64, radius: f64) -> Vec<(f64, f64)> {
    const STEPS: usize = 1_440;
    let (cx, cy) = ((x0 + x1) / 2.0, (y0 + y1) / 2.0);
    let (rx, ry) = ((x1 - x0) / 2.0, (y1 - y0) / 2.0);
    #[expect(clippy::cast_precision_loss, reason = "the steps are few")]
    let angle = |step: f64| std::f64::consts::TAU * step / STEPS as f64;
    let at = |angle: f64| (rx.mul_add(angle.cos(), cx), ry.mul_add(angle.sin(), cy));
    let mut lengths = Vec::with_capacity(STEPS + 1);
    lengths.push(0.0);
    for step in 1..=STEPS {
        #[expect(clippy::cast_precision_loss, reason = "the steps are few")]
        let ((ax, ay), (bx, by)) = (at(angle((step - 1) as f64)), at(angle(step as f64)));
        lengths.push(lengths[step - 1] + (bx - ax).hypot(by - ay));
    }
    let perimeter = lengths[STEPS];
    // A little closer than twice the radius, so that no chord needs two bumps.
    #[expect(
        clippy::cast_possible_truncation,
        clippy::cast_sign_loss,
        reason = "the count is clamped to the bump limit"
    )]
    let count = ((perimeter / (1.9 * radius)).ceil() as usize).clamp(6, MAX_CLOUD_BUMPS);
    let mut vertices = Vec::with_capacity(count);
    let mut step = 0;
    for i in 0..count {
        #[expect(clippy::cast_precision_loss, reason = "the count is small")]
        let target = perimeter * i as f64 / count as f64;
        while step < STEPS - 1 && lengths[step + 1] < target {
            step += 1;
        }
        let span = lengths[step + 1] - lengths[step];
        let fraction = if span > 0.0 {
            ((target - lengths[step]) / span).clamp(0.0, 1.0)
        } else {
            0.0
        };
        #[expect(clippy::cast_precision_loss, reason = "the steps are few")]
        vertices.push(at(angle(step as f64 + fraction)));
    }
    vertices
}
