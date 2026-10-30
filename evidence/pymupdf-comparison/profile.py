# ruff: noqa: INP001, T201 - a script, not a package; it prints its progress.
"""Profile pyropdf's rendering of the documents where it trails PyMuPDF most.

    cargo install rustfilt
    CARGO_PROFILE_RELEASE_DEBUG=line-tables-only uv sync --reinstall-package pyropdf
    uv run python evidence/pymupdf-comparison/profile.py

macOS only: ``/usr/bin/sample`` records the stacks. The extension needs line
tables to name its frames, hence the variable above, which changes no code;
``rustfilt`` demangles the frames. For each document and
resolution, a worker renders every page in a loop and reads its samples,
opening the document again each pass as ``bench.py`` does, while ``sample``
records its main thread. The script charges each sample's leaf frame to the
crate that defines it, sums whole phases of a page's rendering, and times each
page against PyMuPDF, from the environment that ``run.py`` installs, both with
the samples read as PyMuPDF's ``samples`` does and with the PNG that
``bench.py`` asks pyropdf for. It writes
``results/profile-<platform>.md`` and the numbers in the ``.json`` beside it;
the raw call graphs stay in ``target/pymupdf-comparison/profile/``.
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

DOCUMENTS = ("bill-hr815", "usrguide")
DPIS = (72, 144)
#: Seconds that ``sample`` records each case, at its default 1 ms interval.
SECONDS = 10
#: The longest function name the report shows whole.
WIDTH = 140
#: Per-page timings are the best of this many passes.
RUNS = 3
ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
TARGET = ROOT / "target" / "pymupdf-comparison"
CORPUS = ROOT / "tests" / "assets" / "real_world"

#: The group of a frame, by the first crate in its symbol, then its library.
CRATES = {
    "hayro_interpret": "hayro-interpret",
    "hayro_syntax": "hayro-syntax",
    "hayro": "hayro",
    "hayro_font": "fonts",
    "skrifa": "fonts",
    "read_fonts": "fonts",
    "font_types": "fonts",
    "hayro_jpeg2000": "image and stream decoding",
    "hayro_jbig2": "image and stream decoding",
    "hayro_ccitt": "image and stream decoding",
    "zune_jpeg": "image and stream decoding",
    "zune_core": "image and stream decoding",
    "zlib_rs": "image and stream decoding",
    "fdeflate": "image and stream decoding",
    "miniz_oxide": "image and stream decoding",
    "flate2": "image and stream decoding",
    "vello_cpu": "vello_cpu",
    "vello_common": "vello_cpu",
    "fearless_simd": "vello_cpu",
    "kurbo": "kurbo",
    "peniko": "vello_cpu",
    "pyropdf_engine": "pyropdf",
    "pyropdf_python": "pyropdf",
    "pyropdf_store": "pyropdf",
    "pyropdf_schema": "pyropdf",
    "pyo3": "pyropdf",
    "hayro_postscript": "fonts",
    "hayro_cmap": "fonts",
    "phf": "fonts",
    "moxcms": "hayro-interpret",
    "hashbrown": "hash maps",
    "siphasher": "hash maps",
    "rustc_hash": "hash maps",
}
LIBRARIES = {
    "libsystem_malloc.dylib": "malloc and free",
    "libsystem_platform.dylib": "memcpy and memset",
    "libsystem_kernel.dylib": "kernel",
}
#: Phases of one page, by a substring of the outermost frame that runs them.
#: A phase may run inside another; the glyph fills run inside hayro.
PHASES = {
    "page raster (pyropdf_engine::render::draw)": "pyropdf_engine::render::draw",
    "interpret and encode (hayro::render_into)": "hayro::render_into",
    "glyph fills (hayro::Renderer::fill_outline_run)": "<hayro::Renderer>::fill_outline_run",
    "rasterize (vello_cpu Dispatcher::rasterize)": "Dispatcher>::rasterize",
    "store, bridge, and open (pyropdf)": ("pyropdf_store::", "pyropdf_engine::bridge::", "_Document>::__pymethod_open"),
}
FRAME = re.compile(
    r"^(?P<prefix>[ +!:|]*)(?P<count>\d+) (?P<symbol>.+?)  \(in (?P<library>[^)]+)\).*?(?P<line>\S+:\d+)?$"
)
#: The frame whose self samples are split by source line: what it inlines.
INLINER = "pyropdf_engine::render::draw"
HASH = re.compile(r"::h[0-9a-f]{16}|\[[0-9a-f]+\]")
CRATE = re.compile(r"([a-z_][a-z0-9_]*)::")


def _render(path: Path, dpi: int, seconds: float) -> None:
    """Render every page of ``path`` in a loop for ``seconds``; print the passes."""
    import pyropdf  # noqa: PLC0415 - only the worker needs the extension

    passes = 0
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        with pyropdf.open(path) as doc:
            for page in doc.pages:
                bytes(page.pixmap(dpi=dpi))
        passes += 1
    print(passes)


def _pages(engine: str, path: Path, dpi: int) -> None:
    """Print, as JSON, the best of RUNS times of each page in milliseconds.

    ENGINE ``pymupdf`` and ``pyropdf`` read the samples; ``pyropdf-png``
    encodes the PNG that ``bench.py`` times.
    """
    if engine == "pymupdf":
        import pymupdf  # noqa: PLC0415 - installed only in run.py's environment

        doc = pymupdf.open(path)
        pages = list(doc)
        render = lambda page: page.get_pixmap(dpi=dpi).samples  # noqa: E731
    else:
        import pyropdf  # noqa: PLC0415

        doc = pyropdf.open(path)
        pages = list(doc.pages)
        if engine == "pyropdf-png":
            render = lambda page: page.pixmap(dpi=dpi).to_bytes()  # noqa: E731
        else:
            render = lambda page: bytes(page.pixmap(dpi=dpi))  # noqa: E731
    best = [float("inf")] * len(pages)
    for _ in range(RUNS):
        for index, page in enumerate(pages):
            start = time.perf_counter()
            render(page)
            best[index] = min(best[index], time.perf_counter() - start)
    print(json.dumps([value * 1000 for value in best]))


def _group(symbol: str, library: str) -> str:
    if library in LIBRARIES:
        return LIBRARIES[library]
    if not library.startswith("_core."):
        return "Python and other"
    # The first crate named, so that a sort of vello's tiles is vello's.
    crates = CRATE.findall(symbol)
    known = next((crate for crate in crates if crate in CRATES), None)
    if known is not None:
        return CRATES[known]
    if not crates or crates[0] in {"core", "alloc", "std"}:
        return "Rust std"
    return f"other ({crates[0]})"


def _call_graph(text: str) -> list[tuple[int, int, str, str, str]]:
    """Return the frames of the busiest thread: (depth, samples, symbol, library, line)."""
    threads: list[list[tuple[int, int, str, str, str]]] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("Call graph:"):
            inside = True
            continue
        if inside and not line.strip():
            break
        match = FRAME.match(line[4:]) if inside else None
        if match is None:
            if inside and "Thread_" in line:
                threads.append([])
            continue
        frame = (len(match["prefix"]), int(match["count"]), match["symbol"], match["library"], match["line"] or "")
        if threads:
            threads[-1].append(frame)
    return max(threads, key=lambda frames: frames[0][1] if frames else 0)


def _analyse(text: str) -> dict:
    """Return the samples per group (leaf frames), per phase, and per function."""
    frames = _call_graph(text)
    total = frames[0][1]
    groups: Counter[str] = Counter()
    leaves: Counter[str] = Counter()
    phases: Counter[str] = Counter()
    inlined: Counter[str] = Counter()
    # Each entry: depth, samples, the samples its children claim, and its phase.
    stack: list[list] = []

    def close(entry: list) -> None:
        own = entry[1] - entry[2]
        if own > 0:
            groups[_group(entry[3], entry[4])] += own
            leaves[f"{HASH.sub('', entry[3])} ({_group(entry[3], entry[4])})"] += own
            if entry[3].startswith(INLINER):
                inlined[entry[6]] += own

    for depth, count, symbol, library, line in frames:
        while stack and stack[-1][0] >= depth:
            close(stack.pop())
        if stack:
            stack[-1][2] += count
        active = {entry[5] for entry in stack}
        phase = next(
            (
                name
                for name, needle in PHASES.items()
                if any(part in symbol for part in ((needle,) if isinstance(needle, str) else needle))
            ),
            None,
        )
        if phase is not None and phase not in active:
            phases[phase] += count
        stack.append([depth, count, 0, symbol, library, phase, line])
    while stack:
        close(stack.pop())
    # The thread's own frame is not a function; drop its line.
    leaves = Counter({name: value for name, value in leaves.items() if not name.startswith("Thread_")})
    return {
        "samples": total,
        "groups": dict(groups.most_common()),
        "phases": dict(phases),
        "functions": dict(leaves.most_common(25)),
        "inlined": dict(inlined.most_common(5)),
    }


def _python(engine: str) -> Path:
    if engine != "pymupdf":
        return Path(sys.executable)
    python = TARGET / "pymupdf-env" / "bin" / "python"
    if not python.exists():
        sys.path.insert(0, str(HERE))
        from run import _environment  # noqa: PLC0415 - run.py installs the pinned release

        return _environment("pymupdf")
    return python


def _per_page(document: str, dpi: int) -> dict[str, list[float]]:
    path = CORPUS / f"{document}.pdf"
    out = {}
    for engine in ("pymupdf", "pyropdf", "pyropdf-png"):
        result = subprocess.run(  # noqa: S603 - this script and a fixed Python
            [str(_python(engine)), __file__, "--pages", engine, str(path), str(dpi)],
            check=True,
            capture_output=True,
            text=True,
        )
        out[engine] = json.loads(result.stdout)
    return out


def _sample(document: str, dpi: int) -> tuple[dict, int]:
    """Sample a worker rendering ``document``; return the analysis and its passes."""
    raw = TARGET / "profile" / f"{document}-{dpi}.txt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    worker = subprocess.Popen(  # noqa: S603 - this script
        [sys.executable, __file__, "--render", str(CORPUS / f"{document}.pdf"), str(dpi), str(SECONDS + 2)],
        stdout=subprocess.PIPE,
        text=True,
    )
    subprocess.run(  # noqa: S603 - a fixed system tool
        ["/usr/bin/sample", str(worker.pid), str(SECONDS), "-mayDie", "-file", str(raw)],
        check=True,
        capture_output=True,
    )
    passes, _ = worker.communicate()
    demangled = subprocess.run(  # noqa: S603 - a fixed tool
        ["rustfilt", "-i", str(raw)],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return _analyse(demangled), int(passes)


def _share(part: float, total: float) -> str:
    return f"{100 * part / total:.1f}%"


def _report(results: dict, system: str) -> list[str]:
    cases = list(results)
    lines = [
        f"# Rendering profile, {system}",
        "",
        f"Measured {datetime.now(UTC).date()} on {platform.platform()}, Python {platform.python_version()},",
        f"pyropdf from this checkout, sampled {SECONDS} s per case at 1 ms by `sample`.",
        "Shares are of the samples of the worker's main thread. `profile.py` describes the method.",
        "",
        "## Self time per group",
        "",
        "Each sample is charged to its leaf frame's crate. Frames inlined into a caller count for the caller.",
        "",
        "| Group | " + " | ".join(cases) + " |",
        "| --- |" + " ---: |" * len(cases),
    ]
    groups = sorted(
        {group for case in results.values() for group in case["profile"]["groups"]},
        key=lambda group: -sum(case["profile"]["groups"].get(group, 0) for case in results.values()),
    )
    for group in groups:
        cells = [
            _share(case["profile"]["groups"].get(group, 0), case["profile"]["samples"]) for case in results.values()
        ]
        lines.append(f"| {group} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Phases",
        "",
        "Inclusive samples of the outermost frame of each phase.",
        "",
        "| Phase | " + " | ".join(cases) + " |",
        "| --- |" + " ---: |" * len(cases),
    ]
    for phase in PHASES:
        cells = [
            _share(case["profile"]["phases"].get(phase, 0), case["profile"]["samples"]) for case in results.values()
        ]
        lines.append(f"| {phase} | " + " | ".join(cells) + " |")
    lines += ["", "## Pages", ""]
    lines += [
        "Best of three renders of each page, in ms, on a document opened once: every page, then the five",
        'where pyropdf spends the most time. PyMuPDF and pyropdf read the samples; "with PNG" is what',
        "`bench.py` times for pyropdf, `to_bytes()`, which encodes a PNG.",
        "",
    ]
    for case, result in results.items():
        mupdf, ours, png = (result["pages"][engine] for engine in ("pymupdf", "pyropdf", "pyropdf-png"))
        lines += [
            f"### {case}",
            "",
            "| Page | PyMuPDF | pyropdf | ratio | pyropdf with PNG | ratio |",
            "| ---: | ---: | ---: | ---: | ---: | ---: |",
            (
                f"| all | {sum(mupdf):.1f} | {sum(ours):.1f} | {sum(ours) / sum(mupdf):.2f}"
                f" | {sum(png):.1f} | {sum(png) / sum(mupdf):.2f} |"
            ),
        ]
        for index in sorted(range(len(ours)), key=lambda i: -ours[i])[:5]:
            lines.append(
                f"| {index + 1} | {mupdf[index]:.1f} | {ours[index]:.1f} | {ours[index] / mupdf[index]:.2f}"
                f" | {png[index]:.1f} | {png[index] / mupdf[index]:.2f} |"
            )
        lines.append("")
    lines += ["## Hottest functions", "", "Self samples, as shares of each case.", ""]
    for case, result in results.items():
        profile = result["profile"]
        lines += [f"### {case}", "", "| Function | Self |", "| --- | ---: |"]
        for name, value in list(profile["functions"].items())[:15]:
            shown = name if len(name) <= WIDTH else name[: WIDTH - 3] + "..."
            lines.append(f"| `{shown.replace('|', '/')}` | {_share(value, profile['samples'])} |")
        lines += ["", f"Self samples of `{INLINER}` by source line, which name what it inlines:", ""]
        lines += [f"- `{line}`: {_share(value, profile['samples'])}" for line, value in profile["inlined"].items()]
        lines.append("")
    return lines


def main() -> None:
    """Profile each document at each resolution and write the report."""
    if sys.argv[1:2] == ["--render"]:
        _render(Path(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]))
        return
    if sys.argv[1:2] == ["--pages"]:
        _pages(sys.argv[2], Path(sys.argv[3]), int(sys.argv[4]))
        return
    results = {}
    for document in DOCUMENTS:
        for dpi in DPIS:
            case = f"{document} {dpi} dpi"
            profile, passes = _sample(document, dpi)
            results[case] = {"passes": passes, "profile": profile, "pages": _per_page(document, dpi)}
            print(case, f"{profile['samples']} samples", f"{passes} passes")
    system = f"{platform.system().lower()}-{platform.machine().lower()}"
    out = HERE / "results" / f"profile-{system}"
    out.with_suffix(".json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    out.with_suffix(".md").write_text("\n".join(_report(results, system)), encoding="utf-8")


if __name__ == "__main__":
    main()
