"""Bounded API for the native baseline Phase 6 directory owner."""
import json
import os
from pathlib import Path
import tempfile

from .errors import ValidationError
from .native_tools import native_file_generation, require_native_schema, resolve_native_executable
from .store_publication import bind_store_source, require_store_sources_unchanged
from .tool_runtime import NativeToolProcess
from .transport_native import transport_request


def run_native_phase6(source, assignment, schedule, platform, platform_path, output,
                      *, design, clocks, virtual_board, runtime, equivalence=None,
                      managed_storage=False, transport_cost=None, transport_cost_path=None,
                      _capture_operation=None):
    require_native_schema('phase6-stage-request-v1')
    configuration, owners = transport_request(assignment, schedule, platform,
        design=design, clocks=clocks, virtual_board=virtual_board, runtime=runtime, emit_rtl=True)
    extra=[bind_store_source(platform_path, 'boarddb-v1')]
    if transport_cost_path is not None:
        extra.append(bind_store_source(transport_cost_path, 'transport-cost-input-v1'))
    sources=(bind_store_source(source, 'emuir-compact-v2'),)+owners+tuple(extra)
    request={'schema':'emuflow.phase6-stage/v1', 'transport':configuration,
             'configuration_sources':[{'path':str(ref.path), 'schema':ref.schema,
                 'generation':list(ref.generation)} for ref in extra],
             'small_design_equivalence':equivalence, 'managed_storage':managed_storage,
             'transport_cost':transport_cost}
    encoded=json.dumps(request, ensure_ascii=True, sort_keys=True,
                       separators=(',', ':'), allow_nan=False)
    if len(encoded.encode())>8*1024*1024:
        raise ValidationError('Phase 6 configuration exceeds bound')
    output=Path(output).absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.native-phase6-', dir=output.parent) as temporary:
        root=Path(temporary); configuration_path=root/'request.json'; operation=root/'operation.json'
        configuration_path.write_text(encoded+'\n')
        require_store_sources_unchanged(sources)
        tool=resolve_native_executable('emuflow-native')
        argv=[tool, '--report', str(operation), 'phase6', 'run', str(Path(source).resolve()),
              str(Path(assignment).resolve()), str(schedule.store_path.resolve()),
              str(configuration_path), str(output)]
        with NativeToolProcess(tool, argv, environment=dict(os.environ), scratch_parent=root) as process:
            result=process.finish()
        if result.returncode:
            raise ValidationError('native Phase 6 failed: '+result.stdout)
        if operation.stat().st_size>256*1024:
            raise ValidationError('native Phase 6 result exceeds bound')
        proof=json.loads(operation.read_text())
        outputs=proof.get('outputs', [])
        if proof.get('stage')!='phase6.run' or proof.get('status')!='pass' or len(outputs)!=3:
            raise ValidationError('native Phase 6 producer proof disagrees')
        fields=('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
        for row,(name,schema) in zip(outputs, (('phase6_report.json','phase6-report-v1'),
                ('split-netlists.sqlite','split-netlists-v3'), ('transport.sqlite','transport-store-v2'))):
            path=output/name
            if (row.get('path')!=str(path.resolve()) or row.get('schema')!=schema or
                tuple(row.get('generation',{}).get(key) for key in fields)!=native_file_generation(path)):
                raise ValidationError('native Phase 6 output generation changed')
        require_store_sources_unchanged(sources)
        if (output/'phase6_report.json').stat().st_size>8*1024*1024:
            raise ValidationError('native Phase 6 report exceeds bound')
        report=json.loads((output/'phase6_report.json').read_text())
        if (report.get('schema')!='emuflow.phase6-report/v1' or report.get('status')!='pass'
                or report.get('stage_implementation_id')!='native-baseline-phase6-v1'):
            raise ValidationError('native Phase 6 report identity disagrees')
        if _capture_operation is not None:
            _capture_operation(proof)
        return report
