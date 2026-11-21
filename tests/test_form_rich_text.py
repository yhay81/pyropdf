"""Rich text and masked password appearances inspected through qpdf and Poppler."""

from __future__ import annotations

import io
import shutil
import subprocess
from typing import TYPE_CHECKING

import pikepdf
import pytest

import pyropdf
from pyropdf import FieldFlags, TextField

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

_BODY = '<body xmlns="http://www.w3.org/1999/xhtml">'
_RICH = (
    _BODY
    + '<p>Hello <b><span style="color:#ff0000">RED</span></b></p>'
    + '<p style="text-align:right">World &amp; friends</p></body>'
)
_DS = "font:normal 12pt Helvetica; color:rgb(0,0,255)"


def _seed() -> bytes:
    doc = pyropdf.Document()
    doc.pages.new((230, 110))
    return doc.to_bytes()


def _operators(stream: pikepdf.Object, name: str) -> list[list[Any]]:
    return [list(args) for args, op in pikepdf.parse_content_stream(stream) if str(op) == name]


def _authored() -> bytes:
    with pyropdf.open(_seed()) as doc:
        doc.pages[0].widgets.add(
            TextField("rich", (4, 4, 225, 90), rich_text=_RICH, default_style=_DS, flags=FieldFlags.MULTILINE)
        )
        return doc.to_bytes()


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.spec("variable-text", "create", "interpret")
def test_rich_field_creation_and_scoped_appearance_after_reopen(*, incremental: bool) -> None:
    with pyropdf.open(_seed()) as doc:
        doc.pages[0].widgets.add(
            TextField("rich", (4, 4, 225, 90), rich_text=_RICH, default_style=_DS, flags=FieldFlags.MULTILINE)
        )
        field = doc.form["rich"]
        assert field.rich_text == _RICH
        assert field.default_style == _DS
        assert field.value == "Hello RED\nWorld & friends"
        assert field.flags & FieldFlags.RICH_TEXT
        saved = doc.to_bytes(incremental=incremental)
    with pikepdf.open(io.BytesIO(saved)) as pdf:
        field = pdf.Root.AcroForm.Fields[0]
        assert str(field.RV) == _RICH
        assert str(field.DS) == _DS
        assert str(field.V) == "Hello RED\nWorld & friends"
        normal = field.AP.N
        assert {str(v.BaseFont) for _, v in normal.Resources.Font.items()} == {"/Helvetica", "/Helvetica-Bold"}
        assert [bytes(args[0]) for args in _operators(normal, "Tj")] == [b"Hello ", b"RED", b"World & friends"]
        assert [list(map(int, operands)) for operands in _operators(normal, "rg")][-3:] == [
            [0, 0, 1],
            [1, 0, 0],
            [0, 0, 1],
        ]
        positions = _operators(normal, "Tm")
        assert float(positions[2][4]) > float(positions[0][4])
        assert float(positions[0][5]) > float(positions[2][5])


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.spec("variable-text", "read", "edit")
def test_qpdf_authored_rich_stream_and_shared_reference_survive_field_edits(*, incremental: bool) -> None:
    # qpdf independently encodes a UTF-16 XML stream shared by another owner.
    with pikepdf.open(io.BytesIO(_authored())) as pdf:
        field = pdf.Root.AcroForm.Fields[0]
        original = pdf.make_stream(_RICH.encode("utf-16"))
        original.Custom = pikepdf.String("preserved")
        field.RV = original
        pdf.Root.SharedRich = original
        field.Other = pikepdf.String("unrelated")
        raw = io.BytesIO()
        pdf.save(raw)
    with pyropdf.open(raw.getvalue()) as doc:
        field = doc.form["rich"]
        assert field.rich_text == _RICH
        field.default_style = "font-size:14pt;color:#224466"
        edited = _BODY + "<p>Edited<br/>second</p></body>"
        field.rich_text = edited
        saved = doc.to_bytes(incremental=incremental)
    with pikepdf.open(io.BytesIO(saved)) as pdf:
        field = pdf.Root.AcroForm.Fields[0]
        assert field.RV.read_bytes() == edited.encode()
        assert str(field.RV.Custom) == "preserved"
        assert pdf.Root.SharedRich.read_bytes() == _RICH.encode("utf-16")
        assert str(field.Other) == "unrelated"
        assert str(field.V) == "Edited\nsecond"
        assert str(field.DS) == "font-size:14pt;color:#224466"
        assert {float(args[1]) for args in _operators(field.AP.N, "Tf")} == {14.0}
    with pyropdf.open(saved) as doc:
        field = doc.form["rich"]
        field.value = "Plain replacement"
        assert field.rich_text is None
        assert field.value == "Plain replacement"
        field.default_style = None
        assert field.default_style is None


@pytest.mark.spec("variable-text", "read", "create", "edit")
def test_malformed_rich_value_leaves_the_field_and_new_widget_transaction_intact() -> None:
    with pyropdf.open(_authored()) as doc:
        field = doc.form["rich"]
        for bad in ("<body/>", _BODY + "<p>broken</body>", "<!DOCTYPE body>" + _RICH):
            with pytest.raises(pyropdf.PdfError, match="XHTML"):
                field.rich_text = bad
            assert field.rich_text == _RICH
            assert field.value == "Hello RED\nWorld & friends"
            with pytest.raises(pyropdf.PdfError, match="XHTML"):
                doc.pages[0].widgets.add(TextField("bad", (4, 4, 120, 60), rich_text=bad))
            assert "bad" not in doc.form
        field.rich_text = None
        assert field.rich_text is None
        assert field.value == "Hello RED\nWorld & friends"


def _password(value: str, *, masked: bool, update: str | None = None, incremental: bool = False) -> bytes:
    with pyropdf.open(_seed()) as doc:
        doc.pages[0].widgets.add(
            TextField(
                "value",
                (4, 4, 210, 60),
                value=value,
                font_size=18,
                flags=FieldFlags.PASSWORD if masked else FieldFlags(0),
            )
        )
        if update is not None:
            doc.form["value"].value = update
        return doc.to_bytes(incremental=incremental)


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.spec("text-fields", "create", "edit")
def test_password_appearance_contains_only_one_asterisk_per_grapheme(*, incremental: bool) -> None:
    secret = "A\N{COMBINING ACUTE ACCENT}🙂"  # noqa: S105 - generated form fixture
    for saved in (
        _password(secret, masked=True, incremental=incremental),
        _password("before", masked=True, update=secret, incremental=incremental),
    ):
        with pikepdf.open(io.BytesIO(saved)) as pdf:
            field = pdf.Root.AcroForm.Fields[0]
            assert str(field.V) == secret
            assert int(field.Ff) & int(FieldFlags.PASSWORD)
            assert [bytes(args[0]) for args in _operators(field.AP.N, "Tj")] == [b"**"]


@pytest.mark.skipif(not shutil.which("pdftoppm"), reason="requires Poppler")
@pytest.mark.spec("text-fields", "interpret")
@pytest.mark.spec("variable-text", "interpret")
def test_poppler_renders_identical_password_masks_and_colored_rich_text(tmp_path: Path) -> None:
    def raster(name: str, data: bytes) -> bytes:
        path = tmp_path / name
        path.write_bytes(data)
        return subprocess.run(  # noqa: S603 - test PDF passed to the installed Poppler reader.
            [shutil.which("pdftoppm") or "pdftoppm", "-r", "96", "-singlefile", str(path)],
            check=True,
            capture_output=True,
        ).stdout

    expected = raster("plain.pdf", _password("**", masked=False))
    assert raster("masked.pdf", _password("é🙂", masked=True)) == expected
    assert raster("edited.pdf", _password("x", masked=True, update="YZ")) == expected
    assert raster("longer.pdf", _password("XYZ", masked=True)) != expected
    pixels = raster("rich.pdf", _authored()).split(b"\n", 3)[3]
    assert any(r > 180 and g < 80 and b < 80 for r, g, b in zip(pixels[::3], pixels[1::3], pixels[2::3], strict=True))
    assert any(r < 80 and g < 80 and b > 180 for r, g, b in zip(pixels[::3], pixels[1::3], pixels[2::3], strict=True))


@pytest.mark.spec("variable-text", "interpret")
def test_rich_text_auto_fit_and_css_decorations_survive_in_the_pdf_stream() -> None:
    xml = (
        _BODY
        + '<p><span style="text-decoration:underline;font-stretch:condensed">WWWWWWWWWWWWWWWWWWWW</span></p></body>'
    )
    with pyropdf.open(_seed()) as doc:
        doc.pages[0].widgets.add(TextField("fit", (5, 5, 45, 25), rich_text=xml))
        with pikepdf.open(io.BytesIO(doc.to_bytes())) as pdf:
            normal = pdf.Root.AcroForm.Fields[0].AP.N
            sizes = [float(args[1]) for args in _operators(normal, "Tf")]
            assert len(sizes) == 1
            assert 0 < sizes[0] < 7
            assert [int(args[0]) for args in _operators(normal, "Tz")] == [75]
            assert len(_operators(normal, "l")) >= 1


@pytest.mark.spec("variable-text", "read", "create", "edit")
def test_split_widgets_use_the_terminal_fields_own_rich_entries() -> None:
    with pyropdf.open(_authored()) as doc:
        page = doc.pages.new((230, 110))
        page.widgets.add(TextField("rich", (4, 4, 225, 90)))
        field = doc.form["rich"]
        field.rich_text = _BODY + "<p>Two widgets</p></body>"
        with pikepdf.open(io.BytesIO(doc.to_bytes())) as pdf:
            root = pdf.Root.AcroForm.Fields[0]
            assert str(root.V) == "Two widgets"
            assert str(root.RV).endswith("Two widgets</p></body>")
            assert len(root.Kids) == 2
            for widget in root.Kids:
                assert "/RV" not in widget
                assert [bytes(args[0]) for args in _operators(widget.AP.N, "Tj")] == [b"Two widgets"]
