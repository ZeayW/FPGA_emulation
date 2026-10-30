"""Small raw documents enter native pin validators through file-backed fixtures."""
from contextlib import contextmanager, ExitStack
from pathlib import Path
import json
import tempfile

from emuflow.transport.pin_artifacts_native import validate_pin_artifacts, audit_electrical_map
from tests.pin_plan_storage_fixture import write_pin_plan_storage
from tests.position_hints_storage_fixture import write_position_hints_storage
from tests.chimew_binding_storage_fixture import write_chimew_binding_storage
from emuflow.transport.chimew.chimew_bank_channel_storage import write_chimew_bank_channel_storage


@contextmanager
def paths(schedule, plan, positions=None, binding=None):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        schedule_path = getattr(schedule, 'manifest_path', None)
        if schedule_path is None:
            schedule_path = root / 'schedule.json'
            from tests.schedule_store_fixture import write_tdm_schedule_storage
            write_tdm_schedule_storage(schedule_path, schedule)
        plan_path = getattr(plan, 'manifest_path', None)
        if plan_path is None:
            plan_path = write_pin_plan_storage(root / 'plan.json', plan).manifest_path
        optional = {}
        if positions is not None:
            value = getattr(positions, 'manifest_path', None)
            if value is None:
                value = write_position_hints_storage(root / 'positions.json', positions).manifest_path
            optional['positions_path'] = value
        if binding is not None:
            value = getattr(binding, 'manifest_path', None)
            if value is None:
                value = write_chimew_binding_storage(root / 'binding.json', binding).manifest_path
            optional['binding_path'] = value
        yield schedule_path, plan_path, optional


def validate_pin_plan(schedule, platform, positions, plan):
    with paths(schedule, plan, positions=positions) as (source, planned, optional):
        return validate_pin_artifacts(source, platform, planned, **optional)['pin_validation']


def validate_chimew_phase6_binding(schedule, platform, pin_plan, binding):
    with paths(schedule, pin_plan, binding=binding) as (source, planned, optional):
        return validate_pin_artifacts(source, platform, planned, **optional)['binding_validation']


def validate_chimew_electrical_map(document, platform, problem):
    def point(value):
        return {'x': value[0], 'y': value[1]}
    domains = problem['domains']
    def direction(value):
        return ('a_to_b', 'b_to_a', 'bidirectional')[value]
    banks = []
    for bank in problem['banks']:
        banks.append({'id': bank['id'], 'domain': domains[bank['domain']]['id'],
            'bank_a': {'id': bank['bank_a']['id'], **point(bank['bank_a']['point'])},
            'bank_b': {'id': bank['bank_b']['id'], **point(bank['bank_b']['point'])},
            'channels': [{'id': c['id'], 'order': c['order'], 'pin_a': point(c['pin_a']), 'pin_b': point(c['pin_b'])} for c in bank['channels']]})
    groups = []
    for group in problem['groups']:
        groups.append({'id': group['id'], 'domain': domains[group['domain']]['id'],
            'kind': ('tdm_group', 'common_signal')[group['kind']], 'direction': direction(group['direction']),
            'members': [{'id': m['id'], 'direction': direction(m['direction']), 'fanout': point(m['fanout']),
                         'fanins': [point(p) for p in m['fanins']], 'timing_weight': m.get('timing_weight', 1.0)} for m in group['members']]})
    raw = {'schema': 'emuflow.chimew-bank-channel-input/v2', 'provider': 'source-qualified-physical-bank-channel-v1',
           'design': problem['design'], 'platform': problem['platform'], 'coordinate_system': 'physical-site-xy',
           'cost_quantization_per_site': int(problem['cost_scale']), 'provenance': problem['provenance'],
           'domains': domains, 'bank_pairs': banks, 'groups': groups,
           'metrics': {k: problem['metrics'][k] for k in ('groups', 'signals', 'fanins', 'bank_pairs', 'channels')}}
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        electrical = root / 'electrical.json'; electrical.write_text(json.dumps(document))
        with write_chimew_bank_channel_storage(root / 'input.json', raw) as bank:
            return audit_electrical_map(bank.manifest_path, electrical, platform)
