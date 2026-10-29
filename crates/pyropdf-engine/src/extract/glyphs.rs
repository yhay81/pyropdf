//! The glyphs that a document's pages and annotation appearances draw with
//! each font, as hayro resolves them for rendering: the glyph use that
//! subsetting an embedded font program keeps (ISO 32000-2 9.6.4).

use std::collections::{BTreeSet, HashMap};

use hayro::hayro_interpret::font::Glyph;
use hayro::hayro_interpret::hayro_syntax::object::{Dict, ObjectIdentifier};
use hayro::hayro_interpret::{CacheKey, DrawMode, InterpreterCache, InterpreterSettings};
use hayro::kurbo::{Affine, BezPath, Rect};
use pyropdf_store::budget::Budget;

use super::Error;
use super::model::{self, ContentSink, GlyphStyle, ItemKinds};
use crate::bridge::Bridged;

/// The sink that gathers the glyph IDs of every outline glyph by font.
struct Uses {
    /// Glyph IDs by hayro's font cache key, which hashes the font dictionary.
    glyphs: HashMap<u128, BTreeSet<u32>>,
}

impl ContentSink for Uses {
    fn glyph_run(&mut self, _: &GlyphStyle) {}
    fn glyph(&mut self, glyph: &Glyph<'_>, _: Affine, _: Option<Rect>) {
        if let Glyph::Outline(outline) = glyph {
            // A font has at most 65,536 glyphs, so each set stays bounded.
            self.glyphs
                .entry(outline.font_cache_key())
                .or_default()
                .insert(outline.glyph_id().to_u32());
        }
    }
    fn stroke_completes_fill(&mut self) {}
    fn path(&mut self, _: &BezPath, _: Affine, _: &DrawMode) {}
    fn stopped(&self) -> bool {
        false
    }
}

/// Returns, for each font dictionary `fonts` names by object number and
/// generation, the glyph IDs that the pages, with their annotation
/// appearances, draw with it: in the same order, empty for a font that no
/// page draws or that the document lacks. Interpretation runs under
/// `budget`, as text extraction does.
pub fn used_glyphs(
    bridged: &Bridged,
    fonts: &[(u32, u16)],
    budget: &Budget,
) -> Result<Vec<Vec<u32>>, Error> {
    let pdf = bridged.pdf();
    let settings = InterpreterSettings {
        render_annotations: true,
        ..InterpreterSettings::default()
    };
    let cache = InterpreterCache::new();
    let mut uses = Uses {
        glyphs: HashMap::new(),
    };
    for page in pdf.pages().iter() {
        let (_, sink) = model::collect_page_content(
            pdf,
            page,
            settings.clone(),
            &cache,
            uses,
            ItemKinds::NONE,
            None,
            budget,
        )?;
        uses = sink;
    }
    let xref = pdf.xref();
    Ok(fonts
        .iter()
        .map(|&(number, generation)| {
            let id = ObjectIdentifier::new(
                i32::try_from(number).unwrap_or(i32::MAX),
                i32::from(generation),
            );
            xref.get::<Dict<'_>>(id)
                .and_then(|dict| uses.glyphs.get(&dict.cache_key()))
                .map(|glyphs| glyphs.iter().copied().collect())
                .unwrap_or_default()
        })
        .collect())
}
