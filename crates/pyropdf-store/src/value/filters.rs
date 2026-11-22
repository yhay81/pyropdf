//! Resolve ordinary stream filter entries against the revision that owns their bytes.

use std::borrow::Cow;

use super::{Dict, MAX_NESTING, Reference, Stream, Value, copy_dict_for_edit, copy_for_edit};
use crate::budget::{Budget, BudgetVec, LimitCode};
use crate::index::Index;
use crate::version::{Slot, Version};
use crate::{Error, Result};

/// A lookup returns the exact generation named by a filter entry.
pub type FilterLookup<'a> = dyn Fn(Reference, &Budget) -> Result<Option<Value>> + 'a;

pub(crate) fn indexed_filter(
    index: &Index,
    reference: Reference,
    budget: &Budget,
) -> Result<Option<Value>> {
    index
        .object(reference, budget)?
        .map(|bytes| {
            budget.check(LimitCode::ObjectParseSize, bytes.len() as u64)?;
            Value::parse(&bytes, budget)
        })
        .transpose()
}

impl Version {
    /// Check filter aliases before publishing an edited or renumbered stream.
    pub(crate) fn check_stream_filters(&self, value: &Value, budget: &Budget) -> Result<()> {
        if let Value::Stream(stream) = value {
            resolved(stream.dict(), &|r, b| self.filter_object(r, b), budget)?;
        }
        Ok(())
    }

    /// Fetch the plain name, array or parameters used by a stream in this version.
    pub fn filter_object(&self, reference: Reference, budget: &Budget) -> Result<Option<Value>> {
        if self.generation(reference.number()) != Some(reference.generation()) {
            return Ok(None);
        }
        if let Some(Slot::Written(written)) = self.slot(reference.number()) {
            return copy_for_edit(&*written.value(budget)?, budget).map(Some);
        }
        if let Some(index) = self.base().file() {
            return indexed_filter(index, reference, budget);
        }
        self.get(reference.number(), budget)?
            .map(|obj| copy_for_edit(obj.value(), budget))
            .transpose()
    }
}

pub(crate) fn direct(
    value: &Value,
    key: &str,
    lookup: &FilterLookup<'_>,
    budget: &Budget,
) -> Result<Value> {
    let mut seen = BudgetVec::new(budget);
    let mut next = match value {
        Value::Reference(reference) => *reference,
        _ => return copy_for_edit(value, budget),
    };
    loop {
        budget.poll()?;
        let depth = seen.len() as u64 + 1;
        budget.check(LimitCode::ObjectDepth, depth)?;
        if depth > MAX_NESTING as u64 || seen.contains(&next) {
            return Err(Error::malformed(format!(
                "cyclic or too deeply nested /{key} reference"
            )));
        }
        seen.try_push(next)?;
        match lookup(next, budget)? {
            Some(Value::Reference(reference)) => next = reference,
            Some(value) => return Ok(value),
            None => return Err(Error::malformed(format!("unresolved /{key} reference"))),
        }
    }
}

// Only scalar parameters interpreted by the supported stream codecs or the
// security handler need dereferencing. Other entries retain their identity.
const PARAM_KEYS: &[&str] = &[
    "Name",
    "Type",
    "Predictor",
    "Colors",
    "BitsPerComponent",
    "Columns",
    "EarlyChange",
    "K",
    "EndOfLine",
    "EncodedByteAlign",
    "Rows",
    "EndOfBlock",
    "BlackIs1",
    "DamagedRowsBeforeError",
    "ColorTransform",
];

fn indirect(value: &Value) -> bool {
    match value {
        Value::Reference(_) => true,
        Value::Array(array) => array.iter().any(|v| {
            matches!(v, Value::Reference(_)) || matches!(v, Value::Dict(d) if indirect_param(d))
        }),
        Value::Dict(dict) => indirect_param(dict),
        _ => false,
    }
}

fn indirect_param(dict: &Dict) -> bool {
    PARAM_KEYS
        .iter()
        .any(|key| matches!(dict.get(key), Some(Value::Reference(_))))
}

fn member(value: &mut Value, key: &str, lookup: &FilterLookup<'_>, budget: &Budget) -> Result<()> {
    if matches!(value, Value::Reference(_)) {
        *value = direct(value, key, lookup, budget)?;
    }
    if let Value::Dict(params) = value
        && key == "DecodeParms"
    {
        for name in PARAM_KEYS {
            if let Some(v @ Value::Reference(_)) = params.get_mut(name) {
                *v = direct(v, name, lookup, budget)?;
                let valid = match *name {
                    "Type" | "Name" => matches!(v, Value::Name(_)),
                    "EndOfLine" | "EncodedByteAlign" | "EndOfBlock" | "BlackIs1" => {
                        matches!(v, Value::Boolean(_))
                    }
                    _ => matches!(v, Value::Integer(_)),
                };
                if !valid {
                    return Err(Error::malformed(format!(
                        "invalid /{name} in stream decode parameters"
                    )));
                }
            }
        }
    } else if !matches!(
        (key, &*value),
        ("Filter", Value::Name(_)) | ("DecodeParms", Value::Null)
    ) {
        return Err(Error::malformed(format!(
            "invalid /{key} stream filter entry"
        )));
    }
    Ok(())
}

pub(crate) fn has_indirect(dict: &Dict, budget: &Budget) -> Result<bool> {
    let keys = ["Filter", "DecodeParms"];
    for key in keys {
        if let Some(Value::Array(a)) = dict.get(key) {
            budget.check(LimitCode::StreamFilterCount, a.len() as u64)?;
        }
    }
    Ok(keys.iter().any(|key| dict.get(key).is_some_and(indirect)))
}

/// Cross-reference stream dictionaries cannot require their own index to interpret them.
pub(crate) fn check_xref(dict: &Dict, budget: &Budget) -> Result<()> {
    let mut values = BudgetVec::new(budget);
    for key in ["Filter", "DecodeParms"] {
        if let Some(value) = dict.get(key) {
            values.try_push(value)?;
        }
    }
    while let Some(value) = values.pop() {
        budget.poll()?;
        match value {
            Value::Array(array) => values.try_extend(array.iter())?,
            Value::Dict(params) => values.try_extend(params.iter().map(|(_, v)| v))?,
            Value::Reference(_) => {
                return Err(Error::malformed(
                    "cross-reference stream filter entries must be direct",
                ));
            }
            _ => {}
        }
    }
    Ok(())
}

/// A decoder reads the direct view. Serializers keep the original references.
pub(crate) fn resolved<'a>(
    dict: &'a Dict,
    lookup: &FilterLookup<'_>,
    budget: &Budget,
) -> Result<Cow<'a, Dict>> {
    let keys = ["Filter", "DecodeParms"];
    if matches!(dict.get("Type"),Some(Value::Name(n)) if n.as_bytes()==b"XRef") {
        check_xref(dict, budget)?;
        return Ok(Cow::Borrowed(dict));
    }
    if !has_indirect(dict, budget)? {
        return Ok(Cow::Borrowed(dict));
    }
    let mut result = copy_dict_for_edit(dict, budget)?;
    for key in keys {
        let Some(value) = result.get_mut(key) else {
            continue;
        };
        if !indirect(value) {
            continue;
        }
        *value = direct(value, key, lookup, budget)?;
        if let Value::Array(array) = value {
            budget.check(LimitCode::StreamFilterCount, array.len() as u64)?;
            for v in array.iter_mut() {
                budget.poll()?;
                member(v, key, lookup, budget)?;
            }
        } else {
            if !matches!(
                (key, &*value),
                ("Filter", Value::Name(_)) | ("DecodeParms", Value::Dict(_) | Value::Null)
            ) {
                return Err(Error::malformed(format!(
                    "invalid /{key} stream filter entry"
                )));
            }
            member(value, key, lookup, budget)?;
        }
    }
    Ok(Cow::Owned(result))
}

impl Stream {
    /// Decode the stream with filter names and parameters from this version.
    pub fn decoded_in(&self, version: &Version, budget: &Budget) -> Result<Vec<u8>> {
        let lookup = |reference, budget: &Budget| version.filter_object(reference, budget);
        match resolved(self.dict(), &lookup, budget)? {
            Cow::Borrowed(_) => self.decoded(budget),
            Cow::Owned(dict) => Self::new(dict, self.data().clone()).decoded(budget),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::budget::CancellationToken;
    use crate::value::{Bytes, Name};

    fn dictionary(bytes: &'static [u8]) -> Dict {
        let Value::Dict(dict) = Value::parse(&Bytes::from(bytes), &Budget::default()).unwrap()
        else {
            panic!("dictionary fixture");
        };
        dict
    }

    #[test]
    fn reference_resolution_charges_count_depth_memory_and_cancellation() {
        let dict = dictionary(b"<< /Filter 1 0 R >>");
        let lookup = |r: Reference, _: &Budget| {
            Ok(Some(match r.number() {
                1 => Value::Reference(Reference::new(2, 0)),
                _ => Value::Array(
                    [
                        Value::Name(Name::from("FlateDecode")),
                        Value::Name(Name::from("FlateDecode")),
                    ]
                    .into_iter()
                    .collect(),
                ),
            }))
        };
        for code in [
            LimitCode::StreamFilterCount,
            LimitCode::ObjectDepth,
            LimitCode::MemorySize,
        ] {
            let budget = Budget::default().with_limit(code, Some(1));
            assert_eq!(
                resolved(&dict, &lookup, &budget)
                    .unwrap_err()
                    .limit()
                    .unwrap()
                    .code(),
                code
            );
        }
        let token = CancellationToken::new();
        token.cancel();
        let budget = Budget::default().with_cancellation(token);
        assert_eq!(
            resolved(&dict, &lookup, &budget)
                .unwrap_err()
                .limit()
                .unwrap()
                .code(),
            LimitCode::Cancelled
        );
    }

    #[test]
    fn cross_reference_filter_entries_must_remain_direct() {
        let dict = dictionary(b"<< /Type /XRef /Filter [1 0 R] >>");
        let error = resolved(
            &dict,
            &|_, _| panic!("xref streams never follow a reference"),
            &Budget::default(),
        )
        .unwrap_err();
        assert!(error.to_string().contains("must be direct"));
    }
}
