"""Whole-stage parity and failure boundaries for native Phase 5 ownership."""
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow_tools.stage_clients.schedule import validate_phase5_generation
from emuflow_tools.stage_clients.schedule_native import run_native_phase5
from emuflow.scheduling.tdm import build_native_exact_schedule_request
from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
from tests.native_schedule_case import _case
from tests.schedule_reference import build_tdm_schedule
from tests.test_native_schedule_generation import install_runtime


def current_case(tmp_path, affine=False, repeated=False):
    from emuflow.common.io import read_json
    from emuflow.routing.system_route_storage import read_system_routes
    from tests.native_route_seal_fixture import write_route_store_fixture

    case, legacy = _case(tmp_path, affine, repeated)
    contract = legacy["semantic_contract"]
    value = read_json(tmp_path / "routes.json")
    value.pop("semantic_contract", None)
    path = tmp_path / "current/routes.json"
    write_route_store_fixture(path, value)
    persisted = read_system_routes(path)
    # Phase 3 semantic ownership is an in-memory bounded overlay, not another
    # persisted copy in the RouteStore. Keep the original reader generation.
    routes = {**persisted, "semantic_contract": contract}
    producer = build_native_exact_schedule_request(routes, case.platform, source_bindings=persisted.bound_store_sources())
    return case, routes, producer


@pytest.mark.parametrize("affine,repeated", [(False, False), (True, False), ("banks", False), ("tree", False), (True, True)])
def test_whole_native_stage_matches_reference_and_has_one_production_owner(tmp_path, monkeypatch, affine, repeated):
    case, routes, producer = current_case(tmp_path, affine, repeated)
    expected = build_tdm_schedule(routes, case.platform)
    install_runtime(tmp_path, monkeypatch)
    output = tmp_path / "phase5"
    with patch("emuflow.scheduling.tdm_schedule_validation.validate_native_exact_schedule", side_effect=AssertionError("Python validation lifecycle")), \
         patch("emuflow.scheduling.phase5_timing_native.project_native_schedule_timing", side_effect=AssertionError("Python timing lifecycle")):
        report = run_native_phase5(producer, output)
    assert report["phase_implementation"] == "native-exact-phase5-v1"
    assert read_tdm_schedule(output / "schedule.json", materialize=True) == expected
    assert validate_phase5_generation(output / "phase5_report.json")["status"] == "pass"
    assert report["validation"]["collisions"] == 0
    assert not list(output.glob(".phase5-generation-*"))
    assert not list(tmp_path.glob(".phase5-request-*"))
    assert {p.name for p in output.iterdir()} == set(report["artifacts"].values())


@pytest.mark.parametrize("damage", ["crc", "version", "truncated", "trailing", "missing-route", "replaced-source"])
def test_stage_request_rejection_keeps_prior_complete_generation(tmp_path, monkeypatch, damage):
    from emuflow_tools.stage_clients import schedule_native as phase5_native
    from emuflow.common.native_protocol import CheckedRequestWriter

    case, routes, producer = current_case(tmp_path)
    install_runtime(tmp_path, monkeypatch)
    output = tmp_path / "phase5"
    run_native_phase5(producer, output)
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    original = phase5_native._write_inputs

    def corrupt(value, root):
        inputs = original(value, root)
        path = Path(inputs[-1])
        if damage in {"missing-route", "replaced-source"}:
            sources = list(value.source_bindings)
            if damage == "missing-route":
                sources = [s for s in sources if s.path != value.route_store]
            writer = CheckedRequestWriter(path)
            try:
                writer.put("QII", 0x31535035464D5545, 1, len(sources))
                for source in sources:
                    writer.text(str(source.path)); writer.text(source.schema)
                    generation = list(source.generation)
                    if damage == "replaced-source": generation[1] += 1
                    writer.put("3Q4q", *generation)
                writer.finish()
            finally:
                writer.close()
        else:
            payload = bytearray(path.read_bytes())
            if damage == "crc": payload[-1] ^= 1
            elif damage == "version": payload[8] = 99
            elif damage == "truncated": payload = payload[:-3]
            else: payload += b"x"
            path.write_bytes(payload)
        return inputs

    monkeypatch.setattr(phase5_native, "_write_inputs", corrupt)
    with pytest.raises(ValidationError): run_native_phase5(producer, output)
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before
    assert validate_phase5_generation(output / "phase5_report.json")["status"] == "pass"


def test_stage_report_preserves_unicode_and_boolean_configuration(tmp_path, monkeypatch):
    case, routes, producer = current_case(tmp_path)
    producer = replace(producer, header={**producer.header, "diagnostic": {"name": "布局\u2603", "enabled": True, "disabled": False, "value": None}})
    install_runtime(tmp_path, monkeypatch)
    report = run_native_phase5(producer, tmp_path / "phase5")
    actual = read_tdm_schedule(tmp_path / "phase5/schedule.json", materialize=True)
    assert actual["diagnostic"] == producer.header["diagnostic"]
    assert report["status"] == "pass"
    assert validate_phase5_generation(tmp_path / "phase5/phase5_report.json")["status"] == "pass"


def test_repeated_stage_keeps_the_published_immutable_store_generation(tmp_path, monkeypatch):
    from emuflow.common.native_tools import native_file_generation

    case, routes, producer = current_case(tmp_path)
    install_runtime(tmp_path, monkeypatch)
    output = tmp_path / "phase5"
    first = run_native_phase5(producer, output)
    store = output / first["artifacts"]["schedule_store"]
    generation = native_file_generation(store)
    borrowed = read_tdm_schedule(output / "schedule.json")
    second = run_native_phase5(producer, output)
    assert second["artifacts"]["schedule_store"] == store.name
    assert native_file_generation(store) == generation
    assert len(borrowed["entries"]) == first["schedule_storage"]["records"]["entries"]
    assert validate_phase5_generation(output / "phase5_report.json")["status"] == "pass"


@pytest.mark.parametrize("report", [None, {"schema": "emuflow.phase5-report/v1", "status": "pass"}])
def test_unsealed_generation_is_rejected_before_opening_schedule(tmp_path, monkeypatch, report):
    import json
    path = tmp_path / "phase5_report.json"
    if report is not None:
        path.write_text(json.dumps(report))
    with pytest.raises(ValidationError, match="commit report is missing|generation seal is missing"):
        validate_phase5_generation(path)
