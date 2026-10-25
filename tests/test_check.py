"""The check runner runs CI's commands and reports a failure by the lines that name it."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("check", ROOT / "tools" / "check.py")
assert SPEC is not None
assert SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check
SPEC.loader.exec_module(check)


def test_the_checks_are_the_ci_commands() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for name in check.CHECKS:
        assert " ".join(check.command(name, locked=True)) in workflow, name
    assert check.command("clippy", locked=False)[-3:] == ["--", "-D", "warnings"]


def test_selection_keeps_the_order_and_syncs_before_python_checks() -> None:
    assert check.select([]) == list(check.CHECKS)
    assert check.select(["clippy", "fmt"]) == ["fmt", "clippy"]
    assert check.select(["pytest"]) == ["sync", "pytest"]
    assert check.select(["rust"]) == ["fmt", "clippy", "cargo-test"]


def test_a_diagnostic_is_one_line_with_its_location_and_warnings_are_left_out() -> None:
    log = """\
   Compiling pyropdf-store v0.1.0
warning: unused import: `std::fmt`
 --> crates/pyropdf-store/src/lib.rs:1:5
error[E0308]: mismatched types
  --> crates/pyropdf-store/src/save.rs:10:5
   |
error: could not compile `pyropdf-store` (lib) due to 1 previous error
F401 [*] `os` imported but unused
 --> tools/check.py:3:8
Would reformat: tools/check.py
FAILED tests/test_check.py::test_x - assert 1 == 2
"""
    assert check.excerpt(log, 10) == [
        "error[E0308]: mismatched types (crates/pyropdf-store/src/save.rs:10:5)",
        "error: could not compile `pyropdf-store` (lib) due to 1 previous error",
        "F401 [*] `os` imported but unused (tools/check.py:3:8)",
        "Would reformat: tools/check.py",
        "FAILED tests/test_check.py::test_x - assert 1 == 2",
    ]


def test_a_panic_keeps_its_message() -> None:
    log = """\
---- save::tests::roundtrip stdout ----

thread 'save::tests::roundtrip' panicked at src/save.rs:40:9:
assertion `left == right` failed
  left: 1
test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out
"""
    assert check.excerpt(log, 10) == [
        "thread 'save::tests::roundtrip' panicked at src/save.rs:40:9: assertion `left == right` failed",
        "test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out",
    ]


def test_an_unrecognized_failure_shows_the_tail_and_many_failures_are_capped() -> None:
    assert check.excerpt("a\nb\nc\n", 2) == ["b", "c"]
    assert check.excerpt("".join(f"error: e{i}\n" for i in range(5)), 2) == ["error: e0", "error: e1", "... 3 more"]


def test_a_failed_sync_skips_the_python_checks_and_the_others_still_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    passing = (sys.executable, "-c", "print('compiled')")
    failing = (sys.executable, "-c", "import sys; print('error: boom'); sys.exit(1)")
    monkeypatch.setattr(check, "CHECKS", {"sync": failing, "pytest": passing, "fmt": passing})
    monkeypatch.setattr(check, "GROUPS", {"python": ("sync", "pytest"), "rust": ("fmt",)})
    monkeypatch.setattr(check, "LOG_DIR", tmp_path)
    assert check.main([]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("FAIL  sync ")
    assert lines[1:3] == ["      error: boom", "skip  pytest (sync failed)"]
    assert lines[3].startswith("ok    fmt ")
    assert lines[4:] == ["failed: sync; skipped: pytest"]
    assert "error: boom" in (tmp_path / "sync.log").read_text(encoding="utf-8")


def test_an_unknown_check_is_refused() -> None:
    with pytest.raises(SystemExit):
        check.main(["lint"])
