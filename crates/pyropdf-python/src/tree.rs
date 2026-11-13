//! PyO3 entry points for canonical name-tree, number-tree, and `/ParentTree`
//! mutations backed by `pyropdf_store::tree`.

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList};
use pyropdf_store::budget::{Budget, BudgetVec};
use pyropdf_store::tree;
use pyropdf_store::value::{PdfString, Value};
use pyropdf_store::version::Version;

use crate::document::_Document;
use crate::errors::{limit_error, store_error};
use crate::objects::{input_bytes, untagged};

fn decode_name_pairs<'b>(
    entries: &Bound<'_, PyList>,
    version: &Version,
    budget: &'b Budget,
) -> PyResult<BudgetVec<'b, (PdfString, Value)>> {
    let mut pairs = BudgetVec::new(budget);
    pairs
        .try_reserve_exact(entries.len())
        .map_err(|e| limit_error(&e))?;
    for item in entries.iter() {
        let (key, value): (Bound<'_, PyBytes>, Bound<'_, PyAny>) = item.extract()?;
        let key_bytes = input_bytes(&key, budget)?;
        let decoded = untagged(&value, version, 1, budget)?;
        pairs
            .try_push((PdfString::literal(key_bytes), decoded))
            .map_err(|e| limit_error(&e))?;
    }
    Ok(pairs)
}

/// Appends one `owners` entry onto `Catalog["StructTreeRoot"]["ParentTree"]`,
/// updates `/ParentTreeNextKey`, and returns the assigned integer key.
#[pyfunction]
pub fn parent_tree_append(
    py: Python<'_>,
    document: &_Document,
    owners: &Bound<'_, PyAny>,
) -> PyResult<i64> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            let decoded = untagged(owners, transaction.version(), 1, budget)?;
            tree::append_parent_tree_entry(transaction, decoded, budget)
                .map_err(|e| store_error(&e))
        })
    })?
}

/// Replaces `Catalog["PageLabels"]` with `entries` (`(start, dict)` pairs),
/// removing `/PageLabels` when `entries` is empty and freeing unreferenced
/// retired `/Kids` nodes.
#[pyfunction]
pub fn page_labels_write(
    py: Python<'_>,
    document: &_Document,
    entries: &Bound<'_, PyList>,
) -> PyResult<()> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            let mut pairs = BudgetVec::new(budget);
            pairs
                .try_reserve_exact(entries.len())
                .map_err(|e| limit_error(&e))?;
            for item in entries.iter() {
                let (start, dict): (i64, Bound<'_, PyAny>) = item.extract()?;
                let decoded = untagged(&dict, transaction.version(), 1, budget)?;
                pairs
                    .try_push((start, decoded))
                    .map_err(|e| limit_error(&e))?;
            }
            tree::replace_catalog_number_tree(
                &document.references,
                transaction,
                "PageLabels",
                pairs,
                budget,
            )
            .map_err(|e| store_error(&e))
        })
    })?
}

/// Inserts or replaces `(key, value)` in `Catalog["Names"]["EmbeddedFiles"]`,
/// falling back to a full canonical rebuild from `fallback` when the existing
/// tree is non-canonical, and freeing unreferenced retired `/Kids` nodes.
#[pyfunction]
pub fn embedded_files_insert(
    py: Python<'_>,
    document: &_Document,
    key: &Bound<'_, PyBytes>,
    value: &Bound<'_, PyAny>,
    fallback: &Bound<'_, PyList>,
) -> PyResult<()> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            let key_bytes = input_bytes(key, budget)?;
            let decoded_val = untagged(value, transaction.version(), 1, budget)?;
            let fallback_pairs = decode_name_pairs(fallback, transaction.version(), budget)?;
            tree::insert_catalog_embedded_files(
                &document.references,
                transaction,
                PdfString::literal(key_bytes),
                decoded_val,
                fallback_pairs,
                budget,
            )
            .map_err(|e| store_error(&e))
        })
    })?
}

/// Removes `keys` from `Catalog["Names"]["EmbeddedFiles"]`, falling back to a
/// canonical rebuild from `fallback` when the existing tree is non-canonical,
/// and frees unreferenced retired `/Kids` nodes alongside `candidates`.
#[pyfunction]
pub fn embedded_files_remove(
    py: Python<'_>,
    document: &_Document,
    keys: &Bound<'_, PyList>,
    fallback: &Bound<'_, PyList>,
    candidates: &Bound<'_, PyList>,
) -> PyResult<()> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            let mut key_vec = BudgetVec::new(budget);
            key_vec
                .try_reserve_exact(keys.len())
                .map_err(|e| limit_error(&e))?;
            for item in keys.iter() {
                let raw: Bound<'_, PyBytes> = item.extract()?;
                key_vec
                    .try_push(PdfString::literal(input_bytes(&raw, budget)?))
                    .map_err(|e| limit_error(&e))?;
            }
            let fallback_pairs = decode_name_pairs(fallback, transaction.version(), budget)?;
            let mut candidate_vec = BudgetVec::new(budget);
            candidate_vec
                .try_reserve_exact(candidates.len())
                .map_err(|e| limit_error(&e))?;
            for item in candidates.iter() {
                let number: u32 = item.extract()?;
                candidate_vec
                    .try_push(number)
                    .map_err(|e| limit_error(&e))?;
            }
            tree::remove_catalog_embedded_files(
                &document.references,
                transaction,
                &key_vec,
                fallback_pairs,
                &candidate_vec,
                budget,
            )
            .map_err(|e| store_error(&e))
        })
    })?
}
