from tests.virtual_io_reference import build_virtual_io_physical_contract, write_virtual_binding_systemverilog, write_virtual_binding_xdc
"""Chimew electrical integration over real fixed wiring, with tiny mocked geometry.

The inventory/pin identities are real. Native assignment and typed publication
are exercised, but these fixtures do not qualify placement geometry or NVDLA.
"""
import copy

import pytest

from emuflow.transport.chimew.chimew_fixed_wiring import (
    FIXED_WIRING_BINDING_SCHEMA, FixedWiringChannels,
    build_fixed_wiring_chimew_electrical_map,
)
from emuflow.transport.chimew.chimew_phase6 import (
    build_chimew_phase6_pin_plan, run_chimew_phase6_adapter,
    validate_chimew_electrical_map, validate_chimew_phase6_binding,
    validate_chimew_adapter_generation, chimew_adapter_report_generation_path,
)
from emuflow.transport.chimew.chimew_bank_channel import validate_chimew_bank_channel_input
from emuflow.transport.chimew.chimew_bank_channel_storage import write_chimew_bank_channel_storage
from emuflow.transport.chimew.chimew_binding_storage import read_chimew_binding, write_chimew_binding_storage
from emuflow.platform.default_platform import research_platform_path
from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256, write_json
from emuflow.transport.pin_plan_storage import read_pin_plan
from emuflow.platform.platform import Platform
from tests.native_build import chimew_bank_channel_assigner


@pytest.fixture
def fixed_case():
    from tests.test_chimew_phase6 import ChimewPhase6AdapterTest
    f = ChimewPhase6AdapterTest(); f.setUp()
    platform = Platform.load(research_platform_path(2))
    link = platform.links[0]
    a, b = link.endpoints
    f.schedule.update(platform=platform.name, metrics={"frame_slots": 8}, bank_allocations=[
        {"link": link.id, "from": source, "to": sink, "bank_count": count,
         "wires_per_bank": 20, "lane_offset": offset, "lanes": count * 20}
        for source, sink, count, offset in [(a, b, 18, 0), (b, a, 0, 360)]
    ])
    for i, entry in enumerate(f.schedule["entries"]):
        entry.update(link=link.id, **{"from": a, "to": b}, lane=i, slot=i, tdm_ratio=8)
    problem = f.assignment_input
    problem["platform"] = platform.name
    problem["domains"][0].update(fpga_a=a, fpga_b=b)
    wire = FixedWiringChannels(platform).channel(link.id, 0, a, b)
    problem["bank_pairs"][0]["bank_a"]["id"] = wire["bank_a"]
    problem["bank_pairs"][0]["bank_b"]["id"] = wire["bank_b"]
    for group in problem["groups"]:
        group["direction"] = "a_to_b"
        for member in group["members"]:
            member["fanout"]["x"] = 0.0
            member["fanins"][0]["x"] = 100.0
    electrical = build_fixed_wiring_chimew_electrical_map(platform, problem,
        channel_lanes={f"channel{i}": {"link": link.id, "physical_lane": i, "direction": "a_to_b"}
                       for i in range(2)},
        fpga_y_bounds=[{"fpga": fpga, "y_min": 0.0, "y_max": 100.0} for fpga in (a, b)],
        boarddb_sha256="d" * 64)
    return platform, f.schedule, problem, electrical


def test_native_assignment_publishes_both_fixed_polarities(fixed_case, tmp_path):
    platform, schedule, problem, electrical = fixed_case
    result = build_chimew_phase6_pin_plan(schedule, platform, problem, electrical,
        executable=str(chimew_bank_channel_assigner()))
    binding = result["electrical_binding"]
    assert binding["schema"] == FIXED_WIRING_BINDING_SCHEMA
    assert binding["metrics"]["package_pins"] == 8
    assert binding["metrics"]["channels"] == 2
    fixed = FixedWiringChannels(platform)
    for row in binding["entries"]:
        expected = fixed.channel(row["link"], row["physical_lane"], row["fpga_a"], row["fpga_b"])
        assert row["fixed_wire"] == expected["fixed_wire"]
        assert "differential_pair_a" not in row and "differential_pair_b" not in row
        assert row["package_pin_a"] == expected["differential_pair_a"]["p"]["package_pin"]
    stored = write_chimew_binding_storage(tmp_path / "binding.json", binding)
    assert stored["schema"] == FIXED_WIRING_BINDING_SCHEMA
    assert validate_chimew_phase6_binding(schedule, platform, result["pin_plan"], stored)["status"] == "pass"
    reopened = read_chimew_binding(tmp_path / "binding.json")
    assert list(reopened["entries"]) == binding["entries"]


@pytest.mark.parametrize("mutation", ["swap-pn", "other-n", "bank", "wire", "single-ended", "voltage", "source", "reverse-bank"])
def test_map_rejects_broken_fixed_electrical_contract(fixed_case, mutation):
    platform, _, problem, electrical = fixed_case
    changed = copy.deepcopy(electrical)
    row = changed["channels"][0]
    if mutation == "swap-pn":
        pair = FixedWiringChannels(platform).channel(row["link"], row["physical_lane"], "fpga0", "fpga1")["differential_pair_a"]
        row["package_pin_a"] = pair["n"]["package_pin"]
    elif mutation == "other-n": row["differential_pair_a"] = {"n": {"package_pin": "forged"}}
    elif mutation == "bank": row["bank_a"] += "-invented"
    elif mutation == "wire": row["fixed_wire"] += "-invented"
    elif mutation == "single-ended": row["electrical_class"] = "single_ended_parallel"
    elif mutation == "voltage": row["bank_voltage"] = 2.5
    elif mutation == "source": changed["fixed_wiring"]["board_sha256"] = "0" * 64
    else: changed["channels"][1]["direction"] = "b_to_a"
    with pytest.raises(ValidationError, match="fixed|differential|HP bank|bank identity"):
        validate_chimew_electrical_map(changed, platform, validate_chimew_bank_channel_input(problem))


def test_independent_binding_rejects_changed_negative_pin(fixed_case):
    platform, schedule, problem, electrical = fixed_case
    result = build_chimew_phase6_pin_plan(schedule, platform, problem, electrical,
        executable=str(chimew_bank_channel_assigner()))
    binding = copy.deepcopy(result["electrical_binding"])
    binding["entries"][0]["differential_pair_b"] = {"n": {"package_pin": "not-a-pin"}}
    with pytest.raises(ValidationError, match="P/N"):
        validate_chimew_phase6_binding(schedule, platform, result["pin_plan"], binding)


def test_report_committed_adapter_accepts_fixed_differential_map(fixed_case, tmp_path):
    platform, schedule, problem, electrical = fixed_case
    write_json(tmp_path / "platform.json", platform.to_dict())
    write_json(tmp_path / "schedule.json", schedule)
    electrical["provenance"]["boarddb_sha256"] = file_sha256(tmp_path / "platform.json")
    write_json(tmp_path / "map.json", electrical)
    with write_chimew_bank_channel_storage(tmp_path / "input.json", problem): pass
    output = tmp_path / "adapter"
    report = run_chimew_phase6_adapter(tmp_path / "schedule.json", tmp_path / "platform.json",
        tmp_path / "input.json", tmp_path / "map.json", output,
        executable=str(chimew_bank_channel_assigner()))
    assert report["status"] == "pass"
    validate_chimew_adapter_generation(chimew_adapter_report_generation_path(output, report))
    binding = read_chimew_binding(output / report["artifacts"]["electrical_binding"])
    plan = read_pin_plan(output / report["artifacts"]["pin_plan"])
    assert binding["schema"] == FIXED_WIRING_BINDING_SCHEMA
    assert validate_chimew_phase6_binding(schedule, platform, plan, binding)["status"] == "pass"
    previous = file_sha256(output / "adapter_report.json")
    electrical["channels"][0]["package_pin_a"] = "forged-after-a-successful-publication"
    write_json(tmp_path / "map.json", electrical)
    with pytest.raises(ValidationError, match="P/N"):
        run_chimew_phase6_adapter(tmp_path / "schedule.json", tmp_path / "platform.json",
            tmp_path / "input.json", tmp_path / "map.json", output,
            executable=str(chimew_bank_channel_assigner()))
    assert file_sha256(output / "adapter_report.json") == previous
    validate_chimew_adapter_generation(chimew_adapter_report_generation_path(output, report))
    assert not list(tmp_path.glob(".adapter.adapter-*.tmp"))


def test_selected_channels_reach_the_existing_phase7_lvds_contract(fixed_case, tmp_path):
    from emuflow.transport.virtual_board import (
        build_virtual_board,
    )
    platform, schedule, problem, electrical = fixed_case
    result = build_chimew_phase6_pin_plan(schedule, platform, problem, electrical,
        executable=str(chimew_bank_channel_assigner()))
    planned = {row["schedule_entry"]: row for row in result["pin_plan"]["entries"]}
    lanes = []
    for entry in schedule["entries"]:
        lane = planned[entry["id"]]["physical_lane"]
        row = {"schedule_entry": entry["id"], "lane": lane,
               **{key: entry[key] for key in ("link", "from", "to", "slot")}}
        lanes.append(row)
    from tests.virtual_binding_fixture import native_binding
    virtual = native_binding(platform, build_virtual_board(platform), schedule,
        {"lane_map": {"entries": lanes}})
    by_entry = {member: record for record in result["electrical_binding"]["entries"]
                for member in record["schedule_entries"]}
    for row in virtual["entries"]:
        selected = by_entry[row["schedule_entry"]]
        assert row["wire_id"] == selected["fixed_wire"]
        resolved = FixedWiringChannels(platform).channel(
            selected["link"], selected["physical_lane"], selected["fpga_a"], selected["fpga_b"])
        for role, side in (("source", "a"), ("sink", "b")):
            for polarity in ("p", "n"):
                assert row[role][polarity]["package_pin"] == resolved[f"differential_pair_{side}"][polarity]["package_pin"]
    for fpga in platform.fpgas:
        xdc, sv = tmp_path / f"{fpga.id}.xdc", tmp_path / f"{fpga.id}.sv"
        write_virtual_binding_xdc(xdc, virtual, fpga.id)
        write_virtual_binding_systemverilog(sv, virtual, fpga.id)
        contract = build_virtual_io_physical_contract(virtual, fpga.id, xdc_path=xdc, adapter_path=sv)
        assert contract["status"] == "pass"
        assert len(contract["entries"]) == 2
        assert all(row["iostandard"] == "LVDS" for row in contract["entries"])


@pytest.mark.parametrize("field", ["y_min", "y_max"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_channel_geometry_is_rejected(fixed_case, field, value):
    platform, _, problem, electrical = fixed_case
    electrical["fpga_y_bounds"][0][field] = value
    with pytest.raises(ValidationError, match="finite"):
        validate_chimew_electrical_map(electrical, platform, validate_chimew_bank_channel_input(problem))
