"""Native-control Chimew contract tests; no physical/NVDLA qualification."""

import copy
import inspect
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.common.errors import EmuFlowError, ValidationError
from emuflow.common.io import write_json
from emuflow.flow.native_compile_adapter import (
    _control_snapshot,
    _pack,
    _restore_control_state,
    _unpack,
    execute,
)
from emuflow.common.native_tools import native_file_generation
from emuflow.transport.chimew.xilinx_chimew_geometry import XilinxPackageGeometry
from tests.test_xilinx_chimew_lookahead import case, fixed_case


def _roundtrip(value):
    return _unpack(json.loads(json.dumps(_pack(value))))


def test_device_control_roundtrip_does_not_rescan_the_architecture(case):
    platform, _source, architecture, *_ = case
    original = XilinxPackageGeometry.open(architecture, platform.fpgas[0].part)
    state = _roundtrip(original.control_snapshot())
    with patch(
        "emuflow.transport.chimew.xilinx_chimew_geometry._native_package_geometry",
        side_effect=AssertionError("ArchitectureDB was rescanned"),
    ):
        restored = XilinxPackageGeometry.from_control_snapshot(state)
    assert restored == original
    assert len(json.dumps(_pack(state))) < 65536
    with pytest.raises(TypeError):
        restored.pins["forged"] = ("site", 0, 0)


@pytest.mark.parametrize(
    "mutation", ["generation", "digest", "coordinates", "source", "schema", "count"]
)
def test_device_control_rejects_changed_source_and_invalid_device_facts(case, mutation):
    platform, _source, architecture, *_ = case
    state = _roundtrip(
        XilinxPackageGeometry.open(
            architecture, platform.fpgas[0].part
        ).control_snapshot()
    )
    if mutation == "generation":
        state["generation"] = (*state["generation"][:-1], -1)
    elif mutation == "digest":
        state["sha256"] = "not-a-sha"
    elif mutation == "coordinates":
        key = next(iter(state["pins"]))
        state["pins"][key] = ("site", True, 0)
    elif mutation == "source":
        architecture.write_text(architecture.read_text() + "\n")
    elif mutation == "schema":
        state["schema"] += "-unknown"
    else:
        state["pins"] = {str(i): ("site", 0, 0) for i in range(16385)}
    with pytest.raises(ValidationError):
        XilinxPackageGeometry.from_control_snapshot(state)


def test_native_preflight_reaches_the_same_chimew_checks_for_both_backends(case,tmp_path,request):
    from tests.chimew_admission_fixture import admitted
    from emuflow.flow import multi_fpga_flow as flow
    backend=request.node.callspec.params['case']
    _source,state=admitted(case,tmp_path/'admission',backend)
    value=json.loads(state.read_text())
    restored=_restore_control_state(_unpack(value['state']))
    assert restored['chimew_package_geometry'].part==case[0].fpgas[0].part
    assert restored['physical_backend']==backend
    assert restored['phase6_provider']=='chimew'
    assert restored['native_phase3_configuration'].is_file()
    assert not (tmp_path/'admission/native/frontend/phase1').exists()


def test_baseline_reports_have_one_control_owner_and_restore_aliases(tmp_path):
    import emuflow.flow.multi_fpga_flow as flow

    physical, runtime = {"schema": "physical", "status": "pass"}, {"status": "pass"}
    comparison = {"baseline": {"physical": physical, "runtime": runtime}}
    ctx = flow._CompileContext(
        {
            "baseline_physical_report": physical,
            "baseline_runtime_report": runtime,
            "phase6_comparison": comparison,
        }
    )
    state = _control_snapshot(ctx, "split", flow)
    assert (
        "baseline_physical_report" not in state
        and "baseline_runtime_report" not in state
    )
    restored = _restore_control_state(_roundtrip(state))
    assert (
        restored["baseline_physical_report"]
        is restored["phase6_comparison"]["baseline"]["physical"]
    )
    assert (
        restored["baseline_runtime_report"]
        is restored["phase6_comparison"]["baseline"]["runtime"]
    )
    with pytest.raises(EmuFlowError, match="duplicate"):
        _restore_control_state(
            {**_roundtrip(state), "baseline_physical_report": physical}
        )
    ctx.baseline_runtime_report = {"status": "different"}
    with pytest.raises(EmuFlowError, match="owners disagree"):
        _control_snapshot(ctx, "split", flow)


def test_real_chimew_kernels_survive_split_and_physical_control_boundaries(
    case, tmp_path
):
    """Real lookahead/typed Chimew pipeline; physical/STA engines are test doubles."""
    import emuflow.flow.multi_fpga_flow as flow
    from emuflow.platform.default_platform import research_platform_path

    platform, source, architecture, placed, schedule, routes = case
    backend = (
        "vivado"
        if "vivado_implementation" in placed["fpgas"][0]["stages"]
        else "rapidwright"
    )
    root = tmp_path / "flow"
    root.mkdir()
    defaults = {
        k: p.default
        for k, p in inspect.signature(flow.run_multi_fpga_flow).parameters.items()
    }
    ctx = flow._CompileContext(defaults)
    ctx.output_dir = root
    ctx.platform_path = research_platform_path(2)
    ctx.phase6_provider = "chimew"
    ctx.physical = True
    ctx.physical_backend = backend
    ctx.physical_architecture = architecture
    ctx.chimew_package_geometry = XilinxPackageGeometry.open(
        architecture, platform.fpgas[0].part
    )
    ctx.ir_path = source
    ctx.assignment_path = root / "assignment.json"
    ctx.assignment_path.write_text("{}")
    ctx.phase4_root = routes.parent
    ctx.phase5_root = schedule.parent
    ctx.phase3_root = root / "partition"
    ctx.projected_timing_paths = None
    ctx.timing_driven = False
    ctx.copied_link_timing_path = None
    ctx.global_sta_launcher_reference = None
    ctx.path_database_path = root / "paths.json"
    ctx.path_database_path.write_text("{}")
    split_calls, physical_calls, timing_calls = [], [], []

    def split_engine(*args, **kwargs):
        out = args[4]
        out.mkdir(parents=True, exist_ok=True)
        report = {
            "schema": "emuflow.phase6-report/v1",
            "status": "pass",
            "provider": "contract-double",
        }
        write_json(out / "phase6_report.json", report)
        split_calls.append((args, kwargs))
        return report

    def physical_engine(*args, **kwargs):
        result = copy.deepcopy(placed)
        result.update(
            schema="emuflow.multi-fpga-physical-flow/v1",
            status="pass",
            backend={"id": backend},
            architecture={"source": str(architecture)},
            execution={"seed": 1},
            physical_summary_ref="physical-summary.json",
            summary={"status": "pass"},
        )
        for record in result["fpgas"]:
            record.update(
                critical_path_ns=1,
                physical_result={
                    "timing": {
                        "wns_ns": 0.1,
                        "tns_ns": 0,
                        "failing_endpoints": 0,
                        "failing_endpoint_constraints": 0,
                    },
                    "closure": {"unrouted_nets": 0, "drc_violations": 0},
                },
            )
        physical_calls.append((args, kwargs))
        return result

    def timing_engine(*args, **kwargs):
        timing_calls.append((args, kwargs))
        timing = {
            "worst_slack_bound_ns": 0.1,
            "total_negative_slack_bound_ns": 0,
            "negative_slack_paths": 0,
        }
        return {
            "status": "pass",
            "system_timing_summary": {
                "status": "pass",
                "target_clock": dict(timing),
                "runtime_clock": dict(timing),
            },
        }

    request = tmp_path / "request.json"
    request.write_text('{"options":{}}')
    generation = [str(v) for v in native_file_generation(request)]
    previous = tmp_path / "tdm-state.json"
    write_json(
        previous,
        {
            "schema": "emuflow.native-compile-state/v1",
            "run_id": "chimew",
            "stage": "tdm",
            "status": "pass",
            "request_generation": generation,
            "state": _pack(_control_snapshot(ctx, "tdm", flow)),
        },
    )
    with (
        patch.object(flow, "run_phase6", side_effect=split_engine),
        patch.object(flow, "run_multi_fpga_physical_flow", side_effect=physical_engine),
        patch.object(flow, "run_native_physical_signoff", side_effect=timing_engine),
        patch.object(
            flow, "validate_multi_fpga_physical_report"
        ) as physical_validation,
    ):
        for stage in ("split", "physical"):
            output = tmp_path / (stage + "-state.json")
            execute(stage, request, previous, output, "chimew")
            assert output.stat().st_size < 262144
            state = _unpack(json.loads(output.read_text())["state"])
            assert (
                "baseline_physical_report" not in state
                and "baseline_runtime_report" not in state
            )
            previous = output
        physical_validation.assert_not_called()
    comparison = state["phase6_comparison"]
    assert comparison["status"] == "pending-physical-comparison"
    assert len(split_calls) == len(physical_calls) == 2
    assert len(timing_calls) == 1  # Only the baseline prepass has run.
    input_path=state["native_phase6_comparison_input"]
    assert tuple(state["native_phase6_comparison_generation"]) == tuple(str(v) for v in native_file_generation(input_path))
    draft=json.loads(input_path.read_text())
    assert draft["baseline"] == comparison["baseline"]
    assert draft["pin_plan_metrics"]["signals"] > 0
    # This fixture's engine doubles are not terminal physical evidence. The
    # native signoff/comparison integration is tested against actual typed
    # evidence in test_native_physical_signoff/test_native_phase6_comparison.
    with pytest.raises(EmuFlowError,match=r"owned by the C\+\+ Phase 7C stage"):
        execute("signoff",request,previous,tmp_path/"retired-state.json","chimew")
    assert physical_calls[0][1] == physical_calls[1][1]
    assert physical_calls[1][1]["backend"] == backend
    assert {
        "pin_plan_path",
        "position_hints_path",
        "electrical_binding_path",
    } <= split_calls[1][1].keys()
    for key in ("pin_plan_path", "position_hints_path", "electrical_binding_path"):
        assert split_calls[1][1][key].is_file()
    assert comparison["baseline"]["runtime"]["status"] == "pass"
    assert not (root / "phase6-comparison/comparison-report.json").exists()



@pytest.mark.parametrize('boundary',['split','physical'])
def test_native_admission_device_owner_is_checked_at_each_remaining_adapter_boundary(case,tmp_path,request,boundary):
    from tests.chimew_admission_fixture import admitted
    backend=request.node.callspec.params['case']
    source,state=admitted(case,tmp_path/'admission',backend)
    value=json.loads(state.read_text())
    value['stage']='tdm' if boundary=='split' else 'split'
    state.write_text(json.dumps(value))
    architecture=case[2];architecture.write_text(architecture.read_text()+'\n')
    with pytest.raises(ValidationError,match='geometry generation changed'):
        execute(boundary,source,state,tmp_path/'must-not-publish','test')
    assert not (tmp_path/'must-not-publish').exists()
