"""Unit tests for ``tools/codemod.py`` (PyMuPDF and pylopdf 0.15 migration codemod)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pyropdf

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("codemod", ROOT / "tools" / "codemod.py")
assert SPEC is not None
assert SPEC.loader is not None
codemod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = codemod
SPEC.loader.exec_module(codemod)


def test_pymupdf_open_pages_text_and_save_mechanical_rewrites() -> None:
    source = """\
import fitz

# Keep this comment intact.
with fitz.open("input.pdf") as doc:
    total = len(doc)
    first = doc[0]
    page = doc.load_page(1)
    idx = page.number
    text = page.get_text()
    hits = page.search_for("Invoice")
    pix = page.get_pixmap(dpi=144, annots=False)
    for p in doc:
        _ = p.get_label()
    doc.save("out.pdf", deflate=True, linear=True, use_objstms=True)
    raw = doc.tobytes(deflate=True)
"""
    result = codemod.transform_source(source, mode="pymupdf")
    assert result.changed
    assert result.diagnostics == ()
    assert "import pyropdf" in result.source
    assert "# Keep this comment intact." in result.source
    assert 'with pyropdf.open("input.pdf") as doc:' in result.source
    assert "total = len(doc.pages)" in result.source
    assert "first = doc.pages[0]" in result.source
    assert "page = doc.pages[1]" in result.source
    assert "idx = page.index" in result.source
    assert "text = page.text()" in result.source
    assert 'hits = page.search("Invoice")' in result.source
    assert "pix = page.pixmap(dpi=144, annotations=False)" in result.source
    assert "for p in doc.pages:" in result.source
    assert "_ = p.label" in result.source
    assert 'doc.save("out.pdf", compress=True, linearize=True, object_streams=True)' in result.source
    assert "raw = doc.to_bytes(compress=True)" in result.source


def test_pymupdf_empty_doc_stream_open_attachments_annotations_and_metadata() -> None:
    source = """\
import pymupdf

blank = pymupdf.open()
from_mem = pymupdf.open(stream=b"%PDF-1.7", filetype="pdf")
names = from_mem.embfile_names()
count = from_mem.embfile_count()
payload = from_mem.embfile_get("a.txt")
from_mem.embfile_del("a.txt")
from_mem.embfile_add("b.txt", b"hi")
from_mem.set_xml_metadata("<x/>")
xml = from_mem.get_xml_metadata()
from_mem.del_xml_metadata()
from_mem.bake(annots=True, widgets=False)
from_mem.scrub("clean.pdf")
from_mem.delete_page(0)
from_mem.move_page(0, 1)
page = from_mem.load_page(0)
page.set_rotation(90)
page.add_highlight_annot([(10, 10, 50, 20)])
page.add_text_annot((10, 10), "Check")
flags = pymupdf.SigFlag_SignaturesExist | pymupdf.SigFlag_AppendOnly
le = pymupdf.PDF_ANNOT_LE_CLOSED_ARROW
"""
    result = codemod.transform_source(source, mode="pymupdf")
    assert result.changed
    assert result.diagnostics == ()
    assert "blank = pyropdf.Document()" in result.source
    assert 'from_mem = pyropdf.open(b"%PDF-1.7")' in result.source
    assert "names = list(from_mem.attachments)" in result.source
    assert "count = len(from_mem.attachments)" in result.source
    assert 'payload = from_mem.attachments["a.txt"].data' in result.source
    assert 'del from_mem.attachments["a.txt"]' in result.source
    assert 'from_mem.attachments.add("b.txt", b"hi")' in result.source
    assert 'from_mem.xmp = "<x/>"' in result.source
    assert "xml = from_mem.xmp" in result.source
    assert "del from_mem.xmp" in result.source
    assert "from_mem.flatten(annotations=True, widgets=False)" in result.source
    assert 'from_mem.sanitize("clean.pdf")' in result.source
    assert "del from_mem.pages[0]" in result.source
    assert "from_mem.pages.move(0, 1)" in result.source
    assert "page.rotation = 90" in result.source
    assert "page.annotations.add(pyropdf.Highlight([(10, 10, 50, 20)]))" in result.source
    assert 'page.annotations.add(pyropdf.Note((10, 10), contents="Check"))' in result.source
    assert "flags = pyropdf.SignatureFlags.SIGNATURES_EXIST | pyropdf.SignatureFlags.APPEND_ONLY" in result.source
    assert "le = pyropdf.LineEnding.CLOSED_ARROW" in result.source


def test_pylopdf_015_rewrites() -> None:
    source = """\
import warnings
import pylopdf
from pylopdf import PdfError, PylopdfWarning, peek_metadata

warnings.filterwarnings("ignore", category=PylopdfWarning)
info = peek_metadata("input.pdf")
font = pylopdf.Font(fontname="Helvetica")
custom = pylopdf.Font(fontfile="Noto.otf", weight=700)
with pylopdf.open("input.pdf") as doc:
    doc.set_form_field("name", "Ada")
    page = doc.load_page(0)
    words = page.get_text("words")
    html = page.get_text("html")
    xml = page.get_text("xml")
    raw = doc.tobytes()
"""
    result = codemod.transform_source(source, mode="pylopdf")
    assert result.changed
    assert "import pyropdf" in result.source
    assert "from pyropdf import PdfError, PyropdfWarning, probe" in result.source
    assert 'warnings.filterwarnings("ignore", category=PyropdfWarning)' in result.source
    assert 'info = probe("input.pdf")' in result.source
    assert 'font = pyropdf.Font.standard("Helvetica")' in result.source
    assert 'custom = pyropdf.Font.from_file("Noto.otf", weight=700)' in result.source
    assert 'doc.form["name"].value = "Ada"' in result.source
    assert "words = page.words()" in result.source
    assert "html = page.layout().to_html()" in result.source
    assert "xml = page.layout().to_xml()" in result.source
    assert "raw = doc.to_bytes()" in result.source
    assert any(d.code == "behavior-words-record" for d in result.diagnostics)


def test_scoped_attribute_rewrites_and_current_save_cbz_options() -> None:
    source = """\
from fitz import open as fitz_open

with fitz_open(stream=b"%PDF-1.7", filetype="pdf") as doc:
    page = doc[0]
    page_idx = page.number
    issue_num = issue.number
    nodes = soup.select("a")
    doc.select([0])
    raw = page.get_text("rawdict")
    doc.save("out.pdf", clean=True, ascii=True, pretty=True)
cbz = fitz_open("comic.cbz", filetype="cbz")
"""
    result = codemod.transform_source(source, mode="pymupdf")
    assert result.changed
    assert "from pyropdf import open as fitz_open" in result.source
    assert 'with fitz_open(b"%PDF-1.7") as doc:' in result.source
    assert "page_idx = page.index" in result.source
    assert "issue_num = issue.number" in result.source
    assert "raw = page.layout(chars=True).to_dict()" in result.source
    codes = [d.code for d in result.diagnostics]
    assert codes.count("manual-page-ops") == 1
    assert "manual-cbz-to-pdf" in codes
    assert "manual-save-option" not in codes
    assert "unsupported-filetype" not in codes


def test_non_mechanical_constructs_emit_actionable_diagnostics() -> None:
    source = """\
import fitz

story = fitz.Story("<p>hello</p>")
epub = fitz.open("book.epub", filetype="epub")
with fitz.open("input.pdf") as doc:
    doc.authenticate("secret")
    toc = doc.get_toc()
    page = doc[0]
    d = page.get_text("dict")
    pix = page.get_pixmap(colorspace=fitz.csCMYK)
    svg = page.get_svg_image(text_as_path=False)
    page.add_redact_annot((0, 0, 50, 50))
    page.apply_redactions()
    page.clip_to_rect((0, 0, 100, 100))
    page.insert_text((72, 72), "Hi")
    page.insert_htmlbox((0, 0, 100, 100), "<p>x</p>")
    r = fitz.Rect(0, 0, 10, 10)
    r.include_point((20, 20))
    doc.save("out.pdf", garbage=4, clean=True)
"""
    result = codemod.transform_source(source, mode="auto")
    codes = {d.code for d in result.diagnostics}
    assert "unsupported-story" in codes
    assert "unsupported-filetype" in codes
    assert "behavior-unlock" in codes
    assert "manual-outline" in codes
    assert "manual-get-text-dict" in codes
    assert "unsupported-cmyk-pixmap" in codes
    assert "unsupported-svg-text" in codes
    assert "manual-redaction" in codes
    assert "manual-clip-to-rect" in codes
    assert "manual-canvas" in codes
    assert "unsupported-htmlbox" in codes
    assert "immutable-geometry" in codes
    assert "manual-save-option" in codes
    assert "d = page.layout().to_dict()" in result.source


def test_unrelated_python_file_is_untouched() -> None:
    source = """\
def load_page(items: list[str]) -> str:
    return items[0]
"""
    result = codemod.transform_source(source, mode="auto")
    assert not result.changed
    assert result.source == source
    assert result.diagnostics == ()


def test_cli_dry_run_diff_check_and_in_place(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    original = 'import fitz\nwith fitz.open("input.pdf") as doc:\n    print(doc[0].get_text())\n'
    sample.write_text(original, encoding="utf-8")

    dry = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "tools" / "codemod.py"), "--dry-run", "--diff", "--check", str(sample)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry.returncode == 1
    assert "-import fitz" in dry.stdout
    assert "+import pyropdf" in dry.stdout
    assert sample.read_text(encoding="utf-8") == original

    apply_run = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "tools" / "codemod.py"), "--from", "pymupdf", str(sample)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "rewrote" in apply_run.stdout
    rewritten = sample.read_text(encoding="utf-8")
    assert "import pyropdf" in rewritten
    assert "doc.pages[0].text()" in rewritten

    recheck = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "tools" / "codemod.py"), "--check", str(sample)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert recheck.returncode == 0


def test_pyropdf_package_has_no_pymupdf_compatibility_aliases() -> None:
    for forbidden in ("fitz", "pymupdf", "get_text", "load_page", "saveIncr", "tobytes", "peek_metadata"):
        assert not hasattr(pyropdf, forbidden), forbidden
        assert forbidden not in pyropdf.__all__, forbidden
