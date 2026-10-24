"""The corpus keeps its records.

The README of tests/assets/real_world records the source and license of every
corpus file, which DESIGN.md section 12 brings over with the corpus, and every
license must allow redistribution. damage.sha256 there records the SHA-256 of
every damaged input that pylopdf's generators derived from the corpus at
884af0db, and the helper must reproduce each one. Since junk-prefix-512 keeps
every byte of its file, these hashes pin the corpus files too.
"""

from __future__ import annotations

import hashlib
import re
from functools import cache
from typing import NamedTuple

import pytest
from corpus import ASSETS, DAMAGE, MEASURED, NAMES, damaged

# A record's license names one of these, each of which allows redistributing an
# unmodified file with the attribution that the record gives.
REDISTRIBUTABLE = re.compile(
    r"public[ -]domain|CC BY 4\.0|Government Standard Terms of Use 2\.0|LPPL|BSD 3-Clause",
    re.IGNORECASE,
)

# The target of a Markdown link, without its fragment.
LINK = re.compile(r"\]\(([^)#]*)[^)]*\)")


class Record(NamedTuple):
    """The source and license that the README records for a file."""

    source: str
    license: str


def readme() -> str:
    """Return the text of the corpus README."""
    return (ASSETS / "README.md").read_text(encoding="utf-8")


@cache
def records() -> dict[str, Record]:
    """Return the record of each file in the README's table of files."""
    table = readme().split("\n## Files\n", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in table.splitlines() if line.startswith("|")]
    found: dict[str, Record] = {}
    # The first two rows are the header and the delimiter.
    for row in rows[2:]:
        name, source, license_, _coverage = (cell.strip() for cell in row.strip("|").split("|"))
        name = name.strip("`")
        assert name not in found, f"README.md records {name} twice"
        found[name] = Record(source, license_)
    return found


@cache
def recorded_digests() -> dict[tuple[str, str], str]:
    """Return the recorded SHA-256 of each damaged input, by kind and file name."""
    digests: dict[tuple[str, str], str] = {}
    for line in (ASSETS / "damage.sha256").read_text(encoding="ascii").splitlines():
        digest, path = line.split("  ")
        kind, name = path.split("/")
        digests[kind, name] = digest
    return digests


@pytest.mark.parametrize("name", NAMES)
def test_every_file_has_a_record_that_allows_redistribution(name: str) -> None:
    record = records().get(name)
    assert record is not None, f"README.md records no source or license for {name}"
    assert record.source, f"README.md records no source for {name}"
    assert REDISTRIBUTABLE.search(record.license), f"{name}: {record.license!r} names no redistributable license"
    if "BSD" in record.license:
        # Copies must retain the license's notice, so the record links to its text.
        texts = [target for target in LINK.findall(record.license) if "://" not in target]
        assert texts, f"{name}: {record.license!r} links to no license text"


def test_every_record_names_a_corpus_file() -> None:
    assert sorted(set(records()) - set(NAMES)) == []


def test_every_relative_link_of_the_readme_resolves() -> None:
    """The license texts that records link to, among others, are in the repository."""
    targets = [target for target in LINK.findall(readme()) if target and "://" not in target]
    assert [target for target in targets if not (ASSETS / target).exists()] == []


def test_damage_records_cover_every_file_under_every_kind() -> None:
    derived = {(kind, name) for kind in DAMAGE for name in NAMES}
    recorded = set(recorded_digests())
    assert sorted(derived - recorded) == [], "damage.sha256 records no SHA-256 for these inputs"
    assert sorted(recorded - derived) == [], "damage.sha256 records inputs that the helper does not derive"


def test_the_measured_kinds_make_the_112_inputs_of_design_section_2_2() -> None:
    assert len(NAMES) * len(MEASURED) == 112


@pytest.mark.parametrize("kind", DAMAGE)
@pytest.mark.parametrize("name", NAMES)
def test_damage_reproduces_pylopdf(name: str, kind: str) -> None:
    digest = hashlib.sha256(damaged(name, kind)).hexdigest()
    assert digest == recorded_digests().get((kind, name))
