//! Update field-tree arrays and split merged field/widget dictionaries.

use super::copy::copy_array;
use pyropdf_store::budget::Budget;
use pyropdf_store::value::{Array, Dict, Name, Reference, Value};
use pyropdf_store::version::Transaction;

use super::field_edit::{FIELD_KEYS, dict_at};
use super::traversal::{FIELD_TRIGGERS, Id, stale};
use crate::annotations::{AnnotationError as Error, AnnotationResult as Result};

/// Edits the array that entry `key` of object `holder` holds: an array
/// object that the entry names, or else the entry itself. An entry that is
/// no array starts empty with `create`, and stays as it is without. The
/// copies are charged to `budget`.
#[expect(
    clippy::disallowed_methods,
    reason = "replaces one fixed form key with the edited array"
)]
pub(super) fn edit_array(
    transaction: &mut Transaction,
    holder: Reference,
    key: &str,
    budget: &Budget,
    create: bool,
    edit: impl FnOnce(&mut Array),
) -> Result<()> {
    let Some(mut dict) = dict_at(transaction, holder, budget)? else {
        return Err(Error::Pdf("a form node is no dictionary".into()));
    };
    if let Some(Value::Reference(array)) = dict.get(key) {
        let array = *array;
        let object = transaction.version().get(array.number(), budget)?;
        if let Some(object) = object.as_ref()
            && object.generation() == array.generation()
            && let Value::Array(items) = object.value().as_ref()
        {
            let mut items = copy_array(items, budget)?;
            edit(&mut items);
            transaction.write(array.number(), &Value::Array(items), budget)?;
            return Ok(());
        }
    }
    let mut items = match dict.remove(key) {
        Some(Value::Array(items)) => items,
        _ if create => Array::new(),
        _ => return Ok(()),
    };
    edit(&mut items);
    dict.insert(Name::from(key), Value::Array(items));
    transaction.write(holder.number(), &Value::Dict(dict), budget)?;
    Ok(())
}

/// Returns the field that takes another widget of field `field`: the field
/// itself, or, for a field merged with its widget, a new parent that takes
/// its field entries and field triggers and its place in the field tree
/// and the calculation order of form `holder`.
#[expect(
    clippy::disallowed_methods,
    reason = "one Kids entry plus FIELD_KEYS and FIELD_TRIGGERS, both fixed arrays"
)]
pub(super) fn join(
    transaction: &mut Transaction,
    holder: Reference,
    field: Id,
    budget: &Budget,
) -> Result<Reference> {
    let reference = Reference::new(field.0, field.1);
    let Some(mut kid) = dict_at(transaction, reference, budget)? else {
        return Err(stale(field));
    };
    if !matches!(kid.get("Subtype"), Some(Value::Name(name)) if name.as_bytes() == b"Widget") {
        return Ok(reference);
    }
    let parent = transaction.allocate()?;
    let mut dict: Dict = [(
        Name::from("Kids"),
        Value::Array(Array::from(vec![Value::Reference(reference)])),
    )]
    .into_iter()
    .collect();
    for key in FIELD_KEYS {
        if let Some(value) = kid.remove(key) {
            dict.insert(Name::from(key), value);
        }
    }
    // A merged dictionary holds the actions of both; only the field's
    // triggers move to the parent.
    let actions = match kid.get("AA") {
        Some(Value::Reference(actions)) => dict_at(transaction, *actions, budget)?,
        Some(Value::Dict(actions)) => Some(actions.clone()),
        _ => None,
    };
    if let Some(mut actions) = actions {
        let triggers: Dict = FIELD_TRIGGERS
            .iter()
            .filter_map(|trigger| Some((Name::from(*trigger), actions.remove(trigger)?)))
            .collect();
        if !triggers.is_empty() {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /AA key"
            )]
            dict.insert(Name::from("AA"), Value::Dict(triggers));
        }
        if actions.is_empty() {
            kid.remove("AA");
        } else {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /AA key"
            )]
            kid.insert(Name::from("AA"), Value::Dict(actions));
        }
    }
    let grand = match kid.get("Parent") {
        Some(Value::Reference(grand)) => Some(*grand),
        _ => None,
    };
    if let Some(grand) = grand {
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /Parent key"
        )]
        dict.insert(Name::from("Parent"), Value::Reference(grand));
    }
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /Parent key"
    )]
    kid.insert(Name::from("Parent"), Value::Reference(parent));
    transaction.write(parent.number(), &Value::Dict(dict), budget)?;
    transaction.write(field.0, &Value::Dict(kid), budget)?;
    let swap = |items: &mut Array| {
        for item in items.iter_mut() {
            if *item == Value::Reference(reference) {
                *item = Value::Reference(parent);
            }
        }
    };
    let (holder_of, key) = match grand {
        Some(grand) => (grand, "Kids"),
        None => (holder, "Fields"),
    };
    edit_array(transaction, holder_of, key, budget, true, swap)?;
    edit_array(transaction, holder, "CO", budget, false, swap)?;
    Ok(parent)
}
