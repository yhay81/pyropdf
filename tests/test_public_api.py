"""Public API surface contracts: exports, ``__module__``, signatures, and generated reference docs."""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping, Sequence
from dataclasses import FrozenInstanceError, is_dataclass
from pathlib import Path
from typing import Any, assert_type, get_args, get_overloads, get_type_hints

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


# Specifications, path commands, and color values accept the object or operands
# they describe. A result record names every field. This list records the public
# positional contracts used by the drawing, form, navigation and editing guides.
_POSITIONAL_VALUES = frozenset(
    [
        "BeadSpecification",
        "Border",
        "Caret",
        "CheckBox",
        "Circle",
        "CollectionField",
        "CollectionFolder",
        "CollectionSortKey",
        "CollectionSplit",
        "CollectionSubitem",
        "Color",
        "ColorStop",
        "ComboBox",
        "CoordinateSystem",
        "CurveTo",
        "Destination",
        "DeveloperExtension",
        "DocumentPartAction",
        "DocumentRequirement",
        "EncryptedPayload",
        "FieldChoice",
        "FieldLock",
        "FileAttachment",
        "FreeText",
        "GeospatialMeasure",
        "HideAction",
        "Highlight",
        "ImportDataAction",
        "Ink",
        "InlineImage",
        "JavaScriptAction",
        "Launch",
        "LayerMembership",
        "LayerStateChange",
        "LineAnnotation",
        "LineTo",
        "ListBox",
        "MoveTo",
        "NamedAction",
        "Navigator",
        "Note",
        "NumberFormat",
        "Operation",
        "OutlineEntry",
        "PageLabelRange",
        "PieceData",
        "PointData",
        "PolyLine",
        "Polygon",
        "PushButton",
        "RadioButton",
        "RectilinearMeasure",
        "RedactionMark",
        "RemoteDestination",
        "RequirementHandler",
        "ResetFormAction",
        "SetLayerStateAction",
        "SignatureField",
        "Square",
        "Squiggly",
        "Stamp",
        "StrikeOut",
        "SubmitFormAction",
        "TextField",
        "ThreadAction",
        "TransitionAction",
        "Underline",
        "Viewport",
    ]
)


def test_exported_dataclasses_distinguish_record_fields_from_positional_operands() -> None:
    positional = set()
    for name in pyropdf.__all__:
        cls = getattr(pyropdf, name)
        if not isinstance(cls, type) or not is_dataclass(cls):
            continue
        assert cls.__dataclass_params__.frozen, name
        assert "__slots__" in vars(cls), name
        if any(param.kind != inspect.Parameter.KEYWORD_ONLY for param in inspect.signature(cls).parameters.values()):
            positional.add(name)
    assert positional == _POSITIONAL_VALUES


def test_live_views_follow_page_and_field_objects_through_edits_and_undo() -> None:
    doc = pyropdf.Document()
    pages, form = doc.pages, doc.form
    first, second = pages.new(), pages.new()
    assert_type(pages[-1], pyropdf.Page)
    assert_type(pages[::-1], list[pyropdf.Page])
    assert isinstance(pages, Sequence)
    assert pages[::-1] == [second, first]
    pages.move(-1, 0)
    assert pages[-1] == first
    with pytest.raises(IndexError):
        _ = pages[-3]

    widgets = first.widgets
    spec = pyropdf.TextField("address", (1, 1, 20, 20))
    with pytest.raises(FrozenInstanceError):
        spec.name = "other"  # ty: ignore[invalid-assignment]
    widget = widgets.add(spec)
    field = assert_type(form["address"], pyropdf.Field)
    assert isinstance(form, Mapping)
    assert widgets[-1] == widget
    assert widget.field == field
    assert list(form.items()) == [("address", field)]
    del form["address"]
    assert not widgets
    assert "address" not in form
    with pytest.raises(KeyError):
        _ = form["address"]
    with pytest.raises(pyropdf.StaleObjectError):
        _ = field.value
    doc.history.undo()
    assert form["address"] == field
    assert widgets[-1] == widget

    del pages[-1]
    with pytest.raises(pyropdf.StalePageError):
        _ = first.index
    assert pages[:] == [second]
    doc.history.undo()
    assert pages[-1] == first


def test_read_only_results_and_python_311_type_information() -> None:
    metadata = {"title": "before"}
    probe = pyropdf.Probe(page_count=0, version="2.0", needs_password=False, is_repaired=False, metadata=metadata)
    removed = {"metadata": 1}
    changes = pyropdf.ChangeReport(signatures=(), claims=())
    sanitized = pyropdf.SanitizeReport(removed=removed, changes=changes, dropped_claims=())
    metadata["title"] = "after"
    removed["metadata"] = 2
    assert probe.metadata == {"title": "before"}
    assert sanitized.removed == {"metadata": 1}
    for result in (probe.metadata, sanitized.removed):
        with pytest.raises(TypeError):
            result["new"] = 1  # ty: ignore[invalid-assignment]
    doc = pyropdf.Document()
    root = assert_type(pyropdf.typed.Catalog(doc.catalog).Pages, pyropdf.typed.PageTreeNodeRoot)
    assert root.Count == len(doc.pages)
    assert_type(doc.profiles, pyropdf.Profiles)
    assert_type(doc.pdfa_claim, tuple[int, str] | None)
    assert get_type_hints(pyropdf.Profiles)["output_intents"] == tuple[pyropdf.OutputIntent, ...]
    assert pyropdf.TextField in get_args(pyropdf.FieldSpecification)
    assert len(get_overloads(pyropdf.Pages.__getitem__)) == 2
    with pytest.raises(FrozenInstanceError):
        doc.profiles.pdfa = "PDF/A-4"  # ty: ignore[invalid-assignment]
    with pytest.raises(TypeError):
        pyropdf.Profiles(None, None, None, None, ())  # ty: ignore[too-many-positional-arguments, missing-argument]


def test_operation_values_own_their_nested_inputs_and_parsed_bytes() -> None:
    buffer = bytearray(b"x")
    nested = [buffer]
    params = {"A": nested}
    operation = pyropdf.Operation("BDC", (pyropdf.Name("Span"), params))
    inline = pyropdf.InlineImage({"W": 1, "H": 1, "BPC": 8, "CS": pyropdf.Name("G"), "Params": params}, b"x")
    sequence = pyropdf.Operations([operation, inline])
    encoded = sequence.to_bytes()
    nested.clear()
    buffer[0] = ord("y")
    assert sequence[-1] == inline
    assert operation.operands[1]["A"] == (b"x",)
    assert inline.dictionary["Params"]["A"] == (b"x",)
    for original in (sequence, pyropdf.Operations.parse(encoded)):
        item = original[-1]
        assert isinstance(item, pyropdf.InlineImage)
        with pytest.raises(TypeError):
            item.dictionary["Params"]["A"] = ()
        with pytest.raises(TypeError):
            del original[-1]  # ty: ignore[not-subscriptable]
        assert original.to_bytes() == encoded


def test_geometry_has_one_spelling_for_each_operation() -> None:
    for rect in (pyropdf.Rect(1, 2, 3, 4), pyropdf.IRect(1, 2, 3, 4)):
        assert rect.top_left in rect
        for alias in ("tl", "tr", "bl", "br", "contains", "get_area", "irect"):
            assert not hasattr(rect, alias)
        with pytest.raises(TypeError):
            rect.area("in")  # ty: ignore[too-many-positional-arguments]
    for value in (pyropdf.Point(3, 4), pyropdf.Matrix.IDENTITY):
        assert not hasattr(value, "norm")
        assert abs(value) > 0
    with pytest.raises(TypeError):
        pyropdf.Point().distance_to((72, 0), "in")  # ty: ignore[too-many-positional-arguments]
