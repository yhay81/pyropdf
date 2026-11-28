"""Child-process and F4 regression suite for pre-flight operator fuel, EI cap, and function bounds (#1275).

Verifies that:
- Exponential Form XObject bombs (20-level binary fan-out, both own and shared
  resources), tiling-pattern tile-copy bombs, SoftMask ``/G`` bombs, visible
  annotation ``/AP /N`` bombs, and Type 3 ``/CharProcs`` bombs refuse cleanly
  with ``LimitError(code="interpretation_fuel")`` before ``hayro`` runs.
- Quadratic inline-image ``BI ... ID <many EI> ... EI`` streams (#17 /
  ``LaurenzV/hayro#1667``) refuse cleanly with ``LimitError(code="interpretation_fuel")``.
- Oversized Type 0 sampled function ``/Size`` products and massive flat Type 4
  PostScript procedures refuse cleanly with ``LimitError(code="interpretation_fuel")``.
- F4 ``PreflightCache`` reuses validated results on unedited versions and
  invalidates accurately when consulted streams or functions are edited.
- All 15 real-world corpus PDFs under ``tests/assets/real_world/`` pass under
  ``DocumentLimits.web()`` with zero false positives.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
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
    annots: bytes | None = None,
) -> bytes:
    """Build a single-page PDF with content stream at obj 4 and extra objects starting at 5."""
    if compress:
        payload = zlib.compress(content, 9)
        content_obj = (
            f"<</Length {len(payload)}/Filter/FlateDecode>>stream\n".encode("ascii") + payload + b"\nendstream"
        )
    else:
        content_obj = f"<</Length {len(content)}>>stream\n".encode("ascii") + content + b"\nendstream"
    annots_entry = b"/Annots " + annots if annots is not None else b""
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Resources " + resources + annots_entry + b"/Contents 4 0 R>>",
        content_obj,
        *(extra or []),
    ]
    return _build_pdf(objects)


def _binary_form_chain(levels: int, start_obj: int, *, own_resources: bool) -> list[bytes]:
    """Return `levels + 1` Form XObject objects forming a binary fan-out DAG (`2^levels` leaves)."""
    extra: list[bytes] = []
    for level in range(levels + 1):
        obj_num = start_obj + level
        body = f"/F{level + 1} Do /F{level + 1} Do".encode("ascii") if level < levels else b"0 0 10 10 re f"
        if own_resources and level < levels:
            res_entry = f"/Resources<</XObject<</F{level + 1} {obj_num + 1} 0 R>>>>".encode("ascii")
        elif own_resources:
            res_entry = b"/Resources<<>>"
        else:
            res_entry = b""
        form = (
            b"<</Type/XObject/Subtype/Form/BBox[0 0 100 100]"
            + res_entry
            + f"/Length {len(body)}>>stream\n".encode("ascii")
            + body
            + b"\nendstream"
        )
        extra.append(form)
    return extra


def _make_binary_form_bomb(levels: int, *, own_resources: bool) -> bytes:
    extra = _binary_form_chain(levels, 5, own_resources=own_resources)
    if own_resources:
        page_res = b"<</XObject<</F0 5 0 R>>>>"
    else:
        entries = b" ".join(f"/F{lvl} {5 + lvl} 0 R".encode("ascii") for lvl in range(levels + 1))
        page_res = b"<</XObject<<" + entries + b">>>>"
    return _one_page(b"/F0 Do", page_res, extra)


def _make_softmask_form_bomb(levels: int) -> bytes:
    # obj 5 = ExtGState, obj 6 = SoftMask /G calling /F0 Do, objs 7.. = binary Form DAG
    forms = _binary_form_chain(levels, 7, own_resources=True)
    sm_body = b"/F0 Do"
    group = (
        b"<</Type/XObject/Subtype/Form/BBox[0 0 100 100]"
        b"/Group<</S/Transparency/CS/DeviceGray>>"
        b"/Resources<</XObject<</F0 7 0 R>>>>"
        + f"/Length {len(sm_body)}>>stream\n".encode("ascii")
        + sm_body
        + b"\nendstream"
    )
    gs = b"<</Type/ExtGState/SMask<</Type/Mask/S/Alpha/G 6 0 R>>>>"
    return _one_page(b"/GS0 gs 0 0 50 50 re f", b"<</ExtGState<</GS0 5 0 R>>>>", [gs, group, *forms])


def _make_annot_ap_form_bomb(levels: int, *, hidden: bool = False) -> bytes:
    # obj 5 = Annot, obj 6 = AP /N calling /F0 Do, objs 7.. = binary Form DAG
    forms = _binary_form_chain(levels, 7, own_resources=True)
    ap_body = b"/F0 Do"
    ap = (
        b"<</Type/XObject/Subtype/Form/BBox[0 0 100 100]"
        b"/Resources<</XObject<</F0 7 0 R>>>>"
        + f"/Length {len(ap_body)}>>stream\n".encode("ascii")
        + ap_body
        + b"\nendstream"
    )
    flags = b"/F 2" if hidden else b""
    annot = b"<</Type/Annot/Subtype/Stamp" + flags + b"/Rect[0 0 50 50]/AP<</N 6 0 R>>>>"
    return _one_page(b"q Q", b"<<>>", [annot, ap, *forms], annots=b"[5 0 R]")


def _make_type3_form_bomb(levels: int) -> bytes:
    # obj 5 = Type3 Font, obj 6 = CharProc /a calling /F0 Do, objs 7.. = binary Form DAG
    forms = _binary_form_chain(levels, 7, own_resources=True)
    proc = b"100 0 d0 /F0 Do"
    cp = f"<</Length {len(proc)}>>stream\n".encode("ascii") + proc + b"\nendstream"
    font = (
        b"<</Type/Font/Subtype/Type3/FontBBox[0 0 100 100]/FontMatrix[0.01 0 0 0.01 0 0]"
        b"/CharProcs<</a 6 0 R>>/Encoding<</Type/Encoding/Differences[97 /a]>>"
        b"/FirstChar 97/LastChar 97/Widths[100]/Resources<</XObject<</F0 7 0 R>>>>>>"
    )
    return _one_page(b"BT /F3 10 Tf (a) Tj ET", b"<</Font<</F3 5 0 R>>>>", [font, cp, *forms])


def _make_tiling_copy_bomb() -> bytes:
    cell = b"0 0 1 1 re f"
    pat = (
        b"<</Type/Pattern/PatternType 1/PaintType 1/TilingType 1"
        b"/BBox[0 0 10000 10000]/XStep 1/YStep 1/Resources<<>>"
        + f"/Length {len(cell)}>>stream\n".encode("ascii")
        + cell
        + b"\nendstream"
    )
    return _one_page(b"/Pattern cs /P0 scn 0 0 50 50 re f", b"<</Pattern<</P0 5 0 R>>>>", [pat])


def _make_type0_function(size: str, range_arr: str, *, nested_in_type3: bool = False) -> bytes:
    t0 = (
        f"<</FunctionType 0/Domain[0 1 0 1]/Range{range_arr}/Size{size}/BitsPerSample 1/Length 1>>stream\n".encode(
            "ascii"
        )
        + b"\x00\nendstream"
    )
    sh = b"<</ShadingType 2/ColorSpace/DeviceGray/Coords[0 0 100 0]/Function 6 0 R>>"
    if nested_in_type3:
        stitch = b"<</FunctionType 3/Domain[0 1]/Bounds[]/Encode[0 1]/Functions[7 0 R]>>"
        t0_1d = (
            f"<</FunctionType 0/Domain[0 1]/Range{range_arr}/Size{size}/BitsPerSample 1/Length 1>>stream\n".encode(
                "ascii"
            )
            + b"\x00\nendstream"
        )
        return _one_page(b"/Sh0 sh", b"<</Shading<</Sh0 5 0 R>>>>", [sh, stitch, t0_1d])
    return _one_page(b"/Sh0 sh", b"<</Shading<</Sh0 5 0 R>>>>", [sh, t0])


def _make_type4_large_ops(pairs: int, *, placement: str = "shading") -> bytes:
    program = b"{" + b" 1 pop" * pairs + b" }"
    func = (
        f"<</FunctionType 4/Domain[0 1]/Range[0 1]/Length {len(program)}>>stream\n".encode("ascii")
        + program
        + b"\nendstream"
    )
    if placement == "shading":
        sh = b"<</ShadingType 2/ColorSpace/DeviceGray/Coords[0 0 100 0]/Function 6 0 R>>"
        return _one_page(b"/Sh0 sh", b"<</Shading<</Sh0 5 0 R>>>>", [sh, func])
    if placement == "separation":
        cs = b"[/Separation /Spot /DeviceGray 6 0 R]"
        return _one_page(b"/CS0 cs 0.5 scn 0 0 50 50 re f", b"<</ColorSpace<</CS0 5 0 R>>>>", [cs, func])
    gs = b"<</Type/ExtGState/TR 6 0 R>>"
    return _one_page(b"/GS0 gs 0 0 50 50 re f", b"<</ExtGState<</GS0 5 0 R>>>>", [gs, func])


FUEL_ADVERSARIAL_VECTORS: tuple[tuple[str, Callable[[], bytes], tuple[str, ...]], ...] = (
    (
        "form-bomb-20-own-res",
        lambda: _make_binary_form_bomb(20, own_resources=True),
        ("text", "pixmap", "content", "svg"),
    ),
    (
        "form-bomb-20-shared-res",
        lambda: _make_binary_form_bomb(20, own_resources=False),
        ("text", "pixmap", "content", "svg"),
    ),
    ("softmask-form-bomb-20", lambda: _make_softmask_form_bomb(20), ("text", "pixmap", "content", "svg")),
    ("annot-ap-form-bomb-20", lambda: _make_annot_ap_form_bomb(20), ("text", "pixmap", "content", "svg")),
    ("type3-glyph-form-bomb-20", lambda: _make_type3_form_bomb(20), ("text", "pixmap", "content", "svg")),
    ("tiling-huge-copies", _make_tiling_copy_bomb, ("text", "pixmap", "content", "svg")),
    (
        "inline-ei-flood-standalone",
        lambda: _one_page(b"BI /W 1 /H 1 ID x " + b"EI " * 200, compress=True),
        ("text", "pixmap", "content", "svg", "operations"),
    ),
    (
        "inline-ei-flood-binary",
        lambda: _one_page(b"BI /W 1 /H 1 ID " + b"\xff EI " * 200 + b"EI", compress=True),
        ("text", "pixmap", "content", "svg", "operations"),
    ),
    (
        "inline-ei-flood-nonws",
        lambda: _one_page(b"BI /W 1 /H 1 ID " + b"aEI " * 200 + b" EI", compress=True),
        ("text", "pixmap", "content", "svg", "operations"),
    ),
    (
        "inline-ei-flood-post-string",
        lambda: _one_page(b"BI /W 1 /H 1 ID x EI " + b"(EI ) Tj " * 200 + b"\xff EI", compress=True),
        ("text", "pixmap", "content", "svg", "operations"),
    ),
    (
        "type0-huge-size",
        lambda: _make_type0_function("[2048 2048]", "[0 1]"),
        ("text", "pixmap", "content", "svg"),
    ),
    (
        "type0-huge-range-product",
        lambda: _make_type0_function("[1024 1024]", "[0 1 0 1 0 1]"),
        ("text", "pixmap", "content", "svg"),
    ),
    (
        "type0-nested-in-type3",
        lambda: _make_type0_function("[5000000]", "[0 1]", nested_in_type3=True),
        ("text", "pixmap", "content", "svg"),
    ),
    (
        "type4-20000-ops-shading",
        lambda: _make_type4_large_ops(10_000, placement="shading"),
        ("text", "pixmap", "content", "svg"),
    ),
    (
        "type4-20000-ops-separation",
        lambda: _make_type4_large_ops(10_000, placement="separation"),
        ("text", "pixmap", "content"),
    ),
    (
        "type4-20000-ops-extgstate-tr",
        lambda: _make_type4_large_ops(10_000, placement="extgstate"),
        ("text", "pixmap", "content"),
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
                page.pixmap(dpi=18)
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
    FUEL_ADVERSARIAL_VECTORS,
    ids=[case[0] for case in FUEL_ADVERSARIAL_VECTORS],
)
def test_fuel_and_bounds_vector_refuses_cleanly_in_child_process(
    tmp_path: Path,
    name: str,
    builder: Callable[[], bytes],
    methods: tuple[str, ...],
) -> None:
    """Every fuel / EI-cap / function-bound adversarial vector refuses with interpretation_fuel."""
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
        assert verdict["code"] == "interpretation_fuel", f"{name}.{method}: unexpected code {verdict}"


def test_binary_form_bomb_refuses_quickly_in_process() -> None:
    """A 20-level binary Form XObject bomb refuses immediately without invoking hayro."""
    bomb_pdf = _make_binary_form_bomb(20, own_resources=True)
    with pyropdf.open(bomb_pdf, limits=DocumentLimits.web()) as doc:
        started = time.perf_counter()
        with pytest.raises(LimitError) as caught:
            doc.pages[0].pixmap(dpi=18)
        elapsed_s = time.perf_counter() - started
        assert caught.value.code == "interpretation_fuel"
        assert elapsed_s < 1.0, f"expected immediate refusal, took {elapsed_s:.3f}s"


def _preflight_counts(doc: pyropdf.Document) -> tuple[int, int]:
    return _core.preflight_counts(doc._core())  # noqa: SLF001


def test_f4_preflight_cache_invalidates_when_edited_into_fuel_or_function_violation() -> None:
    """Editing a consulted stream or Type 4 function into an over-budget payload invalidates the F4 memo."""
    # Start with a safe 4-level binary Form DAG (63 effective ops) + a small Type 4 shading.
    safe_pdf = _make_binary_form_bomb(4, own_resources=True)
    with pyropdf.open(safe_pdf, limits=DocumentLimits.web()) as doc:
        p0 = doc.pages[0]
        _ = p0.text()
        _ = p0.pixmap(dpi=18)
        _ = p0.svg()
        _ = p0.content()
        hits, computations = _preflight_counts(doc)
        assert computations == 1
        assert hits == 3

        # Mutate the page content stream into a quadratic inline-image EI flood.
        with doc.transaction("write quadratic inline-image EI flood"):
            stream = doc.objects[4]
            assert isinstance(stream, pyropdf.Stream)
            stream.write(b"BI /W 1 /H 1 ID " + b"\xff EI " * 200 + b"EI")
        with pytest.raises(LimitError) as caught:
            doc.pages[0].pixmap(dpi=18)
        assert caught.value.code == "interpretation_fuel"
        _, comps_after = _preflight_counts(doc)
        assert comps_after == 2

        with pytest.raises(LimitError) as caught_render:
            doc.render(pages=[0], dpi=18)
        assert caught_render.value.code == "interpretation_fuel"

    # Mutating a consulted Type 4 function stream from valid to oversized also invalidates.
    safe_type4 = _make_type4_large_ops(10, placement="shading")
    with pyropdf.open(safe_type4, limits=DocumentLimits.web()) as doc:
        _ = doc.pages[0].pixmap(dpi=18)
        assert _preflight_counts(doc) == (0, 1)
        with doc.transaction("expand Type 4 procedure"):
            func = doc.objects[6]
            assert isinstance(func, pyropdf.Stream)
            func.write(b"{" + b" 1 pop" * 10_000 + b" }")
        with pytest.raises(LimitError) as caught_t4:
            doc.pages[0].pixmap(dpi=18)
        assert caught_t4.value.code == "interpretation_fuel"
        assert _preflight_counts(doc) == (0, 2)


def test_legitimate_dags_functions_and_hidden_annotations_pass() -> None:
    """Small Form DAGs, normal Type 0/Type 4 functions, and hidden annotations do not false-positive."""
    safe_cases = (
        _make_binary_form_bomb(4, own_resources=True),
        _make_binary_form_bomb(4, own_resources=False),
        _make_type0_function("[17 17]", "[0 1 0 1 0 1]"),
        _make_type4_large_ops(20, placement="shading"),
        _make_annot_ap_form_bomb(20, hidden=True),
    )
    for pdf_bytes in safe_cases:
        with pyropdf.open(pdf_bytes, limits=DocumentLimits.web()) as doc:
            page = doc.pages[0]
            _ = page.text()
            _ = page.pixmap(dpi=18)
            _ = page.svg()
            _ = page.content()


def test_real_world_corpus_has_zero_false_positives_under_web_limits() -> None:
    """All 15 real-world corpus PDFs pass pre-flight fuel/EI/function bounds with zero false positives."""
    files = sorted(REAL_WORLD.glob("*.pdf"))
    assert len(files) == 15
    limits = DocumentLimits.web()
    for path in files:
        with pyropdf.open(path, limits=limits) as doc:
            for page in doc.pages:
                _ = page.text(unmapped="replacement")
            first = doc.pages[0]
            _ = first.pixmap(dpi=18)
            _ = first.svg()
            _ = first.content()
            _ = first.operations
