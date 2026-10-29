"""Bounded optional language host for native frontend configuration."""
import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.physical.xilinx_primitives import DEFAULT_XILINX_PRIMITIVE_LIBRARY
from emuflow.platform.platform import Platform

_FIELDS = ('device', 'inode', 'bytes', 'mtime_seconds', 'mtime_nanoseconds',
           'ctime_seconds', 'ctime_nanoseconds')


def prepare_configuration(ctx, mode, request_directory=None, *, engine=None):
    """Pass configuration only; scripts, selectors and source leases are native."""
    def path(value):
        return None if value is None else str(Path(value).absolute())
    output = Path(ctx.output_dir).absolute()
    target = Path(request_directory).absolute() if request_directory is not None else (
        output / 'frontend' if mode == 'rtl' else output / '.native-frontend-input-request')
    board_generation = native_file_generation(ctx.platform_path)
    platform = Platform.load(ctx.platform_path).to_dict()
    if native_file_generation(ctx.platform_path) != board_generation:
        raise ValidationError('frontend BoardDB changed during configuration')
    value = {
        'schema': 'emuflow.frontend-configuration/v1', 'mode': mode,
        'output_dir': str(output), 'working_directory': str(Path.cwd()),
        'top': ctx.top or '', 'clocks': list(ctx.clocks),
        'clock_periods': dict(getattr(ctx, 'clock_periods', None) or {}),
        'mapping_profile': getattr(ctx, 'mapping_profile', 'xilinx-ultrascaleplus-open-v1'),
        'mapping_strategy': getattr(ctx, 'xilinx_mapping_strategy', None),
        'platform': platform, 'board_generation': list(board_generation),
        'board': path(ctx.platform_path), 'library': path(DEFAULT_XILINX_PRIMITIVE_LIBRARY),
        'engine': engine, 'rtl_sources': [path(p) for p in ctx.sources],
        'include_dirs': [path(p) for p in getattr(ctx, 'include_dirs', ())], 'defines': list(getattr(ctx, 'defines', ())),
        'source_checksums': getattr(ctx, 'source_checksums', None),
        'prepared_frontend': path(getattr(ctx, 'prepared_frontend', None)),
        'prepared_timing': path(getattr(ctx, 'prepared_timing', None)),
        'yosys_json': path(getattr(ctx, 'yosys_json', None)),
    }
    text = json.dumps(value, separators=(',', ':'), allow_nan=False)
    if len(text.encode()) > 4 * 1024 * 1024:
        raise ValidationError('frontend configuration exceeds bound')
    native = resolve_native_executable('emuflow-native')
    with tempfile.TemporaryDirectory(prefix='.frontend-configuration-') as scratch:
        root = Path(scratch).resolve()
        request, proof = root / 'configuration.json', root / 'operation.json'
        request.write_text(text, encoding='utf-8')
        with NativeToolProcess(native, [native, '--report', str(proof), 'phase1',
                'prepare-configuration', str(request), str(target)],
                environment=dict(os.environ), scratch_parent=root) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('native frontend configuration failed: ' + completed.stdout)
        if proof.stat().st_size > 8 * 1024 * 1024:
            raise ValidationError('frontend configuration proof exceeds bound')
        operation = json.loads(proof.read_text(encoding='utf-8'))
        name = 'native-frontend-request.bin' if mode == 'rtl' else 'inputs.json'
        schema = 'frontend-request-v1' if mode == 'rtl' else 'frontend-input-request-v1'
        if (operation.get('stage') != 'phase1.prepare-configuration'
                or operation.get('status') != 'pass' or len(operation.get('outputs', [])) != 1):
            raise ValidationError('frontend configuration producer disagrees')
        ref = operation['outputs'][0]
        published = target / name
        if (ref.get('schema') != schema or ref.get('path') != str(published.resolve())
                or tuple(ref.get('generation', {}).get(k) for k in _FIELDS) != native_file_generation(published)):
            raise ValidationError('frontend configuration output generation changed')
        bindings = []
        for source in operation.get('inputs', []):
            source_path = Path(source['path'])
            generation = tuple(source.get('generation', {}).get(k) for k in _FIELDS)
            if native_file_generation(source_path) != generation:
                raise ValidationError('frontend configuration source generation changed')
            if source_path != request:
                bindings.append((source_path, generation))
        return published, tuple(bindings)
