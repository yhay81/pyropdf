//! The descriptors of pyropdf-schema, which `pyropdf.typed` reads (DESIGN.md
//! section 8).

use pyo3::prelude::*;
use pyropdf_schema::{Condition, Kind, OBJECTS, object};

/// One type of a key: its kind, the object types it links to, its default,
/// and its possible values, as the model writes them.
type TypedRow = (
    &'static str,
    Vec<&'static str>,
    Option<&'static str>,
    &'static [&'static str],
);

/// One key: its name, whether it is inheritable, whether it is required
/// (`TRUE`, `FALSE`, or a predicate), and its types.
type KeyRow = (&'static str, bool, &'static str, Vec<TypedRow>);

/// The name of a kind in the model.
const fn kind_name(kind: Kind) -> &'static str {
    match kind {
        Kind::Array => "array",
        Kind::Bitmask => "bitmask",
        Kind::Boolean => "boolean",
        Kind::Date => "date",
        Kind::Dictionary => "dictionary",
        Kind::Integer => "integer",
        Kind::Matrix => "matrix",
        Kind::Name => "name",
        Kind::NameTree => "name-tree",
        Kind::Null => "null",
        Kind::Number => "number",
        Kind::NumberTree => "number-tree",
        Kind::Rectangle => "rectangle",
        Kind::Stream => "stream",
        Kind::String => "string",
        Kind::StringAscii => "string-ascii",
        Kind::StringByte => "string-byte",
        Kind::StringText => "string-text",
    }
}

const fn condition(condition: Condition) -> &'static str {
    match condition {
        Condition::Never => "FALSE",
        Condition::Always => "TRUE",
        Condition::When(predicate) => predicate,
    }
}

/// The names of every object type of the model, sorted.
#[pyfunction]
pub fn schema_names() -> Vec<&'static str> {
    OBJECTS.iter().map(|object| object.name).collect()
}

/// The keys of the object type `name`, or `None` for an unknown name.
#[pyfunction]
pub fn schema_object(name: &str) -> Option<Vec<KeyRow>> {
    let described = object(name)?;
    let keys = described.keys.iter().map(|key| {
        let types = key.types.iter().map(|typed| {
            let links = typed.links.iter().map(|link| link.target().name).collect();
            (kind_name(typed.kind), links, typed.default, typed.values)
        });
        (
            key.name,
            key.inheritable,
            condition(key.required),
            types.collect(),
        )
    });
    Some(keys.collect())
}
