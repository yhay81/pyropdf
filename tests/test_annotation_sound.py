"""Reading the sound of a sound annotation: ``Annotation.sound``."""

from __future__ import annotations

import io
import math
import struct
import zlib

import pikepdf
import pytest

import pyropdf
from pyropdf import AnnotationType, LimitError, MalformedPdfError, Sound, SoundEncoding

# A 16-bit stereo sine at 8 kHz, as signed big-endian samples.
_SAMPLES = b"".join(
    struct.pack(">hh", round(8000 * math.sin(i / 8)), -round(8000 * math.sin(i / 8))) for i in range(400)
)
_COMPRESSED = zlib.compress(_SAMPLES)


def _stream(entries: str, data: bytes) -> bytes:
    return f"<< {entries} /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream"


def _pdf(sounds: list[bytes]) -> bytes:
    """Return a page with one sound annotation per entry: a sound stream, or any other object."""
    annots = " ".join(f"{5 + 2 * i} 0 R" for i in range(len(sounds)))
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Annots [4 0 R {annots}] >>".encode(),
        4: b"<< /Type /Annot /Subtype /Square /Rect [10 10 20 20] >>",
    }
    for i, sound in enumerate(sounds):
        objects[5 + 2 * i] = f"<< /Type /Annot /Subtype /Sound /Rect [30 30 50 50] /Sound {6 + 2 * i} 0 R >>".encode()
        objects[6 + 2 * i] = sound
    out = bytearray(b"%PDF-1.7\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for number in sorted(objects):
        out += f"{offsets[number]:010} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


_STEREO = _stream("/Type /Sound /R 8000 /C 2 /B 16 /E /Signed /Filter /FlateDecode", _COMPRESSED)
_DEFAULTS = _stream("/R 22050.5", b"\x80\x90\xa0")


def test_the_fixture_reads_alike_in_pikepdf() -> None:
    with pikepdf.open(io.BytesIO(_pdf([_STEREO, _DEFAULTS]))) as pdf:
        stereo = pdf.pages[0].Annots[1].Sound
        assert stereo.read_bytes() == _SAMPLES
        assert stereo.read_raw_bytes() == _COMPRESSED
        assert [stereo.R, stereo.C, stereo.B] == [8000, 2, 16]
        assert str(stereo.E) == "/Signed"
        assert pdf.pages[0].Annots[2].Sound.read_bytes() == b"\x80\x90\xa0"


def test_a_sound_annotation_returns_its_samples_and_format() -> None:
    page = pyropdf.open(_pdf([_STEREO, _DEFAULTS])).pages[0]
    stereo, defaults = page.annotations[1], page.annotations[2]
    assert stereo.type is AnnotationType.SOUND

    assert stereo.sound() == Sound(
        data=_SAMPLES, rate=8000.0, channels=2, bits=16, encoding=SoundEncoding.SIGNED, compression=None
    )
    raw = stereo.sound(decode=False)
    assert raw is not None
    assert raw.data == _COMPRESSED
    assert defaults.sound() == Sound(
        data=b"\x80\x90\xa0", rate=22050.5, channels=1, bits=8, encoding=SoundEncoding.RAW, compression=None
    )


def test_compressed_and_encoded_sounds_keep_their_payload() -> None:
    sound = _stream("/R 8000 /E /muLaw /CO /SomeCodec", b"\x01\x02\x03")
    read = pyropdf.open(_pdf([sound])).pages[0].annotations[1].sound()

    assert read is not None
    assert (read.data, read.encoding, read.compression) == (b"\x01\x02\x03", SoundEncoding.MU_LAW, "SomeCodec")


def test_another_kind_of_annotation_has_no_sound() -> None:
    assert pyropdf.open(_pdf([_STEREO])).pages[0].annotations[0].sound() is None


@pytest.mark.parametrize(
    ("sound", "match"),
    [
        (_stream("/C 1", b"\x00"), "/R"),
        (_stream("/R 0", b"\x00"), "/R"),
        (_stream("/R (fast)", b"\x00"), "/R"),
        (_stream("/R 8000 /C 1.5", b"\x00"), "/C"),
        (_stream("/R 8000 /C 0", b"\x00"), "/C"),
        (_stream("/R 8000 /B -8", b"\x00"), "/B"),
        (_stream("/R 8000 /E /Opus", b"\x00"), "/E"),
        (_stream("/R 8000 /CO 3", b"\x00"), "/CO"),
        (b"<< /R 8000 >>", "/Sound"),
        (b"null", "/Sound"),
    ],
)
def test_a_malformed_sound_raises(sound: bytes, match: str) -> None:
    annotation = pyropdf.open(_pdf([sound])).pages[0].annotations[1]

    with pytest.raises(MalformedPdfError, match=match):
        annotation.sound()


def test_the_samples_are_bounded() -> None:
    annotation = pyropdf.open(_pdf([_STEREO])).pages[0].annotations[1]

    with pytest.raises(LimitError) as raised:
        annotation.sound(max_size=len(_SAMPLES) - 1)
    assert raised.value.code == "stream_data_size"
    sound = annotation.sound(max_size=len(_SAMPLES))
    assert sound is not None
    assert len(sound.data) == len(_SAMPLES)
    with pytest.raises(ValueError, match="max_size"):
        annotation.sound(max_size=0)
    with pytest.raises(TypeError, match="decode"):
        annotation.sound(decode=1)  # ty: ignore[invalid-argument-type]
