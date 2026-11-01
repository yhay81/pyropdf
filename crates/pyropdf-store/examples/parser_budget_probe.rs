//! Reproduce the parser's input-derived allocations before budget refusal (#509).
//!
//! Run with `cargo run --locked --release -p pyropdf-store --example parser_budget_probe`.
//! Allocation counts are cumulative requested bytes, not peak/live memory. The
//! input and budgets are created before counting starts. This is evidence of
//! an unfinished upstream limitation, not an allocation-bound benchmark.

#![expect(
    clippy::disallowed_methods,
    reason = "the standalone probe constructs its test input before measuring"
)]

use pyropdf_store::budget::{Budget, CancellationToken, LimitCode};
use pyropdf_store::value::{Bytes, Value};
use std::alloc::{GlobalAlloc, Layout, System};
use std::fmt::Write;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::Instant;

struct CountingAllocator;
static TRACK: AtomicBool = AtomicBool::new(false);
static ALLOCATED: AtomicU64 = AtomicU64::new(0);

// SAFETY: all allocation operations delegate their exact arguments to System;
// the counters do not allocate or modify the returned pointer.
unsafe impl GlobalAlloc for CountingAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if TRACK.load(Ordering::Relaxed) {
            ALLOCATED.fetch_add(layout.size() as u64, Ordering::Relaxed);
        }
        unsafe { System.alloc(layout) }
    }
    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        unsafe { System.dealloc(ptr, layout) }
    }
    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        if TRACK.load(Ordering::Relaxed) {
            ALLOCATED.fetch_add(new_size as u64, Ordering::Relaxed);
        }
        unsafe { System.realloc(ptr, layout, new_size) }
    }
}

#[global_allocator]
static ALLOCATOR: CountingAllocator = CountingAllocator;

fn main() {
    let mut source = String::from("<< ");
    for n in 0..100_000 {
        write!(&mut source, "/K{n} 0 ").unwrap();
    }
    source.push_str(">>");
    let input = Bytes::from(source.into_bytes());
    let token = CancellationToken::new();
    token.cancel();
    let cases = [
        (
            "zero_memory",
            Budget::default().with_limit(LimitCode::MemorySize, Some(0)),
        ),
        ("pre_cancelled", Budget::default().with_cancellation(token)),
    ];
    for (name, budget) in cases {
        ALLOCATED.store(0, Ordering::Relaxed);
        TRACK.store(true, Ordering::Relaxed);
        let start = Instant::now();
        let result = Value::parse(&input, &budget);
        let elapsed = start.elapsed();
        TRACK.store(false, Ordering::Relaxed);
        println!(
            "{name}: input={} cumulative_allocated={} budget_used={} elapsed_ms={} result={:?}",
            input.as_slice().len(),
            ALLOCATED.load(Ordering::Relaxed),
            budget.used(LimitCode::MemorySize),
            elapsed.as_millis(),
            result.as_ref().err().map(|e| e.kind())
        );
        assert!(result.is_err());
    }
}
