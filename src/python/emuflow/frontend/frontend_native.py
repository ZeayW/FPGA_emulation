"""Bounded RTL frontend configuration and native stage result references."""
from pathlib import Path
import json

from emuflow.common.errors import EmuFlowError
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.platform.platform import Platform
from emuflow.frontend.synthesis import build_yosys_script, DEFAULT_XILINX_MAPPING_STRATEGY
from emuflow.physical.xilinx_primitives import DEFAULT_XILINX_PRIMITIVE_LIBRARY, XILINX_ULTRASCALEPLUS_OPEN_PROFILE


def prepare_frontend(ctx):
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
    bindings = tuple((path, native_file_generation(path)) for path in (library, *sources, ctx.platform_path.resolve(), Path(tool)))
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
    root.mkdir(parents=True, exist_ok=True)
    request = root / "native-frontend-request.bin"
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
