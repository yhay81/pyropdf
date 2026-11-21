"""Legacy multimedia dictionaries built with independent PDF syntax and reopened with qpdf."""

from __future__ import annotations

import io

import pikepdf
import pytest
from test_actions import _build_raw_pdf

import pyropdf as pdf
from pyropdf import typed


def legacy_media_pdf() -> bytes:
    return _build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R /Names << /AlternatePresentations 14 0 R >> /OpenAction 8 0 R >>",
            2: "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
            3: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 40 40] /Resources <<>> /Annots [5 0 R 6 0 R 7 0 R] >>",
            4: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 40 40] /Resources <<>> /Annots [11 0 R] >>",
            5: (
                "<< /Type /Annot /Subtype /Movie /Rect [0 1 4 5] /T (intro) /Movie << /F 10 0 R "
                "/Poster 12 0 R >> /A << /Start [<0000000100000001> 600] /Duration 300 /Mode "
                "/Repeat >> >>"
            ),
            6: (
                "<< /Type /Annot /Subtype /Movie /Rect [1 2 5 6] /T (ending) /Movie << /F "
                "(out.mov) /Poster false >> /A true >>"
            ),
            7: "<< /Type /Annot /Subtype /Sound /Rect [2 3 6 7] /Sound 13 0 R >>",
            8: "<< /S /Movie /T (intro) /Next << /S /Sound /Sound 13 0 R >> >>",
            10: "<< /Type /Filespec /F (clip.mov) >>",
            11: "<< /Type /Annot /Subtype /Movie /Rect [0 1 4 5] /T (intro) /Movie << /F (other.mov) >> >>",
            12: (
                "<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray "
                "/BitsPerComponent 8 /Length 1 >>\nstream\nx\nendstream"
            ),
            13: (
                "<< /Type /Sound /F 23 0 R /R 11025 /CO /Example /CP << /Flavor 8 >> /B 16 /C 2 "
                "/E /Signed /Length 0 >>\nstream\n\nendstream"
            ),
            14: "<< /Kids [15 0 R 16 0 R] >>",
            15: "<< /Limits [(a) (a)] /Names [(a) 17 0 R] >>",
            16: "<< /Limits [(b) (b)] /Names [(b) 18 0 R] >>",
            17: "<< /Type /SlideShow /Subtype /Embedded /StartResource (file) /Resources 19 0 R >>",
            18: "<< /Type /SlideShow /Subtype /Embedded /StartResource (file) /Resources 19 0 R >>",
            19: "<< /Kids [20 0 R 21 0 R] >>",
            20: "<< /Limits [(file) (file)] /Names [(file) 22 0 R] >>",
            21: "<< /Limits [(poster) (poster)] /Names [(poster) 12 0 R] >>",
            22: "<< /Type /Filespec /F (root.svg) >>",
            23: "<< /Type /Filespec /F (speech.wav) >>",
        },
        version="1.7",
    )


def _opening(doc: pdf.Document) -> typed.ActionMovie:
    action = typed.Catalog(doc.catalog).OpenAction
    assert isinstance(action, typed.ActionMovie)
    return action


def _check_source_movie(annotation: typed.AnnotMovie) -> None:
    poster = annotation.Movie.Poster
    assert isinstance(poster, typed.XObjectImage)
    assert poster.obj.read() == b"x"
    file = annotation.Movie.F
    assert isinstance(file, typed.FileSpecification)
    assert file.F == b"clip.mov"


@pytest.mark.spec("movie-actions", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("movie-annotations", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("movies", "read", "interpret", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_title_target_movie_time_and_poster(*, incremental: bool) -> None:
    source = legacy_media_pdf()
    doc = pdf.open(source)
    action = _opening(doc)
    movie = pdf.MovieAction(doc.pages[0], action)
    annotation = movie.target
    assert annotation.obj.xref == 5
    assert movie.start == (2**32 + 1, 600)
    movie.duration = (240, 24)
    assert movie.duration == (240, 24)
    _check_source_movie(annotation)
    activation = annotation.A
    assert isinstance(activation, typed.MovieActivation)
    activation.Start = pdf.String((2**33).to_bytes(8, "big"))
    activation.Mode = "Palindrome"
    annotation.Movie.Poster = False
    movie.start = (2**35, 100)
    assert movie.start == (2**35, 100)
    assert action.obj["Start"][0] == (2**35).to_bytes(8, "big")
    movie.start = None
    movie.duration = None
    assert movie.start == (2**33, None)
    assert movie.duration == (300, None)
    with pytest.raises(ValueError, match="does not exist"):
        movie.target = "absent"
    other = doc.pages[1].annotations[0].media
    assert isinstance(other, typed.AnnotMovie)
    with pytest.raises(ValueError, match="does not exist"):
        movie.target = other
    assert action.T == "intro"
    assert action.Annotation is None
    ending = doc.pages[0].annotations[1].media
    assert isinstance(ending, typed.AnnotMovie)
    movie.target = ending
    assert movie.target.obj.xref == 6
    assert action.T is None
    movie.target = "ending"
    assert movie.target.obj.xref == 6
    assert action.Annotation is None
    saved = doc.to_bytes(incremental=incremental)
    assert not pdf.open(saved).check()
    with pikepdf.open(io.BytesIO(saved)) as independent:
        first = independent.pages[0].Annots[0]
        assert str(independent.Root.OpenAction.T) == "ending"
        assert "/Annotation" not in independent.Root.OpenAction
        assert int.from_bytes(bytes(first.A.Start), "big") == 2**33
        assert str(first.A.Mode) == "/Palindrome"
        assert first.Movie.Poster is False
        assert first.Movie.F.objgen == (10, 0)
        assert independent.get_object(12, 0).read_bytes() == b"x"


@pytest.mark.spec("sound-actions", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("sound-annotations", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("sounds", "read", "interpret", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_external_sound_and_compression_parameters(*, incremental: bool) -> None:
    doc = pdf.open(legacy_media_pdf())
    sound = doc.pages[0].annotations[2]
    action = _opening(doc).Next
    assert isinstance(action, typed.ActionSound)
    info = sound.sound()
    assert info
    assert isinstance(info.external_file, pdf.Dictionary)
    assert isinstance(info.compression_parameters, pdf.Dictionary)
    assert info.external_file["F"].text == "speech.wav"
    assert info.data == b""
    assert info.compression == "Example"
    assert info.compression_parameters["Flavor"] == 8
    assert pdf.Sound.from_stream(action.Sound.obj) == info
    info.external_file["F"] = "edited.wav"
    info.compression_parameters["Flavor"] = 16
    action.Sound.E = "ALaw"
    info = sound.sound()
    assert info
    assert info.encoding is pdf.SoundEncoding.A_LAW
    saved = doc.to_bytes(incremental=incremental)
    assert not pdf.open(saved).check()
    with pikepdf.open(io.BytesIO(saved)) as independent:
        shared = independent.Root.OpenAction.Next.Sound
        assert independent.pages[0].Annots[2].Sound.objgen == shared.objgen
        assert str(shared.F.F) == "edited.wav"
        assert str(shared.E) == "/ALaw"
        assert shared.CP.Flavor == 16
        assert shared.B == 16
        assert shared.C == 2


@pytest.mark.spec("alternate-presentations", "read", "interpret", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_shared_slide_resources_and_cross_leaf_rename(*, incremental: bool) -> None:
    doc = pdf.open(legacy_media_pdf())
    first, second = doc.alternate_presentations.values()
    assert first.start_resource == "file"
    assert first.resources["poster"].xref == 12
    with pytest.raises(KeyError):
        first.start_resource = "missing"
    with pytest.raises(ValueError, match="already exists"):
        first.rename_resource("file", "poster")
    first.rename_resource("file", "遠景.svg")
    assert list(first.resources) == ["poster", "遠景.svg"]
    assert first.start_resource == "遠景.svg"
    assert first.resources["遠景.svg"].xref == 22
    assert second.start_resource == "file"
    assert list(second.resources) == ["file", "poster"]
    first.start_resource = "poster"
    saved = doc.to_bytes(incremental=incremental)
    assert not pdf.open(saved).check()
    with pikepdf.open(io.BytesIO(saved)) as independent:
        show, untouched = [leaf.Names[1] for leaf in independent.Root.Names.AlternatePresentations.Kids]
        assert bytes(show.StartResource) == b"poster"
        assert bytes(untouched.StartResource) == b"file"
        assert untouched.Resources.objgen == (19, 0)
        expected = [(b"poster", 12), ("遠景.svg".encode(), 22)]
        for leaf, (name, xref) in zip(show.Resources.Kids, expected, strict=True):
            assert [bytes(bound) for bound in leaf.Limits] == [name, name]
            assert bytes(leaf.Names[0]) == name
            assert leaf.Names[1].objgen == (xref, 0)


@pytest.mark.spec("alternate-presentations", "read", "interpret", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_slideshow_keeps_a_resource_alias_for_opaque_media(*, incremental: bool) -> None:
    doc = pdf.open(legacy_media_pdf())
    show = doc.alternate_presentations[b"a"]
    show.rename_resource("file", "new", keep_alias=True)
    assert show.start_resource == "new"
    assert show.resources["file"] == show.resources["new"]
    with pdf.open(doc.to_bytes(incremental=incremental)) as reopened:
        retained = reopened.alternate_presentations[b"a"]
        assert retained.start_resource == "new"
        assert retained.resources["file"].xref == retained.resources["new"].xref == 22
        assert list(reopened.alternate_presentations[b"b"].resources) == ["file", "poster"]


def test_malformed_legacy_relationships_are_rejected() -> None:
    doc = pdf.open(legacy_media_pdf())
    action = _opening(doc)
    movie = pdf.MovieAction(doc.pages[0], action)
    action.Annotation = movie.target
    with pytest.raises(ValueError, match="exactly one"):
        _ = movie.target
    del action.Annotation
    doc.objects[6]["T"] = "intro"
    with pytest.raises(ValueError, match="ambiguous"):
        _ = movie.target
    doc.objects[6]["T"] = "ending"
    for value in ((-1, None), (2**63, None), (8, 0)):
        with pytest.raises(ValueError, match=r"(ticks|scale)"):
            movie.start = value
    action.obj["Start"] = [b"short", 600]
    with pytest.raises(ValueError, match="/Start"):
        _ = movie.start
    doc.objects[19]["Kids"] = [doc.objects[19]]
    with pytest.raises(pdf.PdfError, match="repeated node"):
        _ = doc.alternate_presentations[b"a"].resources
    stream = doc.objects[13]
    del stream["R"]
    assert pdf.Sound.from_stream(stream).rate is None
    stream["CP"] = False
    with pytest.raises(pdf.MalformedPdfError, match="/CP"):
        pdf.Sound.from_stream(stream)


def test_invalid_slideshow_resource_type_and_names_do_not_change_the_show() -> None:
    doc = pdf.open(legacy_media_pdf())
    show = doc.alternate_presentations[b"a"]
    names = doc.objects[20]["Names"]
    names[1] = doc.objects[2]
    with pytest.raises(pdf.PdfError, match="image XObject or a file specification"):
        show.start_resource = "file"
    assert doc.objects[17]["StartResource"] == b"file"
    names[1] = doc.objects[22]
    names[0] = b"\x80"
    with pytest.raises(pdf.PdfError, match=r"UTF-8|ordered"):
        show.rename_resource("file", "new")
    assert doc.objects[17]["Resources"].xref == 19


@pytest.mark.spec("sounds", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("sound-actions", "read", "interpret", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("encoding", list(pdf.SoundEncoding))
def test_internal_sound_encodings_parameters_and_shared_actions(
    encoding: pdf.SoundEncoding, *, incremental: bool
) -> None:
    doc = pdf.open(legacy_media_pdf())
    stream = doc.objects[13]
    del stream["F"]
    stream["R"] = 8000 if encoding in {pdf.SoundEncoding.MU_LAW, pdf.SoundEncoding.A_LAW} else 11025
    stream["B"] = 8
    stream["C"] = 1
    stream["E"] = pdf.Name(encoding)
    stream.write(b"\x81\x82", filter=None)
    action = _opening(doc).Next
    assert isinstance(action, typed.ActionSound)
    action.Mix = True
    stream["CP"] = [1, 2]
    read = pdf.Sound.from_stream(action.Sound.obj)
    assert isinstance(read.compression_parameters, pdf.Array)
    assert tuple(read.compression_parameters) == (1, 2)
    parameter = doc.objects.add_stream(b"codec parameters")
    stream["CP"] = doc.objects[parameter]
    with pdf.open(doc.to_bytes(incremental=incremental)) as reopened:
        info = reopened.pages[0].annotations[2].sound()
        assert info
        assert info.data == b"\x81\x82"
        assert info.encoding is encoding
        assert isinstance(info.compression_parameters, pdf.Stream)
        assert info.compression_parameters.read() == b"codec parameters"
        assert not reopened.check()


@pytest.mark.spec("movie-annotations", "read", "interpret", "edit", "preserve")
@pytest.mark.spec("movies", "read", "interpret", "edit", "preserve")
@pytest.mark.parametrize("incremental", [False, True])
def test_movie_uses_a_self_describing_files_poster_and_external_sound_string(*, incremental: bool) -> None:
    doc = pdf.open(legacy_media_pdf())
    movie = pdf.MovieAction(doc.pages[0], _opening(doc))
    movie.target.Movie.Poster = True
    doc.objects[13]["F"] = b"audio.au"
    sound = doc.pages[0].annotations[2].sound()
    assert sound
    assert sound.external_file == b"audio.au"
    with pikepdf.open(io.BytesIO(doc.to_bytes(incremental=incremental))) as independent:
        assert independent.pages[0].Annots[0].Movie.Poster is True
        assert bytes(independent.Root.OpenAction.Next.Sound.F) == b"audio.au"
