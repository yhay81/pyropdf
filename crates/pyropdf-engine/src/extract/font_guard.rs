//! Conservative protection for fonts reached by unreadable page content.
//!
//! hayro skips content streams and form XObjects it cannot decode. Before
//! subsetting fonts, check those streams with the same operation budget and
//! keep the fonts reachable from an affected page whole. Forms named in a
//! resource dictionary are checked even when no content invokes them: after
//! an unreadable stream there is no reliable way to know which forms it uses.

use hayro::hayro_syntax::object::{Array, Dict, Name, Object, ObjectIdentifier, Stream};
use hayro::hayro_syntax::page::{Page, Resources};
use pyropdf_store::budget::{Budget, BudgetVec, LimitCode, StreamKind};
use pyropdf_store::value::Value;
use pyropdf_store::version::Version;
use std::collections::hash_map::RandomState;
use std::hash::{BuildHasher, Hash, Hasher};

use super::Error;

/// Out of range for an OpenType glyph ID, which is at most 65535. The binding
/// returns this internal marker instead of glyph IDs when the font must stay
/// whole. A program shared by several fonts must stay whole if any is marked.
pub(super) const KEEP_WHOLE: u32 = u32::MAX;

/// An open-addressed set with fallible, budgeted allocation. Random hashing
/// prevents input-chosen object numbers from forcing quadratic probing.
struct Seen<'b> {
    slots: BudgetVec<'b, Option<ObjectIdentifier>>,
    count: usize,
    hash: RandomState,
}

impl<'b> Seen<'b> {
    fn new(budget: &'b Budget) -> Self {
        Self {
            slots: BudgetVec::new(budget),
            count: 0,
            hash: RandomState::new(),
        }
    }

    fn position(
        hash: &RandomState,
        slots: &[Option<ObjectIdentifier>],
        id: ObjectIdentifier,
        budget: &Budget,
    ) -> Result<usize, Error> {
        let mut state = hash.build_hasher();
        id.hash(&mut state);
        let mut pos = state.finish() as usize & (slots.len() - 1);
        while slots[pos].is_some_and(|other| other != id) {
            budget.charge(LimitCode::ContentElementCount, 1)?;
            pos = (pos + 1) & (slots.len() - 1);
        }
        Ok(pos)
    }

    fn insert(&mut self, id: ObjectIdentifier, budget: &'b Budget) -> Result<bool, Error> {
        if self.slots.is_empty() || self.count >= self.slots.len() / 2 {
            let capacity = self
                .slots
                .len()
                .max(8)
                .checked_mul(2)
                .ok_or_else(|| Error::Allocation("too many form resources".into()))?;
            let mut slots = BudgetVec::new(budget);
            for _ in 0..capacity {
                slots.try_push(None)?;
            }
            for existing in self.slots.iter().flatten().copied() {
                let pos = Self::position(&self.hash, &slots, existing, budget)?;
                slots[pos] = Some(existing);
            }
            self.slots = slots;
        }
        let pos = Self::position(&self.hash, &self.slots, id, budget)?;
        if self.slots[pos].is_some() {
            return Ok(false);
        }
        budget.charge(LimitCode::ContentElementCount, 1)?;
        self.slots[pos] = Some(id);
        self.count += 1;
        Ok(true)
    }
}

fn store_error(error: &pyropdf_store::Error) -> Option<Error> {
    error.limit().copied().map(Error::Limit)
}

/// A failed decode is different from a budget refusal: the former protects
/// fonts, while the latter aborts the operation without changing the document.
fn readable(stream: &Stream<'_>, version: &Version, budget: &Budget) -> Result<bool, Error> {
    let Some(id) = stream.dict().obj_id() else {
        return Ok(false);
    };
    let (Ok(number), Ok(generation)) = (u32::try_from(id.obj_number), u16::try_from(id.gen_number))
    else {
        return Ok(false);
    };
    let object = match version.get(number, budget) {
        Ok(Some(object)) if object.generation() == generation => object,
        Ok(_) => return Ok(false),
        Err(error) => return store_error(&error).map_or(Ok(false), Err),
    };
    let Value::Stream(stream) = object.value().as_ref() else {
        return Ok(false);
    };
    // `decoded_as` currently charges filtered streams after decoding (#48).
    // Its unfiltered fast path copies without a decode charge, so account for
    // that case here as well.
    if stream.dict().get("Filter").is_none() {
        budget
            .decode(StreamKind::PageContent)
            .charge(u64::try_from(stream.data().len()).unwrap_or(u64::MAX))?;
    }
    match stream.decoded_as(StreamKind::PageContent, budget) {
        Ok(_) => Ok(true),
        Err(error) => store_error(&error).map_or(Ok(false), Err),
    }
}

fn check_content(value: Object<'_>, version: &Version, budget: &Budget) -> Result<bool, Error> {
    match value {
        Object::Stream(stream) => readable(&stream, version, budget),
        Object::Array(array) => {
            let mut count = 0usize;
            let mut ok = true;
            for value in array.iter::<Object<'_>>() {
                budget.poll()?;
                count += 1;
                if let Object::Stream(stream) = value {
                    ok &= readable(&stream, version, budget)?;
                } else {
                    ok = false;
                }
            }
            // Unresolvable entries are skipped by hayro's resolved iterator.
            let mut raw = 0usize;
            for _ in array.raw_iter() {
                budget.poll()?;
                raw += 1;
            }
            Ok(ok && count == raw)
        }
        _ => Ok(false),
    }
}

/// Conservatively inspect annotation appearance streams. Appearance streams
/// without resources inherit the page's resources, which are already included.
fn appearances<'a, 'b>(
    page: &Page<'a>,
    version: &Version,
    budget: &'b Budget,
    resources: &mut BudgetVec<'b, Resources<'a>>,
) -> Result<(bool, bool), Error> {
    let Some(annots) = page.raw().get::<Array<'_>>("Annots") else {
        return Ok((true, page.raw().contains_key("Annots")));
    };
    let mut ok = true;
    let mut unknown = false;
    let mut count = 0usize;
    for annot in annots.iter::<Dict<'_>>() {
        budget.charge(LimitCode::ContentElementCount, 1)?;
        count += 1;
        let Some(ap) = annot.get::<Dict<'_>>("AP") else {
            if annot.contains_key("AP") {
                unknown = true;
            }
            continue;
        };
        let mut pending = BudgetVec::counting(budget, LimitCode::ObjectDepth);
        for key in ap.keys() {
            if let Some(value) = ap.get::<Object<'_>>(key.as_ref()) {
                pending.try_push(value)?;
            } else {
                unknown = true;
            }
        }
        while let Some(value) = pending.pop() {
            budget.poll()?;
            match value {
                Object::Stream(stream) => {
                    ok &= readable(&stream, version, budget)?;
                    if let Some(dict) = stream.dict().get::<Dict<'_>>("Resources") {
                        unknown |= missing_resources(&dict);
                        resources.try_push(Resources::new(dict))?;
                    } else if stream.dict().contains_key("Resources") {
                        unknown = true;
                    }
                }
                Object::Dict(states) => {
                    for key in states.keys() {
                        if let Some(value) = states.get::<Object<'_>>(key.as_ref()) {
                            // An appearance state dictionary names streams, not
                            // further dictionaries. Unknown nested dictionaries
                            // cannot be safely interpreted as appearances.
                            if matches!(value, Object::Dict(_)) {
                                unknown = true;
                            } else {
                                pending.try_push(value)?;
                            }
                        } else {
                            unknown = true;
                        }
                    }
                }
                _ => {
                    unknown = true;
                }
            }
        }
    }
    let mut raw = 0usize;
    for _ in annots.raw_iter() {
        budget.charge(LimitCode::ContentElementCount, 1)?;
        raw += 1;
    }
    Ok((ok, unknown || count != raw))
}

fn missing_resources(dict: &Dict<'_>) -> bool {
    ["Font", "XObject"]
        .iter()
        .any(|key| dict.contains_key(key) && dict.get::<Dict<'_>>(key).is_none())
}

/// Mark indexes of `fonts` whose programs must stay whole.
pub(super) fn protect(
    page: &Page<'_>,
    version: &Version,
    fonts: &[(u32, u16, usize)],
    protected: &mut [bool],
    unknown_page_resources: bool,
    budget: &Budget,
) -> Result<(), Error> {
    let mut resources = BudgetVec::new(budget);
    resources.try_push(page.resources().clone())?;
    let mut ok = match page.raw().get::<Object<'_>>("Contents") {
        Some(value) => check_content(value, version, budget)?,
        None => !page.raw().contains_key("Contents"),
    };
    let (appearances_ok, appearances_unknown) = appearances(page, version, budget, &mut resources)?;
    let mut unknown = appearances_unknown || unknown_page_resources;
    ok &= appearances_ok;
    let mut seen = Seen::new(budget);
    let mut reached: BudgetVec<'_, usize> = BudgetVec::new(budget);
    while let Some(resource) = resources.pop() {
        budget.poll()?;
        for key in resource.fonts.keys() {
            budget.charge(LimitCode::ContentElementCount, 1)?;
            if let Some(reference) = resource.fonts.get_ref(key.as_ref())
                && let (Ok(number), Ok(generation)) = (
                    u32::try_from(reference.obj_number),
                    u16::try_from(reference.gen_number),
                )
            {
                let Some(dict) = resource.fonts.get::<Dict<'_>>(key.as_ref()) else {
                    unknown = true;
                    continue;
                };
                let resolved = dict.obj_id().and_then(|id| {
                    Some((
                        u32::try_from(id.obj_number).ok()?,
                        u16::try_from(id.gen_number).ok()?,
                    ))
                });
                if resolved.is_some_and(|id| id != (number, generation)) {
                    unknown = true;
                }
                let first = fonts.partition_point(|&(n, g, _)| (n, g) < (number, generation));
                for &(n, g, index) in &fonts[first..] {
                    if (n, g) != (number, generation) {
                        break;
                    }
                    reached.try_push(index)?;
                }
            }
        }
        for key in resource.x_objects.keys() {
            budget.charge(LimitCode::ContentElementCount, 1)?;
            let Some(stream) = resource.get_x_object(&key) else {
                unknown = true;
                continue;
            };
            let Some(subtype) = stream.dict().get::<Name<'_>>("Subtype") else {
                unknown = true;
                continue;
            };
            if subtype.as_ref() != b"Form" {
                continue;
            }
            let Some(id) = stream.dict().obj_id() else {
                unknown = true;
                continue;
            };
            if !seen.insert(id, budget)? {
                continue;
            }
            ok &= readable(&stream, version, budget)?;
            if let Some(dict) = stream.dict().get::<Dict<'_>>("Resources") {
                unknown |= missing_resources(&dict);
                resources.try_push(Resources::new(dict))?;
            } else if stream.dict().contains_key("Resources") {
                unknown = true;
            }
        }
    }
    if unknown {
        protected.fill(true);
    } else if !ok {
        for &index in reached.iter() {
            protected[index] = true;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn form_cycles_are_visited_once_across_set_growth() {
        let budget = Budget::default();
        let mut seen = Seen::new(&budget);
        for number in 0..100 {
            assert!(
                seen.insert(ObjectIdentifier::new(number, 0), &budget)
                    .unwrap()
            );
        }
        for number in 0..100 {
            assert!(
                !seen
                    .insert(ObjectIdentifier::new(number, 0), &budget)
                    .unwrap()
            );
        }
        assert!(seen.insert(ObjectIdentifier::new(2, 1), &budget).unwrap());
    }

    #[test]
    fn form_walk_respects_memory_and_work_limits() {
        let memory = Budget::default().with_limit(LimitCode::MemorySize, Some(0));
        assert!(matches!(
            Seen::new(&memory).insert(ObjectIdentifier::new(1, 0), &memory),
            Err(Error::Limit(_))
        ));
        let work = Budget::default().with_limit(LimitCode::ContentElementCount, Some(0));
        assert!(matches!(
            Seen::new(&work).insert(ObjectIdentifier::new(1, 0), &work),
            Err(Error::Limit(_))
        ));
    }
}
