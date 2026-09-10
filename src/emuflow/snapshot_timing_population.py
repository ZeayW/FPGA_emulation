"""Source-owned local timing boundaries for the fixed snapshot pair.

Derived from EmuIR and the selected partition, never from a physical report's
observed paths. These conservative structural dependencies are not a timing
engine, partition objective, or an optimization-equivalence proof.
"""
import json
from collections import defaultdict, deque
from .equivalence import _lut_definition
from .errors import ValidationError
from .ulx3s_dut import build_snapshot_boundary_plan


def boundary_id(kind, identity, port='', bit=0):
    return json.dumps((kind,identity,port,bit),separators=(',',':'))


def build_snapshot_timing_population(ir, assignment, *, board, port_owners):
    if board not in ('board0','board1'): raise ValidationError('unknown snapshot timing board')
    plan=build_snapshot_boundary_plan(ir,assignment,port_owners=port_owners)
    local={c['id']:c for c in ir.value['instances'] if assignment[c['id']]==board}
    imports=set(plan['outbound_nets']['board1' if board=='board0' else 'board0'])
    exports=set(plan['outbound_nets'][board])
    launches={}; captures={}; launch_nets={}; capture_nets={}; pins={}; constants={}; nodes=set()

    def launch(kind, identity, net, port='', bit=0):
        key=boundary_id(kind,identity,port,bit)
        launches[key]={'kind':kind,'identity':identity,'port':port,'bit':bit}
        launch_nets.setdefault(key,set()).add(net)

    def capture(kind,identity,net,port='',bit=0):
        key=boundary_id(kind,identity,port,bit)
        if key in captures: raise ValidationError('duplicate logical capture boundary')
        captures[key]={'kind':kind,'identity':identity,'port':port,'bit':bit}
        capture_nets[key]=net

    for name,cell in local.items():
        if cell['type']!='$_DFF_P_' and not (cell['type'].startswith('LUT') or cell['type'] in ('$lut','$_LUT_')):
            raise ValidationError('snapshot timing population requires mapped LUT/positive-edge FF primitives')
        for entry in cell.get('constant_connections',[]):
            if entry['value'] not in ('0','1'):raise ValidationError('unknown source constant')
            constants[name,entry['port'],entry['bit']]=entry['value']
    for net in ir.value['nets']:
        if net['cut_class']=='clock':continue
        if net['cut_class']=='reset':raise ValidationError('DUT reset needs explicit data binding before timing population')
        if len(net['drivers'])!=1:raise ValidationError('source timing net needs one driver')
        driver=net['drivers'][0]; instance=driver['instance']; name=net['id']
        touches=instance in local or name in imports or name in exports or any(
            e['instance'] in local or (e['instance'] is None and port_owners.get(e['port'])==board)
            for e in net['sinks'])
        if not touches:continue
        nodes.add(name)
        for ep in net['drivers']+net['sinks']:
            if ep['instance'] in local:
                key=(ep['instance'],ep['port'],ep['bit'])
                if key in pins or key in constants:raise ValidationError('multiply bound source timing pin')
                pins[key]=name
        if name in imports:
            launch('cut',name,name)
        elif instance is None:
            if port_owners.get(driver['port'])!=board:raise ValidationError('missing host launch owner')
            launch('host',driver['port'],name,bit=driver['bit'])
        elif instance in local and local[instance]['type']=='$_DFF_P_':
            if driver['port']!='Q' or driver['bit']!=0:raise ValidationError('unsupported source FF output')
            launch('state',instance,name)
        elif instance not in local:
            raise ValidationError('foreign data driver has no imported cut boundary')
        if name in exports:capture('cut',name,name)
        for ep in net['sinks']:
            if ep['instance'] is None and port_owners.get(ep['port'])==board:
                capture('host',ep['port'],name,bit=ep['bit'])
    edges=defaultdict(set)
    for name,cell in local.items():
        if cell['type']=='$_DFF_P_':
            key=(name,'D',0)
            if key not in pins and key not in constants:raise ValidationError('missing original FF data input')
            capture('state',name,pins.get(key))
            continue
        width,ip,op,_=_lut_definition(cell)
        if not 1<=width<=6:raise ValidationError('unsupported source LUT width')
        target=pins.get((name,op,0))
        if target is None:raise ValidationError('missing local LUT output')
        for bit in range(width):
            key=(name,f'I{bit}' if ip=='I' else ip,0 if ip=='I' else bit)
            if key in pins:edges[pins[key]].add(target)
            elif key not in constants:raise ValidationError('missing local LUT input')
    labels=tuple(sorted(launches)); masks={}
    for index,key in enumerate(labels):
        for net in launch_nets[key]:masks[net]=masks.get(net,0)|(1<<index)
    indegree={net:0 for net in nodes}
    for targets in edges.values():
        for net in targets:indegree[net]+=1
    queue=deque(sorted(net for net in nodes if not indegree[net])); visited=0
    while queue:
        net=queue.popleft();visited+=1
        for target in edges[net]:
            masks[target]=masks.get(target,0)|masks.get(net,0)
            indegree[target]-=1
            if not indegree[target]:queue.append(target)
    if visited!=len(nodes):raise ValidationError('source combinational cycle in snapshot timing population')
    return {'board':board,'launches':launches,'captures':captures,'launch_labels':labels,
            'capture_masks':{key:masks.get(net,0) for key,net in capture_nets.items()},
            'scope':'source_local_structural_boundary_dependencies',
            'optimization_equivalence_qualified':False,'global_timing_qualified':False}


def required_snapshot_connections(population):
    """Stream source-required pairs without retaining a duplicated pair table."""
    labels=population['launch_labels']
    for capture,mask in population['capture_masks'].items():
        while mask:
            bit=mask & -mask
            yield labels[bit.bit_length()-1],capture
            mask-=bit
