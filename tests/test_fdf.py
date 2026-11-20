"""FDF (ISO 32000-2 §12.7.8) and XFDF (ISO 19444-1) form data export, parsing, and import."""

from __future__ import annotations

import io
import zlib
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from functools import cache
from pathlib import Path

import pytest
from test_acroform import build_raw_pdf
from test_preservation import snapshot

import pyropdf
from pyropdf import (
    CheckBox,
    ComboBox,
    DocumentLimits,
    FdfData,
    FieldChoice,
    FieldFlags,
    Font,
    LimitError,
    ListBox,
    MalformedPdfError,
    Name,
    Note,
    PdfError,
    PushButton,
    RadioButton,
    SignatureField,
    Stream,
    TextField,
    parse_fdf,
    parse_xfdf,
)
from pyropdf._fdf import _open_fdf_document

ROOT = Path(__file__).resolve().parent.parent
NOTO_SANS_JP = ROOT / "crates" / "pyropdf-engine" / "src" / "fonts" / "testdata" / "NotoSansJP-subset.otf"


@cache
def japanese_font() -> Font:
    return Font.from_file(NOTO_SANS_JP)


def _build_hierarchical_form(*, filled: bool = False) -> bytes:
    """Build a valid AcroForm PDF with hierarchical text, button, combobox, single/multi listbox, push, and sig."""
    doc = pyropdf.Document()
    page0 = doc.pages.new((600, 800))
    page1 = doc.pages.new((600, 800))
    page1.annotations.add(Note((40, 40), contents="Keep untouched"))

    page0.widgets.add(TextField("notes", (40, 40, 240, 80), flags=FieldFlags.MULTILINE, default_value="Default note"))
    page0.widgets.add(CheckBox("agree", (40, 100, 58, 118), checked=filled))
    page0.widgets.add(RadioButton("plan", (40, 140, 58, 158), on_state="Basic", selected=False))
    page0.widgets.add(RadioButton("plan", (80, 140, 98, 158), on_state="Pro", selected=filled))
    page0.widgets.add(
        ComboBox(
            "level",
            (40, 180, 240, 204),
            choices=[FieldChoice("b", "Beginner"), FieldChoice("p", "Professional")],
            value="p" if filled else None,
        )
    )
    page0.widgets.add(
        ListBox(
            "country",
            (40, 220, 240, 260),
            choices=["JP", "UK", "US"],
            value="UK" if filled else None,
        )
    )
    page0.widgets.add(
        ListBox(
            "tags",
            (40, 280, 240, 340),
            choices=[FieldChoice("alpha", "Alpha"), FieldChoice("beta", "Beta"), FieldChoice("gamma", "Gamma")],
            flags=FieldFlags.MULTI_SELECT,
        )
    )
    page0.widgets.add(PushButton("submit", (40, 360, 140, 384), caption="Submit"))
    page0.widgets.add(SignatureField("sig", (40, 400, 200, 440)))

    first_w = page0.widgets.add(TextField("first_tmp", (300, 40, 500, 64), value="Ada" if filled else None))
    last_w = page0.widgets.add(TextField("last_tmp", (300, 80, 500, 104), value="Lovelace" if filled else None))
    city_w = page0.widgets.add(TextField("city_tmp", (300, 120, 500, 144), value="London" if filled else None))

    first_obj = doc.objects[first_w.xref]
    last_obj = doc.objects[last_w.xref]
    city_obj = doc.objects[city_w.xref]
    first_obj["T"] = "first"
    last_obj["T"] = "last"
    city_obj["T"] = "city"

    person_xref = doc.objects.add({"T": "person", "Kids": [first_obj, last_obj]})
    person_obj = doc.objects[person_xref]
    first_obj["Parent"] = person_obj
    last_obj["Parent"] = person_obj

    shipping_xref = doc.objects.add({"T": "shipping", "Kids": [city_obj]})
    shipping_obj = doc.objects[shipping_xref]
    city_obj["Parent"] = shipping_obj
    address_xref = doc.objects.add({"T": "address", "Kids": [shipping_obj]})
    address_obj = doc.objects[address_xref]
    shipping_obj["Parent"] = address_obj

    fields_arr = doc.catalog["AcroForm"]["Fields"]
    kept = [item for item in fields_arr if item.xref not in {first_w.xref, last_w.xref, city_w.xref}]
    doc.catalog["AcroForm"]["Fields"] = [*kept, person_obj, address_obj]
    for btn_name in ("agree", "plan"):
        doc.form[btn_name].obj["DA"] = b"/Helv 0 Tf 0 g"

    if filled:
        doc.form["notes"].value = "Line 1\r\nLine 2 & <3>"
        doc.form.import_fdf(FdfData(fields={"tags": ("alpha", "gamma")}))

    saved = doc.to_bytes()
    doc.close()
    return saved


def test_fdf_data_validation_and_immutability() -> None:
    assert FdfData.__module__ == "pyropdf"
    assert parse_fdf.__module__ == "pyropdf"
    assert parse_xfdf.__module__ == "pyropdf"

    data = FdfData(
        fields={"a": "1", "b": ("x", "y"), "c": None},
        file="form.pdf",
        id=(bytearray(b"0123"), memoryview(b"4567")),  # ty: ignore[invalid-argument-type]
    )
    assert isinstance(data.fields, Mapping)
    assert data.fields == {"a": "1", "b": ("x", "y"), "c": None}
    assert data.file == "form.pdf"
    assert data.id == (b"0123", b"4567")

    with pytest.raises(FrozenInstanceError):
        data.file = "other.pdf"  # ty: ignore[invalid-assignment]
    with pytest.raises(TypeError):
        data.fields["a"] = "2"  # ty: ignore[invalid-assignment]

    with pytest.raises(TypeError, match="Mapping"):
        FdfData(fields=[("a", "1")])  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="field names must be str"):
        FdfData(fields={1: "x"})  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="non-empty"):
        FdfData(fields={"": "x"})
    with pytest.raises(TypeError, match="tuple field values"):
        FdfData(fields={"a": ("ok", 2)})  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="field value"):
        FdfData(fields={"a": 123})  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="file"):
        FdfData(fields={}, file=123)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="2-tuple"):
        FdfData(fields={}, id=(b"only_one",))  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="bytes-like"):
        FdfData(fields={}, id=("a", "b"))  # ty: ignore[invalid-argument-type]


@pytest.mark.spec("fdf", "read", "interpret", "create")
def test_fdf_export_and_parse_preserve_exact_xref_offsets_and_hierarchy(tmp_path: Path) -> None:
    pdf_bytes = _build_hierarchical_form(filled=True)
    with pyropdf.open(pdf_bytes) as doc:
        assert not doc.check()
        expected_id = (bytes(doc.trailer["ID"][0]), bytes(doc.trailer["ID"][1]))
        fdf_bytes = doc.form.to_fdf(file="application.pdf")

    assert fdf_bytes.startswith(b"%FDF-1.2\n")
    with _open_fdf_document(fdf_bytes, None) as fdf_doc:
        assert not fdf_doc.is_repaired

    parsed = parse_fdf(fdf_bytes)
    assert parsed.file == "application.pdf"
    assert parsed.id == expected_id
    assert parsed.fields == {
        "address.shipping.city": "London",
        "agree": "Yes",
        "country": "UK",
        "level": "p",
        "notes": "Line 1\r\nLine 2 & <3>",
        "person.first": "Ada",
        "person.last": "Lovelace",
        "plan": "Pro",
        "tags": ("alpha", "gamma"),
    }
    assert "submit" not in parsed.fields
    assert "sig" not in parsed.fields

    fdf_path = tmp_path / "exported.fdf"
    fdf_path.write_bytes(fdf_bytes)
    assert parse_fdf(fdf_path) == parsed
    assert parse_fdf(str(fdf_path)) == parsed
    assert parse_fdf(bytearray(fdf_bytes)) == parsed
    assert parse_fdf(memoryview(fdf_bytes)) == parsed


@pytest.mark.spec("fdf", "read", "interpret")
def test_parse_fdf_reads_xrefless_files_streams_filespecs_and_widget_kids() -> None:
    compressed_text = zlib.compress(b"\xfe\xff" + "日本語テキスト".encode("utf-16-be"))
    xrefless = (
        b"% Comment before header\n"
        b"%FDF-1.4\n"
        b"1 0 obj\n"
        b"<< /FDF << /F 2 0 R /ID [<01020304> <A0B0C0D0>] /Fields [3 0 R 4 0 R 6 0 R 7 0 R] >> >>\n"
        b"endobj\n"
        b"2 0 obj\n"
        b"<< /Type /Filespec /F (fallback.pdf) /UF (unicode_form.pdf) >>\n"
        b"endobj\n"
        b"3 0 obj\n"
        b"<< /T (account) /Kids [<< /T (owner) /V 5 0 R >> << /T (blank) >>] >>\n"
        b"endobj\n"
        b"4 0 obj\n"
        b"<< /T (status) /V /Approved /Kids [<< /Subtype /Widget /AS /Approved >>] >>\n"
        b"endobj\n"
        b"5 0 obj\n"
        + f"<< /Filter /FlateDecode /Length {len(compressed_text)} >>\nstream\n".encode("ascii")
        + compressed_text
        + b"\nendstream\nendobj\n"
        b"6 0 obj\n"
        b"<< /T (choices) /V [(One) /Two] >>\n"
        b"endobj\n"
        b"7 0 obj\n"
        b"<< /T (cleared) /V null >>\n"
        b"endobj\n"
        b"trailer\n"
        b"<< /Root 1 0 R >>\n"
        b"%%EOF\n"
    )

    parsed = parse_fdf(xrefless)
    assert parsed.file == "unicode_form.pdf"
    assert parsed.id == (b"\x01\x02\x03\x04", b"\xa0\xb0\xc0\xd0")
    assert parsed.fields == {
        "account.owner": "日本語テキスト",
        "account.blank": None,
        "status": "Approved",
        "choices": ("One", "Two"),
        "cleared": None,
    }


@pytest.mark.spec("fdf", "read", "interpret", "create")
def test_xfdf_export_parse_and_round_trip_with_fdf(tmp_path: Path) -> None:
    with pyropdf.open(_build_hierarchical_form(filled=True)) as doc:
        doc.pages[0].widgets.add(TextField("unset_leaf", (300, 180, 500, 204)))
        fdf_data = parse_fdf(doc.form.to_fdf(file="contract & co.pdf"))
        xfdf_text = doc.form.to_xfdf(file="contract & co.pdf")
        filtered_xfdf = doc.form.to_xfdf(fields=["person.first", "tags"], include_empty=False)

    assert xfdf_text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<xfdf xmlns="http://ns.adobe.com/xfdf/"')
    xfdf_data = parse_xfdf(xfdf_text)
    assert xfdf_data == fdf_data
    assert xfdf_data.fields["unset_leaf"] is None
    assert xfdf_data.fields["notes"] == "Line 1\r\nLine 2 & <3>"
    assert parse_xfdf(filtered_xfdf).fields == {"person.first": "Ada", "tags": ("alpha", "gamma")}

    xfdf_path = tmp_path / "data.xfdf"
    xfdf_path.write_text("\ufeff" + xfdf_text, encoding="utf-8")
    assert parse_xfdf(xfdf_path) == fdf_data
    assert parse_xfdf(str(xfdf_path)) == fdf_data
    assert parse_xfdf("\ufeff" + xfdf_text) == fdf_data
    assert parse_xfdf(xfdf_text.encode("utf-8")) == fdf_data
    assert parse_xfdf(bytearray(xfdf_text.encode("utf-8"))) == fdf_data

    no_ns = (
        "<xfdf>"
        '<f href="plain.pdf"/>'
        '<ids original="deadbeef" modified="cafebabe"/>'
        '<annots><text page="0"><contents>Ignored</contents></text></annots>'
        "<fields>"
        '<field name="grp"><field name="empty"/><field name="empty_str"><value/></field></field>'
        '<field name="single"><value>Hello</value></field>'
        "</fields>"
        "</xfdf>"
    )
    parsed_no_ns = parse_xfdf(no_ns)
    assert parsed_no_ns.file == "plain.pdf"
    assert parsed_no_ns.id == (b"\xde\xad\xbe\xef", b"\xca\xfe\xba\xbe")
    assert parsed_no_ns.fields == {"grp.empty": None, "grp.empty_str": "", "single": "Hello"}


def _assert_filled_target(doc: pyropdf.Document) -> None:
    assert doc.form["person.first"].value == "Ada"
    assert doc.form["person.last"].value == "Lovelace"
    assert doc.form["address.shipping.city"].value == "London"
    assert doc.form["agree"].value == "Yes"
    assert doc.form["plan"].value == "Pro"
    assert doc.form["level"].value == "p"
    assert doc.form["country"].value == "UK"
    assert doc.form["tags"].value == "alpha, gamma"
    assert list(doc.form["tags"].obj["I"]) == [0, 2]
    assert doc.form["clear_me"].value is None
    assert "V" not in doc.form["clear_me"].obj
    assert doc.form.need_appearances is False


@pytest.mark.spec("fdf", "edit", "preserve")
@pytest.mark.parametrize("format_kind", ["fdf", "xfdf"])
@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_import_applies_atomically_with_undo_redo_and_preserves_untouched_objects(
    format_kind: str,
    *,
    incremental: bool,
) -> None:
    with pyropdf.open(_build_hierarchical_form(filled=True)) as src_doc:
        payload_fdf = src_doc.form.to_fdf(file="target.pdf")
        payload_xfdf = src_doc.form.to_xfdf(file="target.pdf")

    with pyropdf.open(_build_hierarchical_form(filled=False)) as target:
        target.pages[0].widgets.add(TextField("clear_me", (300, 220, 500, 244), value="stale", default_value="def"))
        baseline_bytes = target.to_bytes()

    with pyropdf.open(baseline_bytes) as doc:
        before_objects = snapshot(doc)
        page1_before = (doc.pages[1].text(), bytes(doc.pages[1].pixmap(dpi=72)))
        annot_xref = doc.pages[1].annotations[0].xref

        if format_kind == "fdf":
            merged = FdfData(fields={**parse_fdf(payload_fdf).fields, "clear_me": None, "unknown.field": "skip"})
            updated = doc.form.import_fdf(merged)
            expected_step = "import fdf"
        else:
            merged = FdfData(fields={**parse_xfdf(payload_xfdf).fields, "clear_me": None, "unknown.field": "skip"})
            updated = doc.form.import_xfdf(merged)
            expected_step = "import xfdf"

        assert updated == [
            "address.shipping.city",
            "agree",
            "clear_me",
            "country",
            "level",
            "notes",
            "person.first",
            "person.last",
            "plan",
            "tags",
        ]
        assert doc.history.steps == (expected_step,)
        _assert_filled_target(doc)
        with pyropdf.open(doc.to_bytes()) as checked:
            assert not checked.check()

        doc.history.undo()
        assert doc.form["person.first"].value is None
        assert doc.form["agree"].value == "Off"
        assert doc.form["tags"].value is None
        assert doc.form["clear_me"].value == "stale"
        with pyropdf.open(doc.to_bytes()) as checked_undo:
            assert not checked_undo.check()

        doc.history.redo()
        _assert_filled_target(doc)
        saved = doc.to_bytes(incremental=incremental)

    with pyropdf.open(saved) as reopened:
        assert not reopened.check()
        _assert_filled_target(reopened)
        after_objects = snapshot(reopened)
        assert after_objects[annot_xref] == before_objects[annot_xref]
        assert after_objects[reopened.pages[1].xref] == before_objects[reopened.pages[1].xref]
        assert (reopened.pages[1].text(), bytes(reopened.pages[1].pixmap(dpi=72))) == page1_before


@pytest.mark.spec("fdf", "edit", "preserve")
def test_clearing_buttons_and_embedded_font_import_keep_pdf20_conformance() -> None:
    doc = pyropdf.Document()
    doc.catalog["Version"] = Name("2.0")
    page = doc.pages.new((400, 300))
    page.widgets.add(TextField("jp_name", (20, 20, 220, 44)))
    page.widgets.add(
        ListBox(
            "jp_tags",
            (20, 60, 220, 120),
            choices=["日本語", "テキスト", "A1"],
            flags=FieldFlags.MULTI_SELECT,
        )
    )
    page.widgets.add(CheckBox("agree", (20, 140, 38, 158), checked=True))
    page.widgets.add(RadioButton("choice", (20, 180, 38, 198), on_state="One", selected=True))
    page.widgets.add(RadioButton("choice", (60, 180, 78, 198), on_state="Two", selected=False))
    for btn_name in ("agree", "choice"):
        doc.form[btn_name].obj["DA"] = b"/Helv 0 Tf 0 g"

    doc.form.font = japanese_font()
    updated = doc.form.import_xfdf(
        '<xfdf xmlns="http://ns.adobe.com/xfdf/"><fields>'
        '<field name="jp_name"><value>日本語のテキスト</value></field>'
        '<field name="jp_tags"><value>日本語</value><value>A1</value></field>'
        '<field name="agree"/>'
        '<field name="choice"><value>Off</value></field>'
        "</fields></xfdf>"
    )
    assert updated == ["agree", "choice", "jp_name", "jp_tags"]
    assert doc.form["jp_name"].value == "日本語のテキスト"
    assert doc.form["jp_tags"].value == "日本語, A1"
    assert doc.form["agree"].value is None
    assert "V" not in doc.form["agree"].obj
    assert doc.form["agree"].widgets[0].obj["AS"] == Name("Off")
    assert doc.form["choice"].value == "Off"
    assert all(w.obj["AS"] == Name("Off") for w in doc.form["choice"].widgets)
    assert doc.form.need_appearances is False

    saved = doc.to_bytes()
    doc.close()
    with pyropdf.open(saved) as reopened:
        assert not reopened.check()
        assert isinstance(reopened.form["jp_name"].widgets[0].obj["AP"]["N"], Stream)
        assert isinstance(reopened.form["jp_tags"].widgets[0].obj["AP"]["N"], Stream)


def test_import_rolls_back_completely_on_incompatible_field_value() -> None:
    with pyropdf.open(_build_hierarchical_form(filled=False)) as doc:
        before = doc.to_bytes()
        bad = FdfData(fields={"person.first": "Changed", "country": ("JP", "US")})
        with pytest.raises(PdfError, match="does not accept multiple values"):
            doc.form.import_fdf(bad)
        assert doc.history.steps == ()
        assert doc.form["person.first"].value is None
        assert doc.to_bytes() == before


def test_export_options_and_deep_32_level_hierarchy() -> None:
    with pyropdf.open(_build_hierarchical_form(filled=False)) as doc:
        doc.form["person.first"].value = "Grace"
        fdf_non_empty = doc.form.to_fdf(include_empty=False)
        xfdf_non_empty = doc.form.to_xfdf(include_empty=False)
        expected_non_empty = {"agree": "Off", "person.first": "Grace", "plan": "Off"}
        assert parse_fdf(fdf_non_empty).fields == expected_non_empty
        assert parse_xfdf(xfdf_non_empty).fields == expected_non_empty

        with pytest.raises(KeyError):
            doc.form.to_fdf(fields=["missing"])
        with pytest.raises(TypeError, match="iterable"):
            doc.form.to_fdf(fields="person.first")
        with pytest.raises(TypeError, match="include_empty"):
            doc.form.to_xfdf(include_empty="yes")  # ty: ignore[invalid-argument-type]

        doc.form["notes"].value = "bad\x00char"
        with pytest.raises(PdfError, match=r"XML 1\.0"):
            doc.form.to_xfdf()

    deep_parts = [f"lvl{i}" for i in range(1, 33)]
    objs: dict[int, str] = {
        1: "<< /Type /Catalog /Pages 2 0 R /AcroForm 4 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 200] >>",
        3: "<< /Type /Page /Parent 2 0 R /Annots [36 0 R] >>",
        4: "<< /Fields [5 0 R] >>",
    }
    for idx, part in enumerate(deep_parts):
        obj_num = 5 + idx
        if idx < 31:
            parent_entry = f"/Parent {obj_num - 1} 0 R " if idx > 0 else ""
            objs[obj_num] = f"<< {parent_entry}/T ({part}) /Kids [{obj_num + 1} 0 R] >>"
        else:
            objs[obj_num] = (
                f"<< /Parent {obj_num - 1} 0 R /FT /Tx /T ({part}) /V (deep_val)"
                " /Type /Annot /Subtype /Widget /Rect [10 10 100 30] /P 3 0 R >>"
            )
    with pyropdf.open(build_raw_pdf(objs)) as deep_doc:
        deep_fdf = deep_doc.form.to_fdf()
        deep_xfdf = deep_doc.form.to_xfdf()

    expected_key = ".".join(deep_parts)
    assert parse_fdf(deep_fdf, limits=DocumentLimits.web()).fields == {expected_key: "deep_val"}
    assert parse_xfdf(deep_xfdf).fields == {expected_key: "deep_val"}


def test_parse_fdf_malformed_inputs(tmp_path: Path) -> None:
    with pytest.raises(MalformedPdfError):
        parse_fdf(b"")
    with pytest.raises(MalformedPdfError, match=r"%FDF-x\.y"):
        parse_fdf(b"%PDF-1.7\n1 0 obj << >> endobj\n")
    with pytest.raises(MalformedPdfError, match=r"%FDF-x\.y"):
        parse_fdf(b"%FDF-abc\n")
    with pytest.raises(MalformedPdfError, match="1024"):
        parse_fdf(b" " * 1024 + b"%FDF-1.2\n")
    with pytest.raises(FileNotFoundError):
        parse_fdf(tmp_path / "missing.fdf")
    with pytest.raises(TypeError, match="read the file object"):
        parse_fdf(io.BytesIO(b"%FDF-1.2\n"))  # ty: ignore[invalid-argument-type]

    no_fdf = b"%FDF-1.2\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
    with pytest.raises(MalformedPdfError, match="/FDF"):
        parse_fdf(no_fdf)

    cyclic = (
        b"%FDF-1.2\n"
        b"1 0 obj << /FDF << /Fields [2 0 R] >> >> endobj\n"
        b"2 0 obj << /T (a) /Kids [3 0 R] >> endobj\n"
        b"3 0 obj << /T (b) /Kids [2 0 R] >> endobj\n"
        b"trailer << /Root 1 0 R >>\n%%EOF\n"
    )
    with pytest.raises(MalformedPdfError, match="cycle"):
        parse_fdf(cyclic)


def test_parse_fdf_limit_enforcement(tmp_path: Path) -> None:
    overdeep_objs = [b"%FDF-1.2\n1 0 obj << /FDF << /Fields [2 0 R] >> >> endobj\n"]
    for level in range(33):
        num = 2 + level
        if level < 32:
            overdeep_objs.append(f"{num} 0 obj << /T (n{level}) /Kids [{num + 1} 0 R] >> endobj\n".encode("ascii"))
        else:
            overdeep_objs.append(f"{num} 0 obj << /T (leaf) /V (x) >> endobj\n".encode("ascii"))
    overdeep_objs.append(b"trailer << /Root 1 0 R >>\n%%EOF\n")
    with pytest.raises(LimitError) as depth_exc:
        parse_fdf(b"".join(overdeep_objs))
    assert depth_exc.value.code == "form_field_parent_depth"

    many_fields = (
        b"%FDF-1.2\n1 0 obj << /FDF << /Fields ["
        + b" ".join(f"<< /T (f{i}) /V (v) >>".encode("ascii") for i in range(4097))
        + b"] >> >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
    )
    with pytest.raises(PdfError, match="4096"):
        parse_fdf(many_fields)

    valid_fdf = (
        b"%FDF-1.2\n1 0 obj << /FDF << /Fields [<< /T (a) /V (1) >>] >> >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
    )
    fdf_file = tmp_path / "small.fdf"
    fdf_file.write_bytes(valid_fdf)

    with pytest.raises(LimitError) as size_buf_exc:
        parse_fdf(valid_fdf, limits=DocumentLimits(max_file_size=10))
    assert size_buf_exc.value.code == "file_size"

    with pytest.raises(LimitError) as size_path_exc:
        parse_fdf(fdf_file, limits=DocumentLimits(max_file_size=10))
    assert size_path_exc.value.code == "file_size"

    with pytest.raises(LimitError) as obj_exc:
        parse_fdf(b"".join(overdeep_objs), limits=DocumentLimits(max_objects=5))
    assert obj_exc.value.code == "object_count"

    bomb_payload = zlib.compress(b"A" * (1024 * 1024 + 32))
    bomb_fdf = (
        b"%FDF-1.2\n"
        b"1 0 obj << /FDF << /Fields [<< /T (big) /V 2 0 R >>] >> >> endobj\n"
        + f"2 0 obj << /Filter /FlateDecode /Length {len(bomb_payload)} >>\nstream\n".encode("ascii")
        + bomb_payload
        + b"\nendstream endobj\n"
        b"trailer << /Root 1 0 R >>\n%%EOF\n"
    )
    with pytest.raises(LimitError) as decomp_exc:
        parse_fdf(bomb_fdf, limits=DocumentLimits(max_decompressed_size=4096))
    assert decomp_exc.value.code == "decompressed_size"

    with pytest.raises(LimitError) as text_cap_exc:
        parse_fdf(bomb_fdf)
    assert text_cap_exc.value.code == "form_field_input_size"


def test_parse_xfdf_dtd_entity_and_limit_enforcement(tmp_path: Path) -> None:
    for invalid in ("", "   \n\t ", b"", "<not_xfdf/>", '<xfdf xmlns="http://example.com/other"/>', "<xfdf><unclosed>"):
        with pytest.raises(MalformedPdfError):
            parse_xfdf(invalid)
    with pytest.raises(MalformedPdfError):
        parse_xfdf("<xfdf><fields><field><value>missing_name</value></field></fields></xfdf>")
    with pytest.raises(FileNotFoundError):
        parse_xfdf(tmp_path / "missing.xfdf")
    with pytest.raises(FileNotFoundError):
        parse_xfdf(str(tmp_path / "missing.xfdf"))
    with pytest.raises(TypeError, match="read the file object"):
        parse_xfdf(io.StringIO("<xfdf/>"))  # ty: ignore[invalid-argument-type]

    dtd_cases: list[str | bytes] = [
        '<?xml version="1.0"?><!DOCTYPE xfdf [<!ENTITY xxe "boom">]><xfdf/>',
        '<!doctype xfdf SYSTEM "http://example.com/xfdf.dtd"><xfdf/>',
        '<!ENTITY lol "lol"><xfdf/>',
        '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE xfdf><xfdf/>'.encode("utf-16"),
    ]
    for payload in dtd_cases:
        with pytest.raises(MalformedPdfError, match="DOCTYPE and ENTITY"):
            parse_xfdf(payload)

    sample = (
        '<xfdf xmlns="http://ns.adobe.com/xfdf/"><fields><field name="a"><value>12345</value></field></fields></xfdf>'
    )
    sample_path = tmp_path / "sample.xfdf"
    sample_path.write_text(sample, encoding="utf-8")
    for src in (sample, sample.encode("utf-8"), sample_path):
        with pytest.raises(LimitError) as size_exc:
            parse_xfdf(src, max_size=20)
        assert size_exc.value.code == "form_field_input_size"

    nested_open = "".join(f'<field name="l{i}">' for i in range(33))
    nested_close = "</field>" * 33
    overdeep_xfdf = f"<xfdf><fields>{nested_open}<value>x</value>{nested_close}</fields></xfdf>"
    with pytest.raises(LimitError) as depth_exc:
        parse_xfdf(overdeep_xfdf)
    assert depth_exc.value.code == "form_field_parent_depth"

    too_many_xfdf = (
        "<xfdf><fields>"
        + "".join(f'<field name="f{i}"><value>1</value></field>' for i in range(4097))
        + "</fields></xfdf>"
    )
    with pytest.raises(PdfError, match="4096"):
        parse_xfdf(too_many_xfdf)

    big_val_xfdf = f'<xfdf><fields><field name="big"><value>{"x" * (1024 * 1024 + 1)}</value></field></fields></xfdf>'
    with pytest.raises(LimitError) as text_exc:
        parse_xfdf(big_val_xfdf, max_size=None)
    assert text_exc.value.code == "form_field_input_size"


def test_direct_file_and_nested_transaction_imports_and_closed_or_locked_errors(tmp_path: Path) -> None:
    with pyropdf.Document() as empty_doc:
        empty_doc.pages.new((200, 200))
        empty_fdf = empty_doc.form.to_fdf()
        empty_xfdf = empty_doc.form.to_xfdf()
        assert parse_fdf(empty_fdf) == FdfData(fields={})
        assert parse_xfdf(empty_xfdf) == FdfData(fields={})
        assert "<fields/>" in empty_xfdf

    with pyropdf.open(_build_hierarchical_form(filled=True)) as src_doc:
        fdf_path = tmp_path / "src.fdf"
        xfdf_path = tmp_path / "src.xfdf"
        fdf_path.write_bytes(src_doc.form.to_fdf(fields=["person.first"]))
        xfdf_path.write_text(src_doc.form.to_xfdf(fields=["person.last"]), encoding="utf-8")

    with pyropdf.open(_build_hierarchical_form(filled=False)) as dst_doc:
        dst_doc.form["notes"].value = "step1"
        dst_doc.history.undo()
        assert dst_doc.history.can_redo
        assert dst_doc.form.import_fdf(FdfData(fields={"nonexistent": "1"})) == []
        assert dst_doc.history.can_redo

        with dst_doc.transaction("batch import"):
            assert dst_doc.form.import_fdf(fdf_path) == ["person.first"]
            assert dst_doc.form.import_xfdf(xfdf_path) == ["person.last"]
        assert dst_doc.history.steps == ("batch import",)
        assert dst_doc.form["person.first"].value == "Ada"
        assert dst_doc.form["person.last"].value == "Lovelace"

        enc = pyropdf.Encryption(user_password="u", owner_password="o")  # noqa: S106
        enc_bytes = dst_doc.to_bytes(encryption=enc)

    with pyropdf.open(enc_bytes) as locked_doc:
        with pytest.raises(pyropdf.EncryptedDocumentError):
            locked_doc.form.to_fdf()
        with pytest.raises(pyropdf.EncryptedDocumentError):
            locked_doc.form.import_fdf(fdf_path)

    closed_doc = pyropdf.Document()
    closed_doc.close()
    with pytest.raises(pyropdf.DocumentClosedError):
        closed_doc.form.to_fdf()
