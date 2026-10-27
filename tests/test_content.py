"""Operator-level content: Page.operations and Operations.

Operations are tokenized as hayro tokenizes them and keep their bytes, so
writing back every content stream of the corpus changes nothing, and new
operations are written from their values. The reading cases of pylopdf's
tests/test_operations.py at 036be111cafe77755db4bb2be17bc70a8f2f9d82, ported.
Intended differences from pylopdf:

- ``Page.operations`` reads only; assigning it is yhay81/pyropdf#107.
- A real operand reads as a :class:`Real`, a ``float`` subclass, as reals
  of the object model do.
- A page whose object is deleted refuses with ``StalePageError``; a page
  stays valid through edits of other pages.
- Comparisons of text and pixels after writing back wait for text
  extraction (yhay81/pyropdf#98) and for assigning operations.
"""

from __future__ import annotations

import random
import zlib
from decimal import Decimal
from pathlib import Path

import pytest

import pyropdf
from pyropdf import DocumentLimits, InlineImage, Name, Operation, Operations, Real, String

REAL_WORLD = Path(__file__).parent / "assets" / "real_world"
CORPUS = sorted(REAL_WORLD.glob("*.pdf"))


def build_raw_pdf(objects: dict[int, str | bytes]) -> bytes:
    """Return a PDF of ``objects`` by number, with a correct cross-reference table."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        body = objects[number]
        out += f"{number} 0 obj\n".encode()
        out += body.encode() if isinstance(body, str) else body
        out += b"\nendobj\n"
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 00000 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _page(content: bytes, *, filtered: bool = False) -> bytes:
    data = zlib.compress(content) if filtered else content
    filter_entry = b" /Filter /FlateDecode" if filtered else b""
    return build_raw_pdf({
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 300 300] >>",
        3: "<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        5: b"<< /Length %d%s >>\nstream\n%s\nendstream" % (len(data), filter_entry, data),
    })  # fmt: skip


def _content_streams(doc: pyropdf.Document) -> list[bytes]:
    """Return the decoded data of every page content stream and Form XObject."""
    streams = []
    for xref in doc.objects:
        value = doc.objects[xref]
        if isinstance(value, pyropdf.Stream) and value.get("Subtype") == "Form":
            streams.append(value.read(max_size=None))
    for page in doc.pages:
        contents = page.obj.get("Contents")
        if isinstance(contents, pyropdf.Stream):
            streams.append(contents.read(max_size=None))
        elif isinstance(contents, pyropdf.Array):
            streams.extend(item.read(max_size=None) for item in contents if isinstance(item, pyropdf.Stream))
    return streams


def _joined_content(page: pyropdf.Page) -> bytes:
    """Return the page's content as hayro joins it: each stream of an array followed by a space."""
    contents = page.obj.get("Contents")
    if isinstance(contents, pyropdf.Stream):
        return contents.read(max_size=None)
    out = b""
    if isinstance(contents, pyropdf.Array):
        for item in contents:
            if not isinstance(item, pyropdf.Stream):
                break
            out += item.read(max_size=None) + b" "
    return out


@pytest.mark.parametrize("path", CORPUS, ids=lambda path: path.name)
def test_every_corpus_content_stream_round_trips(path: Path) -> None:
    doc = pyropdf.open(path)
    streams = _content_streams(doc)
    assert streams
    for data in streams:
        operations = Operations.parse(data, max_operations=None, max_size=None)
        assert operations.to_bytes() == data


@pytest.mark.parametrize("path", CORPUS, ids=lambda path: path.name)
def test_every_corpus_page_reads_its_joined_content(path: Path) -> None:
    doc = pyropdf.open(path)
    for page in doc.pages:
        operations = page.operations
        assert operations.to_bytes() == _joined_content(page)
        assert operations == Operations.parse(_joined_content(page), max_operations=None, max_size=None)


def test_operations_hold_hayro_values() -> None:
    content = b"q 1 0 0 1 10.5 -2 cm BT /F1 12 Tf [(A) -120 <42>] TJ ET Q /P <</MCID 3 /Lang (en)>> BDC EMC"
    operations = Operations.parse(content)

    assert [operation.operator for operation in operations] == [
        "q", "cm", "BT", "Tf", "TJ", "ET", "Q", "BDC", "EMC",
    ]  # fmt: skip
    assert operations[1].operands == (1, 0, 0, 1, 10.5, -2)
    assert isinstance(operations[1].operands[0], int)
    assert isinstance(operations[1].operands[4], Real)
    assert operations[3].operands == (Name("F1"), 12)
    assert isinstance(operations[3].operands[0], Name)
    assert operations[4].operands == ((String(b"A"), -120, String(b"B")),)
    assert operations[7].operands == (Name("P"), {"MCID": 3, "Lang": String(b"en")})
    assert operations.to_bytes() == content


def test_comments_and_white_space_stay_with_the_operation_that_follows() -> None:
    content = b"% header\nq\n  % inside\n  0 0 m\n\nQ\n% end\n"
    operations = Operations.parse(content)

    assert [operation.operator for operation in operations] == ["q", "m", "Q"]
    assert operations.tail == b"\n% end\n"
    assert operations.to_bytes() == content
    assert Operations([operations[0], operations[2]], tail=operations.tail).to_bytes() == b"% header\nq\n\nQ\n% end\n"


def test_inline_images_keep_their_dictionary_and_data() -> None:
    data = bytes(range(256))[:12]
    content = b"q 12 0 0 1 0 0 cm BI /W 4 /H 1 /BPC 8 /CS /RGB ID " + data + b" EI Q"
    operations = Operations.parse(content)

    image = operations[2]
    assert isinstance(image, InlineImage)
    assert image.dictionary == {"W": 4, "H": 1, "BPC": 8, "CS": Name("RGB")}
    assert image.data == data
    assert operations[3].operator == "Q"
    assert operations.to_bytes() == content


def test_filtered_inline_images_are_read() -> None:
    # lopdf's strict parse refuses filtered inline images; hayro reads them.
    encoded = b'8;Z]"!!~>'
    content = b"BI /W 1 /H 1 /BPC 8 /CS /G /F /A85 ID " + encoded + b"\nEI Q"
    operations = Operations.parse(content)

    assert isinstance(operations[0], InlineImage)
    assert operations[0].dictionary["F"] == Name("A85")
    assert operations[1].operator == "Q"


def test_new_operations_are_written_from_their_values() -> None:
    operations = Operations([
        Operation("q"),
        Operation("cm", (1, 0, 0, 1, 0.25, -1e-7)),
        Operation("Tf", (Name("F 1#"), 9.5)),
        Operation("Tj", (String(b"a(b)\\c\n\xff"),)),
        Operation("TJ", ([String(b"x"), -250.0, True, None],)),
        Operation("BDC", (Name("Span"), {"ActualText": "é"})),
        InlineImage({"W": 1, "H": 1, "BPC": 8, "CS": Name("G")}, b"\x80"),
        Operation("Q"),
    ])  # fmt: skip
    data = operations.to_bytes()

    assert b"0.25 -0.0000001 cm" in data
    assert b"/F#201#23 9.5 Tf" in data
    assert b"(a\\(b\\)\\\\c\\012\\377) Tj" in data
    assert b"[(x) -250 true null] TJ" in data
    reread = Operations.parse(data)
    # A plain str is written as a text string, which reads back as a String.
    expected = [
        *operations[:4],
        Operation("TJ", ((String(b"x"), -250, True, None),)),
        Operation("BDC", (Name("Span"), {"ActualText": String(b"\xfe\xff\x00\xe9")})),
        *operations[6:],
    ]
    assert reread == Operations(expected)


def test_reals_write_their_lexemes_and_decimals_their_digits() -> None:
    data = Operations([Operation("w", (Real("0.50"), Decimal("1.25"), 2.5))]).to_bytes()

    assert data == b"0.50 1.25 2.5 w"


def test_operators_may_hold_any_regular_character() -> None:
    # pdf.js issue13999.pdf draws with an operator that is a control byte.
    operations = Operations.parse(b"q \x14 Q")

    assert [operation.operator for operation in operations] == ["q", "\x14", "Q"]
    assert Operations([Operation("\x14"), Operation("\udcff")]).to_bytes() == b"\x14\n\xff"


def test_operations_are_values() -> None:
    operation = Operation("re", [0, 0, 10, 10])  # ty: ignore[invalid-argument-type]

    assert operation.operands == (0, 0, 10, 10)
    assert operation == Operation("re", (0, 0, 10, 10))
    assert hash(operation) == hash(Operation("re", (0, 0, 10, 10)))
    with pytest.raises(AttributeError):
        operation.operator = "m"  # ty: ignore[invalid-assignment]
    assert Operations.parse(b"0 0 m")[0] == Operation("m", (0, 0))


@pytest.mark.parametrize(
    ("operator", "operands", "error"),
    [
        ("", (), ValueError),
        ("T j", (), ValueError),
        ("/Tj", (), ValueError),
        ("BI", (), ValueError),
        (b"Tj", (), TypeError),
        ("Tj", "text", TypeError),
        ("re", (float("nan"),), ValueError),
        ("Do", (object(),), TypeError),
        ("BDC", ({1: 2},), TypeError),
    ],
)
def test_operations_refuse_what_cannot_be_written(operator: object, operands: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        Operation(operator, operands)  # ty: ignore[invalid-argument-type]


def test_deep_operands_are_refused() -> None:
    value: object = 1
    for _ in range(65):
        value = [value]
    with pytest.raises(pyropdf.LimitError) as error:
        Operation("Do", (value,))
    assert error.value.code == "object_value_depth"


def test_operands_as_deep_as_hayro_reads_them_are_read() -> None:
    content = b"[" * 64 + b"]" * 64 + b" Do"
    operations = Operations.parse(content)

    assert [operation.operator for operation in operations] == ["Do"]
    assert operations.to_bytes() == content


def test_content_after_what_hayro_reads_stays_in_the_tail() -> None:
    # hayro reads at most ten operands and ignores the rest of the stream.
    content = b"q 1 2 3 4 5 6 7 8 9 10 11 sc Q"
    operations = Operations.parse(content)

    assert [operation.operator for operation in operations] == ["q"]
    assert operations.tail == b" 1 2 3 4 5 6 7 8 9 10 11 sc Q"
    assert operations.to_bytes() == content


#: Malformed inline images, which pylopdf's fuzz run found raising ``ValueError``:
#: ``BI`` without ``ID``, ``ID`` without ``EI``, and data cut short.
MALFORMED_INLINE_IMAGES = [
    b"BI /W 1 /H 1 ID \x00 Q",
    b"q BI /W 1 /H 1 Q",
    b"q BI /W 1 /H 1",
    b"BI /W 1 /H 1 ID \x00\x01",
    b"q BI /W 2 /H 2 /BPC 8 /CS /G ID \x00",
    b"BI /W 4 /H 4 /BPC 8 /CS /G ID \x00 EI Q",
    b"BI BI ID x EI EI",
    b"BI\nQ BI /W 1 ID",
]


@pytest.mark.parametrize("content", MALFORMED_INLINE_IMAGES)
def test_malformed_inline_images_read_and_write_back(content: bytes) -> None:
    operations = Operations.parse(content)

    assert operations.to_bytes() == content
    assert Operations.parse(operations.to_bytes()) == operations
    page = pyropdf.open(_page(content)).pages[0]
    assert page.operations == operations


def test_a_bi_that_names_no_image_is_read_as_an_operation() -> None:
    operations = Operations.parse(b"BI /W 1 /H 1 ID \x00 Q")

    assert [operation.operator for operation in operations] == ["BI", "Q"]
    assert operations[0] == Operations.parse(b"BI /W 1 /H 1 ID \x00 Q")[0]
    assert not isinstance(operations[0], InlineImage)
    # A program still writes an inline image as InlineImage.
    with pytest.raises(ValueError, match="write an inline image as InlineImage"):
        Operation("BI", ())
    # The operation read keeps its bytes among new operations.
    edited = Operations([Operation("q"), *operations, Operation("Q")])
    assert edited.to_bytes() == b"q\nBI /W 1 /H 1 ID \x00 Q\nQ"


def test_content_around_inline_images_reads_without_raising() -> None:
    # A seeded sweep over the tokens around inline images: whatever hayro
    # reads writes back as the same bytes and reads as the same operations.
    tokens = [b"BI", b"ID", b"EI", b"/W", b"1", b"\x00", b"q", b"Q", b"\n", b" ", b"(", b")", b"<<", b">>", b"%"]
    draw = random.Random(113)  # noqa: S311 - a reproducible sweep, not a secret
    for _ in range(2000):
        content = b" ".join(draw.choice(tokens) for _ in range(draw.randrange(1, 12)))
        operations = Operations.parse(content)
        assert operations.to_bytes() == content, content
        assert Operations.parse(operations.to_bytes()) == operations, content


def test_an_escaped_operator_is_refused() -> None:
    with pytest.raises(pyropdf.MalformedPdfError, match="# escape"):
        Operations.parse(b"q T#6a Q")


def test_parse_limits() -> None:
    with pytest.raises(pyropdf.LimitError) as error:
        Operations.parse(b"q Q q Q", max_operations=3)
    assert error.value.code == "content_operation_count"
    assert len(Operations.parse(b"q Q q Q", max_operations=4)) == 4
    with pytest.raises(pyropdf.LimitError) as error:
        Operations.parse(b"q Q", max_size=2)
    assert error.value.code == "content_input_size"
    with pytest.raises(ValueError, match="max_size"):
        Operations.parse(b"q", max_size=0)


def test_page_operations_join_streams_as_hayro_does() -> None:
    objects: dict[int, str | bytes] = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 300 300] >>",
        3: "<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> /Contents [5 0 R 6 0 R 7 0 R] >>",
        4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        5: "<< /Length 21 >>\nstream\nBT /F1 12 Tf 9 9 Td (\nendstream",
        6: "<< /Length 7 >>\nstream\nA) Tj ET\nendstream",
        # hayro stops at the first entry that is not a stream.
        7: "<< /Type /NotAStream >>",
    }
    doc = pyropdf.open(build_raw_pdf(objects))
    operations = doc.pages[0].operations

    assert operations.to_bytes() == b"BT /F1 12 Tf 9 9 Td ( A) Tj ET "
    assert operations[3] == Operation("Tj", (String(b" A"),))


def test_page_operations_read_the_current_version() -> None:
    doc = pyropdf.open(_page(b"BT /F1 12 Tf 20 250 Td (Keep) Tj ET", filtered=True))
    page = doc.pages[0]
    before = page.operations
    page.obj["Contents"].write(b"q 0 0 10 10 re f Q")

    assert [operation.operator for operation in page.operations] == ["q", "re", "f", "Q"]
    doc.history.undo()
    assert page.operations == before


def test_page_operations_decode_within_the_limits() -> None:
    limits = DocumentLimits(max_page_content_size=16)
    doc = pyropdf.open(_page(b"BT /F1 12 Tf 20 250 Td (Keep) Tj ET", filtered=True), limits=limits)
    with pytest.raises(pyropdf.LimitError) as error:
        doc.pages[0].operations  # noqa: B018 - the property raises.
    assert error.value.code == "page_content_size"


def test_a_page_without_content_has_no_operations() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new()

    assert len(page.operations) == 0
    assert page.operations.to_bytes() == b""


def test_a_deleted_page_refuses_operations() -> None:
    doc = pyropdf.open(_page(b"q Q"))
    page = doc.pages[0]
    del doc.pages[0]
    with pytest.raises(pyropdf.StalePageError):
        page.operations  # noqa: B018 - the property raises.
