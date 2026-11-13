//! Canonical name-tree insert, remove, and replace helpers (ISO 32000-2
//! 7.9.6).

use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::refs::{self, copy_for_edit};
use crate::value::{Array, Dict, Name, PdfString, Reference, Value};
use crate::version::table::Table;
use crate::version::{Transaction, Version};
use crate::{Error, Result};

use super::{
    MAX_LEAF_PAIRS, MAX_TREE_DEPTH, build_hierarchy, catalog_of, collect_tree_nodes, editable,
    insert_limits, located_in,
};

struct NameAncestor<'a> {
    object: Option<Reference>,
    dict: Dict,
    kids: BudgetVec<'a, (Reference, PdfString, PdfString)>,
    chosen: usize,
}

fn string_limits(dict: &Dict) -> Option<(PdfString, PdfString)> {
    let Some(Value::Array(limits)) = dict.get("Limits") else {
        return None;
    };
    let [Value::String(low), Value::String(high)] = limits.as_slice() else {
        return None;
    };
    if low.as_bytes() > high.as_bytes() {
        return None;
    }
    Some((low.clone(), high.clone()))
}

/// Validates that `names` is an even-length array of `1..=MAX_LEAF_PAIRS`
/// `(PdfString, Value)` pairs in strictly ascending byte order, optionally
/// checking `Limits` when `require_limits` is true.
fn canonical_leaf_names(dict: &Dict, require_limits: bool) -> Option<&Array> {
    if dict.get("Kids").is_some() {
        return None;
    }
    if !require_limits && dict.get("Limits").is_some() {
        return None;
    }
    let Some(Value::Array(names)) = dict.get("Names") else {
        return None;
    };
    let slice = names.as_slice();
    if slice.len() % 2 != 0 {
        return None;
    }
    let pairs = slice.len() / 2;
    if pairs > MAX_LEAF_PAIRS || (require_limits && pairs == 0) {
        return None;
    }
    let mut prev: Option<&[u8]> = None;
    for [first_val, _] in slice.as_chunks::<2>().0 {
        let Value::String(key) = first_val else {
            return None;
        };
        if let Some(prev) = prev
            && prev >= key.as_bytes()
        {
            return None;
        }
        prev = Some(key.as_bytes());
    }
    if require_limits {
        let (low, high) = string_limits(dict)?;
        let Value::String(first) = &slice[0] else {
            return None;
        };
        let Value::String(last) = &slice[slice.len() - 2] else {
            return None;
        };
        if low.as_bytes() != first.as_bytes() || high.as_bytes() != last.as_bytes() {
            return None;
        }
    }
    Some(names)
}

fn read_canonical_kids<'b>(
    version: &Version,
    dict: &Dict,
    require_limits: bool,
    visited: &mut Table<()>,
    budget: &'b Budget,
) -> Result<Option<BudgetVec<'b, (Reference, PdfString, PdfString)>>> {
    if dict.get("Names").is_some() {
        return Ok(None);
    }
    if !require_limits && dict.get("Limits").is_some() {
        return Ok(None);
    }
    let Some(Value::Array(kids)) = dict.get("Kids") else {
        return Ok(None);
    };
    if kids.is_empty() || kids.len() > MAX_LEAF_PAIRS {
        return Ok(None);
    }
    let mut out = BudgetVec::new(budget);
    out.try_reserve_exact(kids.len())?;
    let mut prev_high: Option<Vec<u8>> = None;
    for item in kids.iter() {
        budget.poll()?;
        let Value::Reference(child_ref) = item else {
            return Ok(None);
        };
        if visited.insert(child_ref.number(), (), budget)?.is_some() {
            return Ok(None);
        }
        let Some(object) = version
            .get(child_ref.number(), budget)?
            .filter(|object| object.generation() == child_ref.generation())
        else {
            return Ok(None);
        };
        let Value::Dict(child_dict) = &**object.value() else {
            return Ok(None);
        };
        if editable(child_dict).is_err() {
            return Ok(None);
        }
        let Some((low, high)) = string_limits(child_dict) else {
            return Ok(None);
        };
        if let Some(prev) = &prev_high
            && prev.as_slice() >= low.as_bytes()
        {
            return Ok(None);
        }
        let mut high_copy = BudgetVec::new(budget);
        high_copy.try_extend_from_slice(high.as_bytes())?;
        prev_high = Some(high_copy.into_vec());
        out.try_push((*child_ref, low, high))?;
    }
    if require_limits {
        let Some((low, high)) = string_limits(dict) else {
            return Ok(None);
        };
        if low.as_bytes() != out[0].1.as_bytes()
            || high.as_bytes() != out[out.len() - 1].2.as_bytes()
        {
            return Ok(None);
        }
    }
    Ok(Some(out))
}

fn write_name_leaf(
    transaction: &mut Transaction,
    items: &[Value],
    target: Option<(Reference, Dict)>,
    budget: &Budget,
) -> Result<(Reference, PdfString, PdfString)> {
    let Value::String(low) = &items[0] else {
        return Err(Error::malformed("a name-tree leaf key is not a string"));
    };
    let Value::String(high) = &items[items.len() - 2] else {
        return Err(Error::malformed("a name-tree leaf key is not a string"));
    };
    let low = low.clone();
    let high = high.clone();
    let mut copied = BudgetVec::new(budget);
    copied.try_reserve_exact(items.len())?;
    for item in items {
        copied.try_push(copy_for_edit(item, budget)?)?;
    }
    let (leaf_ref, mut dict) = match target {
        Some((reference, mut dict)) => {
            dict.remove("Kids");
            (reference, dict)
        }
        None => (transaction.allocate()?, Dict::new()),
    };
    #[expect(
        clippy::disallowed_methods,
        reason = "leaf node holds fixed /Names and /Limits entries"
    )]
    dict.insert(
        Name::from("Names"),
        Value::Array(Array::from(copied.into_vec())),
    );
    insert_limits(
        &mut dict,
        Value::String(low.clone()),
        Value::String(high.clone()),
    );
    transaction.write(leaf_ref.number(), &Value::Dict(dict), budget)?;
    Ok((leaf_ref, low, high))
}

fn write_name_intermediate(
    transaction: &mut Transaction,
    entries: &[(Reference, PdfString, PdfString)],
    target: Option<(Reference, Dict)>,
    budget: &Budget,
) -> Result<(Reference, PdfString, PdfString)> {
    let low = entries[0].1.clone();
    let high = entries[entries.len() - 1].2.clone();
    let mut kids = BudgetVec::new(budget);
    kids.try_reserve_exact(entries.len())?;
    for &(reference, _, _) in entries {
        kids.try_push(Value::Reference(reference))?;
    }
    let (mid_ref, mut dict) = match target {
        Some((reference, mut dict)) => {
            dict.remove("Names");
            (reference, dict)
        }
        None => (transaction.allocate()?, Dict::new()),
    };
    #[expect(
        clippy::disallowed_methods,
        reason = "intermediate node holds fixed /Kids and /Limits entries"
    )]
    dict.insert(
        Name::from("Kids"),
        Value::Array(Array::from(kids.into_vec())),
    );
    insert_limits(
        &mut dict,
        Value::String(low.clone()),
        Value::String(high.clone()),
    );
    transaction.write(mid_ref.number(), &Value::Dict(dict), budget)?;
    Ok((mid_ref, low, high))
}

/// Inserts `(key, value)` in place into a canonical bounded name tree rooted at
/// `root_dict`. Returns `Ok(true)` when `root_dict` was canonical (and did not
/// already contain `key`) and was updated; returns `Ok(false)` without writing
/// to `transaction` when the tree is non-canonical or `key` is already present.
pub fn insert_name_tree(
    transaction: &mut Transaction,
    root_dict: &mut Dict,
    key: PdfString,
    value: Value,
    budget: &Budget,
) -> Result<bool> {
    editable(root_dict)?;
    if let Some(names) = canonical_leaf_names(root_dict, false) {
        let slice = names.as_slice();
        let pairs = slice.len() / 2;
        let mut pos = 0_usize;
        while pos < pairs {
            let Value::String(existing) = &slice[2 * pos] else {
                return Ok(false);
            };
            match existing.as_bytes().cmp(key.as_bytes()) {
                std::cmp::Ordering::Less => pos += 1,
                std::cmp::Ordering::Equal => return Ok(false),
                std::cmp::Ordering::Greater => break,
            }
        }
        let mut updated = BudgetVec::new(budget);
        updated.try_reserve_exact(slice.len() + 2)?;
        for item in &slice[..2 * pos] {
            updated.try_push(copy_for_edit(item, budget)?)?;
        }
        updated.try_push(Value::String(key))?;
        updated.try_push(value)?;
        for item in &slice[2 * pos..] {
            updated.try_push(copy_for_edit(item, budget)?)?;
        }
        if pairs < MAX_LEAF_PAIRS {
            #[expect(
                clippy::disallowed_methods,
                reason = "single-node root holds the fixed /Names entry"
            )]
            root_dict.insert(
                Name::from("Names"),
                Value::Array(Array::from(updated.into_vec())),
            );
            return Ok(true);
        }
        let mid_pair = pairs.div_ceil(2);
        let split_at = 2 * mid_pair;
        let (left_ref, _, _) = write_name_leaf(transaction, &updated[..split_at], None, budget)?;
        let (right_ref, _, _) = write_name_leaf(transaction, &updated[split_at..], None, budget)?;
        root_dict.remove("Names");
        root_dict.remove("Limits");
        #[expect(
            clippy::disallowed_methods,
            reason = "promoted name-tree root holds the fixed /Kids entry"
        )]
        root_dict.insert(
            Name::from("Kids"),
            Value::Array(Array::from(vec![
                Value::Reference(left_ref),
                Value::Reference(right_ref),
            ])),
        );
        return Ok(true);
    }

    let mut visited: Table<()> = Table::default();
    let mut ancestors: BudgetVec<'_, NameAncestor<'_>> = BudgetVec::new(budget);
    let mut cur_object: Option<Reference> = None;
    let mut cur_dict = copy_for_edit(&Value::Dict(root_dict.clone()), budget)?;
    let Value::Dict(mut cur_node) = cur_dict else {
        return Ok(false);
    };

    let (leaf_ref, leaf_dict) = loop {
        budget.poll()?;
        let require_limits = cur_object.is_some();
        if cur_object.is_some() && canonical_leaf_names(&cur_node, true).is_some() {
            let Some(leaf_ref) = cur_object else {
                return Ok(false);
            };
            break (leaf_ref, cur_node);
        }
        if ancestors.len() >= MAX_TREE_DEPTH {
            return Ok(false);
        }
        let Some(kids) = read_canonical_kids(
            transaction.version(),
            &cur_node,
            require_limits,
            &mut visited,
            budget,
        )?
        else {
            return Ok(false);
        };
        let chosen = kids
            .iter()
            .position(|(_, _, high)| key.as_bytes() <= high.as_bytes())
            .unwrap_or(kids.len() - 1);
        let child_ref = kids[chosen].0;
        let Some(child_obj) = transaction
            .version()
            .get(child_ref.number(), budget)?
            .filter(|obj| obj.generation() == child_ref.generation())
        else {
            return Ok(false);
        };
        let Value::Dict(next_dict) = copy_for_edit(child_obj.value(), budget)? else {
            return Ok(false);
        };
        ancestors.try_push(NameAncestor {
            object: cur_object,
            dict: cur_node,
            kids,
            chosen,
        })?;
        cur_object = Some(child_ref);
        cur_dict = Value::Dict(next_dict);
        let Value::Dict(unpacked) = cur_dict else {
            return Ok(false);
        };
        cur_node = unpacked;
    };

    let Some(names) = canonical_leaf_names(&leaf_dict, true) else {
        return Ok(false);
    };
    let slice = names.as_slice();
    let pairs = slice.len() / 2;
    let mut pos = 0_usize;
    while pos < pairs {
        let Value::String(existing) = &slice[2 * pos] else {
            return Ok(false);
        };
        match existing.as_bytes().cmp(key.as_bytes()) {
            std::cmp::Ordering::Less => pos += 1,
            std::cmp::Ordering::Equal => return Ok(false),
            std::cmp::Ordering::Greater => break,
        }
    }

    let mut updated = BudgetVec::new(budget);
    updated.try_reserve_exact(slice.len() + 2)?;
    for item in &slice[..2 * pos] {
        updated.try_push(copy_for_edit(item, budget)?)?;
    }
    updated.try_push(Value::String(key))?;
    updated.try_push(value)?;
    for item in &slice[2 * pos..] {
        updated.try_push(copy_for_edit(item, budget)?)?;
    }

    let mut updated_left: (Reference, PdfString, PdfString);
    let mut split_right: Option<(Reference, PdfString, PdfString)>;
    if pairs < MAX_LEAF_PAIRS {
        updated_left = write_name_leaf(transaction, &updated, Some((leaf_ref, leaf_dict)), budget)?;
        split_right = None;
    } else {
        let mid_pair = pairs.div_ceil(2);
        let split_at = 2 * mid_pair;
        updated_left = write_name_leaf(
            transaction,
            &updated[..split_at],
            Some((leaf_ref, leaf_dict)),
            budget,
        )?;
        split_right = Some(write_name_leaf(
            transaction,
            &updated[split_at..],
            None,
            budget,
        )?);
    }

    for (idx, anc) in ancestors.into_vec().into_iter().enumerate().rev() {
        budget.poll()?;
        let mut combined = BudgetVec::new(budget);
        combined.try_reserve_exact(anc.kids.len() + usize::from(split_right.is_some()))?;
        for (at, entry) in anc.kids.into_vec().into_iter().enumerate() {
            if at == anc.chosen {
                combined.try_push(updated_left.clone())?;
                if let Some(right) = split_right.take() {
                    combined.try_push(right)?;
                }
            } else {
                combined.try_push(entry)?;
            }
        }

        if combined.len() <= MAX_LEAF_PAIRS {
            if idx == 0 {
                let mut root_kids = BudgetVec::new(budget);
                root_kids.try_reserve_exact(combined.len())?;
                for (r, _, _) in combined.into_vec() {
                    root_kids.try_push(Value::Reference(r))?;
                }
                root_dict.remove("Names");
                root_dict.remove("Limits");
                #[expect(
                    clippy::disallowed_methods,
                    reason = "multi-node root holds the fixed /Kids entry"
                )]
                root_dict.insert(
                    Name::from("Kids"),
                    Value::Array(Array::from(root_kids.into_vec())),
                );
            } else if let Some(anc_ref) = anc.object {
                updated_left = write_name_intermediate(
                    transaction,
                    &combined,
                    Some((anc_ref, anc.dict)),
                    budget,
                )?;
                split_right = None;
            }
        } else if idx > 0 {
            let Some(anc_ref) = anc.object else {
                return Ok(false);
            };
            let mid = combined.len() / 2;
            updated_left = write_name_intermediate(
                transaction,
                &combined[..mid],
                Some((anc_ref, anc.dict)),
                budget,
            )?;
            split_right = Some(write_name_intermediate(
                transaction,
                &combined[mid..],
                None,
                budget,
            )?);
        } else {
            let mid = combined.len() / 2;
            let (left_mid, _, _) =
                write_name_intermediate(transaction, &combined[..mid], None, budget)?;
            let (right_mid, _, _) =
                write_name_intermediate(transaction, &combined[mid..], None, budget)?;
            root_dict.remove("Names");
            root_dict.remove("Limits");
            #[expect(
                clippy::disallowed_methods,
                reason = "promoted multi-level root holds the fixed /Kids entry"
            )]
            root_dict.insert(
                Name::from("Kids"),
                Value::Array(Array::from(vec![
                    Value::Reference(left_mid),
                    Value::Reference(right_mid),
                ])),
            );
        }
    }
    Ok(true)
}

/// Removes `key` in place from a canonical bounded name tree rooted at
/// `root_dict`.
///
/// Returns `Ok(None)` without writing to `transaction` if the tree is
/// non-canonical or `key` is absent; `Ok(Some(true))` if `key` was removed and
/// entries remain; or `Ok(Some(false))` if removing `key` emptied the tree.
/// Any emptied or collapsed indirect `/Kids` nodes are appended to `retired`.
pub fn remove_name_tree<'b>(
    transaction: &mut Transaction,
    root_dict: &mut Dict,
    key: &[u8],
    retired: &mut BudgetVec<'b, u32>,
    budget: &'b Budget,
) -> Result<Option<bool>> {
    editable(root_dict)?;
    if let Some(names) = canonical_leaf_names(root_dict, false) {
        let slice = names.as_slice();
        let pairs = slice.len() / 2;
        let mut found = None;
        for pos in 0..pairs {
            let Value::String(existing) = &slice[2 * pos] else {
                return Ok(None);
            };
            if existing.as_bytes() == key {
                found = Some(pos);
                break;
            }
        }
        let Some(pos) = found else {
            return Ok(None);
        };
        if pairs == 1 {
            root_dict.remove("Names");
            root_dict.remove("Kids");
            root_dict.remove("Limits");
            return Ok(Some(false));
        }
        let mut updated = BudgetVec::new(budget);
        updated.try_reserve_exact(slice.len() - 2)?;
        for item in &slice[..2 * pos] {
            updated.try_push(copy_for_edit(item, budget)?)?;
        }
        for item in &slice[2 * (pos + 1)..] {
            updated.try_push(copy_for_edit(item, budget)?)?;
        }
        #[expect(
            clippy::disallowed_methods,
            reason = "single-node root holds the fixed /Names entry"
        )]
        root_dict.insert(
            Name::from("Names"),
            Value::Array(Array::from(updated.into_vec())),
        );
        return Ok(Some(true));
    }

    let mut visited: Table<()> = Table::default();
    let mut ancestors: BudgetVec<'_, NameAncestor<'_>> = BudgetVec::new(budget);
    let mut cur_object: Option<Reference> = None;
    let Value::Dict(mut cur_node) = copy_for_edit(&Value::Dict(root_dict.clone()), budget)? else {
        return Ok(None);
    };

    let (leaf_ref, leaf_dict) = loop {
        budget.poll()?;
        let require_limits = cur_object.is_some();
        if cur_object.is_some() && canonical_leaf_names(&cur_node, true).is_some() {
            let Some(leaf_ref) = cur_object else {
                return Ok(None);
            };
            break (leaf_ref, cur_node);
        }
        if ancestors.len() >= MAX_TREE_DEPTH {
            return Ok(None);
        }
        let Some(kids) = read_canonical_kids(
            transaction.version(),
            &cur_node,
            require_limits,
            &mut visited,
            budget,
        )?
        else {
            return Ok(None);
        };
        let Some(chosen) = kids
            .iter()
            .position(|(_, low, high)| low.as_bytes() <= key && key <= high.as_bytes())
        else {
            return Ok(None);
        };
        let child_ref = kids[chosen].0;
        let Some(child_obj) = transaction
            .version()
            .get(child_ref.number(), budget)?
            .filter(|obj| obj.generation() == child_ref.generation())
        else {
            return Ok(None);
        };
        let Value::Dict(next_dict) = copy_for_edit(child_obj.value(), budget)? else {
            return Ok(None);
        };
        ancestors.try_push(NameAncestor {
            object: cur_object,
            dict: cur_node,
            kids,
            chosen,
        })?;
        cur_object = Some(child_ref);
        cur_node = next_dict;
    };

    let Some(names) = canonical_leaf_names(&leaf_dict, true) else {
        return Ok(None);
    };
    let slice = names.as_slice();
    let pairs = slice.len() / 2;
    let mut found = None;
    for pos in 0..pairs {
        let Value::String(existing) = &slice[2 * pos] else {
            return Ok(None);
        };
        if existing.as_bytes() == key {
            found = Some(pos);
            break;
        }
    }
    let Some(pos) = found else {
        return Ok(None);
    };

    let mut replacement: Option<(Reference, PdfString, PdfString)> = if pairs == 1 {
        retired.try_push(leaf_ref.number())?;
        None
    } else {
        let mut updated = BudgetVec::new(budget);
        updated.try_reserve_exact(slice.len() - 2)?;
        for item in &slice[..2 * pos] {
            updated.try_push(copy_for_edit(item, budget)?)?;
        }
        for item in &slice[2 * (pos + 1)..] {
            updated.try_push(copy_for_edit(item, budget)?)?;
        }
        Some(write_name_leaf(
            transaction,
            &updated,
            Some((leaf_ref, leaf_dict)),
            budget,
        )?)
    };

    for (idx, anc) in ancestors.into_vec().into_iter().enumerate().rev() {
        budget.poll()?;
        let mut combined = BudgetVec::new(budget);
        combined.try_reserve_exact(anc.kids.len())?;
        for (at, entry) in anc.kids.into_vec().into_iter().enumerate() {
            if at == anc.chosen {
                if let Some(updated) = replacement.take() {
                    combined.try_push(updated)?;
                }
            } else {
                combined.try_push(entry)?;
            }
        }

        if idx > 0 {
            let Some(anc_ref) = anc.object else {
                return Ok(None);
            };
            if combined.is_empty() {
                retired.try_push(anc_ref.number())?;
                replacement = None;
            } else {
                replacement = Some(write_name_intermediate(
                    transaction,
                    &combined,
                    Some((anc_ref, anc.dict)),
                    budget,
                )?);
            }
        } else {
            if combined.is_empty() {
                root_dict.remove("Names");
                root_dict.remove("Kids");
                root_dict.remove("Limits");
                return Ok(Some(false));
            }
            if combined.len() == 1 {
                let sole_ref = combined[0].0;
                let snap = transaction.snapshot();
                if let Some(sole_obj) = snap
                    .get(sole_ref.number(), budget)?
                    .filter(|obj| obj.generation() == sole_ref.generation())
                    && let Value::Dict(sole_dict) = copy_for_edit(sole_obj.value(), budget)?
                    && editable(&sole_dict).is_ok()
                {
                    root_dict.remove("Names");
                    root_dict.remove("Kids");
                    root_dict.remove("Limits");
                    if let Some(sole_names) = sole_dict.get("Names") {
                        #[expect(
                            clippy::disallowed_methods,
                            reason = "collapsed single-node root holds the fixed /Names entry"
                        )]
                        root_dict.insert(Name::from("Names"), copy_for_edit(sole_names, budget)?);
                        retired.try_push(sole_ref.number())?;
                        return Ok(Some(true));
                    }
                    if let Some(sole_kids) = sole_dict.get("Kids") {
                        #[expect(
                            clippy::disallowed_methods,
                            reason = "collapsed multi-node root holds the fixed /Kids entry"
                        )]
                        root_dict.insert(Name::from("Kids"), copy_for_edit(sole_kids, budget)?);
                        retired.try_push(sole_ref.number())?;
                        return Ok(Some(true));
                    }
                }
            }
            let mut root_kids = BudgetVec::new(budget);
            root_kids.try_reserve_exact(combined.len())?;
            for (r, _, _) in combined.into_vec() {
                root_kids.try_push(Value::Reference(r))?;
            }
            root_dict.remove("Names");
            root_dict.remove("Limits");
            #[expect(
                clippy::disallowed_methods,
                reason = "multi-node root holds the fixed /Kids entry"
            )]
            root_dict.insert(
                Name::from("Kids"),
                Value::Array(Array::from(root_kids.into_vec())),
            );
            return Ok(Some(true));
        }
    }
    Ok(Some(true))
}

fn populate_name_root(
    transaction: &mut Transaction,
    root_dict: &mut Dict,
    mut pairs: BudgetVec<'_, (PdfString, Value)>,
    budget: &Budget,
) -> Result<()> {
    budget.charge(
        LimitCode::MemorySize,
        pairs.len().saturating_mul(size_of::<(PdfString, Value)>()) as u64,
    )?;
    pairs.sort_by(|(left, _), (right, _)| left.as_bytes().cmp(right.as_bytes()));
    let mut unique = BudgetVec::new(budget);
    unique.try_reserve_exact(pairs.len())?;
    for (key, value) in pairs.into_vec() {
        budget.poll()?;
        if unique
            .last()
            .is_none_or(|(prev, _): &(PdfString, Value)| prev.as_bytes() != key.as_bytes())
        {
            unique.try_push((key, value))?;
        }
    }

    root_dict.remove("Names");
    root_dict.remove("Kids");
    root_dict.remove("Limits");

    if unique.len() <= MAX_LEAF_PAIRS {
        let mut names = BudgetVec::new(budget);
        names.try_reserve_exact(2 * unique.len())?;
        for (key, value) in unique.into_vec() {
            names.try_push(Value::String(key))?;
            names.try_push(value)?;
        }
        #[expect(
            clippy::disallowed_methods,
            reason = "single-node root holds the fixed /Names entry"
        )]
        root_dict.insert(
            Name::from("Names"),
            Value::Array(Array::from(names.into_vec())),
        );
        return Ok(());
    }

    let mut leaves = BudgetVec::new(budget);
    leaves.try_reserve_exact(unique.len().div_ceil(MAX_LEAF_PAIRS))?;
    for chunk in unique.chunks(MAX_LEAF_PAIRS) {
        budget.poll()?;
        let low = chunk[0].0.clone();
        let high = chunk[chunk.len() - 1].0.clone();
        let mut names = BudgetVec::new(budget);
        names.try_reserve_exact(2 * chunk.len())?;
        for (key, value) in chunk {
            names.try_push(Value::String(key.clone()))?;
            names.try_push(copy_for_edit(value, budget)?)?;
        }
        let mut leaf = Dict::new();
        #[expect(
            clippy::disallowed_methods,
            reason = "leaf node holds fixed /Names and /Limits entries"
        )]
        leaf.insert(
            Name::from("Names"),
            Value::Array(Array::from(names.into_vec())),
        );
        insert_limits(
            &mut leaf,
            Value::String(low.clone()),
            Value::String(high.clone()),
        );
        let leaf_ref = transaction.allocate()?;
        transaction.write(leaf_ref.number(), &Value::Dict(leaf), budget)?;
        leaves.try_push((leaf_ref, low, high))?;
    }
    let kids = build_hierarchy(transaction, leaves, Value::String, budget)?;
    #[expect(
        clippy::disallowed_methods,
        reason = "multi-node root holds the fixed /Kids entry"
    )]
    root_dict.insert(Name::from("Kids"), Value::Array(kids));
    Ok(())
}

/// Replaces `holder[key]` with a canonical bounded name tree built from
/// `pairs` (or removes `holder[key]` when `pairs` is empty). Returns `(changed,
/// retired)` where `changed` reports whether `holder` itself was modified and
/// `retired` lists indirect objects of any prior tree to pass to
/// [`refs::free_unreferenced`] after writing `holder`.
pub fn replace_name_tree_entry<'b>(
    transaction: &mut Transaction,
    holder: &mut Dict,
    key: &str,
    pairs: BudgetVec<'_, (PdfString, Value)>,
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
        populate_name_root(transaction, &mut root_dict, pairs, budget)?;
        transaction.write(tree_ref.number(), &Value::Dict(root_dict), budget)?;
        return Ok((false, retired));
    }

    let retired = match holder.get(key) {
        Some(old) => collect_tree_nodes(transaction.version(), old, true, budget)?,
        None => BudgetVec::new(budget),
    };
    let mut root_dict = Dict::new();
    populate_name_root(transaction, &mut root_dict, pairs, budget)?;
    let tree_ref = transaction.allocate()?;
    transaction.write(tree_ref.number(), &Value::Dict(root_dict), budget)?;
    #[expect(
        clippy::disallowed_methods,
        reason = "replaces or adds one named name-tree entry on the holder dictionary"
    )]
    holder.insert(Name::from(key), Value::Reference(tree_ref));
    Ok((true, retired))
}

/// Replaces `Catalog["Names"]["EmbeddedFiles"]` with a canonical bounded name
/// tree built from `pairs` (removing `"EmbeddedFiles"` and an emptied `"Names"`
/// dictionary when `pairs` is empty), and frees unreferenced retired `/Kids`
/// nodes alongside `extra_candidates`.
pub fn replace_catalog_embedded_files(
    cache: &refs::Cache,
    transaction: &mut Transaction,
    pairs: BudgetVec<'_, (PdfString, Value)>,
    extra_candidates: &[u32],
    budget: &Budget,
) -> Result<()> {
    let (catalog_ref, mut catalog_dict) = catalog_of(transaction.version(), budget)?;
    let (names_object, mut names_dict) =
        match located_in(transaction.version(), catalog_dict.get("Names"), budget)? {
            Some((Value::Dict(dict), Some(names_ref)))
                if names_ref != catalog_ref && editable(&dict).is_ok() =>
            {
                (Some(names_ref), dict)
            }
            Some((Value::Dict(dict), _)) => (None, dict),
            _ if pairs.is_empty() => {
                if !extra_candidates.is_empty() {
                    refs::free_unreferenced(cache, transaction, &[], extra_candidates, budget)?;
                }
                return Ok(());
            }
            _ => (None, Dict::new()),
        };

    let (names_changed, mut retired) = replace_name_tree_entry(
        transaction,
        &mut names_dict,
        "EmbeddedFiles",
        pairs,
        names_object.or(Some(catalog_ref)),
        budget,
    )?;

    if names_dict.is_empty() {
        if let Some(names_ref) = names_object {
            retired.try_push(names_ref.number())?;
        }
        if catalog_dict.remove("Names").is_some() {
            transaction.write(catalog_ref.number(), &Value::Dict(catalog_dict), budget)?;
        }
    } else if let Some(names_ref) = names_object {
        if names_changed {
            transaction.write(names_ref.number(), &Value::Dict(names_dict), budget)?;
        }
    } else {
        #[expect(
            clippy::disallowed_methods,
            reason = "sets the fixed /Names entry on the catalog dictionary"
        )]
        catalog_dict.insert(Name::from("Names"), Value::Dict(names_dict));
        transaction.write(catalog_ref.number(), &Value::Dict(catalog_dict), budget)?;
    }

    retired.try_extend_from_slice(extra_candidates)?;
    refs::free_unreferenced(cache, transaction, &[], &retired, budget)?;
    Ok(())
}

/// Inserts `(key, value)` into `Catalog["Names"]["EmbeddedFiles"]` in place
/// when the existing tree is canonical, falling back to
/// [`replace_catalog_embedded_files`] with `fallback_pairs` when `/EmbeddedFiles`
/// is absent or non-canonical.
pub fn insert_catalog_embedded_files(
    cache: &refs::Cache,
    transaction: &mut Transaction,
    key: PdfString,
    value: Value,
    fallback_pairs: BudgetVec<'_, (PdfString, Value)>,
    budget: &Budget,
) -> Result<()> {
    let (catalog_ref, mut catalog_dict) = catalog_of(transaction.version(), budget)?;
    if let Some((Value::Dict(mut names_dict), names_object)) =
        located_in(transaction.version(), catalog_dict.get("Names"), budget)?
        && editable(&names_dict).is_ok()
        && let Some((Value::Dict(mut ef_dict), ef_object)) = located_in(
            transaction.version(),
            names_dict.get("EmbeddedFiles"),
            budget,
        )?
        && editable(&ef_dict).is_ok()
        && ef_object != Some(catalog_ref)
        && (names_object.is_none() || ef_object != names_object)
        && insert_name_tree(transaction, &mut ef_dict, key, value, budget)?
    {
        if let Some(ef_ref) = ef_object {
            transaction.write(ef_ref.number(), &Value::Dict(ef_dict), budget)?;
        } else {
            #[expect(
                clippy::disallowed_methods,
                reason = "sets the fixed /EmbeddedFiles entry on the catalog /Names dictionary"
            )]
            names_dict.insert(Name::from("EmbeddedFiles"), Value::Dict(ef_dict));
            if let Some(names_ref) = names_object.filter(|&r| r != catalog_ref) {
                transaction.write(names_ref.number(), &Value::Dict(names_dict), budget)?;
            } else {
                #[expect(
                    clippy::disallowed_methods,
                    reason = "sets the fixed /Names entry on the catalog dictionary"
                )]
                catalog_dict.insert(Name::from("Names"), Value::Dict(names_dict));
                transaction.write(catalog_ref.number(), &Value::Dict(catalog_dict), budget)?;
            }
        }
        return Ok(());
    }
    replace_catalog_embedded_files(cache, transaction, fallback_pairs, &[], budget)
}

/// Removes `keys_to_remove` from `Catalog["Names"]["EmbeddedFiles"]` in place
/// when a single key is removed from a canonical tree, falling back to
/// [`replace_catalog_embedded_files`] with `fallback_pairs` otherwise, and
/// frees unreferenced retired `/Kids` nodes alongside `extra_candidates`.
pub fn remove_catalog_embedded_files(
    cache: &refs::Cache,
    transaction: &mut Transaction,
    keys_to_remove: &[PdfString],
    fallback_pairs: BudgetVec<'_, (PdfString, Value)>,
    extra_candidates: &[u32],
    budget: &Budget,
) -> Result<()> {
    if keys_to_remove.len() == 1 {
        let (catalog_ref, mut catalog_dict) = catalog_of(transaction.version(), budget)?;
        if let Some((Value::Dict(mut names_dict), names_object)) =
            located_in(transaction.version(), catalog_dict.get("Names"), budget)?
            && editable(&names_dict).is_ok()
            && let Some((Value::Dict(mut ef_dict), ef_object)) = located_in(
                transaction.version(),
                names_dict.get("EmbeddedFiles"),
                budget,
            )?
            && editable(&ef_dict).is_ok()
            && ef_object != Some(catalog_ref)
            && (names_object.is_none() || ef_object != names_object)
        {
            let mut retired = BudgetVec::new(budget);
            if let Some(non_empty) = remove_name_tree(
                transaction,
                &mut ef_dict,
                keys_to_remove[0].as_bytes(),
                &mut retired,
                budget,
            )? {
                if non_empty {
                    if let Some(ef_ref) = ef_object {
                        transaction.write(ef_ref.number(), &Value::Dict(ef_dict), budget)?;
                    } else {
                        #[expect(
                            clippy::disallowed_methods,
                            reason = "sets the fixed /EmbeddedFiles entry on the catalog /Names dictionary"
                        )]
                        names_dict.insert(Name::from("EmbeddedFiles"), Value::Dict(ef_dict));
                        if let Some(names_ref) = names_object.filter(|&r| r != catalog_ref) {
                            transaction.write(
                                names_ref.number(),
                                &Value::Dict(names_dict),
                                budget,
                            )?;
                        } else {
                            #[expect(
                                clippy::disallowed_methods,
                                reason = "sets the fixed /Names entry on the catalog dictionary"
                            )]
                            catalog_dict.insert(Name::from("Names"), Value::Dict(names_dict));
                            transaction.write(
                                catalog_ref.number(),
                                &Value::Dict(catalog_dict),
                                budget,
                            )?;
                        }
                    }
                } else {
                    if let Some(ef_ref) = ef_object {
                        retired.try_push(ef_ref.number())?;
                    }
                    names_dict.remove("EmbeddedFiles");
                    if names_dict.is_empty() {
                        if let Some(names_ref) = names_object.filter(|&r| r != catalog_ref) {
                            retired.try_push(names_ref.number())?;
                        }
                        if catalog_dict.remove("Names").is_some() {
                            transaction.write(
                                catalog_ref.number(),
                                &Value::Dict(catalog_dict),
                                budget,
                            )?;
                        }
                    } else if let Some(names_ref) = names_object.filter(|&r| r != catalog_ref) {
                        transaction.write(names_ref.number(), &Value::Dict(names_dict), budget)?;
                    } else {
                        #[expect(
                            clippy::disallowed_methods,
                            reason = "sets the fixed /Names entry on the catalog dictionary"
                        )]
                        catalog_dict.insert(Name::from("Names"), Value::Dict(names_dict));
                        transaction.write(
                            catalog_ref.number(),
                            &Value::Dict(catalog_dict),
                            budget,
                        )?;
                    }
                }
                retired.try_extend_from_slice(extra_candidates)?;
                refs::free_unreferenced(cache, transaction, &[], &retired, budget)?;
                return Ok(());
            }
        }
    }
    replace_catalog_embedded_files(cache, transaction, fallback_pairs, extra_candidates, budget)
}
