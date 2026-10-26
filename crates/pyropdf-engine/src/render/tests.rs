//! Rasters through the bridge equal pylopdf's: hayro's rendering of the
//! original bytes with pylopdf's raster geometry and default interpreter
//! settings, which is what pylopdf renders for an unedited document
//! (rust/src/document/rendering.rs at 884af0db).

use std::fs;
use std::path::Path;
use std::sync::Arc;

use pyropdf_store::budget::Budget;
use pyropdf_store::index::Index;
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::save::FileBase;
use pyropdf_store::value::Bytes;
use pyropdf_store::version::Document;

use super::*;

#[test]
fn corpus_pages_raster_at_1x_as_pylopdf_renders_the_original() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/assets/real_world");
    let mut files: Vec<_> = fs::read_dir(&dir)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.extension().is_some_and(|e| e == "pdf"))
        .collect();
    files.sort();
    assert_eq!(files.len(), 14);
    std::thread::scope(|scope| {
        for path in &files {
            scope.spawn(move || {
                let bytes = fs::read(path).unwrap();
                let budget = Budget::default();
                let index = Index::open(Bytes::new(bytes.clone()), &budget).unwrap();
                let base = FileBase::new(index, &budget).unwrap();
                let document = Document::new(Arc::new(base), None);
                let version = document.current();
                let pages = PageIndex::build(&mut Reads::new(&version, &budget)).unwrap();
                let bridged = Arc::new(Bridged::new(&version, &pages, &budget).unwrap());
                let original = Arc::new(Bridged::of_bytes(bytes));
                for at in 0..pages.len() {
                    for alpha in [false, true] {
                        let ours = raster(&bridged, at, (1.0, 0.0, 0.0, 1.0), alpha).unwrap();
                        let theirs = raster(&original, at, (1.0, 0.0, 0.0, 1.0), alpha).unwrap();
                        assert!(ours == theirs, "{} page {at} alpha={alpha}", path.display());
                        assert_eq!(ours.resolution, (72, 72));
                        assert_eq!(
                            ours.data.len(),
                            usize::from(ours.width)
                                * usize::from(ours.height)
                                * (3 + usize::from(alpha))
                        );
                    }
                }
            });
        }
    });
}

#[test]
fn geometry_follows_the_matrix() {
    assert_eq!(resolution((2.0, 0.0, 0.0, 2.0)), (144, 144));
    assert_eq!(resolution((0.0, 2.0, -2.0, 0.0)), (144, 144));
    assert_eq!(resolution((1.0, 0.0, 0.0, 3.0)), (72, 216));
}

#[test]
fn a_shared_render_cache_renders_as_a_fresh_one() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/assets/real_world");
    let mut files: Vec<_> = fs::read_dir(&dir)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.extension().is_some_and(|e| e == "pdf"))
        .collect();
    files.sort();
    std::thread::scope(|scope| {
        for path in &files {
            scope.spawn(move || {
                let bytes = fs::read(path).unwrap();
                let shared = Arc::new(Bridged::of_bytes(bytes.clone()));
                let other = Arc::new(Bridged::of_bytes(bytes));
                let pages = shared.pdf().pages().len();
                let identity = (0.5, 0.0, 0.0, 0.5);
                // Every page in turn through one cache, which earlier pages filled.
                let cached: Vec<_> = (0..pages)
                    .map(|at| raster(&shared, at, identity, true).unwrap())
                    .collect();
                for (at, cached) in cached.iter().enumerate() {
                    // Rendering another bridge first makes this one start a new cache.
                    raster(&other, 0, identity, true).unwrap();
                    let fresh = raster(&shared, at, identity, true).unwrap();
                    assert!(*cached == fresh, "{} page {at}", path.display());
                }
            });
        }
    });
}
