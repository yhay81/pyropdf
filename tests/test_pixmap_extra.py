"""Rotation, flips, perspective warps, color statistics, and pixel edits of Pixmap (issue 424)."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from pyropdf import Pixmap, Quad

WIDTH, HEIGHT = 7, 5
MODES = ["L", "LA", "RGB", "RGBA"]


def _image(mode: str) -> Image.Image:
    image = Image.new(mode, (WIDTH, HEIGHT))
    channels = len(mode)
    image.putdata(
        [
            tuple((x * 37 + y * 53 + channel * 71) % 256 for channel in range(channels))
            if channels > 1
            else (x * 37 + y * 53) % 256
            for y in range(HEIGHT)
            for x in range(WIDTH)
        ]
    )
    return image


def _pixmap(image: Image.Image) -> Pixmap:
    out = io.BytesIO()
    image.save(out, "PNG", dpi=(100, 200))
    return Pixmap.from_bytes(out.getvalue())


def _samples(image: Image.Image) -> bytes:
    return image.tobytes()


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("degrees", "transpose"),
    [
        (90, Image.Transpose.ROTATE_270),
        (180, Image.Transpose.ROTATE_180),
        (270, Image.Transpose.ROTATE_90),
        (-90, Image.Transpose.ROTATE_90),
        (450, Image.Transpose.ROTATE_270),
    ],
)
def test_rotation_turns_clockwise(mode: str, degrees: int, transpose: Image.Transpose) -> None:
    image = _image(mode)
    original = _pixmap(image)
    before = bytes(original)

    rotated = original.rotate(degrees)

    expected = image.transpose(transpose)
    assert (rotated.width, rotated.height) == expected.size
    assert bytes(rotated) == _samples(expected)
    assert rotated.resolution == ((100, 200) if degrees % 180 == 0 else (200, 100))
    assert bytes(original) == before
    assert bytes(original.rotate(0)) == before
    assert bytes(original.rotate(360)) == before


def test_rotation_takes_right_angles_only() -> None:
    with pytest.raises(ValueError, match="multiple of 90"):
        _pixmap(_image("RGB")).rotate(45)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("axis", "transpose"),
    [("horizontal", Image.Transpose.FLIP_LEFT_RIGHT), ("vertical", Image.Transpose.FLIP_TOP_BOTTOM)],
)
def test_flips_mirror(mode: str, axis: str, transpose: Image.Transpose) -> None:
    image = _image(mode)
    original = _pixmap(image)
    before = bytes(original)

    assert bytes(original.flip(axis)) == _samples(image.transpose(transpose))  # ty: ignore[invalid-argument-type]
    assert bytes(original) == before
    with pytest.raises(ValueError, match="axis"):
        original.flip("diagonal")  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("mode", MODES)
def test_a_warp_onto_the_whole_pixmap_is_the_identity(mode: str) -> None:
    pixmap = _pixmap(_image(mode))

    warped = pixmap.warp(Quad((0, 0), (WIDTH, 0), (0, HEIGHT), (WIDTH, HEIGHT)), WIDTH, HEIGHT)

    assert bytes(warped) == bytes(pixmap)


@pytest.mark.parametrize("mode", MODES)
def test_warp_corners_follow_the_quad(mode: str) -> None:
    pixmap = _pixmap(_image(mode))

    # The quad's upper left is the pixmap's upper right, its upper right the lower right.
    warped = pixmap.warp([(WIDTH, 0), (WIDTH, HEIGHT), (0, 0), (0, HEIGHT)], HEIGHT, WIDTH)

    assert bytes(warped) == bytes(pixmap.rotate(270))


def test_a_perspective_warp_maps_corners_and_straightens_a_trapezoid() -> None:
    # A white trapezoid on black: the quad picks it out, and the warp fills the result with it.
    image = Image.new("L", (40, 40))
    for y in range(10, 30):
        inset = (30 - y) // 2
        for x in range(5 + inset, 35 - inset):
            image.putpixel((x, y), 255)
    for corner, value in [((5, 29), 10), ((34, 29), 20)]:
        image.putpixel(corner, value)
    pixmap = _pixmap(image)
    quad = Quad((15, 10), (25, 10), (5, 30), (35, 30))

    warped = pixmap.warp(quad, 30, 20)

    assert (warped.width, warped.height) == (30, 20)
    assert warped.pixel(15, 10) == (255,)
    assert warped.pixel(15, 5) == (255,)
    assert warped.pixel(3, 15) == (255,)
    assert warped.pixel(0, 19)[0] < 255
    assert warped.pixel(29, 19)[0] < 255
    assert warped.color_count() > 1


def test_warp_refuses_bad_quads_and_sizes() -> None:
    pixmap = _pixmap(_image("RGB"))
    square = [(0, 0), (4, 0), (0, 4), (4, 4)]

    with pytest.raises(ValueError, match="convex"):
        pixmap.warp([(0, 0), (4, 0), (4, 4), (0, 4)], 4, 4)
    with pytest.raises(ValueError, match="convex"):
        pixmap.warp([(0, 0), (2, 0), (4, 0), (0, 4)], 4, 4)
    with pytest.raises(ValueError, match="convex"):
        pixmap.warp([(0, 0), (float("nan"), 0), (0, 4), (4, 4)], 4, 4)
    with pytest.raises(ValueError, match="pixels"):
        pixmap.warp(square, 0, 4)
    with pytest.raises(ValueError, match="pixels"):
        pixmap.warp(square, 10_000, 10_000)


def test_alpha_weights_warped_colors() -> None:
    # An opaque red pixel beside a transparent green one blends to red, not brown.
    image = Image.new("RGBA", (2, 1))
    image.putdata([(255, 0, 0, 255), (0, 255, 0, 0)])
    pixmap = _pixmap(image)

    warped = pixmap.warp([(0.5, 0), (1.5, 0), (0.5, 1), (1.5, 1)], 2, 1)

    assert warped.pixel(0, 0) == (255, 0, 0, 191)
    assert warped.pixel(1, 0) == (255, 0, 0, 64)


@pytest.mark.parametrize("mode", MODES)
def test_color_statistics_match_pillow(mode: str) -> None:
    image = _image(mode).resize((WIDTH * 3, HEIGHT), Image.Resampling.NEAREST)
    pixmap = _pixmap(image)
    counted = image.getcolors()
    assert counted is not None
    expected = {(color if isinstance(color, tuple) else (color,)): count for count, color in counted}

    assert pixmap.color_count() == len(expected)
    assert pixmap.colors() == expected
    assert list(pixmap.colors()) == sorted(expected)
    top = pixmap.top_color()
    assert top is not None
    share, color = top
    most = max(expected.values())
    assert share == most / (WIDTH * 3 * HEIGHT)
    assert color == min(key for key, count in expected.items() if count == most)


def test_color_statistics_of_a_region() -> None:
    pixmap = _pixmap(_image("RGB")).fill([1, 2, 3], (0, 0, 2, 2))

    assert pixmap.colors((0, 0, 2, 2)) == {(1, 2, 3): 4}
    assert pixmap.color_count((0, 0, 2, 2)) == 1
    assert pixmap.top_color((0, 0, 3, 2)) == (4 / 6, (1, 2, 3))
    # A region outside the pixmap has no pixels.
    assert pixmap.colors((50, 50, 60, 60)) == {}
    assert pixmap.color_count((50, 50, 60, 60)) == 0
    assert pixmap.top_color((50, 50, 60, 60)) is None
    with pytest.raises(ValueError, match="x0 < x1"):
        pixmap.colors((2, 0, 1, 1))


def test_colors_are_bounded() -> None:
    pixmap = _pixmap(_image("RGB"))

    with pytest.raises(ValueError, match="more than 3 distinct colors"):
        pixmap.colors(max_colors=3)
    assert len(pixmap.colors(max_colors=None)) == WIDTH * HEIGHT
    assert len(pixmap.colors(max_colors=WIDTH * HEIGHT)) == WIDTH * HEIGHT
    with pytest.raises(ValueError, match="positive"):
        pixmap.colors(max_colors=0)


@pytest.mark.parametrize("mode", MODES)
def test_pixels_read_and_edit_with_alpha(mode: str) -> None:
    image = _image(mode)
    original = _pixmap(image)
    before = bytes(original)
    value = (9, 8, 7, 6)[: len(mode)]

    expected = image.getpixel((3, 2))
    assert original.pixel(3, 2) == (expected if isinstance(expected, tuple) else (expected,))
    edited = original.with_pixel(3, 2, value)

    assert edited.pixel(3, 2) == value
    assert bytes(original) == before
    changed = [at for at, (a, b) in enumerate(zip(bytes(original), bytes(edited), strict=True)) if a != b]
    stride, channels = original.stride, original.channels
    assert all(at // stride == 2 and at % stride // channels == 3 for at in changed)
    assert edited.resolution == original.resolution


@pytest.mark.parametrize("mode", MODES)
def test_fill_sets_a_rectangle_cut_to_the_pixmap(mode: str) -> None:
    original = _pixmap(_image(mode))
    before = bytes(original)
    value = (200, 100, 50, 25)[: len(mode)]

    filled = original.fill(value, (5, 3, 99, 99))

    for y in range(HEIGHT):
        for x in range(WIDTH):
            inside = x >= 5 and y >= 3
            assert (filled.pixel(x, y) == value) == inside or original.pixel(x, y) == value
            if not inside:
                assert filled.pixel(x, y) == original.pixel(x, y)
    assert bytes(original) == before
    assert original.fill(value).colors() == {value: WIDTH * HEIGHT}
    # A rectangle outside the pixmap changes nothing.
    assert bytes(original.fill(value, (-9, -9, 0, 0))) == before


def test_pixel_bounds_and_values_are_checked() -> None:
    pixmap = _pixmap(_image("RGBA"))

    for x, y in [(-1, 0), (0, -1), (WIDTH, 0), (0, HEIGHT)]:
        with pytest.raises(IndexError, match="outside"):
            pixmap.pixel(x, y)
        with pytest.raises(IndexError, match="outside"):
            pixmap.with_pixel(x, y, [0, 0, 0, 0])
    with pytest.raises(ValueError, match="4 components"):
        pixmap.with_pixel(0, 0, [0, 0, 0])
    with pytest.raises(ValueError, match="4 components"):
        pixmap.fill([0, 0, 0])
    with pytest.raises(OverflowError):
        pixmap.fill([0, 0, 0, 256])
    with pytest.raises(ValueError, match="x0 < x1"):
        pixmap.fill([0, 0, 0, 0], (3, 3, 3, 4))
