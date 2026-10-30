"""Bounded API for the complete native Phase 6 directory owner."""
import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.storage.store_publication import bind_store_source, require_store_sources_unchanged
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.transport.transport_native import transport_request


def prepare_configuration_request(directory, platform_path, *, clock_periods,
                           transport_cost_path=None, equivalence_cycles=1,
                           equivalence_seed=20260727):
    """Submit only source references and finite options to the native owner."""
    from emuflow.transport.board_configuration import prepare_phase6
    return prepare_phase6(directory, platform_path, clock_periods=clock_periods,
        transport_cost_path=transport_cost_path, equivalence_cycles=equivalence_cycles,
        equivalence_seed=equivalence_seed)


def prepare_compile_policy(directory, source, platform_path, **options):
    """Optional bounded host; the native producer binds the actual source IR."""
    directory=Path(directory).resolve();directory.mkdir(parents=True,exist_ok=False)
    try:
        with tempfile.TemporaryDirectory(prefix='.phase6-policy-host-',dir=directory) as temporary:
            root=Path(temporary)
            settings=prepare_configuration_request(root/'configuration',platform_path,**options)
            output,operation=directory/'phase6-policy.json',root/'operation.json'
            native=resolve_native_executable('emuflow-native')
            argv=[native,'--report',str(operation),'phase6','prepare-policy',str(Path(source).resolve()),str(settings),str(output)]
            with NativeToolProcess(native,argv,environment=dict(os.environ),scratch_parent=root) as process:
                result=process.finish()
            if result.returncode:
                raise ValidationError('native Phase 6 policy preparation failed: '+result.stdout)
            if operation.stat().st_size>256*1024:
                raise ValidationError('native Phase 6 policy operation exceeds bound')
            proof=json.loads(operation.read_text());outputs=proof.get('outputs',[])
            fields=('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
            if (proof.get('schema')!='emuflow.native-stage-result/v1' or proof.get('stage')!='phase6.prepare-policy'
                    or proof.get('status')!='pass' or len(outputs)!=1 or outputs[0].get('path')!=str(output)
                    or outputs[0].get('schema')!='phase6-compile-policy-v1'
                    or tuple(outputs[0].get('generation',{}).get(k) for k in fields)!=native_file_generation(output)):
                raise ValidationError('native Phase 6 policy producer binding disagrees')
            return output
    except BaseException:
        import shutil
        shutil.rmtree(directory)
        raise


def run_native_phase6(source, assignment, schedule, platform, platform_path, output,
                      *, design, clocks, virtual_board, runtime, equivalence=None,
                      managed_storage=False, transport_cost=None, transport_cost_path=None,
                      _capture_operation=None, pin_plan=None, position_hints=None, electrical_binding=None):
    pinned = pin_plan is not None
    require_native_schema('phase6-stage-request-v2' if pinned else 'phase6-stage-request-v1')
    configuration, owners = transport_request(assignment, schedule, platform,
        design=design, clocks=clocks, pin_plan=pin_plan, virtual_board=virtual_board, runtime=runtime, emit_rtl=True)
    extra=[bind_store_source(platform_path, 'boarddb-v1')]
    if transport_cost_path is not None:
        extra.append(bind_store_source(transport_cost_path, 'transport-cost-input-v1'))
    sources=(bind_store_source(source, 'emuir-compact-v2'),)+owners+tuple(extra)
    request={'schema':'emuflow.phase6-stage/v2' if pinned else 'emuflow.phase6-stage/v1', 'transport':configuration,
             'configuration_sources':[{'path':str(ref.path), 'schema':ref.schema,
                 'generation':list(ref.generation)} for ref in extra],
             'small_design_equivalence':equivalence, 'managed_storage':managed_storage,
             'transport_cost':transport_cost}
    if pinned:
        from emuflow.transport.pin_artifacts_native import _board_sources, _row
        rows = _board_sources(platform, Path(output).absolute().parent, platform_path)
        for role, value, schema in (("schedule",schedule,"physical-input-document-v1"),
                ("plan",pin_plan,"pin-plan-selector-v1"),
                ("positions",position_hints,"position-hints-selector-v1"),
                ("binding",electrical_binding,"chimew-binding-selector-v1")):
            path = getattr(value,"manifest_path",None)
            if path is None:
                raise ValidationError("native Phase 6 requires current typed pin artifacts")
            rows[role] = _row(path,schema)
        pin_sources = [bind_store_source(row["path"],row["schema"]) for row in rows.values()]
        request["pin_artifacts"] = {role:{**row,"generation":list(ref.generation)}
            for (role,row),ref in zip(rows.items(),pin_sources)}
        sources += tuple(pin_sources)
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
                or report.get('stage_implementation_id')!=('native-chimew-phase6-v1' if pinned else 'native-baseline-phase6-v1')):
            raise ValidationError('native Phase 6 report identity disagrees')
        if _capture_operation is not None:
            _capture_operation(proof)
        return report
