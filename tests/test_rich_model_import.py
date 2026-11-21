"""Import only the model declarations required by the selected foreign pages."""

from __future__ import annotations

import io

import pikepdf
import pytest
from test_rich_models import _GLTF, _STEP, _data, _foreign_pdf, _Model

import pyropdf as p


def _file_bytes(file: p.typed.FileSpecification) -> bytes:
    embedded = file.EF
    assert embedded is not None
    stream = embedded.F
    assert stream is not None
    return stream.obj.read()


@pytest.mark.parametrize(("model", "other"), [(_GLTF, _STEP), (_STEP, _GLTF)], ids=["gltf", "step"])
@pytest.mark.parametrize("incremental", [False, True])
def test_import_resolves_model_and_preserves_existing_catalog_metadata(
    model: _Model, other: _Model, *, incremental: bool
) -> None:
    source_bytes, target_bytes = _foreign_pdf(model), _foreign_pdf(other)
    with p.open(source_bytes) as src, p.open(target_bytes) as dst:
        src.pages.new((10, 10))
        # Copying the empty page does not introduce requirements from another page.
        dst.pages.insert(0, src.pages[1])
        assert [req.type.value for req in dst.requirements] == [other[0]]
        dst.pages.insert(1, src.pages[0])
        dst.pages.extend(src.pages[0])
        assert [req.type.value for req in dst.requirements] == [other[0], model[0]]
        for index in (1, 3):
            (copied,) = dst.pages[index].annotations[0].rich_media_models
            assert (copied.format, copied.versions) == (model[0], (model[2],))
            assert _file_bytes(copied.file) == _data(model[5])
            if model == _GLTF:
                assert [_file_bytes(file) for file in copied.resources] == [_data("triangle.bin")]
        result = dst.to_bytes(incremental=incremental)
    if incremental:
        assert result.startswith(target_bytes)
    with pikepdf.open(io.BytesIO(result)) as check:
        assert not check.check_pdf_syntax()
        assert [ext.ExtensionLevel for ext in check.Root.Extensions.ISO_] == [other[1], model[1]]
        assert check.Root.Extensions.ISO_[0].XXKeep == 17
        assert check.Root.Requirements[0].XXData == "local"
        assert [(str(req.S), str(req.V)) for req in check.Root.Requirements] == [
            ("/" + code[0], "/" + code[2]) for code in (other, model)
        ]
        ann = check.pages[1].Annots[0]
        content = ann.RichMediaContent
        primary = content.Configurations[0].Instances[0].Asset
        assert primary.EF.F.read_bytes() == _data(model[5])
        assert primary.objgen in [f.objgen for f in list(content.Assets.Names)[1::2]]


@pytest.mark.parametrize("model", [_GLTF, _STEP], ids=["gltf", "step"])
def test_import_into_empty_document_registers_pdf_2_model(model: _Model) -> None:
    with p.open(_foreign_pdf(model)) as src:
        target = p.Document()
        target.pages.extend(src.pages[0])
        (entry,) = target.pages[0].annotations[0].rich_media_models
        assert (entry.format, entry.versions) == (model[0], (model[2],))
        with pikepdf.open(io.BytesIO(target.to_bytes())) as check:
            assert max(check.pdf_version, str(check.Root.get("/Version", "/0.0"))[1:]) >= "2.0"
            assert [ext.ExtensionLevel for ext in check.Root.Extensions.ISO_] == [model[1]]


@pytest.mark.parametrize(
    ("failure", "error", "match"),
    [
        ("source", TypeError, "Requirements"),
        ("destination", ValueError, "extension"),
        ("budget", p.LimitError, "4096"),
    ],
)
def test_invalid_or_over_budget_import_rolls_back_pages_and_declarations(
    failure: str, error: type[Exception], match: str
) -> None:
    with p.open(_foreign_pdf(_GLTF)) as src, p.open(_foreign_pdf(_STEP)) as dst:
        if failure == "source":
            del src.catalog["Requirements"]
        elif failure == "destination":
            conflicting = dict(src.catalog["Extensions"]["ISO_"][0])
            conflicting["URL"] = "https://example.invalid/"
            dst.catalog["Extensions"]["ISO_"].append(conflicting)
        else:
            src.catalog["Requirements"] = [{"S": p.Name("EnableJavaScripts")}] * 4097
        before = (len(dst.pages), len(dst.objects), list(dst.history.steps))
        with pytest.raises(error, match=match) as rejected:
            dst.pages.extend(src.pages[0])
        if failure == "budget":
            assert isinstance(rejected.value, p.LimitError)
            assert rejected.value.code == "annotation_count"
        assert (len(dst.pages), len(dst.objects), list(dst.history.steps)) == before
        assert dst.catalog["Extensions"]["ISO_"][0]["XXKeep"] == 17
        assert dst.catalog["Requirements"][0]["XXData"].text == "local"
        assert [req.type.value for req in dst.requirements] == ["STEP"]
