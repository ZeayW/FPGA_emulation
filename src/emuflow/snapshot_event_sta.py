"""Explicit source/protocol/physical setup qualification for snapshot RTL.

This orchestrates independent source correspondence and native physical STA.
It reports finite-trace local setup checks, never whole-original-design QoR.
"""
from pathlib import Path

from .errors import ValidationError
from .ecp5_sta import run_ecp5_event_setup_checks
from .snapshot_sensitivity import qualify_snapshot_source_correspondence
from .snapshot_timing_binding import iter_bound_snapshot_connections
from .snapshot_timing_population import build_snapshot_timing_population
from .snapshot_timing_windows import iter_snapshot_timing_windows


def qualify_snapshot_event_setup(ir, assignment, *, board, port_owners, interface,
        bindings, graph, protocol, initial_ready_ns, setup_uncertainty_ns,
        output_dir, yosys, sta):
    """Check every timed source relation at each observed capture occurrence.

    Source Boolean independence is proven here with native SAT, not accepted
    as a caller's waiver. CE retention remains a nontimed state-hold relation,
    not a zero-delay route. Detailed native files are active-run scratch only.
    """
    root = Path(output_dir)
    population = build_snapshot_timing_population(ir, assignment, board=board, port_owners=port_owners)
    correspondence = qualify_snapshot_source_correspondence(ir, assignment, board=board,
        port_owners=port_owners, bindings=bindings, graph=graph,
        output_dir=root / 'source-correspondence', yosys=yosys)
    independent = {(proof['launch'], proof['capture']) for proof in correspondence['sensitivity_proofs']}
    connections = list(iter_bound_snapshot_connections(population, bindings, graph))
    unexplained = {(a, z) for a, z, _, _, kind in connections if kind == 'unexplained'}
    if unexplained != independent:
        raise ValidationError('source independence does not exactly cover unexplained event relations')
    windows = []
    for index, window in enumerate(iter_snapshot_timing_windows(population, interface, protocol,
                                                              initial_ready_ns=initial_ready_ns)):
        selected = set(window['captures'])
        pairs, launch_edges, capture_edges = set(), {}, {}
        logical_timed = retained = independent_count = 0
        for launch, capture, pin, targets, kind in connections:
            if capture not in selected:
                continue
            if kind == 'state_hold':
                retained += 1
                continue
            if kind == 'unexplained':
                independent_count += 1
                continue
            if not targets:
                raise ValidationError('timed source relation has no physical target')
            logical_timed += 1
            time = window['launches_ns'][launch]
            if pin in launch_edges and launch_edges[pin] != time:
                raise ValidationError('merged physical launch has inconsistent logical event epochs')
            launch_edges[pin] = time
            for target in targets:
                pairs.add((pin, target))
                capture_edges[target] = window['capture_edge_ns']
        row = dict(cycle=window['cycle'], epoch=window['epoch'],
            capture_edge_ns=window['capture_edge_ns'], logical_timed_pairs=logical_timed,
            state_hold_relations=retained, boolean_independent_relations=independent_count,
            physical_pairs=len(pairs), minimum_setup_slack_ns=None, negative_pairs=0)
        if pairs:
            measurements = run_ecp5_event_setup_checks(graph, root / f'window-{index}',
                pairs=pairs, launch_edges_ns=launch_edges, capture_edges_ns=capture_edges,
                setup_uncertainty_ns=setup_uncertainty_ns, executable=sta)
            if set(measurements) != pairs:
                raise ValidationError('native event setup pair coverage mismatch')
            row.update(minimum_setup_slack_ns=min(v['slack_ns'] for v in measurements.values()),
                       negative_pairs=sum(v['slack_ns'] < 0 for v in measurements.values()))
        windows.append(row)
    if not windows:
        raise ValidationError('snapshot event setup has no capture windows')
    negative = sum(row['negative_pairs'] for row in windows)
    return dict(scope='finite_trace_source_bound_physical_setup_not_global_timing',
        status='setup_violations' if negative else 'setup_checks_pass',
        board=board, source_correspondence=correspondence, windows=windows,
        negative_pair_occurrences=negative, setup_uncertainty_ns=setup_uncertainty_ns,
        initial_readiness_scope='explicit_conditional_assumption_not_reset_qualification',
        global_timing_qualified=False, hold_qualified=False, hardware_qualified=False)
