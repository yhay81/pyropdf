"""The package imports, and its version comes from the core."""

from importlib.metadata import version

import pyropdf
from pyropdf import _core


def test_version_comes_from_the_core() -> None:
    assert pyropdf.__version__ == _core.__version__


def test_version_is_the_distribution_version() -> None:
    assert pyropdf.__version__ == version("pyropdf")
