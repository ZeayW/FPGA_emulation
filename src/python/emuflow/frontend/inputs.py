"""Bounded file references for native frontend input adoption and validation."""
from pathlib import Path
import json
import os
import tempfile
from types import SimpleNamespace

from emuflow.common.errors import ValidationError
from emuflow.common.io import write_json
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.physical.xilinx_primitives import DEFAULT_XILINX_PRIMITIVE_LIBRARY
from emuflow.platform.platform import Platform
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.frontend.frontend_native import read_frontend_result

PROFILE = "xilinx-ultrascaleplus-open-v1"


def _small(path):
    path = Path(path)
    generation = native_file_generation(path)
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValidationError("frontend control document exceeds bound")
    value = json.loads(path.read_text(encoding="utf-8"))
    if native_file_generation(path) != generation:
        raise ValidationError("frontend control generation changed")
    return value


def _source(role, path, schema):
    path = Path(path).resolve(strict=True)
    return {"role": role, "path": str(path), "schema": schema,
            "generation": list(native_file_generation(path))}


def _prepared_sources(frontend, timing):
    frontend, timing = Path(frontend).resolve(), Path(timing).resolve()
    phase1 = frontend / "phase1" if (frontend / "phase1").is_dir() else frontend
    sources = [_source("emuir", phase1 / "design.emuir.sqlite", "emuir-compact-v2")]
    report = phase1 / "phase1_report.json"
    if not report.is_file():
        report = phase1 / "phase1_report.source.json"
    sources.extend((_source("phase1-report", report, "frontend-input-report-v1"),
                    _source("phase1-board", phase1 / "platform.normalized.json", "boarddb-v1")))
    for role, name, schema, key, store in (
        ("path-selector", "path-database.json", "sta-path-selector-v1", "paths", "timing-path-store-v1"),
        ("weight-selector", "partition-net-weights.json", "net-weight-selector-v2", "weights", "net-weight-store-v1"),
    ):
        selector = _source(role, timing / name, schema)
        value = _small(selector["path"])
        member = value.get("payloads", {}).get(key, {}).get("path")
        if not isinstance(member, str) or Path(member).name != member or member in {".", "..", ""}:
            raise ValidationError("prepared payload must be a sibling of its manifest")
        sources.extend((selector, _source("path-store" if key == "paths" else "weight-store", timing / member, store)))
    report = timing / "experiment-timing-report.json"
    if report.is_file():
        sources.append(_source("timing-report", report, "frontend-input-report-v1"))
    return sources


def prepare_inputs(ctx, *, request_directory=None):
    """Bind input files and small configuration; C++ imports and publishes."""
    require_native_schema("frontend-input-request-v1")
    prepared = ctx.prepared_frontend is not None or ctx.prepared_timing is not None
    if prepared:
        if ctx.prepared_frontend is None or ctx.prepared_timing is None or ctx.sources or ctx.yosys_json is not None:
            raise ValidationError("prepared continuation requires both Phase 1 and Phase 2 boundaries and no synthesis inputs")
        sources = _prepared_sources(ctx.prepared_frontend, ctx.prepared_timing)
    else:
        if ctx.yosys_json is None or ctx.sources or ctx.include_dirs or ctx.defines:
            raise ValidationError("supplied Yosys JSON requires no RTL, include directories or defines")
        sources = [_source("json", ctx.yosys_json, "yosys-source-document-v1"),
                   _source("library", DEFAULT_XILINX_PRIMITIVE_LIBRARY, "xilinx-primitive-library-v1")]
    sources.append(_source("board", ctx.platform_path, "boarddb-v1"))
    request = (Path(request_directory) / "inputs.json" if request_directory is not None
               else ctx.output_dir / ".native-frontend-input-request/inputs.json")
    request.parent.mkdir(parents=True, exist_ok=True)
    write_json(request, {"schema": "emuflow.frontend-input-request/v1",
        "mode": "prepared" if prepared else "yosys-json", "sources": sources,
        "top": ctx.top or "", "clocks": sorted(set(ctx.clocks)),
        "clock_periods": dict(ctx.clock_periods or {}), "mapping_profile": ctx.mapping_profile,
        "platform": Platform.load(ctx.platform_path).to_dict()}, compact=True)
    ctx.native_frontend_input_request = request
    ctx.native_frontend_input_generation = tuple(str(v) for v in native_file_generation(request))
    return request


def run_supplied_frontend(yosys_json, platform_path, output_root, *, top=None, clocks=()):
    """Import a supplied source through the maintained native frontend owner.

    The result is a bounded summary. Detailed evidence and the compact IR use
    the same frontend/phase1 layout as native compile, with no host relocation.
    """
    output_root = Path(output_root).absolute()
    output_root.parent.mkdir(parents=True, exist_ok=True)
    ctx = SimpleNamespace(prepared_frontend=None, prepared_timing=None, sources=(),
        include_dirs=(), defines=(), yosys_json=Path(yosys_json),
        platform_path=Path(platform_path), output_dir=output_root, top=top,
        clocks=tuple(clocks), clock_periods={}, mapping_profile=PROFILE)
    with tempfile.TemporaryDirectory(prefix='.frontend-input-host-', dir=output_root.parent) as temporary:
        root = Path(temporary)
        request = prepare_inputs(ctx, request_directory=root)
        generation = native_file_generation(request)
        sources = _small(request)['sources']
        native = resolve_native_executable('emuflow-native')
        operation = root / 'operation.json'
        command = [native, '--report', str(operation), 'phase1', 'import-inputs', str(request), str(output_root)]
        with NativeToolProcess(native, command, environment=dict(os.environ), scratch_parent=root) as process:
            result = process.finish()
        if result.returncode:
            raise ValidationError('native supplied frontend failed: ' + result.stdout)
        if native_file_generation(request) != generation:
            raise ValidationError('frontend request generation changed')
        summary = read_frontend_result(operation, output_root/'frontend/phase1', 'phase1.import-inputs')
        for source in sources:
            if native_file_generation(Path(source['path'])) != tuple(source['generation']):
                raise ValidationError('native frontend original source generation changed')
        return summary


def read_timing_summary(path, generation):
    path = Path(path)
    if path.is_symlink() or tuple(native_file_generation(path)) != tuple(generation):
        raise ValidationError("prepared timing report generation changed")
    value = _small(path)
    if (set(value) != {"schema", "status", "implementation_id", "summary", "validation", "sources"}
            or value["schema"] != "emuflow.prepared-timing-summary/v1" or value["status"] != "pass"
            or value["implementation_id"] != "native-frontend-input-v1"):
        raise ValidationError("prepared timing report identity disagrees")
    for source in value["sources"]:
        if native_file_generation(Path(source["path"])) != tuple(source["generation"]):
            raise ValidationError("prepared timing source generation changed")
    if native_file_generation(path) != tuple(generation):
        raise ValidationError("prepared timing report generation changed")
    return value
