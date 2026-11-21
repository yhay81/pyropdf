"""Named rich media assets, configuration choices and per-annotation settings."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pikepdf
import pytest
from test_multimedia import media_pdf
from test_preservation import raw_objects

import pyropdf as pdf
from pyropdf import typed

if TYPE_CHECKING:
    from pathlib import Path


def rich_settings_pdf(*, scripts: bool = True) -> bytes:
    """Create two annotations sharing stored content and selecting different configurations."""
    with pikepdf.open(io.BytesIO(media_pdf())) as doc:
        page = doc.pages[0]
        rich = page.Annots[2]
        d, n = pikepdf.Dictionary, pikepdf.Name
        content = doc.make_indirect(rich.RichMediaContent)
        rich.RichMediaContent = content
        first = content.Configurations[0]
        first.Name = "front"
        other = doc.make_indirect(
            d(Type=n.RichMediaConfiguration, Subtype=n("/3D"), Name="side", Instances=first.Instances)
        )
        content.Configurations.append(other)
        view = content.Views[0]
        alt = doc.make_indirect(d(Type=n("/3DView"), XN="Side", IN="side"))
        content.Views.append(alt)
        model = first.Instances[0].Asset
        script_stream = doc.make_stream(b"")
        script_stream.Type = n.EmbeddedFile
        script_stream.Subtype = n("/application/javascript")
        script = doc.make_indirect(d(Type=n.Filespec, F="empty.js", EF=d(F=script_stream)))
        names = pikepdf.Array()
        for i in range(63):
            names.extend([pikepdf.String(f"model-{i:02}.u3d"), model])
        names.extend([pikepdf.String("start.js"), script])
        content.Assets = doc.make_indirect(d(Names=names))
        rich.RichMediaSettings = d(
            Activation=d(
                Condition=n.XA,
                Configuration=first,
                View=view,
                Animation=d(Subtype=n.Linear, Speed=1, PlayCount=2),
                Presentation=d(Style=n.Embedded, Toolbar=False),
            ),
            Deactivation=d(Condition=n.XD),
        )
        sibling = doc.make_indirect(d(dict(rich.items())))
        sibling.RichMediaSettings = d(Activation=d(Configuration=other, View=alt))
        if scripts:
            sibling.RichMediaSettings.Activation.Scripts = [script]
        page.Annots.append(sibling)
        doc.trailer.ID = [b"richmedia-sample", b"richmedia-sample"]
        out = io.BytesIO()
        doc.save(out, deterministic_id=True)
        return out.getvalue()


def edit_rich_fixture(doc: pdf.Document) -> None:
    ann = doc.pages[0].annotations[2]
    files = ann.rich_media_assets
    assert len(files) == 64
    shared = files["model-00.u3d"]
    assert shared.obj == files["model-62.u3d"].obj
    ann.set_rich_media_asset("model-extra.u3d", shared)
    added = ann.add_rich_media_configuration(
        "two instances", kind=pdf.MediaKind.THREE_D, assets=(shared, files["model-62.u3d"])
    )
    assert added.Instances
    assert added.Instances[0].Asset == added.Instances[1].Asset == shared
    with ann.edit_rich_media() as media:
        content = media.RichMediaContent
        settings = media.RichMediaSettings
        assert settings
        assert settings.Activation
        assert settings.Deactivation
        current = settings.Activation
        assert current.Animation
        assert current.Presentation
        current.Configuration = content.Configurations[1]
        assert content.Views
        current.View = content.Views[1]
        current.Animation.PlayCount = 5
        current.Presentation.Toolbar = True
        settings.Deactivation.Condition = "PI"


@pytest.mark.spec("rich-annotations", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.spec("rich-media", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.parametrize("scripts", [False, True])
@pytest.mark.parametrize("incremental", [False, True])
def test_add_named_asset_split_and_select_shared_configuration(
    tmp_path: Path, *, incremental: bool, scripts: bool
) -> None:
    source = rich_settings_pdf(scripts=scripts)
    with pdf.open(source) as doc:
        edit_rich_fixture(doc)
        output = tmp_path / "rich-settings.pdf"
        doc.save(output, incremental=incremental)
    if incremental:
        assert output.read_bytes().startswith(source)
    with pikepdf.open(output) as independent:
        ann, sibling = independent.pages[0].Annots[2], independent.pages[0].Annots[-1]
        content = ann.RichMediaContent
        assert content.objgen == sibling.RichMediaContent.objgen
        assert len(content.Assets.Kids) >= 2
        keys = [bytes(key) for leaf in content.Assets.Kids for key in leaf.Names[::2]]
        assert keys == sorted(keys)
        assert len(keys) == 65
        for leaf in content.Assets.Kids:
            assert list(leaf.Limits) == [leaf.Names[0], leaf.Names[-2]]
        choice = ann.RichMediaSettings.Activation
        assert choice.Configuration.objgen == content.Configurations[1].objgen
        assert choice.View.objgen == content.Views[1].objgen
        assert choice.Animation.PlayCount == 5
        assert choice.Presentation.Toolbar
        assert len(content.Configurations) == 3
        third = content.Configurations[2]
        assert str(third.Name) == "two instances"
        assert third.Instances[0].objgen != third.Instances[1].objgen
        assert third.Instances[0].Asset.objgen == third.Instances[1].Asset.objgen
        if scripts:
            assert sibling.RichMediaSettings.Activation.Scripts[0].EF.F.read_bytes() == b""
        assert ann.RichMediaSettings.Deactivation.Condition == "/PI"
        assert "/Animation" not in sibling.RichMediaSettings.Activation
        asset = content.Configurations[0].Instances[0].Asset
        assert asset.objgen == content.Configurations[1].Instances[0].Asset.objgen
        assert asset.EF.F.read_bytes().startswith(b"U3D")


@pytest.mark.spec("rich-media", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_author_activation_animation_and_windowed_presentation(*, incremental: bool) -> None:
    with pdf.open(media_pdf()) as doc:
        ann = doc.pages[0].annotations[2]
        with ann.edit_rich_media() as media:
            settings = typed.RichMediaSettings(doc.objects[doc.objects.add({})])
            activation = typed.RichMediaActivation(doc.objects[doc.objects.add({})])
            animation = typed.RichMediaAnimation(doc.objects[doc.objects.add({})])
            presentation = typed.RichMediaPresentation(doc.objects[doc.objects.add({})])
            window = typed.RichMediaWindow(doc.objects[doc.objects.add({})])
            position = typed.RichMediaPosition(doc.objects[doc.objects.add({})])
            deactivation = typed.RichMediaDeactivation(doc.objects[doc.objects.add({})])
            animation.Subtype = "Oscillating"
            animation.Speed = 2
            presentation.Style = "Windowed"
            position.HAlign = "Far"
            window.Position = position
            presentation.Window = window
            activation.Condition = "PV"
            activation.Animation = animation
            activation.Presentation = presentation
            deactivation.Condition = "PI"
            settings.Activation = activation
            settings.Deactivation = deactivation
            media.RichMediaSettings = settings
        with pikepdf.open(io.BytesIO(doc.to_bytes(incremental=incremental))) as reader:
            stored = reader.pages[0].Annots[2].RichMediaSettings
            assert stored.Activation.Condition == "/PV"
            assert stored.Activation.Animation.Subtype == "/Oscillating"
            assert stored.Activation.Animation.Speed == 2
            assert stored.Activation.Presentation.Window.Position.HAlign == "/Far"
            assert stored.Deactivation.Condition == "/PI"


def test_removing_an_asset_required_by_another_annotations_settings_is_atomic() -> None:
    with pdf.open(rich_settings_pdf()) as doc:
        ann = doc.pages[0].annotations[2]
        pos = doc.history.position
        with pytest.raises(ValueError, match="activation script"):
            ann.set_rich_media_asset("start.js", None)
        assert doc.history.position == pos
        sibling = doc.pages[0].annotations[-1].media
        assert isinstance(sibling, typed.AnnotRichMedia)
        assert sibling.RichMediaSettings
        assert sibling.RichMediaSettings.Activation
        assert sibling.RichMediaSettings.Activation.Scripts
        assert sibling.RichMediaSettings.Activation.Scripts[0].obj == ann.rich_media_assets["start.js"].obj


def test_removing_a_selected_configuration_rolls_back_all_edits() -> None:
    with pdf.open(rich_settings_pdf()) as doc:
        ann = doc.pages[0].annotations[2]
        pos = doc.history.position
        with pytest.raises(ValueError, match="activation Configuration"), ann.edit_rich_media() as media:
            del media.RichMediaContent.Configurations.obj[1]
        assert doc.history.position == pos


def test_remove_aliases_without_destroying_instance_assets_or_scripts() -> None:
    with pdf.open(rich_settings_pdf()) as doc:
        ann = doc.pages[0].annotations[2]
        original = ann.rich_media_assets["start.js"]
        ann.set_rich_media_asset("z-alias.js", original)
        ann.set_rich_media_asset("start.js", None)
        assert ann.rich_media_assets["z-alias.js"] == original
        ann.set_rich_media_asset(
            "model-00.u3d", pdf.MediaAsset(filename="note.txt", content_type="text/plain", data=b"note")
        )
        assert ann.rich_media_assets["model-00.u3d"].F == b"note.txt"
        with pytest.raises(ValueError, match="activation script"):
            ann.set_rich_media_asset("z-alias.js", ann.rich_media_assets["model-00.u3d"])
    with pdf.open(media_pdf()) as doc:
        ann = doc.pages[0].annotations[2]
        ann.set_rich_media_asset("empty.u3d", None)
        assert not ann.rich_media_assets
        with pikepdf.open(io.BytesIO(doc.to_bytes())) as independent:
            content = independent.pages[0].Annots[2].RichMediaContent
            assert not content.Assets.Names
            assert content.Configurations[0].Instances[0].Asset.EF.F.read_bytes().startswith(b"U3D")


def test_selected_view_foreign_assets_and_tree_limits_are_atomic() -> None:
    with pdf.open(rich_settings_pdf()) as doc, pdf.open(media_pdf()) as foreign:
        ann = doc.pages[0].annotations[2]
        pos = doc.history.position
        with pytest.raises(ValueError, match="activation View"), ann.edit_rich_media() as media:  # noqa: PT012
            assert media.RichMediaContent.Views
            del media.RichMediaContent.Views.obj[1]
        with pytest.raises(ValueError, match="same document"):
            ann.set_rich_media_asset("foreign", foreign.pages[0].annotations[2].rich_media_assets["empty.u3d"])
        with pytest.raises(pdf.LimitError, match="4096"), ann.edit_rich_media() as media:
            media.RichMediaContent.obj["Configurations"] = [media.RichMediaContent.Configurations[0].obj] * 4097
        with pytest.raises(ValueError, match="repeated node"), ann.edit_rich_media() as media:  # noqa: PT012
            root = media.RichMediaContent.Assets
            assert root
            del root["Names"]
            root["Kids"] = [root]
        assert doc.history.position == pos


def test_split_refuses_before_publishing_under_a_small_document_budget() -> None:
    with pdf.open(rich_settings_pdf(), limits=pdf.DocumentLimits(max_memory_size=128 * 1024)) as doc:
        ann = doc.pages[0].annotations[2]
        files = ann.rich_media_assets
        pos = doc.history.position
        with pytest.raises(pdf.LimitError) as error:
            ann.set_rich_media_asset("new-model.u3d", files["model-00.u3d"])
        assert error.value.code == "memory_size"
        assert doc.history.position == pos
        assert ann.rich_media_assets == files


def test_edit_nested_tree_preserves_private_entries_and_untouched_leaf_bytes() -> None:
    with pdf.open(rich_settings_pdf()) as doc:
        ann = doc.pages[0].annotations[2]
        ann.set_rich_media_asset("model-extra.u3d", ann.rich_media_assets["model-00.u3d"])
        with ann.edit_rich_media() as media:
            root = media.RichMediaContent.Assets
            assert root
            left, right = root["Kids"]
            middle = doc.objects[
                doc.objects.add({"Kids": [left, right], "Limits": [left["Limits"][0], right["Limits"][1]]})
            ]
            root["Kids"] = [middle]
            middle["XXPrivate"] = 72
            right["XXPrivate"] = 53
        untouched = left.xref
        source = doc.to_bytes(incremental=True)
    with pdf.open(source) as doc:
        ann = doc.pages[0].annotations[2]
        media = ann.media
        assert isinstance(media, typed.AnnotRichMedia)
        root = media.RichMediaContent.Assets
        assert root
        before = raw_objects(doc)[untouched]
        ann.set_rich_media_asset("model-extra.u3d", None)
        assert root["Kids"][0]["XXPrivate"] == 72
        assert root["Kids"][0]["Kids"][1]["XXPrivate"] == 53
        assert root["Kids"][0]["Limits"][1] == b"start.js"
        ann.set_rich_media_asset("z-last", ann.rich_media_assets["model-00.u3d"])
        assert root["Kids"][0]["Limits"][1] == b"z-last"
        with pdf.open(doc.to_bytes(incremental=True)) as saved:
            assert raw_objects(saved)[untouched] == before


def test_full_kids_and_leaf_split_together_then_prune_an_empty_branch() -> None:
    """Insert into a full leaf of a 64-child root and delete the final name in another leaf."""
    with pikepdf.open(io.BytesIO(rich_settings_pdf())) as source:
        root = source.pages[0].Annots[2].RichMediaContent.Assets
        full = source.make_indirect(pikepdf.Dictionary(Names=root.Names, Limits=[root.Names[0], root.Names[-2]]))
        file = full.Names[1]
        leaves = [full]
        for i in range(63):
            key = pikepdf.String(f"z-{i:02}")
            leaves.append(source.make_indirect(pikepdf.Dictionary(Names=[key, file], Limits=[key, key])))
        del root.Names
        root.Kids = leaves
        data = io.BytesIO()
        source.save(data, deterministic_id=True)
    with pdf.open(data.getvalue()) as doc:
        ann = doc.pages[0].annotations[2]
        ann.set_rich_media_asset("more", ann.rich_media_assets["model-00.u3d"])
        ann.set_rich_media_asset("z-62", None)
        assert len(ann.rich_media_assets) == 127
        with pikepdf.open(io.BytesIO(doc.to_bytes())) as reader:
            root = reader.pages[0].Annots[2].RichMediaContent.Assets
            assert len(root.Kids) == 2
            assert root.Kids[-1].Limits[-1] == "z-61"
            for branch in root.Kids:
                assert list(branch.Limits) == [branch.Kids[0].Limits[0], branch.Kids[-1].Limits[-1]]
                assert all(leaf.Names for leaf in branch.Kids)


def test_removing_an_instance_targeted_by_a_shared_annotations_command_rolls_back() -> None:
    with pdf.open(rich_settings_pdf()) as doc:
        ann, sibling = doc.pages[0].annotations[2], doc.pages[0].annotations[-1]
        extra = ann.add_rich_media_configuration(
            "extra", kind=pdf.MediaKind.THREE_D, assets=(ann.rich_media_assets["model-00.u3d"],)
        )
        assert extra.Instances
        doc.open_action = pdf.RichMediaExecute(annotation=sibling.xref, command="select", instance=extra.Instances[0])
        pos = doc.history.position
        with pytest.raises(ValueError, match="still targets"), ann.edit_rich_media() as media:
            del media.RichMediaContent.Configurations.obj[2]
        assert doc.history.position == pos
        del doc.open_action
        with ann.edit_rich_media() as media:
            del media.RichMediaContent.Configurations.obj[2]
        assert len(media.RichMediaContent.Configurations.obj) == 2


@pytest.mark.parametrize("selector", [1, "side"])
def test_view_actions_keep_their_named_or_indexed_target_after_a_shared_edit(selector: int | str) -> None:
    with pdf.open(rich_settings_pdf()) as doc:
        ann, sibling = doc.pages[0].annotations[2], doc.pages[0].annotations[-1]
        doc.open_action = pdf.GoTo3DView(annotation=sibling.xref, view=selector)
        with sibling.edit_rich_media() as media:
            del media.RichMediaSettings
        with pytest.raises(ValueError, match="GoTo3DView"), ann.edit_rich_media() as media:  # noqa: PT012
            views = media.RichMediaContent.Views
            assert views
            del views.obj[1]
        assert sibling.rich_media_view == ann.rich_media_view


@pytest.mark.spec("rich-media", "read", "interpret")
def test_activation_selects_explicit_or_first_configuration_and_view() -> None:
    with pdf.open(rich_settings_pdf()) as doc:
        first, sibling = doc.pages[0].annotations[2], doc.pages[0].annotations[-1]
        assert sibling.rich_media_configuration.Name == "side"
        assert sibling.rich_media_view
        assert sibling.rich_media_view.IN == b"side"
        with sibling.edit_rich_media() as media:
            del media.RichMediaSettings
        assert sibling.rich_media_configuration == first.rich_media_configuration
        assert sibling.rich_media_view == first.rich_media_view
        with pytest.raises(ValueError, match="unique"), sibling.edit_rich_media() as media:  # noqa: PT012
            settings = typed.RichMediaSettings(doc.objects[doc.objects.add({})])
            media.RichMediaSettings = settings
            first.obj["RichMediaSettings"] = settings.obj
        assert sibling.rich_media_view == first.rich_media_view


def legacy_pdf() -> bytes:
    """Build a PDF 1.7 extension-level 3 annotation containing inert Flash binding parameters."""
    with pikepdf.open(io.BytesIO(media_pdf())) as doc:
        page = doc.pages[0]
        rich = page.Annots[2]
        page.Annots = [rich]
        content = rich.RichMediaContent
        del content.Views
        config = content.Configurations[0]
        del config.Subtype
        instance = config.Instances[0]
        instance.Subtype = pikepdf.Name.Flash
        instance.Params = pikepdf.Dictionary(Binding=pikepdf.Name.Material, BindingMaterial="display")
        file = instance.Asset
        file.F = "empty.swf"
        content.Assets.Names[0] = pikepdf.String("empty.swf")
        stream = file.EF.F
        stream.Subtype = pikepdf.Name("/application/x-shockwave-flash")
        # A SWF header, empty frame rectangle and End tag, with no actions.
        stream.write(bytes.fromhex("46575309100000000800000c00000000"))
        doc.trailer.ID = [b"richmedia-sample", b"richmedia-sample"]
        out = io.BytesIO()
        doc.save(out, force_version=("1.7", 3), deterministic_id=True)
        return out.getvalue()


@pytest.mark.parametrize("incremental", [False, True])
def test_version_specific_instance_parameters_roundtrip(*, incremental: bool) -> None:
    with pdf.open(legacy_pdf()) as doc:
        ann = doc.pages[0].annotations[0]
        with ann.edit_rich_media() as media:
            instance = media.RichMediaContent.Configurations[0].Instances[0]
            assert instance.Params
            instance.Params.FlashVars = "mode=stored"
            instance.Params.BindingMaterial = "surface"
        with pikepdf.open(io.BytesIO(doc.to_bytes(incremental=incremental))) as independent:
            params = independent.pages[0].Annots[0].RichMediaContent.Configurations[0].Instances[0].Params
            assert str(params.BindingMaterial) == "surface"
            assert str(params.FlashVars) == "mode=stored"
        with pytest.raises(ValueError, match="BindingMaterial"), ann.edit_rich_media() as media:  # noqa: PT012
            instance = media.RichMediaContent.Configurations[0].Instances[0]
            assert instance.Params
            del instance.Params.BindingMaterial
        with pytest.raises(ValueError, match="must remain named"):
            ann.set_rich_media_asset("empty.swf", None)
    with pdf.open(media_pdf()) as doc:
        ann = doc.pages[0].annotations[2]
        pos = doc.history.position
        with pytest.raises(ValueError, match="Params require"), ann.edit_rich_media() as media:  # noqa: PT012
            instance = media.RichMediaContent.Configurations[0].Instances[0]
            instance.Params = typed.RichMediaParams(doc.objects[doc.objects.add({})])
        assert doc.history.position == pos
