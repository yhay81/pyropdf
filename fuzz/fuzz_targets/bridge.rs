//! The hayro bridge: a document that opens and walks its page tree is
//! handed to hayro, and its first page renders at a small scale.

#![no_main]

use std::sync::Arc;

use libfuzzer_sys::fuzz_target;
use pyropdf_engine::bridge::Bridged;
use pyropdf_engine::render;
use pyropdf_store::budget::{Budget, DocumentLimits};
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::save::FileBase;
use pyropdf_store::value::Bytes;
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
    let document = Document::new(Arc::new(base), None);
    let version = document.current();
    let Ok(pages) = PageIndex::build(&mut Reads::new(&version, &budget)) else {
        return;
    };
    let Ok(bridged) = Bridged::new(&version, &pages, &budget) else {
        return;
    };
    if !pages.is_empty() {
        let _ = render::raster(&bridged, 0, (0.1, 0.0, 0.0, 0.1), false);
    }
    }
);
