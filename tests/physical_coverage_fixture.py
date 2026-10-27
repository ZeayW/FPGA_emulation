"""Explicit bounded factual import for native physical-coverage tests."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import weakref

from emuflow.errors import ValidationError
from emuflow.physical_coverage import build_physical_coverage, audit_physical_coverage


def _identity(contract,schedule,physical):
    return json.dumps([contract,schedule,physical],sort_keys=True,ensure_ascii=False,separators=(',',':'))


def build_cross_layer_physical_binding(contract,schedule,physical,platform,assignment_index_path=None,*,output_path=None):
    from tests.schedule_store_fixture import write_tdm_schedule_storage
    from emuflow.tdm_schedule_storage import read_tdm_schedule, TDMScheduleView
    from emuflow.boundary_identity_storage import write_boundary_identity_storage
    from tests.timing_facts import boundary_timing_facts
    from tests.timing_facts import logic_timing_facts
    if isinstance(schedule,TDMScheduleView):
        return build_physical_coverage(contract,schedule,physical,platform,assignment_index_path,output_path=output_path,fixture=True)
    if len(schedule.get('entries',[]))>4096 or sum(len(db.get(section,[])) for key,section in (
        ('boundary_identities','endpoints'),('boundary_timing','endpoints'),('logic_segment_timing','segments'))
        for db in physical.get(key,{}).values())>4096:
        raise ValidationError('explicit coverage fixture exceeds bound')
    temporary=tempfile.TemporaryDirectory(prefix='coverage-input-fixture-');root=Path(temporary.name);stack=ExitStack()
    try:
        fixture_schedule=deepcopy(schedule)
        for section in ('routes','demand_completions','domain_schedules'):fixture_schedule.setdefault(section,[])
        write_tdm_schedule_storage(root/'schedule.json',fixture_schedule)
        indexed_schedule=read_tdm_schedule(root/'schedule.json')
        fixture_contract=deepcopy(contract)
        fixture_contract['schedule_binding']['sha256']=indexed_schedule.manifest['logical_pretty_sha256']
        entries={row['id']:row for row in schedule['entries']}
        indexed={key:value for key,value in physical.items() if key not in ('boundary_identities','boundary_timing','logic_segment_timing')}
        for key,section,writer in (
            ('boundary_identities','endpoints',write_boundary_identity_storage),
            ('boundary_timing','endpoints',boundary_timing_facts),
            ('logic_segment_timing','segments',logic_timing_facts),
        ):
            indexed[key]={}
            for fpga,database in physical.get(key,{}).items():
                rows=deepcopy(list(database[section]))
                header={k:v for k,v in database.items() if k not in (section,'unsupported_member_paths','unmeasured_segments')}
                if key=='boundary_timing':
                    header.setdefault('measurement_scope','partition-boundary')
                if key=='boundary_identities':
                    for row in rows:
                        entry=entries[row['schedule_entry']];kind=row['kind']
                        for field,value in dict(demand=entry['demand'],logical_net=entry['net'],link=entry['link'],
                            peer=entry['to'] if kind=='tx' else entry['from'],lane=entry['lane'],logical_lane=entry['lane'],
                            internal_signal='net:'+entry['net'],source_class='dut-net' if kind=='tx' else 'captured-shadow').items():row.setdefault(field,value)
                        row.setdefault('merged_ir',{'external_port':kind,'external_port_bit':row['lane'],
                            'external_net':row['id'],'logical_net':entry['net'],'boundary_register_instances':[] if kind=='tx' else ['rx']})
                if key=='logic_segment_timing':
                    header.setdefault('system_routes_sha256','a'*64)
                    header.setdefault('tdm_schedule_sha256',indexed_schedule.manifest['logical_pretty_sha256'])
                    rows.sort(key=lambda row:(row['system_path'],row['member_path'],row['cut_index'],row['id']))
                    view=writer(root/f'{key}-{fpga}.json',header=header,segments=rows,
                        unsupported_member_paths=database.get('unsupported_member_paths',()),
                        unmeasured_segments=database.get('unmeasured_segments',()))
                else:view=writer(root/f'{key}-{fpga}.json',header,rows)
                indexed[key][fpga]=view;stack.callback(view.close)
        binding=build_physical_coverage(fixture_contract,indexed_schedule,indexed,platform,assignment_index_path,
                                       output_path=output_path,fixture=True)
        binding._fixture_inputs=(fixture_contract,indexed_schedule,indexed,_identity(contract,schedule,physical))
        binding._fixture_finalizer=weakref.finalize(binding,lambda:(stack.close(),temporary.cleanup()))
        return binding
    except BaseException:
        stack.close();temporary.cleanup();raise


def validate_cross_layer_physical_binding(contract,schedule,physical,platform,binding,assignment_index_path=None):
    from emuflow.physical_coverage import PhysicalCoverageView
    if not isinstance(binding,PhysicalCoverageView):
        raise ValidationError('cross-layer physical binding does not match canonical inputs')
    try:stored_contract,stored_schedule,stored_physical,identity=binding._fixture_inputs
    except AttributeError:
        return audit_physical_coverage(contract,schedule,physical,platform,binding,assignment_index_path,fixture=True)
    if _identity(contract,schedule,physical)!=identity:
        raise ValidationError('cross-layer physical binding does not match canonical inputs')
    return audit_physical_coverage(stored_contract,stored_schedule,stored_physical,platform,binding,
                                  assignment_index_path,fixture=True)
