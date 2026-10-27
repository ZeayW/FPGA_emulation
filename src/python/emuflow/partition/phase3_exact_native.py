"""Bounded source-bound validation of current Static Exact binary files."""
import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import require_native_schema, resolve_native_executable
from emuflow.storage.store_publication import bind_store_source, require_store_sources_unchanged
from emuflow.common.tool_runtime import NativeToolProcess


def _run(arguments, scratch):
    executable = resolve_native_executable('emuflow-native')
    with NativeToolProcess(executable, [executable, *arguments], environment=dict(os.environ),
            timeout_ms=21600000, scratch_parent=Path(scratch)) as process:
        result = process.finish()
    if result.returncode:
        raise ValidationError('native Static Exact binary operation failed: '+result.stdout)
    return result


def validate(path, descriptor):
    schema = descriptor.get('schema')
    versions = {'emuflow.phase3-static-exact-binary/v1': 1, 'emuflow.phase3-static-exact-binary/v2': 2}
    if schema not in versions:
        raise ValidationError('Static Exact binary descriptor schema is invalid')
    version = versions[schema]
    require_native_schema('exact-binary-validation-request-v1')
    try:
        source = bind_store_source(Path(path), 'phase3-exact-data-v'+str(version))
    except OSError as error:
        raise ValidationError('Static Exact binary artifact is missing') from error
    encoded = json.dumps(descriptor, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(encoded.encode()) > 8*1024*1024-1024:
        raise ValidationError('Static Exact binary descriptor exceeds bound')
    with tempfile.TemporaryDirectory(prefix='exact-binary-validation-') as temporary:
        root = Path(temporary);request = root/'request.bin';summary = root/'summary.json'
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QII', 0x31565145554D5545, 1, version)
            writer.put('3Q4q', *source.generation);writer.text(encoded);writer.finish()
        finally:
            writer.close()
        _run(['phase3', 'validate-exact-binary', str(source.path), str(request), str(summary)], root)
        require_store_sources_unchanged((source,))
        if summary.stat().st_size > 4096:
            raise ValidationError('Static Exact binary validation summary exceeds bound')
        result = json.loads(summary.read_text())
        required = {'status', 'schema', 'semantic_contract_sha256', 'cut_nodes', 'source_segments',
            'logic_segments', 'capture_requirements', 'dependency_edges'}
        if version == 2:required.add('capture_bit_max_bits')
        if (not isinstance(result, dict) or set(result) != required or result.get('status') != 'pass'
                or result.get('schema') != schema or result.get('semantic_contract_sha256') != descriptor.get('semantic_contract_sha256')
                or any(type(result[key]) is not int or result[key] < 0 for key in required-{'status', 'schema', 'semantic_contract_sha256'})
                or (version == 2 and result['capture_bit_max_bits'] != 8192)):
            raise ValidationError('Static Exact binary validation summary disagrees')
        return result
