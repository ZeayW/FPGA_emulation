"""Bounded RTL frontend configuration and native stage result references."""
from pathlib import Path
import json
import os
import tempfile
from types import SimpleNamespace

from emuflow.common.errors import EmuFlowError
from emuflow.common.io import file_sha256
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.platform.platform import Platform
from emuflow.frontend.synthesis import build_yosys_script, DEFAULT_XILINX_MAPPING_STRATEGY
from emuflow.physical.xilinx_primitives import DEFAULT_XILINX_PRIMITIVE_LIBRARY, XILINX_ULTRASCALEPLUS_OPEN_PROFILE
from emuflow.common.tool_runtime import NativeToolProcess


def prepare_frontend(ctx, *, request_directory=None):
    """Generate only engine argv, board configuration and source bindings."""
    require_native_schema("frontend-request-v1")
    sources = tuple(Path(path).resolve() for path in ctx.sources)
    includes = tuple(Path(path).resolve() for path in ctx.include_dirs)
    if not sources or not ctx.top:
        raise EmuFlowError("native RTL frontend requires sources and top")
    for path in sources:
        if not path.is_file():
            raise EmuFlowError(f"RTL source does not exist: {path}")
    for path in includes:
        if not path.is_dir():
            raise EmuFlowError(f"Verilog include directory does not exist: {path}")
    strategy = ctx.xilinx_mapping_strategy or DEFAULT_XILINX_MAPPING_STRATEGY
    defines = tuple(ctx.defines)
    tool = resolve_native_executable("yosys", ctx.yosys)
    script = build_yosys_script(sources, ctx.top, Path("<stdout>"), "xcup", "native",
        include_dirs=includes, defines=defines, mapping_profile=XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
        mapping_strategy=strategy, stream_json=True, hierarchical_interchange=True)
    library = DEFAULT_XILINX_PRIMITIVE_LIBRARY.resolve()
    checksums = getattr(ctx, "source_checksums", None)
    declared = []
    if checksums is not None:
        if type(checksums) is not dict or not checksums or len(checksums) > 16384:
            raise EmuFlowError("invalid design source checksum inventory")
        for raw, digest in checksums.items():
            path = Path(raw)
            if not path.is_absolute() or not path.is_file() or not isinstance(digest, str) or len(digest) != 64:
                raise EmuFlowError("invalid design source checksum reference")
            generation = native_file_generation(path)
            if file_sha256(path) != digest or native_file_generation(path) != generation:
                raise EmuFlowError("design RTL/include changed after run preparation")
            declared.append((path, generation))
        if any(str(path) not in checksums for path in sources):
            raise EmuFlowError("design source checksum coverage is incomplete")
    bindings = tuple((path, native_file_generation(path)) for path in dict.fromkeys((library, *sources, ctx.platform_path.resolve(), Path(tool), *(path for path, _ in declared))))
    bound_by_path = dict(bindings)
    if any(bound_by_path[path] != generation for path, generation in declared):
        raise EmuFlowError("design source generation changed during request preparation")
    board = json.dumps(Platform.load(ctx.platform_path).to_dict(), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    synthesis = json.dumps({
        "provider": "yosys", "mode": "xilinx-ultrascaleplus-open", "mapping_profile": XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
        "sources": [str(path) for path in sources], "include_dirs": [str(path) for path in includes],
        "defines": list(defines), "interchange_retention": "never-materialized-stream",
        "tool_report": {"status": "pass", "provider": "yosys-synth-xilinx", "family": "xcup", "policy": "native",
                        "mapping_profile": XILINX_ULTRASCALEPLUS_OPEN_PROFILE, "mapping_strategy": strategy,
                        "interchange": "stdout-hierarchical-to-indexed-phase1"}}, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(board.encode()) > 1024*1024 or len(synthesis.encode()) > 1024*1024 or len(sources)>16384:
        raise EmuFlowError("native frontend configuration exceeds bound")
    root = ctx.output_dir / "frontend"
    request_root = Path(request_directory) if request_directory is not None else root
    request_root.mkdir(parents=True, exist_ok=True)
    request = request_root / "native-frontend-request.bin"
    # Configuration inputs are bound before serialization, not rebound after
    # the engine has run. Native runtime independently checks every generation.
    writer = CheckedRequestWriter(request)
    try:
        writer.put("QI", 0x31544E52464D5545, 1)
        writer.text(str(root / "phase1")); writer.text(ctx.top)
        writer.catalog(sorted(set(ctx.clocks)))
        writer.text(board); writer.text(synthesis); writer.put("I", 0)
        writer.catalog((tool, "-q", "-p", script)); writer.text(str(Path.cwd()))
        writer.put("Q", 0)
        writer.text(str(library)); writer.put("3Q4q", *bindings[0][1])
        writer.put("I", len(bindings)-1)
        for path, generation in bindings[1:]:
            writer.text(str(path)); writer.put("3Q4q", *generation)
        writer.finish()
    finally:
        writer.close()
    if request.stat().st_size > 4*1024*1024:
        request.unlink()
        raise EmuFlowError("native frontend request exceeds bound")
    for path, generation in bindings:
        if native_file_generation(path) != generation:
            request.unlink()
            raise EmuFlowError("native frontend input generation changed during configuration")
    ctx.native_frontend_request = request
    ctx.native_frontend_generation = tuple(str(value) for value in native_file_generation(request))
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
