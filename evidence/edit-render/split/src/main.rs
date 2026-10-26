//! The split of evidence/edit-render's rotate-and-render loop through the
//! store and the bridge, as the bridge's instrumentation reports it. The
//! arguments are the documents; it prints a Markdown table.

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

/// Pixels per point: 18 dpi, as measure.py renders.
const SCALE: f64 = 0.25;

fn ms(total: Duration, count: usize) -> String {
    format!("{:.3} ms", total.as_secs_f64() * 1e3 / count as f64)
}

fn main() {
    println!("## Where one rotate-and-render goes in pyropdf\n");
    println!(
        "The loop of the table above, through the store and the engine without Python; \
         each column is the mean per page.\n"
    );
    println!(
        "| Pages | Transaction | Page index | Bridge: store | Bridge: hayro re-read | Render | Total |"
    );
    println!("| ---: | ---: | ---: | ---: | ---: | ---: | ---: |");
    for path in std::env::args().skip(1) {
        let budget = Budget::default();
        let bytes = std::fs::read(&path).unwrap();
        let base = FileBase::open(Bytes::new(bytes), &budget).unwrap();
        let document = Document::new(Arc::new(base), Some(256 * 1024 * 1024));
        let memo = Memo::new(1);
        let count = PageIndex::memoized(&memo, &document.current(), &budget).unwrap().len();
        let mut totals = [Duration::ZERO; 5];
        for at in 0..count {
            let started = Instant::now();
            let index = PageIndex::memoized(&memo, &document.current(), &budget).unwrap();
            let page = index.get(at).unwrap();
            let mut dict = page.dict().clone();
            dict.insert(Name::from("Rotate"), Value::Integer(90));
            let mut transaction = document.begin();
            transaction
                .write(page.object().unwrap().number(), &Value::Dict(dict), &budget)
                .unwrap();
            let version = transaction.commit(&budget).unwrap();
            totals[0] += started.elapsed();
            let started = Instant::now();
            let index = PageIndex::memoized(&memo, &version, &budget).unwrap();
            totals[1] += started.elapsed();
            let bridged = Arc::new(Bridged::new(&version, &index, &budget).unwrap());
            totals[2] += bridged.stats().store;
            totals[3] += bridged.stats().hayro;
            let started = Instant::now();
            render::raster(&bridged, at, (SCALE, 0.0, 0.0, SCALE), false).unwrap();
            totals[4] += started.elapsed();
        }
        let total: Duration = totals.iter().sum();
        println!(
            "| {count} | {} | {} | {} | {} | {} | {} |",
            ms(totals[0], count),
            ms(totals[1], count),
            ms(totals[2], count),
            ms(totals[3], count),
            ms(totals[4], count),
            ms(total, count)
        );
    }
}
