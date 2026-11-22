"""Generate alternate resource and structure representations from the per-flavour corpus.

Fixtures retain the source's XMP claim, page, tagged figure (when present)
and output profile. The pinned veraPDF driver checks the prepared input as
well as the edited and control saves under each explicitly selected flavour.
"""

from __future__ import annotations

from pathlib import Path

import pyropdf

Name = pyropdf.Name
ROOT = Path(__file__).resolve().parents[2]


def cases(flavour: str, *, dual: bool) -> list[str]:
    archival = dual or not flavour.startswith("ua")
    result = [
        "nested-simple-font",
        "nested-unused-font",
        "nested-empty-show",
        "nested-composite-font",
        "type3-charproc",
        "type3-unused-charproc",
    ]
    if archival:
        result += ["nested-rgb", "nested-default-rgb", "inherited-default-rgb", "nested-gray", "nested-cmyk"]
    if dual or flavour.startswith("4"):
        result += ["page-intent", "page-intent-fallback"]
    if flavour.startswith("ua") or flavour.endswith("a"):
        result += [
            "named-properties",
            "parent-tree-slot",
            "parent-tree-other-element",
            "figure-actual-text",
            "figure-ancestor-alt",
            "figure-local-alt",
        ]
    return result


def indirect(doc: pyropdf.Document, value: dict) -> pyropdf.Dictionary:
    return doc.objects[doc.objects.add(value)]


def stream(doc: pyropdf.Document, data: bytes, entries: dict | None = None) -> pyropdf.Stream:
    return doc.objects[doc.objects.add_stream(data, entries)]


def form(doc: pyropdf.Document, data: bytes, resources: dict) -> pyropdf.Stream:
    return stream(
        doc, data, {"Type": Name("XObject"), "Subtype": Name("Form"), "BBox": [0, 0, 612, 792], "Resources": resources}
    )


def nest(doc: pyropdf.Document, content: bytes, resources: dict) -> None:
    """Only the invoked inner Form uses the resource. The outer Form invokes it."""
    page = doc.pages[0].obj
    inner = form(doc, content, resources)
    outer = form(doc, b"/Inner Do", {"XObject": {"Inner": inner}})
    page["Resources"] = {"XObject": {"Outer": outer}}
    data = page["Contents"].read()
    start = data.partition(b"BDC")[0] + b"BDC " if b"BDC" in data else b""
    page["Contents"].write(start + b"/Outer Do" + (b" EMC" if start else b""))


def simple(doc: pyropdf.Document) -> pyropdf.Dictionary:
    """Embed the OFL Noto Sans Hebrew subset with its 260-unit space and 1,000 units/em."""
    data = (ROOT / "crates/pyropdf-engine/src/fonts/testdata/NotoSansHebrew-subset.ttf").read_bytes()
    face = Name("NotoSansHebrew-Regular")
    desc = indirect(
        doc,
        {
            "Type": Name("FontDescriptor"),
            "FontName": face,
            "Flags": 32,
            "FontBBox": [-105, -300, 1100, 1100],
            "ItalicAngle": 0,
            "Ascent": 1068,
            "Descent": -292,
            "CapHeight": 714,
            "StemV": 80,
            "FontFile2": stream(doc, data, {"Length1": len(data)}),
        },
    )
    return indirect(
        doc,
        {
            "Type": Name("Font"),
            "Subtype": Name("TrueType"),
            "BaseFont": face,
            "FontDescriptor": desc,
            "FirstChar": 32,
            "LastChar": 32,
            "Widths": [260],
            "Encoding": Name("WinAnsiEncoding"),
        },
    )


def type3(doc: pyropdf.Document) -> pyropdf.Dictionary:
    glyph = stream(doc, b"600 0 0 0 600 700 d1 0 0 600 700 re f")
    return indirect(
        doc,
        {
            "Type": Name("Font"),
            "Subtype": Name("Type3"),
            "FontBBox": [0, 0, 600, 700],
            "FontMatrix": [0.001, 0, 0, 0.001, 0, 0],
            "CharProcs": indirect(doc, {"A": glyph, "B": glyph}),
            "Encoding": {"Type": Name("Encoding"), "Differences": [65, Name("A"), Name("B")]},
            "FirstChar": 65,
            "LastChar": 66,
            "Widths": [600, 600],
            "Resources": {},
            "ToUnicode": stream(
                doc,
                b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
                b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def "
                b"/CMapName /Glyph-UCS def /CMapType 2 def 1 begincodespacerange <00> <FF> endcodespacerange "
                b"2 beginbfchar <41> <0041> <42> <0042> endbfchar endcmap "
                b"CMapName currentdict /CMap defineresource pop end end",
            ),
        },
    )


def prepare_colour(doc: pyropdf.Document, case: str) -> None:
    page = doc.pages[0].obj
    cat = doc.catalog
    if case == "nested-gray":
        nest(doc, b"10 10 30 40 re f", {})
        return
    cs = {"ColorSpace": {"DefaultRGB": [Name("ICCBased"), cat["OutputIntents"][0]["DestOutputProfile"]]}}
    if case == "inherited-default-rgb":
        page["Parent"]["Resources"] = cs
        del page["Resources"]
        del cat["OutputIntents"]
    else:
        nest(doc, b"0.2 0.4 0.6 rg 10 10 30 40 re f", cs if case == "nested-default-rgb" else {})


def prepare(doc: pyropdf.Document, case: str) -> bytes:
    page = doc.pages[0].obj
    cat = doc.catalog
    if case in {"nested-rgb", "nested-default-rgb", "inherited-default-rgb", "nested-gray", "nested-cmyk"}:
        prepare_colour(doc, case)
    elif case.startswith("page-intent"):
        page["OutputIntents"] = list(cat["OutputIntents"])
        if case == "page-intent":
            del cat["OutputIntents"]
    elif case == "nested-composite-font":
        font = pyropdf.Font.from_file(ROOT / "crates/pyropdf-engine/src/fonts/testdata/NotoSansJP-subset.otf")
        with doc.pages[0].draw() as canvas:
            canvas.text("A", (20, 30), font=font, size=10)
        # Settle the subset in the normal save before moving its content to the Form.
        settled = doc.to_bytes()
        with pyropdf.open(settled) as ready:
            return nested_composite(ready)
    elif case in {
        "nested-simple-font",
        "nested-unused-font",
        "nested-empty-show",
        "type3-charproc",
        "type3-unused-charproc",
    }:
        font = simple(doc) if case.startswith("nested-") else type3(doc)
        char = {"nested-simple-font": b" ", "nested-empty-show": b""}.get(case, b"A")
        content = b"q Q" if case == "nested-unused-font" else b"BT /F 10 Tf 20 30 Td (" + char + b") Tj ET"
        nest(doc, content, {"Font": {"F": font}})
    else:
        prepare_structure(doc, case)
    return doc.to_bytes()


def prepare_structure(doc: pyropdf.Document, case: str) -> None:
    page = doc.pages[0].obj
    cat = doc.catalog
    figure = cat["StructTreeRoot"]["K"][0]["K"][0]
    if case == "named-properties":
        page["Resources"]["Properties"] = {"FigureProps": indirect(doc, {"MCID": 0})}
        content = page["Contents"]
        content.write(content.read().replace(b"<< /MCID 0 >>", b"/FigureProps"))
    elif case.startswith("parent-tree-"):
        tree = cat["StructTreeRoot"]["ParentTree"]
        leaf = indirect(doc, {"Limits": [0, 0], "Nums": [0, doc.objects[doc.objects.add([figure])]]})
        del tree["Nums"]
        tree["Kids"] = [leaf]
    elif case in {"figure-actual-text", "figure-local-alt"}:
        figure["ActualText"] = figure["Alt"]
        if case == "figure-actual-text":
            del figure["Alt"]
    elif case == "figure-ancestor-alt":
        figure["P"]["Alt"] = figure["Alt"]


def nested_composite(doc: pyropdf.Document) -> bytes:
    page = doc.pages[0].obj
    content = page["Contents"]
    text = content[-1].read()
    page["Contents"] = content[0]
    nest(doc, text, dict(page["Resources"]))
    return doc.to_bytes()


def edit(doc: pyropdf.Document, case: str) -> int | None:
    page = doc.pages[0].obj
    if case == "nested-cmyk":
        inner = page["Resources"]["XObject"]["Outer"]["Resources"]["XObject"]["Inner"]
        inner.write(b"0 1 0 0 k 10 10 30 40 re f")
        return inner.xref
    if case in {"nested-rgb", "nested-default-rgb", "nested-gray"}:
        owner, key = doc.catalog, "OutputIntents"
    elif case == "inherited-default-rgb":
        owner, key = page["Parent"]["Resources"]["ColorSpace"], "DefaultRGB"
    elif case.startswith("page-intent"):
        owner, key = page, "OutputIntents"
    elif case in {
        "nested-simple-font",
        "nested-unused-font",
        "nested-empty-show",
        "nested-composite-font",
        "type3-charproc",
        "type3-unused-charproc",
    }:
        fonts = page["Resources"]["XObject"]["Outer"]["Resources"]["XObject"]["Inner"]["Resources"]["Font"]
        font = fonts[next(iter(fonts))]
        if case.startswith("type3-"):
            owner, key = font["CharProcs"], "A" if case == "type3-charproc" else "B"
        else:
            if case == "nested-composite-font":
                font = font["DescendantFonts"][0]
            owner = font["FontDescriptor"]
            key = next(k for k in ("FontFile2", "FontFile3") if k in owner)
    elif case == "named-properties":
        owner, key = page["Resources"]["Properties"]["FigureProps"], "MCID"
    elif case.startswith("parent-tree-"):
        owner = doc.catalog["StructTreeRoot"]["ParentTree"]["Kids"][0]["Nums"][1]
        owner[0] = None if case == "parent-tree-slot" else doc.catalog["StructTreeRoot"]["K"][0]
        return owner.xref
    else:
        owner = doc.catalog["StructTreeRoot"]["K"][0]["K"][0]
        key = "ActualText" if case in {"figure-actual-text", "figure-local-alt"} else "Alt"
    number = owner.xref or (page["Parent"].xref if case == "inherited-default-rgb" else None)
    del owner[key]
    return number


def failed_rules(flavour: str, case: str) -> set[str]:
    """Pinned veraPDF 1.28.2 results for the destructive edit. Every prepared input passes."""
    group = flavour if flavour.startswith("ua") else flavour[0]
    if case in {"nested-simple-font", "nested-empty-show", "nested-composite-font"}:
        rule = {
            "1": "6.3.4-1",
            "2": "6.2.11.4.1-1",
            "3": "6.2.11.4.1-1",
            "4": "6.2.10.4.1-1",
            "ua1": "7.21.4.1-1",
            "ua2": "8.4.5.5.1-1",
        }[group]
        return {rule, "6.3.5-3"} if group == "1" and case == "nested-composite-font" else {rule}
    if case == "type3-charproc":
        rules = {
            "1": ("6.3.5-1", "6.3.6-1"),
            "2": ("6.2.11.4.1-2", "6.2.11.5-1"),
            "3": ("6.2.11.4.1-2", "6.2.11.5-1"),
            "4": ("6.2.10.4.1-2", "6.2.10.5-1"),
            "ua1": ("7.21.4.1-2", "7.21.5-1"),
            "ua2": ("8.4.5.5.1-2", "8.4.5.6-1"),
        }
        return set(rules[group])
    colour_rules = {
        "nested-gray": ("6.2.3.3-3", "6.2.4.3-4"),
        "nested-cmyk": ("6.2.3.3-2", "6.2.4.3-3"),
        **dict.fromkeys(("nested-rgb", "inherited-default-rgb", "page-intent"), ("6.2.3.3-1", "6.2.4.3-2")),
    }
    if case in colour_rules and not flavour.startswith("ua"):
        return {colour_rules[case][0 if group == "1" else 1]}
    if case in {"named-properties", "parent-tree-slot"} and flavour.startswith("ua"):
        return {"7.1-3" if group == "ua1" else "8.2.2-1"}
    if case in {"figure-actual-text", "figure-ancestor-alt"} and flavour.startswith("ua"):
        return {"7.3-1" if group == "ua1" else "8.2.5.28.2-1"}
    return set()
