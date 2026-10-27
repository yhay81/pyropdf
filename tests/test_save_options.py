# ruff: noqa: S106 - the fixtures' passwords are test data.
"""Save options beyond full and incremental saves (DESIGN.md section 4.4)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from corpus import ASSETS, NAMES
from test_attachments import build_pdf

import pyropdf
from pyropdf import Encryption

REFERENCE = re.compile(rb"(\d+) \d+ R")
ENCRYPTED = Path(__file__).resolve().parent / "assets" / "encrypted"


def _numbers(data: bytes) -> set[int]:
    return set(pyropdf.open(data).objects)


def test_a_pruned_save_leaves_out_what_an_edit_unlinked() -> None:
    doc = pyropdf.open(build_pdf(["Hello"]))
    doc.attachments.add("a.txt", b"data")
    doc = pyropdf.open(doc.to_bytes())
    filespec = doc.catalog["Names"]["EmbeddedFiles"]["Names"][1]
    unlinked = {filespec.xref, filespec["EF"]["F"].xref}
    del doc.attachments["a.txt"]

    pruned = pyropdf.open(doc.to_bytes(prune=True))
    assert not unlinked & set(pruned.objects)
    assert unlinked <= _numbers(doc.to_bytes())
    assert unlinked <= set(doc.objects)
    assert len(pruned.pages) == 1
    assert pruned.pages[0].text().strip() == "Hello"


def test_a_pruned_save_keeps_what_the_trailer_reaches_through_any_object() -> None:
    doc = pyropdf.open(build_pdf(["Hello"]))
    leaf = doc.objects.add({"Leaf": 1})
    orphan = doc.objects.add({"Child": doc.objects[leaf]})
    kept = doc.objects.add({"Child": doc.objects[leaf]})
    doc.catalog["Extra"] = doc.objects[kept]

    numbers = _numbers(doc.to_bytes(prune=True))
    assert {leaf, kept} <= numbers
    assert orphan not in numbers


def test_a_pruned_save_writes_a_new_encryption_without_the_old_dictionary(tmp_path: Path) -> None:
    doc = pyropdf.open(ENCRYPTED / "user-aes-256.pdf", password="userpw")
    old = doc.trailer["Encrypt"].xref
    path = tmp_path / "pruned.pdf"
    doc.save(path, encryption=Encryption(user_password="new"), prune=True)
    locked = pyropdf.open(path)
    assert locked.needs_password
    locked.unlock("new")
    assert len(locked.pages) == len(doc.pages)
    assert locked.trailer["Encrypt"].xref != old
    assert old not in set(locked.objects)


@pytest.mark.parametrize("name", NAMES)
def test_a_pruned_corpus_save_keeps_every_reachable_object(name: str) -> None:
    source = pyropdf.open(ASSETS / name)
    values = {number: body for number, _, body in source._core()._objects()}  # noqa: SLF001
    pruned = pyropdf.open(source.to_bytes(prune=True))
    kept = {number: body for number, _, body in pruned._core()._objects()}  # noqa: SLF001

    assert source.catalog.xref in kept
    assert len(pruned.pages) == len(source.pages)
    for number, body in kept.items():
        assert body == values[number], number
        # What a kept object refers to is kept, unless the source lacks it too.
        head = body.split(b"stream\n", 1)[0]
        for match in REFERENCE.finditer(head):
            target = int(match[1])
            assert (target in kept) == (target in values), (number, target)


def test_prune_requires_a_full_save_and_a_bool() -> None:
    doc = pyropdf.open(build_pdf(["Hello"]))
    with pytest.raises(ValueError, match="full save"):
        doc.to_bytes(incremental=True, prune=True)
    with pytest.raises(TypeError, match="prune must be a bool"):
        doc.to_bytes(prune=1)  # ty: ignore[invalid-argument-type]
