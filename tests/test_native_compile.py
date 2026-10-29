"""Native control lifecycle tests; these do not qualify a hardware design."""
import inspect
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from emuflow.common.errors import EmuFlowError
from emuflow.flow.native_compile_adapter import STAGES, _control_snapshot, _initial_context, _pack, _unpack, execute
from emuflow.flow.native_compile import _request, run_native_multi_fpga_flow
from tests.native_build import emuflow_native, tlr_router


def test_control_state_preserves_paths_tuples_and_marker_like_user_values():
    value = {"path": Path("/path with spaces"), "tuple": (None, True, 1, 0.25),
             "user": ["path", "/a user list"], "nested": {"map": ["map", {}]}}
    assert _unpack(_pack(value)) == value
    reference_like_user_value = ["phase-report", {"schema": "user-data", "path": "relative"}]
    assert _unpack(_pack(reference_like_user_value)) == reference_like_user_value


def test_control_state_rejects_graph_views_without_traversal():
    class GraphView(dict):
        def __iter__(self):
            raise AssertionError("graph expanded")

    with pytest.raises(EmuFlowError, match="unsupported object"):
        _pack(GraphView())
    with pytest.raises(EmuFlowError, match="graph-sized collection"):
        _pack([0] * 16385)
    with pytest.raises(EmuFlowError, match="unsupported object"):
        _pack(float("nan"))


def test_controller_transfers_report_projection_without_graph_diagnostics():
    from emuflow.flow import multi_fpga_flow as flow
    class GraphView(dict):
        def __iter__(self):
            raise AssertionError("diagnostic graph expanded")
    ctx = flow._CompileContext({"phase3_report": {"status": "pass", "design": "fixture",
                              "validation": {"cut_nets": 2}, "diagnostics": GraphView()},
                              "report": GraphView()})
    del ctx.report
    assert _unpack(_pack(_control_snapshot(ctx, "partition", flow))) == {
        "phase3_report": {"status": "pass", "design": "fixture", "validation": {"cut_nets": 2}}}


def test_adapter_uses_actual_compile_defaults_and_rejects_relative_bindings(tmp_path):
    from emuflow.flow.multi_fpga_flow import run_multi_fpga_flow
    context = _initial_context({"options": {"output_dir": str(tmp_path / "out")}})
    defaults = inspect.signature(run_multi_fpga_flow).parameters
    for key in ("partition_provider", "seed", "cut_mode", "physical_seed"):
        assert getattr(context, key) == defaults[key].default
    with pytest.raises(EmuFlowError, match="absolute"):
        _initial_context({"options": {"output_dir": "relative"}})
    with pytest.raises(TypeError):
        _initial_context({"options": {"unknown_parameter": True}})


def test_adapter_rejects_mixed_state_before_calling_phase(tmp_path):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"options": {"output_dir": str(tmp_path / "out")}}))
    previous = tmp_path / "previous.json"
    previous.write_text(json.dumps({"schema": "emuflow.native-compile-state/v1", "run_id": "other"}))
    with pytest.raises(EmuFlowError, match="mixed or out-of-order"):
        execute("split", request, previous, tmp_path / "next", "this-run")


def test_shared_preflight_publishes_effective_configuration_and_restores_next_context(tmp_path):
    from emuflow.flow.multi_fpga_flow import run_multi_fpga_flow
    request = tmp_path / "request.json"
    options = {"output_dir": str(tmp_path / "out"), "mapping_profile": "xilinx-ultrascaleplus-open-v1",
               "partition_provider": "tritonpart", "clock_periods": {"clk": 10.0}, "opensta": sys.executable,
               "openroad": sys.executable}
    from tests.xilinx_timing_fixture import write_mock_xilinx_timing_contract
    options["architecture_timing_db"] = str(write_mock_xilinx_timing_contract(tmp_path / "mock-timing.json"))
    request.write_text(json.dumps({"options": options}))
    previous = tmp_path / "preflight.json"
    with patch("emuflow.flow.multi_fpga_flow.check_opensta_engine_version"):
        execute("preflight", request, None, previous, "run")
    value = json.loads(previous.read_text())
    assert set(value["effective_options"]) == set(inspect.signature(run_multi_fpga_flow).parameters)
    assert value["effective_options"]["clock_periods"] == {"clk": 10.0}
    with pytest.raises(EmuFlowError, match='owned by C'):
        execute("timing-policy", request, previous, tmp_path / "timing-policy.json", "run")


def test_bridge_preserves_invocation_directory_defaults_and_path_bindings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    options, encoded = _request((), {"output_dir": Path("out"), "sources": (Path("rtl.v") for _ in range(1)),
                                    "router": "relative/bin/router", "clocks": ["clk"]})
    assert json.loads(encoded) == options
    assert options["working_directory"] == str(tmp_path)
    assert options["options"]["sources"] == [str(tmp_path / "rtl.v")]
    assert options["options"]["router"] == "relative/bin/router"
    assert "slot_refinement_iterations" not in options["options"]
    assert options["options"]["physical_seed"] == 1
    _initial_context(options)


def test_cli_uses_the_sole_native_controller_by_default():
    from emuflow.flow.cli import _build_parser, _dispatch
    args = _build_parser().parse_args(["multi-fpga", "compile", "--out", "unused"])
    assert not hasattr(args, "compile_implementation")
    with patch("emuflow.flow.native_compile.run_native_multi_fpga_flow", return_value={"status": "pass"}) as native, patch("emuflow.flow.cli._print_json"):
        assert _dispatch(args) == 0
    assert native.call_args.kwargs["partition_provider"] == "patron"
    assert native.call_args.kwargs["phase6_provider"] == "baseline"
    assert native.call_args.kwargs["physical_seed"] == 1
    assert native.call_args.kwargs["global_timing_engine"] == "opensta"


@pytest.mark.parametrize("implementation", ["python-control-v1", "native-control-v1"])
def test_compile_has_no_second_control_selector(implementation):
    from emuflow.flow.cli import _build_parser
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["multi-fpga", "compile", "--out", "unused", "--compile-implementation", implementation])


def test_retired_python_order_and_native_stage_launchers_are_absent():
    import ast
    from emuflow.flow import multi_fpga_flow as flow
    source = ast.parse(inspect.getsource(flow.run_multi_fpga_flow))
    assert not any(isinstance(node, ast.Name) and node.id.startswith("_compile_") for node in ast.walk(source))
    for name in ("_compile_partition", "_compile_route", "_compile_schedule", "_compile_routing_schedule", "_compile_chimew_signoff"):
        assert not hasattr(flow, name)
    assert set(inspect.signature(flow._CompileContext).parameters) == {"values"}


@pytest.mark.parametrize("backend", ["rapidwright", "vivado"])
@pytest.mark.parametrize("phase6", ["baseline", "chimew"])
def test_public_api_submits_retained_configuration_once_without_fallback(tmp_path, backend, phase6):
    from emuflow.flow.multi_fpga_flow import run_multi_fpga_flow
    failure = EmuFlowError("terminal native failure")
    with patch("emuflow.flow.native_compile.run_native_multi_fpga_flow", side_effect=failure) as controller:
        with pytest.raises(EmuFlowError, match="terminal native failure"):
            run_multi_fpga_flow(output_dir=tmp_path / "flow", physical=True, physical_backend=backend,
                                phase6_provider=phase6, prepared_frontend=tmp_path / "front",
                                prepared_timing=tmp_path / "timing", clock_periods={"dla_core_clk": 10.0, "dla_csb_clk": 20.0})
    controller.assert_called_once()
    options = controller.call_args.kwargs
    assert options["physical_backend"] == backend
    assert options["phase6_provider"] == phase6
    assert options["prepared_frontend"] == tmp_path / "front"
    assert options["clock_periods"] == {"dla_core_clk": 10.0, "dla_csb_clk": 20.0}
    assert not (tmp_path / "flow").exists()


def test_missing_native_install_does_not_start_another_controller(tmp_path):
    from emuflow.flow.multi_fpga_flow import run_multi_fpga_flow
    with patch("emuflow.common.native_tools.require_native_schema"), patch("emuflow.flow.native_compile.resolve_native_executable", side_effect=EmuFlowError("missing native installation")) as resolver:
        with pytest.raises(EmuFlowError, match="missing native installation"):
            run_multi_fpga_flow(output_dir=tmp_path / "flow")
    resolver.assert_called_once_with("emuflow-native")
    assert not (tmp_path / "flow").exists()


def fixture_request(tmp_path, *, fail=None, corrupt=None, waiting=False, temporary_reference=None, prepared_timing=True):
    """A process test double, with no RTL/physical or qualification claims."""
    from tests.test_phase5_from_stores import native_inputs
    native_root = tmp_path / "native-inputs"
    native_root.mkdir()
    case, _, _, _ = native_inputs(native_root)
    from emuflow.ir.emuir_compact import write_compact_emuir
    from tests.native_partition_facts import (build_clusters)
    from emuflow.partition.partition_ir import load_partition_ir
    from emuflow.partition.phase3_native import prepare_configuration_request
    source_ir = native_root / "source.emuir.sqlite"
    write_compact_emuir(source_ir, case.ir.value)
    limits = dict(case._phase5_assignment['constraints'], balance_dimensions=[], balance_tolerance=1.0)
    limits['fixed'] = [{'instance': name, 'fpga': part} for name, part in case._phase5_assignment['instance_assignment'].items()]
    constraint_path = native_root/'constraints.json';constraint_path.write_text(json.dumps(limits))
    depth = case._phase5_assignment['semantic_contract']['max_cross_fpga_dependency_depth']
    clusters = build_clusters(load_partition_ir(source_ir), limits, cut_mode='static-exact-combinational', max_cross_fpga_dependency_depth=depth)
    parts = [fpga.id for fpga in case.platform.fpgas]
    solution = native_root/'solution.txt'
    solution.write_text(''.join(str(parts.index(case._phase5_assignment['instance_assignment'][cluster['instances'][0]]))+'\n' for cluster in clusters['clusters']))
    partition_request = prepare_configuration_request(native_root/'partition-request', native_root/'platform.normalized.json',
        provider='tritonpart', constraints_path=constraint_path, tritonpart_solution=solution,
        max_cross_fpga_dependency_depth=depth)
    from emuflow.transport.phase6_native import prepare_configuration_request as prepare_transport
    phase6_policy=prepare_transport(native_root/'phase6-policy',
        native_root/'platform.normalized.json',clock_periods={clock['id']:1.0 for clock in case.ir.value['clocks']})
    from emuflow.routing.phase4_native import prepare_configuration_request as prepare_routing
    (native_root/'phase4-policy').mkdir()
    phase4_policy=prepare_routing(native_root/'phase4-policy/request.json',
        native_root/'platform.normalized.json',frame_slots=16,max_iterations=3,timing_driven=False)
    from tests.frontend_input_fixture import write_timing, write_report, input_request
    from emuflow.common.native_tools import native_file_generation
    front = native_root / 'prepared'
    front.mkdir()
    (front / 'design.emuir.sqlite').symlink_to(source_ir)
    (front / 'platform.normalized.json').write_bytes((native_root / 'platform.normalized.json').read_bytes())
    write_report(front, case.ir.value['design']['name'])
    clocks = {clock['id']: 1.0 for clock in case.ir.value['clocks']}
    timing = native_root / 'timing'
    write_timing(timing, case.ir.value['design']['name'], clocks, source_ir)
    # Generate a fully bound input request, then place it only during preflight.
    input_configuration = input_request(front, timing, native_root / 'request-staging', native_root / 'platform.normalized.json', clocks)
    state_fields = {
        "phase4_root": ["path", str(native_root / "current")],
        "phase3_root": ["path", str(native_root)],
        "phase1_root": ["path", str(native_root)],
        "phase5_root": ["path", str(tmp_path / "out/tdm")],
        "effective_tdm_provider": "deterministic-dependency-earliest-slot-v1",
        "optimize_frame_slots": False,
        "phase6_provider":"baseline",
        "partition_provider":"tritonpart", "timing_driven":False, "route_constraints":None,
        "net_weights_path":["path",str(tmp_path/"out/timing/partition-net-weights.json")],
        "physical": False,
        "ir_path":["path",str(source_ir)],
        "platform_path":["path",str(native_root/'platform.normalized.json')],
        "physical_architecture":None,
        "prepared_timing": ["path", str(native_root)] if prepared_timing else None,
        "internal_timing_database": not prepared_timing,
        "timing_paths":None,
    }
    source = tmp_path / "source/emuflow"
    source.mkdir(parents=True)
    (source / "flow").mkdir()
    (source / "flow/__init__.py").write_text("")
    (source / "__init__.py").write_text("")
    (source / "flow/native_compile_adapter.py").write_text('''
import argparse,json,os,pathlib,signal,time,shutil
p=argparse.ArgumentParser()
for name in ('stage','request','previous','output','run-id'):p.add_argument('--'+name)
a=p.parse_args();request=pathlib.Path(a.request);v=json.loads(request.read_text());out=pathlib.Path(v['options']['output_dir']);out.mkdir(exist_ok=True)
temporary=pathlib.Path(os.environ['TMPDIR'])
assert os.environ['TMP']==os.environ['TEMP']==str(temporary)
assert pathlib.Path(os.environ['XDG_CACHE_HOME']).parent==temporary
(temporary/'unreleased-engine-cache').mkdir()
(temporary/'unreleased-engine-cache'/'payload').write_bytes(b'stage-owned')
with (out/'temporary-paths').open('a') as f:f.write(str(temporary)+'\\n')
with (out/'calls').open('a') as f:f.write(a.stage+'\\n')
if a.stage==FAIL:raise SystemExit(7)
if WAITING and a.stage=='preflight':
 (out/'pid').write_text(str(os.getpid()))
 time.sleep(60)
st=request.stat();generation=[str(x) for x in (st.st_dev,st.st_ino,st.st_size,*divmod(st.st_mtime_ns,1000000000),*divmod(st.st_ctime_ns,1000000000))]
if a.stage in ('timing-policy','partition-policy','route-policy','tdm','partition','route','signoff') or (a.stage=='split' and v['options'].get('phase6_provider','baseline')=='baseline'):raise AssertionError('Python native-owned phase adapter started')
state=STATE_FIELDS if not a.previous else json.loads(pathlib.Path(a.previous).read_text())['state'][1]
if a.stage=='preflight':
 root=out/'.native-frontend-input-request';root.mkdir()
 path=root/'inputs.json';path.write_text(pathlib.Path(INPUT_REQUEST).read_text())
 st=path.stat();g=[str(x) for x in (st.st_dev,st.st_ino,st.st_size,*divmod(st.st_mtime_ns,1000000000),*divmod(st.st_ctime_ns,1000000000))]
 state.update(native_frontend_input_request=['path',str(path)],native_frontend_input_generation=['tuple',g])
 root=out/'.native-phase3-configuration';root.mkdir()
 path=root/'request.json';path.write_text(pathlib.Path(PARTITION_REQUEST).read_text())
 st=path.stat();g=[str(x) for x in (st.st_dev,st.st_ino,st.st_size,*divmod(st.st_mtime_ns,1000000000),*divmod(st.st_ctime_ns,1000000000))]
 state.update(native_phase3_configuration=['path',str(path)],native_phase3_configuration_generation=['tuple',g])

if a.stage=='preflight':
 for name,source in (('phase4',PHASE4_POLICY),('phase6',PHASE6_POLICY)):
  root=out/('.native-'+name+'-configuration');root.mkdir()
  path=root/'request.json';path.write_text(pathlib.Path(source).read_text())
  st=path.stat();g=[str(x) for x in (st.st_dev,st.st_ino,st.st_size,*divmod(st.st_mtime_ns,1000000000),*divmod(st.st_ctime_ns,1000000000))]
  state['native_'+name+'_configuration']=['path',str(path)]
  state['native_'+name+'_configuration_generation']=['tuple',g]

# FIXTURE_CONTROL_STATE_READY
value={'schema':'emuflow.native-compile-state/v1','run_id':a.run_id,'stage':a.stage,'status':'pass','request_generation':generation,'state':['map',state],'effective_options':v['options']}
if TEMPORARY_REFERENCE and a.stage=='preflight':
 path=temporary/'unreleased-engine-cache'/'payload'
 if TEMPORARY_REFERENCE=='alias':
  alias=out/'unpublished-input';alias.symlink_to(path);path=alias
 value['state']=['map',{'next_input':['path',str(path)]}]
if a.stage=='preflight' and CORRUPT:value[CORRUPT]='invalid'
pathlib.Path(a.output).write_text(json.dumps(value))
'''.replace("FAIL", repr(fail)).replace("CORRUPT", repr(corrupt)).replace("WAITING", repr(waiting))
       .replace("TEMPORARY_REFERENCE", repr(temporary_reference)).replace("STATE_FIELDS", repr(state_fields)).replace("INPUT_REQUEST", repr(str(input_configuration))).replace("PARTITION_REQUEST", repr(str(partition_request))).replace("PHASE6_POLICY",repr(str(phase6_policy))).replace("PHASE4_POLICY",repr(str(phase4_policy))))
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"schema": "emuflow.native-compile/v1", "python_executable": sys.executable,
        "python_source": str(source.parent), "working_directory": str(tmp_path),
        "options": {"output_dir": str(tmp_path / "out"),
                    **({"prepared_timing": str(native_root)} if prepared_timing else {})}}))
    return request


def test_native_controller_executes_fixed_order_and_publishes_metrics(tmp_path):
    request = fixture_request(tmp_path)
    result = subprocess.run([str(emuflow_native()), "compile", str(request)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out/calls").read_text().splitlines() == [stage for stage in STAGES if stage not in {"frontend", "timing-policy", "partition-policy", "route-policy", "timing", "tdm", "partition", "route", "split", "physical", "signoff", "finalize"}]
    report = json.loads((tmp_path / "out/native-compile-result.json").read_text())
    assert report["operation"]["status"] == "pass"
    assert report["native_data_plane_complete"] is False
    route_step = next(step for step in report["stages"] if step["stage"] == "route")
    assert route_step["implementation_id"] == "native-phase4-v1"
    assert next(step for step in report["stages"] if step["stage"]=="route-policy")["implementation_id"]=="native-routing-preparation-v1"
    assert not (tmp_path/"out/.native-phase4-configuration").exists()
    assert not (tmp_path/"out/.native-phase6-configuration").exists()
    assert not (tmp_path / "out/.native-phase4-request").exists()
    route_report = json.loads((tmp_path / "out/system-route/phase4_report.json").read_text())
    assert route_report["validation"]["status"] == "pass"
    assert route_report["stage_implementation_id"] == "native-phase4-v1"
    native_step = next(step for step in report["stages"] if step["stage"] == "tdm")
    assert native_step["implementation_id"] == "native-exact-phase5-v1"
    split_step=next(step for step in report['stages'] if step['stage']=='split')
    assert split_step['implementation_id']=='native-baseline-phase6-v1'
    physical_step=next(step for step in report['stages'] if step['stage']=='physical')
    assert physical_step['implementation_id']=='native-no-physical-stage-v1'
    signoff_step=next(step for step in report['stages'] if step['stage']=='signoff')
    assert signoff_step['implementation_id']=='native-nonphysical-phase7c-v1'
    finalize_step=next(step for step in report['stages'] if step['stage']=='finalize')
    assert finalize_step['implementation_id']=='native-flow-finalize-v1'
    split=json.loads((tmp_path/'out/split/phase6_report.json').read_text())
    assert split['status']=='pass' and split['equivalence']['mismatches']==0
    assert split['equivalence']['cycles']>0
    summary = json.loads((tmp_path / "out/tdm/phase5_report.json").read_text())
    assert summary["provider"] == "deterministic-dependency-earliest-slot-v1"
    assert summary["validation"]["status"] == "pass"
    metrics = json.loads((tmp_path / "out/stage-metrics/phase5.json").read_text())
    assert metrics["rss_measurement"] == "wait4-single-native-process-high-water"
    assert report["effective_configuration"] == json.loads(request.read_text())["options"]
    assert report["request_configuration"] == json.loads(request.read_text())
    assert [step["stage"] for step in report["stages"]] == list(STAGES)
    assert all(step["maximum_process_rss_kib"] > 0 for step in report["stages"] if step["implementation_id"] != "native-timing-boundary-v1")
    assert not list(tmp_path.glob(".emuflow-compile-*"))


def test_python_tdm_adapter_entry_is_retired_before_execution(tmp_path):
    with pytest.raises(EmuFlowError, match=r"owned by the C\+\+ Phase 5 stage"):
        execute("tdm", tmp_path / "missing", None, tmp_path / "unused", "run")


def test_python_finalize_adapter_entry_is_retired_before_execution(tmp_path):
    with pytest.raises(EmuFlowError, match=r"owned by the C\+\+ controller"):
        execute("finalize", tmp_path / "missing", None, tmp_path / "unused", "run")


@pytest.mark.parametrize("physical", [False, True])
@pytest.mark.parametrize("provider", ["baseline", "chimew"])
def test_python_signoff_adapter_entry_is_retired(tmp_path, physical, provider):
    from emuflow.common.native_tools import native_file_generation
    request=tmp_path/'request.json';request.write_text('{}')
    prior=tmp_path/'prior.json'
    prior.write_text(json.dumps({'schema':'emuflow.native-compile-state/v1','run_id':'run',
        'stage':'physical','status':'pass','request_generation':[str(value) for value in native_file_generation(request)],
        'state':_pack({'phase6_provider':provider,'physical':physical})}))
    with pytest.raises(EmuFlowError,match=r'owned by the C\+\+ Phase 7C stage'):
        execute('signoff',request,prior,tmp_path/'unused','run')


@pytest.mark.parametrize("physical",[False,True])
def test_python_baseline_physical_adapter_entry_is_retired(tmp_path,physical):
    from emuflow.common.native_tools import native_file_generation
    request=tmp_path/'request.json';request.write_text('{}')
    prior=tmp_path/'prior.json'
    prior.write_text(json.dumps({'schema':'emuflow.native-compile-state/v1','run_id':'run',
        'stage':'split','status':'pass','request_generation':[str(value) for value in native_file_generation(request)],
        'state':_pack({'phase6_provider':'baseline','physical':physical})}))
    with pytest.raises(EmuFlowError,match=r'owned by the C\+\+ controller'):
        execute('physical',request,prior,tmp_path/'unused','run')


def test_baseline_split_never_executes_a_python_phase_adapter(tmp_path):
    from emuflow.common.native_tools import native_file_generation
    request=tmp_path/'request.json';request.write_text('{}')
    prior=tmp_path/'prior.json'
    prior.write_text(json.dumps({'schema':'emuflow.native-compile-state/v1','run_id':'run',
        'stage':'tdm','status':'pass','request_generation':[str(value) for value in native_file_generation(request)],
        'state':_pack({'phase6_provider':'baseline'})}))
    with patch('emuflow.flow.multi_fpga_flow._compile_split',side_effect=AssertionError('Python split started')):
        with pytest.raises(EmuFlowError,match=r'owned by the C\+\+ stage'):
            execute('split',request,prior,tmp_path/'unused','run')


def test_native_controller_retires_each_phase_temporary_directory_before_next_phase(tmp_path):
    request = fixture_request(tmp_path)
    source = tmp_path / "source/emuflow/flow/native_compile_adapter.py"
    text = source.read_text().replace(
        "temporary=pathlib.Path(os.environ['TMPDIR'])",
        """previous=out/'temporary-paths'
if previous.exists():
 assert all(not pathlib.Path(p).exists() for p in previous.read_text().splitlines())
assert all(p.name==a.stage for p in pathlib.Path(os.environ['TMPDIR']).parent.iterdir())
temporary=pathlib.Path(os.environ['TMPDIR'])""")
    source.write_text(text)
    result = subprocess.run([str(emuflow_native()), "compile", str(request)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    roots = (tmp_path / "out/temporary-paths").read_text().splitlines()
    assert len(set(roots)) == len(STAGES)-12
    assert all(not Path(root).exists() for root in roots)


@pytest.mark.parametrize("binding", ["path", "alias"])
def test_native_controller_rejects_phase_temporary_input_even_through_output_alias(tmp_path, binding):
    request = fixture_request(tmp_path, temporary_reference=binding)
    result = subprocess.run([str(emuflow_native()), "compile", str(request)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "phase temporary path" in result.stderr
    assert (tmp_path / "out/calls").read_text().splitlines() == ["preflight"]
    assert '"status":"fail"' in result.stderr
    assert not (tmp_path / "out/.native-compile-owner").exists()
    assert not (tmp_path / "out/multi-fpga-flow-report.json").exists()
    assert not list(tmp_path.glob(".emuflow-compile-*"))


def test_native_controller_result_matches_registered_schema(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    from referencing import Registry, Resource
    request = fixture_request(tmp_path)
    completed = subprocess.run([str(emuflow_native()), "compile", str(request)], capture_output=True)
    assert completed.returncode == 0
    schemas = Path(__file__).resolve().parents[1] / "schemas"
    registry = Registry()
    for name in ("native-stage-result-v1", "native-compile-request-v1"):
        path = schemas / f"{name}.schema.json"
        registry = registry.with_resource(path.as_uri(), Resource.from_contents(json.loads(path.read_text())))
    path = schemas / "native-compile-result-v2.schema.json"
    schema = {**json.loads(path.read_text()), "$id": path.as_uri()}
    jsonschema.Draft202012Validator(schema, registry=registry).validate(
        json.loads((tmp_path / "out/native-compile-result.json").read_text()))


def test_bridge_runs_native_controller_and_removes_only_request_scratch(tmp_path, monkeypatch):
    request = fixture_request(tmp_path)
    value = json.loads(request.read_text())
    monkeypatch.setattr("emuflow.flow.native_compile._request", lambda *_: (value, json.dumps(value)))
    monkeypatch.setattr("emuflow.flow.native_compile.resolve_native_executable", lambda _: str(emuflow_native()))
    assert run_native_multi_fpga_flow()["status"] == "pass"
    assert (tmp_path / "out/native-compile-result.json").is_file()
    assert not list(tmp_path.glob(".emuflow-compile-*"))


def test_controller_rejects_ambiguous_request_before_starting_any_phase(tmp_path):
    request = fixture_request(tmp_path)
    text = request.read_text().replace('"options": {', '"options": {"output_dir":"/wrong",')
    request.write_text(text)
    result = subprocess.run([str(emuflow_native()), "compile", str(request)], capture_output=True, text=True)
    assert result.returncode == 2
    assert "duplicate" in result.stderr
    assert not (tmp_path / "out").exists()


def test_native_controller_completes_existing_nonphysical_contract_fixture(tmp_path, monkeypatch):
    """Controller integration only; required large-design acceptance is NVDLA."""
    from emuflow.flow.multi_fpga_flow import validate_multi_fpga_flow_bundle
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr("emuflow.flow.native_compile.resolve_native_executable", lambda _: str(emuflow_native()))
    output = tmp_path / "flow"
    native_root = tmp_path / "native"
    (native_root / "bin").mkdir(parents=True)
    (native_root / "bin/emuflow-native").symlink_to(emuflow_native())
    monkeypatch.setenv("EMUFLOW_NATIVE_ROOT", str(native_root))
    # Real launcher environments may contain intentionally empty values. The
    # native timing stage forwards its environment through the tool protocol.
    monkeypatch.setenv("EMUFLOW_COMPILE_EMPTY_ENV", "")
    from tests.partition_fixture import write_fixture_solution
    from emuflow.frontend.yosys import import_yosys_json
    solution = tmp_path / "initial.part"
    write_fixture_solution(
        import_yosys_json(root / "examples/yosys/static_exact_chain.json", top="static_exact_chain", clocks=["clk"]),
        root / "platforms/virtual/static_exact_acceptance_2fpga.json", solution,
        cut_mode="static-exact-combinational", balance_tolerance=1.0,
        max_cross_fpga_dependency_depth=2,
    )
    from tests.xilinx_timing_fixture import write_mock_xilinx_timing_contract
    from emuflow.flow.multi_fpga_flow import run_multi_fpga_flow
    report = run_multi_fpga_flow(
        tritonpart_solution=solution,
        platform_path=root / "platforms/virtual/static_exact_acceptance_2fpga.json",
        output_dir=output, yosys_json=root / "examples/yosys/static_exact_chain.json",
        top="static_exact_chain", clocks=["clk"], partition_provider="tritonpart", balance_tolerance=1.0,
        timing_driven=False, clock_periods={"clk": 10.0},
        opensta=str(root / "tests/fixtures/fake_opensta_paths.py"), router=str(tlr_router()),
        frame_slots=32, equivalence_cycles=2, cut_mode="static-exact-combinational",
        max_cross_fpga_dependency_depth=2, mapping_profile="xilinx-ultrascaleplus-open-v1",
        architecture_timing_db=write_mock_xilinx_timing_contract(tmp_path / "mock-timing.json"),
    )
    from contextlib import closing
    from emuflow.timing.indexed_timing import IndexedTimingView
    from emuflow.partition.phase3_storage import read_phase3_cut_timing_view
    from emuflow.timing.sta import iter_sta_path_database_paths
    from emuflow.timing.opensta import DEFAULT_TIMING_MODEL
    from tests.cut_qualification_reference import _build_indexed_cut_segment_qualification
    path_database=output/'timing/path-database.json'
    database=json.loads(path_database.read_text())
    with closing(IndexedTimingView(output/'frontend/phase1/design.emuir.sqlite')) as ir:
        expected=_build_indexed_cut_segment_qualification(ir,
            read_phase3_cut_timing_view(output/'partition/assignment.json'),database,
            assignment_index_path=output/'partition/assignment.sqlite',timing_model_path=DEFAULT_TIMING_MODEL,
            architecture_timing_db_path=tmp_path/'mock-timing.json',
            path_records=iter_sta_path_database_paths(path_database,database,verify_payload=False))
    assert json.loads((output/'timing/cut-segment-qualification.json').read_text())==expected
    assert report["status"] == "pass"
    assert validate_multi_fpga_flow_bundle(output, minimum_combinational_cut_nets=1)["status"] == "pass"
    control = json.loads((output / "native-compile-result.json").read_text())
    assert [step["stage"] for step in control["stages"]] == list(STAGES)
    assert next(step for step in control["stages"] if step["stage"] == "timing")["implementation_id"] == "native-phase2-v1"
    assert next(step for step in control["stages"] if step["stage"] == "timing-policy")["implementation_id"] == "native-phase2-configuration-v1"
    assert not (output / ".native-phase2-configuration").exists()
    assert next(step for step in control["stages"] if step["stage"]=="route-policy")["implementation_id"]=="native-routing-preparation-v1"
    assert not (output/".native-phase4-configuration").exists()
    assert not (output/".native-phase6-configuration").exists()
    timing = json.loads((output / "timing/phase2_report.json").read_text())
    assert timing["stage_implementation_id"] == "native-phase2-v1"
    assert timing["status"] == "pass"
    assert not (output / ".native-phase2-request").exists()


@pytest.mark.parametrize("failure", ["failed-phase", "run_id", "stage", "request_generation", "state"])
def test_native_controller_does_not_advance_retry_or_publish_success_after_failure(tmp_path, failure):
    request = fixture_request(tmp_path, fail="preflight" if failure == "failed-phase" else None,
                              corrupt=None if failure == "failed-phase" else failure)
    result = subprocess.run([str(emuflow_native()), "compile", str(request)], capture_output=True, text=True)
    assert result.returncode != 0
    calls = (tmp_path / "out/calls").read_text().splitlines()
    assert calls == (["preflight"] if failure == "failed-phase" else ["preflight"])
    assert not (tmp_path / "out/multi-fpga-flow-report.json").exists()
    assert not list(tmp_path.glob(".emuflow-compile-*"))



def test_raw_frontend_cannot_fall_back_to_python_when_native_timing_policy_is_missing(tmp_path):
    request = fixture_request(tmp_path, prepared_timing=False)
    result = subprocess.run([str(emuflow_native()), "compile", str(request)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "policy" in result.stderr.lower()
    calls = (tmp_path / "out/calls").read_text().splitlines()
    assert calls == ["preflight"]
    assert "timing" not in calls
    assert not (tmp_path / "out/multi-fpga-flow-report.json").exists()
    assert '"status":"fail"' in result.stderr
    assert not (tmp_path / "out/.native-compile-owner").exists()
    assert not (tmp_path / "out/multi-fpga-flow-report.json").exists()
    assert not list(tmp_path.glob(".emuflow-compile-*"))

def test_native_controller_preserves_existing_output_directory(tmp_path):
    request = fixture_request(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "existing").write_text("keep")
    result = subprocess.run([str(emuflow_native()), "compile", str(request)], capture_output=True)
    assert result.returncode == 2
    assert list(out.iterdir()) == [out / "existing"]
    assert (out / "existing").read_text() == "keep"


def test_native_controller_cancellation_reaps_live_adapter(tmp_path):
    request = fixture_request(tmp_path, waiting=True)
    process = subprocess.Popen([str(emuflow_native()), "compile", str(request)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "out/pid").exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        adapter = int((tmp_path / "out/pid").read_text())
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=10)
        assert process.returncode == 1
        with pytest.raises(ProcessLookupError):
            os.kill(adapter, 0)
        assert b'"status":"fail"' in stderr
        assert not (tmp_path / "out/.native-compile-owner").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_bridge_sigterm_waits_for_native_cleanup_before_removing_request(tmp_path):
    request = fixture_request(tmp_path, waiting=True)
    command = f'''
import json
from emuflow.flow import native_compile as bridge
value=json.loads(open({str(request)!r}).read())
bridge._request=lambda *args:(value,json.dumps(value))
bridge.resolve_native_executable=lambda name:{str(emuflow_native())!r}
bridge.run_native_multi_fpga_flow()
'''
    process = subprocess.Popen([sys.executable, "-c", command], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while not (tmp_path / "out/pid").exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        adapter = int((tmp_path / "out/pid").read_text())
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=15)
        assert process.returncode != 0
        with pytest.raises(ProcessLookupError):
            os.kill(adapter, 0)
        assert b'native compiler cancelled by signal 15' in stderr
        assert not (tmp_path / "out/.native-compile-owner").exists()
        assert not list(tmp_path.glob(".emuflow-compile-*"))
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=15)


def test_actual_adapter_cancellation_unwinds_nested_native_engine_owner(tmp_path):
    request = fixture_request(tmp_path)
    source = tmp_path / "source/emuflow"
    package = Path(__file__).resolve().parents[1] / "src/python/emuflow"
    (source / "__init__.py").write_text(f"__path__.append({str(package)!r})\n")
    (source / "flow/__init__.py").write_text(f"__path__.append({str(package / 'flow')!r})\n")
    (source / "flow/native_compile_adapter.py").write_text(f'''
import importlib.util
s=importlib.util.spec_from_file_location('emuflow.actual_adapter',{str(package / 'flow/native_compile_adapter.py')!r})
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
raise SystemExit(m.main())
''')
    (source / "flow/multi_fpga_flow.py").write_text(f'''
from pathlib import Path
import os,sys
from emuflow.common.tool_runtime import NativeToolProcess
class _CompileContext:
 def __init__(self,values,**kw):self.__dict__.update(values)
def run_multi_fpga_flow(output_dir: Path=None, prepared_timing: Path=None):pass
def _compile_preflight(ctx):
 ctx.output_dir.mkdir()
 root=ctx.output_dir/'.native-frontend-input-request';root.mkdir()
 path=root/'inputs.json';path.write_text(Path({str(tmp_path / "native-inputs/request-staging/.native-frontend-input-request/inputs.json")!r}).read_text())
 from emuflow.common.native_tools import native_file_generation
 ctx.native_frontend_input_request=path;ctx.native_frontend_input_generation=tuple(str(v) for v in native_file_generation(path))
 ctx.internal_timing_database=True
 (ctx.output_dir/'adapter.pid').write_text(str(os.getpid()))
 engine="import os,signal,time,pathlib;signal.signal(signal.SIGTERM,signal.SIG_IGN);pathlib.Path('engine.pid').write_text(str(os.getpid()));time.sleep(60)"
 with NativeToolProcess({str(emuflow_native())!r},[sys.executable,'-c',engine],cwd=ctx.output_dir,scratch_parent=ctx.output_dir) as p:
  (ctx.output_dir/'supervisor.pid').write_text(str(p.process.pid))
  p.finish()
''')
    process = subprocess.Popen([str(emuflow_native()), "compile", str(request)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while not (tmp_path / "out/engine.pid").exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        pids = [int((tmp_path / f"out/{name}.pid").read_text()) for name in ("adapter", "supervisor", "engine")]
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=15)
        assert process.returncode == 1
        for pid in pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        assert not list((tmp_path / "out").glob(".emuflow-tool-*"))
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=15)


def test_native_partition_report_reference_rejects_changes_and_python_partition_adapter(tmp_path):
    request = fixture_request(tmp_path)
    result = subprocess.run([str(emuflow_native()), 'compile', str(request)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    from emuflow.common.native_tools import native_file_generation
    path=tmp_path/'out/partition/phase3_report.json'
    reference=['partition-report',{'schema':'phase3-report-v1','path':str(path),'generation':list(native_file_generation(path))}]
    assert _unpack(reference)['stage_implementation_id'] == 'native-phase3-v1'
    assert not (tmp_path/'out/.native-phase3-request').exists()
    assert not (tmp_path/'out/.native-phase3-configuration').exists()
    for field, value in [('schema', 'unknown'), ('generation', [True]*7), ('path', 'relative')]:
        bad = json.loads(json.dumps(reference));bad[1][field] = value
        with pytest.raises(EmuFlowError, match='partition report reference'):
            _unpack(bad)
    path = Path(reference[1]['path']);path.write_text(path.read_text()+'\n')
    with pytest.raises(EmuFlowError, match='partition report generation changed'):
        _unpack(reference)
    with pytest.raises(EmuFlowError, match=r'owned by the C\+\+ Phase 3 stage'):
        execute('partition', tmp_path/'missing', None, tmp_path/'unused', 'run')
    control = json.loads((tmp_path/'out/native-compile-result.json').read_text())
    assert next(row for row in control['stages'] if row['stage'] == 'partition')['implementation_id'] == 'native-phase3-v1'


@pytest.mark.parametrize('damage', ['configuration-generation', 'source-generation', 'bound-input', 'escaped-owner'])
def test_native_timing_configuration_rejects_mixed_or_unowned_inputs(tmp_path, damage):
    from emuflow.common.native_tools import native_file_generation
    from emuflow.timing.phase2_native import prepare_configuration_request
    repository = Path(__file__).resolve().parents[1]
    request = fixture_request(tmp_path, prepared_timing=False)
    source_ir = tmp_path / 'native-inputs/source.emuir.sqlite'
    # The fixture helper creates its native inputs under this stable root.
    assert source_ir.is_file()
    before = native_file_generation(source_ir)
    settings = prepare_configuration_request(tmp_path/'settings', clocks={'clk': 1},
        executable=str(repository/'tests/fixtures/fake_opensta_paths.py'))
    value = json.loads(settings.read_text())
    row = next(r for r in value['sources'] if r['role'] == 'timing-model')
    model = tmp_path/'owned-model.json';model.write_bytes(Path(row['path']).read_bytes())
    row.update(path=str(model), generation=list(native_file_generation(model)))
    if damage == 'bound-input':
        value['sources'].append({'role':'emuir','schema':'emuir-compact-v2',
            'path':str(source_ir),'generation':list(before)})
    settings.write_text(json.dumps(value))
    adapter = tmp_path/'source/emuflow/flow/native_compile_adapter.py'
    text = adapter.read_text();marker='# FIXTURE_CONTROL_STATE_READY';assert marker in text
    setup = f'''if a.stage=='preflight':
 root=out/'.native-phase2-configuration';root.mkdir()
 config=root/'request.json';config.write_text(pathlib.Path({str(settings)!r}).read_text())
 if {damage!r}=='escaped-owner':config=pathlib.Path({str(settings)!r})
 st=config.stat();g=[str(x) for x in (st.st_dev,st.st_ino,st.st_size,*divmod(st.st_mtime_ns,1000000000),*divmod(st.st_ctime_ns,1000000000))]
 state.update(native_phase2_configuration=['path',str(config)],native_phase2_configuration_generation=['tuple',g])
 if {damage!r}=='configuration-generation':config.write_text(config.read_text()+' ')
 if {damage!r}=='source-generation':
  model=pathlib.Path({str(model)!r});model.write_text(model.read_text()+' ')
'''
    adapter.write_text(text.replace(marker,setup+marker))
    completed = subprocess.run([str(emuflow_native()),'compile',str(request)],capture_output=True,text=True)
    assert completed.returncode != 0
    expected = {'configuration-generation':'configuration generation changed',
        'source-generation':'source generation changed','bound-input':'must bind the published frontend IR',
        'escaped-owner':'configuration escapes compiler ownership'}[damage]
    assert expected in completed.stderr, completed.stderr
    assert native_file_generation(source_ir) == before
    assert not (tmp_path/'out/timing/phase2_report.json').exists()
    assert (tmp_path/'out/calls').read_text().splitlines() == ['preflight']
    report = json.loads((tmp_path/'out/native-compile-result.json').read_text())
    assert report['operation']['status'] == 'fail'
    assert report['stages'][-1]['stage'] == 'timing-policy'
    assert not list(tmp_path.glob('.emuflow-compile-*'))


def test_native_timing_configuration_cancellation_reaps_its_engine_group(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    engine = tmp_path/'opensta.py'
    engine.write_text(f'''#!{sys.executable}
import json,os,signal,subprocess,sys,time
from pathlib import Path
root=Path({str(tmp_path)!r})
if sys.argv[1:]==['-version']:
 trace=root/'version-count';count=int(trace.read_text())+1 if trace.exists() else 1
 trace.write_text(str(count))
 if count>=2:
  signal.signal(signal.SIGTERM,signal.SIG_IGN)
  child=subprocess.Popen([sys.executable,'-c',"import os,signal,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path("+repr(str(root/'child.pid'))+").write_text(str(os.getpid()));time.sleep(60)"])
  (root/'engine.pid').write_text(str(os.getpid()))
  time.sleep(60)
 print('3.1.0');raise SystemExit(0)
if '-no_init' in sys.argv:
 print('EMUFLOW_OPENSTA_RUNTIME status=pass tcl=8.6.16');raise SystemExit(0)
raise AssertionError('timing extraction must not begin after cancellation')
''')
    engine.chmod(0o755)
    from tests.xilinx_timing_fixture import write_mock_xilinx_timing_contract
    solution = tmp_path/'initial.part';solution.write_text('0\n')
    _value, encoded = _request((), {'output_dir':tmp_path/'out',
        'yosys_json':repository/'examples/yosys/static_exact_chain.json',
        'top':'static_exact_chain','clocks':['clk'],'clock_periods':{'clk':10.0},
        'platform_path':repository/'platforms/virtual/static_exact_acceptance_2fpga.json',
        'partition_provider':'tritonpart','tritonpart_solution':solution,
        'mapping_profile':'xilinx-ultrascaleplus-open-v1',
        'architecture_timing_db':write_mock_xilinx_timing_contract(tmp_path/'architecture.json'),
        'opensta':str(engine)})
    request=tmp_path/'compile.json';request.write_text(encoded)
    process=subprocess.Popen([str(emuflow_native()),'compile',str(request)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+20
        while not all((tmp_path/name).exists() for name in ('engine.pid','child.pid')):
            assert process.poll() is None
            assert time.monotonic()<deadline
            time.sleep(.01)
        pids=[int((tmp_path/name).read_text()) for name in ('engine.pid','child.pid')]
        process.send_signal(signal.SIGTERM)
        _stdout,stderr=process.communicate(timeout=25)
        assert process.returncode==1,stderr.decode()
        for pid in pids:
            deadline=time.monotonic()+2
            while True:
                try:os.kill(pid,0)
                except ProcessLookupError:break
                assert time.monotonic()<deadline,('unreaped timing engine',pid)
                time.sleep(.01)
        output=tmp_path/'out'
        report=json.loads((output/'native-compile-result.json').read_text())
        assert report['operation']['status']=='fail'
        assert report['stages'][-1]['stage']=='timing-policy'
        assert report['stages'][-1]['implementation_id']=='native-phase2-configuration-v1'
        assert not (output/'.native-phase2-request/phase2-policy.json').exists()
        assert not (output/'timing').exists()
        assert not list(tmp_path.glob('.emuflow-compile-*'))
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM);process.communicate(timeout=25)


@pytest.mark.parametrize('damage', ['configuration-generation', 'source-generation', 'bound-input', 'escaped-owner', 'missing-policy'])
def test_native_partition_configuration_rejects_mixed_or_unowned_inputs(tmp_path, damage):
    request = fixture_request(tmp_path)
    adapter = tmp_path/'source/emuflow/flow/native_compile_adapter.py'
    injection = '''
if a.stage=='preflight':
 path=pathlib.Path(state['native_phase3_configuration'][1])
 settings=json.loads(path.read_text())
 if DAMAGE=='source-generation':
  next(row for row in settings['sources'] if row['role']=='constraints')['generation'][2]+=1
 elif DAMAGE=='bound-input':
  original=pathlib.Path(SOURCE_IR);st=original.stat()
  settings['sources'].append({'role':'source','path':str(original),'schema':'emuir-compact-v2','generation':[st.st_dev,st.st_ino,st.st_size,*divmod(st.st_mtime_ns,1000000000),*divmod(st.st_ctime_ns,1000000000)]})
 path.write_text(json.dumps(settings));st=path.stat()
 if DAMAGE=='configuration-generation':
  state['native_phase3_configuration_generation'][1][2]=str(st.st_size+1)
 else:
  state['native_phase3_configuration_generation']=['tuple',[str(x) for x in (st.st_dev,st.st_ino,st.st_size,*divmod(st.st_mtime_ns,1000000000),*divmod(st.st_ctime_ns,1000000000))]]
 if DAMAGE=='escaped-owner':state['native_phase3_configuration']=['path',str(pathlib.Path(ESCAPED_POLICY))]
 if DAMAGE=='missing-policy':state.pop('native_phase3_configuration')
'''.replace('DAMAGE', repr(damage)).replace('SOURCE_IR', repr(str(tmp_path/'native-inputs/source.emuir.sqlite'))).replace('ESCAPED_POLICY', repr(str(tmp_path/'native-inputs/partition-request/request.json')))
    adapter.write_text(adapter.read_text().replace('# FIXTURE_CONTROL_STATE_READY', injection))
    result = subprocess.run([str(emuflow_native()), 'compile', str(request)], capture_output=True, text=True)
    assert result.returncode != 0
    report = json.loads((tmp_path/'out/native-compile-result.json').read_text())
    assert report['stages'][-1]['stage'] == 'partition-policy'
    assert report['stages'][-1]['implementation_id'] == 'native-phase3-configuration-v1'
    assert (tmp_path/'out/calls').read_text().splitlines() == ['preflight']
    assert not (tmp_path/'out/partition').exists()
    assert not (tmp_path/'out/multi-fpga-flow-report.json').exists()


def test_partition_policy_python_adapter_and_stage_body_are_retired(tmp_path):
    from emuflow.flow import multi_fpga_flow
    assert not hasattr(multi_fpga_flow, '_compile_partition_policy')
    with pytest.raises(EmuFlowError, match='owned by C'):
        execute('partition-policy', tmp_path/'missing', None, tmp_path/'unused', 'run')


@pytest.mark.parametrize('damage', ['generation', 'source-generation', 'bound-assignment', 'escaped-owner', 'missing-policy', 'transport-generation'])
def test_native_routing_preparation_rejects_mixed_or_unowned_inputs(tmp_path, damage):
    request=fixture_request(tmp_path)
    adapter=tmp_path/'source/emuflow/flow/native_compile_adapter.py'
    injection='''
if a.stage=='preflight':
 path=pathlib.Path(state['native_phase4_configuration'][1]);settings=json.loads(path.read_text())
 if DAMAGE=='source-generation':settings['sources'][0]['generation'][2]+=1
 if DAMAGE=='bound-assignment':
  original=pathlib.Path(ASSIGNMENT);st=original.stat()
  settings['sources'].append({'role':'assignment','schema':'assignment-index-v4','path':str(original),'generation':[st.st_dev,st.st_ino,st.st_size,*divmod(st.st_mtime_ns,1000000000),*divmod(st.st_ctime_ns,1000000000)]})
 path.write_text(json.dumps(settings));st=path.stat()
 state['native_phase4_configuration_generation']=['tuple',[str(x) for x in (st.st_dev,st.st_ino,st.st_size,*divmod(st.st_mtime_ns,1000000000),*divmod(st.st_ctime_ns,1000000000))]]
 if DAMAGE=='generation':state['native_phase4_configuration_generation'][1][2]=str(st.st_size+1)
 if DAMAGE=='transport-generation':state['native_phase6_configuration_generation'][1][2]='0'
 if DAMAGE=='escaped-owner':
  path=pathlib.Path(ESCAPED);st=path.stat()
  state['native_phase4_configuration']=['path',str(path)]
  state['native_phase4_configuration_generation']=['tuple',[str(x) for x in (st.st_dev,st.st_ino,st.st_size,*divmod(st.st_mtime_ns,1000000000),*divmod(st.st_ctime_ns,1000000000))]]
 if DAMAGE=='missing-policy':state.pop('native_phase4_configuration')
'''.replace('DAMAGE',repr(damage)).replace('ASSIGNMENT',repr(str(tmp_path/'native-inputs/assignment.sqlite'))).replace('ESCAPED',repr(str(tmp_path/'native-inputs/phase4-policy/request.json')))
    adapter.write_text(adapter.read_text().replace('# FIXTURE_CONTROL_STATE_READY',injection))
    result=subprocess.run([str(emuflow_native()),'compile',str(request)],capture_output=True,text=True)
    assert result.returncode!=0
    report=json.loads((tmp_path/'out/native-compile-result.json').read_text())
    assert report['stages'][-1]['stage']=='route-policy'
    assert report['stages'][-1]['implementation_id']=='native-routing-preparation-v1'
    assert (tmp_path/'out/calls').read_text().splitlines()==['preflight']
    assert not (tmp_path/'out/system-route').exists()
    assert not (tmp_path/'out/multi-fpga-flow-report.json').exists()


def test_python_routing_preparation_body_and_adapter_are_retired(tmp_path):
    from emuflow.flow import multi_fpga_flow
    assert not hasattr(multi_fpga_flow,'_compile_route_policy')
    with pytest.raises(EmuFlowError,match='owned by C'):
        execute('route-policy',tmp_path/'missing',None,tmp_path/'unused','run')
