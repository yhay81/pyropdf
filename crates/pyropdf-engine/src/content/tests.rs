//! Operations keep their bytes, and a page's content is the content that
//! hayro reads from the original file.

use std::fs;
use std::path::Path;
use std::sync::Arc;

use hayro::hayro_syntax::Pdf;
use hayro::hayro_syntax::content::UntypedIter;
use proptest::prelude::*;
use pyropdf_store::budget::{Budget, DocumentLimits, LimitCode};
use pyropdf_store::index::Index;
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::save::FileBase;
use pyropdf_store::value::{Bytes, Name, Reference, Value};
use pyropdf_store::version::Document;
use pyropdf_store::{ErrorKind, LimitExceeded};

use super::*;

fn parse(data: &[u8]) -> Operations {
    Operations::parse(Bytes::new(data.to_vec()), &Budget::default()).unwrap()
}

fn operators(operations: &Operations) -> Vec<&[u8]> {
    operations
        .operations()
        .iter()
        .map(|operation| operations.operator(operation))
        .collect()
}

/// The bytes of every operation and the tail, in order.
fn joined(operations: &Operations) -> Vec<u8> {
    let mut out = Vec::new();
    for operation in operations.operations() {
        out.extend_from_slice(operations.bytes(operation));
    }
    out.extend_from_slice(operations.tail());
    out
}

fn refusal(result: Result<Operations>) -> LimitExceeded {
    match result.unwrap_err().kind() {
        ErrorKind::Limit(refusal) => *refusal,
        kind => panic!("not a limit: {kind:?}"),
    }
}

#[test]
fn operations_keep_their_bytes_and_operands() {
    let content =
        b"q 1 0 0 1 10.50 -2 cm BT /F1 12 Tf [(A) -120 <42>] TJ ET Q /P <</MCID 3>> BDC EMC";
    let operations = parse(content);
    assert_eq!(
        operators(&operations),
        [
            &b"q"[..],
            b"cm",
            b"BT",
            b"Tf",
            b"TJ",
            b"ET",
            b"Q",
            b"BDC",
            b"EMC"
        ]
    );
    let cm = &operations.operations()[1];
    assert_eq!(operations.bytes(cm), b" 1 0 0 1 10.50 -2 cm");
    assert_eq!(cm.span(), 1..21);
    assert_eq!(cm.operator_span(), 19..21);
    let operands = operations.operands(cm);
    assert_eq!(operands[0], Value::Integer(1));
    let Value::Real(real) = &operands[4] else {
        panic!("not a real: {:?}", operands[4]);
    };
    assert_eq!(real.value(), 10.5);
    assert_eq!(operands[5], Value::Integer(-2));
    let tf = operations.operands(&operations.operations()[3]);
    assert_eq!(
        tf,
        [Value::Name(Name::new(b"F1".to_vec())), Value::Integer(12)]
    );
    let bdc = operations.operands(&operations.operations()[7]);
    let Value::Dict(dict) = &bdc[1] else {
        panic!("not a dictionary: {:?}", bdc[1]);
    };
    assert_eq!(dict.get("MCID"), Some(&Value::Integer(3)));
    assert!(operations.tail().is_empty());
    assert_eq!(joined(&operations), content);
}

#[test]
fn comments_and_white_space_stay_with_the_operation_that_follows() {
    let content = b"% header\nq\n  % inside\n  0 0 m\n\nQ\n% end\n";
    let operations = parse(content);
    assert_eq!(operators(&operations), [&b"q"[..], b"m", b"Q"]);
    assert_eq!(
        operations.bytes(&operations.operations()[0]),
        b"% header\nq"
    );
    assert_eq!(operations.tail(), b"\n% end\n");
    assert_eq!(joined(&operations), content);
}

#[test]
fn inline_images_end_after_ei() {
    let data: Vec<u8> = (0..12).collect();
    let mut content = b"q BI /W 4 /H 1 /BPC 8 /CS /RGB ID ".to_vec();
    content.extend_from_slice(&data);
    content.extend_from_slice(b" EI Q");
    let operations = parse(&content);
    assert_eq!(operators(&operations), [&b"q"[..], b"BI", b"Q"]);
    let image = &operations.operations()[1];
    assert_eq!(operations.image_data(image), Some(&data[..]));
    assert!(operations.bytes(image).ends_with(b" EI"));
    let [Value::Dict(dict)] = operations.operands(image) else {
        panic!("not one dictionary: {:?}", operations.operands(image));
    };
    assert_eq!(dict.get("W"), Some(&Value::Integer(4)));
    assert_eq!(
        dict.get("CS"),
        Some(&Value::Name(Name::new(b"RGB".to_vec())))
    );
    assert_eq!(operations.image_data(&operations.operations()[2]), None);
    assert_eq!(joined(&operations), content);
}

#[test]
fn content_after_what_hayro_reads_stays_in_the_tail() {
    // hayro reads at most ten operands and stops at an eleventh.
    let content = b"q 1 2 3 4 5 6 7 8 9 10 11 sc Q";
    let operations = parse(content);
    assert_eq!(operators(&operations), [&b"q"[..]]);
    assert_eq!(operations.tail(), b" 1 2 3 4 5 6 7 8 9 10 11 sc Q");
}

#[test]
fn operators_may_hold_any_regular_character() {
    // pdf.js issue13999.pdf draws with an operator that is a control byte.
    let operations = parse(b"q \x14 Q");
    assert_eq!(operators(&operations), [&b"q"[..], b"\x14", b"Q"]);
}

#[test]
fn an_escaped_operator_is_refused() {
    let error =
        Operations::parse(Bytes::new(b"q T#6a Q".to_vec()), &Budget::default()).unwrap_err();
    assert!(matches!(error.kind(), ErrorKind::Malformed(_)), "{error}");
}

#[test]
fn the_operation_count_is_bounded() {
    let budget =
        |limit| Budget::default().with_limit(LimitCode::ContentOperationCount, Some(limit));
    let data = || Bytes::new(b"q Q q Q".to_vec());
    let refused = refusal(Operations::parse(data(), &budget(3)));
    assert_eq!(refused.code(), LimitCode::ContentOperationCount);
    assert_eq!(Operations::parse(data(), &budget(4)).unwrap().len(), 4);
    // The default is pylopdf's: 1,048,576 operations.
    assert_eq!(
        Budget::default().limit(LimitCode::ContentOperationCount),
        Some(1 << 20)
    );
}

#[test]
fn deep_operands_are_bounded_by_the_object_depth() {
    let mut content = vec![b'['; 20];
    content.extend_from_slice(&[b']'; 20]);
    content.extend_from_slice(b" Do");
    let limits = DocumentLimits {
        max_object_depth: Some(8),
        ..DocumentLimits::default()
    };
    let refused = refusal(Operations::parse(
        Bytes::new(content),
        &Budget::new(&limits),
    ));
    assert_eq!(refused.code(), LimitCode::ObjectDepth);
}

proptest! {
    /// Any bytes tokenize into contiguous operations whose bytes and the
    /// tail are the bytes, or refuse.
    #[test]
    fn any_content_keeps_its_bytes(content in proptest::collection::vec(any::<u8>(), 0..256)) {
        let Ok(operations) = Operations::parse(Bytes::new(content.clone()), &Budget::default())
        else {
            return Ok(());
        };
        let mut end = 0;
        for operation in operations.operations() {
            prop_assert_eq!(operation.span().start, end);
            prop_assert!(operation.span().end >= operation.operator_span().end);
            prop_assert!(operation.operator_span().start >= operation.span().start);
            end = operation.span().end;
        }
        prop_assert_eq!(operations.tail_start(), end);
        prop_assert_eq!(joined(&operations), content);
    }

    /// Content made of operations reads them back in order.
    #[test]
    fn written_operations_read_back(
        operations in proptest::collection::vec(
            prop_oneof![
                Just(&b"q"[..]),
                Just(&b"Q"[..]),
                Just(&b"0 0 10 10 re"[..]),
                Just(&b"/F1 12 Tf"[..]),
                Just(&b"(a b) Tj"[..]),
                Just(&b"[(x) -2.5 (y)] TJ"[..]),
                Just(&b"BI /W 1 /H 1 /BPC 8 /CS /G ID \x80 EI"[..]),
            ],
            0..32,
        ),
        separator in prop_oneof![Just(&b" "[..]), Just(&b"\n"[..]), Just(&b"\r\n% c\n"[..])],
    ) {
        let content = operations.join(separator);
        let parsed = parse(&content);
        prop_assert_eq!(parsed.len(), operations.len());
        prop_assert_eq!(joined(&parsed), content);
    }
}

fn open(bytes: Vec<u8>) -> Document {
    let budget = Budget::default();
    let index = Index::open(Bytes::new(bytes), &budget).unwrap();
    Document::new(Arc::new(FileBase::new(index, &budget).unwrap()), None)
}

/// Returns the content of every page of `document`.
fn pages(document: &Document) -> Vec<PageContent> {
    let version = document.current();
    let budget = Budget::default();
    let index = PageIndex::build(&mut Reads::new(&version, &budget)).unwrap();
    index
        .pages()
        .iter()
        .map(|page| PageContent::read(&mut Reads::new(&version, &budget), page).unwrap())
        .collect()
}

/// A one-page document of `objects`, numbered from 1, whose page is
/// object 3.
fn document(objects: &[&[u8]]) -> Vec<u8> {
    let mut out = b"%PDF-1.7\n".to_vec();
    let mut offsets = Vec::new();
    for (number, object) in (1..).zip(objects) {
        offsets.push(out.len());
        out.extend_from_slice(format!("{number} 0 obj\n").as_bytes());
        out.extend_from_slice(object);
        out.extend_from_slice(b"\nendobj\n");
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
    out
}

#[test]
fn page_content_joins_streams_as_hayro_does() {
    let bytes = document(&[
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 300 300] >>",
        b"<< /Type /Page /Parent 2 0 R /Contents [4 0 R 5 0 R 6 0 R 4 0 R] >>",
        b"<< /Length 21 >>\nstream\nBT /F1 12 Tf 9 9 Td (\nendstream",
        b"<< /Length 8 >>\nstream\nA) Tj ET\nendstream",
        // hayro stops at the first item that is not a stream.
        b"<< /Type /NotAStream >>",
    ]);
    let content = &pages(&open(bytes.clone()))[0];
    let operations = content.operations();
    assert_eq!(
        operations.data().as_slice(),
        b"BT /F1 12 Tf 9 9 Td ( A) Tj ET "
    );
    let theirs = Pdf::new(bytes).unwrap();
    assert_eq!(
        Some(operations.data().as_slice()),
        theirs.pages()[0].page_stream()
    );
    assert_eq!(
        content.parts(),
        [
            Part {
                stream: Reference::new(4, 0),
                span: 0..21
            },
            Part {
                stream: Reference::new(5, 0),
                span: 22..30
            },
        ]
    );
    // The string that the first stream begins names the second stream,
    // which holds its operator.
    let tj = &operations.operations()[3];
    assert_eq!(operations.operator(tj), b"Tj");
    assert_eq!(content.stream_of(tj), Some(Reference::new(5, 0)));
    assert_eq!(
        content.stream_of(&operations.operations()[0]),
        Some(Reference::new(4, 0))
    );
    assert_eq!(content.part_at(21), None);
}

#[test]
fn a_page_without_readable_content_has_no_operations() {
    for contents in [
        &b"/Contents 9 0 R"[..],
        b"/Contents 4 0 R",
        b"/Contents (text)",
        b"",
    ] {
        let mut page = b"<< /Type /Page /Parent 2 0 R ".to_vec();
        page.extend_from_slice(contents);
        page.extend_from_slice(b" >>");
        let bytes = document(&[
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 300 300] >>",
            &page,
            // A stream whose filter does not decode is left out.
            b"<< /Length 4 /Filter /DCTDecode >>\nstream\nq Q \nendstream",
        ]);
        let content = &pages(&open(bytes.clone()))[0];
        let theirs = Pdf::new(bytes).unwrap();
        assert_eq!(theirs.pages()[0].page_stream(), None);
        assert!(content.operations().is_empty());
        assert!(content.parts().is_empty());
    }
}

#[test]
fn the_joined_content_is_bounded() {
    let bytes = document(&[
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 300 300] >>",
        b"<< /Type /Page /Parent 2 0 R /Contents [4 0 R 4 0 R] >>",
        b"<< /Length 3 >>\nstream\nq Q\nendstream",
    ]);
    let document = open(bytes);
    let version = document.current();
    let budget = Budget::default().with_limit(LimitCode::ContentStreamSize, Some(7));
    let index = PageIndex::build(&mut Reads::new(&version, &budget)).unwrap();
    let error =
        PageContent::read(&mut Reads::new(&version, &budget), &index.pages()[0]).unwrap_err();
    assert_eq!(
        error.limit().map(LimitExceeded::code),
        Some(LimitCode::ContentStreamSize)
    );
    let budget = Budget::default().with_limit(LimitCode::ContentStreamSize, Some(8));
    let content = PageContent::read(&mut Reads::new(&version, &budget), &index.pages()[0]);
    assert_eq!(content.unwrap().operations().len(), 4);
}

#[test]
fn every_corpus_page_reads_the_content_that_hayro_reads() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/assets/real_world");
    let mut files: Vec<_> = fs::read_dir(&dir)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.extension().is_some_and(|e| e == "pdf"))
        .collect();
    files.sort();
    assert_eq!(files.len(), 14);
    let (mut operations_read, mut pages_read) = (0, 0);
    for path in &files {
        let bytes = fs::read(path).unwrap();
        let theirs = Pdf::new(bytes.clone()).unwrap();
        let ours = pages(&open(bytes));
        assert_eq!(ours.len(), theirs.pages().len(), "{}", path.display());
        for (index, (content, page)) in ours.iter().zip(theirs.pages().iter()).enumerate() {
            let at = format!("{} page {index}", path.display());
            let operations = content.operations();
            let data = operations.data().as_slice();
            let expected = page.page_stream().unwrap_or_default();
            assert_eq!(data, expected, "{at}");
            assert_eq!(joined(operations), data, "{at}");
            let mut iter = UntypedIter::new(expected);
            let mut theirs = Vec::new();
            while let Some(instruction) = iter.next() {
                theirs.push(instruction.operator.to_vec());
            }
            let ours: Vec<_> = operators(operations)
                .into_iter()
                .map(<[u8]>::to_vec)
                .collect();
            assert_eq!(ours, theirs, "{at}");
            let mut end = 0;
            for part in content.parts() {
                assert!(
                    part.span.start >= end && part.span.end <= data.len(),
                    "{at}"
                );
                end = part.span.end;
            }
            operations_read += operations.len();
            pages_read += 1;
        }
    }
    // 155 pages, with 80,233 operations.
    assert_eq!(pages_read, 155);
    assert!(operations_read > 80_000, "{operations_read}");
}
