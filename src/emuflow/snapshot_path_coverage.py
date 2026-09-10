"""Exact structural net-chain coverage, independent of STA path selection.

The population is conservative mapped LUT connectivity, not sensitizable
Boolean paths or rise/fall arc permutations. Parallel tied LUT inputs collapse
to the same ordered net chain, matching TimingPathDB's representation. Count
paths on the acyclic source graph without enumerating the missing population.
"""
from collections import defaultdict, deque

from .errors import ValidationError
from .snapshot_path_binding import iter_snapshot_path_bindings
from .snapshot_timing_population import boundary_id, build_snapshot_timing_population


def _source_path_graph(ir, port_owners):
    original = build_snapshot_timing_population(ir,
        {cell['id']: 'board0' for cell in ir.value['instances']}, board='board0',
        port_owners={name: 'board0' for name in port_owners})
    cells = {cell['id']: cell for cell in ir.value['instances']}
    outputs, inputs, successors = {}, defaultdict(set), defaultdict(set)
    counts, indegree, captures = {}, {}, {}
    for net in ir.value['nets']:
        if net['cut_class'] == 'clock':
            continue
        name = net['id']
        indegree[name] = 0
        driver = net['drivers'][0]  # Single driver was checked by population.
        instance = driver['instance']
        counts[name] = int(instance is None or cells[instance]['type'] == '$_DFF_P_')
        if instance is not None and cells[instance]['type'] != '$_DFF_P_':
            outputs[instance] = name
        for sink in net['sinks']:
            instance = sink['instance']
            if instance is None:
                captures[boundary_id('host', sink['port'], bit=sink['bit'])] = name
            elif cells[instance]['type'] == '$_DFF_P_':
                if sink['port'] != 'D' or sink['bit'] != 0:
                    raise ValidationError('non-data FF sink in snapshot source path population')
                captures[boundary_id('state', instance)] = name
            else:
                inputs[instance].add(name)
    for instance, sources in inputs.items():
        for source in sources:
            successors[source].add(outputs[instance])
    for targets in successors.values():
        for target in targets:
            indegree[target] += 1
    ready = deque(name for name, degree in indegree.items() if not degree)
    visited = 0
    while ready:
        name = ready.popleft()
        visited += 1
        for target in successors[name]:
            counts[target] += counts[name]
            indegree[target] -= 1
            if not indegree[target]:
                ready.append(target)
    if visited != len(indegree):
        raise ValidationError('combinational cycle in original path population')
    expected = {key: counts.get(captures.get(key), 0) for key in original['captures']}
    return successors, captures, expected


def qualify_snapshot_path_coverage(database, ir, assignment, *, port_owners):
    """Require every distinct original structural net chain exactly once.

    Validated, distinct members form a subset of source paths; equality of
    counts per capture proves coverage including reconvergence. Endpoint-pair
    equality alone cannot do that. This gate never calculates timing and does
    not authorize a sampled native extractor or prove physical correspondence.
    """
    _, _, expected = _source_path_graph(ir, port_owners)
    observed = defaultdict(int)
    seen = set()
    bindings = iter_snapshot_path_bindings(database, ir, assignment, port_owners=port_owners)
    for member, binding in zip(database['paths'], bindings):
        segments = binding['segments']
        capture = segments[-1]['capture']
        signature = (segments[0]['launch'], capture, tuple(member['path_nets']))
        if signature in seen:
            raise ValidationError('duplicate original structural net chain, even with a different member ID')
        seen.add(signature)
        observed[capture] += 1
    if set(observed) - set(expected) or any(observed[key] != count for key, count in expected.items()):
        raise ValidationError('incomplete original structural path population: '
            f'expected {sum(expected.values())} distinct net chains, received {len(seen)}; '
            'endpoint coverage or a non-saturated STA path limit is insufficient')
    return dict(status='pass', scope='all_original_structural_net_chains',
        original_members=len(seen), captures=len(expected),
        nonconstant_captures=sum(count > 0 for count in expected.values()),
        global_timing_qualified=False, physical_correspondence_qualified=False)
