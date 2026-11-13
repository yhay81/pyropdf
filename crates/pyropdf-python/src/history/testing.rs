//! Private panic-injection hooks, called only by the Python regression tests.
//! They are also built into release wheels so CI tests the shipped artifact.
//! No normal operation reads a test flag or injects a panic; a test must
//! explicitly call a hook on its own document.

use std::panic::panic_any;
use std::sync::atomic::{AtomicBool, Ordering};

use pyropdf_store::settle::{Settler, Settlers, Structure};
use pyropdf_store::value::Value;

use super::*;

const MESSAGE: &str = "pyropdf injected test panic";

struct PanickingPayload;

impl Drop for PanickingPayload {
    fn drop(&mut self) {
        panic!("panic while dropping the injected payload");
    }
}

fn inject(payload: &str) -> ! {
    match payload {
        "str" => panic!("pyropdf injected test panic"),
        "string" => panic_any(String::from(MESSAGE)),
        "opaque" => panic_any(674_u32),
        "drop" => panic_any(PanickingPayload),
        _ => unreachable!("the test hook validates the payload"),
    }
}

/// Exercises attached/detached work, both transaction helpers, and a lock
/// that really becomes poisoned while the panic leaves its guard.
#[pyfunction]
#[pyo3(signature = (document, site, payload = "str"))]
pub fn _test_panic(
    py: Python<'_>,
    document: &_Document,
    site: &str,
    payload: &str,
) -> PyResult<()> {
    crate::errors::boundary(|| {
        if !matches!(payload, "str" | "string" | "opaque" | "drop") {
            return Err(PyValueError::new_err("unknown test panic payload"));
        }
        let write_then_panic = |transaction: &mut Transaction, budget: &Budget| {
            // The tests create a document whose catalog is object 1. A
            // missing rollback makes every subsequent catalog read fail.
            transaction
                .write(1, &Value::Null, budget)
                .map_err(|error| store_error(&error))?;
            inject(payload)
        };
        match site {
            "attached" => inject(payload),
            "detached" => py.detach(|| inject(payload)),
            "edit" => document.edit(py, write_then_panic),
            "nested_edit" => document.edit(py, |transaction, budget| {
                crate::errors::boundary(|| write_then_panic(transaction, budget))?
            }),
            "scratch" => document.scratch(py, write_then_panic),
            "locked" => {
                let _guard = lock(&document.history.open);
                inject(payload)
            }
            _ => Err(PyValueError::new_err("unknown test panic site")),
        }
    })?
}

struct PanicOnce(AtomicBool);

impl Settler for PanicOnce {
    fn settle(
        &self,
        transaction: &mut Transaction,
        _: &[u32],
        budget: &Budget,
    ) -> pyropdf_store::Result<()> {
        if self.0.swap(false, Ordering::Relaxed) {
            transaction.write(1, &Value::Null, budget)?;
            panic!("pyropdf injected settler panic");
        }
        Ok(())
    }
}

/// Installs a one-shot panicking settler on this test document only.
#[pyfunction]
pub fn _test_arm_panic_settler(document: &_Document) -> PyResult<()> {
    crate::errors::boundary(|| {
        document.document()?.set_settlers(Settlers::default().with(
            Structure::Appearance,
            Arc::new(PanicOnce(AtomicBool::new(true))),
        ));
        Ok(())
    })?
}

/// Makes a normal read/commit reach the settler installed by the test.
#[pyfunction]
pub fn _test_mark_for_panic(py: Python<'_>, document: &_Document) -> PyResult<()> {
    crate::errors::boundary(|| {
        document.edit(py, |transaction, budget| {
            transaction
                .mark(Structure::Appearance, 1, budget)
                .map_err(|error| store_error(&error))
        })
    })?
}
