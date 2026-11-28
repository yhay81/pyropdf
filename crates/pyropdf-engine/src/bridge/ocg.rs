//! Temporary Rust bridge lowering for optional-content visibility expressions
//! (`/VE`), direct OCMDs, and annotation `/OC` (DESIGN.md §6 workaround for
//! yhay81/pyropdf#372; tracked in #7 and #1270).
//!
//! Pinned `hayro-interpret` reads `/OCProperties /D` (`/BaseState`, `/ON`,
//! `/OFF`), indirect single OCGs, and indirect OCMDs using `/OCGs` + `/P`, but:
//!
//! 1. Ignores `/VE` visibility expressions on OCMDs (ISO 32000-2 §8.11.2.2).
//! 2. Ignores direct OCMD dictionaries inside `/Resources /Properties` because
//!    it looks up entries with `properties.get_ref(name)`.
//! 3. Ignores annotation `/OC` entries during `interpret_annotation`.
//! 4. Requires `/Type /OCMD` on indirect OCMDs and treats null/missing `/OCGs`
//!    references as active rather than ignoring them.
//!
//! This module evaluates the default configuration's active group set once in
//! Rust and emits ephemeral stand-in objects for the bridge's in-memory update,
//! without mutating the store `Version` or saved PDF bytes:
//!
//! - Indirect OCMDs needing lowering are replaced in the bridge with a lowered
//!   `/Type /OCMD` dictionary (`/OCGs []` when visible, or
//!   `/OCGs [synth_off] /P /AllOn` when hidden).
//! - Direct hidden OCMDs in `/Resources /Properties` are replaced with an
//!   indirect reference to `synth_off` (or a lowered indirect OCMD when the
//!   dictionary carries non-OCMD keys).
//! - Direct XObject `/OC` dictionaries needing lowering are lowered on the
//!   ephemeral XObject stream dictionary.
//! - Annotations whose `/OC` evaluates to hidden receive the `Hidden` flag
//!   (bit 2, `1 << 1`) in the bridged view and are skipped during appearance
//!   synthesis.
//! - A single synthetic hidden OCG (`synth_off`) is allocated and added to the
//!   bridged `/OCProperties /D /OFF` only when at least one lowered OCMD
//!   evaluates to hidden.
//!
//! Documents without `/OCProperties` return immediately with zero allocations
//! and zero extra objects (ISO 32000-2 §8.11.4.2).
//!
//! Removal condition (tracked in yhay81/pyropdf#7): delete this module once
//! pyropdf adopts a released `hayro` containing `LaurenzV/hayro#1691` and
//! `LaurenzV/hayro#1740` (`#372`, `#373`).

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode, try_insert_map, try_insert_set};
use pyropdf_store::pages::{Page, PageIndex};
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::value::{Array, Dict, Name, PdfString, Reference, Value};
use pyropdf_store::version::Version;
use pyropdf_store::{Error, LimitExceeded, Result};

/// Maximum nesting levels of a `/VE` visibility expression (matches `doc.layers.visible`).
const MAX_EXPRESSION_DEPTH: usize = 32;
/// Maximum total operands evaluated in one `/VE` visibility expression.
const MAX_EXPRESSION_OPERANDS: u64 = 4_096;
/// Maximum indirect reference chain hops followed for one lookup.
const MAX_REF_CHAIN: usize = 16;
/// Maximum `/Parent` hops walked to find inherited page `/Resources`.
const MAX_PARENT_CHAIN: usize = 64;
/// Maximum nested interpreted stream depth walked for optional content.
const MAX_STREAM_DEPTH: usize = 50;
/// How often graph loops poll the budget for deadline / cancellation.
const POLL_INTERVAL: usize = 256;
/// Annotation `/F` flag bit 2 (`Hidden`, ISO 32000-2 Table 167).
const ANNOTATION_HIDDEN: u32 = 1 << 1;
/// Annotation `/F` flag bit 6 (`NoView`, ISO 32000-2 Table 167).
const ANNOTATION_NO_VIEW: u32 = 1 << 5;

/// Ephemeral bridge stand-in objects and hidden annotations produced by OCG lowering.
pub(crate) struct Lowered {
    /// Stand-in objects sorted by object number, unique per number.
    pub(crate) objects: Vec<(Reference, Value)>,
    /// Indirect annotations hidden by `/OC` (so appearance synthesis skips them).
    pub(crate) hidden_annots: HashSet<Reference>,
    /// Next available object number after any objects allocated by lowering.
    pub(crate) next_number: u64,
}

/// Evaluates optional content on `version` and returns any ephemeral stand-in
/// objects needed for `hayro` to match ISO 32000-2 §8.11 and `doc.layers.visible`.
pub(crate) fn lower(version: &Version, pages: &PageIndex, budget: &Budget) -> Result<Lowered> {
    let empty = || Lowered {
        objects: Vec::new(),
        hidden_annots: HashSet::new(),
        next_number: version.next_number(),
    };

    let Some(&Value::Reference(root_ref)) = version.trailer().get("Root") else {
        return Ok(empty());
    };
    let Some(Resolved::Indirect(catalog_ref, catalog_val)) =
        resolve_ref(version, root_ref, budget)?
    else {
        return Ok(empty());
    };
    let Value::Dict(catalog_dict) = catalog_val.as_ref() else {
        return Ok(empty());
    };
    let Some(oc_props_entry) = catalog_dict.get("OCProperties") else {
        return Ok(empty());
    };
    let Some(oc_props_resolved) = resolve_value(version, oc_props_entry, budget)? else {
        return Ok(empty());
    };
    let Value::Dict(oc_props_dict) = oc_props_resolved.value() else {
        return Ok(empty());
    };

    let oc_props_target = match oc_props_resolved.indirect_ref() {
        Some(r) => OcPropsTarget::Indirect(r),
        None => OcPropsTarget::DirectInCatalog(catalog_ref),
    };

    let mut off: HashSet<Reference> = HashSet::new();
    let d_target = read_default_off_state(version, oc_props_dict, &mut off, budget)?;

    let mut lowerer = Lowerer {
        version,
        budget,
        off,
        overrides: HashMap::new(),
        hidden_annots: HashSet::new(),
        lowered_ocmds: HashSet::new(),
        visited_indirect_resources: HashSet::new(),
        visited_direct_resources_owners: HashSet::new(),
        visited_xobjects: HashSet::new(),
        visited_annots: HashSet::new(),
        synth_off: None,
        next_number: version.next_number(),
        steps: 0,
    };

    lowerer.walk_pages(pages)?;

    if let Some(synth_off) = lowerer.synth_off {
        lowerer.install_synth_off(synth_off, oc_props_target, d_target)?;
    }

    let mut objects: BudgetVec<'_, (Reference, Value)> = BudgetVec::new(budget);
    for (_, entry) in lowerer.overrides {
        objects.try_push(entry)?;
    }
    objects.sort_unstable_by_key(|(reference, _)| reference.number());

    Ok(Lowered {
        objects: objects.into_vec(),
        hidden_annots: lowerer.hidden_annots,
        next_number: lowerer.next_number,
    })
}

/// Resolved PDF value together with its indirect reference when indirect.
#[derive(Clone)]
enum Resolved {
    Direct(Value),
    Indirect(Reference, Arc<Value>),
}

impl Resolved {
    fn value(&self) -> &Value {
        match self {
            Self::Direct(v) => v,
            Self::Indirect(_, v) => v.as_ref(),
        }
    }

    const fn indirect_ref(&self) -> Option<Reference> {
        match self {
            Self::Direct(_) => None,
            Self::Indirect(r, _) => Some(*r),
        }
    }
}

/// Where the catalog's `/OCProperties` dictionary is stored.
#[derive(Clone, Copy)]
enum OcPropsTarget {
    DirectInCatalog(Reference),
    Indirect(Reference),
}

/// Where `/OCProperties /D` (and optionally its `/OFF` array) is stored.
#[derive(Clone, Copy)]
enum DTarget {
    Absent,
    Direct {
        off_indirect: Option<Reference>,
    },
    Indirect {
        d_ref: Reference,
        off_indirect: Option<Reference>,
    },
}

/// Where a `/Resources` dictionary is stored so direct `/Properties` edits write back to the right object.
#[derive(Clone, Copy)]
enum ResourceOwner {
    Indirect(Reference),
    DirectInDict(Reference),
    DirectInStream(Reference),
}

/// Where a `/Properties` dictionary is stored.
#[derive(Clone, Copy)]
enum PropsOwner {
    Indirect(Reference),
    DirectInResources(ResourceOwner),
}

/// Where a page's `/Annots` array is stored.
#[derive(Clone, Copy)]
enum AnnotsOwner {
    IndirectArray(Reference),
    DirectInPage(Reference),
}

/// Worklist items for iterative traversal of `/Resources` and nested streams.
enum WorkItem {
    Resources {
        owner: Option<ResourceOwner>,
        dict: Dict,
        depth: usize,
    },
    Stream {
        reference: Reference,
        value: Arc<Value>,
        depth: usize,
    },
}

fn resolve_ref(
    version: &Version,
    reference: Reference,
    budget: &Budget,
) -> Result<Option<Resolved>> {
    let mut current = reference;
    for _ in 0..MAX_REF_CHAIN {
        let obj = match version.get(current.number(), budget) {
            Ok(Some(obj)) => obj,
            Ok(None) => return Ok(None),
            Err(err) if err.limit().is_some() => return Err(err),
            Err(_) => return Ok(None),
        };
        if obj.generation() != current.generation() {
            return Ok(None);
        }
        if let Value::Reference(next) = obj.value().as_ref() {
            current = *next;
            continue;
        }
        return Ok(Some(Resolved::Indirect(current, Arc::clone(obj.value()))));
    }
    Ok(None)
}

fn resolve_value(version: &Version, value: &Value, budget: &Budget) -> Result<Option<Resolved>> {
    match value {
        Value::Reference(r) => resolve_ref(version, *r, budget),
        other => Ok(Some(Resolved::Direct(other.clone()))),
    }
}

/// Reads the inactive OCG set from `/OCProperties /D` matching `doc.layers.visible`
/// and `hayro-interpret`'s `OcgState::from_catalog`.
fn read_default_off_state(
    version: &Version,
    oc_props: &Dict,
    off: &mut HashSet<Reference>,
    budget: &Budget,
) -> Result<DTarget> {
    let Some(d_entry) = oc_props.get("D") else {
        return Ok(DTarget::Absent);
    };
    let Some(d_resolved) = resolve_value(version, d_entry, budget)? else {
        return Ok(DTarget::Absent);
    };
    let Value::Dict(d_dict) = d_resolved.value() else {
        return Ok(DTarget::Absent);
    };

    let base_is_off = d_dict
        .get("BaseState")
        .map(|v| resolve_value(version, v, budget))
        .transpose()?
        .flatten()
        .is_some_and(|r| matches!(r.value(), Value::Name(n) if n.as_bytes() == b"OFF"));

    if base_is_off
        && let Some(ocgs_val) = oc_props.get("OCGs")
        && let Some(ocgs_res) = resolve_value(version, ocgs_val, budget)?
        && let Value::Array(arr) = ocgs_res.value()
    {
        budget.check(LimitCode::OptionalContentCount, arr.len() as u64)?;
        for item in arr.iter() {
            budget.poll()?;
            if let Value::Reference(r) = item
                && let Some(Resolved::Indirect(target, val)) = resolve_ref(version, *r, budget)?
                && matches!(val.as_ref(), Value::Dict(_))
            {
                try_insert_set(off, target, budget)?;
                if *r != target {
                    try_insert_set(off, *r, budget)?;
                }
            }
        }
    }

    for (key, is_on) in [("ON", true), ("OFF", false)] {
        if let Some(arr_val) = d_dict.get(key)
            && let Some(arr_res) = resolve_value(version, arr_val, budget)?
            && let Value::Array(arr) = arr_res.value()
        {
            budget.check(LimitCode::OptionalContentCount, arr.len() as u64)?;
            for item in arr.iter() {
                budget.poll()?;
                if let Value::Reference(r) = item
                    && let Some(Resolved::Indirect(target, val)) = resolve_ref(version, *r, budget)?
                    && matches!(val.as_ref(), Value::Dict(_))
                {
                    if is_on {
                        off.remove(&target);
                        off.remove(r);
                    } else {
                        try_insert_set(off, target, budget)?;
                        if *r != target {
                            try_insert_set(off, *r, budget)?;
                        }
                    }
                }
            }
        }
    }

    let off_indirect = d_dict
        .get("OFF")
        .and_then(|v| match v {
            Value::Reference(r) => Some(*r),
            _ => None,
        })
        .map(|r| resolve_ref(version, r, budget))
        .transpose()?
        .flatten()
        .and_then(|res| match res {
            Resolved::Indirect(target, val) if matches!(val.as_ref(), Value::Array(_)) => {
                Some(target)
            }
            _ => None,
        });

    Ok(match d_resolved.indirect_ref() {
        Some(d_ref) => DTarget::Indirect {
            d_ref,
            off_indirect,
        },
        None => DTarget::Direct { off_indirect },
    })
}

/// Returns whether `dict` is an Optional Content Membership Dictionary (`/OCMD`).
fn is_ocmd(version: &Version, dict: &Dict, budget: &Budget) -> Result<bool> {
    match dict
        .get("Type")
        .map(|v| resolve_value(version, v, budget))
        .transpose()?
        .flatten()
        .as_ref()
        .map(Resolved::value)
    {
        Some(Value::Name(name)) => Ok(name.as_bytes() == b"OCMD"),
        None => Ok(dict.get("VE").is_some() || dict.get("OCGs").is_some()),
        _ => Ok(false),
    }
}

/// Evaluates whether `value` (an OCG or OCMD) is visible under `off`.
fn evaluate_oc(
    version: &Version,
    value: &Value,
    off: &HashSet<Reference>,
    budget: &Budget,
) -> Result<bool> {
    let Some(resolved) = resolve_value(version, value, budget)? else {
        return Ok(true);
    };
    let Value::Dict(dict) = resolved.value() else {
        return Ok(true);
    };
    if is_ocmd(version, dict, budget)? {
        return evaluate_ocmd(version, dict, off, budget);
    }
    if let Some(ty) = dict
        .get("Type")
        .map(|v| resolve_value(version, v, budget))
        .transpose()?
        .flatten()
        && !matches!(ty.value(), Value::Name(n) if n.as_bytes() == b"OCG")
    {
        return Ok(true);
    }
    Ok(resolved.indirect_ref().is_none_or(|r| !off.contains(&r)))
}

/// Evaluates an OCMD dictionary: `/VE` takes precedence over `/OCGs` and `/P`.
fn evaluate_ocmd(
    version: &Version,
    dict: &Dict,
    off: &HashSet<Reference>,
    budget: &Budget,
) -> Result<bool> {
    if let Some(ve_val) = dict.get("VE")
        && let Some(ve_res) = resolve_value(version, ve_val, budget)?
        && let Value::Array(ve_arr) = ve_res.value()
    {
        return evaluate_ve(version, ve_arr, off, budget);
    }
    let mut states: BudgetVec<'_, bool> = BudgetVec::new(budget);
    if let Some(ocgs_val) = dict.get("OCGs")
        && let Some(ocgs_res) = resolve_value(version, ocgs_val, budget)?
    {
        match ocgs_res.value() {
            Value::Dict(_) => {
                states.try_push(ocgs_res.indirect_ref().is_none_or(|r| !off.contains(&r)))?;
            }
            Value::Array(arr) => {
                budget.check(LimitCode::OptionalContentCount, arr.len() as u64)?;
                for item in arr.iter() {
                    budget.poll()?;
                    if let Some(item_res) = resolve_value(version, item, budget)?
                        && matches!(item_res.value(), Value::Dict(_))
                    {
                        states
                            .try_push(item_res.indirect_ref().is_none_or(|r| !off.contains(&r)))?;
                    }
                }
            }
            _ => {}
        }
    }
    if states.is_empty() {
        return Ok(true);
    }
    let policy = dict
        .get("P")
        .map(|v| resolve_value(version, v, budget))
        .transpose()?
        .flatten();
    let policy_bytes = policy.as_ref().and_then(|res| match res.value() {
        Value::Name(name) => Some(name.as_bytes()),
        _ => None,
    });
    Ok(match policy_bytes {
        Some(b"AllOn") => states.iter().all(|&s| s),
        Some(b"AnyOff") => states.iter().any(|&s| !s),
        Some(b"AllOff") => states.iter().all(|&s| !s),
        _ => states.iter().any(|&s| s),
    })
}

#[derive(Clone, Copy)]
enum VeOp {
    And,
    Or,
    Not,
}

struct VeFrame {
    array: Array,
    op: VeOp,
    at: usize,
    values_start: usize,
}

/// Evaluates a `/VE` visibility expression iteratively with an explicit stack
/// (ISO 32000-2 §8.11.2.2), matching `doc.layers.visible`.
fn evaluate_ve(
    version: &Version,
    root: &Array,
    off: &HashSet<Reference>,
    budget: &Budget,
) -> Result<bool> {
    let mut stack: BudgetVec<'_, VeFrame> = BudgetVec::new(budget);
    let mut values: BudgetVec<'_, bool> = BudgetVec::new(budget);
    let mut operands = 0u64;

    let make_frame = |array: &Array, depth: usize, values_start: usize| -> Result<VeFrame> {
        if depth == MAX_EXPRESSION_DEPTH {
            return Err(Error::from(LimitExceeded::over(
                LimitCode::OptionalContentExpression,
                MAX_EXPRESSION_DEPTH as u64,
                (MAX_EXPRESSION_DEPTH + 1) as u64,
            ))
            .context("a visibility expression is deeper than 32 levels"));
        }
        let op_res = array
            .get(0)
            .map(|v| resolve_value(version, v, budget))
            .transpose()?
            .flatten();
        let op = match op_res.as_ref().map(Resolved::value) {
            Some(Value::Name(name)) if name.as_bytes() == b"Not" => VeOp::Not,
            Some(Value::Name(name)) if name.as_bytes() == b"Or" => VeOp::Or,
            _ => VeOp::And,
        };
        Ok(VeFrame {
            array: array.clone(),
            op,
            at: 1,
            values_start,
        })
    };

    stack.try_push(make_frame(root, 0, 0)?)?;

    while let Some(frame) = stack.last_mut() {
        if frame.at < frame.array.len() {
            operands += 1;
            if operands.is_multiple_of(POLL_INTERVAL as u64) {
                budget.poll()?;
            }
            budget.check(LimitCode::OptionalContentExpression, operands)?;
            if operands > MAX_EXPRESSION_OPERANDS {
                return Err(Error::from(LimitExceeded::over(
                    LimitCode::OptionalContentExpression,
                    MAX_EXPRESSION_OPERANDS,
                    operands,
                ))
                .context("a visibility expression has more than 4096 operands"));
            }
            let operand_val = frame.array.get(frame.at).cloned();
            frame.at += 1;
            if let Some(raw) = operand_val
                && let Some(resolved) = resolve_value(version, &raw, budget)?
            {
                match resolved.value() {
                    Value::Array(sub) => {
                        let depth = stack.len();
                        let start = values.len();
                        let next = make_frame(sub, depth, start)?;
                        stack.try_push(next)?;
                    }
                    Value::Dict(_) => {
                        values
                            .try_push(resolved.indirect_ref().is_none_or(|r| !off.contains(&r)))?;
                    }
                    _ => {}
                }
            }
            continue;
        }
        let Some(done) = stack.pop() else {
            break;
        };
        let slice = &values.as_slice()[done.values_start..];
        let result = match done.op {
            VeOp::Not => slice.first().is_none_or(|&v| !v),
            VeOp::Or => slice.is_empty() || slice.iter().any(|&v| v),
            VeOp::And => slice.iter().all(|&v| v),
        };
        values.truncate(done.values_start);
        if stack.is_empty() {
            return Ok(result);
        }
        values.try_push(result)?;
    }

    Ok(true)
}

/// Returns whether an OCMD dictionary uses features that pinned `hayro-interpret`
/// cannot evaluate natively (`/VE`, missing `/Type /OCMD`, or non-dict/missing
/// `/OCGs` references).
fn needs_ocmd_lowering(version: &Version, dict: &Dict, budget: &Budget) -> Result<bool> {
    if let Some(ve_val) = dict.get("VE")
        && let Some(ve_res) = resolve_value(version, ve_val, budget)?
        && matches!(ve_res.value(), Value::Array(_))
    {
        return Ok(true);
    }
    let has_ocmd_type = dict
        .get("Type")
        .map(|v| resolve_value(version, v, budget))
        .transpose()?
        .flatten()
        .is_some_and(|r| matches!(r.value(), Value::Name(n) if n.as_bytes() == b"OCMD"));
    if !has_ocmd_type {
        return Ok(true);
    }
    let Some(ocgs_val) = dict.get("OCGs") else {
        return Ok(false);
    };
    match ocgs_val {
        Value::Reference(r) => {
            let Some(res) = resolve_ref(version, *r, budget)? else {
                return Ok(true);
            };
            match res.value() {
                Value::Dict(_) => Ok(res.indirect_ref() != Some(*r)),
                Value::Array(arr) => array_needs_ocmd_lowering(version, arr, budget),
                _ => Ok(true),
            }
        }
        Value::Array(arr) => array_needs_ocmd_lowering(version, arr, budget),
        _ => Ok(true),
    }
}

fn array_needs_ocmd_lowering(version: &Version, arr: &Array, budget: &Budget) -> Result<bool> {
    budget.check(LimitCode::OptionalContentCount, arr.len() as u64)?;
    for item in arr.iter() {
        budget.poll()?;
        let Value::Reference(r) = item else {
            return Ok(true);
        };
        let Some(res) = resolve_ref(version, *r, budget)? else {
            return Ok(true);
        };
        if !matches!(res.value(), Value::Dict(_)) || res.indirect_ref() != Some(*r) {
            return Ok(true);
        }
    }
    Ok(false)
}

fn has_non_ocmd_keys(dict: &Dict) -> bool {
    dict.keys()
        .any(|key| !matches!(key.as_bytes(), b"Type" | b"OCGs" | b"P" | b"VE"))
}

struct Lowerer<'a> {
    version: &'a Version,
    budget: &'a Budget,
    off: HashSet<Reference>,
    overrides: HashMap<u32, (Reference, Value)>,
    hidden_annots: HashSet<Reference>,
    lowered_ocmds: HashSet<u32>,
    visited_indirect_resources: HashSet<u32>,
    visited_direct_resources_owners: HashSet<u32>,
    visited_xobjects: HashSet<u32>,
    visited_annots: HashSet<u32>,
    synth_off: Option<Reference>,
    next_number: u64,
    steps: usize,
}

impl Lowerer<'_> {
    fn tick(&mut self) -> Result<()> {
        self.steps = self.steps.wrapping_add(1);
        if self.steps.is_multiple_of(POLL_INTERVAL) {
            self.budget.poll()?;
        }
        Ok(())
    }

    fn alloc_ref(&mut self) -> Result<Reference> {
        let number = u32::try_from(self.next_number)
            .ok()
            .filter(|&n| n < u32::MAX)
            .ok_or_else(|| {
                Error::malformed("no object number remains for optional content bridge lowering")
            })?;
        self.next_number += 1;
        Ok(Reference::new(number, 0))
    }

    fn ensure_synth_off(&mut self) -> Result<Reference> {
        if let Some(reference) = self.synth_off {
            return Ok(reference);
        }
        let reference = self.alloc_ref()?;
        self.synth_off = Some(reference);
        Ok(reference)
    }

    fn get_or_clone_override(&mut self, target: Reference) -> Result<Option<&mut Value>> {
        if !self.overrides.contains_key(&target.number()) {
            let Some(obj) = self.version.get(target.number(), self.budget)? else {
                return Ok(None);
            };
            if obj.generation() != target.generation() {
                return Ok(None);
            }
            let cloned = copy_for_edit(obj.value(), self.budget)?;
            try_insert_map(
                &mut self.overrides,
                target.number(),
                (target, cloned),
                self.budget,
            )?;
        }
        Ok(self.overrides.get_mut(&target.number()).map(|(_, v)| v))
    }

    fn build_lowered_ocmd(&mut self, dict: &Dict, visible: bool) -> Result<Dict> {
        let Value::Dict(mut lowered) = copy_for_edit(&Value::Dict(dict.clone()), self.budget)?
        else {
            unreachable!("copied dict remains a dict");
        };
        lowered.remove("VE");
        insert_fixed_key(&mut lowered, "Type", Value::Name(Name::from("OCMD")));
        if visible {
            insert_fixed_key(&mut lowered, "OCGs", Value::Array(Array::new()));
            lowered.remove("P");
        } else {
            let synth = self.ensure_synth_off()?;
            let mut items: BudgetVec<'_, Value> = BudgetVec::new(self.budget);
            items.try_push(Value::Reference(synth))?;
            insert_fixed_key(
                &mut lowered,
                "OCGs",
                Value::Array(Array::from(items.into_vec())),
            );
            insert_fixed_key(&mut lowered, "P", Value::Name(Name::from("AllOn")));
        }
        Ok(lowered)
    }

    fn lower_indirect_ocmd(&mut self, target: Reference, dict: &Dict) -> Result<()> {
        if !try_insert_set(&mut self.lowered_ocmds, target.number(), self.budget)? {
            return Ok(());
        }
        if !needs_ocmd_lowering(self.version, dict, self.budget)? {
            return Ok(());
        }
        let visible = evaluate_ocmd(self.version, dict, &self.off, self.budget)?;
        let lowered = self.build_lowered_ocmd(dict, visible)?;
        try_insert_map(
            &mut self.overrides,
            target.number(),
            (target, Value::Dict(lowered)),
            self.budget,
        )?;
        Ok(())
    }

    fn walk_pages(&mut self, pages: &PageIndex) -> Result<()> {
        let mut work: BudgetVec<'_, WorkItem> = BudgetVec::new(self.budget);

        for page in pages.pages() {
            self.tick()?;
            if let Some((owner, dict)) = self.find_page_resources(page)? {
                work.try_push(WorkItem::Resources {
                    owner,
                    dict,
                    depth: 0,
                })?;
            }
            self.process_page_annots(page, &mut work)?;
        }

        while let Some(item) = work.pop() {
            self.tick()?;
            match item {
                WorkItem::Resources { owner, dict, depth } => {
                    self.process_resources(owner, &dict, depth, &mut work)?;
                }
                WorkItem::Stream {
                    reference,
                    value,
                    depth,
                } => {
                    self.process_stream(reference, &value, depth, &mut work)?;
                }
            }
        }

        Ok(())
    }

    fn find_page_resources(&self, page: &Page) -> Result<Option<(Option<ResourceOwner>, Dict)>> {
        if let Some(res_val) = page.dict().get("Resources") {
            return self
                .resolve_resources_entry(res_val, page.object().map(ResourceOwner::DirectInDict));
        }
        // Walk /Parent chain to locate the owning ancestor object of inherited /Resources.
        let mut seen: HashSet<u32> = HashSet::new();
        let mut next = parent_ref(page.dict());
        let mut hops = 0usize;
        while let Some(parent) = next.take() {
            hops += 1;
            if hops > MAX_PARENT_CHAIN || !try_insert_set(&mut seen, parent.number(), self.budget)?
            {
                break;
            }
            let Some(Resolved::Indirect(node_ref, node_val)) =
                resolve_ref(self.version, parent, self.budget)?
            else {
                break;
            };
            let Value::Dict(node_dict) = node_val.as_ref() else {
                break;
            };
            if let Some(res_val) = node_dict.get("Resources") {
                return self
                    .resolve_resources_entry(res_val, Some(ResourceOwner::DirectInDict(node_ref)));
            }
            next = parent_ref(node_dict);
        }
        Ok(None)
    }

    fn resolve_resources_entry(
        &self,
        entry: &Value,
        direct_owner: Option<ResourceOwner>,
    ) -> Result<Option<(Option<ResourceOwner>, Dict)>> {
        let Some(resolved) = resolve_value(self.version, entry, self.budget)? else {
            return Ok(None);
        };
        let Value::Dict(dict) = resolved.value() else {
            return Ok(None);
        };
        let owner = match resolved.indirect_ref() {
            Some(res_ref) => Some(ResourceOwner::Indirect(res_ref)),
            None => direct_owner,
        };
        Ok(Some((owner, dict.clone())))
    }

    fn process_page_annots(
        &mut self,
        page: &Page,
        work: &mut BudgetVec<'_, WorkItem>,
    ) -> Result<()> {
        let Some(annots_entry) = page.dict().get("Annots") else {
            return Ok(());
        };
        let Some(annots_res) = resolve_value(self.version, annots_entry, self.budget)? else {
            return Ok(());
        };
        let Value::Array(annots_arr) = annots_res.value() else {
            return Ok(());
        };
        let annots_owner = match annots_res.indirect_ref() {
            Some(r) => Some(AnnotsOwner::IndirectArray(r)),
            None => page.object().map(AnnotsOwner::DirectInPage),
        };

        for (index, item) in annots_arr.iter().enumerate() {
            self.tick()?;
            let Some(annot_res) = resolve_value(self.version, item, self.budget)? else {
                continue;
            };
            let Value::Dict(annot_dict) = annot_res.value() else {
                continue;
            };
            if let Some(annot_ref) = annot_res.indirect_ref() {
                if !try_insert_set(&mut self.visited_annots, annot_ref.number(), self.budget)? {
                    continue;
                }
                let hidden = match annot_dict.get("OC") {
                    Some(oc_val) => !evaluate_oc(self.version, oc_val, &self.off, self.budget)?,
                    None => false,
                };
                if hidden {
                    try_insert_set(&mut self.hidden_annots, annot_ref, self.budget)?;
                    let flags = read_flags(self.version, annot_dict, self.budget)?;
                    if flags & (ANNOTATION_HIDDEN | ANNOTATION_NO_VIEW) == 0
                        && let Some(Value::Dict(dict)) = self.get_or_clone_override(annot_ref)?
                    {
                        insert_fixed_key(
                            dict,
                            "F",
                            Value::Integer(i64::from(flags | ANNOTATION_HIDDEN)),
                        );
                    }
                    continue;
                }
            } else {
                let hidden = match annot_dict.get("OC") {
                    Some(oc_val) => !evaluate_oc(self.version, oc_val, &self.off, self.budget)?,
                    None => false,
                };
                if hidden {
                    let flags = read_flags(self.version, annot_dict, self.budget)?;
                    if flags & (ANNOTATION_HIDDEN | ANNOTATION_NO_VIEW) == 0
                        && let Some(owner) = annots_owner
                    {
                        self.hide_direct_annotation(owner, index, flags | ANNOTATION_HIDDEN)?;
                    }
                    continue;
                }
            }

            // Visible annotation: walk its /AP /N appearance stream(s) for nested OCMDs.
            let flags = read_flags(self.version, annot_dict, self.budget)?;
            if flags & (ANNOTATION_HIDDEN | ANNOTATION_NO_VIEW) != 0 {
                continue;
            }
            self.enqueue_annotation_appearances(annot_dict, work)?;
        }

        Ok(())
    }

    fn hide_direct_annotation(
        &mut self,
        owner: AnnotsOwner,
        index: usize,
        new_flags: u32,
    ) -> Result<()> {
        match owner {
            AnnotsOwner::IndirectArray(annots_ref) => {
                if let Some(Value::Array(arr)) = self.get_or_clone_override(annots_ref)?
                    && let Some(Value::Dict(annot_dict)) = arr.get_mut(index)
                {
                    insert_fixed_key(annot_dict, "F", Value::Integer(i64::from(new_flags)));
                }
            }
            AnnotsOwner::DirectInPage(page_ref) => {
                if let Some(Value::Dict(page_dict)) = self.get_or_clone_override(page_ref)?
                    && let Some(Value::Array(arr)) = page_dict.get_mut("Annots")
                    && let Some(Value::Dict(annot_dict)) = arr.get_mut(index)
                {
                    insert_fixed_key(annot_dict, "F", Value::Integer(i64::from(new_flags)));
                }
            }
        }
        Ok(())
    }

    fn enqueue_annotation_appearances(
        &mut self,
        annot_dict: &Dict,
        work: &mut BudgetVec<'_, WorkItem>,
    ) -> Result<()> {
        let Some(ap_val) = annot_dict.get("AP") else {
            return Ok(());
        };
        let Some(ap_res) = resolve_value(self.version, ap_val, self.budget)? else {
            return Ok(());
        };
        let Value::Dict(ap_dict) = ap_res.value() else {
            return Ok(());
        };
        let Some(n_val) = ap_dict.get("N") else {
            return Ok(());
        };
        let Some(n_res) = resolve_value(self.version, n_val, self.budget)? else {
            return Ok(());
        };
        match n_res {
            Resolved::Indirect(r, val) if matches!(val.as_ref(), Value::Stream(_)) => {
                work.try_push(WorkItem::Stream {
                    reference: r,
                    value: val,
                    depth: 1,
                })?;
            }
            Resolved::Direct(Value::Dict(states)) => {
                self.enqueue_state_streams(&states, work)?;
            }
            Resolved::Indirect(_, val) => {
                if let Value::Dict(states) = val.as_ref() {
                    self.enqueue_state_streams(states, work)?;
                }
            }
            _ => {}
        }
        Ok(())
    }

    fn enqueue_state_streams(
        &mut self,
        states: &Dict,
        work: &mut BudgetVec<'_, WorkItem>,
    ) -> Result<()> {
        for (_, state_val) in states.iter() {
            self.tick()?;
            if let Some(Resolved::Indirect(r, val)) =
                resolve_value(self.version, state_val, self.budget)?
                && matches!(val.as_ref(), Value::Stream(_))
            {
                work.try_push(WorkItem::Stream {
                    reference: r,
                    value: val,
                    depth: 1,
                })?;
            }
        }
        Ok(())
    }

    fn process_resources(
        &mut self,
        owner: Option<ResourceOwner>,
        res_dict: &Dict,
        depth: usize,
        work: &mut BudgetVec<'_, WorkItem>,
    ) -> Result<()> {
        match owner {
            Some(ResourceOwner::Indirect(r))
                if !try_insert_set(
                    &mut self.visited_indirect_resources,
                    r.number(),
                    self.budget,
                )? =>
            {
                return Ok(());
            }
            Some(ResourceOwner::DirectInDict(r) | ResourceOwner::DirectInStream(r))
                if !try_insert_set(
                    &mut self.visited_direct_resources_owners,
                    r.number(),
                    self.budget,
                )? =>
            {
                return Ok(());
            }
            _ => {}
        }

        // 1. /Properties: lower indirect OCMDs with /VE (or invalid /OCGs) and direct OCMDs.
        if let Some(props_entry) = res_dict.get("Properties")
            && let Some(props_res) = resolve_value(self.version, props_entry, self.budget)?
            && let Value::Dict(props_dict) = props_res.value()
        {
            let props_owner = match props_res.indirect_ref() {
                Some(r) => Some(PropsOwner::Indirect(r)),
                None => owner.map(PropsOwner::DirectInResources),
            };
            self.process_properties(props_owner, props_dict)?;
        }

        // 2. /XObject: check XObject /OC and enqueue Form XObjects.
        if let Some(xobj_entry) = res_dict.get("XObject")
            && let Some(xobj_res) = resolve_value(self.version, xobj_entry, self.budget)?
            && let Value::Dict(xobj_dict) = xobj_res.value()
        {
            for (_, val) in xobj_dict.iter() {
                self.tick()?;
                if let Some(Resolved::Indirect(r, stream_val)) =
                    resolve_value(self.version, val, self.budget)?
                    && matches!(stream_val.as_ref(), Value::Stream(_))
                {
                    work.try_push(WorkItem::Stream {
                        reference: r,
                        value: stream_val,
                        depth: depth.saturating_add(1),
                    })?;
                }
            }
        }

        if depth >= MAX_STREAM_DEPTH {
            return Ok(());
        }

        // 3. /ExtGState -> /SMask -> /G (SoftMask transparency group Form XObject).
        if let Some(gs_entry) = res_dict.get("ExtGState")
            && let Some(gs_res) = resolve_value(self.version, gs_entry, self.budget)?
            && let Value::Dict(gs_dict) = gs_res.value()
        {
            for (_, val) in gs_dict.iter() {
                self.tick()?;
                if let Some(gs_item) = resolve_value(self.version, val, self.budget)?
                    && let Value::Dict(item_dict) = gs_item.value()
                    && let Some(smask_val) = item_dict.get("SMask")
                    && let Some(smask_res) = resolve_value(self.version, smask_val, self.budget)?
                    && let Value::Dict(smask_dict) = smask_res.value()
                    && let Some(g_val) = smask_dict.get("G")
                    && let Some(Resolved::Indirect(r, stream_val)) =
                        resolve_value(self.version, g_val, self.budget)?
                    && matches!(stream_val.as_ref(), Value::Stream(_))
                {
                    work.try_push(WorkItem::Stream {
                        reference: r,
                        value: stream_val,
                        depth: depth.saturating_add(1),
                    })?;
                }
            }
        }

        // 4. /Pattern: Type 1 tiling pattern streams.
        if let Some(pat_entry) = res_dict.get("Pattern")
            && let Some(pat_res) = resolve_value(self.version, pat_entry, self.budget)?
            && let Value::Dict(pat_dict) = pat_res.value()
        {
            for (_, val) in pat_dict.iter() {
                self.tick()?;
                if let Some(Resolved::Indirect(r, stream_val)) =
                    resolve_value(self.version, val, self.budget)?
                    && matches!(stream_val.as_ref(), Value::Stream(_))
                {
                    work.try_push(WorkItem::Stream {
                        reference: r,
                        value: stream_val,
                        depth: depth.saturating_add(1),
                    })?;
                }
            }
        }

        // 5. /Font: Type 3 font /Resources.
        if let Some(font_entry) = res_dict.get("Font")
            && let Some(font_res) = resolve_value(self.version, font_entry, self.budget)?
            && let Value::Dict(font_dict) = font_res.value()
        {
            for (_, val) in font_dict.iter() {
                self.tick()?;
                if let Some(font_item) = resolve_value(self.version, val, self.budget)?
                    && let Value::Dict(f_dict) = font_item.value()
                    && f_dict
                        .get("Subtype")
                        .is_some_and(|s| matches!(s, Value::Name(n) if n.as_bytes() == b"Type3"))
                    && let Some(t3_res_val) = f_dict.get("Resources")
                    && let Some((t3_owner, t3_dict)) = self.resolve_resources_entry(
                        t3_res_val,
                        font_item.indirect_ref().map(ResourceOwner::DirectInDict),
                    )?
                {
                    work.try_push(WorkItem::Resources {
                        owner: t3_owner,
                        dict: t3_dict,
                        depth: depth.saturating_add(1),
                    })?;
                }
            }
        }

        Ok(())
    }

    fn process_properties(
        &mut self,
        props_owner: Option<PropsOwner>,
        props_dict: &Dict,
    ) -> Result<()> {
        for (prop_name, prop_val) in props_dict.iter() {
            self.tick()?;
            match prop_val {
                Value::Reference(r) => {
                    let Some(Resolved::Indirect(target, val)) =
                        resolve_ref(self.version, *r, self.budget)?
                    else {
                        continue;
                    };
                    let Value::Dict(dict) = val.as_ref() else {
                        continue;
                    };
                    if is_ocmd(self.version, dict, self.budget)? {
                        self.lower_indirect_ocmd(target, dict)?;
                    }
                }
                Value::Dict(dict) => {
                    if !is_ocmd(self.version, dict, self.budget)? {
                        continue;
                    }
                    // Always evaluate so over-deep / over-wide /VE expressions refuse cleanly.
                    let visible = evaluate_ocmd(self.version, dict, &self.off, self.budget)?;
                    if !visible && let Some(owner) = props_owner {
                        let replacement = if has_non_ocmd_keys(dict) {
                            let ocmd_ref = self.alloc_ref()?;
                            let lowered = self.build_lowered_ocmd(dict, false)?;
                            try_insert_map(
                                &mut self.overrides,
                                ocmd_ref.number(),
                                (ocmd_ref, Value::Dict(lowered)),
                                self.budget,
                            )?;
                            ocmd_ref
                        } else {
                            self.ensure_synth_off()?
                        };
                        self.replace_property_entry(owner, prop_name.clone(), replacement)?;
                    }
                }
                _ => {}
            }
        }
        Ok(())
    }

    fn replace_property_entry(
        &mut self,
        owner: PropsOwner,
        key: Name,
        replacement: Reference,
    ) -> Result<()> {
        match owner {
            PropsOwner::Indirect(props_ref) => {
                if let Some(Value::Dict(props)) = self.get_or_clone_override(props_ref)? {
                    replace_existing_key(props, key, Value::Reference(replacement));
                }
            }
            PropsOwner::DirectInResources(ResourceOwner::Indirect(res_ref)) => {
                if let Some(Value::Dict(res)) = self.get_or_clone_override(res_ref)?
                    && let Some(Value::Dict(props)) = res.get_mut("Properties")
                {
                    replace_existing_key(props, key, Value::Reference(replacement));
                }
            }
            PropsOwner::DirectInResources(ResourceOwner::DirectInDict(dict_ref)) => {
                if let Some(Value::Dict(dict)) = self.get_or_clone_override(dict_ref)?
                    && let Some(Value::Dict(res)) = dict.get_mut("Resources")
                    && let Some(Value::Dict(props)) = res.get_mut("Properties")
                {
                    replace_existing_key(props, key, Value::Reference(replacement));
                }
            }
            PropsOwner::DirectInResources(ResourceOwner::DirectInStream(stream_ref)) => {
                if let Some(Value::Stream(stream)) = self.get_or_clone_override(stream_ref)?
                    && let Some(Value::Dict(res)) = stream.dict_mut().get_mut("Resources")
                    && let Some(Value::Dict(props)) = res.get_mut("Properties")
                {
                    replace_existing_key(props, key, Value::Reference(replacement));
                }
            }
        }
        Ok(())
    }

    fn process_stream(
        &mut self,
        reference: Reference,
        value: &Arc<Value>,
        depth: usize,
        work: &mut BudgetVec<'_, WorkItem>,
    ) -> Result<()> {
        if !try_insert_set(&mut self.visited_xobjects, reference.number(), self.budget)? {
            return Ok(());
        }
        let Value::Stream(stream) = value.as_ref() else {
            return Ok(());
        };

        // Check /OC on Form or Image XObjects.
        if let Some(oc_val) = stream.dict().get("OC") {
            match oc_val {
                Value::Reference(r) => {
                    if let Some(Resolved::Indirect(target, val)) =
                        resolve_ref(self.version, *r, self.budget)?
                        && let Value::Dict(dict) = val.as_ref()
                        && is_ocmd(self.version, dict, self.budget)?
                    {
                        self.lower_indirect_ocmd(target, dict)?;
                    }
                }
                Value::Dict(dict)
                    if is_ocmd(self.version, dict, self.budget)?
                        && needs_ocmd_lowering(self.version, dict, self.budget)? =>
                {
                    let visible = evaluate_ocmd(self.version, dict, &self.off, self.budget)?;
                    let lowered = self.build_lowered_ocmd(dict, visible)?;
                    if let Some(Value::Stream(overridden)) =
                        self.get_or_clone_override(reference)?
                    {
                        insert_fixed_key(overridden.dict_mut(), "OC", Value::Dict(lowered));
                    }
                }
                _ => {}
            }
        }

        if depth >= MAX_STREAM_DEPTH {
            return Ok(());
        }

        // Walk /Resources on Form XObjects and Type 1 Tiling Patterns.
        let is_form = stream
            .dict()
            .get("Subtype")
            .is_some_and(|v| matches!(v, Value::Name(n) if n.as_bytes() == b"Form"));
        let is_tiling = stream
            .dict()
            .get("PatternType")
            .is_some_and(|v| matches!(v, Value::Integer(1)));
        if (is_form || is_tiling)
            && let Some(res_val) = stream.dict().get("Resources")
            && let Some((owner, dict)) = self
                .resolve_resources_entry(res_val, Some(ResourceOwner::DirectInStream(reference)))?
        {
            work.try_push(WorkItem::Resources { owner, dict, depth })?;
        }

        Ok(())
    }

    /// Registers `synth_off` as a synthetic OCG object and appends it to the
    /// bridged catalog's `/OCProperties /D /OFF` array.
    fn install_synth_off(
        &mut self,
        synth_off: Reference,
        oc_props_target: OcPropsTarget,
        d_target: DTarget,
    ) -> Result<()> {
        let mut synth_dict = Dict::new();
        insert_fixed_key(&mut synth_dict, "Type", Value::Name(Name::from("OCG")));
        insert_fixed_key(
            &mut synth_dict,
            "Name",
            Value::String(PdfString::literal(b"pyropdf.oc.off".as_slice())),
        );
        try_insert_map(
            &mut self.overrides,
            synth_off.number(),
            (synth_off, Value::Dict(synth_dict)),
            self.budget,
        )?;

        match d_target {
            DTarget::Direct {
                off_indirect: Some(off_ref),
            }
            | DTarget::Indirect {
                off_indirect: Some(off_ref),
                ..
            } => {
                let budget = self.budget;
                if let Some(Value::Array(arr)) = self.get_or_clone_override(off_ref)? {
                    *arr = append_ref_to_array(arr, synth_off, budget)?;
                }
            }
            DTarget::Indirect {
                d_ref,
                off_indirect: None,
            } => {
                let new_off = self.build_off_array_for_dict(d_ref, synth_off)?;
                if let Some(Value::Dict(d_dict)) = self.get_or_clone_override(d_ref)? {
                    insert_fixed_key(d_dict, "OFF", Value::Array(new_off));
                }
            }
            DTarget::Direct { off_indirect: None } | DTarget::Absent => match oc_props_target {
                OcPropsTarget::Indirect(props_ref) => {
                    let budget = self.budget;
                    if let Some(Value::Dict(props)) = self.get_or_clone_override(props_ref)? {
                        patch_d_off_in_oc_props(props, synth_off, budget)?;
                    }
                }
                OcPropsTarget::DirectInCatalog(catalog_ref) => {
                    let budget = self.budget;
                    if let Some(Value::Dict(catalog)) = self.get_or_clone_override(catalog_ref)?
                        && let Some(Value::Dict(props)) = catalog.get_mut("OCProperties")
                    {
                        patch_d_off_in_oc_props(props, synth_off, budget)?;
                    }
                }
            },
        }
        Ok(())
    }

    fn build_off_array_for_dict(&self, d_ref: Reference, synth_off: Reference) -> Result<Array> {
        let existing = match self.version.get(d_ref.number(), self.budget)? {
            Some(obj) if obj.generation() == d_ref.generation() => match obj.value().as_ref() {
                Value::Dict(dict) => dict
                    .get("OFF")
                    .map(|v| resolve_value(self.version, v, self.budget))
                    .transpose()?
                    .flatten()
                    .and_then(|res| match res.value() {
                        Value::Array(arr) => Some(arr.clone()),
                        _ => None,
                    }),
                _ => None,
            },
            _ => None,
        };
        match existing {
            Some(arr) => append_ref_to_array(&arr, synth_off, self.budget),
            None => single_ref_array(synth_off, self.budget),
        }
    }
}

fn patch_d_off_in_oc_props(props: &mut Dict, synth_off: Reference, budget: &Budget) -> Result<()> {
    if let Some(Value::Dict(d_dict)) = props.get_mut("D") {
        let new_off = match d_dict.get("OFF") {
            Some(Value::Array(arr)) => append_ref_to_array(arr, synth_off, budget)?,
            _ => single_ref_array(synth_off, budget)?,
        };
        insert_fixed_key(d_dict, "OFF", Value::Array(new_off));
    } else {
        let mut d_dict = Dict::new();
        insert_fixed_key(
            &mut d_dict,
            "OFF",
            Value::Array(single_ref_array(synth_off, budget)?),
        );
        insert_fixed_key(props, "D", Value::Dict(d_dict));
    }
    Ok(())
}

fn single_ref_array(reference: Reference, budget: &Budget) -> Result<Array> {
    let mut items: BudgetVec<'_, Value> = BudgetVec::new(budget);
    items.try_push(Value::Reference(reference))?;
    Ok(Array::from(items.into_vec()))
}

fn append_ref_to_array(existing: &Array, reference: Reference, budget: &Budget) -> Result<Array> {
    let mut items: BudgetVec<'_, Value> = BudgetVec::new(budget);
    for item in existing.iter() {
        items.try_push(copy_for_edit(item, budget)?)?;
    }
    items.try_push(Value::Reference(reference))?;
    Ok(Array::from(items.into_vec()))
}

fn read_flags(version: &Version, annot_dict: &Dict, budget: &Budget) -> Result<u32> {
    let Some(f_val) = annot_dict.get("F") else {
        return Ok(0);
    };
    let Some(resolved) = resolve_value(version, f_val, budget)? else {
        return Ok(0);
    };
    match resolved.value() {
        Value::Integer(n) if *n >= 0 => Ok(u32::try_from(*n).unwrap_or(0)),
        _ => Ok(0),
    }
}

fn parent_ref(dict: &Dict) -> Option<Reference> {
    match dict.get("Parent") {
        Some(Value::Reference(r)) => Some(*r),
        _ => None,
    }
}

fn insert_fixed_key(dict: &mut Dict, key: &'static str, value: Value) {
    #[expect(
        clippy::disallowed_methods,
        reason = "inserts a single constant PDF dictionary key"
    )]
    dict.insert(Name::from(key), value);
}

fn replace_existing_key(dict: &mut Dict, key: Name, value: Value) {
    #[expect(
        clippy::disallowed_methods,
        reason = "replaces an existing key already read from the dictionary; map size does not grow"
    )]
    dict.insert(key, value);
}
