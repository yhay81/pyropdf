"""Regenerate the independent ISO/TS 32004:2024 fixtures with the locked pyHanko."""

from __future__ import annotations

import io
from pathlib import Path

from asn1crypto import keys, x509
from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.writer import PageObject, PdfFileWriter
from pyhanko.sign import signers
from pyhanko_certvalidator.registry import SimpleCertificateStore

HERE = Path(__file__).resolve().parent
KEYS = HERE.parent / "recipients"


def writer(*, stream_xrefs: bool = True) -> PdfFileWriter:
    """Create an encrypted page whose content is independently identifiable."""
    output = PdfFileWriter(stream_xrefs=stream_xrefs)
    stream = output.add_object(generic.StreamObject(stream_data=b"0 0 1 rg 8 8 16 16 re f\n"))
    page = PageObject(stream, (0, 0, 100, 100))
    page[generic.pdf_name("/FixtureLabel")] = generic.pdf_string("independent integrity source")
    output.insert_page(page)
    return output


def main() -> None:
    """Write standalone table/stream and signature-attached revisions."""
    for mode in ("table", "stream"):
        output = writer(stream_xrefs=mode == "stream")
        output.encrypt("ownerpw", "userpw", pdf_mac=True)
        buf = io.BytesIO()
        output.write(buf)
        (HERE / f"standalone-{mode}.pdf").write_bytes(buf.getvalue())

    update = IncrementalPdfFileWriter(io.BytesIO((HERE / "standalone-stream.pdf").read_bytes()))
    update.encrypt("ownerpw")
    signer = signers.SimpleSigner(
        signing_cert=x509.Certificate.load((KEYS / "recipient-owner.cer").read_bytes()),
        signing_key=keys.PrivateKeyInfo.load((KEYS / "recipient-owner.key").read_bytes()),
        cert_registry=SimpleCertificateStore(),
    )
    signed = signers.sign_pdf(update, signers.PdfSignatureMetadata(field_name="Example"), signer=signer)
    (HERE / "attached.pdf").write_bytes(signed.getvalue())

    output = writer()
    output.encrypt_pubkey([x509.Certificate.load((KEYS / "recipient-owner.cer").read_bytes())], pdf_mac=True)
    buf = io.BytesIO()
    output.write(buf)
    (HERE / "public-key.pdf").write_bytes(buf.getvalue())


if __name__ == "__main__":
    main()
