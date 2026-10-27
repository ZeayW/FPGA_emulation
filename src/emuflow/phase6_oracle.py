"""Independent, size-limited behavioral qualification for native Phase 6."""
import json
from pathlib import Path
import sys


def qualify(request_path, output):
    from emuflow.errors import ValidationError
    from emuflow.ir import EmuIR
    from emuflow.emuir_compact import read_compact_emuir
    from emuflow.io import read_json,write_json
    from emuflow.phase6 import _static_exact_equivalence_evidence
    from emuflow.equivalence import simulate_partition_equivalence
    from emuflow.split_netlist_storage import read_compact_emuir_control_metadata
    from emuflow.tdm import is_sampled_virtual_wire_schedule
    from emuflow.tdm_schedule_storage import read_tdm_schedule
    from emuflow.store_publication import require_store_sources_unchanged
    request_path=Path(request_path)
    if request_path.stat().st_size>1024*1024:
        raise ValidationError('small oracle request exceeds bound')
    request=json.loads(request_path.read_text())
    if request.get('schema')!='emuflow.phase6-small-oracle/v1':
        raise ValidationError('unsupported small oracle request')
    from emuflow.store_publication import StoreSource
    refs=tuple(StoreSource(Path(row['path']),row['schema'],tuple(row['generation']))
               for row in request['sources'])
    require_store_sources_unchanged(refs)
    source=read_compact_emuir_control_metadata(request['ir'])
    counts=source['record_counts']
    if counts['cell_records']>10000 or counts['endpoint_records']>100000:
        raise ValidationError('behavioral oracle is limited to independent small-design qualification')
    schedule=read_tdm_schedule(Path(request['schedule']),materialize=True)
    ir=EmuIR(read_compact_emuir(Path(request['ir'])));assignment=read_json(Path(request['assignment']))
    if is_sampled_virtual_wire_schedule(schedule):
        result=_static_exact_equivalence_evidence(ir,assignment,schedule,
            cycles=request['cycles'],seed=request['seed'],clock_periods=request['clock_periods'])
    else:
        result=simulate_partition_equivalence(ir,assignment,schedule,
            cycles=request['cycles'],seed=request['seed'])
    require_store_sources_unchanged(refs)
    write_json(Path(output),result,compact=True)


if __name__=='__main__':
    qualify(sys.argv[1],sys.argv[2])
