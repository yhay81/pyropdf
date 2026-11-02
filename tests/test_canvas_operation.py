"""A canvas runs as one operation from its document's limits (#500).

Its input checks, buffers, text layout and encoding, generated content,
and writes charge one budget, and observe one deadline and one
cancellation token. These tests check accumulation within a canvas, no
leak between canvases, typed refusals that write nothing and record no
undo step, and the content-size check before buffered bytes are joined.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import pyropdf
from pyropdf import _canvas, _core

# ruff: noqa: SLF001, PT012 - the canvas's operation and core are under test; a refusal ends a with block.

FONTS = Path(__file__).resolve().parent.parent / "crates" / "pyropdf-engine" / "src" / "fonts" / "testdata"
JAPANESE = FONTS / "NotoSansJP-subset.otf"
RED = pyropdf.Color(1, 0, 0)


def _document(**limits: object) -> tuple[pyropdf.Document, pyropdf.Page]:
    document = pyropdf.Document(limits=pyropdf.DocumentLimits(**limits))  # ty: ignore[invalid-argument-type]
    return document, document.pages.new((200, 200))


def _state(document: pyropdf.Document) -> object:
    return _core.history_state(document._core())


# One operation.


def test_a_canvas_charges_one_operation_from_the_document_limits() -> None:
    document, page = _document(max_memory_size=50_000_000)
    with page.draw() as canvas:
        operation = canvas._active
        assert operation.limit("memory_size") == 50_000_000
        before = operation.used("memory_size")
        canvas.rect((0, 0, 10, 10), fill=RED)
        after_path = operation.used("memory_size")
        canvas.text("Hello", (10, 50))
        after_text = operation.used("memory_size")
        # Reads and writes inside the canvas charge the same operation.
        assert document.pages[0].obj["MediaBox"] is not None
        assert before < after_path < after_text
    assert "Hello" in page.text()


def test_charges_accumulate_within_a_canvas_and_not_across_canvases() -> None:
    _, page = _document()
    used = []
    for _ in range(2):
        with page.draw() as canvas:
            start = canvas._active.used("memory_size")
            for _ in range(10):
                canvas.text("Line of text", (10, 50))
            used.append((start, canvas._active.used("memory_size")))
    (first_start, first_end), (second_start, second_end) = used
    assert first_end > first_start
    # The second canvas starts afresh: the first one's charges did not leak.
    assert second_start == first_start
    assert second_end == first_end


def test_draws_that_fit_alone_are_refused_together() -> None:
    def draw(times: int) -> None:
        _, page = _document(max_memory_size=limit)
        with page.draw() as canvas:
            for _ in range(times):
                canvas.text("x" * 2_000, (0, 50))

    # Find a limit that one drawing fits and many do not.
    limit = 200_000
    draw(1)
    with pytest.raises(pyropdf.LimitError) as error:
        draw(200)
    assert error.value.code == "memory_size"


def test_a_canvas_operation_is_refused_after_its_canvas() -> None:
    _, page = _document()
    with page.draw() as canvas:
        canvas.rect((0, 0, 10, 10), fill=RED)
        operation = canvas._active
    with pytest.raises(ValueError, match="not active"):
        operation.charge("memory_size", 1)


def test_a_call_inside_a_canvas_tightens_the_operation() -> None:
    document, page = _document()
    with page.draw() as canvas:
        canvas.rect((0, 0, 10, 10), fill=RED)
        canvas.rect((20, 20, 30, 30), fill=RED)
    other = document.pages.new((200, 200))
    with other.draw() as canvas:
        content = page.content()
        assert len(content.paths) == 2
        with pytest.raises(pyropdf.LimitError) as error:
            page.content(max_elements=1)
        assert error.value.code == "content_element_count"
        # The refused call leaves the canvas usable.
        canvas.rect((0, 0, 5, 5), fill=RED)
    assert len(other.content().paths) == 1


# Refusals write nothing.


@pytest.mark.parametrize(
    "draw",
    [
        lambda canvas: canvas.rect((0, 0, 10, 10), fill=RED),
        lambda canvas: canvas.text("Hello\nworld", (10, 50)),
        lambda canvas: canvas.textbox((0, 0, 200, 200), "Hello world " * 20),
    ],
    ids=["path", "text", "textbox"],
)
def test_a_low_memory_limit_refuses_without_a_write_or_undo_step(draw: object) -> None:
    limit = 10_000_000
    document, page = _document(max_memory_size=limit)
    before = _state(document)
    count = len(document.objects)
    with pytest.raises(pyropdf.LimitError) as error, page.draw() as canvas:
        canvas.rect((0, 0, 10, 10), fill=RED)
        # Leave the operation 16 bytes, as earlier work of the canvas would.
        operation = canvas._active
        operation.charge("memory_size", limit - operation.used("memory_size") - 16)
        draw(canvas)  # ty: ignore[call-non-callable]
    assert error.value.code == "memory_size"
    assert _state(document) == before
    assert len(document.objects) == count
    assert page.obj.get("Contents") is None


def test_the_element_limit_refuses_without_a_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_canvas, "_MAX_POINTS", 8)
    document, page = _document()
    before = _state(document)
    with pytest.raises(pyropdf.LimitError) as error, page.draw() as canvas:
        canvas.polygon([(0, 0), (10, 0), (10, 10), (0, 10)], fill=RED)
        canvas.polygon([(0, 0), (10, 0), (10, 10), (0, 10), (5, 5)], fill=RED)
    assert error.value.code == "content_element_count"
    assert _state(document) == before


# Deadline and cancellation.


def test_cancellation_stops_preparation() -> None:
    token = pyropdf.CancellationToken()
    document, page = _document(cancellation=token)
    before = _state(document)
    with pytest.raises(pyropdf.LimitError) as error, page.draw() as canvas:
        canvas.text("Before", (10, 50))
        token.cancel()
        canvas.textbox((0, 0, 200, 200), "After " * 100)
    assert error.value.code == "cancelled"
    assert _state(document) == before


def test_cancellation_stops_writing() -> None:
    token = pyropdf.CancellationToken()
    document, page = _document(cancellation=token)
    before = _state(document)
    with pytest.raises(pyropdf.LimitError) as error, page.draw() as canvas:
        canvas.text("Drawn", (10, 50))
        # Cancelled after the last drawing call: the commit refuses.
        token.cancel()
    assert error.value.code == "cancelled"
    # Reads of the document refuse too, once its token is cancelled.
    assert _state(document) == before


def test_the_deadline_stops_preparation_and_writing() -> None:
    document, page = _document(max_duration=0.2)
    before = _state(document)
    with pytest.raises(pyropdf.LimitError) as error, page.draw() as canvas:
        canvas.text("Drawn", (10, 50))
        time.sleep(0.3)
        canvas.text("Late", (10, 80))
    assert error.value.code == "deadline"
    with pytest.raises(pyropdf.LimitError) as error, page.draw() as canvas:
        canvas.text("Drawn", (10, 50))
        time.sleep(0.3)
    assert error.value.code == "deadline"
    assert _state(document) == before


def test_font_calls_observe_the_canvas_operation() -> None:
    token = pyropdf.CancellationToken()
    _, page = _document(cancellation=token)
    face = pyropdf.Font.from_file(JAPANESE)._font
    helvetica = pyropdf.Font.standard("Helvetica")._font
    canvas = page.draw()
    with pytest.raises(pyropdf.LimitError) as error, canvas:
        operation = canvas._active
        before = operation.used("memory_size")
        helvetica.encode("Hello", operation=operation)
        helvetica.layout("Hello world", 100, 100, 12, None, False, None, operation=operation)  # noqa: FBT003
        face.shape("日本語", operation=operation)
        # A call returns the memory it charged when it ends.
        assert operation.used("memory_size") == before
        token.cancel()
        face.shape("日本語", operation=operation)
    assert error.value.code == "cancelled"


def test_text_layout_observes_the_operation_memory() -> None:
    _, page = _document(max_memory_size=20_000)
    with pytest.raises(pyropdf.LimitError) as error, page.draw() as canvas:
        canvas.textbox((0, 0, 200, 200), "word " * 2_000)
    assert error.value.code == "memory_size"


# Content size.


def test_content_size_is_checked_before_buffering(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_canvas, "_DEFAULT_MAX_CONTENT_SIZE", 200)
    document, page = _document()
    before = _state(document)
    with pytest.raises(pyropdf.LimitError) as error, page.draw() as canvas:
        for _ in range(20):
            canvas.rect((0, 0, 10, 10), fill=RED)
        pytest.fail("the content limit was not checked while buffering")
    assert error.value.code == "content_input_size"
    assert _state(document) == before


def test_content_size_is_checked_before_joining_at_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    _, probe = _document()
    with probe.draw() as canvas:
        canvas.rect((0, 0, 10, 10), fill=RED)
        buffered = canvas._buffered
    # The buffer fits; the page's transform around it does not.
    monkeypatch.setattr(_canvas, "_DEFAULT_MAX_CONTENT_SIZE", buffered + 4)
    document, page = _document()
    before = _state(document)
    with pytest.raises(pyropdf.LimitError) as error, page.draw() as canvas:
        canvas.rect((0, 0, 10, 10), fill=RED)
    assert error.value.code == "content_input_size"
    assert _state(document) == before


def test_an_opacity_group_returns_its_buffer() -> None:
    _, page = _document()
    with page.draw() as canvas:
        with canvas.opacity(0.5):
            canvas.rect((0, 0, 10, 10), fill=RED)
            inside = canvas._buffered
        after = canvas._buffered
        assert after < inside + len(b"q\n/PyroG1 gs\n/PyroX1 Do\nQ") + 2
    assert len(page.content().paths) == 1


# Line splitting.


LINES = [
    "",
    "a",
    "a\n",
    "a\nb",
    "a\r\nb",
    "a\rb\r\n",
    "\r\r\n",
    "\n\n",
    "a\x0bb\x0cc",
    "a\x1cb\x1dc\x1ed",
    "a\x85b c ",
    "trailing\n\n",
    "\nleading",
]


@pytest.mark.parametrize("text", LINES)
def test_line_count_matches_splitlines(text: str) -> None:
    assert _canvas._line_count(text) == len(text.splitlines())


@pytest.mark.parametrize("text", ["one\r\ntwo", "one\x0btwo", "one two", "one\n\ntwo\n"])
def test_text_draws_the_lines_of_splitlines(text: str) -> None:
    _, page = _document()
    with page.draw() as canvas:
        canvas.text(text, (10, 150))
    assert page.text().split() == ["one", "two"]


def test_the_line_limit_counts_before_splitting() -> None:
    _, page = _document()
    most = _canvas._max_lines(1)
    assert most is not None
    with page.draw() as canvas:
        canvas.text("a\n" * most, (10, 150))
    with pytest.raises(pyropdf.LimitError) as error, page.draw() as canvas:
        canvas.text("a\r\n" * most + "a", (10, 150))
    assert error.value.code == "text_line_count"
    # Trusted text takes any number of lines.
    with page.draw() as canvas:
        canvas.text("a\n" * (most + 1), (10, 150), max_text_size=None)
