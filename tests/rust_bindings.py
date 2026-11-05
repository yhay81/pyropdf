"""Locate Rust binding bodies for the panic-boundary audit.

This scans balanced delimiters, ignoring comments and literals, so a string,
comment, or nested helper cannot supply the boundary required of an entry point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TOKEN = re.compile(
    r'//[^\n]*|/\*|(?:br|cr|r)(?P<hashes>\#*)".*?"(?P=hashes)'
    r'|(?:b|c)?"(?:\\.|[^"\\])*"'
    r"|b?'(?:\\(?:u\{[\da-fA-F]+\}|x[\da-fA-F]{2}|.)|[^'\\\n])'",
    re.DOTALL,
)
ATTRIBUTE = re.compile(r"#\s*\[\s*(?:pyo3::)?(pyfunction|pymethods)\b")
FUNCTION = re.compile(r"\bfn\s+(\w+)")


def mask_rust(source: str) -> str:
    """Replace comments and literals with spaces, retaining their line breaks."""
    masked = list(source)
    position = 0
    while match := TOKEN.search(source, position):
        start, end = match.span()
        if match[0] == "/*":
            depth = 1
            while depth:
                nested = re.search(r"/\*|\*/", source[end:])
                if nested is None:
                    msg = "unclosed Rust block comment"
                    raise ValueError(msg)
                depth += 1 if nested[0] == "/*" else -1
                end += nested.end()
        for index in range(start, end):
            if masked[index] not in "\r\n":
                masked[index] = " "
        position = end
    return "".join(masked)


def closing(source: str, opening: int) -> int:
    """Find a balanced closing delimiter in masked Rust."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = [pairs[source[opening]]]
    for index in range(opening + 1, len(source)):
        character = source[index]
        if character in pairs:
            stack.append(pairs[character])
        elif character in pairs.values():
            if not stack or character != stack.pop():
                msg = "unbalanced Rust delimiter"
                raise ValueError(msg)
            if not stack:
                return index
    msg = "unclosed Rust delimiter"
    raise ValueError(msg)


@dataclass(frozen=True)
class EntryPoint:
    """Offsets into a function, keeping its original signature and body."""

    name: str
    start: int
    parameters_end: int
    opening: int
    end: int


def _function(masked: str, match: re.Match[str]) -> EntryPoint:
    parameters = masked.index("(", match.end())
    parameters_end = closing(masked, parameters)
    opening = masked.index("{", parameters_end)
    return EntryPoint(match[1], match.start(), parameters_end, opening, closing(masked, opening))


def entry_points(source: str) -> list[EntryPoint]:
    """Find pyfunctions and every method in a pymethods block."""
    masked = mask_rust(source)
    entries: list[EntryPoint] = []
    for attribute in ATTRIBUTE.finditer(masked):
        if attribute[1] == "pyfunction":
            function = FUNCTION.search(masked, attribute.end())
            assert function is not None
            entries.append(_function(masked, function))
        else:
            opening = masked.index("{", attribute.end())
            end = closing(masked, opening)
            position = opening + 1
            while function := FUNCTION.search(masked, position, end):
                entry = _function(masked, function)
                entries.append(entry)
                position = entry.end + 1
    return entries


def guarded(source: str, entry: EntryPoint) -> bool:
    """Require the whole body, not just one call, to be inside the boundary."""
    body = mask_rust(source)[entry.opening + 1 : entry.end].strip()
    call = re.match(r"crate::errors::boundary\s*\(", body)
    if call is None:
        return False
    end = closing(body, call.end() - 1)
    arguments = body[call.end() : end].lstrip()
    callback = arguments.startswith("||") or re.fullmatch(r"(?:\w+::)*\w+", arguments) is not None
    return callback and body[end + 1 :].strip() in {"", "?"}
