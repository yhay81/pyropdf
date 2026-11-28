//! The bridge renders what hayro renders from the original file, and after
//! edits what it renders from a full save of the same version.

use std::fs;
use std::path::Path;
use std::sync::Arc;

use proptest::prelude::*;
use pyropdf_store::index::Index;
use pyropdf_store::memo::Reads;
use pyropdf_store::save::{self, FileBase};
use pyropdf_store::value::{Array, Bytes, Stream};
use pyropdf_store::version::{Document, MemoryBase};

use super::*;
use crate::render::{self, Rgba};

/// The scale of the test renders: small, so that the corpus renders fast.
const SCALE: f32 = 0.5;

fn open(bytes: Vec<u8>) -> Document {
    let budget = Budget::default();
    let index = Index::open(Bytes::new(bytes), &budget).unwrap();
    Document::new(Arc::new(FileBase::new(index, &budget).unwrap()), None)
}

fn page_index(version: &Version) -> PageIndex {
    let budget = Budget::default();
    PageIndex::build(&mut Reads::new(version, &budget)).unwrap()
}

fn bridge(version: &Version) -> Bridged {
    Bridged::new(version, &page_index(version), &Budget::default()).unwrap()
}

/// Renders every page of hayro's own reading of `bytes`.
fn hayro_renders(bytes: Vec<u8>) -> Vec<Rgba> {
    let bridged = Bridged::of_bytes(bytes);
    (0..bridged.pdf().pages().len())
        .map(|index| render::page(&bridged, index, SCALE).unwrap())
        .collect()
}

fn bridge_renders(bridged: &Bridged) -> Vec<Rgba> {
    (0..bridged.pdf().pages().len())
        .map(|index| render::page(bridged, index, SCALE).unwrap())
        .collect()
}

#[test]
fn raw_rasters_scale_only_the_page_with_a_user_unit() {
    let document = three_pages();
    let version = document.current();
    let original = bridge(&version);
    let mut page = page_index(&version).pages()[1].dict().clone();
    page.insert(Name::from("UserUnit"), 2.into());
    let budget = Budget::default();
    let mut transaction = document.begin();
    transaction.write(5, &Value::Dict(page), &budget).unwrap();
    let scaled = bridge(&transaction.commit(&budget).unwrap());
    for (at, expected_scale) in [(0, 0.5), (1, 1.0), (2, 0.5)] {
        assert_eq!(
            render::page(&scaled, at, 0.5).unwrap(),
            render::page(&original, at, expected_scale).unwrap()
        );
    }
}

#[test]
fn corpus_pages_render_as_hayro_renders_the_original() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/assets/real_world");
    let mut files: Vec<_> = fs::read_dir(&dir)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.extension().is_some_and(|e| e == "pdf"))
        .collect();
    files.sort();
    assert_eq!(files.len(), 15);
    // Debug builds render slowly: one thread per file.
    std::thread::scope(|scope| {
        for path in &files {
            scope.spawn(move || {
                let bytes = fs::read(path).unwrap();
                let document = open(bytes.clone());
                let bridged = bridge(&document.current());
                assert_eq!(bridged.stats().bytes, bytes.len() + bridged.stats().update);
                let ours = bridge_renders(&bridged);
                let theirs = hayro_renders(bytes);
                assert_eq!(ours.len(), theirs.len(), "{}", path.display());
                for (index, (ours, theirs)) in ours.iter().zip(&theirs).enumerate() {
                    assert!(ours == theirs, "{} page {index}", path.display());
                }
            });
        }
    });
}

#[test]
fn a_highlight_stored_without_an_appearance_is_drawn_inside_its_rectangle() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/assets/real_world/pdfium-links-highlights-annots.pdf");
    let bytes = fs::read(path).unwrap();
    let document = open(bytes.clone());
    let version = document.current();
    let budget = Budget::default();
    let bridged = Bridged::with_appearances(&version, &page_index(&version), &budget).unwrap();
    assert!(!version.has_changes());
    let ours = render::page(&bridged, 0, 1.0).unwrap();
    let theirs = render::page(&Bridged::of_bytes(bytes), 0, 1.0).unwrap();
    assert_eq!((ours.width, ours.height), (theirs.width, theirs.height));
    // The highlight covers x 293 to 349 and y 250 to 262 from the top.
    let mut changed = 0;
    for (at, (a, b)) in ours.data.chunks(4).zip(theirs.data.chunks(4)).enumerate() {
        if a != b {
            let (x, y) = (at % usize::from(ours.width), at / usize::from(ours.width));
            assert!((292..=350).contains(&x), "pixel {x} {y}");
            assert!((249..=263).contains(&y), "pixel {x} {y}");
            changed += 1;
        }
    }
    assert!(changed > 0);
}

/// A content stream that fills a rectangle whose position and grey level
/// `seed` picks.
fn content(seed: u8) -> Value {
    let x = u32::from(seed % 50) * 2;
    let grey = f32::from(seed) / 255.0;
    let data = format!(
        "{grey:.3} g {x} 10 40 30 re f 0 0 1 rg 5 5 10 {} re f",
        10 + seed % 40
    );
    Value::Stream(Box::new(Stream::new(
        Dict::new(),
        Bytes::new(data.into_bytes()),
    )))
}

fn dict(entries: Vec<(&str, Value)>) -> Value {
    Value::Dict(
        entries
            .into_iter()
            .map(|(k, v)| (Name::from(k), v))
            .collect(),
    )
}

fn reference(number: u32) -> Value {
    Value::Reference(Reference::new(number, 0))
}

fn rect(width: i64, height: i64) -> Value {
    Value::Array(Array::from(vec![
        0.into(),
        0.into(),
        width.into(),
        height.into(),
    ]))
}

/// A document of three pages: objects 1 (catalog), 2 (tree), pages 3, 5,
/// 7 with contents 4, 6, 8, and an unrelated object 9.
fn three_pages() -> Document {
    let mut base = MemoryBase::new([(Name::from("Root"), reference(1))].into_iter().collect());
    let mut put = |number: u32, value: Value| {
        let budget = Budget::default();
        let mut out = BudgetVec::new(&budget);
        write::value(&mut out, &value).unwrap();
        base.insert(Reference::new(number, 0), Bytes::new(out.into_vec()));
    };
    put(
        1,
        dict(vec![
            ("Type", Value::Name(Name::from("Catalog"))),
            ("Pages", reference(2)),
        ]),
    );
    put(
        2,
        dict(vec![
            ("Type", Value::Name(Name::from("Pages"))),
            (
                "Kids",
                Value::Array(Array::from(vec![reference(3), reference(5), reference(7)])),
            ),
            ("Count", 3.into()),
            ("MediaBox", rect(100, 80)),
        ]),
    );
    for (page, seed) in [(3, 10), (5, 120), (7, 230)] {
        put(page, page_value(page + 1, None));
        put(page + 1, content(seed));
    }
    put(9, Value::Integer(9));
    Document::new(Arc::new(base), None)
}

fn page_value(contents: u32, rotate: Option<i64>) -> Value {
    let mut entries = vec![
        ("Type", Value::Name(Name::from("Page"))),
        ("Parent", reference(2)),
        ("Contents", reference(contents)),
    ];
    if let Some(rotate) = rotate {
        entries.push(("Rotate", rotate.into()));
    }
    dict(entries)
}

#[derive(Clone, Debug)]
enum Edit {
    Content(u32, u8),
    Rotate(u32, i64),
    MediaBox(i64, i64),
    Unrelated(i64),
    Reverse,
    Undo,
}

fn edit() -> impl Strategy<Value = Edit> {
    let page = prop_oneof![Just(3u32), Just(5), Just(7)];
    prop_oneof![
        (page.clone(), any::<u8>()).prop_map(|(p, s)| Edit::Content(p, s)),
        (
            page,
            prop_oneof![Just(0i64), Just(90), Just(180), Just(270)]
        )
            .prop_map(|(p, r)| Edit::Rotate(p, r)),
        (40..160i64, 40..160i64).prop_map(|(w, h)| Edit::MediaBox(w, h)),
        any::<i64>().prop_map(Edit::Unrelated),
        Just(Edit::Reverse),
        Just(Edit::Undo),
    ]
}

fn apply(document: &Document, edit: &Edit, budget: &Budget) {
    if let Edit::Undo = edit {
        document.undo();
        return;
    }
    let mut transaction = document.begin();
    match *edit {
        Edit::Content(page, seed) => {
            transaction.write(page + 1, &content(seed), budget).unwrap();
        }
        Edit::Rotate(page, rotate) => {
            transaction
                .write(page, &page_value(page + 1, Some(rotate)), budget)
                .unwrap();
        }
        Edit::MediaBox(..) | Edit::Reverse => {
            let object = transaction.version().get(2, budget).unwrap().unwrap();
            let Value::Dict(mut tree) = (**object.value()).clone() else {
                unreachable!("object 2 is the page tree");
            };
            if let Edit::MediaBox(width, height) = *edit {
                tree.insert(Name::from("MediaBox"), rect(width, height));
            } else if let Some(Value::Array(kids)) = tree.get_mut("Kids") {
                let mut reversed: Vec<Value> = kids.iter().cloned().collect();
                reversed.reverse();
                *kids = Array::from(reversed);
            }
            transaction.write(2, &Value::Dict(tree), budget).unwrap();
        }
        Edit::Unrelated(value) => {
            transaction
                .write(9, &Value::Integer(value), budget)
                .unwrap();
        }
        Edit::Undo => unreachable!(),
    }
    transaction.commit(budget).unwrap();
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(24))]
    #[test]
    fn edited_versions_render_as_their_full_saves(edits in proptest::collection::vec(edit(), 1..8)) {
        let budget = Budget::default();
        let document = three_pages();
        let cache = Cache::new();
        for edit in &edits {
            apply(&document, edit, &budget);
            let version = document.current();
            let pages = page_index(&version);
            let bridged = cache.get(&version, &pages, &budget).unwrap();
            let mut saved = Vec::new();
            save::full(&version, &mut saved, &budget).unwrap();
            prop_assert_eq!(bridge_renders(&bridged), hayro_renders(saved));
        }
    }
}

#[test]
fn an_edited_file_renders_as_its_full_save() {
    let budget = Budget::default();
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/assets/real_world");
    let path = fs::read_dir(&dir)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.extension().is_some_and(|e| e == "pdf"))
        .min()
        .unwrap();
    let document = open(fs::read(&path).unwrap());
    let version = document.current();
    let pages = page_index(&version);
    let first = pages.get(0).unwrap().object().unwrap();
    let mut page = pages.get(0).unwrap().dict().clone();
    page.insert(Name::from("Rotate"), 90.into());
    let mut transaction = document.begin();
    transaction
        .write(first.number(), &Value::Dict(page), &budget)
        .unwrap();
    let edited = transaction.commit(&budget).unwrap();
    let bridged = bridge(&edited);
    assert!(bridged.stats().update > 0);
    let mut saved = Vec::new();
    save::full(&edited, &mut saved, &budget).unwrap();
    let ours = render::page(&bridged, 0, SCALE).unwrap();
    assert_eq!(ours, hayro_renders(saved).swap_remove(0));
    let original = render::page(&bridge(&version), 0, SCALE).unwrap();
    assert_eq!((ours.width, ours.height), (original.height, original.width));
}

#[test]
fn a_high_object_number_adds_one_row_not_every_number_below_it() {
    let budget = Budget::default();
    let document = three_pages();
    let high = 200_000_000;
    let mut transaction = document.begin();
    transaction.write(high, &content(77), &budget).unwrap();
    transaction
        .write(3, &page_value(high, None), &budget)
        .unwrap();
    let version = transaction.commit(&budget).unwrap();
    assert!(version.next_number() > u64::from(high));
    let bridged = bridge(&version);
    // Ten rows of 13 bytes, not one for each number below `high`.
    assert!(bridged.stats().update < 4096, "{}", bridged.stats().update);
    let mut saved = Vec::new();
    save::full(&version, &mut saved, &budget).unwrap();
    assert_eq!(bridge_renders(&bridged), hayro_renders(saved));
}

#[test]
fn the_cache_keeps_one_version_and_no_version_alive() {
    let budget = Budget::default();
    let document = three_pages();
    let cache = Cache::new();
    let version = document.current();
    let pages = page_index(&version);
    let first = cache.get(&version, &pages, &budget).unwrap();
    assert!(Arc::ptr_eq(
        &first,
        &cache.get(&version, &pages, &budget).unwrap()
    ));
    apply(&document, &Edit::Unrelated(1), &budget);
    let next = document.current();
    let second = cache.get(&next, &page_index(&next), &budget).unwrap();
    assert!(!Arc::ptr_eq(&first, &second));
}

#[test]
fn clear_drops_the_cached_bridge() {
    let budget = Budget::default();
    let document = three_pages();
    let cache = Cache::new();
    let version = document.current();
    let pages = page_index(&version);
    let cached = cache.get(&version, &pages, &budget).unwrap();
    let weak = Arc::downgrade(&cached);
    drop(cached);
    assert!(weak.upgrade().is_some());
    cache.clear();
    assert!(weak.upgrade().is_none());
}

fn assert_interpretation_limit(error: Error, limit: u64) {
    let Error::Store(store) = error else {
        panic!("expected an interpreter input limit, got {error}");
    };
    let refusal = store.limit().unwrap();
    assert_eq!(
        refusal.code(),
        pyropdf_store::budget::LimitCode::InterpretationSize
    );
    assert_eq!(refusal.limit(), Some(limit));
    assert!(refusal.amount().unwrap() > limit);
}

fn input_budget(limit: u64) -> Budget {
    Budget::default().with_limit(
        pyropdf_store::budget::LimitCode::InterpretationSize,
        Some(limit),
    )
}

#[test]
fn the_exact_input_fits_before_and_after_an_edit() {
    let assets = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/assets");
    let plain =
        open(fs::read(assets.join("real_world/pdfium-links-highlights-annots.pdf")).unwrap());
    let (encrypted, _) = open_encrypted(&assets.join("encrypted/user-aes-128.pdf"));
    for document in [three_pages(), plain, encrypted] {
        for edited in [false, true] {
            if edited {
                let mut transaction = document.begin();
                let reference = transaction.allocate().unwrap();
                transaction
                    .write(reference.number(), &content(7), &Budget::default())
                    .unwrap();
                transaction.commit(&Budget::default()).unwrap();
            }
            let version = document.current();
            let pages = page_index(&version);
            let size = bridge(&version).stats().bytes as u64;
            let exact = input_budget(size);
            // The same operation can interpret two inputs of this size.
            for _ in 0..2 {
                assert_eq!(
                    Bridged::new(&version, &pages, &exact)
                        .unwrap()
                        .stats()
                        .bytes as u64,
                    size
                );
            }
            let below = size - 1;
            assert_interpretation_limit(
                Bridged::new(&version, &pages, &input_budget(below)).unwrap_err(),
                below,
            );
        }
    }
}

#[test]
fn cached_inputs_and_render_only_appearances_observe_the_callers_limit() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/assets/real_world/pdfium-links-highlights-annots.pdf");
    let version = open(fs::read(path).unwrap()).current();
    let pages = page_index(&version);
    let base_limit = version.base().file().unwrap().bytes().len() as u64 - 1;
    let no_memory = input_budget(base_limit).with_limit(LimitCode::MemorySize, Some(0));
    assert_interpretation_limit(
        Bridged::with_appearances(&version, &pages, &no_memory).unwrap_err(),
        base_limit,
    );
    let cache = Cache::new();
    let plain = cache.get(&version, &pages, &Budget::default()).unwrap();
    let size = plain.stats().bytes as u64;
    assert_interpretation_limit(
        cache
            .get(&version, &pages, &input_budget(size - 1))
            .unwrap_err(),
        size - 1,
    );
    let exact = input_budget(size);
    assert_interpretation_limit(
        cache
            .get_for_render(&version, &pages, &exact, true)
            .unwrap_err(),
        size,
    );
    assert!(Arc::ptr_eq(
        &plain,
        &cache.get(&version, &pages, &exact).unwrap()
    ));
    let painted = cache
        .get_for_render(&version, &pages, &Budget::default(), true)
        .unwrap();
    let painted_size = painted.stats().bytes as u64;
    assert!(painted_size > size);
    assert_interpretation_limit(
        cache
            .get_for_render(&version, &pages, &input_budget(painted_size - 1), true)
            .unwrap_err(),
        painted_size - 1,
    );
    assert!(Arc::ptr_eq(
        &painted,
        &cache
            .get_for_render(&version, &pages, &input_budget(painted_size), true)
            .unwrap()
    ));
}

#[test]
fn a_page_list_that_differs_from_the_index_refuses() {
    let budget = Budget::default();
    let document = three_pages();
    let version = document.current();
    let other = three_pages();
    apply(&other, &Edit::Reverse, &budget);
    let reversed = page_index(&other.current());
    assert!(matches!(
        Bridged::new(&version, &reversed, &budget),
        Err(Error::PageMismatch { .. })
    ));
}

/// Opens an encrypted fixture with its user password, or with none when
/// the user password is empty, and returns it with that password.
fn open_encrypted(path: &Path) -> (Document, &'static str) {
    let budget = Budget::default();
    let password = if path
        .file_name()
        .unwrap()
        .to_str()
        .unwrap()
        .starts_with("owneronly")
    {
        ""
    } else {
        "userpw"
    };
    let bytes = Bytes::new(fs::read(path).unwrap());
    let base = FileBase::open_with_password(bytes, Some(password.as_bytes()), &budget).unwrap();
    (Document::new(Arc::new(base), None), password)
}

/// Returns a plain full save of `version`, which a transaction decrypts by
/// dropping `/Encrypt`.
fn decrypted(document: &Document, budget: &Budget) -> Vec<u8> {
    let mut transaction = document.begin();
    let mut trailer = transaction.version().trailer().clone();
    trailer.remove("Encrypt");
    transaction.set_trailer(trailer, budget).unwrap();
    let plain = transaction.commit(budget).unwrap();
    document.undo();
    let mut saved = Vec::new();
    save::full(&plain, &mut saved, budget).unwrap();
    saved
}

#[test]
fn encrypted_files_render_unedited_and_edited_as_their_decrypted_saves() {
    let budget = Budget::default();
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/assets/encrypted");
    let mut files: Vec<_> = fs::read_dir(&dir)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.extension().is_some_and(|e| e == "pdf"))
        .collect();
    files.sort();
    assert_eq!(files.len(), 11);
    for path in files {
        let (document, password) = open_encrypted(&path);
        let version = document.current();
        if path.ends_with("user-aes-gcm.pdf") {
            assert!(matches!(
                Bridged::new(&version, &page_index(&version), &budget),
                Err(Error::AesGcm)
            ));
            assert_eq!(hayro_renders(decrypted(&document, &budget)).len(), 1);
            continue;
        }
        let unedited = bridge(&version);
        assert_eq!(unedited.stats().update, 0, "{}", path.display());
        assert_eq!(
            bridge_renders(&unedited),
            hayro_renders(decrypted(&document, &budget)),
            "{}",
            path.display()
        );

        // A rotated page and a new content stream, whose data hayro
        // decrypts only if the bridge encrypted it.
        let pages = page_index(&version);
        let first = pages.get(0).unwrap().object().unwrap();
        let mut page = pages.get(0).unwrap().dict().clone();
        let mut transaction = document.begin();
        let contents = transaction.allocate().unwrap();
        transaction
            .write(contents.number(), &content(7), &budget)
            .unwrap();
        page.insert(Name::from("Rotate"), 90.into());
        page.insert(Name::from("Contents"), Value::Reference(contents));
        transaction
            .write(first.number(), &Value::Dict(page), &budget)
            .unwrap();
        let edited = transaction.commit(&budget).unwrap();
        let bridged = bridge(&edited);
        assert!(bridged.stats().update > 0);
        assert_eq!(
            bridge_renders(&bridged),
            hayro_renders(decrypted(&document, &budget)),
            "{} with password {password:?}",
            path.display()
        );
    }
}

#[test]
fn legacy_owner_bridge_keeps_the_original_ciphertext_as_its_prefix() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/assets/encrypted");
    let budget = Budget::default();
    for name in [
        "user-rc4-40.pdf",
        "user-rc4-128-indirect-length.pdf",
        "user-r4-rc4-128.pdf",
        "user-aes-128.pdf",
    ] {
        let source = Bytes::new(fs::read(dir.join(name)).unwrap());
        let base = FileBase::open_with_password(source.clone(), Some(b"ownerpw"), &budget).unwrap();
        let document = Document::new(Arc::new(base), None);
        let original = document.current();
        let before = bridge(&original);
        assert_eq!(before.stats().update, 0);
        assert_eq!(before.pdf.data().as_ref(), source.as_slice());

        let page = page_index(&original);
        let first = page.get(0).unwrap();
        let mut dict = first.dict().clone();
        dict.insert(Name::from("Rotate"), 90.into());
        let mut change = document.begin();
        change
            .write(
                first.object().unwrap().number(),
                &Value::Dict(dict),
                &budget,
            )
            .unwrap();
        let edited = change.commit(&budget).unwrap();
        let after = bridge(&edited);
        assert!(after.stats().update > 0);
        assert!(after.pdf.data().as_ref().starts_with(source.as_slice()));
    }
}

#[test]
fn a_version_that_changes_the_encryption_is_refused() {
    let budget = Budget::default();
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/assets/encrypted");
    let (document, _) = open_encrypted(&dir.join("user-aes-128.pdf"));
    let version = document.current();
    let Some(Value::Reference(encrypt)) = version.trailer().get("Encrypt").cloned() else {
        panic!("the fixture's /Encrypt is a reference");
    };
    let rewritten = version.get(encrypt.number(), &budget).unwrap().unwrap();
    let mut transaction = document.begin();
    transaction
        .write(encrypt.number(), rewritten.value(), &budget)
        .unwrap();
    let edited = transaction.commit(&budget).unwrap();
    assert_eq!(
        Bridged::new(&edited, &page_index(&edited), &budget).unwrap_err(),
        Error::EncryptionChanged
    );
    let mut transaction = document.begin();
    let mut trailer = transaction.version().trailer().clone();
    trailer.insert(Name::from("Encrypt"), Value::Integer(0));
    transaction.set_trailer(trailer, &budget).unwrap();
    let edited = transaction.commit(&budget).unwrap();
    assert_eq!(
        Bridged::new(&edited, &page_index(&edited), &budget).unwrap_err(),
        Error::EncryptionChanged
    );
}

#[test]
fn an_encrypted_version_without_a_file_is_refused() {
    let budget = Budget::default();
    let mut base = MemoryBase::new(
        [
            (Name::from("Root"), reference(1)),
            (Name::from("Encrypt"), reference(3)),
        ]
        .into_iter()
        .collect(),
    );
    base.insert(
        Reference::new(1, 0),
        Bytes::new(&b"<</Type/Catalog/Pages 2 0 R>>"[..]),
    );
    base.insert(
        Reference::new(2, 0),
        Bytes::new(&b"<</Type/Pages/Kids[]/Count 0>>"[..]),
    );
    let document = Document::new(Arc::new(base), None);
    let version = document.current();
    assert_eq!(
        Bridged::new(&version, &page_index(&version), &budget).unwrap_err(),
        Error::EncryptionChanged
    );
}

/// A file base that records every object the store reads from it.
#[derive(Debug)]
struct Counting {
    file: FileBase,
    reads: Mutex<Vec<u32>>,
}

impl pyropdf_store::version::Base for Counting {
    fn generation(&self, number: u32) -> Option<u16> {
        self.file.generation(number)
    }

    fn free_generation(&self, number: u32) -> Option<u16> {
        self.file.free_generation(number)
    }

    fn object(
        &self,
        reference: Reference,
        budget: &Budget,
    ) -> pyropdf_store::Result<Option<Bytes>> {
        self.reads.lock().unwrap().push(reference.number());
        self.file.object(reference, budget)
    }

    fn trailer(&self) -> &Dict {
        self.file.trailer()
    }

    fn next_number(&self) -> u64 {
        self.file.next_number()
    }

    fn numbers(&self, out: &mut BudgetVec<'_, u32>) -> pyropdf_store::Result<()> {
        self.file.numbers(out)
    }

    fn file(&self) -> Option<&Index> {
        self.file.file()
    }
}

#[test]
fn an_edit_then_a_page_read_parses_and_serializes_only_what_it_wrote() {
    let budget = Budget::default();
    let path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/assets/real_world/bill-hr815.pdf");
    let index = Index::open(Bytes::new(fs::read(path).unwrap()), &budget).unwrap();
    let base = Arc::new(Counting {
        file: FileBase::new(index, &budget).unwrap(),
        reads: Mutex::new(Vec::new()),
    });
    let document = Document::new(
        Arc::clone(&base) as Arc<dyn pyropdf_store::version::Base>,
        None,
    );
    // A first page read: the page index walk and the bridge.
    let version = document.current();
    let pages = page_index(&version);
    assert_eq!(pages.len(), 110);
    bridge(&version);
    let first: Vec<u32> = base.reads.lock().unwrap().drain(..).collect();
    assert!(!first.is_empty());

    let page = pages.get(pages.len() / 2).unwrap();
    let number = page.object().unwrap().number();
    let mut dict = page.dict().clone();
    dict.insert(Name::from("Rotate"), 90.into());
    let mut transaction = document.begin();
    transaction
        .write(number, &Value::Dict(dict), &budget)
        .unwrap();
    let edited = transaction.commit(&budget).unwrap();

    let edited_pages = page_index(&edited);
    let (bytes, update) = update(&edited, &[], None, None, &budget).unwrap();
    let bridged = Bridged::new(&edited, &edited_pages, &budget).unwrap();
    let reads = base.reads.lock().unwrap().clone();
    // The store reads no object of the file again, the page tree's nodes
    // included: the first walk parsed them, and every version shares them.
    assert_eq!(reads, Vec::<u32>::new());
    // The update holds the written page and the cross-reference stream.
    let appended = &bytes[bytes.len() - update..];
    let objects = appended.windows(7).filter(|w| *w == b"\nendobj").count();
    assert_eq!(objects, 2);
    assert!(appended.starts_with(format!("\n{number} 0 obj").as_bytes()));
    assert_eq!(bridged.stats().update, update);
    let rotated = render::page(&bridged, pages.len() / 2, SCALE).unwrap();
    let original = render::page(&bridge(&version), pages.len() / 2, SCALE).unwrap();
    assert_eq!(
        (rotated.width, rotated.height),
        (original.height, original.width)
    );
}

#[test]
fn the_cache_separates_plain_reads_and_annotation_drawing() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/assets/real_world/pdfium-links-highlights-annots.pdf");
    let document = open(fs::read(path).unwrap());
    let version = document.current();
    let budget = Budget::default();
    let pages = page_index(&version);
    let cache = Cache::new();
    let plain = cache.get(&version, &pages, &budget).unwrap();
    let painted = cache
        .get_for_render(&version, &pages, &budget, true)
        .unwrap();
    assert!(!Arc::ptr_eq(&plain, &painted));
    assert_ne!(
        render::page(&plain, 0, 1.0).unwrap().data,
        render::page(&painted, 0, 1.0).unwrap().data
    );
    let plain_again = cache.get(&version, &pages, &budget).unwrap();
    assert_eq!(
        render::page(&plain, 0, 1.0).unwrap().data,
        render::page(&plain_again, 0, 1.0).unwrap().data
    );
    assert!(!version.has_changes());
}

#[test]
fn synthesis_refuses_before_exceeding_aggregate_points() {
    use pyropdf_store::budget::LimitCode;
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/assets/real_world/pdfium-links-highlights-annots.pdf");
    let document = open(fs::read(path).unwrap());
    let version = document.current();
    let budget = Budget::default().with_limit(LimitCode::AnnotationSynthesisPointCount, Some(3));
    let error =
        crate::annotations::synthesized(&version, &page_index(&version), &budget).unwrap_err();
    assert_eq!(
        error.limit().unwrap().code(),
        LimitCode::AnnotationSynthesisPointCount
    );
    assert!(budget.used(LimitCode::AnnotationSynthesisPointCount) <= 3);
    assert!(!version.has_changes());
}

#[test]
fn synthesis_refuses_exhausted_virtual_object_numbers() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/assets/real_world/pdfium-links-highlights-annots.pdf");
    let document = open(fs::read(path).unwrap());
    let budget = Budget::default();
    let mut transaction = document.begin();
    transaction
        .write(u32::MAX - 1, &Value::Null, &budget)
        .unwrap();
    let version = transaction.commit(&budget).unwrap();
    let error =
        crate::annotations::synthesized(&version, &page_index(&version), &budget).unwrap_err();
    assert!(error.to_string().contains("no object number remains"));
}

#[test]
fn synthesis_counts_generated_appearances_against_the_shared_budget() {
    use pyropdf_store::budget::LimitCode;
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/assets/real_world/pdfium-links-highlights-annots.pdf");
    let document = open(fs::read(path).unwrap());
    let version = document.current();
    let budget = Budget::default().with_limit(LimitCode::AnnotationSynthesisCount, Some(0));
    let error =
        crate::annotations::synthesized(&version, &page_index(&version), &budget).unwrap_err();
    assert_eq!(
        error.limit().unwrap().code(),
        LimitCode::AnnotationSynthesisCount
    );
    assert!(!version.has_changes());
}

#[test]
fn ocg_lowering_is_noop_without_oc_properties() {
    let document = three_pages();
    let version = document.current();
    let budget = Budget::default();
    let pages = page_index(&version);
    let lowered = ocg::lower(&version, &pages, &budget).unwrap();
    assert!(lowered.objects.is_empty());
    assert!(lowered.hidden_annots.is_empty());
    assert_eq!(lowered.next_number, version.next_number());
}

#[test]
fn ocg_lowering_handles_ve_direct_ocmd_and_annotation_oc_without_mutating_version() {
    use pyropdf_store::value::{PdfString, Reference};

    let budget = Budget::default();
    let mut base = MemoryBase::new([(Name::from("Root"), reference(1))].into_iter().collect());
    let mut put = |number: u32, value: Value| {
        let mut out = BudgetVec::new(&budget);
        write::value(&mut out, &value).unwrap();
        base.insert(Reference::new(number, 0), Bytes::new(out.into_vec()));
    };
    let catalog = Reference::new(1, 0);
    let pages = Reference::new(2, 0);
    let page = Reference::new(3, 0);
    let content_ref = Reference::new(4, 0);
    let ocg_on = Reference::new(5, 0);
    let ocg_off = Reference::new(6, 0);
    let ocmd_ve_hidden = Reference::new(7, 0);
    let ocmd_ve_visible = Reference::new(8, 0);
    let form_xobj = Reference::new(9, 0);
    let annot_ap = Reference::new(10, 0);
    let annot_hidden = Reference::new(11, 0);
    let annot_visible = Reference::new(12, 0);

    // Two OCGs: ocg_on is ON, ocg_off is OFF.
    let mut g1 = Dict::new();
    g1.insert(Name::from("Type"), Value::Name(Name::from("OCG")));
    g1.insert(
        Name::from("Name"),
        Value::String(PdfString::literal(b"On".as_slice())),
    );
    put(ocg_on.number(), Value::Dict(g1));

    let mut g2 = Dict::new();
    g2.insert(Name::from("Type"), Value::Name(Name::from("OCG")));
    g2.insert(
        Name::from("Name"),
        Value::String(PdfString::literal(b"Off".as_slice())),
    );
    put(ocg_off.number(), Value::Dict(g2));

    // Indirect OCMD with /VE [/And ocg_on ocg_off] -> hidden (even though /OCGs says ocg_on AllOn).
    let mut hidden_ocmd = Dict::new();
    hidden_ocmd.insert(Name::from("Type"), Value::Name(Name::from("OCMD")));
    hidden_ocmd.insert(
        Name::from("OCGs"),
        Value::Array(Array::from(vec![Value::Reference(ocg_on)])),
    );
    hidden_ocmd.insert(Name::from("P"), Value::Name(Name::from("AllOn")));
    hidden_ocmd.insert(
        Name::from("VE"),
        Value::Array(Array::from(vec![
            Value::Name(Name::from("And")),
            Value::Reference(ocg_on),
            Value::Reference(ocg_off),
        ])),
    );
    put(ocmd_ve_hidden.number(), Value::Dict(hidden_ocmd));

    // Indirect OCMD with /VE [/And ocg_on [/Not ocg_off]] -> visible.
    let mut visible_ocmd = Dict::new();
    visible_ocmd.insert(Name::from("Type"), Value::Name(Name::from("OCMD")));
    visible_ocmd.insert(
        Name::from("VE"),
        Value::Array(Array::from(vec![
            Value::Name(Name::from("And")),
            Value::Reference(ocg_on),
            Value::Array(Array::from(vec![
                Value::Name(Name::from("Not")),
                Value::Reference(ocg_off),
            ])),
        ])),
    );
    put(ocmd_ve_visible.number(), Value::Dict(visible_ocmd));

    // Form XObject with direct /OC containing hidden /VE painting green at (60..80, 60..80).
    let mut form_oc = Dict::new();
    form_oc.insert(Name::from("Type"), Value::Name(Name::from("OCMD")));
    form_oc.insert(
        Name::from("VE"),
        Value::Array(Array::from(vec![
            Value::Name(Name::from("Not")),
            Value::Reference(ocg_on),
        ])),
    );
    let mut form_dict = Dict::new();
    form_dict.insert(Name::from("Type"), Value::Name(Name::from("XObject")));
    form_dict.insert(Name::from("Subtype"), Value::Name(Name::from("Form")));
    form_dict.insert(
        Name::from("BBox"),
        Value::Array(Array::from(vec![
            0.into(),
            0.into(),
            100.into(),
            100.into(),
        ])),
    );
    form_dict.insert(Name::from("OC"), Value::Dict(form_oc));
    let form_stream = Stream::new(form_dict, Bytes::new(b"0 1 0 rg 60 60 20 20 re f".to_vec()));
    put(form_xobj.number(), Value::from(form_stream));

    // Annotation appearance painting blue in its rect.
    let mut ap_dict = Dict::new();
    ap_dict.insert(Name::from("Type"), Value::Name(Name::from("XObject")));
    ap_dict.insert(Name::from("Subtype"), Value::Name(Name::from("Form")));
    ap_dict.insert(
        Name::from("BBox"),
        Value::Array(Array::from(vec![0.into(), 0.into(), 20.into(), 20.into()])),
    );
    let ap_stream = Stream::new(ap_dict, Bytes::new(b"0 0 1 rg 0 0 20 20 re f".to_vec()));
    put(annot_ap.number(), Value::from(ap_stream));

    let make_annot = |rect: [i64; 4], oc: Reference| {
        let mut ap = Dict::new();
        ap.insert(Name::from("N"), Value::Reference(annot_ap));
        let mut a = Dict::new();
        a.insert(Name::from("Type"), Value::Name(Name::from("Annot")));
        a.insert(Name::from("Subtype"), Value::Name(Name::from("Stamp")));
        a.insert(
            Name::from("Rect"),
            Value::Array(Array::from(
                rect.into_iter().map(Value::Integer).collect::<Vec<_>>(),
            )),
        );
        a.insert(Name::from("AP"), Value::Dict(ap));
        a.insert(Name::from("OC"), Value::Reference(oc));
        a.insert(Name::from("F"), Value::Integer(4));
        Value::Dict(a)
    };
    put(annot_hidden.number(), make_annot([10, 10, 30, 30], ocg_off));
    put(
        annot_visible.number(),
        make_annot([40, 10, 60, 30], ocmd_ve_visible),
    );

    // Direct OCMD inside /Properties with hidden /VE painting red at (10..30, 60..80).
    let mut direct_hidden = Dict::new();
    direct_hidden.insert(Name::from("Type"), Value::Name(Name::from("OCMD")));
    direct_hidden.insert(
        Name::from("VE"),
        Value::Array(Array::from(vec![
            Value::Name(Name::from("And")),
            Value::Reference(ocg_on),
            Value::Reference(ocg_off),
        ])),
    );

    // Page content draws:
    // - hidden indirect OCMD red square at (10..30, 40..60)
    // - visible indirect OCMD red square at (40..60, 40..60)
    // - hidden direct OCMD red square at (10..30, 60..80)
    // - hidden Form XObject green square at (60..80, 60..80)
    let stream_bytes = b"/OC /HidInd BDC 1 0 0 rg 10 40 20 20 re f EMC \
        /OC /VisInd BDC 1 0 0 rg 40 40 20 20 re f EMC \
        /OC /HidDir BDC 1 0 0 rg 10 60 20 20 re f EMC \
        /Fm1 Do";
    put(
        content_ref.number(),
        Value::from(Stream::new(Dict::new(), Bytes::new(stream_bytes.to_vec()))),
    );

    let mut props = Dict::new();
    props.insert(Name::from("HidInd"), Value::Reference(ocmd_ve_hidden));
    props.insert(Name::from("VisInd"), Value::Reference(ocmd_ve_visible));
    props.insert(Name::from("HidDir"), Value::Dict(direct_hidden));

    let mut xobjs = Dict::new();
    xobjs.insert(Name::from("Fm1"), Value::Reference(form_xobj));

    let mut resources = Dict::new();
    resources.insert(Name::from("Properties"), Value::Dict(props));
    resources.insert(Name::from("XObject"), Value::Dict(xobjs));

    let mut page_dict = Dict::new();
    page_dict.insert(Name::from("Type"), Value::Name(Name::from("Page")));
    page_dict.insert(Name::from("Parent"), Value::Reference(pages));
    page_dict.insert(
        Name::from("MediaBox"),
        Value::Array(Array::from(vec![
            0.into(),
            0.into(),
            100.into(),
            100.into(),
        ])),
    );
    page_dict.insert(Name::from("Resources"), Value::Dict(resources));
    page_dict.insert(Name::from("Contents"), Value::Reference(content_ref));
    page_dict.insert(
        Name::from("Annots"),
        Value::Array(Array::from(vec![
            Value::Reference(annot_hidden),
            Value::Reference(annot_visible),
        ])),
    );
    put(page.number(), Value::Dict(page_dict));

    let mut pages_dict = Dict::new();
    pages_dict.insert(Name::from("Type"), Value::Name(Name::from("Pages")));
    pages_dict.insert(
        Name::from("Kids"),
        Value::Array(Array::from(vec![Value::Reference(page)])),
    );
    pages_dict.insert(Name::from("Count"), Value::Integer(1));
    put(pages.number(), Value::Dict(pages_dict));

    let mut default_cfg = Dict::new();
    default_cfg.insert(Name::from("BaseState"), Value::Name(Name::from("ON")));
    default_cfg.insert(
        Name::from("OFF"),
        Value::Array(Array::from(vec![Value::Reference(ocg_off)])),
    );
    let mut oc_props = Dict::new();
    oc_props.insert(
        Name::from("OCGs"),
        Value::Array(Array::from(vec![
            Value::Reference(ocg_on),
            Value::Reference(ocg_off),
        ])),
    );
    oc_props.insert(Name::from("D"), Value::Dict(default_cfg));

    let mut cat = Dict::new();
    cat.insert(Name::from("Type"), Value::Name(Name::from("Catalog")));
    cat.insert(Name::from("Pages"), Value::Reference(pages));
    cat.insert(Name::from("OCProperties"), Value::Dict(oc_props));
    put(catalog.number(), Value::Dict(cat));

    let document = Document::new(Arc::new(base), None);
    let version = document.current();

    let index = page_index(&version);
    let bridged = Bridged::with_appearances(&version, &index, &budget).unwrap();
    let raster = render::page(&bridged, 0, 1.0).unwrap();
    let pixel = |x: usize, pdf_y: usize| -> [u8; 4] {
        // Raster origin is top-left on a 100x100 page over transparent background.
        let y = 99 - pdf_y;
        let offset = (y * usize::from(raster.width) + x) * 4;
        [
            raster.data[offset],
            raster.data[offset + 1],
            raster.data[offset + 2],
            raster.data[offset + 3],
        ]
    };

    assert_eq!(pixel(20, 50), [0, 0, 0, 0], "hidden indirect /VE");
    assert_eq!(pixel(50, 50), [255, 0, 0, 255], "visible indirect /VE");
    assert_eq!(pixel(20, 70), [0, 0, 0, 0], "hidden direct /VE");
    assert_eq!(pixel(70, 70), [0, 0, 0, 0], "hidden Form XObject /OC");
    assert_eq!(pixel(20, 20), [0, 0, 0, 0], "hidden annotation /OC");
    assert_eq!(
        pixel(50, 20),
        [0, 0, 255, 255],
        "visible annotation /VE /OC"
    );

    // Store version objects stay completely untouched.
    let stored_annot = version
        .get(annot_hidden.number(), &budget)
        .unwrap()
        .unwrap();
    let Value::Dict(stored_dict) = stored_annot.value().as_ref() else {
        panic!("expected dict");
    };
    assert_eq!(stored_dict.get("F"), Some(&Value::Integer(4)));
}

#[test]
fn ocg_lowering_refuses_cyclic_ve_with_optional_content_expression_limit() {
    use pyropdf_store::budget::LimitCode;

    let budget = Budget::default();
    let mut base = MemoryBase::new([(Name::from("Root"), reference(1))].into_iter().collect());
    let mut put = |number: u32, value: Value| {
        let mut out = BudgetVec::new(&budget);
        write::value(&mut out, &value).unwrap();
        base.insert(Reference::new(number, 0), Bytes::new(out.into_vec()));
    };
    let catalog = Reference::new(1, 0);
    let pages = Reference::new(2, 0);
    let page = Reference::new(3, 0);
    let ocg = Reference::new(4, 0);
    let cycle = Reference::new(5, 0);
    let ocmd = Reference::new(6, 0);

    let mut g = Dict::new();
    g.insert(Name::from("Type"), Value::Name(Name::from("OCG")));
    put(ocg.number(), Value::Dict(g));
    put(
        cycle.number(),
        Value::Array(Array::from(vec![
            Value::Name(Name::from("Not")),
            Value::Reference(cycle),
        ])),
    );
    let mut ocmd_dict = Dict::new();
    ocmd_dict.insert(Name::from("Type"), Value::Name(Name::from("OCMD")));
    ocmd_dict.insert(Name::from("VE"), Value::Reference(cycle));
    put(ocmd.number(), Value::Dict(ocmd_dict));

    let mut props = Dict::new();
    props.insert(Name::from("C"), Value::Reference(ocmd));
    let mut resources = Dict::new();
    resources.insert(Name::from("Properties"), Value::Dict(props));
    let mut page_dict = Dict::new();
    page_dict.insert(Name::from("Type"), Value::Name(Name::from("Page")));
    page_dict.insert(Name::from("Parent"), Value::Reference(pages));
    page_dict.insert(
        Name::from("MediaBox"),
        Value::Array(Array::from(vec![
            0.into(),
            0.into(),
            100.into(),
            100.into(),
        ])),
    );
    page_dict.insert(Name::from("Resources"), Value::Dict(resources));
    put(page.number(), Value::Dict(page_dict));

    let mut pages_dict = Dict::new();
    pages_dict.insert(Name::from("Type"), Value::Name(Name::from("Pages")));
    pages_dict.insert(
        Name::from("Kids"),
        Value::Array(Array::from(vec![Value::Reference(page)])),
    );
    pages_dict.insert(Name::from("Count"), Value::Integer(1));
    put(pages.number(), Value::Dict(pages_dict));

    let mut oc_props = Dict::new();
    oc_props.insert(
        Name::from("OCGs"),
        Value::Array(Array::from(vec![Value::Reference(ocg)])),
    );
    let mut cat = Dict::new();
    cat.insert(Name::from("Type"), Value::Name(Name::from("Catalog")));
    cat.insert(Name::from("Pages"), Value::Reference(pages));
    cat.insert(Name::from("OCProperties"), Value::Dict(oc_props));
    put(catalog.number(), Value::Dict(cat));

    let document = Document::new(Arc::new(base), None);
    let version = document.current();

    let err = Bridged::new(&version, &page_index(&version), &budget).unwrap_err();
    match err {
        Error::Store(store_err) => {
            assert_eq!(
                store_err.limit().unwrap().code(),
                LimitCode::OptionalContentExpression
            );
        }
        other => panic!("expected optional_content_expression limit, got {other:?}"),
    }
}
