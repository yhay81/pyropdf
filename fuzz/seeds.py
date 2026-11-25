# ruff: noqa: INP001 - a script, not a package.
"""Write the fuzz targets' seeds: the corpus and its damaged inputs, and their objects for ``value``.

Run from the repository root: ``python3 -I fuzz/seeds.py``. The seeds go to
fuzz/corpus/<target>/, which git ignores; tests/corpus.py keeps the damage's
one definition.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from corpus import DAMAGE, NAMES, damaged  # noqa: E402

ASSETS = ROOT / "tests" / "assets"
#: The subset faces of the engine's font tests, as seeds of ``fonts``, and its
#: colour faces, as seeds of ``bitmaps`` and ``colr`` too.
FONTS = ROOT / "crates" / "pyropdf-engine" / "src" / "fonts" / "testdata"
TARGETS = ("index", "save", "bridge", "interpret")
#: The values of uncompressed objects, as the ``value`` target reads them.
OBJECT = re.compile(rb"\d+\s+\d+\s+obj\b(.*?)\bendobj", re.DOTALL)
MAX_VALUE_SEED = 4096
#: The data of streams, and an operator that marks one as content, for ``content`` and ``interpret``.
STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)
CONTENT_OPERATOR = re.compile(rb"(?<![^\s\]\)>])(?:BT|cm|re|Do)\s")
MAX_CONTENT_SEED = 65536
#: Hex strings in encrypted/integrity fixtures (CMS envelopes and MAC tokens).
HEX_STRING = re.compile(rb"<([0-9A-Fa-f\s]{64,32768})>")
MAX_CMAP_SEED = 65536
MAX_TYPE1_SEED = 262144
T1_SMALL_INT = 107
HISTORY_FORMAT_2 = 2
HISTORY_KIND_MIXED = 3


def write(target: str, data: bytes) -> None:
    """Write one seed of ``target``, named by its hash."""
    directory = ROOT / "fuzz" / "corpus" / target
    directory.mkdir(parents=True, exist_ok=True)
    (directory / hashlib.sha256(data).hexdigest()[:16]).write_bytes(data)


def _t1_encrypt(data: bytes, seed: int) -> bytes:
    """Apply Adobe Type 1 eexec / charstring encryption."""
    out = bytearray()
    for byte in data:
        cipher = byte ^ (seed >> 8)
        out.append(cipher)
        seed = ((cipher + seed) * 52845 + 22719) & 0xFFFF
    return bytes(out)


def _t1_int(value: int) -> bytes:
    """Encode a small Type 1 charstring integer."""
    if -T1_SMALL_INT <= value <= T1_SMALL_INT:
        return bytes([value + 139])
    mag = abs(value) - 108
    return bytes([(247 if value > 0 else 251) + mag // 256, mag % 256])


def _t1_rect(left: int, width: int) -> bytes:
    """Build a minimal Type 1 rectangle charstring."""
    out = bytearray(_t1_int(0) + _t1_int(600) + b"\x0d")
    out.extend(_t1_int(left) + _t1_int(0) + b"\x15")
    for dx, dy in ((width, 0), (0, 600), (-width, 0)):
        out.extend(_t1_int(dx) + _t1_int(dy) + b"\x05")
    return bytes(out) + b"\x09\x0e"


def _synthetic_type1(*, iv: int, composite: bool, pfb: bool) -> bytes:
    """Build a valid synthetic Type 1 font (PDF stream or PFB-framed)."""
    header = (
        b"%!FontType1-1.0: Fuzz-Type1 1.0\n"
        b"10 dict begin\n"
        b"/FontName /Fuzz-Type1 def /FontType 1 def /PaintType 0 def\n"
        b"/FontMatrix [.001 0 0 .001 0 0] def /FontBBox [0 0 600 600] def\n"
        b"/Encoding StandardEncoding def\n"
        b"currentdict end\n"
        b"currentfile eexec\n"
    )
    glyphs: dict[str, bytes] = {
        ".notdef": _t1_int(0) + _t1_int(600) + b"\x0d\x0e",
        "A": _t1_rect(25, 150),
        "B": _t1_rect(300, 200),
        "C": _t1_rect(180, 240),
        "D": _t1_rect(50, 330),
    }
    if composite:
        glyphs["C"] = (
            _t1_int(0) + _t1_int(600) + b"\x0d" + b"".join(_t1_int(n) for n in (0, 150, 50, 65, 66)) + b"\x0c\x06"
        )
    metrics = _t1_int(0) + _t1_int(600) + b"\x0d"
    subr = glyphs["A"][len(metrics) : -1] + b"\x0b"
    glyphs["A"] = metrics + _t1_int(0) + b"\x0a\x0e"
    if iv >= 0:
        subr = _t1_encrypt(bytes(iv) + subr, 4330)
        glyphs = {name: _t1_encrypt(bytes(iv) + outline, 4330) for name, outline in glyphs.items()}
    private = bytearray(
        b"dup /Private 9 dict dup begin\n"
        b"/RD {string currentfile exch readstring pop} executeonly def\n"
        b"/ND {noaccess def} executeonly def\n"
        + f"/lenIV {iv} def\n".encode()
        + b"/BlueValues [] def /MinFeature {16 16} def /password 5839 def\n"
        b"/NP {noaccess put} executeonly def\n"
        + f"/Subrs 1 array dup 0 {len(subr)} RD ".encode()
        + subr
        + b" NP def\n2 index /CharStrings 8 dict dup begin\n"
    )
    for name, outline in glyphs.items():
        private.extend(f"/{name} {len(outline)} RD ".encode() + outline + b" ND\n")
    private.extend(
        b"end end readonly put noaccess put\ndup /FontName get exch definefont pop\nmark currentfile closefile\n"
    )
    encrypted = _t1_encrypt(b"abcd" + bytes(private), 55665)
    tail = b"0" * 512 + b"\ncleartomark\n"
    if pfb:
        return (
            b"\x80\x01"
            + struct.pack("<I", len(header))
            + header
            + b"\x80\x02"
            + struct.pack("<I", len(encrypted))
            + encrypted
            + b"\x80\x01"
            + struct.pack("<I", len(tail))
            + tail
            + b"\x80\x03"
        )
    return header + encrypted + tail


def _history_seed(fmt: int, *, include_mixed: bool = False) -> bytes:
    """Build a deterministic Format 1 or Format 2 saved-history seed."""
    buf = bytearray(b"PYROPDF-HISTORY\n")
    buf.extend(struct.pack("<I", fmt))
    buf.extend(b"\x00" * 32)  # Fingerprint placeholder; target patches to base fingerprint.
    buf.extend(struct.pack("<II", 1, 2))  # position=1, versions=2
    # Version 0: empty diff from opened base.
    buf.extend(struct.pack("<IQBII", 0, 6, 0, 0, 0))
    # Version 1: label="edit", one written object (1 0), one stale mark (Appearance on 3).
    label = b"edit"
    buf.extend(struct.pack("<I", len(label)) + label)
    buf.extend(struct.pack("<Q", 7))
    buf.append(0)  # no trailer change
    buf.extend(struct.pack("<I", 1))  # 1 changed object
    obj_bytes = (
        b"<< /Type /Catalog /Pages 2 0 R /StructTreeRoot 4 0 R /Names << /Dests << /Names [(a) [3 0 R /Fit]] >> >> >>"
    )
    buf.extend(struct.pack("<I", 1))
    if fmt >= HISTORY_FORMAT_2 and include_mixed:
        kind = HISTORY_KIND_MIXED
    elif fmt >= HISTORY_FORMAT_2:
        kind = HISTORY_FORMAT_2
    else:
        kind = 0
    buf.append(kind)
    buf.extend(struct.pack("<HI", 0, len(obj_bytes)) + obj_bytes)
    if kind == HISTORY_KIND_MIXED:
        flags = b"\x00"
        buf.extend(struct.pack("<I", len(flags)) + flags)
    buf.extend(struct.pack("<IBI", 1, 0, 3))  # 1 stale mark: Appearance (0) on obj 3
    buf.extend(hashlib.sha256(buf).digest())
    return bytes(buf)


def _write_encryption_seeds() -> None:
    """Write encrypted PDFs, recipient/integrity fixtures, /Encrypt dicts, and CMS/MAC tokens."""
    for path in sorted((ASSETS / "encrypted").rglob("*.pdf")):
        raw = path.read_bytes()
        write("encryption", raw)
        for match in HEX_STRING.finditer(raw):
            cleaned = re.sub(rb"\s+", b"", match.group(1))
            if len(cleaned) % 2 == 0:
                with contextlib.suppress(ValueError):
                    token = bytes.fromhex(cleaned.decode("ascii"))
                    if token:
                        write("encryption", token)
    for name in ("pdf20-simple.pdf",):
        write("encryption", (ASSETS / "real_world" / name).read_bytes())
    for seed in (
        b"<< /Filter /Standard /V 4 /R 4 /Length 128 /P -4 /O <"
        + b"11" * 32
        + b"> /U <"
        + b"22" * 32
        + b"> /StmF /StdCF /StrF /StdCF /CF << /StdCF << /CFM /AESV2 /Length 16 >> >> >>",
        b"<< /Filter /Standard /V 6 /R 7 /Length 256 /P -4 /O <"
        + b"11" * 48
        + b"> /U <"
        + b"22" * 48
        + b"> /OE <"
        + b"33" * 32
        + b"> /UE <"
        + b"44" * 32
        + b"> /Perms <"
        + b"55" * 16
        + b"> /StmF /StdCF /StrF /StdCF /CF << /StdCF << /CFM /AESV4 /Length 32 >> >> >>",
        (
            b"<< /Filter /Adobe.PubSec /SubFilter /adbe.pkcs7.s5 /V 6 /StmF /DefaultCryptFilter "
            b"/StrF /DefaultCryptFilter /CF << /DefaultCryptFilter << /CFM /AESV4 /Length 256 "
            b"/AuthEvent /DocOpen /Recipients [<3003020101>] >> >> >>"
        ),
        b"<< /Filter /Adobe.PubSec /SubFilter /adbe.pkcs7.s4 /V 2 /Length 128 /Recipients [<3003020102>] >>",
        # RFC 2268 RC2-CBC test vector (key_len=8, key=8*0, iv=7*0+1, ciphertext).
        bytes([8]) + bytes(8) + bytes(7) + b"\x01" + bytes.fromhex("ebb773f993278eff") + bytes([63]),
        b"\x05secretpayload0123456789abcdef",
    ):
        write("encryption", seed)


def _write_forms_markup_seeds() -> None:
    """Write FDF, XFDF, XHTML rich-text, and AcroForm PDF seeds."""
    for name in ("f1040.pdf", "pdfium-links-highlights-annots.pdf"):
        write("forms_markup", (ASSETS / "real_world" / name).read_bytes())
    fdf_seeds = (
        (
            b"%FDF-1.2\n1 0 obj\n<< /FDF << /F (form.pdf) /ID [<01020304> <05060708>] "
            b"/Fields [<< /T (person) /Kids [<< /T (first) /V (Ada) >> << /T (tags) /V [(alpha) (beta)] >>] >> "
            b'<< /T (rich) /RV (<body xmlns="http://www.w3.org/1999/xhtml"><p>Hi</p></body>) >>] >> >>\nendobj\n'
            b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
        ),
        (
            b"%FDF-1.2\n1 0 obj\n<< /FDF << /F << /UF (target.pdf) >> "
            b"/Fields [<< /T (notes) /V 2 0 R >>] >> >>\nendobj\n"
            b"2 0 obj\n<< /Length 11 >>\nstream\nStream text\nendstream\nendobj\n"
            b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
        ),
    )
    xfdf_seeds = (
        (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<xfdf xmlns="http://ns.adobe.com/xfdf/" xml:space="preserve">'
            b'<f href="form.pdf"/><ids original="01020304" modified="05060708"/>'
            b'<fields><field name="person"><field name="first"><value>Ada</value></field>'
            b'<field name="rich"><value-richtext><body xmlns="http://www.w3.org/1999/xhtml">'
            b'<p style="text-align:center"><b>Hello</b> <i>World</i></p></body></value-richtext></field>'
            b"</field></fields></xfdf>"
        ),
        b'<xfdf><fields><field name="tags"><value>alpha</value><value>beta</value></field></fields></xfdf>',
    )
    xhtml_seeds = (
        b'<body xmlns="http://www.w3.org/1999/xhtml"><p>Hello <b>World</b></p></body>',
        (
            b'<h:body xmlns:h="http://www.w3.org/1999/xhtml">'
            b'<h:p style="text-align:center;font:italic bold 12pt Times,serif;color:#112233">'
            b'<h:b>X<h:span style="font-stretch:condensed;text-decoration:underline;vertical-align:2pt">'
            b"Y&amp;&#x1f600;</h:span></h:b>"
            b"<h:br/><h:sup>2</h:sup><h:sub>n</h:sub></h:p>"
            b'<h:p style="color:rgb(10, 20, 30)">\xd7\xa9\xd7\x9c\xd7\x95\xd7\x9d ABC</h:p></h:body>'
        ),
        (
            b'<body xmlns="http://www.w3.org/1999/xhtml"><p>Styled</p></body>'
            b"\x00font-size:10pt;font-family:Courier;color:#ff0000"
        ),
        "\ufeff<body xmlns='http://www.w3.org/1999/xhtml'><p>UTF-16BE</p></body>".encode("utf-16-be"),
        "\ufeff<body xmlns='http://www.w3.org/1999/xhtml'><p>UTF-16LE</p></body>".encode("utf-16-le"),
    )
    for seed in (*fdf_seeds, *xfdf_seeds, *xhtml_seeds):
        write("forms_markup", seed)


def _write_structure_history_seeds() -> None:
    """Write tagged PDFs (PDF/UA, PDF/A, logical reading order) and history binary seeds."""
    for folder in ("tagged-reading", "pdfua", "pdfa", "profile-sources"):
        directory = ASSETS / folder
        if directory.is_dir():
            for path in sorted(directory.glob("*.pdf")):
                write("structure_history", path.read_bytes())
    write("structure_history", (ASSETS / "real_world" / "pdf20-simple.pdf").read_bytes())
    write("structure_history", _history_seed(1))
    write("structure_history", _history_seed(2, include_mixed=False))
    write("structure_history", _history_seed(2, include_mixed=True))


def _write_type1_cmap_seeds(files: list[bytes]) -> None:
    """Write real & synthetic Type 1 programs and real & synthetic CMap streams."""
    for iv in (-1, 4):
        write("type1_cmap", _synthetic_type1(iv=iv, composite=False, pfb=False))
        write("type1_cmap", _synthetic_type1(iv=iv, composite=True, pfb=False))
    write("type1_cmap", _synthetic_type1(iv=4, composite=True, pfb=True))

    cmap_seeds = (
        (
            b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            b"/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
            b"1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
            b"2 beginbfchar\n<0001> <0041>\n<0002> <D83DDE00>\nendbfchar\n"
            b"1 beginbfrange\n<0010> <0012> <0061>\nendbfrange\n"
            b"endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
        ),
        (
            b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> def\n"
            b"/CMapName /Fuzz-CID def\n/CMapType 1 def\n/WMode 0 def\n"
            b"2 begincodespacerange\n<00> <7F>\n<8000> <FFFF>\nendcodespacerange\n"
            b"2 begincidchar\n<41> 1\n<42> 2\nendcidchar\n"
            b"1 begincidrange\n<8000> <8010> 10\nendcidrange\n"
            b"1 beginnotdefrange\n<00> <1F> 0\nendnotdefrange\n"
            b"endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
        ),
    )
    for seed in cmap_seeds:
        write("type1_cmap", seed)

    extra_pdfs = [path.read_bytes() for path in sorted((ASSETS / "text").glob("*.pdf"))]
    for data in (*files, *extra_pdfs):
        for match in STREAM.finditer(data):
            stream = match.group(1)
            with contextlib.suppress(zlib.error):
                stream = zlib.decompress(stream)
            is_cmap = len(stream) <= MAX_CMAP_SEED and b"begincmap" in stream
            is_type1 = (
                len(stream) <= MAX_TYPE1_SEED
                and b"eexec" in stream
                and (b"%!FontType1" in stream[:256] or b"%!PS-AdobeFont" in stream[:256])
            )
            if is_cmap or is_type1:
                write("type1_cmap", stream)


def main() -> None:
    """Write every seed."""
    files = [(ASSETS / "real_world" / name).read_bytes() for name in NAMES]
    files += [path.read_bytes() for path in sorted((ASSETS / "encrypted").glob("*.pdf"))]
    files += [damaged(name, kind) for name in NAMES for kind in DAMAGE]
    # Save-fuzz crash 48ddebe9596f7ddd421a47eb48cc7105a4d0f49c, minimized:
    # repair finds a live object zero that a save's free-list head cannot retain.
    files.append(b"%PDF-1.7\n0 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n")
    for data in files:
        for target in TARGETS:
            write(target, data)
        for match in OBJECT.finditer(data):
            value = match.group(1).strip()
            if value and len(value) <= MAX_VALUE_SEED and b"stream" not in value:
                write("value", value)
        for match in STREAM.finditer(data):
            content = match.group(1)
            with contextlib.suppress(zlib.error):
                content = zlib.decompress(content)
            if len(content) <= MAX_CONTENT_SEED and CONTENT_OPERATOR.search(content):
                write("content", content)
                write("interpret", content)
    for path in sorted(FONTS.glob("*.[ot]tf")):
        write("fonts", path.read_bytes())
        if path.name.startswith("colour-"):
            write("bitmaps", path.read_bytes())
            write("colr", path.read_bytes())

    _write_encryption_seeds()
    _write_forms_markup_seeds()
    _write_structure_history_seeds()
    _write_type1_cmap_seeds(files)


if __name__ == "__main__":
    main()
