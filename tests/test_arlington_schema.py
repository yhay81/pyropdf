"""pyropdf-schema's descriptors are generated from the pinned Arlington PDF Model.

tools/arlington/generate.py reads the model's TSV files, vendored under
tools/arlington/model, and writes crates/pyropdf-schema/src/generated.rs and
the stub of the typed views, python/pyropdf/typed.pyi (DESIGN.md section 8).
The Rust tests of the crate sample the descriptors; these tests check that
the committed tables are the model's, whole.
"""

from __future__ import annotations

import csv
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "tools/arlington/generate.py"
MODEL = Path("tools/arlington/model")
GENERATED = Path("crates/pyropdf-schema/src/generated.rs")
STUB = Path("python/pyropdf/typed.pyi")
COMMAND = "python3 tools/arlington/generate.py"

OBJECT = re.compile(r'    Object \{ name: "([^"]+)", keys: &\[$')
KEY = re.compile(r'        key\("((?:[^"\\]|\\.)*)", ')


def generate(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the generator on a repository root, from that root."""
    command = [sys.executable, str(GENERATOR), "--root", str(root), *args]
    return subprocess.run(command, capture_output=True, check=False, cwd=root, text=True)  # noqa: S603 - fixed arguments


def copy_model(root: Path) -> Path:
    """Copy the model and the generated files to another root, and return it."""
    shutil.copytree(ROOT / MODEL, root / MODEL)
    for path in (GENERATED, STUB):
        (root / path).parent.mkdir(parents=True)
        shutil.copyfile(ROOT / path, root / path)
    return root


def described() -> dict[str, list[str]]:
    """Return the keys of every descriptor of the generated file, by object."""
    objects: dict[str, list[str]] = {}
    keys: list[str] = []
    for line in (ROOT / GENERATED).read_text(encoding="utf-8").splitlines():
        if match := OBJECT.match(line):
            assert match.group(1) not in objects
            keys = objects.setdefault(match.group(1), [])
        elif match := KEY.match(line):
            keys.append(match.group(1).encode().decode("unicode_escape"))
    return objects


def test_the_generated_tables_are_current() -> None:
    result = generate(ROOT, "--check")
    assert result.returncode == 0, result.stderr


def test_every_file_and_row_of_the_model_has_a_descriptor() -> None:
    files = sorted((ROOT / MODEL / "tsv").glob("*.tsv"))
    objects = described()

    assert len(files) == len(objects) == 613
    for path in files:
        with path.open(encoding="utf-8", newline="") as file:
            rows = list(csv.reader(file, delimiter="\t", quoting=csv.QUOTE_NONE))
        assert objects[path.stem] == [row[0] for row in rows[1:]], path.name
    commit = (ROOT / MODEL / "COMMIT").read_text(encoding="utf-8").strip()
    assert f'pub const COMMIT: &str = "{commit}";' in (ROOT / GENERATED).read_text(encoding="utf-8")


def test_the_check_fails_when_the_model_changes(tmp_path: Path) -> None:
    root = copy_model(tmp_path)
    catalog = root / MODEL / "tsv/Catalog.tsv"
    catalog.write_text(catalog.read_text(encoding="utf-8").replace("Table 29", "Table 28"), encoding="utf-8")

    result = generate(root, "--check")

    assert result.returncode == 1
    assert result.stderr == f"{GENERATED.as_posix()} is stale; run `{COMMAND}`\n"
    assert generate(root).returncode == 0
    assert "Table 28" in (root / GENERATED).read_text(encoding="utf-8")


def test_the_check_fails_when_the_stub_is_stale(tmp_path: Path) -> None:
    root = copy_model(tmp_path)
    (root / STUB).unlink()

    result = generate(root, "--check")

    assert result.returncode == 1
    assert result.stderr == f"{STUB.as_posix()} is stale; run `{COMMAND}`\n"


def test_a_model_the_generator_does_not_understand_is_refused(tmp_path: Path) -> None:
    root = copy_model(tmp_path)
    catalog = root / MODEL / "tsv/Catalog.tsv"
    text = catalog.read_text(encoding="utf-8")
    cases = [
        (text.replace("Version\tname\t", "Version\tvector\t"), "unknown type 'vector'"),
        (text.replace("Pages\tdictionary\t", "Pages\tdictionary;integer\t"), "1 entries for 2 types"),
        (text.replace("[PageTreeNodeRoot]", "[NoSuchObject]"), "links to unknown object 'NoSuchObject'"),
    ]
    for broken, message in cases:
        assert broken != text
        catalog.write_text(broken, encoding="utf-8")
        result = generate(root, "--check")
        assert result.returncode == 1
        assert message in result.stderr
