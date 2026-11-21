"""PDF 2.0 STEP and glTF relationships, with independent PDF producers and readers."""

from __future__ import annotations

import io
import json
import struct
from pathlib import Path

import pikepdf
import pytest

import pyropdf as pdf

_FIXTURES = Path(__file__).parent / "fixtures" / "rich-models"
_RECT = pdf.Rect(5, 5, 60, 45)
_STEP = ("STEP", 24064, "2020.0", "1:2023", "77686", "triangle.step", "model/step")
_GLTF = ("glTF", 32007, "2.0", "1:2024", "45880", "triangle.gltf", "model/gltf+json")
_Model = tuple[str, int, str, str, str, str, str]


def _data(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _asset(name: str, mime: str) -> pdf.MediaAsset:
    return pdf.MediaAsset(filename=name, content_type=mime, data=_data(name))


def _stream(file: pdf.typed.FileSpecification) -> pdf.Stream:
    embedded = file.EF
    assert embedded is not None
    stream = embedded.F
    assert stream is not None
    assert isinstance(stream.obj, pdf.Stream)
    return stream.obj


def _foreign_pdf(model: _Model) -> bytes:
    kind, level, version, revision, page, filename, mime = model
    name = pikepdf.Name
    dictionary = pikepdf.Dictionary
    doc = pikepdf.Pdf.new()
    sheet = doc.add_blank_page(page_size=(100, 100))

    def embedded(filename: str, mime: str) -> pikepdf.Object:
        stream = doc.make_stream(_data(filename))
        stream.Type = name.EmbeddedFile
        stream.Subtype = name("/" + mime)
        return doc.make_indirect(dictionary(Type=name.Filespec, F=filename, EF=dictionary(F=stream)))

    primary = embedded(filename, mime)
    names = pikepdf.Array()
    if model == _GLTF:
        names.extend([pikepdf.String("bin alias"), embedded("triangle.bin", "application/gltf-buffer")])
    names.extend([pikepdf.String("model alias"), primary])
    instance = doc.make_indirect(dictionary(Type=name.RichMediaInstance, Subtype=name("/3D"), Asset=primary))
    if model == _GLTF:
        instance.Scene = 0
    config = doc.make_indirect(dictionary(Type=name.RichMediaConfiguration, Subtype=name("/3D"), Instances=[instance]))
    view = doc.make_indirect(dictionary(Type=name("/3DView"), XN="Front", IN="front"))
    content = dictionary(Assets=dictionary(Names=names), Configurations=[config], Views=[view])
    form = doc.make_stream(b"")
    form.Type, form.Subtype, form.BBox, form.Resources = name.XObject, name.Form, [0, 0, 55, 40], dictionary()
    annot = dictionary(
        Type=name.Annot,
        Subtype=name.RichMedia,
        Rect=[5, 5, 60, 45],
        P=sheet.obj,
        AP=dictionary(N=form),
        RichMediaContent=content,
    )
    sheet.Annots = [doc.make_indirect(annot)]
    doc.Root.Extensions = dictionary(
        ISO_=[
            dictionary(
                Type=name.DeveloperExtensions,
                BaseVersion=name("/2.0"),
                ExtensionLevel=level,
                URL=f"https://www.iso.org/standard/{page}.html",
                ExtensionRevision=revision,
                XXKeep=17,
            )
        ]
    )
    doc.Root.Requirements = [
        dictionary(Type=name.Requirement, S=name("/" + kind), V=name("/" + version), XXData="local")
    ]
    out = io.BytesIO()
    doc.save(out, force_version="2.0", deterministic_id=True)
    return out.getvalue()


def _check_pdf(data: bytes, model: _Model, expected: bytes, *, authored: bool = False) -> None:
    kind, level, version, revision, page, filename, mime = model
    with pikepdf.open(io.BytesIO(data)) as reader:
        assert not reader.check_pdf_syntax()
        assert max(reader.pdf_version, str(reader.Root.get("/Version", "/0.0"))[1:]) >= "2.0"
        ext = next(e for e in reader.Root.Extensions.ISO_ if e.ExtensionLevel == level)
        assert not ext.is_indirect
        assert str(ext.BaseVersion) == "/2.0"
        assert ext.ExtensionRevision == revision
        assert f"https://www.iso.org/standard/{page}.html" == ext.URL
        assert (f"/{kind}", f"/{version}") in {(str(r.S), str(r.V)) for r in reader.Root.Requirements}
        if not authored:
            assert ext.XXKeep == 17
            assert reader.Root.Requirements[0].XXData == "local"
        content = reader.pages[0].Annots[0].RichMediaContent
        named = list(content.Assets.Names)[1::2]
        primary = next(f for f in named if str(f.F) == filename)
        assert str(primary.EF.F.Subtype) == "/" + mime
        assert primary.EF.F.read_bytes() == expected
        for config in content.Configurations:
            assert str(config.Subtype) == "/3D"
            for instance in config.Instances:
                assert str(instance.Subtype) == "/3D"
                assert instance.Asset.objgen == primary.objgen
        assert str(content.Views[0].XN) == "Front"
        if model == _GLTF:
            resource = next(f for f in named if str(f.F) == "triangle.bin")
            assert resource.EF.F.read_bytes() == _data("triangle.bin")
            assert str(resource.EF.F.Subtype) == "/application/gltf-buffer"


@pytest.mark.spec("step-rich-media", "read", "interpret", "preserve")
@pytest.mark.spec("gltf-rich-media", "read", "interpret", "preserve")
@pytest.mark.parametrize("model", [_STEP, _GLTF], ids=["step", "gltf"])
@pytest.mark.parametrize("incremental", [False, True])
def test_independent_rich_model_survives_unrelated_edit(model: _Model, *, incremental: bool) -> None:
    source = _foreign_pdf(model)
    with pdf.open(source) as doc:
        assert not doc.check()
        ann = doc.pages[0].annotations[0]
        (entry,) = ann.rich_media_models
        assert entry.format == model[0]
        assert entry.versions == (model[2],)
        assert entry.configuration.Instances is not None
        assert entry.instance == entry.configuration.Instances[0]
        assert entry.instance.Asset == entry.file == ann.rich_media_assets["model alias"]
        assert [resource.F for resource in entry.resources] == ([b"triangle.bin"] if model == _GLTF else [])
        assert entry.scene == (0 if model == _GLTF else None)
        assert ann.rich_media_view is not None
        assert ann.rich_media_view.XN == b"Front"
        ann.contents = "Unrelated edit"
        result = doc.to_bytes(incremental=incremental)
    if incremental:
        assert result.startswith(source)
    _check_pdf(result, model, _data(model[5]))
    with pdf.open(result) as reopened:
        assert not reopened.check()


def _author(model: _Model) -> tuple[pdf.Document, pdf.Annotation]:
    kind, _, version, _, _, filename, mime = model
    doc = pdf.Document()
    page = doc.pages.new((100, 100))
    resources = (_asset("triangle.bin", "application/gltf-buffer"),) if kind == "glTF" else ()
    annotation = page.annotations.add(
        pdf.RichMedia(
            rect=_RECT,
            kind=pdf.MediaKind.THREE_D,
            asset=_asset(filename, mime),
            resources=resources,
            step_version=version if kind == "STEP" else None,
            views=(pdf.ModelView(name="Front"),),
        )
    )
    return doc, annotation


@pytest.mark.spec("step-rich-media", "create", "edit")
@pytest.mark.spec("gltf-rich-media", "create", "edit")
@pytest.mark.parametrize("model", [_STEP, _GLTF], ids=["step", "gltf"])
@pytest.mark.parametrize("incremental", [False, True])
def test_create_and_replace_a_shared_model(model: _Model, *, incremental: bool) -> None:
    kind, _, _, _, _, filename, _ = model
    doc, annotation = _author(model)
    file = annotation.rich_media_models[0].file
    annotation.add_rich_media_configuration("Second", kind=pdf.MediaKind.THREE_D, assets=[file])
    source = doc.to_bytes()
    _check_pdf(source, model, _data(filename), authored=True)
    with pdf.open(source) as opened:
        ann = opened.pages[0].annotations[0]
        a, b = ann.rich_media_models
        assert a.file == b.file
        assert a.instance != b.instance
        replacement = _data(filename).replace(b"pyropdf", b"PyroPDF") if kind == "STEP" else _data(filename) + b"\n"
        assert replacement != _data(filename)
        with ann.edit_rich_media():
            _stream(a.file).write(replacement)
        result = opened.to_bytes(incremental=incremental)
        if incremental:
            assert result.startswith(source)
    _check_pdf(result, model, replacement, authored=True)


def _inline(*, scene: int | None = None, binary: bool = False) -> pdf.RichMedia:
    root = json.loads(_data("triangle-inline.gltf"))
    root.update(scene=1, scenes=[{}, root["scenes"][0]])
    data = json.dumps(root).encode()
    mime = "model/gltf+json"
    if binary:
        data += b" " * (-len(data) % 4)
        data = struct.pack("<4sIII4s", b"glTF", 2, 20 + len(data), len(data), b"JSON") + data
        mime = "model/gltf-binary"
    return pdf.RichMedia(
        rect=_RECT,
        kind=pdf.MediaKind.THREE_D,
        asset=pdf.MediaAsset(filename="model.glb" if binary else "model.gltf", content_type=mime, data=data),
        scene=scene,
    )


@pytest.mark.parametrize("binary", [False, True])
def test_gltf_default_override_and_atomic_refusal(*, binary: bool) -> None:
    doc = pdf.Document()
    page = doc.pages.new((100, 100))
    a = page.annotations.add(_inline(binary=binary))
    b = page.annotations.add(_inline(scene=0, binary=binary))
    assert (a.rich_media_models[0].scene, b.rich_media_models[0].scene) == (1, 0)
    entry = b.rich_media_models[0]
    with pytest.raises(ValueError, match="scene index"), b.edit_rich_media():
        entry.instance.Scene = 2
    assert entry.instance.Scene == 0
    with pytest.raises(ValueError, match="scene index"):
        page.annotations.add(_inline(scene=2, binary=binary))
    assert len(page.annotations) == 2
    assert len(doc.catalog["Extensions"]["ISO_"]) == len(doc.requirements) == 1


def test_extension_edit_preserves_other_requirements_and_private_metadata() -> None:
    with pdf.open(_foreign_pdf(_STEP)) as doc:
        page = doc.pages[0]
        page.annotations.add(_inline())
        assert [ext["ExtensionLevel"] for ext in doc.catalog["Extensions"]["ISO_"]] == [24064, 32007]
        assert doc.catalog["Extensions"]["ISO_"][0]["XXKeep"] == 17
        assert doc.catalog["Requirements"][0]["XXData"].text == "local"
        assert [(req.type.value, req.version) for req in doc.requirements] == [("STEP", "2020.0"), ("glTF", "2.0")]
        assert [ann.rich_media_models[0].format for ann in page.annotations] == ["STEP", "glTF"]


def test_gltf_requires_all_external_files_and_a_typed_embedded_stream() -> None:
    with pdf.open(_foreign_pdf(_GLTF)) as doc:
        ann = doc.pages[0].annotations[0]
        external = ann.rich_media_assets["bin alias"]
        with pytest.raises(ValueError, match="resource URI"):
            ann.set_rich_media_asset("bin alias", None)
        assert ann.rich_media_assets["bin alias"] == external
        with pytest.raises(ValueError, match="embedded glTF resource"), ann.edit_rich_media():
            _stream(external)["Subtype"] = pdf.Name("text/plain")
        with pytest.raises(ValueError, match="resource URI"), ann.edit_rich_media():
            external.F = pdf.String(b"other.bin")
        assert ann.rich_media_models[0].resources == (external,)
        entry = ann.rich_media_models[0]
        before = _stream(entry.file).read()
        with pytest.raises(ValueError, match="at least one scene"), ann.edit_rich_media():
            _stream(entry.file).write(b'{"asset":{"version":"2.0"},"scenes":[]}')
        assert _stream(entry.file).read() == before


def test_out_of_budget_gltf_rolls_back_the_annotation_and_declarations() -> None:
    doc = pdf.Document()
    page = doc.pages.new((100, 100))
    data = b'{"extras":' + b"[" * 100 + b"0" + b"]" * 100 + b"}"
    spec = pdf.RichMedia(
        rect=_RECT,
        kind=pdf.MediaKind.THREE_D,
        asset=pdf.MediaAsset(filename="deep.gltf", content_type="model/gltf+json", data=data),
    )
    with pytest.raises(pdf.LimitError, match="nesting"):
        page.annotations.add(spec)
    assert not page.annotations
    assert "Extensions" not in doc.catalog
    assert "Requirements" not in doc.catalog


@pytest.mark.parametrize("model", [_STEP, _GLTF], ids=["step", "gltf"])
def test_malformed_model_declarations_remain_inspectable_in_the_store(model: _Model) -> None:
    with pdf.open(_foreign_pdf(model)) as doc:
        doc.catalog["Extensions"]["ISO_"][0]["ExtensionLevel"] = str(model[1])
        ann = doc.pages[0].annotations[0]
        with pytest.raises(ValueError, match="missing ISO"):
            _ = ann.rich_media_models
        assert _stream(ann.rich_media_assets["model alias"]).read() == _data(model[5])


def test_edit_of_file_shared_between_independent_contents_is_atomic() -> None:
    with pdf.open(_foreign_pdf(_GLTF)) as doc:
        page = doc.pages[0]
        a = page.annotations[0]
        b = page.annotations.add(_inline())
        shared = a.rich_media_models[0]
        b.set_rich_media_asset("primary", shared.file)
        b.set_rich_media_asset("buffer", shared.resources[0])
        b.add_rich_media_configuration("Shared model", kind=pdf.MediaKind.THREE_D, assets=[shared.file])
        a.set_rich_media_asset(
            "alternate",
            pdf.MediaAsset(filename="other.bin", content_type="application/gltf-buffer", data=_data("triangle.bin")),
        )
        source = _stream(shared.file).read()
        wrong = source.replace(b"triangle.bin", b"other.bin")
        with pytest.raises(ValueError, match="resource URI"), a.edit_rich_media():
            _stream(shared.file).write(wrong)
        assert _stream(shared.file).read() == source
        assert b.rich_media_models[1].resources == shared.resources
        b.set_rich_media_asset("alternate", a.rich_media_assets["alternate"])
        with a.edit_rich_media():
            _stream(shared.file).write(wrong)
        assert a.rich_media_models[0].resources == b.rich_media_models[1].resources


def test_empty_non_model_configuration_and_primary_stream_type() -> None:
    with pdf.open(_foreign_pdf(_GLTF)) as doc:
        ann = doc.pages[0].annotations[0]
        config = doc.objects.add({"Type": pdf.Name("RichMediaConfiguration"), "Subtype": pdf.Name("Sound")})
        ann.obj["RichMediaContent"]["Configurations"].append(doc.objects[config])
        (entry,) = ann.rich_media_models
        with pytest.raises(TypeError, match="Subtype must be a PDF name"), ann.edit_rich_media():
            _stream(entry.file)["Subtype"] = "model/gltf+json"
        with pytest.raises(ValueError, match="retain its registered"), ann.edit_rich_media():
            del _stream(entry.file)["Subtype"]
        assert ann.rich_media_models == (entry,)


def _glb(root: dict, payload: bytes) -> bytes:
    manifest = json.dumps(root).encode()
    manifest += b" " * (-len(manifest) % 4)
    chunk = struct.pack("<I4s", len(payload), b"BIN\0") + payload
    length = 20 + len(manifest) + len(chunk)
    return struct.pack("<4sIII4s", b"glTF", 2, length, len(manifest), b"JSON") + manifest + chunk


@pytest.mark.spec("gltf-rich-media", "read", "interpret", "create", "edit", "preserve")
def test_glb_binary_chunk_and_external_image_preserve_their_own_types() -> None:
    from PIL import Image  # noqa: PLC0415

    output = io.BytesIO()
    Image.new("RGB", (1, 1), (25, 90, 160)).save(output, format="PNG")
    root = json.loads(_data("triangle.gltf"))
    del root["buffers"][0]["uri"]
    root["images"] = [{"uri": "texture.png", "mimeType": "image/png"}]
    binary = _glb(root, _data("triangle.bin"))
    doc = pdf.Document()
    page = doc.pages.new((100, 100))
    image = pdf.MediaAsset(filename="texture.png", content_type="image/png", data=output.getvalue())
    spec = pdf.RichMedia(
        rect=_RECT,
        kind=pdf.MediaKind.THREE_D,
        asset=pdf.MediaAsset(filename="triangle.glb", content_type="model/gltf-binary", data=binary),
        resources=(image,),
    )
    ann = page.annotations.add(spec)
    (model,) = ann.rich_media_models
    assert model.scene == 0
    assert _stream(model.resources[0]).read() == output.getvalue()
    bad_bin = _glb(root, b"1234")
    with pytest.raises(ValueError, match="GLB BIN chunk"), ann.edit_rich_media():
        _stream(model.file).write(bad_bin)
    with pytest.raises(ValueError, match="glTF header"), ann.edit_rich_media():
        _stream(model.file).write(binary[:-1])
    with pytest.raises(ValueError, match="embedded glTF resource"), ann.edit_rich_media():
        _stream(model.resources[0])["Subtype"] = pdf.Name("image/jpeg")
    assert _stream(model.file).read() == binary
    with pikepdf.open(io.BytesIO(doc.to_bytes())) as reader:
        entries = list(reader.pages[0].Annots[0].RichMediaContent.Assets.Names)
        assert [str(value) for value in entries[::2]] == ["texture.png", "triangle.glb"]
        assert entries[1].EF.F.read_bytes() == output.getvalue()
        assert entries[3].EF.F.read_bytes() == binary


@pytest.mark.parametrize("version", [None, "2.0", "2020", "２０２０.0"])
def test_step_needs_an_edition_and_three_d_configuration(version: str | None) -> None:
    asset = _asset("triangle.step", "model/step")
    with pytest.raises(ValueError, match="edition year"):
        pdf.RichMedia(rect=_RECT, kind=pdf.MediaKind.THREE_D, asset=asset, step_version=version)
    with pytest.raises(ValueError, match="3D media"):
        pdf.RichMedia(rect=_RECT, kind=pdf.MediaKind.VIDEO, asset=asset, step_version="2020.0")


@pytest.mark.spec("step-rich-media", "read", "interpret", "edit")
def test_step_instance_may_use_a_filespec_outside_the_pdf20_name_tree() -> None:
    with pdf.open(_foreign_pdf(_STEP)) as doc:
        ann = doc.pages[0].annotations[0]
        shared = ann.rich_media_models[0].file
        ann.set_rich_media_asset("model alias", None)
        assert not ann.rich_media_assets
        assert ann.rich_media_models[0].file == shared


def test_many_gltf_resources_use_a_sorted_name_tree_with_shared_leaf_values() -> None:
    root = json.loads(_data("triangle-inline.gltf"))
    uris = [f"buffer-{number:02}.bin" for number in reversed(range(66))]
    root["buffers"].extend({"uri": uri, "byteLength": 4} for uri in uris)
    spec = pdf.RichMedia(
        rect=_RECT,
        kind=pdf.MediaKind.THREE_D,
        asset=pdf.MediaAsset(filename="model.gltf", content_type="model/gltf+json", data=json.dumps(root).encode()),
        resources=tuple(
            pdf.MediaAsset(filename=uri, content_type="application/gltf-buffer", data=b"1234") for uri in uris
        ),
    )
    doc = pdf.Document()
    ann = doc.pages.new((100, 100)).annotations.add(spec)
    assert list(ann.rich_media_assets) == sorted([*uris, "model.gltf"])
    assert ann.rich_media_models[0].resources == tuple(ann.rich_media_assets[uri] for uri in uris)
    with pikepdf.open(io.BytesIO(doc.to_bytes())) as reader:
        tree = reader.pages[0].Annots[0].RichMediaContent.Assets
        assert len(tree.Kids) == 2
        for child in tree.Kids:
            assert len(child.Names) <= 128
            assert child.Limits == [child.Names[0], child.Names[-2]]
