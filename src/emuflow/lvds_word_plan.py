"""Bind periodic word buses to whole native banks using bounded topology facts.

The endpoint owner supplies distinct physical lane occupancy through SQLite.
This plan owns the logical bus-to-bank binding only; package pairs and cable
wires remain in the referenced wiring owner. It is not physical acceptance.
"""
from __future__ import annotations

from .errors import ValidationError
from .netlist import transport_endpoint_port
from .tdm_schedule_storage import canonical_tdm_schedule_sha256
from .tdm_word import WORD_EVENT_POLICY
from .lvds_selectio import SELECTIO_IP_DEFINITION,WORD_PHY_RX_ALIGNMENT
from .virtual_board import WORD_MODEL,WORD_WIRING_SCHEMA,_digest,_artifact_digest,validate_virtual_board

WORD_PHY_PLAN_SCHEMA='emuflow.lvds-word-phy-plan/v2'


def _physical_groups(transport):
    native=getattr(transport,'iter_physical_word_groups',None)
    if callable(native):yield from native();return
    # Explicit materialized fixture/API compatibility only; production uses
    # the indexed reducer above and never scans endpoint JSON here.
    groups=set()
    for e in transport['endpoints']:
        lane=e['lane']
        if isinstance(lane,bool)or not isinstance(lane,int)or lane<0:
            raise ValidationError('word-plan physical lane is invalid')
        groups.add((e['link'],e['peer'],e['kind'],lane//8))
    yield from sorted(groups)


def build_word_phy_plan(platform,board,schedule,transports):
    """Consume producer-validated wiring and the canonical endpoint owner."""
    if (platform.virtual_board_model!=WORD_MODEL or board.get('schema')!=WORD_WIRING_SCHEMA
        or board.get('model')!=WORD_MODEL or board.get('platform')!=platform.name
        or schedule.get('platform')!=platform.name or schedule.get('word_event_policy')!=WORD_EVENT_POLICY
        or set(transports)!={f.id for f in platform.fpgas}):
        raise ValidationError('word PHY plan source identities or policy differ')
    links={link.id:link for link in platform.links}
    allocation={}
    for record in schedule.get('bank_allocations',[]):
        key=(record['link'],record['from'],record['to'])
        if key in allocation:raise ValidationError('word PHY direction allocation repeats')
        link=links.get(key[0]);count,offset,lanes=(record.get(k)for k in ('bank_count','lane_offset','lanes'))
        if (link is None or set(key[1:])!=set(link.endpoints)or record.get('wires_per_bank')!=20
            or any(isinstance(n,bool)or not isinstance(n,int)or n<0 for n in (count,offset,lanes))
            or offset%160 or lanes!=count*160 or offset+lanes>link.transport_bits_per_cycle_per_direction):
            raise ValidationError('word PHY whole-bank direction allocation is invalid')
        allocation[key]=(offset//8,(offset+lanes)//8)
    for link in platform.links:
        a,b=sorted(link.endpoints)
        if (link.word_timing is None or link.wires_per_bank!=20
            or (link.id,a,b)not in allocation or (link.id,b,a)not in allocation
            or allocation[link.id,a,b][0]!=0
            or allocation[link.id,a,b][1]!=allocation[link.id,b,a][0]
            or allocation[link.id,b,a][1]!=link.data_lanes_per_direction):
            raise ValidationError('word PHY bank allocations must partition the shared physical pool')
    board_banks={}
    for item in board['fpgas']:
        for bank in item['bank_assignments']:
            key=(item['id'],bank['link'],bank['bank_group'])
            if key in board_banks:raise ValidationError('word board bank identity repeats')
            board_banks[key]=bank['bank']
    clocks={(c['link'],c['bank_group']):c['id']for c in board['forwarded_clocks']}
    fpgas=[];source_digests={}
    for fpga,transport in sorted(transports.items()):
        if transport.get('fpga')!=fpga or transport.get('word_event_policy')!=WORD_EVENT_POLICY:
            raise ValidationError('word transport identity differs from its physical plan')
        source_digests[fpga]=_artifact_digest(transport)
        active={}
        for link_id,peer,kind,lane in _physical_groups(transport):
            link=links.get(link_id)
            if link is None or kind not in {'tx','rx'}or {fpga,peer}!=set(link.endpoints):
                raise ValidationError('word bus direction or peer is invalid')
            direction=(fpga,peer)if kind=='tx'else(peer,fpga)
            begin,end=allocation[(link_id,*direction)]
            if not begin<=lane<end:raise ValidationError('word lane is outside its whole-bank direction budget')
            group=lane//20;bank=board_banks.get((fpga,link_id,group));clock=clocks.get((link_id,group))
            if bank is None or clock is None:raise ValidationError('word bus has no fixed data/clock bank binding')
            record={'bank':bank,'direction':kind,'link':link_id,'peer':peer,'bank_group':group,
                'logical_port':transport_endpoint_port(dict(kind=kind,link=link_id,peer=peer)),
                'logical_lsb':group*160,'logical_port_width':link.transport_bits_per_cycle_per_direction,
                'forwarded_clock':clock,'ip_module':f'emuflow_selectio_bank{bank}_{kind}'}
            if active.setdefault(bank,record)!=record:raise ValidationError('one native word bank cannot change direction or peer')
        fpgas.append({'fpga':fpga,'banks':sorted(active.values(),key=lambda r:int(r['bank']))})
    return {'schema':WORD_PHY_PLAN_SCHEMA,'status':'pass','qualification':'virtual-board-native-word-bank-plan',
        'implementation_status':'requires-vendor-physical-top-and-global-timing',
        'native_phy':{'ip_definition':SELECTIO_IP_DEFINITION,'rx_alignment':WORD_PHY_RX_ALIGNMENT},
        'platform':platform.name,'design':schedule.get('design'),'word_event_policy':WORD_EVENT_POLICY,
        'source':{'board_sha256':_digest(board),'schedule_sha256':canonical_tdm_schedule_sha256(schedule),
                  'transport_sha256':source_digests},'fpgas':fpgas,
        'metrics':{'active_banks':sum(len(f['banks'])for f in fpgas),'word_bits_per_bank':160}}


def validate_word_phy_plan(platform,board,schedule,transports,plan):
    if plan.get('schema')!=WORD_PHY_PLAN_SCHEMA:
        raise ValidationError('unsupported word PHY plan schema; a native alignment selection is required')
    validate_virtual_board(platform,board)
    expected=build_word_phy_plan(platform,board,schedule,transports)
    if plan!=expected:raise ValidationError('word PHY plan differs from its canonical endpoint/wiring sources')
    return {'status':'pass','qualification':plan['qualification'],
        'implementation_status':plan['implementation_status'],**plan['metrics']}
