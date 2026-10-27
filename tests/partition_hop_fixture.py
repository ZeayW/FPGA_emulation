"""Small current-store inputs and an independent hop-count oracle."""
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from emuflow.emuir_compact import write_compact_emuir
from tests.partition_balance_fixture import active_partition_fpgas
from tests.partition_balance_fixture import transported_cut_classes_for_clusters
from tests.native_partition_facts import (build_clusters, build_partition_assignment, normalize_partition_constraints)
from tests.native_partition_kernel_driver import initialize
from emuflow.partition_ir import load_partition_ir
from emuflow.platform import Platform
from emuflow.yosys import import_yosys_json


PLACEMENT = {'q_reg[0]': 'F0', 'q_reg[1]': 'F0', 'q_reg[2]': 'F1', 'q_reg[3]': 'F1',
             'next_lut[1]': 'F2', 'next_lut[2]': 'F2', 'next_lut[0]': 'F3', 'next_lut[3]': 'F3'}


def line_platform():
    return Platform.from_dict({
        'schema': 'emuflow.boarddb/v1', 'platform': {'name': 'academic_line4', 'kind': 'virtual'},
        'fpgas': [{'id': f'F{i}', 'part': 'vtr-academic', 'utilization_limit': 1.0,
                   'capacity': {'lut': 32, 'ff': 32}} for i in range(4)],
        'links': [{'id': f'L{i}{i+1}', 'endpoints': [f'F{i}', f'F{i+1}'],
                   'direction': 'full_duplex', 'mode': 'abstract', 'data_lanes_per_direction': 8,
                   'fabric_clock_mhz': 50, 'latency_cycles': 1} for i in range(3)]})


def case(root, *, fixed=False):
    repository = Path(__file__).resolve().parents[1]
    reference = import_yosys_json(repository / 'examples/yosys/counter.json', top='counter', clocks=['clk'])
    platform = line_platform()
    raw = {'schema': 'emuflow.partition-constraints/v1', 'min_used_fpgas': 4, 'balance_tolerance': 1.0}
    if fixed:
        raw['fixed'] = [{'instance': 'q_reg[0]', 'fpga': 'F0'},
                        {'instance': 'next_lut[0]', 'fpga': 'F3'}]
    path = root / 'source.sqlite'
    write_compact_emuir(path, reference.value)
    ir = load_partition_ir(path)
    constraints = normalize_partition_constraints(raw, ir, platform)
    clusters = build_clusters(ir, constraints, cut_mode='static-exact-combinational', _native_compact_clusters=True)
    parts = [fpga.id for fpga in active_partition_fpgas(platform, constraints)]
    targets = []
    for cluster in clusters['clusters']:
        owners = {PLACEMENT[member] for member in cluster['instances']}
        assert len(owners) == 1
        targets.append(parts.index(owners.pop()))
    solution = root / 'initial.txt'
    solution.write_text(''.join(str(part)+'\n' for part in targets))
    owner, _ = initialize(ir, platform, clusters, constraints, seed=7, executable=None, solution_input=solution)
    assignment = build_partition_assignment(ir, platform, clusters, constraints, owner, provider='fixture', seed=7,
        _include_semantic_contract=False, _stream_semantic_records=True)
    route_path = root / 'route-constraints.json'
    route_path.write_text(json.dumps({'schema': 'emuflow.system-route-constraints/v1',
                                     'max_route_hops': 1, 'frame_slots': 32}))
    return SimpleNamespace(reference=reference, ir=ir, platform=platform, constraints=constraints,
                           clusters=clusters, assignment=assignment, route_path=route_path,
                           solution=solution)


def weights(root, rows):
    path = root / 'weights.sqlite'
    with sqlite3.connect(path) as database:
        database.execute('PRAGMA user_version=1')
        database.executescript((Path(__file__).parents[1] / 'src/emuflow/data/schema/sta_net_weights.sql').read_text())
        database.executemany('INSERT INTO net_weights VALUES(?,?,?,?)', rows)
    selector = root / 'weights.json'
    selector.write_text(json.dumps({'schema': 'emuflow.partition-net-weights/v2', 'payloads': {'weights': {
        'format': 'sqlite-net-weights/v1', 'path': path.name,
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'records': len(rows)}}}))
    return selector


def audit(reference, clusters, owners, distances, limit):
    """Count original-source endpoints; no solver or projected kernel model."""
    by_cell = {member: owners[cluster['id']] for cluster in clusters['clusters'] for member in cluster['instances']}
    classes = transported_cut_classes_for_clusters(clusters)
    count = total = maximum = unreachable = 0
    violations = []
    for net in reference.value['nets']:
        if net['cut_class'] not in classes:
            continue
        sources = {by_cell[p['instance']] for p in net['drivers'] if p['instance'] is not None}
        sinks = {by_cell[p['instance']] for p in net['sinks'] if p['instance'] is not None}
        if not sources or not sinks:
            continue
        assert len(sources) == 1
        source = next(iter(sources))
        for sink in sorted(sinks - {source}):
            count += 1
            hops = distances[source][sink]
            if hops is None:
                unreachable += 1
            else:
                total += hops
                maximum = max(maximum, hops)
            if hops is None or hops > limit:
                violations.append({'net': net['id'], 'source': source, 'sink': sink, 'hops': hops, 'limit': limit})
    violations.sort(key=lambda row: (row['net'], row['source'], row['sink']))
    return {'status': 'violation' if violations else 'pass', 'max_route_hops': limit,
            'routed_net_sink_pairs': count, 'violating_net_sink_pairs': len(violations),
            'unreachable_net_sink_pairs': unreachable, 'maximum_observed_hops': maximum,
            'total_shortest_path_hops': total, 'violations': violations[:64],
            'violations_truncated': max(0, len(violations) - 64)}


def weighted_objective(reference, clusters, owners, distances, limit, weights_by_net):
    """Independent scalar objective check; no move search or model protocol."""
    by_cell = {member: owners[cluster['id']] for cluster in clusters['clusters'] for member in cluster['instances']}
    classes = transported_cut_classes_for_clusters(clusters)
    result = {'weighted_excess': 0.0, 'weighted_hops': 0.0, 'cut_weight': 0.0}
    for net in reference.value['nets']:
        drivers = {by_cell[p['instance']] for p in net['drivers'] if p['instance'] is not None}
        sinks = {by_cell[p['instance']] for p in net['sinks'] if p['instance'] is not None}
        if net['cut_class'] not in classes or not drivers or not sinks:
            continue
        assert len(drivers) == 1
        source = next(iter(drivers))
        remote = sinks - {source}
        weight = weights_by_net.get(net['id'], 1.0)
        if remote:
            result['cut_weight'] += weight
        for sink in remote:
            hops = distances[source][sink]
            assert hops is not None  # This oracle fixture has a connected line.
            result['weighted_hops'] += weight * hops
            result['weighted_excess'] += weight * max(0, hops - limit)
    return result
