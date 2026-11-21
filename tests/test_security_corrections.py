"""Wire controls for the corrected PDF standard password algorithms (7.6.4)."""

from __future__ import annotations

import io
import sysconfig
from pathlib import Path
from typing import Literal

import pikepdf
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import pyropdf

ASSETS = Path(__file__).with_name("assets") / "encrypted"
USER = "userpw"
OWNER = "ownerpw"


@pytest.mark.parametrize("case", ["read-r5", "read-r6", "write-r6"])
def test_permissions_are_one_ecb_block_under_the_document_key(case: str) -> None:
    revision = 5 if case == "read-r5" else 6
    name = "user-aes-256-r5.pdf" if revision == 5 else "user-aes-256.pdf"
    original = (ASSETS / name).read_bytes()
    if case == "write-r6":
        with pyropdf.open(original, password=OWNER) as author:
            original = author.to_bytes(
                encryption=pyropdf.Encryption(method="aes-256", user_password=USER, owner_password=OWNER)
            )
    with pyropdf.open(original, password=USER) as doc:
        doc.metadata["subject"] = "retained encryption"
        saved = doc.to_bytes(encryption=pyropdf.Encryption.KEEP, incremental=True)
    for data in (original, saved):
        with pikepdf.open(io.BytesIO(data), password=OWNER) as independent:
            enc = independent.trailer.Encrypt
            assert revision == enc.R
            block = bytes(enc.Perms)
            assert len(block) == 16
            decrypt = Cipher(algorithms.AES(independent.encryption.encryption_key), modes.ECB()).decryptor()  # noqa: S305 - ISO 32000-2 correction 24
            plain = decrypt.update(block) + decrypt.finalize()
            assert int.from_bytes(plain[:4], "little") == int(enc.P) & 0xFFFFFFFF
            assert plain[4:8] == b"\xff" * 4
            assert plain[8:12] == (b"T" if enc.get("/EncryptMetadata", True) else b"F") + b"adb"


@pytest.mark.parametrize("role", ["user", "owner"])
def test_revision_six_uses_the_correct_password_hash_sequence(role: str) -> None:
    if sysconfig.get_config_var("Py_GIL_DISABLED") == 1:
        pytest.skip("pyHanko imports enable the GIL on free-threaded Python")
    # Use pyHanko's independent Algorithm 2.B implementation to check the salts
    # and hashes written by pyropdf, including the owner-only 48-byte user entry.
    from pyhanko.pdf_utils.crypt.standard import _r6_hash_algo  # noqa: PLC0415

    with pyropdf.open((ASSETS / "user-aes-256.pdf").read_bytes(), password=OWNER) as doc:
        data = doc.to_bytes(encryption=pyropdf.Encryption(method="aes-256", user_password=USER, owner_password=OWNER))
    password = USER if role == "user" else OWNER
    with pikepdf.open(io.BytesIO(data), password=password) as independent:
        assert independent.owner_password_matched == (role == "owner")
        enc = independent.trailer.Encrypt
        assert enc.R == 6
        user = bytes(enc.U)
        entry = user if role == "user" else bytes(enc.O)
        assert len(user) == len(entry) == 48
        owner_input = user if role == "owner" else None
        pw = password.encode()
        assert _r6_hash_algo(pw, entry[32:40], owner_input) == entry[:32]
        assert _r6_hash_algo(pw, entry[32:40], None if role == "owner" else user) != entry[:32]
        key_hash = _r6_hash_algo(pw, entry[40:], owner_input)
        encrypted_key = bytes(enc.UE if role == "user" else enc.OE)
        decrypt = Cipher(algorithms.AES(key_hash), modes.CBC(bytes(16))).decryptor()
        assert decrypt.update(encrypted_key) + decrypt.finalize() == independent.encryption.encryption_key


@pytest.mark.parametrize("revision", [4, 6])
def test_standard_handler_authoring_selects_one_named_docopen_filter(revision: int) -> None:
    source = (ASSETS / "user-aes-256.pdf").read_bytes()
    method = "aes-128" if revision == 4 else "aes-256"
    with pyropdf.open(source, password=OWNER) as doc:
        output = doc.to_bytes(encryption=pyropdf.Encryption(method=method, user_password=USER, owner_password=OWNER))
    with pikepdf.open(io.BytesIO(output), password=USER) as independent:
        security = independent.trailer.Encrypt
        assert security.Filter == pikepdf.Name.Standard
        assert set(security.CF.keys()) == {"/StdCF"}
        assert security.StrF == security.StmF == pikepdf.Name.StdCF
        assert security.CF.StdCF.get("/AuthEvent", pikepdf.Name.DocOpen) == pikepdf.Name.DocOpen
        assert (pikepdf.Name.AESV2 if revision == 4 else pikepdf.Name.AESV3) == security.CF.StdCF.CFM


@pytest.mark.parametrize("role", ["owner", "user"])
@pytest.mark.parametrize("revision", [2, 3, 4])
def test_legacy_passwords_use_the_32_byte_padding_step_for_both_roles(revision: Literal[2, 3, 4], role: str) -> None:
    # Fixed test inputs contain short passwords to pad and long passwords to truncate.
    for size in (8, 40):
        user, owner = "u" * size, "o" * size
        out = io.BytesIO()
        with pikepdf.open(ASSETS / "user-rc4-40.pdf", password=OWNER) as original:
            original.save(
                out,
                encryption=pikepdf.Encryption(
                    owner=owner, user=user, R=revision, aes=revision == 4, metadata=revision == 4
                ),
            )
        password = owner if role == "owner" else user
        with pyropdf.open(out.getvalue(), password=password[:32]) as doc:
            assert doc.access == (pyropdf.Access.OWNER if role == "owner" else pyropdf.Access.USER)
            assert len(doc.pages) == 2


@pytest.mark.parametrize("case", ["standard-rc4-r4", "standard-aes-r5"])
def test_existing_standard_handler_retains_a_single_docopen_crypt_filter(case: str) -> None:
    filename = "user-r4-rc4-128.pdf" if case == "standard-rc4-r4" else "user-aes-256-r5.pdf"
    original = (ASSETS / filename).read_bytes()
    with pyropdf.open(original, password=USER) as doc:
        doc.metadata["subject"] = "Retain the named filter"
        for saved in (
            original,
            doc.to_bytes(encryption=pyropdf.Encryption.KEEP),
            doc.to_bytes(encryption=pyropdf.Encryption.KEEP, incremental=True),
        ):
            with pikepdf.open(io.BytesIO(saved), password=OWNER) as pdf:
                enc = pdf.trailer.Encrypt
                assert set(enc.CF.keys()) == {"/StdCF"}
                assert enc.StrF == enc.StmF == pikepdf.Name.StdCF
                assert enc.CF.StdCF.get("/AuthEvent", pikepdf.Name.DocOpen) == pikepdf.Name.DocOpen
                assert (pikepdf.Name.V2 if case == "standard-rc4-r4" else pikepdf.Name.AESV3) == enc.CF.StdCF.CFM
