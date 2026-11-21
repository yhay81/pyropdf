"""Exercise the executable accessible archival recipe and inspect its tagged content independently."""

from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pikepdf
import pytest

import pyropdf

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "docs/includes/accessible-archive.py"
FONT = ROOT / "crates/pyropdf-engine/src/fonts/testdata/NotoSansJP-subset.otf"
REVISED = "The first trial measured 2 and the second measured 1."
ORIGINAL = "Air flow fell from 2 in the first trial to 1 in the second."


def rgb_profile() -> bytes:
    return runpy.run_path(str(ROOT / "tests/assets/pdfa/generate.py"))["icc"]()


def archive_saves(directory: Path) -> list[Path]:
    """Produce the original, then independent full and incremental revisions of each edit."""
    create = runpy.run_path(str(RECIPE))["create"]
    original = directory / "authored-chart.pdf"
    with create(FONT, rgb_profile()) as doc:
        doc.save(original)
    paths = [original]
    for edit in ("reviewed-alt", "without-alt"):
        for incremental in (False, True):
            mode = "incremental" if incremental else "full"
            path = directory / f"authored-chart-{edit}-{mode}.pdf"
            with pyropdf.open(original) as doc:
                figure = doc.structure[0].children[2]
                if edit == "reviewed-alt":
                    figure.alt = REVISED
                else:
                    del figure.alt
                doc.save(path, incremental=incremental)
            paths.append(path)
    return paths


def check_structure(pdf: pikepdf.Pdf, alt: str | None) -> None:
    """Check PDF 1.7 structure, embedded text and both directions of the MCID mapping."""
    tree = pdf.Root.StructTreeRoot
    root = tree.K[0]
    assert str(root.S) == "/Document"
    assert [str(element.S) for element in root.K] == ["/H1", "/P", "/Figure"]
    assert (str(root.K[2].Alt) if "/Alt" in root.K[2] else None) == alt
    assert "/Namespaces" not in tree
    assert all("/NS" not in element for element in (root, *root.K))

    (form,) = pdf.pages[0].Resources.XObject.values()
    parent_index, parents = tree.ParentTree.Nums
    assert form.StructParents == parent_index
    for mcid, element in enumerate(root.K):
        (reference,) = element.K
        assert reference.Stm.objgen == form.objgen
        assert reference.Pg.objgen == pdf.pages[0].obj.objgen
        assert mcid == reference.MCID
        assert parents[mcid].objgen == element.objgen
    content = form.read_bytes()
    for mcid in range(len(root.K)):
        assert f"/MCID {mcid}".encode() in content
    (font,) = form.Resources.Font.values()
    assert font.DescendantFonts[0].FontDescriptor.FontFile3.read_bytes()
    assert b"begincmap" in font.ToUnicode.read_bytes()


@pytest.mark.spec("pdfa-2a", "create")
@pytest.mark.spec("pdfua-1", "create")
def test_accessible_archival_chart(tmp_path: Path) -> None:
    paths = archive_saves(tmp_path)
    initial = paths[0].read_bytes()
    for path in paths:
        saved = path.read_bytes()
        if path != paths[0]:
            assert saved.startswith(initial) == ("incremental" in path.stem)
        assert saved.startswith(b"%PDF-1.7")
        alt = None if "without-alt" in path.stem else REVISED if "reviewed-alt" in path.stem else ORIGINAL
        with pyropdf.open(path) as doc:
            assert (doc.profiles.pdfa, doc.profiles.pdfua) == ("PDF/A-2A", "PDF/UA-1")
            assert doc.pages[0].text().splitlines() == ["Air flow", "Air flow fell to 1", "2", "1"]
        with pikepdf.Pdf.open(path) as pdf:
            assert str(pdf.Root.Lang) == "en-US"
            assert pdf.Root.ViewerPreferences.DisplayDocTitle
            assert pdf.Root.MarkInfo.Marked
            metadata = ET.fromstring(pdf.Root.Metadata.read_bytes())  # noqa: S314 - self-authored packet.
            title = metadata.findtext(
                ".//dc:title/rdf:Alt/rdf:li[@xml:lang='x-default']",
                namespaces={
                    "dc": "http://purl.org/dc/elements/1.1/",
                    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
                    "xml": "http://www.w3.org/XML/1998/namespace",
                },
            )
            assert title == "Air flow across two trials"
            (intent,) = pdf.Root.OutputIntents
            assert str(intent.S) == "/GTS_PDFA1"
            assert intent.DestOutputProfile.N == 3
            assert intent.DestOutputProfile.read_bytes() == rgb_profile()
            check_structure(pdf, alt)


def test_documented_command_accepts_external_inputs(tmp_path: Path) -> None:
    profile = tmp_path / "chart.icc"
    profile.write_bytes(rgb_profile())
    path = tmp_path / "chart.pdf"
    command = [sys.executable, str(RECIPE), str(FONT), str(profile), str(path)]
    subprocess.run(command, check=True)  # noqa: S603 - repository program and test-owned inputs.
    with pyropdf.open(path) as doc:
        assert (doc.profiles.pdfa, doc.profiles.pdfua) == ("PDF/A-2A", "PDF/UA-1")
