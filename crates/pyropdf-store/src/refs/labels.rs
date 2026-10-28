//! The page labels of pages that a structural edit moves or removes
//! (ISO 32000-2 section 12.4.2): each page that stays keeps its label.
//!
//! A page's label comes from the range of the catalog's `/PageLabels`
//! number tree that starts at or before the page's index. When the pages
//! change, the tree becomes one leaf whose ranges give each page that stays
//! the label it had. A tree that the store cannot read whole, or that
//! labels no first page, stays as it was.

use crate::Result;
use crate::budget::{Budget, BudgetVec};
use crate::memo::Reads;
use crate::value::{Array, Dict, Name, Value};
use crate::version::Transaction;
use crate::version::table::Table;

use super::copy::{copy_dict_for_edit, copy_for_edit};
use super::navigation::{catalog, dict_of, is_aliasing, readable};

/// The most levels of the page label tree that a structural edit rewrites.
const MAX_TREE_DEPTH: usize = 32;

/// The page labels that a new order of pages writes.
pub struct Labels<'b> {
    /// The object that the tree is, or the catalog that holds it.
    number: u32,
    in_catalog: bool,
    tree: Dict,
    nodes: BudgetVec<'b, u32>,
}

impl Labels<'_> {
    /// Returns the objects of the tree before, which the labels that
    /// [`Labels::write`] writes no longer refer to.
    #[must_use]
    pub fn nodes(&self) -> &[u32] {
        &self.nodes
    }

    /// Writes the labels in `transaction`, over the catalog's other
    /// changes.
    pub fn write(&self, transaction: &mut Transaction, budget: &Budget) -> Result<()> {
        let tree = Value::Dict(copy_dict_for_edit(&self.tree, budget)?);
        let value = if self.in_catalog {
            let Some(object) = transaction.version().get(self.number, budget)? else {
                return Ok(());
            };
            let mut catalog = copy_for_edit(object.value(), budget)?;
            let Value::Dict(dict) = &mut catalog else {
                return Ok(());
            };
            #[expect(
                clippy::disallowed_methods,
                reason = "the catalog grows by one entry at most"
            )]
            dict.insert(Name::from("PageLabels"), tree);
            catalog
        } else {
            tree
        };
        transaction.write(self.number, &value, budget)?;
        Ok(())
    }
}

/// One range of labels: the index of its first page and its label
/// dictionary.
struct Range {
    start: usize,
    label: Dict,
    ordinal: usize,
}

/// Returns the page labels that keep the label of each page of `after`, a
/// new order of pages of `before`, the order of the version that `reads`
/// reads; `None` when the labels stay as they are.
pub fn relabel<'b>(
    reads: &mut Reads<'b>,
    before: &[u32],
    after: &[u32],
) -> Result<Option<Labels<'b>>> {
    let budget = reads.budget();
    let Some(catalog) = catalog(reads)? else {
        return Ok(None);
    };
    let Some(object) = readable(reads, catalog)? else {
        return Ok(None);
    };
    let Some(dict) = dict_of(&object) else {
        return Ok(None);
    };
    let Some(tree) = dict.get("PageLabels") else {
        return Ok(None);
    };
    let root = match tree {
        Value::Reference(root) => Some(root.number()),
        _ => None,
    };
    let mut nodes = BudgetVec::new(budget);
    let Some(ranges) = read(reads, tree, root, &mut nodes)? else {
        return Ok(None);
    };
    if ranges.first().is_none_or(|range| range.start != 0) {
        return Ok(None);
    }
    let mut position: Table<usize> = Table::new();
    for (index, &page) in before.iter().enumerate() {
        if position.get(page).is_none() {
            position.insert(page, index, budget)?;
        }
    }
    let mut pairs = BudgetVec::new(budget);
    let mut previous: Option<(usize, usize)> = None;
    for (at, &page) in after.iter().enumerate() {
        budget.poll()?;
        let Some(&old) = position.get(page) else {
            return Ok(None);
        };
        let range = ranges.partition_point(|range| range.start <= old) - 1;
        let follows = previous.is_some_and(|(last, index)| last == range && index + 1 == old);
        previous = Some((range, old));
        if follows {
            continue;
        }
        let Range { start, label, .. } = &ranges[range];
        let Some(first) = i64::try_from(old - start)
            .ok()
            .and_then(|offset| first_number(label).checked_add(offset))
        else {
            return Ok(None);
        };
        pairs.try_push((at, range, first))?;
    }
    let same = pairs.len() == ranges.len()
        && pairs.iter().zip(ranges.iter()).enumerate().all(
            |(position, (&(at, index, first), range))| {
                at == range.start && index == position && first == first_number(&range.label)
            },
        );
    if same {
        return Ok(None);
    }
    let mut nums = BudgetVec::new(budget);
    for (at, index, first) in pairs.into_vec() {
        budget.poll()?;
        let label = labelled(&ranges[index].label, first, budget)?;
        nums.try_push(Value::Integer(i64::try_from(at).unwrap_or(i64::MAX)))?;
        nums.try_push(Value::Dict(label))?;
    }
    budget.charge(
        crate::budget::LimitCode::MemorySize,
        (8 * size_of::<(Name, Value)>()) as u64,
    )?;
    #[expect(
        clippy::disallowed_methods,
        reason = "the tree holds one charged entry, and its array grew through the budget"
    )]
    let tree: Dict = [(
        Name::from("Nums"),
        Value::Array(Array::from(nums.into_vec())),
    )]
    .into_iter()
    .collect();
    Ok(Some(Labels {
        number: root.unwrap_or(catalog),
        in_catalog: root.is_none(),
        tree,
        nodes,
    }))
}

/// Returns the ranges of the page label tree `tree`, ascending, the first
/// of each start alone, and records in `nodes` the objects that it reads
/// but `root`. `None` for a tree deeper than [`MAX_TREE_DEPTH`].
fn read<'b>(
    reads: &mut Reads<'b>,
    tree: &Value,
    root: Option<u32>,
    nodes: &mut BudgetVec<'b, u32>,
) -> Result<Option<BudgetVec<'b, Range>>> {
    let budget = reads.budget();
    let mut ranges = BudgetVec::new(budget);
    let mut seen: Table<()> = Table::new();
    let mut pending = BudgetVec::new(budget);
    pending.try_push((copy_for_edit(tree, budget)?, 1))?;
    while let Some((value, depth)) = pending.pop() {
        budget.poll()?;
        if let Value::Reference(node) = &value {
            if seen.get(node.number()).is_some() {
                continue;
            }
            seen.insert(node.number(), (), budget)?;
        }
        let Some(Value::Dict(node)) = deref(reads, &value, root, nodes)? else {
            return Ok(None);
        };
        if is_aliasing(&node) {
            return Err(crate::Error::malformed(
                "page label tree cannot alias a page or catalog dictionary",
            ));
        }
        if let Some(nums) = node.get("Nums") {
            let Some(Value::Array(nums)) = deref(reads, nums, root, nodes)? else {
                return Ok(None);
            };
            if nums.len() % 2 != 0 {
                return Ok(None);
            }
            let mut items = nums.iter();
            while let (Some(key), Some(label)) = (items.next(), items.next()) {
                budget.poll()?;
                let Some(Value::Integer(start)) = deref(reads, key, root, nodes)? else {
                    return Ok(None);
                };
                let (Ok(start), Some(Value::Dict(label))) =
                    (usize::try_from(start), deref(reads, label, root, nodes)?)
                else {
                    return Ok(None);
                };
                ranges.try_push(Range {
                    start,
                    label,
                    ordinal: ranges.len(),
                })?;
            }
        }
        if let Some(kids) = node.get("Kids") {
            let Some(Value::Array(kids)) = deref(reads, kids, root, nodes)? else {
                return Ok(None);
            };
            for kid in kids.iter() {
                budget.poll()?;
                if let Value::Reference(reference) = kid
                    && seen.get(reference.number()).is_some()
                    && reads.generation(reference.number())? == Some(reference.generation())
                {
                    continue;
                }
                if depth >= MAX_TREE_DEPTH {
                    return Ok(None);
                }
                pending.try_push((copy_for_edit(kid, budget)?, depth + 1))?;
            }
        }
    }
    ranges.sort_unstable_by_key(|range| (range.start, range.ordinal));
    budget.poll()?;
    let mut first = BudgetVec::new(budget);
    for range in ranges.into_vec() {
        if first
            .last()
            .is_none_or(|last: &Range| last.start != range.start)
        {
            first.try_push(range)?;
        }
    }
    Ok(Some(first))
}

/// Returns `value`, or the object that it refers to, which it records in
/// `nodes` unless it is `root`.
fn deref(
    reads: &mut Reads<'_>,
    value: &Value,
    root: Option<u32>,
    nodes: &mut BudgetVec<'_, u32>,
) -> Result<Option<Value>> {
    let Value::Reference(reference) = value else {
        return Ok(Some(copy_for_edit(value, reads.budget())?));
    };
    let number = reference.number();
    let Some(object) =
        readable(reads, number)?.filter(|object| object.generation() == reference.generation())
    else {
        return Ok(None);
    };
    if root != Some(number) {
        nodes.try_push(number)?;
    }
    Ok(Some(copy_for_edit(object.value(), reads.budget())?))
}

/// Returns the number of the first page of a range, its `/St`.
fn first_number(label: &Dict) -> i64 {
    match label.get("St") {
        Some(&Value::Integer(first)) if first >= 1 => first,
        _ => 1,
    }
}

/// Returns `label` with `first` as the number of its first page.
fn labelled(label: &Dict, first: i64, budget: &Budget) -> Result<Dict> {
    let mut label = copy_dict_for_edit(label, budget)?;
    if first == 1 {
        label.remove("St");
    } else {
        #[expect(
            clippy::disallowed_methods,
            reason = "the label grows by one entry at most"
        )]
        label.insert(Name::from("St"), Value::Integer(first));
    }
    Ok(label)
}
