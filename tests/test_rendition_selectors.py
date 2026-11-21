"""Selector preference, media sections and player settings in ISO 32000 clause 13.2."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pikepdf
import pytest
from test_multimedia import media_pdf

import pyropdf as pdf
from pyropdf import typed

if TYPE_CHECKING:
    from pathlib import Path


def _selector_pdf() -> bytes:
    """Create nested selectors and a shared clip section with an independent writer."""
    with pikepdf.open(io.BytesIO(media_pdf())) as source:
        dictionary = pikepdf.Dictionary
        name = pikepdf.Name
        screen = source.pages[0].Annots[0]
        base = screen.A.R
        section = source.make_indirect(
            dictionary(
                S=name.MCS,
                D=base.C,
                MH=dictionary(B=dictionary(S=name.T, T=dictionary(S=name.S, V=1.25))),
                BE=dictionary(E=dictionary(S=name.M, M="credits")),
            )
        )
        outer = source.make_indirect(dictionary(S=name.MCS, D=section, BE=dictionary(B=dictionary(S=name.F, F=2))))
        player = dictionary(PID=dictionary(U="vnd.adobe.swname:player"))
        first = source.make_indirect(
            dictionary(
                S=name.MR,
                N="first",
                C=outer,
                P=dictionary(PL=dictionary(MU=[player]), MH=dictionary(V=70)),
                SP=dictionary(BE=dictionary(W=1)),
            )
        )
        backup = source.make_indirect(dictionary(S=name.MR, N="backup", C=section))
        screen.A.R = source.make_indirect(
            dictionary(
                S=name.SR,
                R=[dictionary(S=name.SR, R=[first, backup]), first],
                MH=dictionary(C=dictionary(A=True)),
                BE=dictionary(C=dictionary(L=["en-US"])),
                N="selector",
            )
        )
        # The unrelated multimedia fixture has a short legacy file identifier.
        source.trailer.ID = [b"selector-fixture", b"selector-fixture"]
        output = io.BytesIO()
        source.save(output, deterministic_id=True)
        return output.getvalue()


def _read(doc: pdf.Document) -> pdf.RenditionAction:
    action = doc.pages[0].annotations[0].action
    assert isinstance(action, pdf.RenditionAction)
    return action


def _change(doc: pdf.Document) -> None:
    action = _read(doc)
    with action.edit_rendition() as selector:
        assert isinstance(selector, typed.RenditionSelector)
        assert selector.MH
        assert selector.MH.C
        assert selector.MH.C.A
        assert selector.BE
        assert selector.BE.C
        assert selector.BE.C.L
        assert selector.BE.C.L[0] == "en-US"
        one, two, repeated = action.media_candidates
        assert [one.N, two.N, repeated.N] == ["first", "backup", "first"]
        assert repeated.obj == one.obj
        outer, shared = one.C, two.C
        assert isinstance(outer, typed.MediaClipSection)
        assert isinstance(shared, typed.MediaClipSection)
        assert outer.D.obj == shared.obj
        assert outer.BE
        assert isinstance(outer.BE.B, typed.MediaOffsetFrame)
        assert outer.BE.B.F == 2
        assert shared.MH
        assert isinstance(shared.MH.B, typed.MediaOffsetTime)
        assert shared.MH.B.T.V == 1.25
        assert shared.BE
        assert isinstance(shared.BE.E, typed.MediaOffsetMarker)
        assert shared.BE.E.M == "credits"
        assert one.P
        assert one.P.MH
        assert one.P.PL
        assert one.P.PL.MU
        assert one.P.PL.MU[0].PID.U == "vnd.adobe.swname:player"
        assert one.SP
        assert one.SP.BE
        assert one.SP.BE.W == 1
        shared.MH.B.T.V = 2.75
        one.P.MH.V = 42
        # Equality and reversed same-unit bounds denote a legal empty interval.
        shared.BE.E = typed.MediaOffsetTime(
            doc.objects[doc.objects.add({"S": pdf.Name("T"), "T": {"S": pdf.Name("S"), "V": 1}})]
        )
        selector.R.obj.append(two.obj)


@pytest.mark.spec("renditions", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_shared_clip_selector_and_required_or_preferred_settings(tmp_path: Path, *, incremental: bool) -> None:
    original = _selector_pdf()
    with pdf.open(original) as doc:
        # A foreign extension sibling stays attached to the same shared section.
        shared = _read(doc).media_candidates[1].C
        assert isinstance(shared, typed.MediaClipSection)
        shared.obj["XXSibling"] = b"opaque"
        _change(doc)
        path = tmp_path / "selector.pdf"
        doc.save(path, incremental=incremental)
    if incremental:
        assert path.read_bytes().startswith(original)
    with pdf.open(path) as saved:
        assert [candidate.N for candidate in _read(saved).media_candidates] == ["first", "backup", "first", "backup"]
    with pikepdf.open(path) as independent:
        selector = independent.pages[0].Annots[0].A.R
        first, backup = selector.R[0].R
        assert selector.R[1].objgen == first.objgen
        assert selector.R[2].objgen == backup.objgen
        assert first.C.D.objgen == backup.C.objgen
        assert float(backup.C.MH.B.T.V) == 2.75
        assert backup.C.BE.E.T.V == 1
        assert bytes(backup.C.XXSibling) == b"opaque"
        assert first.P.MH.V == 42
        assert bytes(first.C.D.D.D.EF.F.read_bytes()).startswith(b"U3D")


@pytest.mark.parametrize(
    "malformation",
    ["selector-cycle", "section-cycle", "negative-time", "negative-frame", "bad-marker", "bad-timespan"],
)
def test_bad_rendition_edits_are_atomic(malformation: str) -> None:
    with pdf.open(_selector_pdf()) as doc:
        action = _read(doc)
        previous = doc.history.position
        with pytest.raises(ValueError, match=r"cycle|offset"):
            _malform(action, malformation)
        assert doc.history.position == previous
        assert action.rendition
        assert action.rendition.N == "selector"
        assert len(action.media_candidates) == 3


def _malform(action: pdf.RenditionAction, malformation: str) -> None:
    with action.edit_rendition() as selector:
        assert isinstance(selector, typed.RenditionSelector)
        first = action.media_candidates[0]
        section = first.C
        assert isinstance(section, typed.MediaClipSection)
        selector.N = "discard this edit"
        if malformation == "selector-cycle":
            selector.R.obj.append(selector.obj)
        elif malformation == "section-cycle":
            section.D = section
        else:
            bounds = {
                "negative-time": {"S": pdf.Name("T"), "T": {"S": pdf.Name("S"), "V": -1}},
                "negative-frame": {"S": pdf.Name("F"), "F": -1},
                "bad-marker": {"S": pdf.Name("M"), "M": pdf.Name("not-a-string")},
                "bad-timespan": {"S": pdf.Name("T"), "T": {"S": pdf.Name("Other"), "V": 1}},
            }
            section.obj["MH"] = {"B": bounds[malformation]}


def test_assigning_an_invalid_selector_rolls_back_before_replacing_the_action() -> None:
    with pdf.open(_selector_pdf()) as doc:
        screen = doc.pages[0].annotations[0]
        root = doc.objects[doc.objects.add({"S": pdf.Name("SR"), "R": []})]
        selector = typed.RenditionSelector(root)
        value = pdf.RenditionAction(rendition=selector, annotation=screen.xref, operation=pdf.RenditionOperation.PLAY)
        assert value.media_candidates == ()
        selector.R.obj.append(selector.obj)
        previous = doc.history.position
        with pytest.raises(ValueError, match="cycle"):
            screen.action = value
        assert doc.history.position == previous
        assert len(_read(doc).media_candidates) == 3


def test_authored_selector_retains_depth_first_order_and_empty_branches() -> None:
    with pdf.open(media_pdf()) as doc:
        screen = doc.pages[0].annotations[0]
        original = _read(doc).rendition
        assert original
        empty = {"S": pdf.Name("SR"), "R": []}
        fallback = {"S": pdf.Name("MR"), "P": {}, "N": "play defaults"}
        root = doc.objects[doc.objects.add({"S": pdf.Name("SR"), "R": [empty, original.obj, fallback]})]
        screen.action = pdf.RenditionAction(
            annotation=screen.xref, operation=pdf.RenditionOperation.PLAY, rendition=typed.RenditionSelector(root)
        )
        assert [leaf.N for leaf in _read(doc).media_candidates] == ["Empty scene", "play defaults"]
        with pikepdf.open(io.BytesIO(doc.to_bytes())) as independent:
            candidates = independent.pages[0].Annots[0].A.R.R
            assert len(candidates[0].R) == 0
            assert candidates[1].C.S == "/MCD"
            assert "/P" in candidates[2]


def test_shared_candidate_visits_count_toward_the_operation_budget() -> None:
    with pikepdf.open(io.BytesIO(_selector_pdf())) as authored:
        selector = authored.pages[0].Annots[0].A.R
        leaf = selector.R[1]
        selector.R = authored.make_indirect(pikepdf.Array([leaf] * 512))
        output = io.BytesIO()
        authored.save(output)
    raw = output.getvalue()
    with pdf.open(raw, limits=pdf.DocumentLimits(max_memory_size=64 * 1024)) as doc:
        action = _read(doc)
        with pytest.raises(pdf.LimitError) as caught:
            _ = action.media_candidates
        assert caught.value.code == "memory_size"
    with pdf.open(raw) as doc:
        assert len(_read(doc).media_candidates) == 512
