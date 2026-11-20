"""Tests for portable collections, collection items, navigators, and named pages."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING

import pytest

import pyropdf
from pyropdf import (
    Collection,
    CollectionColors,
    CollectionField,
    CollectionFieldKind,
    CollectionFolder,
    CollectionSortKey,
    CollectionSplit,
    CollectionSplitDirection,
    CollectionSubitem,
    CollectionView,
    Color,
    LimitError,
    MalformedPdfError,
    Name,
    NamedPages,
    Navigator,
    Real,
    StaleObjectError,
    StalePageError,
)

if TYPE_CHECKING:
    from pathlib import Path


def _pdf_with_version(version: str, page_count: int = 3) -> bytes:
    """Build a minimal valid PDF of ``version`` with ``page_count`` pages."""
    kids = " ".join(f"{3 + i} 0 R" for i in range(page_count))
    parts = [
        f"%PDF-{version}\n".encode("ascii"),
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {page_count} >>\nendobj\n".encode("ascii"),
    ]
    parts.extend(
        f"{3 + i} 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Resources << >> >>\nendobj\n".encode(
            "ascii"
        )
        for i in range(page_count)
    )
    body = b"".join(parts)
    offsets = []
    pos = 0
    total_objs = 2 + page_count
    for obj_num in range(1, total_objs + 1):
        marker = f"{obj_num} 0 obj".encode("ascii")
        idx = body.index(marker, pos)
        offsets.append(idx)
        pos = idx + 1
    xref_offset = len(body)
    xref = [f"xref\n0 {total_objs + 1}\n0000000000 65535 f \n".encode("ascii")]
    xref.extend(f"{off:010d} 00000 n \n".encode("ascii") for off in offsets)
    trailer = f"trailer\n<< /Size {total_objs + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    return body + b"".join(xref) + trailer


def _assert_valid(doc: pyropdf.Document) -> None:
    """Assert zero structural defects in memory (aside from unsaved trailer /Size) and after saving."""
    assert [d for d in doc.check().defects if d.clause != "FileTrailer/Size"] == []
    with pyropdf.open(doc.to_bytes()) as saved:
        assert not saved.check()


def test_public_exports_and_module_names() -> None:
    exported = (
        Collection,
        CollectionColors,
        CollectionField,
        CollectionFieldKind,
        CollectionFolder,
        CollectionSortKey,
        CollectionSplit,
        CollectionSplitDirection,
        CollectionSubitem,
        CollectionView,
        NamedPages,
        Navigator,
    )
    for cls in exported:
        assert cls.__name__ in pyropdf.__all__
        assert cls.__module__ == "pyropdf"


@pytest.mark.spec("collections", "read", "interpret", "create", "edit", "preserve")
def test_portable_collection_pdf17_schema_sort_undo_and_save(tmp_path: Path) -> None:
    src = tmp_path / "portfolio.pdf"
    src.write_bytes(_pdf_with_version("1.4", page_count=1))

    with pyropdf.open(src) as doc:
        assert doc.collection is None
        doc.attachments.add("alpha.csv", b"col1,col2\n1,2\n", description="Alpha CSV")
        doc.attachments.add("beta.txt", b"notes\n", description="Beta notes")

        base_col = Collection(
            view=CollectionView.DETAILS,
            initial_document="alpha.csv",
            schema={
                "Dept": CollectionField(CollectionFieldKind.STRING, "Department", order=1, editable=True),
                "Issued": CollectionField(CollectionFieldKind.DATE, "Issue Date", order=2),
                "Amount": CollectionField(CollectionFieldKind.NUMBER, "Amount", order=3),
                "File": CollectionField(CollectionFieldKind.FILENAME, "File", visible=False),
                "Summary": CollectionField(CollectionFieldKind.DESCRIPTION, "Summary"),
                "Updated": CollectionField(CollectionFieldKind.MODIFIED_DATE, "Updated"),
                "Created": CollectionField(CollectionFieldKind.CREATION_DATE, "Created"),
                "Bytes": CollectionField(CollectionFieldKind.SIZE, "Bytes"),
            },
            sort=(CollectionSortKey("Dept", ascending=False),),
        )
        steps_before = doc.history.position
        doc.collection = base_col
        assert doc.history.position == steps_before + 1
        assert doc.catalog.get("Version") == Name("1.7")
        assert doc.catalog["Collection"].is_indirect
        first_col_xref = doc.catalog["Collection"].xref
        assert first_col_xref is not None
        assert doc.catalog["Collection"]["Sort"]["S"] == Name("Dept")
        assert doc.catalog["Collection"]["Sort"]["A"] is False
        assert doc.collection == base_col
        assert isinstance(doc.collection.schema, MappingProxyType)
        _assert_valid(doc)

        updated_col = Collection(
            view=CollectionView.TILE,
            initial_document="beta.txt",
            schema=[("Dept", CollectionField(CollectionFieldKind.STRING, "Department"))],
            sort=(CollectionSortKey("Dept", ascending=True), CollectionSortKey("Amount", ascending=False)),
        )
        doc.collection = updated_col
        assert first_col_xref not in doc.objects
        assert doc.collection == updated_col
        assert list(doc.catalog["Collection"]["Sort"]["S"]) == [Name("Dept"), Name("Amount")]
        assert list(doc.catalog["Collection"]["Sort"]["A"]) == [True, False]
        _assert_valid(doc)

        doc.history.undo()
        assert doc.collection == base_col
        doc.history.redo()
        assert doc.collection == updated_col

        inc_path = tmp_path / "portfolio-inc.pdf"
        doc.save(inc_path, incremental=True)

    with pyropdf.open(inc_path) as reopened:
        assert not reopened.check()
        assert reopened.collection == updated_col
        col_xref = reopened.catalog["Collection"].xref
        assert col_xref is not None
        del reopened.collection
        assert reopened.collection is None
        assert col_xref not in reopened.objects
        del reopened.collection  # Idempotent when absent.
        _assert_valid(reopened)
        full_path = tmp_path / "portfolio-empty.pdf"
        reopened.save(full_path)

    with pyropdf.open(full_path) as final:
        assert final.collection is None
        assert not final.check()


@pytest.mark.spec("navigators", "read", "interpret", "create", "edit", "preserve")
def test_collection_pdf20_navigator_colors_split_and_compressed_size() -> None:
    assert Navigator(("FreeForm",)).layout == "FreeForm"
    assert Navigator(["Linear", "Tree"]).layout == ("Linear", "Tree")

    with pyropdf.open(_pdf_with_version("1.4", page_count=1)) as doc:
        col_custom = Collection(
            view=CollectionView.CUSTOM,
            schema={
                "Compressed": CollectionField(CollectionFieldKind.COMPRESSED_SIZE, "Compressed Size", order=0),
            },
            colors=CollectionColors(
                background=Color(0.95, 0.96, 0.98),
                card_background=Color(1.0, 1.0, 1.0),
                card_border=Color(0.75, 0.78, 0.82),
                primary_text=Color(0.1, 0.1, 0.12),
                secondary_text=Color(0.4, 0.42, 0.45),
            ),
            split=CollectionSplit(CollectionSplitDirection.VERTICAL, 32.5),
            navigator=Navigator("FilmStrip"),
        )
        doc.collection = col_custom
        assert doc.catalog.get("Version") == Name("2.0")
        nav_obj = doc.catalog["Collection"]["Navigator"]
        assert nav_obj.is_indirect
        nav_xref = nav_obj.xref
        assert nav_xref is not None
        assert doc.collection == col_custom
        _assert_valid(doc)

        # Replace with multi-layout fallback navigator and NONE split.
        col_multi = Collection(
            view=CollectionView.HIDDEN,
            split=CollectionSplit(CollectionSplitDirection.NONE),
            navigator=Navigator(("Linear", "Tree")),
        )
        doc.collection = col_multi
        assert nav_xref not in doc.objects
        new_nav_xref = doc.catalog["Collection"]["Navigator"].xref
        assert new_nav_xref is not None
        assert "Position" not in doc.catalog["Collection"]["Split"]
        assert doc.collection == col_multi
        _assert_valid(doc)

        del doc.collection
        assert new_nav_xref not in doc.objects
        _assert_valid(doc)

    # Brand-new unsaved Document() promoted to 2.0 also passes _assert_valid cleanly.
    with pyropdf.Document() as fresh:
        fresh.pages.new()
        fresh.collection = Collection(view=CollectionView.CUSTOM, navigator=Navigator("CustomExtLayout"))
        assert fresh.collection is not None
        assert fresh.collection.navigator == Navigator("CustomExtLayout")
        _assert_valid(fresh)


@pytest.mark.spec("collections", "read", "interpret", "create", "edit")
def test_collection_folder_hierarchy_and_object_cleanup() -> None:
    t_created = datetime(2026, 4, 1, 8, 30, 0, tzinfo=UTC)
    t_modified = datetime(2026, 4, 2, 17, 45, 0, tzinfo=timezone(timedelta(hours=9)))

    invoices = CollectionFolder(
        1,
        "Invoices",
        description="Outgoing invoices",
        created=t_created,
        modified=t_modified,
        collection_item={"Count": 2, "Total": CollectionSubitem(150.5, "$")},
        children=(
            CollectionFolder(3, "Paid"),
            CollectionFolder(4, "Pending"),
        ),
    )
    receipts = CollectionFolder(2, "Receipts", created="D:20260401000000Z")
    root = CollectionFolder(0, "Root", description="Portfolio root", children=(invoices, receipts))

    with pyropdf.open(_pdf_with_version("1.7", page_count=1)) as doc:
        col = Collection(root_folder=root)
        doc.collection = col
        assert doc.catalog.get("Version") == Name("2.0")

        raw_root = doc.catalog["Collection"]["Folders"]
        assert raw_root.is_indirect
        assert "Parent" not in raw_root
        assert "Next" not in raw_root
        raw_inv = raw_root["Child"]
        raw_rec = raw_inv["Next"]
        assert raw_inv.is_indirect
        assert raw_rec.is_indirect
        assert raw_inv["Parent"].xref == raw_root.xref
        assert raw_rec["Parent"].xref == raw_root.xref
        folder_xrefs = [
            raw_root.xref,
            raw_inv.xref,
            raw_rec.xref,
            raw_inv["Child"].xref,
            raw_inv["Child"]["Next"].xref,
        ]
        assert all(x is not None for x in folder_xrefs)

        read_back = doc.collection
        assert read_back == col
        assert read_back is not None
        assert read_back.root_folder is not None
        assert read_back.root_folder.children[0].created == "D:20260401083000Z"
        assert read_back.root_folder.children[0].modified == "D:20260402174500+09'00'"
        _assert_valid(doc)

        del doc.collection
        for xref in folder_xrefs:
            assert xref not in doc.objects
        _assert_valid(doc)


def test_collection_folder_iterative_bounds_and_cycles() -> None:
    # Maximum allowed 32-level folder chain succeeds iteratively without recursion.
    deep = CollectionFolder(31, "L31")
    for level in range(30, -1, -1):
        deep = CollectionFolder(level, f"L{level}", children=(deep,))
    with pyropdf.Document() as doc:
        doc.pages.new()
        doc.collection = Collection(root_folder=deep)
        assert doc.collection is not None
        cur = doc.collection.root_folder
        depth = 0
        while cur is not None:
            depth += 1
            cur = cur.children[0] if cur.children else None
        assert depth == 32
        _assert_valid(doc)

    # 33rd level is refused on construction.
    with pytest.raises(ValueError, match="32-level"):
        CollectionFolder(99, "TooDeep", children=(deep,))

    # Duplicate folder IDs are refused.
    with pytest.raises(ValueError, match="duplicate collection folder id"):
        CollectionFolder(0, "Root", children=(CollectionFolder(1, "A"), CollectionFolder(1, "B")))
    with pytest.raises(ValueError, match="duplicate collection folder id"):
        CollectionFolder(5, "Root", children=(CollectionFolder(5, "SelfDup"),))

    # Crafted PDF with a cycle in /Next and /Child terminates cleanly, and >32 levels raises LimitError.
    with pyropdf.Document() as cyclic_doc:
        cyclic_doc.pages.new()
        f0 = cyclic_doc.objects.add({"Type": Name("Folder"), "ID": 0, "Name": "Root"})
        f1 = cyclic_doc.objects.add({"Type": Name("Folder"), "ID": 1, "Name": "Child"})
        cyclic_doc.objects[f0]["Child"] = cyclic_doc.objects[f1]
        cyclic_doc.objects[f1]["Next"] = cyclic_doc.objects[f1]  # Self-cycle on /Next.
        cyclic_doc.catalog["Collection"] = {"Type": Name("Collection"), "Folders": cyclic_doc.objects[f0]}
        parsed_cyclic = cyclic_doc.collection
        assert parsed_cyclic is not None
        assert parsed_cyclic.root_folder is not None
        assert [c.id for c in parsed_cyclic.root_folder.children] == [1]

    with pyropdf.Document() as overdeep_doc:
        overdeep_doc.pages.new()
        node_xrefs = [
            overdeep_doc.objects.add({"Type": Name("Folder"), "ID": idx, "Name": f"F{idx}"}) for idx in range(33)
        ]
        for idx in range(32):
            overdeep_doc.objects[node_xrefs[idx]]["Child"] = overdeep_doc.objects[node_xrefs[idx + 1]]
        overdeep_doc.catalog["Collection"] = {
            "Type": Name("Collection"),
            "Folders": overdeep_doc.objects[node_xrefs[0]],
        }
        with pytest.raises(LimitError) as exc_info:
            _ = overdeep_doc.collection
        assert exc_info.value.code == "object_depth"


@pytest.mark.spec("collection-items", "read", "interpret", "create", "edit", "preserve")
def test_attachment_and_folder_collection_items(tmp_path: Path) -> None:
    path = tmp_path / "items.pdf"
    path.write_bytes(_pdf_with_version("1.4", page_count=1))
    issued = datetime(2026, 6, 15, 10, 0, 0, tzinfo=UTC)

    with pyropdf.open(path) as doc:
        att = doc.attachments.add("invoice.pdf", b"%PDF-stub", description="Invoice")
        assert att.collection_item is None

        att.collection_item = {
            "Client": "Acme Corp",
            "Count": 7,
            "Rate": Real("19.95"),
            "Issued": issued,
            "Total": CollectionSubitem(139.65, "$"),
            "Stamped": CollectionSubitem(issued, "On "),
        }
        assert doc.catalog.get("Version") == Name("1.7")
        filespec = doc.catalog["Names"]["EmbeddedFiles"]["Names"][1]
        assert filespec["CI"].is_indirect
        first_ci_xref = filespec["CI"].xref
        assert first_ci_xref is not None

        ci = att.collection_item
        assert isinstance(ci, MappingProxyType)
        assert ci == {
            "Client": "Acme Corp",
            "Count": 7,
            "Rate": Real("19.95"),
            "Issued": "D:20260615100000Z",
            "Total": CollectionSubitem(139.65, "$"),
            "Stamped": CollectionSubitem("D:20260615100000Z", "On "),
        }
        _assert_valid(doc)

        # Replacing frees the previous indirect /CI dictionary.
        att.collection_item = {"Client": "Globex"}
        assert first_ci_xref not in doc.objects
        assert att.collection_item == {"Client": "Globex"}
        _assert_valid(doc)

        # Undo/redo preserves exact state.
        doc.history.undo()
        assert att.collection_item is not None
        assert att.collection_item["Client"] == "Acme Corp"
        doc.history.redo()
        assert att.collection_item == {"Client": "Globex"}

        saved_path = tmp_path / "items-saved.pdf"
        doc.save(saved_path)

    with pyropdf.open(saved_path) as reopened:
        assert not reopened.check()
        att2 = reopened.attachments["invoice.pdf"]
        assert att2.collection_item == {"Client": "Globex"}
        ci_xref = reopened.catalog["Names"]["EmbeddedFiles"]["Names"][1]["CI"].xref
        assert ci_xref is not None

        del att2.collection_item
        assert att2.collection_item is None
        assert ci_xref not in reopened.objects
        del att2.collection_item  # Idempotent when absent.
        _assert_valid(reopened)

        # Re-assign /CI and delete the attachment itself: indirect /CI is freed too.
        att2.collection_item = {"Tag": "Archived"}
        ci_xref_2 = reopened.catalog["Names"]["EmbeddedFiles"]["Names"][1]["CI"].xref
        assert ci_xref_2 is not None
        del reopened.attachments["invoice.pdf"]
        assert ci_xref_2 not in reopened.objects
        _assert_valid(reopened)

        with pytest.raises(StaleObjectError):
            _ = att2.collection_item
        with pytest.raises(StaleObjectError):
            att2.collection_item = {"Tag": "Stale"}
        with pytest.raises(StaleObjectError):
            del att2.collection_item


@pytest.mark.spec("name-dictionary", "read", "interpret", "create", "edit", "preserve")
def test_named_pages_crud_and_page_pruning(tmp_path: Path) -> None:
    # PDF 1.3 keeps header 1.3 without needing /Version; PDF 1.2 promotes /Version to 1.4.
    with pyropdf.open(_pdf_with_version("1.3", page_count=4)) as doc13:
        assert isinstance(doc13.named_pages, NamedPages)
        assert len(doc13.named_pages) == 0
        assert repr(doc13.named_pages) == "NamedPages({})"
        doc13.named_pages["cover"] = 0
        assert "Version" not in doc13.catalog
        _assert_valid(doc13)

    with pyropdf.open(_pdf_with_version("1.2", page_count=4)) as doc:
        doc.named_pages["cover"] = 0
        assert doc.catalog.get("Version") == Name("1.4")
        doc.named_pages["summary"] = doc.pages[1]
        doc.named_pages.update({"appendix": -1}, index_page=2)

        assert list(doc.named_pages) == ["appendix", "cover", "index_page", "summary"]
        assert len(doc.named_pages) == 4
        assert "cover" in doc.named_pages
        assert "missing" not in doc.named_pages
        assert 123 not in doc.named_pages
        assert doc.named_pages["appendix"] == doc.pages[3]
        assert doc.named_pages["summary"] == doc.pages[1]
        _assert_valid(doc)

        del doc.named_pages["index_page"]
        assert "index_page" not in doc.named_pages
        with pytest.raises(KeyError):
            _ = doc.named_pages["index_page"]
        with pytest.raises(KeyError):
            del doc.named_pages["index_page"]

        # Deleting page 1 prunes "summary" in the same single transaction, keeping surviving handles aligned.
        steps_before = doc.history.position
        del doc.pages[1]
        assert doc.history.position == steps_before + 1
        assert dict(doc.named_pages) == {"appendix": doc.pages[2], "cover": doc.pages[0]}
        _assert_valid(doc)

        # Single undo restores both the removed page and its named_pages entry.
        doc.history.undo()
        assert len(doc.pages) == 4
        assert doc.named_pages["summary"] == doc.pages[1]
        _assert_valid(doc)

        # Pages.select prunes removed pages' named entries in the same transaction too.
        doc.pages.select([0, 3])
        assert dict(doc.named_pages) == {"appendix": doc.pages[1], "cover": doc.pages[0]}
        _assert_valid(doc)

        # Replace via property setter and save/reopen.
        doc.named_pages = {"front": 0, "back": 1}
        assert dict(doc.named_pages) == {"back": doc.pages[1], "front": doc.pages[0]}
        saved_path = tmp_path / "named-pages.pdf"
        doc.save(saved_path)

    with pyropdf.open(saved_path) as reopened:
        assert not reopened.check()
        assert dict(reopened.named_pages) == {"back": reopened.pages[1], "front": reopened.pages[0]}
        del reopened.named_pages
        assert len(reopened.named_pages) == 0
        assert "Names" not in reopened.catalog
        _assert_valid(reopened)


@pytest.mark.spec("name-dictionary", "read", "interpret", "create", "edit")
def test_named_pages_canonical_multi_leaf_tree_and_cleanup() -> None:
    with pyropdf.Document() as big:
        p0 = big.pages.new()
        p1 = big.pages.new()
        big.named_pages = {f"p_{idx:03d}": (p0 if idx % 2 == 0 else p1) for idx in range(140)}
        assert len(big.named_pages) == 140
        pages_tree = big.catalog["Names"]["Pages"]
        assert "Limits" not in pages_tree
        kids = pages_tree["Kids"]
        assert len(kids) == 3
        kid_xrefs = [kid.xref for kid in kids]
        assert all(x is not None for x in kid_xrefs)
        assert list(kids[0]["Limits"]) == [b"p_000", b"p_063"]
        assert list(kids[2]["Limits"]) == [b"p_128", b"p_139"]
        _assert_valid(big)

        big.named_pages.clear()
        assert len(big.named_pages) == 0
        assert "Names" not in big.catalog
        for xref in kid_xrefs:
            assert xref not in big.objects
        _assert_valid(big)


def test_validation_and_malformed_pdf_errors() -> None:
    with pytest.raises(ValueError, match=r"CollectionView\.CUSTOM requires a navigator"):
        Collection(view=CollectionView.CUSTOM)
    with pytest.raises(ValueError, match="position must be None"):
        CollectionSplit(CollectionSplitDirection.NONE, 50.0)
    with pytest.raises(ValueError, match="between 0 and 100"):
        CollectionSplit(CollectionSplitDirection.HORIZONTAL, 101.0)
    bad_rgb: object = (1.0, 0.0, 0.0)
    with pytest.raises(TypeError, match="Color"):
        CollectionColors(background=bad_rgb)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="opaque Color"):
        CollectionColors(background=Color(0.2, 0.3, 0.4, alpha=0.5))
    with pytest.raises(ValueError, match="opaque Color"):
        CollectionColors(background=Color(1.2, 0.0, 0.0))
    with pytest.raises(ValueError, match="between 1 and 64"):
        Navigator([])
    with pytest.raises(ValueError, match="multi-layout fallback arrays"):
        Navigator(("Linear", "CustomNonStandard"))
    with pytest.raises(TypeError, match="not bool"):
        CollectionSubitem(value=True)
    with pytest.raises(ValueError, match="finite"):
        CollectionSubitem(float("nan"))
    with pytest.raises(ValueError, match="cannot be 'Type'"):
        Collection(schema={"Type": CollectionField(CollectionFieldKind.STRING, "Bad")})
    with pytest.raises(ValueError, match="cannot be 'Type'"):
        CollectionSortKey("Type")

    with pyropdf.Document() as doc, pyropdf.Document() as other:
        p0 = doc.pages.new()
        other_page = other.pages.new()
        att = doc.attachments.add("f.txt", b"x")
        with pytest.raises(ValueError, match="cannot be 'Type'"):
            att.collection_item = {"Type": "Bad"}
        with pytest.raises(TypeError, match="not bool"):
            att.collection_item = {"Flag": False}
        with pytest.raises(ValueError, match="another document"):
            doc.named_pages["other"] = other_page
        del doc.pages[0]
        with pytest.raises(StalePageError):
            doc.named_pages["stale"] = p0

    # Malformed /Collection dictionary in PDF surfaces MalformedPdfError.
    with pyropdf.Document() as bad:
        bad.pages.new()
        bad.catalog["Collection"] = {"Type": Name("Collection"), "View": Name("C")}
        with pytest.raises(MalformedPdfError, match="Navigator"):
            _ = bad.collection
