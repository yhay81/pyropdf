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
        interior: None,
        width: 1.0,
        dashes: Vec::new(),
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

#[test]
fn cloud_bumps_stay_inside_the_box_of_a_cloudy_square() {
    let paint = Paint {
        stroke: Some((1.0, 0.0, 0.0)),
        interior: Some((1.0, 1.0, 0.0)),
        width: 1.0,
        dashes: Vec::new(),
    };
    let mut canvas = Canvas::new(60.0);
    shapes::boxed(&mut canvas, &paint, false, (100.0, 60.0), [0.0; 4], 2.0);
    let numbers: Vec<f64> = canvas
        .out
        .lines()
        .filter(|line| line.ends_with(" c") || line.ends_with(" m"))
        .flat_map(|line| line.split(' ').filter_map(|word| word.parse::<f64>().ok()))
        .collect();
    assert!(numbers.len() > 40);
    let (xs, ys): (Vec<f64>, Vec<f64>) = numbers
        .as_chunks::<2>()
        .0
        .iter()
        .map(|&[x, y]| (x, y))
        .unzip();
    // The stroke's half width lies between the bumps and the box.
    assert!(xs.iter().all(|x| (0.5..=99.5).contains(x)));
    assert!(ys.iter().all(|y| (0.5..=59.5).contains(y)));
    assert!(canvas.out.ends_with("B\n"));
}

#[test]
fn line_endings_fill_only_closed_shapes() {
    let paint = Paint {
        stroke: Some((0.0, 0.0, 1.0)),
        interior: Some((1.0, 1.0, 0.0)),
        width: 2.0,
        dashes: vec![4.0, 2.0],
    };
    for (ending, closed) in [(1, true), (4, false), (5, true), (6, false), (9, false)] {
        let mut canvas = Canvas::new(100.0);
        assert!(shapes::lines(
            &mut canvas,
            &paint,
            b"Line",
            &[vec![(10.0, 50.0), (90.0, 50.0)]],
            0.0,
            [0, ending],
            |point| point,
        ));
        let painted: Vec<&str> = canvas
            .out
            .lines()
            .filter(|line| matches!(*line, "S" | "B" | "f"))
            .collect();
        // The dashed line, then its ending, which never dashes.
        assert_eq!(painted, ["S", if closed { "B" } else { "S" }], "{ending}");
        assert!(canvas.out.ends_with("[] 0 d\nB\n") || canvas.out.ends_with("[] 0 d\nS\n"));
    }
    let mut canvas = Canvas::new(100.0);
    assert!(!shapes::lines(
        &mut canvas,
        &paint,
        b"PolyLine",
        &[vec![(10.0, 50.0)]],
        0.0,
        [0, 0],
        |point| point,
    ));
}

#[test]
fn ending_sizes_and_cloud_radii_follow_the_width() {
    assert!((ending_size(0.5) - 3.0).abs() < f64::EPSILON);
    assert!((ending_size(2.0) - 6.0).abs() < f64::EPSILON);
    assert!((cloud_radius(0.1, 0.0) - 2.0).abs() < f64::EPSILON);
    assert!((cloud_radius(2.0, 1.0) - 9.0).abs() < f64::EPSILON);
}
