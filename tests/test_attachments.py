"""The Document.attachments mapping and its Attachment handles.

Ported from pyropdf ebb01d3c's tests/test_attachments.py. Intended
difference: pyropdf writes no object streams, so the compressed save of
``test_attachment_survives_compressed_and_garbage_save`` is a full save.
"""

from __future__ import annotations

import zlib

import pytest

import pyropdf


def build_raw_pdf(objects: dict[int, str | bytes]) -> bytes:
    """Return a PDF of ``objects`` by number, with a correct cross-reference table."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        body = objects[number]
        out += f"{number} 0 obj\n".encode() + (body.encode() if isinstance(body, str) else body) + b"\nendobj\n"
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 00000 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def build_pdf(texts: list[str]) -> bytes:
    """Return pyropdf's test document: one page per text."""
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(texts)))
    objects: dict[int, str | bytes] = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {len(texts)} /MediaBox [0 0 612 792] >>",
    }
    for i, text in enumerate(texts):
        content = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET"
        objects[3 + 2 * i] = f"<< /Type /Page /Parent 2 0 R /Contents {4 + 2 * i} 0 R >>"
        objects[4 + 2 * i] = f"<< /Length {len(content)} >>\nstream\n{content}\nendstream"
    return build_raw_pdf(objects)


def _compressed_attachment_pdf(
    payload: bytes,
    *,
    filter_name: bytes = b"/FlateDecode",
) -> bytes:
    """Build one attachment without asking pyropdf to decode it first."""
    encoded = zlib.compress(payload) if filter_name in {b"/FlateDecode", b"/Fl"} else payload
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles 4 0 R >> >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
            4: "<< /Names [(payload.bin) 5 0 R] >>",
            5: "<< /Type /Filespec /F (payload.bin) /EF << /F 6 0 R >> >>",
            6: (
                b"<< /Type /EmbeddedFile /Filter "
                + filter_name
                + b" /Length "
                + str(len(encoded)).encode()
                + b" >>\nstream\n"
                + encoded
                + b"\nendstream"
            ),
        }
    )


@pytest.mark.spec("embedded-files", "read", "create")
def test_attachment_add_get_roundtrip() -> None:
    doc = pyropdf.open(build_pdf(["Hello"]))
    payload = b"\x00\x01binary\xff\xfe" * 100
    doc.attachments.add("invoice.xml", payload, filename="請求書データ.xml", description="請求書の構造化データ")
    assert list(doc.attachments) == ["invoice.xml"]
    assert doc.attachments["invoice.xml"].read() == payload

    reopened = pyropdf.open(doc.to_bytes())
    assert list(reopened.attachments) == ["invoice.xml"]
    assert reopened.attachments["invoice.xml"].read() == payload


def test_attachment_add_bounds_input_atomically() -> None:
    doc = pyropdf.open(build_pdf(["Hello"]))
    payload = b"1234"

    doc.attachments.add("exact.bin", payload, max_size=len(payload))
    before = doc.to_bytes()
    with pytest.raises(pyropdf.LimitError) as caught:
        doc.attachments.add("too-large.bin", payload, max_size=len(payload) - 1)
    assert caught.value.code == "embedded_file_size"
    assert doc.to_bytes() == before
    assert list(doc.attachments) == ["exact.bin"]

    doc.attachments.add("unbounded.bin", payload, max_size=None)
    assert doc.attachments["unbounded.bin"].read() == payload


@pytest.mark.parametrize("max_size", [True, 1.5, "1024"])
def test_attachment_add_rejects_non_integer_size_limits(max_size: object) -> None:
    doc = pyropdf.Document()
    with pytest.raises(TypeError, match="max_size"):
        doc.attachments.add("x", b"x", max_size=max_size)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("max_size", [0, -1])
def test_attachment_add_rejects_non_positive_size_limits(max_size: int) -> None:
    doc = pyropdf.Document()
    with pytest.raises(ValueError, match="max_size"):
        doc.attachments.add("x", b"x", max_size=max_size)


@pytest.mark.spec("embedded-files", "preserve")
def test_attachment_survives_compressed_and_garbage_save() -> None:
    doc = pyropdf.open(build_pdf(["Hello"]))
    payload = b"repetitive " * 1000
    doc.attachments.add("data.txt", payload)
    data = doc.to_bytes()
    reopened = pyropdf.open(data)
    assert reopened.attachments["data.txt"].read() == payload


@pytest.mark.spec("name-trees", "read", "create")
def test_attachment_multiple_names_sorted() -> None:
    doc = pyropdf.Document()
    doc.pages.new()
    doc.attachments.add("b.txt", b"B")
    doc.attachments.add("a.txt", b"A")
    assert list(doc.attachments) == ["a.txt", "b.txt"]
    assert doc.attachments["a.txt"].read() == b"A"
    assert doc.attachments["b.txt"].read() == b"B"


@pytest.mark.spec("file-specifications", "read")
def test_inline_filespec_reads_do_not_mutate_document() -> None:
    """Read a valid inline FileSpec without creating orphan objects."""
    pdf = build_raw_pdf(
        {
            1: (
                "<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << "
                "/Names [(x.txt) << /Type /Filespec /F (x.txt) /EF << /F 4 0 R >> >>] >> >> >>"
            ),
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
            4: b"<< /Type /EmbeddedFile /Length 3 >>\nstream\nabc\nendstream",
        }
    )
    doc = pyropdf.open(pdf)
    before = len(doc.to_bytes())

    for _ in range(4):
        assert list(doc.attachments) == ["x.txt"]
        assert doc.attachments["x.txt"].read() == b"abc"

    assert len(doc.to_bytes()) == before
    doc.attachments.add("y.txt", b"def")
    assert list(doc.attachments) == ["x.txt", "y.txt"]
    assert doc.attachments["x.txt"].read() == b"abc"
    del doc.attachments["x.txt"]
    assert list(doc.attachments) == ["y.txt"]


@pytest.mark.spec("name-trees", "edit")
def test_attachment_del_removes() -> None:
    doc = pyropdf.Document()
    doc.pages.new()
    doc.attachments.add("a.txt", b"A")
    doc.attachments.add("b.txt", b"B")
    del doc.attachments["a.txt"]
    assert list(doc.attachments) == ["b.txt"]
    reopened = pyropdf.open(doc.to_bytes())
    assert list(reopened.attachments) == ["b.txt"]


def test_attachment_errors() -> None:
    doc = pyropdf.Document()
    doc.pages.new()
    doc.attachments.add("a.txt", b"A")
    with pytest.raises(pyropdf.PdfError, match="already exists"):
        doc.attachments.add("a.txt", b"other")
    with pytest.raises(KeyError):
        _ = doc.attachments["missing.txt"]
    with pytest.raises(KeyError):
        del doc.attachments["missing.txt"]
    assert "missing.txt" not in doc.attachments
    assert 1 not in doc.attachments
    with pytest.raises(ValueError, match="name"):
        doc.attachments.add("", b"x")


def test_attachment_get_bounds_compressed_output_before_materializing_it() -> None:
    payload = b"attachment payload " * 100_000
    doc = pyropdf.open(_compressed_attachment_pdf(payload))

    with pytest.raises(pyropdf.LimitError) as caught:
        doc.attachments["payload.bin"].read(max_size=1024)
    assert caught.value.code == "embedded_file_size"
    assert "1024-byte" in str(caught.value)

    assert doc.attachments["payload.bin"].read(max_size=len(payload)) == payload
    assert doc.attachments["payload.bin"].read(max_size=None) == payload


def test_attachment_get_applies_the_same_limit_to_uncompressed_content() -> None:
    doc = pyropdf.Document()
    doc.pages.new()
    doc.attachments.add("raw.bin", b"1234")
    with pytest.raises(pyropdf.LimitError) as caught:
        doc.attachments["raw.bin"].read(max_size=3)
    assert caught.value.code == "embedded_file_size"
    assert doc.attachments["raw.bin"].read(max_size=4) == b"1234"


def test_attachment_get_accepts_filter_abbreviations_and_rejects_decode_failures() -> None:
    payload = b"abbreviated Flate attachment"
    abbreviated = pyropdf.open(_compressed_attachment_pdf(payload, filter_name=b"/Fl"))
    assert abbreviated.attachments["payload.bin"].read() == payload

    unsupported = pyropdf.open(_compressed_attachment_pdf(b"encoded", filter_name=b"/DCTDecode"))
    with pytest.raises(pyropdf.PdfError, match="failed to decode attachment"):
        unsupported.attachments["payload.bin"].read()

    malformed = pyropdf.open(_compressed_attachment_pdf(b"encoded", filter_name=b"123"))
    with pytest.raises(pyropdf.PdfError, match="invalid Filter"):
        malformed.attachments["payload.bin"].read()

    filters = b"[" + b" ".join([b"/FlateDecode"] * 17) + b"]"
    excessive = pyropdf.open(_compressed_attachment_pdf(b"", filter_name=filters))
    with pytest.raises(pyropdf.LimitError) as filter_count:
        excessive.attachments["payload.bin"].read()
    assert filter_count.value.code == "stream_filter_count"


@pytest.mark.parametrize("max_size", [True, 1.5, "1024"])
def test_attachment_get_rejects_non_integer_size_limits(max_size: object) -> None:
    doc = pyropdf.Document()
    doc.pages.new()
    doc.attachments.add("x", b"x")
    with pytest.raises(TypeError, match="max_size"):
        doc.attachments["x"].read(max_size=max_size)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("max_size", [0, -1])
def test_attachment_get_rejects_non_positive_size_limits(max_size: int) -> None:
    doc = pyropdf.Document()
    doc.pages.new()
    doc.attachments.add("x", b"x")
    with pytest.raises(ValueError, match="max_size"):
        doc.attachments["x"].read(max_size=max_size)


@pytest.mark.spec("name-dictionary", "read")
def test_attachment_name_tree_reference_cycle_is_visited_once() -> None:
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles 4 0 R >> >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
            4: "<< /Names [(x.txt) 5 0 R] /Kids [4 0 R] >>",
            5: "<< /Type /Filespec /F (x.txt) /EF << /F 6 0 R >> >>",
            6: b"<< /Type /EmbeddedFile /Length 1 >>\nstream\nx\nendstream",
        }
    )
    doc = pyropdf.open(pdf)
    assert list(doc.attachments) == ["x.txt"]
    assert doc.attachments["x.txt"].read() == b"x"


def test_attachment_name_tree_rejects_excessive_depth() -> None:
    objects: dict[int, str | bytes] = {
        1: "<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles 4 0 R >> >>",
        2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
    }
    for object_id in range(4, 36):
        objects[object_id] = f"<< /Kids [{object_id + 1} 0 R] >>"
    objects[36] = "<< /Names [(x.txt) 37 0 R] >>"
    objects[37] = "<< /Type /Filespec /F (x.txt) /EF << /F 38 0 R >> >>"
    objects[38] = b"<< /Type /EmbeddedFile /Length 1 >>\nstream\nx\nendstream"

    doc = pyropdf.open(build_raw_pdf(objects))
    with pytest.raises(pyropdf.PdfError, match="32-level safety limit"):
        list(doc.attachments)


def test_attachment_name_tree_rejects_excessive_entries_without_partial_output() -> None:
    pairs = " ".join(f"(name-{index}) 5 0 R" for index in range(4097))
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles 4 0 R >> >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
            4: f"<< /Names [{pairs}] >>",
            5: "<< /Type /Filespec /F (x.txt) /EF << /F 6 0 R >> >>",
            6: b"<< /Type /EmbeddedFile /Length 1 >>\nstream\nx\nendstream",
        }
    )
    doc = pyropdf.open(pdf)
    with pytest.raises(pyropdf.PdfError, match="4096-entry safety limit"):
        list(doc.attachments)
    with pytest.raises(pyropdf.PdfError, match="4096-entry safety limit"):
        _ = doc.attachments["name-0"]


def test_attachment_add_refuses_to_create_an_unreadable_oversized_tree() -> None:
    pairs = " ".join(f"(name-{index}) 5 0 R" for index in range(4096))
    pdf = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles 4 0 R >> >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
            4: f"<< /Names [{pairs}] >>",
            5: "<< /Type /Filespec /F (x.txt) /EF << /F 6 0 R >> >>",
            6: b"<< /Type /EmbeddedFile /Length 1 >>\nstream\nx\nendstream",
        }
    )
    doc = pyropdf.open(pdf)
    before = doc.to_bytes()
    with pytest.raises(pyropdf.PdfError, match="4096-entry safety limit is reached"):
        doc.attachments.add("one-too-many", b"x")
    assert doc.to_bytes() == before


def test_attachment_writes_bound_inline_filespec_clone_shape() -> None:
    custom_values = " ".join("null" for _ in range(4096))
    pdf = build_raw_pdf(
        {
            1: (
                "<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << "
                "/Names [(x.txt) << /Type /Filespec /F (x.txt) "
                f"/EF << /F 4 0 R >> /Custom [{custom_values}] >>] >> >> >>"
            ),
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
            4: b"<< /Type /EmbeddedFile /Length 1 >>\nstream\nx\nendstream",
        }
    )
    doc = pyropdf.open(pdf)
    before = doc.to_bytes()

    assert list(doc.attachments) == ["x.txt"]
    assert doc.attachments["x.txt"].read() == b"x"
    with pytest.raises(pyropdf.PdfError, match="4096-object safety limit"):
        doc.attachments.add("y.txt", b"y")
    with pytest.raises(pyropdf.PdfError, match="4096-object safety limit"):
        del doc.attachments["x.txt"]
    assert doc.to_bytes() == before


@pytest.mark.parametrize(
    ("filename", "desc"),
    [
        ("x" * (1024 * 1024), None),
        (None, "x" * (1024 * 1024)),
    ],
    ids=["filename", "description"],
)
def test_attachment_add_bounds_input_metadata_atomically(
    filename: str | None,
    desc: str | None,
) -> None:
    doc = pyropdf.open(build_pdf(["Hello"]))
    before = doc.to_bytes()

    with pytest.raises(pyropdf.LimitError) as caught:
        doc.attachments.add("x", b"x", filename=filename, description=desc)
    assert caught.value.code == "embedded_file_input_size"
    assert doc.to_bytes() == before


@pytest.mark.parametrize("method", ["get", "del"])
def test_attachment_lookup_bounds_name_input_before_traversal(method: str) -> None:
    doc = pyropdf.open(build_pdf(["Hello"]))
    doc.attachments.add("x", b"x")
    before = doc.to_bytes()
    name = "é" * (512 * 1024 + 1)

    def call() -> None:
        if method == "get":
            _ = doc.attachments[name]
        else:
            del doc.attachments[name]

    with pytest.raises(pyropdf.LimitError) as caught:
        call()

    assert caught.value.code == "embedded_file_input_size"
    assert doc.to_bytes() == before


def _described_pdf() -> bytes:
    """Build one attachment with a description, dates, and a declared size."""
    return build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles 4 0 R >> >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
            4: "<< /Names [(data) 5 0 R] >>",
            5: "<< /Type /Filespec /F (legacy.csv) /UF <FEFF0064006100740061002E006300730076> /Desc (Rows) "
            "/EF << /F 6 0 R /UF 6 0 R >> >>",
            6: (
                b"<< /Type /EmbeddedFile /Subtype /text#2Fcsv /Params << /Size 3 /CheckSum <00> "
                b"/CreationDate (D:20260101000000Z) /ModDate (D:20260102000000Z) >> /Length 3 >>\n"
                b"stream\na,b\nendstream"
            ),
        }
    )


def test_attachment_handles_report_pymupdf_metadata() -> None:
    doc = pyropdf.open(_described_pdf())

    attachment = doc.attachments["data"]

    assert isinstance(attachment, pyropdf.Attachment)
    assert isinstance(doc.attachments, pyropdf.Attachments)
    assert (attachment.name, attachment.filename, attachment.description) == ("data", "data.csv", "Rows")
    assert attachment.size == 3
    assert (attachment.created, attachment.modified) == ("D:20260101000000Z", "D:20260102000000Z")
    assert attachment.read() == b"a,b"
    assert len(doc.attachments) == 1
    assert "data" in doc.attachments
    assert dict(doc.attachments.items()) == {"data": attachment}
    assert repr(attachment) == "Attachment('data')"
    assert repr(doc.attachments) == "Attachments(['data'])"


@pytest.mark.spec("file-specifications", "create")
def test_added_attachments_report_their_metadata() -> None:
    doc = pyropdf.Document()
    doc.pages.new()

    added = doc.attachments.add("a.txt", b"hello")
    plain = doc.attachments.add("b.txt", b"", filename="B.TXT")

    assert added == doc.attachments["a.txt"]
    assert (added.filename, added.description, added.size) == ("a.txt", None, 5)
    assert (added.created, added.modified) == (None, None)
    assert (plain.filename, plain.size) == ("B.TXT", 0)
    with pytest.raises(TypeError):
        doc.attachments.add(name="c.txt", data=b"")  # ty: ignore[positional-only-parameter-as-kwarg]


@pytest.mark.spec("embedded-files", "edit")
def test_attachment_write_replaces_the_data_and_keeps_the_metadata() -> None:
    doc = pyropdf.open(_described_pdf())
    attachment = doc.attachments["data"]

    attachment.write(b"x,y,z\n1,2,3")

    for current in (doc, pyropdf.open(doc.to_bytes())):
        rewritten = current.attachments["data"]
        assert rewritten.read() == b"x,y,z\n1,2,3"
        assert (rewritten.filename, rewritten.description, rewritten.size) == ("data.csv", "Rows", 11)
        assert rewritten.created == "D:20260101000000Z"
        assert rewritten.modified is not None
        assert rewritten.modified.startswith("D:20")
        assert rewritten.modified != "D:20260102000000Z"
        assert list(current.attachments) == ["data"]
    assert b"CheckSum" not in doc.to_bytes()


@pytest.mark.spec("file-specifications", "edit")
def test_attachment_write_updates_an_inline_file_specification() -> None:
    pdf = build_raw_pdf(
        {
            1: (
                "<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << "
                "/Names [(x.txt) << /Type /Filespec /F (x.txt) /EF << /F 4 0 R >> >>] >> >> >>"
            ),
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
            4: b"<< /Type /EmbeddedFile /Length 3 >>\nstream\nabc\nendstream",
        }
    )
    doc = pyropdf.open(pdf)

    doc.attachments["x.txt"].write(b"new")

    assert doc.attachments["x.txt"].read() == b"new"
    assert pyropdf.open(doc.to_bytes()).attachments["x.txt"].read() == b"new"


def test_attachment_write_bounds_its_input_atomically() -> None:
    doc = pyropdf.Document()
    doc.pages.new()
    attachment = doc.attachments.add("a.bin", b"1234")
    before = doc.to_bytes()

    with pytest.raises(pyropdf.LimitError) as caught:
        attachment.write(b"12345", max_size=4)
    assert caught.value.code == "embedded_file_size"
    assert doc.to_bytes() == before
    attachment.write(b"12345", max_size=None)
    assert attachment.read() == b"12345"


def test_deleted_attachment_handles_go_stale() -> None:
    doc = pyropdf.Document()
    doc.pages.new()
    attachment = doc.attachments.add("a.txt", b"A")

    del doc.attachments["a.txt"]

    for read in (lambda: attachment.filename, attachment.read, lambda: attachment.write(b"B")):
        with pytest.raises(pyropdf.StaleObjectError):
            read()
    assert attachment.name == "a.txt"
    assert len(doc.attachments) == 0


def test_attachments_require_an_open_document() -> None:
    doc = pyropdf.Document()
    doc.pages.new()
    attachments = doc.attachments
    attachment = attachments.add("a.txt", b"A")
    doc.close()

    with pytest.raises(pyropdf.DocumentClosedError):
        list(attachments)
    with pytest.raises(pyropdf.DocumentClosedError):
        attachment.read()
    with pytest.raises(pyropdf.DocumentClosedError):
        _ = doc.attachments


def test_attachments_replace_the_embfile_methods() -> None:
    doc = pyropdf.Document()
    for name in ("embfile_add", "embfile_get", "embfile_del", "embfile_names", "embfile_info", "embfile_upd"):
        assert not hasattr(doc, name)
