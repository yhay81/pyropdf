//! Decrypting and encrypting the strings and stream data of one object's
//! value (ISO 32000-2 section 7.6.2).

use super::{Handler, Kind};
use crate::budget::Budget;
use crate::value::{Bytes, PdfString, Reference, Value};
use crate::{Error, Result};

/// The deepest nesting that the walk follows. Parsed values are shallower:
/// hayro-syntax reads 64 levels at most.
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
    transform(handler, reference, value, budget, false)
}

/// Returns `value`, of the object that `reference` names, with its strings
/// and stream data encrypted, as [`decrypt_value`] decrypts them.
pub fn encrypt_value(
    handler: &Handler,
    reference: Reference,
    value: &Value,
    budget: &Budget,
) -> Result<Value> {
    transform(handler, reference, value, budget, true)
}

fn transform(
    handler: &Handler,
    reference: Reference,
    value: &Value,
    budget: &Budget,
    encrypt: bool,
) -> Result<Value> {
    if let Value::Stream(stream) = value
        && matches!(stream.dict().get("Type"), Some(Value::Name(n)) if n.as_bytes() == b"XRef")
    {
        return Ok(value.clone());
    }
    let mut out = value.clone();
    walk(handler, reference, &mut out, budget, encrypt, 0)?;
    Ok(out)
}

fn walk(
    handler: &Handler,
    reference: Reference,
    value: &mut Value,
    budget: &Budget,
    encrypt: bool,
    depth: usize,
) -> Result<()> {
    if depth > MAX_DEPTH {
        return Err(Error::malformed("a value nests too deeply to decrypt"));
    }
    let apply = |kind, data: &[u8]| {
        if encrypt {
            handler.encrypt(kind, reference, data, budget)
        } else {
            handler.decrypt(kind, reference, data, budget)
        }
    };
    match value {
        Value::String(string) => {
            let data = apply(Kind::String, string.as_bytes())?;
            *string = PdfString::new(data, string.form());
        }
        Value::Array(array) => {
            for item in array.iter_mut() {
                walk(handler, reference, item, budget, encrypt, depth + 1)?;
            }
        }
        Value::Dict(dict) => {
            for (_, item) in dict.iter_mut() {
                walk(handler, reference, item, budget, encrypt, depth + 1)?;
            }
        }
        Value::Stream(stream) => {
            for (_, item) in stream.dict_mut().iter_mut() {
                walk(handler, reference, item, budget, encrypt, depth + 1)?;
            }
            let metadata = matches!(
                stream.dict().get("Type"),
                Some(Value::Name(n)) if n.as_bytes() == b"Metadata"
            );
            if !(metadata && !handler.encrypts_metadata()) {
                let data = apply(Kind::Stream, stream.data())?;
                stream.set_data(Bytes::new(data));
            }
        }
        _ => {}
    }
    Ok(())
}
