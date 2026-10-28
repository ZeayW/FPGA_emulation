"""Bounded locator bridge to the native Vivado input generation owner."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess


def prepare_vivado_inputs(ir_path, output_dir, runtime, virtual_io_contract=None):
    source = Path(ir_path).resolve()
    parent = Path(output_dir).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    generation = native_file_generation(source)
    for schema in ('vivado-input-request-v1', 'vivado-input-report-v1'):
        require_native_schema(schema)
    destination = Path(tempfile.mkdtemp(prefix='vivado-inputs.generation.', dir=parent))
    destination.rmdir()
    try:
        with tempfile.TemporaryDirectory(prefix='.vivado-input-host-', dir=parent) as temporary:
            root = Path(temporary)
            request, proof = root/'request.json', root/'operation.json'
            payload = json.dumps({'schema': 'emuflow.vivado-input-request/v1',
                'source': {'path': str(source), 'generation': generation},
                'runtime': runtime, 'virtual_io_contract': virtual_io_contract},
                sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            if len(payload.encode()) > 8*1024*1024:
                raise ValidationError('Vivado input control document exceeds bound')
            request.write_text(payload, encoding='utf-8')
            executable = resolve_native_executable('emuflow-native')
            with NativeToolProcess(executable, [executable, '--report', str(proof),
                'physical', 'prepare-vivado-inputs', str(request), str(destination)],
                environment=dict(os.environ), scratch_parent=root) as process:
                completed = process.finish()
            if completed.returncode:
                raise ValidationError('native Vivado inputs failed: '+completed.stdout)
            result = json.loads(proof.read_text())
            outputs = result.get('outputs')
            if (result.get('stage') != 'physical.prepare-vivado-inputs' or result.get('status') != 'pass'
                    or not isinstance(outputs, list) or not outputs):
                raise ValidationError('native Vivado input proof disagrees')
            for artifact in outputs:
                path = Path(artifact['path'])
                stamped = tuple(artifact['generation'].get(field) for field in (
                    'device', 'inode', 'bytes', 'mtime_seconds', 'mtime_nanoseconds',
                    'ctime_seconds', 'ctime_nanoseconds'))
                if (path.parent != destination or path.is_symlink()
                        or stamped != native_file_generation(path)):
                    raise ValidationError('native Vivado input generation changed')
            path = destination/'summary.json'
            if path.stat().st_size > 262144:
                raise ValidationError('native Vivado input summary exceeds bound')
            summary = json.loads(path.read_text())
            if (summary.get('schema') != 'emuflow.vivado-input-report/v1'
                    or summary.get('status') != 'pass'
                    or summary.get('implementation_id') != 'native-vivado-inputs-v1'
                    or native_file_generation(source) != generation):
                raise ValidationError('native Vivado input identity disagrees')
            expected = {'summary.json', 'runtime.xdc'}
            if 'fixed_io' in summary:
                expected.update(('physical_top.sv', 'fixed-io-query.tsv', 'fixed-io-timing-map.tsv'))
            if {Path(row['path']).name for row in outputs} != expected or len(outputs) != len(expected):
                raise ValidationError('native Vivado input coverage disagrees')
            return summary
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
