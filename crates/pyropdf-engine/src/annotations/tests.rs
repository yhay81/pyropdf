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
