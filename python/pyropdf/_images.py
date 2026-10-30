"""Images: the pixels and stored data of the images that a page paints, and replacing an image object.

``page.images()`` lists :class:`PageImage` records in the paint order of
:meth:`Page.content`. hayro decodes an image's pixels as it does to render
the page, and the data of an image filter (ISO 32000-2 table 6) passes
through as stored. ``doc.replace_image`` and ``doc.remove_image`` rewrite an
image object in place, so every placement of it changes in one transaction.
"""

from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pyropdf import _core
from pyropdf._errors import PdfError
from pyropdf._objects import Array, Dictionary, Name, Stream
from pyropdf._operations import InlineImage, Operation, Operations
from pyropdf._recolor import (
    _MAX_FORM_DEPTH,
    _MAX_FORMS,
    _MAX_INLINE_PIXELS,
    _STREAM_KEYS,
    _inline_data,
    _repoint_page,
    _resources,
    _set_appearance,
    _sub,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pyropdf._content import Image
    from pyropdf._document import Document
    from pyropdf._page import Page
    from pyropdf._pixmap import Pixmap

#: The file extension of the data of each image filter.
_EXTENSIONS = {"DCTDecode": "jpg", "JPXDecode": "jp2", "JBIG2Decode": "jb2", "CCITTFaxDecode": "ccitt"}
#: The keys of an image dictionary that describe its data, which a replacement rewrites.
_DATA_KEYS = (
    "Width",
    "Height",
    "BitsPerComponent",
    "ColorSpace",
    "Decode",
    "DecodeParms",
    "Filter",
    "ImageMask",
    "Mask",
    "SMask",
    "SMaskInData",
)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JP2_SIGNATURE = b"\x00\x00\x00\x0cjP  \r\n\x87\n"
_J2K_SIGNATURE = b"\xff\x4f\xff\x51"
#: JPEG markers that start a frame and state its size (ITU T.81 table B.1).
_JPEG_FRAMES = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})
_DEVICE_SPACES = {1: "DeviceGray", 3: "DeviceRGB", 4: "DeviceCMYK"}
_CMYK = 4
_MARKER = 0xFF
#: The APP14 marker of Adobe's JPEGs.
_APP14 = 0xEE


@dataclass(frozen=True, slots=True)
class StoredImage:
    """An image's data as the document stores it, for an image filter that a file format holds."""

    filter: str
    """The image filter: ``DCTDecode`` (JPEG), ``JPXDecode`` (JPEG 2000), ``JBIG2Decode``, or ``CCITTFaxDecode``."""
    data: bytes
    """The data that the filter decodes, the stream's other filters decoded."""
    parameters: Dictionary | None
    """The filter's ``/DecodeParms``, such as ``/JBIG2Globals`` or CCITT's ``/K`` and ``/Columns``."""

    @property
    def extension(self) -> str:
        """Return the usual file extension of the data: ``jpg``, ``jp2``, ``jb2``, or ``ccitt``."""
        return _EXTENSIONS[self.filter]


@dataclass(frozen=True, slots=True)
class PageImage:
    """One image that a page paints: its content item and its place among the page's images."""

    page: Page
    index: int
    """The image's position among the page's images, in paint order."""
    item: Image
    """The :class:`Image` item of :meth:`Page.content`."""
    annotations: bool
    """Whether annotation appearances count, as ``Page.content(annotations=...)``."""

    def pixmap(self) -> Pixmap:
        """Return the image's pixels as hayro decodes them, with its mask as alpha.

        A stencil mask returns black with the mask as alpha. The decoded
        bytes count against the document's ``total_decompressed_size``
        limit; :class:`PdfError` is raised for an image that hayro cannot
        decode.
        """
        page = self.page
        pixmap = _core.page_image(
            page._document._core(),  # noqa: SLF001
            page._number,  # noqa: SLF001
            page._generation,  # noqa: SLF001
            self.index,
            self.annotations,
        )
        if pixmap is None:
            msg = f"image {self.index} of the page cannot be decoded"
            raise PdfError(msg)
        return pixmap

    def stored(self) -> StoredImage | None:
        """Return the image's data as stored, or ``None`` for an inline image or one without an image filter."""
        if self.item.xref == 0:
            return None
        stream = self.page._document.objects.get(self.item.xref)  # noqa: SLF001
        if not isinstance(stream, Stream):
            return None
        stored = _core.image_stored(self.page._document._core(), (self.item.xref, stream.generation or 0))  # noqa: SLF001
        if stored is None:
            return None
        filter_name, data = stored
        parameters = stream.get("DecodeParms")
        if isinstance(parameters, Array):
            parameters = parameters[-1] if len(parameters) else None
        return StoredImage(filter_name, data, parameters if isinstance(parameters, Dictionary) else None)


def page_images(page: Page, *, annotations: bool) -> list[PageImage]:
    """Return the images that ``page`` paints, in paint order."""
    content = page.content(text_runs=False, paths=False, images=True, annotations=annotations)
    return [PageImage(page, index, item, annotations) for index, item in enumerate(content.images)]


def _jpeg_frame(data: bytes) -> tuple[int, int, int, bool]:
    """Return a JPEG's width, height, components, and whether an Adobe marker inverts its CMYK."""
    at, adobe = 2, False
    while at + 4 <= len(data):
        if data[at] != _MARKER:
            break
        marker = data[at + 1]
        if marker == _MARKER:
            at += 1
            continue
        (length,) = struct.unpack(">H", data[at + 2 : at + 4])
        if marker == _APP14 and data[at + 4 : at + 9] == b"Adobe":
            adobe = True
        if marker in _JPEG_FRAMES and at + 10 <= len(data):
            height, width = struct.unpack(">HH", data[at + 5 : at + 9])
            return width, height, data[at + 9], adobe
        at += 2 + length
    msg = "the JPEG data has no frame header"
    raise ValueError(msg)


def _jpx_size(data: bytes) -> tuple[int, int]:
    """Return the width and height of JPEG 2000 data, a JP2 file or a codestream."""
    at = data.find(b"ihdr") if data.startswith(_JP2_SIGNATURE) else -1
    if at >= 0 and at + 12 <= len(data):
        height, width = struct.unpack(">II", data[at + 4 : at + 12])
        return width, height
    at = data.find(_J2K_SIGNATURE)
    if at >= 0 and at + 24 <= len(data):
        width, height, x, y = struct.unpack(">IIII", data[at + 8 : at + 24])
        return width - x, height - y
    msg = "the JPEG 2000 data has no image header"
    raise ValueError(msg)


def _image_stream(document: Document, xref: int) -> Stream:
    if isinstance(xref, bool) or not isinstance(xref, int):
        msg = f"xref must be an int: {xref!r}"
        raise TypeError(msg)
    stream = document.objects.get(xref)
    if not isinstance(stream, Stream) or stream.get("Subtype") != "Image":
        msg = f"object {xref} is not an image XObject"
        raise ValueError(msg)
    return stream


def _write_samples(  # noqa: PLR0913 - the parts of one decoded image
    document: Document, stream: Stream, width: int, height: int, *, gray: bool, color: bytes, alpha: bytes | None
) -> None:
    stream.write(color, filter="FlateDecode")
    stream.update(
        {
            "Width": width,
            "Height": height,
            "BitsPerComponent": 8,
            "ColorSpace": Name("DeviceGray" if gray else "DeviceRGB"),
        }
    )
    if alpha is not None and alpha.count(0xFF) != len(alpha):
        mask = {
            "Type": Name("XObject"),
            "Subtype": Name("Image"),
            "Width": width,
            "Height": height,
            "BitsPerComponent": 8,
            "ColorSpace": Name("DeviceGray"),
        }
        stream["SMask"] = document.objects[document.objects.add_stream(alpha, mask)]


def replace_image(document: Document, xref: int, data: bytes) -> None:
    """Replace image object ``xref`` with JPEG, JPEG 2000, or PNG ``data`` in one transaction."""
    stream = _image_stream(document, xref)
    with document.transaction("replace image"):
        _store_image(document, stream, bytes(data))


def add_image(document: Document, data: bytes) -> int:
    """Add JPEG, JPEG 2000, or PNG ``data`` as a new image object, in the current transaction, and return its number."""
    data = bytes(data)
    xref = document.objects.add_stream(b"", {"Type": Name("XObject"), "Subtype": Name("Image")}, filter=None)
    _store_image(document, document.objects[xref], data)
    return xref


def _store_image(document: Document, stream: Stream, data: bytes) -> None:
    """Write encoded ``data`` into image ``stream``, storing JPEG and JPEG 2000 as they are."""
    if data.startswith(b"\xff\xd8\xff"):
        width, height, components, adobe = _jpeg_frame(data)
        if components not in _DEVICE_SPACES:
            msg = f"a JPEG with {components} components has no device color space"
            raise ValueError(msg)
        entries: dict[str, object] = {
            "Width": width,
            "Height": height,
            "BitsPerComponent": 8,
            "ColorSpace": Name(_DEVICE_SPACES[components]),
            "Filter": Name("DCTDecode"),
        }
        if components == _CMYK and adobe:
            # Adobe's CMYK JPEGs store inverted samples.
            entries["Decode"] = [1, 0] * 4
    elif data.startswith((_JP2_SIGNATURE, _J2K_SIGNATURE)):
        width, height = _jpx_size(data)
        entries = {"Width": width, "Height": height, "Filter": Name("JPXDecode")}
    elif data.startswith(_PNG_SIGNATURE):
        width, height, gray, color, alpha = _core.decode_png(document._core(), data)  # noqa: SLF001
        _clear(stream)
        _write_samples(document, stream, width, height, gray=gray, color=color, alpha=alpha)
        return
    else:
        msg = "the image data is not JPEG, JPEG 2000, or PNG"
        raise ValueError(msg)
    _clear(stream)
    stream.write(data, filter=None)
    stream.update(entries)


def _clear(stream: Stream) -> None:
    for key in _DATA_KEYS:
        if key in stream:
            del stream[key]


def remove_image(document: Document, xref: int) -> None:
    """Make image object ``xref`` one transparent pixel, which every placement then paints, in one transaction."""
    stream = _image_stream(document, xref)
    with document.transaction("remove image"):
        _clear(stream)
        _write_samples(document, stream, 1, 1, gray=True, color=b"\x00", alpha=b"\x00")


@dataclass(frozen=True, slots=True)
class ImageRewriteReport:
    """What :meth:`Document.rewrite_images` did.

    The byte counts compare the stored data of the rewritten images before
    and after, not the size of the saved document.
    """

    considered: int
    """The image objects that the pages place, each counted once."""
    rewritten: int
    """The image objects whose smaller data replaced their own."""
    bytes_before: int
    """The stored bytes of the rewritten images before the call."""
    bytes_after: int
    """Their stored bytes after it."""
    unsupported: tuple[str, ...] = ()
    """The selected images left as they are because a rewrite would not keep
    them, such as ``"object 12: a CMYK JPEG"``, at most 1,024."""

    @property
    def skipped(self) -> int:
        """Return the image objects left as they were: ``considered - rewritten``."""
        return self.considered - self.rewritten

    @property
    def bytes_saved(self) -> int:
        """Return ``bytes_before - bytes_after``."""
        return self.bytes_before - self.bytes_after


#: The resolutions that ``rewrite_images`` accepts, in pixels per inch.
_MIN_DPI, _MAX_DPI = 36, 2400
#: The JPEG quality of a resampled lossy image when ``quality`` is ``None``.
_DEFAULT_QUALITY = 75
#: The share of its stored data that a rewrite must save to be kept, so a repeated call leaves a JPEG alone.
_MIN_SAVING = 0.05
#: Nesting of a mask's entries that a scratch document copies, as /DecodeParms over /JBIG2Globals.
_MAX_TRANSFER_DEPTH = 4
#: Sources above this many pixels are skipped before they are decoded.
_MAX_SOURCE_PIXELS = 64_000_000
_POINTS_PER_INCH = 72
#: The image filters whose data is lossy; their images are rewritten as JPEG.
_LOSSY_FILTERS = frozenset({"DCTDecode", "JPXDecode"})
_BITONAL_FILTERS = frozenset({"JBIG2Decode", "CCITTFaxDecode"})
#: The color spaces whose decoded colors, as hayro paints them, are their samples.
_PAINTED_SPACES = {"DeviceGray": 1, "DeviceRGB": 3}
#: The color spaces whose stored samples a rewrite resamples and keeps, with the space itself.
_SAMPLE_SPACES = frozenset({"DeviceCMYK", "ICCBased", "CalGray", "CalRGB"})
_BITS = 8
_MAX_QUALITY = 100
_LOSSLESS_FORMATS = ("flate", "jpeg")
_BITONAL_FORMATS = ("fax", "flate")
_PAM_TYPES = {1: "GRAYSCALE", 2: "GRAYSCALE_ALPHA", 3: "RGB", 4: "RGB_ALPHA"}
#: Inverts the bits of a byte: PBM's 1 is black, and a DeviceGray sample's 1 is white.
_INVERT = bytes(range(255, -1, -1))
#: The inline image abbreviations of ISO 32000-2 table 91 and 92 that a rewrite reads.
_INLINE_SPACES = {"G": 1, "DeviceGray": 1, "RGB": 3, "DeviceRGB": 3}
_INLINE_DCT = frozenset({"DCT", "DCTDecode"})


@dataclass(slots=True)
class _Candidate:
    """One image object that ``rewrite_images`` selected, and the largest size that its placements paint it at."""

    image: PageImage
    kind: str
    """``"lossy"``, ``"lossless"``, or ``"bitonal"``."""
    gray: bool
    samples: bool
    """Whether the stored samples are resampled, in the image's own color space, rather than hayro's colors."""
    width: float = 0.0
    """The longest length, in points, that a placement gives the image's width."""
    height: float = 0.0


@dataclass(frozen=True, slots=True)
class _Options:
    threshold: float | None
    target: float
    quality: int | None
    selected: dict[str, bool]
    set_to_gray: bool
    lossless_format: str
    bitonal_format: str

    def jpeg(self, kind: str) -> bool:
        return kind == "lossy" or (kind == "lossless" and self.lossless_format == "jpeg")


@dataclass(slots=True)
class _Pixels:
    """Interleaved 8-bit samples of one to four components, and straight alpha."""

    width: int
    height: int
    components: int
    color: bytes
    alpha: bytes | None


@dataclass(slots=True)
class _Tally:
    considered: int = 0
    rewritten: int = 0
    before: int = 0
    after: int = 0
    unsupported: dict[str, None] | None = None

    def report(self, detail: str) -> None:
        if self.unsupported is None:
            self.unsupported = {}
        if len(self.unsupported) < _MAX_UNSUPPORTED:
            self.unsupported[detail] = None

    def kept(self, before: int, after: int | None) -> None:
        if after is not None:
            self.rewritten += 1
            self.before += before
            self.after += after


#: The most entries that :attr:`ImageRewriteReport.unsupported` lists.
_MAX_UNSUPPORTED = 1024


def _rewrite_dpi(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{name} must be a number from {_MIN_DPI} through {_MAX_DPI}: {value!r}"
        raise TypeError(msg)
    if not (math.isfinite(value) and _MIN_DPI <= value <= _MAX_DPI):
        msg = f"{name} must be a number from {_MIN_DPI} through {_MAX_DPI}: {value!r}"
        raise ValueError(msg)
    return float(value)


def _check_formats(lossless_format: object, bitonal_format: object) -> None:
    for name, value, choices in (
        ("lossless_format", lossless_format, _LOSSLESS_FORMATS),
        ("bitonal_format", bitonal_format, _BITONAL_FORMATS),
    ):
        if value not in choices:
            msg = f"{name} must be one of {', '.join(map(repr, choices))}: {value!r}"
            raise ValueError(msg)


def _rewrite_options(
    dpi_threshold: float | None, dpi_target: float | None, quality: int | None, flags: dict[str, bool]
) -> tuple[float | None, float]:
    """Validate the options of ``rewrite_images`` and return its threshold and target."""
    for name, value in flags.items():
        if not isinstance(value, bool):
            msg = f"{name} must be a bool: {value!r}"
            raise TypeError(msg)
    if quality is not None:
        if isinstance(quality, bool) or not isinstance(quality, int):
            msg = f"quality must be an integer from 1 through 100: {quality!r}"
            raise TypeError(msg)
        if not 1 <= quality <= _MAX_QUALITY:
            msg = f"quality must be an integer from 1 through 100: {quality!r}"
            raise ValueError(msg)
    if dpi_threshold is None:
        if dpi_target is not None:
            msg = "dpi_target needs a dpi_threshold"
            raise ValueError(msg)
        return None, 0.0
    threshold = _rewrite_dpi("dpi_threshold", dpi_threshold)
    if dpi_target is None:
        msg = "dpi_threshold needs a dpi_target"
        raise ValueError(msg)
    target = _rewrite_dpi("dpi_target", dpi_target)
    if target >= threshold:
        msg = f"dpi_target ({dpi_target}) must be less than dpi_threshold ({dpi_threshold})"
        raise ValueError(msg)
    return threshold, target


def _filters(value: object) -> list[str]:
    if isinstance(value, Name):
        return [str(value)]
    return [str(name) for name in value] if isinstance(value, (Array, tuple, list)) else []


def _family(value: object) -> str:
    if isinstance(value, Name):
        return str(value)
    if isinstance(value, (Array, tuple, list)) and len(value) and isinstance(value[0], Name):
        return str(value[0])
    return ""


def _mask_reason(stream: Stream, item: Image) -> str | None:
    """Return why a rewrite cannot keep an image's mask, or ``None`` when it can."""
    if stream.get("SMaskInData"):
        return "JPEG 2000 data with its own alpha"
    mask = stream.get("SMask")
    if isinstance(mask, Stream) and "Matte" in mask:
        matte = mask["Matte"]
        if (mask.get("Width"), mask.get("Height")) != (item.width, item.height):
            return "a soft mask with /Matte of another size"
        if not isinstance(matte, Array) or len(matte) != item.components:
            return "a soft mask whose /Matte does not match the image's components"
    for key, what in (("SMask", "soft mask"), ("Mask", "stencil mask")):
        mask = stream.get(key)
        if isinstance(mask, Stream) and _other_size(mask, item):
            width, height = mask.get("Width"), mask.get("Height")
            if not (isinstance(width, int) and isinstance(height, int) and 0 < width * height <= _MAX_SOURCE_PIXELS):
                return f"a {what} of another size that a rewrite does not decode"
    return None


def _other_size(mask: Stream, item: Image) -> bool:
    return (mask.get("Width"), mask.get("Height")) != (item.width, item.height)


def _classify(stream: Stream, item: Image, selected: dict[str, bool]) -> tuple[str, bool, bool] | str | None:
    """Return a selected image's kind, whether it is gray, and whether its stored samples are resampled.

    An image that its kind and color flags leave out returns ``None``, and
    one selected but not rewritten returns why.
    """
    names = _filters(stream.get("Filter"))
    image_filter = names[-1] if names else ""
    bitonal = item.is_stencil or item.bits == 1 or image_filter in _BITONAL_FILTERS
    kind = "bitonal" if bitonal else "lossy" if image_filter in _LOSSY_FILTERS else "lossless"
    gray = bitonal or item.components == 1
    if not selected[kind] or not (bitonal or selected["gray" if gray else "color"]):
        return None
    reason = _unsupported(stream, item, image_filter, bitonal=bitonal) or _mask_reason(stream, item)
    if reason is not None:
        return reason
    return kind, gray, not bitonal and _PAINTED_SPACES.get(item.colorspace) != item.components


def _unsupported(stream: Stream, item: Image, image_filter: str, *, bitonal: bool) -> str | None:
    """Return why a rewrite would not keep an image's samples, or ``None`` when it would."""
    family = item.colorspace or "no color space"
    stored = not bitonal and _PAINTED_SPACES.get(item.colorspace) != item.components
    checks = (
        (item.is_stencil, "a stencil mask"),
        (item.width * item.height > _MAX_SOURCE_PIXELS, f"more than {_MAX_SOURCE_PIXELS:,} pixels"),
        (bitonal and item.colorspace != "DeviceGray", f"a bitonal image in {family}"),
        (image_filter == "JPXDecode" and not item.colorspace, "JPEG 2000 that names no color space"),
        (not bitonal and item.bits != _BITS, f"{item.bits} bits per component"),
        (
            stored and (item.colorspace not in _SAMPLE_SPACES or item.components not in (1, 3, _CMYK)),
            f"a {family} color space",
        ),
        (stored and image_filter == "JPXDecode", f"JPEG 2000 in {family}"),
        (stored and image_filter == "DCTDecode" and item.components == _CMYK, "a CMYK JPEG"),
        (stored and "Decode" in stream, f"a /Decode array in {family}"),
    )
    return next((reason for failed, reason in checks if failed), None)


def _target_size(
    pixels: tuple[int, int], lengths: tuple[float, float], threshold: float | None, target: float
) -> tuple[int, int]:
    """Return the pixels that an image keeps: ``target`` per inch of its largest placement, over ``threshold``.

    An axis never gains pixels, and an image whose resolution exceeds the
    threshold on either axis is resampled on both, so it keeps its aspect.
    """
    width, height = pixels
    axes = ((width, lengths[0]), (height, lengths[1]))
    if threshold is None or not any(
        length > 0 and pixels * _POINTS_PER_INCH / length > threshold for pixels, length in axes
    ):
        return width, height
    sizes = [
        pixels if length <= 0 else min(pixels, max(1, math.ceil(target * length / _POINTS_PER_INCH - 1e-9)))
        for pixels, length in axes
    ]
    return sizes[0], sizes[1]


def _candidates(
    document: Document, options: _Options, tally: _Tally, inline: list[int]
) -> dict[int, _Candidate | None]:
    """Return each image object that the pages place, with its largest placement, or ``None`` when not selected.

    One record per object, so memory grows with the objects, not the
    placements; ``inline`` gains each page's count of inline images.
    """
    candidates: dict[int, _Candidate | None] = {}
    for page in document.pages:
        inline.append(0)
        for image in page.images(annotations=True):
            item = image.item
            if item.xref == 0:
                inline[-1] += 1
                continue
            if item.xref not in candidates:
                stream = document.objects.get(item.xref)
                kind = _classify(stream, item, options.selected) if isinstance(stream, Stream) else None
                if isinstance(kind, str):
                    tally.report(f"object {item.xref}: {kind}")
                candidates[item.xref] = None if kind is None or isinstance(kind, str) else _Candidate(image, *kind)
            candidate = candidates[item.xref]
            if candidate is not None:
                a, b, c, d = item.matrix[:4]
                candidate.width = max(candidate.width, math.hypot(a, b))
                candidate.height = max(candidate.height, math.hypot(c, d))
    return candidates


def _pixmap(width: int, height: int, depth: int, data: bytes) -> Pixmap:
    """Return 8-bit samples of ``depth`` channels as a pixmap, through a PAM header."""
    header = f"P7\nWIDTH {width}\nHEIGHT {height}\nDEPTH {depth}\nMAXVAL 255\nTUPLTYPE {_PAM_TYPES[depth]}\nENDHDR\n"
    return _core.Pixmap.from_bytes(header.encode() + data)


def _planes(data: bytes, channels: int) -> list[bytes]:
    return [data[channel::channels] for channel in range(channels)]


def _interleave(planes: list[bytes]) -> bytes:
    if len(planes) == 1:
        return planes[0]
    out = bytearray(len(planes[0]) * len(planes))
    for channel, plane in enumerate(planes):
        out[channel :: len(planes)] = plane
    return bytes(out)


def _from_pixmap(pixmap: Pixmap) -> _Pixels:
    """Split a pixmap into its color samples and its alpha, ``None`` when every pixel is opaque."""
    data = bytes(pixmap)
    components = 1 if pixmap.colorspace == "gray" else 3
    if not pixmap.alpha:
        return _Pixels(pixmap.width, pixmap.height, components, data, None)
    planes = _planes(data, components + 1)
    alpha = planes.pop()
    return _Pixels(
        pixmap.width, pixmap.height, components, _interleave(planes), None if alpha.count(0xFF) == len(alpha) else alpha
    )


def _resized(pixels: _Pixels, size: tuple[int, int]) -> _Pixels:
    """Resample ``pixels`` to ``size``: color and alpha together, through premultiplied alpha, below four components."""
    if size == (pixels.width, pixels.height):
        return pixels
    width, height = pixels.width, pixels.height
    if pixels.components < _CMYK:
        depth = pixels.components + (pixels.alpha is not None)
        data = (
            pixels.color
            if pixels.alpha is None
            else _interleave([*_planes(pixels.color, pixels.components), pixels.alpha])
        )
        scaled = _from_pixmap(_pixmap(width, height, depth, data).scale(*size))
        if pixels.alpha is not None and scaled.alpha is None:
            scaled.alpha = b"\xff" * (size[0] * size[1])
        return scaled
    planes = [
        bytes(_pixmap(width, height, 1, plane).scale(*size)) for plane in _planes(pixels.color, pixels.components)
    ]
    alpha = None if pixels.alpha is None else bytes(_pixmap(width, height, 1, pixels.alpha).scale(*size))
    return _Pixels(size[0], size[1], pixels.components, _interleave(planes), alpha)


def _bitonal(pixels: _Pixels) -> bytes:
    """Return gray samples as 1-bit rows, padded to whole bytes, 1 for white: below 128 is black."""
    pbm = _pixmap(pixels.width, pixels.height, 1, pixels.color).to_bytes("pbm", max_size=None)
    return pbm.split(b"\n", 2)[2].translate(_INVERT)


def _encode(pixels: _Pixels, form: str, quality: int, limit: int) -> bytes | None:
    """Return the color samples encoded as ``form``: ``"jpeg"``, ``"flate"``, ``"fax"``, or ``"flate1"`` for 1 bit."""
    if form == "fax":
        return _core.encode_ccitt_g4(_bitonal(pixels), pixels.width, pixels.height, limit)
    if form == "flate1":
        return zlib.compress(_bitonal(pixels), 9)
    if form == "jpeg":
        return _pixmap(pixels.width, pixels.height, pixels.components, pixels.color).to_bytes("jpeg", quality=quality)
    return zlib.compress(pixels.color, 9)


def _form(kind: str, components: int, options: _Options) -> str:
    if kind == "bitonal":
        return "fax" if options.bitonal_format == "fax" else "flate1"
    return "jpeg" if options.jpeg(kind) and components < _CMYK else "flate"


def _source(candidate: _Candidate, stream: Stream) -> _Pixels:
    """Return an image's pixels: hayro's colors, or its stored samples with hayro's alpha."""
    item = candidate.image.item
    pixmap = candidate.image.pixmap()
    if candidate.gray and pixmap.colorspace != "gray":
        pixmap = pixmap.to_gray()
    painted = _from_pixmap(pixmap)
    if not candidate.samples:
        # hayro paints a /Matte image's colors un-premultiplied.
        return painted
    if (painted.width, painted.height) != (item.width, item.height):
        msg = f"image object {item.xref} decodes to another size"
        raise PdfError(msg)
    stored = candidate.image.stored()
    if stored is not None and stored.filter == "DCTDecode":
        decoded = _core.Pixmap.from_bytes(stored.data)
        color = bytes(decoded)
    else:
        color = stream.read()[: item.width * item.height * item.components]
    if len(color) != item.width * item.height * item.components:
        msg = f"image object {item.xref} holds fewer samples than its size needs"
        raise PdfError(msg)
    mask = stream.get("SMask")
    if isinstance(mask, Stream) and isinstance(mask.get("Matte"), Array) and painted.alpha is not None:
        color = _unmatte(color, painted.alpha, mask["Matte"], item.components)
    return _Pixels(item.width, item.height, item.components, color, painted.alpha)


def _unmatte(color: bytes, alpha: bytes, matte: Array, components: int) -> bytes:
    """Return samples premultiplied with a /Matte color as straight ones: c = m + (c' - m) / a (ISO 32000-2 11.6.5.3).

    A pixel whose alpha is 0 keeps its sample, which nothing shows.
    """
    mattes = [
        round(255 * min(1.0, max(0.0, float(value)))) if isinstance(value, (int, float)) else 0 for value in matte
    ]
    out = bytearray(color)
    for channel, value in enumerate(mattes):
        # One table per alpha, built as alphas appear: at most 256 of 256 entries.
        tables: dict[int, bytes] = {}
        for at in range(channel, len(out), components):
            opacity = alpha[at // components]
            if opacity in (0, 255):
                continue
            table = tables.get(opacity)
            if table is None:
                table = bytes(
                    min(255, max(0, round(value + (sample - value) * 255 / opacity))) for sample in range(256)
                )
                tables[opacity] = table
            out[at] = table[out[at]]
    return bytes(out)


def _rewrite(
    document: Document, xref: int, candidate: _Candidate, size: tuple[int, int], options: _Options
) -> int | None:
    """Re-encode one image at ``size`` and store it when it is smaller enough; return its new stored length.

    A mask of another size than the image is resampled at its own
    resolution and stays a stencil ``/Mask`` or a soft mask; one that keeps
    its pixels stays as stored.
    """
    item = candidate.image.item
    stream = document.objects[xref]
    lengths = (candidate.width, candidate.height)
    masks = [
        (key, mask, _mask_size(mask, lengths, options))
        for key in ("SMask", "Mask")
        if isinstance(mask := stream.get(key), Stream) and _other_size(mask, item)
    ]
    kept = {key for key, mask, mask_size in masks if mask_size == (mask["Width"], mask["Height"])}
    before = item.encoded_size + sum(
        len(mask.read(decode=False))
        for key in ("SMask", "Mask")
        if isinstance(mask := stream.get(key), Stream) and key not in kept
    )
    limit = int(before * (1 - _MIN_SAVING))
    pixels = _resized(_source(candidate, stream), size)
    form = _form(candidate.kind, pixels.components, options)
    quality = _DEFAULT_QUALITY if options.quality is None else options.quality
    data = _encode(pixels, form, quality, limit)
    alpha = None if pixels.alpha is None else zlib.compress(pixels.alpha, 9)
    resampled = [
        (key, mask_size, _encode_mask(mask, mask_size, options, limit, stencil=key == "Mask"))
        for key, mask, mask_size in masks
        if key not in kept
    ]
    after = None if data is None else len(data) + len(alpha or b"")
    for _, _, encoded in resampled:
        after = None if after is None or encoded is None else after + len(encoded[0])
    if after is None or after > limit:
        return None
    # Resampled stored samples keep the color space, which stays in place.
    for key in _DATA_KEYS:
        if key in stream and key not in kept and not (candidate.samples and key == "ColorSpace"):
            del stream[key]
    stream.write(data, filter=None)
    entries: dict[str, object] = {
        "Width": size[0],
        "Height": size[1],
        "BitsPerComponent": 1 if candidate.kind == "bitonal" else 8,
        "Filter": Name({"jpeg": "DCTDecode", "fax": "CCITTFaxDecode"}.get(form, "FlateDecode")),
    }
    if not candidate.samples:
        entries["ColorSpace"] = Name("DeviceGray" if pixels.components == 1 else "DeviceRGB")
    if form == "fax":
        entries["DecodeParms"] = {"K": -1, "Columns": size[0], "Rows": size[1]}
    stream.update(entries)
    if alpha is not None:
        stream["SMask"] = _add_mask(document, size, (alpha, "FlateDecode", None), stencil=False)
    for key, mask_size, encoded in resampled:
        if encoded is not None:
            stream[key] = _add_mask(document, mask_size, encoded, stencil=key == "Mask")
    return after


def _mask_size(mask: Stream, lengths: tuple[float, float], options: _Options) -> tuple[int, int]:
    """Return the pixels that a mask of another size keeps, at the image's largest placement."""
    return _target_size((mask["Width"], mask["Height"]), lengths, options.threshold, options.target)


def _encode_mask(
    mask: Stream, size: tuple[int, int], options: _Options, limit: int, *, stencil: bool
) -> tuple[bytes, str, dict[str, object] | None] | None:
    """Return a mask resampled to ``size``: its data, filter, and parameters, or ``None`` when larger than ``limit``."""
    pixels = _resized(_mask_pixels(mask, stencil=stencil), size)
    if not stencil:
        return zlib.compress(pixels.color, 9), "FlateDecode", None
    form = "fax" if options.bitonal_format == "fax" else "flate1"
    data = _encode(pixels, form, 0, limit)
    if data is None:
        return None
    if form == "fax":
        return data, "CCITTFaxDecode", {"K": -1, "Columns": size[0], "Rows": size[1]}
    return data, "FlateDecode", None


def _add_mask(
    document: Document,
    size: tuple[int, int],
    encoded: tuple[bytes, str, dict[str, object] | None],
    *,
    stencil: bool,
) -> Stream:
    """Add a mask object: a 1-bit stencil, or an 8-bit DeviceGray soft mask."""
    data, filter_name, parameters = encoded
    mask: dict[str, object] = {"Type": Name("XObject"), "Subtype": Name("Image"), "Width": size[0], "Height": size[1]}
    if stencil:
        mask.update({"ImageMask": True, "BitsPerComponent": 1})
    else:
        mask.update({"BitsPerComponent": 8, "ColorSpace": Name("DeviceGray")})
    number = document.objects.add_stream(b"", mask, filter=None)
    added = document.objects[number]
    added.write(data, filter=None)
    added["Filter"] = Name(filter_name)
    if parameters is not None:
        added["DecodeParms"] = parameters
    return added


def _mask_pixels(mask: Stream, *, stencil: bool) -> _Pixels:
    """Return a mask's pixels at its own size, black where a stencil paints, as hayro decodes it alone.

    hayro decodes a mask of another size than its image only to paint it,
    so a scratch document paints the mask, its data copied as stored.
    """
    scratch = _new_document()
    entries = {str(key): _transfer(scratch, value, 0) for key, value in mask.items() if key not in _STREAM_KEYS}
    entries.setdefault("Subtype", Name("Image"))
    number = scratch.objects.add_stream(b"", entries, filter=None)
    copy = scratch.objects[number]
    copy.write(mask.read(decode=False), filter=None)
    for key in ("Filter", "DecodeParms"):
        if key in mask:
            copy[key] = _transfer(scratch, mask[key], 0)
    scratch.pages.new((1, 1))
    page = scratch.pages[0]
    page.obj["Resources"] = {"XObject": {"M": copy}}
    page.operations = Operations([Operation("Do", (Name("M"),))])
    (image,) = page.images()
    painted = _from_pixmap(image.pixmap())
    if stencil:
        size = painted.width * painted.height
        color = b"\x00" * size if painted.alpha is None else painted.alpha.translate(_INVERT)
        return _Pixels(painted.width, painted.height, 1, color, None)
    if painted.components != 1:
        painted = _from_pixmap(_pixmap(painted.width, painted.height, 3, painted.color).to_gray())
    return _Pixels(painted.width, painted.height, 1, painted.color, None)


def _new_document() -> Document:
    from pyropdf._document import Document  # noqa: PLC0415 - the document module imports this one.

    return Document()


def _transfer(scratch: Document, value: object, depth: int) -> object:
    """Return ``value`` for the scratch document: its arrays, dictionaries, and streams copied as stored."""
    if depth > _MAX_TRANSFER_DEPTH:
        msg = f"a mask's entries nest deeper than {_MAX_TRANSFER_DEPTH}"
        raise PdfError(msg)
    if isinstance(value, Stream):
        entries = {
            str(key): _transfer(scratch, entry, depth + 1) for key, entry in value.items() if key not in _STREAM_KEYS
        }
        number = scratch.objects.add_stream(b"", entries, filter=None)
        copy = scratch.objects[number]
        copy.write(value.read(decode=False), filter=None)
        for key in ("Filter", "DecodeParms"):
            if key in value:
                copy[key] = _transfer(scratch, value[key], depth + 1)
        return copy
    if isinstance(value, Dictionary):
        return {str(key): _transfer(scratch, entry, depth + 1) for key, entry in value.items()}
    if isinstance(value, Array):
        return [_transfer(scratch, entry, depth + 1) for entry in value]
    return value


def _inline_pixels(image: InlineImage, selected: dict[str, bool]) -> tuple[str, _Pixels] | str | None:
    """Return an inline image's kind and pixels, why it is not rewritten, or ``None`` when its kind is not selected."""
    entries = image.dictionary
    width, height = entries.get("W", entries.get("Width")), entries.get("H", entries.get("Height"))
    bits = entries.get("BPC", entries.get("BitsPerComponent"))
    space = entries.get("CS", entries.get("ColorSpace"))
    names = _filters(entries.get("F", entries.get("Filter")))
    stencil = entries.get("IM", entries.get("ImageMask")) is True
    if stencil and bits is None:
        bits = 1
    bitonal = stencil or bits == 1
    lossy = bool(names) and names[-1] in _INLINE_DCT
    kind = "bitonal" if bitonal else "lossy" if lossy else "lossless"
    components = _INLINE_SPACES.get(str(space)) if isinstance(space, Name) else None
    gray = bitonal or components == 1
    if not selected[kind] or not (bitonal or selected["gray" if gray else "color"]):
        return None
    checks = (
        (
            not stencil and (components is None or (bitonal and components != 1)),
            f"an inline image in {_family(space) or 'no color space'}",
        ),
        (
            bits not in (1, _BITS) or any(key in entries for key in ("D", "Decode", "DP", "DecodeParms")),
            "an inline image with other bits, /Decode, or /DecodeParms",
        ),
        (
            not (isinstance(width, int) and isinstance(height, int) and 0 < width * height <= _MAX_INLINE_PIXELS),
            "an inline image of another size",
        ),
        (lossy and len(names) != 1, "an inline JPEG under other filters"),
    )
    reason = next((reason for failed, reason in checks if failed), None)
    if reason is not None:
        return reason
    if stencil:
        components = 1
    if not isinstance(width, int) or not isinstance(height, int) or components is None:
        return "an inline image of another size"
    pixels = _inline_decode(image, (width, height, components), lossy=lossy, bitonal=bitonal)
    return (kind, pixels) if isinstance(pixels, _Pixels) else pixels


def _inline_decode(image: InlineImage, shape: tuple[int, int, int], *, lossy: bool, bitonal: bool) -> _Pixels | str:
    """Return an inline image's pixels, or why they are not read."""
    width, height, components = shape
    if lossy:
        pixels = _from_pixmap(_core.Pixmap.from_bytes(image.data))
        if (pixels.width, pixels.height, pixels.components) != shape:
            return "an inline JPEG that does not match its dictionary"
        return pixels
    entries = image.dictionary
    data = _inline_data(entries.get("F", entries.get("Filter")), image.data)
    stride = (width + 7) // 8 if bitonal else width * components
    if data is None or len(data) < stride * height:
        return "an inline image whose filters a rewrite does not decode"
    data = data[: stride * height]
    if bitonal:
        pbm = f"P4\n{width} {height}\n".encode() + data.translate(_INVERT)
        return _from_pixmap(_core.Pixmap.from_bytes(pbm))
    return _Pixels(width, height, components, data, None)


def _rewrite_inline(image: InlineImage, lengths: tuple[float, float], options: _Options, tally: _Tally) -> InlineImage:
    """Return an inline image rewritten as its largest placement and the options say, or ``image`` when it stays."""
    found = _inline_pixels(image, options.selected)
    if found is None:
        return image
    tally.considered += 1
    if isinstance(found, str):
        tally.report(found)
        return image
    kind, pixels = found
    entries = image.dictionary
    stencil = entries.get("IM", entries.get("ImageMask")) is True
    recolor = options.set_to_gray and pixels.components != 1
    if recolor:
        pixels = _from_pixmap(_pixmap(pixels.width, pixels.height, 3, pixels.color).to_gray())
    size = _target_size((pixels.width, pixels.height), lengths, options.threshold, options.target)
    if size == (pixels.width, pixels.height) and not recolor and options.quality is None:
        return image
    pixels = _resized(pixels, size)
    form = _form(kind, pixels.components, options)
    limit = int(len(image.data) * (1 - _MIN_SAVING))
    quality = _DEFAULT_QUALITY if options.quality is None else options.quality
    data = _encode(pixels, form, quality, limit)
    if data is None or len(data) > limit:
        return image
    dictionary: dict[str, object] = {"W": size[0], "H": size[1], "BPC": 1 if kind == "bitonal" else 8}
    if stencil:
        dictionary["IM"] = True
    else:
        dictionary["CS"] = Name("G" if pixels.components == 1 else "RGB")
    dictionary["F"] = Name({"jpeg": "DCT", "fax": "CCF"}.get(form, "Fl"))
    if form == "fax":
        dictionary["DP"] = {"K": -1, "Columns": size[0], "Rows": size[1]}
    dictionary.update({key: value for key, value in entries.items() if key in ("I", "Interpolate", "Intent")})
    tally.kept(len(image.data), len(data))
    return InlineImage(dictionary, data)


def _lengths(matrix: tuple[float, ...]) -> tuple[float, float]:
    """Return the lengths, in points, that ``matrix`` gives the unit square's sides."""
    return math.hypot(matrix[0], matrix[1]), math.hypot(matrix[2], matrix[3])


def _rewrite_page_inline(page: Page, options: _Options, tally: _Tally, nested: _Nested) -> int:
    """Rewrite the inline images of a page's own content, which the page then writes as one new stream.

    ``nested`` reads the Form XObjects and appearances that the page draws.
    Return how many inline images the page paints that a rewrite reaches.
    """
    operations = page.operations
    out: list[Operation | InlineImage] = []
    drawn: dict[str, int] = {}
    found = 0

    def visit(image: InlineImage, matrix: tuple[float, ...]) -> None:
        nonlocal found
        found += 1
        rewritten = _rewrite_inline(image, _lengths(matrix), options, tally)
        out[-1] = rewritten

    reached = nested.walk(operations, (_resources(page), drawn), _IDENTITY, 0, (visit, out.append))
    if any(a is not b for a, b in zip(out, operations, strict=True)):
        page.operations = Operations(out, tail=operations.tail)
    if drawn:
        nested.pages.append((page, drawn))
    return found + reached + nested.annotations(page)


_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _concat(first: tuple[float, ...], second: tuple[float, ...]) -> tuple[float, ...]:
    """Return ``first`` then ``second``, as ``cm`` prepends to the current matrix."""
    a, b, c, d, e, f = first
    p, q, r, s, t, u = second
    return (a * p + b * r, a * q + b * s, c * p + d * r, c * q + d * s, e * p + f * r + t, e * q + f * s + u)


def _matrix(value: object) -> tuple[float, ...] | None:
    """Return six numbers as a matrix, or ``None`` for another value."""
    if not isinstance(value, (Array, tuple, list)) or len(value) != 6:  # noqa: PLR2004 - a matrix.
        return None
    if not all(isinstance(number, (int, float)) and not isinstance(number, bool) for number in value):
        return None
    return tuple(float(number) for number in value)


@dataclass(slots=True)
class _NestedForm:
    """A Form XObject or appearance that holds inline images, or draws one that does."""

    stream: Stream
    operations: Operations
    resources: Dictionary | None
    """Its own resources, through which its drawings are named, or ``None`` when it uses its drawer's."""
    lengths: list[tuple[float, float]]
    """The longest lengths that the drawings give each of its inline images, in paint order."""
    drawn: dict[str, int]
    """The forms that its own resources name and that hold inline images, by name."""


class _Nested:
    """The inline images of the Form XObjects and annotation appearances that the pages draw.

    Every drawing is followed, to find each inline image's largest
    placement, and each form is rewritten once. A form that changes, or
    that draws one that changes, becomes a new object that the pages and
    appearances draw, so whatever else draws the original keeps it.
    """

    def __init__(self, tally: _Tally) -> None:
        self.tally = tally
        self.forms: dict[int, _NestedForm] = {}
        #: The forms in an order that puts each after the forms it draws.
        self.order: list[int] = []
        self.parsed: dict[int, Operations | None] = {}
        self.active: set[int] = set()
        self.drawings = 0
        self.pages: list[tuple[Page, dict[str, int]]] = []
        self.appearances: list[tuple[Dictionary, str, str | None, int]] = []
        self.copies: dict[int, Stream] = {}
        #: The stencil mask images drawn, by object, with their longest lengths.
        self.stencils: dict[int, tuple[Stream, float, float]] = {}

    def stencil(self, stream: Stream, matrix: tuple[float, ...]) -> None:
        width, height = _lengths(matrix)
        _, longest_width, longest_height = self.stencils.get(stream.xref or 0, (stream, 0.0, 0.0))
        self.stencils[stream.xref or 0] = (stream, max(width, longest_width), max(height, longest_height))

    def walk(
        self,
        operations: Operations,
        scope: tuple[Dictionary | None, dict[str, int]],
        matrix: tuple[float, ...],
        depth: int,
        sinks: tuple[Callable[[InlineImage, tuple[float, ...]], None], Callable[[Operation | InlineImage], None]],
    ) -> int:
        """Follow one drawing: report each inline image to ``sinks`` and each form it draws to ``scope``.

        ``scope`` holds the resources that name its drawings and the forms
        drawn through them that hold inline images. ``sinks`` take each
        inline image with its matrix, and each operation. Return how many
        inline images the forms that it draws hold, counted per drawing.
        """
        resources, drawn = scope
        visit, emit = sinks
        xobjects = _sub(resources, "XObject")
        saved: list[tuple[float, ...]] = []
        reached = 0
        for operation in operations:
            emit(operation)
            if isinstance(operation, InlineImage):
                visit(operation, matrix)
                continue
            operator = operation.operator
            if operator == "q":
                saved.append(matrix)
            elif operator == "Q" and saved:
                matrix = saved.pop()
            elif operator == "cm":
                values = _matrix(operation.operands)
                if values is not None:
                    matrix = _concat(values, matrix)
            elif operator == "Do" and xobjects is not None and operation.operands:
                reached += self.draw(operation.operands[0], xobjects, (resources, drawn), matrix, depth)
        return reached

    def draw(
        self,
        name: object,
        xobjects: Dictionary,
        scope: tuple[Dictionary | None, dict[str, int]],
        matrix: tuple[float, ...],
        depth: int,
    ) -> int:
        """Follow a ``Do``; return how many images that hayro lists as inline it paints."""
        xobject = xobjects.get(str(name)) if isinstance(name, Name) else None
        if not isinstance(xobject, Stream):
            return 0
        if xobject.get("Subtype") == "Image" and xobject.get("ImageMask") is True:
            # hayro lists a stencil mask image without its object, as an inline image.
            self.stencil(xobject, matrix)
            return 1
        if xobject.get("Subtype") != "Form":
            return 0
        placed = _concat(_matrix(xobject.get("Matrix")) or _IDENTITY, matrix)
        number, count = self.form(xobject, scope, placed, depth + 1)
        if number is not None:
            scope[1][str(name)] = number
        return count

    def form(
        self, form: Stream, scope: tuple[Dictionary | None, dict[str, int]], matrix: tuple[float, ...], depth: int
    ) -> tuple[int | None, int]:
        """Follow one drawing of ``form``; return its object when it holds inline images, and how many it paints."""
        number = form.xref or 0
        if depth > _MAX_FORM_DEPTH:
            self.tally.report(f"inline images in Form XObjects nested deeper than {_MAX_FORM_DEPTH}")
            return None, 0
        if number in self.active:
            return None, 0
        if self.drawings >= _MAX_FORMS:
            self.tally.report(f"inline images past {_MAX_FORMS} drawings of Form XObjects")
            return None, 0
        self.drawings += 1
        operations = self.parse(form, number)
        if operations is None:
            return None, 0
        own = form.get("Resources")
        own = own if isinstance(own, Dictionary) else None
        record = self.forms.get(number) or _NestedForm(form, operations, own, [], {})
        index = 0

        def visit(_: InlineImage, placed: tuple[float, ...]) -> None:
            nonlocal index
            lengths = _lengths(placed)
            if index == len(record.lengths):
                record.lengths.append(lengths)
            else:
                width, height = record.lengths[index]
                record.lengths[index] = (max(width, lengths[0]), max(height, lengths[1]))
            index += 1

        # A form without resources of its own draws through its drawer's.
        inner = (own, record.drawn) if own is not None else scope
        self.active.add(number)
        try:
            reached = self.walk(operations, inner, matrix, depth, (visit, _ignore))
        finally:
            self.active.discard(number)
        if not (record.lengths or record.drawn):
            return None, reached
        if number not in self.forms:
            self.forms[number] = record
            self.order.append(number)
        return number, reached + index

    def parse(self, form: Stream, number: int) -> Operations | None:
        if number not in self.parsed:
            try:
                self.parsed[number] = Operations.parse(form.read())
            except (PdfError, ValueError):
                self.tally.report(f"object {number}: a Form XObject that cannot be read")
                self.parsed[number] = None
        return self.parsed[number]

    def annotations(self, page: Page) -> int:
        """Follow the normal appearances of a page's annotations; return how many inline images they paint."""
        annotations = page.obj.get("Annots")
        reached = 0
        for annotation in annotations if isinstance(annotations, Array) else ():
            appearances = annotation.get("AP") if isinstance(annotation, Dictionary) else None
            entry = appearances.get("N") if isinstance(appearances, Dictionary) else None
            states = [(None, entry)] if isinstance(entry, Stream) else []
            if isinstance(entry, Dictionary) and not isinstance(entry, Stream):
                states = [(str(state), value) for state, value in entry.items() if isinstance(value, Stream)]
            for state, stream in states:
                matrix = _appearance_matrix(annotation, stream)
                if matrix is None:
                    continue
                number, count = self.form(stream, (None, {}), matrix, 1)
                reached += count
                if number is not None:
                    self.appearances.append((annotation, "N", state, number))
        return reached

    def write(self, document: Document, options: _Options) -> None:
        """Rewrite each stencil mask image, and each form's inline images once; draw the forms that change anew."""
        if options.selected["bitonal"]:
            for stream, width, height in self.stencils.values():
                _rewrite_stencil(stream, (width, height), options, self.tally)
        for number in self.order:
            self.copy(document, number, options)
        for page, drawn in self.pages:
            repointed = {name: self.copies[number] for name, number in drawn.items() if number in self.copies}
            if repointed:
                _repoint_page(page, repointed)
        for annotation, key, state, number in self.appearances:
            if number in self.copies:
                _set_appearance(annotation, key, state, self.copies[number])

    def copy(self, document: Document, number: int, options: _Options) -> None:
        """Add a copy of a form whose inline images, or whose drawn forms, change."""
        record = self.forms[number]
        lengths = iter(record.lengths)
        out: list[Operation | InlineImage] = []
        changed = False
        for operation in record.operations:
            if isinstance(operation, InlineImage):
                image = _rewrite_inline(operation, next(lengths), options, self.tally)
                changed = changed or image is not operation
                operation = image  # noqa: PLW2901 - the rewritten image.
            out.append(operation)
        children = {name: self.copies[child] for name, child in record.drawn.items() if child in self.copies}
        if not (changed or children):
            return
        dictionary = {key: value for key, value in record.stream.items() if key not in _STREAM_KEYS}
        if children and record.resources is not None:
            own = dict(record.resources.items())
            xobjects = own.get("XObject")
            own["XObject"] = {**(dict(xobjects.items()) if isinstance(xobjects, Dictionary) else {}), **children}
            dictionary["Resources"] = own
        data = Operations(out, tail=record.operations.tail).to_bytes()
        self.copies[number] = document.objects[document.objects.add_stream(data, dictionary)]


def _rewrite_stencil(stream: Stream, lengths: tuple[float, float], options: _Options, tally: _Tally) -> None:
    """Resample a stencil mask image in place as its largest placement and the options say."""
    tally.considered += 1
    width, height = stream.get("Width"), stream.get("Height")
    if not (isinstance(width, int) and isinstance(height, int) and 0 < width * height <= _MAX_SOURCE_PIXELS):
        tally.report(f"object {stream.xref}: a stencil mask of another size that a rewrite does not decode")
        return
    size = _target_size((width, height), lengths, options.threshold, options.target)
    if size == (width, height) and options.quality is None:
        return
    before = len(stream.read(decode=False))
    limit = int(before * (1 - _MIN_SAVING))
    encoded = _encode_mask(stream, size, options, limit, stencil=True)
    if encoded is None or len(encoded[0]) > limit:
        return
    data, filter_name, parameters = encoded
    for key in _DATA_KEYS:
        if key in stream:
            del stream[key]
    stream.write(data, filter=None)
    stream.update(
        {"Width": size[0], "Height": size[1], "ImageMask": True, "BitsPerComponent": 1, "Filter": Name(filter_name)}
    )
    if parameters is not None:
        stream["DecodeParms"] = parameters
    tally.kept(before, len(data))


def _ignore(_: object) -> None:
    pass


def _appearance_matrix(annotation: Dictionary, appearance: Stream) -> tuple[float, ...] | None:
    """Return the matrix that places an appearance's form in its annotation's rectangle (ISO 32000-2 12.5.5)."""
    rect, box = _matrix([*annotation.get("Rect", ()), 0, 0]), _matrix([*appearance.get("BBox", ()), 0, 0])
    if rect is None or box is None:
        return None
    matrix = _matrix(appearance.get("Matrix")) or _IDENTITY
    a, b, c, d, e, f = matrix
    corners = [(x * a + y * c + e, x * b + y * d + f) for x in (box[0], box[2]) for y in (box[1], box[3])]
    xs, ys = [x for x, _ in corners], [y for _, y in corners]
    left, right = min(rect[0], rect[2]), max(rect[0], rect[2])
    bottom, top = min(rect[1], rect[3]), max(rect[1], rect[3])
    if max(xs) == min(xs) or max(ys) == min(ys):
        return None
    sx, sy = (right - left) / (max(xs) - min(xs)), (top - bottom) / (max(ys) - min(ys))
    return _concat(matrix, (sx, 0.0, 0.0, sy, left - min(xs) * sx, bottom - min(ys) * sy))


def rewrite_images(  # noqa: PLR0913 - pymupdf's keyword arguments, and pyropdf's output choices
    document: Document,
    *,
    dpi_threshold: float | None,
    dpi_target: float | None,
    quality: int | None,
    selected: dict[str, bool],
    set_to_gray: bool,
    lossless_format: str,
    bitonal_format: str,
) -> ImageRewriteReport:
    """Downsample and recompress the images that the pages place, each object once, in one transaction."""
    threshold, target = _rewrite_options(dpi_threshold, dpi_target, quality, {**selected, "set_to_gray": set_to_gray})
    _check_formats(lossless_format, bitonal_format)
    if threshold is None and quality is None and not set_to_gray:
        return ImageRewriteReport(0, 0, 0, 0)
    options = _Options(threshold, target, quality, selected, set_to_gray, lossless_format, bitonal_format)
    tally = _Tally()
    inline: list[int] = []
    candidates = _candidates(document, options, tally, inline)
    tally.considered = len(candidates)
    with document.transaction("rewrite images"):
        for xref, candidate in candidates.items():
            if candidate is None:
                continue
            item = candidate.image.item
            size = _target_size((item.width, item.height), (candidate.width, candidate.height), threshold, target)
            recolor = set_to_gray and not candidate.gray
            if size == (item.width, item.height) and not recolor and quality is None:
                continue
            if recolor:
                candidate.gray, candidate.samples = True, False
            tally.kept(item.encoded_size, _rewrite(document, xref, candidate, size, options))
        nested = _Nested(tally)
        for index, (page, count) in enumerate(zip(document.pages, inline, strict=False)):
            missed = count - (_rewrite_page_inline(page, options, tally, nested) if count else 0)
            if missed > 0:
                tally.report(f"page {index}: {missed} inline images that a rewrite does not reach, as in patterns")
        nested.write(document, options)
    return ImageRewriteReport(
        tally.considered, tally.rewritten, tally.before, tally.after, tuple(tally.unsupported or ())
    )


# The package re-exports these names; reprs, pickles, and documentation name them.
for _public in (ImageRewriteReport, PageImage, StoredImage):
    _public.__module__ = "pyropdf"
