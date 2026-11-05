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
    assert_eq!(files.len(), 15);
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
                let none = Fallback::default();
                for at in 0..pages.len() {
                    for alpha in [false, true] {
                        let options = Options::new((1.0, 0.0, 0.0, 1.0), alpha);
                        let ours = raster(&bridged, &none, at, &options).unwrap();
                        let theirs = raster(&original, &none, at, &options).unwrap();
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
                let options = Options::new((0.5, 0.0, 0.0, 0.5), true);
                let none = Fallback::default();
                // Every page in turn through one cache, which earlier pages filled.
                let cached: Vec<_> = (0..pages)
                    .map(|at| raster(&shared, &none, at, &options).unwrap())
                    .collect();
                for (at, cached) in cached.iter().enumerate() {
                    // Rendering another bridge first makes this one start a new cache.
                    raster(&other, &none, 0, &options).unwrap();
                    let fresh = raster(&shared, &none, at, &options).unwrap();
                    assert!(*cached == fresh, "{} page {at}", path.display());
                }
            });
        }
    });
}

/// The options of a raster at 72 dpi on white, hinted or not.
fn hinted_options(hinting: bool) -> Options {
    Options {
        hinting,
        ..Options::new((1.0, 0.0, 0.0, 1.0), false)
    }
}

/// The options of a raster at 72 dpi on white through the glyph cache,
/// hinted or not.
fn cached_options(hinting: bool) -> Options {
    Options {
        glyph_cache: true,
        ..hinted_options(hinting)
    }
}

/// A corpus document with text in an embedded font.
fn pdf20_simple() -> Arc<Bridged> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/assets/real_world/pdf20-simple.pdf");
    Arc::new(Bridged::of_bytes(fs::read(path).unwrap()))
}

/// A corpus form of two pages of text.
fn f1040() -> Arc<Bridged> {
    let path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/assets/real_world/f1040.pdf");
    Arc::new(Bridged::of_bytes(fs::read(path).unwrap()))
}

/// hayro's own rendering of the first page of `bridged` at 72 dpi on
/// white, as RGB samples, with its default settings but `hinting` and
/// `glyph_caching`.
fn hayro_rgb(bridged: &Bridged, hinting: bool, glyph_caching: bool) -> Vec<u8> {
    let pages = bridged.pdf().pages();
    hayro::render(
        pages.first().unwrap(),
        &RenderCache::new(),
        &InterpreterSettings::default(),
        &RenderSettings {
            hinting,
            glyph_caching,
            ..RenderSettings::default()
        },
        &PixmapSettings {
            x_scale: 1.0,
            y_scale: 1.0,
            bg_color: AlphaColor::WHITE,
        },
    )
    .data()
    .iter()
    .flat_map(|pixel| [pixel.r, pixel.g, pixel.b])
    .collect()
}

#[test]
fn a_render_fills_glyphs_without_the_glyph_cache() {
    let bridged = f1040();
    let options = Options::new((1.0, 0.0, 0.0, 1.0), false);
    let ours = raster(&bridged, &Fallback::default(), 0, &options).unwrap();
    assert!(ours.data == hayro_rgb(&bridged, false, false));
    assert!(ours.data != hayro_rgb(&bridged, false, true));
}

#[test]
fn the_glyph_cache_renders_as_hayro_caches_glyphs() {
    let none = Fallback::default();
    let form = f1040();
    let cached = raster(&form, &none, 0, &cached_options(false)).unwrap();
    assert!(cached.data == hayro_rgb(&form, false, true));
    assert!(cached.data != hayro_rgb(&form, false, false));
    // Hinted as well, text renders as hayro's default settings render it.
    let text = pdf20_simple();
    let both = raster(&text, &none, 0, &cached_options(true)).unwrap();
    assert!(both.data == hayro_rgb(&text, true, true));
    assert!(both.data != hayro_rgb(&text, true, false));
}

#[test]
fn hinting_renders_as_hayro_hints_without_its_glyph_cache() {
    let bridged = pdf20_simple();
    let theirs = hayro_rgb(&bridged, true, false);
    let none = Fallback::default();
    let hinted = raster(&bridged, &none, 0, &hinted_options(true)).unwrap();
    assert!(hinted.data == theirs);
    let unhinted = raster(&bridged, &none, 0, &hinted_options(false)).unwrap();
    assert!(unhinted.data != theirs);
}

#[test]
fn a_render_cache_keeps_the_glyphs_of_each_setting_apart() {
    let bridged = pdf20_simple();
    let other = pdf20_simple();
    let none = Fallback::default();
    let settings = [
        hinted_options(false),
        hinted_options(true),
        cached_options(false),
        cached_options(true),
    ];
    let fresh: Vec<Raster> = settings
        .iter()
        .map(|options| {
            // Rendering another bridge first makes this one start a new cache.
            raster(&other, &none, 0, options).unwrap();
            raster(&bridged, &none, 0, options).unwrap()
        })
        .collect();
    // The four settings draw four different rasters.
    for (at, one) in fresh.iter().enumerate() {
        assert!(fresh[at + 1..].iter().all(|later| later != one), "{at}");
    }
    // Each setting after each other, through the cache that they share.
    for (first, first_fresh) in settings.iter().zip(&fresh) {
        for (then, then_fresh) in settings.iter().zip(&fresh) {
            assert!(raster(&bridged, &none, 0, first).unwrap() == *first_fresh);
            assert!(
                raster(&bridged, &none, 0, then).unwrap() == *then_fresh,
                "{first:?} then {then:?}"
            );
        }
    }
}

#[test]
fn a_batch_renders_each_entry_as_raster_does_in_order() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/assets/real_world/pdfium-links-highlights-annots.pdf");
    let bridged = Arc::new(Bridged::of_bytes(fs::read(path).unwrap()));
    let options = Options::new((0.5, 0.0, 0.0, 0.5), true);
    let count = bridged.pdf().pages().len();
    let indexes: Vec<usize> = (0..count).rev().chain(0..count).collect();
    let none = Fallback::default();
    let alone: Vec<Raster> = indexes
        .iter()
        .map(|&at| raster(&bridged, &none, at, &options).unwrap())
        .collect();

    for workers in [1, 3, MAX_WORKERS] {
        let batched = batch(&bridged, &none, &indexes, &options, workers, |raster| {
            Ok::<_, RasterError>(raster)
        })
        .unwrap();
        assert!(batched == alone, "workers={workers}");
    }
}

#[test]
fn a_batch_through_the_glyph_cache_renders_each_entry_as_raster_does() {
    let bridged = f1040();
    let options = Options {
        glyph_cache: true,
        ..Options::new((0.5, 0.0, 0.0, 0.5), false)
    };
    let indexes = [1, 0, 0, 1];
    let none = Fallback::default();
    let alone: Vec<Raster> = indexes
        .iter()
        .map(|&at| raster(&bridged, &none, at, &options).unwrap())
        .collect();

    for workers in [1, 2] {
        let batched = batch(&bridged, &none, &indexes, &options, workers, |raster| {
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
    let none = Fallback::default();

    let result = batch(&bridged, &none, &[0, count, 0], &options, 2, |raster| {
        rendered.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        Ok::<_, RasterError>(raster)
    });

    assert_eq!(result.unwrap_err(), RasterError::NoPage(count));
    assert_eq!(rendered.into_inner(), 0);
    let failed = batch(&bridged, &none, &[0, 0, 0, 0], &options, 2, |_| {
        Err::<Raster, _>(RasterError::Clip("stop".to_owned()))
    });
    assert_eq!(failed.unwrap_err(), RasterError::Clip("stop".to_owned()));
}

/// Samples taken through vello_cpu's RGB narrowing equal `to_layout`'s,
/// for an opaque pixmap, which takes that path, and a translucent one,
/// which falls back, in every layout.
#[test]
fn take_samples_matches_to_layout() {
    use hayro::vello_cpu::peniko::ImageAlphaType;
    use hayro::vello_cpu::{PixelMetadata, Pixmap};

    let pixmap = |opacity: u8| {
        let data = (0..17 * 3)
            .flat_map(|at: u32| [(at * 5) as u8, (at * 11) as u8, (at * 23) as u8, opacity])
            .collect();
        let metadata = PixelMetadata {
            may_have_transparency: true,
            alpha_type: ImageAlphaType::Alpha,
        };
        Pixmap::from_parts(data, 17, 3, metadata)
    };
    for opacity in [255, 128] {
        for (gray, alpha) in [(false, false), (true, false), (false, true), (true, true)] {
            let mut expected = pixmap(opacity).take_rgba8(ImageAlphaType::Alpha);
            to_layout(&mut expected, gray, alpha);
            assert_eq!(
                take_samples(pixmap(opacity), gray, alpha),
                expected,
                "opacity {opacity}, gray {gray}, alpha {alpha}"
            );
        }
    }
}

/// One page that shows 日本語 at 40 points in a CID font named `name` of
/// the Adobe-Japan1 collection, which the document does not embed.
fn nonembedded_japanese(name: &str) -> Vec<u8> {
    let stream = "BT /F1 40 Tf 20 30 Td <65E5672C8A9E> Tj ET";
    let objects = [
        "<< /Type /Catalog /Pages 2 0 R >>".to_owned(),
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>".to_owned(),
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] \
         /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
            .to_owned(),
        format!(
            "<< /Length {} >>\nstream\n{stream}\nendstream",
            stream.len()
        ),
        format!(
            "<< /Type /Font /Subtype /Type0 /BaseFont /{name} /Encoding /UniJIS-UCS2-H \
             /DescendantFonts [6 0 R] >>"
        ),
        format!(
            "<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{name} /CIDSystemInfo \
             << /Registry (Adobe) /Ordering (Japan1) /Supplement 2 >> /FontDescriptor 7 0 R \
             /DW 1000 >>"
        ),
        format!(
            "<< /Type /FontDescriptor /FontName /{name} /Flags 4 /FontBBox [0 -141 1000 859] \
             /ItalicAngle 0 /Ascent 859 /Descent -141 /CapHeight 709 /StemV 80 >>"
        ),
    ];
    let mut out = b"%PDF-1.7\n".to_vec();
    let mut offsets = Vec::new();
    for (at, body) in objects.iter().enumerate() {
        offsets.push(out.len());
        out.extend_from_slice(format!("{} 0 obj\n{body}\nendobj\n", at + 1).as_bytes());
    }
    let xref = out.len();
    out.extend_from_slice(
        format!("xref\n0 {}\n0000000000 65535 f \n", objects.len() + 1).as_bytes(),
    );
    for offset in offsets {
        out.extend_from_slice(format!("{offset:010} 00000 n \n").as_bytes());
    }
    out.extend_from_slice(
        format!(
            "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n",
            objects.len() + 1
        )
        .as_bytes(),
    );
    out
}

/// The pixels of an RGB raster with a channel darker than mid-gray.
fn ink(raster: &Raster) -> usize {
    raster
        .data
        .chunks(3)
        .filter(|pixel| pixel.iter().any(|&channel| channel < 128))
        .count()
}

/// The subset of Noto Sans JP in the engine's test data, which draws 日本語.
fn japanese_face() -> Source {
    let data = include_bytes!("../fonts/testdata/NotoSansJP-subset.otf");
    Source::Face(Arc::new(crate::fonts::embed::Face::read(data, 0).unwrap()))
}

#[test]
fn fallback_faces_draw_the_cjk_fonts_that_a_document_does_not_embed() {
    let bridged = Arc::new(Bridged::of_bytes(nonembedded_japanese("MS-Gothic")));
    let options = Options::new((1.0, 0.0, 0.0, 1.0), false);
    let none = Fallback::default();
    let mut japanese = Fallback::default();
    japanese.set(Language::Japanese, false, Some(japanese_face()));
    let mut korean = Fallback::default();
    korean.set(Language::Korean, false, Some(japanese_face()));

    // hayro's own resolver answers with a Standard 14 font, which has no CJK glyphs.
    let blank = raster(&bridged, &none, 0, &options).unwrap();
    assert_eq!(ink(&blank), 0);
    let drawn = raster(&bridged, &japanese, 0, &options).unwrap();
    assert!(ink(&drawn) > 500, "{} pixels", ink(&drawn));
    // The thread's cache holds fonts resolved through the faces, so other
    // faces resolve the fonts again.
    assert!(raster(&bridged, &none, 0, &options).unwrap() == blank);
    assert!(raster(&bridged, &korean, 0, &options).unwrap() == blank);
    assert!(raster(&bridged, &japanese.clone(), 0, &options).unwrap() == drawn);

    let batched = batch(
        &bridged,
        &japanese,
        &[0, 0],
        &options,
        2,
        Ok::<_, RasterError>,
    )
    .unwrap();
    assert!(batched == [drawn.clone(), drawn]);
    let empty_outlines = |fallback| {
        svg(&bridged, fallback, 0, true)
            .unwrap()
            .matches(" d=\"\"")
            .count()
    };
    assert_eq!(empty_outlines(&none), 3);
    assert_eq!(empty_outlines(&japanese), 0);
}
