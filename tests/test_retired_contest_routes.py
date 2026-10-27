"""Removed contest producers must not survive as independently runnable routes."""
import importlib.util
from pathlib import Path

import pytest

from emuflow.flow.cli import _build_parser


@pytest.mark.parametrize("command", [
    "eda2025-import", "eda2025-evaluate", "eda2025-optimize-topology",
    "eda2025-optimize-routing", "eda2025-materialize-boarddb",
])
def test_retired_contest_command_is_rejected(command):
    with pytest.raises(SystemExit) as error:
        _build_parser().parse_args(["contest", command])
    assert error.value.code == 2


def test_retired_topology_pipeline_has_no_module_or_native_target():
    root = Path(__file__).resolve().parents[1]
    assert importlib.util.find_spec("emuflow.contest_eda2025") is None
    assert not (root / "src/core/eda2025_topology_optimizer.cpp").exists()
    # The former option may remain only as an explicit rejection. The build
    # target and implementation must be absent.
    assert "emuflow_eda2025_topology_optimizer" not in (root / "CMakeLists.txt").read_text()
    assert "emuflow_eda2025_topology_optimizer" not in (root / "tests/native_build.py").read_text()
