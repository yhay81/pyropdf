//! Colour bitmaps: any bytes that load as a face draw their bitmaps into
//! Type 3 fonts of one transaction, which read back the codes that they gave.
//! The glyphs come from a text, and from the input's last bytes, since a face
//! that this builds rarely maps a character. Decoding a PNG is `Canvas.image`'s
//! path, so what this checks of one is where it lies and what names it.

#![no_main]

use std::sync::Arc;

use libfuzzer_sys::fuzz_target;
use pyropdf_engine::fonts::bitmap::{self, Bitmaps, DRAWING_PPI};
use pyropdf_engine::fonts::colour::refused_canvas_glyph;
use pyropdf_engine::fonts::embed::{Face, GlyphId, Measure};
use pyropdf_engine::fonts::shape::ShapingFace;
use pyropdf_engine::fonts::type3::{Fonts, GLYPHS_PER_FONT, Glyph};
use pyropdf_store::budget::{Budget, DocumentLimits};
use pyropdf_store::save::FileBase;
use pyropdf_store::value::{Bytes, Name, Reference, Value};
use pyropdf_store::version::Document;

const TEXT: &str = "AB CD 😀😁 日本語";
const SIZES: [f64; 3] = [1.0, 12.0, 96.0];
const PDF: &[u8] = b"%PDF-1.7\n1 0 obj\n<</Type/Catalog/Pages 2 0 R>>\nendobj\n\
2 0 obj\n<</Type/Pages/Count 0/Kids[]>>\nendobj\n\
trailer\n<</Root 1 0 R/Size 3>>\n%%EOF\n";

fuzz_target!(
    init: {
        let _ = std::panic::take_hook();
    },
    |data: &[u8]| {
    let Ok(face) = Face::read(data, 0) else {
        return;
    };
    let budget = Budget::new(&DocumentLimits::web());
    let font = face.font();
    let Ok(bitmaps) = Bitmaps::read(&font) else {
        return;
    };
    let mut ids: Vec<u32> = data
        .rchunks_exact(2)
        .take(64)
        .map(|pair| u32::from(u16::from_be_bytes([pair[0], pair[1]])))
        .collect();
    if let Ok(shaping) = ShapingFace::new(face.program.clone(), 0)
        && let Ok(glyphs) = shaping.shape(TEXT, &budget)
    {
        if let Ok(Some(character)) = refused_canvas_glyph(&face, TEXT, &glyphs, &bitmaps, &budget) {
            assert!(TEXT.contains(character), "a refusal names a character of the text");
        }
        ids.extend(glyphs.iter().map(|glyph| glyph.glyph));
    }
    ids.sort_unstable();
    ids.dedup();

    let measure = Measure::new(&face);
    let mut new = Vec::new();
    for size in SIZES {
        for &id in &ids {
            let glyph = GlyphId::new(id);
            let width = measure.advance(glyph);
            let Some(found) = bitmaps.best(glyph, size * DRAWING_PPI / 72.0) else {
                continue;
            };
            assert!(bitmaps.has(glyph), "a bitmap that a drawing finds is held");
            assert!(found.rect.iter().all(|value| value.is_finite()));
            assert!(found.ppem > 0);
            assert!(found.png.starts_with(b"\x89PNG"));
            // The same size finds the same bitmap, which is what names it.
            let again = bitmaps.best(glyph, f64::from(found.ppem));
            assert_eq!(again.map(|bitmap| bitmap.ppem), Some(found.ppem));
            let name = format!("g{id}s{}", found.ppem);
            if new.iter().any(|known: &Glyph| known.name.as_bytes() == name.as_bytes()) {
                continue;
            }
            new.push(Glyph {
                name: Name::from(name.as_str()),
                width,
                ink: Some(found.rect),
                procedure: bitmap::procedure(width, found.rect, &name),
                resources: vec![(
                    Name::from("XObject"),
                    Name::from(name.as_str()),
                    Value::Reference(Reference::new(1, 0)),
                )],
            });
        }
    }
    if new.is_empty() {
        return;
    }
    let Ok(base) = FileBase::open(Bytes::new(PDF.to_vec()), &budget) else {
        return;
    };
    let document = Document::new(Arc::new(base), None);
    let mut transaction = document.begin();
    let version = transaction.version();
    let Ok(mut fonts) = Fonts::read(version, &[], face.units_per_em, &budget) else {
        return;
    };
    let names: Vec<Name> = new.iter().map(|glyph| glyph.name.clone()).collect();
    if fonts.add(&mut transaction, new).is_err() || fonts.write(&mut transaction).is_err() {
        return;
    }
    let numbers: Vec<u32> = fonts.numbers().collect();
    assert!(numbers.len() * GLYPHS_PER_FONT >= names.len());
    let version = transaction.version();
    let Ok(read) = Fonts::read(version, &numbers, face.units_per_em, &budget) else {
        return;
    };
    assert_eq!(read.numbers().count(), numbers.len());
    for name in &names {
        assert_eq!(read.find(name.as_bytes()), fonts.find(name.as_bytes()));
    }
});
