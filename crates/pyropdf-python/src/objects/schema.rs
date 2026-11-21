//! Interpret typed handles at a single version of the store.
use std::sync::Arc;

use pyo3::exceptions::{PyKeyError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyropdf_store::LimitExceeded;
use pyropdf_store::budget::{Budget, LimitCode};
use pyropdf_store::value::{Array, Dict, Reference, Value};
use pyropdf_store::version::Version;

use super::{Root, Step, child, reference_target, resolve, root_value, tagged, walk};
use crate::document::_Document;
use crate::errors::{MalformedPdfError, limit_error, store_error};

/// Interpret a single typed entry at the same version and budget for its
/// discriminator, declared extensions, defaults and indirect references.
type TypedEntry<'py> = (
    &'static str,
    Option<&'static str>,
    Option<&'static str>,
    Bound<'py, PyAny>,
    Option<(u32, u16)>,
    Option<&'static str>,
);

#[pyfunction]
pub fn schema_entry<'py>(
    py: Python<'py>,
    document: &_Document,
    root: Root,
    path: Vec<Step>,
    step: Step,
    descriptor: &str,
    key: &str,
) -> PyResult<TypedEntry<'py>> {
    crate::errors::boundary(|| {
        use pyropdf_schema::check::{Interpretation, interpret};

        let model = pyropdf_schema::object(descriptor)
            .ok_or_else(|| PyKeyError::new_err(descriptor.to_owned()))?;
        let mut key = model
            .key(key)
            .ok_or_else(|| PyKeyError::new_err(format!("{descriptor}/{key}")))?;
        let version = document.reading()?;
        let budget = document.budget();
        let graph = crate::check::store_graph(&version, &budget).map_err(|e| store_error(&e))?;
        let base = root_value(&version, root, &path, &budget)?;
        let base = walk(&base, root, &path)?;
        let mut parent: Option<(Reference, Arc<Value>)> = None;
        let mut alias = None;
        // The lookup is anchored to exactly one store version, including
        // conditional defaults on the holder from which a value is inherited.
        let limit = budget.limit(LimitCode::ObjectDepth).unwrap_or(64).min(64);
        for depth in 0..=limit {
            budget.poll().map_err(|e| limit_error(&e))?;
            let holder = parent.as_ref().map_or(base, |(_, value)| value);
            let raw = if let Some(name) = alias {
                match holder {
                    Value::Dict(dict) => dict.get(name),
                    Value::Stream(stream) => stream.dict().get(name),
                    _ => None,
                }
            } else {
                child(holder, &step)
            };
            let resolved;
            let (value, reference) = if let Some(Value::Reference(reference)) = raw {
                resolved =
                    reference_target(&version, *reference, &budget).map_err(|e| store_error(&e))?;
                let Some((target, value)) = &resolved else {
                    return Err(MalformedPdfError::new_err(format!(
                        "{descriptor}/{} has an unresolved reference",
                        key.name
                    )));
                };
                (Some(value.as_ref()), Some(*target))
            } else {
                (raw, None)
            };
            let permits_null = key
                .types
                .iter()
                .any(|ty| ty.kind == pyropdf_schema::Kind::Null);
            let value = value.filter(|v| !matches!(v, Value::Null) || permits_null);
            if value.is_none()
                && key.inheritable
                && let Some(Value::Reference(reference)) = match holder {
                    Value::Dict(dict) => dict.get("Parent"),
                    Value::Stream(stream) => stream.dict().get("Parent"),
                    _ => None,
                }
            {
                if depth == limit {
                    return Err(limit_error(&LimitExceeded::over(
                        LimitCode::ObjectDepth,
                        limit,
                        limit + 1,
                    )));
                }
                let next = resolve(&version, *reference, &budget)?;
                if next.is_some() {
                    parent = next;
                    continue;
                }
            }
            let inherited = parent.as_ref().map(|(r, _)| (r.number(), r.generation()));
            let tag = match interpret(&graph, holder, key, value).map_err(|e| store_error(&e))? {
                Interpretation::Absent => ("", None, None, py.None().into_bound(py), None, None),
                Interpretation::Default(_, literal) if literal.starts_with('@') => {
                    let source = &literal[1..];
                    key = model
                        .key(source)
                        .ok_or_else(|| MalformedPdfError::new_err("unknown default source key"))?;
                    alias = Some(source);
                    parent = None;
                    continue;
                }
                Interpretation::Default(kind, literal) => (
                    kind.name(),
                    None,
                    Some(literal),
                    py.None().into_bound(py),
                    None,
                    None,
                ),
                Interpretation::Value(kind, target) => {
                    let value = value.unwrap_or(&Value::Null);
                    let result = match kind {
                        pyropdf_schema::Kind::Matrix => numbers::<6>(py, &version, value, &budget)?,
                        pyropdf_schema::Kind::Rectangle => {
                            numbers::<4>(py, &version, value, &budget)?
                        }
                        _ => tagged(py, &version, value, reference, &budget)?,
                    };
                    (kind.name(), target, None, result, inherited, alias)
                }
                Interpretation::Refused(reason) => {
                    return Err(MalformedPdfError::new_err(format!(
                        "{descriptor}/{}: {reason}",
                        key.name
                    )));
                }
            };
            return Ok(tag);
        }
        Err(limit_error(&LimitExceeded::over(
            LimitCode::ObjectDepth,
            limit,
            limit + 1,
        )))
    })?
}

fn numbers<'py, const N: usize>(
    py: Python<'py>,
    version: &Version,
    value: &Value,
    budget: &Budget,
) -> PyResult<Bound<'py, PyAny>> {
    let Value::Array(array) = value else {
        return Err(MalformedPdfError::new_err("expected an array of numbers"));
    };
    if array.len() != N {
        return Err(MalformedPdfError::new_err(format!("expected {N} numbers")));
    }
    let mut numbers = [0.0; N];
    for (slot, item) in numbers.iter_mut().zip(array.iter()) {
        *slot = match resolved(version, item, budget)?.value() {
            #[expect(
                clippy::cast_precision_loss,
                reason = "Python rectangle and matrix coordinates are f64"
            )]
            Value::Integer(value) => *value as f64,
            Value::Real(value) => value.value(),
            _ => {
                return Err(MalformedPdfError::new_err(
                    "array contains a non-numeric coordinate",
                ));
            }
        };
    }
    Ok(pyo3::types::PyTuple::new(py, numbers)?.into_any())
}

/// A direct child borrows its owner. An indirect child keeps the correctly
/// generated reference so Python can construct a live handle to the leaf.
enum Child<'a> {
    Direct(&'a Value),
    Indirect(Reference, Arc<Value>),
}

impl Child<'_> {
    fn value(&self) -> &Value {
        match self {
            Self::Direct(value) => value,
            Self::Indirect(_, value) => value,
        }
    }

    fn reference(&self) -> Option<Reference> {
        match self {
            Self::Direct(_) => None,
            Self::Indirect(reference, _) => Some(*reference),
        }
    }
}

fn resolved<'a>(version: &Version, value: &'a Value, budget: &Budget) -> PyResult<Child<'a>> {
    match value {
        Value::Reference(reference) => reference_target(version, *reference, budget)
            .map_err(|e| store_error(&e))?
            .map(|(reference, value)| Child::Indirect(reference, value))
            .ok_or_else(|| MalformedPdfError::new_err("unresolved tree reference")),
        other => Ok(Child::Direct(other)),
    }
}

#[derive(FromPyObject)]
pub enum TreeKey<'py> {
    Name(Bound<'py, PyBytes>),
    Number(i64),
}

impl TreeKey<'_> {
    fn compare(&self, value: &Value) -> PyResult<std::cmp::Ordering> {
        match (self, value) {
            (Self::Name(wanted), Value::String(key)) => Ok(key.as_bytes().cmp(wanted.as_bytes())),
            (Self::Number(wanted), Value::Integer(key)) => Ok(key.cmp(wanted)),
            _ => Err(MalformedPdfError::new_err("invalid tree key type")),
        }
    }

    fn pairs(&self) -> &'static str {
        match self {
            Self::Name(_) => "Names",
            Self::Number(_) => "Nums",
        }
    }
}

fn array<'a>(value: &'a Child<'_>) -> PyResult<&'a Array> {
    match value.value() {
        Value::Array(array) => Ok(array),
        _ => Err(MalformedPdfError::new_err("tree entry is not an array")),
    }
}

fn dict(value: &Value) -> PyResult<&Dict> {
    match value {
        Value::Dict(dict) => Ok(dict),
        _ => Err(MalformedPdfError::new_err("tree node is not a dictionary")),
    }
}

/// Select the one child whose inclusive /Limits contain the lookup key.
fn tree_kid(
    version: &Version,
    kids: &Array,
    key: &TreeKey<'_>,
    budget: &Budget,
) -> PyResult<Option<(Reference, Arc<Value>)>> {
    let (mut lo, mut hi) = (0, kids.len());
    while lo < hi {
        let mid = lo + (hi - lo) / 2;
        let kid = resolved(version, &kids.as_slice()[mid], budget)?;
        let Child::Indirect(reference, kid) = kid else {
            return Err(MalformedPdfError::new_err("tree kid must be indirect"));
        };
        let bounds = dict(&kid)?
            .get("Limits")
            .ok_or_else(|| MalformedPdfError::new_err("tree kid is missing /Limits"))?;
        let bounds = resolved(version, bounds, budget)?;
        let [low, high] = array(&bounds)?.as_slice() else {
            return Err(MalformedPdfError::new_err(
                "tree /Limits must contain a pair",
            ));
        };
        if key.compare(low)?.is_gt() {
            hi = mid;
        } else if key.compare(high)?.is_lt() {
            lo = mid + 1;
        } else {
            return Ok(Some((reference, kid)));
        }
    }
    Ok(None)
}

type TreeResult<'py> = (Option<&'static str>, Bound<'py, PyAny>, Root, bool, usize);

/// Look up a single tree key and interpret its linked leaf at the same version.
#[pyfunction]
pub fn schema_leaf<'py>(
    py: Python<'py>,
    document: &_Document,
    root: Root,
    path: Vec<Step>,
    descriptor: &str,
    key: &str,
    needle: TreeKey<'py>,
) -> PyResult<Option<TreeResult<'py>>> {
    crate::errors::boundary(|| {
        use pyropdf_schema::{Kind, object};
        let kind = match needle {
            TreeKey::Name(_) => Kind::NameTree,
            TreeKey::Number(_) => Kind::NumberTree,
        };
        let declared = object(descriptor)
            .and_then(|object| object.key(key))
            .and_then(|key| key.types.iter().find(|ty| ty.kind == kind))
            .ok_or_else(|| {
                PyTypeError::new_err("a name tree takes bytes and a number tree takes integers")
            })?;
        let version = document.reading()?;
        let budget = document.budget();
        let graph = crate::check::store_graph(&version, &budget).map_err(|e| store_error(&e))?;
        let base = root_value(&version, root, &path, &budget)?;
        let root_node = walk(&base, root, &path)?;
        let mut current: Option<(Reference, Arc<Value>)> = None;
        let limit = budget.limit(LimitCode::ObjectDepth).unwrap_or(64).min(64);
        for depth in 0..=limit {
            budget.poll().map_err(|e| limit_error(&e))?;
            let node = current
                .as_ref()
                .map_or(root_node, |(_, value)| value.as_ref());
            let node_dict = dict(node)?;
            let pairs = needle.pairs();
            if let Some(raw_kids) = node_dict.get("Kids") {
                if node_dict.get(pairs).is_some() {
                    return Err(MalformedPdfError::new_err(
                        "tree node mixes kids and leaves",
                    ));
                }
                if depth == limit {
                    return Err(limit_error(&LimitExceeded::over(
                        LimitCode::ObjectDepth,
                        limit,
                        limit + 1,
                    )));
                }
                let kids = resolved(&version, raw_kids, &budget)?;
                current = tree_kid(&version, array(&kids)?, &needle, &budget)?;
                if current.is_none() {
                    return Ok(None);
                }
                continue;
            }
            let Some(raw) = node_dict.get(pairs) else {
                return Ok(None);
            };
            let holder = resolved(&version, raw, &budget)?;
            let pairs = array(&holder)?.as_slice();
            if pairs.len() % 2 != 0 {
                return Err(MalformedPdfError::new_err("tree leaf has an unpaired key"));
            }
            let (mut lo, mut hi) = (0, pairs.len() / 2);
            while lo < hi {
                let mid = lo + (hi - lo) / 2;
                match needle.compare(&pairs[mid * 2])? {
                    std::cmp::Ordering::Less => lo = mid + 1,
                    std::cmp::Ordering::Greater => hi = mid,
                    std::cmp::Ordering::Equal => {
                        let at = mid * 2 + 1;
                        let leaf = resolved(&version, &pairs[at], &budget)?;
                        let target = pyropdf_schema::check::tree_target(
                            &graph,
                            declared,
                            node,
                            leaf.value(),
                        )
                        .map_err(|e| store_error(&e))?;
                        if !declared.links.is_empty() && target.is_none() {
                            return Err(MalformedPdfError::new_err(
                                "no unique linked tree value type matches",
                            ));
                        }
                        let tag = tagged(py, &version, leaf.value(), leaf.reference(), &budget)?;
                        let reference = holder
                            .reference()
                            .or_else(|| current.as_ref().map(|(r, _)| *r));
                        return Ok(Some((
                            target,
                            tag,
                            reference.map(|r| (r.number(), r.generation())),
                            holder.reference().is_some(),
                            at,
                        )));
                    }
                }
            }
            return Ok(None);
        }
        unreachable!("the last iteration refuses another kid")
    })?
}
