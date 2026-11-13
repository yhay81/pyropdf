//! `/StructTreeRoot` `/ParentTree` and `/ParentTreeNextKey` benchmark and
//! Option A / Option B prototypes (#765).

use std::collections::BTreeMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use pyropdf_store::Result;
use pyropdf_store::budget::Budget;
use pyropdf_store::refs;
use pyropdf_store::settle::{Scope, Settler, Settlers, Structure};
use pyropdf_store::value::{Array, Bytes, Dict, Name, Reference, Stream, Value};
use pyropdf_store::version::{Document, MemoryBase, Transaction, Version};

use crate::{BenchRow, Stats, time_with_undo};

const CATALOG: u32 = 1;
const PAGES: u32 = 2;
const PAGE: u32 = 3;
const STRUCT_ROOT: u32 = 4;
const PARENT_TREE: u32 = 5;
const MAX_LEAF_PAIRS: usize = 64;
const MCIDS_PER_CANVAS: usize = 4;

fn build_tagged_doc(initial_entries: usize, settlers: Settlers) -> Document {
    let trailer: Dict = [
        (
            Name::from("Root"),
            Value::Reference(Reference::new(CATALOG, 0)),
        ),
        (Name::from("Size"), Value::Integer(6)),
    ]
    .into_iter()
    .collect();
    let mut base = MemoryBase::new(trailer);
    base.insert(
        Reference::new(CATALOG, 0),
        Bytes::new(
            &b"<</Type/Catalog/Pages 2 0 R/StructTreeRoot 4 0 R/MarkInfo<</Marked true>>>>"[..],
        ),
    );
    base.insert(
        Reference::new(PAGES, 0),
        Bytes::new(&b"<</Type/Pages/Kids[3 0 R]/Count 1>>"[..]),
    );
    base.insert(
        Reference::new(PAGE, 0),
        Bytes::new(&b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>"[..]),
    );
    base.insert(
        Reference::new(STRUCT_ROOT, 0),
        Bytes::new(&b"<</Type/StructTreeRoot/K[]/ParentTree 5 0 R/ParentTreeNextKey 0>>"[..]),
    );
    base.insert(
        Reference::new(PARENT_TREE, 0),
        Bytes::new(&b"<</Nums[]>>"[..]),
    );

    let doc = Document::new(Arc::new(base), None);
    let budget = Budget::default();
    if initial_entries > 0 {
        let mut tx = doc.begin();
        let mut dummy = Stats::default();
        for _ in 0..initial_entries {
            append_tagged_canvas_option_b(&mut tx, &budget, &mut dummy).unwrap();
        }
        tx.commit(&budget).unwrap();
    }
    doc.set_settlers(settlers);
    doc
}

/// Allocates one Form XObject and `MCIDS_PER_CANVAS` `StructElem`s with MCR
/// dictionaries pointing to `form_ref`, returning `(form_ref, elem_refs)`.
fn write_canvas_elements(
    tx: &mut Transaction,
    budget: &Budget,
    struct_parents_key: Option<i64>,
    stats: &mut Stats,
) -> Result<(Reference, Vec<Reference>)> {
    let form_ref = tx.allocate()?;
    let mut form_dict = Dict::new();
    form_dict.insert(Name::from("Type"), Value::Name(Name::from("XObject")));
    form_dict.insert(Name::from("Subtype"), Value::Name(Name::from("Form")));
    form_dict.insert(
        Name::from("BBox"),
        Value::Array(Array::from(vec![
            Value::Integer(0),
            Value::Integer(0),
            Value::Integer(612),
            Value::Integer(792),
        ])),
    );
    if let Some(key) = struct_parents_key {
        form_dict.insert(Name::from("StructParents"), Value::Integer(key));
    }
    let stream = Stream::new(form_dict, Bytes::new(&b"q Q"[..]));
    stats.writes += 1;
    tx.write(form_ref.number(), &Value::from(stream), budget)?;

    let mut elems = Vec::with_capacity(MCIDS_PER_CANVAS);
    for mcid in 0..MCIDS_PER_CANVAS {
        let elem_ref = tx.allocate()?;
        let mut mcr = Dict::new();
        mcr.insert(Name::from("Type"), Value::Name(Name::from("MCR")));
        mcr.insert(Name::from("Pg"), Value::Reference(Reference::new(PAGE, 0)));
        mcr.insert(Name::from("Stm"), Value::Reference(form_ref));
        mcr.insert(Name::from("MCID"), Value::Integer(mcid as i64));

        let mut elem = Dict::new();
        elem.insert(Name::from("Type"), Value::Name(Name::from("StructElem")));
        elem.insert(Name::from("S"), Value::Name(Name::from("P")));
        elem.insert(
            Name::from("P"),
            Value::Reference(Reference::new(STRUCT_ROOT, 0)),
        );
        elem.insert(Name::from("Pg"), Value::Reference(Reference::new(PAGE, 0)));
        elem.insert(
            Name::from("K"),
            Value::Array(Array::from(vec![Value::Dict(mcr)])),
        );
        stats.writes += 1;
        tx.write(elem_ref.number(), &Value::Dict(elem), budget)?;
        elems.push(elem_ref);
    }
    Ok((form_ref, elems))
}

/// Current eager approach (matching `python/pyropdf/_canvas/_tags.py`):
/// walks down rightmost `/Kids` to the last `/Nums` leaf, extends `/Nums`
/// without splitting leaves, updates `/Limits` on non-root path nodes, and
/// increments `/ParentTreeNextKey`.
fn append_tagged_canvas_current(
    tx: &mut Transaction,
    budget: &Budget,
    stats: &mut Stats,
) -> Result<()> {
    stats.reads += 2;
    let root_obj = tx.version().get(STRUCT_ROOT, budget)?.unwrap();
    let Value::Dict(mut root_dict) = refs::copy_for_edit(root_obj.value(), budget)? else {
        panic!("StructTreeRoot dict");
    };
    let tree_ref = match root_dict.get("ParentTree") {
        Some(&Value::Reference(r)) => r.number(),
        _ => panic!("ParentTree ref"),
    };
    let mut path: Vec<(u32, Dict)> = Vec::new();
    let mut cur_num = tree_ref;
    loop {
        stats.reads += 1;
        let node_obj = tx.version().get(cur_num, budget)?.unwrap();
        let Value::Dict(node_dict) = refs::copy_for_edit(node_obj.value(), budget)? else {
            panic!("tree node dict");
        };
        if node_dict.get("Nums").is_some() {
            path.push((cur_num, node_dict));
            break;
        }
        let Some(Value::Array(kids)) = node_dict.get("Kids") else {
            panic!("expected Kids or Nums");
        };
        let Some(&Value::Reference(last_kid)) = kids.as_slice().last() else {
            panic!("non-empty Kids");
        };
        let next_num = last_kid.number();
        path.push((cur_num, node_dict));
        cur_num = next_num;
    }

    let next_prop = match root_dict.get("ParentTreeNextKey") {
        Some(&Value::Integer(k)) if k >= 0 => k,
        _ => 0,
    };
    let is_non_root = path.len() > 1;
    let (_, leaf_dict) = path.last_mut().unwrap();
    let Value::Array(old_nums) = leaf_dict.get("Nums").unwrap() else {
        panic!("Nums array");
    };
    let mut key = next_prop;
    if old_nums.len() >= 2
        && let Some(&Value::Integer(last_k)) = old_nums.get(old_nums.len() - 2)
    {
        key = key.max(last_k + 1);
    }

    let (_, elems) = write_canvas_elements(tx, budget, Some(key), stats)?;
    let owners = Value::Array(Array::from(
        elems
            .iter()
            .copied()
            .map(Value::Reference)
            .collect::<Vec<_>>(),
    ));

    let mut new_nums: Vec<Value> = old_nums.iter().cloned().collect();
    new_nums.push(Value::Integer(key));
    new_nums.push(owners);
    let first_key = match new_nums.first() {
        Some(&Value::Integer(k)) => k,
        _ => key,
    };
    leaf_dict.insert(Name::from("Nums"), Value::Array(Array::from(new_nums)));
    if is_non_root {
        leaf_dict.insert(
            Name::from("Limits"),
            Value::Array(Array::from(vec![
                Value::Integer(first_key),
                Value::Integer(key),
            ])),
        );
    }
    let (leaf_num, leaf_owned) = path.pop().unwrap();
    stats.writes += 1;
    tx.write(leaf_num, &Value::Dict(leaf_owned), budget)?;

    for (idx, (anc_num, mut anc_dict)) in path.into_iter().enumerate() {
        if idx > 0 {
            let low = match anc_dict.get("Limits") {
                Some(Value::Array(arr)) => arr.get(0).cloned().unwrap_or(Value::Integer(key)),
                _ => Value::Integer(key),
            };
            anc_dict.insert(
                Name::from("Limits"),
                Value::Array(Array::from(vec![low, Value::Integer(key)])),
            );
            stats.writes += 1;
            tx.write(anc_num, &Value::Dict(anc_dict), budget)?;
        }
    }

    // Append new top-level StructElems to StructTreeRoot /K and bump ParentTreeNextKey.
    let mut k_vec: Vec<Value> = match root_dict.get("K") {
        Some(Value::Array(arr)) => arr.iter().cloned().collect(),
        _ => Vec::new(),
    };
    k_vec.extend(elems.into_iter().map(Value::Reference));
    root_dict.insert(Name::from("K"), Value::Array(Array::from(k_vec)));
    root_dict.insert(Name::from("ParentTreeNextKey"), Value::Integer(key + 1));
    stats.writes += 1;
    tx.write(STRUCT_ROOT, &Value::Dict(root_dict), budget)?;
    Ok(())
}

/// Option B prototype: canonical Rust helper with bounded 64-pair leaves.
/// Appends `[key, owners]` to the rightmost leaf if `< MAX_LEAF_PAIRS`, else
/// allocates a new leaf under root `/Kids`, maintaining `/Limits` on `/Kids`
/// leaves inline in the same write.
pub fn append_tagged_canvas_option_b(
    tx: &mut Transaction,
    budget: &Budget,
    stats: &mut Stats,
) -> Result<()> {
    stats.reads += 2;
    let root_obj = tx.version().get(STRUCT_ROOT, budget)?.unwrap();
    let Value::Dict(mut root_dict) = refs::copy_for_edit(root_obj.value(), budget)? else {
        panic!("StructTreeRoot dict");
    };
    let next_prop = match root_dict.get("ParentTreeNextKey") {
        Some(&Value::Integer(k)) if k >= 0 => k,
        _ => 0,
    };
    let tree_obj = tx.version().get(PARENT_TREE, budget)?.unwrap();
    let Value::Dict(mut tree_dict) = refs::copy_for_edit(tree_obj.value(), budget)? else {
        panic!("ParentTree dict");
    };

    let key = next_prop;
    let (_, elems) = write_canvas_elements(tx, budget, Some(key), stats)?;
    let owners = Value::Array(Array::from(
        elems
            .iter()
            .copied()
            .map(Value::Reference)
            .collect::<Vec<_>>(),
    ));

    if let Some(Value::Array(nums)) = tree_dict.get("Nums").cloned() {
        let pairs = nums.len() / 2;
        if pairs < MAX_LEAF_PAIRS {
            let mut updated: Vec<Value> = nums.iter().cloned().collect();
            updated.push(Value::Integer(key));
            updated.push(owners);
            tree_dict.insert(Name::from("Nums"), Value::Array(Array::from(updated)));
            stats.writes += 1;
            tx.write(PARENT_TREE, &Value::Dict(tree_dict), budget)?;
        } else {
            // Promote single-node root into `/Kids [first_leaf, second_leaf]`.
            let first_low = nums.get(0).cloned().unwrap_or(Value::Integer(0));
            let first_high = nums
                .get(nums.len() - 2)
                .cloned()
                .unwrap_or(Value::Integer(0));
            let leaf1_ref = tx.allocate()?;
            let mut leaf1 = Dict::new();
            leaf1.insert(Name::from("Nums"), Value::Array(nums));
            leaf1.insert(
                Name::from("Limits"),
                Value::Array(Array::from(vec![first_low, first_high])),
            );
            stats.writes += 1;
            tx.write(leaf1_ref.number(), &Value::Dict(leaf1), budget)?;

            let leaf2_ref = tx.allocate()?;
            let mut leaf2 = Dict::new();
            leaf2.insert(
                Name::from("Nums"),
                Value::Array(Array::from(vec![Value::Integer(key), owners])),
            );
            leaf2.insert(
                Name::from("Limits"),
                Value::Array(Array::from(vec![Value::Integer(key), Value::Integer(key)])),
            );
            stats.writes += 1;
            tx.write(leaf2_ref.number(), &Value::Dict(leaf2), budget)?;

            tree_dict.remove("Nums");
            tree_dict.remove("Limits");
            tree_dict.insert(
                Name::from("Kids"),
                Value::Array(Array::from(vec![
                    Value::Reference(leaf1_ref),
                    Value::Reference(leaf2_ref),
                ])),
            );
            stats.writes += 1;
            tx.write(PARENT_TREE, &Value::Dict(tree_dict), budget)?;
        }
    } else {
        let Some(Value::Array(kids)) = tree_dict.get("Kids").cloned() else {
            panic!("ParentTree must have Nums or Kids");
        };
        let last_ref = match kids.as_slice().last() {
            Some(&Value::Reference(r)) => r,
            _ => panic!("Kids ref"),
        };
        stats.reads += 1;
        let leaf_obj = tx.version().get(last_ref.number(), budget)?.unwrap();
        let Value::Dict(mut leaf_dict) = refs::copy_for_edit(leaf_obj.value(), budget)? else {
            panic!("leaf dict");
        };
        let Some(Value::Array(nums)) = leaf_dict.get("Nums").cloned() else {
            panic!("leaf Nums");
        };
        if nums.len() / 2 < MAX_LEAF_PAIRS {
            let mut updated: Vec<Value> = nums.iter().cloned().collect();
            updated.push(Value::Integer(key));
            updated.push(owners);
            let low = updated[0].clone();
            leaf_dict.insert(Name::from("Nums"), Value::Array(Array::from(updated)));
            leaf_dict.insert(
                Name::from("Limits"),
                Value::Array(Array::from(vec![low, Value::Integer(key)])),
            );
            stats.writes += 1;
            tx.write(last_ref.number(), &Value::Dict(leaf_dict), budget)?;
        } else {
            let new_leaf_ref = tx.allocate()?;
            let mut new_leaf = Dict::new();
            new_leaf.insert(
                Name::from("Nums"),
                Value::Array(Array::from(vec![Value::Integer(key), owners])),
            );
            new_leaf.insert(
                Name::from("Limits"),
                Value::Array(Array::from(vec![Value::Integer(key), Value::Integer(key)])),
            );
            stats.writes += 1;
            tx.write(new_leaf_ref.number(), &Value::Dict(new_leaf), budget)?;

            let mut updated_kids: Vec<Value> = kids.iter().cloned().collect();
            updated_kids.push(Value::Reference(new_leaf_ref));
            tree_dict.insert(Name::from("Kids"), Value::Array(Array::from(updated_kids)));
            stats.writes += 1;
            tx.write(PARENT_TREE, &Value::Dict(tree_dict), budget)?;
        }
    }

    let mut k_vec: Vec<Value> = match root_dict.get("K") {
        Some(Value::Array(arr)) => arr.iter().cloned().collect(),
        _ => Vec::new(),
    };
    k_vec.extend(elems.into_iter().map(Value::Reference));
    root_dict.insert(Name::from("K"), Value::Array(Array::from(k_vec)));
    root_dict.insert(Name::from("ParentTreeNextKey"), Value::Integer(key + 1));
    stats.writes += 1;
    tx.write(STRUCT_ROOT, &Value::Dict(root_dict), budget)?;
    Ok(())
}

/// Best-case Option A prototype:
/// - Mutations mark only newly created `StructElem` object numbers under
///   `Structure::ParentTree` (avoiding re-walking untouched `StructElem`s).
/// - `ParentTreeSettler` reads the marked `StructElem`s and their `Stm`
///   `/StructParents` keys, appends all collected `(key, owners)` pairs into
///   bounded 64-pair `/ParentTree` leaves, and marks touched leaves under
///   `Structure::TreeLimits`.
/// - `ParentTreeLimitsSettler` updates `/Limits` on marked leaves.
#[derive(Default)]
struct ParentTreeSettler {
    runs: AtomicUsize,
    reads: AtomicUsize,
    writes: AtomicUsize,
}

impl Settler for ParentTreeSettler {
    fn settle(&self, tx: &mut Transaction, numbers: &[u32], budget: &Budget) -> Result<()> {
        self.runs.fetch_add(1, Ordering::Relaxed);
        let mut by_key: BTreeMap<i64, Vec<(usize, Reference)>> = BTreeMap::new();
        for &elem_num in numbers {
            self.reads.fetch_add(1, Ordering::Relaxed);
            let Some(elem_obj) = tx.version().get(elem_num, budget)? else {
                continue;
            };
            let elem_ref = Reference::new(elem_num, elem_obj.generation());
            let Value::Dict(elem_dict) = &**elem_obj.value() else {
                continue;
            };
            let Some(Value::Array(kids)) = elem_dict.get("K") else {
                continue;
            };
            for item in kids.iter() {
                let Value::Dict(mcr) = item else { continue };
                let Some(&Value::Reference(stm)) = mcr.get("Stm") else {
                    continue;
                };
                let Some(&Value::Integer(mcid)) = mcr.get("MCID") else {
                    continue;
                };
                self.reads.fetch_add(1, Ordering::Relaxed);
                let Some(stm_obj) = tx.version().get(stm.number(), budget)? else {
                    continue;
                };
                let Value::Stream(stream) = &**stm_obj.value() else {
                    continue;
                };
                let Some(&Value::Integer(sp_key)) = stream.dict().get("StructParents") else {
                    continue;
                };
                by_key
                    .entry(sp_key)
                    .or_default()
                    .push((mcid as usize, elem_ref));
            }
        }
        if by_key.is_empty() {
            return Ok(());
        }

        self.reads.fetch_add(1, Ordering::Relaxed);
        let tree_obj = tx.version().get(PARENT_TREE, budget)?.unwrap();
        let Value::Dict(mut tree_dict) = refs::copy_for_edit(tree_obj.value(), budget)? else {
            return Ok(());
        };

        let pairs: Vec<(i64, Value)> = by_key
            .into_iter()
            .map(|(k, mut items)| {
                let len = items.iter().map(|(m, _)| *m + 1).max().unwrap_or(0);
                let mut owners = vec![Value::Null; len];
                items.sort_by_key(|(m, _)| *m);
                for (m, r) in items {
                    owners[m] = Value::Reference(r);
                }
                (k, Value::Array(Array::from(owners)))
            })
            .collect();

        if let Some(Value::Array(nums)) = tree_dict.get("Nums").cloned() {
            let mut all: Vec<Value> = nums.iter().cloned().collect();
            for (k, v) in pairs {
                all.push(Value::Integer(k));
                all.push(v);
            }
            if all.len() / 2 <= MAX_LEAF_PAIRS {
                tree_dict.insert(Name::from("Nums"), Value::Array(Array::from(all)));
                self.writes.fetch_add(1, Ordering::Relaxed);
                tx.write(PARENT_TREE, &Value::Dict(tree_dict), budget)?;
            } else {
                let mut kid_refs = Vec::new();
                for chunk in all.chunks(2 * MAX_LEAF_PAIRS) {
                    let leaf_ref = tx.allocate()?;
                    let mut leaf = Dict::new();
                    leaf.insert(
                        Name::from("Nums"),
                        Value::Array(Array::from(chunk.to_vec())),
                    );
                    self.writes.fetch_add(1, Ordering::Relaxed);
                    tx.write(leaf_ref.number(), &Value::Dict(leaf), budget)?;
                    tx.mark(Structure::TreeLimits, leaf_ref.number(), budget)?;
                    kid_refs.push(Value::Reference(leaf_ref));
                }
                tree_dict.remove("Nums");
                tree_dict.remove("Limits");
                tree_dict.insert(Name::from("Kids"), Value::Array(Array::from(kid_refs)));
                self.writes.fetch_add(1, Ordering::Relaxed);
                tx.write(PARENT_TREE, &Value::Dict(tree_dict), budget)?;
            }
        } else {
            let Some(Value::Array(kids)) = tree_dict.get("Kids").cloned() else {
                return Ok(());
            };
            let mut kid_refs: Vec<Value> = kids.iter().cloned().collect();
            let last_ref = match kid_refs.last() {
                Some(&Value::Reference(r)) => r,
                _ => return Ok(()),
            };
            self.reads.fetch_add(1, Ordering::Relaxed);
            let last_obj = tx.version().get(last_ref.number(), budget)?.unwrap();
            let Value::Dict(mut last_dict) = refs::copy_for_edit(last_obj.value(), budget)? else {
                return Ok(());
            };
            let mut cur_nums: Vec<Value> = match last_dict.get("Nums") {
                Some(Value::Array(a)) => a.iter().cloned().collect(),
                _ => Vec::new(),
            };
            let mut iter = pairs.into_iter().peekable();
            let mut touched_last = false;
            while cur_nums.len() / 2 < MAX_LEAF_PAIRS {
                let Some((k, v)) = iter.next() else { break };
                cur_nums.push(Value::Integer(k));
                cur_nums.push(v);
                touched_last = true;
            }
            if touched_last {
                last_dict.insert(Name::from("Nums"), Value::Array(Array::from(cur_nums)));
                self.writes.fetch_add(1, Ordering::Relaxed);
                tx.write(last_ref.number(), &Value::Dict(last_dict), budget)?;
                tx.mark(Structure::TreeLimits, last_ref.number(), budget)?;
            }
            let rest: Vec<(i64, Value)> = iter.collect();
            if !rest.is_empty() {
                for chunk in rest.chunks(MAX_LEAF_PAIRS) {
                    let mut flat = Vec::with_capacity(2 * chunk.len());
                    for (k, v) in chunk {
                        flat.push(Value::Integer(*k));
                        flat.push(v.clone());
                    }
                    let leaf_ref = tx.allocate()?;
                    let mut leaf = Dict::new();
                    leaf.insert(Name::from("Nums"), Value::Array(Array::from(flat)));
                    self.writes.fetch_add(1, Ordering::Relaxed);
                    tx.write(leaf_ref.number(), &Value::Dict(leaf), budget)?;
                    tx.mark(Structure::TreeLimits, leaf_ref.number(), budget)?;
                    kid_refs.push(Value::Reference(leaf_ref));
                }
                tree_dict.insert(Name::from("Kids"), Value::Array(Array::from(kid_refs)));
                self.writes.fetch_add(1, Ordering::Relaxed);
                tx.write(PARENT_TREE, &Value::Dict(tree_dict), budget)?;
            }
        }
        Ok(())
    }
}

#[derive(Default)]
struct LeafLimitsSettler {
    runs: AtomicUsize,
    reads: AtomicUsize,
    writes: AtomicUsize,
}

impl Settler for LeafLimitsSettler {
    fn settle(&self, tx: &mut Transaction, numbers: &[u32], budget: &Budget) -> Result<()> {
        self.runs.fetch_add(1, Ordering::Relaxed);
        for &num in numbers {
            self.reads.fetch_add(1, Ordering::Relaxed);
            let Some(obj) = tx.version().get(num, budget)? else {
                continue;
            };
            let Value::Dict(mut dict) = refs::copy_for_edit(obj.value(), budget)? else {
                continue;
            };
            let Some(Value::Array(nums)) = dict.get("Nums") else {
                continue;
            };
            if nums.len() >= 2 {
                let limits = Value::Array(Array::from(vec![
                    nums.get(0).unwrap().clone(),
                    nums.get(nums.len() - 2).unwrap().clone(),
                ]));
                if dict.get("Limits") != Some(&limits) {
                    dict.insert(Name::from("Limits"), limits);
                    self.writes.fetch_add(1, Ordering::Relaxed);
                    tx.write(num, &Value::Dict(dict), budget)?;
                }
            }
        }
        Ok(())
    }
}

fn append_tagged_canvas_option_a(
    tx: &mut Transaction,
    budget: &Budget,
    stats: &mut Stats,
) -> Result<()> {
    stats.reads += 1;
    let root_obj = tx.version().get(STRUCT_ROOT, budget)?.unwrap();
    let Value::Dict(mut root_dict) = refs::copy_for_edit(root_obj.value(), budget)? else {
        panic!("StructTreeRoot dict");
    };
    let key = match root_dict.get("ParentTreeNextKey") {
        Some(&Value::Integer(k)) if k >= 0 => k,
        _ => 0,
    };
    let (_, elems) = write_canvas_elements(tx, budget, Some(key), stats)?;
    for &elem in &elems {
        tx.mark(Structure::ParentTree, elem.number(), budget)?;
    }
    let mut k_vec: Vec<Value> = match root_dict.get("K") {
        Some(Value::Array(arr)) => arr.iter().cloned().collect(),
        _ => Vec::new(),
    };
    k_vec.extend(elems.into_iter().map(Value::Reference));
    root_dict.insert(Name::from("K"), Value::Array(Array::from(k_vec)));
    root_dict.insert(Name::from("ParentTreeNextKey"), Value::Integer(key + 1));
    stats.writes += 1;
    tx.write(STRUCT_ROOT, &Value::Dict(root_dict), budget)?;
    Ok(())
}

fn read_all_parent_pairs(version: &Version, budget: &Budget) -> (i64, Vec<(i64, usize)>) {
    let root_obj = version.get(STRUCT_ROOT, budget).unwrap().unwrap();
    let Value::Dict(root_dict) = &**root_obj.value() else {
        panic!("StructTreeRoot");
    };
    let Some(&Value::Integer(next_key)) = root_dict.get("ParentTreeNextKey") else {
        panic!("ParentTreeNextKey");
    };
    let tree_obj = version.get(PARENT_TREE, budget).unwrap().unwrap();
    let Value::Dict(tree_dict) = &**tree_obj.value() else {
        panic!("ParentTree");
    };
    let mut out = Vec::new();
    if let Some(Value::Array(nums)) = tree_dict.get("Nums") {
        assert!(
            tree_dict.get("Limits").is_none(),
            "single-node root must not carry /Limits"
        );
        for pair in nums.as_slice().chunks(2) {
            let Value::Integer(k) = pair[0] else { panic!() };
            let Value::Array(arr) = &pair[1] else {
                panic!()
            };
            out.push((k, arr.len()));
        }
    } else if let Some(Value::Array(kids)) = tree_dict.get("Kids") {
        for kid in kids.iter() {
            let Value::Reference(r) = kid else { panic!() };
            let kid_obj = version.get(r.number(), budget).unwrap().unwrap();
            let Value::Dict(kid_dict) = &**kid_obj.value() else {
                panic!()
            };
            let Some(Value::Array(nums)) = kid_dict.get("Nums") else {
                panic!()
            };
            let Some(Value::Array(limits)) = kid_dict.get("Limits") else {
                panic!("kid leaf missing /Limits");
            };
            assert_eq!(limits.get(0), nums.get(0));
            assert_eq!(limits.get(1), nums.get(nums.len() - 2));
            for pair in nums.as_slice().chunks(2) {
                let Value::Integer(k) = pair[0] else { panic!() };
                let Value::Array(arr) = &pair[1] else {
                    panic!()
                };
                out.push((k, arr.len()));
            }
        }
    }
    (next_key, out)
}

pub fn run_all() -> Vec<BenchRow> {
    let mut rows = Vec::new();
    for &initial in &[0_usize, 256] {
        for &ops in &[1_usize, 16, 64] {
            for &interleaved in &[false, true] {
                rows.extend(run_case(initial, ops, interleaved));
            }
        }
    }
    rows
}

fn run_case(initial: usize, ops: usize, interleaved: bool) -> Vec<BenchRow> {
    let budget = Budget::default();
    let mode = if interleaved { "interleaved" } else { "pure" };

    let doc_cur = build_tagged_doc(initial, Settlers::default());
    let pt_settler = Arc::new(ParentTreeSettler::default());
    let lim_settler = Arc::new(LeafLimitsSettler::default());
    let doc_a = build_tagged_doc(
        initial,
        Settlers::default()
            .with(Structure::ParentTree, pt_settler.clone())
            .with(Structure::TreeLimits, lim_settler.clone()),
    );
    let doc_b = build_tagged_doc(initial, Settlers::default());

    let step_read = |tx: &mut Transaction, stats: &mut Stats| {
        if interleaved {
            // Matches `Open::view()` before reading page geometry at canvas start.
            tx.settle(Scope::Commit, &budget).unwrap();
            stats.reads += 1;
            let _ = tx.version().get(PAGE, &budget).unwrap();
        }
    };

    let exec_cur = |doc: &Document| -> (Arc<Version>, Stats) {
        let mut stats = Stats::default();
        let mut tx = doc.begin();
        for _ in 0..ops {
            step_read(&mut tx, &mut stats);
            append_tagged_canvas_current(&mut tx, &budget, &mut stats).unwrap();
        }
        let v = tx.commit(&budget).unwrap();
        (v, stats)
    };

    let exec_a = |doc: &Document| -> (Arc<Version>, Stats) {
        pt_settler.runs.store(0, Ordering::Relaxed);
        pt_settler.reads.store(0, Ordering::Relaxed);
        pt_settler.writes.store(0, Ordering::Relaxed);
        lim_settler.runs.store(0, Ordering::Relaxed);
        lim_settler.reads.store(0, Ordering::Relaxed);
        lim_settler.writes.store(0, Ordering::Relaxed);
        let mut stats = Stats::default();
        let mut tx = doc.begin();
        for _ in 0..ops {
            step_read(&mut tx, &mut stats);
            append_tagged_canvas_option_a(&mut tx, &budget, &mut stats).unwrap();
        }
        let v = tx.commit(&budget).unwrap();
        stats.settler_runs =
            pt_settler.runs.load(Ordering::Relaxed) + lim_settler.runs.load(Ordering::Relaxed);
        stats.reads +=
            pt_settler.reads.load(Ordering::Relaxed) + lim_settler.reads.load(Ordering::Relaxed);
        stats.writes +=
            pt_settler.writes.load(Ordering::Relaxed) + lim_settler.writes.load(Ordering::Relaxed);
        (v, stats)
    };

    let exec_b = |doc: &Document| -> (Arc<Version>, Stats) {
        let mut stats = Stats::default();
        let mut tx = doc.begin();
        for _ in 0..ops {
            step_read(&mut tx, &mut stats);
            append_tagged_canvas_option_b(&mut tx, &budget, &mut stats).unwrap();
        }
        let v = tx.commit(&budget).unwrap();
        (v, stats)
    };

    let parent_cur = doc_cur.current();
    let (v_cur, stats_cur) = exec_cur(&doc_cur);
    let chg_cur = parent_cur
        .changes(&v_cur, &budget)
        .unwrap()
        .unwrap()
        .objects()
        .len();
    doc_cur.undo().unwrap();

    let parent_a = doc_a.current();
    let (v_a, stats_a) = exec_a(&doc_a);
    let chg_a = parent_a
        .changes(&v_a, &budget)
        .unwrap()
        .unwrap()
        .objects()
        .len();
    doc_a.undo().unwrap();

    let parent_b = doc_b.current();
    let (v_b, stats_b) = exec_b(&doc_b);
    let chg_b = parent_b
        .changes(&v_b, &budget)
        .unwrap()
        .unwrap()
        .objects()
        .len();
    doc_b.undo().unwrap();

    let pairs_cur = read_all_parent_pairs(&v_cur, &budget);
    let pairs_a = read_all_parent_pairs(&v_a, &budget);
    let pairs_b = read_all_parent_pairs(&v_b, &budget);
    assert_eq!(pairs_cur, pairs_a, "Option A ParentTree mismatch");
    assert_eq!(pairs_cur, pairs_b, "Option B ParentTree mismatch");
    assert_eq!(pairs_b.1.len(), initial + ops);

    let samples = if ops >= 64 { 21 } else { 31 };
    let (med_cur, p90_cur) = time_with_undo(samples, &doc_cur, |d| {
        let _ = exec_cur(d);
    });
    let (med_a, p90_a) = time_with_undo(samples, &doc_a, |d| {
        let _ = exec_a(d);
    });
    let (med_b, p90_b) = time_with_undo(samples, &doc_b, |d| {
        let _ = exec_b(d);
    });

    vec![
        BenchRow {
            structure: "ParentTree",
            workload: "tagged_canvas_pages",
            size: initial,
            ops,
            mode,
            variant: "current_unsplit_leaf",
            settler_runs: stats_cur.settler_runs,
            object_reads: stats_cur.reads,
            object_writes: stats_cur.writes,
            changed_objects: chg_cur,
            median_us: med_cur,
            p90_us: p90_cur,
        },
        BenchRow {
            structure: "ParentTree",
            workload: "tagged_canvas_pages",
            size: initial,
            ops,
            mode,
            variant: "option_a_settler",
            settler_runs: stats_a.settler_runs,
            object_reads: stats_a.reads,
            object_writes: stats_a.writes,
            changed_objects: chg_a,
            median_us: med_a,
            p90_us: p90_a,
        },
        BenchRow {
            structure: "ParentTree",
            workload: "tagged_canvas_pages",
            size: initial,
            ops,
            mode,
            variant: "option_b_bounded_immediate",
            settler_runs: stats_b.settler_runs,
            object_reads: stats_b.reads,
            object_writes: stats_b.writes,
            changed_objects: chg_b,
            median_us: med_b,
            p90_us: p90_b,
        },
    ]
}
