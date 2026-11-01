use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Instant;
use unicode_bidi::{BidiInfo, ParagraphBidiInfo};

struct Meter;
static ALLOCATED: AtomicUsize = AtomicUsize::new(0);
unsafe impl GlobalAlloc for Meter {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        ALLOCATED.fetch_add(layout.size(), Ordering::Relaxed);
        System.alloc(layout)
    }
    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        System.dealloc(ptr, layout)
    }
    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, size: usize) -> *mut u8 {
        ALLOCATED.fetch_add(size, Ordering::Relaxed);
        System.realloc(ptr, layout, size)
    }
}
#[global_allocator]
static GLOBAL: Meter = Meter;
fn main() {
    for n in [500, 1000, 2000, 4000, 8000] {
        let text = "a\n".repeat(n);
        let bidi = BidiInfo::new(&text, None);
        ALLOCATED.store(0, Ordering::Relaxed);
        let start = Instant::now();
        let mut runs_total = 0;
        for paragraph in &bidi.paragraphs {
            let (levels, runs) = bidi.visual_runs(paragraph, paragraph.range.clone());
            std::hint::black_box(levels);
            runs_total += runs.len();
        }
        let bytes = ALLOCATED.load(Ordering::Relaxed);
        let elapsed = start.elapsed().as_secs_f64() * 1000.0;
        ALLOCATED.store(0, Ordering::Relaxed);
        for paragraph in &bidi.paragraphs {
            let part =
                ParagraphBidiInfo::new(&text[paragraph.range.clone()], Some(paragraph.level));
            std::hint::black_box(part.visual_runs(0..paragraph.range.len()));
        }
        let alternative_bytes = ALLOCATED.load(Ordering::Relaxed);
        println!("input_bytes={} paragraphs={} allocated_bytes={} elapsed_ms={:.3} runs={} alternative_allocated_bytes={}",
            text.len(), bidi.paragraphs.len(), bytes, elapsed, runs_total, alternative_bytes);
    }
}
