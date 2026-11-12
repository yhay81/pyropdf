//! Reading, editing and copying pages (DESIGN.md section 8).

use std::{fmt, sync::Arc};

use pyropdf_store::budget::Budget;
use pyropdf_store::pages::{MAX_CHAIN, Page};
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::value::{Dict, Reference, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::{annotations::AnnotationError, navigation};

pub mod canvas;
pub mod copy;
pub mod geometry;
pub mod import;
pub mod sources;
pub mod tree;
pub mod unrotate;

/// A failure while reading or editing pages.
#[derive(Debug)]
pub enum PageError {
    Store(pyropdf_store::Error),
    Navigation(navigation::Error),
    Annotation(AnnotationError),
    Malformed(String),
    Value(String),
}

impl fmt::Display for PageError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Store(error) => error.fmt(f),
            Self::Navigation(error) => error.fmt(f),
            Self::Annotation(error) => error.fmt(f),
            Self::Malformed(message) | Self::Value(message) => f.write_str(message),
        }
    }
}

impl std::error::Error for PageError {}

impl From<pyropdf_store::Error> for PageError {
    fn from(error: pyropdf_store::Error) -> Self {
        Self::Store(error)
    }
}

impl From<pyropdf_store::LimitExceeded> for PageError {
    fn from(error: pyropdf_store::LimitExceeded) -> Self {
        Self::Store(error.into())
    }
}

impl From<navigation::Error> for PageError {
    fn from(error: navigation::Error) -> Self {
        Self::Navigation(error)
    }
}

impl From<AnnotationError> for PageError {
    fn from(error: AnnotationError) -> Self {
        Self::Annotation(error)
    }
}

pub type Result<T> = std::result::Result<T, PageError>;

/// Discard all writes of a failed edit while preserving earlier writes in the transaction.
fn with_savepoint<T>(
    transaction: &mut Transaction,
    change: impl FnOnce(&mut Transaction) -> Result<T>,
) -> Result<T> {
    let savepoint = transaction.savepoint();
    let result = change(transaction);
    if result.is_err() {
        transaction.rollback(savepoint);
    }
    result
}

fn not_a_page(page: Reference) -> PageError {
    PageError::Value(format!(
        "object {} {} is not a page of the document",
        page.number(),
        page.generation()
    ))
}

/// Returns a page's object identity, refusing a direct page dictionary.
pub fn indirect(page: &Page, at: usize) -> Result<Reference> {
    page.object()
        .ok_or_else(|| PageError::Malformed(format!("page {at} is not an indirect object")))
}

/// Follows a bounded chain of references, sharing the stored value.
fn target(
    version: &Version,
    mut reference: Reference,
    budget: &Budget,
) -> pyropdf_store::Result<Option<Arc<Value>>> {
    for _ in 0..MAX_CHAIN {
        let Some(object) = version
            .get(reference.number(), budget)?
            .filter(|object| object.generation() == reference.generation())
        else {
            return Ok(None);
        };
        match &**object.value() {
            Value::Null => return Ok(None),
            Value::Reference(next) => reference = *next,
            _ => return Ok(Some(Arc::clone(object.value()))),
        }
    }
    Err(pyropdf_store::Error::malformed(
        "a reference chain is longer than 16 references",
    ))
}

/// Resolves a PDF entry and copies it for editing under the operation's budget.
pub fn resolve(
    version: &Version,
    value: Option<&Value>,
    budget: &Budget,
) -> pyropdf_store::Result<Option<Value>> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Reference(reference)) => target(version, *reference, budget)?
            .map(|value| copy_for_edit(&value, budget))
            .transpose(),
        Some(value) => Ok(Some(copy_for_edit(value, budget)?)),
    }
}

fn page_dict(version: &Version, reference: Reference, budget: &Budget) -> Result<Dict> {
    match resolve(version, Some(&Value::Reference(reference)), budget)? {
        Some(Value::Dict(dict)) => Ok(dict),
        _ => Err(not_a_page(reference)),
    }
}

#[cfg(test)]
fn test_document(objects: &[&str]) -> pyropdf_store::version::Document {
    use pyropdf_store::value::{Bytes, Name};
    use pyropdf_store::version::{Document, MemoryBase};

    let mut trailer = Dict::new();
    trailer.insert(Name::from("Root"), Value::Reference(Reference::new(1, 0)));
    let mut base = MemoryBase::new(trailer);
    for (i, value) in objects.iter().enumerate() {
        base.insert(
            Reference::new(u32::try_from(i + 1).unwrap(), 0),
            Bytes::new(value.as_bytes().to_vec()),
        );
    }
    Document::new(Arc::new(base), None)
}
