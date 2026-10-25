//! Tests of the page index: the cases of pylopdf's tests/test_page_index.py
//! and tests/test_page_structure.py at 884af0db that read the tree, limits,
//! memoization, and agreement with hayro on the corpus and its damaged
//! inputs.

use std::sync::Arc;

use super::*;
use crate::ErrorKind;
use crate::budget::DocumentLimits;
use crate::index::Index;
use crate::index::tests::{corpus, damaged_inputs};
use crate::memo::Stats;
use crate::save::FileBase;
use crate::value::{Bytes, Name};
use crate::version::{Document, MemoryBase};

/// Returns a document whose object `n` has the value `body`.
fn document(objects: &[(u32, &str)]) -> Document {
    let trailer: Dict = [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
        .into_iter()
        .collect();
    let mut base = MemoryBase::new(trailer);
    for &(number, body) in objects {
        base.insert(
            Reference::new(number, 0),
            Bytes::new(body.as_bytes().to_vec()),
        );
    }
    Document::new(Arc::new(base), None)
}

fn index_with(document: &Document, budget: &Budget) -> Result<PageIndex> {
    let version = document.current();
    PageIndex::build(&mut Reads::new(&version, budget))
}

fn index(document: &Document) -> PageIndex {
    index_with(document, &Budget::default()).unwrap()
}

fn numbers(index: &PageIndex) -> Vec<Option<u32>> {
    index
        .pages()
        .iter()
        .map(|page| page.object().map(|r| r.number()))
        .collect()
}

fn malformed(result: Result<PageIndex>) -> String {
    match result.map(|_| ()).unwrap_err().kind() {
        ErrorKind::Malformed(message) => message.to_string(),
        other => panic!("{other:?}"),
    }
}

fn integer(value: Option<&Value>) -> Option<i64> {
    match value? {
        Value::Integer(number) => Some(*number),
        _ => None,
    }
}

/// The width of a page's media box.
fn width(page: &Page) -> Option<i64> {
    match page.media_box()? {
        Value::Array(array) => integer(array.get(2)),
        _ => None,
    }
}

/// pylopdf's `_nested_pdf`: a two-level tree whose second node gives pages
/// 2 and 3 their rotation, and whose root gives every page its resources.
fn nested() -> Document {
    document(&[
        (1, "<< /Type /Catalog /Pages 2 0 R >>"),
        (
            2,
            "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 5 /Resources << /Font << /F1 5 0 R >> >> >>",
        ),
        (
            3,
            "<< /Type /Pages /Parent 2 0 R /Kids [6 0 R 8 0 R] /Count 2 >>",
        ),
        (
            4,
            "<< /Type /Pages /Parent 2 0 R /Kids [10 0 R 12 0 R 14 0 R] /Count 3 /Rotate 90 >>",
        ),
        (5, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
        (
            6,
            "<< /Type /Page /Parent 3 0 R /MediaBox [0 0 100 200] /Rotate 0 >>",
        ),
        (
            8,
            "<< /Type /Page /Parent 3 0 R /MediaBox [0 0 110 200] /Rotate 90 >>",
        ),
        (
            10,
            "<< /Type /Page /Parent 4 0 R /MediaBox [0 0 120 200] >>",
        ),
        (
            12,
            "<< /Type /Page /Parent 4 0 R /MediaBox [0 0 130 200] >>",
        ),
        (
            14,
            "<< /Type /Page /Parent 4 0 R /MediaBox [0 0 140 200] /Rotate 0 >>",
        ),
    ])
}

#[test]
fn a_nested_tree_lists_pages_in_order_with_inherited_attributes() {
    let index = index(&nested());
    assert_eq!(
        numbers(&index),
        [Some(6), Some(8), Some(10), Some(12), Some(14)]
    );
    let widths: Vec<_> = index.pages().iter().map(width).collect();
    assert_eq!(
        widths,
        [Some(100), Some(110), Some(120), Some(130), Some(140)]
    );
    let rotations: Vec<_> = index.pages().iter().map(|p| integer(p.rotate())).collect();
    assert_eq!(rotations, [Some(0), Some(90), Some(90), Some(90), Some(0)]);
    for page in index.pages() {
        let Some(Value::Dict(resources)) = page.resources() else {
            panic!("{page:?}");
        };
        assert!(resources.get("Font").is_some());
        assert_eq!(page.inherited("Resources"), page.resources());
        assert!(page.crop_box().is_none());
    }
    for (at, number) in [6, 8, 10, 12, 14].into_iter().enumerate() {
        assert_eq!(index.index_of(Reference::new(number, 0)), Some(at));
    }
    assert_eq!(index.index_of(Reference::new(6, 1)), None);
    assert_eq!(index.index_of(Reference::new(3, 0)), None);
}

#[test]
fn a_reference_cycle_in_an_attribute_refuses() {
    let message = malformed(index_with(
        &document(&[
            (1, "<< /Type /Catalog /Pages 2 0 R >>"),
            (2, "<< /Type /Pages /Kids [3 0 R] >>"),
            (3, "<< /Type /Page /Rotate 4 0 R >>"),
            (4, "4 0 R"),
        ]),
        &Budget::default(),
    ));
    assert!(
        message.contains("Rotate contains a reference cycle"),
        "{message}"
    );
}

#[test]
fn absent_entries_inherit_through_nulls() {
    let index = index(&document(&[
        (1, "<< /Type /Catalog /Pages 2 0 R >>"),
        (
            2,
            "<< /Type /Pages /Kids [3 0 R 4 0 R 5 0 R] /Rotate 7 0 R /MediaBox [0 0 9 9] >>",
        ),
        (3, "<< /Type /Page /Rotate null /MediaBox 99 0 R >>"),
        (4, "null"),
        (5, "<< /Type /Page /Rotate 6 1 R >>"),
        (7, "180"),
    ]));
    assert_eq!(numbers(&index), [Some(3), Some(5)]);
    for page in index.pages() {
        assert_eq!(integer(page.rotate()), Some(180));
        assert_eq!(width(page), Some(9));
    }
}

#[test]
fn cycles_and_reused_nodes_refuse() {
    let cycle = document(&[
        (1, "<< /Type /Catalog /Pages 2 0 R >>"),
        (2, "<< /Type /Pages /Kids [2 0 R] /Count 1 >>"),
    ]);
    assert!(malformed(index_with(&cycle, &Budget::default())).contains("cycle"));
    let deeper = document(&[
        (1, "<< /Type /Catalog /Pages 2 0 R >>"),
        (2, "<< /Type /Pages /Kids [3 0 R] >>"),
        (3, "<< /Type /Pages /Kids [4 0 R 2 0 R] >>"),
        (4, "<< /Type /Page >>"),
    ]);
    assert!(malformed(index_with(&deeper, &Budget::default())).contains("cycle through 2"));
    let reused = document(&[
        (1, "<< /Type /Catalog /Pages 2 0 R >>"),
        (2, "<< /Type /Pages /Kids [3 0 R 3 0 R] /Count 2 >>"),
        (3, "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>"),
    ]);
    assert!(malformed(index_with(&reused, &Budget::default())).contains("reuses object 3"));
    let shared_node = document(&[
        (1, "<< /Type /Catalog /Pages 2 0 R >>"),
        (2, "<< /Type /Pages /Kids [3 0 R 3 0 R] >>"),
        (3, "<< /Type /Pages /Kids [4 0 R] >>"),
        (4, "<< /Type /Page >>"),
    ]);
    assert!(malformed(index_with(&shared_node, &Budget::default())).contains("reuses object 3"));
    let kids_cycle = document(&[
        (1, "<< /Type /Catalog /Pages 2 0 R >>"),
        (2, "<< /Type /Pages /Kids 3 0 R /Count 1 >>"),
        (3, "3 0 R"),
    ]);
    assert!(
        malformed(index_with(&kids_cycle, &Budget::default()))
            .contains("Kids contains a reference cycle")
    );
}

#[test]
fn a_missing_catalog_or_tree_refuses() {
    assert!(malformed(index_with(&document(&[]), &Budget::default())).contains("catalog"));
    let no_tree = document(&[(1, "<< /Type /Catalog >>")]);
    assert!(malformed(index_with(&no_tree, &Budget::default())).contains("page tree"));
}

#[test]
fn aliased_trees_read_as_pylopdf_reads_them() {
    let inherited = "/MediaBox [0 0 100 100] /Resources << /Font << /F1 9 0 R >> >>";
    // The catalog is also the first page.
    let catalog_page = document(&[
        (1, "<< /Type /Page /Pages 2 0 R /Parent 2 0 R >>"),
        (
            2,
            &format!("<< /Type /Pages /Kids [1 0 R 4 0 R] /Count 2 {inherited} >>"),
        ),
        (4, "<< /Type /Page /Parent 2 0 R >>"),
    ]);
    let pages = index(&catalog_page);
    assert_eq!(numbers(&pages), [Some(1), Some(4)]);
    assert!(pages.pages().iter().all(|page| width(page) == Some(100)));
    // The first page's /Annots and the second page's /Contents are /Kids.
    let kids_alias = document(&[
        (1, "<< /Type /Catalog /Pages 2 0 R >>"),
        (
            2,
            &format!("<< /Type /Pages /Kids 3 0 R /Count 2 {inherited} >>"),
        ),
        (3, "[4 0 R 6 0 R]"),
        (4, "<< /Type /Page /Parent 2 0 R /Annots 3 0 R >>"),
        (6, "<< /Type /Page /Parent 2 0 R /Contents 3 0 R >>"),
    ]);
    assert_eq!(numbers(&index(&kids_alias)), [Some(4), Some(6)]);
    // A page object that is a reference to another page's dictionary is a
    // page of its own: the reference in /Kids names it.
    let page_reference = document(&[
        (1, "<< /Type /Catalog /Pages 2 0 R >>"),
        (
            2,
            &format!("<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 {inherited} >>"),
        ),
        (3, "<< /Type /Page /Parent 2 0 R /Annots 6 0 R >>"),
        (5, "3 0 R"),
        (6, "[7 0 R]"),
    ]);
    let pages = index(&page_reference);
    assert_eq!(numbers(&pages), [Some(3), Some(5)]);
    assert_eq!(pages.get(0).unwrap().dict(), pages.get(1).unwrap().dict());
    // A /Kids entry names an absent object.
    let missing_kid = document(&[
        (1, "<< /Type /Catalog /Pages 2 0 R >>"),
        (
            2,
            &format!("<< /Type /Pages /Kids [3 0 R 10 0 R 5 0 R] /Count 2 {inherited} >>"),
        ),
        (3, "<< /Type /Page /Parent 2 0 R >>"),
        (5, "<< /Type /Page /Parent 2 0 R >>"),
    ]);
    assert_eq!(numbers(&index(&missing_kid)), [Some(3), Some(5)]);
}

#[test]
fn direct_kids_and_other_dictionaries_are_pages() {
    let index = index(&document(&[
        (1, "<< /Type /Catalog /Pages 2 0 R >>"),
        (
            2,
            "<< /Type /Pages /Kids [<< /Type /Page >> 3 0 R 4 0 R (text) 5 0 R] >>",
        ),
        (3, "<< /MediaBox [0 0 1 1] >>"),
        (4, "<< /Type /Pages >>"),
        (5, "[1 2]"),
    ]));
    assert_eq!(numbers(&index), [None, Some(3)]);
}

#[test]
fn limits_refuse_with_their_codes() {
    let nested = nested();
    let limits = DocumentLimits {
        max_pages: Some(4),
        ..DocumentLimits::default()
    };
    let refusal = index_with(&nested, &Budget::new(&limits)).unwrap_err();
    assert_eq!(refusal.limit().unwrap().code(), LimitCode::PageCount);
    let limits = DocumentLimits {
        max_object_depth: Some(1),
        ..DocumentLimits::default()
    };
    let refusal = index_with(&nested, &Budget::new(&limits)).unwrap_err();
    assert_eq!(refusal.limit().unwrap().code(), LimitCode::ObjectDepth);
    let limits = DocumentLimits {
        max_pages: Some(5),
        max_object_depth: Some(2),
        ..DocumentLimits::default()
    };
    assert_eq!(index_with(&nested, &Budget::new(&limits)).unwrap().len(), 5);
}

#[test]
fn a_deep_tree_walks_without_recursion() {
    let depth = 20_000u32;
    let mut objects: Vec<(u32, String)> = vec![(1, "<< /Type /Catalog /Pages 2 0 R >>".into())];
    for number in 2..depth {
        objects.push((
            number,
            format!("<< /Type /Pages /Kids [{} 0 R] >>", number + 1),
        ));
    }
    objects.push((depth, "<< /Type /Page >>".into()));
    let borrowed: Vec<(u32, &str)> = objects.iter().map(|(n, b)| (*n, b.as_str())).collect();
    let index = index(&document(&borrowed));
    assert_eq!(numbers(&index), [Some(depth)]);
}

#[test]
fn an_edit_outside_the_page_tree_reuses_the_memoized_index() {
    let budget = Budget::default();
    let mut objects = vec![
        (1, "<< /Type /Catalog /Pages 2 0 R /Outlines 20 0 R >>"),
        (20, "<< /Type /Outlines >>"),
    ];
    let nested_objects = [
        (2, "<< /Type /Pages /Kids [3 0 R] >>"),
        (3, "<< /Type /Page /MediaBox [0 0 100 200] >>"),
    ];
    objects.extend(nested_objects);
    let document = document(&objects);
    let memo = Memo::new(4);
    let first = PageIndex::memoized(&memo, &document.current(), &budget).unwrap();

    let mut transaction = document.begin();
    transaction.write(20, &Value::Integer(1), &budget).unwrap();
    transaction.write(21, &Value::Integer(2), &budget).unwrap();
    let edited = transaction.commit(&budget).unwrap();
    let reused = PageIndex::memoized(&memo, &edited, &budget).unwrap();
    assert!(Arc::ptr_eq(&first, &reused));
    assert_eq!(
        memo.stats(),
        Stats {
            hits: 1,
            computations: 1
        }
    );

    let mut transaction = document.begin();
    let page: Value = [(
        Name::from("MediaBox"),
        Value::Array(vec![0.into(), 0.into(), 300.into(), 200.into()].into()),
    )]
    .into_iter()
    .collect::<Dict>()
    .into();
    transaction.write(3, &page, &budget).unwrap();
    let resized = transaction.commit(&budget).unwrap();
    let rebuilt = PageIndex::memoized(&memo, &resized, &budget).unwrap();
    assert_eq!(width(rebuilt.get(0).unwrap()), Some(300));
    assert_eq!(
        memo.stats(),
        Stats {
            hits: 1,
            computations: 2
        }
    );
}

#[test]
fn failed_walks_are_never_memoized() {
    let budget = Budget::default();
    let document = document(&[
        (1, "<< /Type /Catalog /Pages 2 0 R >>"),
        (2, "<< /Type /Pages /Kids [2 0 R] >>"),
    ]);
    let memo = Memo::new(4);
    for _ in 0..2 {
        assert!(PageIndex::memoized(&memo, &document.current(), &budget).is_err());
    }
    assert_eq!(
        memo.stats(),
        Stats {
            hits: 0,
            computations: 2
        }
    );
    assert!(memo.is_empty());
}

/// Returns hayro's page list of `bytes`, as the object of each page, or
/// `None` when hayro does not open them.
fn hayro_pages(bytes: &[u8]) -> Option<Vec<Option<u32>>> {
    let pdf = hayro_syntax::Pdf::new(bytes.to_vec()).ok()?;
    Some(
        pdf.pages()
            .iter()
            .map(|page| {
                page.raw()
                    .obj_id()
                    .and_then(|id| u32::try_from(id.obj_number).ok())
            })
            .collect(),
    )
}

/// Returns the page index of `bytes`, or why it is refused.
fn our_pages(bytes: &[u8]) -> Result<Vec<Option<u32>>> {
    let budget = Budget::default();
    let index = Index::open(Bytes::new(bytes.to_vec()), &budget)?;
    let base = FileBase::new(index, &budget)?;
    let document = Document::new(Arc::new(base), None);
    Ok(numbers(&index_with(&document, &budget)?))
}

#[test]
fn corpus_indexes_equal_hayros_page_lists() {
    for (file, bytes) in corpus() {
        let ours = our_pages(&bytes).unwrap_or_else(|error| panic!("{file}: {error}"));
        assert_eq!(Some(ours), hayro_pages(&bytes), "{file}");
    }
}

#[test]
fn damaged_inputs_index_as_hayro_lists_their_pages_or_refuse() {
    let (mut equal, mut refused) = (0, 0);
    for input in damaged_inputs() {
        let Some(theirs) = hayro_pages(&input.bytes) else {
            continue;
        };
        match our_pages(&input.bytes) {
            Ok(ours) => {
                assert_eq!(ours, theirs, "{} {}", input.kind, input.file);
                equal += 1;
            }
            Err(error) => {
                assert!(
                    matches!(error.kind(), ErrorKind::Malformed(_) | ErrorKind::Limit(_)),
                    "{} {}: {error}",
                    input.kind,
                    input.file
                );
                eprintln!("refused {} {}: {error}", input.kind, input.file);
                refused += 1;
            }
        }
    }
    assert!(equal > 0, "{equal} equal, {refused} refused");
    eprintln!("damaged inputs: {equal} equal to hayro, {refused} refused");
}
