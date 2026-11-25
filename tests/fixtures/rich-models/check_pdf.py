"""Run an installed Arlington CLI on the independent, authored and saved PDF models."""

from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pyropdf


def main(executable: str) -> None:
    """Require four explicit extension-aware passes for each specification."""
    cases = runpy.run_path(str(Path(__file__).resolve().parents[2] / "test_rich_models.py"))
    with TemporaryDirectory() as directory:
        for model in (cases["_STEP"], cases["_GLTF"]):
            kind, level, *_ = model
            paths = [Path(directory) / f"{kind}-{label}.pdf" for label in ("source", "authored", "full", "incremental")]
            source = cases["_foreign_pdf"](model)
            paths[0].write_bytes(source)
            doc, _ = cases["_author"](model)
            with doc:
                doc.save(paths[1])
            with pyropdf.open(source) as doc:
                doc.pages[0].annotations[0].contents = "Saved"
                doc.save(paths[2])
                doc.save(paths[3], incremental=True)
            cmd = [executable, "--flavour", "arlington2.0", "--extensions", f"ISO_TS_{level}", "--format", "text"]
            result = subprocess.run([*cmd, *map(str, paths)], capture_output=True, text=True, check=True)  # noqa: S603
            verdicts = [line.split()[0] for line in result.stdout.splitlines() if line.strip()]
            if verdicts != ["PASS"] * len(paths):
                msg = f"Unexpected Arlington results for {kind}: {result.stdout}\n{result.stderr}"
                raise ValueError(msg)
            sys.stdout.write(result.stdout)


if __name__ == "__main__":
    main(sys.argv[1])
