"""Explicit qualification miters for structurally present source dependencies.

This exports two Boolean cofactors for a native SAT engine. It does not infer
zero delay, modify the timing population, or prove mapped sequential equivalence.
"""
from .errors import ValidationError
from .equivalence import _lut_definition
from .snapshot_timing_population import build_snapshot_timing_population, boundary_id
from pathlib import Path
import subprocess


def export_snapshot_sensitivity_miter(ir, assignment, *, board, port_owners, launch, capture):
    population=build_snapshot_timing_population(ir,assignment,board=board,port_owners=port_owners)
    if launch not in population['launches'] or capture not in population['captures']:
        raise ValidationError('unknown source sensitivity boundary')
    cells={c['id']:c for c in ir.value['instances']}
    nets={n['id']:n for n in ir.value['nets'] if n['cut_class']!='clock'}
    inputs={};constant={};capture_nets={};root_names={}
    for c in cells.values():
        for p in c.get('constant_connections',[]):
            constant[c['id'],p['port'],p['bit']]=int(p['value'])
    for name,net in nets.items():
        d=net['drivers'][0];instance=d['instance']
        if instance is None:
            kind='host' if port_owners.get(d['port'])==board else 'cut'
            root_names[name]=boundary_id(kind,d['port'] if kind=='host' else name,bit=d['bit'] if kind=='host' else 0)
        elif assignment[instance]!=board:root_names[name]=boundary_id('cut',name)
        elif cells[instance]['type']=='$_DFF_P_':root_names[name]=boundary_id('state',instance)
        capture_nets[boundary_id('cut',name)]=name
        for s in net['sinks']:
            inputs[s['instance'],s['port'],s['bit']]=name
            if s['instance'] is None:capture_nets[boundary_id('host',s['port'],bit=s['bit'])]=name
            elif cells[s['instance']]['type']=='$_DFF_P_' and s['port']=='D':
                capture_nets[boundary_id('state',s['instance'])]=name
    target=capture_nets.get(capture)
    if target is None:raise ValidationError('sensitivity capture has no dynamic source net')
    # Only the selected capture cone is expanded, stopping at every local
    # state/import/host launch. Other launches are unconstrained shared inputs.
    order=[];seen=set();visiting=set();definition={};stack=[(target,False)]
    while stack:
        net,finish=stack.pop()
        if net in seen:continue
        if finish:
            visiting.remove(net);seen.add(net);order.append(net);continue
        if net in visiting:raise ValidationError('cycle in source sensitivity cone')
        visiting.add(net);stack.append((net,True))
        if net in root_names:continue
        c=cells[nets[net]['drivers'][0]['instance']]
        width,ip,_,truth=_lut_definition(c)
        arguments=[]
        for bit in range(width):
            key=(c['id'],f'I{bit}' if ip=='I' else ip,0 if ip=='I' else bit)
            if key in inputs:
                arg=inputs[key];arguments.append(('net',arg));stack.append((arg,False))
            elif key in constant:arguments.append(('constant',constant[key]))
            else:raise ValidationError('unbound sensitivity LUT input')
        definition[net]=(width,truth,arguments)
    free=sorted({root_names[n] for n in order if n in root_names and root_names[n]!=launch})
    free_index={name:i for i,name in enumerate(free)};index={name:i for i,name in enumerate(order)}
    lines=[f'module sensitivity(input wire [{max(1,len(free))-1}:0] free_inputs, output wire different);']
    for value in (0,1):
        for net in order:
            dst=f'n{value}_{index[net]}'
            if net in root_names:
                name=root_names[net]
                expr=f"1'b{value}" if name==launch else f'free_inputs[{free_index[name]}]'
            else:
                width,truth,args=definition[net]
                exprs=[f'n{value}_{index[a]}' if kind=='net' else f"1'b{a}" for kind,a in args]
                expr=f"({1<<width}'h{truth:x} >> {{{', '.join(reversed(exprs))}}})"
            lines.append(f'wire {dst} = {expr};')
    lines.extend([f'assign different = n0_{index[target]} ^ n1_{index[target]};','endmodule'])
    return '\n'.join(lines)+'\n',{'launch':launch,'capture':capture,'cone_nets':len(order),
        'free_boundary_inputs':len(free),'scope':'source_boolean_sensitivity_miter',
        'global_timing_qualified':False}


def qualify_snapshot_source_correspondence(ir, assignment, *, board, port_owners,
        bindings, graph, output_dir, yosys, timeout_seconds=60):
    """Explicit bounded qualification; never a production per-path SAT replay.

    Structurally unexplained pairs require native proof on the ORIGINAL cone.
    No caller-supplied pass flag or missing-connection exception is accepted.
    This establishes boundary influence correspondence, not physical Boolean
    equivalence, complete original path timing, or an asynchronous deadline.
    """
    from .snapshot_timing_binding import classify_snapshot_boundary_connections
    population=build_snapshot_timing_population(ir,assignment,board=board,port_owners=port_owners)
    result=classify_snapshot_boundary_connections(population,bindings,graph)
    unresolved=result['classification']['unexplained']
    pairs=result['unexplained_examples']
    if unresolved!=len(pairs):
        raise ValidationError('too many unexplained pairs for bounded sensitivity qualification')
    root=Path(output_dir);root.mkdir(parents=True,exist_ok=True)
    proofs=[]
    for index,(launch,capture) in enumerate(pairs):
        rtl,info=export_snapshot_sensitivity_miter(ir,assignment,board=board,port_owners=port_owners,
            launch=launch,capture=capture)
        directory=root/str(index);directory.mkdir(exist_ok=False)
        (directory/'miter.v').write_text(rtl)
        proc=subprocess.run([str(yosys),'-p',
            'read_verilog miter.v; prep -top sensitivity; sat -verify -prove different 0 -show-inputs'],
            cwd=directory,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout_seconds)
        (directory/'proof.log').write_text(proc.stdout)
        if proc.returncode or 'SAT proof finished - no model found: SUCCESS!' not in proc.stdout:
            raise ValidationError('source dependency independence not proven by native SAT')
        proofs.append(dict(info,proven_independent=True))
    result['classification']=dict(result['classification'],unexplained=0,boolean_independent=unresolved)
    result.update(status='pass',sensitivity_proofs=proofs,scope='source_boundary_influence_correspondence')
    result.pop('unexplained_examples')
    return result
