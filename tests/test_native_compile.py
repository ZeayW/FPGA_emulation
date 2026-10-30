from tests.native_compile_state_fixture import CompileContext
from tests.preflight_reference import _initial_context
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
from tests.native_compile_state_fixture import STAGES, _control_snapshot, _pack, _unpack
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
    from emuflow_tools import flow_validation as flow
    class GraphView(dict):
        def __iter__(self):
            raise AssertionError("diagnostic graph expanded")
    ctx = CompileContext({"phase3_report": {"status": "pass", "design": "fixture",
                              "validation": {"cut_nets": 2}, "diagnostics": GraphView()},
                              "report": GraphView()})
    del ctx.report
    assert _unpack(_pack(_control_snapshot(ctx, "partition", flow))) == {
        "phase3_report": {"status": "pass", "design": "fixture", "validation": {"cut_nets": 2}}}


def test_adapter_uses_actual_compile_defaults_and_rejects_relative_bindings(tmp_path):
    from emuflow.flow.native_compile import run_native_multi_fpga_flow as run_multi_fpga_flow
    context = _initial_context({"options": {"output_dir": str(tmp_path / "out")}})
    defaults = inspect.signature(run_multi_fpga_flow).parameters
    for key in ("partition_provider", "seed", "cut_mode", "physical_seed"):
        assert getattr(context, key) == defaults[key].default
    with pytest.raises(EmuFlowError, match="absolute"):
        _initial_context({"options": {"output_dir": "relative"}})
    with pytest.raises(TypeError):
        _initial_context({"options": {"unknown_parameter": True}})






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
    from emuflow_tools import flow_validation as flow
    source = ast.parse(inspect.getsource(run_native_multi_fpga_flow))
    assert not any(isinstance(node, ast.Name) and node.id.startswith("_compile_") for node in ast.walk(source))
    for name in ("_compile_partition", "_compile_route", "_compile_schedule", "_compile_routing_schedule", "_compile_chimew_signoff"):
        assert not hasattr(flow, name)
    assert set(inspect.signature(CompileContext).parameters) == {"values"}


@pytest.mark.parametrize("backend", ["rapidwright", "vivado"])
@pytest.mark.parametrize("phase6", ["baseline", "chimew"])
def test_public_api_submits_retained_configuration_once_without_fallback(tmp_path, backend, phase6):
    import emuflow.flow.native_compile as client
    original = client.subprocess.Popen
    requests = []
    def spawn(argv, *args, **kwargs):
        if len(argv) > 2 and argv[1] == 'compile':
            requests.append(json.loads(Path(argv[2]).read_text()))
        return original(argv, *args, **kwargs)
    with patch.object(client.subprocess, 'Popen', side_effect=spawn):
        with pytest.raises(EmuFlowError, match='native compile failed'):
            client.run_native_multi_fpga_flow(output_dir=tmp_path/'flow', physical=True,
                physical_backend=backend, phase6_provider=phase6,
                prepared_frontend=tmp_path/'front', prepared_timing=tmp_path/'timing',
                clock_periods={'dla_core_clk':10.0, 'dla_csb_clk':20.0})
    assert len(requests) == 1
    options = requests[0]['options']
    assert options['physical_backend'] == backend
    assert options['phase6_provider'] == phase6
    assert options['prepared_frontend'] == str(tmp_path/'front')
    assert options['clock_periods'] == {'dla_core_clk':10.0, 'dla_csb_clk':20.0}
    assert not (tmp_path/'flow').exists()


def test_missing_native_install_does_not_start_another_controller(tmp_path):
    from emuflow.flow.native_compile import run_native_multi_fpga_flow as run_multi_fpga_flow
    with patch("emuflow.common.native_tools.require_native_schema"), patch("emuflow.flow.native_compile.resolve_native_executable", side_effect=EmuFlowError("missing native installation")) as resolver:
        with pytest.raises(EmuFlowError, match="missing native installation"):
            run_multi_fpga_flow(output_dir=tmp_path / "flow")
    resolver.assert_called_once_with("emuflow-native")
    assert not (tmp_path / "flow").exists()


def fixture_request(tmp_path, *, fail=None, waiting=False, prepared_timing=True):
    """A process test double, with no RTL/physical or qualification claims."""
    from tests.test_phase5_from_stores import native_inputs
    native_root = tmp_path / "native-inputs"
    native_root.mkdir()
    case, _, _, _ = native_inputs(native_root)
    from emuflow.ir.emuir_compact import write_compact_emuir
    from tests.native_partition_facts import (build_clusters)
    from emuflow_tools.partition.partition_ir import load_partition_ir
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
    from tests.frontend_input_fixture import write_timing, write_report
    from emuflow.common.native_tools import native_file_generation
    front = native_root / 'prepared'
    front.mkdir()
    (front / 'design.emuir.sqlite').symlink_to(source_ir)
    (front / 'platform.normalized.json').write_bytes((native_root / 'platform.normalized.json').read_bytes())
    write_report(front, case.ir.value['design']['name'])
    clocks = {clock['id']: 1.0 for clock in case.ir.value['clocks']}
    timing = native_root / 'timing'
    write_timing(timing, case.ir.value['design']['name'], clocks, source_ir)
    # Exercise the real internal TimingPathDB projection, rather than marking
    # an adopted timing boundary as an external/no-timing path in fake state.
    import sqlite3,hashlib
    from contextlib import closing
    with closing(sqlite3.connect(source_ir)) as db:
        net_names=[row[0] for row in db.execute('SELECT id FROM net_records ORDER BY seq')]
    with closing(sqlite3.connect(timing/'paths.sqlite')) as db, db:
        for path_seq, in db.execute('SELECT ordinal FROM paths').fetchall():
            db.execute('DELETE FROM path_nets WHERE path_ordinal=?',(path_seq,))
            db.executemany('INSERT INTO path_nets VALUES(?,?,?)',[(path_seq,i,n) for i,n in enumerate(net_names)])
    manifest=timing/'path-database.json';value=json.loads(manifest.read_text())
    value['payloads']['paths']['sha256']=hashlib.sha256((timing/'paths.sqlite').read_bytes()).hexdigest()
    value['summary']['unique_path_nets']=len(net_names)
    manifest.write_text(json.dumps(value))
    # Admission is the real C++ producer. Any Python phase launch is a failure.
    shadow=tmp_path/'source/emuflow/flow';shadow.mkdir(parents=True)
    (shadow/'native_compile_adapter.py').write_text("raise AssertionError('baseline compile launched a Python phase adapter')\n")
    engine=tmp_path/'admission-opensta'
    engine.write_text('#!'+sys.executable+'\n'+f"""
import os,sys,time
from pathlib import Path
if {waiting!r}:
 out=Path({str(tmp_path/'out')!r});out.mkdir(exist_ok=True)
 (out/'pid').write_text(str(os.getpid()))
 time.sleep(60)
if {fail is not None!r}:raise SystemExit(7)
if '-version' in sys.argv:print('3.1.0')
else:print('EMUFLOW_OPENSTA_RUNTIME status=pass tcl=8.6.16')
""")
    engine.chmod(0o700)
    from tests.xilinx_timing_fixture import write_mock_xilinx_timing_contract
    options=dict(output_dir=tmp_path/'out',platform_path=native_root/'platform.normalized.json',
        prepared_frontend=front,prepared_timing=timing if prepared_timing else None,
        clock_periods=clocks,opensta=str(engine),partition_provider='tritonpart',
        partition_constraints=constraint_path,tritonpart_solution=solution,
        max_cross_fpga_dependency_depth=depth,frame_slots=16,route_max_iterations=3,timing_driven=False,
        architecture_timing_db=write_mock_xilinx_timing_contract(native_root/'architecture-timing.json'))
    if not prepared_timing:
        # A real supplied frontend lets timing tests exercise the native
        # preparation/extraction lifecycle instead of forging controller state.
        cells={row['id']:{'type':row['type'],'parameters':row.get('parameters',{}),
                          'attributes':{},'connections':{},'port_directions':{}}
               for row in case.ir.value['instances']}
        netnames={}
        for index,net in enumerate(case.ir.value['nets'],start=1):
            netnames[net['id']]={'bits':[index]}
            for role,direction in (('drivers','output'),('sinks','input')):
                for endpoint in net.get(role,[]):
                    if endpoint.get('instance') is None:continue
                    cell=cells[endpoint['instance']];port=endpoint['port']
                    bits=cell['connections'].setdefault(port,[])
                    while len(bits)<=endpoint.get('bit',0):bits.append('0')
                    bits[endpoint.get('bit',0)]=index;cell['port_directions'][port]=direction
        clock_bit=len(netnames)+1;netnames['clk']={'bits':[clock_bit]}
        for cell in cells.values():
            if cell['type']=='FDRE':
                for port,bits in {'C':[clock_bit],'CE':['1'],'R':['0'],'D':['0']}.items():
                    cell['connections'].setdefault(port,bits);cell['port_directions'][port]='input'
            elif cell['type']=='LUT2':
                for port in ('I0','I1'):
                    cell['connections'].setdefault(port,['0']);cell['port_directions'][port]='input'
        raw=native_root/'source.json'
        raw.write_text(json.dumps({'creator':'controlled lifecycle fixture','modules':{
            case.ir.value['design']['name']:{'attributes':{'top':'1'},'cells':cells,
            'ports':{'clk':{'direction':'input','bits':[clock_bit]}},'netnames':netnames}}}))
        options.update(prepared_frontend=None,prepared_timing=None,yosys_json=raw,clocks=tuple(clocks))
    value,_=_request((),options)
    request=tmp_path/'request.json';request.write_text(json.dumps(value));return request


def test_native_controller_executes_fixed_order_and_publishes_metrics(tmp_path):
    request = fixture_request(tmp_path)
    result = subprocess.run([str(emuflow_native()), "compile", str(request)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "out/calls").exists()
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












def test_native_controller_retires_native_configuration_and_process_scratch(tmp_path):
    request=fixture_request(tmp_path)
    result=subprocess.run([str(emuflow_native()),'compile',str(request)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert not list(tmp_path.glob('.emuflow-compile-*'))
    assert not list((tmp_path/'out').glob('.compile-admission-*'))
    assert not list((tmp_path/'out').glob('.native-*-request'))
    assert not list((tmp_path/'out').glob('.native-*-configuration'))
    assert not (tmp_path/'out/calls').exists()


@pytest.mark.parametrize('binding',['path','alias'])
def test_native_worker_cannot_publish_a_private_temporary_input(tmp_path,binding):
    from tests.native_controller_fault_fixture import install
    request=fixture_request(tmp_path)
    body="""temporary=Path(os.environ['TMPDIR'])/'private-input'
temporary.write_text('phase cache')
ctx.next_input=temporary
"""
    if binding=='alias':body+="ctx.next_input=ctx.output_dir/'unpublished-alias'\nctx.next_input.symlink_to(temporary)\n"
    command=install(request,body)
    result=subprocess.run(command,capture_output=True,text=True)
    assert result.returncode!=0
    assert 'changed' in result.stderr
    assert not (tmp_path/'out/.native-compile-owner').exists()
    assert not (tmp_path/'out/multi-fpga-flow-report.json').exists()
    assert not list(tmp_path.glob('.emuflow-compile-*'))


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
    from emuflow_tools.flow_validation import validate_multi_fpga_flow_bundle
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
    from tests.yosys_reference import import_yosys_json
    solution = tmp_path / "initial.part"
    write_fixture_solution(
        import_yosys_json(root / "examples/yosys/static_exact_chain.json", top="static_exact_chain", clocks=["clk"]),
        root / "platforms/virtual/static_exact_acceptance_2fpga.json", solution,
        cut_mode="static-exact-combinational", balance_tolerance=1.0,
        max_cross_fpga_dependency_depth=2,
    )
    from tests.xilinx_timing_fixture import write_mock_xilinx_timing_contract
    from emuflow.flow.native_compile import run_native_multi_fpga_flow as run_multi_fpga_flow
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
    from emuflow_tools.timing.indexed_timing import IndexedTimingView
    from emuflow.partition.phase3_storage import read_phase3_cut_timing_view
    from emuflow_tools.timing.sta import iter_sta_path_database_paths
    from emuflow_tools.stage_clients.opensta import DEFAULT_TIMING_MODEL
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


@pytest.mark.parametrize('failure',['failed-phase','schema','status','run_id','stage','request_generation','state'])
def test_native_controller_rejects_failed_admission_and_external_control_state(tmp_path,failure):
    request=fixture_request(tmp_path,fail='preflight' if failure=='failed-phase' else None)
    if failure!='failed-phase':
        value=json.loads(request.read_text())
        value['schema' if failure=='schema' else failure]='invalid'
        request.write_text(json.dumps(value))
    result=subprocess.run([str(emuflow_native()),'compile',str(request)],capture_output=True,text=True)
    assert result.returncode!=0
    assert not (tmp_path/'out/calls').exists()
    assert not (tmp_path/'out/multi-fpga-flow-report.json').exists()
    assert not list(tmp_path.glob('.emuflow-compile-*'))



def test_internal_timing_engine_failure_never_falls_back_to_python(tmp_path):
    request=fixture_request(tmp_path,prepared_timing=False)
    result=subprocess.run([str(emuflow_native()),'compile',str(request)],capture_output=True,text=True)
    assert result.returncode!=0
    assert not (tmp_path/'out/calls').exists()
    report=json.loads((tmp_path/'out/native-compile-result.json').read_text())
    assert report['operation']['status']=='fail'
    assert report['stages'][-1]['stage']=='timing'
    assert report['stages'][-1]['implementation_id']=='native-phase2-v1'
    assert not list(tmp_path.glob('.emuflow-compile-*'))

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


def test_native_worker_cancellation_unwinds_nested_native_engine_owner(tmp_path):
    from tests.native_controller_fault_fixture import install
    request=fixture_request(tmp_path)
    body="""from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.common.native_tools import resolve_native_executable
(ctx.output_dir/'worker.pid').write_text(str(os.getpid()))
engine="import os,signal,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path('engine.pid').write_text(str(os.getpid()));time.sleep(60)"
with NativeToolProcess(resolve_native_executable('emuflow-native'),[sys.executable,'-c',engine],cwd=ctx.output_dir,scratch_parent=ctx.output_dir) as process:
 (ctx.output_dir/'supervisor.pid').write_text(str(process.process.pid))
 process.finish()
"""
    command=install(request,body)
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+20
        while not (tmp_path/'out/engine.pid').exists():
            assert process.poll() is None
            assert time.monotonic()<deadline
            time.sleep(.01)
        pids=[int((tmp_path/f'out/{name}.pid').read_text()) for name in ('worker','supervisor','engine')]
        process.send_signal(signal.SIGTERM);process.communicate(timeout=20)
        assert process.returncode==1
        for pid in pids:
            with pytest.raises(ProcessLookupError):os.kill(pid,0)
        assert not (tmp_path/'out/.native-compile-owner').exists()
        assert not list(tmp_path.glob('.emuflow-compile-*'))
    finally:
        if process.poll() is None:process.send_signal(signal.SIGTERM);process.communicate(timeout=20)


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
    control = json.loads((tmp_path/'out/native-compile-result.json').read_text())
    assert next(row for row in control['stages'] if row['stage'] == 'partition')['implementation_id'] == 'native-phase3-v1'


@pytest.mark.parametrize('damage',['configuration-generation','source-generation','bound-input','escaped-owner'])
def test_native_worker_cannot_rewrite_timing_configuration_bindings(tmp_path,damage):
    from tests.native_controller_fault_fixture import install
    from emuflow.common.native_tools import native_file_generation
    request=fixture_request(tmp_path,prepared_timing=False)
    original=tmp_path/'native-inputs/source.emuir.sqlite';before=native_file_generation(original)
    body=f"""import json
from emuflow.common.native_tools import native_file_generation
p=ctx.native_phase2_configuration
value=json.loads(p.read_text())
if {damage!r}=='source-generation':value['sources'][0]['generation'][2]+=1
if {damage!r}=='bound-input':value['sources'].append({{'role':'emuir','schema':'emuir-compact-v2','path':{str(original)!r},'generation':list(native_file_generation({str(original)!r}))}})
p.write_text(json.dumps(value))
if {damage!r}=='escaped-owner':
 other=Path({str(tmp_path/'escaped-phase2.json')!r});other.write_bytes(p.read_bytes());p=other;ctx.native_phase2_configuration=p
ctx.native_phase2_configuration_generation=tuple(map(str,native_file_generation(p)))
if {damage!r}=='configuration-generation':p.write_text(p.read_text()+' ')
"""
    command=install(request,body)
    result=subprocess.run(command,capture_output=True,text=True)
    assert result.returncode!=0
    expected={'configuration-generation':'configuration generation changed','source-generation':'source generation changed','bound-input':'must bind the published frontend IR','escaped-owner':'configuration escapes compiler ownership'}[damage]
    assert 'changed' in result.stderr,result.stderr
    assert native_file_generation(original)==before
    assert not (tmp_path/'out/timing/phase2_report.json').exists()
    report=json.loads((tmp_path/'out/native-compile-result.json').read_text())
    assert report['stages'][-1]['stage']=='frontend'
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


@pytest.mark.parametrize('damage',['configuration-generation','source-generation','bound-input','escaped-owner','missing-policy'])
def test_native_worker_cannot_rewrite_partition_configuration_bindings(tmp_path,damage):
    from tests.native_controller_fault_fixture import install
    request=fixture_request(tmp_path)
    original=tmp_path/'native-inputs/source.emuir.sqlite'
    body=f"""import json
from emuflow.common.native_tools import native_file_generation
p=ctx.native_phase3_configuration
value=json.loads(p.read_text())
if {damage!r}=='source-generation':value['sources'][0]['generation'][2]+=1
if {damage!r}=='bound-input':value['sources'].append({{'role':'source','schema':'emuir-compact-v2','path':{str(original)!r},'generation':list(native_file_generation({str(original)!r}))}})
p.write_text(json.dumps(value))
if {damage!r}=='escaped-owner':
 other=Path({str(tmp_path/'escaped-phase3.json')!r});other.write_bytes(p.read_bytes());p=other;ctx.native_phase3_configuration=p
ctx.native_phase3_configuration_generation=tuple(map(str,native_file_generation(p)))
if {damage!r}=='configuration-generation':p.write_text(p.read_text()+' ')
if {damage!r}=='missing-policy':del ctx.native_phase3_configuration
"""
    command=install(request,body)
    result=subprocess.run(command,capture_output=True,text=True)
    assert result.returncode!=0
    report=json.loads((tmp_path/'out/native-compile-result.json').read_text())
    assert report['stages'][-1]['stage']=='frontend',result.stderr
    assert not (tmp_path/'out/partition').exists()
    assert not (tmp_path/'out/multi-fpga-flow-report.json').exists()
    assert not list(tmp_path.glob('.emuflow-compile-*'))


def test_partition_policy_python_adapter_and_stage_body_are_retired(tmp_path):
    from emuflow_tools import flow_validation as multi_fpga_flow
    assert not hasattr(multi_fpga_flow, '_compile_partition_policy')


@pytest.mark.parametrize('damage',['generation','source-generation','bound-assignment','escaped-owner','missing-policy'])
def test_native_worker_cannot_rewrite_routing_configuration_bindings(tmp_path,damage):
    from tests.native_controller_fault_fixture import install
    request=fixture_request(tmp_path)
    assignment=tmp_path/'native-inputs/assignment.sqlite'
    body=f"""import json
from emuflow.common.native_tools import native_file_generation
p=ctx.native_phase4_configuration
value=json.loads(p.read_text())
if {damage!r}=='source-generation':value['sources'][0]['generation'][2]+=1
if {damage!r}=='bound-assignment':value['sources'].append({{'role':'assignment','schema':'assignment-index-v4','path':{str(assignment)!r},'generation':list(native_file_generation({str(assignment)!r}))}})
p.write_text(json.dumps(value))
if {damage!r}=='escaped-owner':
 other=Path({str(tmp_path/'escaped-phase4.json')!r});other.write_bytes(p.read_bytes());p=other;ctx.native_phase4_configuration=p
ctx.native_phase4_configuration_generation=tuple(map(str,native_file_generation(p)))
if {damage!r}=='generation':p.write_text(p.read_text()+' ')
if {damage!r}=='missing-policy':del ctx.native_phase4_configuration
"""
    command=install(request,body)
    result=subprocess.run(command,capture_output=True,text=True)
    assert result.returncode!=0
    report=json.loads((tmp_path/'out/native-compile-result.json').read_text())
    assert report['stages'][-1]['stage']=='frontend',result.stderr
    assert report['stages'][-1]['implementation_id']=='native-frontend-input-v1'
    assert not (tmp_path/'out/system-route').exists()
    assert not (tmp_path/'out/multi-fpga-flow-report.json').exists()
    assert not list(tmp_path.glob('.emuflow-compile-*'))


def test_python_routing_preparation_body_and_adapter_are_retired(tmp_path):
    from emuflow_tools import flow_validation as multi_fpga_flow
    assert not hasattr(multi_fpga_flow,'_compile_route_policy')
