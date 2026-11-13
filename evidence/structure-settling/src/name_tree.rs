//! Name-tree `/Limits` (`/Names /EmbeddedFiles`) benchmark and Option A /
//! Option B prototypes (#765).

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use pyropdf_store::Result;
use pyropdf_store::budget::Budget;
use pyropdf_store::refs;
use pyropdf_store::settle::{Scope, Settler, Settlers, Structure};
use pyropdf_store::value::{Array, Bytes, Dict, Name, PdfString, Reference, Stream, Value};
use pyropdf_store::version::{Document, MemoryBase, Transaction, Version};

use crate::{BenchRow, Stats, time_with_undo};

const CATALOG: u32 = 1;
const PAGES: u32 = 2;
const PAGE: u32 = 3;
const NAMES_DICT: u32 = 4;
const EMBEDDED_TREE: u32 = 5;
const MAX_LEAF_PAIRS: usize = 64;

fn build_attachments_doc(initial: usize, settlers: Settlers) -> Document {
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
        Bytes::new(&b"<</Type/Catalog/Pages 2 0 R/Names 4 0 R>>"[..]),
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
        Reference::new(NAMES_DICT, 0),
        Bytes::new(&b"<</EmbeddedFiles 5 0 R>>"[..]),
    );
    base.insert(
        Reference::new(EMBEDDED_TREE, 0),
        Bytes::new(&b"<</Names[]>>"[..]),
    );

    let doc = Document::new(Arc::new(base), None);
    let budget = Budget::default();
    if initial > 0 {
        let mut tx = doc.begin();
        let mut dummy = Stats::default();
        // Seed even-numbered keys (`item_00000.bin`, `item_00002.bin`, ...) so
        // benchmark insertions of odd keys land across interior leaves.
        for idx in 0..initial {
            let key = format!("item_{:05}.bin", idx * 2);
            insert_attachment_option_b(&mut tx, &budget, key.as_bytes(), b"seed", &mut dummy)
                .unwrap();
        }
        tx.commit(&budget).unwrap();
    }
    doc.set_settlers(settlers);
    doc
}

/// Allocates an `/EmbeddedFile` stream + `/Filespec` dictionary, returning the
/// `Filespec` indirect reference (2 writes, shared identically across variants).
fn write_filespec(
    tx: &mut Transaction,
    budget: &Budget,
    name: &[u8],
    payload: &[u8],
    stats: &mut Stats,
) -> Result<Reference> {
    let ef_stream_ref = tx.allocate()?;
    let mut stream_dict = Dict::new();
    stream_dict.insert(Name::from("Type"), Value::Name(Name::from("EmbeddedFile")));
    let mut params = Dict::new();
    params.insert(Name::from("Size"), Value::Integer(payload.len() as i64));
    stream_dict.insert(Name::from("Params"), Value::Dict(params));
    let stream = Stream::new(stream_dict, Bytes::new(payload.to_vec()));
    stats.writes += 1;
    tx.write(ef_stream_ref.number(), &Value::from(stream), budget)?;

    let filespec_ref = tx.allocate()?;
    let mut ef_dict = Dict::new();
    ef_dict.insert(Name::from("F"), Value::Reference(ef_stream_ref));
    ef_dict.insert(Name::from("UF"), Value::Reference(ef_stream_ref));
    let mut fs_dict = Dict::new();
    fs_dict.insert(Name::from("Type"), Value::Name(Name::from("Filespec")));
    fs_dict.insert(
        Name::from("F"),
        Value::String(PdfString::literal(name.to_vec())),
    );
    fs_dict.insert(
        Name::from("UF"),
        Value::String(PdfString::literal(name.to_vec())),
    );
    fs_dict.insert(Name::from("EF"), Value::Dict(ef_dict));
    stats.writes += 1;
    tx.write(filespec_ref.number(), &Value::Dict(fs_dict), budget)?;
    Ok(filespec_ref)
}

fn collect_name_pairs(
    version: &Version,
    node_num: u32,
    budget: &Budget,
    stats: &mut Stats,
    out: &mut Vec<(Vec<u8>, Reference)>,
) -> Result<()> {
    stats.reads += 1;
    let obj = version.get(node_num, budget)?.unwrap();
    let Value::Dict(dict) = &**obj.value() else {
        panic!("name tree node dict");
    };
    if let Some(Value::Array(names)) = dict.get("Names") {
        for pair in names.as_slice().chunks(2) {
            let Value::String(s) = &pair[0] else { panic!() };
            let Value::Reference(r) = pair[1] else {
                panic!()
            };
            out.push((s.as_bytes().to_vec(), r));
        }
    } else if let Some(Value::Array(kids)) = dict.get("Kids") {
        for kid in kids.iter() {
            let Value::Reference(r) = kid else { panic!() };
            collect_name_pairs(version, r.number(), budget, stats, out)?;
        }
    }
    Ok(())
}

/// Current eager flat-rewrite approach (matching `python/pyropdf/_attachments.py`):
/// walks the entire `/EmbeddedFiles` name tree on every `add()`, checks
/// uniqueness, and rewrites a single flat `Names` array on the root node
/// (removing `Kids` and `Limits`, leaving any prior `/Kids` nodes orphaned).
fn insert_attachment_current_flat(
    tx: &mut Transaction,
    budget: &Budget,
    key: &[u8],
    payload: &[u8],
    stats: &mut Stats,
) -> Result<()> {
    let mut pairs = Vec::new();
    collect_name_pairs(tx.version(), EMBEDDED_TREE, budget, stats, &mut pairs)?;
    let pos = pairs
        .binary_search_by(|(k, _)| k.as_slice().cmp(key))
        .expect_err("duplicate attachment name");
    let fs_ref = write_filespec(tx, budget, key, payload, stats)?;
    pairs.insert(pos, (key.to_vec(), fs_ref));

    let mut flat = Vec::with_capacity(pairs.len() * 2);
    for (k, r) in pairs {
        flat.push(Value::String(PdfString::literal(k)));
        flat.push(Value::Reference(r));
    }
    stats.reads += 1;
    let root_obj = tx.version().get(EMBEDDED_TREE, budget)?.unwrap();
    let Value::Dict(mut root_dict) = refs::copy_for_edit(root_obj.value(), budget)? else {
        panic!("root dict");
    };
    root_dict.remove("Kids");
    root_dict.remove("Limits");
    root_dict.insert(Name::from("Names"), Value::Array(Array::from(flat)));
    stats.writes += 1;
    tx.write(EMBEDDED_TREE, &Value::Dict(root_dict), budget)?;
    Ok(())
}

fn leaf_limits_value(names: &[Value]) -> Value {
    assert!(names.len() >= 2);
    Value::Array(Array::from(vec![
        names[0].clone(),
        names[names.len() - 2].clone(),
    ]))
}

fn insert_into_names_vec(names: &Array, key: &[u8], fs_ref: Reference) -> Vec<Value> {
    let slice = names.as_slice();
    let pair_count = slice.len() / 2;
    let mut lo = 0_usize;
    let mut hi = pair_count;
    while lo < hi {
        let mid = (lo + hi) / 2;
        let Value::String(s) = &slice[mid * 2] else {
            panic!("string key")
        };
        match s.as_bytes().cmp(key) {
            std::cmp::Ordering::Less => lo = mid + 1,
            std::cmp::Ordering::Greater => hi = mid,
            std::cmp::Ordering::Equal => panic!("duplicate attachment key"),
        }
    }
    let mut out = Vec::with_capacity(slice.len() + 2);
    out.extend_from_slice(&slice[..lo * 2]);
    out.push(Value::String(PdfString::literal(key.to_vec())));
    out.push(Value::Reference(fs_ref));
    out.extend_from_slice(&slice[lo * 2..]);
    out
}

/// Option B prototype: canonical bounded-leaf helper with immediate inline
/// `/Limits` maintenance.
///
/// Routes via `/Limits` on `/Kids` leaves, inserts into the target leaf, splits
/// any leaf exceeding `MAX_LEAF_PAIRS` into two balanced leaves, and sets
/// `/Limits = [first, last]` on the already-copied `Dict` before `tx.write`
/// (zero extra reads and zero extra writes).
pub fn insert_attachment_option_b(
    tx: &mut Transaction,
    budget: &Budget,
    key: &[u8],
    payload: &[u8],
    stats: &mut Stats,
) -> Result<()> {
    stats.reads += 1;
    let root_obj = tx.version().get(EMBEDDED_TREE, budget)?.unwrap();
    let Value::Dict(mut root_dict) = refs::copy_for_edit(root_obj.value(), budget)? else {
        panic!("root dict");
    };
    let fs_ref = write_filespec(tx, budget, key, payload, stats)?;

    if let Some(Value::Array(names)) = root_dict.get("Names").cloned() {
        let updated = insert_into_names_vec(&names, key, fs_ref);
        if updated.len() / 2 <= MAX_LEAF_PAIRS {
            root_dict.insert(Name::from("Names"), Value::Array(Array::from(updated)));
            stats.writes += 1;
            tx.write(EMBEDDED_TREE, &Value::Dict(root_dict), budget)?;
        } else {
            let mid_pair = (updated.len() / 2) / 2;
            let split_at = mid_pair * 2;
            let left_vec = updated[..split_at].to_vec();
            let right_vec = updated[split_at..].to_vec();

            let left_ref = tx.allocate()?;
            let mut left = Dict::new();
            left.insert(Name::from("Limits"), leaf_limits_value(&left_vec));
            left.insert(Name::from("Names"), Value::Array(Array::from(left_vec)));
            stats.writes += 1;
            tx.write(left_ref.number(), &Value::Dict(left), budget)?;

            let right_ref = tx.allocate()?;
            let mut right = Dict::new();
            right.insert(Name::from("Limits"), leaf_limits_value(&right_vec));
            right.insert(Name::from("Names"), Value::Array(Array::from(right_vec)));
            stats.writes += 1;
            tx.write(right_ref.number(), &Value::Dict(right), budget)?;

            root_dict.remove("Names");
            root_dict.remove("Limits");
            root_dict.insert(
                Name::from("Kids"),
                Value::Array(Array::from(vec![
                    Value::Reference(left_ref),
                    Value::Reference(right_ref),
                ])),
            );
            stats.writes += 1;
            tx.write(EMBEDDED_TREE, &Value::Dict(root_dict), budget)?;
        }
        return Ok(());
    }

    let Some(Value::Array(kids)) = root_dict.get("Kids").cloned() else {
        panic!("expected Names or Kids");
    };
    let kid_slice = kids.as_slice();
    // Route using valid `/Limits` on `/Kids` leaves: choose the first leaf whose
    // upper limit `>= key`, or the last leaf if `key` exceeds all upper limits.
    let mut chosen_idx = kid_slice.len() - 1;
    let mut chosen_dict: Option<(u32, Dict)> = None;
    for (idx, item) in kid_slice.iter().enumerate() {
        let Value::Reference(r) = item else { panic!() };
        stats.reads += 1;
        let kid_obj = tx.version().get(r.number(), budget)?.unwrap();
        let Value::Dict(kid_dict) = &**kid_obj.value() else {
            panic!()
        };
        let Some(Value::Array(limits)) = kid_dict.get("Limits") else {
            panic!("Option B keeps /Limits valid at every point");
        };
        let Some(Value::String(high)) = limits.get(1) else {
            panic!()
        };
        if key <= high.as_bytes() || idx + 1 == kid_slice.len() {
            chosen_idx = idx;
            let Value::Dict(editable) = refs::copy_for_edit(kid_obj.value(), budget)? else {
                panic!()
            };
            chosen_dict = Some((r.number(), editable));
            break;
        }
    }

    let (leaf_num, mut leaf_dict) = chosen_dict.unwrap();
    let Some(Value::Array(names)) = leaf_dict.get("Names").cloned() else {
        panic!("leaf Names");
    };
    let updated = insert_into_names_vec(&names, key, fs_ref);
    if updated.len() / 2 <= MAX_LEAF_PAIRS {
        leaf_dict.insert(Name::from("Limits"), leaf_limits_value(&updated));
        leaf_dict.insert(Name::from("Names"), Value::Array(Array::from(updated)));
        stats.writes += 1;
        tx.write(leaf_num, &Value::Dict(leaf_dict), budget)?;
    } else {
        let mid_pair = (updated.len() / 2) / 2;
        let split_at = mid_pair * 2;
        let left_vec = updated[..split_at].to_vec();
        let right_vec = updated[split_at..].to_vec();

        leaf_dict.insert(Name::from("Limits"), leaf_limits_value(&left_vec));
        leaf_dict.insert(Name::from("Names"), Value::Array(Array::from(left_vec)));
        stats.writes += 1;
        tx.write(leaf_num, &Value::Dict(leaf_dict), budget)?;

        let right_ref = tx.allocate()?;
        let mut right = Dict::new();
        right.insert(Name::from("Limits"), leaf_limits_value(&right_vec));
        right.insert(Name::from("Names"), Value::Array(Array::from(right_vec)));
        stats.writes += 1;
        tx.write(right_ref.number(), &Value::Dict(right), budget)?;

        let mut new_kids: Vec<Value> = kid_slice.to_vec();
        new_kids.insert(chosen_idx + 1, Value::Reference(right_ref));
        root_dict.insert(Name::from("Kids"), Value::Array(Array::from(new_kids)));
        stats.writes += 1;
        tx.write(EMBEDDED_TREE, &Value::Dict(root_dict), budget)?;
    }
    Ok(())
}

/// Option A prototype: `Settler` registered on `Structure::TreeLimits`.
/// Re-reads each marked `/Kids` leaf and updates `/Limits = [first, last]`.
#[derive(Default)]
struct NameLimitsSettler {
    runs: AtomicUsize,
    reads: AtomicUsize,
    writes: AtomicUsize,
}

impl Settler for NameLimitsSettler {
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
            let Some(Value::Array(names)) = dict.get("Names") else {
                continue;
            };
            if names.len() >= 2 {
                let limits = leaf_limits_value(names.as_slice());
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

/// Option A insertion: modifies `/Names` on bounded leaves without updating
/// `/Limits`, marking touched leaves under `Structure::TreeLimits`.
/// Notice that because `/Limits` may be stale on previously modified leaves in
/// the same transaction, routing inspects `Names[len - 2]` directly on each
/// candidate leaf rather than trusting stale `/Limits`.
fn insert_attachment_option_a(
    tx: &mut Transaction,
    budget: &Budget,
    key: &[u8],
    payload: &[u8],
    stats: &mut Stats,
) -> Result<()> {
    stats.reads += 1;
    let root_obj = tx.version().get(EMBEDDED_TREE, budget)?.unwrap();
    let Value::Dict(mut root_dict) = refs::copy_for_edit(root_obj.value(), budget)? else {
        panic!("root dict");
    };
    let fs_ref = write_filespec(tx, budget, key, payload, stats)?;

    if let Some(Value::Array(names)) = root_dict.get("Names").cloned() {
        let updated = insert_into_names_vec(&names, key, fs_ref);
        if updated.len() / 2 <= MAX_LEAF_PAIRS {
            root_dict.insert(Name::from("Names"), Value::Array(Array::from(updated)));
            stats.writes += 1;
            tx.write(EMBEDDED_TREE, &Value::Dict(root_dict), budget)?;
        } else {
            let mid_pair = (updated.len() / 2) / 2;
            let split_at = mid_pair * 2;
            let left_vec = updated[..split_at].to_vec();
            let right_vec = updated[split_at..].to_vec();

            let left_ref = tx.allocate()?;
            let mut left = Dict::new();
            left.insert(Name::from("Names"), Value::Array(Array::from(left_vec)));
            stats.writes += 1;
            tx.write(left_ref.number(), &Value::Dict(left), budget)?;
            tx.mark(Structure::TreeLimits, left_ref.number(), budget)?;

            let right_ref = tx.allocate()?;
            let mut right = Dict::new();
            right.insert(Name::from("Names"), Value::Array(Array::from(right_vec)));
            stats.writes += 1;
            tx.write(right_ref.number(), &Value::Dict(right), budget)?;
            tx.mark(Structure::TreeLimits, right_ref.number(), budget)?;

            root_dict.remove("Names");
            root_dict.remove("Limits");
            root_dict.insert(
                Name::from("Kids"),
                Value::Array(Array::from(vec![
                    Value::Reference(left_ref),
                    Value::Reference(right_ref),
                ])),
            );
            stats.writes += 1;
            tx.write(EMBEDDED_TREE, &Value::Dict(root_dict), budget)?;
        }
        return Ok(());
    }

    let Some(Value::Array(kids)) = root_dict.get("Kids").cloned() else {
        panic!("expected Names or Kids");
    };
    let kid_slice = kids.as_slice();
    let mut chosen_idx = kid_slice.len() - 1;
    let mut chosen_dict: Option<(u32, Dict)> = None;
    for (idx, item) in kid_slice.iter().enumerate() {
        let Value::Reference(r) = item else { panic!() };
        stats.reads += 1;
        let kid_obj = tx.version().get(r.number(), budget)?.unwrap();
        let Value::Dict(kid_dict) = &**kid_obj.value() else {
            panic!()
        };
        let Some(Value::Array(names)) = kid_dict.get("Names") else {
            panic!()
        };
        let Some(Value::String(high)) = names.get(names.len() - 2) else {
            panic!()
        };
        if key <= high.as_bytes() || idx + 1 == kid_slice.len() {
            chosen_idx = idx;
            let Value::Dict(editable) = refs::copy_for_edit(kid_obj.value(), budget)? else {
                panic!()
            };
            chosen_dict = Some((r.number(), editable));
            break;
        }
    }

    let (leaf_num, mut leaf_dict) = chosen_dict.unwrap();
    let Some(Value::Array(names)) = leaf_dict.get("Names").cloned() else {
        panic!("leaf Names");
    };
    let updated = insert_into_names_vec(&names, key, fs_ref);
    if updated.len() / 2 <= MAX_LEAF_PAIRS {
        leaf_dict.insert(Name::from("Names"), Value::Array(Array::from(updated)));
        stats.writes += 1;
        tx.write(leaf_num, &Value::Dict(leaf_dict), budget)?;
        tx.mark(Structure::TreeLimits, leaf_num, budget)?;
    } else {
        let mid_pair = (updated.len() / 2) / 2;
        let split_at = mid_pair * 2;
        let left_vec = updated[..split_at].to_vec();
        let right_vec = updated[split_at..].to_vec();

        leaf_dict.insert(Name::from("Names"), Value::Array(Array::from(left_vec)));
        stats.writes += 1;
        tx.write(leaf_num, &Value::Dict(leaf_dict), budget)?;
        tx.mark(Structure::TreeLimits, leaf_num, budget)?;

        let right_ref = tx.allocate()?;
        let mut right = Dict::new();
        right.insert(Name::from("Names"), Value::Array(Array::from(right_vec)));
        stats.writes += 1;
        tx.write(right_ref.number(), &Value::Dict(right), budget)?;
        tx.mark(Structure::TreeLimits, right_ref.number(), budget)?;

        let mut new_kids: Vec<Value> = kid_slice.to_vec();
        new_kids.insert(chosen_idx + 1, Value::Reference(right_ref));
        root_dict.insert(Name::from("Kids"), Value::Array(Array::from(new_kids)));
        stats.writes += 1;
        tx.write(EMBEDDED_TREE, &Value::Dict(root_dict), budget)?;
    }
    Ok(())
}

fn verify_and_collect_keys(version: &Version, budget: &Budget) -> Vec<Vec<u8>> {
    let root_obj = version.get(EMBEDDED_TREE, budget).unwrap().unwrap();
    let Value::Dict(root_dict) = &**root_obj.value() else {
        panic!("root dict");
    };
    assert!(
        root_dict.get("Limits").is_none(),
        "root name-tree node must not carry /Limits"
    );
    let mut keys = Vec::new();
    if let Some(Value::Array(names)) = root_dict.get("Names") {
        for pair in names.as_slice().chunks(2) {
            let Value::String(s) = &pair[0] else { panic!() };
            keys.push(s.as_bytes().to_vec());
        }
    } else if let Some(Value::Array(kids)) = root_dict.get("Kids") {
        for kid in kids.iter() {
            let Value::Reference(r) = kid else { panic!() };
            let kid_obj = version.get(r.number(), budget).unwrap().unwrap();
            let Value::Dict(kid_dict) = &**kid_obj.value() else {
                panic!()
            };
            let Some(Value::Array(names)) = kid_dict.get("Names") else {
                panic!()
            };
            let Some(Value::Array(limits)) = kid_dict.get("Limits") else {
                panic!("kid leaf missing /Limits");
            };
            assert_eq!(limits.get(0), names.get(0));
            assert_eq!(limits.get(1), names.get(names.len() - 2));
            for pair in names.as_slice().chunks(2) {
                let Value::String(s) = &pair[0] else { panic!() };
                keys.push(s.as_bytes().to_vec());
            }
        }
    }
    for win in keys.windows(2) {
        assert!(win[0] < win[1], "name tree keys must be strictly ascending");
    }
    keys
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

    // Precompute odd insertion keys scattered across `[1..512]` so multiple
    // leaves are touched and split within one batch.
    let insert_keys: Vec<Vec<u8>> = (0..ops)
        .map(|step| {
            let odd = 1 + ((step * 17) % 256) * 2 + if step >= 256 { 512 } else { 0 };
            // Guarantee uniqueness for ops <= 64 since gcd(17, 256) == 1.
            format!("item_{odd:05}.bin").into_bytes()
        })
        .collect();

    let doc_cur = build_attachments_doc(initial, Settlers::default());
    let lim_settler = Arc::new(NameLimitsSettler::default());
    let doc_a = build_attachments_doc(
        initial,
        Settlers::default().with(Structure::TreeLimits, lim_settler.clone()),
    );
    let doc_b = build_attachments_doc(initial, Settlers::default());

    let step_read = |tx: &mut Transaction, stats: &mut Stats| {
        if interleaved {
            // Matches `Open::view()` inside `_writable(self._document)` at the
            // start of every `doc.attachments.add(...)` call.
            tx.settle(Scope::Commit, &budget).unwrap();
            stats.reads += 1;
            let _ = tx.version().get(CATALOG, &budget).unwrap();
        }
    };

    let exec_cur = |doc: &Document| -> (Arc<Version>, Stats) {
        let mut stats = Stats::default();
        let mut tx = doc.begin();
        for key in &insert_keys {
            step_read(&mut tx, &mut stats);
            insert_attachment_current_flat(&mut tx, &budget, key, b"payload", &mut stats).unwrap();
        }
        let v = tx.commit(&budget).unwrap();
        (v, stats)
    };

    let exec_a = |doc: &Document| -> (Arc<Version>, Stats) {
        lim_settler.runs.store(0, Ordering::Relaxed);
        lim_settler.reads.store(0, Ordering::Relaxed);
        lim_settler.writes.store(0, Ordering::Relaxed);
        let mut stats = Stats::default();
        let mut tx = doc.begin();
        for key in &insert_keys {
            step_read(&mut tx, &mut stats);
            insert_attachment_option_a(&mut tx, &budget, key, b"payload", &mut stats).unwrap();
        }
        let v = tx.commit(&budget).unwrap();
        stats.settler_runs = lim_settler.runs.load(Ordering::Relaxed);
        stats.reads += lim_settler.reads.load(Ordering::Relaxed);
        stats.writes += lim_settler.writes.load(Ordering::Relaxed);
        (v, stats)
    };

    let exec_b = |doc: &Document| -> (Arc<Version>, Stats) {
        let mut stats = Stats::default();
        let mut tx = doc.begin();
        for key in &insert_keys {
            step_read(&mut tx, &mut stats);
            insert_attachment_option_b(&mut tx, &budget, key, b"payload", &mut stats).unwrap();
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

    let keys_cur = verify_and_collect_keys(&v_cur, &budget);
    let keys_a = verify_and_collect_keys(&v_a, &budget);
    let keys_b = verify_and_collect_keys(&v_b, &budget);
    assert_eq!(keys_cur, keys_a, "Option A name-tree keys mismatch");
    assert_eq!(keys_cur, keys_b, "Option B name-tree keys mismatch");
    assert_eq!(keys_b.len(), initial + ops);

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
            structure: "TreeLimits",
            workload: "attachment_inserts",
            size: initial,
            ops,
            mode,
            variant: "current_flat_rewrite",
            settler_runs: stats_cur.settler_runs,
            object_reads: stats_cur.reads,
            object_writes: stats_cur.writes,
            changed_objects: chg_cur,
            median_us: med_cur,
            p90_us: p90_cur,
        },
        BenchRow {
            structure: "TreeLimits",
            workload: "attachment_inserts",
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
            structure: "TreeLimits",
            workload: "attachment_inserts",
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
