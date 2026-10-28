//! Reference-driven discovery of objects that may hold a form widget of a
//! removed page. This is a conservative candidate set, not a deletion plan.

use std::ops::Deref;
use std::sync::Arc;

use crate::budget::BudgetVec;
use crate::memo::Reads;
use crate::value::{Reference, Value};
use crate::version::table::Table;
use crate::{Error, Result};

use super::navigation::{dict_of, is_aliasing, is_in};
use super::{References, Referrer, Step, find};

/// The longest chain of indirect values that page annotations may use.
const MAX_CHAIN: usize = crate::pages::MAX_CHAIN;
/// The longest reverse path that the impact walk follows.
const MAX_DEPTH: usize = 64;

/// Returns objects that may hold widgets associated with the removed pages.
///
/// Seeds are indirect entries in the pages' `/Annots` and objects whose
/// `/P` names a removed page. The walk follows incoming references, stopping
/// at the catalog and page or page-tree dictionaries. A catalog containing
/// a direct affected form can itself be returned as a holder; callers must
/// preserve the catalog and edit only its embedded form. It intentionally
/// over-approximates: a reverse edge may have a stale generation or an
/// unrelated role. A caller must verify membership and generations before
/// editing. The result includes each live object's current generation, once,
/// sorted by object number. `removed` and `retained` are sorted page numbers.
///
/// Work is proportional to the removed pages and the reverse references
/// reachable from their candidates, not the unrelated form tree. Bounds,
/// cancellation and allocation charges use `reads`' operation budget.
pub fn affected_form_objects<'b>(
    reads: &mut Reads<'b>,
    index: &References,
    removed: &[u32],
    retained: &[u32],
) -> Result<BudgetVec<'b, Reference>> {
    let budget = reads.budget();
    budget.poll()?;
    let mut seeds = Seeds {
        queue: BudgetVec::new(budget),
        catalog_holders: BudgetVec::new(budget),
    };
    let mut result = BudgetVec::new(budget);
    if removed.is_empty() {
        return Ok(result);
    }
    let root_reference = match reads.trailer().get("Root") {
        Some(Value::Reference(reference)) => Some(*reference),
        _ => None,
    };
    let resolved_root = match root_reference {
        Some(reference) => {
            resolve_reference(reads, reference)?.map(|(reference, _)| reference.number())
        }
        None => None,
    };
    let root = [
        root_reference.map(|reference| reference.number()),
        resolved_root,
    ];
    let mut seen = Table::new();
    for &page in removed {
        budget.poll()?;
        let Some(object) = reads.get(page)? else {
            continue;
        };
        let generation = object.generation();
        if let Some(annots) = dict_of(&object).and_then(|dict| dict.get("Annots"))
            && let Some(resolved) = resolve(reads, annots)?
            && let Value::Array(items) = &*resolved
        {
            for item in items {
                budget.poll()?;
                if let Value::Reference(reference) = item
                    && let Some((reference, value)) = resolve_reference(reads, *reference)?
                    && let Value::Dict(dict) = &*value
                {
                    if protected(reference.number(), root, removed, retained, dict) {
                        return Err(Error::malformed(
                            "a page annotation aliases a page or catalog",
                        ));
                    }
                    seeds.queue.try_push((reference, 1_usize))?;
                }
            }
        }
        seed_page_referrers(
            reads,
            index,
            Reference::new(page, generation),
            root,
            removed,
            retained,
            &mut seeds,
        )?;
    }
    while let Some((reference, depth)) = seeds.queue.pop() {
        budget.poll()?;
        if seen.insert(reference.number(), (), budget)?.is_some() {
            continue;
        }
        let Some(object) = reads.get(reference.number())? else {
            continue;
        };
        if object.generation() != reference.generation() {
            continue;
        }
        if let Value::Dict(dict) = &**object.value()
            && protected(reference.number(), root, removed, retained, dict)
        {
            continue;
        }
        result.try_push(reference)?;
        for &owner in index.referrers(reference.number()) {
            budget.poll()?;
            if seen.get(owner).is_some() {
                continue;
            }
            let Some(object) = reads.get(owner)? else {
                continue;
            };
            if let Value::Dict(dict) = &**object.value()
                && protected(owner, root, removed, retained, dict)
            {
                continue;
            }
            if depth >= MAX_DEPTH {
                return Err(Error::malformed(
                    "form impact reference chain exceeds 64 levels",
                ));
            }
            seeds
                .queue
                .try_push((Reference::new(owner, object.generation()), depth + 1))?;
        }
    }
    for reference in seeds.catalog_holders.iter().copied() {
        budget.poll()?;
        if seen.insert(reference.number(), (), budget)?.is_none() {
            result.try_push(reference)?;
        }
    }
    result.sort_unstable_by_key(|reference| (reference.number(), reference.generation()));
    budget.poll()?;
    Ok(result)
}

struct Seeds<'b> {
    queue: BudgetVec<'b, (Reference, usize)>,
    catalog_holders: BudgetVec<'b, Reference>,
}

fn seed_page_referrers(
    reads: &mut Reads<'_>,
    index: &References,
    page: Reference,
    root: [Option<u32>; 2],
    removed: &[u32],
    retained: &[u32],
    seeds: &mut Seeds<'_>,
) -> Result<()> {
    let budget = reads.budget();
    let mut aliases = BudgetVec::new(budget);
    let mut seen = Table::new();
    aliases.try_push((page, 0_usize))?;
    while let Some((target, depth)) = aliases.pop() {
        budget.poll()?;
        if seen.insert(target.number(), (), budget)?.is_some() {
            continue;
        }
        for &owner in index.referrers(target.number()) {
            budget.poll()?;
            let Some(object) = reads.get(owner)? else {
                continue;
            };
            if root.contains(&Some(owner)) {
                let found = find(reads, Referrer::Object(owner), target.number())?;
                if found.iter().any(|found| {
                    found.reference == target
                        && matches!(found.path.first(), Some(Step::Key(key)) if key.as_bytes() == b"AcroForm")
                        && matches!(found.path.last(), Some(Step::Key(key)) if key.as_bytes() == b"P")
                }) {
                    seeds.catalog_holders.try_push(Reference::new(owner, object.generation()))?;
                }
                continue;
            }
            if is_in(removed, owner) || is_in(retained, owner) {
                continue;
            }
            let reference = Reference::new(owner, object.generation());
            match &**object.value() {
                Value::Reference(to) if *to == target => {
                    if seen.get(owner).is_none() {
                        if depth >= MAX_CHAIN {
                            return Err(Error::malformed(
                                "form impact page alias chain exceeds the indirection limit",
                            ));
                        }
                        aliases.try_push((reference, depth + 1))?;
                    }
                }
                value => {
                    if matches!(value, Value::Dict(dict) if is_aliasing(dict)) {
                        continue;
                    }
                    let found = find(reads, Referrer::Object(owner), target.number())?;
                    if found.iter().any(|found| {
                        found.reference == target
                            && matches!(found.path.last(), Some(Step::Key(key)) if key.as_bytes() == b"P")
                    }) {
                        seeds.queue.try_push((reference, 1))?;
                    }
                }
            }
        }
    }
    Ok(())
}

fn protected(
    number: u32,
    root: [Option<u32>; 2],
    removed: &[u32],
    retained: &[u32],
    dict: &crate::value::Dict,
) -> bool {
    root.contains(&Some(number))
        || is_in(removed, number)
        || is_in(retained, number)
        || is_aliasing(dict)
}

enum Resolved<'a> {
    Borrowed(&'a Value),
    Shared(Arc<Value>),
}

impl Deref for Resolved<'_> {
    type Target = Value;
    fn deref(&self) -> &Value {
        match self {
            Self::Borrowed(value) => value,
            Self::Shared(value) => value,
        }
    }
}

fn resolve<'a>(reads: &mut Reads<'_>, value: &'a Value) -> Result<Option<Resolved<'a>>> {
    match value {
        Value::Reference(reference) => {
            Ok(resolve_reference(reads, *reference)?.map(|(_, value)| Resolved::Shared(value)))
        }
        _ => Ok(Some(Resolved::Borrowed(value))),
    }
}

fn resolve_reference(
    reads: &mut Reads<'_>,
    mut reference: Reference,
) -> Result<Option<(Reference, Arc<Value>)>> {
    for _ in 0..MAX_CHAIN {
        reads.budget().poll()?;
        let Some(object) = reads.get(reference.number())? else {
            return Ok(None);
        };
        if object.generation() != reference.generation() {
            return Ok(None);
        }
        match &**object.value() {
            Value::Reference(next) => reference = *next,
            _ => return Ok(Some((reference, Arc::clone(object.value())))),
        }
    }
    Err(Error::malformed(
        "form impact reference chain exceeds the indirection limit",
    ))
}
