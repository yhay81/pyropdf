"""Single-file ICC and document-part rules exercised on an independently written print hierarchy."""

from __future__ import annotations

import io
from collections import deque
from xml.parsers import expat

import pikepdf
import pytest
from PIL import Image, ImageCms
from print_exchange_fixtures import CONTENTS, ICC, VT_IDENTIFICATION, new_job

import pyropdf
from pyropdf import Name


def hierarchy_pdf(*, single_leaf: bool = False) -> bytes:
    """Write a four-page job using pikepdf's independent indirect-object and stream implementation."""
    with new_job("x4") as base:
        assert base.xmp is not None
        base.xmp = base.xmp.replace("</rdf:RDF>", VT_IDENTIFICATION + "</rdf:RDF>")
        original = base.to_bytes()
    with pikepdf.Pdf.open(io.BytesIO(original)) as pdf:
        for paint in CONTENTS:
            page = pdf.add_blank_page(page_size=(100, 100))
            page.TrimBox = [4, 4, 96, 96]
            page.Contents = pdf.make_stream(paint)
        root = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.DPartRoot))
        job = pdf.make_indirect(pikepdf.Dictionary(Parent=root))
        root.DPartRootNode = job
        pdf.Root.DPartRoot = root
        if single_leaf:
            root.NodeNameList = [pikepdf.Name.Job]
            root.RecordLevel = 0
            job.Start, job.End = pdf.pages[0].obj, pdf.pages[3].obj
            for page in pdf.pages:
                page.DPart = job
        else:
            root.NodeNameList = [pikepdf.Name.Job, pikepdf.Name.Batch, pikepdf.Name.Record]
            root.RecordLevel = 2
            groups = []
            for first in (0, 2):
                group = pdf.make_indirect(pikepdf.Dictionary(Parent=job))
                leaf = pdf.make_indirect(
                    pikepdf.Dictionary(Parent=group, Start=pdf.pages[first].obj, End=pdf.pages[first + 1].obj)
                )
                group.DParts = [[leaf]]
                groups.append(group)
                pdf.pages[first].DPart = leaf
                pdf.pages[first + 1].DPart = leaf
            job.DParts = [groups]
        result = io.BytesIO()
        pdf.save(result)
        return result.getvalue()


def profile_findings(doc: pyropdf.Document) -> list[tuple[int | None, str, str]]:
    """Return the actual object, path and normative clause from the public report."""
    return [(item.xref, item.path, item.spec) for item in doc.check().defects if item.kind == "profile"]


def _xml_token(name: str) -> bool:
    parser = expat.ParserCreate()
    names = []
    parser.StartElementHandler = lambda tag, _attrs: names.append(tag)
    try:
        parser.Parse(f"<n{name}/>", True)  # noqa: FBT003 - final XML chunk.
    except expat.ExpatError:
        return False
    return names == [f"n{name}"]


def _oracle_chunks(chunks: pikepdf.Object) -> bool:
    """Independently inspect lengths and duplicate object numbers before expanding any test chunk."""
    if not chunks:
        return False
    for index, chunk in enumerate(chunks):
        sizes = range(1, 8193) if index == len(chunks) - 1 else (8192,)
        if not isinstance(chunk, pikepdf.Array) or len(chunk) not in sizes:
            return False
        if len({child.objgen for child in chunk}) != len(chunk):
            return False
    return True


def _oracle_levels(root: pikepdf.Object) -> bool:
    levels = root.NodeNameList
    return all(isinstance(name, pikepdf.Name) and _xml_token(str(name)[1:]) for name in levels) and (
        "/RecordLevel" not in root or 0 <= root.RecordLevel < len(levels)
    )


def independent_hierarchy(data: bytes) -> bool:
    """Inspect the test fixtures with qpdf and a breadth-first page ownership oracle."""
    with pikepdf.Pdf.open(io.BytesIO(data)) as pdf:
        root = pdf.Root.DPartRoot
        if not isinstance(root, pikepdf.Dictionary) or not _oracle_levels(root):
            return False
        pages = {page.obj.objgen: at for at, page in enumerate(pdf.pages)}
        queue: deque[tuple[pikepdf.Object, pikepdf.Object, tuple[int, ...]]] = deque([(root.DPartRootNode, root, ())])
        seen = set()
        leaves = []
        depth = 0
        while queue:
            node, parent, indices = queue.popleft()
            if node.objgen in seen or node.Parent.objgen != parent.objgen:
                return False
            seen.add(node.objgen)
            depth = max(depth, len(indices) + 1)
            if "/DParts" in node:
                chunks = node.DParts
                if "/Start" in node or "/End" in node or not _oracle_chunks(chunks):
                    return False
                for idx, chunk in enumerate(chunks):
                    queue.extend((child, node, (*indices, idx * 8192 + j)) for j, child in enumerate(chunk))
            else:
                start = pages[node.Start.objgen]
                end = pages[node.End.objgen] if "/End" in node else start
                if "/End" in node and start >= end:
                    return False
                leaves.append((indices, start, end, node.objgen))
        ownership = [(at, node) for _, start, end, node in sorted(leaves) for at in range(start, end + 1)]
        backlinks = [(at, page.DPart.objgen) for at, page in enumerate(pdf.pages)]
        return depth == len(root.NodeNameList) and ownership == backlinks


def independent_icc(data: bytes) -> bool:
    """Validate the actual destination profile header with LittleCMS and the stream type with qpdf."""
    with pikepdf.Pdf.open(io.BytesIO(data)) as pdf:
        icc = pdf.Root.OutputIntents[0].DestOutputProfile
        try:
            parsed = ImageCms.getOpenProfile(io.BytesIO(icc.read_bytes())).profile
        except (OSError, ImageCms.PyCMSError):
            return False
        components = {"GRAY": 1, "RGB": 3, "CMYK": 4}.get(parsed.xcolor_space.strip())
        count = icc.get("/N")
        return parsed.device_class == "prtr" and components is not None and type(count) is int and count == components


def _change_hierarchy(doc: pyropdf.Document, case: str) -> tuple[int | None, str]:
    root = doc.catalog["DPartRoot"]
    job = root["DPartRootNode"]
    batch_a, batch_b = job["DParts"][0]
    leaf_a, leaf_b = batch_a["DParts"][0][0], batch_b["DParts"][0][0]
    if case == "root-stream":
        replacement = doc.objects[doc.objects.add_stream(b"", {"Type": Name("DPartRoot"), "DPartRootNode": job})]
        doc.catalog["DPartRoot"] = replacement
        return doc.catalog.xref, "DPartRoot"
    if case == "orphan-backlink":
        orphan = doc.objects[doc.objects.add({"Parent": batch_b, "Start": doc.pages[2].obj, "End": doc.pages[3].obj})]
        doc.pages[2].obj["DPart"] = orphan
        return doc.pages[2].xref, "DPart"
    if case == "uncovered":
        del leaf_b["End"]
        return doc.pages[3].xref, "DPart"
    if case in ("oversize", "full-group-duplicate"):
        if case == "oversize":
            batch_b["DParts"] = [[leaf_b] * 8193]
            return batch_b.xref, "DParts/0"
        batch_b["DParts"] = [[leaf_b] * 8192, [leaf_b]]
        return batch_b.xref, "DParts/0/1"
    # For each assignment, identify the first object and key that the reader
    # can prove wrong when traversing the changed tree.
    changes = {
        "overlap": (leaf_b, "Start", doc.pages[1].obj, leaf_b, "Start"),
        "wrong-parent": (leaf_b, "Parent", job, leaf_b, "Parent"),
        "nonfinal-short": (job, "DParts", [[batch_a], [batch_b]], job, "DParts/0"),
        "empty-chunk": (batch_b, "DParts", [[]], batch_b, "DParts/0"),
        "cycle": (batch_b, "DParts", [[batch_b]], batch_b, "Parent"),
        "reversed": (job, "DParts", [[batch_b, batch_a]], leaf_b, "Start"),
        "superfluous-end": (leaf_b, "End", leaf_b["Start"], leaf_b, "End"),
        "children-and-start": (batch_b, "Start", doc.pages[2].obj, batch_b, "Start"),
        "wrong-root-parent": (job, "Parent", leaf_a, job, "Parent"),
        "bad-name-count": (root, "NodeNameList", [Name("Job"), Name("Record")], root, "NodeNameList"),
        "bad-name-token": (
            root,
            "NodeNameList",
            [Name("Job"), Name("Batch"), Name("Recipient ID")],
            root,
            "NodeNameList/2",
        ),
        "record-level": (root, "RecordLevel", 3, root, "RecordLevel"),
    }
    holder, key, value, finding, path = changes[case]
    holder[key] = value
    return finding.xref, path


@pytest.mark.spec("pdfvt", "interpret")
@pytest.mark.parametrize("single_leaf", [True, False], ids=["root-leaf", "two-nested-records"])
@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_independent_positive_hierarchies_cover_every_page(*, single_leaf: bool, incremental: bool) -> None:
    with pyropdf.open(hierarchy_pdf(single_leaf=single_leaf)) as doc:
        assert not profile_findings(doc)
        root = doc.catalog["DPartRoot"]
        del root["RecordLevel"]  # Optional: the document still declares a valid ordered hierarchy.
        saved = doc.to_bytes(incremental=incremental)
    assert independent_hierarchy(saved)
    with pyropdf.open(saved) as reopened, pikepdf.Pdf.open(io.BytesIO(saved)) as other:
        assert not profile_findings(reopened)
        assert len(other.pages) == 4
        assert other.pages[0].DPart.Start.objgen == other.pages[0].objgen
        assert other.pages[-1].DPart.End.objgen == other.pages[-1].objgen
        assert (other.pages[0].DPart.objgen == other.pages[-1].DPart.objgen) == single_leaf


HIERARCHY_CASES = [
    "root-stream",
    "cycle",
    "orphan-backlink",
    "overlap",
    "uncovered",
    "wrong-parent",
    "nonfinal-short",
    "oversize",
    "full-group-duplicate",
    "empty-chunk",
    "reversed",
    "superfluous-end",
    "children-and-start",
    "wrong-root-parent",
    "bad-name-count",
    "bad-name-token",
    "record-level",
]


@pytest.mark.spec("pdfvt", "interpret")
@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.parametrize("case", HIERARCHY_CASES)
def test_nested_hierarchy_finding_names_the_exact_entry(case: str, *, incremental: bool) -> None:
    with pyropdf.open(hierarchy_pdf()) as doc:
        xref, path = _change_hierarchy(doc, case)
        expected = [(xref, path, "ISO 16612-2:2010 6.5")]
        assert profile_findings(doc) == expected
        data = doc.to_bytes(incremental=incremental)
    assert not independent_hierarchy(data)
    with pyropdf.open(data) as reopened:
        assert profile_findings(reopened) == expected
        assert tuple(page.obj["Contents"].read() for page in reopened.pages) == (*CONTENTS, *CONTENTS)


MEDIA = (
    ("GRAY", "virtual-gray.icc", 1, "L", b".5 g"),
    ("RGB", "virtual-rgb.icc", 3, "RGB", b".2 .5 .7 rg"),
    ("CMYK", "FOGRA39L-Coated.icc", 4, "CMYK", b".2 .5 .7 .1 k"),
)


def media_pdf(model: str) -> bytes:
    """Embed the corresponding device profile and paint using its characterized number of components."""
    _, filename, channels, _, operator = next(item for item in MEDIA if item[0] == model)
    with new_job("x4") as doc:
        intent = doc.catalog["OutputIntents"][0]
        intent["DestOutputProfile"] = doc.objects[
            doc.objects.add_stream((ICC.parent / filename).read_bytes(), {"N": channels})
        ]
        if model != "CMYK":
            del intent["RegistryName"]
            intent["OutputConditionIdentifier"] = f"pyropdf virtual {model} output"
            intent["Info"] = f"pyropdf virtual {model} output"
        for page in doc.pages:
            page.obj["Contents"].write(b"q " + operator + b" 10 10 80 80 re f Q")
        return doc.to_bytes()


@pytest.mark.spec("pdfx", "interpret")
@pytest.mark.parametrize(("model", "filename", "channels", "mode", "_operator"), MEDIA)
@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_functional_output_profile_matches_its_stream_and_device(
    model: str, filename: str, channels: int, mode: str, _operator: bytes, *, incremental: bool
) -> None:
    with pyropdf.open(media_pdf(model)) as doc:
        assert not profile_findings(doc)
        doc.catalog["OutputIntents"][0]["Info"] = "Checked press profile"
        saved = doc.to_bytes(incremental=incremental)
    assert independent_icc(saved)
    with pyropdf.open(saved) as reopened, pikepdf.Pdf.open(io.BytesIO(saved)) as other:
        assert not profile_findings(reopened)
        embedded = other.Root.OutputIntents[0].DestOutputProfile
        assert channels == embedded.N
        assert embedded.read_bytes() == (ICC.parent / filename).read_bytes()
        profile = ImageCms.getOpenProfile(io.BytesIO(embedded.read_bytes()))
        assert profile.profile.device_class == "prtr"
        assert profile.profile.xcolor_space.strip() == model
        srgb = ImageCms.createProfile("sRGB")
        # LittleCMS builds and applies both directions, including the LUT printer profile.
        to_rgb = ImageCms.buildTransformFromOpenProfiles(profile, srgb, mode, "RGB")
        from_rgb = ImageCms.buildTransformFromOpenProfiles(srgb, profile, "RGB", mode)
        swatch = Image.new("RGB", (2, 1), (0, 0, 0))
        swatch.putpixel((1, 0), (255, 255, 255))
        press_swatch = ImageCms.applyTransform(swatch, from_rgb)
        assert press_swatch is not None
        converted = ImageCms.applyTransform(press_swatch, to_rgb)
        assert converted is not None
        dark, light = converted.getpixel((0, 0)), converted.getpixel((1, 0))
        assert isinstance(dark, tuple)
        assert isinstance(light, tuple)
        assert all(b > a for a, b in zip(dark, light, strict=True))


def break_profile(doc: pyropdf.Document, case: str) -> str:
    """Mutate a single ICC field, preserving the remainder of a genuine CMYK output profile."""
    stream = doc.catalog["OutputIntents"][0]["DestOutputProfile"]
    if case == "input-profile":
        stream.write((ICC.parent / "CGATS001Compat-v2-micro.icc").read_bytes())
        return ""
    if case == "wrong-components":
        stream["N"] = 3
    elif case == "boolean-components":
        stream["N"] = True
    elif case == "missing-components":
        del stream["N"]
    else:
        data = bytearray(stream.read())
        if case == "scanner":
            data[12:16] = b"scnr"
        elif case == "display":
            data[12:16] = b"mntr"
        elif case == "unrecognized-model":
            data[16:20] = b"5CLR"
        elif case == "bad-signature":
            data[36:40] = b"nope"
        elif case == "short-header":
            data = data[:60]
        else:
            raise ValueError(case)
        stream.write(bytes(data))
        return ""
    return "N"


ICC_CASES = [
    "input-profile",
    "scanner",
    "display",
    "unrecognized-model",
    "bad-signature",
    "short-header",
    "wrong-components",
    "boolean-components",
    "missing-components",
]


@pytest.mark.spec("pdfx", "interpret")
@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
@pytest.mark.parametrize("case", ICC_CASES)
def test_bad_output_profile_names_the_specific_entry(case: str, *, incremental: bool) -> None:
    with pyropdf.open(media_pdf("CMYK")) as doc:
        path = break_profile(doc, case)
        expected = [(doc.catalog["OutputIntents"][0]["DestOutputProfile"].xref, path, "ISO 15930-7:2010 6.4.2.1")]
        assert profile_findings(doc) == expected
        saved = doc.to_bytes(incremental=incremental)
    assert not independent_icc(saved)
    with pyropdf.open(saved) as reopened:
        assert profile_findings(reopened) == expected
        assert len(reopened.pages) == 2


def test_profile_decode_honours_the_document_budget() -> None:
    with pyropdf.open(media_pdf("CMYK"), limits=pyropdf.DocumentLimits(max_decompressed_size=4096)) as doc:
        with pytest.raises(pyropdf.LimitError) as error:
            doc.check()
        assert error.value.code == "decompressed_size"


def test_deep_hierarchy_uses_the_operation_budget_without_python_recursion() -> None:
    with pikepdf.Pdf.open(io.BytesIO(hierarchy_pdf(single_leaf=True))) as pdf:
        root = pdf.Root.DPartRoot
        node = root.DPartRootNode
        del node.Start
        del node.End
        for _ in range(1100):
            child = pdf.make_indirect(pikepdf.Dictionary(Parent=node))
            node.DParts = [[child]]
            node = child
        node.Start, node.End = pdf.pages[0].obj, pdf.pages[-1].obj
        for page in pdf.pages:
            page.DPart = node
        root.NodeNameList = [pikepdf.Name.Record] * 1101
        stream = io.BytesIO()
        pdf.save(stream)
        encoded = stream.getvalue()
    with pyropdf.open(encoded) as doc:
        assert not profile_findings(doc)
    with pyropdf.open(encoded, limits=pyropdf.DocumentLimits(max_memory_size=1 << 20)) as doc:
        with pytest.raises(pyropdf.LimitError) as error:
            doc.check()
        assert error.value.code == "memory_size"
