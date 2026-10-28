//! Render-time appearances for annotations stored without a usable /AP /N.
//!
//! The bridge substitutes these objects only in hayro's view. The store's
//! version and history stay unchanged. Geometry, aggregate counts, output,
//! and allocations share the rendering operation's budget; a bound refuses
//! instead of returning an incomplete drawing.
//!
//! Ported from pylopdf's `missing_appearance_plans` at
//! ebb01d3c4c742046e8ce7fd72957cd15a2933321, without its silent guards.

use std::collections::HashSet;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::pages::PageIndex;
use pyropdf_store::value::{Dict, Name, Reference, Stream, Value};
use pyropdf_store::version::Version;
use pyropdf_store::{Error, LimitExceeded, Result};

use super::{CopyPart, Reader, appearance_for_render, charge_copy, draws, numbers_array};

/// The default maximum synthesized appearances of one render.
pub const MAX_SYNTHESIZED_APPEARANCES: usize = 4_096;
/// The default maximum generated path points of one render.
pub const MAX_SYNTHESIZED_POINTS: usize = 262_144;

/// Returns sorted virtual annotation dictionaries and forms. Annotations
/// stored directly in a page remain outside this object-number substitution.
/// Store errors propagate; missing or unsupported values need no appearance.
pub fn synthesized(
    version: &Version,
    pages: &PageIndex,
    budget: &Budget,
) -> Result<Vec<(Reference, Value)>> {
    let reader = Reader { version, budget };
    let mut objects = BudgetVec::new(budget);
    let mut visited = HashSet::new();
    let mut next = version.next_number();
    for page in pages.pages() {
        budget.poll()?;
        let Some(annots) = reader.entry(page.dict(), "Annots")? else {
            continue;
        };
        let Value::Array(items) = &*annots else {
            continue;
        };
        budget.check(LimitCode::AnnotationCount, items.len() as u64)?;
        for item in items.iter() {
            budget.poll()?;
            let &Value::Reference(reference) = item else {
                continue;
            };
            if visited.contains(&reference) {
                continue;
            }
            // Hash slots, load-factor slack, and temporary rehash storage.
            budget.charge(LimitCode::MemorySize, 64)?;
            visited
                .try_reserve(1)
                .map_err(|_| LimitExceeded::new(LimitCode::MemorySize))?;
            visited.insert(reference);
            let Some((mut dict, form)) = plan(&reader, reference)? else {
                continue;
            };
            // Leave an object number for the bridge's cross-reference stream.
            let number = u32::try_from(next)
                .ok()
                .filter(|number| *number < u32::MAX)
                .ok_or_else(|| {
                    Error::malformed("no object number remains for a synthesized appearance")
                })?;
            next += 1;
            let mut normal = Dict::new();
            normal.insert(Name::from("N"), Value::Reference(Reference::new(number, 0)));
            dict.insert(Name::from("AP"), Value::Dict(normal));
            objects.try_push((reference, Value::Dict(dict)))?;
            objects.try_push((Reference::new(number, 0), Value::from(form)))?;
        }
    }
    objects.sort_unstable_by_key(|(reference, _)| reference.number());
    budget.poll()?;
    Ok(objects.into_vec())
}

/// Returns an annotation copy and its form only when a drawing is needed.
fn plan(reader: &Reader<'_>, reference: Reference) -> Result<Option<(Dict, Stream)>> {
    let Some(object) = reader
        .version
        .get(reference.number(), reader.budget)?
        .filter(|object| object.generation() == reference.generation())
    else {
        return Ok(None);
    };
    let Value::Dict(dict) = &**object.value() else {
        return Ok(None);
    };
    let Some(subtype) = reader.name(dict, "Subtype")? else {
        return Ok(None);
    };
    if !draws(&subtype) || has_normal_appearance(reader, dict)? {
        return Ok(None);
    }
    let Some(drawn) = appearance_for_render(reader, dict)? else {
        return Ok(None);
    };
    if drawn.points == 0 {
        return Ok(None);
    }
    reader
        .budget
        .charge(LimitCode::AnnotationSynthesisCount, 1)?;
    charge_copy(CopyPart::Dict(dict), reader.budget)?;
    let mut dict = dict.clone();
    dict.insert(Name::from("Rect"), numbers_array(&drawn.rect));
    Ok(Some((dict, drawn.form())))
}

/// Mirrors hayro's state selection: /AS's stream, falling back to /Off.
/// Merely having a state dictionary does not make an appearance usable.
fn has_normal_appearance(reader: &Reader<'_>, dict: &Dict) -> Result<bool> {
    let Some(appearances) = reader.entry(dict, "AP")? else {
        return Ok(false);
    };
    let Value::Dict(appearances) = &*appearances else {
        return Ok(false);
    };
    let Some(normal) = reader.entry(appearances, "N")? else {
        return Ok(false);
    };
    let Value::Dict(states) = &*normal else {
        return Ok(matches!(*normal, Value::Stream(_)));
    };
    if let Some(state) = reader.name(dict, "AS")?
        && let Some(value) = states.get(&*state)
        && matches!(*reader.resolve(value)?, Value::Stream(_))
    {
        return Ok(true);
    }
    Ok(reader
        .entry(states, "Off")?
        .is_some_and(|value| matches!(*value, Value::Stream(_))))
}
