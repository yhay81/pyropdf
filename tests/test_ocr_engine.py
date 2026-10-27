"""The PP-OCR engine: ``OcrEngine``, ``OcrWord``, and ``Page.ocr``.

Ported from pylopdf 036be111 ``tests/test_ocr_engine.py``. The differences:

- The ``apply_ocr`` tests are not ported; ``Page.apply_ocr`` writes a text
  layer, which is #146.
- pylopdf reads the models from its repository's ``models/`` directory;
  these tests read them from the installed ``pylopdf-ocr-models``, the
  ``ocr`` extra, and skip without it.
- pylopdf draws its pages with ``page.draw``; these tests write content
  streams that show Helvetica.
- The Japanese test reads a region of the corpus's ``mhlw-doc.pdf``, since
  pyropdf embeds no font to draw with.
- ``OcrError`` is a ``PdfError``, which in pyropdf is not a ``ValueError``.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from typing import TYPE_CHECKING, Any

import pytest
from corpus import ASSETS
from test_content import build_raw_pdf

import pyropdf
from pyropdf._core import _OcrEngine

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(scope="module")
def model_paths() -> Any:  # noqa: ANN401 - the models package has no type information.
    """Return the detector, recognizer, and dictionary of the ``ocr`` extra."""
    return pytest.importorskip("pylopdf_ocr_models").model_paths()


@pytest.fixture(scope="module")
def ocr_engine(model_paths: Any) -> pyropdf.OcrEngine:  # noqa: ANN401
    """Load the installed model set once for OCR integration tests."""
    return pyropdf.OcrEngine(model_paths.detector, model_paths.recognizer, model_paths.dictionary)


def _text_page(size: tuple[int, int], *texts: tuple[str, float, float, float]) -> pyropdf.Page:
    """Return a page of ``size`` showing each ``(text, x, y, font size)``, with ``y`` the baseline in display space."""
    width, height = size
    shows = " ".join(f"BT /F1 {font} Tf {x} {height - y} Td ({text}) Tj ET" for text, x, y, font in texts)
    return pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: f"<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 {width} {height}]"
                " /Resources << /Font << /F1 3 0 R >> >> >>",
                3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
                5: f"<< /Length {len(shows)} >>\nstream\n{shows}\nendstream",
            }
        )
    ).pages[0]


def _blank_page(size: tuple[int, int] = (100, 100)) -> pyropdf.Page:
    document = pyropdf.Document()
    return document.pages.new(size)


def test_ocr_model_artifact_hashes(model_paths: Any) -> None:  # noqa: ANN401
    root = model_paths.detector.parent
    checksums = (root / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    for entry in checksums:
        expected, filename = entry.split("  ", maxsplit=1)
        assert hashlib.sha256((root / filename).read_bytes()).hexdigest() == expected


def test_ocr_model_package_exposes_complete_paths(model_paths: Any) -> None:  # noqa: ANN401
    assert model_paths.detector.name == "PP-OCRv6_det_small.rten"
    assert model_paths.recognizer.name == "PP-OCRv6_rec_small.rten"
    assert model_paths.dictionary.name == "ppocrv6_dict.txt"
    assert all(path.is_file() for path in model_paths)
    assert (model_paths.detector.parent / "SHA256SUMS").is_file()


def test_ocr_engine_recognizes_rendered_text(ocr_engine: pyropdf.OcrEngine) -> None:
    page = _text_page((360, 120), ("Invoice 123", 20, 75, 36))

    words = page.ocr(engine=ocr_engine, dpi=150, tile_size=512, overlap=64)

    assert [word.text for word in words] == ["Invoice 123"]
    assert words[0].confidence > 0.99
    assert words[0].bbox.x0 < 20 < words[0].bbox.x1
    assert words[0].bbox.y0 < 60 < words[0].bbox.y1


@pytest.mark.parametrize("alpha", [False, True])
def test_ocr_engine_recognizes_any_pixmap_in_pixel_coordinates(
    ocr_engine: pyropdf.OcrEngine,
    *,
    alpha: bool,
) -> None:
    page = _text_page((360, 120), ("Invoice 123", 20, 75, 36))
    pixmap = page.pixmap(dpi=150, alpha=alpha, background=(255, 255, 255))

    words = ocr_engine.recognize(pixmap, tile_size=512, overlap=64)

    assert [word.text for word in words] == ["Invoice 123"]
    scale = 150 / 72
    assert words[0].bbox.x0 < 20 * scale < words[0].bbox.x1
    assert words[0].bbox.y0 < 60 * scale < words[0].bbox.y1
    with pytest.raises(TypeError, match=r"pixmap must be a pyropdf\.Pixmap"):
        ocr_engine.recognize(page)  # ty: ignore[invalid-argument-type]


def test_ocr_engine_recognizes_a_gray_pixmap(ocr_engine: pyropdf.OcrEngine) -> None:
    page = _text_page((360, 120), ("Invoice 123", 20, 75, 36))

    words = ocr_engine.recognize(page.pixmap(dpi=150, colorspace="gray"), tile_size=512, overlap=64)

    assert [word.text for word in words] == ["Invoice 123"]


def test_ocr_engine_recognizes_japanese(ocr_engine: pyropdf.OcrEngine) -> None:
    page = pyropdf.open(ASSETS / "mhlw-doc.pdf").pages[0]
    hit = page.search("裁判例")[0]

    words = page.ocr(
        engine=ocr_engine, dpi=150, tile_size=512, overlap=64, clip=(hit.x0 - 4, hit.y0 - 4, hit.x1 + 4, hit.y1 + 4)
    )

    assert "裁判例" in "".join(word.text for word in words)
    assert all(hit.x0 - 5 <= word.bbox.x0 and word.bbox.x1 <= hit.x1 + 5 for word in words)


def test_ocr_engine_orders_sustained_columns(ocr_engine: pyropdf.OcrEngine) -> None:
    page = _text_page(
        (500, 180),
        ("Left one", 20, 60, 24),
        ("Left two", 20, 120, 24),
        ("Right one", 290, 60, 24),
        ("Right two", 290, 120, 24),
    )

    words = page.ocr(engine=ocr_engine, dpi=150, tile_size=512, overlap=64)

    assert [word.text for word in words] == ["Left one", "Left two", "Right one", "Right two"]


def test_ocr_engine_rotates_sideways_input(ocr_engine: pyropdf.OcrEngine) -> None:
    page = _text_page((360, 120), ("Invoice 123", 20, 75, 36))
    upright = page.ocr(engine=ocr_engine, dpi=150, tile_size=512, overlap=64)
    page.rotation = 90

    # The page turns clockwise; a counterclockwise turn reads it upright.
    words = page.ocr(engine=ocr_engine, dpi=150, tile_size=512, overlap=64, rotation=270)

    assert [word.text for word in words] == ["Invoice 123"]
    box, before = words[0].bbox, upright[0].bbox
    assert box.width < box.height
    assert box.x0 == pytest.approx(120 - before.y1, abs=4)
    assert box.y0 == pytest.approx(before.x0, abs=4)


def test_ocr_engine_is_reusable_across_concurrent_documents(ocr_engine: pyropdf.OcrEngine) -> None:
    pages = [_text_page((280, 100), (text, 20, 65, 30)) for text in ("Alpha 123", "Beta 456")]

    def recognize(page: pyropdf.Page) -> list[str]:
        words = page.ocr(engine=ocr_engine, dpi=150, tile_size=512, overlap=64)
        return [word.text for word in words]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(recognize, pages))

    assert results == [["Alpha 123"], ["Beta 456"]]


def test_ocr_engine_bounds_complete_concurrent_calls(model_paths: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN401
    class ProbeEngine:
        def __init__(self) -> None:
            self.active = 0
            self.calls = 0
            self.max_active = 0
            self.lock = Lock()
            self.first_entered = Event()
            self.second_entered = Event()
            self.release_first = Event()

        def recognize_pixmap(self, *_args: object, **_kwargs: object) -> list[object]:
            with self.lock:
                self.calls += 1
                call = self.calls
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            if call == 1:
                self.first_entered.set()
                assert self.release_first.wait(timeout=5)
            else:
                self.second_entered.set()
            with self.lock:
                self.active -= 1
            return []

    engine = pyropdf.OcrEngine(model_paths.detector, model_paths.recognizer, model_paths.dictionary, threads=1)
    probe = ProbeEngine()
    monkeypatch.setattr(engine, "_engine", probe)
    pages = [_blank_page(), _blank_page()]

    second_started = Event()

    def second_call() -> list[pyropdf.OcrWord]:
        second_started.set()
        return pages[1].ocr(engine=engine, dpi=72, tile_size=256, overlap=32)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(pages[0].ocr, engine=engine, dpi=72, tile_size=256, overlap=32)
        assert probe.first_entered.wait(timeout=5)
        second = pool.submit(second_call)
        assert second_started.wait(timeout=5)
        assert not probe.second_entered.wait(timeout=0.2)
        probe.release_first.set()
        assert first.result(timeout=5) == []
        assert second.result(timeout=5) == []

    assert probe.second_entered.is_set()
    assert probe.max_active == 1
    assert engine.max_concurrent == 1


def test_ocr_engine_can_raise_measured_concurrency(model_paths: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN401
    class ProbeEngine:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.lock = Lock()
            self.barrier = Barrier(2)

        def recognize_pixmap(self, *_args: object, **_kwargs: object) -> list[object]:
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            self.barrier.wait(timeout=5)
            with self.lock:
                self.active -= 1
            return []

    engine = pyropdf.OcrEngine(
        model_paths.detector, model_paths.recognizer, model_paths.dictionary, threads=1, max_concurrent=2
    )
    probe = ProbeEngine()
    monkeypatch.setattr(engine, "_engine", probe)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda page: page.ocr(engine=engine, dpi=72, tile_size=256, overlap=32),
                [_blank_page(), _blank_page()],
            )
        )

    assert results == [[], []]
    assert probe.max_active == 2
    assert engine.max_concurrent == 2


@pytest.mark.parametrize("dpi", [0, -1, float("inf"), float("nan"), "bad"])
def test_ocr_engine_rejects_invalid_dpi(ocr_engine: pyropdf.OcrEngine, dpi: object) -> None:
    with pytest.raises(pyropdf.OcrError, match="dpi"):
        _blank_page().ocr(engine=ocr_engine, dpi=dpi)  # ty: ignore[invalid-argument-type]


def test_ocr_engine_rejects_incomplete_or_missing_model_paths(tmp_path: Path) -> None:
    with pytest.raises(pyropdf.OcrError, match="provided together"):
        pyropdf.OcrEngine(tmp_path / "det.onnx", None, tmp_path / "dict.txt")
    with pytest.raises(FileNotFoundError, match=r"det\.onnx"):
        pyropdf.OcrEngine(tmp_path / "det.onnx", tmp_path / "rec.onnx", tmp_path / "dict.txt")


def _tiny_models(tmp_path: Path) -> tuple[Path, Path, Path]:
    detector = tmp_path / "det.rten"
    recognizer = tmp_path / "rec.rten"
    dictionary = tmp_path / "dict.txt"
    detector.write_bytes(b"det!")
    recognizer.write_bytes(b"rec!")
    dictionary.write_bytes(b"x\n")
    return detector, recognizer, dictionary


def test_ocr_engine_bounds_cumulative_model_input(tmp_path: Path) -> None:
    detector, recognizer, dictionary = _tiny_models(tmp_path)

    with pytest.raises(pyropdf.LimitError) as caught:
        pyropdf.OcrEngine(detector, recognizer, dictionary, max_model_size=9)
    assert caught.value.code == "ocr_model_size"

    with pytest.raises(pyropdf.OcrError, match="failed to load OCR detector"):
        pyropdf.OcrEngine(detector, recognizer, dictionary, max_model_size=10)
    with pytest.raises(pyropdf.OcrError, match="failed to load OCR detector"):
        pyropdf.OcrEngine(detector, recognizer, dictionary, max_model_size=None)


def test_ocr_core_repeats_cumulative_model_limit(tmp_path: Path) -> None:
    detector, recognizer, dictionary = _tiny_models(tmp_path)

    with pytest.raises(pyropdf.LimitError) as caught:
        _OcrEngine(str(detector), str(recognizer), str(dictionary), 1, 9)
    assert caught.value.code == "ocr_model_size"


def test_ocr_engine_bounds_dictionary_entries(tmp_path: Path) -> None:
    detector = tmp_path / "det.rten"
    recognizer = tmp_path / "rec.rten"
    dictionary = tmp_path / "dict.txt"
    detector.write_bytes(b"x")
    recognizer.write_bytes(b"x")
    dictionary.write_text("x\n" * 65_537, encoding="utf-8")

    with pytest.raises(pyropdf.LimitError) as caught:
        pyropdf.OcrEngine(detector, recognizer, dictionary, max_model_size=None)
    assert caught.value.code == "ocr_dictionary_entries"


def test_ocr_engine_rejects_mismatched_dictionary(model_paths: Any, tmp_path: Path) -> None:  # noqa: ANN401
    dictionary = tmp_path / "mismatched.txt"
    dictionary.write_text("x\n", encoding="utf-8")
    engine = pyropdf.OcrEngine(model_paths.detector, model_paths.recognizer, dictionary)
    page = _text_page((200, 100), ("Text", 20, 60, 30))

    with pytest.raises(pyropdf.OcrError, match="dictionary requires"):
        page.ocr(engine=engine, dpi=150, tile_size=512, overlap=64)


def test_ocr_engine_discovers_installed_model_extra(model_paths: Any) -> None:  # noqa: ANN401, ARG001
    engine = pyropdf.OcrEngine()
    assert 1 <= engine.threads <= 4
    assert engine.max_model_size == 64 * 1024 * 1024


@pytest.mark.parametrize(
    ("threads", "error"),
    [(0, pyropdf.OcrError), (17, pyropdf.OcrError), (True, TypeError), (1.5, TypeError)],
)
def test_ocr_engine_validates_threads(threads: object, error: type[Exception]) -> None:
    with pytest.raises(error, match="threads"):
        pyropdf.OcrEngine(threads=threads)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    ("max_concurrent", "error"),
    [(0, pyropdf.OcrError), (17, pyropdf.OcrError), (True, TypeError), (1.5, TypeError)],
)
def test_ocr_engine_validates_max_concurrent(max_concurrent: object, error: type[Exception]) -> None:
    with pytest.raises(error, match="max_concurrent"):
        pyropdf.OcrEngine(max_concurrent=max_concurrent)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    ("max_model_size", "error"),
    [(0, ValueError), (-1, ValueError), (True, TypeError), (1.5, TypeError)],
)
def test_ocr_engine_validates_max_model_size(max_model_size: object, error: type[Exception]) -> None:
    with pytest.raises(error, match="max_model_size"):
        pyropdf.OcrEngine(max_model_size=max_model_size)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    ("rotation", "error"),
    [(45, pyropdf.OcrError), (-90, pyropdf.OcrError), (360, pyropdf.OcrError), (True, TypeError), (1.5, TypeError)],
)
def test_ocr_engine_validates_input_rotation(
    ocr_engine: pyropdf.OcrEngine,
    rotation: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error, match="rotation"):
        _blank_page().ocr(engine=ocr_engine, dpi=72, rotation=rotation)  # ty: ignore[invalid-argument-type]


def test_ocr_engine_validates_runtime_options(ocr_engine: pyropdf.OcrEngine) -> None:
    page = _blank_page()
    with pytest.raises(pyropdf.OcrError, match="tile_size"):
        page.ocr(engine=ocr_engine, dpi=72, tile_size=500)
    with pytest.raises(pyropdf.OcrError, match="overlap"):
        page.ocr(engine=ocr_engine, dpi=72, tile_size=512, overlap=16)
    with pytest.raises(pyropdf.OcrError, match="min_confidence"):
        page.ocr(engine=ocr_engine, dpi=72, tile_size=512, overlap=64, min_confidence=2)
    with pytest.raises(TypeError, match="tile_size"):
        page.ocr(engine=ocr_engine, dpi=72, tile_size=512.0)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="overlap"):
        page.ocr(engine=ocr_engine, dpi=72, tile_size=512, overlap=True)
    with pytest.raises(TypeError, match="dpi"):
        page.ocr(engine=ocr_engine, dpi=True)
    with pytest.raises(TypeError, match="min_confidence"):
        page.ocr(engine=ocr_engine, dpi=72, min_confidence=True)
    with pytest.raises(TypeError, match="engine must be"):
        page.ocr(engine=object())  # ty: ignore[invalid-argument-type]
    with pytest.raises(pyropdf.PdfError, match="clip does not intersect"):
        page.ocr(engine=ocr_engine, dpi=72, clip=(200, 200, 300, 300))


def test_ocr_core_validates_its_options(ocr_engine: pyropdf.OcrEngine) -> None:
    pixmap = _blank_page().pixmap()
    core = ocr_engine._engine  # noqa: SLF001
    cases: list[tuple[dict[str, Any], str]] = [
        ({"tile_size": 500}, "tile_size"),
        ({"overlap": 16}, "overlap"),
        ({"min_confidence": float("nan")}, "min_confidence"),
        ({"rotation": 45}, "rotation"),
    ]
    for options, match in cases:
        with pytest.raises(pyropdf.OcrError, match=match):
            core.recognize_pixmap(pixmap, **options)
    with pytest.raises(pyropdf.OcrError, match="threads"):
        _OcrEngine("det", "rec", "dict", 17, None)
    with pytest.raises(ValueError, match="max_model_size"):
        _OcrEngine("det", "rec", "dict", 1, 0)


def test_ocr_error_hierarchy() -> None:
    assert issubclass(pyropdf.OcrError, pyropdf.PdfError)
    assert pyropdf.OcrError.__module__ == "pyropdf"
    assert pyropdf.OcrEngine.__module__ == pyropdf.OcrWord.__module__ == "pyropdf"
