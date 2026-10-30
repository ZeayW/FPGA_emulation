"""Bounded host for native raw-placement import and physical handoff."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.common.control_document import read_small_object


def import_placement(
    placement_path, name_map_path, mapped_path, architecture_path, packed_path, report_path, *,
    native_constraints_path=None, provider_manifest_path=None, constraints_path=None,
    metadata=None, legalize_ff=True, top=None,
):
    require_native_schema('openparf-placement-import-request-v1')
    report_path = Path(report_path).absolute()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    sources=[]
    for role,path,schema in (
        ('placement',placement_path,'physical-input-document-v1'),
        ('name_selector',name_map_path,'openparf-name-map-selector-v1'),
        ('mapped',mapped_path,'xilinx-mapped-index-v2'),
        ('architecture',architecture_path,'physical-input-document-v1'),
        ('packed',packed_path,'openparf-atomic-source-selector-v1'),
        ('native',native_constraints_path,'physical-input-document-v1'),
        ('provider',provider_manifest_path,'physical-input-document-v1'),
        ('constraints',constraints_path,'physical-input-document-v1'),
    ):
        if path is not None:
            path=Path(path).resolve()
            sources.append({'role':role,'path':str(path),'schema':schema,'generation':native_file_generation(path)})
    request_value={'schema':'emuflow.openparf-placement-import-request/v1', 'sources':sources,
        'metadata':metadata or {}, 'legalize_ff':legalize_ff, 'top':top or ''}
    text=json.dumps(request_value,sort_keys=True,separators=(',', ':'),allow_nan=False)
    if len(text.encode())>8*1024*1024:
        raise ValidationError('placement import control document exceeds bound')
    with tempfile.TemporaryDirectory(prefix='.placement-import-host-',dir=report_path.parent) as temporary:
        root=Path(temporary);request,operation=root/'request.json',root/'operation.json'
        request.write_text(text,encoding='utf-8')
        executable=resolve_native_executable('emuflow-native')
        command=[executable,'--report',str(operation),'physical','import-openparf-placement',str(request),str(report_path)]
        with NativeToolProcess(executable,command,environment=dict(os.environ),scratch_parent=root,
            termination_grace_ms=5000) as process:
            completed=process.finish()
        if completed.returncode:
            raise ValidationError('native OpenPARF placement import failed: '+completed.stdout)
        proof=read_small_object(operation,262144)
        rows=proof.get('outputs',[])
        fields=('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
        if (proof.get('stage')!='physical.import-openparf-placement' or proof.get('status')!='pass' or len(rows)!=5
                or rows[0].get('path')!=str(report_path.resolve()) or rows[0].get('schema')!='openparf-placement-import-report-v1'
                or tuple(rows[0].get('generation',{}).get(key) for key in fields)!=native_file_generation(report_path)):
            raise ValidationError('native placement import proof disagrees')
        result=read_small_object(report_path)
        if (result.get('schema')!='emuflow.openparf-placement-import-report/v1' or result.get('status')!='pass'
                or result.get('implementation_id')!='native-openparf-placement-import-v1'
                or result.get('bridge',{}).get('status')!='pass'
                or result['bridge'].get('implementation_id')!='native-physical-handoff-bridge-v1'):
            raise ValidationError('native placement import result disagrees')
        expected=((result['certificate_path'],'openparf-placement-selector-v2'),
            (str(Path(result['certificate_path']).with_name(result['certificate']['cluster_storage']['file'])),
             'openparf-placement-certificate-v3'),
            (result['bridge']['physical_handoff'],'xilinx-physical-handoff-v1'),
            (str(Path(result['bridge']['physical_handoff']).with_name('physical-bridge-report.json')),
             'physical-handoff-bridge-report-v1'))
        for row,(path,schema) in zip(rows[1:],expected):
            if (row.get('path')!=str(Path(path).resolve()) or row.get('schema')!=schema
                    or tuple(row.get('generation',{}).get(key) for key in fields)!=native_file_generation(path)):
                raise ValidationError('native placement output generation changed')
        if result.get('handoff_generation')!=list(native_file_generation(result['bridge']['physical_handoff'])):
            raise ValidationError('native physical handoff generation changed')
        result['native_metrics']=proof['metrics']
        return result
