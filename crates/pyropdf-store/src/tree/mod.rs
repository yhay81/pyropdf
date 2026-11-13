//! Canonical bounded-depth, bounded-leaf name-tree, number-tree, and
//! `/StructTreeRoot` `/ParentTree` helpers (ISO 32000-2 7.9.6, 7.9.7,
//! 14.7.5.4).
//!
//! Every tree walk and multi-level build is iterative (`MAX_TREE_DEPTH = 64`),
//! visits each indirect node at most once via a budgeted visited table,
//! charges dynamic growth to the caller's [`Budget`] (DESIGN.md F6), maintains
//! `/Limits` inline on non-root nodes while omitting `/Limits` on new or
//! rebuilt roots, bounds leaves and intermediate `/Kids` fanout at
//! [`MAX_LEAF_PAIRS`] (`64`), and frees unreferenced retired `/Kids` nodes
//! through [`refs::free_unreferenced`].

mod name;
mod number;
mod parent;
#[cfg(test)]
mod tests;

use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::pages::MAX_CHAIN;
use crate::refs::copy_for_edit;
use crate::value::{Array, Dict, Name, Reference, Value};
use crate::version::table::Table;
use crate::version::{Transaction, Version};
use crate::{Error, LimitExceeded, Result};

pub use name::{
    insert_catalog_embedded_files, insert_name_tree, remove_catalog_embedded_files,
    remove_name_tree, replace_catalog_embedded_files, replace_name_tree_entry,
};
pub use number::{number_tree_pairs, replace_catalog_number_tree, replace_number_tree_entry};
pub use parent::{append_parent_tree, append_parent_tree_entry};

/// Maximum depth below the root of a name or number tree (`0` at the root).
pub const MAX_TREE_DEPTH: usize = 64;

/// Maximum key-value pairs in one leaf node (`128` array elements), and
/// maximum child references in one `/Kids` node before splitting.
pub const MAX_LEAF_PAIRS: usize = 64;

/// Maximum distinct tree nodes or entries visited in one tree scan.
pub(crate) const MAX_TREE_NODES: usize = 1 << 20;

/// Resolves reference aliases up to [`MAX_CHAIN`] steps while retaining the
/// identity of the final indirect object, returning a budgeted editable copy.
pub(crate) fn located_in(
    version: &Version,
    value: Option<&Value>,
    budget: &Budget,
) -> Result<Option<(Value, Option<Reference>)>> {
    let mut current = match value {
        None => return Ok(None),
        Some(Value::Reference(reference)) => *reference,
        Some(direct) => return copy_for_edit(direct, budget).map(|copied| Some((copied, None))),
    };
    for _ in 0..MAX_CHAIN {
        budget.poll()?;
        let Some(object) = version
            .get(current.number(), budget)?
            .filter(|object| object.generation() == current.generation())
        else {
            return Ok(None);
        };
        match &**object.value() {
            Value::Reference(next) => current = *next,
            other => {
                return copy_for_edit(other, budget).map(|copied| Some((copied, Some(current))));
            }
        }
    }
    Err(Error::malformed("a tree reference cycle or overlong chain"))
}

pub(crate) fn resolve_in(
    version: &Version,
    value: Option<&Value>,
    budget: &Budget,
) -> Result<Option<Value>> {
    Ok(located_in(version, value, budget)?.map(|(value, _)| value))
}

/// Refuses a lookup-tree dictionary that aliases a page, page-tree node, or
/// catalog dictionary.
pub(crate) fn editable(dict: &Dict) -> Result<()> {
    if matches!(
        dict.get("Type"),
        Some(Value::Name(kind)) if matches!(kind.as_bytes(), b"Page" | b"Pages" | b"Catalog")
    ) {
        return Err(Error::malformed(
            "a lookup tree node aliases a page, page tree, or catalog",
        ));
    }
    Ok(())
}

/// Resolves the trailer's `/Root` catalog dictionary as an editable copy.
pub(crate) fn catalog_of(version: &Version, budget: &Budget) -> Result<(Reference, Dict)> {
    match located_in(version, version.trailer().get("Root"), budget)? {
        Some((Value::Dict(dict), Some(reference))) => Ok((reference, dict)),
        _ => Err(Error::malformed(
            "the catalog is not an indirect dictionary",
        )),
    }
}

/// Collects indirect object numbers belonging to a name or number tree rooted
/// at `root`, bounded by [`MAX_TREE_DEPTH`] and [`MAX_TREE_NODES`], visiting
/// each indirect object at most once.
///
/// When `include_root` is `false`, the root dictionary's own object number is
/// omitted (for callers that overwrite the root dictionary in place), while
/// indirect `/Kids`, `/Names`, and `/Nums` descendants and arrays are included
/// so [`crate::refs::free_unreferenced`] can reclaim retired nodes.
pub fn collect_tree_nodes<'b>(
    version: &Version,
    root: &Value,
    include_root: bool,
    budget: &'b Budget,
) -> Result<BudgetVec<'b, u32>> {
    let mut visited: Table<()> = Table::default();
    let mut retired = BudgetVec::new(budget);
    let mut pending = BudgetVec::new(budget);
    pending.try_push((copy_for_edit(root, budget)?, 0_usize))?;
    let mut nodes = 0_usize;
    while let Some((value, depth)) = pending.pop() {
        budget.poll()?;
        let Some((resolved, object)) = located_in(version, Some(&value), budget)? else {
            continue;
        };
        if let Some(reference) = object {
            if visited.insert(reference.number(), (), budget)?.is_some() {
                continue;
            }
            if depth > 0 || include_root {
                retired.try_push(reference.number())?;
            }
        }
        let Value::Dict(node) = resolved else {
            continue;
        };
        nodes += 1;
        if nodes > MAX_TREE_NODES || depth > MAX_TREE_DEPTH {
            return Err(LimitExceeded::new(LimitCode::StructureTreeCount).into());
        }
        for array_key in ["Names", "Nums"] {
            if let Some((Value::Array(_), Some(arr_ref))) =
                located_in(version, node.get(array_key), budget)?
                && visited.insert(arr_ref.number(), (), budget)?.is_none()
            {
                retired.try_push(arr_ref.number())?;
            }
        }
        if let Some((Value::Array(kids), arr_ref)) = located_in(version, node.get("Kids"), budget)?
        {
            if let Some(arr_ref) = arr_ref
                && visited.insert(arr_ref.number(), (), budget)?.is_none()
            {
                retired.try_push(arr_ref.number())?;
            }
            for kid in kids.iter() {
                pending.try_push((copy_for_edit(kid, budget)?, depth + 1))?;
            }
        }
    }
    Ok(retired)
}

pub(crate) fn insert_limits(dict: &mut Dict, least: Value, greatest: Value) {
    #[expect(
        clippy::disallowed_methods,
        reason = "sets the fixed /Limits key with a two-element bounds array"
    )]
    dict.insert(
        Name::from("Limits"),
        Value::Array(Array::from(vec![least, greatest])),
    );
}

/// Groups `leaves` (`len >= 2`) into `MAX_LEAF_PAIRS`-way intermediate `/Kids`
/// levels until at most [`MAX_LEAF_PAIRS`] top-level references remain, and
/// returns the root's `/Kids` array.
pub(crate) fn build_hierarchy<'b, K: Clone>(
    transaction: &mut Transaction,
    mut level: BudgetVec<'b, (Reference, K, K)>,
    to_value: impl Fn(K) -> Value,
    budget: &'b Budget,
) -> Result<Array> {
    for _ in 0..MAX_TREE_DEPTH {
        budget.poll()?;
        if level.len() <= MAX_LEAF_PAIRS {
            let mut kids = BudgetVec::new(budget);
            kids.try_reserve_exact(level.len())?;
            for (reference, _, _) in level.into_vec() {
                kids.try_push(Value::Reference(reference))?;
            }
            return Ok(Array::from(kids.into_vec()));
        }
        let mut next = BudgetVec::new(budget);
        next.try_reserve_exact(level.len().div_ceil(MAX_LEAF_PAIRS))?;
        for chunk in level.chunks(MAX_LEAF_PAIRS) {
            budget.poll()?;
            let mut kids = BudgetVec::new(budget);
            kids.try_reserve_exact(chunk.len())?;
            for &(reference, _, _) in chunk {
                kids.try_push(Value::Reference(reference))?;
            }
            let low = chunk[0].1.clone();
            let high = chunk[chunk.len() - 1].2.clone();
            let mut mid = Dict::new();
            #[expect(
                clippy::disallowed_methods,
                reason = "intermediate tree node holds fixed /Kids and /Limits entries"
            )]
            mid.insert(
                Name::from("Kids"),
                Value::Array(Array::from(kids.into_vec())),
            );
            insert_limits(&mut mid, to_value(low.clone()), to_value(high.clone()));
            let mid_ref = transaction.allocate()?;
            transaction.write(mid_ref.number(), &Value::Dict(mid), budget)?;
            next.try_push((mid_ref, low, high))?;
        }
        level = next;
    }
    Err(LimitExceeded::new(LimitCode::StructureTreeCount).into())
}
