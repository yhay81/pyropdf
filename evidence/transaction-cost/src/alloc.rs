//! A global allocator that counts bytes and blocks while a measurement runs,
//! the one of evidence/persistent-map. The bytes are the sizes requested,
//! before the system allocator rounds them up to its size classes.

use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::Ordering::Relaxed;
use std::sync::atomic::{AtomicBool, AtomicIsize};

pub struct Counting;

static ON: AtomicBool = AtomicBool::new(false);
static BYTES: AtomicIsize = AtomicIsize::new(0);
static BLOCKS: AtomicIsize = AtomicIsize::new(0);

/// Bytes and blocks allocated, less those freed.
#[derive(Clone, Copy)]
pub struct Usage {
    pub bytes: isize,
    pub blocks: isize,
}

/// Runs `f` with counting on and returns what it allocated, less what it
/// freed. Timed code runs with counting off.
pub fn measure<T>(f: impl FnOnce() -> T) -> (T, Usage) {
    let (bytes, blocks) = (BYTES.load(Relaxed), BLOCKS.load(Relaxed));
    ON.store(true, Relaxed);
    let value = f();
    ON.store(false, Relaxed);
    let usage = Usage {
        bytes: BYTES.load(Relaxed) - bytes,
        blocks: BLOCKS.load(Relaxed) - blocks,
    };
    (value, usage)
}

fn count(bytes: usize, sign: isize, blocks: isize) {
    if ON.load(Relaxed) {
        BYTES.fetch_add(sign * bytes as isize, Relaxed);
        BLOCKS.fetch_add(blocks, Relaxed);
    }
}

// SAFETY: every method forwards to `System` with the caller's arguments.
unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        // SAFETY: the caller upholds `GlobalAlloc::alloc`'s contract.
        let ptr = unsafe { System.alloc(layout) };
        if !ptr.is_null() {
            count(layout.size(), 1, 1);
        }
        ptr
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        // SAFETY: the caller upholds `GlobalAlloc::alloc_zeroed`'s contract.
        let ptr = unsafe { System.alloc_zeroed(layout) };
        if !ptr.is_null() {
            count(layout.size(), 1, 1);
        }
        ptr
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        // SAFETY: the caller upholds `GlobalAlloc::dealloc`'s contract.
        unsafe { System.dealloc(ptr, layout) };
        count(layout.size(), -1, -1);
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        // SAFETY: the caller upholds `GlobalAlloc::realloc`'s contract.
        let new = unsafe { System.realloc(ptr, layout, new_size) };
        if !new.is_null() {
            count(layout.size(), -1, 0);
            count(new_size, 1, 0);
        }
        new
    }
}
