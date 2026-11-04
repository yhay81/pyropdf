# ruff: noqa: INP001 - a script, not a package.
"""Time ``Page.words`` over the corpus under each mode of ``timing-toggle.patch``.

    git apply evidence/stretch-reload/timing-toggle.patch
    uv sync
    uv run --no-sync python evidence/stretch-reload/time_words.py

Each round runs every mode in a process of its own, and each document keeps
its best time of the rounds. The script writes ``results.txt`` beside itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import warnings
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORPUS = sorted((HERE.parents[1] / "tests" / "assets" / "real_world").glob("*.pdf"))
#: The patch's modes: the stretch left in, the shape filter, and every font loaded again.
MODES = ("off", "on", "always")
ROUNDS = 7


def _read_every_page() -> None:
    """Write the seconds that each document's words take, as JSON."""
    import pyropdf  # noqa: PLC0415 - only the child processes read a page.

    warnings.simplefilter("ignore")
    seconds = {}
    for path in CORPUS:
        with pyropdf.open(path.read_bytes()) as document:
            start = time.perf_counter()
            for page in document.pages:
                page.words()
            seconds[path.stem] = time.perf_counter() - start
    sys.stdout.write(json.dumps(seconds))


def main() -> None:
    """Run the rounds and write their best times."""
    best: dict[str, dict[str, float]] = {mode: {} for mode in MODES}
    for _ in range(ROUNDS):
        for mode in MODES:
            environment = {**os.environ, "PYROPDF_SCRATCH_STRETCH": mode}
            output = subprocess.check_output([sys.executable, __file__, "child"], env=environment)  # noqa: S603
            for stem, seconds in json.loads(output).items():
                best[mode][stem] = min(best[mode].get(stem, float("inf")), seconds)
    lines = []
    for path in CORPUS:
        off, on, always = (best[mode][path.stem] * 1e3 for mode in MODES)
        lines.append(
            f"{path.stem[:36]:36} off {off:8.1f} ms  on {on:8.1f} ms ({on / off - 1:+6.1%})"
            f"  always {always:8.1f} ms ({always / off - 1:+6.1%})"
        )
    off, on, always = (sum(best[mode].values()) * 1e3 for mode in MODES)
    lines.append(
        f"{'total':36} off {off:8.1f} ms  on {on:8.1f} ms ({on / off - 1:+6.1%})"
        f"  always {always:8.1f} ms ({always / off - 1:+6.1%})"
    )
    (HERE / "results.txt").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    if sys.argv[1:] == ["child"]:
        _read_every_page()
    else:
        main()
