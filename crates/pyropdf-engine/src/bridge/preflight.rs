//! Temporary iterative pre-flight nesting and inline-image guard before `hayro`
//! (DESIGN.md §6 contingency for yhay81/pyropdf#15, #740, and #1242; tracked in #7 and #1241).
//!
//! Validates a page's reachable PDF object graph and content streams through
//! `pyropdf-store` (`Reads` + `Budget` + `BudgetVec` + `try_insert_map` /
//! `try_insert_set`) *before* handing the page to `hayro` / `hayro-syntax`,
//! closing every known uncatchable `SIGSEGV` stack-overflow, cycle, silent
//! truncation, and exponential inline-image lookahead path in pinned `hayro`:
//!
//! - Color space nesting and cycles (`/Indexed`, `/Separation`, `/DeviceN`,
//!   `/Pattern`, `/ICCBased /Alternate`) — bounded at [`MAX_COLOR_SPACE_DEPTH`] (32),
//!   matching upstream `LaurenzV/hayro#1735`.
//! - Function nesting and cycles (Type 3 `/Functions` stitching chains) — bounded
//!   at [`MAX_FUNCTION_DEPTH`] (32), matching upstream `LaurenzV/hayro#1665`.
//! - Type 4 PostScript procedure `{ ... }` brace nesting — scanned iteratively
//!   over decoded stream bytes and bounded at [`MAX_TYPE4_BRACES`] (256), matching
//!   Acrobat and `LaurenzV/hayro#1665`.
//! - Nested interpreted streams (Form XObjects, annotation `/AP /N`, SoftMask `/G`,
//!   Type 1 Tiling Patterns, Type 3 `/CharProcs`) — iterative DFS with cycle
//!   refusal and [`MAX_STREAM_DEPTH`] (50), covering `hayro`'s silent truncation
//!   of Forms/SoftMasks and unbounded recursion in `TilingPattern::interpret`
//!   and `Type3::render_glyph` (#1242).
//! - Image XObject `/ColorSpace` (or `/CS`), `/SMask`, `/Mask`, and page `/Thumb`
//!   color-space chains and cycles.
//! - Content stream operand container nesting (`[`/`]`, `<<`/`>>`) — bounded at
//!   `min(max_object_depth, MAX_CONTENT_OPERAND_DEPTH)`, below `hayro-syntax`'s
//!   512 silent-truncation cap (`LaurenzV/hayro#1666`).
//! - Inline image (`BI ... ID ... EI`) nested lookahead chains (#740,
//!   `LaurenzV/hayro#1832`) — detected in a single linear byte scan before
//!   `hayro-syntax::content::UntypedIter` runs.
//!
//! Active whenever `DocumentLimits.max_object_depth` is set (active by default
//! in `DocumentLimits.web()`, opt-out when `max_object_depth` is `None`).
//! Memoized per immutable `Version` in [`PreflightCache`] via F4 `Memo` so
//! repeated operations on an unedited page pay ~0 ms.
//!
//! Removal condition (tracked in yhay81/pyropdf#7): delete this module once
//! pyropdf adopts a released `hayro` version containing bounded CS/Function/Type 4
//! construction with error signals (#15), non-nested inline-image end lookahead
//! (#740), and `MAX_NESTED_INTERPRETATION_DEPTH` enforcement in
//! `TilingPattern::interpret` and `Type3::render_glyph` (#1242).

use std::collections::{HashMap, HashSet};
use std::ops::Range;
use std::sync::Arc;

use pyropdf_store::budget::{
    Budget, BudgetVec, LimitCode, StreamKind, try_insert_map, try_insert_set,
};
use pyropdf_store::memo::{Memo, Reads, Stats};
use pyropdf_store::pages::{Page, PageIndex};
use pyropdf_store::value::{Array, Dict, Reference, Stream, Value};
use pyropdf_store::version::Version;
use pyropdf_store::{Error, ErrorKind, LimitExceeded, Result};

/// Matches upstream `LaurenzV/hayro#1735`.
pub const MAX_COLOR_SPACE_DEPTH: usize = 32;
/// Matches upstream `LaurenzV/hayro#1665` (stitching functions).
pub const MAX_FUNCTION_DEPTH: usize = 32;
/// Matches Acrobat and upstream `LaurenzV/hayro#1665` (Type 4 procedures).
pub const MAX_TYPE4_BRACES: usize = 256;
/// Matches `hayro-interpret`'s `MAX_NESTED_INTERPRETATION_DEPTH`.
pub const MAX_STREAM_DEPTH: usize = 50;
/// Matches `DocumentLimits.web().max_object_depth` (below hayro-syntax's 512).
pub const MAX_CONTENT_OPERAND_DEPTH: usize = 64;
/// Maximum indirect reference chain length resolved in a single lookup.
pub const MAX_REF_CHAIN: usize = 16;
/// Maximum `/Parent` links followed while recording a page's F4 read set.
const MAX_PARENT_CHAIN: usize = 64;
/// Maximum inline images in one stream that can trigger `hayro-syntax`'s
/// recursive `starts_with_content` lookahead (#740: `2^8 = 256` is negligible,
/// `2^22` takes ~2 s, `200,000` overflows the stack).
pub const MAX_INLINE_LOOKAHEAD_CHAIN: usize = 8;
/// Default F4 memo capacity in pages (covers `DocumentLimits.web().max_pages` = 200).
pub const DEFAULT_CACHE_CAPACITY: usize = 256;
/// How often graph loops poll the budget for deadline / cancellation.
const POLL_INTERVAL: usize = 256;
/// How often byte scanners poll the budget for deadline / cancellation.
const BYTE_POLL_INTERVAL: usize = 8192;
/// Operators that terminate `hayro-syntax`'s `starts_with_content` lookahead early.
const LOOKAHEAD_OPERATOR_LIMIT: usize = 20;

/// Returns whether the pre-flight nesting guard is active for `budget`.
///
/// Active whenever `DocumentLimits.max_object_depth` is set (`Some(_)`), and
/// opted out when it is `None`.
#[must_use]
pub const fn is_active(budget: &Budget) -> bool {
    budget.limit(LimitCode::ObjectDepth).is_some()
}

/// Returns the effective content operand nesting bound when the guard is active,
/// or `None` when opted out.
#[must_use]
pub fn content_operand_limit(budget: &Budget) -> Option<usize> {
    budget.limit(LimitCode::ObjectDepth).map(|limit| {
        usize::try_from(limit)
            .unwrap_or(usize::MAX)
            .min(MAX_CONTENT_OPERAND_DEPTH)
    })
}

/// Measurements collected while validating a page.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Report {
    pub streams_scanned: u64,
    pub content_bytes_scanned: u64,
    pub type4_streams_scanned: u64,
    pub type4_bytes_scanned: u64,
    pub objects_visited: u64,
    pub max_cs_depth: usize,
    pub max_fn_depth: usize,
    pub max_type4_braces: usize,
    pub max_stream_depth: usize,
    pub max_content_depth: usize,
    pub inline_images_seen: u64,
    pub max_inline_lookahead_chain: usize,
}

/// Reusable depth-aware validated-object memo across pages of one immutable `Version`.
///
/// Each entry records the *maximum* entry depth at which the indirect object's
/// entire reachable subgraph has already been proven safe (never a boolean
/// visited set). Re-visiting an object at `depth <= recorded` is guaranteed
/// safe; re-visiting at `depth > recorded` re-walks the subgraph so shared DAG
/// nodes cannot bypass depth bounds.
#[derive(Default)]
pub struct CheckedCache {
    color_spaces: HashMap<u32, usize>,
    functions: HashMap<u32, usize>,
    streams: HashMap<u32, usize>,
    resources: HashMap<u32, usize>,
}

impl CheckedCache {
    /// Returns an empty depth-aware cache.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Merges validated local depth maps into this cache after a page passes.
    fn merge_from(
        &mut self,
        cs: &HashMap<u32, usize>,
        fns: &HashMap<u32, usize>,
        streams: &HashMap<u32, usize>,
        resources: &HashMap<u32, usize>,
        budget: &Budget,
    ) -> Result<()> {
        merge_depth_map(&mut self.color_spaces, cs, budget)?;
        merge_depth_map(&mut self.functions, fns, budget)?;
        merge_depth_map(&mut self.streams, streams, budget)?;
        merge_depth_map(&mut self.resources, resources, budget)?;
        Ok(())
    }
}

fn merge_depth_map(
    target: &mut HashMap<u32, usize>,
    source: &HashMap<u32, usize>,
    budget: &Budget,
) -> Result<()> {
    for (&num, &depth) in source {
        let best = target.get(&num).copied().unwrap_or(0).max(depth);
        if target.get(&num).copied() != Some(best) {
            try_insert_map(target, num, best, budget)?;
        }
    }
    Ok(())
}

/// Memo key for F4 per-page pre-flight validation.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
struct PreflightKey {
    page: Reference,
    content_operand_limit: usize,
}

/// F4-memoized per-page pre-flight cache (DESIGN.md §5 and §6).
///
/// Holds weak references to versions and the exact `ReadSet` of every object
/// consulted while validating each page, so repeated operations on an unedited
/// page pay ~0 ms and any edit to a consulted object invalidates only affected
/// pages.
pub struct PreflightCache {
    memo: Memo<PreflightKey, ()>,
}

impl Default for PreflightCache {
    fn default() -> Self {
        Self::new(DEFAULT_CACHE_CAPACITY)
    }
}

impl PreflightCache {
    /// Returns an empty cache holding at most `capacity` validated pages.
    #[must_use]
    pub fn new(capacity: usize) -> Self {
        Self {
            memo: Memo::new(capacity),
        }
    }

    /// Validates `page` on `version` when `budget` bounds `ObjectDepth`,
    /// reusing any valid F4 memo entry.
    pub fn validate_page(
        &self,
        version: &Arc<Version>,
        page: &Page,
        budget: &Budget,
    ) -> Result<()> {
        let Some(limit) = content_operand_limit(budget) else {
            return Ok(());
        };
        if let Some(reference) = page.object() {
            let key = PreflightKey {
                page: reference,
                content_operand_limit: limit,
            };
            self.memo
                .get_or_compute(&key, version, budget, |reads| {
                    validate_page(reads, page, None).map(|_| ())
                })
                .map(|_| ())
        } else {
            let mut reads = Reads::new(version, budget);
            validate_page(&mut reads, page, None).map(|_| ())
        }
    }

    /// Validates each page in `indexes` of `pages` on `version` when `budget`
    /// bounds `ObjectDepth`, deduplicating via the F4 memo.
    pub fn validate_pages(
        &self,
        version: &Arc<Version>,
        pages: &PageIndex,
        indexes: &[usize],
        budget: &Budget,
    ) -> Result<()> {
        if !is_active(budget) {
            return Ok(());
        }
        for &at in indexes {
            budget.poll()?;
            if let Some(page) = pages.get(at) {
                self.validate_page(version, page, budget)?;
            }
        }
        Ok(())
    }

    /// Returns memo lookup statistics (hits and computations), for tests.
    #[must_use]
    pub fn stats(&self) -> Stats {
        self.memo.stats()
    }

    /// Drops every cached validation result.
    pub fn clear(&self) {
        self.memo.clear();
    }
}

/// Validates raw decoded content stream bytes iteratively before
/// `hayro-syntax::content::UntypedIter` runs.
///
/// Active only when `budget` bounds [`LimitCode::ObjectDepth`]. Refuses
/// operand container nesting beyond `min(max_object_depth, MAX_CONTENT_OPERAND_DEPTH)`
/// and `#740` nested inline-image lookahead chains with `LimitCode::ObjectDepth`.
pub fn validate_content_bytes(data: &[u8], budget: &Budget) -> Result<()> {
    let Some(operand_limit) = content_operand_limit(budget) else {
        return Ok(());
    };
    let mut report = Report::default();
    scan_content_bytes(data, operand_limit, budget, &mut report, None)
}

/// Validates one `Page` iteratively under `reads` (recording the full F4
/// read-set and charging F6 `Budget`).
///
/// Active only when `reads.budget()` bounds [`LimitCode::ObjectDepth`]. When
/// `shared` is `Some`, indirect objects already proven safe at `>= depth` on
/// earlier pages of the same immutable `Version` are skipped in O(1), and new
/// proofs are flushed into `shared` only if the page passes completely.
pub fn validate_page(
    reads: &mut Reads<'_>,
    page: &Page,
    shared: Option<&mut CheckedCache>,
) -> Result<Report> {
    let Some(operand_limit) = content_operand_limit(reads.budget()) else {
        return Ok(Report::default());
    };
    let (report, local_cs, local_fn, local_streams, local_resources) = {
        let mut guard = PageGuard {
            reads,
            shared: shared.as_deref(),
            operand_limit,
            local_cs: HashMap::new(),
            local_fn: HashMap::new(),
            local_streams: HashMap::new(),
            local_resources: HashMap::new(),
            report: Report::default(),
            steps: 0,
        };
        guard.check_page(page)?;
        (
            guard.report,
            guard.local_cs,
            guard.local_fn,
            guard.local_streams,
            guard.local_resources,
        )
    };
    if let Some(cache) = shared {
        cache.merge_from(
            &local_cs,
            &local_fn,
            &local_streams,
            &local_resources,
            reads.budget(),
        )?;
    }
    Ok(report)
}

/// Constructs a `LimitCode::ObjectDepth` refusal with a clear context message
/// and numeric `(limit, observed)` bounds.
fn depth_refusal(context: &str, limit: usize, observed: usize) -> Error {
    Error::from(LimitExceeded::over(
        LimitCode::ObjectDepth,
        limit as u64,
        observed as u64,
    ))
    .context(context)
}

/// Constructs a `LimitCode::ObjectDepth` refusal for a cycle (no numeric bound).
fn cycle_refusal(context: String) -> Error {
    Error::from(LimitExceeded::new(LimitCode::ObjectDepth)).context(context)
}

/// Resolved PDF value together with its indirect object number (if any).
#[derive(Clone)]
enum Resolved {
    Direct(Value),
    Indirect(u32, Arc<Value>),
}

impl Resolved {
    fn value(&self) -> &Value {
        match self {
            Self::Direct(v) => v,
            Self::Indirect(_, v) => v.as_ref(),
        }
    }

    const fn obj_num(&self) -> Option<u32> {
        match self {
            Self::Direct(_) => None,
            Self::Indirect(n, _) => Some(*n),
        }
    }
}

/// Reads and records object `number` through `reads`. Limit refusals propagate;
/// malformed unparseable objects are skipped (as `hayro` skips them).
fn read_object(reads: &mut Reads<'_>, number: u32) -> Result<Option<Arc<Value>>> {
    match reads.get(number) {
        Ok(Some(obj)) => Ok(Some(Arc::clone(obj.value()))),
        Ok(None) => Ok(None),
        Err(err) if matches!(err.kind(), ErrorKind::Limit(_)) => Err(err),
        Err(_) => Ok(None),
    }
}

fn resolve(reads: &mut Reads<'_>, value: &Value) -> Result<Option<Resolved>> {
    let mut current = match value {
        Value::Reference(r) => r.number(),
        other => return Ok(Some(Resolved::Direct(other.clone()))),
    };
    let mut steps = 0usize;
    loop {
        steps += 1;
        if steps > MAX_REF_CHAIN {
            return Err(depth_refusal(
                "indirect reference chain too deep",
                MAX_REF_CHAIN,
                steps,
            ));
        }
        let Some(val) = read_object(reads, current)? else {
            return Ok(None);
        };
        if let Value::Reference(next) = val.as_ref() {
            current = next.number();
            continue;
        }
        return Ok(Some(Resolved::Indirect(current, val)));
    }
}

/// Records any indirect references inside a stream's `/Filter` or `/DecodeParms`
/// in `reads` so F4 memoization tracks them before `Stream::decoded_in` runs.
fn record_stream_filter_refs(reads: &mut Reads<'_>, stream: &Stream) -> Result<()> {
    for key in ["Filter", "DecodeParms"] {
        if let Some(val) = stream.dict().get(key) {
            record_value_refs_shallow(reads, val)?;
        }
    }
    Ok(())
}

fn record_value_refs_shallow(reads: &mut Reads<'_>, value: &Value) -> Result<()> {
    match value {
        Value::Reference(r) => {
            if let Some(resolved) = resolve(reads, &Value::Reference(*r))?
                && let Value::Array(arr) = resolved.value()
            {
                for item in arr.iter() {
                    if matches!(item, Value::Reference(_)) {
                        let _ = resolve(reads, item)?;
                    }
                }
            }
        }
        Value::Array(arr) => {
            for item in arr.iter() {
                if matches!(item, Value::Reference(_)) {
                    let _ = resolve(reads, item)?;
                }
            }
        }
        _ => {}
    }
    Ok(())
}

struct PageGuard<'a, 'b> {
    reads: &'a mut Reads<'b>,
    shared: Option<&'a CheckedCache>,
    operand_limit: usize,
    local_cs: HashMap<u32, usize>,
    local_fn: HashMap<u32, usize>,
    local_streams: HashMap<u32, usize>,
    local_resources: HashMap<u32, usize>,
    report: Report,
    steps: usize,
}

fn depth_checked(
    local: &HashMap<u32, usize>,
    shared: Option<&HashMap<u32, usize>>,
    num: u32,
    depth: usize,
) -> bool {
    local.get(&num).is_some_and(|&d| d >= depth)
        || shared.is_some_and(|m| m.get(&num).is_some_and(|&d| d >= depth))
}

fn record_local_depth(
    local: &mut HashMap<u32, usize>,
    num: u32,
    depth: usize,
    budget: &Budget,
) -> Result<()> {
    let best = local.get(&num).copied().unwrap_or(0).max(depth);
    if local.get(&num).copied() != Some(best) {
        try_insert_map(local, num, best, budget)?;
    }
    Ok(())
}

impl<'b> PageGuard<'_, 'b> {
    fn budget(&self) -> &'b Budget {
        self.reads.budget()
    }

    fn tick(&mut self) -> Result<()> {
        self.steps = self.steps.wrapping_add(1);
        if self.steps.is_multiple_of(POLL_INTERVAL) {
            self.budget().poll()?;
        }
        Ok(())
    }

    fn is_cs_checked(&self, num: u32, depth: usize) -> bool {
        depth_checked(
            &self.local_cs,
            self.shared.map(|c| &c.color_spaces),
            num,
            depth,
        )
    }

    fn mark_cs_checked(&mut self, num: u32, depth: usize) -> Result<()> {
        let budget = self.reads.budget();
        record_local_depth(&mut self.local_cs, num, depth, budget)
    }

    fn is_fn_checked(&self, num: u32, depth: usize) -> bool {
        depth_checked(
            &self.local_fn,
            self.shared.map(|c| &c.functions),
            num,
            depth,
        )
    }

    fn mark_fn_checked(&mut self, num: u32, depth: usize) -> Result<()> {
        let budget = self.reads.budget();
        record_local_depth(&mut self.local_fn, num, depth, budget)
    }

    fn is_stream_checked(&self, num: u32, depth: usize) -> bool {
        depth_checked(
            &self.local_streams,
            self.shared.map(|c| &c.streams),
            num,
            depth,
        )
    }

    fn mark_stream_checked(&mut self, num: u32, depth: usize) -> Result<()> {
        let budget = self.reads.budget();
        record_local_depth(&mut self.local_streams, num, depth, budget)
    }

    fn is_resources_checked(&self, num: u32, depth: usize) -> bool {
        depth_checked(
            &self.local_resources,
            self.shared.map(|c| &c.resources),
            num,
            depth,
        )
    }

    fn mark_resources_checked(&mut self, num: u32, depth: usize) -> Result<()> {
        let budget = self.reads.budget();
        record_local_depth(&mut self.local_resources, num, depth, budget)
    }

    /// Records the page's own object number and `/Parent` ancestor chain in
    /// `Reads` so F4 memoization invalidates if the page dictionary or any
    /// inherited `/Resources` changes.
    fn record_page_lineage(&mut self, page: &Page) -> Result<()> {
        if let Some(reference) = page.object() {
            let _ = read_object(self.reads, reference.number())?;
        }
        let mut seen: HashSet<u32> = HashSet::new();
        let mut next = parent_number(page.dict());
        let mut hops = 0usize;
        while let Some(number) = next.take() {
            hops += 1;
            if hops > MAX_PARENT_CHAIN || !try_insert_set(&mut seen, number, self.reads.budget())? {
                break;
            }
            let Some(obj) = read_object(self.reads, number)? else {
                break;
            };
            let Value::Dict(node) = obj.as_ref() else {
                break;
            };
            next = parent_number(node);
        }
        Ok(())
    }

    fn check_page(&mut self, page: &Page) -> Result<()> {
        self.record_page_lineage(page)?;

        // 1. Scan the page's own /Contents stream(s).
        if let Some(contents) = page.dict().get("Contents").cloned() {
            self.scan_page_contents(&contents)?;
        }

        // 2. Check page-level /Group /CS and /Thumb image stream if present.
        if let Some(group_val) = page.dict().get("Group").cloned()
            && let Some(group) = resolve(self.reads, &group_val)?
            && let Value::Dict(group_dict) = group.value()
            && let Some(cs_val) = group_dict.get("CS")
        {
            self.check_color_space(cs_val)?;
        }
        if let Some(thumb_val) = page.dict().get("Thumb").cloned()
            && let Some(thumb) = resolve(self.reads, &thumb_val)?
            && let Value::Stream(stream) = thumb.value()
        {
            self.check_image_xobject(thumb.obj_num(), stream)?;
        }

        // 3. Collect top-level nested stream work items (page resources + visible annotation appearances).
        let mut stream_stack: BudgetVec<'_, StreamFrame> = BudgetVec::new(self.budget());
        let mut active_streams: HashSet<u32> = HashSet::new();

        let page_resources = match page.resources() {
            Some(resources_val) => resolve(self.reads, resources_val)?,
            None => None,
        };
        if let Some(resolved) = &page_resources {
            self.collect_from_resources(resolved, 0, &mut stream_stack, &active_streams)?;
        }

        if let Some(annots_val) = page.dict().get("Annots").cloned()
            && let Some(annots) = resolve(self.reads, &annots_val)?
            && let Value::Array(arr) = annots.value()
        {
            for item in arr.iter() {
                self.tick()?;
                let Some(annot) = resolve(self.reads, item)? else {
                    continue;
                };
                let Value::Dict(dict) = annot.value() else {
                    continue;
                };
                if let Some(Value::Integer(flags)) = dict.get("F")
                    && (*flags as u64) & ((1 << 1) | (1 << 5)) != 0
                {
                    continue;
                }
                if let Some(ap_val) = dict.get("AP")
                    && let Some(ap) = resolve(self.reads, ap_val)?
                    && let Value::Dict(ap_dict) = ap.value()
                    && let Some(n_val) = ap_dict.get("N")
                    && let Some(n_res) = resolve(self.reads, n_val)?
                {
                    match n_res.value() {
                        Value::Stream(_) => {
                            stream_stack.try_push(StreamFrame::Enter {
                                stream: n_res,
                                own_fallback: None,
                                inherited_resources: page_resources.clone(),
                                depth: 1,
                            })?;
                        }
                        Value::Dict(states) => {
                            for (_, state_val) in states.iter() {
                                self.tick()?;
                                if let Some(s_res) = resolve(self.reads, state_val)?
                                    && matches!(s_res.value(), Value::Stream(_))
                                {
                                    stream_stack.try_push(StreamFrame::Enter {
                                        stream: s_res,
                                        own_fallback: None,
                                        inherited_resources: page_resources.clone(),
                                        depth: 1,
                                    })?;
                                }
                            }
                        }
                        _ => {}
                    }
                }
            }
        }

        // 4. Iterative DFS over nested streams (Forms, SoftMasks, Tiling Patterns, Type 3 CharProcs).
        while let Some(frame) = stream_stack.pop() {
            self.tick()?;
            match frame {
                StreamFrame::Leave(num, depth) => {
                    active_streams.remove(&num);
                    self.mark_stream_checked(num, depth)?;
                }
                StreamFrame::Enter {
                    stream,
                    own_fallback,
                    inherited_resources,
                    depth,
                } => {
                    if depth > self.report.max_stream_depth {
                        self.report.max_stream_depth = depth;
                    }
                    if depth > MAX_STREAM_DEPTH {
                        return Err(depth_refusal(
                            "nested interpreted streams exceeded depth limit",
                            MAX_STREAM_DEPTH,
                            depth,
                        ));
                    }
                    if let Some(num) = stream.obj_num() {
                        if active_streams.contains(&num) {
                            return Err(cycle_refusal(format!(
                                "cyclic nested stream at object {num}"
                            )));
                        }
                        if self.is_stream_checked(num, depth) {
                            continue;
                        }
                        try_insert_set(&mut active_streams, num, self.reads.budget())?;
                        stream_stack.try_push(StreamFrame::Leave(num, depth))?;
                    }
                    self.report.objects_visited += 1;
                    let Value::Stream(s) = stream.value() else {
                        continue;
                    };
                    // Check Group /CS on transparency groups if present.
                    if let Some(group_val) = s.dict().get("Group")
                        && let Some(group) = resolve(self.reads, group_val)?
                        && let Value::Dict(group_dict) = group.value()
                        && let Some(cs_val) = group_dict.get("CS")
                    {
                        self.check_color_space(cs_val)?;
                    }
                    // Determine the effective resources for this stream:
                    // - Stream's own /Resources (or Type 3 font's /Resources): walk in full.
                    // - Otherwise, if hayro falls back to `inherited_resources` (Form, SoftMask,
                    //   Annotation AP, or Type 3 without font /Resources), resolve only the
                    //   resource names actually invoked by this stream's content bytes so a
                    //   non-recursive child sharing page resources never false-positives.
                    let own_res = match s.dict().get("Resources") {
                        Some(res_val) => resolve(self.reads, res_val)?,
                        None => own_fallback,
                    };
                    if let Some(res) = own_res {
                        self.scan_stream_bytes(s, StreamKind::Any, None)?;
                        self.collect_from_resources(
                            &res,
                            depth,
                            &mut stream_stack,
                            &active_streams,
                        )?;
                    } else if let Some(inherited) = inherited_resources {
                        let mut invoked: BudgetVec<'_, InvokedRef> = BudgetVec::new(self.budget());
                        let decoded =
                            self.scan_stream_bytes(s, StreamKind::Any, Some(&mut invoked))?;
                        if !invoked.is_empty()
                            && let Some(bytes) = decoded
                        {
                            self.collect_invoked_from_resources(
                                &inherited,
                                &bytes,
                                &invoked,
                                depth,
                                &mut stream_stack,
                            )?;
                        }
                    } else {
                        self.scan_stream_bytes(s, StreamKind::Any, None)?;
                    }
                }
            }
        }
        Ok(())
    }

    fn scan_page_contents(&mut self, contents: &Value) -> Result<()> {
        let Some(resolved) = resolve(self.reads, contents)? else {
            return Ok(());
        };
        match resolved.value() {
            Value::Stream(stream) => {
                self.scan_stream_bytes(stream, StreamKind::PageContent, None)?;
                Ok(())
            }
            Value::Array(arr) => {
                if arr.len() == 1 {
                    if let Some(first) = arr.iter().next()
                        && let Some(res) = resolve(self.reads, first)?
                        && let Value::Stream(stream) = res.value()
                    {
                        self.scan_stream_bytes(stream, StreamKind::PageContent, None)?;
                    }
                    return Ok(());
                }
                let mut joined: BudgetVec<'_, u8> = BudgetVec::new(self.budget());
                for item in arr.iter() {
                    self.tick()?;
                    if let Some(res) = resolve(self.reads, item)?
                        && let Value::Stream(stream) = res.value()
                    {
                        record_stream_filter_refs(self.reads, stream)?;
                        let decoded = match stream.decoded_in(self.reads.version(), self.budget()) {
                            Ok(bytes) => bytes,
                            Err(err) if err.limit().is_some() => return Err(err),
                            Err(_) => continue,
                        };
                        self.budget()
                            .check(LimitCode::PageContentSize, decoded.len() as u64)?;
                        let next_len = joined.len().saturating_add(decoded.len()).saturating_add(1);
                        self.budget()
                            .check(LimitCode::ContentStreamSize, next_len as u64)?;
                        joined.try_extend_from_slice(&decoded)?;
                        joined.try_push(b' ')?;
                    }
                }
                if !joined.is_empty() {
                    self.report.streams_scanned += 1;
                    self.report.content_bytes_scanned += joined.len() as u64;
                    scan_content_bytes(
                        joined.as_slice(),
                        self.operand_limit,
                        self.budget(),
                        &mut self.report,
                        None,
                    )?;
                }
                Ok(())
            }
            _ => Ok(()),
        }
    }

    fn scan_stream_bytes(
        &mut self,
        stream: &Stream,
        kind: StreamKind,
        invoked: Option<&mut BudgetVec<'b, InvokedRef>>,
    ) -> Result<Option<Vec<u8>>> {
        record_stream_filter_refs(self.reads, stream)?;
        let decoded = match stream.decoded_in(self.reads.version(), self.budget()) {
            Ok(bytes) => bytes,
            // Streams with unsupported/corrupt filters are skipped by hayro rather than overflowing.
            Err(err) if err.limit().is_some() => return Err(err),
            Err(_) => return Ok(None),
        };
        if kind == StreamKind::PageContent {
            self.budget()
                .check(LimitCode::PageContentSize, decoded.len() as u64)?;
        }
        self.report.streams_scanned += 1;
        self.report.content_bytes_scanned += decoded.len() as u64;
        let keep = invoked.is_some();
        scan_content_bytes(
            &decoded,
            self.operand_limit,
            self.budget(),
            &mut self.report,
            invoked,
        )?;
        Ok(keep.then_some(decoded))
    }

    /// Resolves only the resource names invoked by a nested stream that omitted
    /// its own `/Resources` and therefore inherits `resources` in `hayro`.
    fn collect_invoked_from_resources(
        &mut self,
        resources: &Resolved,
        content: &[u8],
        invoked: &[InvokedRef],
        depth: usize,
        stream_stack: &mut BudgetVec<'_, StreamFrame>,
    ) -> Result<()> {
        let Value::Dict(dict) = resources.value() else {
            return Ok(());
        };
        let mut decoded_name: BudgetVec<'_, u8> = BudgetVec::new(self.budget());
        for entry in invoked {
            self.tick()?;
            let raw = &content[entry.name.clone()];
            let name_bytes = decode_pdf_name(raw, &mut decoded_name)?;
            match entry.kind {
                InvokedKind::XObject => {
                    if let Some(xo_dict_val) = dict.get("XObject")
                        && let Some(xo_dict_res) = resolve(self.reads, xo_dict_val)?
                        && let Value::Dict(xo_dict) = xo_dict_res.value()
                        && let Some(val) = dict_get_bytes(xo_dict, name_bytes)
                        && let Some(xo) = resolve(self.reads, val)?
                        && let Value::Stream(stream) = xo.value()
                        && matches!(
                            stream.dict().get("Subtype"),
                            Some(Value::Name(n)) if n.as_bytes() == b"Form"
                        )
                    {
                        stream_stack.try_push(StreamFrame::Enter {
                            stream: xo,
                            own_fallback: None,
                            inherited_resources: Some(resources.clone()),
                            depth: depth + 1,
                        })?;
                    }
                }
                InvokedKind::Font => {
                    if let Some(font_dict_val) = dict.get("Font")
                        && let Some(font_dict_res) = resolve(self.reads, font_dict_val)?
                        && let Value::Dict(font_dict) = font_dict_res.value()
                        && let Some(val) = dict_get_bytes(font_dict, name_bytes)
                    {
                        self.enqueue_type3_font(val, resources, depth, stream_stack)?;
                    }
                }
                InvokedKind::ExtGState => {
                    if let Some(gs_dict_val) = dict.get("ExtGState")
                        && let Some(gs_dict_res) = resolve(self.reads, gs_dict_val)?
                        && let Value::Dict(gs_dict) = gs_dict_res.value()
                        && let Some(val) = dict_get_bytes(gs_dict, name_bytes)
                    {
                        self.check_ext_g_state(val, resources, depth, stream_stack)?;
                    }
                }
                InvokedKind::Pattern => {
                    if let Some(pat_dict_val) = dict.get("Pattern")
                        && let Some(pat_dict_res) = resolve(self.reads, pat_dict_val)?
                        && let Value::Dict(pat_dict) = pat_dict_res.value()
                        && let Some(val) = dict_get_bytes(pat_dict, name_bytes)
                        && let Some(pat) = resolve(self.reads, val)?
                        && matches!(pat.value(), Value::Stream(_))
                    {
                        // TilingPattern in hayro does not inherit parent resources.
                        stream_stack.try_push(StreamFrame::Enter {
                            stream: pat,
                            own_fallback: None,
                            inherited_resources: None,
                            depth: depth + 1,
                        })?;
                    }
                }
            }
        }
        Ok(())
    }

    /// Inspect every resource sub-dictionary that hayro interprets:
    /// `/ColorSpace`, `/Shading`, `/ExtGState`, `/Pattern`, `/XObject`, `/Font`.
    fn collect_from_resources(
        &mut self,
        resources: &Resolved,
        depth: usize,
        stream_stack: &mut BudgetVec<'_, StreamFrame>,
        active_streams: &HashSet<u32>,
    ) -> Result<()> {
        if let Some(num) = resources.obj_num()
            && self.is_resources_checked(num, depth)
        {
            return Ok(());
        }
        let Value::Dict(dict) = resources.value() else {
            return Ok(());
        };
        self.report.objects_visited += 1;

        // /ColorSpace
        if let Some(cs_dict_val) = dict.get("ColorSpace")
            && let Some(cs_dict_res) = resolve(self.reads, cs_dict_val)?
            && let Value::Dict(cs_dict) = cs_dict_res.value()
        {
            for (_, val) in cs_dict.iter() {
                self.tick()?;
                self.check_color_space(val)?;
            }
        }

        // /Shading
        if let Some(sh_dict_val) = dict.get("Shading")
            && let Some(sh_dict_res) = resolve(self.reads, sh_dict_val)?
            && let Value::Dict(sh_dict) = sh_dict_res.value()
        {
            for (_, val) in sh_dict.iter() {
                self.tick()?;
                self.check_shading(val)?;
            }
        }

        // /ExtGState
        if let Some(gs_dict_val) = dict.get("ExtGState")
            && let Some(gs_dict_res) = resolve(self.reads, gs_dict_val)?
            && let Value::Dict(gs_dict) = gs_dict_res.value()
        {
            for (_, val) in gs_dict.iter() {
                self.tick()?;
                self.check_ext_g_state(val, resources, depth, stream_stack)?;
            }
        }

        // /Pattern
        if let Some(pat_dict_val) = dict.get("Pattern")
            && let Some(pat_dict_res) = resolve(self.reads, pat_dict_val)?
            && let Value::Dict(pat_dict) = pat_dict_res.value()
        {
            for (_, val) in pat_dict.iter() {
                self.tick()?;
                if let Some(pat) = resolve(self.reads, val)? {
                    match pat.value() {
                        Value::Dict(pat_d) => {
                            if let Some(sh) = pat_d.get("Shading") {
                                self.check_shading(sh)?;
                            }
                        }
                        Value::Stream(_) => {
                            // TilingPattern in hayro does not inherit parent resources.
                            stream_stack.try_push(StreamFrame::Enter {
                                stream: pat,
                                own_fallback: None,
                                inherited_resources: None,
                                depth: depth + 1,
                            })?;
                        }
                        _ => {}
                    }
                }
            }
        }

        // /XObject (Forms and Image ColorSpaces)
        if let Some(xo_dict_val) = dict.get("XObject")
            && let Some(xo_dict_res) = resolve(self.reads, xo_dict_val)?
            && let Value::Dict(xo_dict) = xo_dict_res.value()
        {
            for (_, val) in xo_dict.iter() {
                self.tick()?;
                let Some(xo) = resolve(self.reads, val)? else {
                    continue;
                };
                let Value::Stream(stream) = xo.value() else {
                    continue;
                };
                let subtype = stream.dict().get("Subtype").and_then(|v| match v {
                    Value::Name(n) => Some(n.as_bytes()),
                    _ => None,
                });
                match subtype {
                    Some(b"Form") => {
                        stream_stack.try_push(StreamFrame::Enter {
                            stream: xo,
                            own_fallback: None,
                            inherited_resources: Some(resources.clone()),
                            depth: depth + 1,
                        })?;
                    }
                    Some(b"Image") => {
                        self.check_image_xobject(xo.obj_num(), stream)?;
                    }
                    _ => {}
                }
            }
        }

        // /Font (Type 3 /CharProcs and /Resources)
        if let Some(font_dict_val) = dict.get("Font")
            && let Some(font_dict_res) = resolve(self.reads, font_dict_val)?
            && let Value::Dict(font_dict) = font_dict_res.value()
        {
            for (_, val) in font_dict.iter() {
                self.tick()?;
                self.enqueue_type3_font(val, resources, depth, stream_stack)?;
            }
        }

        // Only cache a Resources dictionary when we're not inside an active cycle path
        // (so every cycle is caught before any object on it is marked clean).
        if active_streams.is_empty()
            && let Some(num) = resources.obj_num()
        {
            self.mark_resources_checked(num, depth)?;
        }
        Ok(())
    }

    fn enqueue_type3_font(
        &mut self,
        val: &Value,
        parent_resources: &Resolved,
        depth: usize,
        stream_stack: &mut BudgetVec<'_, StreamFrame>,
    ) -> Result<()> {
        let Some(font) = resolve(self.reads, val)? else {
            return Ok(());
        };
        let Value::Dict(f_dict) = font.value() else {
            return Ok(());
        };
        let is_type3 = matches!(
            f_dict.get("Subtype"),
            Some(Value::Name(n)) if n.as_bytes() == b"Type3"
        );
        if !is_type3 {
            return Ok(());
        }
        let font_resources = match f_dict.get("Resources") {
            Some(f_res_val) => resolve(self.reads, f_res_val)?,
            None => None,
        };
        if let Some(procs_val) = f_dict.get("CharProcs")
            && let Some(procs) = resolve(self.reads, procs_val)?
            && let Value::Dict(procs_dict) = procs.value()
        {
            for (_, proc_val) in procs_dict.iter() {
                self.tick()?;
                if let Some(proc_res) = resolve(self.reads, proc_val)?
                    && matches!(proc_res.value(), Value::Stream(_))
                {
                    stream_stack.try_push(StreamFrame::Enter {
                        stream: proc_res,
                        own_fallback: font_resources.clone(),
                        inherited_resources: font_resources
                            .is_none()
                            .then(|| parent_resources.clone()),
                        depth: depth + 1,
                    })?;
                }
            }
        }
        Ok(())
    }

    /// Inspect an Image XObject's own `/ColorSpace` (or `/CS`) and any chained
    /// `/SMask` or `/Mask` image stream color spaces iteratively with cycle
    /// detection and depth tracking.
    fn check_image_xobject(&mut self, root_num: Option<u32>, root: &Stream) -> Result<()> {
        let mut stack: BudgetVec<'_, ImageMaskFrame> = BudgetVec::new(self.budget());
        let mut active: HashSet<u32> = HashSet::new();
        let mut checked: HashMap<u32, usize> = HashMap::new();
        if let Some(num) = root_num {
            try_insert_set(&mut active, num, self.reads.budget())?;
        }
        self.inspect_image_stream(root, 0, &mut stack)?;
        while let Some(frame) = stack.pop() {
            self.tick()?;
            match frame {
                ImageMaskFrame::Leave(num) => {
                    active.remove(&num);
                }
                ImageMaskFrame::Enter { item, depth } => {
                    if depth > MAX_STREAM_DEPTH {
                        return Err(depth_refusal(
                            "image mask chain exceeded depth limit",
                            MAX_STREAM_DEPTH,
                            depth,
                        ));
                    }
                    if let Some(num) = item.obj_num() {
                        if active.contains(&num) {
                            return Err(cycle_refusal(format!(
                                "cyclic image mask at object {num}"
                            )));
                        }
                        if checked.get(&num).is_some_and(|&d| d >= depth) {
                            continue;
                        }
                        try_insert_set(&mut active, num, self.reads.budget())?;
                        stack.try_push(ImageMaskFrame::Leave(num))?;
                        let best = checked.get(&num).copied().unwrap_or(0).max(depth);
                        try_insert_map(&mut checked, num, best, self.reads.budget())?;
                    }
                    if let Value::Stream(mask_stream) = item.value() {
                        self.inspect_image_stream(mask_stream, depth, &mut stack)?;
                    }
                }
            }
        }
        Ok(())
    }

    fn inspect_image_stream(
        &mut self,
        stream: &Stream,
        depth: usize,
        stack: &mut BudgetVec<'_, ImageMaskFrame>,
    ) -> Result<()> {
        if let Some(cs) = stream
            .dict()
            .get("ColorSpace")
            .or_else(|| stream.dict().get("CS"))
        {
            self.check_color_space(cs)?;
        }
        for mask_key in ["SMask", "Mask"] {
            if let Some(mask_val) = stream.dict().get(mask_key)
                && let Some(mask_res) = resolve(self.reads, mask_val)?
                && matches!(mask_res.value(), Value::Stream(_))
            {
                stack.try_push(ImageMaskFrame::Enter {
                    item: mask_res,
                    depth: depth + 1,
                })?;
            }
        }
        Ok(())
    }

    fn check_shading(&mut self, value: &Value) -> Result<()> {
        let Some(resolved) = resolve(self.reads, value)? else {
            return Ok(());
        };
        let dict = match resolved.value() {
            Value::Dict(d) => d,
            Value::Stream(s) => s.dict(),
            _ => return Ok(()),
        };
        if let Some(cs) = dict.get("ColorSpace") {
            self.check_color_space(cs)?;
        }
        if let Some(func) = dict.get("Function") {
            self.check_function_or_array(func)?;
        }
        Ok(())
    }

    fn check_ext_g_state(
        &mut self,
        value: &Value,
        parent_resources: &Resolved,
        depth: usize,
        stream_stack: &mut BudgetVec<'_, StreamFrame>,
    ) -> Result<()> {
        let Some(resolved) = resolve(self.reads, value)? else {
            return Ok(());
        };
        let Value::Dict(dict) = resolved.value() else {
            return Ok(());
        };
        for key in ["TR2", "TR"] {
            if let Some(tr) = dict.get(key) {
                self.check_function_or_array(tr)?;
            }
        }
        if let Some(smask_val) = dict.get("SMask")
            && let Some(smask) = resolve(self.reads, smask_val)?
            && let Value::Dict(sm_dict) = smask.value()
        {
            if let Some(tr) = sm_dict.get("TR") {
                self.check_function_or_array(tr)?;
            }
            if let Some(g_val) = sm_dict.get("G")
                && let Some(g_res) = resolve(self.reads, g_val)?
                && matches!(g_res.value(), Value::Stream(_))
            {
                stream_stack.try_push(StreamFrame::Enter {
                    stream: g_res,
                    own_fallback: None,
                    inherited_resources: Some(parent_resources.clone()),
                    depth: depth + 1,
                })?;
            }
        }
        Ok(())
    }

    fn check_function_or_array(&mut self, value: &Value) -> Result<()> {
        let Some(resolved) = resolve(self.reads, value)? else {
            return Ok(());
        };
        match resolved.value() {
            Value::Array(arr) => {
                for item in arr.iter() {
                    self.tick()?;
                    self.check_function(item)?;
                }
                Ok(())
            }
            Value::Name(_) => Ok(()),
            _ => self.check_function_resolved(resolved),
        }
    }

    /// Iterative DFS over PDF functions (Type 3 stitching + Type 4 PostScript).
    fn check_function(&mut self, value: &Value) -> Result<()> {
        let Some(resolved) = resolve(self.reads, value)? else {
            return Ok(());
        };
        self.check_function_resolved(resolved)
    }

    fn check_function_resolved(&mut self, root: Resolved) -> Result<()> {
        enum Frame {
            Enter { item: Resolved, depth: usize },
            Leave(u32, usize),
        }
        let mut stack: BudgetVec<'_, Frame> = BudgetVec::new(self.budget());
        let mut active: HashSet<u32> = HashSet::new();
        stack.try_push(Frame::Enter {
            item: root,
            depth: 1,
        })?;
        while let Some(frame) = stack.pop() {
            self.tick()?;
            match frame {
                Frame::Leave(num, depth) => {
                    active.remove(&num);
                    self.mark_fn_checked(num, depth)?;
                }
                Frame::Enter { item, depth } => {
                    if depth > self.report.max_fn_depth {
                        self.report.max_fn_depth = depth;
                    }
                    if depth > MAX_FUNCTION_DEPTH {
                        return Err(depth_refusal(
                            "function nesting exceeded depth limit",
                            MAX_FUNCTION_DEPTH,
                            depth,
                        ));
                    }
                    if let Some(num) = item.obj_num() {
                        if active.contains(&num) {
                            return Err(cycle_refusal(format!(
                                "cyclic function graph at object {num}"
                            )));
                        }
                        if self.is_fn_checked(num, depth) {
                            continue;
                        }
                        try_insert_set(&mut active, num, self.budget())?;
                        stack.try_push(Frame::Leave(num, depth))?;
                    }
                    self.report.objects_visited += 1;
                    let (dict, stream) = match item.value() {
                        Value::Dict(d) => (d, None),
                        Value::Stream(s) => (s.dict(), Some(s)),
                        _ => continue,
                    };
                    let ftype = dict.get("FunctionType").and_then(|v| match v {
                        Value::Integer(n) => Some(*n),
                        _ => None,
                    });
                    match ftype {
                        Some(3) => {
                            if let Some(funcs_val) = dict.get("Functions")
                                && let Some(funcs_res) = resolve(self.reads, funcs_val)?
                                && let Value::Array(arr) = funcs_res.value()
                            {
                                for child in arr.iter() {
                                    self.tick()?;
                                    if let Some(c_res) = resolve(self.reads, child)? {
                                        stack.try_push(Frame::Enter {
                                            item: c_res,
                                            depth: depth + 1,
                                        })?;
                                    }
                                }
                            }
                        }
                        Some(4) => {
                            if let Some(s) = stream {
                                self.check_type4_stream(s)?;
                            }
                        }
                        _ => {}
                    }
                }
            }
        }
        Ok(())
    }

    /// Iterative brace-depth scanner for Type 4 PostScript calculator functions.
    fn check_type4_stream(&mut self, stream: &Stream) -> Result<()> {
        record_stream_filter_refs(self.reads, stream)?;
        let decoded = match stream.decoded_in(self.reads.version(), self.budget()) {
            Ok(bytes) => bytes,
            Err(err) if err.limit().is_some() => return Err(err),
            Err(_) => return Ok(()),
        };
        self.report.type4_streams_scanned += 1;
        self.report.type4_bytes_scanned += decoded.len() as u64;
        let mut depth = 0usize;
        let mut max_depth = 0usize;
        let mut in_comment = false;
        for (idx, &b) in decoded.iter().enumerate() {
            if idx.is_multiple_of(4096) {
                self.budget().poll()?;
            }
            if in_comment {
                if b == b'\n' || b == b'\r' {
                    in_comment = false;
                }
                continue;
            }
            match b {
                b'%' => in_comment = true,
                b'{' => {
                    depth += 1;
                    if depth > max_depth {
                        max_depth = depth;
                    }
                    if depth > MAX_TYPE4_BRACES {
                        return Err(depth_refusal(
                            "Type 4 PostScript procedure braces exceeded limit",
                            MAX_TYPE4_BRACES,
                            depth,
                        ));
                    }
                }
                b'}' => {
                    depth = depth.saturating_sub(1);
                }
                _ => {}
            }
        }
        if max_depth > self.report.max_type4_braces {
            self.report.max_type4_braces = max_depth;
        }
        Ok(())
    }

    /// Iterative DFS over PDF color spaces (`/Indexed`, `/Separation`,
    /// `/DeviceN`, `/Pattern`, `/ICCBased /Alternate`) and their tint functions.
    fn check_color_space(&mut self, value: &Value) -> Result<()> {
        enum Frame {
            Enter { item: Resolved, depth: usize },
            Leave(u32, usize),
        }
        let Some(root) = resolve(self.reads, value)? else {
            return Ok(());
        };
        let mut stack: BudgetVec<'_, Frame> = BudgetVec::new(self.budget());
        let mut active: HashSet<u32> = HashSet::new();
        let mut pending_functions: BudgetVec<'_, Value> = BudgetVec::new(self.budget());
        stack.try_push(Frame::Enter {
            item: root,
            depth: 1,
        })?;
        while let Some(frame) = stack.pop() {
            self.tick()?;
            match frame {
                Frame::Leave(num, depth) => {
                    active.remove(&num);
                    self.mark_cs_checked(num, depth)?;
                }
                Frame::Enter { item, depth } => {
                    if depth > self.report.max_cs_depth {
                        self.report.max_cs_depth = depth;
                    }
                    if depth > MAX_COLOR_SPACE_DEPTH {
                        return Err(depth_refusal(
                            "color space nesting exceeded depth limit",
                            MAX_COLOR_SPACE_DEPTH,
                            depth,
                        ));
                    }
                    if let Some(num) = item.obj_num() {
                        if active.contains(&num) {
                            return Err(cycle_refusal(format!(
                                "cyclic color space at object {num}"
                            )));
                        }
                        if self.is_cs_checked(num, depth) {
                            continue;
                        }
                        try_insert_set(&mut active, num, self.budget())?;
                        stack.try_push(Frame::Leave(num, depth))?;
                    }
                    self.report.objects_visited += 1;
                    let Value::Array(arr) = item.value() else {
                        continue;
                    };
                    let Some(tag) = array_name(arr, 0) else {
                        continue;
                    };
                    match tag {
                        b"Indexed" | b"I" => {
                            if let Some(base) = arr.get(1)
                                && let Some(res) = resolve(self.reads, base)?
                            {
                                stack.try_push(Frame::Enter {
                                    item: res,
                                    depth: depth + 1,
                                })?;
                            }
                        }
                        b"Separation" | b"DeviceN" => {
                            if let Some(alt) = arr.get(2)
                                && let Some(res) = resolve(self.reads, alt)?
                            {
                                stack.try_push(Frame::Enter {
                                    item: res,
                                    depth: depth + 1,
                                })?;
                            }
                            if let Some(tint) = arr.get(3) {
                                pending_functions.try_push(tint.clone())?;
                            }
                        }
                        b"Pattern" => {
                            if let Some(under) = arr.get(1)
                                && let Some(res) = resolve(self.reads, under)?
                            {
                                stack.try_push(Frame::Enter {
                                    item: res,
                                    depth: depth + 1,
                                })?;
                            }
                        }
                        b"ICCBased" => {
                            if let Some(icc_val) = arr.get(1)
                                && let Some(icc) = resolve(self.reads, icc_val)?
                                && let Value::Stream(stream) = icc.value()
                                && let Some(alt) = stream.dict().get("Alternate")
                                && let Some(res) = resolve(self.reads, alt)?
                            {
                                stack.try_push(Frame::Enter {
                                    item: res,
                                    depth: depth + 1,
                                })?;
                            }
                        }
                        _ => {}
                    }
                }
            }
        }
        for func in pending_functions.iter() {
            self.check_function(func)?;
        }
        Ok(())
    }
}

enum ImageMaskFrame {
    Enter { item: Resolved, depth: usize },
    Leave(u32),
}

enum StreamFrame {
    Enter {
        stream: Resolved,
        /// Own fallback resources (Type 3 font `/Resources`), walked in full.
        own_fallback: Option<Resolved>,
        /// Caller resources inherited when stream has no `/Resources` of its own;
        /// only names actually invoked by the stream's operators are followed.
        inherited_resources: Option<Resolved>,
        depth: usize,
    },
    Leave(u32, usize),
}

#[derive(Clone, Copy)]
enum InvokedKind {
    XObject,
    Font,
    ExtGState,
    Pattern,
}

struct InvokedRef {
    kind: InvokedKind,
    name: Range<usize>,
}

/// Single linear iterative pass over decoded content bytes.
///
/// Checks:
/// 1. Operand container nesting (`[`/`]`, `<<`/`>>`) <= `operand_limit`
///    (prevents `hayro-syntax`'s silent truncation past 512, #15 / #1666).
/// 2. Inline image (`BI ... ID ... EI`) nested lookahead hazards (#740):
///    tracks inline images whose post-`EI` tail contains another `EI` (either as
///    a standalone token or raw `EI` inside a comment/string/name before 20
///    operators or a text-showing operator) before the next `BI`, which triggers
///    `O(2^n)` time and `O(n)` stack frames in `hayro-syntax::content::read_inline_image`.
/// 3. Optionally records invoked `/Name` operands of `Do`, `Tf`, `gs`, `scn`/`SCN`
///    when a nested stream inherits caller resources.
fn scan_content_bytes<'b>(
    data: &[u8],
    operand_limit: usize,
    budget: &'b Budget,
    report: &mut Report,
    mut invoked: Option<&mut BudgetVec<'b, InvokedRef>>,
) -> Result<()> {
    let mut i = 0usize;
    let len = data.len();
    let mut depth = 0usize;
    let mut max_depth = 0usize;
    let mut inline_images = 0u64;
    let mut lookahead_chain = 0usize;
    let mut max_lookahead_chain = 0usize;
    let mut prev_was_bi = false;
    let mut saw_raw_ei_since_bi = false;
    let mut ops_since_bi = 0usize;
    let mut next_poll = BYTE_POLL_INTERVAL;
    // Last two top-level operands (name ranges, if each operand was a `/Name`).
    let mut prev_operand: Option<Range<usize>> = None;
    let mut prev_prev_operand: Option<Range<usize>> = None;

    while i < len {
        if i >= next_poll {
            budget.poll()?;
            next_poll = i.saturating_add(BYTE_POLL_INTERVAL);
        }
        let b = data[i];
        match b {
            b'%' => {
                let start = i + 1;
                i = start;
                while i < len && data[i] != b'\n' && data[i] != b'\r' {
                    if i >= next_poll {
                        budget.poll()?;
                        next_poll = i.saturating_add(BYTE_POLL_INTERVAL);
                    }
                    i += 1;
                }
                if prev_was_bi && !saw_raw_ei_since_bi && contains_ei(&data[start..i]) {
                    saw_raw_ei_since_bi = true;
                }
            }
            b'(' => {
                // Iterative balanced literal string scan.
                let start = i + 1;
                i = start;
                let mut parens = 1usize;
                while i < len && parens > 0 {
                    if i >= next_poll {
                        budget.poll()?;
                        next_poll = i.saturating_add(BYTE_POLL_INTERVAL);
                    }
                    match data[i] {
                        b'\\' => i = (i + 2).min(len),
                        b'(' => {
                            parens += 1;
                            i += 1;
                        }
                        b')' => {
                            parens -= 1;
                            i += 1;
                        }
                        _ => i += 1,
                    }
                }
                if prev_was_bi && !saw_raw_ei_since_bi && contains_ei(&data[start..i]) {
                    saw_raw_ei_since_bi = true;
                }
                if depth == 0 {
                    prev_prev_operand = prev_operand.take();
                }
            }
            b'<' => {
                if i + 1 < len && data[i + 1] == b'<' {
                    depth += 1;
                    if depth > max_depth {
                        max_depth = depth;
                    }
                    if depth > operand_limit {
                        return Err(depth_refusal(
                            "content operand nesting exceeded limit",
                            operand_limit,
                            depth,
                        ));
                    }
                    i += 2;
                } else {
                    // Hex string `<...>`
                    let start = i + 1;
                    i = start;
                    while i < len && data[i] != b'>' {
                        if i >= next_poll {
                            budget.poll()?;
                            next_poll = i.saturating_add(BYTE_POLL_INTERVAL);
                        }
                        i += 1;
                    }
                    if prev_was_bi && !saw_raw_ei_since_bi && contains_ei(&data[start..i]) {
                        saw_raw_ei_since_bi = true;
                    }
                    if i < len {
                        i += 1;
                    }
                    if depth == 0 {
                        prev_prev_operand = prev_operand.take();
                    }
                }
            }
            b'>' if i + 1 < len && data[i + 1] == b'>' => {
                depth = depth.saturating_sub(1);
                if depth == 0 {
                    prev_prev_operand = prev_operand.take();
                }
                i += 2;
            }
            b'[' => {
                depth += 1;
                if depth > max_depth {
                    max_depth = depth;
                }
                if depth > operand_limit {
                    return Err(depth_refusal(
                        "content operand nesting exceeded limit",
                        operand_limit,
                        depth,
                    ));
                }
                i += 1;
            }
            b']' => {
                depth = depth.saturating_sub(1);
                if depth == 0 {
                    prev_prev_operand = prev_operand.take();
                }
                i += 1;
            }
            b'/' => {
                let start = i + 1;
                i = start;
                while i < len && is_regular(data[i]) {
                    i += 1;
                }
                if prev_was_bi && !saw_raw_ei_since_bi && contains_ei(&data[start..i]) {
                    saw_raw_ei_since_bi = true;
                }
                if depth == 0 {
                    prev_prev_operand = prev_operand.take();
                    prev_operand = Some(start..i);
                }
            }
            _ if is_whitespace(b) => {
                i += 1;
            }
            _ if is_regular(b) => {
                let start = i;
                i += 1;
                while i < len && is_regular(data[i]) {
                    i += 1;
                }
                let tok = &data[start..i];
                if tok == b"BI" {
                    if prev_was_bi && saw_raw_ei_since_bi && ops_since_bi < LOOKAHEAD_OPERATOR_LIMIT
                    {
                        lookahead_chain += 1;
                        if lookahead_chain > max_lookahead_chain {
                            max_lookahead_chain = lookahead_chain;
                        }
                        if lookahead_chain > MAX_INLINE_LOOKAHEAD_CHAIN {
                            return Err(depth_refusal(
                                "nested inline-image lookahead hazard (#740)",
                                MAX_INLINE_LOOKAHEAD_CHAIN,
                                lookahead_chain,
                            ));
                        }
                    }
                    inline_images += 1;
                    prev_was_bi = true;
                    saw_raw_ei_since_bi = false;
                    ops_since_bi = 0;
                    prev_operand = None;
                    prev_prev_operand = None;
                    let Some(after_id) =
                        scan_inline_dict(data, i, depth, operand_limit, &mut max_depth, budget)?
                    else {
                        break;
                    };
                    let Some(ei_pos) = find_candidate_ei(data, after_id, budget)? else {
                        break;
                    };
                    i = (ei_pos + 2).min(len);
                    next_poll = i.saturating_add(BYTE_POLL_INTERVAL);
                } else if tok == b"EI" && prev_was_bi {
                    // Stray `EI` following an inline image before the next `BI`:
                    // direct trigger for `hayro-syntax`'s recursive
                    // `starts_with_content` lookahead (#740).
                    lookahead_chain += 1;
                    if lookahead_chain > max_lookahead_chain {
                        max_lookahead_chain = lookahead_chain;
                    }
                    if lookahead_chain > MAX_INLINE_LOOKAHEAD_CHAIN {
                        return Err(depth_refusal(
                            "nested inline-image lookahead hazard (#740)",
                            MAX_INLINE_LOOKAHEAD_CHAIN,
                            lookahead_chain,
                        ));
                    }
                    prev_was_bi = false;
                    saw_raw_ei_since_bi = false;
                    prev_operand = None;
                    prev_prev_operand = None;
                } else if depth == 0 {
                    if is_number_or_keyword(tok) {
                        prev_prev_operand = prev_operand.take();
                    } else {
                        // Content operator.
                        if prev_was_bi {
                            if matches!(tok, b"Tj" | b"TJ" | b"'" | b"\"") {
                                // `hayro-syntax::starts_with_content` returns true on text ops.
                                prev_was_bi = false;
                                saw_raw_ei_since_bi = false;
                            } else {
                                ops_since_bi = ops_since_bi.saturating_add(1);
                                if ops_since_bi >= LOOKAHEAD_OPERATOR_LIMIT {
                                    prev_was_bi = false;
                                    saw_raw_ei_since_bi = false;
                                }
                            }
                        }
                        if let Some(ref mut list) = invoked {
                            match tok {
                                b"Do" => {
                                    if let Some(name) = prev_operand.take() {
                                        list.try_push(InvokedRef {
                                            kind: InvokedKind::XObject,
                                            name,
                                        })?;
                                    }
                                }
                                b"Tf" => {
                                    if let Some(name) = prev_prev_operand.take() {
                                        list.try_push(InvokedRef {
                                            kind: InvokedKind::Font,
                                            name,
                                        })?;
                                    }
                                }
                                b"gs" => {
                                    if let Some(name) = prev_operand.take() {
                                        list.try_push(InvokedRef {
                                            kind: InvokedKind::ExtGState,
                                            name,
                                        })?;
                                    }
                                }
                                b"scn" | b"SCN" => {
                                    if let Some(name) = prev_operand.take() {
                                        list.try_push(InvokedRef {
                                            kind: InvokedKind::Pattern,
                                            name,
                                        })?;
                                    }
                                }
                                _ => {}
                            }
                        }
                        prev_operand = None;
                        prev_prev_operand = None;
                    }
                }
            }
            _ => {
                i += 1;
            }
        }
    }
    if max_depth > report.max_content_depth {
        report.max_content_depth = max_depth;
    }
    report.inline_images_seen += inline_images;
    if max_lookahead_chain > report.max_inline_lookahead_chain {
        report.max_inline_lookahead_chain = max_lookahead_chain;
    }
    Ok(())
}

fn contains_ei(slice: &[u8]) -> bool {
    slice.windows(2).any(|w| w == b"EI")
}

fn is_number_or_keyword(tok: &[u8]) -> bool {
    if matches!(tok, b"true" | b"false" | b"null") {
        return true;
    }
    tok.first()
        .is_some_and(|&b| b.is_ascii_digit() || matches!(b, b'+' | b'-' | b'.'))
}

fn decode_pdf_name<'a>(raw: &'a [u8], scratch: &'a mut BudgetVec<'_, u8>) -> Result<&'a [u8]> {
    if !raw.contains(&b'#') {
        return Ok(raw);
    }
    scratch.clear();
    let mut i = 0usize;
    while i < raw.len() {
        if raw[i] == b'#'
            && i + 2 < raw.len()
            && let (Some(hi), Some(lo)) = (hex_digit(raw[i + 1]), hex_digit(raw[i + 2]))
        {
            scratch.try_push((hi << 4) | lo)?;
            i += 3;
            continue;
        }
        scratch.try_push(raw[i])?;
        i += 1;
    }
    Ok(scratch.as_slice())
}

const fn hex_digit(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

fn dict_get_bytes<'a>(dict: &'a Dict, name: &[u8]) -> Option<&'a Value> {
    dict.iter()
        .find(|(key, _)| key.as_bytes() == name)
        .map(|(_, value)| value)
}

fn parent_number(dict: &Dict) -> Option<u32> {
    match dict.get("Parent") {
        Some(Value::Reference(r)) => Some(r.number()),
        _ => None,
    }
}

fn array_name(arr: &Array, idx: usize) -> Option<&[u8]> {
    match arr.get(idx)? {
        Value::Name(n) => Some(n.as_bytes()),
        _ => None,
    }
}

const fn is_whitespace(b: u8) -> bool {
    matches!(b, 0x00 | 0x09 | 0x0A | 0x0C | 0x0D | 0x20)
}

const fn is_delimiter(b: u8) -> bool {
    matches!(
        b,
        b'(' | b')' | b'<' | b'>' | b'[' | b']' | b'{' | b'}' | b'/' | b'%'
    )
}

const fn is_regular(b: u8) -> bool {
    !is_whitespace(b) && !is_delimiter(b)
}

/// Scan an inline image dictionary starting after `BI` until `ID` + one whitespace byte.
fn scan_inline_dict(
    data: &[u8],
    mut i: usize,
    base_depth: usize,
    operand_limit: usize,
    max_depth: &mut usize,
    budget: &Budget,
) -> Result<Option<usize>> {
    let len = data.len();
    let mut depth = base_depth;
    let mut next_poll = i.saturating_add(BYTE_POLL_INTERVAL);
    while i < len {
        if i >= next_poll {
            budget.poll()?;
            next_poll = i.saturating_add(BYTE_POLL_INTERVAL);
        }
        match data[i] {
            b'%' => {
                i += 1;
                while i < len && data[i] != b'\n' && data[i] != b'\r' {
                    if i >= next_poll {
                        budget.poll()?;
                        next_poll = i.saturating_add(BYTE_POLL_INTERVAL);
                    }
                    i += 1;
                }
            }
            b'(' => {
                i += 1;
                let mut parens = 1usize;
                while i < len && parens > 0 {
                    if i >= next_poll {
                        budget.poll()?;
                        next_poll = i.saturating_add(BYTE_POLL_INTERVAL);
                    }
                    match data[i] {
                        b'\\' => i = (i + 2).min(len),
                        b'(' => {
                            parens += 1;
                            i += 1;
                        }
                        b')' => {
                            parens -= 1;
                            i += 1;
                        }
                        _ => i += 1,
                    }
                }
            }
            b'<' if i + 1 < len && data[i + 1] == b'<' => {
                depth += 1;
                *max_depth = (*max_depth).max(depth);
                if depth > operand_limit {
                    return Err(depth_refusal(
                        "inline image dictionary nesting exceeded limit",
                        operand_limit,
                        depth,
                    ));
                }
                i += 2;
            }
            b'<' => {
                i += 1;
                while i < len && data[i] != b'>' {
                    if i >= next_poll {
                        budget.poll()?;
                        next_poll = i.saturating_add(BYTE_POLL_INTERVAL);
                    }
                    i += 1;
                }
                if i < len {
                    i += 1;
                }
            }
            b'>' if i + 1 < len && data[i + 1] == b'>' => {
                depth = depth.saturating_sub(1);
                i += 2;
            }
            b'[' => {
                depth += 1;
                *max_depth = (*max_depth).max(depth);
                if depth > operand_limit {
                    return Err(depth_refusal(
                        "inline image dictionary nesting exceeded limit",
                        operand_limit,
                        depth,
                    ));
                }
                i += 1;
            }
            b']' => {
                depth = depth.saturating_sub(1);
                i += 1;
            }
            b'/' => {
                i += 1;
                while i < len && is_regular(data[i]) {
                    i += 1;
                }
            }
            b if is_whitespace(b) => i += 1,
            b if is_regular(b) => {
                let start = i;
                i += 1;
                while i < len && is_regular(data[i]) {
                    i += 1;
                }
                if &data[start..i] == b"ID" && depth == base_depth {
                    if i < len && is_whitespace(data[i]) {
                        return Ok(Some(i + 1));
                    }
                    return Ok(Some(i));
                }
            }
            _ => i += 1,
        }
    }
    Ok(None)
}

/// Find the first `EI` preceded by whitespace (or at start) and followed by whitespace or EOF.
fn find_candidate_ei(data: &[u8], start: usize, budget: &Budget) -> Result<Option<usize>> {
    let len = data.len();
    let mut i = start;
    let mut next_poll = start.saturating_add(BYTE_POLL_INTERVAL);
    while i + 1 < len {
        if i >= next_poll {
            budget.poll()?;
            next_poll = i.saturating_add(BYTE_POLL_INTERVAL);
        }
        if data[i] == b'E' && data[i + 1] == b'I' {
            let before_ok = i == start || is_whitespace(data[i - 1]);
            let after_ok = i + 2 == len || is_whitespace(data[i + 2]);
            if before_ok && after_ok {
                return Ok(Some(i));
            }
        }
        i += 1;
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use std::fmt::{self, Write as _};
    use std::fs;
    use std::path::Path;

    use pyropdf_store::budget::{CancellationToken, DocumentLimits};
    use pyropdf_store::save::FileBase;
    use pyropdf_store::value::{Bytes, Name};
    use pyropdf_store::version::Document;

    use super::*;

    struct ByteWriter<'a>(&'a mut Vec<u8>);
    impl fmt::Write for ByteWriter<'_> {
        fn write_str(&mut self, s: &str) -> fmt::Result {
            self.0.extend_from_slice(s.as_bytes());
            Ok(())
        }
    }

    fn build_pdf(objects: &[Vec<u8>]) -> Vec<u8> {
        let mut out = Vec::from(&b"%PDF-1.7\n"[..]);
        let mut offsets = Vec::with_capacity(objects.len());
        for (idx, body) in objects.iter().enumerate() {
            offsets.push(out.len());
            let _ = writeln!(ByteWriter(&mut out), "{} 0 obj", idx + 1);
            out.extend_from_slice(body);
            out.extend_from_slice(b"\nendobj\n");
        }
        let xref = out.len();
        let _ = write!(
            ByteWriter(&mut out),
            "xref\n0 {}\n0000000000 65535 f \n",
            objects.len() + 1
        );
        for offset in offsets {
            let _ = writeln!(ByteWriter(&mut out), "{offset:010} 00000 n ");
        }
        let _ = write!(
            ByteWriter(&mut out),
            "trailer\n<</Size {}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n",
            objects.len() + 1
        );
        out
    }

    fn one_page(content: &[u8], resources: &[u8], extra: &[Vec<u8>]) -> Vec<u8> {
        let mut cobj = Vec::new();
        let _ = writeln!(ByteWriter(&mut cobj), "<</Length {}>>stream", content.len());
        cobj.extend_from_slice(content);
        cobj.extend_from_slice(b"\nendstream");

        let mut page_dict =
            Vec::from(&b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Resources "[..]);
        page_dict.extend_from_slice(resources);
        page_dict.extend_from_slice(b"/Contents 4 0 R>>");

        let mut objects = vec![
            b"<</Type/Catalog/Pages 2 0 R>>".to_vec(),
            b"<</Type/Pages/Kids[3 0 R]/Count 1>>".to_vec(),
            page_dict,
            cobj,
        ];
        objects.extend_from_slice(extra);
        build_pdf(&objects)
    }

    fn open_doc(bytes: Vec<u8>) -> (Document, PageIndex) {
        let budget = Budget::default();
        let base = FileBase::open(Bytes::new(bytes), &budget).expect("open test pdf");
        let doc = Document::new(Arc::new(base), None);
        let version = doc.current();
        let pages = PageIndex::build(&mut Reads::new(&version, &budget)).expect("page index");
        (doc, pages)
    }

    fn web_budget() -> Budget {
        Budget::new(&DocumentLimits::web())
    }

    fn adversarial_vectors() -> Vec<(&'static str, Vec<u8>)> {
        let type4_50k = {
            let mut prog = vec![b'{'; 50_000];
            prog.extend(std::iter::repeat_n(b'}', 50_000));
            let mut func = Vec::new();
            let _ = writeln!(
                ByteWriter(&mut func),
                "<</FunctionType 4/Domain[0 1]/Range[0 1]/Length {}>>stream",
                prog.len()
            );
            func.extend_from_slice(&prog);
            func.extend_from_slice(b"\nendstream");
            let sh = b"<</ShadingType 2/ColorSpace/DeviceGray/Coords[0 0 200 0]/Function 6 0 R>>"
                .to_vec();
            one_page(b"/Sh0 sh", b"<</Shading<</Sh0 5 0 R>>>>", &[sh, func])
        };

        let type3_cycle = {
            let sh = b"<</ShadingType 2/ColorSpace/DeviceGray/Coords[0 0 200 0]/Function 6 0 R>>"
                .to_vec();
            let func =
                b"<</FunctionType 3/Domain[0 1]/Bounds[]/Encode[0 1]/Functions[6 0 R]>>".to_vec();
            one_page(b"/Sh0 sh", b"<</Shading<</Sh0 5 0 R>>>>", &[sh, func])
        };

        let type3_chain_5k = {
            let mut extra = vec![
                b"<</ShadingType 2/ColorSpace/DeviceGray/Coords[0 0 200 0]/Function 6 0 R>>"
                    .to_vec(),
            ];
            for i in 0..5_000usize {
                extra.push(
                    format!(
                        "<</FunctionType 3/Domain[0 1]/Bounds[]/Encode[0 1]/Functions[{} 0 R]>>",
                        7 + i
                    )
                    .into_bytes(),
                );
            }
            extra.push(b"<</FunctionType 2/Domain[0 1]/C0[0]/C1[1]/N 1>>".to_vec());
            one_page(b"/Sh0 sh", b"<</Shading<</Sh0 5 0 R>>>>", &extra)
        };

        let cs_indexed_cycle = one_page(
            b"/CS0 cs 0 scn 0 0 50 50 re f",
            b"<</ColorSpace<</CS0 5 0 R>>>>",
            &[b"[/Indexed 5 0 R 0 <00>]".to_vec()],
        );

        let cs_icc_cycle = {
            let icc = b"<</N 3/Alternate 5 0 R/Length 4>>stream\nxxxx\nendstream".to_vec();
            one_page(
                b"/CS0 cs 0 0 0 scn 0 0 50 50 re f",
                b"<</ColorSpace<</CS0 5 0 R>>>>",
                &[b"[/ICCBased 6 0 R]".to_vec(), icc],
            )
        };

        let cs_chain_5k = {
            let mut extra = Vec::new();
            for i in 0..5_000usize {
                extra.push(format!("[/Indexed {} 0 R 0 <00>]", 6 + i).into_bytes());
            }
            extra.push(b"/DeviceGray".to_vec());
            one_page(
                b"/CS0 cs 0 scn 0 0 50 50 re f",
                b"<</ColorSpace<</CS0 5 0 R>>>>",
                &extra,
            )
        };

        let dag_deeper_cs = {
            let mut extra = Vec::new();
            for i in 0..20usize {
                extra.push(format!("[/Indexed {} 0 R 0 <00>]", 6 + i).into_bytes());
            }
            extra.push(b"/DeviceGray".to_vec());
            for i in 0..15usize {
                let target = if i + 1 < 15 { 27 + i } else { 5 };
                extra.push(format!("[/Indexed {target} 0 R 0 <00>]").into_bytes());
            }
            one_page(
                b"/CS0 cs 0 scn /CS1 cs 0 scn",
                b"<</ColorSpace<</CS0 5 0 R/CS1 26 0 R>>>>",
                &extra,
            )
        };

        let image_smask_cs = {
            let mut extra = Vec::new();
            extra.push(
                b"<</Type/XObject/Subtype/Image/Width 1/Height 1/BitsPerComponent 8/ColorSpace/DeviceGray/SMask 6 0 R/Length 1>>stream\n\x00\nendstream".to_vec(),
            );
            extra.push(
                b"<</Type/XObject/Subtype/Image/Width 1/Height 1/BitsPerComponent 8/ColorSpace 7 0 R/Length 1>>stream\n\x00\nendstream".to_vec(),
            );
            for i in 0..40usize {
                extra.push(format!("[/Indexed {} 0 R 0 <00>]", 8 + i).into_bytes());
            }
            extra.push(b"/DeviceGray".to_vec());
            one_page(b"/Im0 Do", b"<</XObject<</Im0 5 0 R>>>>", &extra)
        };

        let inline_200k = {
            let unit = b"BI /W 1 /H 1 ID x EI EI ";
            let mut body = Vec::with_capacity(unit.len() * 200_000);
            for _ in 0..200_000 {
                body.extend_from_slice(unit);
            }
            one_page(&body, b"<<>>", &[])
        };

        let tiling_cycle = {
            let cell = b"/Pattern cs /P0 scn 0 0 10 10 re f";
            let pat = format!(
                "<</Type/Pattern/PatternType 1/PaintType 1/TilingType 1/BBox[0 0 10 10]/XStep 10/YStep 10/Resources<</Pattern<</P0 5 0 R>>>>/Length {}>>stream\n{}\nendstream",
                cell.len(),
                std::str::from_utf8(cell).unwrap()
            )
            .into_bytes();
            one_page(
                b"/Pattern cs /P0 scn 0 0 50 50 re f",
                b"<</Pattern<</P0 5 0 R>>>>",
                &[pat],
            )
        };

        let tiling_chain_2k = {
            let mut extra = Vec::with_capacity(2_000);
            for i in 0..2_000usize {
                let obj_num = 5 + i;
                let (cell, res) = if i + 1 < 2_000 {
                    (
                        format!("/Pattern cs /P{} scn 0 0 10 10 re f", i + 1),
                        format!("<</Pattern<</P{} {} 0 R>>>>", i + 1, obj_num + 1),
                    )
                } else {
                    ("1 0 0 rg 0 0 10 10 re f".to_string(), "<<>>".to_string())
                };
                let pat = format!(
                    "<</Type/Pattern/PatternType 1/PaintType 1/TilingType 1/BBox[0 0 10 10]/XStep 10/YStep 10/Resources {res}/Length {}>>stream\n{cell}\nendstream",
                    cell.len()
                )
                .into_bytes();
                extra.push(pat);
            }
            one_page(
                b"/Pattern cs /P0 scn 0 0 50 50 re f",
                b"<</Pattern<</P0 5 0 R>>>>",
                &extra,
            )
        };

        let type3_glyph_cycle = {
            let proc = b"100 0 d0 BT /F0 10 Tf (a) Tj ET";
            let cp = format!(
                "<</Length {}>>stream\n{}\nendstream",
                proc.len(),
                std::str::from_utf8(proc).unwrap()
            )
            .into_bytes();
            let font = b"<</Type/Font/Subtype/Type3/FontBBox[0 0 100 100]/FontMatrix[0.01 0 0 0.01 0 0]/CharProcs<</a 6 0 R>>/Encoding<</Type/Encoding/Differences[97 /a]>>/FirstChar 97/LastChar 97/Widths[100]/Resources<</Font<</F0 5 0 R>>>>>>".to_vec();
            one_page(
                b"BT /F0 10 Tf (a) Tj ET",
                b"<</Font<</F0 5 0 R>>>>",
                &[font, cp],
            )
        };

        let type3_chain_2k = {
            let mut extra = Vec::with_capacity(4_000);
            for i in 0..2_000usize {
                let font_num = 5 + 2 * i;
                let cp_num = font_num + 1;
                let (proc, res) = if i + 1 < 2_000 {
                    let next_font = 5 + 2 * (i + 1);
                    (
                        format!("100 0 d0 BT /F{} 10 Tf (a) Tj ET", i + 1),
                        format!("<</Font<</F{} {next_font} 0 R>>>>", i + 1),
                    )
                } else {
                    ("100 0 d0 0 0 50 50 re f".to_string(), "<<>>".to_string())
                };
                let font = format!(
                    "<</Type/Font/Subtype/Type3/FontBBox[0 0 100 100]/FontMatrix[0.01 0 0 0.01 0 0]/CharProcs<</a {cp_num} 0 R>>/Encoding<</Type/Encoding/Differences[97 /a]>>/FirstChar 97/LastChar 97/Widths[100]/Resources {res}>>"
                )
                .into_bytes();
                let cp =
                    format!("<</Length {}>>stream\n{proc}\nendstream", proc.len()).into_bytes();
                extra.push(font);
                extra.push(cp);
            }
            one_page(b"BT /F0 10 Tf (a) Tj ET", b"<</Font<</F0 5 0 R>>>>", &extra)
        };

        let form_cycle = {
            let body = b"/Fm0 Do";
            let form = format!(
                "<</Type/XObject/Subtype/Form/BBox[0 0 100 100]/Resources<</XObject<</Fm0 5 0 R>>>>/Length {}>>stream\n/Fm0 Do\nendstream",
                body.len()
            )
            .into_bytes();
            one_page(b"/Fm0 Do", b"<</XObject<</Fm0 5 0 R>>>>", &[form])
        };

        let content_600 = {
            let mut body = vec![b'['; 600];
            body.extend(std::iter::repeat_n(b']', 600));
            body.extend_from_slice(b" pop");
            one_page(&body, b"<<>>", &[])
        };

        vec![
            ("type4-braces-50000", type4_50k),
            ("type3-function-cycle", type3_cycle),
            ("type3-function-chain-5000", type3_chain_5k),
            ("cs-indexed-cycle", cs_indexed_cycle),
            ("cs-icc-alternate-cycle", cs_icc_cycle),
            ("cs-indexed-chain-5000", cs_chain_5k),
            ("dag-revisited-deeper-cs", dag_deeper_cs),
            ("image-smask-cs-chain-40", image_smask_cs),
            ("inline-image-nested-200000", inline_200k),
            ("tiling-pattern-cycle", tiling_cycle),
            ("tiling-pattern-chain-2000", tiling_chain_2k),
            ("type3-glyph-cycle", type3_glyph_cycle),
            ("type3-glyph-chain-2000", type3_chain_2k),
            ("form-xobject-cycle", form_cycle),
            ("content-operand-depth-600", content_600),
        ]
    }

    #[test]
    fn refuses_all_15_adversarial_vectors_with_object_depth() {
        for (name, pdf) in adversarial_vectors() {
            let (doc, pages) = open_doc(pdf);
            let version = doc.current();
            let budget = web_budget();
            let mut reads = Reads::new(&version, &budget);
            let err = validate_page(&mut reads, &pages.pages()[0], None)
                .expect_err(&format!("expected guard refusal for {name}"));
            assert_eq!(
                err.limit().map(LimitExceeded::code),
                Some(LimitCode::ObjectDepth),
                "{name}: unexpected error {err}"
            );
        }
    }

    #[test]
    fn inherited_resource_cycles_are_refused_without_false_positives() {
        // Form without own /Resources invoking itself from page /Resources.
        let form_self =
            b"<</Type/XObject/Subtype/Form/BBox[0 0 100 100]/Length 7>>stream\n/Fm0 Do\nendstream"
                .to_vec();
        let (doc, pages) = open_doc(one_page(
            b"/Fm0 Do",
            b"<</XObject<</Fm0 5 0 R>>>>",
            &[form_self],
        ));
        let err = validate_page(
            &mut Reads::new(&doc.current(), &web_budget()),
            &pages.pages()[0],
            None,
        )
        .unwrap_err();
        assert_eq!(
            err.limit().map(LimitExceeded::code),
            Some(LimitCode::ObjectDepth)
        );

        // Non-recursive Form without own /Resources (does NOT call /Fm0 Do) must pass.
        let form_leaf =
            b"<</Type/XObject/Subtype/Form/BBox[0 0 100 100]/Length 15>>stream\n0 0 10 10 re f\nendstream"
                .to_vec();
        let (doc, pages) = open_doc(one_page(
            b"/Fm0 Do",
            b"<</XObject<</Fm0 5 0 R>>>>",
            &[form_leaf],
        ));
        validate_page(
            &mut Reads::new(&doc.current(), &web_budget()),
            &pages.pages()[0],
            None,
        )
        .expect("non-recursive form sharing page resources must pass");

        // Type 3 font without own /Resources invoking itself from page /Resources.
        let cp = b"<</Length 31>>stream\n100 0 d0 BT /F0 10 Tf (a) Tj ET\nendstream".to_vec();
        let font = b"<</Type/Font/Subtype/Type3/FontBBox[0 0 100 100]/FontMatrix[0.01 0 0 0.01 0 0]/CharProcs<</a 6 0 R>>/Encoding<</Type/Encoding/Differences[97 /a]>>/FirstChar 97/LastChar 97/Widths[100]>>".to_vec();
        let (doc, pages) = open_doc(one_page(
            b"BT /F0 10 Tf (a) Tj ET",
            b"<</Font<</F0 5 0 R>>>>",
            &[font, cp],
        ));
        let err = validate_page(
            &mut Reads::new(&doc.current(), &web_budget()),
            &pages.pages()[0],
            None,
        )
        .unwrap_err();
        assert_eq!(
            err.limit().map(LimitExceeded::code),
            Some(LimitCode::ObjectDepth)
        );
    }

    #[test]
    fn inline_image_ei_boundaries_and_comment_string_lookahead() {
        let budget = web_budget();
        // Leading/trailing whitespace boundary on EI: `xEI ` inside image data is ignored,
        // so 20 normal inline images containing `xEI ` in their payload pass cleanly.
        let mut safe_binary = Vec::new();
        for _ in 0..20 {
            safe_binary.extend_from_slice(b"BI /W 2 /H 2 ID xEI EIx EI q Q ");
        }
        validate_content_bytes(&safe_binary, &budget)
            .expect("non-whitespace-bounded EI inside image data must not false-positive");

        // Legitimate inline images followed by text containing "EI" ("THEIR") pass.
        let mut safe_text = Vec::new();
        for _ in 0..20 {
            safe_text.extend_from_slice(b"BI /W 1 /H 1 ID x EI BT (THEIR) Tj ET ");
        }
        validate_content_bytes(&safe_text, &budget)
            .expect("inline images separated by Tj text must not false-positive");

        // Adversarial raw `EI` in comment or literal string without intervening Tj is refused.
        for unit in [
            &b"BI /W 1 /H 1 ID x EI %EI\n"[..],
            &b"BI /W 1 /H 1 ID x EI (EI) "[..],
        ] {
            let mut payload = Vec::new();
            for _ in 0..20 {
                payload.extend_from_slice(unit);
            }
            let err = validate_content_bytes(&payload, &budget).unwrap_err();
            assert_eq!(
                err.limit().map(LimitExceeded::code),
                Some(LimitCode::ObjectDepth)
            );
        }
    }

    #[test]
    fn image_mask_diamond_passes_and_thumb_deep_cs_refuses() {
        // Diamond DAG: /SMask and /Mask both reference the same valid mask stream obj 6.
        let img = b"<</Type/XObject/Subtype/Image/Width 1/Height 1/BitsPerComponent 8/ColorSpace/DeviceGray/SMask 6 0 R/Mask 6 0 R/Length 1>>stream\n\x00\nendstream".to_vec();
        let mask = b"<</Type/XObject/Subtype/Image/Width 1/Height 1/BitsPerComponent 8/ColorSpace/DeviceGray/Length 1>>stream\n\x00\nendstream".to_vec();
        let (doc, pages) = open_doc(one_page(
            b"/Im0 Do",
            b"<</XObject<</Im0 5 0 R>>>>",
            &[img, mask],
        ));
        validate_page(
            &mut Reads::new(&doc.current(), &web_budget()),
            &pages.pages()[0],
            None,
        )
        .expect("diamond image mask DAG must not false-positive");

        // Page /Thumb carrying a 40-level Indexed color space chain is refused.
        let mut extra = vec![
            b"<</Type/XObject/Subtype/Image/Width 1/Height 1/BitsPerComponent 8/ColorSpace 6 0 R/Length 1>>stream\n\x00\nendstream".to_vec(),
        ];
        for i in 0..40usize {
            extra.push(format!("[/Indexed {} 0 R 0 <00>]", 7 + i).into_bytes());
        }
        extra.push(b"/DeviceGray".to_vec());
        let mut objs = vec![
            b"<</Type/Catalog/Pages 2 0 R>>".to_vec(),
            b"<</Type/Pages/Kids[3 0 R]/Count 1>>".to_vec(),
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]/Contents 4 0 R/Thumb 5 0 R>>"
                .to_vec(),
            b"<</Length 0>>stream\n\nendstream".to_vec(),
        ];
        objs.extend(extra);
        let (doc, pages) = open_doc(build_pdf(&objs));
        let err = validate_page(
            &mut Reads::new(&doc.current(), &web_budget()),
            &pages.pages()[0],
            None,
        )
        .unwrap_err();
        assert_eq!(
            err.limit().map(LimitExceeded::code),
            Some(LimitCode::ObjectDepth)
        );
    }

    #[test]
    fn f4_preflight_cache_hits_and_invalidates_on_read_set_edit() {
        let mut objects = vec![
            b"<</Type/Catalog/Pages 2 0 R>>".to_vec(),
            b"<</Type/Pages/Kids[3 0 R]/Count 1>>".to_vec(),
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]/Resources<</ColorSpace<</CS0 5 0 R>>>>/Contents 4 0 R>>".to_vec(),
            b"<</Length 3>>stream\nq Q\nendstream".to_vec(),
            b"[/Indexed 6 0 R 0 <00>]".to_vec(),
            b"/DeviceGray".to_vec(),
            b"<</Unrelated 1>>".to_vec(),
        ];
        let (doc, pages) = open_doc(build_pdf(&objects));
        let cache = PreflightCache::default();
        let budget = web_budget();

        let v0 = doc.current();
        cache
            .validate_page(&v0, &pages.pages()[0], &budget)
            .unwrap();
        assert_eq!(
            cache.stats(),
            Stats {
                hits: 0,
                computations: 1
            }
        );

        // Repeated check on same Version hits memo (0 work).
        cache
            .validate_page(&v0, &pages.pages()[0], &budget)
            .unwrap();
        assert_eq!(
            cache.stats(),
            Stats {
                hits: 1,
                computations: 1
            }
        );

        // Edit an unrelated object (7 0 R): F4 ReadSet is disjoint -> still hits!
        let mut tx = doc.begin();
        tx.write(7, &Value::Integer(42), &budget).unwrap();
        let v1 = tx.commit(&budget).unwrap();
        let p1 = PageIndex::build(&mut Reads::new(&v1, &budget)).unwrap();
        cache.validate_page(&v1, &p1.pages()[0], &budget).unwrap();
        assert_eq!(
            cache.stats(),
            Stats {
                hits: 2,
                computations: 1
            }
        );

        // Edit a consulted leaf object (6 0 R -> cycle back to 5 0 R): invalidates and refuses!
        let mut tx = doc.begin();
        let cyclic = Value::parse(&Bytes::from(&b"[/Indexed 5 0 R 0 <00>]"[..]), &budget).unwrap();
        tx.write(6, &cyclic, &budget).unwrap();
        let v2 = tx.commit(&budget).unwrap();
        let p2 = PageIndex::build(&mut Reads::new(&v2, &budget)).unwrap();
        let err = cache
            .validate_page(&v2, &p2.pages()[0], &budget)
            .unwrap_err();
        assert_eq!(
            err.limit().map(LimitExceeded::code),
            Some(LimitCode::ObjectDepth)
        );
        assert_eq!(
            cache.stats(),
            Stats {
                hits: 2,
                computations: 2
            }
        );

        // Opt-out when max_object_depth is None does no work and succeeds.
        let opt_out = Budget::default();
        assert!(!is_active(&opt_out));
        cache.validate_page(&v2, &p2.pages()[0], &opt_out).unwrap();
        assert_eq!(
            cache.stats(),
            Stats {
                hits: 2,
                computations: 2
            }
        );
        let _ = &mut objects;
    }

    #[test]
    fn f6_cancellation_and_memory_budget_refuse_cleanly() {
        let token = CancellationToken::new();
        token.cancel();
        let cancelled = web_budget().with_cancellation(token);
        let err = validate_content_bytes(&vec![b' '; 16_384], &cancelled).unwrap_err();
        assert_eq!(
            err.limit().map(LimitExceeded::code),
            Some(LimitCode::Cancelled)
        );

        // Direct page dict edit invalidation check + memory budget refusal.
        let mut dict = Dict::new();
        dict.insert(Name::from("Type"), Value::Name(Name::from("Page")));
        let _ = dict;
        let tight_mem = web_budget().with_limit(LimitCode::MemorySize, Some(8));
        let (doc, pages) = open_doc(one_page(
            b"/CS0 cs 0 scn",
            b"<</ColorSpace<</CS0 5 0 R>>>>",
            &[b"[/Indexed /DeviceGray 0 <00>]".to_vec()],
        ));
        let err = validate_page(
            &mut Reads::new(&doc.current(), &tight_mem),
            &pages.pages()[0],
            None,
        )
        .unwrap_err();
        assert_eq!(
            err.limit().map(LimitExceeded::code),
            Some(LimitCode::MemorySize)
        );
    }

    #[test]
    fn zero_false_positives_across_real_world_corpus() {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/assets/real_world");
        let mut files: Vec<_> = fs::read_dir(&dir)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .filter(|path| path.extension().is_some_and(|e| e == "pdf"))
            .collect();
        files.sort();
        assert_eq!(files.len(), 15);
        for path in files {
            let bytes = fs::read(&path).unwrap();
            let (doc, pages) = open_doc(bytes);
            let version = doc.current();
            let mut shared = CheckedCache::new();
            for (idx, page) in pages.pages().iter().enumerate() {
                let budget = web_budget();
                let mut reads = Reads::new(&version, &budget);
                validate_page(&mut reads, page, Some(&mut shared)).unwrap_or_else(|err| {
                    panic!(
                        "false positive on {} page {}: {err}",
                        path.display(),
                        idx + 1
                    )
                });
            }
        }
    }
}
