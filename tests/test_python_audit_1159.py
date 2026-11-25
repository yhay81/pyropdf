"""Regression tests for issue #1159 (pre-v0.4.1 Python & PyO3 audit fixes)."""

from __future__ import annotations

import ast
import contextlib
import gc
import struct
import sys
import sysconfig
import threading
import time
import zlib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import pyropdf
from pyropdf import Access, DocumentClosedError, FallbackFont, Font, LimitError, Name, Operations, Permissions, _core

if TYPE_CHECKING:
    from collections.abc import Callable

    from pyropdf._signing import Timestamper

ROOT = Path(__file__).resolve().parent.parent
FONTS = ROOT / "crates" / "pyropdf-engine" / "src" / "fonts" / "testdata"
JAPANESE_FONT = FONTS / "NotoSansJP-subset.otf"
USER_CREDENTIAL = "user-audit-1159"
OWNER_CREDENTIAL = "owner-audit-1159"


def _build_raw_pdf(objects: dict[int, str]) -> bytes:
    """Return a PDF of ``objects`` by number, with a valid cross-reference table."""
    out = bytearray(b"%PDF-1.7\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode("latin-1")
    size = max(objects) + 1
    xref = len(out)
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010} 00000 n \n".encode() if number in offsets else b"0000000000 00000 f \n"
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _stream(content: str, entries: str = "") -> str:
    return f"<< /Length {len(content.encode('latin-1'))} {entries}>>\nstream\n{content}\nendstream"


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


def _encrypted_pdf_bytes() -> bytes:
    source = pyropdf.Document()
    page = source.pages.new((200, 200))
    with page.draw() as canvas:
        canvas.text("Secret", (20, 100), font=Font.standard("Helvetica"), size=14)
    return source.to_bytes(
        encryption=pyropdf.Encryption(
            user_password=USER_CREDENTIAL,
            owner_password=OWNER_CREDENTIAL,
            permissions=Permissions.PRINT,
            method="aes-256",
        )
    )


def _run_threads(tasks: list[Callable[[], None]], *, timeout: float = 15.0) -> list[BaseException]:
    errors: list[BaseException] = []
    lock = threading.Lock()

    def runner(task: Callable[[], None]) -> None:
        try:
            task()
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=runner, args=(task,)) for task in tasks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
        assert not thread.is_alive(), "worker thread deadlocked"
    return errors


# 1. Direct-dict key in Page.replace_text() and _recolor.py.


def test_replace_text_works_on_canvas_standard_font_and_direct_font_dicts() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((300, 200))
    with page.draw() as canvas:
        canvas.text("Hello PDF world", (36, 100), font=Font.standard("Helvetica"), size=18)

    assert page.replace_text("PDF", "Cat") == 1
    assert "Hello Cat world" in page.text()
    assert "PDF" not in page.text()
    with pyropdf.open(doc.to_bytes()) as reopened:
        assert "Hello Cat world" in reopened.pages[0].text()

    direct_pdf = _build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 300 200] >>",
            3: (
                "<< /Type /Page /Parent 2 0 R /Contents 4 0 R "
                "/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> >>"
            ),
            4: _stream("BT /F1 16 Tf 20 120 Td (One PDF) Tj /F1 16 Tf 0 -24 Td (Two PDF) Tj ET"),
        }
    )
    with pyropdf.open(direct_pdf) as direct_doc:
        direct_page = direct_doc.pages[0]
        assert direct_page.replace_text("PDF", "Dog") == 2
        text = direct_page.text()
        assert "One Dog" in text
        assert "Two Dog" in text

    gstate_pdf = _build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 300 200] >>",
            3: (
                "<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << /ExtGState << "
                "/GS1 << /Font [ << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> 16 ] >> >> >> >>"
            ),
            4: _stream("BT /GS1 gs 20 120 Td (State PDF) Tj ET"),
        }
    )
    with pyropdf.open(gstate_pdf) as gstate_doc:
        assert gstate_doc.pages[0].replace_text("PDF", "Fox") == 1
        assert "State Fox" in gstate_doc.pages[0].text()


def test_recolor_caches_direct_inherited_resources() -> None:
    pdf = _build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: (
                "<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 /MediaBox [0 0 200 200] "
                "/Resources << /XObject << /Fm0 7 0 R >> >> >>"
            ),
            3: "<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>",
            4: _stream("/Fm0 Do"),
            5: "<< /Type /Page /Parent 2 0 R /Contents 6 0 R >>",
            6: _stream("/Fm0 Do"),
            7: _stream("1 0 0 rg 0 0 10 10 re f", "/Type /XObject /Subtype /Form /BBox [0 0 10 10] "),
        }
    )
    with pyropdf.open(pdf) as doc:
        doc.recolor("gray")
        copy0 = doc.pages[0].obj["Resources"]["XObject"]["Fm0"]
        copy1 = doc.pages[1].obj["Resources"]["XObject"]["Fm0"]
        assert copy0.xref is not None
        assert copy0.xref != 7
        assert copy0.xref == copy1.xref
        ops = [op.operator for op in Operations.parse(copy0.read())]
        assert "g" in ops
        assert "rg" not in ops


# 2. Canvas.__enter__ cleans up transaction and write_lock on failure.


def test_canvas_enter_cleans_up_transaction_and_write_lock_on_catalog_failure() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    saved_root = doc.trailer["Root"]
    doc.trailer["Root"] = 123

    canvas = page.draw()
    with pytest.raises(pyropdf.StaleObjectError, match="catalog"), canvas:
        pass
    assert canvas._operation is None  # noqa: SLF001
    assert not canvas._entered  # noqa: SLF001

    doc.history.clear()

    done = threading.Event()

    def restore_root() -> None:
        with doc.transaction("restore-root"):
            doc.trailer["Root"] = saved_root
        done.set()

    assert _run_threads([restore_root], timeout=5.0) == []
    assert done.is_set()

    with doc.transaction("outer"):
        doc.trailer["Root"] = 123
        nested = page.draw()
        with pytest.raises(pyropdf.StaleObjectError, match="catalog"), nested:
            pass
        assert nested._operation is None  # noqa: SLF001
        doc.trailer["Root"] = saved_root
        doc.catalog["AfterNestedFailure"] = 1
    assert doc.catalog["AfterNestedFailure"] == 1
    doc.history.clear()


# 3. Concurrent close() vs unlock() / sign() / timestamp() and concurrent unlock() races.


def _race_unlock_vs_close(encrypted: bytes) -> None:
    doc = pyropdf.open(encrypted)
    core = doc._doc  # noqa: SLF001
    barrier = threading.Barrier(3)

    def unlock_with(password: str) -> None:
        barrier.wait(timeout=5.0)
        with contextlib.suppress(DocumentClosedError):
            doc.unlock(password)

    def close_doc() -> None:
        barrier.wait(timeout=5.0)
        doc.close()

    errors = _run_threads(
        [
            lambda: unlock_with(USER_CREDENTIAL),
            lambda: unlock_with(OWNER_CREDENTIAL),
            close_doc,
        ]
    )
    assert errors == []
    assert doc.is_closed
    with pytest.raises(DocumentClosedError):
        doc.to_bytes()
    with pytest.raises(DocumentClosedError):
        _core.page_count(core)


def test_concurrent_close_cannot_be_resurrected_by_unlock() -> None:
    encrypted = _encrypted_pdf_bytes()
    for _ in range(12):
        _race_unlock_vs_close(encrypted)


def _race_unlock_owner_and_user(encrypted: bytes, marker: int, *, owner_edits: bool) -> None:
    doc = pyropdf.open(encrypted)
    barrier = threading.Barrier(3)

    def unlock_owner() -> None:
        barrier.wait(timeout=5.0)
        assert doc.unlock(OWNER_CREDENTIAL) is Access.OWNER
        if owner_edits:
            doc.catalog["AuditMarker"] = marker

    def unlock_user(*, edit: bool) -> None:
        barrier.wait(timeout=5.0)
        assert doc.unlock(USER_CREDENTIAL) is Access.USER
        if edit:
            doc.catalog["AuditMarker"] = marker

    errors = _run_threads(
        [
            unlock_owner,
            lambda: unlock_user(edit=not owner_edits),
            lambda: unlock_user(edit=False),
        ]
    )
    assert errors == []
    assert not doc.needs_password
    assert doc.access is Access.OWNER
    assert doc.permissions == Permissions.ALL
    assert doc.catalog["AuditMarker"] == marker
    doc.close()


def test_concurrent_unlock_preserves_edits_and_never_downgrades_owner_access() -> None:
    encrypted = _encrypted_pdf_bytes()
    for round_idx in range(8):
        _race_unlock_owner_and_user(encrypted, round_idx, owner_edits=True)
        _race_unlock_owner_and_user(encrypted, round_idx + 100, owner_edits=False)


def _race_sign_vs_close(base_bytes: bytes, certs: list[bytes], sign_fn: Callable[[bytes], bytes]) -> None:
    doc = pyropdf.open(base_bytes)
    core = doc._doc  # noqa: SLF001
    entered = threading.Event()

    def slow_sign(payload: bytes) -> bytes:
        entered.set()
        time.sleep(0.005)
        return sign_fn(payload)

    signer = pyropdf.Signer.callback(certs, slow_sign)

    def do_sign() -> None:
        with contextlib.suppress(DocumentClosedError):
            doc.sign("Sig1", signer)

    def do_close() -> None:
        entered.wait(timeout=5.0)
        doc.close()

    assert _run_threads([do_sign, do_close]) == []
    assert doc.is_closed
    with pytest.raises(DocumentClosedError):
        _core.page_count(core)


def _race_timestamp_vs_close(base_bytes: bytes, tsa: Timestamper) -> None:
    doc = pyropdf.open(base_bytes)
    core = doc._doc  # noqa: SLF001
    entered = threading.Event()

    def slow_stamp(digest: bytes, algorithm: str) -> bytes:
        entered.set()
        time.sleep(0.005)
        return tsa(digest, algorithm)

    def do_timestamp() -> None:
        with contextlib.suppress(DocumentClosedError):
            doc.timestamp(slow_stamp)

    def do_close() -> None:
        entered.wait(timeout=5.0)
        doc.close()

    assert _run_threads([do_timestamp, do_close]) == []
    assert doc.is_closed
    with pytest.raises(DocumentClosedError):
        _core.page_count(core)


def test_concurrent_close_cannot_be_resurrected_by_sign_or_timestamp() -> None:
    if sysconfig.get_config_var("Py_GIL_DISABLED") == 1:
        pytest.skip("pyHanko enables the GIL of a free-threaded build")

    from cryptography.hazmat.primitives import hashes  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import ec  # noqa: PLC0415
    from test_signing import DER, _timestamper, make_pki  # noqa: PLC0415

    pki = make_pki()
    key, cert = pki.signers["p256"]
    certs = [cert.public_bytes(DER), pki.root.public_bytes(DER)]
    tsa = _timestamper(pki)

    def sign_payload(payload: bytes) -> bytes:
        return key.sign(payload, ec.ECDSA(hashes.SHA256()))

    base = pyropdf.Document()
    base.pages.new((200, 200))
    base_bytes = base.to_bytes()

    # Same-thread close() inside sign() callback must not deadlock or resurrect.
    doc_same = pyropdf.open(base_bytes)
    core_same = doc_same._doc  # noqa: SLF001

    def sign_and_close(payload: bytes) -> bytes:
        doc_same.close()
        return sign_payload(payload)

    with pytest.raises(DocumentClosedError):
        doc_same.sign("Sig1", pyropdf.Signer.callback(certs, sign_and_close))
    assert doc_same.is_closed
    with pytest.raises(DocumentClosedError):
        _core.page_count(core_same)

    for _ in range(6):
        _race_sign_vs_close(base_bytes, certs, sign_payload)

    # Same-thread close() inside timestamp() callback must not deadlock or resurrect.
    doc_ts_same = pyropdf.open(base_bytes)
    core_ts_same = doc_ts_same._doc  # noqa: SLF001

    def stamp_and_close(digest: bytes, algorithm: str) -> bytes:
        doc_ts_same.close()
        return tsa(digest, algorithm)

    with pytest.raises(DocumentClosedError):
        doc_ts_same.timestamp(stamp_and_close)
    assert doc_ts_same.is_closed
    with pytest.raises(DocumentClosedError):
        _core.page_count(core_ts_same)

    for _ in range(6):
        _race_timestamp_vs_close(base_bytes, tsa)


# 4. Document.sanitize(..., hidden_text=True) preserves operations.tail.


def test_sanitize_hidden_text_preserves_operations_tail(tmp_path: Path) -> None:
    doc = pyropdf.Document()
    doc.pages.new((200, 100))
    page = doc.pages[0]
    font = doc.objects[doc.objects.add({"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Helvetica")})]
    form_bytes = b"BT /F1 12 Tf 10 40 Td (FormVis) Tj 3 Tr (HiddenForm) Tj ET\n% form-tail\n"
    page_bytes = b"BT /F1 12 Tf 10 70 Td (PageVis) Tj 3 Tr (HiddenPage) Tj 0 Tr ET /FM1 Do\n% page-tail\n"
    form_xref = doc.objects.add_stream(
        form_bytes,
        {
            "Type": Name("XObject"),
            "Subtype": Name("Form"),
            "BBox": [0, 0, 200, 100],
            "Resources": {"Font": {"F1": font}},
        },
    )
    page.obj["Resources"] = {"Font": {"F1": font}, "XObject": {"FM1": doc.objects[form_xref]}}
    page.obj["Contents"] = doc.objects[doc.objects.add_stream(page_bytes)]

    expected_page_tail = page.operations.tail
    expected_form_tail = Operations.parse(doc.objects[form_xref].read()).tail
    assert b"% page-tail" in expected_page_tail
    assert b"% form-tail" in expected_form_tail

    out_path = tmp_path / "sanitized.pdf"
    report = doc.sanitize(
        out_path,
        javascript=False,
        actions=False,
        attachments=False,
        metadata=False,
        comments=False,
        hidden_text=True,
        unused=False,
    )
    assert report.removed == {"hidden_text": 2}
    assert page.operations.tail == expected_page_tail
    assert Operations.parse(doc.objects[form_xref].read()).tail == expected_form_tail

    with pyropdf.open(out_path) as reopened:
        reopened_page = reopened.pages[0]
        assert reopened_page.operations.tail == expected_page_tail
        reopened_form = reopened_page.obj["Resources"]["XObject"]["FM1"]
        assert Operations.parse(reopened_form.read()).tail == expected_form_tail
        words = reopened_page.text().split()
        assert "PageVis" in words
        assert "FormVis" in words
        assert "HiddenPage" not in words
        assert "HiddenForm" not in words


# 5 & 6. Document.close() releases caches; fallback_fonts & _revisions.py are thread-safe.


def test_document_close_releases_caches() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    with page.draw() as canvas:
        canvas.image((10, 10, 50, 50), _png_bytes(4, 4, (255, 0, 0, 255)))
    _ = page.pixmap(dpi=36)
    doc.pages.new((100, 100))
    del doc.pages[1]

    form_font = Font.from_file(JAPANESE_FONT)
    fallback_font = Font.from_file(JAPANESE_FONT)
    fallback_entry = FallbackFont(sans=fallback_font)
    doc.form.font = form_font
    doc.fallback_fonts["ja"] = fallback_entry

    drawn = doc._drawn  # noqa: SLF001
    assert drawn is not None
    drawn_refs = sys.getrefcount(drawn)
    form_font_refs = sys.getrefcount(form_font)
    fallback_entry_refs = sys.getrefcount(fallback_entry)

    doc.close()
    gc.collect()

    assert doc._drawn is None  # noqa: SLF001
    assert doc._fallback is None  # noqa: SLF001
    assert doc._form_font is None  # noqa: SLF001
    assert doc._access is None  # noqa: SLF001
    assert sys.getrefcount(drawn) < drawn_refs
    assert sys.getrefcount(form_font) < form_font_refs
    assert sys.getrefcount(fallback_entry) < fallback_entry_refs


def _concurrent_set_languages(doc: pyropdf.Document, fb: FallbackFont, languages: tuple[str, ...]) -> None:
    doc.fallback_fonts = {}
    barrier = threading.Barrier(len(languages))

    def set_lang(language: str) -> None:
        barrier.wait(timeout=5.0)
        doc.fallback_fonts[language] = fb

    assert _run_threads([lambda lang=lang: set_lang(lang) for lang in languages], timeout=10.0) == []
    assert set(doc.fallback_fonts) == set(languages)


def test_fallback_fonts_and_revision_open_are_thread_safe() -> None:
    font = Font.from_file(JAPANESE_FONT)
    fb_sans = FallbackFont(sans=font)
    fb_serif = FallbackFont(serif=font)

    src = pyropdf.Document()
    src.pages.new((100, 100))
    with pyropdf.open(src.to_bytes()) as edited:
        edited.metadata = {"title": "rev1"}
        two_revs = edited.to_bytes(incremental=True)

    with pyropdf.open(two_revs) as doc:
        languages = ("ja", "ko", "zh-CN", "zh-TW")
        for _ in range(15):
            _concurrent_set_languages(doc, fb_sans, languages)

        revs = doc.revisions
        assert len(revs) >= 2
        barrier = threading.Barrier(4)

        def mutator_items() -> None:
            barrier.wait(timeout=5.0)
            for _ in range(40):
                doc.fallback_fonts["ja"] = fb_sans
                doc.fallback_fonts["ko"] = fb_serif
                with contextlib.suppress(KeyError):
                    del doc.fallback_fonts["ko"]

        def mutator_replace() -> None:
            barrier.wait(timeout=5.0)
            for _ in range(40):
                doc.fallback_fonts = {"ja": fb_serif}
                del doc.fallback_fonts

        def reader_and_render() -> None:
            barrier.wait(timeout=5.0)
            for _ in range(40):
                _ = list(doc.fallback_fonts)
                _ = len(doc.fallback_fonts)
                _ = "ja" in doc.fallback_fonts
                _ = repr(doc.fallback_fonts)
                _ = doc.pages[0].pixmap(dpi=36)

        def revision_opener() -> None:
            barrier.wait(timeout=5.0)
            for _ in range(30):
                with revs[-1].open() as rev_doc:
                    _ = list(rev_doc.fallback_fonts)

        errors = _run_threads(
            [mutator_items, mutator_replace, reader_and_render, revision_opener],
            timeout=20.0,
        )
        assert errors == []


# 7. _core.pyi stubs match runtime behavior and open_buffer shares one Budget.


def test_core_pyi_stubs_match_runtime_and_open_buffer_budget() -> None:
    stub_path = ROOT / "python" / "pyropdf" / "_core.pyi"
    tree = ast.parse(stub_path.read_text(encoding="utf-8"))

    pixmap_cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Pixmap")
    from_bytes_fn = next(
        node for node in pixmap_cls.body if isinstance(node, ast.FunctionDef) and node.name == "from_bytes"
    )
    data_ann = ast.unparse(from_bytes_fn.args.args[0].annotation) if from_bytes_fn.args.args[0].annotation else ""
    assert "Pixmap" not in data_ann

    pm = pyropdf.Pixmap.from_bytes(_png_bytes(2, 2, (10, 20, 30, 255)))
    with pytest.raises(ValueError, match="not PNG, JPEG"):
        pyropdf.Pixmap.from_bytes(pm)  # ty: ignore[invalid-argument-type]

    funcs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}

    add_args = funcs["annotation_add"].args
    assert add_args.posonlyargs == []
    assert add_args.kwonlyargs == []
    assert [arg.arg for arg in add_args.args] == ["document", "number", "generation", "spec", "face"]
    assert len(add_args.defaults) == 1

    edit_args = funcs["annotation_edit"].args
    assert edit_args.posonlyargs == []
    assert edit_args.kwonlyargs == []
    assert [arg.arg for arg in edit_args.args] == ["document", "number", "generation", "index", "id", "edit", "face"]
    assert len(edit_args.defaults) == 1

    unlock_args = funcs["unlock"].args
    assert unlock_args.posonlyargs == []
    assert unlock_args.kwonlyargs == []
    assert [arg.arg for arg in unlock_args.args] == ["document", "password", "recipient"]
    assert len(unlock_args.defaults) == 2

    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    annot_id = _core.annotation_add(
        doc._core(),  # noqa: SLF001
        page._number,  # noqa: SLF001
        page._generation,  # noqa: SLF001
        {"subtype": "Text", "rect": (10.0, 10.0, 30.0, 30.0), "contents": "hi"},
        False,  # noqa: FBT003 - testing positional face argument
    )
    assert annot_id is not None
    edited_id = _core.annotation_edit(
        doc._core(),  # noqa: SLF001
        page._number,  # noqa: SLF001
        page._generation,  # noqa: SLF001
        0,
        id=annot_id,
        edit={"contents": "updated", "clear": []},
        face=False,
    )
    assert edited_id is not None
    assert page.annotations[0].contents == "updated"

    enc_bytes = _encrypted_pdf_bytes()
    with pyropdf.open(enc_bytes) as enc_doc:
        assert _core.unlock(enc_doc._core(), password=USER_CREDENTIAL.encode()) == "user"  # noqa: SLF001

    raw = doc.to_bytes()
    token = pyropdf.CancellationToken()
    token.cancel()
    for buf in (raw, bytearray(raw)):
        with pytest.raises(LimitError) as cancelled_exc:
            pyropdf.open(buf, limits=pyropdf.DocumentLimits(cancellation=token))
        assert cancelled_exc.value.code == "cancelled"
        with pytest.raises(LimitError) as size_exc:
            pyropdf.open(buf, limits=pyropdf.DocumentLimits(max_file_size=10))
        assert size_exc.value.code == "file_size"
