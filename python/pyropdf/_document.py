"""Documents: opening, probing, closing, and saving."""

from __future__ import annotations

import os
import secrets
import stat
import sys
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from pyropdf import _chunks, _core, _markdown
from pyropdf._attachments import Attachments
from pyropdf._changes import changes as _changes
from pyropdf._check import structural_check as _structural_check
from pyropdf._core import _Document
from pyropdf._core import probe as _probe
from pyropdf._encryption import Access, Encryption, Permissions, _encryption_arg, _password_bytes
from pyropdf._errors import DocumentClosedError, PdfError
from pyropdf._fonts import FontInfo, FontProgram, SubsetFont, font_program, subset_fonts
from pyropdf._form import Form
from pyropdf._history import History, Transaction
from pyropdf._images import ImageRewriteReport, remove_image, replace_image, rewrite_images
from pyropdf._layers import Layers
from pyropdf._limits import DocumentLimits, _validate_optional_positive_int
from pyropdf._links import Destinations
from pyropdf._metadata import METADATA_KEYS, Metadata, _metadata_value, read_xmp, write_entries, write_xmp
from pyropdf._objects import Dictionary, Objects, _catalog, _trailer
from pyropdf._outline import Outline, OutlineEntry, _write_outline
from pyropdf._page import _clip_box
from pyropdf._page_labels import PageLabelRange, _read_page_labels, _write_page_labels
from pyropdf._pages import Pages, _page_entries, _resolve
from pyropdf._pixmap import Colorspace, _render_background, _render_gray, _render_matrix, _validate_encoding
from pyropdf._profiles import pdfa_claim as _pdfa_claim
from pyropdf._profiles import profiles as _profiles
from pyropdf._recolor import RecolorReport, RecolorTarget, recolor
from pyropdf._sanitize import SanitizeReport, sanitize
from pyropdf._signing import DEFAULT_RESERVE
from pyropdf._signing import sign as _sign
from pyropdf._text import _DEFAULT_MAX_TEXT_GLYPHS, _DEFAULT_MAX_TEXT_SIZE, Unmapped, _warn_skipped, text_options

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from datetime import datetime
    from types import TracebackType
    from typing import Self

    from pyropdf._canvas import _Images
    from pyropdf._changes import ChangeReport
    from pyropdf._check import StructuralReport
    from pyropdf._chunks import Chunk
    from pyropdf._profiles import Profiles
    from pyropdf._signing import Signer, Timestamper

_DEFAULT_MAX_PDF_OUTPUT_SIZE = 512 * 1024 * 1024
_DEFAULT_MAX_RENDER_BATCH_SIZE = 512 * 1024 * 1024
_DEFAULT_RENDER_WORKERS = 4
_MAX_RENDER_WORKERS = 64
_TEMPORARY_FILE_ATTEMPTS = 100

#: The Info dictionary keys that :class:`Probe` reports, by their names in pyropdf.
_PROBE_METADATA_KEYS = (
    ("title", "Title"),
    ("author", "Author"),
    ("subject", "Subject"),
    ("keywords", "Keywords"),
    ("creator", "Creator"),
    ("producer", "Producer"),
    ("created", "CreationDate"),
    ("modified", "ModDate"),
)


class Document:
    """A PDF document.

    :func:`open` opens an existing document, and ``Document()`` creates an
    empty one. Documents are context managers that close on exit.
    """

    __slots__ = ("_access", "_closed", "_doc", "_drawn", "_limits")

    def __init__(self, *, limits: DocumentLimits | None = None) -> None:
        """Create an empty document without pages; :func:`open` opens an existing one."""
        self._setup(_Document.empty, _resolve_limits(limits))

    def _setup(self, load: Callable[[DocumentLimits], _Document], limits: DocumentLimits) -> None:
        """Set the state of a document that ``load`` makes."""
        self._doc = load(limits)
        self._limits = limits
        self._closed = False
        self._access: Access | None = None
        # What canvases stored, so that drawing the same image or page again reuses it.
        self._drawn: _Images | None = None

    @property
    def limits(self) -> DocumentLimits:
        """Return the limits that the document's operations start from."""
        return self._limits

    @property
    def pages(self) -> Pages:
        """Return the pages, a sequence of :class:`Page` handles that edits keep valid."""
        self._ensure_open()
        return Pages(self)

    @property
    def attachments(self) -> Attachments:
        """Return the embedded files, a mapping of :class:`Attachment` handles by name."""
        self._ensure_open()
        return Attachments(self)

    @property
    def objects(self) -> Objects:
        """Return the indirect objects, a mapping by object number.

        A locked encrypted document raises :class:`EncryptedDocumentError`.
        """
        self._ensure_open()
        objects = Objects(self)
        objects._core()  # noqa: SLF001 - refuses a locked document now.
        return objects

    @property
    def destinations(self) -> Destinations:
        """Return the named destinations, a read-only mapping from name to :class:`Destination`."""
        self._ensure_open()
        return Destinations(self)

    @property
    def form(self) -> Form:
        """Return the interactive form, a mapping from full field names to :class:`Field` handles."""
        self._ensure_open()
        return Form(self)

    @property
    def outline(self) -> Outline:
        """Return the outline (bookmarks) as its top-level items.

        Assigning an iterable of :class:`OutlineEntry` records replaces the
        outline, and ``del doc.outline`` removes it; either frees the items
        before that nothing else refers to.
        """
        self._ensure_open()
        return Outline(self)

    @outline.setter
    def outline(self, entries: Iterable[OutlineEntry]) -> None:
        _write_outline(self, entries)

    @outline.deleter
    def outline(self) -> None:
        _write_outline(self, ())

    @property
    def trailer(self) -> Dictionary:
        """Return the trailer dictionary, which names the catalog as ``/Root``."""
        self._ensure_open()
        return _trailer(self)

    @property
    def catalog(self) -> Dictionary:
        """Return the catalog, the indirect dictionary that the trailer's ``/Root`` names."""
        self._ensure_open()
        return _catalog(self)

    def transaction(self, name: str = "") -> Transaction:
        """Return a context manager that groups the edits of its block into one step named ``name``."""
        self._ensure_open()
        return Transaction(self, name)

    def changes(self, *, incremental: bool) -> ChangeReport:
        """Return what the edits since opening invalidate if the document is saved now.

        The report lists the signatures of the opened file that the edits
        change, or that a save that is not ``incremental`` rewrites, and the
        PDF/A or PDF/UA claims of the metadata that the edits break. Its cost
        follows the edits, not the document.
        """
        self._ensure_open()
        return _changes(self, incremental=incremental)

    def check(self) -> StructuralReport:
        """Return how the document departs from the Arlington PDF Model.

        The check walks the current version from the trailer through the
        model's object types and reports each value of the wrong type,
        missing required key, key or value newer than the file's version,
        value direct where it must be indirect or the reverse, and value
        outside the key's possible values. It also reports the ``invariant``
        defects of page, name, number, and structure trees, references, and
        appearance states, and the ``profile`` defects of a claimed PDF/A or
        PDF/UA profile that need no rendering. Each defect names its object
        and its clause. The walk runs under the document's limits.
        """
        self._ensure_open()
        return _structural_check(self)

    @property
    def profiles(self) -> Profiles:
        """Return the PDF/A, PDF/UA, PDF/X, and PDF/VT claims of the XMP metadata, and the output intents.

        The claims are what the document declares, not a validation. The
        metadata read is capped at 16 MiB, beyond which :class:`LimitError`
        is raised with code ``stream_data_size``.
        """
        self._ensure_open()
        return _profiles(self)

    def replace_image(self, xref: int, data: bytes) -> None:
        """Replace image object ``xref`` with JPEG, JPEG 2000, or PNG ``data``, in one transaction.

        The object keeps its number, so every page and form that paints it
        paints the new image. JPEG and JPEG 2000 data is stored as it is;
        PNG is decoded, its alpha written as a soft mask. The image's other
        entries, such as ``/Interpolate``, stay.
        """
        self._ensure_open()
        replace_image(self, xref, data)

    def remove_image(self, xref: int) -> None:
        """Make image object ``xref`` one transparent pixel, so that no placement shows it, in one transaction."""
        self._ensure_open()
        remove_image(self, xref)

    def rewrite_images(  # noqa: PLR0913 - pymupdf's keyword arguments, and pyropdf's output choices
        self,
        *,
        dpi_threshold: float | None = None,
        dpi_target: float | None = None,
        quality: int | None = None,
        lossy: bool = True,
        lossless: bool = True,
        bitonal: bool = True,
        color: bool = True,
        gray: bool = True,
        set_to_gray: bool = False,
        lossless_format: Literal["flate", "jpeg"] = "flate",
        bitonal_format: Literal["fax", "flate"] = "fax",
    ) -> ImageRewriteReport:
        """Downsample and recompress the images that the pages place, each image object once, in one transaction.

        An image whose resolution at its largest placement exceeds
        ``dpi_threshold`` on either axis is resampled to ``dpi_target`` on
        both, never gaining pixels, so an image placed on several pages
        keeps the pixels that its largest placement needs. Resampling uses
        :meth:`Pixmap.scale`'s filter, through premultiplied alpha.
        ``quality`` (1 through 100) re-encodes every selected image even
        when it keeps its size.

        Lossy images (JPEG and JPEG 2000) are written as JPEG at
        ``quality``, or 75 when it is ``None``. Lossless ones are written as
        8-bit Flate, or as JPEG when ``lossless_format`` is ``"jpeg"``.
        Bitonal ones (1 bit, CCITT, or JBIG2) are written as CCITT Group 4
        with ``bitonal_format="fax"``, or as 1-bit Flate with ``"flate"``;
        resampled bitonal pixels below half gray become black. A soft mask,
        a stencil ``/Mask`` of the image's size, or a color-key mask
        becomes a new 8-bit soft mask resampled with the image, so a mask
        that other images share stays as it is. A mask of another size is
        resampled at its own resolution by the image's placement and stays
        a stencil ``/Mask`` (written as the bitonal format says) or a soft
        mask; one that keeps its pixels stays as stored. A soft mask with
        ``/Matte`` is un-premultiplied, and stencil mask images
        (``/ImageMask``) are resampled as bitonal stencils.

        DeviceGray and DeviceRGB images are rewritten from the colors that
        hayro paints, with their ``/Decode`` arrays applied, and so is JPEG
        2000 that names no color space, its own alpha (``/SMaskInData``)
        becoming a soft mask. Images in DeviceCMYK, ICCBased, CalGray,
        CalRGB, Lab, Separation, DeviceN, and Indexed keep their color space
        and ``/Decode`` array: their stored samples, JPEG and JPEG 2000
        decoded as hayro decodes them, are resampled channel by channel, and
        a CMYK JPEG stays a CMYK JPEG, with an Adobe marker when its source
        had one. Indexed images keep their bits and are resampled to the
        nearest index, so that every color stays one of the palette's;
        samples of 2 or 4 bits keep their depth, the resampled values
        rounded to its levels, unless they become JPEG; 16-bit samples keep
        their high 8 bits. Inline images are rewritten
        too, by the placement that the ``cm`` operators give: those of a
        page's own content make it one new stream, and a Form XObject or
        annotation appearance that holds them is rewritten once, at their
        largest drawing, as a new object that the pages draw, so whatever
        else draws the original keeps it.

        ``lossy``, ``lossless``, ``bitonal``, ``color``, and ``gray`` select
        the kinds of image to rewrite. A selected image that a rewrite would
        not keep stays as it is and is named in
        :attr:`ImageRewriteReport.unsupported`: soft masks with ``/Matte``
        of another size, Indexed JPEG and JPEG 2000, other color spaces,
        and inline images that the walk of content does not reach, as in
        patterns.

        ``set_to_gray`` converts the selected color images to DeviceGray,
        and only images, where PyMuPDF's option of the same name converts
        whole pages: :meth:`recolor` with ``"gray"`` converts the text,
        vector graphics, and images of pages.

        A rewrite is kept only when it saves at least 5% of the stored
        data, its masks included, so repeating a call does not re-encode a
        JPEG again. With none of ``dpi_threshold``, ``quality``, and
        ``set_to_gray`` there is nothing to do, and the call returns zero
        counts. A decoding failure raises and leaves the document as it
        was; :attr:`history` can undo the step.
        """
        self._ensure_open()
        return rewrite_images(
            self,
            dpi_threshold=dpi_threshold,
            dpi_target=dpi_target,
            quality=quality,
            selected={"lossy": lossy, "lossless": lossless, "bitonal": bitonal, "color": color, "gray": gray},
            set_to_gray=set_to_gray,
            lossless_format=lossless_format,
            bitonal_format=bitonal_format,
        )

    def recolor(self, target: RecolorTarget, pages: Iterable[int] | None = None) -> RecolorReport:
        """Convert the colors that ``pages`` paint to ``target``, ``"gray"``, ``"rgb"``, or ``"cmyk"``, as one step.

        Every color operator of each page's content, of the Form XObjects
        that it draws, and of its annotations' appearances becomes ``g``,
        ``rg``, or ``k`` (and ``G``, ``RG``, ``K``) in the target. Device,
        CIE-based gray and RGB, and ICC-based spaces convert by their
        component count, Indexed through its base, and Separation through
        its alternate space when its tint transform is exponential, with
        the conversions of ISO 32000-2 10.4.2: BT.601 luma for gray, and
        full black generation for CMYK. Image objects and 8-bit inline
        images convert too: a JPEG to a JPEG at the quality that its
        quantization tables show, or Flate for CMYK, and others to 8-bit
        Flate; their masks stay. The color arrays of annotations (``/C``,
        ``/IC``, and ``/MK``'s ``/BC`` and ``/BG``) convert as well.

        Nothing is edited in place: a Form XObject, appearance, or image
        that changes becomes a new object, so pages left out keep their
        colors. Shadings, patterns, Lab, DeviceN, other tint transforms,
        images whose masks read their samples, and Form XObjects nested
        deeper than 16 or past 4,096 drawings are left as they are and
        listed in :attr:`RecolorReport.unconverted`. ``pages`` holds page
        indexes, negative ones from the end; it defaults to every page.
        """
        self._ensure_open()
        count = len(self.pages)
        if pages is None:
            return recolor(self, target, range(count))
        return recolor(self, target, dict.fromkeys(_resolve(index, count) for index in _page_entries(pages, count)))

    @property
    def layers(self) -> Layers:
        """Return the optional content groups and configurations as a live :class:`Layers` sequence.

        ``doc.layers.switch(xref, on=False)`` hides a group and
        ``doc.layers.use(index)`` applies a configuration of
        ``doc.layers.configs``; rendering and text extraction follow both.
        """
        self._ensure_open()
        return Layers(self)

    @property
    def page_labels(self) -> list[PageLabelRange]:
        """Return the page label ranges in page order, or ``[]`` without labels.

        Each :class:`PageLabelRange` labels the pages from its zero-based
        ``start`` up to the next range; ``page.label`` gives one page's
        label. Assigning a list replaces the ranges in one transaction, the
        first of which must start at page 0, and ``del doc.page_labels``
        removes them. Inserting, moving, and deleting pages keeps each
        page's label. Reads raise :class:`LimitError` past 4,096 ranges or
        kids, 32 levels, or 1 MiB of text; writes stop at 4,096 ranges and
        1 MiB of prefixes (``page_label_input_size``).
        """
        self._ensure_open()
        return _read_page_labels(self)

    @page_labels.setter
    def page_labels(self, ranges: Iterable[PageLabelRange]) -> None:
        self._ensure_open()
        with self.transaction("page labels"):
            _write_page_labels(self, ranges)

    @page_labels.deleter
    def page_labels(self) -> None:
        self._ensure_open()
        _write_page_labels(self, ())

    @property
    def metadata(self) -> Metadata:
        """Return the Info dictionary as a live :class:`Metadata` mapping.

        Assign a mapping to replace every entry in one transaction, or an
        empty one to remove them. Edits write the matching properties of an
        existing XMP packet too, so that the two stay equivalent.
        """
        self._ensure_open()
        return Metadata(self)

    @metadata.setter
    def metadata(self, values: Mapping[str, str | datetime]) -> None:
        self._ensure_open()
        if not isinstance(values, Mapping):
            msg = f"metadata must be a mapping: {type(values).__name__}"
            raise TypeError(msg)
        checked = {key: _metadata_value(key, value) for key, value in values.items()}
        write_entries(self, {key: checked.get(key, "") for key in METADATA_KEYS})

    @property
    def xmp(self) -> str | None:
        """Return the catalog's XMP metadata packet as text, or ``None`` when absent.

        The read is capped at 1 MiB, beyond which :class:`LimitError` is
        raised with code ``stream_data_size``; read a larger packet through
        ``doc.catalog["Metadata"].read(max_size=...)``. Assigning a string
        writes it unfiltered, into the existing stream or a new one, and
        ``del doc.xmp`` removes the catalog's entry. Assigning a packet does
        not change :attr:`metadata`.
        """
        self._ensure_open()
        return read_xmp(self)

    @xmp.setter
    def xmp(self, packet: str) -> None:
        self._ensure_open()
        if not isinstance(packet, str):
            msg = f"xmp must be a str: {type(packet).__name__}"
            raise TypeError(msg)
        write_xmp(self, packet)

    @xmp.deleter
    def xmp(self) -> None:
        self._ensure_open()
        catalog = self.catalog
        if "Metadata" in catalog:
            del catalog["Metadata"]

    @property
    def pdfa_claim(self) -> tuple[int, str] | None:
        """Return the PDF/A part and conformance level of the XMP metadata, such as ``(2, "B")``, or ``None``."""
        self._ensure_open()
        return _pdfa_claim(self)

    @property
    def history(self) -> History:
        """Return the steps that undo and redo walk."""
        self._ensure_open()
        return History(self)

    def _core(self) -> _Document:
        """Return the core document, refusing a closed one."""
        self._ensure_open()
        return self._doc

    @property
    def is_closed(self) -> bool:
        """Return whether :meth:`close` was called."""
        return self._closed

    @property
    def is_repaired(self) -> bool:
        """Return whether opening recovered damaged cross-reference data."""
        self._ensure_open()
        return self._doc.is_repaired()

    @property
    def needs_password(self) -> bool:
        """Return whether the document is locked until :meth:`unlock` receives its password."""
        self._ensure_open()
        encrypted, unlocked, _, _, _ = _core.security_state(self._doc)
        return encrypted and not unlocked

    @property
    def access(self) -> Access | None:
        """Return what opened an unlocked encrypted document, or ``None`` for a locked one.

        An unencrypted document grants :attr:`Access.OWNER`.
        """
        self._ensure_open()
        encrypted, unlocked, opened, _, _ = _core.security_state(self._doc)
        if not encrypted:
            return Access.OWNER
        if not unlocked:
            return None
        access = Access.OWNER if opened == "owner" else Access.USER
        return max(access, self._access) if self._access is not None else access

    @property
    def encryption_method(self) -> str | None:
        """Return the security handler of an unlocked encrypted document, such as ``"Standard V5 R6 256-bit AES"``.

        ``None`` for an unencrypted or locked document.
        """
        self._ensure_open()
        return _core.security_state(self._doc)[4]

    @property
    def permissions(self) -> Permissions:
        """Return what the document permits, as :class:`Permissions` flags.

        A locked document permits nothing. An unencrypted document, or one
        unlocked with its owner password, permits everything. Otherwise the
        flags are those that the encryption grants the user password.
        """
        access = self.access
        if access is None:
            return Permissions(0)
        if access is Access.OWNER:
            return Permissions.ALL
        _, _, _, flags, _ = _core.security_state(self._doc)
        return Permissions((flags or 0) & Permissions.ALL)

    def unlock(self, password: str) -> Access:
        """Unlock the document with a password and return the access that it grants.

        The owner password grants :attr:`Access.OWNER` and the user password
        :attr:`Access.USER`; a password that matches neither raises
        :class:`PasswordError`. An unencrypted document grants
        :attr:`Access.OWNER` to any password. Unlocking a locked document
        reads it again, which starts its history over; unlocking an unlocked
        one only checks the password, and keeps the history. Passwords are
        capped at 127 UTF-8 bytes.
        """
        self._ensure_open()
        opened = _core.unlock(self._doc, _password_bytes(password))
        access = Access.OWNER if opened == "owner" else Access.USER
        if self._access is None or access > self._access:
            self._access = access
        return access

    def save(  # noqa: PLR0913 - the save options are all keyword-only
        self,
        path: str | os.PathLike[str],
        /,
        *,
        incremental: bool = False,
        encryption: Encryption | None = None,
        prune: bool = False,
        compress: bool = False,
        compression_level: int = 6,
        decompress: bool = False,
        keep_id: bool = False,
        cross_reference: Literal["table", "stream"] | None = None,
        object_streams: bool = False,
        renumber: bool = False,
        dedupe: bool = False,
    ) -> None:
        """Write the document to a file.

        The document is written completely to a new file beside ``path``,
        which then replaces ``path`` atomically, keeping an existing file's
        permissions; a final symlink is followed and kept. A failed save
        leaves ``path`` as it was. ``incremental=True`` appends only what
        changed to the bytes of the opened file, so that a save without
        edits reproduces it byte for byte; a full save rewrites every object
        and keeps the values of untouched ones.

        Without ``encryption``, a full save writes an encrypted document
        decrypted and an incremental save keeps its encryption;
        :attr:`Encryption.KEEP` keeps it in a full save too, copying its
        untouched encrypted bytes, and an :class:`Encryption` encrypts a full
        save anew.

        ``prune=True`` writes only the objects that the trailer reaches, so
        what an edit unlinked leaves the file; the document in memory keeps
        them. An incremental save keeps the objects of the opened file, so
        it refuses ``prune``.

        ``compress=True`` compresses with Flate, at zlib's
        ``compression_level`` from 0 through 9, each stream that has no
        filter, where that makes it shorter; XMP metadata stays unfiltered.
        ``decompress=True`` removes the filters that hayro-syntax decodes,
        keeping image filters such as ``DCTDecode``, and excludes
        ``compress``. An incremental save compresses only the streams that
        it writes and refuses ``decompress``. An encrypted save compresses
        before it encrypts. The document in memory keeps its streams.

        A save of an edited document keeps the first element of the
        trailer's ``/ID`` and renews the second, and a full save, or an
        incremental save of edits, gives a document without ``/ID`` a new
        one (ISO 32000-2 section 14.4). The new element is the MD5 digest
        of the bytes before it, so the same document saves to the same
        bytes. A save without edits writes ``/ID`` as read, and so does any
        save with ``keep_id=True``. Encryption keys derive from the first
        element, which a save keeps.

        ``cross_reference`` writes the cross-reference data as a ``"table"``
        or as a ``"stream"`` (PDF 1.5); ``None`` keeps the form of the
        section that the opened file's ``startxref`` names, and writes a
        table for a hybrid-reference file or a document without a file.
        ``object_streams=True`` packs every object that ISO 32000-2 section
        7.5.7 lets an object stream hold into object streams of up to 100
        objects: those that are not streams, have generation 0, and are not
        the encryption dictionary. It writes a cross-reference stream, so
        it excludes ``cross_reference="table"``. Both raise the header's
        version to 1.5 when it is lower. The streams that a save adds are
        compressed with Flate at ``compression_level`` unless
        ``decompress``, and an encrypted save encrypts each object stream
        as a whole. An incremental save keeps the form of the opened file,
        so it refuses both.

        ``renumber=True`` numbers the objects that a full save writes 1, 2,
        3, and so on in the order of their numbers, with generation 0, and
        the objects that it adds after them; each reference follows its
        object, and a reference to an object that the save does not write
        becomes null. An encrypted save encrypts each object with the key
        of its new number. The document in memory keeps its numbers, and an
        incremental save, which keeps the opened file's, refuses it.

        ``dedupe=True`` merges each set of identical objects into the one
        with the smallest number and points every reference to it; streams
        merge when their dictionaries and data are identical. Pages,
        annotations, form fields, structure elements, outline items,
        optional-content groups, the objects that the trailer names, and
        objects in cycles of references never merge. It needs
        ``prune=True``, and so a full save. The document in memory keeps its
        objects.
        """
        self._ensure_open()
        options = _save_options(
            incremental=incremental,
            prune=prune,
            compress=compress,
            compression_level=compression_level,
            decompress=decompress,
            keep_id=keep_id,
            cross_reference=cross_reference,
            object_streams=object_streams,
            renumber=renumber,
            dedupe=dedupe,
        )
        arg = _encryption_arg(encryption)
        _atomic_save_file(path, lambda temporary: self._doc.save_path(temporary, incremental, arg, *options))

    def to_bytes(  # noqa: PLR0913 - the save options are all keyword-only
        self,
        *,
        incremental: bool = False,
        encryption: Encryption | None = None,
        max_size: int | None = _DEFAULT_MAX_PDF_OUTPUT_SIZE,
        prune: bool = False,
        compress: bool = False,
        compression_level: int = 6,
        decompress: bool = False,
        keep_id: bool = False,
        cross_reference: Literal["table", "stream"] | None = None,
        object_streams: bool = False,
        renumber: bool = False,
        dedupe: bool = False,
    ) -> bytes:
        """Return the document as PDF bytes, as :meth:`save` writes them with the same options.

        ``max_size`` bounds the output, 512 MiB by default, and refuses the
        write that would pass it with :class:`LimitError` code
        ``pdf_output_size``; ``None`` leaves it unbounded for trusted input.
        """
        self._ensure_open()
        options = _save_options(
            incremental=incremental,
            prune=prune,
            compress=compress,
            compression_level=compression_level,
            decompress=decompress,
            keep_id=keep_id,
            cross_reference=cross_reference,
            object_streams=object_streams,
            renumber=renumber,
            dedupe=dedupe,
        )
        _validate_optional_positive_int("max_size", max_size)
        return self._doc.to_bytes(incremental, max_size, _encryption_arg(encryption), *options)

    def sign(  # noqa: PLR0913 - the signature's options are all keyword-only
        self,
        field: str,
        signer: Signer,
        *,
        timestamper: Timestamper | None = None,
        reason: str | None = None,
        location: str | None = None,
        contact: str | None = None,
        reserve: int = DEFAULT_RESERVE,
    ) -> bytes:
        """Sign the document as PAdES B-B, or B-T with ``timestamper``, in a new revision, and return its bytes.

        ``field`` names an unsigned signature field, or a new invisible one
        on the first page. The signature dictionary, the field, and any
        unsaved edits are saved incrementally after the opened file's bytes,
        so earlier signatures stay intact, and the CMS signature of
        ``signer`` covers every byte but its own (ETSI EN 319 142-1). The
        signature dictionary's ``/M`` is the current time, and ``reason``,
        ``location``, and ``contact`` become ``/Reason``, ``/Location``, and
        ``/ContactInfo``.

        ``timestamper`` receives the digest of the signature value and the
        name of its hash algorithm, and returns an RFC 3161 time-stamp token
        over that digest; pyropdf makes no network access. A token that
        does not stamp the signature raises :class:`ValueError`.

        ``reserve`` is the bytes kept for the CMS signature, 16 KiB by
        default; a signature that needs more raises :class:`ValueError`.
        Afterwards the document reads the signed revision, so that
        ``save(path, incremental=True)`` writes the returned bytes, and its
        undo history starts anew. A failure leaves the document as it was.

        Signing runs as one transaction, which other threads' edits wait
        for. Signing inside a transaction raises :class:`PdfError`, as does
        a callback that edits the document. An encrypted document raises
        :class:`ValueError`.
        """
        return _sign(
            self,
            field,
            signer,
            timestamper=timestamper,
            reason=reason,
            location=location,
            contact=contact,
            reserve=reserve,
        )

    def render(  # noqa: PLR0913 - the rendering and batch options are all keyword-only
        self,
        pages: Iterable[int] | None = None,
        *,
        dpi: float | None = None,
        matrix: Sequence[float] | None = None,
        alpha: bool = False,
        background: tuple[int, int, int] | tuple[int, int, int, int] | None = None,
        annotations: bool = True,
        colorspace: Colorspace | Literal["gray", "rgb"] = Colorspace.RGB,
        format: Literal["png", "jpeg"] = "png",  # noqa: A002 - the public name.
        quality: int = 95,
        workers: int | None = None,
        max_size: int | None = _DEFAULT_MAX_RENDER_BATCH_SIZE,
    ) -> list[bytes]:
        """Render pages of the current version to encoded images, in the order of ``pages``.

        ``dpi``, ``matrix``, ``alpha``, ``background``, ``annotations``, and
        ``colorspace`` mean what they mean to :meth:`Page.pixmap`, so the
        default is opaque RGB at 72 dpi, and each image is what
        ``page.pixmap(...).to_bytes(format, quality=quality)`` returns.
        ``format`` is ``"png"`` or ``"jpeg"`` at ``quality``, 1 through 100;
        JPEG has no alpha. ``pages`` holds page indexes, negative ones from
        the end, and may repeat them; it defaults to every page, and may
        hold up to the larger of the page count and 4,096 entries, past
        which :class:`LimitError` code ``page_entry_count`` refuses it.

        Every page renders from the one version that is current when the
        call starts, so commits from other threads leave the batch as it
        was. The pages render on up to ``workers`` threads, 1 through 64,
        and up to four CPUs by default, with the GIL released; the threads
        are fewer when their largest rasters would hold more than about
        512 MB at once. ``max_size`` bounds the encoded bytes of all the
        images together, 512 MiB by default, with :class:`LimitError` code
        ``render_output_size``; ``None`` leaves it unbounded.
        """
        linear = _render_matrix(matrix, dpi)
        rgba = _render_background(background, alpha=alpha)
        _validate_bool("annotations", annotations)
        gray = _render_gray(colorspace)
        _validate_encoding(format, quality, alpha=alpha)
        if workers is None:
            workers = min(_DEFAULT_RENDER_WORKERS, os.cpu_count() or 1)
        elif isinstance(workers, bool) or not isinstance(workers, int):
            msg = f"workers must be an integer from 1 through {_MAX_RENDER_WORKERS}: {workers!r}"
            raise TypeError(msg)
        if not 1 <= workers <= _MAX_RENDER_WORKERS:
            msg = f"workers must be an integer from 1 through {_MAX_RENDER_WORKERS}: {workers!r}"
            raise ValueError(msg)
        _validate_optional_positive_int("max_size", max_size)
        doc = self._core()
        entries = None if pages is None else _page_entries(pages, _core.page_count(doc))
        return _core.render_pages(
            doc, entries, linear, alpha, gray, rgba, annotations, format, quality, workers, max_size
        )

    def text(  # noqa: PLR0913 - the text options are all keyword-only
        self,
        pages: Iterable[int] | None = None,
        *,
        clip: Sequence[float] | None = None,
        clipped: bool = False,
        ligatures: bool = True,
        whitespace: bool = True,
        dehyphenate: bool = False,
        unmapped: Unmapped = "skip",
        max_glyphs: int | None = _DEFAULT_MAX_TEXT_GLYPHS,
        max_size: int | None = _DEFAULT_MAX_TEXT_SIZE,
    ) -> str:
        """Return the text of the given pages of the current version, joined in their order.

        ``pages`` holds zero-based page indexes, negative ones counted from
        the end and repeats allowed; ``None`` means every page. It may hold
        up to the larger of the page count and 4,096 entries, past which
        :class:`LimitError` code ``page_entry_count`` refuses it. The text
        equals joining :meth:`Page.text` of each page with the same options,
        read from one version even while other threads edit the document.
        ``max_glyphs`` bounds each page and ``max_size`` the whole text. The
        GIL is released while hayro interprets the pages.
        """
        options = text_options(
            clip=None if clip is None else _clip_box(clip),
            clipped=clipped,
            ligatures=ligatures,
            whitespace=whitespace,
            dehyphenate=dehyphenate,
            unmapped=unmapped,
            max_glyphs=max_glyphs,
            max_size=max_size,
        )
        doc = self._core()
        entries = None if pages is None else _page_entries(pages, _core.page_count(doc))
        text, skipped = _core.document_text(doc, entries, options, max_glyphs, max_size)
        _warn_skipped(skipped)
        return text

    def markdown(
        self,
        pages: Iterable[int] | None = None,
        *,
        table_strategy: Literal["lines", "text"] | None = "lines",
        max_size: int | None = _markdown._DEFAULT_MAX_MARKDOWN_SIZE,  # noqa: SLF001
    ) -> str:
        """Return the given pages of the current version as Markdown, joined in their order.

        Headings come from font sizes: the size that holds the most text is
        the body's, and larger sizes map in descending order to ``#``
        through ``####``. Wrapped CJK lines join without spaces, leading
        bullets and ``1.`` or ``1)`` become Markdown lists, and the bold and
        italic of embedded fonts become emphasis. Multicolumn text follows
        its gutters, and vertical CJK columns read top to bottom and right to
        left; ruby and mixed orientations are not interpreted.

        Complete bordered tables of :meth:`Page.tables` become Markdown
        tables in reading order; ``table_strategy="text"`` adds the
        borderless tables that overlap none of them, and ``None`` converts no
        table. ``pages`` is as for :meth:`text`. More than ``max_size`` bytes
        of UTF-8 in all raise :class:`LimitError` with code
        ``markdown_output_size`` without a partial result;
        ``max_size=None`` lifts the limit.
        """
        _markdown.validate_options(table_strategy, max_size)
        count = _core.page_count(self._core())
        indexes = range(count) if pages is None else _page_entries(pages, count)
        return _markdown.pages_markdown(
            [self.pages[index] for index in indexes], table_strategy=table_strategy, max_size=max_size
        )

    def to_json(
        self,
        pages: Iterable[int] | None = None,
        *,
        table_strategy: Literal["lines", "text"] | None = "lines",
        max_size: int | None = _chunks._DEFAULT_MAX_EXPORT_SIZE,  # noqa: SLF001
    ) -> str:
        """Return the given pages of the current version as JSON: each page's size and its elements.

        The document is ``{"pages": [...]}``, each page ``{"index",
        "width", "height", "elements"}``, and each element as
        :meth:`Element.to_dict` gives it: a heading, paragraph, list item,
        table, or image in reading order with its page index and its box in
        display space. Headings, lists, and tables are found as
        :meth:`markdown` finds them, and ``pages`` and ``table_strategy``
        are as for it. More than ``max_size`` bytes of UTF-8 element text
        raise :class:`LimitError` with code ``export_output_size`` without a
        partial result; ``max_size=None`` lifts the limit.
        """
        _chunks.validate_options(table_strategy, max_size)
        count = _core.page_count(self._core())
        indexes = range(count) if pages is None else _page_entries(pages, count)
        return _chunks.export_json(
            [self.pages[index] for index in indexes], table_strategy=table_strategy, max_size=max_size
        )

    def to_chunks(
        self,
        pages: Iterable[int] | None = None,
        *,
        max_chars: int = _chunks._DEFAULT_MAX_CHARS,  # noqa: SLF001
        table_strategy: Literal["lines", "text"] | None = "lines",
        max_size: int | None = _chunks._DEFAULT_MAX_EXPORT_SIZE,  # noqa: SLF001
    ) -> list[Chunk]:
        """Return the elements of :meth:`to_json` grouped into retrieval chunks of at most ``max_chars`` characters.

        A chunk is a run of consecutive elements joined as Markdown, headings
        marked with ``#``; each heading starts a chunk, and
        :attr:`Chunk.section` names the headings above it. A paragraph or
        list item longer than ``max_chars`` is split at white space, its
        pieces keeping its page and box; a table, image, or heading is never
        split, and a chunk of one that is longer is
        :attr:`Chunk.oversized`. Every element keeps its page index and box,
        and :meth:`Chunk.to_dict` gives JSON-safe values. ``pages``,
        ``table_strategy``, and ``max_size`` are as for :meth:`to_json`.
        """
        _chunks.validate_max_chars(max_chars)
        _chunks.validate_options(table_strategy, max_size)
        count = _core.page_count(self._core())
        indexes = range(count) if pages is None else _page_entries(pages, count)
        return _chunks.export_chunks(
            [self.pages[index] for index in indexes],
            max_chars=max_chars,
            table_strategy=table_strategy,
            max_size=max_size,
        )

    def flatten(self, *, annotations: bool = True, widgets: bool = True) -> None:
        """Draw annotation and widget appearances into page content and take them off their pages.

        ``annotations`` covers every annotation but links, which stay, and
        takes popups off with them; ``widgets`` covers form widgets and
        removes the interactive form. Each one is drawn as rendering shows
        it: its normal appearance mapped onto its rectangle, or the
        appearance that rendering draws for one stored without, and hidden
        ones leave undrawn. A widget without an appearance draws nothing;
        fill fields first, which draws them. The edit is one step.
        """
        _validate_bool("annotations", annotations)
        _validate_bool("widgets", widgets)
        _core.flatten(self._core(), None, annotations, widgets)

    def sanitize(  # noqa: PLR0913 - one switch per kind
        self,
        path: str | os.PathLike[str],
        /,
        *,
        javascript: bool = True,
        actions: bool = True,
        attachments: bool = True,
        metadata: bool = True,
        comments: bool = True,
        hidden_text: bool = True,
        unused: bool = True,
    ) -> SanitizeReport:
        """Remove hidden or active content in one transaction and save the document in full to ``path``.

        ``javascript`` removes every action whose ``/Next`` chain runs
        JavaScript and the document's JavaScript name tree; ``actions``
        removes every action: ``/A``, ``/OpenAction``, and ``/AA``, while
        links keep their ``/Dest`` destinations. ``attachments`` removes
        the embedded file name tree, portfolios, ``/AF`` associations, and
        file attachment annotations. ``metadata`` removes the Info
        dictionary and every ``/Metadata`` and ``/PieceInfo``. ``comments``
        removes markup annotations and their pop-ups, keeping links and
        widgets. ``hidden_text`` removes text shown in rendering mode 3, as
        OCR layers are, from page content and from the form XObjects that
        pages and appearances paint, each with the mode it is painted in; a
        form painted both invisibly and visibly keeps the text that it
        does not hide itself. ``unused`` writes only the objects that the
        trailer reaches.

        The save is never incremental, so the file has one revision. The
        returned :class:`SanitizeReport` counts what each kind removed and
        reports, as :meth:`changes` does before the save, the signatures
        and profile claims that the result breaks, and the claims that it
        no longer makes, as removing the XMP metadata drops them. The
        document in memory
        keeps the edits, which :attr:`history` can undo.
        """
        self._ensure_open()
        return sanitize(
            self,
            path,
            javascript=javascript,
            actions=actions,
            attachments=attachments,
            metadata=metadata,
            comments=comments,
            hidden_text=hidden_text,
            unused=unused,
        )

    def subset_fonts(self) -> tuple[SubsetFont, ...]:
        """Subset each embedded TrueType, OpenType, and CFF program to the glyphs that the pages draw, as one step.

        The glyphs are those that rendering draws with every optional
        content group on, with the glyphs that they draw as composite
        components or ``seac`` accents. A subset keeps every glyph ID and
        empties the outlines of the others, so text, encodings, widths, and
        ToUnicode are unchanged; the font names gain a subset tag. Programs
        that annotation appearances or the form's default resources reach,
        and Type 1 and Type 3 fonts, stay whole. Returns the programs that
        shrank; :attr:`history` can undo the step.
        """
        self._ensure_open()
        return subset_fonts(self)

    def font_program(self, font: int | FontInfo) -> FontProgram | None:
        """Return the embedded program of a font, by its object number or a :class:`FontInfo` from ``page.fonts()``.

        The program is the decoded ``FontFile``, ``FontFile2``, or
        ``FontFile3`` stream of the font's descriptor, or of a ``Type0``
        font's descendant font; ``None`` when the font embeds none, as Type 3
        fonts and the standard 14 fonts do. A value that is not a font
        dictionary, or a :class:`FontInfo` of another document, raises
        :class:`ValueError`.
        """
        self._ensure_open()
        return font_program(self, font)

    def close(self) -> None:
        """Close the document and release its data; later operations raise :class:`DocumentClosedError`."""
        self._closed = True
        self._doc.close()

    def _ensure_open(self) -> None:
        """Refuse operations on a closed document."""
        if self._closed:
            msg = "document closed"
            raise DocumentClosedError(msg)

    def __enter__(self) -> Self:
        """Enter a context manager and return this document."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the document when leaving a context manager."""
        self.close()

    def __repr__(self) -> str:
        """Return a representation containing the open or closed state."""
        state = "closed " if self._closed else ""
        return f"<{state}pyropdf.Document>"


#: zlib's highest compression level, which ``compression_level`` takes.
_MAX_COMPRESSION_LEVEL = 9


def _validate_bool(name: str, value: object) -> None:
    """Refuse a value that is not a bool."""
    if not isinstance(value, bool):
        msg = f"{name} must be a bool: {value!r}"
        raise TypeError(msg)


def _save_options(  # noqa: PLR0913 - the save options are all keyword-only
    *,
    incremental: bool,
    prune: bool,
    compress: bool,
    compression_level: int,
    decompress: bool,
    keep_id: bool,
    cross_reference: object,
    object_streams: bool,
    renumber: bool,
    dedupe: bool,
) -> tuple[bool, int | None, bool, bool, bool | None, bool, bool, bool]:
    """Check the save options, and return them as the core's save methods take them.

    They are ``prune``, the ``compress`` level, ``decompress``, ``renew_id``,
    ``cross_reference_stream``, ``object_streams``, ``renumber``, and
    ``dedupe``.
    """
    flags = {
        "incremental": incremental,
        "prune": prune,
        "compress": compress,
        "decompress": decompress,
        "keep_id": keep_id,
        "object_streams": object_streams,
        "renumber": renumber,
        "dedupe": dedupe,
    }
    for name, value in flags.items():
        _validate_bool(name, value)
    if isinstance(compression_level, bool) or not isinstance(compression_level, int):
        msg = f"compression_level must be an integer from 0 through 9: {compression_level!r}"
        raise TypeError(msg)
    if not 0 <= compression_level <= _MAX_COMPRESSION_LEVEL:
        msg = f"compression_level must be from 0 through 9: {compression_level}"
        raise ValueError(msg)
    if incremental:
        _refuse_full_save_options(
            prune=prune,
            decompress=decompress,
            cross_reference=cross_reference,
            object_streams=object_streams,
            renumber=renumber,
            dedupe=dedupe,
        )
    if compress and decompress:
        msg = "decompress=True needs compress=False, which would compress the streams again"
        raise ValueError(msg)
    if cross_reference not in (None, "table", "stream"):
        msg = f"cross_reference must be 'table', 'stream', or None: {cross_reference!r}"
        raise ValueError(msg)
    if dedupe and not prune:
        msg = "dedupe=True needs prune=True, which leaves the merged objects out"
        raise ValueError(msg)
    if object_streams and cross_reference == "table":
        msg = "object_streams=True writes a cross-reference stream, so it needs cross_reference='stream' or None"
        raise ValueError(msg)
    stream = None if cross_reference is None else cross_reference == "stream"
    return (
        prune,
        compression_level if compress else None,
        decompress,
        not keep_id,
        stream,
        object_streams,
        renumber,
        dedupe,
    )


def _refuse_full_save_options(  # noqa: PLR0913 - the save options are all keyword-only
    *,
    prune: bool,
    decompress: bool,
    cross_reference: object,
    object_streams: bool,
    renumber: bool,
    dedupe: bool,
) -> None:
    """Raise :class:`ValueError` for an option that an incremental save refuses, as only a full save takes it."""
    kept = {
        "prune": (prune, "the objects"),
        "decompress": (decompress, "the streams"),
        "cross_reference and object_streams": (cross_reference is not None or object_streams, "the form"),
        "renumber": (renumber, "the numbers"),
        "dedupe": (dedupe, "the objects"),
    }
    for name, (given, what) in kept.items():
        if given:
            verb = "require" if " and " in name else "requires"
            msg = f"{name} {verb} a full save: an incremental save keeps {what} of the opened file"
            raise ValueError(msg)


def _resolve_limits(limits: object) -> DocumentLimits:
    """Return ``limits``, or the default limits for ``None``."""
    if limits is None:
        return DocumentLimits()
    if not isinstance(limits, DocumentLimits):
        msg = f"limits must be a DocumentLimits instance or None: {limits!r}"
        raise TypeError(msg)
    return limits


def _open_source(source: object) -> tuple[str | None, bytes | bytearray | memoryview | None]:
    """Split a source into a file path or a buffer holding the document."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        return None, source
    if isinstance(source, (str, os.PathLike)):
        path = os.fspath(source)
        if isinstance(path, str):
            return path, None
    if hasattr(source, "read"):
        msg = f"source must be a path or bytes; read the file object first: {type(source).__name__}"
        raise TypeError(msg)
    msg = f"source must be a path (str or os.PathLike[str]) or bytes, bytearray, or memoryview: {type(source).__name__}"
    raise TypeError(msg)


def open(  # noqa: A001 - the package's entry point, pyropdf.open.
    source: str | os.PathLike[str] | bytes | bytearray | memoryview,
    /,
    *,
    password: str | None = None,
    limits: DocumentLimits | None = None,
) -> Document:
    """Open a PDF from a file path or from the bytes of a document.

    A document with an empty user password decrypts on its own; any other
    encrypted document needs ``password``, or :meth:`Document.unlock` after
    opening, and refuses to read its objects until then. A wrong password
    raises :class:`PasswordError`. Passwords are capped at 127 UTF-8 bytes.

    Opening reads the cross-reference data only; ``bytes`` are read in place,
    without a copy, and other buffers are copied. ``limits`` bounds the work,
    and :meth:`DocumentLimits.web` also reads every object at open. Input that is not a readable PDF, including empty
    input, raises :class:`MalformedPdfError`, and a missing file raises
    :class:`FileNotFoundError`.
    """
    path, data = _open_source(source)
    secret = None if password is None else _password_bytes(password)
    resolved = _resolve_limits(limits)
    document = Document.__new__(Document)
    if data is not None:
        buffer = data
        document._setup(lambda lim: _Document.open_buffer(buffer, lim, secret), resolved)  # noqa: SLF001
    elif path is not None:
        file = path
        document._setup(lambda lim: _Document.open_path(file, lim, secret), resolved)  # noqa: SLF001
    return document


@dataclass(frozen=True, slots=True, kw_only=True)
class Probe:
    """What :func:`probe` reads without loading the whole document."""

    #: The number of pages, as the page tree's root counts them.
    page_count: int
    #: The PDF version of the header, such as ``"1.7"``.
    version: str
    #: Whether the document is encrypted and the password given, if any, did not unlock it.
    needs_password: bool
    #: Whether reading it needed cross-reference recovery.
    is_repaired: bool
    #: The Info dictionary's text entries that are present: ``title``,
    #: ``author``, ``subject``, ``keywords``, ``creator``, ``producer``,
    #: ``created``, and ``modified``, the last two as PDF date strings.
    #: A locked document reports none.
    metadata: Mapping[str, str]


def probe(
    source: str | os.PathLike[str] | bytes | bytearray | memoryview,
    /,
    *,
    password: str | None = None,
    max_file_size: int | None = None,
) -> Probe:
    """Read the page count, version, encryption, and metadata without loading the whole document.

    This suits scanning many files. ``max_file_size`` refuses larger input
    before parsing with :class:`LimitError` code ``file_size``; ``None``, the
    default, leaves it unbounded. The metadata text is capped at 1 MiB, and
    passwords at 127 UTF-8 bytes; a wrong password raises :class:`PasswordError`.
    """
    path, data = _open_source(source)
    _validate_optional_positive_int("max_file_size", max_file_size)
    secret = None if password is None else _password_bytes(password)
    page_count, version, encrypted, repaired, raw = _probe(path, data, max_file_size, secret)
    return Probe(
        page_count=page_count,
        version=version,
        needs_password=encrypted,
        is_repaired=repaired,
        metadata=MappingProxyType({name: raw[key] for name, key in _PROBE_METADATA_KEYS if key in raw}),
    )


def _temporary_sibling_path(target: Path) -> Path:
    """Create one same-directory output path with normal umask permissions."""
    for _ in range(_TEMPORARY_FILE_ATTEMPTS):
        candidate = target.parent / f".pyropdf-{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        except FileExistsError:
            continue
        os.close(descriptor)
        return candidate
    msg = f"failed to create a unique temporary output beside {target}"
    raise FileExistsError(msg)


def _save_target_path(requested: Path) -> Path:
    """Return the file that a save replaces: the target of a final symlink, with Windows long paths supported.

    On Windows the path takes the verbatim prefix, after lexical
    normalization, which the prefix does not do, so that paths beyond 260
    characters save. Errors keep naming the caller's path.
    """
    target = requested
    if sys.platform == "win32":
        absolute = os.path.abspath(requested)  # noqa: PTH100 - lexical normalization must precede the prefix.
        if not absolute.startswith(("\\\\?\\", "\\\\.\\")):
            absolute = "\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute
        target = Path(absolute)
    return target.resolve(strict=False) if target.is_symlink() else target


def _copy_access_control(target: Path, temporary: Path, requested: Path) -> None:
    """Give a Windows sibling the target's access control list before any PDF data is written."""
    try:
        _core._copy_windows_dacl(target, temporary)  # noqa: SLF001
    except OSError as exc:
        msg = f"failed to save {requested}: {exc}"
        raise PdfError(msg) from exc


def _remove_temporary_output(temporary: Path) -> None:
    """Remove a temporary output, best effort, including a Windows file made read-only."""
    if sys.platform == "win32":
        with suppress(OSError):
            temporary.chmod(stat.S_IWRITE)
    with suppress(OSError):
        temporary.unlink()


def _atomic_save_file(filename: str | os.PathLike[str], writer: Callable[[str], None]) -> None:
    """Write a sibling file completely before atomically replacing the target."""
    requested = Path(filename)
    try:
        target = _save_target_path(requested)
        try:
            target_metadata = target.stat()
        except FileNotFoundError:
            target_mode = None
        else:
            target_mode = stat.S_IMODE(target_metadata.st_mode) if stat.S_ISREG(target_metadata.st_mode) else None
        temporary = _temporary_sibling_path(target)
    except (OSError, RuntimeError) as exc:
        msg = f"failed to save {requested}: {exc}"
        raise PdfError(msg) from exc
    replaced = False
    try:
        if sys.platform == "win32" and target_mode is not None:
            _copy_access_control(target, temporary, requested)
        try:
            writer(str(temporary))
        except PdfError as exc:
            # I/O errors name the output; name the caller's path instead.
            if str(temporary) not in str(exc):
                raise
            message = str(exc).replace(str(temporary), str(requested))
            raise PdfError(message) from exc
        try:
            if target_mode is not None:
                temporary.chmod(target_mode)
            temporary.replace(target)
        except OSError as exc:
            msg = f"failed to save {requested}: {exc}"
            raise PdfError(msg) from exc
        replaced = True
    finally:
        if not replaced:
            _remove_temporary_output(temporary)


__all__ = ["Document", "Probe", "open", "probe"]
