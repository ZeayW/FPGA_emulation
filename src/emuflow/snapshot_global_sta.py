"""Export original snapshot member observations to the shared native engine.

Inputs are source/event bindings and physical segment cone bounds. No UART
delay estimate, frontend slack, or Python-composed arrival enters the checks.
This adapter does not prove the completeness/provenance of upstream inputs.
"""
import math
from pathlib import Path

from .errors import ValidationError
from .global_sta import EventCheck, validate_checks, run_event_checks


def build_snapshot_global_checks(database, paths, events, segment_timing, *, cycle,
                                 setup_uncertainty_ns):
    if type(cycle) is not int or cycle < 0:
        raise ValidationError('snapshot global STA requires an explicit cycle')
    if (type(setup_uncertainty_ns) not in (int, float) or
            not math.isfinite(setup_uncertainty_ns) or setup_uncertainty_ns < 0):
        raise ValidationError('snapshot global STA requires explicit uncertainty')
    originals = {path['id']: path for path in database['paths']}
    if len(originals) != len(database['paths']):
        raise ValidationError('duplicate original database member')
    bindings = {}
    for path in paths:
        if path['path'] in bindings:
            raise ValidationError('duplicate snapshot source member')
        bindings[path['path']] = path
    observed = {}
    for event in events:
        if event['cycle'] == cycle:
            if event['path'] in observed:
                raise ValidationError('duplicate snapshot member/cycle observation')
            observed[event['path']] = event
    if not originals or set(originals) != set(bindings) or set(originals) != set(observed):
        raise ValidationError('snapshot global STA requires every original database member exactly once')
    checks = []
    for name, original in originals.items():
        segments = bindings[name]['segments']
        clocks = observed[name]['segment_events']
        if not segments or [event['segment_index'] for event in clocks] != list(range(len(segments))):
            raise ValidationError('snapshot global STA segment event coverage mismatch')
        origin = clocks[0]['launch_ns']
        period = original['clock_period_ns']
        if type(period) not in (int, float) or not math.isfinite(period) or period <= 0:
            raise ValidationError('snapshot original target period is invalid')
        for index, (segment, clock) in enumerate(zip(segments, clocks)):
            key = (segment['board'], segment['launch'], segment['capture'])
            physical = segment_timing.get(key)
            if physical is None:
                raise ValidationError('snapshot global STA missing physical segment bound; no zero-delay fallback')
            delay, setup = physical['delay_ns'], physical['setup_ns']
            if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (delay, setup)) or delay < 0):
                raise ValidationError('snapshot physical segment has invalid delay/setup')
            launch = clock['launch_ns'] - origin
            required = clock['capture_ns'] - origin - setup - setup_uncertainty_ns
            last = index == len(segments)-1
            checks.append(EventCheck(name, 'commit' if last else 'tx', f'segment-{index}',
                                     launch, (delay,), required))
            if last:
                checks.append(EventCheck(name, 'target', 'capture', launch, (delay,),
                                         period - setup - setup_uncertainty_ns))
                checks.append(EventCheck(name, 'runtime', 'capture', launch, (delay,), required))
    return validate_checks(checks)


def run_snapshot_global_checks(checks, directory, *, sta):
    """Native-only numerical execution; no implicit Python arc cross-check.

    Results still need original-population, physical and protocol acceptance.
    Per-cycle observations must not be pooled as duplicate original TNS paths.
    """
    return run_event_checks(checks, Path(directory), executable=sta, verify_arcs=False)
