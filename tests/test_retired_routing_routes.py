"""Retired routing providers cannot load designs or run another production chain."""
from unittest.mock import patch

import pytest

from emuflow.flow.cli import _build_parser
from emuflow.common.errors import EmuFlowError
from emuflow.flow.multi_fpga_flow import run_multi_fpga_flow
from emuflow.routing.phase4 import run_phase4
from emuflow.scheduling.timing_routing import (GLOBAL_CANDIDATE_PROVIDER)


RETIRED = (
    "native-load-balanced-v1",
    "native-load-balanced-timing-evaluated-v1",
    "timing-aware-load-balanced-v1",
    "timing-aware-route-tdm-cooptimized-v1",
)


@pytest.mark.parametrize("provider", RETIRED)
def test_retired_route_provider_rejected_before_circuit_loading(tmp_path, provider):
    with pytest.raises(ValueError, match="unsupported Phase 4 routing provider"):
        run_phase4(tmp_path / "assignment", tmp_path / "board", tmp_path / "out", provider=provider)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("provider", RETIRED)
def test_retired_route_provider_rejected_before_compile_frontend(tmp_path, provider):
    with patch("emuflow.flow.multi_fpga_flow._compile_frontend", side_effect=AssertionError("frontend entered")):
        with pytest.raises(EmuFlowError, match="unsupported Phase 4 routing provider"):
            run_multi_fpga_flow(output_dir=tmp_path / "out", route_provider=provider)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("provider", RETIRED)
def test_retired_routing_choices_removed_at_every_cli_entry(provider):
    parser = _build_parser()
    for args in (
        ["phase4", "--assignment", "a", "--platform", "b", "--out", "out", "--provider", provider],
        ["multi-fpga", "compile", "--out", "out", "--route-provider", provider],
    ):
        # Establish a valid command before testing the retired choice.
        parser.parse_args([GLOBAL_CANDIDATE_PROVIDER if a == provider else a for a in args])
        with pytest.raises(SystemExit) as failure:
            parser.parse_args(args)
        assert failure.value.code == 2


@pytest.mark.parametrize("command", ["compare-routing-tdm", "compare-routing-tdm-scale"])
def test_obsolete_routing_comparison_commands_are_removed(command):
    with pytest.raises(SystemExit) as failure:
        _build_parser().parse_args(["multi-fpga", command])
    assert failure.value.code == 2


def test_global_engine_and_explicit_chimew_entry_remain():
    args = _build_parser().parse_args([
        "multi-fpga", "compile", "--out", "out", "--route-provider", GLOBAL_CANDIDATE_PROVIDER,
        "--phase6-provider", "chimew", "--no-timing-driven",
    ])
    assert args.route_provider == GLOBAL_CANDIDATE_PROVIDER
    assert args.phase6_provider == "chimew"
    assert args.timing_driven is False


@pytest.mark.parametrize("header", [f"EMUFLOW_TLR_INPUT_V{v}" for v in range(3, 10)])
def test_native_stage_rejects_retired_input_versions(tmp_path, header):
    import subprocess
    from tests.native_build import emuflow_native

    source = tmp_path / "old-router.in"
    source.write_text(
        header + "\nPARAM 2 0 20 8 2 4 1 0.1 8 32 1 1 1 0 1 0 0 1\n"
        "ARC 0 0 0 1 0 -1 -1 32 1 1 1 0\nDEMAND 0 0 1 1 0 1\n"
    )
    output = tmp_path / "result.sqlite"
    result = subprocess.run(
        [str(emuflow_native()), "phase4", "route", "--typed-output", str(source), str(output)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "unsupported global routing input header" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("option", [
    "--tdm-feedback", "--tdm-feedback-routes", "--tdm-feedback-schedule",
    "--physical-feedback", "--physical-feedback-runtime", "--physical-feedback-summary",
    "--physical-feedback-weight",
])
def test_retired_external_feedback_cli_is_rejected(option):
    arguments = ["phase4", "--assignment", "a", "--platform", "b", "--out", "out"]
    parser = _build_parser()
    parser.parse_args(arguments)
    with pytest.raises(SystemExit) as failure:
        parser.parse_args([*arguments, option, "1"])
    assert failure.value.code == 2


def test_retired_external_feedback_api_cannot_load_a_design(tmp_path):
    with pytest.raises(TypeError, match="tdm_feedback_path"):
        run_phase4(tmp_path / "assignment", tmp_path / "board", tmp_path / "out",
                   tdm_feedback_path=tmp_path / "feedback")
    assert not (tmp_path / "out").exists()
