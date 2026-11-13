use std::sync::Arc;

use crate::LimitExceeded;
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::refs;
use crate::value::{Array, Bytes, Dict, Name, PdfString, Reference, Value};
use crate::version::{Document, MemoryBase, Version};

use super::*;

fn minimal_doc() -> Document {
    let trailer: Dict = [
        (Name::from("Root"), Value::Reference(Reference::new(1, 0))),
        (Name::from("Size"), Value::Integer(4)),
    ]
    .into_iter()
    .collect();
    let mut base = MemoryBase::new(trailer);
    base.insert(
        Reference::new(1, 0),
        Bytes::new(&b"<</Type/Catalog/Pages 2 0 R>>"[..]),
    );
    base.insert(
        Reference::new(2, 0),
        Bytes::new(&b"<</Type/Pages/Kids[3 0 R]/Count 1>>"[..]),
    );
    base.insert(
        Reference::new(3, 0),
        Bytes::new(&b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]>>"[..]),
    );
    Document::new(Arc::new(base), None)
}

fn live_objects(version: &Version, budget: &Budget) -> Vec<u32> {
    let mut nums = BudgetVec::new(budget);
    version.base().numbers(&mut nums).unwrap();
    let mut out = nums.into_vec();
    for (num, _) in version.slots() {
        out.push(num);
    }
    out.sort_unstable();
    out.dedup();
    out.retain(|&n| version.get(n, budget).unwrap().is_some());
    out
}

/// Checks canonical invariants on a name or number tree:
/// - Root has NO `/Limits` (unless `allow_root_limits` is true).
/// - Every non-root node has `/Limits: [least, greatest]` matching its subtree.
/// - Every leaf has `1..=MAX_LEAF_PAIRS` strictly ascending keys.
/// - Every leaf sits at the exact same depth (`<= MAX_TREE_DEPTH`).
fn assert_canonical_number_tree(version: &Version, root_ref: Reference, budget: &Budget) -> usize {
    let root_obj = version.get(root_ref.number(), budget).unwrap().unwrap();
    let Value::Dict(root_dict) = &**root_obj.value() else {
        panic!("root must be dict");
    };
    assert!(root_dict.get("Limits").is_none(), "root must omit /Limits");
    if let Some(Value::Array(nums)) = root_dict.get("Nums") {
        assert!(root_dict.get("Kids").is_none());
        let pairs = nums.len() / 2;
        assert!(pairs <= MAX_LEAF_PAIRS);
        return 0;
    }
    let Some(Value::Array(kids)) = root_dict.get("Kids") else {
        panic!("root must have /Nums or /Kids");
    };
    assert!((2..=MAX_LEAF_PAIRS).contains(&kids.len()));
    let mut leaf_depth: Option<usize> = None;
    let mut prev_high: Option<i64> = None;
    for item in kids.iter() {
        let Value::Reference(r) = item else {
            panic!("kid must be reference");
        };
        let (low, high, depth) = check_num_subtree(version, *r, 1, budget);
        if let Some(prev) = prev_high {
            assert!(prev < low, "sibling bounds must ascend: {prev} < {low}");
        }
        prev_high = Some(high);
        match leaf_depth {
            Some(d) => assert_eq!(d, depth, "all leaves must share one depth"),
            None => leaf_depth = Some(depth),
        }
    }
    leaf_depth.unwrap()
}

fn check_num_subtree(
    version: &Version,
    node_ref: Reference,
    depth: usize,
    budget: &Budget,
) -> (i64, i64, usize) {
    assert!(depth <= MAX_TREE_DEPTH);
    let obj = version.get(node_ref.number(), budget).unwrap().unwrap();
    let Value::Dict(dict) = &**obj.value() else {
        panic!("subtree node must be dict");
    };
    let Some(Value::Array(limits)) = dict.get("Limits") else {
        panic!("non-root node must carry /Limits");
    };
    let [Value::Integer(lim_low), Value::Integer(lim_high)] = limits.as_slice() else {
        panic!("integer /Limits expected");
    };
    if let Some(Value::Array(nums)) = dict.get("Nums") {
        assert!(dict.get("Kids").is_none());
        let pairs = nums.len() / 2;
        assert!((1..=MAX_LEAF_PAIRS).contains(&pairs));
        let keys: Vec<i64> = nums
            .iter()
            .step_by(2)
            .map(|v| match v {
                Value::Integer(k) => *k,
                _ => panic!("int key"),
            })
            .collect();
        assert!(keys.windows(2).all(|w| w[0] < w[1]));
        assert_eq!((*lim_low, *lim_high), (keys[0], *keys.last().unwrap()));
        return (*lim_low, *lim_high, depth);
    }
    let Some(Value::Array(kids)) = dict.get("Kids") else {
        panic!("node must have /Nums or /Kids");
    };
    assert!((1..=MAX_LEAF_PAIRS).contains(&kids.len()));
    let mut sub_depth = None;
    let mut first_low = None;
    let mut prev_high: Option<i64> = None;
    for item in kids.iter() {
        let Value::Reference(r) = item else {
            panic!("kid ref");
        };
        let (low, high, d) = check_num_subtree(version, *r, depth + 1, budget);
        if first_low.is_none() {
            first_low = Some(low);
        }
        if let Some(prev) = prev_high {
            assert!(prev < low);
        }
        prev_high = Some(high);
        match sub_depth {
            Some(expected) => assert_eq!(expected, d),
            None => sub_depth = Some(d),
        }
    }
    assert_eq!(
        (*lim_low, *lim_high),
        (first_low.unwrap(), prev_high.unwrap())
    );
    (*lim_low, *lim_high, sub_depth.unwrap())
}

#[test]
fn name_tree_grows_and_shrinks_back_to_zero_without_orphans() {
    let doc = minimal_doc();
    let cache = refs::Cache::new();
    let budget = Budget::default();
    let baseline = live_objects(&doc.current(), &budget);

    // Insert 150 keys in interleaved order so interior leaves split too.
    let mut order: Vec<usize> = (0..75)
        .map(|i| i * 2)
        .chain((0..75).map(|i| i * 2 + 1))
        .collect();
    for &idx in &order {
        let key_str = format!("k_{idx:04}");
        let key = PdfString::literal(key_str.into_bytes());
        let mut fallback = BudgetVec::new(&budget);
        // Fallback is only used on the very first insertion when /EmbeddedFiles is absent.
        fallback
            .try_push((key.clone(), Value::Integer(idx as i64)))
            .unwrap();
        let mut tx = doc.begin();
        insert_catalog_embedded_files(
            &cache,
            &mut tx,
            key,
            Value::Integer(idx as i64),
            fallback,
            &budget,
        )
        .unwrap();
        tx.commit(&budget).unwrap();
    }

    // Verify root has /Kids (no /Limits) and every leaf has <= 64 pairs + valid /Limits.
    let v_full = doc.current();
    let (_, cat) = catalog_of(&v_full, &budget).unwrap();
    let Some((Value::Dict(names), _)) = located_in(&v_full, cat.get("Names"), &budget).unwrap()
    else {
        panic!("Names dict");
    };
    let Some((Value::Dict(ef), _)) =
        located_in(&v_full, names.get("EmbeddedFiles"), &budget).unwrap()
    else {
        panic!("EmbeddedFiles dict");
    };
    assert!(ef.get("Limits").is_none());
    let Some(Value::Array(kids)) = ef.get("Kids") else {
        panic!("expected multi-leaf Kids");
    };
    assert!(kids.len() >= 3);
    for kid in kids.iter() {
        let Some((Value::Dict(leaf), _)) = located_in(&v_full, Some(kid), &budget).unwrap() else {
            panic!("leaf dict");
        };
        let Some(Value::Array(leaf_names)) = leaf.get("Names") else {
            panic!("leaf Names");
        };
        assert!((1..=MAX_LEAF_PAIRS).contains(&(leaf_names.len() / 2)));
        assert!(leaf.get("Limits").is_some());
    }

    // Delete all 150 keys in reverse-interleaved order and verify zero leaked objects.
    order.reverse();
    for idx in order {
        let key = PdfString::literal(format!("k_{idx:04}").into_bytes());
        let mut tx = doc.begin();
        remove_catalog_embedded_files(
            &cache,
            &mut tx,
            std::slice::from_ref(&key),
            BudgetVec::new(&budget),
            &[],
            &budget,
        )
        .unwrap();
        tx.commit(&budget).unwrap();
    }

    let v_empty = doc.current();
    let (_, cat_after) = catalog_of(&v_empty, &budget).unwrap();
    assert!(cat_after.get("Names").is_none());
    assert_eq!(live_objects(&v_empty, &budget), baseline);
}

#[test]
fn parent_tree_grows_homogeneously_past_depth_two() {
    let doc = minimal_doc();
    let budget = Budget::default();
    {
        let mut tx = doc.begin();
        let root_ref = tx.allocate().unwrap();
        let mut root = Dict::new();
        root.insert(
            Name::from("Type"),
            Value::Name(Name::from("StructTreeRoot")),
        );
        tx.write(root_ref.number(), &Value::Dict(root), &budget)
            .unwrap();
        let (cat_ref, mut cat) = catalog_of(tx.version(), &budget).unwrap();
        cat.insert(Name::from("StructTreeRoot"), Value::Reference(root_ref));
        tx.write(cat_ref.number(), &Value::Dict(cat), &budget)
            .unwrap();
        tx.commit(&budget).unwrap();
    }

    // Batch-append 4,100 entries (exceeding 64 * 64 = 4,096 -> depth 2), then
    // append single entries via `append_parent_tree_entry` to exercise both paths.
    {
        let mut tx = doc.begin();
        let (_, cat) = catalog_of(tx.version(), &budget).unwrap();
        let Some((Value::Dict(mut root), Some(root_ref))) =
            located_in(tx.version(), cat.get("StructTreeRoot"), &budget).unwrap()
        else {
            panic!("StructTreeRoot");
        };
        let batch: Vec<(i64, Value)> = (0..4100).map(|k| (k, Value::Array(Array::new()))).collect();
        append_parent_tree(&mut tx, &mut root, Some(root_ref), &batch, 4100, &budget).unwrap();
        tx.write(root_ref.number(), &Value::Dict(root), &budget)
            .unwrap();
        tx.commit(&budget).unwrap();
    }

    {
        let mut tx = doc.begin();
        for expected in 4100..4170 {
            let assigned =
                append_parent_tree_entry(&mut tx, Value::Array(Array::new()), &budget).unwrap();
            assert_eq!(assigned, expected);
        }
        tx.commit(&budget).unwrap();
    }

    let v = doc.current();
    let (_, cat) = catalog_of(&v, &budget).unwrap();
    let Some((Value::Dict(root), _)) = located_in(&v, cat.get("StructTreeRoot"), &budget).unwrap()
    else {
        panic!("StructTreeRoot");
    };
    assert_eq!(root.get("ParentTreeNextKey"), Some(&Value::Integer(4170)));
    let Some(&Value::Reference(tree_ref)) = root.get("ParentTree") else {
        panic!("ParentTree ref");
    };
    let depth = assert_canonical_number_tree(&v, tree_ref, &budget);
    assert_eq!(depth, 2);

    let mut pairs = BudgetVec::new(&budget);
    number_tree_pairs(&v, &Value::Reference(tree_ref), &mut pairs, &budget).unwrap();
    assert_eq!(pairs.len(), 4170);
    assert!(
        pairs
            .iter()
            .enumerate()
            .all(|(idx, (k, _))| *k == idx as i64)
    );
}

#[test]
fn tree_helpers_enforce_depth_limit_cycles_and_memory_budget() {
    let trailer: Dict = [
        (Name::from("Root"), Value::Reference(Reference::new(1, 0))),
        (Name::from("Size"), Value::Integer(100)),
    ]
    .into_iter()
    .collect();
    let mut base = MemoryBase::new(trailer);
    base.insert(
        Reference::new(1, 0),
        Bytes::new(&b"<</Type/Catalog/Pages 2 0 R/StructTreeRoot 4 0 R>>"[..]),
    );
    base.insert(
        Reference::new(2, 0),
        Bytes::new(&b"<</Type/Pages/Kids[3 0 R]/Count 1>>"[..]),
    );
    base.insert(
        Reference::new(3, 0),
        Bytes::new(&b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]>>"[..]),
    );
    base.insert(
        Reference::new(4, 0),
        Bytes::new(&b"<</Type/StructTreeRoot/K[]/ParentTree 10 0 R>>"[..]),
    );
    // Depth 65 chain (10 -> 11 -> ... -> 75): exceeds MAX_TREE_DEPTH = 64.
    for n in 10..75 {
        base.insert(
            Reference::new(n, 0),
            Bytes::new(format!("<</Kids[{} 0 R]>>", n + 1).into_bytes()),
        );
    }
    base.insert(Reference::new(75, 0), Bytes::new(&b"<</Nums[0 []]>>"[..]));
    let doc = Document::new(Arc::new(base), None);
    let budget = Budget::default();

    let mut pairs = BudgetVec::new(&budget);
    let err = number_tree_pairs(
        &doc.current(),
        &Value::Reference(Reference::new(10, 0)),
        &mut pairs,
        &budget,
    )
    .unwrap_err();
    assert_eq!(
        err.limit().map(LimitExceeded::code),
        Some(LimitCode::StructureTreeCount)
    );

    let mut tx = doc.begin();
    let err = append_parent_tree_entry(&mut tx, Value::Array(Array::new()), &budget).unwrap_err();
    assert_eq!(
        err.limit().map(LimitExceeded::code),
        Some(LimitCode::StructureTreeCount)
    );

    // Tight memory budget refuses large replacement cleanly.
    let small_doc = minimal_doc();
    let tight = Budget::default().with_limit(LimitCode::MemorySize, Some(1024));
    let mut tx = small_doc.begin();
    let mut big = BudgetVec::new(&budget);
    for k in 0..200 {
        big.try_push((k, Value::Dict(Dict::new()))).unwrap();
    }
    let err = replace_catalog_number_tree(&refs::Cache::new(), &mut tx, "PageLabels", big, &tight)
        .unwrap_err();
    assert_eq!(
        err.limit().map(LimitExceeded::code),
        Some(LimitCode::MemorySize)
    );

    // Pure /Kids cycle (10 -> 11 -> 10) terminates via the visited table.
    let mut cyclic_base = MemoryBase::new(
        [
            (Name::from("Root"), Value::Reference(Reference::new(1, 0))),
            (Name::from("Size"), Value::Integer(15)),
        ]
        .into_iter()
        .collect(),
    );
    cyclic_base.insert(
        Reference::new(1, 0),
        Bytes::new(&b"<</Type/Catalog/Pages 2 0 R/StructTreeRoot 4 0 R>>"[..]),
    );
    cyclic_base.insert(
        Reference::new(2, 0),
        Bytes::new(&b"<</Type/Pages/Kids[3 0 R]/Count 1>>"[..]),
    );
    cyclic_base.insert(
        Reference::new(3, 0),
        Bytes::new(&b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]>>"[..]),
    );
    cyclic_base.insert(
        Reference::new(4, 0),
        Bytes::new(&b"<</Type/StructTreeRoot/K[]/ParentTree 10 0 R>>"[..]),
    );
    cyclic_base.insert(Reference::new(10, 0), Bytes::new(&b"<</Kids[11 0 R]>>"[..]));
    cyclic_base.insert(Reference::new(11, 0), Bytes::new(&b"<</Kids[10 0 R]>>"[..]));
    let cyclic_doc = Document::new(Arc::new(cyclic_base), None);
    let mut tx = cyclic_doc.begin();
    let assigned = append_parent_tree_entry(&mut tx, Value::Array(Array::new()), &budget).unwrap();
    assert_eq!(assigned, 0);
}
