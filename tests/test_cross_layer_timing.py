"""The cross-layer API accepts only the current native source-bound contract."""
import copy

import pytest

from emuflow.scheduling.cross_layer_timing import build_cross_layer_timing_contract, validate_cross_layer_timing_contract
from emuflow.common.errors import ValidationError
from tests.test_phase5_timing_native import _timed_case


@pytest.mark.parametrize('generation', [1, 2])
def test_old_cross_layer_contracts_are_retired(tmp_path, monkeypatch, generation):
    _, _, semantic, routes, schedule, _ = _timed_case(tmp_path, monkeypatch)
    current = build_cross_layer_timing_contract(routes, schedule, semantic)
    old = {**current, 'schema': f'emuflow.cross-layer-timing/v{generation}'}
    with pytest.raises(ValidationError, match='requires v3'):
        validate_cross_layer_timing_contract(routes, old, schedule, semantic)


@pytest.mark.parametrize('owner', ['routes', 'schedule', 'semantics'])
def test_inline_or_mixed_sources_cannot_reenable_python_projection(tmp_path, monkeypatch, owner):
    _, raw, semantic, routes, schedule, raw_schedule = _timed_case(tmp_path, monkeypatch)
    if owner=='routes':routes=raw
    elif owner=='schedule':schedule=raw_schedule
    else:semantic={**semantic,'indexed':False}
    with pytest.raises(ValidationError, match='current typed owners'):
        build_cross_layer_timing_contract(routes,schedule,semantic)


@pytest.mark.parametrize('field', ['route_binding', 'schedule_binding', 'path_binding', 'metrics'])
def test_current_contract_rejects_changed_bound_evidence(tmp_path, monkeypatch, field):
    _, _, semantic, routes, schedule, _ = _timed_case(tmp_path, monkeypatch)
    current = build_cross_layer_timing_contract(routes,schedule,semantic)
    bad = copy.deepcopy(current)
    if field=='metrics':bad[field]['scheduled_hops']+=1
    else:bad[field]['sha256']='0'*64
    with pytest.raises(ValidationError, match='does not match canonical'):
        validate_cross_layer_timing_contract(routes,bad,schedule,semantic)
    assert validate_cross_layer_timing_contract(routes,current,schedule,semantic)['status']=='pass'
