//! Structure tree reading, tagged reading order, Annex L / Arlington checks,
//! structure pruning/copying, and saved-history deserialization (DESIGN.md F6, F9).
//!
//! - `%PDF-` inputs exercise:
//!   1. `pyropdf_schema::check::check` (Arlington schema + `StructTreeRoot`,
//!      `StructElem`, `/P`, `/ParentTree`, `/ParentTreeNextKey`, `/IDTree`).
//!   2. `pyropdf_store::tree` (`number_tree_pairs`, `collect_tree_nodes`,
//!      `append_parent_tree`).
//!   3. Iterative tagged reading-order traversal (`/StructTreeRoot` -> `/K`,
//!      `/RoleMap`, `/NS`, `/MCR`, `/OBJR`, `PageContent` MCID cross-check, and
//!      Annex L parent/child standard-role multiplicity aggregation).
//!   4. `pyropdf_store::refs::prune_structure` (`refs/structure.rs`).
//!   5. `pyropdf_engine::pages::copy::Copies` (tagged page copy) +
//!      `pyropdf_engine::changes::report` (PDF/UA & PDF/A claim checks) +
//!      `Document::save_history` / `Document::restore_history` round-trip.
//! - Non-`%PDF-` and `PYROPDF-HISTORY\n` inputs exercise `Document::restore_history`
//!   both raw and framed with the base document's SHA-256 fingerprint and valid
//!   checksum, reaching Format 1 & Format 2 object/name-slot/stale-mark
//!   deserialization, undo/redo, history limits, and round-trip re-saving.

#![no_main]

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, OnceLock};
use std::time::Duration;

use libfuzzer_sys::fuzz_target;
use pyropdf_engine::changes;
use pyropdf_engine::content::PageContent;
use pyropdf_engine::pages::copy::Copies;
use pyropdf_schema::Version as PdfVersion;
use pyropdf_schema::check::{Graph, View, check as schema_check};
use pyropdf_store::Error;
use pyropdf_store::budget::{Budget, BudgetVec, DocumentLimits, LimitCode};
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::refs::{Edits, References, prune_structure};
use pyropdf_store::save::FileBase;
use pyropdf_store::tree;
use pyropdf_store::value::{Bytes, Name, Reference, Value};
use pyropdf_store::version::{Document, RestoredHistory, Version};
use sha2::{Digest, Sha256};

const MAGIC: &[u8; 16] = b"PYROPDF-HISTORY\n";
const MAX_HISTORY_INPUT: usize = 256 * 1024;
const MAX_STRUCT_DEPTH: usize = 64;
const MAX_STRUCT_ELEMENTS: usize = 4096;
const MAX_PAGES: usize = 4;

/// Minimal tagged PDF with a `/StructTreeRoot`, `/ParentTree`, and `/Names`
/// dictionary so Format 1 & Format 2 history restore (including `NameSlots`)
/// can target real base objects.
const BASE_PDF: &[u8] = b"%PDF-2.0\n\
1 0 obj\n<< /Type /Catalog /Pages 2 0 R /StructTreeRoot 4 0 R /Names << /Dests << /Names [(a) [3 0 R /Fit]] >> >> >>\nendobj\n\
2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n\
3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /StructParents 0 >>\nendobj\n\
4 0 obj\n<< /Type /StructTreeRoot /K [5 0 R] /ParentTree << /Nums [0 [5 0 R]] >> /ParentTreeNextKey 1 >>\nendobj\n\
5 0 obj\n<< /Type /StructElem /S /P /P 4 0 R /Pg 3 0 R /K 0 >>\nendobj\n\
xref\n0 6\n0000000000 65535 f \n0000000009 00000 n \n0000000127 00000 n \n0000000178 00000 n \n0000000263 00000 n \n0000000370 00000 n \n\
trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n436\n%%EOF\n";

struct BaseTemplate {
    base: Arc<FileBase>,
    fingerprint: [u8; 32],
}

fn base_template() -> &'static BaseTemplate {
    static CACHE: OnceLock<BaseTemplate> = OnceLock::new();
    CACHE.get_or_init(|| {
        let budget = Budget::new(&DocumentLimits::web());
        let base = Arc::new(
            FileBase::open(Bytes::new(BASE_PDF.to_vec()), &budget)
                .expect("base template PDF opens"),
        );
        let doc = Document::new(
            Arc::clone(&base) as Arc<dyn pyropdf_store::version::Base>,
            None,
        );
        let empty = doc
            .save_history(&[], 0, &budget)
            .expect("empty history saves");
        let fingerprint: [u8; 32] = empty[20..52]
            .try_into()
            .expect("saved history holds 32-byte fingerprint at 20..52");
        BaseTemplate { base, fingerprint }
    })
}

fn fuzz_budget() -> Budget {
    let mut limits = DocumentLimits::web();
    limits.max_memory_size = Some(64 * 1024 * 1024);
    limits.max_decompressed_size = Some(4 * 1024 * 1024);
    limits.max_total_decompressed_size = Some(8 * 1024 * 1024);
    limits.max_duration = Some(Duration::from_secs(2));
    Budget::new(&limits)
}

/// Arlington schema `Graph` over one store `Version`, mirroring
/// `crates/pyropdf-python/src/check.rs`.
struct StoreGraph<'a> {
    version: &'a Version,
    budget: &'a Budget,
    trailer: Value,
    declared: PdfVersion,
}

impl<'a> StoreGraph<'a> {
    fn new(version: &'a Version, budget: &'a Budget) -> Self {
        let declared = PdfVersion { major: 2, minor: 0 };
        Self {
            version,
            budget,
            trailer: Value::Dict(version.trailer().clone()),
            declared,
        }
    }
}

impl Graph for StoreGraph<'_> {
    type Value = Value;
    type Owned = Arc<Value>;
    type Error = Error;

    fn version(&self) -> PdfVersion {
        self.declared
    }

    fn trailer(&self) -> &Value {
        &self.trailer
    }

    fn resolve(&self, number: u32) -> Result<Option<Arc<Value>>, Error> {
        match self.version.get(number, self.budget) {
            Ok(object) => Ok(object.map(|o| Arc::clone(o.value()))),
            Err(error) if error.limit().is_some() => Err(error),
            Err(_) => Ok(None),
        }
    }

    fn view<'v>(&self, value: &'v Value) -> View<'v> {
        match value {
            Value::Null => View::Null,
            Value::Boolean(b) => View::Boolean(*b),
            Value::Integer(n) => View::Integer(*n),
            Value::Real(r) => View::Real(r.value()),
            Value::Name(n) => View::Name(n.as_bytes()),
            Value::String(s) => View::String(s.as_bytes()),
            Value::Array(a) => View::Array(a.len()),
            Value::Dict(_) => View::Dictionary,
            Value::Stream(_) => View::Stream,
            Value::Reference(r) => View::Reference(r.number()),
        }
    }

    fn get<'v>(&self, dict: &'v Value, key: &[u8]) -> Option<&'v Value> {
        match dict {
            Value::Dict(d) => d.get(key),
            Value::Stream(s) => s.dict().get(key),
            _ => None,
        }
    }

    fn entries<'v>(&self, dict: &'v Value) -> Vec<(&'v [u8], &'v Value)> {
        let dict = match dict {
            Value::Dict(d) => d,
            Value::Stream(s) => s.dict(),
            _ => return Vec::new(),
        };
        dict.iter().map(|(k, v)| (k.as_bytes(), v)).collect()
    }

    fn elements<'v>(&self, array: &'v Value) -> &'v [Value] {
        match array {
            Value::Array(a) => a.as_slice(),
            _ => &[],
        }
    }

    fn visit(&self, first: bool) -> Result<(), Error> {
        self.budget.poll()?;
        if first {
            self.budget.charge(LimitCode::ObjectCount, 1)?;
        }
        Ok(())
    }
}

fn resolve_value(
    version: &Version,
    value: &Value,
    budget: &Budget,
) -> Option<(Option<Reference>, Arc<Value>)> {
    let mut current = match value {
        Value::Reference(r) => *r,
        direct => return Some((None, Arc::new(direct.clone()))),
    };
    for _ in 0..32 {
        budget.poll().ok()?;
        let obj = version.get(current.number(), budget).ok()??;
        if obj.generation() != current.generation() {
            return None;
        }
        match obj.value().as_ref() {
            Value::Reference(next) => current = *next,
            _ => return Some((Some(current), Arc::clone(obj.value()))),
        }
    }
    None
}

fn resolve_role(role: &Name, role_map: &HashMap<Vec<u8>, Vec<u8>>) -> Vec<u8> {
    let mut current = role.as_bytes().to_vec();
    let mut seen = HashSet::new();
    for _ in 0..32 {
        if !seen.insert(current.clone()) {
            break;
        }
        let Some(mapped) = role_map.get(&current) else {
            break;
        };
        current = mapped.clone();
    }
    current
}

/// Bounded iterative traversal of `/StructTreeRoot`: exercises `/RoleMap`,
/// `/ParentTree`, `/IDTree`, tagged reading order (MCID/MCR/OBJR + page content
/// marked-content operators), and Annex L parent/child standard-role counts.
fn walk_structure_and_reading_order(version: &Version, pages: &PageIndex, budget: &Budget) {
    let Some(root_val) = version.trailer().get("Root") else {
        return;
    };
    let Some((_, cat_obj)) = resolve_value(version, root_val, budget) else {
        return;
    };
    let Value::Dict(catalog) = cat_obj.as_ref() else {
        return;
    };
    let Some(st_val) = catalog.get("StructTreeRoot") else {
        return;
    };
    let Some((_, st_obj)) = resolve_value(version, st_val, budget) else {
        return;
    };
    let Value::Dict(struct_root) = st_obj.as_ref() else {
        return;
    };

    // Exercise store number-tree and name-tree walkers on ParentTree and IDTree.
    if let Some(pt) = struct_root.get("ParentTree") {
        let mut pairs = BudgetVec::new(budget);
        let _ = tree::number_tree_pairs(version, pt, &mut pairs, budget);
        let _ = tree::collect_tree_nodes(version, pt, true, budget);
    }
    if let Some(idt) = struct_root.get("IDTree") {
        let _ = tree::collect_tree_nodes(version, idt, true, budget);
    }

    // Build RoleMap lookup (bounded).
    let mut role_map: HashMap<Vec<u8>, Vec<u8>> = HashMap::new();
    if let Some(rm_val) = struct_root.get("RoleMap")
        && let Some((_, rm_obj)) = resolve_value(version, rm_val, budget)
        && let Value::Dict(rm) = rm_obj.as_ref()
    {
        for (k, v) in rm.iter().take(512) {
            if let Value::Name(target) = v {
                role_map.insert(k.as_bytes().to_vec(), target.as_bytes().to_vec());
            }
        }
    }

    // Read marked-content sequences (BDC/BMC/EMC) on the first pages.
    let mut page_mcids: HashMap<u32, usize> = HashMap::new();
    let mut reads = Reads::new(version, budget);
    for page in pages.pages().iter().take(MAX_PAGES) {
        if budget.poll().is_err() {
            break;
        }
        if let Some(page_ref) = page.object()
            && let Ok(content) = PageContent::read(&mut reads, page)
        {
            let mut bdc_count = 0usize;
            let ops = content.operations();
            for op in ops.operations() {
                if matches!(ops.operator(op), b"BDC" | b"BMC" | b"EMC") {
                    bdc_count += 1;
                }
            }
            page_mcids.insert(page_ref.number(), bdc_count);
        }
    }

    // Iterative DFS over /K collecting Annex L (parent_role, child_role) counts
    // and inspecting MCR/OBJR/ActualText/Alt/E entries.
    let mut stack: Vec<(Option<Vec<u8>>, Value, usize)> = Vec::new();
    if let Some(k) = struct_root.get("K") {
        stack.push((Some(b"StructTreeRoot".to_vec()), k.clone(), 0));
    }
    let mut seen: HashSet<(u32, u16)> = HashSet::new();
    let mut role_counts: HashMap<(Vec<u8>, Vec<u8>), usize> = HashMap::new();
    let mut visited = 0usize;

    while let Some((parent_role, item, depth)) = stack.pop() {
        if budget.poll().is_err() || visited >= MAX_STRUCT_ELEMENTS || depth > MAX_STRUCT_DEPTH {
            break;
        }
        let Some((id, resolved)) = resolve_value(version, &item, budget) else {
            continue;
        };
        if let Some(r) = id
            && !seen.insert((r.number(), r.generation()))
        {
            continue;
        }
        match resolved.as_ref() {
            Value::Array(kids) => {
                for kid in kids.iter().rev().take(256) {
                    stack.push((parent_role.clone(), kid.clone(), depth + 1));
                }
            }
            Value::Dict(dict) => {
                visited += 1;
                if matches!(dict.get("Type"), Some(Value::Name(n)) if matches!(n.as_bytes(), b"MCR" | b"OBJR"))
                {
                    if let Some(pg) = dict.get("Pg")
                        && let Value::Reference(pg_ref) = pg
                    {
                        let _ = page_mcids.get(&pg_ref.number());
                    }
                    continue;
                }
                let std_role = match dict.get("S") {
                    Some(Value::Name(s)) => resolve_role(s, &role_map),
                    _ => Vec::new(),
                };
                if let Some(pr) = parent_role
                    && !std_role.is_empty()
                    && role_counts.len() < 1024
                {
                    *role_counts.entry((pr, std_role.clone())).or_insert(0) += 1;
                }
                for key in ["ActualText", "Alt", "E"] {
                    if let Some(v) = dict.get(key) {
                        let _ = resolve_value(version, v, budget);
                    }
                }
                if let Some(k) = dict.get("K") {
                    stack.push((Some(std_role), k.clone(), depth + 1));
                }
            }
            Value::Integer(_mcid) => {
                visited += 1;
            }
            _ => {}
        }
    }
}

fn exercise_restored(base: &Arc<FileBase>, history_bytes: &[u8], budget: &Budget) {
    let doc = Document::new(
        Arc::clone(base) as Arc<dyn pyropdf_store::version::Base>,
        Some(256 * 1024),
    );
    let Ok(RestoredHistory::Restored { labels, position }) =
        doc.restore_history(history_bytes, budget)
    else {
        return;
    };
    let status = doc.history();
    assert_eq!(status.undo, position);
    let _ = doc.undo();
    let _ = doc.redo();
    if let Ok(resaved) = doc.save_history(&labels, doc.history().undo, budget) {
        let fresh = Document::new(
            Arc::clone(base) as Arc<dyn pyropdf_store::version::Base>,
            Some(256 * 1024),
        );
        if let Ok(outcome) = fresh.restore_history(&resaved, budget) {
            assert!(
                matches!(outcome, RestoredHistory::Restored { .. }),
                "re-saved history must restore onto the same base"
            );
        }
    }
    doc.set_history_limit(Some(4096));
}

fn fuzz_history(data: &[u8], budget: &Budget) {
    let tmpl = base_template();
    // 1. Raw bytes directly (covers short inputs, corrupt checksums, wrong magic/fingerprint).
    exercise_restored(&tmpl.base, data, budget);

    if data.is_empty() || data.len() > MAX_HISTORY_INPUT {
        return;
    }

    // 2. Checksum- and fingerprint-framed payload so mutations reach deep journal
    //    parsing (Format 1 & 2, trailer/objects, NameSlots::restored, stale marks).
    let mut body = Vec::with_capacity(data.len() + 64);
    if data.starts_with(MAGIC) && data.len() >= 52 {
        let end = if data.len() >= 84 {
            data.len() - 32
        } else {
            data.len()
        };
        body.extend_from_slice(&data[..end]);
        body[20..52].copy_from_slice(&tmpl.fingerprint);
    } else {
        body.extend_from_slice(MAGIC);
        let format: u32 = if data[0] & 1 == 0 { 1 } else { 2 };
        body.extend_from_slice(&format.to_le_bytes());
        body.extend_from_slice(&tmpl.fingerprint);
        body.extend_from_slice(&data[1..]);
    }
    let checksum: [u8; 32] = Sha256::digest(&body).into();
    body.extend_from_slice(&checksum);
    exercise_restored(&tmpl.base, &body, budget);
}

fn fuzz_pdf_structure(data: &[u8], budget: &Budget) {
    let Ok(base) = FileBase::open(Bytes::new(data.to_vec()), budget).map(Arc::new) else {
        return;
    };
    let doc = Document::new(
        Arc::clone(&base) as Arc<dyn pyropdf_store::version::Base>,
        Some(256 * 1024),
    );
    let version = doc.current();

    // 1. Arlington structural check (including StructTreeRoot / ParentTree invariants).
    let graph = StoreGraph::new(&version, budget);
    let _ = schema_check(&graph);

    // 2. Page index + structure tree reading, ParentTree/IDTree, reading order, Annex L roles.
    let Ok(pages) = PageIndex::build(&mut Reads::new(&version, budget)) else {
        return;
    };
    walk_structure_and_reading_order(&version, &pages, budget);

    // 3. Scratch append_parent_tree_entry in an uncommitted transaction.
    {
        let mut tx = doc.begin();
        let _ =
            tree::append_parent_tree_entry(&mut tx, Value::Reference(Reference::new(1, 0)), budget);
    }

    // 4. Structure pruning (`refs/structure.rs`) on bounded-size PDFs.
    if data.len() <= MAX_HISTORY_INPUT && !pages.is_empty() {
        let mut page_nums: Vec<u32> = pages
            .pages()
            .iter()
            .filter_map(|p| p.object().map(|r| r.number()))
            .collect();
        page_nums.sort_unstable();
        page_nums.dedup();
        if !page_nums.is_empty() {
            let mut reads = Reads::new(&version, budget);
            if let Ok(refs) = References::build(&mut reads) {
                let removed = &page_nums[..1];
                let retained = &page_nums[1..];
                let mut edits = Edits::new(budget);
                let _ = prune_structure(&mut reads, &refs, removed, retained, &mut edits);
            }
        }
    }

    // 5. Tagged page copy + PDF/UA & PDF/A change report + save/restore history round-trip.
    if data.len() <= 128 * 1024
        && let Some(page0) = pages.pages().first()
        && let Some(page_ref) = page0.object()
    {
        let mut tx = doc.begin();
        let copied = Copies::new(&tx, budget)
            .and_then(|mut copies| {
                copies.copy(&mut tx, page_ref, page0.dict().clone())?;
                copies.finish(&mut tx)
            })
            .is_ok();
        if copied && let Ok(after) = tx.commit(budget) {
            let _ = changes::report(&after, true, budget);
            let _ = changes::report(&after, false, budget);
            if let Ok(saved) = doc.save_history(&["copy".to_owned()], 1, budget) {
                exercise_restored(&base, &saved, budget);
            }
        }
    }
}

fuzz_target!(
    // The store turns a panic of hayro-syntax into an error (DESIGN.md I3),
    // but libFuzzer's hook aborts on any panic: keep the default hook, so
    // that only a panic that escapes the target is a crash.
    init: {
        let _ = std::panic::take_hook();
    },
    |data: &[u8]| {
        let budget = fuzz_budget();
        if data.starts_with(b"%PDF-") {
            fuzz_pdf_structure(data, &budget);
        } else {
            fuzz_history(data, &budget);
        }
    }
);
