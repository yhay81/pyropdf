//! The index reads every form of cross-reference data, checks it against
//! the object headers, and rebuilds it by one deterministic scan when the
//! data cannot be read or the check fails (DESIGN.md section 4.3).
//!
//! Besides synthetic files, the tests read the real-document corpus and
//! the damaged inputs that tests/corpus.py derives from it. The Rust tests
//! run that Python helper to get the damaged inputs, so that the damage
//! keeps one definition, the one that damage.sha256 pins, and no damaged
//! file enters the repository.

use std::fmt::Write as _;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use hayro_syntax::object::{FromBytes, MaybeRef, Object, ObjectIdentifier};

use super::*;
use crate::ErrorKind;
use crate::budget::CancellationToken;
use crate::value::{Name, Value};

/// A PDF written object by object, which records where each object begins.
struct Pdf {
    bytes: Vec<u8>,
    /// The objects written since the last section, with their positions.
    objects: Vec<(u32, usize)>,
}

impl Pdf {
    fn new() -> Self {
        Self {
            bytes: b"%PDF-1.7\n".to_vec(),
            objects: Vec::new(),
        }
    }

    /// Writes object `number` of generation 0, and returns where its header
    /// begins.
    fn object(&mut self, number: u32, body: &str) -> usize {
        let pos = self.bytes.len();
        self.objects.push((number, pos));
        self.bytes
            .extend_from_slice(format!("{number} 0 obj\n{body}\nendobj\n").as_bytes());
        pos
    }

    /// Writes an unfiltered object stream of `members`, and returns where
    /// its header begins.
    fn object_stream(&mut self, number: u32, members: &[(u32, &str)]) -> usize {
        let mut table = String::new();
        let mut data = String::new();
        for (member, body) in members {
            write!(table, "{member} {} ", data.len()).unwrap();
            data.push_str(body);
            data.push(' ');
        }
        let body = format!(
            "<< /Type /ObjStm /N {} /First {} /Length {} >>\nstream\n{table}{data}\nendstream",
            members.len(),
            table.len(),
            table.len() + data.len(),
        );
        self.object(number, &body)
    }

    /// Writes a classic table that lists `free` and the objects written
    /// since the last section, its trailer, and a `startxref` line naming
    /// it, and returns where the table begins.
    fn table(&mut self, free: &[(u32, u16)], trailer: &str) -> usize {
        let pos = self.bytes.len();
        let mut text = String::from("xref\n");
        for &(number, generation) in free {
            writeln!(text, "{number} 1\n0000000000 {generation:05} f ").unwrap();
        }
        for (number, offset) in self.objects.drain(..) {
            writeln!(text, "{number} 1\n{offset:010} 00000 n ").unwrap();
        }
        write!(text, "trailer\n{trailer}\nstartxref\n{pos}\n%%EOF\n").unwrap();
        self.bytes.extend_from_slice(text.as_bytes());
        pos
    }

    /// Writes an unfiltered cross-reference stream as object `number`, with
    /// one row of a type and two fields for each of `rows` and the extra
    /// entries `trailer` in its dictionary, and a `startxref` line naming
    /// it, and returns where its header begins.
    fn xref_stream(&mut self, number: u32, rows: &[(u32, [usize; 3])], trailer: &str) -> usize {
        let pos = self.bytes.len();
        let mut data = Vec::new();
        let mut index = String::new();
        for &(row, [kind, second, third]) in rows {
            write!(index, "{row} 1 ").unwrap();
            data.push(u8::try_from(kind).unwrap());
            data.extend_from_slice(&u32::try_from(second).unwrap().to_be_bytes());
            data.extend_from_slice(&u16::try_from(third).unwrap().to_be_bytes());
        }
        self.bytes.extend_from_slice(
            format!(
                "{number} 0 obj\n<< /Type /XRef /W [1 4 2] /Index [{index}] /Length {} {trailer} >>\nstream\n",
                data.len()
            )
            .as_bytes(),
        );
        self.bytes.extend_from_slice(&data);
        self.bytes.extend_from_slice(
            format!("\nendstream\nendobj\nstartxref\n{pos}\n%%EOF\n").as_bytes(),
        );
        pos
    }
}

/// Returns a document of three objects with a classic table.
fn simple() -> Vec<u8> {
    let mut pdf = Pdf::new();
    pdf.object(1, "<< /Type /Catalog /Pages 2 0 R >>");
    pdf.object(2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>");
    pdf.object(3, "<< /Type /Page /Parent 2 0 R >>");
    pdf.table(&[(0, 65535)], "<< /Size 4 /Root 1 0 R >>");
    pdf.bytes
}

/// Returns a document whose catalog is object 1 and whose cross-reference
/// stream, object 5, lists `rows` besides the catalog, object stream 4,
/// and itself, and the extra entries `trailer`.
fn compressed(members: &[(u32, &str)], rows: &[(u32, [usize; 3])], trailer: &str) -> Vec<u8> {
    let mut pdf = Pdf::new();
    let catalog = pdf.object(1, "<< /Type /Catalog /Pages 2 0 R >>");
    let stream = pdf.object_stream(4, members);
    let xref = pdf.bytes.len();
    let mut all = vec![(1, [1, catalog, 0]), (4, [1, stream, 0]), (5, [1, xref, 0])];
    all.extend_from_slice(rows);
    pdf.xref_stream(5, &all, &format!("/Size 8 /Root 1 0 R {trailer}"));
    pdf.bytes
}

fn open(bytes: Vec<u8>) -> Index {
    Index::open(Bytes::from(bytes), &Budget::default()).unwrap()
}

/// Returns the value of object `number` of `index`, if the index locates
/// it.
fn value(index: &Index, number: u32) -> Option<Value> {
    let budget = Budget::default();
    let entry = index.get(number)?;
    let bytes = index
        .object(Reference::new(number, entry.generation()), &budget)
        .unwrap()?;
    Some(Value::parse(&bytes, &budget).unwrap())
}

/// Returns the dictionary of object `number` of `index`.
fn dict(index: &Index, number: u32) -> Dict {
    match value(index, number) {
        Some(Value::Dict(dict)) => dict,
        other => panic!("object {number} is not a dictionary: {other:?}"),
    }
}

fn name(name: &str) -> Value {
    Value::Name(Name::from(name))
}

fn reference(number: u32) -> Value {
    Value::Reference(Reference::new(number, 0))
}

/// Returns where `part` first begins in `bytes`.
fn position(bytes: &[u8], part: &[u8]) -> usize {
    bytes
        .windows(part.len())
        .position(|window| window == part)
        .unwrap_or_else(|| panic!("{} is not in the bytes", part.escape_ascii()))
}

/// Returns `bytes` with the first `from` replaced by `to`.
fn replace(bytes: &[u8], from: &[u8], to: &[u8]) -> Vec<u8> {
    let at = position(bytes, from);
    [&bytes[..at], to, &bytes[at + from.len()..]].concat()
}

/// Returns the damage that made opening rebuild `index`.
fn damage(index: &Index) -> &Damage {
    index
        .repair()
        .unwrap_or_else(|| panic!("the index was not rebuilt"))
        .damage()
}

/// What each object of `index` resolves to when its in-use numbers are read
/// in the order that `order` gives: its value, or the error that reading it
/// gives, by object number.
fn outcomes(index: &Index, order: impl Fn(&mut Vec<u32>)) -> Vec<(u32, String)> {
    let budget = Budget::default();
    let mut numbers: Vec<u32> = index.entries().map(|(number, _)| number).collect();
    order(&mut numbers);
    let mut outcomes: Vec<(u32, String)> = numbers
        .iter()
        .map(|&number| {
            let generation = index.get(number).unwrap().generation();
            let outcome = index
                .object(Reference::new(number, generation), &budget)
                .and_then(|bytes| bytes.map(|bytes| Value::parse(&bytes, &budget)).transpose());
            (number, format!("{outcome:?}"))
        })
        .collect();
    outcomes.sort();
    outcomes
}

/// Checks that every object of the document of `bytes` resolves the same
/// whatever order its objects are read in, each order on an index of its
/// own.
fn assert_order_free(bytes: &[u8]) {
    let open = || Index::open(Bytes::from(bytes.to_vec()), &Budget::default());
    let Ok(index) = open() else {
        return;
    };
    let ascending = outcomes(&index, |_| {});
    let descending = outcomes(&open().unwrap(), |numbers| numbers.reverse());
    // The two halves interleaved, the second half first.
    let interleaved = outcomes(&open().unwrap(), |numbers| {
        let (low, high) = numbers.split_at(numbers.len() / 2);
        let mut order = Vec::with_capacity(numbers.len());
        for (index, &number) in high.iter().enumerate() {
            order.push(number);
            order.extend(low.get(index));
        }
        *numbers = order;
    });
    assert_eq!(ascending, descending);
    assert_eq!(ascending, interleaved);
}

#[test]
fn a_table_locates_every_object_and_reads_its_trailer() {
    let bytes = simple();
    let index = open(bytes.clone());
    assert!(!index.is_repaired());
    assert_eq!(index.origin(), 0);
    assert_eq!(index.len(), 3);
    assert_eq!(index.trailer().get("Root"), Some(&reference(1)));
    assert_eq!(dict(&index, 2).get("Type"), Some(&name("Pages")));
    assert_eq!(
        index.get(1).map(|entry| entry.location()),
        Some(Location::Offset(9))
    );
    // A free entry, a missing number, and another generation name no object.
    let budget = Budget::default();
    assert_eq!(index.object(Reference::new(0, 65535), &budget), Ok(None));
    assert_eq!(index.object(Reference::new(7, 0), &budget), Ok(None));
    assert_eq!(index.object(Reference::new(1, 1), &budget), Ok(None));
    let [revision] = index.revisions() else {
        panic!("not one revision: {:?}", index.revisions());
    };
    assert_eq!(revision.end(), Some(bytes.len()));
    let [section] = revision.sections() else {
        panic!("not one section");
    };
    assert_eq!(section.kind(), SectionKind::Table);
    assert_eq!(section.trailer().get("Size"), Some(&Value::Integer(4)));
    assert_eq!(
        section.listings()[0],
        (0, Listing::Free { generation: 65535 })
    );
    assert_eq!(section.listings().len(), 4);
}

#[test]
fn an_object_reads_no_further_than_the_next_object() {
    let index = open(simple());
    let bytes = index
        .object(Reference::new(1, 0), &Budget::default())
        .unwrap()
        .unwrap();
    assert_eq!(
        bytes.as_slice(),
        b"<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    );
}

#[test]
fn table_entries_read_whatever_white_space_separates_their_fields() {
    // Entries of 19 bytes, which end with one end-of-line byte, and of 22.
    let mut pdf = Pdf::new();
    let catalog = pdf.object(1, "<< /Type /Catalog /Pages 2 0 R >>");
    let pages = pdf.object(2, "<< /Type /Pages /Kids [] /Count 0 >>");
    let table = pdf.bytes.len();
    pdf.bytes.extend_from_slice(
        format!(
            "xref\n0 3\n0000000000 65535 f\n{catalog:010} 00000 n\n{pages:010}  00000 n \r\ntrailer << /Size 3 /Root 1 0 R >>\nstartxref\n{table}\n%%EOF"
        )
        .as_bytes(),
    );
    let index = open(pdf.bytes);
    assert!(!index.is_repaired(), "{:?}", index.repair());
    assert_eq!(dict(&index, 2).get("Count"), Some(&Value::Integer(0)));
}

#[test]
fn a_cross_reference_stream_locates_members_of_object_streams() {
    let bytes = compressed(
        &[
            (2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
            (3, "<< /Type /Page /Parent 2 0 R >>"),
        ],
        &[(0, [0, 0, 65535]), (2, [2, 4, 0]), (3, [2, 4, 1])],
        "",
    );
    let xref = position(&bytes, b"5 0 obj");
    let index = open(bytes);
    assert!(!index.is_repaired(), "{:?}", index.repair());
    assert_eq!(
        index.get(3).map(|entry| entry.location()),
        Some(Location::Member {
            stream: 4,
            index: 1
        })
    );
    assert_eq!(dict(&index, 3).get("Parent"), Some(&reference(2)));
    assert_eq!(dict(&index, 2).get("Count"), Some(&Value::Integer(1)));
    let budget = Budget::default();
    let objects = index.object_stream(4, &budget).unwrap().unwrap();
    assert!(objects.mismatched().is_empty());
    let numbers: Vec<u32> = objects
        .members()
        .iter()
        .map(|&(number, _)| number)
        .collect();
    assert_eq!(numbers, [2, 3]);
    assert!(index.object_stream(1, &budget).unwrap().is_none());
    let [revision] = index.revisions() else {
        panic!("not one revision");
    };
    assert_eq!(revision.sections()[0].kind(), SectionKind::Stream);
    assert_eq!(revision.sections()[0].offset(), xref);
    // A cross-reference stream's dictionary is its trailer.
    assert_eq!(index.trailer().get("Type"), Some(&name("XRef")));
}

#[test]
fn a_hybrid_section_reads_its_stream_under_the_free_entries_of_its_table() {
    // The table lists the member of the object stream as free, so that a
    // reader without cross-reference streams skips it (ISO 32000-2 section
    // 7.5.8.4); the stream that /XRefStm names lists it as a member.
    let mut pdf = Pdf::new();
    pdf.object(1, "<< /Type /Catalog /Pages 2 0 R >>");
    let stream = pdf.object_stream(4, &[(2, "<< /Type /Pages /Kids [] /Count 0 >>")]);
    let objects = std::mem::take(&mut pdf.objects);
    let hidden = pdf.xref_stream(6, &[(2, [2, 4, 0])], "/Size 7");
    let table = pdf.bytes.len();
    pdf.objects = objects;
    pdf.table(
        &[(0, 65535), (2, 0)],
        &format!("<< /Size 7 /Root 1 0 R /XRefStm {hidden} >>"),
    );
    let index = open(pdf.bytes);
    assert!(!index.is_repaired(), "{:?}", index.repair());
    assert_eq!(dict(&index, 2).get("Type"), Some(&name("Pages")));
    assert_eq!(
        index.get(4).map(|entry| entry.location()),
        Some(Location::Offset(stream))
    );
    let section = &index.revisions()[0].sections()[0];
    assert_eq!(section.offset(), table);
    assert_eq!(section.kind(), SectionKind::Hybrid { stream: hidden });
    let member = Listing::InUse(Entry {
        generation: 0,
        location: Location::Member {
            stream: 4,
            index: 0,
        },
    });
    assert!(section.listings().contains(&(2, member)));
}

#[test]
fn an_update_replaces_and_frees_objects_and_ends_a_revision() {
    let mut pdf = Pdf::new();
    pdf.object(1, "<< /Type /Catalog /Pages 2 0 R >>");
    pdf.object(2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>");
    pdf.object(3, "<< /Type /Page /Parent 2 0 R /Rotate 0 >>");
    pdf.object(4, "(deleted later)");
    let first = pdf.table(&[(0, 65535)], "<< /Size 5 /Root 1 0 R >>");
    let first_end = pdf.bytes.len();
    pdf.object(3, "<< /Type /Page /Parent 2 0 R /Rotate 90 >>");
    let second = pdf.table(
        &[(4, 1)],
        &format!("<< /Size 5 /Root 1 0 R /Prev {first} >>"),
    );
    let index = open(pdf.bytes.clone());
    assert!(!index.is_repaired(), "{:?}", index.repair());
    assert_eq!(dict(&index, 3).get("Rotate"), Some(&Value::Integer(90)));
    assert_eq!(index.get(4), None);
    assert_eq!(index.len(), 3);
    let [old, new] = index.revisions() else {
        panic!("not two revisions: {:?}", index.revisions());
    };
    assert_eq!(old.sections()[0].offset(), first);
    assert_eq!(old.end(), Some(first_end));
    assert_eq!(new.sections()[0].offset(), second);
    assert_eq!(new.end(), Some(pdf.bytes.len()));
    let listed: Vec<_> = new.sections()[0]
        .listings()
        .iter()
        .map(|&(number, listing)| (number, matches!(listing, Listing::InUse(_))))
        .collect();
    assert_eq!(listed, [(3, true), (4, false)]);
    // The old revision opens on its own, as it was.
    let old = open(pdf.bytes[..first_end].to_vec());
    assert_eq!(dict(&old, 3).get("Rotate"), Some(&Value::Integer(0)));
    assert!(old.get(4).is_some());
}

#[test]
fn a_linearized_file_is_one_revision_of_two_sections() {
    // The first-page section begins the file and its /Prev names the main
    // section at the end, whose `startxref` names the first-page section
    // (ISO 32000-2 Annex F). The first-page trailer's `startxref` gives 0.
    let mut pdf = Pdf::new();
    let linearized = pdf.object(10, "<< /Linearized 1 >>");
    pdf.objects.clear();
    let first = pdf.bytes.len();
    pdf.bytes.extend_from_slice(
        format!(
            "xref\n10 2\n{linearized:010} 00000 n \nCATALOGPOS 00000 n \ntrailer\n<< /Size 12 /Root 11 0 R /Prev MAINPOSXXX >>\nstartxref\n0\n%%EOF\n"
        )
        .as_bytes(),
    );
    let catalog = pdf.object(11, "<< /Type /Catalog /Pages 1 0 R >>");
    pdf.objects.clear();
    pdf.object(1, "<< /Type /Pages /Kids [] /Count 0 >>");
    let main = pdf.bytes.len();
    let pages = pdf.objects[0].1;
    pdf.bytes.extend_from_slice(
        format!(
            "xref\n0 2\n0000000000 65535 f \n{pages:010} 00000 n \ntrailer\n<< /Size 12 >>\nstartxref\n{first}\n%%EOF\n"
        )
        .as_bytes(),
    );
    let bytes = replace(
        &pdf.bytes,
        b"CATALOGPOS",
        format!("{catalog:010}").as_bytes(),
    );
    let bytes = replace(&bytes, b"MAINPOSXXX", format!("{main:010}").as_bytes());
    let index = open(bytes.clone());
    assert!(!index.is_repaired(), "{:?}", index.repair());
    let numbers: Vec<u32> = index.entries().map(|(number, _)| number).collect();
    assert_eq!(numbers, [1, 10, 11]);
    let [revision] = index.revisions() else {
        panic!("not one revision: {:?}", index.revisions());
    };
    let offsets: Vec<_> = revision.sections().iter().map(Section::offset).collect();
    assert_eq!(offsets, [first, main]);
    assert_eq!(revision.end(), Some(bytes.len()));
}

#[test]
fn offsets_count_from_a_header_after_a_transport_prefix() {
    let prefix = b"Content-Type: application/pdf\r\n\r\n";
    let index = open([&prefix[..], &simple()].concat());
    assert!(!index.is_repaired(), "{:?}", index.repair());
    assert_eq!(index.origin(), prefix.len());
    assert_eq!(
        index.get(1).map(|entry| entry.location()),
        Some(Location::Offset(prefix.len() + 9))
    );
    assert_eq!(dict(&index, 3).get("Type"), Some(&name("Page")));
}

#[test]
fn an_entry_that_misses_its_header_rebuilds_the_index() {
    // pylopdf's test_unreachable_page_tree_rebuilds_the_xref_table: the
    // entry of the catalog or of the root page-tree node points past the
    // end of the file, or into its own header.
    let bytes = simple();
    for number in [1, 2] {
        let header = position(&bytes, format!("\n{number} 0 obj").as_bytes()) + 1;
        for wrong in [4_294_967_295, header + 2] {
            let damaged = replace(
                &bytes,
                format!("{number} 1\n{header:010}").as_bytes(),
                format!("{number} 1\n{wrong:010}").as_bytes(),
            );
            let index = open(damaged.clone());
            assert_eq!(
                damage(&index),
                &Damage::Entries {
                    first: number,
                    count: 1
                }
            );
            assert_eq!(
                index.get(number).map(|entry| entry.location()),
                Some(Location::Offset(header))
            );
            assert!(index.revisions().is_empty());
            let trailer = position(&damaged, b"trailer\n") + 8;
            assert_eq!(index.repair().unwrap().trailer(), Some(trailer));
            assert_eq!(dict(&index, 3).get("Parent"), Some(&reference(2)));
        }
    }
}

#[test]
fn an_entry_that_names_another_object_resolves_the_same_in_any_order() {
    // pylopdf's object_recovery.rs: the entry of object 2 points to the
    // header of object 3. hayro-syntax 0.8 reads object 2 as null until it
    // reads that entry, and then rebuilds its table, so what it resolves
    // depends on what it read first; the index checks every entry when it
    // opens.
    let bytes = simple();
    let pages = position(&bytes, b"\n2 0 obj") + 1;
    let third = position(&bytes, b"\n3 0 obj") + 1;
    let damaged = replace(
        &bytes,
        format!("2 1\n{pages:010}").as_bytes(),
        format!("2 1\n{third:010}").as_bytes(),
    );
    let index = open(damaged.clone());
    assert_eq!(damage(&index), &Damage::Entries { first: 2, count: 1 });
    assert_eq!(dict(&index, 2).get("Type"), Some(&name("Pages")));
    assert_order_free(&damaged);
}

#[test]
fn unreadable_cross_reference_data_rebuilds_the_index() {
    let bytes = simple();
    let table = position(&bytes, b"xref\n0 1\n");
    let startxref = format!("startxref\n{table}");
    let moved = |value: usize| {
        replace(
            &bytes,
            startxref.as_bytes(),
            format!("startxref\n{value}").as_bytes(),
        )
    };
    let cases = [
        (
            replace(&bytes, b"startxref", b"startxrex"),
            "the file has no `startxref`",
        ),
        (
            replace(&bytes, startxref.as_bytes(), b"startxref\n"),
            "no offset follows the last `startxref`",
        ),
        // pylopdf's test_incorrect_startxref_value_recovers_exactly, where
        // lopdf moves the offset to a nearby table.
        (
            moved(table + 12),
            "neither `xref` nor an object header begins there",
        ),
        // The header is a comment, which the first object follows.
        (
            moved(0),
            "the cross-reference section at byte 0: the object is not a stream",
        ),
        (
            replace(&bytes, b"trailer", b"trailor"),
            "no `trailer` follows the table",
        ),
        (
            replace(&bytes, b"0000000000 65535 f", b"0000000000 65535 x"),
            "the table's entry of object 0 cannot be read",
        ),
        (
            replace(&bytes, b"/Root 1 0 R", b"/Root 9 0 R"),
            "the trailer names no catalog that the entries locate",
        ),
    ];
    for (damaged, problem) in cases {
        let index = open(damaged);
        let found = damage(&index).to_string();
        assert!(found.contains(problem), "{found}");
        assert_eq!(
            dict(&index, 3).get("Parent"),
            Some(&reference(2)),
            "{found}"
        );
        assert_eq!(index.trailer().get("Root"), Some(&reference(1)), "{found}");
    }
}

#[test]
fn a_chain_of_sections_that_returns_to_a_section_is_unreadable() {
    let bytes = simple();
    let table = position(&bytes, b"xref\n0 1\n");
    let damaged = replace(
        &bytes,
        b"/Root 1 0 R >>",
        format!("/Root 1 0 R /Prev {table} >>").as_bytes(),
    );
    let index = open(damaged);
    assert!(damage(&index).to_string().contains("returns to it"));
    assert_eq!(index.len(), 3);
}

#[test]
fn objects_after_the_last_startxref_rebuild_the_index_from_their_latest_copies() {
    // pylopdf's test_rebuilt_xref_prefers_later_revision_objects: an update
    // whose cross-reference stream gives no offset of its own, after which
    // the file's last `startxref` gives 0.
    let content = |text: &str| {
        let operators = format!("BT /F1 12 Tf 10 50 Td ({text}) Tj ET");
        format!(
            "<< /Length {} >>\nstream\n{operators}\nendstream",
            operators.len()
        )
    };
    let mut pdf = Pdf::new();
    pdf.object(1, "<< /Type /Catalog /Pages 2 0 R >>");
    pdf.object(2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>");
    pdf.object(3, "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>");
    pdf.object(5, &content("Earlier revision"));
    pdf.table(&[(0, 65535)], "<< /Size 6 /Root 1 0 R >>");
    let update = format!(
        "\n5 0 obj\n{}\nendobj\n6 0 obj\n<< /Type /XRef /Size 7 /Root 1 0 R /Length 0 >>\nstream\n\nendstream\nendobj\nstartxref\n0\n%%EOF\n",
        content("Later revision")
    );
    pdf.bytes.extend_from_slice(update.as_bytes());
    // The update's `startxref` names no section, and when it is misspelled,
    // the first revision's `startxref` is the file's last.
    let misspelled = replace(&pdf.bytes, b"startxref\n0\n", b"startxrex\n0\n");
    for (bytes, problem) in [
        (pdf.bytes.clone(), "the cross-reference section at byte 0"),
        (misspelled, "follows the last `startxref`"),
    ] {
        let index = open(bytes);
        let found = damage(&index).to_string();
        assert!(found.contains(problem), "{found}");
        let Some(Value::Stream(stream)) = value(&index, 5) else {
            panic!("object 5 is not a stream");
        };
        assert!(
            stream.data().ends_with(b"(Later revision) Tj ET"),
            "{found}"
        );
    }
}

#[test]
fn the_scan_skips_stream_data_whose_length_is_right_and_no_other() {
    // A file embedded in a stream lends the rebuilt index none of its
    // objects, while a stream whose length is wrong hides no object after
    // it.
    let embedded = "9 0 obj\n<< /Embedded true >>\nendobj\n";
    let mut pdf = Pdf::new();
    pdf.object(1, "<< /Type /Catalog /Pages 2 0 R >>");
    pdf.object(2, "<< /Type /Pages /Kids [] /Count 0 >>");
    pdf.object(
        7,
        &format!(
            "<< /Length {} >>\nstream\n{embedded}\nendstream",
            embedded.len()
        ),
    );
    pdf.object(8, "<< /Length 999 >>\nstream\nshort\nendstream");
    pdf.object(10, "(after the short stream)");
    pdf.bytes.extend_from_slice(b"%%EOF\n");
    let index = open(pdf.bytes);
    assert_eq!(
        damage(&index),
        &Damage::Unreadable("the file has no `startxref`".into())
    );
    assert_eq!(index.get(9), None);
    assert!(index.get(10).is_some());
    assert_eq!(index.trailer().get("Root"), Some(&reference(1)));
    assert_eq!(index.repair().unwrap().trailer(), None);
}

#[test]
fn the_scan_keeps_the_last_header_whose_object_parses() {
    let mut pdf = Pdf::new();
    pdf.object(1, "<< /Type /Catalog /Pages 2 0 R >>");
    pdf.object(2, "<< /Type /Pages /Kids [] /Count 0 >>");
    pdf.bytes.extend_from_slice(b"2 0 obj\n)\n");
    let index = open(pdf.bytes.clone());
    assert_eq!(dict(&index, 2).get("Count"), Some(&Value::Integer(0)));
    // When none parses, the last header wins.
    pdf.bytes.extend_from_slice(b"3 0 obj\n)\n3 0 obj\n>\n");
    let last = pdf.bytes.len() - b"3 0 obj\n>\n".len();
    let index = open(pdf.bytes);
    assert_eq!(
        index.get(3).map(|entry| entry.location()),
        Some(Location::Offset(last))
    );
}

#[test]
fn a_rebuild_without_a_trailer_names_the_catalog_that_it_finds() {
    let mut pdf = Pdf::new();
    pdf.object(1, "<< /Type /Pages /Kids [] /Count 0 >>");
    pdf.object(2, "<< /Type /Catalog /Pages 1 0 R >>");
    let index = open(pdf.bytes.clone());
    assert_eq!(index.trailer().get("Root"), Some(&reference(2)));
    assert_eq!(index.repair().unwrap().trailer(), None);
    // Without a catalog the trailer is empty, and the index still locates
    // the objects.
    let index = open(replace(&pdf.bytes, b"/Type /Catalog", b"/Type /Other  "));
    assert!(index.trailer().is_empty());
    assert_eq!(index.len(), 2);
    // Without an object header, opening refuses.
    let error = Index::open(
        Bytes::from(&b"%PDF-1.7\nnothing here\n"[..]),
        &Budget::default(),
    )
    .unwrap_err();
    assert!(matches!(error.kind(), ErrorKind::Malformed(_)), "{error}");
}

#[test]
fn a_member_that_the_table_does_not_confirm_names_no_object() {
    // pylopdf's object_recovery.rs: hayro-syntax reads the member at the
    // entry's index whatever number the table gives it there, and lopdf the
    // member that the table gives the entry's number. The index names a
    // member only when both agree, which neither the order of reads nor the
    // other entries change.
    let bytes = compressed(
        &[
            (2, "<< /Type /Pages /Kids [] /Count 0 >>"),
            (3, "(three)"),
            (6, "(six)"),
        ],
        &[
            (2, [2, 4, 0]),
            // The table lists 6 at index 2, and 3 at index 1.
            (3, [2, 4, 2]),
            (6, [2, 4, 1]),
            // Past the table's members.
            (7, [2, 4, 9]),
        ],
        "",
    );
    let index = open(bytes.clone());
    assert!(!index.is_repaired(), "{:?}", index.repair());
    assert_eq!(value(&index, 3), None);
    assert_eq!(value(&index, 6), None);
    assert_eq!(value(&index, 7), None);
    assert_eq!(dict(&index, 2).get("Count"), Some(&Value::Integer(0)));
    let objects = index.object_stream(4, &Budget::default()).unwrap().unwrap();
    assert_eq!(objects.mismatched(), [3, 6, 7]);
    assert_order_free(&bytes);
}

#[test]
fn an_object_stream_whose_length_is_a_reference_keeps_all_of_its_data() {
    // hayro-syntax cannot resolve the reference, and reads the data to the
    // first `endstream` without the white space before it; the index
    // resolves the length, which keeps that white space.
    let table = "2 0 ";
    let data = "<< /Type /Pages /Kids [] /Count 0 >>\n\n  ";
    let mut pdf = Pdf::new();
    let catalog = pdf.object(1, "<< /Type /Catalog /Pages 2 0 R >>");
    let stream = pdf.object(
        4,
        &format!(
            "<< /Type /ObjStm /N 1 /First {} /Length 6 0 R >>\nstream\n{table}{data}\nendstream",
            table.len()
        ),
    );
    let length = pdf.object(6, &(table.len() + data.len()).to_string());
    let xref = pdf.bytes.len();
    pdf.xref_stream(
        5,
        &[
            (1, [1, catalog, 0]),
            (2, [2, 4, 0]),
            (4, [1, stream, 0]),
            (5, [1, xref, 0]),
            (6, [1, length, 0]),
        ],
        "/Size 7 /Root 1 0 R",
    );
    let index = open(pdf.bytes);
    assert!(!index.is_repaired(), "{:?}", index.repair());
    let objects = index.object_stream(4, &Budget::default()).unwrap().unwrap();
    assert_eq!(
        objects.data().as_slice(),
        format!("{table}{data}").as_bytes()
    );
    assert_eq!(dict(&index, 2).get("Type"), Some(&name("Pages")));
}

#[test]
fn an_object_stream_that_cannot_be_read_keeps_its_error() {
    let mut pdf = Pdf::new();
    let catalog = pdf.object(1, "<< /Type /Catalog /Pages 2 0 R >>");
    let stream = pdf.object(
        4,
        "<< /Type /ObjStm /N 3 /First 4 /Length 8 >>\nstream\n2 0 3 x \nendstream",
    );
    let xref = pdf.bytes.len();
    pdf.xref_stream(
        5,
        &[
            (1, [1, catalog, 0]),
            (2, [2, 4, 0]),
            (4, [1, stream, 0]),
            (5, [1, xref, 0]),
        ],
        "/Size 6 /Root 1 0 R",
    );
    let index = open(pdf.bytes);
    for _ in 0..2 {
        let error = index
            .object(Reference::new(2, 0), &Budget::default())
            .unwrap_err();
        assert_eq!(
            error.to_string(),
            "object stream 4: its member table ends early"
        );
    }
}

#[test]
fn a_refusal_while_decoding_an_object_stream_is_not_kept() {
    let bytes = compressed(
        &[(2, "<< /Type /Pages /Kids [] /Count 0 >>")],
        &[(2, [2, 4, 0])],
        "",
    );
    let index = open(bytes);
    let tight = Budget::default().with_limit(LimitCode::DecompressedSize, Some(4));
    let error = index.object(Reference::new(2, 0), &tight).unwrap_err();
    assert_eq!(
        error.limit().map(LimitExceeded::code),
        Some(LimitCode::DecompressedSize)
    );
    assert_eq!(dict(&index, 2).get("Type"), Some(&name("Pages")));
}

#[test]
fn an_encrypted_document_does_not_decode_its_object_streams() {
    let bytes = compressed(
        &[(2, "<< /Type /Pages /Kids [] /Count 0 >>")],
        &[(2, [2, 4, 0])],
        "/Encrypt << /Filter /Standard >>",
    );
    let index = open(bytes);
    assert!(index.is_encrypted());
    let error = index
        .object(Reference::new(2, 0), &Budget::default())
        .unwrap_err();
    assert_eq!(error.kind(), &ErrorKind::Password(Password::Required));
    assert!(dict(&index, 1).get("Pages").is_some());
}

#[test]
fn the_scan_stays_linear_on_hostile_input() {
    // pylopdf's test_unterminated_stream_keywords_keep_recovery_linear and
    // test_repeated_xref_keywords_fail_without_quadratic_scanning.
    let trailer: &[u8] = b"trailer\n<</Root 1 0 R>>\nstartxref\n0\n%%EOF\n";
    let rebuilt = [
        &b"%PDF-1.4\n"[..],
        &b"1 0 obj\n<</Length 5>>stream\n".repeat(300_000),
        trailer,
    ]
    .concat();
    let rejected = [&b"%PDF-1.4\n"[..], &b"stream\n".repeat(1_200_000), trailer].concat();
    let hostile = [
        &b"%PDF-1.4\n"[..],
        &b"xref".repeat(262_144),
        b"\ntrailer\n<< >>\nstartxref\n0\n%%EOF\n",
    ]
    .concat();
    let start = Instant::now();
    let index = open(rebuilt);
    assert!(index.is_repaired());
    assert_eq!(index.len(), 1);
    for input in [rejected, hostile] {
        let error = Index::open(Bytes::from(input), &Budget::default()).unwrap_err();
        assert!(matches!(error.kind(), ErrorKind::Malformed(_)), "{error}");
    }
    let elapsed = start.elapsed();
    // pylopdf bounds these inputs at 2 seconds in its release build; an
    // unoptimized test build takes several times as long.
    let bound = if cfg!(debug_assertions) { 20 } else { 2 };
    assert!(elapsed < Duration::from_secs(bound), "{elapsed:?}");
}

#[test]
fn the_budget_bounds_opening() {
    let refusal = |bytes: &[u8], budget: Budget| {
        Index::open(Bytes::from(bytes.to_vec()), &budget)
            .unwrap_err()
            .limit()
            .map(LimitExceeded::code)
    };
    let bytes = simple();
    let few = || Budget::default().with_limit(LimitCode::ObjectCount, Some(2));
    assert_eq!(refusal(&bytes, few()), Some(LimitCode::ObjectCount));
    // pylopdf's test_recovered_documents_keep_document_limits: a rebuilt
    // index counts its objects too.
    let rebuilt = replace(&bytes, b"startxref", b"startxrex");
    assert!(open(rebuilt.clone()).is_repaired());
    assert_eq!(refusal(&rebuilt, few()), Some(LimitCode::ObjectCount));
    let small = Budget::default().with_limit(LimitCode::FileSize, Some(10));
    assert_eq!(refusal(&bytes, small), Some(LimitCode::FileSize));
    let memory = Budget::default().with_limit(LimitCode::MemorySize, Some(64));
    assert_eq!(refusal(&bytes, memory), Some(LimitCode::MemorySize));
    let token = CancellationToken::new();
    token.cancel();
    let cancelled = Budget::default().with_cancellation(token);
    assert_eq!(refusal(&bytes, cancelled), Some(LimitCode::Cancelled));
}

#[test]
fn the_index_is_shared_between_threads() {
    const fn shared<T: Send + Sync>() {}
    shared::<Index>();
}

/// The real-document corpus (tests/assets/real_world): each file's name and
/// bytes.
fn corpus() -> Vec<(String, Vec<u8>)> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/assets/real_world");
    let mut files: Vec<PathBuf> = fs::read_dir(&dir)
        .unwrap_or_else(|error| panic!("{}: {error}", dir.display()))
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.extension().is_some_and(|extension| extension == "pdf"))
        .collect();
    files.sort();
    assert_eq!(files.len(), 14, "the corpus has 14 files");
    files
        .into_iter()
        .map(|path| {
            let name = path.file_name().unwrap().to_string_lossy().into_owned();
            (name, fs::read(&path).unwrap())
        })
        .collect()
}

#[test]
fn every_corpus_file_indexes_without_repair_and_every_entry_parses() {
    for (file, bytes) in corpus() {
        let pdf = hayro_syntax::Pdf::new(bytes.clone()).unwrap();
        let index = open(bytes);
        assert!(!index.is_repaired(), "{file}: {:?}", index.repair());
        let budget = Budget::default();
        for (number, entry) in index.entries() {
            let reference = Reference::new(number, entry.generation());
            let bytes = index
                .object(reference, &budget)
                .unwrap_or_else(|error| panic!("{file}, object {number}: {error}"))
                .unwrap_or_else(|| panic!("{file}, object {number} is not there"));
            // hayro-syntax parses the object from the bytes that the index
            // gives, as the store's values do, and its own reader resolves
            // the same number.
            assert!(
                MaybeRef::<Object<'_>>::from_bytes(&bytes).is_some(),
                "{file}, object {number}"
            );
            Value::parse(&bytes, &budget)
                .unwrap_or_else(|error| panic!("{file}, object {number}: {error}"));
            let id = ObjectIdentifier::new(
                i32::try_from(number).unwrap(),
                i32::from(entry.generation()),
            );
            assert!(
                pdf.xref().get::<Object<'_>>(id).is_some(),
                "{file}, object {number}"
            );
        }
    }
}

/// The revisions of a file: the offsets of each revision's sections, and
/// where the revision ends.
type Revisions<'a> = &'a [(&'a [usize], usize)];

#[test]
fn the_revisions_of_updated_corpus_files_are_listed_with_their_offsets() {
    // A linearized file's first revision ends where its /L says that the
    // file ends.
    let updated: &[(&str, Revisions<'_>)] = &[
        (
            "f1040.pdf",
            &[(&[116, 205_530], 206_144), (&[219_885], 220_237)],
        ),
        (
            "patent-us223898.pdf",
            &[(&[230_864], 232_285), (&[320_614], 321_048)],
        ),
        ("wdl6812-manuscript.pdf", &[(&[110, 377_532], 377_877)]),
    ];
    for (file, bytes) in corpus() {
        let index = open(bytes.clone());
        let revisions: Vec<(Vec<usize>, Option<usize>)> = index
            .revisions()
            .iter()
            .map(|revision| {
                let offsets = revision.sections().iter().map(Section::offset).collect();
                (offsets, revision.end())
            })
            .collect();
        let expected: Vec<(Vec<usize>, Option<usize>)> =
            match updated.iter().find(|(name, _)| *name == file) {
                Some((_, revisions)) => revisions
                    .iter()
                    .map(|(offsets, end)| (offsets.to_vec(), Some(*end)))
                    .collect(),
                None => {
                    let at = lex::last_startxref(&bytes).unwrap();
                    let offset = lex::startxref(&bytes, at).unwrap().offset;
                    vec![(vec![usize::try_from(offset).unwrap()], Some(bytes.len()))]
                }
            };
        assert_eq!(revisions, expected, "{file}");
    }
}

#[test]
fn a_rebuild_finds_every_object_that_the_corpus_cross_reference_data_locates() {
    // With its last `startxref` misspelled, every file is rebuilt by the
    // scan, which finds the value of every object that the intact index
    // locates.
    for (file, bytes) in corpus() {
        let intact = open(bytes.clone());
        let at = bytes
            .windows(9)
            .rposition(|window| window == b"startxref")
            .unwrap();
        let mut damaged = bytes;
        damaged[at + 8] = b'x';
        let rebuilt = open(damaged);
        assert!(rebuilt.is_repaired(), "{file}");
        for (number, _) in intact.entries() {
            assert_eq!(
                value(&rebuilt, number),
                value(&intact, number),
                "{file}, object {number}"
            );
        }
        assert_eq!(
            rebuilt.trailer().get("Root"),
            intact.trailer().get("Root"),
            "{file}"
        );
    }
}

/// A damaged input of the corpus helper.
struct Damaged {
    kind: String,
    file: String,
    /// Whether the kind is one of the 8 of the measurement that DESIGN.md
    /// section 2.2 cites.
    measured: bool,
    bytes: Vec<u8>,
}

/// The Python that writes every damaged input of tests/corpus.py, after
/// checking it against damage.sha256, to standard output: a line of its
/// kind, its file, its length, and whether it is measured, then its bytes.
const DAMAGED_INPUTS: &str = r#"
import hashlib
import sys

sys.path.insert(0, "tests")
from corpus import ASSETS, DAMAGE, MEASURED, NAMES, damaged

lines = (ASSETS / "damage.sha256").read_text().splitlines()
recorded = {path: digest for digest, path in (line.split() for line in lines if line)}
for kind in DAMAGE:
    for name in NAMES:
        data = damaged(name, kind)
        if hashlib.sha256(data).hexdigest() != recorded[f"{kind}/{name}"]:
            sys.exit(f"{kind}/{name} differs from damage.sha256")
        sys.stdout.buffer.write(f"{kind} {name} {len(data)} {int(kind in MEASURED)}\n".encode())
        sys.stdout.buffer.write(data)
"#;

/// Returns every damaged input of the corpus helper, which the Python
/// interpreter that `PYTHON` names, or `python3`, writes.
fn damaged_inputs() -> Vec<Damaged> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let python = std::env::var_os("PYTHON").unwrap_or_else(|| "python3".into());
    let output = Command::new(&python)
        .args(["-I", "-c", DAMAGED_INPUTS])
        .current_dir(&root)
        .output()
        .unwrap_or_else(|error| panic!("{}: {error}", python.to_string_lossy()));
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let mut inputs = Vec::new();
    let mut rest = output.stdout.as_slice();
    while let Some(line) = rest.iter().position(|&byte| byte == b'\n') {
        let header = std::str::from_utf8(&rest[..line]).unwrap();
        let [kind, file, len, measured] = header.split(' ').collect::<Vec<_>>()[..] else {
            panic!("{header}");
        };
        let end = line + 1 + len.parse::<usize>().unwrap();
        inputs.push(Damaged {
            kind: kind.into(),
            file: file.into(),
            measured: measured == "1",
            bytes: rest[line + 1..end].to_vec(),
        });
        rest = &rest[end..];
    }
    assert!(rest.is_empty());
    assert_eq!(inputs.len(), 14 * 9);
    inputs
}

/// Returns the value of the object that `value` refers to, if the index
/// locates it and it parses.
fn resolve(index: &Index, value: Option<&Value>, budget: &Budget) -> Option<Value> {
    let Some(&Value::Reference(reference)) = value else {
        return None;
    };
    Value::parse(&index.object(reference, budget).ok()??, budget).ok()
}

/// Whether the first page of the document that `index` locates resolves.
///
/// The trailer's /Root names a catalog whose /Pages leads, through the
/// first kid of each node that resolves, to a dictionary that is not a
/// /Pages node, as hayro-syntax 0.8 reads the page tree. When the tree
/// yields no page, the first page is the first object in the order of the
/// bytes whose dictionary has /Type /Page, as hayro-syntax then searches
/// for pages.
fn first_page(index: &Index, budget: &Budget) -> bool {
    let pages = name("Pages");
    if let Some(Value::Dict(catalog)) = resolve(index, index.trailer().get("Root"), budget)
        && let Some(Value::Dict(mut node)) = resolve(index, catalog.get("Pages"), budget)
    {
        for _ in 0..64 {
            if node.get("Type") != Some(&pages) {
                return true;
            }
            let Some(Value::Array(kids)) = node.get("Kids") else {
                break;
            };
            let Some(kid) = kids
                .iter()
                .find_map(|kid| match resolve(index, Some(kid), budget) {
                    Some(Value::Dict(kid)) => Some(kid),
                    _ => None,
                })
            else {
                break;
            };
            node = kid;
        }
    }
    let place = |entry: Entry| match entry.location() {
        Location::Offset(pos) => (pos, 0),
        Location::Member { stream, index: at } => {
            let container = match index.get(stream).map(|entry| entry.location()) {
                Some(Location::Offset(pos)) => pos,
                _ => usize::MAX,
            };
            (container, at)
        }
    };
    let mut entries: Vec<_> = index.entries().collect();
    entries.sort_by_key(|&(_, entry)| place(entry));
    let page = name("Page");
    entries.iter().any(|&(number, entry)| {
        let reference = Value::Reference(Reference::new(number, entry.generation()));
        matches!(
            resolve(index, Some(&reference), budget),
            Some(Value::Dict(dict)) if dict.get("Type") == Some(&page)
        )
    })
}

#[test]
fn damaged_inputs_resolve_their_first_page_whatever_order_objects_are_read_in() {
    let mut resolved = 0;
    let mut by_hayro = 0;
    let mut lost = Vec::new();
    for input in damaged_inputs() {
        let budget = Budget::default();
        let opened = || Index::open(Bytes::from(input.bytes.clone()), &budget);
        // Every input opens to the same index and repair report, and its
        // objects resolve the same whatever order they are read in.
        let repair = |index: Result<Index>| index.map(|index| index.repair().cloned());
        assert_eq!(
            repair(opened()),
            repair(opened()),
            "{} {}",
            input.kind,
            input.file
        );
        assert_order_free(&input.bytes);
        if input.measured {
            let page = opened().is_ok_and(|index| first_page(&index, &budget));
            resolved += usize::from(page);
            by_hayro += usize::from(
                hayro_syntax::Pdf::new(input.bytes.clone())
                    .is_ok_and(|pdf| !pdf.pages().is_empty()),
            );
            if !page {
                lost.push(format!("{} {}", input.kind, input.file));
            }
        }
    }
    // pylopdf measured that hayro alone renders 103 of the 112 inputs
    // (DESIGN.md section 2.2). On these bytes, hayro-syntax 0.8 reaches a
    // first page of 101.
    println!("first pages: {resolved} of 112, hayro-syntax {by_hayro}; lost: {lost:?}");
    assert!(resolved >= 103, "{resolved} of 112 resolve; lost: {lost:?}");
}
