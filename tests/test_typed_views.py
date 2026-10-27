"""pyropdf.typed: a view per object type of the Arlington PDF Model.

The views read keys as the model declares them, follow links to the views of
the linked types, and inherit through /Parent (DESIGN.md section 8).
tools/arlington/generate.py writes their stub, python/pyropdf/typed.pyi.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest
from corpus import ASSETS

import pyropdf
from pyropdf import Matrix, Name, typed

STUB = Path(typed.__file__).with_suffix(".pyi")


def _class_name(name: str) -> str:
    return "ThreeD" + name[2:] if name.startswith("3D") else name


def test_every_object_type_has_a_view_and_a_stub() -> None:
    pattern = r"^class (\w+)\(((?:Dictionary|Stream|Array)View)\):$"
    stubbed = dict(re.findall(pattern, STUB.read_text(encoding="utf-8"), re.MULTILINE))
    assert stubbed.pop("StreamView") == "DictionaryView"

    assert len(typed.TYPES) == len(stubbed) == 613
    assert sorted(stubbed) == sorted(_class_name(name) for name in typed.TYPES)
    for name in typed.TYPES:
        view = getattr(typed, _class_name(name))
        assert view.__bases__ == (getattr(typed, stubbed[_class_name(name)]),)
        assert view.__name__ == _class_name(name)
        assert view._type == name  # noqa: SLF001
    assert typed.ThreeDView is getattr(typed, "ThreeDView")  # noqa: B009 - the class is made once.
    assert "Catalog" in dir(typed)
    with pytest.raises(AttributeError):
        _ = cast("Any", typed).NoSuchType


def test_the_catalog_and_its_page_tree_read_as_declared() -> None:
    doc = pyropdf.open(ASSETS / "pdf20-simple.pdf")

    catalog = typed.Catalog(doc.catalog)
    root = catalog.Pages
    kid = root.Kids[0]

    assert catalog.Type == "Catalog"
    assert isinstance(catalog.Type, Name)
    assert isinstance(root, typed.PageTreeNodeRoot)
    assert isinstance(root.Count, int)
    assert root.Count == len(doc.pages)
    assert isinstance(root.Kids, typed.ArrayOfPageTreeNodeKids)
    assert kid == typed.PageObject(doc.pages[0].obj)
    assert isinstance(kid, typed.PageObject)
    assert repr(catalog) == f"<Catalog {doc.catalog.xref} 0 R>"
    assert catalog.obj == doc.catalog


def test_a_page_its_font_and_its_annotations_read_as_declared() -> None:
    doc = pyropdf.open(ASSETS / "pdfium-links-highlights-annots.pdf")
    page = typed.PageObject(doc.pages[0].obj)

    annotations = page.Annots
    assert annotations is not None
    kinds = [type(annotations[index]).__name__ for index in range(len(annotations.obj))]
    assert kinds == ["AnnotWidget"] * 4 + ["AnnotLink", "AnnotHighlight"]
    highlight = annotations[5]
    assert isinstance(highlight, typed.AnnotHighlight)
    assert highlight.Subtype == "Highlight"
    rect = highlight.Rect
    assert isinstance(rect, tuple)
    assert len(rect) == 4
    assert all(isinstance(value, float) for value in rect)
    assert isinstance(page.MediaBox, tuple)

    fonts = typed.PageObject(pyropdf.open(ASSETS / "f1040.pdf").pages[0].obj).Resources.Font
    assert isinstance(fonts, typed.FontMap)
    font = fonts["T1_0"]
    assert isinstance(font, typed.FontType1)
    assert font.Subtype == "Type1"
    assert isinstance(font.BaseFont, str)
    type3_fonts = typed.PageObject(pyropdf.open(ASSETS / "pdfium-type3.pdf").pages[0].obj).Resources.Font
    assert type3_fonts is not None
    type3 = type3_fonts["F1"]
    assert isinstance(type3, typed.FontType3)
    assert isinstance(type3.FontMatrix, Matrix)


def test_an_inheritable_key_reads_through_the_page_tree() -> None:
    doc = pyropdf.Document()
    doc.pages.new((200, 300))
    page = typed.PageObject(doc.pages[0].obj)
    if "Rotate" in page:
        del page.Rotate

    assert page.Rotate == 0
    with pytest.raises(KeyError):
        _ = page.obj["Rotate"]

    doc.catalog["Pages"]["Rotate"] = 90

    assert page.Rotate == 90
    assert page["Rotate"] == 90
    assert "Rotate" not in page
    assert doc.pages[0].rotation == 90


def test_inheritance_is_bounded() -> None:
    doc = pyropdf.Document()
    looped = doc.objects.add({"Type": Name("Page")})
    doc.objects[looped]["Parent"] = doc.objects[looped]
    assert typed.PageObject(doc.objects[looped]).Rotate == 0

    parent = doc.objects.add({"Type": Name("Pages"), "Rotate": 180})
    for _ in range(70):
        parent = doc.objects.add({"Type": Name("Pages"), "Parent": doc.objects[parent]})
    deep = typed.PageObject(doc.objects[doc.objects.add({"Type": Name("Page"), "Parent": doc.objects[parent]})])
    with pytest.raises(pyropdf.LimitError) as caught:
        _ = deep.Rotate
    assert caught.value.code == "object_depth"


def test_absent_required_keys_and_values_of_no_declared_type_are_refused() -> None:
    doc = pyropdf.Document()
    bare = typed.Catalog(doc.objects[doc.objects.add({"Type": Name("Catalog")})])

    with pytest.raises(pyropdf.PdfError, match="Catalog /Pages is required and absent"):
        _ = bare.Pages
    assert bare.Lang is None
    with pytest.raises(KeyError):
        _ = bare["Lang"]
    # An absent key with a default reads as the default.
    assert bare.PageLayout == "SinglePage"

    bare.obj["Lang"] = 3
    with pytest.raises(pyropdf.PdfError, match="Catalog /Lang is an integer, which is none of its types: string-text"):
        _ = bare.Lang
    bare.obj["Lang"] = None
    assert bare.Lang is None


def test_plain_values_are_written_as_declared() -> None:
    doc = pyropdf.Document()
    doc.pages.new((200, 300))
    catalog = typed.Catalog(doc.catalog)
    page = typed.PageObject(doc.pages[0].obj)

    page.Rotate = 180
    page.CropBox = (10, 10, 100, 100)
    page["UserUnit"] = 2.5
    catalog.PageLayout = "TwoColumnLeft"
    catalog.Lang = "en-US"

    assert page.obj["Rotate"] == 180
    assert page.CropBox == (10.0, 10.0, 100.0, 100.0)
    assert page.UserUnit == 2.5
    assert catalog.obj["PageLayout"] == Name("TwoColumnLeft")
    assert catalog.Lang == "en-US"
    assert doc.history.can_undo

    catalog.PageLayout = None
    del page.CropBox
    assert "PageLayout" not in catalog.obj
    assert "CropBox" not in page.obj

    root = catalog.Pages
    page.Parent = root
    assert page.obj["Parent"] == root.obj


def test_writes_of_no_declared_type_are_refused() -> None:
    doc = pyropdf.Document()
    doc.pages.new((200, 300))
    catalog = typed.Catalog(doc.catalog)
    page = typed.PageObject(doc.pages[0].obj)
    before = doc.to_bytes()

    with pytest.raises(TypeError, match="PageObject /Rotate takes integer, not str"):
        page.Rotate = cast("Any", "90")
    with pytest.raises(TypeError, match="takes integer, not bool"):
        page.Rotate = True
    with pytest.raises(ValueError, match="PageLayout is one of"):
        catalog.PageLayout = "Diagonal"
    with pytest.raises(TypeError, match="takes rectangle"):
        page.CropBox = cast("Any", (0, 0, 1))
    with pytest.raises(TypeError, match="takes dictionary"):
        catalog.Pages = cast("Any", page)
    with pytest.raises(KeyError, match="not a key of the model"):
        catalog["NoSuchKey"] = 1
    with pytest.raises(AttributeError, match="Catalog has no key 'NoSuchKey'"):
        cast("Any", catalog).NoSuchKey = 1
    with pytest.raises(AttributeError):
        _ = cast("Any", catalog).NoSuchKey

    assert doc.to_bytes() == before


def test_views_take_handles_only() -> None:
    doc = pyropdf.Document()

    with pytest.raises(TypeError, match="a Catalog views a Dictionary, not int"):
        typed.Catalog(cast("Any", 5))
    with pytest.raises(TypeError, match="a ArrayOfAnnots views a Array, not Dictionary"):
        typed.ArrayOfAnnots(cast("Any", doc.catalog))
    with pytest.raises(TypeError, match="a Metadata views a Stream, not Dictionary"):
        typed.Metadata(cast("Any", doc.catalog))
    with pytest.raises(TypeError, match="DictionaryView is a base class"):
        typed.DictionaryView(doc.catalog)
    assert typed.Catalog(doc.catalog) == typed.Catalog(doc.catalog)
    assert typed.Catalog(doc.catalog) != typed.PageObject(doc.catalog)
    assert len({typed.Catalog(doc.catalog), typed.Catalog(doc.catalog)}) == 1
