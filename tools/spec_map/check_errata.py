"""Compare the recorded correction locations with a local pdf-association/pdf-issues checkout."""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
import tomllib
from html.parser import HTMLParser
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parent
ISSUE_URL = re.compile(r"https://github[.]com/pdf-association/pdf-issues/issues/([0-9]+)")


class CorrectedPage(HTMLParser):
    """Read issue identifiers in the published corrections, skipping navigation and comments."""

    def __init__(self) -> None:
        """Start before the clauses, where the publisher repeats the headings in a menu."""
        super().__init__()
        self.started = False
        self.fragment = ""
        self.sections: dict[str, int] = {}
        self.locations: set[tuple[int, str]] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Collect both marked edits and issue links in editor notes."""
        attributes = dict(attrs)
        if tag == "div" and "isostyle" in (attributes.get("class") or "").split():
            self.started = True
            self.sections.setdefault("", self.getpos()[0] - 1)
        if not self.started:
            return
        if re.fullmatch(r"h[1-6]", tag) and attributes.get("id"):
            self.fragment = "#" + str(attributes["id"])
            self.sections[self.fragment] = self.getpos()[0] - 1
        numbers = (attributes.get("data-issue") or "").split(",")
        link = ISSUE_URL.fullmatch(attributes.get("href") or "")
        if link:
            self.locations.add((int(link[1]), self.fragment))
        self.locations.update((int(number), self.fragment) for number in numbers if number.strip())


def published(checkout: Path, directories: list[str]) -> tuple[set[tuple[int, str]], dict[int, str]]:
    """Fingerprint the publisher's actual sections, including edits that keep an issue's ID and anchor."""
    excerpts: dict[int, dict[str, str]] = {}
    upstream: set[tuple[int, str]] = set()
    for name in directories:
        path = checkout / "docs" / name
        if not (path / "index.md").is_file():
            msg = f"missing errata index in {path}"
            raise ValueError(msg)
        for source in sorted(path.glob("clause*.md")):
            page = CorrectedPage()
            text = source.read_text(encoding="utf-8")
            page.feed(text)
            page.close()
            if not page.started:
                msg = f"unrecognized errata page format in {source}"
                raise ValueError(msg)
            lines = text.splitlines(keepends=True)
            starts = list(page.sections.items())
            sections = {
                fragment: "".join(lines[start:end])
                for (fragment, start), end in zip(starts, [v for _, v in starts[1:]] + [len(lines)], strict=True)
            }
            for issue, fragment in page.locations:
                location = f"{name}/{source.stem}.html{fragment}"
                upstream.add((issue, location))
                excerpts.setdefault(issue, {})[location] = sections[fragment]
    return upstream, {
        issue: hashlib.sha256("".join(loc + "\n" + text for loc, text in sorted(parts.items())).encode()).hexdigest()
        for issue, parts in excerpts.items()
    }


def main() -> int:
    """Report every new, removed or moved correction location, without accessing the network."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path, help="local checkout of pdf-association/pdf-issues")
    args = parser.parse_args()
    baseline = tomllib.loads((DIRECTORY / "baseline.toml").read_text(encoding="utf-8"))
    try:
        upstream, digests = published(args.checkout, baseline["sources"]["errata"]["directories"])
    except ValueError as error:
        parser.error(str(error))
    with (DIRECTORY / "errata.tsv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    recorded = {(int(row["issue"]), loc) for row in rows for loc in row["locations"].split()}
    changed = [
        row["issue"]
        for row in rows
        if row.get("section_sha256") and row["section_sha256"] != digests.get(int(row["issue"]))
    ]
    for issue in changed:
        sys.stdout.write(f"Changed: #{issue} corrected section content; review its behavior evidence.\n")
    for issue, location in sorted(upstream - recorded):
        sys.stdout.write(f"New: #{issue} {location}\n")
    for issue, location in sorted(recorded - upstream):
        sys.stdout.write(f"Removed: #{issue} {location}\n")
    return int(recorded != upstream or bool(changed))


if __name__ == "__main__":
    sys.exit(main())
