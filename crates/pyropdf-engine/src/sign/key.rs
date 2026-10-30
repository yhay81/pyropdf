//! The ECDSA keys that pyropdf signs with itself: P-256 and P-384, read from
//! PKCS#8, encrypted PKCS#8, or SEC1. ECDSA signs deterministically
//! (RFC 6979), so it needs no random numbers.

use der::pem;
use p256::ecdsa::signature::Signer as _;
use p256::pkcs8::DecodePrivateKey as _;
use pkcs8::EncryptedPrivateKeyInfoRef;
use pkcs8::pkcs5::EncryptionScheme;
use pkcs8::pkcs5::pbes2::Kdf;
use x509_cert::Certificate;

use super::{Error, SignatureAlgorithm};

/// The most iterations of a key derivation from a password: of PBKDF2, and
/// of a PKCS#12 MAC. OpenSSL writes 2048, and OWASP asks 600,000 for
/// PBKDF2 with SHA-256.
pub(super) const MAX_ITERATIONS: u32 = 1_000_000;
/// The most memory of a scrypt key derivation, 128 r N bytes: 64 MiB, four
/// times what the common N = 2^14 and r = 8 take.
const MAX_SCRYPT_MEMORY: u64 = 64 << 20;
/// The most work of a scrypt key derivation, 128 r N p bytes mixed.
const MAX_SCRYPT_WORK: u64 = 256 << 20;

/// Refuses an encryption other than PBES2, and a key derivation that would
/// take more iterations or memory than pyropdf's bounds, before it runs.
pub(super) fn bounded(scheme: &EncryptionScheme) -> Result<(), Error> {
    let EncryptionScheme::Pbes2(parameters) = scheme else {
        return Err(Error::Invalid(
            "the key's encryption is not PBES2, which pyropdf reads".to_owned(),
        ));
    };
    match &parameters.kdf {
        Kdf::Pbkdf2(pbkdf2) if pbkdf2.iteration_count > MAX_ITERATIONS => {
            Err(Error::Invalid(format!(
                "the key's PBKDF2 takes {} iterations, more than {MAX_ITERATIONS}",
                pbkdf2.iteration_count
            )))
        }
        Kdf::Pbkdf2(_) => Ok(()),
        Kdf::Scrypt(scrypt) => {
            let memory = 128_u64
                .saturating_mul(u64::from(scrypt.block_size))
                .saturating_mul(scrypt.cost_parameter);
            let work = memory.saturating_mul(u64::from(scrypt.parallelization));
            if memory > MAX_SCRYPT_MEMORY || work > MAX_SCRYPT_WORK {
                return Err(Error::Invalid(format!(
                    "the key's scrypt takes {memory} bytes and {work} bytes of work, \
                     more than {MAX_SCRYPT_MEMORY} and {MAX_SCRYPT_WORK}"
                )));
            }
            Ok(())
        }
        _ => Err(Error::Invalid(
            "the key's derivation is neither PBKDF2 nor scrypt".to_owned(),
        )),
    }
}

/// An ECDSA private key.
#[derive(Clone)]
pub enum Key {
    /// A P-256 key, which signs with SHA-256.
    P256(p256::ecdsa::SigningKey),
    /// A P-384 key, which signs with SHA-384.
    P384(p384::ecdsa::SigningKey),
}

impl std::fmt::Debug for Key {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // The key's value never reaches a message.
        formatter.write_str(match self {
            Self::P256(_) => "Key::P256",
            Self::P384(_) => "Key::P384",
        })
    }
}

impl Key {
    /// Reads a PEM private key: `PRIVATE KEY` (PKCS#8), `ENCRYPTED PRIVATE
    /// KEY` with `password`, or `EC PRIVATE KEY` (SEC1).
    ///
    /// # Errors
    ///
    /// [`Error::Invalid`] for another label, a missing or wrong password, or
    /// a key on a curve other than P-256 and P-384.
    pub fn from_pem(pem: &[u8], password: Option<&[u8]>) -> Result<Self, Error> {
        let (label, der) =
            pem::decode_vec(pem).map_err(|error| Error::invalid("the key is not PEM", error))?;
        match label {
            "PRIVATE KEY" => Self::from_pkcs8(&der),
            "ENCRYPTED PRIVATE KEY" => {
                let password = password.ok_or_else(|| {
                    Error::Invalid("the key is encrypted; pass its password".to_owned())
                })?;
                Self::from_encrypted_pkcs8(&der, password)
            }
            "EC PRIVATE KEY" => p256::SecretKey::from_sec1_der(&der)
                .map(|key| Self::P256(key.into()))
                .or_else(|_| p384::SecretKey::from_sec1_der(&der).map(|key| Self::P384(key.into())))
                .map_err(|_| unsupported_key()),
            "RSA PRIVATE KEY" => Err(rsa_key()),
            other => Err(Error::Invalid(format!(
                "a PEM {other:?} is not a private key; pyropdf reads PRIVATE KEY, \
                 ENCRYPTED PRIVATE KEY, and EC PRIVATE KEY"
            ))),
        }
    }

    /// Reads a DER PKCS#8 PrivateKeyInfo.
    pub(super) fn from_pkcs8(der: &[u8]) -> Result<Self, Error> {
        if let Ok(key) = p256::ecdsa::SigningKey::from_pkcs8_der(der) {
            return Ok(Self::P256(key));
        }
        if let Ok(key) = p384::ecdsa::SigningKey::from_pkcs8_der(der) {
            return Ok(Self::P384(key));
        }
        let info = pkcs8::PrivateKeyInfoRef::try_from(der)
            .map_err(|error| Error::invalid("the key is not PKCS#8", error))?;
        if info.algorithm.oid == super::RSA_ENCRYPTION {
            return Err(rsa_key());
        }
        Err(unsupported_key())
    }

    /// Reads a DER PKCS#8 EncryptedPrivateKeyInfo with `password`.
    pub(super) fn from_encrypted_pkcs8(der: &[u8], password: &[u8]) -> Result<Self, Error> {
        let info = EncryptedPrivateKeyInfoRef::try_from(der)
            .map_err(|error| Error::invalid("the key is not encrypted PKCS#8", error))?;
        bounded(&info.encryption_algorithm)?;
        let document = info
            .decrypt(password)
            .map_err(|_| Error::Invalid("the key does not decrypt: a wrong password".to_owned()))?;
        Self::from_pkcs8(document.as_bytes())
    }

    /// Returns the signature algorithm of the key.
    #[must_use]
    pub const fn algorithm(&self) -> SignatureAlgorithm {
        match self {
            Self::P256(_) => SignatureAlgorithm::EcdsaP256,
            Self::P384(_) => SignatureAlgorithm::EcdsaP384,
        }
    }

    /// Returns whether `certificate` holds this key's public key.
    #[must_use]
    pub fn matches(&self, certificate: &Certificate) -> bool {
        let info = certificate.tbs_certificate().subject_public_key_info();
        let Some(point) = info.subject_public_key.as_bytes() else {
            return false;
        };
        // The certificate may hold the point compressed or uncompressed.
        SignatureAlgorithm::of(certificate).ok() == Some(self.algorithm())
            && match self {
                Self::P256(key) => p256::ecdsa::VerifyingKey::from_sec1_bytes(point)
                    .is_ok_and(|public| &public == key.verifying_key()),
                Self::P384(key) => p384::ecdsa::VerifyingKey::from_sec1_bytes(point)
                    .is_ok_and(|public| &public == key.verifying_key()),
            }
    }

    /// Returns the DER ECDSA signature of `data`, hashed with the key's
    /// digest algorithm.
    #[must_use]
    pub fn sign(&self, data: &[u8]) -> Vec<u8> {
        match self {
            Self::P256(key) => {
                let signature: p256::ecdsa::DerSignature = key.sign(data);
                signature.as_bytes().to_vec()
            }
            Self::P384(key) => {
                let signature: p384::ecdsa::DerSignature = key.sign(data);
                signature.as_bytes().to_vec()
            }
        }
    }
}

fn unsupported_key() -> Error {
    Error::Invalid("the key is not an ECDSA key on P-256 or P-384".to_owned())
}

fn rsa_key() -> Error {
    Error::Invalid(
        "pyropdf signs with ECDSA keys only; sign with an RSA key through Signer.callback"
            .to_owned(),
    )
}
