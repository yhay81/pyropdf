//! ISO 32000-2 §§7.4.10 and 7.6.6. Explicit stream filters use the
//! filter's shared key directly; implicit legacy encryption derives an
//! object key. Keep the declaration in the working stream dictionary.

use std::sync::{Arc, OnceLock};

use super::{Handler, Kind, Method, copy, crypto};
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::value::{Dict, FilterLookup, Name, Reference, Value};
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
                    b"AESV4" => {
                        if !matches!(encrypt.get("V"), Some(Value::Integer(6))) {
                            return Err(Error::malformed(
                                "AESV4 requires standard security handler V6 and R7",
                            ));
                        }
                        Method::AesGcm
                    }
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
            || matches!((method,shared_key.len()),(Method::Aes256 | Method::AesGcm,n) if n != 32)
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
        if method == Method::AesGcm {
            return if encrypt {
                crypto::gcm_encrypt(key, data, budget)
            } else {
                crypto::gcm_decrypt(key, data, budget)
            };
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
        let padding = 16 - data.len() % 16;
        let mut out = BudgetVec::new(budget);
        out.try_reserve_exact(16 + data.len() + padding)?;
        out.try_extend_from_slice(&iv)?;
        crypto::aes_cbc_encrypt_into(key, &iv, data, true, &mut out, budget)?;
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

#[expect(clippy::disallowed_methods, reason = "one fixed /Name parameter")]
pub(super) fn prepend_identity(dict: &mut Dict, budget: &Budget) -> Result<()> {
    let mut identity = Dict::new();
    identity.insert(Name::from("Name"), Value::Name(Name::from("Identity")));
    prepend_crypt(
        dict,
        Value::Name(Name::from("Crypt")),
        Some(Value::Dict(identity)),
        budget,
    )
}

#[expect(
    clippy::disallowed_methods,
    reason = "two fixed dictionary entries with budgeted arrays"
)]
fn prepend_crypt(
    dict: &mut Dict,
    crypt: Value,
    first: Option<Value>,
    budget: &Budget,
) -> Result<()> {
    let filters = items(dict.get("Filter"));
    let params = items(dict.get("DecodeParms"));
    budget.check(LimitCode::StreamFilterCount, filters.len() as u64 + 1)?;
    if !params.is_empty() && params.len() != filters.len() {
        return Err(Error::malformed(
            "filter parameters must correspond to their filters",
        ));
    }
    let mut methods = BudgetVec::new(budget);
    methods.try_push(crypt)?;
    for method in filters {
        budget.poll()?;
        methods.try_push(crate::value::copy_for_edit(method, budget)?)?;
    }
    if first.is_some() || !params.is_empty() {
        let mut options = BudgetVec::new(budget);
        options.try_push(first.unwrap_or(Value::Null))?;
        for index in 0..filters.len() {
            options.try_push(crate::value::copy_for_edit(
                params.get(index).unwrap_or(&Value::Null),
                budget,
            )?)?;
        }
        dict.insert(
            Name::from("DecodeParms"),
            Value::Array(options.into_vec().into()),
        );
    }
    dict.insert(
        Name::from("Filter"),
        Value::Array(methods.into_vec().into()),
    );
    Ok(())
}

/// Drop only the consumed security filter when exporting the decrypted file.
pub(crate) fn plain(
    bytes: crate::value::Bytes,
    lookup: &FilterLookup<'_>,
    budget: &Budget,
) -> Result<crate::value::Bytes> {
    let mut value = Value::parse(&bytes, budget)?;
    if !remove_explicit(&mut value, lookup, budget)? {
        return Ok(bytes);
    }
    let mut out = BudgetVec::new(budget);
    crate::write::value(&mut out, &value)?;
    Ok(crate::value::Bytes::new(out.into_vec()))
}

/// Keep data filters and any shared parameters when removing document encryption.
pub(crate) fn remove_explicit(
    value: &mut Value,
    lookup: &FilterLookup<'_>,
    budget: &Budget,
) -> Result<bool> {
    let Value::Stream(stream) = value else {
        return Ok(false);
    };
    if explicit(
        crate::value::filters::resolved(stream.dict(), lookup, budget)?.as_ref(),
        budget,
    )?
    .is_none()
    {
        return Ok(false);
    }
    for key in ["Filter", "DecodeParms"] {
        let value = stream.dict_mut().remove(key);
        if let Some(value) = value
            && let Value::Array(mut array) =
                crate::value::filters::direct(&value, key, lookup, budget)?
        {
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
    Ok(true)
}

impl Handler {
    /// An edit to a shared filter or file specification can change the encryption
    /// of an otherwise untouched stream. Its original plaintext must be re-encrypted.
    pub fn changed_streams(
        &self,
        version: &crate::version::Version,
        index: &crate::index::Index,
        old: &Handler,
        selector_changed: bool,
        budget: &Budget,
    ) -> Result<Vec<u32>> {
        let include_direct = selector_changed
            || old.distinguishes_embedded_files()
            || self.distinguishes_embedded_files();
        let mut numbers = BudgetVec::new(budget);
        for (number, entry) in index.entries() {
            budget.poll()?;
            if version.slot(number).is_some()
                || !matches!(entry.location(), crate::index::Location::Offset(_))
            {
                continue;
            }
            let reference = Reference::new(number, entry.generation());
            let Some(dict) = index.stream_dictionary(reference, include_direct, budget)? else {
                continue;
            };
            let role_changed = old.embedded_stream(reference, &dict)
                != self.embedded_stream(reference, &dict)
                || selector_changed && self.embedded_stream(reference, &dict);
            if !role_changed && !crate::value::filters::has_indirect(&dict, budget)? {
                continue;
            }
            let old_filters = crate::value::filters::resolved(
                &dict,
                &|r, b| crate::value::filters::indexed_filter(index, r, b),
                budget,
            )?;
            let new_filters = crate::value::filters::resolved(
                &dict,
                &|r, b| version.filter_object(r, b),
                budget,
            )?;
            let old_name = explicit(&old_filters, budget)?;
            let new_name = explicit(&new_filters, budget)?;
            if old_name != new_name || role_changed && new_name.is_none() {
                numbers.try_push(number)?;
            }
        }
        numbers.sort_unstable();
        Ok(numbers.into_vec())
    }
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

fn first_declaration(
    dict: &Dict,
    key: &str,
    lookup: &FilterLookup<'_>,
    budget: &Budget,
) -> Result<Option<Value>> {
    let Some(value) = dict.get(key) else {
        return Ok(None);
    };
    let resolved = crate::value::filters::direct(value, key, lookup, budget)?;
    let first = match &resolved {
        Value::Array(array) => array.get(0),
        Value::Null => None,
        _ => Some(value),
    };
    first
        .filter(|v| !matches!(v, Value::Null))
        .map(|v| crate::value::copy_for_edit(v, budget))
        .transpose()
}

impl crate::value::Stream {
    /// Replace a decoded payload while preserving its revision's security declaration.
    #[expect(clippy::disallowed_methods, reason = "one fixed filter entry")]
    pub fn replace_encoded_data_with(
        &mut self,
        data: crate::value::Bytes,
        flate: bool,
        lookup: &FilterLookup<'_>,
        budget: &Budget,
    ) -> Result<()> {
        let mut options = Dict::new();
        if flate {
            options.insert(Name::from("Filter"), Value::Name(Name::from("FlateDecode")));
        }
        self.replace_encoded_options_with(data, options, lookup, budget)
    }

    /// An unchanged data-filter tail keeps the original shared filter declarations.
    /// A different tail still encrypts for the original audience.
    #[expect(clippy::disallowed_methods, reason = "two fixed filter entries")]
    pub(crate) fn replace_encoded_options_with(
        &mut self,
        data: crate::value::Bytes,
        mut options: Dict,
        lookup: &FilterLookup<'_>,
        budget: &Budget,
    ) -> Result<()> {
        let resolved = crate::value::filters::resolved(self.dict(), lookup, budget)?;
        if explicit(&resolved, budget)?.is_some() {
            let previous = items(resolved.get("Filter"));
            let previous_params = items(resolved.get("DecodeParms"));
            let filters = items(options.get("Filter"));
            let params = items(options.get("DecodeParms"));
            if &previous[1..] == filters
                && (0..filters.len())
                    .all(|i| same_parameters(previous_params.get(i + 1), params.get(i)))
            {
                self.set_data(data);
                return Ok(());
            }
            let crypt = first_declaration(self.dict(), "Filter", lookup, budget)?
                .expect("the validated leading Crypt filter");
            let first = first_declaration(self.dict(), "DecodeParms", lookup, budget)?;
            if filters.is_empty() {
                options.insert(Name::from("Filter"), crypt);
                if let Some(first) = first {
                    options.insert(Name::from("DecodeParms"), first);
                }
            } else {
                prepend_crypt(&mut options, crypt, first, budget)?;
            }
        }
        let dict = self.dict_mut();
        for name in ["Filter", "DecodeParms"] {
            if let Some(value) = options.remove(name) {
                dict.insert(Name::from(name), value);
            } else {
                dict.remove(name);
            }
        }
        self.set_data(data);
        Ok(())
    }
}

/// Recognize differently ordered standard predictor parameters in bounded time.
fn same_parameters(left: Option<&Value>, right: Option<&Value>) -> bool {
    let left = left.unwrap_or(&Value::Null);
    let right = right.unwrap_or(&Value::Null);
    if left == right {
        return true;
    }
    let (Value::Dict(left), Value::Dict(right)) = (left, right) else {
        return false;
    };
    left.len() == right.len()
        && right.len() <= 5
        && right.iter().all(|(k, v)| left.get(k.as_bytes()) == Some(v))
}
