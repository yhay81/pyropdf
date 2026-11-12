use super::super::traversal::walk;
use super::*;
use pyropdf_store::value::{Bytes, Name, PdfString, Reference};
use pyropdf_store::version::{Document, MemoryBase};

fn document(widget: &'static [u8], normal: &'static [u8]) -> Document {
    let mut base = MemoryBase::new(Dict::new());
    base.insert(Reference::new(4, 0), Bytes::new(widget));
    base.insert(Reference::new(8, 0), Bytes::new(widget));
    base.insert(Reference::new(5, 0), Bytes::new(&b"<< /N 6 0 R >>"[..]));
    base.insert(Reference::new(6, 0), Bytes::new(normal));
    base.insert(
        Reference::new(7, 0),
        Bytes::new(&b"<< /Length 3 >>\nstream\nq Q\nendstream"[..]),
    );
    Document::new(Arc::new(base), None)
}

fn version(widget: &'static [u8], normal: &'static [u8]) -> Arc<Version> {
    document(widget, normal).current()
}

#[test]
fn selected_appearance_records_indirect_dependencies() {
    let version = version(
        b"<< /Subtype /Widget /AS /On /AP 5 0 R >>",
        b"<< /On 7 0 R >>",
    );
    let budget = Budget::default();
    let mut reads = Reads::new(&version, &budget);
    assert_eq!(
        widget_appearance(&mut reads, (4, 0), AppearanceKind::State).unwrap(),
        Some(true)
    );
    assert_eq!(reads.finish().unwrap().objects(), &[4, 5, 6, 7]);
}

#[test]
fn missing_and_stale_appearances_are_recorded() {
    let version = version(b"<< /Subtype /Widget /AP 5 0 R >>", b"7 1 R");
    let budget = Budget::default();
    let mut reads = Reads::new(&version, &budget);
    assert_eq!(
        widget_appearance(&mut reads, (4, 0), AppearanceKind::Normal).unwrap(),
        Some(false)
    );
    assert_eq!(reads.finish().unwrap().objects(), &[4, 5, 6, 7]);
}

#[test]
fn shared_appearance_changes_and_rollback_refresh_only_its_readers() {
    let document = document(b"<< /Subtype /Widget /AP 5 0 R >>", b"7 0 R");
    let before = document.current();
    let budget = Budget::default();
    let mut index = AppearanceIndex::default();
    for key in [1, 2, 3] {
        index
            .record(
                key,
                IndexedWidget {
                    field: (key, 0),
                    widget: (if key == 2 { 8 } else { 4 }, 0),
                    kind: AppearanceKind::Normal,
                },
                &before,
                &budget,
            )
            .unwrap();
    }
    let saved = index.clone();
    assert_eq!(index.missing.iter().count(), 0);
    assert_eq!(
        index
            .dependencies
            .affected(&[7], false, &budget)
            .unwrap()
            .into_vec(),
        vec![1, 2, 3]
    );

    let mut transaction = document.begin();
    let savepoint = transaction.savepoint();
    transaction.write(7, &Value::Null, &budget).unwrap();
    let changed = transaction.snapshot();
    let changes = before.changes(&changed, &budget).unwrap().unwrap();
    index
        .refresh(&changed, changes.objects(), changes.trailer(), &budget)
        .unwrap();
    assert_eq!(
        index.missing.iter().map(|(key, _)| key).collect::<Vec<_>>(),
        vec![1, 2, 3]
    );
    assert_eq!(saved.missing.iter().count(), 0);

    transaction.rollback(savepoint);
    let restored = transaction.snapshot();
    let changes = changed.changes(&restored, &budget).unwrap().unwrap();
    index
        .refresh(&restored, changes.objects(), changes.trailer(), &budget)
        .unwrap();
    assert_eq!(index.missing.iter().count(), 0);
}

#[test]
fn refused_appearance_refresh_leaves_the_prior_index_unchanged() {
    let document = document(b"<< /Subtype /Widget /AP 5 0 R >>", b"7 0 R");
    let before = document.current();
    let budget = Budget::default();
    let mut index = AppearanceIndex::default();
    index
        .record(
            1,
            IndexedWidget {
                field: (1, 0),
                widget: (4, 0),
                kind: AppearanceKind::Normal,
            },
            &before,
            &budget,
        )
        .unwrap();
    let mut transaction = document.begin();
    transaction.write(7, &Value::Null, &budget).unwrap();
    let changed = transaction.snapshot();
    let limited = Budget::default().with_limit(LimitCode::MemorySize, Some(0));
    assert!(index.refresh(&changed, &[7], false, &limited).is_err());
    assert_eq!(index.missing.iter().count(), 0);
    index.refresh(&changed, &[7], false, &budget).unwrap();
    assert_eq!(index.missing.iter().count(), 1);
}

#[test]
fn appearance_index_follows_undo_and_redo() {
    let document = document(b"<< /Subtype /Widget /AP 5 0 R >>", b"7 0 R");
    let before = document.current();
    let budget = Budget::default();
    let mut index = AppearanceIndex::default();
    index
        .record(
            1,
            IndexedWidget {
                field: (1, 0),
                widget: (4, 0),
                kind: AppearanceKind::Normal,
            },
            &before,
            &budget,
        )
        .unwrap();
    let mut transaction = document.begin();
    transaction.write(7, &Value::Null, &budget).unwrap();
    let after = transaction.commit(&budget).unwrap();
    let changes = before.changes(&after, &budget).unwrap().unwrap();
    index
        .refresh(&after, changes.objects(), changes.trailer(), &budget)
        .unwrap();
    assert_eq!(index.missing.iter().count(), 1);

    let undone = document.undo().unwrap();
    let changes = after.changes(&undone, &budget).unwrap().unwrap();
    index
        .refresh(&undone, changes.objects(), changes.trailer(), &budget)
        .unwrap();
    assert_eq!(index.missing.iter().count(), 0);
    let redone = document.redo().unwrap();
    let changes = undone.changes(&redone, &budget).unwrap().unwrap();
    index
        .refresh(&redone, changes.objects(), changes.trailer(), &budget)
        .unwrap();
    assert_eq!(index.missing.iter().count(), 1);
}

#[test]
fn topology_reuses_membership_for_value_and_appearance_changes_only() {
    let mut base = MemoryBase::new(
        [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
            .into_iter()
            .collect(),
    );
    for (number, bytes) in [
        (1, &b"<< /Type /Catalog /AcroForm 4 0 R >>"[..]),
        (4, &b"<< /Fields [5 0 R] >>"[..]),
        (
            5,
            &b"<< /FT /Tx /T (field) /V (old) /Subtype /Widget /Rect [0 0 10 10] >>"[..],
        ),
    ] {
        base.insert(Reference::new(number, 0), Bytes::new(bytes));
    }
    let document = Document::new(Arc::new(base), None);
    let budget = Budget::default();
    let before = document.current();
    let topology = FormTopology::build(&before, &budget).unwrap();
    assert_eq!(topology.form.fields.len(), 1);
    assert!(topology.form.fields[0].value.is_none());

    let mut transaction = document.begin();
    let changed_value = Value::parse(
        &Bytes::new(
            &b"<< /FT /Tx /T (field) /V (new) /Subtype /Widget /Rect [1 2 3 4] /AP << >> >>"[..],
        ),
        &budget,
    )
    .unwrap();
    transaction.write(5, &changed_value, &budget).unwrap();
    let value_version = transaction.snapshot();
    assert!(
        topology
            .unchanged(&before, &value_version, &budget)
            .unwrap()
    );

    let changed_name = Value::parse(
        &Bytes::new(&b"<< /FT /Tx /T (other) /V (new) /Subtype /Widget /Rect [1 2 3 4] >>"[..]),
        &budget,
    )
    .unwrap();
    transaction.write(5, &changed_name, &budget).unwrap();
    let renamed = transaction.snapshot();
    assert!(!topology.unchanged(&before, &renamed, &budget).unwrap());
}

#[test]
fn topology_rebuilds_when_a_form_holder_changes() {
    let mut base = MemoryBase::new(
        [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
            .into_iter()
            .collect(),
    );
    base.insert(
        Reference::new(1, 0),
        Bytes::new(&b"<< /AcroForm 4 0 R >>"[..]),
    );
    base.insert(
        Reference::new(4, 0),
        Bytes::new(&b"<< /Fields [5 0 R] >>"[..]),
    );
    base.insert(
        Reference::new(5, 0),
        Bytes::new(&b"<< /FT /Tx /T (field) >>"[..]),
    );
    let document = Document::new(Arc::new(base), None);
    let budget = Budget::default();
    let before = document.current();
    let topology = FormTopology::build(&before, &budget).unwrap();
    let mut transaction = document.begin();
    transaction.write(4, &Value::Null, &budget).unwrap();
    let after = transaction.snapshot();
    assert!(!topology.unchanged(&before, &after, &budget).unwrap());
}

#[test]
fn topology_rebuilds_for_changed_kids_and_indirect_type() {
    let mut base = MemoryBase::new(
        [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
            .into_iter()
            .collect(),
    );
    for (number, bytes) in [
        (1, &b"<< /AcroForm 4 0 R >>"[..]),
        (4, &b"<< /Fields [5 0 R] >>"[..]),
        (5, &b"<< /FT 9 0 R /T (field) /Kids [6 0 R] >>"[..]),
        (6, &b"<< /Subtype /Widget >>"[..]),
        (8, &b"<< /Subtype /Widget >>"[..]),
        (9, &b"/Tx"[..]),
    ] {
        base.insert(Reference::new(number, 0), Bytes::new(bytes));
    }
    let document = Document::new(Arc::new(base), None);
    let budget = Budget::default();
    let before = document.current();
    let topology = FormTopology::build(&before, &budget).unwrap();
    let mut transaction = document.begin();
    let initial = transaction.savepoint();
    let field = Value::parse(
        &Bytes::new(&b"<< /FT 9 0 R /T (field) /Kids [8 0 R] >>"[..]),
        &budget,
    )
    .unwrap();
    transaction.write(5, &field, &budget).unwrap();
    assert!(
        !topology
            .unchanged(&before, &transaction.snapshot(), &budget)
            .unwrap()
    );
    transaction.rollback(initial);
    transaction
        .write(9, &Value::Name(Name::from("Btn")), &budget)
        .unwrap();
    assert!(
        !topology
            .unchanged(&before, &transaction.snapshot(), &budget)
            .unwrap()
    );
}

#[test]
fn fill_index_updates_appearances_and_rebuilds_changed_membership() {
    let mut base = MemoryBase::new(
        [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
            .into_iter()
            .collect(),
    );
    for (number, bytes) in [
        (1, &b"<< /AcroForm 4 0 R >>"[..]),
        (4, &b"<< /Fields [5 0 R] >>"[..]),
        (
            5,
            &b"<< /FT /Tx /T (field) /V (old) /Subtype /Widget /AP 6 0 R >>"[..],
        ),
        (6, &b"<< /N 7 0 R >>"[..]),
        (7, &b"<< /Length 3 >>\nstream\nq Q\nendstream"[..]),
    ] {
        base.insert(Reference::new(number, 0), Bytes::new(bytes));
    }
    let document = Document::new(Arc::new(base), None);
    let budget = Budget::default();
    let before = document.current();
    let first = FillIndex::build(&before, &budget).unwrap();
    assert_eq!(first.appearances.missing.iter().count(), 0);
    let mut transaction = document.begin();
    transaction.write(7, &Value::Null, &budget).unwrap();
    let changed = transaction.snapshot();
    let second = first.update(&before, &changed, &budget).unwrap();
    assert!(Arc::ptr_eq(&first.topology, &second.topology));
    assert_eq!(second.appearances.missing.iter().count(), 1);
    assert_eq!(first.appearances.missing.iter().count(), 0);

    let renamed = Value::parse(
        &Bytes::new(&b"<< /FT /Tx /T (renamed) /V (old) /Subtype /Widget /AP 6 0 R >>"[..]),
        &budget,
    )
    .unwrap();
    transaction.write(5, &renamed, &budget).unwrap();
    let after = transaction.snapshot();
    let third = second.update(&changed, &after, &budget).unwrap();
    assert!(!Arc::ptr_eq(&second.topology, &third.topology));
    assert_eq!(third.topology.form.fields[0].name, "renamed");
    assert_eq!(third.appearances.missing.iter().count(), 1);
}

#[test]
fn indexed_inherited_values_follow_indirect_and_parent_changes() {
    let mut base = MemoryBase::new(
        [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
            .into_iter()
            .collect(),
    );
    for (number, bytes) in [
        (1, &b"<< /AcroForm 4 0 R >>"[..]),
        (4, &b"<< /Fields [5 0 R] >>"[..]),
        (
            5,
            &b"<< /FT /Tx /T (parent) /V 9 0 R /Kids [6 0 R 7 0 R] >>"[..],
        ),
        (6, &b"<< /T (a) /Parent 5 0 R >>"[..]),
        (7, &b"<< /T (b) /Parent 5 0 R /V (local) >>"[..]),
        (9, &b"(inherited)"[..]),
    ] {
        base.insert(Reference::new(number, 0), Bytes::new(bytes));
    }
    let document = Document::new(Arc::new(base), None);
    let budget = Budget::default();
    let before = document.current();
    let first = FillIndex::build(&before, &budget).unwrap();
    assert_eq!(
        first.values.leaves.get(0).unwrap().as_deref(),
        Some("inherited")
    );
    assert_eq!(
        first.values.leaves.get(1).unwrap().as_deref(),
        Some("local")
    );
    let mut transaction = document.begin();
    transaction
        .write(
            9,
            &Value::String(PdfString::literal(&b"changed"[..])),
            &budget,
        )
        .unwrap();
    let changed = transaction.snapshot();
    let second = first.update(&before, &changed, &budget).unwrap();
    assert!(Arc::ptr_eq(&first.topology, &second.topology));
    assert_eq!(
        second.values.leaves.get(0).unwrap().as_deref(),
        Some("changed")
    );
    assert_eq!(
        second.values.leaves.get(1).unwrap().as_deref(),
        Some("local")
    );
    let fresh = walk(&mut Reads::new(&changed, &budget)).unwrap();
    for (at, field) in fresh.fields.iter().enumerate() {
        assert_eq!(
            second.values.leaves.get(at as u32).unwrap().as_deref(),
            field.value.as_deref()
        );
    }

    let parent = Value::parse(
        &Bytes::new(&b"<< /FT /Tx /T (parent) /V null /Kids [6 0 R 7 0 R] >>"[..]),
        &budget,
    )
    .unwrap();
    transaction.write(5, &parent, &budget).unwrap();
    let no_parent_value = transaction.snapshot();
    let third = second.update(&changed, &no_parent_value, &budget).unwrap();
    assert!(Arc::ptr_eq(&second.topology, &third.topology));
    assert_eq!(third.values.leaves.get(0).unwrap().as_deref(), None);
    assert_eq!(
        third.values.leaves.get(1).unwrap().as_deref(),
        Some("local")
    );
}

#[test]
fn indexed_values_keep_the_whole_form_value_limit() {
    let mut base = MemoryBase::new(
        [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
            .into_iter()
            .collect(),
    );
    for (number, bytes) in [
        (1, &b"<< /AcroForm 4 0 R >>"[..]),
        (4, &b"<< /Fields [5 0 R 6 0 R] >>"[..]),
        (5, &b"<< /FT /Tx /T (a) /V 9 0 R >>"[..]),
        (6, &b"<< /FT /Tx /T (b) /V 10 0 R >>"[..]),
        (9, &b"(a)"[..]),
        (10, &b"(b)"[..]),
    ] {
        base.insert(Reference::new(number, 0), Bytes::new(bytes));
    }
    let document = Document::new(Arc::new(base), None);
    let budget = Budget::default();
    let mut transaction = document.begin();
    transaction
        .write(
            9,
            &Value::String(PdfString::literal(vec![b'a'; 600_000])),
            &budget,
        )
        .unwrap();
    transaction
        .write(
            10,
            &Value::String(PdfString::literal(vec![b'b'; 100_000])),
            &budget,
        )
        .unwrap();
    let before = transaction.snapshot();
    let index = FillIndex::build(&before, &budget).unwrap();
    transaction
        .write(
            10,
            &Value::String(PdfString::literal(vec![b'b'; 600_000])),
            &budget,
        )
        .unwrap();
    let after = transaction.snapshot();
    assert!(index.update(&before, &after, &budget).is_err());
    assert!(walk(&mut Reads::new(&after, &budget)).is_err());
    assert_eq!(
        index.values.leaves.get(1).unwrap().as_ref().unwrap().len(),
        100_000
    );
}

#[test]
fn fill_cache_tracks_versions_without_reusing_stale_values() {
    let mut base = MemoryBase::new(
        [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
            .into_iter()
            .collect(),
    );
    for (number, bytes) in [
        (1, &b"<< /AcroForm 4 0 R >>"[..]),
        (4, &b"<< /Fields [5 0 R] >>"[..]),
        (5, &b"<< /FT /Tx /T (field) /V (old) >>"[..]),
    ] {
        base.insert(Reference::new(number, 0), Bytes::new(bytes));
    }
    let document = Document::new(Arc::new(base), None);
    let budget = Budget::default();
    let before = document.current();
    let cache = FillCache::new();
    let first = cache.get(&before, &budget).unwrap();
    let mut transaction = document.begin();
    let field = Value::parse(
        &Bytes::new(&b"<< /FT /Tx /T (field) /V (new) >>"[..]),
        &budget,
    )
    .unwrap();
    transaction.write(5, &field, &budget).unwrap();
    let after = transaction.commit(&budget).unwrap();
    let second = cache.get(&after, &budget).unwrap();
    assert!(Arc::ptr_eq(&first.topology, &second.topology));
    assert_eq!(first.values.leaves.get(0).unwrap().as_deref(), Some("old"));
    assert_eq!(second.values.leaves.get(0).unwrap().as_deref(), Some("new"));
    let undone = document.undo().unwrap();
    let undone = cache.get(&undone, &budget).unwrap();
    assert_eq!(undone.values.leaves.get(0).unwrap().as_deref(), Some("old"));
}

#[test]
fn deferred_changes_follow_new_dependencies_without_old_versions() {
    let mut base = MemoryBase::new(
        [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
            .into_iter()
            .collect(),
    );
    for (number, bytes) in [
        (1, &b"<< /AcroForm 4 0 R >>"[..]),
        (4, &b"<< /Fields [5 0 R] >>"[..]),
        (5, &b"<< /FT /Tx /T (field) /V 9 0 R >>"[..]),
        (9, &b"(old)"[..]),
        (10, &b"(new)"[..]),
    ] {
        base.insert(Reference::new(number, 0), Bytes::new(bytes));
    }
    let document = Document::new(Arc::new(base), Some(0));
    let budget = Budget::default();
    let before = document.current();
    let cache = FillCache::new();
    let first = cache.get(&before, &budget).unwrap();
    let mut transaction = document.begin();
    let field = Value::parse(
        &Bytes::new(&b"<< /FT /Tx /T (field) /V 10 0 R >>"[..]),
        &budget,
    )
    .unwrap();
    transaction.write(5, &field, &budget).unwrap();
    let changed = transaction.commit(&budget).unwrap();
    cache.carry_from(&cache, &before, &changed, &budget);
    let mut transaction = document.begin();
    transaction
        .write(
            10,
            &Value::String(PdfString::literal(&b"latest"[..])),
            &budget,
        )
        .unwrap();
    let latest = transaction.commit(&budget).unwrap();
    cache.carry_from(&cache, &changed, &latest, &budget);
    drop(before);
    drop(changed);
    let result = cache.get(&latest, &budget).unwrap();
    assert!(Arc::ptr_eq(&first.topology, &result.topology));
    assert_eq!(
        result.values.leaves.get(0).unwrap().as_deref(),
        Some("latest")
    );
}

#[test]
fn unrelated_edits_do_not_accumulate_in_the_fill_cache() {
    let mut base = MemoryBase::new(
        [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
            .into_iter()
            .collect(),
    );
    for (number, bytes) in [
        (1, &b"<< /AcroForm 4 0 R >>"[..]),
        (4, &b"<< /Fields [5 0 R] >>"[..]),
        (5, &b"<< /FT /Tx /T (field) /V (old) >>"[..]),
    ] {
        base.insert(Reference::new(number, 0), Bytes::new(bytes));
    }
    let document = Document::new(Arc::new(base), Some(0));
    let budget = Budget::default();
    let cache = FillCache::new();
    let original = cache.get(&document.current(), &budget).unwrap();
    for number in 1000..2000 {
        let before = document.current();
        let mut transaction = document.begin();
        transaction
            .write(number, &Value::Integer(i64::from(number)), &budget)
            .unwrap();
        let after = transaction.commit(&budget).unwrap();
        cache.carry_from(&cache, &before, &after, &budget);
    }
    let held = cache
        .entry
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .clone()
        .unwrap();
    assert!(held.pending.iter().next().is_none());
    assert!(Arc::ptr_eq(
        &original,
        &cache.get(&document.current(), &budget).unwrap()
    ));
}

#[test]
fn indexed_values_match_a_fresh_walk_for_varied_inheritance() {
    let mut base = MemoryBase::new(
        [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
            .into_iter()
            .collect(),
    );
    for (number, bytes) in [
        (1, &b"<< /AcroForm 4 0 R >>"[..]),
        (4, &b"<< /Fields [5 0 R] >>"[..]),
        (
            5,
            &b"<< /FT /Tx /T (parent) /V (inherited) /Kids [6 0 R 7 0 R] >>"[..],
        ),
        (6, &b"<< /T (a) /Parent 5 0 R >>"[..]),
        (7, &b"<< /T (b) /Parent 5 0 R /V (other) >>"[..]),
        (9, &b"(indirect)"[..]),
    ] {
        base.insert(Reference::new(number, 0), Bytes::new(bytes));
    }
    let document = Document::new(Arc::new(base), None);
    let budget = Budget::default();
    let before = document.current();
    let index = FillIndex::build(&before, &budget).unwrap();
    let mut transaction = document.begin();
    let start = transaction.savepoint();
    for value in [
        "null",
        "(direct)",
        "9 0 R",
        "[(a) (b)]",
        "123",
        "<FEFF4E2D>",
    ] {
        let bytes = format!("<< /T (a) /Parent 5 0 R /V {value} >>").into_bytes();
        let field = Value::parse(&Bytes::new(bytes), &budget).unwrap();
        transaction.write(6, &field, &budget).unwrap();
        let version = transaction.snapshot();
        let updated = index.update(&before, &version, &budget).unwrap();
        assert!(Arc::ptr_eq(&index.topology, &updated.topology));
        let fresh = walk(&mut Reads::new(&version, &budget)).unwrap();
        for (at, field) in fresh.fields.iter().enumerate() {
            assert_eq!(
                updated.values.leaves.get(at as u32).unwrap().as_deref(),
                field.value.as_deref()
            );
        }
    }
    transaction.rollback(start);
}

#[test]
fn indexed_forms_match_fresh_walks_for_varied_edits() {
    let mut base = MemoryBase::new(
        [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
            .into_iter()
            .collect(),
    );
    for (number, bytes) in [
        (1, &b"<< /AcroForm 4 0 R >>"[..]),
        (4, &b"<< /Fields [5 0 R 6 0 R] >>"[..]),
        (
            5,
            &b"<< /FT /Tx /T (a) /Ff 0 /V (old) /Kids [] /Subtype /Widget /Rect [0 0 1 1] /AP 10 0 R >>"[..],
        ),
        (6, &b"<< /FT /Tx /T (b) /V (other) >>"[..]),
        (8, &b"<< /Subtype /Widget >>"[..]),
        (9, &b"(indirect)"[..]),
        (10, &b"<< /N 11 0 R >>"[..]),
        (11, &b"<< /Length 3 >>\nstream\nq Q\nendstream"[..]),
    ] {
        base.insert(Reference::new(number, 0), Bytes::new(bytes));
    }
    let document = Document::new(Arc::new(base), None);
    let budget = Budget::default();
    let before = document.current();
    let index = FillIndex::build(&before, &budget).unwrap();
    let mut transaction = document.begin();
    let mut seed = 0x52a3_49d1_u64;
    let mut reused = 0;
    for _ in 0..2048 {
        seed = seed.wrapping_mul(6_364_136_223_846_793_005).wrapping_add(1);
        let name = if seed & 1 == 0 { "a" } else { "renamed" };
        let kind = if seed & 2 == 0 { "Tx" } else { "Btn" };
        let flags = if seed & 4 == 0 { 0 } else { 32_768 };
        let kids = if seed & 8 == 0 { "[]" } else { "[8 0 R]" };
        let subtype = if seed & 16 == 0 { "/Widget" } else { "/Link" };
        let rectangle = if seed & 32 == 0 {
            "/Rect [0 0 1 1]"
        } else {
            ""
        };
        let values = ["null", "(x)", "9 0 R", "[(a) (b)]", "123", "<FEFF4E2D>"];
        let value = values[((seed >> 8) as usize) % values.len()];
        let bytes = format!("<< /FT /{kind} /T ({name}) /Ff {flags} /V {value} /Kids {kids} /Subtype {subtype} {rectangle} /AP 10 0 R >>").into_bytes();
        let field = Value::parse(&Bytes::new(bytes), &budget).unwrap();
        let savepoint = transaction.savepoint();
        transaction.write(5, &field, &budget).unwrap();
        let after = transaction.snapshot();
        let updated = index.update(&before, &after, &budget).unwrap();
        reused += usize::from(Arc::ptr_eq(&index.topology, &updated.topology));
        let fresh = walk(&mut Reads::new(&after, &budget)).unwrap();
        assert_eq!(updated.topology.form.fields.len(), fresh.fields.len());
        for (at, (indexed, actual)) in updated
            .topology
            .form
            .fields
            .iter()
            .zip(&fresh.fields)
            .enumerate()
        {
            assert_eq!(
                (
                    &indexed.name,
                    indexed.id,
                    indexed.kind,
                    indexed.flags,
                    &indexed.widgets
                ),
                (
                    &actual.name,
                    actual.id,
                    actual.kind,
                    actual.flags,
                    &actual.widgets
                )
            );
            assert_eq!(
                updated.values.leaves.get(at as u32).unwrap().as_deref(),
                actual.value.as_deref()
            );
        }
        transaction.rollback(savepoint);
    }
    assert!(reused > 0 && reused < 2048);
}

#[test]
#[ignore = "measurement run by evidence/form-fill-cost/measure_index.py"]
fn measure_fill_index_updates() {
    use std::hint::black_box;
    use std::time::Instant;

    for count in [1_u32, 10, 100, 1_000, 4_000] {
        let mut base = MemoryBase::new(
            [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
                .into_iter()
                .collect(),
        );
        base.insert(
            Reference::new(1, 0),
            Bytes::new(&b"<< /AcroForm 4 0 R >>"[..]),
        );
        base.insert(
            Reference::new(3, 0),
            Bytes::new(&b"<< /Length 3 >>\nstream\nq Q\nendstream"[..]),
        );
        let references = (5..count + 5)
            .map(|n| format!("{n} 0 R"))
            .collect::<Vec<_>>()
            .join(" ");
        base.insert(
            Reference::new(4, 0),
            Bytes::new(format!("<< /Fields [{references}] >>").into_bytes()),
        );
        for number in 5..count + 5 {
            base.insert(Reference::new(number, 0), Bytes::new(format!("<< /FT /Tx /T (field{number}) /V (old) /Subtype /Widget /AP << /N 3 0 R >> >>").into_bytes()));
        }
        let document = Document::new(Arc::new(base), None);
        let before = document.current();
        let budget = Budget::default();
        let start = Instant::now();
        let index = FillIndex::build(&before, &budget).unwrap();
        let cold = start.elapsed().as_secs_f64() * 1000.0;
        let mut transaction = document.begin();
        let field = Value::parse(
            &Bytes::new(
                &b"<< /FT /Tx /T (field5) /V (new) /Subtype /Widget /AP << /N 3 0 R >> >>"[..],
            ),
            &budget,
        )
        .unwrap();
        transaction.write(5, &field, &budget).unwrap();
        let after = transaction.snapshot();
        let mut batches = Vec::new();
        for _ in 0..7 {
            let start = Instant::now();
            for _ in 0..100 {
                let budget = Budget::default();
                black_box(index.update(&before, &after, &budget).unwrap());
            }
            batches.push(start.elapsed().as_secs_f64() * 10.0);
        }
        batches.sort_by(f64::total_cmp);
        println!("PYRO_FILL_INDEX {count} {cold:.3} {:.4}", batches[3]);
    }
}
