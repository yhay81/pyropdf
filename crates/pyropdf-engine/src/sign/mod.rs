//! PAdES signatures (ETSI EN 319 142-1): the CMS signature of a document's
//! byte ranges as B-B (section 5.1), and with a signature time-stamp as B-T
//! (section 5.3). `evidence/signing` measured the approach in pyHanko.
//!
//! The caller saves the document incrementally with the signature
//! dictionary's [`BYTE_RANGE_PLACEHOLDER`] followed by a `/Contents` literal
//! of `0`s; [`sign`] fills both in place, so that no offset moves. Keys are
//! ECDSA P-256 and P-384 keys ([`Key`]), or a callback that signs outside
//! pyropdf, for RSA keys, HSMs, and PKCS#11. A time-stamp is a callback that
//! returns an RFC 3161 token: pyropdf makes no network access.

mod key;
mod pkcs12;
mod signer_info;
#[cfg(test)]
mod tests;

use std::fmt;
use std::ops::Range;

use der::asn1::ObjectIdentifier;
use der::{Decode, Encode};
use sha2::{Digest, Sha256, Sha384};
use x509_cert::Certificate;

pub use der;

pub use self::key::Key;
pub use self::pkcs12::parse as parse_pkcs12;

/// The `/ByteRange` that the signature dictionary holds until [`sign`] fills
/// it, wide enough for the offsets of any file under 10 GB. The writer writes
/// it, followed by `/Contents`, as [`placeholder`] spells them.
pub const BYTE_RANGE_PLACEHOLDER: [i64; 4] = [0, 9_999_999_999, 9_999_999_999, 9_999_999_999];

/// The bytes that the CMS signature may take by default: twice as many hex
/// digits fill the `/Contents` string.
pub const DEFAULT_RESERVE: usize = 16_384;
/// The most bytes that a caller may reserve for the signature.
pub const MAX_RESERVE: usize = 1 << 20;

const RSA_ENCRYPTION: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.113549.1.1.1");
const EC_PUBLIC_KEY: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.10045.2.1");
const PRIME256V1: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.10045.3.1.7");
const SECP384R1: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.3.132.0.34");

/// Why signing, or reading a key or a certificate, failed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// A key, a certificate, a PKCS#12 file, or a time-stamp token that
    /// cannot be used, with the reason.
    Invalid(String),
    /// The saved document does not hold exactly one placeholder.
    Placeholder(String),
    /// The CMS signature needs more bytes than the caller reserved.
    Reserve {
        /// The bytes that the signature needs.
        needed: usize,
        /// The bytes reserved for it.
        reserved: usize,
    },
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid(message) | Self::Placeholder(message) => formatter.write_str(message),
            Self::Reserve { needed, reserved } => write!(
                formatter,
                "the signature needs {needed} bytes, more than the {reserved} reserved; \
                 sign with a larger reserve"
            ),
        }
    }
}

impl std::error::Error for Error {}

impl Error {
    pub(crate) fn invalid(what: &str, error: impl fmt::Display) -> Self {
        Self::Invalid(format!("{what}: {error}"))
    }
}

/// A failure of [`sign`]: its own, or one of a callback's, passed through.
#[derive(Debug)]
pub enum Failure<E> {
    /// Signing failed.
    Sign(Error),
    /// The signer or the time-stamp callback failed.
    Callback(E),
}

impl<E> From<Error> for Failure<E> {
    fn from(error: Error) -> Self {
        Self::Sign(error)
    }
}

/// The digest algorithm of a signature, which its key's size chooses.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DigestAlgorithm {
    /// SHA-256, for P-256 and RSA keys.
    Sha256,
    /// SHA-384, for P-384 keys.
    Sha384,
}

impl DigestAlgorithm {
    /// Returns the algorithm's name, as Python's `hashlib` spells it.
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Sha256 => "sha256",
            Self::Sha384 => "sha384",
        }
    }

    const fn oid(self) -> ObjectIdentifier {
        match self {
            Self::Sha256 => ObjectIdentifier::new_unwrap("2.16.840.1.101.3.4.2.1"),
            Self::Sha384 => ObjectIdentifier::new_unwrap("2.16.840.1.101.3.4.2.2"),
        }
    }

    /// Returns the digest of the concatenated `parts`.
    #[must_use]
    pub fn digest(self, parts: &[&[u8]]) -> Vec<u8> {
        match self {
            Self::Sha256 => digest_parts::<Sha256>(parts),
            Self::Sha384 => digest_parts::<Sha384>(parts),
        }
    }
}

fn digest_parts<D: Digest>(parts: &[&[u8]]) -> Vec<u8> {
    let mut hasher = D::new();
    for part in parts {
        hasher.update(part);
    }
    hasher.finalize().to_vec()
}

/// The signature algorithm that a certificate's public key implies.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SignatureAlgorithm {
    /// ECDSA on P-256 with SHA-256.
    EcdsaP256,
    /// ECDSA on P-384 with SHA-384.
    EcdsaP384,
    /// RSA PKCS#1 v1.5 with SHA-256, which only a callback signs.
    RsaSha256,
}

impl SignatureAlgorithm {
    /// Returns the algorithm of the public key of `certificate`.
    ///
    /// # Errors
    ///
    /// [`Error::Invalid`] for a key other than RSA, P-256, or P-384.
    pub fn of(certificate: &Certificate) -> Result<Self, Error> {
        let key = certificate.tbs_certificate().subject_public_key_info();
        if key.algorithm.oid == RSA_ENCRYPTION {
            return Ok(Self::RsaSha256);
        }
        let curve = key
            .algorithm
            .parameters
            .as_ref()
            .and_then(|parameters| parameters.decode_as::<ObjectIdentifier>().ok());
        match (key.algorithm.oid, curve) {
            (EC_PUBLIC_KEY, Some(PRIME256V1)) => Ok(Self::EcdsaP256),
            (EC_PUBLIC_KEY, Some(SECP384R1)) => Ok(Self::EcdsaP384),
            (oid, _) => Err(Error::Invalid(format!(
                "the signing certificate's key ({oid}) is neither RSA nor ECDSA on P-256 or P-384"
            ))),
        }
    }

    /// Returns the digest algorithm that this algorithm signs with.
    #[must_use]
    pub const fn digest(self) -> DigestAlgorithm {
        match self {
            Self::EcdsaP256 | Self::RsaSha256 => DigestAlgorithm::Sha256,
            Self::EcdsaP384 => DigestAlgorithm::Sha384,
        }
    }
}

/// A certificate chain whose first certificate is the signer's.
#[derive(Debug, Clone)]
pub struct Chain {
    certificates: Vec<Certificate>,
    algorithm: SignatureAlgorithm,
}

impl Chain {
    /// Returns the chain of `certificates`, the signer's first.
    ///
    /// # Errors
    ///
    /// [`Error::Invalid`] for an empty chain, or a signer's key that
    /// [`SignatureAlgorithm::of`] refuses.
    pub fn new(certificates: Vec<Certificate>) -> Result<Self, Error> {
        let signer = certificates
            .first()
            .ok_or_else(|| Error::Invalid("the certificate chain is empty".to_owned()))?;
        let algorithm = SignatureAlgorithm::of(signer)?;
        Ok(Self {
            certificates,
            algorithm,
        })
    }

    /// Returns the chain of DER certificates, the signer's first.
    ///
    /// # Errors
    ///
    /// As [`Chain::new`], and for a certificate that is not DER.
    pub fn from_der(certificates: &[&[u8]]) -> Result<Self, Error> {
        let parsed = certificates
            .iter()
            .map(|der| Certificate::from_der(der))
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| Error::invalid("a certificate is not DER X.509", error))?;
        Self::new(parsed)
    }

    /// Returns the chain of the PEM certificates in `pem`, the signer's
    /// first.
    ///
    /// # Errors
    ///
    /// As [`Chain::new`], and for input that holds no PEM certificate.
    pub fn from_pem(pem: &[u8]) -> Result<Self, Error> {
        let parsed = Certificate::load_pem_chain(pem)
            .map_err(|error| Error::invalid("the certificates are not PEM X.509", error))?;
        Self::new(parsed)
    }

    /// Returns the signer's certificate.
    #[must_use]
    pub fn signer(&self) -> &Certificate {
        &self.certificates[0]
    }

    /// Returns the certificates, the signer's first.
    #[must_use]
    pub fn certificates(&self) -> &[Certificate] {
        &self.certificates
    }

    /// Returns the signature algorithm of the signer's key.
    #[must_use]
    pub const fn algorithm(&self) -> SignatureAlgorithm {
        self.algorithm
    }
}

/// The `/Contents` string of a saved document, between its delimiters
/// included, which the signature's byte ranges leave out.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Placeholder {
    byte_range: Range<usize>,
    contents: Range<usize>,
}

/// Returns the bytes that the writer writes for the placeholder entries of a
/// signature dictionary, up to the `0`s of `/Contents`.
fn placeholder_prefix() -> Vec<u8> {
    let [start, first, second, third] = BYTE_RANGE_PLACEHOLDER;
    format!("/ByteRange [{start} {first} {second} {third}] /Contents (").into_bytes()
}

impl Placeholder {
    /// Finds the one placeholder of `data`, a `/Contents` literal of
    /// `2 * reserve` `0`s that follows [`BYTE_RANGE_PLACEHOLDER`].
    ///
    /// # Errors
    ///
    /// [`Error::Placeholder`] when `data` holds none, or more than one.
    pub fn find(data: &[u8], reserve: usize) -> Result<Self, Error> {
        let prefix = placeholder_prefix();
        let digits = 2 * reserve;
        let mut found = None;
        let mut position = 0;
        while let Some(offset) = find_bytes(&data[position..], &prefix) {
            let start = position + offset;
            let open = start + prefix.len() - 1;
            let close = open + 1 + digits;
            let zeros = data.get(open + 1..close);
            if zeros.is_some_and(|zeros| zeros.iter().all(|&byte| byte == b'0'))
                && data.get(close) == Some(&b')')
            {
                if found.is_some() {
                    return Err(Error::Placeholder(
                        "the saved document holds more than one signature placeholder".to_owned(),
                    ));
                }
                let byte_range = start + "/ByteRange ".len()..open - " /Contents ".len();
                found = Some(Self {
                    byte_range,
                    contents: open..close + 1,
                });
            }
            position = start + 1;
        }
        found.ok_or_else(|| {
            Error::Placeholder("the saved document holds no signature placeholder".to_owned())
        })
    }

    /// Returns the `/ByteRange` values that sign every byte but `/Contents`.
    #[must_use]
    pub fn byte_range(&self, length: usize) -> [usize; 4] {
        [
            0,
            self.contents.start,
            self.contents.end,
            length - self.contents.end,
        ]
    }
}

fn find_bytes(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

/// Signs `data`, a saved document that holds the [`Placeholder`] of
/// `reserve` bytes, in place.
///
/// It fills `/ByteRange`, signs the digest of the byte ranges with the
/// signed attributes of PAdES B-B, with `signer` over their DER, and writes
/// the CMS signature into `/Contents`. With `timestamp`, which receives the
/// digest of the signature value and returns an RFC 3161 time-stamp token,
/// the token becomes the signature time-stamp of B-T.
///
/// # Errors
///
/// [`Failure::Sign`] for a missing placeholder, a token that does not stamp
/// the signature, or a signature larger than `reserve`;
/// [`Failure::Callback`] passes a callback's error through.
pub fn sign<E>(
    data: &mut [u8],
    reserve: usize,
    chain: &Chain,
    signer: impl FnOnce(&[u8]) -> Result<Vec<u8>, E>,
    timestamp: Option<impl FnOnce(&[u8], DigestAlgorithm) -> Result<Vec<u8>, E>>,
) -> Result<(), Failure<E>> {
    let placeholder = Placeholder::find(data, reserve)?;
    let ranges = placeholder.byte_range(data.len());
    let written = format!("[{} {} {} {}", ranges[0], ranges[1], ranges[2], ranges[3]);
    let field = &mut data[placeholder.byte_range.clone()];
    // The placeholder's width holds every offset of a file under 10 GB.
    let Some(padding) = (field.len() - 1).checked_sub(written.len()) else {
        return Err(Error::Placeholder("the document is too large to sign".to_owned()).into());
    };
    field[..written.len()].copy_from_slice(written.as_bytes());
    field[written.len()..written.len() + padding].fill(b' ');

    let algorithm = chain.algorithm();
    let digest_algorithm = algorithm.digest();
    let digest = digest_algorithm.digest(&[
        &data[..placeholder.contents.start],
        &data[placeholder.contents.end..],
    ]);
    let attributes = signer_info::signed_attributes(chain, &digest)?;
    let signature = signer(&attributes).map_err(Failure::Callback)?;
    let token = match timestamp {
        None => None,
        Some(timestamp) => {
            let imprint = digest_algorithm.digest(&[&signature]);
            let token = timestamp(&imprint, digest_algorithm).map_err(Failure::Callback)?;
            signer_info::check_token(&token, digest_algorithm, &imprint)?;
            Some(token)
        }
    };
    let signed = signer_info::signed_data(chain, &attributes, &signature, token.as_deref())?;
    if signed.len() > reserve {
        return Err(Error::Reserve {
            needed: signed.len(),
            reserved: reserve,
        }
        .into());
    }
    let hex = &mut data[placeholder.contents];
    hex[0] = b'<';
    let last = hex.len() - 1;
    hex[last] = b'>';
    for (pair, byte) in hex[1..last].as_chunks_mut::<2>().0.iter_mut().zip(&signed) {
        *pair = hex_pair(*byte);
    }
    Ok(())
}

const fn hex_pair(byte: u8) -> [u8; 2] {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    [DIGITS[(byte >> 4) as usize], DIGITS[(byte & 0x0f) as usize]]
}

/// Returns the DER of `value`, or [`Error::Invalid`] naming `what`.
pub(crate) fn to_der(value: &impl Encode, what: &str) -> Result<Vec<u8>, Error> {
    value.to_der().map_err(|error| Error::invalid(what, error))
}
