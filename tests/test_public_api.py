"""Public API surface contracts: exports, ``__module__``, signatures, and generated reference docs."""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, get_args

import pytest

import pyropdf

ROOT = Path(__file__).resolve().parent.parent
API_DOCS = ROOT / "docs" / "en" / "api"

NEW_EXPORTS: dict[str, str] = {
    "DefectKind": "checks-and-profiles.md",
    "ElementKind": "text-and-content.md",
    "LIMIT_CODES": "limits-and-errors.md",
    "LimitCode": "limits-and-errors.md",
    "METADATA_KEYS": "metadata.md",
    "PathInput": "drawing.md",
    "RecolorTarget": "recoloring.md",
    "Timestamper": "signing.md",
}

FORBIDDEN_SIGNATURE_TYPES = re.compile(r"\b(?:CharColumns|PathColumns)\b|(?<!os\.)\bPathLike\b")
FORBIDDEN_DOC_PATTERNS = (
    "issue 153",
    "PDF_ANNOT_",
    "STAMP_",
    "embfile_",
    "TEXT_COLLECT_STYLES",
    "get_text(",
    "cluster_drawings",
    "pymupdf",
    "tests/test_",
    "DESIGN.md section",
)


def test_public_domain_types_and_constants_are_exported() -> None:
    assert len(pyropdf.__all__) == len(set(pyropdf.__all__))
    for name in pyropdf.__all__:
        assert hasattr(pyropdf, name), name

    for name in NEW_EXPORTS:
        assert name in pyropdf.__all__, name
        assert getattr(pyropdf, name) is not None

    assert isinstance(pyropdf.LIMIT_CODES, Mapping)
    assert tuple(pyropdf.LIMIT_CODES) == get_args(pyropdf.LimitCode)
    assert pyropdf.LIMIT_CODES.__doc__
    read_only_codes: Any = pyropdf.LIMIT_CODES
    with pytest.raises(TypeError):
        read_only_codes["file_size"] = "changed"
    assert set(pyropdf.METADATA_KEYS) == {
        "title",
        "author",
        "subject",
        "keywords",
        "creator",
        "producer",
        "created",
        "modified",
        "trapped",
    }
    assert set(get_args(pyropdf.RecolorTarget)) == {"gray", "rgb", "cmyk"}
    assert pyropdf.PathBuilder in get_args(pyropdf.PathInput)


def test_every_exported_class_and_function_reports_top_level_module() -> None:
    bad = [
        f"{name}: {obj.__module__}"
        for name in pyropdf.__all__
        if isinstance(obj := getattr(pyropdf, name), type) or inspect.isfunction(obj)
        if obj.__module__ != "pyropdf"
    ]
    assert bad == []


def _public_callables() -> list[tuple[str, object]]:
    targets: list[tuple[str, object]] = []
    for name in pyropdf.__all__:
        obj = getattr(pyropdf, name)
        if isinstance(obj, type):
            targets.append((name, obj))
            for attr, member in vars(obj).items():
                if attr.startswith("_") and attr != "__init__":
                    continue
                if isinstance(member, classmethod | staticmethod):
                    targets.append((f"{name}.{attr}", member.__func__))
                elif inspect.isfunction(member):
                    targets.append((f"{name}.{attr}", member))
        elif inspect.isroutine(obj):
            targets.append((name, obj))
    return targets


def test_no_public_signature_exposes_private_parameters_or_internal_types() -> None:
    private_params: list[str] = []
    leaked_types: list[str] = []
    for label, target in _public_callables():
        try:
            signature = inspect.signature(target)  # ty: ignore[invalid-argument-type]
        except (TypeError, ValueError):
            continue
        for index, param in enumerate(signature.parameters.values()):
            if index == 0 and param.name in {"self", "cls"}:
                continue
            if param.name.startswith("_") and param.kind not in {
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            }:
                private_params.append(f"{label}({param.name})")
            annotation = str(param.annotation)
            if FORBIDDEN_SIGNATURE_TYPES.search(annotation):
                leaked_types.append(f"{label}({param.name}: {annotation})")
        return_text = str(signature.return_annotation)
        if FORBIDDEN_SIGNATURE_TYPES.search(return_text):
            leaked_types.append(f"{label} -> {return_text}")

    assert private_params == []
    assert leaked_types == []


def test_value_records_and_column_views_hide_private_backing_state() -> None:
    font_a = pyropdf.FontInfo(
        xref=1,
        names=("F1",),
        base_font="Helvetica",
        subtype="Type1",
        encoding="WinAnsiEncoding",
        program=None,
        subset=False,
        forms=(),
    )
    font_b = pyropdf.FontInfo(
        xref=1,
        names=("F1",),
        base_font="Helvetica",
        subtype="Type1",
        encoding="WinAnsiEncoding",
        program=None,
        subset=False,
        forms=(),
    )
    assert font_a == font_b
    assert ", _font=" not in repr(font_a)

    sig_a = pyropdf.Signature(field="Sig1", xref=5, byte_range=(0, 1, 2, 3), revision=0)
    sig_b = pyropdf.Signature(field="Sig1", xref=5, byte_range=(0, 1, 2, 3), revision=0)
    assert sig_a == sig_b
    assert ", _document=" not in repr(sig_a)
    assert not sig_a.verify()

    diagnostics = pyropdf.TableDiagnostics(
        strategy="lines",
        confidence=1.0,
        alignment_error_em=None,
        minimum_gutter_em=None,
        row_gap_variation_em=None,
    )
    rect = pyropdf.Rect(0, 0, 10, 10)
    table_a = pyropdf.Table(bbox=rect, rows=(("A",),), cells=((rect,),), diagnostics=diagnostics)
    table_b = pyropdf.Table(bbox=rect, rows=(("A",),), cells=((rect,),), diagnostics=diagnostics)
    assert table_a == table_b
    assert ", _anchors=" not in repr(table_a)
    assert "A" in table_a.markdown()

    with pytest.raises(TypeError, match=r"Page\.chars"):
        pyropdf.Chars()
    with pytest.raises(TypeError, match=r"Page\.paths"):
        pyropdf.Paths()


def test_generated_api_reference_documents_exports_without_stale_references() -> None:
    pages = sorted(API_DOCS.glob("*.md"))
    assert pages

    index_content = (API_DOCS / "index.md").read_text(encoding="utf-8")
    for name, filename in NEW_EXPORTS.items():
        content = (API_DOCS / filename).read_text(encoding="utf-8")
        heading = f"## `{name}`\n\n"
        assert heading in content, f"{name} missing from {filename}"
        section = content.split(heading, 1)[1].split("\n## ", 1)[0].strip()
        assert "\n\n" in section, f"{name} lacks description in {filename}"
        assert f"- [`{name}`]({filename}#" in index_content, f"{name} missing from index.md"

    violations: list[str] = []
    for page in pages:
        content = page.read_text(encoding="utf-8")
        violations.extend(f"{page.name}: {needle}" for needle in FORBIDDEN_DOC_PATTERNS if needle in content)
    assert violations == []
