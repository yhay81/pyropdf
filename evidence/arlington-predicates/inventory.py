# ruff: noqa: INP001 - a script that prints its report.
"""Inventory the predicates of the pinned Arlington PDF Model, for #216.

Reads the TSV files that tools/arlington/model vendors and prints, as
Markdown:

- each predicate function, how often the model calls it, how many keys
  call it, and in which columns;
- the class of each function: what the structural check needs to decide it;
- how many predicate cells each cumulative set of classes decides.

A cell is one column of one key. A set decides a cell when every function
that the cell calls is in the set or is advisory, since an advisory function
reports nothing.

    python3 evidence/arlington-predicates/inventory.py > evidence/arlington-predicates/results/inventory.md
"""

from __future__ import annotations

import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

MODEL = Path(__file__).resolve().parent.parent.parent / "tools/arlington/model"
FUNCTION = re.compile(r"fn:([A-Za-z0-9]+)\(")

# The classes in the order that the recommendation adds them. Each function
# of the model is in exactly one; the script fails on a function it does not
# know, so a new pin of the model updates this table.
CLASSES: dict[str, tuple[str, tuple[str, ...]]] = {
    "expressions": (
        (
            "The expression language: comparisons and arithmetic over the object's own keys and its "
            "parents (`@Key`, `parent::`), and the functions that only wrap an expression."
        ),
        ("Eval", "Not", "IsRequired", "IsPresent", "DefaultValue", "RequiredValue", "Contains"),
    ),
    "versions": (
        "The PDF version of the file and the extensions that `/Extensions` declares.",
        ("SinceVersion", "BeforeVersion", "IsPDFVersion", "Deprecated", "Extension"),
    ),
    "storage": (
        "Whether a value is stored directly or as an indirect object, and whether encryption applies to it.",
        ("MustBeDirect", "MustBeIndirect", "AlwaysUnencrypted", "IsEncryptedWrapper"),
    ),
    "values": (
        "Arithmetic on one value: lengths, bits, rectangle sizes, sort order.",
        (
            "ArrayLength",
            "BitsClear",
            "BitClear",
            "BitSet",
            "BitsSet",
            "RectWidth",
            "RectHeight",
            "StringLength",
            "ArraySortAscending",
            "IsLastInNumberFormatArray",
        ),
    ),
    "document": (
        (
            "Lookups across the document: name trees, destinations, the page count, the file size, "
            "field names, cycles, tagging, fonts and colorants named in dictionaries."
        ),
        (
            "InKeyMap",
            "InNameTree",
            "NumberOfPages",
            "PageProperty",
            "FileSize",
            "IsFieldName",
            "NoCycle",
            "IsPDFTagged",
            "NotStandard14Font",
            "KeyNameIsColorant",
            "HasSpotColorants",
            "HasProcessColorants",
        ),
    ),
    "lexical": (
        (
            "The lexical form of the source: whether a string was written in hex. hayro-syntax 0.8 "
            "decodes hex strings without keeping the form."
        ),
        ("IsHexString",),
    ),
    "content": (
        "Content streams or font programs: marked content and the glyphs a font holds.",
        ("FontHasLatinChars", "ImageIsStructContentItem", "PageContainsStructContentItems"),
    ),
}
ADVISORY = (
    (
        "Advisory functions that report nothing: what a reader ignores, what is meaningful, "
        "what an implementation chooses."
    ),
    ("Ignore", "IsMeaningful", "ImplementationDependent"),
)


def cells(model: Path) -> list[tuple[str, str, str, list[str]]]:
    """Return each predicate cell: its object, key, column, and calls."""
    found = []
    for path in sorted((model / "tsv").glob("*.tsv")):
        with path.open(encoding="utf-8", newline="") as file:
            rows = list(csv.reader(file, delimiter="\t", quoting=csv.QUOTE_NONE))
        for row in rows[1:]:
            for column, text in zip(rows[0], row, strict=True):
                if called := FUNCTION.findall(text):
                    found.append((path.stem, row[0], column, called))
    return found


def main() -> int:
    """Print the inventory."""
    commit = (MODEL / "COMMIT").read_text(encoding="utf-8").strip()
    found = cells(MODEL)
    classes = {name: label for label, (_, names) in CLASSES.items() for name in names}
    classes.update(dict.fromkeys(ADVISORY[1], "advisory"))

    calls: Counter[str] = Counter()
    keys: defaultdict[str, set[tuple[str, str]]] = defaultdict(set)
    columns: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for name_of_object, key, column, called in found:
        calls.update(called)
        for name in set(called):
            keys[name].add((name_of_object, key))
            columns[name][column] += 1
    unknown = sorted(set(calls) - set(classes))
    if unknown:
        sys.stderr.write(f"unclassified functions: {', '.join(unknown)}\n")
        return 1

    out = sys.stdout.write
    out(f"# Arlington predicates at {commit}\n\n")
    out(f"{len(calls)} functions, {sum(calls.values())} calls, {len(found)} predicate cells")
    out(f" in {len({(o, k) for o, k, _, _ in found})} keys.\n\n")
    out("| Function | Calls | Keys | Class | Columns (cells) |\n| --- | ---: | ---: | --- | --- |\n")
    for name, count in calls.most_common():
        spread = ", ".join(f"{column} {n}" for column, n in columns[name].most_common())
        out(f"| `{name}` | {count} | {len(keys[name])} | {classes[name]} | {spread} |\n")

    out("\n## Classes\n\n")
    for label, (description, _) in [*CLASSES.items(), ("advisory", ADVISORY)]:
        out(f"- **{label}**: {description}\n")

    out("\n## Cells decided by each cumulative set\n\n| Set | Cells decided | Share |\n| --- | ---: | ---: |\n")
    decided: set[str] = set(ADVISORY[1])
    labels: list[str] = []
    for label, (_, names) in CLASSES.items():
        decided.update(names)
        labels.append(label)
        count = sum(set(called) <= decided for _, _, _, called in found)
        out(f"| {' + '.join(labels)} | {count} | {count / len(found):.1%} |\n")

    out("\n## Cells left by the first four classes, by function\n\n")
    first = set(ADVISORY[1]).union(*(names for _, names in list(CLASSES.values())[:4]))
    left: Counter[str] = Counter()
    for _, _, _, called in found:
        for name in set(called) - first:
            left[name] += 1
    out("| Function | Cells |\n| --- | ---: |\n")
    for name, count in left.most_common():
        out(f"| `{name}` | {count} |\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
