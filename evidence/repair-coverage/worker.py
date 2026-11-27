# ruff: noqa: INP001, ANN401, BLE001, C901, PLR0915, S603, S607, PLC0415 - isolated worker script across engine venvs.
"""Evaluate one PDF engine on the damaged-PDF benchmark matrix.

Invoked by ``run.py`` inside an isolated Python environment for each engine:

    <engine-python> evidence/repair-coverage/worker.py <engine> <out.jsonl> [--start N]

Writes one JSON record per case to ``<out.jsonl>`` (flushed immediately before
and after each case via ``<out.jsonl>.current``) so a native C crash (SIGABRT /
SIGSEGV) in an external oracle can be attributed to the exact case and resumed.
"""

from __future__ import annotations

import argparse
import io
import json
import platform
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from cases import Case, all_cases  # noqa: E402


def _qpdf_verify(pdf_bytes: bytes, tmp_dir: Path, name: str) -> tuple[bool, bool, int | None, str | None]:
    """Check a saved PDF with qpdf.

    Returns ``(opens_ok, clean_ok, page_count, error)`` where ``opens_ok``
    means qpdf parsed the file without fatal error (exit 0 or 3) and reported
    at least one page, and ``clean_ok`` means ``qpdf --check`` exited 0.
    """
    path = tmp_dir / name
    path.write_bytes(pdf_bytes)
    try:
        npages = subprocess.run(
            ["qpdf", "--show-npages", str(path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        pages = int(npages.stdout.strip()) if npages.returncode in (0, 3) and npages.stdout.strip().isdigit() else None
        check = subprocess.run(
            ["qpdf", "--check", str(path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        opens_ok = check.returncode in (0, 3) and (pages is not None and pages >= 1)
        clean_ok = check.returncode == 0 and (pages is not None and pages >= 1)
        err = None if opens_ok else (check.stderr.strip() or npages.stderr.strip() or f"qpdf exit {check.returncode}")
        return opens_ok, clean_ok, pages, err
    finally:
        path.unlink(missing_ok=True)


def _text_matches(text: str, case: Case) -> bool:
    if case.expected_substring is not None:
        return case.expected_substring in text
    if case.expect_text:
        return bool(text.strip())
    return True


def _eval_one_pyropdf(pyropdf: Any, case: Case, idx: int, tmp_dir: Path) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "open": False,
        "is_repaired": None,
        "page_count": None,
        "pages_ok": False,
        "exact_pages": False,
        "text_ok": False,
        "render_ok": False,
        "full_save_ok": False,
        "full_save_clean": False,
        "incremental_refused": None,
        "crashed": False,
        "error": None,
    }
    errors: list[str] = []
    try:
        with pyropdf.open(case.data) as doc:
            entry["open"] = True
            entry["is_repaired"] = doc.is_repaired
            if doc.is_repaired:
                try:
                    doc.to_bytes(incremental=True)
                    entry["incremental_refused"] = False
                    errors.append("incremental save unexpectedly succeeded on repaired doc")
                except pyropdf.PdfError:
                    entry["incremental_refused"] = True
            try:
                count = len(doc.pages)
                entry["page_count"] = count
                entry["pages_ok"] = count >= 1
                entry["exact_pages"] = count == case.expected_pages
            except Exception as exc:
                errors.append(f"pages: {exc}")
                count = 0
            if count >= 1:
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        text = doc.pages[0].text()
                    entry["text_ok"] = _text_matches(text, case)
                    if not entry["text_ok"]:
                        errors.append(f"text mismatch: got {text[:60]!r}")
                except Exception as exc:
                    errors.append(f"text: {exc}")
                try:
                    pix = doc.pages[0].pixmap(dpi=36)
                    entry["render_ok"] = pix.width > 0 and pix.height > 0 and len(memoryview(pix)) > 0
                except Exception as exc:
                    errors.append(f"render: {exc}")
            try:
                saved = doc.to_bytes()
                with pyropdf.open(saved) as reopened:
                    reopen_ok = (not reopened.is_repaired) and len(reopened.pages) >= 1
                    if not reopen_ok:
                        errors.append(f"reopen: is_repaired={reopened.is_repaired}, pages={len(reopened.pages)}")
                q_ok, q_clean, _q_pages, q_err = _qpdf_verify(saved, tmp_dir, f"pyro_{idx}.pdf")
                entry["full_save_ok"] = reopen_ok and q_ok
                entry["full_save_clean"] = reopen_ok and q_clean
                if not q_ok and q_err:
                    errors.append(f"save-qpdf: {q_err.splitlines()[0]}")
            except Exception as exc:
                errors.append(f"save: {exc}")
    except Exception as exc:
        errors.append(f"open: {exc}")
    entry["error"] = "; ".join(errors) if errors else None
    return entry


def _eval_one_pymupdf(pymupdf: Any, case: Case, idx: int, tmp_dir: Path) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "open": False,
        "is_repaired": None,
        "page_count": None,
        "pages_ok": False,
        "exact_pages": False,
        "text_ok": False,
        "render_ok": False,
        "full_save_ok": False,
        "full_save_clean": False,
        "crashed": False,
        "error": None,
    }
    errors: list[str] = []
    try:
        with pymupdf.open(stream=case.data, filetype="pdf") as doc:
            entry["open"] = True
            entry["is_repaired"] = bool(doc.is_repaired)
            count = doc.page_count
            entry["page_count"] = count
            entry["pages_ok"] = count >= 1
            entry["exact_pages"] = count == case.expected_pages
            if count >= 1:
                try:
                    text = doc[0].get_text()
                    entry["text_ok"] = _text_matches(text, case)
                    if not entry["text_ok"]:
                        errors.append(f"text mismatch: got {text[:60]!r}")
                except Exception as exc:
                    errors.append(f"text: {exc}")
                try:
                    pix = doc[0].get_pixmap(dpi=36)
                    entry["render_ok"] = pix.width > 0 and pix.height > 0 and len(pix.samples) > 0
                except Exception as exc:
                    errors.append(f"render: {exc}")
            try:
                saved = doc.tobytes()
                q_ok, q_clean, _q_pages, q_err = _qpdf_verify(saved, tmp_dir, f"mupdf_{idx}.pdf")
                entry["full_save_ok"] = q_ok
                entry["full_save_clean"] = q_clean
                if not q_ok and q_err:
                    errors.append(f"save-qpdf: {q_err.splitlines()[0]}")
            except Exception as exc:
                errors.append(f"save: {exc}")
    except Exception as exc:
        errors.append(f"open: {exc}")
    entry["error"] = "; ".join(errors) if errors else None
    return entry


def _eval_one_pdfium(pypdfium2: Any, case: Case, idx: int, tmp_dir: Path) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "open": False,
        "is_repaired": None,
        "page_count": None,
        "pages_ok": False,
        "exact_pages": False,
        "text_ok": False,
        "render_ok": False,
        "full_save_ok": False,
        "full_save_clean": False,
        "crashed": False,
        "error": None,
    }
    errors: list[str] = []
    doc = None
    try:
        doc = pypdfium2.PdfDocument(case.data)
        entry["open"] = True
        count = len(doc)
        entry["page_count"] = count
        entry["pages_ok"] = count >= 1
        entry["exact_pages"] = count == case.expected_pages
        if count >= 1:
            page = doc[0]
            try:
                textpage = page.get_textpage()
                text = textpage.get_text_range()
                textpage.close()
                entry["text_ok"] = _text_matches(text, case)
                if not entry["text_ok"]:
                    errors.append(f"text mismatch: got {text[:60]!r}")
            except Exception as exc:
                errors.append(f"text: {exc}")
            try:
                bitmap = page.render(scale=0.5)
                entry["render_ok"] = bitmap.width > 0 and bitmap.height > 0
                bitmap.close()
            except Exception as exc:
                errors.append(f"render: {exc}")
            page.close()
        try:
            buf = io.BytesIO()
            doc.save(buf)
            saved = buf.getvalue()
            q_ok, q_clean, _q_pages, q_err = _qpdf_verify(saved, tmp_dir, f"pdfium_{idx}.pdf")
            entry["full_save_ok"] = q_ok
            entry["full_save_clean"] = q_clean
            if not q_ok and q_err:
                errors.append(f"save-qpdf: {q_err.splitlines()[0]}")
        except Exception as exc:
            errors.append(f"save: {exc}")
    except Exception as exc:
        errors.append(f"open: {exc}")
    finally:
        if doc is not None:
            doc.close()
    entry["error"] = "; ".join(errors) if errors else None
    return entry


def _eval_one_qpdf(_unused: Any, case: Case, idx: int, tmp_dir: Path) -> dict[str, Any]:
    src = tmp_dir / f"in_{idx}.pdf"
    dst = tmp_dir / f"out_{idx}.pdf"
    src.write_bytes(case.data)
    entry: dict[str, Any] = {
        "open": False,
        "is_repaired": None,
        "page_count": None,
        "pages_ok": False,
        "exact_pages": False,
        "text_ok": None,
        "render_ok": None,
        "full_save_ok": False,
        "full_save_clean": False,
        "crashed": False,
        "error": None,
    }
    errors: list[str] = []
    try:
        npages = subprocess.run(
            ["qpdf", "--show-npages", str(src)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if npages.returncode in (0, 3) and npages.stdout.strip().isdigit():
            count = int(npages.stdout.strip())
            entry["open"] = True
            entry["page_count"] = count
            entry["pages_ok"] = count >= 1
            entry["exact_pages"] = count == case.expected_pages
        else:
            first_line = npages.stderr.strip().splitlines()[0] if npages.stderr.strip() else str(npages.returncode)
            errors.append(f"open: {first_line}")
        save = subprocess.run(
            ["qpdf", str(src), str(dst)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if save.returncode in (0, 3) and dst.is_file() and dst.stat().st_size > 0:
            entry["open"] = True
            q_ok, q_clean, _q_pages, q_err = _qpdf_verify(dst.read_bytes(), tmp_dir, f"qchk_{idx}.pdf")
            entry["full_save_ok"] = q_ok
            entry["full_save_clean"] = q_clean
            if not q_ok and q_err:
                errors.append(f"save-check: {q_err.splitlines()[0]}")
        elif save.stderr.strip():
            errors.append(f"save: {save.stderr.strip().splitlines()[0]}")
    finally:
        src.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)
    entry["error"] = "; ".join(errors) if errors else None
    return entry


def _init_engine(engine: str) -> tuple[str, Any, Any]:
    if engine == "pyropdf":
        import pyropdf

        return f"pyropdf {pyropdf.__version__}", pyropdf, _eval_one_pyropdf
    if engine == "pymupdf":
        import pymupdf

        pymupdf.TOOLS.mupdf_display_errors(False)  # noqa: FBT003
        return f"PyMuPDF {pymupdf.__version__}", pymupdf, _eval_one_pymupdf
    if engine == "pdfium":
        import importlib.metadata

        import pypdfium2

        ver = importlib.metadata.version("pypdfium2")
        return f"pypdfium2 {ver}", pypdfium2, _eval_one_pdfium
    if engine == "qpdf":
        ver = subprocess.run(["qpdf", "--version"], capture_output=True, text=True, check=True)
        version_line = ver.stdout.splitlines()[0].strip() if ver.stdout else "qpdf"
        return version_line, None, _eval_one_qpdf
    msg = f"unknown engine: {engine}"
    raise ValueError(msg)


def main() -> None:
    """Evaluate one engine from ``--start`` onward, appending JSONL lines."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", choices=("pyropdf", "pymupdf", "pdfium", "qpdf"))
    parser.add_argument("out", type=Path)
    parser.add_argument("--start", type=int, default=0)
    args = parser.parse_args()

    engine_name, mod, eval_fn = _init_engine(args.engine)
    cases = all_cases()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if args.start == 0 else "a"
    with tempfile.TemporaryDirectory() as tmp, args.out.open(mode, encoding="utf-8") as handle:
        tmp_dir = Path(tmp)
        if args.start == 0:
            header = {"_meta": True, "engine": engine_name, "python": platform.python_version()}
            handle.write(json.dumps(header) + "\n")
            handle.flush()
        for idx in range(args.start, len(cases)):
            case = cases[idx]
            result = eval_fn(mod, case, idx, tmp_dir)
            handle.write(json.dumps({"index": idx, "case_id": case.case_id, "result": result}) + "\n")
            handle.flush()


if __name__ == "__main__":
    main()
