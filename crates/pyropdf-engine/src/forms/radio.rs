//! Repair top-level radio widgets that were written as separate fields.

use std::collections::{HashMap, HashSet};

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::memo::Reads;
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::value::{Array, Dict, Name, PdfString, Reference, Value};
use pyropdf_store::version::Transaction;

use super::traversal::{
    FIELD_TRIGGERS, Id, MAX_EDGES, MAX_NAME_BYTES, charge, edges_error, id, insert_set, located,
    object, object_location, push, read, reserve_map,
};
use super::{RADIO, text_string};
use crate::annotations::{AnnotationError as Error, AnnotationResult as Result};

/// The keys of a radio button that a repaired group gives to its new
/// parent field.
const RADIO_KEYS: [&str; 7] = ["FT", "T", "TU", "TM", "Ff", "V", "DV"];

/// Where the form's `/Fields` array is written: as an object of its own,
/// in the form dictionary's object, or in the catalog's.
enum Holder {
    Array(Id),
    Form(Id),
    Catalog(Id),
}

/// Resolve aliases to the actual object that owns the form or fields array.
fn holder(reads: &mut Reads<'_>) -> Result<Option<(Holder, Array)>> {
    let Some(&Value::Reference(root)) = reads.trailer().get("Root") else {
        return Ok(None);
    };
    let Some((Value::Dict(catalog), Some(root))) = located(reads, Some(&Value::Reference(root)))?
    else {
        return Ok(None);
    };
    let Some((Value::Dict(form), form_object)) = located(reads, catalog.get("AcroForm"))? else {
        return Ok(None);
    };
    if matches!(form.get("Type"), Some(Value::Name(kind)) if matches!(kind.as_bytes(), b"Page" | b"Pages" | b"Catalog"))
    {
        return Err(Error::Malformed(
            "the form aliases a page, page tree, or catalog".to_owned(),
        ));
    }
    let owner = form_object.map_or(Holder::Catalog(root), Holder::Form);
    Ok(match located(reads, form.get("Fields"))? {
        Some((Value::Array(fields), Some(object))) => Some((Holder::Array(object), fields)),
        Some((Value::Array(fields), None)) => Some((owner, fields)),
        _ => None,
    })
}

/// A radio group to repair: the name its buttons share and their objects.
type Group = (PdfString, Vec<(Reference, Reference)>);

/// Returns the top-level radio buttons that share a name, each a field
/// merged with its widget, by name in `/Fields` order.
#[expect(
    clippy::disallowed_methods,
    reason = "reserve_map charges and fallibly reserves the slot before insertion"
)]
fn radio_groups(reads: &mut Reads<'_>, fields: &Array) -> Result<Vec<Group>> {
    if fields.len() > MAX_EDGES {
        return Err(edges_error());
    }
    let budget = reads.budget();
    let mut groups: Vec<Group> = Vec::new();
    let mut positions: HashMap<String, usize> = HashMap::new();
    let mut totals = (0, 0);
    let mut seen = HashSet::new();
    for item in fields.iter() {
        budget.poll()?;
        let Value::Reference(reference) = item else {
            continue;
        };
        let Some((actual, value)) = object_location(reads, *reference)? else {
            continue;
        };
        if !insert_set(&mut seen, actual, budget)? {
            continue;
        }
        let actual = Reference::new(actual.0, actual.1);
        let Value::Dict(field) = &*value else {
            continue;
        };
        let is_name = |value: Option<Value>, name: &[u8]| matches!(value, Some(Value::Name(value)) if value.as_bytes() == name);
        let radio = is_name(read(reads, field.get("FT"))?, b"Btn")
            && matches!(read(reads, field.get("Ff"))?, Some(Value::Integer(flags)) if flags & RADIO != 0);
        if !radio
            || !is_name(read(reads, field.get("Subtype"))?, b"Widget")
            || field.get("Kids").is_some()
        {
            continue;
        }
        let Some(Value::String(title)) = read(reads, field.get("T"))? else {
            continue;
        };
        charge(
            &mut totals.0,
            title.as_bytes().len(),
            MAX_NAME_BYTES,
            "encoded field name",
        )?;
        budget.charge(
            LimitCode::MemorySize,
            title.as_bytes().len().saturating_mul(4) as u64,
        )?;
        let decoded = text_string(title.as_bytes());
        charge(
            &mut totals.1,
            decoded.len(),
            MAX_NAME_BYTES,
            "decoded field name",
        )?;
        match positions.get(decoded.as_str()) {
            Some(&at) => push(&mut groups[at].1, (*reference, actual), budget)?,
            None => {
                reserve_map(&mut positions, budget)?;
                positions.insert(decoded, groups.len());
                let mut members = Vec::new();
                push(&mut members, (*reference, actual), budget)?;
                push(&mut groups, (title, members), budget)?;
            }
        }
    }
    groups.retain(|(_, members)| members.len() > 1);
    Ok(groups)
}
/// Gathers the top-level radio buttons that share a name, each a field
/// merged with its widget, under one new parent field of that name, which
/// takes the place of the first in `/Fields`; returns how many groups it
/// made. Viewers and the walk otherwise see only one of them.
#[expect(
    clippy::disallowed_methods,
    reason = "field and action keys come from fixed lists; replaced entries are charged through reserve_map"
)]
pub fn repair_radio_groups(transaction: &mut Transaction, budget: &Budget) -> Result<u32> {
    let before = transaction.snapshot();
    let mut reads = Reads::new(&before, budget);
    let Some((owner, fields)) = holder(&mut reads)? else {
        return Ok(0);
    };
    let groups = radio_groups(&mut reads, &fields)?;
    if groups.is_empty() {
        return Ok(0);
    }
    let mut replaced: HashMap<Reference, Option<Reference>> = HashMap::new();
    for (title, members) in &groups {
        let first = object(&mut reads, id(members[0].1))?;
        let Some(Value::Dict(first)) = first.as_deref() else {
            continue;
        };
        budget.charge(
            LimitCode::MemorySize,
            (32 * size_of::<(Name, Value)>()) as u64,
        )?;
        let mut parent = Dict::new();
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /FT key"
        )]
        parent.insert(Name::from("FT"), Value::Name(Name::from("Btn")));
        budget.charge(LimitCode::MemorySize, title.as_bytes().len() as u64)?;
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /T key"
        )]
        parent.insert(Name::from("T"), Value::String(title.clone()));
        let mut kids = BudgetVec::new(budget);
        for &(entry, _) in members {
            kids.try_push(Value::Reference(entry))?;
        }
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /Kids key"
        )]
        parent.insert(
            Name::from("Kids"),
            Value::Array(Array::from(kids.into_vec())),
        );
        for key in ["Ff", "TU", "TM", "DV"] {
            if let Some(value) = first.get(key) {
                parent.insert(Name::from(key), copy_for_edit(value, budget)?);
            }
        }
        // The group's value is the first value that one of its buttons
        // holds.
        for &(_, member) in members {
            if let Some(Value::Dict(member)) = object(&mut reads, id(member))?.as_deref()
                && let Some(value) = member.get("V")
                && read(&mut reads, Some(value))?.is_some()
            {
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /V key"
                )]
                parent.insert(Name::from("V"), copy_for_edit(value, budget)?);
                break;
            }
        }
        let mut field_actions = Dict::new();
        for &(_, member) in members {
            if let Some(Value::Dict(member)) = object(&mut reads, id(member))?.as_deref()
                && let Some(Value::Dict(mut actions)) = read(&mut reads, member.get("AA"))?
            {
                for trigger in FIELD_TRIGGERS {
                    if field_actions.get(trigger).is_none()
                        && let Some(action) = actions.remove(trigger)
                    {
                        field_actions.insert(Name::from(trigger), action);
                    }
                }
            }
        }
        if !field_actions.is_empty() {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /AA key"
            )]
            parent.insert(Name::from("AA"), Value::Dict(field_actions));
        }
        let reference = transaction.allocate()?;
        transaction.write(reference.number(), &Value::Dict(parent), budget)?;
        for &(_, member) in members {
            let Some(value) = object(&mut reads, id(member))? else {
                continue;
            };
            let Value::Dict(mut dict) = copy_for_edit(&value, budget)? else {
                continue;
            };
            if matches!(dict.get("Type"), Some(Value::Name(kind)) if matches!(kind.as_bytes(), b"Page" | b"Pages" | b"Catalog"))
            {
                return Err(Error::Malformed(
                    "a radio widget aliases a page, page tree, or catalog".to_owned(),
                ));
            }
            for key in RADIO_KEYS {
                dict.remove(key);
            }
            if let Some(Value::Dict(mut actions)) = read(&mut reads, dict.get("AA"))? {
                for trigger in FIELD_TRIGGERS {
                    actions.remove(trigger);
                }
                if actions.is_empty() {
                    dict.remove("AA");
                } else {
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "this call inserts only the fixed /AA key"
                    )]
                    dict.insert(Name::from("AA"), Value::Dict(actions));
                }
            }
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /Parent key"
            )]
            dict.insert(Name::from("Parent"), Value::Reference(reference));
            transaction.write(member.number(), &Value::Dict(dict), budget)?;
            reserve_map(&mut replaced, budget)?;
            replaced.insert(member, (member == members[0].1).then_some(reference));
        }
    }
    let mut changed_fields = BudgetVec::new(budget);
    let mut emitted = HashSet::new();
    for item in fields.iter() {
        let replacement = match item {
            Value::Reference(reference) => {
                let actual = object_location(&mut reads, *reference)?
                    .map(|(id, _)| Reference::new(id.0, id.1));
                match actual.and_then(|actual| {
                    replaced
                        .get(&actual)
                        .map(|replacement| (actual, replacement))
                }) {
                    Some((actual, Some(parent)))
                        if insert_set(&mut emitted, id(actual), budget)? =>
                    {
                        Some(Value::Reference(*parent))
                    }
                    Some(_) => None,
                    None => Some(copy_for_edit(item, budget)?),
                }
            }
            _ => Some(copy_for_edit(item, budget)?),
        };
        if let Some(value) = replacement {
            changed_fields.try_push(value)?;
        }
    }
    let fields = changed_fields.into_vec();
    let fields = Value::Array(Array::from(fields));
    let (number, value) = match owner {
        Holder::Array((number, _)) => (number, fields),
        Holder::Form(form) => {
            let Some(value) = object(&mut reads, form)? else {
                return Ok(0);
            };
            let Value::Dict(mut dict) = copy_for_edit(&value, budget)? else {
                return Ok(0);
            };
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /Fields key"
            )]
            dict.insert(Name::from("Fields"), fields);
            (form.0, Value::Dict(dict))
        }
        Holder::Catalog((number, generation)) => {
            let catalog = object(&mut reads, (number, generation))?;
            let Some(catalog) = catalog else {
                return Ok(0);
            };
            let Value::Dict(mut catalog) = copy_for_edit(&catalog, budget)? else {
                return Ok(0);
            };
            let Some(Value::Dict(form)) = catalog.get_mut("AcroForm") else {
                return Ok(0);
            };
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /Fields key"
            )]
            form.insert(Name::from("Fields"), fields);
            (number, Value::Dict(catalog))
        }
    };
    transaction.write(number, &value, budget)?;
    Ok(u32::try_from(groups.len()).unwrap_or(u32::MAX))
}
