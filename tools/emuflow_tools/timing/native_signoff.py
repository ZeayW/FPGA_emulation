"""Bounded host for the shared physical Phase 7C transaction."""
from pathlib import Path
import os
import tempfile

from emuflow.common.errors import ValidationError
from emuflow_tools.document_io import read_json
from emuflow.common.native_tools import resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess


def run_native_physical_signoff(flow_root, split_root, physical_root, runtime_root, *,
                                executable=None, launcher_reference=None):
    root=Path(flow_root).resolve()
    split,physical,runtime=(Path(path).absolute() for path in (split_root,physical_root,runtime_root))
    runner=resolve_native_executable('emuflow-native')
    engine=resolve_native_executable('sta',executable)
    if launcher_reference is None:
        from emuflow.timing.engine_runtime import _runtime_data_path
        launcher_reference=_runtime_data_path(Path('scripts/opensta/container_runtime.sh'))
    launcher=str(Path(launcher_reference).absolute()) if Path(launcher_reference).is_file() else '-'
    with tempfile.TemporaryDirectory(prefix='.physical-signoff-driver-',dir=root) as scratch:
        command=[runner,'phase7c','run',str(root),engine,launcher,str(split),str(physical),str(runtime)]
        with NativeToolProcess(runner,command,environment=dict(os.environ),scratch_parent=Path(scratch)) as process:
            result=process.finish()
        if result.returncode:
            raise ValidationError('native physical Phase 7C failed: '+result.stdout)
    report_path=runtime/'phase7c_report.json'
    if report_path.is_symlink() or report_path.stat().st_size>256*1024:
        raise ValidationError('native physical Phase 7C report is invalid')
    report=read_json(report_path)
    if (report.get('schema')!='emuflow.phase7c-report/v3'
            or report.get('stage_implementation_id')!='native-physical-phase7c-v1'
            or report.get('status') not in {'pass','fail','incomplete'}):
        raise ValidationError('native physical Phase 7C report identity disagrees')
    return report
