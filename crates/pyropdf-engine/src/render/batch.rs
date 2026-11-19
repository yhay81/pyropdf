//! Rendering many pages of one version on a bounded pool of threads, as
//! pylopdf's `Document.render` does (pylopdf rust/src/document/rendering.rs
//! at 036be111).

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::thread;

use hayro::RenderCache;
use pyropdf_store::pages::PageIndex;

use super::{
    Fallback, Options, Raster, RasterError, Scale, draw, extent, page_size, scaled_options,
};
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
/// repeated, through `options`, with `fallback` drawing the CJK fonts that
/// the document does not embed, and passes each raster to `finish`;
/// returns what `finish` returns, in the order of `indexes`.
///
/// Every page is checked before any is rendered. The pages render on up to
/// `workers` threads, at most [`MAX_WORKERS`], the calling one among them,
/// and on fewer when their largest raster would hold more than 512 MB in
/// all. A thread that cannot start leaves its share to the others. The
/// first error stops the workers from starting another page.
///
/// Each worker renders through a cache of its own, which also holds the
/// glyphs that [`Options::glyph_cache`] rasterizes, and drops it after
/// 4,096 pages and when the batch ends.
pub fn batch<T, E>(
    bridged: &Bridged,
    fallback: &Fallback,
    indexes: &[usize],
    options: &Options,
    workers: usize,
    finish: impl Fn(Raster) -> Result<T, E> + Sync,
) -> Result<Vec<T>, E>
where
    T: Send,
    E: Send + From<RasterError>,
{
    batch_scaled(
        bridged,
        None,
        fallback,
        indexes,
        options,
        Scale::Matrix(options.matrix),
        workers,
        finish,
    )
}

/// Renders pages `indexes` of `bridged` as [`batch`] does, with `scale`
/// choosing each page's linear map from its display size in `pages` (or
/// `bridged` when `pages` is `None`).
#[expect(
    clippy::too_many_arguments,
    reason = "batch options with per-page scale"
)]
pub fn batch_scaled<T, E>(
    bridged: &Bridged,
    pages: Option<&PageIndex>,
    fallback: &Fallback,
    indexes: &[usize],
    options: &Options,
    scale: Scale,
    workers: usize,
    finish: impl Fn(Raster) -> Result<T, E> + Sync,
) -> Result<Vec<T>, E>
where
    T: Send,
    E: Send + From<RasterError>,
{
    if matches!(scale, Scale::Fit(0, _) | Scale::Fit(_, 0)) {
        return Err(E::from(RasterError::Geometry(
            "fit dimensions must be positive".to_owned(),
        )));
    }
    let mut most = 0;
    for &index in indexes {
        let (_, display) = bridged.page(index).ok_or(RasterError::NoPage(index))?;
        let resolved = scaled_options(options, scale, page_size(pages, index, display))?;
        let (_, _, width, height) = extent(display, &resolved)?;
        most = u64::max(most, u64::from(width) * u64::from(height));
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
            match draw(bridged, pages, &cache, fallback, index, options, scale)
                .map_err(E::from)
                .and_then(&finish)
            {
                #[expect(
                    clippy::disallowed_methods,
                    reason = "one result per page of `indexes`, which the caller holds"
                )]
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
        #[expect(
            clippy::disallowed_methods,
            reason = "one thread per worker, and MAX_WORKERS at most"
        )]
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
            #[expect(
                clippy::disallowed_methods,
                reason = "one part per worker, and MAX_WORKERS at most"
            )]
            parts.push(
                handle
                    .join()
                    .unwrap_or_else(|panic| std::panic::resume_unwind(panic)),
            );
        }
        parts
    });
    #[expect(
        clippy::disallowed_methods,
        reason = "one result per page of `indexes`, which the caller holds"
    )]
    let mut ordered: Vec<Option<T>> = std::iter::repeat_with(|| None)
        .take(indexes.len())
        .collect();
    for part in parts {
        for (at, value) in part? {
            ordered[at] = Some(value);
        }
    }
    #[expect(
        clippy::disallowed_methods,
        reason = "one result per page of `indexes`, which the caller holds"
    )]
    let rendered = ordered
        .into_iter()
        .map(|value| value.expect("every page that a worker claimed is rendered"))
        .collect();
    Ok(rendered)
}
