//! Opens each document 50 times with the patched hayro-syntax and prints the
//! median time of its two steps: reading the cross-reference data and building
//! the page list. The arguments are the documents; it prints a Markdown table.

use std::path::Path;
use std::time::Duration;

use hayro_syntax::Pdf;

/// The opens per document.
const RUNS: usize = 50;

fn median_ms(mut times: Vec<Duration>) -> f64 {
    times.sort();
    times[times.len() / 2].as_secs_f64() * 1e3
}

fn main() {
    println!("| Document | Pages | Objects | Cross-reference | Page list | Page list share |");
    println!("| --- | ---: | ---: | ---: | ---: | ---: |");
    for path in std::env::args().skip(1) {
        let bytes = std::fs::read(&path).unwrap();
        let (mut xrefs, mut lists) = (Vec::new(), Vec::new());
        let (mut pages, mut objects) = (0, 0);
        for _ in 0..RUNS {
            let pdf = Pdf::new(bytes.clone()).unwrap();
            let (xref, list) = hayro_syntax::load_split::last();
            xrefs.push(xref);
            lists.push(list);
            pages = pdf.pages().len();
            objects = pdf.len();
        }
        let (xref, list) = (median_ms(xrefs), median_ms(lists));
        let name = Path::new(&path)
            .file_name()
            .unwrap()
            .to_string_lossy()
            .into_owned();
        println!(
            "| {name} | {pages} | {objects} | {xref:.3} ms | {list:.3} ms | {:.0}% |",
            100.0 * list / (xref + list)
        );
    }
}
