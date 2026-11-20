"""Alternates, explicit masks and offline reference Form XObjects, checked against independent PDF objects."""

from __future__ import annotations

import io
import shutil
import subprocess
from typing import TYPE_CHECKING

import pikepdf
import pytest
from test_rewrite_images import flate_document
from test_rewrite_images import stream as image_stream

import pyropdf
from pyropdf import Name, typed

if TYPE_CHECKING:
    from pathlib import Path

_RED = b"\xff\x00\x00"
_GREEN = b"\x00\xff\x00"
_BLUE = b"\x00\x00\xff"


def _source() -> bytes:
    """Build independent image alternates and an unavailable reference with a blue proxy."""
    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(12, 12))

        def image(samples: bytes) -> pikepdf.Object:
            stream = pdf.make_stream(samples)
            for key, value in {
                "/Type": pikepdf.Name.XObject,
                "/Subtype": pikepdf.Name.Image,
                "/Width": 1,
                "/Height": 1,
                "/ColorSpace": pikepdf.Name.DeviceRGB,
                "/BitsPerComponent": 8,
            }.items():
                stream[key] = value
            return stream

        base, display, print_image = (image(colour) for colour in (_RED, _GREEN, _BLUE))
        layer = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.OCG, Name="Proof"))
        hidden = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.OCG, Name="Hidden"))
        pdf.Root.OCProperties = pikepdf.Dictionary(
            OCGs=[layer, hidden], D=pikepdf.Dictionary(BaseState=pikepdf.Name.ON, OFF=[hidden])
        )
        display.OC = hidden  # The selected alternate's own /OC does not participate in selection.
        base.Alternates = [
            pikepdf.Dictionary(Image=display),
            pikepdf.Dictionary(Image=display, OC=layer, XXKeep=12),
            pikepdf.Dictionary(Image=print_image, DefaultForPrinting=True, OC=hidden),
        ]
        proxy = pdf.make_stream(b"0 0 1 rg 0 0 12 12 re f")
        proxy.Type, proxy.Subtype = pikepdf.Name.XObject, pikepdf.Name.Form
        proxy.BBox, proxy.Resources = [0, 0, 12, 12], pikepdf.Dictionary()
        proxy.Ref = pikepdf.Dictionary(F="unavailable.pdf", Page=0, XXKeep=17)
        page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Base=base, Proxy=proxy))
        page.Contents = pdf.make_stream(b"/Proxy Do")
        out = io.BytesIO()
        pdf.save(out, min_version="2.0", compress_streams=False)
        return out.getvalue()


def _resources(doc: pyropdf.Document) -> pyropdf.Dictionary:
    return doc.pages[0].obj["Resources"]["XObject"]


@pytest.mark.spec("images", "read")
def test_view_and_print_select_by_base_state_then_alternate_entry_order() -> None:
    with pyropdf.open(_source()) as doc:
        base = _resources(doc)["Base"]
        visible, hidden = doc.layers
        alternates = typed.XObjectImage(base).Alternates
        assert alternates is not None
        assert len(alternates.obj) == 3
        assert alternates[1].Image.obj.read() == _GREEN
        assert alternates[1].OC.obj.xref == visible.xref
        selected = doc.image_for_output(base.xref)
        assert selected is not None
        assert selected.read() == _GREEN
        selected = doc.image_for_output(base.xref, printing=True)
        assert selected is not None
        assert selected.read() == _BLUE
        doc.layers.switch(visible.xref, on=False)
        assert doc.image_for_output(base.xref) == base
        base["OC"] = doc.objects[hidden.xref]
        assert doc.image_for_output(base.xref, printing=True) is None
        doc.layers.switch(hidden.xref, on=True)
        assert doc.image_for_output(base.xref, printing=True) == base


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("images", "create", "edit")
def test_authoring_and_retargeting_alternates_keeps_unrelated_streams(*, incremental: bool) -> None:
    with pyropdf.open(_source()) as doc:
        base = _resources(doc)["Base"]
        entries = typed.XObjectImage(base).Alternates
        assert entries is not None
        first, second = entries[1].Image, entries[2].Image
        second_data = second.obj.read(decode=False)
        with doc.transaction():
            entries[1].Image = second
            base["Alternates"].append({"Image": first.obj, "DefaultForPrinting": False})
        saved = doc.to_bytes(incremental=incremental)
    with pikepdf.open(io.BytesIO(saved)) as pdf:
        base = pdf.pages[0].Resources.XObject.Base
        a, b, c, d = base.Alternates
        assert a.Image.objgen == d.Image.objgen
        assert b.Image.objgen == c.Image.objgen
        assert b.XXKeep == 12
        assert b.Image.read_raw_bytes() == second_data
        assert base.read_bytes() == _RED
        assert d.Image.read_bytes() == _GREEN
        assert not pdf.check_pdf_syntax()
    with pyropdf.open(saved) as reopened:
        selected = reopened.image_for_output(_resources(reopened)["Base"].xref)
        assert selected is not None
        assert selected.read() == _BLUE


@pytest.mark.spec("images", "read")
def test_invalid_and_oversized_alternate_entries_are_reported() -> None:
    with pyropdf.open(_source()) as doc:
        base = _resources(doc)["Base"]
        base["Alternates"] = 1
        with pytest.raises(pyropdf.PdfError, match="must be an array"):
            doc.image_for_output(base.xref)
        base["Alternates"] = [{"Image": _resources(doc)["Proxy"], "DefaultForPrinting": True}]
        with pytest.raises(pyropdf.PdfError, match="image XObject"):
            doc.image_for_output(base.xref, printing=True)
        base["Alternates"] = [None] * 4097
        with pytest.raises(pyropdf.LimitError, match="alternate") as caught:
            doc.image_for_output(base.xref)
        assert caught.value.code == "content_element_count"


@pytest.mark.parametrize("kind", ["stencil", "colour-key"])
@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("image-masks", "create")
def test_creating_an_explicit_or_colour_key_mask_preserves_its_image(kind: str, *, incremental: bool) -> None:
    with pyropdf.open(_source()) as doc:
        base = _resources(doc)["Base"]
        if kind == "stencil":
            n = doc.objects.add_stream(
                b"\x00", {"Type": Name("XObject"), "Subtype": Name("Image"), "Width": 1, "Height": 1, "ImageMask": True}
            )
            base["Mask"] = doc.objects[n]
        else:
            base["Mask"] = [255, 255, 0, 0, 0, 0]
        data = doc.to_bytes(incremental=incremental)
    with pikepdf.open(io.BytesIO(data)) as pdf:
        image = pdf.pages[0].Resources.XObject.Base
        assert image.read_bytes() == _RED
        assert len(image.Alternates) == 3
        if kind == "stencil":
            assert image.Mask.is_indirect
            assert image.Mask.ImageMask
            assert image.Mask.read_bytes() == b"\x00"
        else:
            assert list(image.Mask) == [255, 255, 0, 0, 0, 0]
        assert not pdf.check_pdf_syntax()


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("reference-xobjects", "read", "interpret", "create", "edit", "preserve")
def test_reference_forms_keep_their_proxy_and_page_selector(*, incremental: bool) -> None:
    with pyropdf.open(_source()) as doc:
        proxy = _resources(doc)["Proxy"]
        ref = typed.XObjectFormType1(proxy).Ref
        assert ref is not None
        assert ref.Page == 0
        assert ref.F == b"unavailable.pdf"
        pixmap = doc.pages[0].pixmap(dpi=72)
        assert bytes(pixmap.pixel(6, 6)[:3]) == _BLUE
        rendered = pixmap.to_bytes()
        with doc.transaction():
            ref.Page = "Plate 2"
            ref.obj["F"] = {"Type": Name("Filespec"), "F": "another-unavailable.pdf"}
            ref.obj["ID"] = [b"first identifier", b"later identifier"]
            second = doc.objects.add_stream(b"", {"Subtype": Name("Form"), "BBox": [0, 0, 1, 1], "Resources": {}})
            doc.objects[second]["Ref"] = {"F": b"third.pdf", "Page": 2}
            _resources(doc)["Second"] = doc.objects[second]
        saved = doc.to_bytes(incremental=incremental)
    with pikepdf.open(io.BytesIO(saved)) as pdf:
        xobjects = pdf.pages[0].Resources.XObject
        assert xobjects.Proxy.read_raw_bytes() == b"0 0 1 rg 0 0 12 12 re f"
        assert str(xobjects.Proxy.Ref.Page) == "Plate 2"
        assert int(xobjects.Proxy.Ref.XXKeep) == 17
        assert str(xobjects.Proxy.Ref.F.F) == "another-unavailable.pdf"
        assert tuple(map(bytes, xobjects.Proxy.Ref.ID)) == (b"first identifier", b"later identifier")
        assert int(xobjects.Second.Ref.Page) == 2
        assert not pdf.check_pdf_syntax()
    with pyropdf.open(saved) as reopened:
        assert reopened.pages[0].pixmap(dpi=72).to_bytes() == rendered


@pytest.mark.spec("reference-xobjects", "read")
def test_reference_page_wrong_type_is_reported_without_changing_the_proxy() -> None:
    with pyropdf.open(_source()) as doc:
        proxy = _resources(doc)["Proxy"]
        proxy["Ref"]["Page"] = Name("Plate")
        with pytest.raises(pyropdf.PdfError, match="Page"):
            _ = typed.Reference(proxy["Ref"]).Page
        assert proxy.read() == b"0 0 1 rg 0 0 12 12 re f"


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="Poppler is not installed")
@pytest.mark.spec("reference-xobjects", "interpret")
def test_poppler_renders_the_unavailable_reference_as_its_blue_proxy(tmp_path: Path) -> None:
    source = tmp_path / "proxy.pdf"
    source.write_bytes(_source())
    target = tmp_path / "render"
    tool = shutil.which("pdftoppm")
    assert tool is not None
    subprocess.run([tool, "-singlefile", "-r", "72", str(source), str(target)], check=True)  # noqa: S603 - fixed options on the generated document.
    header, pixels = target.with_suffix(".ppm").read_bytes().split(b"255\n", 1)
    assert header.startswith(b"P6\n")
    assert pixels == _BLUE * (12 * 12)


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("images", "edit")
def test_bulk_resampling_preserves_the_alternate_stream_and_print_selector(*, incremental: bool) -> None:
    source, _ = flate_document()
    with source:
        base = image_stream(source)
        alt = source.objects.add_stream(
            _BLUE,
            {
                "Type": Name("XObject"),
                "Subtype": Name("Image"),
                "Width": 1,
                "Height": 1,
                "ColorSpace": Name("DeviceRGB"),
                "BitsPerComponent": 8,
            },
        )
        base["Alternates"] = [{"Image": source.objects[alt], "DefaultForPrinting": True, "XXKeep": 4}]
        original = source.to_bytes()
    with pyropdf.open(original) as doc:
        base = image_stream(doc)
        entry = base["Alternates"][0]
        stored = entry["Image"].read(decode=False)
        alternate_number = entry["Image"].xref
        report = doc.rewrite_images(dpi_threshold=96, dpi_target=64)
        assert report.rewritten == 1
        assert base["Width"] == 64
        assert base.xref is not None
        selected = doc.image_for_output(base.xref, printing=True)
        assert selected is not None
        assert selected.xref == alternate_number
        saved = doc.to_bytes(incremental=incremental)
    with pikepdf.open(io.BytesIO(saved)) as pdf:
        main = next(iter(pdf.pages[0].Resources.XObject.values()))
        alt = main.Alternates[0]
        assert int(main.Width) == 64
        assert int(alt.XXKeep) == 4
        assert alt.DefaultForPrinting
        assert alt.Image.read_raw_bytes() == stored
        assert alt.Image.read_bytes() == _BLUE


def test_selection_charges_one_budget_across_the_alternate_list() -> None:
    with pyropdf.open(_source()) as doc:
        base = _resources(doc)["Base"]
        alternate = base["Alternates"][0]["Image"]
        base["Alternates"] = [
            doc.objects[doc.objects.add({"Image": alternate, "DefaultForPrinting": index == 95})] for index in range(96)
        ]
        number = base.xref
        data = doc.to_bytes()
    with pyropdf.open(data) as doc:
        selected = doc.image_for_output(number, printing=True)
        assert selected is not None
        assert selected.read() == _GREEN
    limits = pyropdf.DocumentLimits(max_memory_size=32_000)
    with pyropdf.open(data, limits=limits) as doc:
        # Every individual entry fits, even the last one.
        assert _resources(doc)["Base"]["Alternates"][-1]["DefaultForPrinting"] is True
    with pyropdf.open(data, limits=limits) as doc:
        with pytest.raises(pyropdf.LimitError) as caught:
            doc.image_for_output(number, printing=True)
        assert caught.value.code == "memory_size"
        assert not doc.history.steps
