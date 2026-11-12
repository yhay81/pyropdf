//! Read-only inspection of form field metadata and defaults.

use pyropdf_store::budget::Budget;
use pyropdf_store::memo::Reads;
use pyropdf_store::value::Value;
use pyropdf_store::version::Version;

use crate::annotations::AnnotationResult as Result;
use crate::forms::traversal::{Form, inherited};
use crate::forms::values::{Id, MAX_VALUE_BYTES, decode, object, own, read, stale, text};

/// Reads field `id`'s label (`/TU`), maximum length (inherited `/MaxLen`,
/// unless negative), and default value (inherited `/DV`, a name or a text
/// string), all from one version.
pub fn field_details(
    version: &Version,
    budget: &Budget,
    form: &Form,
    id: Id,
) -> Result<(Option<String>, Option<i64>, Option<String>)> {
    form.field(id).ok_or_else(|| stale(id))?;
    let mut reads = Reads::new(version, budget);
    let mut totals = (0, 0);
    let field = object(&mut reads, id)?;
    let label = match field.as_deref() {
        Some(Value::Dict(dict)) => match own(&mut reads, dict, "TU")? {
            Some(label) => text(
                &mut reads,
                label,
                &mut totals,
                MAX_VALUE_BYTES,
                "field label",
            )?,
            None => None,
        },
        _ => None,
    };
    let length = inherited(&mut reads, id, "MaxLen")?;
    let max_length = match read(&mut reads, length.as_ref())? {
        Some(Value::Integer(length)) if length >= 0 => Some(length),
        _ => None,
    };
    let default = inherited(&mut reads, id, "DV")?;
    let default = match read(&mut reads, default.as_ref())? {
        Some(Value::Name(name)) => Some(String::from_utf8_lossy(name.as_bytes()).into_owned()),
        Some(Value::String(encoded)) => Some(decode(
            &encoded,
            &mut totals,
            MAX_VALUE_BYTES,
            "field default value",
            budget,
        )?),
        _ => None,
    };
    Ok((label, max_length, default))
}
