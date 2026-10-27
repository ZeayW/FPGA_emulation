"""Bounded API for the native baseline Phase 6 directory owner."""
import json
import os
from pathlib import Path
import tempfile
import sys

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.storage.store_publication import bind_store_source, require_store_sources_unchanged
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.transport.transport_native import transport_request


def prepare_compile_policy(directory, source, platform_path, *, clock_periods,
                           transport_cost_path=None, equivalence_cycles=1,
                           equivalence_seed=20260727):
    """Prepare only board/clock policy; no schedule, hop or endpoint records."""
    from emuflow.platform.platform import Platform
    from emuflow.transport.split_netlist_storage import read_compact_emuir_control_metadata
    from emuflow.transport.virtual_board import build_virtual_board
    from emuflow.common.io import read_json
    from emuflow.transport.transport_cost import validate_transport_cost_database
    require_native_schema('phase6-compile-policy-v1')
    source_ref=bind_store_source(source, 'emuir-compact-v2')
    board=bind_store_source(platform_path, 'boarddb-v1')
    sources=[source_ref,board]
    platform=Platform.load(board.path)
    if clock_periods is None:
        clocks=read_compact_emuir_control_metadata(source_ref.path)['clocks']
        if len(clocks)!=1:
            raise ValidationError('multi-clock Phase 6 requires explicit periods')
        clock_periods={clocks[0]['id']:1.0}
    cost=None;extra=[board]
    if transport_cost_path is not None:
        ref=bind_store_source(transport_cost_path,'transport-cost-input-v1')
        sources.append(ref);extra.append(ref);cost=read_json(ref.path)
        validate_transport_cost_database(cost,expected_platform=platform.name)
    oracle=bind_store_source(Path(__file__).with_name('phase6_oracle.py'),'phase6-oracle-source-v1')
    python=bind_store_source(Path(sys.executable).resolve(),'external-tool-executable-v1')
    sources.extend((oracle,python))
    def record(ref):
        return {'path':str(ref.path),'schema':ref.schema,'generation':list(ref.generation)}
    policy={'schema':'emuflow.phase6-compile-policy/v1','source':record(source_ref),
            'configuration_sources':[record(ref) for ref in extra],
            'clock_periods':dict(clock_periods),
            'virtual_board':build_virtual_board(platform) if platform.virtual_board_model is not None else None,
            'transport_cost':cost,
            'small_oracle':{'python':record(python),'source':record(oracle),
                            'cycles':equivalence_cycles,'seed':equivalence_seed}}
    encoded=json.dumps(policy,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False)
    if len(encoded.encode())>8*1024*1024:
        raise ValidationError('Phase 6 policy exceeds bound')
    require_store_sources_unchanged(sources)
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    path=directory/'phase6-policy.json';path.write_text(encoded+'\n')
    require_store_sources_unchanged(sources)
    return path


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
