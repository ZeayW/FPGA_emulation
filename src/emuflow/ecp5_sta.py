"""Export a shared physical ECP5 data graph for native OpenSTA analysis.

This graph is a conservative scalar-max abstraction of routed rise/fall arcs,
not a functional Verilog design or a whole-original-design timing report.
Every launch epoch and capture deadline is supplied explicitly by the caller.
The asynchronous protocol contract must supply these bounds before promotion.
"""
import math
import subprocess
from pathlib import Path
from .errors import ValidationError
from .opensta import render_opensta_liberty
from .native_tools import resolve_native_executable


def export_ecp5_data_checks(graph, directory, *, launches_ns, deadlines_ns):
    dynamic = graph['dynamic_nodes']
    roots = sorted(set(graph['roots']) & dynamic)
    captures = sorted(set(graph['captures']) & dynamic)
    if not roots or not captures:
        raise ValidationError('physical STA has no dynamic launch/capture population')
    if set(launches_ns) != set(roots) or set(deadlines_ns) != set(captures):
        raise ValidationError('physical STA requires explicit complete launch/deadline bindings')
    for value in (*launches_ns.values(), *deadlines_ns.values()):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValidationError('physical STA constraints must be finite numbers')
    def maximum(values):
        if (len(values) != 2 or any(len(v) != 3 for v in values)
                or any(type(n) not in (int,float) or not math.isfinite(n) or n < 0
                       for v in values for n in v)):
            raise ValidationError('invalid physical propagation delay')
        return max(v[2] for v in values)
    arcs = [(a,b,maximum(v)) for a,b,v in graph['edges'] if a in dynamic]
    cq = {r:maximum(graph['roots'][r]['clock_to_q']) for r in roots
          if graph['roots'][r]['kind']=='ff'}
    values = sorted(set(cq.values()) | {v for _,_,v in arcs})
    names = {v:f'D{i}' for i,v in enumerate(values)}
    model = {'name':'ecp5_physical_pin_arcs','cells':{
        name:{'kind':'combinational','inputs':['A'],'output':'Y','delay_ns':v}
        for v,name in names.items()}}
    nodes = {n:f'n{i}' for i,n in enumerate(graph['order']) if n in dynamic}
    incoming = {n:[] for n in nodes}
    for index, (_,sink,_) in enumerate(arcs): incoming[sink].append(f'e{index}')
    for size in {len(v) for v in incoming.values() if len(v)>1}:
        model['cells'][f'M{size}']={'kind':'combinational','inputs':[f'A{i}' for i in range(size)],
                                  'output':'Y','delay_ns':0.0}
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=True)
    (directory/'physical.lib').write_text(render_opensta_liberty(model))
    with (directory/'physical.v').open('w') as v, (directory/'physical.sdc').open('w') as s:
        ports=[f'i{i}' for i in range(len(roots))]+[f'o{i}' for i in range(len(captures))]
        v.write('module physical_data('+','.join(ports)+');\n')
        for i in range(len(roots)): v.write(f'input i{i};\n')
        for i in range(len(captures)): v.write(f'output o{i};\n')
        for name in nodes.values(): v.write(f'wire {name};\n')
        s.write('create_clock -name epoch -period 1\n')
        for i,root in enumerate(roots):
            if root in cq: v.write(f'{names[cq[root]]} q{i} (.A(i{i}), .Y({nodes[root]}));\n')
            else: v.write(f'assign {nodes[root]}=i{i};\n')
            s.write(f'set_input_delay -clock epoch -max {launches_ns[root]:.17g} [get_ports i{i}]\n')
            s.write(f'set_input_transition 0 [get_ports i{i}]\n')
        for i,(source,sink,value) in enumerate(arcs):
            v.write(f'wire e{i};\n{names[value]} a{i} (.A({nodes[source]}), .Y(e{i}));\n')
        for sink, fanin in incoming.items():
            if not fanin: continue
            if len(fanin)==1: v.write(f'assign {nodes[sink]}={fanin[0]};\n')
            else:
                pins=', '.join(f'.A{i}({wire})' for i,wire in enumerate(fanin))
                v.write(f'M{len(fanin)} m{nodes[sink]} ({pins}, .Y({nodes[sink]}));\n')
        for i,capture in enumerate(captures):
            v.write(f'assign o{i}={nodes[capture]};\n')
            s.write(f'set_output_delay -clock epoch -max {1-deadlines_ns[capture]:.17g} [get_ports o{i}]\n')
        v.write('endmodule\n')
    (directory/'analyze.tcl').write_text('''proc analyze {} {
  read_liberty physical.lib
  read_verilog physical.v
  link_design physical_data
  set ::emuflow_cell [[sta::top_instance] cell]
  set constraints [open physical.sdc r]
  while {[gets $constraints line] >= 0} {
    uplevel #0 [string map [list {[get_ports } {[$::emuflow_cell find_port }] $line]
  }
  close $constraints
  set out [open measurements.tsv w]
  puts $out "endpoint\\tarrival_ns\\trequired_ns\\tslack_ns"
  set paths [find_timing_paths -path_delay max -group_count COUNT -endpoint_count 1]
  foreach p $paths {
    set endpoint [get_property [get_property $p endpoint] full_name]
    set arrival [expr {[$p data_arrival_time] * 1.0e9}]
    set required [expr {[$p data_required_time] * 1.0e9}]
    set slack [expr {[$p slack] * 1.0e9}]
    puts $out "$endpoint\\t$arrival\\t$required\\t$slack"
  }
  close $out
}
if {[catch {analyze} message]} {puts stderr $message; exit 2}
exit 0
'''.replace('COUNT',str(len(captures))))
    return captures


def run_ecp5_data_checks(graph, directory, *, launches_ns, deadlines_ns, executable=None):
    directory=Path(directory)
    captures=export_ecp5_data_checks(graph,directory,launches_ns=launches_ns,deadlines_ns=deadlines_ns)
    tool=resolve_native_executable('sta',executable)
    output=directory/'measurements.tsv'; output.unlink(missing_ok=True)
    with (directory/'opensta.log').open('w') as log:
        result=subprocess.run([tool,'-exit','analyze.tcl'],cwd=directory,stdout=log,stderr=subprocess.STDOUT)
    if result.returncode or not output.is_file(): raise ValidationError('physical OpenSTA failed; see opensta.log')
    measurements={}
    with output.open() as stream:
        if next(stream,'').strip()!='endpoint\tarrival_ns\trequired_ns\tslack_ns':
            raise ValidationError('invalid physical STA result header')
        for line in stream:
            fields=line.rstrip('\n').split('\t')
            if len(fields)!=4 or fields[0] in measurements: raise ValidationError('duplicate/malformed physical STA result')
            try: values=tuple(map(float,fields[1:]))
            except ValueError as exc: raise ValidationError('nonnumeric physical STA result') from exc
            if not all(math.isfinite(n) for n in values): raise ValidationError('nonfinite physical STA result')
            measurements[fields[0]]=values
    if set(measurements)!={f'o{i}' for i in range(len(captures))}:
        raise ValidationError('physical STA missing/extra/unconstrained endpoints')
    return {capture:dict(zip(('arrival_ns','required_ns','slack_ns'),measurements[f'o{i}']))
            for i,capture in enumerate(captures)}


def run_ecp5_pair_checks(graph, directory, *, pairs, launches_ns, deadlines_ns, executable=None):
    """Native launch/capture-pair checks, not just each endpoint's worst root.

    All requests share one physical timing graph. Queries are grouped by
    physical launch; merged original identities can reuse a returned pair.
    Missing paths fail. Boolean-independent and hold relations must be
    classified separately and must not be submitted as zero-delay paths.
    """
    pairs=sorted(set(pairs));roots=sorted(set(graph['roots']) & graph['dynamic_nodes'])
    captures=sorted(set(graph['captures']) & graph['dynamic_nodes'])
    ri={p:i for i,p in enumerate(roots)};ci={p:i for i,p in enumerate(captures)}
    if not pairs or any(a not in ri or z not in ci for a,z in pairs):
        raise ValidationError('pair STA requires bound dynamic launch/capture pins')
    directory=Path(directory)
    export_ecp5_data_checks(graph,directory,launches_ns=launches_ns,deadlines_ns=deadlines_ns)
    with (directory/'requests.tsv').open('w') as stream:
        for a,z in pairs:stream.write(f'i{ri[a]}\to{ci[z]}\n')
    # Keep constraint loading identical to the endpoint adapter. Only the
    # native query/report block differs; no Python timing propagation occurs.
    script=(directory/'analyze.tcl').read_text()
    begin=script.index('  set out [open measurements.tsv w]')
    end=script.index('\n  close $out',begin)+len('\n  close $out')
    query='''  set requests [open requests.tsv r]
  set groups [dict create]
  while {[gets $requests line] >= 0} {
    lassign [split $line "\\t"] launch capture
    dict lappend groups $launch $capture
  }
  close $requests
  set out [open pairs.tsv w]
  puts $out "launch\\tendpoint\\tarrival_ns\\trequired_ns\\tslack_ns"
  dict for {launch endpoints} $groups {
    set targets {}
    foreach endpoint $endpoints {lappend targets [$::emuflow_cell find_port $endpoint]}
    set paths [find_timing_paths -from [$::emuflow_cell find_port $launch] -to $targets -path_delay max -group_count [llength $endpoints] -endpoint_count 1]
    foreach p $paths {
      set endpoint [get_property [get_property $p endpoint] full_name]
      set arrival [expr {[$p data_arrival_time] * 1.0e9}]
      set required [expr {[$p data_required_time] * 1.0e9}]
      set slack [expr {[$p slack] * 1.0e9}]
      puts $out "$launch\\t$endpoint\\t$arrival\\t$required\\t$slack"
    }
  }
  close $out'''
    (directory/'analyze.tcl').write_text(script[:begin]+query+script[end:])
    output=directory/'pairs.tsv';output.unlink(missing_ok=True)
    tool=resolve_native_executable('sta',executable)
    with (directory/'opensta.log').open('w') as log:
        proc=subprocess.run([tool,'-exit','analyze.tcl'],cwd=directory,stdout=log,stderr=subprocess.STDOUT)
    if proc.returncode or not output.is_file():raise ValidationError('pair OpenSTA failed; see opensta.log')
    expected={(f'i{ri[a]}',f'o{ci[z]}'):(a,z) for a,z in pairs};result={}
    with output.open() as stream:
        if next(stream,'').strip()!='launch\tendpoint\tarrival_ns\trequired_ns\tslack_ns':
            raise ValidationError('invalid pair STA result header')
        for line in stream:
            fields=line.rstrip('\n').split('\t');key=tuple(fields[:2])
            if len(fields)!=5 or key not in expected or expected[key] in result:
                raise ValidationError('duplicate/extra/malformed pair STA result')
            try:values=tuple(map(float,fields[2:]))
            except ValueError as exc:raise ValidationError('nonnumeric pair STA result') from exc
            if not all(math.isfinite(v) for v in values):raise ValidationError('nonfinite pair STA result')
            result[expected[key]]=dict(zip(('arrival_ns','required_ns','slack_ns'),values))
    if set(result)!=set(pairs):raise ValidationError('pair STA missing requested paths')
    return result
