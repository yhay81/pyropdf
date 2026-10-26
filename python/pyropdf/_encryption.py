"""Passwords, permissions, and the encryption of saves (DESIGN.md section 4.5)."""

from __future__ import annotations

import enum
import os
from dataclasses import dataclass
from typing import ClassVar, Literal

from pyropdf._errors import LimitError

#: The longest password, in UTF-8 bytes, as PDF 2.0 and pylopdf bound it.
_MAX_PASSWORD_BYTES = 127
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


def _encryption_arg(encryption: object) -> str | tuple[bytes, bytes, int, bool, bytes] | None:
    """Return the core's form of a save's ``encryption``, refusing other values."""
    if encryption is None:
        return None
    if not isinstance(encryption, Encryption):
        msg = f"encryption must be an Encryption or None: {encryption!r}"
        raise TypeError(msg)
    return encryption._core()  # noqa: SLF001


for _public in (Permissions, Access, Encryption):
    _public.__module__ = "pyropdf"

__all__ = ["Access", "Encryption", "Permissions"]
