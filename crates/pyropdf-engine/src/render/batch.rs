//! Rendering many pages of one version on a bounded pool of threads, as
//! pylopdf's `Document.render` does (pylopdf rust/src/document/rendering.rs
//! at 036be111).

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::thread;

use hayro::RenderCache;

use super::{Options, Raster, RasterError, draw, extent};
use crate::bridge::Bridged;

/// The most workers that a batch renders on.
pub const MAX_WORKERS: usize = 64;

/// The estimated bytes of live rasters and conversion buffers that the
/// workers of one batch hold at once, as pylopdf bounds them.
const MAX_LIVE_BYTES: u64 = 512_000_000;

/// The estimated bytes that one pixel of a raster holds while it is
/// rendered and converted.
const BYTES_PER_PIXEL: u64 = 8;

/// The pages that a worker renders through one cache before it starts
/// another, so that a cache holds what at most this many pages use.
const CACHE_PAGES: usize = 4096;

/// Renders pages `indexes` of `bridged`, counted from 0 and possibly
/// repeated, through `options`, and passes each raster to `finish`; returns
/// what `finish` returns, in the order of `indexes`.
///
/// Every page is checked before any is rendered. The pages render on up to
/// `workers` threads, at most [`MAX_WORKERS`], the calling one among them,
/// and on fewer when their largest raster would hold more than 512 MB in
/// all. A thread that cannot start leaves its share to the others. The
/// first error stops the workers from starting another page.
pub fn batch<T, E>(
    bridged: &Bridged,
    indexes: &[usize],
    options: &Options,
    workers: usize,
    finish: impl Fn(Raster) -> Result<T, E> + Sync,
) -> Result<Vec<T>, E>
where
    T: Send,
    E: Send + From<RasterError>,
{
    let pdf = bridged.pdf();
    let pages = pdf.pages();
    let mut most = 0;
    for &index in indexes {
        let page = pages.get(index).ok_or(RasterError::NoPage(index))?;
        let (_, _, width, height) = extent(page, options)?;
        most = u64::max(most, width as u64 * height as u64);
    }
    let by_memory = usize::try_from(MAX_LIVE_BYTES / (most * BYTES_PER_PIXEL).max(1))
        .unwrap_or(usize::MAX)
        .max(1);
    let count = workers
        .clamp(1, MAX_WORKERS)
        .min(indexes.len())
        .min(by_memory);
    let next = AtomicUsize::new(0);
    let failed = AtomicBool::new(false);
    let work = || -> Result<Vec<(usize, T)>, E> {
        let mut done = Vec::new();
        let mut cache = RenderCache::new();
        let mut cached = 0;
        while !failed.load(Ordering::Relaxed) {
            let at = next.fetch_add(1, Ordering::Relaxed);
            let Some(&index) = indexes.get(at) else {
                break;
            };
            if cached == CACHE_PAGES {
                cache = RenderCache::new();
                cached = 0;
            }
            cached += 1;
            match draw(pdf, &cache, index, options)
                .map_err(E::from)
                .and_then(&finish)
            {
                Ok(value) => done.push((at, value)),
                Err(error) => {
                    failed.store(true, Ordering::Relaxed);
                    return Err(error);
                }
            }
        }
        Ok(done)
    };
    let parts = thread::scope(|scope| {
        let spawned: Vec<_> = (1..count)
            .filter_map(|_| {
                thread::Builder::new()
                    .name("pyropdf-render".to_owned())
                    .spawn_scoped(scope, work)
                    .ok()
            })
            .collect();
        let mut parts = vec![work()];
        for handle in spawned {
            parts.push(
                handle
                    .join()
                    .unwrap_or_else(|panic| std::panic::resume_unwind(panic)),
            );
        }
        parts
    });
    let mut ordered: Vec<Option<T>> = std::iter::repeat_with(|| None)
        .take(indexes.len())
        .collect();
    for part in parts {
        for (at, value) in part? {
            ordered[at] = Some(value);
        }
    }
    Ok(ordered
        .into_iter()
        .map(|value| value.expect("every page that a worker claimed is rendered"))
        .collect())
}
