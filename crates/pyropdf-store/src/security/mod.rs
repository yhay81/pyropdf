//! The standard security handler, revisions 2 to 6 (ISO 32000-2 section
//! 7.6): key derivation, the user and owner password checks, permissions,
//! and the encryption and decryption of strings and streams by object
//! number and generation (DESIGN.md section 4.5).
//!
//! The store reads RC4 and AES and writes only AES: AES-128 (revision 4)
//! and AES-256 (revision 6). Public-key handlers and AES-GCM (ISO/TS 32003)
//! are not supported. Passwords are bounded at 127 UTF-8 bytes
//! (`password_input_size`), as pylopdf bounds them; revisions 2 to 4 read
//! the bytes as they are given, where PDFDocEncoding would differ only
//! beyond ASCII.

mod crypto;
#[cfg(test)]
mod tests;
mod value;

use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::value::{Dict, Name, PdfString, Reference, StringForm, Value};
use crate::{Error, Password, Result};

pub use value::{decrypt_value, encrypt_value};

/// The 32 bytes that pad a password (ISO 32000-2 section 7.6.4.3.2).
const PADDING: [u8; 32] = [
    0x28, 0xBF, 0x4E, 0x5E, 0x4E, 0x75, 0x8A, 0x41, 0x64, 0x00, 0x4E, 0x56, 0xFF, 0xFA, 0x01, 0x08,
    0x2E, 0x2E, 0x00, 0xB6, 0xD0, 0x68, 0x3E, 0x80, 0x2F, 0x0C, 0xA9, 0xFE, 0x64, 0x53, 0x69, 0x7A,
];

/// The longest password, in UTF-8 bytes.
pub const MAX_PASSWORD: usize = 127;

/// What a password opened.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Access {
    /// The user password, which the permissions restrict.
    User,
    /// The owner password, which allows everything.
    Owner,
}

/// How a crypt filter encrypts.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Method {
    /// No encryption: the `/Identity` filter.
    Identity,
    /// RC4 with a per-object key.
    Rc4,
    /// AES-128 in CBC mode with a per-object key (`/AESV2`).
    Aes128,
    /// AES-256 in CBC mode with the file key (`/AESV3`).
    Aes256,
}

/// What a value encrypted is: a string or a stream's data.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// A string.
    String,
    /// A stream's data.
    Stream,
}

/// An authenticated standard security handler: the file key and how
/// strings and streams are encrypted.
#[derive(Clone)]
pub struct Handler {
    key: Box<[u8]>,
    revision: u8,
    version: u8,
    strings: Method,
    streams: Method,
    encrypt_metadata: bool,
    permissions: u32,
    access: Access,
}

impl std::fmt::Debug for Handler {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // The key stays out of debug output.
        f.debug_struct("Handler")
            .field("revision", &self.revision)
            .field("strings", &self.strings)
            .field("streams", &self.streams)
            .field("access", &self.access)
            .finish_non_exhaustive()
    }
}

impl Handler {
    /// Authenticates `password` against `encrypt`, the `/Encrypt`
    /// dictionary, with `id`, the first element of the trailer's `/ID`:
    /// as the owner password first, and then as the user password.
    ///
    /// Refuses with [`Password::Incorrect`] a password that is neither,
    /// with `password_input_size` one longer than [`MAX_PASSWORD`], and
    /// with a malformed input error a dictionary of another handler or a
    /// revision outside 2 to 6.
    #[expect(
        clippy::disallowed_methods,
        reason = "keys, hashes, and the /Encrypt entries are at most 127 + 64 bytes, not input-sized"
    )]
    pub fn open(encrypt: &Dict, id: &[u8], password: &[u8], budget: &Budget) -> Result<Self> {
        budget.check(LimitCode::PasswordInputSize, password.len() as u64)?;
        let Params {
            version,
            revision,
            length,
            o,
            u,
            p,
            encrypt_metadata,
            strings,
            streams,
        } = Params::read(encrypt)?;
        let make = |key: Box<[u8]>, access| Self {
            key,
            revision,
            version,
            strings,
            streams,
            encrypt_metadata,
            permissions: p as u32,
            access,
        };
        if revision >= 5 {
            let oe = bytes(encrypt, "OE").filter(|b| b.len() >= 32);
            let ue = bytes(encrypt, "UE").filter(|b| b.len() >= 32);
            let (Some(oe), Some(ue)) = (oe, ue) else {
                return Err(Error::malformed(
                    "the /Encrypt dictionary has no /OE or /UE",
                ));
            };
            if o.len() < 48 || u.len() < 48 {
                return Err(Error::malformed("/O or /U is shorter than 48 bytes"));
            }
            let r6 = revision == 6;
            if crypto::hash(password, &o[32..40], &u[..48], r6, budget)? == o[..32] {
                let intermediate = crypto::hash(password, &o[40..48], &u[..48], r6, budget)?;
                let key =
                    crypto::aes_cbc_decrypt(&intermediate, &[0; 16], &oe[..32], false, budget)?;
                return Ok(make(key.into_boxed_slice(), Access::Owner));
            }
            if crypto::hash(password, &u[32..40], &[], r6, budget)? == u[..32] {
                let intermediate = crypto::hash(password, &u[40..48], &[], r6, budget)?;
                let key =
                    crypto::aes_cbc_decrypt(&intermediate, &[0; 16], &ue[..32], false, budget)?;
                return Ok(make(key.into_boxed_slice(), Access::User));
            }
            return Err(Error::from(Password::Incorrect));
        }
        if o.len() < 32 || u.len() < 32 {
            return Err(Error::malformed("/O or /U is shorter than 32 bytes"));
        }
        let n = if revision == 2 { 5 } else { length / 8 };
        // The owner password recovers the user password (algorithm 7).
        let owner_key = owner_key(password, revision, n);
        let mut user = o[..32].to_owned();
        if revision == 2 {
            user = crypto::rc4(&owner_key, &user, budget)?;
        } else {
            for i in (0..20u8).rev() {
                let key: Vec<u8> = owner_key.iter().map(|b| b ^ i).collect();
                user = crypto::rc4(&key, &user, budget)?;
            }
        }
        let params = (&o[..32], p, id, encrypt_metadata, revision, n);
        let recovered = file_key(&user, params);
        if user_matches(&recovered, u, revision, id, budget)? {
            return Ok(make(recovered, Access::Owner));
        }
        let key = file_key(password, params);
        if user_matches(&key, u, revision, id, budget)? {
            return Ok(make(key, Access::User));
        }
        Err(Error::from(Password::Incorrect))
    }

    /// Returns what the password opened.
    #[must_use]
    pub const fn access(&self) -> Access {
        self.access
    }

    /// Returns the permission flags of `/P` (ISO 32000-2 table 22).
    #[must_use]
    pub const fn permissions(&self) -> u32 {
        self.permissions
    }

    /// Returns the revision of the handler.
    #[must_use]
    pub const fn revision(&self) -> u8 {
        self.revision
    }

    /// Returns how strings and streams are encrypted.
    #[must_use]
    pub const fn methods(&self) -> (Method, Method) {
        (self.strings, self.streams)
    }

    /// Returns whether the document's metadata streams are encrypted.
    #[must_use]
    pub const fn encrypts_metadata(&self) -> bool {
        self.encrypt_metadata
    }

    /// Returns a description such as `Standard V5 R6 256-bit AES`, as
    /// pylopdf describes handlers.
    #[must_use]
    pub fn description(&self) -> String {
        let method = match self.streams {
            Method::Identity => "no",
            Method::Rc4 => "RC4",
            Method::Aes128 | Method::Aes256 => "AES",
        };
        let bits = match self.streams {
            Method::Aes256 => 256,
            _ if self.revision == 2 => 40,
            _ => self.key.len() * 8,
        };
        format!(
            "Standard V{} R{} {bits}-bit {method}",
            self.version, self.revision
        )
    }

    /// Decrypts `data`, a string or a stream's data of the object that
    /// `reference` names.
    pub fn decrypt(
        &self,
        kind: Kind,
        reference: Reference,
        data: &[u8],
        budget: &Budget,
    ) -> Result<Vec<u8>> {
        let method = self.method(kind);
        match method {
            Method::Identity => copy(data, budget),
            Method::Rc4 => crypto::rc4(&self.object_key(reference, false), data, budget),
            Method::Aes128 | Method::Aes256 => {
                if data.len() < 16 {
                    // An empty string encrypts to at least the IV and one
                    // block; anything shorter decrypts to nothing.
                    return Ok(Vec::new());
                }
                let key = self.aes_key(method, reference);
                let iv: [u8; 16] = data[..16].try_into().expect("16 bytes");
                crypto::aes_cbc_decrypt(&key, &iv, &data[16..], true, budget)
            }
        }
    }

    /// Encrypts `data`, a string or a stream's data of the object that
    /// `reference` names. The AES initialization vector is derived from the
    /// object key and the data, so that one input always encrypts alike and
    /// no two inputs share a vector.
    pub fn encrypt(
        &self,
        kind: Kind,
        reference: Reference,
        data: &[u8],
        budget: &Budget,
    ) -> Result<Vec<u8>> {
        let method = self.method(kind);
        match method {
            Method::Identity => copy(data, budget),
            Method::Rc4 => crypto::rc4(&self.object_key(reference, false), data, budget),
            Method::Aes128 | Method::Aes256 => {
                let key = self.aes_key(method, reference);
                let tag = [u8::from(kind == Kind::Stream)];
                let iv: [u8; 16] = crypto::sha256(&[&key, &tag, data])[..16]
                    .try_into()
                    .expect("16 bytes");
                let mut out = BudgetVec::new(budget);
                out.try_extend_from_slice(&iv)?;
                out.try_extend_from_slice(&crypto::aes_cbc_encrypt(
                    &key, &iv, data, true, budget,
                )?)?;
                Ok(out.into_vec())
            }
        }
    }

    const fn method(&self, kind: Kind) -> Method {
        match kind {
            Kind::String => self.strings,
            Kind::Stream => self.streams,
        }
    }

    fn aes_key(&self, method: Method, reference: Reference) -> Box<[u8]> {
        if method == Method::Aes256 {
            self.key.clone()
        } else {
            self.object_key(reference, true)
        }
    }

    /// The key of one object (algorithm 1).
    fn object_key(&self, reference: Reference, aes: bool) -> Box<[u8]> {
        let number = reference.number().to_le_bytes();
        let generation = reference.generation().to_le_bytes();
        let salt: &[u8] = if aes { b"sAlT" } else { b"" };
        let hash = crypto::md5(&[&self.key, &number[..3], &generation, salt]);
        let n = (self.key.len() + 5).min(16);
        Box::from(&hash[..n])
    }
}

/// The parameters of an `/Encrypt` dictionary of the standard handler.
struct Params<'a> {
    version: u8,
    revision: u8,
    length: usize,
    o: &'a [u8],
    u: &'a [u8],
    p: i32,
    encrypt_metadata: bool,
    strings: Method,
    streams: Method,
}

impl<'a> Params<'a> {
    fn read(encrypt: &'a Dict) -> Result<Self> {
        if !matches!(encrypt.get("Filter"), Some(Value::Name(n)) if n.as_bytes() == b"Standard") {
            return Err(Error::malformed(
                "the document uses a security handler other than the standard one",
            ));
        }
        let version = integer(encrypt, "V").unwrap_or(0);
        let revision = integer(encrypt, "R")
            .filter(|r| (2..=6).contains(r))
            .ok_or_else(|| Error::malformed("the standard handler's /R is not 2 to 6"))?;
        let length = integer(encrypt, "Length").unwrap_or(40);
        let length = if version == 1 { 40 } else { length };
        if !(40..=128).contains(&length) && revision < 5 {
            return Err(Error::malformed(
                "the /Length of the key is not 40 to 128 bits",
            ));
        }
        let p = match encrypt.get("P") {
            Some(&Value::Integer(p)) => p as i32,
            _ => return Err(Error::malformed("the /Encrypt dictionary has no /P")),
        };
        let (o, u) = match (bytes(encrypt, "O"), bytes(encrypt, "U")) {
            (Some(o), Some(u)) => (o, u),
            _ => return Err(Error::malformed("the /Encrypt dictionary has no /O or /U")),
        };
        let encrypt_metadata =
            !matches!(encrypt.get("EncryptMetadata"), Some(Value::Boolean(false)));
        let (strings, streams) = if version >= 4 {
            (filter(encrypt, "StrF")?, filter(encrypt, "StmF")?)
        } else {
            (Method::Rc4, Method::Rc4)
        };
        Ok(Self {
            version: u8::try_from(version).unwrap_or(u8::MAX),
            revision: revision as u8,
            length: length as usize,
            o,
            u,
            p,
            encrypt_metadata,
            strings,
            streams,
        })
    }
}

/// Reads the method of the crypt filter that `key` names (ISO 32000-2
/// section 7.6.6).
fn filter(encrypt: &Dict, key: &str) -> Result<Method> {
    let name = match encrypt.get(key) {
        Some(Value::Name(name)) => name.as_bytes(),
        _ => b"Identity",
    };
    if name == b"Identity" {
        return Ok(Method::Identity);
    }
    let Some(Value::Dict(filters)) = encrypt.get("CF") else {
        return Err(Error::malformed("the /Encrypt dictionary has no /CF"));
    };
    let Some(Value::Dict(filter)) = filters.get(name) else {
        return Err(Error::malformed("a crypt filter that /CF does not define"));
    };
    match filter.get("CFM") {
        Some(Value::Name(m)) if m.as_bytes() == b"V2" => Ok(Method::Rc4),
        Some(Value::Name(m)) if m.as_bytes() == b"AESV2" => Ok(Method::Aes128),
        Some(Value::Name(m)) if m.as_bytes() == b"AESV3" => Ok(Method::Aes256),
        None => Ok(Method::Identity),
        Some(Value::Name(m)) if m.as_bytes() == b"None" => Ok(Method::Identity),
        _ => Err(Error::malformed("a crypt filter's /CFM is not supported")),
    }
}

fn integer(dict: &Dict, key: &str) -> Option<i64> {
    match dict.get(key) {
        Some(&Value::Integer(n)) => Some(n),
        _ => None,
    }
}

fn bytes<'a>(dict: &'a Dict, key: &str) -> Option<&'a [u8]> {
    match dict.get(key) {
        Some(Value::String(s)) => Some(s.as_bytes()),
        _ => None,
    }
}

fn copy(data: &[u8], budget: &Budget) -> Result<Vec<u8>> {
    let mut out = BudgetVec::new(budget);
    out.try_extend_from_slice(data)?;
    Ok(out.into_vec())
}

/// Pads or truncates a password to 32 bytes.
fn padded(password: &[u8]) -> [u8; 32] {
    let mut out = PADDING;
    let n = password.len().min(32);
    out[..n].copy_from_slice(&password[..n]);
    out[n..].copy_from_slice(&PADDING[..32 - n]);
    out
}

/// The RC4 key of the owner password (algorithm 3, steps a to d).
fn owner_key(password: &[u8], revision: u8, n: usize) -> Vec<u8> {
    let mut hash = crypto::md5(&[&padded(password)]);
    if revision >= 3 {
        for _ in 0..50 {
            hash = crypto::md5(&[&hash]);
        }
    }
    hash[..n].to_owned()
}

/// The file key of a password (algorithm 2): `/O`, `/P`, the first `/ID`,
/// whether metadata is encrypted, the revision, and the key length.
fn file_key(
    password: &[u8],
    (o, p, id, metadata, revision, n): (&[u8], i32, &[u8], bool, u8, usize),
) -> Box<[u8]> {
    let skip: &[u8] = if revision >= 4 && !metadata {
        &[0xFF; 4]
    } else {
        &[]
    };
    let mut hash = crypto::md5(&[&padded(password), o, &p.to_le_bytes(), id, skip]);
    if revision >= 3 {
        for _ in 0..50 {
            hash = crypto::md5(&[&hash[..n]]);
        }
    }
    Box::from(&hash[..n])
}

/// Returns whether `key` is the file key that `/U` confirms (algorithms 4
/// and 5).
fn user_matches(key: &[u8], u: &[u8], revision: u8, id: &[u8], budget: &Budget) -> Result<bool> {
    let computed = computed_u(key, revision, id, budget)?;
    // Revisions 3 and 4 fill the last 16 bytes arbitrarily.
    let n = if revision == 2 { 32 } else { 16 };
    Ok(computed[..n] == u[..n])
}

/// Returns `/U` of `key` (algorithms 4 and 5), 32 bytes.
#[expect(
    clippy::disallowed_methods,
    reason = "keys, hashes, and the /Encrypt entries are at most 127 + 64 bytes, not input-sized"
)]
fn computed_u(key: &[u8], revision: u8, id: &[u8], budget: &Budget) -> Result<Vec<u8>> {
    if revision == 2 {
        return crypto::rc4(key, &PADDING, budget);
    }
    let mut data = crypto::md5(&[&PADDING, id]).to_vec();
    data = crypto::rc4(key, &data, budget)?;
    for i in 1..20u8 {
        let round: Vec<u8> = key.iter().map(|b| b ^ i).collect();
        data = crypto::rc4(&round, &data, budget)?;
    }
    data.resize(32, 0);
    Ok(data)
}

/// The encryption of a new save: AES-128 (revision 4) or AES-256
/// (revision 6), with passwords, permissions, and the randomness that the
/// caller draws.
#[derive(Clone, Debug)]
pub struct Encryption {
    /// The user password.
    pub user: Vec<u8>,
    /// The owner password.
    pub owner: Vec<u8>,
    /// The permission flags of `/P`.
    pub permissions: u32,
    /// AES-256 rather than AES-128.
    pub aes256: bool,
    /// Random bytes: the file key and the salts of AES-256, or the
    /// document's `/ID` of AES-128 from the first 16.
    pub random: [u8; 64],
}

impl Encryption {
    /// Returns the `/Encrypt` dictionary, the `/ID` to write, and the
    /// handler that encrypts the document's strings and streams.
    #[expect(
        clippy::disallowed_methods,
        reason = "keys, hashes, and the /Encrypt entries are at most 127 + 64 bytes, not input-sized"
    )]
    pub fn build(&self, id: Option<&[u8]>, budget: &Budget) -> Result<(Dict, Vec<u8>, Handler)> {
        budget.check(LimitCode::PasswordInputSize, self.user.len() as u64)?;
        budget.check(LimitCode::PasswordInputSize, self.owner.len() as u64)?;
        // Bits that ISO 32000-2 table 22 reserves as 1: 7, 8, and 13 to 32.
        let p = (self.permissions | 0xFFFF_F0C0) & !0b11;
        let p = p as i32;
        let id = id.map_or_else(|| self.random[..16].to_vec(), <[u8]>::to_vec);
        let name = |s: &str| Value::Name(Name::from(s));
        let string = |b: &[u8]| Value::String(PdfString::new(b, StringForm::Hexadecimal));
        let mut dict = Dict::new();
        let mut put = |k: &str, v: Value| {
            dict.insert(Name::from(k), v);
        };
        put("Filter", name("Standard"));
        let handler = if self.aes256 {
            let key = &self.random[..32];
            let salts = &self.random[32..64];
            let user = &self.user[..self.user.len().min(MAX_PASSWORD)];
            let owner = &self.owner[..self.owner.len().min(MAX_PASSWORD)];
            let mut u = crypto::hash(user, &salts[..8], &[], true, budget)?.to_vec();
            u.extend_from_slice(&salts[..16]);
            let ue_key = crypto::hash(user, &salts[8..16], &[], true, budget)?;
            let ue = crypto::aes_cbc_encrypt(&ue_key, &[0; 16], key, false, budget)?;
            let mut o = crypto::hash(owner, &salts[16..24], &u, true, budget)?.to_vec();
            o.extend_from_slice(&salts[16..32]);
            let oe_key = crypto::hash(owner, &salts[24..32], &u, true, budget)?;
            let oe = crypto::aes_cbc_encrypt(&oe_key, &[0; 16], key, false, budget)?;
            let mut perms = [0u8; 16];
            perms[..4].copy_from_slice(&p.to_le_bytes());
            perms[4..8].copy_from_slice(&[0xFF; 4]);
            perms[8..12].copy_from_slice(b"Tadb");
            perms[12..].copy_from_slice(&crypto::sha256(&[&self.random])[..4]);
            let perms = crypto::aes256_ecb_block(key, perms, false)?;
            put("V", 5.into());
            put("R", 6.into());
            put("Length", 256.into());
            put("CF", crypt_filters("AESV3", 32));
            put("StmF", name("StdCF"));
            put("StrF", name("StdCF"));
            put("O", string(&o));
            put("U", string(&u));
            put("OE", string(&oe));
            put("UE", string(&ue));
            put("Perms", string(&perms));
            Handler {
                key: Box::from(key),
                revision: 6,
                version: 5,
                strings: Method::Aes256,
                streams: Method::Aes256,
                encrypt_metadata: true,
                permissions: p as u32,
                access: Access::Owner,
            }
        } else {
            let n = 16;
            let owner_key = owner_key(&self.owner, 4, n);
            let mut o = padded(&self.user).to_vec();
            o = crypto::rc4(&owner_key, &o, budget)?;
            for i in 1..20u8 {
                let round: Vec<u8> = owner_key.iter().map(|b| b ^ i).collect();
                o = crypto::rc4(&round, &o, budget)?;
            }
            let key = file_key(&self.user, (&o, p, &id, true, 4, n));
            let u = computed_u(&key, 4, &id, budget)?;
            put("V", 4.into());
            put("R", 4.into());
            put("Length", 128.into());
            put("CF", crypt_filters("AESV2", 16));
            put("StmF", name("StdCF"));
            put("StrF", name("StdCF"));
            put("O", string(&o));
            put("U", string(&u));
            Handler {
                key,
                revision: 4,
                version: 4,
                strings: Method::Aes128,
                streams: Method::Aes128,
                encrypt_metadata: true,
                permissions: p as u32,
                access: Access::Owner,
            }
        };
        put("P", Value::Integer(i64::from(p)));
        Ok((dict, id, handler))
    }
}

#[expect(
    clippy::disallowed_methods,
    reason = "keys, hashes, and the /Encrypt entries are at most 127 + 64 bytes, not input-sized"
)]
fn crypt_filters(method: &str, length: i64) -> Value {
    let mut filter = Dict::new();
    filter.insert(Name::from("CFM"), Value::Name(Name::from(method)));
    filter.insert(Name::from("AuthEvent"), Value::Name(Name::from("DocOpen")));
    filter.insert(Name::from("Length"), length.into());
    let mut filters = Dict::new();
    filters.insert(Name::from("StdCF"), Value::Dict(filter));
    Value::Dict(filters)
}
