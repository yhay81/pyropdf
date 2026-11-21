"""Standalone object authoring, incoming reference rules and saved wire values."""

from __future__ import annotations

import csv
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pikepdf
import pytest
from arlington_samples import SAMPLES, VERSIONS, object_pdf, sample_pdf

import pyropdf
from pyropdf import Array, Name, Stream, typed


def _independent(value: pikepdf.Object) -> tuple[object, bytes | None]:
    """Compare dictionary entries, scalar types, indirect identities and raw stream bytes."""
    return json.loads(value.to_json(dereference=True)), value.read_raw_bytes() if isinstance(
        value, pikepdf.Stream
    ) else None


_DEPRECATED = {
    "ActionNOP",
    "ActionSetState",
    "ActionMovie",
    "ActionSound",
    "AnnotMovie",
    "AnnotSound",
    "ArrayOfNamesForProcSet",
    "ArrayOfXFA",
    "Movie",
    "MovieActivation",
    "SlideShow",
    "SoundObject",
}


@pytest.mark.spec("arlington-authoring", "create", "edit")
@pytest.mark.parametrize("name", [name for name in SAMPLES if name not in _DEPRECATED])
def test_create_replace_and_remove_a_standalone_object(name: str) -> None:
    data = sample_pdf(name)
    cls = getattr(typed, "ThreeD" + name[2:] if name.startswith("3D") else name)
    with pikepdf.Pdf.open(io.BytesIO(data)) as independent:
        expected = _independent(independent.get_object((3, 0)))
    with pyropdf.open(data) as doc:
        before = doc.history.position
        src = doc.objects[3]
        with pytest.raises((pyropdf.PdfError, TypeError)):
            cls.create(doc, {} if isinstance(src, Array) else [])
        assert doc.history.position == before
        view = cls.create(doc, src)
        number = view.obj.xref
        assert number is not None
        assert number != 3
        assert view.obj.generation == 0
        view.replace(src)
        with pytest.raises((pyropdf.PdfError, TypeError)):
            view.replace({} if isinstance(src, Array) else [])
        doc.catalog["Authored"] = view.obj
        attached = doc.history.position
        with pytest.raises(pyropdf.PdfError, match="referenced"):
            view.remove()
        with pytest.raises(pyropdf.PdfError, match="referenced"):
            view.replace(src)
        assert doc.history.position == attached
        for incremental in (False, True):
            saved = doc.to_bytes(incremental=incremental)
            with pikepdf.Pdf.open(io.BytesIO(saved)) as independent:
                assert str(independent.Root.Sentinel) == "unchanged"
                obj = independent.Root.Authored
                assert obj.objgen == (number, 0)
                assert _independent(obj) == expected
            with pyropdf.open(saved) as reopened:
                assert isinstance(cls(reopened.catalog["Authored"]), cls)
        del doc.catalog["Authored"]
        view.remove()
        assert number not in doc.objects
        for incremental in (False, True):
            with pyropdf.open(doc.to_bytes(incremental=incremental)) as reopened:
                assert number not in reopened.objects
                assert reopened.catalog["Sentinel"].text == "unchanged"
                assert 3 in reopened.objects


@pytest.mark.spec("arlington-legacy-edits", "edit")
@pytest.mark.parametrize("name", sorted(_DEPRECATED))
def test_deprecated_objects_remain_editable_without_construction(name: str) -> None:
    data = sample_pdf(name)
    with pyropdf.open(data) as doc:
        cls = getattr(typed, name)
        original = doc.objects[3]
        with pytest.raises(pyropdf.PdfError, match="deprecated"):
            cls.create(doc, original)
        view = cls(original)
        view.replace(original)
        doc.catalog["Existing"] = view.obj
        with pytest.raises(pyropdf.PdfError, match="referenced"):
            view.remove()
        for incremental in (False, True):
            with pikepdf.Pdf.open(io.BytesIO(doc.to_bytes(incremental=incremental))) as saved:
                assert saved.Root.Existing.objgen == (3, 0)
                assert str(saved.Root.Sentinel) == "unchanged"
        del doc.catalog["Existing"]
        view.remove()
        assert 3 not in doc.objects


def test_required_indirect_links_reject_an_incomplete_or_stale_graph_atomically() -> None:
    doc = pyropdf.Document()
    original_count = len(doc.objects)
    with doc.transaction("outer"):
        doc.catalog["Unrelated"] = 42
        for invalid in ({}, {"Type": Name("Catalog"), "Pages": {"Type": Name("Pages"), "Kids": [], "Count": 0}}):
            with pytest.raises(pyropdf.PdfError):
                typed.Catalog.create(doc, invalid)
        assert len(doc.objects) == original_count
        assert doc.catalog["Unrelated"] == 42
    assert doc.history.steps[-1] == "outer"

    data = object_pdf(["<< /Type /Catalog /Pages 4 1 R >>", "<< /Type /Pages /Kids [] /Count 0 >>"])
    doc = pyropdf.open(data)
    with pytest.raises(pyropdf.PdfError, match="unresolved reference"):
        typed.Catalog.create(doc, doc.objects[3])


def test_a_simple_model_link_accepts_only_a_matching_replacement() -> None:
    doc = pyropdf.Document()
    entries = {"Type": Name("Metadata"), "Subtype": Name("XML")}
    metadata = typed.Metadata.create(doc, entries, data=b"<original/>")
    doc.catalog["Metadata"] = metadata.obj
    assert isinstance(metadata.obj, Stream)
    replacement = typed.Metadata.create(doc, entries, data=b"<replacement/>")
    metadata.replace(replacement)
    with pytest.raises(pyropdf.PdfError):
        metadata.replace({})
    assert metadata.obj.read() == b"<replacement/>"
    assert doc.catalog["Metadata"] == metadata.obj
    with pytest.raises(pyropdf.PdfError, match="referenced"):
        metadata.remove()


def test_coupled_page_and_structure_links_reject_generic_replacement_and_removal() -> None:
    doc = pyropdf.Document()
    page = doc.pages.new((100, 200))
    view = typed.PageObject(page.obj)
    before = doc.history.position
    for change in (lambda: view.replace(page.obj), view.remove):
        with pytest.raises(pyropdf.PdfError, match="referenced"):
            change()
    assert doc.history.position == before
    assert doc.pages[0].obj == page.obj
    del doc.pages[0]
    assert len(doc.pages) == 0


@pytest.mark.spec("PageObject", "create", "edit")
@pytest.mark.spec("PageTreeNode", "create", "edit")
def test_attach_pages_and_nested_nodes_atomically() -> None:
    doc = pyropdf.open(pyropdf.Document().to_bytes())
    root = doc.catalog["Pages"]
    entries = {"Type": Name("Page"), "Parent": root, "MediaBox": [0, 0, 150, 300], "Resources": {}}

    def attach_page(page: typed.PageObject) -> None:
        root["Kids"].append(page.obj)
        root["Count"] += 1

    with pytest.raises(pyropdf.PdfError, match="reciprocal"):
        typed.PageObject.create(doc, entries)
    assert not doc.pages
    page = typed.PageObject.create(doc, entries, attach=attach_page)

    def insert_parent(node: typed.PageTreeNode) -> None:
        root["Kids"][0] = node.obj
        page.obj["Parent"] = node.obj

    node = typed.PageTreeNode.create(
        doc, {"Type": Name("Pages"), "Parent": root, "Kids": [page.obj], "Count": 1}, attach=insert_parent
    )
    position = doc.history.position
    for owned in (page, node):
        with pytest.raises(pyropdf.PdfError, match="referenced"):
            owned.replace(owned.obj)
        with pytest.raises(pyropdf.PdfError, match="referenced"):
            owned.remove()
    assert doc.history.position == position
    for incremental in (False, True):
        with pikepdf.Pdf.open(io.BytesIO(doc.to_bytes(incremental=incremental))) as saved:
            parent = saved.Root.Pages.Kids[0]
            leaf = parent.Kids[0]
            assert parent.objgen == (node.obj.xref, 0)
            assert leaf.Parent.objgen == parent.objgen
            assert leaf.objgen == (page.obj.xref, 0)
            assert len(saved.pages) == 1


@pytest.mark.spec("StructElem", "create", "edit")
def test_attach_a_structure_element_and_reject_breaking_its_parent() -> None:
    doc = pyropdf.open(pyropdf.Document().to_bytes())
    root = doc.objects[doc.objects.add({"Type": Name("StructTreeRoot"), "K": []})]
    doc.catalog["StructTreeRoot"] = root
    data = {"Type": Name("StructElem"), "S": Name("P"), "P": root}
    with pytest.raises(pyropdf.PdfError, match="reciprocal"):
        typed.StructElem.create(doc, data)
    view = typed.StructElem.create(doc, data, attach=lambda item: root["K"].append(item.obj))
    before = doc.history.position
    with pytest.raises(pyropdf.PdfError, match="referenced"):
        view.remove()
    assert doc.history.position == before
    for incremental in (False, True):
        with pikepdf.Pdf.open(io.BytesIO(doc.to_bytes(incremental=incremental))) as saved:
            node = saved.Root.StructTreeRoot
            assert node.K[0].P.objgen == node.objgen
            assert str(node.K[0].S) == "/P"


@pytest.mark.spec("DPart", "create", "edit")
@pytest.mark.spec("DPartRoot", "create", "edit")
def test_attach_document_part_children_and_refuse_a_dangling_parent() -> None:
    doc = pyropdf.open(pyropdf.Document().to_bytes())
    doc.catalog["Version"] = Name("2.0")
    page = doc.pages.new((100, 100))

    def attach_root(tree: typed.DPartRoot) -> None:
        initial = doc.objects.add({"Type": Name("DPart"), "Parent": tree.obj, "Start": page.obj})
        tree.obj["DPartRootNode"] = doc.objects[initial]
        doc.catalog["DPartRoot"] = tree.obj

    tree = typed.DPartRoot.create(doc, {"Type": Name("DPartRoot")}, attach=attach_root)
    root = tree.obj
    values = {"Type": Name("DPart"), "Parent": root, "Start": page.obj}
    with pytest.raises(pyropdf.PdfError, match="reciprocal"):
        typed.DPart.create(doc, values)

    def attach(child: typed.DPart) -> None:
        root["DPartRootNode"] = child.obj

    leaf = typed.DPart.create(doc, values, attach=attach)
    with pytest.raises(pyropdf.PdfError, match="referenced"):
        leaf.remove()
    for incremental in (False, True):
        with pikepdf.Pdf.open(io.BytesIO(doc.to_bytes(incremental=incremental))) as saved:
            node = saved.Root.DPartRoot
            assert node.DPartRootNode.Parent.objgen == node.objgen
            assert node.DPartRootNode.Start.objgen == saved.pages[0].objgen


def test_failed_registration_rolls_back_the_new_object_and_other_writes() -> None:
    doc = pyropdf.Document()
    original_count = len(doc.objects)

    def reject(view: typed.DocInfo) -> None:
        doc.catalog["PartialAttachment"] = view.obj
        msg = "registration refused"
        raise ValueError(msg)

    with doc.transaction("outer"):
        doc.catalog["Keep"] = 5
        with pytest.raises(ValueError, match="registration refused"):
            typed.DocInfo.create(doc, {"Title": "New"}, attach=reject)
    assert doc.catalog["Keep"] == 5
    assert "PartialAttachment" not in doc.catalog
    assert len(doc.objects) == original_count


def test_field_parent_requires_a_reciprocal_kid() -> None:
    doc = pyropdf.Document()
    fields = doc.objects[doc.objects.add({"FT": Name("Tx"), "T": "parent", "Kids": [], "DA": "/Helv 12 Tf 0 g"})]
    child = {"FT": Name("Tx"), "Parent": fields, "T": "child", "DA": "/Helv 12 Tf 0 g"}
    with pytest.raises(pyropdf.PdfError, match="reciprocal"):
        typed.FieldTx.create(doc, child)
    view = typed.FieldTx.create(doc, child, attach=lambda item: fields["Kids"].append(item.obj))
    assert view.obj == fields["Kids"][0]
    with pytest.raises(pyropdf.PdfError, match="referenced"):
        view.remove()


def _roles() -> dict[str, list[dict[str, str]]]:
    """Read every named, wildcard, repeating and conditional role, plus unlinked fields."""
    result = subprocess.run(  # noqa: S603 - the generator and its arguments are fixed.
        [sys.executable, str(Path(__file__).resolve().parents[1] / "tools/arlington/generate.py"), "--roles"],
        check=True,
        capture_output=True,
        text=True,
    )
    roles: dict[str, list[dict[str, str]]] = {}
    for row in csv.DictReader(io.StringIO(result.stdout), delimiter="\t"):
        roles.setdefault(row["Holder"], []).append(row)
    return roles


_ROLES = _roles()


def test_the_role_inventory_covers_every_pinned_key_and_target() -> None:
    model = Path(__file__).resolve().parents[1] / "tools/arlington/model/tsv"
    assert set(_ROLES) == set(typed.TYPES)
    for path in sorted(model.glob("*.tsv")):
        with path.open(encoding="utf-8", newline="") as f:
            pinned = list(csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE))
        roles = _ROLES[path.stem]
        assert {r["Key"] for r in roles} == {r["Key"] for r in pinned}
        for row in pinned:
            matching = [r for r in roles if r["Key"] == row["Key"]]
            since = row["SinceVersion"]
            if path.stem == "MeasureGEO" and row["Key"] in {"Type", "Subtype"}:
                since = "fn:Eval(fn:Extension(ADBE_Extn3,1.7) || 2.0)"
            assert all(r["Required"] == row["Required"] and r["Since"] == since for r in matching)
            # Target names, including gated alternatives, come straight from
            # the vendored Link column. A missing wildcard or repeated slot
            # would otherwise hide an entire class of incoming references.
            targets = set(re.findall(r"[A-Za-z0-9_-]+", row["Link"])) & set(typed.TYPES)
            assert {r["Target"] for r in matching if r["Target"]} == targets


def _seed(name: str) -> list[str]:
    if name in SAMPLES:
        return SAMPLES[name]
    # The page, structure, document-part and article tests exercise the
    # full reciprocal graphs. These indirect values isolate a single incoming
    # edge in the reverse index, independent of other owners of real articles.
    return {
        "PageObject": ["<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>"],
        "PageTreeNode": ["<< /Type /Pages /Parent 2 0 R /Count 0 /Kids [] >>"],
        "StructElem": ["<< /Type /StructElem /S /P /P 4 0 R >>", "<< /Type /StructTreeRoot /K [3 0 R] >>"],
        "DPartRoot": ["<< /Type /DPartRoot /DPartRootNode 4 0 R >>", "<< /Type /DPart /Parent 3 0 R /Start 5 0 R >>"],
        "DPart": ["<< /Type /DPart /Parent 4 0 R /Start 5 0 R >>", "<< /Type /DPartRoot /DPartRootNode 3 0 R >>"],
        "Thread": [
            "<< /Type /Thread /F 4 0 R >>",
            "<< /Type /Bead /T 3 0 R /N 4 0 R /V 4 0 R /P 5 0 R /R [0 0 1 1] >>",
        ],
        "BeadFirst": [
            "<< /Type /Bead /T 4 0 R /N 3 0 R /V 3 0 R /P 5 0 R /R [0 0 1 1] >>",
            "<< /Type /Thread /F 3 0 R >>",
        ],
        "Bead": [
            "<< /Type /Bead /N 4 0 R /V 4 0 R /P 5 0 R /R [0 0 1 1] >>",
            "<< /Type /Bead /N 3 0 R /V 3 0 R /P 5 0 R /R [0 0 1 1] >>",
        ],
    }[name]


def _refer(holder: str, key: str, kind: str, doc: pyropdf.Document, target: object) -> None:
    cls = getattr(typed, "ThreeD" + holder[2:] if holder.startswith("3D") else holder)
    value: object = target
    if kind in {"name-tree", "number-tree"}:
        value = {"Names": [b"key", target]} if kind == "name-tree" else {"Nums": [1, target]}
    if issubclass(cls, typed.ArrayView):
        at = int(key.rstrip("*") or "0")
        doc.objects.add([None] * at + [value])
    else:
        identities = re.findall(r"/(Type|Subtype|S|FT) /([A-Za-z0-9_.-]+)", _seed(holder)[0])
        owner: dict[str, object] = {field: Name(name) for field, name in identities}
        owner["Sample" if key == "*" else key] = value
        if issubclass(cls, typed.StreamView):
            stream = doc.objects[doc.objects.add_stream(b"", filter=None)]
            for field, item in owner.items():
                stream[field] = item
        else:
            doc.objects.add(owner)


@pytest.mark.spec("arlington-roles", "edit")
@pytest.mark.parametrize("name", [holder for holder, rows in _ROLES.items() if any(row["Target"] for row in rows)])
def test_every_declared_incoming_reference_is_kept_or_refused_atomically(name: str) -> None:
    """Every target of every role protects its number, even in an unknown or malformed holder."""
    for row in _ROLES[name]:
        target = row["Target"]
        if not target:
            continue
        cls = getattr(typed, "ThreeD" + target[2:] if target.startswith("3D") else target)
        with pyropdf.open(object_pdf(_seed(target), VERSIONS.get(target, "2.0"))) as doc:
            seed = doc.objects[3]
            number = doc.objects.add(seed)
            view = cls(doc.objects[number])
            # No other edge can make this refusal pass.
            view.remove()
            assert number not in doc.objects
            doc.history.undo()
            _refer(name, row["Key"], row["Type"], doc, view.obj)
            before = doc.history.position
            with pytest.raises(pyropdf.PdfError, match="referenced"):
                view.remove()
            assert doc.history.position == before
            try:
                view.replace(seed)
            except pyropdf.PdfError:
                assert doc.history.position == before
            assert doc.objects[number] == view.obj
            assert doc.catalog["Sentinel"].text == "unchanged"


def test_wrong_generation_does_not_retain_a_freed_object() -> None:
    with pyropdf.open(object_pdf(["<< /Title (existing) >>", "<< /Old 3 1 R >>"])) as doc:
        typed.DocInfo(doc.objects[3]).remove()
        assert 3 not in doc.objects
        assert 4 in doc.objects
        for incremental in (False, True):
            with pyropdf.open(doc.to_bytes(incremental=incremental)) as saved:
                assert 3 not in saved.objects
                assert saved.catalog["Sentinel"].text == "unchanged"


def test_creation_excludes_rc4_and_retains_existing_legacy_objects() -> None:
    with pyropdf.open(object_pdf(["<< /CFM /V2 /Length 16 >>"], "1.7")) as doc:
        legacy = typed.CryptFilter(doc.objects[3])
        before = doc.history.position
        with pytest.raises(pyropdf.PdfError, match="RC4"):
            typed.CryptFilter.create(doc, legacy.obj)
        with pytest.raises(pyropdf.PdfError, match="RC4"):
            typed.EncryptionStandard.create(doc, {"Filter": Name("Standard"), "V": 2})
        assert doc.history.position == before
        assert legacy.obj["CFM"] == Name("V2")


def test_repeating_positions_begin_after_the_fixed_array_prefix() -> None:
    with pyropdf.open(object_pdf(["[/monochrome (Cyan) 1 (Yellow) 0]"], "1.7")) as doc:
        inks = typed.ArrayOfOPI2Inks(doc.objects[3])
        inks.replace([Name("monochrome"), b"Cyan", 1, b"Yellow", 0])
        with pytest.raises(pyropdf.PdfError):
            inks.replace([Name("monochrome"), 1, b"Cyan"])
        assert inks.obj[1].text == "Cyan"
