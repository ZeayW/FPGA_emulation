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

from emuflow.errors import EmuFlowError
from emuflow.native_compile_adapter import STAGES, _control_snapshot, _initial_context, _pack, _unpack, execute
from emuflow.native_compile import _request, run_native_multi_fpga_flow
from tests.native_build import emuflow_native, tlr_router


def test_control_state_preserves_paths_tuples_and_marker_like_user_values():
    value = {"path": Path("/path with spaces"), "tuple": (None, True, 1, 0.25),
             "user": ["path", "/a user list"], "nested": {"map": ["map", {}]}}
    assert _unpack(_pack(value)) == value


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
    from emuflow import multi_fpga_flow as flow
    class GraphView(dict):
        def __iter__(self):
            raise AssertionError("diagnostic graph expanded")
    ctx = flow._CompileContext({"phase3_report": {"status": "pass", "design": "fixture",
                              "validation": {"cut_nets": 2}, "diagnostics": GraphView()},
                              "report": GraphView()})
    del ctx.report
    assert _unpack(_pack(_control_snapshot(ctx, "partition", flow))) == {
        "phase3_report": {"status": "pass", "design": "fixture", "validation": {"cut_nets": 2}}}
    ctx.report = GraphView()
    assert _control_snapshot(ctx, "finalize", flow) == {}


def test_adapter_uses_actual_compile_defaults_and_rejects_relative_bindings(tmp_path):
    from emuflow.multi_fpga_flow import run_multi_fpga_flow
    context = _initial_context({"options": {"output_dir": str(tmp_path / "out")}})
    defaults = inspect.signature(run_multi_fpga_flow).parameters
    for key in ("partition_provider", "seed", "cut_mode", "physical_seed", "slot_refinement_iterations"):
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
    with patch("emuflow.multi_fpga_flow._compile_frontend", side_effect=AssertionError("phase started")):
        with pytest.raises(EmuFlowError, match="mixed or out-of-order"):
            execute("frontend", request, previous, tmp_path / "next", "this-run")


def test_shared_preflight_publishes_effective_configuration_and_restores_next_context(tmp_path):
    from emuflow.multi_fpga_flow import run_multi_fpga_flow
    request = tmp_path / "request.json"
    options = {"output_dir": str(tmp_path / "out"), "mapping_profile": "vtr-hard-blocks",
               "partition_provider": "greedy", "clock_periods": {"clk": 10.0}, "opensta": sys.executable}
    request.write_text(json.dumps({"options": options}))
    previous = tmp_path / "preflight.json"
    with patch("emuflow.multi_fpga_flow.check_opensta_engine_version"):
        execute("preflight", request, None, previous, "run")
    value = json.loads(previous.read_text())
    assert set(value["effective_options"]) == set(inspect.signature(run_multi_fpga_flow).parameters)
    assert value["effective_options"]["clock_periods"] == {"clk": 10.0}
    observed = []
    def frontend(ctx):
        observed.append((ctx.output_dir, ctx.clocks, ctx.seed, ctx.opensta))
    with patch("emuflow.multi_fpga_flow._compile_frontend", side_effect=frontend):
        execute("frontend", request, previous, tmp_path / "frontend.json", "run")
    assert observed == [(tmp_path / "out", (), 0, str(Path(sys.executable).resolve()))]


def test_bridge_preserves_invocation_directory_defaults_and_path_bindings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    options, encoded = _request((), {"output_dir": Path("out"), "sources": (Path("rtl.v") for _ in range(1)),
                                    "router": "relative/bin/router", "clocks": ["clk"]})
    assert json.loads(encoded) == options
    assert options["working_directory"] == str(tmp_path)
    assert options["options"]["sources"] == [str(tmp_path / "rtl.v")]
    assert options["options"]["router"] == "relative/bin/router"
    assert options["options"]["slot_refinement_iterations"] == 200
    assert options["options"]["physical_seed"] == 1
    _initial_context(options)


def test_cli_selects_native_control_without_calling_python_compiler():
    from emuflow.cli import _build_parser, _dispatch
    args = _build_parser().parse_args(["multi-fpga", "compile", "--out", "unused",
                                      "--compile-implementation", "native-control-v1"])
    with (patch("emuflow.native_compile.run_native_multi_fpga_flow", return_value={"status": "pass"}) as native,
          patch("emuflow.cli.run_multi_fpga_flow", side_effect=AssertionError("Python compiler called")),
          patch("emuflow.cli._print_json")):
        assert _dispatch(args) == 0
    assert native.call_args.kwargs["slot_refinement_iterations"] == 200
    assert native.call_args.kwargs["partition_provider"] == "patron"


def fixture_request(tmp_path, *, fail=None, corrupt=None, waiting=False, temporary_reference=None):
    """A process test double, with no RTL/physical or qualification claims."""
    source = tmp_path / "source/emuflow"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("")
    (source / "native_compile_adapter.py").write_text('''
import argparse,json,os,pathlib,signal,time
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
if WAITING and a.stage=='frontend':
 (out/'pid').write_text(str(os.getpid()))
 time.sleep(60)
st=request.stat();generation=[str(x) for x in (st.st_dev,st.st_ino,st.st_size,*divmod(st.st_mtime_ns,1000000000),*divmod(st.st_ctime_ns,1000000000))]
value={'schema':'emuflow.native-compile-state/v1','run_id':a.run_id,'stage':a.stage,'status':'pass','request_generation':generation,'state':['map',{}],'effective_options':v['options']}
if TEMPORARY_REFERENCE and a.stage=='frontend':
 path=temporary/'unreleased-engine-cache'/'payload'
 if TEMPORARY_REFERENCE=='alias':
  alias=out/'unpublished-input';alias.symlink_to(path);path=alias
 value['state']=['map',{'next_input':['path',str(path)]}]
if a.stage=='preflight' and CORRUPT:value[CORRUPT]='invalid'
pathlib.Path(a.output).write_text(json.dumps(value))
if a.stage=='finalize':(out/'multi-fpga-flow-report.json').write_text(json.dumps({'schema':'emuflow.multi-fpga-flow/v2','status':'pass'}))
'''.replace("FAIL", repr(fail)).replace("CORRUPT", repr(corrupt)).replace("WAITING", repr(waiting))
       .replace("TEMPORARY_REFERENCE", repr(temporary_reference)))
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"schema": "emuflow.native-compile/v1", "python_executable": sys.executable,
        "python_source": str(source.parent), "working_directory": str(tmp_path),
        "options": {"output_dir": str(tmp_path / "out")}}))
    return request


def test_native_controller_executes_fixed_order_and_publishes_metrics(tmp_path):
    request = fixture_request(tmp_path)
    result = subprocess.run([str(emuflow_native()), "compile", str(request)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out/calls").read_text().splitlines() == list(STAGES)
    report = json.loads((tmp_path / "out/native-compile-result.json").read_text())
    assert report["operation"]["status"] == "pass"
    assert report["native_data_plane_complete"] is False
    assert report["effective_configuration"] == {"output_dir": str(tmp_path / "out")}
    assert report["request_configuration"] == json.loads(request.read_text())
    assert [step["stage"] for step in report["stages"]] == list(STAGES)
    assert all(step["maximum_process_rss_kib"] > 0 for step in report["stages"])
    assert not list(tmp_path.glob(".emuflow-compile-*"))


def test_native_controller_retires_each_phase_temporary_directory_before_next_phase(tmp_path):
    request = fixture_request(tmp_path)
    source = tmp_path / "source/emuflow/native_compile_adapter.py"
    text = source.read_text().replace(
        "temporary=pathlib.Path(os.environ['TMPDIR'])",
        """previous=out/'temporary-paths'
if previous.exists():
 assert all(not pathlib.Path(p).exists() for p in previous.read_text().splitlines())
temporary=pathlib.Path(os.environ['TMPDIR'])""")
    source.write_text(text)
    result = subprocess.run([str(emuflow_native()), "compile", str(request)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    roots = (tmp_path / "out/temporary-paths").read_text().splitlines()
    assert len(set(roots)) == len(STAGES)
    assert all(not Path(root).exists() for root in roots)


@pytest.mark.parametrize("binding", ["path", "alias"])
def test_native_controller_rejects_phase_temporary_input_even_through_output_alias(tmp_path, binding):
    request = fixture_request(tmp_path, temporary_reference=binding)
    result = subprocess.run([str(emuflow_native()), "compile", str(request)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "phase temporary path" in result.stderr
    assert (tmp_path / "out/calls").read_text().splitlines() == ["preflight", "frontend"]
    assert json.loads((tmp_path / "out/native-compile-result.json").read_text())["operation"]["status"] == "fail"
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
    path = schemas / "native-compile-result-v1.schema.json"
    schema = {**json.loads(path.read_text()), "$id": path.as_uri()}
    jsonschema.Draft202012Validator(schema, registry=registry).validate(
        json.loads((tmp_path / "out/native-compile-result.json").read_text()))


def test_bridge_runs_native_controller_and_removes_only_request_scratch(tmp_path, monkeypatch):
    request = fixture_request(tmp_path)
    value = json.loads(request.read_text())
    monkeypatch.setattr("emuflow.native_compile._request", lambda *_: (value, json.dumps(value)))
    monkeypatch.setattr("emuflow.native_compile.resolve_native_executable", lambda _: str(emuflow_native()))
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


def test_actual_shared_phase_adapters_complete_existing_nonphysical_contract_fixture(tmp_path, monkeypatch):
    """Adapter integration only; the required large-design acceptance is NVDLA."""
    from emuflow.multi_fpga_flow import validate_multi_fpga_flow_bundle
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr("emuflow.native_compile.resolve_native_executable", lambda _: str(emuflow_native()))
    output = tmp_path / "flow"
    native_root = tmp_path / "native"
    (native_root / "bin").mkdir(parents=True)
    (native_root / "bin/emuflow-native").symlink_to(emuflow_native())
    monkeypatch.setenv("EMUFLOW_NATIVE_ROOT", str(native_root))
    # Real launcher environments may contain intentionally empty values. The
    # timing adapter forwards its environment through the binary tool protocol.
    monkeypatch.setenv("EMUFLOW_COMPILE_EMPTY_ENV", "")
    report = run_native_multi_fpga_flow(
        platform_path=root / "platforms/virtual/static_exact_acceptance_2fpga.json",
        output_dir=output, yosys_json=root / "examples/yosys/static_exact_chain.json",
        top="static_exact_chain", clocks=["clk"], partition_provider="greedy", balance_tolerance=1.0,
        timing_driven=False, clock_periods={"clk": 10.0},
        opensta=str(root / "tests/fixtures/fake_opensta_paths.py"), router=str(tlr_router()),
        frame_slots=32, equivalence_cycles=2, cut_mode="static-exact-combinational",
        max_cross_fpga_dependency_depth=2, mapping_profile="vtr-hard-blocks",
    )
    assert report["status"] == "pass"
    assert validate_multi_fpga_flow_bundle(output, minimum_combinational_cut_nets=1)["status"] == "pass"
    control = json.loads((output / "native-compile-result.json").read_text())
    assert [step["stage"] for step in control["stages"]] == list(STAGES)


@pytest.mark.parametrize("failure", ["failed-phase", "run_id", "stage", "request_generation", "state"])
def test_native_controller_does_not_advance_retry_or_publish_success_after_failure(tmp_path, failure):
    request = fixture_request(tmp_path, fail="timing" if failure == "failed-phase" else None,
                              corrupt=None if failure == "failed-phase" else failure)
    result = subprocess.run([str(emuflow_native()), "compile", str(request)], capture_output=True, text=True)
    assert result.returncode != 0
    calls = (tmp_path / "out/calls").read_text().splitlines()
    assert calls == (["preflight", "frontend", "timing"] if failure == "failed-phase" else ["preflight"])
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
        process.communicate(timeout=10)
        assert process.returncode == 1
        with pytest.raises(ProcessLookupError):
            os.kill(adapter, 0)
        report = json.loads((tmp_path / "out/native-compile-result.json").read_text())
        assert report["operation"]["status"] == "fail"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_bridge_sigterm_waits_for_native_cleanup_before_removing_request(tmp_path):
    request = fixture_request(tmp_path, waiting=True)
    command = f'''
import json
from emuflow import native_compile as bridge
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
        process.communicate(timeout=15)
        assert process.returncode != 0
        with pytest.raises(ProcessLookupError):
            os.kill(adapter, 0)
        result = json.loads((tmp_path / "out/native-compile-result.json").read_text())
        assert result["operation"]["status"] == "fail"
        assert not list(tmp_path.glob(".emuflow-compile-*"))
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=15)


def test_actual_adapter_cancellation_unwinds_nested_native_engine_owner(tmp_path):
    request = fixture_request(tmp_path)
    source = tmp_path / "source/emuflow"
    package = Path(__file__).resolve().parents[1] / "src/emuflow"
    (source / "__init__.py").write_text(f"__path__.append({str(package)!r})\n")
    (source / "native_compile_adapter.py").write_text(f'''
import importlib.util
s=importlib.util.spec_from_file_location('emuflow.actual_adapter',{str(package / 'native_compile_adapter.py')!r})
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
raise SystemExit(m.main())
''')
    (source / "multi_fpga_flow.py").write_text(f'''
from pathlib import Path
import os,sys
from emuflow.tool_runtime import NativeToolProcess
class _CompileContext:
 def __init__(self,values,**kw):self.__dict__.update(values)
def run_multi_fpga_flow(output_dir: Path=None):pass
def _compile_preflight(ctx):ctx.output_dir.mkdir()
def _compile_frontend(ctx):
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
