//! Indexing and repair: any input opens or refuses, and every object of an
//! index that opens reads or refuses, without a panic.

#![no_main]

use libfuzzer_sys::fuzz_target;
use pyropdf_store::budget::{Budget, DocumentLimits};
use pyropdf_store::index::Index;
use pyropdf_store::save::FileBase;
use pyropdf_store::value::{Bytes, Reference, Value};

fuzz_target!(
    // The store turns a panic of hayro-syntax into an error (DESIGN.md I3),
    // but libFuzzer's hook aborts on any panic: keep the default hook, so
    // that only a panic that escapes the target is a crash.
    init: {
        let _ = std::panic::take_hook();
    },
    |data: &[u8]| {
    let budget = Budget::new(&DocumentLimits::web());
    let Ok(index) = Index::open(Bytes::new(data.to_vec()), &budget) else {
        return;
    };
    let entries: Vec<_> = index.entries().take(4096).collect();
    for (number, entry) in entries {
        if let Ok(Some(bytes)) = index.object(Reference::new(number, entry.generation()), &budget) {
            let _ = Value::parse(&bytes, &budget);
        }
    }
    let _ = FileBase::new(index, &budget);
    }
);
