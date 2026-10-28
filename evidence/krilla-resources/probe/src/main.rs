//! Stamps one face on many pages into a pyropdf-store transaction through
//! krilla's ObjectSink: once with one krilla document per stamp (W1), and
//! once with resources that the documents share (W2). Counts the font
//! programs that each writes and checks that every stamp's font reference
//! resolves in the committed version.

use std::sync::Arc;
use std::time::{Duration, Instant};

use krilla::Document;
use krilla::SerializeSettings;
use krilla::geom::Point;
use krilla::page::PageSettings;
use krilla::resources::Resources;
use krilla::sink::ObjectSink;
use krilla::text::{Font, TextDirection};
use pyropdf_store::budget::Budget;
use pyropdf_store::value::{Bytes, Dict, Value};
use pyropdf_store::version::{Document as Store, MemoryBase, Transaction};

/// Allocates numbers from the transaction and keeps the objects krilla
/// writes until they are parsed.
struct StoreSink {
    transaction: Transaction,
    allocated: Vec<u32>,
    objects: Vec<u8>,
}

impl ObjectSink for StoreSink {
    fn allocate(&mut self) -> i32 {
        let number = self.transaction.allocate().expect("a free number").number();
        self.allocated.push(number);
        i32::try_from(number).expect("krilla numbers are i32")
    }

    fn write(&mut self, objects: &[u8]) {
        self.objects.extend_from_slice(objects);
    }
}

/// Starts a document with one page that stamps `Page {index}` in `font`.
fn stamp(mut document: Document, font: &Font, index: usize) -> Document {
    let mut page = document.start_page_with(PageSettings::from_wh(200.0, 200.0).unwrap());
    let mut surface = page.surface();
    surface.draw_text(
        Point::from_xy(20.0, 100.0),
        font.clone(),
        12.0,
        &format!("Page {index}"),
        false,
        TextDirection::Auto,
    );
    surface.finish();
    page.finish();
    document
}

/// Splits pdf-writer's output, `N 0 obj ... endobj` objects, into numbers
/// and value bytes. The count check in `store` catches a stream that
/// happens to hold `endobj\n`.
fn split(objects: &[u8]) -> Vec<(u32, Vec<u8>)> {
    let mut found = vec![];
    let mut rest = objects;
    while let Some(end) = rest.windows(7).position(|w| w == b"endobj\n") {
        let object = &rest[..end];
        let header = object.windows(4).position(|w| w == b" obj").unwrap();
        let text = std::str::from_utf8(&object[..header]).unwrap().trim();
        let number = text.split(' ').next().unwrap().parse().unwrap();
        let value = &object[header + 4..];
        let start = value.iter().position(|b| !b.is_ascii_whitespace()).unwrap();
        found.push((number, value[start..].to_vec()));
        rest = &rest[end + 7..];
    }
    found
}

/// The numbers that `/Font` resource dictionaries name.
fn font_references(objects: &[u8]) -> Vec<u32> {
    let text = String::from_utf8_lossy(objects);
    text.match_indices("/Font<</f0 ")
        .map(|(at, needle)| {
            let rest = &text[at + needle.len()..];
            rest.split(' ').next().unwrap().parse().unwrap()
        })
        .collect()
}

struct Run {
    programs: usize,
    objects: usize,
    bytes: usize,
    written: Duration,
    finalized: Option<Duration>,
}

/// Parses every object that the sink received, writes it, commits, and
/// checks that each font reference resolves to a Type0 font.
fn store(sink: StoreSink, budget: &Budget) -> (usize, usize) {
    let objects = split(&sink.objects);
    assert_eq!(objects.len(), sink.allocated.len());
    let mut transaction = sink.transaction;
    for (number, bytes) in &objects {
        assert!(sink.allocated.contains(number));
        let value = Value::parse(&Bytes::new(bytes.clone()), budget).unwrap();
        transaction.write(*number, &value, budget).unwrap();
    }
    let version = transaction.commit(budget).unwrap();
    for number in font_references(&sink.objects) {
        let font = version.get(number, budget).unwrap().expect("the font");
        assert!(format!("{:?}", font.value()).contains("Type0"));
    }
    (objects.len(), sink.objects.len())
}

fn sink() -> StoreSink {
    let store = Store::new(Arc::new(MemoryBase::new(Dict::default())), None);
    StoreSink {
        transaction: store.begin(),
        allocated: vec![],
        objects: vec![],
    }
}

fn programs(objects: &[u8]) -> usize {
    objects.windows(10).filter(|w| *w == b"/FontFile2").count()
}

/// One krilla document per stamp, each with its own font subset.
fn per_document(font: &Font, pages: usize, budget: &Budget) -> Run {
    let mut sink = sink();
    let start = Instant::now();
    for index in 0..pages {
        stamp(Document::new(), font, index)
            .finish_into(&mut sink)
            .unwrap();
    }
    let written = start.elapsed();
    let programs = programs(&sink.objects);
    assert_eq!(font_references(&sink.objects).len(), pages);
    let (objects, bytes) = store(sink, budget);
    Run {
        programs,
        objects,
        bytes,
        written,
        finalized: None,
    }
}

/// Documents that share resources, finalized once at the end.
fn shared(font: &Font, pages: usize, budget: &Budget) -> Run {
    let mut sink = sink();
    let mut resources = Resources::new(SerializeSettings::default());
    let start = Instant::now();
    for index in 0..pages {
        let document = stamp(Document::new_with_resources(resources), font, index);
        let (result, next) = document.finish_with_resources(&mut sink);
        result.unwrap();
        resources = next;
    }
    let written = start.elapsed();
    resources.finalize_into(&mut sink).unwrap();
    let finalized = start.elapsed() - written;
    let programs = programs(&sink.objects);
    let references = font_references(&sink.objects);
    assert_eq!(references.len(), pages);
    assert!(references.iter().all(|number| *number == references[0]));
    let (objects, bytes) = store(sink, budget);
    Run {
        programs,
        objects,
        bytes,
        written,
        finalized: Some(finalized),
    }
}

fn main() {
    let path = std::env::args()
        .nth(1)
        .expect("the path of a TrueType font");
    let font = Font::new(std::fs::read(path).unwrap().into(), 0).unwrap();
    let budget = Budget::default();
    println!(
        "| Stamps | Mode | Font programs | Objects | Bytes | Documents written (ms) | finalize_into (ms) |"
    );
    println!("| ---: | --- | ---: | ---: | ---: | ---: | ---: |");
    // The first run warms up the font and the allocator.
    shared(&font, 1, &budget);
    for pages in [1, 10, 100, 1000] {
        for (mode, run) in [
            (
                "one document per stamp (W1)",
                per_document(&font, pages, &budget),
            ),
            ("shared resources (W2)", shared(&font, pages, &budget)),
        ] {
            println!(
                "| {pages} | {mode} | {} | {} | {} | {:.3} | {} |",
                run.programs,
                run.objects,
                run.bytes,
                run.written.as_secs_f64() * 1e3,
                run.finalized.map_or("–".to_string(), |time| format!(
                    "{:.3}",
                    time.as_secs_f64() * 1e3
                )),
            );
        }
    }
}
