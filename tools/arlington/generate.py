"""Generate the object descriptors of pyropdf-schema from the Arlington PDF Model.

The model's TSV files at the commit that tools/arlington/model/COMMIT names
are vendored under tools/arlington/model/tsv (DESIGN.md section 8). This
script reads every file, one object type each, and writes the Rust tables
of crates/pyropdf-schema/src/generated.rs: per key, its types, the PDF
versions that introduced or deprecated it, whether it is required, indirect,
or inheritable, its default and possible values, its special cases, and the
objects it links to. Predicates (`fn:...`) are kept as the model writes them.
It also writes python/pyropdf/typed.pyi, the stub that declares the typed
view of each object type with its keys' Python types.

Run it from anywhere after changing the model. It needs only the standard
library, and the extension cannot build without its output:

    python3 tools/arlington/generate.py

With `--check` it writes nothing and fails when a generated file differs
from what the model generates. tests/test_arlington_schema.py runs that
check.
"""

from __future__ import annotations

import argparse
import csv
import keyword
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

MODEL = Path("tools/arlington/model")
RUST = Path("crates/pyropdf-schema/src/generated.rs")
STUB = Path("python/pyropdf/typed.pyi")

COMMAND = "python3 tools/arlington/generate.py"

COLUMNS = (
    "Key",
    "Type",
    "SinceVersion",
    "DeprecatedIn",
    "Required",
    "IndirectReference",
    "Inheritable",
    "DefaultValue",
    "PossibleValues",
    "SpecialCase",
    "Link",
    "Note",
)

# The basic types of the model, and their variants of `Kind`.
KINDS = {
    "array": "Array",
    "bitmask": "Bitmask",
    "boolean": "Boolean",
    "date": "Date",
    "dictionary": "Dictionary",
    "integer": "Integer",
    "matrix": "Matrix",
    "name": "Name",
    "name-tree": "NameTree",
    "null": "Null",
    "number": "Number",
    "number-tree": "NumberTree",
    "rectangle": "Rectangle",
    "stream": "Stream",
    "string": "String",
    "string-ascii": "StringAscii",
    "string-byte": "StringByte",
    "string-text": "StringText",
}

VERSION = re.compile(r"([12])\.(\d)")
NAME = re.compile(r"[A-Za-z0-9_-]+")


class ModelError(Exception):
    """The model holds what this generator does not understand."""


@dataclass(frozen=True)
class Typed:
    """One type of a key, with the columns that the model gives per type."""

    kind: str
    condition: str | None
    indirect: str
    default: str | None
    values: tuple[str, ...]
    special: str | None
    links: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True)
class Key:
    """One row of an object's TSV file."""

    name: str
    types: tuple[Typed, ...]
    since: str
    deprecated: str | None
    required: str
    inheritable: bool
    note: str


def split(text: str, separator: str) -> list[str]:
    """Split ``text`` at each ``separator`` outside parentheses and brackets."""
    parts: list[str] = []
    depth = 0
    start = 0
    for at, char in enumerate(text):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == separator and depth == 0:
            parts.append(text[start:at])
            start = at + 1
    parts.append(text[start:])
    return parts


def unbracket(text: str, where: str) -> str:
    """Return the text inside the brackets of ``text``."""
    if not (text.startswith("[") and text.endswith("]")):
        msg = f"{where}: expected a bracketed list, found {text!r}"
        raise ModelError(msg)
    return text[1:-1]


def per_type(text: str, count: int, where: str, *, scalar: bool) -> list[str]:
    """Return the entry of a per-type column for each of ``count`` types.

    An empty column is empty for every type. A column of one value that is
    not bracketed applies to every type when ``scalar`` allows it.
    """
    if not text:
        return [""] * count
    parts = split(text, ";")
    if len(parts) == 1 and scalar and not text.startswith("["):
        return [text] * count
    if len(parts) != count:
        msg = f"{where}: {len(parts)} entries for {count} types"
        raise ModelError(msg)
    return [unbracket(part, where) for part in parts]


def condition(text: str, where: str) -> str:
    """Return the Rust `Condition` of a TRUE, FALSE, or predicate entry."""
    if text == "TRUE":
        return "Always"
    if text == "FALSE":
        return "Never"
    if text.startswith("fn:"):
        return f"When({rust_str(text)})"
    msg = f"{where}: expected TRUE, FALSE, or a predicate, found {text!r}"
    raise ModelError(msg)


def version(text: str, where: str) -> str:
    """Return the Rust `Version` of a version entry such as ``1.7``."""
    match = VERSION.fullmatch(text)
    if match is None:
        msg = f"{where}: expected a PDF version, found {text!r}"
        raise ModelError(msg)
    return f"version({match.group(1)}, {match.group(2)})"


def wrapped(text: str, where: str) -> tuple[str, str | None]:
    """Return the name that ``text`` names, and the predicate around it."""
    name = text
    # The name is the last argument of each predicate, as `array` is in
    # `fn:SinceVersion(2.0,array)`, and predicates nest.
    while name.startswith("fn:") and "(" in name and name.endswith(")"):
        name = split(name[name.index("(") + 1 : -1], ",")[-1]
    if NAME.fullmatch(name) is None:
        msg = f"{where}: cannot find the name in {text!r}"
        raise ModelError(msg)
    return name, None if name == text else text


def read_object(path: Path) -> list[Key]:
    """Return the keys of one object's TSV file."""
    with path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.reader(file, delimiter="\t", quoting=csv.QUOTE_NONE))
    if not rows or tuple(rows[0]) != COLUMNS:
        msg = f"{path.name}: unexpected header {rows[0] if rows else None!r}"
        raise ModelError(msg)
    keys = []
    for line, row in enumerate(rows[1:], start=2):
        where = f"{path.name}:{line}"
        if len(row) != len(COLUMNS):
            msg = f"{where}: {len(row)} columns"
            raise ModelError(msg)
        entry = dict(zip(COLUMNS, row, strict=True))
        kinds = split(entry["Type"], ";")
        count = len(kinds)
        indirect = per_type(entry["IndirectReference"], count, where, scalar=True)
        defaults = (
            [entry["DefaultValue"]] if count == 1 else per_type(entry["DefaultValue"], count, where, scalar=False)
        )
        values = per_type(entry["PossibleValues"], count, where, scalar=False)
        specials = per_type(entry["SpecialCase"], count, where, scalar=False)
        links = per_type(entry["Link"], count, where, scalar=False)
        types = []
        for at, kind in enumerate(kinds):
            name, wrapper = wrapped(kind, where)
            if name not in KINDS:
                msg = f"{where}: unknown type {name!r}"
                raise ModelError(msg)
            types.append(
                Typed(
                    kind=name,
                    condition=wrapper,
                    indirect=indirect[at] or "FALSE",
                    default=defaults[at] or None,
                    values=tuple(split(values[at], ",")) if values[at] else (),
                    special=specials[at] or None,
                    links=tuple(wrapped(link, where) for link in split(links[at], ",")) if links[at] else (),
                )
            )
        inheritable = entry["Inheritable"]
        if inheritable not in {"TRUE", "FALSE"}:
            msg = f"{where}: Inheritable is {inheritable!r}"
            raise ModelError(msg)
        keys.append(
            Key(
                name=entry["Key"],
                types=tuple(types),
                since=entry["SinceVersion"],
                deprecated=entry["DeprecatedIn"] or None,
                required=entry["Required"],
                inheritable=inheritable == "TRUE",
                note=entry["Note"],
            )
        )
    return keys


def load(model: Path) -> tuple[str, dict[str, list[Key]]]:
    """Return the pinned commit and every object of the model, by name."""
    commit = (model / "COMMIT").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        msg = f"COMMIT is not a commit hash: {commit!r}"
        raise ModelError(msg)
    objects = {path.stem: read_object(path) for path in sorted((model / "tsv").glob("*.tsv"))}
    if not objects:
        msg = "the model has no TSV files"
        raise ModelError(msg)
    return commit, objects


def rust_str(text: str) -> str:
    """Return a Rust string literal of ``text``."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped = "".join(char if char.isprintable() else f"\\u{{{ord(char):x}}}" for char in escaped)
    return f'"{escaped}"'


def option(text: str | None) -> str:
    """Return a Rust `Option<&str>` of ``text``."""
    return "None" if text is None else f"Some({rust_str(text)})"


def rust(commit: str, objects: dict[str, list[Key]]) -> str:
    """Return the Rust tables of every object."""
    names = sorted(objects)
    index = {name: at for at, name in enumerate(names)}
    lines = [
        "// Generated by tools/arlington/generate.py from the Arlington PDF Model.",
        f"// Do not edit this file: change the model and run `{COMMAND}`.",
        "",
        "//! The descriptors of the Arlington PDF Model at commit",
        f"//! {commit}, one per object type, sorted by name.",
        "",
        "use crate::Condition::{Always, Never, When};",
        "use crate::Kind::*;",
        "use crate::{Condition, Key, Kind, Link, Object, Since, Typed, Version};",
        "",
        "/// The commit of the Arlington PDF Model that the descriptors come from.",
        f"pub const COMMIT: &str = {rust_str(commit)};",
        "",
        "const fn version(major: u8, minor: u8) -> Version {",
        "    Version { major, minor }",
        "}",
        "",
        "const fn since(major: u8, minor: u8) -> Since {",
        "    Since::Version(version(major, minor))",
        "}",
        "",
        "const fn link(object: u16, condition: Option<&'static str>) -> Link {",
        "    Link { object, condition }",
        "}",
        "",
        "const fn typed(",
        "    kind: Kind,",
        "    condition: Option<&'static str>,",
        "    indirect: Condition,",
        "    default: Option<&'static str>,",
        "    values: &'static [&'static str],",
        "    special: Option<&'static str>,",
        "    links: &'static [Link],",
        ") -> Typed {",
        "    Typed { kind, condition, indirect, default, values, special, links }",
        "}",
        "",
        "const fn key(",
        "    name: &'static str,",
        "    types: &'static [Typed],",
        "    since: Since,",
        "    deprecated: Option<Version>,",
        "    required: Condition,",
        "    inheritable: bool,",
        "    note: &'static str,",
        ") -> Key {",
        "    Key { name, types, since, deprecated, required, inheritable, note }",
        "}",
        "",
        "/// Every object type of the model, sorted by name.",
        f"pub static OBJECTS: [Object; {len(names)}] = [",
    ]
    for name in names:
        lines.append(f"    Object {{ name: {rust_str(name)}, keys: &[")
        for key in objects[name]:
            where = f"{name}.tsv {key.name}"
            types = []
            for typed in key.types:
                links = []
                for target, wrapper in typed.links:
                    if target not in index:
                        msg = f"{where}: links to unknown object {target!r}"
                        raise ModelError(msg)
                    links.append(f"link({index[target]}, {option(wrapper)})")
                values = ", ".join(rust_str(value) for value in typed.values)
                types.append(
                    f"typed({KINDS[typed.kind]}, {option(typed.condition)}, "
                    f"{condition(typed.indirect, where)}, {option(typed.default)}, &[{values}], "
                    f"{option(typed.special)}, &[{', '.join(links)}])"
                )
            since = (
                f"Since::When({rust_str(key.since)})"
                if key.since.startswith("fn:")
                else version(key.since, where).replace("version", "since", 1)
            )
            deprecated = "None" if key.deprecated is None else f"Some({version(key.deprecated, where)})"
            lines.append(
                f"        key({rust_str(key.name)}, &[{', '.join(types)}], {since}, {deprecated}, "
                f"{condition(key.required, where)}, {'true' if key.inheritable else 'false'}, {rust_str(key.note)}),"
            )
        lines.append("    ] },")
    lines.append("];")
    return "\n".join(lines) + "\n"


# The Python type of each kind in a view's stub: what a read returns and a
# write takes. A container that links to object types is their views instead.
STUB_TYPES = {
    "array": "_Array",
    "bitmask": "int",
    "boolean": "bool",
    "date": "str",
    "dictionary": "_Dictionary",
    "integer": "int",
    "matrix": "_Matrix",
    "name": "str",
    "name-tree": "_Dictionary",
    "null": "None",
    "number": "float",
    "number-tree": "_Dictionary",
    "rectangle": "tuple[float, float, float, float]",
    "stream": "_Stream",
    "string": "bytes",
    "string-ascii": "str",
    "string-byte": "bytes",
    "string-text": "str",
}
CONTAINERS = frozenset({"array", "dictionary", "stream"})


ARRAY_KEY = re.compile(r"\d+\*?|\*")


def base(name: str, keys: list[Key]) -> str:
    """Return the view base of an object type, as python/pyropdf/typed.py chooses it.

    The model does not say whether an object type is a dictionary, a stream,
    or an array. An array's keys are positions, `N*` for repeated elements,
    or `*`, and a stream's keys include Length, Filter, and DecodeParms.
    """
    names = {key.name for key in keys}
    if all(ARRAY_KEY.fullmatch(key) for key in names) and ("Array" in name or any(key[0].isdigit() for key in names)):
        return "ArrayView"
    return "StreamView" if {"Length", "Filter", "DecodeParms"} <= names else "DictionaryView"


def python_name(name: str) -> str:
    """Return the class name of an object type, as python/pyropdf/typed.py names it."""
    return "ThreeD" + name[2:] if name.startswith("3D") else name


def stub_type(key: Key, shadowed: set[str], aliases: set[str]) -> str:
    """Return the Python type of a key in a view's stub.

    A view class whose name is also an attribute of the class being declared
    is named through an alias, added to ``aliases``, since the attribute
    shadows it in the class body.
    """
    parts: list[str] = []
    for typed in key.types:
        if typed.kind in CONTAINERS and typed.links:
            names = [python_name(target) for target, _ in typed.links]
            aliases.update(name for name in names if name in shadowed)
            names = [f"_View{name}" if name in shadowed else name for name in names]
        else:
            names = [STUB_TYPES[typed.kind]]
        parts.extend(name for name in names if name not in parts)
    if key.required != "TRUE" and "None" not in parts:
        parts.append("None")
    return " | ".join(parts)


def stub(commit: str, objects: dict[str, list[Key]]) -> str:
    """Return the stub of python/pyropdf/typed.py, with a view class per object type."""
    lines = [
        "# Generated by tools/arlington/generate.py from the Arlington PDF Model.",
        f"# Do not edit this file: change the model and run `{COMMAND}`.",
        f"# The model's commit is {commit}.",
        "",
        "from typing import Any, ClassVar, TypeAlias",
        "",
        "from pyropdf._geometry import Matrix as _Matrix",
        "from pyropdf._objects import Array as _Array",
        "from pyropdf._objects import Dictionary as _Dictionary",
        "from pyropdf._objects import Stream as _Stream",
        "",
        "TYPES: tuple[str, ...]",
        "",
        "class View:",
        "    _type: ClassVar[str]",
        "    def __init__(self, obj: _Dictionary | _Array, /) -> None: ...",
        "    @property",
        "    def obj(self) -> _Dictionary | _Array: ...",
        "    def __getitem__(self, key: str | int, /) -> Any: ...",
        "    def __setitem__(self, key: str | int, value: object, /) -> None: ...",
        "    def __delitem__(self, key: str | int, /) -> None: ...",
        "    def __contains__(self, key: object, /) -> bool: ...",
        "    def __eq__(self, other: object, /) -> bool: ...",
        "    def __hash__(self) -> int: ...",
        "",
        "class DictionaryView(View):",
        "    def __init__(self, obj: _Dictionary, /) -> None: ...",
        "    @property",
        "    def obj(self) -> _Dictionary: ...",
        "",
        "class StreamView(DictionaryView):",
        "    def __init__(self, obj: _Stream, /) -> None: ...",
        "    @property",
        "    def obj(self) -> _Stream: ...",
        "",
        "class ArrayView(View):",
        "    def __init__(self, obj: _Array, /) -> None: ...",
        "    @property",
        "    def obj(self) -> _Array: ...",
    ]
    aliases: set[str] = set()
    for name in sorted(objects, key=python_name):
        keys = [
            key
            for key in objects[name]
            if key.name.isidentifier() and not keyword.iskeyword(key.name) and key.name != "obj"
        ]
        shadowed = {key.name for key in keys}
        attributes = [f"    {key.name}: {stub_type(key, shadowed, aliases)}" for key in keys]
        lines.extend(["", f"class {python_name(name)}({base(name, objects[name])}):", *(attributes or ["    ..."])])
    lines.append("")
    lines.extend(f"_View{name}: TypeAlias = {name}" for name in sorted(aliases))
    return "\n".join(lines) + "\n"


def read(path: Path) -> str | None:
    """Return the text of a file, or None if it does not exist."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def main(argv: list[str] | None = None) -> int:
    """Write the generated files, or with --check report the stale ones."""
    parser = argparse.ArgumentParser(description="Generate pyropdf-schema's descriptors from the Arlington PDF Model.")
    parser.add_argument("--check", action="store_true", help="write nothing; fail when a generated file is stale")
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository root (default: %(default)s)")
    args = parser.parse_args(argv)
    root: Path = args.root
    try:
        commit, objects = load(root / MODEL)
        generated = {RUST: rust(commit, objects), STUB: stub(commit, objects)}
    except ModelError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    stale = [path for path, text in generated.items() if read(root / path) != text]
    if args.check:
        for path in stale:
            sys.stderr.write(f"{path.as_posix()} is stale; run `{COMMAND}`\n")
        return 1 if stale else 0
    for path in stale:
        (root / path).write_text(generated[path], encoding="utf-8", newline="\n")
        sys.stdout.write(f"wrote {path.as_posix()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
