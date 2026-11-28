"""Images: the pixels and stored data of the images that a page paints, and replacing an image object.

``page.images()`` lists :class:`PageImage` records in the paint order of
:meth:`Page.content`. hayro decodes an image's pixels as it does to render
the page, and the data of an image filter (ISO 32000-2 table 6) passes
through as stored. ``doc.replace_image`` and ``doc.remove_image`` rewrite an
image object in place, so every placement of it changes in one transaction.
"""

from __future__ import annotations

import io
import math
import os
import re
import stat
import struct
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pyropdf import _core
from pyropdf._errors import LimitError, PdfError
from pyropdf._layers import _visible_with_state
from pyropdf._metadata import _require_version
from pyropdf._objects import Array, Dictionary, Name, Stream
from pyropdf._operations import InlineImage, Operation, Operations
from pyropdf._xobjects import (
    _MAX_FORM_DEPTH,
    _MAX_FORMS,
    _MAX_INLINE_PIXELS,
    _STREAM_KEYS,
    _appearance_streams,
    _copy_form,
    _inline_data,
    _parse_form,
    _repoint_page,
    _resources,
    _set_appearance,
    _sub,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator
    from typing import BinaryIO

    from pyropdf._content import Image
    from pyropdf._document import Document
    from pyropdf._limits import DocumentLimits
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
_MAX_IMAGE_ALTERNATES = 4096
_DEVICE_SPACES = {1: "DeviceGray", 3: "DeviceRGB", 4: "DeviceCMYK"}
_CMYK = 4
_MARKER = 0xFF
#: The APP14 marker of Adobe's JPEGs.
_APP14 = 0xEE
#: The most bytes of encoded image data accepted by ``replace_image``, ``add_image``, and ``Canvas.image``.
_MAX_IMAGE_INPUT = 268_435_456
#: Chunk size for bounded streaming reads from files and ZIP members.
_ZIP_READ_CHUNK = 65_536
#: Most ZIP central-directory entries inspected in one CBZ archive.
_MAX_ARCHIVE_ENTRIES = 16_384
#: Maximum decompression ratio for compressed ZIP entries once output exceeds ``_ZIP_RATIO_FLOOR``.
_MAX_ZIP_EXPANSION_RATIO = 100
_ZIP_RATIO_FLOOR = 65_536
#: File extensions recognized as image pages inside a CBZ archive.
_CBZ_IMAGE_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".jpe",
        ".jfif",
        ".png",
        ".jp2",
        ".j2k",
        ".jpx",
        ".j2c",
        ".tif",
        ".tiff",
        ".gif",
        ".webp",
        ".bmp",
        ".ppm",
        ".pgm",
        ".pbm",
        ".pnm",
    }
)
_IGNORED_ARCHIVE_FILES = frozenset({".ds_store", "thumbs.db", "desktop.ini"})
_NATURAL_SPLIT = re.compile(r"([0-9]+)")
_PATH_SEPARATORS = re.compile(r"[/\\]")
_MIN_PNG_IHDR_BYTES = 24
_MAX_JP2_BOXES = 1024
_JP2_EXIF_UUID = b"JpgTiffExif->JP2"
_ZIP_EOCD_SIG = b"PK\x05\x06"
_ZIP64_LOCATOR_SIG = b"PK\x06\x07"
_ZIP64_EOCD_SIG = b"PK\x06\x06"
_ZIP_EOCD_MIN_SIZE = 22
_ZIP_MAX_TAIL = _ZIP_EOCD_MIN_SIZE + 65_535
_ZIP64_LOCATOR_SIZE = 20
_ZIP64_EOCD_MIN_SIZE = 56
_ZIP_LOCAL_HEADER_SIZE = 30
_ZIP_INFO_ESTIMATE = 512
_ALLOWED_ZIP_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})


@dataclass(frozen=True, slots=True, kw_only=True)
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


@dataclass(frozen=True, slots=True, kw_only=True)
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
        return StoredImage(
            filter=filter_name, data=data, parameters=parameters if isinstance(parameters, Dictionary) else None
        )


def page_images(page: Page, *, annotations: bool) -> list[PageImage]:
    """Return the images that ``page`` paints, in paint order."""
    content = page.content(text_runs=False, paths=False, images=True, annotations=annotations)
    return [
        PageImage(page=page, index=index, item=item, annotations=annotations)
        for index, item in enumerate(content.images)
    ]


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
            if width <= 0 or height <= 0:
                msg = "the JPEG frame header has non-positive dimensions"
                raise ValueError(msg)
            return width, height, data[at + 9], adobe
        at += 2 + length
    msg = "the JPEG data has no frame header"
    raise ValueError(msg)


def _iter_jp2_boxes(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, bytes]]:
    """Walk ISO/IEC 15444-1 JP2 boxes inside ``data[start:end]`` within ``_MAX_JP2_BOXES``."""
    at = start
    seen = 0
    while at + 8 <= end and seen < _MAX_JP2_BOXES:
        lbox, tbox = struct.unpack(">I4s", data[at : at + 8])
        if lbox == 0:
            header = 8
            box_end = end
        elif lbox == 1:
            if at + 16 > end:
                break
            (xlbox,) = struct.unpack(">Q", data[at + 8 : at + 16])
            if xlbox < 16 or at + xlbox > end:  # noqa: PLR2004 - extended box header is 16 bytes
                break
            header = 16
            box_end = at + xlbox
        else:
            if lbox < 8 or at + lbox > end:  # noqa: PLR2004 - minimum JP2 box header is 8 bytes
                break
            header = 8
            box_end = at + lbox
        yield tbox, data[at + header : box_end]
        at = box_end
        seen += 1


def _j2k_codestream_size(data: bytes) -> tuple[int, int]:
    """Return ``(width, height)`` from a JPEG 2000 codestream starting with SOC+SIZ."""
    if not data.startswith(_J2K_SIGNATURE) or len(data) < 24:  # noqa: PLR2004 - SOC+SIZ header
        msg = "the JPEG 2000 data has no image header"
        raise ValueError(msg)
    xsiz, ysiz, xosiz, yosiz = struct.unpack(">IIII", data[8:24])
    width, height = xsiz - xosiz, ysiz - yosiz
    if width > 0 and height > 0:
        return width, height
    msg = "the JPEG 2000 codestream header has non-positive dimensions"
    raise ValueError(msg)


def _jpx_size(data: bytes) -> tuple[int, int]:
    """Return the width and height of JPEG 2000 data, a JP2 file or a codestream."""
    if data.startswith(_JP2_SIGNATURE):
        codestream: bytes | None = None
        for tbox, payload in _iter_jp2_boxes(data, 0, len(data)):
            if tbox == b"jp2h":
                for sub_type, sub_payload in _iter_jp2_boxes(payload, 0, len(payload)):
                    if sub_type == b"ihdr" and len(sub_payload) >= 8:  # noqa: PLR2004
                        height, width = struct.unpack(">II", sub_payload[:8])
                        if width > 0 and height > 0:
                            return width, height
                        msg = "the JPEG 2000 image header has non-positive dimensions"
                        raise ValueError(msg)
            elif tbox == b"jp2c" and codestream is None:
                codestream = payload
        if codestream is not None:
            return _j2k_codestream_size(codestream)
        msg = "the JPEG 2000 data has no image header"
        raise ValueError(msg)
    return _j2k_codestream_size(data)


def _jp2_exif_bytes(data: bytes) -> bytes | None:
    """Return the Exif TIFF payload from a JP2 ``uuid`` box, or ``None`` if absent."""
    if not data.startswith(_JP2_SIGNATURE):
        return None
    for tbox, payload in _iter_jp2_boxes(data, 0, len(data)):
        if tbox == b"uuid" and len(payload) >= len(_JP2_EXIF_UUID) and payload[: len(_JP2_EXIF_UUID)] == _JP2_EXIF_UUID:
            body = payload[len(_JP2_EXIF_UUID) :]
            return body[6:] if body.startswith(b"Exif\x00\x00") else body
    return None


def _image_stream(document: Document, xref: int) -> Stream:
    if isinstance(xref, bool) or not isinstance(xref, int):
        msg = f"xref must be an int: {xref!r}"
        raise TypeError(msg)
    stream = document.objects.get(xref)
    if not isinstance(stream, Stream) or stream.get("Subtype") != "Image":
        msg = f"object {xref} is not an image XObject"
        raise ValueError(msg)
    return stream


def _alternates(base: Stream) -> Array | None:
    alternates = base.get("Alternates")
    if alternates is None:
        return None
    if not isinstance(alternates, Array):
        msg = "an image's /Alternates must be an array"
        raise PdfError(msg)
    if len(alternates) > _MAX_IMAGE_ALTERNATES:
        msg = f"an image has more than {_MAX_IMAGE_ALTERNATES} alternate entries"
        code = "content_element_count"
        raise LimitError(code, msg)
    return alternates


def _alternate_image(entry: Dictionary) -> Stream:
    image = entry.get("Image")
    if not isinstance(image, Stream) or image.get("Subtype") != "Image":
        msg = "an alternate image's /Image must reference an image XObject"
        raise PdfError(msg)
    return image


def image_for_output(document: Document, xref: int, *, printing: bool) -> Stream | None:
    base = _image_stream(document, xref)
    if isinstance(oc := base.get("OC"), Dictionary):
        return base if document.layers.visible(oc) else None
    alternates = _alternates(base)
    if alternates is None:
        return base
    # A later print default takes precedence over any earlier visible entry.
    if printing:
        for entry in alternates:
            if isinstance(entry, Dictionary) and entry.get("DefaultForPrinting") is True:
                return _alternate_image(entry)
    off = None
    for entry in alternates:
        if isinstance(entry, Dictionary) and isinstance(oc := entry.get("OC"), Dictionary):
            if off is None:
                off = document.layers._off()  # noqa: SLF001 - read the default configuration once per selection.
            if _visible_with_state(oc, off):
                return _alternate_image(entry)
    return base


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


def _image_input_limit(budget: _core._OperationBudget) -> int:
    """Return the effective encoded-image byte limit under ``budget``."""
    configured = budget.limit("image_input_size")
    return _MAX_IMAGE_INPUT if configured is None else min(_MAX_IMAGE_INPUT, configured)


def _checked_image_bytes(document: Document, data: object) -> bytes:
    """Validate bytes-like image input and enforce ``image_input_size`` and ``memory_size`` before copying."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        msg = f"data must be bytes, bytearray, or memoryview, not {type(data).__name__}"
        raise TypeError(msg)
    size = data.nbytes if isinstance(data, memoryview) else len(data)

    def check(budget: _core._OperationBudget) -> bytes:
        limit = _image_input_limit(budget)
        if size > limit:
            code = "image_input_size"
            msg = f"the image data is larger than {limit} bytes"
            raise LimitError(code, msg)
        budget.check("image_input_size", size)
        budget.check("memory_size", size)
        if isinstance(data, bytes):
            return data
        budget.charge("memory_size", size)
        try:
            return bytes(data)
        finally:
            budget.release("memory_size", size)

    return _core.read_operation(document._core(), check)  # noqa: SLF001


def replace_image(document: Document, xref: int, data: bytes | bytearray | memoryview) -> None:
    """Replace image object ``xref`` with JPEG, JPEG 2000, or PNG ``data`` in one transaction."""
    stream = _image_stream(document, xref)
    checked = _checked_image_bytes(document, data)
    with document.transaction("replace image"):
        _store_image(document, stream, checked)


def add_image(document: Document, data: bytes | bytearray | memoryview) -> int:
    """Add JPEG, JPEG 2000, or PNG ``data`` as a new image object, in the current transaction, and return its number."""
    checked = _checked_image_bytes(document, data)
    xref = document.objects.add_stream(b"", {"Type": Name("XObject"), "Subtype": Name("Image")}, filter=None)
    _store_image(document, document.objects[xref], checked)
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


@dataclass(frozen=True, slots=True, kw_only=True)
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
_SAMPLE_SPACES = frozenset({"DeviceCMYK", "ICCBased", "CalGray", "CalRGB", "Lab", "Indexed", "Separation", "DeviceN"})
_BITS = 8
#: The bits per component of stored samples that a rewrite reads; 1 is bitonal.
_SAMPLE_BITS = frozenset({2, 4, 8, 16})
_JPEG_COMPONENTS = frozenset({1, 3, _CMYK})
_MAX_QUALITY = 100
_LOSSLESS_FORMATS = ("flate", "jpeg", "jpx")
_BITONAL_FORMATS = ("fax", "fax-g3", "fax-g3-2d", "jbig2", "flate")
_IMAGE_FILTERS = {
    "jpeg": "DCTDecode",
    "jpx": "JPXDecode",
    "fax": "CCITTFaxDecode",
    "fax-g3": "CCITTFaxDecode",
    "fax-g3-2d": "CCITTFaxDecode",
    "jbig2": "JBIG2Decode",
    "flate": "FlateDecode",
    "flate1": "FlateDecode",
}
_FAX_K = {"fax": -1, "fax-g3": 0, "fax-g3-2d": 2}


def _fax_parameters(form: str, size: tuple[int, int]) -> dict[str, object] | None:
    if form not in _FAX_K:
        return None
    parameters: dict[str, object] = {"K": _FAX_K[form], "Columns": size[0], "Rows": size[1]}
    if form != "fax":
        parameters["EndOfLine"] = True
    return parameters


def _filter_version(document: Document, filter_name: str) -> None:
    if filter_name in ("JBIG2Decode", "JPXDecode"):
        _require_version(document, document.catalog, (1, 4 if filter_name == "JBIG2Decode" else 5))


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
    """The selected image family, including JPEG 2000 sources that can keep JPX output."""
    gray: bool
    samples: bool
    """Whether the stored samples are resampled, in the image's own color space, rather than hayro's colors."""
    indexed: bool = False
    """Whether the stored samples are palette indexes, which keep their bits and are resampled to the nearest."""
    width: float = 0.0
    """The longest length, in points, that a placement gives the image's width."""
    height: float = 0.0


@dataclass(frozen=True, slots=True)
class _Options:
    document: Document
    threshold: float | None
    target: float
    quality: int | None
    selected: dict[str, bool]
    set_to_gray: bool
    lossless_format: str
    bitonal_format: str
    budget: _core._OperationBudget | None = None

    def poll(self) -> None:
        if self.budget is not None:
            self.budget.poll()

    def jpeg(self, kind: str) -> bool:
        return kind in ("lossy", "jpx") or (kind == "lossless" and self.lossless_format == "jpeg")


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


def _classify(stream: Stream, item: Image, selected: dict[str, bool]) -> tuple[str, bool, bool, bool] | str | None:
    """Return a selected image's kind, whether it is gray, whether its stored samples are resampled, and are indexes.

    An image that its kind and color flags leave out returns ``None``, and
    one selected but not rewritten returns why.
    """
    names = _filters(stream.get("Filter"))
    image_filter = names[-1] if names else ""
    bitonal = item.is_stencil or item.bits == 1 or image_filter in _BITONAL_FILTERS
    kind = "bitonal" if bitonal else "lossy" if image_filter in _LOSSY_FILTERS else "lossless"
    indexed = item.colorspace == "Indexed"
    gray = bitonal or (item.components == 1 and not indexed)
    if not selected[kind] or not (bitonal or selected["gray" if gray else "color"]):
        return None
    reason = _unsupported(item, image_filter, bitonal=bitonal) or _mask_reason(stream, item)
    if reason is not None:
        return reason
    samples = _keeps_samples(item, image_filter, bitonal=bitonal)
    return "jpx" if image_filter == "JPXDecode" and not bitonal else kind, gray, samples, samples and indexed


def _keeps_samples(item: Image, image_filter: str, *, bitonal: bool) -> bool:
    """Return whether a rewrite resamples an image's stored samples in its own space, rather than hayro's colors.

    Samples of 2 or 4 bits keep their space and depth; JPEG 2000 that
    names no color space paints the colors of its own.
    """
    if bitonal or (image_filter == "JPXDecode" and not item.colorspace):
        return False
    return item.bits < _BITS or _PAINTED_SPACES.get(item.colorspace) != item.components


def _unsupported(item: Image, image_filter: str, *, bitonal: bool) -> str | None:
    """Return why a rewrite would not keep an image's samples, or ``None`` when it would."""
    family = item.colorspace or "no color space"
    stored = _keeps_samples(item, image_filter, bitonal=bitonal)
    checks = (
        (item.is_stencil, "a stencil mask"),
        (item.width * item.height > _MAX_SOURCE_PIXELS, f"more than {_MAX_SOURCE_PIXELS:,} pixels"),
        (bitonal and item.colorspace != "DeviceGray", f"a bitonal image in {family}"),
        # JPEG 2000 states its own depth, which hayro scales to 8 bits.
        (
            not bitonal and image_filter != "JPXDecode" and item.bits not in _SAMPLE_BITS,
            f"{item.bits} bits per component",
        ),
        (
            stored and (item.colorspace not in _SAMPLE_SPACES | _PAINTED_SPACES.keys() or item.components < 1),
            f"a {family} color space",
        ),
        (stored and family == "Indexed" and image_filter in _LOSSY_FILTERS, f"an Indexed image in {image_filter}"),
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
    placements; ``inline`` gains each page's count of inline images and
    stencils, which use their own stream rewriting below.
    """
    candidates: dict[int, _Candidate | None] = {}
    for page in document.pages:
        inline.append(0)
        for image in page.images(annotations=True):
            item = image.item
            if item.xref == 0 or item.is_stencil:
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
    if pixels.components in (1, 3):
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


def _compress(data: bytes, budget: _core._OperationBudget | None) -> bytes:
    """Flate-compress ``data``, enforcing ``memory_size`` and polling under ``budget``."""
    if budget is None:
        return zlib.compress(data, 9)
    budget.charge("memory_size", len(data))
    try:
        return zlib.compress(data, 9)
    finally:
        budget.release("memory_size", len(data))


def _encode(  # noqa: PLR0913 - image layout, encoder settings, and the document budget.
    document: Document,
    pixels: _Pixels,
    form: str,
    quality: int,
    limit: int,
    *,
    adobe: bool = False,
    budget: _core._OperationBudget | None = None,
) -> bytes | None:
    """Encode color or bitonal samples in the selected representation.

    CMYK JPEG data carries an Adobe marker when ``adobe``, as its source did.
    """
    if form == "fax":
        return _core.encode_ccitt_g4(document._core(), _bitonal(pixels), pixels.width, pixels.height, limit)  # noqa: SLF001
    if form in ("fax-g3", "fax-g3-2d", "jbig2", "jpx"):
        samples = pixels.color if form == "jpx" else _bitonal(pixels)
        return _core.encode_image_samples(
            document._core(),  # noqa: SLF001
            form,
            samples,
            (pixels.width, pixels.height),
            pixels.components,
            limit,
        )
    if form == "flate1":
        return _compress(_bitonal(pixels), budget)
    if form == "jpeg":
        return _core.encode_jpeg_samples(
            document._core(),  # noqa: SLF001
            pixels.color,
            pixels.width,
            pixels.height,
            pixels.components,
            quality,
            adobe,
        )
    return _compress(pixels.color, budget)


def _form(kind: str, components: int, options: _Options, *, indexed: bool = False, stored_samples: bool = False) -> str:
    if kind == "bitonal":
        return "flate1" if options.bitonal_format == "flate" else options.bitonal_format
    if kind in ("lossless", "jpx") and options.lossless_format == "jpx":
        return "jpx" if components in (1, 3) and not stored_samples and not indexed else "flate"
    return "jpeg" if options.jpeg(kind) and components in _JPEG_COMPONENTS and not indexed else "flate"


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
    if stored is not None and stored.filter in _LOSSY_FILTERS:
        width, height, components, color, _ = _core.image_samples(
            candidate.image.page.document._core(),  # noqa: SLF001
            stored.data,
            stored.filter == "JPXDecode",
        )
        if (width, height, components) != (item.width, item.height, item.components):
            msg = f"image object {item.xref} stores {components}-component data of {width} by {height} pixels"
            raise PdfError(msg)
    else:
        color = _unpack(stream.read(), item, keep=candidate.indexed)
    if len(color) != item.width * item.height * item.components:
        msg = f"image object {item.xref} holds fewer samples than its size needs"
        raise PdfError(msg)
    mask = stream.get("SMask")
    if isinstance(mask, Stream) and isinstance(mask.get("Matte"), Array) and painted.alpha is not None:
        color = _unmatte(color, painted.alpha, mask["Matte"], item.components)
    return _Pixels(item.width, item.height, item.components, color, painted.alpha)


def _unpack(data: bytes, item: Image, *, keep: bool) -> bytes:
    """Return stored samples of 2, 4, 8, or 16 bits as one byte each, rows unpadded.

    Samples of 2 and 4 bits scale exactly to 8 bits unless ``keep``, which
    keeps their values, as palette indexes; 16-bit samples keep their high
    8 bits.
    """
    bits, count = item.bits, item.width * item.components
    if bits == _BITS:
        return data[: count * item.height]
    if bits == _BITS * 2:
        return data[: count * item.height * 2 : 2]
    stride, per_byte = (count * bits + 7) // 8, 8 // bits
    top = (1 << bits) - 1
    scale = 1 if keep else 255 // top
    tables = [
        bytes(((byte >> (8 - bits * (slot + 1))) & top) * scale for byte in range(256)) for slot in range(per_byte)
    ]
    rows = []
    for row in range(min(item.height, len(data) // stride)):
        packed = data[row * stride : (row + 1) * stride]
        rows.append(_interleave([packed.translate(table) for table in tables])[:count])
    return b"".join(rows)


def _pack(data: bytes, width: int, bits: int) -> bytes:
    """Return one-byte samples of ``bits`` bits packed into rows of ``width`` samples, padded to whole bytes."""
    if bits == _BITS:
        return data
    per_byte = 8 // bits
    stride = (width + per_byte - 1) // per_byte
    rows = []
    for at in range(0, len(data), width):
        row = data[at : at + width].ljust(stride * per_byte, b"\0")
        value = 0
        for slot in range(per_byte):
            value |= int.from_bytes(row[slot::per_byte]) << (8 - bits * (slot + 1))
        rows.append(value.to_bytes(stride))
    return b"".join(rows)


def _nearest(pixels: _Pixels, size: tuple[int, int]) -> _Pixels:
    """Resample ``pixels`` to ``size`` by the nearest sample, which keeps each value, as a palette index."""
    if size == (pixels.width, pixels.height):
        return pixels
    width, height, components = pixels.width, pixels.height, pixels.components
    xs = [min(width - 1, (2 * x + 1) * width // (2 * size[0])) for x in range(size[0])]
    ys = [min(height - 1, (2 * y + 1) * height // (2 * size[1])) for y in range(size[1])]
    picks = [x * components + channel for x in xs for channel in range(components)]
    stride = width * components
    color = b"".join(bytes(pixels.color[y * stride + at] for at in picks) for y in ys)
    alpha = None
    if pixels.alpha is not None:
        alpha = _resized(_Pixels(width, height, 1, pixels.alpha, None), size).color
    return _Pixels(size[0], size[1], components, color, alpha)


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
) -> tuple[int, int | None]:
    """Re-encode one image at ``size`` and store it when it is smaller enough; return its old and new stored lengths.

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
    pixels = (_nearest if candidate.indexed else _resized)(_source(candidate, stream), size)
    form = _form(
        candidate.kind, pixels.components, options, indexed=candidate.indexed, stored_samples=candidate.samples
    )
    data = _encode_candidate(candidate, pixels, form, options, limit)
    alpha = None if pixels.alpha is None else _compress(pixels.alpha, options.budget)
    resampled = [
        (key, mask_size, _encode_mask(mask, mask_size, options, limit, stencil=key == "Mask"))
        for key, mask, mask_size in masks
        if key not in kept
    ]
    after = None if data is None else len(data) + len(alpha or b"")
    for _, _, encoded in resampled:
        after = None if after is None or encoded is None else after + len(encoded[0])
    if after is None or after > limit:
        return before, None
    # Resampled stored samples keep the color space and the /Decode array, which stay in place.
    for key in _DATA_KEYS:
        if key in stream and key not in kept and not (candidate.samples and key in ("ColorSpace", "Decode")):
            del stream[key]
    stream.write(data, filter=None)
    entries: dict[str, object] = {
        "Width": size[0],
        "Height": size[1],
        "BitsPerComponent": 1 if candidate.kind == "bitonal" else _depth(candidate, form),
        "Filter": Name(_IMAGE_FILTERS[form]),
    }
    if not candidate.samples:
        entries["ColorSpace"] = Name("DeviceGray" if pixels.components == 1 else "DeviceRGB")
    if (parameters := _fax_parameters(form, size)) is not None:
        entries["DecodeParms"] = parameters
    _filter_version(document, _IMAGE_FILTERS[form])
    stream.update(entries)
    if alpha is not None:
        stream["SMask"] = _add_mask(document, size, (alpha, "FlateDecode", None), stencil=False)
    for key, mask_size, encoded in resampled:
        if encoded is not None:
            stream[key] = _add_mask(document, mask_size, encoded, stencil=key == "Mask")
    return before, after


def _encode_candidate(candidate: _Candidate, pixels: _Pixels, form: str, options: _Options, limit: int) -> bytes | None:
    """Encode a candidate's resampled pixels: palette indexes at their bits, and CMYK JPEG with its source's marker."""
    bits = candidate.image.item.bits
    if candidate.indexed:
        return _compress(_pack(pixels.color, pixels.width * pixels.components, bits), options.budget)
    if candidate.samples and bits < _BITS and form == "flate":
        # Resampled samples round to the nearest of their depth's levels.
        top = (1 << bits) - 1
        levels = bytes((value * top + 127) // 255 for value in range(256))
        return _compress(_pack(pixels.color.translate(levels), pixels.width * pixels.components, bits), options.budget)
    stored = candidate.image.stored() if candidate.samples and form == "jpeg" else None
    adobe = stored is not None and stored.filter == "DCTDecode" and _jpeg_frame(stored.data)[3]
    quality = _DEFAULT_QUALITY if options.quality is None else options.quality
    return _encode(options.document, pixels, form, quality, limit, adobe=adobe, budget=options.budget)


def _depth(candidate: _Candidate, form: str) -> int:
    """Return the bits per component of a candidate's new samples: 2 and 4 bits stay, but not in JPEG."""
    bits = candidate.image.item.bits
    return bits if candidate.samples and bits < _BITS and form == "flate" else _BITS


def _mask_size(mask: Stream, lengths: tuple[float, float], options: _Options) -> tuple[int, int]:
    """Return the pixels that a mask of another size keeps, at the image's largest placement."""
    return _target_size((mask["Width"], mask["Height"]), lengths, options.threshold, options.target)


def _encode_mask(
    mask: Stream, size: tuple[int, int], options: _Options, limit: int, *, stencil: bool
) -> tuple[bytes, str, dict[str, object] | None] | None:
    """Return a mask resampled to ``size``: its data, filter, and parameters, or ``None`` when larger than ``limit``."""
    options.poll()
    pixels = _resized(_mask_pixels(mask, limits=options.document.limits, stencil=stencil), size)
    if not stencil:
        return _compress(pixels.color, options.budget), "FlateDecode", None
    form = _form("bitonal", 1, options)
    data = _encode(options.document, pixels, form, 0, limit, budget=options.budget)
    if data is None:
        return None
    return data, _IMAGE_FILTERS[form], _fax_parameters(form, size)


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
    _filter_version(document, filter_name)
    if parameters is not None:
        added["DecodeParms"] = parameters
    return added


def _mask_pixels(mask: Stream, *, limits: DocumentLimits | None = None, stencil: bool) -> _Pixels:
    """Return a mask's pixels at its own size, black where a stencil paints, as hayro decodes it alone.

    hayro decodes a mask of another size than its image only to paint it,
    so a scratch document paints the mask, its data copied as stored.
    """
    with _new_document(limits) as scratch:
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


def _new_document(limits: DocumentLimits | None = None) -> Document:
    from pyropdf._document import Document  # noqa: PLC0415 - the document module imports this one.

    return Document(limits=limits)


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
    # ISO 32000-2, 8.9.7: JBIG2Decode and JPXDecode are XObject filters.
    if form in ("jbig2", "jpx"):
        form = "flate1" if kind == "bitonal" else "flate"
    limit = int(len(image.data) * (1 - _MIN_SAVING))
    quality = _DEFAULT_QUALITY if options.quality is None else options.quality
    data = _encode(options.document, pixels, form, quality, limit, budget=options.budget)
    if data is None or len(data) > limit:
        return image
    dictionary: dict[str, object] = {"W": size[0], "H": size[1], "BPC": 1 if kind == "bitonal" else 8}
    if stencil:
        dictionary["IM"] = True
    else:
        dictionary["CS"] = Name("G" if pixels.components == 1 else "RGB")
    dictionary["F"] = Name("CCF" if form in _FAX_K else "DCT" if form == "jpeg" else "Fl")
    if (parameters := _fax_parameters(form, size)) is not None:
        dictionary["DP"] = parameters
    dictionary.update({key: value for key, value in entries.items() if key in ("I", "Interpolate", "Intent")})
    tally.kept(len(image.data), len(data))
    return InlineImage(dictionary, data)


def _lengths(matrix: tuple[float, ...]) -> tuple[float, float]:
    """Return the lengths, in points, that ``matrix`` gives the unit square's sides."""
    return math.hypot(matrix[0], matrix[1]), math.hypot(matrix[2], matrix[3])


def _rewrite_page_inline(page: Page, options: _Options, tally: _Tally, nested: _Nested) -> int:
    """Rewrite the inline images of a page's own content, which the page then writes as one new stream.

    ``nested`` reads the Form XObjects and appearances that the page draws.
    Return how many inline and stencil images the page paints that a rewrite reaches.
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
        inline and stencil images the forms that it draws hold, counted per drawing.
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
        """Follow a ``Do``; return how many inline and stencil images it paints."""
        xobject = xobjects.get(str(name)) if isinstance(name, Name) else None
        if not isinstance(xobject, Stream):
            return 0
        if xobject.get("Subtype") == "Image" and xobject.get("ImageMask") is True:
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
            operations = _parse_form(form)
            if operations is None:
                self.tally.report(f"object {number}: a Form XObject that cannot be read")
            self.parsed[number] = operations
        return self.parsed[number]

    def annotations(self, page: Page) -> int:
        """Follow the normal appearances of a page's annotations; count their inline and stencil images."""
        annotations = page.obj.get("Annots")
        reached = 0
        for annotation in annotations if isinstance(annotations, Array) else ():
            if not isinstance(annotation, Dictionary):
                continue
            for key, state, stream in _appearance_streams(annotation, ("N",)):
                matrix = _appearance_matrix(annotation, stream)
                if matrix is None:
                    continue
                number, count = self.form(stream, (None, {}), matrix, 1)
                reached += count
                if number is not None:
                    self.appearances.append((annotation, key, state, number))
        return reached

    def write(self, document: Document, options: _Options) -> None:
        """Rewrite each stencil mask image, and each form's inline images once; draw the forms that change anew."""
        if options.selected["bitonal"]:
            for stream, width, height in self.stencils.values():
                _rewrite_stencil(document, stream, (width, height), options, self.tally)
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
        data = Operations(out, tail=record.operations.tail).to_bytes()
        added = _copy_form(document, record.stream, data, record.resources, children)
        self.copies[number] = document.objects[added]


def _rewrite_stencil(
    document: Document, stream: Stream, lengths: tuple[float, float], options: _Options, tally: _Tally
) -> None:
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
    _filter_version(document, filter_name)
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

    def run(budget: _core._OperationBudget) -> ImageRewriteReport:
        if threshold is None and quality is None and not set_to_gray:
            return ImageRewriteReport(considered=0, rewritten=0, bytes_before=0, bytes_after=0)
        options = _Options(
            document, threshold, target, quality, selected, set_to_gray, lossless_format, bitonal_format, budget
        )
        tally = _Tally()
        inline: list[int] = []
        candidates = _candidates(document, options, tally, inline)
        tally.considered = len(candidates)
        with document.transaction("rewrite images"):
            for xref, candidate in candidates.items():
                budget.poll()
                if candidate is None:
                    continue
                item = candidate.image.item
                size = _target_size((item.width, item.height), (candidate.width, candidate.height), threshold, target)
                recolor = set_to_gray and not candidate.gray
                if size == (item.width, item.height) and not recolor and quality is None:
                    continue
                if recolor:
                    candidate.gray, candidate.samples = True, False
                before, after = _rewrite(document, xref, candidate, size, options)
                tally.kept(before, after)
            nested = _Nested(tally)
            for index, (page, count) in enumerate(zip(document.pages, inline, strict=False)):
                budget.poll()
                missed = count - (_rewrite_page_inline(page, options, tally, nested) if count else 0)
                if missed > 0:
                    tally.report(
                        f"page {index}: {missed} inline or stencil images that a rewrite does not reach, as in patterns"
                    )
            nested.write(document, options)
        return ImageRewriteReport(
            considered=tally.considered,
            rewritten=tally.rewritten,
            bytes_before=tally.before,
            bytes_after=tally.after,
            unsupported=tuple(tally.unsupported or ()),
        )

    return _core.read_operation(document._core(), run)  # noqa: SLF001


def _encoded_image_size(data: bytes) -> tuple[int, int] | None:
    """Return ``(width, height)`` from JPEG, JPEG 2000, or PNG headers without decoding pixels."""
    if data.startswith(b"\xff\xd8\xff"):
        width, height, _components, _adobe = _jpeg_frame(data)
        return width, height
    if data.startswith((_JP2_SIGNATURE, _J2K_SIGNATURE)):
        return _jpx_size(data)
    if data.startswith(_PNG_SIGNATURE):
        if len(data) < _MIN_PNG_IHDR_BYTES or data[12:16] != b"IHDR":
            msg = "the PNG data has no IHDR header"
            raise ValueError(msg)
        width, height = struct.unpack(">II", data[16:24])
        if width <= 0 or height <= 0:
            msg = "the PNG image header has non-positive dimensions"
            raise ValueError(msg)
        return width, height
    return None


def _natural_tokens(text: str) -> tuple[tuple[int, int, str, int, str] | tuple[int, str, str], ...]:
    """Return natural-sort tokens for one path component without integer string conversion limits."""
    tokens: list[tuple[int, int, str, int, str] | tuple[int, str, str]] = []
    for part in _NATURAL_SPLIT.split(text):
        if not part:
            continue
        if part.isascii() and part.isdigit():
            stripped = part.lstrip("0") or "0"
            tokens.append((0, len(stripped), stripped, len(part), part))
        else:
            tokens.append((1, part.casefold(), part))
    return tuple(tokens)


def _split_extension(basename: str) -> tuple[str, str]:
    """Split ``basename`` into ``(stem, suffix)`` matching ``PurePosixPath`` semantics."""
    dot = basename.rfind(".")
    if dot <= 0:
        return basename, ""
    return basename[:dot], basename[dot:]


def _cbz_sort_key(filename: str) -> tuple[tuple[tuple[object, ...], tuple[object, ...]], ...]:
    """Return a deterministic natural-order sort key across nested archive directories."""
    normalized = filename.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    parts = [part for part in normalized.split("/") if part]
    if not parts:
        return ()
    stem, ext = _split_extension(parts[-1])
    return (
        *((_natural_tokens(part), ()) for part in parts[:-1]),
        (_natural_tokens(stem), _natural_tokens(ext)),
    )


def _validate_cbz_entry(info: zipfile.ZipInfo) -> list[str]:
    """Validate one ZIP entry name and mode against NUL bytes, path traversal, and symlinks."""
    raw_name = getattr(info, "orig_filename", info.filename)
    for candidate in (raw_name, info.filename):
        if "\x00" in candidate:
            msg = f"CBZ entry {raw_name!r} contains a NUL byte"
            raise ValueError(msg)
        if candidate.startswith(("/", "\\")) or (
            len(candidate) >= 2 and candidate[0].isalpha() and candidate[1] == ":"  # noqa: PLR2004
        ):
            msg = f"CBZ entry {raw_name!r} has an absolute path"
            raise ValueError(msg)
        cand_parts = [part for part in _PATH_SEPARATORS.split(candidate) if part not in ("", ".")]
        if ".." in cand_parts:
            msg = f"CBZ entry {raw_name!r} escapes the archive root"
            raise ValueError(msg)
    if stat.S_ISLNK(info.external_attr >> 16):
        msg = f"CBZ entry {raw_name!r} is a symbolic link"
        raise ValueError(msg)
    return [part for part in _PATH_SEPARATORS.split(info.filename) if part not in ("", ".")]


def _is_cbz_image_entry(info: zipfile.ZipInfo, parts: list[str]) -> bool:
    """Return whether a validated ZIP entry names an image page rather than OS or comic metadata."""
    if info.is_dir() or info.filename.endswith(("/", "\\")) or not parts:
        return False
    if any(part == "__MACOSX" for part in parts):
        return False
    basename = parts[-1]
    if basename.startswith("._") or basename.lower() in _IGNORED_ARCHIVE_FILES:
        return False
    _, ext = _split_extension(basename)
    return ext.lower() in _CBZ_IMAGE_EXTENSIONS


def _effective_limit(budget: _core._OperationBudget, hard_max: int, *codes: str) -> int:
    """Return the smallest active limit among ``hard_max`` and ``codes``."""
    bound = hard_max
    for code in codes:
        configured = budget.limit(code)
        if configured is not None:
            bound = min(bound, configured)
    return bound


def _check_ratio(
    info: zipfile.ZipInfo,
    output_bytes: int,
    archive_size: int,
    *,
    max_span: int | None = None,
) -> None:
    """Refuse compressed ZIP members whose expansion ratio exceeds ``_MAX_ZIP_EXPANSION_RATIO``."""
    if info.compress_type == zipfile.ZIP_STORED:
        if output_bytes > archive_size:
            msg = f"CBZ entry {info.filename!r} exceeds the archive size"
            raise ValueError(msg)
        return
    if output_bytes <= _ZIP_RATIO_FLOOR:
        return
    bound = archive_size if max_span is None else min(archive_size, max_span)
    compressed = max(1, min(info.compress_size, bound))
    if output_bytes > compressed * _MAX_ZIP_EXPANSION_RATIO:
        code = "decompressed_size"
        msg = f"CBZ entry {info.filename!r} expands beyond the {_MAX_ZIP_EXPANSION_RATIO}x decompression ratio limit"
        raise LimitError(code, msg)


def _check_archive_ratio(total_output: int, archive_size: int) -> None:
    """Refuse archives whose cumulative decompressed bytes exceed ``_MAX_ZIP_EXPANSION_RATIO``."""
    if total_output <= _ZIP_RATIO_FLOOR:
        return
    if total_output > max(1, archive_size) * _MAX_ZIP_EXPANSION_RATIO:
        code = "total_decompressed_size"
        msg = f"the CBZ archive expands beyond the {_MAX_ZIP_EXPANSION_RATIO}x total decompression ratio limit"
        raise LimitError(code, msg)


def _check_entry_size(budget: _core._OperationBudget, size: int, hard_max: int) -> None:
    """Enforce per-entry ``decompressed_size`` and ``image_input_size`` limits."""
    budget.check("decompressed_size", size)
    budget.check("image_input_size", size)
    if size > hard_max:
        code = "image_input_size"
        msg = f"the image data is larger than {hard_max} bytes"
        raise LimitError(code, msg)


def _join_charged(parts: list[bytes], total: int, budget: _core._OperationBudget) -> bytes:
    """Join ``parts`` while temporarily charging their combined copy against ``memory_size``."""
    if not parts:
        return b""
    if len(parts) == 1:
        return parts[0]
    budget.charge("memory_size", total)
    try:
        data = b"".join(parts)
    finally:
        budget.release("memory_size", total)
    parts.clear()
    return data


def _require_bytes_chunk(chunk: object) -> bytes:
    """Validate that a stream read returned ``bytes`` or ``bytearray``."""
    if isinstance(chunk, bytes):
        return chunk
    if isinstance(chunk, bytearray):
        return bytes(chunk)
    msg = f"file-like source must return bytes, not {type(chunk).__name__}"
    raise TypeError(msg)


def _check_stream_total(budget: _core._OperationBudget, total: int, limit: int, limit_code: str) -> None:
    """Refuse a stream read once ``total`` exceeds ``limit``."""
    if total > limit:
        budget.check(limit_code, total)
        msg = f"the input is larger than {limit} bytes"
        raise LimitError(limit_code, msg)


def _read_bounded_stream(
    stream: BinaryIO,
    budget: _core._OperationBudget,
    *,
    hard_max: int,
    limit_code: str,
) -> tuple[bytes, int]:
    """Read a binary stream in chunks under ``limit_code`` and ``memory_size``; return ``(data, charged)``."""
    limit = _effective_limit(budget, hard_max, limit_code)
    parts: list[bytes] = []
    total = 0
    try:
        while True:
            budget.poll()
            remaining = limit - total
            chunk_bytes = _require_bytes_chunk(stream.read(min(_ZIP_READ_CHUNK, remaining + 1)))
            if not chunk_bytes:
                break
            _check_stream_total(budget, total + len(chunk_bytes), limit, limit_code)
            budget.charge("memory_size", len(chunk_bytes))
            total += len(chunk_bytes)
            parts.append(chunk_bytes)
        data = _join_charged(parts, total, budget)
    except BaseException:
        budget.release("memory_size", total)
        raise
    return data, total


def _find_zip_eocd(tail: bytes) -> int:
    """Return the offset within ``tail`` of the ZIP End of Central Directory record, or -1."""
    search_end = len(tail)
    while search_end >= _ZIP_EOCD_MIN_SIZE:
        pos = tail.rfind(_ZIP_EOCD_SIG, 0, search_end)
        if pos < 0:
            return -1
        if pos + _ZIP_EOCD_MIN_SIZE <= len(tail):
            (comment_len,) = struct.unpack("<H", tail[pos + 20 : pos + 22])
            if pos + _ZIP_EOCD_MIN_SIZE + comment_len == len(tail):
                return pos
        search_end = pos
    return -1


def _read_zip64_directory_bounds(
    stream: BinaryIO,
    archive_size: int,
    eocd_file_offset: int,
) -> tuple[int, int] | None:
    """Read ``(total_entries, cd_size)`` from a ZIP64 EOCD record, or ``None`` if absent/invalid."""
    locator_offset = eocd_file_offset - _ZIP64_LOCATOR_SIZE
    if locator_offset < 0:
        return None
    stream.seek(locator_offset)
    locator = stream.read(_ZIP64_LOCATOR_SIZE)
    if len(locator) != _ZIP64_LOCATOR_SIZE or locator[:4] != _ZIP64_LOCATOR_SIG:
        return None
    (eocd64_offset,) = struct.unpack("<Q", locator[8:16])
    if eocd64_offset + _ZIP64_EOCD_MIN_SIZE > archive_size:
        msg = "invalid CBZ archive: ZIP64 central directory locator is out of range"
        raise ValueError(msg)
    stream.seek(eocd64_offset)
    record = stream.read(_ZIP64_EOCD_MIN_SIZE)
    if len(record) != _ZIP64_EOCD_MIN_SIZE or record[:4] != _ZIP64_EOCD_SIG:
        return None
    total_entries, cd_size = struct.unpack("<QQ", record[32:48])
    return total_entries, cd_size


def _preflight_zip_directory(stream: BinaryIO, archive_size: int, budget: _core._OperationBudget) -> int:
    """Enforce ``_MAX_ARCHIVE_ENTRIES`` and charge Central Directory memory before ``zipfile.ZipFile`` parses it."""
    budget.poll()
    if archive_size < _ZIP_EOCD_MIN_SIZE:
        return 0
    tail_len = min(archive_size, _ZIP_MAX_TAIL)
    tail_start = archive_size - tail_len
    try:
        stream.seek(tail_start)
        tail = stream.read(tail_len)
        eocd_pos = _find_zip_eocd(tail)
        if eocd_pos < 0:
            return 0
        _disk, _cd_disk, disk_entries, total_entries, cd_size, cd_offset, _comment_len = struct.unpack(
            "<HHHHIIH", tail[eocd_pos + 4 : eocd_pos + _ZIP_EOCD_MIN_SIZE]
        )
        if (
            disk_entries == 0xFFFF  # noqa: PLR2004 - ZIP64 sentinel
            or total_entries == 0xFFFF  # noqa: PLR2004
            or cd_size == 0xFFFFFFFF  # noqa: PLR2004
            or cd_offset == 0xFFFFFFFF  # noqa: PLR2004
        ):
            zip64 = _read_zip64_directory_bounds(stream, archive_size, tail_start + eocd_pos)
            if zip64 is not None:
                total_entries, cd_size = zip64
    finally:
        stream.seek(0)
    if total_entries > _MAX_ARCHIVE_ENTRIES:
        code = "content_element_count"
        msg = f"the CBZ archive has more than {_MAX_ARCHIVE_ENTRIES} entries"
        raise LimitError(code, msg)
    if cd_size > archive_size:
        msg = "invalid CBZ archive: central directory size exceeds archive size"
        raise ValueError(msg)
    cd_charge = cd_size + total_entries * _ZIP_INFO_ESTIMATE
    budget.charge("memory_size", cd_charge)
    return cd_charge


@contextmanager
def _open_cbz_archive(
    source: str | os.PathLike[str] | bytes | bytearray | memoryview | BinaryIO,
    budget: _core._OperationBudget,
    *,
    max_input: int,
) -> Generator[tuple[zipfile.ZipFile, int]]:
    """Open a CBZ/ZIP archive from path, bytes, or binary stream under ``file_size`` and ``memory_size``."""
    file_limit = _effective_limit(budget, max_input, "file_size", "image_input_size")
    charged = 0
    owned: BinaryIO | None = None
    try:
        if isinstance(source, (str, os.PathLike)):
            handle = open(source, "rb")  # noqa: PTH123, SIM115 - os.PathLike and str alike.
            owned = handle
            archive_size = os.fstat(handle.fileno()).st_size
            _check_archive_size(budget, archive_size, file_limit)
            stream: BinaryIO = handle
        elif isinstance(source, (bytes, bytearray, memoryview)):
            archive_size = source.nbytes if isinstance(source, memoryview) else len(source)
            _check_archive_size(budget, archive_size, file_limit)
            budget.charge("memory_size", archive_size)
            charged = archive_size
            stream = io.BytesIO(source if isinstance(source, bytes) else bytes(source))
        elif hasattr(source, "read") and callable(source.read):
            stream, archive_size, charged = _prepare_cbz_stream(source, budget, file_limit=file_limit)
        else:
            msg = f"source must be a path, bytes, or binary file-like object, not {type(source).__name__}"
            raise TypeError(msg)
        charged += _preflight_zip_directory(stream, archive_size, budget)
        try:
            with zipfile.ZipFile(stream, "r") as archive:
                yield archive, archive_size
        except (zipfile.BadZipFile, zlib.error, EOFError, struct.error, NotImplementedError) as exc:
            msg = f"invalid CBZ archive: {exc}"
            raise ValueError(msg) from exc
    finally:
        if owned is not None:
            owned.close()
        if charged:
            budget.release("memory_size", charged)


def _check_archive_size(budget: _core._OperationBudget, size: int, file_limit: int) -> None:
    """Refuse archives larger than ``file_limit`` or ``DocumentLimits.max_file_size``."""
    budget.check("file_size", size)
    budget.check("image_input_size", size)
    if size > file_limit:
        code = "file_size"
        msg = f"the CBZ archive is larger than {file_limit} bytes"
        raise LimitError(code, msg)


def _prepare_cbz_stream(
    source: BinaryIO, budget: _core._OperationBudget, *, file_limit: int
) -> tuple[BinaryIO, int, int]:
    """Validate a binary file-like CBZ source without unbounded reads."""
    probe = source.read(0)
    if not isinstance(probe, (bytes, bytearray)):
        msg = f"file-like source must be opened in binary mode, not returning {type(probe).__name__}"
        raise TypeError(msg)
    seekable = (
        hasattr(source, "seek") and hasattr(source, "tell") and (not hasattr(source, "seekable") or source.seekable())
    )
    if seekable:
        pos = source.tell()
        if pos == 0:
            size = source.seek(0, os.SEEK_END)
            source.seek(0, os.SEEK_SET)
            _check_archive_size(budget, size, file_limit)
            return source, size, 0
    data, charged = _read_bounded_stream(source, budget, hard_max=file_limit, limit_code="file_size")
    return io.BytesIO(data), len(data), charged


def _check_cbz_spans(infos: list[zipfile.ZipInfo], archive_size: int) -> dict[int, int]:
    """Refuse duplicate, out-of-bounds, or overlapping ZIP member spans; return per-entry max payload span."""
    ordered = sorted(infos, key=lambda entry: (entry.header_offset, entry.compress_size))
    max_spans: dict[int, int] = {}
    for index, info in enumerate(ordered):
        offset = info.header_offset
        comp = info.compress_size
        if offset < 0 or comp < 0 or offset + _ZIP_LOCAL_HEADER_SIZE > archive_size:
            msg = f"invalid CBZ archive: entry {info.filename!r} has an out-of-range header offset"
            raise ValueError(msg)
        next_offset = ordered[index + 1].header_offset if index + 1 < len(ordered) else archive_size
        if index + 1 < len(ordered):
            if next_offset == offset:
                msg = f"invalid CBZ archive: overlapping entries at offset {offset}"
                raise ValueError(msg)
            if offset + _ZIP_LOCAL_HEADER_SIZE + comp > next_offset:
                msg = f"invalid CBZ archive: overlapping entries {info.filename!r} and {ordered[index + 1].filename!r}"
                raise ValueError(msg)
        max_spans[id(info)] = max(1, next_offset - offset - _ZIP_LOCAL_HEADER_SIZE)
    return max_spans


def _select_cbz_entries(
    archive: zipfile.ZipFile,
    budget: _core._OperationBudget,
    *,
    archive_size: int,
    max_frames: int,
    max_input: int,
) -> list[tuple[zipfile.ZipInfo, int]]:
    """Validate every ZIP entry, check archive bounds before decompression, and sort image entries naturally."""
    infos = archive.infolist()
    budget.poll()
    if len(infos) > _MAX_ARCHIVE_ENTRIES:
        code = "content_element_count"
        msg = f"the CBZ archive has more than {_MAX_ARCHIVE_ENTRIES} entries"
        raise LimitError(code, msg)
    max_spans = _check_cbz_spans(infos, archive_size)
    selected: list[zipfile.ZipInfo] = []
    for info in infos:
        budget.poll()
        parts = _validate_cbz_entry(info)
        if _is_cbz_image_entry(info, parts):
            if info.flag_bits & 0x1:
                msg = f"encrypted CBZ entry {info.filename!r} is not supported"
                raise ValueError(msg)
            if info.compress_type not in _ALLOWED_ZIP_METHODS:
                msg = f"unsupported compression method {info.compress_type} for CBZ entry {info.filename!r}"
                raise ValueError(msg)
            selected.append(info)
    if not selected:
        msg = "the CBZ archive contains no image entries"
        raise ValueError(msg)
    if len(selected) > max_frames:
        code = "content_element_count"
        msg = f"the CBZ archive has more than {max_frames} image entries"
        raise LimitError(code, msg)
    total_limit = _effective_limit(budget, max_input, "total_decompressed_size")
    declared_total = 0
    for info in selected:
        _check_entry_size(budget, info.file_size, max_input)
        _check_ratio(info, info.file_size, archive_size, max_span=max_spans[id(info)])
        declared_total += info.file_size
        budget.check("total_decompressed_size", declared_total)
        _check_archive_ratio(declared_total, archive_size)
        if declared_total > total_limit:
            code = "total_decompressed_size"
            msg = f"the CBZ archive expands beyond {total_limit} bytes"
            raise LimitError(code, msg)
    selected.sort(key=lambda entry: (_cbz_sort_key(entry.filename), entry.filename))
    return [(info, max_spans[id(info)]) for info in selected]


def _check_total_decompressed(budget: _core._OperationBudget, total: int, total_limit: int) -> None:
    """Refuse cumulative archive expansion beyond ``total_limit``."""
    if total > total_limit:
        budget.check("total_decompressed_size", total)
        code = "total_decompressed_size"
        msg = f"the CBZ archive expands beyond {total_limit} bytes"
        raise LimitError(code, msg)


def _read_cbz_member(  # noqa: PLR0913 - bounded archive read context.
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    budget: _core._OperationBudget,
    *,
    archive_size: int,
    max_input: int,
    total_so_far: int,
    max_span: int | None = None,
) -> bytes:
    """Read one CBZ member in bounded chunks, charging ``total_decompressed_size`` and ``memory_size``."""
    entry_limit = _effective_limit(budget, max_input, "decompressed_size", "image_input_size")
    total_limit = _effective_limit(budget, max_input, "total_decompressed_size")
    parts: list[bytes] = []
    entry_bytes = 0
    charged = 0
    try:
        with archive.open(info, "r") as member:
            while True:
                budget.poll()
                remaining = min(entry_limit - entry_bytes, total_limit - (total_so_far + entry_bytes))
                chunk = member.read(min(_ZIP_READ_CHUNK, max(remaining, 0) + 1))
                if not chunk:
                    break
                entry_bytes += len(chunk)
                _check_entry_size(budget, entry_bytes, max_input)
                _check_ratio(info, entry_bytes, archive_size, max_span=max_span)
                _check_archive_ratio(total_so_far + entry_bytes, archive_size)
                _check_total_decompressed(budget, total_so_far + entry_bytes, total_limit)
                budget.charge("total_decompressed_size", len(chunk))
                budget.charge("memory_size", len(chunk))
                charged += len(chunk)
                parts.append(chunk)
        return _join_charged(parts, entry_bytes, budget)
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError) as exc:
        if charged:
            budget.release("memory_size", charged)
        msg = f"corrupt CBZ entry {info.filename!r}: {exc}"
        raise ValueError(msg) from exc
    except BaseException:
        if charged:
            budget.release("memory_size", charged)
        raise


# The package re-exports these names; reprs, pickles, and documentation name them.
for _public in (ImageRewriteReport, PageImage, StoredImage):
    _public.__module__ = "pyropdf"
