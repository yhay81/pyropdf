"""Install a built distribution and exercise the installed package.

The release workflow runs this on every wheel and on the sdist, outside the
checkout's `python/` directory, so that the import finds the installed copy.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

_TEXT = "pyropdf artifact smoke test"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
#: The smoke page and the page that the smoke edit adds.
_SAVED_PAGES = 2


def _require(*, condition: bool, message: str) -> None:
    """Raise a smoke-test failure that optimized runs do not skip."""
    if not condition:
        raise RuntimeError(message)


def _text_pdf() -> bytes:
    """Return a one-page document that draws the smoke text with Helvetica."""
    stream = f"q 1 0 0 rg 0 0 20 20 re f Q BT /F1 18 Tf 36 100 Td ({_TEXT}) Tj ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 200] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("latin-1")
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("latin-1")
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("latin-1")
    return bytes(out)


def _run_smoke() -> None:
    """Open, extract, render, edit, save, and reopen with the installed package."""
    import pyropdf  # noqa: PLC0415  # Import only after the artifact is installed.

    if sysconfig.get_config_var("Py_GIL_DISABLED") == 1:
        is_gil_enabled = getattr(sys, "_is_gil_enabled", None)
        _require(
            condition=is_gil_enabled is not None and not is_gil_enabled(),
            message="importing pyropdf enabled the GIL",
        )

    source = _text_pdf()
    try:
        pyropdf.open(source, limits=pyropdf.DocumentLimits(max_file_size=1))
    except pyropdf.LimitError as error:
        _require(condition=error.code == "file_size", message=f"unexpected limit code {error.code}")
    else:
        msg = "the file size limit did not refuse the document"
        raise RuntimeError(msg)

    with pyropdf.open(source) as doc:
        # This private native hook throws a single Rust panic inside the binding.
        try:
            doc._core()._test_panic()  # noqa: SLF001
        except pyropdf.InternalError as error:
            _require(condition="pyropdf injected method panic" in str(error), message="unwind lost its message")
        else:
            msg = "the binding did not propagate the injected panic"
            raise RuntimeError(msg)
        page = doc.pages[0]
        _require(condition=_TEXT in page.text(), message="text extraction lost the smoke text")
        pixmap = page.pixmap()
        _require(condition=pixmap.pixel(10, 190) == (255, 0, 0), message="rendering lost the red square")
        png = pixmap.to_bytes("png")
        _require(condition=png.startswith(_PNG_SIGNATURE), message="rendering did not produce a PNG")
        with doc.transaction():
            doc.pages.new((200, 200))
        for incremental in (False, True):
            data = doc.to_bytes(incremental=incremental)
            if incremental:
                _require(condition=data.startswith(source), message="incremental save lost the original prefix")
            with pyropdf.open(data) as reopened:
                _require(condition=len(reopened.pages) == _SAVED_PAGES, message="saving lost the page edit")
                _require(condition=_TEXT in reopened.pages[0].text(), message="saving lost the smoke text")

    print(f"pyropdf {pyropdf.__version__} smoke test passed")  # noqa: T201


def _install(artifact: Path, target: Path) -> None:
    """Install one artifact, without dependencies, into an isolated directory."""
    subprocess.run(  # noqa: S603 - the artifact path comes from the release workflow.
        [sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(target), str(artifact)],
        check=True,
    )


def main() -> None:
    """Install each matching artifact in turn and smoke-test it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--pattern", default="*.whl")
    parser.add_argument("--run", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.run:
        _run_smoke()
        return
    artifacts = sorted(args.directory.glob(args.pattern))
    _require(condition=bool(artifacts), message=f"no artifact matches {args.pattern} in {args.directory}")
    for artifact in artifacts:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            _install(artifact, target)
            subprocess.run(  # noqa: S603 - runs this script with the installed package first on the path.
                [sys.executable, "-I", "-c", _RUNNER, str(target), str(Path(__file__).resolve())],
                check=True,
                cwd=directory,
            )


#: Put the installed package first on the path, then run `--run` in the same
#: interpreter, so that neither the checkout nor its `python/` directory is found.
_RUNNER = (
    "import runpy, sys; sys.path.insert(0, sys.argv[1]); "
    "sys.argv = [sys.argv[2], '.', '--run']; runpy.run_path(sys.argv[0], run_name='__main__')"
)


if __name__ == "__main__":
    main()
