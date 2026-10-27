"""No public text claims safety for untrusted input before I2 is released (DESIGN.md section 6)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Words that make the claim: safety for untrusted, hostile, or malicious
#: input, or for uploads.
CLAIM = re.compile(r"untrusted|hostile|malicious|uploads?\b", re.IGNORECASE)

#: The one sentence that may name untrusted input, because it withholds the
#: claim. Relax it and this test together once I2 (#15) is released.
WITHHOLDING = "No pyropdf release claims safety for untrusted input"


def _public_texts() -> dict[str, str]:
    paths = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    package = ROOT / "python" / "pyropdf"
    paths += sorted(package.rglob("*.py")) + sorted(package.rglob("*.pyi"))
    texts = {str(path.relative_to(ROOT)): path.read_text(encoding="utf-8") for path in paths}
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    texts["pyproject.toml"] = " ".join([project["description"], *project.get("keywords", [])])
    return texts


def _claims(text: str) -> list[str]:
    lines = text.replace(WITHHOLDING, "").splitlines()
    return [line.strip() for line in lines if CLAIM.search(line)]


def test_no_public_text_claims_safety_for_untrusted_input() -> None:
    found = {name: claims for name, text in _public_texts().items() if (claims := _claims(text))}
    assert found == {}


def test_the_readme_withholds_the_claim() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert WITHHOLDING in readme
    assert "issues/15" in readme


def test_a_claim_is_caught() -> None:
    assert _claims("Budgets for opening untrusted PDFs.")
    assert _claims("A profile for user uploads.")
    assert not _claims(f"{WITHHOLDING} before I2 is released.")
