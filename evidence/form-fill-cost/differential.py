# ruff: noqa: INP001 - a script, not a package.
"""Emit field, appearance and rollback observations for a form-fill comparison.

Run from the repository root on each implementation and compare the JSON
outputs. The fixture intentionally varies existing and missing appearances,
field values and the input NeedAppearances flag.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import sys
from typing import TYPE_CHECKING

import pikepdf

import pyropdf

if TYPE_CHECKING:
    from collections.abc import Callable


def _appearance(obj: pikepdf.Object) -> object:
    try:
        ap = obj.get("/AP")
        if ap is None:
            return None
        normal = ap.get("/N")
        if isinstance(normal, pikepdf.Stream):
            return ("stream", hashlib.sha256(normal.read_bytes()).hexdigest())
        if isinstance(normal, pikepdf.Dictionary):
            states = []
            for key in sorted(normal.keys()):
                value = normal[key]
                content = (
                    hashlib.sha256(value.read_bytes()).hexdigest() if isinstance(value, pikepdf.Stream) else str(value)
                )
                states.append((str(key), content))
            return ("states", states)
        return str(normal)
    except Exception as error:  # noqa: BLE001 - the oracle records malformed-output failures.
        return ("error", type(error).__name__)


def _snapshot(doc: pyropdf.Document) -> dict[str, object]:
    try:
        fields = [(name, field.type.value, field.value) for name, field in doc.form.items()]
        data = doc.to_bytes()
        with pikepdf.open(io.BytesIO(data)) as pdf:
            acro = pdf.Root.get("/AcroForm")
            need = bool(acro.get("/NeedAppearances", False))
            appearances = [_appearance(pdf.get_object(number, 0)) for number in (5, 6, 7)]
    except Exception as error:  # noqa: BLE001 - compare the public error class and limit code.
        return {"error": type(error).__name__, "code": getattr(error, "code", None)}
    else:
        return {"fields": fields, "need": need, "appearances": appearances}


def _run(case: int, build_pdf: Callable[[dict[int, str]], bytes]) -> list[object]:
    forms = ["/AP << /N 9 0 R >>", "", "/AP << /N 10 0 R >>"]
    values = ["(old)", "<FEFF4E2D>", "null", "[(a) (b)]"]
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Annots [5 0 R 6 0 R 7 0 R] >>",
        4: (
            "<< /Fields [5 0 R 6 0 R 7 0 R] /DA (/Helv 10 Tf 0 g) "
            f"/NeedAppearances {'true' if case & 1 else 'false'} >>"
        ),
        5: f"<< /FT /Tx /T (a) /V (old) /Subtype /Widget /Rect [0 0 50 20] {forms[(case // 2) % 3]} >>",
        6: (
            f"<< /FT /Tx /T (b) /V {values[(case // 6) % 4]} /Subtype /Widget "
            f"/Rect [0 20 50 40] {forms[(case // 24) % 3]} >>"
        ),
        7: (
            f"<< /FT /Tx /T (c) /V {values[(case // 7) % 4]} /Subtype /Widget "
            f"/Rect [0 40 50 60] {forms[(case // 5) % 3]} >>"
        ),
        9: "<< /Length 3 >>\nstream\nq Q\nendstream",
        10: "<< /Length 0 >>\nstream\n\nendstream",
    }
    doc = pyropdf.open(build_pdf(objects))
    field = doc.form["a"]
    other = doc.form["b"].obj
    out: list[object] = []

    def action(label: str, fn: Callable[[], object]) -> None:
        try:
            fn()
        except Exception as error:  # noqa: BLE001 - compare refusals as well as successes.
            refusal = (type(error).__name__, getattr(error, "code", None))
        else:
            refusal = None
        out.append((label, refusal, _snapshot(doc)))

    action("first", lambda: setattr(field, "value", "alpha"))
    action("external", lambda: other.__setitem__("V", pyropdf.String(b"external")))
    action("second", lambda: setattr(field, "value", "beta"))
    action("undo", doc.history.undo)
    action("redo", doc.history.redo)

    def rollback() -> None:
        with contextlib.suppress(RuntimeError), doc.transaction("rollback"):
            field.value = "temporary"
            message = "rollback"
            raise RuntimeError(message)

    action("rollback", rollback)
    action("final", lambda: setattr(field, "value", "gamma"))
    return out


def main() -> None:
    """Print comparable JSON for 144 documents and seven observations each."""
    spec = importlib.util.spec_from_file_location("fixture", "tests/test_acroform.py")
    if spec is None or spec.loader is None:
        msg = "could not load the raw-PDF test fixture"
        raise RuntimeError(msg)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    json.dump([_run(case, fixture.build_raw_pdf) for case in range(144)], sys.stdout, ensure_ascii=True, sort_keys=True)


if __name__ == "__main__":
    main()
