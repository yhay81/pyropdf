"""Existing XFA packet streams and arrays authored independently through qpdf."""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from pathlib import Path

import pikepdf
import pytest

import pyropdf

_OPEN = '<x:xdp xmlns:x="http://ns.adobe.com/xdp/" xmlns:a="urn:data">'
_TEMPLATE = '<template note="x > y"><title>model</title></template>'
_DATA = '<a:datasets id="one">first</a:datasets>'
_SECOND = '<a:datasets id="two"/>'
_END = "</x:xdp>"
_NEW = '<a:datasets id="one">updated</a:datasets>'


def _source(*, array: bool, encoding: str = "utf-8") -> tuple[bytes, list[bytes]]:
    pieces = [part.encode(encoding) for part in (_OPEN, _TEMPLATE, _DATA, _SECOND, _END)]
    with pikepdf.new() as pdf:
        pdf.add_blank_page(page_size=(200, 200))
        if array:
            xfa = pikepdf.Array()
            for name, part in zip(("xdp:xdp", "template", "datasets", "datasets", "/xdp:xdp"), pieces, strict=True):
                packet = pdf.make_stream(part)
                packet.Marker = pikepdf.String("keep stream entries")
                xfa.extend((pikepdf.String(name), packet))
            resource = pdf.make_indirect(xfa)
            pdf.Root.SharedPacket = xfa[5]
        else:
            resource = pdf.make_stream(b"".join(pieces))
            resource.Marker = pikepdf.String("keep stream entries")
            pdf.Root.SharedPacket = resource
        pdf.Root.AcroForm = pdf.make_indirect(pikepdf.Dictionary(Fields=pikepdf.Array(), XFA=resource))
        pdf.Root.AcroForm.Other = pikepdf.String("keep form entries")
        buffer = io.BytesIO()
        pdf.save(buffer)
    return buffer.getvalue(), pieces


@pytest.mark.parametrize("array", [False, True], ids=["xdp-stream", "packet-array"])
@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.spec("xfa", "read", "edit", "preserve")
def test_read_edit_remove_and_preserve_xfa_with_qpdf(*, array: bool, incremental: bool) -> None:
    raw, pieces = _source(array=array)
    index = 2 if array else 1
    with pyropdf.open(raw) as doc:
        assert doc.form.xfa_packet_names == (
            ("xdp:xdp", "template", "datasets", "datasets", "/xdp:xdp")
            if array
            else ("template", "datasets", "datasets")
        )
        assert doc.form.xfa_packet(index) == pieces[2]
        assert doc.form.xfa_packet(index + 1) == pieces[3]
        doc.form.replace_xfa_packet(index, _NEW.encode())
        doc.form.remove_xfa_packet(index + 1)
        saved = doc.to_bytes(incremental=incremental)
    if incremental:
        assert saved.startswith(raw)
    with pikepdf.open(io.BytesIO(saved)) as pdf:
        form = pdf.Root.AcroForm
        assert str(form.Other) == "keep form entries"
        assert pdf.Root.SharedPacket.read_bytes() == (pieces[2] if array else b"".join(pieces))
        if array:
            assert [str(x) for x in list(form.XFA)[::2]] == ["xdp:xdp", "template", "datasets", "/xdp:xdp"]
            parts = [x.read_bytes() for x in list(form.XFA)[1::2]]
            assert parts == [pieces[0], pieces[1], _NEW.encode(), pieces[-1]]
            assert str(form.XFA[5].Marker) == "keep stream entries"
            xml = b"".join(parts)
        else:
            xml = form.XFA.read_bytes()
            assert str(form.XFA.Marker) == "keep stream entries"
        root = ET.fromstring(xml)  # noqa: S314 - test-generated XDP.
        assert [e.tag for e in root] == ["template", "{urn:data}datasets"]
        assert root[1].text == "updated"
    with pyropdf.open(saved) as doc:
        assert doc.form.xfa_packet(index) == _NEW.encode()
        assert doc.form.remove_xfa()
        assert doc.form.xfa_packet_names == ()
        assert not doc.form.remove_xfa()
        with pytest.raises(IndexError):
            doc.form.replace_xfa_packet(0, _NEW.encode())
        with pikepdf.open(io.BytesIO(doc.to_bytes(incremental=incremental))) as clean:
            assert "/XFA" not in clean.Root.AcroForm
            assert str(clean.Root.AcroForm.Other) == "keep form entries"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-16-be"])
@pytest.mark.spec("xfa", "interpret")
def test_single_xdp_xml_slices_preserve_original_encoding_and_empty_packets(encoding: str) -> None:
    raw, _ = _source(array=False, encoding=encoding)
    with pyropdf.open(raw) as doc:
        assert doc.form.xfa_packet(-1) == _SECOND.encode(encoding)
        doc.form.replace_xfa_packet(-1, _NEW.encode(encoding))
        assert doc.form.xfa_packet(-1) == _NEW.encode(encoding)


@pytest.mark.parametrize("array", [False, True], ids=["xdp-stream", "packet-array"])
@pytest.mark.spec("xfa", "read", "interpret", "edit")
def test_xfa_rejections_restore_original_resource_and_bounded_reads(*, array: bool) -> None:
    raw, _ = _source(array=array)
    index = 2 if array else 1
    with pyropdf.open(raw) as doc:
        for value, error in (
            (b"<a:datasets>", pyropdf.MalformedPdfError),
            (b"<other/>", ValueError),
            (b"<a:datasets/><a:datasets/>", ValueError),
        ):
            with pytest.raises(error):
                doc.form.replace_xfa_packet(index, value)
            assert doc.form.xfa_packet(index) == _DATA.encode()
        if array:
            with pytest.raises(pyropdf.MalformedPdfError):
                doc.form.remove_xfa_packet(0)
            form = doc.catalog["AcroForm"]
            form["XFA"] = ["name only"]
            with pytest.raises(pyropdf.MalformedPdfError, match="no corresponding stream"):
                _ = doc.form.xfa_packet_names
            assert doc.form.remove_xfa()
            assert "XFA" not in form
        else:
            doc.catalog["AcroForm"]["XFA"].write(b'<!DOCTYPE x SYSTEM "file:///ignored"><x/>')
            with pytest.raises(pyropdf.MalformedPdfError, match="document type"):
                _ = doc.form.xfa_packet_names


def test_absent_xfa_cannot_be_created_by_packet_edit() -> None:
    doc = pyropdf.Document()
    assert doc.form.xfa_packet_names == ()
    with pytest.raises(IndexError):
        doc.form.replace_xfa_packet(0, b"<template/>")
    assert not doc.form.remove_xfa()
    assert "AcroForm" not in doc.catalog


def test_xdp_packet_walk_obeys_document_depth_and_cancellation() -> None:
    raw, _ = _source(array=False)
    with pyropdf.open(raw, limits=pyropdf.DocumentLimits(max_object_depth=2)) as doc:
        with pytest.raises(pyropdf.LimitError) as error:
            _ = doc.form.xfa_packet_names
        assert error.value.code == "object_depth"
    token = pyropdf.CancellationToken()
    with pyropdf.open(raw, limits=pyropdf.DocumentLimits(cancellation=token)) as doc:
        token.cancel()
        with pytest.raises(pyropdf.LimitError):
            _ = doc.form.xfa_packet_names


@pytest.mark.spec("xfa", "read", "interpret", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_irs_form_1040_preserves_ten_other_xfa_packets(*, incremental: bool) -> None:
    source = Path(__file__).parent / "assets" / "real_world" / "f1040.pdf"
    with pikepdf.open(source) as pdf:
        array = pdf.Root.AcroForm.XFA
        names = tuple(str(name) for name in list(array)[::2])
        original = [stream.read_bytes() for stream in list(array)[1::2]]
        encoded = [stream.read_raw_bytes() for stream in list(array)[1::2]]
    assert len(names) == 11
    index = names.index("datasets")
    with pyropdf.open(source) as doc:
        assert doc.form.xfa_packet_names == names
        assert doc.form.xfa_packet(index) == original[index]
        changed = original[index].replace(b"<xfa:data", b"<!--pyropdf edited packet--><xfa:data", 1)
        assert changed != original[index]
        doc.form.replace_xfa_packet(index, changed)
        saved = doc.to_bytes(incremental=incremental)
    with pikepdf.open(io.BytesIO(saved)) as pdf:
        array = pdf.Root.AcroForm.XFA
        assert tuple(str(name) for name in list(array)[::2]) == names
        for i, packet in enumerate(list(array)[1::2]):
            assert packet.read_bytes() == (changed if i == index else original[i])
            if i != index:
                assert packet.read_raw_bytes() == encoded[i]
        assert ET.fromstring(b"".join(stream.read_bytes() for stream in list(array)[1::2])).tag == (  # noqa: S314
            "{http://ns.adobe.com/xdp/}xdp"
        )
