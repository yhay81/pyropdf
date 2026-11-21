//! ISO 32000-2 §7.6.5. The credential provider opens CMS envelopes. The
//! store selects the crypt filter, derives its key, and applies its rules.

use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};

use sha1::Sha1;
use sha2::{Digest, Sha256};

use super::{Access, Handler, Method, PasswordEncryption, copy, crypto, integer};
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::value::{Array, Dict, Name, PdfString, StringForm, Value};
use crate::{Error, Password, Result};

/// The only CMS plaintexts defined by ISO 32000-2 Table 27.
#[derive(Clone, Copy)]
pub enum RecipientPayload {
    /// A stream-specific seed. It carries no document permissions.
    Stream([u8; 20]),
    /// A default-filter seed followed by four permission bytes.
    Document([u8; 24]),
}

impl RecipientPayload {
    /// Copies the bounded result of CMS decryption from the credential provider.
    pub fn from_bytes(bytes: &[u8]) -> Result<Self> {
        if let Ok(seed) = bytes.try_into() {
            Ok(Self::Stream(seed))
        } else if let Ok(payload) = bytes.try_into() {
            Ok(Self::Document(payload))
        } else {
            Err(Error::malformed(
                "a CMS recipient payload must be 20 or 24 bytes",
            ))
        }
    }
}

fn name<'a>(dict: &'a Dict, field: &str) -> Result<&'a [u8]> {
    match dict.get(field) {
        Some(Value::Name(n)) => Ok(n.as_bytes()),
        _ => Err(Error::malformed(format!(
            "missing or invalid /{field} name in public-key encryption"
        ))),
    }
}

#[derive(Clone)]
pub(super) struct RecipientAccess {
    pub(super) subfilter: &'static str,
    owner: Arc<AtomicBool>,
}

impl RecipientAccess {
    pub(super) fn owner(&self) -> bool {
        self.owner.load(Ordering::Relaxed)
    }
}

#[derive(Clone, Copy)]
struct Profile {
    subfilter: &'static str,
    version: u8,
    length: usize,
    strings: Method,
    streams: Method,
    metadata: bool,
}

impl Profile {
    fn aes(method: Method, metadata: bool) -> Result<Self> {
        let (version, length) = match method {
            Method::Aes128 => (4, 16),
            Method::Aes256 => (5, 32),
            Method::AesGcm => (6, 32),
            _ => return Err(Error::malformed("new recipient encryption requires AES")),
        };
        Ok(Self {
            subfilter: "adbe.pkcs7.s5",
            version,
            length,
            strings: method,
            streams: method,
            metadata,
        })
    }

    fn read<'a>(encrypt: &'a Dict, budget: &Budget) -> Result<(Self, &'a Array)> {
        let subfilter = name(encrypt, "SubFilter")?;
        let (profile, source) = match (subfilter, integer(encrypt, "V")) {
            (b"adbe.pkcs7.s3" | b"adbe.pkcs7.s4", Some(v @ (1 | 2))) => {
                let bits = match (v, encrypt.get("Length")) {
                    (1, _) | (_, None) => 40,
                    (_, Some(Value::Integer(bits)))
                        if (40..=128).contains(bits) && bits % 8 == 0 =>
                    {
                        *bits
                    }
                    _ => {
                        return Err(Error::malformed(
                            "public-key RC4 /Length must be a multiple of 8 from 40 to 128",
                        ));
                    }
                };
                if ["CF", "StrF", "StmF", "EFF"]
                    .iter()
                    .any(|k| encrypt.get(k).is_some())
                {
                    return Err(Error::malformed(
                        "legacy public-key encryption cannot select crypt filters",
                    ));
                }
                let subfilter = if subfilter == b"adbe.pkcs7.s3" {
                    "adbe.pkcs7.s3"
                } else {
                    "adbe.pkcs7.s4"
                };
                (
                    Self {
                        subfilter,
                        version: v as u8,
                        length: bits as usize / 8,
                        strings: Method::Rc4,
                        streams: Method::Rc4,
                        metadata: true,
                    },
                    encrypt,
                )
            }
            (b"adbe.pkcs7.s5", Some(v @ (4..=6))) => Self::crypt_filter(encrypt, v as u8)?,
            _ => {
                return Err(Error::malformed(
                    "invalid public-key SubFilter and V combination; adbe.pkcs7.s5 needs V4, V5 or V6",
                ));
            }
        };
        let Some(Value::Array(envelopes)) = source.get("Recipients") else {
            return Err(Error::malformed(
                "default public-key /Recipients must be an array of CMS strings",
            ));
        };
        budget.check(LimitCode::RecipientCount, envelopes.len() as u64)?;
        if envelopes.is_empty() {
            return Err(Error::malformed(
                "a public-key recipient list must not be empty",
            ));
        }
        Ok((profile, envelopes))
    }

    fn crypt_filter(encrypt: &Dict, version: u8) -> Result<(Self, &Dict)> {
        let selected = |key| match encrypt.get(key) {
            None => Ok(b"Identity".as_slice()),
            _ => name(encrypt, key),
        };
        let strings = selected("StrF")?;
        let streams = selected("StmF")?;
        let active = if streams == b"Identity" {
            strings
        } else {
            streams
        };
        if active == b"Identity"
            || (strings != b"Identity" && strings != active)
            || (encrypt.get("EFF").is_some() && name(encrypt, "EFF")? != streams)
        {
            return Err(Error::malformed(
                "public-key encryption requires a single default crypt filter; distinct /EFF is unsupported",
            ));
        }
        let Some(Value::Dict(filters)) = encrypt.get("CF") else {
            return Err(Error::malformed("missing public-key /CF"));
        };
        if filters.len() != 1 {
            return Err(Error::malformed(
                "multiple public-key crypt filters require separate recipient selection",
            ));
        }
        let Some(Value::Dict(cf)) = filters.get(active) else {
            return Err(Error::malformed("undefined public-key crypt filter"));
        };
        let (cipher, bits) = match (version, name(cf, "CFM")?) {
            (6, b"AESV4") => (Method::AesGcm, 256),
            (5, b"AESV3") => (Method::Aes256, 256),
            (4, b"AESV2") => (Method::Aes128, 128),
            (4, b"V2") => {
                let bits = integer(cf, "Length")
                    .or_else(|| integer(encrypt, "Length"))
                    .unwrap_or(128);
                if !(40..=128).contains(&bits) || bits % 8 != 0 {
                    return Err(Error::malformed("invalid public-key RC4 filter length"));
                }
                (Method::Rc4, bits)
            }
            _ => {
                return Err(Error::malformed(
                    "public-key crypt filters require V4 V2/AESV2, V5 AESV3 or V6 AESV4",
                ));
            }
        };
        for dict in [encrypt, cf] {
            if dict.get("Length").is_some() && integer(dict, "Length") != Some(bits) {
                return Err(Error::malformed(
                    "public-key /Length must give the AES key size in bits",
                ));
            }
        }
        let metadata = match cf.get("EncryptMetadata") {
            None => true,
            Some(Value::Boolean(b)) => *b,
            _ => {
                return Err(Error::malformed(
                    "public-key /EncryptMetadata must be a boolean",
                ));
            }
        };
        let method = |cf: &[u8]| {
            if cf == b"Identity" {
                Method::Identity
            } else {
                cipher
            }
        };
        Ok((
            Self {
                subfilter: "adbe.pkcs7.s5",
                version,
                length: bits as usize / 8,
                strings: method(strings),
                streams: method(streams),
                metadata,
            },
            cf,
        ))
    }

    fn handler(
        self,
        seed: &[u8],
        envelopes: &Array,
        permissions: u32,
        budget: &Budget,
    ) -> Result<Handler> {
        let clear_metadata = !self.metadata && self.streams != Method::Identity;
        let key = if self.version >= 5 {
            derive_key::<Sha256>(seed, envelopes, clear_metadata, self.length, budget)?
        } else {
            derive_key::<Sha1>(seed, envelopes, clear_metadata, self.length, budget)?
        };
        Ok(Handler {
            key,
            recipient: Some(RecipientAccess {
                subfilter: self.subfilter,
                owner: Arc::new(AtomicBool::new(permissions & 2 != 0)),
            }),
            password: Box::default(),
            version: self.version,
            revision: 0,
            strings: self.strings,
            streams: self.streams,
            encrypt_metadata: self.metadata,
            permissions,
            access: if permissions & 2 == 0 {
                Access::User
            } else {
                Access::Owner
            },
        })
    }
}

impl Handler {
    /// Whether this is an authenticated recipient security handler.
    #[must_use]
    pub const fn is_recipient(&self) -> bool {
        self.recipient.is_some()
    }

    /// Retain authorization to change encryption when a second verified
    /// credential opens this same file key. The version and its edits remain
    /// in place; unrelated documents cannot inherit the new authority.
    pub fn retain_recipient_access(&self, verified: &Self) {
        if let Some(recipient) = &self.recipient
            && verified.is_recipient()
            && verified.access() == Access::Owner
            && self.key == verified.key
        {
            recipient.owner.store(true, Ordering::Relaxed);
        }
    }

    /// Opens a recipient-encrypted document. Each callback returns the
    /// authenticated recipient's 20-byte seed and four permission bytes.
    pub fn open_recipient(
        encrypt: &Dict,
        mut decrypt: impl FnMut(&[u8]) -> Result<Option<RecipientPayload>>,
        budget: &Budget,
    ) -> Result<Self> {
        if name(encrypt, "Filter")? != b"Adobe.PubSec" {
            return Err(Password::Recipient.into());
        }
        let (profile, envelopes) = Profile::read(encrypt, budget)?;
        let mut payload = None;
        for value in envelopes.iter() {
            budget.poll()?;
            let envelope = envelope(value, budget)?;
            if payload.is_none() {
                budget.charge(LimitCode::MemorySize, envelope.len() as u64)?;
                let opened = decrypt(envelope);
                budget.release(LimitCode::MemorySize, envelope.len() as u64);
                payload = opened?;
            }
        }
        let RecipientPayload::Document(payload) = payload.ok_or(Password::Recipient)? else {
            return Err(Error::malformed(
                "a default recipient filter requires four permission bytes after its seed",
            ));
        };
        let permissions = u32::from_be_bytes(payload[20..].try_into().expect("four bytes"));
        let permissions = if profile.subfilter == "adbe.pkcs7.s3" {
            permissions & 0x3f
        } else {
            permissions
        };
        profile.handler(&payload[..20], envelopes, permissions, budget)
    }

    /// For hayro's password-only interface, wrap the authenticated AES-256
    /// key in a private in-memory Standard dictionary. The working password
    /// is separate from the key and only travels alongside the bridge.
    #[expect(
        clippy::disallowed_methods,
        reason = "the hex representation of SHA-256 is exactly 64 bytes"
    )]
    pub fn recipient_bridge(&self, budget: &Budget) -> Result<Option<(String, Dict)>> {
        if !self.is_recipient() || self.version != 5 {
            return Ok(None);
        }
        use std::fmt::Write;
        let mut password = String::with_capacity(64);
        for b in crypto::sha256(&[b"pyropdf hayro bridge password", &self.key]) {
            write!(&mut password, "{b:02x}").expect("writing to a string");
        }
        let mut random = [0; 64];
        random[..32].copy_from_slice(&self.key);
        random[32..].copy_from_slice(&crypto::sha256(&[b"pyropdf hayro bridge salts", &self.key]));
        let password_encryption = PasswordEncryption {
            user: password.as_bytes().into(),
            owner: password.as_bytes().into(),
            permissions: self.permissions,
            method: Method::Aes256,
            random,
        };
        let (mut dict, _, _) = password_encryption.build(None, budget)?;
        if self.strings == Method::Identity {
            put_name(&mut dict, "StrF", "Identity");
        }
        if self.streams == Method::Identity {
            put_name(&mut dict, "StmF", "Identity");
        }
        if !self.encrypt_metadata {
            put(&mut dict, "EncryptMetadata", Value::Boolean(false));
            let mut perms = [0; 16];
            let p = integer(&dict, "P").expect("Standard permissions") as i32;
            perms[..4].copy_from_slice(&p.to_le_bytes());
            perms[4..8].fill(0xff);
            perms[8..12].copy_from_slice(b"Fadb");
            let perms = crypto::aes256_ecb_block(&self.key, perms, false)?;
            put(
                &mut dict,
                "Perms",
                Value::String(PdfString::new(&perms[..], StringForm::Hexadecimal)),
            );
        }
        Ok(Some((password, dict)))
    }
}

fn derive_key<D: Digest>(
    seed: &[u8],
    envelopes: &Array,
    clear_metadata: bool,
    size: usize,
    budget: &Budget,
) -> Result<Box<[u8]>> {
    let mut hash = D::new();
    hash.update(seed);
    for value in envelopes.iter() {
        budget.poll()?;
        hash.update(envelope(value, budget)?);
    }
    if clear_metadata {
        hash.update([0xff; 4]);
    }
    Ok(Box::from(&hash.finalize()[..size]))
}

/// Map a clear metadata stream's explicit Identity filter to the store's
/// ordinary filter chain. Other explicit stream overrides belong to #893.
pub(super) fn stream_filter(
    dict: &mut Dict,
    clear_metadata: bool,
    encrypt: bool,
    budget: &Budget,
) -> Result<()> {
    fn items(value: Option<&Value>) -> &[Value] {
        match value {
            Some(Value::Array(a)) => a.as_slice(),
            Some(v) => std::slice::from_ref(v),
            None => &[],
        }
    }
    let filters = items(dict.get("Filter"));
    let params = items(dict.get("DecodeParms"));
    budget.check(LimitCode::StreamFilterCount, filters.len() as u64)?;
    let crypt = |v: &Value| matches!(v, Value::Name(n) if n.as_bytes() == b"Crypt");
    let explicit = filters.iter().position(crypt);
    if explicit.is_none() && !(clear_metadata && encrypt) {
        return Ok(());
    }
    if !clear_metadata || explicit.is_some_and(|i| i != 0 || filters[1..].iter().any(crypt)) {
        return Err(Error::malformed(
            "explicit public-key stream /Crypt override is unsupported",
        ));
    }
    if !params.is_empty() && params.len() != filters.len() {
        return Err(Error::malformed(
            "metadata filter parameters must correspond to its filters",
        ));
    }
    let skip = usize::from(explicit.is_some());
    if skip != 0 {
        let selected = match params.first() {
            None | Some(Value::Null) => None,
            Some(Value::Dict(param)) => param.get("Name"),
            _ => return Err(Error::malformed("invalid metadata /Crypt parameters")),
        };
        if !matches!(selected, None | Some(Value::Name(_)))
            || matches!(selected, Some(Value::Name(n)) if n.as_bytes() != b"Identity")
        {
            return Err(Error::malformed(
                "clear metadata requires an Identity crypt filter",
            ));
        }
        if encrypt {
            return Ok(());
        }
    }
    let mut methods = BudgetVec::new(budget);
    let mut options = BudgetVec::new(budget);
    if encrypt {
        budget.check(LimitCode::StreamFilterCount, filters.len() as u64 + 1)?;
        methods.try_push(Value::Name(Name::from("Crypt")))?;
        let mut identity = Dict::new();
        put_name(&mut identity, "Name", "Identity");
        options.try_push(Value::Dict(identity))?;
    }
    for (i, method) in filters.iter().enumerate().skip(skip) {
        methods.try_push(method.clone())?;
        options.try_push(params.get(i).cloned().unwrap_or(Value::Null))?;
    }
    dict.remove("Filter");
    dict.remove("DecodeParms");
    if !methods.is_empty() {
        put(dict, "Filter", Value::Array(methods.into_vec().into()));
        if options.iter().any(|v| !matches!(v, Value::Null)) {
            put(dict, "DecodeParms", Value::Array(options.into_vec().into()));
        }
    }
    Ok(())
}

fn envelope<'a>(value: &'a Value, budget: &Budget) -> Result<&'a [u8]> {
    let Value::String(string) = value else {
        return Err(Error::malformed(
            "each CMS recipient envelope must be a byte string",
        ));
    };
    let der = string.as_bytes();
    if der.is_empty() {
        return Err(Error::malformed("empty CMS recipient envelope"));
    }
    budget.check(LimitCode::RecipientEnvelopeSize, der.len() as u64)?;
    Ok(der)
}

#[expect(
    clippy::disallowed_methods,
    reason = "the encryption dictionary has a fixed number of entries"
)]
fn put(dict: &mut Dict, field: &str, value: Value) {
    dict.insert(Name::from(field), value);
}

fn put_name(dict: &mut Dict, field: &str, value: &str) {
    put(dict, field, Value::Name(Name::from(value)));
}

/// A fresh seed, a document identifier, and its CMS envelopes in file order.
#[derive(Clone)]
pub struct RecipientEncryption {
    /// Random 20-byte seed encrypted into each CMS payload.
    pub seed: [u8; 20],
    /// A fresh identifier for documents without one.
    pub id: [u8; 16],
    /// CMS EnvelopedData strings for distinct authorized recipient groups.
    pub envelopes: Vec<Vec<u8>>,
    /// Whether the default stream filter encrypts metadata.
    pub encrypt_metadata: bool,
    /// The AES content encryption method for the recipient group.
    pub method: Method,
}

impl RecipientEncryption {
    pub(super) fn build(
        &self,
        id: Option<&[u8]>,
        budget: &Budget,
    ) -> Result<(Dict, Vec<u8>, Handler)> {
        budget.check(LimitCode::RecipientCount, self.envelopes.len() as u64)?;
        if self.envelopes.is_empty() {
            return Err(Error::malformed(
                "a public-key recipient list must not be empty",
            ));
        }
        let mut items = BudgetVec::new(budget);
        for der in &self.envelopes {
            budget.poll()?;
            budget.check(LimitCode::RecipientEnvelopeSize, der.len() as u64)?;
            if der.is_empty() {
                return Err(Error::malformed("empty CMS recipient envelope"));
            }
            items.try_push(Value::String(PdfString::new(
                copy(der, budget)?,
                StringForm::Hexadecimal,
            )))?;
        }
        let envelopes = Array::from(items.into_vec());
        let profile = Profile::aes(self.method, self.encrypt_metadata)?;
        let bits = (profile.length * 8) as i64;
        let cfm = match profile.version {
            4 => "AESV2",
            5 => "AESV3",
            _ => "AESV4",
        };
        let handler = profile.handler(&self.seed, &envelopes, u32::MAX, budget)?;
        let mut cf = Dict::new();
        put_name(&mut cf, "CFM", cfm);
        put_name(&mut cf, "AuthEvent", "DocOpen");
        put(&mut cf, "Length", bits.into());
        put(
            &mut cf,
            "EncryptMetadata",
            Value::Boolean(self.encrypt_metadata),
        );
        put(&mut cf, "Recipients", Value::Array(envelopes));
        let mut filters = Dict::new();
        put(&mut filters, "DefaultCryptFilter", Value::Dict(cf));
        let mut dict = Dict::new();
        put_name(&mut dict, "Filter", "Adobe.PubSec");
        put_name(&mut dict, "SubFilter", "adbe.pkcs7.s5");
        put(&mut dict, "V", i64::from(profile.version).into());
        put(
            &mut dict,
            "EncryptMetadata",
            Value::Boolean(self.encrypt_metadata),
        );
        put(&mut dict, "CF", Value::Dict(filters));
        put_name(&mut dict, "StmF", "DefaultCryptFilter");
        put_name(&mut dict, "StrF", "DefaultCryptFilter");
        Ok((dict, copy(id.unwrap_or(&self.id), budget)?, handler))
    }
}
