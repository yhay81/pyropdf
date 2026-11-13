//! Canonical `/StructTreeRoot` `/ParentTree` and `/ParentTreeNextKey` append
//! helpers (ISO 32000-2 14.7.5.4).

use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::refs::copy_for_edit;
use crate::value::{Array, Dict, Name, Reference, Value};
use crate::version::table::Table;
use crate::version::{Transaction, Version};
use crate::{Error, LimitExceeded, Result};

use super::{
    MAX_LEAF_PAIRS, MAX_TREE_DEPTH, build_hierarchy, catalog_of, editable, insert_limits,
    located_in, resolve_in,
};

struct AncestorFrame {
    object: Option<Reference>,
    dict: Dict,
    kids: Array,
    kids_array_object: Option<Reference>,
}

struct LeafFrame {
    object: Option<Reference>,
    dict: Dict,
    nums: Array,
    nums_array_object: Option<Reference>,
}

/// Walks down the rightmost `/Kids` spine of `tree_dict` up to
/// [`MAX_TREE_DEPTH`], checking cycles and identity aliasing.
fn walk_right_spine<'b>(
    transaction: &mut Transaction,
    tree_object: Option<Reference>,
    tree_dict: Dict,
    budget: &'b Budget,
) -> Result<(BudgetVec<'b, AncestorFrame>, LeafFrame)> {
    let mut visited: Table<()> = Table::default();
    if let Some(reference) = tree_object {
        visited.insert(reference.number(), (), budget)?;
    }
    let mut spine = BudgetVec::new(budget);
    let mut cur_object = tree_object;
    let mut cur_dict = tree_dict;

    loop {
        budget.poll()?;
        editable(&cur_dict)?;
        match located_in(transaction.version(), cur_dict.get("Kids"), budget)? {
            Some((Value::Array(mut kids), kids_arr_ref)) if !kids.is_empty() => {
                let last_val = kids.as_slice()[kids.len() - 1].clone();
                match located_in(transaction.version(), Some(&last_val), budget)? {
                    Some((Value::Dict(child_dict), Some(child_ref))) => {
                        if visited.insert(child_ref.number(), (), budget)?.is_some() {
                            if cur_dict.get("Nums").is_some() {
                                break;
                            }
                            let new_leaf_ref = transaction.allocate()?;
                            let mut extended = BudgetVec::new(budget);
                            extended.try_reserve_exact(kids.len() + 1)?;
                            for item in kids.iter() {
                                extended.try_push(copy_for_edit(item, budget)?)?;
                            }
                            extended.try_push(Value::Reference(new_leaf_ref))?;
                            kids = Array::from(extended.into_vec());
                            spine.try_push(AncestorFrame {
                                object: cur_object,
                                dict: cur_dict,
                                kids,
                                kids_array_object: kids_arr_ref,
                            })?;
                            return Ok((
                                spine,
                                LeafFrame {
                                    object: Some(new_leaf_ref),
                                    dict: Dict::new(),
                                    nums: Array::new(),
                                    nums_array_object: None,
                                },
                            ));
                        }
                        if spine.len() >= MAX_TREE_DEPTH {
                            return Err(LimitExceeded::new(LimitCode::StructureTreeCount).into());
                        }
                        spine.try_push(AncestorFrame {
                            object: cur_object,
                            dict: cur_dict,
                            kids,
                            kids_array_object: kids_arr_ref,
                        })?;
                        cur_object = Some(child_ref);
                        cur_dict = child_dict;
                    }
                    Some((Value::Dict(child_dict), None)) => {
                        if spine.len() >= MAX_TREE_DEPTH {
                            return Err(LimitExceeded::new(LimitCode::StructureTreeCount).into());
                        }
                        let child_ref = transaction.allocate()?;
                        let mut updated_kids = BudgetVec::new(budget);
                        updated_kids.try_reserve_exact(kids.len())?;
                        for item in &kids.as_slice()[..kids.len() - 1] {
                            updated_kids.try_push(copy_for_edit(item, budget)?)?;
                        }
                        updated_kids.try_push(Value::Reference(child_ref))?;
                        kids = Array::from(updated_kids.into_vec());
                        spine.try_push(AncestorFrame {
                            object: cur_object,
                            dict: cur_dict,
                            kids,
                            kids_array_object: kids_arr_ref,
                        })?;
                        cur_object = Some(child_ref);
                        cur_dict = child_dict;
                    }
                    None | Some((Value::Null, _)) => break,
                    Some(_) => {
                        return Err(Error::malformed("a parent-tree kid is not a dictionary"));
                    }
                }
            }
            Some((Value::Array(_), _)) | None | Some((Value::Null, _)) => break,
            Some(_) => return Err(Error::malformed("a parent-tree /Kids is not an array")),
        }
    }

    let (nums, nums_array_object) =
        match located_in(transaction.version(), cur_dict.get("Nums"), budget)? {
            Some((Value::Array(nums), arr_ref)) => (nums, arr_ref),
            None | Some((Value::Null, _)) => (Array::new(), None),
            Some(_) => return Err(Error::malformed("a parent-tree /Nums is not an array")),
        };
    Ok((
        spine,
        LeafFrame {
            object: cur_object,
            dict: cur_dict,
            nums,
            nums_array_object,
        },
    ))
}

fn limits_bounds(version: &Version, dict: &Dict, budget: &Budget) -> Result<Option<(i64, i64)>> {
    let Some(Value::Array(limits)) = resolve_in(version, dict.get("Limits"), budget)? else {
        return Ok(None);
    };
    let slice = limits.as_slice();
    if slice.is_empty() {
        return Ok(None);
    }
    let low = match resolve_in(version, slice.first(), budget)? {
        Some(Value::Integer(low)) => low,
        _ => return Ok(None),
    };
    let high = match resolve_in(version, slice.get(1), budget)? {
        Some(Value::Integer(high)) => high,
        _ => low,
    };
    Ok(Some((low, high)))
}

/// Finds the least integer key of `dict` or its leftmost `/Kids` descendant,
/// falling back to `fallback` when empty.
fn leftmost_key(
    version: &Version,
    dict: &Dict,
    kids: Option<&Array>,
    fallback: i64,
    budget: &Budget,
) -> Result<i64> {
    if let Some((low, _)) = limits_bounds(version, dict, budget)? {
        return Ok(low);
    }
    if let Some(Value::Array(nums)) = resolve_in(version, dict.get("Nums"), budget)?
        && let Some(first) = nums.as_slice().first()
        && let Some(Value::Integer(key)) = resolve_in(version, Some(first), budget)?
    {
        return Ok(key);
    }
    let Some(first_kid) = kids.and_then(|arr| arr.as_slice().first()) else {
        return Ok(fallback);
    };
    let mut visited: Table<()> = Table::default();
    let mut current = copy_for_edit(first_kid, budget)?;
    for _ in 0..=MAX_TREE_DEPTH {
        budget.poll()?;
        let Some((Value::Dict(node), object)) = located_in(version, Some(&current), budget)? else {
            return Ok(fallback);
        };
        if let Some(reference) = object
            && visited.insert(reference.number(), (), budget)?.is_some()
        {
            return Ok(fallback);
        }
        if let Some((low, _)) = limits_bounds(version, &node, budget)? {
            return Ok(low);
        }
        if let Some(Value::Array(nums)) = resolve_in(version, node.get("Nums"), budget)?
            && let Some(first) = nums.as_slice().first()
            && let Some(Value::Integer(key)) = resolve_in(version, Some(first), budget)?
        {
            return Ok(key);
        }
        let Some(Value::Array(next_kids)) = resolve_in(version, node.get("Kids"), budget)? else {
            return Ok(fallback);
        };
        let Some(first) = next_kids.as_slice().first() else {
            return Ok(fallback);
        };
        current = copy_for_edit(first, budget)?;
    }
    Ok(fallback)
}

fn write_tree_root(
    transaction: &mut Transaction,
    root_dict: &mut Dict,
    tree_object: Option<Reference>,
    tree_dict: Dict,
    budget: &Budget,
) -> Result<()> {
    match tree_object {
        Some(reference) => {
            transaction.write(reference.number(), &Value::Dict(tree_dict), budget)?;
            Ok(())
        }
        None => {
            #[expect(
                clippy::disallowed_methods,
                reason = "sets the fixed /ParentTree entry on StructTreeRoot"
            )]
            root_dict.insert(Name::from("ParentTree"), Value::Dict(tree_dict));
            Ok(())
        }
    }
}

fn write_kids_on_node(
    transaction: &mut Transaction,
    dict: &mut Dict,
    kids_array_object: Option<Reference>,
    kids: Array,
    budget: &Budget,
) -> Result<()> {
    match kids_array_object {
        Some(arr_ref) => {
            transaction.write(arr_ref.number(), &Value::Array(kids), budget)?;
            Ok(())
        }
        None => {
            #[expect(
                clippy::disallowed_methods,
                reason = "sets the fixed /Kids entry on a parent-tree node"
            )]
            dict.insert(Name::from("Kids"), Value::Array(kids));
            Ok(())
        }
    }
}

fn allocate_leaf(
    transaction: &mut Transaction,
    pairs: &[(Value, Value)],
    low: i64,
    high: i64,
    budget: &Budget,
) -> Result<(Reference, i64, i64)> {
    let mut nums = BudgetVec::new(budget);
    nums.try_reserve_exact(2 * pairs.len())?;
    for (k, v) in pairs {
        nums.try_push(copy_for_edit(k, budget)?)?;
        nums.try_push(copy_for_edit(v, budget)?)?;
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
    Ok((leaf_ref, low, high))
}

fn allocate_intermediate(
    transaction: &mut Transaction,
    children: &[(Reference, i64, i64)],
    budget: &Budget,
) -> Result<(Reference, i64, i64)> {
    let mut kids = BudgetVec::new(budget);
    kids.try_reserve_exact(children.len())?;
    for &(reference, _, _) in children {
        kids.try_push(Value::Reference(reference))?;
    }
    let low = children[0].1;
    let high = children[children.len() - 1].2;
    let mut mid = Dict::new();
    #[expect(
        clippy::disallowed_methods,
        reason = "intermediate node holds fixed /Kids and /Limits entries"
    )]
    mid.insert(
        Name::from("Kids"),
        Value::Array(Array::from(kids.into_vec())),
    );
    insert_limits(&mut mid, Value::Integer(low), Value::Integer(high));
    let mid_ref = transaction.allocate()?;
    transaction.write(mid_ref.number(), &Value::Dict(mid), budget)?;
    Ok((mid_ref, low, high))
}

/// Appends strictly ascending `(key, value)` pairs `added` onto `root_dict`'s
/// `/ParentTree` (creating `/ParentTree` if absent) and sets
/// `root_dict["ParentTreeNextKey"] = next_key`.
///
/// Leaves and intermediate `/Kids` nodes are bounded at [`MAX_LEAF_PAIRS`]
/// (`64`) pairs/children, growing homogeneously along the right spine so leaf
/// nodes never mix with intermediate `/Kids` nodes on multi-level trees.
pub fn append_parent_tree(
    transaction: &mut Transaction,
    root_dict: &mut Dict,
    root_ref: Option<Reference>,
    added: &[(i64, Value)],
    next_key: i64,
    budget: &Budget,
) -> Result<()> {
    if next_key > i64::from(i32::MAX) {
        return Err(LimitExceeded::new(LimitCode::StructureTreeCount).into());
    }
    let (tree_object, tree_dict) =
        match located_in(transaction.version(), root_dict.get("ParentTree"), budget)? {
            Some((Value::Dict(dict), object)) => {
                editable(&dict)?;
                if root_ref.is_some() && root_ref == object {
                    return Err(Error::malformed("a structure object has conflicting roles"));
                }
                (object, dict)
            }
            None | Some((Value::Null, _)) => {
                let tree_ref = transaction.allocate()?;
                let mut empty = Dict::new();
                #[expect(
                    clippy::disallowed_methods,
                    reason = "new parent tree holds the fixed /Nums entry"
                )]
                empty.insert(Name::from("Nums"), Value::Array(Array::new()));
                #[expect(
                    clippy::disallowed_methods,
                    reason = "sets the fixed /ParentTree entry on StructTreeRoot"
                )]
                root_dict.insert(Name::from("ParentTree"), Value::Reference(tree_ref));
                if added.is_empty() {
                    transaction.write(tree_ref.number(), &Value::Dict(empty), budget)?;
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "sets the fixed /ParentTreeNextKey entry on StructTreeRoot"
                    )]
                    root_dict.insert(Name::from("ParentTreeNextKey"), Value::Integer(next_key));
                    return Ok(());
                }
                (Some(tree_ref), empty)
            }
            Some(_) => return Err(Error::malformed("the parent tree is not a dictionary")),
        };

    if added.is_empty() {
        write_tree_root(transaction, root_dict, tree_object, tree_dict, budget)?;
        #[expect(
            clippy::disallowed_methods,
            reason = "sets the fixed /ParentTreeNextKey entry on StructTreeRoot"
        )]
        root_dict.insert(Name::from("ParentTreeNextKey"), Value::Integer(next_key));
        return Ok(());
    }

    let (spine, leaf) = walk_right_spine(transaction, tree_object, tree_dict, budget)?;
    append_on_spine(transaction, root_dict, spine, leaf, added, budget)?;
    #[expect(
        clippy::disallowed_methods,
        reason = "sets the fixed /ParentTreeNextKey entry on StructTreeRoot"
    )]
    root_dict.insert(Name::from("ParentTreeNextKey"), Value::Integer(next_key));
    Ok(())
}

fn append_on_spine(
    transaction: &mut Transaction,
    root_dict: &mut Dict,
    spine: BudgetVec<'_, AncestorFrame>,
    mut leaf: LeafFrame,
    added: &[(i64, Value)],
    budget: &Budget,
) -> Result<()> {
    let first_added_key = added[0].0;
    let last_added_key = added[added.len() - 1].0;
    let even_len = leaf.nums.len() & !1;
    let existing_pairs = even_len / 2;

    if existing_pairs.saturating_add(added.len()) <= MAX_LEAF_PAIRS {
        let first_num = if even_len >= 2 {
            match resolve_in(transaction.version(), leaf.nums.as_slice().first(), budget)? {
                Some(Value::Integer(k)) => k,
                _ => first_added_key,
            }
        } else {
            first_added_key
        };
        let leaf_low = match limits_bounds(transaction.version(), &leaf.dict, budget)? {
            Some((low, _)) => low,
            None => first_num,
        };
        let mut updated = BudgetVec::new(budget);
        updated.try_reserve_exact(even_len + 2 * added.len())?;
        for item in &leaf.nums.as_slice()[..even_len] {
            updated.try_push(copy_for_edit(item, budget)?)?;
        }
        for (key, value) in added {
            updated.try_push(Value::Integer(*key))?;
            updated.try_push(copy_for_edit(value, budget)?)?;
        }
        match leaf.nums_array_object {
            Some(arr_ref) => {
                transaction.write(
                    arr_ref.number(),
                    &Value::Array(Array::from(updated.into_vec())),
                    budget,
                )?;
            }
            None => {
                #[expect(
                    clippy::disallowed_methods,
                    reason = "sets the fixed /Nums entry on the parent-tree leaf"
                )]
                leaf.dict.insert(
                    Name::from("Nums"),
                    Value::Array(Array::from(updated.into_vec())),
                );
            }
        }
        if !spine.is_empty() || leaf.dict.get("Limits").is_some() {
            insert_limits(
                &mut leaf.dict,
                Value::Integer(leaf_low),
                Value::Integer(last_added_key),
            );
        }
        if spine.is_empty() {
            return write_tree_root(transaction, root_dict, leaf.object, leaf.dict, budget);
        }
        let leaf_ref = leaf
            .object
            .ok_or_else(|| Error::malformed("a non-root parent-tree leaf must be indirect"))?;
        transaction.write(leaf_ref.number(), &Value::Dict(leaf.dict), budget)?;

        for (idx, mut anc) in spine.into_vec().into_iter().enumerate().rev() {
            budget.poll()?;
            if idx > 0 || anc.dict.get("Limits").is_some() {
                let anc_low = leftmost_key(
                    transaction.version(),
                    &anc.dict,
                    Some(&anc.kids),
                    leaf_low,
                    budget,
                )?;
                insert_limits(
                    &mut anc.dict,
                    Value::Integer(anc_low),
                    Value::Integer(last_added_key),
                );
                if idx == 0 {
                    write_tree_root(transaction, root_dict, anc.object, anc.dict, budget)?;
                } else if let Some(anc_ref) = anc.object {
                    transaction.write(anc_ref.number(), &Value::Dict(anc.dict), budget)?;
                }
            } else if idx == 0 && anc.object.is_none() {
                write_tree_root(transaction, root_dict, None, anc.dict, budget)?;
            }
        }
        return Ok(());
    }

    // Leaf overflows `MAX_LEAF_PAIRS`: split into <= 64-pair leaves.
    let mut extra_children: BudgetVec<'_, (Reference, i64, i64)>;
    let mut kept_last_high: i64;

    if !spine.is_empty() && existing_pairs == MAX_LEAF_PAIRS {
        let first_existing =
            match resolve_in(transaction.version(), leaf.nums.as_slice().first(), budget)? {
                Some(Value::Integer(k)) => k,
                _ => first_added_key,
            };
        let last_existing = match resolve_in(
            transaction.version(),
            leaf.nums.as_slice().get(even_len - 2),
            budget,
        )? {
            Some(Value::Integer(k)) => k,
            _ => first_added_key,
        };
        kept_last_high = match limits_bounds(transaction.version(), &leaf.dict, budget)? {
            Some((_, high)) => high,
            None => {
                insert_limits(
                    &mut leaf.dict,
                    Value::Integer(first_existing),
                    Value::Integer(last_existing),
                );
                if let Some(leaf_ref) = leaf.object {
                    transaction.write(leaf_ref.number(), &Value::Dict(leaf.dict), budget)?;
                }
                last_existing
            }
        };
        extra_children = BudgetVec::new(budget);
        extra_children.try_reserve_exact(added.len().div_ceil(MAX_LEAF_PAIRS))?;
        for chunk in added.chunks(MAX_LEAF_PAIRS) {
            budget.poll()?;
            let mut raw = BudgetVec::new(budget);
            raw.try_reserve_exact(chunk.len())?;
            for (k, v) in chunk {
                raw.try_push((Value::Integer(*k), copy_for_edit(v, budget)?))?;
            }
            let low = chunk[0].0;
            let high = chunk[chunk.len() - 1].0;
            extra_children.try_push(allocate_leaf(transaction, &raw, low, high, budget)?)?;
        }
    } else {
        let mut combined: BudgetVec<'_, (Value, Value, i64)> = BudgetVec::new(budget);
        combined.try_reserve_exact(existing_pairs.saturating_add(added.len()))?;
        let slice = &leaf.nums.as_slice()[..even_len];
        for [k_val, v_val] in slice.as_chunks::<2>().0 {
            budget.poll()?;
            let key_int = match resolve_in(transaction.version(), Some(k_val), budget)? {
                Some(Value::Integer(k)) => k,
                _ => 0,
            };
            combined.try_push((
                copy_for_edit(k_val, budget)?,
                copy_for_edit(v_val, budget)?,
                key_int,
            ))?;
        }
        for (key, value) in added {
            budget.poll()?;
            combined.try_push((Value::Integer(*key), copy_for_edit(value, budget)?, *key))?;
        }

        if spine.is_empty() {
            let root_low = match limits_bounds(transaction.version(), &leaf.dict, budget)? {
                Some((low, _)) => low,
                None => combined[0].2,
            };
            let mut leaves = BudgetVec::new(budget);
            leaves.try_reserve_exact(combined.len().div_ceil(MAX_LEAF_PAIRS))?;
            for chunk in combined.chunks(MAX_LEAF_PAIRS) {
                budget.poll()?;
                let mut raw = BudgetVec::new(budget);
                raw.try_reserve_exact(chunk.len())?;
                for (k, v, _) in chunk {
                    raw.try_push((copy_for_edit(k, budget)?, copy_for_edit(v, budget)?))?;
                }
                let low = chunk[0].2;
                let high = chunk[chunk.len() - 1].2;
                leaves.try_push(allocate_leaf(transaction, &raw, low, high, budget)?)?;
            }
            let top_kids = build_hierarchy(transaction, leaves, Value::Integer, budget)?;
            leaf.dict.remove("Nums");
            #[expect(
                clippy::disallowed_methods,
                reason = "promoted parent-tree root holds the fixed /Kids entry"
            )]
            leaf.dict.insert(Name::from("Kids"), Value::Array(top_kids));
            if leaf.dict.get("Limits").is_some() {
                insert_limits(
                    &mut leaf.dict,
                    Value::Integer(root_low),
                    Value::Integer(last_added_key),
                );
            }
            return write_tree_root(transaction, root_dict, leaf.object, leaf.dict, budget);
        }

        let leaf_ref = leaf
            .object
            .ok_or_else(|| Error::malformed("a non-root parent-tree leaf must be indirect"))?;
        let first_chunk = &combined[..MAX_LEAF_PAIRS];
        let leaf_low = match limits_bounds(transaction.version(), &leaf.dict, budget)? {
            Some((low, _)) => low,
            None => first_chunk[0].2,
        };
        kept_last_high = first_chunk[first_chunk.len() - 1].2;
        let mut first_nums = BudgetVec::new(budget);
        first_nums.try_reserve_exact(2 * first_chunk.len())?;
        for (k, v, _) in first_chunk {
            first_nums.try_push(copy_for_edit(k, budget)?)?;
            first_nums.try_push(copy_for_edit(v, budget)?)?;
        }
        #[expect(
            clippy::disallowed_methods,
            reason = "rewritten leaf node holds fixed /Nums and /Limits entries"
        )]
        leaf.dict.insert(
            Name::from("Nums"),
            Value::Array(Array::from(first_nums.into_vec())),
        );
        insert_limits(
            &mut leaf.dict,
            Value::Integer(leaf_low),
            Value::Integer(kept_last_high),
        );
        transaction.write(leaf_ref.number(), &Value::Dict(leaf.dict), budget)?;

        let rest = &combined[MAX_LEAF_PAIRS..];
        extra_children = BudgetVec::new(budget);
        extra_children.try_reserve_exact(rest.len().div_ceil(MAX_LEAF_PAIRS))?;
        for chunk in rest.chunks(MAX_LEAF_PAIRS) {
            budget.poll()?;
            let mut raw = BudgetVec::new(budget);
            raw.try_reserve_exact(chunk.len())?;
            for (k, v, _) in chunk {
                raw.try_push((copy_for_edit(k, budget)?, copy_for_edit(v, budget)?))?;
            }
            let low = chunk[0].2;
            let high = chunk[chunk.len() - 1].2;
            extra_children.try_push(allocate_leaf(transaction, &raw, low, high, budget)?)?;
        }
    }

    // Propagate `extra_children` and updated upper bounds up `spine`.
    for (idx, mut anc) in spine.into_vec().into_iter().enumerate().rev() {
        budget.poll()?;
        let anc_low = leftmost_key(
            transaction.version(),
            &anc.dict,
            Some(&anc.kids),
            first_added_key,
            budget,
        )?;

        if extra_children.is_empty() {
            if idx > 0 || anc.dict.get("Limits").is_some() {
                insert_limits(
                    &mut anc.dict,
                    Value::Integer(anc_low),
                    Value::Integer(last_added_key),
                );
                if idx == 0 {
                    write_tree_root(transaction, root_dict, anc.object, anc.dict, budget)?;
                } else if let Some(anc_ref) = anc.object {
                    transaction.write(anc_ref.number(), &Value::Dict(anc.dict), budget)?;
                }
            } else if idx == 0 && anc.object.is_none() {
                write_tree_root(transaction, root_dict, None, anc.dict, budget)?;
            }
            continue;
        }

        if anc.kids.len().saturating_add(extra_children.len()) <= MAX_LEAF_PAIRS {
            let mut updated_kids = BudgetVec::new(budget);
            updated_kids.try_reserve_exact(anc.kids.len() + extra_children.len())?;
            for item in anc.kids.iter() {
                updated_kids.try_push(copy_for_edit(item, budget)?)?;
            }
            for &(child_ref, _, _) in extra_children.iter() {
                updated_kids.try_push(Value::Reference(child_ref))?;
            }
            write_kids_on_node(
                transaction,
                &mut anc.dict,
                anc.kids_array_object,
                Array::from(updated_kids.into_vec()),
                budget,
            )?;
            if idx > 0 || anc.dict.get("Limits").is_some() {
                insert_limits(
                    &mut anc.dict,
                    Value::Integer(anc_low),
                    Value::Integer(last_added_key),
                );
            }
            if idx == 0 {
                write_tree_root(transaction, root_dict, anc.object, anc.dict, budget)?;
            } else if let Some(anc_ref) = anc.object {
                transaction.write(anc_ref.number(), &Value::Dict(anc.dict), budget)?;
            }
            extra_children.clear();
            continue;
        }

        // `anc` exceeds `MAX_LEAF_PAIRS` children and splits.
        let take = MAX_LEAF_PAIRS.saturating_sub(anc.kids.len());
        let mut kept_kids = BudgetVec::new(budget);
        kept_kids.try_reserve_exact(anc.kids.len() + take)?;
        for item in anc.kids.iter() {
            kept_kids.try_push(copy_for_edit(item, budget)?)?;
        }
        for &(child_ref, _, _) in &extra_children[..take] {
            kept_kids.try_push(Value::Reference(child_ref))?;
        }
        let anc_kept_high = if take > 0 {
            extra_children[take - 1].2
        } else {
            kept_last_high
        };
        let remaining = &extra_children[take..];

        if idx > 0 {
            let anc_ref = anc.object.ok_or_else(|| {
                Error::malformed("a non-root parent-tree intermediate node must be indirect")
            })?;
            write_kids_on_node(
                transaction,
                &mut anc.dict,
                anc.kids_array_object,
                Array::from(kept_kids.into_vec()),
                budget,
            )?;
            insert_limits(
                &mut anc.dict,
                Value::Integer(anc_low),
                Value::Integer(anc_kept_high),
            );
            transaction.write(anc_ref.number(), &Value::Dict(anc.dict), budget)?;

            let mut next_extra = BudgetVec::new(budget);
            next_extra.try_reserve_exact(remaining.len().div_ceil(MAX_LEAF_PAIRS))?;
            for chunk in remaining.chunks(MAX_LEAF_PAIRS) {
                budget.poll()?;
                next_extra.try_push(allocate_intermediate(transaction, chunk, budget)?)?;
            }
            extra_children = next_extra;
            kept_last_high = anc_kept_high;
        } else {
            // Root splits homogeneously: wrap kept kids in a new intermediate node,
            // allocate sibling intermediates for `remaining`, and build hierarchy.
            let mut left_mid = Dict::new();
            #[expect(
                clippy::disallowed_methods,
                reason = "new intermediate node holds fixed /Kids and /Limits entries"
            )]
            left_mid.insert(
                Name::from("Kids"),
                Value::Array(Array::from(kept_kids.into_vec())),
            );
            insert_limits(
                &mut left_mid,
                Value::Integer(anc_low),
                Value::Integer(anc_kept_high),
            );
            let left_mid_ref = transaction.allocate()?;
            transaction.write(left_mid_ref.number(), &Value::Dict(left_mid), budget)?;

            let mut level = BudgetVec::new(budget);
            level.try_reserve_exact(1 + remaining.len().div_ceil(MAX_LEAF_PAIRS))?;
            level.try_push((left_mid_ref, anc_low, anc_kept_high))?;
            for chunk in remaining.chunks(MAX_LEAF_PAIRS) {
                budget.poll()?;
                level.try_push(allocate_intermediate(transaction, chunk, budget)?)?;
            }
            let top_kids = build_hierarchy(transaction, level, Value::Integer, budget)?;
            anc.dict.remove("Nums");
            #[expect(
                clippy::disallowed_methods,
                reason = "promoted parent-tree root holds the fixed /Kids entry"
            )]
            anc.dict.insert(Name::from("Kids"), Value::Array(top_kids));
            if anc.dict.get("Limits").is_some() {
                insert_limits(
                    &mut anc.dict,
                    Value::Integer(anc_low),
                    Value::Integer(last_added_key),
                );
            }
            write_tree_root(transaction, root_dict, anc.object, anc.dict, budget)?;
            extra_children.clear();
        }
    }
    Ok(())
}

/// Appends one `owners` entry onto `Catalog["StructTreeRoot"]`'s `/ParentTree`
/// at the next available integer key (`>= ParentTreeNextKey` and strictly
/// greater than the rightmost leaf's keys), updates `/ParentTreeNextKey`, and
/// returns the assigned key.
pub fn append_parent_tree_entry(
    transaction: &mut Transaction,
    owners: Value,
    budget: &Budget,
) -> Result<i64> {
    let (catalog_ref, mut catalog_dict) = catalog_of(transaction.version(), budget)?;
    let (root_object, mut root_dict) = match located_in(
        transaction.version(),
        catalog_dict.get("StructTreeRoot"),
        budget,
    )? {
        Some((Value::Dict(dict), object)) => {
            editable(&dict)?;
            (object, dict)
        }
        _ => {
            return Err(Error::malformed(
                "the structure tree root is not a dictionary",
            ));
        }
    };

    let mut key = match resolve_in(
        transaction.version(),
        root_dict.get("ParentTreeNextKey"),
        budget,
    )? {
        Some(Value::Integer(next)) if next >= 0 => next,
        _ => 0,
    };

    let (tree_object, tree_dict) =
        match located_in(transaction.version(), root_dict.get("ParentTree"), budget)? {
            Some((Value::Dict(dict), object)) => {
                editable(&dict)?;
                if root_object.is_some() && root_object == object {
                    return Err(Error::malformed("a structure object has conflicting roles"));
                }
                (object, dict)
            }
            _ => {
                let tree_ref = transaction.allocate()?;
                let mut empty = Dict::new();
                #[expect(
                    clippy::disallowed_methods,
                    reason = "new parent tree holds the fixed /Nums entry"
                )]
                empty.insert(Name::from("Nums"), Value::Array(Array::new()));
                #[expect(
                    clippy::disallowed_methods,
                    reason = "sets the fixed /ParentTree entry on StructTreeRoot"
                )]
                root_dict.insert(Name::from("ParentTree"), Value::Reference(tree_ref));
                (Some(tree_ref), empty)
            }
        };

    let (spine, leaf) = walk_right_spine(transaction, tree_object, tree_dict, budget)?;
    let even_len = leaf.nums.len() & !1;
    if even_len >= 2
        && let Some(Value::Integer(last_k)) = resolve_in(
            transaction.version(),
            leaf.nums.as_slice().get(even_len - 2),
            budget,
        )?
    {
        key = key.max(last_k.saturating_add(1));
    }
    if let Some((_, high)) = limits_bounds(transaction.version(), &leaf.dict, budget)? {
        key = key.max(high.saturating_add(1));
    }
    for anc in spine.iter() {
        if let Some((_, high)) = limits_bounds(transaction.version(), &anc.dict, budget)? {
            key = key.max(high.saturating_add(1));
        }
    }
    let next_key = key
        .checked_add(1)
        .filter(|&next| next <= i64::from(i32::MAX))
        .ok_or_else(|| LimitExceeded::new(LimitCode::StructureTreeCount))?;

    let added = [(key, owners)];
    append_on_spine(transaction, &mut root_dict, spine, leaf, &added, budget)?;
    #[expect(
        clippy::disallowed_methods,
        reason = "sets the fixed /ParentTreeNextKey entry on StructTreeRoot"
    )]
    root_dict.insert(Name::from("ParentTreeNextKey"), Value::Integer(next_key));

    match root_object {
        Some(reference) => {
            transaction.write(reference.number(), &Value::Dict(root_dict), budget)?;
        }
        None => {
            #[expect(
                clippy::disallowed_methods,
                reason = "sets the fixed /StructTreeRoot entry on Catalog"
            )]
            catalog_dict.insert(Name::from("StructTreeRoot"), Value::Dict(root_dict));
            transaction.write(catalog_ref.number(), &Value::Dict(catalog_dict), budget)?;
        }
    }
    Ok(key)
}
