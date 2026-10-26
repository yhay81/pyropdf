//! Profiles rendering every page of an unedited version at 18 dpi: hayro
//! with a fresh `RenderCache` per page, as `render::raster` rendered before
//! issue #75; hayro with one cache for every page, as pylopdf renders; and
//! `render::raster` now. The arguments are the documents of
//! evidence/edit-render; it prints a Markdown table.

use std::sync::Arc;
use std::time::Instant;

use hayro::hayro_interpret::InterpreterSettings;
use hayro::vello_cpu::color::AlphaColor;
use hayro::{PixmapSettings, RenderCache, RenderSettings};
use pyropdf_engine::bridge::Bridged;
use pyropdf_engine::render;
use pyropdf_store::budget::Budget;
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::save::FileBase;
use pyropdf_store::value::Bytes;
use pyropdf_store::version::Document;

const SCALE: f32 = 0.25;

fn main() {
    let settings = PixmapSettings {
        x_scale: SCALE,
        y_scale: SCALE,
        bg_color: AlphaColor::WHITE,
    };
    println!("| Pages | Fresh cache per page | One cache per version | `render::raster` |");
    println!("| ---: | ---: | ---: | ---: |");
    for path in std::env::args().skip(1) {
        let budget = Budget::default();
        let bytes = std::fs::read(&path).unwrap();
        let base = FileBase::open(Bytes::new(bytes), &budget).unwrap();
        let document = Document::new(Arc::new(base), None);
        let version = document.current();
        let pages = PageIndex::build(&mut Reads::new(&version, &budget)).unwrap();
        let bridged = Arc::new(Bridged::new(&version, &pages, &budget).unwrap());
        let count = pages.len();
        let per_page = |started: Instant| started.elapsed().as_secs_f64() * 1e3 / count as f64;
        let render = |page, cache: &RenderCache<'_>| {
            hayro::render(
                page,
                cache,
                &InterpreterSettings::default(),
                &RenderSettings::default(),
                &settings,
            )
        };
        let started = Instant::now();
        for page in bridged.pdf().pages().iter() {
            let cache = RenderCache::new();
            hayro::render(
                page,
                &cache,
                &InterpreterSettings::default(),
                &RenderSettings::default(),
                &settings,
            );
        }
        let fresh = per_page(started);
        let cache = RenderCache::new();
        let started = Instant::now();
        for page in bridged.pdf().pages().iter() {
            hayro::render(
                page,
                &cache,
                &InterpreterSettings::default(),
                &RenderSettings::default(),
                &settings,
            );
        }
        let shared = per_page(started);
        let scale = f64::from(SCALE);
        let started = Instant::now();
        for at in 0..count {
            render::raster(&bridged, at, (scale, 0.0, 0.0, scale), false).unwrap();
        }
        let ours = per_page(started);
        println!("| {count} | {fresh:.3} ms | {shared:.3} ms | {ours:.3} ms |");
    }
}
