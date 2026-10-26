//! Value conversion and writing: a value that parses writes bytes that
//! parse to the same value, both keeping lexemes and canonically.

#![no_main]

use libfuzzer_sys::fuzz_target;
use pyropdf_store::budget::{Budget, BudgetVec, DocumentLimits};
use pyropdf_store::value::{Bytes, Value};
use pyropdf_store::write;

fuzz_target!(
    // The store turns a panic of hayro-syntax into an error (DESIGN.md I3),
    // but libFuzzer's hook aborts on any panic: keep the default hook, so
    // that only a panic that escapes the target is a crash.
    init: {
        let _ = std::panic::take_hook();
    },
    |data: &[u8]| {
    let budget = Budget::new(&DocumentLimits::web());
    let Ok(value) = Value::parse(&Bytes::new(data.to_vec()), &budget) else {
        return;
    };
    for canonical in [false, true] {
        let mut out = BudgetVec::new(&budget);
        let written = if canonical {
            write::canonical(&mut out, &value)
        } else {
            write::value(&mut out, &value)
        };
        if written.is_err() {
            continue;
        }
        let reread = Value::parse(&Bytes::new(out.into_vec()), &budget)
            .expect("written bytes parse again");
        assert_eq!(reread, value, "canonical={canonical}");
    }
    }
);
