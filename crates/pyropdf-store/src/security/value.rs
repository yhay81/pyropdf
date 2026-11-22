//! Decrypting and encrypting the strings and stream data of one object's
//! value (ISO 32000-2 section 7.6.2).

use super::{Handler, Kind};
use crate::budget::{Budget, BudgetVec};
use crate::value::{Bytes, FilterLookup, PdfString, Reference, Value};
use crate::{Error, Result, write};

/// The deepest nesting that the walk follows. Parsed values are shallower:
/// the store converts [`MAX_NESTING`](crate::value::MAX_NESTING) levels at
/// most.
const MAX_DEPTH: usize = 256;

/// Returns `value`, of the object that `reference` names, with its strings
/// and stream data decrypted. A cross-reference stream, and a metadata
/// stream when the handler leaves metadata unencrypted, keep their data.
pub fn decrypt_value(
    handler: &Handler,
    reference: Reference,
    value: &Value,
    budget: &Budget,
) -> Result<Value> {
    decrypt_value_with(handler, reference, value, &|_, _| Ok(None), budget)
}

/// Decrypt an object using the filter entries from its sealed revision.
pub(crate) fn decrypt_value_with(
    handler: &Handler,
    reference: Reference,
    value: &Value,
    lookup: &FilterLookup<'_>,
    budget: &Budget,
) -> Result<Value> {
    transform(
        handler,
        (reference, reference),
        value,
        lookup,
        budget,
        false,
    )
}

/// Returns `value`, of the object that `reference` names, with its strings
/// and stream data encrypted, as [`decrypt_value`] decrypts them.
pub fn encrypt_value(
    handler: &Handler,
    reference: Reference,
    value: &Value,
    budget: &Budget,
) -> Result<Value> {
    encrypt_value_with(
        handler,
        (reference, reference),
        value,
        &|_, _| Ok(None),
        budget,
    )
}

/// Encrypt an object for its target number using the current revision's selectors.
pub fn encrypt_value_with(
    handler: &Handler,
    references: (Reference, Reference),
    value: &Value,
    lookup: &FilterLookup<'_>,
    budget: &Budget,
) -> Result<Value> {
    transform(handler, references, value, lookup, budget, true)
}

/// Returns the bytes of a value, `bytes`, of the object that `reference`
/// names, with its strings and stream data encrypted: the bytes that a
/// file encrypted with `handler` holds for it.
pub fn encrypt_bytes(
    handler: &Handler,
    reference: Reference,
    bytes: &Bytes,
    budget: &Budget,
) -> Result<Bytes> {
    encrypt_bytes_as(
        handler,
        reference,
        reference,
        bytes,
        &|_, _| Ok(None),
        budget,
    )
}

pub(crate) fn encrypt_bytes_as(
    handler: &Handler,
    source: Reference,
    target: Reference,
    bytes: &Bytes,
    lookup: &FilterLookup<'_>,
    budget: &Budget,
) -> Result<Bytes> {
    let value = Value::parse(bytes, budget)?;
    let value = encrypt_value_with(handler, (source, target), &value, lookup, budget)?;
    let mut out = BudgetVec::new(budget);
    write::value(&mut out, &value)?;
    Ok(Bytes::new(out.into_vec()))
}

fn transform(
    handler: &Handler,
    reference: (Reference, Reference),
    value: &Value,
    lookup: &FilterLookup<'_>,
    budget: &Budget,
    encrypt: bool,
) -> Result<Value> {
    if let Value::Stream(stream) = value
        && matches!(stream.dict().get("Type"), Some(Value::Name(n)) if n.as_bytes() == b"XRef")
    {
        return Ok(value.clone());
    }
    let mut out = value.clone();
    walk(handler, reference, &mut out, lookup, budget, encrypt, 0)?;
    Ok(out)
}

fn walk(
    handler: &Handler,
    reference: (Reference, Reference),
    value: &mut Value,
    lookup: &FilterLookup<'_>,
    budget: &Budget,
    encrypt: bool,
    depth: usize,
) -> Result<()> {
    if depth > MAX_DEPTH {
        return Err(Error::malformed("a value nests too deeply to decrypt"));
    }
    let apply = |kind, data: &[u8]| {
        if encrypt {
            handler.encrypt(kind, reference.1, data, budget)
        } else {
            handler.decrypt(kind, reference.1, data, budget)
        }
    };
    match value {
        Value::String(string) => {
            let data = apply(Kind::String, string.as_bytes())?;
            *string = PdfString::new(data, string.form());
        }
        Value::Array(array) => {
            for item in array.iter_mut() {
                walk(handler, reference, item, lookup, budget, encrypt, depth + 1)?;
            }
        }
        Value::Dict(dict) => {
            for (_, item) in dict.iter_mut() {
                walk(handler, reference, item, lookup, budget, encrypt, depth + 1)?;
            }
        }
        Value::Stream(stream) => {
            for (_, item) in stream.dict_mut().iter_mut() {
                walk(handler, reference, item, lookup, budget, encrypt, depth + 1)?;
            }
            let metadata = matches!(
                stream.dict().get("Type"),
                Some(Value::Name(n)) if n.as_bytes() == b"Metadata"
            );
            let selectors = crate::value::filters::resolved(stream.dict(), lookup, budget)?;
            let prepend = encrypt
                && handler.is_recipient()
                && metadata
                && !handler.encrypts_metadata()
                && super::filters::explicit(&selectors, budget)?.is_none();
            let data = handler.transform_data(
                Kind::Stream,
                Some(&selectors),
                reference,
                stream.data(),
                budget,
                encrypt,
            )?;
            if prepend {
                if let std::borrow::Cow::Owned(selectors) = selectors {
                    *stream.dict_mut() = selectors;
                }
                super::filters::prepend_identity(stream.dict_mut(), budget)?;
            }
            stream.set_data(Bytes::new(data));
        }
        _ => {}
    }
    Ok(())
}
