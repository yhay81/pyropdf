"""Generate the files that the registry of limit codes, limits.toml, defines.

The registry is the one source of limit codes (DESIGN.md section 9). This
script writes the Rust enum `LimitCode`, the Python mapping of the codes that
`LimitError.code` carries, and the table of codes for the security guide.

Run it from anywhere after changing the registry:

    uv run python tools/generate_limits.py

With `--check` it writes nothing and fails when a generated file differs from
what the registry generates. tests/test_limit_codes.py runs that check.
"""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REGISTRY = Path("limits.toml")
RUST = Path("crates/pyropdf-store/src/budget/code.rs")
PYTHON = Path("python/pyropdf/_limit_codes.py")
GUIDE = Path("docs/limit-codes.md")

COMMAND = "uv run python tools/generate_limits.py"

FIELDS = frozenset({"message", "unit", "default", "reached"})
REQUIRED = frozenset({"message", "reached"})

CODE_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
UNIT_PATTERN = re.compile(r"[a-z]+(?: [a-z]+)*")
MESSAGE_PATTERN = re.compile(r"[a-z].*[^.\s]")
REACHED_PATTERN = re.compile(r"[A-Z`].*\.")
# Characters that would need escaping in a Rust or Python string literal, or
# that would break a Markdown table.
FORBIDDEN = re.compile(r'["\\|]|[^ -~]')

U64_MAX = (1 << 64) - 1

# rustfmt's maximum width, the width of comments in the repository's Rust
# code, and ruff's line length in pyproject.toml.
RUST_WIDTH = 100
COMMENT_WIDTH = 80
PYTHON_WIDTH = 120

BINARY_UNITS = ((1 << 30, "GiB"), (1 << 20, "MiB"), (1 << 10, "KiB"))


class RegistryError(Exception):
    """The registry breaks one of its rules."""


@dataclass(frozen=True)
class Code:
    """A limit code of the registry, with the fields that limits.toml describes."""

    name: str
    message: str
    reached: str
    unit: str | None = None
    default: int | None = None

    @property
    def variant(self) -> str:
        """Return the name of the code's variant in the Rust enum."""
        return "".join(part.capitalize() for part in self.name.split("_"))


def fail(name: str, problem: str) -> RegistryError:
    """Return the error for a code that breaks a rule of the registry."""
    return RegistryError(f"{REGISTRY}: {name}: {problem}")


def text_field(name: str, table: dict[str, object], key: str, pattern: re.Pattern[str]) -> str:
    """Return a field that holds one line of text, checked against its pattern."""
    value = table[key]
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise fail(name, f"{key} does not follow the rule of limits.toml: {value!r}")
    if FORBIDDEN.search(value):
        raise fail(name, f"{key} holds a quote, a backslash, a bar, or a character outside printable ASCII")
    return value


def parse_code(name: str, table: object) -> Code:
    """Return one code of the registry, checking each of its fields."""
    if not CODE_PATTERN.fullmatch(name):
        raise fail(name, "a code is lowercase words joined by underscores")
    if not isinstance(table, dict):
        raise fail(name, "a code is a table")
    keys = set(table)
    if unknown := keys - FIELDS:
        raise fail(name, f"unknown fields: {', '.join(sorted(unknown))}")
    if missing := REQUIRED - keys:
        raise fail(name, f"missing fields: {', '.join(sorted(missing))}")
    unit = text_field(name, table, "unit", UNIT_PATTERN) if "unit" in table else None
    default = table.get("default")
    if default is not None:
        if isinstance(default, bool) or not isinstance(default, int) or not 0 < default <= U64_MAX:
            raise fail(name, f"default is a positive integer that fits in 64 bits: {default!r}")
        if unit is None:
            raise fail(name, "a code with a default has a unit")
    return Code(
        name=name,
        message=text_field(name, table, "message", MESSAGE_PATTERN),
        reached=text_field(name, table, "reached", REACHED_PATTERN),
        unit=unit,
        default=default,
    )


def load(path: Path) -> list[Code]:
    """Read the registry and check its rules."""
    try:
        registry = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        msg = f"{REGISTRY}: {error}"
        raise RegistryError(msg) from error
    if unknown := set(registry) - {"codes"}:
        msg = f"{REGISTRY}: unknown top-level keys: {', '.join(sorted(unknown))}"
        raise RegistryError(msg)
    tables = registry.get("codes")
    if not isinstance(tables, dict) or not tables:
        msg = f"{REGISTRY}: the registry has no codes"
        raise RegistryError(msg)
    codes = [parse_code(name, table) for name, table in tables.items()]
    variants: dict[str, str] = {}
    for code in codes:
        if (other := variants.setdefault(code.variant, code.name)) != code.name:
            raise fail(code.name, f"its Rust variant {code.variant} is also the variant of {other}")
    return codes


def doc_comment(text: str, indent: str) -> list[str]:
    """Return a Rust doc comment that wraps the text at the comment width."""
    prefix = f"{indent}/// "
    return textwrap.wrap(
        text,
        width=COMMENT_WIDTH,
        initial_indent=prefix,
        subsequent_indent=prefix,
        break_long_words=False,
        break_on_hyphens=False,
    )


def match_arm(variant: str, value: str) -> list[str]:
    """Return a match arm as rustfmt writes it, in a block when it is too wide."""
    indent = " " * 12
    line = f"{indent}Self::{variant} => {value},"
    if len(line) <= RUST_WIDTH:
        return [line]
    return [f"{indent}Self::{variant} => {{", f"{indent}    {value}", f"{indent}}}"]


def const_fn(doc: str, signature: str, arms: list[tuple[str, str]]) -> list[str]:
    """Return a documented const function of `LimitCode` that matches on the code."""
    lines = [*doc_comment(doc, " " * 4), "    #[must_use]", f"    pub const fn {signature} {{", "        match self {"]
    for variant, value in arms:
        lines += match_arm(variant, value)
    return [*lines, "        }", "    }"]


def rust_integer(value: int) -> str:
    """Return a Rust integer literal with its digits grouped by three."""
    return f"{value:_}"


def rust(codes: list[Code]) -> str:
    """Return the Rust module of the enum `LimitCode`."""
    lines = [
        "// Generated by tools/generate_limits.py from limits.toml. Do not edit this",
        f"// file: change the registry and run `{COMMAND}`.",
        "",
        "//! The limit codes of the registry, limits.toml (DESIGN.md section 9).",
        "",
        "/// A limit that refuses an operation, named by a stable code.",
        "///",
        "/// [`LimitCode::as_str`] gives the code that Python's `LimitError.code`",
        "/// carries, which stays the same across releases. The variants follow the",
        "/// order of the registry.",
        "#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]",
        "#[non_exhaustive]",
        "pub enum LimitCode {",
    ]
    for code in codes:
        lines += [*doc_comment(code.reached, " " * 4), f"    {code.variant},"]
    lines += [
        "}",
        "",
        "impl LimitCode {",
        "    /// Every code, in the order of the registry.",
        f"    pub const ALL: [Self; {len(codes)}] = [",
        *(f"        Self::{code.variant}," for code in codes),
        "    ];",
        "",
    ]
    lines += const_fn(
        "The stable code, as Python's `LimitError.code` carries it.",
        "as_str(self) -> &'static str",
        [(code.variant, f'"{code.name}"') for code in codes],
    )
    lines.append("")
    lines += const_fn(
        "How a refusal with the code reads, before the amount and the limit that it may give.",
        "message(self) -> &'static str",
        [(code.variant, f'"{code.message}"') for code in codes],
    )
    lines.append("")
    lines += const_fn(
        "What the limit counts, in the plural, or `None` for a code whose refusals give no numbers.",
        "unit(self) -> Option<&'static str>",
        [(code.variant, "None" if code.unit is None else f'Some("{code.unit}")') for code in codes],
    )
    lines.append("")
    lines += const_fn(
        "The limit that applies unless `DocumentLimits` or the call sets another, or `None` for no limit.",
        "default_limit(self) -> Option<u64>",
        [(code.variant, "None" if code.default is None else f"Some({rust_integer(code.default)})") for code in codes],
    )
    lines.append("}")
    return "\n".join(lines) + "\n"


def python_entry(key: str, value: str) -> list[str]:
    """Return a dictionary entry of the Python mapping as ruff formats it."""
    indent = " " * 8
    line = f'{indent}"{key}": "{value}",'
    if len(line) <= PYTHON_WIDTH:
        return [line]
    inner = indent + " " * 4
    if len(inner) + len(value) + 2 <= PYTHON_WIDTH:
        return [f'{indent}"{key}": (', f'{inner}"{value}"', f"{indent}),"]
    # ruff keeps an implicit concatenation that does not fit on one line. Each
    # part but the last ends with the space that separates it from the next.
    parts = textwrap.wrap(
        value,
        width=PYTHON_WIDTH - len(inner) - 3,
        break_long_words=False,
        break_on_hyphens=False,
    )
    pieces = [f'{inner}"{part} "' for part in parts[:-1]] + [f'{inner}"{parts[-1]}"']
    return [f'{indent}"{key}": (', *pieces, f"{indent}),"]


def python(codes: list[Code]) -> str:
    """Return the Python module of the codes that `LimitError.code` carries."""
    lines = [
        '"""The stable codes that ``LimitError.code`` carries, from the registry ``limits.toml``.',
        "",
        "Generated by tools/generate_limits.py. Do not edit this file: change the registry and",
        f"run ``{COMMAND}``.",
        '"""',
        "",
        "from collections.abc import Mapping",
        "from types import MappingProxyType",
        "from typing import Final, Literal, TypeAlias",
        "",
        "#: A stable code that ``LimitError.code`` carries.",
        "LimitCode: TypeAlias = Literal[",
        *(f'    "{code.name}",' for code in codes),
        "]",
        "",
        "#: Each code and when an operation reaches it, in the order of the registry.",
        "LIMIT_CODES: Final[Mapping[LimitCode, str]] = MappingProxyType(",
        "    {",
    ]
    for code in codes:
        lines += python_entry(code.name, code.reached)
    lines += ["    }", ")"]
    return "\n".join(lines) + "\n"


def default_limit(code: Code) -> str:
    """Return the default limit of a code for the guide, with its unit."""
    if code.default is None:
        return "No limit" if code.unit is not None else "—"
    if code.unit == "bytes":
        for size, unit in BINARY_UNITS:
            if code.default % size == 0:
                return f"{code.default // size:,} {unit}"
    return f"{code.default:,} {code.unit}"


def markdown(codes: list[Code]) -> str:
    """Return the table of codes for the security guide."""
    lines = [
        "<!-- Generated by tools/generate_limits.py from limits.toml. Do not edit this",
        f"file: change the registry and run `{COMMAND}`. -->",
        "",
        "| Code | Default | Reached when |",
        "| --- | --- | --- |",
        *(f"| `{code.name}` | {default_limit(code)} | {code.reached} |" for code in codes),
    ]
    return "\n".join(lines) + "\n"


def read(path: Path) -> str | None:
    """Return the text of a file, or None if it does not exist."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def main(argv: list[str] | None = None) -> int:
    """Write the generated files, or with --check report the stale ones."""
    parser = argparse.ArgumentParser(description="Generate the files that the registry limits.toml defines.")
    parser.add_argument("--check", action="store_true", help="write nothing; fail when a generated file is stale")
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository root (default: %(default)s)")
    args = parser.parse_args(argv)
    root: Path = args.root
    try:
        codes = load(root / REGISTRY)
    except RegistryError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    outputs = {RUST: rust(codes), PYTHON: python(codes), GUIDE: markdown(codes)}
    stale = [path for path, text in outputs.items() if read(root / path) != text]
    if args.check:
        for path in stale:
            sys.stderr.write(f"{path.as_posix()} is stale; run `{COMMAND}`\n")
        return 1 if stale else 0
    for path in stale:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(outputs[path], encoding="utf-8", newline="\n")
        sys.stdout.write(f"wrote {path.as_posix()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
