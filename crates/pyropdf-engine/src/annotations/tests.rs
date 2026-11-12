use super::*;

#[test]
fn free_text_default_appearance_refuses_an_oversized_value() {
    use pyropdf_store::value::PdfString;
    use pyropdf_store::version::{Document, MemoryBase};
    let document = Document::new(Arc::new(MemoryBase::new(Dict::new())), None);
    let version = document.current();
    let budget = Budget::default();
    let reader = Reader::new(&version, &budget);
    let mut dict = Dict::new();
    dict.insert(
        Name::from("DA"),
        Value::String(PdfString::literal(vec![b' '; 4096])),
    );
    assert!(text::default_appearance(&reader, &dict).is_ok());
    dict.insert(
        Name::from("DA"),
        Value::String(PdfString::literal(vec![b' '; 4097])),
    );
    let error = text::default_appearance(&reader, &dict).unwrap_err();
    assert_eq!(
        error.limit().unwrap().code(),
        LimitCode::FormDefaultAppearanceSize
    );
}

#[test]
fn an_unknown_stamp_name_is_never_silently_truncated() {
    use pyropdf_store::version::{Document, MemoryBase};
    let document = Document::new(Arc::new(MemoryBase::new(Dict::new())), None);
    let version = document.current();
    let budget = Budget::default();
    let reader = Reader::new(&version, &budget);
    let mut dict = Dict::new();
    dict.insert(Name::from("Subtype"), Value::Name(Name::from("Stamp")));
    dict.insert(Name::from("Rect"), numbers_array(&[0.0, 0.0, 200.0, 100.0]));
    dict.insert(Name::from("Name"), Value::Name(Name::new(vec![b'A'; 256])));
    assert!(appearance(&reader, &dict).unwrap().is_some());
    dict.insert(Name::from("Name"), Value::Name(Name::new(vec![b'A'; 257])));
    let error = match appearance(&reader, &dict) {
        Err(error) => error,
        Ok(_) => panic!("a long stamp name must be refused"),
    };
    assert_eq!(error.limit().unwrap().code(), LimitCode::AnnotationTextSize);
}

#[test]
fn callout_does_not_copy_an_unknown_line_ending_name() {
    use pyropdf_store::version::{Document, MemoryBase};
    let document = Document::new(Arc::new(MemoryBase::new(Dict::new())), None);
    let version = document.current();
    let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(65_536));
    let reader = Reader::new(&version, &budget);
    let mut dict = Dict::new();
    dict.insert(Name::from("Subtype"), Value::Name(Name::from("FreeText")));
    dict.insert(Name::from("Rect"), numbers_array(&[0.0, 0.0, 200.0, 100.0]));
    dict.insert(Name::from("CL"), numbers_array(&[10.0, 10.0, 20.0, 20.0]));
    dict.insert(
        Name::from("LE"),
        Value::Name(Name::new(vec![b'A'; 1_048_576])),
    );
    assert!(appearance(&reader, &dict).unwrap().is_some());
}

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
        unit: 1.0,
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
        unit: 1.0,
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
        unit: 1.0,
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
    let reader = Reader::new(&version, &budget);
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
    let reader = Reader::new(&version, &budget);
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
    let reader = Reader::new(&version, &budget);
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
    let reader = Reader::new(&version, &budget);
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
    let reader = Reader::new(&version, &budget);
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
    let reader = Reader::new(&version, &budget);
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
    let reader = Reader::new(&version, &budget);
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
        unit: 1.0,
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

#[test]
fn page_units_resolve_from_parent_page_or_annots_list() {
    use pyropdf_store::version::{Document, MemoryBase};

    let mut trailer = Dict::new();
    trailer.insert(Name::from("Root"), Value::Reference(Reference::new(1, 0)));
    let document = Document::new(Arc::new(MemoryBase::new(trailer)), None);
    let budget = Budget::default();
    let mut tx = document.begin();
    let mut catalog = Dict::new();
    catalog.insert(Name::from("Type"), Value::Name(Name::from("Catalog")));
    catalog.insert(Name::from("Pages"), Value::Reference(Reference::new(2, 0)));
    tx.write(1, &Value::Dict(catalog), &budget).unwrap();

    let mut pages = Dict::new();
    pages.insert(Name::from("Type"), Value::Name(Name::from("Pages")));
    pages.insert(
        Name::from("Kids"),
        Value::Array(
            [
                Value::Reference(Reference::new(3, 0)),
                Value::Reference(Reference::new(4, 0)),
            ]
            .into_iter()
            .collect(),
        ),
    );
    tx.write(2, &Value::Dict(pages), &budget).unwrap();

    let mut page_one = Dict::new();
    page_one.insert(Name::from("Type"), Value::Name(Name::from("Page")));
    page_one.insert(
        Name::from("Annots"),
        Value::Array(
            [Value::Reference(Reference::new(10, 0))]
                .into_iter()
                .collect(),
        ),
    );
    tx.write(3, &Value::Dict(page_one), &budget).unwrap();

    let mut page_two = Dict::new();
    page_two.insert(Name::from("Type"), Value::Name(Name::from("Page")));
    page_two.insert(Name::from("UserUnit"), Value::from(2.5));
    page_two.insert(
        Name::from("Annots"),
        Value::Array(
            [Value::Reference(Reference::new(11, 0))]
                .into_iter()
                .collect(),
        ),
    );
    tx.write(4, &Value::Dict(page_two), &budget).unwrap();

    let version = tx.commit(&budget).unwrap();
    let mut units = PageUnits::new(&version, &budget);

    let mut with_parent = Dict::new();
    with_parent.insert(Name::from("P"), Value::Reference(Reference::new(4, 0)));
    assert_eq!(units.unit_of(None, &with_parent).unwrap(), 2.5);

    let without_parent = Dict::new();
    assert_eq!(
        units
            .unit_of(Some(Reference::new(11, 0)), &without_parent)
            .unwrap(),
        2.5
    );
    assert_eq!(
        units
            .unit_of(Some(Reference::new(10, 0)), &without_parent)
            .unwrap(),
        1.0
    );
    assert_eq!(
        units
            .unit_of(Some(Reference::new(99, 0)), &without_parent)
            .unwrap(),
        1.0
    );
}

fn test_document_with_pages(page_dicts: Vec<Dict>) -> pyropdf_store::version::Document {
    use pyropdf_store::settle::{Settlers, Structure};
    use pyropdf_store::version::{Document, MemoryBase};

    let mut trailer = Dict::new();
    trailer.insert(Name::from("Root"), Value::Reference(Reference::new(1, 0)));
    let document = Document::new(Arc::new(MemoryBase::new(trailer)), None);
    document
        .set_settlers(Settlers::default().with(Structure::Appearance, Arc::new(AppearanceSettler)));
    let budget = Budget::default();
    let mut tx = document.begin();

    let mut catalog = Dict::new();
    catalog.insert(Name::from("Type"), Value::Name(Name::from("Catalog")));
    catalog.insert(Name::from("Pages"), Value::Reference(Reference::new(2, 0)));
    tx.write(1, &Value::Dict(catalog), &budget).unwrap();

    let mut kids = Vec::new();
    for (idx, mut page) in page_dicts.into_iter().enumerate() {
        let number = 3 + u32::try_from(idx).unwrap();
        page.insert(Name::from("Type"), Value::Name(Name::from("Page")));
        page.insert(Name::from("Parent"), Value::Reference(Reference::new(2, 0)));
        if page.get("MediaBox").is_none() {
            page.insert(
                Name::from("MediaBox"),
                numbers_array(&[0.0, 0.0, 200.0, 300.0]),
            );
        }
        tx.write(number, &Value::Dict(page), &budget).unwrap();
        kids.push(Value::Reference(Reference::new(number, 0)));
    }

    let mut pages = Dict::new();
    pages.insert(Name::from("Type"), Value::Name(Name::from("Pages")));
    pages.insert(
        Name::from("Count"),
        Value::Integer(i64::try_from(kids.len()).unwrap()),
    );
    pages.insert(Name::from("Kids"), Value::Array(Array::from(kids)));
    tx.write(2, &Value::Dict(pages), &budget).unwrap();
    let _ = tx.commit(&budget).unwrap();
    document
}

fn normal_appearance_ref(version: &Version, number: u32, budget: &Budget) -> Option<Reference> {
    let object = version.get(number, budget).unwrap()?;
    let Value::Dict(dict) = &**object.value() else {
        return None;
    };
    let Value::Dict(ap) = dict.get("AP")? else {
        return None;
    };
    match ap.get("N")? {
        Value::Reference(reference) => Some(*reference),
        _ => None,
    }
}

fn popup_ref(version: &Version, number: u32, budget: &Budget) -> Option<Reference> {
    let object = version.get(number, budget).unwrap()?;
    let Value::Dict(dict) = &**object.value() else {
        return None;
    };
    match dict.get("Popup")? {
        Value::Reference(reference) => Some(*reference),
        _ => None,
    }
}

#[test]
fn crud_marks_stale_appearances_and_settles_fresh_forms_on_commit() {
    use pyropdf_store::memo::{Memo, Reads};
    use pyropdf_store::pages::PageTree;

    let document = test_document_with_pages(vec![Dict::new()]);
    let draft_pages = Memo::<PageTree, PageIndex>::new(1);
    let budget = Budget::default();

    let mut tx = document.begin();
    let spec = AnnotationSpec {
        subtype: "Square".to_owned(),
        rect: Some((10.0, 20.0, 110.0, 80.0)),
        color: Some((1.0, 0.0, 0.0)),
        interior_color: Some((0.0, 1.0, 0.0)),
        border: Some((2.0, "S".to_owned(), Vec::new())),
        clouds: Some(1.0),
        opacity: Some(0.75),
        blend_mode: Some("Multiply".to_owned()),
        contents: Some("box note".to_owned()),
        created: Some("D:20260101000000Z".to_owned()),
        ..Default::default()
    };
    let added = annotation_add(&mut tx, &draft_pages, &budget, 3, 0, &spec, false)
        .unwrap()
        .unwrap();
    assert_eq!(
        stale_appearances(&tx.snapshot(), &budget).unwrap(),
        vec![added.0]
    );
    assert!(normal_appearance_ref(&tx.snapshot(), added.0, &budget).is_none());

    let committed = tx.commit(&budget).unwrap();
    assert!(stale_appearances(&committed, &budget).unwrap().is_empty());
    let first_ap = normal_appearance_ref(&committed, added.0, &budget)
        .expect("settling creates an /AP /N form XObject");
    assert!(matches!(
        &**committed
            .get(first_ap.number(), &budget)
            .unwrap()
            .unwrap()
            .value(),
        Value::Stream(_)
    ));

    let pages = Arc::new(PageIndex::build(&mut Reads::new(&committed, &budget)).unwrap());
    let listed = annotation_list(committed.clone(), pages.clone(), &budget, 3, 0)
        .unwrap()
        .unwrap();
    assert_eq!(listed.len(), 1);
    let record = annotation_read(committed, pages, &budget, 3, 0, 0, added)
        .unwrap()
        .unwrap();
    assert_eq!(record, listed[0]);
    assert_eq!(record.subtype.as_deref(), Some("Square"));
    assert_eq!(record.texts[0].as_deref(), Some("box note"));
    assert_eq!(record.color, vec![1.0, 0.0, 0.0]);
    assert_eq!(record.interior_color, vec![0.0, 1.0, 0.0]);
    assert_eq!(record.opacity, Some(0.75));
    assert_eq!(record.blend_mode, Some("Multiply"));

    // Editing visual properties marks the appearance stale again and writes a
    // distinct form XObject rather than mutating `first_ap` in place.
    let mut tx = document.begin();
    let patch = AnnotationPatch {
        color: Some((0.0, 0.0, 1.0)),
        contents: Some("updated".to_owned()),
        author: Some("Reviewer".to_owned()),
        clear: vec!["interior_color".to_owned(), "clouds".to_owned()],
        ..Default::default()
    };
    let edited = annotation_edit(
        &mut tx,
        &draft_pages,
        &budget,
        3,
        0,
        0,
        added,
        &patch,
        false,
    )
    .unwrap()
    .unwrap();
    assert_eq!(edited, added);
    assert_eq!(
        stale_appearances(&tx.snapshot(), &budget).unwrap(),
        vec![added.0]
    );
    let committed = tx.commit(&budget).unwrap();
    let second_ap = normal_appearance_ref(&committed, added.0, &budget).unwrap();
    assert_ne!(second_ap, first_ap);

    let pages = Arc::new(PageIndex::build(&mut Reads::new(&committed, &budget)).unwrap());
    let updated = annotation_read(committed.clone(), pages, &budget, 3, 0, 0, added)
        .unwrap()
        .unwrap();
    assert_eq!(updated.color, vec![0.0, 0.0, 1.0]);
    assert!(updated.interior_color.is_empty());
    assert_eq!(updated.texts[0].as_deref(), Some("updated"));
    assert_eq!(updated.texts[2].as_deref(), Some("Reviewer"));

    // `face = true` skips Standard 14 text fitting and leaves `stale_appearances` empty.
    let mut tx = document.begin();
    let free_text = AnnotationSpec {
        subtype: "FreeText".to_owned(),
        rect: Some((10.0, 100.0, 180.0, 150.0)),
        contents: Some("日本語テキスト".to_owned()),
        font_name: Some("Helvetica".to_owned()),
        font_size: Some(12.0),
        text_color: Some((0.0, 0.0, 0.0)),
        ..Default::default()
    };
    let face_id = annotation_add(&mut tx, &draft_pages, &budget, 3, 0, &free_text, true)
        .unwrap()
        .unwrap();
    assert!(
        stale_appearances(&tx.snapshot(), &budget)
            .unwrap()
            .is_empty()
    );
    let _ = tx.commit(&budget).unwrap();

    // Single-annotation scratch isolation strips other annotations and flattens `added`.
    let mut scratch = document.begin();
    let isolated = isolate_annotation(&mut scratch, &budget, 3, 0, 0, added)
        .unwrap()
        .unwrap();
    assert_eq!(isolated.page_index, 0);
    let isolated_obj = isolated.version.get(3, &budget).unwrap().unwrap();
    let Value::Dict(isolated_page) = &**isolated_obj.value() else {
        panic!("isolated page must be a dict");
    };
    assert!(isolated_page.get("Annots").is_none());
    assert!(isolated_page.get("Contents").is_some());
    let _ = face_id;
}

#[test]
fn remove_promotes_direct_annotations_embeds_files_and_cleans_up_popups() {
    use pyropdf_store::memo::{Memo, Reads};
    use pyropdf_store::pages::PageTree;

    let mut direct_note = Dict::new();
    direct_note.insert(Name::from("Type"), Value::Name(Name::from("Annot")));
    direct_note.insert(Name::from("Subtype"), Value::Name(Name::from("Text")));
    direct_note.insert(
        Name::from("Rect"),
        numbers_array(&[10.0, 260.0, 30.0, 280.0]),
    );

    let mut direct_stamp = Dict::new();
    direct_stamp.insert(Name::from("Type"), Value::Name(Name::from("Annot")));
    direct_stamp.insert(Name::from("Subtype"), Value::Name(Name::from("Stamp")));
    direct_stamp.insert(
        Name::from("Rect"),
        numbers_array(&[40.0, 200.0, 140.0, 250.0]),
    );

    let mut page = Dict::new();
    page.insert(
        Name::from("Annots"),
        Value::Array(Array::from(vec![
            Value::Dict(direct_note),
            Value::Dict(direct_stamp),
        ])),
    );
    let document = test_document_with_pages(vec![page]);
    let draft_pages = Memo::<PageTree, PageIndex>::new(1);
    let budget = Budget::default();

    // Missing pages return None / false cleanly without mutating anything.
    let mut tx = document.begin();
    assert!(
        annotation_add(
            &mut tx,
            &draft_pages,
            &budget,
            999,
            0,
            &AnnotationSpec {
                subtype: "Text".to_owned(),
                rect: Some((0.0, 0.0, 20.0, 20.0)),
                ..Default::default()
            },
            false,
        )
        .unwrap()
        .is_none()
    );
    assert!(!annotation_remove(&mut tx, &draft_pages, &budget, 999, 0, &[(0, (0, 0))]).unwrap());

    // Embed a FileAttachment and promote direct annotations by attaching a popup to index 1.
    let file_spec = AnnotationSpec {
        subtype: "FileAttachment".to_owned(),
        rect: Some((150.0, 20.0, 170.0, 40.0)),
        icon: Some("Paperclip".to_owned()),
        data: Some(b"hello embedded payload"),
        filename: Some("hello.txt".to_owned()),
        description: Some("Sample attachment".to_owned()),
        ..Default::default()
    };
    let file_id = annotation_add(&mut tx, &draft_pages, &budget, 3, 0, &file_spec, false)
        .unwrap()
        .unwrap();
    let stamp_id = annotation_popup(
        &mut tx,
        &draft_pages,
        &budget,
        3,
        0,
        1,
        (0, 0),
        Some((50.0, 50.0, 150.0, 120.0)),
    )
    .unwrap()
    .unwrap();
    assert_ne!(stamp_id, (0, 0));
    let committed = tx.commit(&budget).unwrap();

    let pages = Arc::new(PageIndex::build(&mut Reads::new(&committed, &budget)).unwrap());
    let listed = annotation_list(committed.clone(), pages.clone(), &budget, 3, 0)
        .unwrap()
        .unwrap();
    assert_eq!(listed.len(), 3);
    assert_ne!(
        listed[0].id,
        (0, 0),
        "direct note promoted to indirect object"
    );
    assert_eq!(listed[1].id, stamp_id);
    assert_eq!(listed[1].popup.1, Some((50.0, 50.0, 150.0, 120.0)));
    let popup_id = popup_ref(&committed, stamp_id.0, &budget).expect("popup linked to stamp");
    assert_eq!(listed[2].id, file_id);

    // Removing the stamp removes both the stamp and its popup from `/Annots`.
    let mut tx = document.begin();
    assert!(annotation_remove(&mut tx, &draft_pages, &budget, 3, 0, &[(1, stamp_id)]).unwrap());
    let committed = tx.commit(&budget).unwrap();

    let pages = Arc::new(PageIndex::build(&mut Reads::new(&committed, &budget)).unwrap());
    let remaining = annotation_list(committed.clone(), pages.clone(), &budget, 3, 0)
        .unwrap()
        .unwrap();
    assert_eq!(remaining.len(), 2);
    assert!(matches!(
        annotation_read(committed.clone(), pages, &budget, 3, 0, 1, stamp_id),
        Err(AnnotationError::Stale(_))
    ));
    let page_dict = committed.get(3, &budget).unwrap().unwrap();
    let Value::Dict(page_dict) = &**page_dict.value() else {
        panic!("page must be a dictionary");
    };
    let Some(Value::Array(annots_array)) = page_dict.get("Annots") else {
        panic!("page /Annots must be an array");
    };
    assert!(
        !annots_array
            .iter()
            .any(|v| matches!(v, Value::Reference(r) if *r == popup_id))
    );
}

#[test]
fn popup_open_and_reply_wiring_preserve_invariants() {
    use pyropdf_store::memo::{Memo, Reads};
    use pyropdf_store::pages::PageTree;

    let document = test_document_with_pages(vec![Dict::new()]);
    let draft_pages = Memo::<PageTree, PageIndex>::new(1);
    let budget = Budget::default();

    let mut tx = document.begin();
    let note = annotation_add(
        &mut tx,
        &draft_pages,
        &budget,
        3,
        0,
        &AnnotationSpec {
            subtype: "Text".to_owned(),
            rect: Some((10.0, 10.0, 30.0, 30.0)),
            icon: Some("Comment".to_owned()),
            open: Some(false),
            ..Default::default()
        },
        false,
    )
    .unwrap()
    .unwrap();
    let highlight = annotation_add(
        &mut tx,
        &draft_pages,
        &budget,
        3,
        0,
        &AnnotationSpec {
            subtype: "Highlight".to_owned(),
            rects: Some(vec![(40.0, 40.0, 120.0, 56.0)]),
            color: Some((1.0, 1.0, 0.0)),
            ..Default::default()
        },
        false,
    )
    .unwrap()
    .unwrap();
    let _ = tx.commit(&budget).unwrap();

    // Note toggles `/Open` without a popup; highlight without popup refuses.
    let mut tx = document.begin();
    annotation_open(&mut tx, &draft_pages, &budget, 3, 0, 0, note, true)
        .unwrap()
        .unwrap();
    assert!(matches!(
        annotation_open(&mut tx, &draft_pages, &budget, 3, 0, 1, highlight, true),
        Err(AnnotationError::Pdf(_))
    ));

    // Add popup to highlight, open it, move it, then clear it.
    annotation_popup(
        &mut tx,
        &draft_pages,
        &budget,
        3,
        0,
        1,
        highlight,
        Some((50.0, 60.0, 150.0, 130.0)),
    )
    .unwrap()
    .unwrap();
    annotation_open(&mut tx, &draft_pages, &budget, 3, 0, 1, highlight, true)
        .unwrap()
        .unwrap();
    let committed = tx.commit(&budget).unwrap();

    let pages = Arc::new(PageIndex::build(&mut Reads::new(&committed, &budget)).unwrap());
    let rec = annotation_read(committed.clone(), pages, &budget, 3, 0, 1, highlight)
        .unwrap()
        .unwrap();
    let popup_id = popup_ref(&committed, highlight.0, &budget).expect("popup created");
    assert_eq!(rec.popup.0, None);
    assert_eq!(rec.popup.1, Some((50.0, 60.0, 150.0, 130.0)));
    assert!(rec.popup.2);

    let mut tx = document.begin();
    annotation_popup(
        &mut tx,
        &draft_pages,
        &budget,
        3,
        0,
        1,
        highlight,
        Some((60.0, 70.0, 160.0, 140.0)),
    )
    .unwrap()
    .unwrap();
    // Self-reply and off-page reply are refused.
    assert!(matches!(
        annotation_reply(
            &mut tx,
            &draft_pages,
            &budget,
            3,
            0,
            1,
            highlight,
            Some(highlight)
        ),
        Err(AnnotationError::Pdf(_))
    ));
    assert!(matches!(
        annotation_reply(
            &mut tx,
            &draft_pages,
            &budget,
            3,
            0,
            1,
            highlight,
            Some((999, 0))
        ),
        Err(AnnotationError::Pdf(_))
    ));
    // Valid reply sets /IRT without marking appearance stale.
    annotation_reply(
        &mut tx,
        &draft_pages,
        &budget,
        3,
        0,
        1,
        highlight,
        Some(note),
    )
    .unwrap()
    .unwrap();
    assert!(
        stale_appearances(&tx.snapshot(), &budget)
            .unwrap()
            .is_empty()
    );
    let committed = tx.commit(&budget).unwrap();

    let pages = Arc::new(PageIndex::build(&mut Reads::new(&committed, &budget)).unwrap());
    let moved = annotation_read(committed.clone(), pages, &budget, 3, 0, 1, highlight)
        .unwrap()
        .unwrap();
    assert_eq!(
        popup_ref(&committed, highlight.0, &budget),
        Some(popup_id),
        "moving popup reuses its object"
    );
    assert_eq!(moved.popup.0, Some(note));
    assert_eq!(moved.popup.1, Some((60.0, 70.0, 160.0, 140.0)));
    let highlight_obj = committed.get(highlight.0, &budget).unwrap().unwrap();
    let Value::Dict(highlight_dict) = &**highlight_obj.value() else {
        panic!("highlight must be a dict");
    };
    assert_eq!(
        highlight_dict.get("IRT"),
        Some(&Value::Reference(Reference::new(note.0, note.1)))
    );

    // Clearing reply removes both /IRT and /RT, and clearing popup removes /Popup.
    let mut tx = document.begin();
    let mut with_rt = highlight_dict.clone();
    with_rt.insert(Name::from("RT"), Value::Name(Name::from("Group")));
    tx.write(highlight.0, &Value::Dict(with_rt), &budget)
        .unwrap();
    annotation_reply(&mut tx, &draft_pages, &budget, 3, 0, 1, highlight, None)
        .unwrap()
        .unwrap();
    annotation_popup(&mut tx, &draft_pages, &budget, 3, 0, 1, highlight, None)
        .unwrap()
        .unwrap();
    let committed = tx.commit(&budget).unwrap();
    let cleared_obj = committed.get(highlight.0, &budget).unwrap().unwrap();
    let Value::Dict(cleared) = &**cleared_obj.value() else {
        panic!("highlight must be a dict");
    };
    assert!(cleared.get("IRT").is_none());
    assert!(cleared.get("RT").is_none());
    assert!(cleared.get("Popup").is_none());
}

#[test]
fn coordinate_conversion_respects_rotation_user_unit_and_shared_annots() {
    use pyropdf_store::memo::{Memo, Reads};
    use pyropdf_store::pages::PageTree;

    let mut page_one = Dict::new();
    page_one.insert(
        Name::from("MediaBox"),
        numbers_array(&[0.0, 0.0, 200.0, 400.0]),
    );
    page_one.insert(Name::from("Rotate"), Value::Integer(90));
    page_one.insert(Name::from("UserUnit"), Value::from(2.0));
    page_one.insert(
        Name::from("Annots"),
        Value::Reference(Reference::new(50, 0)),
    );

    let mut page_two = Dict::new();
    page_two.insert(
        Name::from("Annots"),
        Value::Reference(Reference::new(50, 0)),
    );

    let document = test_document_with_pages(vec![page_one, page_two]);
    let budget = Budget::default();
    let mut tx = document.begin();
    tx.write(50, &Value::Array(Array::new()), &budget).unwrap();
    let _ = tx.commit(&budget).unwrap();

    let draft_pages = Memo::<PageTree, PageIndex>::new(1);
    let mut tx = document.begin();
    let line = annotation_add(
        &mut tx,
        &draft_pages,
        &budget,
        3,
        0,
        &AnnotationSpec {
            subtype: "Line".to_owned(),
            points: Some(vec![vec![(40.0, 60.0), (200.0, 140.0)]]),
            line_endings: Some(("OpenArrow".to_owned(), "Circle".to_owned())),
            border: Some((4.0, "D".to_owned(), vec![6.0, 2.0])),
            color: Some((0.2, 0.4, 0.8)),
            ..Default::default()
        },
        false,
    )
    .unwrap()
    .unwrap();
    let callout = annotation_add(
        &mut tx,
        &draft_pages,
        &budget,
        3,
        0,
        &AnnotationSpec {
            subtype: "FreeText".to_owned(),
            rect: Some((300.0, 80.0, 500.0, 180.0)),
            contents: Some("Callout".to_owned()),
            font_name: Some("Helvetica".to_owned()),
            font_size: Some(12.0),
            text_color: Some((0.0, 0.0, 0.0)),
            callout: Some(CalloutPoints::three(
                (120.0, 120.0),
                (220.0, 120.0),
                (300.0, 130.0),
            )),
            callout_ending: Some("OpenArrow".to_owned()),
            ..Default::default()
        },
        false,
    )
    .unwrap()
    .unwrap();
    let committed = tx.commit(&budget).unwrap();

    let pages = Arc::new(PageIndex::build(&mut Reads::new(&committed, &budget)).unwrap());
    let geometry = annotation_geometry(committed.clone(), pages.clone(), &budget, 3, 0, 0, line)
        .unwrap()
        .unwrap();
    let pts = geometry.points.expect("line points");
    assert_eq!(pts.len(), 1);
    assert!((pts[0][0].0 - 40.0).abs() < 1e-6 && (pts[0][0].1 - 60.0).abs() < 1e-6);
    assert!((pts[0][1].0 - 200.0).abs() < 1e-6 && (pts[0][1].1 - 140.0).abs() < 1e-6);
    assert_eq!(geometry.line_endings, Some(("OpenArrow", "Circle")));

    let line_rec = annotation_read(committed.clone(), pages.clone(), &budget, 3, 0, 0, line)
        .unwrap()
        .unwrap();
    let (width, style, dashes) = line_rec.border.expect("scaled border");
    assert!((width - 4.0).abs() < 1e-6);
    assert_eq!(style, "D");
    assert_eq!(dashes, vec![6.0, 2.0]);

    let callout_rec = annotation_read(committed.clone(), pages.clone(), &budget, 3, 0, 1, callout)
        .unwrap()
        .unwrap();
    assert_eq!(callout_rec.texts[7].as_deref(), Some("FreeTextCallout"));

    // Page 2 shared object 50 0 R originally; copy-on-write kept page 2 empty!
    let page_two_list = annotation_list(committed, pages, &budget, 4, 0)
        .unwrap()
        .unwrap();
    assert!(page_two_list.is_empty());
}

#[test]
fn budget_limits_refuse_annotation_operations_and_rollback_edits() {
    use pyropdf_store::memo::{Memo, Reads};
    use pyropdf_store::pages::PageTree;

    let document = test_document_with_pages(vec![Dict::new()]);
    let draft_pages = Memo::<PageTree, PageIndex>::new(1);
    let default_budget = Budget::default();

    let mut tx = document.begin();
    let first = annotation_add(
        &mut tx,
        &draft_pages,
        &default_budget,
        3,
        0,
        &AnnotationSpec {
            subtype: "Text".to_owned(),
            rect: Some((10.0, 10.0, 30.0, 30.0)),
            contents: Some("hello world".to_owned()),
            ..Default::default()
        },
        false,
    )
    .unwrap()
    .unwrap();
    let committed = tx.commit(&default_budget).unwrap();
    let pages = Arc::new(PageIndex::build(&mut Reads::new(&committed, &default_budget)).unwrap());

    // 1. AnnotationCount on list and add.
    let zero_annots = Budget::default().with_limit(LimitCode::AnnotationCount, Some(0));
    let err = annotation_list(committed.clone(), pages.clone(), &zero_annots, 3, 0).unwrap_err();
    assert_eq!(err.limit_code(), Some(LimitCode::AnnotationCount));

    let one_annot = Budget::default().with_limit(LimitCode::AnnotationCount, Some(1));
    let mut tx = document.begin();
    let err = annotation_add(
        &mut tx,
        &draft_pages,
        &one_annot,
        3,
        0,
        &AnnotationSpec {
            subtype: "Text".to_owned(),
            rect: Some((40.0, 10.0, 60.0, 30.0)),
            ..Default::default()
        },
        false,
    )
    .unwrap_err();
    assert_eq!(err.limit_code(), Some(LimitCode::AnnotationCount));

    // 2. AnnotationTextSize on read.
    let tiny_text = Budget::default().with_limit(LimitCode::AnnotationTextSize, Some(4));
    let err =
        annotation_read(committed.clone(), pages.clone(), &tiny_text, 3, 0, 0, first).unwrap_err();
    assert_eq!(err.limit_code(), Some(LimitCode::AnnotationTextSize));

    // 3. AnnotationInputSize on edit rolls back transaction writes cleanly.
    let tiny_input = Budget::default().with_limit(LimitCode::AnnotationInputSize, Some(5));
    let err = annotation_edit(
        &mut tx,
        &draft_pages,
        &tiny_input,
        3,
        0,
        0,
        first,
        &AnnotationPatch {
            contents: Some("much longer than five bytes".to_owned()),
            ..Default::default()
        },
        false,
    )
    .unwrap_err();
    assert_eq!(err.limit_code(), Some(LimitCode::AnnotationInputSize));
    let intact = annotation_read(
        committed.clone(),
        pages.clone(),
        &default_budget,
        3,
        0,
        0,
        first,
    )
    .unwrap()
    .unwrap();
    assert_eq!(intact.texts[0].as_deref(), Some("hello world"));

    // 4. Quad, point (including squiggly zigzag), dash, and embedded-file limits.
    let one_quad = Budget::default().with_limit(LimitCode::AnnotationQuadCount, Some(1));
    let err = annotation_add(
        &mut tx,
        &draft_pages,
        &one_quad,
        3,
        0,
        &AnnotationSpec {
            subtype: "Highlight".to_owned(),
            rects: Some(vec![(10.0, 10.0, 50.0, 20.0), (10.0, 30.0, 50.0, 40.0)]),
            ..Default::default()
        },
        false,
    )
    .unwrap_err();
    assert_eq!(err.limit_code(), Some(LimitCode::AnnotationQuadCount));

    let few_points = Budget::default().with_limit(LimitCode::AnnotationPointCount, Some(10));
    let err = annotation_add(
        &mut tx,
        &draft_pages,
        &few_points,
        3,
        0,
        &AnnotationSpec {
            subtype: "Squiggly".to_owned(),
            rects: Some(vec![(10.0, 10.0, 110.0, 30.0)]),
            ..Default::default()
        },
        false,
    )
    .unwrap_err();
    assert_eq!(err.limit_code(), Some(LimitCode::AnnotationPointCount));

    let one_dash = Budget::default().with_limit(LimitCode::AnnotationDashCount, Some(1));
    let err = annotation_add(
        &mut tx,
        &draft_pages,
        &one_dash,
        3,
        0,
        &AnnotationSpec {
            subtype: "Square".to_owned(),
            rect: Some((10.0, 10.0, 50.0, 50.0)),
            border: Some((1.0, "D".to_owned(), vec![2.0, 2.0])),
            ..Default::default()
        },
        false,
    )
    .unwrap_err();
    assert_eq!(err.limit_code(), Some(LimitCode::AnnotationDashCount));

    let err = annotation_add(
        &mut tx,
        &draft_pages,
        &default_budget,
        3,
        0,
        &AnnotationSpec {
            subtype: "FileAttachment".to_owned(),
            rect: Some((10.0, 10.0, 30.0, 30.0)),
            data: Some(b"0123456789"),
            filename: Some("ten.bin".to_owned()),
            max_size: Some(4),
            ..Default::default()
        },
        false,
    )
    .unwrap_err();
    assert_eq!(err.limit_code(), Some(LimitCode::EmbeddedFileSize));

    // 5. MemorySize limit refuses deep copy via `copy_for_edit`.
    let tiny_mem = Budget::default().with_limit(LimitCode::MemorySize, Some(16));
    let err = annotation_read(committed, pages, &tiny_mem, 3, 0, 0, first).unwrap_err();
    assert_eq!(err.limit_code(), Some(LimitCode::MemorySize));
}
