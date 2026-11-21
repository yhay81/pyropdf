"""Independent wire fixtures and graph comparisons for standalone PDF objects."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pikepdf
from arlington_samples import SAMPLES, VERSIONS, object_pdf
from assets.broken.generate import pdf

ASSETS = Path(__file__).with_name("assets")

# Native PDF paths, including reciprocal edges, share one real page tree.
COUPLED = {
    "Catalog": 1,
    "PageTreeNodeRoot": 2,
    "PageTreeNode": 3,
    "PageObject": 4,
    "StructTreeRoot": 5,
    "StructElem": 6,
    "DPartRoot": 7,
    "DPart": 8,
    "Thread": 9,
    "BeadFirst": 10,
    "Bead": 11,
}


def coupled_pdf() -> bytes:
    """Reach the page, document parts, tag and article beads through their standard owners."""
    return pdf(
        [
            (
                b"<< /Type /Catalog /Pages 2 0 R /StructTreeRoot 5 0 R /MarkInfo << /Marked true >>"
                b" /DPartRoot 7 0 R /Threads [9 0 R] >>"
            ),
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Pages /Parent 2 0 R /Kids [4 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 3 0 R /MediaBox [0 0 100 100] /Resources << >> /DPart 8 0 R /B [10 0 R 11 0 R] >>",
            b"<< /Type /StructTreeRoot /K [6 0 R] >>",
            b"<< /Type /StructElem /S /Document /P 5 0 R >>",
            b"<< /Type /DPartRoot /DPartRootNode 8 0 R >>",
            b"<< /Type /DPart /Parent 7 0 R /Start 4 0 R /End 4 0 R >>",
            b"<< /Type /Thread /F 10 0 R >>",
            b"<< /Type /Bead /T 9 0 R /P 4 0 R /N 11 0 R /V 11 0 R /R [4 4 14 14] >>",
            b"<< /Type /Bead /P 4 0 R /N 10 0 R /V 10 0 R /R [15 15 25 25] >>",
        ],
        "2.0",
    )


def reachable_sample(name: str) -> tuple[bytes, int]:
    """Root the static example before either reader opens the original bytes."""
    if name in COUPLED:
        return coupled_pdf(), COUPLED[name]
    return object_pdf(SAMPLES[name], VERSIONS[name], catalog_entry=b"/PyroSample 3 0 R"), 3


def graph(reader: pikepdf.Pdf, root: tuple[int, int]) -> dict:
    """Follow qpdf's serialized references, retaining wire types, cycles, generations and raw streams."""
    objects = {}
    pending = [root]
    while pending:
        ref = pending.pop()
        if ref in objects:
            continue
        obj = reader.get_object(ref)
        if obj is None:
            msg = f"missing target {ref}"
            raise AssertionError(msg)
        raw = obj.to_json(dereference=True)
        objects[ref] = (json.loads(raw), obj.read_raw_bytes() if isinstance(obj, pikepdf.Stream) else None)
        # qpdf JSON prefixes PDF string values with u: or b:, so these exact
        # JSON strings can only denote indirect references.
        pending.extend((int(n), int(g)) for n, g in re.findall(rb'"(\d+) (\d+) R"', raw))
    return objects


def packed_pdf(*, linearize: bool = False) -> bytes:
    """Create genuine compressed members, xref data or hint tables using independent qpdf."""
    out = io.BytesIO()
    with pikepdf.open(io.BytesIO(coupled_pdf())) as original:
        mode = pikepdf.ObjectStreamMode.disable if linearize else pikepdf.ObjectStreamMode.generate
        original.save(out, object_stream_mode=mode, linearize=linearize, deterministic_id=True)
    return out.getvalue()
