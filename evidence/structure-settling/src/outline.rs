//! Outline `/Count` benchmark and Option A / Option B prototypes (#765).

use std::collections::HashSet;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use pyropdf_engine::navigation::{
    self, NamedDestinations, Outline, Spec, outline_children, outline_entries, outline_set_open,
    outline_write,
};
use pyropdf_store::Result;
use pyropdf_store::budget::Budget;
use pyropdf_store::memo::Memo;
use pyropdf_store::pages::{PageIndex, PageTree};
use pyropdf_store::refs;
use pyropdf_store::settle::{Scope, Settler, Settlers, Structure};
use pyropdf_store::value::{Bytes, Dict, Name, Reference, Value};
use pyropdf_store::version::{Document, MemoryBase, Transaction, Version};

use crate::{BenchRow, Stats, time_with_undo};

const CHAPTERS: usize = 64;

fn empty_pdf_with_one_page() -> Arc<MemoryBase> {
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
        Bytes::new(&b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>"[..]),
    );
    Arc::new(base)
}

/// Builds a 3-level outline of `total` items (`CHAPTERS` top-level items,
/// each with open level-2 sections and level-3 subsections) using the
/// production `navigation::outline_write` implementation.
fn build_outline_doc(total: usize, settlers: Settlers) -> (Document, Vec<(u32, u16)>, u32) {
    assert!(total >= CHAPTERS * 2 && total.is_multiple_of(CHAPTERS));
    let doc = Document::new(empty_pdf_with_one_page(), None);
    doc.set_settlers(settlers);
    let budget = Budget::default();
    let per_chapter = total / CHAPTERS;
    let mut specs: Vec<Spec> = Vec::with_capacity(total);
    let target = ("goto".to_owned(), Some(0), None, None, None, None);
    for ch in 0..CHAPTERS {
        specs.push((
            1,
            format!("Chapter {}", ch + 1),
            target.clone(),
            None,
            0,
            true,
        ));
        let rest = per_chapter - 1;
        let sections = (rest / 2).max(1);
        let mut remaining = rest;
        for sec in 0..sections {
            if remaining == 0 {
                break;
            }
            specs.push((
                2,
                format!("Section {}.{}", ch + 1, sec + 1),
                target.clone(),
                None,
                0,
                true,
            ));
            remaining -= 1;
            if remaining > 0 && (sec + 1 < sections || remaining == 1) {
                specs.push((
                    3,
                    format!("Subsection {}.{}.1", ch + 1, sec + 1),
                    target.clone(),
                    None,
                    0,
                    false,
                ));
                remaining -= 1;
            }
        }
        while remaining > 0 {
            specs.push((
                3,
                format!("Detail {}.{}", ch + 1, remaining),
                target.clone(),
                None,
                0,
                false,
            ));
            remaining -= 1;
        }
    }
    assert_eq!(specs.len(), total);

    let pages = Memo::<PageTree, PageIndex>::new(1);
    let refs_cache = refs::Cache::new();
    let mut tx = doc.begin();
    outline_write(&mut tx, &pages, &refs_cache, &budget, specs).expect("outline_write");
    let committed = tx.commit(&budget).expect("commit initial outline");

    let chapters = outline_children(&committed, &budget, None).expect("outline_children");
    assert_eq!(chapters.len(), CHAPTERS);
    let catalog = committed.get(1, &budget).unwrap().unwrap();
    let Value::Dict(cat_dict) = &**catalog.value() else {
        panic!("catalog must be dict");
    };
    let Some(&Value::Reference(outlines_ref)) = cat_dict.get("Outlines") else {
        panic!("Outlines reference missing");
    };
    (doc, chapters, outlines_ref.number())
}

/// Option A prototype: `Settler` registered on `Structure::OutlineCount`.
/// Walks the outline rooted at each marked `/Outlines` dictionary once per
/// settle pass and updates any stale `/Count` entries.
#[derive(Default)]
struct OutlineCountSettler {
    runs: AtomicUsize,
    reads: AtomicUsize,
    writes: AtomicUsize,
}

impl Settler for OutlineCountSettler {
    fn settle(&self, tx: &mut Transaction, numbers: &[u32], budget: &Budget) -> Result<()> {
        self.runs.fetch_add(1, Ordering::Relaxed);
        for &root_num in numbers {
            self.reads.fetch_add(1, Ordering::Relaxed);
            let Some(root_obj) = tx.version().get(root_num, budget)? else {
                continue;
            };
            let Value::Dict(root_dict) = &**root_obj.value() else {
                continue;
            };
            let mut order: Vec<(u32, Option<usize>, bool, i64)> = Vec::new();
            let mut stack: Vec<(Reference, Option<usize>, usize)> = Vec::new();
            let mut visited: HashSet<u32> = HashSet::new();
            visited.insert(root_num);
            if let Some(&Value::Reference(first)) = root_dict.get("First") {
                stack.push((first, None, 1));
            }
            while let Some((reference, parent, depth)) = stack.pop() {
                if depth > navigation::MAX_DEPTH || !visited.insert(reference.number()) {
                    continue;
                }
                self.reads.fetch_add(1, Ordering::Relaxed);
                let Some(obj) = tx.version().get(reference.number(), budget)? else {
                    continue;
                };
                let Value::Dict(dict) = &**obj.value() else {
                    continue;
                };
                let existing = match dict.get("Count") {
                    Some(&Value::Integer(c)) => c,
                    _ => 0,
                };
                let is_open = existing > 0;
                let idx = order.len();
                if let Some(&Value::Reference(next)) = dict.get("Next") {
                    stack.push((next, parent, depth));
                }
                if let Some(&Value::Reference(first)) = dict.get("First") {
                    stack.push((first, Some(idx), depth + 1));
                }
                order.push((reference.number(), parent, is_open, existing));
            }
            let mut visible = vec![0_i64; order.len()];
            let mut has_children = vec![false; order.len()];
            let mut root_visible = 0_i64;
            for at in (0..order.len()).rev() {
                let (_, parent, is_open, _) = order[at];
                let shown = 1 + if is_open { visible[at] } else { 0 };
                match parent {
                    Some(owner) => {
                        visible[owner] += shown;
                        has_children[owner] = true;
                    }
                    None => root_visible += shown,
                }
            }
            for (at, &(num, _, is_open, existing)) in order.iter().enumerate() {
                if !has_children[at] {
                    continue;
                }
                let target = if is_open { visible[at] } else { -visible[at] };
                if existing != target {
                    self.reads.fetch_add(1, Ordering::Relaxed);
                    let obj = tx.version().get(num, budget)?.unwrap();
                    let Value::Dict(mut dict) = refs::copy_for_edit(obj.value(), budget)? else {
                        continue;
                    };
                    dict.insert(Name::from("Count"), Value::Integer(target));
                    self.writes.fetch_add(1, Ordering::Relaxed);
                    tx.write(num, &Value::Dict(dict), budget)?;
                }
            }
            let existing_root = match root_dict.get("Count") {
                Some(&Value::Integer(c)) => Some(c),
                _ => None,
            };
            if existing_root != Some(root_visible) {
                self.reads.fetch_add(1, Ordering::Relaxed);
                let root_obj = tx.version().get(root_num, budget)?.unwrap();
                let Value::Dict(mut dict) = refs::copy_for_edit(root_obj.value(), budget)? else {
                    continue;
                };
                dict.insert(Name::from("Count"), Value::Integer(root_visible));
                self.writes.fetch_add(1, Ordering::Relaxed);
                tx.write(root_num, &Value::Dict(dict), budget)?;
            }
        }
        Ok(())
    }
}

/// Option A setter: validates `has_children` on the item, flips its `/Count`
/// sign so open/closed state is recorded on the item, and marks
/// `Structure::OutlineCount` on `outlines_root`.
fn set_open_option_a(
    tx: &mut Transaction,
    budget: &Budget,
    outlines_root: u32,
    id: (u32, u16),
    open: bool,
    stats: &mut Stats,
) -> Result<()> {
    stats.reads += 1;
    let obj = tx.version().get(id.0, budget)?.expect("item exists");
    let Value::Dict(mut dict) = refs::copy_for_edit(obj.value(), budget)? else {
        panic!("item must be dict");
    };
    assert!(dict.get("First").is_some(), "item must have children");
    let mag = match dict.get("Count") {
        Some(&Value::Integer(c)) if c != 0 => c.abs(),
        _ => 1,
    };
    let signed = if open { mag } else { -mag };
    if dict.get("Count") != Some(&Value::Integer(signed)) {
        dict.insert(Name::from("Count"), Value::Integer(signed));
        stats.writes += 1;
        tx.write(id.0, &Value::Dict(dict), budget)?;
    }
    tx.mark(Structure::OutlineCount, outlines_root, budget)?;
    Ok(())
}

/// Option B prototype: immediate `/Parent`-chain delta update in O(depth).
/// Walks only from `id` up `/Parent` to `/Outlines` root, stopping upward
/// delta propagation as soon as a closed ancestor (`Count < 0`) is updated.
fn set_open_option_b(
    tx: &mut Transaction,
    budget: &Budget,
    id: (u32, u16),
    open: bool,
    stats: &mut Stats,
) -> Result<()> {
    stats.reads += 1;
    let obj = tx.version().get(id.0, budget)?.expect("item exists");
    let Value::Dict(mut dict) = refs::copy_for_edit(obj.value(), budget)? else {
        panic!("item must be dict");
    };
    assert!(dict.get("First").is_some(), "item must have children");
    let Some(&Value::Integer(old_count)) = dict.get("Count") else {
        outline_set_open(tx, budget, id, open).expect("fallback outline_set_open");
        return Ok(());
    };
    if old_count == 0 || (old_count > 0) == open {
        if old_count == 0 {
            outline_set_open(tx, budget, id, open).expect("fallback outline_set_open");
        }
        return Ok(());
    }
    let vis = old_count.abs();
    let new_count = if open { vis } else { -vis };
    let delta = if open { vis } else { -vis };
    let mut parent_ref = match dict.get("Parent") {
        Some(&Value::Reference(r)) => Some(r),
        _ => None,
    };
    dict.insert(Name::from("Count"), Value::Integer(new_count));
    stats.writes += 1;
    tx.write(id.0, &Value::Dict(dict), budget)?;

    let mut seen = HashSet::new();
    seen.insert(id.0);
    let mut steps = 0_usize;
    while let Some(anc) = parent_ref {
        steps += 1;
        assert!(steps <= navigation::MAX_DEPTH && seen.insert(anc.number()));
        stats.reads += 1;
        let anc_obj = tx
            .version()
            .get(anc.number(), budget)?
            .expect("ancestor exists");
        let Value::Dict(mut anc_dict) = refs::copy_for_edit(anc_obj.value(), budget)? else {
            break;
        };
        let is_root = matches!(
            anc_dict.get("Type"),
            Some(Value::Name(n)) if n.as_bytes() == b"Outlines"
        ) || anc_dict.get("Parent").is_none();
        let Some(&Value::Integer(anc_count)) = anc_dict.get("Count") else {
            break;
        };
        if is_root {
            anc_dict.insert(Name::from("Count"), Value::Integer(anc_count + delta));
            stats.writes += 1;
            tx.write(anc.number(), &Value::Dict(anc_dict), budget)?;
            break;
        }
        parent_ref = match anc_dict.get("Parent") {
            Some(&Value::Reference(r)) => Some(r),
            _ => None,
        };
        if anc_count > 0 {
            anc_dict.insert(Name::from("Count"), Value::Integer(anc_count + delta));
            stats.writes += 1;
            tx.write(anc.number(), &Value::Dict(anc_dict), budget)?;
        } else {
            // Closed ancestor: its negative magnitude changes by `delta`, while
            // its visible contribution above stays 1 + 0 = 1.
            let anc_vis = anc_count.abs() + delta;
            anc_dict.insert(Name::from("Count"), Value::Integer(-anc_vis));
            stats.writes += 1;
            tx.write(anc.number(), &Value::Dict(anc_dict), budget)?;
            break;
        }
    }
    Ok(())
}

fn root_count(version: &Version, root_num: u32, budget: &Budget) -> i64 {
    let obj = version.get(root_num, budget).unwrap().unwrap();
    let Value::Dict(d) = &**obj.value() else {
        panic!("root dict");
    };
    match d.get("Count") {
        Some(&Value::Integer(c)) => c,
        other => panic!("unexpected root /Count: {other:?}"),
    }
}

pub fn run_all() -> Vec<BenchRow> {
    let mut rows = Vec::new();
    for &total in &[512_usize, 2048] {
        for &ops in &[1_usize, 16, 64] {
            for &interleaved in &[false, true] {
                rows.extend(run_case(total, ops, interleaved));
            }
        }
    }
    rows
}

fn run_case(total: usize, ops: usize, interleaved: bool) -> Vec<BenchRow> {
    let budget = Budget::default();
    let mode = if interleaved { "interleaved" } else { "pure" };

    // 1. Verify exact equivalence of committed states across all three variants.
    let (doc_cur, chapters, root_num) = build_outline_doc(total, Settlers::default());

    let settler = Arc::new(OutlineCountSettler::default());
    let (doc_a, _, _) = build_outline_doc(
        total,
        Settlers::default().with(Structure::OutlineCount, settler.clone()),
    );
    let (doc_b, _, _) = build_outline_doc(total, Settlers::default());

    let run_cur = |doc: &Document| -> (Arc<Version>, Stats) {
        let mut stats = Stats::default();
        let mut tx = doc.begin();
        for &id in &chapters[..ops] {
            if interleaved {
                tx.settle(Scope::Commit, &budget).unwrap();
                let kids = outline_children(tx.version(), &budget, Some(id)).unwrap();
                assert!(!kids.is_empty());
                stats.reads += 1 + kids.len();
            }
            // `outline_set_open` walks the whole `total`-item outline + root + catalog
            // and writes each changed `/Count` (here `id` and `Outlines` root).
            stats.reads += total + 3;
            stats.writes += 2;
            outline_set_open(&mut tx, &budget, id, false).unwrap();
        }
        let v = tx.commit(&budget).unwrap();
        (v, stats)
    };

    let run_a = |doc: &Document, counter: &OutlineCountSettler| -> (Arc<Version>, Stats) {
        counter.runs.store(0, Ordering::Relaxed);
        counter.reads.store(0, Ordering::Relaxed);
        counter.writes.store(0, Ordering::Relaxed);
        let mut stats = Stats::default();
        let mut tx = doc.begin();
        for &id in &chapters[..ops] {
            if interleaved {
                tx.settle(Scope::Commit, &budget).unwrap();
                let kids = outline_children(tx.version(), &budget, Some(id)).unwrap();
                assert!(!kids.is_empty());
                stats.reads += 1 + kids.len();
            }
            set_open_option_a(&mut tx, &budget, root_num, id, false, &mut stats).unwrap();
        }
        let v = tx.commit(&budget).unwrap();
        stats.settler_runs = counter.runs.load(Ordering::Relaxed);
        stats.reads += counter.reads.load(Ordering::Relaxed);
        stats.writes += counter.writes.load(Ordering::Relaxed);
        (v, stats)
    };

    let run_b = |doc: &Document| -> (Arc<Version>, Stats) {
        let mut stats = Stats::default();
        let mut tx = doc.begin();
        for &id in &chapters[..ops] {
            if interleaved {
                tx.settle(Scope::Commit, &budget).unwrap();
                let kids = outline_children(tx.version(), &budget, Some(id)).unwrap();
                assert!(!kids.is_empty());
                stats.reads += 1 + kids.len();
            }
            set_open_option_b(&mut tx, &budget, id, false, &mut stats).unwrap();
        }
        let v = tx.commit(&budget).unwrap();
        (v, stats)
    };

    let parent_cur = doc_cur.current();
    let (v_cur, stats_cur) = run_cur(&doc_cur);
    let chg_cur = parent_cur
        .changes(&v_cur, &budget)
        .unwrap()
        .unwrap()
        .objects()
        .len();
    doc_cur.undo().unwrap();

    let parent_a = doc_a.current();
    let (v_a, stats_a) = run_a(&doc_a, &settler);
    let chg_a = parent_a
        .changes(&v_a, &budget)
        .unwrap()
        .unwrap()
        .objects()
        .len();
    doc_a.undo().unwrap();

    let parent_b = doc_b.current();
    let (v_b, stats_b) = run_b(&doc_b);
    let chg_b = parent_b
        .changes(&v_b, &budget)
        .unwrap()
        .unwrap()
        .objects()
        .len();
    doc_b.undo().unwrap();

    let entries_cur = outline_entries(
        &Memo::<(), Outline>::new(1),
        &Memo::<PageTree, PageIndex>::new(1),
        &Memo::<(), NamedDestinations>::new(1),
        &v_cur,
        &budget,
    )
    .unwrap();
    let entries_a = outline_entries(
        &Memo::<(), Outline>::new(1),
        &Memo::<PageTree, PageIndex>::new(1),
        &Memo::<(), NamedDestinations>::new(1),
        &v_a,
        &budget,
    )
    .unwrap();
    let entries_b = outline_entries(
        &Memo::<(), Outline>::new(1),
        &Memo::<PageTree, PageIndex>::new(1),
        &Memo::<(), NamedDestinations>::new(1),
        &v_b,
        &budget,
    )
    .unwrap();
    assert_eq!(entries_cur, entries_a, "Option A outline entries mismatch");
    assert_eq!(entries_cur, entries_b, "Option B outline entries mismatch");
    assert_eq!(
        root_count(&v_cur, root_num, &budget),
        root_count(&v_a, root_num, &budget)
    );
    assert_eq!(
        root_count(&v_cur, root_num, &budget),
        root_count(&v_b, root_num, &budget)
    );

    let samples = if total >= 2048 && ops >= 64 { 15 } else { 31 };
    let (med_cur, p90_cur) = time_with_undo(samples, &doc_cur, |d| {
        let _ = run_cur(d);
    });
    let (med_a, p90_a) = time_with_undo(samples, &doc_a, |d| {
        let _ = run_a(d, &settler);
    });
    let (med_b, p90_b) = time_with_undo(samples, &doc_b, |d| {
        let _ = run_b(d);
    });

    vec![
        BenchRow {
            structure: "OutlineCount",
            workload: "outline_toggle_open",
            size: total,
            ops,
            mode,
            variant: "current_eager_full_walk",
            settler_runs: stats_cur.settler_runs,
            object_reads: stats_cur.reads,
            object_writes: stats_cur.writes,
            changed_objects: chg_cur,
            median_us: med_cur,
            p90_us: p90_cur,
        },
        BenchRow {
            structure: "OutlineCount",
            workload: "outline_toggle_open",
            size: total,
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
            structure: "OutlineCount",
            workload: "outline_toggle_open",
            size: total,
            ops,
            mode,
            variant: "option_b_parent_delta",
            settler_runs: stats_b.settler_runs,
            object_reads: stats_b.reads,
            object_writes: stats_b.writes,
            changed_objects: chg_b,
            median_us: med_b,
            p90_us: p90_b,
        },
    ]
}
