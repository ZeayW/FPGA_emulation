import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from emuflow.errors import EmuFlowError
from emuflow.process_output import run_with_bounded_output
from emuflow.synthesis import _run_yosys_json_consumer
from emuflow.tool_runtime import NativeToolProcess, _request_bytes, native_tool_runner
from tests.native_build import emuflow_native


@pytest.fixture(scope="module")
def runner():
    return str(emuflow_native())


def test_versioned_runtime_sibling_wins_over_symlinked_engine_base(tmp_path, monkeypatch):
    current, base = tmp_path / "current/bin", tmp_path / "base/bin"
    current.mkdir(parents=True)
    base.mkdir(parents=True)
    for path in [current / "emuflow-native", base / "emuflow-native", base / "yosys"]:
        path.write_text("fixture")
        path.chmod(0o755)
    engine = current / "yosys"
    engine.symlink_to(base / "yosys")
    monkeypatch.setattr("emuflow.tool_runtime.native_install_roots", lambda: ())
    assert native_tool_runner(str(engine)) == str(current / "emuflow-native")


def process_alive(pid):
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


def await_file(path):
    deadline = time.monotonic() + 5
    while not path.exists():
        assert time.monotonic() < deadline, "engine did not launch"
        time.sleep(0.01)


def test_argv_environment_directory_and_child_metrics(runner, tmp_path):
    literal = '中文 quotes " $(`touch never-created`)'
    command = [sys.executable, "-c", "import os,sys,json; print(json.dumps([sys.argv[1],os.getcwd(),dict(os.environ)]))", literal]
    with NativeToolProcess(runner, command, cwd=tmp_path, environment={"ONLY": "value"}) as process:
        result = process.finish()
        value = json.loads(result.stdout)
        assert result.returncode == 0
        assert value[:2] == [literal, str(tmp_path)]
        assert value[2]["ONLY"] == "value"
        assert "PATH" not in value[2]
        assert result.native_metrics["rss_scope"] == "wait4-maximum-process-high-water"
        assert result.native_metrics["wall_seconds"] > 0
        assert result.native_metrics["maximum_process_rss_kib"] > 0
    assert not list(tmp_path.iterdir())


def test_empty_argv_and_environment_values_survive_native_protocol(runner, tmp_path):
    command = [sys.executable, "-c", "import os,sys,json; print(json.dumps([sys.argv[1:],dict(os.environ)]))", "", "after-empty"]
    with NativeToolProcess(runner, command, cwd=tmp_path,
                           environment={"EMPTY": "", "NONEMPTY": "kept"}) as process:
        result = process.finish()
        assert result.returncode == 0
        arguments, environment = json.loads(result.stdout)
        assert arguments == ["", "after-empty"]
        assert environment["EMPTY"] == "" and environment["NONEMPTY"] == "kept"
        assert "PATH" not in environment


@pytest.mark.parametrize("environment", [{"": "value"}, {"BAD=NAME": ""}])
def test_empty_value_support_does_not_accept_invalid_environment_names(runner, tmp_path, environment):
    marker = tmp_path / "started"
    command = [sys.executable, "-c", "open(__import__('sys').argv[1],'w').write('bad')", str(marker)]
    with NativeToolProcess(runner, command, environment=environment) as process:
        with pytest.raises(EmuFlowError, match="supervisor failed|launch failed"):
            process.finish()
    assert not marker.exists()


def test_result_uses_common_operation_schema(runner):
    jsonschema = pytest.importorskip("jsonschema")
    from referencing import Registry, Resource

    schemas = Path(__file__).resolve().parents[1] / "schemas"
    operation_path = schemas / "native-stage-result-v1.schema.json"
    operation = json.loads(operation_path.read_text())
    registry = Registry().with_resource(operation_path.as_uri(), Resource.from_contents(operation))
    schema_path = schemas / "external-tool-result-v1.schema.json"
    schema = {**json.loads(schema_path.read_text()), "$id": schema_path.as_uri()}
    with NativeToolProcess(runner, [sys.executable, "-c", "print('validated')"]) as process:
        process.finish()
        result = json.loads(process._result_path.read_text())
        jsonschema.Draft202012Validator(schema, registry=registry).validate(result)
        assert result["operation"]["inputs"][0]["schema"] == "external-tool-request-v1"
        assert result["operation"]["inputs"][1]["schema"] == "external-tool-executable-v1"


def test_combined_binary_tail_and_nonzero_exit(runner, tmp_path):
    command = [sys.executable, "-c", "import os; os.write(1,b'a'*200000); os.write(2,b'END\\xff'); raise SystemExit(7)"]
    with NativeToolProcess(runner, command, maximum_bytes=128, scratch_parent=tmp_path) as process:
        result = process.finish()
        assert result.returncode == 7
        assert result.stdout == "a" * 124 + "END\ufffd"
        assert process.finish() is result
    assert not list(tmp_path.iterdir())


def test_public_bounded_runner_selects_native_without_python_retry(runner, monkeypatch):
    monkeypatch.setattr("emuflow.process_output.native_tool_runner", lambda _: runner)
    calls = []
    original = subprocess.Popen

    def record(command, **kwargs):
        calls.append(command)
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", record)
    completed = run_with_bounded_output([sys.executable, "-c", "print('native')"])
    assert completed.stdout == "native\n"
    assert len(calls) == 1 and calls[0][:2] == [runner, "run-tool"]
    with pytest.raises(EmuFlowError, match="supervisor failed"):
        monkeypatch.setattr("emuflow.process_output.native_tool_runner", lambda _: "/does/not/exist")
        run_with_bounded_output([sys.executable, "-c", "print('must not retry')"])


def test_timeout_kills_ignoring_engine_and_descendant(runner, tmp_path):
    pid_path = tmp_path / "pids"
    script = (
        "import os,signal,subprocess,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)']); "
        "open(sys.argv[1],'w').write(str(os.getpid())+' '+str(child.pid)); time.sleep(60)"
    )
    started = time.monotonic()
    with NativeToolProcess(
        runner, [sys.executable, "-c", script, str(pid_path)],
        timeout_ms=800, termination_grace_ms=50, scratch_parent=tmp_path,
    ) as process:
        with pytest.raises(subprocess.TimeoutExpired):
            process.finish()
    assert time.monotonic() - started < 5
    assert all(not process_alive(int(pid)) for pid in pid_path.read_text().split())


def test_cancellation_is_reaped_before_returning(runner, tmp_path):
    pid_path = tmp_path / "pid"
    script = "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(60)"
    with NativeToolProcess(runner, [sys.executable, "-c", script, str(pid_path)], scratch_parent=tmp_path) as process:
        await_file(pid_path)
        process.process.send_signal(signal.SIGTERM)
        with pytest.raises(EmuFlowError, match="cancelled"):
            process.finish()
    assert not process_alive(int(pid_path.read_text()))


def test_engine_exit_reaps_pipe_holding_descendant(runner, tmp_path):
    pid_path = tmp_path / "pid"
    script = (
        "import subprocess,sys; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "open(sys.argv[1],'w').write(str(child.pid)); print('done')"
    )
    with NativeToolProcess(runner, [sys.executable, "-c", script, str(pid_path)]) as process:
        assert process.finish().stdout == "done\n"
    assert not process_alive(int(pid_path.read_text()))


def test_streamed_yosys_stdout_preserves_document_and_bounded_diagnostics(runner, tmp_path, monkeypatch):
    monkeypatch.setattr("emuflow.synthesis.native_tool_runner", lambda _: runner)
    command = [sys.executable, "-c", "import os; os.write(2,b'x'*3000000+b'END'); os.write(1,b'{\"design\":\"stream\"}')"]
    log = tmp_path / "yosys.log"
    value = _run_yosys_json_consumer(command, json.load, tmp_path, log, "Yosys")
    assert value == {"design": "stream"}
    assert log.stat().st_size == 2 * 1024 * 1024
    assert log.read_text().endswith("END")
    assert list(tmp_path.iterdir()) == [log]


def test_stream_consumer_failure_cancels_engine(runner, tmp_path, monkeypatch):
    monkeypatch.setattr("emuflow.synthesis.native_tool_runner", lambda _: runner)
    pid_path = tmp_path / "pid"
    command = [sys.executable, "-c", "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); print('diagnostic',file=sys.stderr,flush=True); print('start',flush=True); time.sleep(60)", str(pid_path)]

    def fail(stream):
        assert stream.readline() == "start\n"
        raise ValueError("importer rejected document")

    with pytest.raises(ValueError, match="importer rejected"):
        _run_yosys_json_consumer(command, fail, tmp_path, tmp_path / "yosys.log", "Yosys")
    assert not process_alive(int(pid_path.read_text()))
    assert (tmp_path / "yosys.log").read_text() == "diagnostic\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["pid", "yosys.log"]


def test_failed_engine_retains_error_precedence_over_truncated_document(runner, tmp_path, monkeypatch):
    monkeypatch.setattr("emuflow.synthesis.native_tool_runner", lambda _: runner)
    command = [sys.executable, "-c", "import sys; print('{'); print('engine failure',file=sys.stderr); raise SystemExit(7)"]
    with pytest.raises(EmuFlowError, match="Yosys failed with exit code 7"):
        _run_yosys_json_consumer(command, json.load, tmp_path, tmp_path / "yosys.log", "Yosys")
    assert (tmp_path / "yosys.log").read_text() == "engine failure\n"


def test_resource_limits_are_applied_to_engine(runner):
    script = "import json,resource; print(json.dumps([resource.getrlimit(resource.RLIMIT_CPU),resource.getrlimit(resource.RLIMIT_FSIZE)]))"
    with NativeToolProcess(runner, [sys.executable, "-c", script], cpu_seconds=3, file_size_bytes=4096) as process:
        assert json.loads(process.finish().stdout) == [[3, 3], [4096, 4096]]


def test_cpu_limit_terminates_busy_engine(runner):
    with NativeToolProcess(runner, [sys.executable, "-c", "while True: pass"], cpu_seconds=1, timeout_ms=5000) as process:
        result = process.finish()
        assert result.returncode in {-signal.SIGKILL, -signal.SIGXCPU}
        assert result.native_metrics["user_cpu_seconds"] > 0.5


def request_payload(command):
    return _request_bytes(command, cwd=None, environment=None, forward_stdout=False,
                          maximum_bytes=128, timeout_ms=0, termination_grace_ms=50,
                          cpu_seconds=0, address_space_bytes=0, file_size_bytes=0)


@pytest.mark.parametrize("mutation", [lambda data: data[:-1], lambda data: data + b"trailing"])
def test_malformed_request_cannot_start_engine_or_replace_result(runner, tmp_path, mutation):
    marker = tmp_path / "started"
    request = tmp_path / "request"
    request.write_bytes(mutation(request_payload([sys.executable, "-c", "open(__import__('sys').argv[1],'w').write('bad')", str(marker)])))
    result = tmp_path / "result"
    result.write_text("previous")
    completed = subprocess.run([runner, "run-tool", str(request), str(result)], capture_output=True)
    assert completed.returncode != 0
    assert result.read_text() == "previous" and not marker.exists()


def test_alias_is_rejected_before_launch(runner, tmp_path):
    request = tmp_path / "request"
    original = request_payload([sys.executable, "-c", "raise RuntimeError('must not launch')"])
    request.write_bytes(original)
    completed = subprocess.run([runner, "run-tool", str(request), str(request)], capture_output=True)
    assert completed.returncode == 2 and request.read_bytes() == original


def test_missing_result_parent_is_rejected_before_launch(runner, tmp_path):
    request = tmp_path / "request"
    marker = tmp_path / "started"
    request.write_bytes(request_payload([sys.executable, "-c", "open(__import__('sys').argv[1],'w').write('bad')", str(marker)]))
    completed = subprocess.run([runner, "run-tool", str(request), str(tmp_path / "missing/result")], capture_output=True)
    assert completed.returncode == 2 and not marker.exists()


def test_exec_failure_does_not_look_like_engine_exit_127(runner, tmp_path):
    bad = tmp_path / "bad-executable"
    bad.write_bytes(b"not an executable")
    bad.chmod(0o755)
    with NativeToolProcess(runner, [str(bad)]) as process:
        with pytest.raises(EmuFlowError, match="launch failed"):
            process.finish()
    with NativeToolProcess(runner, [sys.executable, "-c", "raise SystemExit(127)"]) as process:
        assert process.finish().returncode == 127
