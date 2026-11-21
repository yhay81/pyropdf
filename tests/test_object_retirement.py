"""Internal copy-on-write retirement must preserve even detached outside owners."""

from __future__ import annotations

import pyropdf
from pyropdf import _core


def test_retire_nominated_objects_without_sweeping_a_detached_outside_owner() -> None:
    doc = pyropdf.Document()
    doc.pages.new()
    objects = doc.objects
    shared = objects.add_stream(b"shared")
    detached = objects.add({"Shared": objects[shared]})
    doc.catalog["Shared"] = objects[shared]
    child = objects.add_stream(b"child")
    outside = objects.add_stream(b"not nominated")
    parent = objects.add({"Child": objects[child], "Keep": objects[outside]})
    doc.catalog["Pair"] = objects[parent]
    root = doc.catalog.xref
    assert root is not None
    with doc.transaction("retire copied containers"):
        del doc.catalog["Pair"]
        candidates = [child, shared, parent, root, child]
        assert _core.object_free_nominated(doc._core(), candidates) == sorted({child, parent})  # noqa: SLF001
        assert _core.object_free_nominated(doc._core(), candidates) == []  # noqa: SLF001
    with pyropdf.open(doc.to_bytes()) as reopened:
        assert reopened.objects[detached]["Shared"].read() == b"shared"
        assert reopened.catalog["Shared"].read() == b"shared"
        assert reopened.objects[outside].read() == b"not nominated"
        assert child not in reopened.objects
        assert parent not in reopened.objects
