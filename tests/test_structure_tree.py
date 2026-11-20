"""``Document.structure``: reading and accessibility editing of structure trees and role maps."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import pyropdf
from pyropdf import (
    Color,
    DocumentClosedError,
    LimitError,
    MarkedContentReference,
    Name,
    ObjectReference,
    PdfError,
    RoleMap,
    StaleObjectError,
    StalePageError,
    StructureChildren,
    StructureElement,
    StructureTree,
    TagRole,
)

ASSETS = Path(__file__).resolve().parent / "assets"
PDFUA_1 = ASSETS / "pdfua" / "pdfua-1.pdf"


def _assert_valid(doc: pyropdf.Document) -> None:
    """Assert zero structural defects both in memory (aside from unsaved /Size) and after saving."""
    assert [d for d in doc.check().defects if d.clause != "FileTrailer/Size"] == []
    assert not pyropdf.open(doc.to_bytes()).check()


def _minimal_pdf_with_version(version: str) -> bytes:
    """Build a minimal valid 1-page PDF declaring header ``%PDF-{version}``."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << >> >>",
    ]
    out = bytearray(f"%PDF-{version}\n%\xe2\xe3\xcf\xd3\n".encode("latin-1"))
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_pos = len(out)
    size = len(objects) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref_pos)
    return bytes(out)


@pytest.mark.spec("logical-structure", "read")
@pytest.mark.spec("tagged-pdf", "read")
@pytest.mark.spec("accessibility", "read")
def test_read_pdfua_structure_tree() -> None:
    doc = pyropdf.open(PDFUA_1)
    tree = doc.structure
    assert isinstance(tree, StructureTree)
    assert tree.obj is not None
    assert len(tree) == 1
    assert repr(tree).startswith("StructureTree([StructureElement(")

    root_elem = tree[0]
    assert tree[-1] == root_elem
    assert tree[:] == [root_elem]
    assert list(tree) == [root_elem]
    assert isinstance(root_elem, StructureElement)
    assert root_elem.document is doc
    assert root_elem.role == "Document"
    assert root_elem.standard_role == "Document"
    assert root_elem.parent is None
    assert root_elem.page is None
    assert root_elem.alt is None
    assert root_elem.title is None
    assert root_elem.language is None
    assert root_elem.actual_text is None
    assert root_elem.expanded is None

    children = root_elem.children
    assert isinstance(children, StructureChildren)
    assert len(children) == 1
    figure = children[0]
    assert children[-1] == figure
    assert children[:] == [figure]
    assert figure.role == "Figure"
    assert figure.parent == root_elem
    assert figure.page == doc.pages[0]
    assert figure.alt == "A blue rectangle crossed by a red line"
    assert len(figure.children) == 0
    assert figure.items == (MarkedContentReference(mcid=0, page=0, stream_xref=None),)
    assert root_elem.items == (figure,)
    assert list(tree.walk()) == [root_elem, figure]
    assert len({root_elem, tree[0], figure}) == 2
    assert not doc.check()


def test_untagged_document_and_closed_document() -> None:
    doc = pyropdf.Document()
    doc.pages.new((200, 200))
    assert doc.structure.obj is None
    assert len(doc.structure) == 0
    assert list(doc.structure) == []
    assert list(doc.structure.walk()) == []
    assert dict(doc.structure.role_map) == {}
    assert doc.structure.resolve_role("Custom") == "Custom"
    del doc.structure
    del doc.structure.role_map
    _assert_valid(doc)

    doc.close()
    with pytest.raises(DocumentClosedError):
        _ = doc.structure


@pytest.mark.spec("accessibility", "create")
def test_canvas_tag_expanded_and_items_reading() -> None:
    doc = pyropdf.open(_minimal_pdf_with_version("1.4"))
    page = doc.pages[0]
    with page.draw() as canvas, canvas.tag(TagRole.PARAGRAPH, title="Intro", lang="en-US"):
        with canvas.tag(TagRole.SPAN, expanded="portable document format", actual_text="PDF"):
            canvas.text("PDF", (20, 40))
        with canvas.tag(TagRole.FIGURE, alt="Logo"):
            canvas.rect((20, 60, 50, 90), fill=Color(0, 0, 1))

    assert doc.catalog["Version"] == "1.5"
    _assert_valid(doc)

    para = doc.structure[0]
    assert para.role == "P"
    assert para.title == "Intro"
    assert para.language == "en-US"
    assert para.page == page

    span, fig = para.children
    assert span.role == "Span"
    assert span.expanded == "portable document format"
    assert span.actual_text == "PDF"
    assert fig.role == "Figure"
    assert fig.alt == "Logo"
    assert len(span.items) == 1
    mcr = span.items[0]
    assert isinstance(mcr, MarkedContentReference)
    assert mcr.mcid == 0
    assert mcr.page == 0
    assert mcr.stream_xref is not None

    reopened = pyropdf.open(doc.to_bytes())
    assert reopened.structure[0].children[0].expanded == "portable document format"
    assert not reopened.check()

    with page.draw() as canvas:
        with pytest.raises(TypeError):
            canvas.tag(TagRole.SPAN, expanded=123)  # ty: ignore[invalid-argument-type]
        with pytest.raises(LimitError) as exc_info:
            canvas.tag(TagRole.SPAN, expanded="x" * (16 * 1024 * 1024 + 1))
        assert exc_info.value.code == "text_input_size"


def test_object_reference_items_and_del_page_preserves_item_pg() -> None:
    doc = pyropdf.Document()
    page0 = doc.pages.new((200, 200))
    page1 = doc.pages.new((200, 200))
    note = page0.annotations.add(pyropdf.Note((20, 20), contents="See note"))
    note1 = page1.annotations.add(pyropdf.Note((30, 30), contents="Other page"))

    link_elem = doc.structure.add(TagRole.LINK, page=page0)
    link_elem.obj["K"] = [
        {"Type": Name("OBJR"), "Obj": note.obj},
        {"Type": Name("OBJR"), "Obj": note1.obj, "Pg": page1.obj},
    ]
    _assert_valid(doc)
    assert link_elem.items == (
        ObjectReference(xref=note.xref, page=0),
        ObjectReference(xref=note1.xref, page=1),
    )

    # Deleting elem.page copies the old /Pg onto child OBJR/MCR dictionaries that relied on parent::Pg.
    del link_elem.page
    assert link_elem.page is None
    assert link_elem.items == (
        ObjectReference(xref=note.xref, page=0),
        ObjectReference(xref=note1.xref, page=1),
    )
    _assert_valid(doc)


@pytest.mark.spec("logical-structure", "edit")
@pytest.mark.spec("tagged-pdf", "edit")
@pytest.mark.spec("accessibility", "edit")
def test_element_property_edits_validation_undo_redo_and_saves() -> None:
    doc = pyropdf.open(_minimal_pdf_with_version("1.3"))
    elem = doc.structure.add(TagRole.PARAGRAPH, page=0)
    assert doc.catalog["Version"] == "1.4"
    _assert_valid(doc)

    elem.role = "Callout"
    assert elem.role == "Callout"
    elem.role = TagRole.HEADING_2
    assert elem.role == "H2"

    elem.title = "Chapter 1"
    elem.language = "sr-Latn-RS"
    elem.alt = "Heading alt"
    elem.actual_text = "Chapter One"
    elem.expanded = "Chapter Number One"
    assert doc.catalog["Version"] == "1.5"
    assert elem.title == "Chapter 1"
    assert elem.language == "sr-Latn-RS"
    assert elem.alt == "Heading alt"
    assert elem.actual_text == "Chapter One"
    assert elem.expanded == "Chapter Number One"
    _assert_valid(doc)

    doc.history.undo()
    assert elem.expanded is None
    doc.history.redo()
    assert elem.expanded == "Chapter Number One"

    for incremental in (False, True):
        saved = pyropdf.open(doc.to_bytes(incremental=incremental))
        re_elem = saved.structure[0]
        assert (
            re_elem.role,
            re_elem.title,
            re_elem.language,
            re_elem.alt,
            re_elem.actual_text,
            re_elem.expanded,
        ) == ("H2", "Chapter 1", "sr-Latn-RS", "Heading alt", "Chapter One", "Chapter Number One")
        assert not saved.check()

    del elem.title
    del elem.language
    del elem.alt
    del elem.actual_text
    del elem.expanded
    assert (elem.title, elem.language, elem.alt, elem.actual_text, elem.expanded) == (None, None, None, None, None)
    _assert_valid(doc)


def test_element_property_and_page_validation_errors() -> None:
    doc = pyropdf.Document()
    doc.pages.new((200, 200))
    elem = doc.structure.add(TagRole.PARAGRAPH, page=0)

    with pytest.raises(TypeError):
        elem.language = 42  # ty: ignore[invalid-assignment]
    with pytest.raises(PdfError):
        elem.language = "bad_tag!"
    with pytest.raises(TypeError):
        elem.alt = None  # ty: ignore[invalid-assignment]
    with pytest.raises(LimitError) as exc_info:
        elem.title = "a" * (16 * 1024 * 1024 + 1)
    assert exc_info.value.code == "text_input_size"
    with pytest.raises(TypeError):
        elem.role = 99  # ty: ignore[invalid-assignment]
    with pytest.raises(ValueError, match="printable"):
        elem.role = "/H1"
    with pytest.raises(ValueError, match="1 to 127"):
        elem.role = ""

    page1 = doc.pages.new((200, 200))
    elem.page = -1
    assert elem.page == page1
    other_doc = pyropdf.Document()
    other_page = other_doc.pages.new((100, 100))
    with pytest.raises(ValueError, match="another document"):
        elem.page = other_page
    with pytest.raises(IndexError):
        elem.page = 5
    with pytest.raises(TypeError):
        elem.page = True
    del doc.pages[1]
    with pytest.raises(StalePageError):
        elem.page = page1
    _assert_valid(doc)


def test_role_map_mutations_cycle_refusal_and_resolution() -> None:
    doc = pyropdf.Document()
    doc.pages.new((200, 200))
    rm = doc.structure.role_map
    assert isinstance(rm, RoleMap)
    assert len(rm) == 0

    rm["Callout"] = TagRole.NOTE
    rm["WarningBox"] = "Callout"
    assert dict(rm) == {"Callout": "Note", "WarningBox": "Callout"}
    assert repr(rm) == "RoleMap({'Callout': 'Note', 'WarningBox': 'Callout'})"
    assert doc.structure.resolve_role("WarningBox") == "Note"

    elem = doc.structure.add("WarningBox")
    assert elem.role == "WarningBox"
    assert elem.standard_role == "Note"
    _assert_valid(doc)

    # Self-mapping and multi-hop cycles are refused without changing the document.
    with pytest.raises(ValueError, match="cycle"):
        rm["Self"] = "Self"
    with pytest.raises(ValueError, match="cycle"):
        rm["Note"] = "WarningBox"
    with pytest.raises(ValueError, match="cycle"):
        doc.structure.role_map = {"A": "B", "B": "C", "C": "A"}
    assert dict(doc.structure.role_map) == {"Callout": "Note", "WarningBox": "Callout"}

    # Deleting keys removes /RoleMap once empty.
    del rm["WarningBox"]
    assert "RoleMap" in doc.structure.obj  # ty: ignore[unsupported-operator]
    del rm["Callout"]
    assert "RoleMap" not in doc.structure.obj  # ty: ignore[unsupported-operator]
    with pytest.raises(KeyError):
        del rm["Callout"]
    _assert_valid(doc)

    # Undo restores Callout; replacement and del doc.structure.role_map work in single transactions.
    doc.history.undo()
    assert dict(doc.structure.role_map) == {"Callout": "Note"}
    doc.structure.role_map = {"Lead": TagRole.PARAGRAPH, "Hero": "Lead"}
    assert elem.standard_role == "WarningBox"
    assert doc.structure.resolve_role("Hero") == "P"
    del doc.structure.role_map
    assert dict(doc.structure.role_map) == {}
    _assert_valid(doc)

    # Malformed non-Name entries and pre-existing cycles in /RoleMap are handled safely on read.
    root = doc.structure.obj
    assert root is not None
    root["RoleMap"] = {"A": Name("B"), "B": Name("A"), "Bad": "not-a-name"}
    assert dict(doc.structure.role_map) == {"A": "B", "B": "A"}
    assert doc.structure.resolve_role("A") in {"A", "B"}

    chain = {f"R{i}": Name(f"R{i + 1}") for i in range(33)}
    root["RoleMap"] = chain
    with pytest.raises(LimitError) as exc_info:
        doc.structure.resolve_role("R0")
    assert exc_info.value.code == "structure_tree_count"


def test_reordering_and_reparenting_preserve_non_element_items_and_refuse_cycles() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((200, 200))
    sec_a = doc.structure.add(TagRole.SECTION, title="A")
    sec_b = doc.structure.add(TagRole.SECTION, title="B")
    p1 = sec_a.children.add(TagRole.PARAGRAPH, page=page, title="P1")
    p2 = sec_a.children.add(TagRole.PARAGRAPH, page=page, title="P2")
    p3 = sec_a.children.add(TagRole.PARAGRAPH, page=page, title="P3")

    # Interleave raw MCIDs around the child elements of sec_a: [0, p1, 1, p2, p3, 2].
    sec_a.obj["K"] = [0, p1.obj, 1, p2.obj, p3.obj, 2]
    sec_a.page = page

    sec_a.children.move(2, 0)
    assert [c.title for c in sec_a.children] == ["P3", "P1", "P2"]
    assert sec_a.items == (
        MarkedContentReference(mcid=0, page=0),
        p3,
        MarkedContentReference(mcid=1, page=0),
        p1,
        p2,
        MarkedContentReference(mcid=2, page=0),
    )
    _assert_valid(doc)

    # Reorder top-level elements and undo.
    doc.structure.move(1, 0)
    assert [e.title for e in doc.structure] == ["B", "A"]
    doc.history.undo()
    assert [e.title for e in doc.structure] == ["A", "B"]

    # Cross-parent move updates both /K arrays and /P in one transaction.
    p1.move(parent=sec_b, index=0)
    assert p1.parent == sec_b
    assert [c.title for c in sec_a.children] == ["P3", "P2"]
    assert [c.title for c in sec_b.children] == ["P1"]
    _assert_valid(doc)

    doc.history.undo()
    assert p1.parent == sec_a
    assert [c.title for c in sec_a.children] == ["P3", "P1", "P2"]

    # Move to top-level (/StructTreeRoot) and same-parent move via elem.move.
    p2.move(parent=None, index=1)
    assert p2.parent is None
    assert [e.title for e in doc.structure] == ["A", "P2", "B"]
    p2.move(parent=None, index=0)
    assert [e.title for e in doc.structure] == ["P2", "A", "B"]
    _assert_valid(doc)

    # Refuse cycles and cross-document moves.
    sub = p1.children.add(TagRole.SPAN, title="Sub")
    with pytest.raises(ValueError, match="itself"):
        sec_a.move(parent=sec_a)
    with pytest.raises(ValueError, match="descendant"):
        sec_a.move(parent=sub)
    other = pyropdf.Document()
    other_elem = other.structure.add(TagRole.DIV)
    with pytest.raises(ValueError, match="another document"):
        p1.move(parent=other_elem)
    _assert_valid(doc)


def _build_multi_leaf_tagged_pdf() -> bytes:
    seed = pyropdf.Document()
    page = seed.pages.new((300, 300))
    with page.draw() as canvas:
        with canvas.tag(TagRole.HEADING_1, title="H"):
            canvas.text("Heading", (20, 40))
        with canvas.tag(TagRole.PARAGRAPH, title="P"):
            canvas.text("Body", (20, 80))

    seed_h, seed_p = seed.structure
    seed_root = seed.structure.obj
    assert seed_root is not None
    seed_h.obj["ID"] = b"h-1"
    seed_p.obj["ID"] = b"p-1"
    leaf1 = seed.objects[seed.objects.add({"Limits": [0, 0], "Nums": [0, [seed_h.obj, seed_p.obj]]})]
    leaf2 = seed.objects[seed.objects.add({"Limits": [1, 2], "Nums": [1, seed_h.obj, 2, seed_p.obj]})]
    seed_root["ParentTree"] = seed.objects[seed.objects.add({"Kids": [leaf1, leaf2]})]
    seed_root["ParentTreeNextKey"] = 3
    seed_root["IDTree"] = seed.objects[seed.objects.add({"Names": [b"h-1", seed_h.obj, b"p-1", seed_p.obj]})]
    return seed.to_bytes()


def test_deletion_cleans_parent_tree_id_tree_and_restores_stale_handles_on_undo() -> None:
    doc = pyropdf.open(_build_multi_leaf_tagged_pdf())
    assert not doc.check()
    heading, paragraph = doc.structure
    heading_xref = heading.xref
    para_xref = paragraph.xref
    root = doc.structure.obj
    assert root is not None
    leaf1, leaf2 = root["ParentTree"]["Kids"]

    # Delete heading: leaf1[0] keeps [None, paragraph], leaf2 drops key 1 and updates /Limits to [2, 2],
    # IDTree drops "h-1", and heading's xref is freed!
    del doc.structure[0]
    assert len(doc.structure) == 1
    assert doc.structure[0] == paragraph
    assert heading_xref not in doc.objects
    assert list(leaf2["Limits"]) == [2, 2]
    assert list(leaf1["Nums"][1]) == [None, paragraph.obj]
    assert [x for x in root["IDTree"]["Names"] if isinstance(x, pyropdf.String)] == [b"p-1"]
    assert repr(heading) == f"<stale StructureElement xref={heading_xref}>"
    with pytest.raises(StaleObjectError):
        _ = heading.role
    with pytest.raises(StaleObjectError):
        heading.title = "Stale"
    assert not doc.check()

    # Undo restores heading and all tree entries; redo + deleting paragraph removes empty trees.
    doc.history.undo()
    assert heading.role == "H1"
    assert len(doc.structure) == 2
    assert not doc.check()

    doc.history.redo()
    del doc.structure[0]
    assert len(doc.structure) == 0
    assert para_xref not in doc.objects
    assert "ParentTree" not in root
    assert "IDTree" not in root
    assert not doc.check()


def test_delete_whole_structure_tree_and_undo() -> None:
    doc = pyropdf.open(_build_multi_leaf_tagged_pdf())
    heading, paragraph = doc.structure
    heading_xref = heading.xref
    para_xref = paragraph.xref
    root_obj = doc.structure.obj
    assert root_obj is not None
    root_xref = root_obj.xref

    del doc.structure
    assert doc.structure.obj is None
    assert root_xref not in doc.objects
    assert heading_xref not in doc.objects
    assert para_xref not in doc.objects
    with pytest.raises(StaleObjectError):
        _ = paragraph.role
    assert not doc.check()

    doc.history.undo()
    assert len(doc.structure) == 2
    assert heading.role == "H1"
    assert paragraph.role == "P"
    assert not doc.check()


def test_depth_element_limits_and_cyclic_graph_resilience() -> None:
    doc = pyropdf.Document()
    # Root is level 1; build down to level 256 (255 nested elements).
    cur = doc.structure.add("Document")
    for _ in range(254):
        cur = cur.children.add(TagRole.DIV)
    assert len(list(doc.structure.walk())) == 255

    # Adding a 256th element level (level 257 counting root) refuses with structure_tree_count.
    with pytest.raises(LimitError) as exc_info:
        cur.children.add(TagRole.SPAN)
    assert exc_info.value.code == "structure_tree_count"
    assert len(list(doc.structure.walk())) == 255

    # Moving a 2-level subtree under level 255 (255 + 2 == 257 > 256) also refuses cleanly.
    branch = doc.structure.add(TagRole.SECTION)
    branch.children.add(TagRole.PARAGRAPH)
    parent_255 = cur.parent
    assert parent_255 is not None
    with pytest.raises(LimitError) as exc_info:
        branch.move(parent=parent_255)
    assert exc_info.value.code == "structure_tree_count"

    # max_elements cap refuses with no partial output.
    with pytest.raises(LimitError) as exc_info:
        doc.structure.walk(max_elements=10)
    assert exc_info.value.code == "object_count"
    assert len(list(doc.structure.walk(max_elements=None))) == 257

    with pytest.raises(TypeError):
        doc.structure.walk(max_elements=True)
    with pytest.raises(ValueError, match="positive integer"):
        doc.structure.walk(max_elements=0)

    # Malformed 257-level chain in raw objects refuses in walk() with no partial output.
    extra_xref = doc.objects.add({"Type": Name("StructElem"), "S": Name("Span"), "P": cur.obj})
    cur.obj["K"] = [doc.objects[extra_xref]]
    with pytest.raises(LimitError) as exc_info:
        doc.structure.walk()
    assert exc_info.value.code == "structure_tree_count"

    # Malformed cyclic /K graph terminates without recursion or hang in walk() and deletion.
    cyclic = pyropdf.Document()
    a = cyclic.structure.add(TagRole.DIV, title="A")
    b = a.children.add(TagRole.DIV, title="B")
    b.obj["K"] = [a.obj, cyclic.structure.obj]
    assert [e.title for e in cyclic.structure.walk()] == ["A", "B"]
    del cyclic.structure[0]
    assert len(cyclic.structure) == 0


def test_slice_deletion_indirect_containers_and_pdfua_direct_check() -> None:
    # Direct `assert not doc.check()` on opened pdfua-1.pdf across accessibility and role-map edits.
    ua = pyropdf.open(PDFUA_1)
    ua.structure.role_map.update({"Diagram": TagRole.FIGURE}, Callout=TagRole.NOTE)
    assert dict(ua.structure.role_map) == {"Diagram": "Figure", "Callout": "Note"}
    fig = ua.structure[0].children[0]
    fig.role = "Diagram"
    assert fig.standard_role == "Figure"
    fig.alt = "Accessible vector graphic"
    fig.language = "en-US"
    fig.title = "Figure 1"
    fig.actual_text = "Rectangle and diagonal line"
    fig.expanded = "Figure One"
    assert not ua.check()

    ua.structure.role_map.clear()
    assert dict(ua.structure.role_map) == {}
    fig.role = TagRole.FIGURE
    assert not ua.check()

    # Slice deletion on children and top-level elements, plus indirect RoleMap and indirect /K cleanup.
    seed = pyropdf.Document()
    seed.pages.new((200, 200))
    s1 = seed.structure.add(TagRole.SECTION, title="S1")
    seed.structure.add(TagRole.SECTION, title="S2")
    seed.structure.add(TagRole.SECTION, title="S3")
    for idx in range(4):
        s1.children.add(TagRole.PARAGRAPH, title=f"P{idx}")
    rm_xref = seed.objects.add({"Hero": Name("H1")})
    seed_root = seed.structure.obj
    assert seed_root is not None
    seed_root["RoleMap"] = seed.objects[rm_xref]
    k_xref = seed.objects.add([c.obj for c in s1.children])
    s1.obj["K"] = seed.objects[k_xref]

    doc = pyropdf.open(seed.to_bytes())
    assert not doc.check()
    sec1 = doc.structure[0]
    doc_root = doc.structure.obj
    assert doc_root is not None
    indirect_k_xref = sec1.obj["K"].xref
    indirect_rm_xref = doc_root["RoleMap"].xref
    assert indirect_k_xref is not None
    assert indirect_rm_xref is not None

    del sec1.children[1:3]
    assert [c.title for c in sec1.children] == ["P0", "P3"]
    assert indirect_k_xref not in doc.objects
    assert not doc.check()

    del doc.structure.role_map
    assert indirect_rm_xref not in doc.objects
    assert not doc.check()

    del doc.structure[1:]
    assert [e.title for e in doc.structure] == ["S1"]
    assert not doc.check()


def _verapdf_ua1_command(pdf_path: Path) -> list[str] | None:
    jar_env = os.environ.get("VERAPDF_JAR")
    if jar_env and Path(jar_env).is_file() and shutil.which("java"):
        return [
            "java",
            "-cp",
            jar_env,
            "org.verapdf.apps.GreenfieldCliWrapper",
            "--flavour",
            "ua1",
            "--format",
            "text",
            str(pdf_path),
        ]
    exe = shutil.which("verapdf")
    if exe:
        return [exe, "--flavour", "ua1", "--format", "text", str(pdf_path)]
    return None


def test_edited_pdfua1_passes_verapdf_when_available(tmp_path: Path) -> None:
    target = tmp_path / "edited-ua1.pdf"
    cmd = _verapdf_ua1_command(target)
    if cmd is None:
        pytest.skip("veraPDF is not installed")

    doc = pyropdf.open(PDFUA_1)
    doc.structure.role_map["Diagram"] = TagRole.FIGURE
    figure = doc.structure[0].children[0]
    figure.role = "Diagram"
    figure.alt = "Updated accessible diagram description"
    figure.language = "en-US"
    figure.expanded = "Diagrammatic figure"
    assert not doc.check()
    doc.save(target)

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603 - fixed veraPDF CLI on test output.
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "PASS" in result.stdout, f"{result.stdout}\n{result.stderr}"
