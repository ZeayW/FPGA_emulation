"""Bounded RTL frontend configuration and native stage result references."""
from pathlib import Path
import json
import os
import tempfile
from types import SimpleNamespace

from emuflow.common.errors import EmuFlowError
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess


def prepare_frontend(ctx, *, request_directory=None):
    """Submit finite RTL settings to the sole native configuration producer."""
    from emuflow_tools.frontend.configuration import prepare_configuration
    require_native_schema("frontend-configuration-request-v1")
    tool = resolve_native_executable("yosys", ctx.yosys)
    path, bindings = prepare_configuration(ctx, "rtl", request_directory, engine=tool)
    ctx.native_frontend_request = path
    ctx.native_frontend_generation = tuple(str(v) for v in native_file_generation(path))
    return bindings


def read_frontend_summary(path, generation):
    """Read only the committed control summary, never the detailed report."""
    path = Path(path)
    if tuple(str(value) for value in native_file_generation(path)) != tuple(str(value) for value in generation):
        raise EmuFlowError("native frontend summary generation changed")
    if path.stat().st_size > 512*1024:
        raise EmuFlowError("native frontend summary exceeds bound")
    result = json.loads(path.read_text(encoding="utf-8"))
    if (result.get("schema") != "emuflow.phase1-report/v1" or result.get("phase") != 1
            or result.get("status") != "pass" or set(result) != {"schema", "phase", "status", "design", "platform", "synthesis"}):
        raise EmuFlowError("native frontend summary is invalid")
    if tuple(str(value) for value in native_file_generation(path)) != tuple(str(value) for value in generation):
        raise EmuFlowError("native frontend summary generation changed")
    return result


def read_frontend_result(operation, root, expected_stage):
    """Consume only the exact published summary/IR references from one owner."""
    operation, root = Path(operation), Path(root)
    generation = native_file_generation(operation)
    if operation.stat().st_size > 8 * 1024 * 1024:
        raise EmuFlowError('native frontend operation exceeds bound')
    proof = json.loads(operation.read_text(encoding='utf-8'))
    if (proof.get('schema') != 'emuflow.native-stage-result/v1' or proof.get('stage') != expected_stage
            or proof.get('status') != 'pass' or len(proof.get('outputs', [])) != 2):
        raise EmuFlowError('native frontend producer identity disagrees')
    fields = ('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
    outputs = ((root/'frontend-summary.json', 'phase1-summary-v1'),
               (root/'design.emuir.sqlite', 'emuir-compact-v2'))
    for row, (path, schema) in zip(proof['outputs'], outputs):
        values = tuple(row.get('generation', {}).get(k) for k in fields)
        if (row.get('schema') != schema or row.get('path') != str(path.resolve())
                or any(type(v) is not int for v in values) or values != native_file_generation(path)):
            raise EmuFlowError('native frontend output generation changed')
    summary_generation = tuple(proof['outputs'][0]['generation'][k] for k in fields)
    summary = read_frontend_summary(outputs[0][0], summary_generation)
    for row, (path, _) in zip(proof['outputs'], outputs):
        if tuple(row['generation'][k] for k in fields) != native_file_generation(path):
            raise EmuFlowError('native frontend output generation changed')
    if native_file_generation(operation) != generation:
        raise EmuFlowError('native frontend operation generation changed')
    return summary


def run_rtl_frontend(sources, top, platform_path, output_root, *, clocks=(),
                     include_dirs=(), defines=(), yosys=None, xilinx_mapping_strategy=None):
    """Use the complete native RTL owner from optional tooling and Python APIs."""
    output_root = Path(output_root).absolute()
    output_root.parent.mkdir(parents=True, exist_ok=True)
    ctx = SimpleNamespace(sources=tuple(sources), top=top, clocks=tuple(clocks),
        include_dirs=tuple(include_dirs), defines=tuple(defines), yosys=yosys,
        platform_path=Path(platform_path), output_dir=output_root,
        xilinx_mapping_strategy=xilinx_mapping_strategy)
    with tempfile.TemporaryDirectory(prefix='.frontend-rtl-host-', dir=output_root.parent) as temporary:
        root = Path(temporary)
        bindings = prepare_frontend(ctx, request_directory=root)
        request = ctx.native_frontend_request
        native = resolve_native_executable('emuflow-native')
        operation = root/'operation.json'
        command = [native, '--report', str(operation), 'phase1', 'run', str(request),
                   str(output_root/'frontend/phase1')]
        with NativeToolProcess(native, command, environment=dict(os.environ), scratch_parent=root) as process:
            result = process.finish()
        if result.returncode:
            raise EmuFlowError('native RTL frontend failed: ' + result.stdout)
        if tuple(str(v) for v in native_file_generation(request)) != ctx.native_frontend_generation:
            raise EmuFlowError('native RTL request generation changed')
        summary = read_frontend_result(operation, output_root/'frontend/phase1', 'phase1.run')
        for path, generation in bindings:
            if native_file_generation(path) != generation:
                raise EmuFlowError('native RTL original source generation changed')
        return summary
