"""The PP-OCR engine: ``OcrEngine``, ``OcrWord``, and ``Page.ocr``.

Ported from pylopdf 036be111 ``tests/test_ocr_engine.py``. The differences:

- The ``apply_ocr`` tests embed the raster of a source page as an image
  XObject, where pylopdf draws its PNG with ``page.draw``.
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
import zlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from typing import TYPE_CHECKING, Any

import pytest
from corpus import ASSETS
from test_content import build_raw_pdf

import pyropdf
from pyropdf._core import _OcrEngine

if TYPE_CHECKING:
    from collections.abc import Sequence
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


def _scanned_page(
    size: tuple[int, int],
    raster: pyropdf.Pixmap,
    box: tuple[float, float, float, float] | None = None,
    *texts: tuple[str, float, float, float],
) -> pyropdf.Page:
    """Return a page of ``size`` that draws an RGB ``raster`` into the display ``box`` and shows ``texts``."""
    width, height = size
    x0, y0, x1, y1 = box or (0, 0, width, height)
    assert (raster.channels, raster.stride) == (3, raster.width * 3)
    samples = zlib.compress(bytes(raster))
    image = (
        (
            f"<< /Type /XObject /Subtype /Image /Width {raster.width} /Height {raster.height} /ColorSpace /DeviceRGB"
            f" /BitsPerComponent 8 /Filter /FlateDecode /Length {len(samples)} >>\nstream\n"
        ).encode()
        + samples
        + b"\nendstream"
    )
    shows = " ".join(f"BT /F1 {font} Tf {x} {height - y} Td ({text}) Tj ET" for text, x, y, font in texts)
    content = f"q {x1 - x0} 0 0 {y1 - y0} {x0} {height - y1} cm /Im0 Do Q {shows}"
    return pyropdf.open(
        build_raw_pdf(
            {
                1: "<< /Type /Catalog /Pages 2 0 R >>",
                2: f"<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 {width} {height}]"
                " /Resources << /Font << /F1 3 0 R >> /XObject << /Im0 6 0 R >> >> >>",
                3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                4: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
                5: f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
                6: image,
            }
        )
    ).pages[0]


def _white(page: pyropdf.Page) -> bytes:
    return bytes(page.pixmap(background=(255, 255, 255), alpha=True))


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


def test_apply_ocr_adds_searchable_invisible_text(ocr_engine: pyropdf.OcrEngine) -> None:
    raster = _text_page((360, 120), ("Search 456", 20, 75, 36)).pixmap(dpi=150)
    target = _scanned_page((360, 120), raster)
    before = _white(target)

    words = target.apply_ocr(dpi=150, engine=ocr_engine, tile_size=512, overlap=64)

    assert [word.text for word in words] == ["Search 456"]
    assert target.search("Search 456")
    assert _white(target) == before
    reopened = pyropdf.open(target.document.to_bytes()).pages[0]
    assert reopened.search("Search 456")
    assert _white(reopened) == before


def test_apply_ocr_clip_handles_mixed_content(ocr_engine: pyropdf.OcrEngine) -> None:
    raster = _text_page((360, 120), ("Region 789", 20, 75, 36)).pixmap(dpi=150)
    target = _scanned_page((360, 220), raster, (0, 80, 360, 200), ("Digital header", 20, 30, 14))
    before = _white(target)

    words = target.apply_ocr(dpi=150, engine=ocr_engine, tile_size=512, overlap=64, clip=(0, 70, 360, 210))

    assert [word.text for word in words] == ["Region 789"]
    assert words[0].bbox.y0 > 70
    assert target.search("Region 789")
    assert _white(target) == before


@pytest.mark.parametrize(("page_rotation", "ocr_rotation"), [(90, 270), (180, 180), (270, 90)])
def test_apply_ocr_corrects_rotated_scan_without_rotating_page(
    ocr_engine: pyropdf.OcrEngine,
    page_rotation: int,
    ocr_rotation: pyropdf.OcrRotation,
) -> None:
    raster = _text_page((360, 200), ("First line 111", 20, 65, 28), ("Second line 222", 20, 145, 28)).pixmap(dpi=150)
    target = _scanned_page((360, 200), raster)
    target.rotation = page_rotation
    before = _white(target)

    words = target.apply_ocr(dpi=150, engine=ocr_engine, tile_size=512, overlap=64, rotation=ocr_rotation)

    assert [word.text for word in words] == ["First line 111", "Second line 222"]
    assert target.text() == "First line 111\nSecond line 222\n"
    for word in words:
        if page_rotation in (90, 270):
            assert word.bbox.height > word.bbox.width
        else:
            assert word.bbox.width > word.bbox.height
        hits = target.search(word.text)
        assert len(hits) == 1
        assert tuple(hits[0]) == pytest.approx(tuple(word.bbox), abs=0.2)
    assert target.rotation == page_rotation
    assert _white(target) == before
    reopened = pyropdf.open(target.document.to_bytes()).pages[0]
    assert reopened.text() == "First line 111\nSecond line 222\n"
    assert reopened.search("First line 111")
    assert reopened.search("Second line 222")
    assert reopened.rotation == page_rotation
    assert _white(reopened) == before


def test_apply_ocr_skips_existing_text_by_default() -> None:
    page = _text_page((200, 100), ("Already searchable", 20, 50, 12))

    assert page.apply_ocr() == []
    assert page.ocr(mode="skip_existing") == []
    with pytest.raises(pyropdf.OcrError, match="dpi"):
        page.apply_ocr(dpi=-1, mode="skip_existing")
    with pytest.raises(TypeError, match="engine"):
        page.apply_ocr(engine="bad", mode="skip_existing")  # ty: ignore[invalid-argument-type]
    with pytest.raises(pyropdf.PdfError, match="clip"):
        page.apply_ocr(clip=(300, 300, 400, 400), mode="skip_existing")
    with pytest.raises(TypeError, match="mode"):
        page.apply_ocr(mode=1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="mode"):
        page.apply_ocr(mode="invalid")  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="mode"):
        page.ocr(mode=True)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="mode"):
        page.ocr(mode="invalid")  # ty: ignore[invalid-argument-type]


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


def test_page_ocr_observes_cancellation_after_rendering(
    ocr_engine: pyropdf.OcrEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = pyropdf.CancellationToken()
    document = pyropdf.open(
        _text_page((200, 100), ("Hello", 20, 60, 30)).document.to_bytes(),
        limits=pyropdf.DocumentLimits(cancellation=token),
    )
    render = pyropdf.Page.pixmap

    def cancel_after_render(page: pyropdf.Page, **kwargs: Any) -> pyropdf.Pixmap:  # noqa: ANN401
        pixmap = render(page, **kwargs)
        token.cancel()
        return pixmap

    monkeypatch.setattr(pyropdf.Page, "pixmap", cancel_after_render)
    with pytest.raises(pyropdf.LimitError) as caught:
        document.pages[0].ocr(engine=ocr_engine, dpi=72, tile_size=256, overlap=32)
    assert caught.value.code == "cancelled"


def _mixed_page(  # noqa: PLR0913 - test helper parameters mirror page properties.
    size: tuple[int, int],
    images: Sequence[tuple[pyropdf.Pixmap, tuple[float, float, float, float]]],
    *,
    texts: Sequence[tuple[str, float, float, float]] = (),
    extra_ops: str = "",
    crop_box: tuple[float, float, float, float] | None = None,
    user_unit: float | None = None,
    limits: pyropdf.DocumentLimits | None = None,
) -> pyropdf.Page:
    """Build a page combining multiple RGB image placements, native texts, and optional raw operators."""
    width, height = size
    objects: dict[int, str | bytes] = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    xobj_entries: list[str] = []
    draw_ops: list[str] = []
    for index, (raster, box) in enumerate(images):
        obj_id = 6 + index
        assert (raster.channels, raster.stride) == (3, raster.width * 3)
        samples = zlib.compress(bytes(raster))
        objects[obj_id] = (
            (
                f"<< /Type /XObject /Subtype /Image /Width {raster.width} /Height {raster.height}"
                f" /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length {len(samples)} >>\nstream\n"
            ).encode()
            + samples
            + b"\nendstream"
        )
        xobj_entries.append(f"/Im{index} {obj_id} 0 R")
        x0, y0, x1, y1 = box
        draw_ops.append(f"q {x1 - x0} 0 0 {y1 - y0} {x0} {height - y1} cm /Im{index} Do Q")
    shows = " ".join(f"BT /F1 {font} Tf {x} {height - y} Td ({text}) Tj ET" for text, x, y, font in texts)
    content = " ".join([*draw_ops, shows, extra_ops]).strip()
    xobj_dict = f" /XObject << {' '.join(xobj_entries)} >>" if xobj_entries else ""
    crop_entry = f" /CropBox [{' '.join(str(v) for v in crop_box)}]" if crop_box is not None else ""
    unit_entry = f" /UserUnit {user_unit}" if user_unit is not None else ""
    objects[2] = (
        f"<< /Type /Pages /Kids [4 0 R] /Count 1 /MediaBox [0 0 {width} {height}]"
        f" /Resources << /Font << /F1 3 0 R >>{xobj_dict} >> >>"
    )
    objects[4] = f"<< /Type /Page /Parent 2 0 R /Contents 5 0 R{crop_entry}{unit_entry} >>"
    objects[5] = f"<< /Length {len(content)} >>\nstream\n{content}\nendstream"
    return pyropdf.open(build_raw_pdf(objects), limits=limits).pages[0]


def test_hybrid_ocr_recognizes_only_raster_region_without_duplicating_native_text(
    ocr_engine: pyropdf.OcrEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raster = _text_page((360, 110), ("Region 789", 20, 70, 34)).pixmap(dpi=150)
    page = _mixed_page(
        (360, 240),
        [(raster, (0, 80, 360, 190))],
        texts=[("Digital header", 20, 30, 14), ("Native footer", 20, 222, 14)],
    )
    before = _white(page)
    clips: list[object] = []
    real_pixmap = pyropdf.Page.pixmap

    def spy_pixmap(self: pyropdf.Page, **kwargs: Any) -> pyropdf.Pixmap:  # noqa: ANN401
        clips.append(kwargs.get("clip"))
        return real_pixmap(self, **kwargs)

    monkeypatch.setattr(pyropdf.Page, "pixmap", spy_pixmap)
    words = page.ocr(engine=ocr_engine, dpi=150, tile_size=512, overlap=64, mode="hybrid")

    assert len(clips) == 1
    assert clips[0] == pytest.approx((0, 80, 360, 190), abs=0.5)
    assert [word.text for word in words] == ["Region 789"]
    assert 80 <= words[0].bbox.y0 < words[0].bbox.y1 <= 190

    clips.clear()
    inserted = page.apply_ocr(engine=ocr_engine, dpi=150, tile_size=512, overlap=64, mode="hybrid")
    assert len(clips) == 1
    assert [word.text for word in inserted] == ["Region 789"]

    # Re-extraction keeps native text once and adds raster text once with zero duplicates.
    extracted = [word.text for word in page.words()]
    assert extracted.count("Digital") == 1
    assert extracted.count("header") == 1
    assert extracted.count("Region") == 1
    assert extracted.count("789") == 1
    assert extracted.count("Native") == 1
    assert extracted.count("footer") == 1
    assert _white(page) == before

    # Calling apply_ocr(mode="hybrid") a second time is idempotent and does not edit the document.
    after_first = page.document.to_bytes()
    assert page.apply_ocr(engine=ocr_engine, dpi=150, tile_size=512, overlap=64, mode="hybrid") == []
    assert page.document.to_bytes() == after_first


def test_hybrid_ocr_deduplicates_overlaid_native_text_and_ignores_invisible_and_artifacts(
    ocr_engine: pyropdf.OcrEngine,
) -> None:
    # Full-page scan image with a visible native header stamped near the top,
    # an /Artifact page marker, and invisible 3 Tr placeholder noise over the scan text.
    raster = _text_page((360, 220), ("Scanned 456", 20, 155, 34)).pixmap(dpi=150)
    extra_ops = (
        "/Artifact << /Type /Pagination >> BDC BT /F1 12 Tf 290 190 Td (Page 1) Tj ET EMC "
        "BT /F1 34 Tf 3 Tr 20 65 Td (Noise) Tj ET"
    )
    page = _mixed_page(
        (360, 220),
        [(raster, (0, 0, 360, 220))],
        texts=[("Digital header", 20, 30, 14)],
        extra_ops=extra_ops,
    )

    inserted = page.apply_ocr(engine=ocr_engine, dpi=150, tile_size=512, overlap=64, mode="hybrid")

    assert [word.text for word in inserted] == ["Scanned 456"]
    extracted = [word.text for word in page.words()]
    assert extracted.count("Digital") == 1
    assert extracted.count("header") == 1
    assert extracted.count("Scanned") == 1
    assert extracted.count("456") == 1


def test_hybrid_ocr_skips_pure_text_and_text_covered_banner_without_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    real_pixmap = pyropdf.Page.pixmap

    def spy_pixmap(self: pyropdf.Page, **kwargs: Any) -> pyropdf.Pixmap:  # noqa: ANN401
        nonlocal calls
        calls += 1
        return real_pixmap(self, **kwargs)

    # 1. Pure text page: no raster images -> 0 renders, does not even need an OCR engine.
    text_only = _text_page((300, 120), ("Pure digital text", 20, 60, 18))
    banner = _blank_page((200, 30)).pixmap(dpi=72)
    covered = _mixed_page((300, 120), [(banner, (20, 20, 220, 50))], texts=[("Banner Title", 25, 42, 22)])
    monkeypatch.setattr(pyropdf.Page, "pixmap", spy_pixmap)
    assert text_only.ocr(mode="hybrid") == []
    assert text_only.apply_ocr(mode="hybrid") == []
    assert calls == 0

    # 2. Banner image covered by legible native text -> 0 renders.
    assert covered.ocr(mode="hybrid") == []
    assert calls == 0


def test_hybrid_ocr_aligns_on_rotated_cropped_and_user_unit_pages(ocr_engine: pyropdf.OcrEngine) -> None:
    # 1. Rotated page (90 degrees) with mixed native text and an embedded raster image.
    raster = _text_page((360, 110), ("Rotated 321", 20, 70, 34)).pixmap(dpi=150)
    rotated = _mixed_page(
        (360, 220),
        [(raster, (0, 90, 360, 200))],
        texts=[("Native title", 20, 30, 14)],
    )
    rotated.rotation = 90
    before_rot = _white(rotated)

    words = rotated.apply_ocr(engine=ocr_engine, dpi=150, tile_size=512, overlap=64, mode="hybrid", rotation=270)

    assert [word.text for word in words] == ["Rotated 321"]
    (image_item,) = rotated.images()
    assert image_item.item.bbox.intersects(words[0].bbox)
    (hit,) = rotated.search("Rotated 321")
    assert tuple(hit) == pytest.approx(tuple(words[0].bbox), abs=0.2)
    assert [word.text for word in rotated.words()].count("Native") == 1
    assert _white(rotated) == before_rot

    # 2. Cropped page (/CropBox offset from MediaBox origin) with native header and raster image.
    cropped_raster = _text_page((320, 100), ("Cropped 654", 20, 65, 30)).pixmap(dpi=150)
    cropped = _mixed_page(
        (460, 300),
        [(cropped_raster, (60, 120, 380, 220))],
        texts=[("Crop header", 70, 80, 14)],
        crop_box=(40, 40, 420, 260),
    )
    crop_words = cropped.apply_ocr(engine=ocr_engine, dpi=150, tile_size=512, overlap=64, mode="hybrid")
    assert [word.text for word in crop_words] == ["Cropped 654"]
    (crop_image,) = cropped.images()
    assert crop_image.item.bbox.intersects(crop_words[0].bbox)
    assert cropped.search("Cropped 654")
    assert [word.text for word in cropped.words()].count("Crop") == 1

    # 3. /UserUnit 2 scaled page with native header and raster image.
    unit_raster = _text_page((180, 60), ("Unit 987", 10, 42, 22)).pixmap(dpi=150)
    scaled = _mixed_page(
        (200, 120),
        [(unit_raster, (10, 45, 190, 105))],
        texts=[("Scaled header", 10, 20, 10)],
        user_unit=2.0,
    )
    unit_words = scaled.apply_ocr(engine=ocr_engine, dpi=150, tile_size=512, overlap=64, mode="hybrid")
    assert [word.text for word in unit_words] == ["Unit 987"]
    (unit_image,) = scaled.images()
    assert unit_image.item.bbox.intersects(unit_words[0].bbox)
    assert scaled.search("Unit 987")
    assert [word.text for word in scaled.words()].count("Scaled") == 1


def test_hybrid_ocr_coalesces_many_images_and_enforces_document_limits(
    model_paths: Any,  # noqa: ANN401
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProbeCore:
        def __init__(self) -> None:
            self.calls = 0

        def recognize_pixmap(self, *_args: object, **_kwargs: object) -> list[object]:
            self.calls += 1
            return []

    engine = pyropdf.OcrEngine(model_paths.detector, model_paths.recognizer, model_paths.dictionary, threads=1)
    probe = ProbeCore()
    monkeypatch.setattr(engine, "_engine", probe)

    tiny = _blank_page((8, 8)).pixmap(dpi=72)
    # 1. Many tiny isolated decorative icons (< 8pt) -> 0 renders.
    decorative = _mixed_page(
        (400, 400),
        [(tiny, (10 + (i % 10) * 35, 10 + (i // 10) * 35, 15 + (i % 10) * 35, 15 + (i // 10) * 35)) for i in range(60)],
    )
    assert decorative.ocr(engine=engine, dpi=72, tile_size=256, overlap=32, mode="hybrid") == []
    assert probe.calls == 0

    # 2. Strip-sliced scan (50 touching 3pt horizontal strips) -> coalesced to 1 render call.
    strip_page = _mixed_page(
        (300, 200),
        [(tiny, (10, 10 + i * 3, 290, 13 + i * 3)) for i in range(50)],
    )
    assert strip_page.ocr(engine=engine, dpi=72, tile_size=256, overlap=32, mode="hybrid") == []
    assert probe.calls == 1

    # 3. Many scattered 20x20 images (> 32) -> coalesced within bounded region cap (<= 32 renders).
    probe.calls = 0
    scattered = _mixed_page(
        (500, 500),
        [(tiny, (10 + (i % 8) * 45, 10 + (i // 8) * 45, 30 + (i % 8) * 45, 30 + (i // 8) * 45)) for i in range(64)],
    )
    assert scattered.ocr(engine=engine, dpi=72, tile_size=256, overlap=32, mode="hybrid") == []
    assert 1 <= probe.calls <= 32

    # 4. Cancellation between multi-region renders stops immediately with LimitError("cancelled").
    token = pyropdf.CancellationToken()
    two_regions = _mixed_page(
        (300, 300),
        [(tiny, (10, 10, 200, 100)), (tiny, (10, 180, 200, 280))],
        limits=pyropdf.DocumentLimits(cancellation=token),
    )

    class CancellingCore:
        def recognize_pixmap(self, *_args: object, **_kwargs: object) -> list[object]:
            token.cancel()
            return []

    monkeypatch.setattr(engine, "_engine", CancellingCore())
    with pytest.raises(pyropdf.LimitError) as caught:
        two_regions.ocr(engine=engine, dpi=72, tile_size=256, overlap=32, mode="hybrid")
    assert caught.value.code == "cancelled"

    # 5. Cumulative raster size across regions is checked against ocr_raster_size before rendering.
    fresh_two_regions = _mixed_page(
        (300, 300),
        [(tiny, (10, 10, 200, 100)), (tiny, (10, 180, 200, 280))],
    )
    monkeypatch.setattr(engine, "_engine", probe)
    probe.calls = 0
    with pytest.raises(pyropdf.LimitError) as caught_raster:
        fresh_two_regions.ocr(engine=engine, dpi=7200, tile_size=256, overlap=32, mode="hybrid")
    assert caught_raster.value.code == "ocr_raster_size"
    assert probe.calls == 0
