use super::*;

const QUAD: [(f64, f64); 4] = [(10.0, 30.0), (110.0, 30.0), (10.0, 10.0), (110.0, 10.0)];

#[test]
fn an_underline_sits_one_point_above_the_bottom_edge() {
    let line = markup_line(b"Underline", QUAD).unwrap();
    assert_eq!(line, vec![(10.0, 11.0), (110.0, 11.0)]);
}

#[test]
fn a_strikeout_runs_through_the_middle() {
    let line = markup_line(b"StrikeOut", QUAD).unwrap();
    assert_eq!(line, vec![(10.0, 20.0), (110.0, 20.0)]);
}

#[test]
fn a_squiggly_line_zigzags_two_points_high_along_the_bottom() {
    let line = markup_line(b"Squiggly", QUAD).unwrap();
    assert_eq!(line.len(), squiggly_segments(100.0) + 1);
    assert_eq!(line.len(), zigzag_points(&[QUAD]));
    assert_eq!(line[0], (10.0, 12.5));
    assert_eq!(line[1], (12.0, 10.5));
}

#[test]
fn a_flat_quadrilateral_draws_no_line() {
    let flat = [(10.0, 10.0), (110.0, 10.0), (10.0, 10.0), (110.0, 10.0)];
    assert!(markup_line(b"Underline", flat).is_none());
}

#[test]
fn a_canvas_flips_its_y_axis_into_the_form() {
    let mut canvas = Canvas::new(20.0);
    canvas.path("mlh", &[0.0, 0.0, 10.5, 20.0]);
    canvas.stroke((1.0, 0.0, 0.0), 1.0, 0, 0, &[3.0]);
    assert_eq!(
        canvas.out,
        "0 20 m\n10.5 0 l\nh\n1 0 0 RG\n1 w 0 J 0 j [3] 0 d\nS\n"
    );
}

#[test]
fn every_note_icon_balances_its_paths() {
    let paint = Paint {
        stroke: Some((1.0, 1.0, 0.0)),
    };
    for icon in [
        &b"Comment"[..],
        b"Key",
        b"Help",
        b"Insert",
        b"NewParagraph",
        b"Paragraph",
        b"Note",
        b"Unknown",
    ] {
        let mut canvas = Canvas::new(20.0);
        icons::draw(&mut canvas, &paint, icon, (20.0, 20.0));
        let painted = canvas
            .out
            .lines()
            .filter(|line| matches!(*line, "f" | "S" | "B"))
            .count();
        let started = canvas
            .out
            .lines()
            .filter(|line| line.ends_with(" m"))
            .count();
        assert_eq!(painted.max(1), painted, "{icon:?}");
        assert!(started >= painted, "{icon:?}");
    }
}
