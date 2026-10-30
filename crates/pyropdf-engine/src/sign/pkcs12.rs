//! PKCS#12 files (RFC 7292) in the form that OpenSSL 3 and Python's
//! `cryptography` write: PBES2 bags (RFC 8018) under an HMAC with SHA-2.
//!
//! The `pkcs12` crate pins a `cms` release that the engine's does not
//! match, so the few structures are here. Legacy PKCS#12 encryption
//! (RC2, 3DES, and SHA-1 MACs) is refused.

use cms::content_info::ContentInfo;
use cms::encrypted_data::EncryptedData;
use der::asn1::{ObjectIdentifier, OctetString};
use der::{Any, Decode, Encode, Sequence};
use hmac::digest::Digest;
use hmac::digest::common::BlockSizeUser;
use hmac::{KeyInit as _, Mac as _, SimpleHmac};
use pkcs8::pkcs5::EncryptionScheme;
use sha2::{Sha256, Sha384, Sha512};
use x509_cert::Certificate;
use x509_cert::spki::AlgorithmIdentifierOwned;

use super::key::{MAX_ITERATIONS, bounded};
use super::{Error, Key};

const ID_DATA: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.113549.1.7.1");
const ID_ENCRYPTED_DATA: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.113549.1.7.6");
const KEY_BAG: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.113549.1.12.10.1.1");
const SHROUDED_KEY_BAG: ObjectIdentifier =
    ObjectIdentifier::new_unwrap("1.2.840.113549.1.12.10.1.2");
const CERT_BAG: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.113549.1.12.10.1.3");
const X509_CERTIFICATE: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.113549.1.9.22.1");
const SHA256: ObjectIdentifier = ObjectIdentifier::new_unwrap("2.16.840.1.101.3.4.2.1");
const SHA384: ObjectIdentifier = ObjectIdentifier::new_unwrap("2.16.840.1.101.3.4.2.2");
const SHA512: ObjectIdentifier = ObjectIdentifier::new_unwrap("2.16.840.1.101.3.4.2.3");
/// The PKCS#12 key derivation's ID of a MAC key (RFC 7292 appendix B.3).
const MAC_KEY_ID: u8 = 3;

#[derive(Sequence)]
struct Pfx {
    version: u8,
    auth_safe: ContentInfo,
    #[asn1(optional = "true")]
    mac_data: Option<MacData>,
}

const fn one() -> u32 {
    1
}

#[derive(Sequence)]
struct MacData {
    mac: DigestInfo,
    mac_salt: OctetString,
    #[asn1(default = "one")]
    iterations: u32,
}

#[derive(Sequence)]
struct DigestInfo {
    algorithm: AlgorithmIdentifierOwned,
    digest: OctetString,
}

#[derive(Sequence)]
struct SafeBag {
    bag_id: ObjectIdentifier,
    #[asn1(context_specific = "0", tag_mode = "EXPLICIT")]
    bag_value: Any,
    // Attributes, such as friendlyName, that pyropdf does not read.
    #[asn1(optional = "true")]
    bag_attributes: Option<Any>,
}

#[derive(Sequence)]
struct CertBag {
    cert_id: ObjectIdentifier,
    #[asn1(context_specific = "0", tag_mode = "EXPLICIT")]
    cert_value: OctetString,
}

fn malformed(error: der::Error) -> Error {
    Error::invalid("the PKCS#12 file is not DER", error)
}

/// Reads the ECDSA key of a PKCS#12 file and its certificates, the key's
/// certificate first, with `password`.
///
/// # Errors
///
/// [`Error::Invalid`] for a wrong password, legacy encryption, a file
/// without exactly one key or without the key's certificate, or a key that
/// [`Key`] does not read.
pub fn parse(data: &[u8], password: &str) -> Result<(Key, Vec<Certificate>), Error> {
    let pfx = Pfx::from_der(data).map_err(malformed)?;
    if pfx.version != 3 || pfx.auth_safe.content_type != ID_DATA {
        return Err(Error::Invalid(
            "the PKCS#12 file is not a password-protected version 3 file".to_owned(),
        ));
    }
    let content = pfx
        .auth_safe
        .content
        .decode_as::<OctetString>()
        .map_err(malformed)?;
    if let Some(mac) = &pfx.mac_data {
        verify_mac(mac, password, content.as_bytes())?;
    }
    let mut keys = Vec::new();
    let mut certificates = Vec::new();
    for safe in Vec::<ContentInfo>::from_der(content.as_bytes()).map_err(malformed)? {
        let bags = if safe.content_type == ID_DATA {
            safe.content
                .decode_as::<OctetString>()
                .map_err(malformed)?
                .into_bytes()
                .into_vec()
        } else if safe.content_type == ID_ENCRYPTED_DATA {
            decrypt(
                &safe
                    .content
                    .decode_as::<EncryptedData>()
                    .map_err(malformed)?,
                password,
            )?
        } else {
            return Err(Error::Invalid(
                "the PKCS#12 file holds a safe that is neither data nor password-encrypted"
                    .to_owned(),
            ));
        };
        for bag in Vec::<SafeBag>::from_der(&bags).map_err(malformed)? {
            let value = || bag.bag_value.to_der().map_err(malformed);
            if bag.bag_id == KEY_BAG {
                keys.push(Key::from_pkcs8(&value()?)?);
            } else if bag.bag_id == SHROUDED_KEY_BAG {
                keys.push(Key::from_encrypted_pkcs8(&value()?, password.as_bytes())?);
            } else if bag.bag_id == CERT_BAG {
                let cert = bag.bag_value.decode_as::<CertBag>().map_err(malformed)?;
                if cert.cert_id == X509_CERTIFICATE {
                    certificates.push(Certificate::from_der(cert.cert_value.as_bytes()).map_err(
                        |error| Error::invalid("a PKCS#12 certificate is not X.509", error),
                    )?);
                }
            }
        }
    }
    let [key] = <[Key; 1]>::try_from(keys).map_err(|keys| {
        Error::Invalid(format!(
            "the PKCS#12 file holds {} private keys, not one",
            keys.len()
        ))
    })?;
    let signer = certificates
        .iter()
        .position(|certificate| key.matches(certificate))
        .ok_or_else(|| {
            Error::Invalid("the PKCS#12 file holds no certificate of its key".to_owned())
        })?;
    certificates.swap(0, signer);
    Ok((key, certificates))
}

fn decrypt(data: &EncryptedData, password: &str) -> Result<Vec<u8>, Error> {
    let info = &data.enc_content_info;
    let scheme = info
        .content_enc_alg
        .to_der()
        .map_err(malformed)
        .and_then(|der| EncryptionScheme::try_from(der.as_slice()).map_err(|_| legacy()))?;
    if !matches!(scheme, EncryptionScheme::Pbes2(_)) {
        return Err(legacy());
    }
    bounded(&scheme)?;
    let ciphertext = info
        .encrypted_content
        .as_ref()
        .ok_or_else(|| Error::Invalid("a PKCS#12 safe holds no content".to_owned()))?;
    scheme
        .decrypt(password, ciphertext.as_bytes())
        .map_err(|_| wrong_password())
}

fn verify_mac(mac: &MacData, password: &str, content: &[u8]) -> Result<(), Error> {
    if mac.iterations > MAX_ITERATIONS {
        return Err(Error::Invalid(format!(
            "the PKCS#12 MAC takes {} iterations, more than {MAX_ITERATIONS}",
            mac.iterations
        )));
    }
    let salt = mac.mac_salt.as_bytes();
    let expected = mac.mac.digest.as_bytes();
    let algorithm = mac.mac.algorithm.oid;
    let valid = if algorithm == SHA256 {
        hmac_matches::<Sha256>(password, salt, mac.iterations, content, expected)
    } else if algorithm == SHA384 {
        hmac_matches::<Sha384>(password, salt, mac.iterations, content, expected)
    } else if algorithm == SHA512 {
        hmac_matches::<Sha512>(password, salt, mac.iterations, content, expected)
    } else {
        return Err(legacy());
    };
    if valid { Ok(()) } else { Err(wrong_password()) }
}

fn hmac_matches<D>(
    password: &str,
    salt: &[u8],
    iterations: u32,
    content: &[u8],
    expected: &[u8],
) -> bool
where
    D: Digest + BlockSizeUser + Clone,
{
    // The password as a BMPString with its two-byte terminator (RFC 7292
    // appendix B.1).
    let mut bmp: Vec<u8> = password.encode_utf16().flat_map(u16::to_be_bytes).collect();
    bmp.extend_from_slice(&[0, 0]);
    let key = derive_key::<D>(
        &bmp,
        salt,
        iterations,
        MAC_KEY_ID,
        <D as Digest>::output_size(),
    );
    SimpleHmac::<D>::new_from_slice(&key).is_ok_and(|mut hmac| {
        hmac.update(content);
        hmac.verify_slice(expected).is_ok()
    })
}

/// The PKCS#12 key derivation (RFC 7292 appendix B.2): `size` bytes for
/// `id` from the BMPString `password` and `salt`.
fn derive_key<D>(password: &[u8], salt: &[u8], iterations: u32, id: u8, size: usize) -> Vec<u8>
where
    D: Digest + BlockSizeUser,
{
    let block = <D as BlockSizeUser>::block_size();
    let fill = |data: &[u8]| -> Vec<u8> {
        if data.is_empty() {
            return Vec::new();
        }
        let length = block * data.len().div_ceil(block);
        data.iter().copied().cycle().take(length).collect()
    };
    let diversifier = vec![id; block];
    let mut input = fill(salt);
    input.extend(fill(password));
    let mut output = Vec::with_capacity(size);
    loop {
        let mut hash = D::new()
            .chain_update(&diversifier)
            .chain_update(&input)
            .finalize()
            .to_vec();
        for _ in 1..iterations {
            hash = D::digest(&hash).to_vec();
        }
        output.extend_from_slice(&hash);
        if output.len() >= size {
            output.truncate(size);
            return output;
        }
        let addend: Vec<u8> = hash.iter().copied().cycle().take(block).collect();
        for chunk in input.chunks_exact_mut(block) {
            let mut carry = 1_u16;
            for (byte, add) in chunk.iter_mut().zip(&addend).rev() {
                let sum = u16::from(*byte) + u16::from(*add) + carry;
                *byte = sum.to_be_bytes()[1];
                carry = sum >> 8;
            }
        }
    }
}

fn legacy() -> Error {
    Error::Invalid(
        "the PKCS#12 file uses legacy encryption; pyropdf reads PBES2 with AES and a SHA-2 MAC, \
         which OpenSSL 3 writes by default"
            .to_owned(),
    )
}

fn wrong_password() -> Error {
    Error::Invalid("the PKCS#12 file does not open with this password".to_owned())
}
