"""Passwords, permissions, and the encryption of saves."""

from __future__ import annotations

import enum
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import cache
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar, Literal

from pyropdf._errors import LimitError, MalformedPdfError, PasswordError

if TYPE_CHECKING:
    from asn1crypto import cms, core
    from asn1crypto import x509 as asn1_x509
    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
    from cryptography.hazmat.primitives.asymmetric.padding import AsymmetricPadding
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
    from cryptography.hazmat.primitives.hashes import HashAlgorithm
    from cryptography.x509 import Certificate

#: The longest password, 127 UTF-8 bytes (ISO 32000-2 §7.6.4.3.3).
_MAX_PASSWORD_BYTES = 127
_MIN_RSA_BITS = 2048
_MAX_RSA_BITS = 8192
_RECIPIENT_PAYLOAD_SIZE = 24
_MAX_RECIPIENTS = 128
#: RC4 methods, which pyropdf reads but never writes.
_RC4_METHODS = frozenset({"rc4", "rc4-40", "rc4-128"})


class Permissions(enum.IntFlag):
    """What an encrypted document permits the user password, combined with ``|``.

    :attr:`Document.permissions` reports them and ``Encryption(permissions=)``
    sets them. The values are the ``/P`` bits of ISO 32000-2 Table 22, and
    ``ALL`` combines every member.
    """

    PRINT = 1 << 2
    MODIFY = 1 << 3
    COPY = 1 << 4
    ANNOTATE = 1 << 5
    FORM = 1 << 8
    ACCESSIBILITY = 1 << 9
    ASSEMBLE = 1 << 10
    PRINT_HQ = 1 << 11
    ALL = PRINT | MODIFY | COPY | ANNOTATE | FORM | ACCESSIBILITY | ASSEMBLE | PRINT_HQ


class Access(enum.IntEnum):
    """What a password opened: the user's restricted access, or the owner's full access."""

    USER = 1
    OWNER = 2


def _password_bytes(password: object, label: str = "password") -> bytes:
    """Return ``password`` in UTF-8, refusing a non-str and more than 127 bytes before any key derivation."""
    if not isinstance(password, str):
        msg = f"{label} must be a str: {password!r}"
        raise TypeError(msg)
    # A character takes at most 4 bytes: a short password needs no encoding to check.
    if len(password) * 4 > _MAX_PASSWORD_BYTES and len(password.encode()) > _MAX_PASSWORD_BYTES:
        code = "password_input_size"
        msg = f"{label} exceeds the {_MAX_PASSWORD_BYTES}-byte UTF-8 limit"
        raise LimitError(code, msg)
    return password.encode()


@dataclass(frozen=True, slots=True, kw_only=True)
class Encryption:
    """How :meth:`Document.save` and :meth:`Document.to_bytes` encrypt a full save.

    The user password opens the document with the rights in ``permissions``,
    and the owner password opens it with every right. ``owner_password``
    defaults to ``user_password``. Each password is capped at 127 UTF-8 bytes.

    ``method`` is ``"aes-256"``, the AES-256 encryption of PDF 2.0 (revision
    6), or ``"aes-128"``, AES-128 with crypt filters (revision 4). pyropdf
    reads RC4 but never writes it. :attr:`Encryption.KEEP` keeps the
    encryption that the document was opened with, copying its untouched
    encrypted bytes. Without ``encryption``, a full save writes the document
    decrypted, and an incremental save keeps its encryption.
    """

    #: Keep the encryption that the document was opened with.
    KEEP: ClassVar[Encryption]

    user_password: str = ""
    owner_password: str | None = None
    permissions: Permissions = Permissions.ALL
    method: Literal["aes-256", "aes-128", "keep"] = "aes-256"

    def __post_init__(self) -> None:
        """Validate the passwords, permissions, and method."""
        _password_bytes(self.user_password, "user_password")
        if self.owner_password is not None:
            _password_bytes(self.owner_password, "owner_password")
        if isinstance(self.permissions, bool) or not isinstance(self.permissions, int):
            msg = f"permissions must be Permissions flags: {self.permissions!r}"
            raise TypeError(msg)
        object.__setattr__(self, "permissions", Permissions(int(self.permissions) & Permissions.ALL))
        if isinstance(self.method, str) and self.method.lower() in _RC4_METHODS:
            msg = f"pyropdf does not write {self.method} encryption, which PDF 2.0 deprecates; use 'aes-256'"
            raise ValueError(msg)
        if self.method not in ("aes-256", "aes-128", "keep"):
            msg = f"method must be 'aes-256' or 'aes-128': {self.method!r}"
            raise ValueError(msg)
        defaults = (self.user_password, self.owner_password, self.permissions) == ("", None, Permissions.ALL)
        if self.method == "keep" and not defaults:
            msg = "Encryption.KEEP takes the passwords and permissions of the document; pass none"
            raise ValueError(msg)

    def __repr__(self) -> str:
        """Return ``Encryption.KEEP`` for the constant, else the fields without the passwords."""
        if self.method == "keep":
            return "Encryption.KEEP"
        return f"Encryption(permissions={self.permissions!r}, method={self.method!r})"

    def _core(self) -> str | tuple[bytes, bytes, int, bool, bytes]:
        """Return what the core takes: ``"keep"``, or the passwords, flags, method, and 64 random bytes."""
        if self.method == "keep":
            return "keep"
        owner = self.user_password if self.owner_password is None else self.owner_password
        return (
            self.user_password.encode(),
            owner.encode(),
            int(self.permissions),
            self.method == "aes-256",
            os.urandom(64),
        )


Encryption.KEEP = Encryption(method="keep")


def _digest(name: str) -> HashAlgorithm:
    from cryptography.hazmat.primitives import hashes  # noqa: PLC0415

    algorithms = {
        "sha1": hashes.SHA1,
        "sha224": hashes.SHA224,
        "sha256": hashes.SHA256,
        "sha384": hashes.SHA384,
        "sha512": hashes.SHA512,
    }
    return algorithms[name]()


def _rsa_padding(algorithm: cms.KeyEncryptionAlgorithm) -> AsymmetricPadding:
    from cryptography.hazmat.primitives.asymmetric import padding  # noqa: PLC0415

    name = algorithm["algorithm"].native
    if name == "rsaes_pkcs1v15":
        if algorithm["parameters"].native is not None:
            msg = "unexpected PKCS#1 v1.5 transport parameters"
            raise ValueError(msg)
        return padding.PKCS1v15()
    if name != "rsaes_oaep":
        msg = "unsupported CMS key transport"
        raise ValueError(msg)
    params = algorithm["parameters"]
    mask = params["mask_gen_algorithm"]
    source = params["p_source_algorithm"]
    if mask["algorithm"].native != "mgf1" or source["algorithm"].native != "p_specified":
        msg = "unsupported OAEP mask or source"
        raise ValueError(msg)
    digest = params["hash_algorithm"]
    mgf_digest = mask["parameters"]
    if digest["parameters"].native is not None or mgf_digest["parameters"].native is not None:
        msg = "invalid OAEP hash parameters"
        raise ValueError(msg)
    return padding.OAEP(
        mgf=padding.MGF1(_digest(mgf_digest["algorithm"].native)),
        algorithm=_digest(digest["algorithm"].native),
        label=source["parameters"].native,
    )


_MAX_CMS_BYTES = 65_536
_ECDH_DIGEST = {
    "1.3.133.16.840.63.0.2": "sha1",
    "1.3.132.1.11.0": "sha224",
    "1.3.132.1.11.1": "sha256",
    "1.3.132.1.11.2": "sha384",
    "1.3.132.1.11.3": "sha512",
}
_AES_WRAP = {128: "2.16.840.1.101.3.4.1.5", 192: "2.16.840.1.101.3.4.1.25", 256: "2.16.840.1.101.3.4.1.45"}


@cache
def _shared_info_type() -> type[core.Sequence]:
    from asn1crypto import cms, core  # noqa: PLC0415

    # RFC 5753 §7.2, including the optional entityUInfo and explicit key size.
    class SharedInfo(core.Sequence):
        _fields = [  # noqa: RUF012 - asn1crypto initializes its class schema from this list.
            ("key_info", cms.KeyEncryptionAlgorithm),
            ("entity_u", core.OctetString, {"explicit": 0, "optional": True}),
            ("public_info", core.OctetString, {"explicit": 2}),
        ]

    return SharedInfo


def _ecdh_key(secret: bytes, digest: str, wrap: cms.KeyEncryptionAlgorithm, ukm: bytes | None) -> bytes:
    from asn1crypto import core  # noqa: PLC0415
    from cryptography.hazmat.primitives.kdf.x963kdf import X963KDF  # noqa: PLC0415

    size = next((bits for bits, oid in _AES_WRAP.items() if wrap["algorithm"].dotted == oid), None)
    if size is None or not isinstance(wrap["parameters"], core.Void):
        msg = "unsupported CMS AES key wrap parameters"
        raise ValueError(msg)
    info = _shared_info_type()({"key_info": wrap, "entity_u": ukm, "public_info": size.to_bytes(4, "big")})
    return X963KDF(algorithm=_digest(digest), length=size // 8, sharedinfo=info.dump()).derive(secret)


def _identifier_matches(
    rid: cms.RecipientIdentifier | cms.KeyAgreementRecipientIdentifier, cert: asn1_x509.Certificate
) -> bool:
    if rid.name == "issuer_and_serial_number":
        named = rid.chosen
        return named["serial_number"].native == cert.serial_number and named["issuer"] == cert.issuer
    if rid.name == "r_key_id":
        identifier = rid.chosen["subject_key_identifier"].native
    elif rid.name == "subject_key_identifier":
        identifier = rid.chosen.native
    else:
        return False
    return cert.key_identifier is not None and identifier == cert.key_identifier


def _recipient_key(
    info: cms.RecipientInfo, cert: asn1_x509.Certificate, credential: RecipientCredential
) -> bytes | None:
    from cryptography.hazmat.primitives.asymmetric import ec, rsa  # noqa: PLC0415

    recipient = info.chosen
    key = credential.private_key
    if info.name == "ktri" and isinstance(key, rsa.RSAPrivateKey):
        rid = recipient["rid"]
        if not _identifier_matches(rid, cert):
            return None
        if recipient["version"].native != ("v0" if rid.name == "issuer_and_serial_number" else "v2"):
            return None
        return key.decrypt(recipient["encrypted_key"].native, _rsa_padding(recipient["key_encryption_algorithm"]))
    if info.name != "kari" or not isinstance(key, ec.EllipticCurvePrivateKey) or recipient["version"].native != "v3":
        return None
    return _agreement_key(recipient, cert, key)


def _agreement_key(
    recipient: cms.KeyAgreeRecipientInfo, cert: asn1_x509.Certificate, key: EllipticCurvePrivateKey
) -> bytes | None:
    from asn1crypto import cms  # noqa: PLC0415
    from cryptography.hazmat.primitives import keywrap  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import ec  # noqa: PLC0415

    encrypted = next(
        (item for item in recipient["recipient_encrypted_keys"] if _identifier_matches(item["rid"], cert)), None
    )
    originator = recipient["originator"]
    if encrypted is None or originator.name != "originator_key":
        return None
    public = originator.chosen
    if public["algorithm"]["algorithm"].native != "ec":
        return None
    curve = public["algorithm"]["parameters"].native
    if curve is not None and curve != key.curve.name:
        return None
    ephemeral = ec.EllipticCurvePublicKey.from_encoded_point(key.curve, public["public_key"].native)
    algorithm = recipient["key_encryption_algorithm"]
    digest = _ECDH_DIGEST[algorithm["algorithm"].dotted]
    wrap = cms.KeyEncryptionAlgorithm.load(algorithm["parameters"].dump(), strict=True)
    wrapping = _ecdh_key(key.exchange(ec.ECDH(), ephemeral), digest, wrap, recipient["ukm"].native)
    return keywrap.aes_key_unwrap(wrapping, encrypted["encrypted_key"].native)


# RFC 2268 §6 assigns the CMS parameter version at index effective_key_bits.
# Only entries 1 through 128 can occur within the PDF recipient-cipher limit.
_RC2_VERSIONS = bytes.fromhex(
    "bd56eaf2a2f1ac2ab093d19c1b33fdd03004b6dc7ddf324bf7cb459b31bb215a"
    "419fe1d94a4d9edaa0682cc3275f80363eeefb951afecea834a913f0a63fd80c"
    "7824af2352c16717f56690e7e807b86048e61e53f392a4728c08156e860084fa"
    "f47f8a4219f6dbcd148d5012ba3c064eecb33511a1888e2b9499b77174d3e4bf3a"
)


def _decrypt_content(info: cms.EncryptedContentInfo, key: bytes) -> bytes:
    from cryptography.hazmat.decrepit.ciphers import algorithms as legacy  # noqa: PLC0415
    from cryptography.hazmat.primitives import padding  # noqa: PLC0415
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: PLC0415

    from pyropdf._core import decrypt_legacy_cms  # noqa: PLC0415

    algorithm = info["content_encryption_algorithm"]
    name = algorithm["algorithm"].native
    params = algorithm["parameters"]
    data = info["encrypted_content"].native
    if name == "rc4" and params.native in (None, b""):
        return decrypt_legacy_cms(key, data)
    if name == "rc2":
        version = params["rc2_parameter_version"].native
        bits = 32 if version is None else _RC2_VERSIONS.index(version, 1)
        return decrypt_legacy_cms(key, data, (bits, params["iv"].native))
    lengths = {"aes128_cbc": 16, "aes192_cbc": 24, "aes256_cbc": 32, "tripledes_3key": 24, "des": 8}
    if len(key) != lengths[name]:
        msg = "the CMS content key length disagrees with its algorithm"
        raise ValueError(msg)
    if name in {"tripledes_3key", "des"}:
        cipher = legacy.TripleDES(key * 3 if name == "des" else key)
        iv = params.native
    else:
        cipher = algorithms.AES(key)
        iv = params.native
    operation = Cipher(cipher, modes.CBC(iv)).decryptor()
    block = operation.update(data) + operation.finalize()
    unpad = padding.PKCS7(cipher.block_size).unpadder()
    return unpad.update(block) + unpad.finalize()


def _open_envelope(envelope: bytes, credential: RecipientCredential) -> bytes | None:
    from asn1crypto import cms  # noqa: PLC0415
    from asn1crypto import x509 as asn1_x509  # noqa: PLC0415
    from cryptography.exceptions import UnsupportedAlgorithm  # noqa: PLC0415
    from cryptography.hazmat.primitives import keywrap, serialization  # noqa: PLC0415

    if len(envelope) > _MAX_CMS_BYTES:
        msg = "a CMS envelope exceeds 64 KiB"
        code = "recipient_envelope_size"
        raise LimitError(code, msg)
    cert = asn1_x509.Certificate.load(credential.certificate.public_bytes(serialization.Encoding.DER))
    refusals = (
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        OverflowError,
        MalformedPdfError,
        UnsupportedAlgorithm,
        keywrap.InvalidUnwrap,
    )
    try:
        wrapper = cms.ContentInfo.load(envelope, strict=True)
        if wrapper["content_type"].native != "enveloped_data":
            return None
        message = wrapper["content"]
        encrypted = message["encrypted_content_info"]
        if encrypted["content_type"].native != "data":
            return None
        recipients = message["recipient_infos"]
        count = len(recipients)
        if count <= _MAX_RECIPIENTS:
            count = sum(len(r.chosen["recipient_encrypted_keys"]) if r.name == "kari" else 1 for r in recipients)
        if count > _MAX_RECIPIENTS:
            code = "recipient_count"
            msg = "a CMS envelope exceeds 128 recipients"
            raise LimitError(code, msg)
        for recipient in recipients:
            try:
                key = _recipient_key(recipient, cert, credential)
                if key is not None:
                    return _decrypt_content(encrypted, key)
            except refusals:
                continue
    except refusals:
        return None
    return None


def _wrap_recipient(cert: Certificate, key: bytes, rsa_padding: str) -> cms.RecipientInfo:
    from asn1crypto import cms, keys  # noqa: PLC0415
    from asn1crypto import x509 as asn1_x509  # noqa: PLC0415
    from cryptography.hazmat.primitives import keywrap, serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import ec, rsa  # noqa: PLC0415

    identity = asn1_x509.Certificate.load(cert.public_bytes(serialization.Encoding.DER))
    rid = {"issuer_and_serial_number": {"issuer": identity.issuer, "serial_number": identity.serial_number}}
    public = cert.public_key()
    if isinstance(public, rsa.RSAPublicKey):
        if rsa_padding == "oaep":
            identifier = cms.KeyEncryptionAlgorithm(
                {
                    "algorithm": "rsaes_oaep",
                    "parameters": {
                        "hash_algorithm": {"algorithm": "sha256"},
                        "mask_gen_algorithm": {"algorithm": "mgf1", "parameters": {"algorithm": "sha256"}},
                    },
                }
            )
        else:
            identifier = cms.KeyEncryptionAlgorithm({"algorithm": "rsaes_pkcs1v15"})
        return cms.RecipientInfo(
            {
                "ktri": {
                    "version": "v0",
                    "rid": rid,
                    "key_encryption_algorithm": identifier,
                    "encrypted_key": public.encrypt(key, _rsa_padding(identifier)),
                }
            }
        )
    if not isinstance(public, ec.EllipticCurvePublicKey):
        msg = "unsupported recipient key"
        raise TypeError(msg)
    ephemeral = ec.generate_private_key(public.curve)
    point = ephemeral.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    agreement = {"secp256r1": "1.3.132.1.11.1", "secp384r1": "1.3.132.1.11.2", "secp521r1": "1.3.132.1.11.3"}[
        public.curve.name
    ]
    wrap = cms.KeyEncryptionAlgorithm({"algorithm": _AES_WRAP[256]})
    ukm = os.urandom(16)
    aes_key = _ecdh_key(ephemeral.exchange(ec.ECDH(), public), _ECDH_DIGEST[agreement], wrap, ukm)
    return cms.RecipientInfo(
        {
            "kari": {
                "version": "v3",
                "originator": {"originator_key": keys.PublicKeyInfo.load(point)},
                "ukm": ukm,
                "key_encryption_algorithm": {"algorithm": agreement, "parameters": wrap},
                "recipient_encrypted_keys": [{"rid": rid, "encrypted_key": keywrap.aes_key_wrap(aes_key, key)}],
            }
        }
    )


def _seal_envelope(payload: bytes, certificates: list[Certificate], cipher: str, rsa_padding: str) -> bytes:
    from asn1crypto import cms  # noqa: PLC0415
    from cryptography.hazmat.primitives import padding  # noqa: PLC0415
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: PLC0415

    key = os.urandom({"aes-128": 16, "aes-192": 24, "aes-256": 32}[cipher])
    iv = os.urandom(16)
    pad = padding.PKCS7(algorithms.AES.block_size).padder()
    encrypt = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    content = encrypt.update(pad.update(payload) + pad.finalize()) + encrypt.finalize()
    recipients = [_wrap_recipient(cert, key, rsa_padding) for cert in certificates]
    return cms.ContentInfo(
        {
            "content_type": "enveloped_data",
            "content": {
                "version": "v2" if any(r.name == "kari" for r in recipients) else "v0",
                "recipient_infos": recipients,
                "encrypted_content_info": {
                    "content_type": "data",
                    "content_encryption_algorithm": {"algorithm": f"aes{len(key) * 8}_cbc", "parameters": iv},
                    "encrypted_content": content,
                },
            },
        }
    ).dump()


def _check_certificate(certificate: Certificate) -> None:
    # asn1crypto registers the RFC 3161 content OIDs on import, before its first CMS value freezes the reverse map.
    from asn1crypto import tsp  # noqa: F401, PLC0415
    from cryptography import x509  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import ec, rsa  # noqa: PLC0415

    if not isinstance(certificate, x509.Certificate):
        msg = "recipient certificate must be a cryptography.x509.Certificate"
        raise TypeError(msg)
    key = certificate.public_key()
    if isinstance(key, rsa.RSAPublicKey):
        if not _MIN_RSA_BITS <= key.key_size <= _MAX_RSA_BITS:
            msg = "recipient certificates require a 2048 to 8192-bit RSA key"
            raise ValueError(msg)
    elif not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name not in {
        "secp256r1",
        "secp384r1",
        "secp521r1",
    }:
        msg = "recipient certificates require RSA or P-256, P-384 or P-521 ECDH"
        raise ValueError(msg)
    try:
        usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        return
    allowed = usage.key_encipherment if isinstance(key, rsa.RSAPublicKey) else usage.key_agreement
    if not allowed:
        msg = "recipient certificate key usage does not permit key encipherment or key agreement for this key"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class RecipientCredential:
    """An X.509 recipient certificate and its matching RSA or ECDH private key.

    Install the ``recipients`` extra, load the certificate and key with
    ``cryptography``, and pass this credential to ``open(credential=...)``,
    ``probe(credential=...)`` or ``Document.unlock``. RSA keys between
    2048 and 8192 bits use PKCS#1 v1.5 or RSAES-OAEP key transport. OAEP
    reads the declared SHA-1, SHA-224, SHA-256, SHA-384 or SHA-512 digest,
    independent MGF1 digest and label. P-256, P-384 and P-521 certificates
    use ephemeral-static ECDH with X9.63 derivation and AES key wrap.

    ISO 32000-2:2020 §7.6.5 defines the CMS content ciphers. RSAES-OAEP
    and ephemeral-static ECDH follow the CMS formats in RFC 3560 and RFC 5753.

    | Envelope algorithm | Read | New PDF |
    | --- | --- | --- |
    | AES-CBC (RFC 3565) | 128, 192 and 256 bits | All three |
    | DES-CBC and three-key Triple DES-CBC | Both | Deprecated in PDF 2.0, read-only |
    | RC2-CBC | 1 to 128 effective bits, with a 1 to 16-byte key | Deprecated in PDF 2.0, read-only |
    | RC4 | Byte-sized keys from 8 through 256 bits | Deprecated in PDF 2.0, read-only |
    | RSA PKCS#1 v1.5 | 2048 to 8192-bit RSA | Default RSA transport |
    | RSAES-OAEP | Declared digest, MGF1 digest and label | SHA-256 and MGF1-SHA-256, empty label |
    | ECDH with AES key wrap | P-256, P-384 and P-521 | All three with AES-256 key wrap |

    The private key stays with the caller. The store retains the authenticated file key.
    """

    certificate: Certificate
    private_key: RSAPrivateKey | EllipticCurvePrivateKey = field(repr=False)

    def __post_init__(self) -> None:
        """Check the key type and its correspondence with the certificate."""
        from cryptography.hazmat.primitives.asymmetric import ec, rsa  # noqa: PLC0415

        _check_certificate(self.certificate)
        if not isinstance(self.private_key, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey)):
            msg = "private_key must be an RSA or ECDH private key"
            raise TypeError(msg)
        if self.private_key.public_key() != self.certificate.public_key():
            msg = "private key does not match the recipient certificate"
            raise PasswordError(msg)

    def _decrypt(self, envelope: bytes) -> bytes | None:
        payload = _open_envelope(envelope, self)
        if payload is None:
            return None
        if len(payload) != _RECIPIENT_PAYLOAD_SIZE:
            msg = "the default public-key recipient payload must contain a 20-byte seed and four permission bytes"
            raise MalformedPdfError(msg)
        return payload


@dataclass(frozen=True, slots=True, kw_only=True)
class RecipientEncryption:
    """Encrypt a full save for certificate recipients with Adobe.PubSec, adbe.pkcs7.s5 and AES.

    Install ``pyropdf[recipients]`` and supply up to 128 X.509 certificates.
    Recipients with the same permissions share a CMS envelope. The named filter for strings and
    streams is ``DefaultCryptFilter``. Set ``encrypt_metadata=False`` to leave
    the metadata stream readable. AES-256 supports direct rendering. AES-128 supports
    store operations including saving, while the engine needs a decrypted input for rendering.
    Both an incremental save and a full save with
    ``Encryption.KEEP`` preserve untouched ciphertext.

    Select the PDF file cipher with ``method``. The separate ``cms_cipher``
    selects AES-128-CBC, AES-192-CBC or AES-256-CBC for each CMS envelope
    (RFC 3565). It defaults to AES-256-CBC. RSA certificates use
    ``rsa_padding="pkcs1v15"`` by default, or ``"oaep"`` with SHA-256,
    MGF1-SHA-256 and an empty label (RFC 3560). A P-256, P-384 or P-521
    certificate uses ECDH with the corresponding SHA-256, SHA-384 or
    SHA-512 digest, fresh user keying material and AES-256 key wrap
    (RFC 5753). Certificate key usage, when present, must permit RSA
    key encipherment or EC key agreement.

    Map each X.509 certificate to its permission flags. A value of ``None`` gives the recipient
    permission to change encryption and implies all other permissions (ISO 32000-2 Table 24 bit 2).
    Other recipients must keep the encryption when saving.
    """

    recipients: Mapping[Certificate, Permissions | None]
    encrypt_metadata: bool = True
    cms_cipher: Literal["aes-128", "aes-192", "aes-256"] = "aes-256"
    rsa_padding: Literal["pkcs1v15", "oaep"] = "pkcs1v15"
    method: Literal["aes-256", "aes-128"] = "aes-256"

    def __post_init__(self) -> None:
        """Validate the recipients and retain their order for file-key derivation."""
        if not isinstance(self.recipients, Mapping) or not self.recipients:
            msg = "recipients must map 1 to 128 certificates to permissions"
            raise ValueError(msg)
        if len(self.recipients) > _MAX_RECIPIENTS:
            code = "recipient_count"
            msg = "recipient encryption accepts at most 128 certificates"
            raise LimitError(code, msg)
        if not isinstance(self.encrypt_metadata, bool):
            msg = "encrypt_metadata must be a bool"
            raise TypeError(msg)
        if self.method not in ("aes-256", "aes-128"):
            msg = "recipient method must be aes-256 or aes-128"
            raise ValueError(msg)
        if self.cms_cipher not in ("aes-128", "aes-192", "aes-256"):
            msg = "CMS content encryption must be aes-128, aes-192 or aes-256"
            raise ValueError(msg)
        if self.rsa_padding not in ("pkcs1v15", "oaep"):
            msg = "RSA key transport padding must be pkcs1v15 or oaep"
            raise ValueError(msg)
        for certificate, flags in self.recipients.items():
            _check_certificate(certificate)
            if flags is not None and not isinstance(flags, Permissions):
                msg = "recipient permissions must be Permissions flags or None"
                raise TypeError(msg)
        object.__setattr__(self, "recipients", MappingProxyType(dict(self.recipients)))

    def _envelopes(self, seed: bytes) -> Iterator[bytes]:
        groups: dict[int, list[Certificate]] = {}
        for cert, permissions in self.recipients.items():
            flags = 2 | int(Permissions.ALL) if permissions is None else int(permissions & Permissions.ALL)
            groups.setdefault(flags, []).append(cert)
        for flags, certificates in groups.items():
            payload = seed + (flags | 0xFFFF_F0C1).to_bytes(4, "big")
            yield _seal_envelope(payload, certificates, self.cms_cipher, self.rsa_padding)

    def _core(self) -> tuple[bytes, bytes, tuple[bool, bool], Iterator[bytes]]:
        seed = os.urandom(20)
        return seed, os.urandom(16), (self.encrypt_metadata, self.method == "aes-256"), self._envelopes(seed)


def _open_credentials(
    password: str | None, credential: RecipientCredential | None
) -> tuple[bytes | None, Callable[[bytes], bytes | None] | None]:
    if credential is None:
        return (None if password is None else _password_bytes(password)), None
    if not isinstance(credential, RecipientCredential):
        msg = "credential must be a RecipientCredential"
        raise TypeError(msg)
    if password is not None:
        msg = "provide only one of password and credential"
        raise ValueError(msg)
    return None, credential._decrypt  # noqa: SLF001


def _encryption_arg(encryption: object) -> object:
    """Return the core's form of a save's encryption."""
    if encryption is None:
        return None
    if not isinstance(encryption, (Encryption, RecipientEncryption)):
        msg = f"encryption must be an Encryption or RecipientEncryption or None: {encryption!r}"
        raise TypeError(msg)
    return encryption._core()  # noqa: SLF001


for _public in (Permissions, Access, Encryption, RecipientEncryption, RecipientCredential):
    _public.__module__ = "pyropdf"

__all__ = ["Access", "Encryption", "Permissions", "RecipientCredential", "RecipientEncryption"]
