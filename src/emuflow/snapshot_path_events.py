"""Bind an original source path to its actual finite-trace transport epochs.

Work backwards from commit. A later round's snapshot can only depend on
remote words received before that snapshot's capture edge, not on whichever
transfer happens to carry the same net in the final round. No delay or slack
is calculated here and no previous-macrocycle word is accepted as current.
"""
from bisect import bisect_left
import json
import math

from .errors import ValidationError


def iter_snapshot_path_events(paths, pair, protocol, *, initial_launch_ns):
    """Yield each original member/cycle with causally selected segment edges.

    Consumes source bindings and a checked protocol stream. Initial state
    readiness remains explicit and conditional, as in local setup binding.
    """
    if protocol.get('scope') != 'simulated_protocol_events_not_measured_link':
        raise ValidationError('path events require bound simulated protocol events')
    exports, imports = {}, {}
    for board in ('board0', 'board1'):
        interface = pair['boards'][board]['interface']
        exports[board] = {name: index for index, name in enumerate(interface['exported_nets'])}
        imports[board] = {name: index for index, name in enumerate(interface['imported_nets'])}
        if len(exports[board]) != len(interface['exported_nets']) or len(imports[board]) != len(interface['imported_nets']):
            raise ValidationError('duplicate snapshot wire bit identity')
    cycles = []
    previous = None
    for cycle in protocol['cycles']:
        groups = {}
        for transfer in cycle['transfers']:
            key = (transfer['source'], transfer['target'], transfer['word'])
            groups.setdefault(key, []).append(transfer)
        index = {}
        for key, rows in groups.items():
            rows.sort(key=lambda row: row['update_ns'])
            index[key] = ([row['update_ns'] for row in rows], rows)
        cycles.append((cycle, previous, index))
        previous = cycle
    seen = set()
    for path in paths:
        if path['path'] in seen:
            raise ValidationError('duplicate original member in path event binding')
        seen.add(path['path'])
        segments = path['segments']
        if not segments or segments[-1]['outgoing_cut'] is not None:
            raise ValidationError('path binding lacks terminal capture')
        for cycle, previous, index in cycles:
            target = cycle['commits_ns'][segments[-1]['board']]
            selected = [None] * (len(segments) - 1)
            for k in range(len(segments) - 2, -1, -1):
                source, receiver = segments[k], segments[k+1]
                net = source['outgoing_cut']
                bit = exports[source['board']].get(net)
                if bit is None or imports[receiver['board']].get(net) != bit:
                    raise ValidationError('original path cut does not match wire bit identity')
                times, rows = index.get((source['board'], receiver['board'], bit // 32), ([], []))
                position = bisect_left(times, target) - 1
                if position < 0:
                    raise ValidationError('original path requires stale or unavailable current-cycle shadow')
                selected[k] = rows[position]
                target = rows[position]['capture_ns']
            start = segments[0]
            kind = json.loads(start['launch'])[0]
            if kind == 'host':
                if start['board'] != 'board0':
                    raise ValidationError('unsupported remote host launch')
                launch = cycle['host_latch_ns']
            elif kind == 'state':
                if previous is not None:
                    launch = previous['commits_ns'][start['board']]
                else:
                    launch = initial_launch_ns.get(start['launch'])
                    if (type(launch) not in (int, float) or not math.isfinite(launch) or
                            launch < protocol['reset_release_ns'][start['board']]):
                        raise ValidationError('original path lacks explicit initial state readiness')
            else:
                raise ValidationError('original path begins at an intermediate boundary')
            events = []
            for k, segment in enumerate(segments):
                transfer = selected[k] if k < len(selected) else None
                capture = transfer['capture_ns'] if transfer else cycle['commits_ns'][segment['board']]
                if not launch < capture:
                    raise ValidationError('original path snapshot precedes current source launch')
                events.append(dict(segment_index=k, launch_ns=launch, capture_ns=capture,
                    transfer_epoch=transfer['epoch'] if transfer else None))
                if transfer:
                    launch = transfer['update_ns']
            yield dict(path=path['path'], cycle=cycle['cycle'], segment_events=events,
                scope='original_member_observed_event_binding_not_global_timing')
    if not seen or not cycles:
        raise ValidationError('empty original path or protocol population')
