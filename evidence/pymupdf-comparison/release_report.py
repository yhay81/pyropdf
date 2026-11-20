# ruff: noqa: INP001 - report for the development-only comparison.
"""Render planned workloads against their observations, retaining uncovered cells in the denominator."""

from __future__ import annotations

from collections import Counter

REPO = "https://github.com/yhay81/pyropdf"
ENGINES = ("pymupdf", "pyropdf")


def timing_rows(raw: dict) -> list[str]:
    """Show the best timings and complete-pair totals for every declared operation."""
    names = raw["corpus"]
    rows = ["| Document | Operation | PyMuPDF ms | pyropdf ms | Ratio |", "| --- | --- | ---: | ---: | ---: |"]
    totals = ["| Operation | Complete pairs | PyMuPDF ms | pyropdf ms | Ratio |", "| --- | ---: | ---: | ---: | ---: |"]
    for op in raw["settings"]["operations"]:
        pairs = []
        for name in names:
            values, cells = [], []
            for engine in ENGINES:
                obs = raw["engines"][engine]["timing"].get(name, {}).get(op, {"status": "unmeasured"})
                if obs["status"] == "ok":
                    value = min(obs["samples_ms"])
                    values.append(value)
                    cells.append(f"{value:.3f}")
                else:
                    cells.append(obs["status"] + " " + obs.get("reason", "missing observation").replace("|", "\\|"))
            ratio = "—"
            if len(values) == len(ENGINES):
                pairs.append(values)
                ratio = f"{values[1] / values[0]:.2f}"
            rows.append(f"| {name} | {op} | {' | '.join(cells)} | {ratio} |")
        a, b = (sum(p[index] for p in pairs) for index in range(len(ENGINES)))
        ratio = f"{b / a:.2f}" if pairs else "—"
        totals.append(f"| {op} | {len(pairs)}/{len(names)} | {a:.3f} | {b:.3f} | {ratio} |")
    return [*totals, "", "### Individual documents", "", *rows]


def quality_rows(raw: dict) -> list[str]:
    """Count each verified label and render covered, missing and worse-scoring cases."""
    rows = [
        "| Page | Engine | Anchors | Ordered pairs | Placed words | Mean ink IoU | Table cells |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for engine in ENGINES:
        total = Counter()
        for name in raw["labels"]:
            result = raw["engines"][engine]["quality"].get(
                name, {"status": "unmeasured", "reason": "missing observation"}
            )
            if result["status"] != "ok":
                rows.append(f"| {name} | {engine} | {result['status']}: {result['reason']} | — | — | — | — |")
                continue
            a, w, t = result["anchors"], result["words"], result.get("table", {})
            total.update({k: a[k] for k in ("found", "anchors", "in_order", "pairs")})
            total.update({k: w[k] for k in ("placed", "words")})
            total.update({k: t[k] for k in ("cells", "cells_right") if k in t})
            total["iou_sum"] += (w["mean_iou"] or 0) * w["words"]
            total["pages"] += 1
            iou = "—" if w["mean_iou"] is None else f"{w['mean_iou']:.3f}"
            table = f"{t['cells_right']}/{t['cells']}" if t else "—"
            rows.append(
                f"| {name} | {engine} | {a['found']}/{a['anchors']} | {a['in_order']}/{a['pairs']} | "
                f"{w['placed']}/{w['words']} | {iou} | {table} |"
            )
        iou = f"{total['iou_sum'] / total['words']:.3f}" if total["words"] else "—"
        rows.append(
            f"| **Total ({total['pages']}/{len(raw['labels'])} pages)** | {engine} | "
            f"{total['found']}/{total['anchors']} | {total['in_order']}/{total['pairs']} | "
            f"{total['placed']}/{total['words']} | {iou} | {total['cells_right']}/{total['cells']} |"
        )
    return rows


def capability_rows(raw: dict, tasks: list[dict]) -> list[str]:
    """Link evidence to the source of the measured artifact."""
    base = f"{REPO}/blob/{raw['release_revision']}"
    lines = ["| Task | PyMuPDF | pyropdf | Release tests and remaining work |", "| --- | --- | --- | --- |"]
    for task in tasks:
        tests = task["Release tests"]
        evidence = [] if tests == "-" else [f"[{t}]({base}/tests/test_{t}.py)" for t in tests.split(",")]
        gap = task["Gap"]
        if gap != "-":
            number, why = gap.split(": ", 1)
            evidence.append(f"[#{number}]({REPO}/issues/{number}) {why}")
        lines.append(
            f"| {task['Task']} | {task['PyMuPDF task']} | {task['Canonical pyropdf task and scope']} | "
            f"{', '.join(evidence)} |"
        )
    return lines


def render(raw: dict, tasks: list[dict]) -> str:
    """Generate the standalone release comparison from the archived measurements."""
    art = raw["artifacts"]
    base = f"{REPO}/blob/{raw['corpus_revision']}"
    oracle = art["pymupdf"]["version"]
    previous_option = " --previous-report previous-observations.json" if raw["previous_release"] else ""
    lines = [
        f"# pyropdf {raw['release']} and PyMuPDF {oracle}",
        "",
        (
            f"Measured on {raw['platform']}, Python {raw['python']}. "
            f"Corpus and label revision [`{raw['corpus_revision'][:12]}`]({base}/tests/assets/real_world). "
            f"[Release specification map]({REPO}/blob/{raw['release_revision']}/docs/spec-coverage.md)."
        ),
        "",
        "| Wheel | SHA-256 |",
        "| --- | --- |",
        *(f"| `{a['wheel']}` | `{a['sha256']}` |" for a in art.values()),
        "",
        "Run from the evidence checkout, with these wheels in the current directory:",
        "",
        "```sh",
        (
            f"python3.13 evidence/pymupdf-comparison/release.py {raw['release']} "
            f"{art['pyropdf']['wheel']} {art['pymupdf']['wheel']} comparison{previous_option}"
        ),
        "```",
        "",
        "## Timings",
        "",
        (
            f"Best of {raw['settings']['runs']} samples (ms), on fresh documents. Operations open a document, read all "
            f"pages, search for `{raw['settings']['search']}`, or save once. Rendering reads raw samples "
            "at the named DPI. The ratio is pyropdf / PyMuPDF. Values above 1 are slower. Totals sum the best times "
            "only for paired successes and give the denominator. Run-to-run noise matters for short operations."
        ),
        "",
        *timing_rows(raw),
        "",
        "## Reading quality",
        "",
        (
            "Both engines use default order. Only human-checked pages score. Adjacent anchor pairs measure order "
            "among found anchors. Word placement covers at least 90% of the labeled ink. IoU measures box conventions. "
            "The aggregate IoU weights each labeled word once. Table cells come from transcribed rows. "
            "Labels and their review are tracked in [#430](https://github.com/yhay81/pyropdf/issues/430) "
            "and [#754](https://github.com/yhay81/pyropdf/issues/754)."
        ),
        "",
        *quality_rows(raw),
        "",
        (
            "Peak memory, pixel quality and speed on other platforms were unmeasured in this run. "
            "[Rendering investigations](https://github.com/yhay81/pyropdf/issues/365) use other artifacts."
        ),
        "",
        "## Oracle delta",
        "",
        (
            f"Compared with PyMuPDF {raw['previous_baseline']}, {len(raw['surface_delta'])} public names, signatures "
            f"or property kinds changed in the {raw['surface_count']} inspected entries of 14 PDF-facing classes. "
            "The previous surface and the added, removed or changed entries are machine-readable. "
            "These checks cover the audited task matrix and classes, not the entire MuPDF binding."
        ),
        "",
        (
            f"The [upstream release notes](https://github.com/pymupdf/PyMuPDF/releases/tag/{oracle}) record two PDF "
            "behavior fixes and two other changes: page placement documentation and lazy initialization. "
            "Import time was unmeasured. Probes compare decoded label prefixes and scale-dependent glyph heights."
        ),
        "",
        "| Baseline or artifact | Escaped and UTF-16 label prefixes | Glyph height follows the Y scale |",
        "| --- | --- | --- |",
    ]
    if raw["previous_release"]:
        previous = raw["previous_release"]
        lines[lines.index("## Oracle delta") + 2] += (
            f" The previous observations came from [{previous}]({REPO}/releases/tag/{previous}) "
            f"(SHA-256 `{raw['previous_report_sha256']}`)."
        )
    for engine, probes in [
        (f"PyMuPDF {raw['previous_baseline']}", raw["previous_probes"]),
        *((e, raw["engines"][e]["probes"]) for e in ENGINES),
    ]:
        if "error" in probes:
            lines.append(f"| {engine} | failed: {probes['error']} | failed |")
        else:
            marks = [
                "pass" if item["passed"] else "fail"
                for item in (probes["page_label_prefixes"], probes["glyph_height_under_scale"])
            ]
            lines.append(f"| {engine} | {' | '.join(marks)} |")
    lines += [
        "",
        "## Task-level capabilities",
        "",
        (
            "Each linked test describes the release's API contract. Optional model data and fonts are outside "
            "the core wheel. The measured workloads above exercise a subset of these tasks."
        ),
        "",
        *capability_rows(raw, tasks),
        "",
        (
            "The accompanying `observations.json` holds every timing sample, exception, corpus/label hash, installed "
            "version, API delta and quality score. The two `*-pages.json.gz` files contain the original extracted "
            "text, word boxes and table cells. A failed extraction retains its diagnostic in the observations."
        ),
    ]
    return "\n".join(lines) + "\n"
