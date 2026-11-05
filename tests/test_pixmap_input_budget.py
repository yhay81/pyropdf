"""Encoded Pixmap input is bounded before copying, reading, and decoding (#697)."""

from __future__ import annotations

import io
import mmap
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from PIL import Image

from pyropdf import LimitError, Pixmap

if TYPE_CHECKING:
    from pathlib import Path


PPM = b"P6\n2 1\n255\n\xff\0\0\0\xff\0"
DEFAULT_MAX_SIZE = 256 * 1024 * 1024


@pytest.mark.parametrize("kind", ["bytes", "bytearray", "memoryview", "strided", "reversed", "multidimensional"])
def test_buffers_are_checked_by_their_exported_byte_count(kind: str) -> None:
    data: bytes | bytearray | memoryview = PPM
    if kind == "bytearray":
        data = bytearray(PPM)
    elif kind == "memoryview":
        data = memoryview(PPM)
    elif kind == "strided":
        interleaved = bytearray(len(PPM) * 2)
        interleaved[::2] = PPM
        data = memoryview(interleaved)[::2]
    elif kind == "reversed":
        data = memoryview(PPM[::-1])[::-1]
    elif kind == "multidimensional":
        data = memoryview(PPM).cast("B", (1, len(PPM)))

    with pytest.raises(LimitError) as caught:
        Pixmap.from_bytes(data, max_size=len(PPM) - 1)
    assert caught.value.code == "image_input_size"
    for limit in (len(PPM), None):
        pixmap = Pixmap.from_bytes(data, max_size=limit)
        assert (pixmap.width, pixmap.height, pixmap.resolution) == (2, 1, (96, 96))
        assert bytes(pixmap) == PPM[-6:]
    assert bytes(data) == PPM


def test_file_limit_accepts_the_boundary_and_none(tmp_path: Path) -> None:
    path = tmp_path / "image.ppm"
    path.write_bytes(PPM)
    with pytest.raises(LimitError) as caught:
        Pixmap.open(path, max_size=len(PPM) - 1)
    assert caught.value.code == "image_input_size"
    for limit in (len(PPM), None):
        assert bytes(Pixmap.open(path, max_size=limit)) == PPM[-6:]
    assert path.read_bytes() == PPM


def test_input_limit_precedes_decoding_errors(tmp_path: Path) -> None:
    invalid = b"not an image"
    path = tmp_path / "invalid"
    path.write_bytes(invalid)
    with pytest.raises(LimitError) as caught:
        Pixmap.from_bytes(invalid, max_size=1)
    assert caught.value.code == "image_input_size"
    with pytest.raises(LimitError) as caught:
        Pixmap.open(path, max_size=1)
    assert caught.value.code == "image_input_size"
    with pytest.raises(ValueError, match="the data is not"):
        Pixmap.from_bytes(invalid, max_size=None)
    with pytest.raises(ValueError, match="the data is not"):
        Pixmap.open(path, max_size=None)


def test_default_limit_rejects_large_file_and_buffer_before_copying(tmp_path: Path) -> None:
    path = tmp_path / "oversized"
    with path.open("w+b") as file:
        # Trailing padding changes encoded size, but not the two decoded pixels.
        # A sparse file and a mapping avoid allocating a giant Python buffer.
        file.write(PPM)
        file.flush()
        file.truncate(DEFAULT_MAX_SIZE + 1)
        with pytest.raises(LimitError) as caught:
            Pixmap.open(path)
        assert caught.value.code == "image_input_size"
        with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as mapping, memoryview(mapping) as data:
            with pytest.raises(LimitError) as caught:
                Pixmap.from_bytes(data)
            assert caught.value.code == "image_input_size"
    assert bytes(Pixmap.open(path, max_size=None)) == PPM[-6:]


@pytest.mark.parametrize(
    ("limit", "error"),
    [
        (True, TypeError),
        (False, TypeError),
        (1.5, TypeError),
        ("10", TypeError),
        (0, ValueError),
        (-1, ValueError),
        (1 << 100, ValueError),
    ],
)
def test_invalid_limits_are_rejected_before_buffer_access_or_file_io(
    tmp_path: Path,
    limit: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error, match="max_size must be a positive integer or None"):
        Pixmap.from_bytes(object(), max_size=limit)  # ty: ignore[invalid-argument-type]
    with pytest.raises(error, match="max_size must be a positive integer or None"):
        Pixmap.open(tmp_path / "missing", max_size=limit)  # ty: ignore[invalid-argument-type]


def test_input_limits_are_keyword_only(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="positional"):
        Pixmap.from_bytes(PPM, len(PPM))  # ty: ignore[too-many-positional-arguments]
    with pytest.raises(TypeError, match="positional"):
        Pixmap.open(tmp_path / "missing", len(PPM))  # ty: ignore[too-many-positional-arguments]


def test_missing_file_preserves_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Pixmap.open(tmp_path / "missing", max_size=1)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions")
def test_unreadable_file_preserves_permission_error(tmp_path: Path) -> None:
    path = tmp_path / "unreadable.ppm"
    path.write_bytes(PPM)
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("the current user can bypass file permissions")
        with pytest.raises(PermissionError):
            Pixmap.open(path, max_size=1)
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize("limit", [1, len(PPM), None])
def test_read_checks_actual_bytes_when_metadata_reports_zero(tmp_path: Path, limit: int | None) -> None:
    if sys.platform == "win32":
        pytest.skip("requires POSIX named pipes")
    path = tmp_path / "image.pipe"
    os.mkfifo(path)
    assert path.stat().st_size == 0

    def write() -> None:
        with path.open("wb", buffering=0) as file:
            file.write(PPM)

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(write)
        if limit == 1:
            with pytest.raises(LimitError) as caught:
                Pixmap.open(path, max_size=limit)
            assert caught.value.code == "image_input_size"
        else:
            assert bytes(Pixmap.open(path, max_size=limit)) == PPM[-6:]
        writer.result(timeout=10)


def test_read_stops_at_the_limit_without_waiting_for_eof(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("requires POSIX named pipes")
    path = tmp_path / "endless.pipe"
    os.mkfifo(path)

    def write() -> int:
        written = 0
        try:
            with path.open("wb", buffering=0) as file:
                # Larger than a pipe buffer: an unbounded reader would
                # consume it all before reporting the size error.
                for _ in range(8192):
                    written += file.write(b"x" * 4096)
        except BrokenPipeError:
            return written
        return written

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(write)
        with pytest.raises(LimitError) as caught:
            Pixmap.open(path, max_size=32)
        assert caught.value.code == "image_input_size"
        assert writer.result(timeout=10) < 8192 * 4096


def test_palette_tiff_copy_keeps_pixels_resolution_and_input(tmp_path: Path) -> None:
    image = Image.frombytes("P", (2, 1), b"\0\1")
    image.putpalette([255, 0, 0, 0, 255, 0] + [0] * (254 * 3))
    output = io.BytesIO()
    image.save(output, "TIFF", dpi=(120, 240))
    data = output.getvalue()
    source = bytearray(data)
    path = tmp_path / "palette.tiff"
    path.write_bytes(data)
    for pixmap in (Pixmap.from_bytes(source, max_size=len(data)), Pixmap.open(path, max_size=len(data))):
        assert bytes(pixmap) == image.convert("RGB").tobytes()
        assert pixmap.resolution == (120, 240)
    with pytest.raises(LimitError) as caught:
        Pixmap.from_bytes(source, max_size=len(data) - 1)
    assert caught.value.code == "image_input_size"
    assert source == data
    assert path.read_bytes() == data


def test_none_does_not_remove_the_pixel_limit() -> None:
    with pytest.raises(ValueError, match="64000000"):
        Pixmap.from_bytes(b"P6\n8001 8000\n255\n", max_size=None)
