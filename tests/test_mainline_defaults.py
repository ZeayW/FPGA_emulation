"""Default selection is explicit and identical across supported host entries."""
import inspect
from unittest.mock import patch

import pytest

from emuflow.cli import _build_parser
from emuflow.errors import ValidationError
from emuflow.experiment_partition import run_partition_checkpoint
from emuflow.multi_fpga_flow import run_multi_fpga_flow
from emuflow.native_compile import _request
from emuflow.partition import (
    build_clusters, DEFAULT_PARTITION_CUT_MODE, DEFAULT_PATRON_ALGORITHM_VERSION,
)
from emuflow.partition_pressure import run_partition_pressure_native
from emuflow.phase3 import run_phase3


@pytest.mark.parametrize("command", [
    ["phase3", "--ir", "ir", "--platform", "board", "--out", "out"],
    ["multi-fpga", "compile", "design.v", "--out", "out"],
    ["experiment-stage", "partition-run", "--frontend", "front", "--timing", "timing", "--platform", "board", "--out", "out"],
    ["cross-stage", "optimize", "--ir", "ir", "--platform", "board", "--database", "timing", "--initial-assignment", "initial", "--out", "out"],
])
def test_cli_default_and_explicit_cut_do_not_choose_different_versions(command):
    parser = _build_parser()
    default = parser.parse_args(command)
    assert (default.patron_algorithm_version, default.cut_mode) == (14, "static-exact-combinational")
    old_cut = parser.parse_args([*command, "--cut-mode", "sequential-only"])
    assert old_cut.patron_algorithm_version == 14
    with pytest.raises(SystemExit) as error:
        parser.parse_args([*command, "--patron-flow-refinement"])
    assert error.value.code == 2


def test_api_and_native_request_share_the_same_defaults(tmp_path):
    for function in (run_phase3, run_multi_fpga_flow, run_partition_checkpoint):
        parameters = inspect.signature(function).parameters
        assert parameters["cut_mode"].default == DEFAULT_PARTITION_CUT_MODE
        assert parameters["patron_algorithm_version"].default == DEFAULT_PATRON_ALGORITHM_VERSION
        assert "patron_flow_refinement" not in parameters
    assert inspect.signature(build_clusters).parameters["cut_mode"].default == "static-exact-combinational"
    assert inspect.signature(run_partition_pressure_native).parameters["algorithm_version"].default == 14
    assert "flow_refinement" not in inspect.signature(run_partition_pressure_native).parameters
    value, _ = _request((), {"output_dir": tmp_path / "flow"})
    assert value["options"]["patron_algorithm_version"] == 14
    assert value["options"]["cut_mode"] == "static-exact-combinational"
    assert "patron_flow_refinement" not in value["options"]


def test_old_cut_without_an_explicit_version_does_not_downgrade(tmp_path):
    with patch("emuflow.phase3.load_partition_ir", side_effect=AssertionError("graph loaded")):
        with pytest.raises(ValidationError, match="v14 requires generalized Static Exact"):
            run_phase3(tmp_path / "ir", tmp_path / "board", tmp_path / "phase3", cut_mode="sequential-only")
    assert not (tmp_path / "phase3/assignment.json").exists()


def test_removed_python_switch_cannot_select_another_algorithm(tmp_path):
    with pytest.raises(TypeError, match="patron_flow_refinement"):
        run_phase3(tmp_path / "ir", tmp_path / "board", tmp_path / "phase3", patron_flow_refinement=True)
    with pytest.raises(TypeError, match="flow_refinement"):
        run_partition_pressure_native(None, None, None, None, None, None, None, flow_refinement=True)
    assert not (tmp_path / "phase3").exists()
