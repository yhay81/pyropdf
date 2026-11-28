//! Automatic 7-bit weight range reduction for `.rten` v2 INT8 models loaded on
//! x86_64 CPUs whose `rten-gemm` INT8 kernel uses saturating `vpmaddubsw`.
//!
//! On x86_64 CPUs without AVX-512 VNNI (such as AVX2-only AMD Zen 1–3 and
//! Intel Haswell–Comet Lake, or AVX-512 CPUs without VNNI), `rten-gemm` computes
//! `u8 * i8 -> i32` dot products using `_mm256_maddubs_epi16` /
//! `_mm512_maddubs_epi16`, whose pairwise `u8 * i8 + u8 * i8` sums saturate at
//! signed 16-bit bounds `[-32768, 32767]`. When a dynamically quantized model
//! stores full 8-bit `u8` weights (`[0, 255]`, i.e. `reduce_range=False`, as in
//! `pyropdf-layout-models==0.1.0`), pairwise products can reach `±65,280` and
//! saturate across `ConvInteger` and `MatMulInteger` layers, collapsing object
//! detection logits to zero detections.
//!
//! Before passing the model bytes to `rten::Model::load`, this module checks
//! whether the active CPU uses a saturating `rten-gemm` INT8 kernel and, if so,
//! rescales any full-range `u8` weight tensors of `ConvInteger` (into `[0, 127]`)
//! and `MatMulInteger` (into `[64, 191]`, so `shift_cast` maps them into
//! `[-64, 63]`) with unbiased round-to-nearest-even centered at each tensor's
//! zero point, multiplying the paired `f32` dequantization scale by `2.0`.
//!
//! Models already within the safe 7-bit range, non-v2 `.rten` models, and CPUs
//! with non-saturating INT8 dot products (ARM64 NEON/SDOT/I8MM, x86_64 with
//! AVX-512 VNNI, WASM, generic) are left completely untouched.

use std::collections::{HashMap, HashSet};

use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, BudgetVec, try_insert_map, try_insert_set};

/// `.rten` v2 file header length in bytes.
const RTEN_V2_HEADER_LEN: usize = 32;
/// `OperatorType::Cast` in `rten-model-file/src/schema.fbs`.
const OP_CAST: u8 = 5;
/// `OperatorType::Mul` in `rten-model-file/src/schema.fbs`.
const OP_MUL: u8 = 33;
/// `OperatorType::MatMulInteger` in `rten-model-file/src/schema.fbs`.
const OP_MATMUL_INTEGER: u8 = 108;
/// `OperatorType::ConvInteger` in `rten-model-file/src/schema.fbs`.
const OP_CONV_INTEGER: u8 = 110;

/// `NodeKind::OperatorNode` union tag.
const NODE_KIND_OPERATOR: u8 = 1;
/// `NodeKind::ConstantNode` union tag.
const NODE_KIND_CONSTANT: u8 = 2;

/// `ConstantDataType` enum values for external tensor data.
const DTYPE_FLOAT32: u16 = 1;
const DTYPE_UINT8: u16 = 3;

/// `ConstantData` union tags for inline tensor data.
const INLINE_FLOAT_DATA: u8 = 1;
const INLINE_UINT8_DATA: u8 = 4;

/// How often loops poll `Budget` for deadline / cancellation.
const POLL_INTERVAL: usize = 256;

/// Returns whether `rten-gemm`'s default `GemmExecutor<u8, i8, i32>` on this
/// CPU uses a saturating `vpmaddubsw` kernel (matching `rten-gemm 0.28.0`
/// kernel dispatch in `kernels/x86_64.rs` and `rten-simd 0.28.0` `Avx512Isa`).
#[must_use]
pub(super) fn rten_int8_may_saturate() -> bool {
    #[cfg(target_arch = "x86_64")]
    {
        let has_avx512 = std::is_x86_feature_detected!("avx512f")
            && std::is_x86_feature_detected!("avx512vl")
            && std::is_x86_feature_detected!("avx512bw")
            && std::is_x86_feature_detected!("f16c");
        if has_avx512 {
            // `rten-gemm`'s `Avx512Int8Kernel::may_saturate()` is true when
            // AVX-512 VNNI (CPUID leaf 7, subleaf 0, ECX bit 11) is absent.
            let cpuid = std::arch::x86_64::__cpuid_count(7, 0);
            let has_vnni = (cpuid.ecx & (1 << 11)) != 0;
            !has_vnni
        } else {
            std::is_x86_feature_detected!("avx2")
        }
    }
    #[cfg(not(target_arch = "x86_64"))]
    {
        false
    }
}

/// If the current CPU uses a saturating `rten-gemm` INT8 kernel, reduces any
/// full-range `u8` `ConvInteger` and `MatMulInteger` weights in `model_data`
/// into the non-saturating 7-bit range in-place.
pub(super) fn maybe_reduce_saturating_int8_weights(
    model_data: &mut [u8],
    budget: &Budget,
) -> Result<(), LimitExceeded> {
    if !rten_int8_may_saturate() {
        return Ok(());
    }
    reduce_saturating_int8_weights(model_data, budget)
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum ConstDtype {
    Float32,
    UInt8,
    Other,
}

#[derive(Clone, Copy)]
struct ConstInfo {
    dtype: ConstDtype,
    file_offset: usize,
    byte_len: usize,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Int8OpKind {
    Conv,
    MatMul,
}

struct QuantOp {
    kind: Int8OpKind,
    weight_node: usize,
    zp_node: Option<usize>,
    scale_node: usize,
    comb_node: Option<usize>,
}

/// Exact integer division by 2 with IEEE-754 round-to-nearest-even (zero DC bias).
#[inline]
const fn div2_round_even(diff: i32) -> i32 {
    let q = diff / 2;
    let r = diff % 2;
    if r == 0 || (q & 1) == 0 { q } else { q + r }
}

/// Bounds-checked FlatBuffers cursor over the `.rten` model metadata slice.
struct Fb<'a> {
    buf: &'a [u8],
}

impl<'a> Fb<'a> {
    fn u8(&self, at: usize) -> Option<u8> {
        self.buf.get(at).copied()
    }

    fn u16(&self, at: usize) -> Option<u16> {
        let bytes: [u8; 2] = self.buf.get(at..at.checked_add(2)?)?.try_into().ok()?;
        Some(u16::from_le_bytes(bytes))
    }

    fn u32(&self, at: usize) -> Option<u32> {
        let bytes: [u8; 4] = self.buf.get(at..at.checked_add(4)?)?.try_into().ok()?;
        Some(u32::from_le_bytes(bytes))
    }

    fn i32(&self, at: usize) -> Option<i32> {
        let bytes: [u8; 4] = self.buf.get(at..at.checked_add(4)?)?.try_into().ok()?;
        Some(i32::from_le_bytes(bytes))
    }

    fn u64(&self, at: usize) -> Option<u64> {
        let bytes: [u8; 8] = self.buf.get(at..at.checked_add(8)?)?.try_into().ok()?;
        Some(u64::from_le_bytes(bytes))
    }

    fn field(&self, table: usize, index: usize) -> Option<usize> {
        let soff = self.i32(table)?;
        let vtable = if soff >= 0 {
            table.checked_sub(usize::try_from(soff).ok()?)?
        } else {
            table.checked_add(usize::try_from(soff.checked_neg()?).ok()?)?
        };
        let vlen = usize::from(self.u16(vtable)?);
        let entry_off = 4usize.checked_add(index.checked_mul(2)?)?;
        if entry_off.checked_add(2)? > vlen {
            return None;
        }
        let rel = usize::from(self.u16(vtable.checked_add(entry_off)?)?);
        if rel == 0 {
            None
        } else {
            table.checked_add(rel)
        }
    }

    fn table_field(&self, table: usize, index: usize) -> Option<usize> {
        let f = self.field(table, index)?;
        let rel = usize::try_from(self.u32(f)?).ok()?;
        f.checked_add(rel)
    }

    fn vec_header(&self, table: usize, index: usize) -> Option<(usize, usize)> {
        let vec = self.table_field(table, index)?;
        let len = usize::try_from(self.u32(vec)?).ok()?;
        let data = vec.checked_add(4)?;
        Some((data, len))
    }

    fn table_vec_item(&self, data: usize, idx: usize) -> Option<usize> {
        let slot = data.checked_add(idx.checked_mul(4)?)?;
        let rel = usize::try_from(self.u32(slot)?).ok()?;
        slot.checked_add(rel)
    }

    fn i32_vec_item(&self, data: usize, idx: usize) -> Option<i32> {
        self.i32(data.checked_add(idx.checked_mul(4)?)?)
    }

    fn u32_vec_item(&self, data: usize, idx: usize) -> Option<u32> {
        self.u32(data.checked_add(idx.checked_mul(4)?)?)
    }
}

/// Rescales full-range `u8` `ConvInteger` and `MatMulInteger` weights in a
/// `.rten` v2 model buffer in-place so `vpmaddubsw` cannot saturate.
///
/// Unrecognized or malformed buffers are left untouched so `rten::Model::load`
/// produces its standard error.
pub(super) fn reduce_saturating_int8_weights(
    model_data: &mut [u8],
    budget: &Budget,
) -> Result<(), LimitExceeded> {
    if model_data.len() < RTEN_V2_HEADER_LEN || &model_data[0..4] != b"RTEN" {
        return Ok(());
    }
    let version = u32::from_le_bytes(model_data[4..8].try_into().unwrap_or([0; 4]));
    if version != 2 {
        return Ok(());
    }
    let model_offset = usize::try_from(u64::from_le_bytes(
        model_data[8..16].try_into().unwrap_or([0; 8]),
    ))
    .unwrap_or(usize::MAX);
    let model_len = usize::try_from(u64::from_le_bytes(
        model_data[16..24].try_into().unwrap_or([0; 8]),
    ))
    .unwrap_or(usize::MAX);
    let tensor_offset = usize::try_from(u64::from_le_bytes(
        model_data[24..32].try_into().unwrap_or([0; 8]),
    ))
    .unwrap_or(usize::MAX);
    let Some(model_end) = model_offset.checked_add(model_len) else {
        return Ok(());
    };
    if model_end > model_data.len() || tensor_offset > model_data.len() {
        return Ok(());
    }

    let plan =
        match build_reduction_plan(model_data, model_offset, model_end, tensor_offset, budget)? {
            Some(plan) => plan,
            None => return Ok(()),
        };

    apply_reduction_plan(model_data, &plan, budget)
}

struct ReductionPlan {
    consts: HashMap<usize, ConstInfo>,
    ops: Vec<QuantOp>,
}

fn build_reduction_plan(
    model_data: &[u8],
    model_offset: usize,
    model_end: usize,
    tensor_offset: usize,
    budget: &Budget,
) -> Result<Option<ReductionPlan>, LimitExceeded> {
    let fb = Fb {
        buf: &model_data[model_offset..model_end],
    };
    let Some(root) = fb.u32(0).and_then(|v| usize::try_from(v).ok()) else {
        return Ok(None);
    };
    let Some(graph) = fb.table_field(root, 1) else {
        return Ok(None);
    };
    let Some((nodes_data, node_count)) = fb.vec_header(graph, 0) else {
        return Ok(None);
    };

    let mut consts: HashMap<usize, ConstInfo> = HashMap::new();
    // Keep lightweight metadata for Cast, Mul, ConvInteger, and MatMulInteger.
    // consumer_counts tracks total operator inputs referencing each node so we
    // never mutate a constant shared with an unrelated operator.
    let mut consumer_counts: HashMap<usize, usize> = HashMap::new();
    let mut cast_out_by_in: HashMap<usize, usize> = HashMap::new();
    // Maps Mul input node -> (mul_op_node_idx, other_input_node).
    let mut mul_by_in: HashMap<usize, (usize, usize)> = HashMap::new();
    // Maps Mul output node -> (mul_op_node_idx, input_a, input_b).
    let mut mul_inputs_by_out: HashMap<usize, (usize, usize, usize)> = HashMap::new();

    struct RawInt8Op {
        kind: Int8OpKind,
        weight_node: usize,
        zp_node: Option<usize>,
        out_node: usize,
    }
    let mut raw_ops: BudgetVec<'_, RawInt8Op> = BudgetVec::new(budget);

    for node_idx in 0..node_count {
        if node_idx.is_multiple_of(POLL_INTERVAL) {
            budget.poll()?;
        }
        let Some(node) = fb.table_vec_item(nodes_data, node_idx) else {
            return Ok(None);
        };
        let Some(kind_pos) = fb.field(node, 1) else {
            continue;
        };
        let Some(kind) = fb.u8(kind_pos) else {
            continue;
        };
        let Some(data_tab) = fb.table_field(node, 2) else {
            continue;
        };

        if kind == NODE_KIND_CONSTANT {
            if let Some(info) =
                parse_constant(&fb, data_tab, model_offset, tensor_offset, model_data.len())
            {
                try_insert_map(&mut consts, node_idx, info, budget)?;
            }
        } else if kind == NODE_KIND_OPERATOR {
            let Some(op_type_pos) = fb.field(data_tab, 0) else {
                continue;
            };
            let Some(op_type) = fb.u8(op_type_pos) else {
                continue;
            };
            let Some((ins_data, ins_len)) = fb.vec_header(data_tab, 3) else {
                continue;
            };
            let Some((outs_data, outs_len)) = fb.vec_header(data_tab, 4) else {
                continue;
            };
            for i in 0..ins_len {
                if let Some(inp) = fb.i32_vec_item(ins_data, i)
                    && let Ok(u) = usize::try_from(inp)
                {
                    let next = consumer_counts
                        .get(&u)
                        .copied()
                        .unwrap_or(0)
                        .saturating_add(1);
                    try_insert_map(&mut consumer_counts, u, next, budget)?;
                }
            }
            match op_type {
                OP_CAST if ins_len >= 1 && outs_len >= 1 => {
                    if let (Some(inp), Some(out)) = (
                        fb.i32_vec_item(ins_data, 0)
                            .and_then(|v| usize::try_from(v).ok()),
                        fb.i32_vec_item(outs_data, 0)
                            .and_then(|v| usize::try_from(v).ok()),
                    ) {
                        try_insert_map(&mut cast_out_by_in, inp, out, budget)?;
                    }
                }
                OP_MUL if ins_len == 2 && outs_len >= 1 => {
                    if let (Some(a), Some(b), Some(out)) = (
                        fb.i32_vec_item(ins_data, 0)
                            .and_then(|v| usize::try_from(v).ok()),
                        fb.i32_vec_item(ins_data, 1)
                            .and_then(|v| usize::try_from(v).ok()),
                        fb.i32_vec_item(outs_data, 0)
                            .and_then(|v| usize::try_from(v).ok()),
                    ) {
                        try_insert_map(&mut mul_by_in, a, (node_idx, b), budget)?;
                        try_insert_map(&mut mul_by_in, b, (node_idx, a), budget)?;
                        try_insert_map(&mut mul_inputs_by_out, out, (node_idx, a, b), budget)?;
                    }
                }
                OP_CONV_INTEGER | OP_MATMUL_INTEGER if ins_len >= 2 && outs_len >= 1 => {
                    let Some(weight_node) = fb
                        .i32_vec_item(ins_data, 1)
                        .and_then(|v| usize::try_from(v).ok())
                    else {
                        continue;
                    };
                    let zp_node = if ins_len >= 4 {
                        fb.i32_vec_item(ins_data, 3)
                            .and_then(|v| usize::try_from(v).ok())
                    } else {
                        None
                    };
                    let Some(out_node) = fb
                        .i32_vec_item(outs_data, 0)
                        .and_then(|v| usize::try_from(v).ok())
                    else {
                        continue;
                    };
                    let kind = if op_type == OP_CONV_INTEGER {
                        Int8OpKind::Conv
                    } else {
                        Int8OpKind::MatMul
                    };
                    raw_ops.try_push(RawInt8Op {
                        kind,
                        weight_node,
                        zp_node,
                        out_node,
                    })?;
                }
                _ => {}
            }
        }
    }

    if raw_ops.is_empty() {
        return Ok(None);
    }

    // Match each eligible full-range ConvInteger / MatMulInteger op to its
    // dequantization f32 scale constant, and track usage counts to ensure no
    // constant is shared across incompatible ops.
    let mut matched: BudgetVec<'_, QuantOp> = BudgetVec::new(budget);
    let mut weight_kind: HashMap<usize, (Int8OpKind, usize)> = HashMap::new();
    let mut zp_kind: HashMap<usize, (Int8OpKind, usize)> = HashMap::new();
    let mut conflicted_consts: HashSet<usize> = HashSet::new();
    // Deduplicate `(node, consuming_op_idx)` edges so a shared scale constant
    // feeding a single combined `Mul(x_scale, w_scale)` consumed by N quantized
    // ops is counted once for `w_scale` and N times for the combined `Mul` out.
    let mut scale_edges: HashSet<(usize, usize)> = HashSet::new();
    let mut scale_uses: HashMap<usize, usize> = HashMap::new();
    let mut comb_edges: HashSet<(usize, usize)> = HashSet::new();
    let mut comb_uses: HashMap<usize, usize> = HashMap::new();

    for raw in raw_ops.iter() {
        budget.poll()?;
        let Some(&w_info) = consts.get(&raw.weight_node) else {
            continue;
        };
        if w_info.dtype != ConstDtype::UInt8 {
            continue;
        }
        let Some(w_bytes) =
            model_data.get(w_info.file_offset..w_info.file_offset + w_info.byte_len)
        else {
            continue;
        };
        let needs_reduction = match raw.kind {
            Int8OpKind::Conv => w_bytes.iter().any(|&b| b > 127),
            Int8OpKind::MatMul => w_bytes.iter().any(|&b| !(64..=191).contains(&b)),
        };
        if !needs_reduction {
            continue;
        }

        // Validate zero point if present: must be scalar u8 (1 byte) so
        // zero-point-centered halving is exact per tensor.
        if let Some(zp_node) = raw.zp_node {
            let Some(&zp_info) = consts.get(&zp_node) else {
                continue;
            };
            if zp_info.dtype != ConstDtype::UInt8 || zp_info.byte_len != 1 {
                continue;
            }
        }

        // Locate downstream Cast(i32 -> f32) -> Mul(cast_out, scale_expr).
        if consumer_counts.get(&raw.out_node).copied() != Some(1) {
            continue;
        }
        let Some(&cast_out) = cast_out_by_in.get(&raw.out_node) else {
            continue;
        };
        if consumer_counts.get(&cast_out).copied() != Some(1) {
            continue;
        }
        let Some(&(mul_op_idx, other)) = mul_by_in.get(&cast_out) else {
            continue;
        };
        let (scale_node, scale_consumer_op, comb_node) = if consts
            .get(&other)
            .is_some_and(|c| c.dtype == ConstDtype::Float32)
        {
            (other, mul_op_idx, None)
        } else if let Some(&(comb_op_idx, ma, mb)) = mul_inputs_by_out.get(&other) {
            let a_is_f32 = consts
                .get(&ma)
                .is_some_and(|c| c.dtype == ConstDtype::Float32);
            let b_is_f32 = consts
                .get(&mb)
                .is_some_and(|c| c.dtype == ConstDtype::Float32);
            match (a_is_f32, b_is_f32) {
                (true, false) => (ma, comb_op_idx, Some(other)),
                (false, true) => (mb, comb_op_idx, Some(other)),
                _ => continue,
            }
        } else {
            continue;
        };

        // Check consistency across shared constants.
        if let Some(&(prev_kind, count)) = weight_kind.get(&raw.weight_node) {
            if prev_kind != raw.kind {
                try_insert_set(&mut conflicted_consts, raw.weight_node, budget)?;
                continue;
            }
            try_insert_map(
                &mut weight_kind,
                raw.weight_node,
                (prev_kind, count + 1),
                budget,
            )?;
        } else {
            try_insert_map(&mut weight_kind, raw.weight_node, (raw.kind, 1), budget)?;
        }
        if let Some(zp_node) = raw.zp_node {
            if let Some(&(prev_kind, count)) = zp_kind.get(&zp_node) {
                if prev_kind != raw.kind {
                    try_insert_set(&mut conflicted_consts, zp_node, budget)?;
                    continue;
                }
                try_insert_map(&mut zp_kind, zp_node, (prev_kind, count + 1), budget)?;
            } else {
                try_insert_map(&mut zp_kind, zp_node, (raw.kind, 1), budget)?;
            }
        }
        if try_insert_set(&mut scale_edges, (scale_node, scale_consumer_op), budget)? {
            let sc_next = scale_uses.get(&scale_node).copied().unwrap_or(0) + 1;
            try_insert_map(&mut scale_uses, scale_node, sc_next, budget)?;
        }
        if let Some(comb) = comb_node
            && try_insert_set(&mut comb_edges, (comb, mul_op_idx), budget)?
        {
            let comb_next = comb_uses.get(&comb).copied().unwrap_or(0) + 1;
            try_insert_map(&mut comb_uses, comb, comb_next, budget)?;
        }

        matched.try_push(QuantOp {
            kind: raw.kind,
            weight_node: raw.weight_node,
            zp_node: raw.zp_node,
            scale_node,
            comb_node,
        })?;
    }

    // Keep only ops whose weight, zero-point, scale, and optional combined-scale
    // nodes are consumed exclusively by retained ops (propagating rejections
    // transitively across shared constants).
    let mut bad_nodes: HashSet<usize> = conflicted_consts;
    let ops_vec = matched.into_vec();
    for _ in 0..=ops_vec.len() {
        let prev_bad = bad_nodes.len();
        for op in &ops_vec {
            let w_ok = !bad_nodes.contains(&op.weight_node)
                && weight_kind.get(&op.weight_node).is_some_and(|&(_, used)| {
                    consumer_counts.get(&op.weight_node).copied() == Some(used)
                });
            let zp_ok = op.zp_node.is_none_or(|z| {
                !bad_nodes.contains(&z)
                    && zp_kind
                        .get(&z)
                        .is_some_and(|&(_, used)| consumer_counts.get(&z).copied() == Some(used))
            });
            let sc_ok = !bad_nodes.contains(&op.scale_node)
                && scale_uses.get(&op.scale_node).is_some_and(|&used| {
                    consumer_counts.get(&op.scale_node).copied() == Some(used)
                });
            let comb_ok = op.comb_node.is_none_or(|c| {
                !bad_nodes.contains(&c)
                    && comb_uses
                        .get(&c)
                        .is_some_and(|&used| consumer_counts.get(&c).copied() == Some(used))
            });
            if !(w_ok && zp_ok && sc_ok && comb_ok) {
                try_insert_set(&mut bad_nodes, op.weight_node, budget)?;
                if let Some(z) = op.zp_node {
                    try_insert_set(&mut bad_nodes, z, budget)?;
                }
                try_insert_set(&mut bad_nodes, op.scale_node, budget)?;
                if let Some(c) = op.comb_node {
                    try_insert_set(&mut bad_nodes, c, budget)?;
                }
            }
        }
        if bad_nodes.len() == prev_bad {
            break;
        }
    }

    let mut safe_ops: BudgetVec<'_, QuantOp> = BudgetVec::new(budget);
    for op in ops_vec {
        let clean = !bad_nodes.contains(&op.weight_node)
            && op.zp_node.is_none_or(|z| !bad_nodes.contains(&z))
            && !bad_nodes.contains(&op.scale_node)
            && op.comb_node.is_none_or(|c| !bad_nodes.contains(&c));
        if clean {
            safe_ops.try_push(op)?;
        }
    }

    if safe_ops.is_empty() {
        return Ok(None);
    }

    Ok(Some(ReductionPlan {
        consts,
        ops: safe_ops.into_vec(),
    }))
}

fn parse_constant(
    fb: &Fb<'_>,
    data_tab: usize,
    model_offset: usize,
    tensor_offset: usize,
    file_len: usize,
) -> Option<ConstInfo> {
    if let Some(doff_pos) = fb.field(data_tab, 4) {
        let rel_offset = usize::try_from(fb.u64(doff_pos)?).ok()?;
        let dtype_raw = fb.u16(fb.field(data_tab, 3)?)?;
        let (dtype, elem_size) = match dtype_raw {
            DTYPE_FLOAT32 => (ConstDtype::Float32, 4usize),
            DTYPE_UINT8 => (ConstDtype::UInt8, 1usize),
            _ => {
                return Some(ConstInfo {
                    dtype: ConstDtype::Other,
                    file_offset: 0,
                    byte_len: 0,
                });
            }
        };
        let (shape_data, shape_len) = fb.vec_header(data_tab, 0)?;
        let mut numel = 1usize;
        for i in 0..shape_len {
            let dim = usize::try_from(fb.u32_vec_item(shape_data, i)?).ok()?;
            numel = numel.checked_mul(dim)?;
        }
        let byte_len = numel.checked_mul(elem_size)?;
        let file_offset = tensor_offset.checked_add(rel_offset)?;
        if file_offset.checked_add(byte_len)? > file_len {
            return None;
        }
        return Some(ConstInfo {
            dtype,
            file_offset,
            byte_len,
        });
    }

    // Inline ConstantData union: field 1 = type tag, field 2 = table.
    let cd_kind = fb.u8(fb.field(data_tab, 1)?)?;
    let (dtype, elem_size) = match cd_kind {
        INLINE_FLOAT_DATA => (ConstDtype::Float32, 4usize),
        INLINE_UINT8_DATA => (ConstDtype::UInt8, 1usize),
        _ => {
            return Some(ConstInfo {
                dtype: ConstDtype::Other,
                file_offset: 0,
                byte_len: 0,
            });
        }
    };
    let cd_tab = fb.table_field(data_tab, 2)?;
    let (vec_data, vec_len) = fb.vec_header(cd_tab, 0)?;
    let byte_len = vec_len.checked_mul(elem_size)?;
    let file_offset = model_offset.checked_add(vec_data)?;
    if file_offset.checked_add(byte_len)? > file_len {
        return None;
    }
    Some(ConstInfo {
        dtype,
        file_offset,
        byte_len,
    })
}

fn apply_reduction_plan(
    model_data: &mut [u8],
    plan: &ReductionPlan,
    budget: &Budget,
) -> Result<(), LimitExceeded> {
    let mut done_weights: HashSet<usize> = HashSet::new();
    let mut done_zps: HashSet<usize> = HashSet::new();
    let mut done_scales: HashSet<usize> = HashSet::new();

    // Snapshot original scalar zero points before mutating any shared zp byte.
    let mut orig_zps: HashMap<usize, u8> = HashMap::new();
    for op in &plan.ops {
        if let Some(zp_node) = op.zp_node
            && !orig_zps.contains_key(&zp_node)
            && let Some(&zp_info) = plan.consts.get(&zp_node)
            && let Some(&zp_byte) = model_data.get(zp_info.file_offset)
        {
            try_insert_map(&mut orig_zps, zp_node, zp_byte, budget)?;
        }
    }

    for op in &plan.ops {
        budget.poll()?;
        let zp_orig = op
            .zp_node
            .and_then(|z| orig_zps.get(&z).copied())
            .unwrap_or(0);
        let (lo, hi, new_zp) = match op.kind {
            Int8OpKind::Conv => (0i32, 127i32, zp_orig / 2),
            Int8OpKind::MatMul => (64i32, 191i32, 64 + (zp_orig / 2)),
        };

        if try_insert_set(&mut done_weights, op.weight_node, budget)?
            && let Some(&w_info) = plan.consts.get(&op.weight_node)
            && let Some(slice) =
                model_data.get_mut(w_info.file_offset..w_info.file_offset + w_info.byte_len)
        {
            let zp_i = i32::from(zp_orig);
            let new_zp_i = i32::from(new_zp);
            for (idx, byte) in slice.iter_mut().enumerate() {
                if idx.is_multiple_of(65_536) {
                    budget.poll()?;
                }
                let q = div2_round_even(i32::from(*byte) - zp_i);
                *byte = (new_zp_i + q).clamp(lo, hi) as u8;
            }
        }

        if let Some(zp_node) = op.zp_node
            && try_insert_set(&mut done_zps, zp_node, budget)?
            && let Some(&zp_info) = plan.consts.get(&zp_node)
            && let Some(slot) = model_data.get_mut(zp_info.file_offset)
        {
            *slot = new_zp;
        }

        if try_insert_set(&mut done_scales, op.scale_node, budget)?
            && let Some(&s_info) = plan.consts.get(&op.scale_node)
            && let Some(slice) =
                model_data.get_mut(s_info.file_offset..s_info.file_offset + s_info.byte_len)
        {
            for chunk in slice.as_chunks_mut::<4>().0 {
                let value = f32::from_le_bytes(*chunk);
                *chunk = (value * 2.0).to_le_bytes();
            }
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::layout::tests::FbBuilder;

    #[test]
    fn div2_round_even_matches_ieee754_ties_to_even() {
        for diff in -255..=255 {
            let expected = (f64::from(diff) / 2.0).round_ties_even() as i32;
            assert_eq!(div2_round_even(diff), expected, "diff={diff}");
        }
    }

    /// Builds a minimal `.rten` v2 model with one `ConvInteger` and one
    /// `MatMulInteger` whose weights span `[0, 255]`, each followed by
    /// `Cast -> Mul(scale)`.
    fn build_v2_int8_fixture(
        conv_weights: &[u8],
        conv_zp: u8,
        matmul_weights: &[u8],
        mm_zp: u8,
    ) -> Vec<u8> {
        use crate::layout::tests::FieldVal;

        let mut tensor_seg = Vec::new();
        let append_u8 = |seg: &mut Vec<u8>, bytes: &[u8]| -> u64 {
            while !seg.len().is_multiple_of(8) {
                seg.push(0);
            }
            let off = seg.len() as u64;
            seg.extend_from_slice(bytes);
            off
        };
        let append_f32 = |seg: &mut Vec<u8>, vals: &[f32]| -> u64 {
            while !seg.len().is_multiple_of(8) {
                seg.push(0);
            }
            let off = seg.len() as u64;
            for v in vals {
                seg.extend_from_slice(&v.to_le_bytes());
            }
            off
        };

        let conv_w_off = append_u8(&mut tensor_seg, conv_weights);
        let conv_zp_off = append_u8(&mut tensor_seg, &[conv_zp]);
        let conv_sc_off = append_f32(&mut tensor_seg, &[0.25]);
        let mm_w_off = append_u8(&mut tensor_seg, matmul_weights);
        let mm_zp_off = append_u8(&mut tensor_seg, &[mm_zp]);
        let mm_sc_off = append_f32(&mut tensor_seg, &[0.5]);

        let mut fb = FbBuilder::new();
        let mut nodes = Vec::new();

        let add_val = |fb: &mut FbBuilder, nodes: &mut Vec<usize>, name: &str| {
            let n = fb.write_string(name);
            let v = fb.write_table(2, &[]);
            nodes.push(fb.write_table(
                3,
                &[
                    (0, FieldVal::Offset(n)),
                    (1, FieldVal::U8(3)),
                    (2, FieldVal::Offset(v)),
                ],
            ));
        };
        let add_const = |fb: &mut FbBuilder,
                         nodes: &mut Vec<usize>,
                         name: &str,
                         shape: &[u32],
                         dtype: u16,
                         off: u64| {
            let n = fb.write_string(name);
            let sh = fb.write_vec_u32(shape);
            let c = fb.write_table(
                5,
                &[
                    (0, FieldVal::Offset(sh)),
                    (3, FieldVal::U16(dtype)),
                    (4, FieldVal::U64(off)),
                ],
            );
            nodes.push(fb.write_table(
                3,
                &[
                    (0, FieldVal::Offset(n)),
                    (1, FieldVal::U8(2)),
                    (2, FieldVal::Offset(c)),
                ],
            ));
        };
        let add_op = |fb: &mut FbBuilder,
                      nodes: &mut Vec<usize>,
                      name: &str,
                      op_type: u8,
                      ins: &[i32],
                      outs: &[i32]| {
            let n = fb.write_string(name);
            let iv = fb.write_vec_i32(ins);
            let ov = fb.write_vec_i32(outs);
            let o = fb.write_table(
                5,
                &[
                    (0, FieldVal::U8(op_type)),
                    (3, FieldVal::Offset(iv)),
                    (4, FieldVal::Offset(ov)),
                ],
            );
            nodes.push(fb.write_table(
                3,
                &[
                    (0, FieldVal::Offset(n)),
                    (1, FieldVal::U8(1)),
                    (2, FieldVal::Offset(o)),
                ],
            ));
        };

        // 0: x_u8, 1: x_zp, 2: x_scale
        add_val(&mut fb, &mut nodes, "x_u8");
        add_val(&mut fb, &mut nodes, "x_zp");
        add_val(&mut fb, &mut nodes, "x_scale");
        // 3: conv_w, 4: conv_zp, 5: conv_scale
        add_const(
            &mut fb,
            &mut nodes,
            "conv_w",
            &[conv_weights.len() as u32],
            DTYPE_UINT8,
            conv_w_off,
        );
        add_const(
            &mut fb,
            &mut nodes,
            "conv_zp",
            &[],
            DTYPE_UINT8,
            conv_zp_off,
        );
        add_const(
            &mut fb,
            &mut nodes,
            "conv_scale",
            &[],
            DTYPE_FLOAT32,
            conv_sc_off,
        );
        // 6: conv_i32, 7: conv_f32, 8: conv_comb_scale, 9: conv_out
        add_val(&mut fb, &mut nodes, "conv_i32");
        add_val(&mut fb, &mut nodes, "conv_f32");
        add_val(&mut fb, &mut nodes, "conv_comb_scale");
        add_val(&mut fb, &mut nodes, "conv_out");
        // 10: mm_w, 11: mm_zp, 12: mm_scale
        add_const(
            &mut fb,
            &mut nodes,
            "mm_w",
            &[matmul_weights.len() as u32],
            DTYPE_UINT8,
            mm_w_off,
        );
        add_const(&mut fb, &mut nodes, "mm_zp", &[], DTYPE_UINT8, mm_zp_off);
        add_const(
            &mut fb,
            &mut nodes,
            "mm_scale",
            &[],
            DTYPE_FLOAT32,
            mm_sc_off,
        );
        // 13: mm_i32, 14: mm_f32, 15: mm_out
        add_val(&mut fb, &mut nodes, "mm_i32");
        add_val(&mut fb, &mut nodes, "mm_f32");
        add_val(&mut fb, &mut nodes, "mm_out");

        // Ops: ConvInteger -> Cast -> Mul(x_scale * conv_scale)
        add_op(
            &mut fb,
            &mut nodes,
            "conv",
            OP_CONV_INTEGER,
            &[0, 3, 1, 4],
            &[6],
        );
        add_op(&mut fb, &mut nodes, "conv_cast", OP_CAST, &[6], &[7]);
        add_op(&mut fb, &mut nodes, "conv_sc_mul", OP_MUL, &[2, 5], &[8]);
        add_op(&mut fb, &mut nodes, "conv_mul", OP_MUL, &[7, 8], &[9]);

        // Ops: MatMulInteger -> Cast -> Mul(mm_scale directly)
        add_op(
            &mut fb,
            &mut nodes,
            "mm",
            OP_MATMUL_INTEGER,
            &[0, 10, 1, 11],
            &[13],
        );
        add_op(&mut fb, &mut nodes, "mm_cast", OP_CAST, &[13], &[14]);
        add_op(&mut fb, &mut nodes, "mm_mul", OP_MUL, &[14, 12], &[15]);

        let nodes_vec = fb.write_vec_offsets(&nodes);
        let ins_vec = fb.write_vec_u32(&[0, 1, 2]);
        let outs_vec = fb.write_vec_u32(&[9, 15]);
        let graph = fb.write_table(
            4,
            &[
                (0, FieldVal::Offset(nodes_vec)),
                (1, FieldVal::Offset(ins_vec)),
                (2, FieldVal::Offset(outs_vec)),
            ],
        );
        let model = fb.write_table(3, &[(0, FieldVal::I32(2)), (1, FieldVal::Offset(graph))]);
        let fb_bytes = fb.finish(model);

        let model_offset = RTEN_V2_HEADER_LEN as u64;
        let model_len = fb_bytes.len() as u64;
        let mut tensor_offset = model_offset + model_len;
        while !tensor_offset.is_multiple_of(8) {
            tensor_offset += 1;
        }
        let mut out = Vec::new();
        out.extend_from_slice(b"RTEN");
        out.extend_from_slice(&2u32.to_le_bytes());
        out.extend_from_slice(&model_offset.to_le_bytes());
        out.extend_from_slice(&model_len.to_le_bytes());
        out.extend_from_slice(&tensor_offset.to_le_bytes());
        out.extend_from_slice(&fb_bytes);
        while (out.len() as u64) < tensor_offset {
            out.push(0);
        }
        out.extend_from_slice(&tensor_seg);
        out
    }

    #[test]
    fn reduces_full_range_v2_weights_and_is_idempotent() {
        let conv_w = [0u8, 127, 140, 200, 255];
        let mm_w = [0u8, 64, 127, 191, 255];
        let mut data = build_v2_int8_fixture(&conv_w, 140, &mm_w, 127);
        let tensor_offset = u64::from_le_bytes(data[24..32].try_into().unwrap()) as usize;

        reduce_saturating_int8_weights(&mut data, &Budget::default()).unwrap();

        let reduced_conv = &data[tensor_offset..tensor_offset + 5];
        assert!(reduced_conv.iter().all(|&b| b <= 127), "{reduced_conv:?}");
        assert_eq!(reduced_conv, &[0, 64, 70, 100, 127]);
        assert_eq!(data[tensor_offset + 8], 70); // conv_zp = 140 / 2
        let conv_sc = f32::from_le_bytes(
            data[tensor_offset + 16..tensor_offset + 20]
                .try_into()
                .unwrap(),
        );
        assert_eq!(conv_sc, 0.5);

        let reduced_mm = &data[tensor_offset + 24..tensor_offset + 29];
        assert!(
            reduced_mm.iter().all(|&b| (64..=191).contains(&b)),
            "{reduced_mm:?}"
        );
        assert_eq!(reduced_mm, &[64, 95, 127, 159, 191]);
        assert_eq!(data[tensor_offset + 32], 127); // mm_zp = 64 + 127 / 2
        let mm_sc = f32::from_le_bytes(
            data[tensor_offset + 40..tensor_offset + 44]
                .try_into()
                .unwrap(),
        );
        assert_eq!(mm_sc, 1.0);

        // Second pass must be an exact no-op (already inside 7-bit safe ranges).
        let after_first = data.clone();
        reduce_saturating_int8_weights(&mut data, &Budget::default()).unwrap();
        assert_eq!(data, after_first);
    }

    #[test]
    fn shared_matmul_scale_and_unrelated_consumer_guard() {
        use crate::layout::tests::FieldVal;

        // Build a v2 model with:
        // - Two MatMulInteger ops sharing one weight, one inline scalar u8 zp,
        //   and one inline scalar f32 scale via a single combined Mul(x_scale, scale).
        // - One ConvInteger whose scale is ALSO consumed by an unrelated Mul,
        //   which must prevent mutating that ConvInteger's weights or scale.
        let mut tensor_seg = Vec::new();
        let mm_w_off = 0u64;
        tensor_seg.extend_from_slice(&[0u8, 64, 128, 191, 255, 0, 0, 0]);
        let conv_w_off = 8u64;
        tensor_seg.extend_from_slice(&[0u8, 128, 255, 0, 0, 0, 0, 0]);

        let mut fb = FbBuilder::new();
        let mut nodes = Vec::new();
        let add_val = |fb: &mut FbBuilder, nodes: &mut Vec<usize>, name: &str| {
            let n = fb.write_string(name);
            let v = fb.write_table(2, &[]);
            nodes.push(fb.write_table(
                3,
                &[
                    (0, FieldVal::Offset(n)),
                    (1, FieldVal::U8(3)),
                    (2, FieldVal::Offset(v)),
                ],
            ));
        };
        let add_ext_const = |fb: &mut FbBuilder,
                             nodes: &mut Vec<usize>,
                             name: &str,
                             shape: &[u32],
                             dtype: u16,
                             off: u64| {
            let n = fb.write_string(name);
            let sh = fb.write_vec_u32(shape);
            let c = fb.write_table(
                5,
                &[
                    (0, FieldVal::Offset(sh)),
                    (3, FieldVal::U16(dtype)),
                    (4, FieldVal::U64(off)),
                ],
            );
            nodes.push(fb.write_table(
                3,
                &[
                    (0, FieldVal::Offset(n)),
                    (1, FieldVal::U8(2)),
                    (2, FieldVal::Offset(c)),
                ],
            ));
        };
        let add_inline_u8 =
            |fb: &mut FbBuilder, nodes: &mut Vec<usize>, name: &str, vals: &[u8]| {
                let n = fb.write_string(name);
                let sh = fb.write_vec_u32(&[]);
                let dv = fb.write_vec_u8(vals);
                let ud = fb.write_table(1, &[(0, FieldVal::Offset(dv))]);
                let c = fb.write_table(
                    5,
                    &[
                        (0, FieldVal::Offset(sh)),
                        (1, FieldVal::U8(INLINE_UINT8_DATA)),
                        (2, FieldVal::Offset(ud)),
                    ],
                );
                nodes.push(fb.write_table(
                    3,
                    &[
                        (0, FieldVal::Offset(n)),
                        (1, FieldVal::U8(2)),
                        (2, FieldVal::Offset(c)),
                    ],
                ));
            };
        let add_inline_f32 =
            |fb: &mut FbBuilder, nodes: &mut Vec<usize>, name: &str, vals: &[f32]| {
                let n = fb.write_string(name);
                let sh = fb.write_vec_u32(&[]);
                let dv = fb.write_vec_f32(vals);
                let fd = fb.write_table(1, &[(0, FieldVal::Offset(dv))]);
                let c = fb.write_table(
                    5,
                    &[
                        (0, FieldVal::Offset(sh)),
                        (1, FieldVal::U8(INLINE_FLOAT_DATA)),
                        (2, FieldVal::Offset(fd)),
                    ],
                );
                nodes.push(fb.write_table(
                    3,
                    &[
                        (0, FieldVal::Offset(n)),
                        (1, FieldVal::U8(2)),
                        (2, FieldVal::Offset(c)),
                    ],
                ));
            };
        let add_op = |fb: &mut FbBuilder,
                      nodes: &mut Vec<usize>,
                      name: &str,
                      op_type: u8,
                      ins: &[i32],
                      outs: &[i32]| {
            let n = fb.write_string(name);
            let iv = fb.write_vec_i32(ins);
            let ov = fb.write_vec_i32(outs);
            let o = fb.write_table(
                5,
                &[
                    (0, FieldVal::U8(op_type)),
                    (3, FieldVal::Offset(iv)),
                    (4, FieldVal::Offset(ov)),
                ],
            );
            nodes.push(fb.write_table(
                3,
                &[
                    (0, FieldVal::Offset(n)),
                    (1, FieldVal::U8(1)),
                    (2, FieldVal::Offset(o)),
                ],
            ));
        };

        // 0: x, 1: x_zp, 2: x_scale
        add_val(&mut fb, &mut nodes, "x");
        add_val(&mut fb, &mut nodes, "x_zp");
        add_val(&mut fb, &mut nodes, "x_scale");
        // 3: mm_w (ext), 4: mm_zp (inline u8 = 128), 5: mm_scale (inline f32 = 0.25)
        add_ext_const(&mut fb, &mut nodes, "mm_w", &[5], DTYPE_UINT8, mm_w_off);
        add_inline_u8(&mut fb, &mut nodes, "mm_zp", &[128]);
        add_inline_f32(&mut fb, &mut nodes, "mm_scale", &[0.25]);
        // 6: mm_comb, 7..=12: mm1/mm2 i32, f32, out
        add_val(&mut fb, &mut nodes, "mm_comb");
        add_val(&mut fb, &mut nodes, "mm1_i32");
        add_val(&mut fb, &mut nodes, "mm1_f32");
        add_val(&mut fb, &mut nodes, "mm1_out");
        add_val(&mut fb, &mut nodes, "mm2_i32");
        add_val(&mut fb, &mut nodes, "mm2_f32");
        add_val(&mut fb, &mut nodes, "mm2_out");
        // 13: conv_w (ext), 14: conv_zp (inline), 15: conv_scale (inline, shared with unrelated Mul)
        add_ext_const(&mut fb, &mut nodes, "conv_w", &[3], DTYPE_UINT8, conv_w_off);
        add_inline_u8(&mut fb, &mut nodes, "conv_zp", &[120]);
        add_inline_f32(&mut fb, &mut nodes, "conv_scale", &[0.5]);
        // 16..=19: conv i32/f32/out, extra_out
        add_val(&mut fb, &mut nodes, "conv_i32");
        add_val(&mut fb, &mut nodes, "conv_f32");
        add_val(&mut fb, &mut nodes, "conv_out");
        add_val(&mut fb, &mut nodes, "extra_out");

        // Shared combined scale for two MatMulInteger ops.
        add_op(&mut fb, &mut nodes, "mm_sc_mul", OP_MUL, &[2, 5], &[6]);
        add_op(
            &mut fb,
            &mut nodes,
            "mm1",
            OP_MATMUL_INTEGER,
            &[0, 3, 1, 4],
            &[7],
        );
        add_op(&mut fb, &mut nodes, "mm1_cast", OP_CAST, &[7], &[8]);
        add_op(&mut fb, &mut nodes, "mm1_mul", OP_MUL, &[8, 6], &[9]);
        add_op(
            &mut fb,
            &mut nodes,
            "mm2",
            OP_MATMUL_INTEGER,
            &[0, 3, 1, 4],
            &[10],
        );
        add_op(&mut fb, &mut nodes, "mm2_cast", OP_CAST, &[10], &[11]);
        add_op(&mut fb, &mut nodes, "mm2_mul", OP_MUL, &[11, 6], &[12]);

        // ConvInteger whose scale is ALSO consumed by an unrelated Mul.
        add_op(
            &mut fb,
            &mut nodes,
            "conv",
            OP_CONV_INTEGER,
            &[0, 13, 1, 14],
            &[16],
        );
        add_op(&mut fb, &mut nodes, "conv_cast", OP_CAST, &[16], &[17]);
        add_op(&mut fb, &mut nodes, "conv_mul", OP_MUL, &[17, 15], &[18]);
        add_op(&mut fb, &mut nodes, "unrelated", OP_MUL, &[2, 15], &[19]);

        let nodes_vec = fb.write_vec_offsets(&nodes);
        let ins_vec = fb.write_vec_u32(&[0, 1, 2]);
        let outs_vec = fb.write_vec_u32(&[9, 12, 18, 19]);
        let graph = fb.write_table(
            4,
            &[
                (0, FieldVal::Offset(nodes_vec)),
                (1, FieldVal::Offset(ins_vec)),
                (2, FieldVal::Offset(outs_vec)),
            ],
        );
        let model = fb.write_table(3, &[(0, FieldVal::I32(2)), (1, FieldVal::Offset(graph))]);
        let fb_bytes = fb.finish(model);

        let model_offset = RTEN_V2_HEADER_LEN as u64;
        let model_len = fb_bytes.len() as u64;
        let mut tensor_offset = model_offset + model_len;
        while !tensor_offset.is_multiple_of(8) {
            tensor_offset += 1;
        }
        let mut data = Vec::new();
        data.extend_from_slice(b"RTEN");
        data.extend_from_slice(&2u32.to_le_bytes());
        data.extend_from_slice(&model_offset.to_le_bytes());
        data.extend_from_slice(&model_len.to_le_bytes());
        data.extend_from_slice(&tensor_offset.to_le_bytes());
        data.extend_from_slice(&fb_bytes);
        while (data.len() as u64) < tensor_offset {
            data.push(0);
        }
        data.extend_from_slice(&tensor_seg);

        let plan = build_reduction_plan(
            &data,
            model_offset as usize,
            (model_offset + model_len) as usize,
            tensor_offset as usize,
            &Budget::default(),
        )
        .unwrap()
        .expect("shared MatMulInteger ops should form a valid plan");
        assert_eq!(plan.ops.len(), 2, "only the two MatMulInteger ops are safe");
        let mm_zp_off = plan.consts[&4].file_offset;
        let mm_sc_off = plan.consts[&5].file_offset;
        let conv_sc_off = plan.consts[&15].file_offset;

        reduce_saturating_int8_weights(&mut data, &Budget::default()).unwrap();

        let toff = tensor_offset as usize;
        // Shared MatMulInteger weights, inline zp, and inline scale were reduced ONCE.
        assert_eq!(&data[toff..toff + 5], &[64, 96, 128, 160, 191]);
        assert_eq!(data[mm_zp_off], 128); // 64 + 128 / 2
        assert_eq!(
            f32::from_le_bytes(data[mm_sc_off..mm_sc_off + 4].try_into().unwrap()),
            0.5
        );
        // ConvInteger with shared unrelated consumer was left completely untouched.
        assert_eq!(&data[toff + 8..toff + 11], &[0, 128, 255]);
        assert_eq!(
            f32::from_le_bytes(data[conv_sc_off..conv_sc_off + 4].try_into().unwrap()),
            0.5
        );
    }

    #[test]
    fn reduces_installed_layout_model_if_present() {
        let model_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join(
            "../../.venv/lib/python3.11/site-packages/pyropdf_layout_models/PP-DocLayout_plus-L_int8.rten",
        );
        let Ok(mut data) = std::fs::read(&model_path) else {
            return;
        };
        let model_offset = u64::from_le_bytes(data[8..16].try_into().unwrap()) as usize;
        let model_len = u64::from_le_bytes(data[16..24].try_into().unwrap()) as usize;
        let tensor_offset = u64::from_le_bytes(data[24..32].try_into().unwrap()) as usize;
        let plan = build_reduction_plan(
            &data,
            model_offset,
            model_offset + model_len,
            tensor_offset,
            &Budget::default(),
        )
        .unwrap()
        .expect("PP-DocLayout_plus-L_int8.rten must need reduction");
        assert_eq!(
            plan.ops.len(),
            212,
            "all 110 ConvInteger + 102 MatMulInteger ops must be reduced"
        );
        reduce_saturating_int8_weights(&mut data, &Budget::default()).unwrap();
        // Second pass must find nothing left to reduce.
        let second = build_reduction_plan(
            &data,
            model_offset,
            model_offset + model_len,
            tensor_offset,
            &Budget::default(),
        )
        .unwrap();
        assert!(second.is_none(), "reduced model must need no further pass");
        // Model must still load cleanly in rten.
        rten::Model::load(data).expect("reduced model must load in rten");
    }
}
