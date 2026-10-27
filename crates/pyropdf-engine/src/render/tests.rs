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
                        let ours = raster(&bridged, at, &Options::new((1.0, 0.0, 0.0, 1.0), alpha))
                            .unwrap();
                        let theirs =
                            raster(&original, at, &Options::new((1.0, 0.0, 0.0, 1.0), alpha))
                                .unwrap();
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
                    .map(|at| raster(&shared, at, &Options::new(identity, true)).unwrap())
                    .collect();
                for (at, cached) in cached.iter().enumerate() {
                    // Rendering another bridge first makes this one start a new cache.
                    raster(&other, 0, &Options::new(identity, true)).unwrap();
                    let fresh = raster(&shared, at, &Options::new(identity, true)).unwrap();
                    assert!(*cached == fresh, "{} page {at}", path.display());
                }
            });
        }
    });
}

#[test]
fn a_batch_renders_each_entry_as_raster_does_in_order() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/assets/real_world/pdfium-links-highlights-annots.pdf");
    let bridged = Arc::new(Bridged::of_bytes(fs::read(path).unwrap()));
    let options = Options::new((0.5, 0.0, 0.0, 0.5), true);
    let count = bridged.pdf().pages().len();
    let indexes: Vec<usize> = (0..count).rev().chain(0..count).collect();
    let alone: Vec<Raster> = indexes
        .iter()
        .map(|&at| raster(&bridged, at, &options).unwrap())
        .collect();

    for workers in [1, 3, MAX_WORKERS] {
        let batched = batch(&bridged, &indexes, &options, workers, |raster| {
            Ok::<_, RasterError>(raster)
        })
        .unwrap();
        assert!(batched == alone, "workers={workers}");
    }
}

#[test]
fn a_batch_checks_every_page_before_rendering_any() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/assets/real_world/pdfium-type3.pdf");
    let bridged = Bridged::of_bytes(fs::read(path).unwrap());
    let count = bridged.pdf().pages().len();
    let options = Options::new((1.0, 0.0, 0.0, 1.0), false);
    let rendered = std::sync::atomic::AtomicUsize::new(0);

    let result = batch(&bridged, &[0, count, 0], &options, 2, |raster| {
        rendered.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        Ok::<_, RasterError>(raster)
    });

    assert_eq!(result.unwrap_err(), RasterError::NoPage(count));
    assert_eq!(rendered.into_inner(), 0);
    let failed = batch(&bridged, &[0, 0, 0, 0], &options, 2, |_| {
        Err::<Raster, _>(RasterError::Clip("stop".to_owned()))
    });
    assert_eq!(failed.unwrap_err(), RasterError::Clip("stop".to_owned()));
}
