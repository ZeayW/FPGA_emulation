"""Bounded projection facts for independent timing/coverage unit tests.

These facts cannot run a flow, select routes or schedule hops. Production
projection and validation are exercised using current typed inputs separately.
"""
import hashlib
import json


def path_facts(routes):
    nets = {row['net']: row for row in routes['routes']}
    paths = routes.get('timing', {}).get('paths', [])
    assert len(paths) <= 4096
    result = []
    for path in paths:
        cuts = []
        for index, net in enumerate(path['cut_nets']):
            route = nets[net]
            transition = path['cut_transitions'][index] if 'cut_transitions' in path else {
                'from': route['source'], 'to': route['sinks'][0]}
            cuts.append(dict(cut=f"{path['path']}:cut:{index}", index=index,
                logical_net=net, demand=route['id'], **{'from':transition['from'], 'to':transition['to']}))
        result.append(dict(path=path['path'], members=path.get('compressed_path_ids', [path['path']]),
            clock_domain=path['clock_domain'], required_time_ns=path.get('required_time_ns',path.get('clock_period_ns')),
            estimated_logic_delay_ns=path['fixed_delay_ns'], cuts=cuts))
    return sorted(result, key=lambda row: row['path'])


def coverage_contract_facts(routes, schedule=None, semantic_contract=None):
    """Encode explicit old-shape facts accepted only by bounded coverage tests."""
    from emuflow.cross_layer_timing import _canonical_sha256
    exact = semantic_contract is not None
    assert len(routes['routes']) <= 4096
    segments = [] if not exact or semantic_contract.get('indexed') else [
        dict(segment=row['id'], **{key:row[key] for key in ('kind','fpga','source_semantics') if key in row})
        for row in semantic_contract['logic_segments']]
    paths = path_facts(routes)
    value = dict(schema='emuflow.cross-layer-timing/v2', provider='provider-neutral-route-schedule-binding-v2',
        design=routes['design'], platform=routes['platform'],
        transport_semantics='sampled-virtual-wire' if exact else 'registered-boundary',
        route_binding=dict(provider=routes.get('provider'), sha256=_canonical_sha256(routes)),
        path_bindings=paths, logic_segment_bindings=sorted(segments,key=lambda row:row['segment']),
        metrics=dict(paths=len(paths), path_cut_bindings=sum(len(row['cuts']) for row in paths),
            logic_segments=len(segments), scheduled_hops=0 if schedule is None else len(schedule['entries'])))
    if exact:
        value['semantic_binding'] = dict(schema=semantic_contract['schema'], sha256=routes['semantic_contract_sha256'])
        if semantic_contract.get('indexed'):
            value['semantic_binding']['storage'] = 'phase3-assignment-index'
            value['logic_segment_binding'] = dict(mode='phase3-assignment-index', semantic_contract_sha256=routes['semantic_contract_sha256'])
    if schedule is not None:
        value['hop_binding'] = dict(mode='canonical-route-schedule-merge-v1', scheduled_hops=len(schedule['entries']))
        value['schedule_binding'] = dict(provider=schedule.get('provider'),sha256=_canonical_sha256(schedule))
    return value
