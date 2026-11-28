"""Permanent child-process regression suite for the pre-flight nesting guard (#1241).

Verifies that every known `hayro` / `hayro-syntax` unbounded recursion, cycle,
silent truncation, and `#740` exponential inline-image vector refuses cleanly
with ``LimitError`` under ``DocumentLimits.web()`` (zero ``SIGSEGV``, zero
silent truncation), that all real-world corpus PDFs pass with zero false
positives, that F4 per-version memoization reuses validated results across
repeated page operations and invalidates on edits, and that setting
``max_object_depth=None`` opts out.

Keep this child-process regression suite permanently in ``tests/`` even after
the temporary pre-flight guard is removed once upstream ``hayro`` releases #15,
#740, and #1242.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import zlib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import pyropdf
from pyropdf import DocumentLimits, LimitError, _core

if TYPE_CHECKING:
    from collections.abc import Callable

REAL_WORLD = Path(__file__).resolve().parent / "assets" / "real_world"
SUBPROCESS_TIMEOUT_S = 15.0


def _build_pdf(objects: list[bytes]) -> bytes:
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


def _one_page(
    content: bytes,
    resources: bytes = b"<<>>",
    extra: list[bytes] | None = None,
    *,
    compress: bool = False,
) -> bytes:
    """Build a single-page PDF with content stream at obj 4 and extra objects starting at 5."""
    if compress:
        payload = zlib.compress(content, 9)
        content_obj = (
            f"<</Length {len(payload)}/Filter/FlateDecode>>stream\n".encode("ascii") + payload + b"\nendstream"
        )
    else:
        content_obj = f"<</Length {len(content)}>>stream\n".encode("ascii") + content + b"\nendstream"
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Resources " + resources + b"/Contents 4 0 R>>",
        content_obj,
        *(extra or []),
    ]
    return _build_pdf(objects)


def _make_type4_shading(depth: int) -> bytes:
    program = b"{" * depth + b"}" * depth
    func = f"<</FunctionType 4/Domain[0 1]/Range[0 1]/Length {len(program)}>>stream\n".encode("ascii")
    func += program + b"\nendstream"
    shading = b"<</ShadingType 2/ColorSpace/DeviceGray/Coords[0 0 200 0]/Function 6 0 R>>"
    return _one_page(b"/Sh0 sh", b"<</Shading<</Sh0 5 0 R>>>>", [shading, func])


def _make_type4_separation(depth: int) -> bytes:
    program = b"{" * depth + b"}" * depth
    func = f"<</FunctionType 4/Domain[0 1]/Range[0 1]/Length {len(program)}>>stream\n".encode("ascii")
    func += program + b"\nendstream"
    cs = b"[/Separation /Spot /DeviceGray 6 0 R]"
    return _one_page(b"/CS0 cs 0.5 scn 0 0 50 50 re f", b"<</ColorSpace<</CS0 5 0 R>>>>", [cs, func])


def _make_type4_extgstate_tr(depth: int) -> bytes:
    program = b"{" * depth + b"}" * depth
    func = f"<</FunctionType 4/Domain[0 1]/Range[0 1]/Length {len(program)}>>stream\n".encode("ascii")
    func += program + b"\nendstream"
    gs = b"<</Type/ExtGState/TR 6 0 R>>"
    return _one_page(b"/GS0 gs 0 0 50 50 re f", b"<</ExtGState<</GS0 5 0 R>>>>", [gs, func])


def _make_type3_function_cycle() -> bytes:
    shading = b"<</ShadingType 2/ColorSpace/DeviceGray/Coords[0 0 200 0]/Function 6 0 R>>"
    func = b"<</FunctionType 3/Domain[0 1]/Bounds[]/Encode[0 1]/Functions[6 0 R]>>"
    return _one_page(b"/Sh0 sh", b"<</Shading<</Sh0 5 0 R>>>>", [shading, func])


def _make_type3_function_chain(length: int) -> bytes:
    shading = b"<</ShadingType 2/ColorSpace/DeviceGray/Coords[0 0 200 0]/Function 6 0 R>>"
    extra: list[bytes] = [shading]
    extra.extend(
        f"<</FunctionType 3/Domain[0 1]/Bounds[]/Encode[0 1]/Functions[{7 + idx} 0 R]>>".encode("ascii")
        for idx in range(length)
    )
    extra.append(b"<</FunctionType 2/Domain[0 1]/C0[0]/C1[1]/N 1>>")
    return _one_page(b"/Sh0 sh", b"<</Shading<</Sh0 5 0 R>>>>", extra)


def _make_cs_indexed_cycle() -> bytes:
    return _one_page(b"/CS0 cs 0 scn 0 0 50 50 re f", b"<</ColorSpace<</CS0 5 0 R>>>>", [b"[/Indexed 5 0 R 0 <00>]"])


def _make_cs_icc_alternate_cycle() -> bytes:
    icc = b"<</N 3/Alternate 5 0 R/Length 4>>stream\nxxxx\nendstream"
    return _one_page(
        b"/CS0 cs 0 0 0 scn 0 0 50 50 re f",
        b"<</ColorSpace<</CS0 5 0 R>>>>",
        [b"[/ICCBased 6 0 R]", icc],
    )


def _make_cs_indexed_chain(length: int) -> bytes:
    extra = [f"[/Indexed {6 + idx} 0 R 0 <00>]".encode("ascii") for idx in range(length)]
    extra.append(b"/DeviceGray")
    return _one_page(b"/CS0 cs 0 scn 0 0 50 50 re f", b"<</ColorSpace<</CS0 5 0 R>>>>", extra)


def _make_dag_revisited_deeper_cs() -> bytes:
    extra = [f"[/Indexed {6 + idx} 0 R 0 <00>]".encode("ascii") for idx in range(20)]
    extra.append(b"/DeviceGray")
    for idx in range(15):
        target = 27 + idx if idx + 1 < 15 else 5
        extra.append(f"[/Indexed {target} 0 R 0 <00>]".encode("ascii"))
    return _one_page(b"/CS0 cs 0 scn /CS1 cs 0 scn", b"<</ColorSpace<</CS0 5 0 R/CS1 26 0 R>>>>", extra)


def _make_image_smask_cs_chain() -> bytes:
    extra: list[bytes] = [
        (
            b"<</Type/XObject/Subtype/Image/Width 1/Height 1/BitsPerComponent 8"
            b"/ColorSpace/DeviceGray/SMask 6 0 R/Length 1>>stream\n\x00\nendstream"
        ),
        (
            b"<</Type/XObject/Subtype/Image/Width 1/Height 1/BitsPerComponent 8"
            b"/ColorSpace 7 0 R/Length 1>>stream\n\x00\nendstream"
        ),
    ]
    extra.extend(f"[/Indexed {8 + idx} 0 R 0 <00>]".encode("ascii") for idx in range(40))
    extra.append(b"/DeviceGray")
    return _one_page(b"/Im0 Do", b"<</XObject<</Im0 5 0 R>>>>", extra)


def _make_inline_images(count: int) -> bytes:
    return _one_page(b"BI /W 1 /H 1 ID x EI EI " * count, compress=True)


def _make_tiling_pattern_cycle() -> bytes:
    cell = b"/Pattern cs /P0 scn 0 0 10 10 re f"
    pat = (
        b"<</Type/Pattern/PatternType 1/PaintType 1/TilingType 1"
        b"/BBox[0 0 10 10]/XStep 10/YStep 10/Resources<</Pattern<</P0 5 0 R>>>>"
        + f"/Length {len(cell)}>>stream\n".encode("ascii")
        + cell
        + b"\nendstream"
    )
    return _one_page(b"/Pattern cs /P0 scn 0 0 50 50 re f", b"<</Pattern<</P0 5 0 R>>>>", [pat])


def _make_tiling_pattern_chain(length: int) -> bytes:
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
    return _one_page(b"/Pattern cs /P0 scn 0 0 50 50 re f", b"<</Pattern<</P0 5 0 R>>>>", extra)


def _make_type3_glyph_cycle() -> bytes:
    proc = b"100 0 d0 BT /F0 10 Tf (a) Tj ET"
    cp = f"<</Length {len(proc)}>>stream\n".encode("ascii") + proc + b"\nendstream"
    font = (
        b"<</Type/Font/Subtype/Type3/FontBBox[0 0 100 100]/FontMatrix[0.01 0 0 0.01 0 0]"
        b"/CharProcs<</a 6 0 R>>/Encoding<</Type/Encoding/Differences[97 /a]>>"
        b"/FirstChar 97/LastChar 97/Widths[100]/Resources<</Font<</F0 5 0 R>>>>>>"
    )
    return _one_page(b"BT /F0 10 Tf (a) Tj ET", b"<</Font<</F0 5 0 R>>>>", [font, cp])


def _make_type3_glyph_chain(length: int) -> bytes:
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
    return _one_page(b"BT /F0 10 Tf (a) Tj ET", b"<</Font<</F0 5 0 R>>>>", extra)


def _make_form_xobject_cycle() -> bytes:
    body = b"/Fm0 Do"
    form = (
        b"<</Type/XObject/Subtype/Form/BBox[0 0 100 100]"
        b"/Resources<</XObject<</Fm0 5 0 R>>>>"
        + f"/Length {len(body)}>>stream\n".encode("ascii")
        + body
        + b"\nendstream"
    )
    return _one_page(b"/Fm0 Do", b"<</XObject<</Fm0 5 0 R>>>>", [form])


def _make_soft_mask_cycle() -> bytes:
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
    return _one_page(b"/GS0 gs 0 0 50 50 re f", b"<</ExtGState<</GS0 5 0 R>>>>", [gs, group])


def _make_content_operand_depth(depth: int) -> bytes:
    payload = b"[" * depth + b"]" * depth + b" pop BT /F1 12 Tf (after) Tj ET"
    return _one_page(payload, b"<</Font<</F1<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>")


# All 15 adversarial vectors from evidence/preflight-guard/ plus extra trigger paths.
ADVERSARIAL_VECTORS: tuple[tuple[str, Callable[[], bytes], tuple[str, ...]], ...] = (
    ("type4-braces-50000", lambda: _make_type4_shading(50_000), ("text", "pixmap", "content", "svg")),
    ("type4-braces-50000-separation", lambda: _make_type4_separation(50_000), ("text", "pixmap", "content")),
    ("type4-braces-50000-extgstate-tr", lambda: _make_type4_extgstate_tr(50_000), ("text", "pixmap", "content")),
    ("type3-function-cycle", _make_type3_function_cycle, ("text", "pixmap", "content", "svg")),
    ("type3-function-chain-5000", lambda: _make_type3_function_chain(5_000), ("text", "pixmap", "content", "svg")),
    ("cs-indexed-cycle", _make_cs_indexed_cycle, ("text", "pixmap", "content", "svg")),
    ("cs-icc-alternate-cycle", _make_cs_icc_alternate_cycle, ("text", "pixmap", "content", "svg")),
    ("cs-indexed-chain-5000", lambda: _make_cs_indexed_chain(5_000), ("text", "pixmap", "content", "svg")),
    ("dag-revisited-deeper-cs", _make_dag_revisited_deeper_cs, ("text", "pixmap", "content", "svg")),
    ("image-smask-cs-chain-40", _make_image_smask_cs_chain, ("text", "pixmap", "content", "svg")),
    (
        "inline-image-nested-200000",
        lambda: _make_inline_images(200_000),
        ("text", "pixmap", "content", "svg", "operations"),
    ),
    ("tiling-pattern-cycle", _make_tiling_pattern_cycle, ("text", "pixmap", "content", "svg")),
    ("tiling-pattern-chain-2000", lambda: _make_tiling_pattern_chain(2_000), ("text", "pixmap", "content", "svg")),
    ("type3-glyph-cycle", _make_type3_glyph_cycle, ("text", "pixmap", "content", "svg")),
    ("type3-glyph-chain-2000", lambda: _make_type3_glyph_chain(2_000), ("text", "pixmap", "content", "svg")),
    ("form-xobject-cycle", _make_form_xobject_cycle, ("text", "pixmap", "content", "svg")),
    ("soft-mask-cycle", _make_soft_mask_cycle, ("text", "pixmap", "content", "svg")),
    (
        "content-operand-depth-600",
        lambda: _make_content_operand_depth(600),
        ("text", "pixmap", "content", "svg", "operations"),
    ),
)

_CHILD_SCRIPT = """
import json, sys, pyropdf

pdf_path = sys.argv[1]
methods = sys.argv[2:]
results = {}
with pyropdf.open(pdf_path, limits=pyropdf.DocumentLimits.web()) as doc:
    page = doc.pages[0]
    for method in methods:
        try:
            if method == "text":
                page.text()
            elif method == "pixmap":
                page.pixmap()
            elif method == "content":
                page.content()
            elif method == "svg":
                page.svg()
            elif method == "operations":
                _ = page.operations
            else:
                raise ValueError(f"unknown method: {method}")
            results[method] = {"status": "returned"}
        except pyropdf.LimitError as err:
            results[method] = {"status": "limit_error", "code": err.code, "message": str(err)}
        except Exception as err:
            results[method] = {"status": "error", "type": type(err).__name__, "message": str(err)}
sys.stdout.write(json.dumps(results) + "\\n")
"""


def _signal_name(returncode: int) -> str:
    if returncode >= 0:
        return f"exit {returncode}"
    try:
        return signal.Signals(-returncode).name
    except ValueError:
        return f"SIG{-returncode}"


@pytest.mark.parametrize(
    ("name", "builder", "methods"),
    ADVERSARIAL_VECTORS,
    ids=[case[0] for case in ADVERSARIAL_VECTORS],
)
def test_adversarial_vector_refuses_cleanly_in_child_process(
    tmp_path: Path,
    name: str,
    builder: Callable[[], bytes],
    methods: tuple[str, ...],
) -> None:
    """Every adversarial vector must raise LimitError(code='object_depth') with zero SIGSEGV."""
    pdf_path = tmp_path / f"{name}.pdf"
    pdf_path.write_bytes(builder())
    completed = subprocess.run(  # noqa: S603 - fixed local test invocation
        [sys.executable, "-c", _CHILD_SCRIPT, str(pdf_path), *methods],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        check=False,
    )
    assert completed.returncode == 0, (
        f"{name} crashed or failed ({_signal_name(completed.returncode)}): "
        f"stderr={completed.stderr!r} stdout={completed.stdout!r}"
    )
    outcomes = json.loads(completed.stdout.strip().splitlines()[-1])
    for method in methods:
        verdict = outcomes[method]
        assert verdict["status"] == "limit_error", f"{name}.{method}: expected limit_error, got {verdict}"
        assert verdict["code"] == "object_depth", f"{name}.{method}: unexpected code {verdict}"


def test_real_world_corpus_has_zero_false_positives() -> None:
    """All 15 real-world corpus PDFs must pass pre-flight validation with zero false positives."""
    files = sorted(REAL_WORLD.glob("*.pdf"))
    assert len(files) == 15
    limits = DocumentLimits(max_object_depth=64)
    for path in files:
        with pyropdf.open(path, limits=limits) as doc:
            for page in doc.pages:
                # Exercises full page preflight (contents, resources, annotations) + text extraction.
                _ = page.text(unmapped="replacement")
            # Also verify render, svg, content, and operations on page 0 reuse the memo cleanly.
            first = doc.pages[0]
            _ = first.pixmap(dpi=18)
            _ = first.svg()
            _ = first.content()
            _ = first.operations


def _preflight_counts(doc: pyropdf.Document) -> tuple[int, int]:
    return _core.preflight_counts(doc._core())  # noqa: SLF001


def test_f4_preflight_memo_hits_and_edit_invalidation() -> None:
    """Repeated page operations on one Version pay 0 ms via F4 Memo; edits invalidate accurately."""
    two_page_pdf = _build_pdf(
        [
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[3 0 R 4 0 R]/Count 2>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]/Resources<</ColorSpace<</CS0 7 0 R>>>>/Contents 5 0 R>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]/Resources<<>>/Contents 6 0 R>>",
            b"<</Length 3>>stream\nq Q\nendstream",
            b"<</Length 3>>stream\nq Q\nendstream",
            b"[/Indexed 8 0 R 0 <00>]",
            b"/DeviceGray",
        ]
    )
    with pyropdf.open(two_page_pdf, limits=DocumentLimits.web()) as doc:
        p0 = doc.pages[0]
        assert _preflight_counts(doc) == (0, 0)

        _ = p0.text()
        assert _preflight_counts(doc) == (0, 1)

        # Repeated operations across different engines hit the F4 preflight memo without recomputing.
        _ = p0.pixmap(dpi=18)
        _ = p0.svg()
        _ = p0.content()
        hits, computations = _preflight_counts(doc)
        assert computations == 1
        assert hits == 3

        # Editing page 1 does not touch page 0's read set -> page 0 still hits!
        with doc.transaction("edit page 1"):
            stream = doc.objects[6]
            assert isinstance(stream, pyropdf.Stream)
            stream.write(b"q Q q Q")
        _ = doc.pages[0].pixmap(dpi=18)
        hits_after_unrelated, comps_after_unrelated = _preflight_counts(doc)
        assert comps_after_unrelated == 1
        assert hits_after_unrelated == 4

        # Mutating page 0's consulted color-space leaf (8 0 R) into a cycle invalidates and refuses!
        with doc.transaction("make color space cyclic"):
            doc.objects[8] = [pyropdf.Name("Indexed"), doc.objects[7], 0, b"\x00"]
        with pytest.raises(LimitError) as caught:
            doc.pages[0].pixmap(dpi=18)
        assert caught.value.code == "object_depth"
        _, comps_after_edit = _preflight_counts(doc)
        assert comps_after_edit == 2

        # Document-level batch render and text also enforce preflight on the mutated page.
        with pytest.raises(LimitError) as caught_batch:
            doc.render(pages=[0], dpi=18)
        assert caught_batch.value.code == "object_depth"
        with pytest.raises(LimitError) as caught_text:
            doc.text(pages=[0])
        assert caught_text.value.code == "object_depth"


def test_opt_out_when_max_object_depth_is_none() -> None:
    """When DocumentLimits.max_object_depth is None, the pre-flight guard is disabled."""
    # Use a vector that hayro itself caps safely without crashing (Form cycle / 600-deep content).
    form_cycle_pdf = _make_form_xobject_cycle()
    content_600_pdf = _make_content_operand_depth(600)

    with pyropdf.open(form_cycle_pdf, limits=DocumentLimits(max_object_depth=None)) as doc:
        _ = doc.pages[0].text()
        _ = doc.pages[0].pixmap(dpi=18)
        _ = doc.pages[0].svg()
        assert _preflight_counts(doc) == (0, 0)

    with pyropdf.open(content_600_pdf, limits=DocumentLimits(max_object_depth=None)) as doc:
        _ = doc.pages[0].text()
        _ = doc.pages[0].operations
        assert _preflight_counts(doc) == (0, 0)

    # Confirm the exact same PDFs refuse under DocumentLimits.web() (max_object_depth=64).
    with (
        pyropdf.open(form_cycle_pdf, limits=DocumentLimits.web()) as doc,
        pytest.raises(LimitError) as caught_form,
    ):
        doc.pages[0].pixmap(dpi=18)
    assert caught_form.value.code == "object_depth"

    with (
        pyropdf.open(content_600_pdf, limits=DocumentLimits.web()) as doc,
        pytest.raises(LimitError) as caught_content,
    ):
        _ = doc.pages[0].operations
    assert caught_content.value.code == "object_depth"
