# ruff: noqa: SLF001 - deliberately exercise the private native regression hooks.
"""Unwinding Rust panics remain ordinary document errors, with usable state."""

from __future__ import annotations

import pickle
import subprocess
import sys

import pytest
from test_document import SIMPLE

import pyropdf
from pyropdf import Document, InternalError, PdfError, _core

SITES = ("attached", "detached", "edit", "nested_edit", "scratch", "locked")
MESSAGE = "pyropdf injected test panic"


def assert_usable(document: Document) -> None:
    """Read, edit, save and undo after both the history and writer locks unwind."""
    assert document.catalog["Type"] == "Catalog"
    assert len(document.pages) == 1
    assert document.pages[0].text() == ""
    document.pages[0].rotation = 90
    assert pyropdf.open(document.to_bytes()).pages[0].rotation == 90
    document.history.undo()
    assert document.pages[0].rotation == 0


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("catch", [PdfError, Exception])
def test_a_native_panic_is_an_ordinary_document_error(site: str, catch: type[Exception]) -> None:
    document = pyropdf.open(SIMPLE)
    before = document.to_bytes()
    with pytest.raises(catch, match=MESSAGE) as raised:
        _core._test_panic(document._core(), site)
    assert type(raised.value) is InternalError
    assert document.to_bytes() == before
    assert document.history.steps == ()
    assert_usable(document)


@pytest.mark.parametrize("payload", ["str", "string", "opaque", "drop"])
def test_panic_payloads_and_their_destructors_cannot_escape(payload: str) -> None:
    document = pyropdf.open(SIMPLE)
    message = MESSAGE if payload in {"str", "string"} else "Rust panic with a non-string payload"
    with pytest.raises(InternalError, match=message):
        _core._test_panic(document._core(), "detached", payload)
    assert_usable(document)


def test_pymethods_use_the_same_boundary() -> None:
    document = pyropdf.open(SIMPLE)
    with pytest.raises(InternalError, match="pyropdf injected method panic"):
        document._core()._test_panic()
    assert_usable(document)


@pytest.mark.parametrize("site", SITES)
def test_an_unwinding_transaction_discards_all_its_writes(site: str) -> None:
    document = pyropdf.open(SIMPLE)
    before = document.to_bytes()

    def fail() -> None:
        with document.transaction("failed"):
            document.pages[0].rotation = 180
            _core._test_panic(document._core(), site)

    with pytest.raises(InternalError, match=MESSAGE):
        fail()
    assert document.to_bytes() == before
    assert document.history.steps == ()
    assert_usable(document)


@pytest.mark.parametrize("site", ["edit", "nested_edit", "scratch", "locked"])
def test_a_caught_panic_cannot_commit_partial_writes(site: str) -> None:
    document = pyropdf.open(SIMPLE)
    with document.transaction("kept"):
        document.pages[0].rotation = 180
        with pytest.raises(InternalError, match=MESSAGE):
            _core._test_panic(document._core(), site)
        assert document.catalog["Type"] == "Catalog"
        assert document.pages[0].rotation == 180
        document.pages[0].rotation = 270
    assert document.pages[0].rotation == 270
    assert document.history.steps == ("kept",)
    document.history.undo()
    assert_usable(document)


def test_a_nested_failed_transaction_keeps_the_outer_writes() -> None:
    document = pyropdf.open(SIMPLE)

    def inner() -> None:
        with document.transaction("inner"):
            document.pages[0].rotation = 180
            _core._test_panic(document._core(), "edit")

    with document.transaction("outer"):
        document.pages[0].rotation = 90
        with pytest.raises(InternalError, match=MESSAGE):
            inner()
        assert document.pages[0].rotation == 90
    assert document.history.steps == ("outer",)
    document.history.undo()
    assert_usable(document)


def test_a_settler_panic_during_a_read_restores_the_open_transaction() -> None:
    document = pyropdf.open(SIMPLE)
    _core._test_arm_panic_settler(document._core())
    with document.transaction("kept"):
        document.pages[0].rotation = 180
        _core._test_mark_for_panic(document._core())
        with pytest.raises(InternalError, match="pyropdf injected settler panic"):
            len(document.pages)
        assert document.catalog["Type"] == "Catalog"
        assert document.pages[0].rotation == 180
    document.history.undo()
    assert_usable(document)


@pytest.mark.parametrize("explicit", [False, True])
def test_a_settler_panic_during_commit_discards_the_transaction(*, explicit: bool) -> None:
    document = pyropdf.open(SIMPLE)
    before = document.to_bytes()
    _core._test_arm_panic_settler(document._core())

    def fail() -> None:
        if explicit:
            with document.transaction("failed"):
                document.pages[0].rotation = 180
                _core._test_mark_for_panic(document._core())
        else:
            _core._test_mark_for_panic(document._core())

    with pytest.raises(InternalError, match="pyropdf injected settler panic"):
        fail()
    assert document.to_bytes() == before
    assert document.history.steps == ()
    assert_usable(document)


def test_the_default_panic_hook_still_reports_to_stderr() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import pyropdf; from pyropdf import _core\n"
                "try: _core._test_panic(pyropdf.Document()._core(), 'detached')\n"
                "except pyropdf.InternalError: print('recovered')\n"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert MESSAGE in result.stderr
    assert result.stdout.strip() == "recovered"


def test_internal_error_is_a_public_pickleable_pdf_error() -> None:
    assert "InternalError" in pyropdf.__all__
    assert InternalError.__module__ == "pyropdf"
    assert issubclass(InternalError, PdfError)
    assert issubclass(InternalError, Exception)
    restored = pickle.loads(pickle.dumps(InternalError(MESSAGE)))  # noqa: S301 - own error round trip.
    assert type(restored) is InternalError
    assert str(restored) == MESSAGE
