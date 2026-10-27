//! The structural check of pyropdf-schema over one version of the store
//! (DESIGN.md section 8).

use std::sync::Arc;

use pyo3::prelude::*;
use pyropdf_schema::Version as PdfVersion;
use pyropdf_schema::check::{Graph, Report, View, check};
use pyropdf_store::Error;
use pyropdf_store::budget::{Budget, LimitCode};
use pyropdf_store::value::Value;
use pyropdf_store::version::Version;

use crate::document::{_Document, header_version};
use crate::errors::store_error;

/// One defect: the object (`None` for the trailer), the path inside it, the
/// Arlington clause, the specification clause, the kind, and the message.
type DefectRow = (
    Option<u32>,
    String,
    String,
    &'static str,
    &'static str,
    String,
);

/// The version when a file declares none, as pyropdf writes it.
const DEFAULT_VERSION: PdfVersion = PdfVersion { major: 1, minor: 7 };

struct Store<'a> {
    version: &'a Version,
    budget: &'a Budget,
    trailer: Value,
    declared: PdfVersion,
}

impl Graph for Store<'_> {
    type Value = Value;
    type Owned = Arc<Value>;
    type Error = Error;

    fn version(&self) -> PdfVersion {
        self.declared
    }

    fn trailer(&self) -> &Value {
        &self.trailer
    }

    fn resolve(&self, number: u32) -> Result<Option<Arc<Value>>, Error> {
        match self.version.get(number, self.budget) {
            Ok(object) => Ok(object.map(|object| Arc::clone(object.value()))),
            Err(error) if error.limit().is_some() => Err(error),
            Err(_) => Ok(None),
        }
    }

    fn view<'v>(&self, value: &'v Value) -> View<'v> {
        match value {
            Value::Null => View::Null,
            Value::Boolean(value) => View::Boolean(*value),
            Value::Integer(value) => View::Integer(*value),
            Value::Real(value) => View::Real(value.value()),
            Value::Name(name) => View::Name(name.as_bytes()),
            Value::String(string) => View::String(string.as_bytes()),
            Value::Array(array) => View::Array(array.len()),
            Value::Dict(_) => View::Dictionary,
            Value::Stream(_) => View::Stream,
            Value::Reference(reference) => View::Reference(reference.number()),
        }
    }

    fn get<'v>(&self, dict: &'v Value, key: &[u8]) -> Option<&'v Value> {
        match dict {
            Value::Dict(dict) => dict.get(key),
            Value::Stream(stream) => stream.dict().get(key),
            _ => None,
        }
    }

    fn entries<'v>(&self, dict: &'v Value) -> Vec<(&'v [u8], &'v Value)> {
        let dict = match dict {
            Value::Dict(dict) => dict,
            Value::Stream(stream) => stream.dict(),
            _ => return Vec::new(),
        };
        dict.iter()
            .map(|(name, value)| (name.as_bytes(), value))
            .collect()
    }

    fn elements<'v>(&self, array: &'v Value) -> &'v [Value] {
        match array {
            Value::Array(array) => array.as_slice(),
            _ => &[],
        }
    }

    fn visit(&self, first: bool) -> Result<(), Error> {
        self.budget.poll()?;
        if first {
            self.budget.charge(LimitCode::ObjectCount, 1)?;
        }
        Ok(())
    }
}

/// Runs the structural check over the document's current version.
#[pyfunction]
pub fn structural_check(
    py: Python<'_>,
    document: &_Document,
) -> PyResult<(Vec<DefectRow>, bool, usize, usize, String)> {
    let version = document.reading()?;
    let budget = document.budget();
    py.detach(|| {
        let report = run(&version, &budget).map_err(|error| store_error(&error))?;
        let (report, declared) = report;
        let defects = report
            .defects
            .into_iter()
            .map(|defect| {
                let kind = defect.kind.as_str();
                (
                    defect.object,
                    defect.path,
                    defect.clause,
                    defect.spec,
                    kind,
                    defect.message,
                )
            })
            .collect();
        Ok((
            defects,
            report.truncated,
            report.objects,
            report.unchecked,
            declared.to_string(),
        ))
    })
}

fn run(version: &Version, budget: &Budget) -> Result<(Report, PdfVersion), Error> {
    let header = version.base().file().and_then(|index| {
        let text = header_version(&index.bytes()[index.origin()..])?;
        parse_version(text.as_bytes())
    });
    let mut store = Store {
        version,
        budget,
        trailer: Value::Dict(version.trailer().clone()),
        declared: header.unwrap_or(DEFAULT_VERSION),
    };
    // The catalog's /Version overrides an earlier header.
    let catalog = match version.trailer().get("Root") {
        Some(Value::Reference(root)) => store.resolve(root.number())?,
        _ => None,
    };
    if let Some(Value::Name(name)) = catalog
        .as_deref()
        .and_then(|catalog| store.get(catalog, b"Version"))
        && let Some(declared) = parse_version(name.as_bytes())
    {
        store.declared = store.declared.max(declared);
    }
    Ok((check(&store)?, store.declared))
}

fn parse_version(text: &[u8]) -> Option<PdfVersion> {
    let text = std::str::from_utf8(text).ok()?;
    let (major, minor) = text.split_once('.')?;
    Some(PdfVersion {
        major: major.parse().ok()?,
        minor: minor.parse().ok()?,
    })
}
