"""Compare the recorded correction locations with a local pdf-association/pdf-issues checkout."""

from __future__ import annotations

import argparse
import csv
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
        self.locations: set[tuple[int, str]] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Collect both marked edits and issue links in editor notes."""
        attributes = dict(attrs)
        if tag == "div" and "isostyle" in (attributes.get("class") or "").split():
            self.started = True
        if not self.started:
            return
        if re.fullmatch(r"h[1-6]", tag) and attributes.get("id"):
            self.fragment = "#" + str(attributes["id"])
        numbers = (attributes.get("data-issue") or "").split(",")
        link = ISSUE_URL.fullmatch(attributes.get("href") or "")
        if link:
            self.locations.add((int(link[1]), self.fragment))
        self.locations.update((int(number), self.fragment) for number in numbers if number.strip())


def main() -> int:
    """Report every new, removed or moved correction location, without accessing the network."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path, help="local checkout of pdf-association/pdf-issues")
    args = parser.parse_args()
    baseline = tomllib.loads((DIRECTORY / "baseline.toml").read_text(encoding="utf-8"))
    upstream: set[tuple[int, str]] = set()
    for name in baseline["sources"]["errata"]["directories"]:
        path = args.checkout / "docs" / name
        if not (path / "index.md").is_file():
            parser.error(f"missing errata index in {path}")
        for source in sorted(path.glob("clause*.md")):
            page = CorrectedPage()
            page.feed(source.read_text(encoding="utf-8"))
            page.close()
            if not page.started:
                parser.error(f"unrecognized errata page format in {source}")
            upstream.update((issue, f"{name}/{source.stem}.html{fragment}") for issue, fragment in page.locations)
    with (DIRECTORY / "errata.tsv").open(encoding="utf-8", newline="") as stream:
        recorded = {
            (int(row["issue"]), location)
            for row in csv.DictReader(stream, delimiter="\t")
            for location in row["locations"].split()
        }
    for issue, location in sorted(upstream - recorded):
        sys.stdout.write(f"New: #{issue} {location}\n")
    for issue, location in sorted(recorded - upstream):
        sys.stdout.write(f"Removed: #{issue} {location}\n")
    return int(recorded != upstream)


if __name__ == "__main__":
    sys.exit(main())
