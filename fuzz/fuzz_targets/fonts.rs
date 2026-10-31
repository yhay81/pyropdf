//! Embedded faces: any bytes that load as a face shape a text, measure its
//! glyphs, and compact to them; any bytes compact or refuse. A face that
//! does not load is a typed error, never a panic.

#![no_main]

use libfuzzer_sys::fuzz_target;
use pyropdf_engine::fonts::compact::compact;
use pyropdf_engine::fonts::embed::{Face, GlyphId, Measure};
use pyropdf_engine::fonts::shape::ShapingFace;
use pyropdf_store::budget::{Budget, DocumentLimits};
use skrifa::FontRef;
use skrifa::raw::TableProvider;

const TEXT: &str = "AVA office 日本語 שָׁלוֹם مرحبا 123";

/// Checks a compacted program: `.notdef` first, the glyphs in order, and a
/// TrueType result that reads with as many glyphs.
fn check(program: &[u8], keep: &[u32]) {
    let Some(compact) = compact(program, keep, "Fuzz") else {
        return;
    };
    assert_eq!(compact.glyphs.first(), Some(&0));
    assert!(compact.glyphs.windows(2).all(|pair| pair[0] < pair[1]));
    if !compact.cff {
        let font = FontRef::new(&compact.data).expect("a compacted program reads");
        let glyphs = font.maxp().expect("it has maxp").num_glyphs();
        assert_eq!(usize::from(glyphs), compact.glyphs.len());
    }
}

fuzz_target!(
    init: {
        let _ = std::panic::take_hook();
    },
    |data: &[u8]| {
    let keep: Vec<u32> = data.iter().rev().take(16).map(|&byte| u32::from(byte)).collect();
    check(data, &keep);
    let Ok(face) = Face::read(data, 0) else {
        return;
    };
    let budget = Budget::new(&DocumentLimits::web());
    let Ok(shaping) = ShapingFace::new(&face.program, 0) else {
        return;
    };
    let Ok(glyphs) = shaping.shape(TEXT, &budget) else {
        return;
    };
    let measure = Measure::new(&face);
    let mut keep = Vec::new();
    for glyph in glyphs.into_vec() {
        let _ = measure.advance(GlyphId::new(glyph.glyph));
        let _ = measure.ink(GlyphId::new(glyph.glyph));
        keep.push(glyph.glyph);
    }
    check(&face.program, &keep);
});
