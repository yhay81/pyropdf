use std::sync::Arc;

use proptest::prelude::*;
use pyropdf_store::budget::{Budget, DocumentLimits, LimitCode};
use pyropdf_store::index::Index;
use pyropdf_store::memo::Memo;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::refs;
use pyropdf_store::save::FileBase;
use pyropdf_store::value::{Bytes, Value};
use pyropdf_store::version::Document;

use crate::display::{encode_text, to_display, to_pdf};
use crate::forms::text_string;

use super::*;

fn open_pdf(objects: &[&str]) -> Document {
    let mut out = b"%PDF-1.7\n".to_vec();
    let mut offsets = Vec::new();
    for (number, object) in (1..).zip(objects) {
        offsets.push(out.len());
        out.extend_from_slice(format!("{number} 0 obj\n{object}\nendobj\n").as_bytes());
    }
    let xref = out.len();
    out.extend_from_slice(
        format!("xref\n0 {}\n0000000000 65535 f \n", offsets.len() + 1).as_bytes(),
    );
    for offset in offsets {
        out.extend_from_slice(format!("{offset:010} 00000 n \n").as_bytes());
    }
    out.extend_from_slice(
        format!(
            "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n",
            objects.len() + 1
        )
        .as_bytes(),
    );
    let budget = Budget::default();
    let index = Index::open(Bytes::new(out), &budget).unwrap();
    Document::new(Arc::new(FileBase::new(index, &budget).unwrap()), None)
}

proptest! {
    #[test]
    fn display_and_pdf_coordinates_round_trip(
        cx0 in -1000.0_f64..1000.0,
        cy0 in -1000.0_f64..1000.0,
        width in 1.0_f64..2000.0,
        height in 1.0_f64..2000.0,
        turn in prop::sample::select(vec![0_i64, 90, 180, 270]),
        unit in 0.25_f64..16.0,
        x in -2000.0_f64..2000.0,
        y in -2000.0_f64..2000.0,
    ) {
        let crop = [cx0, cy0, cx0 + width, cy0 + height];
        let (dx, dy) = to_display(crop, turn, unit, x, y);
        let (rx, ry) = to_pdf(crop, turn, unit, dx, dy);
        prop_assert!((rx - x).abs() < 1e-7, "rx={rx} x={x}");
        prop_assert!((ry - y).abs() < 1e-7, "ry={ry} y={y}");

        let (ux, uy) = to_pdf(crop, turn, unit, x, y);
        let (rdx, rdy) = to_display(crop, turn, unit, ux, uy);
        prop_assert!((rdx - x).abs() < 1e-7, "rdx={rdx} x={x}");
        prop_assert!((rdy - y).abs() < 1e-7, "rdy={rdy} y={y}");
    }

    #[test]
    fn encoded_pdf_text_strings_round_trip(text in ".*") {
        let Value::String(encoded) = encode_text(&text) else {
            panic!("encode_text returns a PDF string");
        };
        if text.is_ascii()
            && text
                .bytes()
                .any(|byte| matches!(byte, 0x18..=0x1F | 0x7F))
        {
            prop_assert_eq!(encoded.as_bytes(), text.as_bytes());
        } else {
            prop_assert_eq!(text_string(encoded.as_bytes()), text);
        }
    }
}

#[test]
fn destination_name_tree_handles_kids_cycles_and_refuses_alias_cycles() {
    // Object 4 and 5 form a `/Kids` cycle; each indirect node is visited once,
    // and `/Names /Dests` takes precedence over legacy `/Dests`.
    let doc = open_pdf(&[
        "<< /Type /Catalog /Pages 2 0 R /Names << /Dests 4 0 R >> /Dests << /intro [3 0 R /Fit] /legacy [3 0 R /Fit] >> >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] >>",
        "<< /Kids [5 0 R] /Names [(intro) [3 0 R /XYZ 10 290 2]] >>",
        "<< /Kids [4 0 R] /Names [(intro) [3 0 R /Fit] (sec) [3 0 R /Fit]] >>",
    ]);
    let version = doc.current();
    let budget = Budget::default();
    let pages_memo = Memo::new(1);
    let dests_memo = Memo::new(1);
    let pages = PageIndex::memoized(&pages_memo, &version, &budget).unwrap();

    let found = named_destinations(&dests_memo, &version, &pages, &budget).unwrap();
    assert_eq!(
        found,
        vec![
            ("intro".to_owned(), Some((0, Some((10.0, 10.0)), Some(2.0)))),
            ("sec".to_owned(), Some((0, None, None))),
            ("legacy".to_owned(), Some((0, None, None))),
        ]
    );

    // An indirect reference alias cycle (`4 0 R -> 5 0 R -> 4 0 R`) refuses cleanly.
    let alias_cycle = open_pdf(&[
        "<< /Type /Catalog /Pages 2 0 R /Names << /Dests 4 0 R >> >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] >>",
        "5 0 R",
        "4 0 R",
    ]);
    let version = alias_cycle.current();
    let pages = PageIndex::memoized(&Memo::new(1), &version, &budget).unwrap();
    let err = named_destinations(&Memo::new(1), &version, &pages, &budget).unwrap_err();
    assert!(matches!(err, Error::Pdf(ref msg) if msg.contains("reference cycle")));

    // Depth limit refuses deep name trees with `NamedDestinationDepth`.
    let deep = open_pdf(&[
        "<< /Type /Catalog /Pages 2 0 R /Names << /Dests 4 0 R >> >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] >>",
        "<< /Kids [5 0 R] >>",
        "<< /Kids [6 0 R] >>",
        "<< /Names [(a) [3 0 R /Fit]] >>",
    ]);
    let tight = Budget::new(&DocumentLimits::default())
        .with_limit(LimitCode::NamedDestinationDepth, Some(2));
    let version = deep.current();
    let pages = PageIndex::memoized(&Memo::new(1), &version, &tight).unwrap();
    let err = named_destinations(&Memo::new(1), &version, &pages, &tight).unwrap_err();
    assert_eq!(err.limit_code(), Some(LimitCode::NamedDestinationDepth));
}

#[test]
fn outline_refuses_parent_and_alias_cycles_and_terminates_on_sibling_cycles() {
    // Sibling `/Next` cycle (`5 -> 6 -> 5`) visits each item once; `/Parent` cycle
    // (`6 -> 7 -> 6`) refuses `outline_item` with a parent-cycle error.
    let doc = open_pdf(&[
        "<< /Type /Catalog /Pages 2 0 R /Outlines 4 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] >>",
        "<< /Type /Outlines /First 5 0 R /Last 6 0 R /Count 2 >>",
        "<< /Title (First) /Parent 4 0 R /Next 6 0 R /Dest [3 0 R /Fit] >>",
        "<< /Title (Second) /Parent 7 0 R /Next 5 0 R >>",
        "<< /Title (CycleParent) /Parent 6 0 R >>",
    ]);
    let version = doc.current();
    let budget = Budget::default();
    let pages_memo = Memo::new(1);
    let outline_memo = Memo::new(1);
    let dests_memo = Memo::new(1);

    assert_eq!(
        outline_children(&version, &budget, None).unwrap(),
        vec![(5, 0), (6, 0)]
    );
    let entries =
        outline_entries(&outline_memo, &pages_memo, &dests_memo, &version, &budget).unwrap();
    assert_eq!(entries.len(), 2);
    assert_eq!(entries[0].2, "First");
    assert_eq!(entries[1].2, "Second");

    let pages = PageIndex::memoized(&pages_memo, &version, &budget).unwrap();
    let err = outline_item(&version, &pages, &dests_memo, &budget, (6, 0)).unwrap_err();
    assert_eq!(err, Error::Pdf("an outline parent cycle".to_owned()));

    // Indirect alias cycle in `/First` (`5 0 R -> 6 0 R -> 5 0 R`) refuses cleanly.
    let alias_cycle = open_pdf(&[
        "<< /Type /Catalog /Pages 2 0 R /Outlines 4 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] >>",
        "<< /Type /Outlines /First 5 0 R >>",
        "6 0 R",
        "5 0 R",
    ]);
    let err = outline_children(&alias_cycle.current(), &budget, None).unwrap_err();
    assert!(matches!(err, Error::Pdf(ref msg) if msg.contains("reference cycle")));

    // Outline root aliasing `/Type /Page` is refused as malformed.
    let aliased = open_pdf(&[
        "<< /Type /Catalog /Pages 2 0 R /Outlines 3 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] /First 3 0 R >>",
    ]);
    let err = outline_children(&aliased.current(), &budget, None).unwrap_err();
    assert!(matches!(err, Error::Malformed(_)));
}

#[test]
fn link_operations_round_trip_on_rotated_scaled_pages_and_rollback_on_error() {
    let doc = open_pdf(&[
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 200] /Rotate 90 /UserUnit 2 /Annots [<< /Subtype /Link /Rect [10 20 30 50] /A << /S /URI /URI (https://a.example) >> >>] >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 200] >>",
    ]);
    let budget = Budget::default();
    let draft_pages = Memo::new(1);
    let dests = Memo::new(1);

    let mut tx = doc.begin();
    let goto: Target = (
        "goto".to_owned(),
        Some(1),
        Some((12.0, 34.0)),
        Some(1.5),
        None,
        None,
    );
    let added = link_add(
        &mut tx,
        &draft_pages,
        &budget,
        3,
        0,
        (20.0, 10.0, 80.0, 50.0),
        &goto,
    )
    .unwrap()
    .unwrap();

    // Promoting the direct link at index 0 allocates a dedicated object for it.
    let new_uri: Target = (
        "uri".to_owned(),
        None,
        None,
        None,
        Some("https://b.example".to_owned()),
        None,
    );
    let promoted = link_edit(
        &mut tx,
        &draft_pages,
        &budget,
        3,
        0,
        0,
        (0, 0),
        Some((40.0, 20.0, 100.0, 60.0)),
        Some(&new_uri),
    )
    .unwrap()
    .unwrap();
    assert_ne!(promoted, (0, 0));

    // A failed edit rolls back its partial writes inside the open transaction.
    let bad_err = link_add(
        &mut tx,
        &draft_pages,
        &budget,
        3,
        0,
        (50.0, 10.0, 10.0, 50.0),
        &new_uri,
    )
    .unwrap_err();
    assert!(matches!(bad_err, Error::Pdf(_)));

    let committed = tx.commit(&budget).unwrap();
    let pages = PageIndex::memoized(&draft_pages, &committed, &budget).unwrap();
    let listed = link_list(&committed, &pages, &dests, &budget, 3, 0)
        .unwrap()
        .unwrap();
    assert_eq!(listed.len(), 2);
    assert_eq!(listed[0], (0, promoted, (40.0, 20.0, 100.0, 60.0), new_uri));
    assert_eq!(
        listed[1],
        (1, added, (20.0, 10.0, 80.0, 50.0), goto.clone())
    );
    assert_eq!(
        link_read(&committed, &pages, &dests, &budget, 3, 0, 1, added)
            .unwrap()
            .unwrap(),
        listed[1]
    );
}

#[test]
fn outline_tree_write_and_item_mutations_update_counts_and_free_old_nodes() {
    let doc = open_pdf(&[
        "<< /Type /Catalog /Pages 2 0 R /Outlines 5 0 R >>",
        "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] >>",
        "<< /Type /Outlines /First 6 0 R /Last 6 0 R /Count 1 >>",
        "<< /Title (Old) /Parent 5 0 R >>",
    ]);
    let budget = Budget::default();
    let pages_memo = Memo::new(1);
    let outline_memo = Memo::new(1);
    let dests_memo = Memo::new(1);
    let refs_cache = refs::Cache::new();

    let p0: Target = ("goto".to_owned(), Some(0), None, None, None, None);
    let p1: Target = (
        "goto".to_owned(),
        Some(1),
        Some((15.0, 25.0)),
        None,
        None,
        None,
    );
    let specs: Vec<Spec> = vec![
        (
            1,
            "Part 1".to_owned(),
            p0.clone(),
            Some((1.0, 0.0, 0.0)),
            2,
            true,
        ),
        (2, "Sec 1.1".to_owned(), p1.clone(), None, 0, false),
        (2, "Sec 1.2".to_owned(), none(), None, 1, false),
        (1, "Part 2".to_owned(), p0.clone(), None, 0, false),
    ];

    let mut tx = doc.begin();
    outline_write(&mut tx, &pages_memo, &refs_cache, &budget, specs).unwrap();
    let v1 = tx.commit(&budget).unwrap();

    // Unreferenced old outline objects 5 and 6 were freed.
    assert!(v1.get(5, &budget).unwrap().is_none());
    assert!(v1.get(6, &budget).unwrap().is_none());

    let entries = outline_entries(&outline_memo, &pages_memo, &dests_memo, &v1, &budget).unwrap();
    assert_eq!(entries.len(), 4);
    let part1_id = entries[0].0;
    let sec11_id = entries[1].0;
    assert_eq!(entries[0].6, 2); // Part 1 open with 2 children => /Count 2

    // Mutate title, target, style, and collapse Part 1 (`set_open(false)`).
    let mut tx = doc.begin();
    outline_set_title(&mut tx, &budget, part1_id, "Chapter 1").unwrap();
    outline_set_style(
        &mut tx,
        &budget,
        part1_id,
        Some((0.0, 0.5, 1.0)),
        None,
        None,
    )
    .unwrap();
    outline_set_style(&mut tx, &budget, part1_id, None, None, Some((1, true))).unwrap();
    outline_set_target(&mut tx, &pages_memo, &budget, sec11_id, &p0).unwrap();
    outline_set_open(&mut tx, &budget, part1_id, false).unwrap();

    // Leaf item refuses `outline_set_open` without corrupting prior writes in the transaction.
    let leaf_err = outline_set_open(&mut tx, &budget, sec11_id, true).unwrap_err();
    assert!(matches!(leaf_err, Error::Pdf(ref msg) if msg.contains("without children")));

    let v2 = tx.commit(&budget).unwrap();
    let pages = PageIndex::memoized(&pages_memo, &v2, &budget).unwrap();
    let (level, title, target, color, flags, count, children) =
        outline_item(&v2, &pages, &dests_memo, &budget, part1_id).unwrap();
    assert_eq!(level, 1);
    assert_eq!(title, "Chapter 1");
    assert_eq!(target, p0);
    assert_eq!(color, Some((0.0, 0.5, 1.0)));
    assert_eq!(flags, 3); // bold (2) | italic (1)
    assert_eq!(count, -2); // collapsed => negative descendant count
    assert!(children);
}
