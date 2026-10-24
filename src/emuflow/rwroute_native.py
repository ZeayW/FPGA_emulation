"""Bounded bridge to the shared native mapped/physical RWRoute exporter."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

from .errors import ValidationError
from .native_protocol import CheckedRequestWriter
from .native_tools import native_file_generation, resolve_native_executable
from .tool_runtime import NativeToolProcess


def export_native_rwroute(mapped_path, handoff_path, output_path, *, top, part,
                           digests, virtual_io_contract, resource_limits):
    inputs = (Path(mapped_path), Path(handoff_path))
    generations = tuple(native_file_generation(path) for path in inputs)
    if digests['packed_sha256'] != digests['placement_sha256']:
        raise ValidationError('one physical handoff has conflicting source digests')
    limits = resource_limits or {}
    if any(key not in {'lut', 'ff', 'bram18k', 'dsp48', 'carry8', 'uram288'}
           or isinstance(value, bool) or not isinstance(value, int)
           or not 0 <= value <= (1 << 63)-1 for key, value in limits.items()):
        raise ValidationError('RWRoute resource budget is invalid')
    io_digest = ''
    entries = ()
    if virtual_io_contract is not None:
        if (virtual_io_contract.get('schema') not in {
                'emuflow.virtual-io-physical-contract/v1',
                'emuflow.virtual-io-physical-contract/v2'}
                or virtual_io_contract.get('status') != 'pass'):
            raise ValidationError('RWRoute virtual I/O contract is invalid')
        entries = virtual_io_contract.get('entries', [])
        if not isinstance(entries, (tuple, list)) or len(entries) > 65536:
            raise ValidationError('RWRoute I/O population is invalid')
        io_digest = hashlib.sha256(json.dumps(
            virtual_io_contract, sort_keys=True, separators=(',', ':'),
        ).encode('utf-8')).hexdigest()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    executable = resolve_native_executable('emuflow-native')
    with tempfile.TemporaryDirectory(prefix='.emuflow-rwroute-', dir=output_path.parent) as temporary:
        root = Path(temporary)
        request, summary, operation = root/'request.bin', root/'summary.json', root/'operation.json'
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QI', 0x31515752554d5545, 1)
            for generation in generations:
                writer.put('3Q4q', *generation)
            for value in (top, part, digests['mapped_sha256'], digests['packed_sha256'],
                          digests['placement_sha256'], io_digest):
                writer.text(value)
            writer.put('I', len(limits))
            for field, count in sorted(limits.items()):
                writer.text(field)
                writer.put('Q', count)
            writer.put('I', len(entries))
            for entry in entries:
                try:
                    bit, width = entry['logical_bit'], entry['logical_port_width']
                    if (isinstance(bit, bool) or not isinstance(bit, int)
                            or isinstance(width, bool) or not isinstance(width, int)
                            or not 0 <= bit < width <= (1 << 63)-1):
                        raise ValueError('invalid logical bit/width')
                    for value in (entry['id'], entry['direction'], entry['p']['port'],
                                  entry['n']['port'], entry['p']['package_pin'],
                                  entry['n']['package_pin'], entry['p']['site'],
                                  entry['n']['site'], entry['logical_port'], entry['iostandard']):
                        writer.text(str(value))
                    writer.put('QQ', bit, width)
                except (KeyError, TypeError, ValueError) as error:
                    raise ValidationError('RWRoute virtual I/O entry is invalid') from error
            writer.finish()
        finally:
            writer.close()
        if request.stat().st_size > 16*1024*1024:
            raise ValidationError('RWRoute bounded export request exceeds limit')
        command = [executable, '--report', str(operation), 'phase7', 'export-rwroute',
                   *(str(path) for path in inputs), str(request), str(output_path), str(summary)]
        with NativeToolProcess(executable, command, environment=dict(os.environ),
                               timeout_ms=21600000, scratch_parent=root) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('native RWRoute export failed: '+completed.stdout)
        if tuple(native_file_generation(path) for path in inputs) != generations:
            raise ValidationError('RWRoute export source generation changed')
        if summary.stat().st_size > 32768 or operation.stat().st_size > 32768:
            raise ValidationError('native RWRoute export summary exceeds bound')
        result = json.loads(summary.read_text(encoding='utf-8'))
        common = json.loads(operation.read_text(encoding='utf-8'))
        fields = ('cells', 'logical_cells', 'virtual_io_cells', 'expanded_lut6_2_cells',
                  'transformed_dsp48e2_cells', 'qualified_clock_gates', 'physical_cells',
                  'routable_nets', 'excluded_nets')
        if (result.get('schema') != 'emuflow.rwroute-export-summary/v1'
                or result.get('status') != 'pass' or result.get('implementation_id') != 'native-rwroute-export-v1'
                or result.get('output') != str(output_path)
                or common.get('schema') != 'emuflow.native-stage-result/v1'
                or common.get('stage') != 'phase7.export-rwroute' or common.get('status') != 'pass'
                or any(isinstance(result.get(key), bool) or not isinstance(result.get(key), int)
                       or result[key] < 0 for key in fields)
                or result['virtual_io_cells'] != len(entries)
                or result['cells'] != result['logical_cells'] + len(entries) + result['expanded_lut6_2_cells']
                or result['physical_cells'] != result['cells'] + 7*result['transformed_dsp48e2_cells']):
            raise ValidationError('native RWRoute export result disagrees')
        result['native_metrics'] = common['metrics']
        return result
