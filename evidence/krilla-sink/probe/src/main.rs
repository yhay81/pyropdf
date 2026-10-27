//! Writes a krilla document into a pyropdf-store transaction through
//! krilla's ObjectSink, then reads every object back from the committed
//! version.

use std::sync::Arc;
use std::time::Instant;

use krilla::Document;
use krilla::geom::{PathBuilder, Rect};
use krilla::page::PageSettings;
use krilla::sink::ObjectSink;
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

fn document(pages: usize) -> Document {
    let mut document = Document::new();
    for i in 0..pages {
        let mut page = document.start_page_with(PageSettings::from_wh(200.0, 200.0).unwrap());
        let mut surface = page.surface();
        let mut builder = PathBuilder::new();
        let x = 10.0 + (i % 100) as f32;
        builder.push_rect(Rect::from_xywh(x, 20.0, 50.0, 50.0).unwrap());
        surface.draw_path(&builder.finish().unwrap());
        surface.finish();
        page.finish();
    }
    document
}

/// Splits pdf-writer's output, `N 0 obj ... endobj` objects, into numbers
/// and value bytes. The probe's streams are compressed content, so the count
/// check in `main` catches a stream that happens to hold `endobj\n`.
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

fn main() {
    let budget = Budget::default();
    println!("| Pages | Objects | Bytes | finish_into (ms) | stored and committed (ms) |");
    println!("| ---: | ---: | ---: | ---: | ---: |");
    for pages in [1, 10, 100, 1000] {
        let store = Store::new(Arc::new(MemoryBase::new(Dict::default())), None);
        let mut sink = StoreSink {
            transaction: store.begin(),
            allocated: vec![],
            objects: vec![],
        };
        let start = Instant::now();
        let output = document(pages).finish_into(&mut sink).unwrap();
        let written = start.elapsed();

        let objects = split(&sink.objects);
        assert_eq!(objects.len(), sink.allocated.len());
        let mut transaction = sink.transaction;
        for (number, bytes) in &objects {
            assert!(sink.allocated.contains(number));
            let value = Value::parse(&Bytes::new(bytes.clone()), &budget).unwrap();
            transaction.write(*number, &value, &budget).unwrap();
        }
        let version = transaction.commit(&budget).unwrap();
        let stored = start.elapsed();

        for (number, _) in &objects {
            version.get(*number, &budget).unwrap().unwrap();
        }
        let root = version
            .get(u32::try_from(output.pages).unwrap(), &budget)
            .unwrap()
            .unwrap();
        let root = root.value();
        assert!(matches!(root.as_ref(), Value::Dict(_)));
        assert!(format!("{root:?}").contains("Pages"));

        println!(
            "| {pages} | {} | {} | {:.3} | {:.3} |",
            objects.len(),
            sink.objects.len(),
            written.as_secs_f64() * 1e3,
            stored.as_secs_f64() * 1e3,
        );
    }
}
