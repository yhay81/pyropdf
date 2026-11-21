"""Passwords, permissions, and the encryption of saves."""

from __future__ import annotations

import enum
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar, Literal

from pyropdf._errors import LimitError, PasswordError

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
    from cryptography.x509 import Certificate

#: The longest password, 127 UTF-8 bytes (ISO 32000-2 §7.6.4.3.3).
_MAX_PASSWORD_BYTES = 127
_MIN_RSA_BITS = 2048
_MAX_RSA_BITS = 8192
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


def _check_certificate(certificate: Certificate) -> None:
    from cryptography import x509  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: PLC0415

    if not isinstance(certificate, x509.Certificate):
        msg = "recipient certificate must be a cryptography.x509.Certificate"
        raise TypeError(msg)
    key = certificate.public_key()
    if not isinstance(key, rsa.RSAPublicKey) or not _MIN_RSA_BITS <= key.key_size <= _MAX_RSA_BITS:
        msg = "recipient certificates require a 2048 to 8192-bit RSA key"
        raise ValueError(msg)
    try:
        usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        return
    if not usage.key_encipherment:
        msg = "recipient certificate key usage does not permit key encipherment"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class RecipientCredential:
    """An X.509 recipient certificate and its matching RSA private key.

    Install the ``recipients`` extra, load the certificate and key with
    ``cryptography``, and pass this credential to ``open(credential=...)``,
    ``probe(credential=...)`` or ``Document.unlock``. RSA keys between
    2048 and 8192 bits can unwrap CMS EnvelopedData with PKCS#1 v1.5
    key transport and AES-CBC content encryption. The store retains the
    authenticated file key. The private key stays with the caller.
    """

    certificate: Certificate
    private_key: RSAPrivateKey = field(repr=False)

    def __post_init__(self) -> None:
        """Check the key type and its correspondence with the certificate."""
        from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: PLC0415

        _check_certificate(self.certificate)
        if not isinstance(self.private_key, rsa.RSAPrivateKey):
            msg = "private_key must be an RSA private key"
            raise TypeError(msg)
        if self.private_key.public_key() != self.certificate.public_key():
            msg = "private key does not match the recipient certificate"
            raise PasswordError(msg)

    def _decrypt(self, envelope: bytes) -> bytes | None:
        from cryptography.exceptions import UnsupportedAlgorithm  # noqa: PLC0415
        from cryptography.hazmat.primitives.serialization import pkcs7  # noqa: PLC0415

        try:
            return pkcs7.pkcs7_decrypt_der(envelope, self.certificate, self.private_key, [])
        except (ValueError, UnsupportedAlgorithm):
            # A malformed, unsupported, or inapplicable envelope grants no access.
            return None


@dataclass(frozen=True, slots=True, kw_only=True)
class RecipientEncryption:
    """Encrypt a full save for RSA certificate recipients with Adobe.PubSec, adbe.pkcs7.s5 and AES.

    Install ``pyropdf[recipients]`` and supply up to 128 X.509 certificates.
    Recipients with the same permissions share a CMS envelope. The named filter for strings and
    streams is ``DefaultCryptFilter``. Set ``encrypt_metadata=False`` to leave
    the metadata stream readable. AES-256 supports direct rendering. AES-128 supports
    store operations including saving, while the engine needs a decrypted input for rendering.
    Both an incremental save and a full save with
    ``Encryption.KEEP`` preserve untouched ciphertext.

    Map each X.509 certificate to its permission flags. A value of ``None`` gives the recipient
    permission to change encryption and implies all other permissions (ISO 32000-2 Table 24 bit 2).
    Other recipients must keep the encryption when saving.
    """

    recipients: Mapping[Certificate, Permissions | None]
    encrypt_metadata: bool = True
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
        for certificate, flags in self.recipients.items():
            _check_certificate(certificate)
            if flags is not None and not isinstance(flags, Permissions):
                msg = "recipient permissions must be Permissions flags or None"
                raise TypeError(msg)
        object.__setattr__(self, "recipients", MappingProxyType(dict(self.recipients)))

    def _envelopes(self, seed: bytes) -> Iterator[bytes]:
        from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
        from cryptography.hazmat.primitives.ciphers.algorithms import AES256  # noqa: PLC0415
        from cryptography.hazmat.primitives.serialization import pkcs7  # noqa: PLC0415

        groups: dict[int, list[Certificate]] = {}
        for cert, permissions in self.recipients.items():
            flags = 2 | int(Permissions.ALL) if permissions is None else int(permissions & Permissions.ALL)
            groups.setdefault(flags, []).append(cert)
        for flags, certificates in groups.items():
            payload = seed + (flags | 0xFFFF_F0C1).to_bytes(4, "big")
            builder = pkcs7.PKCS7EnvelopeBuilder().set_data(payload).set_content_encryption_algorithm(AES256)
            for cert in certificates:
                builder = builder.add_recipient(cert)
            yield builder.encrypt(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])

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
