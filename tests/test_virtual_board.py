"""Virtual wiring is fixed hardware; TDM and direction only select its use."""
import copy
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict

import pytest

from emuflow import virtual_board
from tests.virtual_binding_fixture import native_binding, binding_text
from emuflow.default_platform import materialize_research_platform, research_platform_path
from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.platform import Platform
from emuflow.virtual_board import (
    INVENTORY_PATH,
    build_virtual_board,
    build_virtual_io_physical_contract,
    validate_virtual_binding,
    validate_virtual_board,
    write_wiring_csv,
)


@pytest.fixture
def platform():
    return Platform.load(research_platform_path(2))


def schedule_and_ports(platform, uses, *, forward_banks=18, physical_wires=None):
    """uses: (source, sink, logical wire, slot); ports model Phase 6 output."""
    link = platform.links[0]
    a, b = link.endpoints
    width = link.wires_per_bank
    schedule = {
        "design": "virtual_wiring_fixture", "platform": platform.name,
        "bank_allocations": [
            {"link": link.id, "from": source, "to": sink, "bank_count": count,
             "wires_per_bank": width, "lane_offset": offset, "lanes": count * width}
            for source, sink, count, offset in [
                (a, b, forward_banks, 0),
                (b, a, 18 - forward_banks, forward_banks * width),
            ]
        ],
        "entries": [],
    }
    artifacts = {"lane_map": {"entries": []}}
    for index, (source, sink, logical_wire, slot) in enumerate(uses):
        entry_id = f"entry{index}"
        wire = logical_wire if physical_wires is None else physical_wires[index]
        entry = {"id": entry_id, "net": f"net{index}", "link": link.id,
                 "from": source, "to": sink, "lane": logical_wire, "slot": slot,
                 "arrival_slot": slot + 9, "tdm_ratio": 8}
        schedule["entries"].append(entry)
        mapping = {"schedule_entry": entry_id, "link": link.id, "from": source,
                   "to": sink, "lane": wire, "slot": slot}
        artifacts["lane_map"]["entries"].append(mapping)
    return schedule, artifacts


@pytest.mark.parametrize("fpga_count,link_count,banks_per_fpga", [(2, 1, 18), (4, 4, 36)])
def test_capacity_real_pin_membership_and_disjoint_neighbor_banks(fpga_count, link_count, banks_per_fpga):
    platform = Platform.load(research_platform_path(fpga_count))
    board = build_virtual_board(platform)
    inventory = read_json(INVENTORY_PATH)
    pins = {(str(bank["id"]), pin["package_pin"]): pin
            for bank in inventory["banks"] for pin in bank["pins"]}
    assert board["metrics"]["differential_pairs"] == link_count * 360
    assert board["metrics"]["physical_conductors"] == link_count * 720
    assert board["metrics"]["endpoint_data_pins"] == link_count * 1440
    assert board["metrics"]["cables"] == link_count * 6
    assert board["assumptions"]["iostandard"] == "LVDS"
    assert Counter(w["link"] for w in board["wires"]) == {link.id: 360 for link in platform.links}
    used_pins, used_sites, contacts = defaultdict(set), defaultdict(set), defaultdict(set)
    bank_pair_counts = Counter()
    for wire in board["wires"]:
        assert len(wire["endpoints"]) == 2
        assert wire["connector_data_contacts"]["p"] != wire["connector_data_contacts"]["n"]
        for contact in wire["connector_data_contacts"].values():
            assert contact not in contacts[wire["cable"]]
            contacts[wire["cable"]].add(contact)
        for endpoint in wire["endpoints"]:
            fpga, bank = endpoint["fpga"], endpoint["bank"]
            bank_pair_counts[fpga, bank] += 1
            assert endpoint["p"]["package_pin"] != endpoint["n"]["package_pin"]
            for polarity in ("p", "n"):
                pin = endpoint[polarity]
                actual = pins[bank, pin["package_pin"]]
                assert actual["site"] == pin["site"]
                assert actual["pin_function"] == pin["pin_function"]
                assert actual["differential_pair_id"] == endpoint["pair_id"]
                assert actual["polarity"] == polarity
                function = re.fullmatch(r"IO_L(\d+)([PN])_.*_(\d+)", pin["pin_function"])
                assert function is not None
                assert "L" + function[1] == endpoint["pair_id"]
                assert function[2].lower() == polarity
                assert function[3] == bank
                assert "_GC_" not in pin["pin_function"]
                assert pin["package_pin"] not in used_pins[fpga]
                assert pin["site"] not in used_sites[fpga]
                used_pins[fpga].add(pin["package_pin"])
                used_sites[fpga].add(pin["site"])
    assert set(bank_pair_counts.values()) == {20}
    assert all(len(values) == 120 for values in contacts.values())
    for fpga in board["fpgas"]:
        assignments = fpga["bank_assignments"]
        assert len(assignments) == banks_per_fpga
        assert len({item["bank"] for item in assignments}) == banks_per_fpga
        assert "65" in fpga["unallocated_hp_banks"]
        assert all(item["data_pins_used"] == 40 for item in assignments)
        assert all(item["differential_pairs_used"] == 20 for item in assignments)
        assert len(used_pins[fpga["id"]]) == banks_per_fpga * 40
        assert len(fpga["unallocated_hp_banks"]) == 38 - banks_per_fpga
    assert validate_virtual_board(platform, board)["status"] == "pass"


def test_input_order_cannot_change_fixed_connections():
    platform = Platform.load(research_platform_path(4))
    value = platform.to_dict()
    value["fpgas"].reverse()
    value["links"].reverse()
    for link in value["links"]:
        link["endpoints"].reverse()
    assert build_virtual_board(Platform.from_dict(value)) == build_virtual_board(platform)


def test_reverse_direction_reuses_exact_same_physical_pair(platform):
    board = build_virtual_board(platform)
    forward, fa = schedule_and_ports(platform, [("fpga0", "fpga1", 7, 2)])
    reverse, ra = schedule_and_ports(platform, [("fpga1", "fpga0", 7, 2)], forward_banks=0)
    f = native_binding(platform, board, forward, fa)["entries"][0]
    r = native_binding(platform, board, reverse, ra)["entries"][0]
    assert f["wire_id"] == r["wire_id"]
    for before, after in [(f["source"], r["sink"]), (f["sink"], r["source"])]:
        assert before["fpga"] == after["fpga"]
        assert before["bank"] == after["bank"]
        assert before["pair_id"] == after["pair_id"]
        for polarity in ("p", "n"):
            assert before[polarity]["package_pin"] == after[polarity]["package_pin"]
            assert before[polarity]["site"] == after[polarity]["site"]


def test_tdm_nets_share_data_ports_and_emit_unique_lvds_constraints(platform):
    board = build_virtual_board(platform)
    schedule, artifacts = schedule_and_ports(platform, [("fpga0", "fpga1", 7, 2), ("fpga0", "fpga1", 7, 3)])
    binding = native_binding(platform, board, schedule, artifacts)
    assert binding["metrics"]["used_differential_pairs"] == 1
    assert binding["metrics"]["bound_data_ports"] == 4
    assert binding["entries"][0]["source"] == binding["entries"][1]["source"]
    assert binding["entries"][0]["sink"] == binding["entries"][1]["sink"]
    assert validate_virtual_binding(platform, board, schedule, artifacts, binding)["status"] == "pass"
    for fpga, role in [("fpga0", "source"), ("fpga1", "sink")]:
        xdc = binding_text(binding, fpga)
        assert xdc.count("set_property PACKAGE_PIN ") == 2
        assert xdc.count("set_property IOSTANDARD LVDS ") == 2
        for polarity in ("p", "n"):
            pin = binding["entries"][0][role][polarity]
            assert f"set_property PACKAGE_PIN {pin['package_pin']} [get_ports {{{pin['port']}}}]" in xdc


def test_same_pair_same_slot_collision_is_rejected(platform):
    board = build_virtual_board(platform)
    schedule, artifacts = schedule_and_ports(platform, [("fpga0", "fpga1", 7, 2), ("fpga0", "fpga1", 7, 2)])
    with pytest.raises(ValidationError, match="slot collision"):
        native_binding(platform, board, schedule, artifacts)


def test_physical_remap_preserves_bank_direction(platform):
    board = build_virtual_board(platform)
    uses = [("fpga0", "fpga1", 0, 2), ("fpga1", "fpga0", 140, 3)]
    schedule, artifacts = schedule_and_ports(platform, uses, forward_banks=7, physical_wires=[19, 159])
    binding = native_binding(platform, board, schedule, artifacts)
    assert [entry["wire"] for entry in binding["entries"]] == [19, 159]
    # Different wires and slots still cannot put opposite directions in one bank.
    schedule, bad = schedule_and_ports(platform, uses, forward_banks=7, physical_wires=[19, 18])
    with pytest.raises(ValidationError, match="allocated direction banks"):
        native_binding(platform, board, schedule, bad)


@pytest.mark.parametrize("mutation", ["missing", "pin", "polarity", "cable", "bank", "duplicate"])
def test_missing_or_modified_wiring_is_rejected(platform, mutation):
    board = build_virtual_board(platform)
    if mutation == "missing":
        board["wires"].pop()
    elif mutation == "pin":
        board["wires"][0]["endpoints"][0]["p"]["package_pin"] = "INVENTED"
    elif mutation == "polarity":
        end = board["wires"][0]["endpoints"][0]
        end["p"], end["n"] = end["n"], end["p"]
    elif mutation == "cable":
        board["wires"][0]["cable"] = board["wires"][-1]["cable"]
    elif mutation == "bank":
        board["wires"][0]["endpoints"][0]["bank"] = "999"
    else:
        board["wires"][1] = copy.deepcopy(board["wires"][0])
    with pytest.raises(ValidationError):
        validate_virtual_board(platform, board)


@pytest.mark.parametrize("mutation", ["missing", "pin", "polarity", "port", "wire", "slot", "digest"])
def test_binding_replay_rejects_modified_assignments(platform, mutation):
    board = build_virtual_board(platform)
    schedule, artifacts = schedule_and_ports(platform, [("fpga0", "fpga1", 7, 2)])
    binding = native_binding(platform, board, schedule, artifacts)
    if mutation == "missing":
        binding._sections["entries"] = []
    elif mutation == "digest":
        binding._header["board_sha256"] = "0" * 64
    else:
        binding._sections["entries"] = list(binding["entries"])
        entry = binding["entries"][0]
        if mutation == "pin":
            entry["source"]["p"]["package_pin"] = "INVENTED"
        elif mutation == "polarity":
            end = entry["source"]
            end["p"], end["n"] = end["n"], end["p"]
        elif mutation == "port":
            entry["source"]["p"]["port"] += "_wrong"
        elif mutation == "wire":
            entry["wire"] += 1
        else:
            entry["slot"] += 1
    with pytest.raises(ValidationError):
        validate_virtual_binding(platform, board, schedule, artifacts, binding)




def test_variable_pair_width_materializes_without_hard_coded_twenty(tmp_path):
    path = tmp_path / "platform.json"
    materialize_research_platform(path, fpga_count=2, wires_per_bank=16)
    platform = Platform.load(path)
    board = build_virtual_board(platform)
    assert len(board["wires"]) == 288
    assert board["metrics"]["physical_conductors"] == 576
    assert all(a["data_pins_used"] == 32 for f in board["fpgas"] for a in f["bank_assignments"])
    assert all(a["differential_pairs_used"] == 16 for f in board["fpgas"] for a in f["bank_assignments"])
    schedule, artifacts = schedule_and_ports(platform, [("fpga0", "fpga1", 287, 2)])
    assert native_binding(platform, board, schedule, artifacts)["entries"][0]["bank_group"] == 17


def test_pair_exhaustion_fails_without_single_ended_fallback(platform):
    value = platform.to_dict()
    value["links"][0].update(wires_per_bank=25, data_lanes_per_direction=450)
    with pytest.raises(ValidationError):
        build_virtual_board(Platform.from_dict(value))


def test_real_bank_exhaustion_fails_without_reusing_neighbor_banks(monkeypatch):
    inventory = read_json(INVENTORY_PATH)
    inventory["banks"] = inventory["banks"][:35]
    monkeypatch.setattr(virtual_board, "_inventory", lambda: inventory)
    with pytest.raises(ValidationError, match="insufficient.*HP banks"):
        build_virtual_board(Platform.load(research_platform_path(4)))


def test_inventory_part_mismatch_is_rejected(platform):
    value = platform.to_dict()
    value["fpgas"][0]["part"] = "xcvu19p-different-package"
    with pytest.raises(ValidationError, match="part"):
        build_virtual_board(Platform.from_dict(value))


@pytest.mark.parametrize("mutation", ["missing-n", "wrong-polarity", "wrong-pair", "unknown-functions"])
def test_pair_identity_requires_complete_vendor_verified_p_and_n(platform, monkeypatch, mutation):
    inventory = read_json(INVENTORY_PATH)
    bank = inventory["banks"][0]
    pins = [pin for pin in bank["pins"] if pin.get("differential_pair_id") is not None]
    positive = next(pin for pin in pins if pin["polarity"] == "p")
    negative = next(pin for pin in pins if pin["differential_pair_id"] == positive["differential_pair_id"]
                    and pin["polarity"] == "n")
    if mutation == "missing-n":
        bank["pins"].remove(negative)
    elif mutation == "wrong-polarity":
        positive["polarity"] = "n"
    elif mutation == "wrong-pair":
        positive["differential_pair_id"] = "L999"
    else:
        for pin in pins:
            pin["pin_function"] = None
    monkeypatch.setattr(virtual_board, "_inventory", lambda: inventory)
    with pytest.raises(ValidationError):
        build_virtual_board(platform)


def test_wiring_csv_contains_both_polarities_of_every_connection(platform, tmp_path):
    board = build_virtual_board(platform)
    path = tmp_path / "export" / "wiring.csv"
    write_wiring_csv(path, board)
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 360
    for wire, row in zip(board["wires"], rows):
        assert row["link"] == wire["link"]
        assert int(row["wire"]) == wire["wire"]
        for tag, endpoint in zip(("a", "b"), wire["endpoints"]):
            assert row[f"fpga_{tag}"] == endpoint["fpga"]
            for polarity in ("p", "n"):
                assert row[f"package_pin_{tag}_{polarity}"] == endpoint[polarity]["package_pin"]
                assert row[f"site_{tag}_{polarity}"] == endpoint[polarity]["site"]


def test_lvds_adapter_deduplicates_tdm_ports_and_preserves_polarity(platform):
    board = build_virtual_board(platform)
    schedule, artifacts = schedule_and_ports(platform, [("fpga0", "fpga1", 7, 2), ("fpga0", "fpga1", 7, 3)])
    binding = native_binding(platform, board, schedule, artifacts)
    for fpga, role, primitive in [("fpga0", "source", "OBUFDS"), ("fpga1", "sink", "IBUFDS")]:
        sv = binding_text(binding, fpga, systemverilog=True)
        assert f"module emuflow_virtual_io_{fpga}" in sv
        assert len(re.findall(rf"\b{primitive}\b", sv)) == 1
        if primitive == "IBUFDS":
            assert '.DIFF_TERM("TRUE")' in sv
        end = binding["entries"][0][role]
        assert end["p"]["port"] in sv and end["n"]["port"] in sv
        for polarity, primitive_pin in [("p", "O" if primitive == "OBUFDS" else "I"),
                                         ("n", "OB" if primitive == "OBUFDS" else "IB")]:
            assert re.search(rf"\.{primitive_pin}\s*\(\s*{re.escape(end[polarity]['port'])}\s*\)", sv)


def test_phase7_physical_contract_consumes_exact_xdc_and_adapter(platform, tmp_path):
    board = build_virtual_board(platform)
    schedule, artifacts = schedule_and_ports(
        platform,
        [("fpga0", "fpga1", 7, 2), ("fpga0", "fpga1", 7, 3)],
    )
    binding = native_binding(platform, board, schedule, artifacts)
    xdc = tmp_path / "virtual_data_pins.xdc"
    adapter = tmp_path / "virtual_differential_io.sv"
    xdc.write_text(binding_text(binding, "fpga0"), encoding="utf-8")
    adapter.write_text(
        binding_text(binding, "fpga0", systemverilog=True),
        encoding="utf-8",
    )
    contract = build_virtual_io_physical_contract(
        binding, "fpga0", xdc_path=xdc, adapter_path=adapter
    )
    assert contract["metrics"] == {
        "differential_pairs": 1,
        "physical_ports": 2,
        "scheduled_hops": 2,
    }
    assert contract["entries"][0]["schedule_entry_count"] == 2
    assert contract["entries"][0]["schedule_entries_sha256"] == hashlib.sha256(
        b'"entry0"\n"entry1"\n').hexdigest()
    with xdc.open("a", encoding="utf-8") as stream:
        stream.write("# stale\n")
    with pytest.raises(ValidationError, match="XDC differs"):
        build_virtual_io_physical_contract(
            binding, "fpga0", xdc_path=xdc, adapter_path=adapter
        )


@pytest.mark.parametrize("damage", ["missing", "duplicate", "wrong-peer", "wrong-slot"])
def test_current_binding_requires_complete_lane_identity(platform, damage):
    board = build_virtual_board(platform)
    schedule, artifacts = schedule_and_ports(platform, [("fpga0", "fpga1", 7, 2)])
    binding = native_binding(platform, board, schedule, artifacts)
    rows = list(artifacts["lane_map"]["entries"])
    if damage == "missing":
        rows.clear()
    elif damage == "duplicate":
        rows *= 2
    elif damage == "wrong-peer":
        rows[0]["to"] = "fpga0"
    else:
        rows[0]["slot"] += 1
    artifacts["lane_map"]._sections["entries"] = rows
    with pytest.raises(ValidationError, match="coverage differs|identity differs|fields differ"):
        validate_virtual_binding(platform, board, schedule, artifacts, binding)


def test_raw_binding_validation_route_is_retired(platform):
    board = build_virtual_board(platform)
    schedule, artifacts = schedule_and_ports(platform, [("fpga0", "fpga1", 7, 2)])
    binding = native_binding(platform, board, schedule, artifacts)
    with pytest.raises(ValidationError, match="current typed transport owner"):
        validate_virtual_binding(platform, board, schedule, artifacts, binding.materialize())
