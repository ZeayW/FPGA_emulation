"""Bounded host for complete native exact-file production and validation."""
import json
import os
from pathlib import Path
import tempfile

from .errors import ValidationError
from .native_protocol import CheckedRequestWriter
from .native_tools import require_native_schema, resolve_native_executable
from .store_publication import (StoreSource, bind_store_source,
    require_store_sources_unchanged, validate_store_destination)
from .tool_runtime import NativeToolProcess


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


def publish(path, header, parts, source, native_paths, digest, version):
    require_native_schema('exact-binary-publication-request-v1')
    nodes, captures = source.cut_nodes, source.captures
    sources = (
        StoreSource(Path(native_paths[0]).resolve(), 'exact-nodes-v2', nodes.native_semantic_generation),
        StoreSource(Path(native_paths[1]).resolve(), 'exact-captures-v1', captures.native_capture_generation),
    )
    sources = validate_store_destination(Path(path), sources)
    require_store_sources_unchanged(sources)
    document = json.dumps(header, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(document.encode()) > 8*1024*1024-1024:
        raise ValidationError('Static Exact binary publication header exceeds bound')
    with tempfile.TemporaryDirectory(prefix='.native-exact-file-', dir=Path(path).parent) as temporary:
        root = Path(temporary);request = root/'request.bin';selected = root/'selected';operation = root/'operation.json'
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QII', 0x31504245554D5545, 1, version)
            for item in sources:writer.put('3Q4q', *item.generation)
            writer.catalog(parts);writer.text(document);writer.text(digest);writer.finish()
        finally:
            writer.close()
        _run(['--report', str(operation), 'phase3', 'publish-exact-binary',
            *(str(item.path) for item in sources), str(request), str(selected)], root)
        require_store_sources_unchanged(sources)
        if operation.stat().st_size > 256*1024:
            raise ValidationError('Static Exact binary producer result exceeds bound')
        proof = json.loads(operation.read_text())
        if (proof.get('stage') != 'phase3.publish-exact-binary' or proof.get('status') != 'pass'
                or not isinstance(proof.get('outputs'), list) or len(proof['outputs']) != 2):
            raise ValidationError('Static Exact binary producer result disagrees')
        originals = []
        fields = ('device', 'inode', 'bytes', 'mtime_seconds', 'mtime_nanoseconds', 'ctime_seconds', 'ctime_nanoseconds')
        for record, name, schema in zip(proof['outputs'], ('assignment.exact.bin', 'descriptor.json'),
                ('phase3-exact-data-v'+str(version), 'exact-binary-descriptor-v1')):
            output = (selected/name).resolve();generation = record.get('generation', {})
            if (record.get('path') != str(output) or record.get('schema') != schema
                    or any(type(generation.get(key)) is not int for key in fields)):
                raise ValidationError('Static Exact binary original output binding disagrees')
            originals.append(StoreSource(output, schema, tuple(generation[key] for key in fields)))
        require_store_sources_unchanged(originals)
        if originals[1].path.stat().st_size > 64*1024:
            raise ValidationError('Static Exact binary descriptor exceeds bound')
        descriptor = json.loads(originals[1].path.read_text())
        expected_counts = {'cut_nodes': len(nodes), 'source_segments': source.launch_count+source.dependency_count,
            'logic_segments': source.launch_count+source.dependency_count+len(captures),
            'consumer_segments': source.dependency_count+len(captures), 'capture_requirements': len(captures)}
        if (descriptor.get('schema') != 'emuflow.phase3-static-exact-binary/v'+str(version)
                or descriptor.get('artifact') != 'assignment.exact.bin' or descriptor.get('bytes') != originals[0].generation[2]
                or descriptor.get('semantic_contract_schema') != header.get('schema')
                or descriptor.get('semantic_contract_sha256') != digest or descriptor.get('counts') != expected_counts
                or (version == 2 and descriptor.get('capture_bit_max_bits') != 8192)):
            raise ValidationError('Static Exact binary descriptor disagrees with its source')
        require_store_sources_unchanged((*sources, *originals))
        # This is a private native generation in the same filesystem; move its
        # completed file without copying the large sections through the host.
        os.replace(originals[0].path, path)
        return descriptor
