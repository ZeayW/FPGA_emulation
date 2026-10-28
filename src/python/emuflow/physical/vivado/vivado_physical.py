"""Bounded host for the complete native Vivado partition stage."""
from __future__ import annotations

from emuflow.common.paths import REPOSITORY_ROOT
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Mapping

from emuflow.common.errors import EmuFlowError, ValidationError
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess


def _vivado_script(name: str) -> Path:
    for directory in (REPOSITORY_ROOT/'scripts/vivado', Path(sys.prefix)/'share/emuflow/scripts/vivado'):
        path = directory/name
        if path.is_file():
            return path
    raise EmuFlowError(f'installed Vivado Tcl script is missing: {name}')


def run_indexed_vivado_partition(
    *, fpga: str, part: str, ir_path: Path, mapped_verilog_path: Path,
    mapped_report: Mapping[str, Any], runtime: Mapping[str, Any],
    original_cells: int, transport_cells: int, output_dir: Path,
    boundary_identity_path: Path | None = None, logic_identity_path: Path | None = None,
    logic_query_path: Path | None = None, local_identity_path: Path | None = None,
    local_query_path: Path | None = None, virtual_io_contract: Mapping[str, Any] | None = None,
    executable: str | None = None, place_directive: str = 'Default', route_directive: str = 'Default',
) -> dict[str, Any]:
    from emuflow.ir.emuir_storage import is_emuir_sqlite
    from emuflow.ir.emuir_compact import is_compact_emuir
    if not is_emuir_sqlite(ir_path) or not is_compact_emuir(ir_path):
        raise ValidationError('Vivado physical implementation requires current indexed EmuIR')
    candidate = shutil.which(executable or 'vivado')
    if candidate is None:
        raise EmuFlowError('Vivado executable was not found; pass --physical-vivado')
    for schema in ('vivado-partition-request-v1', 'vivado-partition-report-v1'):
        require_native_schema(schema)
    output_dir = Path(output_dir).absolute()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    def source(path):
        path = Path(path).resolve()
        return {'path': str(path), 'generation': native_file_generation(path)}
    sources = {'ir':source(ir_path), 'rtl':source(mapped_verilog_path),
        'mapped_index':source(mapped_report['mapped_index']['path']),
        'functions':source(mapped_report['source_functions']['path']), 'tool':source(candidate)}
    for name, path in (('boundary_identity',boundary_identity_path), ('logic_identity',logic_identity_path),
                       ('logic_query',logic_query_path), ('local_identity',local_identity_path), ('local_query',local_query_path)):
        if path is not None:
            sources[name] = source(path)
    if virtual_io_contract is not None:
        sources.update(io_adapter=source(virtual_io_contract['adapter']['path']),
                       io_xdc=source(virtual_io_contract['xdc']['path']))
    for script in sorted(_vivado_script('implement_indexed_partition.tcl').parent.glob('*.tcl')):
        sources['script.'+script.name] = source(script)
    config = {'schema':'emuflow.vivado-partition-request/v1', 'sources':sources,
        'fpga':fpga, 'part':part, 'runtime':runtime, 'virtual_io_contract':virtual_io_contract,
        'mapped_report':{'optimization_policy':mapped_report['optimization_policy'],
            'mapped_sha256':mapped_report['mapped_index']['sha256'],
            'rtl_sha256':mapped_report['output_sha256'],
            'functions_sha256':mapped_report['source_functions']['sha256'],
            'emitted_instances':mapped_report['emitted_instances'],
            'original_cells':original_cells,'transport_cells':transport_cells},
        'place_directive':place_directive,'route_directive':route_directive,
        'threads':int(os.environ.get('EMUFLOW_VIVADO_THREADS','2'))}
    # Default ASCII escaping retains the existing canonical fixed-I/O seal.
    payload = json.dumps(config, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(payload.encode()) > 8*1024*1024:
        raise ValidationError('Vivado partition control document exceeds bound')
    with tempfile.TemporaryDirectory(prefix='.vivado-partition-host-', dir=output_dir.parent) as temporary:
        root = Path(temporary)
        request, proof = root/'request.json', root/'operation.json'
        request.write_text(payload, encoding='utf-8')
        native = resolve_native_executable('emuflow-native')
        with NativeToolProcess(native, [native,'--report',str(proof),'physical',
            'run-vivado-partition',str(request),str(output_dir)], environment=dict(os.environ), scratch_parent=root, termination_grace_ms=5000) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('native Vivado partition failed: '+completed.stdout)
        operation = json.loads(proof.read_text())
        rows = operation.get('outputs')
        report_path = output_dir/'vivado-partition-report.json'
        if (operation.get('stage') != 'physical.run-vivado-partition' or operation.get('status') != 'pass'
                or not isinstance(rows,list) or len(rows) != 1 or rows[0].get('path') != str(report_path.resolve())
                or rows[0].get('schema') != 'vivado-partition-report-v1'):
            raise ValidationError('native Vivado partition proof disagrees')
        stamped = tuple(rows[0]['generation'].get(key) for key in ('device','inode','bytes','mtime_seconds',
            'mtime_nanoseconds','ctime_seconds','ctime_nanoseconds'))
        if stamped != native_file_generation(report_path) or report_path.stat().st_size > 8*1024*1024:
            raise ValidationError('native Vivado partition report generation changed')
        report = json.loads(report_path.read_text())
        if (report.get('status') != 'pass' or report.get('implementation_id') != 'native-vivado-partition-v1'
                or report.get('result',{}).get('identity') != {'backend':'vivado','fpga':fpga,'part':part}):
            raise ValidationError('native Vivado partition report identity disagrees')
        return report
