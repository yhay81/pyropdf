//! CMS AuthenticatedData restricted to the profile in ISO/TS 32004:2024 §6.

use super::syntax::{
    Algorithm, Attribute, Attributes, AuthenticatedData, ContentInfo, EncapsulatedContentInfo,
    Originator, PasswordRecipientInfo, RecipientInfo, SignedData,
};
use aes_kw::KwAes256;
use der::asn1::{ObjectIdentifier as Oid, OctetString, SetOfVec};
use der::{Any, Decode, Encode, Reader, Sequence, SliceReader};
use hkdf::Hkdf;
use hmac::{Hmac, KeyInit, Mac};
use sha2::{Digest, Sha256, Sha384, Sha512};
use sha3::{Sha3_256, Sha3_384, Sha3_512};

use crate::budget::{Budget, LimitCode};
use crate::{Error, Result};

const INFO: Oid = Oid::new_unwrap("1.0.32004.1.0");
const KDF: Oid = Oid::new_unwrap("1.0.32004.1.1");
const ATTACHED: Oid = Oid::new_unwrap("1.0.32004.1.2");
const AUTH: Oid = Oid::new_unwrap("1.2.840.113549.1.9.16.1.2");
const SIGNED: Oid = Oid::new_unwrap("1.2.840.113549.1.7.2");
const HMAC: Oid = Oid::new_unwrap("1.2.840.113549.2.9");
const WRAP: Oid = Oid::new_unwrap("2.16.840.1.101.3.4.1.45");
const SHA256: Oid = Oid::new_unwrap("2.16.840.1.101.3.4.2.1");
const SHA384: Oid = Oid::new_unwrap("2.16.840.1.101.3.4.2.2");
const SHA512: Oid = Oid::new_unwrap("2.16.840.1.101.3.4.2.3");
const SHA3_256: Oid = Oid::new_unwrap("2.16.840.1.101.3.4.2.8");
const SHA3_384: Oid = Oid::new_unwrap("2.16.840.1.101.3.4.2.9");
const SHA3_512: Oid = Oid::new_unwrap("2.16.840.1.101.3.4.2.10");
const CONTENT_TYPE: Oid = Oid::new_unwrap("1.2.840.113549.1.9.3");
const MESSAGE_DIGEST: Oid = Oid::new_unwrap("1.2.840.113549.1.9.4");
const PROTECTION: Oid = Oid::new_unwrap("1.2.840.113549.1.9.52");

#[derive(Sequence)]
struct Info {
    version: u8,
    data_digest: OctetString,
    #[asn1(context_specific = "0", tag_mode = "IMPLICIT", optional = "true")]
    signature_digest: Option<OctetString>,
}

#[derive(Sequence)]
struct AlgorithmProtection {
    digest_algorithm: Algorithm,
    #[asn1(context_specific = "2", tag_mode = "IMPLICIT", constructed = "true")]
    mac_algorithm: Algorithm,
}

fn invalid(_: der::Error) -> Error {
    Error::malformed("invalid DER structure in PDF MAC")
}

fn check(ok: bool, reason: &'static str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(Error::malformed(reason))
    }
}

fn algorithm(oid: Oid) -> Algorithm {
    Algorithm {
        oid,
        parameters: None,
    }
}

fn mac_kek(file_key: &[u8], salt: &[u8; 32]) -> KwAes256 {
    let mut key = [0u8; 32];
    Hkdf::<Sha256>::new(Some(salt), file_key)
        .expand(b"PDFMAC", &mut key)
        .expect("one SHA-256 block");
    KwAes256::new(&key.into())
}

fn octets(data: &[u8]) -> Result<OctetString> {
    OctetString::new(data).map_err(invalid)
}

fn single(attrs: &Attributes, oid: Oid) -> Result<Option<&Any>> {
    let mut matching = attrs.iter().filter(|a| a.oid == oid);
    let Some(first) = matching.next() else {
        return Ok(None);
    };
    check(
        matching.next().is_none() && first.values.len() == 1,
        "duplicate or multivalued PDF MAC attribute",
    )?;
    Ok(first.values.get(0))
}

fn attribute(oid: Oid, value: Any) -> Result<Attribute> {
    Ok(Attribute {
        oid,
        values: SetOfVec::try_from(vec![value]).map_err(invalid)?,
    })
}

/// Builds a fixed-size standalone token over the supplied SHA-256 digest.
/// The save draws a new MAC key once per revision, independently of its file key.
pub(super) fn encode(
    file_key: &[u8],
    salt: &[u8; 32],
    mac_key: &[u8; 32],
    digest: &[u8; 32],
    signature: Option<&[u8]>,
) -> Result<Vec<u8>> {
    let message = Info {
        version: 0,
        data_digest: octets(digest)?,
        signature_digest: signature
            .map(|bytes| octets(&Sha256::digest(bytes)))
            .transpose()?,
    }
    .to_der()
    .map_err(invalid)?;
    let digest_alg = algorithm(SHA256);
    let mac_alg = algorithm(HMAC);
    let protection = AlgorithmProtection {
        digest_algorithm: digest_alg.clone(),
        mac_algorithm: mac_alg.clone(),
    };
    let attributes = SetOfVec::try_from(vec![
        attribute(CONTENT_TYPE, Any::encode_from(&INFO).map_err(invalid)?)?,
        attribute(
            MESSAGE_DIGEST,
            Any::encode_from(&octets(&Sha256::digest(&message))?).map_err(invalid)?,
        )?,
        attribute(PROTECTION, Any::encode_from(&protection).map_err(invalid)?)?,
    ])
    .map_err(invalid)?;
    let mut mac = Hmac::<Sha256>::new_from_slice(mac_key).expect("HMAC accepts a 32-byte key");
    mac.update(&attributes.to_der().map_err(invalid)?);
    let mut wrapped = [0u8; 40];
    let wrapped = mac_kek(file_key, salt)
        .wrap_key(mac_key, &mut wrapped)
        .expect("RFC 3394 fixed-size key");
    let recipient = RecipientInfo::Pwri(PasswordRecipientInfo {
        version: 0,
        key_derivation_alg: Some(algorithm(KDF)),
        key_enc_alg: algorithm(WRAP),
        enc_key: octets(wrapped)?,
    });
    let auth = AuthenticatedData {
        version: 0,
        originator_info: None,
        recip_infos: SetOfVec::try_from(vec![recipient]).map_err(invalid)?,
        mac_alg,
        digest_alg: Some(digest_alg),
        encap_content_info: EncapsulatedContentInfo {
            econtent_type: INFO,
            econtent: Some(Any::encode_from(&octets(&message)?).map_err(invalid)?),
        },
        auth_attrs: Some(attributes),
        mac: octets(&mac.finalize().into_bytes())?,
        unauth_attrs: None,
    };
    ContentInfo {
        content_type: AUTH,
        content: Any::encode_from(&auth).map_err(invalid)?,
    }
    .to_der()
    .map_err(invalid)
}

#[expect(
    clippy::disallowed_methods,
    reason = "the published digest sizes are 32, 48 and 64 bytes"
)]
fn hash<D: Digest>(slices: &[&[u8]], budget: &Budget) -> Result<Vec<u8>> {
    let mut state = D::new();
    for part in slices {
        for chunk in part.chunks(64 * 1024) {
            budget.poll()?;
            Digest::update(&mut state, chunk);
        }
    }
    Ok(state.finalize().to_vec())
}

fn digest(oid: Oid, slices: &[&[u8]], budget: &Budget) -> Result<Vec<u8>> {
    match oid {
        SHA256 => hash::<Sha256>(slices, budget),
        SHA384 => hash::<Sha384>(slices, budget),
        SHA512 => hash::<Sha512>(slices, budget),
        SHA3_256 => hash::<Sha3_256>(slices, budget),
        SHA3_384 => hash::<Sha3_384>(slices, budget),
        SHA3_512 => hash::<Sha3_512>(slices, budget),
        _ => Err(Error::malformed(
            "unsupported digest in ISO/TS 32004:2024 PDF MAC",
        )),
    }
}

/// DER objects owned by the CMS decoder have bounded depth. Reserve space
/// for the decoded values and their re-encoding before entering the decoder.
fn reserve_der(data: &[u8], budget: &Budget) -> Result<()> {
    budget.charge(
        LimitCode::MemorySize,
        (data.len() as u64).saturating_mul(32),
    )?;
    budget.poll()?;
    Ok(())
}

pub(super) fn standalone(data: &[u8], budget: &Budget) -> Result<ContentInfo> {
    reserve_der(data, budget)?;
    ContentInfo::from_der(data).map_err(invalid)
}

fn signed_container(data: &[u8], budget: &Budget) -> Result<SignedData> {
    reserve_der(data, budget)?;
    let mut reader = SliceReader::new(data).map_err(invalid)?;
    let container = ContentInfo::decode(&mut reader).map_err(invalid)?;
    check(
        data[usize::try_from(reader.position()).map_err(invalid)?..]
            .iter()
            .all(|b| *b == 0),
        "invalid signature container padding",
    )?;
    check(
        container.content_type == SIGNED,
        "expected CMS SignedData for an attached PDF MAC",
    )?;
    let signed: SignedData = container.content.decode_as().map_err(invalid)?;
    check(
        signed.signer_infos.len() == 1,
        "an attached PDF MAC requires exactly one signer",
    )?;
    Ok(signed)
}

pub(super) fn attach(
    data: &[u8],
    writer: &super::Writer,
    digest: &[u8; 32],
    budget: &Budget,
) -> Result<Vec<u8>> {
    let mut signed = signed_container(data, budget)?;
    let mut signer = signed.signer_infos.into_vec().pop().expect("one signer");
    let token = encode(
        &writer.file_key,
        &writer.salt,
        &writer.mac_key,
        digest,
        Some(signer.signature.as_bytes()),
    )?;
    let attrs = signer.unsigned_attrs.get_or_insert_with(Attributes::new);
    check(
        single(attrs, ATTACHED)?.is_none(),
        "a new signature already contains a PDF MAC",
    )?;
    attrs
        .insert(attribute(
            ATTACHED,
            Any::from_der(&token).map_err(invalid)?,
        )?)
        .map_err(invalid)?;
    signed.signer_infos = SetOfVec::try_from(vec![signer]).map_err(invalid)?;
    ContentInfo {
        content_type: SIGNED,
        content: Any::encode_from(&signed).map_err(invalid)?,
    }
    .to_der()
    .map_err(invalid)
}

pub(super) fn attached(data: &[u8], budget: &Budget) -> Result<(ContentInfo, OctetString)> {
    let signed = signed_container(data, budget)?;
    let signer = signed.signer_infos.get(0).expect("one signer");
    let value = signer
        .unsigned_attrs
        .as_ref()
        .map(|attrs| single(attrs, ATTACHED))
        .transpose()?
        .flatten()
        .ok_or_else(|| Error::malformed("missing attached PDF MAC attribute"))?;
    Ok((
        value.decode_as().map_err(invalid)?,
        signer.signature.clone(),
    ))
}

pub(super) fn verify(
    token: ContentInfo,
    file_key: &[u8],
    salt: &[u8; 32],
    covered: &[&[u8]],
    signature: Option<&[u8]>,
    budget: &Budget,
) -> Result<()> {
    check(
        token.content_type == AUTH,
        "expected CMS AuthenticatedData in PDF MAC",
    )?;
    let auth: AuthenticatedData = token.content.decode_as().map_err(invalid)?;
    let expected_version = auth.originator_info.as_ref().map_or(0, Originator::version);
    check(
        auth.version == expected_version && auth.unauth_attrs.is_none(),
        "incorrect CMS version or unauthenticated PDF MAC attributes",
    )?;
    let (Some(digest_alg), Some(attrs)) = (&auth.digest_alg, &auth.auth_attrs) else {
        return Err(Error::malformed(
            "missing PDF MAC digest algorithm or authenticated attributes",
        ));
    };
    check(
        auth.mac_alg.oid == HMAC
            && auth.mac_alg.parameters.as_ref().is_none_or(Any::is_null)
            && digest_alg.parameters.as_ref().is_none_or(Any::is_null)
            && auth.encap_content_info.econtent_type == INFO,
        "unsupported MAC algorithm or encapsulated PDF MAC content type",
    )?;
    let [RecipientInfo::Pwri(recipient)] = auth.recip_infos.as_slice() else {
        return Err(Error::malformed("PDF MAC requires one password recipient"));
    };
    check(
        recipient.version == 0
            && recipient.key_derivation_alg == Some(algorithm(KDF))
            && recipient.key_enc_alg == algorithm(WRAP)
            && recipient.enc_key.as_bytes().len() == 40,
        "invalid PDF MAC wrapped key or derivation algorithm",
    )?;
    let mut key = [0u8; 32];
    let key = mac_kek(file_key, salt)
        .unwrap_key(recipient.enc_key.as_bytes(), &mut key)
        .map_err(|_| Error::malformed("failed to unwrap the PDF MAC key"))?;
    let mut mac = Hmac::<Sha256>::new_from_slice(key).expect("HMAC accepts a 32-byte key");
    mac.update(&attrs.to_der().map_err(invalid)?);
    mac.verify_slice(auth.mac.as_bytes())
        .map_err(|_| Error::malformed("PDF MAC authentication failed"))?;
    let required = |oid| {
        single(attrs, oid)?
            .ok_or_else(|| Error::malformed("missing required authenticated PDF MAC attribute"))
    };
    check(
        required(CONTENT_TYPE)?
            .decode_as::<Oid>()
            .map_err(invalid)?
            == INFO,
        "incorrect authenticated PDF MAC content type",
    )?;
    if let Some(value) = single(attrs, PROTECTION)? {
        let protection: AlgorithmProtection = value.decode_as().map_err(invalid)?;
        check(
            protection.digest_algorithm == *digest_alg && protection.mac_algorithm == auth.mac_alg,
            "inconsistent authenticated PDF MAC algorithms",
        )?;
    }
    let content: OctetString = auth
        .encap_content_info
        .econtent
        .ok_or_else(|| Error::malformed("missing PDF MAC integrity info"))?
        .decode_as()
        .map_err(invalid)?;
    let claimed: OctetString = required(MESSAGE_DIGEST)?.decode_as().map_err(invalid)?;
    check(
        claimed.as_bytes() == digest(digest_alg.oid, &[content.as_bytes()], budget)?,
        "incorrect PDF MAC message digest",
    )?;
    let info = Info::from_der(content.as_bytes()).map_err(invalid)?;
    check(
        info.version == 0
            && info.data_digest.as_bytes() == digest(digest_alg.oid, covered, budget)?,
        "document digest does not match PDF MAC",
    )?;
    let expected = signature
        .map(|signature| digest(digest_alg.oid, &[signature], budget))
        .transpose()?;
    check(
        info.signature_digest.as_ref().map(OctetString::as_bytes) == expected.as_deref(),
        "signature digest does not match PDF MAC",
    )
}
