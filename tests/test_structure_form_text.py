"""Tagged PDF controls with independent MCID spaces and a reused Form XObject."""

from __future__ import annotations

import io
from pathlib import Path

import pikepdf
import pytest

import pyropdf as pdf

SOURCE = Path(__file__).parent / "assets" / "tagged-reading" / "shared-forms.pdf"


@pytest.mark.spec("logical-structure", "interpret")
@pytest.mark.spec("tagged-pdf", "interpret")
@pytest.mark.parametrize("incremental", [False, True])
def test_reused_form_expansion_preserves_the_declared_source_and_logical_order(*, incremental: bool) -> None:
    original = SOURCE.read_bytes()
    with pdf.open(original) as doc:
        doc.catalog["ForeignNote"] = "unrelated"
        data = doc.to_bytes(incremental=incremental)
        if incremental:
            assert data.startswith(original)
    with pdf.open(data) as viewed:
        plain = viewed.structure.reading_order()
        assert [row.text for row in plain] == [None, "Page one", None, "Page two", None, "日本"]
        assert [r.text for r in viewed.structure.reading_order(expand_abbreviations=True)] == [
            "Shared expanded",
            "Page one",
            None,
            "Page two",
            None,
            "日本",
        ]
        assert [r.item for r in plain] == [
            pdf.MarkedContentReference(page=1, mcid=0, stream_xref=11),
            pdf.MarkedContentReference(page=0, mcid=0),
            pdf.MarkedContentReference(page=0, mcid=0, stream_xref=10),
            pdf.MarkedContentReference(page=1, mcid=0),
            pdf.MarkedContentReference(page=0, mcid=1),
            None,
        ]
    with pikepdf.open(io.BytesIO(data)) as independent:
        elements = independent.Root.StructTreeRoot.K.K
        shared = independent.pages[1].Resources.XObject.Shared
        assert elements[0].K.Stm.objgen == elements[-1].K.Stm.objgen == shared.objgen
        assert elements[0].Pg.objgen == independent.pages[1].obj.objgen
        assert elements[-1].Pg.objgen == independent.pages[0].obj.objgen
        assert elements[2].K.Stm.objgen == independent.pages[0].Resources.XObject.Group.Resources.XObject.Inner.objgen
        assert str(shared.Resources.Properties.S.E) == "Shared expanded"
        assert str(elements[-1].ActualText) == "日本"


@pytest.mark.spec("tagged-pdf", "interpret")
@pytest.mark.parametrize("replacement", ["", "shared replacement"])
def test_form_replacement_and_expansion_follow_the_whole_target_sequence(replacement: str) -> None:
    with pdf.open(SOURCE) as doc:
        shared = doc.objects[11]
        props = shared["Resources"]["Properties"]["S"]
        props["ActualText"] = replacement
        assert doc.structure.reading_order()[0].text == replacement
        assert doc.structure.reading_order(expand_abbreviations=True)[0].text == "Shared expanded"
        # A nested replacement covers only part of the referenced MCID. Text
        # painted outside it still needs the glyph origin from the interpreter.
        del props["ActualText"]
        shared.write(b"/P /S BDC /Span << /ActualText (part) >> BDC EMC BT /F 12 Tf (rest) Tj ET EMC")
        assert doc.structure.reading_order()[0].text is None
        # The two forms may share resource dictionaries with a reference cycle.
        # Resolving a complete substitution never executes XObject resources.
        shared["Resources"]["XObject"] = {"Shared": shared}
        shared.write(b"/Artifact BMC /Span /S BDC BT /F 12 Tf (stamp) Tj ET EMC EMC")
        assert doc.structure.reading_order(expand_abbreviations=True)[0].text == ""


def _only_form(doc: pdf.Document) -> None:
    root = doc.structure[0]
    root.obj["K"] = root.children[0].obj


def test_form_stream_uses_the_document_operation_limits_and_cancellation() -> None:
    # The decoded-stream size limit applies to filtered content.
    with pdf.open(SOURCE) as source:
        form = source.objects[11]
        form.write(form.read(), filter="FlateDecode")
        filtered = source.to_bytes()
    limited = pdf.DocumentLimits(max_page_content_size=1)
    with pdf.open(filtered, limits=limited) as doc:
        _only_form(doc)
        with pytest.raises(pdf.LimitError) as caught:
            doc.structure.reading_order()
        assert caught.value.code == "page_content_size"
    token = pdf.CancellationToken()
    with pdf.open(SOURCE, limits=pdf.DocumentLimits(cancellation=token)) as doc:
        _only_form(doc)
        token.cancel()
        with pytest.raises(pdf.LimitError) as caught:
            doc.structure.reading_order()
        assert caught.value.code == "cancelled"


def test_bad_form_reference_and_unclosed_mark_do_not_return_partial_text() -> None:
    with pdf.open(SOURCE) as doc:
        _only_form(doc)
        shared = doc.objects[11]
        shared.write(b"/P /S BDC")
        with pytest.raises(pdf.MalformedPdfError, match="unclosed"):
            doc.structure.reading_order(expand_abbreviations=True)
        doc.structure[0].children[0].obj["K"]["Stm"] = doc.pages[0].obj
        with pytest.raises(pdf.MalformedPdfError, match="child"):
            doc.structure.reading_order()
