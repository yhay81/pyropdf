//! Embedded faces: any bytes that load as a face shape a text, look for the
//! colour glyphs that drawing refuses, measure its glyphs, and compact to
//! them, and so does the instance of a face with a weight axis, whose program
//! reads as a face with as many glyphs; any bytes compact or refuse. A face
//! that does not load is a typed error, never a panic: `Unsupported` only for
//! data that reads as OpenType, and `Font` with its reason otherwise.

#![no_main]

use libfuzzer_sys::fuzz_target;
use pyropdf_engine::fonts::colour::refused_colour_glyph;
use pyropdf_engine::fonts::compact::compact;
use pyropdf_engine::fonts::embed::{Face, FaceError, GlyphId, Measure};
use pyropdf_engine::fonts::shape::ShapingFace;
use pyropdf_store::budget::{Budget, DocumentLimits, LimitCode};
use skrifa::FontRef;
use skrifa::raw::{FileRef, TableProvider};

const TEXT: &str = "AVA office 日本語 שָׁלוֹם مرحبا 123";
/// The most bytes of an instance's program, far past what a small input needs
/// unless its composites amplify.
const MAX_INSTANCE: u64 = 1 << 20;

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
    let face = match Face::read(data, 0) {
        Ok(face) => face,
        Err(FaceError::Unsupported(_)) => {
            assert!(FileRef::new(data).is_ok(), "an unsupported face reads as OpenType");
            return;
        }
        Err(FaceError::Font(reason)) => {
            assert!(!reason.is_empty(), "a font error names its reason");
            return;
        }
    };
    use_face(&face);
    // The weight comes from the input, so that it falls on and off the axis.
    let weight = data.last().map_or(0, |&byte| u16::from(byte) * 4);
    let limited = Budget::new(&DocumentLimits::web())
        .with_limit(LimitCode::FontInputSize, Some(MAX_INSTANCE));
    if let Ok(instance) = face.at_weight(weight, &limited) {
        assert!(instance.program.len() as u64 <= MAX_INSTANCE);
        assert_eq!(instance.glyph_count, face.glyph_count);
        let again = Face::read(&instance.program, 0).expect("an instance reads as a face");
        assert_eq!(again.glyph_count, face.glyph_count);
        use_face(&instance);
    }
});

/// Shapes `TEXT` in `face`, and measures and compacts to what it drew.
fn use_face(face: &Face) {
    let budget = Budget::new(&DocumentLimits::web());
    let Ok(shaping) = ShapingFace::of(face) else {
        return;
    };
    let Ok(glyphs) = shaping.shape(TEXT, &budget) else {
        return;
    };
    if let Ok(Some(character)) = refused_colour_glyph(face, TEXT, &glyphs, &budget) {
        assert!(TEXT.contains(character), "a refusal names a character of the text");
    }
    let measure = Measure::new(face);
    let mut keep = Vec::new();
    for glyph in glyphs.into_vec() {
        let _ = measure.advance(GlyphId::new(glyph.glyph));
        let _ = measure.ink(GlyphId::new(glyph.glyph));
        keep.push(glyph.glyph);
    }
    check(&face.program, &keep);
}
