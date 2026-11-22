"""Raster and vector playback of the version captured by a complete Content."""

import dataclasses
import pickle
import shutil
from pathlib import Path

import pytest
from test_canvas_page_forms import _forms
from test_canvas_round_trip import CORPUS, ROUND_TRIP_TOLERANCE, _largest_difference, _poppler_page
from test_content_model import stencil_page

import pyropdf


@pytest.mark.parametrize("name", sorted(path.name for path in CORPUS.glob("*.pdf")))
def test_complete_retained_raster_and_form_cover_every_corpus_page(name: str) -> None:
    source = pyropdf.open(CORPUS / name)
    for page in source.pages:
        captured = page.content()
        expected = page.pixmap(dpi=72)
        assert bytes(captured.pixmap(dpi=72)) == bytes(expected)
        target = pyropdf.Document()
        placed = target.pages.new((page.rect.width, page.rect.height))
        with placed.draw() as canvas:
            canvas.page(placed.rect, captured)
        actual = placed.pixmap(dpi=72)
        assert (actual.width, actual.height) == (expected.width, expected.height)
        assert _largest_difference(expected, actual, []) <= ROUND_TRIP_TOLERANCE, (name, page.index)


def _painted_page() -> tuple[pyropdf.Document, pyropdf.Page, pyropdf.Stream]:
    doc = pyropdf.Document()
    page = doc.pages.new((64, 48))
    form = doc.objects.add_stream(
        b"1 0 0 rg 8 12 36 24 re f",
        {"Type": pyropdf.Name("XObject"), "Subtype": pyropdf.Name("Form"), "BBox": [0, 0, 64, 48]},
    )
    page.obj["Resources"] = {"XObject": {"Paint": doc.objects[form]}}
    page.obj["Contents"] = doc.objects[doc.objects.add_stream(b"/Paint Do")]
    stream = doc.objects[form]
    assert isinstance(stream, pyropdf.Stream)
    return doc, page, stream


def test_retained_raster_keeps_its_geometry_and_resources_after_replacement() -> None:
    doc, page, form = _painted_page()
    captured = page.content()
    clip = (9, 13, 30, 33)
    whole = page.pixmap()
    expected = page.pixmap(matrix=(2, 0, 0, 3, 0, 0), clip=clip, colorspace="gray")
    form.write(b"0 0 1 rg 8 12 36 24 re f")
    assert page.pixmap().pixel(20, 20) == (0, 0, 255)
    del doc.pages[0]
    for _ in range(2):
        assert bytes(captured.pixmap()) == bytes(whole)
        actual = captured.pixmap(matrix=(2, 0, 0, 3, 0, 0), clip=clip, colorspace="gray")
        assert (actual.width, actual.height) == (42, 60)
        assert bytes(actual) == bytes(expected)


@pytest.mark.skipif(not shutil.which("pdftoppm") or not shutil.which("pdftotext"), reason="Poppler unavailable")
def test_replay_retains_a_stencil_as_vectors_after_source_edit(tmp_path: Path) -> None:
    page = stencil_page(inline=False)
    source = page.document
    captured = page.content()
    before = tmp_path / "original.pdf"
    source.save(before)
    _, reference = _poppler_page(before, 1)
    assert max(reference) - min(reference) == 255
    rgb = bytes(captured.pixmap())
    # Compare interiors of all four stencil cells and adjacent paper. The
    # renderers assign the exact one-pixel outer edge differently.
    clip = (8, 68, 36, 94)
    clipped = captured.pixmap(clip=clip)
    samples = set()
    for x, y in ((9, 74), (15, 75), (25, 75), (15, 85), (25, 85)):
        offset = (y * 200 + x) * 3
        color = tuple(reference[offset : offset + 3])
        samples.add(color)
        assert clipped.pixel(x - clip[0], y - clip[1]) == color
    assert samples == {(255, 255, 255), (0, 0, 255)}
    original = source.objects[6]
    assert isinstance(original, pyropdf.Stream)
    original.write(b"\x00" * len(original.read()))
    target = pyropdf.Document()
    placed = target.pages.new((page.rect.width, page.rect.height))
    with placed.draw() as canvas:
        canvas.page(placed.rect, captured)
    path = tmp_path / "placed.pdf"
    target.save(path)
    _, copied = _poppler_page(path, 1)
    assert copied == reference
    reopened = pyropdf.open(path)
    first, second = reopened.pages[0].content().images
    assert first.is_stencil
    assert second.is_stencil
    assert first.xref == second.xref > 0
    assert bytes(page.pixmap()) != rgb


@pytest.mark.parametrize("include", [False, True])
def test_captured_annotation_appearance_policy_applies_to_raster_and_placement(*, include: bool) -> None:
    doc, page, _ = _painted_page()
    name = pyropdf.Name
    appearance = doc.objects.add_stream(b"0 1 0 rg 0 0 8 8 re f", {"Subtype": name("Form"), "BBox": [0, 0, 8, 8]})
    link = doc.objects.add(
        {
            "Type": name("Annot"),
            "Subtype": name("Link"),
            "Rect": [16, 16, 32, 32],
            "AP": {"N": doc.objects[appearance]},
            "Border": [0, 0, 0],
        }
    )
    page.obj["Annots"] = [doc.objects[link]]
    captured = page.content(annotations=include)
    reference = page.pixmap(annotations=include)
    assert reference.pixel(20, 20) == ((0, 255, 0) if include else (255, 0, 0))
    del page.obj["Annots"]
    target = pyropdf.Document()
    placed = target.pages.new((64, 48))
    with placed.draw() as canvas:
        canvas.page(placed.rect, captured)
    assert bytes(captured.pixmap()) == bytes(reference)
    revived = pyropdf.open(target.to_bytes())
    assert bytes(revived.pages[0].pixmap()) == bytes(reference)
    assert not revived.pages[0].annotations


def test_filtered_manual_and_unpickled_values_refuse_replay_without_mutating_the_target() -> None:
    _, page, _ = _painted_page()
    captured = page.content()
    assert [field.name for field in dataclasses.fields(captured)] == ["items"]
    assert dataclasses.asdict(pyropdf.Content(items=())) == {"items": ()}
    pickled = pickle.loads(pickle.dumps(captured))  # noqa: S301 - a value pickled here.
    for content in (
        page.content(paths=False),
        page.content(clip=(0, 0, 10, 10)),
        pyropdf.Content(items=captured.items),
        dataclasses.replace(captured, items=()),
        pickled,
    ):
        with pytest.raises(ValueError, match="complete, unfiltered"):
            content.pixmap()
        target = pyropdf.Document()
        dest = target.pages.new((64, 48))
        before = target.to_bytes()
        with pytest.raises(ValueError, match="complete, unfiltered"), dest.draw() as draw:
            draw.page(dest.rect, content)
        assert target.to_bytes() == before


def test_retained_content_observes_source_cancellation_and_close() -> None:
    original, _, _ = _painted_page()
    token = pyropdf.CancellationToken()
    source = pyropdf.open(original.to_bytes(), limits=pyropdf.DocumentLimits(cancellation=token))
    capture = source.pages[0].content()
    target = pyropdf.Document()
    dest = target.pages.new((64, 48))
    before = target.to_bytes()
    token.cancel()
    with pytest.raises(pyropdf.LimitError) as cancelled:
        capture.pixmap()
    assert cancelled.value.code == "cancelled"
    with pytest.raises(pyropdf.LimitError) as cancelled, dest.draw() as draw:
        draw.page(dest.rect, capture)
    assert cancelled.value.code == "cancelled"
    assert target.to_bytes() == before
    source.close()
    with pytest.raises(pyropdf.DocumentClosedError, match="closed"):
        capture.pixmap()


def test_retained_placements_keep_opposite_states_for_same_named_groups() -> None:
    source = pyropdf.Document()
    page = source.pages.new((32, 32))
    group = source.layers.add("Source", visible=False)
    source.layers.add("Unpainted")
    with page.draw() as draw, draw.layer(group):
        draw.rect(page.rect, fill=pyropdf.Color(0, 0, 1))
    hidden = page.content()
    source.layers.switch(group.xref, on=True)
    shown = page.content()
    target = pyropdf.Document()
    unrelated = target.layers.add("Source", visible=True)
    placed = target.pages.new((96, 64))
    with placed.draw() as draw:
        with draw.layer(unrelated):
            draw.rect((0, 32, 96, 64), fill=pyropdf.Color(1, 1, 0))
        draw.page((0, 0, 32, 32), hidden)
        draw.page((32, 0, 64, 32), shown)
        draw.page((64, 0, 96, 32), hidden)
    reread = pyropdf.open(target.to_bytes())
    raster = reread.pages[0].pixmap()
    assert [raster.pixel(x, 16) for x in (16, 48, 80)] == [(255, 255, 255), (0, 0, 255), (255, 255, 255)]
    assert raster.pixel(16, 48) == (255, 255, 0)
    assert [(g.name, g.on) for g in reread.layers] == [(unrelated.name, True), ("Source", False), ("Source", True)]
    reread.layers.switch(unrelated.xref, on=False)
    switched = reread.pages[0].pixmap()
    assert switched.pixel(16, 48) == (255, 255, 255)
    assert [switched.pixel(x, 16) for x in (16, 48, 80)] == [(255, 255, 255), (0, 0, 255), (255, 255, 255)]


def test_retained_page_transparency_group_is_copied_from_its_original_version() -> None:
    source, page, _ = _painted_page()
    group = source.objects.add({"S": pyropdf.Name("Transparency"), "CS": pyropdf.Name("DeviceRGB"), "I": True})
    page.obj["Group"] = source.objects[group]
    captured = page.content()
    source.objects[group]["CS"] = pyropdf.Name("DeviceGray")
    target = pyropdf.Document()
    placed = target.pages.new((128, 48))
    with placed.draw() as draw:
        draw.page((0, 0, 64, 48), captured)
    (first,) = _forms(placed)
    target.objects[first]["Group"]["CS"] = pyropdf.Name("DeviceCMYK")
    with placed.draw() as draw:
        draw.page((64, 0, 128, 48), captured)
    reread = pyropdf.open(target.to_bytes())
    first, second = _forms(reread.pages[0])
    assert first != second
    assert reread.objects[first]["Group"]["CS"] == "DeviceCMYK"
    stored = reread.objects[second]["Group"]
    assert stored["S"] == "Transparency"
    assert stored["CS"] == "DeviceRGB"
    assert stored["I"] is True
    assert source.objects[group]["CS"] == "DeviceGray"


def test_retained_layer_registration_detaches_shared_configuration_arrays() -> None:
    source = pyropdf.Document()
    group = source.layers.add("Held", visible=False)
    src = source.pages.new((32, 32))
    with src.draw() as draw, draw.layer(group):
        draw.rect(src.rect, fill=pyropdf.Color(0, 0, 1))
    target = pyropdf.Document(version="1.4")
    empty = target.objects.add([])
    target.catalog["OCProperties"] = {"OCGs": [], "D": {"ON": target.objects[empty], "OFF": target.objects[empty]}}
    placed = target.pages.new((32, 32))
    with placed.draw() as draw:
        draw.page(placed.rect, src.content())
    reopened = pyropdf.open(target.to_bytes())
    config = reopened.catalog["OCProperties"]["D"]
    assert not config["ON"]
    assert len(config["OFF"]) == 1
    assert not target.objects[empty]
    assert reopened.catalog["Version"] == "1.5"
    assert reopened.pages[0].pixmap().pixel(16, 16) == (255, 255, 255)


@pytest.mark.parametrize("same_document", [False, True])
def test_a_repeat_placement_uses_the_capture_after_a_destination_descendant_changes(*, same_document: bool) -> None:
    source, original, original_paint = _painted_page()
    capture = original.content()
    target = source if same_document else pyropdf.Document()
    placed = target.pages.new((128, 48))
    with placed.draw() as draw:
        draw.page((0, 0, 64, 48), capture)
    (first,) = _forms(placed)
    copied_paint = target.objects[first]["Resources"]["XObject"]["Paint"]
    copied_paint.write(b"0 0 1 rg 8 12 36 24 re f")
    original_paint.write(b"0 1 0 rg 8 12 36 24 re f")
    assert original.pixmap().pixel(20, 20) == (0, 255, 0)
    with placed.draw() as draw:
        draw.page((64, 0, 128, 48), capture)
    reread = pyropdf.open(target.to_bytes())
    last = reread.pages[-1]
    first, second = _forms(last)
    assert first != second
    pixels = last.pixmap()
    assert [pixels.pixel(x, 20) for x in (20, 84)] == [(0, 0, 255), (255, 0, 0)]
    assert capture.pixmap().pixel(20, 20) == (255, 0, 0)
