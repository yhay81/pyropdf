//! Register imported form fields and merge their default resources.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use pyropdf_store::budget::{Budget, LimitCode};
use pyropdf_store::memo::Reads;
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::value::{Array, Dict, Name, PdfString, Reference, Value};
use pyropdf_store::version::{Transaction, Version};

use super::defaults::{self, Joined, Renames, Slot};
use super::field_edit::dict_at;
use super::traversal::{
    MAX_NAME_BYTES, id, inherited, object, omitted, push, read, reserve_map, text,
};
use super::tree_edit::edit_array;
use crate::annotations::{AnnotationError as Error, AnnotationResult as Result};
use crate::display::encode_text;

/// The form defaults that an import of pages brings, planned against the
/// receiving form before the import copies the source values they name.
#[derive(Default)]
pub struct ImportDefaults {
    /// The source values for the import to copy, one per place.
    pub values: Vec<Value>,
    /// Where the copy of each value goes.
    places: Vec<Place>,
    /// The renames of the source default resources.
    renames: Renames,
    /// The source form's `/DA`, after the renames.
    appearance: Option<Vec<u8>>,
    /// The source form's `/Q`.
    quadding: i64,
    /// Whether the source form asks for appearances to be drawn again.
    need_appearances: bool,
    /// The receiving form's `/DA`, `/Q`, and `/NeedAppearances`, or `None`
    /// when the import creates the form.
    receiving: Option<(Option<Vec<u8>>, i64, bool)>,
}

impl ImportDefaults {
    /// Whether default appearance strings follow renamed resources, so
    /// that the import must return every object it copies.
    pub fn renames(&self) -> bool {
        !self.renames.is_empty()
    }
}

/// Where the copy of a source value of [`ImportDefaults`] goes.
enum Place {
    /// An entry of the form: `/DR`, `/DA`, or `/Q` of a new form, or the
    /// `/DR` of a form whose own is no dictionary.
    Form(Name),
    /// A category of the receiving `/DR`, or with a name a resource of it.
    Resource(Name, Option<Name>),
}

/// A form dictionary with the object that holds it, `None` for the
/// catalog.
type Located = (Option<Reference>, Dict);

/// Returns the catalog of `reads` with its reference, and its form.
fn located_form(
    reads: &mut Reads<'_>,
    budget: &Budget,
) -> Result<(Reference, Dict, Option<Located>)> {
    let Some(Value::Reference(root)) = reads.trailer().get("Root").cloned() else {
        return Err(Error::Pdf("the document has no catalog".into()));
    };
    let catalog = match object(reads, id(root))?.as_deref() {
        Some(catalog @ Value::Dict(_)) => copy_for_edit(catalog, budget)?,
        _ => return Err(Error::Pdf("the document has no catalog".into())),
    };
    let Value::Dict(catalog) = catalog else {
        unreachable!("a copied dictionary remains a dictionary");
    };
    let form = match catalog.get("AcroForm") {
        Some(&Value::Reference(reference)) => match object(reads, id(reference))?.as_deref() {
            Some(form @ Value::Dict(_)) => match copy_for_edit(form, budget)? {
                Value::Dict(form) => Some((Some(reference), form)),
                _ => unreachable!("a copied dictionary remains a dictionary"),
            },
            _ => None,
        },
        Some(form @ Value::Dict(_)) => match copy_for_edit(form, budget)? {
            Value::Dict(form) => Some((None, form)),
            _ => unreachable!("a copied dictionary remains a dictionary"),
        },
        _ => None,
    };
    Ok((root, catalog, form))
}

/// Returns the `/DA`, the `/Q`, and the `/NeedAppearances` of `form`: a
/// `/Q` other than 0, 1, or 2 is 0.
#[expect(
    clippy::disallowed_methods,
    reason = "one copy of the DA string already copied under the read budget"
)]
fn form_defaults(reads: &mut Reads<'_>, form: &Dict) -> Result<(Option<Vec<u8>>, i64, bool)> {
    let appearance = match read(reads, form.get("DA"))? {
        Some(Value::String(appearance)) => Some(appearance.as_bytes().to_vec()),
        _ => None,
    };
    let quadding = match read(reads, form.get("Q"))? {
        Some(Value::Integer(quadding @ 0..=2)) => quadding,
        _ => 0,
    };
    let need = read(reads, form.get("NeedAppearances"))? == Some(Value::Boolean(true));
    Ok((appearance, quadding, need))
}

/// Plans the form defaults that an import from `source` brings into the
/// form of `receiving`, as pylopdf's `AcroFormImport::prepare` does.
///
/// A new form takes the source `/DR`, `/DA`, and `/Q`. An existing form
/// whose `/DR` is no dictionary takes the source one; otherwise the
/// source resources join it ([`defaults::merge`]), and imported default
/// appearance strings follow their renames.
#[expect(
    clippy::disallowed_methods,
    reason = "fixed form keys plus one value and place per resource returned by the budgeted resource join"
)]
pub fn plan_imported_defaults(
    receiving: &Version,
    source: &Version,
    budget: &Budget,
) -> Result<ImportDefaults> {
    let mut plan = ImportDefaults::default();
    let mut sources = Reads::new(source, budget);
    let root = sources.trailer().get("Root").cloned();
    let Some(Value::Dict(catalog)) = read(&mut sources, root.as_ref())? else {
        return Ok(plan);
    };
    let Some(Value::Dict(source_form)) = read(&mut sources, catalog.get("AcroForm"))? else {
        return Ok(plan);
    };
    (plan.appearance, plan.quadding, plan.need_appearances) =
        form_defaults(&mut sources, &source_form)?;
    let mut reads = Reads::new(receiving, budget);
    let Some((_, form)) = located_form(&mut reads, budget)?.2 else {
        for key in ["DR", "DA", "Q"] {
            if let Some(value) = source_form.get(key)
                && !omitted(&mut sources, value)?
            {
                plan.values.push(value.clone());
                plan.places.push(Place::Form(Name::from(key)));
            }
        }
        return Ok(plan);
    };
    plan.receiving = Some(form_defaults(&mut reads, &form)?);
    if let Some(value) = source_form.get("DR")
        && !omitted(&mut sources, value)?
    {
        if let Slot::Found(_, Value::Dict(resources)) = defaults::slot(&mut reads, &form, b"DR")? {
            if let Some(Value::Dict(source_resources)) = read(&mut sources, Some(value))? {
                let (joined, renames) =
                    defaults::merge(&mut reads, &resources, &mut sources, &source_resources)?;
                for Joined {
                    category,
                    name,
                    value,
                } in joined
                {
                    plan.values.push(value);
                    plan.places.push(Place::Resource(category, name));
                }
                plan.renames = renames;
            }
        } else {
            plan.values.push(value.clone());
            plan.places.push(Place::Form(Name::from("DR")));
        }
    }
    if let Some(appearance) = &mut plan.appearance
        && let Some(renamed) = defaults::rewrite(appearance, &plan.renames)
    {
        *appearance = renamed;
    }
    Ok(plan)
}

/// Registers `fields`, the copies of the top-level fields that an import
/// of pages brought, in the receiving form, and `order` in its calculation
/// order, as pylopdf's `AcroFormImport` does. A form that is no dictionary
/// gives way to a new one. An imported partial name that a receiving
/// top-level field has takes the smallest `+N` suffix that no top-level
/// field of either has, shared by the imported fields of that name.
///
/// `values` are the copies of the values of `defaults`, and `copies`
/// every object that the import wrote when `defaults` renames resources.
/// A copied default appearance string follows the renames, and a copied
/// top-level field whose `/DA` or `/Q` the form would otherwise give it
/// differently takes the source form's.
#[expect(
    clippy::disallowed_methods,
    reason = "one placed entry per budgeted import result, at most DA and Q overrides per field, and one reference per imported field"
)]
pub fn register_imported(
    transaction: &mut Transaction,
    fields: &[Reference],
    order: &[Reference],
    defaults: &ImportDefaults,
    values: &[Value],
    copies: &[Reference],
    budget: &Budget,
) -> Result<()> {
    if fields.is_empty() {
        return Ok(());
    }
    let version = transaction.snapshot();
    let mut reads = Reads::new(&version, budget);
    let (root, mut catalog, form) = located_form(&mut reads, budget)?;
    let references = |items: &[Reference]| -> Result<Array> {
        let mut out = Vec::new();
        for &item in items {
            push(&mut out, Value::Reference(item), budget)?;
        }
        Ok(Array::from(out))
    };
    let placed = || defaults.places.iter().zip(values);
    let Some((holder, form)) = form else {
        let mut form = Dict::new();
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /Fields key"
        )]
        form.insert(Name::from("Fields"), Value::Array(references(fields)?));
        for (place, value) in placed() {
            if let Place::Form(key) = place {
                form.insert(key.clone(), value.clone());
            }
        }
        if defaults.need_appearances {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /NeedAppearances key"
            )]
            form.insert(Name::from("NeedAppearances"), Value::Boolean(true));
        }
        if !order.is_empty() {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /CO key"
            )]
            form.insert(Name::from("CO"), Value::Array(references(order)?));
        }
        let reference = transaction.allocate()?;
        transaction.write(reference.number(), &Value::Dict(form), budget)?;
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /AcroForm key"
        )]
        catalog.insert(Name::from("AcroForm"), Value::Reference(reference));
        transaction.write(root.number(), &Value::Dict(catalog), budget)?;
        return Ok(());
    };
    rename_imported(transaction, &mut reads, &form, fields, budget)?;
    let holder = match holder {
        Some(holder) => holder,
        None => {
            let reference = transaction.allocate()?;
            transaction.write(reference.number(), &Value::Dict(form), budget)?;
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /AcroForm key"
            )]
            catalog.insert(Name::from("AcroForm"), Value::Reference(reference));
            transaction.write(root.number(), &Value::Dict(catalog), budget)?;
            reference
        }
    };
    let dr = Name::from("DR");
    let placed = placed().collect::<Vec<_>>();
    // The resources of one category join it together.
    for group in placed.chunk_by(|(a, _), (b, _)| match (a, b) {
        (Place::Resource(a, Some(_)), Place::Resource(b, Some(_))) => a == b,
        _ => false,
    }) {
        let (path, entries) = match group {
            [(Place::Form(key), value)] => (vec![], vec![(key, *value)]),
            [(Place::Resource(category, None), value)] => (vec![&dr], vec![(category, *value)]),
            _ => {
                let mut entries = Vec::with_capacity(group.len());
                let mut category = &dr;
                for (place, value) in group {
                    if let Place::Resource(of, Some(name)) = place {
                        category = of;
                        entries.push((name, *value));
                    }
                }
                (vec![&dr, category], entries)
            }
        };
        edit_dict(transaction, holder, &path, budget, |dict| {
            for (key, value) in entries {
                dict.insert(key.clone(), value.clone());
            }
        })?;
    }
    if defaults.renames() {
        rename_appearances(transaction, copies, &defaults.renames, budget)?;
    }
    if let Some((appearance, quadding, need)) = &defaults.receiving {
        let version = transaction.snapshot();
        let mut reads = Reads::new(&version, budget);
        for &field in fields {
            let mut entries = Vec::new();
            if let Some(source) = &defaults.appearance
                && appearance.as_ref() != Some(source)
                && inherited(&mut reads, id(field), "DA")?.is_none()
            {
                entries.push(("DA", Value::String(PdfString::literal(source.clone()))));
            }
            if defaults.quadding != *quadding && inherited(&mut reads, id(field), "Q")?.is_none() {
                entries.push(("Q", Value::Integer(defaults.quadding)));
            }
            if entries.is_empty() {
                continue;
            }
            let Some(mut dict) = dict_at(transaction, field, budget)? else {
                continue;
            };
            for (key, value) in entries {
                dict.insert(Name::from(key), value);
            }
            transaction.write(field.number(), &Value::Dict(dict), budget)?;
        }
        if defaults.need_appearances && !need {
            edit_dict(transaction, holder, &[], budget, |form| {
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /NeedAppearances key"
                )]
                form.insert(Name::from("NeedAppearances"), Value::Boolean(true));
            })?;
        }
    }
    edit_array(transaction, holder, "Fields", budget, true, |items| {
        for &field in fields {
            items.push(Value::Reference(field));
        }
    })?;
    if !order.is_empty() {
        edit_array(transaction, holder, "CO", budget, true, |items| {
            for &field in order {
                items.push(Value::Reference(field));
            }
        })?;
    }
    Ok(())
}

/// Edits the dictionary that `path` leads to from object `holder`, each
/// key naming a dictionary directly or through references; a path that
/// leads to no dictionary leaves everything as it is.
#[expect(
    clippy::disallowed_methods,
    reason = "the direct-key stack follows the caller's fixed form-resource path of at most two keys"
)]
fn edit_dict(
    transaction: &mut Transaction,
    holder: Reference,
    path: &[&Name],
    budget: &Budget,
    edit: impl FnOnce(&mut Dict),
) -> Result<()> {
    let version = transaction.snapshot();
    let mut reads = Reads::new(&version, budget);
    let Some(mut current) = dict_at(transaction, holder, budget)? else {
        return Ok(());
    };
    // The object that holds the dictionary, and the keys to it from there.
    let mut number = holder;
    let mut direct = Vec::new();
    for &key in path {
        match defaults::slot(&mut reads, &current, key.as_bytes())? {
            Slot::Found(Some(object), Value::Dict(next)) => {
                let Some(generation) = version.generation(object) else {
                    return Ok(());
                };
                number = Reference::new(object, generation);
                direct.clear();
                current = next;
            }
            Slot::Found(None, Value::Dict(next)) => {
                direct.push(key);
                current = next;
            }
            _ => return Ok(()),
        }
    }
    let Some(mut root) = dict_at(transaction, number, budget)? else {
        return Ok(());
    };
    let mut dict = &mut root;
    for key in direct {
        match dict.get_mut(key.as_bytes()) {
            Some(Value::Dict(next)) => dict = next,
            _ => return Ok(()),
        }
    }
    edit(dict);
    transaction.write(number.number(), &Value::Dict(root), budget)?;
    Ok(())
}

/// Renames the resources that the default appearance strings of `copies`
/// name, directly or through a string object, after `renames`; a page,
/// page-tree node, or catalog keeps its own.
#[expect(
    clippy::disallowed_methods,
    reason = "one visited string reference per field from the budgeted form walk"
)]
fn rename_appearances(
    transaction: &mut Transaction,
    copies: &[Reference],
    renames: &Renames,
    budget: &Budget,
) -> Result<()> {
    let read = |transaction: &Transaction, number: u32| {
        transaction
            .version()
            .get(number, budget)
            .map(|object| object.map(|object| Arc::clone(object.value())))
    };
    let mut strings = HashSet::new();
    for copy in copies {
        let Some(value) = read(transaction, copy.number())? else {
            continue;
        };
        let dict = match &*value {
            Value::Dict(dict) => dict,
            Value::Stream(stream) => stream.dict(),
            _ => continue,
        };
        if let Some(Value::Name(kind)) = dict.get("Type")
            && matches!(kind.as_bytes(), b"Page" | b"Pages" | b"Catalog")
        {
            continue;
        }
        match dict.get("DA") {
            Some(Value::String(appearance)) => {
                let Some(renamed) = defaults::rewrite(appearance.as_bytes(), renames) else {
                    continue;
                };
                let mut value = (*value).clone();
                let dict = match &mut value {
                    Value::Dict(dict) => dict,
                    Value::Stream(stream) => stream.dict_mut(),
                    _ => continue,
                };
                #[expect(
                    clippy::disallowed_methods,
                    reason = "this call inserts only the fixed /DA key"
                )]
                dict.insert(Name::from("DA"), Value::String(PdfString::literal(renamed)));
                transaction.write(copy.number(), &value, budget)?;
            }
            Some(&Value::Reference(string)) if strings.insert(string.number()) => {
                let object = read(transaction, string.number())?;
                let Some(Value::String(appearance)) = object.as_deref() else {
                    continue;
                };
                if let Some(renamed) = defaults::rewrite(appearance.as_bytes(), renames) {
                    transaction.write(
                        string.number(),
                        &Value::String(PdfString::literal(renamed)),
                        budget,
                    )?;
                }
            }
            _ => {}
        }
    }
    Ok(())
}

/// Gives each of the imported top-level `fields` whose partial name a
/// top-level field of `form` has the smallest `+N` suffix that no
/// top-level field of either has; see [`register_imported`].
#[expect(
    clippy::disallowed_methods,
    reason = "reserve_map and the following name-byte charge precede insertion"
)]
fn rename_imported(
    transaction: &mut Transaction,
    reads: &mut Reads<'_>,
    form: &Dict,
    fields: &[Reference],
    budget: &Budget,
) -> Result<()> {
    let mut receiving = HashSet::new();
    let mut totals = (0, 0);
    if let Some(Value::Array(items)) = read(reads, form.get("Fields"))? {
        for item in items.iter() {
            if let Some(name) = partial_name(reads, item, &mut totals)? {
                insert_name(&mut receiving, name, budget)?;
            }
        }
    }
    let mut imported = Vec::new();
    let mut totals = (0, 0);
    for &field in fields {
        if let Some(name) = partial_name(reads, &Value::Reference(field), &mut totals)? {
            push(&mut imported, (field, name), budget)?;
        }
    }
    if !imported.iter().any(|(_, name)| receiving.contains(name)) {
        return Ok(());
    }
    let mut reserved = HashSet::new();
    for name in receiving
        .iter()
        .chain(imported.iter().map(|(_, name)| name))
    {
        insert_name(&mut reserved, name.clone(), budget)?;
    }
    let mut renamed: HashMap<String, String> = HashMap::new();
    for (field, name) in &imported {
        budget.poll()?;
        if !receiving.contains(name) {
            continue;
        }
        let new_name = match renamed.get(name) {
            Some(new_name) => new_name.clone(),
            None => {
                // Each name taken rules out at most one suffix, so the
                // search ends within the reserved names.
                let mut suffix = 1usize;
                let candidate = loop {
                    budget.poll()?;
                    let candidate = format!("{name}+{suffix}");
                    if !reserved.contains(&candidate) {
                        break candidate;
                    }
                    suffix += 1;
                };
                insert_name(&mut reserved, candidate.clone(), budget)?;
                reserve_map(&mut renamed, budget)?;
                budget.charge(
                    LimitCode::MemorySize,
                    name.len().saturating_add(candidate.len()) as u64,
                )?;
                renamed.insert(name.clone(), candidate.clone());
                candidate
            }
        };
        let Some(mut dict) = dict_at(transaction, *field, budget)? else {
            continue;
        };
        let partial = read(reads, dict.get("T"))?;
        let suffix = &new_name[name.len()..];
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /T key"
        )]
        dict.insert(
            Name::from("T"),
            renamed_partial(partial.as_ref(), &new_name, suffix),
        );
        transaction.write(field.number(), &Value::Dict(dict), budget)?;
    }
    Ok(())
}

/// Adds `name` to `names`, charging the entry and its bytes to `budget`.
#[expect(
    clippy::disallowed_methods,
    reason = "the name and set overhead are charged before fallible reservation and insertion"
)]
fn insert_name(names: &mut HashSet<String>, name: String, budget: &Budget) -> Result<()> {
    budget.charge(
        LimitCode::MemorySize,
        (64 + 2 * size_of::<String>()).saturating_add(name.len()) as u64,
    )?;
    names
        .try_reserve(1)
        .map_err(|_| pyropdf_store::LimitExceeded::new(LimitCode::MemorySize))?;
    names.insert(name);
    Ok(())
}

/// Returns the non-empty partial name `/T` of the field that `entry`
/// names, charging it to `totals` under the field-name limit.
fn partial_name(
    reads: &mut Reads<'_>,
    entry: &Value,
    totals: &mut (usize, usize),
) -> Result<Option<String>> {
    let &Value::Reference(reference) = entry else {
        return Ok(None);
    };
    let Some(field) = object(reads, id(reference))? else {
        return Ok(None);
    };
    let Value::Dict(field) = &*field else {
        return Ok(None);
    };
    let Some(partial) = field.get("T") else {
        return Ok(None);
    };
    Ok(text(reads, partial, totals, MAX_NAME_BYTES, "field-name")?.filter(|name| !name.is_empty()))
}

/// Returns partial name `partial` with `suffix` appended in its own
/// encoding, UTF-16BE or single bytes, or `renamed` as a new text string
/// when `partial` is no string or odd UTF-16BE.
#[expect(
    clippy::disallowed_methods,
    reason = "the old bytes and candidate name are charged by rename_imported; the suffix adds at most two bytes per character"
)]
fn renamed_partial(partial: Option<&Value>, renamed: &str, suffix: &str) -> Value {
    let Some(Value::String(partial)) = partial else {
        return encode_text(renamed);
    };
    let bytes = partial.as_bytes();
    let utf16 = bytes.starts_with(&[0xfe, 0xff]);
    if utf16 && bytes.len() % 2 != 0 {
        return encode_text(renamed);
    }
    let mut encoded = bytes.to_vec();
    for byte in suffix.bytes() {
        if utf16 {
            encoded.push(0);
        }
        encoded.push(byte);
    }
    Value::String(PdfString::new(encoded, partial.form()))
}
