//! Measures an edit followed by a render through the hayro bridge, at 110,
//! 440, and 1,760 pages, split as the bridge's instrumentation reports it.
//! The README describes the workload. The only argument is the CSV to write.

use std::fmt::Write as _;
use std::sync::Arc;
use std::time::{Duration, Instant};

use pyropdf_engine::bridge::Bridged;
use pyropdf_engine::render;
use pyropdf_store::budget::Budget;
use pyropdf_store::memo::Memo;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::save::FileBase;
use pyropdf_store::value::{Bytes, Name, Value};
use pyropdf_store::version::Document;

const SIZES: [usize; 3] = [110, 440, 1_760];
const EDITS: usize = 41;

/// Writes a file of `pages` letter-size pages, each with a few lines of
/// Helvetica text and a filled rectangle, as a classic file with one table.
fn file(pages: usize) -> Vec<u8> {
    let mut out = b"%PDF-1.7\n".to_vec();
    let mut offsets = Vec::new();
    let mut object = |out: &mut Vec<u8>, number: usize, body: &str| {
        offsets.push((number, out.len()));
        out.extend_from_slice(format!("{number} 0 obj\n{body}\nendobj\n").as_bytes());
    };
    let kids: String = (0..pages).map(|p| format!("{} 0 R ", 4 + 2 * p)).collect();
    object(&mut out, 1, "<< /Type /Catalog /Pages 2 0 R >>");
    object(
        &mut out,
        2,
        &format!(
            "<< /Type /Pages /Kids [{kids}] /Count {pages} /MediaBox [0 0 612 792] \
             /Resources << /Font << /F1 3 0 R >> >> >>"
        ),
    );
    object(
        &mut out,
        3,
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    );
    for page in 0..pages {
        let mut text = String::from("0.8 g 72 600 468 120 re f 0 g BT /F1 12 Tf 72 720 Td ");
        for line in 0..20 {
            write!(
                text,
                "(Page {page}, line {line}: the quick brown fox) Tj 0 -14 Td "
            )
            .unwrap();
        }
        text.push_str("ET");
        object(
            &mut out,
            4 + 2 * page,
            &format!(
                "<< /Type /Page /Parent 2 0 R /Contents {} 0 R >>",
                5 + 2 * page
            ),
        );
        object(
            &mut out,
            5 + 2 * page,
            &format!("<< /Length {} >>\nstream\n{text}\nendstream", text.len()),
        );
    }
    let xref = out.len();
    let size = 4 + 2 * pages;
    out.extend_from_slice(format!("xref\n0 {size}\n0000000000 65535 f \n").as_bytes());
    offsets.sort_unstable();
    for (_, offset) in offsets {
        out.extend_from_slice(format!("{offset:010} 00000 n \n").as_bytes());
    }
    out.extend_from_slice(
        format!("trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n").as_bytes(),
    );
    out
}

#[derive(Default, Clone, Copy)]
struct Sample {
    transaction: Duration,
    page_index: Duration,
    store: Duration,
    hayro: Duration,
    render: Duration,
    total: Duration,
    update: usize,
}

fn median(mut values: Vec<Duration>) -> Duration {
    values.sort_unstable();
    values[values.len() / 2]
}

fn show(duration: Duration) -> String {
    let micros = duration.as_secs_f64() * 1e6;
    if micros >= 1000.0 {
        format!("{:.2} ms", micros / 1000.0)
    } else {
        format!("{micros:.1} µs")
    }
}

fn main() {
    let csv_path = std::env::args().nth(1).expect("the CSV to write");
    let budget = Budget::default();
    let mut csv = String::from(
        "pages,bytes,update_bytes,transaction_us,page_index_us,bridge_store_us,bridge_hayro_us,render_us,total_us\n",
    );
    println!("## An edit followed by a render\n");
    println!(
        "Each edit rotates one page, drawn in turn from the document, and renders that page at 72 dpi through the bridge; \
         the table gives the median of {EDITS} edits.\n"
    );
    println!(
        "| Pages | File | Update | Transaction | Page index | Bridge: store | Bridge: hayro | Render | Total |"
    );
    println!("| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |");
    for pages in SIZES {
        let bytes = file(pages);
        let len = bytes.len();
        let base = FileBase::open(Bytes::new(bytes), &budget).unwrap();
        let document = Document::new(Arc::new(base), None);
        let memo = Memo::new(1);
        // Warm up: the first index and bridge of the unedited version.
        let first = document.current();
        let index = PageIndex::memoized(&memo, &first, &budget).unwrap();
        Bridged::new(&first, &index, &budget).unwrap();
        let mut samples = Vec::new();
        for edit in 0..EDITS {
            let page = edit * 7 % pages;
            let started = Instant::now();
            let index = PageIndex::memoized(&memo, &document.current(), &budget).unwrap();
            let object = index.get(page).unwrap().object().unwrap();
            let mut dict = index.get(page).unwrap().dict().clone();
            dict.insert(Name::from("Rotate"), Value::Integer(90 * (edit as i64 % 4)));
            let mut transaction = document.begin();
            transaction
                .write(object.number(), &Value::Dict(dict), &budget)
                .unwrap();
            let version = transaction.commit(&budget).unwrap();
            let transaction = started.elapsed();
            let started = Instant::now();
            let index = PageIndex::memoized(&memo, &version, &budget).unwrap();
            let page_index = started.elapsed();
            let bridged = Bridged::new(&version, &index, &budget).unwrap();
            let started = Instant::now();
            let pixels = render::page(&bridged, page, 1.0).unwrap();
            let render = started.elapsed();
            assert!(!pixels.data.is_empty());
            let stats = bridged.stats();
            samples.push(Sample {
                transaction,
                page_index,
                store: stats.store,
                hayro: stats.hayro,
                render,
                total: transaction + page_index + stats.store + stats.hayro + render,
                update: stats.update,
            });
        }
        let pick = |f: fn(&Sample) -> Duration| median(samples.iter().map(f).collect());
        let row = Sample {
            transaction: pick(|s| s.transaction),
            page_index: pick(|s| s.page_index),
            store: pick(|s| s.store),
            hayro: pick(|s| s.hayro),
            render: pick(|s| s.render),
            total: pick(|s| s.total),
            update: samples[samples.len() / 2].update,
        };
        println!(
            "| {pages} | {len} B | {} B | {} | {} | {} | {} | {} | {} |",
            row.update,
            show(row.transaction),
            show(row.page_index),
            show(row.store),
            show(row.hayro),
            show(row.render),
            show(row.total)
        );
        let us = |d: Duration| d.as_secs_f64() * 1e6;
        writeln!(
            csv,
            "{pages},{len},{},{:.1},{:.1},{:.1},{:.1},{:.1},{:.1}",
            row.update,
            us(row.transaction),
            us(row.page_index),
            us(row.store),
            us(row.hayro),
            us(row.render),
            us(row.total)
        )
        .unwrap();
    }
    std::fs::write(csv_path, csv).unwrap();
}
