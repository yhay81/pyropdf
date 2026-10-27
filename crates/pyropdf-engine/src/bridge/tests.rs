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
    let pdf = Pdf::new(bytes).unwrap();
    let pages = pdf.pages().len();
    let bridged = Bridged {
        pdf,
        stats: Stats::default(),
    };
    (0..pages)
        .map(|index| render::page(&bridged, index, SCALE).unwrap())
        .collect()
}

fn bridge_renders(bridged: &Bridged) -> Vec<Rgba> {
    (0..bridged.pdf().pages().len())
        .map(|index| render::page(bridged, index, SCALE).unwrap())
        .collect()
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
    assert_eq!(files.len(), 14);
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
    assert_eq!(files.len(), 10);
    for path in files {
        let (document, password) = open_encrypted(&path);
        let version = document.current();
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
    let (bytes, update) = update(&edited, &[], None, &budget).unwrap();
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
