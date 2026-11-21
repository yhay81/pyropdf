//! ISO developer extensions declared on the catalog of the outgoing version.

use std::sync::Arc;

use crate::budget::Budget;
use crate::refs::copy_for_edit;
use crate::tree::{catalog_of, resolve_in};
use crate::value::{Array, Dict, Name, PdfString, Value};
use crate::version::{Document, Transaction, Version};
use crate::{Error, Result};

/// Declare a PDF 2.0 ISO extension on an isolated save version. Leave other
/// registered extensions intact, including their indirect references.
pub(super) fn declare(
    version: &Version,
    level: i64,
    revision: &str,
    url: &str,
    budget: &Budget,
) -> Result<Arc<Version>> {
    let (root, mut catalog) = catalog_of(version, budget)?;
    let mut writer = Document::for_save(version);
    let mut extensions = match catalog.get("Extensions") {
        None => Dict::new(),
        value => match resolve_in(version, value, budget)? {
            Some(Value::Dict(dict)) => dict,
            _ => return Err(Error::malformed("catalog /Extensions is not a dictionary")),
        },
    };
    let original = extensions.get("ISO_");
    let mut entries = match original {
        None => Array::new(),
        Some(value) => match resolve_in(version, Some(value), budget)? {
            Some(Value::Array(array)) => array,
            Some(Value::Dict(_)) => {
                let mut array = Array::new();
                array.push(copy_for_edit(value, budget)?);
                array
            }
            _ => return Err(Error::malformed("invalid /ISO_ extension declaration")),
        },
    };
    let mut found = false;
    for entry in entries.iter() {
        let Some(Value::Dict(dict)) = resolve_in(version, Some(entry), budget)? else {
            return Err(Error::malformed("invalid /ISO_ extension entry"));
        };
        if dict.get("ExtensionLevel") == Some(&Value::Integer(level)) {
            if !matches!(dict.get("BaseVersion"), Some(Value::Name(name)) if name.as_bytes() == b"2.0")
            {
                return Err(Error::malformed("ISO extension requires /BaseVersion /2.0"));
            }
            found = true;
        }
    }
    if !found {
        let mut extension = Dict::new();
        extension.insert(Name::from("Type"), Name::from("DeveloperExtensions").into());
        extension.insert(Name::from("BaseVersion"), Name::from("2.0").into());
        extension.insert(Name::from("ExtensionLevel"), level.into());
        extension.insert(
            Name::from("ExtensionRevision"),
            PdfString::literal(revision.as_bytes()).into(),
        );
        extension.insert(Name::from("URL"), PdfString::literal(url.as_bytes()).into());
        entries.push(extension.into());
    }
    let array = preserve_indirection(&mut writer, original, entries.into(), budget)?;
    extensions.insert(Name::from("ISO_"), array);
    let value = preserve_indirection(
        &mut writer,
        catalog.get("Extensions"),
        extensions.into(),
        budget,
    )?;
    catalog.insert(Name::from("Extensions"), value);
    if !matches!(catalog.get("Version"), Some(Value::Name(name)) if name.as_bytes() >= b"2.0") {
        catalog.insert(Name::from("Version"), Name::from("2.0").into());
    }
    writer.write(root.number(), &catalog.into(), budget)?;
    Ok(writer.snapshot())
}

/// A shared indirect container may have other owners. Give only the outgoing
/// catalog a replacement container, leaving their references unchanged.
fn preserve_indirection(
    writer: &mut Transaction,
    original: Option<&Value>,
    value: Value,
    budget: &Budget,
) -> Result<Value> {
    if matches!(original, Some(Value::Reference(_))) {
        let reference = writer.allocate()?;
        writer.write(reference.number(), &value, budget)?;
        Ok(reference.into())
    } else {
        Ok(value)
    }
}
