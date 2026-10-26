"""Bounded configuration host; source catalogs and resource scans stay native."""
import json
import fnmatch
import os
from pathlib import Path
import tempfile
import re
from functools import lru_cache

from .errors import ValidationError
from .native_protocol import CheckedRequestWriter
from .native_tools import require_native_schema, resolve_native_executable
from .store_publication import (StoreSource, bind_store_source,
                               require_store_sources_unchanged)
from .tool_runtime import NativeToolProcess


class NativePartitionConstraints(dict):
    implementation_id = 'native-partition-configuration-v1'


@lru_cache(maxsize=1)
def _pattern_range_policy():
    try:
        matched = fnmatch.fnmatchcase('a', '[z-aa]')
    except re.error:
        return 'strict-ranges-v1'
    if matched:
        return 'empty-ranges-v1'
    raise ValidationError('unsupported instance pattern range convention')


def prepare_configuration(source_path, platform, value, *, min_used_fpgas=None,
                          balance_tolerance=None, constraints_path=None):
    require_native_schema('partition-configuration-request-v1')
    source = bind_store_source(Path(source_path).resolve(), 'emuir-compact-v2')
    configuration = {
        'pattern_range_policy': _pattern_range_policy(),
        'min_used_fpgas': min_used_fpgas,
        'balance_tolerance': balance_tolerance,
        'board': {
            'fpgas': [{'id': fpga.id, 'effective_capacity': fpga.effective_capacity}
                      for fpga in platform.fpgas],
            'links': [{'from': link.endpoints[0], 'to': link.endpoints[1],
                       'capacity': link.transport_bits_per_cycle_per_direction,
                       'latency': link.latency_cycles,
                       'bidirectional': link.direction in {'full_duplex', 'half_duplex'}}
                      for link in platform.links],
        },
    }
    try:
        payload = json.dumps(configuration, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValidationError('partition configuration is not finite JSON metadata') from error
    if len(payload.encode()) > 8 * 1024 * 1024:
        raise ValidationError('partition configuration exceeds bounded metadata contract')
    with tempfile.TemporaryDirectory(prefix='emuflow-partition-configuration-') as temporary:
        root = Path(temporary)
        request, output, operation = root/'request.bin', root/'constraints.json', root/'operation.json'
        raw_source = None
        if constraints_path is not None:
            if value is not None:
                raise ValidationError('partition configuration has two raw input owners')
            raw_source = bind_store_source(Path(constraints_path).resolve(), 'partition-constraints-input-v1')
        elif value is not None:
            raw = root/'raw.json'
            try:
                with raw.open('w', encoding='utf-8') as stream:
                    json.dump(value, stream, sort_keys=True, separators=(',', ':'), allow_nan=False)
            except (TypeError, ValueError, UnicodeError) as error:
                raise ValidationError('partition constraints are not finite UTF-8 JSON') from error
            raw_source = bind_store_source(raw, 'partition-constraints-input-v1')
        sources = (source,) if raw_source is None else (source, raw_source)
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QII', 0x31474350464D5545, 1, int(raw_source is not None))
            for item in sources:
                writer.put('3Q4q', *item.generation)
            writer.text(payload)
            writer.finish()
        finally:
            writer.close()
        executable = resolve_native_executable('emuflow-native')
        with NativeToolProcess(executable, [executable, '--report', str(operation),
                'phase3', 'normalize-configuration', str(source.path), str(request),
                *(() if raw_source is None else (str(raw_source.path),)), str(output)],
                environment=dict(os.environ), timeout_ms=21600000, scratch_parent=root) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('native partition configuration failed: ' + completed.stdout)
        require_store_sources_unchanged(sources)
        if operation.stat().st_size > 256 * 1024:
            raise ValidationError('native partition configuration result exceeds bound')
        proof = json.loads(operation.read_text())
        outputs = proof.get('outputs')
        if proof.get('stage') != 'phase3.normalize-configuration' or proof.get('status') != 'pass' or not isinstance(outputs, list) or len(outputs) != 1:
            raise ValidationError('native partition configuration producer result disagrees')
        record = outputs[0]
        fields = ('device', 'inode', 'bytes', 'mtime_seconds', 'mtime_nanoseconds', 'ctime_seconds', 'ctime_nanoseconds')
        generation = record.get('generation', {})
        if record.get('path') != str(output.resolve()) or record.get('schema') != 'partition-constraints-v1' or any(type(generation.get(key)) is not int for key in fields):
            raise ValidationError('native partition configuration original output binding disagrees')
        published = StoreSource(output.resolve(), 'partition-constraints-v1', tuple(generation[key] for key in fields))
        require_store_sources_unchanged((published, *sources))
        if output.stat().st_size > 8 * 1024 * 1024 + 1:
            raise ValidationError('native normalized configuration exceeds bound')
        result = json.loads(output.read_text())
        require_store_sources_unchanged((published, *sources))
        if not isinstance(result, dict) or result.get('schema') != 'emuflow.partition-constraints/v1':
            raise ValidationError('native normalized configuration schema disagrees')
        result = NativePartitionConstraints(result)
        result.native_metrics = completed.native_metrics
        result.pattern_range_policy = configuration['pattern_range_policy']
        return result
