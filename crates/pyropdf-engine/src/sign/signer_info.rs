//! The one SignerInfo of a PAdES signature, assembled without the `cms`
//! crate's builder (DESIGN.md section 11), and the check of a time-stamp
//! token's message imprint.

use cms::cert::{CertificateChoices, IssuerAndSerialNumber};
use cms::content_info::{CmsVersion, ContentInfo};
use cms::signed_data::{
    CertificateSet, EncapsulatedContentInfo, SignedData, SignerIdentifier, SignerInfo, SignerInfos,
};
use der::asn1::{ObjectIdentifier, OctetString, SetOfVec};
use der::{Any, Decode, Encode, Sequence};
use x509_cert::attr::{Attribute, Attributes};
use x509_cert::spki::AlgorithmIdentifierOwned;

use super::{Chain, DigestAlgorithm, Error, SignatureAlgorithm, to_der};

const ID_DATA: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.113549.1.7.1");
const ID_SIGNED_DATA: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.113549.1.7.2");
const ID_CT_TST_INFO: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.113549.1.9.16.1.4");
const CONTENT_TYPE: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.113549.1.9.3");
const MESSAGE_DIGEST: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.113549.1.9.4");
/// id-aa-signingCertificateV2 (RFC 5035), which PAdES B-B requires.
const SIGNING_CERTIFICATE_V2: ObjectIdentifier =
    ObjectIdentifier::new_unwrap("1.2.840.113549.1.9.16.2.47");
/// id-aa-timeStampToken (RFC 3161 appendix A).
const TIME_STAMP_TOKEN: ObjectIdentifier =
    ObjectIdentifier::new_unwrap("1.2.840.113549.1.9.16.2.14");
const ECDSA_WITH_SHA256: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.10045.4.3.2");
const ECDSA_WITH_SHA384: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.10045.4.3.3");
const SHA256_WITH_RSA: ObjectIdentifier = ObjectIdentifier::new_unwrap("1.2.840.113549.1.1.11");

/// ESSCertIDv2 (RFC 5035) with the default SHA-256 hash, so that only the
/// certificate's hash is written.
#[derive(Sequence)]
struct EssCertIdV2 {
    cert_hash: OctetString,
}

/// SigningCertificateV2 (RFC 5035) without policies.
#[derive(Sequence)]
struct SigningCertificateV2 {
    certs: Vec<EssCertIdV2>,
}

/// The MessageImprint of a TSTInfo (RFC 3161 section 2.4.2).
#[derive(Sequence)]
struct MessageImprint {
    hash_algorithm: AlgorithmIdentifierOwned,
    hashed_message: OctetString,
}

fn attribute(oid: ObjectIdentifier, value: &impl Encode) -> Result<Attribute, Error> {
    let value = Any::from_der(&to_der(value, "an attribute")?)
        .map_err(|error| Error::invalid("an attribute", error))?;
    let values =
        SetOfVec::try_from(vec![value]).map_err(|error| Error::invalid("an attribute", error))?;
    Ok(Attribute { oid, values })
}

fn digest_identifier(algorithm: DigestAlgorithm) -> AlgorithmIdentifierOwned {
    AlgorithmIdentifierOwned {
        oid: algorithm.oid(),
        parameters: None,
    }
}

fn signature_identifier(algorithm: SignatureAlgorithm) -> AlgorithmIdentifierOwned {
    match algorithm {
        SignatureAlgorithm::EcdsaP256 => AlgorithmIdentifierOwned {
            oid: ECDSA_WITH_SHA256,
            parameters: None,
        },
        SignatureAlgorithm::EcdsaP384 => AlgorithmIdentifierOwned {
            oid: ECDSA_WITH_SHA384,
            parameters: None,
        },
        // RFC 4055 section 5: the parameters of an RSA signature are NULL.
        SignatureAlgorithm::RsaSha256 => AlgorithmIdentifierOwned {
            oid: SHA256_WITH_RSA,
            parameters: Some(Any::null()),
        },
    }
}

fn signed_attribute_set(chain: &Chain, digest: &[u8]) -> Result<Attributes, Error> {
    let signer = to_der(chain.signer(), "the signing certificate")?;
    let hash = DigestAlgorithm::Sha256.digest(&[&signer]);
    let certificate = SigningCertificateV2 {
        certs: vec![EssCertIdV2 {
            cert_hash: OctetString::new(hash).map_err(|error| Error::invalid("a hash", error))?,
        }],
    };
    let digest = OctetString::new(digest).map_err(|error| Error::invalid("a digest", error))?;
    // PAdES B-B writes no signing-time attribute; /M of the signature
    // dictionary holds the claimed time (ETSI EN 319 142-1 section 5.2.1).
    SetOfVec::try_from(vec![
        attribute(CONTENT_TYPE, &ID_DATA)?,
        attribute(MESSAGE_DIGEST, &digest)?,
        attribute(SIGNING_CERTIFICATE_V2, &certificate)?,
    ])
    .map_err(|error| Error::invalid("the signed attributes", error))
}

/// Returns the DER of the signed attributes over `digest`, the digest of the
/// byte ranges: what the signer signs (RFC 5652 section 5.4).
pub(super) fn signed_attributes(chain: &Chain, digest: &[u8]) -> Result<Vec<u8>, Error> {
    to_der(
        &signed_attribute_set(chain, digest)?,
        "the signed attributes",
    )
}

/// Returns the DER ContentInfo of the SignedData that holds `chain`, the
/// one SignerInfo with `attributes` and `signature`, and the time-stamp
/// `token` as its unsigned attribute.
pub(super) fn signed_data(
    chain: &Chain,
    attributes: &[u8],
    signature: &[u8],
    token: Option<&[u8]>,
) -> Result<Vec<u8>, Error> {
    let algorithm = chain.algorithm();
    let signer = chain.signer().tbs_certificate();
    let unsigned_attrs = match token {
        None => None,
        Some(token) => {
            let token = Any::from_der(token)
                .map_err(|error| Error::invalid("the time-stamp token is not DER", error))?;
            Some(
                SetOfVec::try_from(vec![attribute(TIME_STAMP_TOKEN, &token)?])
                    .map_err(|error| Error::invalid("the unsigned attributes", error))?,
            )
        }
    };
    let info = SignerInfo {
        version: CmsVersion::V1,
        sid: SignerIdentifier::IssuerAndSerialNumber(IssuerAndSerialNumber {
            issuer: signer.issuer().clone(),
            serial_number: signer.serial_number().clone(),
        }),
        digest_alg: digest_identifier(algorithm.digest()),
        signed_attrs: Some(
            Attributes::from_der(attributes)
                .map_err(|error| Error::invalid("the signed attributes", error))?,
        ),
        signature_algorithm: signature_identifier(algorithm),
        signature: OctetString::new(signature)
            .map_err(|error| Error::invalid("the signature", error))?,
        unsigned_attrs,
    };
    let mut certificates = SetOfVec::new();
    for certificate in chain.certificates() {
        let choice = CertificateChoices::Certificate(certificate.clone());
        // A chain that names a certificate twice holds it once.
        if !certificates.iter().any(|known| known == &choice) {
            certificates
                .insert(choice)
                .map_err(|error| Error::invalid("the certificates", error))?;
        }
    }
    let signed = SignedData {
        version: CmsVersion::V1,
        digest_algorithms: SetOfVec::try_from(vec![digest_identifier(algorithm.digest())])
            .map_err(|error| Error::invalid("the digest algorithms", error))?,
        encap_content_info: EncapsulatedContentInfo {
            econtent_type: ID_DATA,
            econtent: None,
        },
        certificates: Some(CertificateSet(certificates)),
        crls: None,
        signer_infos: SignerInfos(
            SetOfVec::try_from(vec![info])
                .map_err(|error| Error::invalid("the signer infos", error))?,
        ),
    };
    let info = ContentInfo {
        content_type: ID_SIGNED_DATA,
        content: Any::encode_from(&signed)
            .map_err(|error| Error::invalid("the signed data", error))?,
    };
    to_der(&info, "the signature")
}

/// Checks that `token` is an RFC 3161 time-stamp token whose message imprint
/// is `imprint` under `algorithm`. Trust in the authority stays with the
/// validator.
pub(super) fn check_token(
    token: &[u8],
    algorithm: DigestAlgorithm,
    imprint: &[u8],
) -> Result<(), Error> {
    let invalid = |error: der::Error| Error::invalid("the time-stamp token is not RFC 3161", error);
    let info = ContentInfo::from_der(token).map_err(invalid)?;
    if info.content_type != ID_SIGNED_DATA {
        return Err(Error::Invalid(
            "the time-stamp token is not a CMS SignedData".to_owned(),
        ));
    }
    let signed = info.content.decode_as::<SignedData>().map_err(invalid)?;
    let content = signed.encap_content_info;
    let Some(econtent) = content
        .econtent
        .filter(|_| content.econtent_type == ID_CT_TST_INFO)
    else {
        return Err(Error::Invalid(
            "the time-stamp token holds no TSTInfo".to_owned(),
        ));
    };
    let tst_info = econtent.decode_as::<OctetString>().map_err(invalid)?;
    // TSTInfo begins with version, policy, and messageImprint.
    let fields = Vec::<Any>::from_der(tst_info.as_bytes()).map_err(invalid)?;
    let stamped = fields
        .get(2)
        .ok_or_else(|| Error::Invalid("the TSTInfo holds no message imprint".to_owned()))?
        .decode_as::<MessageImprint>()
        .map_err(invalid)?;
    if stamped.hash_algorithm.oid != algorithm.oid() || stamped.hashed_message.as_bytes() != imprint
    {
        return Err(Error::Invalid(format!(
            "the time-stamp token does not stamp the signature's {} digest",
            algorithm.name()
        )));
    }
    Ok(())
}
