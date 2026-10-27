"""The registry of limit codes, limits.toml, generates the files that name the codes.

The registry is the one source of limit codes (DESIGN.md section 9).
tools/generate_limits.py writes the Rust enum, the Python mapping, and the
table of the security guide from it, and the first test fails when one of
them is stale.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, get_args

import pytest

from pyropdf import _limit_codes

if TYPE_CHECKING:
    from typing import Any

ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "tools" / "generate_limits.py"
REGISTRY = Path("limits.toml")
GENERATED = (
    Path("crates/pyropdf-store/src/budget/code.rs"),
    Path("python/pyropdf/_limit_codes.py"),
    Path("docs/limit-codes.md"),
)
COMMAND = "uv run python tools/generate_limits.py"

# The codes of pylopdf at 884af0dbf21973cfd434a9eca4e769de62fcc25c that pyropdf
# has, which keep their names: opening and the structure of the document,
# decoding streams, the data handed to the interpreter, content operations,
# the low-level object model, saving, pixmaps, and passwords.
PYLOPDF_CODES = (
    "file_size",
    "page_count",
    "object_count",
    "object_depth",
    "decompressed_size",
    "page_content_size",
    "total_decompressed_size",
    "decompression_unverifiable",
    "stream_filter_count",
    "interpretation_size",
    "content_operation_count",
    "content_stream_size",
    "content_input_size",
    "stream_data_size",
    "object_value_depth",
    "pdf_output_size",
    "pixmap_output_size",
    "password_input_size",
)

# One code that follows every rule of the registry.
VALID = '[codes.file_size]\nmessage = "the input is too large"\nunit = "bytes"\nreached = "The input is too large."\n'


def generate(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the generator on a repository root, from that root."""
    command = [sys.executable, str(GENERATOR), "--root", str(root), *args]
    return subprocess.run(command, capture_output=True, check=False, cwd=root, text=True)  # noqa: S603 - fixed arguments


def copy_registry(root: Path) -> Path:
    """Copy the registry and the generated files to another root, and return it."""
    for path in (REGISTRY, *GENERATED):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, root / path)
    return root


def test_the_generated_files_are_current() -> None:
    result = generate(ROOT, "--check")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("path", GENERATED, ids=Path.as_posix)
def test_the_check_fails_when_a_generated_file_is_stale(tmp_path: Path, path: Path) -> None:
    root = copy_registry(tmp_path)
    with (root / path).open("a", encoding="utf-8") as file:
        file.write("\n")
    result = generate(root, "--check")
    assert result.returncode == 1
    assert result.stderr == f"{path.as_posix()} is stale; run `{COMMAND}`\n"


@pytest.mark.parametrize("path", GENERATED, ids=Path.as_posix)
def test_the_check_fails_when_a_generated_file_is_missing(tmp_path: Path, path: Path) -> None:
    root = copy_registry(tmp_path)
    (root / path).unlink()
    result = generate(root, "--check")
    assert result.returncode == 1
    assert result.stderr == f"{path.as_posix()} is stale; run `{COMMAND}`\n"


def test_the_check_fails_when_the_registry_changes(tmp_path: Path) -> None:
    root = copy_registry(tmp_path)
    registry = (root / REGISTRY).read_text(encoding="utf-8")
    (root / REGISTRY).write_text(registry.replace("default = 16", "default = 17"), encoding="utf-8")
    result = generate(root, "--check")
    assert result.returncode == 1
    assert result.stderr.splitlines() == [
        f"crates/pyropdf-store/src/budget/code.rs is stale; run `{COMMAND}`",
        f"docs/limit-codes.md is stale; run `{COMMAND}`",
    ]


def test_the_generator_writes_what_the_check_expects(tmp_path: Path) -> None:
    root = copy_registry(tmp_path)
    for path in GENERATED:
        (root / path).unlink()
    result = generate(root)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [f"wrote {path.as_posix()}" for path in GENERATED]
    for path in GENERATED:
        assert (root / path).read_text(encoding="utf-8") == (ROOT / path).read_text(encoding="utf-8")
    assert generate(root, "--check").returncode == 0
    # A second run finds nothing to write.
    assert generate(root).stdout == ""


@pytest.mark.parametrize(
    ("registry", "problem"),
    [
        ("", "limits.toml: the registry has no codes"),
        ("[codes", "limits.toml: "),
        (VALID + "[other]\n", "limits.toml: unknown top-level keys: other"),
        (VALID.replace("file_size", "FileSize"), "FileSize: a code is lowercase words joined by underscores"),
        (VALID.replace("unit =", "units ="), "file_size: unknown fields: units"),
        (VALID.replace('reached = "The input is too large."\n', ""), "file_size: missing fields: reached"),
        (VALID.replace("too large", "too large."), "file_size: message does not follow the rule"),
        (VALID.replace("The input", "the input"), "file_size: reached does not follow the rule"),
        (VALID.replace('"bytes"', '"Bytes"'), "file_size: unit does not follow the rule"),
        (VALID.replace("too large.", "too large | wide."), "file_size: reached holds a quote, a backslash, a bar"),
        (VALID + "default = 0\n", "file_size: default is a positive integer that fits in 64 bits: 0"),
        (VALID + "default = true\n", "file_size: default is a positive integer that fits in 64 bits: True"),
        (VALID.replace('unit = "bytes"\n', "default = 1\n"), "file_size: a code with a default has a unit"),
        (VALID.replace("file_size", "x_1") + VALID.replace("file_size", "x1"), "x1: its Rust variant X1"),
    ],
)
def test_the_generator_refuses_a_registry_that_breaks_its_rules(tmp_path: Path, registry: str, problem: str) -> None:
    (tmp_path / REGISTRY).write_text(registry, encoding="utf-8")
    result = generate(tmp_path, "--check")
    assert result.returncode == 1
    assert problem in result.stderr


def test_the_registry_keeps_the_pylopdf_codes_that_phase_0_needs() -> None:
    codes = get_args(_limit_codes.LimitCode)
    assert [code for code in PYLOPDF_CODES if code not in codes] == []


def test_the_python_mapping_follows_the_registry() -> None:
    registry = tomllib.loads((ROOT / REGISTRY).read_text(encoding="utf-8"))["codes"]
    assert tuple(_limit_codes.LIMIT_CODES) == tuple(registry)
    assert get_args(_limit_codes.LimitCode) == tuple(registry)
    for code, reached in _limit_codes.LIMIT_CODES.items():
        assert reached == registry[code]["reached"]


def test_the_python_mapping_is_read_only() -> None:
    mapping: Any = _limit_codes.LIMIT_CODES
    with pytest.raises(TypeError):
        mapping["file_size"] = "changed"
