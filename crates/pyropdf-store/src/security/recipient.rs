//! ISO 32000-2 §7.6.5. The credential provider opens CMS envelopes. The
//! store selects the crypt filter, derives its key, and applies its rules.

use std::sync::{
    Arc, OnceLock,
    atomic::{AtomicBool, Ordering},
};

use sha1::Sha1;
use sha2::{Digest, Sha256};

use super::filters::{Filters, IDENTITY, Key, selected};
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
    profile_id: Option<[u8; 32]>,
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
    fn aes(aes256: bool, metadata: bool) -> Self {
        let (version, length, method) = if aes256 {
            (5, 32, Method::Aes256)
        } else {
            (4, 16, Method::Aes128)
        };
        Self {
            subfilter: "adbe.pkcs7.s5",
            version,
            length,
            strings: method,
            streams: method,
            metadata,
        }
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
            _ => {
                return Err(Error::malformed(
                    "invalid public-key SubFilter and V combination; adbe.pkcs7.s5 needs V4 or V5",
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

    fn crypt_filter(encrypt: &Dict, cf: &Dict, version: u8) -> Result<Self> {
        let (cipher, bits) = match (version, name(cf, "CFM")?) {
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
                    "public-key crypt filters require V4 V2/AESV2 or V5 AESV3",
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
        Ok(Self {
            subfilter: "adbe.pkcs7.s5",
            version,
            length: bits as usize / 8,
            strings: cipher,
            streams: cipher,
            metadata,
        })
    }

    fn handler(
        self,
        seed: &[u8],
        envelopes: &[Value],
        permissions: u32,
        budget: &Budget,
    ) -> Result<Handler> {
        let clear_metadata = !self.metadata && self.streams != Method::Identity;
        let key = if self.version == 5 {
            derive_key::<Sha256>(seed, envelopes, clear_metadata, self.length, budget)?
        } else {
            derive_key::<Sha1>(seed, envelopes, clear_metadata, self.length, budget)?
        };
        Ok(Handler {
            filters: super::filters::Filters::default(),
            key,
            recipient: Some(RecipientAccess {
                profile_id: None,
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
        if let (Some(recipient), Some(other)) = (&self.recipient, &verified.recipient)
            && (recipient.profile_id.is_some() && recipient.profile_id == other.profile_id
                || !self.key.is_empty() && self.key == verified.key)
        {
            self.filters.retain_keys(&verified.filters);
            if verified.access() == Access::Owner {
                recipient.owner.store(true, Ordering::Relaxed);
            }
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
        if name(encrypt, "SubFilter")? == b"adbe.pkcs7.s5"
            && let Some(version @ (4 | 5)) = integer(encrypt, "V")
        {
            return open_filters(encrypt, version as u8, &mut decrypt, budget);
        }
        let (profile, envelopes) = Profile::read(encrypt, budget)?;
        let payload =
            unwrap_list(envelopes.as_slice(), &mut decrypt, budget)?.ok_or(Password::Recipient)?;
        let RecipientPayload::Document(payload) = payload else {
            return Err(Error::malformed(
                "a default recipient filter requires four permission bytes after its seed",
            ));
        };
        let permissions = permissions(&payload)?;
        let permissions = if profile.subfilter == "adbe.pkcs7.s3" {
            permissions & 0x3f
        } else {
            permissions
        };
        profile.handler(&payload[..20], envelopes.as_slice(), permissions, budget)
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
        // A public document with protected attachments has no authenticated
        // document key. Its in-memory wrapper selects Identity on public content.
        let key: &[u8] = if self.key.len() == 32 {
            &self.key
        } else if self.strings == Method::Identity && self.streams == Method::Identity {
            &[0; 32]
        } else {
            return Ok(None);
        };
        use std::fmt::Write;
        let mut password = String::with_capacity(64);
        for b in crypto::sha256(&[b"pyropdf hayro bridge password", key]) {
            write!(&mut password, "{b:02x}").expect("writing to a string");
        }
        let mut random = [0; 64];
        random[..32].copy_from_slice(key);
        random[32..].copy_from_slice(&crypto::sha256(&[b"pyropdf hayro bridge salts", key]));
        let password_encryption = PasswordEncryption {
            user: password.as_bytes().into(),
            owner: password.as_bytes().into(),
            permissions: self.permissions,
            aes256: true,
            random,
        };
        let (mut dict, _, _) = password_encryption.build(None, budget)?;
        let Some(Value::Dict(cf)) = dict.get_mut("CF") else {
            unreachable!("Standard V5 filter dictionary")
        };
        let aes = cf
            .remove("StdCF")
            .expect("the Standard wrapper defines AES-256");
        let mut entries = BudgetVec::new(budget);
        for (name, method, selected) in self.filters.named.iter() {
            budget.poll()?;
            let available = match selected {
                Key::File => true,
                Key::Recipient(confirmed) => {
                    confirmed.get().is_some_and(|other| other.as_ref() == key)
                }
            };
            if available && *method == Method::Aes256 {
                entries.try_push((name.clone(), aes.clone()))?;
            }
        }
        #[expect(
            clippy::disallowed_methods,
            reason = "the authenticated filter names are unique and their entries are bounded by the operation budget"
        )]
        {
            *cf = entries.into_vec().into_iter().collect();
        }
        for (field, name) in [
            ("StmF", &self.filters.streams),
            ("StrF", &self.filters.strings),
        ] {
            if let Some(name) = name {
                put(&mut dict, field, Value::Name(name.clone()));
            }
        }
        if !self.encrypt_metadata {
            put(&mut dict, "EncryptMetadata", Value::Boolean(false));
            let mut perms = [0; 16];
            let p = integer(&dict, "P").expect("Standard permissions") as i32;
            perms[..4].copy_from_slice(&p.to_le_bytes());
            perms[4..8].fill(0xff);
            perms[8..12].copy_from_slice(b"Fadb");
            let perms = crypto::aes256_ecb_block(key, perms, false)?;
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
    envelopes: &[Value],
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
    /// Select V5 AESV3; otherwise use V4 AESV2.
    pub aes256: bool,
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
        let profile = Profile::aes(self.aes256, self.encrypt_metadata);
        let bits = (profile.length * 8) as i64;
        let cfm = if self.aes256 { "AESV3" } else { "AESV2" };
        let mut handler = profile.handler(&self.seed, envelopes.as_slice(), u32::MAX, budget)?;
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
        let mut entries = BudgetVec::new(budget);
        entries.try_push((Name::from("DefaultCryptFilter"), profile.streams, Key::File))?;
        handler.filters = Filters::from_entries(&dict, entries)?;
        Ok((dict, copy(id.unwrap_or(&self.id), budget)?, handler))
    }
}

fn permissions(payload: &[u8]) -> Result<u32> {
    let flags: [u8; 4] = payload
        .get(20..)
        .and_then(|b| b.try_into().ok())
        .ok_or_else(|| {
            Error::malformed("a default recipient payload needs a seed and four permission bytes")
        })?;
    Ok(u32::from_be_bytes(flags))
}

fn unwrap_list(
    values: &[Value],
    decrypt: &mut impl FnMut(&[u8]) -> Result<Option<RecipientPayload>>,
    budget: &Budget,
) -> Result<Option<RecipientPayload>> {
    let mut payload = None;
    for value in values {
        budget.poll()?;
        let der = envelope(value, budget)?;
        if payload.is_none() {
            budget.charge(LimitCode::MemorySize, der.len() as u64)?;
            let result = decrypt(der);
            budget.release(LimitCode::MemorySize, der.len() as u64);
            payload = result?;
        }
    }
    Ok(payload)
}

fn open_filters(
    encrypt: &Dict,
    version: u8,
    decrypt: &mut impl FnMut(&[u8]) -> Result<Option<RecipientPayload>>,
    budget: &Budget,
) -> Result<Handler> {
    let strings = selected(encrypt, "StrF", IDENTITY)?;
    let streams = selected(encrypt, "StmF", IDENTITY)?;
    let embedded = selected(encrypt, "EFF", streams)?;
    let Some(Value::Dict(filters)) = encrypt.get("CF") else {
        return Err(Error::malformed("missing public-key /CF"));
    };
    let mut entries = BudgetVec::new(budget);
    let mut primary: Option<(bool, Handler)> = None;
    let document_public = strings == IDENTITY && streams == IDENTITY;
    let mut doc_permissions = if document_public { 0 } else { u32::MAX };
    let mut total = 0usize;
    for (name, filter) in filters.iter() {
        budget.poll()?;
        let name_bytes = name.as_bytes();
        let Value::Dict(filter) = filter else {
            return Err(Error::malformed(
                "a public-key /CF entry must be a dictionary",
            ));
        };
        if name_bytes == IDENTITY {
            return Err(Error::malformed(
                "the built-in /Identity crypt filter cannot be redefined",
            ));
        }
        let mut profile = Profile::crypt_filter(encrypt, filter, version)?;
        let event = selected(filter, "AuthEvent", b"DocOpen")?;
        if !matches!(event, b"DocOpen" | b"EFOpen") {
            return Err(Error::malformed("invalid public-key /AuthEvent"));
        }
        let required = name_bytes == strings || name_bytes == streams;
        let (recipients, with_permissions) = match filter.get("Recipients") {
            Some(Value::Array(array)) if required || name_bytes == embedded => {
                (array.as_slice(), true)
            }
            Some(v @ Value::String(_)) if !required => (std::slice::from_ref(v), false),
            _ => {
                return Err(Error::malformed(
                    "invalid /Recipients for the selected public-key crypt filter",
                ));
            }
        };
        if recipients.is_empty() {
            return Err(Error::malformed(
                "a public-key recipient list must not be empty",
            ));
        }
        total = total.saturating_add(recipients.len());
        budget.check(LimitCode::RecipientCount, total as u64)?;
        let method = profile.streams;
        let key = match unwrap_list(recipients, decrypt, budget)? {
            Some(payload) => {
                let payload: &[u8] = match &payload {
                    RecipientPayload::Document(bytes) => bytes,
                    RecipientPayload::Stream(bytes) => bytes,
                };
                let flags = if with_permissions {
                    let flags = permissions(payload)?;
                    if flags & (1 << 12) == 0 && version == 5 {
                        return Err(Error::malformed(
                            "public-key encryption requires unsupported PDF MAC validation",
                        ));
                    }
                    flags
                } else if payload.len() == 20 {
                    0
                } else {
                    return Err(Error::malformed(
                        "a stream-specific CMS recipient payload must contain only a 20-byte seed",
                    ));
                };
                if required {
                    doc_permissions &= flags;
                } else if document_public && name_bytes == embedded {
                    doc_permissions = flags;
                }
                // Only the default stream filter's EncryptMetadata controls its key.
                if name_bytes != streams && !(name_bytes == embedded && with_permissions) {
                    profile.metadata = true;
                }
                let handler = profile.handler(&payload[..20], recipients, flags, budget)?;
                let key = handler.key.clone();
                let preferred = name_bytes == streams
                    || (streams == IDENTITY && name_bytes == strings)
                    || (streams == IDENTITY && strings == IDENTITY && name_bytes == embedded);
                if primary
                    .as_ref()
                    .is_none_or(|(was_preferred, _)| preferred && !was_preferred)
                {
                    primary = Some((preferred, handler));
                }
                Some(key)
            }
            None if required => return Err(Password::Recipient.into()),
            None => None,
        };
        let confirmed = OnceLock::new();
        if let Some(key) = key {
            let _ = confirmed.set(key);
        }
        entries.try_push((name.clone(), method, Key::Recipient(Arc::new(confirmed))))?;
    }
    let selection = Filters::from_entries(encrypt, entries)?;
    let mut handler = match primary {
        Some((_, handler)) => handler,
        None if document_public => Handler {
            key: Box::default(),
            password: Box::default(),
            recipient: Some(RecipientAccess {
                profile_id: None,
                subfilter: "adbe.pkcs7.s5",
                owner: Arc::new(AtomicBool::new(false)),
            }),
            filters: Filters::default(),
            version,
            revision: 0,
            strings: Method::Identity,
            streams: Method::Identity,
            permissions: 0,
            access: Access::User,
            encrypt_metadata: false,
        },
        None => return Err(Password::Recipient.into()),
    };
    if document_public {
        handler.encrypt_metadata = false;
    }
    if streams == IDENTITY && strings == IDENTITY && embedded == IDENTITY {
        doc_permissions = 0;
    }
    handler.permissions = doc_permissions;
    handler.access = if doc_permissions & 2 != 0 {
        Access::Owner
    } else {
        Access::User
    };
    if let Some(access) = &mut handler.recipient {
        // Subsequent credentials share only keys verified against this same encryption dictionary.
        access.profile_id = encrypt
            .lexeme()
            .map(|bytes| crypto::sha256(&[bytes.as_slice()]));
        access
            .owner
            .store(handler.access == Access::Owner, Ordering::Relaxed);
    }
    handler.strings = selection.method(strings)?;
    handler.streams = selection.method(streams)?;
    handler.filters = selection;
    Ok(handler)
}
