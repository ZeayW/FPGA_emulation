"""Independent original endpoint reachability for snapshot global binding.

This is a structural coverage gate, not enumeration of every logic path or
timing analysis. Reconvergent paths with the same endpoints remain distinct
timing obligations for a later consumer; their TNS must not be inferred here.
"""
from collections import defaultdict, deque

from .errors import ValidationError
from .snapshot_timing_population import build_snapshot_timing_population, required_snapshot_connections


def qualify_snapshot_global_reachability(ir, assignment, *, port_owners):
    """Compare unsplit-source reachability with actual two-board composition.

    Only exported-cut to imported-cut boundaries bridge boards. State capture
    never connects to state launch: otherwise sequential feedback would be
    incorrectly traversed as combinational logic. No physical report chooses
    the required endpoint population.
    """
    original = build_snapshot_timing_population(ir,
        {cell['id']: 'board0' for cell in ir.value['instances']}, board='board0',
        port_owners={name: 'board0' for name in port_owners})
    local = {board: build_snapshot_timing_population(ir, assignment, board=board,
        port_owners=port_owners) for board in ('board0', 'board1')}
    labels = original['launch_labels']
    label_index = {name: index for index, name in enumerate(labels)}
    outgoing, indegree, masks, crossing_masks = defaultdict(list), {}, {}, {}
    original_launches, original_captures, cuts = {}, {}, defaultdict(dict)
    for board, population in local.items():
        for role in ('launches', 'captures'):
            for key, boundary in population[role].items():
                node = (board, role, key)
                indegree[node] = 0
                if boundary['kind'] == 'cut':
                    if role in cuts[key]:
                        raise ValidationError('duplicate cross-board cut role')
                    cuts[key][role] = node
                else:
                    endpoints = original_launches if role == 'launches' else original_captures
                    if key in endpoints:
                        raise ValidationError('duplicate original endpoint owner')
                    endpoints[key] = node
        for source, sink in required_snapshot_connections(population):
            outgoing[(board, 'launches', source)].append(((board, 'captures', sink), False))
    if set(original_launches) != set(original['launches']) or set(original_captures) != set(original['captures']):
        raise ValidationError('split source does not cover original endpoint population')
    for key, roles in cuts.items():
        if set(roles) != {'launches', 'captures'} or roles['launches'][0] == roles['captures'][0]:
            raise ValidationError('cut lacks exactly one opposite-board source and receiver')
        outgoing[roles['captures']].append((roles['launches'], True))
    for targets in outgoing.values():
        for target, _ in targets:
            indegree[target] += 1
    for key, node in original_launches.items():
        masks[node] = 1 << label_index[key]
    ready = deque(node for node, degree in indegree.items() if degree == 0)
    visited = 0
    while ready:
        node = ready.popleft()
        visited += 1
        for target, crossing in outgoing[node]:
            masks[target] = masks.get(target, 0) | masks.get(node, 0)
            crossing_masks[target] = crossing_masks.get(target, 0) | (
                masks.get(node, 0) if crossing else crossing_masks.get(node, 0))
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if visited != len(indegree):
        raise ValidationError('cross-board combinational boundary cycle')
    observed = {key: masks.get(node, 0) for key, node in original_captures.items()}
    if observed != original['capture_masks']:
        raise ValidationError('cross-board composition changes original endpoint reachability')
    crossing = {key: crossing_masks.get(node, 0) for key, node in original_captures.items()}
    return dict(scope='unsplit_source_endpoint_reachability_not_enumerated_path_timing',
        status='pass', launch_labels=labels, capture_masks=observed,
        crossing_capture_masks=crossing,
        endpoint_pairs=sum(bin(mask).count('1') for mask in observed.values()),
        crossing_endpoint_pairs=sum(bin(mask).count('1') for mask in crossing.values()),
        cut_bridges=len(cuts), global_timing_qualified=False,
        original_path_tns_qualified=False)
