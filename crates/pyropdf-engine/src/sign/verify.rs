//! Reading a signature's CMS SignedData for verification (RFC 5652 section
//! 5): its signer, the digest that it signs, the signature checks that a
//! verifier makes, and the time-stamp token that it may carry (RFC 3161).
//!
//! This module parses and checks ECDSA; it never fetches anything. The
//! caller digests the byte ranges, and verifies RSA checks with a library
//! of its own, as pyropdf holds no RSA code (`Cargo.toml` says why).

use cms::cert::CertificateChoices;
use cms::content_info::ContentInfo;
use cms::signed_data::{SignedData, SignerIdentifier, SignerInfo};
use der::asn1::{ObjectIdentifier, OctetString};
use der::{Any, Decode, Encode};
use p256::ecdsa::signature::hazmat::PrehashVerifier as _;
use x509_cert::Certificate;
use x509_cert::spki::SubjectPublicKeyInfoRef;
use x509_cert::time::Time;

use super::{EC_PUBLIC_KEY, Error, PRIME256V1, RSA_ENCRYPTION, SECP384R1};

const ID_SIGNED_DATA: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.113549.1.7.2");
const ID_CT_TST_INFO: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.113549.1.9.16.1.4");
const MESSAGE_DIGEST: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.113549.1.9.4");
const SIGNING_TIME: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.113549.1.9.5");
const TIME_STAMP_TOKEN: ObjectIdentifier =
    ObjectIdentifier::new_unwrap("1.2.840.113549.1.9.16.2.14");
const SUBJECT_KEY_IDENTIFIER: ObjectIdentifier = ObjectIdentifier::new_unwrap("2.5.29.14");
const RSASSA_PSS: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.113549.1.1.10");

/// The most certificates of one SignedData that a verification reads.
pub const MAX_CERTIFICATES: usize = 64;

/// A signature to verify: `signature` by the key of `key` (a DER
/// SubjectPublicKeyInfo) over `message`, hashed with `digest`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Check {
    /// The signer's DER SubjectPublicKeyInfo.
    pub key: Vec<u8>,
    /// `"ecdsa"`, `"rsa-pkcs1"`, `"rsa-pss"`, or the dotted OID of a
    /// scheme that pyropdf does not name.
    pub scheme: String,
    /// The digest algorithm, as Python's `hashlib` spells it, or its OID.
    pub digest: String,
    /// The signature value.
    pub signature: Vec<u8>,
    /// The bytes signed; `None` when the signature signs the content
    /// itself, which only the caller holds.
    pub message: Option<Vec<u8>>,
}

/// The parts of a CMS SignedData that a verification reads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Signed {
    /// The digest algorithm of the content, as `hashlib` spells it, or its
    /// OID.
    pub digest: String,
    /// The message-digest attribute, which the content's digest must match;
    /// `None` without signed attributes.
    pub message_digest: Option<Vec<u8>>,
    /// The signing-time attribute, in seconds since 1970.
    pub signing_time: Option<u64>,
    /// The DER certificates that the SignedData carries, at most
    /// [`MAX_CERTIFICATES`].
    pub certificates: Vec<Vec<u8>>,
    /// The index in `certificates` of the signer's certificate.
    pub signer: Option<usize>,
    /// The signer's signature, `None` without the signer's certificate.
    pub check: Option<Check>,
    /// The signature value, which a time-stamp token stamps.
    pub signature: Vec<u8>,
    /// The time-stamp token of the unsigned attributes.
    pub timestamp: Option<Box<TimeStamp>>,
}

/// An RFC 3161 time-stamp token.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TimeStamp {
    /// The token's own SignedData, whose content is the TSTInfo.
    pub signed: Signed,
    /// The DER TSTInfo, whose digest the token's message digest is.
    pub tst_info: Vec<u8>,
    /// The time that the token states, in seconds since 1970.
    pub time: u64,
    /// Whether the token's message imprint is the digest of the signature
    /// value that it stamps.
    pub imprint: bool,
}

/// The parts of a certificate that building and checking a chain read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CertificateInfo {
    /// The subject, as RFC 4514 writes it.
    pub subject: String,
    /// The DER subject and issuer names, which chain building matches.
    pub subject_der: Vec<u8>,
    /// The DER issuer name.
    pub issuer_der: Vec<u8>,
    /// The validity period, in seconds since 1970.
    pub validity: (u64, u64),
    /// The certificate's signature by its issuer's key, whose `key` the
    /// caller fills in with the issuer's.
    pub check: Check,
    /// The DER SubjectPublicKeyInfo.
    pub key: Vec<u8>,
}

fn invalid(what: &str) -> impl Fn(der::Error) -> Error + '_ {
    move |error| Error::invalid(what, error)
}

/// Returns the `hashlib` name of a digest algorithm, or its OID.
fn digest_name(oid: ObjectIdentifier) -> String {
    match oid.to_string().as_str() {
        "1.3.14.3.2.26" => "sha1".to_owned(),
        "2.16.840.1.101.3.4.2.1" => "sha256".to_owned(),
        "2.16.840.1.101.3.4.2.2" => "sha384".to_owned(),
        "2.16.840.1.101.3.4.2.3" => "sha512".to_owned(),
        other => other.to_owned(),
    }
}

/// Returns the scheme and digest of a signature algorithm, the digest
/// `digest` when the algorithm names only the key.
fn scheme(algorithm: ObjectIdentifier, digest: &str) -> (String, String) {
    let named = |scheme: &str, digest: &str| (scheme.to_owned(), digest.to_owned());
    match algorithm.to_string().as_str() {
        "1.2.840.10045.4.1" => named("ecdsa", "sha1"),
        "1.2.840.10045.4.3.2" => named("ecdsa", "sha256"),
        "1.2.840.10045.4.3.3" => named("ecdsa", "sha384"),
        "1.2.840.10045.4.3.4" => named("ecdsa", "sha512"),
        "1.2.840.113549.1.1.5" => named("rsa-pkcs1", "sha1"),
        "1.2.840.113549.1.1.11" => named("rsa-pkcs1", "sha256"),
        "1.2.840.113549.1.1.12" => named("rsa-pkcs1", "sha384"),
        "1.2.840.113549.1.1.13" => named("rsa-pkcs1", "sha512"),
        _ if algorithm == EC_PUBLIC_KEY => named("ecdsa", digest),
        _ if algorithm == RSA_ENCRYPTION => named("rsa-pkcs1", digest),
        // The parameters name the hash; PAdES signers use the content's.
        _ if algorithm == RSASSA_PSS => named("rsa-pss", digest),
        other => named(other, digest),
    }
}

fn seconds(time: Time) -> u64 {
    time.to_unix_duration().as_secs()
}

/// Reads the CMS SignedData of a signature's `/Contents`, trailing zero
/// padding allowed.
///
/// # Errors
///
/// [`Error::Invalid`] when `contents` is not a SignedData with a signer.
pub fn read(contents: &[u8]) -> Result<Signed, Error> {
    let length = der_length(contents).unwrap_or(contents.len());
    signed_data(&contents[..length.min(contents.len())], true)
}

/// Returns the length of the DER element at the start of `data`.
fn der_length(data: &[u8]) -> Option<usize> {
    let first = usize::from(*data.get(1)?);
    if first < 0x80 {
        return Some(2 + first);
    }
    let count = first & 0x7F;
    if count == 0 || count > 4 {
        return None;
    }
    let mut length = 0usize;
    for byte in data.get(2..2 + count)? {
        length = (length << 8) | usize::from(*byte);
    }
    Some(2 + count + length)
}

fn signed_data(data: &[u8], outer: bool) -> Result<Signed, Error> {
    let info = ContentInfo::from_der(data).map_err(invalid("the signature's CMS"))?;
    if info.content_type != ID_SIGNED_DATA {
        return Err(Error::Invalid(
            "the signature is not a CMS SignedData".to_owned(),
        ));
    }
    let signed = info
        .content
        .decode_as::<SignedData>()
        .map_err(invalid("the SignedData"))?;
    let signer_info = signed
        .signer_infos
        .0
        .iter()
        .next()
        .ok_or_else(|| Error::Invalid("the SignedData has no signer".to_owned()))?;
    let mut certificates = Vec::new();
    let mut parsed = Vec::new();
    for choice in signed.certificates.iter().flat_map(|set| set.0.iter()) {
        if certificates.len() == MAX_CERTIFICATES {
            break;
        }
        if let CertificateChoices::Certificate(certificate) = choice {
            certificates.push(certificate.to_der().map_err(invalid("a certificate"))?);
            parsed.push(certificate.clone());
        }
    }
    let signer = parsed
        .iter()
        .position(|certificate| identifies(&signer_info.sid, certificate));
    let digest = digest_name(signer_info.digest_alg.oid);
    let mut message_digest = None;
    let mut signing_time = None;
    let mut message = None;
    if let Some(attributes) = &signer_info.signed_attrs {
        for attribute in attributes.iter() {
            let Some(value) = attribute.values.iter().next() else {
                continue;
            };
            if attribute.oid == MESSAGE_DIGEST {
                let value = value
                    .decode_as::<OctetString>()
                    .map_err(invalid("the message digest"))?;
                message_digest = Some(value.as_bytes().to_vec());
            } else if attribute.oid == SIGNING_TIME {
                signing_time = value.decode_as::<Time>().ok().map(seconds);
            }
        }
        if message_digest.is_none() {
            return Err(Error::Invalid(
                "the signed attributes hold no message digest".to_owned(),
            ));
        }
        message = Some(
            attributes
                .to_der()
                .map_err(invalid("the signed attributes"))?,
        );
    }
    let signature = signer_info.signature.as_bytes().to_vec();
    let check = signer.map(|index| {
        let (scheme, digest) = scheme(signer_info.signature_algorithm.oid, &digest);
        Check {
            key: parsed[index]
                .tbs_certificate()
                .subject_public_key_info()
                .to_der()
                .unwrap_or_default(),
            scheme,
            digest,
            signature: signature.clone(),
            message,
        }
    });
    let timestamp = if outer {
        token(signer_info)?.map(Box::new)
    } else {
        None
    };
    Ok(Signed {
        digest,
        message_digest,
        signing_time,
        certificates,
        signer,
        check,
        signature,
        timestamp,
    })
}

fn token(signer_info: &SignerInfo) -> Result<Option<TimeStamp>, Error> {
    let Some(value) = signer_info
        .unsigned_attrs
        .iter()
        .flat_map(|set| set.iter())
        .find_map(|attribute| {
            (attribute.oid == TIME_STAMP_TOKEN)
                .then(|| attribute.values.iter().next())
                .flatten()
        })
    else {
        return Ok(None);
    };
    let data = value.to_der().map_err(invalid("the time-stamp token"))?;
    let signed = signed_data(&data, false)?;
    let info = ContentInfo::from_der(&data).map_err(invalid("the time-stamp token"))?;
    let content = info
        .content
        .decode_as::<SignedData>()
        .map_err(invalid("the time-stamp token"))?
        .encap_content_info;
    let Some(econtent) = content
        .econtent
        .filter(|_| content.econtent_type == ID_CT_TST_INFO)
    else {
        return Err(Error::Invalid(
            "the time-stamp token holds no TSTInfo".to_owned(),
        ));
    };
    let tst_info = econtent
        .decode_as::<OctetString>()
        .map_err(invalid("the TSTInfo"))?
        .as_bytes()
        .to_vec();
    // TSTInfo: version, policy, messageImprint, serialNumber, genTime.
    let fields = Vec::<Any>::from_der(&tst_info).map_err(invalid("the TSTInfo"))?;
    let imprint = fields
        .get(2)
        .ok_or_else(|| Error::Invalid("the TSTInfo holds no message imprint".to_owned()))?;
    let (algorithm, hashed) =
        Vec::<Any>::from_der(&imprint.to_der().map_err(invalid("the imprint"))?)
            .ok()
            .and_then(|parts| {
                let algorithm = parts
                    .first()?
                    .decode_as::<x509_cert::spki::AlgorithmIdentifierOwned>()
                    .ok()?;
                let hashed = parts.get(1)?.decode_as::<OctetString>().ok()?;
                Some((algorithm.oid, hashed.as_bytes().to_vec()))
            })
            .ok_or_else(|| {
                Error::Invalid("the TSTInfo's message imprint is malformed".to_owned())
            })?;
    let time = fields
        .get(4)
        .filter(|field| der::Tagged::tag(*field) == der::Tag::GeneralizedTime)
        .and_then(|field| generalized_time(field.value()))
        .ok_or_else(|| Error::Invalid("the TSTInfo holds no time".to_owned()))?;
    let stamped = super::digest_named(&digest_name(algorithm), &[signer_info.signature.as_bytes()]);
    Ok(Some(TimeStamp {
        signed,
        tst_info,
        time,
        imprint: stamped.is_some_and(|digest| digest == hashed),
    }))
}

/// Returns the seconds since 1970 of a UTC GeneralizedTime, whose fraction
/// of a second (RFC 3161 section 2.4.2 allows one) is dropped.
fn generalized_time(value: &[u8]) -> Option<u64> {
    let (digits, rest) = value.split_at_checked(14)?;
    let fraction = rest.strip_suffix(b"Z")?;
    let whole = fraction.is_empty()
        || fraction
            .strip_prefix(b".")
            .is_some_and(|digits| !digits.is_empty() && digits.iter().all(u8::is_ascii_digit));
    if !whole || !digits.iter().all(u8::is_ascii_digit) {
        return None;
    }
    let number = |range: std::ops::Range<usize>| {
        digits[range]
            .iter()
            .fold(0u16, |total, digit| total * 10 + u16::from(digit - b'0'))
    };
    let year = number(0..4);
    let month = u8::try_from(number(4..6)).ok()?;
    let day = u8::try_from(number(6..8)).ok()?;
    let hour = u8::try_from(number(8..10)).ok()?;
    let minute = u8::try_from(number(10..12)).ok()?;
    let second = u8::try_from(number(12..14)).ok()?;
    der::DateTime::new(year, month, day, hour, minute, second)
        .ok()
        .map(|time| time.unix_duration().as_secs())
}

fn identifies(sid: &SignerIdentifier, certificate: &Certificate) -> bool {
    let tbs = certificate.tbs_certificate();
    match sid {
        SignerIdentifier::IssuerAndSerialNumber(id) => {
            id.issuer == *tbs.issuer() && id.serial_number == *tbs.serial_number()
        }
        SignerIdentifier::SubjectKeyIdentifier(id) => tbs
            .extensions()
            .into_iter()
            .flatten()
            .filter(|extension| extension.extn_id == SUBJECT_KEY_IDENTIFIER)
            .any(|extension| {
                OctetString::from_der(extension.extn_value.as_bytes())
                    .is_ok_and(|value| value.as_bytes() == id.0.as_bytes())
            }),
    }
}

/// Reads the parts of a DER certificate that chain building and checking
/// read.
///
/// # Errors
///
/// [`Error::Invalid`] when `data` is not a certificate.
pub fn certificate(data: &[u8]) -> Result<CertificateInfo, Error> {
    let certificate = Certificate::from_der(data).map_err(invalid("a certificate"))?;
    let tbs = certificate.tbs_certificate();
    let (scheme, digest) = scheme(certificate.signature_algorithm().oid, "sha256");
    let validity = tbs.validity();
    Ok(CertificateInfo {
        subject: tbs.subject().to_string(),
        subject_der: tbs.subject().to_der().map_err(invalid("a subject"))?,
        issuer_der: tbs.issuer().to_der().map_err(invalid("an issuer"))?,
        validity: (seconds(validity.not_before), seconds(validity.not_after)),
        check: Check {
            key: Vec::new(),
            scheme,
            digest,
            signature: certificate.signature().raw_bytes().to_vec(),
            message: Some(tbs.to_der().map_err(invalid("a certificate"))?),
        },
        key: tbs
            .subject_public_key_info()
            .to_der()
            .map_err(invalid("a public key"))?,
    })
}

/// Returns whether the ECDSA `signature` (DER) by the P-256 or P-384 key of
/// the DER SubjectPublicKeyInfo `key` signs `prehash`, a message's digest.
///
/// # Errors
///
/// [`Error::Invalid`] when `key` is not a P-256 or P-384 key.
pub fn verify_ecdsa(key: &[u8], prehash: &[u8], signature: &[u8]) -> Result<bool, Error> {
    let info = SubjectPublicKeyInfoRef::from_der(key).map_err(invalid("a public key"))?;
    let curve = info
        .algorithm
        .parameters
        .as_ref()
        .and_then(|parameters| parameters.decode_as::<ObjectIdentifier>().ok());
    let point = info.subject_public_key.raw_bytes();
    match (info.algorithm.oid, curve) {
        (EC_PUBLIC_KEY, Some(PRIME256V1)) => {
            let key = p256::ecdsa::VerifyingKey::from_sec1_bytes(point)
                .map_err(|_| Error::Invalid("the P-256 public key is invalid".to_owned()))?;
            Ok(p256::ecdsa::Signature::from_der(signature)
                .is_ok_and(|signature| key.verify_prehash(prehash, &signature).is_ok()))
        }
        (EC_PUBLIC_KEY, Some(SECP384R1)) => {
            let key = p384::ecdsa::VerifyingKey::from_sec1_bytes(point)
                .map_err(|_| Error::Invalid("the P-384 public key is invalid".to_owned()))?;
            Ok(p384::ecdsa::Signature::from_der(signature)
                .is_ok_and(|signature| key.verify_prehash(prehash, &signature).is_ok()))
        }
        (oid, _) => Err(Error::Invalid(format!(
            "the key ({oid}) is not ECDSA on P-256 or P-384"
        ))),
    }
}
