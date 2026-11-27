# ruff: noqa: INP001, PLR0913, PLR0915, PLR2004 - standalone evidence matrix generator, not a package.
"""Deterministic damaged-PDF test matrix for repair-coverage benchmarking.

Generates two suites using only the Python standard library so every isolated
engine environment (pyropdf, PyMuPDF, qpdf, pypdfium2) evaluates the exact
same byte streams:

1. Corpus-derived damage: all 15 real-world PDFs in ``tests/assets/real_world``
   under 19 deterministic structural damage categories (the 9 historical kinds
   from ``tests/corpus.py`` plus 10 additional structural-damage kinds).
2. Targeted malformed fixtures: 31 self-contained synthetic PDFs isolating
   individual repair scenarios (truncated trailers, corrupt xref streams,
   shifted offsets, leading/trailing garbage, broken /Prev chains, damaged
   object streams, unreferenced pages with broken /Root, duplicate generations,
   object 0, missing headers, bare CR endings, and encrypted broken xref).
"""

from __future__ import annotations

import hashlib
import random
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[2]
REAL_WORLD = ROOT / "tests" / "assets" / "real_world"
CORPUS_SOURCES = tuple(sorted(REAL_WORLD.glob("*.pdf")))

#: Expected intact page counts of tests/assets/real_world/*.pdf.
CORPUS_PAGE_COUNTS: dict[str, int] = {
    "bill-hr815.pdf": 110,
    "bunka-kokugo-series-019-p4.pdf": 1,
    "f1040.pdf": 2,
    "mhlw-doc.pdf": 2,
    "nics-background-checks-2015-11.pdf": 1,
    "patent-us223898.pdf": 4,
    "pdf20-simple.pdf": 1,
    "pdfium-jpx-lzw.pdf": 1,
    "pdfium-links-highlights-annots.pdf": 1,
    "pdfium-ocg-invisible.pdf": 1,
    "pdfium-smask-blend.pdf": 1,
    "pdfium-type3.pdf": 1,
    "senate-expenditures.pdf": 1,
    "usrguide.pdf": 27,
    "wdl6812-manuscript.pdf": 2,
}

#: Corpus PDFs whose intact page 0 has no default extractable text (pure image/graphics or unmapped Type 3).
NO_TEXT_CORPUS: frozenset[str] = frozenset(
    {
        "patent-us223898.pdf",
        "pdfium-jpx-lzw.pdf",
        "pdfium-ocg-invisible.pdf",
        "pdfium-smask-blend.pdf",
        "pdfium-type3.pdf",
        "wdl6812-manuscript.pdf",
    }
)


@dataclass(frozen=True)
class Case:
    """One damaged-PDF test case with its expected baseline properties."""

    case_id: str
    suite: str
    category: str
    source: str
    data: bytes
    expected_pages: int
    expect_text: bool
    expected_substring: str | None = None
    expect_repaired: bool = True


# ---------------------------------------------------------------------------
# Corpus-derived damage generators
# ---------------------------------------------------------------------------


def _startxref_value_span(data: bytes) -> tuple[int, int]:
    """Return the byte span of the final startxref value."""
    eof = data.rfind(b"%%EOF")
    startxref = data.rfind(b"startxref", 0, eof if eof >= 0 else len(data))
    if startxref < 0:
        msg = "missing startxref"
        raise ValueError(msg)
    number_start = startxref + len(b"startxref")
    while data[number_start] in b"\x00\t\n\x0c\r ":
        number_start += 1
    number_end = number_start
    while data[number_end : number_end + 1].isdigit():
        number_end += 1
    return number_start, number_end


def _final_xref_offset(data: bytes) -> int:
    start, end = _startxref_value_span(data)
    return data.index(b"%PDF-") + int(data[start:end])


def _truncated(fraction: float) -> Callable[[str, bytes], bytes]:
    def damage(_name: str, data: bytes) -> bytes:
        return data[: int(len(data) * fraction)]

    return damage


def _junk_prefixed(length: int) -> Callable[[str, bytes], bytes]:
    def damage(name: str, data: bytes) -> bytes:
        rng = random.Random(f"junk:{name}") if length == 512 else random.Random(f"junk{length}:{name}")  # noqa: S311
        return bytes(rng.randrange(256) for _ in range(length)).replace(b"%", b"#") + data

    return damage


def _startxref_keyword_corrupted(_name: str, data: bytes) -> bytes:
    position = data.rfind(b"startxref")
    return data[:position] + b"startxrex" + data[position + len(b"startxref") :]


def _final_xref_section_removed(_name: str, data: bytes) -> bytes:
    offset = _final_xref_offset(data)
    if data.startswith(b"xref", offset):
        return data[:offset] + data[data.index(b"trailer", offset) :]
    return data[:offset] + data[data.index(b"endobj", offset) + len(b"endobj") :]


def _lengths_off_by_37(_name: str, data: bytes) -> bytes:
    damaged = bytearray(data)
    changed = 0
    for match in re.finditer(rb"/Length\s+(\d+)(\s+\d+\s+R)?", data):
        if match.group(2):
            continue
        digits = match.group(1)
        for candidate in (int(digits) + 37, int(digits) - 37):
            if candidate >= 0 and len(str(candidate)) == len(digits):
                damaged[match.start(1) : match.end(1)] = str(candidate).encode()
                changed += 1
                break
        if changed == 3:
            break
    return bytes(damaged)


def _bits_flipped(name: str, data: bytes) -> bytes:
    rng = random.Random(f"bitflip:{name}")  # noqa: S311
    damaged = bytearray(data)
    for _ in range(12):
        damaged[rng.randrange(1024, len(damaged))] ^= 1 << rng.randrange(8)
    return bytes(damaged)


def _endobj_removed(_name: str, data: bytes) -> bytes:
    for _ in range(2):
        position = data.index(b"endobj")
        data = data[:position] + data[position + len(b"endobj") :]
    return data


def _bytes_inserted_late(_name: str, data: bytes) -> bytes:
    start, _ = _startxref_value_span(data)
    headers = [match.start() for match in re.finditer(rb"(?m)^\d+ \d+ obj", data[:start])]
    position = headers[len(headers) * 3 // 4]
    return data[:position] + b"%" + b"x" * 16 + b"\n" + data[position:]


def _truncated_eof_trailer(_name: str, data: bytes) -> bytes:
    """Truncate the file right before the final cross-reference section."""
    offset = _final_xref_offset(data)
    # For linearized PDFs whose last startxref points near the start, cut at the last xref/trailer instead.
    if offset < len(data) // 2:
        last_xref = data.rfind(b"\nxref")
        last_trailer = data.rfind(b"trailer")
        cut = max(last_xref, last_trailer)
        if cut > len(data) // 2:
            return data[:cut]
    return data[:offset]


def _truncated_mid_trailer(_name: str, data: bytes) -> bytes:
    """Truncate inside the final trailer dictionary or xref stream dictionary."""
    offset = _final_xref_offset(data)
    # For linearized PDFs whose last startxref points near the start, cut inside the tail trailer instead.
    if offset < len(data) // 2:
        last_trailer = data.rfind(b"trailer")
        if last_trailer > len(data) // 2:
            dict_start = data.find(b"<<", last_trailer)
            if dict_start >= 0:
                return data[: min(len(data), dict_start + 14)]
    if data.startswith(b"xref", offset):
        trailer_pos = data.find(b"trailer", offset)
        if trailer_pos >= 0:
            dict_start = data.find(b"<<", trailer_pos)
            if dict_start >= 0:
                return data[: min(len(data), dict_start + 14)]
    # XRef stream: cut inside the stream dictionary or start of stream data.
    dict_start = data.find(b"<<", offset)
    if dict_start >= 0:
        return data[: min(len(data), dict_start + 20)]
    return data[: max(1, len(data) - 32)]


def _corrupt_xref_payload(_name: str, data: bytes) -> bytes:
    """Corrupt bytes inside the final xref table entries or xref stream payload."""
    offset = _final_xref_offset(data)
    damaged = bytearray(data)
    if data.startswith(b"xref", offset):
        trailer_pos = data.find(b"trailer", offset)
        span_end = trailer_pos if trailer_pos > offset else len(data)
        # Corrupt the in-use flags and offsets in the middle of the table.
        mid = offset + (span_end - offset) // 2
        for i in range(mid, min(mid + 24, span_end)):
            damaged[i] = ord("?")
        return bytes(damaged)
    stream_pos = data.find(b"stream", offset)
    if stream_pos >= 0:
        payload_start = stream_pos + 6
        while payload_start < len(damaged) and damaged[payload_start] in (10, 13):
            payload_start += 1
        for i in range(payload_start, min(payload_start + 24, len(damaged))):
            damaged[i] ^= 0xFF
        return bytes(damaged)
    return bytes(damaged)


def _shifted_offsets_start(_name: str, data: bytes) -> bytes:
    """Insert a 23-byte comment right after the PDF header line, shifting every object offset."""
    header = data.index(b"%PDF-")
    eol = data.find(b"\n", header)
    pos = eol + 1 if eol >= 0 else header + 8
    return data[:pos] + b"%shifted-by-23-bytes!\n" + data[pos:]


def _trailing_garbage_4096(name: str, data: bytes) -> bytes:
    """Append 4096 bytes of deterministic non-PDF bytes after %%EOF."""
    rng = random.Random(f"trail4096:{name}")  # noqa: S311
    junk = bytes(rng.randrange(256) for _ in range(4096))
    return data + b"\n" + junk


def _trailing_stray_object(_name: str, data: bytes) -> bytes:
    """Append an unindexed object after %%EOF with no xref/startxref."""
    return data + b"\n99999 0 obj\n<< /StrayUpdate true >>\nendobj\n"


def _wrong_startxref_offset(_name: str, data: bytes) -> bytes:
    """Replace the final startxref offset with 0."""
    start, end = _startxref_value_span(data)
    return data[:start] + b"0" + data[end:]


def _corrupt_root_ref(_name: str, data: bytes) -> bytes:
    """Point the final trailer/xref-stream /Root to a nonexistent object 999999 0 R."""
    offset = _final_xref_offset(data)
    tail = data[offset:]
    match = re.search(rb"/Root\s+\d+\s+\d+\s+R", tail)
    if match is None:
        # Fallback: replace last /Root in entire file
        matches = list(re.finditer(rb"/Root\s+\d+\s+\d+\s+R", data))
        if not matches:
            return data
        m = matches[-1]
        return data[: m.start()] + b"/Root 999999 0 R" + data[m.end() :]
    abs_start = offset + match.start()
    abs_end = offset + match.end()
    return data[:abs_start] + b"/Root 999999 0 R" + data[abs_end:]


def _broken_prev_chain(_name: str, data: bytes) -> bytes:
    """Inject or corrupt /Prev in the final trailer/xref-stream to point past EOF."""
    offset = _final_xref_offset(data)
    tail = data[offset:]
    prev_match = re.search(rb"/Prev\s+\d+", tail)
    if prev_match is not None:
        abs_start = offset + prev_match.start()
        abs_end = offset + prev_match.end()
        return data[:abs_start] + b"/Prev 99999999" + data[abs_end:]
    root_match = re.search(rb"/Root\s+\d+\s+\d+\s+R", tail)
    if root_match is not None:
        abs_end = offset + root_match.end()
        return data[:abs_end] + b" /Prev 99999999" + data[abs_end:]
    return data


CORPUS_DAMAGE: dict[str, Callable[[str, bytes], bytes]] = {
    # Historical 9 kinds from tests/corpus.py
    "truncate-95": _truncated(0.95),
    "truncate-60": _truncated(0.60),
    "junk-prefix-512": _junk_prefixed(512),
    "startxref-keyword": _startxref_keyword_corrupted,
    "xref-section-removed": _final_xref_section_removed,
    "length-off-37": _lengths_off_by_37,
    "bit-flips-12": _bits_flipped,
    "endobj-removed-2": _endobj_removed,
    "bytes-inserted-late": _bytes_inserted_late,
    # Additional 10 structural damage kinds for #1190
    "truncated-eof-trailer": _truncated_eof_trailer,
    "truncated-mid-trailer": _truncated_mid_trailer,
    "corrupt-xref-payload": _corrupt_xref_payload,
    "shifted-offsets-start": _shifted_offsets_start,
    "leading-garbage-2048": _junk_prefixed(2048),
    "trailing-garbage-4096": _trailing_garbage_4096,
    "trailing-stray-object": _trailing_stray_object,
    "wrong-startxref-offset": _wrong_startxref_offset,
    "corrupt-root-ref": _corrupt_root_ref,
    "broken-prev-chain": _broken_prev_chain,
}


# ---------------------------------------------------------------------------
# Targeted synthetic malformed PDF builders
# ---------------------------------------------------------------------------


class _PdfBuilder:
    """Minimal pure-Python PDF builder tracking exact byte offsets."""

    def __init__(self, header: bytes = b"%PDF-1.7\n") -> None:
        self.buf = bytearray(header)
        self.offsets: list[tuple[int, int, int]] = []

    def add_obj(self, num: int, body: bytes, gen: int = 0) -> int:
        pos = len(self.buf)
        self.offsets.append((num, gen, pos))
        self.buf.extend(f"{num} {gen} obj\n".encode("ascii"))
        self.buf.extend(body)
        self.buf.extend(b"\nendobj\n")
        return pos

    def add_stream(self, num: int, dict_inner: bytes, data: bytes, gen: int = 0) -> int:
        body = b"<< " + dict_inner + f" /Length {len(data)} >>\nstream\n".encode("ascii") + data + b"\nendstream"
        return self.add_obj(num, body, gen=gen)

    def add_objstm(self, num: int, members: list[tuple[int, bytes]], *, compress: bool = True) -> int:
        table = bytearray()
        payload = bytearray()
        for obj_num, obj_body in members:
            table.extend(f"{obj_num} {len(payload)} ".encode("ascii"))
            payload.extend(obj_body)
            payload.extend(b" ")
        raw = bytes(table + payload)
        if compress:
            encoded = zlib.compress(raw)
            extra = f"/Type /ObjStm /N {len(members)} /First {len(table)} /Filter /FlateDecode".encode("ascii")
            return self.add_stream(num, extra, encoded)
        extra = f"/Type /ObjStm /N {len(members)} /First {len(table)}".encode("ascii")
        return self.add_stream(num, extra, raw)

    def add_xref_table(self, trailer_inner: bytes, *, include_eof: bool = True) -> int:
        xref_pos = len(self.buf)
        max_num = max((n for n, _, _ in self.offsets), default=0)
        by_num = {n: (g, p) for n, g, p in self.offsets}
        lines = [b"xref\n", f"0 {max_num + 1}\n".encode("ascii"), b"0000000000 65535 f \n"]
        for num in range(1, max_num + 1):
            if num in by_num:
                gen, pos = by_num[num]
                lines.append(f"{pos:010d} {gen:05d} n \n".encode("ascii"))
            else:
                lines.append(b"0000000000 00000 f \n")
        lines.append(b"trailer\n<< " + trailer_inner + b" >>\n")
        lines.append(f"startxref\n{xref_pos}\n".encode("ascii"))
        if include_eof:
            lines.append(b"%%EOF\n")
        self.buf.extend(b"".join(lines))
        return xref_pos

    def add_xref_stream(
        self,
        num: int,
        rows: list[tuple[int, int, int, int]],
        trailer_inner: bytes,
        *,
        compress: bool = True,
    ) -> int:
        xref_pos = len(self.buf)
        all_rows = sorted([*rows, (num, 1, xref_pos, 0)], key=lambda r: r[0])
        raw = bytearray()
        index_parts: list[str] = []
        for obj_num, kind, f2, f3 in all_rows:
            index_parts.append(f"{obj_num} 1")
            raw.extend(struct.pack(">BIH", kind, f2, f3))
        data = zlib.compress(bytes(raw)) if compress else bytes(raw)
        filt = b" /Filter /FlateDecode" if compress else b""
        idx = " ".join(index_parts).encode("ascii")
        body = (
            b"<< /Type /XRef /W [1 4 2] /Index ["
            + idx
            + b"] "
            + trailer_inner
            + filt
            + f" /Length {len(data)} >>\nstream\n".encode("ascii")
            + data
            + b"\nendstream"
        )
        self.add_obj(num, body)
        self.buf.extend(f"startxref\n{xref_pos}\n%%EOF\n".encode("ascii"))
        return xref_pos


def _hello_content(text: str = "Hello Repair") -> bytes:
    return f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode("ascii")


def _build_base_pdf(text: str = "Hello Repair") -> tuple[_PdfBuilder, bytes]:
    b = _PdfBuilder()
    b.add_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    b.add_obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    b.add_obj(
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    )
    b.add_obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    b.add_stream(5, b"", _hello_content(text))
    b.add_xref_table(b"/Size 6 /Root 1 0 R")
    return b, bytes(b.buf)


def _rc4(key: bytes, data: bytes) -> bytes:
    """Pure-Python RC4 for building synthetic RC4-40 encrypted test PDFs."""
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


def _build_rc4_encrypted_broken_root() -> bytes:
    """Build an RC4-40 encrypted PDF (empty user password) with broken xref and /Root."""
    padding = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")
    file_id = bytes.fromhex("0123456789ABCDEF0123456789ABCDEF")
    p_val = -4  # all permissions allowed
    # Standard security handler Rev 2 (40-bit RC4)
    owner_key = hashlib.md5(padding).digest()[:5]  # noqa: S324 - PDF Standard R2 spec
    o_entry = _rc4(owner_key, padding)
    h = hashlib.md5()  # noqa: S324 - PDF Standard R2 spec
    h.update(padding)
    h.update(o_entry)
    h.update(struct.pack("<i", p_val))
    h.update(file_id)
    enc_key = h.digest()[:5]
    u_entry = _rc4(enc_key, padding)

    def obj_key(obj_num: int, gen: int = 0) -> bytes:
        mh = hashlib.md5(enc_key)  # noqa: S324 - PDF Standard R2 spec
        mh.update(struct.pack("<I", obj_num)[:3])
        mh.update(struct.pack("<H", gen)[:2])
        return mh.digest()[:10]

    content_enc = _rc4(obj_key(5), _hello_content("Hello Encrypted Repair"))
    b = _PdfBuilder()
    b.add_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    b.add_obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    b.add_obj(
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    )
    b.add_obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    b.add_stream(5, b"", content_enc)
    enc_dict = (
        b"<< /Filter /Standard /V 1 /R 2 /Length 40 "
        + f"/P {p_val} /O <{o_entry.hex().upper()}> /U <{u_entry.hex().upper()}> >>".encode("ascii")
    )
    b.add_obj(6, enc_dict)
    # Break both startxref and /Root while leaving /Encrypt and /ID in trailer.
    b.add_xref_table(
        f"/Size 7 /Root 999 0 R /Encrypt 6 0 R /ID [<{file_id.hex().upper()}><{file_id.hex().upper()}>]".encode("ascii")
    )
    return bytes(b.buf).replace(b"startxref", b"startxrex")


def targeted_cases() -> list[Case]:
    """Build the 31 targeted malformed-PDF fixtures."""
    cases: list[Case] = []

    def add(
        slug: str,
        category: str,
        data: bytes,
        *,
        expected_pages: int = 1,
        expected_substring: str = "Hello Repair",
        expect_repaired: bool = True,
    ) -> None:
        cases.append(
            Case(
                case_id=f"targeted/{slug}",
                suite="targeted",
                category=category,
                source=f"{slug}.pdf",
                data=data,
                expected_pages=expected_pages,
                expect_text=True,
                expected_substring=expected_substring,
                expect_repaired=expect_repaired,
            )
        )

    # 1. Missing %%EOF
    b1, _ = _build_base_pdf()
    add("missing-eof", "truncated-eof-trailer", bytes(b1.buf).removesuffix(b"%%EOF\n"), expect_repaired=False)

    # 2. Truncated mid-trailer
    _, base = _build_base_pdf()
    add("truncated-mid-trailer", "truncated-mid-trailer", base[: base.index(b"/Root") + 4])

    # 3. Corrupt classic xref table entries
    add(
        "corrupt-xref-table-entries",
        "corrupt-xref-payload",
        base.replace(b"00000 n ", b"00000 x ", 3),
    )

    # 4. Corrupt FlateDecode xref stream payload with intact ObjStm
    b4 = _PdfBuilder(b"%PDF-1.5\n")
    p_cat = b4.add_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    p_stm = b4.add_objstm(
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
    p_cnt = b4.add_stream(5, b"", _hello_content())
    b4.add_xref_stream(
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
    raw4 = bytearray(b4.buf)
    x7 = raw4.index(b"7 0 obj")
    st7 = raw4.index(b"stream\n", x7) + 7
    for i in range(st7, min(st7 + 12, len(raw4))):
        raw4[i] ^= 0xFF
    add("corrupt-xref-stream-flate", "corrupt-xref-payload", bytes(raw4))

    # 5. XRef stream with invalid /W [0 0 0]
    add(
        "corrupt-xref-stream-widths",
        "corrupt-xref-payload",
        bytes(b4.buf).replace(b"/W [1 4 2]", b"/W [0 0 0]"),
    )

    # 6. Shifted offsets (+15 bytes comment before obj 1)
    add(
        "shifted-offsets-classic",
        "shifted-offsets",
        base.replace(b"%PDF-1.7\n", b"%PDF-1.7\n%15bytes-shift\n", 1),
    )

    # 7. Leading garbage beyond 1024 bytes (2048 bytes)
    add("leading-garbage-2048", "leading-garbage", (b"JUNK" * 512) + base)

    # 8. Trailing garbage (4096 bytes after %%EOF)
    add("trailing-garbage-4096", "trailing-garbage", base + b"\n" + (b"TAIL" * 1024), expect_repaired=False)

    # 9. Trailing updated page content appended after %%EOF with no xref/startxref
    b9 = _PdfBuilder()
    b9.add_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    b9.add_obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    b9.add_obj(
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    )
    b9.add_obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    b9.add_stream(5, b"", _hello_content("Old Revision"))
    b9.add_xref_table(b"/Size 6 /Root 1 0 R")
    b9.add_stream(5, b"", _hello_content("Hello Repair"))
    add("trailing-update-no-startxref", "trailing-garbage", bytes(b9.buf))

    # 10. Missing startxref completely
    add("missing-startxref", "missing-startxref", base[: base.index(b"startxref")])

    # 11. Broken /Prev pointing past EOF
    add("broken-prev-dangling", "broken-prev-chain", base.replace(b"/Root 1 0 R", b"/Root 1 0 R /Prev 999999"))

    # 12. Cyclic /Prev pointing to own xref offset
    xref_off = _final_xref_offset(base)
    add(
        "broken-prev-cycle",
        "broken-prev-chain",
        base.replace(b"/Root 1 0 R", f"/Root 1 0 R /Prev {xref_off}".encode("ascii")),
    )

    # 13. Two-revision PDF with corrupt revision-0 xref table
    b13 = _PdfBuilder()
    b13.add_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    b13.add_obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    b13.add_obj(
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    )
    b13.add_obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    b13.add_stream(5, b"", _hello_content("Old Text"))
    rev0_xref = b13.add_xref_table(b"/Size 6 /Root 1 0 R")
    p5_new = b13.add_stream(5, b"", _hello_content("Hello Repair"))
    rev1_xref = len(b13.buf)
    b13.buf.extend(
        f"xref\n5 1\n{p5_new:010d} 00000 n \n"
        f"trailer\n<< /Size 6 /Root 1 0 R /Prev {rev0_xref} >>\n"
        f"startxref\n{rev1_xref}\n%%EOF\n".encode("ascii")
    )
    raw13 = bytearray(b13.buf)
    raw13[rev0_xref : rev0_xref + 4] = b"XXXX"
    add("broken-prev-corrupt-rev0", "broken-prev-chain", bytes(raw13))

    # 14. Missing /Root in trailer, intact Catalog in body
    add("missing-root-catalog-intact", "broken-root", base.replace(b"/Root 1 0 R", b""))

    # 15. Wrong /Root in xref stream while real Catalog is inside an ObjStm
    b15 = _PdfBuilder(b"%PDF-1.5\n")
    p_stm15 = b15.add_objstm(
        6,
        [
            (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
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
    p_cnt15 = b15.add_stream(5, b"", _hello_content())
    b15.add_xref_stream(
        7,
        [
            (0, 0, 0, 65535),
            (1, 2, 6, 0),
            (2, 2, 6, 1),
            (3, 2, 6, 2),
            (4, 2, 6, 3),
            (5, 1, p_cnt15, 0),
            (6, 1, p_stm15, 0),
        ],
        b"/Size 8 /Root 999 0 R",
    )
    add("wrong-root-catalog-in-objstm", "broken-root", bytes(b15.buf))

    # 16. Catalog without /Type /Catalog (only /Pages 2 0 R), trailer missing
    b16 = _PdfBuilder()
    b16.add_obj(1, b"<< /Pages 2 0 R >>")
    b16.add_obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    b16.add_obj(
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    )
    b16.add_obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    b16.add_stream(5, b"", _hello_content())
    add("catalog-without-type-key", "broken-root", bytes(b16.buf))

    # 17. Unreferenced pages with broken /Root (NO Catalog object at all, /Type /Pages + /Type /Page intact)
    b17 = _PdfBuilder()
    b17.add_obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    b17.add_obj(
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    )
    b17.add_obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    b17.add_stream(5, b"", _hello_content())
    b17.add_xref_table(b"/Size 6 /Root 999 0 R")
    add("unreferenced-pages-broken-root", "broken-root-unreferenced-pages", bytes(b17.buf))

    # 18. Orphan /Type /Page objects with NO Catalog and NO /Type /Pages node
    b18 = _PdfBuilder()
    b18.add_obj(
        3,
        b"<< /Type /Page /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    )
    b18.add_obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    b18.add_stream(5, b"", _hello_content())
    b18.add_xref_table(b"/Size 6 /Root 999 0 R")
    add("orphan-pages-no-catalog-no-pages", "broken-root-unreferenced-pages", bytes(b18.buf))

    # 19. Duplicate overlapping generations (3 0 obj old, 3 1 obj new, /Kids [3 1 R], xref broken)
    b19 = _PdfBuilder()
    b19.add_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    b19.add_obj(2, b"<< /Type /Pages /Kids [3 1 R] /Count 1 >>")
    b19.add_obj(
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 6 0 R >>",
        gen=0,
    )
    b19.add_obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    b19.add_stream(5, b"", _hello_content("Hello Repair"))
    b19.add_stream(6, b"", _hello_content("Stale Gen 0"))
    b19.add_obj(
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        gen=1,
    )
    b19.add_xref_table(b"/Size 7 /Root 1 0 R")
    add("duplicate-generations", "duplicate-generations", bytes(b19.buf).replace(b"startxref", b"startxrex"))

    # 20. Duplicate object where later copy is broken/truncated
    b20, _ = _build_base_pdf()
    raw20 = bytes(b20.buf).replace(b"startxref", b"startxrex") + b"\n3 0 obj\n<< /Type /Page /Broken\n"
    add("duplicate-object-broken-later", "duplicate-generations", raw20)

    # 21. Object stream with mismatched index in xref stream (3 is at index 0 in ObjStm, xref says index 1)
    b21 = _PdfBuilder(b"%PDF-1.5\n")
    p_cat21 = b21.add_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    p_stm21 = b21.add_objstm(
        6,
        [
            (
                3,
                (
                    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
                ),
            ),
            (2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
            (4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
        ],
    )
    p_cnt21 = b21.add_stream(5, b"", _hello_content())
    b21.add_xref_stream(
        7,
        [
            (0, 0, 0, 65535),
            (1, 1, p_cat21, 0),
            (2, 2, 6, 0),  # swapped with 3!
            (3, 2, 6, 1),  # swapped with 2!
            (4, 2, 6, 2),
            (5, 1, p_cnt21, 0),
            (6, 1, p_stm21, 0),
        ],
        b"/Size 8 /Root 1 0 R",
    )
    add("objstm-mismatched-index", "damaged-objstm", bytes(b21.buf))

    # 22. Object stream with /N larger than actual pairs in table (plus broken xref)
    b22 = _PdfBuilder(b"%PDF-1.5\n")
    b22.add_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    obj2_body = b"<< /Type /Pages /Kids [3 0 R] /Count 1 >> "
    obj3_body = (
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> "
    )
    obj4_body = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> "
    table22 = f"2 0 3 {len(obj2_body)} 4 {len(obj2_body) + len(obj3_body)} ".encode("ascii")
    raw_stm22 = table22 + obj2_body + obj3_body + obj4_body
    # Declare /N 5 even though only 3 valid pairs precede /First!
    b22.add_stream(
        6,
        f"/Type /ObjStm /N 5 /First {len(table22)}".encode("ascii"),
        raw_stm22,
    )
    b22.add_stream(5, b"", _hello_content())
    add("objstm-corrupt-n-too-large", "damaged-objstm", bytes(b22.buf))

    # 23. Missing /Size in classic trailer
    add("missing-size-classic", "missing-size", base.replace(b"/Size 6 ", b""), expect_repaired=False)

    # 24. Missing %PDF- header completely
    add("missing-pdf-header", "missing-header", base.removeprefix(b"%PDF-1.7\n"))

    # 25. Bare CR (\r) line endings throughout
    add("bare-cr-line-endings", "line-endings", base.replace(b"\n", b"\r"), expect_repaired=False)

    # 26. Classic xref subsection off-by-one (declares 0 5 instead of 1 5, omitting free obj 0)
    b26 = _PdfBuilder()
    p1 = b26.add_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    p2 = b26.add_obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    p3 = b26.add_obj(
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    )
    p4 = b26.add_obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    p5 = b26.add_stream(5, b"", _hello_content())
    x26 = len(b26.buf)
    b26.buf.extend(
        (
            f"xref\n0 5\n"
            f"{p1:010d} 00000 n \n"
            f"{p2:010d} 00000 n \n"
            f"{p3:010d} 00000 n \n"
            f"{p4:010d} 00000 n \n"
            f"{p5:010d} 00000 n \n"
            f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{x26}\n%%EOF\n"
        ).encode("ascii")
    )
    add("xref-subsection-off-by-one", "corrupt-xref-payload", bytes(b26.buf))

    # 27. Wrong stream /Length + damaged xref
    wrong_len = re.sub(rb"/Length \d+", b"/Length 9999", base)
    add("wrong-stream-length", "corrupt-stream-length", wrong_len.replace(b"startxref", b"startxrex"))

    # 28. Spurious live object 0 in body + broken xref
    b28 = _PdfBuilder()
    b28.add_obj(0, b"(spurious object zero)")
    b28.add_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    b28.add_obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    b28.add_obj(
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    )
    b28.add_obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    b28.add_stream(5, b"", _hello_content())
    add("object-zero-in-body", "object-zero", bytes(b28.buf))

    # 29. Compact objects without endobj and without whitespace before <<
    content29 = _hello_content()
    raw29 = (
        b"%PDF-1.7\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>"
        b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"
        + f"5 0 obj<</Length {len(content29)}>>stream\n".encode("ascii")
        + content29
        + b"\nendstream\n"
    )
    add("compact-obj-no-endobj", "missing-endobj", raw29)

    # 30. Wrong generation in trailer /Root (trailer says 1 0 R, body has 1 1 obj)
    add(
        "wrong-generation-in-trailer-root",
        "duplicate-generations",
        base.replace(b"1 0 obj", b"1 1 obj", 1),
    )

    # 31. Encrypted (empty user pw, RC4-40) with broken xref AND broken /Root (retains /Encrypt & /ID)
    add(
        "encrypted-empty-pw-broken-root",
        "encrypted-broken-root",
        _build_rc4_encrypted_broken_root(),
        expected_substring="Hello Encrypted Repair",
    )

    return cases


def all_cases() -> list[Case]:
    """Return all deterministic benchmark cases: corpus-derived plus targeted fixtures."""
    out: list[Case] = []
    for kind, generator in CORPUS_DAMAGE.items():
        for source in CORPUS_SOURCES:
            name = source.name
            raw = source.read_bytes()
            damaged = generator(name, raw)
            out.append(
                Case(
                    case_id=f"corpus/{kind}/{name}",
                    suite="corpus",
                    category=kind,
                    source=name,
                    data=damaged,
                    expected_pages=CORPUS_PAGE_COUNTS[name],
                    expect_text=name not in NO_TEXT_CORPUS,
                )
            )
    out.extend(targeted_cases())
    return out
