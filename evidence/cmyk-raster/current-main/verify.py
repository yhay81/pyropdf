# ruff: noqa: INP001, S603, S607, T201 - Standalone evidence script that executes installed reference tools.
"""Replay the native CMYK proposal, compare Ghostscript and check the default renderer."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]
BASE = "e7ada1354dff4041cdeed3d8ba56fe01b6186132"
PROPOSAL = "600842691d971fedf3628bcd4a397c2b725c047e"
SEED = "dcc1db05f5cb7b44df5f16306c304d536a3a2c48"
RESULT = "b21e9dd7f07930beb042ab050681d42932c190d7"
OPAQUE = 255
UPSTREAM = "https://github.com/LaurenzV/hayro.git"


def run(folder: Path, *args: str | Path) -> None:
    """Run a reference tool in the specified checkout."""
    subprocess.run(args, cwd=folder, check=True)


def tree(checkout: Path) -> str:
    """Return the object ID of the staged Git tree."""
    return subprocess.check_output(["git", "write-tree"], cwd=checkout, text=True).strip()


def build_probe(checkout: Path, work: Path, source: Path, name: str) -> Path:
    """Build the selected probe against this checkout using its dependency lock."""
    probe = work / name
    (probe / "src").mkdir(parents=True)
    shutil.copyfile(source, probe / "src/main.rs")
    (probe / "Cargo.toml").write_text(
        f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2024"\n'
        '[workspace]\n[dependencies]\nhayro = { path = "../hayro/hayro" }\n'
    )
    shutil.copyfile(checkout / "Cargo.lock", probe / "Cargo.lock")
    run(probe, "cargo", "build", "--release")
    return Path(os.environ["CARGO_TARGET_DIR"]) / "release" / name


def check_inks(checkout: Path, work: Path, native: Path, profile: Path) -> None:
    """Measure solid patch centres against the independent Ghostscript raster."""
    pdf = checkout / "tests/pdfs/custom/cmyk_patches.pdf"
    reference = work / "ghostscript.cmyk"
    run(
        work,
        "gs",
        "-q",
        "-dNOPAUSE",
        "-dBATCH",
        "-sDEVICE=bitcmyk",
        "-dGrayValues=256",
        "-r72",
        f"-sOutputICCProfile={profile}",
        "-dRenderIntent=1",
        f"-sOutputFile={reference}",
        pdf,
    )
    gs = reference.read_bytes()
    if len(gs) != 660 * 180 * 4:
        message = "Ghostscript produced an unexpected raster size"
        raise ValueError(message)
    patches = json.loads((HERE.parent / "patches.json").read_text())
    with (work / "patches.tsv").open("w", newline="") as file:
        report = csv.writer(file, delimiter="\t", lineterminator="\n")
        report.writerow(["profile", "patch", "native_cmyk", "ghostscript_cmyk", "max_delta"])
        for mode, value in [("default", "none"), ("icc", profile)]:
            raster = work / "cmyk_patches.cmyka"
            run(work, native, raster, value, pdf)
            data = raster.read_bytes()
            if len(data) != 660 * 180 * 5:
                message = "Native renderer produced an unexpected raster size"
                raise ValueError(message)
            for patch in patches:
                x = patch["x"] + patch["size"] // 2
                y = 180 - patch["y"] - patch["size"] // 2
                pixel = y * 660 + x
                ink = data[5 * pixel : 5 * pixel + 4]
                expected = gs[4 * pixel : 4 * pixel + 4]
                delta = max(abs(a - b) for a, b in zip(ink, expected, strict=True))
                if data[5 * pixel + 4] != OPAQUE:
                    message = f"Unexpected transparency for {patch['name']}"
                    raise ValueError(message)
                if delta and not patch["name"].startswith("rgb-"):
                    message = f"{mode} {patch['name']} produced {ink.hex()}, expected {expected.hex()}"
                    raise ValueError(message)
                report.writerow([mode, patch["name"], ink.hex(), expected.hex(), delta])


def check_default(checkout: Path, work: Path, baseline: Path, candidate: Path) -> None:
    """Compare the complete RGBA raster before and after applying the proposal."""
    custom = checkout / "tests/pdfs/custom"
    colr = PROJECT / "evidence/colr-glyphs/rendering-fixtures"
    inputs = [
        *sorted(custom.glob("cmyk*.pdf")),
        custom / "shading_mesh_same_dict.pdf",
        custom / "pattern_shading_radial_extend_interior.pdf",
        colr / "radial-gradient.pdf",
        colr / "clipped-shading.pdf",
        colr / "mask-1024-32.pdf",
        PROJECT / "tests/assets/text/hebrew-niqqud.pdf",
    ]
    with (HERE / "default-rgba.tsv").open() as reference:
        recorded = {row["fixture"] for row in csv.DictReader(reference, delimiter="\t")}
    if {pdf.name for pdf in inputs} != recorded:
        message = "Fixture list differs from the recorded default-renderer controls"
        raise ValueError(message)
    with (work / "default-rgba.tsv").open("w") as file:
        file.write("fixture\twidth\theight\trgba_sha256\n")
        for pdf in inputs:
            results = []
            for binary in (baseline, candidate):
                png = work / f"{binary.name}.png"
                run(work, binary, pdf, png, "1", "1")
                with Image.open(png) as image:
                    rgba = image.convert("RGBA")
                    results.append((rgba.width, rgba.height, hashlib.sha256(rgba.tobytes()).hexdigest()))
            if results[0] != results[1]:
                message = f"Default rendering changed for {pdf.name}: {results}"
                raise ValueError(message)
            width, height, digest = results[0]
            file.write(f"{pdf.name}\t{width}\t{height}\t{digest}\n")


def main() -> None:
    """Replay the resolution and save both comparison tables in a fresh directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scratch", type=Path)
    parser.add_argument("profile", type=Path, help="Ghostscript default_cmyk.icc or another output profile")
    args = parser.parse_args()
    profile = args.profile.resolve(strict=True)
    args.scratch.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="cmyk-", dir=args.scratch.resolve()))
    print(f"Measurements and checkout: {work}", flush=True)
    os.environ["CARGO_TARGET_DIR"] = str(Path(os.environ.get("CARGO_TARGET_DIR", work / "target")).resolve())
    checkout = work / "hayro"
    run(work, "git", "init", "-q", checkout)
    run(checkout, "git", "remote", "add", "origin", UPSTREAM)
    run(checkout, "git", "fetch", "--depth=128", "origin", BASE, PROPOSAL)
    run(checkout, "git", "checkout", "-q", "--detach", BASE)
    rgb_source = PROJECT / "evidence/colr-glyphs/hayro-probe/src/main.rs"
    baseline = build_probe(checkout, work, rgb_source, "base-rgba")
    run(
        checkout,
        "git",
        "-c",
        "user.name=CMYK rehearsal",
        "-c",
        "user.email=rehearsal@example.invalid",
        "merge",
        "--no-commit",
        "-Xours",
        PROPOSAL,
    )
    if tree(checkout) != SEED:
        message = "Unexpected common merge tree before applying the resolution"
        raise ValueError(message)
    run(checkout, "git", "apply", "--index", HERE / "reconcile.patch")
    if tree(checkout) != RESULT:
        message = "Patched Git tree differs from the reviewed resolution"
        raise ValueError(message)
    run(
        checkout,
        "cargo",
        "test",
        "--locked",
        "--release",
        "-p",
        "hayro-tests",
        "--test",
        "tests",
        "--",
        "cmyk::",
        "shading_mesh_same_dict",
        "pattern_shading_radial_extend_interior",
        "shading_operator_",
        "pattern_shading_",
    )
    candidate = build_probe(checkout, work, rgb_source, "candidate-rgba")
    native = build_probe(checkout, work, HERE / "native.rs", "native-cmyk")
    check_inks(checkout, work, native, profile)
    check_default(checkout, work, baseline, candidate)
    print(f"Verified colour patches and 13 unchanged default rasters in {work}")


if __name__ == "__main__":
    main()
