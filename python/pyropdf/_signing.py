"""Signing documents: PAdES B-B and B-T signatures (ETSI EN 319 142-1).

:attr:`Document.signatures` lists the signatures of an opened file, and
:meth:`Signature.verify` checks one: its digest, its CMS signature, its
time-stamp, its chain to the trust anchors given, and what later revisions
modify. Revocation and long-term validation are left to a validator such as
pyHanko.
"""

from __future__ import annotations

import typing
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeAlias

from pyropdf import _core
from pyropdf._core import _Signer
from pyropdf._errors import LimitError, PdfError
from pyropdf._form import FieldType
from pyropdf._metadata import _require_version, pdf_date
from pyropdf._objects import Array, Dictionary, Name, Stream, _catalog
from pyropdf._signature_build import build_entries
from pyropdf._signature_seeds import field_settings
from pyropdf._signature_structures import (
    DocMdpPermission,
    FieldLock,
    _updated_lock,
    _validate_doc_mdp,
    build_reference_entries,
    ensure_pades_version,
    ensure_unsigned_signature_field,
    is_timestamp_sig,
    parse_rfc3161_tst_info,
    read_certification,
    read_doc_mdp_from_sig,
    read_field_locks_from_sig,
    require_signature_pdf20,
    signature_operation,
    write_perms_doc_mdp,
)

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.hashes import HashAlgorithm

    from pyropdf._document import Document

#: A time-stamp callback: it receives the digest of the signature value and
#: the name of the selected SHA-2, SHA-3 or SHAKE256 algorithm and returns
#: the DER of an RFC 3161 time-stamp token over that digest.
Timestamper: TypeAlias = typing.Callable[[bytes, str], bytes]
Timestamper.__doc__ = (
    "A time-stamp callback: it receives the digest of the signature value and "
    "the name of the selected SHA-2, SHA-3 or SHAKE256 algorithm and returns "
    "the DER of an RFC 3161 time-stamp token over that digest."
)

#: The bytes reserved for the CMS signature by default.
DEFAULT_RESERVE = 16_384
#: The most bytes that ``reserve`` may reserve.
_MAX_RESERVE = 1 << 20
#: The ``/ByteRange`` that the saved document holds until the core fills it.
_BYTE_RANGE_PLACEHOLDER = (0, 9_999_999_999, 9_999_999_999, 9_999_999_999)
#: The AcroForm's ``/SigFlags``: SignaturesExist and AppendOnly.
_SIGNATURE_FLAGS = 3


def _bytes(name: str, value: object) -> bytes:
    if isinstance(value, str):
        return value.encode()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    msg = f"{name} must be bytes or str: {type(value).__name__}"
    raise TypeError(msg)


class Signer:
    """A signing key or callback, with its certificate chain, which :meth:`Document.sign` signs with.

    :meth:`from_pem` and :meth:`from_pkcs12` read NIST and Brainpool
    ECDSA, Ed25519 and Ed448 keys. P-256 and P-384 run in Rust; the
    other curves use the optional ``cryptography`` package. :meth:`callback` signs with a
    key that stays outside pyropdf, including P-521, Brainpool P256r1,
    P384r1 and P512r1, Ed25519, Ed448, RSA, or an HSM. Use the optional
    ``cryptography`` package to verify signatures on those additional curves.
    """

    __slots__ = ("_core",)

    _core: _Signer

    def __init__(self) -> None:
        """Refuse construction; use :meth:`from_pem`, :meth:`from_pkcs12`, or :meth:`callback`."""
        msg = "create a Signer with Signer.from_pem, Signer.from_pkcs12, or Signer.callback"
        raise TypeError(msg)

    @classmethod
    def _wrap(cls, core: _Signer) -> Signer:
        signer = cls.__new__(cls)
        signer._core = core  # noqa: SLF001 - the classmethods' own instance.
        return signer

    @classmethod
    def _local(
        cls,
        loaded: _Signer | tuple[bytes, list[bytes]],
        digest_algorithm: str | None,
        *,
        ordered: bool,
    ) -> Signer:
        if isinstance(loaded, _Signer):
            return cls._wrap(loaded)
        try:
            from cryptography import x509  # noqa: PLC0415
            from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
            from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519  # noqa: PLC0415
        except ImportError as error:
            msg = (
                "loading a P-521, Brainpool or EdDSA key requires cryptography; an external key can use Signer.callback"
            )
            raise ValueError(msg) from error
        encoded, certificates = loaded
        key = serialization.load_der_private_key(encoded, password=None)
        if not isinstance(key, (ec.EllipticCurvePrivateKey, ed25519.Ed25519PrivateKey, ed448.Ed448PrivateKey)):
            msg = "this private key must sign through Signer.callback"
            raise ValueError(msg)  # noqa: TRY004 - the encoded key type is an invalid file value.
        public = key.public_key()
        index = next(
            (n for n, der in enumerate(certificates) if x509.load_der_x509_certificate(der).public_key() == public),
            None,
        )
        if index is None or (ordered and index != 0):
            msg = (
                "the first certificate does not hold the key's public key"
                if ordered
                else "the PKCS#12 file holds no certificate of its key"
            )
            raise ValueError(msg)
        certificates[0], certificates[index] = certificates[index], certificates[0]
        if isinstance(key, ec.EllipticCurvePrivateKey):
            # The CMS chain checks the selected curve/digest pair at construction.
            def sign(data: bytes) -> bytes:
                return key.sign(data, ec.ECDSA(algorithm))

            result = cls.callback(certificates, sign, digest_algorithm=digest_algorithm)
            algorithm = _backend_hash(result.digest_algorithm)
            return result
        return cls.callback(certificates, key.sign, digest_algorithm=digest_algorithm)

    @classmethod
    def from_pem(
        cls,
        key: bytes | str,
        certificates: bytes | str,
        *,
        password: bytes | str | None = None,
        digest_algorithm: str | None = None,
    ) -> Signer:
        """Read a PEM private key and the PEM certificate chain whose first certificate holds its public key.

        ``key`` is a ``PRIVATE KEY`` (PKCS#8), an ``ENCRYPTED PRIVATE KEY``
        that ``password`` decrypts, or an ``EC PRIVATE KEY`` (SEC1).
        ``certificates`` holds the signer's certificate first,
        then any intermediate certificates to embed. A key that pyropdf does
        not sign with, a wrong password, or a certificate of another key
        raises :class:`ValueError`. Choose the curve's digest with ``digest_algorithm`` as in :meth:`callback`.
        """
        secret = None if password is None else _bytes("password", password)
        return cls._local(
            _Signer.from_pem(_bytes("key", key), _bytes("certificates", certificates), secret, digest_algorithm),
            digest_algorithm,
            ordered=True,
        )

    @classmethod
    def from_pkcs12(cls, data: bytes, password: str | None = None, *, digest_algorithm: str | None = None) -> Signer:
        """Read the ECDSA or EdDSA key and its certificates from a PKCS#12 file.

        The file is PBES2-encrypted with a SHA-2 MAC, as OpenSSL 3 and
        Python's ``cryptography`` write it; ``None`` is the empty password.
        ``digest_algorithm`` accepts the same hashes as :meth:`from_pem`.
        Legacy encryption, a wrong password, or a file without exactly one
        key and its certificate raises :class:`ValueError`.
        """
        if password is not None and not isinstance(password, str):
            msg = f"password must be a str or None: {type(password).__name__}"
            raise TypeError(msg)
        return cls._local(
            _Signer.from_pkcs12(_bytes("data", data), password or "", digest_algorithm), digest_algorithm, ordered=False
        )

    @classmethod
    def callback(
        cls, certificates: Sequence[bytes], sign: Callable[[bytes], bytes], *, digest_algorithm: str | None = None
    ) -> Signer:
        """Sign with ``sign``, which holds the key of the first of the DER ``certificates``.

        ``sign`` receives the bytes to sign, the DER of the signed
        attributes, and returns their signature. For ECDSA return a DER
        signature using ``digest_algorithm``. NIST curves use the corresponding
        SHA-2 or SHA-3 digest (256, 384 or 512 bits). Brainpool curves allow
        the same choices plus the longer digests. The default is the matching
        SHA-2 digest. Ed25519 and Ed448 sign the attributes directly and use
        ``sha512`` and ``shake256`` respectively for the document digest.
        RSA uses PKCS#1 v1.5 with a SHA-2 or SHA-3 digest (SHA-256 by default). Callback exceptions propagate from
        :meth:`Document.sign`.
        """
        if not callable(sign):
            msg = f"sign must be callable: {type(sign).__name__}"
            raise TypeError(msg)
        if isinstance(certificates, (bytes, str)):
            msg = "certificates must be a sequence of DER certificates, not one"
            raise TypeError(msg)
        chain = [_bytes("a certificate", certificate) for certificate in certificates]
        return cls._wrap(_Signer.callback(chain, sign, digest_algorithm))

    @property
    def certificates(self) -> tuple[bytes, ...]:
        """Return the DER certificates that the signature embeds, the signer's first."""
        return tuple(self._core.certificates)

    @property
    def digest_algorithm(self) -> str:
        """Return the selected SHA-2, SHA-3, or fixed 512-bit SHAKE256 digest name."""
        return self._core.digest_algorithm

    def __repr__(self) -> str:
        """Return the signer's digest algorithm and chain length."""
        return f"<Signer {self.digest_algorithm}, {len(self.certificates)} certificates>"


def _optional_text(name: str, value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        msg = f"{name} must be a str or None: {type(value).__name__}"
        raise TypeError(msg)
    return value


def _signature_field(document: Document, field: str, signature: Dictionary) -> None:
    """Set ``signature`` as the value of the signature field ``field``, adding an invisible one when none exists."""
    widget = ensure_unsigned_signature_field(document, field, signature_flags=_SIGNATURE_FLAGS)
    widget["V"] = signature


def _validate_reserve(reserve: object) -> int:
    if isinstance(reserve, bool) or not isinstance(reserve, int):
        msg = f"reserve must be an integer: {reserve!r}"
        raise TypeError(msg)
    if not 0 < reserve <= _MAX_RESERVE:
        msg = f"reserve must be from 1 through {_MAX_RESERVE} bytes: {reserve}"
        raise ValueError(msg)
    return reserve


def _resolve_sign_settings(  # noqa: PLR0913 - the requested field and its signing settings.
    document: Document,
    field: str,
    checked_mdp: DocMdpPermission | None,
    field_lock: FieldLock | None,
    reason_text: str | None,
    *,
    signer: Signer,
    timestamped: bool,
) -> tuple[DocMdpPermission | None, FieldLock | None, str | None]:
    effective_mdp, effective_lock, effective_reason = checked_mdp, field_lock, reason_text
    if field in document.form:
        existing = document.form[field]
        if existing.type is FieldType.SIGNATURE and not existing.is_signed:
            effective_mdp, effective_lock, effective_reason = field_settings(
                document,
                existing.obj,
                checked_mdp,
                field_lock,
                reason_text,
                None if signer._core.fixed_digest else signer.digest_algorithm,  # noqa: SLF001
                signer.certificates,
                timestamped=timestamped,
            )
    if effective_mdp is not None and (read_certification(document) is not None or bool(document.signatures)):
        msg = "cannot add a DocMDP certification signature to a document that is already signed or certified"
        raise ValueError(msg)
    return effective_mdp, effective_lock, effective_reason


def _apply_lock(document: Document, field: str, lock: FieldLock | None) -> None:
    holder = document.form[field].obj
    previous = bool(document.signatures)
    if lock is not None and (
        ("Lock" not in holder and not previous)
        or (lock.permission is not None and ("Lock" not in holder or holder["Lock"].get("P") != lock.permission))
    ):
        # A later approval can add a FieldMDP transform without rewriting
        # the unsigned field's previously certified structure.
        holder["Lock"] = _updated_lock(document, holder.get("Lock"), lock)
    if previous:
        for verdict in document.changes(incremental=True).permissions:
            if (
                verdict.field == field
                and verdict.transform.startswith(("FieldMDP", "Field lock P="))
                and verdict.permitted is False
            ):
                msg = f"signing field {field!r} violates the {verdict.transform} lock from {verdict.signature!r}"
                raise ValueError(msg)


def sign(  # noqa: PLR0913 - the signature's options are all keyword-only
    document: Document,
    field: str,
    signer: Signer,
    *,
    timestamper: Timestamper | None,
    doc_mdp: DocMdpPermission | None = None,
    field_lock: FieldLock | None = None,
    reason: str | None,
    location: str | None,
    contact: str | None,
    build_properties: Mapping[str, Mapping[str, object]] | None = None,
    reserve: int,
) -> bytes:
    """Sign ``document`` in a new revision, as :meth:`Document.sign` describes, and return the signed bytes."""
    if not isinstance(field, str):
        msg = f"field must be a str: {type(field).__name__}"
        raise TypeError(msg)
    if not isinstance(signer, Signer):
        msg = f"signer must be a Signer: {type(signer).__name__}"
        raise TypeError(msg)
    if timestamper is not None and not callable(timestamper):
        msg = f"timestamper must be callable or None: {type(timestamper).__name__}"
        raise TypeError(msg)
    checked_mdp = None if doc_mdp is None else _validate_doc_mdp(doc_mdp)
    if field_lock is not None and not isinstance(field_lock, FieldLock):
        msg = f"field_lock must be a FieldLock or None: {type(field_lock).__name__}"
        raise TypeError(msg)
    reserve = _validate_reserve(reserve)
    reason_text = _optional_text("reason", reason)
    location_text = _optional_text("location", location)
    contact_text = _optional_text("contact", contact)
    document._ensure_open()  # noqa: SLF001
    if document.needs_password or (
        document.encryption_method is not None and document.verify_integrity().status != "valid"
    ):
        msg = "pyropdf does not sign encrypted documents without valid PDF MAC protection"
        raise ValueError(msg)
    core = document._core()  # noqa: SLF001
    _core.begin_signing(core)
    try:
        effective_mdp, effective_lock, effective_reason = _resolve_sign_settings(
            document,
            field,
            checked_mdp,
            field_lock,
            reason_text,
            signer=signer,
            timestamped=timestamper is not None,
        )
        build = build_entries(build_properties)
        _require_version(
            document,
            _catalog(document),
            (1, 7) if "SigQ" in build else (1, 6) if "REx" in build.get("App", {}) else (1, 5),
        )
        optional_entries = {
            key: text
            for key, text in (
                ("Reason", effective_reason),
                ("Location", location_text),
                ("ContactInfo", contact_text),
            )
            if text is not None
        }
        entries: dict[str, Any] = {
            "Type": Name("Sig"),
            "Filter": Name("Adobe.PPKLite"),
            "SubFilter": Name("ETSI.CAdES.detached"),
            # The core finds these two entries in the saved bytes as written here, in this order.
            "ByteRange": list(_BYTE_RANGE_PLACEHOLDER),
            "Contents": b"0" * (2 * reserve),
            "M": pdf_date(datetime.now(UTC)),
            "Prop_Build": build,
            **optional_entries,
        }
        _declare_signature_extensions(document, *signer._core.extensions)  # noqa: SLF001
        ensure_pades_version(document)
        if effective_mdp is not None or effective_lock is not None:
            entries["Reference"] = build_reference_entries(document, effective_mdp, effective_lock)
        signature_number = document.objects.add(entries)
        signature = document.objects[signature_number]
        _signature_field(document, field, signature)
        if effective_mdp is not None:
            write_perms_doc_mdp(document, signature)
        _apply_lock(document, field, effective_lock)
        signed = _core.sign_document(core, signature_number, reserve, signer._core, timestamper)  # noqa: SLF001
    except BaseException:
        # Signing ends its transaction only once it replaced the document.
        _core.end_transaction(core, False)  # noqa: FBT003 - the core's flag
        raise
    document._drawn = None  # noqa: SLF001 - what canvases drew lived in the replaced document.
    return signed


def _declare_signature_extensions(document: Document, hash_extension: bool, curve_extension: bool) -> None:  # noqa: FBT001
    if not (hash_extension or curve_extension):
        return
    catalog = _catalog(document)
    require_signature_pdf20(document, catalog, "the selected signature algorithm")
    if "Extensions" not in catalog:
        catalog["Extensions"] = {}
    extensions = catalog["Extensions"]
    if not isinstance(extensions, Dictionary):
        msg = "the catalog Extensions entry must be a dictionary"
        raise PdfError(msg)
    if "ISO_" not in extensions:
        extensions["ISO_"] = []
    declarations = extensions["ISO_"]
    if isinstance(declarations, Dictionary):
        extensions["ISO_"] = [declarations]
        declarations = extensions["ISO_"]
    if not isinstance(declarations, Array):
        msg = "the ISO_ developer extensions must be an array in PDF 2.0"
        raise PdfError(msg)
    for level, standard, required in ((32001, 45874, hash_extension), (32002, 45875, curve_extension)):
        if required and not any(
            isinstance(item, Dictionary)
            and item.get("BaseVersion") == "2.0"
            and item.get("ExtensionLevel") == level
            and getattr(item.get("ExtensionRevision"), "text", None) == ":2022"
            for item in declarations
        ):
            declarations.append(
                {
                    "Type": Name("DeveloperExtensions"),
                    "BaseVersion": Name("2.0"),
                    "ExtensionLevel": level,
                    "ExtensionRevision": ":2022",
                    "URL": f"https://www.iso.org/standard/{standard}.html",
                }
            )


def _find_placeholder(data: bytes, reserve: int) -> tuple[slice, slice]:
    """Locate the single ``/ByteRange`` and ``/Contents`` placeholder slices in ``data``."""
    start, first, second, third = _BYTE_RANGE_PLACEHOLDER
    prefix = f"/ByteRange [{start} {first} {second} {third}] /Contents (".encode("ascii")
    digits = 2 * reserve
    found: tuple[slice, slice] | None = None
    pos = 0
    while (idx := data.find(prefix, pos)) != -1:
        open_paren = idx + len(prefix) - 1
        close_paren = open_paren + 1 + digits
        if (
            close_paren < len(data)
            and data[close_paren] == ord(")")
            and data[open_paren + 1 : close_paren] == b"0" * digits
        ):
            if found is not None:
                msg = "the saved document holds more than one signature placeholder"
                raise PdfError(msg)
            byte_range_slice = slice(idx + len(b"/ByteRange "), open_paren - len(b" /Contents "))
            contents_slice = slice(open_paren, close_paren + 1)
            found = (byte_range_slice, contents_slice)
        pos = idx + 1
    if found is None:
        msg = "the saved document holds no signature placeholder"
        raise PdfError(msg)
    return found


def _fill_timestamp(
    saved_bytes: bytes,
    reserve: int,
    timestamper: Timestamper,
    digest_algorithm: str,
) -> bytes:
    """Patch ``/ByteRange`` and ``/Contents`` with an RFC 3161 ``TimeStampToken`` over ``saved_bytes``."""
    data = bytearray(saved_bytes)
    br_slice, contents_slice = _find_placeholder(saved_bytes, reserve)
    c_start = contents_slice.start
    c_end = contents_slice.stop
    written = f"[0 {c_start} {c_end} {len(data) - c_end}".encode("ascii")
    br_len = br_slice.stop - br_slice.start
    padding_len = (br_len - 1) - len(written)
    if padding_len < 0:
        msg = "the document is too large to timestamp"
        raise PdfError(msg)
    data[br_slice.start : br_slice.start + len(written)] = written
    data[br_slice.start + len(written) : br_slice.stop - 1] = b" " * padding_len

    digest = _digest(digest_algorithm, bytes(data[:c_start]) + bytes(data[c_end:]))
    if digest is None:
        msg = f"unsupported timestamp digest {digest_algorithm}"
        raise ValueError(msg)
    raw_token = timestamper(digest, digest_algorithm)
    if not isinstance(raw_token, (bytes, bytearray, memoryview)):
        msg = f"the timestamper must return bytes: {type(raw_token).__name__}"
        raise TypeError(msg)
    token = bytes(raw_token)
    try:
        _, imprint_alg, hashed_message, _ = parse_rfc3161_tst_info(token)
        _core.signature_read(token)
    except LimitError:
        raise
    except (ValueError, PdfError) as error:
        msg = f"the time-stamp token is not RFC 3161: {error}"
        raise ValueError(msg) from error
    if imprint_alg != digest_algorithm or hashed_message != digest:
        msg = f"the time-stamp token does not stamp the document's {digest_algorithm} digest"
        raise ValueError(msg)
    if len(token) > reserve:
        msg = (
            f"the signature needs {len(token)} bytes, more than the {reserve} reserved; timestamp with a larger reserve"
        )
        raise ValueError(msg)
    hex_body = token.hex().encode("ascii") + b"0" * (2 * (reserve - len(token)))
    data[c_start:c_end] = b"<" + hex_body + b">"
    return bytes(data)


def timestamp(
    document: Document,
    timestamper: Timestamper,
    *,
    field: str = "DocTimeStamp",
    reserve: int = DEFAULT_RESERVE,
    digest_algorithm: str = "sha256",
) -> bytes:
    """Append an RFC 3161 ``/DocTimeStamp`` revision to ``document`` and return the timestamped bytes."""
    if digest_algorithm not in ("sha256", "sha384", "sha512", "sha3_256", "sha3_384", "sha3_512", "shake256"):
        msg = f"unsupported timestamp digest {digest_algorithm!r}"
        raise ValueError(msg)
    if not callable(timestamper):
        msg = f"timestamper must be callable: {type(timestamper).__name__}"
        raise TypeError(msg)
    if not isinstance(field, str):
        msg = f"field must be a str: {type(field).__name__}"
        raise TypeError(msg)
    reserve = _validate_reserve(reserve)
    document._ensure_open()  # noqa: SLF001
    if document.needs_password or (
        document.encryption_method is not None and document.verify_integrity().status != "valid"
    ):
        msg = "pyropdf does not timestamp encrypted documents without valid PDF MAC protection"
        raise ValueError(msg)
    core = document._core()  # noqa: SLF001
    _core.begin_signing(core)
    try:
        with signature_operation(document, "timestamp"):
            require_signature_pdf20(document, _catalog(document), "/DocTimeStamp")
            _declare_signature_extensions(
                document,
                hash_extension=digest_algorithm.startswith("sha3_") or digest_algorithm == "shake256",
                curve_extension=False,
            )
            entries: dict[str, Any] = {
                "Type": Name("DocTimeStamp"),
                "Filter": Name("Adobe.PPKLite"),
                "SubFilter": Name("ETSI.RFC3161"),
                "V": 0,
                "ByteRange": list(_BYTE_RANGE_PLACEHOLDER),
                "Contents": b"0" * (2 * reserve),
            }
            signature_number = document.objects.add(entries)
            sig_obj = document.objects[signature_number]
            _signature_field(document, field, sig_obj)
        stamped = _core.timestamp_document(
            core, signature_number, reserve, lambda data: _fill_timestamp(data, reserve, timestamper, digest_algorithm)
        )
    except BaseException:
        _core.end_transaction(core, False)  # noqa: FBT003 - release the failed signing transaction.
        raise
    document._drawn = None  # noqa: SLF001
    return stamped


#: The levels of modification after a signed revision, least first, as pyHanko's default diff policy reports
#: them: it has no rule that explains an annotation, so an added or changed one other than a widget is "other".
_LEVELS = ("none", "form filling", "other")
#: The keys of a field or widget that filling a form changes.
_FILLING_KEYS = frozenset({"V", "AS", "AP", "MK", "Kids", "Fields", "NeedAppearances", "SigFlags", "DR", "DA"})
#: The keys of the catalog that a signature or its validation data adds.
_SIGNING_KEYS = frozenset({"DSS", "Extensions", "Legal", "Perms", "Version"})
#: The most certificates in one chain.
_MAX_CHAIN = 16
#: The most levels of nested containers that comparing two revisions of an object reads.
_MAX_SHAPE_DEPTH = 32
#: The most objects that one annotation's appearance reaches, which the classification walks.
_MAX_APPEARANCE_OBJECTS = 64


@dataclass(frozen=True, slots=True, kw_only=True)
class SignatureVerification:
    """What :meth:`Signature.verify` found.

    ``intact`` says whether the digest of the signed byte ranges matches the
    one that the signature signs, ``covers_revision`` whether the byte ranges
    cover the whole signed revision except ``/Contents``, and ``valid``
    whether the CMS signature verifies with the signer's certificate:
    ``None`` when it cannot be checked, as an RSA signature without the
    ``cryptography`` package. ``trusted`` is whether the certificates chain
    to one of the trust anchors given, each valid at the signing time,
    ``None`` without anchors. ``timestamp`` and ``timestamp_valid`` describe
    a signature time-stamp token: its time, and whether it stamps this
    signature and its own signature verifies (its authority's trust is not
    checked). ``modifications`` classifies the later revisions as pyHanko's
    default diff policy does: ``"none"``, ``"form filling"``, or ``"other"``,
    which an added or changed annotation other than a widget is.
    ``problems`` explains each check that failed or could not be made.
    """

    intact: bool
    covers_revision: bool
    valid: bool | None
    trusted: bool | None
    signer: str | None
    signing_time: datetime | None
    timestamp: datetime | None
    timestamp_valid: bool | None
    modifications: str
    problems: tuple[str, ...]

    def __bool__(self) -> bool:
        """Return whether the signature is intact, covers its revision, verifies, and chains to any anchors given."""
        return self.intact and self.covers_revision and self.valid is True and self.trusted is not False


@dataclass(frozen=True, slots=True, kw_only=True)
class Signature:
    """A signature of the opened file: its field, its signature dictionary, and the revision that it signs.

    ``revision`` counts the file's revisions from 0, the oldest, and is
    ``None`` when the byte ranges end where no revision does.
    """

    field: str
    xref: int | None
    byte_range: tuple[int, ...]
    revision: int | None
    _document: Document = field(init=False, repr=False, compare=False)

    def _dictionary(self) -> Dictionary | None:
        document = getattr(self, "_document", None)
        if document is None:
            return None
        if self.xref is not None and self.xref in document.objects:
            obj = document.objects[self.xref]
            if isinstance(obj, Dictionary):
                return obj
        if self.field in document.form:
            val = _inherited(document.form[self.field].obj, "V")
            if isinstance(val, Dictionary):
                return val
        return None

    @property
    def is_timestamp(self) -> bool:
        """Return whether this signature is an RFC 3161 document timestamp (``/DocTimeStamp``)."""
        return is_timestamp_sig(self._dictionary())

    @property
    def doc_mdp(self) -> DocMdpPermission | None:
        """Return the ``DocMDP`` permission declared in this signature's ``/Reference``, or ``None``."""
        return read_doc_mdp_from_sig(self._dictionary())

    @property
    def field_locks(self) -> tuple[FieldLock, ...]:
        """Return the ``FieldMDP`` :class:`FieldLock` entries signed by this signature's ``/Reference``."""
        return read_field_locks_from_sig(self._dictionary())

    def verify(self, trust: Sequence[bytes] | None = None) -> SignatureVerification:
        """Verify the signature against the opened file, with ``trust`` the DER or PEM certificates to trust.

        Nothing is fetched: revocation, long-term validation, and DocMDP
        permissions are not checked.
        """
        return _verify(self, _anchors(trust))


def _anchors(trust: Sequence[bytes] | None) -> list[bytes] | None:
    if trust is None:
        return None
    if isinstance(trust, (bytes, str)) or not isinstance(trust, Sequence):
        msg = f"trust must be a sequence of certificates or None: {type(trust).__name__}"
        raise TypeError(msg)
    anchors = []
    for certificate in trust:
        data = _bytes("a trusted certificate", certificate)
        if data.lstrip().startswith(b"-----BEGIN"):
            anchors.extend(_pem_certificates(data))
        else:
            anchors.append(data)
    return anchors


def _pem_certificates(pem: bytes) -> list[bytes]:
    """Return the DER certificates of PEM data."""
    import base64  # noqa: PLC0415

    out = []
    for block in pem.split(b"-----BEGIN CERTIFICATE-----")[1:]:
        body = block.split(b"-----END CERTIFICATE-----", 1)[0]
        out.append(base64.b64decode(b"".join(body.split())))
    return out


def signatures(document: Document) -> list[Signature]:
    """Return the signed signature fields of ``document``, in field order; see :attr:`Document.signatures`."""
    found = _core.document_file(document._core())  # noqa: SLF001
    ends = [] if found is None else [end for end, _ in found[2]]
    origin = 0 if found is None else found[1]
    out = []
    for name in document.form:
        item = document.form[name]
        if item.type is not FieldType.SIGNATURE or not item.is_signed:
            continue
        value = _inherited(item.obj, "V")
        if not isinstance(value, Dictionary):
            continue
        byte_range = value.get("ByteRange")
        if isinstance(byte_range, Array) and all(type(part) is int for part in byte_range):
            ranges = tuple(byte_range)
        else:
            ranges = ()
        revision = None
        if len(ranges) == 4 and all(part >= 0 for part in ranges) and found is not None:  # noqa: PLR2004 - two ranges of start and length
            revision = _revision(found[0], origin + ranges[2] + ranges[3], ends)
        sig = Signature(field=name, xref=value.xref, byte_range=ranges, revision=revision)
        object.__setattr__(sig, "_document", document)
        out.append(sig)
    return out


def _inherited(node: Any, key: str) -> Any:  # noqa: ANN401 - a PDF value
    for _ in range(64):
        if not isinstance(node, Dictionary):
            return None
        if key in node:
            return node[key]
        node = node.get("Parent")
    return None


def _revision(data: bytes, end: int, ends: list[int | None]) -> int | None:
    """Return the revision that ends at ``end``, or after only an end-of-line marker that follows it."""
    if end < 0:
        return None
    for index, revision_end in enumerate(ends):
        if revision_end is not None and 0 <= end <= revision_end and not data[end:revision_end].strip(b"\r\n"):
            return index
    return None


def _failure(problem: str, *, covers: bool = False, valid: bool | None = None) -> SignatureVerification:
    """Return the verification of a signature that no digest or certificate could be read for."""
    return SignatureVerification(
        intact=False,
        covers_revision=covers,
        valid=valid,
        trusted=None,
        signer=None,
        signing_time=None,
        timestamp=None,
        timestamp_valid=None,
        modifications="other",
        problems=(problem,),
    )


def _byte_range_bounds(
    byte_range: tuple[int, ...], origin: int, total_len: int
) -> tuple[int, int, int, int, int] | str:
    """Validate and resolve a 4-integer ``/ByteRange`` into ``(start, a, b, c, length)`` or an error message."""
    start, first, second, length = byte_range
    if min(start, first, second, length) < 0:
        return "the /ByteRange contains negative values"
    a, b, c = origin + start, origin + start + first, origin + second
    if b > c or c + length > total_len:
        return "the /ByteRange is outside the document or overlaps"
    return start, a, b, c, length


def _verify(signature: Signature, anchors: list[bytes] | None) -> SignatureVerification:  # noqa: C901, PLR0912
    problems: list[str] = []
    document = getattr(signature, "_document", None)
    found = None if document is None else _core.document_file(document._core())  # noqa: SLF001
    if found is None or len(signature.byte_range) != 4:  # noqa: PLR2004 - two ranges
        reason = "the document was not read from a file" if found is None else "the /ByteRange is not two ranges"
        return _failure(reason)
    data, origin, revisions = found
    bounds = _byte_range_bounds(signature.byte_range, origin, len(data))
    if isinstance(bounds, str):
        return _failure(bounds, covers=False, valid=False)
    start, a, b, c, length = bounds
    covers = (
        start == 0 and b < c and data[b : b + 1] == b"<" and data[c - 1 : c] == b">" and signature.revision is not None
    )
    if not covers:
        problems.append("the byte ranges do not cover the whole signed revision except /Contents")
    signed = data[a:b] + data[c : c + length]
    try:
        contents = bytes.fromhex(data[b + 1 : c - 1].decode("latin-1")) if b + 1 < c - 1 else b""
        if signature.is_timestamp:
            assert document is not None  # noqa: S101 - found came from document.
            return _verify_doc_timestamp(
                document,
                signature,
                signed,
                contents,
                covers=covers,
                revisions=revisions,
                data=data,
                end_offset=c + length,
                anchors=anchors,
                problems=problems,
            )
        parsed, token = _core.signature_read(contents)
    except LimitError:
        raise
    except ValueError:
        return _failure("the /Contents between the byte ranges is not a hexadecimal string", covers=covers, valid=False)
    except PdfError as error:
        return _failure(str(error), covers=covers, valid=False)
    digest_name, message_digest, signing_time, certificates, signer_index, check, _ = parsed
    digest = _digest(digest_name, signed)
    if digest is None:
        problems.append(f"the digest algorithm {digest_name} is not one that pyropdf computes")
    intact = digest is not None and message_digest is not None and digest == message_digest
    if check is None:
        valid: bool | None = False
        problems.append("the SignedData holds no certificate of its signer")
    else:
        valid = _check(check, signed, problems)
        if message_digest is None:
            intact = valid is True
    if not intact:
        problems.append("the signed bytes' digest does not match the signature's")
    if valid is False:
        problems.append("the CMS signature does not verify with the signer's certificate")
    signer = None if signer_index is None else _core.certificate_read(certificates[signer_index])[0]
    stamp_time, stamp_valid = (None, None) if token is None else _timestamp(token, problems)
    when = stamp_time or (None if signing_time is None else datetime.fromtimestamp(signing_time, UTC))
    trusted = None
    if anchors is not None:
        trusted = signer_index is not None and _trusted(
            certificates[signer_index], certificates, anchors, when or datetime.now(UTC), problems
        )
    later = {number for _, numbers in revisions[(signature.revision or 0) + 1 :] for number in numbers}
    assert document is not None  # noqa: S101 - found came from document.
    modifications = (
        _classify(document, signature.revision, data[: c + length], data, later)
        if signature.revision is not None and later
        else "none"
    )
    return SignatureVerification(
        intact=intact,
        covers_revision=covers,
        valid=valid,
        trusted=trusted,
        signer=signer,
        signing_time=None if signing_time is None else datetime.fromtimestamp(signing_time, UTC),
        timestamp=stamp_time,
        timestamp_valid=stamp_valid,
        modifications=modifications,
        problems=tuple(problems),
    )


def _digest_matches(alg: str, data: bytes, expected: bytes | None, problems: list[str]) -> bool:
    computed = _digest(alg, data)
    if computed is None:
        problems.append(f"the digest algorithm {alg} is not one that pyropdf computes")
        return False
    return expected is None or computed == expected


def _verify_doc_timestamp(  # noqa: PLR0913 - all pieces of one timestamp verification.
    document: Document,
    signature: Signature,
    signed: bytes,
    contents: bytes,
    *,
    covers: bool,
    revisions: list[tuple[int | None, list[int]]],
    data: bytes,
    end_offset: int,
    anchors: list[bytes] | None,
    problems: list[str],
) -> SignatureVerification:
    """Verify an RFC 3161 ``/DocTimeStamp`` signature over ``signed``."""
    try:
        tst_info_der, imprint_alg, hashed_message, stamp_time = parse_rfc3161_tst_info(contents)
        parsed, _ = _core.signature_read(contents)
    except LimitError:
        raise
    except (ValueError, PdfError) as error:
        return _failure(str(error), covers=covers, valid=False)
    digest_name, message_digest, signing_time, certificates, signer_index, check, _ = parsed
    imprint_ok = _digest_matches(imprint_alg, signed, hashed_message, problems)
    tst_ok = _digest_matches(digest_name, tst_info_der, message_digest, problems)
    intact = imprint_ok and tst_ok
    if check is None:
        valid: bool | None = False
        problems.append("the SignedData holds no certificate of its signer")
    else:
        valid = _check(check, tst_info_der, problems)
        if message_digest is None:
            intact = imprint_ok and valid is True
    if not intact:
        problems.append("the signed bytes' digest does not match the signature's")
    if valid is False:
        problems.append("the CMS signature does not verify with the signer's certificate")
    stamp_valid: bool | None = False if (not intact or valid is False) else valid
    if stamp_valid is False:
        problems.append("the time-stamp token does not verify for this signature")
    signer = None if signer_index is None else _core.certificate_read(certificates[signer_index])[0]
    trusted = None
    if anchors is not None:
        trusted = signer_index is not None and _trusted(
            certificates[signer_index], certificates, anchors, stamp_time, problems
        )
    later = {number for _, numbers in revisions[(signature.revision or 0) + 1 :] for number in numbers}
    modifications = (
        _classify(document, signature.revision, data[:end_offset], data, later)
        if signature.revision is not None and later
        else "none"
    )
    return SignatureVerification(
        intact=intact,
        covers_revision=covers,
        valid=valid,
        trusted=trusted,
        signer=signer,
        signing_time=None if signing_time is None else datetime.fromtimestamp(signing_time, UTC),
        timestamp=stamp_time,
        timestamp_valid=stamp_valid,
        modifications=modifications,
        problems=tuple(problems),
    )


def _digest(name: str, data: bytes) -> bytes | None:
    import hashlib  # noqa: PLC0415

    try:
        if name == "shake256":
            return hashlib.shake_256(data).digest(64)
        return hashlib.new(name, data).digest()
    except ValueError:
        return None


def _check(check: _core._Check, content: bytes, problems: list[str]) -> bool | None:
    """Return whether one signature verifies: ``None`` when pyropdf cannot check it."""
    key, scheme, digest_name, signature, message, _ = check
    signed_bytes = content if message is None else message
    prehash = _digest(digest_name, signed_bytes)
    if prehash is None:
        problems.append(f"the digest algorithm {digest_name} is not one that pyropdf computes")
        return None
    if scheme == "ecdsa":
        try:
            return _core.verify_ecdsa(key, prehash, signature)
        except ValueError:
            # P-521 and Brainpool keys use the same optional backend as RSA.
            pass
    if scheme not in ("ecdsa", "ed25519", "ed448", "rsa-pkcs1", "rsa-pss"):
        problems.append(f"the signature scheme {scheme} is not one that pyropdf verifies")
        return None
    return _check_external(check, signed_bytes, prehash, problems)


def _check_external(check: _core._Check, signed_bytes: bytes, prehash: bytes, problems: list[str]) -> bool | None:
    try:
        from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm  # noqa: PLC0415
    except ImportError:
        problems.append(f"{check[1]} verification with this key requires the cryptography package")
        return None
    try:
        return _verify_external_key(check, signed_bytes, prehash)
    except (ValueError, InvalidSignature):
        return False
    except UnsupportedAlgorithm as error:
        problems.append(str(error))
        return None


def _backend_hash(name: str) -> HashAlgorithm:
    from cryptography.exceptions import UnsupportedAlgorithm  # noqa: PLC0415
    from cryptography.hazmat.primitives import hashes  # noqa: PLC0415

    known = {
        "sha1": hashes.SHA1,
        "sha256": hashes.SHA256,
        "sha384": hashes.SHA384,
        "sha512": hashes.SHA512,
        "sha3_256": hashes.SHA3_256,
        "sha3_384": hashes.SHA3_384,
        "sha3_512": hashes.SHA3_512,
    }
    factory = known.get(name)
    if factory is None:
        msg = f"unsupported signature hash {name}"
        raise UnsupportedAlgorithm(msg)
    return factory()


def _verify_external_key(check: _core._Check, signed_bytes: bytes, prehash: bytes) -> bool:
    from cryptography.exceptions import UnsupportedAlgorithm  # noqa: PLC0415
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, padding, rsa  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric.utils import Prehashed  # noqa: PLC0415

    key, scheme, digest_name, signature, _, pss = check
    public = serialization.load_der_public_key(key)
    if (scheme == "ed25519" and isinstance(public, ed25519.Ed25519PublicKey)) or (
        scheme == "ed448" and isinstance(public, ed448.Ed448PublicKey)
    ):
        public.verify(signature, signed_bytes)
        return True
    if scheme == "ecdsa" and isinstance(public, ec.EllipticCurvePublicKey):
        if public.curve.name not in ("secp521r1", "brainpoolP256r1", "brainpoolP384r1", "brainpoolP512r1"):
            msg = f"unsupported ECDSA curve {public.curve.name}"
            raise UnsupportedAlgorithm(msg)
        public.verify(signature, prehash, ec.ECDSA(Prehashed(_backend_hash(digest_name))))
    elif scheme in ("rsa-pkcs1", "rsa-pss") and isinstance(public, rsa.RSAPublicKey):
        if pss is not None and pss[1] > (public.key_size + 6) // 8 - _backend_hash(digest_name).digest_size - 2:
            return False
        rsa_padding = (
            padding.PKCS1v15()
            if pss is None
            else padding.PSS(mgf=padding.MGF1(_backend_hash(pss[0])), salt_length=pss[1])
        )
        public.verify(signature, prehash, rsa_padding, Prehashed(_backend_hash(digest_name)))
    else:
        return False
    return True


def _timestamp(token: tuple[Any, bytes, int, bool], problems: list[str]) -> tuple[datetime, bool]:
    """Return the time of a signature time-stamp token and whether it stamps the signature and verifies."""
    signed, tst_info, seconds, imprint = token
    valid = imprint and _token_valid(signed, tst_info, problems)
    if not valid:
        problems.append("the time-stamp token does not verify for this signature")
    return datetime.fromtimestamp(seconds, UTC), valid


def _token_valid(signed: tuple[Any, ...], tst_info: bytes, problems: list[str]) -> bool:
    digest_name, message_digest, _, _, _, check, _ = signed
    return (
        check is not None
        and message_digest is not None
        and _digest(digest_name, tst_info) == message_digest
        and _check(check, tst_info, problems) is True
    )


def _trusted(
    signer: bytes, certificates: list[bytes], anchors: list[bytes], when: datetime, problems: list[str]
) -> bool:
    """Return whether ``signer`` chains to an anchor through ``certificates``, every link valid at ``when``."""
    pool = [*certificates, *anchors]
    infos = {certificate: _core.certificate_read(certificate) for certificate in pool}
    current = signer
    moment = when.timestamp()
    for _ in range(_MAX_CHAIN):
        _, subject, issuer, (not_before, not_after), check, key = infos[current]
        if not not_before <= moment <= not_after:
            problems.append(f"the certificate of {infos[current][0]} is not valid at the signing time")
            return False
        if any(infos[anchor][1] == subject and infos[anchor][5] == key for anchor in anchors):
            return True
        parent = next(
            (
                candidate
                for candidate in pool
                if infos[candidate][1] == issuer
                and candidate != current
                and _check((infos[candidate][5], *check[1:]), b"", []) is True
            ),
            None,
        )
        if parent is None:
            problems.append(f"no trusted certificate issued the certificate of {infos[current][0]}")
            return False
        current = parent
    problems.append("the certificate chain is longer than pyropdf follows")
    return False


def _classify_documents(before: Document, after: Document, written: set[int]) -> str:
    appearances = _appearance_objects(after)
    level = 0
    for number in sorted(written):
        old = before.objects.get(number) if number in before.objects else None
        new = after.objects.get(number) if number in after.objects else None
        if new is None:
            if old is not None:
                return "other"
            continue
        if old is not None and _shape(old, 0) == _shape(new, 0):
            continue
        level = max(level, _object_level(old, new, number in appearances))
        if level == len(_LEVELS) - 1:
            break
    return _LEVELS[level]


def _classify(
    document: Document,
    revision: int,
    before_data: bytes,
    after_data: bytes,
    written: set[int],
) -> str:
    """Return how the objects written after the signed revision modify it, as pyHanko's levels name them."""
    from pyropdf._document import open as open_document  # noqa: PLC0415

    revs = document.revisions
    if 0 <= revision < len(revs):
        with revs[revision].open() as before, revs[-1].open() as after:
            return _classify_documents(before, after, written)
    with (
        open_document(before_data, limits=document.limits) as before,
        open_document(after_data, limits=document.limits) as after,
    ):
        return _classify_documents(before, after, written)


def _is_invisible_timestamp_widget(item: object) -> bool:
    """Return whether ``item`` is an invisible widget annotation of a ``/DocTimeStamp`` field."""
    if not isinstance(item, Dictionary) or isinstance(item, Stream):
        return False
    if item.get("Subtype") != "Widget" or item.get("FT") != "Sig" or "AP" in item:
        return False
    rect = item.get("Rect")
    if not isinstance(rect, Array) or len(rect) != 4:  # noqa: PLR2004
        return False
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) == 0.0 for v in rect):
        return False
    val = item.get("V")
    return isinstance(val, Dictionary) and is_timestamp_sig(val)


def _fields_only_added_invisible_timestamps(old: Any, new: Any) -> bool:  # noqa: ANN401
    old_refs = [item.xref for item in old or () if isinstance(item, Dictionary)]
    new_items = [item for item in new or () if isinstance(item, Dictionary)]
    kept = {item.xref for item in new_items}
    if len(old_refs) != len(old or ()) or not kept.issuperset(old_refs):
        return False
    added = [item for item in new_items if item.xref not in set(old_refs)]
    return all(_is_invisible_timestamp_widget(item) for item in added)


def _acroform_level(old: Any, new: Any) -> int:  # noqa: ANN401
    if not isinstance(new, Dictionary):
        return 2
    if old is None:
        changed = set(new)
        old_fields = None
    elif isinstance(old, Dictionary):
        changed = {key for key in set(old) | set(new) if _shape(old.get(key), 1) != _shape(new.get(key), 1)}
        old_fields = old.get("Fields")
    else:
        return 2
    if changed <= {"Fields", "SigFlags"} and _fields_only_added_invisible_timestamps(old_fields, new.get("Fields")):
        return 0
    return 1 if changed <= _FILLING_KEYS else 2


def _changed_dict_level(kind: object, old: Dictionary, new: Dictionary, changed: set[str]) -> int:
    if kind == "Catalog":
        rest = changed - _SIGNING_KEYS
        if not rest:
            return 0
        return _acroform_level(old.get("AcroForm"), new.get("AcroForm")) if rest == {"AcroForm"} else 2
    if kind == "DSS":
        return 0 if changed <= {"Type", "VRI", "Certs", "OCSPs", "CRLs"} else 2
    if kind == "Page":
        return _annotations_level(old.get("Annots"), new.get("Annots")) if changed <= {"Annots"} else 2
    if changed <= {"Fields", "SigFlags"} and _fields_only_added_invisible_timestamps(
        old.get("Fields"), new.get("Fields")
    ):
        return 0
    return 1 if changed <= _FILLING_KEYS else 2


def _object_level(old: Any, new: Any, appearance: bool) -> int:  # noqa: ANN401, FBT001
    """Return the level of one written object: ``old`` is ``None`` for a new one."""
    kind = new.get("Type") if isinstance(new, Dictionary) else None
    subtype = new.get("Subtype") if isinstance(new, Dictionary) else None
    annotation = isinstance(new, Dictionary) and not isinstance(new, Stream) and "Rect" in new and subtype is not None
    if annotation and subtype != "Widget":
        return 2
    if old is None:
        if _is_invisible_timestamp_widget(new):
            return 0
        return 1 if annotation or (isinstance(new, Dictionary) and ("FT" in new or kind == "Sig")) else 0
    if not isinstance(new, Dictionary) or not isinstance(old, Dictionary):
        return 2
    if isinstance(new, Stream):
        return 1 if appearance and subtype == "Form" else 2
    changed = {key for key in set(old) | set(new) if _shape(old.get(key), 1) != _shape(new.get(key), 1)}
    return _changed_dict_level(kind, old, new, changed)


def _annotations_level(old: Any, new: Any) -> int:  # noqa: ANN401
    """Return the level of a page's changed ``/Annots``: form filling when it only adds widgets."""
    old_refs = [item.xref for item in old or () if isinstance(item, Dictionary)]
    new_items = [item for item in new or () if isinstance(item, Dictionary)]
    kept = {item.xref for item in new_items}
    if len(old_refs) != len(old or ()) or not kept.issuperset(old_refs):
        return 2
    added = [item for item in new_items if item.xref not in set(old_refs)]
    if all(_is_invisible_timestamp_widget(item) for item in added):
        return 0
    return 1 if all(item.get("Subtype") == "Widget" for item in added) else 2


def _appearance_objects(document: Document) -> set[int]:
    """Return the objects that the annotations' appearance dictionaries name directly or in their states."""
    out: set[int] = set()
    for page in document.pages:
        annotations = page.obj.get("Annots")
        for annotation in annotations if isinstance(annotations, Array) else ():
            appearance = annotation.get("AP") if isinstance(annotation, Dictionary) else None
            if not isinstance(appearance, Dictionary):
                continue
            for state in appearance.values():
                values = state.values() if isinstance(state, Dictionary) and not isinstance(state, Stream) else [state]
                for count, value in enumerate(values):
                    if count == _MAX_APPEARANCE_OBJECTS:
                        break
                    if isinstance(value, Stream) and value.xref is not None:
                        out.add(value.xref)
    return out


def _shape(value: Any, depth: int) -> Any:  # noqa: ANN401
    """Return a comparable form of a PDF value: references by number, streams with their stored data."""
    if isinstance(value, (Dictionary, Array)):
        if depth and value.is_indirect:
            return ("ref", value.xref)
        if depth > _MAX_SHAPE_DEPTH:
            return ("deep",)
        if isinstance(value, Array):
            return tuple(_shape(item, depth + 1) for item in value)
        items = tuple(sorted((str(key), _shape(item, depth + 1)) for key, item in value.items()))
        return (items, value.read(decode=False, max_size=None)) if isinstance(value, Stream) else items
    return (type(value).__name__, value)
