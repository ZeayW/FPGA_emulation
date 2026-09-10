"""One-shot open dual-ULX3S qualification entry, not fixed-slot compilation.

Run as a dedicated process with ``emuflow ulx3s-qualify``. All
intermediates belong to one fresh scratch directory; only the terminal JSON
survives after synchronous tools have exited. No server-specific paths, cache
publication, fabricated link latency, or automatic qualification promotion.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import time

from .errors import ValidationError


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    add_arguments(p)
    return p


def add_arguments(p):
    p.add_argument('--source', type=Path, action='append', required=True)
    p.add_argument('--top', required=True)
    p.add_argument('--clock', required=True)
    p.add_argument('--target-period-ns', type=float, required=True)
    p.add_argument('--reset-data-port', action='append', default=[])
    p.add_argument('--vectors', type=Path, required=True, help='JSON array of complete host-input value maps')
    p.add_argument('--undefined-state-seed', type=int, required=True)
    p.add_argument('--timing-cycle', type=int, required=True, help='explicit finite-trace cycle for global metrics')
    p.add_argument('--setup-uncertainty-ns', type=float, required=True)
    p.add_argument('--tools', type=Path, required=True, help='OSS CAD Suite bin directory')
    p.add_argument('--openroad', type=Path, required=True)
    p.add_argument('--sta', type=Path, required=True)
    p.add_argument('--toolchain-id', required=True, help='externally pinned installation/version identity')
    p.add_argument('--out', type=Path, required=True, help='new one-shot directory; only result.json is retained')


def _prepare(args):
    sources = [p.resolve(strict=True) for p in args.source]
    vectors_path = args.vectors.resolve(strict=True)
    vectors = json.loads(vectors_path.read_text())
    if not isinstance(vectors,list) or not vectors or any(not isinstance(v,dict) for v in vectors):
        raise ValidationError('qualification vectors must be a nonempty array of host-input maps')
    if (not math.isfinite(args.target_period_ns) or args.target_period_ns <= 0 or
            not math.isfinite(args.setup_uncertainty_ns) or args.setup_uncertainty_ns < 0 or
            args.undefined_state_seed < 0 or not 0 <= args.timing_cycle < len(vectors)):
        raise ValidationError('invalid explicit timing or initial-state contract')
    if len(set(sources)) != len(sources) or any(not p.is_file() for p in sources):
        raise ValidationError('qualification RTL sources must be distinct files')
    tools = args.tools.resolve(strict=True)
    executables = [tools/name for name in ('yosys','yosys-abc','nextpnr-ecp5','ecppack','iverilog','vvp')]
    executables += [args.openroad.resolve(strict=True), args.sta.resolve(strict=True)]
    if any(not p.is_file() or not os.access(p,os.X_OK) for p in executables):
        raise ValidationError('qualification requires the complete executable open toolchain')
    out = args.out.resolve()
    if out.exists():
        raise ValidationError('qualification output already exists; refusing overwrite or duplicate run')
    if any(out in path.parents for path in sources+[vectors_path]+executables):
        raise ValidationError('qualification inputs/tools cannot belong to disposable output')
    return sources,vectors,tools,out


def _remove_joined_scratch(root):
    """Linux process-aware deletion; refuse uncertain ownership, never kill."""
    if not Path('/proc').is_dir():
        raise ValidationError('automatic scratch cleanup requires Linux process inspection')
    ancestors = set(); pid = os.getpid()
    while pid > 0 and pid not in ancestors:
        ancestors.add(pid)
        try:
            pid = int(Path('/proc',str(pid),'stat').read_text().split(') ',1)[1].split()[1])
        except FileNotFoundError:
            break
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or int(proc.name) in ancestors:
            continue
        try:
            cwd = Path(os.readlink(proc/'cwd'))
            command = (proc/'cmdline').read_bytes()
        except (FileNotFoundError,PermissionError,ProcessLookupError):
            continue
        if cwd == root or root in cwd.parents or str(root).encode() in command:
            raise ValidationError('live scratch consumer prevents cleanup: '+proc.name)
    shutil.rmtree(root)


def _connected_vectors(ir, clock, vectors):
    widths = {p['id']:p['width'] for p in ir.value['ports'] if p['direction']=='input' and p['id']!=clock}
    required = {ep['port'] for net in ir.value['nets'] if net['cut_class']!='clock'
                for ep in net['drivers'] if ep['instance'] is None}
    result = []
    for index, vector in enumerate(vectors):
        missing, unknown = required-set(vector), set(vector)-set(widths)
        if missing or unknown:
            raise ValidationError(f'vector {index}: missing connected inputs {sorted(missing)}; unknown inputs {sorted(unknown)}')
        if any(type(value) is not int or not 0 <= value < 1 << widths[name] for name,value in vector.items()):
            raise ValidationError(f'vector {index}: out-of-range or noninteger input value')
        # A supplied value for a declared but optimized-unconnected port is
        # legal, but has no physical host bit. Never fill a missing live input.
        result.append({name:vector[name] for name in required})
    return result


def _measure_hold(graph, pairs, directory, sta):
    from .ecp5_data_graph import select_ecp5_data_cones
    from .ecp5_sta import run_ecp5_pair_checks
    if not pairs:
        return dict(physical_pairs=0,minimum_slack_ns=None,negative_pairs=0,
                    scope='same_edge_exported_sdf_ideal_skew')
    launches = {q:0.0 for q,_ in pairs}
    deadlines = {d:max(v[1][2] for v in graph['captures'][d]['setuphold'].values()) for _,d in pairs}
    cone = select_ecp5_data_cones(graph,roots=set(launches),captures=set(deadlines))
    rows = run_ecp5_pair_checks(cone,directory,pairs=pairs,launches_ns=launches,
        deadlines_ns=deadlines,executable=sta,analysis='min')
    if set(rows) != pairs:
        raise ValidationError('incomplete local hold pair coverage')
    return dict(physical_pairs=len(rows),minimum_slack_ns=min(v['slack_ns'] for v in rows.values()),
        negative_pairs=sum(v['slack_ns']<0 for v in rows.values()),scope='same_edge_exported_sdf_ideal_skew')


def _execute(args, sources, vectors, tools, root, report):
    from .synthesis import run_generic_yosys
    from .yosys import import_yosys_json
    from .snapshot_pair import bind_snapshot_reset_inputs, emit_snapshot_pair
    from .snapshot_partition import partition_snapshot_pair
    from .snapshot_initial import mapped_snapshot_initial_state
    from .snapshot_source_paths import build_snapshot_source_paths
    from .snapshot_equivalence import build_snapshot_equivalence_testbench
    from .snapshot_protocol_events import bind_snapshot_protocol_events
    from .ecp5_backend import run_ulx3s_physical
    from .opensta import _runtime_data_path
    from .ecp5_data_graph import build_ecp5_data_graph
    from .snapshot_timing_population import build_snapshot_timing_population
    from .snapshot_timing_binding import bind_snapshot_timing_boundaries, iter_bound_snapshot_connections
    from .snapshot_global_qualification import qualify_snapshot_global_timing
    mapped_path = root/'mapped.json'
    run_generic_yosys(sources,args.top,mapped_path,executable=str(tools/'yosys'),
        log_path=root/'frontend.log',lut_size=4,abc_executable=str(tools)+'/./yosys-abc')
    ir = import_yosys_json(mapped_path,top=args.top,clocks=[args.clock])
    vectors = _connected_vectors(ir,args.clock,vectors)
    owners = {p['id']:'board0' for p in ir.value['ports'] if p['id'] != args.clock}
    bound = bind_snapshot_reset_inputs(ir,args.reset_data_port)
    database = build_snapshot_source_paths(bound,port_owners=owners,clock_port=args.clock,
                                          target_period_ns=args.target_period_ns)
    report['frontend'] = dict(cells=len(ir.value['instances']),nets=len(ir.value['nets']),source_paths=len(database['paths']))
    result,partition_check = partition_snapshot_pair(ir,output_dir=root/'partition',
        executable=str(args.openroad.resolve()),reset_data_ports=args.reset_data_port,seed=1)
    assignment = result['instance_assignment']
    report['partition'] = partition_check
    report['assignment_sha256'] = hashlib.sha256(json.dumps(assignment,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    initial,report['initial_state'] = mapped_snapshot_initial_state(json.loads(mapped_path.read_text()),
        top=args.top,undefined_seed=args.undefined_state_seed)
    pair = emit_snapshot_pair(ir,assignment,prefix='snapshot_pair',port_owners=owners,initial_state=initial,
        session_id=17,reset_data_ports=args.reset_data_port,include_source_binding=True)
    report['evaluation_rounds'] = pair['evaluation_rounds']
    for board, value in pair['boards'].items():
        (root/(board+'.sv')).write_text(value['rtl'])
    tb,report['macrocycle_equivalence'] = build_snapshot_equivalence_testbench(bound,assignment,pair,
        initial_state=initial,vectors=vectors,timing_events=True)
    (root/'equivalence.sv').write_text(tb)
    transport_dir = _runtime_data_path(Path('rtl/transport/emuflow_gpio_exchange.sv')).parent
    transport = sorted(transport_dir.glob('emuflow_gpio*.sv'))+[transport_dir/'emuflow_snapshot_host.sv']
    with (root/'equivalence.log').open('w') as log:
        subprocess.run([str(tools/'iverilog'),'-g2012','-s','snapshot_equivalence_tb','-o',str(root/'simulation')]+
            list(map(str,transport))+[str(root/(b+'.sv')) for b in ('board0','board1')]+[str(root/'equivalence.sv')],
            stdout=log,stderr=subprocess.STDOUT,check=True)
        subprocess.run([str(tools/'vvp'),str(root/'simulation')],stdout=log,stderr=subprocess.STDOUT,check=True)
    with (root/'equivalence.log').open() as log:
        protocol = bind_snapshot_protocol_events(log,pair,macrocycles=len(vectors))
    models,initial_ready = {},{}
    def physical(board):
        value = pair['boards'][board]
        def consume(routed,mapped,delays):
            graph = build_ecp5_data_graph(routed,delays)
            population = build_snapshot_timing_population(bound,assignment,board=board,port_owners=owners)
            bindings = bind_snapshot_timing_boundaries(population,value['interface'],routed,graph,
                mapped=mapped,mapped_top=value['physical_top'])
            models[board] = dict(graph=graph,bindings=bindings,population=population)
        physical_report = run_ulx3s_physical(transport+[root/(board+'.sv')],top=value['physical_top'],tools=tools,
            output_dir=root/board,board=board,host_uart=value['host_uart'],snapshot_interface=value['interface'],
            timing_consumer=consume)
        physical_report['bitstream_sha256'] = hashlib.sha256((root/board/'endpoint.bit').read_bytes()).hexdigest()
        return board,physical_report
    with ThreadPoolExecutor(max_workers=2) as pool:
        report['physical'] = dict(pool.map(physical,('board0','board1')))
    report['local_hold'] = {}
    for board in ('board0','board1'):
        directory = root/board
        graph,bindings,population = (models[board][key] for key in ('graph','bindings','population'))
        initial_ready.update({key:protocol['reset_release_ns'][board] for key,record in population['launches'].items()
                              if record['kind']=='state'})
        pairs = {(q,d) for _,_,q,targets,_ in iter_bound_snapshot_connections(population,bindings,graph) for d in targets}
        report['local_hold'][board] = _measure_hold(graph,pairs,directory/'hold',str(args.sta.resolve()))
    report['global_timing'] = qualify_snapshot_global_timing(database,bound,assignment,pair=pair,protocol=protocol,
        physical_models=models,port_owners=owners,initial_launch_ns=initial_ready,cycle=args.timing_cycle,
        setup_uncertainty_ns=args.setup_uncertainty_ns,output_dir=root/'global',yosys=tools/'yosys',sta=str(args.sta.resolve()))
    report['status'] = 'checks_finished_qualification_pending'
    if report['global_timing']['readiness_violations'] or any(r['negative_pairs'] for r in report['local_hold'].values()):
        report['status'] = 'timing_violations'


def run(args):
    sources,vectors,tools,out = _prepare(args)
    out.mkdir(parents=True)
    root = out/'.scratch'; root.mkdir()
    report = dict(schema='emuflow.ulx3s-one-shot-qualification/v1',status='running',
        platform='ulx3s-85f-v3.0.x-pair-gpio',top=args.top,clock=args.clock,target_period_ns=args.target_period_ns,
        physical_seed=1,physical_workers=2,toolchain_id=args.toolchain_id,
        sources=[dict(path=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in sources],
        vectors_sha256=hashlib.sha256(args.vectors.read_bytes()).hexdigest(),
        timing_cycle=args.timing_cycle,full_flow_qualified=False,hardware_qualified=False,
        pending=['integrated_acceptance','reset_recovery_removal','metastability_and_external_link_assumptions'])
    env = {name:os.environ.get(name) for name in ('HOME','TMPDIR','XDG_CACHE_HOME','XDG_CONFIG_HOME','XDG_DATA_HOME')}
    start = time.monotonic()
    try:
        for name in env:
            directory = root/name.lower(); directory.mkdir()
            os.environ[name] = str(directory)
        _execute(args,sources,vectors,tools,root,report)
    except Exception as error:
        report.update(status='failed',error=str(error))
    finally:
        for name,value in env.items():
            if value is None:os.environ.pop(name,None)
            else:os.environ[name]=value
        report['elapsed_seconds'] = time.monotonic()-start
        # Save the compact result before attempting process-aware cleanup.
        (out/'result.json').write_text(json.dumps(report,indent=2)+'\n')
        try:
            _remove_joined_scratch(root)
            report['scratch_removed'] = True
        except Exception as error:
            report.update(scratch_removed=False,cleanup_error=str(error))
        (out/'result.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))
    return 0 if report['status']=='checks_finished_qualification_pending' and report['scratch_removed'] else 1


def main(argv=None):
    return run(parser().parse_args(argv))


if __name__ == '__main__':
    raise SystemExit(main())
