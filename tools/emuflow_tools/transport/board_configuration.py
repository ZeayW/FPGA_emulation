"""Bounded file-reference hosts for native wiring and Phase 6 configuration."""
from __future__ import annotations

from emuflow.common.paths import PACKAGE_ROOT
import json
import os
from pathlib import Path
import sys
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess

_FIELDS=('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')


def _wiring_file(board, root):
    reference=board['platform'].get('virtual_board_wiring')
    if reference is None:return None
    for path in (Path(root)/reference,Path(sys.prefix)/'share/emuflow/platforms'/reference):
        if path.is_file():return path.resolve()
    raise ValidationError('fixed virtual-board wiring table is missing: '+reference)


def _record(path, schema, role=None):
    path=Path(path).resolve()
    row={'path':str(path),'schema':schema,'generation':list(native_file_generation(path))}
    if role is not None:row['role']=role
    return row


def _run(command, source_rows, output, schema, stage, temporary):
    native=resolve_native_executable('emuflow-native')
    proof=temporary/'operation.json'
    with NativeToolProcess(native,[native,'--report',str(proof),*map(str,command)],
            environment=dict(os.environ),scratch_parent=temporary) as process:
        result=process.finish()
    if result.returncode:
        message=result.stdout
        if proof.is_file() and proof.stat().st_size<=262144:
            try:message=json.loads(proof.read_text())['error']['message']
            except (KeyError,ValueError,TypeError):pass
        raise ValidationError('native board/transport configuration failed: '+message)
    if proof.stat().st_size>262144 or output.stat().st_size>8*1024*1024:
        raise ValidationError('native board/transport configuration exceeds bound')
    operation=json.loads(proof.read_text())
    if (operation.get('schema')!='emuflow.native-stage-result/v1' or operation.get('stage')!=stage
            or operation.get('status')!='pass' or len(operation.get('outputs',[]))!=1):
        raise ValidationError('native board/transport configuration producer disagrees')
    published=_record(output,schema)
    row=operation['outputs'][0]
    if (row.get('path')!=published['path'] or row.get('schema')!=schema
            or [row.get('generation',{}).get(k) for k in _FIELDS]!=published['generation']):
        raise ValidationError('native board/transport output receipt disagrees')
    actual=operation.get('inputs',[])
    if len(actual)!=len(source_rows):raise ValidationError('native board/transport source coverage disagrees')
    for row,expected in zip(actual,source_rows):
        if (row.get('path')!=expected['path'] or row.get('schema')!=expected['schema']
                or [row.get('generation',{}).get(k) for k in _FIELDS]!=expected['generation']):
            raise ValidationError('native board/transport input receipt disagrees')
    for expected in [*source_rows,published]:
        if list(native_file_generation(expected['path']))!=expected['generation']:
            raise ValidationError('native board/transport source or output generation changed')
    return published


def build_wiring(board, *, inventory, wiring_root):
    with tempfile.TemporaryDirectory(prefix='.board-wiring-') as temporary:
        root=Path(temporary).resolve();source=root/'board.json'
        text=json.dumps(board,sort_keys=True,separators=(',',':'),allow_nan=False)
        if len(text.encode())>8*1024*1024:raise ValidationError('board configuration exceeds bound')
        source.write_text(text,encoding='utf-8')
        wiring=_wiring_file(board,wiring_root)
        rows=[_record(source,'boarddb-v1'),_record(inventory,'hp-package-pin-inventory-v1')]
        if wiring is not None:rows.append(_record(wiring,'fixed-wiring-csv-v1'))
        output=root/'wiring.json'
        published=_run(['board','build-wiring',source,Path(inventory).resolve(),wiring or '-',output],rows,
            output,'virtual-board-wiring-v1','board.build-wiring',root)
        value=json.loads(output.read_text(encoding='utf-8'))
        if value.get('schema')!='emuflow.virtual-board-wiring/v1':raise ValidationError('native fixed-wiring version disagrees')
        if list(native_file_generation(output))!=published['generation']:raise ValidationError('native fixed-wiring output changed')
        return value


def prepare_phase6(directory, platform_path, *, clock_periods, transport_cost_path=None,
                   equivalence_cycles=1, equivalence_seed=20260727):
    from emuflow.platform.platform import Platform
    from emuflow_tools.transport.virtual_board import INVENTORY_PATH, FIXED_WIRING_ROOT
    directory=Path(directory).absolute()
    board=_record(platform_path,'boarddb-v1','board')
    platform=Platform.load(board['path']).to_dict()
    rows=[board]
    if platform['platform'].get('virtual_board_model') is not None:
        rows.append(_record(INVENTORY_PATH,'hp-package-pin-inventory-v1','inventory'))
        wiring=_wiring_file(platform,FIXED_WIRING_ROOT)
        if wiring is not None:rows.append(_record(wiring,'fixed-wiring-csv-v1','wiring'))
    if transport_cost_path is not None:rows.append(_record(transport_cost_path,'transport-cost-input-v1','cost'))
    rows.extend((_record(sys.executable,'external-tool-executable-v1','python'),
                 _record(PACKAGE_ROOT / 'transport/phase6_oracle.py','phase6-oracle-source-v1','oracle')))
    request={'schema':'emuflow.phase6-configuration-input/v1','sources':rows,
             'options':{'clock_periods':clock_periods,'cycles':equivalence_cycles,'seed':equivalence_seed}}
    encoded=json.dumps(request,sort_keys=True,separators=(',',':'),allow_nan=False)
    if len(encoded.encode())>8*1024*1024:raise ValidationError('Phase 6 configuration input exceeds bound')
    directory.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.phase6-configuration-host-',dir=directory.parent) as temporary:
        root=Path(temporary);source=root/'request.json';source.write_text(encoded,encoding='utf-8')
        inputs=[_record(source,'phase6-configuration-input-v1'),*rows]
        output=directory/'request.json'
        _run(['phase6','prepare-configuration',source,directory],inputs,output,
             'phase6-configuration-v1','phase6.prepare-configuration',root)
        return output
