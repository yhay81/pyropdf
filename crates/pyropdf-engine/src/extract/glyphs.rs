//! The glyphs that a document's pages and annotation appearances draw with
//! each font, as hayro resolves them for rendering: the glyph use that
//! subsetting an embedded font program keeps (ISO 32000-2 9.6.4).

use hayro::hayro_interpret::font::Glyph;
use hayro::hayro_interpret::hayro_syntax::object::{Dict, ObjectIdentifier};
use hayro::hayro_interpret::{CacheKey, DrawMode, InterpreterCache, InterpreterSettings};
use hayro::kurbo::{Affine, BezPath, Rect};
use pyropdf_store::budget::Budget;
use pyropdf_store::budget::BudgetVec;
use pyropdf_store::budget::LimitCode;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::value::Value;
use pyropdf_store::version::Version;

use super::Error;
use super::model::{self, ContentSink, GlyphStyle, ItemKinds};
use crate::bridge::Bridged;

/// The sink that gathers the glyph IDs of every outline glyph by font.
struct Uses<'a> {
    /// Font cache key and glyph ID, sorted after collecting the pages.
    glyphs: BudgetVec<'a, (u128, u32)>,
    refusal: Option<pyropdf_store::LimitExceeded>,
}

impl ContentSink for Uses<'_> {
    fn glyph_run(&mut self, _: &GlyphStyle) {}
    fn glyph(&mut self, glyph: &Glyph<'_>, _: Affine, _: Option<Rect>) {
        if let Glyph::Outline(outline) = glyph
            && let Err(error) = self
                .glyphs
                .try_push((outline.font_cache_key(), outline.glyph_id().to_u32()))
        {
            self.refusal = Some(error);
        }
    }
    fn stroke_completes_fill(&mut self) {}
    fn path(&mut self, _: &BezPath, _: Affine, _: &DrawMode) {}
    fn stopped(&self) -> bool {
        self.refusal.is_some()
    }
}

/// In-place heapsort with cancellation/deadline polling during the sort.
/// Its work is bounded by O(n log n), with no input-derived allocation.
fn sort_budgeted<T: Ord>(items: &mut [T], budget: &Budget) -> Result<(), Error> {
    fn sift<T: Ord>(
        items: &mut [T],
        mut root: usize,
        end: usize,
        budget: &Budget,
    ) -> Result<(), Error> {
        loop {
            budget.poll()?;
            let Some(mut child) = root.checked_mul(2).and_then(|value| value.checked_add(1)) else {
                break;
            };
            if child >= end {
                break;
            }
            if child + 1 < end && items[child] < items[child + 1] {
                child += 1;
            }
            if items[root] >= items[child] {
                break;
            }
            items.swap(root, child);
            root = child;
        }
        Ok(())
    }
    for root in (0..items.len() / 2).rev() {
        sift(items, root, items.len(), budget)?;
    }
    for end in (1..items.len()).rev() {
        budget.poll()?;
        items.swap(0, end);
        sift(items, 0, end, budget)?;
    }
    Ok(())
}

/// Returns, for each font dictionary `fonts` names by object number and
/// generation, the glyph IDs that the pages, with their annotation
/// appearances, draw with it: in the same order, empty for a font that no
/// page draws or that the document lacks. Interpretation runs under
/// `budget`, as text extraction does.
pub fn used_glyphs(
    bridged: &Bridged,
    version: &Version,
    pages: &PageIndex,
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
        glyphs: BudgetVec::counting(budget, LimitCode::TextGlyphCount),
        refusal: None,
    };
    let mut protected = BudgetVec::new(budget);
    let mut targets = BudgetVec::new(budget);
    for (index, &(number, generation)) in fonts.iter().enumerate() {
        budget.charge(LimitCode::ContentElementCount, 1)?;
        protected.try_push(false)?;
        targets.try_push((number, generation, index))?;
    }
    sort_budgeted(&mut targets, budget)?;
    for (index, page) in pdf.pages().iter().enumerate() {
        // A missing/unreadable font or XObject dictionary in the inherited
        // page resources hides the fonts it could name. Keep all programs in
        // that case; empty valid dictionaries are also conservatively kept.
        let unknown = match pages.get(index).and_then(|page| page.resources()) {
            Some(Value::Dict(dict)) => {
                (dict.get("Font").is_some() && page.resources().fonts.is_empty())
                    || (dict.get("XObject").is_some() && page.resources().x_objects.is_empty())
            }
            None => false,
            Some(_) => true,
        };
        super::font_guard::protect(page, version, &targets, &mut protected, unknown, budget)?;
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
        if let Some(refusal) = sink.refusal {
            return Err(Error::Limit(refusal));
        }
        uses = sink;
    }
    sort_budgeted(&mut uses.glyphs, budget)?;
    let xref = pdf.xref();
    let mut results = BudgetVec::new(budget);
    for (index, &(number, generation)) in fonts.iter().enumerate() {
        let mut ids = BudgetVec::new(budget);
        if protected[index] {
            ids.try_push(super::font_guard::KEEP_WHOLE)?;
        } else {
            let dict = i32::try_from(number).ok().and_then(|number| {
                xref.get::<Dict<'_>>(ObjectIdentifier::new(number, i32::from(generation)))
            });
            if let Some(dict) = dict {
                let key = dict.cache_key();
                let first = uses.glyphs.partition_point(|&(font, _)| font < key);
                for &(font, glyph) in &uses.glyphs[first..] {
                    if font != key {
                        break;
                    }
                    budget.charge(LimitCode::ContentElementCount, 1)?;
                    if ids.last() != Some(&glyph) {
                        ids.try_push(glyph)?;
                    }
                }
            } else {
                // A missing font dictionary must not be mistaken for a font
                // that draws no glyphs: subsetting it could discard glyphs.
                ids.try_push(super::font_guard::KEEP_WHOLE)?;
            }
        }
        results.try_push(ids.into_vec())?;
    }
    Ok(results.into_vec())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn glyph_order_is_sorted_with_duplicates_and_cancelled_sort_refuses() {
        let budget = Budget::default();
        let mut items = [(5, 2), (1, 4), (1, 2), (1, 4), (0, 0)];
        sort_budgeted(&mut items, &budget).unwrap();
        assert_eq!(items, [(0, 0), (1, 2), (1, 4), (1, 4), (5, 2)]);
        let token = pyropdf_store::budget::CancellationToken::new();
        token.cancel();
        let cancelled = Budget::default().with_cancellation(token);
        assert!(matches!(
            sort_budgeted(&mut items, &cancelled),
            Err(Error::Limit(_))
        ));
    }
}
