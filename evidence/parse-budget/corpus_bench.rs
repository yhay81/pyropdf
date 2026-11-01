//! Times hayro-syntax without a budget on the corpus, in three phases:
//! opening each document, walking every object it defines through lazy
//! reads, and reading the operations of every page's content.
//!
//! Run as an example of hayro-syntax with the directory of the corpus:
//! `cargo run --release --example corpus_bench -- <dir> [rounds]`.

use hayro_syntax::Pdf;
use hayro_syntax::object::{MaybeRef, Object};
use std::time::{Duration, Instant};
use std::{env, fs};

/// Counts the values of `object`, reading arrays and dictionaries lazily.
fn walk(object: &Object<'_>, depth: usize) -> usize {
    if depth > 64 {
        return 1;
    }
    let maybe = |item: MaybeRef<Object<'_>>| match item {
        MaybeRef::Ref(_) => 1,
        MaybeRef::NotRef(object) => walk(&object, depth + 1),
    };
    match object {
        Object::Array(array) => 1 + array.raw_iter().map(maybe).sum::<usize>(),
        Object::Dict(dict) => 1 + dict.entries().map(|(_, value)| maybe(value)).sum::<usize>(),
        Object::Stream(stream) => {
            1 + stream
                .dict()
                .entries()
                .map(|(_, value)| maybe(value))
                .sum::<usize>()
        }
        _ => 1,
    }
}

fn main() {
    let mut args = env::args().skip(1);
    let dir = args.next().expect("the corpus directory");
    let rounds: usize = args.next().map_or(20, |n| n.parse().unwrap());
    let mut files: Vec<_> = fs::read_dir(&dir)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.extension().is_some_and(|e| e == "pdf"))
        .collect();
    files.sort();
    let documents: Vec<_> = files.iter().map(|path| fs::read(path).unwrap()).collect();

    // The best time of each phase of each document, summed over documents.
    let mut best = vec![[Duration::MAX; 3]; documents.len()];
    let mut counts = vec![[0usize; 2]; documents.len()];
    for _ in 0..rounds {
        for (index, data) in documents.iter().enumerate() {
            let start = Instant::now();
            let Ok(pdf) = Pdf::new(data.clone()) else {
                continue;
            };
            let opened = Instant::now();
            let values: usize = pdf.objects().into_iter().map(|o| walk(&o, 0)).sum();
            let walked = Instant::now();
            let operations: usize = pdf
                .pages()
                .iter()
                .map(|page| {
                    // Typed operations parse every operand, as an interpreter does.
                    let mut iter = page.typed_operations();
                    let mut count = 0;
                    while iter.next().is_some() {
                        count += 1;
                    }
                    count
                })
                .sum();
            let read = Instant::now();
            let times = [opened - start, walked - opened, read - walked];
            for (slot, time) in best[index].iter_mut().zip(times) {
                *slot = (*slot).min(time);
            }
            counts[index] = [values, operations];
        }
    }

    let mut total = [Duration::ZERO; 3];
    for ((path, times), [values, operations]) in files.iter().zip(&best).zip(&counts) {
        if times[0] == Duration::MAX {
            println!("{} not opened", path.file_name().unwrap().to_string_lossy());
            continue;
        }
        println!(
            "{:<40} open {:>8.3} ms  walk {:>8.3} ms ({values} values)  content {:>8.3} ms ({operations} ops)",
            path.file_name().unwrap().to_string_lossy(),
            times[0].as_secs_f64() * 1e3,
            times[1].as_secs_f64() * 1e3,
            times[2].as_secs_f64() * 1e3,
        );
        for (sum, time) in total.iter_mut().zip(times) {
            *sum += *time;
        }
    }
    println!(
        "total open {:.3} ms  walk {:.3} ms  content {:.3} ms  all {:.3} ms",
        total[0].as_secs_f64() * 1e3,
        total[1].as_secs_f64() * 1e3,
        total[2].as_secs_f64() * 1e3,
        total.iter().sum::<Duration>().as_secs_f64() * 1e3,
    );
}
