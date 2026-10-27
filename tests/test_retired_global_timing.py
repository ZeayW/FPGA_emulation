"""Retired engine selection must fail before opening circuit-sized inputs."""
import importlib.util
from unittest.mock import patch

import pytest

from emuflow.common.errors import EmuFlowError
from emuflow.flow.multi_fpga_flow import run_multi_fpga_flow
from emuflow.flow.native_compile import _request
from emuflow.timing.phase7c import run_phase7c


@pytest.mark.parametrize("entry", ["phase7c", "compile", "native-request"])
def test_python_engine_rejected_before_input_or_frontend(tmp_path, entry):
    with patch("emuflow.timing.phase7c.read_tdm_schedule", side_effect=AssertionError("schedule opened")), patch(
        "emuflow.flow.multi_fpga_flow._compile_frontend", side_effect=AssertionError("frontend entered")
    ), pytest.raises(EmuFlowError, match="only the OpenSTA"):
        if entry == "phase7c":
            run_phase7c(*(tmp_path / name for name in ("schedule", "platform", "phase3", "phase4", "phase5", "phase6")),
                        tmp_path / "out", global_timing_engine="python")
        elif entry == "compile":
            run_multi_fpga_flow(output_dir=tmp_path / "out", global_timing_engine="python")
        else:
            _request((), {"output_dir": tmp_path / "out", "global_timing_engine": "python"})
    assert not (tmp_path / "out").exists()


def test_production_has_no_python_timing_or_comparison_entry():
    from emuflow.timing import global_sta; from emuflow.transport import runtime
    assert importlib.util.find_spec("emuflow.system_timing") is None
    assert not hasattr(runtime, "build_system_timing")
    assert not hasattr(global_sta, "compare_system_timing")
    assert not hasattr(global_sta, "adopt_opensta_results")


def test_physical_qor_requires_engine_result_without_oracle_fallback():
    from emuflow.transport.runtime import aggregate_qor, build_virtual_runtime
    from tests.test_phase7c import Phase7CTest
    f = Phase7CTest(); f.setUp()
    runtime = build_virtual_runtime(f.schedule, f.platform)
    with patch("tests.system_timing_oracle.build_system_timing", side_effect=AssertionError("oracle entered")), pytest.raises(
        EmuFlowError, match="requires standalone OpenSTA"
    ):
        aggregate_qor(runtime, *(f.reports[p] for p in ("phase3", "phase4", "phase5", "phase6")),
                      f._physical_summary(), f.platform)
