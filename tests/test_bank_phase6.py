from tests.pin_planning_reference import validate_bank_pin_assignment
from tests.pin_contract_fixture import validate_pin_plan
"""Phase 6 cannot turn direction-fixed banks into independently reusable wires."""

from tests.schedule_store_fixture import write_tdm_schedule_storage
import copy

import pytest

from emuflow.transport.chimew.chimew_phase6 import CHIMEW_PHASE6_BINDING_PROVIDER
from tests.chimew_pin_reference import _canonical_sha256
from tests.pin_contract_fixture import validate_chimew_phase6_binding
from emuflow.transport.chimew.chimew_fixed_wiring import FIXED_WIRING_BINDING_SCHEMA, FixedWiringChannels
from emuflow.platform.default_platform import research_platform_path
from emuflow.common.errors import ValidationError
from emuflow.transport.pin_planning import CHIMEW_PIN_PLAN_PROVIDER, PIN_PLAN_SCHEMA, SIGNAL_POSITION_HINTS_SCHEMA
from tests.pin_planning_reference import _assignment_metrics, _baseline_assignment, validate_bank_pin_assignment
from tests.pin_contract_fixture import validate_pin_plan
from tests.pin_plan_storage_fixture import write_pin_plan_storage
from emuflow.platform.platform import Platform
from emuflow.scheduling.tdm import TDM_SCHEDULE_SCHEMA
from emuflow.scheduling.tdm_schedule_storage import (canonical_tdm_schedule_sha256, read_tdm_schedule)


def fixture():
    platform = Platform.load(research_platform_path(2))
    link = platform.links[0]
    schedule = {
        "routes": [], "demand_completions": [], "domain_schedules": [],
        "schema": TDM_SCHEDULE_SCHEMA, "design": "banks", "platform": platform.name,
        "bank_allocations": [
            {"link": link.id, "from": source, "to": sink, "bank_count": count,
             "wires_per_bank": 20, "lane_offset": offset, "lanes": count * 20}
            for source, sink, count, offset in
            [("fpga0", "fpga1", 7, 0), ("fpga1", "fpga0", 11, 140)]
        ],
        "entries": [
            {"id": f"e{i}", "demand": f"d{i}", "net": f"n{i}",
             "hop": 0, "link": link.id, "from": source, "to": sink,
             "capacity_key": f"{link.id}:{source}:{sink}",
             "ready_slot": i, "arrival_slot": i + 1,
             "lane": lane, "slot": i, "tdm_ratio": 1}
            for i, (source, sink, lane) in enumerate(
                [("fpga0", "fpga1", 0), ("fpga1", "fpga0", 140)])
        ],
    }
    positions = {
        "schema": SIGNAL_POSITION_HINTS_SCHEMA, "design": "banks", "platform": platform.name,
        "provider": "openparf-lookahead-centroid-v1", "region_count": 1,
        "metrics": {"signals": 2, "endpoint_centroid_fallbacks": 0},
        "entries": [{"schedule_entry": f"e{i}", "source_y": .5, "sink_y": .5,
                     "source_region": 0, "sink_region": 0,
                     "source_fallback": False, "sink_fallback": False} for i in range(2)],
    }
    assignment = {"e0": (0, 19), "e1": (1, 159)}
    metrics = _assignment_metrics(schedule, platform, positions, assignment, 1, 1)
    baseline = _assignment_metrics(schedule, platform, positions,
                                   _baseline_assignment(schedule), 1, 1)
    plan = {
        "schema": PIN_PLAN_SCHEMA, "design": "banks", "platform": platform.name,
        "provider": CHIMEW_PIN_PLAN_PROVIDER, "configuration": {"refinement_iterations": 0},
        "weights": {"crossing": 1, "position": 1},
        "metrics": {"signals": 2, "groups": 2, **metrics,
                    **{f"baseline_logical_lane_{key}": value for key, value in baseline.items()}},
        "entries": [{"schedule_entry": f"e{i}", "group": i, "physical_lane": pin,
                     "logical_lane": lane}
                    for i, (lane, pin) in enumerate([(0, 19), (140, 159)])],
    }
    return platform, schedule, positions, plan


def test_valid_remapping_stays_inside_the_phase5_direction_banks():
    platform, schedule, positions, plan = fixture()
    assert validate_pin_plan(schedule, platform, positions, plan)["status"] == "pass"


def test_legacy_bank_validator_streams_schedule_rows_once():
    platform, schedule, _, plan = fixture()

    class SinglePassEntries(list):
        def __init__(self, values):
            super().__init__(values)
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("bank validation rescanned schedule rows")
            yield from super().__iter__()

    entries = SinglePassEntries(schedule["entries"])
    schedule["entries"] = entries
    validate_bank_pin_assignment(
        schedule,
        platform,
        {
            item["schedule_entry"]: item["physical_lane"]
            for item in plan["entries"]
        },
    )
    assert entries.iterations == 1


def test_indexed_validation_preserves_phase5_direction_banks(tmp_path):
    platform, schedule, positions, plan = fixture()
    schedule_document = {
        **schedule,
        "routes": [],
        "demand_completions": [],
        "domain_schedules": [],
    }
    schedule_path = tmp_path / "schedule.json"
    write_tdm_schedule_storage(schedule_path, schedule_document)
    indexed_schedule = read_tdm_schedule(schedule_path)
    indexed_plan = write_pin_plan_storage(tmp_path / "pin_plan.json", plan)
    assert validate_pin_plan(
        indexed_schedule, platform, positions, indexed_plan
    )["status"] == "pass"

    broken = copy.deepcopy(plan)
    broken["entries"][1]["physical_lane"] = 18
    indexed_broken = write_pin_plan_storage(tmp_path / "broken.json", broken)
    with pytest.raises(ValidationError, match="allocated direction banks"):
        validate_pin_plan(
            indexed_schedule, platform, positions, indexed_broken
        )


def test_opposite_direction_cannot_use_another_wire_in_the_same_bank():
    platform, schedule, positions, plan = fixture()
    plan["entries"][1]["physical_lane"] = 18
    # No lane/slot collision: pins 19 and 18 are different, and slots differ.
    with pytest.raises(ValidationError, match="allocated direction banks"):
        validate_pin_plan(schedule, platform, positions, plan)


def test_bidirectional_chimew_group_cannot_bypass_bank_policy():
    platform, schedule, positions, plan = fixture()
    plan["provider"] = CHIMEW_PIN_PLAN_PROVIDER
    for entry in plan["entries"]:
        entry.update(group=0, physical_lane=19)
    with pytest.raises(ValidationError, match="allocated direction banks"):
        validate_pin_plan(schedule, platform, positions, plan)


@pytest.mark.parametrize("mutation", ["missing", "overlap", "partial", "logical-lane"])
def test_bank_allocation_is_required_and_checked(mutation):
    platform, schedule, _, _ = fixture()
    if mutation == "missing":
        del schedule["bank_allocations"]
    elif mutation == "overlap":
        schedule["bank_allocations"][1]["lane_offset"] = 120
    elif mutation == "partial":
        schedule["bank_allocations"][0]["lane_offset"] = 1
    else:
        schedule["entries"][1]["lane"] = 1
    with pytest.raises(ValidationError):
        validate_bank_pin_assignment(schedule, platform, {})


def test_chimew_electrical_validator_rejects_bidirectional_bank_bundle():
    platform, schedule, _, plan = fixture()
    digest = _canonical_sha256(schedule)
    plan["provider"] = CHIMEW_PIN_PLAN_PROVIDER
    plan["configuration"].update(schedule_sha256=digest, electrical_map_sha256="a" * 64)
    binding = {
        "schema": FIXED_WIRING_BINDING_SCHEMA,
        "fixed_wiring": FixedWiringChannels(platform).source,
        "fpga_y_bounds": [{"fpga": f.id, "y_min": 0.0, "y_max": 100.0} for f in platform.fpgas], "status": "pass",
        "integration_status": "phase6-pin-plan", "provider": CHIMEW_PHASE6_BINDING_PROVIDER,
        "design": "banks", "platform": platform.name, "paper_provider": "test",
        "provenance": {"producer": "test", "producer_version": "1",
                       "schedule_sha256": digest, "electrical_map_sha256": "a" * 64,
                       "boarddb_sha256": "b" * 64, "package_pin_inventory_sha256": "c" * 64,
                       "assignment_input_sha256": "d" * 64},
        "entries": [{"group": "g", "schedule_entries": ["e0", "e1"],
                     "link": platform.links[0].id, "fpga_a": "fpga0", "fpga_b": "fpga1",
                     "physical_lane": 19, "direction": "bidirectional",
                     "package_pin_a": "P", "package_pin_b": "Q"}],
    }
    with pytest.raises(ValidationError, match="bidirectional bundle.*HP-bank directions"):
        validate_chimew_phase6_binding(schedule, platform, plan, binding)


def test_indexed_chimew_electrical_validator_preserves_bank_budget(tmp_path):
    platform, schedule, _, plan = fixture()
    schedule_document = {
        **schedule,
        "routes": [],
        "demand_completions": [],
        "domain_schedules": [],
    }
    schedule_path = tmp_path / "schedule.json"
    write_tdm_schedule_storage(schedule_path, schedule_document)
    indexed_schedule = read_tdm_schedule(schedule_path)
    digest = canonical_tdm_schedule_sha256(indexed_schedule)
    plan["provider"] = CHIMEW_PIN_PLAN_PROVIDER
    plan["configuration"].update(
        schedule_sha256=digest, electrical_map_sha256="a" * 64
    )
    plan["entries"][1]["physical_lane"] = 18
    indexed_plan = write_pin_plan_storage(tmp_path / "pin_plan.json", plan)
    binding = {
        "schema": FIXED_WIRING_BINDING_SCHEMA,
        "fixed_wiring": FixedWiringChannels(platform).source,
        "fpga_y_bounds": [{"fpga": f.id, "y_min": 0.0, "y_max": 100.0} for f in platform.fpgas],
        "status": "pass",
        "integration_status": "phase6-pin-plan",
        "provider": CHIMEW_PHASE6_BINDING_PROVIDER,
        "design": "banks",
        "platform": platform.name,
        "paper_provider": "test",
        "provenance": {
            "producer": "test",
            "producer_version": "1",
            "schedule_sha256": digest,
            "electrical_map_sha256": "a" * 64,
            "boarddb_sha256": "b" * 64,
            "package_pin_inventory_sha256": "c" * 64,
            "assignment_input_sha256": "d" * 64,
        },
        "entries": [],
    }
    with pytest.raises(ValidationError, match="allocated direction banks"):
        validate_chimew_phase6_binding(
            indexed_schedule, platform, indexed_plan, binding
        )
