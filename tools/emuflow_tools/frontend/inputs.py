"""Bounded file references for native frontend input adoption and validation."""
from pathlib import Path
import json
import os
import tempfile
from types import SimpleNamespace

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow_tools.frontend.frontend_native import read_frontend_result

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


def prepare_inputs(ctx, *, request_directory=None):
    """Submit input references; C++ binds selectors and constructs the request."""
    from emuflow_tools.frontend.configuration import prepare_configuration
    require_native_schema("frontend-configuration-request-v1")
    mode = "prepared" if ctx.prepared_frontend is not None or ctx.prepared_timing is not None else "yosys-json"
    request, _ = prepare_configuration(ctx, mode, request_directory)
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
