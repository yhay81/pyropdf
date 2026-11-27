# ruff: noqa: INP001, S603 - standalone reproducible wheel-size measurement script.
"""Measure exact native abi3 wheel-size deltas for #1198 (Option A parley vs Option B harfrust+bidi).

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
GROWTH_LIMIT = MIB // 4

# Published v0.4.1 abi3 release wheels (from `gh release view v0.4.1`).
V041_ABI3_WHEELS: dict[str, int] = {
    "win_amd64": 8_961_169,
    "manylinux_x86_64": 8_900_191,
    "macosx_x86_64": 8_566_157,
    "manylinux_aarch64": 8_324_603,
    "macosx_arm64": 7_735_902,
}
V041_WASM_WHEEL = 6_748_614

PARLEY_ENGINE_PROBE = """//! Temporary #1198 wheel-size probe for Option A (parley 0.13.0).
use parley::{
    Alignment, AlignmentOptions, BaseDirection, FontContext, FontFamily,
    LayoutContext, PositionedLayoutItem, StyleProperty,
};

pub fn spike_parley_layout(
    font_bytes: &[u8],
    text: &str,
    max_width: f32,
    rtl: bool,
    justify: bool,
) -> (f32, f32, Vec<(u32, f32, f32, f32)>) {
    let mut font_cx = FontContext::new();
    let registered = font_cx.collection.register_fonts(font_bytes.to_vec().into(), None);
    let family = registered
        .first()
        .and_then(|(id, _)| font_cx.collection.family_name(*id))
        .map(str::to_owned);
    let mut layout_cx: LayoutContext<u32> = LayoutContext::new();
    let mut builder = layout_cx.ranged_builder(&mut font_cx, text, 1.0, false);
    builder.set_base_direction(if rtl { BaseDirection::Rtl } else { BaseDirection::Auto });
    builder.push_default(StyleProperty::FontSize(12.0));
    if let Some(ref name) = family {
        builder.push_default(StyleProperty::FontFamily(FontFamily::named(name)));
    }
    let mut layout = builder.build(text);
    layout.break_all_lines(Some(max_width));
    layout.set_alignment(
        if justify { Alignment::Justify } else { Alignment::Start },
        AlignmentOptions::default(),
    );
    let mut out = Vec::new();
    for line in layout.lines() {
        for item in line.items() {
            if let PositionedLayoutItem::GlyphRun(run) = item {
                for g in run.glyphs() {
                    out.push((g.id, g.advance, g.x, g.y));
                }
            }
        }
    }
    (layout.width(), layout.height(), out)
}
"""

PARLEY_PY_PROBE = """//! Temporary PyO3 export so fat LTO retains the parley layout pipeline.
use pyo3::prelude::*;

#[pyfunction]
pub fn _spike_rich_text(
    font_bytes: &[u8],
    text: &str,
    max_width: f32,
    rtl: bool,
    justify: bool,
) -> (f32, f32, Vec<(u32, f32, f32, f32)>) {
    pyropdf_engine::_spike_rich_text::spike_parley_layout(font_bytes, text, max_width, rtl, justify)
}
"""

OPTION_B_PY_PROBE = """//! Temporary PyO3 export so fat LTO retains the Option B rich-text layout pipeline.
use std::sync::Arc;
use pyo3::prelude::*;
use pyropdf_engine::_spike_rich_text::{Align, BaseDirection, RichFace, SpanInput, layout_rich_text};
use pyropdf_store::budget::Budget;

#[pyfunction]
pub fn _spike_rich_text(
    font_bytes: &[u8],
    spans: Vec<(String, f64, bool, bool, Option<u32>)>,
    width: f64,
    height: f64,
    rtl: bool,
    justify: bool,
) -> PyResult<(f64, f64, usize, usize, usize)> {
    let face = RichFace::from_opentype(Arc::new(font_bytes.to_vec()), 0)
        .map_err(pyo3::exceptions::PyValueError::new_err)?;
    let faces = [face];
    let inputs: Vec<SpanInput<'_>> = spans
        .iter()
        .map(|(t, sz, ul, st, lk)| SpanInput {
            text: t.as_str(),
            face_index: 0,
            size: *sz,
            underline: *ul,
            strikethrough: *st,
            link_id: *lk,
        })
        .collect();
    let dir = if rtl { BaseDirection::Rtl } else { BaseDirection::Auto };
    let align = if justify { Align::Justify } else { Align::Start };
    let budget = Budget::default();
    let layout = layout_rich_text(&faces, &inputs, (width, height), dir, align, None, &budget)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("{e:?}")))?;
    Ok((layout.height, layout.spare_height, layout.lines.len(), layout.total_glyphs, layout.links.len()))
}
"""

PARLEY_PROFILE_Z = """
[profile.release.package.parley]
opt-level = "z"
[profile.release.package.parley_engine]
opt-level = "z"
[profile.release.package.parley_data]
opt-level = "z"
[profile.release.package.parley_emoji]
opt-level = "z"
[profile.release.package.fontique]
opt-level = "z"
[profile.release.package.parlance]
opt-level = "z"
[profile.release.package.icu_properties]
opt-level = "z"
[profile.release.package.icu_properties_data]
opt-level = "z"
[profile.release.package.icu_normalizer]
opt-level = "z"
[profile.release.package.icu_normalizer_data]
opt-level = "z"
[profile.release.package.icu_segmenter]
opt-level = "z"
[profile.release.package.icu_segmenter_data]
opt-level = "z"
[profile.release.package.icu_locale_core]
opt-level = "z"
[profile.release.package.icu_locale_fallback]
opt-level = "z"
[profile.release.package.icu_locale_fallback_data]
opt-level = "z"
[profile.release.package.icu_collections]
opt-level = "z"
[profile.release.package.icu_provider]
opt-level = "z"
"""


def run(cmd: list[str], *, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run one subprocess in the repository root."""
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, check=check, env=env)


def restore_workspace() -> None:
    """Revert all temporary workspace modifications."""
    run(["git", "checkout", "--", "Cargo.toml", "Cargo.lock", "crates/"])
    for stray in (
        ROOT / "crates/pyropdf-engine/src/_spike_rich_text.rs",
        ROOT / "crates/pyropdf-python/src/_spike_rich_text.rs",
    ):
        stray.unlink(missing_ok=True)


def patch_exports() -> None:
    """Expose `_spike_rich_text` from `pyropdf-engine` and `pyropdf-python`."""
    eng_lib = ROOT / "crates/pyropdf-engine/src/lib.rs"
    eng_lib.write_text(eng_lib.read_text() + "\npub mod _spike_rich_text;\n")

    py_lib = ROOT / "crates/pyropdf-python/src/lib.rs"
    src = py_lib.read_text()
    src = src.replace("mod unrotate;\n", "mod unrotate;\nmod _spike_rich_text;\n", 1)
    src = src.replace(
        "    #[pymodule_export]\n    use super::ocr::{_OcrEngine, insert_ocr_layer};",
        "    #[pymodule_export]\n    use super::ocr::{_OcrEngine, insert_ocr_layer};\n"
        "    #[pymodule_export]\n    use super::_spike_rich_text::_spike_rich_text;",
        1,
    )
    py_lib.write_text(src)


def build_and_measure(out_dir: Path, baseline_dir: Path, *, locked: bool) -> dict[str, Any]:
    """Build release abi3 wheel, repack with Zopfli, and measure delta against baseline."""
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
    repack_json = json.loads(repack.stdout)
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
    base_bytes = results["variants"]["option_b_harfrust_bidi"]["base"]["size_bytes"]
    lines = [
        "# Measured Native `abi3` Wheel-Size Delta (`macosx_arm64`, Zopfli-1 repacked)",
        "",
        "Baseline built from current `main` with `tools/native-wheel-args.txt` + `tools/repack_wheel.py`.",
        "Every variant wires a live PyO3 export so fat LTO retains the full layout pipeline.",
        "",
        (
            "| Variant | Delivered wheel (B) | Wheel growth (B / KiB) | Extension growth (B / KiB) "
            "| `Cargo.lock` pkgs | `cargo-audit` | `wasm32-emscripten` |"
        ),
        "| --- | ---: | ---: | ---: | ---: | --- | --- |",
        (
            f"| Baseline (`main`) | {base_bytes:,} | 0 (0.0 KiB) | 0 (0.0 KiB) | "
            f"{baseline_meta['cargo_lock_packages']} | {baseline_meta['cargo_audit_summary']} | "
            f"{'OK' if baseline_meta['wasm32_emscripten_ok'] else 'FAIL'} |"
        ),
    ]
    labels = {
        "option_b_harfrust_bidi": "Option B (`harfrust 0.16` + `unicode-bidi`, 0 new crates)",
        "option_a1_parley_std": 'Option A1 (`parley 0.13` `std`, `opt-level="z"`)',
        "option_a2_parley_complex_scripts": 'Option A2 (`parley 0.13` `std+complex-scripts`, `opt-level="z"`)',
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
        "## 5-Platform 9.0 MiB (`9,437,184` B) Native `abi3` Budget Projection",
        "",
        "Anchored to published `v0.4.1` Zopfli-repacked `abi3` wheels (`gh release view v0.4.1`).",
        "Because `x86_64` wheels compress ~5–15% larger than `arm64` for identical code/data, applying the",
        "`macosx_arm64` byte delta directly is a **conservative lower bound** for `win_amd64` and `manylinux_x86_64`.",
        "",
        (
            "| Target (`v0.4.1`) | Current (B) | Headroom (KiB) | +Option B (proj.) "
            "| +Option A1 `parley std` (proj.) | +Option A2 `+complex-scripts` (proj.) |"
        ),
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    g_b = results["variants"]["option_b_harfrust_bidi"]["growth_bytes"]
    g_a1 = results["variants"]["option_a1_parley_std"]["growth_bytes"]
    g_a2 = results["variants"]["option_a2_parley_complex_scripts"]["growth_bytes"]

    def fmt_proj(cur: int, delta: int) -> str:
        tot = cur + delta
        rem = CAP_BYTES - tot
        status = "PASS" if rem >= 0 else "**OVER**"
        return f"{tot / MIB:.3f} MiB ({rem / 1024:+.0f} KiB) {status}"

    for target, size in V041_ABI3_WHEELS.items():
        headroom = (CAP_BYTES - size) / 1024
        lines.append(
            f"| `{target}` | {size:,} | {headroom:.1f} KiB | "
            f"{fmt_proj(size, g_b)} | {fmt_proj(size, g_a1)} | {fmt_proj(size, g_a2)} |"
        )
    return "\n".join(lines) + "\n"


def _log(message: str) -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def main() -> None:
    """Measure Option B, Option A1 (parley std), and Option A2 (parley +complex-scripts)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    baseline_meta = check_audit_and_deps()
    results: dict[str, Any] = {
        "platform": "macosx_arm64",
        "baseline_meta": baseline_meta,
        "v041_abi3_wheels": V041_ABI3_WHEELS,
        "v041_wasm_wheel": V041_WASM_WHEEL,
        "variants": {},
    }

    try:
        # 1. Option B: bounded harfrust + unicode-bidi + unicode-linebreak + unicode-segmentation (0 new crates).
        restore_workspace()
        src_b = (ROOT / "evidence/canvas-rich-text/src/bidi_rich.rs").read_text()
        (ROOT / "crates/pyropdf-engine/src/_spike_rich_text.rs").write_text(
            '#![allow(clippy::all, dead_code, reason = "temporary spike wheel-size probe")]\n' + src_b
        )
        (ROOT / "crates/pyropdf-python/src/_spike_rich_text.rs").write_text(
            '#![allow(clippy::all, reason = "temporary spike wheel-size probe")]\n' + OPTION_B_PY_PROBE
        )
        patch_exports()
        _log("Building Option B (harfrust + unicode-bidi)...")
        opt_b = build_and_measure(args.work_dir / "option_b", args.baseline, locked=True)
        opt_b["meta"] = check_audit_and_deps()
        results["variants"]["option_b_harfrust_bidi"] = opt_b
        _log(f"Option B growth: {opt_b['growth_bytes']:+,} B")

        # 2. Option A1: parley 0.13.0 (default-features = false, features = ["std"], opt-level = "z").
        restore_workspace()
        eng_toml = ROOT / "crates/pyropdf-engine/Cargo.toml"
        eng_toml.write_text(
            eng_toml.read_text().replace(
                'harfrust = "0.16.0"\n',
                'harfrust = "0.16.0"\nparley = { version = "0.13.0", default-features = false, features = ["std"] }\n',
                1,
            )
        )
        root_toml = ROOT / "Cargo.toml"
        root_toml.write_text(root_toml.read_text() + PARLEY_PROFILE_Z)
        (ROOT / "crates/pyropdf-engine/src/_spike_rich_text.rs").write_text(
            '#![allow(clippy::all, reason = "temporary spike wheel-size probe")]\n' + PARLEY_ENGINE_PROBE
        )
        (ROOT / "crates/pyropdf-python/src/_spike_rich_text.rs").write_text(
            '#![allow(clippy::all, reason = "temporary spike wheel-size probe")]\n' + PARLEY_PY_PROBE
        )
        patch_exports()
        _log("Building Option A1 (parley std, opt-level=z)...")
        opt_a1 = build_and_measure(args.work_dir / "option_a1", args.baseline, locked=False)
        opt_a1["meta"] = check_audit_and_deps()
        results["variants"]["option_a1_parley_std"] = opt_a1
        _log(f"Option A1 growth: {opt_a1['growth_bytes']:+,} B")

        # 3. Option A2: parley 0.13.0 (default-features = false, features = ["std", "complex-scripts"]).
        eng_toml.write_text(
            eng_toml.read_text().replace(
                'features = ["std"]',
                'features = ["std", "complex-scripts"]',
                1,
            )
        )
        _log("Building Option A2 (parley std+complex-scripts, opt-level=z)...")
        opt_a2 = build_and_measure(args.work_dir / "option_a2", args.baseline, locked=False)
        opt_a2["meta"] = check_audit_and_deps()
        results["variants"]["option_a2_parley_complex_scripts"] = opt_a2
        _log(f"Option A2 growth: {opt_a2['growth_bytes']:+,} B")
    finally:
        restore_workspace()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "wheel-delta.json").write_text(json.dumps(results, indent=2) + "\n")
    markdown = format_markdown(results)
    (args.out_dir / "wheel-delta.md").write_text(markdown)
    sys.stdout.write(markdown)


if __name__ == "__main__":
    main()
