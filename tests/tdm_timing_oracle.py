"""Independent, bounded fixture arithmetic for scheduled route/path delays.

No runner, store index, cache, publication or fallback lives here. Native Phase
5 owns production timing projection. These equations cover at most 4096 fixture
routes, hops and original path members.
"""
from __future__ import annotations
import math
from emuflow.common.errors import ValidationError
from emuflow.routing.routing import normalize_route_constraints, route_link_delay_coefficients_ns, route_link_delay_ns

_LIMIT = 4096


def _small(records):
    if not hasattr(records, '__len__') or len(records) > _LIMIT:
        raise ValidationError('timing oracle is limited to small test fixtures')
    return list(records)


def path_facts(routes, platform, schedule):
    """Evaluate concrete selected tree paths and their fixed-slot/ratio events."""
    route_records = _small(routes['routes'])
    entries = _small(schedule['entries'])
    timing = routes.get('timing', {})
    paths = _small(timing.get('paths', []))
    if sum(len(row.get('compressed_path_ids', [row['path']])) for row in paths) > _LIMIT or sum(len(r['tree_edges']) for r in route_records) > _LIMIT:
        raise ValidationError('timing oracle is limited to small test fixtures')
    normalization = timing.get('normalization')
    if paths and not isinstance(normalization, dict):
        raise ValidationError('timing oracle requires timing normalization')
    constraints = normalize_route_constraints(routes.get('constraints'), platform)
    links = {link.id: link for link in platform.links}
    by_net, hops = {}, {}
    for route in route_records:
        if route['net'] in by_net:
            raise ValidationError('duplicate route net')
        parents = {}
        for edge in route['tree_edges']:
            if edge['to'] in parents or edge['to'] == route['source']:
                raise ValidationError('duplicate tree parent')
            parents[edge['to']] = edge
        by_net[route['net']] = route, parents
    for entry in entries:
        key = entry['demand'], entry['link'], entry['from'], entry['to']
        if key in hops:
            raise ValidationError('duplicate scheduled hop')
        hops[key] = entry

    def tree_path(route, parents, sink):
        result, seen = [], set()
        while sink != route['source']:
            if sink not in parents or sink in seen:
                raise ValidationError('unreachable sink or route cycle')
            seen.add(sink)
            edge = parents[sink]
            result.append(edge)
            sink = edge['from']
        return result[::-1]

    def delay(route, edge):
        key = route['id'], edge['link'], edge['from'], edge['to']
        if key not in hops:
            raise ValidationError('timing oracle is missing routed hop')
        entry, link = hops[key], links[edge['link']]
        base = route_link_delay_ns(platform, *key[1:], constraints)
        _, slope = route_link_delay_coefficients_ns(platform, *key[1:], constraints)
        if link.tdm_delay_model is not None:
            ratio = entry.get('tdm_ratio')
            if type(ratio) is not int or ratio < 1:
                raise ValidationError('ratio-delay schedule entry requires a positive tdm_ratio')
            return base+slope*(ratio-1), base, slope, entry, link
        return base, base, slope, entry, link

    result = []
    for path in paths:
        nets = path.get('cut_nets')
        if not isinstance(nets, list) or not nets or any(net not in by_net for net in nets):
            raise ValidationError('timing oracle has unknown or invalid cut nets')
        transitions = path.get('cut_transitions')
        if transitions is None:
            transitions = []
            for net in nets:
                route, parents = by_net[net]
                sink = min(route['sinks'], key=lambda target: (-sum(delay(route,e)[0] for e in tree_path(route,parents,target)), target))
                transitions.append(dict(net=net, **{'from':route['source'],'to':sink}))
        elif (not isinstance(transitions, list) or len(transitions) != len(nets) or
                any(a.get('to') != b.get('from') for a,b in zip(transitions,transitions[1:]))):
            raise ValidationError('discontinuous member partition chain')
        period = path['clock_period_ns']
        required, fixed = path.get('required_time_ns',period), path['fixed_delay_ns']
        if any(type(value) not in (int,float) or not math.isfinite(value) for value in (period,required,fixed)) or period <= 0 or required <= 0 or fixed < 0:
            raise ValidationError('invalid timing values')
        arrival, records, seen = 0.0, [], set()
        for net, transition in zip(nets,transitions):
            route, parents = by_net[net]
            if transition.get('net') != net or transition.get('from') != route['source'] or transition.get('to') not in route['sinks']:
                raise ValidationError('invalid cut_transitions')
            for edge in tree_path(route,parents,transition['to']):
                key = route['id'],edge['link'],edge['from'],edge['to']
                if key in seen:
                    raise ValidationError('duplicate routed hop')
                seen.add(key)
                declared, base, slope, entry, link = delay(route,edge)
                wait = entry['slot']-entry['ready_slot']
                if wait < 0:
                    raise ValidationError('negative wait')
                slot, tx = 1000.0/link.fabric_clock_mhz, float(entry['slot'])*(1000.0/link.fabric_clock_mhz)
                if link.tdm_delay_model is not None:
                    hop_delay, arrival = declared, arrival+declared
                else:
                    hop_delay, arrival = base+slot*wait, max(arrival,tx)+base
                record = dict(schedule_entry=entry['id'], demand=route['id'], link=edge['link'],
                    **{'from':edge['from'],'to':edge['to']}, tx_endpoint=f"__emuflow_tx_{entry['id']}",
                    rx_endpoint=f"__emuflow_rx_{entry['id']}",base_link_delay_ns=base,tdm_wait_slots=wait,
                    tdm_slot_ns=slot,link_tdm_delay_ns=hop_delay,fixed_slot_tx_time_ns=tx,projected_arrival_ns=arrival)
                if link.tdm_delay_model is not None:
                    record.update(tdm_ratio=entry['tdm_ratio'],ratio_delay_slope_ns=slope,timing_model='per-hop-ratio-delay-v1')
                records.append(record)
        value = float(fixed)+arrival
        slack = float(required)-value
        normalized = (slack*float(period)/(normalization['positive_slack_scale_ns']*normalization['max_clock_period_ns'])
                      if slack >= 0 else slack/(normalization['negative_slack_scale_ns']*float(period)))
        result.append(dict(path=path['path'],clock_domain=path['clock_domain'],clock_period_ns=float(period),
            required_time_ns=float(required),preplacement_fixed_delay_ns=float(fixed),transport_delay_ns=arrival,
            delay_ns=value,slack_ns=slack,normalized_slack=normalized,cut_nets=list(nets),
            _cut_demands=[by_net[net][0]['id'] for net in nets],compressed_path_ids=list(path.get('compressed_path_ids',[path['path']])),
            cut_transitions=[dict(t) for t in transitions],routed_hops=len(records),scheduled_hops=records))
    return result


def timing_paths(routes, platform, schedule):
    return [{key:value for key,value in row.items() if key != '_cut_demands'} for row in path_facts(routes,platform,schedule)]


def timing_summary(routes, platform, schedule):
    records = timing_paths(routes,platform,schedule)
    if not records:
        raise ValidationError('schedule timing reconstruction has no timing paths')
    records.sort(key=lambda row:(row['normalized_slack'],row['path']))
    worst, normalized = records[0],sorted(row['normalized_slack'] for row in records)
    return dict(status='pass',timing_model='per-hop-ratio-delay-v1' if any(link.tdm_delay_model is not None for link in platform.links) else 'projected-fixed-slot-event-v1',
        timing_paths=len(records),worst_path=worst['path'],worst_delay_ns=worst['delay_ns'],worst_slack_ns=worst['slack_ns'],
        worst_normalized_slack=worst['normalized_slack'],negative_slack_paths=sum(row['slack_ns']<0 for row in records),
        total_negative_slack_ns=sum(min(0.0,row['slack_ns']) for row in records),
        total_negative_normalized_slack=sum(min(0.0,row['normalized_slack']) for row in records),
        p01_normalized_slack=normalized[len(normalized)//100],median_normalized_slack=normalized[len(normalized)//2])
