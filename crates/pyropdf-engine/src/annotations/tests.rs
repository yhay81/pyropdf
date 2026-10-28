use super::*;

const QUAD: [(f64, f64); 4] = [(10.0, 30.0), (110.0, 30.0), (10.0, 10.0), (110.0, 10.0)];

#[test]
fn an_underline_sits_one_point_above_the_bottom_edge() {
    let budget = Budget::default();
    let line = markup_line(b"Underline", QUAD, &budget).unwrap().unwrap();
    assert_eq!(line.as_slice(), vec![(10.0, 11.0), (110.0, 11.0)]);
}

#[test]
fn a_strikeout_runs_through_the_middle() {
    let budget = Budget::default();
    let line = markup_line(b"StrikeOut", QUAD, &budget).unwrap().unwrap();
    assert_eq!(line.as_slice(), vec![(10.0, 20.0), (110.0, 20.0)]);
}

#[test]
fn a_squiggly_line_zigzags_two_points_high_along_the_bottom() {
    let budget = Budget::default();
    let line = markup_line(b"Squiggly", QUAD, &budget).unwrap().unwrap();
    assert_eq!(line.len(), squiggly_segments(100.0) + 1);
    assert_eq!(line.len(), zigzag_points(&[QUAD]));
    assert_eq!(line[0], (10.0, 12.5));
    assert_eq!(line[1], (12.0, 10.5));
}

#[test]
fn a_flat_quadrilateral_draws_no_line() {
    let budget = Budget::default();
    let flat = [(10.0, 10.0), (110.0, 10.0), (10.0, 10.0), (110.0, 10.0)];
    assert!(markup_line(b"Underline", flat, &budget).unwrap().is_none());
}

#[test]
fn a_canvas_flips_its_y_axis_into_the_form() {
    let budget = Budget::default();
    let mut canvas = Canvas::new(20.0, &budget);
    canvas.path("mlh", &[0.0, 0.0, 10.5, 20.0]);
    canvas.stroke((1.0, 0.0, 0.0), 1.0, 0, 0, &[3.0]);
    assert_eq!(
        String::from_utf8(canvas.out.finish().unwrap()).unwrap(),
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
        let budget = Budget::default();
        let mut canvas = Canvas::new(20.0, &budget);
        icons::draw(&mut canvas, &paint, icon, (20.0, 20.0));
        let output = String::from_utf8(canvas.out.finish().unwrap()).unwrap();
        let painted = output
            .lines()
            .filter(|line| matches!(*line, "f" | "S" | "B"))
            .count();
        let started = output.lines().filter(|line| line.ends_with(" m")).count();
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
    let budget = Budget::default();
    let mut canvas = Canvas::new(60.0, &budget);
    shapes::boxed(&mut canvas, &paint, false, (100.0, 60.0), [0.0; 4], 2.0).unwrap();
    let output = String::from_utf8(canvas.out.finish().unwrap()).unwrap();
    let numbers: Vec<f64> = output
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
    assert!(output.ends_with("B\n"));
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
        let budget = Budget::default();
        let mut canvas = Canvas::new(100.0, &budget);
        assert!(
            shapes::lines(
                &mut canvas,
                &paint,
                b"Line",
                &[vec![(10.0, 50.0), (90.0, 50.0)]],
                0.0,
                [0, ending],
                |point| point,
            )
            .unwrap()
        );
        let output = String::from_utf8(canvas.out.finish().unwrap()).unwrap();
        let painted: Vec<&str> = output
            .lines()
            .filter(|line| matches!(*line, "S" | "B" | "f"))
            .collect();
        // The dashed line, then its ending, which never dashes.
        assert_eq!(painted, ["S", if closed { "B" } else { "S" }], "{ending}");
        assert!(output.ends_with("[] 0 d\nB\n") || output.ends_with("[] 0 d\nS\n"));
    }
    let budget = Budget::default();
    let mut canvas = Canvas::new(100.0, &budget);
    assert!(
        !shapes::lines(
            &mut canvas,
            &paint,
            b"PolyLine",
            &[vec![(10.0, 50.0)]],
            0.0,
            [0, 0],
            |point| point,
        )
        .unwrap()
    );
}

#[test]
fn ending_sizes_and_cloud_radii_follow_the_width() {
    assert!((ending_size(0.5) - 3.0).abs() < f64::EPSILON);
    assert!((ending_size(2.0) - 6.0).abs() < f64::EPSILON);
    assert!((cloud_radius(0.1, 0.0) - 2.0).abs() < f64::EPSILON);
    assert!((cloud_radius(2.0, 1.0) - 9.0).abs() < f64::EPSILON);
}

fn marked_dict(subtype: &str, quads: usize, width: f64) -> Dict {
    let points = [0.0, 10.0, width, 10.0, 0.0, 0.0, width, 0.0];
    [
        (Name::from("Subtype"), Value::Name(Name::from(subtype))),
        (Name::from("Rect"), numbers_array(&[0.0, 0.0, width, 10.0])),
        (Name::from("C"), numbers_array(&[1.0, 1.0, 0.0])),
        (
            Name::from("QuadPoints"),
            Value::Array((0..quads).flat_map(|_| points.map(Value::from)).collect()),
        ),
    ]
    .into_iter()
    .collect()
}

#[test]
fn excessive_existing_markup_refuses_instead_of_silently_drawing_nothing() {
    use pyropdf_store::version::{Document, MemoryBase};
    let document = Document::new(Arc::new(MemoryBase::new(Dict::new())), None);
    let version = document.current();
    let budget = Budget::default();
    let reader = Reader {
        version: &version,
        budget: &budget,
    };
    for dict in [
        marked_dict("Highlight", MAX_QUADS + 1, 10.0),
        marked_dict("Squiggly", 1, 131_072.0),
    ] {
        assert!(appearance(&reader, &dict).is_err());
    }
}

#[test]
fn appearance_output_refuses_before_exceeding_its_memory_budget() {
    use pyropdf_store::version::{Document, MemoryBase};
    let document = Document::new(Arc::new(MemoryBase::new(Dict::new())), None);
    let version = document.current();
    let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(0));
    let reader = Reader {
        version: &version,
        budget: &budget,
    };
    assert!(appearance(&reader, &marked_dict("Highlight", 1, 10.0)).is_err());
}

#[test]
fn a_long_appearance_reference_chain_refuses_instead_of_becoming_null() {
    use pyropdf_store::value::Reference;
    use pyropdf_store::version::{Document, MemoryBase};
    let document = Document::new(Arc::new(MemoryBase::new(Dict::new())), None);
    let budget = Budget::default();
    let mut transaction = document.begin();
    for number in 1..=20 {
        transaction
            .write(
                number,
                &Value::Reference(Reference::new(number + 1, 0)),
                &budget,
            )
            .unwrap();
    }
    let version = transaction.commit(&budget).unwrap();
    let reader = Reader {
        version: &version,
        budget: &budget,
    };
    assert!(
        reader
            .resolve(&Value::Reference(Reference::new(1, 0)))
            .is_err()
    );
}

#[test]
fn a_narrow_appearance_path_keeps_its_width() {
    let budget = Budget::default();
    let mut canvas = Canvas::new(1.0, &budget);
    canvas.path("ml", &[0.0, 0.0, 0.000001, 1.0]);
    assert!(
        String::from_utf8(canvas.out.finish().unwrap())
            .unwrap()
            .contains("0.000001 0 l")
    );
}

#[test]
fn content_growth_refuses_with_the_specific_limit() {
    for code in [LimitCode::MemorySize, LimitCode::AnnotationAppearanceSize] {
        let budget = Budget::default().with_limit(code, Some(32));
        let mut canvas = Canvas::new(10.0, &budget);
        canvas.path("ml", &[0.0, 0.0, 1.0, 1.0]);
        canvas.stroke((1.0, 0.0, 0.0), 1.0, 0, 0, &[]);
        assert_eq!(
            canvas.out.finish().unwrap_err().limit().unwrap().code(),
            code
        );
    }
}

#[test]
fn overflowing_geometry_is_refused_without_generating_a_path() {
    use pyropdf_store::version::{Document, MemoryBase};
    let document = Document::new(Arc::new(MemoryBase::new(Dict::new())), None);
    let version = document.current();
    let budget = Budget::default();
    let reader = Reader {
        version: &version,
        budget: &budget,
    };
    let quad = [
        (-f64::MAX, 10.0),
        (f64::MAX, 10.0),
        (-f64::MAX, 0.0),
        (f64::MAX, 0.0),
    ];
    assert_eq!(zigzag_points(&[quad]), usize::MAX);
    let mut dict = marked_dict("Highlight", 1, 10.0);
    dict.insert(
        Name::from("QuadPoints"),
        Value::Array(
            quad.into_iter()
                .flat_map(|(x, y)| [Value::from(x), Value::from(y)])
                .collect(),
        ),
    );
    assert!(appearance(&reader, &dict).is_err());
}

#[test]
fn ink_point_count_counts_points_not_stroke_headers() {
    use pyropdf_store::version::{Document, MemoryBase};
    let document = Document::new(Arc::new(MemoryBase::new(Dict::new())), None);
    let version = document.current();
    let budget = Budget::default();
    let reader = Reader {
        version: &version,
        budget: &budget,
    };
    let mut dict = Dict::new();
    dict.insert(
        Name::from("InkList"),
        Value::Array(
            [numbers_array(&[0.0, 0.0]), numbers_array(&[1.0, 1.0])]
                .into_iter()
                .collect(),
        ),
    );
    assert_eq!(reader.point_count(&dict, b"Ink").unwrap(), 2);
}

#[test]
fn excessive_point_arrays_and_dashes_refuse_instead_of_becoming_absent() {
    use pyropdf_store::version::{Document, MemoryBase};
    let document = Document::new(Arc::new(MemoryBase::new(Dict::new())), None);
    let version = document.current();
    let budget = Budget::default();
    let reader = Reader {
        version: &version,
        budget: &budget,
    };
    let mut dict = Dict::new();
    dict.insert(
        Name::from("Vertices"),
        Value::Array(
            (0..2 * (MAX_ANNOTATION_POINTS + 1))
                .map(|_| Value::Integer(0))
                .collect(),
        ),
    );
    assert!(reader.point_lists(&dict, b"PolyLine").is_err());
    let mut style = Dict::new();
    style.insert(Name::from("S"), Value::Name(Name::from("D")));
    style.insert(
        Name::from("D"),
        numbers_array(&[1.0; MAX_BORDER_DASHES + 1]),
    );
    dict.insert(Name::from("BS"), Value::Dict(style));
    assert!(reader.border(&dict).is_err());
}

#[test]
fn returned_point_lists_retain_their_memory_charge() {
    use pyropdf_store::version::{Document, MemoryBase};
    let document = Document::new(Arc::new(MemoryBase::new(Dict::new())), None);
    let version = document.current();
    let budget = Budget::default();
    let reader = Reader {
        version: &version,
        budget: &budget,
    };
    let mut dict = Dict::new();
    dict.insert(Name::from("Vertices"), numbers_array(&[0.0, 0.0, 1.0, 1.0]));
    let points = reader.point_lists(&dict, b"PolyLine").unwrap().unwrap();
    assert_eq!(points[0].len(), 2);
    assert!(budget.used(LimitCode::MemorySize) >= 2 * size_of::<(f64, f64)>() as u64);
}

#[test]
fn cloudy_polygons_count_all_generated_control_points() {
    let paint = Paint {
        stroke: Some((1.0, 0.0, 0.0)),
        interior: None,
        width: 1.0,
        dashes: Vec::new(),
    };
    let budget = Budget::default();
    let mut canvas = Canvas::new(10.0, &budget);
    let vertices: Vec<_> = (0..11_000)
        .map(|index| (f64::from(index % 2), 0.0))
        .collect();
    let error = shapes::lines(
        &mut canvas,
        &paint,
        b"Polygon",
        &[vertices],
        1.0,
        [0, 0],
        |point| point,
    )
    .unwrap_err();
    assert_eq!(
        error.limit().unwrap().code(),
        LimitCode::AnnotationPointCount
    );
}
