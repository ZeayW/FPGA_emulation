"""Observed protocol edge bindings for independent physical STA qualification.

This binds clocks/events, not propagation delays or slack. It does not turn
UART transmission into a combinational timing arc. Initial storage readiness
is an explicit conditional assumption, not inferred reset recovery/removal.
"""
from bisect import bisect_left
import math

from .errors import ValidationError


def iter_snapshot_timing_windows(population, interface, protocol, *, initial_ready_ns):
    """Yield capture groups sharing one edge and a complete local launch map.

    State launches use the preceding DUT commit; cut launches use the latest
    preceding update of the actual imported word; host launches use the held
    input latch. Same-edge register updates are deliberately excluded: the
    receiving capture sees the previous value on that edge. The caller adds
    physical CQ, setup and skew through the native STA adapter, not here.
    This is trace-conditional setup binding, not a hold or global timing proof.
    """
    board = population['board']
    if board not in ('board0', 'board1') or interface['board'] != board:
        raise ValidationError('snapshot timing window board mismatch')
    if protocol.get('scope') != 'simulated_protocol_events_not_measured_link':
        raise ValidationError('snapshot windows require bound protocol events')
    initial_kinds = {key for key, value in population['launches'].items() if value['kind'] in ('state', 'cut')}
    if set(initial_ready_ns) != initial_kinds:
        raise ValidationError('explicit complete initial storage readiness required')
    reset = protocol['reset_release_ns'][board]
    for time in initial_ready_ns.values():
        if type(time) not in (int, float) or not math.isfinite(time) or time < reset:
            raise ValidationError('invalid conditional initial storage readiness')
    imports = {name: index // 32 for index, name in enumerate(interface['imported_nets'])}
    if len(imports) != len(interface['imported_nets']):
        raise ValidationError('duplicate imported timing net')
    history = {key: [initial_ready_ns[key]] if key in initial_kinds else []
               for key in population['launches']}
    state_events, host_events, updates, capture_events = [], [], {}, {}
    for cycle in protocol['cycles']:
        state_events.append(cycle['commits_ns'][board])
        if board == 'board0':
            host_events.append(cycle['host_latch_ns'])
        for transfer in cycle['transfers']:
            if transfer['target'] == board:
                updates.setdefault(transfer['word'], []).append(transfer['update_ns'])
            if transfer['source'] == board:
                event = (cycle['cycle'], transfer['epoch'])
                time = transfer['capture_ns']
                if event in capture_events and capture_events[event] != time:
                    raise ValidationError('word transfers disagree on snapshot capture edge')
                capture_events[event] = time
    for key, boundary in population['launches'].items():
        kind = boundary['kind']
        if kind == 'state':
            times = state_events
        elif kind == 'host':
            times = host_events
        elif kind == 'cut':
            word = imports.get(boundary['identity'])
            if word is None or word not in updates:
                raise ValidationError('missing imported word update binding')
            times = updates[word]
        else:
            raise ValidationError('unknown snapshot launch kind')
        history[key].extend(times)
        if any(a >= b for a, b in zip(history[key], history[key][1:])):
            raise ValidationError('unordered or premature snapshot storage event')
    cut_captures = tuple(key for key, b in population['captures'].items() if b['kind'] == 'cut')
    commit_captures = tuple(key for key, b in population['captures'].items() if b['kind'] in ('state', 'host'))
    if len(cut_captures) + len(commit_captures) != len(population['captures']):
        raise ValidationError('unknown snapshot capture kind')
    groups = [(time, cycle, epoch, cut_captures) for (cycle, epoch), time in capture_events.items()]
    groups.extend((cycle['commits_ns'][board], cycle['cycle'], None, commit_captures)
                  for cycle in protocol['cycles'])
    for edge, cycle, epoch, captures in sorted(groups, key=lambda row: row[0]):
        if not captures:
            continue
        launches = {}
        for key, times in history.items():
            index = bisect_left(times, edge) - 1
            if index < 0:
                raise ValidationError('capture lacks a strictly preceding storage readiness event')
            launches[key] = times[index]
        yield dict(board=board, cycle=cycle, epoch=epoch, capture_edge_ns=edge,
                   captures=captures, launches_ns=launches,
                   scope='trace_conditional_local_setup_event_binding')
