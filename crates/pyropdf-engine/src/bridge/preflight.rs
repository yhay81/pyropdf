//! Temporary iterative pre-flight nesting, operator fuel, and inline-image guard
//! before `hayro` (DESIGN.md §6 contingency for yhay81/pyropdf#15, #17, #105,
//! #740, and #1242; tracked in #7, #1241, and #1275).
//!
//! Validates a page's reachable PDF object graph and content streams through
//! `pyropdf-store` (`Reads` + `Budget` + `BudgetVec` + `try_insert_map` /
//! `try_insert_set`) *before* handing the page to `hayro` / `hayro-syntax`,
//! closing every known uncatchable `SIGSEGV` stack-overflow, cycle, silent
//! truncation, exponential/quadratic inline-image scan, unbounded function
//! construction, and exponential Form-bomb path in pinned `hayro`:
//!
//! - Color space nesting and cycles (`/Indexed`, `/Separation`, `/DeviceN`,
//!   `/Pattern`, `/ICCBased /Alternate`) — bounded at [`MAX_COLOR_SPACE_DEPTH`] (32),
//!   matching upstream `LaurenzV/hayro#1735`.
//! - Function nesting and cycles (Type 3 `/Functions` stitching chains) — bounded
//!   at [`MAX_FUNCTION_DEPTH`] (32), matching upstream `LaurenzV/hayro#1665`.
//! - Type 0 sampled function sample-table size (`product(/Size) * outputs`) —
//!   bounded at [`MAX_TYPE0_SAMPLES`] (1,048,576) before `hayro` unpacks it (#1275).
//! - Type 4 PostScript procedure `{ ... }` brace nesting — scanned iteratively
//!   over decoded stream bytes and bounded at [`MAX_TYPE4_BRACES`] (256), matching
//!   Acrobat and `LaurenzV/hayro#1665`, plus total PostScript operator count
//!   bounded at [`MAX_TYPE4_OPERATORS`] (16,384) (#1275).
//! - Nested interpreted streams (Form XObjects, annotation `/AP /N`, SoftMask `/G`,
//!   Type 1 Tiling Patterns, Type 3 `/CharProcs`) — iterative DFS with cycle
//!   refusal and [`MAX_STREAM_DEPTH`] (50), covering `hayro`'s silent truncation
//!   of Forms/SoftMasks and unbounded recursion in `TilingPattern::interpret`
//!   and `Type3::render_glyph` (#1242).
//! - DAG effective operator fuel counting (`LimitCode::InterpretationFuel`, #1275) —
//!   iterative bottom-up multiplicity propagation across page `/Contents`, nested
//!   Form XObjects, visible annotation `/AP /N`, SoftMask `/G`, tiling pattern
//!   cells (times tile copies), and Type 3 `/CharProcs`, catching exponential Form
//!   bombs (e.g., 20-level binary fan-out charging `>= 2^20`) in microseconds.
//! - Image XObject `/ColorSpace` (or `/CS`), `/SMask`, `/Mask`, and page `/Thumb`
//!   color-space chains and cycles.
//! - Content stream operand container nesting (`[`/`]`, `<<`/`>>`) — bounded at
//!   `min(max_object_depth, MAX_CONTENT_OPERAND_DEPTH)`, below `hayro-syntax`'s
//!   512 silent-truncation cap (`LaurenzV/hayro#1666`).
//! - Inline image (`BI ... ID ... EI`) nested lookahead chains (#740,
//!   `LaurenzV/hayro#1832`) and quadratic candidate `EI` scan counts (#17,
//!   `LaurenzV/hayro#1667`, capped at [`MAX_INLINE_IMAGE_EI_CANDIDATES`]) —
//!   detected in a single linear byte scan before `hayro-syntax::content::UntypedIter`
//!   runs.
//!
//! Active whenever either `LimitCode::ObjectDepth` (`DocumentLimits.max_object_depth`)
//! or `LimitCode::InterpretationFuel` is set (active by default in
//! `DocumentLimits.web()`, opt-out when both are `None`).
//! Memoized per immutable `Version` in [`PreflightCache`] via F4 `Memo` (keyed by
//! both operand-depth and fuel limits) so repeated operations on an unedited page
//! pay ~0 ms while tightening either bound separates cache entries.
//!
//! Removal condition (tracked in yhay81/pyropdf#7): delete this module once
//! pyropdf adopts a released `hayro` version containing bounded CS/Function/Type 4
//! construction with error signals (#15), non-nested and linear-time inline-image
//! end lookahead (#740, #17), `MAX_NESTED_INTERPRETATION_DEPTH` enforcement in
//! `TilingPattern::interpret` and `Type3::render_glyph` (#1242), and cooperative
//! interpreter fuel/cancellation (#105).

use std::cell::Cell;
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
/// Maximum PostScript tokens/operators in a Type 4 calculator function stream
/// before `hayro-interpret` parses and evaluates it (#1275).
pub const MAX_TYPE4_OPERATORS: usize = 16_384;
/// Maximum total sample values (`product(/Size) * outputs`) in a Type 0 sampled
/// function before `hayro-interpret` allocates and unpacks it (#1275).
pub const MAX_TYPE0_SAMPLES: u64 = 1_048_576;
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
/// Maximum candidate `EI` markers scanned per inline image (#17 /
/// `LaurenzV/hayro#1667`), refusing quadratic `BI ... ID <many EI> ... EI`
/// scans in `hayro-syntax::content::read_inline_image`.
pub const MAX_INLINE_IMAGE_EI_CANDIDATES: usize = 64;
/// Default per-page effective operator fuel budget when the pre-flight guard is
/// active via `DocumentLimits.max_object_depth` and `LimitCode::InterpretationFuel`
/// has no explicit override. Matches `LimitCode::ContentOperationCount`'s
/// default (`1_048_576`, ~90x the largest page in the real-world corpus) while
/// refusing exponential Form XObject bombs (e.g., 20-level binary fan-out) in
/// microseconds.
pub const DEFAULT_INTERPRETATION_FUEL: u64 = 1_048_576;
/// Default F4 memo capacity in pages (covers `DocumentLimits.web().max_pages` = 200).
pub const DEFAULT_CACHE_CAPACITY: usize = 256;
/// Cap on the lightweight `q`/`Q` graphics-state stack tracked during content scanning.
const MAX_GSTATE_DEPTH: usize = 1024;
/// How often graph loops poll the budget for deadline / cancellation.
const POLL_INTERVAL: usize = 256;
/// How often byte scanners poll the budget for deadline / cancellation.
const BYTE_POLL_INTERVAL: usize = 8192;
/// Operators that terminate `hayro-syntax`'s `starts_with_content` lookahead early.
const LOOKAHEAD_OPERATOR_LIMIT: usize = 20;

/// Returns whether the pre-flight guard is active for `budget`.
///
/// Active whenever either `LimitCode::ObjectDepth` (`DocumentLimits.max_object_depth`)
/// or `LimitCode::InterpretationFuel` is bounded (`Some(_)`), and opted out when
/// both are `None`.
#[must_use]
pub const fn is_active(budget: &Budget) -> bool {
    budget.limit(LimitCode::ObjectDepth).is_some()
        || budget.limit(LimitCode::InterpretationFuel).is_some()
}

/// Returns the effective content operand nesting bound when `ObjectDepth` is
/// bounded, or `None` when `ObjectDepth` is unset.
#[must_use]
pub fn content_operand_limit(budget: &Budget) -> Option<usize> {
    budget.limit(LimitCode::ObjectDepth).map(|limit| {
        usize::try_from(limit)
            .unwrap_or(usize::MAX)
            .min(MAX_CONTENT_OPERAND_DEPTH)
    })
}

/// Returns the effective per-page operator fuel limit for `budget`.
///
/// Honors an explicit `LimitCode::InterpretationFuel` bound when set; otherwise
/// defaults to [`DEFAULT_INTERPRETATION_FUEL`] whenever `LimitCode::ObjectDepth`
/// is bounded (such as `DocumentLimits.web()`). Returns `None` when both are unset.
#[must_use]
pub const fn interpretation_fuel_limit(budget: &Budget) -> Option<u64> {
    match budget.limit(LimitCode::InterpretationFuel) {
        Some(limit) => Some(limit),
        None => {
            if budget.limit(LimitCode::ObjectDepth).is_some() {
                Some(DEFAULT_INTERPRETATION_FUEL)
            } else {
                None
            }
        }
    }
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
    pub max_type4_operators: usize,
    pub max_type0_samples: u64,
    pub max_stream_depth: usize,
    pub max_content_depth: usize,
    pub inline_images_seen: u64,
    pub max_inline_lookahead_chain: usize,
    pub max_inline_ei_candidates: usize,
    pub effective_operators: u64,
}

/// Deterministic identity for an effective `/Resources` dictionary within one `Version`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
enum ResourcesId {
    None,
    Indirect(u32),
    PageDirect,
    StreamOwn(u32),
    FontOwn(u32),
    Anonymous(u64),
}

/// Memo key for a stream interpreted with a particular effective `/Resources` dictionary.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
struct FuelKey {
    stream: u32,
    resources: ResourcesId,
}

/// Reusable depth-aware validated-object memo across pages of one immutable `Version`.
///
/// Each entry records the *maximum* entry depth at which the indirect object's
/// entire reachable subgraph has already been proven safe (never a boolean
/// visited set). Re-visiting an object at `depth <= recorded` is guaranteed
/// safe; re-visiting at `depth > recorded` re-walks the subgraph so shared DAG
/// nodes cannot bypass depth bounds. Also caches non-truncated `(effective_ops,
/// subtree_height)` per `(stream, resources)` pair so multi-page walks still
/// aggregate full per-page DAG fuel without re-decoding shared Form XObjects.
#[derive(Default)]
pub struct CheckedCache {
    color_spaces: HashMap<u32, usize>,
    functions: HashMap<u32, usize>,
    streams: HashMap<u32, usize>,
    resources: HashMap<u32, usize>,
    stream_fuel: HashMap<FuelKey, (u64, usize)>,
}

impl CheckedCache {
    /// Returns an empty depth-aware cache.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Merges validated local depth and fuel maps into this cache after a page passes.
    fn merge_from(
        &mut self,
        cs: &HashMap<u32, usize>,
        fns: &HashMap<u32, usize>,
        streams: &HashMap<u32, usize>,
        resources: &HashMap<u32, usize>,
        fuel: &HashMap<FuelKey, (u64, usize)>,
        budget: &Budget,
    ) -> Result<()> {
        merge_depth_map(&mut self.color_spaces, cs, budget)?;
        merge_depth_map(&mut self.functions, fns, budget)?;
        merge_depth_map(&mut self.streams, streams, budget)?;
        merge_depth_map(&mut self.resources, resources, budget)?;
        for (&key, &val) in fuel {
            if !self.stream_fuel.contains_key(&key) {
                try_insert_map(&mut self.stream_fuel, key, val, budget)?;
            }
        }
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
    content_operand_limit: Option<usize>,
    interpretation_fuel_limit: Option<u64>,
}

/// F4-memoized per-page pre-flight cache (DESIGN.md §5 and §6).
///
/// Holds weak references to versions and the exact `ReadSet` of every object
/// consulted while validating each page, so repeated operations on an unedited
/// page pay ~0 ms and any edit to a consulted object invalidates only affected
/// pages. Stores the validated page's effective operator count so cache hits
/// still charge `LimitCode::InterpretationFuel` on the caller's `Budget`.
pub struct PreflightCache {
    memo: Memo<PreflightKey, u64>,
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

    /// Validates `page` on `version` when `budget` bounds `ObjectDepth` or
    /// `InterpretationFuel`, reusing any valid F4 memo entry.
    pub fn validate_page(
        &self,
        version: &Arc<Version>,
        page: &Page,
        budget: &Budget,
    ) -> Result<()> {
        if !is_active(budget) {
            return Ok(());
        }
        let operand_limit = content_operand_limit(budget);
        let fuel_limit = interpretation_fuel_limit(budget);
        if let Some(reference) = page.object() {
            let key = PreflightKey {
                page: reference,
                content_operand_limit: operand_limit,
                interpretation_fuel_limit: fuel_limit,
            };
            let computed = Cell::new(false);
            let effective_ops = self.memo.get_or_compute(&key, version, budget, |reads| {
                computed.set(true);
                validate_page(reads, page, None).map(|report| report.effective_operators)
            })?;
            if !computed.get() {
                // On a memo hit `validate_page` did not run for this call, so
                // still charge the cached page's effective operators to this
                // operation's fuel meter (catching cumulative multi-page exhaustion).
                charge_page_fuel(budget, *effective_ops, fuel_limit)?;
            }
            Ok(())
        } else {
            let mut reads = Reads::new(version, budget);
            validate_page(&mut reads, page, None).map(|_| ())
        }
    }

    /// Validates each page in `indexes` of `pages` on `version` when `budget`
    /// bounds `ObjectDepth` or `InterpretationFuel`, deduplicating via the F4 memo.
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
/// Active whenever `budget` bounds [`LimitCode::ObjectDepth`] or
/// [`LimitCode::InterpretationFuel`]. When `ObjectDepth` is bounded, refuses
/// operand container nesting beyond `min(max_object_depth, MAX_CONTENT_OPERAND_DEPTH)`
/// and `#740` nested inline-image lookahead chains with `LimitCode::ObjectDepth`.
/// Always caps inline-image candidate `EI` scans at [`MAX_INLINE_IMAGE_EI_CANDIDATES`]
/// with `LimitCode::InterpretationFuel`, and enforces an explicit
/// `LimitCode::InterpretationFuel` operator bound when configured.
pub fn validate_content_bytes(data: &[u8], budget: &Budget) -> Result<()> {
    if !is_active(budget) {
        return Ok(());
    }
    let operand_limit = content_operand_limit(budget);
    let explicit_fuel = budget.limit(LimitCode::InterpretationFuel);
    let mut report = Report::default();
    let own_ops = scan_content_bytes(
        data,
        operand_limit,
        Some(MAX_INLINE_IMAGE_EI_CANDIDATES),
        explicit_fuel,
        budget,
        &mut report,
        None,
    )?;
    if explicit_fuel.is_some() {
        charge_page_fuel(budget, own_ops, explicit_fuel)?;
    }
    Ok(())
}

/// Validates one `Page` iteratively under `reads` (recording the full F4
/// read-set and charging F6 `Budget`).
///
/// Active whenever `reads.budget()` bounds [`LimitCode::ObjectDepth`] or
/// [`LimitCode::InterpretationFuel`]. When `shared` is `Some`, indirect objects
/// already proven safe at `>= depth` on earlier pages of the same immutable
/// `Version` are skipped in O(1), and new proofs are flushed into `shared` only
/// if the page passes completely.
pub fn validate_page(
    reads: &mut Reads<'_>,
    page: &Page,
    shared: Option<&mut CheckedCache>,
) -> Result<Report> {
    if !is_active(reads.budget()) {
        return Ok(Report::default());
    }
    let operand_limit = content_operand_limit(reads.budget());
    let fuel_limit = interpretation_fuel_limit(reads.budget());
    let (report, local_cs, local_fn, local_streams, local_resources, local_fuel) = {
        let mut guard = PageGuard {
            reads,
            shared: shared.as_deref(),
            operand_limit,
            fuel_limit,
            local_cs: HashMap::new(),
            local_fn: HashMap::new(),
            local_streams: HashMap::new(),
            local_resources: HashMap::new(),
            local_scans: HashMap::new(),
            type3_hints: HashMap::new(),
            local_fuel: HashMap::new(),
            next_anon_res: 1,
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
            guard.local_fuel,
        )
    };
    if let Some(cache) = shared {
        cache.merge_from(
            &local_cs,
            &local_fn,
            &local_streams,
            &local_resources,
            &local_fuel,
            reads.budget(),
        )?;
    }
    Ok(report)
}

/// Checks `effective_ops` against `fuel_limit` and charges `LimitCode::InterpretationFuel`.
fn charge_page_fuel(budget: &Budget, effective_ops: u64, fuel_limit: Option<u64>) -> Result<()> {
    if let Some(limit) = fuel_limit
        && effective_ops > limit
    {
        return Err(fuel_refusal(
            "page effective operator fuel exceeded limit",
            limit,
            effective_ops,
        ));
    }
    budget
        .charge(LimitCode::InterpretationFuel, effective_ops)
        .map_err(Error::from)
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

/// Constructs a `LimitCode::InterpretationFuel` refusal with a clear context
/// message and numeric `(limit, observed)` bounds.
fn fuel_refusal(context: &str, limit: u64, observed: u64) -> Error {
    Error::from(LimitExceeded::over(
        LimitCode::InterpretationFuel,
        limit,
        observed,
    ))
    .context(context)
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

fn resolve(reads: &mut Reads<'_>, value: &Value, strict_depth: bool) -> Result<Option<Resolved>> {
    let mut current = match value {
        Value::Reference(r) => r.number(),
        other => return Ok(Some(Resolved::Direct(other.clone()))),
    };
    let mut steps = 0usize;
    loop {
        steps += 1;
        if steps > MAX_REF_CHAIN {
            if strict_depth {
                return Err(depth_refusal(
                    "indirect reference chain too deep",
                    MAX_REF_CHAIN,
                    steps,
                ));
            }
            return Ok(None);
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
fn record_stream_filter_refs(
    reads: &mut Reads<'_>,
    stream: &Stream,
    strict_depth: bool,
) -> Result<()> {
    for key in ["Filter", "DecodeParms"] {
        if let Some(val) = stream.dict().get(key) {
            record_value_refs_shallow(reads, val, strict_depth)?;
        }
    }
    Ok(())
}

fn record_value_refs_shallow(
    reads: &mut Reads<'_>,
    value: &Value,
    strict_depth: bool,
) -> Result<()> {
    match value {
        Value::Reference(r) => {
            if let Some(resolved) = resolve(reads, &Value::Reference(*r), strict_depth)?
                && let Value::Array(arr) = resolved.value()
            {
                for item in arr.iter() {
                    if matches!(item, Value::Reference(_)) {
                        let _ = resolve(reads, item, strict_depth)?;
                    }
                }
            }
        }
        Value::Array(arr) => {
            for item in arr.iter() {
                if matches!(item, Value::Reference(_)) {
                    let _ = resolve(reads, item, strict_depth)?;
                }
            }
        }
        _ => {}
    }
    Ok(())
}

fn budgeted_bytes(slice: &[u8], budget: &Budget) -> Result<Box<[u8]>> {
    let mut buf = BudgetVec::new(budget);
    buf.try_extend_from_slice(slice)?;
    Ok(buf.into_vec().into_boxed_slice())
}

/// Compact per-stream scan result cached within one `PageGuard` so no stream is
/// decoded and scanned twice between the safety walk and the DAG fuel walk.
struct CachedStreamScan {
    own_ops: u64,
    /// `Do` invocations: `(decoded name, call count)`.
    xobjects: Vec<(Box<[u8]>, u64)>,
    /// `gs` invocations: `(decoded name, call count)`.
    ext_g_states: Vec<(Box<[u8]>, u64)>,
    /// Painted pattern invocations: `(decoded name, paint count)`.
    pattern_paints: Vec<(Box<[u8]>, u64)>,
    /// Distinct `/Name` operands seen with `scn`/`SCN` (for inherited-resource safety).
    pattern_refs: Vec<Box<[u8]>>,
    /// Distinct `/Name` operands seen with `Tf` (for inherited-resource safety).
    font_refs: Vec<Box<[u8]>>,
    /// `ResourcesId` for which `type3_uses` was collected, or `None` when no Type 3
    /// fonts were present in the effective resources.
    type3_res_id: Option<ResourcesId>,
    /// `(index in Type3Hint::fonts, per-byte code counts)`.
    type3_uses: Vec<(usize, [u64; 256])>,
}

impl CachedStreamScan {
    const fn empty() -> Self {
        Self {
            own_ops: 0,
            xobjects: Vec::new(),
            ext_g_states: Vec::new(),
            pattern_paints: Vec::new(),
            pattern_refs: Vec::new(),
            font_refs: Vec::new(),
            type3_res_id: None,
            type3_uses: Vec::new(),
        }
    }
}

/// Pre-resolved Type 3 font descriptor for one `/Resources` dictionary.
struct Type3FontInfo {
    char_procs: Resolved,
    resources: Option<(ResourcesId, Resolved)>,
    names: [Option<Box<[u8]>>; 256],
    ascii_fallback: bool,
}

impl Type3FontInfo {
    fn glyph_name(&self, code: usize) -> Option<&[u8]> {
        self.names[code].as_deref()
    }
}

/// Lightweight lookup table of Type 3 fonts reachable from one `/Resources` dict.
struct Type3Hint {
    font_map: Vec<(Box<[u8]>, usize)>,
    /// Maps ExtGState `/Name` -> `Some(idx)` for Type 3 `/Font`, or `None` for non-Type 3 `/Font`.
    gs_map: Vec<(Box<[u8]>, Option<usize>)>,
    fonts: Vec<Type3FontInfo>,
}

impl Type3Hint {
    fn lookup_font(&self, name: &[u8]) -> Option<usize> {
        self.font_map
            .iter()
            .find(|(k, _)| k.as_ref() == name)
            .map(|(_, idx)| *idx)
    }

    fn lookup_gs(&self, name: &[u8]) -> Option<Option<usize>> {
        self.gs_map
            .iter()
            .find(|(k, _)| k.as_ref() == name)
            .map(|(_, mapped)| *mapped)
    }
}

struct PageGuard<'a, 'b> {
    reads: &'a mut Reads<'b>,
    shared: Option<&'a CheckedCache>,
    operand_limit: Option<usize>,
    fuel_limit: Option<u64>,
    local_cs: HashMap<u32, usize>,
    local_fn: HashMap<u32, usize>,
    local_streams: HashMap<u32, usize>,
    local_resources: HashMap<u32, usize>,
    local_scans: HashMap<u32, Arc<CachedStreamScan>>,
    type3_hints: HashMap<ResourcesId, Option<Arc<Type3Hint>>>,
    local_fuel: HashMap<FuelKey, (u64, usize)>,
    next_anon_res: u64,
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

    const fn strict_depth(&self) -> bool {
        self.operand_limit.is_some()
    }

    fn alloc_anon_res_id(&mut self) -> ResourcesId {
        let id = self.next_anon_res;
        self.next_anon_res = self.next_anon_res.saturating_add(1);
        ResourcesId::Anonymous(id)
    }

    fn resolve_val(&mut self, value: &Value) -> Result<Option<Resolved>> {
        let strict = self.strict_depth();
        resolve(self.reads, value, strict)
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

    fn lookup_cached_fuel(&self, key: FuelKey, depth: usize) -> Option<(u64, usize)> {
        if let Some(&entry) = self.local_fuel.get(&key) {
            return Some(entry);
        }
        if let Some(shared) = self.shared
            && let Some(&(ops, height)) = shared.stream_fuel.get(&key)
            && depth.saturating_add(height) <= MAX_STREAM_DEPTH
        {
            return Some((ops, height));
        }
        None
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

        let page_resources = match page.resources() {
            Some(resources_val) => self.resolve_val(resources_val)?,
            None => None,
        };
        let page_res_id = match &page_resources {
            Some(res) => res
                .obj_num()
                .map_or(ResourcesId::PageDirect, ResourcesId::Indirect),
            None => ResourcesId::None,
        };

        // 1. Scan the page's own /Contents stream(s) once (checks nesting, #740,
        //    #17 EI cap, and captures own operator count + stream invocations).
        let page_scan = match page.dict().get("Contents").cloned() {
            Some(contents) => {
                self.scan_page_contents(&contents, page_res_id, page_resources.as_ref())?
            }
            None => Arc::new(CachedStreamScan::empty()),
        };

        // 2. Check page-level /Group /CS and /Thumb image stream if present.
        if let Some(group_val) = page.dict().get("Group").cloned()
            && let Some(group) = self.resolve_val(&group_val)?
            && let Value::Dict(group_dict) = group.value()
            && let Some(cs_val) = group_dict.get("CS")
        {
            self.check_color_space(cs_val)?;
        }
        if let Some(thumb_val) = page.dict().get("Thumb").cloned()
            && let Some(thumb) = self.resolve_val(&thumb_val)?
            && let Value::Stream(stream) = thumb.value()
        {
            self.check_image_xobject(thumb.obj_num(), stream)?;
        }

        // 3. Collect top-level nested stream work items (page resources + visible annotation appearances).
        let mut stream_stack: BudgetVec<'_, StreamFrame> = BudgetVec::new(self.budget());
        let mut active_streams: HashSet<u32> = HashSet::new();
        let mut annot_fuel_streams: BudgetVec<'_, Resolved> = BudgetVec::new(self.budget());

        if let Some(resolved) = &page_resources {
            self.collect_from_resources(
                resolved,
                page_res_id,
                0,
                &mut stream_stack,
                &active_streams,
            )?;
        }

        if let Some(annots_val) = page.dict().get("Annots").cloned()
            && let Some(annots) = self.resolve_val(&annots_val)?
            && let Value::Array(arr) = annots.value()
        {
            for item in arr.iter() {
                self.tick()?;
                let Some(annot) = self.resolve_val(item)? else {
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
                    && let Some(ap) = self.resolve_val(ap_val)?
                    && let Value::Dict(ap_dict) = ap.value()
                    && let Some(n_val) = ap_dict.get("N")
                    && let Some(n_res) = self.resolve_val(n_val)?
                {
                    match n_res.value() {
                        Value::Stream(_) => {
                            annot_fuel_streams.try_push(n_res.clone())?;
                            stream_stack.try_push(StreamFrame::Enter {
                                stream: n_res,
                                own_fallback: None,
                                inherited_resources: page_resources
                                    .as_ref()
                                    .map(|r| (page_res_id, r.clone())),
                                depth: 1,
                            })?;
                        }
                        Value::Dict(states) => {
                            // For fuel accounting, match hayro's active state selection (/AS, else /Off).
                            let active_state_name = dict.get("AS").and_then(|v| match v {
                                Value::Name(n) => Some(n.as_bytes()),
                                _ => None,
                            });
                            let active_val = active_state_name
                                .and_then(|name| dict_get_bytes(states, name))
                                .or_else(|| states.get("Off"));
                            if let Some(av) = active_val
                                && let Some(s_res) = self.resolve_val(av)?
                                && matches!(s_res.value(), Value::Stream(_))
                            {
                                annot_fuel_streams.try_push(s_res)?;
                            }
                            // Safety walk validates every appearance state stream.
                            for (_, state_val) in states.iter() {
                                self.tick()?;
                                if let Some(s_res) = self.resolve_val(state_val)?
                                    && matches!(s_res.value(), Value::Stream(_))
                                {
                                    stream_stack.try_push(StreamFrame::Enter {
                                        stream: s_res,
                                        own_fallback: None,
                                        inherited_resources: page_resources
                                            .as_ref()
                                            .map(|r| (page_res_id, r.clone())),
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
                        if self.strict_depth() {
                            return Err(depth_refusal(
                                "nested interpreted streams exceeded depth limit",
                                MAX_STREAM_DEPTH,
                                depth,
                            ));
                        }
                        continue;
                    }
                    if let Some(num) = stream.obj_num() {
                        if active_streams.contains(&num) {
                            if self.strict_depth() {
                                return Err(cycle_refusal(format!(
                                    "cyclic nested stream at object {num}"
                                )));
                            }
                            continue;
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
                        && let Some(group) = self.resolve_val(group_val)?
                        && let Value::Dict(group_dict) = group.value()
                        && let Some(cs_val) = group_dict.get("CS")
                    {
                        self.check_color_space(cs_val)?;
                    }
                    // Determine effective resources:
                    // - Stream's own /Resources (or Type 3 font's /Resources): walk in full.
                    // - Otherwise, if hayro falls back to `inherited_resources` (Form, SoftMask,
                    //   Annotation AP, or Type 3 without font /Resources), follow only the
                    //   resource names actually invoked by this stream's content bytes so a
                    //   non-recursive child sharing page resources never false-positives.
                    let own_res = match s.dict().get("Resources") {
                        Some(res_val) => self.resolve_val(res_val)?.map(|res| {
                            let id = res.obj_num().map_or_else(
                                || match stream.obj_num() {
                                    Some(n) => ResourcesId::StreamOwn(n),
                                    None => self.alloc_anon_res_id(),
                                },
                                ResourcesId::Indirect,
                            );
                            (id, res)
                        }),
                        None => own_fallback,
                    };
                    if let Some((res_id, res)) = own_res {
                        let _ = self.scan_stream_cached(
                            stream.obj_num(),
                            s,
                            StreamKind::Any,
                            res_id,
                            Some(&res),
                        )?;
                        self.collect_from_resources(
                            &res,
                            res_id,
                            depth,
                            &mut stream_stack,
                            &active_streams,
                        )?;
                    } else if let Some((inh_id, inherited)) = inherited_resources {
                        let scan = self.scan_stream_cached(
                            stream.obj_num(),
                            s,
                            StreamKind::Any,
                            inh_id,
                            Some(&inherited),
                        )?;
                        self.collect_invoked_from_scan(
                            &inherited,
                            inh_id,
                            &scan,
                            depth,
                            &mut stream_stack,
                        )?;
                    } else {
                        let _ = self.scan_stream_cached(
                            stream.obj_num(),
                            s,
                            StreamKind::Any,
                            ResourcesId::None,
                            None,
                        )?;
                    }
                }
            }
        }

        // 5. Iterative bottom-up DAG operator fuel propagation (reusing cached stream scans).
        if let Some(fuel_limit) = self.fuel_limit {
            self.compute_page_fuel(
                &page_scan,
                page_res_id,
                page_resources.as_ref(),
                annot_fuel_streams.as_slice(),
                fuel_limit,
            )?;
        }
        Ok(())
    }

    fn scan_page_contents(
        &mut self,
        contents: &Value,
        res_id: ResourcesId,
        resources: Option<&Resolved>,
    ) -> Result<Arc<CachedStreamScan>> {
        let Some(resolved) = self.resolve_val(contents)? else {
            return Ok(Arc::new(CachedStreamScan::empty()));
        };
        match resolved.value() {
            Value::Stream(stream) => self.scan_stream_cached(
                resolved.obj_num(),
                stream,
                StreamKind::PageContent,
                res_id,
                resources,
            ),
            Value::Array(arr) => {
                if arr.len() == 1 {
                    if let Some(first) = arr.iter().next()
                        && let Some(res) = self.resolve_val(first)?
                        && let Value::Stream(stream) = res.value()
                    {
                        return self.scan_stream_cached(
                            res.obj_num(),
                            stream,
                            StreamKind::PageContent,
                            res_id,
                            resources,
                        );
                    }
                    return Ok(Arc::new(CachedStreamScan::empty()));
                }
                let mut joined: BudgetVec<'_, u8> = BudgetVec::new(self.budget());
                for item in arr.iter() {
                    self.tick()?;
                    if let Some(res) = self.resolve_val(item)?
                        && let Value::Stream(stream) = res.value()
                    {
                        let strict = self.strict_depth();
                        record_stream_filter_refs(self.reads, stream, strict)?;
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
                if joined.is_empty() {
                    return Ok(Arc::new(CachedStreamScan::empty()));
                }
                self.report.streams_scanned += 1;
                self.report.content_bytes_scanned += joined.len() as u64;
                let type3_hint = self.get_type3_hint(res_id, resources)?;
                let scan =
                    self.run_content_scan(joined.as_slice(), res_id, type3_hint.as_deref())?;
                Ok(Arc::new(scan))
            }
            _ => Ok(Arc::new(CachedStreamScan::empty())),
        }
    }

    fn scan_stream_cached(
        &mut self,
        stream_num: Option<u32>,
        stream: &Stream,
        kind: StreamKind,
        res_id: ResourcesId,
        resources: Option<&Resolved>,
    ) -> Result<Arc<CachedStreamScan>> {
        let type3_hint = self.get_type3_hint(res_id, resources)?;
        let need_type3 = type3_hint.is_some();
        if let Some(num) = stream_num
            && let Some(existing) = self.local_scans.get(&num)
            && (!need_type3 || existing.type3_res_id == Some(res_id))
        {
            return Ok(Arc::clone(existing));
        }
        let strict = self.strict_depth();
        record_stream_filter_refs(self.reads, stream, strict)?;
        let decoded = match stream.decoded_in(self.reads.version(), self.budget()) {
            Ok(bytes) => bytes,
            // Streams with unsupported/corrupt filters are skipped by hayro rather than overflowing.
            Err(err) if err.limit().is_some() => return Err(err),
            Err(_) => return Ok(Arc::new(CachedStreamScan::empty())),
        };
        if kind == StreamKind::PageContent {
            self.budget()
                .check(LimitCode::PageContentSize, decoded.len() as u64)?;
        }
        self.report.streams_scanned += 1;
        self.report.content_bytes_scanned += decoded.len() as u64;
        let scan = Arc::new(self.run_content_scan(&decoded, res_id, type3_hint.as_deref())?);
        if let Some(num) = stream_num {
            try_insert_map(
                &mut self.local_scans,
                num,
                Arc::clone(&scan),
                self.reads.budget(),
            )?;
        }
        Ok(scan)
    }

    fn run_content_scan(
        &mut self,
        data: &[u8],
        res_id: ResourcesId,
        type3_hint: Option<&Type3Hint>,
    ) -> Result<CachedStreamScan> {
        let budget = self.reads.budget();
        let mut collector = StreamCollector::new(budget, type3_hint);
        let own_ops = scan_content_bytes(
            data,
            self.operand_limit,
            Some(MAX_INLINE_IMAGE_EI_CANDIDATES),
            self.fuel_limit,
            budget,
            &mut self.report,
            Some(&mut collector),
        )?;
        collector.finish(own_ops, type3_hint.map(|_| res_id))
    }

    /// Resolves and caches any Type 3 font tables reachable from `resources`.
    fn get_type3_hint(
        &mut self,
        res_id: ResourcesId,
        resources: Option<&Resolved>,
    ) -> Result<Option<Arc<Type3Hint>>> {
        if let Some(cached) = self.type3_hints.get(&res_id) {
            return Ok(cached.clone());
        }
        let Some(res) = resources else {
            return Ok(None);
        };
        let Value::Dict(dict) = res.value() else {
            return Ok(None);
        };

        let mut fonts: BudgetVec<'_, Type3FontInfo> = BudgetVec::new(self.budget());
        let mut font_map: BudgetVec<'_, (Box<[u8]>, usize)> = BudgetVec::new(self.budget());
        let mut gs_map: BudgetVec<'_, (Box<[u8]>, Option<usize>)> = BudgetVec::new(self.budget());

        if let Some(font_dict_val) = dict.get("Font")
            && let Some(font_dict_res) = self.resolve_val(font_dict_val)?
            && let Value::Dict(font_dict) = font_dict_res.value()
        {
            for (name, val) in font_dict.iter() {
                self.tick()?;
                if let Some(info) = self.parse_type3_font_info(val)? {
                    let idx = fonts.len();
                    fonts.try_push(info)?;
                    let key = budgeted_bytes(name.as_bytes(), self.budget())?;
                    font_map.try_push((key, idx))?;
                }
            }
        }

        if let Some(gs_dict_val) = dict.get("ExtGState")
            && let Some(gs_dict_res) = self.resolve_val(gs_dict_val)?
            && let Value::Dict(gs_dict) = gs_dict_res.value()
        {
            for (name, val) in gs_dict.iter() {
                self.tick()?;
                let Some(gs_res) = self.resolve_val(val)? else {
                    continue;
                };
                let Value::Dict(gs_d) = gs_res.value() else {
                    continue;
                };
                let Some(font_arr_val) = gs_d.get("Font") else {
                    continue;
                };
                let Some(font_arr_res) = self.resolve_val(font_arr_val)? else {
                    continue;
                };
                let Value::Array(arr) = font_arr_res.value() else {
                    continue;
                };
                let Some(font_val) = arr.get(0) else {
                    continue;
                };
                let mapped = if let Some(info) = self.parse_type3_font_info(font_val)? {
                    let idx = fonts.len();
                    fonts.try_push(info)?;
                    Some(idx)
                } else {
                    None
                };
                let key = budgeted_bytes(name.as_bytes(), self.budget())?;
                gs_map.try_push((key, mapped))?;
            }
        }

        let hint = if fonts.is_empty() {
            None
        } else {
            Some(Arc::new(Type3Hint {
                font_map: font_map.into_vec(),
                gs_map: gs_map.into_vec(),
                fonts: fonts.into_vec(),
            }))
        };
        try_insert_map(
            &mut self.type3_hints,
            res_id,
            hint.clone(),
            self.reads.budget(),
        )?;
        Ok(hint)
    }

    fn parse_type3_font_info(&mut self, val: &Value) -> Result<Option<Type3FontInfo>> {
        let Some(font) = self.resolve_val(val)? else {
            return Ok(None);
        };
        let Value::Dict(f_dict) = font.value() else {
            return Ok(None);
        };
        if !matches!(
            f_dict.get("Subtype"),
            Some(Value::Name(n)) if n.as_bytes() == b"Type3"
        ) {
            return Ok(None);
        }
        let Some(procs_val) = f_dict.get("CharProcs") else {
            return Ok(None);
        };
        let Some(char_procs) = self.resolve_val(procs_val)? else {
            return Ok(None);
        };
        if !matches!(char_procs.value(), Value::Dict(_)) {
            return Ok(None);
        }
        let resources = match f_dict.get("Resources") {
            Some(r_val) => self.resolve_val(r_val)?.map(|res| {
                let id = res.obj_num().map_or_else(
                    || match font.obj_num() {
                        Some(n) => ResourcesId::FontOwn(n),
                        None => self.alloc_anon_res_id(),
                    },
                    ResourcesId::Indirect,
                );
                (id, res)
            }),
            None => None,
        };
        let mut names: [Option<Box<[u8]>>; 256] = std::array::from_fn(|_| None);
        let mut ascii_fallback = false;
        if let Some(enc_val) = f_dict.get("Encoding")
            && let Some(enc_res) = self.resolve_val(enc_val)?
        {
            match enc_res.value() {
                Value::Name(_) => {
                    ascii_fallback = true;
                }
                Value::Dict(enc_dict) => {
                    if enc_dict.get("BaseEncoding").is_some() {
                        ascii_fallback = true;
                    }
                    if let Some(diff_val) = enc_dict.get("Differences")
                        && let Some(diff_res) = self.resolve_val(diff_val)?
                        && let Value::Array(diffs) = diff_res.value()
                    {
                        let mut code: i64 = 0;
                        for item in diffs.iter() {
                            self.tick()?;
                            match item {
                                Value::Integer(n) => code = *n,
                                Value::Name(n) => {
                                    if let Ok(at) = usize::try_from(code)
                                        && at < names.len()
                                    {
                                        names[at] =
                                            Some(budgeted_bytes(n.as_bytes(), self.budget())?);
                                    }
                                    code = code.saturating_add(1);
                                }
                                _ => {}
                            }
                        }
                    }
                }
                _ => {}
            }
        }
        Ok(Some(Type3FontInfo {
            char_procs,
            resources,
            names,
            ascii_fallback,
        }))
    }

    /// Computes total effective operator invocations for the page via iterative
    /// bottom-up DAG propagation with saturating arithmetic and memoization.
    fn compute_page_fuel(
        &mut self,
        page_scan: &CachedStreamScan,
        page_res_id: ResourcesId,
        page_resources: Option<&Resolved>,
        annot_streams: &[Resolved],
        fuel_limit: u64,
    ) -> Result<()> {
        struct FuelFrame {
            key: Option<FuelKey>,
            depth: usize,
            total_ops: u64,
            max_child_height: usize,
            truncated: bool,
            children: Vec<(FuelChild, u64)>,
            next: usize,
        }

        let mut root_children: BudgetVec<'_, (FuelChild, u64)> = BudgetVec::new(self.budget());
        self.resolve_fuel_children(page_scan, page_res_id, page_resources, &mut root_children)?;
        for annot_stream in annot_streams {
            self.tick()?;
            let Value::Stream(s) = annot_stream.value() else {
                continue;
            };
            let (child_res_id, child_res) = match s.dict().get("Resources") {
                Some(r_val) => match self.resolve_val(r_val)? {
                    Some(res) => {
                        let id = res.obj_num().map_or_else(
                            || match annot_stream.obj_num() {
                                Some(n) => ResourcesId::StreamOwn(n),
                                None => self.alloc_anon_res_id(),
                            },
                            ResourcesId::Indirect,
                        );
                        (id, Some(res))
                    }
                    None => (page_res_id, page_resources.cloned()),
                },
                None => (page_res_id, page_resources.cloned()),
            };
            push_fuel_child(
                &mut root_children,
                FuelChild {
                    stream: annot_stream.clone(),
                    res_id: child_res_id,
                    resources: child_res,
                },
                1,
            )?;
        }

        if page_scan.own_ops > fuel_limit {
            return Err(fuel_refusal(
                "page effective operator fuel exceeded limit",
                fuel_limit,
                page_scan.own_ops,
            ));
        }

        let mut stack: BudgetVec<'_, FuelFrame> = BudgetVec::new(self.budget());
        stack.try_push(FuelFrame {
            key: None,
            depth: 0,
            total_ops: page_scan.own_ops,
            max_child_height: 0,
            truncated: false,
            children: root_children.into_vec(),
            next: 0,
        })?;
        let mut active: HashSet<FuelKey> = HashSet::new();

        while let Some(top) = stack.last_mut() {
            self.tick()?;
            if top.next < top.children.len() {
                let idx = top.next;
                let times = top.children[idx].1;
                let child_depth = top.depth + 1;
                let child_stream = top.children[idx].0.stream.clone();
                let child_res_id = top.children[idx].0.res_id;
                let child_resources = top.children[idx].0.resources.clone();
                let child_key = child_stream.obj_num().map(|num| FuelKey {
                    stream: num,
                    resources: child_res_id,
                });

                if let Some(key) = child_key {
                    if let Some((cached_ops, cached_height)) =
                        self.lookup_cached_fuel(key, child_depth)
                    {
                        let top = stack.last_mut().expect("frame present");
                        let added = cached_ops.max(1).saturating_mul(times);
                        top.total_ops = top.total_ops.saturating_add(added);
                        top.max_child_height =
                            top.max_child_height.max(cached_height.saturating_add(1));
                        if top.total_ops > fuel_limit {
                            return Err(fuel_refusal(
                                "page effective operator fuel exceeded limit",
                                fuel_limit,
                                top.total_ops,
                            ));
                        }
                        top.next += 1;
                        continue;
                    }
                    if active.contains(&key) {
                        return Err(fuel_refusal(
                            "cyclic stream exhausted interpretation fuel",
                            fuel_limit,
                            u64::MAX,
                        ));
                    }
                }

                if child_depth > MAX_STREAM_DEPTH {
                    let top = stack.last_mut().expect("frame present");
                    top.truncated = true;
                    top.next += 1;
                    continue;
                }

                let child_scan = match child_stream.value() {
                    Value::Stream(s) => self.scan_stream_cached(
                        child_stream.obj_num(),
                        s,
                        StreamKind::Any,
                        child_res_id,
                        child_resources.as_ref(),
                    )?,
                    _ => Arc::new(CachedStreamScan::empty()),
                };

                if child_scan.own_ops.max(1).saturating_mul(times) > fuel_limit {
                    return Err(fuel_refusal(
                        "page effective operator fuel exceeded limit",
                        fuel_limit,
                        child_scan.own_ops.max(1).saturating_mul(times),
                    ));
                }

                let mut grand_children: BudgetVec<'_, (FuelChild, u64)> =
                    BudgetVec::new(self.budget());
                self.resolve_fuel_children(
                    &child_scan,
                    child_res_id,
                    child_resources.as_ref(),
                    &mut grand_children,
                )?;

                if let Some(key) = child_key {
                    try_insert_set(&mut active, key, self.reads.budget())?;
                }
                stack.try_push(FuelFrame {
                    key: child_key,
                    depth: child_depth,
                    total_ops: child_scan.own_ops,
                    max_child_height: 0,
                    truncated: false,
                    children: grand_children.into_vec(),
                    next: 0,
                })?;
            } else {
                let done = stack.pop().expect("frame present");
                if let Some(key) = done.key {
                    active.remove(&key);
                    if !done.truncated {
                        try_insert_map(
                            &mut self.local_fuel,
                            key,
                            (done.total_ops, done.max_child_height),
                            self.reads.budget(),
                        )?;
                    }
                }
                match stack.last_mut() {
                    Some(parent) => {
                        let times = parent.children[parent.next].1;
                        let charge = done.total_ops.max(1).saturating_mul(times);
                        parent.total_ops = parent.total_ops.saturating_add(charge);
                        parent.max_child_height = parent
                            .max_child_height
                            .max(done.max_child_height.saturating_add(1));
                        parent.truncated |= done.truncated;
                        if parent.total_ops > fuel_limit {
                            return Err(fuel_refusal(
                                "page effective operator fuel exceeded limit",
                                fuel_limit,
                                parent.total_ops,
                            ));
                        }
                        parent.next += 1;
                    }
                    None => {
                        self.report.effective_operators = done.total_ops;
                        charge_page_fuel(self.budget(), done.total_ops, Some(fuel_limit))?;
                        return Ok(());
                    }
                }
            }
        }
        Ok(())
    }

    fn resolve_fuel_children(
        &mut self,
        scan: &CachedStreamScan,
        res_id: ResourcesId,
        resources: Option<&Resolved>,
        out: &mut BudgetVec<'_, (FuelChild, u64)>,
    ) -> Result<()> {
        let Some(res) = resources else {
            return Ok(());
        };
        let Value::Dict(dict) = res.value() else {
            return Ok(());
        };

        // Form XObjects invoked by `Do`.
        if !scan.xobjects.is_empty()
            && let Some(xo_dict_val) = dict.get("XObject")
            && let Some(xo_dict_res) = self.resolve_val(xo_dict_val)?
            && let Value::Dict(xo_dict) = xo_dict_res.value()
        {
            for (name, count) in &scan.xobjects {
                self.tick()?;
                if *count == 0 {
                    continue;
                }
                let Some(val) = dict_get_bytes(xo_dict, name) else {
                    continue;
                };
                let Some(xo) = self.resolve_val(val)? else {
                    continue;
                };
                let Value::Stream(stream) = xo.value() else {
                    continue;
                };
                if !matches!(
                    stream.dict().get("Subtype"),
                    Some(Value::Name(n)) if n.as_bytes() == b"Form"
                ) {
                    continue;
                }
                let (child_res_id, child_res) = match stream.dict().get("Resources") {
                    Some(r_val) => match self.resolve_val(r_val)? {
                        Some(own) => {
                            let id = own.obj_num().map_or_else(
                                || match xo.obj_num() {
                                    Some(n) => ResourcesId::StreamOwn(n),
                                    None => self.alloc_anon_res_id(),
                                },
                                ResourcesId::Indirect,
                            );
                            (id, Some(own))
                        }
                        None => (res_id, Some(res.clone())),
                    },
                    None => (res_id, Some(res.clone())),
                };
                push_fuel_child(
                    out,
                    FuelChild {
                        stream: xo,
                        res_id: child_res_id,
                        resources: child_res,
                    },
                    *count,
                )?;
            }
        }

        // SoftMask `/G` groups invoked by `gs`.
        if !scan.ext_g_states.is_empty()
            && let Some(gs_dict_val) = dict.get("ExtGState")
            && let Some(gs_dict_res) = self.resolve_val(gs_dict_val)?
            && let Value::Dict(gs_dict) = gs_dict_res.value()
        {
            for (name, count) in &scan.ext_g_states {
                self.tick()?;
                if *count == 0 {
                    continue;
                }
                let Some(val) = dict_get_bytes(gs_dict, name) else {
                    continue;
                };
                let Some(gs_res) = self.resolve_val(val)? else {
                    continue;
                };
                let Value::Dict(gs_d) = gs_res.value() else {
                    continue;
                };
                if let Some(smask_val) = gs_d.get("SMask")
                    && let Some(smask) = self.resolve_val(smask_val)?
                    && let Value::Dict(sm_dict) = smask.value()
                    && let Some(g_val) = sm_dict.get("G")
                    && let Some(g_res) = self.resolve_val(g_val)?
                    && let Value::Stream(g_stream) = g_res.value()
                {
                    let (child_res_id, child_res) = match g_stream.dict().get("Resources") {
                        Some(r_val) => match self.resolve_val(r_val)? {
                            Some(own) => {
                                let id = own.obj_num().map_or_else(
                                    || match g_res.obj_num() {
                                        Some(n) => ResourcesId::StreamOwn(n),
                                        None => self.alloc_anon_res_id(),
                                    },
                                    ResourcesId::Indirect,
                                );
                                (id, Some(own))
                            }
                            None => (res_id, Some(res.clone())),
                        },
                        None => (res_id, Some(res.clone())),
                    };
                    push_fuel_child(
                        out,
                        FuelChild {
                            stream: g_res,
                            res_id: child_res_id,
                            resources: child_res,
                        },
                        *count,
                    )?;
                }
            }
        }

        // Tiling patterns painted while active via `scn`/`SCN`.
        if !scan.pattern_paints.is_empty()
            && let Some(pat_dict_val) = dict.get("Pattern")
            && let Some(pat_dict_res) = self.resolve_val(pat_dict_val)?
            && let Value::Dict(pat_dict) = pat_dict_res.value()
        {
            for (name, paint_count) in &scan.pattern_paints {
                self.tick()?;
                if *paint_count == 0 {
                    continue;
                }
                let Some(val) = dict_get_bytes(pat_dict, name) else {
                    continue;
                };
                let Some(pat) = self.resolve_val(val)? else {
                    continue;
                };
                let Value::Stream(stream) = pat.value() else {
                    continue;
                };
                let Some(copies) = self.tiling_cell_copies(stream.dict())? else {
                    continue;
                };
                let times = paint_count.saturating_mul(copies);
                // TilingPattern in hayro does not inherit parent resources.
                let (child_res_id, child_res) = match stream.dict().get("Resources") {
                    Some(r_val) => match self.resolve_val(r_val)? {
                        Some(own) => {
                            let id = own.obj_num().map_or_else(
                                || match pat.obj_num() {
                                    Some(n) => ResourcesId::StreamOwn(n),
                                    None => self.alloc_anon_res_id(),
                                },
                                ResourcesId::Indirect,
                            );
                            (id, Some(own))
                        }
                        None => (ResourcesId::None, None),
                    },
                    None => (ResourcesId::None, None),
                };
                push_fuel_child(
                    out,
                    FuelChild {
                        stream: pat,
                        res_id: child_res_id,
                        resources: child_res,
                    },
                    times,
                )?;
            }
        }

        // Type 3 `/CharProcs` glyph procedures invoked by text-showing operators.
        if !scan.type3_uses.is_empty()
            && let Some(hint) = self.get_type3_hint(res_id, Some(res))?
        {
            for (font_idx, code_counts) in &scan.type3_uses {
                let Some(font) = hint.fonts.get(*font_idx) else {
                    continue;
                };
                let Value::Dict(procs_dict) = font.char_procs.value() else {
                    continue;
                };
                for (code, &times) in code_counts.iter().enumerate() {
                    if times == 0 {
                        continue;
                    }
                    self.tick()?;
                    let fallback_byte = [code as u8];
                    let glyph_name = font.glyph_name(code).or_else(|| {
                        (font.ascii_fallback && (code as u8).is_ascii_alphabetic())
                            .then_some(&fallback_byte[..])
                    });
                    let Some(name_bytes) = glyph_name else {
                        continue;
                    };
                    let Some(proc_val) = dict_get_bytes(procs_dict, name_bytes) else {
                        continue;
                    };
                    let Some(proc_res) = self.resolve_val(proc_val)? else {
                        continue;
                    };
                    let Value::Stream(proc_stream) = proc_res.value() else {
                        continue;
                    };
                    let (child_res_id, child_res) = match proc_stream.dict().get("Resources") {
                        Some(r_val) => match self.resolve_val(r_val)? {
                            Some(own) => {
                                let id = own.obj_num().map_or_else(
                                    || match proc_res.obj_num() {
                                        Some(n) => ResourcesId::StreamOwn(n),
                                        None => self.alloc_anon_res_id(),
                                    },
                                    ResourcesId::Indirect,
                                );
                                (id, Some(own))
                            }
                            None => match &font.resources {
                                Some((fid, fres)) => (*fid, Some(fres.clone())),
                                None => (res_id, Some(res.clone())),
                            },
                        },
                        None => match &font.resources {
                            Some((fid, fres)) => (*fid, Some(fres.clone())),
                            None => (res_id, Some(res.clone())),
                        },
                    };
                    push_fuel_child(
                        out,
                        FuelChild {
                            stream: proc_res,
                            res_id: child_res_id,
                            resources: child_res,
                        },
                        times,
                    )?;
                }
            }
        }

        Ok(())
    }

    /// Computes how many copies of a Type 1 tiling pattern cell one tile draws
    /// (matching `hayro/src/tiling.rs` and `evidence/interpretation-fuel/src/walk.rs`),
    /// or `None` if the stream is not a valid Type 1 tiling pattern.
    fn tiling_cell_copies(&mut self, dict: &Dict) -> Result<Option<u64>> {
        let Some(ptype_val) = dict.get("PatternType") else {
            return Ok(None);
        };
        let Some(ptype_res) = self.resolve_val(ptype_val)? else {
            return Ok(None);
        };
        if !matches!(ptype_res.value(), Value::Integer(1)) {
            return Ok(None);
        }
        let Some(bbox_val) = dict.get("BBox") else {
            return Ok(None);
        };
        let Some(bbox_res) = self.resolve_val(bbox_val)? else {
            return Ok(None);
        };
        let Value::Array(bbox_arr) = bbox_res.value() else {
            return Ok(None);
        };
        if bbox_arr.len() < 4 {
            return Ok(None);
        }
        let mut bbox = [0.0f64; 4];
        for (slot, item) in bbox.iter_mut().zip(bbox_arr.iter().take(4)) {
            let Some(v) = self.resolve_val(item)? else {
                return Ok(None);
            };
            let Some(num) = value_as_f64(v.value()) else {
                return Ok(None);
            };
            *slot = num;
        }
        let x_step = match dict.get("XStep") {
            Some(v) => self
                .resolve_val(v)?
                .and_then(|r| value_as_f64(r.value()))
                .map(f64::abs),
            None => None,
        };
        let y_step = match dict.get("YStep") {
            Some(v) => self
                .resolve_val(v)?
                .and_then(|r| value_as_f64(r.value()))
                .map(f64::abs),
            None => None,
        };
        let (Some(x_step), Some(y_step)) = (x_step, y_step) else {
            return Ok(None);
        };
        let [x0, y0, x1, y1] = bbox;
        if x_step < 1e-6 || y_step < 1e-6 || x0 == x1 || y0 == y1 {
            return Ok(None);
        }
        let axis = |extent: f64, step: f64| {
            let copies = (extent.abs() / step).ceil() + 1.0;
            if copies.is_finite() && copies < 1e18 {
                copies as u64
            } else {
                u64::MAX
            }
        };
        Ok(Some(
            axis(x1 - x0, x_step).saturating_mul(axis(y1 - y0, y_step)),
        ))
    }

    /// Resolves only the resource names invoked by a nested stream that omitted
    /// its own `/Resources` and therefore inherits `resources` in `hayro`.
    fn collect_invoked_from_scan(
        &mut self,
        resources: &Resolved,
        resources_id: ResourcesId,
        scan: &CachedStreamScan,
        depth: usize,
        stream_stack: &mut BudgetVec<'_, StreamFrame>,
    ) -> Result<()> {
        let Value::Dict(dict) = resources.value() else {
            return Ok(());
        };
        if !scan.xobjects.is_empty()
            && let Some(xo_dict_val) = dict.get("XObject")
            && let Some(xo_dict_res) = self.resolve_val(xo_dict_val)?
            && let Value::Dict(xo_dict) = xo_dict_res.value()
        {
            for (name_bytes, _) in &scan.xobjects {
                self.tick()?;
                if let Some(val) = dict_get_bytes(xo_dict, name_bytes)
                    && let Some(xo) = self.resolve_val(val)?
                    && let Value::Stream(stream) = xo.value()
                    && matches!(
                        stream.dict().get("Subtype"),
                        Some(Value::Name(n)) if n.as_bytes() == b"Form"
                    )
                {
                    stream_stack.try_push(StreamFrame::Enter {
                        stream: xo,
                        own_fallback: None,
                        inherited_resources: Some((resources_id, resources.clone())),
                        depth: depth + 1,
                    })?;
                }
            }
        }
        if !scan.font_refs.is_empty()
            && let Some(font_dict_val) = dict.get("Font")
            && let Some(font_dict_res) = self.resolve_val(font_dict_val)?
            && let Value::Dict(font_dict) = font_dict_res.value()
        {
            for name_bytes in &scan.font_refs {
                self.tick()?;
                if let Some(val) = dict_get_bytes(font_dict, name_bytes) {
                    self.enqueue_type3_font(val, resources, resources_id, depth, stream_stack)?;
                }
            }
        }
        if !scan.ext_g_states.is_empty()
            && let Some(gs_dict_val) = dict.get("ExtGState")
            && let Some(gs_dict_res) = self.resolve_val(gs_dict_val)?
            && let Value::Dict(gs_dict) = gs_dict_res.value()
        {
            for (name_bytes, _) in &scan.ext_g_states {
                self.tick()?;
                if let Some(val) = dict_get_bytes(gs_dict, name_bytes) {
                    self.check_ext_g_state(val, resources, resources_id, depth, stream_stack)?;
                }
            }
        }
        if !scan.pattern_refs.is_empty()
            && let Some(pat_dict_val) = dict.get("Pattern")
            && let Some(pat_dict_res) = self.resolve_val(pat_dict_val)?
            && let Value::Dict(pat_dict) = pat_dict_res.value()
        {
            for name_bytes in &scan.pattern_refs {
                self.tick()?;
                if let Some(val) = dict_get_bytes(pat_dict, name_bytes)
                    && let Some(pat) = self.resolve_val(val)?
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
        Ok(())
    }

    /// Inspect every resource sub-dictionary that hayro interprets:
    /// `/ColorSpace`, `/Shading`, `/ExtGState`, `/Pattern`, `/XObject`, `/Font`.
    fn collect_from_resources(
        &mut self,
        resources: &Resolved,
        resources_id: ResourcesId,
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
            && let Some(cs_dict_res) = self.resolve_val(cs_dict_val)?
            && let Value::Dict(cs_dict) = cs_dict_res.value()
        {
            for (_, val) in cs_dict.iter() {
                self.tick()?;
                self.check_color_space(val)?;
            }
        }

        // /Shading
        if let Some(sh_dict_val) = dict.get("Shading")
            && let Some(sh_dict_res) = self.resolve_val(sh_dict_val)?
            && let Value::Dict(sh_dict) = sh_dict_res.value()
        {
            for (_, val) in sh_dict.iter() {
                self.tick()?;
                self.check_shading(val)?;
            }
        }

        // /ExtGState
        if let Some(gs_dict_val) = dict.get("ExtGState")
            && let Some(gs_dict_res) = self.resolve_val(gs_dict_val)?
            && let Value::Dict(gs_dict) = gs_dict_res.value()
        {
            for (_, val) in gs_dict.iter() {
                self.tick()?;
                self.check_ext_g_state(val, resources, resources_id, depth, stream_stack)?;
            }
        }

        // /Pattern
        if let Some(pat_dict_val) = dict.get("Pattern")
            && let Some(pat_dict_res) = self.resolve_val(pat_dict_val)?
            && let Value::Dict(pat_dict) = pat_dict_res.value()
        {
            for (_, val) in pat_dict.iter() {
                self.tick()?;
                if let Some(pat) = self.resolve_val(val)? {
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
            && let Some(xo_dict_res) = self.resolve_val(xo_dict_val)?
            && let Value::Dict(xo_dict) = xo_dict_res.value()
        {
            for (_, val) in xo_dict.iter() {
                self.tick()?;
                let Some(xo) = self.resolve_val(val)? else {
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
                            inherited_resources: Some((resources_id, resources.clone())),
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
            && let Some(font_dict_res) = self.resolve_val(font_dict_val)?
            && let Value::Dict(font_dict) = font_dict_res.value()
        {
            for (_, val) in font_dict.iter() {
                self.tick()?;
                self.enqueue_type3_font(val, resources, resources_id, depth, stream_stack)?;
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
        parent_resources_id: ResourcesId,
        depth: usize,
        stream_stack: &mut BudgetVec<'_, StreamFrame>,
    ) -> Result<()> {
        let Some(font) = self.resolve_val(val)? else {
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
            Some(f_res_val) => self.resolve_val(f_res_val)?.map(|res| {
                let id = res.obj_num().map_or_else(
                    || match font.obj_num() {
                        Some(n) => ResourcesId::FontOwn(n),
                        None => self.alloc_anon_res_id(),
                    },
                    ResourcesId::Indirect,
                );
                (id, res)
            }),
            None => None,
        };
        if let Some(procs_val) = f_dict.get("CharProcs")
            && let Some(procs) = self.resolve_val(procs_val)?
            && let Value::Dict(procs_dict) = procs.value()
        {
            for (_, proc_val) in procs_dict.iter() {
                self.tick()?;
                if let Some(proc_res) = self.resolve_val(proc_val)?
                    && matches!(proc_res.value(), Value::Stream(_))
                {
                    stream_stack.try_push(StreamFrame::Enter {
                        stream: proc_res,
                        own_fallback: font_resources.clone(),
                        inherited_resources: font_resources
                            .is_none()
                            .then(|| (parent_resources_id, parent_resources.clone())),
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
                        if self.strict_depth() {
                            return Err(depth_refusal(
                                "image mask chain exceeded depth limit",
                                MAX_STREAM_DEPTH,
                                depth,
                            ));
                        }
                        continue;
                    }
                    if let Some(num) = item.obj_num() {
                        if active.contains(&num) {
                            if self.strict_depth() {
                                return Err(cycle_refusal(format!(
                                    "cyclic image mask at object {num}"
                                )));
                            }
                            continue;
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
                && let Some(mask_res) = self.resolve_val(mask_val)?
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
        let Some(resolved) = self.resolve_val(value)? else {
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
        parent_resources_id: ResourcesId,
        depth: usize,
        stream_stack: &mut BudgetVec<'_, StreamFrame>,
    ) -> Result<()> {
        let Some(resolved) = self.resolve_val(value)? else {
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
            && let Some(smask) = self.resolve_val(smask_val)?
            && let Value::Dict(sm_dict) = smask.value()
        {
            if let Some(tr) = sm_dict.get("TR") {
                self.check_function_or_array(tr)?;
            }
            if let Some(g_val) = sm_dict.get("G")
                && let Some(g_res) = self.resolve_val(g_val)?
                && matches!(g_res.value(), Value::Stream(_))
            {
                stream_stack.try_push(StreamFrame::Enter {
                    stream: g_res,
                    own_fallback: None,
                    inherited_resources: Some((parent_resources_id, parent_resources.clone())),
                    depth: depth + 1,
                })?;
            }
        }
        Ok(())
    }

    fn check_function_or_array(&mut self, value: &Value) -> Result<()> {
        let Some(resolved) = self.resolve_val(value)? else {
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

    /// Iterative DFS over PDF functions (Type 0 sampled, Type 3 stitching, Type 4 PostScript).
    fn check_function(&mut self, value: &Value) -> Result<()> {
        let Some(resolved) = self.resolve_val(value)? else {
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
                        if self.strict_depth() {
                            return Err(depth_refusal(
                                "function nesting exceeded depth limit",
                                MAX_FUNCTION_DEPTH,
                                depth,
                            ));
                        }
                        continue;
                    }
                    if let Some(num) = item.obj_num() {
                        if active.contains(&num) {
                            if self.strict_depth() {
                                return Err(cycle_refusal(format!(
                                    "cyclic function graph at object {num}"
                                )));
                            }
                            continue;
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
                        Some(0) => {
                            self.check_type0_function(dict)?;
                        }
                        Some(3) => {
                            if let Some(funcs_val) = dict.get("Functions")
                                && let Some(funcs_res) = self.resolve_val(funcs_val)?
                                && let Value::Array(arr) = funcs_res.value()
                            {
                                for child in arr.iter() {
                                    self.tick()?;
                                    if let Some(c_res) = self.resolve_val(child)? {
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

    /// Bounds the unpacked sample table size (`product(/Size) * outputs`) of a
    /// Type 0 sampled function before `hayro-interpret::function::type0::Type0::new`
    /// allocates `Vec<f32>` (#1275).
    fn check_type0_function(&mut self, dict: &Dict) -> Result<()> {
        let Some(size_val) = dict.get("Size") else {
            return Ok(());
        };
        let Some(size_res) = self.resolve_val(size_val)? else {
            return Ok(());
        };
        let Value::Array(size_arr) = size_res.value() else {
            return Ok(());
        };
        let mut product = 1u64;
        for item in size_arr.iter() {
            self.tick()?;
            let Some(resolved) = self.resolve_val(item)? else {
                continue;
            };
            if let Value::Integer(n) = resolved.value() {
                if *n <= 0 {
                    return Ok(());
                }
                product = product.saturating_mul(*n as u64);
                if product > MAX_TYPE0_SAMPLES {
                    return Err(fuel_refusal(
                        "Type 0 sampled function /Size product exceeded limit",
                        MAX_TYPE0_SAMPLES,
                        product,
                    ));
                }
            }
        }
        let outputs = match dict.get("Range") {
            Some(range_val) => match self.resolve_val(range_val)? {
                Some(range_res) => match range_res.value() {
                    Value::Array(arr) => (arr.len() / 2).max(1) as u64,
                    _ => 1,
                },
                None => 1,
            },
            None => 1,
        };
        let total_samples = product.saturating_mul(outputs);
        if total_samples > self.report.max_type0_samples {
            self.report.max_type0_samples = total_samples;
        }
        if total_samples > MAX_TYPE0_SAMPLES {
            return Err(fuel_refusal(
                "Type 0 sampled function sample count exceeded limit",
                MAX_TYPE0_SAMPLES,
                total_samples,
            ));
        }
        Ok(())
    }

    /// Iterative brace-depth and operator-count scanner for Type 4 PostScript
    /// calculator functions.
    fn check_type4_stream(&mut self, stream: &Stream) -> Result<()> {
        let strict = self.strict_depth();
        record_stream_filter_refs(self.reads, stream, strict)?;
        let decoded = match stream.decoded_in(self.reads.version(), self.budget()) {
            Ok(bytes) => bytes,
            Err(err) if err.limit().is_some() => return Err(err),
            Err(_) => return Ok(()),
        };
        self.report.type4_streams_scanned += 1;
        self.report.type4_bytes_scanned += decoded.len() as u64;
        let mut depth = 0usize;
        let mut max_depth = 0usize;
        let mut operators = 0usize;
        let mut in_comment = false;
        let mut in_token = false;
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
                b'%' => {
                    in_token = false;
                    in_comment = true;
                }
                b'{' => {
                    in_token = false;
                    depth += 1;
                    if depth > max_depth {
                        max_depth = depth;
                    }
                    if strict && depth > MAX_TYPE4_BRACES {
                        return Err(depth_refusal(
                            "Type 4 PostScript procedure braces exceeded limit",
                            MAX_TYPE4_BRACES,
                            depth,
                        ));
                    }
                    operators += 1;
                    if operators > MAX_TYPE4_OPERATORS {
                        return Err(fuel_refusal(
                            "Type 4 PostScript procedure operator count exceeded limit",
                            MAX_TYPE4_OPERATORS as u64,
                            operators as u64,
                        ));
                    }
                }
                b'}' => {
                    in_token = false;
                    depth = depth.saturating_sub(1);
                }
                b if is_whitespace(b) => {
                    in_token = false;
                }
                _ => {
                    if !in_token {
                        in_token = true;
                        operators += 1;
                        if operators > MAX_TYPE4_OPERATORS {
                            return Err(fuel_refusal(
                                "Type 4 PostScript procedure operator count exceeded limit",
                                MAX_TYPE4_OPERATORS as u64,
                                operators as u64,
                            ));
                        }
                    }
                }
            }
        }
        if max_depth > self.report.max_type4_braces {
            self.report.max_type4_braces = max_depth;
        }
        if operators > self.report.max_type4_operators {
            self.report.max_type4_operators = operators;
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
        let Some(root) = self.resolve_val(value)? else {
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
                        if self.strict_depth() {
                            return Err(depth_refusal(
                                "color space nesting exceeded depth limit",
                                MAX_COLOR_SPACE_DEPTH,
                                depth,
                            ));
                        }
                        continue;
                    }
                    if let Some(num) = item.obj_num() {
                        if active.contains(&num) {
                            if self.strict_depth() {
                                return Err(cycle_refusal(format!(
                                    "cyclic color space at object {num}"
                                )));
                            }
                            continue;
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
                                && let Some(res) = self.resolve_val(base)?
                            {
                                stack.try_push(Frame::Enter {
                                    item: res,
                                    depth: depth + 1,
                                })?;
                            }
                        }
                        b"Separation" | b"DeviceN" => {
                            if let Some(alt) = arr.get(2)
                                && let Some(res) = self.resolve_val(alt)?
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
                                && let Some(res) = self.resolve_val(under)?
                            {
                                stack.try_push(Frame::Enter {
                                    item: res,
                                    depth: depth + 1,
                                })?;
                            }
                        }
                        b"ICCBased" => {
                            if let Some(icc_val) = arr.get(1)
                                && let Some(icc) = self.resolve_val(icc_val)?
                                && let Value::Stream(stream) = icc.value()
                                && let Some(alt) = stream.dict().get("Alternate")
                                && let Some(res) = self.resolve_val(alt)?
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

struct FuelChild {
    stream: Resolved,
    res_id: ResourcesId,
    resources: Option<Resolved>,
}

impl FuelChild {
    const fn fuel_key(&self) -> Option<FuelKey> {
        match self.stream.obj_num() {
            Some(stream) => Some(FuelKey {
                stream,
                resources: self.res_id,
            }),
            None => None,
        }
    }
}

fn push_fuel_child(
    out: &mut BudgetVec<'_, (FuelChild, u64)>,
    child: FuelChild,
    times: u64,
) -> Result<()> {
    if times == 0 {
        return Ok(());
    }
    if let Some(key) = child.fuel_key() {
        for (existing, count) in out.as_mut_slice() {
            if existing.fuel_key() == Some(key) {
                *count = count.saturating_add(times);
                return Ok(());
            }
        }
    }
    out.try_push((child, times))?;
    Ok(())
}

fn value_as_f64(value: &Value) -> Option<f64> {
    match value {
        Value::Integer(n) => Some(*n as f64),
        Value::Real(r) => Some(r.value()),
        _ => None,
    }
}

enum ImageMaskFrame {
    Enter { item: Resolved, depth: usize },
    Leave(u32),
}

#[expect(
    clippy::large_enum_variant,
    reason = "stack holds at most MAX_STREAM_DEPTH + fan-out frames, bounded by BudgetVec"
)]
enum StreamFrame {
    Enter {
        stream: Resolved,
        /// Own fallback resources (Type 3 font `/Resources`), walked in full.
        own_fallback: Option<(ResourcesId, Resolved)>,
        /// Caller resources inherited when stream has no `/Resources` of its own;
        /// only names actually invoked by the stream's operators are followed.
        inherited_resources: Option<(ResourcesId, Resolved)>,
        depth: usize,
    },
    Leave(u32, usize),
}

#[derive(Clone)]
enum TextSpan {
    Literal(Range<usize>),
    Hex(Range<usize>),
    Array(Range<usize>),
}

#[derive(Clone, Copy, Default)]
struct ScanGState {
    fill_pattern: Option<usize>,
    stroke_pattern: Option<usize>,
    type3_font: Option<usize>,
}

struct StreamCollector<'b, 'h> {
    budget: &'b Budget,
    type3_hint: Option<&'h Type3Hint>,
    xobjects: HashMap<Box<[u8]>, u64>,
    ext_g_states: HashMap<Box<[u8]>, u64>,
    pattern_index: HashMap<Box<[u8]>, usize>,
    pattern_names: BudgetVec<'b, Box<[u8]>>,
    pattern_paints: BudgetVec<'b, u64>,
    font_refs: HashSet<Box<[u8]>>,
    type3_uses: BudgetVec<'b, (usize, [u64; 256])>,
    gstack: BudgetVec<'b, ScanGState>,
    scratch: BudgetVec<'b, u8>,
}

impl<'b, 'h> StreamCollector<'b, 'h> {
    fn new(budget: &'b Budget, type3_hint: Option<&'h Type3Hint>) -> Self {
        Self {
            budget,
            type3_hint,
            xobjects: HashMap::new(),
            ext_g_states: HashMap::new(),
            pattern_index: HashMap::new(),
            pattern_names: BudgetVec::new(budget),
            pattern_paints: BudgetVec::new(budget),
            font_refs: HashSet::new(),
            type3_uses: BudgetVec::new(budget),
            gstack: BudgetVec::new(budget),
            scratch: BudgetVec::new(budget),
        }
    }

    fn current_state(&self) -> ScanGState {
        self.gstack.as_slice().last().copied().unwrap_or_default()
    }

    fn ensure_state(&mut self) -> Result<&mut ScanGState> {
        if self.gstack.is_empty() {
            self.gstack.try_push(ScanGState::default())?;
        }
        Ok(self.gstack.as_mut_slice().last_mut().expect("non-empty"))
    }

    fn push_state(&mut self) -> Result<()> {
        if self.gstack.is_empty() {
            self.gstack.try_push(ScanGState::default())?;
        }
        if self.gstack.len() < MAX_GSTATE_DEPTH {
            let copy = self.current_state();
            self.gstack.try_push(copy)?;
        }
        Ok(())
    }

    fn pop_state(&mut self) {
        if self.gstack.len() > 1 {
            let _ = self.gstack.pop();
        }
    }

    fn record_xobject(&mut self, raw: &[u8]) -> Result<()> {
        let name = decode_pdf_name(raw, &mut self.scratch)?;
        if let Some(count) = self.xobjects.get_mut(name) {
            *count = count.saturating_add(1);
            return Ok(());
        }
        let owned = budgeted_bytes(name, self.budget)?;
        try_insert_map(&mut self.xobjects, owned, 1, self.budget)?;
        Ok(())
    }

    fn record_ext_g_state(&mut self, raw: &[u8]) -> Result<()> {
        let name = decode_pdf_name(raw, &mut self.scratch)?;
        let gs_font = self.type3_hint.and_then(|hint| hint.lookup_gs(name));
        if let Some(count) = self.ext_g_states.get_mut(name) {
            *count = count.saturating_add(1);
        } else {
            let owned = budgeted_bytes(name, self.budget)?;
            try_insert_map(&mut self.ext_g_states, owned, 1, self.budget)?;
        }
        if let Some(mapped) = gs_font {
            self.ensure_state()?.type3_font = mapped;
        }
        Ok(())
    }

    fn record_font(&mut self, raw: &[u8]) -> Result<()> {
        let name = decode_pdf_name(raw, &mut self.scratch)?;
        let t3 = self.type3_hint.and_then(|hint| hint.lookup_font(name));
        if !self.font_refs.contains(name) {
            let owned = budgeted_bytes(name, self.budget)?;
            try_insert_set(&mut self.font_refs, owned, self.budget)?;
        }
        if self.type3_hint.is_some() {
            self.ensure_state()?.type3_font = t3;
        }
        Ok(())
    }

    fn intern_pattern(&mut self, raw: &[u8]) -> Result<usize> {
        let name = decode_pdf_name(raw, &mut self.scratch)?;
        if let Some(&id) = self.pattern_index.get(name) {
            return Ok(id);
        }
        let id = self.pattern_names.len();
        let name_for_vec = budgeted_bytes(name, self.budget)?;
        let name_for_map = budgeted_bytes(name, self.budget)?;
        self.pattern_names.try_push(name_for_vec)?;
        self.pattern_paints.try_push(0)?;
        try_insert_map(&mut self.pattern_index, name_for_map, id, self.budget)?;
        Ok(id)
    }

    fn paint_pattern(&mut self, id: usize) {
        if let Some(slot) = self.pattern_paints.as_mut_slice().get_mut(id) {
            *slot = slot.saturating_add(1);
        }
    }

    fn record_type3_text(&mut self, font_idx: usize, data: &[u8], span: TextSpan) -> Result<()> {
        let pos = if let Some(at) = self
            .type3_uses
            .as_slice()
            .iter()
            .position(|(idx, _)| *idx == font_idx)
        {
            at
        } else {
            let at = self.type3_uses.len();
            self.type3_uses.try_push((font_idx, [0u64; 256]))?;
            at
        };
        let counts = &mut self.type3_uses.as_mut_slice()[pos].1;
        match span {
            TextSpan::Literal(range) => accumulate_literal_codes(&data[range], counts),
            TextSpan::Hex(range) => accumulate_hex_codes(&data[range], counts),
            TextSpan::Array(range) => accumulate_tj_array_codes(&data[range], counts),
        }
        Ok(())
    }

    fn finish(self, own_ops: u64, type3_res_id: Option<ResourcesId>) -> Result<CachedStreamScan> {
        let mut xobjects: BudgetVec<'_, (Box<[u8]>, u64)> = BudgetVec::new(self.budget);
        for (k, v) in self.xobjects {
            xobjects.try_push((k, v))?;
        }
        let mut ext_g_states: BudgetVec<'_, (Box<[u8]>, u64)> = BudgetVec::new(self.budget);
        for (k, v) in self.ext_g_states {
            ext_g_states.try_push((k, v))?;
        }
        let mut pattern_paints: BudgetVec<'_, (Box<[u8]>, u64)> = BudgetVec::new(self.budget);
        let mut pattern_refs: BudgetVec<'_, Box<[u8]>> = BudgetVec::new(self.budget);
        for (name, count) in self
            .pattern_names
            .into_vec()
            .into_iter()
            .zip(self.pattern_paints.into_vec())
        {
            if count > 0 {
                let copy = budgeted_bytes(&name, self.budget)?;
                pattern_paints.try_push((copy, count))?;
            }
            pattern_refs.try_push(name)?;
        }
        let mut font_refs: BudgetVec<'_, Box<[u8]>> = BudgetVec::new(self.budget);
        for k in self.font_refs {
            font_refs.try_push(k)?;
        }
        Ok(CachedStreamScan {
            own_ops,
            xobjects: xobjects.into_vec(),
            ext_g_states: ext_g_states.into_vec(),
            pattern_paints: pattern_paints.into_vec(),
            pattern_refs: pattern_refs.into_vec(),
            font_refs: font_refs.into_vec(),
            type3_res_id,
            type3_uses: self.type3_uses.into_vec(),
        })
    }
}

fn accumulate_literal_codes(slice: &[u8], counts: &mut [u64; 256]) {
    let mut i = 0usize;
    let len = slice.len();
    while i < len {
        let b = slice[i];
        if b == b'\\' && i + 1 < len {
            i += 1;
            let esc = slice[i];
            match esc {
                b'n' => {
                    counts[usize::from(b'\n')] = counts[usize::from(b'\n')].saturating_add(1);
                    i += 1;
                }
                b'r' => {
                    counts[usize::from(b'\r')] = counts[usize::from(b'\r')].saturating_add(1);
                    i += 1;
                }
                b't' => {
                    counts[usize::from(b'\t')] = counts[usize::from(b'\t')].saturating_add(1);
                    i += 1;
                }
                b'b' => {
                    counts[0x08] = counts[0x08].saturating_add(1);
                    i += 1;
                }
                b'f' => {
                    counts[0x0C] = counts[0x0C].saturating_add(1);
                    i += 1;
                }
                b'\r' => {
                    i += 1;
                    if i < len && slice[i] == b'\n' {
                        i += 1;
                    }
                }
                b'\n' => {
                    i += 1;
                }
                b'0'..=b'7' => {
                    let mut val = u32::from(esc - b'0');
                    i += 1;
                    for _ in 0..2 {
                        if i < len && matches!(slice[i], b'0'..=b'7') {
                            val = (val << 3) | u32::from(slice[i] - b'0');
                            i += 1;
                        } else {
                            break;
                        }
                    }
                    let byte = (val & 0xFF) as usize;
                    counts[byte] = counts[byte].saturating_add(1);
                }
                other => {
                    counts[usize::from(other)] = counts[usize::from(other)].saturating_add(1);
                    i += 1;
                }
            }
        } else {
            counts[usize::from(b)] = counts[usize::from(b)].saturating_add(1);
            i += 1;
        }
    }
}

fn accumulate_hex_codes(slice: &[u8], counts: &mut [u64; 256]) {
    let mut hi: Option<u8> = None;
    for &b in slice {
        let Some(nib) = hex_digit(b) else {
            continue;
        };
        match hi.take() {
            None => hi = Some(nib),
            Some(h) => {
                let byte = usize::from((h << 4) | nib);
                counts[byte] = counts[byte].saturating_add(1);
            }
        }
    }
    if let Some(h) = hi {
        let byte = usize::from(h << 4);
        counts[byte] = counts[byte].saturating_add(1);
    }
}

fn accumulate_tj_array_codes(slice: &[u8], counts: &mut [u64; 256]) {
    let mut i = 0usize;
    let len = slice.len();
    while i < len {
        match slice[i] {
            b'(' => {
                let start = i + 1;
                i = start;
                let mut parens = 1usize;
                while i < len && parens > 0 {
                    match slice[i] {
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
                let end = if parens == 0 { i.saturating_sub(1) } else { i };
                accumulate_literal_codes(&slice[start..end], counts);
            }
            b'<' if i + 1 < len && slice[i + 1] != b'<' => {
                let start = i + 1;
                i = start;
                while i < len && slice[i] != b'>' {
                    i += 1;
                }
                accumulate_hex_codes(&slice[start..i], counts);
                if i < len {
                    i += 1;
                }
            }
            b'%' => {
                i += 1;
                while i < len && slice[i] != b'\n' && slice[i] != b'\r' {
                    i += 1;
                }
            }
            _ => i += 1,
        }
    }
}

/// Single linear iterative pass over decoded content bytes.
///
/// Checks:
/// 1. Operand container nesting (`[`/`]`, `<<`/`>>`) <= `operand_limit`
///    (prevents `hayro-syntax`'s silent truncation past 512, #15 / #1666).
/// 2. Inline image (`BI ... ID ... EI`) nested lookahead hazards (#740):
///    tracks inline images whose post-`EI` tail contains another `EI` before 20
///    operators or a text-showing operator before the next `BI`, which triggers
///    `O(2^n)` time and `O(n)` stack frames in `hayro-syntax::content::read_inline_image`.
/// 3. Inline image candidate `EI` marker scan count <= `ei_candidate_limit` (#17 /
///    `LaurenzV/hayro#1667`), refusing quadratic `BI ... ID <many EI> ... EI` streams.
/// 4. Counts stream operators (`own_ops`), refusing early if `direct_op_limit` is
///    exceeded, and optionally collects invoked XObjects, ExtGStates, painted
///    tiling patterns, and Type 3 glyph code counts for DAG fuel accounting (#1275).
fn scan_content_bytes<'b>(
    data: &[u8],
    operand_limit: Option<usize>,
    ei_candidate_limit: Option<usize>,
    direct_op_limit: Option<u64>,
    budget: &'b Budget,
    report: &mut Report,
    mut collector: Option<&mut StreamCollector<'b, '_>>,
) -> Result<u64> {
    let mut i = 0usize;
    let len = data.len();
    let mut depth = 0usize;
    let mut max_depth = 0usize;
    let mut own_ops = 0u64;
    let mut inline_images = 0u64;
    let mut lookahead_chain = 0usize;
    let mut max_lookahead_chain = 0usize;
    let mut prev_was_bi = false;
    let mut in_post_bi_tail = false;
    let mut ei_candidates_for_bi = 0usize;
    let mut max_ei_candidates = 0usize;
    let mut saw_raw_ei_since_bi = false;
    let mut ops_since_bi = 0usize;
    let mut next_poll = BYTE_POLL_INTERVAL;
    // Last two top-level operands (name ranges, if each operand was a `/Name`).
    let mut prev_operand: Option<Range<usize>> = None;
    let mut prev_prev_operand: Option<Range<usize>> = None;
    let mut last_text: Option<TextSpan> = None;
    let mut top_array_start = 0usize;

    let bump_ei = |candidates: &mut usize, max_seen: &mut usize, add: usize| -> Result<()> {
        if add == 0 {
            return Ok(());
        }
        *candidates = candidates.saturating_add(add);
        if *candidates > *max_seen {
            *max_seen = *candidates;
        }
        if let Some(limit) = ei_candidate_limit
            && *candidates > limit
        {
            return Err(fuel_refusal(
                "inline image candidate EI scan count exceeded limit (#17)",
                limit as u64,
                *candidates as u64,
            ));
        }
        Ok(())
    };

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
                if in_post_bi_tail {
                    bump_ei(
                        &mut ei_candidates_for_bi,
                        &mut max_ei_candidates,
                        count_candidate_ei_in_slice(&data[start..i], true),
                    )?;
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
                let content_end = if parens == 0 { i.saturating_sub(1) } else { i };
                if prev_was_bi && !saw_raw_ei_since_bi && contains_ei(&data[start..i]) {
                    saw_raw_ei_since_bi = true;
                }
                if in_post_bi_tail {
                    bump_ei(
                        &mut ei_candidates_for_bi,
                        &mut max_ei_candidates,
                        count_candidate_ei_in_slice(&data[start..content_end], false),
                    )?;
                }
                if depth == 0 {
                    prev_prev_operand = prev_operand.take();
                    last_text = Some(TextSpan::Literal(start..content_end));
                }
            }
            b'<' => {
                if i + 1 < len && data[i + 1] == b'<' {
                    depth += 1;
                    if depth > max_depth {
                        max_depth = depth;
                    }
                    if let Some(limit) = operand_limit
                        && depth > limit
                    {
                        return Err(depth_refusal(
                            "content operand nesting exceeded limit",
                            limit,
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
                    let hex_end = i;
                    if prev_was_bi && !saw_raw_ei_since_bi && contains_ei(&data[start..hex_end]) {
                        saw_raw_ei_since_bi = true;
                    }
                    if in_post_bi_tail {
                        bump_ei(
                            &mut ei_candidates_for_bi,
                            &mut max_ei_candidates,
                            count_candidate_ei_in_slice(&data[start..hex_end], false),
                        )?;
                    }
                    if i < len {
                        i += 1;
                    }
                    if depth == 0 {
                        prev_prev_operand = prev_operand.take();
                        last_text = Some(TextSpan::Hex(start..hex_end));
                    }
                }
            }
            b'>' if i + 1 < len && data[i + 1] == b'>' => {
                depth = depth.saturating_sub(1);
                if depth == 0 {
                    prev_prev_operand = prev_operand.take();
                    last_text = None;
                }
                i += 2;
            }
            b'[' => {
                if depth == 0 {
                    top_array_start = i + 1;
                }
                depth += 1;
                if depth > max_depth {
                    max_depth = depth;
                }
                if let Some(limit) = operand_limit
                    && depth > limit
                {
                    return Err(depth_refusal(
                        "content operand nesting exceeded limit",
                        limit,
                        depth,
                    ));
                }
                i += 1;
            }
            b']' => {
                if depth == 1 {
                    last_text = Some(TextSpan::Array(top_array_start..i));
                }
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
                if in_post_bi_tail {
                    let trailing_ws = i == len || is_whitespace(data[i]);
                    bump_ei(
                        &mut ei_candidates_for_bi,
                        &mut max_ei_candidates,
                        count_candidate_ei_in_slice(&data[start..i], trailing_ws),
                    )?;
                }
                if depth == 0 {
                    prev_prev_operand = prev_operand.take();
                    prev_operand = Some(start..i);
                    last_text = None;
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
                    own_ops = own_ops.saturating_add(1);
                    if let Some(limit) = direct_op_limit
                        && own_ops > limit
                    {
                        return Err(fuel_refusal(
                            "content stream operator fuel exceeded limit",
                            limit,
                            own_ops,
                        ));
                    }
                    if operand_limit.is_some()
                        && prev_was_bi
                        && saw_raw_ei_since_bi
                        && ops_since_bi < LOOKAHEAD_OPERATOR_LIMIT
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
                    in_post_bi_tail = true;
                    saw_raw_ei_since_bi = false;
                    ops_since_bi = 0;
                    prev_operand = None;
                    prev_prev_operand = None;
                    last_text = None;
                    let Some(after_id) =
                        scan_inline_dict(data, i, depth, operand_limit, &mut max_depth, budget)?
                    else {
                        break;
                    };
                    let Some((ei_pos, candidates)) =
                        find_candidate_ei(data, after_id, ei_candidate_limit, budget)?
                    else {
                        break;
                    };
                    ei_candidates_for_bi = candidates;
                    if ei_candidates_for_bi > max_ei_candidates {
                        max_ei_candidates = ei_candidates_for_bi;
                    }
                    i = (ei_pos + 2).min(len);
                    next_poll = i.saturating_add(BYTE_POLL_INTERVAL);
                } else if tok == b"EI" {
                    if in_post_bi_tail {
                        bump_ei(&mut ei_candidates_for_bi, &mut max_ei_candidates, 1)?;
                    }
                    if prev_was_bi {
                        // Stray `EI` following an inline image before the next `BI`:
                        // direct trigger for `hayro-syntax`'s recursive
                        // `starts_with_content` lookahead (#740).
                        if operand_limit.is_some() {
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
                        prev_was_bi = false;
                        saw_raw_ei_since_bi = false;
                    }
                    if depth == 0 {
                        own_ops = own_ops.saturating_add(1);
                        if let Some(limit) = direct_op_limit
                            && own_ops > limit
                        {
                            return Err(fuel_refusal(
                                "content stream operator fuel exceeded limit",
                                limit,
                                own_ops,
                            ));
                        }
                        prev_operand = None;
                        prev_prev_operand = None;
                        last_text = None;
                    }
                } else {
                    if in_post_bi_tail {
                        let trailing_ws = i == len || is_whitespace(data[i]);
                        bump_ei(
                            &mut ei_candidates_for_bi,
                            &mut max_ei_candidates,
                            count_candidate_ei_in_slice(tok, trailing_ws),
                        )?;
                    }
                    if depth == 0 {
                        if is_number_or_keyword(tok) {
                            prev_prev_operand = prev_operand.take();
                            last_text = None;
                        } else {
                            // Content operator.
                            own_ops = own_ops.saturating_add(1);
                            if let Some(limit) = direct_op_limit
                                && own_ops > limit
                            {
                                return Err(fuel_refusal(
                                    "content stream operator fuel exceeded limit",
                                    limit,
                                    own_ops,
                                ));
                            }
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
                            let text_arg = last_text.take();
                            if let Some(ref mut coll) = collector {
                                match tok {
                                    b"q" => coll.push_state()?,
                                    b"Q" => coll.pop_state(),
                                    b"Do" => {
                                        if let Some(range) = prev_operand.take() {
                                            coll.record_xobject(&data[range])?;
                                        }
                                    }
                                    b"Tf" => {
                                        if let Some(range) = prev_prev_operand.take() {
                                            coll.record_font(&data[range])?;
                                        }
                                    }
                                    b"gs" => {
                                        if let Some(range) = prev_operand.take() {
                                            coll.record_ext_g_state(&data[range])?;
                                        }
                                    }
                                    b"cs" | b"sc" | b"g" | b"rg" | b"k" => {
                                        if !coll.gstack.is_empty() {
                                            coll.ensure_state()?.fill_pattern = None;
                                        }
                                    }
                                    b"CS" | b"SC" | b"G" | b"RG" | b"K" => {
                                        if !coll.gstack.is_empty() {
                                            coll.ensure_state()?.stroke_pattern = None;
                                        }
                                    }
                                    b"scn" => {
                                        if let Some(range) = prev_operand.take() {
                                            let id = coll.intern_pattern(&data[range])?;
                                            coll.ensure_state()?.fill_pattern = Some(id);
                                        } else if !coll.gstack.is_empty() {
                                            coll.ensure_state()?.fill_pattern = None;
                                        }
                                    }
                                    b"SCN" => {
                                        if let Some(range) = prev_operand.take() {
                                            let id = coll.intern_pattern(&data[range])?;
                                            coll.ensure_state()?.stroke_pattern = Some(id);
                                        } else if !coll.gstack.is_empty() {
                                            coll.ensure_state()?.stroke_pattern = None;
                                        }
                                    }
                                    b"f" | b"F" | b"f*" => {
                                        if let Some(id) = coll.current_state().fill_pattern {
                                            coll.paint_pattern(id);
                                        }
                                    }
                                    b"S" | b"s" => {
                                        if let Some(id) = coll.current_state().stroke_pattern {
                                            coll.paint_pattern(id);
                                        }
                                    }
                                    b"B" | b"B*" | b"b" | b"b*" => {
                                        let st = coll.current_state();
                                        if let Some(id) = st.fill_pattern {
                                            coll.paint_pattern(id);
                                        }
                                        if let Some(id) = st.stroke_pattern {
                                            coll.paint_pattern(id);
                                        }
                                    }
                                    b"Tj" | b"TJ" | b"'" | b"\"" => {
                                        let st = coll.current_state();
                                        if let Some(id) = st.fill_pattern {
                                            coll.paint_pattern(id);
                                        }
                                        if let Some(font_idx) = st.type3_font
                                            && let Some(span) = text_arg
                                        {
                                            coll.record_type3_text(font_idx, data, span)?;
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
    if max_ei_candidates > report.max_inline_ei_candidates {
        report.max_inline_ei_candidates = max_ei_candidates;
    }
    Ok(own_ops)
}

fn contains_ei(slice: &[u8]) -> bool {
    slice.windows(2).any(|w| w == b"EI")
}

/// Counts occurrences of `EI` in `slice` followed by PDF whitespace (or end of
/// `slice` when `trailing_ws` is true), matching `hayro-syntax::content::read_inline_image`'s
/// outer candidate condition.
fn count_candidate_ei_in_slice(slice: &[u8], trailing_ws: bool) -> usize {
    let len = slice.len();
    if len < 2 {
        return 0;
    }
    let mut count = 0usize;
    let mut i = 0usize;
    while i + 1 < len {
        if slice[i] == b'E' && slice[i + 1] == b'I' {
            let after_ok = if i + 2 < len {
                is_whitespace(slice[i + 2])
            } else {
                trailing_ws
            };
            if after_ok {
                count = count.saturating_add(1);
                i += 2;
                continue;
            }
        }
        i += 1;
    }
    count
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
    operand_limit: Option<usize>,
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
                if let Some(limit) = operand_limit
                    && depth > limit
                {
                    return Err(depth_refusal(
                        "inline image dictionary nesting exceeded limit",
                        limit,
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
                if let Some(limit) = operand_limit
                    && depth > limit
                {
                    return Err(depth_refusal(
                        "inline image dictionary nesting exceeded limit",
                        limit,
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

/// Find the first `EI` preceded by whitespace (or at start) and followed by
/// whitespace or EOF, counting every `EI` followed by whitespace or EOF against
/// `ei_limit` (#17 / `LaurenzV/hayro#1667`).
fn find_candidate_ei(
    data: &[u8],
    start: usize,
    ei_limit: Option<usize>,
    budget: &Budget,
) -> Result<Option<(usize, usize)>> {
    let len = data.len();
    let mut i = start;
    let mut candidates = 0usize;
    let mut next_poll = start.saturating_add(BYTE_POLL_INTERVAL);
    while i + 1 < len {
        if i >= next_poll {
            budget.poll()?;
            next_poll = i.saturating_add(BYTE_POLL_INTERVAL);
        }
        if data[i] == b'E' && data[i + 1] == b'I' {
            let after_ok = i + 2 == len || is_whitespace(data[i + 2]);
            if after_ok {
                candidates = candidates.saturating_add(1);
                if let Some(limit) = ei_limit
                    && candidates > limit
                {
                    return Err(fuel_refusal(
                        "inline image candidate EI scan count exceeded limit (#17)",
                        limit as u64,
                        candidates as u64,
                    ));
                }
                let before_ok = i == start || is_whitespace(data[i - 1]);
                if before_ok {
                    return Ok(Some((i, candidates)));
                }
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
                let report =
                    validate_page(&mut reads, page, Some(&mut shared)).unwrap_or_else(|err| {
                        panic!(
                            "false positive on {} page {}: {err}",
                            path.display(),
                            idx + 1
                        )
                    });
                assert!(report.effective_operators <= DEFAULT_INTERPRETATION_FUEL);
                assert!(report.max_inline_ei_candidates <= MAX_INLINE_IMAGE_EI_CANDIDATES);
                assert!(report.max_type4_operators <= MAX_TYPE4_OPERATORS);
                assert!(report.max_type0_samples <= MAX_TYPE0_SAMPLES);
            }
        }
    }

    fn build_binary_form_dag(levels: usize, own_resources: bool) -> Vec<u8> {
        let mut extra = Vec::with_capacity(levels + 1);
        for level in 0..=levels {
            let obj_num = 5 + level;
            let body = if level < levels {
                format!("/F{} Do /F{} Do", level + 1, level + 1)
            } else {
                "0 0 10 10 re f".to_string()
            };
            let res_entry = if own_resources && level < levels {
                format!(
                    "/Resources<</XObject<</F{} {} 0 R>>>>",
                    level + 1,
                    obj_num + 1
                )
            } else if own_resources {
                "/Resources<<>>".to_string()
            } else {
                String::new()
            };
            let form = format!(
                "<</Type/XObject/Subtype/Form/BBox[0 0 100 100]{res_entry}/Length {}>>stream\n{body}\nendstream",
                body.len()
            );
            extra.push(form.into_bytes());
        }
        let page_res = if own_resources {
            b"<</XObject<</F0 5 0 R>>>>".to_vec()
        } else {
            let mut entries = String::from("<</XObject<<");
            for level in 0..=levels {
                let _ = write!(entries, "/F{} {} 0 R ", level, 5 + level);
            }
            entries.push_str(">>>>");
            entries.into_bytes()
        };
        one_page(b"/F0 Do", &page_res, &extra)
    }

    #[test]
    fn binary_form_xobject_bomb_refuses_in_microseconds() {
        // Small 4-level binary DAG computes exact bottom-up operator multiplicity:
        // leaf=2, L3=6, L2=14, L1=30, L0=62, page(/F0 Do)=1+62=63.
        for own_res in [true, false] {
            let (doc, pages) = open_doc(build_binary_form_dag(4, own_res));
            let version = doc.current();
            let budget = web_budget();
            let mut reads = Reads::new(&version, &budget);
            let report = validate_page(&mut reads, &pages.pages()[0], None)
                .expect("4-level binary form DAG must pass");
            assert_eq!(report.effective_operators, 63);
            assert_eq!(budget.used(LimitCode::InterpretationFuel), 63);
        }

        // 20-level binary Form bomb (> 2^20 effective operators) refuses immediately.
        for own_res in [true, false] {
            let (doc, pages) = open_doc(build_binary_form_dag(20, own_res));
            let version = doc.current();
            let budget = web_budget();
            let started = std::time::Instant::now();
            let err = validate_page(&mut Reads::new(&version, &budget), &pages.pages()[0], None)
                .expect_err("20-level binary Form bomb must refuse");
            let elapsed = started.elapsed();
            assert_eq!(
                err.limit().map(LimitExceeded::code),
                Some(LimitCode::InterpretationFuel),
                "own_res={own_res}: unexpected error {err}"
            );
            assert!(
                elapsed.as_millis() < 250,
                "own_res={own_res}: bomb refusal took {elapsed:?}"
            );
        }
    }

    #[test]
    fn quadratic_inline_image_ei_candidates_refuse() {
        let budget = web_budget();
        // A legitimate inline image with a few fake `aEI ` markers passes cleanly.
        let mut safe = b"BI /W 1 /H 1 ID ".to_vec();
        for _ in 0..10 {
            safe.extend_from_slice(b"aEI ");
        }
        safe.extend_from_slice(b" EI q Q");
        validate_content_bytes(&safe, &budget).expect("10 candidate EI markers must pass");

        // Adversarial streams exceeding MAX_INLINE_IMAGE_EI_CANDIDATES refuse with InterpretationFuel.
        let cases: Vec<(&str, Vec<u8>)> = vec![
            (
                "standalone-ei-flood",
                [b"BI /W 1 /H 1 ID x ".as_slice(), &b"EI ".repeat(100)].concat(),
            ),
            (
                "binary-ei-flood",
                [
                    b"BI /W 1 /H 1 ID ".as_slice(),
                    &b"\xff EI ".repeat(100),
                    b"EI",
                ]
                .concat(),
            ),
            (
                "non-ws-prefix-ei-flood",
                [b"BI /W 1 /H 1 ID ".as_slice(), &b"aEI ".repeat(100), b" EI"].concat(),
            ),
            (
                "post-bi-string-ei-flood",
                [
                    b"BI /W 1 /H 1 ID x EI ".as_slice(),
                    &b"(EI ) Tj ".repeat(100),
                    b"\xff EI",
                ]
                .concat(),
            ),
        ];
        for (name, stream) in cases {
            let err = validate_content_bytes(&stream, &budget)
                .expect_err(&format!("{name}: validate_content_bytes must refuse"));
            assert_eq!(
                err.limit().map(LimitExceeded::code),
                Some(LimitCode::InterpretationFuel),
                "{name}: unexpected error {err}"
            );

            let (doc, pages) = open_doc(one_page(&stream, b"<<>>", &[]));
            let err = validate_page(
                &mut Reads::new(&doc.current(), &web_budget()),
                &pages.pages()[0],
                None,
            )
            .expect_err(&format!("{name}: validate_page must refuse"));
            assert_eq!(
                err.limit().map(LimitExceeded::code),
                Some(LimitCode::InterpretationFuel),
                "{name}: unexpected page error {err}"
            );
        }
    }

    #[test]
    fn type0_sampled_function_size_product_refuses() {
        // Legitimate 3D 17x17x17 RGB LUT (14,739 samples <= MAX_TYPE0_SAMPLES) passes.
        let good_fn = b"<</FunctionType 0/Domain[0 1 0 1 0 1]/Range[0 1 0 1 0 1]/Size[17 17 17]/BitsPerSample 8/Length 1>>stream\n\x00\nendstream".to_vec();
        let sh =
            b"<</ShadingType 2/ColorSpace/DeviceRGB/Coords[0 0 100 0]/Function 6 0 R>>".to_vec();
        let (doc, pages) = open_doc(one_page(
            b"/Sh0 sh",
            b"<</Shading<</Sh0 5 0 R>>>>",
            &[sh.clone(), good_fn],
        ));
        let report = validate_page(
            &mut Reads::new(&doc.current(), &web_budget()),
            &pages.pages()[0],
            None,
        )
        .expect("normal 17^3 Type 0 function must pass");
        assert_eq!(report.max_type0_samples, 17 * 17 * 17 * 3);

        // Huge /Size product directly exceeds MAX_TYPE0_SAMPLES.
        let huge_size = b"<</FunctionType 0/Domain[0 1 0 1]/Range[0 1]/Size[2048 2048]/BitsPerSample 1/Length 1>>stream\n\x00\nendstream".to_vec();
        let (doc, pages) = open_doc(one_page(
            b"/Sh0 sh",
            b"<</Shading<</Sh0 5 0 R>>>>",
            &[sh.clone(), huge_size],
        ));
        let err = validate_page(
            &mut Reads::new(&doc.current(), &web_budget()),
            &pages.pages()[0],
            None,
        )
        .unwrap_err();
        assert_eq!(
            err.limit().map(LimitExceeded::code),
            Some(LimitCode::InterpretationFuel)
        );

        // /Size product fits alone (1024*1024 == 1_048_576) but * 3 outputs overflows MAX_TYPE0_SAMPLES.
        let huge_range = b"<</FunctionType 0/Domain[0 1 0 1]/Range[0 1 0 1 0 1]/Size[1024 1024]/BitsPerSample 1/Length 1>>stream\n\x00\nendstream".to_vec();
        let (doc, pages) = open_doc(one_page(
            b"/Sh0 sh",
            b"<</Shading<</Sh0 5 0 R>>>>",
            &[sh.clone(), huge_range],
        ));
        let err = validate_page(
            &mut Reads::new(&doc.current(), &web_budget()),
            &pages.pages()[0],
            None,
        )
        .unwrap_err();
        assert_eq!(
            err.limit().map(LimitExceeded::code),
            Some(LimitCode::InterpretationFuel)
        );

        // Oversized Type 0 nested inside a Type 3 stitching function also refuses.
        let stitch =
            b"<</FunctionType 3/Domain[0 1]/Bounds[]/Encode[0 1]/Functions[7 0 R]>>".to_vec();
        let huge_nested = b"<</FunctionType 0/Domain[0 1]/Range[0 1]/Size[5000000]/BitsPerSample 1/Length 1>>stream\n\x00\nendstream".to_vec();
        let (doc, pages) = open_doc(one_page(
            b"/Sh0 sh",
            b"<</Shading<</Sh0 5 0 R>>>>",
            &[sh, stitch, huge_nested],
        ));
        let err = validate_page(
            &mut Reads::new(&doc.current(), &web_budget()),
            &pages.pages()[0],
            None,
        )
        .unwrap_err();
        assert_eq!(
            err.limit().map(LimitExceeded::code),
            Some(LimitCode::InterpretationFuel)
        );
    }

    #[test]
    fn type4_postscript_operator_count_refuses() {
        // Legitimate small Type 4 procedure passes.
        let small = b"{ dup 0.5 mul exch 1 exch sub add }";
        let good_fn = format!(
            "<</FunctionType 4/Domain[0 1]/Range[0 1]/Length {}>>stream\n{}\nendstream",
            small.len(),
            std::str::from_utf8(small).unwrap()
        )
        .into_bytes();
        let sh =
            b"<</ShadingType 2/ColorSpace/DeviceGray/Coords[0 0 100 0]/Function 6 0 R>>".to_vec();
        let (doc, pages) = open_doc(one_page(
            b"/Sh0 sh",
            b"<</Shading<</Sh0 5 0 R>>>>",
            &[sh.clone(), good_fn],
        ));
        let report = validate_page(
            &mut Reads::new(&doc.current(), &web_budget()),
            &pages.pages()[0],
            None,
        )
        .expect("normal Type 4 function must pass");
        assert_eq!(report.max_type4_braces, 1);
        assert_eq!(report.max_type4_operators, 9);

        // Flat massive Type 4 procedure (brace depth 1, 20,001 tokens > MAX_TYPE4_OPERATORS) refuses.
        let mut big = Vec::from(&b"{"[..]);
        for _ in 0..10_000 {
            big.extend_from_slice(b" 1 pop");
        }
        big.push(b'}');
        let mut big_fn = Vec::new();
        let _ = writeln!(
            ByteWriter(&mut big_fn),
            "<</FunctionType 4/Domain[0 1]/Range[0 1]/Length {}>>stream",
            big.len()
        );
        big_fn.extend_from_slice(&big);
        big_fn.extend_from_slice(b"\nendstream");
        let (doc, pages) = open_doc(one_page(
            b"/Sh0 sh",
            b"<</Shading<</Sh0 5 0 R>>>>",
            &[sh, big_fn],
        ));
        let err = validate_page(
            &mut Reads::new(&doc.current(), &web_budget()),
            &pages.pages()[0],
            None,
        )
        .unwrap_err();
        assert_eq!(
            err.limit().map(LimitExceeded::code),
            Some(LimitCode::InterpretationFuel)
        );
    }

    #[test]
    fn tiling_pattern_softmask_annotation_and_type3_fuel() {
        // 1. Tiling pattern with huge BBox/Step tile copies refuses.
        let cell = b"0 0 1 1 re f";
        let huge_tiling = format!(
            "<</Type/Pattern/PatternType 1/PaintType 1/TilingType 1/BBox[0 0 10000 10000]/XStep 1/YStep 1/Resources<<>>/Length {}>>stream\n{}\nendstream",
            cell.len(),
            std::str::from_utf8(cell).unwrap()
        )
        .into_bytes();
        let (doc, pages) = open_doc(one_page(
            b"/Pattern cs /P0 scn 0 0 50 50 re f",
            b"<</Pattern<</P0 5 0 R>>>>",
            &[huge_tiling],
        ));
        let err = validate_page(
            &mut Reads::new(&doc.current(), &web_budget()),
            &pages.pages()[0],
            None,
        )
        .unwrap_err();
        assert_eq!(
            err.limit().map(LimitExceeded::code),
            Some(LimitCode::InterpretationFuel)
        );

        // 2. Type 3 glyph invocation counts Tj + TJ + hex string multiplicities accurately.
        let glyph = b"100 0 d0 0 0 10 10 re f"; // 3 operators
        let cp = format!(
            "<</Length {}>>stream\n{}\nendstream",
            glyph.len(),
            std::str::from_utf8(glyph).unwrap()
        )
        .into_bytes();
        let font = b"<</Type/Font/Subtype/Type3/FontBBox[0 0 100 100]/FontMatrix[0.01 0 0 0.01 0 0]/CharProcs<</a 6 0 R>>/Encoding<</Type/Encoding/Differences[97 /a]>>/FirstChar 97/LastChar 97/Widths[100]>>".to_vec();
        // Content: BT /F0 10 Tf (aaa) Tj [(a) -10 <6161>] TJ ET -> 5 own ops + 6 glyph calls * 3 ops = 23 ops.
        let (doc, pages) = open_doc(one_page(
            b"BT /F0 10 Tf (aaa) Tj [(a) -10 <6161>] TJ ET",
            b"<</Font<</F0 5 0 R>>>>",
            &[font, cp],
        ));
        let report = validate_page(
            &mut Reads::new(&doc.current(), &web_budget()),
            &pages.pages()[0],
            None,
        )
        .expect("normal Type 3 page must pass");
        assert_eq!(report.effective_operators, 5 + 6 * 3);

        // 3. SoftMask /G invoked via `gs` contributes its operators.
        let sm_stream = b"0 0 10 10 re f 0 0 5 5 re f"; // 4 operators
        let group = format!(
            "<</Type/XObject/Subtype/Form/BBox[0 0 100 100]/Group<</S/Transparency/CS/DeviceGray>>/Resources<<>>/Length {}>>stream\n{}\nendstream",
            sm_stream.len(),
            std::str::from_utf8(sm_stream).unwrap()
        )
        .into_bytes();
        let gs = b"<</Type/ExtGState/SMask<</Type/Mask/S/Alpha/G 6 0 R>>>>".to_vec();
        // Page: /GS0 gs /GS0 gs (2 own ops + 2 * 4 = 10 ops).
        let (doc, pages) = open_doc(one_page(
            b"/GS0 gs /GS0 gs",
            b"<</ExtGState<</GS0 5 0 R>>>>",
            &[gs, group],
        ));
        let report = validate_page(
            &mut Reads::new(&doc.current(), &web_budget()),
            &pages.pages()[0],
            None,
        )
        .expect("SoftMask page must pass");
        assert_eq!(report.effective_operators, 10);

        // 4. Visible annotation /AP /N contributes operators; hidden annotation (/F 2) does not.
        let ap_body = b"0 0 10 10 re f"; // 2 ops
        let ap_stream = format!(
            "<</Type/XObject/Subtype/Form/BBox[0 0 10 10]/Length {}>>stream\n{}\nendstream",
            ap_body.len(),
            std::str::from_utf8(ap_body).unwrap()
        )
        .into_bytes();
        let visible_annot =
            b"<</Type/Annot/Subtype/Stamp/Rect[0 0 10 10]/AP<</N 6 0 R>>>>".to_vec();
        let hidden_annot =
            b"<</Type/Annot/Subtype/Stamp/F 2/Rect[0 0 10 10]/AP<</N 6 0 R>>>>".to_vec();
        let objs = vec![
            b"<</Type/Catalog/Pages 2 0 R>>".to_vec(),
            b"<</Type/Pages/Kids[3 0 R]/Count 1>>".to_vec(),
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]/Contents 4 0 R/Annots[5 0 R 7 0 R]>>"
                .to_vec(),
            b"<</Length 3>>stream\nq Q\nendstream".to_vec(),
            visible_annot,
            ap_stream,
            hidden_annot,
        ];
        let (doc, pages) = open_doc(build_pdf(&objs));
        let report = validate_page(
            &mut Reads::new(&doc.current(), &web_budget()),
            &pages.pages()[0],
            None,
        )
        .expect("annotation page must pass");
        assert_eq!(report.effective_operators, 2 + 2);
    }

    #[test]
    fn tightening_interpretation_fuel_separates_preflight_cache_and_charges_hits() {
        let (doc, pages) = open_doc(build_binary_form_dag(4, true)); // 63 effective ops
        let version = doc.current();
        let page = &pages.pages()[0];
        let cache = PreflightCache::default();

        // First call with generous explicit fuel limit computes and caches.
        let loose = web_budget().with_limit(LimitCode::InterpretationFuel, Some(10_000));
        cache.validate_page(&version, page, &loose).unwrap();
        assert_eq!(
            cache.stats(),
            Stats {
                hits: 0,
                computations: 1
            }
        );
        assert_eq!(loose.used(LimitCode::InterpretationFuel), 63);

        // Second call with identical fuel limit hits the F4 memo AND charges its fresh budget.
        let loose2 = web_budget().with_limit(LimitCode::InterpretationFuel, Some(10_000));
        cache.validate_page(&version, page, &loose2).unwrap();
        assert_eq!(
            cache.stats(),
            Stats {
                hits: 1,
                computations: 1
            }
        );
        assert_eq!(loose2.used(LimitCode::InterpretationFuel), 63);

        // Tightening InterpretationFuel below 63 on the same Document/Version separates the cache
        // key (does NOT reuse the looser cached pass) and refuses cleanly!
        let tight = web_budget().with_limit(LimitCode::InterpretationFuel, Some(50));
        let err = cache.validate_page(&version, page, &tight).unwrap_err();
        assert_eq!(
            err.limit().map(LimitExceeded::code),
            Some(LimitCode::InterpretationFuel)
        );
        assert_eq!(
            cache.stats(),
            Stats {
                hits: 1,
                computations: 2
            }
        );

        // Fuel-only activation (ObjectDepth = None, InterpretationFuel = Some(_)) is active
        // and enforces fuel without raising ObjectDepth on deep operand nesting.
        let fuel_only_tight = Budget::default().with_limit(LimitCode::InterpretationFuel, Some(50));
        assert!(is_active(&fuel_only_tight));
        assert_eq!(content_operand_limit(&fuel_only_tight), None);
        let err = cache
            .validate_page(&version, page, &fuel_only_tight)
            .unwrap_err();
        assert_eq!(
            err.limit().map(LimitExceeded::code),
            Some(LimitCode::InterpretationFuel)
        );

        // Multi-page validate_pages sharing one explicit fuel budget charges on cache hits too:
        // 3 * 63 = 189 > 150 must refuse on the third visit!
        let shared_batch = web_budget().with_limit(LimitCode::InterpretationFuel, Some(150));
        let err = cache
            .validate_pages(&version, &pages, &[0, 0, 0], &shared_batch)
            .unwrap_err();
        assert_eq!(
            err.limit().map(LimitExceeded::code),
            Some(LimitCode::InterpretationFuel)
        );

        // Fuel-only budget does NOT refuse deep operand nesting with ObjectDepth.
        let mut deep_body = vec![b'['; 600];
        deep_body.extend(std::iter::repeat_n(b']', 600));
        deep_body.extend_from_slice(b" pop");
        let (deep_doc, deep_pages) = open_doc(one_page(&deep_body, b"<<>>", &[]));
        let fuel_only_loose =
            Budget::default().with_limit(LimitCode::InterpretationFuel, Some(10_000));
        validate_page(
            &mut Reads::new(&deep_doc.current(), &fuel_only_loose),
            &deep_pages.pages()[0],
            None,
        )
        .expect("ObjectDepth=None with loose InterpretationFuel must not refuse operand depth");
    }
}
