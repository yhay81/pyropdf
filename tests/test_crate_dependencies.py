"""The crates depend on each other in the order of DESIGN.md section 11.

Section 11 lists the crates from the store to the binding, and a crate depends
only on the crates listed before it. The store and the schema reach no
interpreter, writer, or model runtime, and no crate but the binding reaches
PyO3. The check reads `cargo metadata` with every feature and every platform,
and follows a crate's normal, build, and development dependencies through
everything they reach.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import deque
from pathlib import Path
from typing import TypedDict

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The crates of section 11, in its order.
CRATES = ("pyropdf-store", "pyropdf-schema", "pyropdf-engine", "pyropdf-python")

# Third-party crates, and the first crate of section 11 that may reach them:
# the engine's interpreter, writer, model runtime, and signing stack, and PyO3, whose pyo3-*
# crates count as pyo3.
FIRST_ALLOWED = {
    "hayro": "pyropdf-engine",
    "hayro-interpret": "pyropdf-engine",
    "hayro-svg": "pyropdf-engine",
    "krilla": "pyropdf-engine",
    "krilla-svg": "pyropdf-engine",
    "rten": "pyropdf-engine",
    # The signing stack of section 11.
    "cms": "pyropdf-engine",
    "x509-cert": "pyropdf-engine",
    "p256": "pyropdf-engine",
    "p384": "pyropdf-engine",
    "pkcs8": "pyropdf-engine",
    "pyo3": "pyropdf-python",
}


class DependencyKind(TypedDict):
    """A kind of dependency: None for a normal one, "build", or "dev"."""

    kind: str | None


class Dependency(TypedDict):
    """A resolved dependency: the ID of the package it names, and its kinds."""

    pkg: str
    dep_kinds: list[DependencyKind]


class Node(TypedDict):
    """A package of the resolved graph and its dependencies."""

    id: str
    deps: list[Dependency]


class Resolve(TypedDict):
    """The resolved dependency graph."""

    nodes: list[Node]


class Package(TypedDict):
    """A package: its ID in the resolved graph, and its name."""

    id: str
    name: str


class Metadata(TypedDict):
    """The parts of `cargo metadata --format-version 1` that the check reads."""

    packages: list[Package]
    workspace_members: list[str]
    resolve: Resolve


def first_allowed(name: str) -> str | None:
    """Return the first crate of section 11 that may reach a crate, or None if any may."""
    if name.startswith("pyo3-"):
        name = "pyo3"
    return FIRST_ALLOWED.get(name)


def crate_violations(member: str, names: dict[str, str], deps: dict[str, list[Dependency]]) -> list[str]:
    """Describe each dependency path from one crate that points against section 11."""
    crate = names[member]
    rank = CRATES.index(crate)
    found = []
    paths = {member: crate}
    queue = deque([member])
    while queue:
        current = queue.popleft()
        for dep in deps[current]:
            # The development dependencies of a crate count only for that crate.
            development = all(kind["kind"] == "dev" for kind in dep["dep_kinds"])
            if dep["pkg"] in paths or (development and current != member):
                continue
            name = names[dep["pkg"]]
            path = paths[dep["pkg"]] = f"{paths[current]} -> {name}"
            allowed = first_allowed(name)
            if name in CRATES and CRATES.index(name) > rank:
                found.append(f"{path}: section 11 lists {crate} before {name}")
            elif allowed is not None and CRATES.index(allowed) > rank:
                found.append(
                    f"{path}: section 11 lists {crate} before {allowed}, the first crate that may reach {name}"
                )
            else:
                queue.append(dep["pkg"])
    return found


def violations(metadata: Metadata) -> list[str]:
    """Describe each dependency path in a workspace that points against section 11."""
    names = {package["id"]: package["name"] for package in metadata["packages"]}
    deps = {node["id"]: node["deps"] for node in metadata["resolve"]["nodes"]}
    found = []
    for member in metadata["workspace_members"]:
        if names[member] in CRATES:
            found += crate_violations(member, names, deps)
        else:
            found.append(f"{names[member]}: not a crate of DESIGN.md section 11")
    return found


def cargo_metadata() -> Metadata:
    """Return the metadata of the workspace with every feature on."""
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.fail("the dependency check needs cargo on PATH")
    manifest = ROOT / "Cargo.toml"
    command = [cargo, "metadata", "--format-version", "1", "--all-features", "--manifest-path", str(manifest)]
    result = subprocess.run(command, capture_output=True, check=False, text=True)  # noqa: S603 - fixed arguments
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def workspace(*edges: tuple[str, str, str | None]) -> Metadata:
    """Return metadata for the crates of section 11 and the given dependencies.

    Each edge is (dependent, dependency, kind), where kind is None, "build", or
    "dev". Every crate whose name starts with "pyropdf-" is a workspace member.
    """
    names = sorted({*CRATES, *(name for edge in edges for name in edge[:2])})
    deps: dict[str, list[Dependency]] = {name: [] for name in names}
    for dependent, dependency, kind in edges:
        deps[dependent].append({"pkg": dependency, "dep_kinds": [{"kind": kind}]})
    return {
        "packages": [{"id": name, "name": name} for name in names],
        "workspace_members": [name for name in names if name.startswith("pyropdf-")],
        "resolve": {"nodes": [{"id": name, "deps": deps[name]} for name in names]},
    }


def paths(found: list[str]) -> list[str]:
    """Return the dependency paths of the violations, without their reasons."""
    return [violation.split(":")[0] for violation in found]


def test_the_workspace_follows_section_11() -> None:
    found = violations(cargo_metadata())
    assert not found, "\n".join(found)


def test_dependencies_in_the_order_of_section_11_pass() -> None:
    metadata = workspace(
        ("pyropdf-store", "hayro-syntax", None),
        ("pyropdf-schema", "pyropdf-store", None),
        ("pyropdf-engine", "pyropdf-store", None),
        ("pyropdf-engine", "pyropdf-schema", None),
        ("pyropdf-engine", "hayro", None),
        ("hayro", "hayro-interpret", None),
        ("hayro-interpret", "hayro-syntax", None),
        ("pyropdf-engine", "krilla", None),
        ("pyropdf-engine", "rten", None),
        ("pyropdf-python", "pyropdf-engine", None),
        ("pyropdf-python", "pyo3", None),
        ("pyo3", "pyo3-ffi", None),
        ("pyropdf-python", "pyo3-build-config", "build"),
    )
    assert violations(metadata) == []


@pytest.mark.parametrize(
    ("edges", "path"),
    [
        # The store reaches no interpreter, writer, or model runtime.
        ([("pyropdf-store", "hayro-interpret", None)], "pyropdf-store -> hayro-interpret"),
        ([("pyropdf-store", "krilla", None)], "pyropdf-store -> krilla"),
        ([("pyropdf-schema", "rten", None)], "pyropdf-schema -> rten"),
        # No crate but the binding reaches PyO3 or one of its crates.
        ([("pyropdf-store", "pyo3", None)], "pyropdf-store -> pyo3"),
        ([("pyropdf-engine", "pyo3-ffi", None)], "pyropdf-engine -> pyo3-ffi"),
        # No dependency points up the list.
        ([("pyropdf-store", "pyropdf-schema", None)], "pyropdf-store -> pyropdf-schema"),
        ([("pyropdf-schema", "pyropdf-engine", None)], "pyropdf-schema -> pyropdf-engine"),
        ([("pyropdf-engine", "pyropdf-python", None)], "pyropdf-engine -> pyropdf-python"),
        # Paths through third-party crates count, and so do build and development dependencies.
        ([("pyropdf-store", "numpy", None), ("numpy", "pyo3", None)], "pyropdf-store -> numpy -> pyo3"),
        ([("pyropdf-schema", "pyo3-build-config", "build")], "pyropdf-schema -> pyo3-build-config"),
        ([("pyropdf-store", "hayro", "dev")], "pyropdf-store -> hayro"),
    ],
)
def test_a_dependency_against_section_11_fails(edges: list[tuple[str, str, str | None]], path: str) -> None:
    assert paths(violations(workspace(*edges))) == [path]


def test_development_dependencies_count_only_for_their_crate() -> None:
    # The schema reaches the store but not the store's development dependencies.
    metadata = workspace(("pyropdf-schema", "pyropdf-store", None), ("pyropdf-store", "krilla", "dev"))
    assert paths(violations(metadata)) == ["pyropdf-store -> krilla"]


def test_a_crate_outside_section_11_fails() -> None:
    metadata = workspace(("pyropdf-extra", "pyropdf-store", None))
    assert violations(metadata) == ["pyropdf-extra: not a crate of DESIGN.md section 11"]
