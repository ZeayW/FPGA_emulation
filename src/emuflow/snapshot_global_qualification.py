"""Explicit complete-source snapshot qualification using native numerical STA.

Structural members eliminated by proven Boolean independence or same-register
CE retention remain accounted for, without synthetic zero-delay paths. This
is a finite-trace, routed-cone-bound analysis, not measured asynchronous link
latency or unrestricted hardware qualification.
"""
import json
import math
from pathlib import Path

from .errors import ValidationError
from .snapshot_global_sta import build_snapshot_global_checks, run_snapshot_global_checks
from .snapshot_path_binding import iter_snapshot_path_bindings
from .snapshot_path_coverage import qualify_snapshot_path_coverage
from .snapshot_path_events import iter_snapshot_path_events
from .snapshot_segment_timing import measure_snapshot_segment_bounds
from .snapshot_sensitivity import qualify_snapshot_source_correspondence
from .snapshot_timing_population import build_snapshot_timing_population


def _classify_members(paths, timed, non_timed):
    numeric, classified = [], {}
    for path in paths:
        reasons = set()
        for segment in path['segments']:
            key = (segment['board'], segment['launch'], segment['capture'])
            if key in timed:
                if key in non_timed:
                    raise ValidationError('snapshot relation is both timed and nontimed')
                continue
            reason = non_timed.get(key)
            if reason == 'state_hold':
                if (len(path['segments']) != 1 or segment['launch'] != segment['capture'] or
                        json.loads(segment['launch'])[0] != 'state'):
                    raise ValidationError(f'retention is not an original same-register local member: {path["path"]}: {key!r}')
            elif reason != 'boolean_independent':
                raise ValidationError(f'original member has an unqualified missing physical segment: {path["path"]}: {key!r}')
            reasons.add(reason)
        if reasons:
            classified[path['path']] = sorted(reasons)
        else:
            numeric.append(path)
    return numeric, classified


def qualify_snapshot_global_timing(database, ir, assignment, *, pair, protocol,
        physical_models, port_owners, initial_launch_ns, cycle, setup_uncertainty_ns,
        output_dir, yosys, sta):
    """Join complete source members, measured cones and observed event epochs.

    `physical_models` owns each board's already parsed routed graph/bindings.
    Native SAT independently resolves missing Boolean influence here; callers
    cannot supply exception pass flags. OpenSTA alone computes numerical
    arrival/required/slack. Python only checks identities and aggregates engine
    scalars. The caller still owes local hold/reset/CDC and full-flow gates.
    """
    root = Path(output_dir)
    coverage = qualify_snapshot_path_coverage(database, ir, assignment, port_owners=port_owners)
    paths = list(iter_snapshot_path_bindings(database, ir, assignment, port_owners=port_owners))
    if set(physical_models) != {'board0','board1'}:
        raise ValidationError('snapshot global timing requires both physical boards')
    timed, non_timed, physical_summary = {}, {}, {}
    for board in ('board0','board1'):
        model = physical_models[board]
        population = build_snapshot_timing_population(ir, assignment, board=board, port_owners=port_owners)
        correspondence = qualify_snapshot_source_correspondence(ir, assignment, board=board,
            port_owners=port_owners,bindings=model['bindings'],graph=model['graph'],
            output_dir=root/board/'source-correspondence',yosys=yosys)
        independent = {(board,p['launch'],p['capture']) for p in correspondence['sensitivity_proofs']}
        measured = measure_snapshot_segment_bounds(population,model['bindings'],model['graph'],
            output_dir=root/board/'segments',sta=sta)
        unexplained = {key for key,kind in measured['untimed'].items() if kind == 'unexplained'}
        if unexplained != independent:
            raise ValidationError('native source proof does not cover exactly the missing segment population')
        timed.update(measured['timed'])
        non_timed.update({key: 'boolean_independent' if key in independent else kind
                          for key,kind in measured['untimed'].items()})
        physical_summary[board] = dict(native_pairs=measured['native_physical_pairs'],
            source_relations=measured['source_relations'],correspondence=correspondence)
    numeric, classified = _classify_members(paths,timed,non_timed)
    ids = {path['path'] for path in numeric}
    if not ids or len(ids)+len(classified) != coverage['original_members']:
        raise ValidationError('snapshot original member accounting is empty or incomplete')
    selected = dict(database,paths=[p for p in database['paths'] if p['id'] in ids])
    events = [event for event in iter_snapshot_path_events(numeric,pair,protocol,
              initial_launch_ns=initial_launch_ns) if event['cycle'] == cycle]
    checks = build_snapshot_global_checks(selected,numeric,events,timed,cycle=cycle,
                                          setup_uncertainty_ns=setup_uncertainty_ns)
    measurements = run_snapshot_global_checks(checks,root/'global',sta=sta)
    expected = {(c.path,c.role,c.event) for c in checks}
    if (len(measurements) != len(expected) or
            {(m['path'],m['role'],m['event']) for m in measurements} != expected or
            any(not math.isfinite(m[k]) for m in measurements for k in ('arrival_ns','required_ns','slack_ns'))):
        raise ValidationError('incomplete or invalid native global measurement population')
    metrics = {}
    for role in ('target','runtime'):
        rows = [m for m in measurements if m['role'] == role]
        worst = min(rows,key=lambda m:m['slack_ns'])
        metrics[role] = dict(wns_ns=worst['slack_ns'],tns_ns=math.fsum(min(0,m['slack_ns']) for m in rows),
            negative_paths=sum(m['slack_ns']<0 for m in rows),critical_path=worst['path'],paths=len(rows))
    failures = sum(m['slack_ns'] < -1e-3 for m in measurements if m['role'] in ('tx','commit'))
    return dict(scope='whole_source_population_finite_trace_routed_cone_bounds',
        status='readiness_violations' if failures else 'native_checks_complete',cycle=cycle,
        original_members=coverage['original_members'],numerically_timed_members=len(ids),
        nontimed_members=len(classified),
        nontimed_by_reason={reason:sum(reason in reasons for reasons in classified.values())
                           for reason in ('state_hold','boolean_independent')},
        nontimed_slack_policy='no_numeric_slack_or_zero_delay_arc',
        source_coverage=coverage,physical_segments=physical_summary,metrics=metrics,
        readiness_violations=failures,setup_uncertainty_ns=setup_uncertainty_ns,
        numerical_engine='OpenSTA',physical_delay_model='routed_boundary_cone_upper_bound',
        communication_model='observed_simulation_epochs_not_measured_or_worst_case_link_delay',
        global_timing_qualified=False,full_flow_qualified=False,hardware_qualified=False)
