"""Bind source-owned snapshot timing boundaries to physical storage pins."""
from .errors import ValidationError
from .snapshot_physical_binding import bind_snapshot_routed_identities
from .snapshot_timing_population import required_snapshot_connections
from .ecp5_data_graph import project_data_reachability


def bind_snapshot_timing_boundaries(population, interface, routed, graph, *, mapped, mapped_top, top='top'):
    if population['board'] != interface['board']:
        raise ValidationError('source timing population belongs to another board')
    source=interface.get('source_binding')
    if not isinstance(source,dict):raise ValidationError('source-bound snapshot interface required')
    aliases={'schema':'emuflow.snapshot-source-binding/v1','nets':{},'registers':{},'register_ports':{}}
    roles={}
    host_tables={}
    cut_tables={}
    for role,field in [('launches','imported_nets'),('captures','exported_nets')]:
        names=interface[field]
        if len(set(names))!=len(names):raise ValidationError('duplicate emitted cut boundary')
        cut_tables[role]={name:index for index,name in enumerate(names)}
    for role,field in [('launches','host_inputs'),('captures','host_outputs')]:
        table={}
        for row in interface[field]:
            key=(row['port'],row['bit'])
            if key in table or type(row['index']) is not int or row['index']<0:
                raise ValidationError('invalid host boundary index')
            table[key]=row['index']
        host_tables[role]=table
    for role in ('launches','captures'):
        for name,boundary in population[role].items():
            identity=boundary['identity'];kind=boundary['kind'];key=role+':'+name
            if kind=='state':
                local=source.get('registers',{}).get(identity)
                if not isinstance(local,str):raise ValidationError('missing source state boundary alias')
                alias='dut.'+local
            elif kind=='cut':
                index=cut_tables[role].get(identity)
                if index is None:
                    raise ValidationError('cut boundary does not match emitted interface')
                port='exchange.remote_snapshot' if role=='launches' else 'exchange.snapshot'
                alias=f'{port}[{index}]'
                aliases['register_ports'][key]=[{'port':port,'bit':index}]
            elif kind=='host':
                index=host_tables[role].get((identity,boundary['bit']))
                if index is None:raise ValidationError('missing emitted host timing boundary')
                port='held_inputs' if role=='launches' else 'host_outputs'
                alias=f'{port}[{index}]'
                aliases['register_ports'][key]=[{'port':port,'bit':index}]
            else:raise ValidationError('unknown source timing boundary kind')
            aliases['registers'][key]=alias;roles[key]=(role,name)
    resolved=bind_snapshot_routed_identities(aliases,routed,hierarchy='core',top=top,
        mapped=mapped,mapped_top=mapped_top)['registers']
    result={'launches':{},'captures':{},'constant_launches':{},'constant_captures':{},
            'capture_controls':{},'hold_pins':{},
            'global_timing_qualified':False}
    for key,record in resolved.items():
        role,name=roles[key]
        if record['kind']=='constant':
            result['constant_'+role][name]=record['value'];continue
        cellname=record['cell'];port='Q'
        if role=='captures':
            mode=str(routed['modules'][top]['cells'][cellname].get('parameters',{}).get('SD','')).strip()
            if mode not in ('0','1'):raise ValidationError('unknown boundary FF capture mode')
            port='DI' if mode=='1' else 'M'
        pin=(cellname,port)
        if pin not in graph['roots' if role=='launches' else 'captures']:
            raise ValidationError('bound source boundary is missing from physical data graph')
        result[role][name]=pin
        if role=='captures':
            # Yosys can extract the source D-input mux into physical CE or
            # synchronous LSR. These are real timed capture pins, not D arcs.
            result['capture_controls'][name]=tuple((cellname,p) for p in ('CE','LSR')
                if (cellname,p) in graph['captures'])
            if (cellname,'CE') in graph['captures']:
                result['hold_pins'][name]=(cellname,'Q')
    return result


def classify_snapshot_boundary_connections(population, bindings, graph):
    # Constant-folded boundaries are explicit, not silently removed from the
    # source dependency population. A separate equivalence proof is required.
    if bindings['constant_launches'] or bindings['constant_captures']:
        for launch,capture in required_snapshot_connections(population):
            if launch in bindings['constant_launches'] or capture in bindings['constant_captures']:
                raise ValidationError('constant-folded source dependency requires semantic qualification')
    pins={}
    for name,pin in bindings['captures'].items():
        pins['data:'+name]=pin
        for control in bindings.get('capture_controls',{}).get(name,()):
            pins[control[1]+':'+name]=control
    projection=project_data_reachability(graph,bindings['launches'],pins)
    labels={name:index for index,name in enumerate(projection['launch_labels'])}
    masks=projection['capture_masks']
    counts={'data':0,'synchronous_control':0,'state_hold':0,'unexplained':0}
    examples=[]
    for launch,capture in required_snapshot_connections(population):
        if launch not in labels or capture not in bindings['captures']:
            raise ValidationError('unbound required source boundary')
        bit=1<<labels[launch]
        if masks.get('data:'+capture,0)&bit:kind='data'
        elif any(masks.get(p+':'+capture,0)&bit for p in ('CE','LSR')):kind='synchronous_control'
        elif bindings.get('hold_pins',{}).get(capture)==bindings['launches'][launch]:
            # Physical CE retains Q with no routed Q->D feedback. Record a
            # state-retention relation, never invent a zero-delay timing path.
            kind='state_hold'
        else:
            kind='unexplained'
            if len(examples)<8:examples.append((launch,capture))
        counts[kind]+=1
    return {'required_pairs':sum(counts.values()),'classification':counts,
            'unexplained_examples':examples,'scope':'source_required_boundary_influence',
            'original_path_coverage_qualified':False,'global_timing_qualified':False}


def qualify_snapshot_boundary_connections(population, bindings, graph):
    result=classify_snapshot_boundary_connections(population,bindings,graph)
    if result['classification']['unexplained']:
        raise ValidationError('missing required physical data connection: '+
            repr(result['unexplained_examples'][0]))
    return dict(result,status='pass')
