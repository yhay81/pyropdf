"""Regression tests for issue #1125 (PyO3 and Python-layer audit fixes)."""

from __future__ import annotations

import contextlib
import io
import struct
import threading
import xml.etree.ElementTree as ET
import zlib
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from test_signing import DER, _pem_signer, make_pki

import pyropdf
from pyropdf import _annotations, _core, _images, _metadata, _text_edit, _xobjects
from pyropdf._canvas._resources import _isolated
from pyropdf._signature_structures import _record_dss_stream

if TYPE_CHECKING:
    from collections.abc import Iterator

FONTS = Path(__file__).resolve().parent.parent / "crates" / "pyropdf-engine" / "src" / "fonts" / "testdata"
JAPANESE_FONT = FONTS / "NotoSansJP-subset.otf"
TEST_CREDENTIAL = "test-credential"


def _png_bytes(width: int, height: int, rgba: tuple[int, int, int, int]) -> bytes:
    """Return a minimal valid RGBA PNG of solid ``rgba``."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    row = b"\x00" + bytes(rgba) * width
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b"")
    )


def _history_writer(doc: pyropdf.Document, worker: int) -> None:
    for index in range(30):
        with doc.transaction(f"w{worker}-{index}"):
            doc.catalog[f"W{worker}"] = index


def _history_clearer(doc: pyropdf.Document, expected_limit: int) -> None:
    for _ in range(30):
        doc.history.clear()
        assert doc.history.max_size == expected_limit


def _history_stepper(doc: pyropdf.Document, expected_limit: int) -> None:
    for _ in range(30):
        with contextlib.suppress(pyropdf.PdfError):
            doc.history.undo()
        with contextlib.suppress(pyropdf.PdfError):
            doc.history.redo()
        assert doc.history.max_size == expected_limit


def _run_concurrent_history_stress(doc: pyropdf.Document, expected_limit: int) -> list[BaseException]:
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def run_safe(fn: object) -> None:
        try:
            barrier.wait()
            assert callable(fn)
            fn()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    tasks = (
        lambda: _history_writer(doc, 0),
        lambda: _history_writer(doc, 1),
        lambda: _history_clearer(doc, expected_limit),
        lambda: _history_stepper(doc, expected_limit),
    )
    threads = [threading.Thread(target=run_safe, args=(task,)) for task in tasks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


def test_history_clear_and_concurrent_writes_preserve_limit_and_steps() -> None:
    doc = pyropdf.Document()
    doc.pages.new((100, 100))
    expected_limit = 4 * 1024 * 1024
    doc.history.max_size = expected_limit

    with doc.transaction("check-in-tx"):
        with pytest.raises(pyropdf.PdfError, match="cannot clear the history inside a transaction"):
            doc.history.clear()
        # Setting the limit inside a transaction must still work without deadlocking.
        doc.history.max_size = expected_limit

    assert _run_concurrent_history_stress(doc, expected_limit) == []
    assert doc.history.max_size == expected_limit
    steps = doc.history.steps
    position = doc.history.position
    assert 0 <= position <= len(steps)
    for _ in range(position):
        doc.history.undo()
    with pytest.raises(pyropdf.PdfError, match="nothing to undo"):
        doc.history.undo()
    for _ in range(len(steps)):
        doc.history.redo()
    with pytest.raises(pyropdf.PdfError, match="nothing to redo"):
        doc.history.redo()


def test_open_file_handles_u64_max_file_size_and_enforces_poll_and_memory(tmp_path: Path) -> None:
    source = pyropdf.Document()
    source.pages.new((100, 100))
    path = tmp_path / "sample.pdf"
    source.save(path)

    with pyropdf.open(path, limits=pyropdf.DocumentLimits(max_file_size=2**64 - 1)) as doc:
        assert len(doc.pages) == 1

    cancelled = pyropdf.CancellationToken()
    cancelled.cancel()
    with pytest.raises(pyropdf.LimitError) as caught:
        pyropdf.open(path, limits=pyropdf.DocumentLimits(max_file_size=2**64 - 1, cancellation=cancelled))
    assert caught.value.code == "cancelled"

    with pytest.raises(pyropdf.LimitError) as caught_mem:
        pyropdf.open(path, limits=pyropdf.DocumentLimits(max_memory_size=32))
    assert caught_mem.value.code == "memory_size"


def test_embedded_font_layout_propagates_limit_error() -> None:
    font = pyropdf.Font.from_file(JAPANESE_FONT)
    # Use a text with many words so layout calls shaped() repeatedly inside word-wrapping.
    words = " ".join(["日本語"] * 60)
    caught_code: str | None = None
    for limit in (4_000, 8_000, 16_000, 32_000, 64_000, 128_000):
        doc = pyropdf.Document(limits=pyropdf.DocumentLimits(max_memory_size=limit))
        page = doc.pages.new((200, 400))
        try:
            with page.draw() as canvas:
                canvas.textbox((10, 10, 80, 380), words, font=font, size=12)
        except pyropdf.LimitError as exc:
            caught_code = exc.code
            break
    assert caught_code == "memory_size"


def test_font_char_bbox_rejects_surrogate_and_core_repr_is_clean() -> None:
    std_core = _core._Font.standard("Helvetica")  # noqa: SLF001
    assert repr(std_core) == '_Font("Helvetica")'

    emb_core = _core._Font.from_bytes(JAPANESE_FONT.read_bytes(), 0)  # noqa: SLF001
    assert repr(emb_core) == f'_Font("{emb_core.name}")'
    assert "Ok(" not in repr(emb_core)

    with pytest.raises(pyropdf.PdfError, match=r"U\+D800"):
        emb_core.char_bbox(0xD800)


def test_unlock_refused_inside_transaction_and_runs_check_at_open() -> None:
    source = pyropdf.Document()
    source.pages.new((100, 100))
    deep: list[object] = [1]
    for _ in range(10):
        deep = [deep]
    source.catalog["Deep"] = deep
    encrypted = source.to_bytes(encryption=pyropdf.Encryption(user_password=TEST_CREDENTIAL))

    with pyropdf.open(encrypted) as doc:
        with doc.transaction("outer"), pytest.raises(pyropdf.PdfError, match="cannot unlock inside a transaction"):
            doc.unlock(TEST_CREDENTIAL)
        assert doc.needs_password

    strict = pyropdf.DocumentLimits(check_at_open=True, max_object_depth=4)
    with pyropdf.open(encrypted, limits=strict) as doc:
        assert doc.needs_password
        with pytest.raises(pyropdf.LimitError) as caught:
            doc.unlock(TEST_CREDENTIAL)
        assert caught.value.code == "object_depth"
        assert doc.needs_password


def test_annotation_data_ignores_lying_bytearray_len() -> None:
    class LyingByteArray(bytearray):
        def __len__(self) -> int:
            return 1

    source = pyropdf.Document()
    source.pages.new((200, 200))
    doc = pyropdf.open(source.to_bytes(), limits=pyropdf.DocumentLimits(max_memory_size=64 * 1024))
    page = doc.pages[0]
    location = (doc._core(), page._number, page._generation)  # noqa: SLF001

    oversized = LyingByteArray(b"A" * 200)
    with pytest.raises(pyropdf.LimitError) as caught:
        _core.annotation_add(
            *location,
            {"subtype": "FileAttachment", "rect": (10, 10, 20, 20), "data": oversized, "filename": "f", "max_size": 10},
        )
    assert caught.value.code == "embedded_file_size"

    huge_mem = LyingByteArray(b"B" * (128 * 1024))
    with pytest.raises(pyropdf.LimitError) as caught_mem:
        _core.annotation_add(
            *location,
            {"subtype": "FileAttachment", "rect": (10, 10, 20, 20), "data": huge_mem, "filename": "f"},
        )
    assert caught_mem.value.code == "memory_size"

    with pytest.raises(TypeError, match="bytes or bytearray"):
        _core.annotation_add(
            *location,
            {
                "subtype": "FileAttachment",
                "rect": (10, 10, 20, 20),
                "data": memoryview(b"abc"),
                "filename": "f",
            },
        )


def test_subset_fonts_terminates_on_cyclic_ap_dictionary() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    ap_xref = doc.objects.add({})
    ap = doc.objects[ap_xref]
    ap["N"] = ap
    annot_xref = doc.objects.add(
        {"Type": pyropdf.Name("Annot"), "Subtype": pyropdf.Name("Text"), "Rect": [0, 0, 20, 20], "AP": ap}
    )
    page.obj["Annots"] = [doc.objects[annot_xref]]

    assert doc.subset_fonts() == ()


def test_sanitize_handles_cyclic_action_array_and_preserves_non_action_a(tmp_path: Path) -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))

    arr_xref = doc.objects.add([])
    arr = doc.objects[arr_xref]
    arr.append(arr)
    action_xref = doc.objects.add({"S": pyropdf.Name("Named"), "N": pyropdf.Name("NextPage"), "Next": arr})
    doc.catalog["OpenAction"] = doc.objects[action_xref]

    root_xref = doc.objects.add({"Type": pyropdf.Name("StructTreeRoot")})
    explicit_elem = doc.objects[
        doc.objects.add(
            {
                "Type": pyropdf.Name("StructElem"),
                "S": pyropdf.Name("P"),
                "P": doc.objects[root_xref],
                "A": {"O": pyropdf.Name("Layout"), "Placement": pyropdf.Name("Block")},
            }
        )
    ]
    implicit_elem = doc.objects[
        doc.objects.add(
            {
                "S": pyropdf.Name("Span"),
                "P": explicit_elem,
                "A": {"O": pyropdf.Name("Layout")},
            }
        )
    ]
    movie_annot = doc.objects[
        doc.objects.add(
            {
                "Type": pyropdf.Name("Annot"),
                "Subtype": pyropdf.Name("Movie"),
                "Rect": [0, 0, 20, 20],
                "A": {"ShowControls": True},
            }
        )
    ]
    link_annot = doc.objects[
        doc.objects.add(
            {
                "Type": pyropdf.Name("Annot"),
                "Subtype": pyropdf.Name("Link"),
                "Rect": [30, 0, 50, 20],
                "A": {"S": pyropdf.Name("URI"), "URI": "https://example.com"},
            }
        )
    ]
    page.obj["Annots"] = [movie_annot, link_annot]

    doc.sanitize(tmp_path / "js.pdf", javascript=True, actions=True)
    assert "OpenAction" not in doc.catalog
    assert "A" not in link_annot
    assert "A" in explicit_elem
    assert "A" in implicit_elem
    assert "A" in movie_annot

    big = pyropdf.Document()
    big.pages.new((100, 100))
    big.catalog["OpenAction"] = {"S": pyropdf.Name("Named"), "N": pyropdf.Name("NextPage"), "Next": [0] * 600}
    with pytest.raises(pyropdf.LimitError) as caught:
        big.sanitize(tmp_path / "big.pdf", javascript=True)
    assert caught.value.code == "sanitize_action_chain"


def test_canvas_image_cache_invalidates_replaced_flate_image() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    red = _png_bytes(4, 4, (255, 0, 0, 255))
    blue = _png_bytes(4, 4, (0, 0, 255, 255))

    with page.draw() as canvas:
        canvas.image((10, 10, 50, 50), red)
    xobjects_1 = page.obj["Resources"]["XObject"]
    first_xref = next(iter(xobjects_1.values())).xref
    assert first_xref is not None
    original_raw = doc.objects[first_xref].read(decode=False)

    blue_stream_xref = _images.add_image(doc, blue)
    doc.objects[first_xref].write(doc.objects[blue_stream_xref].read(decode=False), filter=None)
    assert doc.objects[first_xref].read(decode=False) != original_raw

    with page.draw() as canvas:
        canvas.image((60, 10, 100, 50), red)
    xobjects_2 = page.obj["Resources"]["XObject"]
    drawn_xrefs = [value.xref for value in xobjects_2.values()]
    assert len(set(drawn_xrefs)) == 2
    fresh_xref = next(xref for xref in drawn_xrefs if xref != first_xref)
    assert fresh_xref is not None
    assert doc.objects[fresh_xref].read(decode=False) == original_raw


def test_subject_dn_uses_logical_or_across_dictionaries() -> None:
    pki = make_pki()
    signer = _pem_signer(pki, "p256")

    src = pyropdf.Document()
    page = src.pages.new((200, 200))
    sig_field = page.widgets.add(pyropdf.SignatureField("Sig1", (10, 10, 100, 40)))
    sig_field.obj["SV"] = {
        "Type": pyropdf.Name("SV"),
        "Cert": {
            "Type": pyropdf.Name("SVCert"),
            "Ff": 8,
            "SubjectDN": [
                {"CN": "NonMatchingSigner"},
                {"CN": "pyropdf test signer p256"},
            ],
        },
    }
    doc = pyropdf.open(src.to_bytes())
    signed_bytes = doc.sign("Sig1", signer)
    assert len(pyropdf.open(signed_bytes).signatures) == 1

    src2 = pyropdf.Document()
    page2 = src2.pages.new((200, 200))
    sig_field2 = page2.widgets.add(pyropdf.SignatureField("Sig2", (10, 10, 100, 40)))
    sig_field2.obj["SV"] = {
        "Type": pyropdf.Name("SV"),
        "Cert": {
            "Type": pyropdf.Name("SVCert"),
            "Ff": 8,
            "SubjectDN": [
                {"CN": "pyropdf test signer p256", "O": "WrongOrg"},
                {"CN": "Other"},
            ],
        },
    }
    doc2 = pyropdf.open(src2.to_bytes())
    with pytest.raises(ValueError, match="SubjectDN"):
        doc2.sign("Sig2", signer)


def test_signing_byterange_validation_and_encrypted_post_signature_revisions() -> None:
    pki = make_pki()
    signer = _pem_signer(pki, "p256")
    trust = [pki.root.public_bytes(DER)]

    base_src = pyropdf.Document()
    page = base_src.pages.new((200, 200))
    page.widgets.add(pyropdf.SignatureField("Sig", (10, 10, 100, 40)))
    base = pyropdf.open(base_src.to_bytes())
    signed = base.sign("Sig", signer)

    for bad_range, expected_len in [
        ([0, "bad", 10, 5], 0),
        ([0, True, 10, 5], 0),
        ([-10, 5, 20, 5], 4),
        ([0, 100, 50, 10], 4),
        ([0, 10, 20, 9_999_999], 4),
    ]:
        tampered = pyropdf.open(signed)
        sig_dict = tampered.form["Sig"].obj["V"]
        sig_dict["ByteRange"] = bad_range
        reopened = pyropdf.open(tampered.to_bytes(incremental=True))
        sigs = reopened.signatures
        assert len(sigs) == 1
        assert len(sigs[0].byte_range) == expected_len
        assert sigs[0].revision is None
        result = sigs[0].verify()
        assert not result.intact
        assert not result.covers_revision

    enc_src = pyropdf.Document()
    enc_src.pages.new((200, 200))
    enc_bytes = enc_src.to_bytes(encryption=pyropdf.Encryption(user_password=TEST_CREDENTIAL, integrity=True))
    with pyropdf.open(enc_bytes, password=TEST_CREDENTIAL) as enc_doc:
        signed_enc = enc_doc.sign("Sig1", signer)
    with pyropdf.open(signed_enc, password=TEST_CREDENTIAL) as after_sign:
        after_sign.pages[0].annotations.add(pyropdf.Note((20, 20), contents="post-sign"))
        final_enc = after_sign.to_bytes(incremental=True)

    with pyropdf.open(final_enc, password=TEST_CREDENTIAL) as verify_doc:
        sigs = verify_doc.signatures
        assert len(sigs) == 1
        report = sigs[0].verify(trust=trust)
        assert report.intact
        assert report.valid is True
        assert report.modifications == "other"


def test_limit_errors_preserved_in_forms_canvas_and_annotations() -> None:
    doc = pyropdf.Document(limits=pyropdf.DocumentLimits(max_decompressed_size=16))
    form_xref = doc.objects.add_stream(
        b"0 0 1 rg " + b" " * 100,
        {"Type": pyropdf.Name("XObject"), "Subtype": pyropdf.Name("Form"), "BBox": [0, 0, 10, 10]},
    )
    reopened = pyropdf.open(doc.to_bytes(), limits=pyropdf.DocumentLimits(max_decompressed_size=16))
    with pytest.raises(pyropdf.LimitError):
        _xobjects._parse_form(reopened.objects[form_xref])  # noqa: SLF001

    token = pyropdf.CancellationToken()
    doc_iso = pyropdf.Document(limits=pyropdf.DocumentLimits(cancellation=token))
    doc_iso.pages.new((100, 100))
    iso_reopened = pyropdf.open(doc_iso.to_bytes(), limits=pyropdf.DocumentLimits(cancellation=token))
    token.cancel()
    with pytest.raises(pyropdf.LimitError) as caught_iso:
        _isolated(iso_reopened.pages[0])
    assert caught_iso.value.code == "cancelled"

    doc_tf = pyropdf.Document()
    page_tf = doc_tf.pages.new((100, 100))

    class LimitMatrix:
        def __iter__(self) -> Iterator[float]:
            code = "cancelled"
            msg = "cancelled during matrix iteration"
            raise pyropdf.LimitError(code, msg)

    with page_tf.draw() as canvas, pytest.raises(pyropdf.LimitError) as caught_tf:
        canvas.transform(LimitMatrix())  # ty: ignore[invalid-argument-type]
    assert caught_tf.value.code == "cancelled"

    token_line = pyropdf.CancellationToken()
    doc_line = pyropdf.Document()
    page_line = doc_line.pages.new((200, 200))
    line_added = page_line.annotations.add(pyropdf.LineAnnotation((10, 10), (100, 10)))
    line_added.contents = "cap"
    line_doc = pyropdf.open(doc_line.to_bytes(), limits=pyropdf.DocumentLimits(cancellation=token_line))
    line_annot = line_doc.pages[0].annotations[0]
    token_line.cancel()
    with pytest.raises(pyropdf.LimitError) as caught_line:
        _ = line_annot.line_details
    assert caught_line.value.code == "cancelled"


def test_limit_errors_preserved_in_dss_recolor_and_attachments() -> None:
    token_dss = pyropdf.CancellationToken()
    doc_dss = pyropdf.Document()
    stream_xref = doc_dss.objects.add_stream(b"cert-bytes")
    dss_reopened = pyropdf.open(doc_dss.to_bytes(), limits=pyropdf.DocumentLimits(cancellation=token_dss))
    stream_obj = dss_reopened.objects[stream_xref]
    token_dss.cancel()
    with pytest.raises(pyropdf.LimitError) as caught_dss:
        _record_dss_stream(stream_obj, [], set(), {})
    assert caught_dss.value.code == "cancelled"

    doc_rec = pyropdf.Document()
    page_rec = doc_rec.pages.new((200, 200))
    with page_rec.draw() as canvas:
        canvas.image((10, 10, 90, 90), _png_bytes(8, 8, (255, 0, 0, 255)))
    token_rec = pyropdf.CancellationToken()
    rec_reopened = pyropdf.open(doc_rec.to_bytes(), limits=pyropdf.DocumentLimits(cancellation=token_rec))
    token_rec.cancel()
    with pytest.raises(pyropdf.LimitError) as caught_rec:
        rec_reopened.pages[0].recolor("gray")
    assert caught_rec.value.code == "cancelled"

    att_doc = pyropdf.Document()
    leaf = att_doc.objects[att_doc.objects.add({"Names": []})]
    node = leaf
    for _ in range(33):
        node = att_doc.objects[att_doc.objects.add({"Kids": [node]})]
    att_doc.catalog["Names"] = {"EmbeddedFiles": node}
    with pytest.raises(pyropdf.LimitError) as caught_depth:
        _ = list(att_doc.attachments)
    assert caught_depth.value.code == "object_depth"


def test_text_edit_probe_overrides_inherited_rotate() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    font_xref = doc.objects.add(
        {
            "Type": pyropdf.Name("Font"),
            "Subtype": pyropdf.Name("Type1"),
            "BaseFont": pyropdf.Name("Helvetica"),
            "Encoding": pyropdf.Name("WinAnsiEncoding"),
        }
    )
    page.obj["Resources"] = {"Font": {"F1": doc.objects[font_xref]}}
    page.operations = pyropdf.Operations(
        [
            pyropdf.Operation("BT"),
            pyropdf.Operation("Tf", (pyropdf.Name("F1"), 12)),
            pyropdf.Operation("Td", (20, 100)),
            pyropdf.Operation("Tj", (pyropdf.String(b"Hello world"),)),
            pyropdf.Operation("ET"),
        ]
    )
    parent = page.obj["Parent"]
    parent["Rotate"] = 90
    assert "Rotate" not in page.obj

    probed = _text_edit._Font(doc.objects[font_xref], True)  # noqa: SLF001, FBT003
    _text_edit._probe(page, [probed])  # noqa: SLF001
    assert probed.advances[ord("l")] == pytest.approx(222.0, abs=5.0)
    assert probed.advances[ord("H")] == pytest.approx(722.0, abs=5.0)

    replaced = page.replace_text("Hello", "Happy")
    assert replaced == 1
    assert "Happy" in page.text()
    assert parent["Rotate"] == 90


def test_annotation_set_restores_id_on_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    page.obj["Annots"] = [
        {
            "Type": pyropdf.Name("Annot"),
            "Subtype": pyropdf.Name("FreeText"),
            "Rect": [10, 10, 150, 40],
            "Contents": "Before",
            "DA": "/Helv 12 Tf 0 g",
        }
    ]
    annot = page.annotations[0]
    assert annot.xref == 0
    previous_id = annot._id  # noqa: SLF001

    def fail_draw(*_args: object, **_kwargs: object) -> None:
        msg = "injected draw failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(_annotations, "_draw_face", fail_draw)
    font = pyropdf.Font.from_file(JAPANESE_FONT)
    with pytest.raises(RuntimeError, match="injected draw failure"):
        annot._set(contents="After", draw_font=font)  # noqa: SLF001

    assert annot._id == previous_id  # noqa: SLF001
    assert annot.contents == "Before"


def test_rewrite_images_bytes_before_includes_mask_and_jpx_size_validates_dimensions() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((72, 72))
    width, height = 200, 200
    rgb_raw = bytes([120, 180, 60]) * (width * height)
    mask_w, mask_h = 240, 240
    mask_raw = bytes((x ^ y) & 0xFF for y in range(mask_h) for x in range(mask_w))
    mask_xref = doc.objects.add_stream(
        mask_raw,
        {
            "Type": pyropdf.Name("XObject"),
            "Subtype": pyropdf.Name("Image"),
            "Width": mask_w,
            "Height": mask_h,
            "BitsPerComponent": 8,
            "ColorSpace": pyropdf.Name("DeviceGray"),
        },
    )
    img_xref = doc.objects.add_stream(
        rgb_raw,
        {
            "Type": pyropdf.Name("XObject"),
            "Subtype": pyropdf.Name("Image"),
            "Width": width,
            "Height": height,
            "BitsPerComponent": 8,
            "ColorSpace": pyropdf.Name("DeviceRGB"),
            "SMask": doc.objects[mask_xref],
        },
    )
    img_encoded = len(doc.objects[img_xref].read(decode=False))
    mask_encoded = len(doc.objects[mask_xref].read(decode=False))
    page.obj["Resources"] = {"XObject": {"Im0": doc.objects[img_xref]}}
    page.operations = pyropdf.Operations(
        [
            pyropdf.Operation("q"),
            pyropdf.Operation("cm", (36, 0, 0, 36, 18, 18)),
            pyropdf.Operation("Do", (pyropdf.Name("Im0"),)),
            pyropdf.Operation("Q"),
        ]
    )

    report = doc.rewrite_images(dpi_threshold=150, dpi_target=72)
    assert report.rewritten == 1
    assert report.bytes_before == img_encoded + mask_encoded
    assert report.bytes_after < report.bytes_before

    ihdr = struct.pack(">I4sII", 16, b"ihdr", 0, 10)
    bad_jp2 = b"\x00\x00\x00\x0cjP  \r\n\x87\n" + struct.pack(">I4s", 8 + len(ihdr), b"jp2h") + ihdr
    with pytest.raises(ValueError, match="non-positive"):
        _images._jpx_size(bad_jp2)  # noqa: SLF001

    bad_j2k = b"\xff\x4f\xff\x51" + b"\x00" * 4 + struct.pack(">IIII", 10, 10, 10, 5)
    with pytest.raises(ValueError, match="non-positive"):
        _images._jpx_size(bad_j2k)  # noqa: SLF001

    bad_j2k_neg = b"\xff\x4f\xff\x51" + b"\x00" * 4 + struct.pack(">IIII", 5, 10, 20, 0)
    with pytest.raises(ValueError, match="non-positive"):
        _images._jpx_size(bad_j2k_neg)  # noqa: SLF001


def test_metadata_xmp_update_does_not_mutate_global_et_namespace_map() -> None:
    ns_map: dict[str, str] = getattr(ET, "_namespace_map")  # noqa: B009
    before = dict(ns_map)
    untrusted_uri = "https://untrusted.example/custom-ns-1125/"
    packet = (
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        f'<rdf:Description rdf:about="" xmlns:evil="{untrusted_uri}" evil:Tag="kept" />'
        "</rdf:RDF></x:xmpmeta>"
        '<?xpacket end="w"?>'
    )
    updated = _metadata._updated_xmp(packet, {"title": "Safe Title"})  # noqa: SLF001
    assert updated is not None
    assert dict(ns_map) == before
    assert untrusted_uri not in ns_map
    assert f'xmlns:evil="{untrusted_uri}"' in updated
    assert "Safe Title" in updated
    start = updated.index("<x:xmpmeta")
    end = updated.index("</x:xmpmeta>") + len("</x:xmpmeta>")
    parsed = ET.parse(io.StringIO(updated[start:end])).getroot()  # noqa: S314
    assert parsed.tag == "{adobe:ns:meta/}xmpmeta"


def test_appearance_states_accepts_inverted_rect_and_bbox() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    annot = page.annotations.add(pyropdf.Note((10, 10)))
    annot.obj["Rect"] = [100, 150, 20, 50]

    normal = annot.set_appearance("normal", b"0 0 1 rg 0 0 80 100 re f")
    assert [float(v) for v in normal["BBox"]] == [0.0, 0.0, 80.0, 100.0]

    rollover = annot.set_appearance("rollover", b"1 0 0 rg 10 20 40 20 re f", bbox=(50, 40, 10, 20))
    assert [float(v) for v in rollover["BBox"]] == [10.0, 20.0, 50.0, 40.0]

    with pytest.raises(ValueError, match="positive area"):
        annot.set_appearance("down", b"0 g", bbox=(10, 20, 10, 40))


def test_real_rejects_non_finite_str_bytes_and_decimal() -> None:
    huge = "1" + "0" * 400 + ".0"
    for bad in (
        huge,
        "-" + huge,
        huge.encode("ascii"),
        b"-" + huge.encode("ascii"),
        Decimal("1e500"),
        Decimal("-1e500"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ):
        with pytest.raises(ValueError, match="finite"):
            pyropdf.Real(bad)
