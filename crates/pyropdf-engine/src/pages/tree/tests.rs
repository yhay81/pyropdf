use super::*;
use crate::pages::test_document;

#[test]
fn new_page_inserts_at_the_requested_position_with_its_own_geometry() {
    let doc = test_document(&[
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 70 100] /CropBox [5 5 65 95] /Rotate 90 >>",
        "<< /Type /Page /Parent 2 0 R >>",
    ]);
    let mut transaction = doc.begin();
    let budget = Budget::default();
    let memo = Memo::new(1);
    let first = new_page(&mut transaction, &memo, 0, 50.0, 80.0, &budget).unwrap();
    let last = new_page(&mut transaction, &memo, usize::MAX, 20.0, 30.0, &budget).unwrap();
    let version = transaction.commit(&budget).unwrap();
    let pages = PageIndex::memoized(&memo, &version, &budget).unwrap();
    let [new, existing, appended] = pages.pages() else {
        panic!("two new pages surround the original");
    };
    assert_eq!(new.object(), Some(first));
    assert_eq!(existing.object(), Some(Reference::new(3, 0)));
    assert_eq!(appended.object(), Some(last));
    assert_eq!(
        new.media_box().and_then(crate::display::rect),
        Some((0.0, 0.0, 50.0, 80.0))
    );
    assert_eq!(new.crop_box(), new.media_box());
    assert_eq!(new.rotate(), Some(&Value::Integer(0)));
    assert_eq!(existing.rotate(), Some(&Value::Integer(90)));
}

#[test]
fn duplicate_keeps_shared_contents_and_rehomes_annotations_when_original_leaves() {
    let doc = test_document(&[
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 100 100] >>",
        "<< /Type /Page /Parent 2 0 R /Contents 5 0 R /Annots [6 0 R] >>",
        "<< /Type /Page /Parent 2 0 R >>",
        "<< /Length 0 >>\nstream\n\nendstream",
        "<< /Type /Annot /Subtype /Text /P 3 0 R /Rect [0 0 10 10] >>",
    ]);
    let budget = Budget::default();
    let memo = Memo::new(1);
    let mut transaction = doc.begin();
    let source = Reference::new(3, 0);
    let retained = Reference::new(4, 0);
    set_pages(
        &mut transaction,
        &memo,
        &refs::Cache::new(),
        [retained, source].into_iter(),
        &[false, true],
        &budget,
    )
    .unwrap();
    let version = transaction.commit(&budget).unwrap();
    let pages = PageIndex::memoized(&memo, &version, &budget).unwrap();
    let [first, copied] = pages.pages() else {
        panic!("one existing page and one copy");
    };
    assert_eq!(first.object(), Some(retained));
    let copy = copied.object().unwrap();
    assert_ne!(copy, source);
    assert_eq!(
        copied.dict().get("Contents"),
        Some(&Value::Reference(Reference::new(5, 0)))
    );
    assert!(version.get(5, &budget).unwrap().is_some());
    let Some(Value::Array(annots)) = copied.dict().get("Annots") else {
        panic!("copied annotations");
    };
    let [Value::Reference(annotation)] = annots.as_slice() else {
        panic!("one copied annotation");
    };
    assert_ne!(annotation.number(), 6);
    let Some(Value::Dict(annotation)) =
        resolve(&version, Some(&Value::Reference(*annotation)), &budget).unwrap()
    else {
        panic!("copied annotation is live");
    };
    assert_eq!(annotation.get("P"), Some(&Value::Reference(copy)));
    assert!(version.get(3, &budget).unwrap().is_none());
    assert!(version.get(6, &budget).unwrap().is_none());
}

#[test]
fn flattening_a_cyclic_parent_obeys_the_depth_limit_and_preserves_the_box() {
    let doc = test_document(&[
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 100 80] >>",
        "<< /Type /Page /Parent 4 0 R >>",
        "<< /Parent 5 0 R >>",
        "<< /Parent 4 0 R >>",
    ]);
    let mut transaction = doc.begin();
    let limited = Budget::default().with_limit(LimitCode::ObjectDepth, Some(32));
    let PageError::Store(error) =
        new_page(&mut transaction, &Memo::new(1), 1, 20.0, 30.0, &limited).unwrap_err()
    else {
        panic!("a cyclic parent exceeds the configured object depth");
    };
    assert_eq!(
        error.limit(),
        Some(&pyropdf_store::LimitExceeded::over(
            LimitCode::ObjectDepth,
            32,
            33
        ))
    );

    let budget = Budget::default();
    new_page(&mut transaction, &Memo::new(1), 1, 20.0, 30.0, &budget).unwrap();
    let pages = PageIndex::build(&mut Reads::new(transaction.version(), &budget)).unwrap();
    let page = &pages.pages()[0];
    assert_eq!(
        page.dict().get("Parent"),
        Some(&Value::Reference(Reference::new(2, 0)))
    );
    assert_eq!(
        page.dict().get("MediaBox").and_then(crate::display::rect),
        Some((0.0, 0.0, 100.0, 80.0))
    );
}

#[test]
fn a_failed_second_copy_restores_the_transaction_to_before_both_copies() {
    let doc = test_document(&[
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        "<< /Type /Page /Parent 2 0 R >>",
        "<< /Type /Page /Parent 2 0 R /Annots 5 0 R >>",
        "5 0 R",
    ]);
    let budget = Budget::default();
    let memo = Memo::new(1);
    let mut transaction = doc.begin();
    let blank = new_page(&mut transaction, &memo, 0, 40.0, 60.0, &budget).unwrap();
    let before = transaction.snapshot();
    let error = set_pages(
        &mut transaction,
        &memo,
        &refs::Cache::new(),
        [Reference::new(3, 0), Reference::new(4, 0)].into_iter(),
        &[true, true],
        &budget,
    )
    .unwrap_err();
    assert!(error.to_string().contains("page-copy reference cycle"));
    let after = transaction.commit(&budget).unwrap();
    assert!(before.changes(&after, &budget).unwrap().unwrap().is_empty());
    let pages = PageIndex::memoized(&memo, &after, &budget).unwrap();
    assert_eq!(pages.len(), 3);
    assert_eq!(pages.pages()[0].object(), Some(blank));
}
