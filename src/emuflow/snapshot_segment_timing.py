"""Native physical cone-bound producer for snapshot source segments.

Untimed source relations remain explicit. This producer neither waives them
nor substitutes zero delay; source correspondence/retention handling and full
original-path coverage are separate required gates.
"""
from pathlib import Path

from .ecp5_data_graph import select_ecp5_data_cones
from .ecp5_sta import run_ecp5_pair_checks
from .errors import ValidationError
from .snapshot_timing_binding import iter_bound_snapshot_connections


def measure_snapshot_segment_bounds(population, bindings, graph, *, output_dir, sta):
    connections = list(iter_bound_snapshot_connections(population, bindings, graph))
    pairs = {(source, target) for _, _, source, targets, _ in connections for target in targets}
    measurements = {}
    if pairs:
        roots, captures = {a for a, _ in pairs}, {b for _, b in pairs}
        cone = select_ecp5_data_cones(graph, roots=roots, captures=captures)
        measurements = run_ecp5_pair_checks(cone, Path(output_dir), pairs=pairs,
            launches_ns={pin: 0.0 for pin in roots}, deadlines_ns={pin: 0.0 for pin in captures},
            executable=sta, analysis='max')
        if set(measurements) != pairs:
            raise ValidationError('snapshot segment native pair coverage mismatch')
    timed, untimed = {}, {}
    for launch, capture, source, targets, kind in connections:
        key = (population['board'], launch, capture)
        if not targets:
            untimed[key] = kind
            continue
        # All reachable D/CE/synchronous-LSR targets are retained. Taking
        # independent maxima gives a conservative bound, not an assertion that
        # the largest delay and largest setup belong to the same pin/path.
        setup = []
        for target in targets:
            record = graph['captures'][target]
            if record.get('kind') != 'ff' or set(record.get('setuphold', {})) != {'posedge', 'negedge'}:
                raise ValidationError('snapshot segment needs physical FF setup bounds')
            setup.extend(record['setuphold'][edge][0][2] for edge in ('posedge', 'negedge'))
        timed[key] = dict(delay_ns=max(measurements[source, target]['arrival_ns'] for target in targets),
                          setup_ns=max(setup), measurement='routed_boundary_cone_upper_bound',
                          physical_target_count=len(targets))
    return dict(timed=timed, untimed=untimed, native_physical_pairs=len(pairs),
                source_relations=len(connections), global_timing_qualified=False)
