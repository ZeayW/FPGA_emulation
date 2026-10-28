"""Bounded file-locator host for the complete native RWRoute transaction."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess


def run_native_rwroute_stage(
    input_path, output_path, *, rapidwright_jar, java, java_source,
    device_data_root, timing_data_dir, mapped_path=None, handoff_path=None,
    placement_path=None, virtual_io_contract=None, resource_limits=None,
    export_input=False, resume=False,
):
    from emuflow.physical.rapidwright.xilinx_rwroute import RAPIDWRIGHT_TIMING_CONTRACT
    require_native_schema('rwroute-stage-request-v1')
    require_native_schema('rwroute-stage-report-v1')
    output_path = Path(output_path).absolute()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    java = Path(java).absolute()
    def source(path):
        path = Path(path).resolve()
        return {'path': str(path), 'generation': native_file_generation(path)}
    device_data_root, timing_data_dir = Path(device_data_root), Path(timing_data_dir)
    sources = {name: source(path) for name, path in (
        ('java', java), ('javac', java.with_name('javac')), ('jar', rapidwright_jar),
        ('java_source', java_source), ('contract', RAPIDWRIGHT_TIMING_CONTRACT),
        ('parts', device_data_root/'data/parts.db'),
        ('device', device_data_root/'data/devices/virtexuplus/xcvu19p_db.dat'),
        ('intersite', timing_data_dir/'intersite_delay_terms.txt'),
        ('intrasite', timing_data_dir/'intrasite_delay_terms.txt'),
    )}
    for name, path in (('mapped',mapped_path), ('packed',handoff_path), ('placement',placement_path)):
        if path is not None:
            sources[name] = source(path)
    if not export_input:
        sources['input'] = source(input_path)
    io_text = json.dumps(virtual_io_contract, sort_keys=True, separators=(',', ':'), allow_nan=False)
    config = {'schema':'emuflow.rwroute-stage-request/v1', 'sources':sources,
        'output_path':str(output_path), 'device_data_root':str(device_data_root.resolve()), 'max_heap':os.environ.get('EMUFLOW_RAPIDWRIGHT_MAX_HEAP','32g'),
        'virtual_io':virtual_io_contract,
        'virtual_io_sha256':hashlib.sha256(io_text.encode()).hexdigest() if virtual_io_contract is not None else None,
        'resource_limits':resource_limits or {}, 'export_input':bool(export_input), 'resume':bool(resume),
        'timeout_ms':21600000}
    text = json.dumps(config, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(text.encode()) > 8*1024*1024:
        raise ValidationError('RWRoute control document exceeds bound')
    with tempfile.TemporaryDirectory(prefix='.rwroute-host-', dir=output_path.parent) as temporary:
        root = Path(temporary)
        request, proof, summary = root/'request.json', root/'operation.json', root/'summary.json'
        request.write_text(text, encoding='utf-8')
        executable = resolve_native_executable('emuflow-native')
        command = [executable,'--report',str(proof),'physical','run-rwroute',str(request),str(summary)]
        with NativeToolProcess(executable, command, environment=dict(os.environ),
            scratch_parent=root, termination_grace_ms=5000) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('native RWRoute stage failed: '+completed.stdout)
        if proof.stat().st_size > 262144 or summary.stat().st_size > 8*1024*1024:
            raise ValidationError('native RWRoute result exceeds bound')
        operation = json.loads(proof.read_text())
        outputs = operation.get('outputs',[])
        if (operation.get('status') != 'pass' or operation.get('stage') != 'physical.run-rwroute'
                or len(outputs) != 1 or outputs[0].get('path') != str(summary.resolve())
                or outputs[0].get('schema') != 'rwroute-stage-report-v1'):
            raise ValidationError('native RWRoute proof disagrees')
        generation = tuple(outputs[0]['generation'].get(key) for key in ('device','inode','bytes',
            'mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds'))
        if generation != native_file_generation(summary):
            raise ValidationError('native RWRoute report generation changed')
        result = json.loads(summary.read_text())
        if (result.get('schema') != 'emuflow.rwroute-stage-report/v1' or result.get('status') != 'pass'
                or result.get('implementation_id') != 'native-rwroute-stage-v1'
                or result.get('route',{}).get('status') != 'pass'
                or result['route'].get('output') != str(output_path.resolve())):
            raise ValidationError('native RWRoute result disagrees')
        result['route']['native_owner'] = {'implementation_id':result['implementation_id'],
            'engine':result['engine'], 'metrics':operation['metrics'], 'input':result['input']}
        return result
