"""Stored Web Capture records and cross-index relationships (ISO 32000-1, 14.10)."""

from __future__ import annotations

import io

import pikepdf
import pytest
from test_attachments import build_raw_pdf

import pyropdf
from pyropdf import Name, typed

IDENTIFIER = bytes(range(16))
URL = "http://example.org/source"
ALIAS = "http://example.org/old"


def _pdf(*, captured: bool = True, version: str = "1.7") -> bytes:
    ident = f"<{IDENTIFIER.hex()}>"
    catalog_entries = "/SpiderInfo 11 0 R /Names 12 0 R" if captured else ""
    id_entry = f"/ID {ident}" if captured else ""
    objects = {
        1: f"<< /Type /Catalog /Pages 2 0 R {catalog_entries} >>",
        2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 32 32] /Resources << /XObject << /Pic 5 0 R >> >> "
        f"{id_entry} >>",
        4: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 32 32] /Resources <<>> >>",
        5: f"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8 "
        f"/Length 1 {id_entry} >>\nstream\nA\nendstream",
    }
    if captured:
        objects.update(
            {
                6: f"<< /Type /SpiderContentSet /S /SPS /ID {ident} /O [3 0 R] /SI 8 0 R /T (Original) /XXKeep 11 >>",
                7: f"<< /Type /SpiderContentSet /S /SIS /ID {ident} /O [5 0 R] /SI [<< /AU ({URL}) >>] /R 1 >>",
                8: f"<< /AU << /U ({URL}) /C [[({ALIAS})]] >> /C 9 0 R /XXSource (keep) >>",
                9: f"<< /URL ({URL}) /L 3 /XXCommand (keep) >>",
                10: "[6 0 R 7 0 R]",
                11: "<< /V 1 /C [9 0 R] /XXCapture 5 >>",
                12: "<< /IDS 13 0 R /URLS 14 0 R /XXTree 17 0 R >>",
                13: f"<< /Names [{ident} 10 0 R] /XXIndex (keep) >>",
                14: "<< /Kids [15 0 R] >>",
                15: f"<< /Limits [({ALIAS}) ({URL})] /Names [({ALIAS}) 6 0 R ({URL}) 10 0 R] >>",
                17: "<< /Names [(other) 9 0 R] >>",
            }
        )
    return f"%PDF-{version}".encode() + build_raw_pdf(objects)[8:]


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("web-capture", "read", "interpret", "preserve")
def test_stored_sets_share_member_and_command_references(*, incremental: bool) -> None:
    doc = pyropdf.open(_pdf())
    database = doc.web_capture
    pages, images = database.by_id(IDENTIFIER)
    assert isinstance(pages, typed.WebCapturePageSet)
    assert isinstance(images, typed.WebCaptureImageSet)
    assert database.by_url("HTTP://EXAMPLE.ORG:80/a/../source#spot") == (pages, images)
    assert database.by_url(ALIAS) == (pages,)
    assert database.by_id(IDENTIFIER[::-1]) == ()
    assert database.info is not None
    assert isinstance(pages.SI, typed.SourceInformation)
    assert pages.SI.C is not None
    assert database.info.C is not None
    assert pages.SI.C.obj == database.info.C[0].obj
    assert pages.O[0].obj == doc.pages[0].obj
    assert images.O[0].obj == doc.objects[5]
    pages.T = "Revised"
    with pikepdf.open(io.BytesIO(doc.to_bytes(incremental=incremental))) as other:
        p, image = other.Root.Names.IDS.Names[1]
        assert str(p.T) == "Revised"
        assert p.XXKeep == 11
        assert p.O[0].objgen == other.pages[0].objgen
        assert image.O[0].objgen == other.pages[0].Resources.XObject.Pic.objgen
        assert p.SI.C.objgen == other.Root.SpiderInfo.C[0].objgen
        assert bytes(p.SI.C.XXCommand) == b"keep"
        assert other.Root.Names.IDS.Names[1].objgen == other.Root.Names.URLS.Kids[0].Names[3].objgen


def _register(doc: pyropdf.Document, *, image: bool) -> typed.WebCapturePageSet | typed.WebCaptureImageSet:
    obj = doc.objects[
        doc.objects.add(
            {
                "S": Name("SIS" if image else "SPS"),
                "ID": IDENTIFIER,
                "O": [doc.objects[5] if image else doc.pages[0].obj],
                "SI": {"AU": URL.encode("ascii")},
                **({"R": 1} if image else {}),
            }
        )
    ]
    view = typed.WebCaptureImageSet(obj) if image else typed.WebCapturePageSet(obj)
    doc.web_capture.add(view)
    return view


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("web-capture", "create")
def test_register_page_and_image_sets_with_the_same_source(*, incremental: bool) -> None:
    doc = pyropdf.open(_pdf(captured=False, version="1.2"))
    pages = _register(doc, image=False)
    images = _register(doc, image=True)
    assert doc.web_capture.by_id(IDENTIFIER) == (pages, images)
    with pikepdf.open(io.BytesIO(doc.to_bytes(incremental=incremental))) as other:
        assert str(other.Root.Version) == "/1.4"
        assert other.Root.SpiderInfo.V == 1
        names = other.Root.Names
        p, image = names.IDS.Names[1]
        assert [item.objgen for item in names.URLS.Names[1]] == [p.objgen, image.objgen]
        assert bytes(names.IDS.Names[0]) == bytes(p.ID) == bytes(image.ID) == IDENTIFIER
        assert bytes(p.O[0].ID) == bytes(image.O[0].ID) == IDENTIFIER
        assert int(image.R) == 1


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("web-capture", "edit", "preserve")
def test_remove_set_prunes_aliases_preserves_sibling_and_unknown_data(*, incremental: bool) -> None:
    doc = pyropdf.open(_pdf())
    pages, image = doc.web_capture.by_id(IDENTIFIER)
    doc.web_capture.remove(pages)
    assert doc.web_capture.by_url(ALIAS) == ()
    assert doc.web_capture.by_url(URL) == (image,)
    assert "ID" not in doc.pages[0].obj
    assert doc.objects[5]["ID"] == IDENTIFIER
    with pikepdf.open(io.BytesIO(doc.to_bytes(incremental=incremental))) as other:
        names = other.Root.Names
        assert bytes(names.IDS.XXIndex) == b"keep"
        assert names.IDS.Names[1].objgen == names.URLS.Names[1].objgen
        assert names.XXTree.Names[1].objgen == other.Root.SpiderInfo.C[0].objgen
        assert names.IDS.Names[1].S == pikepdf.Name.SIS


def test_structural_page_edits_refuse_before_changing_membership_or_image_counts() -> None:
    doc = pyropdf.open(_pdf())
    original = doc.to_bytes(incremental=True)
    with pytest.raises(pyropdf.PdfError, match="Web Capture"):
        del doc.pages[0]
    with pytest.raises(pyropdf.PdfError, match="Web Capture"):
        doc.pages.select([1, 0, 0])
    assert doc.to_bytes(incremental=True) == original
    doc.pages.move(0, 1)
    del doc.pages[0]  # The unrelated page may still be removed.
    pages, image = doc.web_capture.by_id(IDENTIFIER)
    doc.web_capture.remove(pages)
    with pytest.raises(pyropdf.PdfError, match="image"):
        del doc.pages[0]
    doc.web_capture.remove(image)
    del doc.pages[0]
    assert len(doc.pages) == 0


def test_creation_is_deprecated_in_20_and_existing_sets_can_be_removed() -> None:
    doc = pyropdf.open(_pdf(version="2.0"))
    pages, image = doc.web_capture.by_id(IDENTIFIER)
    before = doc.to_bytes(incremental=True)
    with pytest.raises(ValueError, match="deprecated"):
        doc.web_capture.add(image)
    assert doc.to_bytes(incremental=True) == before
    doc.web_capture.remove(pages)
    assert doc.web_capture.by_id(IDENTIFIER) == (image,)


def test_malformed_tree_and_conflicting_membership_refuse_atomically() -> None:
    doc = pyropdf.open(_pdf())
    pages, _ = doc.web_capture.by_id(IDENTIFIER)
    separate = doc.objects[
        doc.objects.add(
            {
                "S": Name("SPS"),
                "O": [doc.pages[0].obj],
                "ID": IDENTIFIER[::-1],
                "SI": {"AU": b"http://example.org/new"},
            }
        )
    ]
    before = doc.to_bytes(incremental=True)
    with pytest.raises(ValueError, match="more than one"):
        doc.web_capture.add(typed.WebCapturePageSet(separate))
    assert doc.to_bytes(incremental=True) == before
    doc.catalog["Names"]["URLS"]["Kids"][0]["XXChild"] = b"keep"
    before = doc.to_bytes(incremental=True)
    with pytest.raises(pyropdf.PdfError, match="private entries"):
        doc.web_capture.remove(pages)
    assert doc.to_bytes(incremental=True) == before
    doc.catalog["Names"]["URLS"]["Kids"][0]["Names"] = [b"orphan"]
    with pytest.raises(pyropdf.PdfError, match="paired Names"):
        doc.web_capture.by_url(URL)


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("web-capture", "edit")
def test_changing_sources_updates_indexed_urls_in_existing_pdf_20(*, incremental: bool) -> None:
    doc = pyropdf.open(_pdf(version="2.0"))
    pages, image = doc.web_capture.by_id(IDENTIFIER)
    source = typed.SourceInformation(doc.objects[8])
    second = typed.SourceInformation(doc.objects[doc.objects.add({"AU": b"ftp://EXAMPLE.org:21/n", "XXProp": 32})])
    original_ids_root = doc.catalog["Names"]["IDS"].xref
    doc.web_capture.set_sources(pages, (source, second))
    assert doc.catalog["Names"]["IDS"].xref == original_ids_root
    assert doc.web_capture.by_url("ftp://example.org/n") == (pages,)
    doc.web_capture.set_sources(pages, second)
    assert doc.web_capture.by_url(URL) == (image,)
    assert doc.web_capture.by_url(ALIAS) == ()
    with pikepdf.open(io.BytesIO(doc.to_bytes(incremental=incremental))) as other:
        p, _ = other.Root.Names.IDS.Names[1]
        assert int(p.SI.XXProp) == 32
        assert p.SI.objgen[0] == second.obj.xref
        assert bytes(other.Root.SpiderInfo.C[0].XXCommand) == b"keep"


def test_authoring_retains_a_large_url_index_and_its_page_and_image_sets() -> None:
    doc = pyropdf.open(_pdf())
    pages, image = doc.web_capture.by_id(IDENTIFIER)
    source = typed.SourceInformation(
        doc.objects[
            doc.objects.add(
                {
                    "AU": {
                        "U": URL.encode(),
                        "C": [[f"http://example.org/old/{i:02d}".encode() for i in range(65)]],
                    }
                }
            )
        ]
    )
    doc.web_capture.set_sources(pages, source)
    with pikepdf.open(io.BytesIO(doc.to_bytes())) as other:
        root = other.Root.Names.URLS
        assert len(root.Kids) == 2
        assert all(len(child.Names) <= 128 for child in root.Kids)
        assert [(bytes(child.Limits[0]), bytes(child.Limits[1])) for child in root.Kids] == [
            (bytes(child.Names[0]), bytes(child.Names[-2])) for child in root.Kids
        ]
    assert doc.web_capture.by_url("http://example.org/old/64") == (pages,)
    assert doc.web_capture.by_url(URL) == (pages, image)


def test_foreign_sources_and_catalog_version_refuse_without_changing_the_document() -> None:
    doc = pyropdf.open(_pdf())
    foreign = pyropdf.open(_pdf())
    pages, image = doc.web_capture.by_id(IDENTIFIER)
    before = doc.to_bytes(incremental=True)
    with pytest.raises(ValueError, match="another document"):
        doc.web_capture.set_sources(pages, typed.SourceInformation(foreign.objects[8]))
    assert doc.to_bytes(incremental=True) == before
    doc.catalog["Version"] = Name("2.0")
    with pytest.raises(ValueError, match="deprecated"):
        doc.web_capture.add(image)


def test_one_optional_index_may_contain_a_direct_dictionary() -> None:
    doc = pyropdf.open(_pdf(captured=False))
    direct = {"S": Name("SPS"), "ID": IDENTIFIER, "O": [doc.pages[0].obj], "SI": {"AU": URL.encode()}}
    doc.catalog["Names"] = {"URLS": {"Names": [URL.encode(), direct]}}
    (pages,) = doc.web_capture.by_url(URL)
    assert isinstance(pages, typed.WebCapturePageSet)
    assert pages.O[0].obj == doc.pages[0].obj
    assert not doc.web_capture.by_id(IDENTIFIER)
    before = doc.to_bytes(incremental=True)
    with pytest.raises(ValueError, match="indirect"):
        doc.web_capture.remove(pages)
    assert doc.to_bytes(incremental=True) == before


def test_capture_lookup_and_removal_share_the_operation_budget_and_refuse_atomically() -> None:
    data = _pdf()
    doc = pyropdf.open(data, limits=pyropdf.DocumentLimits(max_memory_size=16_384))
    pages, image = doc.web_capture.by_id(IDENTIFIER)
    with pytest.raises(pyropdf.LimitError) as caught:
        doc.web_capture.remove(pages)
    assert caught.value.code == "memory_size"
    assert doc.web_capture.by_id(IDENTIFIER) == (pages, image)
    assert not doc.history.steps
    assert doc.to_bytes(incremental=True) == data
