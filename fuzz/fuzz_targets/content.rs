//! Content streams: any bytes that tokenize split into contiguous
//! operations whose bytes, and the tail after them, are the bytes.

#![no_main]

use libfuzzer_sys::fuzz_target;
use pyropdf_engine::content::Operations;
use pyropdf_store::budget::{Budget, DocumentLimits};
use pyropdf_store::value::Bytes;

fuzz_target!(
    // The parser turns a panic of hayro-syntax into an error (DESIGN.md I3),
    // but libFuzzer's hook aborts on any panic: keep the default hook, so
    // that only a panic that escapes the target is a crash.
    init: {
        let _ = std::panic::take_hook();
    },
    |data: &[u8]| {
    let budget = Budget::new(&DocumentLimits::web());
    let Ok(operations) = Operations::parse(Bytes::new(data.to_vec()), &budget) else {
        return;
    };
    let mut end = 0;
    for operation in operations.operations() {
        let (span, operator) = (operation.span(), operation.operator_span());
        assert_eq!(span.start, end, "operations are contiguous");
        assert!(span.start <= operator.start && operator.end <= span.end);
        if let Some(image) = operation.image_span() {
            assert!(operator.end <= image.start && image.end <= span.end);
        }
        end = span.end;
    }
    assert_eq!(operations.tail_start(), end);
    assert!(end <= data.len());
    }
);
