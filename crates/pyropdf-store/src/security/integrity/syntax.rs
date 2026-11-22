//! DER views of the RFC 5652 fields used by ISO/TS 32004:2024. The store
//! neither interprets certificates nor validates a digital signature.
//! Preserve the signer's identifiers, signed attributes, certificates,
//! revocation information and encapsulated data as encoded.

use der::asn1::{ObjectIdentifier, OctetString, SetOfVec};
use der::{Any, Choice, Sequence, Tag, TagNumber, Tagged, ValueOrd};

#[derive(Clone, Eq, PartialEq, Sequence, ValueOrd)]
pub(super) struct Algorithm {
    pub oid: ObjectIdentifier,
    pub parameters: Option<Any>,
}

#[derive(Sequence)]
pub(super) struct ContentInfo {
    pub content_type: ObjectIdentifier,
    #[asn1(context_specific = "0", tag_mode = "EXPLICIT")]
    pub content: Any,
}

#[derive(Clone, Eq, PartialEq, Sequence, ValueOrd)]
pub(super) struct Attribute {
    pub oid: ObjectIdentifier,
    pub values: SetOfVec<Any>,
}
pub(super) type Attributes = SetOfVec<Attribute>;

#[derive(Sequence)]
pub(super) struct EncapsulatedContentInfo {
    pub econtent_type: ObjectIdentifier,
    #[asn1(context_specific = "0", tag_mode = "EXPLICIT", optional = "true")]
    pub econtent: Option<Any>,
}

#[derive(Clone, Eq, PartialEq, Choice, ValueOrd)]
pub(super) enum RecipientInfo {
    #[asn1(context_specific = "3", tag_mode = "IMPLICIT", constructed = "true")]
    Pwri(PasswordRecipientInfo),
}

#[derive(Clone, Eq, PartialEq, Sequence, ValueOrd)]
pub(super) struct PasswordRecipientInfo {
    pub version: u8,
    #[asn1(
        context_specific = "0",
        tag_mode = "IMPLICIT",
        constructed = "true",
        optional = "true"
    )]
    pub key_derivation_alg: Option<Algorithm>,
    pub key_enc_alg: Algorithm,
    pub enc_key: OctetString,
}

#[derive(Sequence)]
pub(super) struct Originator {
    #[asn1(
        context_specific = "0",
        tag_mode = "IMPLICIT",
        constructed = "true",
        optional = "true"
    )]
    certs: Option<SetOfVec<Any>>,
    #[asn1(
        context_specific = "1",
        tag_mode = "IMPLICIT",
        constructed = "true",
        optional = "true"
    )]
    crls: Option<SetOfVec<Any>>,
}

impl Originator {
    /// RFC 5652 §9.1 bases the CMS version on certificate and CRL choices.
    pub fn version(&self) -> u8 {
        fn contains(choices: &Option<SetOfVec<Any>>, number: TagNumber) -> bool {
            choices.as_ref().is_some_and(|set| set.iter().any(|entry| matches!(entry.tag(), Tag::ContextSpecific { number: n, constructed: true } if n == number)))
        }
        if contains(&self.certs, TagNumber(3)) || contains(&self.crls, TagNumber(1)) {
            3
        } else if contains(&self.certs, TagNumber(2)) {
            1
        } else {
            0
        }
    }
}

#[derive(Sequence)]
pub(super) struct AuthenticatedData {
    pub version: u8,
    #[asn1(
        context_specific = "0",
        tag_mode = "IMPLICIT",
        constructed = "true",
        optional = "true"
    )]
    pub originator_info: Option<Originator>,
    pub recip_infos: SetOfVec<RecipientInfo>,
    pub mac_alg: Algorithm,
    #[asn1(
        context_specific = "1",
        tag_mode = "IMPLICIT",
        constructed = "true",
        optional = "true"
    )]
    pub digest_alg: Option<Algorithm>,
    pub encap_content_info: EncapsulatedContentInfo,
    #[asn1(
        context_specific = "2",
        tag_mode = "IMPLICIT",
        constructed = "true",
        optional = "true"
    )]
    pub auth_attrs: Option<Attributes>,
    pub mac: OctetString,
    #[asn1(
        context_specific = "3",
        tag_mode = "IMPLICIT",
        constructed = "true",
        optional = "true"
    )]
    pub unauth_attrs: Option<Attributes>,
}

#[derive(Sequence)]
pub(super) struct SignedData {
    version: u8,
    digest_algorithms: Any,
    encap_content_info: Any,
    #[asn1(
        context_specific = "0",
        tag_mode = "IMPLICIT",
        constructed = "true",
        optional = "true"
    )]
    certificates: Option<SetOfVec<Any>>,
    #[asn1(
        context_specific = "1",
        tag_mode = "IMPLICIT",
        constructed = "true",
        optional = "true"
    )]
    crls: Option<SetOfVec<Any>>,
    pub signer_infos: SetOfVec<SignerInfo>,
}

#[derive(Clone, Eq, PartialEq, Sequence, ValueOrd)]
pub(super) struct SignerInfo {
    version: u8,
    signer_identifier: Any,
    digest_algorithm: Any,
    #[asn1(
        context_specific = "0",
        tag_mode = "IMPLICIT",
        constructed = "true",
        optional = "true"
    )]
    signed_attrs: Option<SetOfVec<Any>>,
    signature_algorithm: Any,
    pub signature: OctetString,
    #[asn1(
        context_specific = "1",
        tag_mode = "IMPLICIT",
        constructed = "true",
        optional = "true"
    )]
    pub unsigned_attrs: Option<Attributes>,
}
