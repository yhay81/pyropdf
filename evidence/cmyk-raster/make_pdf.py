# ruff: noqa: INP001, D103 - a script that writes the test file.
"""Write `patches.pdf` and `patches.json`, the colour patches that compare.py samples.

    python3 evidence/cmyk-raster/make_pdf.py

One 660 by 180 point page holds 16 patches of 60 by 60 points in two rows of
eight: DeviceCMYK fills, DeviceRGB and DeviceGray fills, two DeviceCMYK
images, and three fills composited at 50 % opacity, where blending in CMYK
and in RGB give different results. The page has no transparency group, so a
CMYK renderer blends in its output space. `patches.json` lists each patch's
name, what it paints, and its lower-left corner in points.

`lanes-cmy.pdf` and `lanes-k.pdf` are the two passes of the lane design in
README.md: every DeviceCMYK colour `c m y k` becomes the DeviceRGB colour
`1-c 1-m 1-y` in the first and `1-k 1-k 1-k` in the second, images likewise.
The other patches stay as they are; compare.py reports only the DeviceCMYK
ones for the lanes.
"""

import json
import re
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).parent
SIZE, GAP, WIDTH, HEIGHT = 60, 20, 660, 180

# (name, description, content). Content paints the unit square, which the
# caller scales to the patch; `Im0` and `Im1` are 1 by 1 DeviceCMYK images.
PATCHES = [
    ("cmyk-c", "1 0 0 0 k", "1 0 0 0 k 0 0 1 1 re f"),
    ("cmyk-m", "0 1 0 0 k", "0 1 0 0 k 0 0 1 1 re f"),
    ("cmyk-y", "0 0 1 0 k", "0 0 1 0 k 0 0 1 1 re f"),
    ("cmyk-k", "0 0 0 1 k", "0 0 0 1 k 0 0 1 1 re f"),
    ("cmyk-k50", "0 0 0 0.5 k", "0 0 0 0.5 k 0 0 1 1 re f"),
    ("rich-black", "0.6 0.4 0.4 1 k", "0.6 0.4 0.4 1 k 0 0 1 1 re f"),
    ("cmyk-tint", "0.2 0.4 0.6 0.1 k", "0.2 0.4 0.6 0.1 k 0 0 1 1 re f"),
    ("image-k", "DeviceCMYK image 0 0 0 255", "/Im0 Do"),
    ("image-tint", "DeviceCMYK image 51 102 153 26", "/Im1 Do"),
    ("rgb-red", "1 0 0 rg", "1 0 0 rg 0 0 1 1 re f"),
    ("rgb-blue", "0 0 1 rg", "0 0 1 rg 0 0 1 1 re f"),
    ("rgb-orange", "1 0.5 0 rg", "1 0.5 0 rg 0 0 1 1 re f"),
    ("gray-50", "0.5 g", "0.5 g 0 0 1 1 re f"),
    ("k50-over-y", "0 0 0 1 k at ca 0.5 over 0 0 1 0 k", "0 0 1 0 k 0 0 1 1 re f /Half gs 0 0 0 1 k 0 0 1 1 re f"),
    ("c50-over-m", "1 0 0 0 k at ca 0.5 over 0 1 0 0 k", "0 1 0 0 k 0 0 1 1 re f /Half gs 1 0 0 0 k 0 0 1 1 re f"),
    ("k-multiply-y", "0 0 0 1 k /Multiply over 0 0 1 0 k", "0 0 1 0 k 0 0 1 1 re f /Mul gs 0 0 0 1 k 0 0 1 1 re f"),
]


def layout() -> list[dict[str, object]]:
    patches = []
    for index, (name, paints, content) in enumerate(PATCHES):
        column, row = index % 8, index // 8
        x = GAP + column * (SIZE + GAP)
        y = HEIGHT - GAP - SIZE - row * (SIZE + GAP)
        patches.append({"name": name, "paints": paints, "content": content, "x": x, "y": y, "size": SIZE})
    return patches


CMYK = re.compile(r"([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+) k")
IMAGES = (bytes([0, 0, 0, 255]), bytes([51, 102, 153, 26]))


def lane(pick: Callable[[list[float]], list[float]]) -> Callable[[str], str]:
    def replace(match: re.Match[str]) -> str:
        return " ".join(f"{1 - v:g}" for v in pick([float(v) for v in match.groups()])) + " rg"

    return lambda content: CMYK.sub(replace, content)


LANES = {
    "lanes-cmy.pdf": (lane(lambda cmyk: cmyk[:3]), lambda data: bytes(255 - v for v in data[:3])),
    "lanes-k.pdf": (lane(lambda cmyk: cmyk[3:] * 3), lambda data: bytes([255 - data[3]] * 3)),
}


def pdf(
    patches: list[dict[str, object]],
    colours: Callable[[str], str] = str,
    pixels: Callable[[bytes], bytes] | None = None,
) -> bytes:
    content = "".join(
        f"q {p['size']} 0 0 {p['size']} {p['x']} {p['y']} cm {colours(str(p['content']))} Q\n" for p in patches
    ).encode()
    space, length = ("DeviceCMYK", 4) if pixels is None else ("DeviceRGB", 3)
    image = (
        f"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /{space} "
        f"/BitsPerComponent 8 /Length {length} >>"
    )
    data = [pixels(i) if pixels else i for i in IMAGES]
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {WIDTH} {HEIGHT}] /Contents 4 0 R "
        "/Resources << /XObject << /Im0 5 0 R /Im1 6 0 R >> "
        "/ExtGState << /Half << /ca 0.5 >> /Mul << /BM /Multiply >> >> >> >>".encode(),
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream",
        image.encode() + b"\nstream\n" + data[0] + b"\nendstream",
        image.encode() + b"\nstream\n" + data[1] + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def main() -> None:
    patches = layout()
    (HERE / "patches.pdf").write_bytes(pdf(patches))
    for name, (colours, pixels) in LANES.items():
        (HERE / name).write_bytes(pdf(patches, colours, pixels))
    (HERE / "patches.json").write_text(json.dumps(patches, indent=1) + "\n")


if __name__ == "__main__":
    main()
