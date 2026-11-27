//! Prototype of the DESIGN.md §6 contingency iterative pre-flight guard (#1197).
//!
//! Validates a page's reachable PDF object graph and content streams through
//! `pyropdf-store` (`Reads` + `Budget` + `BudgetVec`) *before* handing the page
//! to `hayro` / `hayro-syntax`, closing every known stack-overflow, cycle, and
//! exponential inline-image lookahead path in pinned hayro (`394e4a0d`):
//!
//! - Color space nesting and cycles (`/Indexed`, `/Separation`, `/DeviceN`,
//!   `/Pattern`, `/ICCBased /Alternate`) — bounded at `MAX_COLOR_SPACE_DEPTH` (32),
//!   matching upstream `LaurenzV/hayro#1735`.
//! - Function nesting and cycles (Type 3 `/Functions` stitching chains) — bounded
//!   at `MAX_FUNCTION_DEPTH` (32), matching upstream `LaurenzV/hayro#1665`.
//! - Type 4 PostScript procedure `{ ... }` brace nesting — scanned iteratively
//!   over decoded stream bytes and bounded at `MAX_TYPE4_BRACES` (256), matching
//!   Acrobat and `LaurenzV/hayro#1665`.
//! - Nested interpreted streams (Form XObjects, annotation `/AP /N`, SoftMask `/G`,
//!   Type 1 Tiling Patterns, Type 3 `/CharProcs`) — iterative DFS with cycle
//!   refusal and `MAX_STREAM_DEPTH` (50), covering both `hayro`'s silent truncation
//!   of Forms/SoftMasks and the unbounded recursion in `TilingPattern::interpret`
//!   and `Type3::render_glyph`.
//! - Content stream operand container nesting (`[`/`]`, `<<`/`>>`) — bounded at
//!   `MAX_CONTENT_OPERAND_DEPTH` (64), below `hayro-syntax`'s 512 silent-truncation
//!   cap (`LaurenzV/hayro#1666`).
//! - Inline image (`BI ... ID ... EI`) nested lookahead chains (#740,
//!   `LaurenzV/hayro#1832`) — detected in a single linear byte scan before
//!   `hayro-syntax::content::UntypedIter` ever runs.

use std::collections::HashSet;
use std::fmt;
use std::sync::Arc;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode, StreamKind, try_insert_set};
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::Page;
use pyropdf_store::value::{Array, Stream, Value};
use pyropdf_store::{Error as StoreError, LimitExceeded};

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
/// Maximum inline images in one stream that have a trailing `EI` before the
/// next `BI` (each such image can double `starts_with_content` lookahead work
/// in pinned hayro-syntax #740: 2^8 = 256 is negligible, 2^22 takes ~2s).
pub const MAX_INLINE_LOOKAHEAD_CHAIN: usize = 8;
/// How often loops poll the budget for deadline / cancellation.
const POLL_INTERVAL: usize = 256;

/// Why the pre-flight guard refused a page.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Refusal {
    /// A store error or `Budget` limit (memory, decoded bytes, deadline, cancel).
    Store(String),
    /// A nesting depth, cycle, or inline-image lookahead bound was exceeded.
    Nesting { kind: &'static str, detail: String },
}

impl fmt::Display for Refusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Store(msg) => f.write_str(msg),
            Self::Nesting { kind, detail } => write!(f, "{kind}: {detail}"),
        }
    }
}

impl From<StoreError> for Refusal {
    fn from(err: StoreError) -> Self {
        Self::Store(err.to_string())
    }
}

impl From<LimitExceeded> for Refusal {
    fn from(err: LimitExceeded) -> Self {
        Self::Store(err.to_string())
    }
}

/// Measurements collected while validating a page (or aggregated across a doc).
#[derive(Clone, Copy, Debug, Default)]
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
    pub peak_budget_memory_bytes: u64,
}

impl Report {
    pub fn merge(&mut self, other: Self) {
        self.streams_scanned += other.streams_scanned;
        self.content_bytes_scanned += other.content_bytes_scanned;
        self.type4_streams_scanned += other.type4_streams_scanned;
        self.type4_bytes_scanned += other.type4_bytes_scanned;
        self.objects_visited += other.objects_visited;
        self.max_cs_depth = self.max_cs_depth.max(other.max_cs_depth);
        self.max_fn_depth = self.max_fn_depth.max(other.max_fn_depth);
        self.max_type4_braces = self.max_type4_braces.max(other.max_type4_braces);
        self.max_stream_depth = self.max_stream_depth.max(other.max_stream_depth);
        self.max_content_depth = self.max_content_depth.max(other.max_content_depth);
        self.inline_images_seen += other.inline_images_seen;
        self.max_inline_lookahead_chain = self
            .max_inline_lookahead_chain
            .max(other.max_inline_lookahead_chain);
        self.peak_budget_memory_bytes = self
            .peak_budget_memory_bytes
            .max(other.peak_budget_memory_bytes);
    }
}

/// Reusable validated-object memo across pages of one immutable `Version`.
#[derive(Default)]
pub struct CheckedCache {
    color_spaces: HashSet<u32>,
    functions: HashSet<u32>,
    streams: HashSet<u32>,
    resources: HashSet<u32>,
}

impl CheckedCache {
    pub fn new() -> Self {
        Self::default()
    }
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

fn resolve(reads: &mut Reads<'_>, value: &Value) -> Result<Option<Resolved>, Refusal> {
    let mut current = match value {
        Value::Reference(r) => r.number(),
        other => return Ok(Some(Resolved::Direct(other.clone()))),
    };
    let mut steps = 0usize;
    loop {
        steps += 1;
        if steps > MAX_REF_CHAIN {
            return Err(Refusal::Nesting {
                kind: "ref_chain",
                detail: format!("indirect reference chain exceeded {MAX_REF_CHAIN}"),
            });
        }
        let Some(obj) = reads.get(current)? else {
            return Ok(None);
        };
        if let Value::Reference(next) = &**obj.value() {
            current = next.number();
            continue;
        }
        return Ok(Some(Resolved::Indirect(current, Arc::clone(obj.value()))));
    }
}

/// Validate one `Page` iteratively under `reads` (recording F4 read-set and
/// charging F6 `Budget`). If `shared` is `Some`, indirect objects already
/// proven safe on earlier pages of the same `Version` are skipped in O(1).
pub fn validate_page(
    reads: &mut Reads<'_>,
    page: &Page,
    shared: Option<&mut CheckedCache>,
) -> Result<Report, Refusal> {
    let mut guard = PageGuard {
        reads,
        shared,
        local_cs: HashSet::new(),
        local_fn: HashSet::new(),
        local_streams: HashSet::new(),
        local_resources: HashSet::new(),
        report: Report::default(),
        steps: 0,
    };
    guard.check_page(page)?;
    guard.sample_memory();
    Ok(guard.report)
}

struct PageGuard<'a, 'b> {
    reads: &'a mut Reads<'b>,
    shared: Option<&'a mut CheckedCache>,
    local_cs: HashSet<u32>,
    local_fn: HashSet<u32>,
    local_streams: HashSet<u32>,
    local_resources: HashSet<u32>,
    report: Report,
    steps: usize,
}

impl<'a, 'b> PageGuard<'a, 'b> {
    fn budget(&self) -> &'b Budget {
        self.reads.budget()
    }

    fn tick(&mut self) -> Result<(), Refusal> {
        self.steps = self.steps.wrapping_add(1);
        if self.steps.is_multiple_of(POLL_INTERVAL) {
            self.budget().poll()?;
            self.sample_memory();
        }
        Ok(())
    }

    fn sample_memory(&mut self) {
        let used = self.budget().used(LimitCode::MemorySize);
        if used > self.report.peak_budget_memory_bytes {
            self.report.peak_budget_memory_bytes = used;
        }
    }

    fn is_cs_checked(&self, num: u32) -> bool {
        self.local_cs.contains(&num)
            || self
                .shared
                .as_ref()
                .is_some_and(|c| c.color_spaces.contains(&num))
    }

    fn mark_cs_checked(&mut self, num: u32) -> Result<(), Refusal> {
        let budget = self.reads.budget();
        try_insert_set(&mut self.local_cs, num, budget)?;
        if let Some(cache) = self.shared.as_deref_mut() {
            try_insert_set(&mut cache.color_spaces, num, budget)?;
        }
        Ok(())
    }

    fn is_fn_checked(&self, num: u32) -> bool {
        self.local_fn.contains(&num)
            || self
                .shared
                .as_ref()
                .is_some_and(|c| c.functions.contains(&num))
    }

    fn mark_fn_checked(&mut self, num: u32) -> Result<(), Refusal> {
        let budget = self.reads.budget();
        try_insert_set(&mut self.local_fn, num, budget)?;
        if let Some(cache) = self.shared.as_deref_mut() {
            try_insert_set(&mut cache.functions, num, budget)?;
        }
        Ok(())
    }

    fn is_stream_checked(&self, num: u32) -> bool {
        self.local_streams.contains(&num)
            || self
                .shared
                .as_ref()
                .is_some_and(|c| c.streams.contains(&num))
    }

    fn mark_stream_checked(&mut self, num: u32) -> Result<(), Refusal> {
        let budget = self.reads.budget();
        try_insert_set(&mut self.local_streams, num, budget)?;
        if let Some(cache) = self.shared.as_deref_mut() {
            try_insert_set(&mut cache.streams, num, budget)?;
        }
        Ok(())
    }

    fn is_resources_checked(&self, num: u32) -> bool {
        self.local_resources.contains(&num)
            || self
                .shared
                .as_ref()
                .is_some_and(|c| c.resources.contains(&num))
    }

    fn mark_resources_checked(&mut self, num: u32) -> Result<(), Refusal> {
        let budget = self.reads.budget();
        try_insert_set(&mut self.local_resources, num, budget)?;
        if let Some(cache) = self.shared.as_deref_mut() {
            try_insert_set(&mut cache.resources, num, budget)?;
        }
        Ok(())
    }

    fn check_page(&mut self, page: &Page) -> Result<(), Refusal> {
        // 1. Scan the page's own /Contents stream(s).
        if let Some(contents) = page.dict().get("Contents").cloned() {
            self.scan_page_contents(&contents)?;
        }

        // 2. Collect top-level nested stream work items (page resources + visible annotation appearances).
        let mut stream_stack: BudgetVec<'_, StreamFrame> = BudgetVec::new(self.budget());
        let mut active_streams: HashSet<u32> = HashSet::new();

        if let Some(resources_val) = page.resources().cloned()
            && let Some(resolved) = resolve(self.reads, &resources_val)?
        {
            self.collect_from_resources(&resolved, 0, &mut stream_stack, &active_streams)?;
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
                                fallback_resources: None,
                                depth: 1,
                            })?;
                        }
                        Value::Dict(states) => {
                            for (_, state_val) in states.iter() {
                                if let Some(s_res) = resolve(self.reads, state_val)?
                                    && matches!(s_res.value(), Value::Stream(_))
                                {
                                    stream_stack.try_push(StreamFrame::Enter {
                                        stream: s_res,
                                        fallback_resources: None,
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

        // 3. Iterative DFS over nested streams (Forms, SoftMasks, Tiling Patterns, Type 3 CharProcs).
        while let Some(frame) = stream_stack.pop() {
            self.tick()?;
            match frame {
                StreamFrame::Leave(num) => {
                    active_streams.remove(&num);
                    self.mark_stream_checked(num)?;
                }
                StreamFrame::Enter {
                    stream,
                    fallback_resources,
                    depth,
                } => {
                    if depth > self.report.max_stream_depth {
                        self.report.max_stream_depth = depth;
                    }
                    if depth > MAX_STREAM_DEPTH {
                        return Err(Refusal::Nesting {
                            kind: "stream_nesting",
                            detail: format!("nested streams exceeded {MAX_STREAM_DEPTH} levels"),
                        });
                    }
                    if let Some(num) = stream.obj_num() {
                        if active_streams.contains(&num) {
                            return Err(Refusal::Nesting {
                                kind: "stream_cycle",
                                detail: format!("cyclic nested stream at object {num}"),
                            });
                        }
                        if self.is_stream_checked(num) {
                            continue;
                        }
                        try_insert_set(&mut active_streams, num, self.reads.budget())?;
                        stream_stack.try_push(StreamFrame::Leave(num))?;
                    }
                    self.report.objects_visited += 1;
                    let Value::Stream(s) = stream.value() else {
                        continue;
                    };
                    // Decode and scan this stream's content bytes for operand nesting and #740.
                    self.scan_stream_bytes(s, StreamKind::Any)?;
                    // Check Group /CS on transparency groups if present.
                    if let Some(group_val) = s.dict().get("Group")
                        && let Some(group) = resolve(self.reads, group_val)?
                        && let Value::Dict(group_dict) = group.value()
                        && let Some(cs_val) = group_dict.get("CS")
                    {
                        self.check_color_space(cs_val)?;
                    }
                    // Recurse iteratively into stream's own /Resources (or Type 3 font /Resources).
                    let own_res = match s.dict().get("Resources") {
                        Some(res_val) => resolve(self.reads, res_val)?,
                        None => fallback_resources,
                    };
                    if let Some(res) = own_res {
                        self.collect_from_resources(
                            &res,
                            depth,
                            &mut stream_stack,
                            &active_streams,
                        )?;
                    }
                }
            }
        }
        Ok(())
    }

    fn scan_page_contents(&mut self, contents: &Value) -> Result<(), Refusal> {
        let Some(resolved) = resolve(self.reads, contents)? else {
            return Ok(());
        };
        match resolved.value() {
            Value::Stream(stream) => self.scan_stream_bytes(stream, StreamKind::PageContent),
            Value::Array(arr) => {
                // Concatenate or scan each stream; scanning individually is conservative for
                // container balance if split across streams, so join only when multiple streams exist.
                if arr.len() == 1 {
                    if let Some(first) = arr.iter().next()
                        && let Some(res) = resolve(self.reads, first)?
                        && let Value::Stream(stream) = res.value()
                    {
                        return self.scan_stream_bytes(stream, StreamKind::PageContent);
                    }
                    return Ok(());
                }
                let mut joined: BudgetVec<'_, u8> = BudgetVec::new(self.budget());
                for item in arr.iter() {
                    self.tick()?;
                    if let Some(res) = resolve(self.reads, item)?
                        && let Value::Stream(stream) = res.value()
                    {
                        let decoded = stream.decoded_in(self.reads.version(), self.budget())?;
                        self.budget().check(
                            LimitCode::PageContentSize,
                            (joined.len() + decoded.len()) as u64,
                        )?;
                        joined.try_extend_from_slice(&decoded)?;
                        joined.try_push(b' ')?;
                    }
                }
                if !joined.is_empty() {
                    self.report.streams_scanned += 1;
                    self.report.content_bytes_scanned += joined.len() as u64;
                    self.scan_content_bytes(joined.as_slice())?;
                }
                Ok(())
            }
            _ => Ok(()),
        }
    }

    fn scan_stream_bytes(&mut self, stream: &Stream, kind: StreamKind) -> Result<(), Refusal> {
        let decoded = match stream.decoded_in(self.reads.version(), self.budget()) {
            Ok(bytes) => bytes,
            // Streams with unsupported/corrupt filters are skipped by hayro rather than overflowing.
            Err(err) if err.limit().is_some() => return Err(err.into()),
            Err(_) => return Ok(()),
        };
        if kind == StreamKind::PageContent {
            self.budget()
                .check(LimitCode::PageContentSize, decoded.len() as u64)?;
        }
        self.report.streams_scanned += 1;
        self.report.content_bytes_scanned += decoded.len() as u64;
        self.scan_content_bytes(&decoded)
    }

    /// Inspect every resource sub-dictionary that hayro interprets:
    /// `/ColorSpace`, `/Shading`, `/ExtGState`, `/Pattern`, `/XObject`, `/Font`.
    fn collect_from_resources(
        &mut self,
        resources: &Resolved,
        depth: usize,
        stream_stack: &mut BudgetVec<'_, StreamFrame>,
        active_streams: &HashSet<u32>,
    ) -> Result<(), Refusal> {
        if let Some(num) = resources.obj_num()
            && self.is_resources_checked(num)
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
                self.check_ext_g_state(val, depth, stream_stack)?;
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
                            stream_stack.try_push(StreamFrame::Enter {
                                stream: pat,
                                fallback_resources: None,
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
                            fallback_resources: None,
                            depth: depth + 1,
                        })?;
                    }
                    Some(b"Image") => {
                        if let Some(cs) = stream
                            .dict()
                            .get("ColorSpace")
                            .or_else(|| stream.dict().get("CS"))
                        {
                            self.check_color_space(cs)?;
                        }
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
                let Some(font) = resolve(self.reads, val)? else {
                    continue;
                };
                let Value::Dict(f_dict) = font.value() else {
                    continue;
                };
                let is_type3 = matches!(
                    f_dict.get("Subtype"),
                    Some(Value::Name(n)) if n.as_bytes() == b"Type3"
                );
                if !is_type3 {
                    continue;
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
                                fallback_resources: font_resources.clone(),
                                depth: depth + 1,
                            })?;
                        }
                    }
                }
            }
        }

        // Only cache a Resources dictionary when we're not inside an active cycle path
        // (so every cycle is caught before any object on it is marked clean).
        if active_streams.is_empty()
            && let Some(num) = resources.obj_num()
        {
            self.mark_resources_checked(num)?;
        }
        Ok(())
    }

    fn check_shading(&mut self, value: &Value) -> Result<(), Refusal> {
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
        depth: usize,
        stream_stack: &mut BudgetVec<'_, StreamFrame>,
    ) -> Result<(), Refusal> {
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
                    fallback_resources: None,
                    depth: depth + 1,
                })?;
            }
        }
        Ok(())
    }

    fn check_function_or_array(&mut self, value: &Value) -> Result<(), Refusal> {
        let Some(resolved) = resolve(self.reads, value)? else {
            return Ok(());
        };
        match resolved.value() {
            Value::Array(arr) => {
                for item in arr.iter() {
                    self.check_function(item)?;
                }
                Ok(())
            }
            Value::Name(_) => Ok(()),
            _ => self.check_function_resolved(resolved),
        }
    }

    /// Iterative DFS over PDF functions (Type 3 stitching + Type 4 PostScript).
    fn check_function(&mut self, value: &Value) -> Result<(), Refusal> {
        let Some(resolved) = resolve(self.reads, value)? else {
            return Ok(());
        };
        self.check_function_resolved(resolved)
    }

    fn check_function_resolved(&mut self, root: Resolved) -> Result<(), Refusal> {
        enum Frame {
            Enter { item: Resolved, depth: usize },
            Leave(u32),
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
                Frame::Leave(num) => {
                    active.remove(&num);
                    self.mark_fn_checked(num)?;
                }
                Frame::Enter { item, depth } => {
                    if depth > self.report.max_fn_depth {
                        self.report.max_fn_depth = depth;
                    }
                    if depth > MAX_FUNCTION_DEPTH {
                        return Err(Refusal::Nesting {
                            kind: "function_depth",
                            detail: format!(
                                "function nesting exceeded {MAX_FUNCTION_DEPTH} levels"
                            ),
                        });
                    }
                    if let Some(num) = item.obj_num() {
                        if active.contains(&num) {
                            return Err(Refusal::Nesting {
                                kind: "function_cycle",
                                detail: format!("cyclic function graph at object {num}"),
                            });
                        }
                        if self.is_fn_checked(num) {
                            continue;
                        }
                        try_insert_set(&mut active, num, self.budget())?;
                        stack.try_push(Frame::Leave(num))?;
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
    fn check_type4_stream(&mut self, stream: &Stream) -> Result<(), Refusal> {
        let decoded = match stream.decoded_in(self.reads.version(), self.budget()) {
            Ok(bytes) => bytes,
            Err(err) if err.limit().is_some() => return Err(err.into()),
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
                        return Err(Refusal::Nesting {
                            kind: "type4_braces",
                            detail: format!(
                                "Type 4 PostScript procedure exceeded {MAX_TYPE4_BRACES} nested braces"
                            ),
                        });
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
    fn check_color_space(&mut self, value: &Value) -> Result<(), Refusal> {
        enum Frame {
            Enter { item: Resolved, depth: usize },
            Leave(u32),
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
                Frame::Leave(num) => {
                    active.remove(&num);
                    self.mark_cs_checked(num)?;
                }
                Frame::Enter { item, depth } => {
                    if depth > self.report.max_cs_depth {
                        self.report.max_cs_depth = depth;
                    }
                    if depth > MAX_COLOR_SPACE_DEPTH {
                        return Err(Refusal::Nesting {
                            kind: "color_space_depth",
                            detail: format!(
                                "color space nesting exceeded {MAX_COLOR_SPACE_DEPTH} levels"
                            ),
                        });
                    }
                    if let Some(num) = item.obj_num() {
                        if active.contains(&num) {
                            return Err(Refusal::Nesting {
                                kind: "color_space_cycle",
                                detail: format!("cyclic color space at object {num}"),
                            });
                        }
                        if self.is_cs_checked(num) {
                            continue;
                        }
                        try_insert_set(&mut active, num, self.budget())?;
                        stack.try_push(Frame::Leave(num))?;
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

    /// Single linear iterative pass over decoded content bytes.
    ///
    /// Checks:
    /// 1. Operand container nesting (`[`/`]`, `<<`/`>>`) <= `MAX_CONTENT_OPERAND_DEPTH`
    ///    (prevents `hayro-syntax`'s silent truncation past 512, #15 / #1666).
    /// 2. Inline image (`BI ... ID ... EI`) nested lookahead hazards (#740):
    ///    tracks inline images whose post-`EI` tail contains another `EI` before
    ///    the next `BI`, which triggers `O(2^n)` time and `O(n)` stack frames
    ///    in `hayro-syntax::content::read_inline_image`.
    fn scan_content_bytes(&mut self, data: &[u8]) -> Result<(), Refusal> {
        let mut i = 0usize;
        let len = data.len();
        let mut depth = 0usize;
        let mut max_depth = 0usize;
        let mut inline_images = 0u64;
        let mut lookahead_chain = 0usize;
        let mut max_lookahead_chain = 0usize;
        let mut prev_was_bi = false;

        while i < len {
            if i.is_multiple_of(8192) {
                self.budget().poll()?;
            }
            let b = data[i];
            match b {
                b'%' => {
                    i += 1;
                    while i < len && data[i] != b'\n' && data[i] != b'\r' {
                        i += 1;
                    }
                }
                b'(' => {
                    // Iterative balanced literal string scan.
                    i += 1;
                    let mut parens = 1usize;
                    while i < len && parens > 0 {
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
                b'<' => {
                    if i + 1 < len && data[i + 1] == b'<' {
                        depth += 1;
                        if depth > max_depth {
                            max_depth = depth;
                        }
                        if depth > MAX_CONTENT_OPERAND_DEPTH {
                            return Err(Refusal::Nesting {
                                kind: "content_operand_depth",
                                detail: format!(
                                    "content operand nesting exceeded {MAX_CONTENT_OPERAND_DEPTH} levels"
                                ),
                            });
                        }
                        i += 2;
                    } else {
                        // Hex string `<...>`
                        i += 1;
                        while i < len && data[i] != b'>' {
                            i += 1;
                        }
                        if i < len {
                            i += 1;
                        }
                    }
                }
                b'>' if i + 1 < len && data[i + 1] == b'>' => {
                    depth = depth.saturating_sub(1);
                    i += 2;
                }
                b'[' => {
                    depth += 1;
                    if depth > max_depth {
                        max_depth = depth;
                    }
                    if depth > MAX_CONTENT_OPERAND_DEPTH {
                        return Err(Refusal::Nesting {
                            kind: "content_operand_depth",
                            detail: format!(
                                "content operand nesting exceeded {MAX_CONTENT_OPERAND_DEPTH} levels"
                            ),
                        });
                    }
                    i += 1;
                }
                b']' => {
                    depth = depth.saturating_sub(1);
                    i += 1;
                }
                b'/' => {
                    // Name token: skip regular bytes.
                    i += 1;
                    while i < len && is_regular(data[i]) {
                        i += 1;
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
                        inline_images += 1;
                        prev_was_bi = true;
                        // Scan inline image dict up to standalone `ID` + one whitespace,
                        // checking container depth inside the inline dict too.
                        let Some(after_id) = scan_inline_dict(data, i, depth, &mut max_depth)?
                        else {
                            break;
                        };
                        // Find first candidate `EI` followed by whitespace or EOF.
                        let Some(ei_pos) = find_candidate_ei(data, after_id) else {
                            break;
                        };
                        i = (ei_pos + 2).min(len);
                    } else if tok == b"EI" && prev_was_bi {
                        // Stray `EI` immediately following an inline image before the next `BI`:
                        // this is the exact trigger for `hayro-syntax`'s recursive
                        // `starts_with_content` lookahead (#740).
                        lookahead_chain += 1;
                        if lookahead_chain > max_lookahead_chain {
                            max_lookahead_chain = lookahead_chain;
                        }
                        if lookahead_chain > MAX_INLINE_LOOKAHEAD_CHAIN {
                            return Err(Refusal::Nesting {
                                kind: "inline_image_lookahead",
                                detail: format!(
                                    "nested inline-image lookahead hazard exceeded {MAX_INLINE_LOOKAHEAD_CHAIN} stray EI triggers (#740)"
                                ),
                            });
                        }
                        prev_was_bi = false;
                    }
                }
                _ => {
                    i += 1;
                }
            }
        }
        if max_depth > self.report.max_content_depth {
            self.report.max_content_depth = max_depth;
        }
        self.report.inline_images_seen += inline_images;
        if max_lookahead_chain > self.report.max_inline_lookahead_chain {
            self.report.max_inline_lookahead_chain = max_lookahead_chain;
        }
        Ok(())
    }
}

enum StreamFrame {
    Enter {
        stream: Resolved,
        fallback_resources: Option<Resolved>,
        depth: usize,
    },
    Leave(u32),
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
    max_depth: &mut usize,
) -> Result<Option<usize>, Refusal> {
    let len = data.len();
    let mut depth = base_depth;
    while i < len {
        match data[i] {
            b'%' => {
                i += 1;
                while i < len && data[i] != b'\n' && data[i] != b'\r' {
                    i += 1;
                }
            }
            b'(' => {
                i += 1;
                let mut parens = 1usize;
                while i < len && parens > 0 {
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
                if depth > MAX_CONTENT_OPERAND_DEPTH {
                    return Err(Refusal::Nesting {
                        kind: "content_operand_depth",
                        detail: format!(
                            "inline image dict nesting exceeded {MAX_CONTENT_OPERAND_DEPTH} levels"
                        ),
                    });
                }
                i += 2;
            }
            b'<' => {
                i += 1;
                while i < len && data[i] != b'>' {
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
                if depth > MAX_CONTENT_OPERAND_DEPTH {
                    return Err(Refusal::Nesting {
                        kind: "content_operand_depth",
                        detail: format!(
                            "inline image dict nesting exceeded {MAX_CONTENT_OPERAND_DEPTH} levels"
                        ),
                    });
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
fn find_candidate_ei(data: &[u8], start: usize) -> Option<usize> {
    let len = data.len();
    let mut i = start;
    while i + 1 < len {
        if data[i] == b'E' && data[i + 1] == b'I' {
            let after_ok = i + 2 == len || is_whitespace(data[i + 2]);
            if after_ok {
                return Some(i);
            }
        }
        i += 1;
    }
    None
}
