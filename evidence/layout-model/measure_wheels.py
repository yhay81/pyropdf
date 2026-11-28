# ruff: noqa: INP001, S603 - standalone reproducible wheel-size measurement script.
"""Measure exact native abi3 wheel-size deltas for #1199 (PP-DocLayout on RTen).

Applies temporary uncommitted probes to the workspace, builds release abi3 wheels with the
pinned maturin release options (`tools/native-wheel-args.txt`), repacks each wheel with
Zopfli (`tools/repack_wheel.py`), compares against the untouched baseline wheel using
`tools/wheel_size.py`, checks `cargo-audit` and `wasm32-unknown-emscripten`, and restores
the workspace cleanly in a `finally` block.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MIB = 1024 * 1024
CAP_BYTES = 9 * MIB

# Published v0.4.1 abi3 release wheels (from `gh release view v0.4.1`).
V041_ABI3_WHEELS: dict[str, int] = {
    "win_amd64": 8_961_169,
    "manylinux_x86_64": 8_900_191,
    "macosx_x86_64": 8_566_157,
    "manylinux_aarch64": 8_324_603,
    "macosx_arm64": 7_735_902,
}
V041_WASM_WHEEL = 6_748_614

# Standalone wasm32-unknown-unknown probe measurements for rten 0.28.0 (rten_format vs +onnx_format).
STANDALONE_RTEN_PROBES: dict[str, dict[str, int]] = {
    "aarch64_apple_darwin": {
        "rten_format_raw": 1_852_720,
        "rten_format_zlib9": 672_308,
        "onnx_format_delta_raw": 150_464,
        "onnx_format_delta_zlib9": 71_482,
    },
    "wasm32_unknown_unknown": {
        "rten_format_raw": 1_596_605,
        "rten_format_zlib9": 510_469,
        "onnx_format_delta_raw": 162_470,
        "onnx_format_delta_zlib9": 45_893,
    },
}

LAYOUT_ENGINE_PROBE = """//! Temporary #1199 wheel-size probe for PP-DocLayout inference over pyropdf-engine's rten.
use std::path::Path;
use std::sync::Arc;

use rten::{Model, NodeId, RunOptions, ThreadPool, ValueOrView};
use rten_tensor::{AsView, Layout, NdTensor};

pub struct SpikeLayoutEngine {
    model: Model,
    pool: Arc<ThreadPool>,
    out_ids: Vec<NodeId>,
    input_size: usize,
}

impl SpikeLayoutEngine {
    pub fn load(path: &Path, threads: usize, max_model_size: u64, input_size: usize) -> Result<Self, String> {
        let meta = std::fs::metadata(path).map_err(|e| format!("stat model: {e}"))?;
        if meta.len() > max_model_size {
            return Err(format!("model exceeds max_model_size ({} > {max_model_size})", meta.len()));
        }
        let bytes = std::fs::read(path).map_err(|e| format!("read model: {e}"))?;
        let model = Model::load(bytes).map_err(|e| format!("load model: {e}"))?;
        let out_ids = model.output_ids().to_vec();
        let pool = Arc::new(ThreadPool::with_num_threads(threads.clamp(1, 16)));
        Ok(Self { model, pool, out_ids, input_size })
    }

    pub fn detect_rgba(
        &self,
        rgba: &[u8],
        width: usize,
        height: usize,
        min_confidence: f32,
    ) -> Result<Vec<(i32, f32, f32, f32, f32, f32)>, String> {
        if width == 0 || height == 0 || rgba.len() != width * height * 4 {
            return Err("invalid RGBA raster".to_owned());
        }
        let target = self.input_size;
        let plane = target * target;
        let mut data = Vec::new();
        data.try_reserve_exact(3 * plane).map_err(|e| format!("alloc input: {e}"))?;
        data.resize(3 * plane, 0.0_f32);
        let sx = width as f32 / target as f32;
        let sy = height as f32 / target as f32;
        let max_x = (width - 1) as f32;
        let max_y = (height - 1) as f32;
        for y in 0..target {
            let fy = ((y as f32 + 0.5) * sy - 0.5).clamp(0.0, max_y);
            let y0 = fy.floor() as usize;
            let y1 = (y0 + 1).min(height - 1);
            let wy = fy - y0 as f32;
            for x in 0..target {
                let fx = ((x as f32 + 0.5) * sx - 0.5).clamp(0.0, max_x);
                let x0 = fx.floor() as usize;
                let x1 = (x0 + 1).min(width - 1);
                let wx = fx - x0 as f32;
                let idx = y * target + x;
                let p00 = (y0 * width + x0) * 4;
                let p01 = (y0 * width + x1) * 4;
                let p10 = (y1 * width + x0) * 4;
                let p11 = (y1 * width + x1) * 4;
                for c in 0..3 {
                    let top = f32::from(rgba[p00 + c]) * (1.0 - wx) + f32::from(rgba[p01 + c]) * wx;
                    let bot = f32::from(rgba[p10 + c]) * (1.0 - wx) + f32::from(rgba[p11 + c]) * wx;
                    data[c * plane + idx] = (top * (1.0 - wy) + bot * wy) / 255.0;
                }
            }
        }
        let image = NdTensor::from_data([1, 3, target, target], data);
        let scale = NdTensor::<f32, 2>::from_data(
            [1, 2],
            vec![target as f32 / height as f32, target as f32 / width as f32],
        );
        let im_shape = NdTensor::<f32, 2>::from_data([1, 2], vec![target as f32, target as f32]);
        let mut inputs: Vec<(NodeId, ValueOrView<'_>)> = Vec::new();
        for &id in self.model.input_ids() {
            let name = self.model.node_info(id).and_then(|n| n.name()).unwrap_or("");
            match name {
                "image" => inputs.push((id, image.view().into())),
                "scale_factor" => inputs.push((id, scale.view().into())),
                "im_shape" => inputs.push((id, im_shape.view().into())),
                _ => {}
            }
        }
        let opts = RunOptions::default().with_thread_pool(Some(Arc::clone(&self.pool)));
        let outs = self
            .model
            .run(inputs, &self.out_ids, Some(opts))
            .map_err(|e| format!("layout infer: {e}"))?;
        let dets = outs[0]
            .as_tensor_view::<f32>()
            .ok_or_else(|| "layout output 0 is not f32".to_owned())?;
        let rows = dets.shape()[0];
        let cols = dets.shape()[1];
        let valid_n = outs
            .get(1)
            .and_then(|v| v.as_tensor_view::<i32>())
            .and_then(|t| t.iter().next().copied())
            .map_or(rows, |n| (n.max(0) as usize).min(rows));
        let flat: Vec<f32> = dets.iter().copied().collect();
        let mut kept = Vec::new();
        let max_w = width as f32;
        let max_h = height as f32;
        for r in 0..valid_n {
            let row = &flat[r * cols..(r + 1) * cols];
            let score = row[1];
            if score < min_confidence {
                continue;
            }
            kept.push((
                row[0] as i32,
                score,
                row[2].clamp(0.0, max_w),
                row[3].clamp(0.0, max_h),
                row[4].clamp(0.0, max_w),
                row[5].clamp(0.0, max_h),
            ));
        }
        Ok(kept)
    }
}
"""

LAYOUT_PY_PROBE = """//! Temporary PyO3 export so fat LTO retains the full LayoutEngine pipeline.
use std::path::Path;
use pyo3::prelude::*;
use pyropdf_engine::_spike_layout::SpikeLayoutEngine;

#[pyclass(frozen)]
pub struct _SpikeLayoutEngine {
    inner: SpikeLayoutEngine,
}

#[pymethods]
impl _SpikeLayoutEngine {
    #[new]
    fn new(model_path: &str, threads: usize, max_model_size: u64, input_size: usize) -> PyResult<Self> {
        let inner = SpikeLayoutEngine::load(Path::new(model_path), threads, max_model_size, input_size)
            .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
        Ok(Self { inner })
    }

    fn detect_rgba(
        &self,
        rgba: &[u8],
        width: usize,
        height: usize,
        min_confidence: f32,
    ) -> PyResult<Vec<(i32, f32, f32, f32, f32, f32)>> {
        self.inner
            .detect_rgba(rgba, width, height, min_confidence)
            .map_err(pyo3::exceptions::PyRuntimeError::new_err)
    }
}
"""


def run(cmd: list[str], *, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run one subprocess in the repository root."""
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, check=check, env=env)


def restore_workspace() -> None:
    """Revert all temporary workspace modifications."""
    run(["git", "checkout", "--", "Cargo.toml", "Cargo.lock", "crates/"])
    for stray in (
        ROOT / "crates/pyropdf-engine/src/_spike_layout.rs",
        ROOT / "crates/pyropdf-python/src/_spike_layout.rs",
    ):
        stray.unlink(missing_ok=True)


def patch_exports() -> None:
    """Expose `_SpikeLayoutEngine` from `pyropdf-engine` and `pyropdf-python`."""
    eng_lib = ROOT / "crates/pyropdf-engine/src/lib.rs"
    eng_lib.write_text(eng_lib.read_text() + "\npub mod _spike_layout;\n")

    py_lib = ROOT / "crates/pyropdf-python/src/lib.rs"
    src = py_lib.read_text()
    mod_anchor = "mod unrotate;\n"
    if mod_anchor not in src:
        msg = f"anchor {mod_anchor!r} not found in {py_lib}"
        raise RuntimeError(msg)
    src = src.replace(mod_anchor, f"{mod_anchor}mod _spike_layout;\n", 1)

    export_anchor = "    /// The version of pyropdf: the version of its crates.\n"
    if export_anchor not in src:
        msg = f"export anchor {export_anchor!r} not found in {py_lib}"
        raise RuntimeError(msg)
    src = src.replace(
        export_anchor,
        f"    #[pymodule_export]\n    use super::_spike_layout::_SpikeLayoutEngine;\n\n{export_anchor}",
        1,
    )
    py_lib.write_text(src)


def build_wheel(out_dir: Path, *, locked: bool) -> dict[str, Any]:
    """Build one Zopfli-repacked release abi3 wheel."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    cmd = [
        "uvx",
        "--from",
        "maturin==1.16.0",
        "maturin",
        "build",
        "--release",
        "--compression-level",
        "9",
        "--out",
        str(out_dir),
    ]
    if locked:
        cmd.append("--locked")
    res = run(cmd, check=False)
    if res.returncode != 0:
        msg = f"maturin build failed:\n{res.stdout}\n{res.stderr}"
        raise RuntimeError(msg)
    repack = run(
        ["uvx", "--with", "zopflipy==1.14", "python3", "tools/repack_wheel.py", str(out_dir)],
        check=False,
    )
    if repack.returncode != 0:
        msg = f"repack_wheel failed:\n{repack.stdout}\n{repack.stderr}"
        raise RuntimeError(msg)
    return json.loads(repack.stdout)


def build_and_measure(out_dir: Path, baseline_dir: Path, *, locked: bool) -> dict[str, Any]:
    """Build release abi3 wheel, repack with Zopfli, and measure delta against baseline."""
    repack_json = build_wheel(out_dir, locked=locked)
    env = {**os.environ, "PR_BODY": "Wheel growth justification: spike measurement"}
    cmp_res = run(
        ["python3", "tools/wheel_size.py", str(out_dir), "--abi", "abi3", "--base", str(baseline_dir)],
        check=False,
        env=env,
    )
    if not cmp_res.stdout.strip():
        msg = f"wheel_size failed:\n{cmp_res.stderr}"
        raise RuntimeError(msg)
    report = json.loads(cmp_res.stdout)
    report["original_deflate9_bytes"] = repack_json["original"]["size_bytes"]
    report["extension_growth_bytes"] = report["head"]["extension_bytes"] - report["base"]["extension_bytes"]
    return report


def check_audit_and_deps() -> dict[str, Any]:
    """Inspect Cargo.lock package count, duplicate crates, cargo-audit, and wasm32-unknown-emscripten."""
    lock = (ROOT / "Cargo.lock").read_text()
    pkg_count = len(re.findall(r"^\[\[package\]\]", lock, re.MULTILINE))
    dups = run(["cargo", "tree", "-p", "pyropdf-engine", "-d", "--depth", "0"], check=False).stdout.strip().splitlines()
    audit_bin = ROOT / "target/audit-tools/cargo-audit"
    audit_ok = None
    audit_summary = "skipped"
    if audit_bin.exists():
        ares = run([str(audit_bin), "audit", "--deny", "warnings"], check=False)
        audit_ok = ares.returncode == 0
        audit_summary = "clean (0 warnings/advisories)" if audit_ok else (ares.stdout + ares.stderr).strip()[:500]
    wasm = run(
        ["cargo", "check", "-p", "pyropdf-engine", "--target", "wasm32-unknown-emscripten"],
        check=False,
    )
    return {
        "cargo_lock_packages": pkg_count,
        "duplicates_in_engine": [line for line in dups if line and not line.startswith(" ")],
        "cargo_audit_clean": audit_ok,
        "cargo_audit_summary": audit_summary,
        "wasm32_emscripten_ok": wasm.returncode == 0,
    }


def format_markdown(results: dict[str, Any]) -> str:
    """Format the measured wheel-size table and 5-platform 9.0 MiB budget projection."""
    baseline_meta = results["baseline_meta"]
    rten_var = results["variants"]["rten_format_wrapper"]
    onnx_var = results["variants"]["onnx_format_wrapper"]
    base_bytes = rten_var["base"]["size_bytes"]
    lines = [
        "# Measured Core `abi3` Wheel-Size Delta (`macosx_arm64`, Zopfli-1 repacked)",
        "",
        "Baseline built from current `main` with `tools/native-wheel-args.txt` + `tools/repack_wheel.py`.",
        "Both variants wire a live PyO3 `_SpikeLayoutEngine` class in `pyropdf._core` so fat LTO retains",
        "the full model load, 800x800 bilinear preprocessing, RTen inference, and box-extraction pipeline.",
        "",
        (
            "| Variant | Delivered wheel (B) | Wheel growth (B / KiB) | Extension growth (B / KiB) "
            "| `Cargo.lock` pkgs | `cargo-audit` | `wasm32-emscripten` |"
        ),
        "| --- | ---: | ---: | ---: | ---: | --- | --- |",
        (
            f"| Baseline (`main`, `rten_format`) | {base_bytes:,} | 0 (0.0 KiB) | 0 (0.0 KiB) | "
            f"{baseline_meta['cargo_lock_packages']} | {baseline_meta['cargo_audit_summary']} | "
            f"{'OK' if baseline_meta['wasm32_emscripten_ok'] else 'FAIL'} |"
        ),
    ]
    labels = {
        "rten_format_wrapper": "**Recommended**: `LayoutEngine` over existing `rten_format` (0 new crates/features)",
        "onnx_format_wrapper": "Alternative: `LayoutEngine` + `rten` `onnx_format` feature (`rten-onnx`)",
    }
    for key, label in labels.items():
        v = results["variants"][key]
        g = v["growth_bytes"]
        eg = v["extension_growth_bytes"]
        m = v["meta"]
        lines.append(
            f"| {label} | {v['head']['size_bytes']:,} | {g:+,} ({g / 1024:+.1f} KiB) | "
            f"{eg:+,} ({eg / 1024:+.1f} KiB) | {m['cargo_lock_packages']} | {m['cargo_audit_summary']} | "
            f"{'OK' if m['wasm32_emscripten_ok'] else 'FAIL'} |"
        )

    lines += [
        "",
        "## Standalone `rten 0.28.0` `onnx_format` Feature Overhead (`arm64` & `wasm32`)",
        "",
        "| Target | `rten_format` only (raw / zlib-9) | `+onnx_format` delta (raw / zlib-9) |",
        "| --- | ---: | ---: |",
        (
            f"| `aarch64-apple-darwin` | "
            f"{STANDALONE_RTEN_PROBES['aarch64_apple_darwin']['rten_format_raw']:,} B / "
            f"{STANDALONE_RTEN_PROBES['aarch64_apple_darwin']['rten_format_zlib9']:,} B | "
            f"+{STANDALONE_RTEN_PROBES['aarch64_apple_darwin']['onnx_format_delta_raw']:,} B / "
            f"+{STANDALONE_RTEN_PROBES['aarch64_apple_darwin']['onnx_format_delta_zlib9']:,} B "
            f"(+{STANDALONE_RTEN_PROBES['aarch64_apple_darwin']['onnx_format_delta_zlib9'] / 1024:.1f} KiB) |"
        ),
        (
            f"| `wasm32-unknown-unknown` | "
            f"{STANDALONE_RTEN_PROBES['wasm32_unknown_unknown']['rten_format_raw']:,} B / "
            f"{STANDALONE_RTEN_PROBES['wasm32_unknown_unknown']['rten_format_zlib9']:,} B | "
            f"+{STANDALONE_RTEN_PROBES['wasm32_unknown_unknown']['onnx_format_delta_raw']:,} B / "
            f"+{STANDALONE_RTEN_PROBES['wasm32_unknown_unknown']['onnx_format_delta_zlib9']:,} B "
            f"(+{STANDALONE_RTEN_PROBES['wasm32_unknown_unknown']['onnx_format_delta_zlib9'] / 1024:.1f} KiB) |"
        ),
        "",
        "## 5-Platform 9.0 MiB (`9,437,184` B) Native `abi3` Budget Projection",
        "",
        "Anchored to published `v0.4.1` Zopfli-repacked `abi3` wheels (`gh release view v0.4.1`).",
        "",
        (
            "| Target (`v0.4.1`) | Current (B) | Headroom (KiB) "
            "| +`rten_format` wrapper (proj.) | +`onnx_format` (proj.) |"
        ),
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    g_rten = rten_var["growth_bytes"]
    g_onnx = onnx_var["growth_bytes"]

    def fmt_proj(cur: int, delta: int) -> str:
        tot = cur + delta
        rem = CAP_BYTES - tot
        status = "PASS" if rem >= 0 else "**OVER**"
        return f"{tot / MIB:.3f} MiB ({rem / 1024:+.0f} KiB) {status}"

    for target, size in V041_ABI3_WHEELS.items():
        headroom = (CAP_BYTES - size) / 1024
        lines.append(
            f"| `{target}` | {size:,} | {headroom:.1f} KiB | {fmt_proj(size, g_rten)} | {fmt_proj(size, g_onnx)} |"
        )
    return "\n".join(lines) + "\n"


def _log(message: str) -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def main() -> None:
    """Measure baseline, rten_format wrapper, and +onnx_format wrapper on native abi3 wheel."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    baseline_meta = check_audit_and_deps()
    results: dict[str, Any] = {
        "platform": "macosx_arm64",
        "baseline_meta": baseline_meta,
        "standalone_rten_probes": STANDALONE_RTEN_PROBES,
        "v041_abi3_wheels": V041_ABI3_WHEELS,
        "v041_wasm_wheel": V041_WASM_WHEEL,
        "variants": {},
    }

    try:
        restore_workspace()
        if not list(args.baseline.glob("*.whl")):
            _log("Building untouched baseline wheel...")
            build_wheel(args.baseline, locked=True)

        # 1. Recommended: LayoutEngine wrapper reusing pyropdf-engine's existing rten_format feature.
        (ROOT / "crates/pyropdf-engine/src/_spike_layout.rs").write_text(
            '#![allow(clippy::all, dead_code, reason = "temporary spike wheel-size probe")]\n' + LAYOUT_ENGINE_PROBE
        )
        (ROOT / "crates/pyropdf-python/src/_spike_layout.rs").write_text(
            '#![allow(clippy::all, reason = "temporary spike wheel-size probe")]\n' + LAYOUT_PY_PROBE
        )
        patch_exports()
        _log("Building Variant 1 (rten_format wrapper, 0 new crates)...")
        var_rten = build_and_measure(args.work_dir / "rten_format_wrapper", args.baseline, locked=True)
        var_rten["meta"] = check_audit_and_deps()
        results["variants"]["rten_format_wrapper"] = var_rten
        _log(f"rten_format wrapper growth: {var_rten['growth_bytes']:+,} B")

        # 2. Alternative: also enable rten's onnx_format feature in pyropdf-engine.
        eng_toml = ROOT / "crates/pyropdf-engine/Cargo.toml"
        eng_toml.write_text(
            eng_toml.read_text().replace(
                'rten = { version = "0.28.0", default-features = false, features = ["rten_format"] }',
                'rten = { version = "0.28.0", default-features = false, features = ["rten_format", "onnx_format"] }',
                1,
            )
        )
        _log("Building Variant 2 (+onnx_format feature)...")
        var_onnx = build_and_measure(args.work_dir / "onnx_format_wrapper", args.baseline, locked=False)
        var_onnx["meta"] = check_audit_and_deps()
        results["variants"]["onnx_format_wrapper"] = var_onnx
        _log(f"+onnx_format wrapper growth: {var_onnx['growth_bytes']:+,} B")
    finally:
        restore_workspace()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "wheel-delta.json").write_text(json.dumps(results, indent=2) + "\n")
    markdown = format_markdown(results)
    (args.out_dir / "wheel-delta.md").write_text(markdown)
    sys.stdout.write(markdown)


if __name__ == "__main__":
    main()
