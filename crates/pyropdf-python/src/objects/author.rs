//! Checked creation, replacement and removal of standalone Arlington objects.
use std::sync::Arc;

use pyo3::exceptions::PyKeyError;
use pyo3::prelude::*;
use pyropdf_schema::check::{
    Graph, Interpretation, authorable, check_authored, interpret_reference,
};
use pyropdf_store::budget::Budget;
use pyropdf_store::memo::Reads;
use pyropdf_store::refs::{self, Referrer, Role};
use pyropdf_store::value::Value;
use pyropdf_store::version::Version;

use super::{root_value, stale, untagged};
use crate::document::_Document;
use crate::errors::{MalformedPdfError, store_error};

fn validate(
    version: &Version,
    budget: &Budget,
    number: u32,
    name: &str,
    value: &Value,
    create: bool,
) -> PyResult<()> {
    let model = pyropdf_schema::object(name).ok_or_else(|| PyKeyError::new_err(name.to_owned()))?;
    let graph = crate::check::store_graph(version, budget).map_err(|e| store_error(&e))?;
    use pyropdf_schema::Kind;
    if !matches!(
        (model.container(), value),
        (Kind::Array, Value::Array(_))
            | (Kind::Dictionary, Value::Dict(_))
            | (Kind::Stream, Value::Stream(_))
    ) {
        return Err(MalformedPdfError::new_err(format!(
            "{name} requires a {}",
            model.container().name()
        )));
    }
    if create && !authorable(&graph, model, value).map_err(|e| store_error(&e))? {
        return Err(MalformedPdfError::new_err(format!(
            "{name} is unavailable or deprecated in this document version"
        )));
    }
    let report = check_authored(&graph, name, number, value)
        .map_err(|e| store_error(&e))?
        .ok_or_else(|| PyKeyError::new_err(name.to_owned()))?;
    if let Some(first) = report.defects.first() {
        return Err(MalformedPdfError::new_err(format!(
            "{}: {}",
            first.clause, first.message
        )));
    }
    Ok(())
}

/// Validate the newly allocated object before the creating transaction commits.
#[pyfunction]
pub fn schema_created(document: &_Document, root: (u32, u16), descriptor: &str) -> PyResult<()> {
    crate::errors::boundary(|| {
        let version = document.reading()?;
        let budget = document.budget();
        let value = root_value(&version, Some(root), &[], &budget)?;
        validate(&version, &budget, root.0, descriptor, &value, true)
    })?
}

/// Check each live inbound edge before changing its target. Unknown contexts
/// and roles with an operation-specific rule require that operation. A
/// generic direct linking key can keep pointing to an admitted replacement.
fn incoming(
    document: &_Document,
    original: &Arc<Version>,
    current: &Version,
    budget: &Budget,
    root: (u32, u16),
    name: &str,
    proposed: Option<&Value>,
) -> PyResult<()> {
    let index = document
        .references
        .get(original, budget)
        .map_err(|e| store_error(&e))?;
    let mut reads = Reads::new(original, budget);
    let graph = crate::check::store_graph(current, budget).map_err(|e| store_error(&e))?;
    let holders = index
        .referrers(root.0)
        .iter()
        .copied()
        .map(Referrer::Object)
        .chain(
            index
                .trailer()
                .binary_search(&root.0)
                .is_ok()
                .then_some(Referrer::Trailer),
        );
    for holder in holders {
        for found in refs::find(&mut reads, holder, root.0)
            .map_err(|e| store_error(&e))?
            .iter()
        {
            if found.reference.generation() != root.1 {
                continue;
            }
            if let (Some(value), Referrer::Object(number), Some(Role::Link(link))) =
                (proposed, holder, found.role)
                && number != root.0
                && !link.element
                && let Some(model) = pyropdf_schema::object(link.holder)
                && let Some(key) = model.key(link.key)
                && let Some(owner) = graph.resolve(number).map_err(|e| store_error(&e))?
                && matches!(interpret_reference(&graph, &owner, model, key, Some(value), Some(root.0)).map_err(|e| store_error(&e))?,
                    Interpretation::Value(_, Some(target)) if target == name)
            {
                continue;
            }
            return Err(MalformedPdfError::new_err(format!(
                "{name} {} {} R is referenced by {holder:?} at {:?} ({:?}); edit the owning relationship",
                root.0, root.1, found.path, found.role
            )));
        }
    }
    Ok(())
}

/// Replace or free one indirect object, keeping its identity for a replacement
/// and preserving every other object for a removal.
#[pyfunction]
pub fn schema_change(
    py: Python<'_>,
    document: &_Document,
    root: (u32, u16),
    descriptor: &str,
    value: Option<&Bound<'_, PyAny>>,
) -> PyResult<()> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            if transaction.version().generation(root.0) != Some(root.1) {
                return Err(stale(Some(root), &[]));
            }
            let proposed = value
                .map(|v| untagged(v, transaction.version(), 0, budget))
                .transpose()?;
            let original = transaction.snapshot();
            if let Some(value) = proposed.as_ref() {
                transaction
                    .write_with_name_edit(
                        root.0,
                        value,
                        pyropdf_store::version::NameEdit::Set(&[]),
                        budget,
                    )
                    .map_err(|e| store_error(&e))?;
                let current = transaction.snapshot();
                incoming(
                    document,
                    &original,
                    &current,
                    budget,
                    root,
                    descriptor,
                    Some(value),
                )?;
                validate(&current, budget, root.0, descriptor, value, false)?;
            } else {
                incoming(
                    document, &original, &original, budget, root, descriptor, None,
                )?;
                transaction
                    .free(root.0, budget)
                    .map_err(|e| store_error(&e))?;
            }
            Ok(())
        })
    })?
}
