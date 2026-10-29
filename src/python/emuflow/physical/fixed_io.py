"""Bounded source locators for the native whole-board fixed-I/O transaction."""
import json
import os
from pathlib import Path
import tempfile
from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.physical.handoff import _small
from emuflow.transport.transport_storage import read_virtual_binding


def prepare_fixed_io(binding_path, lane_path, wiring_path, schedule, xdc, adapters, output_dir):
    require_native_schema('physical-io-stage-request-v1')
    binding = read_virtual_binding(Path(binding_path))
    if binding.store_schema != 'transport-store-v2' or getattr(schedule, 'store_path', None) is None:
        raise ValidationError('fixed I/O preparation requires current typed transport and schedule owners')
    sources = []
    rows = [('transport', binding.store_path, 'transport-store-v2'),
            ('binding', binding.manifest_path, 'physical-input-document-v1'),
            ('lane_map', lane_path, 'physical-input-document-v1'),
            ('wiring', wiring_path, 'physical-input-document-v1'),
            ('schedule', schedule.store_path, 'tdm-schedule-store-v4')]
    rows += [('xdc.' + fpga, path, 'exact-sha256-text-v1') for fpga, path in sorted(xdc.items())]
    rows += [('adapter.' + fpga, path, 'exact-sha256-text-v1') for fpga, path in sorted(adapters.items())]
    for role, path, schema in rows:
        path = Path(path).resolve()
        sources.append({'role': role, 'path': str(path), 'schema': schema,
                        'generation': list(native_file_generation(path))})
    output = Path(output_dir).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.fixed-io-host-', dir=output.parent) as temporary:
        root = Path(temporary)
        request, operation = root/'request.json', root/'operation.json'
        request.write_text(json.dumps({'schema': 'emuflow.physical-io-stage/v1', 'sources': sources},
                                     sort_keys=True, separators=(',', ':')), encoding='utf-8')
        native = resolve_native_executable('emuflow-native')
        with NativeToolProcess(native, [native, '--report', str(operation), 'physical', 'prepare-fixed-io',
                str(request), str(output)], environment=dict(os.environ), scratch_parent=root) as process:
            result = process.finish()
        if result.returncode:
            raise ValidationError('native physical I/O preparation failed: ' + result.stdout)
        proof = _small(operation, 262144)
        report = _small(output/'physical-io-report.json')
        contracts = report.get('contracts', {})
        expected = [(output/'physical-io-report.json', 'physical-io-stage-report-v1')]
        expected += [(output/fpga/'virtual-io-physical-contract.json', 'physical-input-document-v1') for fpga in sorted(xdc)]
        if (proof.get('status') != 'pass' or proof.get('stage') != 'physical.prepare-fixed-io'
                or len(proof.get('outputs', [])) != len(expected)
                or report.get('schema') != 'emuflow.physical-io-stage-report/v1'
                or report.get('implementation_id') != 'native-physical-io-stage-v1'
                or report.get('status') != 'pass' or set(contracts) != set(xdc)
                or any(contracts[fpga] != str(output/fpga/'virtual-io-physical-contract.json') for fpga in xdc)):
            raise ValidationError('native physical I/O publication proof disagrees')
        fields = ('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
        def unchanged():
            for row, (path, schema) in zip(proof['outputs'], expected):
                if (row.get('path') != str(path.resolve()) or row.get('schema') != schema
                        or tuple(row.get('generation', {}).get(key) for key in fields) != native_file_generation(path)):
                    raise ValidationError('native physical I/O output generation changed')
            for row in sources:
                if row['generation'] != list(native_file_generation(row['path'])):
                    raise ValidationError('native physical I/O source generation changed')
            binding.bound_store_sources()
        unchanged()
        documents = {fpga: _small(Path(path)) for fpga, path in contracts.items()}
        for fpga, value in documents.items():
            if (value.get('schema') != 'emuflow.virtual-io-physical-contract/v2'
                    or value.get('status') != 'pass' or value.get('fpga') != fpga):
                raise ValidationError('native physical I/O contract identity disagrees')
        unchanged()
        return documents
