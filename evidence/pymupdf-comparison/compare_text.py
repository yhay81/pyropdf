# ruff: noqa: INP001, T201 - standalone comparison tool.
"""Compare the engines' default page text and word outputs without saving document text.

Run each worker in its own environment, then compare the two results::

    python compare_text.py collect pymupdf /tmp/mupdf.json tests/assets/real_world/*.pdf
    python compare_text.py collect pyropdf /tmp/pyropdf.json tests/assets/real_world/*.pdf
    python compare_text.py compare /tmp/mupdf.json /tmp/pyropdf.json results/text-main.json COMMIT

The workers store only lengths, word counts, and SHA-256 hashes. Text is
compared verbatim and after Unicode NFKC plus whitespace normalization;
words are compared as a sorted multiset after the same normalization. Each
engine uses its default reading options. Matching hashes are not proof of
correctness; differing hashes do not identify which output is better.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import platform
import sys
import unicodedata
import warnings
from pathlib import Path
from typing import Any


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def collect(engine: str, out: str, files: list[str]) -> None:
    """Read each page once and record hashes, counts, or error types."""
    if engine == "pymupdf":
        pymupdf: Any = importlib.import_module("pymupdf")

        version = f"PyMuPDF {pymupdf.__version__} (MuPDF {pymupdf.mupdf_version})"
        opener: Any = pymupdf.open
    elif engine == "pyropdf":
        import pyropdf  # noqa: PLC0415 - independent environment.

        version = f"pyropdf {pyropdf.__version__}"
        opener = pyropdf.open
    else:
        raise ValueError(engine)
    result: dict[str, Any] = {"engine": version, "python": platform.python_version(), "files": {}}
    warnings.simplefilter("ignore")
    for filename in files:
        path = Path(filename)
        row: dict[str, Any] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "pages": []}
        try:
            with opener(path) as doc:
                pages: Any = doc if engine == "pymupdf" else doc.pages
                for page in pages:
                    try:
                        if engine == "pymupdf":
                            text = page.get_text()
                            words = [word[4] for word in page.get_text("words")]
                        else:
                            text = page.text()
                            words = [word.text for word in page.words()]
                        normalized_words = sorted(_normalize(word) for word in words)
                        row["pages"].append(
                            {
                                "text": _hash(text),
                                "normalized_text": _hash(_normalize(text)),
                                "word_multiset": _hash(json.dumps(normalized_words, ensure_ascii=False)),
                                "characters": len(text),
                                "words": len(words),
                            }
                        )
                    except Exception as error:  # noqa: BLE001 - comparison records failures.
                        row["pages"].append({"error": type(error).__name__})
        except Exception as error:  # noqa: BLE001 - compare document-level failures too.
            row["error"] = type(error).__name__
        result["files"][path.name] = row
        print(engine, path.name, len(row["pages"]), flush=True)
    Path(out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def compare(left: str, right: str, out: str, commit: str) -> None:
    """Summarize equal hashes and differing counts on pages read by both engines."""
    first = json.loads(Path(left).read_text(encoding="utf-8"))
    second = json.loads(Path(right).read_text(encoding="utf-8"))
    if first["files"].keys() != second["files"].keys():
        msg = "different document sets"
        raise ValueError(msg)
    rows = {}
    for name, source in first["files"].items():
        target = second["files"][name]
        if source["sha256"] != target["sha256"]:
            msg = f"different input bytes: {name}"
            raise ValueError(msg)
        a, b = source["pages"], target["pages"]
        successful = [(x, y) for x, y in zip(a, b, strict=False) if "error" not in x and "error" not in y]
        rows[name] = {
            "sha256": source["sha256"],
            "pymupdf_pages": len(a),
            "pyropdf_pages": len(b),
            "pymupdf_error": source.get("error"),
            "pyropdf_error": target.get("error"),
            "pymupdf_page_errors": sum("error" in page for page in a),
            "pyropdf_page_errors": sum("error" in page for page in b),
            "compared_pages": len(successful),
            "exact_text_matches": sum(x["text"] == y["text"] for x, y in successful),
            "normalized_text_matches": sum(x["normalized_text"] == y["normalized_text"] for x, y in successful),
            "word_multiset_matches": sum(x["word_multiset"] == y["word_multiset"] for x, y in successful),
            "pymupdf_characters": sum(x["characters"] for x, _ in successful),
            "pyropdf_characters": sum(y["characters"] for _, y in successful),
            "pymupdf_words": sum(x["words"] for x, _ in successful),
            "pyropdf_words": sum(y["words"] for _, y in successful),
        }
    report = {
        "pyropdf_main": commit,
        "pymupdf": first["engine"],
        "pymupdf_python": first["python"],
        "pyropdf": second["engine"],
        "pyropdf_python": second["python"],
        "normalization": "Unicode NFKC, collapse whitespace; sorted multiset for words",
        "files": rows,
    }
    Path(out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    totals = {
        key: sum(row[key] for row in rows.values())
        for key in ("compared_pages", "exact_text_matches", "normalized_text_matches", "word_multiset_matches")
    }
    print(json.dumps(totals))


def main() -> None:
    """Select collection or comparison."""
    mode, *args = sys.argv[1:]
    if mode == "collect":
        engine, out, *files = args
        collect(engine, out, files)
    elif mode == "compare":
        left, right, out, commit = args
        compare(left, right, out, commit)
    else:
        raise ValueError(mode)


if __name__ == "__main__":
    main()
