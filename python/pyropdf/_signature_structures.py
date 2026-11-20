"""Signature structures beyond signing itself, and PDF 2.0 encrypted-payload wrappers.

DocMDP and FieldMDP permissions (ISO 32000-2 §12.8.2, §12.7.5.5), the Document
Security Store (``/DSS`` and ``/VRI``, §12.8.4.3), document timestamps
(``/DocTimeStamp``, §12.8.5), permissions dictionaries (``/Perms``, §12.8.6),
legal content attestations (``/Legal``, §12.8.7), and PDF 2.0 unencrypted
wrapper documents (``EncryptedPayload``, §7.6.7).
"""

from __future__ import annotations

import enum
import hashlib
import re
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import KW_ONLY, dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from pyropdf import _core
from pyropdf._attachments import _DEFAULT_MAX_EMBEDDED_FILE_SIZE, _MAX_TREE_ENTRIES, _entries, _file, _text, _version
from pyropdf._collections import (
    _INT64_MAX,
    _check_text_budget,
    _indirect_xref,
    _is_dict,
    _is_non_bool_int,
    _is_pdf_date,
    _require_collection_version,
    _validate_date_input,
    _validate_pdf_name,
)
from pyropdf._errors import LimitError, MalformedPdfError, PdfError
from pyropdf._fonts import Font
from pyropdf._metadata import _require_version, pdf_date
from pyropdf._objects import (
    _DEFAULT_MAX_STREAM_DATA_SIZE,
    Array,
    Dictionary,
    Name,
    Stream,
    String,
    _catalog,
    _encode,
)

if TYPE_CHECKING:
    from pyropdf._document import Document
    from pyropdf._form import Field

#: Maximum field names in a ``FieldLock``, streams in a ``DSS`` array, or keys in ``/VRI``.
_MAX_LOCK_FIELDS = 4096
_MAX_DSS_ENTRIES = 4096
_MAX_SIG_REFS = 64
#: Uppercase 40-character SHA-1 hexadecimal key required by ``VRIMap`` (ISO 32000-2 Table 261).
_VRI_KEY_RE = re.compile(r"[0-9A-F]{40}")
#: Invisible widget flags: Print (4) | Locked (128).
_INVISIBLE_WIDGET_FLAGS = 132
_SIG_EXIST_FLAG = 1
_MAX_RESERVE = 1 << 20

_ID_SIGNED_DATA = "1.2.840.113549.1.7.2"
_ID_CT_TST_INFO = "1.2.840.113549.1.9.16.1.4"
_DIGEST_OIDS: Mapping[str, str] = {
    "1.3.14.3.2.26": "sha1",
    "2.16.840.1.101.3.4.2.1": "sha256",
    "2.16.840.1.101.3.4.2.2": "sha384",
    "2.16.840.1.101.3.4.2.3": "sha512",
}
_DER_SEQUENCE = 0x30
_DER_OCTET_STRING = 0x04
_DER_OID = 0x06
_DER_GENERALIZED_TIME = 0x18
_DER_CONTEXT_0 = 0xA0
_DER_LONG_FORM = 0x80
_MAX_DER_LEN_OCTETS = 4
_MAX_DER_ITEMS = 32
_MAX_OID_BYTES = 64
_GEN_TIME_DIGITS = 14
_MIN_TST_FIELDS = 5
_MIN_CI_FIELDS = 2

#: Mapping between ``LegalAttestation`` dataclass attributes and ``LegalAttestation.tsv`` PDF keys.
_LEGAL_COUNT_FIELDS: tuple[tuple[str, str], ...] = (
    ("javascript_actions", "JavaScriptActions"),
    ("launch_actions", "LaunchActions"),
    ("uri_actions", "URIActions"),
    ("movie_actions", "MovieActions"),
    ("sound_actions", "SoundActions"),
    ("hide_annotation_actions", "HideAnnotationActions"),
    ("goto_remote_actions", "GoToRemoteActions"),
    ("alternate_images", "AlternateImages"),
    ("external_streams", "ExternalStreams"),
    ("truetype_fonts", "TrueTypeFonts"),
    ("external_ref_xobjects", "ExternalRefXobjects"),
    ("external_opi_dicts", "ExternalOPIdicts"),
    ("non_embedded_fonts", "NonEmbeddedFonts"),
    ("dev_dep_gs_op", "DevDepGS_OP"),
    ("dev_dep_gs_ht", "DevDepGS_HT"),
    ("dev_dep_gs_tr", "DevDepGS_TR"),
    ("dev_dep_gs_ucr", "DevDepGS_UCR"),
    ("dev_dep_gs_bg", "DevDepGS_BG"),
    ("dev_dep_gs_fl", "DevDepGS_FL"),
    ("annotations", "Annotations"),
)
_LEGAL_ALLOWED_KEYS = frozenset({pdf_key for _, pdf_key in _LEGAL_COUNT_FIELDS} | {"OptionalContent", "Attestation"})


class DocMdpPermission(enum.IntEnum):
    """Changes permitted after a DocMDP certification signature (``/P``, ISO 32000-2 Table 257)."""

    NO_CHANGES = 1
    """No changes to the document are permitted (``1``)."""
    FORM_FILLING_AND_SIGNATURES = 2
    """Filling in forms, instantiating page templates, and signing are permitted (``2``, the default)."""
    ANNOTATIONS_FORM_FILLING_AND_SIGNATURES = 3
    """Annotation creation, deletion, and modification are also permitted (``3``)."""


class FieldMdpAction(enum.StrEnum):
    """Which form fields a :class:`FieldLock` locks after signing (``/Action``, ISO 32000-2 Table 236)."""

    ALL = "All"
    """Lock all form fields in the document."""
    INCLUDE = "Include"
    """Lock only the fields named in :attr:`FieldLock.fields`."""
    EXCLUDE = "Exclude"
    """Lock all form fields except those named in :attr:`FieldLock.fields`."""


def _validate_doc_mdp(value: object, label: str = "doc_mdp") -> DocMdpPermission:
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{label} must be a DocMdpPermission (1, 2, or 3): {value!r}"
        raise TypeError(msg)
    return DocMdpPermission(int(value))


def _validate_lock_fields(action: FieldMdpAction, fields: object) -> tuple[str, ...]:
    if isinstance(fields, (str, bytes, bytearray, memoryview)) or not isinstance(fields, Iterable):
        msg = f"fields must be a sequence of field names: {fields!r}"
        raise TypeError(msg)
    names: list[str] = []
    for item in fields:
        if not isinstance(item, str):
            msg = f"field name in FieldLock must be a str: {item!r}"
            raise TypeError(msg)
        plain = str.__str__(item)
        if not plain:
            msg = "field name in FieldLock must not be empty"
            raise ValueError(msg)
        if len(names) >= _MAX_LOCK_FIELDS:
            msg = f"FieldLock holds at most {_MAX_LOCK_FIELDS} field names"
            raise ValueError(msg)
        names.append(plain)
    _check_text_budget(names, "FieldLock fields")
    if action is FieldMdpAction.ALL and names:
        msg = "FieldLock with FieldMdpAction.ALL cannot name specific fields"
        raise ValueError(msg)
    if action in (FieldMdpAction.INCLUDE, FieldMdpAction.EXCLUDE) and not names:
        msg = f"FieldLock with FieldMdpAction.{action.name} requires at least one field name"
        raise ValueError(msg)
    return tuple(names)


@dataclass(frozen=True, slots=True, init=False)
class FieldLock:
    """Form-field locking specification for ``Field.lock`` (``SigFieldLock``) and ``FieldMDP`` transforms.

    ``FieldMdpAction.ALL`` locks every field and forbids ``fields``;
    ``INCLUDE`` and ``EXCLUDE`` require one to 4,096 non-empty field names.
    """

    action: FieldMdpAction
    fields: tuple[str, ...]

    def __init__(
        self,
        action: FieldMdpAction = FieldMdpAction.ALL,
        fields: Sequence[str] = (),
    ) -> None:
        """Validate ``action`` and ``fields``."""
        if not isinstance(action, str):
            msg = f"action must be a FieldMdpAction or str: {action!r}"
            raise TypeError(msg)
        checked_action = FieldMdpAction(action)
        checked_fields = _validate_lock_fields(checked_action, fields)
        object.__setattr__(self, "action", checked_action)
        object.__setattr__(self, "fields", checked_fields)


def _buffer_bytes(value: object, label: str, max_size: int | None = None, *, embedded_file: bool = False) -> bytes:
    """Copy ``value`` via the buffer protocol, checking ``max_size`` first."""
    try:
        view = memoryview(value)  # ty: ignore[invalid-argument-type]
    except TypeError:
        msg = f"{label} must be a bytes-like object: {type(value).__name__}"
        raise TypeError(msg) from None
    with view:
        if max_size is not None and view.nbytes > max_size:
            msg = f"{label} is {view.nbytes} bytes, exceeding the {max_size}-byte limit"
            if embedded_file:
                code = "embedded_file_size"
                raise LimitError(code, msg)
            code = "stream_data_size"
            raise LimitError(code, msg)
        return view.tobytes()


def vri_key(signature_contents: bytes) -> str:
    """Return the 40-character uppercase SHA-1 hexadecimal ``/VRI`` key for ``signature_contents``.

    ISO 32000-2 Table 261 defines each key of ``/DSS /VRI`` as the uppercase
    hexadecimal SHA-1 digest of the raw byte string in a signature's
    ``/Contents`` entry (for example ``bytes(sig_dict["Contents"])``).
    """
    try:
        view = memoryview(signature_contents)
    except TypeError:
        msg = f"signature_contents must be a bytes-like object: {type(signature_contents).__name__}"
        raise TypeError(msg) from None
    with view:
        return hashlib.sha1(view).hexdigest().upper()  # noqa: S324 - ISO 32000-2 Table 261 mandates SHA-1 for VRI keys.


def _validate_blob_sequence(items: object, label: str, budget: list[int]) -> tuple[bytes, ...]:
    if isinstance(items, (str, bytes, bytearray, memoryview)) or not isinstance(items, Iterable):
        msg = f"{label} must be an iterable of DER byte strings, not {type(items).__name__}"
        raise TypeError(msg)
    out: list[bytes] = []
    for item in items:
        if len(out) >= _MAX_DSS_ENTRIES:
            msg = f"{label} holds at most {_MAX_DSS_ENTRIES} entries"
            raise ValueError(msg)
        raw = _buffer_bytes(item, f"an entry of {label}", _DEFAULT_MAX_STREAM_DATA_SIZE - budget[0])
        budget[0] += len(raw)
        out.append(raw)
    return tuple(out)


@dataclass(frozen=True, slots=True, init=False)
class ValidationInfo:
    """Validation-related information for one signature in ``/DSS /VRI`` (ISO 32000-2 Table 262).

    ``timestamp`` (``/TS``) and ``created`` (``/TU``) are mutually exclusive per
    ETSI EN 319 142-1 and the Arlington PDF Model (``VRI.tsv``).
    """

    certificates: tuple[bytes, ...]
    ocsps: tuple[bytes, ...]
    crls: tuple[bytes, ...]
    timestamp: bytes | None
    created: str | None

    def __init__(
        self,
        *,
        certificates: Iterable[bytes] = (),
        ocsps: Iterable[bytes] = (),
        crls: Iterable[bytes] = (),
        timestamp: bytes | None = None,
        created: str | datetime | None = None,
    ) -> None:
        """Validate DER validation streams and mutual exclusion of ``timestamp`` and ``created``."""
        budget = [0]
        checked_certs = _validate_blob_sequence(certificates, "certificates", budget)
        checked_ocsps = _validate_blob_sequence(ocsps, "ocsps", budget)
        checked_crls = _validate_blob_sequence(crls, "crls", budget)
        checked_ts: bytes | None = None
        if timestamp is not None:
            checked_ts = _buffer_bytes(timestamp, "timestamp", _DEFAULT_MAX_STREAM_DATA_SIZE - budget[0])
            budget[0] += len(checked_ts)
        checked_created = None if created is None else _validate_date_input(created, "created")
        if checked_ts is not None and checked_created is not None:
            msg = "ValidationInfo cannot set both timestamp (/TS) and created (/TU)"
            raise ValueError(msg)
        object.__setattr__(self, "certificates", checked_certs)
        object.__setattr__(self, "ocsps", checked_ocsps)
        object.__setattr__(self, "crls", checked_crls)
        object.__setattr__(self, "timestamp", checked_ts)
        object.__setattr__(self, "created", checked_created)


def _validate_vri_map(
    vri: Mapping[str, ValidationInfo] | Iterable[tuple[str, ValidationInfo]],
    budget: list[int],
) -> dict[str, ValidationInfo]:
    if isinstance(vri, (str, bytes, bytearray, memoryview)):
        msg = "vri must be a mapping or iterable of (key, ValidationInfo) pairs"
        raise TypeError(msg)
    pairs = vri.items() if isinstance(vri, Mapping) else vri
    checked: dict[str, ValidationInfo] = {}
    for pair in pairs:
        if isinstance(pair, (str, bytes)) or not isinstance(pair, tuple) or len(pair) != 2:  # noqa: PLR2004
            msg = f"vri entry must be a (key, ValidationInfo) pair: {pair!r}"
            raise TypeError(msg)
        raw_key, info = pair
        if not isinstance(raw_key, str):
            msg = f"vri key must be a 40-character uppercase hex str: {raw_key!r}"
            raise TypeError(msg)
        key = str.__str__(raw_key)
        if _VRI_KEY_RE.fullmatch(key) is None:
            msg = f"vri key must be 40 uppercase hexadecimal characters ([0-9A-F]{{40}}): {raw_key!r}"
            raise ValueError(msg)
        if not isinstance(info, ValidationInfo):
            msg = f"vri[{key!r}] must be a ValidationInfo: {info!r}"
            raise TypeError(msg)
        if len(checked) >= _MAX_DSS_ENTRIES and key not in checked:
            msg = f"vri holds at most {_MAX_DSS_ENTRIES} entries"
            raise ValueError(msg)
        entry_bytes = (
            sum(len(b) for b in info.certificates)
            + sum(len(b) for b in info.ocsps)
            + sum(len(b) for b in info.crls)
            + (0 if info.timestamp is None else len(info.timestamp))
        )
        if budget[0] + entry_bytes > _DEFAULT_MAX_STREAM_DATA_SIZE:
            code = "stream_data_size"
            msg = f"SecurityStore exceeds the {_DEFAULT_MAX_STREAM_DATA_SIZE}-byte safety limit"
            raise LimitError(code, msg)
        budget[0] += entry_bytes
        checked[key] = info
    return checked


@dataclass(frozen=True, slots=True, init=False)
class SecurityStore:
    """The Document Security Store ``Catalog['DSS']`` for long-term signature validation (ISO 32000-2 Table 261).

    Holds DER-encoded X.509 ``certificates`` (``/Certs``), OCSP responses
    ``ocsps`` (``/OCSPs``), CRLs ``crls`` (``/CRLs``), and per-signature
    :class:`ValidationInfo` entries keyed by :func:`vri_key` in ``vri`` (``/VRI``).
    Writing deduplicates identical DER byte strings into shared indirect Flate
    streams. An older document is promoted to PDF 2.0 in the same transaction.
    If that introduces a structural defect, assignment raises :class:`PdfError`.
    """

    certificates: tuple[bytes, ...]
    ocsps: tuple[bytes, ...]
    crls: tuple[bytes, ...]
    vri: Mapping[str, ValidationInfo]

    def __init__(
        self,
        *,
        certificates: Iterable[bytes] = (),
        ocsps: Iterable[bytes] = (),
        crls: Iterable[bytes] = (),
        vri: Mapping[str, ValidationInfo] | Iterable[tuple[str, ValidationInfo]] = (),
    ) -> None:
        """Validate DER streams and ``/VRI`` entries."""
        budget = [0]
        checked_certs = _validate_blob_sequence(certificates, "certificates", budget)
        checked_ocsps = _validate_blob_sequence(ocsps, "ocsps", budget)
        checked_crls = _validate_blob_sequence(crls, "crls", budget)
        checked_vri = _validate_vri_map(vri, budget)
        object.__setattr__(self, "certificates", checked_certs)
        object.__setattr__(self, "ocsps", checked_ocsps)
        object.__setattr__(self, "crls", checked_crls)
        object.__setattr__(self, "vri", MappingProxyType(checked_vri))


@dataclass(frozen=True, slots=True, kw_only=True)
class LegalAttestation:
    """Legal content attestation dictionary on ``Catalog['Legal']`` (ISO 32000-2 Table 264, PDF 1.5).

    Each count is a non-negative integer or ``None`` when omitted; ``attestation``
    holds explanatory text and ``optional_content`` whether optional content is present.
    """

    javascript_actions: int | None = None
    launch_actions: int | None = None
    uri_actions: int | None = None
    movie_actions: int | None = None
    sound_actions: int | None = None
    hide_annotation_actions: int | None = None
    goto_remote_actions: int | None = None
    alternate_images: int | None = None
    external_streams: int | None = None
    truetype_fonts: int | None = None
    external_ref_xobjects: int | None = None
    external_opi_dicts: int | None = None
    non_embedded_fonts: int | None = None
    dev_dep_gs_op: int | None = None
    dev_dep_gs_ht: int | None = None
    dev_dep_gs_tr: int | None = None
    dev_dep_gs_ucr: int | None = None
    dev_dep_gs_bg: int | None = None
    dev_dep_gs_fl: int | None = None
    annotations: int | None = None
    optional_content: bool | None = None
    attestation: str | None = None

    def __post_init__(self) -> None:
        """Validate non-negative integer counts, ``optional_content``, and ``attestation``."""
        for attr, _ in _LEGAL_COUNT_FIELDS:
            val = getattr(self, attr)
            if val is None:
                continue
            if not _is_non_bool_int(val):
                msg = f"{attr} must be a non-negative int or None: {val!r}"
                raise TypeError(msg)
            checked = int(val)
            if not 0 <= checked <= _INT64_MAX:
                msg = f"{attr} must be a non-negative 64-bit integer: {checked}"
                raise ValueError(msg)
            object.__setattr__(self, attr, checked)
        if self.optional_content is not None and not isinstance(self.optional_content, bool):
            msg = f"optional_content must be a bool or None: {self.optional_content!r}"
            raise TypeError(msg)
        if self.attestation is not None:
            if not isinstance(self.attestation, str):
                msg = f"attestation must be a str or None: {self.attestation!r}"
                raise TypeError(msg)
            plain = str.__str__(self.attestation)
            _check_text_budget((plain,), "legal attestation")
            object.__setattr__(self, "attestation", plain)


@dataclass(frozen=True, slots=True)
class EncryptedPayload:
    """An encrypted PDF payload carried by a PDF 2.0 unencrypted wrapper document (ISO 32000-2 §7.6.7).

    ``data`` holds the encrypted payload bytes (capped at 64 MiB), ``subtype``
    names the cryptographic filter (``/Subtype``, such as ``"adbe.pkcs7.s5"``),
    ``filename`` names the embedded file, ``version`` sets the optional PDF name
    ``/Version``, and ``description`` sets ``/Desc`` on the file specification.
    """

    data: bytes
    _: KW_ONLY
    subtype: str = "adbe.pkcs7.s5"
    filename: str = "encrypted.pdf"
    version: str | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        """Validate payload bytes, cryptographic ``subtype``, ``filename``, ``version``, and ``description``."""
        raw = _buffer_bytes(self.data, "data", _DEFAULT_MAX_EMBEDDED_FILE_SIZE, embedded_file=True)
        if not raw:
            msg = "encrypted payload data must not be empty"
            raise ValueError(msg)
        checked_subtype = _validate_pdf_name(self.subtype, "subtype")
        if not isinstance(self.filename, str):
            msg = f"filename must be a str: {self.filename!r}"
            raise TypeError(msg)
        plain_filename = str.__str__(self.filename)
        if not plain_filename:
            msg = "filename must not be empty"
            raise ValueError(msg)
        checked_version = None if self.version is None else _validate_pdf_name(self.version, "version")
        checked_desc: str | None = None
        if self.description is not None:
            if not isinstance(self.description, str):
                msg = f"description must be a str or None: {self.description!r}"
                raise TypeError(msg)
            checked_desc = str.__str__(self.description)
        texts = [plain_filename] if checked_desc is None else [plain_filename, checked_desc]
        _check_text_budget(texts, "encrypted payload metadata", code="embedded_file_input_size")
        object.__setattr__(self, "data", raw)
        object.__setattr__(self, "subtype", checked_subtype)
        object.__setattr__(self, "filename", plain_filename)
        object.__setattr__(self, "version", checked_version)
        object.__setattr__(self, "description", checked_desc)


def _complete_pdf20_standard_fonts(doc: Document, page_obj: Dictionary) -> None:
    """Populate PDF 2.0 required metrics on Standard 14 Type 1 fonts written on ``page_obj``."""
    resources = page_obj.get("Resources")
    fonts = resources.get("Font") if isinstance(resources, Dictionary) else None
    if not isinstance(fonts, Dictionary):
        return
    helvetica = Font.standard("Helvetica")
    widths: list[int] = []
    for code in range(32, 256):
        ch = bytes([code]).decode("cp1252", errors="ignore")
        try:
            widths.append(round(helvetica.text_width(ch, 1000)) if ch else 278)
        except PdfError:
            widths.append(278)
    for key in list(fonts):
        font_dict = fonts.get(key)
        if not isinstance(font_dict, Dictionary) or font_dict.get("Subtype") != "Type1":
            continue
        if "FontDescriptor" in font_dict:
            continue
        base_font = str(font_dict.get("BaseFont") or "Helvetica")
        fd_xref = doc.objects.add(
            {
                "Type": Name("FontDescriptor"),
                "FontName": Name(base_font),
                "Flags": 32,
                "FontBBox": [-166, -225, 1000, 931],
                "ItalicAngle": 0,
                "Ascent": 718,
                "Descent": -207,
                "CapHeight": 718,
                "StemV": 88,
            }
        )
        font_dict["FirstChar"] = 32
        font_dict["LastChar"] = 255
        font_dict["Widths"] = widths
        font_dict["FontDescriptor"] = doc.objects[fd_xref]


def wrap_encrypted_payload(
    payload: EncryptedPayload,
    *,
    cover_text: str = "This PDF document contains an encrypted payload.",
) -> Document:
    """Create a PDF 2.0 unencrypted wrapper document carrying ``payload`` (ISO 32000-2 §7.6.7).

    Draws ``cover_text`` on a single A4 cover page, embeds ``payload.data`` with
    ``/Subtype /application#2Fpdf``, ``/AFRelationship /EncryptedPayload``, and
    ``/EP``, lists the file specification in ``Catalog['AF']``, and configures
    ``Catalog['Collection']`` with ``/View /H`` and an opaque name-tree key for
    ``/D``. The file specification holds the display filename. The tree key is
    also available as :attr:`Collection.initial_document`.
    """
    from pyropdf._document import Document  # noqa: PLC0415 - _document imports _signature_structures.

    if not isinstance(payload, EncryptedPayload):
        msg = f"payload must be an EncryptedPayload: {type(payload).__name__}"
        raise TypeError(msg)
    if not isinstance(cover_text, str):
        msg = f"cover_text must be a str: {type(cover_text).__name__}"
        raise TypeError(msg)
    plain_cover = str.__str__(cover_text)
    if not plain_cover:
        msg = "cover_text must not be empty"
        raise ValueError(msg)
    _check_text_budget((plain_cover,), "cover_text")

    doc = Document()
    page = doc.pages.new((595, 842))
    with page.draw() as canvas:
        canvas.textbox((72, 72, 523, 770), plain_cover, size=12)

    with doc.transaction("encrypted payload wrapper"):
        catalog = _catalog(doc)
        _require_collection_version(doc, catalog, (2, 0))
        _complete_pdf20_standard_fonts(doc, page.obj)
        stream_dict: dict[str, object] = {
            "Type": Name("EmbeddedFile"),
            "Subtype": Name("application/pdf"),
            "Params": {
                "Size": len(payload.data),
                "ModDate": pdf_date(datetime.now(UTC)),
            },
        }
        stream = doc.objects[doc.objects.add_stream(payload.data, stream_dict)]
        ep_dict: dict[str, object] = {
            "Type": Name("EncryptedPayload"),
            "Subtype": Name(payload.subtype),
        }
        if payload.version is not None:
            ep_dict["Version"] = Name(payload.version)
        filespec_dict: dict[str, object] = {
            "Type": Name("Filespec"),
            "F": payload.filename,
            "UF": payload.filename,
            "EF": {"F": stream, "UF": stream},
            "AFRelationship": Name("EncryptedPayload"),
            "EP": ep_dict,
        }
        if payload.description is not None:
            filespec_dict["Desc"] = payload.description
        filespec = doc.objects[doc.objects.add(filespec_dict)]
        # The outer tree key is an identifier independent of the embedded filename (Table 33).
        raw_key = uuid4().hex.encode("ascii")
        encoded_spec = _encode(doc, filespec, 1)
        _core.embedded_files_insert(doc._core(), raw_key, encoded_spec, [(raw_key, encoded_spec)])  # noqa: SLF001
        catalog["AF"] = [filespec]
        col_xref = doc.objects.add(
            {
                "Type": Name("Collection"),
                "View": Name("H"),
                "D": String(raw_key),
            }
        )
        catalog["Collection"] = doc.objects[col_xref]
    return doc


def _contains_associated_file(catalog: Dictionary, xref: int) -> bool:
    """Check catalog membership using the exact indirect file specification."""
    associated = catalog.get("AF")
    if not isinstance(associated, Array):
        return False
    if len(associated) > _MAX_TREE_ENTRIES:
        code = "embedded_file_input_size"
        msg = f"wrapper /AF exceeds the {_MAX_TREE_ENTRIES}-entry limit"
        raise LimitError(code, msg)
    return any(_indirect_xref(item) == xref for item in associated)


def _wrapper_target_filespec(document: Document) -> tuple[str, Dictionary, Dictionary] | None:
    catalog = _catalog(document)
    raw_col = catalog.get("Collection")
    if not _is_dict(raw_col) or document.encryption_method is not None or _version(document) < (2, 0):
        return None
    col = cast("Dictionary", raw_col)
    key = col.get("D")
    if col.get("View") != "H" or not isinstance(key, String) or not key:
        return None
    # Name-tree keys are byte strings. Different encodings can decode to the same text.
    filespec = next((entry.filespec for entry in _entries(document) if entry.key == key), None)
    if (
        not isinstance(filespec, Dictionary)
        or isinstance(filespec, Stream)
        or filespec.xref is None
        or filespec.get("AFRelationship") != "EncryptedPayload"
        or not _contains_associated_file(catalog, filespec.xref)
    ):
        return None
    raw_ep = filespec.get("EP")
    if not _is_dict(raw_ep):
        return None
    ep = cast("Dictionary", raw_ep)
    if ep.get("Type") not in (None, "EncryptedPayload"):
        return None
    return key.text, filespec, ep


def read_encrypted_payload(document: Document) -> EncryptedPayload | None:
    """Read a PDF 2.0 unencrypted wrapper document's :class:`EncryptedPayload`, or ``None``."""
    found = _wrapper_target_filespec(document)
    if found is None:
        return None
    target_name, filespec, ep = found
    raw_subtype = ep.get("Subtype")
    if not isinstance(raw_subtype, Name) or not str(raw_subtype):
        msg = "EncryptedPayload dictionary is missing a valid /Subtype name"
        raise MalformedPdfError(msg)
    raw_version = ep.get("Version")
    if raw_version is not None and (not isinstance(raw_version, Name) or not raw_version):
        msg = "EncryptedPayload /Version must be a nonempty name"
        raise MalformedPdfError(msg)
    version = str(raw_version) if raw_version is not None else None
    stream = _file(filespec)
    if stream is None:
        msg = "encrypted payload file specification has no embedded file stream"
        raise MalformedPdfError(msg)
    try:
        data = stream.read(max_size=_DEFAULT_MAX_EMBEDDED_FILE_SIZE)
    except LimitError as error:
        if error.code == "stream_data_size":
            code = "embedded_file_size"
            msg = f"encrypted payload exceeds the {_DEFAULT_MAX_EMBEDDED_FILE_SIZE}-byte limit"
            raise LimitError(code, msg) from error
        raise
    if not data:
        msg = "encrypted payload stream is empty"
        raise MalformedPdfError(msg)
    filename = _text(filespec.get("UF")) or _text(filespec.get("F")) or target_name
    description = _text(filespec.get("Desc")) or None
    return EncryptedPayload(
        data,
        subtype=str(raw_subtype),
        filename=filename,
        version=version,
        description=description,
    )


def _signed_file_xrefs(document: Document) -> set[int]:
    """Return indirect object numbers listed in the opened file when signatures are present."""
    if not document.signatures:
        return set()
    found = _core.document_file(document._core())  # noqa: SLF001
    if found is None:
        return set()
    return {number for _, numbers in found[2] for number in numbers}


def _parse_field_lock_dict(raw: object) -> FieldLock | None:
    """Parse a ``SigFieldLock`` or ``FieldMDPTransformParameters`` dictionary into a :class:`FieldLock`."""
    if not _is_dict(raw):
        return None
    lock_dict = cast("Dictionary", raw)
    raw_action = lock_dict.get("Action")
    try:
        action = FieldMdpAction(str(raw_action)) if isinstance(raw_action, Name) else None
    except ValueError:
        action = None
    if action is None:
        return None
    if action is FieldMdpAction.ALL:
        return FieldLock(FieldMdpAction.ALL)
    raw_fields = lock_dict.get("Fields")
    if not isinstance(raw_fields, Array) or len(raw_fields) == 0:
        return None
    if len(raw_fields) > _MAX_LOCK_FIELDS:
        code = "object_count"
        msg = f"field lock /Fields exceeds the {_MAX_LOCK_FIELDS}-entry safety limit"
        raise LimitError(code, msg)
    if any(not isinstance(item, String) or not item.text for item in raw_fields):
        return None
    return FieldLock(action, [cast("String", item).text for item in raw_fields])


def _sig_field_lock_entries(lock: FieldLock, doc_mdp: DocMdpPermission | None = None) -> dict[str, object]:
    entries: dict[str, object] = {
        "Type": Name("SigFieldLock"),
        "Action": Name(lock.action.value),
    }
    if lock.fields:
        entries["Fields"] = list(lock.fields)
    if doc_mdp is not None:
        entries["P"] = int(doc_mdp)
    return entries


def read_field_lock(field: Field) -> FieldLock | None:
    """Read ``field.obj['Lock']`` on a signature field, or ``None``."""
    from pyropdf._form import FieldType  # noqa: PLC0415

    if field.type is not FieldType.SIGNATURE:
        return None
    return _parse_field_lock_dict(field.obj.get("Lock"))


def write_field_lock(field: Field, lock: FieldLock | None) -> None:
    """Write or clear ``field.obj['Lock']`` in one transaction."""
    from pyropdf._form import FieldType  # noqa: PLC0415

    if lock is None:
        delete_field_lock(field)
        return
    if not isinstance(lock, FieldLock):
        msg = f"lock must be a FieldLock or None: {type(lock).__name__}"
        raise TypeError(msg)
    if field.type is not FieldType.SIGNATURE:
        msg = f"field {field.name!r} is a {field.type.value} field, not a signature field"
        raise ValueError(msg)
    document = field._document  # noqa: SLF001
    with document.transaction("field lock"):
        holder = field.obj
        catalog = _catalog(document)
        _require_version(document, catalog, (1, 5))
        old_xref = _indirect_xref(holder.get("Lock"))
        lock_xref = document.objects.add(_sig_field_lock_entries(lock))
        holder["Lock"] = document.objects[lock_xref]
        if old_xref is not None and old_xref not in _signed_file_xrefs(document):
            _core.object_free_unreferenced(document._core(), [old_xref])  # noqa: SLF001


def delete_field_lock(field: Field) -> None:
    """Remove ``field.obj['Lock']`` and free an unreferenced lock dictionary in one transaction."""
    from pyropdf._form import FieldType  # noqa: PLC0415

    if field.type is not FieldType.SIGNATURE:
        return
    holder = field.obj
    if "Lock" not in holder:
        return
    document = field._document  # noqa: SLF001
    with document.transaction("field lock"):
        holder = field.obj
        if "Lock" not in holder:
            return
        old_xref = _indirect_xref(holder.get("Lock"))
        del holder["Lock"]
        if old_xref is not None and old_xref not in _signed_file_xrefs(document):
            _core.object_free_unreferenced(document._core(), [old_xref])  # noqa: SLF001


def _parse_doc_mdp_params(params: object) -> DocMdpPermission | None:
    if params is None:
        return DocMdpPermission.FORM_FILLING_AND_SIGNATURES
    if not isinstance(params, Dictionary):
        return None
    raw_p = params.get("P")
    if raw_p is None:
        return DocMdpPermission.FORM_FILLING_AND_SIGNATURES
    if not _is_non_bool_int(raw_p):
        return None
    try:
        return DocMdpPermission(int(cast("int", raw_p)))
    except ValueError:
        return None


def read_doc_mdp_from_sig(sig_dict: Dictionary | None) -> DocMdpPermission | None:
    """Extract the ``DocMDP`` permission from a signature dictionary's ``/Reference`` array."""
    refs = None if sig_dict is None else sig_dict.get("Reference")
    if not isinstance(refs, Array):
        return None
    for index, ref in enumerate(refs):
        if index >= _MAX_SIG_REFS:
            break
        if isinstance(ref, Dictionary) and ref.get("TransformMethod") == "DocMDP":
            found = _parse_doc_mdp_params(ref.get("TransformParams"))
            if found is not None:
                return found
    return None


def read_field_locks_from_sig(sig_dict: Dictionary | None) -> tuple[FieldLock, ...]:
    """Extract all ``FieldMDP`` transform locks from a signature dictionary's ``/Reference`` array."""
    if sig_dict is None:
        return ()
    refs = sig_dict.get("Reference")
    if not isinstance(refs, Array):
        return ()
    locks: list[FieldLock] = []
    for index, ref in enumerate(refs):
        if index >= _MAX_SIG_REFS:
            break
        if not isinstance(ref, Dictionary) or ref.get("TransformMethod") != "FieldMDP":
            continue
        lock = _parse_field_lock_dict(ref.get("TransformParams"))
        if lock is not None:
            locks.append(lock)
    return tuple(locks)


def is_timestamp_sig(sig_dict: Dictionary | None) -> bool:
    """Return whether ``sig_dict`` is an RFC 3161 document timestamp dictionary."""
    if sig_dict is None:
        return False
    return sig_dict.get("Type") == "DocTimeStamp" or sig_dict.get("SubFilter") == "ETSI.RFC3161"


def read_certification(document: Document) -> DocMdpPermission | None:
    """Read ``Catalog['Perms']['DocMDP']`` and return its :class:`DocMdpPermission`, or ``None``."""
    perms = _catalog(document).get("Perms")
    if not _is_dict(perms):
        return None
    sig = cast("Dictionary", perms).get("DocMDP")
    if not _is_dict(sig):
        return None
    return read_doc_mdp_from_sig(cast("Dictionary", sig))


def build_reference_entries(
    document: Document,
    doc_mdp: DocMdpPermission | None,
    field_lock: FieldLock | None,
) -> list[dict[str, object]]:
    """Build direct ``/Reference`` entries for ``DocMDP`` and ``FieldMDP`` transforms."""
    refs: list[dict[str, object]] = []
    if doc_mdp is not None:
        refs.append(
            {
                "Type": Name("SigRef"),
                "TransformMethod": Name("DocMDP"),
                "TransformParams": {
                    "Type": Name("TransformParams"),
                    "P": int(doc_mdp),
                    "V": Name("1.2"),
                },
            }
        )
    if field_lock is not None:
        params: dict[str, object] = {
            "Type": Name("TransformParams"),
            "Action": Name(field_lock.action.value),
            "V": Name("1.2"),
        }
        if field_lock.fields:
            params["Fields"] = list(field_lock.fields)
        refs.append(
            {
                "Type": Name("SigRef"),
                "TransformMethod": Name("FieldMDP"),
                "Data": _catalog(document),
                "TransformParams": params,
            }
        )
    return refs


def ensure_pades_version(document: Document) -> None:
    """Ensure ``document`` is at least PDF 1.7 and declares ``ETSI_PAdES`` when below PDF 2.0."""
    from pyropdf._attachments import _version  # noqa: PLC0415

    catalog = _catalog(document)
    _require_version(document, catalog, (1, 7))
    if _version(document) < (2, 0):
        pades_ext: dict[str, object] = {
            "Type": Name("DeveloperExtensions"),
            "BaseVersion": Name("1.7"),
            "ExtensionLevel": 2,
        }
        raw_ext = catalog.get("Extensions")
        if _is_dict(raw_ext) and not cast("Dictionary", raw_ext).is_indirect:
            ext_dict = cast("Dictionary", raw_ext)
            if "ETSI_PAdES" not in ext_dict:
                ext_dict["ETSI_PAdES"] = pades_ext
        elif raw_ext is None:
            catalog["Extensions"] = {"Type": Name("Extensions"), "ETSI_PAdES": pades_ext}


def write_perms_doc_mdp(document: Document, signature: Dictionary) -> None:
    """Record ``signature`` under ``Catalog['Perms']['DocMDP']`` as an indirect reference."""
    catalog = _catalog(document)
    perms = catalog.get("Perms")
    if _is_dict(perms):
        cast("Dictionary", perms)["DocMDP"] = signature
    else:
        catalog["Perms"] = {"DocMDP": signature}


def inherit_prepared_field_settings(
    field_obj: Dictionary,
    doc_mdp: DocMdpPermission | None,
    field_lock: FieldLock | None,
    reason: str | None,
) -> tuple[DocMdpPermission | None, FieldLock | None, str | None]:
    """Read ``/Lock`` and ``/SV`` seed values from an existing unsigned signature field when not overridden."""
    if field_lock is None:
        field_lock = _parse_field_lock_dict(field_obj.get("Lock"))
    sv = field_obj.get("SV")
    if _is_dict(sv):
        sv_dict = cast("Dictionary", sv)
        if doc_mdp is None:
            mdp = sv_dict.get("MDP")
            if _is_dict(mdp):
                raw_p = cast("Dictionary", mdp).get("P")
                if _is_non_bool_int(raw_p) and int(cast("int", raw_p)) in (1, 2, 3):
                    doc_mdp = DocMdpPermission(int(cast("int", raw_p)))
        if reason is None:
            reasons = sv_dict.get("Reasons")
            if isinstance(reasons, Array) and len(reasons) == 1 and isinstance(reasons[0], String):
                reason = reasons[0].text
    return doc_mdp, field_lock, reason


def _ensure_unsigned_signature_field(document: Document, field: str) -> Dictionary:
    """Return the dictionary of an existing or newly added unsigned signature field ``field``."""
    from pyropdf._form import FieldType  # noqa: PLC0415

    form = document.form
    if field in form:
        existing = form[field]
        if existing.type is not FieldType.SIGNATURE:
            msg = f"field {field!r} is a {existing.type.value} field, not a signature field"
            raise ValueError(msg)
        if existing.is_signed:
            msg = f"field {field!r} is already signed"
            raise ValueError(msg)
        target = existing.obj
    else:
        if not field or "." in field:
            msg = f"a new signature field needs a name without dots: {field!r}"
            raise ValueError(msg)
        if len(document.pages) == 0:
            msg = "a document without pages has no page for the signature field's widget"
            raise ValueError(msg)
        page = document.pages[0].obj
        target = document.objects[
            document.objects.add(
                {
                    "Type": Name("Annot"),
                    "Subtype": Name("Widget"),
                    "FT": Name("Sig"),
                    "T": field,
                    "Rect": [0, 0, 0, 0],
                    "F": _INVISIBLE_WIDGET_FLAGS,
                    "P": page,
                }
            )
        ]
        annotations = page.get("Annots")
        if isinstance(annotations, Array):
            annotations.append(target)
        else:
            page["Annots"] = [target]
        acroform = document.catalog.get("AcroForm")
        if not isinstance(acroform, Dictionary):
            document.catalog["AcroForm"] = {"Fields": [target]}
        else:
            fields_arr = acroform.get("Fields")
            if isinstance(fields_arr, Array):
                fields_arr.append(target)
            else:
                acroform["Fields"] = [target]
    acroform = document.catalog["AcroForm"]
    flags = acroform.get("SigFlags")
    acroform["SigFlags"] = (flags if _is_non_bool_int(flags) else 0) | _SIG_EXIST_FLAG
    return target


def _validate_optional_str(label: str, value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        msg = f"{label} must be a str or None: {type(value).__name__}"
        raise TypeError(msg)
    _check_text_budget((value,), label)


def _validate_prepare_signature_args(  # noqa: PLR0913
    document: Document,
    field: str,
    doc_mdp: DocMdpPermission | None,
    field_lock: FieldLock | None,
    *,
    is_timestamp: bool,
    reason: str | None,
    location: str | None,
    contact: str | None,
    reserve: int,
) -> DocMdpPermission | None:
    if not isinstance(field, str):
        msg = f"field must be a str: {type(field).__name__}"
        raise TypeError(msg)
    checked_mdp = None if doc_mdp is None else _validate_doc_mdp(doc_mdp)
    if field_lock is not None and not isinstance(field_lock, FieldLock):
        msg = f"field_lock must be a FieldLock or None: {type(field_lock).__name__}"
        raise TypeError(msg)
    if not isinstance(is_timestamp, bool):
        msg = f"is_timestamp must be a bool: {is_timestamp!r}"
        raise TypeError(msg)
    for label, text in (("reason", reason), ("location", location), ("contact", contact)):
        _validate_optional_str(label, text)
    if isinstance(reserve, bool) or not isinstance(reserve, int) or not 0 < reserve <= _MAX_RESERVE:
        if isinstance(reserve, bool) or not isinstance(reserve, int):
            msg = f"reserve must be an integer: {reserve!r}"
            raise TypeError(msg)
        msg = f"reserve must be from 1 through {_MAX_RESERVE} bytes: {reserve}"
        raise ValueError(msg)
    if is_timestamp and checked_mdp is not None:
        msg = "a document timestamp cannot set a DocMDP certification policy"
        raise ValueError(msg)
    if checked_mdp is not None and (read_certification(document) is not None or bool(document.signatures)):
        msg = "cannot prepare a DocMDP certification field on an already signed or certified document"
        raise ValueError(msg)
    return checked_mdp


def _build_sv_entries(
    *,
    is_timestamp: bool,
    checked_mdp: DocMdpPermission | None,
    reason: str | None,
) -> dict[str, object]:
    entries: dict[str, object] = {
        "Type": Name("SV"),
        "Filter": Name("Adobe.PPKLite"),
    }
    if is_timestamp:
        entries["SubFilter"] = [Name("ETSI.RFC3161")]
    if checked_mdp is not None:
        entries["MDP"] = {"P": int(checked_mdp)}
    if reason is not None:
        entries["Reasons"] = [str.__str__(reason)]
    return entries


@contextmanager
def signature_operation(document: Document, name: str) -> Generator[None, None, None]:
    """Apply a signature edit and its structural checks under one operation budget."""
    core = document._core()  # noqa: SLF001
    operation = _core.begin_operation(core, name)
    try:
        yield
    except BaseException:
        _core.abort_canvas(core, operation)
        raise
    _core.end_transaction(core, True)  # noqa: FBT003 - commit the operation.


def require_signature_pdf20(document: Document, catalog: Dictionary, feature: str) -> None:
    """Refuse a PDF 2.0 promotion that introduces structural defects in existing objects."""
    before = document.check() if _version(document) < (2, 0) else None
    _require_collection_version(document, catalog, (2, 0))
    if before is None:
        return
    after = document.check()
    if after.truncated:
        msg = f"cannot promote the document to PDF 2.0 for {feature}: structural defect report is truncated"
        raise PdfError(msg)
    existing = set(before.defects)
    for defect in after.defects:
        if defect not in existing:
            location = "trailer" if defect.xref is None else f"object {defect.xref}"
            msg = f"cannot promote the document to PDF 2.0 for {feature}: {location}, {defect.clause}: {defect.message}"
            raise PdfError(msg)


def prepare_signature(  # noqa: PLR0913 - mirrors Document.prepare_signature keyword options.
    document: Document,
    field: str,
    *,
    doc_mdp: DocMdpPermission | None,
    field_lock: FieldLock | None,
    is_timestamp: bool,
    reason: str | None,
    location: str | None,
    contact: str | None,
    reserve: int,
) -> int:
    """Prepare an unsigned signature field with ``/Lock`` and ``/SV`` seed values in one transaction."""
    checked_mdp = _validate_prepare_signature_args(
        document,
        field,
        doc_mdp,
        field_lock,
        is_timestamp=is_timestamp,
        reason=reason,
        location=location,
        contact=contact,
        reserve=reserve,
    )
    step = "prepare signature"
    with signature_operation(document, step) if is_timestamp else document.transaction(step):
        catalog = _catalog(document)
        retired: list[int] = []
        needs_sv = checked_mdp is not None or is_timestamp or reason is not None
        if is_timestamp:
            require_signature_pdf20(document, catalog, "/DocTimeStamp")
        elif needs_sv:
            _require_version(document, catalog, (1, 7))
        elif field_lock is not None:
            _require_version(document, catalog, (1, 5))

        sig_field = _ensure_unsigned_signature_field(document, field)
        if field_lock is not None:
            if (old_lock := _indirect_xref(sig_field.get("Lock"))) is not None:
                retired.append(old_lock)
            lock_mdp = checked_mdp if catalog.get("Version") == "2.0" else None
            sig_field["Lock"] = document.objects[document.objects.add(_sig_field_lock_entries(field_lock, lock_mdp))]

        if needs_sv:
            if (old_sv := _indirect_xref(sig_field.get("SV"))) is not None:
                retired.append(old_sv)
            sv_entries = _build_sv_entries(is_timestamp=is_timestamp, checked_mdp=checked_mdp, reason=reason)
            sig_field["SV"] = document.objects[document.objects.add(sv_entries)]

        candidates = sorted(set(retired) - _signed_file_xrefs(document))
        if candidates:
            _core.object_free_unreferenced(document._core(), candidates)  # noqa: SLF001

        assert sig_field.xref is not None  # noqa: S101 - signature fields are indirect dictionaries.
        return sig_field.xref


def _read_stream_bytes(raw: object, cache: dict[int, bytes], budget: list[int], label: str) -> bytes:
    if not isinstance(raw, Stream):
        msg = f"{label} entry is not a stream"
        raise MalformedPdfError(msg)
    xref = raw.xref
    if xref is not None and xref in cache:
        return cache[xref]
    remaining = _DEFAULT_MAX_STREAM_DATA_SIZE - budget[0]
    if remaining <= 0:
        code = "stream_data_size"
        msg = f"DSS streams exceed the {_DEFAULT_MAX_STREAM_DATA_SIZE}-byte safety limit"
        raise LimitError(code, msg)
    data = raw.read(max_size=remaining)
    budget[0] += len(data)
    if xref is not None:
        cache[xref] = data
    return data


def _read_stream_array(raw: object, cache: dict[int, bytes], budget: list[int], label: str) -> tuple[bytes, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Array):
        msg = f"{label} is not an array"
        raise MalformedPdfError(msg)
    if len(raw) > _MAX_DSS_ENTRIES:
        code = "object_count"
        msg = f"{label} exceeds the {_MAX_DSS_ENTRIES}-entry safety limit"
        raise LimitError(code, msg)
    return tuple(_read_stream_bytes(item, cache, budget, label) for item in raw)


def _read_vri_entry(raw: object, cache: dict[int, bytes], budget: list[int], key: str) -> ValidationInfo:
    if not _is_dict(raw):
        msg = f"/DSS/VRI/{key} is not a dictionary"
        raise MalformedPdfError(msg)
    vri = cast("Dictionary", raw)
    certs = _read_stream_array(vri.get("Cert"), cache, budget, f"/DSS/VRI/{key}/Cert")
    ocsps = _read_stream_array(vri.get("OCSP"), cache, budget, f"/DSS/VRI/{key}/OCSP")
    crls = _read_stream_array(vri.get("CRL"), cache, budget, f"/DSS/VRI/{key}/CRL")
    raw_ts = vri.get("TS")
    ts = None if raw_ts is None else _read_stream_bytes(raw_ts, cache, budget, f"/DSS/VRI/{key}/TS")
    raw_tu = vri.get("TU")
    created: str | None = None
    if raw_tu is not None:
        if not isinstance(raw_tu, String) or not _is_pdf_date(raw_tu.text):
            msg = f"/DSS/VRI/{key}/TU is not a valid PDF date string"
            raise MalformedPdfError(msg)
        created = raw_tu.text
    if ts is not None and created is not None:
        msg = f"/DSS/VRI/{key} sets both /TS and /TU"
        raise MalformedPdfError(msg)
    return ValidationInfo(certificates=certs, ocsps=ocsps, crls=crls, timestamp=ts, created=created)


def read_security_store(document: Document) -> SecurityStore | None:
    """Read ``Catalog['DSS']`` into a :class:`SecurityStore`, or ``None`` when absent."""
    raw_dss = _catalog(document).get("DSS")
    if raw_dss is None:
        return None
    if not _is_dict(raw_dss):
        msg = "/DSS is not a dictionary"
        raise MalformedPdfError(msg)
    dss = cast("Dictionary", raw_dss)
    cache: dict[int, bytes] = {}
    budget = [0]
    certs = _read_stream_array(dss.get("Certs"), cache, budget, "/DSS/Certs")
    ocsps = _read_stream_array(dss.get("OCSPs"), cache, budget, "/DSS/OCSPs")
    crls = _read_stream_array(dss.get("CRLs"), cache, budget, "/DSS/CRLs")
    vri_entries: dict[str, ValidationInfo] = {}
    raw_vri = dss.get("VRI")
    if raw_vri is not None:
        if not _is_dict(raw_vri):
            msg = "/DSS/VRI is not a dictionary"
            raise MalformedPdfError(msg)
        vri_map = cast("Dictionary", raw_vri)
        if len(vri_map) > _MAX_DSS_ENTRIES:
            code = "object_count"
            msg = f"/DSS/VRI exceeds the {_MAX_DSS_ENTRIES}-entry safety limit"
            raise LimitError(code, msg)
        for raw_key in vri_map:
            key = str(raw_key)
            if _VRI_KEY_RE.fullmatch(key) is None:
                msg = f"invalid /DSS/VRI key: {key!r}"
                raise MalformedPdfError(msg)
            vri_entries[key] = _read_vri_entry(vri_map.get(key), cache, budget, key)
    return SecurityStore(certificates=certs, ocsps=ocsps, crls=crls, vri=vri_entries)


def _record_dss_stream(item: object, xrefs: list[int], seen_xrefs: set[int], streams: dict[bytes, Stream]) -> None:
    if not isinstance(item, Stream) or item.xref is None:
        return
    xrefs.append(item.xref)
    if item.xref in seen_xrefs:
        return
    seen_xrefs.add(item.xref)
    try:
        data = item.read(max_size=_DEFAULT_MAX_STREAM_DATA_SIZE)
    except PdfError:
        return
    streams.setdefault(data, item)


def _record_dss_array(arr: object, xrefs: list[int], seen_xrefs: set[int], streams: dict[bytes, Stream]) -> None:
    if (arr_xref := _indirect_xref(arr)) is not None:
        xrefs.append(arr_xref)
    if isinstance(arr, Array):
        for elem in list(arr)[:_MAX_DSS_ENTRIES]:
            _record_dss_stream(elem, xrefs, seen_xrefs, streams)


def _record_vri_state(
    key: str,
    entry: object,
    state: tuple[list[int], set[int], dict[bytes, Stream], dict[str, Dictionary]],
) -> None:
    if not _is_dict(entry):
        return
    xrefs, seen_xrefs, streams, vris = state
    ed = cast("Dictionary", entry)
    if (ed_xref := _indirect_xref(ed)) is not None:
        xrefs.append(ed_xref)
        vris[key] = ed
    for sub_key in ("Cert", "OCSP", "CRL"):
        _record_dss_array(ed.get(sub_key), xrefs, seen_xrefs, streams)
    _record_dss_stream(ed.get("TS"), xrefs, seen_xrefs, streams)


def _collect_dss_state(raw_dss: object) -> tuple[list[int], dict[bytes, Stream], dict[str, Dictionary]]:
    """Collect indirect xrefs, decoded stream blobs, and indirect VRI entries of an existing ``/DSS``."""
    if not _is_dict(raw_dss):
        return [], {}, {}
    dss = cast("Dictionary", raw_dss)
    xrefs: list[int] = []
    seen_xrefs: set[int] = set()
    streams: dict[bytes, Stream] = {}
    vris: dict[str, Dictionary] = {}
    if (dss_xref := _indirect_xref(dss)) is not None:
        xrefs.append(dss_xref)
    for arr_key in ("Certs", "OCSPs", "CRLs"):
        _record_dss_array(dss.get(arr_key), xrefs, seen_xrefs, streams)

    raw_vri = dss.get("VRI")
    if (vri_map_xref := _indirect_xref(raw_vri)) is not None:
        xrefs.append(vri_map_xref)
    if _is_dict(raw_vri):
        vri_map = cast("Dictionary", raw_vri)
        state = (xrefs, seen_xrefs, streams, vris)
        for key in list(vri_map)[:_MAX_DSS_ENTRIES]:
            _record_vri_state(str(key), vri_map.get(key), state)
    return xrefs, streams, vris


def _vri_value_matches(current: object, wanted: object) -> bool:
    if isinstance(wanted, list):
        return (
            isinstance(current, Array)
            and len(current) == len(wanted)
            and all(isinstance(c, Stream) and c.xref == w.xref for c, w in zip(current, wanted, strict=True))
        )
    if isinstance(wanted, Stream):
        return isinstance(current, Stream) and current.xref == wanted.xref
    if isinstance(wanted, Name):
        return isinstance(current, Name) and str(current) == str(wanted)
    return isinstance(current, String) and current.text == wanted


def _vri_dict_matches(existing: Dictionary, desired: Mapping[str, object]) -> bool:
    return set(existing) == set(desired) and all(
        _vri_value_matches(existing.get(key), wanted) for key, wanted in desired.items()
    )


def _build_vri_entry_dict(info: ValidationInfo, stream_for: Any) -> dict[str, object]:  # noqa: ANN401
    entry: dict[str, object] = {"Type": Name("VRI")}
    for pdf_key, blobs in (("Cert", info.certificates), ("OCSP", info.ocsps), ("CRL", info.crls)):
        if blobs:
            entry[pdf_key] = [stream_for(b) for b in blobs]
    if info.timestamp is not None:
        entry["TS"] = stream_for(info.timestamp)
    if info.created is not None:
        entry["TU"] = info.created
    return entry


def _build_vri_map_object(
    document: Document,
    vri: Mapping[str, ValidationInfo],
    existing_vris: Mapping[str, Dictionary],
    stream_for: Any,  # noqa: ANN401
    used_xrefs: set[int],
) -> Dictionary:
    vri_map_entries: dict[str, object] = {}
    for key, info in vri.items():
        vri_entry = _build_vri_entry_dict(info, stream_for)
        prior = existing_vris.get(key)
        vri_handle = (
            prior
            if prior is not None and _vri_dict_matches(prior, vri_entry)
            else document.objects[document.objects.add(vri_entry)]
        )
        if vri_handle.xref is not None:
            used_xrefs.add(vri_handle.xref)
        vri_map_entries[key] = vri_handle
    vri_map_xref = document.objects.add(vri_map_entries)
    used_xrefs.add(vri_map_xref)
    return document.objects[vri_map_xref]


def write_security_store(document: Document, store: SecurityStore | None) -> None:
    """Write ``store`` to ``Catalog['DSS']`` in one transaction, deduplicating DER streams."""
    if store is None:
        delete_security_store(document)
        return
    if not isinstance(store, SecurityStore):
        msg = f"security_store must be a SecurityStore or None: {type(store).__name__}"
        raise TypeError(msg)

    core = document._core()  # noqa: SLF001
    with signature_operation(document, "security store"):
        catalog = _catalog(document)
        retired, existing_streams, existing_vris = _collect_dss_state(catalog.get("DSS"))
        require_signature_pdf20(document, catalog, "/DSS")

        pool: dict[bytes, Stream] = {}
        used_xrefs: set[int] = set()

        def stream_for(blob: bytes) -> Stream:
            if blob not in pool:
                pool[blob] = (
                    existing_streams[blob]
                    if blob in existing_streams
                    else document.objects[document.objects.add_stream(blob)]
                )
            handle = pool[blob]
            if handle.xref is not None:
                used_xrefs.add(handle.xref)
            return handle

        dss_entries: dict[str, object] = {
            "Type": Name("DSS"),
            **{
                pdf_key: [stream_for(b) for b in blobs]
                for pdf_key, blobs in (
                    ("Certs", store.certificates),
                    ("OCSPs", store.ocsps),
                    ("CRLs", store.crls),
                )
                if blobs
            },
        }
        if store.vri:
            dss_entries["VRI"] = _build_vri_map_object(document, store.vri, existing_vris, stream_for, used_xrefs)

        dss_xref = document.objects.add(dss_entries)
        used_xrefs.add(dss_xref)
        catalog["DSS"] = document.objects[dss_xref]

        candidates = sorted(set(retired) - used_xrefs - _signed_file_xrefs(document))
        if candidates:
            _core.object_free_unreferenced(core, candidates)


def delete_security_store(document: Document) -> None:
    """Remove ``Catalog['DSS']`` and free unreferenced DSS streams in one transaction."""
    catalog = _catalog(document)
    if "DSS" not in catalog:
        return
    with document.transaction("security store"):
        catalog = _catalog(document)
        if "DSS" not in catalog:
            return
        retired, _, _ = _collect_dss_state(catalog.get("DSS"))
        del catalog["DSS"]
        if retired:
            _core.object_free_unreferenced(document._core(), sorted(set(retired)))  # noqa: SLF001


def read_legal_attestation(document: Document) -> LegalAttestation | None:
    """Read ``Catalog['Legal']`` into a :class:`LegalAttestation`, or ``None`` when absent."""
    raw = _catalog(document).get("Legal")
    if raw is None:
        return None
    if not _is_dict(raw):
        msg = "/Legal is not a dictionary"
        raise MalformedPdfError(msg)
    legal = cast("Dictionary", raw)
    kwargs: dict[str, Any] = {}
    for attr, pdf_key in _LEGAL_COUNT_FIELDS:
        val = legal.get(pdf_key)
        if val is None:
            continue
        if not _is_non_bool_int(val) or cast("int", val) < 0:
            msg = f"/Legal/{pdf_key} must be a non-negative integer"
            raise MalformedPdfError(msg)
        kwargs[attr] = int(cast("int", val))
    raw_oc = legal.get("OptionalContent")
    if raw_oc is not None:
        if not isinstance(raw_oc, bool):
            msg = "/Legal/OptionalContent must be a boolean"
            raise MalformedPdfError(msg)
        kwargs["optional_content"] = raw_oc
    raw_att = legal.get("Attestation")
    if raw_att is not None:
        if not isinstance(raw_att, String):
            msg = "/Legal/Attestation must be a text string"
            raise MalformedPdfError(msg)
        kwargs["attestation"] = raw_att.text
    return LegalAttestation(**kwargs)


def write_legal_attestation(document: Document, attestation: LegalAttestation | None) -> None:
    """Write ``attestation`` to ``Catalog['Legal']`` in one transaction."""
    if attestation is None:
        delete_legal_attestation(document)
        return
    if not isinstance(attestation, LegalAttestation):
        msg = f"legal_attestation must be a LegalAttestation or None: {type(attestation).__name__}"
        raise TypeError(msg)

    entries: dict[str, object] = {}
    for attr, pdf_key in _LEGAL_COUNT_FIELDS:
        val = getattr(attestation, attr)
        if val is not None:
            entries[pdf_key] = val
    if attestation.optional_content is not None:
        entries["OptionalContent"] = attestation.optional_content
    if attestation.attestation is not None:
        entries["Attestation"] = attestation.attestation

    with document.transaction("legal attestation"):
        catalog = _catalog(document)
        old_xref = _indirect_xref(catalog.get("Legal"))
        _require_version(document, catalog, (1, 5))
        legal_xref = document.objects.add(entries)
        catalog["Legal"] = document.objects[legal_xref]
        if old_xref is not None and old_xref not in _signed_file_xrefs(document):
            _core.object_free_unreferenced(document._core(), [old_xref])  # noqa: SLF001


def delete_legal_attestation(document: Document) -> None:
    """Remove ``Catalog['Legal']`` and free an unreferenced dictionary in one transaction."""
    catalog = _catalog(document)
    if "Legal" not in catalog:
        return
    with document.transaction("legal attestation"):
        catalog = _catalog(document)
        if "Legal" not in catalog:
            return
        old_xref = _indirect_xref(catalog.get("Legal"))
        del catalog["Legal"]
        if old_xref is not None:
            _core.object_free_unreferenced(document._core(), [old_xref])  # noqa: SLF001


def _der_tlv(data: bytes, pos: int) -> tuple[int, bytes, int]:
    """Read one DER tag-length-value element starting at ``pos``."""
    if pos + 2 > len(data):
        msg = "truncated DER element"
        raise ValueError(msg)
    tag = data[pos]
    first = data[pos + 1]
    if first < _DER_LONG_FORM:
        length = first
        start = pos + 2
    else:
        octets = first & 0x7F
        if octets == 0 or octets > _MAX_DER_LEN_OCTETS or pos + 2 + octets > len(data):
            msg = "invalid DER length"
            raise ValueError(msg)
        length = int.from_bytes(data[pos + 2 : pos + 2 + octets], "big")
        start = pos + 2 + octets
    end = start + length
    if end > len(data):
        msg = "DER element exceeds input buffer"
        raise ValueError(msg)
    return tag, data[start:end], end


def _der_children(body: bytes) -> list[tuple[int, bytes]]:
    """Split a constructed DER body into bounded ``(tag, value)`` children."""
    items: list[tuple[int, bytes]] = []
    pos = 0
    while pos < len(body):
        if len(items) >= _MAX_DER_ITEMS:
            msg = "DER container exceeds element limit"
            raise ValueError(msg)
        tag, val, pos = _der_tlv(body, pos)
        items.append((tag, val))
    return items


def _decode_oid(raw: bytes) -> str:
    """Decode DER OBJECT IDENTIFIER bytes to dotted decimal text."""
    if not raw or len(raw) > _MAX_OID_BYTES:
        msg = "invalid DER OBJECT IDENTIFIER"
        raise ValueError(msg)
    first = raw[0]
    arc0 = min(first // 40, 2)
    arcs = [arc0, first - 40 * arc0]
    acc = 0
    pending = False
    for byte in raw[1:]:
        acc = (acc << 7) | (byte & 0x7F)
        pending = True
        if (byte & 0x80) == 0:
            arcs.append(acc)
            acc = 0
            pending = False
    if pending:
        msg = "truncated DER OBJECT IDENTIFIER"
        raise ValueError(msg)
    return ".".join(str(arc) for arc in arcs)


def _parse_generalized_time(raw: bytes) -> datetime:
    """Parse an RFC 3161 UTC ``GeneralizedTime`` (dropping fractional seconds)."""
    if len(raw) < _GEN_TIME_DIGITS + 1 or not raw.endswith(b"Z"):
        msg = "TSTInfo genTime is not UTC GeneralizedTime"
        raise ValueError(msg)
    digits = raw[:_GEN_TIME_DIGITS]
    rest = raw[_GEN_TIME_DIGITS:-1]
    if not digits.isdigit() or (rest and not (rest.startswith(b".") and len(rest) > 1 and rest[1:].isdigit())):
        msg = "TSTInfo genTime is malformed"
        raise ValueError(msg)
    text = digits.decode("ascii")
    return datetime(
        int(text[0:4]),
        int(text[4:6]),
        int(text[6:8]),
        int(text[8:10]),
        int(text[10:12]),
        int(text[12:14]),
        tzinfo=UTC,
    )


def _extract_encapsulated_tst_info(contents: bytes) -> bytes:
    ci_tag, ci_body, _ = _der_tlv(contents, 0)
    if ci_tag != _DER_SEQUENCE:
        msg = "the time-stamp token is not a CMS ContentInfo"
        raise ValueError(msg)
    ci_items = _der_children(ci_body)
    if (
        len(ci_items) < _MIN_CI_FIELDS
        or ci_items[0][0] != _DER_OID
        or _decode_oid(ci_items[0][1]) != _ID_SIGNED_DATA
        or ci_items[1][0] != _DER_CONTEXT_0
    ):
        msg = "the time-stamp token is not a CMS SignedData"
        raise ValueError(msg)
    sd_tag, sd_body, _ = _der_tlv(ci_items[1][1], 0)
    if sd_tag != _DER_SEQUENCE:
        msg = "the SignedData is malformed"
        raise ValueError(msg)
    sd_items = _der_children(sd_body)
    if len(sd_items) <= 2 or sd_items[2][0] != _DER_SEQUENCE:  # noqa: PLR2004
        msg = "the SignedData holds no EncapsulatedContentInfo"
        raise ValueError(msg)
    eci_items = _der_children(sd_items[2][1])
    if (
        len(eci_items) < _MIN_CI_FIELDS
        or eci_items[0][0] != _DER_OID
        or _decode_oid(eci_items[0][1]) != _ID_CT_TST_INFO
        or eci_items[1][0] != _DER_CONTEXT_0
    ):
        msg = "the time-stamp token holds no TSTInfo"
        raise ValueError(msg)
    oct_tag, tst_info_der, oct_end = _der_tlv(eci_items[1][1], 0)
    if oct_tag != _DER_OCTET_STRING or oct_end != len(eci_items[1][1]):
        msg = "the encapsulated TSTInfo is not an OCTET STRING"
        raise ValueError(msg)
    return tst_info_der


def parse_rfc3161_tst_info(contents: bytes) -> tuple[bytes, str, bytes, datetime]:
    """Parse an RFC 3161 ``TimeStampToken`` into ``(tst_info_der, imprint_digest, hashed_message, gen_time)``."""
    tst_info_der = _extract_encapsulated_tst_info(contents)
    tst_tag, tst_body, tst_end = _der_tlv(tst_info_der, 0)
    if tst_tag != _DER_SEQUENCE or tst_end != len(tst_info_der):
        msg = "the TSTInfo is malformed"
        raise ValueError(msg)
    tst_items = _der_children(tst_body)
    if len(tst_items) < _MIN_TST_FIELDS or tst_items[2][0] != _DER_SEQUENCE or tst_items[4][0] != _DER_GENERALIZED_TIME:
        msg = "the TSTInfo is missing messageImprint or genTime"
        raise ValueError(msg)
    imprint_items = _der_children(tst_items[2][1])
    if (
        len(imprint_items) != _MIN_CI_FIELDS
        or imprint_items[0][0] != _DER_SEQUENCE
        or imprint_items[1][0] != _DER_OCTET_STRING
    ):
        msg = "the TSTInfo messageImprint is malformed"
        raise ValueError(msg)
    alg_items = _der_children(imprint_items[0][1])
    if not alg_items or alg_items[0][0] != _DER_OID:
        msg = "the TSTInfo messageImprint algorithm is malformed"
        raise ValueError(msg)
    oid = _decode_oid(alg_items[0][1])
    digest_name = _DIGEST_OIDS.get(oid, oid)
    return tst_info_der, digest_name, imprint_items[1][1], _parse_generalized_time(tst_items[4][1])


__all__ = [
    "DocMdpPermission",
    "EncryptedPayload",
    "FieldLock",
    "FieldMdpAction",
    "LegalAttestation",
    "SecurityStore",
    "ValidationInfo",
    "vri_key",
    "wrap_encrypted_payload",
]
