"""Signing documents: PAdES B-B and B-T signatures (ETSI EN 319 142-1).

pyropdf signs; it does not validate. A validator such as pyHanko decides
whether a signature is intact and whether its certificates are trusted.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeAlias

from pyropdf import _core
from pyropdf._core import _Signer
from pyropdf._form import FieldType
from pyropdf._metadata import pdf_date
from pyropdf._objects import Array, Dictionary, Name

if TYPE_CHECKING:
    from pyropdf._document import Document

#: A time-stamp callback: it receives the digest of the signature value and
#: the name of its hash algorithm (``"sha256"`` or ``"sha384"``) and returns
#: the DER of an RFC 3161 time-stamp token over that digest.
Timestamper: TypeAlias = Callable[[bytes, str], bytes]

#: The bytes reserved for the CMS signature by default.
DEFAULT_RESERVE = 16_384
#: The most bytes that ``reserve`` may reserve.
_MAX_RESERVE = 1 << 20
#: The ``/ByteRange`` that the saved document holds until the core fills it.
_BYTE_RANGE_PLACEHOLDER = (0, 9_999_999_999, 9_999_999_999, 9_999_999_999)
#: The widget flags of an invisible signature: Print and Locked.
_INVISIBLE_WIDGET_FLAGS = 132
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

    :meth:`from_pem` and :meth:`from_pkcs12` read ECDSA P-256 and P-384
    keys, which pyropdf signs with itself. :meth:`callback` signs with a
    key that stays outside pyropdf: an RSA key, an HSM, or PKCS#11.
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
    def from_pem(cls, key: bytes | str, certificates: bytes | str, *, password: bytes | str | None = None) -> Signer:
        """Read a PEM private key and the PEM certificate chain whose first certificate holds its public key.

        ``key`` is a ``PRIVATE KEY`` (PKCS#8), an ``ENCRYPTED PRIVATE KEY``
        that ``password`` decrypts, or an ``EC PRIVATE KEY`` (SEC1), on P-256
        or P-384. ``certificates`` holds the signer's certificate first,
        then any intermediate certificates to embed. A key that pyropdf does
        not sign with, a wrong password, or a certificate of another key
        raises :class:`ValueError`.
        """
        secret = None if password is None else _bytes("password", password)
        return cls._wrap(_Signer.from_pem(_bytes("key", key), _bytes("certificates", certificates), secret))

    @classmethod
    def from_pkcs12(cls, data: bytes, password: str | None = None) -> Signer:
        """Read the ECDSA key and the certificates of a PKCS#12 file.

        The file is PBES2-encrypted with a SHA-2 MAC, as OpenSSL 3 and
        Python's ``cryptography`` write it; ``None`` is the empty password.
        Legacy encryption, a wrong password, or a file without exactly one
        key and its certificate raises :class:`ValueError`.
        """
        if password is not None and not isinstance(password, str):
            msg = f"password must be a str or None: {type(password).__name__}"
            raise TypeError(msg)
        return cls._wrap(_Signer.from_pkcs12(_bytes("data", data), password or ""))

    @classmethod
    def callback(cls, certificates: Sequence[bytes], sign: Callable[[bytes], bytes]) -> Signer:
        """Sign with ``sign``, which holds the key of the first of the DER ``certificates``.

        ``sign`` receives the bytes to sign, the DER of the signed
        attributes, and returns their signature: PKCS#1 v1.5 with SHA-256
        for an RSA key, and the DER of an ECDSA signature with SHA-256 for a
        P-256 key or SHA-384 for a P-384 key. Its exceptions propagate from
        :meth:`Document.sign`.
        """
        if not callable(sign):
            msg = f"sign must be callable: {type(sign).__name__}"
            raise TypeError(msg)
        if isinstance(certificates, (bytes, str)):
            msg = "certificates must be a sequence of DER certificates, not one"
            raise TypeError(msg)
        chain = [_bytes("a certificate", certificate) for certificate in certificates]
        return cls._wrap(_Signer.callback(chain, sign))

    @property
    def certificates(self) -> tuple[bytes, ...]:
        """Return the DER certificates that the signature embeds, the signer's first."""
        return tuple(self._core.certificates)

    @property
    def digest_algorithm(self) -> str:
        """Return the name of the signature's digest algorithm: ``"sha256"`` or ``"sha384"``."""
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
    form = document.form
    if field in form:
        existing = form[field]
        if existing.type is not FieldType.SIGNATURE:
            msg = f"field {field!r} is a {existing.type.value} field, not a signature field"
            raise ValueError(msg)
        if existing.is_signed:
            msg = f"field {field!r} is already signed"
            raise ValueError(msg)
        existing.obj["V"] = signature
    else:
        if not field or "." in field:
            msg = f"a new signature field needs a name without dots: {field!r}"
            raise ValueError(msg)
        if len(document.pages) == 0:
            msg = "a document without pages has no page for the signature field's widget"
            raise ValueError(msg)
        page = document.pages[0].obj
        widget = document.objects[
            document.objects.add(
                {
                    "Type": Name("Annot"),
                    "Subtype": Name("Widget"),
                    "FT": Name("Sig"),
                    "T": field,
                    "Rect": [0, 0, 0, 0],
                    "F": _INVISIBLE_WIDGET_FLAGS,
                    "P": page,
                    "V": signature,
                }
            )
        ]
        annotations = page.get("Annots")
        if isinstance(annotations, Array):
            annotations.append(widget)
        else:
            page["Annots"] = [widget]
        acroform = document.catalog.get("AcroForm")
        if not isinstance(acroform, Dictionary):
            document.catalog["AcroForm"] = {"Fields": [widget]}
            acroform = document.catalog["AcroForm"]
        else:
            fields = acroform.get("Fields")
            if isinstance(fields, Array):
                fields.append(widget)
            else:
                acroform["Fields"] = [widget]
    acroform = document.catalog["AcroForm"]
    flags = acroform.get("SigFlags")
    acroform["SigFlags"] = (flags if isinstance(flags, int) and not isinstance(flags, bool) else 0) | _SIGNATURE_FLAGS


def sign(  # noqa: PLR0913 - the signature's options are all keyword-only
    document: Document,
    field: str,
    signer: Signer,
    *,
    timestamper: Timestamper | None,
    reason: str | None,
    location: str | None,
    contact: str | None,
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
    if isinstance(reserve, bool) or not isinstance(reserve, int):
        msg = f"reserve must be an integer: {reserve!r}"
        raise TypeError(msg)
    if not 0 < reserve <= _MAX_RESERVE:
        msg = f"reserve must be from 1 through {_MAX_RESERVE} bytes: {reserve}"
        raise ValueError(msg)
    entries: dict[str, Any] = {
        "Type": Name("Sig"),
        "Filter": Name("Adobe.PPKLite"),
        "SubFilter": Name("ETSI.CAdES.detached"),
        # The core finds these two entries in the saved bytes as written here, in this order.
        "ByteRange": list(_BYTE_RANGE_PLACEHOLDER),
        "Contents": b"0" * (2 * reserve),
        "M": pdf_date(datetime.now(UTC)),
    }
    for key, name, value in (
        ("Reason", "reason", reason),
        ("Location", "location", location),
        ("ContactInfo", "contact", contact),
    ):
        text = _optional_text(name, value)
        if text is not None:
            entries[key] = text
    document._ensure_open()  # noqa: SLF001
    if document.encryption_method is not None:
        msg = "pyropdf does not sign encrypted documents"
        raise ValueError(msg)
    with document.transaction("sign"):
        signature = document.objects[document.objects.add(entries)]
        _signature_field(document, field, signature)
    try:
        data = document.to_bytes(incremental=True)
        signed = _core.sign_document(data, reserve, signer._core, timestamper)  # noqa: SLF001
    except BaseException:
        document.history.undo()
        raise
    document._reopen(signed)  # noqa: SLF001
    return signed
