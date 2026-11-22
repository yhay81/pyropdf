"""Direct signing-environment dictionaries for the signature's /Prop_Build entry."""

from __future__ import annotations

import math
from collections.abc import Mapping

from pyropdf import _core
from pyropdf._collections import _check_text_budget
from pyropdf._objects import Name

_ROLES = {"App", "Filter", "PubSec", "SigQ"}
_SWITCHES = {"PreRelease", "NonEFontNoWarn", "TrustedMode"}
_MAX_OS_NAMES = 32
_MAX_BUILD_KEYS = 10


def _entry(role: str, key: str, value: object) -> object:
    if key == "Name" and isinstance(value, str) and value:
        _check_text_budget((value,), "build name")
        return Name(value)
    if (key == "Date" or (role == "App" and key == "REx")) and isinstance(value, str):
        _check_text_budget((value,), "build data")
        return str(value)
    if key == "R" and not isinstance(value, bool):
        if isinstance(value, int) and -(2**63) <= value < 2**63:
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
    if (key in _SWITCHES or (role == "SigQ" and key == "Preview")) and isinstance(value, bool):
        return value
    if (
        key == "OS"
        and isinstance(value, (tuple, list))
        and len(value) <= _MAX_OS_NAMES
        and all(isinstance(v, str) for v in value)
    ):
        _check_text_budget(value, "build operating systems")
        return [str(v) for v in value]
    msg = f"invalid signature build property /{role}/{key}"
    raise ValueError(msg)


def build_entries(properties: Mapping[str, Mapping[str, object]] | None) -> dict[str, dict[str, object]]:
    """Describe the actual signing application by default and validate supplied build data."""
    if properties is None:
        return {"Filter": {"Name": Name("pyropdf")}, "App": {"Name": Name("pyropdf"), "REx": _core.__version__}}
    if not isinstance(properties, Mapping) or len(properties) > len(_ROLES):
        msg = "build_properties must map signing components to build dictionaries"
        raise TypeError(msg)
    result = {}
    for role, settings in properties.items():
        if role not in _ROLES or not isinstance(settings, Mapping) or len(settings) > _MAX_BUILD_KEYS:
            msg = f"invalid signature build component {role!r}"
            raise ValueError(msg)
        result[role] = {key: _entry(role, key, value) for key, value in settings.items()}
    return result
