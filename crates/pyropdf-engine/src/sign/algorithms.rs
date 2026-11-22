//! ISO/TS 32001:2022 digest identifiers and ISO/TS 32002:2022 curve/digest pairs.

use der::asn1::ObjectIdentifier;
use sha2::{Digest, Sha256, Sha384, Sha512};
use sha3::{Sha3_256, Sha3_384, Sha3_512, Shake256};
use x509_cert::Certificate;

use super::{EC_PUBLIC_KEY, Error, PRIME256V1, RSA_ENCRYPTION, SECP384R1};

pub(super) const ED25519: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.3.101.112");
pub(super) const ED448: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.3.101.113");
pub(super) const SHAKE256_LEN: ObjectIdentifier =
    ObjectIdentifier::new_unwrap("2.16.840.1.101.3.4.2.18");

/// The digest of a PDF signature's content and signed attributes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DigestAlgorithm {
    /// SHA-256.
    Sha256,
    /// SHA-384.
    Sha384,
    /// SHA-512.
    Sha512,
    /// SHA3-256.
    Sha3_256,
    /// SHA3-384.
    Sha3_384,
    /// SHA3-512.
    Sha3_512,
    /// SHAKE256 with a 512-bit result (RFC 8419).
    Shake256,
}

impl DigestAlgorithm {
    const ALL: [Self; 7] = [
        Self::Sha256,
        Self::Sha384,
        Self::Sha512,
        Self::Sha3_256,
        Self::Sha3_384,
        Self::Sha3_512,
        Self::Shake256,
    ];

    const fn identifiers(self) -> (&'static str, ObjectIdentifier) {
        let (name, oid) = match self {
            Self::Sha256 => ("sha256", "2.16.840.1.101.3.4.2.1"),
            Self::Sha384 => ("sha384", "2.16.840.1.101.3.4.2.2"),
            Self::Sha512 => ("sha512", "2.16.840.1.101.3.4.2.3"),
            Self::Sha3_256 => ("sha3_256", "2.16.840.1.101.3.4.2.8"),
            Self::Sha3_384 => ("sha3_384", "2.16.840.1.101.3.4.2.9"),
            Self::Sha3_512 => ("sha3_512", "2.16.840.1.101.3.4.2.10"),
            Self::Shake256 => ("shake256", "2.16.840.1.101.3.4.2.12"),
        };
        (name, ObjectIdentifier::new_unwrap(oid))
    }

    /// The algorithm name given to signer and timestamp callbacks.
    #[must_use]
    pub const fn name(self) -> &'static str {
        self.identifiers().0
    }

    pub(super) const fn oid(self) -> ObjectIdentifier {
        self.identifiers().1
    }

    pub(super) fn from_oid(oid: ObjectIdentifier) -> Option<Self> {
        if oid == SHAKE256_LEN {
            return Some(Self::Shake256);
        }
        Self::ALL
            .into_iter()
            .find(|algorithm| algorithm.oid() == oid)
    }

    pub(super) fn named(name: &str) -> Option<Self> {
        Self::ALL
            .into_iter()
            .find(|algorithm| algorithm.name() == name)
    }

    /// Whether this hash requires the ISO/TS 32001 extension.
    #[must_use]
    pub const fn needs_extension(self) -> bool {
        matches!(
            self,
            Self::Sha3_256 | Self::Sha3_384 | Self::Sha3_512 | Self::Shake256
        )
    }

    /// Returns the digest of the concatenated parts without copying the input.
    #[must_use]
    #[expect(
        clippy::disallowed_methods,
        reason = "all hash outputs are at most 64 bytes"
    )]
    pub fn digest(self, parts: &[&[u8]]) -> Vec<u8> {
        fn fixed<D: Digest>(parts: &[&[u8]]) -> Vec<u8> {
            let mut hash = D::new();
            for part in parts {
                hash.update(part);
            }
            hash.finalize().to_vec()
        }
        match self {
            Self::Sha256 => fixed::<Sha256>(parts),
            Self::Sha384 => fixed::<Sha384>(parts),
            Self::Sha512 => fixed::<Sha512>(parts),
            Self::Sha3_256 => fixed::<Sha3_256>(parts),
            Self::Sha3_384 => fixed::<Sha3_384>(parts),
            Self::Sha3_512 => fixed::<Sha3_512>(parts),
            Self::Shake256 => {
                use sha3::digest::{ExtendableOutput, Update};
                let mut hash = Shake256::default();
                for part in parts {
                    hash.update(part);
                }
                let mut bytes = vec![0; 64];
                hash.finalize_xof_into(&mut bytes);
                bytes
            }
        }
    }
}

/// Computes a digest using the algorithm name exposed to callers.
#[must_use]
pub fn digest_named(name: &str, parts: &[&[u8]]) -> Option<Vec<u8>> {
    Some(DigestAlgorithm::named(name)?.digest(parts))
}

/// Signature key types accepted by the PDF signer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SignatureAlgorithm {
    /// NIST P-256.
    EcdsaP256,
    /// NIST P-384.
    EcdsaP384,
    /// NIST P-521.
    EcdsaP521,
    /// Brainpool P256r1.
    BrainpoolP256,
    /// Brainpool P384r1.
    BrainpoolP384,
    /// Brainpool P512r1.
    BrainpoolP512,
    /// Ed25519 using PureEdDSA.
    Ed25519,
    /// Ed448 using PureEdDSA.
    Ed448,
    /// RSA PKCS#1 v1.5, with SHA-256 by default.
    RsaSha256,
}

impl SignatureAlgorithm {
    /// Identifies the public key of a signing certificate.
    pub fn of(certificate: &Certificate) -> Result<Self, Error> {
        let key = &certificate
            .tbs_certificate()
            .subject_public_key_info()
            .algorithm;
        let curve = key
            .parameters
            .as_ref()
            .and_then(|p| p.decode_as::<ObjectIdentifier>().ok());
        Self::identify(key.oid, key.parameters.is_some(), curve)
    }

    /// Identifies the signing key inside a plaintext PKCS#8 document.
    pub fn from_pkcs8(data: &[u8]) -> Result<Self, Error> {
        let info = pkcs8::PrivateKeyInfoRef::try_from(data)
            .map_err(|error| Error::invalid("the key is not PKCS#8", error))?;
        let key = info.algorithm;
        let curve = key
            .parameters
            .and_then(|p| p.decode_as::<ObjectIdentifier>().ok());
        Self::identify(key.oid, key.parameters.is_some(), curve)
    }

    fn identify(
        oid: ObjectIdentifier,
        has_parameters: bool,
        curve: Option<ObjectIdentifier>,
    ) -> Result<Self, Error> {
        match oid {
            RSA_ENCRYPTION => return Ok(Self::RsaSha256),
            ED25519 | ED448 if has_parameters => {
                return Err(Error::Invalid(
                    "an EdDSA public key must omit algorithm parameters".to_owned(),
                ));
            }
            ED25519 => return Ok(Self::Ed25519),
            ED448 => return Ok(Self::Ed448),
            EC_PUBLIC_KEY => {}
            _ => {
                return Err(Error::Invalid(format!("unsupported signing key {oid}")));
            }
        }
        let curve = curve.ok_or_else(|| {
            Error::Invalid("the ECDSA signing key must specify a named curve".to_owned())
        })?;
        match curve {
            PRIME256V1 => Ok(Self::EcdsaP256),
            SECP384R1 => Ok(Self::EcdsaP384),
            _ => match curve.to_string().as_str() {
                "1.3.132.0.35" => Ok(Self::EcdsaP521),
                "1.3.36.3.3.2.8.1.1.7" => Ok(Self::BrainpoolP256),
                "1.3.36.3.3.2.8.1.1.11" => Ok(Self::BrainpoolP384),
                "1.3.36.3.3.2.8.1.1.13" => Ok(Self::BrainpoolP512),
                _ => Err(Error::Invalid(format!("unsupported ECDSA curve {curve}"))),
            },
        }
    }

    /// Default digest for this key.
    #[must_use]
    pub const fn digest(self) -> DigestAlgorithm {
        use DigestAlgorithm as D;
        match self {
            Self::EcdsaP256 | Self::BrainpoolP256 | Self::RsaSha256 => D::Sha256,
            Self::EcdsaP384 | Self::BrainpoolP384 => D::Sha384,
            Self::EcdsaP521 | Self::BrainpoolP512 | Self::Ed25519 => D::Sha512,
            Self::Ed448 => D::Shake256,
        }
    }

    /// The curve/digest combinations of ISO/TS 32002 Tables 3 and 4.
    pub(super) const fn permits(self, digest: DigestAlgorithm) -> bool {
        use DigestAlgorithm as D;
        match self {
            Self::EcdsaP256 => matches!(digest, D::Sha256 | D::Sha3_256),
            Self::EcdsaP384 => matches!(digest, D::Sha384 | D::Sha3_384),
            Self::EcdsaP521 | Self::BrainpoolP512 => matches!(digest, D::Sha512 | D::Sha3_512),
            Self::BrainpoolP256 => !matches!(digest, D::Shake256),
            Self::BrainpoolP384 => {
                matches!(digest, D::Sha384 | D::Sha512 | D::Sha3_384 | D::Sha3_512)
            }
            Self::Ed25519 => matches!(digest, D::Sha512),
            Self::Ed448 => matches!(digest, D::Shake256),
            Self::RsaSha256 => !matches!(digest, D::Shake256),
        }
    }

    /// Whether the key needs the ISO/TS 32002 extension declaration.
    #[must_use]
    pub const fn needs_extension(self) -> bool {
        !matches!(self, Self::EcdsaP256 | Self::EcdsaP384 | Self::RsaSha256)
    }
}
