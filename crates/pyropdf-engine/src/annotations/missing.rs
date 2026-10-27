//! The appearances that a render draws for annotations that a file stores
//! without one (yhay81/pyropdf#169).
//!
//! hayro draws `/AP /N` only, so the bridge hands it, beside the version's
//! objects, each such annotation's dictionary with `/AP /N` and `/Rect` set
//! as the settler would set them, and the form that `/AP /N` names. The
//! version never holds them: rendering leaves the document as it was.
//!
//! A document cannot amplify small dictionaries into unbounded drawing:
//! more than [`MAX_SYNTHESIZED_APPEARANCES`] appearances, more than
//! [`MAX_SYNTHESIZED_POINTS`] path points in all, or one annotation whose
//! geometry holds more than the settler draws, leave every annotation
//! undrawn rather than some of them. Ported from pylopdf's
//! `missing_appearance_plans` in `rust/src/document/annotations.rs` at
//! ebb01d3c4c742046e8ce7fd72957cd15a2933321.

use std::collections::HashSet;

use pyropdf_store::Result;
use pyropdf_store::budget::{Budget, LimitCode};
use pyropdf_store::pages::PageIndex;
use pyropdf_store::value::{Dict, Name, Reference, Stream, Value};
use pyropdf_store::version::Version;

use super::{
    MAX_ANNOTATION_POINTS, MAX_QUADS, Quad, Reader, appearance, draws, has_points, is_text_markup,
    numbers_array, zigzag_points,
};

/// The most annotations whose appearances one version's render draws.
pub const MAX_SYNTHESIZED_APPEARANCES: usize = 4_096;

/// The most path points that the appearances of one version's render draw
/// together.
pub const MAX_SYNTHESIZED_POINTS: usize = 262_144;

/// Returns the objects that draw every annotation of `pages` that the
/// settler draws and that has no usable `/AP /N`: each annotation's
/// dictionary with `/AP /N` and `/Rect` set, under its own reference, and
/// its form, under the numbers from the version's next one on, in that
/// order. An annotation that a page holds directly, not by reference, is
/// not drawn, as hayro reads it from the page itself.
///
/// An annotation whose objects the store cannot read is not drawn; a
/// limit of the budget refuses.
pub fn synthesized(
    version: &Version,
    pages: &PageIndex,
    budget: &Budget,
) -> Result<Vec<(Reference, Value)>> {
    let reader = Reader { version, budget };
    let mut objects = Vec::new();
    let mut drawn = HashSet::new();
    let mut points = 0usize;
    let mut next = version.next_number();
    for page in pages.pages() {
        let Some(annots) = readable(reader.entry(page.dict(), "Annots"))?.flatten() else {
            continue;
        };
        let Value::Array(items) = &*annots else {
            continue;
        };
        for item in items.iter() {
            budget.poll()?;
            let &Value::Reference(reference) = item else {
                continue;
            };
            if drawn.contains(&reference.number()) {
                continue;
            }
            let Some(plan) = readable(plan(&reader, reference))?.flatten() else {
                continue;
            };
            let Plan::Draw(dict, form, count) = plan else {
                return Ok(Vec::new());
            };
            points = points.saturating_add(count);
            if drawn.len() >= MAX_SYNTHESIZED_APPEARANCES || points > MAX_SYNTHESIZED_POINTS {
                return Ok(Vec::new());
            }
            let Ok(number) = u32::try_from(next) else {
                return Ok(Vec::new());
            };
            next += 1;
            budget.charge(LimitCode::MemorySize, form.data().len() as u64)?;
            let mut dict = dict;
            let mut normal = Dict::new();
            normal.insert(Name::from("N"), Value::Reference(Reference::new(number, 0)));
            dict.insert(Name::from("AP"), Value::Dict(normal));
            drawn.insert(reference.number());
            objects.push((reference, Value::Dict(dict)));
            objects.push((Reference::new(number, 0), Value::from(form)));
        }
    }
    objects.sort_by_key(|(reference, _)| reference.number());
    Ok(objects)
}

/// What a render does with one annotation.
enum Plan {
    /// Draws it: its dictionary with the fitted `/Rect`, its form, and the
    /// path points that the form draws.
    Draw(Dict, Stream, usize),
    /// Draws no annotation, since this one holds more than the settler
    /// draws.
    Refuse,
}

/// Returns the plan of the annotation at `reference`, or `None` for one
/// that has an appearance, that the settler does not draw, or whose
/// drawing is empty.
fn plan(reader: &Reader<'_>, reference: Reference) -> Result<Option<Plan>> {
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
    if exceeds_points(reader, dict, &subtype)? {
        return Ok(Some(Plan::Refuse));
    }
    let Some(drawn) = appearance(reader, dict)? else {
        return Ok(None);
    };
    // An appearance that draws nothing needs no form.
    if drawn.points == 0 {
        return Ok(None);
    }
    let mut dict = dict.clone();
    dict.insert(Name::from("Rect"), numbers_array(&drawn.rect));
    Ok(Some(Plan::Draw(dict, drawn.form(), drawn.points)))
}

/// Returns `Ok(None)` for a store error other than a limit, which leaves
/// one annotation undrawn, and refuses with a limit.
fn readable<T>(result: Result<T>) -> Result<Option<T>> {
    match result {
        Ok(value) => Ok(Some(value)),
        Err(error) if error.limit().is_some() => Err(error),
        Err(_) => Ok(None),
    }
}

/// Returns whether `/AP /N` of the annotation `dict` is a stream or a
/// dictionary of them, which hayro draws or chooses from.
fn has_normal_appearance(reader: &Reader<'_>, dict: &Dict) -> Result<bool> {
    let Some(appearances) = reader.entry(dict, "AP")? else {
        return Ok(false);
    };
    let Value::Dict(appearances) = &*appearances else {
        return Ok(false);
    };
    Ok(reader
        .entry(appearances, "N")?
        .is_some_and(|normal| matches!(*normal, Value::Stream(_) | Value::Dict(_))))
}

/// Returns whether the geometry of the annotation `dict` holds more than
/// the settler draws: more than [`MAX_QUADS`] quadrilaterals, a squiggly
/// line of more than [`MAX_ANNOTATION_POINTS`] zigzag points, or more than
/// as many points of a line, polygon, polyline, or ink.
fn exceeds_points(reader: &Reader<'_>, dict: &Dict, subtype: &[u8]) -> Result<bool> {
    if has_points(subtype) {
        return Ok(reader.point_count(dict, subtype)? > MAX_ANNOTATION_POINTS);
    }
    if !is_text_markup(subtype) {
        return Ok(false);
    }
    let Some(array) = reader.entry(dict, "QuadPoints")? else {
        return Ok(false);
    };
    let Value::Array(items) = &*array else {
        return Ok(false);
    };
    if items.len() / 8 > MAX_QUADS {
        return Ok(true);
    }
    if subtype != b"Squiggly" {
        return Ok(false);
    }
    let Some(values) = reader.numbers_of(&array, 8 * MAX_QUADS)? else {
        return Ok(false);
    };
    let quads: Vec<Quad> = values
        .as_chunks::<8>()
        .0
        .iter()
        .map(|q| [(q[0], q[1]), (q[2], q[3]), (q[4], q[5]), (q[6], q[7])])
        .collect();
    Ok(zigzag_points(&quads) > MAX_ANNOTATION_POINTS)
}
