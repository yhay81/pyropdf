# Contributing to pyropdf

Thank you for helping. pyropdf is a Python library implemented in Rust;
[DESIGN.md](DESIGN.md) describes its foundation, and its principles F1 to F9
(section 3) bind every change.

## Report a bug or ask for a feature

Open an [issue](https://github.com/yhay81/pyropdf/issues/new/choose). For a
bug, include the pyropdf and Python versions, the smallest code that shows
it, and, when you can share it, the PDF. Report a vulnerability privately
instead, as [SECURITY.md](SECURITY.md) says.

## Set up

You need [uv](https://docs.astral.sh/uv/) and the Rust toolchain that
`rust-toolchain.toml` pins (rustup installs it on first use).

```bash
git clone https://github.com/yhay81/pyropdf.git
cd pyropdf
uv sync
```

`uv sync` builds the extension with maturin, and `uv run` rebuilds it when a
Rust source changes.

## Check a change

```bash
python3 tools/check.py
```

It runs the Python tests, ruff, ty, rustfmt, clippy, and the Rust tests, as
CI does, and prints one line per check. `python3 tools/check.py python` runs
only the Python checks. The documentation site builds with:

```bash
uv run --group docs zensical build -f mkdocs.yml -s
```

## Propose a change

- Work from an issue, and say in it that you are taking it. Larger changes
  start with a discussion in the issue.
- One issue, one branch, one pull request, whose body says
  `Closes #<issue>` and checks the issue's acceptance criteria.
- Add or update tests: Python tests in `tests/` are the public contract,
  and Rust tests cover invariants that no public API exposes.
- A change that claims a performance effect commits its measurement and
  script under `evidence/`.
- Public names need docstrings; `uv run python tools/api_reference.py`
  regenerates the API reference from them, and the documentation examples
  in `docs/` must run.
- Write code, comments, and documentation in English; the Japanese pages in
  `docs/ja/` translate the English ones.

Pull requests are squash-merged once CI passes and the maintainer has
reviewed them. By contributing, you agree that your contribution is licensed
under the [MIT License](LICENSE).
