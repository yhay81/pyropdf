"""Tests for developer extensions, associated files, page-piece data, document parts, and structure namespaces."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import pyropdf
from pyropdf import (
    ISO_EXTENSIONS,
    MATHML_NAMESPACE,
    PDF_1_7_NAMESPACE,
    PDF_2_0_NAMESPACE,
    AssociatedFile,
    AssociatedFileRelationship,
    DeveloperExtension,
    DocumentPartAction,
    LimitError,
    Name,
    NavigationStep,
    Note,
    OutlineEntry,
    PageTrigger,
    PieceData,
    StaleObjectError,
)


def _assert_clean(doc: pyropdf.Document) -> None:
    """Assert zero structural defects in memory (aside from unsaved trailer /Size) and after saving."""
    assert [d for d in doc.check().defects if d.clause != "FileTrailer/Size"] == []
    with pyropdf.open(doc.to_bytes()) as saved:
        report = saved.check()
        assert report.defects == (), report.defects


# ---------------------------------------------------------------------------
# 1. Developer and ISO Extensions (§7.12, developer-extensions)
# ---------------------------------------------------------------------------


@pytest.mark.spec("developer-extensions", "read", "interpret", "create", "edit", "preserve")
def test_developer_and_iso_extensions_lifecycle() -> None:
    with pyropdf.Document() as doc:
        page = doc.pages.new()
        assert not doc.extensions
        assert len(doc.extensions) == 0
        assert doc.extensions.all() == ()

        # Single 1.7 vendor extension stays direct and unlocks ADBE_Extn3 features (e.g. Page /Tabs /A).
        adbe = DeveloperExtension("ADBE", "1.7", 3)
        doc.extensions.add(adbe)
        page.obj["Tabs"] = Name("A")
        assert bool(doc.extensions)
        assert len(doc.extensions) == 1
        assert "ADBE" in doc.extensions
        assert adbe in doc.extensions
        assert doc.extensions["ADBE"] == (adbe,)
        assert not doc.catalog["Extensions"].is_indirect
        assert not doc.catalog["Extensions"]["ADBE"].is_indirect
        _assert_clean(doc)

        # Canonical ISO/TS extensions auto-populate URL and ExtensionRevision and promote to PDF 2.0 + trailer /ID.
        iso_32001 = DeveloperExtension("ISO_", "2.0", 32001)
        iso_32002 = DeveloperExtension("ISO_", "2.0", 32002)
        assert iso_32001.url == ISO_EXTENSIONS[32001][0]
        assert iso_32001.extension_revision == ISO_EXTENSIONS[32001][1]
        doc.extensions.add(iso_32001)
        doc.extensions.add(iso_32002)
        doc.extensions.add(iso_32002)  # Idempotent duplicate add.

        assert doc.catalog["Version"] == Name("2.0")
        assert "ID" in doc.trailer
        assert doc.extensions["ISO_"] == (iso_32001, iso_32002)
        assert not doc.catalog["Extensions"]["ISO_"].is_indirect
        assert list(doc.extensions) == ["ADBE", "ISO_"]
        assert doc.extensions.all() == (adbe, iso_32001, iso_32002)
        _assert_clean(doc)

        saved = doc.to_bytes()

    with pyropdf.open(saved) as reopened:
        _assert_clean(reopened)
        assert reopened.extensions["ADBE"] == (adbe,)
        assert reopened.extensions["ISO_"] == (iso_32001, iso_32002)

        # Remove one ISO entry -> collapses from direct array back to direct dictionary.
        reopened.extensions.remove(iso_32001)
        assert reopened.extensions["ISO_"] == (iso_32002,)
        assert isinstance(reopened.catalog["Extensions"]["ISO_"], pyropdf.Dictionary)
        assert not reopened.catalog["Extensions"]["ISO_"].is_indirect
        _assert_clean(reopened)

        # Delete prefix and replace via property assignment.
        del reopened.extensions["ADBE"]
        assert "ADBE" not in reopened.extensions
        iso_24064 = DeveloperExtension("ISO_", "2.0", 24064)
        reopened.extensions = [iso_24064]
        assert reopened.extensions.all() == (iso_24064,)
        _assert_clean(reopened)

        # Removing the last entry deletes Catalog["Extensions"].
        del reopened.extensions
        assert "Extensions" not in reopened.catalog
        assert not reopened.extensions
        _assert_clean(reopened)


def test_developer_extension_validation() -> None:
    for code, (url, rev) in ISO_EXTENSIONS.items():
        ext = DeveloperExtension("ISO_", "2.0", code)
        assert ext.url == url
        assert ext.extension_revision == rev

    with pytest.raises(ValueError, match="prefix"):
        DeveloperExtension("Type", "1.7", 1)
    with pytest.raises(ValueError, match="prefix"):
        DeveloperExtension("BAD/NAME", "1.7", 1)
    with pytest.raises(ValueError, match="base_version"):
        DeveloperExtension("ACME", "1.6", 1)
    with pytest.raises(TypeError, match="extension_level"):
        DeveloperExtension("ACME", "1.7", extension_level=True)
    with pytest.raises(ValueError, match="ISO_ extensions require base_version"):
        DeveloperExtension("ISO_", "1.7", 32001)
    with pytest.raises(ValueError, match="ISO_ extension_level"):
        DeveloperExtension("ISO_", "2.0", 99999)
    with pytest.raises(ValueError, match="url must be"):
        DeveloperExtension("ISO_", "2.0", 32001, url="https://example.com/wrong")
    with pytest.raises(ValueError, match="extension_revision must be"):
        DeveloperExtension("ISO_", "2.0", 32001, extension_revision=":1999")


def test_developer_extensions_preserve_foreign_keys() -> None:
    iso_step = DeveloperExtension("ISO_", "2.0", 24064)
    iso_gltf = DeveloperExtension("ISO_", "2.0", 32007)
    with pyropdf.Document() as doc:
        doc.pages.new()
        doc.extensions.add(iso_step)
        doc.catalog["Extensions"]["ISO_"]["XXKeep"] = 17
        doc.extensions.add(iso_gltf)
        entries = doc.catalog["Extensions"]["ISO_"]
        assert isinstance(entries, pyropdf.Array)
        assert entries[0]["XXKeep"] == 17
        assert set(doc.extensions["ISO_"]) == {iso_step, iso_gltf}

        full_bytes = doc.to_bytes()
        with pyropdf.open(full_bytes) as reopened:
            assert reopened.catalog["Extensions"]["ISO_"][0]["XXKeep"] == 17
            reopened.extensions.remove(iso_gltf)
            assert reopened.catalog["Extensions"]["ISO_"]["XXKeep"] == 17
            inc_bytes = reopened.to_bytes(incremental=True)

        with pyropdf.open(inc_bytes) as inc:
            assert inc.catalog["Extensions"]["ISO_"]["XXKeep"] == 17
            assert inc.extensions["ISO_"] == (iso_step,)


# ---------------------------------------------------------------------------
# 2. Associated Files (§14.13, associated-files)
# ---------------------------------------------------------------------------


@pytest.mark.spec("associated-files", "read", "interpret", "create", "edit", "preserve")
def test_associated_files_across_all_hosts() -> None:  # noqa: PLR0915 - end-to-end lifecycle across all 5 hosts.
    with pyropdf.Document() as doc:
        page = doc.pages.new()
        annot = page.annotations.add(Note((72, 72), contents="Attached source"))
        elem = doc.structure.add("Document", page=page)
        part = doc.parts.add_part(page, dpm={"Part": "Main"})

        doc_af = doc.associated_files.add(
            "データの元.csv",
            b"id,amount\n1,42\n",
            media_type="text/csv",
            relationship=AssociatedFileRelationship.DATA,
            description="Invoice dataset",
            created=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
            modified="D:20260102030500Z",
        )
        page_af = page.associated_files.add(
            "page.xml",
            b"<page n='1'/>",
            media_type="application/xml",
            relationship=AssociatedFileRelationship.SOURCE,
        )
        elem_af = elem.associated_files.add(
            "alt.txt",
            b"Accessible plain text",
            media_type="text/plain",
            relationship=AssociatedFileRelationship.ALTERNATIVE,
        )
        annot_af = annot.associated_files.add(
            "supplement.json",
            b'{"note":true}',
            media_type="application/json",
            relationship=AssociatedFileRelationship.SUPPLEMENT,
        )
        part_af = part.associated_files.add(
            "form.fdf",
            b"%FDF-1.2\n%%EOF\n",
            media_type="application/vnd.fdf",
            relationship=AssociatedFileRelationship.FORM_DATA,
        )

        assert doc_af.filename == "データの元.csv"
        assert doc_af.data == b"id,amount\n1,42\n"
        assert doc_af.size == len(b"id,amount\n1,42\n")
        assert doc_af.media_type == "text/csv"
        assert doc_af.relationship is AssociatedFileRelationship.DATA
        assert doc_af.description == "Invoice dataset"
        assert doc_af.created == "D:20260102030405Z"
        assert doc_af.modified == "D:20260102030500Z"
        assert len(page.associated_files) == 1
        assert page_af in page.associated_files
        assert elem_af.relationship is AssociatedFileRelationship.ALTERNATIVE
        assert annot_af.relationship is AssociatedFileRelationship.SUPPLEMENT
        assert part_af.relationship is AssociatedFileRelationship.FORM_DATA
        _assert_clean(doc)

        # Edit properties in place.
        doc_af.filename = "updated.csv"
        doc_af.data = b"id,amount\n1,99\n2,100\n"
        doc_af.media_type = "text/plain"
        doc_af.relationship = AssociatedFileRelationship.ENCRYPTED_PAYLOAD
        assert "EP" in doc_af.obj
        _assert_clean(doc)
        doc_af.relationship = AssociatedFileRelationship.SCHEMA
        assert "EP" not in doc_af.obj
        doc_af.description = "Updated schema"
        del doc_af.description
        del doc_af.created
        doc_af.modified = "D:20260201000000Z"
        assert doc_af.description is None
        assert doc_af.created is None
        assert doc_af.size == len(b"id,amount\n1,99\n2,100\n")
        _assert_clean(doc)

        saved = doc.to_bytes()

    with pyropdf.open(saved) as reopened:
        _assert_clean(reopened)
        r_af = reopened.associated_files[0]
        assert r_af.filename == "updated.csv"
        assert r_af.data == b"id,amount\n1,99\n2,100\n"
        assert r_af.media_type == "text/plain"
        assert r_af.relationship is AssociatedFileRelationship.SCHEMA
        assert reopened.pages[0].associated_files[0].data == b"<page n='1'/>"
        assert reopened.structure[0].associated_files[0].data == b"Accessible plain text"
        assert reopened.pages[0].annotations[0].associated_files[0].data == b'{"note":true}'
        assert reopened.parts[0].associated_files[0].data == b"%FDF-1.2\n%%EOF\n"

        # Preserve unknown custom PDF /AFRelationship names on read.
        reopened.extensions.add(DeveloperExtension("C2PA", "2.0", 1))
        r_af.obj["AFRelationship"] = Name("C2PA_Manifest")
        assert r_af.relationship == "C2PA_Manifest"
        _assert_clean(reopened)

        # Deleting the last entry removes /AF cleanly and marks the handle stale until undo.
        del reopened.associated_files[0]
        assert "AF" not in reopened.catalog
        assert not reopened.associated_files
        with pytest.raises(StaleObjectError):
            _ = r_af.filename
        reopened.history.undo()
        assert r_af.filename == "updated.csv"
        reopened.associated_files.clear()
        assert "AF" not in reopened.catalog
        _assert_clean(reopened)


def test_associated_files_limits_and_pdfa3_version() -> None:
    with pyropdf.Document() as doc:
        doc.extensions.add(DeveloperExtension("ISO_19005_3", "1.7", 1))
        af = doc.associated_files.add("invoice.xml", b"<x/>", relationship=AssociatedFileRelationship.SOURCE)
        assert af.filename == "invoice.xml"
        assert doc.catalog.get("Version") != Name("2.0")
        _assert_clean(doc)

        with pytest.raises(LimitError) as exc_info:
            doc.associated_files.add("huge.bin", b"abcdef", max_size=3)
        assert exc_info.value.code == "embedded_file_size"


# ---------------------------------------------------------------------------
# 3. Page-Piece Dictionaries (§14.5, page-piece)
# ---------------------------------------------------------------------------


@pytest.mark.spec("page-piece", "read", "interpret", "create", "edit", "preserve")
def test_document_and_page_piece_info_lifecycle() -> None:
    with pyropdf.Document() as doc:
        page = doc.pages.new()
        blob_xref = doc.objects.add_stream(b"binary-private-state", {})
        blob = doc.objects[blob_xref]
        assert isinstance(blob, pyropdf.Stream)

        doc_entry = doc.piece_info.set(
            "ACME_Workflow",
            {"job": "J-100", "steps": [1, 2, 3], "mode": Name("CMYK"), "stream": blob},
            last_modified="D:20260315103000Z",
        )
        assert doc_entry.last_modified == "D:20260315103000Z"
        assert "Info" in doc.trailer
        assert doc.trailer["Info"]["ModDate"].text == "D:20260315103000Z"

        page_entry = page.piece_info.set("Illustrator", {"artboard": "A4", "rev": 7}, last_modified="D:20260315110000Z")
        assert page_entry.private == {"artboard": "A4", "rev": 7}
        assert page.obj["LastModified"].text == "D:20260315110000Z"
        assert len(doc.piece_info) == 1
        assert bool(page.piece_info)
        _assert_clean(doc)

        # Edit and replace via MutableMapping and property setter.
        page.piece_info["Illustrator"] = PieceData("D:20260316000000Z", {"artboard": "Letter", "rev": 8})
        assert page.obj["LastModified"].text == "D:20260316000000Z"
        _assert_clean(doc)

        saved = doc.to_bytes()

    with pyropdf.open(saved) as reopened:
        _assert_clean(reopened)
        wf = reopened.piece_info["ACME_Workflow"]
        assert wf.last_modified == "D:20260315103000Z"
        assert wf.private["job"] == "J-100"
        assert wf.private["steps"] == [1, 2, 3]
        assert wf.private["mode"] == Name("CMYK")
        assert isinstance(wf.private["stream"], pyropdf.Stream)
        assert wf.private["stream"].read() == b"binary-private-state"

        p0 = reopened.pages[0]
        assert p0.piece_info["Illustrator"].private == {"artboard": "Letter", "rev": 8}

        # Deleting the last entry removes /PieceInfo cleanly.
        del p0.piece_info["Illustrator"]
        assert "PieceInfo" not in p0.obj
        assert not p0.piece_info
        del reopened.piece_info
        assert "PieceInfo" not in reopened.catalog
        _assert_clean(reopened)


def test_piece_data_validation() -> None:
    with pytest.raises(TypeError, match="PDF date string"):
        PieceData(123)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="PDF date string"):
        PieceData("2026-01-01")

    with pyropdf.Document() as doc:
        cyclic: list[object] = []
        cyclic.append(cyclic)
        with pytest.raises(ValueError, match="cyclic"):
            doc.piece_info.set("CyclicApp", cyclic)


# ---------------------------------------------------------------------------
# 4. Document Parts and /GoToDp Actions (§14.12, §12.6.4.5)
# ---------------------------------------------------------------------------


@pytest.mark.spec("document-parts", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.spec("goto-dpart-actions", "read", "interpret", "create", "edit", "preserve")
def test_document_parts_and_gotodp_actions_across_all_surfaces() -> None:  # noqa: PLR0915 - full DPart/GoToDp roundtrip.
    with pyropdf.Document() as doc:
        pages = [doc.pages.new() for _ in range(4)]
        assert not doc.parts
        assert doc.parts.root is None

        root = doc.parts.ensure_root(record_level=1, node_names=["Batch", "Recipient"])
        _assert_clean(doc)
        assert doc.parts.record_level == 1
        assert doc.parts.node_names == ("Batch", "Recipient")

        part_a = root.add_child(pages[0], pages[1], dpm={"Recipient": "Alice", "Copies": 2})
        part_b = doc.parts.add_part(2, 3, dpm={"Recipient": "Bob"})
        assert root.children == (part_a, part_b)
        assert list(doc.parts) == [part_a, part_b]
        assert part_a.parent == root
        assert root.parent is None
        assert part_a.pages == (pages[0], pages[1])
        assert part_b.pages == (pages[2], pages[3])
        assert root.pages == tuple(pages)
        assert "Metadata" not in part_a.obj
        _assert_clean(doc)

        # Wire DocumentPartAction across links, outline, open_action, page triggers, and navigation steps.
        act_a = DocumentPartAction(part_a)
        act_b = DocumentPartAction(part_b)
        link = pages[0].links.add((36, 36, 180, 72), act_b)
        doc.outline = [
            OutlineEntry(1, "Alice Packet", act_a),
            OutlineEntry(1, "Bob Packet", act_b),
        ]
        doc.open_action = act_a
        pages[0].triggers[PageTrigger.OPEN] = act_b
        pages[0].navigation_steps = [NavigationStep(next_action=act_b, previous_action=act_a, duration=2.0)]

        assert link.target == act_b
        assert doc.outline[0].destination == act_a
        assert doc.open_action == act_a
        assert pages[0].triggers[PageTrigger.OPEN] == act_b
        assert pages[0].navigation_steps[0].next_action == act_b
        _assert_clean(doc)

        # Edit DPM, record_level, node_names, and targets.
        part_b.dpm = {"Recipient": "Bob Updated"}
        doc.parts.record_level = 2
        doc.parts.node_names = ["Group", "Record"]
        link.target = act_a
        doc.outline[1].destination = act_a
        _assert_clean(doc)

        saved = doc.to_bytes()

    with pyropdf.open(saved) as reopened:
        _assert_clean(reopened)
        r_a, r_b = reopened.parts[0], reopened.parts[1]
        assert r_a.dpm == {"Recipient": "Alice", "Copies": 2}
        assert r_b.dpm == {"Recipient": "Bob Updated"}
        assert reopened.parts.record_level == 2
        assert reopened.parts.node_names == ("Group", "Record")
        assert reopened.open_action == DocumentPartAction(r_a)
        assert reopened.pages[0].links[0].target == DocumentPartAction(r_a)
        assert reopened.outline[0].destination == DocumentPartAction(r_a)
        assert reopened.pages[0].triggers[PageTrigger.OPEN] == DocumentPartAction(r_b)
        assert reopened.pages[0].navigation_steps[0].next_action == DocumentPartAction(r_b)

        # Deleting page 1 shrinks r_a from 2 pages to 1 page without breaking anything.
        del reopened.pages[1]
        assert len(r_a.pages) == 1
        assert "End" not in r_a.obj
        _assert_clean(reopened)

        # Selecting only the remaining Bob pages prunes r_a AND scrubs GoToDp actions pointing to r_a!
        reopened.pages.select([1, 2])
        assert list(reopened.parts) == [r_b]
        with pytest.raises(StaleObjectError):
            _ = r_a.dpm
        _assert_clean(reopened)

        # Deleting the last surviving part removes Catalog["DPartRoot"] completely.
        r_b.delete()
        assert "DPartRoot" not in reopened.catalog
        assert not reopened.parts
        _assert_clean(reopened)


def test_dpart_rejects_streams_in_dpm_and_handles_cycles() -> None:
    with pyropdf.Document() as doc:
        p = doc.pages.new()
        stream = doc.objects[doc.objects.add_stream(b"x", {})]
        with pytest.raises(ValueError, match="cannot contain streams"):
            doc.parts.add_part(p, dpm={"bad": stream})

        part = doc.parts.add_part(p, dpm={"ok": 1})
        root = doc.parts.root
        assert root is not None
        # Inject a cycle in DParts to verify iterative cycle detection without recursion.
        part.obj["DParts"] = [root.obj]
        assert root.pages == ()


# ---------------------------------------------------------------------------
# 5. Structure Namespaces and ISO/TS 32005 (§14.7.4, structure-namespaces, namespace-inclusion)
# ---------------------------------------------------------------------------


@pytest.mark.spec("structure-namespaces", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.spec("namespace-inclusion", "read", "interpret", "create", "edit", "preserve")
def test_structure_namespaces_and_iso32005_inclusion() -> None:  # noqa: PLR0915 - full namespace/ISO 32005 roundtrip.
    assert PDF_1_7_NAMESPACE == "http://iso.org/pdf/ssn"
    assert PDF_2_0_NAMESPACE == "http://iso.org/pdf2/ssn"
    assert MATHML_NAMESPACE == "http://www.w3.org/1998/Math/MathML"

    with pyropdf.Document() as doc:
        page = doc.pages.new()
        schema_af = doc.associated_files.add(
            "callout.xsd",
            b"<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'/>",
            media_type="application/xml",
            relationship=AssociatedFileRelationship.SCHEMA,
        )

        ns20 = doc.structure.namespaces.add(PDF_2_0_NAMESPACE)
        ns17 = doc.structure.namespaces.add(PDF_1_7_NAMESPACE)
        ns_math = doc.structure.namespaces.add(MATHML_NAMESPACE)
        ns_custom = doc.structure.namespaces.add(
            "https://example.com/publishing/ns",
            schema=schema_af,
            role_map={
                "Callout": ("Aside", ns20),
                "LegacyIndex": ("Index", ns17),
                "BodyText": "P",
            },
        )

        assert len(doc.structure.namespaces) == 4
        assert PDF_2_0_NAMESPACE in doc.structure.namespaces
        assert ns_custom in doc.structure.namespaces
        assert doc.structure.namespaces.find(PDF_1_7_NAMESPACE) == ns17
        assert isinstance(ns_custom.schema, AssociatedFile)
        assert ns_custom.schema.filename == "callout.xsd"

        # ISO/TS 32005: mix PDF 2.0, PDF 1.7 (e.g. TOC/TOCI), MathML, and custom namespaces in one tree.
        doc_elem = doc.structure.add("Document", page=page, namespace=ns20)
        toc_elem = doc_elem.children.add("TOC", page=page, namespace=ns17)
        toci_elem = toc_elem.children.add("TOCI", page=page, namespace=ns17)
        callout_elem = doc_elem.children.add("Callout", page=page, namespace=ns_custom)
        legacy_elem = doc_elem.children.add("LegacyIndex", page=page, namespace=ns_custom)
        body_elem = doc_elem.children.add("BodyText", page=page, namespace=ns_custom)
        math_elem = doc_elem.children.add("math", page=page, namespace=ns_math)
        plain_elem = doc_elem.children.add("CustomPara", page=page)
        doc.structure.role_map["CustomPara"] = "P"

        assert doc_elem.resolved_role == ("Document", PDF_2_0_NAMESPACE)
        assert toc_elem.resolved_role == ("TOC", PDF_1_7_NAMESPACE)
        assert toci_elem.resolved_role == ("TOCI", PDF_1_7_NAMESPACE)
        assert callout_elem.resolved_role == ("Aside", PDF_2_0_NAMESPACE)
        assert legacy_elem.resolved_role == ("Index", PDF_1_7_NAMESPACE)
        assert body_elem.resolved_role == ("P", PDF_1_7_NAMESPACE)
        assert math_elem.resolved_role == ("math", MATHML_NAMESPACE)
        assert plain_elem.resolved_role == ("P", None)
        _assert_clean(doc)

        # Edit namespace URI, schema, role_map, and element namespace.
        ns_custom.uri = "https://example.com/publishing/v2"
        ns_custom.schema = "https://example.com/publishing/v2.xsd"
        ns_custom.role_map["PullQuote"] = ("BlockQuote", ns17)
        del ns_custom.role_map["BodyText"]
        body_elem.namespace = ns20
        body_elem.role = "P"
        assert body_elem.resolved_role == ("P", PDF_2_0_NAMESPACE)
        _assert_clean(doc)

        saved = doc.to_bytes()

    with pyropdf.open(saved) as reopened:
        _assert_clean(reopened)
        r_custom = reopened.structure.namespaces.find("https://example.com/publishing/v2")
        assert r_custom is not None
        assert r_custom.schema == "https://example.com/publishing/v2.xsd"
        r_doc = reopened.structure[0]
        r_toc, r_callout, r_legacy, r_body, r_math, r_plain = r_doc.children
        assert r_toc.resolved_role == ("TOC", PDF_1_7_NAMESPACE)
        assert r_callout.resolved_role == ("Aside", PDF_2_0_NAMESPACE)
        assert r_legacy.resolved_role == ("Index", PDF_1_7_NAMESPACE)
        assert r_body.resolved_role == ("P", PDF_2_0_NAMESPACE)
        assert r_math.resolved_role == ("math", MATHML_NAMESPACE)
        assert r_plain.resolved_role == ("P", None)

        # Deleting a namespace scrubs /NS from elements and removes /RoleMapNS when cleared.
        del r_custom.schema
        assert r_custom.schema is None
        r_custom.role_map.clear()
        assert "RoleMapNS" not in r_custom.obj
        r_custom.delete()
        assert r_callout.namespace is None
        with pytest.raises(StaleObjectError):
            _ = r_custom.uri
        reopened.structure.namespaces.clear()
        assert reopened.structure.obj is not None
        assert "Namespaces" not in reopened.structure.obj
        _assert_clean(reopened)


def test_namespace_role_map_rejects_cycles_and_deep_chains() -> None:
    with pyropdf.Document() as doc:
        ns_a = doc.structure.namespaces.add("https://example.com/a")
        ns_b = doc.structure.namespaces.add("https://example.com/b")
        ns_a.role_map["Alpha"] = ("Beta", ns_b)
        with pytest.raises(ValueError, match="cycle"):
            ns_b.role_map["Beta"] = ("Alpha", ns_a)

        chain = [doc.structure.namespaces.add(f"https://example.com/c{i}") for i in range(34)]
        for idx in range(32):
            chain[idx].role_map["Step"] = ("Step", chain[idx + 1])
        with pytest.raises(LimitError) as exc_info:
            chain[0].role_map["Start"] = ("Step", chain[0])
        assert exc_info.value.code == "structure_tree_count"
