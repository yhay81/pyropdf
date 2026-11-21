"""pyropdf.typed: a view per object type of the Arlington PDF Model.

The views read keys as the model declares them, follow links to the views of
the linked types, and inherit through /Parent (DESIGN.md section 8).
tools/arlington/generate.py writes their stub, python/pyropdf/typed.pyi.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any, cast

import pikepdf
import pytest
from assets.broken.generate import pdf
from corpus import ASSETS

import pyropdf
from pyropdf import Array, Dictionary, Matrix, Name, Stream, typed

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


@pytest.mark.spec("page-objects", "read")
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


@pytest.mark.spec("page-tree", "interpret")
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


_SAMPLES: dict[str, object] = {
    "boolean": True,
    "integer": 1,
    "bitmask": 1,
    "number": 2.5,
    "string-text": "text",
    "string-ascii": "ascii",
    "date": "D:20261028000000Z",
    "string": b"bytes",
    "string-byte": b"bytes",
    "null": None,
    "rectangle": (0, 0, 10, 20),
    "matrix": (1, 0, 0, 1, 5, 6),
}


def _container(doc: pyropdf.Document, handle: type) -> Any:  # noqa: ANN401 - a handle of the given type.
    """Return a new indirect object that ``handle`` addresses."""
    if handle is Stream:
        return doc.objects[doc.objects.add_stream(b"data")]
    return doc.objects[doc.objects.add([] if handle is Array else {})]


def _sample(doc: pyropdf.Document, kind: str, links: tuple[str, ...], names: frozenset[str] | None) -> object:
    """Return a value that a key writes as ``kind``."""
    if kind in _SAMPLES:
        return _SAMPLES[kind]
    if kind == "name":
        return min(names) if names else "Example"
    # A tree links to the types of its values, not to its own.
    if links and kind not in {"name-tree", "number-tree"}:
        handles = {"array": Array, "stream": Stream}
        views = [getattr(typed, _class_name(link)) for link in links]
        view = next((view for view in views if view._handle is handles.get(kind, Dictionary)), views[0])  # noqa: SLF001
        return view(_container(doc, view._handle))  # noqa: SLF001
    return _container(doc, {"array": Array, "stream": Stream}.get(kind, Dictionary))


def _plain(value: object) -> object:
    """Return ``value`` with its direct arrays as lists, which compare by content."""
    if isinstance(value, Array) and not value.is_indirect:
        return [_plain(item) for item in value]
    return value


@pytest.mark.spec("arlington", "read", "edit")
@pytest.mark.parametrize("name", typed.TYPES)
def test_every_key_of_every_object_type_round_trips(name: str) -> None:
    doc = pyropdf.Document()
    view_type = getattr(typed, _class_name(name))
    obj = _container(doc, view_type._handle)  # noqa: SLF001
    view = view_type(obj)
    if isinstance(obj, Stream):
        assert obj.read() == b"data"
        assert obj.read(decode=False) != b"data"
    keys = cast("Any", typed)._keys(name)  # noqa: SLF001
    # A wildcard key stands for the keys and positions that no other key names.
    steps: dict[str, str | int]
    if isinstance(obj, Array):
        positions = {key: int(key.rstrip("*")) for key in keys if key != "*"}
        if "*" in keys:
            positions["*"] = max(positions.values(), default=-1) + 1
        obj.extend([None] * (max(positions.values(), default=-1) + 1))
        steps = dict(positions)
    else:
        steps = {key: "Undeclared" if key == "*" else key for key in keys}

    for key, described in keys.items():
        step = steps[key]
        for declared in described.types:
            view[step] = _sample(doc, declared.kind, declared.links, declared.names)
            stored = _plain(obj[step])
            value = view[step]
            view[step] = value
            assert _plain(obj[step]) == stored, (key, declared.kind)
        # The store writes a stream's /Length from its data.
        if isinstance(obj, Dictionary) and not (isinstance(obj, Stream) and key == "Length"):
            del view[step]
            assert step not in obj


# Each row fixes an expected result independently of the runtime descriptors.
# Every probe keeps the valid or malformed value in object 4; replacing that
# object exercises the versioned handle without reconstructing the typed view.
with (Path(__file__).parent / "fixtures" / "arlington-values.tsv").open(encoding="utf-8", newline="") as _file:
    _INTERPRET = {row["Object"]: row for row in csv.DictReader(_file, delimiter="\t")}

_PRIMITIVE_ONLY = {
    "ArrayOfBooleansGeneral",
    "ArrayOfIntegersGeneral",
    "ArrayOfNamesGeneral",
    "ArrayOfNumbersGeneral",
    "_UniversalArray",
    "_UniversalDictionary",
}


def test_standalone_interpretation_inventory_is_exhaustive() -> None:
    assert set(_INTERPRET).isdisjoint(_PRIMITIVE_ONLY)
    assert set(_INTERPRET) | _PRIMITIVE_ONLY == set(typed.TYPES)


def _probe(name: str, key: str, raw: bytes, reference: bytes = b"4 0 R") -> tuple[Any, str | int]:
    """Build a standalone holder independently of pyropdf's model and writer."""
    cls = getattr(typed, _class_name(name))
    step: str | int
    if issubclass(cls, typed.ArrayView):
        step = int(key.rstrip("*") or "0")
        suffix = b" 2 -100 100" if name == "LabRangeArray" else b""
        holder = b"[" + b"null " * step + reference + suffix + b"]"
    else:
        step = "Probe" if key == "*" else key
        extra = b" /max 2" if name == "OptContentZoom" else b""
        holder = b"<< /" + step.encode() + b" " + reference + extra + b" >>"
        if issubclass(cls, typed.StreamView):
            holder = holder[:-2] + b" /Length 0 >>\nstream\n\nendstream"
    catalog = (
        b"<< /Type /Catalog /Pages 2 0 R /Extensions << /AAPL << >> /OpenOffice << >> "
        b"/GTSm [<< /BaseVersion /2.0 /ExtensionLevel 1 >>] >> >>"
    )
    data = pdf([catalog, b"<< /Type /Pages /Kids [] /Count 0 >>", holder, raw], "2.0")
    doc = pyropdf.open(data)
    return cls(doc.objects[3]), step


@pytest.mark.spec("arlington-values", "interpret")
@pytest.mark.parametrize("name", _INTERPRET)
def test_standalone_entry_interprets_the_current_store_value(name: str) -> None:
    row = _INTERPRET[name]
    view, step = _probe(name, row["Key"], row["Value"].replace(r"\n", "\n").encode())
    value = view.interpret(step)
    expected = row["Expected"]
    if expected in typed.TYPES and row["Kind"] in {"dictionary", "stream", "array"}:
        assert isinstance(value, getattr(typed, _class_name(expected)))
        assert value.obj.xref == 4
    elif row["Kind"] in {"array", "dictionary", "stream", "name-tree", "number-tree"}:
        handles = {"array": Array, "dictionary": Dictionary, "stream": Stream}
        assert isinstance(value, handles[expected])
        assert value.xref == 4
    elif row["Kind"] in {"matrix", "rectangle"}:
        assert tuple(value) == tuple(map(float, expected.split()))
    elif row["Kind"] in {"integer", "number", "bitmask"}:
        assert type(value) in {int, pyropdf.Real}
        assert value == float(expected)
    elif row["Kind"] == "null":
        assert value is None
    elif row["Kind"] == "boolean":
        assert value is (expected == "true")
    elif row["Kind"] in {"string", "string-byte"}:
        assert isinstance(value, pyropdf.String)
        assert bytes(value) == expected.encode()
    else:
        assert value == expected
        assert isinstance(value, Name) == (row["Kind"] == "name")
    broken, address = _probe(name, row["Key"], row["Value"].replace(r"\n", "\n").encode(), b"4 1 R")
    with pytest.raises(pyropdf.MalformedPdfError, match="unresolved reference"):
        broken.interpret(address)
    invalid = {"false": False, "-9": -9, "/Bogus": Name("Bogus")}
    view.obj[step] = invalid[row["Invalid"]]
    with pytest.raises(pyropdf.PdfError):
        view.interpret(step)


def test_ambiguous_dictionary_and_array_links_do_not_guess_a_type() -> None:
    doc = pyropdf.Document()
    table = typed.ColorSpaceMap(doc.objects[doc.objects.add({"Paint": [Name("Lab"), {"WhitePoint": [1, 1, 1]}]})])
    assert isinstance(table.interpret("Paint"), typed.LabColorSpace)
    assert isinstance(table["Paint"], typed.LabColorSpace)
    table.obj["Paint"][0] = Name("CalGray")
    assert isinstance(table.interpret("Paint"), typed.CalGrayColorSpace)
    table.obj["Paint"][0] = Name("Bogus")
    with pytest.raises(pyropdf.PdfError, match="no unique linked object"):
        table.interpret("Paint")
    doc.history.undo()
    assert isinstance(table.interpret("Paint"), typed.CalGrayColorSpace)

    params = typed.ArrayOfDecodeParams(doc.objects[doc.objects.add([{}])])
    with pytest.raises(pyropdf.PdfError, match="no unique linked object"):
        params.interpret(0)
    params.obj[0] = {"Type": Name("CryptFilterDecodeParms")}
    assert isinstance(params.interpret(-1), typed.FilterCrypt)


@pytest.mark.parametrize(
    ("flags", "target"), [(0, typed.FieldBtnCheckbox), (1 << 15, typed.FieldBtnRadio), (1 << 16, typed.FieldBtnPush)]
)
def test_ambiguous_button_subtypes_use_inherited_field_flags(flags: int, target: type) -> None:
    doc = pyropdf.Document()
    parent = doc.objects[doc.objects.add({"FT": Name("Btn"), "Ff": flags})]
    child = doc.objects[doc.objects.add({"T": "Example", "Parent": parent})]
    fields = typed.ArrayOfFields(doc.objects[doc.objects.add([child])])
    assert isinstance(fields.interpret(0), target)


@pytest.mark.parametrize("ref", [b"60 0 R", b"4 1 R", b"4 0 R"])
def test_invalid_and_cyclic_indirect_targets_are_refused(ref: bytes) -> None:
    view, step = _probe("PageTreeNode", "Kids", b"4 0 R", ref)
    with pytest.raises(pyropdf.MalformedPdfError, match=r"unresolved reference|reference chain"):
        view.interpret(step)


def test_possible_values_and_types_follow_the_documents_effective_version() -> None:
    data = pdf(
        [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [] /Count 0 >>", b"[/FlateDecode]"], "1.1"
    )
    doc = pyropdf.open(data)
    filters = typed.ArrayOfFilterNames(doc.objects[3])
    with pytest.raises(pyropdf.PdfError, match="admitted types or values"):
        filters.interpret(0)
    doc.catalog["Version"] = Name("1.2")
    assert filters.interpret(0) == Name("FlateDecode")


def test_conditional_defaults_and_required_keys_use_the_current_holder() -> None:
    doc = pyropdf.Document()
    obj = doc.objects[doc.objects.add({"StateModel": "Review"})]
    note = typed.AnnotText(obj)
    assert note.interpret("State") == "None"
    obj["StateModel"] = "Marked"
    assert note.interpret("State") == "Unmarked"
    del obj["StateModel"]
    assert note.interpret("State") is None
    zoom = typed.OptContentZoom(doc.objects[doc.objects.add({"min": 2, "max": 1})])
    with pytest.raises(pyropdf.PdfError, match="admitted types or values"):
        zoom.interpret("max")
    zoom.obj["max"] = 3
    assert zoom.interpret("max") == 3

    annotation = typed.AnnotText(doc.objects[doc.objects.add({"Rect": [0, 0, 5, 5]})])
    doc.catalog["Version"] = Name("2.0")
    with pytest.raises(pyropdf.PdfError, match="required value is absent"):
        annotation.interpret("AP")


def test_repeating_predicates_use_the_actual_position_and_inheritance_has_a_budget() -> None:
    doc = pyropdf.Document()
    values = typed.ArrayOfIntegersGreaterThanZero(doc.objects[doc.objects.add([3, 2, -1])])
    assert values.interpret(1) == 2
    with pytest.raises(pyropdf.PdfError, match="admitted types or values"):
        values.interpret(2)
    obj = doc.objects[doc.objects.add({})]
    obj["Parent"] = obj
    with pytest.raises(pyropdf.LimitError) as caught:
        typed.PageObject(obj).interpret("Rotate")
    assert caught.value.code == "object_depth"


def test_typed_tree_lookup_resolves_multilevel_and_direct_leaf_handles() -> None:
    doc = pyropdf.Document()
    pages = doc.pages.new((100, 100))
    values = doc.objects[
        doc.objects.add([b"chart", [pages.obj, Name("Fit")], b"photo", [pages.obj, Name("XYZ"), None, None, 2]])
    ]
    child = doc.objects[doc.objects.add({"Limits": [b"chart", b"photo"], "Names": values})]
    names = typed.Name(doc.objects[doc.objects.add({"Dests": {"Kids": [child]}})])
    tree = names.interpret("Dests")
    assert isinstance(tree, typed.LinkedTree)
    assert isinstance(tree.lookup(b"chart"), typed.Dest0Array)
    result = tree.lookup(b"photo")
    assert isinstance(result, typed.DestXYZArray)
    assert result.interpret(4) == 2
    values[3] = [pages.obj, Name("FitH"), 15]
    updated = tree.lookup(b"photo")
    assert isinstance(updated, typed.Dest1Array)
    assert updated.interpret(2) == 15
    with pytest.raises(KeyError):
        tree.lookup(b"missing")
    with pytest.raises(TypeError):
        tree.lookup(0)
    values[1] = [pages.obj, Name("Unknown")]
    with pytest.raises(pyropdf.PdfError, match="no unique linked tree value"):
        tree.lookup(b"chart")

    doc.catalog["PageLabels"] = {"Nums": [0, {"S": Name("r")}, 1, {"S": Name("D")}]}
    labels = typed.Catalog(doc.catalog).interpret("PageLabels")
    label = labels.lookup(1)
    assert isinstance(label, typed.PageLabel)
    assert label.interpret("S") == Name("D")
    doc.catalog["PageLabels"]["Nums"][3]["S"] = Name("R")
    assert label.interpret("S") == Name("R")
    with pytest.raises(KeyError):
        labels.lookup(-1)
    with pytest.raises(TypeError):
        labels.lookup(b"0")
    doc.catalog["PageLabels"]["Nums"] = [0]
    with pytest.raises(pyropdf.PdfError, match="unpaired key"):
        labels.lookup(0)


def test_indirect_discriminators_and_defaults_agree_with_qpdf_after_reopening() -> None:
    # File bytes encode an indirect /ShadingType, an indirect /ColorSpace,
    # and a crop box default that ultimately inherits the page's media box.
    data = pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Count 1 /Kids [3 0 R] /MediaBox [0 0 100 200] >>",
            b"<< /Type /Page /Parent 2 0 R /Resources << /Shading << /Paint 4 0 R >> >> >>",
            b"<< /ShadingType 5 0 R /ColorSpace 6 0 R /Coords [0 0 1 1] /Function << /FunctionType 2 >> >>",
            b"2",
            b"/DeviceRGB",
        ],
        "2.0",
    )
    doc = pyropdf.open(data)
    page = typed.PageObject(doc.pages[0].obj)
    shadings = page.interpret("Resources").interpret("Shading")
    assert isinstance(shadings.interpret("Paint"), typed.ShadingType2)
    assert page.interpret("TrimBox") == (0, 0, 100, 200)
    doc.pages[0].obj["CropBox"] = [1, 2, 50, 60]
    saved = doc.to_bytes(incremental=True)
    reopened = pyropdf.open(saved)
    reread = typed.PageObject(reopened.pages[0].obj)
    with pikepdf.open(io.BytesIO(saved)) as oracle:
        paint = oracle.pages[0].Resources.Shading.Paint
        assert paint.objgen == (shadings.interpret("Paint").obj.xref, 0)
        assert int(paint.ShadingType) == reread.interpret("Resources").interpret("Shading").interpret(
            "Paint"
        ).interpret("ShadingType")
        assert str(paint.ColorSpace) == "/DeviceRGB"
        assert tuple(oracle.pages[0].CropBox) == reread.interpret("TrimBox")


def test_an_invalid_indirect_discriminator_cannot_select_a_link() -> None:
    data = pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Count 0 /Kids [] >>",
            b"<< /Paint [5 1 R << /WhitePoint [1 1 1] >>] >>",
            b"null",
            b"/Lab",
        ],
        "2.0",
    )
    colors = typed.ColorSpaceMap(pyropdf.open(data).objects[3])
    with pytest.raises(pyropdf.PdfError, match="no unique linked object"):
        colors.interpret("Paint")


def test_required_value_overrides_otherwise_admitted_alternatives() -> None:
    doc = pyropdf.Document()
    params = typed.CryptFilter(doc.objects[doc.objects.add({"CFM": Name("AESV3"), "Length": 16})])
    with pytest.raises(pyropdf.PdfError, match="admitted types or values"):
        params.interpret("Length")
    params.obj["Length"] = 32
    assert params.interpret("Length") == 32
    params.obj["CFM"] = Name("AESV2")
    with pytest.raises(pyropdf.PdfError, match="admitted types or values"):
        params.interpret("Length")


def test_a_single_link_still_requires_its_own_discriminator() -> None:
    doc = pyropdf.Document()
    obj = doc.objects[doc.objects.add({"Pages": {"Type": Name("Font"), "Count": 0, "Kids": []}})]
    with pytest.raises(pyropdf.PdfError, match="no unique linked object type"):
        typed.Catalog(obj).interpret("Pages")


def test_a_later_required_value_allows_an_earlier_declared_extension() -> None:
    path = Path(__file__).parent / "assets/encrypted/user-aes-256-object-streams.pdf"
    doc = pyropdf.open(path)
    doc.unlock("userpw")
    assert doc.catalog["Extensions"]["ADBE"]["ExtensionLevel"] == 8
    assert typed.EncryptionStandard(doc.trailer["Encrypt"]).interpret("R") == 6
