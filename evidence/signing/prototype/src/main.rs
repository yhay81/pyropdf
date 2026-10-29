//! The CMS half of the signing prototype: a PAdES B-B signature over the
//! byte ranges of a PDF (ETSI EN 319 142-1 section 5.1), and the signature
//! time-stamp token that makes it B-T (section 5.3).
//!
//! `sign KEY.pem CERT.der DATA OUT.der` signs the bytes of DATA, the
//! signed byte ranges concatenated; `signature CMS.der` prints the hex
//! signature value that a time-stamp covers; `timestamp CMS.der TOKEN.der
//! OUT.der` adds the token as the signer's unsigned attribute.

use std::fs;

use cms::builder::{SignedDataBuilder, SignerInfoBuilder};
use cms::cert::{CertificateChoices, IssuerAndSerialNumber};
use cms::content_info::ContentInfo;
use cms::signed_data::{EncapsulatedContentInfo, SignedData, SignerIdentifier, SignerInfos};
use const_oid::ObjectIdentifier;
use const_oid::db::rfc5911::{ID_DATA, ID_SIGNED_DATA};
use der::asn1::SetOfVec;
use der::{Any, Decode, Encode};
use p256::ecdsa::{DerSignature, SigningKey};
use p256::pkcs8::DecodePrivateKey;
use sha2::{Digest, Sha256};
use spki::AlgorithmIdentifierOwned;
use x509_cert::Certificate;
use x509_cert::attr::{Attribute, AttributeValue};

/// id-aa-signingCertificateV2 (RFC 5035), which PAdES B-B requires.
const SIGNING_CERTIFICATE_V2: ObjectIdentifier =
    ObjectIdentifier::new_unwrap("1.2.840.113549.1.9.16.2.47");
/// id-aa-timeStampToken (RFC 3161 appendix A).
const TIME_STAMP_TOKEN: ObjectIdentifier =
    ObjectIdentifier::new_unwrap("1.2.840.113549.1.9.16.2.14");
const SHA256: ObjectIdentifier = ObjectIdentifier::new_unwrap("2.16.840.1.101.3.4.2.1");

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

/// Returns the SigningCertificateV2 attribute of `cert`: one ESSCertIDv2
/// with the default SHA-256 hash, so only the certificate hash is written.
fn signing_certificate(cert: &[u8]) -> Result<Attribute> {
    let hash = Sha256::digest(cert);
    let octets = der::asn1::OctetString::new(hash.as_slice())?.to_der()?;
    let id = sequence(&octets);
    let value = sequence(&sequence(&id));
    let mut values = SetOfVec::new();
    values.insert(AttributeValue::from_der(&value)?)?;
    Ok(Attribute {
        oid: SIGNING_CERTIFICATE_V2,
        values,
    })
}

/// Wraps `content`, shorter than 128 bytes, in a DER SEQUENCE.
fn sequence(content: &[u8]) -> Vec<u8> {
    let mut out = vec![0x30, u8::try_from(content.len()).expect("a short sequence")];
    out.extend_from_slice(content);
    out
}

/// The builder's errors implement no `std::error::Error`.
fn built<T>(result: std::result::Result<T, cms::builder::Error>) -> Result<T> {
    result.map_err(|error| format!("{error:?}").into())
}

fn sign(key: &str, cert: &str, data: &str, out: &str) -> Result<()> {
    let key = SigningKey::from_pkcs8_pem(&fs::read_to_string(key)?)?;
    let cert_der = fs::read(cert)?;
    let cert = Certificate::from_der(&cert_der)?;
    let digest = Sha256::digest(fs::read(data)?);
    let content = EncapsulatedContentInfo {
        econtent_type: ID_DATA,
        econtent: None,
    };
    let sha256 = AlgorithmIdentifierOwned {
        oid: SHA256,
        parameters: None,
    };
    let sid = SignerIdentifier::IssuerAndSerialNumber(IssuerAndSerialNumber {
        issuer: cert.tbs_certificate.issuer.clone(),
        serial_number: cert.tbs_certificate.serial_number.clone(),
    });
    let mut signer = built(SignerInfoBuilder::new(
        &key,
        sid,
        sha256.clone(),
        &content,
        Some(digest.as_slice()),
    ))?;
    built(signer.add_signed_attribute(signing_certificate(&cert_der)?))?;
    let mut builder = SignedDataBuilder::new(&content);
    built(builder.add_digest_algorithm(sha256))?;
    built(builder.add_certificate(CertificateChoices::Certificate(cert)))?;
    built(builder.add_signer_info::<SigningKey, DerSignature>(signer))?;
    let signed = built(builder.build())?;
    fs::write(out, signed.to_der()?)?;
    Ok(())
}

fn signed_data(cms: &str) -> Result<SignedData> {
    let info = ContentInfo::from_der(&fs::read(cms)?)?;
    Ok(info.content.decode_as::<SignedData>()?)
}

fn signature(cms: &str) -> Result<()> {
    let signed = signed_data(cms)?;
    let signer = signed.signer_infos.0.iter().next().ok_or("no signer")?;
    let hex: String = signer
        .signature
        .as_bytes()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    println!("{hex}");
    Ok(())
}

fn timestamp(cms: &str, token: &str, out: &str) -> Result<()> {
    let mut signed = signed_data(cms)?;
    let mut signers: Vec<_> = signed.signer_infos.0.into_vec();
    let signer = signers.first_mut().ok_or("no signer")?;
    let mut values = SetOfVec::new();
    values.insert(Any::from_der(&fs::read(token)?)?)?;
    let mut attributes = SetOfVec::new();
    attributes.insert(Attribute {
        oid: TIME_STAMP_TOKEN,
        values,
    })?;
    signer.unsigned_attrs = Some(attributes);
    signed.signer_infos = SignerInfos(SetOfVec::try_from(signers)?);
    let info = ContentInfo {
        content_type: ID_SIGNED_DATA,
        content: Any::encode_from(&signed)?,
    };
    fs::write(out, info.to_der()?)?;
    Ok(())
}

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    match args.as_slice() {
        ["sign", key, cert, data, out] => sign(key, cert, data, out),
        ["signature", cms] => signature(cms),
        ["timestamp", cms, token, out] => timestamp(cms, token, out),
        _ => Err("usage: sign KEY CERT DATA OUT | signature CMS | timestamp CMS TOKEN OUT".into()),
    }
}
