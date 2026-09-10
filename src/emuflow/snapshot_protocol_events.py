"""Qualification-only binding of simulated snapshot protocol events.

The returned word transfers preserve observed capture/receive epochs; they
are not measured cable delays or a worst-case bound over clock phase/PVT.
Physical STA and source-path composition consume these epochs separately.
"""
import math

from .errors import ValidationError


def bind_snapshot_protocol_events(lines, pair, *, macrocycles):
    """Check complete event coverage and bind each received word to its capture.

    Input is the explicit RTL qualification stream, not a production log.
    Intermediate follower captures may be overwritten; only the last capture
    before DATA handoff supplies that word. No missing event gets a default.
    """
    rounds = pair['evaluation_rounds']
    if (type(macrocycles) is not int or macrocycles < 1 or
            type(rounds) is not int or rounds < 1 or macrocycles * rounds > 65535):
        raise ValidationError('invalid or exhausted snapshot event epoch range')
    words = {board: pair['boards'][board]['interface']['words'] for board in ('board0', 'board1')}
    if any(type(n) is not int or not 1 <= n <= 256 for n in words.values()) or len(set(words.values())) != 1:
        raise ValidationError('snapshot event binding requires matching wire word counts')
    epochs = macrocycles * rounds
    events = {}
    last_time = -1.0
    count = 0
    passed = False
    # Linear in this explicitly requested finite trace, not in logical paths.
    limit = 2 + epochs * (2 * (2 * words['board0'] + 2) + 3)
    for line in lines:
        if line.startswith('PASS snapshot '):
            if passed or not line.startswith(f'PASS snapshot macrocycles={macrocycles} observed_ff='):
                raise ValidationError('snapshot event completion does not match requested trace')
            passed = True
        if not line.startswith('SNAPSHOT_EVENT '):
            continue
        count += 1
        row = line.split()
        if passed or count > limit or len(row) != 7:
            raise ValidationError('malformed, excessive or post-completion snapshot events')
        _, board, timestamp, kind, epoch, round_index, word = row
        try:
            time = float(timestamp)
            epoch, round_index, word = int(epoch), int(round_index), int(word)
        except ValueError as exc:
            raise ValidationError('nonnumeric snapshot event') from exc
        if board not in words or not math.isfinite(time) or time < 0 or time < last_time:
            raise ValidationError('invalid snapshot event time/board/order')
        last_time = time
        if (kind not in ('reset_release', 'host_latch', 'tx_capture', 'tx_data', 'rx_update', 'commit') or
                not 0 <= epoch < epochs or round_index != epoch % rounds or
                not 0 <= word < words[board] or (kind not in ('tx_data', 'rx_update') and word != 0)):
            raise ValidationError('invalid snapshot event identity')
        events.setdefault((board, kind, epoch, word), []).append(time)
    if not passed:
        raise ValidationError('snapshot trace did not complete equivalence qualification')

    def take(board, kind, epoch, word=0, count=1):
        values = events.pop((board, kind, epoch, word), [])
        if len(values) != count or any(a >= b for a, b in zip(values, values[1:])):
            raise ValidationError(f'missing/duplicate/unordered snapshot {kind} event')
        return values

    reset = {board: take(board, 'reset_release', 0)[0] for board in words}
    result = []
    previous_commit = dict(reset)
    for cycle in range(macrocycles):
        first_epoch = cycle * rounds
        host = take('board0', 'host_latch', first_epoch)[0]
        if host <= previous_commit['board0']:
            raise ValidationError('host input was not latched after previous commit/reset')
        transfers = []
        received = {}
        for epoch in range(first_epoch, first_epoch + rounds):
            previous_received = dict(received)
            for board, peer in (('board0', 'board1'), ('board1', 'board0')):
                captures = take(board, 'tx_capture', epoch,
                    count=2 if board == 'board1' and epoch % rounds else 1)
                if captures[0] <= previous_commit[board] or (board == 'board0' and captures[0] <= host):
                    raise ValidationError('snapshot captured before local state/input epoch')
                if board in previous_received and captures[0] <= previous_received[board]:
                    raise ValidationError('next round capture precedes previous local shadow update')
                previous_sent = captures[-1]
                previous_update = -1.0
                for word in range(words[board]):
                    sent = take(board, 'tx_data', epoch, word)[0]
                    update = take(peer, 'rx_update', epoch, word)[0]
                    if not previous_sent < sent < update or update <= previous_update:
                        raise ValidationError('DATA transfer is not bound to a preceding snapshot')
                    previous_sent, previous_update = sent, update
                    received[peer] = max(received.get(peer, -1), update)
                    transfers.append(dict(source=board, target=peer, epoch=epoch,
                        round=epoch % rounds, word=word, capture_ns=captures[-1],
                        handoff_ns=sent, update_ns=update))
        commits = {board: take(board, 'commit', first_epoch + rounds - 1)[0] for board in words}
        if any(commits[board] <= received[board] for board in words):
            raise ValidationError('DUT commit precedes complete local remote snapshot')
        result.append(dict(cycle=cycle, host_latch_ns=host, commits_ns=commits, transfers=transfers))
        previous_commit = commits
    if events:
        raise ValidationError('unexpected/unconsumed snapshot events')
    return dict(scope='simulated_protocol_events_not_measured_link', cycles=result, reset_release_ns=reset,
        physical_timing_qualified=False, global_timing_qualified=False)
