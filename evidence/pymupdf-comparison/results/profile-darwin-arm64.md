# Rendering profile, darwin-arm64

Measured 2026-10-30 on macOS-27.0.1-arm64-arm-64bit-Mach-O, Python 3.15.0,
pyropdf from this checkout, sampled 10 s per case at 1 ms by `sample`.
Shares are of the samples of the worker's main thread. `profile.py` describes the method.

## Self time per group

Each sample is charged to its leaf frame's crate. Frames inlined into a caller count for the caller.

| Group | bill-hr815 72 dpi | bill-hr815 144 dpi | usrguide 72 dpi | usrguide 144 dpi |
| --- | ---: | ---: | ---: | ---: |
| vello_cpu | 76.2% | 67.3% | 71.2% | 64.4% |
| pyropdf | 9.7% | 20.2% | 7.9% | 18.0% |
| memcpy and memset | 6.1% | 7.7% | 4.0% | 7.3% |
| hayro-syntax | 0.8% | 0.5% | 3.7% | 2.2% |
| fonts | 0.4% | 0.2% | 3.1% | 2.0% |
| malloc and free | 1.0% | 1.2% | 2.0% | 1.4% |
| image and stream decoding | 0.4% | 0.3% | 2.8% | 1.6% |
| hash maps | 2.0% | 1.1% | 1.4% | 0.8% |
| hayro-interpret | 1.1% | 0.6% | 1.6% | 0.8% |
| Python and other | 1.3% | 0.4% | 0.3% | 0.2% |
| hayro | 0.6% | 0.3% | 0.7% | 0.3% |
| Rust std | 0.2% | 0.2% | 0.8% | 0.4% |
| kernel | 0.2% | 0.1% | 0.3% | 0.4% |
| other (smallvec) | 0.1% | 0.0% | 0.3% | 0.2% |
| kurbo | 0.0% | 0.0% | 0.1% | 0.1% |
| other (pxfm) | 0.0% | 0.0% | 0.0% | 0.0% |

## Phases

Inclusive samples of the outermost frame of each phase.

| Phase | bill-hr815 72 dpi | bill-hr815 144 dpi | usrguide 72 dpi | usrguide 144 dpi |
| --- | ---: | ---: | ---: | ---: |
| page raster (pyropdf_engine::render::draw) | 95.3% | 96.0% | 97.3% | 95.3% |
| interpret and encode (hayro::render_into) | 78.0% | 60.4% | 82.7% | 67.1% |
| glyph fills (hayro::Renderer::fill_outline_run) | 73.4% | 57.6% | 69.4% | 59.0% |
| rasterize (vello_cpu Dispatcher::rasterize) | 6.8% | 13.5% | 6.2% | 9.1% |
| take pixels (pyropdf_engine::render::take) | 0.0% | 0.0% | 0.0% | 0.0% |
| RGBA to RGB (pyropdf_engine::render::to_layout) | 9.6% | 20.1% | 7.7% | 17.9% |
| store, bridge, and open (pyropdf) | 0.2% | 0.1% | 1.2% | 0.7% |

## Pages

Best of three renders of each page, in ms, on a document opened once: every page, then the five
where pyropdf spends the most time. PyMuPDF and pyropdf read the samples; "with PNG" is what
`bench.py` times for pyropdf, `to_bytes()`, which encodes a PNG.

### bill-hr815 72 dpi

| Page | PyMuPDF | pyropdf | ratio | pyropdf with PNG | ratio |
| ---: | ---: | ---: | ---: | ---: | ---: |
| all | 79.5 | 408.7 | 5.14 | 601.2 | 7.56 |
| 25 | 0.9 | 4.7 | 5.21 | 6.6 | 7.43 |
| 35 | 0.8 | 4.6 | 5.50 | 6.4 | 7.67 |
| 23 | 0.9 | 4.5 | 4.93 | 6.4 | 7.06 |
| 9 | 0.8 | 4.4 | 5.19 | 6.3 | 7.42 |
| 26 | 0.8 | 4.4 | 5.23 | 6.3 | 7.60 |

### bill-hr815 144 dpi

| Page | PyMuPDF | pyropdf | ratio | pyropdf with PNG | ratio |
| ---: | ---: | ---: | ---: | ---: | ---: |
| all | 138.3 | 766.1 | 5.54 | 1503.9 | 10.88 |
| 25 | 1.4 | 8.2 | 5.70 | 15.5 | 10.79 |
| 7 | 1.4 | 8.1 | 5.76 | 14.9 | 10.66 |
| 9 | 1.4 | 8.0 | 5.73 | 15.4 | 10.96 |
| 28 | 1.4 | 8.0 | 5.73 | 14.7 | 10.49 |
| 26 | 1.4 | 8.0 | 5.71 | 15.1 | 10.80 |

### usrguide 72 dpi

| Page | PyMuPDF | pyropdf | ratio | pyropdf with PNG | ratio |
| ---: | ---: | ---: | ---: | ---: | ---: |
| all | 16.7 | 77.1 | 4.61 | 109.2 | 6.53 |
| 3 | 0.8 | 3.7 | 4.92 | 4.8 | 6.40 |
| 17 | 0.7 | 3.5 | 4.84 | 4.8 | 6.59 |
| 4 | 0.8 | 3.4 | 4.46 | 4.8 | 6.32 |
| 24 | 0.7 | 3.4 | 4.69 | 4.8 | 6.65 |
| 14 | 0.8 | 3.2 | 4.32 | 4.4 | 5.81 |

### usrguide 144 dpi

| Page | PyMuPDF | pyropdf | ratio | pyropdf with PNG | ratio |
| ---: | ---: | ---: | ---: | ---: | ---: |
| all | 27.0 | 135.4 | 5.01 | 250.2 | 9.26 |
| 3 | 1.2 | 6.1 | 5.15 | 10.7 | 9.02 |
| 17 | 1.1 | 6.1 | 5.40 | 10.6 | 9.46 |
| 24 | 1.2 | 5.7 | 4.70 | 10.4 | 8.67 |
| 14 | 1.1 | 5.6 | 4.94 | 10.1 | 8.83 |
| 20 | 1.1 | 5.6 | 5.03 | 9.8 | 8.70 |

## Hottest functions

Self samples, as shares of each case.

### bill-hr815 72 dpi

| Function | Self |
| --- | ---: |
| `vello_common::flatten::fill::<&kurbo::bezpath::BezPath> (vello_cpu)` | 22.6% |
| `vello_common::strip::render (vello_cpu)` | 20.1% |
| `<vello_common::tile::Tiles>::make_tiles_analytic_aa (vello_cpu)` | 15.4% |
| `pyropdf_engine::render::to_layout (pyropdf)` | 9.6% |
| `core::slice::sort::unstable::quicksort::quicksort::<vello_common::tile::Tile, <vello_common::tile::Tile as core::cmp::PartialOrd>::lt> (v...` | 7.4% |
| `_platform_memmove (memcpy and memset)` | 5.1% |
| `vello_common::clip::intersect (vello_cpu)` | 2.4% |
| `<vello_cpu::dispatch::single_threaded::SingleThreadedDispatcher>::bucket_and_rasterize::<fearless_simd::generated::neon::Neon, vello_cpu:...` | 1.8% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::paint_fill (vello_cpu)` | 1.8% |
| `<vello_cpu::coarse::bucketer::CommandBucketer>::bucket_commands (vello_cpu)` | 1.5% |
| `<hashbrown::map::HashMap<alloc::string::String, font_types::glyph_id::GlyphId, rustc_hash::FxBuildHasher>>::get::<str> (hash maps)` | 1.0% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::init_uncovered_range (vello_cpu)` | 0.8% |
| `hayro_interpret::interpret::text::append_text_string (hayro-interpret)` | 0.6% |
| `__bzero (memcpy and memset)` | 0.6% |
| `<vello_cpu::dispatch::single_threaded::SingleThreadedDispatcher>::record_fill (vello_cpu)` | 0.6% |

Self samples of `pyropdf_engine::render::draw` by source line, which name what it inlines:


### bill-hr815 144 dpi

| Function | Self |
| --- | ---: |
| `pyropdf_engine::render::to_layout (pyropdf)` | 20.1% |
| `vello_common::flatten::fill::<&kurbo::bezpath::BezPath> (vello_cpu)` | 18.5% |
| `vello_common::strip::render (vello_cpu)` | 15.2% |
| `<vello_common::tile::Tiles>::make_tiles_analytic_aa (vello_cpu)` | 11.7% |
| `<vello_cpu::dispatch::single_threaded::SingleThreadedDispatcher>::bucket_and_rasterize::<fearless_simd::generated::neon::Neon, vello_cpu:...` | 7.8% |
| `core::slice::sort::unstable::quicksort::quicksort::<vello_common::tile::Tile, <vello_common::tile::Tile as core::cmp::PartialOrd>::lt> (v...` | 5.9% |
| `_platform_memmove (memcpy and memset)` | 5.8% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::paint_fill (vello_cpu)` | 2.1% |
| `vello_common::clip::intersect (vello_cpu)` | 1.9% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::init_uncovered_range (vello_cpu)` | 1.7% |
| `__bzero (memcpy and memset)` | 1.5% |
| `<vello_cpu::coarse::bucketer::CommandBucketer>::bucket_commands (vello_cpu)` | 0.8% |
| `<hashbrown::map::HashMap<alloc::string::String, font_types::glyph_id::GlyphId, rustc_hash::FxBuildHasher>>::get::<str> (hash maps)` | 0.6% |
| `hayro_interpret::interpret::text::append_text_string (hayro-interpret)` | 0.3% |
| `_platform_memset (memcpy and memset)` | 0.3% |

Self samples of `pyropdf_engine::render::draw` by source line, which name what it inlines:


### usrguide 72 dpi

| Function | Self |
| --- | ---: |
| `vello_common::flatten::fill::<&kurbo::bezpath::BezPath> (vello_cpu)` | 22.9% |
| `vello_common::strip::render (vello_cpu)` | 18.2% |
| `<vello_common::tile::Tiles>::make_tiles_analytic_aa (vello_cpu)` | 13.3% |
| `pyropdf_engine::render::to_layout (pyropdf)` | 7.7% |
| `core::slice::sort::unstable::quicksort::quicksort::<vello_common::tile::Tile, <vello_common::tile::Tile as core::cmp::PartialOrd>::lt> (v...` | 6.7% |
| `_platform_memmove (memcpy and memset)` | 3.0% |
| `vello_common::clip::intersect (vello_cpu)` | 2.4% |
| `<vello_cpu::dispatch::single_threaded::SingleThreadedDispatcher>::bucket_and_rasterize::<fearless_simd::generated::neon::Neon, vello_cpu:...` | 2.1% |
| `zlib_rs::inflate::inflate_fast_help (image and stream decoding)` | 1.6% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::paint_fill (vello_cpu)` | 1.3% |
| `<vello_cpu::coarse::bucketer::CommandBucketer>::bucket_commands (vello_cpu)` | 1.2% |
| `hayro_interpret::interpret::text::append_text_string (hayro-interpret)` | 0.8% |
| `zlib_rs::inflate::inflate (image and stream decoding)` | 0.7% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::init_uncovered_range (vello_cpu)` | 0.7% |
| `<read_fonts::ps::type1::Type1Font>::new (fonts)` | 0.7% |

Self samples of `pyropdf_engine::render::draw` by source line, which name what it inlines:


### usrguide 144 dpi

| Function | Self |
| --- | ---: |
| `vello_common::flatten::fill::<&kurbo::bezpath::BezPath> (vello_cpu)` | 19.5% |
| `pyropdf_engine::render::to_layout (pyropdf)` | 17.9% |
| `vello_common::strip::render (vello_cpu)` | 15.1% |
| `<vello_common::tile::Tiles>::make_tiles_analytic_aa (vello_cpu)` | 12.0% |
| `core::slice::sort::unstable::quicksort::quicksort::<vello_common::tile::Tile, <vello_common::tile::Tile as core::cmp::PartialOrd>::lt> (v...` | 6.4% |
| `_platform_memmove (memcpy and memset)` | 5.7% |
| `<vello_cpu::dispatch::single_threaded::SingleThreadedDispatcher>::bucket_and_rasterize::<fearless_simd::generated::neon::Neon, vello_cpu:...` | 4.3% |
| `vello_common::clip::intersect (vello_cpu)` | 1.7% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::paint_fill (vello_cpu)` | 1.7% |
| `<vello_cpu::fine::Fine<fearless_simd::generated::neon::Neon, vello_cpu::fine::lowp::U8Kernel>>::init_uncovered_range (vello_cpu)` | 1.4% |
| `__bzero (memcpy and memset)` | 1.1% |
| `zlib_rs::inflate::inflate_fast_help (image and stream decoding)` | 1.0% |
| `<vello_cpu::coarse::bucketer::CommandBucketer>::bucket_commands (vello_cpu)` | 1.0% |
| `<read_fonts::ps::type1::Type1Font>::new (fonts)` | 0.5% |
| `hayro_interpret::interpret::text::append_text_string (hayro-interpret)` | 0.5% |

Self samples of `pyropdf_engine::render::draw` by source line, which name what it inlines:

