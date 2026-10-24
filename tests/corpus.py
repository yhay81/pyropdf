"""The real-document corpus, and the damaged inputs that pylopdf derives from it.

The corpus is pylopdf's tests/assets/real_world at 884af0db, and its README
records the source and license of each file. DAMAGE holds the damage
generators of pylopdf's tests/test_real_world.py at that commit, with the same
behavior: each takes a file's name and bytes and returns the damaged bytes.
The seeded kinds seed Python's random module with the file's name, so every
run derives the same inputs, and test_corpus_records.py compares each one with
pylopdf's by hash. Python keeps only random.random() the same across versions,
not randrange, so that comparison runs on every Python that CI runs.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets" / "real_world"

# The names of the corpus files, sorted.
NAMES = tuple(sorted(path.name for path in ASSETS.glob("*.pdf")))

# A kind of damage, which takes a file's name and bytes and returns the damaged bytes.
Damage = Callable[[str, bytes], bytes]


def _startxref_value_span(data: bytes) -> tuple[int, int]:
    """Return the byte span of the final startxref value."""
    eof = data.rfind(b"%%EOF")
    startxref = data.rfind(b"startxref", 0, eof)
    assert startxref >= 0
    number_start = startxref + len(b"startxref")
    while data[number_start] in b"\x00\t\n\x0c\r ":
        number_start += 1
    number_end = number_start
    while data[number_end : number_end + 1].isdigit():
        number_end += 1
    assert number_end > number_start
    return number_start, number_end


def _truncated(fraction: float) -> Damage:
    """Return damage that keeps the first `fraction` of the bytes."""

    def damage(_name: str, data: bytes) -> bytes:
        return data[: int(len(data) * fraction)]

    return damage


def _junk_prefixed(name: str, data: bytes) -> bytes:
    """Prepend 512 seeded bytes that cannot form a PDF header."""
    rng = random.Random(f"junk:{name}")  # noqa: S311 - deterministic fixture damage
    return bytes(rng.randrange(256) for _ in range(512)).replace(b"%", b"#") + data


def _startxref_keyword_corrupted(_name: str, data: bytes) -> bytes:
    """Misspell the last startxref keyword without moving bytes."""
    position = data.rfind(b"startxref")
    return data[:position] + b"startxrex" + data[position + len(b"startxref") :]


def _final_xref_section_removed(_name: str, data: bytes) -> bytes:
    """Delete the final classic table up to its trailer, or the final xref stream."""
    start, end = _startxref_value_span(data)
    offset = data.index(b"%PDF-") + int(data[start:end])
    if data.startswith(b"xref", offset):
        return data[:offset] + data[data.index(b"trailer", offset) :]
    return data[:offset] + data[data.index(b"endobj", offset) + len(b"endobj") :]


def _lengths_off_by_37(_name: str, data: bytes) -> bytes:
    """Change the first three direct stream lengths by 37 without moving bytes."""
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
    """Flip 12 seeded bits after the first KiB."""
    rng = random.Random(f"bitflip:{name}")  # noqa: S311 - deterministic fixture damage
    damaged = bytearray(data)
    for _ in range(12):
        damaged[rng.randrange(1024, len(damaged))] ^= 1 << rng.randrange(8)
    return bytes(damaged)


def _endobj_removed(_name: str, data: bytes) -> bytes:
    """Delete the first two endobj keywords, shifting every later offset."""
    for _ in range(2):
        position = data.index(b"endobj")
        data = data[:position] + data[position + len(b"endobj") :]
    return data


def _bytes_inserted_late(_name: str, data: bytes) -> bytes:
    """Insert an 18-byte comment line before the last quarter of the object headers."""
    start, _ = _startxref_value_span(data)
    headers = [match.start() for match in re.finditer(rb"(?m)^\d+ \d+ obj", data[:start])]
    position = headers[len(headers) * 3 // 4]
    return data[:position] + b"%" + b"x" * 16 + b"\n" + data[position:]


# Each kind of damage, by the name that pylopdf gives it. Every kind applies to
# every corpus file. length-off-37 leaves nics-background-checks-2015-11.pdf
# unchanged, since each stream length there is an indirect reference.
DAMAGE: dict[str, Damage] = {
    "truncate-95": _truncated(0.95),
    "truncate-60": _truncated(0.60),
    "junk-prefix-512": _junk_prefixed,
    "startxref-keyword": _startxref_keyword_corrupted,
    "xref-section-removed": _final_xref_section_removed,
    "length-off-37": _lengths_off_by_37,
    "bit-flips-12": _bits_flipped,
    "endobj-removed-2": _endobj_removed,
    "bytes-inserted-late": _bytes_inserted_late,
}

# The 8 kinds of the measurements that DESIGN.md section 2.2 cites, whose 112
# inputs are the 14 files under each of them. yhay81/pylopdf#347 added
# bytes-inserted-late and measured over the same 8 kinds as before.
MEASURED = tuple(kind for kind in DAMAGE if kind != "bytes-inserted-late")


def damaged(name: str, kind: str) -> bytes:
    """Return a corpus file under one kind of damage."""
    return DAMAGE[kind](name, (ASSETS / name).read_bytes())
