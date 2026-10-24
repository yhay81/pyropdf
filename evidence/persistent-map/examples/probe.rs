//! A shared library that holds one map's code, built once per `probe-*`
//! feature by `run.sh`. The size above the baseline's is what the map adds
//! to a binary: the operations that versions need, monomorphized for the
//! slots they store, and nothing else.

use persistent_map_bench::maps::Map;
use persistent_map_bench::slot::Slot;

#[cfg(feature = "probe-baseline")]
type Probe = persistent_map_bench::maps::VecTable;
#[cfg(feature = "probe-rpds-hash")]
type Probe = persistent_map_bench::maps::RpdsHash;
#[cfg(feature = "probe-rpds-tree")]
type Probe = persistent_map_bench::maps::RpdsTree;
#[cfg(feature = "probe-im-hash")]
type Probe = persistent_map_bench::maps::ImHash;
#[cfg(feature = "probe-im-ord")]
type Probe = persistent_map_bench::maps::ImOrd;
#[cfg(feature = "probe-imbl-hash")]
type Probe = persistent_map_bench::maps::ImblHash;
#[cfg(feature = "probe-imbl-ord")]
type Probe = persistent_map_bench::maps::ImblOrd;
#[cfg(feature = "probe-chunks")]
type Probe = persistent_map_bench::chunks::ChunkTable<Slot, 16, 32>;

#[unsafe(no_mangle)]
pub extern "C" fn probe_new() -> *mut Probe {
    Box::into_raw(Box::new(Probe::new()))
}

/// # Safety
///
/// `map` comes from `probe_new` or `probe_clone` and is not freed.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn probe_clone(map: *const Probe) -> *mut Probe {
    // SAFETY: the caller passes a live map.
    Box::into_raw(Box::new(unsafe { &*map }.clone()))
}

/// # Safety
///
/// `map` comes from `probe_new` or `probe_clone` and is not freed.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn probe_free(map: *mut Probe) {
    // SAFETY: the caller passes a live map, which this frees.
    drop(unsafe { Box::from_raw(map) });
}

/// # Safety
///
/// `map` comes from `probe_new` or `probe_clone` and is not freed.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn probe_insert(map: *mut Probe, key: u32, generation: u16, free: bool) {
    let slot = if free {
        Slot::Free(generation)
    } else {
        Slot::written(generation)
    };
    // SAFETY: the caller passes a live map.
    unsafe { &mut *map }.insert(key, slot);
}

/// # Safety
///
/// `map` comes from `probe_new` or `probe_clone` and is not freed.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn probe_get(map: *const Probe, key: u32) -> u64 {
    // SAFETY: the caller passes a live map.
    unsafe { &*map }.get(key).map_or(0, Slot::tag)
}

/// # Safety
///
/// `map` comes from `probe_new` or `probe_clone` and is not freed.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn probe_len(map: *const Probe) -> usize {
    // SAFETY: the caller passes a live map.
    unsafe { &*map }.len()
}

/// # Safety
///
/// `map` comes from `probe_new` or `probe_clone` and is not freed.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn probe_walk(map: *const Probe, visit: extern "C" fn(u32, u64)) {
    // SAFETY: the caller passes a live map.
    unsafe { &*map }.for_each_ordered(|key, value| visit(key, value.tag()));
}
