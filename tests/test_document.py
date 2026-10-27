"""Opening, probing, closing, and saving documents, and the errors that they raise."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from corpus import ASSETS, NAMES

import pyropdf
from pyropdf import (
    Document,
    DocumentClosedError,
    DocumentLimits,
    EncryptedDocumentError,
    LimitError,
    MalformedPdfError,
    PasswordError,
    PdfError,
    StaleObjectError,
)


def build_pdf(objects: list[bytes], trailer: bytes = b"/Root 1 0 R") -> bytes:
    """Return a PDF whose objects 1 to n are ``objects``, with a correct cross-reference table."""
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<</Size %d %s>>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, trailer, xref)
    return bytes(out)


CATALOG = b"<</Type/Catalog/Pages 2 0 R>>"
PAGES = b"<</Type/Pages/Kids[3 0 R]/Count 1>>"
PAGE = b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>"
SIMPLE = build_pdf([CATALOG, PAGES, PAGE])


def temporary_outputs(directory: Path) -> list[Path]:
    return sorted(directory.glob(".pyropdf-*.tmp"))


# Sources


@pytest.mark.parametrize(
    "make",
    [
        bytes,
        bytearray,
        memoryview,
        lambda data: memoryview(bytearray(data)),
        lambda data: memoryview(bytearray(data)).toreadonly(),
    ],
    ids=["bytes", "bytearray", "memoryview", "memoryview-bytearray", "readonly-view-bytearray"],
)
def test_buffers_open(make: object) -> None:
    source = make(SIMPLE)  # ty: ignore[call-non-callable]
    with pyropdf.open(source) as doc:
        assert doc.to_bytes(incremental=True) == SIMPLE


def test_a_bytearray_changed_after_opening_does_not_change_the_document() -> None:
    data = bytearray(SIMPLE)
    doc = pyropdf.open(data)
    data[:] = b"\0" * len(data)
    assert doc.to_bytes(incremental=True) == SIMPLE


@pytest.mark.parametrize("as_path", [str, Path], ids=["str", "pathlike"])
def test_paths_open(tmp_path: Path, as_path: type) -> None:
    path = tmp_path / "simple.pdf"
    path.write_bytes(SIMPLE)
    with pyropdf.open(as_path(path)) as doc:
        assert doc.to_bytes(incremental=True) == SIMPLE


def test_a_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        pyropdf.open(tmp_path / "missing.pdf")


def test_other_sources_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "simple.pdf"
    path.write_bytes(SIMPLE)
    with path.open("rb") as file, pytest.raises(TypeError, match="read the file object first"):
        pyropdf.open(file)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="source must be a path"):
        pyropdf.open(42)  # ty: ignore[invalid-argument-type]


# Errors


def test_the_hierarchy_is_pylopdfs() -> None:
    for error in (
        MalformedPdfError,
        PasswordError,
        EncryptedDocumentError,
        LimitError,
        StaleObjectError,
        DocumentClosedError,
    ):
        assert issubclass(error, PdfError)
        assert error.__module__ == "pyropdf"
    assert issubclass(PdfError, Exception)


@pytest.mark.parametrize("data", [b"", b"not a PDF at all"], ids=["empty", "garbage"])
def test_input_that_is_not_pdf_is_malformed(data: bytes) -> None:
    with pytest.raises(MalformedPdfError):
        pyropdf.open(data)


def test_limit_error_carries_its_code() -> None:
    with pytest.raises(LimitError) as caught:
        pyropdf.open(SIMPLE, limits=DocumentLimits(max_file_size=len(SIMPLE) - 1))
    assert caught.value.code == "file_size"
    assert "file_size" not in str(caught.value)
    assert str(len(SIMPLE)) in str(caught.value)


def test_file_size_is_checked_before_reading_a_file(tmp_path: Path) -> None:
    path = tmp_path / "simple.pdf"
    path.write_bytes(SIMPLE)
    with pytest.raises(LimitError) as caught:
        pyropdf.open(path, limits=DocumentLimits(max_file_size=10))
    assert caught.value.code == "file_size"


def test_object_count_is_checked_at_open() -> None:
    with pytest.raises(LimitError) as caught:
        pyropdf.open(SIMPLE, limits=DocumentLimits(max_objects=2))
    assert caught.value.code == "object_count"


def test_saving_an_encrypted_document_needs_the_security_handler() -> None:
    encrypt = b"<</Filter/Standard/V 2/R 3/Length 128/O<00>/U<00>/P -4>>"
    data = build_pdf([CATALOG, PAGES, PAGE, encrypt], b"/Root 1 0 R/Encrypt 4 0 R/ID[<00><00>]")
    doc = pyropdf.open(data)
    with pytest.raises(EncryptedDocumentError):
        doc.to_bytes()
    assert pyropdf.probe(data).needs_password


# Limits at open

DEEP = build_pdf([b"<</Type/Catalog/Pages 2 0 R/Deep " + b"[" * 10 + b"]" * 10 + b">>", PAGES, PAGE])


def test_opening_reads_only_the_index() -> None:
    with pyropdf.open(DEEP, limits=DocumentLimits(max_object_depth=8)) as doc:
        assert not doc.is_repaired


def test_check_at_open_reads_every_object() -> None:
    with pytest.raises(LimitError) as caught:
        pyropdf.open(DEEP, limits=DocumentLimits(max_object_depth=8, check_at_open=True))
    assert caught.value.code == "object_depth"


def test_the_web_profile_checks_at_open() -> None:
    web = DocumentLimits.web()
    assert web.check_at_open
    assert web.max_file_size == 10 * 1024 * 1024
    assert web.max_object_depth == 64
    damaged = build_pdf([CATALOG, PAGES, b")"])
    assert not pyropdf.open(damaged).is_repaired
    with pytest.raises(MalformedPdfError):
        pyropdf.open(damaged, limits=web)
    with pyropdf.open(SIMPLE, limits=web) as doc:
        assert doc.limits is web


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"max_pages": 0}, ValueError),
        ({"max_pages": -1}, ValueError),
        ({"max_pages": True}, TypeError),
        ({"max_pages": 1.5}, TypeError),
        ({"max_duration": 0.0}, ValueError),
        ({"max_duration": float("nan")}, ValueError),
        ({"max_duration": "1"}, TypeError),
        ({"check_at_open": 1}, TypeError),
    ],
)
def test_invalid_limits_are_refused(fields: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        DocumentLimits(**fields)  # ty: ignore[invalid-argument-type]


def test_limits_must_be_document_limits() -> None:
    with pytest.raises(TypeError, match="DocumentLimits"):
        pyropdf.open(SIMPLE, limits={"max_pages": 1})  # ty: ignore[invalid-argument-type]


# Closing


def test_close_and_the_context_manager() -> None:
    with pyropdf.open(SIMPLE) as doc:
        assert not doc.is_closed
        assert repr(doc) == "<pyropdf.Document>"
    assert doc.is_closed
    assert repr(doc) == "<closed pyropdf.Document>"
    for operation in (doc.to_bytes, lambda: doc.is_repaired, lambda: doc.save("unused.pdf")):
        with pytest.raises(DocumentClosedError):
            operation()
    doc.close()


# Empty documents and probing


def test_an_empty_document_saves_in_full_only() -> None:
    doc = Document()
    data = doc.to_bytes()
    assert data.startswith(b"%PDF-1.7")
    probed = pyropdf.probe(data)
    assert probed.page_count == 0
    with pytest.raises(PdfError, match="original PDF file"):
        doc.to_bytes(incremental=True)


@pytest.mark.spec("file-header", "read")
def test_probe_reads_the_page_count_version_and_metadata() -> None:
    title = b"(Caf\xe9 \x93\x94 \x80)"
    author = b"<FEFF65E5672C>"
    data = build_pdf(
        [CATALOG, PAGES, PAGE, b"<</Title " + title + b"/Author " + author + b"/Bogus(x)>>"], b"/Root 1 0 R/Info 4 0 R"
    )
    probed = pyropdf.probe(memoryview(data))
    assert probed.page_count == 1
    assert probed.version == "1.7"
    assert not probed.needs_password
    assert not probed.is_repaired
    assert dict(probed.metadata) == {"title": "Café ﬁﬂ •", "author": "日本"}


def test_probe_bounds_the_file_size(tmp_path: Path) -> None:
    path = tmp_path / "simple.pdf"
    path.write_bytes(SIMPLE)
    assert pyropdf.probe(path).page_count == 1
    with pytest.raises(LimitError) as caught:
        pyropdf.probe(path, max_file_size=10)
    assert caught.value.code == "file_size"


def test_probe_refuses_a_document_without_a_page_tree() -> None:
    with pytest.raises(MalformedPdfError):
        pyropdf.probe(build_pdf([b"<</Type/Catalog>>"]))


# Saving


def test_to_bytes_bounds_the_output() -> None:
    with pyropdf.open(SIMPLE) as doc:
        with pytest.raises(LimitError) as caught:
            doc.to_bytes(max_size=100)
        assert caught.value.code == "pdf_output_size"
        assert doc.to_bytes(max_size=None)
        with pytest.raises(ValueError, match="max_size"):
            doc.to_bytes(max_size=0)
        with pytest.raises(TypeError, match="incremental"):
            doc.to_bytes(incremental=1)  # ty: ignore[invalid-argument-type]


def test_save_writes_what_to_bytes_returns(tmp_path: Path) -> None:
    path = tmp_path / "out.pdf"
    with pyropdf.open(SIMPLE) as doc:
        doc.save(path)
        assert path.read_bytes() == doc.to_bytes()
        doc.save(path, incremental=True)
        assert path.read_bytes() == SIMPLE
    assert temporary_outputs(tmp_path) == []


def test_saving_over_the_opened_file_is_safe(tmp_path: Path) -> None:
    path = tmp_path / "simple.pdf"
    path.write_bytes(SIMPLE)
    with pyropdf.open(path) as doc:
        doc.save(path)
        doc.save(path, incremental=True)
    assert path.read_bytes() == SIMPLE


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_save_keeps_an_existing_files_permissions(tmp_path: Path) -> None:
    path = tmp_path / "out.pdf"
    path.write_bytes(b"old")
    path.chmod(0o640)
    pyropdf.open(SIMPLE).save(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert path.read_bytes().startswith(b"%PDF-")


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_save_keeps_a_final_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.pdf"
    real.write_bytes(b"old")
    link = tmp_path / "link.pdf"
    link.symlink_to(real)
    pyropdf.open(SIMPLE).save(link, incremental=True)
    assert link.is_symlink()
    assert real.read_bytes() == SIMPLE


def test_a_failed_save_leaves_the_target_and_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "out.pdf"
    path.write_bytes(b"old")
    with pytest.raises(PdfError, match="original PDF file"):
        Document().save(path, incremental=True)
    assert path.read_bytes() == b"old"
    assert temporary_outputs(tmp_path) == []


def test_a_save_into_a_missing_directory_fails_with_the_callers_path(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "out.pdf"
    with pytest.raises(PdfError, match="failed to save") as caught:
        pyropdf.open(SIMPLE).save(path)
    assert str(path) in str(caught.value)


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX directory permissions")
def test_a_save_into_a_read_only_directory_fails(tmp_path: Path) -> None:
    directory = tmp_path / "locked"
    directory.mkdir()
    directory.chmod(0o500)
    try:
        with pytest.raises(PdfError, match="failed to save"):
            pyropdf.open(SIMPLE).save(directory / "out.pdf")
    finally:
        directory.chmod(0o700)
    assert list(directory.iterdir()) == []


# The corpus


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.spec("xref-table", "read", "preserve")
def test_corpus_files_round_trip_without_edits(name: str) -> None:
    data = (ASSETS / name).read_bytes()
    with pyropdf.open(data) as doc:
        if not doc.is_repaired:
            assert doc.to_bytes(incremental=True) == data
        full = doc.to_bytes()
        original = {number: (generation, value) for number, generation, value in doc._doc._objects()}  # noqa: SLF001
    with pyropdf.open(full) as saved:
        assert not saved.is_repaired
        rewritten = {number: (generation, value) for number, generation, value in saved._doc._objects()}  # noqa: SLF001
    assert original.items() <= rewritten.items()


# Documentation


def test_the_public_names_have_docstrings() -> None:
    for name in pyropdf.__all__:
        if name.startswith("__"):
            continue
        public = getattr(pyropdf, name)
        assert public.__doc__, name
        for member, value in vars(public).items() if isinstance(public, type) else ():
            if not member.startswith("_") and callable(value):
                assert value.__doc__, f"{name}.{member}"
