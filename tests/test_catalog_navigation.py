"""Catalog resource and viewer navigation interoperability (ISO 32000-2, 7.7.4, 12.3 and 12.6)."""

from __future__ import annotations

import io

import pikepdf
import pytest

import pyropdf as p
from pyropdf import Name

RECT = (1, 1, 20, 20)


def _resource(doc: p.Document, kind: str) -> p.Dictionary:
    match kind:
        case "Templates":
            return doc.pages[0].obj
        case "AP":
            return doc.objects[
                doc.objects.add_stream(b"q Q", {"Type": Name("XObject"), "Subtype": Name("Form"), "BBox": [0, 0, 4, 4]})
            ]
        case "Renditions":
            spec = {"Type": Name("Rendition"), "S": Name("SR"), "R": []}
        case "AlternatePresentations":
            spec = {"Type": Name("SlideShow")}
        case _:
            spec = {
                "Type": Name("SpiderContentSet"),
                "S": Name("SPS"),
                "ID": b"capture",
                "O": [],
                "SI": {"AU": "https://example.org/"},
            }
    return doc.objects[doc.objects.add(spec)]


@pytest.mark.spec("name-dictionary", "interpret", "create", "edit")
@pytest.mark.parametrize("kind", ["AP", "Templates", "IDS", "URLS", "AlternatePresentations", "Renditions"])
@pytest.mark.parametrize("incremental", [False, True])
def test_resource_tree_read_create_edit(kind: str, *, incremental: bool) -> None:
    with p.Document() as source:
        source.pages.new()
        source.catalog["Other"] = b"unchanged"
        raw = source.to_bytes()
    with p.open(raw) as doc:
        values = doc.named_resources(kind)
        value = _resource(doc, kind)
        values[b"z"] = value
        values[b"\x00\xff"] = value
        del values[b"z"]
        assert list(values) == [b"\x00\xff"]
        with pytest.raises(KeyError):
            _ = values[b"unknown"]
        with pytest.raises(ValueError, match="resource object"):
            values[b"bad"] = 42
        saved = doc.to_bytes(incremental=incremental)
    with p.open(saved) as doc, pikepdf.open(io.BytesIO(saved), attempt_recovery=False) as other:
        resource = doc.named_resources(kind)[b"\x00\xff"]
        assert isinstance(resource, p.Dictionary)
        assert resource.xref == value.xref
        assert other.Root.Other == "unchanged"
        names = other.Root.Names["/" + kind].Names
        assert bytes(names[0]) == b"\x00\xff"
        assert names[1].objgen[0] == resource.xref


@pytest.mark.spec("name-dictionary", "interpret", "edit")
def test_resource_tree_preserves_shared_nodes_and_refuses_cycles() -> None:
    with p.Document() as doc:
        page = doc.pages.new().obj
        source = doc.objects[doc.objects.add({"Names": [b"a", page], "Limits": [b"a", b"a"]})]
        branch = doc.objects[doc.objects.add({"Kids": [source], "Extra": b"retain"})]
        doc.catalog["Names"] = {"Templates": branch, "Pages": branch}
        doc.named_resources("Templates")[b"b"] = page
        assert bytes(doc.catalog["Names"]["Templates"]["Extra"]) == b"retain"
        assert list(doc.named_pages) == ["a"]
        assert bytes(doc.catalog["Names"]["Pages"]["Kids"][0]["Names"][0]) == b"a"
        branch["Kids"] = [branch]
        doc.catalog["Names"]["Templates"] = branch
        before = doc.to_bytes()
        with pytest.raises(p.MalformedPdfError, match="cycle"):
            del doc.named_resources("Templates")[b"missing"]
        assert doc.to_bytes() == before


@pytest.mark.spec("destinations", "read", "interpret", "create", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_structure_destination_as_link_outline_and_named_resource(*, incremental: bool) -> None:
    with p.Document() as base:
        base.pages.new()
        base.pages.new()
        raw = base.to_bytes()
    with p.open(raw) as doc:
        element = doc.structure.add("P", page=doc.pages[1])
        dest = p.StructureDestination(
            element=element.xref, fallback=p.Destination(0), fit="XYZ", operands=(2, None, 1.5)
        )
        link = doc.pages[0].links.add(RECT, dest)
        doc.outline = [p.OutlineEntry(1, "Contents", dest)]
        doc.destinations.set_structure("contents", dest)
        original = doc.named_resources("Dests")[b"contents"]
        assert isinstance(original, p.Dictionary)
        original["Vendor"] = b"keep"
        dest = p.StructureDestination(element=element.xref, fallback=p.Destination(0))
        doc.destinations.set_structure("contents", dest)
        link.target = dest
        doc.outline[0].destination = dest
        named = doc.pages[0].links.add(RECT, p.Destination(0))
        named.obj["A"] = {"S": Name("GoTo"), "D": b"contents"}
        saved = doc.to_bytes(incremental=incremental)
    with p.open(saved) as doc, pikepdf.open(io.BytesIO(saved), attempt_recovery=False) as other:
        assert doc.destinations["contents"] == p.Destination(0)
        assert doc.destinations.structure("contents") == dest
        assert doc.outline[0].destination == dest
        assert [x.target for x in doc.pages[0].links] == [dest, dest]
        resolved = dest.resolve(doc)
        assert resolved is not None
        assert resolved.page == doc.pages[1]
        assert doc.destinations.structure("absent") is None
        assert p.StructureDestination(element=b"missing", fallback=p.Destination(0)).resolve(doc) is None
        assert other.Root.Names.Dests.Names[1].Vendor == "keep"
        assert other.pages[0].Annots[0].A.SD[0].objgen[0] == element.xref


@pytest.mark.spec("uri-actions", "read", "interpret", "create", "edit")
@pytest.mark.spec("launch-actions", "read", "interpret", "create", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_uri_and_windows_parameters_are_data(*, incremental: bool) -> None:
    with p.Document() as source:
        source.pages.new()
        raw = source.to_bytes()
    with p.open(raw) as doc:
        doc.catalog["URI"] = {"Extra": 7}
        doc.base_uri = "https://example.org/guide/"
        uri = p.UriAction(uri="figures/overview", is_map=True)
        windows = p.WindowsLaunch(
            file=b"notes-\xff.pdf",
            directory=b"documents",
            operation="print",
            parameters=b"notes",
            fallback_file="portable.pdf",
            new_window=True,
        )
        link = doc.pages[0].links.add(RECT, uri)
        doc.outline = [p.OutlineEntry(1, "Open", windows)]
        assert link.target == uri
        link.target = windows
        doc.outline[0].destination = uri
        doc.base_uri = "https://example.org/updated/"
        saved = doc.to_bytes(incremental=incremental)
    with p.open(saved) as doc, pikepdf.open(io.BytesIO(saved), attempt_recovery=False) as other:
        assert doc.pages[0].links[0].target == windows
        assert doc.outline[0].destination == uri
        assert (
            uri.resolve(doc, position=(12.4, 16.2), rectangle=RECT)
            == "https://example.org/updated/figures/overview?11,4"
        )
        assert bytes(other.pages[0].Annots[0].A.Win.F) == b"notes-\xff.pdf"
        assert str(other.Root.Outlines.First.A.IsMap).lower() == "true"
        del doc.base_uri
        assert doc.catalog["URI"]["Extra"] == 7
        assert (
            uri.resolve(doc, document_uri="https://example.org/docs/s.pdf")
            == "https://example.org/docs/figures/overview"
        )
        with pytest.raises(ValueError, match="finite"):
            uri.resolve(doc, position=(float("nan"), 1))
        assert p.WindowsLaunch(file=b"notes.pdf").effective_operation == "open"
        with pytest.raises(ValueError, match="operation"):
            p.WindowsLaunch(file=b"notes.pdf", operation="typo")
        doc.catalog["Version"] = Name("2.0")
        with pytest.raises(ValueError, match="deprecated"):
            doc.pages[0].links.add(RECT, windows)


@pytest.mark.spec("remote-goto-actions", "interpret")
@pytest.mark.spec("named-actions", "interpret")
def test_resolve_remote_and_named_actions_without_opening_any_files() -> None:
    with p.Document() as remote:
        remote.pages.new()
        remote.pages.new()
        remote.named_resources("Dests")[b"chapter"] = [remote.pages[1].obj, Name("Fit")]
        assert p.RemoteDestination("relative.pdf", name="chapter").resolve(remote) == p.Destination(1)
        assert p.RemoteDestination("relative.pdf", name="unknown").resolve(remote) is None
        assert p.RemoteDestination("relative.pdf", 0).resolve(remote) == p.Destination(0)
        assert p.RemoteDestination("relative.pdf", 2).resolve(remote) is None
        for action, page in (("FirstPage", 0), ("NextPage", 1), ("LastPage", 1), ("PrevPage", None), ("Private", None)):
            assert p.NamedAction(action).resolve(remote, current_page=0) == (
                p.Destination(page) if page is not None else None
            )
        link = remote.pages[0].links.add(RECT, p.RemoteDestination("relative.pdf", 0))
        link.obj["A"]["D"] = [Name("invalid"), Name("Fit")]
        assert link.target is None


@pytest.mark.spec("name-dictionary", "interpret", "edit")
@pytest.mark.parametrize("bad", ["bounds", "type", "length", "direct"])
def test_malformed_sibling_refuses_name_tree_edit_before_writing(bad: str) -> None:
    with p.Document() as doc:
        page = doc.pages.new().obj
        left = doc.objects[doc.objects.add({"Names": [b"a", page], "Limits": [b"a", b"a"]})]
        right: dict[str, object] = {
            "Names": [b"z", page],
            "Limits": {"bounds": [b"b", b"z"], "type": [4, b"z"], "length": [b"z"], "direct": [b"z", b"z"]}[bad],
        }
        sibling = right if bad == "direct" else doc.objects[doc.objects.add(right)]
        doc.catalog["Names"] = {"Templates": {"Kids": [left, sibling]}}
        before = doc.to_bytes()
        with pytest.raises(p.MalformedPdfError):
            doc.named_resources("Templates")[b"a"] = page
        assert doc.to_bytes() == before


@pytest.mark.spec("name-dictionary", "create", "edit")
@pytest.mark.parametrize("empty", ["Kids", "Names"])
def test_first_and_last_resource_in_empty_tree(empty: str) -> None:
    with p.Document() as doc:
        page = doc.pages.new().obj
        doc.catalog["Names"] = {"Templates": {empty: [], "Extra": 7}}
        resources = doc.named_resources("Templates")
        resources[b"first"] = page
        assert resources[b"first"] == page
        assert doc.catalog["Names"]["Templates"]["Extra"] == 7
        del resources[b"first"]
        assert "Names" not in doc.catalog


@pytest.mark.spec("destinations", "read", "interpret", "create", "edit")
@pytest.mark.spec("remote-goto-actions", "interpret")
def test_remote_structure_ids_use_the_supplied_documents_byte_index() -> None:
    target = p.RemoteDestination(
        "appendix.pdf", 0, structure_id=b"\x00\xff", structure_fit="FitH", structure_operands=(15,)
    )
    with p.Document() as doc:
        doc.pages.new().links.add(RECT, target)
        saved = doc.to_bytes()
    with p.open(saved) as doc, pikepdf.open(io.BytesIO(saved)) as independent:
        assert doc.pages[0].links[0].target == target
        assert bytes(independent.pages[0].Annots[0].A.SD[0]) == b"\x00\xff"
        assert target.resolve_structure(doc) is None
    with p.Document() as remote:
        page = remote.pages.new()
        elem = remote.structure.add("Sect", page=page)
        elem.obj["ID"] = b"\x00\xff"
        assert remote.structure.obj is not None
        remote.structure.obj["IDTree"] = {"Names": [b"\x00\xff", elem.obj]}
        assert target.resolve_structure(remote) == elem
        assert target.resolve(remote) == p.Destination(0)


@pytest.mark.spec("name-dictionary", "interpret", "create", "edit")
def test_retired_name_trees_remain_readable_in_pdf_2_but_new_entries_are_refused() -> None:
    with p.Document() as doc:
        doc.pages.new()
        capture = _resource(doc, "IDS")
        tree = doc.named_resources("IDS")
        tree[b"captured"] = capture
        doc.catalog["Version"] = Name("2.0")
        before = doc.to_bytes()
        with pytest.raises(ValueError, match="deprecated"):
            tree[b"another"] = capture
        assert doc.to_bytes() == before
        assert tree[b"captured"] == capture
        del tree[b"captured"]
        assert not tree


@pytest.mark.spec("launch-actions", "read", "interpret")
@pytest.mark.spec("uri-actions", "read", "interpret", "edit")
def test_malformed_platform_parameters_keep_the_portable_target_available() -> None:
    with p.Document() as doc:
        page = doc.pages.new()
        link = page.links.add(RECT, p.Launch("guide.pdf"))
        link.obj["A"]["Win"] = {"F": b"guide.pdf", "O": b"typo"}
        assert link.target == p.Launch("guide.pdf")
        link.target = "guide.pdf"
        link.obj["A"]["IsMap"] = 7
        assert link.target == "guide.pdf"
        doc.base_uri = "https://example.org/"
        with pytest.raises(ValueError, match="base_uri"):
            doc.base_uri = ""
        assert doc.base_uri == "https://example.org/"


@pytest.mark.spec("destinations", "read", "interpret", "edit")
def test_invalid_structure_views_fall_back_to_the_page() -> None:
    with p.Document() as doc:
        page = doc.pages.new()
        elem = doc.structure.add("Sect", page=page)
        link = page.links.add(RECT, p.StructureDestination(element=elem.xref, fallback=p.Destination(0)))
        link.obj["A"]["SD"] = [elem.obj, Name("FitR"), None, 1, 2, 3]
        assert link.target == p.Destination(0)
        with pytest.raises(ValueError, match="rectangle coordinates"):
            p.StructureDestination(element=elem.xref, fallback=p.Destination(0), fit="FitR", operands=(0, None, 2, 3))


@pytest.mark.spec("destinations", "create", "edit")
@pytest.mark.parametrize("holder", ["link", "outline", "open", "names"])
def test_refuse_incompatible_pdf2_navigation_promotion_without_changing_the_document(holder: str) -> None:
    from test_attachments import build_raw_pdf  # noqa: PLC0415

    legacy = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 50 50] /Resources << /Font << /F1 4 0 R >> >> >>",
            4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        }
    )
    with p.open(legacy) as doc:
        elem = doc.structure.add("Sect", page=doc.pages[0])
        target = p.StructureDestination(element=elem.xref, fallback=p.Destination(0))
        before = doc.to_bytes(incremental=True)
        history = doc.history.save()

        def write_target() -> None:
            match holder:
                case "link":
                    doc.pages[0].links.add(RECT, target)
                case "outline":
                    doc.outline = [p.OutlineEntry(1, "Section", target)]
                case "open":
                    doc.open_action = target
                case _:
                    doc.destinations.set_structure("section", target)

        with pytest.raises(p.PdfError, match=r"cannot promote.*FontType1"):
            write_target()
        assert doc.history.save() == history
        assert doc.to_bytes(incremental=True) == before
