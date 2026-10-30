"""Images: the pixels and stored data of the images that a page paints, and replacing an image object.

``page.images()`` lists :class:`PageImage` records in the paint order of
:meth:`Page.content`. hayro decodes an image's pixels as it does to render
the page, and the data of an image filter (ISO 32000-2 table 6) passes
through as stored. ``doc.replace_image`` and ``doc.remove_image`` rewrite an
image object in place, so every placement of it changes in one transaction.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pyropdf import _core
from pyropdf._errors import PdfError
from pyropdf._objects import Array, Dictionary, Name, Stream

if TYPE_CHECKING:
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


# The package re-exports these names; reprs, pickles, and documentation name them.
for _public in (PageImage, StoredImage):
    _public.__module__ = "pyropdf"
