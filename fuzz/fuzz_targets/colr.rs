//! COLR glyphs: any bytes that load as a face with a COLR table paint its
//! glyphs into one transaction under the web profile's budget, whole or not
//! at all, as the check said. The glyphs come from a text, from the input's
//! last bytes, and from the first glyphs of the face, since a face that this
//! builds rarely maps a character. The Type 3 fonts then read back the codes
//! that they gave.

#![no_main]

use std::sync::Arc;

use libfuzzer_sys::fuzz_target;
use pyropdf_engine::content::shading::Shadings;
use pyropdf_engine::fonts::bitmap::Bitmaps;
use pyropdf_engine::fonts::colour::{Painted, refused_canvas_glyph};
use pyropdf_engine::fonts::colr::{Colr, ColrError, Verdict};
use pyropdf_engine::fonts::embed::{Face, GlyphId, Measure};
use pyropdf_engine::fonts::shape::ShapingFace;
use pyropdf_engine::fonts::type3::{Fonts, Glyph};
use pyropdf_store::budget::{Budget, DocumentLimits, LimitCode};
use pyropdf_store::save::FileBase;
use pyropdf_store::value::{Bytes, Name};
use pyropdf_store::version::Document;

const TEXT: &str = "AB CD 😀😁 日本語";
/// How many of the face's first glyphs are painted besides.
const FIRST: u32 = 32;
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
    let limits = DocumentLimits::web();
    let budget = Budget::new(&limits);
    let font = face.font();
    let Some(colr) = Colr::read(&font) else {
        return;
    };
    let mut ids: Vec<u32> = data
        .rchunks_exact(2)
        .take(16)
        .map(|pair| u32::from(u16::from_be_bytes([pair[0], pair[1]])))
        .chain(0..FIRST)
        .collect();
    if let Ok(bitmaps) = Bitmaps::read(&font)
        && let Ok(shaping) = ShapingFace::new(face.program.clone(), 0)
        && let Ok(glyphs) = shaping.shape(TEXT, &budget)
    {
        let painted = Painted {
            colr: Some(&colr),
            bitmaps: &bitmaps,
        };
        if let Ok(Some(refused)) = refused_canvas_glyph(&face, TEXT, &glyphs, painted, &budget) {
            assert!(TEXT.contains(refused.character), "a refusal names a character of the text");
            assert_ne!(refused.verdict, Some(Verdict::Paints));
        }
        ids.extend(glyphs.iter().map(|glyph| glyph.glyph));
    }
    ids.sort_unstable();
    ids.dedup();

    let Ok(base) = FileBase::open(Bytes::new(PDF.to_vec()), &budget) else {
        return;
    };
    let document = Document::new(Arc::new(base), None);
    let mut transaction = document.begin();
    let mut shadings = Shadings::new();
    let measure = Measure::new(&face);
    let mut new = Vec::new();
    for &id in &ids {
        let glyph = GlyphId::new(id);
        let Ok(verdict) = colr.check(glyph, &budget) else {
            return;
        };
        let name = format!("g{id}c");
        let width = measure.advance(glyph);
        let painted = colr.paint(
            &mut transaction,
            &mut shadings,
            glyph,
            Name::from(name.as_str()),
            width,
            &budget,
        );
        match (verdict, painted) {
            (Some(Verdict::Paints), Ok(painted)) => {
                assert!(painted.procedure.starts_with(format!("{width} 0 d0").as_bytes()));
                assert!(painted.resources.len() <= 1);
                if let Some(ink) = painted.ink {
                    assert!(ink.iter().all(|value| value.is_finite()));
                    assert!(ink[0] <= ink[2] && ink[1] <= ink[3]);
                }
                new.push(painted);
            }
            // The budget refuses what passes the check, and a coordinate out
            // of PDF's range; nothing else refuses a glyph that it passes.
            (Some(Verdict::Paints), Err(ColrError::Store(_) | ColrError::Refused(_))) => return,
            (_, Ok(_)) => panic!("glyph {id} painted, which the check refused"),
            (_, Err(_)) => {}
        }
        // The budget bounds what a glyph writes.
        assert!(
            limits
                .max_memory_size
                .is_none_or(|most| budget.used(LimitCode::MemorySize) <= most)
        );
    }
    if new.is_empty() {
        return;
    }
    let version = transaction.version();
    let Ok(mut fonts) = Fonts::read(version, &[], face.units_per_em, &budget) else {
        return;
    };
    let names: Vec<Name> = new.iter().map(|glyph: &Glyph| glyph.name.clone()).collect();
    if fonts.add(&mut transaction, new).is_err() || fonts.write(&mut transaction).is_err() {
        return;
    }
    let numbers: Vec<u32> = fonts.numbers().collect();
    let version = transaction.version();
    let Ok(read) = Fonts::read(version, &numbers, face.units_per_em, &budget) else {
        return;
    };
    for name in &names {
        assert_eq!(read.find(name.as_bytes()), fonts.find(name.as_bytes()));
    }
});
