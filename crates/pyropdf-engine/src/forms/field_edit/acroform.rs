//! Bootstrapping `/AcroForm` and registering Standard 14 fonts in `/DR`.

use pyropdf_store::budget::{Budget, BudgetVec};
use pyropdf_store::memo::Reads;
use pyropdf_store::refs::copy_dict_for_edit;
use pyropdf_store::value::{Array, Dict, Name, PdfString, Reference, Value};
use pyropdf_store::version::Transaction;

use crate::annotations::{AnnotationError as Error, AnnotationResult as Result};
use crate::forms::values::{id, object, read};

/// Makes Standard 14 font `base` resolvable in the `/DR` of form `holder`
/// under `preferred`, its `/DA` abbreviation, or a free `preferred_N` when
/// another font holds that name; returns the resource name. Existing
/// resources are copied, not edited through shared references.
#[expect(
    clippy::disallowed_methods,
    reason = "adds one font resource entry to the copied dictionary"
)]
pub(in crate::forms) fn register_standard_font(
    transaction: &mut Transaction,
    holder: Reference,
    preferred: &str,
    base: &str,
    budget: &Budget,
) -> Result<String> {
    let mut form = dict_at(transaction, holder, budget)?
        .ok_or_else(|| Error::Pdf("the interactive form is not a dictionary".into()))?;
    let snapshot = transaction.snapshot();
    let mut reads = Reads::new(&snapshot, budget);
    let mut resources = match read(&mut reads, form.get("DR"))? {
        Some(Value::Dict(dict)) => dict,
        None => Dict::new(),
        _ => return Err(Error::Pdf("the form's /DR is not a dictionary".into())),
    };
    let mut fonts = match read(&mut reads, resources.get("Font"))? {
        Some(Value::Dict(dict)) => dict,
        None => Dict::new(),
        _ => {
            return Err(Error::Pdf(
                "the form's /DR /Font is not a dictionary".into(),
            ));
        }
    };
    if standard_font_matches(read(&mut reads, fonts.get(preferred))?.as_ref(), base) {
        return Ok(preferred.to_owned());
    }
    let resource = if fonts.get(preferred).is_none() {
        preferred.to_owned()
    } else {
        // Track suffixes in one pass: Dict lookup is linear, so repeatedly
        // searching it for candidate names would take quadratic work.
        let mut occupied = BudgetVec::new(budget);
        occupied.try_reserve_exact(fonts.len().saturating_add(1))?;
        for _ in 0..=fonts.len() {
            occupied.try_push(false)?;
        }
        let prefix = format!("{preferred}_");
        for (name, value) in fonts.iter() {
            budget.poll()?;
            let Some(suffix) = name.as_bytes().strip_prefix(prefix.as_bytes()) else {
                continue;
            };
            // A usize decimal representation is at most 20 ASCII bytes.
            if suffix.len() > 20 {
                continue;
            }
            let Ok(suffix) = std::str::from_utf8(suffix) else {
                continue;
            };
            let Ok(number) = suffix.parse::<usize>() else {
                continue;
            };
            if number == 0 || number.to_string() != suffix {
                continue;
            }
            if standard_font_matches(read(&mut reads, Some(value))?.as_ref(), base) {
                return Ok(format!("{prefix}{suffix}"));
            }
            if number <= fonts.len() {
                occupied[number] = true;
            }
        }
        let suffix = occupied
            .iter()
            .enumerate()
            .skip(1)
            .find(|(_, used)| !**used)
            .map_or(fonts.len().saturating_add(1), |(index, _)| index);
        format!("{preferred}_{suffix}")
    };
    let mut font = Dict::new();
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /Type key"
    )]
    font.insert(Name::from("Type"), Value::Name(Name::from("Font")));
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /Subtype key"
    )]
    font.insert(Name::from("Subtype"), Value::Name(Name::from("Type1")));
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /BaseFont key"
    )]
    font.insert(Name::from("BaseFont"), Value::Name(Name::from(base)));
    if !matches!(base, "Symbol" | "ZapfDingbats") {
        #[expect(
            clippy::disallowed_methods,
            reason = "this call inserts only the fixed /Encoding key"
        )]
        font.insert(
            Name::from("Encoding"),
            Value::Name(Name::from("WinAnsiEncoding")),
        );
    }
    let reference = transaction.allocate()?;
    transaction.write(reference.number(), &Value::Dict(font), budget)?;
    fonts.insert(Name::from(resource.as_str()), Value::Reference(reference));
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /Font key"
    )]
    resources.insert(Name::from("Font"), Value::Dict(fonts));
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /DR key"
    )]
    form.insert(Name::from("DR"), Value::Dict(resources));
    transaction.write(holder.number(), &Value::Dict(form), budget)?;
    Ok(resource)
}

fn standard_font_matches(value: Option<&Value>, base: &str) -> bool {
    let Some(Value::Dict(font)) = value else {
        return false;
    };
    let symbolic = matches!(base, "Symbol" | "ZapfDingbats");
    font.len() == if symbolic { 3 } else { 4 }
        && font.get("Type") == Some(&Value::Name(Name::from("Font")))
        && font.get("Subtype") == Some(&Value::Name(Name::from("Type1")))
        && font.get("BaseFont") == Some(&Value::Name(Name::from(base)))
        && if symbolic {
            font.get("Encoding").is_none()
        } else {
            font.get("Encoding") == Some(&Value::Name(Name::from("WinAnsiEncoding")))
        }
}

/// Returns the form dictionary's object, after moving one that the catalog
/// holds into an object of its own, or creating an empty one that names
/// Helvetica as `/Helv`.
#[expect(
    clippy::disallowed_methods,
    reason = "only fixed-size literal dictionaries and the constant default-appearance bytes are constructed"
)]
pub(in crate::forms) fn form_object(
    transaction: &mut Transaction,
    budget: &Budget,
) -> Result<Reference> {
    let version = transaction.snapshot();
    let mut reads = Reads::new(&version, budget);
    let Some(Value::Reference(root)) = reads.trailer().get("Root").cloned() else {
        return Err(Error::Pdf("the document has no catalog".into()));
    };
    let Some(catalog) = object(&mut reads, id(root))? else {
        return Err(Error::Pdf("the document has no catalog".into()));
    };
    let Value::Dict(catalog_dict) = &*catalog else {
        return Err(Error::Pdf("the document has no catalog".into()));
    };
    let form = match catalog_dict.get("AcroForm") {
        Some(Value::Reference(reference)) => {
            if let Some(Value::Dict(_)) = object(&mut reads, id(*reference))?.as_deref() {
                return Ok(*reference);
            }
            None
        }
        Some(Value::Dict(form)) => Some(Value::Dict(copy_dict_for_edit(form, budget)?)),
        _ => None,
    };
    let form = match form {
        Some(form) => form,
        None => {
            let font = transaction.allocate()?;
            let helvetica: Dict = [
                ("Type", "Font"),
                ("Subtype", "Type1"),
                ("BaseFont", "Helvetica"),
                ("Encoding", "WinAnsiEncoding"),
            ]
            .into_iter()
            .map(|(key, value)| (Name::from(key), Value::Name(Name::from(value))))
            .collect();
            transaction.write(font.number(), &Value::Dict(helvetica), budget)?;
            let fonts: Dict = [(Name::from("Helv"), Value::Reference(font))]
                .into_iter()
                .collect();
            let resources: Dict = [(Name::from("Font"), Value::Dict(fonts))]
                .into_iter()
                .collect();
            let appearance = PdfString::literal(b"/Helv 0 Tf 0 g".to_vec());
            Value::Dict(
                [
                    (Name::from("Fields"), Value::Array(Array::new())),
                    (Name::from("DA"), Value::String(appearance)),
                    (Name::from("DR"), Value::Dict(resources)),
                ]
                .into_iter()
                .collect(),
            )
        }
    };
    let mut catalog = copy_dict_for_edit(catalog_dict, budget)?;
    let reference = transaction.allocate()?;
    transaction.write(reference.number(), &form, budget)?;
    #[expect(
        clippy::disallowed_methods,
        reason = "this call inserts only the fixed /AcroForm key"
    )]
    catalog.insert(Name::from("AcroForm"), Value::Reference(reference));
    transaction.write(root.number(), &Value::Dict(catalog), budget)?;
    Ok(reference)
}

/// Returns a copy of the dictionary of object `reference` in the
/// transaction's version, charged to `budget`, or `None` when that object,
/// with that generation, is no dictionary.
pub(in crate::forms) fn dict_at(
    transaction: &Transaction,
    reference: Reference,
    budget: &Budget,
) -> Result<Option<Dict>> {
    let object = transaction.version().get(reference.number(), budget)?;
    match object.as_ref() {
        Some(object) if object.generation() == reference.generation() => match &**object.value() {
            Value::Dict(dict) => Ok(Some(copy_dict_for_edit(dict, budget)?)),
            _ => Ok(None),
        },
        _ => Ok(None),
    }
}
