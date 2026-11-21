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

import sysconfig
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
from test_pages import build_raw_pdf, numbered

import pyropdf
from pyropdf import Access, Encryption, Permissions

if TYPE_CHECKING:
    from pyhanko.pdf_utils.generic import DictionaryObject
    from pyhanko.pdf_utils.reader import PdfFileReader

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


def test_recipient_wheel_declares_its_optional_schema_and_crypto_provider() -> None:
    from importlib.metadata import requires  # noqa: PLC0415
    from importlib.util import find_spec  # noqa: PLC0415

    from packaging.requirements import Requirement  # noqa: PLC0415

    requirements = requires("pyropdf")
    assert requirements is not None
    providers = {r.name: r.marker for r in map(Requirement, requirements) if r.name in {"asn1crypto", "cryptography"}}
    assert set(providers) == {"asn1crypto", "cryptography"}
    for marker in providers.values():
        assert marker is not None
        assert marker.evaluate({"extra": "recipients"})
        assert not marker.evaluate({"extra": ""})
    assert find_spec("asn1crypto.cms") is not None
    assert find_spec("cryptography.hazmat.primitives.serialization.pkcs7") is not None


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


@pytest.mark.spec("crypt-filters", "read")
@pytest.mark.spec("encryption-dictionary", "read")
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


@pytest.mark.spec("crypt-filters", "edit")
@pytest.mark.spec("encryption-dictionary", "edit")
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


@pytest.mark.spec("crypt-filters", "interpret")
@pytest.mark.spec("encryption-dictionary", "interpret")
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


@pytest.mark.spec("crypt-filters", "create")
@pytest.mark.spec("encryption-dictionary", "create")
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
    with pytest.raises(TypeError, match="encryption must be an Encryption or RecipientEncryption or None"):
        doc.to_bytes(encryption=value)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="encryption must be an Encryption or RecipientEncryption or None"):
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


RECIPIENT_ASSETS = ASSETS / "recipients"


def recipient(label: str) -> pyropdf.RecipientCredential:
    """Load the published fixture credential, whose key is used only by these tests."""
    from cryptography import x509  # noqa: PLC0415
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: PLC0415

    key = serialization.load_der_private_key((RECIPIENT_ASSETS / f"recipient-{label}.key").read_bytes(), None)
    assert isinstance(key, rsa.RSAPrivateKey)
    cert = x509.load_der_x509_certificate((RECIPIENT_ASSETS / f"recipient-{label}.cer").read_bytes())
    return pyropdf.RecipientCredential(certificate=cert, private_key=key)


RECIPIENT_STREAM = b"0 g 2 3 12 8 re f\n"
RECIPIENT_STRING = b"recipient\x00value\xff"


@pytest.mark.spec("public-key-security-handler", "read")
@pytest.mark.parametrize(
    ("label", "access", "flags"),
    [("editor", Access.USER, Permissions.MODIFY), ("owner", Access.OWNER, Permissions.ALL)],
)
def test_public_key_independent_envelopes(label: str, access: Access, flags: Permissions) -> None:
    source = RECIPIENT_ASSETS / "pyhanko-recipients.pdf"
    credential = recipient(label)
    doc = pyropdf.open(source)
    assert doc.needs_password
    with pytest.raises(pyropdf.EncryptedDocumentError):
        _ = doc.objects[3]
    with pytest.raises(pyropdf.PasswordError):
        doc.unlock("unrelated")
    assert doc.needs_password
    assert doc.unlock(credential) is access
    assert doc.permissions == flags
    assert doc.objects[3].read() == RECIPIENT_STREAM
    assert doc.objects[4]["FixtureValue"] == RECIPIENT_STRING
    assert doc.encryption_method == "Adobe.PubSec adbe.pkcs7.s5 V5 256-bit AES"
    assert not pyropdf.probe(source, credential=credential).needs_password


@pytest.mark.spec("public-key-security-handler", "interpret")
def test_public_key_render_uses_authenticated_version() -> None:
    with (
        pyropdf.open(RECIPIENT_ASSETS / "pyhanko-recipients.pdf", credential=recipient("owner")) as doc,
        pyropdf.open(doc.to_bytes()) as clear,
    ):
        assert doc.pages[0].pixmap().to_bytes() == clear.pages[0].pixmap().to_bytes()
        doc.objects[3].write(b"1 g 0 0 50 40 re f\n", filter=None)
        assert doc.pages[0].pixmap().to_bytes() != clear.pages[0].pixmap().to_bytes()
        assert doc.objects[3].read() == b"1 g 0 0 50 40 re f\n"
        assert b"/Standard" not in doc.history.save()


@pytest.mark.spec("public-key-security-handler", "preserve")
@pytest.mark.spec("crypt-filters", "preserve")
@pytest.mark.spec("public-key-security-handler", "edit")
@pytest.mark.parametrize("incremental", [False, True])
def test_public_key_edit_keeps_untouched_ciphertext(*, incremental: bool) -> None:
    source = (RECIPIENT_ASSETS / "pyhanko-recipients.pdf").read_bytes()
    untouched = source.split(b"3 0 obj\n", 1)[1].split(b"\nendobj", 1)[0]
    with pyropdf.open(source, credential=recipient("editor")) as doc:
        doc.objects[4]["FixtureValue"] = b"edited\x00string"
        saved = doc.to_bytes(incremental=incremental, encryption=Encryption.KEEP)
        assert saved.startswith(source) is incremental
        assert untouched in saved
        assert b"/Standard" not in saved
        assert b"edited\x00string" not in saved
        with pyropdf.open(saved, credential=recipient("owner")) as reopened:
            assert reopened.objects[3].read() == RECIPIENT_STREAM
            assert reopened.objects[4]["FixtureValue"] == b"edited\x00string"
            if incremental:
                with reopened.revisions[0].open() as previous:
                    assert previous.objects[4]["FixtureValue"] == RECIPIENT_STRING


@pytest.fixture(scope="module")
def _pyhanko_oracle() -> None:
    if sysconfig.get_config_var("Py_GIL_DISABLED") == 1:
        pytest.skip("pyHanko imports enable the GIL on free-threaded Python")
    # Register RFC 3161 before asn1crypto caches the reverse CMS content-type map.
    from asn1crypto import tsp  # noqa: F401, PLC0415


@pytest.mark.spec("public-key-security-handler", "create")
@pytest.mark.parametrize("encrypt_metadata", [True, False])
@pytest.mark.parametrize("method", ["aes-256", "aes-128"])
@pytest.mark.usefixtures("_pyhanko_oracle")
def test_public_key_output_read_by_pyhanko(method: Literal["aes-256", "aes-128"], *, encrypt_metadata: bool) -> None:
    import io  # noqa: PLC0415

    from asn1crypto import keys, x509  # noqa: PLC0415
    from pyhanko.pdf_utils.crypt.pubkey import SimpleEnvelopeKeyDecrypter  # noqa: PLC0415
    from pyhanko.pdf_utils.reader import PdfFileReader  # noqa: PLC0415

    editor, owner = recipient("editor"), recipient("owner")
    with pyropdf.open(RECIPIENT_ASSETS / "pyhanko-recipients.pdf", credential=owner) as doc:
        original_xmp = doc.catalog["Metadata"].read()
        out = doc.to_bytes(
            encryption=pyropdf.RecipientEncryption(
                recipients={owner.certificate: None, editor.certificate: Permissions.PRINT},
                encrypt_metadata=encrypt_metadata,
                method=method,
            )
        )
    assert (original_xmp in out) == (not encrypt_metadata)
    for label, flags in (("editor", int(Permissions.PRINT)), ("owner", int(Permissions.ALL) | 2)):
        reader = PdfFileReader(io.BytesIO(out))
        key = keys.PrivateKeyInfo.load((RECIPIENT_ASSETS / f"recipient-{label}.key").read_bytes())
        cert = x509.Certificate.load((RECIPIENT_ASSETS / f"recipient-{label}.cer").read_bytes())
        auth = reader.decrypt_pubkey(SimpleEnvelopeKeyDecrypter(cert, key))
        assert auth.permission_flags is not None
        assert auth.permission_flags.as_uint32() & (int(Permissions.ALL) | 2) == flags
        page = reader.root["/Pages"]["/Kids"][0].get_object()
        assert page["/Contents"].data == RECIPIENT_STREAM
        assert bytes(page["/FixtureValue"]) == RECIPIENT_STRING
        assert reader.root["/Metadata"].data == original_xmp
    with pyropdf.open(out, credential=editor) as reopened:
        assert reopened.permissions == Permissions.PRINT
        assert reopened.catalog["Metadata"].read() == original_xmp
    with pytest.raises(pyropdf.PasswordError):
        pyropdf.open(
            pyropdf.open(numbered(1)).to_bytes(
                encryption=pyropdf.RecipientEncryption(recipients={editor.certificate: None})
            ),
            credential=owner,
        )


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (b"/adbe.pkcs7.s5", b"/adbe.pkcs7.s4", "adbe.pkcs7.s5"),
        (b"/AESV3", b"/AESV2", "AESV3"),
        (b"/EncryptMetadata true", b"/EncryptMetadata null", "boolean"),
        (b"/Recipients [", b"/Skipieeets [", "Recipients"),
    ],
)
def test_public_key_malformed_filter_is_atomic(old: bytes, new: bytes, message: str) -> None:
    source = (RECIPIENT_ASSETS / "pyhanko-recipients.pdf").read_bytes()
    assert old in source
    bad = source.replace(old, new)
    doc = pyropdf.open(bad)
    with pytest.raises(pyropdf.MalformedPdfError, match=message):
        doc.unlock(recipient("editor"))
    assert doc.needs_password
    with pytest.raises(pyropdf.EncryptedDocumentError):
        _ = doc.catalog


@pytest.mark.spec("public-key-security-handler", "read")
@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("identity-strings", "adbe.pkcs7.s5 V5 256-bit AES"),
        ("recipients-aes128", "adbe.pkcs7.s5 V4 128-bit AES"),
        ("s3", "adbe.pkcs7.s3 V1 40-bit RC4"),
        ("s4", "adbe.pkcs7.s4 V2 128-bit RC4"),
        ("v2", "adbe.pkcs7.s5 V4 128-bit RC4"),
    ],
)
def test_public_key_algorithms_and_filter_selection(fixture: str, expected: str) -> None:
    with pyropdf.open(RECIPIENT_ASSETS / f"pyhanko-{fixture}.pdf", credential=recipient("editor")) as doc:
        assert doc.encryption_method == f"Adobe.PubSec {expected}"
        assert not doc.check().defects
        assert doc.objects[3].read() == RECIPIENT_STREAM
        assert doc.objects[4]["FixtureValue"] == RECIPIENT_STRING
        assert doc.permissions == Permissions.MODIFY
        if fixture == "identity-strings":
            assert b"<x:xmpmeta" in doc.catalog["Metadata"].read()
            assert doc.pages[0].pixmap(dpi=72).width == 50
        else:
            with pytest.raises(pyropdf.PdfError, match="legacy public-key file key"):
                doc.pages[0].pixmap()
        doc.objects[4]["FixtureValue"] = b"edited"
        for incremental in (True, False):
            if "RC4" in expected:
                with pytest.raises(pyropdf.PdfError, match="does not write RC4"):
                    doc.to_bytes(incremental=incremental, encryption=Encryption.KEEP)
            else:
                saved = doc.to_bytes(incremental=incremental, encryption=Encryption.KEEP)
                with pyropdf.open(saved, credential=recipient("editor")) as reopened:
                    assert reopened.objects[4]["FixtureValue"] == b"edited"


def test_public_key_changing_encryption_requires_authorized_recipient(tmp_path: Path) -> None:
    with pyropdf.open(RECIPIENT_ASSETS / "pyhanko-recipients.pdf", credential=recipient("editor")) as doc:
        doc.objects[4]["FixtureValue"] = b"keep my edit"
        for encryption in (None, Encryption(user_password="new")):
            with pytest.raises(pyropdf.PdfError, match="permission to change encryption"):
                doc.to_bytes(encryption=encryption)
        target = tmp_path / "untouched.pdf"
        target.write_bytes(b"original")
        with pytest.raises(pyropdf.PdfError, match="permission to change encryption"):
            doc.save(target)
        assert target.read_bytes() == b"original"
        assert doc.unlock(recipient("owner")) is Access.OWNER
        with pyropdf.open(doc.to_bytes()) as clear:
            assert clear.objects[4]["FixtureValue"] == b"keep my edit"


@pytest.mark.parametrize(
    ("count", "envelope_bytes", "code"), [(129, 1, "recipient_count"), (1, 65_537, "recipient_envelope_size")]
)
def test_public_key_recipient_bounds_fail_before_disclosure(count: int, envelope_bytes: int, code: str) -> None:
    envelopes = ("<" + "00" * envelope_bytes + "> ") * count
    source = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [] /Count 0 >>",
            3: f"<< /Filter /Adobe.PubSec /SubFilter /adbe.pkcs7.s4 /V 2 /Length 128 /Recipients [{envelopes}] >>",
        }
    ).replace(b"/Root 1 0 R >>", b"/Root 1 0 R /Encrypt 3 0 R >>")
    doc = pyropdf.open(source)
    with pytest.raises(pyropdf.LimitError) as refusal:
        doc.unlock(recipient("owner"))
    assert refusal.value.code == code
    assert doc.needs_password
    with pytest.raises(pyropdf.EncryptedDocumentError):
        _ = doc.catalog


def test_public_key_equal_permissions_share_a_cms_envelope() -> None:
    editor, owner = recipient("editor"), recipient("owner")
    with pytest.raises(pyropdf.PasswordError, match="does not match"):
        pyropdf.RecipientCredential(certificate=editor.certificate, private_key=owner.private_key)
    settings = pyropdf.RecipientEncryption(
        recipients={editor.certificate: Permissions.COPY, owner.certificate: Permissions.COPY}
    )
    saved = pyropdf.open(numbered(1)).to_bytes(encryption=settings)
    for credential in (owner, editor):
        with pyropdf.open(saved, credential=credential) as doc:
            assert doc.permissions == Permissions.COPY
            assert len(doc.trailer["Encrypt"]["CF"]["DefaultCryptFilter"]["Recipients"]) == 1


@pytest.mark.parametrize("length", [19, 20, 24, 25])
def test_recipient_cms_seed_shape_does_not_grant_default_permissions(length: int) -> None:
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.serialization import pkcs7  # noqa: PLC0415

    owner = recipient("owner")
    payload = bytes(20) + b"\xff" * 5
    envelope = (
        pkcs7.PKCS7EnvelopeBuilder()
        .set_data(payload[:length])
        .add_recipient(owner.certificate)
        .encrypt(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])
    )
    cms = f"<{envelope.hex()}>"
    source = build_raw_pdf(
        {
            1: "<< /Type /Catalog /Pages 2 0 R >>",
            2: "<< /Type /Pages /Kids [] /Count 0 >>",
            3: f"<< /Filter /Adobe.PubSec /SubFilter /adbe.pkcs7.s4 /V 2 /Length 128 /Recipients [{cms}] >>",
        }
    ).replace(b"/Root 1 0 R >>", b"/Root 1 0 R /Encrypt 3 0 R >>")
    with pyropdf.open(source) as doc:
        with pytest.raises(pyropdf.PasswordError):
            doc.unlock(recipient("editor"))
        if length == 24:
            assert doc.unlock(owner) is Access.OWNER
            assert doc.permissions == Permissions.ALL
        else:
            message = "four permission bytes" if length == 20 else "20 or 24 bytes"
            with pytest.raises(pyropdf.MalformedPdfError, match=message):
                doc.unlock(owner)
            assert doc.needs_password


def _independent_crypt_reader(data: bytes, handler: str) -> PdfFileReader:
    """Open the result with pyHanko's independent crypt-filter implementation."""
    import io  # noqa: PLC0415

    from asn1crypto import keys, x509  # noqa: PLC0415
    from pyhanko.pdf_utils.crypt.pubkey import SimpleEnvelopeKeyDecrypter  # noqa: PLC0415
    from pyhanko.pdf_utils.reader import PdfFileReader  # noqa: PLC0415

    reader = PdfFileReader(io.BytesIO(data))
    if handler == "password":
        reader.decrypt("owner")
    else:
        key = keys.PrivateKeyInfo.load((RECIPIENT_ASSETS / "recipient-owner.key").read_bytes())
        cert = x509.Certificate.load((RECIPIENT_ASSETS / "recipient-owner.cer").read_bytes())
        reader.decrypt_pubkey(SimpleEnvelopeKeyDecrypter(cert, key))
    return reader


def _add_crypt_streams(
    doc: pyropdf.Document, selected: str, attachment: bytes, clear: bytes, private: bytes
) -> tuple[int, int, int, int]:
    name = pyropdf.Name
    doc.attachments.add("file.bin", attachment)
    ef = doc.catalog["Names"]["EmbeddedFiles"]["Names"][1]["EF"]["F"]
    ef.write(attachment, filter=None)
    ef_ref = ef.xref
    public_ref = doc.objects.add_stream(clear, filter=None)
    public = doc.objects[public_ref]
    public["Filter"] = name("Crypt")
    # A missing /Name means /Identity. Dictionary strings still use StrF.
    public["Marker"] = b"a protected PDF string"
    private_ref = doc.objects.add_stream(private)
    protected = doc.objects[private_ref]
    protected["Filter"] = [name("Crypt"), name("FlateDecode")]
    protected["DecodeParms"] = [{"Name": name(selected)}, None]
    ordinary_ref = doc.objects.add_stream(b"unrelated private stream", filter=None)
    return ef_ref, public_ref, private_ref, ordinary_ref


@pytest.mark.parametrize("handler", ["password", "recipient"])
@pytest.mark.parametrize("method", ["aes-128", "aes-256"])
@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("crypt-filter", "read", "create", "edit", "preserve")
@pytest.mark.spec("crypt-filters", "read", "interpret", "create", "edit", "preserve")
@pytest.mark.usefixtures("_pyhanko_oracle")
def test_explicit_stream_crypt_and_embedded_defaults(
    handler: str, method: Literal["aes-128", "aes-256"], *, incremental: bool
) -> None:
    from pyhanko.pdf_utils import generic  # noqa: PLC0415

    name = pyropdf.Name
    password = "owner" if handler == "password" else None
    credential = recipient("owner") if handler == "recipient" else None
    options = (
        pyropdf.RecipientEncryption(recipients={credential.certificate: None}, method=method)
        if credential is not None
        else Encryption(user_password="user", owner_password="owner", method=method)
    )
    selected = "StdCF" if handler == "password" else "DefaultCryptFilter"
    attachment = b"data governed by the embedded-file default"
    clear = b"explicitly public ordinary stream"
    private = b"a distinct named and compressed stream"
    with pyropdf.open(numbered(1)) as doc:
        ef_ref, public_ref, private_ref, ordinary_ref = _add_crypt_streams(doc, selected, attachment, clear, private)
        initial = doc.to_bytes(encryption=options)
    assert clear in initial
    assert private not in initial
    assert attachment not in initial
    reader = _independent_crypt_reader(initial, handler)
    assert reader.get_object(generic.Reference(private_ref, 0, reader)).data == private
    assert reader.get_object(generic.Reference(public_ref, 0, reader)).data == clear
    untouched = initial.split(f"{ordinary_ref} 0 obj\n".encode(), 1)[1].split(b"\nendobj", 1)[0]
    with pyropdf.open(initial, password=password, credential=credential) as doc:
        assert doc.objects[private_ref].read() == private
        assert doc.objects[public_ref]["Marker"] == b"a protected PDF string"
        assert doc.attachments["file.bin"].read() == attachment
        doc.trailer["Encrypt"]["EFF"] = name("Identity")
        doc.objects[private_ref]["DecodeParms"][0]["Name"] = name("Identity")
        # The old public stream is now protected by the explicit named filter.
        doc.objects[public_ref]["DecodeParms"] = {"Name": name(selected)}
        changed = doc.to_bytes(encryption=Encryption.KEEP, incremental=incremental)
    assert untouched in changed
    if incremental:
        assert changed.startswith(initial)
    reader = _independent_crypt_reader(changed, handler)
    for reference, expected in ((public_ref, clear), (private_ref, private), (ef_ref, attachment)):
        assert reader.get_object(generic.Reference(reference, 0, reader)).data == expected
    assert attachment in changed
    with pyropdf.open(changed, password=password, credential=credential) as doc:
        assert doc.objects[public_ref].read() == clear
        assert doc.objects[private_ref].read() == private
        assert doc.attachments["file.bin"].read() == attachment
        assert doc.objects[ordinary_ref].read() == b"unrelated private stream"
        if incremental:
            with doc.revisions[0].open() as old:
                assert old.attachments["file.bin"].read() == attachment
                assert old.objects[private_ref].read() == private
                assert "EFF" not in old.trailer["Encrypt"]
        plain = doc.to_bytes()
    with pyropdf.open(plain) as doc:
        assert doc.objects[private_ref].read() == private
        assert name("Crypt") not in doc.objects[private_ref]["Filter"]
        assert "Filter" not in doc.objects[public_ref]


@pytest.mark.parametrize(
    ("filters", "params", "message"),
    [
        (["FlateDecode", "Crypt"], [None, None], "first"),
        (["Crypt", "Crypt"], [None, None], "only"),
        (["Crypt", "FlateDecode"], [None], "correspond"),
        (["Crypt"], [{"Name": 12}], "must name"),
        (["Crypt"], [{"Type": pyropdf.Name("Wrong")}], "parameter /Type"),
        (["Crypt"], [{"Name": pyropdf.Name("Absent")}], "undefined"),
    ],
)
@pytest.mark.spec("crypt-filter", "create", "edit")
def test_malformed_explicit_stream_crypt_refuses_save(
    filters: list[str], params: list[object], message: str, tmp_path: Path
) -> None:
    path = tmp_path / "unchanged.pdf"
    path.write_bytes(b"untouched")
    with pyropdf.open(numbered(1)) as doc:
        ref = doc.objects.add_stream(b"secret", filter=None)
        doc.objects[ref]["Filter"] = [pyropdf.Name(f) for f in filters]
        doc.objects[ref]["DecodeParms"] = params
        with pytest.raises(pyropdf.MalformedPdfError, match=message):
            doc.save(path, encryption=Encryption(user_password="u"))
    assert path.read_bytes() == b"untouched"


@pytest.mark.spec("crypt-filters", "create")
def test_encryption_requires_a_resolved_filter_selector(tmp_path: Path) -> None:
    path = tmp_path / "existing.pdf"
    path.write_bytes(b"unchanged")
    with pyropdf.open(numbered(1)) as doc:
        stream = doc.objects[doc.objects.add_stream(b"test data", filter=None)]
        filters = doc.objects[doc.objects.add([pyropdf.Name("Crypt")])]
        stream["Filter"] = filters
        with pytest.raises(pyropdf.MalformedPdfError, match="unresolved /Filter"):
            doc.save(path, encryption=Encryption(user_password="reader"))
        assert stream["Filter"].xref == filters.xref
    assert path.read_bytes() == b"unchanged"


def _private_recipient_control(
    *, attachment_only: bool, implicit: bool, page_data: bytes | None = None, private_page: bool = False
) -> bytes:
    """Produce two independent recipient seeds with pyHanko (ISO Table 27)."""
    import io  # noqa: PLC0415
    import re  # noqa: PLC0415

    from asn1crypto import x509  # noqa: PLC0415
    from pyhanko.pdf_utils import generic  # noqa: PLC0415
    from pyhanko.pdf_utils.crypt.api import CryptFilterConfiguration, SecurityHandlerVersion  # noqa: PLC0415
    from pyhanko.pdf_utils.crypt.pubkey import (  # noqa: PLC0415
        PubKeyAdbeSubFilter,
        PubKeyAESCryptFilter,
        PubKeySecurityHandler,
        RecipientEncryptionPolicy,
    )
    from pyhanko.pdf_utils.embed import EmbeddedFileObject  # noqa: PLC0415
    from pyhanko.pdf_utils.writer import PageObject, PdfFileWriter  # noqa: PLC0415

    name = generic.pdf_name
    writer = PdfFileWriter(stream_xrefs=False)
    public, private = name("/PublicDefault"), name("/PrivateContent")
    default = name("/Identity") if attachment_only else public
    normal = PubKeyAESCryptFilter(keylen=32, acts_as_default=True)
    restricted = PubKeyAESCryptFilter(keylen=32, acts_as_default=False)
    config = CryptFilterConfiguration(
        {private: restricted} if attachment_only else {public: normal, private: restricted},
        default_stream_filter=default,
        default_string_filter=default,
        default_file_filter=private,
    )
    crypto = PubKeySecurityHandler(
        SecurityHandlerVersion.AES256, PubKeyAdbeSubFilter.S5, 32, crypt_filter_config=config, compat_entries=False
    )
    certs = [
        x509.Certificate.load((RECIPIENT_ASSETS / f"recipient-{label}.cer").read_bytes())
        for label in ("owner", "editor")
    ]
    if not attachment_only:
        normal.add_recipients(certs, policy=RecipientEncryptionPolicy())
    restricted.add_recipients(certs[:1], policy=RecipientEncryptionPolicy())
    writer._assign_security_handler(crypto)  # noqa: SLF001 - pyHanko accepts a custom crypt-filter configuration here.
    content = generic.StreamObject(stream_data=b"0 g 1 2 3 4 re f\n" if page_data is None else page_data)
    if private_page:
        content.compress()
        content.add_crypt_filter(private, handler=crypto)
    page = PageObject(
        writer.add_object(content), [0, 0, 40, 50], resources=None if page_data is None else _source_font_resources()
    )
    page[name("/FixtureName")] = generic.TextStringObject("fixture string")
    writer.insert_page(page)
    named = generic.StreamObject(stream_data=b"Named private data")
    named.compress()
    named.add_crypt_filter(private, handler=crypto)
    writer.root[name("/FixtureNamed")] = writer.add_object(named)
    embedded = EmbeddedFileObject.from_file_data(writer, b"Embedded private data", compress=False)
    writer.root[name("/FixtureEmbedded")] = embedded.ef_stream_ref
    output = io.BytesIO()
    writer.write(output)
    data = output.getvalue()
    if implicit:
        # pyHanko explicitly spells its /EFF on embedded-file writes. Remove
        # just those entries without disturbing any offsets or ciphertext.
        start = data.index(b"/Type /EmbeddedFile")
        end = data.index(b"stream", start)
        head = data[start:end]
        for pattern in (rb"/Filter\s*/Crypt\b", rb"/DecodeParms\s*<<[^>]*>>"):
            head, count = re.subn(pattern, lambda m: b" " * len(m[0]), head)
            assert count == 1
        data = data[:start] + head + data[end:]
    return data


@pytest.mark.spec("crypt-filter", "read", "preserve")
@pytest.mark.spec("crypt-filters", "read", "interpret")
@pytest.mark.parametrize("attachment_only", [False, True])
@pytest.mark.parametrize("implicit", [False, True])
@pytest.mark.usefixtures("_pyhanko_oracle")
def test_distinct_cms_filter_keys_and_credential_upgrade(*, attachment_only: bool, implicit: bool) -> None:
    data = _private_recipient_control(attachment_only=attachment_only, implicit=implicit)
    owner, editor = recipient("owner"), recipient("editor")
    with pyropdf.open(data, credential=None if attachment_only else editor) as doc:
        assert doc.pages[0].obj["Contents"].read() == b"0 g 1 2 3 4 re f\n"
        assert doc.pages[0].obj["FixtureName"] == b"fixture string"
        with pytest.raises(pyropdf.PasswordError):
            doc.catalog["FixtureNamed"].read()
        with pytest.raises(pyropdf.PasswordError):
            doc.catalog["FixtureEmbedded"].read()
        doc.catalog["KeepThisEdit"] = 1
        doc.unlock(owner)
        assert doc.catalog["FixtureNamed"].read() == b"Named private data"
        assert doc.catalog["FixtureEmbedded"].read() == b"Embedded private data"
        assert doc.catalog["KeepThisEdit"] == 1
        for incremental in (True, False):
            output = doc.to_bytes(encryption=Encryption.KEEP, incremental=incremental)
            with pyropdf.open(output, credential=owner) as reopened:
                assert reopened.catalog["FixtureNamed"].read() == b"Named private data"
                assert reopened.catalog["FixtureEmbedded"].read() == b"Embedded private data"
                assert reopened.catalog["KeepThisEdit"] == 1
            with pyropdf.open(output, credential=editor) as limited, pytest.raises(pyropdf.PasswordError):
                limited.catalog["FixtureEmbedded"].read()


@pytest.mark.parametrize("use_aes", [True, False], ids=["AESV2", "V2"])
@pytest.mark.spec("crypt-filter", "read")
@pytest.mark.usefixtures("_pyhanko_oracle")
def test_explicit_legacy_filter_uses_the_global_key_from_an_independent_writer(*, use_aes: bool) -> None:
    import io  # noqa: PLC0415

    from pyhanko.pdf_utils import generic  # noqa: PLC0415
    from pyhanko.pdf_utils.crypt.standard import StandardSecurityHandler, StandardSecuritySettingsRevision  # noqa: PLC0415
    from pyhanko.pdf_utils.writer import PdfFileWriter  # noqa: PLC0415

    writer = PdfFileWriter(stream_xrefs=False)
    handler = StandardSecurityHandler.build_from_pw_legacy(
        StandardSecuritySettingsRevision.RC4_OR_AES128,
        writer.document_id[0],
        "owner",
        "user",
        use_aes128=use_aes,
    )
    writer._assign_security_handler(handler)  # noqa: SLF001 - supply the independent writer's exact R4 configuration.
    payload = b"a stream with the shared key"
    stream = generic.StreamObject(stream_data=payload)
    stream.compress()
    stream.add_crypt_filter(generic.pdf_name("/StdCF"), handler=handler)
    writer.root[generic.pdf_name("/WithCrypt")] = writer.add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    original = buffer.getvalue()
    with pyropdf.open(original, password="user") as doc:
        assert doc.catalog["WithCrypt"].read() == payload
        saved = doc.to_bytes(incremental=True, encryption=Encryption.KEEP)
        assert saved == original


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.spec("crypt-filters", "edit")
def test_invalid_embedded_file_default_refuses_atomically(tmp_path: Path, *, incremental: bool) -> None:
    with pyropdf.open(numbered(1)) as doc:
        initial = doc.to_bytes(encryption=Encryption(owner_password="owner"))
    with pyropdf.open(initial, password="owner") as doc:
        path = tmp_path / "keep.pdf"
        path.write_bytes(initial)
        doc.trailer["Encrypt"]["EFF"] = pyropdf.Name("Undefined")
        with pytest.raises(pyropdf.MalformedPdfError, match="undefined"):
            doc.save(path, incremental=incremental, encryption=Encryption.KEEP)
        assert path.read_bytes() == initial


@pytest.mark.spec("crypt-filter", "read")
def test_undefined_crypt_name_on_an_existing_stream_refuses_its_data() -> None:
    with pyropdf.open(numbered(1)) as doc:
        ref = doc.objects.add_stream(b"declared clear data", filter=None)
        doc.objects[ref]["Filter"] = pyropdf.Name("Crypt")
        doc.objects[ref]["DecodeParms"] = {"Name": pyropdf.Name("Identity")}
        original = doc.to_bytes(encryption=Encryption(user_password="pw"))
    assert original.count(b"/Name /Identity") == 1
    damaged = original.replace(b"/Name /Identity", b"/Name /UnknownX")
    with pyropdf.open(damaged, password="pw") as doc, pytest.raises(pyropdf.MalformedPdfError, match="undefined"):
        doc.objects[ref].read()


@pytest.mark.parametrize("selected", ["Identity", "StdCF"])
@pytest.mark.parametrize("repeat_count", [False, True])
@pytest.mark.spec("crypt-filter", "read")
@pytest.mark.usefixtures("_pyhanko_oracle")
def test_object_stream_members_obey_the_containers_explicit_crypt_filter(
    monkeypatch: pytest.MonkeyPatch, selected: str, *, repeat_count: bool
) -> None:
    import io  # noqa: PLC0415

    from pyhanko.pdf_utils import generic  # noqa: PLC0415
    from pyhanko.pdf_utils.writer import PdfFileWriter  # noqa: PLC0415
    from pyhanko.pdf_utils.xref import ObjectStream  # noqa: PLC0415

    writer = PdfFileWriter()
    writer.encrypt("owner", "user", pdf_mac=False)
    container = writer.prepare_object_stream()
    materialise = container.as_pdf_object

    def with_filter() -> generic.StreamObject:
        result = materialise()
        result.add_crypt_filter(generic.pdf_name(f"/{selected}"), handler=writer.security_handler)
        if repeat_count:
            result[generic.pdf_name("/X")] = generic.NumberObject(1)
        return result

    assert isinstance(container, ObjectStream)
    monkeypatch.setattr(container, "as_pdf_object", with_filter)
    ref = writer.add_object(generic.TextStringObject("object stream member"), obj_stream=container)
    writer.root[generic.pdf_name("/Packed")] = ref
    output = io.BytesIO()
    writer.write(output)
    data = output.getvalue()
    if repeat_count:
        # The last /N supplies the member count. Equal-width replacements keep xref offsets valid.
        assert data.count(b"/N 1\n/First") == data.count(b"/X 1\n/Length") == 1
        data = data.replace(b"/N 1\n/First", b"/N 0\n/First").replace(b"/X 1\n/Length", b"/N 1\n/Length")
    with pyropdf.open(data, password="user") as doc:
        assert doc.catalog["Packed"] == b"object stream member"


@pytest.mark.spec("crypt-filters", "interpret")
@pytest.mark.parametrize("handler", ["password", "recipient"])
@pytest.mark.parametrize("selected", ["default", "Identity"])
def test_aes256_named_page_content_matches_the_clear_render(handler: str, selected: str) -> None:
    with pyropdf.open(numbered(1)) as doc:
        content = doc.objects[doc.objects.add_stream(b"0 g 11 12 18 33 re f\n", filter=None)]
        doc.pages[0].obj["Contents"] = content
        expected = doc.pages[0].pixmap(dpi=20).to_bytes()
        default = "StdCF" if handler == "password" else "DefaultCryptFilter"
        content["Filter"] = pyropdf.Name("Crypt")
        content["DecodeParms"] = {"Name": pyropdf.Name(default if selected == "default" else selected)}
        if handler == "password":
            saved = doc.to_bytes(encryption=Encryption(user_password="pw"))
            reopened = pyropdf.open(saved, password="pw")
        else:
            owner = recipient("owner")
            saved = doc.to_bytes(encryption=pyropdf.RecipientEncryption(recipients={owner.certificate: None}))
            reopened = pyropdf.open(saved, credential=owner)
    with reopened:
        assert reopened.pages[0].pixmap(dpi=20).to_bytes() == expected


@pytest.mark.parametrize("role", ["EF", "RF"])
@pytest.mark.parametrize("incremental", [True, False])
@pytest.mark.spec("crypt-filters", "read", "edit", "preserve")
@pytest.mark.usefixtures("_pyhanko_oracle")
def test_untyped_file_streams_follow_their_current_file_specification(role: str, *, incremental: bool) -> None:
    import io  # noqa: PLC0415

    from pyhanko.pdf_utils import generic  # noqa: PLC0415
    from pyhanko.pdf_utils.reader import PdfFileReader  # noqa: PLC0415

    raw_a, raw_b = b"first untyped payload", b"second untyped payload"
    with pyropdf.open(numbered(1)) as doc:
        doc.attachments.add("file.bin", raw_a)
        spec = doc.catalog["Names"]["EmbeddedFiles"]["Names"][1]
        original = spec["EF"]["F"]
        original.write(raw_a, filter=None)
        del original["Type"]
        other = doc.objects[doc.objects.add_stream(raw_b, filter=None)]
        if role == "RF":
            links = doc.objects[doc.objects.add([b"related.bin", original])]
            spec["RF"] = doc.objects[doc.objects.add({"F": links})]
            base = doc.objects[doc.objects.add_stream(b"primary file", filter=None)]
            base["Type"] = pyropdf.Name("EmbeddedFile")
            spec["EF"] = {"F": base}
        else:
            spec["EF"] = doc.objects[doc.objects.add({"F": original})]
        first, second = original.xref, other.xref
        protected = doc.to_bytes(
            encryption=Encryption(user_password="user", owner_password="owner"), object_streams=True
        )
    with pyropdf.open(protected, password="owner") as doc:
        doc.trailer["Encrypt"]["EFF"] = pyropdf.Name("Identity")
        initial = doc.to_bytes(incremental=incremental, encryption=Encryption.KEEP)
    with pyropdf.open(initial, password="owner") as doc:
        assert doc.objects[first].read() == raw_a
        assert doc.objects[second].read() == raw_b
        spec = doc.catalog["Names"]["EmbeddedFiles"]["Names"][1]
        if role == "EF":
            spec["EF"] = {"F": doc.objects[second]}
        else:
            spec["RF"] = {"F": [b"related.bin", doc.objects[second]]}
        changed = doc.to_bytes(incremental=incremental, encryption=Encryption.KEEP)
    for data, clear_id, secret_id, clear_data, secret_data in (
        (initial, first, second, raw_a, raw_b),
        (changed, second, first, raw_b, raw_a),
    ):
        reader = PdfFileReader(io.BytesIO(data))
        reader.decrypt("user")
        # Inspect the actual active objects, including the last revision of
        # an incremental edit, independently of any reader's type heuristic.
        raw_clear = reader.get_object(generic.Reference(clear_id, 0, reader), never_decrypt=True).encoded_data
        raw_secret = reader.get_object(generic.Reference(secret_id, 0, reader), never_decrypt=True).encoded_data
        assert raw_clear == clear_data
        assert raw_secret != secret_data
        with pyropdf.open(data, password="user") as doc:
            assert doc.objects[clear_id].read() == clear_data
            assert doc.objects[secret_id].read() == secret_data


@pytest.mark.spec("crypt-filters", "edit")
def test_invalid_related_file_pairs_refuse_a_new_default_without_overwriting_the_original(tmp_path: Path) -> None:
    with pyropdf.open(numbered(1)) as doc:
        doc.attachments.add("file.bin", b"payload")
        initial = doc.to_bytes(encryption=Encryption(owner_password="owner"))
    with pyropdf.open(initial, password="owner") as doc:
        spec = doc.catalog["Names"]["EmbeddedFiles"]["Names"][1]
        spec["RF"] = {"F": [b"missing stream"]}
        doc.trailer["Encrypt"]["EFF"] = pyropdf.Name("Identity")
        path = tmp_path / "original.pdf"
        path.write_bytes(initial)
        with pytest.raises(pyropdf.MalformedPdfError, match="name and stream pairs"):
            doc.save(path, incremental=True, encryption=Encryption.KEEP)
        assert path.read_bytes() == initial


_SOURCE_PAGE = b"0 g 2 3 10 8 re f\nBT /F1 7 Tf 3 23 Td (Filter) Tj ET\n"


def _source_font_resources() -> DictionaryObject:
    from pyhanko.pdf_utils import generic  # noqa: PLC0415

    name = generic.pdf_name
    font = generic.DictionaryObject(
        {name("/Type"): name("/Font"), name("/Subtype"): name("/Type1"), name("/BaseFont"): name("/Helvetica")}
    )
    return generic.DictionaryObject({name("/Font"): generic.DictionaryObject({name("/F1"): font})})


def _filter_engine_pdf(method: str, selector: str, rotation: int = 0) -> bytes:
    import io  # noqa: PLC0415

    from pyhanko.pdf_utils import generic  # noqa: PLC0415
    from pyhanko.pdf_utils.crypt.standard import StandardSecurityHandler, StandardSecuritySettingsRevision  # noqa: PLC0415
    from pyhanko.pdf_utils.writer import PageObject, PdfFileWriter  # noqa: PLC0415

    if method == "recipient":
        return _private_recipient_control(
            attachment_only=selector == "attachment",
            implicit=False,
            page_data=_SOURCE_PAGE,
            private_page=selector == "private",
        )
    writer = PdfFileWriter(stream_xrefs=False)
    stream = generic.StreamObject(stream_data=_SOURCE_PAGE)
    stream.compress()
    if method != "clear":
        handler = StandardSecurityHandler.build_from_pw_legacy(
            StandardSecuritySettingsRevision.RC4_OR_AES128,
            writer.document_id[0],
            "owner",
            "user",
            use_aes128=method == "AESV2",
        )
        writer._assign_security_handler(handler)  # noqa: SLF001 - choose the independent writer's R4 method.
        if selector != "implicit":
            name = generic.pdf_name("/StdCF" if selector == "named" else "/Identity")
            stream.add_crypt_filter(name, handler=handler)
    page = PageObject(writer.add_object(stream), [0, 0, 40, 50], resources=_source_font_resources())
    page[generic.pdf_name("/Rotate")] = generic.NumberObject(rotation)
    writer.insert_page(page)
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


@pytest.mark.parametrize(
    ("method", "selector"),
    [
        ("AESV2", "named"),
        ("AESV2", "Identity"),
        ("AESV2", "implicit"),
        ("V2", "named"),
        ("V2", "Identity"),
        ("V2", "implicit"),
        ("recipient", "private"),
        ("recipient", "attachment"),
    ],
)
@pytest.mark.usefixtures("_pyhanko_oracle")
def test_named_filter_engine_reference_pages(method: str, selector: str) -> None:
    original = _filter_engine_pdf(method, selector)
    password = None if method == "recipient" else "user"
    credential = recipient("owner") if method == "recipient" else None
    with pyropdf.open(original, password=password, credential=credential) as doc:
        assert doc.pages[0].obj["Contents"].read() == _SOURCE_PAGE
        doc.pages[0].rotation = 90
        for incremental in (False, True):
            saved = doc.to_bytes(encryption=Encryption.KEEP, incremental=incremental)
            assert saved.startswith(original) == incremental
            with pyropdf.open(saved, password=password, credential=credential) as reopened:
                assert reopened.pages[0].rotation == 90
                assert reopened.pages[0].obj["Contents"].read() == _SOURCE_PAGE
            if method == "recipient":
                with pyropdf.open(saved, credential=recipient("editor")) as limited:
                    with pytest.raises(pyropdf.PasswordError):
                        limited.catalog["FixtureEmbedded"].read()
                    if selector == "private":
                        with pytest.raises(pyropdf.PasswordError):
                            limited.pages[0].obj["Contents"].read()
                    else:
                        assert limited.pages[0].obj["Contents"].read() == _SOURCE_PAGE


@pytest.mark.spec("crypt-filters", "interpret")
@pytest.mark.parametrize(
    ("method", "selector", "identity"),
    [
        ("AESV2", "Identity", None),
        ("AESV2", "implicit", None),
        ("V2", "Identity", None),
        ("V2", "implicit", None),
        ("recipient", "attachment", None),
        ("recipient", "attachment", "editor"),
        ("recipient", "attachment", "owner"),
    ],
)
@pytest.mark.usefixtures("_pyhanko_oracle")
def test_available_filter_engine_pages_match_independent_controls(
    method: str, selector: str, identity: str | None
) -> None:
    password = None if method == "recipient" else "user"
    credential = recipient(identity) if identity is not None else None
    with pyropdf.open(_filter_engine_pdf(method, selector), password=password, credential=credential) as doc:
        for rotation in (0, 90):
            if rotation:
                doc.pages[0].rotation = rotation
            with pyropdf.open(_filter_engine_pdf("clear", "implicit", rotation)) as reference:
                expected = reference.pages[0]
                assert expected.text() == "Filter\n"
                pixels = expected.pixmap()
                assert (pixels.width, pixels.height) == ((40, 50) if rotation == 0 else (50, 40))
                point = (4, 43) if rotation == 0 else (5, 5)
                assert pixels.pixel(*point) == (0, 0, 0)
                assert doc.pages[0].pixmap().to_bytes() == pixels.to_bytes()
                assert doc.pages[0].text() == expected.text()
