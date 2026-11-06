"""The crates share one clippy configuration, which refuses unbudgeted growth.

DESIGN.md F6 and section 9: a collection that grows from input grows through
the budget, and `crates/clippy.toml` refuses the methods that grow the
standard collections and the store's values. Clippy reads the first
`clippy.toml` that it meets going up from a crate's manifest, so a crate's own
file replaces the shared one whole. Every crate adopts the shared
configuration, so none may keep a configuration of its own.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRATES = ROOT / "crates"
NAMES = ("clippy.toml", ".clippy.toml")


def own_files(directory: Path) -> list[Path]:
    """Return the clippy configurations that `directory` holds itself."""
    return [directory / name for name in NAMES if (directory / name).exists()]


def test_the_crates_read_the_shared_configuration() -> None:
    crates = sorted(manifest.parent for manifest in CRATES.glob("*/Cargo.toml"))
    assert {"pyropdf-store", "pyropdf-schema", "pyropdf-engine", "pyropdf-python"} <= {crate.name for crate in crates}
    for crate in crates:
        assert own_files(crate) == [], f"{crate.name} would not read crates/clippy.toml"


def test_nothing_outside_the_crates_reads_a_configuration() -> None:
    # A root file would bind fuzz/, the evidence crates, and the crates of a
    # checkout nested under .claude/, none of which tools/check.py lints.
    assert own_files(ROOT) == []
    assert own_files(CRATES) == [CRATES / "clippy.toml"]


def test_the_configuration_refuses_growth_of_collections_and_values() -> None:
    config = tomllib.loads((CRATES / "clippy.toml").read_text(encoding="utf-8"))
    paths = {entry["path"] for entry in config["disallowed-methods"]}
    assert {
        "std::vec::Vec::push",
        "std::string::String::push_str",
        "std::collections::HashMap::insert",
        "core::iter::Iterator::collect",
        "pyropdf_store::value::Array::push",
        "pyropdf_store::value::Array::insert",
        "pyropdf_store::value::Dict::insert",
    } <= paths
