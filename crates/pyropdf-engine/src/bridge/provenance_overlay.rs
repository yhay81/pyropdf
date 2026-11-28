//! Ephemeral bridge overlay that instruments a single page's reachable content
//! streams with collision-free marked-content wrappers (`BMC ... EMC`) around
//! painted operators, without mutating the store's [`Version`].
//!
//! Temporary DESIGN.md §6 workaround for upstream `hayro-interpret` provenance
//! (`I4`, yhay81/pyropdf#104, `LaurenzV/hayro#1742`), tracked in #7 and #1273.
//!
//! Removal condition: delete this module once pyropdf adopts a released
//! `hayro-interpret` containing `LaurenzV/hayro#1742` (#104).

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode, try_insert_map, try_insert_set};
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::PageIndex;
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::value::{Array, Bytes, Dict, Name, Reference, Stream, Value};
use pyropdf_store::version::Version;
use pyropdf_store::{Error as StoreError, ErrorKind, LimitExceeded};

use super::preflight::{MAX_REF_CHAIN, MAX_STREAM_DEPTH};
use super::{Bridged, Error as BridgeError, Result as BridgeResult};
use crate::content::{Operations, PageContent};

/// Default marked-content tag prefix when no reachable stream already uses it.
pub(crate) const BASE_PREFIX: &str = "pyropdf.prov.";

/// Annotation flags matching `hayro-interpret`: Hidden (bit 2) and NoView (bit 6).
const ANNOTATION_HIDDEN: u32 = 1 << 1;
const ANNOTATION_NO_VIEW: u32 = 1 << 5;

/// How often loops poll the budget for deadline / cancellation.
const POLL_INTERVAL: usize = 256;

/// Direct painted operators wrapped with `/<prefix>op.<stream>.<op> BMC ... EMC`.
const DIRECT_PAINT_OPS: [&[u8]; 13] = [
    b"Tj", b"TJ", b"'", b"\"", b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*",
];

/// Stream filter/external keys removed when replacing a stream body with
/// uncompressed instrumented bytes.
const STRIP_STREAM_KEYS: [&str; 6] = [
    "Filter",
    "DecodeParms",
    "F",
    "FFilter",
    "FDecodeParms",
    "DL",
];

/// Ephemeral bridged document whose target page has all reachable streams
/// instrumented for provenance tracking.
pub(crate) struct ProvenanceOverlay {
    /// Bridged view over `version` with ephemeral instrumented object stand-ins.
    pub(crate) bridged: Bridged,
    /// Collision-free tag prefix used in every wrapper on this page
    /// (e.g. `"pyropdf.prov."` or `"pyropdf.prov.1."`).
    pub(crate) prefix: String,
    /// Number of distinct instrumented stream objects built for this overlay.
    #[cfg(test)]
    pub(crate) instrumented_streams: usize,
}

/// Resolved PDF value together with its indirect reference (if any).
#[derive(Clone)]
struct Resolved {
    reference: Option<Reference>,
    value: Arc<Value>,
}

impl Resolved {
    fn value(&self) -> &Value {
        self.value.as_ref()
    }

    const fn obj_num(&self) -> Option<u32> {
        match self.reference {
            Some(r) => Some(r.number()),
            None => None,
        }
    }
}

/// A decoded and parsed Form XObject or annotation appearance stream, cached
/// once per object number.
struct ParsedStream {
    reference: Reference,
    stream: Stream,
    operations: Operations,
}

/// A visible annotation on the target page whose selected `/AP /N` stream will
/// receive a per-annotation root wrapper in the ephemeral overlay.
struct AnnotEntry {
    /// Annotation indirect reference, or `None` when stored directly in `/Annots`.
    annot_ref: Option<Reference>,
    /// Index of this annotation in the page's `/Annots` array.
    annot_index: usize,
    /// Resolved annotation dictionary.
    annot_dict: Dict,
    /// Resolved `/AP` dictionary.
    ap_dict: Dict,
    /// Resolved `/AP /N` state subdictionary and selected state key when `/AP /N`
    /// is a state dictionary; `None` when `/AP /N` is a direct stream reference.
    state_subdict: Option<(Dict, Name)>,
    /// Object number of the selected `/AP /N` appearance stream.
    ap_stream_obj: u32,
}

/// Iterative DFS frame for walking Form XObjects bounded by `MAX_STREAM_DEPTH`.
enum DfsFrame {
    Enter {
        reference: Reference,
        stream: Stream,
        inherited_resources: Option<Resolved>,
        inherited_res_id: u32,
        depth: usize,
        is_annot_root: bool,
    },
    Leave {
        obj_num: u32,
        key: (u32, u32),
        depth: usize,
    },
}

/// Builds an ephemeral [`ProvenanceOverlay`] for page `index` of `version`,
/// instrumenting page `/Contents`, reachable Form XObjects (up to
/// [`MAX_STREAM_DEPTH`], cycle-safe, deduplicated per object), and (when
/// `annotations` is set) visible annotation `/AP /N` appearance streams.
///
/// Never mutates `version`.
pub(crate) fn build_provenance_overlay(
    version: &Version,
    pages: &PageIndex,
    index: usize,
    annotations: bool,
    budget: &Budget,
) -> BridgeResult<ProvenanceOverlay> {
    let Some(page) = pages.pages().get(index) else {
        return Err(BridgeError::NoPage(index));
    };

    let mut reads = Reads::new(version, budget);
    let page_content = PageContent::read(&mut reads, page)?;
    let page_resources = match page.resources() {
        Some(res_val) => resolve(&mut reads, res_val)?,
        None => None,
    };
    let page_res_id = page_resources
        .as_ref()
        .and_then(Resolved::obj_num)
        .unwrap_or(0);

    let mut base_collided = false;
    let mut used_nonces: HashSet<u32> = HashSet::new();
    let mut form_streams: HashMap<u32, ParsedStream> = HashMap::new();
    let mut used_as_form: HashSet<u32> = HashSet::new();
    let mut active_forms: HashSet<u32> = HashSet::new();
    let mut checked_depth: HashMap<(u32, u32), usize> = HashMap::new();
    let mut dfs_stack: BudgetVec<'_, DfsFrame> = BudgetVec::new(budget);
    let mut annot_entries: BudgetVec<'_, AnnotEntry> = BudgetVec::new(budget);
    let mut raw_annots_array: Option<Array> = None;

    // 1. Scan page /Contents for prefix collisions and top-level `Do` Form targets.
    record_prefix_collisions(
        page_content.operations(),
        &mut base_collided,
        &mut used_nonces,
        budget,
    )?;
    if let Some(res) = &page_resources {
        enqueue_invoked_forms(
            &mut reads,
            page_content.operations(),
            res,
            page_res_id,
            1,
            &mut dfs_stack,
            budget,
        )?;
    }

    // 2. When annotations are requested, collect visible annotation /AP /N streams.
    if annotations
        && let Some(annots_val) = page.dict().get("Annots").cloned()
        && let Some(annots_res) = resolve(&mut reads, &annots_val)?
        && let Value::Array(items) = annots_res.value()
    {
        budget.check(LimitCode::AnnotationCount, items.len() as u64)?;
        let mut has_direct_annot = false;
        for (annot_index, item) in items.iter().enumerate() {
            budget.poll()?;
            let Some(annot_res) = resolve(&mut reads, item)? else {
                continue;
            };
            let Value::Dict(annot_dict) = annot_res.value() else {
                continue;
            };
            if !annotation_visible(&mut reads, annot_dict)? {
                continue;
            }
            let Some(SelectedAppearance {
                ap_dict,
                state_subdict,
                ap_ref,
                ap_stream,
            }) = select_annotation_appearance(&mut reads, annot_dict)?
            else {
                continue;
            };
            if annot_res.reference.is_none() {
                has_direct_annot = true;
            }
            let ap_stream_obj = ap_ref.number();
            annot_entries.try_push(AnnotEntry {
                annot_ref: annot_res.reference,
                annot_index,
                annot_dict: annot_dict.clone(),
                ap_dict,
                state_subdict,
                ap_stream_obj,
            })?;
            dfs_stack.try_push(DfsFrame::Enter {
                reference: ap_ref,
                stream: ap_stream,
                inherited_resources: page_resources.clone(),
                inherited_res_id: page_res_id,
                depth: 1,
                is_annot_root: true,
            })?;
        }
        if has_direct_annot {
            raw_annots_array = Some(items.clone());
        }
    }

    // 3. Iterative DFS over all reachable Form XObjects and annotation appearances.
    let mut steps = 0usize;
    while let Some(frame) = dfs_stack.pop() {
        steps = steps.wrapping_add(1);
        if steps.is_multiple_of(POLL_INTERVAL) {
            budget.poll()?;
        }
        match frame {
            DfsFrame::Leave {
                obj_num,
                key,
                depth,
            } => {
                active_forms.remove(&obj_num);
                let best = checked_depth.get(&key).copied().unwrap_or(0).max(depth);
                try_insert_map(&mut checked_depth, key, best, budget)?;
            }
            DfsFrame::Enter {
                reference,
                stream,
                inherited_resources,
                inherited_res_id,
                depth,
                is_annot_root,
            } => {
                if depth > MAX_STREAM_DEPTH {
                    return Err(BridgeError::Store(
                        StoreError::from(LimitExceeded::over(
                            LimitCode::ObjectDepth,
                            MAX_STREAM_DEPTH as u64,
                            depth as u64,
                        ))
                        .context("nested Form XObjects exceeded depth limit"),
                    ));
                }
                let obj_num = reference.number();
                if active_forms.contains(&obj_num) {
                    return Err(BridgeError::Store(
                        StoreError::from(LimitExceeded::new(LimitCode::ObjectDepth))
                            .context(format!("cyclic Form XObject reference at object {obj_num}")),
                    ));
                }
                if !is_annot_root {
                    try_insert_set(&mut used_as_form, obj_num, budget)?;
                }

                let own_res = match stream.dict().get("Resources") {
                    Some(res_val) => resolve(&mut reads, res_val)?,
                    None => None,
                };
                let (effective_res, res_id) = match own_res {
                    Some(own) => {
                        let id = own.obj_num().unwrap_or(obj_num);
                        (Some(own), id)
                    }
                    None => (inherited_resources, inherited_res_id),
                };
                let key = (obj_num, res_id);
                if checked_depth.get(&key).is_some_and(|&d| d >= depth) {
                    continue;
                }

                // Decode and parse each distinct stream object at most once.
                if !form_streams.contains_key(&obj_num) {
                    let decoded = match stream.decoded_in(version, budget) {
                        Ok(bytes) => bytes,
                        Err(err) if matches!(err.kind(), ErrorKind::Limit(_)) => {
                            return Err(BridgeError::Store(err));
                        }
                        Err(_) => continue,
                    };
                    let ops = match Operations::parse(Bytes::from(decoded), budget) {
                        Ok(ops) => ops,
                        Err(err) if matches!(err.kind(), ErrorKind::Limit(_)) => {
                            return Err(BridgeError::Store(err));
                        }
                        Err(_) => continue,
                    };
                    record_prefix_collisions(&ops, &mut base_collided, &mut used_nonces, budget)?;
                    try_insert_map(
                        &mut form_streams,
                        obj_num,
                        ParsedStream {
                            reference,
                            stream,
                            operations: ops,
                        },
                        budget,
                    )?;
                }

                try_insert_set(&mut active_forms, obj_num, budget)?;
                dfs_stack.try_push(DfsFrame::Leave {
                    obj_num,
                    key,
                    depth,
                })?;

                if let Some(res) = &effective_res
                    && let Some(parsed) = form_streams.get(&obj_num)
                {
                    enqueue_invoked_forms(
                        &mut reads,
                        &parsed.operations,
                        res,
                        res_id,
                        depth + 1,
                        &mut dfs_stack,
                        budget,
                    )?;
                }
            }
        }
    }

    // 4. Select a collision-free marked-content tag prefix.
    let prefix = choose_prefix(base_collided, &used_nonces, budget)?;

    // 5. Build ephemeral object overrides (`extra`) without mutating `version`.
    let mut next_obj = version.next_number();
    let mut extra: BudgetVec<'_, (Reference, Value)> = BudgetVec::new(budget);
    let mut instrumented_streams = 0usize;
    let mut page_override_dict: Option<(Reference, Dict)> = None;

    // 5a. Instrument page /Contents into a single ephemeral stream.
    if !page_content.operations().is_empty() {
        let wrapped_page = instrument_page_content(&page_content, &prefix, budget)?;
        if let Some(page_ref) = page.object() {
            let contents_ref = alloc_ephemeral(&mut next_obj)?;
            let contents_stream = Stream::new(Dict::new(), Bytes::from(wrapped_page));
            extra.try_push((contents_ref, Value::from(contents_stream)))?;
            let Value::Dict(mut page_dict) =
                copy_for_edit(&Value::Dict(page.dict().clone()), budget)?
            else {
                unreachable!("cloned page dict");
            };
            insert_const_key(&mut page_dict, "Contents", Value::Reference(contents_ref));
            page_override_dict = Some((page_ref, page_dict));
            instrumented_streams += 1;
        } else if let Some(&Value::Reference(stream_ref)) = page.dict().get("Contents")
            && let Some(obj) = version.get(stream_ref.number(), budget)?
            && let Value::Stream(orig_stream) = obj.value().as_ref()
        {
            let clean_dict = clean_stream_dict(orig_stream.dict(), budget)?;
            extra.try_push((
                Reference::new(stream_ref.number(), obj.generation()),
                Value::from(Stream::new(clean_dict, Bytes::from(wrapped_page))),
            ))?;
            instrumented_streams += 1;
        }
    }

    // 5b. Instrument each reachable Form XObject once (deduplicated by object number).
    let mut form_nums: BudgetVec<'_, u32> = BudgetVec::new(budget);
    form_nums.try_extend(used_as_form.iter().copied())?;
    form_nums.sort_unstable();
    let mut cached_bodies: HashMap<u32, Bytes> = HashMap::new();
    for &obj_num in form_nums.iter() {
        budget.poll()?;
        let Some(parsed) = form_streams.get(&obj_num) else {
            continue;
        };
        let wrapped = instrument_stream_ops(&parsed.operations, obj_num, &prefix, budget)?;
        let body = Bytes::from(wrapped);
        try_insert_map(&mut cached_bodies, obj_num, body.clone(), budget)?;
        let clean_dict = clean_stream_dict(parsed.stream.dict(), budget)?;
        extra.try_push((parsed.reference, Value::from(Stream::new(clean_dict, body))))?;
        instrumented_streams += 1;
    }

    // 5c. Instrument each visible annotation's selected /AP /N stream with its
    // own per-annotation root marker so shared appearance streams never collide.
    let mut overridden_annots: HashSet<u32> = HashSet::new();
    let mut updated_annots_vec: Option<BudgetVec<'_, Value>> = None;
    if let Some(arr) = raw_annots_array {
        let mut items = BudgetVec::new(budget);
        items.try_extend(arr.iter().cloned())?;
        updated_annots_vec = Some(items);
    }

    for entry in annot_entries.iter() {
        budget.poll()?;
        let Some(parsed) = form_streams.get(&entry.ap_stream_obj) else {
            continue;
        };
        let body = if let Some(existing) = cached_bodies.get(&entry.ap_stream_obj) {
            existing.clone()
        } else {
            let wrapped =
                instrument_stream_ops(&parsed.operations, entry.ap_stream_obj, &prefix, budget)?;
            let bytes = Bytes::from(wrapped);
            try_insert_map(
                &mut cached_bodies,
                entry.ap_stream_obj,
                bytes.clone(),
                budget,
            )?;
            bytes
        };

        let annot_obj = entry.annot_ref.map_or(0, |r| r.number());
        let wrapped_ap =
            wrap_annotation_root(&body, &prefix, annot_obj, entry.ap_stream_obj, budget)?;
        let ephem_ap_ref = alloc_ephemeral(&mut next_obj)?;
        let clean_dict = clean_stream_dict(parsed.stream.dict(), budget)?;
        extra.try_push((
            ephem_ap_ref,
            Value::from(Stream::new(clean_dict, Bytes::from(wrapped_ap))),
        ))?;
        instrumented_streams += 1;

        let Value::Dict(mut ap_dict) = copy_for_edit(&Value::Dict(entry.ap_dict.clone()), budget)?
        else {
            unreachable!("cloned AP dict");
        };
        if let Some((states, state_key)) = &entry.state_subdict {
            let Value::Dict(mut states_copy) = copy_for_edit(&Value::Dict(states.clone()), budget)?
            else {
                unreachable!("cloned AP /N states dict");
            };
            insert_single_key(
                &mut states_copy,
                state_key.clone(),
                Value::Reference(ephem_ap_ref),
            );
            insert_const_key(&mut ap_dict, "N", Value::Dict(states_copy));
        } else {
            insert_const_key(&mut ap_dict, "N", Value::Reference(ephem_ap_ref));
        }

        let Value::Dict(mut annot_copy) =
            copy_for_edit(&Value::Dict(entry.annot_dict.clone()), budget)?
        else {
            unreachable!("cloned annot dict");
        };
        insert_const_key(&mut annot_copy, "AP", Value::Dict(ap_dict));

        if let Some(annot_ref) = entry.annot_ref {
            if try_insert_set(&mut overridden_annots, annot_ref.number(), budget)? {
                extra.try_push((annot_ref, Value::Dict(annot_copy)))?;
            }
        } else if let Some(items) = &mut updated_annots_vec
            && let Some(slot) = items.get_mut(entry.annot_index)
        {
            *slot = Value::Dict(annot_copy);
        }
    }

    if let Some(items) = updated_annots_vec
        && let Some(page_ref) = page.object()
    {
        let (_, page_dict) = match &mut page_override_dict {
            Some(pair) => pair,
            None => {
                let Value::Dict(dict) = copy_for_edit(&Value::Dict(page.dict().clone()), budget)?
                else {
                    unreachable!("cloned page dict");
                };
                page_override_dict = Some((page_ref, dict));
                page_override_dict.as_mut().unwrap()
            }
        };
        insert_const_key(
            page_dict,
            "Annots",
            Value::Array(Array::from(items.into_vec())),
        );
    }

    if let Some((page_ref, page_dict)) = page_override_dict {
        extra.try_push((page_ref, Value::Dict(page_dict)))?;
    }

    // `bridge::update` requires `extra` sorted by unique object number.
    extra.sort_unstable_by_key(|(reference, _)| reference.number());
    dedup_extra(&mut extra);

    let bridged = Bridged::with_extra(version, pages, extra.as_slice(), budget)?;
    #[cfg(not(test))]
    let _ = instrumented_streams;
    Ok(ProvenanceOverlay {
        bridged,
        prefix,
        #[cfg(test)]
        instrumented_streams,
    })
}

/// Removes any accidental duplicate object numbers from sorted `extra`,
/// keeping the first entry for each object number.
fn dedup_extra(extra: &mut BudgetVec<'_, (Reference, Value)>) {
    if extra.len() < 2 {
        return;
    }
    let mut write = 1usize;
    for read in 1..extra.len() {
        if extra[read].0.number() != extra[write - 1].0.number() {
            if write != read {
                extra.swap(write, read);
            }
            write += 1;
        }
    }
    extra.truncate(write);
}

/// Allocates a fresh ephemeral object number below `u32::MAX` (leaving room for
/// the bridge cross-reference stream).
fn alloc_ephemeral(next: &mut u64) -> BridgeResult<Reference> {
    let number = u32::try_from(*next)
        .ok()
        .filter(|&n| n < u32::MAX)
        .ok_or_else(|| {
            BridgeError::Store(StoreError::malformed(
                "no object number remains for an ephemeral provenance stream",
            ))
        })?;
    *next += 1;
    Ok(Reference::new(number, 0))
}

/// Resolves an indirect reference chain up to [`MAX_REF_CHAIN`] hops,
/// propagating limit errors and treating malformed objects as absent (matching
/// `hayro`).
fn resolve(reads: &mut Reads<'_>, value: &Value) -> BridgeResult<Option<Resolved>> {
    let mut current = match value {
        &Value::Reference(r) => r,
        other => {
            return Ok(Some(Resolved {
                reference: None,
                value: Arc::new(other.clone()),
            }));
        }
    };
    let mut steps = 0usize;
    loop {
        steps += 1;
        if steps > MAX_REF_CHAIN {
            return Err(BridgeError::Store(
                StoreError::from(LimitExceeded::over(
                    LimitCode::ObjectDepth,
                    MAX_REF_CHAIN as u64,
                    steps as u64,
                ))
                .context("indirect reference chain too deep"),
            ));
        }
        let obj = match reads.get(current.number()) {
            Ok(Some(obj)) if obj.generation() == current.generation() => obj,
            Ok(_) => return Ok(None),
            Err(err) if matches!(err.kind(), ErrorKind::Limit(_)) => {
                return Err(BridgeError::Store(err));
            }
            Err(_) => return Ok(None),
        };
        if let &Value::Reference(next) = obj.value().as_ref() {
            current = next;
            continue;
        }
        return Ok(Some(Resolved {
            reference: Some(Reference::new(current.number(), obj.generation())),
            value: Arc::clone(obj.value()),
        }));
    }
}

/// Checks whether an annotation dictionary is visible and has a `/Rect` array
/// matching `hayro-interpret::interpret_annotation`.
fn annotation_visible(reads: &mut Reads<'_>, dict: &Dict) -> BridgeResult<bool> {
    if let Some(flags_val) = dict.get("F")
        && let Some(flags_res) = resolve(reads, flags_val)?
        && let Value::Integer(flags) = flags_res.value()
        && (*flags as u64) & u64::from(ANNOTATION_HIDDEN | ANNOTATION_NO_VIEW) != 0
    {
        return Ok(false);
    }
    let Some(rect_val) = dict.get("Rect") else {
        return Ok(false);
    };
    let Some(rect_res) = resolve(reads, rect_val)? else {
        return Ok(false);
    };
    let Value::Array(rect) = rect_res.value() else {
        return Ok(false);
    };
    Ok(rect.len() == 4)
}

/// Selected normal appearance stream (`/AP /N`) for one visible annotation.
struct SelectedAppearance {
    ap_dict: Dict,
    state_subdict: Option<(Dict, Name)>,
    ap_ref: Reference,
    ap_stream: Stream,
}

/// Selects the annotation's normal appearance stream (`/AP /N`), matching
/// `hayro-interpret::appearance_stream`: direct stream, or `/AS`-keyed state
/// subdictionary falling back to `/Off`.
fn select_annotation_appearance(
    reads: &mut Reads<'_>,
    annot_dict: &Dict,
) -> BridgeResult<Option<SelectedAppearance>> {
    let Some(ap_val) = annot_dict.get("AP") else {
        return Ok(None);
    };
    let Some(ap_res) = resolve(reads, ap_val)? else {
        return Ok(None);
    };
    let Value::Dict(ap_dict) = ap_res.value() else {
        return Ok(None);
    };
    let Some(n_val) = ap_dict.get("N") else {
        return Ok(None);
    };
    let Some(n_res) = resolve(reads, n_val)? else {
        return Ok(None);
    };
    match n_res.value() {
        Value::Stream(stream) => {
            let Some(ap_ref) = n_res.reference else {
                return Ok(None);
            };
            if stream.dict().get("BBox").is_none() {
                return Ok(None);
            }
            Ok(Some(SelectedAppearance {
                ap_dict: ap_dict.clone(),
                state_subdict: None,
                ap_ref,
                ap_stream: (**stream).clone(),
            }))
        }
        Value::Dict(states) => {
            let as_name = match annot_dict.get("AS") {
                Some(as_val) => match resolve(reads, as_val)? {
                    Some(res) => match res.value() {
                        Value::Name(name) => Some(name.clone()),
                        _ => None,
                    },
                    None => None,
                },
                None => None,
            };
            let selected = if let Some(state) = &as_name
                && let Some(val) = states.get(state.as_bytes())
                && let Some(res) = resolve(reads, val)?
                && let Value::Stream(stream) = res.value()
                && let Some(ap_ref) = res.reference
            {
                Some((state.clone(), ap_ref, (**stream).clone()))
            } else if let Some(val) = states.get("Off")
                && let Some(res) = resolve(reads, val)?
                && let Value::Stream(stream) = res.value()
                && let Some(ap_ref) = res.reference
            {
                Some((Name::from("Off"), ap_ref, (**stream).clone()))
            } else {
                None
            };
            let Some((state_key, ap_ref, ap_stream)) = selected else {
                return Ok(None);
            };
            if ap_stream.dict().get("BBox").is_none() {
                return Ok(None);
            }
            Ok(Some(SelectedAppearance {
                ap_dict: ap_dict.clone(),
                state_subdict: Some((states.clone(), state_key)),
                ap_ref,
                ap_stream,
            }))
        }
        _ => Ok(None),
    }
}

/// Scans `ops` for `Do` operators whose XObject names resolve to Form XObjects
/// in `resources /XObject`, pushing each distinct target onto `dfs_stack`.
fn enqueue_invoked_forms(
    reads: &mut Reads<'_>,
    ops: &Operations,
    resources: &Resolved,
    resources_id: u32,
    child_depth: usize,
    dfs_stack: &mut BudgetVec<'_, DfsFrame>,
    budget: &Budget,
) -> BridgeResult<()> {
    let Value::Dict(res_dict) = resources.value() else {
        return Ok(());
    };
    let Some(xobj_val) = res_dict.get("XObject") else {
        return Ok(());
    };
    let Some(xobj_res) = resolve(reads, xobj_val)? else {
        return Ok(());
    };
    let Value::Dict(xobj_dict) = xobj_res.value() else {
        return Ok(());
    };

    let mut seen_names: HashSet<&[u8]> = HashSet::new();
    for (idx, op) in ops.operations().iter().enumerate() {
        if idx.is_multiple_of(POLL_INTERVAL) {
            budget.poll()?;
        }
        if ops.operator(op) != b"Do" {
            continue;
        }
        let Some(Value::Name(name)) = ops.operands(op).last() else {
            continue;
        };
        if !try_insert_set(&mut seen_names, name.as_bytes(), budget)? {
            continue;
        }
        let Some(target_val) = xobj_dict.get(name.as_bytes()) else {
            continue;
        };
        let Some(target_res) = resolve(reads, target_val)? else {
            continue;
        };
        let Value::Stream(stream) = target_res.value() else {
            continue;
        };
        let Some(target_ref) = target_res.reference else {
            continue;
        };
        let is_form = match stream.dict().get("Subtype") {
            Some(subtype_val) => match resolve(reads, subtype_val)? {
                Some(res) => {
                    matches!(res.value(), Value::Name(n) if n.as_bytes() == b"Form")
                }
                None => false,
            },
            None => false,
        };
        if !is_form {
            continue;
        }
        dfs_stack.try_push(DfsFrame::Enter {
            reference: target_ref,
            stream: (**stream).clone(),
            inherited_resources: Some(resources.clone()),
            inherited_res_id: resources_id,
            depth: child_depth,
            is_annot_root: false,
        })?;
    }
    Ok(())
}

/// Records whether any `BMC`/`BDC` tag in `ops` collides with `BASE_PREFIX` or
/// any numbered fallback prefix `pyropdf.prov.<nonce>.`.
fn record_prefix_collisions(
    ops: &Operations,
    base_collided: &mut bool,
    used_nonces: &mut HashSet<u32>,
    budget: &Budget,
) -> Result<(), LimitExceeded> {
    let base = BASE_PREFIX.as_bytes();
    for (idx, op) in ops.operations().iter().enumerate() {
        if idx.is_multiple_of(POLL_INTERVAL) {
            budget.poll()?;
        }
        if !matches!(ops.operator(op), b"BMC" | b"BDC") {
            continue;
        }
        for operand in ops.operands(op) {
            let Value::Name(tag) = operand else {
                continue;
            };
            let Some(rest) = tag.as_bytes().strip_prefix(base) else {
                continue;
            };
            *base_collided = true;
            if let Some(dot) = rest.iter().position(|&b| b == b'.')
                && dot > 0
                && let Ok(digits) = std::str::from_utf8(&rest[..dot])
                && let Ok(nonce) = digits.parse::<u32>()
            {
                try_insert_set(used_nonces, nonce, budget)?;
            }
        }
    }
    Ok(())
}

/// Picks `BASE_PREFIX` when collision-free, or the first unused
/// `pyropdf.prov.<nonce>.` (`nonce >= 1`).
fn choose_prefix(
    base_collided: bool,
    used_nonces: &HashSet<u32>,
    budget: &Budget,
) -> Result<String, LimitExceeded> {
    if !base_collided {
        let mut bytes = BudgetVec::new(budget);
        bytes.try_extend_from_slice(BASE_PREFIX.as_bytes())?;
        return Ok(String::from_utf8(bytes.into_vec()).expect("ASCII base prefix"));
    }
    let mut nonce = 1u32;
    while used_nonces.contains(&nonce) {
        if nonce.is_multiple_of(POLL_INTERVAL as u32) {
            budget.poll()?;
        }
        nonce = nonce
            .checked_add(1)
            .ok_or_else(|| LimitExceeded::new(LimitCode::MemorySize))?;
    }
    let mut bytes = BudgetVec::new(budget);
    bytes.try_extend_from_slice(BASE_PREFIX.as_bytes())?;
    bytes.try_extend_from_slice(nonce.to_string().as_bytes())?;
    bytes.try_push(b'.')?;
    Ok(String::from_utf8(bytes.into_vec()).expect("ASCII numbered prefix"))
}

/// Instruments a page's joined [`PageContent`], encoding each painted
/// operator's original stream object number (via [`PageContent::stream_of`])
/// and page-level joined operation index.
fn instrument_page_content(
    content: &PageContent,
    prefix: &str,
    budget: &Budget,
) -> Result<Vec<u8>, LimitExceeded> {
    instrument_operations(
        content.operations(),
        |op| content.stream_of(op).map_or(0, |r| r.number()),
        prefix,
        budget,
    )
}

/// Instruments one Form XObject or annotation appearance stream whose object
/// number is `stream_obj`.
fn instrument_stream_ops(
    ops: &Operations,
    stream_obj: u32,
    prefix: &str,
    budget: &Budget,
) -> Result<Vec<u8>, LimitExceeded> {
    instrument_operations(ops, |_| stream_obj, prefix, budget)
}

/// Rewrites `ops` so every painted operator (`Tj`, `TJ`, `'`, `"`, `S`, `s`,
/// `f`, `F`, `f*`, `B`, `B*`, `b`, `b*`, `Do`, `BI..EI`, `sh`) is wrapped in
/// collision-free marked content (`/<prefix>... BMC\n<op>\nEMC\n`).
fn instrument_operations(
    ops: &Operations,
    stream_of: impl Fn(&crate::content::Operation) -> u32,
    prefix: &str,
    budget: &Budget,
) -> Result<Vec<u8>, LimitExceeded> {
    let data = ops.data();
    let mut out = BudgetVec::new(budget);
    out.try_reserve(data.len())?;
    let mut copied = 0usize;
    let prefix_bytes = prefix.as_bytes();

    for (index, op) in ops.operations().iter().enumerate() {
        if index.is_multiple_of(POLL_INTERVAL) {
            budget.poll()?;
        }
        let operator = ops.operator(op);
        let wrapper_kind = if DIRECT_PAINT_OPS.contains(&operator) {
            Some(WrapperKind::Op)
        } else if operator == b"BI" {
            Some(WrapperKind::InlineImage)
        } else if operator == b"Do" {
            Some(WrapperKind::Do)
        } else if operator == b"sh" {
            Some(WrapperKind::Shading)
        } else {
            None
        };
        let Some(kind) = wrapper_kind else {
            continue;
        };

        let span = op.span();
        out.try_extend_from_slice(&data[copied..span.start])?;
        out.try_extend_from_slice(b"\n/")?;
        out.try_extend_from_slice(prefix_bytes)?;
        let stream_obj = stream_of(op);
        match kind {
            WrapperKind::Op => {
                out.try_extend_from_slice(b"op.")?;
                push_u32(&mut out, stream_obj)?;
                out.try_push(b'.')?;
                push_usize(&mut out, index)?;
            }
            WrapperKind::InlineImage => {
                out.try_extend_from_slice(b"bi.")?;
                push_u32(&mut out, stream_obj)?;
                out.try_push(b'.')?;
                push_usize(&mut out, index)?;
            }
            WrapperKind::Do => {
                out.try_extend_from_slice(b"do.")?;
                push_u32(&mut out, stream_obj)?;
                out.try_push(b'.')?;
                push_usize(&mut out, index)?;
                out.try_push(b'.')?;
                if let Some(Value::Name(name)) = ops.operands(op).last() {
                    push_hex(&mut out, name.as_bytes())?;
                }
            }
            WrapperKind::Shading => {
                out.try_extend_from_slice(b"sh.")?;
                push_u32(&mut out, stream_obj)?;
                out.try_push(b'.')?;
                push_usize(&mut out, index)?;
                out.try_push(b'.')?;
                if let Some(Value::Name(name)) = ops.operands(op).last() {
                    push_hex(&mut out, name.as_bytes())?;
                }
            }
        }
        out.try_extend_from_slice(b" BMC\n")?;
        out.try_extend_from_slice(&data[span.clone()])?;
        out.try_extend_from_slice(b"\nEMC\n")?;
        copied = span.end;
    }

    out.try_extend_from_slice(&data[copied..])?;
    Ok(out.into_vec())
}

enum WrapperKind {
    Op,
    InlineImage,
    Do,
    Shading,
}

/// Wraps an already-instrumented annotation `/AP /N` body with its
/// per-annotation root marked-content frame.
fn wrap_annotation_root(
    body: &[u8],
    prefix: &str,
    annot_obj: u32,
    ap_stream_obj: u32,
    budget: &Budget,
) -> Result<Vec<u8>, LimitExceeded> {
    let mut out = BudgetVec::new(budget);
    out.try_reserve(body.len().saturating_add(prefix.len() + 48))?;
    out.try_extend_from_slice(b"/")?;
    out.try_extend_from_slice(prefix.as_bytes())?;
    out.try_extend_from_slice(b"annot.")?;
    push_u32(&mut out, annot_obj)?;
    out.try_push(b'.')?;
    push_u32(&mut out, ap_stream_obj)?;
    out.try_extend_from_slice(b" BMC\n")?;
    out.try_extend_from_slice(body)?;
    out.try_extend_from_slice(b"\nEMC\n")?;
    Ok(out.into_vec())
}

/// Clones `dict` via `copy_for_edit` and strips stream filter / external-file
/// keys so `Stream::new` holds uncompressed bytes directly.
fn clean_stream_dict(dict: &Dict, budget: &Budget) -> BridgeResult<Dict> {
    let Value::Dict(mut clean) = copy_for_edit(&Value::Dict(dict.clone()), budget)? else {
        unreachable!("cloned stream dict");
    };
    for key in STRIP_STREAM_KEYS {
        clean.remove(key);
    }
    Ok(clean)
}

fn push_u32(out: &mut BudgetVec<'_, u8>, value: u32) -> Result<(), LimitExceeded> {
    out.try_extend_from_slice(value.to_string().as_bytes())
}

fn push_usize(out: &mut BudgetVec<'_, u8>, value: usize) -> Result<(), LimitExceeded> {
    out.try_extend_from_slice(value.to_string().as_bytes())
}

fn push_hex(out: &mut BudgetVec<'_, u8>, bytes: &[u8]) -> Result<(), LimitExceeded> {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    out.try_reserve(bytes.len().saturating_mul(2))?;
    for &byte in bytes {
        out.try_push(HEX[usize::from(byte >> 4)])?;
        out.try_push(HEX[usize::from(byte & 0x0f)])?;
    }
    Ok(())
}

fn insert_const_key(dict: &mut Dict, key: &'static str, value: Value) {
    #[expect(
        clippy::disallowed_methods,
        reason = "replaces or adds one constant key on an ephemeral overlay dictionary"
    )]
    dict.insert(Name::from(key), value);
}

fn insert_single_key(dict: &mut Dict, key: Name, value: Value) {
    #[expect(
        clippy::disallowed_methods,
        reason = "replaces one existing state key on an ephemeral /AP /N state dictionary"
    )]
    dict.insert(key, value);
}
