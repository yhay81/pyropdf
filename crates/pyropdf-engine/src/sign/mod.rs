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

mod algorithms;
mod key;
mod pkcs12;
mod signer_info;
#[cfg(test)]
mod tests;
pub mod verify;

use std::fmt;
use std::ops::Range;

use der::asn1::ObjectIdentifier;
use der::{Decode, Encode};
use x509_cert::Certificate;

pub use der;

pub use self::algorithms::{DigestAlgorithm, SignatureAlgorithm, digest_named};
pub use self::key::{Key, pem_key_material};
pub use self::pkcs12::{
    material as pkcs12_material, native_key as native_pkcs12_key, parse as parse_pkcs12,
};

/// The `/ByteRange` that the signature dictionary holds until [`sign`] fills
/// it, wide enough for the offsets of any file under 10 GB. The writer writes
/// it, followed by `/Contents`, as [`placeholder`] spells them.
pub const BYTE_RANGE_PLACEHOLDER: [i64; 4] = [0, 9_999_999_999, 9_999_999_999, 9_999_999_999];

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

/// A certificate chain whose first certificate is the signer's.
#[derive(Debug, Clone)]
pub struct Chain {
    certificates: Vec<Certificate>,
    algorithm: SignatureAlgorithm,
    digest: DigestAlgorithm,
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
            digest: algorithm.digest(),
        })
    }

    /// Returns the chain of DER certificates, the signer's first.
    ///
    /// # Errors
    ///
    /// As [`Chain::new`], and for a certificate that is not DER.
    pub fn from_der(certificates: &[&[u8]]) -> Result<Self, Error> {
        #[expect(
            clippy::disallowed_methods,
            reason = "one certificate per DER certificate that the caller holds"
        )]
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

    /// Selects the digest used in both CMS attributes and the signature.
    pub fn with_digest(mut self, name: Option<&str>) -> Result<Self, Error> {
        if let Some(name) = name {
            self.digest = DigestAlgorithm::named(name)
                .filter(|digest| self.algorithm.permits(*digest))
                .ok_or_else(|| {
                    Error::Invalid(format!(
                        "{name} is not a permitted PDF signature digest for {:?}",
                        self.algorithm
                    ))
                })?;
        }
        Ok(self)
    }

    /// The digest used for the signed content and attributes.
    #[must_use]
    pub const fn digest(&self) -> DigestAlgorithm {
        self.digest
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

/// The bytes that the writer writes for the placeholder entries of a
/// signature dictionary, up to the `0`s of `/Contents`, matching
/// [`BYTE_RANGE_PLACEHOLDER`].
const PLACEHOLDER_PREFIX: &[u8] = b"/ByteRange [0 9999999999 9999999999 9999999999] /Contents (";

/// Zero-allocation classification for [`Placeholder::find_with_prefix`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PlaceholderError {
    Missing,
    Multiple,
}

impl PlaceholderError {
    fn into_error(self) -> Error {
        let message = match self {
            Self::Missing => "the saved document holds no signature placeholder",
            Self::Multiple => "the saved document holds more than one signature placeholder",
        };
        Error::Placeholder(message.to_owned())
    }
}

impl Placeholder {
    /// Finds the one placeholder of `data`, a `/Contents` literal of
    /// `2 * reserve` `0`s that follows [`BYTE_RANGE_PLACEHOLDER`].
    ///
    /// # Errors
    ///
    /// [`Error::Placeholder`] when `data` holds none, or more than one.
    pub fn find(data: &[u8], reserve: usize) -> Result<Self, Error> {
        Self::find_with_prefix(
            data,
            PLACEHOLDER_PREFIX,
            "/ByteRange ".len(),
            " /Contents ".len(),
            reserve,
        )
        .map_err(PlaceholderError::into_error)
    }

    fn find_with_prefix(
        data: &[u8],
        prefix: &[u8],
        byte_range_offset: usize,
        contents_label_len: usize,
        reserve: usize,
    ) -> Result<Self, PlaceholderError> {
        let digits = reserve.checked_mul(2).ok_or(PlaceholderError::Missing)?;
        let mut found = None;
        let mut position = 0;
        while let Some(offset) = find_bytes(&data[position..], prefix) {
            let start = position + offset;
            let open = start + prefix.len() - 1;
            let close = open.checked_add(1).and_then(|n| n.checked_add(digits));
            let zeros = close.and_then(|close| data.get(open + 1..close));
            if let Some(close) = close
                && zeros.is_some_and(|zeros| zeros.iter().all(|&byte| byte == b'0'))
                && data.get(close) == Some(&b')')
            {
                if found.is_some() {
                    return Err(PlaceholderError::Multiple);
                }
                let byte_range = start + byte_range_offset..open - contents_label_len;
                found = Some(Self {
                    byte_range,
                    contents: open..close + 1,
                });
            }
            position = start + 1;
        }
        found.ok_or(PlaceholderError::Missing)
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
    let limit = haystack.len().checked_sub(needle.len())?;
    let first = *needle.first()?;
    let mut i = 0;
    while i <= limit {
        if haystack[i] == first {
            let mut j = 1;
            while j < needle.len() && haystack[i + j] == needle[j] {
                j += 1;
            }
            if j == needle.len() {
                return Some(i);
            }
        }
        i += 1;
    }
    None
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
    if !write_padded_byte_range(field, written.as_bytes()) {
        return Err(Error::Placeholder("the document is too large to sign".to_owned()).into());
    }

    let digest_algorithm = chain.digest();
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

/// Writes `[a b c d` and trailing ASCII spaces into `field` while leaving the
/// closing `]` at `field[field.len() - 1]` untouched so no PDF byte offset moves.
fn write_padded_byte_range(field: &mut [u8], written: &[u8]) -> bool {
    let Some(padding) = field
        .len()
        .checked_sub(1)
        .and_then(|limit| limit.checked_sub(written.len()))
    else {
        return false;
    };
    field[..written.len()].copy_from_slice(written);
    field[written.len()..written.len() + padding].fill(b' ');
    true
}

const fn hex_pair(byte: u8) -> [u8; 2] {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    [DIGITS[(byte >> 4) as usize], DIGITS[(byte & 0x0f) as usize]]
}

/// Returns the DER of `value`, or [`Error::Invalid`] naming `what`.
pub(crate) fn to_der(value: &impl Encode, what: &str) -> Result<Vec<u8>, Error> {
    value.to_der().map_err(|error| Error::invalid(what, error))
}

#[cfg(kani)]
mod kani_proofs {
    use super::*;

    /// Verifies `hex_pair` encodes every `u8` as two lowercase ASCII hex digits
    /// that decode back to the exact input byte.
    #[kani::proof]
    fn kani_sign_hex_pair_roundtrip() {
        let byte: u8 = kani::any();
        let [hi, lo] = hex_pair(byte);
        let decode = |c: u8| -> u8 {
            match c {
                b'0'..=b'9' => c - b'0',
                b'a'..=b'f' => c - b'a' + 10,
                _ => panic!("non-hex digit produced"),
            }
        };
        assert_eq!((decode(hi) << 4) | decode(lo), byte);
    }

    /// Verifies in-place `/ByteRange` padding never panics, preserves the
    /// placeholder slice length and closing `]`, and pads strictly with spaces.
    #[kani::proof]
    #[kani::unwind(18)]
    fn kani_sign_padded_byte_range_preserves_length() {
        let field_len: usize = kani::any();
        let written_len: usize = kani::any();
        kani::assume(field_len <= 16 && written_len <= 16);
        let mut field = [b']'; 16];
        let written: [u8; 16] = kani::any();
        let field_slice = &mut field[..field_len];
        let written_slice = &written[..written_len];

        let ok = write_padded_byte_range(field_slice, written_slice);
        assert_eq!(ok, field_len >= 1 && written_len <= field_len - 1);
        if ok {
            assert_eq!(field_slice[field_len - 1], b']');
            assert_eq!(&field_slice[..written_len], written_slice);
            for &b in &field_slice[written_len..field_len - 1] {
                assert_eq!(b, b' ');
            }
        }
    }

    /// Verifies `Placeholder::find_with_prefix` handles arbitrary `reserve: usize`
    /// (including `usize::MAX / 2` and `usize::MAX` that would overflow
    /// `2 * reserve` or `open + 1 + digits`) without panicking.
    #[kani::proof]
    #[kani::unwind(9)]
    fn kani_sign_placeholder_reserve_overflow_no_panic() {
        let reserve: usize = kani::any();
        kani::assume(reserve > 1);

        // Short buffer with the production prefix.
        let small = [0_u8; 4];
        assert_eq!(
            Placeholder::find_with_prefix(
                &small,
                PLACEHOLDER_PREFIX,
                "/ByteRange ".len(),
                " /Contents ".len(),
                reserve,
            ),
            Err(PlaceholderError::Missing)
        );

        // Matched prefix exercises both `reserve.checked_mul(2)` and
        // `open.checked_add(1).and_then(|n| n.checked_add(digits))`.
        const SHORT: &[u8] = b"B[](";
        let buf = *b"B[](00)";
        assert_eq!(
            Placeholder::find_with_prefix(&buf, SHORT, 1, 0, reserve),
            Err(PlaceholderError::Missing)
        );
    }

    /// Verifies `Placeholder::find_with_prefix` locates a unique placeholder,
    /// keeps `/ByteRange` strictly before `/Contents`, and produces a
    /// `/ByteRange` that partitions the entire document.
    #[kani::proof]
    #[kani::unwind(10)]
    fn kani_sign_placeholder_layout_and_partition() {
        const SHORT_PREFIX: &[u8] = b"B[](";
        let pre: usize = kani::any();
        let reserve: usize = kani::any();
        let post: usize = kani::any();
        kani::assume(pre <= 1 && reserve <= 1 && post <= 1);

        let digits = reserve * 2;
        let total = pre + SHORT_PREFIX.len() + digits + 1 + post;
        let mut buf = [b' '; 9];
        let mut at = pre;
        buf[at..at + SHORT_PREFIX.len()].copy_from_slice(SHORT_PREFIX);
        at += SHORT_PREFIX.len();
        buf[at..at + digits].fill(b'0');
        at += digits;
        buf[at] = b')';

        let data = &buf[..total];
        let found = Placeholder::find_with_prefix(data, SHORT_PREFIX, 1, 0, reserve)
            .expect("valid single placeholder");
        assert!(found.byte_range.start < found.byte_range.end);
        assert!(found.byte_range.end <= found.contents.start);
        assert!(found.contents.start < found.contents.end);
        assert!(found.contents.end <= data.len());
        assert_eq!(found.contents.len(), digits + 2);
        assert_eq!(data[found.byte_range.start], b'[');
        assert_eq!(data[found.byte_range.end - 1], b']');
        assert_eq!(data[found.contents.start], b'(');
        assert_eq!(data[found.contents.end - 1], b')');

        let ranges = found.byte_range(data.len());
        assert_eq!(ranges[0], 0);
        assert_eq!(ranges[1], found.contents.start);
        assert_eq!(ranges[2], found.contents.end);
        assert_eq!(ranges[2] + ranges[3], data.len());
        assert_eq!(ranges[1] + found.contents.len() + ranges[3], data.len());
    }

    /// Verifies duplicate placeholders are rejected rather than silently
    /// signing only one of them.
    #[kani::proof]
    #[kani::unwind(11)]
    fn kani_sign_placeholder_rejects_duplicate() {
        let sep: u8 = kani::any();
        kani::assume(sep != b'B');
        let buf = [
            b'B', b'[', b']', b'(', b')', sep, b'B', b'[', b']', b'(', b')',
        ];
        assert_eq!(
            Placeholder::find_with_prefix(&buf, b"B[](", 1, 0, 0),
            Err(PlaceholderError::Multiple)
        );
    }
}
