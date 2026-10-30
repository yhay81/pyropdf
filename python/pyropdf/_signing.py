"""Signing documents: PAdES B-B and B-T signatures (ETSI EN 319 142-1).

:attr:`Document.signatures` lists the signatures of an opened file, and
:meth:`Signature.verify` checks one: its digest, its CMS signature, its
time-stamp, its chain to the trust anchors given, and what later revisions
modify. Revocation and long-term validation are left to a validator such as
pyHanko.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeAlias

from pyropdf import _core
from pyropdf._core import _Signer
from pyropdf._errors import PdfError
from pyropdf._form import FieldType
from pyropdf._metadata import pdf_date
from pyropdf._objects import Array, Dictionary, Name, Stream

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
    core = document._core()  # noqa: SLF001
    _core.begin_signing(core)
    try:
        signature = document.objects[document.objects.add(entries)]
        _signature_field(document, field, signature)
        signed = _core.sign_document(core, reserve, signer._core, timestamper)  # noqa: SLF001
    except BaseException:
        # Signing ends its transaction only once it replaced the document.
        _core.end_transaction(core, False)  # noqa: FBT003 - the core's flag
        raise
    document._drawn = None  # noqa: SLF001 - what canvases drew lived in the replaced document.
    return signed


#: The levels of modification after a signed revision, least first, as pyHanko's default diff policy reports
#: them: it has no rule that explains an annotation, so an added or changed one other than a widget is "other".
_LEVELS = ("none", "form filling", "other")
#: The keys of a field or widget that filling a form changes.
_FILLING_KEYS = frozenset({"V", "AS", "AP", "MK", "Kids", "Fields", "NeedAppearances", "SigFlags", "DR", "DA"})
#: The keys of the catalog that a signature or its validation data adds.
_SIGNING_KEYS = frozenset({"DSS", "Extensions", "Perms"})
#: The most certificates in one chain.
_MAX_CHAIN = 16
#: The most levels of nested containers that comparing two revisions of an object reads.
_MAX_SHAPE_DEPTH = 32
#: The most objects that one annotation's appearance reaches, which the classification walks.
_MAX_APPEARANCE_OBJECTS = 64


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class Signature:
    """A signature of the opened file: its field, its signature dictionary, and the revision that it signs.

    ``revision`` counts the file's revisions from 0, the oldest, and is
    ``None`` when the byte ranges end where no revision does.
    """

    field: str
    xref: int | None
    byte_range: tuple[int, ...]
    revision: int | None
    _document: Document = field(repr=False, compare=False)

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
        ranges = tuple(int(part) for part in byte_range) if isinstance(byte_range, Array) else ()
        revision = None
        if len(ranges) == 4 and found is not None:  # noqa: PLR2004 - two ranges of start and length
            revision = _revision(found[0], origin + ranges[2] + ranges[3], ends)
        out.append(Signature(name, value.xref, ranges, revision, document))
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
    for index, revision_end in enumerate(ends):
        if revision_end is not None and end <= revision_end and not data[end:revision_end].strip(b"\r\n"):
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


def _verify(signature: Signature, anchors: list[bytes] | None) -> SignatureVerification:  # noqa: C901
    problems: list[str] = []
    found = _core.document_file(signature._document._core())  # noqa: SLF001
    if found is None or len(signature.byte_range) != 4:  # noqa: PLR2004 - two ranges
        reason = "the document was not read from a file" if found is None else "the /ByteRange is not two ranges"
        return _failure(reason)
    data, origin, revisions = found
    start, first, second, length = signature.byte_range
    a, b, c = origin + start, origin + start + first, origin + second
    covers = (
        start == 0
        and first >= 0
        and length >= 0
        and b < c <= len(data)
        and c + length <= len(data)
        and data[b : b + 1] == b"<"
        and data[c - 1 : c] == b">"
        and signature.revision is not None
    )
    if not covers:
        problems.append("the byte ranges do not cover the whole signed revision except /Contents")
    signed = data[a:b] + data[c : c + length]
    try:
        contents = bytes.fromhex(data[b + 1 : c - 1].decode("latin-1")) if b + 1 < c - 1 else b""
        parsed, token = _core.signature_read(contents)
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
    modifications = _classify(data[: c + length], data, later) if signature.revision is not None and later else "none"
    return SignatureVerification(
        intact,
        covers,
        valid,
        trusted,
        signer,
        None if signing_time is None else datetime.fromtimestamp(signing_time, UTC),
        stamp_time,
        stamp_valid,
        modifications,
        tuple(problems),
    )


def _digest(name: str, data: bytes) -> bytes | None:
    import hashlib  # noqa: PLC0415

    try:
        return hashlib.new(name, data).digest()
    except ValueError:
        return None


def _check(  # noqa: PLR0911
    check: tuple[bytes, str, str, bytes, bytes | None], content: bytes, problems: list[str]
) -> bool | None:
    """Return whether one signature verifies: ``None`` when pyropdf cannot check it."""
    key, scheme, digest_name, signature, message = check
    prehash = _digest(digest_name, content if message is None else message)
    if prehash is None:
        problems.append(f"the digest algorithm {digest_name} is not one that pyropdf computes")
        return None
    if scheme == "ecdsa":
        try:
            return _core.verify_ecdsa(key, prehash, signature)
        except ValueError as error:
            problems.append(str(error))
            return None
    if scheme not in ("rsa-pkcs1", "rsa-pss"):
        problems.append(f"the signature scheme {scheme} is not one that pyropdf verifies")
        return None
    try:
        from cryptography.exceptions import InvalidSignature  # noqa: PLC0415
        from cryptography.hazmat.primitives import hashes, serialization  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric.utils import Prehashed  # noqa: PLC0415
    except ImportError:
        problems.append("RSA signatures verify through the cryptography package, which is not installed")
        return None
    public = serialization.load_der_public_key(key)
    algorithm = {"sha1": hashes.SHA1, "sha256": hashes.SHA256, "sha384": hashes.SHA384, "sha512": hashes.SHA512}[
        digest_name
    ]()
    if not isinstance(public, rsa.RSAPublicKey):
        return False
    scheme_padding = (
        padding.PKCS1v15()
        if scheme == "rsa-pkcs1"
        else padding.PSS(mgf=padding.MGF1(algorithm), salt_length=padding.PSS.AUTO)
    )
    try:
        public.verify(signature, prehash, scheme_padding, Prehashed(algorithm))
    except InvalidSignature:
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


def _classify(before_data: bytes, after_data: bytes, written: set[int]) -> str:
    """Return how the objects written after the signed revision modify it, as pyHanko's levels name them."""
    from pyropdf._document import open as open_document  # noqa: PLC0415

    before, after = open_document(before_data), open_document(after_data)
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


def _object_level(old: Any, new: Any, appearance: bool) -> int:  # noqa: ANN401, FBT001, PLR0911
    """Return the level of one written object: ``old`` is ``None`` for a new one."""
    kind = new.get("Type") if isinstance(new, Dictionary) else None
    subtype = new.get("Subtype") if isinstance(new, Dictionary) else None
    annotation = isinstance(new, Dictionary) and not isinstance(new, Stream) and "Rect" in new and subtype is not None
    if annotation and subtype != "Widget":
        return 2
    if old is None:
        return 1 if annotation or (isinstance(new, Dictionary) and ("FT" in new or kind == "Sig")) else 0
    if not isinstance(new, Dictionary) or not isinstance(old, Dictionary):
        return 2
    if isinstance(new, Stream):
        return 1 if appearance and subtype == "Form" else 2
    changed = {key for key in set(old) | set(new) if _shape(old.get(key), 1) != _shape(new.get(key), 1)}
    if kind == "Catalog":
        rest = changed - _SIGNING_KEYS
        return 0 if not rest else 1 if rest <= {"AcroForm"} else 2
    if kind == "Page":
        return _annotations_level(old.get("Annots"), new.get("Annots")) if changed <= {"Annots"} else 2
    return 1 if changed <= _FILLING_KEYS else 2


def _annotations_level(old: Any, new: Any) -> int:  # noqa: ANN401
    """Return the level of a page's changed ``/Annots``: form filling when it only adds widgets."""
    old_refs = [item.xref for item in old or () if isinstance(item, Dictionary)]
    new_items = [item for item in new or () if isinstance(item, Dictionary)]
    kept = {item.xref for item in new_items}
    if len(old_refs) != len(old or ()) or not kept.issuperset(old_refs):
        return 2
    added = [item for item in new_items if item.xref not in set(old_refs)]
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
