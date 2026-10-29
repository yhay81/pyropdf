//! Corpus round trips and the invariants of bounded, lossless saving.

use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;
use std::process::Command;
use std::sync::Arc;

use proptest::prelude::*;

use super::*;
use crate::budget::CancellationToken;
use crate::index::SectionKind;
use crate::value::{Bytes, Name, Stream};
use crate::version::{Base, Document, MemoryBase};

fn corpus() -> Vec<PathBuf> {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/assets/real_world");
    let mut files: Vec<_> = fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|ext| ext == "pdf"))
        .collect();
    files.sort();
    assert_eq!(files.len(), 15);
    files
}

fn open(bytes: Vec<u8>) -> Document {
    Document::new(
        Arc::new(FileBase::open(bytes.into(), &Budget::default()).unwrap()),
        None,
    )
}

fn saved(version: &Version, incremental_mode: bool) -> Vec<u8> {
    let mut bytes = Vec::new();
    let save = if incremental_mode { incremental } else { full };
    let written = save(version, &mut bytes, &Budget::default()).unwrap();
    assert_eq!(written, bytes.len() as u64);
    bytes
}

fn assert_objects(version: &Version, bytes: Vec<u8>) {
    let reread = FileBase::open(bytes.into(), &Budget::default()).unwrap();
    assert_reread(version, &reread);
}

/// Asserts that `reread`, a save of `version`, has every object of it.
fn assert_reread(version: &Version, reread: &FileBase) {
    assert!(
        !reread.index().is_repaired(),
        "{:?}",
        reread.index().repair()
    );
    let budget = Budget::default();
    let mut numbers = BudgetVec::new(&budget);
    version.base().numbers(&mut numbers).unwrap();
    for (number, _) in version.slots() {
        numbers.try_push(number).unwrap();
    }
    for &number in &numbers {
        if number == 0 {
            continue;
        }
        assert_eq!(
            version.generation(number),
            reread.generation(number),
            "object {number}"
        );
        if let Some(generation) = version.generation(number) {
            let reference = Reference::new(number, generation);
            let original = match version.slot(number) {
                Some(Slot::Written(value)) => value.bytes().clone(),
                _ => raw::body(&**version.base(), reference, &budget).unwrap(),
            };
            let restored = raw::body(reread, reference, &budget).unwrap();
            assert_eq!(original, restored, "object {number}: exact value bytes");
            assert_eq!(
                Value::parse(&original, &budget).unwrap(),
                Value::parse(&restored, &budget).unwrap(),
                "object {number}: values"
            );
        }
    }
}

#[test]
fn every_corpus_file_round_trips_without_edits() {
    for path in corpus() {
        eprintln!("{}", path.display());
        let original = fs::read(&path).unwrap();
        let doc = open(original.clone());
        let version = doc.current();
        assert_eq!(saved(&version, true), original);
        assert_objects(&version, saved(&version, false));
    }
}

fn memory() -> Document {
    let trailer = [(Name::from("Root"), Value::Reference(Reference::new(1, 0)))]
        .into_iter()
        .collect();
    let mut base = MemoryBase::new(trailer);
    base.insert(
        Reference::new(1, 0),
        Bytes::from(&b"<< /Type /Catalog /Pages 2 0 R >>"[..]),
    );
    base.insert(
        Reference::new(2, 0),
        Bytes::from(&b"<< /Type /Pages /Count 0 /Kids [] >>"[..]),
    );
    base.insert(
        Reference::new(3, 7),
        Bytes::from(&b"[+001 35.6812345 (literal) <00ff>]"[..]),
    );
    Document::new(Arc::new(base), None)
}

#[test]
fn full_save_opens_memory_documents_and_incremental_preserves_the_prefix() {
    let doc = memory();
    let initial = saved(&doc.current(), false);
    assert_objects(&doc.current(), initial.clone());
    let doc = open(initial.clone());
    let mut transaction = doc.begin();
    transaction
        .write(3, &Value::Integer(42), &Budget::default())
        .unwrap();
    transaction
        .write(20, &Value::Integer(123), &Budget::default())
        .unwrap();
    let version = transaction.commit(&Budget::default()).unwrap();
    for mode in [false, true] {
        let bytes = saved(&version, mode);
        if mode {
            assert!(bytes.starts_with(&initial));
        }
        assert_objects(&version, bytes);
    }
    doc.undo().unwrap();
    assert_eq!(saved(&doc.current(), true), initial);
}

#[test]
fn repaired_memory_and_encrypted_bases_refuse_before_output() {
    let mut output = Vec::new();
    assert!(matches!(
        incremental(&memory().current(), &mut output, &Budget::default()),
        Err(Error::Refused(Refusal::NoOriginalFile))
    ));
    let mut bytes = saved(&memory().current(), false);
    let at = bytes.windows(9).rposition(|w| w == b"startxref").unwrap();
    bytes[at + 8] = b'x';
    let repaired = open(bytes);
    assert!(matches!(
        incremental(&repaired.current(), &mut output, &Budget::default()),
        Err(Error::Refused(Refusal::RepairedBase))
    ));
    assert!(output.is_empty());
    assert_objects(&repaired.current(), saved(&repaired.current(), false));
    let doc = memory();
    let mut transaction = doc.begin();
    let mut trailer = transaction.version().trailer().clone();
    trailer.insert(
        Name::from("Encrypt"),
        Value::Reference(Reference::new(99, 0)),
    );
    transaction
        .set_trailer(trailer, &Budget::default())
        .unwrap();
    let version = transaction.commit(&Budget::default()).unwrap();
    assert!(matches!(
        full(&version, &mut output, &Budget::default()),
        Err(Error::Refused(Refusal::Encryption))
    ));
    assert!(output.is_empty());
}

fn edited(doc: &Document, marker: i64) -> Arc<Version> {
    let budget = Budget::default();
    let mut tx = doc.begin();
    let number = tx.allocate().unwrap().number();
    tx.write(number, &Value::Integer(marker), &budget).unwrap();
    let mut trailer = tx.version().trailer().clone();
    trailer.insert(Name::from("PyropdfTest"), Value::Integer(marker));
    tx.set_trailer(trailer, &budget).unwrap();
    tx.commit(&budget).unwrap()
}

#[test]
fn corpus_edits_preserve_every_other_value_and_the_incremental_prefix() {
    for path in corpus() {
        let original = fs::read(path).unwrap();
        let doc = open(original.clone());
        let version = edited(&doc, 42);
        for mode in [false, true] {
            let output = saved(&version, mode);
            if mode {
                assert!(output.starts_with(&original));
            }
            assert_objects(&version, output);
        }
    }
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(96))]
    #[test]
    fn random_transactions_round_trip(operations in prop::collection::vec((0_u8..6, 3_u32..50, any::<i32>()), 1..40)) {
        let original = saved(&memory().current(), false);
        let doc = open(original.clone());
        let budget = Budget::default();
        for (op, number, value) in operations {
            match op {
                0 => { doc.undo(); }
                1 => { doc.redo(); }
                _ => {
                    let mut tx = doc.begin();
                    if op == 2 { tx.free(number, &budget).unwrap(); }
                    else { tx.write(number, &Value::Integer(i64::from(value)), &budget).unwrap(); }
                    if op != 3 { tx.commit(&budget).unwrap(); }
                }
            }
        }
        let version = doc.current();
        let inc = saved(&version, true);
        prop_assert!(inc.starts_with(&original));
        assert_objects(&version, inc);
        assert_objects(&version, saved(&version, false));
    }
}

#[test]
fn budgets_and_sink_failures_do_not_change_the_version() {
    let doc = open(saved(&memory().current(), false));
    let version = edited(&doc, 7);
    let expected = saved(&version, true);
    for mode in [false, true] {
        let save = if mode { incremental } else { full };
        let complete = saved(&version, mode);
        for limit in [0, 1, complete.len() as u64 - 1, complete.len() as u64] {
            let budget = Budget::default().with_limit(LimitCode::PdfOutputSize, Some(limit));
            let mut bytes = Vec::new();
            let result = save(&version, &mut bytes, &budget);
            assert!(bytes.len() as u64 <= limit);
            if limit == complete.len() as u64 {
                assert_eq!(bytes, complete);
                assert!(result.is_ok());
            } else {
                assert!(
                    matches!(result, Err(Error::Store(ref e)) if e.limit().unwrap().code() == LimitCode::PdfOutputSize)
                );
            }
        }
        let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(0));
        let mut bytes = Vec::new();
        assert!(
            matches!(save(&version, &mut bytes, &budget), Err(Error::Store(ref e)) if e.limit().unwrap().code() == LimitCode::MemorySize)
        );
        let token = CancellationToken::new();
        token.cancel();
        let budget = Budget::default().with_cancellation(token);
        let mut bytes = Vec::new();
        assert!(
            matches!(save(&version, &mut bytes, &budget), Err(Error::Store(ref e)) if e.limit().unwrap().code() == LimitCode::Cancelled)
        );
        assert!(bytes.is_empty());
        let mut sink = FailingSink { left: 33 };
        assert!(
            matches!(save(&version, &mut sink, &Budget::default()), Err(Error::Io(ref e)) if e.kind() == io::ErrorKind::BrokenPipe)
        );
        assert!(Arc::ptr_eq(&version, &doc.current()));
        assert_eq!(saved(&version, true), expected);
    }
}

struct FailingSink {
    left: usize,
}

impl Write for FailingSink {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if self.left == 0 {
            return Err(io::Error::new(io::ErrorKind::BrokenPipe, "test failure"));
        }
        let n = self.left.min(bytes.len()).min(3);
        self.left -= n;
        Ok(n)
    }
    fn flush(&mut self) -> io::Result<()> {
        panic!("the caller owns flushing")
    }
}

#[test]
fn no_edit_incremental_needs_no_object_or_metadata_allocation() {
    let bytes = saved(&memory().current(), false);
    let doc = open(bytes.clone());
    let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(0));
    let mut out = Vec::new();
    incremental(&doc.current(), &mut out, &budget).unwrap();
    assert_eq!(out, bytes);
}

#[test]
fn prefixed_files_and_chains_use_offsets_relative_to_the_pdf_header() {
    let original = saved(&memory().current(), false);
    let bytes = [b"prefix junk\n".as_slice(), &original].concat();
    let first = open(bytes.clone());
    let update = saved(&edited(&first, 1), true);
    assert!(update.starts_with(&bytes));
    let second = open(update.clone());
    assert_eq!(second.current().base().file().unwrap().revisions().len(), 2);
    let version = edited(&second, 2);
    let next = saved(&version, true);
    assert!(next.starts_with(&update));
    assert_objects(&version, next.clone());
    assert_eq!(
        open(next)
            .current()
            .base()
            .file()
            .unwrap()
            .revisions()
            .len(),
        3
    );
    assert_objects(&version, saved(&version, false));
}

#[test]
fn trailer_only_changes_append_a_revision_and_undo_restores_exact_bytes() {
    let original = saved(&memory().current(), false);
    let doc = open(original.clone());
    let mut tx = doc.begin();
    let mut trailer = tx.version().trailer().clone();
    trailer.insert(Name::from("CustomTrailer"), Value::Integer(321));
    tx.set_trailer(trailer, &Budget::default()).unwrap();
    let version = tx.commit(&Budget::default()).unwrap();
    assert!(version.slots().next().is_none());
    assert!(version.has_changes());
    let updated = saved(&version, true);
    assert!(updated.starts_with(&original));
    let reread = open(updated);
    assert_eq!(
        reread.current().trailer().get("CustomTrailer"),
        Some(&Value::Integer(321))
    );
    assert_objects(&version, saved(&version, false));
    doc.undo();
    assert_eq!(saved(&doc.current(), true), original);
}

#[test]
fn indirect_stream_lengths_preserve_embedded_keywords_and_trailing_bytes() {
    let data = b"a\nendstream\nendobj\nb  \r\n";
    let doc = memory();
    let initial = saved(&doc.current(), false);
    // Build a file whose untouched stream has an indirect length. The
    // regular object writer intentionally normalizes lengths on edits.
    let mut bytes = initial.clone();
    let previous =
        open(initial).current().base().file().unwrap().revisions()[0].sections()[0].offset();
    let stream_offset = bytes.len();
    bytes.extend_from_slice(b"4 0 obj\n<< /Length 5 0 R >>\nstream\n");
    bytes.extend_from_slice(data);
    bytes.extend_from_slice(b"\nendstream\nendobj\n");
    let length_offset = bytes.len();
    bytes.extend_from_slice(format!("5 0 obj\n{}\nendobj\n", data.len()).as_bytes());
    let xref = bytes.len();
    bytes.extend_from_slice(format!("xref\n4 2\n{stream_offset:010} 00000 n \n{length_offset:010} 00000 n \ntrailer\n<< /Root 1 0 R /Size 6 /Prev {previous} >>\nstartxref\n{xref}\n%%EOF\n").as_bytes());
    let doc = open(bytes);
    let full = saved(&doc.current(), false);
    assert!(full.windows(data.len()).any(|w| w == data));
    assert_objects(&doc.current(), full);
}

#[test]
fn free_generations_survive_reopening_and_sparse_numbers_stay_bounded() {
    let doc = open(saved(&memory().current(), false));
    let mut tx = doc.begin();
    tx.free(3, &Budget::default()).unwrap();
    tx.write(u32::MAX, &Value::Integer(12), &Budget::default())
        .unwrap();
    let version = tx.commit(&Budget::default()).unwrap();
    for mode in [false, true] {
        let bytes = saved(&version, mode);
        assert!(bytes.len() < 2000, "do not materialize billions of gaps");
        let reopened = open(bytes);
        assert_eq!(reopened.current().base().free_generation(3), Some(8));
        assert_eq!(reopened.current().next_number(), u64::from(u32::MAX) + 1);
        let mut tx = reopened.begin();
        assert_eq!(
            tx.write(3, &Value::Integer(9), &Budget::default())
                .unwrap()
                .generation(),
            8
        );
    }
}

fn qpdf_warnings(path: &std::path::Path) -> BTreeMap<String, usize> {
    let result = Command::new("qpdf")
        .arg("--check")
        .arg(path)
        .output()
        .expect("install qpdf for this development-only check");
    let stderr = String::from_utf8_lossy(&result.stderr);
    assert!(
        matches!(result.status.code(), Some(0 | 3)),
        "{}: {stderr}",
        path.display()
    );
    let mut warnings = BTreeMap::new();
    for line in stderr.lines().filter(|line| line.starts_with("WARNING:")) {
        let message = line.rsplit_once(": ").unwrap().1.to_owned();
        *warnings.entry(message).or_insert(0) += 1;
    }
    warnings
}

#[test]
#[ignore = "development-only acceptance check: requires the qpdf executable"]
fn qpdf_accepts_corpus_outputs_without_new_warnings() {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../target/save-validation");
    fs::create_dir_all(&dir).unwrap();
    for path in corpus() {
        let baseline = qpdf_warnings(&path);
        let original = fs::read(&path).unwrap();
        let doc = open(original);
        for edits in [false, true] {
            let version = if edits {
                edited(&doc, 27)
            } else {
                doc.current()
            };
            for incremental_mode in [false, true] {
                let name = format!(
                    "{}-{}-{}",
                    u8::from(edits),
                    u8::from(incremental_mode),
                    path.file_name().unwrap().to_str().unwrap()
                );
                let output = dir.join(&name);
                // Exercise a file sink directly; there is no output Vec.
                let mut file = fs::File::create(&output).unwrap();
                if incremental_mode {
                    incremental(&version, &mut file, &Budget::default()).unwrap();
                } else {
                    full(&version, &mut file, &Budget::default()).unwrap();
                }
                drop(file);
                let warnings = qpdf_warnings(&output);
                for (message, count) in &warnings {
                    assert!(
                        *count <= baseline.get(message).copied().unwrap_or(0),
                        "{name}: new warning ({count}): {message}"
                    );
                }
                eprintln!(
                    "{name}: no errors; {} existing warnings",
                    warnings.values().sum::<usize>()
                );
            }
        }
    }
    // Also validate deletions and reusing a free number in an incremental
    // chain; corpus edits above deliberately keep their reference graphs.
    let source = corpus()
        .into_iter()
        .find(|p| p.file_name().unwrap() == "pdf20-simple.pdf")
        .unwrap();
    let mut doc = open(fs::read(source).unwrap());
    let mut tx = doc.begin();
    let number = tx.allocate().unwrap().number();
    tx.write(number, &Value::Integer(-1), &Budget::default())
        .unwrap();
    let version = tx.commit(&Budget::default()).unwrap();
    doc = open(saved(&version, true));
    for step in 0..4 {
        let mut tx = doc.begin();
        if step % 2 == 0 {
            tx.free(number, &Budget::default()).unwrap();
        } else {
            tx.write(number, &Value::Integer(step), &Budget::default())
                .unwrap();
        }
        let version = tx.commit(&Budget::default()).unwrap();
        for mode in [false, true] {
            let output = dir.join(format!("free-reuse-{step}-{mode}.pdf"));
            fs::write(&output, saved(&version, mode)).unwrap();
            assert!(qpdf_warnings(&output).is_empty());
        }
        doc = open(saved(&version, true));
    }
}

fn dict(entries: &[(&str, Value)]) -> Value {
    Value::Dict(
        entries
            .iter()
            .map(|(name, value)| (Name::from(*name), value.clone()))
            .collect(),
    )
}

fn to(number: u32) -> Value {
    Value::Reference(Reference::new(number, 0))
}

#[test]
fn pruning_keeps_what_the_trailer_reaches_through_cycles_and_deep_chains() {
    let budget = Budget::default();
    let doc = open(saved(&memory().current(), false));
    let mut tx = doc.begin();
    let catalog = dict(&[
        ("Type", Value::Name(Name::from("Catalog"))),
        ("Pages", to(2)),
        ("Cycle", to(10)),
        ("Chain", to(100)),
    ]);
    tx.write(1, &catalog, &budget).unwrap();
    tx.write(10, &dict(&[("Next", to(11))]), &budget).unwrap();
    tx.write(11, &dict(&[("Next", to(10))]), &budget).unwrap();
    let last = 100 + 5_000;
    for number in 100..last {
        tx.write(number, &dict(&[("Next", to(number + 1))]), &budget)
            .unwrap();
    }
    tx.write(last, &Value::Integer(1), &budget).unwrap();
    // Object 3 and the orphan that refers to it are unreachable.
    tx.write(20, &dict(&[("Orphan", to(3))]), &budget).unwrap();
    let version = tx.commit(&budget).unwrap();

    let mut bytes = Vec::new();
    let options = Options {
        prune: true,
        ..Options::default()
    };
    full_with(&version, &mut bytes, &budget, None, options).unwrap();
    let reopened = open(bytes).current();
    for number in [3, 20] {
        assert_eq!(reopened.generation(number), None, "object {number}");
    }
    assert_eq!(reopened.base().free_generation(3), Some(8));
    for number in [1, 2, 10, 11, 100, 2_600, last] {
        assert_eq!(
            reopened.get(number, &budget).unwrap().unwrap().value(),
            version.get(number, &budget).unwrap().unwrap().value(),
            "object {number}"
        );
    }

    let mut plain = Vec::new();
    full_with(&version, &mut plain, &budget, None, Options::default()).unwrap();
    assert_eq!(open(plain).current().generation(20), Some(0));
}

#[test]
fn pruning_stops_when_the_operation_is_cancelled() {
    let version = memory().current();
    let cancelled = CancellationToken::new();
    cancelled.cancel();
    let budget = Budget::default().with_cancellation(cancelled);
    let options = Options {
        prune: true,
        ..Options::default()
    };
    assert!(full_with(&version, &mut Vec::new(), &budget, None, options).is_err());
}

fn stream(entries: &[(&str, Value)], data: &[u8]) -> Stream {
    let Value::Dict(dict) = dict(entries) else {
        unreachable!()
    };
    Stream::new(dict, Bytes::new(data.to_vec()))
}

fn recoded_stream(stream: &Stream, streams: Streams) -> Option<Stream> {
    let value = Value::Stream(Box::new(stream.clone()));
    match recode::recoded(&value, streams, &Budget::default()).unwrap() {
        Some(Value::Stream(stream)) => Some(*stream),
        Some(other) => panic!("not a stream: {other:?}"),
        None => None,
    }
}

fn name(name: &str) -> Value {
    Value::Name(Name::from(name))
}

#[test]
fn compressing_flates_unfiltered_data_only_where_it_shrinks() {
    let data = b"0 0 m 10 10 l S\n".repeat(200);
    let plain = stream(&[], &data);
    let flated = recoded_stream(&plain, Streams::Compress(9)).unwrap();
    assert_eq!(flated.dict().get("Filter"), Some(&name("FlateDecode")));
    assert!(flated.data().len() < data.len());
    assert_eq!(flated.decoded(&Budget::default()).unwrap(), data);

    // Stored blocks at level 0 are longer than the data.
    assert_eq!(recoded_stream(&plain, Streams::Compress(0)), None);
    for entries in [
        &[("Type", name("Metadata"))][..],
        &[("Filter", name("DCTDecode"))],
        &[("DecodeParms", dict(&[("Predictor", Value::Integer(12))]))],
        &[("F", Value::String(PdfString::literal(&b"data.bin"[..])))],
    ] {
        let kept = stream(entries, &data);
        assert_eq!(
            recoded_stream(&kept, Streams::Compress(9)),
            None,
            "{entries:?}"
        );
    }
    assert_eq!(
        recoded_stream(&stream(&[], b""), Streams::Compress(9)),
        None
    );
}

#[test]
fn decompressing_decodes_the_leading_filters_and_keeps_image_filters() {
    let data = b"BT /F1 12 Tf (Hello) Tj ET\n".repeat(50);
    let flated = recoded_stream(&stream(&[], &data), Streams::Compress(6)).unwrap();
    let plain = recoded_stream(&flated, Streams::Decompress).unwrap();
    assert_eq!(plain.dict().get("Filter"), None);
    assert_eq!(plain.data().as_slice(), &data[..]);

    // The Flate output of the JPEG bytes, with the JPEG filter after it.
    let jpeg = b"\xFF\xD8 not really a JPEG \xFF\xD9".repeat(20);
    let parameters = dict(&[("ColorTransform", Value::Integer(0))]);
    let chained = stream(
        &[
            (
                "Filter",
                Value::Array(vec![name("FlateDecode"), name("DCTDecode")].into()),
            ),
            (
                "DecodeParms",
                Value::Array(vec![Value::Null, parameters.clone()].into()),
            ),
        ],
        recoded_stream(&stream(&[], &jpeg), Streams::Compress(6))
            .unwrap()
            .data(),
    );
    let image = recoded_stream(&chained, Streams::Decompress).unwrap();
    assert_eq!(
        image.dict().get("Filter"),
        Some(&Value::Array(vec![name("DCTDecode")].into()))
    );
    assert_eq!(
        image.dict().get("DecodeParms"),
        Some(&Value::Array(vec![parameters].into()))
    );
    assert_eq!(image.data().as_slice(), &jpeg[..]);

    // An image filter first, and data that does not decode, stay.
    let jpeg_only = stream(&[("Filter", name("DCTDecode"))], &jpeg);
    assert_eq!(recoded_stream(&jpeg_only, Streams::Decompress), None);
    let broken = stream(&[("Filter", name("FlateDecode"))], b"not flate data");
    assert_eq!(recoded_stream(&broken, Streams::Decompress), None);
}

#[test]
fn a_compressed_save_round_trips_the_corpus_values_except_stream_encoding() {
    for path in corpus() {
        let doc = open(fs::read(&path).unwrap());
        let version = doc.current();
        let plain = saved(&version, false);
        let mut bytes = Vec::new();
        let options = Options {
            streams: Streams::Compress(6),
            ..Options::default()
        };
        full_with(&version, &mut bytes, &Budget::default(), None, options).unwrap();
        assert!(bytes.len() <= plain.len(), "{}", path.display());
        // A file read with a cross-reference stream gains one in both.
        let reopened = open(bytes).current();
        assert_eq!(
            reopened.next_number(),
            open(plain).current().next_number(),
            "{}",
            path.display()
        );
    }
}

#[test]
fn saves_renew_the_second_identifier_of_edits_and_incremental_saves_refuse_rewrites() {
    let doc = open(fs::read(&corpus()[0]).unwrap());
    let Some(Value::Array(read)) = doc.current().trailer().get("ID").cloned() else {
        panic!("the corpus file has no /ID");
    };
    let unedited = doc.current();
    let edited = edited(&doc, 7);
    let options = Options {
        renew_id: true,
        ..Options::default()
    };
    for incremental_mode in [false, true] {
        let save = if incremental_mode {
            incremental_with
        } else {
            full_with_plain
        };
        let mut bytes = Vec::new();
        save(&edited, &mut bytes, &Budget::default(), options).unwrap();
        let mut again = Vec::new();
        save(&edited, &mut again, &Budget::default(), options).unwrap();
        assert_eq!(bytes, again);
        let Some(Value::Array(id)) = open(bytes).current().trailer().get("ID").cloned() else {
            panic!("no /ID");
        };
        assert_eq!(id.get(0), read.get(0));
        assert!(matches!(id.get(1), Some(Value::String(second)) if second.as_bytes().len() == 16));
        assert_ne!(id.get(1), read.get(1));
        // An unedited save writes /ID as read.
        let mut bytes = Vec::new();
        save(&unedited, &mut bytes, &Budget::default(), options).unwrap();
        let reread = open(bytes).current().trailer().get("ID").cloned();
        assert_eq!(reread, Some(Value::Array(read.clone())));
    }
    for options in [
        Options {
            prune: true,
            ..Options::default()
        },
        Options {
            streams: Streams::Decompress,
            ..Options::default()
        },
    ] {
        assert!(matches!(
            incremental_with(&edited, &mut Vec::new(), &Budget::default(), options),
            Err(Error::Refused(Refusal::FullSaveOption))
        ));
    }
}

fn full_with_plain(
    version: &Version,
    target: &mut dyn Write,
    budget: &Budget,
    options: Options,
) -> Result<u64> {
    full_with(version, target, budget, None, options)
}

/// Saves `version` in full with `options` and returns the bytes and the
/// reopened file, unlocked with `password`.
fn reopened(version: &Version, options: Options, password: Option<&[u8]>) -> (Vec<u8>, FileBase) {
    let mut bytes = Vec::new();
    full_with_plain(version, &mut bytes, &Budget::default(), options).unwrap();
    let reread =
        FileBase::open_with_password(bytes.clone().into(), password, &Budget::default()).unwrap();
    (bytes, reread)
}

/// Asserts that the object streams of `reread`, a save of `version`, hold
/// only what ISO 32000-2 section 7.5.7 lets them hold, and returns the
/// number of their members.
fn assert_members(version: &Version, reread: &FileBase) -> usize {
    let encrypt = match version.trailer().get("Encrypt") {
        Some(Value::Reference(encrypt)) => Some(encrypt.number()),
        _ => None,
    };
    let mut members = 0;
    for (number, entry) in reread.index().entries() {
        if let Location::Member { .. } = entry.location() {
            members += 1;
            assert_eq!(version.generation(number), Some(0), "object {number}");
            assert_ne!(Some(number), encrypt);
            let value = version.get(number, &Budget::default()).unwrap().unwrap();
            assert!(
                !matches!(**value.value(), Value::Stream(_)),
                "object {number}"
            );
        }
    }
    members
}

#[test]
fn corpus_files_round_trip_through_cross_reference_streams_and_object_streams() {
    let stream = Options {
        cross_reference: CrossReference::Stream,
        ..Options::default()
    };
    let packed = Options {
        object_streams: true,
        ..Options::default()
    };
    let table = Options {
        cross_reference: CrossReference::Table,
        ..Options::default()
    };
    for path in corpus() {
        eprintln!("{}", path.display());
        let doc = open(fs::read(&path).unwrap());
        let version = doc.current();
        for options in [stream, packed, table] {
            let (bytes, reread) = reopened(&version, options, None);
            let section = &reread.index().revisions()[0].sections()[0];
            assert_eq!(
                section.kind() == SectionKind::Stream,
                options != table,
                "{options:?}"
            );
            if options != table {
                assert!(bytes[5..8] >= b"1.5"[..]);
            }
            let members = assert_members(&version, &reread);
            assert_eq!(members > 0, options == packed, "{options:?}");
            assert_reread(&version, &reread);
        }
    }
}

#[test]
fn object_streams_hold_no_stream_later_generation_or_encryption_dictionary() {
    let doc = open(saved(&memory().current(), false));
    let mut tx = doc.begin();
    let number = tx.allocate().unwrap().number();
    let data = stream(&[], b"0 0 m 1 1 l S");
    tx.write(number, &Value::Stream(Box::new(data)), &Budget::default())
        .unwrap();
    let version = tx.commit(&Budget::default()).unwrap();
    let packed = Options {
        object_streams: true,
        ..Options::default()
    };
    let (_, reread) = reopened(&version, packed, None);
    // The catalog and the page tree; object 3 has generation 7.
    assert_eq!(assert_members(&version, &reread), 2);
    assert_eq!(version.generation(3), Some(7));
    assert_reread(&version, &reread);
    // An unedited save of an encrypted file keeps its /Encrypt dictionary
    // and its encrypted streams out of object streams.
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/assets/encrypted/user-r4-rc4-128.pdf");
    let base = FileBase::open_with_password(
        fs::read(path).unwrap().into(),
        Some(b"userpw"),
        &Budget::default(),
    )
    .unwrap();
    let doc = Document::new(Arc::new(base), None);
    let version = doc.current();
    assert!(version.trailer().get("Encrypt").is_some());
    let (_, reread) = reopened(&version, packed, Some(b"userpw"));
    assert!(assert_members(&version, &reread) > 0);
    assert_reread(&version, &reread);
}

#[test]
fn an_incremental_save_keeps_the_cross_reference_form_and_refuses_another() {
    let stream = Options {
        cross_reference: CrossReference::Stream,
        ..Options::default()
    };
    let (bytes, _) = reopened(&memory().current(), stream, None);
    let doc = open(bytes.clone());
    let version = edited(&doc, 5);
    let mut update = Vec::new();
    incremental(&version, &mut update, &Budget::default()).unwrap();
    assert!(update.starts_with(&bytes));
    let reread = FileBase::open(update.into(), &Budget::default()).unwrap();
    let revisions = reread.index().revisions();
    assert_eq!(revisions.len(), 2);
    assert!(
        revisions
            .iter()
            .all(|revision| revision.sections()[0].kind() == SectionKind::Stream)
    );
    assert_reread(&version, &reread);
    // A default full save keeps the form too.
    let (_, reread) = reopened(&version, Options::default(), None);
    assert_eq!(
        reread.index().revisions()[0].sections()[0].kind(),
        SectionKind::Stream
    );
    for options in [
        stream,
        Options {
            object_streams: true,
            ..Options::default()
        },
    ] {
        assert!(matches!(
            incremental_with(&version, &mut Vec::new(), &Budget::default(), options),
            Err(Error::Refused(Refusal::FullSaveOption))
        ));
    }
}

/// Asserts that `restored` is `original` with each reference renumbered as
/// `map` renumbers it, and null where `map` has no number.
fn assert_renumbered_value(original: &Value, restored: &Value, map: &BTreeMap<u32, u32>) {
    let mut stack = vec![(original, restored)];
    while let Some((original, restored)) = stack.pop() {
        match (original, restored) {
            (Value::Reference(old), _) => {
                let expected = map.get(&old.number()).map_or(Value::Null, |&new| to(new));
                assert_eq!(restored, &expected, "{old:?}");
            }
            (Value::Array(original), Value::Array(restored)) => {
                assert_eq!(original.len(), restored.len());
                stack.extend(original.iter().zip(restored.iter()));
            }
            (Value::Dict(original), Value::Dict(restored)) => {
                assert_eq!(original.len(), restored.len());
                for (name, value) in original.iter() {
                    stack.push((value, restored.get(name.as_bytes()).unwrap()));
                }
            }
            (Value::Stream(original), Value::Stream(restored)) => {
                assert_eq!(original.data(), restored.data());
                assert_eq!(original.dict().len(), restored.dict().len());
                for (name, value) in original.dict().iter() {
                    let restored = restored.dict().get(name.as_bytes()).unwrap();
                    // An indirect /Length becomes the length that it names.
                    if name.as_bytes() != b"Length" {
                        stack.push((value, restored));
                    }
                }
            }
            _ => assert_eq!(original, restored),
        }
    }
}

/// Asserts that `reread`, a renumbering save of `version` that writes every
/// object, numbers them 1, 2, 3, and so on in order, with their values, and
/// with their streams decoded when `decompress` is true.
fn assert_renumbered(version: &Version, reread: &FileBase, decompress: bool) {
    assert!(!reread.index().is_repaired());
    let budget = Budget::default();
    let mut numbers = BudgetVec::new(&budget);
    version.base().numbers(&mut numbers).unwrap();
    for (number, _) in version.slots() {
        numbers.try_push(number).unwrap();
    }
    let mut numbers = numbers.into_vec();
    numbers.sort_unstable();
    numbers.dedup();
    let old: Vec<u32> = numbers
        .iter()
        .copied()
        .filter(|&number| number != 0 && version.generation(number).is_some())
        .collect();
    let map: BTreeMap<u32, u32> = old.iter().copied().zip(1..).collect();
    // Every number up to the last object's is in use, with generation 0,
    // and the objects that the save adds follow.
    let added = reread
        .index()
        .entries()
        .filter(|(number, _)| *number as usize > old.len() && reread.generation(*number).is_some())
        .count();
    let in_use = reread
        .index()
        .entries()
        .filter(|(number, _)| *number != 0 && reread.generation(*number).is_some())
        .count();
    assert_eq!(in_use, old.len() + added);
    for (&old, &new) in &map {
        assert_eq!(reread.generation(new), Some(0), "object {old} as {new}");
        let reference = Reference::new(old, version.generation(old).unwrap());
        let original = match version.slot(old) {
            Some(Slot::Written(value)) => value.bytes().clone(),
            _ => raw::body(&**version.base(), reference, &budget).unwrap(),
        };
        let restored = raw::body(reread, Reference::new(new, 0), &budget).unwrap();
        let fetch = |reference| version.base().object(reference, &budget);
        let mut original = Value::parse_with_lengths(&original, &budget, &fetch).unwrap();
        if decompress
            && let Some(decoded) = recode::recoded(&original, Streams::Decompress, &budget).unwrap()
        {
            original = decoded;
        }
        let fetch = |reference| reread.object(reference, &budget);
        let restored = Value::parse_with_lengths(&restored, &budget, &fetch).unwrap();
        assert_renumbered_value(&original, &restored, &map);
    }
    let original = Value::Dict(version.trailer().clone());
    let restored = Value::Dict(reread.trailer().clone());
    for name in ["Root", "Info", "Encrypt"] {
        if let (Value::Dict(original), Value::Dict(restored)) = (&original, &restored)
            && let Some(value) = original.get(name)
        {
            assert_renumbered_value(value, restored.get(name).unwrap(), &map);
        }
    }
}

#[test]
fn corpus_files_renumber_to_consecutive_numbers() {
    let renumber = Options {
        renumber: true,
        ..Options::default()
    };
    for path in corpus() {
        eprintln!("{}", path.display());
        let doc = open(fs::read(&path).unwrap());
        let version = doc.current();
        for options in [
            renumber,
            Options {
                object_streams: true,
                ..renumber
            },
            Options {
                cross_reference: CrossReference::Table,
                streams: Streams::Decompress,
                ..renumber
            },
        ] {
            let (_, reread) = reopened(&version, options, None);
            assert_renumbered(&version, &reread, options.streams == Streams::Decompress);
        }
    }
}

fn catalog_of(reread: &FileBase) -> Dict {
    let budget = Budget::default();
    let body = raw::body(reread, Reference::new(1, 0), &budget).unwrap();
    match Value::parse(&body, &budget).unwrap() {
        Value::Dict(catalog) => catalog,
        value => panic!("the catalog is {value:?}"),
    }
}

#[test]
fn a_renumbering_save_nulls_dangling_references_and_reencrypts() {
    // Object 3 has generation 7, and the catalog names an object that the
    // version does not have.
    let doc = memory();
    let budget = Budget::default();
    let mut tx = doc.begin();
    let catalog = dict(&[
        ("Type", name("Catalog")),
        ("Pages", to(2)),
        ("Missing", to(40)),
        ("Kept", Value::Reference(Reference::new(3, 7))),
    ]);
    tx.write(1, &catalog, &budget).unwrap();
    let version = tx.commit(&budget).unwrap();
    let renumber = Options {
        renumber: true,
        ..Options::default()
    };
    let (_, reread) = reopened(&version, renumber, None);
    assert_renumbered(&version, &reread, false);
    let catalog = catalog_of(&reread);
    assert_eq!(catalog.get("Missing"), Some(&Value::Null));
    assert_eq!(catalog.get("Kept"), Some(&to(3)));
    // Pruning leaves out object 3 when nothing names it, and the save then
    // numbers two objects.
    let (_, reread) = reopened(
        &memory().current(),
        Options {
            prune: true,
            ..renumber
        },
        None,
    );
    assert_eq!(reread.trailer().get("Size"), Some(&Value::Integer(3)));
    assert_eq!(catalog_of(&reread).get("Pages"), Some(&to(2)));
    // An encrypted file's objects take the keys of their new numbers.
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/assets/encrypted/user-r4-rc4-128.pdf");
    let base =
        FileBase::open_with_password(fs::read(path).unwrap().into(), Some(b"userpw"), &budget)
            .unwrap();
    let doc = Document::new(Arc::new(base), None);
    let version = doc.current();
    for options in [
        renumber,
        Options {
            object_streams: true,
            ..renumber
        },
    ] {
        let (_, reread) = reopened(&version, options, Some(b"userpw"));
        assert_renumbered(&version, &reread, false);
    }
    assert!(matches!(
        incremental_with(&version, &mut Vec::new(), &budget, renumber),
        Err(Error::Refused(Refusal::FullSaveOption))
    ));
}

/// Returns the value of the object `number` of `version`, or none for one
/// that it does not have.
fn value_of(version: &Version, number: u32) -> Option<Value> {
    let budget = Budget::default();
    let reference = Reference::new(number, version.generation(number)?);
    let bytes = match version.slot(number) {
        Some(Slot::Written(value)) => value.bytes().clone(),
        _ => raw::body(&**version.base(), reference, &budget).unwrap(),
    };
    let fetch = |reference| version.base().object(reference, &budget);
    Some(Value::parse_with_lengths(&bytes, &budget, &fetch).unwrap())
}

/// Asserts that what the trailer of `reread`, a save of `version`, reaches
/// is what the trailer of `version` reaches: each pair of objects that the
/// walk meets has equal values, with references paired again, so that
/// merged objects may become one. A reference to an object that `version`
/// does not have may stay or become null.
fn assert_equivalent(version: &Version, reread: &FileBase) {
    assert!(!reread.index().is_repaired());
    let budget = Budget::default();
    let restored_of = |reference: Reference| {
        reread.generation(reference.number())?;
        let bytes = raw::body(reread, reference, &budget).unwrap();
        let fetch = |reference| reread.object(reference, &budget);
        Some(Value::parse_with_lengths(&bytes, &budget, &fetch).unwrap())
    };
    let mut seen = std::collections::BTreeSet::new();
    let mut objects = Vec::new();
    let mut values = Vec::new();
    for name in ["Root", "Info"] {
        if let Some(original) = version.trailer().get(name) {
            values.push((
                original.clone(),
                reread.trailer().get(name).unwrap().clone(),
            ));
        }
    }
    loop {
        let Some((original, restored)) = values.pop() else {
            let Some((old, new)) = objects.pop() else {
                break;
            };
            match value_of(version, old) {
                Some(original) => values.push((original, restored_of(new).unwrap())),
                None => assert!(restored_of(new).is_none(), "object {old} as {new:?}"),
            }
            continue;
        };
        match (original, restored) {
            (Value::Reference(old), Value::Reference(new)) => {
                if seen.insert((old.number(), new.number())) {
                    objects.push((old.number(), new));
                }
            }
            (Value::Reference(old), restored) => {
                assert_eq!(version.generation(old.number()), None, "{old:?}");
                assert_eq!(restored, Value::Null);
            }
            (Value::Array(original), Value::Array(restored)) => {
                assert_eq!(original.len(), restored.len());
                values.extend(original.iter().cloned().zip(restored.iter().cloned()));
            }
            (Value::Dict(original), Value::Dict(restored)) => {
                assert_eq!(original.len(), restored.len());
                for (name, value) in original.iter() {
                    values.push((
                        value.clone(),
                        restored.get(name.as_bytes()).unwrap().clone(),
                    ));
                }
            }
            (Value::Stream(original), Value::Stream(restored)) => {
                assert_eq!(original.data(), restored.data());
                assert_eq!(original.dict().len(), restored.dict().len());
                for (name, value) in original.dict().iter() {
                    // An indirect /Length becomes the length that it names.
                    if name.as_bytes() != b"Length" {
                        let restored = restored.dict().get(name.as_bytes()).unwrap();
                        values.push((value.clone(), restored.clone()));
                    }
                }
            }
            (original, restored) => assert_eq!(original, restored),
        }
    }
}

/// Returns the number of in-use objects of `reread`.
fn in_use(reread: &FileBase) -> usize {
    reread
        .index()
        .entries()
        .filter(|(number, _)| *number != 0 && reread.generation(*number).is_some())
        .count()
}

#[test]
fn corpus_files_deduplicate_to_equivalent_objects() {
    let pruned = Options {
        prune: true,
        ..Options::default()
    };
    let dedupe = Options {
        dedupe: true,
        ..pruned
    };
    let mut merged = 0;
    for path in corpus() {
        let version = open(fs::read(&path).unwrap()).current();
        for options in [
            dedupe,
            Options {
                renumber: true,
                ..dedupe
            },
            Options {
                object_streams: true,
                ..dedupe
            },
        ] {
            let (bytes, reread) = reopened(&version, options, None);
            assert_equivalent(&version, &reread);
            // Merging writes no more than the same save without it.
            let (kept, unmerged) = reopened(
                &version,
                Options {
                    dedupe: false,
                    ..options
                },
                None,
            );
            assert!(bytes.len() <= kept.len(), "{}", path.display());
            merged += in_use(&unmerged) - in_use(&reread);
        }
    }
    assert!(merged > 0);
}

#[test]
fn a_deduplicating_save_keeps_cycles_identities_and_its_version() {
    let doc = memory();
    let budget = Budget::default();
    let mut tx = doc.begin();
    let tag = Value::String(PdfString::literal(&b"same"[..]));
    let catalog = dict(&[
        ("Type", name("Catalog")),
        ("Pages", to(2)),
        (
            "Items",
            Value::Array(vec![to(4), to(5), to(6), to(7), to(8), to(9), to(10), to(11)].into()),
        ),
    ]);
    tx.write(1, &catalog, &budget).unwrap();
    // 4 and 5 are equal and merge, with 6 and 7, which name them.
    tx.write(4, &dict(&[("Tag", tag.clone())]), &budget)
        .unwrap();
    tx.write(5, &dict(&[("Tag", tag.clone())]), &budget)
        .unwrap();
    tx.write(6, &dict(&[("Child", to(4))]), &budget).unwrap();
    tx.write(7, &dict(&[("Child", to(5))]), &budget).unwrap();
    // 8 and 9 form a cycle, and 10 and 11 are annotations.
    tx.write(8, &dict(&[("Other", to(9)), ("Tag", tag.clone())]), &budget)
        .unwrap();
    tx.write(9, &dict(&[("Other", to(8)), ("Tag", tag.clone())]), &budget)
        .unwrap();
    let annotation = dict(&[
        ("Type", name("Annot")),
        ("Subtype", name("Square")),
        ("Rect", Value::Array(vec![Value::Integer(0); 4].into())),
    ]);
    tx.write(10, &annotation, &budget).unwrap();
    tx.write(11, &annotation, &budget).unwrap();
    let version = tx.commit(&budget).unwrap();
    let dedupe = Options {
        prune: true,
        dedupe: true,
        ..Options::default()
    };
    let (_, reread) = reopened(&version, dedupe, None);
    assert_equivalent(&version, &reread);
    let items = |reread: &FileBase| match catalog_of(reread).get("Items") {
        Some(Value::Array(items)) => items.iter().cloned().collect::<Vec<_>>(),
        items => panic!("the items are {items:?}"),
    };
    assert_eq!(items(&reread), [4, 4, 6, 6, 8, 9, 10, 11].map(to).to_vec());
    for number in [5, 7] {
        assert_eq!(reread.generation(number), None, "object {number}");
    }
    // Renumbering closes the numbers that merging frees.
    let (_, reread) = reopened(
        &version,
        Options {
            renumber: true,
            ..dedupe
        },
        None,
    );
    assert_eq!(items(&reread), [3, 3, 4, 4, 5, 6, 7, 8].map(to).to_vec());
    assert_eq!(in_use(&reread), 8);
    // The version keeps every object, and an incremental save refuses.
    assert_eq!(version.generation(5), Some(0));
    assert!(matches!(
        incremental_with(&version, &mut Vec::new(), &budget, dedupe),
        Err(Error::Refused(Refusal::FullSaveOption))
    ));
}

#[test]
fn full_save_refuses_live_object_zero_before_output() {
    // Minimized from save-fuzz crash 48ddebe9596f7ddd421a47eb48cc7105a4d0f49c.
    let doc = open(b"%PDF-1.7\n0 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n".to_vec());
    let version = doc.current();
    assert_eq!(version.generation(0), Some(0));
    let encryption = Encryption {
        user: b"user".to_vec(),
        owner: b"owner".to_vec(),
        permissions: u32::MAX,
        aes256: true,
        random: [7; 64],
    };
    for options in [
        Options::default(),
        Options {
            prune: true,
            ..Options::default()
        },
        Options {
            renumber: true,
            ..Options::default()
        },
        Options {
            prune: true,
            dedupe: true,
            ..Options::default()
        },
        Options {
            object_streams: true,
            cross_reference: CrossReference::Stream,
            ..Options::default()
        },
    ] {
        for encryption in [None, Some(&encryption)] {
            let mut output = Vec::new();
            let result = full_with(
                &version,
                &mut output,
                &Budget::default(),
                encryption,
                options,
            );
            assert!(
                matches!(result, Err(Error::Store(ref error))
                if matches!(error.kind(), ErrorKind::Malformed(_))),
                "{result:?}"
            );
            assert!(output.is_empty());
            assert!(Arc::ptr_eq(&version, &doc.current()));
            assert_eq!(version.generation(0), Some(0));
        }
    }
}

#[test]
fn full_save_accepts_object_zero_explicitly_freed_after_repair() {
    let doc = open(b"%PDF-1.7\n0 0 obj (invalid live zero) endobj\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n2 0 obj << /Type /Pages /Kids [] /Count 0 >> endobj\n".to_vec());
    let budget = Budget::default();
    let mut transaction = doc.begin();
    assert!(transaction.free(0, &budget).unwrap());
    let version = transaction.commit(&budget).unwrap();
    assert_eq!(version.generation(0), None);
    assert_objects(&version, saved(&version, false));
    // A normal file whose zero is already free also remains saveable.
    let normal = open(saved(&version, false));
    assert_objects(&normal.current(), saved(&normal.current(), false));
}
