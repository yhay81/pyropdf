"""Sanitizing: each kind leaves the document, the file has one revision, and what breaks is reported."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

import pyropdf
from pyropdf import Name

ASSETS = Path(__file__).resolve().parent / "assets"
CORPUS = sorted((ASSETS / "real_world").glob("*.pdf"))
KINDS = ("javascript", "actions", "attachments", "metadata", "comments", "hidden_text")
COMMENTS = {"Text", "FreeText", "Highlight", "Square", "Ink", "Popup", "FileAttachment", "Stamp", "Line"}


def _only(kind: str) -> dict[str, bool]:
    return {name: name == kind for name in KINDS}


def _dictionaries(doc: pyropdf.Document) -> list[pyropdf.Dictionary]:
    return [value for number in doc.objects if isinstance(value := doc.objects.get(number), pyropdf.Dictionary)]


def _actions(value: object) -> bool:
    return isinstance(value, pyropdf.Dictionary)


def _javascript(value: object) -> bool:
    return isinstance(value, pyropdf.Dictionary) and (
        value.get("S") == "JavaScript" or ("Next" in value and _any_javascript(value["Next"]))
    )


def _any_javascript(value: object) -> bool:
    if isinstance(value, pyropdf.Array):
        return any(_javascript(item) for item in value)
    return _javascript(value)


def _hidden_text(doc: pyropdf.Document) -> int:
    count = 0
    for page in doc.pages:
        mode, saved = 0, []
        for operation in page.operations:
            if operation.operator == "q":
                saved.append(mode)
            elif operation.operator == "Q" and saved:
                mode = saved.pop()
            elif operation.operator == "Tr":
                mode = operation.operands[0]
            elif operation.operator in {"Tj", "TJ", "'", '"'} and mode == 3:
                count += 1
    return count


def present(doc: pyropdf.Document, kind: str) -> int:
    """Count what of ``kind`` the document still holds."""
    names = doc.catalog.get("Names")
    names = names if isinstance(names, pyropdf.Dictionary) else {}
    dictionaries = _dictionaries(doc)
    if kind in ("javascript", "actions"):
        test = _any_javascript if kind == "javascript" else _actions
        count = int("JavaScript" in names)
        for dictionary in dictionaries:
            count += sum(test(dictionary[key]) for key in ("A", "OpenAction") if key in dictionary)
            triggers = dictionary.get("AA")
            if isinstance(triggers, pyropdf.Dictionary):
                count += sum(test(triggers[key]) for key in triggers)
        return count
    if kind == "attachments":
        annotations = [item for page in doc.pages for item in page.obj.get("Annots", [])]
        return (
            int("EmbeddedFiles" in names)
            + int("Collection" in doc.catalog)
            + sum("AF" in dictionary for dictionary in dictionaries)
            + sum(item.get("Subtype") == "FileAttachment" for item in annotations)
        )
    if kind == "metadata":
        return int("Info" in doc.trailer) + sum(
            key in dictionary for dictionary in dictionaries for key in ("Metadata", "PieceInfo")
        )
    if kind == "comments":
        return sum(item.get("Subtype") in COMMENTS for page in doc.pages for item in page.obj.get("Annots", []))
    return _hidden_text(doc)


def _defects(doc: pyropdf.Document) -> Counter[tuple[str, str, str]]:
    return Counter((defect.kind, defect.clause, defect.message) for defect in doc.check().defects)


def _built() -> pyropdf.Document:
    """Return a page with one of each kind, a link, a widget, and an unused object."""
    doc = pyropdf.Document()
    doc.pages.new((200, 100))
    page = doc.pages[0].obj
    font = doc.objects.add({"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name("Helvetica")})
    page["Resources"] = {"Font": {"F1": doc.objects[font]}}
    page["Contents"] = doc.objects[
        doc.objects.add_stream(
            b"BT /F1 12 Tf 10 70 Td (Shown) Tj 3 Tr (Hidden) Tj 10 TL (Line) ' 0 0 (Spaced) \" "
            b"q 0 Tr (Again) Tj Q [(Gone)] TJ ET"
        )
    ]
    script = doc.objects.add({"S": Name("JavaScript"), "JS": "app.alert(1)"})
    chained = {"S": Name("URI"), "URI": "https://example.com", "Next": [doc.objects[script]]}
    page["AA"] = {"O": {"S": Name("JavaScript"), "JS": "1"}, "C": {"S": Name("Named"), "N": Name("NextPage")}}
    doc.catalog["OpenAction"] = chained
    doc.catalog["Names"] = {"JavaScript": {"Names": ["a", doc.objects[script]]}}
    doc.attachments.add("note.txt", b"attached")
    doc.catalog["AF"] = [doc.objects[doc.objects.add({"Type": Name("Filespec"), "F": "x"})]]
    page["Annots"] = [
        {
            "Type": Name("Annot"),
            "Subtype": Name("Link"),
            "Rect": [0, 0, 10, 10],
            "Dest": [doc.pages[0].obj, Name("Fit")],
        },
        {"Type": Name("Annot"), "Subtype": Name("Link"), "Rect": [0, 0, 10, 10], "A": chained},
        {"Type": Name("Annot"), "Subtype": Name("Text"), "Rect": [20, 0, 30, 10], "Contents": "note"},
        {"Type": Name("Annot"), "Subtype": Name("Popup"), "Rect": [20, 0, 60, 40]},
        {"Type": Name("Annot"), "Subtype": Name("FileAttachment"), "Rect": [40, 0, 50, 10]},
    ]
    doc.metadata = {"title": "Secret", "author": "Someone"}
    page["PieceInfo"] = {"App": {"LastModified": "D:2026"}}
    doc.objects.add("an unreferenced string")
    return doc


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("path", CORPUS, ids=lambda path: path.name)
def test_each_kind_leaves_every_corpus_document(path: Path, kind: str, tmp_path: Path) -> None:
    doc = pyropdf.open(path)
    before = _defects(doc)
    found = present(doc, kind)
    report = doc.sanitize(tmp_path / "out.pdf", **_only(kind))
    assert set(report.removed) == {kind}
    if kind != "hidden_text":
        assert (found == 0) == (report.removed[kind] == 0)
    data = (tmp_path / "out.pdf").read_bytes()
    assert data.count(b"startxref") == 1  # One revision.
    sanitized = pyropdf.open(data)
    assert present(sanitized, kind) == 0
    assert not _defects(sanitized) - before  # The structural check finds nothing new.
    assert len(sanitized.pages) == len(doc.pages)


def test_the_corpus_holds_what_the_kinds_remove() -> None:
    totals = Counter()
    for path in CORPUS:
        with pyropdf.open(path) as doc:
            totals.update({kind: present(doc, kind) for kind in KINDS})
    assert totals["javascript"] > 0
    assert totals["actions"] > 0
    assert totals["metadata"] > 0
    assert totals["comments"] > 0
    assert totals["hidden_text"] > 0


def test_every_kind_leaves_a_built_document(tmp_path: Path) -> None:
    doc = _built()
    assert all(present(doc, kind) for kind in KINDS)
    steps = doc.history.position
    report = doc.sanitize(tmp_path / "out.pdf")
    assert doc.history.position == steps + 1
    assert doc.history.steps[-1] == "sanitize"
    assert dict(report.removed) == {
        "javascript": 4,  # The catalog's and the link's chains, the page's /O trigger, and the name tree.
        "actions": 1,  # The page's /C trigger; the first link's /Dest is no action.
        "attachments": 3,  # The name tree, the catalog's /AF, and the file attachment annotation.
        "metadata": 2,  # The Info dictionary and the page's /PieceInfo.
        "comments": 2,  # The text annotation and its pop-up; attachments took the file attachment.
        "hidden_text": 4,
    }


def test_javascript_keeps_other_actions(tmp_path: Path) -> None:
    doc = _built()
    report = doc.sanitize(tmp_path / "out.pdf", **_only("javascript"))
    sanitized = pyropdf.open(tmp_path / "out.pdf")
    assert present(sanitized, "javascript") == 0
    assert "OpenAction" not in sanitized.catalog  # Its chain runs JavaScript.
    assert "JavaScript" not in sanitized.catalog["Names"]
    assert list(sanitized.pages[0].obj["AA"]) == ["C"]
    assert report.removed["javascript"] == 4
    doc.history.undo()
    assert "OpenAction" in doc.catalog


def test_actions_keep_link_destinations(tmp_path: Path) -> None:
    doc = _built()
    doc.sanitize(tmp_path / "out.pdf", **_only("actions"))
    sanitized = pyropdf.open(tmp_path / "out.pdf")
    links = [item for item in sanitized.pages[0].obj["Annots"] if item["Subtype"] == "Link"]
    assert "Dest" in links[0]
    assert "A" not in links[1]
    assert "AA" not in sanitized.pages[0].obj
    assert "OpenAction" not in sanitized.catalog


def test_comments_keep_links_and_hidden_text_keeps_shown_text(tmp_path: Path) -> None:
    doc = _built()
    doc.sanitize(tmp_path / "out.pdf", **_only("comments") | {"hidden_text": True})
    sanitized = pyropdf.open(tmp_path / "out.pdf")
    assert [item["Subtype"] for item in sanitized.pages[0].obj["Annots"]] == ["Link", "Link"]
    assert sanitized.pages[0].text().split() == ["Shown", "Again"]
    operators = [operation.operator for operation in sanitized.pages[0].operations]
    assert operators.count("T*") == 2  # ' and " still move to the next line.
    assert "Tw" in operators


def test_attachments_and_metadata_leave(tmp_path: Path) -> None:
    doc = _built()
    doc.sanitize(tmp_path / "out.pdf", **_only("attachments") | {"metadata": True})
    sanitized = pyropdf.open(tmp_path / "out.pdf")
    assert len(sanitized.attachments) == 0
    assert "AF" not in sanitized.catalog
    assert sanitized.metadata.get("title") is None
    assert "PieceInfo" not in sanitized.pages[0].obj


def test_unused_objects_leave_only_when_asked(tmp_path: Path) -> None:
    doc = _built()
    none = dict.fromkeys(KINDS, False)
    doc.sanitize(tmp_path / "kept.pdf", **none, unused=False)
    doc.sanitize(tmp_path / "pruned.pdf", **none)
    kept = pyropdf.open(tmp_path / "kept.pdf")
    pruned = pyropdf.open(tmp_path / "pruned.pdf")
    strings = [kept.objects.get(number) for number in kept.objects]
    assert "an unreferenced string" in [str(value.text) for value in strings if isinstance(value, pyropdf.String)]
    assert len(pruned.objects) < len(kept.objects)


def test_an_incremental_file_saves_as_one_revision(tmp_path: Path) -> None:
    doc = _built()
    doc.save(tmp_path / "base.pdf")
    edited = pyropdf.open(tmp_path / "base.pdf")
    edited.metadata = {"title": "Second"}
    edited.save(tmp_path / "base.pdf", incremental=True)
    assert (tmp_path / "base.pdf").read_bytes().count(b"startxref") == 2
    pyropdf.open(tmp_path / "base.pdf").sanitize(tmp_path / "out.pdf", **dict.fromkeys(KINDS, False))
    assert (tmp_path / "out.pdf").read_bytes().count(b"startxref") == 1


def test_broken_signatures_are_reported(tmp_path: Path) -> None:
    doc = pyropdf.open(ASSETS / "real_world" / "f1040.pdf")
    report = doc.sanitize(tmp_path / "out.pdf")
    assert report.changes.signatures
    assert report.changes == doc.changes(incremental=False)


def test_broken_profile_claims_are_reported(tmp_path: Path) -> None:
    doc = pyropdf.open(ASSETS / "pdfa" / "pdfa-2b.pdf")
    assert doc.sanitize(tmp_path / "kept.pdf", **dict.fromkeys(KINDS, False)).dropped_claims == ()
    report = doc.sanitize(tmp_path / "out.pdf", **_only("metadata"))
    assert report.dropped_claims == ("PDF/A-2B",)
    assert pyropdf.open(tmp_path / "out.pdf").pdfa_claim is None


def test_an_action_chain_is_bounded(tmp_path: Path) -> None:
    doc = _built()
    action = doc.objects[doc.objects.add({"S": Name("Named"), "N": Name("NextPage")})]
    for _ in range(300):
        action = doc.objects[doc.objects.add({"S": Name("Named"), "N": Name("NextPage"), "Next": action})]
    doc.catalog["OpenAction"] = action
    with pytest.raises(pyropdf.LimitError) as caught:
        doc.sanitize(tmp_path / "out.pdf", **_only("javascript"))
    assert caught.value.code == "sanitize_action_chain"
    assert not (tmp_path / "out.pdf").exists()


def test_a_cyclic_action_chain_ends(tmp_path: Path) -> None:
    doc = _built()
    number = doc.objects.add({"S": Name("Named"), "N": Name("NextPage")})
    action = doc.objects[number]
    action["Next"] = action
    doc.catalog["OpenAction"] = action
    report = doc.sanitize(tmp_path / "out.pdf", **_only("javascript"))
    assert "OpenAction" in pyropdf.open(tmp_path / "out.pdf").catalog
    assert report.removed["javascript"] == 3


def test_options_must_be_bools(tmp_path: Path) -> None:
    doc = _built()
    with pytest.raises(TypeError, match="comments"):
        doc.sanitize(tmp_path / "out.pdf", comments=1)  # ty: ignore[invalid-argument-type]
    assert not (tmp_path / "out.pdf").exists()
