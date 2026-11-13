//! Canonical number-tree read and replace helpers (ISO 32000-2 7.9.7).

use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::refs::{self, copy_for_edit};
use crate::value::{Array, Dict, Name, Reference, Value};
use crate::version::table::Table;
use crate::version::{Transaction, Version};
use crate::{Error, LimitExceeded, Result};

use super::{
    MAX_LEAF_PAIRS, MAX_TREE_DEPTH, MAX_TREE_NODES, build_hierarchy, catalog_of,
    collect_tree_nodes, editable, insert_limits, located_in, resolve_in,
};

/// Appends the `(key, value)` pairs of the number tree at `root` to `pairs` in
/// tree order, visiting each indirect node at most once and refusing trees
/// deeper than [`MAX_TREE_DEPTH`] or larger than [`MAX_TREE_NODES`].
pub fn number_tree_pairs<'b>(
    version: &Version,
    root: &Value,
    pairs: &mut BudgetVec<'b, (i64, Value)>,
    budget: &'b Budget,
) -> Result<()> {
    let mut visited: Table<()> = Table::default();
    let mut pending = BudgetVec::new(budget);
    pending.try_push((copy_for_edit(root, budget)?, 0_usize))?;
    let mut nodes = 0_usize;
    while let Some((value, depth)) = pending.pop() {
        budget.poll()?;
        let (node, object) = match located_in(version, Some(&value), budget)? {
            None | Some((Value::Null, _)) => continue,
            Some((Value::Dict(node), object)) => (node, object),
            _ => return Err(Error::malformed("a number-tree node is not a dictionary")),
        };
        if let Some(reference) = object
            && visited.insert(reference.number(), (), budget)?.is_some()
        {
            continue;
        }
        editable(&node)?;
        nodes += 1;
        if nodes > MAX_TREE_NODES || depth > MAX_TREE_DEPTH {
            return Err(LimitExceeded::new(LimitCode::StructureTreeCount).into());
        }
        if let Some(Value::Array(nums)) = resolve_in(version, node.get("Nums"), budget)? {
            let mut items = nums.iter();
            while let (Some(key), Some(entry)) = (items.next(), items.next()) {
                budget.poll()?;
                if let Some(Value::Integer(key)) = resolve_in(version, Some(key), budget)? {
                    if pairs.len() >= MAX_TREE_NODES {
                        return Err(LimitExceeded::new(LimitCode::StructureTreeCount).into());
                    }
                    pairs.try_push((key, copy_for_edit(entry, budget)?))?;
                }
            }
        }
        if let Some(Value::Array(kids)) = resolve_in(version, node.get("Kids"), budget)? {
            for kid in kids.iter().rev() {
                pending.try_push((copy_for_edit(kid, budget)?, depth + 1))?;
            }
        }
    }
    Ok(())
}

/// Populates `root_dict` as a canonical number-tree root for non-empty `pairs`,
/// sorting and deduplicating keys (keeping the first entry for each key),
/// omitting `/Limits` on the root, and splitting past [`MAX_LEAF_PAIRS`] into
/// bounded indirect leaf and intermediate `/Kids` nodes with inline `/Limits`.
fn populate_number_root(
    transaction: &mut Transaction,
    root_dict: &mut Dict,
    mut pairs: BudgetVec<'_, (i64, Value)>,
    budget: &Budget,
) -> Result<()> {
    budget.charge(
        LimitCode::MemorySize,
        pairs.len().saturating_mul(size_of::<(i64, Value)>()) as u64,
    )?;
    pairs.sort_by_key(|(key, _)| *key);
    let mut unique = BudgetVec::new(budget);
    unique.try_reserve_exact(pairs.len())?;
    for (key, value) in pairs.into_vec() {
        budget.poll()?;
        if unique
            .last()
            .is_none_or(|(prev, _): &(i64, Value)| *prev != key)
        {
            unique.try_push((key, value))?;
        }
    }

    root_dict.remove("Nums");
    root_dict.remove("Kids");
    root_dict.remove("Limits");

    if unique.len() <= MAX_LEAF_PAIRS {
        let mut nums = BudgetVec::new(budget);
        nums.try_reserve_exact(2 * unique.len())?;
        for (key, value) in unique.into_vec() {
            nums.try_push(Value::Integer(key))?;
            nums.try_push(value)?;
        }
        #[expect(
            clippy::disallowed_methods,
            reason = "single-node root holds the fixed /Nums entry"
        )]
        root_dict.insert(
            Name::from("Nums"),
            Value::Array(Array::from(nums.into_vec())),
        );
        return Ok(());
    }

    let mut leaves = BudgetVec::new(budget);
    leaves.try_reserve_exact(unique.len().div_ceil(MAX_LEAF_PAIRS))?;
    for chunk in unique.chunks(MAX_LEAF_PAIRS) {
        budget.poll()?;
        let low = chunk[0].0;
        let high = chunk[chunk.len() - 1].0;
        let mut nums = BudgetVec::new(budget);
        nums.try_reserve_exact(2 * chunk.len())?;
        for (key, value) in chunk {
            nums.try_push(Value::Integer(*key))?;
            nums.try_push(copy_for_edit(value, budget)?)?;
        }
        let mut leaf = Dict::new();
        #[expect(
            clippy::disallowed_methods,
            reason = "leaf node holds fixed /Nums and /Limits entries"
        )]
        leaf.insert(
            Name::from("Nums"),
            Value::Array(Array::from(nums.into_vec())),
        );
        insert_limits(&mut leaf, Value::Integer(low), Value::Integer(high));
        let leaf_ref = transaction.allocate()?;
        transaction.write(leaf_ref.number(), &Value::Dict(leaf), budget)?;
        leaves.try_push((leaf_ref, low, high))?;
    }
    let kids = build_hierarchy(transaction, leaves, Value::Integer, budget)?;
    #[expect(
        clippy::disallowed_methods,
        reason = "multi-node root holds the fixed /Kids entry"
    )]
    root_dict.insert(Name::from("Kids"), Value::Array(kids));
    Ok(())
}

/// Replaces `holder[key]` with a canonical bounded number tree built from
/// `pairs` (or removes `holder[key]` when `pairs` is empty). Returns `(changed,
/// retired)` where `changed` reports whether `holder` itself was modified and
/// `retired` lists indirect objects of any prior tree to pass to
/// [`refs::free_unreferenced`] after writing `holder`.
pub fn replace_number_tree_entry<'b>(
    transaction: &mut Transaction,
    holder: &mut Dict,
    key: &str,
    pairs: BudgetVec<'_, (i64, Value)>,
    owner: Option<Reference>,
    budget: &'b Budget,
) -> Result<(bool, BudgetVec<'b, u32>)> {
    if pairs.is_empty() {
        let retired = match holder.get(key) {
            Some(old) => collect_tree_nodes(transaction.version(), old, true, budget)?,
            None => BudgetVec::new(budget),
        };
        let changed = holder.remove(key).is_some();
        return Ok((changed, retired));
    }

    let existing_indirect = match located_in(transaction.version(), holder.get(key), budget)? {
        Some((Value::Dict(dict), Some(tree_ref)))
            if Some(tree_ref) != owner && editable(&dict).is_ok() =>
        {
            Some((tree_ref, dict))
        }
        _ => None,
    };

    if let Some((tree_ref, mut root_dict)) = existing_indirect {
        let retired = match holder.get(key) {
            Some(old) => collect_tree_nodes(transaction.version(), old, false, budget)?,
            None => BudgetVec::new(budget),
        };
        populate_number_root(transaction, &mut root_dict, pairs, budget)?;
        transaction.write(tree_ref.number(), &Value::Dict(root_dict), budget)?;
        return Ok((false, retired));
    }

    let retired = match holder.get(key) {
        Some(old) => collect_tree_nodes(transaction.version(), old, true, budget)?,
        None => BudgetVec::new(budget),
    };
    let mut root_dict = Dict::new();
    populate_number_root(transaction, &mut root_dict, pairs, budget)?;
    let tree_ref = transaction.allocate()?;
    transaction.write(tree_ref.number(), &Value::Dict(root_dict), budget)?;
    #[expect(
        clippy::disallowed_methods,
        reason = "replaces or adds one named number-tree entry on the holder dictionary"
    )]
    holder.insert(Name::from(key), Value::Reference(tree_ref));
    Ok((true, retired))
}

/// Replaces `Catalog[key]` (such as `/PageLabels`) with a canonical bounded
/// number tree built from `pairs` (removing `Catalog[key]` when `pairs` is
/// empty) and frees unreferenced retired `/Kids` and root nodes.
pub fn replace_catalog_number_tree(
    cache: &refs::Cache,
    transaction: &mut Transaction,
    key: &str,
    pairs: BudgetVec<'_, (i64, Value)>,
    budget: &Budget,
) -> Result<()> {
    let (catalog_ref, mut catalog_dict) = catalog_of(transaction.version(), budget)?;
    let (changed, retired) = replace_number_tree_entry(
        transaction,
        &mut catalog_dict,
        key,
        pairs,
        Some(catalog_ref),
        budget,
    )?;
    if changed {
        transaction.write(catalog_ref.number(), &Value::Dict(catalog_dict), budget)?;
    }
    refs::free_unreferenced(cache, transaction, &[], &retired, budget)?;
    Ok(())
}
