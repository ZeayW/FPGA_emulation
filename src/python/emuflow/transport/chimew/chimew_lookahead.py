"""Bounded request host for native Xilinx Chimew input preparation."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256
from emuflow.common.native_tools import resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.transport.board_configuration import _record
from emuflow.transport.chimew.geometry import XilinxChimewGeometry
from emuflow.transport.chimew.signals import ChimewSignals

XILINX_CHIMEW_LOOKAHEAD_SCHEMA = 'emuflow.xilinx-chimew-lookahead/v1'
XILINX_CHIMEW_LOOKAHEAD_PROVIDER = 'indexed-xilinx-placement+fixed-lvds-lookahead-v1'
XILINX_CHIMEW_TIMING_WEIGHT_PROVIDER = 'partition-projected-sta-criticality-v1'


def prepare_chimew_lookahead(signals, output_dir: Path, *, grouper=None, refiner=None):
    """Generate all input records in the core, retaining source generation leases."""
    native = resolve_native_executable('emuflow-native')
    for label, executable in (('grouper', grouper), ('refiner', refiner)):
        if executable is not None:
            try:
                same = Path(shutil.which(str(executable)) or executable).samefile(native)
            except OSError:
                same = False
            if not same:
                raise ValidationError(f'Chimew {label} must use the unified native executable')
    output_dir = Path(output_dir)
    if output_dir.is_symlink():
        raise ValidationError("Chimew lookahead output directory is a symlink")
    output_dir = output_dir.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.chimew-lookahead-host-', dir=output_dir.parent) as temporary:
        root = Path(temporary)
        request, receipt = root / 'request.json', root / 'operation.json'
        payload = {'schema': 'emuflow.chimew-lookahead-input/v1',
                   'signals': {k: v for k, v in _record(signals.path,
                       'chimew-signal-selector-v1', 'signals').items() if k != 'role'}}
        request.write_text(json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False),
                           encoding='utf-8')
        with NativeToolProcess(native, [native, '--report', str(receipt), 'phase6',
                'prepare-chimew-lookahead', str(request), str(output_dir)],
                environment=dict(os.environ), scratch_parent=root) as process:
            result = process.finish()
        if not receipt.is_file() or receipt.stat().st_size > 1024 * 1024:
            raise ValidationError('native Chimew lookahead receipt is absent or exceeds bound')
        operation = json.loads(receipt.read_text())
        if result.returncode:
            raise ValidationError('native Chimew lookahead failed: ' +
                operation.get('error', {}).get('message', result.stdout))
        if (operation.get('schema') != 'emuflow.native-stage-result/v1'
                or operation.get('stage') != 'phase6.prepare-chimew-lookahead'
                or operation.get('status') != 'pass' or len(operation.get('outputs', [])) != 1):
            raise ValidationError('native Chimew lookahead producer disagrees')
        path = output_dir / 'chimew-lookahead-report.json'
        output = operation['outputs'][0]
        if (output.get('path') != str(path) or output.get('schema') != 'chimew-lookahead-report-v1'
                or path.stat().st_size > 262144):
            raise ValidationError('native Chimew lookahead report owner disagrees')
        from emuflow.common.native_tools import native_file_generation
        names = ('device', 'inode', 'bytes', 'mtime_seconds', 'mtime_nanoseconds',
                 'ctime_seconds', 'ctime_nanoseconds')
        for row in (*operation['inputs'][1:], output):
            if native_file_generation(Path(row['path'])) != tuple(row['generation'][key] for key in names):
                raise ValidationError('native Chimew lookahead source generation changed')
        report = json.loads(path.read_text())
        if (report.get('schema') != XILINX_CHIMEW_LOOKAHEAD_SCHEMA or report.get('status') != 'pass'
                or report.get('provider') != XILINX_CHIMEW_LOOKAHEAD_PROVIDER
                or report.get('implementation_id') != 'native-chimew-lookahead-preparation-v1'
                or set(report.get('artifacts', {})) != {'schedule', 'crossings', 'positions',
                    'rudy_input', 'bank_channel_input', 'bank_channel_input_store', 'electrical_map'}):
            raise ValidationError('native Chimew lookahead report identity disagrees')
        for label, record in report['artifacts'].items():
            artifact = Path(record['path'])
            if (label != 'schedule' and artifact.parent != output_dir / 'inputs'
                    or not artifact.is_file() or artifact.is_symlink()
                    or file_sha256(artifact) != record['sha256']):
                raise ValidationError('native Chimew lookahead artifact seal/owner disagrees')
        signals.require_unchanged()
        return report


def materialize_xilinx_chimew_inputs(*, architecture_path: Path, ir_path: Path,
        schedule_path: Path, routes_path: Path, platform_path: Path, physical_report,
        output_dir: Path, package_geometry=None, timing_paths_path=None,
        timing_weight_scale=9.0, timing_path_scope='exact-hop', region_count=4,
        grouper=None, refiner=None):
    output_dir = Path(output_dir)
    with XilinxChimewGeometry(ir_path, platform_path, physical_report, architecture_path,
            schedule_path=schedule_path, output_dir=output_dir / 'sources/geometry',
            package_geometry=package_geometry) as geometry:
        with ChimewSignals(geometry, schedule_path, routes_path, output_dir / 'sources/signals',
                timing=timing_paths_path, weight_scale=timing_weight_scale,
                path_scope=timing_path_scope, region_count=region_count) as signals:
            return prepare_chimew_lookahead(signals, output_dir / 'preparation',
                                           grouper=grouper, refiner=refiner)
