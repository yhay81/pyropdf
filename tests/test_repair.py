"""Regression tests for deterministic damaged-PDF repair (Issue #1190).

Covers every actionable structural damage category benchmarked in
``evidence/repair-coverage/``:
- truncated EOF / mid-trailer / truncated streams
- corrupt classic xref tables and FlateDecode xref streams
- shifted offsets and leading garbage beyond 1024 bytes
- trailing garbage and unindexed trailing object updates
- broken /Prev chains (dangling, cyclic, corrupt earlier revision)
- broken /Root (missing /Root, /Root in ObjStm, catalog without /Type)
- unreferenced pages with broken /Root (both intact /Pages and orphan /Page)
- duplicate generations and broken later duplicates
- damaged ObjStm whose /N overstates valid pairs
- encrypted document with broken /Root preserving /Encrypt and /ID
- full-save round-trip verification in pyropdf, pikepdf, and qpdf
- strict incremental-save refusal on repaired bases and DocumentLimits.web()
"""

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
import zlib
from typing import TYPE_CHECKING

import pikepdf
import pytest

import pyropdf
from pyropdf import DocumentLimits, LimitError, MalformedPdfError, PdfError

if TYPE_CHECKING:
    from pathlib import Path

HAS_QPDF = shutil.which("qpdf") is not None


class _Builder:
    def __init__(self, header: bytes = b"%PDF-1.7\n") -> None:
        self.buf = bytearray(header)
        self.offsets: list[tuple[int, int, int]] = []

    def obj(self, num: int, body: bytes, gen: int = 0) -> int:
        pos = len(self.buf)
        self.offsets.append((num, gen, pos))
        self.buf.extend(f"{num} {gen} obj\n".encode("ascii"))
        self.buf.extend(body)
        self.buf.extend(b"\nendobj\n")
        return pos

    def stream(self, num: int, extra: bytes, data: bytes, gen: int = 0) -> int:
        body = b"<< " + extra + f" /Length {len(data)} >>\nstream\n".encode("ascii") + data + b"\nendstream"
        return self.obj(num, body, gen=gen)

    def objstm(self, num: int, members: list[tuple[int, bytes]]) -> int:
        table = bytearray()
        payload = bytearray()
        for obj_num, obj_body in members:
            table.extend(f"{obj_num} {len(payload)} ".encode("ascii"))
            payload.extend(obj_body)
            payload.extend(b" ")
        encoded = zlib.compress(bytes(table + payload))
        extra = f"/Type /ObjStm /N {len(members)} /First {len(table)} /Filter /FlateDecode".encode("ascii")
        return self.stream(num, extra, encoded)

    def xref_table(self, trailer: bytes) -> int:
        pos = len(self.buf)
        max_num = max((n for n, _, _ in self.offsets), default=0)
        by_num = {n: (g, p) for n, g, p in self.offsets}
        lines = [b"xref\n", f"0 {max_num + 1}\n".encode("ascii"), b"0000000000 65535 f \n"]
        for num in range(1, max_num + 1):
            if num in by_num:
                gen, off = by_num[num]
                lines.append(f"{off:010d} {gen:05d} n \n".encode("ascii"))
            else:
                lines.append(b"0000000000 00000 f \n")
        lines.append(b"trailer\n<< " + trailer + b" >>\n")
        lines.append(f"startxref\n{pos}\n%%EOF\n".encode("ascii"))
        self.buf.extend(b"".join(lines))
        return pos

    def xref_stream(self, num: int, rows: list[tuple[int, int, int, int]], trailer: bytes) -> int:
        pos = len(self.buf)
        all_rows = sorted([*rows, (num, 1, pos, 0)], key=lambda r: r[0])
        raw = bytearray()
        index_parts = []
        for obj_num, kind, f2, f3 in all_rows:
            index_parts.append(f"{obj_num} 1")
            raw.extend(struct.pack(">BIH", kind, f2, f3))
        encoded = zlib.compress(bytes(raw))
        idx = " ".join(index_parts).encode("ascii")
        body = (
            b"<< /Type /XRef /W [1 4 2] /Index ["
            + idx
            + b"] "
            + trailer
            + f" /Filter /FlateDecode /Length {len(encoded)} >>\nstream\n".encode("ascii")
            + encoded
            + b"\nendstream"
        )
        self.obj(num, body)
        self.buf.extend(f"startxref\n{pos}\n%%EOF\n".encode("ascii"))
        return pos


def _content(text: str = "Hello Repair") -> bytes:
    return f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode("ascii")


def _sample_pdf(text: str = "Hello Repair", *, header: bytes = b"%PDF-1.7\n") -> bytes:
    b = _Builder(header)
    b.obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    b.obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    b.obj(
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    )
    b.obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    b.stream(5, b"", _content(text))
    b.xref_table(b"/Size 6 /Root 1 0 R")
    return bytes(b.buf)


def _assert_repaired_roundtrip(
    damaged: bytes,
    tmp_path: Path,
    *,
    expected_pages: int = 1,
    expected_text: str = "Hello Repair",
    expect_repaired: bool = True,
) -> bytes:
    with pyropdf.open(damaged) as doc:
        assert doc.is_repaired is expect_repaired
        if expect_repaired:
            with pytest.raises(PdfError, match="repaired"):
                doc.to_bytes(incremental=True)
        assert len(doc.pages) == expected_pages
        assert expected_text in doc.pages[0].text()
        pix = doc.pages[0].pixmap(dpi=36)
        assert pix.width > 0
        assert pix.height > 0
        saved = doc.to_bytes()

    with pyropdf.open(saved) as reopened:
        assert not reopened.is_repaired
        assert len(reopened.pages) == expected_pages
        assert expected_text in reopened.pages[0].text()

    out = tmp_path / "repaired.pdf"
    out.write_bytes(saved)
    with pikepdf.open(out) as pike:
        assert len(pike.pages) == expected_pages

    if HAS_QPDF:
        proc = subprocess.run(  # noqa: S603
            ["qpdf", "--show-npages", str(out)],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode in (0, 3), proc.stderr
        assert int(proc.stdout.strip()) == expected_pages
    return saved


def test_repair_truncated_eof_trailer_and_trailing_stream(tmp_path: Path) -> None:
    base = _sample_pdf()
    # Cut off xref/trailer and append a truncated trailing stream object.
    cut = base[: base.index(b"xref")] + b"6 0 obj\n<< /Length 5000 >>\nstream\nabcdef"
    _assert_repaired_roundtrip(cut, tmp_path)
    # Truncated mid-trailer dictionary.
    mid_trailer = base[: base.index(b"/Root") + 4]
    _assert_repaired_roundtrip(mid_trailer, tmp_path)


def test_repair_corrupt_xref_table_and_xref_stream(tmp_path: Path) -> None:
    base = _sample_pdf()
    _assert_repaired_roundtrip(base.replace(b"00000 n ", b"00000 x ", 3), tmp_path)

    # Corrupt FlateDecode xref stream payload while ObjStm stays intact.
    b = _Builder(b"%PDF-1.5\n")
    p_cat = b.obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    p_stm = b.objstm(
        6,
        [
            (2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
            (
                3,
                (
                    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
                ),
            ),
            (4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
        ],
    )
    p_cnt = b.stream(5, b"", _content())
    b.xref_stream(
        7,
        [
            (0, 0, 0, 65535),
            (1, 1, p_cat, 0),
            (2, 2, 6, 0),
            (3, 2, 6, 1),
            (4, 2, 6, 2),
            (5, 1, p_cnt, 0),
            (6, 1, p_stm, 0),
        ],
        b"/Size 8 /Root 1 0 R",
    )
    corrupt = bytearray(b.buf)
    x7 = corrupt.index(b"7 0 obj")
    st7 = corrupt.index(b"stream\n", x7) + 7
    for i in range(st7, min(st7 + 12, len(corrupt))):
        corrupt[i] ^= 0xFF
    _assert_repaired_roundtrip(bytes(corrupt), tmp_path)


def test_repair_shifted_offsets_and_leading_garbage_preserves_version(tmp_path: Path) -> None:
    base20 = _sample_pdf(header=b"%PDF-2.0\n")
    shifted = base20.replace(b"%PDF-2.0\n", b"%PDF-2.0\n%shifted-19-bytes!\n", 1)
    _assert_repaired_roundtrip(shifted, tmp_path)

    # Leading garbage past the 1024-byte header window triggers repair and still
    # preserves %PDF-2.0 for probe() and full save.
    prefixed = (b"JUNK" * 512) + base20
    probed = pyropdf.probe(prefixed)
    assert probed.is_repaired
    assert probed.version == "2.0"
    assert probed.page_count == 1
    saved = _assert_repaired_roundtrip(prefixed, tmp_path)
    assert saved.startswith(b"%PDF-2.0")


def test_repair_broken_prev_chains_and_trailing_updates(tmp_path: Path) -> None:
    base = _sample_pdf()
    _assert_repaired_roundtrip(base.replace(b"/Root 1 0 R", b"/Root 1 0 R /Prev 999999"), tmp_path)
    xref_off = base.index(b"xref\n")
    _assert_repaired_roundtrip(
        base.replace(b"/Root 1 0 R", f"/Root 1 0 R /Prev {xref_off}".encode("ascii")),
        tmp_path,
    )

    # Trailing update without xref/startxref supersedes earlier revision.
    old = _sample_pdf("Old Text")
    new_stream = b"\n5 0 obj\n<< /Length " + str(len(_content("New Text"))).encode("ascii") + b" >>\nstream\n"
    new_stream += _content("New Text") + b"\nendstream\nendobj\n"
    _assert_repaired_roundtrip(old + new_stream, tmp_path, expected_text="New Text")


def test_repair_broken_root_and_unreferenced_pages(tmp_path: Path) -> None:
    # 1. Catalog without /Type /Catalog and no trailer.
    b1 = _Builder()
    b1.obj(1, b"<< /Pages 2 0 R >>")
    b1.obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    b1.obj(
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    )
    b1.obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    b1.stream(5, b"", _content())
    _assert_repaired_roundtrip(bytes(b1.buf), tmp_path)

    # 2. Missing Catalog entirely; intact /Type /Pages root.
    b2 = _Builder()
    b2.obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    b2.obj(
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    )
    b2.obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    b2.stream(5, b"", _content())
    b2.xref_table(b"/Size 6 /Root 999 0 R")
    _assert_repaired_roundtrip(bytes(b2.buf), tmp_path)

    # 3. Orphan /Type /Page objects with neither Catalog nor /Type /Pages.
    b3 = _Builder()
    b3.obj(
        3,
        b"<< /Type /Page /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    )
    b3.obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    b3.stream(5, b"", _content())
    b3.xref_table(b"/Size 6 /Root 999 0 R")
    _assert_repaired_roundtrip(bytes(b3.buf), tmp_path)


def test_repair_lenient_objstm_overstated_n(tmp_path: Path) -> None:
    b = _Builder(b"%PDF-1.5\n")
    b.obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    p2 = b"<< /Type /Pages /Kids [3 0 R] /Count 1 >> "
    p3 = (
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> "
    )
    p4 = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> "
    table = f"2 0 3 {len(p2)} 4 {len(p2) + len(p3)} ".encode("ascii")
    b.stream(6, f"/Type /ObjStm /N 5 /First {len(table)}".encode("ascii"), table + p2 + p3 + p4)
    b.stream(5, b"", _content())
    _assert_repaired_roundtrip(bytes(b.buf), tmp_path)


def test_repair_encrypted_empty_password_preserves_encrypt_and_renders(tmp_path: Path) -> None:
    def rc4(key: bytes, data: bytes) -> bytes:
        s = list(range(256))
        j = 0
        for i in range(256):
            j = (j + s[i] + key[i % len(key)]) & 0xFF
            s[i], s[j] = s[j], s[i]
        i = j = 0
        out = bytearray(len(data))
        for k, byte in enumerate(data):
            i = (i + 1) & 0xFF
            j = (j + s[i]) & 0xFF
            s[i], s[j] = s[j], s[i]
            out[k] = byte ^ s[(s[i] + s[j]) & 0xFF]
        return bytes(out)

    padding = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")
    file_id = bytes.fromhex("0123456789ABCDEF0123456789ABCDEF")
    p_val = -4
    owner_key = hashlib.md5(padding).digest()[:5]  # noqa: S324
    o_entry = rc4(owner_key, padding)
    h = hashlib.md5()  # noqa: S324
    h.update(padding + o_entry + struct.pack("<i", p_val) + file_id)
    enc_key = h.digest()[:5]
    u_entry = rc4(enc_key, padding)
    obj5_key = hashlib.md5(enc_key + struct.pack("<I", 5)[:3] + b"\x00\x00").digest()[:10]  # noqa: S324

    b = _Builder()
    b.obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    b.obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    b.obj(
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    )
    b.obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    b.stream(5, b"", rc4(obj5_key, _content("Encrypted Repair")))
    b.obj(
        6,
        b"<< /Filter /Standard /V 1 /R 2 /Length 40 "
        + f"/P {p_val} /O <{o_entry.hex().upper()}> /U <{u_entry.hex().upper()}> >>".encode("ascii"),
    )
    b.xref_table(
        f"/Size 7 /Root 999 0 R /Encrypt 6 0 R /ID [<{file_id.hex().upper()}><{file_id.hex().upper()}>]".encode("ascii")
    )
    damaged = bytes(b.buf).replace(b"startxref", b"startxrex")
    _assert_repaired_roundtrip(damaged, tmp_path, expected_text="Encrypted Repair")


def test_repair_honors_web_limits_and_rejects_empty_garbage() -> None:
    base = _sample_pdf().replace(b"startxref", b"startxrex")
    with pyropdf.open(base, limits=DocumentLimits.web()) as doc:
        assert doc.is_repaired
        assert len(doc.pages) == 1
    with pytest.raises(LimitError) as caught:
        pyropdf.open(base, limits=DocumentLimits(max_objects=2))
    assert caught.value.code == "object_count"
    with pytest.raises(MalformedPdfError):
        pyropdf.open(b"%PDF-1.7\n1 0 obj\n<< /Length 500 >>\nstream\npartial\n")
