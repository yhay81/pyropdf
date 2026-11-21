"""Fixed standalone Arlington objects shared by authoring and preservation tests."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from assets.broken.generate import pdf

_FIXTURE = Path(__file__).with_name("fixtures") / "arlington-authoring.tsv"
with _FIXTURE.open(encoding="utf-8", newline="") as source:
    _ROWS = list(csv.DictReader(source, delimiter="\t"))
SAMPLES: dict[str, list[str]] = {row["Object"]: json.loads(row["PDFObjects"]) for row in _ROWS}
VERSIONS = {row["Object"]: row["Version"] for row in _ROWS}


def object_pdf(objects: list[str], version: str = "2.0") -> bytes:
    """Build one PDF with its representative object at 3 and related objects immediately after it."""
    page = len(objects) + 3
    beads = " ".join(f"{i} 0 R" for i, obj in enumerate(objects, 3) if re.search(r"/Type /Bead(?:\s|>)", obj))
    return pdf(
        [
            (
                b"<< /Type /Catalog /Pages 2 0 R /Sentinel (unchanged) /Extensions <<"
                b" /AAPL << >> /OpenOffice << >> /GTSm [<< /BaseVersion /2.0 /ExtensionLevel 1 >>]"
                b" /ADBE << /BaseVersion /1.7 /ExtensionLevel 8 >>"
                b" /ISO_19593 << /BaseVersion /1.7 /ExtensionLevel 1 >> /ISO_ ["
                b"<< /BaseVersion /2.0 /ExtensionLevel 24064 >> << /BaseVersion /2.0 /ExtensionLevel 32002 >>"
                b" << /BaseVersion /2.0 /ExtensionLevel 32004 >>] >> >>"
            ),
            f"<< /Type /Pages /Kids [{page} 0 R] /Count 1 >>".encode(),
            *(obj.encode() for obj in objects),
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << >> /B [{beads}] >>".encode(),
        ],
        version,
    )


def sample_pdf(name: str) -> bytes:
    """Return the complete PDF for a fixed standalone descriptor case."""
    return object_pdf(SAMPLES[name], VERSIONS[name])
