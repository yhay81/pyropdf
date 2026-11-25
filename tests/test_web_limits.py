"""DocumentLimits, DocumentLimits.web(), budget, and cancellation coverage for v0.4.0 and image/content paths."""

from __future__ import annotations

import io
import time
import zlib
from typing import TYPE_CHECKING

import pytest
from PIL import Image

import pyropdf
from pyropdf import (
    AssociatedFileRelationship,
    BeadSpecification,
    CancellationToken,
    Collection,
    CollectionField,
    CollectionFieldKind,
    CollectionView,
    DeveloperExtension,
    DocumentLimits,
    LimitError,
    Name,
    Operation,
    Operations,
    PieceData,
    _core,
    _images,
    _xobjects,
)

if TYPE_CHECKING:
    from pathlib import Path


def _png_bytes(width: int = 16, height: int = 16, *, alpha: bool = False) -> bytes:
    mode = "RGBA" if alpha else "RGB"
    color = (40, 120, 200, 180) if alpha else (40, 120, 200)
    buf = io.BytesIO()
    Image.new(mode, (width, height), color).save(buf, "PNG")
    return buf.getvalue()


def _noise_jpeg_bytes(width: int = 128, height: int = 128) -> bytes:
    """Return a high-entropy JPEG large enough (~20 KiB) to exceed small memory budgets after open."""
    state = 0x12345678
    raw = bytearray(width * height * 3)
    for index in range(len(raw)):
        state = (1_664_525 * state + 1_013_904_223) & 0xFFFFFFFF
        raw[index] = state >> 24
    buf = io.BytesIO()
    Image.frombytes("RGB", (width, height), bytes(raw)).save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _jpeg_bytes(width: int = 64, height: int = 64, *, mode: str = "RGB") -> bytes:
    buf = io.BytesIO()
    if mode == "CMYK":
        img = Image.new("CMYK", (width, height), (20, 40, 60, 80))
    elif mode == "L":
        img = Image.linear_gradient("L").resize((width, height))
    else:
        img = Image.linear_gradient("L").resize((width, height)).convert("RGB")
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _build_raw_pdf(objects: dict[int, str | bytes], *, version: str = "1.7") -> bytes:
    out = bytearray(f"%PDF-{version}\n".encode("ascii"))
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        body = objects[number]
        raw = body.encode("latin-1") if isinstance(body, str) else body
        out += f"{number} 0 obj\n".encode("ascii") + raw + b"\nendobj\n"
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode("ascii")
    for number in range(1, size):
        if number in offsets:
            out += f"{offsets[number]:010} 00000 n \n".encode("ascii")
        else:
            out += b"0000000000 65535 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    return bytes(out)


def _raw_stream(data: str | bytes, extra: str = "") -> bytes:
    raw = data.encode("latin-1") if isinstance(data, str) else data
    return f"<< /Length {len(raw)} {extra}>>\nstream\n".encode("ascii") + raw + b"\nendstream"


def _doc_with_image(*, limits: DocumentLimits | None = None) -> tuple[pyropdf.Document, int]:
    doc = pyropdf.Document(limits=limits)
    doc.pages.new((200, 200))
    xref = _images.add_image(doc, _png_bytes(8, 8))
    page = doc.pages[0]
    page.obj["Resources"] = {"XObject": {"Im0": doc.objects[xref]}}
    page.operations = Operations(
        [
            Operation("q"),
            Operation("cm", (72, 0, 0, 72, 0, 0)),
            Operation("Do", (Name("Im0"),)),
            Operation("Q"),
        ]
    )
    return doc, xref


def _image_pdf(data: bytes, entries: dict[str, object], placement: tuple[float, float] = (72.0, 72.0)) -> bytes:
    with pyropdf.Document() as doc:
        xref = doc.objects.add_stream(data, {"Type": Name("XObject"), "Subtype": Name("Image")}, filter=None)
        doc.objects[xref].update(entries)
        doc.pages.new((200, 200))
        page = doc.pages[0]
        page.obj["Resources"] = {"XObject": {"Im0": doc.objects[xref]}}
        page.obj["Contents"] = doc.objects[
            doc.objects.add_stream(f"q {placement[0]} 0 0 {placement[1]} 0 0 cm /Im0 Do Q".encode("ascii"))
        ]
        return doc.to_bytes()


def _masked_image_pdf() -> bytes:
    width, height = 64, 64
    mask_side = 128
    samples = bytes(((x * 7 + y * 13) & 0xFF) for y in range(height) for x in range(width) for _ in range(3))
    alpha = bytes(((x * 5 + y * 3) & 0xFF) for y in range(mask_side) for x in range(mask_side))
    with pyropdf.Document() as doc:
        mask_xref = doc.objects.add_stream(
            zlib.compress(alpha),
            {
                "Type": Name("XObject"),
                "Subtype": Name("Image"),
                "Width": mask_side,
                "Height": mask_side,
                "BitsPerComponent": 8,
                "ColorSpace": Name("DeviceGray"),
                "Filter": Name("FlateDecode"),
            },
            filter=None,
        )
        img_xref = doc.objects.add_stream(
            zlib.compress(samples),
            {
                "Type": Name("XObject"),
                "Subtype": Name("Image"),
                "Width": width,
                "Height": height,
                "BitsPerComponent": 8,
                "ColorSpace": Name("DeviceRGB"),
                "Filter": Name("FlateDecode"),
                "SMask": doc.objects[mask_xref],
            },
            filter=None,
        )
        doc.pages.new((200, 200))
        page = doc.pages[0]
        page.obj["Resources"] = {"XObject": {"Im0": doc.objects[img_xref]}}
        page.obj["Contents"] = doc.objects[doc.objects.add_stream(b"q 36 0 0 36 0 0 cm /Im0 Do Q")]
        return doc.to_bytes()


def _form_and_appearance_pdf(*, content: str = "q 1 0 0 1 0 0 cm Q\n" * 400) -> bytes:
    return _build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 200] >>",
            3: (
                "<< /Type /Page /Parent 2 0 R /Resources << /XObject << /Fm0 4 0 R >> >> "
                "/Contents 5 0 R /Annots [6 0 R 7 0 R] >>"
            ),
            4: _raw_stream(content, "/Type /XObject /Subtype /Form /BBox [0 0 100 100] "),
            5: _raw_stream("q /Fm0 Do Q"),
            6: "<< /Type /Annot /Subtype /Square /Rect [10 10 50 50] /AP << /N 8 0 R >> >>",
            7: "<< /Type /Annot /Subtype /Square /Rect [60 10 100 50] /AP << /N 9 0 R >> >>",
            8: _raw_stream(content, "/Type /XObject /Subtype /Form /BBox [0 0 40 40] "),
            9: _raw_stream(content, "/Type /XObject /Subtype /Form /BBox [0 0 40 40] "),
        }
    )


def _tagged_pdf(paragraphs: int = 2) -> bytes:
    ops = ["BT /F1 12 Tf 10 180 Td"]
    kids: list[str] = []
    objects: dict[int, str | bytes] = {
        1: "<< /Type /Catalog /Version /2.0 /Pages 2 0 R /StructTreeRoot 7 0 R /MarkInfo << /Marked true >> >>",
        2: ("<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 200] /Resources << /Font << /F1 6 0 R >> >> >>"),
        3: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R /StructParents 0 >>",
        6: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        7: "<< /Type /StructTreeRoot /K 8 0 R >>",
    }
    for index in range(paragraphs):
        obj_num = 9 + index
        kids.append(f"{obj_num} 0 R")
        ops.append(f"0 -12 Td /P << /MCID {index} >> BDC (Paragraph {index}) Tj EMC")
        objects[obj_num] = f"<< /Type /StructElem /S /P /P 8 0 R /Pg 3 0 R /K {index} >>"
    ops.append("ET")
    objects[5] = _raw_stream(" ".join(ops))
    objects[8] = f"<< /Type /StructElem /S /Document /P 7 0 R /K [{' '.join(kids)}] >>"
    return _build_raw_pdf(objects, version="2.0")


def _chunks_pdf(lines: int = 2) -> bytes:
    body = ["BT /F1 18 Tf 40 360 Td (Overview) Tj ET"]
    body.extend(
        f"BT /F1 10 Tf 40 {340 - index * 12} Td (Paragraph line {index} tests retrieval chunks under web limits.) Tj ET"
        for index in range(lines)
    )
    content = "\n".join(body) + "\n"
    return _build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: (
                "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 300 400] "
                "/Resources << /Font << /F1 4 0 R >> >> >>"
            ),
            3: "<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>",
            4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            5: _raw_stream(content),
        }
    )


# ---------------------------------------------------------------------------
# 1. Image inputs: replace_image, add_image, Canvas.image
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [1024, 10**12, True, "png", None, [1, 2, 3]])
def test_replace_image_and_add_image_reject_non_buffer_inputs(bad: object) -> None:
    with _doc_with_image()[0] as doc:
        xref = doc.pages[0].images()[0].item.xref
        assert xref is not None
        with pytest.raises(TypeError, match="bytes"):
            doc.replace_image(xref, bad)  # ty: ignore[invalid-argument-type]
        with pytest.raises(TypeError, match="bytes"):
            _images.add_image(doc, bad)  # ty: ignore[invalid-argument-type]


def test_replace_image_and_add_image_accept_bytes_like_and_enforce_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    png = _png_bytes(8, 8)
    jpeg = _jpeg_bytes(16, 16)

    with _doc_with_image(limits=DocumentLimits.web())[0] as doc:
        xref = doc.pages[0].images()[0].item.xref
        assert xref is not None
        doc.replace_image(xref, bytearray(jpeg))  # ty: ignore[invalid-argument-type]
        added = _images.add_image(doc, memoryview(png))
        assert doc.objects[added]["Width"] == 8

    # image_input_size enforced before copying for bytes, bytearray, and cast memoryview.
    padded = png + b"\0" * (-len(png) % 4)
    wide_view = memoryview(padded).cast("I")
    with _doc_with_image()[0] as doc, monkeypatch.context() as mp:
        xref = doc.pages[0].images()[0].item.xref
        assert xref is not None
        mp.setattr("pyropdf._images._MAX_IMAGE_INPUT", len(png) - 1)
        for source in (png, bytearray(png), wide_view):
            with pytest.raises(LimitError) as caught:
                doc.replace_image(xref, source)  # ty: ignore[invalid-argument-type]
            assert caught.value.code == "image_input_size"
            with pytest.raises(LimitError) as caught:
                _images.add_image(doc, source)
            assert caught.value.code == "image_input_size"

    # memory_size enforced before copying (large JPEG exceeds budget while doc setup fits easily).
    big = _noise_jpeg_bytes(128, 128)
    assert len(big) > 8192
    with _doc_with_image(limits=DocumentLimits(max_memory_size=len(big) - 1))[0] as doc:
        xref = doc.pages[0].images()[0].item.xref
        assert xref is not None
        for source in (big, bytearray(big), memoryview(big)):
            with pytest.raises(LimitError) as caught:
                doc.replace_image(xref, source)  # ty: ignore[invalid-argument-type]
            assert caught.value.code == "memory_size"
            with pytest.raises(LimitError) as caught:
                _images.add_image(doc, source)
            assert caught.value.code == "memory_size"

    # cancellation enforced immediately.
    token = CancellationToken()
    with _doc_with_image(limits=DocumentLimits(cancellation=token))[0] as doc:
        xref = doc.pages[0].images()[0].item.xref
        assert xref is not None
        token.cancel()
        with pytest.raises(LimitError) as caught:
            doc.replace_image(xref, png)
        assert caught.value.code == "cancelled"
        with pytest.raises(LimitError) as caught:
            _images.add_image(doc, png)
        assert caught.value.code == "cancelled"


def test_canvas_image_enforces_types_size_memory_duration_and_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    png = _png_bytes(16, 16)
    img_path = tmp_path / "sample.png"
    img_path.write_bytes(png)

    # Non-buffer types raise TypeError; nonexistent int xref raises ValueError without allocating.
    with pyropdf.Document(limits=DocumentLimits.web()) as doc:
        page = doc.pages.new((200, 200))
        with page.draw() as canvas:
            for bad in (True, 4.2, object()):
                with pytest.raises(TypeError):
                    canvas.image((0, 0, 50, 50), bad)  # ty: ignore[invalid-argument-type]
            with pytest.raises(ValueError, match="not an image XObject"):
                canvas.image((0, 0, 50, 50), 1024)
            canvas.image((0, 0, 50, 50), img_path)
            canvas.image((60, 0, 110, 50), bytearray(png))
            canvas.image((120, 0, 170, 50), memoryview(png))
        assert len(page.images()) == 3

    # image_input_size enforced for bytes, bytearray, memoryview, Path, and str without unbounded reads.
    with pyropdf.Document() as doc, monkeypatch.context() as mp:
        page = doc.pages.new((200, 200))
        mp.setattr("pyropdf._images._MAX_IMAGE_INPUT", len(png) - 1)
        padded = png + b"\0" * (-len(png) % 4)
        for source in (png, bytearray(png), memoryview(padded).cast("I"), img_path, str(img_path)):
            with pytest.raises(LimitError) as caught, page.draw() as canvas:
                canvas.image((0, 0, 50, 50), source)
            assert caught.value.code == "image_input_size"

    # memory_size enforced on buffer and file inputs (setup fits comfortably; image input exceeds limit).
    large = _noise_jpeg_bytes(128, 128)
    large_path = tmp_path / "large.jpg"
    large_path.write_bytes(large)
    with pyropdf.Document(limits=DocumentLimits(max_memory_size=len(large) - 1)) as doc:
        page = doc.pages.new((200, 200))
        for source in (large, bytearray(large), memoryview(large), large_path):
            with pytest.raises(LimitError) as caught, page.draw() as canvas:
                canvas.image((0, 0, 50, 50), source)
            assert caught.value.code == "memory_size"

    # Cancellation enforced inside an active canvas before drawing an image.
    token = CancellationToken()
    with pyropdf.Document(limits=DocumentLimits(cancellation=token)) as doc:
        page = doc.pages.new((200, 200))
        with page.draw() as canvas:
            token.cancel()
            with pytest.raises(LimitError) as caught:
                canvas.image((0, 0, 50, 50), png)
        assert caught.value.code == "cancelled"

    # Deadline enforced inside an active canvas before drawing an image.
    with pyropdf.Document(limits=DocumentLimits(max_duration=0.005)) as doc:
        page = doc.pages.new((200, 200))
        with page.draw() as canvas:
            time.sleep(0.02)
            with pytest.raises(LimitError) as caught:
                canvas.image((0, 0, 50, 50), img_path)
        assert caught.value.code == "deadline"


# ---------------------------------------------------------------------------
# 2. Image rewriting, image_samples, encode_jpeg_samples, encode_ccitt_g4, _mask_pixels
# ---------------------------------------------------------------------------


def test_image_codec_helpers_enforce_document_budget() -> None:
    cmyk_jpeg = _jpeg_bytes(16, 16, mode="CMYK")
    rgb_samples = bytes([120, 140, 160]) * (16 * 16)
    fax_samples = bytes([0xAA]) * (64 * 64 // 8)

    # Tight max_memory_size refuses all three helpers.
    with pyropdf.Document(limits=DocumentLimits(max_memory_size=32)) as doc:
        core = doc._core()  # noqa: SLF001
        with pytest.raises(LimitError) as caught:
            _core.image_samples(core, cmyk_jpeg, False)  # noqa: FBT003
        assert caught.value.code == "memory_size"
        with pytest.raises(LimitError) as caught:
            _core.encode_jpeg_samples(core, rgb_samples, 16, 16, 3, 75, False)  # noqa: FBT003
        assert caught.value.code == "memory_size"
        with pytest.raises(LimitError) as caught:
            _core.encode_ccitt_g4(core, fax_samples, 64, 64, 1 << 20)
        assert caught.value.code == "memory_size"

    # Cancellation refuses all three helpers.
    token = CancellationToken()
    token.cancel()
    with pyropdf.Document(limits=DocumentLimits(cancellation=token)) as doc:
        core = doc._core()  # noqa: SLF001
        for call in (
            lambda: _core.image_samples(core, cmyk_jpeg, False),  # noqa: FBT003
            lambda: _core.encode_jpeg_samples(core, rgb_samples, 16, 16, 3, 75, False),  # noqa: FBT003
            lambda: _core.encode_ccitt_g4(core, fax_samples, 64, 64, 1 << 20),
        ):
            with pytest.raises(LimitError) as caught:
                call()
            assert caught.value.code == "cancelled"

    # Deadline refuses all three helpers inside an expired operation.
    with pyropdf.Document(limits=DocumentLimits(max_duration=0.005)) as doc:
        core = doc._core()  # noqa: SLF001
        for call in (
            lambda: _core.image_samples(core, cmyk_jpeg, False),  # noqa: FBT003
            lambda: _core.encode_jpeg_samples(core, rgb_samples, 16, 16, 3, 75, False),  # noqa: FBT003
            lambda: _core.encode_ccitt_g4(core, fax_samples, 64, 64, 1 << 20),
        ):

            def run(_budget: _core._OperationBudget, fn: object = call) -> None:
                time.sleep(0.015)
                assert callable(fn)
                fn()

            with pytest.raises(LimitError) as caught:
                _core.read_operation(core, run)
            assert caught.value.code == "deadline"


def test_rewrite_images_and_mask_pixels_enforce_document_limits() -> None:
    cmyk_pdf = _image_pdf(
        _jpeg_bytes(64, 64, mode="CMYK"),
        {
            "Width": 64,
            "Height": 64,
            "BitsPerComponent": 8,
            "ColorSpace": Name("DeviceCMYK"),
            "Filter": Name("DCTDecode"),
        },
        placement=(36.0, 36.0),
    )
    bitonal_raw = bytes([0xFF, 0x00] * 32) * 64  # 512 bytes for 64x64 1-bit
    bitonal_pdf = _image_pdf(
        zlib.compress(bitonal_raw),
        {
            "Width": 64,
            "Height": 64,
            "BitsPerComponent": 1,
            "ColorSpace": Name("DeviceGray"),
            "Filter": Name("FlateDecode"),
        },
        placement=(36.0, 36.0),
    )
    masked_pdf = _masked_image_pdf()

    # DocumentLimits.web() works across CMYK JPEG, bitonal fax, and mismatched-size mask rewriting.
    with pyropdf.open(cmyk_pdf, limits=DocumentLimits.web()) as doc:
        report = doc.rewrite_images(dpi_threshold=96, dpi_target=48, quality=65)
        assert report.considered == 1
    with pyropdf.open(bitonal_pdf, limits=DocumentLimits.web()) as doc:
        report = doc.rewrite_images(dpi_threshold=96, dpi_target=48, bitonal_format="fax")
        assert report.considered == 1
    with pyropdf.open(masked_pdf, limits=DocumentLimits.web()) as doc:
        report = doc.rewrite_images(dpi_threshold=96, dpi_target=48)
        assert report.considered == 1

    # Cancellation stops rewrite_images (even on no-op options and on CMYK/bitonal/masked docs).
    token = CancellationToken()
    with pyropdf.open(cmyk_pdf, limits=DocumentLimits(cancellation=token)) as doc:
        token.cancel()
        with pytest.raises(LimitError) as caught:
            doc.rewrite_images()
        assert caught.value.code == "cancelled"
        with pytest.raises(LimitError) as caught:
            doc.rewrite_images(dpi_threshold=96, dpi_target=48, quality=65)
        assert caught.value.code == "cancelled"

    # Tight max_memory_size stops rewrite_images after open succeeds.
    with pyropdf.open(cmyk_pdf, limits=DocumentLimits(max_memory_size=8 * 1024)) as doc:
        with pytest.raises(LimitError) as caught:
            doc.rewrite_images(dpi_threshold=96, dpi_target=48, quality=65)
        assert caught.value.code == "memory_size"

    # _mask_pixels forwards options.document.limits to its scratch Document.
    with pyropdf.open(masked_pdf) as doc:
        image_xref = doc.pages[0].images()[0].item.xref
        assert image_xref is not None
        mask = doc.objects[image_xref]["SMask"]
        cancelled = CancellationToken()
        cancelled.cancel()
        with pytest.raises(LimitError) as caught:
            _images._mask_pixels(mask, limits=DocumentLimits(cancellation=cancelled), stencil=False)  # noqa: SLF001
        assert caught.value.code == "cancelled"
        with pytest.raises(LimitError) as caught:
            _images._mask_pixels(  # noqa: SLF001
                mask, limits=DocumentLimits(max_total_decompressed_size=64), stencil=False
            )
        assert caught.value.code == "total_decompressed_size"


# ---------------------------------------------------------------------------
# 3. Operations.parse backward compatibility, Form/appearance parsing, clean_page
# ---------------------------------------------------------------------------


def test_operations_parse_backward_compatibility_and_form_appearance_limits() -> None:
    # Standalone Operations.parse(data) remains backward-compatible.
    ops = Operations.parse(b"q 1 0 0 1 0 0 cm Q")
    assert [op.operator for op in ops] == ["q", "cm", "Q"]
    with pytest.raises(LimitError) as caught:
        Operations.parse(b"q Q q Q", max_operations=2)
    assert caught.value.code == "content_operation_count"
    with pytest.raises(LimitError) as caught:
        Operations.parse(b"q Q", max_size=2)
    assert caught.value.code == "content_input_size"

    pdf = _form_and_appearance_pdf()
    with pyropdf.open(pdf, limits=DocumentLimits.web()) as doc:
        form = doc.objects[4]
        appearance = doc.objects[8]
        assert _xobjects._parse_form(form) is not None  # noqa: SLF001
        assert len(_xobjects._parse_stream_operations(appearance)) > 0  # noqa: SLF001

    # Form/appearance parsing obeys document max_memory_size (open succeeds at 4 KiB; parsing fails).
    with pyropdf.open(pdf, limits=DocumentLimits(max_memory_size=4096)) as doc:
        with pytest.raises(LimitError) as caught:
            _xobjects._parse_form(doc.objects[4])  # noqa: SLF001
        assert caught.value.code == "memory_size"
        with pytest.raises(LimitError) as caught:
            _xobjects._parse_stream_operations(doc.objects[8])  # noqa: SLF001
        assert caught.value.code == "memory_size"

    token = CancellationToken()
    with pyropdf.open(pdf, limits=DocumentLimits(cancellation=token)) as doc:
        form = doc.objects[4]
        appearance = doc.objects[8]
        token.cancel()
        with pytest.raises(LimitError) as caught:
            _xobjects._parse_form(form)  # noqa: SLF001
        assert caught.value.code == "cancelled"
        with pytest.raises(LimitError) as caught:
            _xobjects._parse_stream_operations(appearance)  # noqa: SLF001
        assert caught.value.code == "cancelled"

    with pyropdf.open(pdf, limits=DocumentLimits(max_duration=0.005)) as doc:
        form = doc.objects[4]

        def parse_after_deadline(_budget: _core._OperationBudget) -> None:
            time.sleep(0.015)
            _xobjects._parse_form(form)  # noqa: SLF001

        with pytest.raises(LimitError) as caught:
            _core.read_operation(doc._core(), parse_after_deadline)  # noqa: SLF001
        assert caught.value.code == "deadline"


def test_clean_page_enforces_limits_and_appearance_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = _form_and_appearance_pdf()

    with pyropdf.open(pdf, limits=DocumentLimits.web()) as doc:
        doc.pages[0].clean_contents(annotations=True)

    # Tight max_memory_size refuses clean_contents without mutating the document.
    with pyropdf.open(pdf, limits=DocumentLimits(max_memory_size=4096)) as doc:
        with pytest.raises(LimitError) as caught:
            doc.pages[0].clean_contents(annotations=True)
        assert caught.value.code == "memory_size"

    # Cancellation refuses clean_contents with and without annotations.
    token = CancellationToken()
    with pyropdf.open(pdf, limits=DocumentLimits(cancellation=token)) as doc:
        page = doc.pages[0]
        token.cancel()
        for include_annotations in (True, False):
            with pytest.raises(LimitError) as caught:
                page.clean_contents(annotations=include_annotations)
            assert caught.value.code == "cancelled"

    # Deadline refuses clean_contents inside an expired operation.
    with pyropdf.open(pdf, limits=DocumentLimits(max_duration=0.005)) as doc:
        page = doc.pages[0]

        def clean_after_deadline(_budget: _core._OperationBudget) -> None:
            time.sleep(0.015)
            page.clean_contents(annotations=True)

        with pytest.raises(LimitError) as caught:
            _core.read_operation(doc._core(), clean_after_deadline)  # noqa: SLF001
        assert caught.value.code == "deadline"

    # Appearance collection is bounded by _MAX_APPEARANCES.
    with pyropdf.open(pdf, limits=DocumentLimits.web()) as doc:
        monkeypatch.setattr("pyropdf._clean._MAX_APPEARANCES", 1)
        with pytest.raises(LimitError) as caught:
            doc.pages[0].clean_contents(annotations=True)
        assert caught.value.code == "annotation_count"


# ---------------------------------------------------------------------------
# 4. v0.4.0 public surfaces under DocumentLimits.web(), memory_size, cancellation
# ---------------------------------------------------------------------------


def test_structure_reading_order_enforces_web_limits_memory_and_cancellation() -> None:
    pdf = _tagged_pdf(paragraphs=2)

    with pyropdf.open(pdf, limits=DocumentLimits.web()) as doc:
        parts = doc.structure.reading_order()
        assert [part.text for part in parts] == ["Paragraph 0", "Paragraph 1"]

    token = CancellationToken()
    with pyropdf.open(pdf, limits=DocumentLimits(cancellation=token)) as doc:
        token.cancel()
        with pytest.raises(LimitError) as caught:
            doc.structure.reading_order()
        assert caught.value.code == "cancelled"

    larger = _tagged_pdf(paragraphs=30)
    with pyropdf.open(larger, limits=DocumentLimits(max_memory_size=8192)) as doc:
        with pytest.raises(LimitError) as caught:
            doc.structure.reading_order()
        assert caught.value.code == "memory_size"


def test_collections_and_named_pages_enforce_web_limits_memory_and_cancellation() -> None:
    with pyropdf.Document() as builder:
        builder.pages.new((200, 200))
        builder.pages.new((200, 200))
        builder.collection = Collection(
            view=CollectionView.DETAILS,
            schema={f"Field{i}": CollectionField(CollectionFieldKind.STRING, f"Label {i}") for i in range(40)},
        )
        builder.named_pages = {f"Page{i}": i % 2 for i in range(20)}
        raw = builder.to_bytes()

    with pyropdf.open(raw, limits=DocumentLimits.web()) as doc:
        assert doc.collection is not None
        assert doc.collection.view is CollectionView.DETAILS
        assert len(doc.collection.schema) == 40
        assert len(doc.named_pages) == 20

    token = CancellationToken()
    with pyropdf.open(raw, limits=DocumentLimits(cancellation=token)) as doc:
        token.cancel()
        with pytest.raises(LimitError) as caught:
            _ = doc.collection
        assert caught.value.code == "cancelled"
        with pytest.raises(LimitError) as caught:
            _ = list(doc.named_pages)
        assert caught.value.code == "cancelled"

    with pyropdf.open(raw, limits=DocumentLimits(max_memory_size=4096)) as doc:
        with pytest.raises(LimitError) as caught:
            _ = doc.collection
        assert caught.value.code == "memory_size"


def test_threads_enforce_web_limits_memory_and_cancellation() -> None:
    with pyropdf.Document() as builder:
        builder.pages.new((300, 400))
        builder.pages.new((300, 400))
        beads = [BeadSpecification(0, (10, 10, 100, 100)), BeadSpecification(1, (10, 10, 100, 100))]
        builder.threads.add(beads, title="Article " + ("x" * 10_000))
        raw = builder.to_bytes()

    with pyropdf.open(raw, limits=DocumentLimits.web()) as doc:
        assert len(doc.threads) == 1
        assert doc.threads[0].title is not None
        assert doc.threads[0].title.startswith("Article ")
        assert len(doc.threads[0].beads) == 2

    token = CancellationToken()
    with pyropdf.open(raw, limits=DocumentLimits(cancellation=token)) as doc:
        token.cancel()
        with pytest.raises(LimitError) as caught:
            _ = len(doc.threads)
        assert caught.value.code == "cancelled"

    with pyropdf.open(raw, limits=DocumentLimits(max_memory_size=4096)) as doc:
        with pytest.raises(LimitError) as caught:
            _ = doc.threads[0].title
        assert caught.value.code == "memory_size"


def test_interchange_enforces_web_limits_memory_and_cancellation() -> None:
    with pyropdf.Document() as builder:
        builder.pages.new((300, 400))
        builder.extensions.add(DeveloperExtension("ADBE", "1.7", 3))
        builder.associated_files.add(
            "notes.txt",
            b"associated payload",
            relationship=AssociatedFileRelationship.SUPPLEMENT,
            media_type="text/plain",
        )
        builder.piece_info["MyApp"] = PieceData("D:20260316000000Z", {f"Key{i}": f"Value{i}" for i in range(40)})
        builder.parts.add_part(0, dpm={"Part": "Main"})
        raw = builder.to_bytes()

    with pyropdf.open(raw, limits=DocumentLimits.web()) as doc:
        assert "ADBE" in doc.extensions
        assert len(doc.associated_files) == 1
        assert "MyApp" in doc.piece_info
        assert doc.parts.root is not None

    token = CancellationToken()
    with pyropdf.open(raw, limits=DocumentLimits(cancellation=token)) as doc:
        token.cancel()
        for accessor in (
            doc.extensions.all,
            lambda: len(doc.associated_files),
            lambda: dict(doc.piece_info),
            lambda: doc.parts.root,
        ):
            with pytest.raises(LimitError) as caught:
                accessor()
            assert caught.value.code == "cancelled"

    with pyropdf.open(raw, limits=DocumentLimits(max_memory_size=4096)) as doc:
        with pytest.raises(LimitError) as caught:
            _ = doc.piece_info["MyApp"]
        assert caught.value.code == "memory_size"


def test_sanitize_enforces_web_limits_memory_and_cancellation(tmp_path: Path) -> None:
    hidden_form = "BT /F1 12 Tf 3 Tr 10 20 Td (secret form) Tj ET\n" + ("q 1 0 0 1 0 0 cm Q\n" * 400)
    pdf = _build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 200] >>",
            3: (
                "<< /Type /Page /Parent 2 0 R "
                "/Resources << /Font << /F1 4 0 R >> /XObject << /Fm0 5 0 R >> >> "
                "/Contents 6 0 R /Annots [7 0 R] >>"
            ),
            4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            5: _raw_stream(
                hidden_form,
                "/Type /XObject /Subtype /Form /BBox [0 0 100 100] /Resources << /Font << /F1 4 0 R >> >> ",
            ),
            6: _raw_stream("q /Fm0 Do Q"),
            7: "<< /Type /Annot /Subtype /Square /Rect [10 10 50 50] /AP << /N 8 0 R >> >>",
            8: _raw_stream(
                hidden_form,
                "/Type /XObject /Subtype /Form /BBox [0 0 40 40] /Resources << /Font << /F1 4 0 R >> >> ",
            ),
        }
    )

    out = tmp_path / "sanitized.pdf"
    with pyropdf.open(pdf, limits=DocumentLimits.web()) as doc:
        report = doc.sanitize(out, hidden_text=True, comments=False)
        assert report.removed["hidden_text"] == 2
    assert out.exists()

    token = CancellationToken()
    with pyropdf.open(pdf, limits=DocumentLimits(cancellation=token)) as doc:
        token.cancel()
        with pytest.raises(LimitError) as caught:
            doc.sanitize(tmp_path / "cancelled.pdf", hidden_text=True)
        assert caught.value.code == "cancelled"

    with pyropdf.open(pdf, limits=DocumentLimits(max_memory_size=4096)) as doc:
        with pytest.raises(LimitError) as caught:
            doc.sanitize(tmp_path / "oom.pdf", hidden_text=True, comments=False)
        assert caught.value.code == "memory_size"


def test_replace_text_enforces_web_limits_memory_and_cancellation() -> None:
    form_content = "BT /F1 12 Tf 10 50 Td (Hello Old World) Tj ET\n" + ("q 1 0 0 1 0 0 cm Q\n" * 400)
    pdf = _build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 200 200] >>",
            3: (
                "<< /Type /Page /Parent 2 0 R "
                "/Resources << /Font << /F1 4 0 R >> /XObject << /Fm0 5 0 R >> >> "
                "/Contents 6 0 R >>"
            ),
            4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            5: _raw_stream(
                form_content,
                "/Type /XObject /Subtype /Form /BBox [0 0 200 200] /Resources << /Font << /F1 4 0 R >> >> ",
            ),
            6: _raw_stream("q /Fm0 Do Q"),
        }
    )

    with pyropdf.open(pdf, limits=DocumentLimits.web()) as doc:
        replaced = doc.pages[0].replace_text("Old", "New")
        assert replaced == 1
        assert "Hello New World" in doc.pages[0].text()

    token = CancellationToken()
    with pyropdf.open(pdf, limits=DocumentLimits(cancellation=token)) as doc:
        page = doc.pages[0]
        token.cancel()
        with pytest.raises(LimitError) as caught:
            page.replace_text("Old", "New")
        assert caught.value.code == "cancelled"

    with pyropdf.open(pdf, limits=DocumentLimits(max_memory_size=4096)) as doc:
        with pytest.raises(LimitError) as caught:
            doc.pages[0].replace_text("Old", "New")
        assert caught.value.code == "memory_size"


def test_document_chunks_and_json_enforce_web_limits_memory_and_cancellation() -> None:
    pdf = _chunks_pdf()

    with pyropdf.open(pdf, limits=DocumentLimits.web()) as doc:
        chunks = doc.to_chunks()
        assert len(chunks) >= 1
        assert "Overview" in chunks[0].text
        assert "Overview" in doc.to_json()

        with pytest.raises(LimitError) as caught:
            doc.to_chunks(max_size=5)
        assert caught.value.code == "export_output_size"

    token = CancellationToken()
    with pyropdf.open(pdf, limits=DocumentLimits(cancellation=token)) as doc:
        token.cancel()
        with pytest.raises(LimitError) as caught:
            doc.to_chunks()
        assert caught.value.code == "cancelled"
        with pytest.raises(LimitError) as caught:
            doc.to_json()
        assert caught.value.code == "cancelled"

    larger_chunks = _chunks_pdf(lines=25)
    with pyropdf.open(larger_chunks, limits=DocumentLimits(max_memory_size=2048)) as doc:
        with pytest.raises(LimitError) as caught:
            doc.to_chunks()
        assert caught.value.code == "memory_size"
