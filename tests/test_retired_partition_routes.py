"""Retired providers fail before loading a circuit or invoking an engine."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.canonical_experiment import (
    CANONICAL_EXPERIMENT_CONFIG_SCHEMA, compile_canonical_experiment_spec,
)
from emuflow.cli import _build_parser
from emuflow.errors import EmuFlowError, ValidationError
from emuflow.multi_fpga_flow import run_multi_fpga_flow
from emuflow.phase3 import run_phase3


@pytest.mark.parametrize("provider", ["repart", "repart-replication", "mfspart"])
def test_retired_provider_rejected_before_phase_input(tmp_path, provider):
    with patch("emuflow.phase3.load_partition_ir", side_effect=AssertionError("circuit loaded")):
        with pytest.raises(ValidationError, match="unsupported Phase 3 provider"):
            run_phase3(tmp_path / "missing.ir", tmp_path / "missing.platform",
                       tmp_path / "partition", provider=provider)
    assert not (tmp_path / "partition/assignment.json").exists()


@pytest.mark.parametrize("provider", ["repart", "repart-replication", "mfspart"])
def test_retired_provider_rejected_before_compile_frontend(tmp_path, provider):
    with patch("emuflow.multi_fpga_flow._compile_frontend", side_effect=AssertionError("frontend entered")):
        with pytest.raises(EmuFlowError, match="unsupported Phase 3 provider"):
            run_multi_fpga_flow(output_dir=tmp_path / "flow", partition_provider=provider)
    assert not (tmp_path / "flow").exists()


@pytest.mark.parametrize("provider", ["repart", "repart-replication", "mfspart"])
def test_retired_provider_has_no_cli_route(tmp_path, provider):
    parser = _build_parser()
    for command in (
        ["phase3", "--ir", "ir", "--platform", "board", "--out", "out", "--provider", provider],
        ["multi-fpga", "compile", "design.v", "--out", "out", "--partition-provider", provider],
    ):
        with pytest.raises(SystemExit) as rejected:
            parser.parse_args(command)
        assert rejected.value.code == 2


@pytest.mark.parametrize("option", ["mfspart_post_refinement", "mfspart_refiner", "repart"])
def test_retired_config_keys_are_rejected_even_when_disabled(tmp_path, option):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"schema": CANONICAL_EXPERIMENT_CONFIG_SCHEMA, option: False}))
    with pytest.raises(ValidationError, match="retired partition configuration"):
        compile_canonical_experiment_spec(config, tmp_path, tmp_path / "spec.json")
    assert not (tmp_path / "spec.json").exists()


def test_chimew_and_fixed_binding_entry_remain_available():
    parser = _build_parser()
    args = parser.parse_args([
        "multi-fpga", "compile", "design.v", "--out", "out", "--phase6-provider", "chimew",
    ])
    assert args.phase6_provider == "chimew"
    from emuflow.phase6 import run_phase6
    from emuflow.chimew_phase6 import run_chimew_phase6_adapter
    assert callable(run_phase6) and callable(run_chimew_phase6_adapter)


@pytest.mark.parametrize("nested", [False, True])
def test_old_post_refinement_evidence_cannot_pass_reduced_validator(tmp_path, nested):
    from emuflow.experiment_partition import validate_partition_checkpoint
    from emuflow.experiment_upstream import EXPERIMENT_PARTITION_SCHEMA
    frontend, timing, root = (tmp_path / name for name in ("frontend", "timing", "partition"))
    (frontend / "phase1").mkdir(parents=True)
    timing.mkdir()
    root.mkdir()
    (frontend / "phase1/design.emuir.sqlite").write_text("not loaded")
    (timing / "partition-net-weights.json").write_text("not loaded")
    report = {"schema": EXPERIMENT_PARTITION_SCHEMA, "status": "pass", "provider": "tritonpart"}
    if nested:
        report["phase3"] = {"mfspart_post_refinement": {"status": "pass"}}
    else:
        report["mfspart_post_refinement"] = True
    (root / "experiment-partition-report.json").write_text(json.dumps(report))
    with pytest.raises(ValidationError, match="retired MFSPart post-refinement"):
        validate_partition_checkpoint(frontend, timing, tmp_path / "platform", root)
