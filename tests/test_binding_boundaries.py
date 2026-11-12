"""Check the panic boundary and test coverage boundaries of the Python extension."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from rust_bindings import entry_points, guarded, mask_rust

ROOT = Path(__file__).resolve().parents[1] / "crates" / "pyropdf-python" / "src"


TEST_ATTRIBUTE = re.compile(r"#\s*!?\s*\[\s*(?:test|cfg\s*\(\s*test\s*,?\s*\))\s*\]")


def test_extension_has_no_unrunnable_rust_tests() -> None:
    manifest = tomllib.loads((ROOT.parent / "Cargo.toml").read_text(encoding="utf-8"))
    if manifest["lib"].get("test", True):
        return
    found = []
    for path in sorted(ROOT.rglob("*.rs")):
        source = mask_rust(path.read_text(encoding="utf-8"))
        for match in TEST_ATTRIBUTE.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            found.append(f"{path.relative_to(ROOT)}:{line}")
    assert not found, "move Rust tests to a crate with a test target: " + ", ".join(found)


def test_all_native_entry_points_have_a_boundary() -> None:
    missing = []
    count = 0
    for path in sorted(ROOT.rglob("*.rs")):
        source = path.read_text(encoding="utf-8")
        for entry in entry_points(source):
            count += 1
            if not guarded(source, entry):
                line = source[: entry.start].count("\n") + 1
                missing.append(f"{path.relative_to(ROOT)}:{line}: {entry.name}")
    assert count, "the audit must discover the native binding"
    assert missing == [], "entry points outside the panic boundary: " + ", ".join(missing)


@pytest.mark.parametrize("attribute", ["#[pyfunction]", '#[pyo3::pyfunction(name = "render")]'])
@pytest.mark.parametrize(
    "body",
    [
        "engine::render()",
        "engine::render(); crate::errors::boundary(|| Ok(()))?",
        "crate::errors::boundary(|| Ok(()))?; engine::render()",
        "crate::errors::boundary(engine::make_work())",
        'let example = "crate::errors::boundary(|| { engine::render() })?"; engine::render()',
        "/* crate::errors::boundary(|| { engine::render() })? */ engine::render()",
    ],
)
def test_the_audit_rejects_work_outside_a_function_boundary(attribute: str, body: str) -> None:
    source = f"{attribute}\nfn render() -> PyResult<()> {{ {body} }}"
    entries = entry_points(source)
    assert len(entries) == 1
    assert not guarded(source, entries[0])


def test_the_audit_checks_every_method_and_ignores_literals_and_comments() -> None:
    source = """
        /* outer /* #[pyfunction] fn fake() {} */ comment */
        const EXAMPLE: &str = r##"#[pymethods] impl Fake { fn fake() {} }"##;
        #[pymethods]
        impl Renderer {
            #[new]
            fn new() -> PyResult<Self> {
                crate::errors::boundary(|| Self { value: '}' })
            }
            #[getter]
            fn pixels<'a>(&'a self) -> PyResult<&'a [u8]> {
                engine::pixels()
            }
        }
    """
    entries = entry_points(source)
    assert [entry.name for entry in entries] == ["new", "pixels"]
    assert [guarded(source, entry) for entry in entries] == [True, False]


def test_a_function_item_is_invoked_inside_the_boundary() -> None:
    source = "#[pyfunction] fn create() -> PyResult<Document> { crate::errors::boundary(Document::new) }"
    entries = entry_points(source)
    assert len(entries) == 1
    assert guarded(source, entries[0])
