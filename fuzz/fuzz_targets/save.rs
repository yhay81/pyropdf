//! Saving after transactions: edits that the input's last bytes choose,
//! then a full and an incremental save, whose output reopens and reads the
//! written values back.

#![no_main]

use std::sync::Arc;

use libfuzzer_sys::fuzz_target;
use pyropdf_store::budget::{Budget, DocumentLimits};
use pyropdf_store::save::{self, FileBase};
use pyropdf_store::value::{Bytes, Value};
use pyropdf_store::version::Document;

fuzz_target!(
    // The store turns a panic of hayro-syntax into an error (DESIGN.md I3),
    // but libFuzzer's hook aborts on any panic: keep the default hook, so
    // that only a panic that escapes the target is a crash.
    init: {
        let _ = std::panic::take_hook();
    },
    |data: &[u8]| {
    let budget = Budget::new(&DocumentLimits::web());
    let Ok(base) = FileBase::open(Bytes::new(data.to_vec()), &budget) else {
        return;
    };
    let numbers: Vec<u32> = base.index().entries().map(|(number, _)| number).take(64).collect();
    // A file whose trailer names a free object reads as damaged, and is
    // repaired from what the scan finds: keep what the trailer names.
    let named: Vec<u32> = base
        .index()
        .trailer()
        .iter()
        .filter_map(|(_, value)| match value {
            Value::Reference(reference) => Some(reference.number()),
            _ => None,
        })
        .collect();
    let document = Document::new(Arc::new(base), None);
    let mut written = Vec::new();
    let mut transaction = document.begin();
    for (step, &choice) in data.iter().rev().take(6).enumerate() {
        let Some(&number) = numbers.get(usize::from(choice) % numbers.len().max(1)) else {
            break;
        };
        if choice % 5 == 0 && !named.contains(&number) {
            let _ = transaction.free(number, &budget);
            written.retain(|&(n, _)| n != number);
        } else if transaction.write(number, &Value::Integer(step as i64), &budget).is_ok() {
            written.retain(|&(n, _)| n != number);
            written.push((number, step as i64));
        }
    }
    let Ok(version) = transaction.commit(&budget) else {
        return;
    };
    for incremental in [false, true] {
        let mut out = Vec::new();
        let saved = if incremental {
            save::incremental(&version, &mut out, &budget)
        } else {
            save::full(&version, &mut out, &budget)
        };
        if saved.is_err() {
            continue;
        }
        if incremental {
            assert!(out.starts_with(data), "an incremental save keeps the original");
        }
        // A version whose trailer names no catalog in use saves as a file
        // that reads as damaged: the store's index requires the catalog.
        let catalog = match version.trailer().get("Root") {
            Some(Value::Reference(root)) => version.generation(root.number()) == Some(root.generation()),
            _ => false,
        };
        let reopened = match FileBase::open(Bytes::new(out), &budget) {
            Ok(reopened) => reopened,
            Err(_) if !catalog => continue,
            Err(error) => panic!("a save reopens (incremental={incremental}): {error}"),
        };
        let reopened = Document::new(Arc::new(reopened), None).current();
        if reopened.base().file().is_some_and(|index| index.is_repaired()) {
            // A damaged input saves as damaged, and reads back repaired.
            continue;
        }
        for &(number, value) in &written {
            if let Ok(Some(object)) = reopened.get(number, &budget) {
                assert_eq!(**object.value(), Value::Integer(value), "object {number}");
            }
        }
    }
    }
);
