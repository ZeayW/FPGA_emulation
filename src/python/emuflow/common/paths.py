"""Locations shared by the source checkout and installed Python package."""

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = PACKAGE_ROOT.parent
DATA_ROOT = PACKAGE_ROOT / "data"
REPOSITORY_ROOT = (
    PACKAGE_ROOT.parents[2]
    if PYTHON_ROOT.name == "python" and PYTHON_ROOT.parent.name == "src"
    else PYTHON_ROOT.parent
)
