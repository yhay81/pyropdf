# ruff: noqa: S106 - the passwords are test data.
"""The low-level object model: ``doc.objects``, the trailer, and the catalog.

pylopdf's tests/test_objects.py at 884af0db, ported. Intended differences
from pylopdf:

- Reals read as :class:`pyropdf.Real`, a ``float`` that keeps its lexeme
  (#11), and an assigned ``Real`` or ``Decimal`` writes its exact digits.
- Text extraction and ``doc.metadata`` come later, so the tests read content
  streams and the Info dictionary instead.
- A handle stays valid across saves, since pyropdf never renumbers objects in
  place; it goes stale when its object leaves the current version, as undo
  of the object's addition does, and works again on redo.
- Reordering ``/Kids`` keeps a :class:`Page` naming its page object, with its
  new index; pylopdf made the page stale.
"""

from __future__ import annotations

import pickle
import zlib
from decimal import Decimal
from pathlib import Path

import pytest

import pyropdf
from pyropdf import Array, Dictionary, Name, Real, Stream, String

REAL_WORLD = Path(__file__).parent / "assets" / "real_world"


def build_raw_pdf(objects: dict[int, str]) -> bytes:
    """Return a PDF of ``objects`` by number, with a correct cross-reference table."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode()
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 00000 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def build_pdf(texts: list[str]) -> bytes:
    """Return pylopdf's test document: one page per text, with MediaBox and Resources on the tree's root."""
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(texts)))
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {len(texts)} /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 3 0 R >> >> >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for i, text in enumerate(texts):
        content = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET"
        objects[4 + 2 * i] = f"<< /Type /Page /Parent 2 0 R /Contents {5 + 2 * i} 0 R >>"
        objects[5 + 2 * i] = f"<< /Length {len(content)} >>\nstream\n{content}\nendstream"
    return build_raw_pdf(objects)


def _two_pages() -> pyropdf.Document:
    return pyropdf.open(build_pdf(["first", "second"]))


def _content(page: pyropdf.Page) -> bytes:
    contents = page.obj["Contents"]
    assert isinstance(contents, Stream)
    return contents.read()


def test_values_map_to_python_types() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /Extra 3 0 R >>",
                2: "<< /Type /Pages /Kids [] /Count 0 >>",
                3: "[null true 7 -2.5 /Na#6De (lit) <414243> [1] << /K 1 >> 4 0 R 99 0 R]",
                4: "<< /Length 2 >>\nstream\nab\nendstream",
            }
        )
    )

    values = doc.objects[3]

    assert isinstance(values, Array)
    assert values[0] is None
    assert values[1] is True
    assert values[2] == 7
    assert isinstance(values[2], int)
    assert values[3] == -2.5
    assert isinstance(values[3], Real)
    assert isinstance(values[3], float)
    assert values[4] == Name("Name")
    assert isinstance(values[4], Name)
    assert values[5] == String(b"lit")
    assert values[6] == b"ABC"
    assert isinstance(values[6], String)
    assert isinstance(values[7], Array)
    assert not values[7].is_indirect
    assert isinstance(values[8], Dictionary)
    assert values[8]["K"] == 1
    assert isinstance(values[9], Stream)
    assert values[9].xref == 4
    # ISO 32000-2 section 7.3.10: a reference to a missing object reads as null.
    assert values[10] is None
    assert len(values) == 11


def test_trailer_catalog_and_page_objects() -> None:
    doc = _two_pages()

    assert doc.trailer.xref is None
    assert "Root" in doc.trailer
    assert doc.catalog == doc.trailer["Root"]
    assert doc.catalog.xref == 1
    assert doc.catalog["Type"] == "Catalog"
    assert doc.pages[0].xref == 4
    assert doc.pages[-1].xref == 6
    assert doc.pages[1].obj == doc.objects[6]
    assert doc.pages[1].obj["Type"] == Name("Page")
    assert doc.catalog["Pages"]["Kids"][1] == doc.pages[1].obj


def test_objects_mapping() -> None:
    doc = _two_pages()

    numbers = list(doc.objects)

    assert numbers == [1, 2, 3, 4, 5, 6, 7]
    assert len(doc.objects) == len(numbers)
    assert 1 in doc.objects
    assert 10_000 not in doc.objects
    assert "1" not in doc.objects
    with pytest.raises(KeyError):
        doc.objects[10_000]
    with pytest.raises(KeyError):
        doc.objects[2**40]
    with pytest.raises(TypeError):
        doc.objects["1"]  # ty: ignore[invalid-argument-type]


def test_dictionary_writes_take_effect_immediately() -> None:
    doc = _two_pages()
    page = doc.pages[0]

    page.obj["Rotate"] = 90
    page.obj["UserUnit"] = 1.5
    del page.obj["UserUnit"]

    assert page.rotation == 90
    assert "UserUnit" not in page.obj
    with pytest.raises(KeyError):
        del page.obj["UserUnit"]
    reopened = pyropdf.open(doc.to_bytes())
    assert reopened.pages[0].rotation == 90


def test_every_write_is_one_step_and_a_transaction_groups_them() -> None:
    doc = _two_pages()

    doc.catalog["A"] = 1
    with doc.transaction("both"):
        doc.catalog["B"] = 2
        doc.catalog["C"] = 3
        assert doc.catalog["B"] == 2

    assert doc.history.steps == ("", "both")
    doc.history.undo()
    assert "B" not in doc.catalog
    assert "C" not in doc.catalog
    assert doc.catalog["A"] == 1


def test_stream_write_compresses_and_reads_back() -> None:
    doc = _two_pages()
    contents = doc.pages[0].obj["Contents"]
    assert isinstance(contents, Stream)

    contents.write(b"BT /F1 12 Tf 72 720 Td (replaced) Tj ET")

    assert contents["Filter"] == "FlateDecode"
    assert contents.read() == b"BT /F1 12 Tf 72 720 Td (replaced) Tj ET"
    assert zlib.decompress(contents.read(decode=False)) == contents.read()
    assert _content(pyropdf.open(doc.to_bytes()).pages[0]) == b"BT /F1 12 Tf 72 720 Td (replaced) Tj ET"


def test_stream_write_without_a_filter_stores_the_data() -> None:
    doc = _two_pages()
    contents = doc.pages[0].obj["Contents"]
    assert isinstance(contents, Stream)

    contents.write(b"BT /F1 12 Tf 72 720 Td (plain) Tj ET", filter=None)

    assert "Filter" not in contents
    assert contents["Length"] == len(b"BT /F1 12 Tf 72 720 Td (plain) Tj ET")
    assert contents.read(decode=False) == contents.read()
    assert _content(pyropdf.open(doc.to_bytes()).pages[0]) == b"BT /F1 12 Tf 72 720 Td (plain) Tj ET"
    with pytest.raises(ValueError, match="filter"):
        contents.write(b"x", filter="LZWDecode")


def test_stream_read_is_bounded() -> None:
    doc = pyropdf.Document()
    xref = doc.objects.add_stream(b"x" * 10_000)
    stream = doc.objects[xref]
    assert isinstance(stream, Stream)

    with pytest.raises(pyropdf.LimitError) as error:
        stream.read(max_size=100)
    assert error.value.code == "stream_data_size"
    with pytest.raises(pyropdf.LimitError):
        stream.read(decode=False, max_size=10)
    assert len(stream.read(max_size=None)) == 10_000
    with pytest.raises(ValueError, match="max_size"):
        stream.read(max_size=0)


def test_stream_read_refuses_a_chain_of_more_filters_than_the_limit() -> None:
    doc = pyropdf.Document()
    data = b"x"
    for _ in range(16):
        data = zlib.compress(data)
    stream = doc.objects[doc.objects.add_stream(data, filter=None)]
    assert isinstance(stream, Stream)

    stream["Filter"] = [Name("FlateDecode")] * 16
    assert stream.read() == b"x"
    stream["Filter"] = [Name("FlateDecode")] * 17
    with pytest.raises(pyropdf.LimitError) as error:
        stream.read()
    assert error.value.code == "stream_filter_count"


def test_unsupported_filters_raise_instead_of_returning_encoded_bytes() -> None:
    doc = pyropdf.Document()
    xref = doc.objects.add_stream(b"data", filter=None)
    doc.objects[xref]["Filter"] = Name("JBIG2Decode")

    with pytest.raises(pyropdf.PdfError):
        doc.objects[xref].read()
    assert doc.objects[xref].read(decode=False) == b"data"


def test_add_builds_a_new_page_from_raw_objects() -> None:
    doc = pyropdf.Document()
    doc.pages.new()
    font = doc.objects.add({"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Helvetica")})
    content = doc.objects.add_stream(b"BT /F9 24 Tf 72 72 Td (raw) Tj ET")

    doc.pages[0].obj["Resources"] = {"Font": {"F9": doc.objects[font]}}
    doc.pages[0].obj["Contents"] = doc.objects[content]

    assert _content(doc.pages[0]) == b"BT /F9 24 Tf 72 72 Td (raw) Tj ET"
    # An indirect handle stored inside a container is a reference.
    assert doc.pages[0].obj["Resources"]["Font"]["F9"].xref == font
    saved = doc.to_bytes()
    assert b"/F9 " + str(font).encode() + b" 0 R" in saved
    assert _content(pyropdf.open(saved).pages[0]) == b"BT /F9 24 Tf 72 72 Td (raw) Tj ET"


def test_assigning_a_handle_at_top_level_copies_it() -> None:
    doc = _two_pages()
    copy = doc.objects.add(doc.catalog)

    assert copy != doc.catalog.xref
    assert doc.objects[copy]["Type"] == "Catalog"
    doc.objects[copy]["Type"] = Name("Changed")
    assert doc.catalog["Type"] == "Catalog"

    target = doc.objects.add(0)
    doc.objects[target] = doc.objects[copy]
    assert dict(doc.objects[target]) == dict(doc.objects[copy])


def test_direct_handles_are_copied() -> None:
    doc = _two_pages()
    media_box = doc.catalog["Pages"]["MediaBox"]
    assert isinstance(media_box, Array)
    assert not media_box.is_indirect

    doc.pages[0].obj["MediaBox"] = media_box
    doc.pages[0].obj["MediaBox"][2] = 300

    assert list(doc.catalog["Pages"]["MediaBox"]) == [0, 0, 612, 792]
    assert list(doc.pages[0].obj["MediaBox"]) == [0, 0, 300, 792]


def test_scalar_objects_read_as_values_and_can_be_replaced() -> None:
    doc = pyropdf.Document()
    xref = doc.objects.add(42)

    assert doc.objects[xref] == 42
    doc.objects[xref] = Name("Answer")
    assert doc.objects[xref] == Name("Answer")
    with pytest.raises(KeyError):
        doc.objects[10_000] = 1


def test_text_strings() -> None:
    doc = pyropdf.Document()
    doc.trailer["Info"] = {"Title": "日本語のタイトル", "Author": "ASCII", "Raw": b"\x00\xff"}

    info = doc.trailer["Info"]

    assert pyropdf.probe(doc.to_bytes()).metadata["title"] == "日本語のタイトル"
    assert info["Title"].text == "日本語のタイトル"
    assert info["Title"].startswith(b"\xfe\xff")
    assert info["Author"] == b"ASCII"
    assert info["Raw"] == b"\x00\xff"
    assert String(b"\xef\xbb\xbfUTF-8").text == "UTF-8"
    assert String(b"caf\xe9").text == "café"


def test_names_round_trip_and_reject_a_leading_slash() -> None:
    doc = pyropdf.Document()
    xref = doc.objects.add([Name("A#B"), Name("日本"), Name("a\udcff")])

    assert list(doc.objects[xref]) == [Name("A#B"), Name("日本"), Name("a\udcff")]
    assert list(pyropdf.open(doc.to_bytes()).objects[xref]) == [Name("A#B"), Name("日本"), Name("a\udcff")]
    with pytest.raises(ValueError, match="leading slash"):
        Name("/Type")
    with pytest.raises(ValueError, match="leading slash"):
        doc.catalog["/Type"]
    assert repr(Name("Type")) == "Name('Type')"


def test_a_str_value_is_a_text_string_not_a_name() -> None:
    doc = pyropdf.Document()
    doc.catalog["PageMode"] = "UseOutlines"

    assert isinstance(doc.catalog["PageMode"], String)
    assert b"/PageMode (UseOutlines)" in doc.to_bytes()


def test_array_editing() -> None:
    doc = pyropdf.Document()
    array = doc.objects[doc.objects.add([1, 2, 3])]
    assert isinstance(array, Array)

    array.append(4)
    array.insert(0, 0)
    array.insert(-1, 3.5)
    array[1] = Name("one")
    del array[2]
    array.extend([5, 6])

    assert list(array) == [0, Name("one"), 3, 3.5, 4, 5, 6]
    assert array[-1] == 6
    assert array[1:3] == [Name("one"), 3]
    with pytest.raises(IndexError):
        array[100]
    with pytest.raises(TypeError):
        array[0:1] = [1]


def test_reordering_kids_keeps_pages_naming_their_objects() -> None:
    doc = _two_pages()
    page = doc.pages[0]
    kids = doc.catalog["Pages"]["Kids"]

    kids[0], kids[1] = kids[1], kids[0]

    assert page.index == 1
    assert b"(first)" in _content(page)
    assert b"(second)" in _content(doc.pages[0])


def test_writes_that_keep_the_page_order_keep_pages() -> None:
    doc = _two_pages()
    page = doc.pages[1]

    doc.catalog["PageLayout"] = Name("TwoColumnLeft")
    doc.objects.add({"Unused": True})

    assert page.index == 1
    assert b"(second)" in _content(page)


def test_handles_survive_saves_and_go_stale_with_their_object() -> None:
    doc = _two_pages()
    pages = doc.catalog["Pages"]
    added = doc.objects[doc.objects.add({"Unreferenced": True})]

    doc.to_bytes()
    assert pages["Count"] == 2
    assert added["Unreferenced"] is True

    doc.history.undo()
    with pytest.raises(pyropdf.StaleObjectError):
        added["Unreferenced"]
    assert pages["Count"] == 2
    doc.history.redo()
    assert added["Unreferenced"] is True


def test_direct_handles_go_stale_when_their_container_is_replaced() -> None:
    doc = _two_pages()
    resources = doc.catalog["Pages"]["Resources"]
    assert not resources.is_indirect

    doc.catalog["Pages"]["Resources"] = 0

    with pytest.raises(pyropdf.StaleObjectError):
        len(resources)


def test_handles_from_another_document_are_refused() -> None:
    doc = _two_pages()
    other = _two_pages()

    with pytest.raises(ValueError, match="another document"):
        doc.catalog["Other"] = other.catalog


def test_unsupported_values_and_depth_are_refused() -> None:
    doc = pyropdf.Document()
    nested: list[object] = []
    deep = nested
    for _ in range(40):
        inner: list[object] = []
        deep.append(inner)
        deep = inner

    with pytest.raises(TypeError, match="cannot write"):
        doc.catalog["Bad"] = object()
    with pytest.raises(ValueError, match="finite"):
        doc.catalog["Bad"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        doc.catalog["Bad"] = Decimal("Infinity")
    with pytest.raises(pyropdf.LimitError) as error:
        doc.catalog["Deep"] = nested
    assert error.value.code == "object_value_depth"
    assert "Bad" not in doc.catalog
    assert "Deep" not in doc.catalog
    cycle: list[object] = []
    cycle.append(cycle)
    with pytest.raises(pyropdf.LimitError):
        doc.catalog["Cycle"] = cycle


def test_handles_compare_and_hash_by_address() -> None:
    doc = _two_pages()

    assert doc.catalog == doc.objects[1]
    assert hash(doc.catalog) == hash(doc.objects[1])
    assert doc.catalog["Pages"]["Kids"] == doc.objects[2]["Kids"]
    assert doc.catalog != doc.objects[2]
    assert repr(doc.objects[2]["Kids"]) == "<Array 2 0 R/Kids>"
    assert repr(doc.trailer) == "<Dictionary trailer>"


def test_closed_and_encrypted_documents_are_refused() -> None:
    doc = _two_pages()
    catalog = doc.catalog
    encrypted = pyropdf.open(doc.to_bytes(encryption=pyropdf.Encryption(user_password="secret")))
    doc.close()

    with pytest.raises(pyropdf.DocumentClosedError):
        catalog["Type"]
    with pytest.raises(pyropdf.EncryptedDocumentError):
        encrypted.objects  # noqa: B018 - property access is the operation under test.


def test_objects_are_readable_after_unlocking() -> None:
    encrypted = pyropdf.open(
        _two_pages().to_bytes(encryption=pyropdf.Encryption(user_password="user", owner_password="owner"))
    )
    with pytest.raises(pyropdf.EncryptedDocumentError):
        encrypted.catalog  # noqa: B018 - property access is the operation under test.

    assert encrypted.unlock("user") is pyropdf.Access.USER
    assert encrypted.catalog["Pages"]["Count"] == 2


def test_real_world_object_graph_reads() -> None:
    doc = pyropdf.open(REAL_WORLD / "f1040.pdf")
    fonts = 0
    for xref in doc.objects:
        value = doc.objects[xref]
        if isinstance(value, Dictionary) and value.get("Type") == "Font":
            fonts += 1
            assert isinstance(value["BaseFont"], Name)
    assert fonts > 0
    form = doc.catalog["AcroForm"]
    assert len(form["Fields"]) == len({field.xref for field in form["Fields"]})


def test_reals_keep_their_digits_through_assignment_and_copies() -> None:
    source = b"35.68123456789012345"
    doc = pyropdf.Document()
    other = pyropdf.Document()
    real = Real(source)

    doc.catalog["GPTS"] = [real, Decimal("0.0000001"), 0.1, Decimal(7)]
    copied = pickle.loads(pickle.dumps(doc.catalog["GPTS"][0]))  # noqa: S301 - the test's own bytes.
    other.catalog["Copy"] = real

    assert b"/GPTS [35.68123456789012345 0.0000001 0.1 7.0]" in doc.to_bytes()
    assert b"/Copy 35.68123456789012345" in other.to_bytes()
    assert real == float(source)
    assert hash(real) == hash(float(source))
    assert real.to_decimal() == Decimal(source.decode())
    assert real + 1 == float(source) + 1
    assert type(real + 1) is float
    assert repr(real) == "Real('35.68123456789012345')"
    assert pickle.loads(pickle.dumps(real)).lexeme == source  # noqa: S301 - the test's own bytes.
    # Until I7 (#16) a real read from bytes has no lexeme and writes its float.
    assert copied.lexeme is None
    assert copied == float(source)


def test_an_unchanged_real_assigned_back_writes_its_value() -> None:
    doc = pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R /Matrix [0.1 2.5 -3.25 1234567.891 .000123] >>",
                2: "<< /Type /Pages /Kids [] /Count 0 >>",
            }
        )
    )
    matrix = doc.catalog["Matrix"]
    values = list(matrix)

    for at, value in enumerate(values):
        matrix[at] = value

    assert list(pyropdf.open(doc.to_bytes()).catalog["Matrix"]) == values


def test_real_construction() -> None:
    assert Real(1.5).lexeme is None
    assert Real(1.5).to_decimal() == Decimal("1.5")
    assert Real("-.5").lexeme == b"-.5"
    assert Real(Decimal(12)).lexeme == b"12.0"
    assert Real(Real("1.50")).lexeme == b"1.50"
    assert Real("1.0") == Real("1.00")
    with pytest.raises(ValueError, match="not a PDF real"):
        Real("1e5")
    with pytest.raises(ValueError, match="finite"):
        Real(float("inf"))
    with pytest.raises(TypeError):
        Real(True)  # noqa: FBT003
