"""Native-control Chimew contract tests; no physical/NVDLA qualification."""

import copy
import inspect
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.errors import EmuFlowError, ValidationError
from emuflow.io import write_json
from emuflow.native_compile_adapter import (
    _control_snapshot,
    _pack,
    _restore_control_state,
    _unpack,
    execute,
)
from emuflow.native_tools import native_file_generation
from emuflow.xilinx_chimew_geometry import XilinxPackageGeometry
from tests.test_xilinx_chimew_lookahead import case, fixed_case


def _roundtrip(value):
    return _unpack(json.loads(json.dumps(_pack(value))))


def test_device_control_roundtrip_does_not_rescan_the_architecture(case):
    platform, _source, architecture, *_ = case
    original = XilinxPackageGeometry.open(architecture, platform.fpgas[0].part)
    state = _roundtrip(original.control_snapshot())
    with patch(
        "emuflow.xilinx_chimew_geometry.read_xilinx_package_geometry",
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


def test_native_preflight_reaches_the_same_chimew_checks_for_both_backends(
    case, tmp_path
):
    import emuflow.multi_fpga_flow as flow
    from emuflow.native_compile_adapter import _initial_context

    platform, _source, architecture, *_ = case
    from emuflow.default_platform import research_platform_path

    class Reached(Exception):
        pass

    for backend in ("rapidwright", "vivado"):
        ctx = _initial_context(
            {
                "options": {
                    "output_dir": str(tmp_path / backend),
                    "platform_path": str(research_platform_path(2)),
                    "phase6_provider": "chimew",
                    "physical": True,
                    "physical_backend": backend,
                    "physical_architecture": str(architecture),
                }
            }
        )
        with patch.object(
            flow, "_validate_mapping_timing_contract", side_effect=Reached
        ):
            with pytest.raises(Reached):
                flow._compile_preflight(ctx)
        snapshot = _control_snapshot(ctx, "preflight", flow)
        restored = _restore_control_state(_roundtrip(snapshot))
        assert restored["chimew_package_geometry"].part == platform.fpgas[0].part
        assert not ctx.output_dir.exists()


def test_baseline_reports_have_one_control_owner_and_restore_aliases(tmp_path):
    import emuflow.multi_fpga_flow as flow

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


def test_real_chimew_kernels_survive_split_physical_signoff_control_boundaries(
    case, tmp_path
):
    """Real lookahead/typed Chimew pipeline; physical/STA engines are test doubles."""
    import emuflow.multi_fpga_flow as flow
    from emuflow.default_platform import research_platform_path

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
    ctx = flow._CompileContext(defaults, native_mode=True)
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
        patch.object(flow, "run_phase7c", side_effect=timing_engine),
        patch.object(
            flow, "validate_multi_fpga_physical_report"
        ) as physical_validation,
    ):
        for stage in ("split", "physical", "signoff"):
            output = tmp_path / (stage + "-state.json")
            execute(stage, request, previous, output, "chimew")
            assert output.stat().st_size < 262144
            state = _unpack(json.loads(output.read_text())["state"])
            assert (
                "baseline_physical_report" not in state
                and "baseline_runtime_report" not in state
            )
            previous = output
        assert physical_validation.call_count == 2
    comparison = state["phase6_comparison"]
    assert comparison["validation"]["status"] == "pass"
    assert len(split_calls) == len(physical_calls) == len(timing_calls) == 2
    assert physical_calls[0][1] == physical_calls[1][1]
    assert physical_calls[1][1]["backend"] == backend
    assert {
        "pin_plan_path",
        "position_hints_path",
        "electrical_binding_path",
    } <= split_calls[1][1].keys()
    for key in ("pin_plan_path", "position_hints_path", "electrical_binding_path"):
        assert split_calls[1][1][key].is_file()
    assert (
        comparison["baseline"]["runtime"]["status"]
        == comparison["chimew"]["runtime"]["status"]
        == "pass"
    )
    assert comparison["baseline_system_timing"] == comparison["chimew_system_timing"]
    assert (root / "phase6-comparison/comparison-report.json").is_file()


@pytest.mark.parametrize("tamper", [None, "split", "finalize"])
def test_native_cpp_controller_carries_real_device_state_across_processes(
    case, tmp_path, tamper
):
    """The C++ owner, Phase 3/4/5 and real codec run; other phase bodies are doubles."""
    import subprocess
    from tests.native_build import emuflow_native
    from tests.test_native_compile import fixture_request
    from emuflow.native_compile_adapter import STAGES

    platform, _source, architecture, *_ = case
    root = tmp_path / "process"
    root.mkdir()
    request = fixture_request(root)
    source = root / "source/emuflow"
    real_source = Path(__file__).parents[1] / "src/emuflow"
    (source / "__init__.py").write_text(
        "__path__.append(" + repr(str(real_source)) + ")\n"
    )
    adapter = source / "native_compile_adapter.py"
    text = adapter.read_text()
    marker = "pathlib.Path(a.output).write_text(json.dumps(value))"
    assert marker in text
    codec = (
        """
import importlib.util
spec=importlib.util.spec_from_file_location('emuflow.real_compile_codec',REAL_CODEC)
codec=importlib.util.module_from_spec(spec);spec.loader.exec_module(codec)
from emuflow.multi_fpga_flow import _CompileContext
from emuflow import multi_fpga_flow as flow
from emuflow.xilinx_chimew_geometry import XilinxPackageGeometry
from types import SimpleNamespace
if a.stage=='preflight':
 state=codec._unpack(value['state'])
 state.update(output_dir=out,phase6_provider='chimew',chimew_package_geometry=XilinxPackageGeometry.open(pathlib.Path(ARCHITECTURE),PART))
else:
 state=codec._restore_control_state(codec._unpack(value['state']))
assert state['phase6_provider']=='chimew'
assert state['chimew_package_geometry'].pins
if a.stage=='split':
 physical={'status':'pass','device-facts-only':True};runtime={'status':'pass'}
 state.update(baseline_physical_report=physical,baseline_runtime_report=runtime,
              phase6_comparison={'baseline':{'physical':physical,'runtime':runtime}})
if a.stage in ('physical','signoff','finalize'):
 assert state['baseline_physical_report'] is state['phase6_comparison']['baseline']['physical']
 assert state['baseline_runtime_report'] is state['phase6_comparison']['baseline']['runtime']
if a.stage==TAMPER:
 target=pathlib.Path(ARCHITECTURE);target.write_text(target.read_text()+'\\n')
value['state']=codec._pack(codec._control_snapshot(_CompileContext(state,native_mode=True),a.stage,flow))
""".replace(
            "REAL_CODEC", repr(str(real_source / "native_compile_adapter.py"))
        )
        .replace("ARCHITECTURE", repr(str(architecture)))
        .replace("PART", repr(platform.fpgas[0].part))
        .replace("TAMPER", repr(tamper))
    )
    adapter.write_text(text.replace(marker, codec + "\n" + marker))
    options = json.loads(request.read_text())
    options["options"]["phase6_provider"] = "chimew"
    write_json(request, options)
    result = subprocess.run(
        [str(emuflow_native()), "compile", str(request)], capture_output=True, text=True
    )
    calls = (root / "out/calls").read_text().splitlines()
    route_report = json.loads((root / "out/system-route/phase4_report.json").read_text())
    assert route_report["stage_implementation_id"] == "native-phase4-v1"
    assert route_report["validation"]["status"] == "pass"
    route_store = root / "out/system-route" / route_report["artifacts"]["route_store"]
    assert route_store.read_bytes().startswith(b"SQLite format 3")
    if tamper:
        assert result.returncode != 0
        assert calls == [stage for stage in STAGES[: STAGES.index(tamper) + 1] if stage not in {"tdm", "partition", "route"}]
        assert "geometry generation changed" in result.stderr
        assert not (root / "out/multi-fpga-flow-report.json").exists()
    else:
        assert result.returncode == 0, result.stderr
        assert calls == [stage for stage in STAGES if stage not in {"tdm", "partition", "route"}]
        report = json.loads((root / "out/native-compile-result.json").read_text())
        assert report["effective_configuration"]["phase6_provider"] == "chimew"
        assert report["native_data_plane_complete"] is False
        route = next(step for step in report["stages"] if step["stage"] == "route")
        assert route["implementation_id"] == "native-phase4-v1"
        partition = next(step for step in report["stages"] if step["stage"] == "partition")
        assert partition["implementation_id"] == "native-phase3-v1"
        assert (root / "out/native-partition-reference.json").is_file()
