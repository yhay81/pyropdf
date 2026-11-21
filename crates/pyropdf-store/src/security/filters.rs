//! ISO 32000-2 §§7.4.10 and 7.6.6. Explicit stream filters use the
//! filter's shared key directly; implicit legacy encryption derives an
//! object key. Keep the declaration in the working stream dictionary.

use std::sync::{Arc, OnceLock};

use super::{Handler, Kind, Method, copy, crypto};
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::value::{Dict, Name, Reference, Value};
use crate::{Error, Password, Result};

pub(super) const IDENTITY: &[u8] = b"Identity";

pub(super) fn selected<'a>(dict: &'a Dict, field: &str, fallback: &'a [u8]) -> Result<&'a [u8]> {
    match dict.get(field) {
        None | Some(Value::Null) => Ok(fallback),
        Some(Value::Name(name)) => Ok(name.as_bytes()),
        _ => Err(Error::malformed(format!(
            "/{field} must name a crypt filter"
        ))),
    }
}

#[derive(Clone)]
pub(super) enum Key {
    File,
    Recipient(Arc<OnceLock<Box<[u8]>>>),
}

#[derive(Clone, Default)]
pub(super) struct Filters {
    pub(super) file_roles: Arc<[Reference]>,
    pub(super) named: Arc<[(Name, Method, Key)]>,
    pub(super) strings: Option<Name>,
    pub(super) streams: Option<Name>,
    pub(super) embedded: Option<Name>,
}

impl Filters {
    pub(super) fn from_entries(
        encrypt: &Dict,
        entries: BudgetVec<'_, (Name, Method, Key)>,
    ) -> Result<Self> {
        let streams = selected(encrypt, "StmF", IDENTITY)?;
        let strings = selected(encrypt, "StrF", IDENTITY)?;
        let embedded = selected(encrypt, "EFF", streams)?;
        for name in [strings, streams, embedded] {
            if name != IDENTITY && !entries.iter().any(|(n, _, _)| n.as_bytes() == name) {
                return Err(Error::malformed(
                    "a selected crypt filter is undefined in /CF",
                ));
            }
        }
        Ok(Self {
            file_roles: Arc::default(),
            named: entries.into_vec().into(),
            strings: Some(strings.into()),
            streams: Some(streams.into()),
            embedded: Some(embedded.into()),
        })
    }

    pub(super) fn password(encrypt: &Dict, budget: &Budget) -> Result<Self> {
        let mut entries = BudgetVec::new(budget);
        if let Some(cf) = encrypt.get("CF") {
            let Value::Dict(cf) = cf else {
                return Err(Error::malformed("/CF must be a dictionary"));
            };
            for (name, filter) in cf.iter() {
                budget.poll()?;
                let Value::Dict(filter) = filter else {
                    return Err(Error::malformed("a /CF entry must be a dictionary"));
                };
                if name.as_bytes() == IDENTITY {
                    return Err(Error::malformed(
                        "the built-in /Identity crypt filter cannot be redefined",
                    ));
                }
                if selected(filter, "Type", b"CryptFilter")? != b"CryptFilter" {
                    return Err(Error::malformed("invalid crypt-filter dictionary /Type"));
                }
                let method = match selected(filter, "CFM", b"None")? {
                    b"None" => Method::Identity,
                    b"V2" => Method::Rc4,
                    b"AESV2" => Method::Aes128,
                    b"AESV3" => Method::Aes256,
                    _ => return Err(Error::malformed("a crypt filter's /CFM is not supported")),
                };
                entries.try_push((name.clone(), method, Key::File))?;
            }
        }
        Self::from_entries(encrypt, entries)
    }

    pub(super) fn retain_keys(&self, verified: &Self) {
        for (name, method, own) in self.named.iter() {
            if let Key::Recipient(available) = own
                && available.get().is_none()
                && let Some((_, _, Key::Recipient(confirmed))) = verified
                    .named
                    .iter()
                    .find(|(n, m, _)| n == name && m == method)
                && let Some(key) = confirmed.get()
            {
                let _ = available.set(key.clone());
            }
        }
    }

    pub(super) fn method(&self, name: &[u8]) -> Result<Method> {
        if name == IDENTITY {
            return Ok(Method::Identity);
        }
        self.named
            .iter()
            .find(|(key, _, _)| key.as_bytes() == name)
            .map(|(_, method, _)| *method)
            .ok_or_else(|| Error::malformed("a selected crypt filter is undefined in /CF"))
    }
}

fn items(value: Option<&Value>) -> &[Value] {
    match value {
        Some(Value::Array(array)) => array.as_slice(),
        Some(Value::Null) | None => &[],
        Some(value) => std::slice::from_ref(value),
    }
}

pub(crate) fn explicit<'a>(dict: &'a Dict, budget: &Budget) -> Result<Option<&'a [u8]>> {
    let filters = items(dict.get("Filter"));
    budget.check(LimitCode::StreamFilterCount, filters.len() as u64)?;
    // An unresolved reference can name a security filter. Without the source
    // revision it cannot be classified as ordinary data encoding.
    if filters
        .iter()
        .any(|item| matches!(item, Value::Reference(_)))
    {
        return Err(Error::malformed(
            "cannot select a stream crypt filter from an unresolved /Filter reference",
        ));
    }
    let crypt = |v: &Value| matches!(v, Value::Name(n) if n.as_bytes() == b"Crypt");
    if !filters.iter().any(crypt) {
        return Ok(None);
    }
    if !crypt(&filters[0]) || filters[1..].iter().any(crypt) {
        return Err(Error::malformed(
            "/Crypt must be the first and only crypt filter in the chain",
        ));
    }
    let params = items(dict.get("DecodeParms"));
    if !params.is_empty() && params.len() != filters.len() {
        return Err(Error::malformed(
            "/Crypt parameters must correspond to the filter chain",
        ));
    }
    match params.first() {
        None | Some(Value::Null) => Ok(Some(IDENTITY)),
        Some(Value::Dict(p)) => {
            if selected(p, "Type", b"CryptFilterDecodeParms")? != b"CryptFilterDecodeParms" {
                return Err(Error::malformed("invalid /Crypt decode parameter /Type"));
            }
            selected(p, "Name", IDENTITY).map(Some)
        }
        _ => Err(Error::malformed(
            "/Crypt decode parameters must be a dictionary",
        )),
    }
}

impl Handler {
    fn selection<'a>(
        &'a self,
        kind: Kind,
        dict: Option<&Dict>,
        source: Reference,
        budget: &Budget,
    ) -> Result<(Method, &'a [u8], bool)> {
        let override_name = dict
            .map(|dict| explicit(dict, budget))
            .transpose()?
            .flatten();
        let type_name = dict.and_then(|d| d.get("Type"));
        if matches!(type_name, Some(Value::Name(n)) if n.as_bytes() == b"Metadata")
            && !self.encrypt_metadata
        {
            if override_name.is_some_and(|name| name != IDENTITY) {
                return Err(Error::malformed(
                    "clear metadata requires an Identity crypt filter",
                ));
            }
            return Ok((Method::Identity, &[], false));
        }
        if override_name.is_some() && self.version < 4 {
            return Err(Error::malformed(
                "legacy encryption cannot select a stream crypt filter",
            ));
        }
        let default = match (kind, type_name) {
            (Kind::String, _) => &self.filters.strings,
            (_, _) if dict.is_some_and(|d| self.embedded_stream(source, d)) => {
                &self.filters.embedded
            }
            _ => &self.filters.streams,
        };
        let Some(name) = override_name.or_else(|| default.as_ref().map(Name::as_bytes)) else {
            return Ok((self.method(kind), &self.key, false));
        };
        if name == IDENTITY {
            return Ok((Method::Identity, &[], override_name.is_some()));
        }
        let (_, method, key) = self
            .filters
            .named
            .iter()
            .find(|(n, _, _)| n.as_bytes() == name)
            .ok_or_else(|| {
                Error::malformed("a selected stream crypt filter is undefined in /CF")
            })?;
        let key: &[u8] = match key {
            Key::File => &self.key,
            Key::Recipient(key) => key.get().map(AsRef::as_ref).ok_or(Password::Recipient)?,
        };
        Ok((*method, key, override_name.is_some()))
    }

    pub(crate) fn transform_data(
        &self,
        kind: Kind,
        dict: Option<&Dict>,
        references: (Reference, Reference),
        data: &[u8],
        budget: &Budget,
        encrypt: bool,
    ) -> Result<Vec<u8>> {
        let (source, reference) = references;
        let (method, shared_key, explicit) = self.selection(kind, dict, source, budget)?;
        if method == Method::Identity {
            return copy(data, budget);
        }
        if encrypt && self.is_recipient() && method == Method::Rc4 {
            return Err(Error::malformed(
                "pyropdf does not write RC4 recipient ciphertext; choose AES for a full save",
            ));
        }
        if matches!((method,shared_key.len()),(Method::Aes128,n) if n != 16)
            || matches!((method,shared_key.len()),(Method::Aes256,n) if n != 32)
        {
            return Err(Error::malformed(
                "the selected crypt-filter key has the wrong AES length",
            ));
        }
        let local;
        let key = if !explicit && matches!(method, Method::Rc4 | Method::Aes128) {
            local = object_key(shared_key, reference, method == Method::Aes128);
            local.as_ref()
        } else {
            shared_key
        };
        if method == Method::Rc4 {
            return crypto::rc4(key, data, budget);
        }
        if !encrypt {
            if data.len() < 16 {
                return Ok(Vec::new());
            }
            let iv = data[..16].try_into().expect("16 bytes");
            return crypto::aes_cbc_decrypt(key, &iv, &data[16..], true, budget);
        }
        let mut iv = [0; 16];
        if self.is_recipient() {
            getrandom::fill(&mut iv).map_err(|_| {
                Error::malformed("OS randomness unavailable for the AES initialization vector")
            })?;
        } else {
            let tag = [u8::from(kind == Kind::Stream)];
            iv.copy_from_slice(&crypto::sha256(&[key, &tag, data])[..16]);
        }
        let mut out = BudgetVec::new(budget);
        out.try_extend_from_slice(&iv)?;
        out.try_extend_from_slice(&crypto::aes_cbc_encrypt(key, &iv, data, true, budget)?)?;
        Ok(out.into_vec())
    }
}

fn object_key(shared: &[u8], reference: Reference, aes: bool) -> Box<[u8]> {
    let number = reference.number().to_le_bytes();
    let generation = reference.generation().to_le_bytes();
    let salt: &[u8] = if aes { b"sAlT" } else { b"" };
    let hash = crypto::md5(&[shared, &number[..3], &generation, salt]);
    Box::from(&hash[..(shared.len() + 5).min(16)])
}

#[expect(
    clippy::disallowed_methods,
    reason = "two fixed dictionary entries and budgeted filter arrays"
)]
pub(super) fn prepend_identity(dict: &mut Dict, budget: &Budget) -> Result<()> {
    let filters = items(dict.get("Filter"));
    let params = items(dict.get("DecodeParms"));
    budget.check(LimitCode::StreamFilterCount, filters.len() as u64 + 1)?;
    if !params.is_empty() && params.len() != filters.len() {
        return Err(Error::malformed(
            "filter parameters must correspond to their filters",
        ));
    }
    let mut methods = BudgetVec::new(budget);
    let mut options = BudgetVec::new(budget);
    let mut identity = Dict::new();
    identity.insert(Name::from("Name"), Value::Name(Name::from("Identity")));
    methods.try_push(Value::Name(Name::from("Crypt")))?;
    options.try_push(Value::Dict(identity))?;
    for (index, method) in filters.iter().enumerate() {
        methods.try_push(method.clone())?;
        options.try_push(params.get(index).cloned().unwrap_or(Value::Null))?;
    }
    dict.insert(
        Name::from("Filter"),
        Value::Array(methods.into_vec().into()),
    );
    dict.insert(
        Name::from("DecodeParms"),
        Value::Array(options.into_vec().into()),
    );
    Ok(())
}

/// Drop only the consumed security filter when exporting the decrypted file.
pub(crate) fn plain(bytes: crate::value::Bytes, budget: &Budget) -> Result<crate::value::Bytes> {
    let Value::Stream(mut stream) = Value::parse(&bytes, budget)? else {
        return Ok(bytes);
    };
    if explicit(stream.dict(), budget)?.is_none() {
        return Ok(bytes);
    }
    for key in ["Filter", "DecodeParms"] {
        let value = stream.dict_mut().remove(key);
        if let Some(Value::Array(mut array)) = value {
            array.remove(0);
            if !array.is_empty() {
                #[expect(
                    clippy::disallowed_methods,
                    reason = "reinserts the existing filter or parameter array"
                )]
                stream
                    .dict_mut()
                    .insert(Name::from(key), Value::Array(array));
            }
        }
    }
    let mut out = BudgetVec::new(budget);
    crate::write::value(&mut out, &Value::Stream(stream))?;
    Ok(crate::value::Bytes::new(out.into_vec()))
}

impl Handler {
    pub(crate) fn same_embedded_filter(&self, other: &Self) -> bool {
        self.filters.embedded == other.filters.embedded
    }

    /// Changes the embedded-file default while retaining authenticated keys.
    pub(crate) fn with_embedded_filter(&self, dict: &Dict) -> Result<Self> {
        if self.version < 4 {
            return Err(Error::malformed("legacy encryption cannot select /EFF"));
        }
        let default = self
            .filters
            .streams
            .as_ref()
            .map_or(IDENTITY, Name::as_bytes);
        let name = selected(dict, "EFF", default)?;
        self.filters.method(name)?;
        let mut handler = self.clone();
        handler.filters.embedded = Some(name.into());
        Ok(handler)
    }
}
