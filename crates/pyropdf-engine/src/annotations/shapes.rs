//! Squares, circles, lines, polygons, polylines, and ink, with their line
//! endings and cloudy borders, as pylopdf draws them.

use super::{Canvas, KAPPA, Paint, ellipse};
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::{Error, Result};

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

/// The target tessellation density; each edge still draws at least one
/// bump. Actual generated points are checked against the operation budget.
const TARGET_CLOUD_BUMPS: usize = 1_024;

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

/// Emits a polyline directly without an input-sized intermediate path.
pub(super) fn polyline(
    canvas: &mut Canvas,
    points: &[(f64, f64)],
    to_canvas: impl Fn((f64, f64)) -> (f64, f64),
) {
    for (index, &point) in points.iter().enumerate() {
        if canvas.out.refusal.is_some() {
            return;
        }
        let (x, y) = to_canvas(point);
        canvas.path(if index == 0 { "m" } else { "l" }, &[x, y]);
    }
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
) -> Result<()> {
    let stroke = paint.stroke(0, 0);
    let fill = paint.interior;
    if stroke.is_none() && fill.is_none() {
        return Ok(());
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
        return Ok(());
    }
    let budget = canvas.out.bytes.budget();
    match (circle, bumps > 0.0) {
        (true, true) => cloud(
            canvas,
            &ellipse_vertices(a0, b0, a1, b1, bumps, budget)?,
            bumps,
        )?,
        (true, false) => canvas.path("mcccch", &ellipse(a0, b0, a1, b1)),
        (false, true) => cloud(canvas, &[(a0, b0), (a1, b0), (a1, b1), (a0, b1)], bumps)?,
        (false, false) => canvas.path("mlllh", &[a0, b0, a1, b0, a1, b1, a0, b1]),
    }
    canvas.draw(fill, stroke.as_ref());
    Ok(())
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
) -> Result<bool> {
    if subtype == b"Ink" {
        let Some(stroke) = paint.stroke(1, 1) else {
            return Ok(true);
        };
        for line in lists.iter().filter(|line| !line.is_empty()) {
            canvas.out.bytes.budget().poll()?;
            polyline(canvas, line, to_canvas);
            if line.len() == 1 {
                // A single point draws a dot with the round cap.
                let (x, y) = to_canvas(line[0]);
                canvas.path("l", &[x, y]);
            }
            canvas.draw(None, Some(&stroke));
        }
        return Ok(true);
    }
    let Some(vertices) = lists.first().filter(|vertices| vertices.len() >= 2) else {
        return Ok(false);
    };
    if subtype == b"Polygon" {
        let stroke = paint.stroke(0, 1);
        if stroke.is_none() && paint.interior.is_none() {
            return Ok(true);
        }
        if intensity > 0.0 && vertices.len() >= 3 {
            let mut points = BudgetVec::new(canvas.out.bytes.budget());
            points.try_extend(vertices.iter().copied().map(to_canvas))?;
            cloud(canvas, &points, cloud_radius(intensity, paint.width))?;
        } else {
            polyline(canvas, vertices, to_canvas);
            canvas.path("h", &[]);
        }
        canvas.draw(paint.interior, stroke.as_ref());
        return Ok(true);
    }
    // A line or polyline, and its two endings.
    if let Some(stroke) = paint.stroke(0, 1) {
        polyline(canvas, vertices, to_canvas);
        canvas.draw(None, Some(&stroke));
    }
    let last = vertices.len() - 1;
    for (ending, tip, from) in [
        (endings[0], vertices[0], vertices[1]),
        (endings[1], vertices[last], vertices[last - 1]),
    ] {
        draw_ending(canvas, paint, ending, tip, from, to_canvas)?;
    }
    Ok(true)
}

/// Draws the line ending `ending` at `tip` of the segment that comes from
/// `from`, in user space.
pub(super) fn draw_ending(
    canvas: &mut Canvas,
    paint: &Paint,
    ending: usize,
    tip: (f64, f64),
    from: (f64, f64),
    to_canvas: impl Fn((f64, f64)) -> (f64, f64),
) -> Result<()> {
    let (dx, dy) = (tip.0 - from.0, tip.1 - from.1);
    let length = dx.hypot(dy);
    if ending == 0 {
        return Ok(());
    }
    if !length.is_finite() {
        return Err(Error::malformed("an annotation line length is nonfinite"));
    }
    if length <= 0.0 {
        return Ok(());
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
            return Ok(());
        }
    };
    let fill = if closed { paint.interior } else { None };
    if fill.is_none() && stroke.is_none() {
        return Ok(());
    }
    polyline(canvas, &points, |point| point);
    if closed {
        canvas.path("h", &[]);
    }
    canvas.draw(fill, stroke.as_ref());
    Ok(())
}

/// Returns a closed outline through canvas `vertices` drawn as a cloud:
/// each edge split into chords about twice `radius` long, each chord a
/// semicircle bulging outward.
fn cloud(canvas: &mut Canvas, vertices: &[(f64, f64)], radius: f64) -> Result<()> {
    let budget = canvas.out.bytes.budget();
    if vertices.is_empty() {
        return Ok(());
    }
    let n = vertices.len();
    let edge = |i: usize| (vertices[i], vertices[(i + 1) % n]);
    let length = |((x0, y0), (x1, y1)): ((f64, f64), (f64, f64))| (x1 - x0).hypot(y1 - y0);
    let mut perimeter = 0.0;
    for i in 0..n {
        budget.poll()?;
        perimeter += length(edge(i));
    }
    if !(perimeter.is_finite() && radius.is_finite()) {
        return Err(Error::malformed(
            "a cloudy outline has nonfinite dimensions",
        ));
    }
    #[expect(
        clippy::cast_precision_loss,
        reason = "the bump limit is small and exact"
    )]
    let spacing = (2.0 * radius).max(perimeter / TARGET_CLOUD_BUMPS as f64);
    // The shoelace sign tells which side of each edge is outside.
    let scale = vertices
        .iter()
        .flat_map(|&(x, y)| [x.abs(), y.abs()])
        .fold(1.0f64, f64::max);
    let area: f64 = (0..n)
        .map(|i| {
            let ((x0, y0), (x1, y1)) = edge(i);
            (x0 / scale).mul_add(y1 / scale, -((x1 / scale) * (y0 / scale)))
        })
        .sum();
    let side = if area >= 0.0 { 1.0 } else { -1.0 };
    canvas.path("m", &[vertices[0].0, vertices[0].1]);
    let mut generated_points = 1usize;
    for i in 0..n {
        budget.poll()?;
        let ((x0, y0), (x1, y1)) = edge(i);
        let total = length(edge(i));
        if total <= 0.0 {
            continue;
        }
        let (tx, ty) = ((x1 - x0) / total, (y1 - y0) / total);
        let (nx, ny) = (side * ty, -side * tx);
        // Each edge remains complete. Check the actual generated point
        // count because rounding per edge can exceed the target density.
        #[expect(
            clippy::cast_possible_truncation,
            clippy::cast_sign_loss,
            reason = "the count is at least one and at most the bump limit per edge"
        )]
        let bumps = (total / spacing).ceil().max(1.0) as usize;
        generated_points = generated_points.saturating_add(bumps.saturating_mul(6));
        budget.check(LimitCode::AnnotationPointCount, generated_points as u64)?;
        #[expect(clippy::cast_precision_loss, reason = "the count is small")]
        let chord = total / bumps as f64;
        let (r, k) = (chord / 2.0, chord / 2.0 * KAPPA);
        for j in 0..bumps {
            #[expect(clippy::cast_precision_loss, reason = "the index is small")]
            let along = chord * j as f64;
            let (px, py) = (tx.mul_add(along, x0), ty.mul_add(along, y0));
            let (qx, qy) = (tx.mul_add(chord, px), ty.mul_add(chord, py));
            let (ax, ay) = (
                nx.mul_add(r, px / 2.0 + qx / 2.0),
                ny.mul_add(r, py / 2.0 + qy / 2.0),
            );
            canvas.path(
                "cc",
                &[
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
                ],
            );
        }
    }
    canvas.path("h", &[]);
    Ok(())
}

/// Returns the vertices of an ellipse inside a box, spaced about twice
/// `radius` apart along its outline, so that every bump of its cloud is
/// alike.
fn ellipse_vertices(
    x0: f64,
    y0: f64,
    x1: f64,
    y1: f64,
    radius: f64,
    budget: &Budget,
) -> Result<BudgetVec<'_, (f64, f64)>> {
    const STEPS: usize = 1_440;
    let (cx, cy) = (x0 / 2.0 + x1 / 2.0, y0 / 2.0 + y1 / 2.0);
    let (rx, ry) = ((x1 - x0) / 2.0, (y1 - y0) / 2.0);
    #[expect(clippy::cast_precision_loss, reason = "the steps are few")]
    let angle = |step: f64| std::f64::consts::TAU * step / STEPS as f64;
    let at = |angle: f64| (rx.mul_add(angle.cos(), cx), ry.mul_add(angle.sin(), cy));
    let mut lengths = BudgetVec::new(budget);
    lengths.try_reserve_exact(STEPS + 1)?;
    lengths.try_push(0.0)?;
    for step in 1..=STEPS {
        #[expect(clippy::cast_precision_loss, reason = "the steps are few")]
        let ((ax, ay), (bx, by)) = (at(angle((step - 1) as f64)), at(angle(step as f64)));
        budget.poll()?;
        lengths.try_push(lengths[step - 1] + (bx - ax).hypot(by - ay))?;
    }
    let perimeter = lengths[STEPS];
    if !perimeter.is_finite() {
        return Err(Error::malformed("a cloudy ellipse has nonfinite perimeter"));
    }
    // A little closer than twice the radius, so that no chord needs two bumps.
    #[expect(
        clippy::cast_possible_truncation,
        clippy::cast_sign_loss,
        reason = "the count is clamped to the bump limit"
    )]
    let count = ((perimeter / (1.9 * radius)).ceil() as usize).clamp(6, TARGET_CLOUD_BUMPS);
    let mut vertices = BudgetVec::new(budget);
    vertices.try_reserve_exact(count)?;
    let mut step = 0;
    for i in 0..count {
        #[expect(clippy::cast_precision_loss, reason = "the count is small")]
        let target = (perimeter / count as f64) * i as f64;
        while step < STEPS - 1 && lengths[step + 1] < target {
            step += 1;
        }
        let span = lengths[step + 1] - lengths[step];
        let fraction = if span > 0.0 {
            ((target - lengths[step]) / span).clamp(0.0, 1.0)
        } else {
            0.0
        };
        budget.poll()?;
        vertices.try_push(at(angle(step as f64 + fraction)))?;
    }
    Ok(vertices)
}
