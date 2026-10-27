"""Tiny independent graph checks for current native cluster/candidate tests.

Transitive reachability intentionally replaces the production SCC algorithm;
this cubic oracle is capped at 256 cells and cannot run a production design.
"""
from emuflow.ir.resources import RESOURCE_FIELDS


def candidate_ids(ir):
    cells={cell['id']:cell for cell in ir.value['instances']}
    assert len(cells)<=256
    def kind(cell):
        resources=cell['resources']
        if any(resources.get(key,0) for key in ('ff','bram','bram18k','uram288')):return 'state'
        if resources.get('lut',0)>0 and not any(resources.get(key,0) for key in RESOURCE_FIELDS if key!='lut'):return 'soft'
        return 'opaque'
    kinds={name:kind(cell) for name,cell in cells.items()}
    reach={name:set() for name in cells if kinds[name]!='state'}
    for net in ir.value['nets']:
        drivers={e['instance'] for e in net['drivers'] if e['instance'] is not None}
        if len(drivers)!=1 or net['cut_class'] in {'clock','reset'}:continue
        source=next(iter(drivers))
        if source in reach:reach[source].update(e['instance'] for e in net['sinks'] if e['instance'] in reach)
    for middle in reach:
        for source in reach:
            if middle in reach[source]:reach[source].update(reach[middle])
    cyclic={name for name,targets in reach.items() if name in targets}
    safe={'FDCE':{'D','CE'},'FDPE':{'D','CE'},'FDRE':{'D','CE','R'},'FDSE':{'D','CE','S'}}
    result=set()
    for net in ir.value['nets']:
        if net['cut_class']!='combinational' or len(net['drivers'])!=1 or not net['sinks']:continue
        source=net['drivers'][0]['instance']
        if source is None or kinds[source]!='soft' or source in cyclic:continue
        permitted=True
        for endpoint in net['sinks']:
            sink=endpoint['instance']
            if sink is None:continue
            cell=cells[sink]
            if kinds[sink]=='soft':permitted &= sink not in cyclic
            elif kinds[sink]=='state':permitted &= endpoint['port']=='D' if cell['type'].startswith('$_DFF_') else endpoint['port'] in safe.get(cell['type'],())
            else:permitted=False
        if permitted:result.add(net['id'])
    return result


def cluster_records(ir,constraints,eligible=()):
    cells={cell['id']:cell for cell in ir.value['instances']}
    assert len(cells)<=256
    components=[{name} for name in sorted(cells)]
    def merge(members):
        nonlocal components
        hit=[group for group in components if group & members]
        if hit:components=[group for group in components if not group & members]+[set().union(*hit)]
    for group in constraints['groups']:merge(set(group['instances']))
    for net in ir.value['nets']:
        if net['cut_class'] not in {'register_input','register_output','clock','reset','primary_input'} and net['id'] not in eligible:
            merge({e['instance'] for e in [*net['drivers'],*net['sinks']] if e['instance'] is not None})
    fixed={row['instance']:row['fpga'] for row in constraints['fixed']}
    result=[]
    for i,members in enumerate(sorted(components,key=lambda group:min(group))):
        targets={fixed[name] for name in members if name in fixed};assert len(targets)<=1
        totals={key:sum(cells[name]['resources'].get(key,0) for name in members) for key in RESOURCE_FIELDS}
        result.append(dict(id=f'c{i:06d}',instances=sorted(members),resources={key:value for key,value in totals.items() if value},
            fixed_fpga=next(iter(targets),None),groups=sorted(group['id'] for group in constraints['groups'] if set(group['instances'])&members)))
    return result
