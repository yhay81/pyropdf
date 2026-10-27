# ruff: noqa: S106 - the fixtures' passwords are test data.
"""Passwords, permissions, and encryption on save, on pylopdf's encrypted fixtures.

The cases of pylopdf's tests/test_encrypted.py at 884af0db, ported. Text
extraction comes in Phase 1, so content is checked by rendering a decrypted
save: every fixture holds the same two pages. Intended differences:

- An incremental save without ``encryption`` keeps the document's encryption,
  since an update cannot decrypt the file it appends to.
- ``object_streams``, page selection, and object handles come later.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_pages import numbered

import pyropdf
from pyropdf import Access, Encryption, Permissions

ASSETS = Path(__file__).parent / "assets" / "encrypted"

#: Every standard security handler revision, by the fixture that uses it.
HANDLERS = {
    "user-rc4-40.pdf": "Standard V1 R2 40-bit RC4",
    "user-rc4-128.pdf": "Standard V2 R3 128-bit RC4",
    "user-r4-rc4-128.pdf": "Standard V4 R4 128-bit RC4",
    "user-aes-128.pdf": "Standard V4 R4 128-bit AES",
    "user-aes-128-plain-metadata.pdf": "Standard V4 R4 128-bit AES",
    "user-aes-256-r5.pdf": "Standard V5 R5 256-bit AES",
    "user-aes-256.pdf": "Standard V5 R6 256-bit AES",
    "user-aes-256-object-streams.pdf": "Standard V5 R6 256-bit AES",
}
USER = pytest.mark.parametrize("name", list(HANDLERS))


def rendered(doc: pyropdf.Document) -> list[bytes]:
    """Return the pixels of every page of ``doc``'s decrypted save."""
    with pyropdf.open(doc.to_bytes()) as plain:
        assert not plain.needs_password
        return [bytes(memoryview(page.pixmap(dpi=36))) for page in plain.pages]


@pytest.fixture(scope="module")
def reference() -> list[bytes]:
    """Return the pages that every fixture holds, rendered."""
    pixels = rendered(pyropdf.open(ASSETS / "owneronly-aes-256.pdf"))
    assert len(pixels) == 2
    assert pixels[0] != pixels[1]
    assert all(page != b"\xff" * len(page) for page in pixels)
    return pixels


@USER
def test_open_without_password_raises_on_use(name: str) -> None:
    doc = pyropdf.open(ASSETS / name)
    assert doc.needs_password
    assert doc.access is None
    assert doc.permissions == Permissions(0)
    with pytest.raises(pyropdf.EncryptedDocumentError, match=r"call unlock\(\)"):
        _ = len(doc.pages)
    with pytest.raises(pyropdf.EncryptedDocumentError):
        doc.to_bytes()


@pytest.mark.spec("standard-security-handler", "read")
@USER
def test_every_standard_handler_reads_with_both_passwords(name: str, reference: list[bytes]) -> None:
    for password, access in (("userpw", Access.USER), ("ownerpw", Access.OWNER)):
        with pyropdf.open(ASSETS / name, password=password) as doc:
            assert not doc.needs_password
            assert doc.access is access
            assert doc.encryption_method == HANDLERS[name]
            assert len(doc.pages) == 2
            assert rendered(doc) == reference
            if access is Access.OWNER:
                assert doc.permissions == Permissions.ALL


@USER
def test_open_with_wrong_password_raises(name: str) -> None:
    with pytest.raises(pyropdf.PasswordError, match="neither"):
        pyropdf.open(ASSETS / name, password="wrong")


@USER
def test_unlock(name: str) -> None:
    doc = pyropdf.open(ASSETS / name)
    with pytest.raises(pyropdf.PasswordError, match="neither"):
        doc.unlock("wrong")
    assert doc.needs_password
    assert doc.unlock("userpw") is Access.USER
    assert not doc.needs_password
    assert len(doc.pages) == 2
    # An unlocked document still checks the passwords of its encryption, as MuPDF does.
    with pytest.raises(pyropdf.PasswordError):
        doc.unlock("anything")
    assert doc.unlock("userpw") is Access.USER
    assert doc.unlock("ownerpw") is Access.OWNER
    assert doc.access is Access.OWNER
    assert doc.permissions == Permissions.ALL


@USER
def test_unlock_a_document_opened_from_bytes(name: str) -> None:
    doc = pyropdf.open((ASSETS / name).read_bytes())
    assert doc.unlock("ownerpw") is Access.OWNER
    assert len(doc.pages) == 2


@pytest.mark.parametrize("password", [None, "", "ownerpw", "wrong"])
def test_owner_only_opens_transparently(password: str | None, reference: list[bytes]) -> None:
    doc = pyropdf.open(ASSETS / "owneronly-aes-256.pdf", password=password)
    assert not doc.needs_password
    assert len(doc.pages) == 2
    assert rendered(doc) == reference
    # The bridge hands hayro the original, which an empty user password opens.
    assert bytes(memoryview(doc.pages[0].pixmap(dpi=36))) == reference[0]


def test_probe_reports_whether_a_password_is_needed() -> None:
    probed = pyropdf.probe(ASSETS / "user-aes-256.pdf")
    assert probed.needs_password is True
    assert probed.is_repaired is False
    assert probed.page_count == 2
    assert pyropdf.probe(ASSETS / "user-aes-256.pdf", password="userpw").needs_password is False
    with pytest.raises(pyropdf.PasswordError):
        pyropdf.probe(ASSETS / "user-aes-256.pdf", password="wrong")


@pytest.mark.spec("standard-security-handler", "edit")
def test_a_decrypted_save_produces_a_plain_pdf(reference: list[bytes]) -> None:
    doc = pyropdf.open(ASSETS / "user-aes-256.pdf", password="userpw")
    del doc.pages[1]
    data = doc.to_bytes()
    assert b"/Encrypt" not in data
    with pyropdf.open(data) as reopened:
        assert not reopened.needs_password
        assert len(reopened.pages) == 1
        assert bytes(memoryview(reopened.pages[0].pixmap(dpi=36))) == reference[0]


@pytest.mark.spec("standard-security-handler", "interpret")
@pytest.mark.parametrize("name", [*HANDLERS, "owneronly-aes-256.pdf"])
def test_an_encrypted_document_renders_unedited_and_edited(name: str) -> None:
    password = "" if name.startswith("owneronly") else "userpw"
    doc = pyropdf.open(ASSETS / name, password=password)
    pixels = [bytes(memoryview(page.pixmap(dpi=36))) for page in doc.pages]
    assert pixels == rendered(doc)
    doc.pages[0].rotation = 90
    edited = doc.pages[0].pixmap(dpi=36)
    with pyropdf.open(doc.to_bytes()) as plain:
        expected = plain.pages[0].pixmap(dpi=36)
        assert (edited.width, edited.height) == (expected.width, expected.height)
        assert edited.width > edited.height
        assert bytes(memoryview(edited)) == bytes(memoryview(expected))


def test_a_stream_with_an_indirect_length_decrypts_the_data_that_its_length_gives() -> None:
    # generate.py ends the encrypted data of each content stream in a line
    # feed, which a search for endstream would trim. RC4 keeps the length.
    with pyropdf.open(ASSETS / "user-rc4-128-indirect-length.pdf", password="userpw") as doc:
        for stream, length in ((6, 7), (8, 9)):
            data = doc.objects[stream].read(decode=False)
            assert len(data) == doc.objects[length]
            assert data.startswith(b"BT /F1 24 Tf 72 720 Td (Encrypted page ")


@pytest.mark.spec("standard-security-handler", "preserve")
def test_keep_copies_the_encryption() -> None:
    doc = pyropdf.open(ASSETS / "user-aes-256.pdf", password="userpw")
    doc.pages[0].rotation = 90
    for data in (doc.to_bytes(encryption=Encryption.KEEP), doc.to_bytes(incremental=True)):
        locked = pyropdf.open(data)
        assert locked.needs_password
        assert locked.unlock("ownerpw") is Access.OWNER
        assert locked.pages[0].rotation == 90
    assert repr(Encryption.KEEP) == "Encryption.KEEP"
    with pytest.raises(ValueError, match="incremental save keeps"):
        doc.to_bytes(incremental=True, encryption=Encryption(user_password="x"))


def test_an_unencrypted_document_grants_owner_access() -> None:
    doc = pyropdf.open(numbered(1))
    assert not doc.needs_password
    assert doc.access is Access.OWNER
    assert doc.permissions == Permissions.ALL
    assert doc.encryption_method is None
    assert doc.unlock("whatever") is Access.OWNER


@pytest.mark.spec("standard-security-handler", "create")
@pytest.mark.parametrize("method", ["aes-256", "aes-128"])
def test_save_encrypted_roundtrip(method: str) -> None:
    doc = pyropdf.open(numbered(3))
    data = doc.to_bytes(encryption=Encryption(user_password="secret", owner_password="boss", method=method))  # ty: ignore[invalid-argument-type]
    # The original in-memory document remains plaintext.
    assert not doc.needs_password
    assert len(doc.pages) == 3
    locked = pyropdf.open(data)
    assert locked.needs_password
    assert locked.unlock("secret") is Access.USER
    assert len(locked.pages) == 3
    assert locked.encryption_method == (
        "Standard V5 R6 256-bit AES" if method == "aes-256" else "Standard V4 R4 128-bit AES"
    )
    assert [page.rect for page in locked.pages] == [page.rect for page in doc.pages]
    assert pyropdf.open(data, password="boss").access is Access.OWNER


def test_to_bytes_bounds_encrypted_output() -> None:
    doc = pyropdf.open(numbered(3))
    with pytest.raises(pyropdf.LimitError) as caught:
        doc.to_bytes(encryption=Encryption(user_password="secret"), max_size=16)
    assert caught.value.code == "pdf_output_size"
    assert not doc.needs_password


def test_save_encrypted_file_and_password_arg(tmp_path: Path) -> None:
    out = tmp_path / "locked.pdf"
    # The owner password defaults to the user password.
    pyropdf.open(numbered(3)).save(out, encryption=Encryption(user_password="secret"))
    opened = pyropdf.open(out, password="secret")
    assert len(opened.pages) == 3
    assert opened.access is Access.OWNER
    with pytest.raises(pyropdf.PasswordError):
        pyropdf.open(out, password="wrong")


def test_save_encrypted_owner_only_restricts() -> None:
    data = pyropdf.open(numbered(3)).to_bytes(
        encryption=Encryption(user_password="", owner_password="boss", permissions=Permissions.PRINT)
    )
    opened = pyropdf.open(data)
    assert not opened.needs_password
    assert opened.access is Access.USER
    assert opened.permissions == Permissions.PRINT
    assert len(opened.pages) == 3
    # The empty user password lets the bridge's hayro read it.
    assert opened.pages[0].pixmap().width == 100


def test_encryption_without_passwords_encrypts_with_empty_passwords() -> None:
    data = pyropdf.open(numbered(3)).to_bytes(encryption=Encryption())
    assert b"/Encrypt" in data
    reopened = pyropdf.open(data)
    assert not reopened.needs_password
    assert len(reopened.pages) == 3


def test_the_owner_password_defaults_to_the_user_password() -> None:
    encryption = Encryption(user_password="pw", permissions=Permissions.PRINT)
    data = pyropdf.open(numbered(3)).to_bytes(encryption=encryption)
    assert pyropdf.open(data, password="pw").permissions == Permissions.ALL


def test_saving_writes_no_encryption_by_default() -> None:
    assert b"/Encrypt" not in pyropdf.open(ASSETS / "user-aes-256.pdf", password="userpw").to_bytes()


def test_permissions_flags() -> None:
    perms = Permissions.PRINT | Permissions.COPY
    assert int(perms) == (1 << 2) | (1 << 4)
    assert Permissions.MODIFY in Permissions.ALL


def test_password_input_boundary_is_exact() -> None:
    password = "é" * 63 + "a"
    data = numbered(1)
    assert len(pyropdf.open(data, password=password).pages) == 1
    assert pyropdf.probe(data, password=password).page_count == 1
    encrypted = pyropdf.open(data).to_bytes(encryption=Encryption(user_password=password))
    assert len(pyropdf.open(encrypted, password=password).pages) == 1


def test_password_input_is_bounded_before_open_unlock_and_probe() -> None:
    password = "é" * 64
    data = numbered(1)
    for call in (lambda: pyropdf.open(data, password=password), lambda: pyropdf.probe(data, password=password)):
        with pytest.raises(pyropdf.LimitError) as caught:
            call()
        assert caught.value.code == "password_input_size"
    locked = pyropdf.open(ASSETS / "user-aes-256.pdf")
    with pytest.raises(pyropdf.LimitError) as unlock_limit:
        locked.unlock(password)
    assert unlock_limit.value.code == "password_input_size"
    assert locked.needs_password


@pytest.mark.parametrize("field", ["user_password", "owner_password"])
def test_encryption_bounds_password_input(field: str) -> None:
    with pytest.raises(pyropdf.LimitError) as caught:
        Encryption(**{field: "é" * 64})  # ty: ignore[invalid-argument-type]
    assert caught.value.code == "password_input_size"


@pytest.mark.parametrize(
    ("arguments", "error", "message"),
    [
        ({"user_password": b"x"}, TypeError, "user_password must be a str"),
        ({"owner_password": 1}, TypeError, "owner_password must be a str"),
        ({"permissions": "all"}, TypeError, "permissions must be Permissions flags"),
        ({"permissions": True}, TypeError, "permissions must be Permissions flags"),
        ({"method": "rc4-128"}, ValueError, "does not write rc4-128 encryption"),
        ({"method": "RC4-40"}, ValueError, "does not write RC4-40 encryption"),
        ({"method": "aes"}, ValueError, "method must be 'aes-256' or 'aes-128'"),
        ({"method": "keep", "user_password": "u"}, ValueError, "Encryption.KEEP takes the passwords"),
    ],
)
def test_encryption_validates_its_fields(arguments: dict[str, object], error: type[Exception], message: str) -> None:
    with pytest.raises(error, match=message):
        Encryption(**arguments)  # ty: ignore[invalid-argument-type]


def test_encryption_keeps_the_defined_permission_flags() -> None:
    assert Encryption(permissions=-1).permissions == Permissions.ALL  # ty: ignore[invalid-argument-type]
    assert Encryption(permissions=4095).permissions == Permissions.ALL  # ty: ignore[invalid-argument-type]
    assert type(Encryption(permissions=4).permissions) is Permissions  # ty: ignore[invalid-argument-type]
    assert "secret" not in repr(Encryption(user_password="secret"))


@pytest.mark.parametrize("value", [True, 5, "aes-256"])
def test_saving_refuses_other_encryption_values(value: object, tmp_path: Path) -> None:
    doc = pyropdf.open(numbered(3))
    before = doc.to_bytes()
    with pytest.raises(TypeError, match="encryption must be an Encryption or None"):
        doc.to_bytes(encryption=value)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="encryption must be an Encryption or None"):
        doc.save(tmp_path / "out.pdf", encryption=value)  # ty: ignore[invalid-argument-type]
    assert not (tmp_path / "out.pdf").exists()
    assert doc.to_bytes() == before


def test_unlocking_an_unlocked_document_keeps_the_history() -> None:
    doc = pyropdf.open(ASSETS / "user-aes-256.pdf")
    doc.unlock("userpw")
    with doc.transaction("rotate"):
        doc.pages[0].rotation = 90
    assert doc.history.steps == ("rotate",)
    doc.unlock("ownerpw")
    assert doc.history.steps == ("rotate",)
