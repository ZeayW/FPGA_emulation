import copy
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
import shutil
from pathlib import Path
from unittest import mock

from emuflow.equivalence import _MappedModel, simulate_partition_equivalence
from emuflow.chimew_phase6 import (
    CHIMEW_PHASE6_BINDING_PROVIDER,
    CHIMEW_PHASE6_BINDING_SCHEMA_V1,
)
from emuflow.errors import ValidationError
from emuflow.emuir_compact import write_compact_emuir
from emuflow.ir import EmuIR
from emuflow.io import read_json, write_json
from emuflow.lowering import (
    run_placement_ir_lowering_from_split,
    run_placement_ir_lowering_from_value,
)
from emuflow.netlist import (
    build_split_artifacts,
    transport_endpoint_port,
    transport_to_systemverilog,
    validate_split_artifacts,
)
from emuflow.partition import (
    assign_clusters,
    build_partition_assignment,
    build_clusters,
    normalize_partition_constraints,
)
from emuflow.phase6 import run_phase6, validate_phase6
from emuflow.phase3_assignment_index import (
    migrate_phase3_assignment_index,
    write_phase3_assignment_index,
)
from emuflow.split_netlist_storage import (
    SplitNetlistView,
    build_streaming_split_metadata,
    read_split_netlist_sqlite,
    write_split_netlists_sqlite,
)
from emuflow.pin_planning import (
    CHIMEW_PIN_PLAN_PROVIDER,
    SIGNAL_POSITION_HINTS_SCHEMA,
    _assignment_metrics,
    build_pin_plan,
)
from emuflow.pin_plan_storage import PIN_PLAN_STORAGE_SCHEMA, read_pin_plan
from emuflow.platform import Platform
from emuflow.open_transport_characterization import (
    BASE_FEATURE_NAMES,
    FRAME_SLOTS,
    OPEN_TRANSPORT_MODEL,
    OPEN_TRANSPORT_PROVENANCE,
)
from emuflow.routing import normalize_route_constraints
from emuflow.tdm import build_tdm_schedule
from emuflow.tdm_schedule_storage import write_tdm_schedule_storage
from emuflow.transport_storage import (
    LANE_MAP_STORAGE_SCHEMA,
    TRANSPORT_STORAGE_SCHEMA,
    LaneMapView,
    TransportView,
    write_transport_storage,
)
from emuflow.timing_routing import route_system_native
from emuflow.yosys import import_yosys_json
from tests.native_build import tlr_router


ROOT = Path(__file__).resolve().parents[1]
PLATFORM_PATH = ROOT / "platforms" / "virtual" / "xcvu3p_2fpga_p2p.json"


class Phase6Test(unittest.TestCase):
    def setUp(self) -> None:
        self.ir = import_yosys_json(
            ROOT / "examples" / "yosys" / "counter.json",
            top="counter",
            clocks=["clk"],
        )
        self.platform = Platform.load(PLATFORM_PATH)
        constraints = normalize_partition_constraints(
            None, self.ir, self.platform
        )
        clusters = build_clusters(self.ir, constraints)
        self.assignment = assign_clusters(
            self.ir,
            self.platform,
            clusters,
            constraints,
            seed=20260727,
        )
        route_constraints = normalize_route_constraints(
            None, self.platform, frame_slots=32
        )
        self.routes = route_system_native(
            self.assignment,
            self.platform,
            route_constraints,
            executable=str(tlr_router()),
        )
        self.schedule = build_tdm_schedule(self.routes, self.platform)

    def _chimew_certificate(self, schedule, positions, plan):
        chimew_plan = copy.deepcopy(plan)
        chimew_plan["provider"] = CHIMEW_PIN_PLAN_PROVIDER
        schedule_sha256 = hashlib.sha256(
            json.dumps(
                schedule,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        chimew_plan["configuration"].update({
            "electrical_map_sha256": "d" * 64,
            "schedule_sha256": schedule_sha256,
        })
        schedule_by_id = {entry["id"]: entry for entry in schedule["entries"]}
        plan_groups = {}
        for entry in chimew_plan["entries"]:
            plan_groups.setdefault(entry["group"], []).append(entry)
        for lane, members in enumerate(plan_groups.values()):
            for entry in members:
                entry["physical_lane"] = lane
        assignment = {
            entry["schedule_entry"]: (entry["group"], entry["physical_lane"])
            for entry in chimew_plan["entries"]
        }
        chimew_plan["metrics"].update(
            _assignment_metrics(
                schedule,
                self.platform,
                positions,
                assignment,
                float(chimew_plan["weights"]["crossing"]),
                float(chimew_plan["weights"]["position"]),
            )
        )

        binding_entries = []
        for index, (group, members) in enumerate(sorted(plan_groups.items())):
            first = schedule_by_id[members[0]["schedule_entry"]]
            if not all(
                schedule_by_id[item["schedule_entry"]][key] == first[key]
                for item in members
                for key in ("link", "from", "to")
            ) or not all(
                item["physical_lane"] == members[0]["physical_lane"]
                for item in members
            ):
                raise AssertionError("fixture pin group is not electrically homogeneous")
            binding_entries.append({
                "group": f"group{group}",
                "schedule_entries": sorted(
                    item["schedule_entry"] for item in members
                ),
                "direction": "a_to_b",
                "bank_pair": f"bank{index}",
                "channel": f"channel{index}",
                "link": first["link"],
                "physical_lane": members[0]["physical_lane"],
                "fpga_a": first["from"],
                "fpga_b": first["to"],
                "bank_a": f"bank_a{index}",
                "bank_b": f"bank_b{index}",
                "package_pin_a": f"PA{index}",
                "package_pin_b": f"PB{index}",
                "iostandard": "LVCMOS18",
                "supported_iostandards": ["LVCMOS18"],
                "bank_voltage": 1.8,
                "electrical_class": "single_ended_parallel",
            })
        binding = {
            "schema": CHIMEW_PHASE6_BINDING_SCHEMA_V1,
            "status": "pass",
            "integration_status": "phase6-pin-plan",
            "design": schedule["design"],
            "platform": self.platform.name,
            "provider": CHIMEW_PHASE6_BINDING_PROVIDER,
            "paper_provider": "chimew-section3.4-two-stage-assignment-v1",
            "extension_scope": "test fixture",
            "provenance": {
                "producer": "fixture",
                "producer_version": "1",
                "boarddb_sha256": "a" * 64,
                "package_pin_inventory_sha256": "b" * 64,
                "electrical_map_sha256": "d" * 64,
                "assignment_input_sha256": "e" * 64,
                "schedule_sha256": schedule_sha256,
            },
            "metrics": {
                "signals": len(schedule["entries"]),
                "groups": len(binding_entries),
                "channels": len(binding_entries),
                "package_pins": 2 * len(binding_entries),
                "lane_slot_collisions": 0,
                "package_pin_collisions": 0,
            },
            "entries": binding_entries,
        }
        return chimew_plan, binding

    def test_transport_store_is_deterministic_and_batched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            assignment_path = root / "assignment.sqlite"
            write_phase3_assignment_index(
                assignment_path, self.ir, self.assignment
            )
            link = self.platform.links[0]
            source, sink = link.endpoints
            count = 5_000
            schedule = {
                "schema": "emuflow.tdm-schedule/v1",
                "design": self.ir.value["design"]["name"],
                "platform": self.platform.name,
                "provider": "transport-store-test",
                "transport_semantics": "registered-boundary",
                "metrics": {"frame_slots": 8_192},
                "routes": [
                    {
                        "id": f"demand_{index:06d}",
                        "net": f"net_{index:06d}",
                        "source": source,
                    }
                    for index in range(count)
                ],
                "entries": [
                    {
                        "id": f"entry_{index:06d}",
                        "demand": f"demand_{index:06d}",
                        "net": f"net_{index:06d}",
                        "link": link.id,
                        "from": source,
                        "to": sink,
                        "slot": index,
                        "arrival_slot": index + 1,
                        "lane": 0,
                    }
                    for index in range(count)
                ],
            }
            summaries = []
            writer_uri_modes = []
            original_connect = sqlite3.connect

            def recording_connect(database, *args, **kwargs):
                if Path(database).name.startswith(".transport.sqlite."):
                    writer_uri_modes.append(kwargs.get("uri", False))
                return original_connect(database, *args, **kwargs)

            with mock.patch(
                "emuflow.transport_storage.sqlite3.connect",
                side_effect=recording_connect,
            ):
                for run in range(2):
                    output = root / f"run-{run}"
                    artifacts = write_transport_storage(
                        output,
                        assignment_path,
                        schedule,
                        self.platform,
                        design=self.ir.value["design"],
                        clocks=self.ir.value["clocks"],
                    )
                    summaries.append(artifacts["summary"])
                    self.assertEqual(
                        len(artifacts["lane_map"]["entries"]), count
                    )
                    self.assertEqual(
                        sum(
                            len(transport["endpoints"])
                            for transport in artifacts["transports"].values()
                        ),
                        2 * count,
                    )
                    self.assertLess(
                        (output / "fpga0/transport.json").stat().st_size,
                        4096,
                    )
            self.assertEqual(writer_uri_modes, [True, True])
            self.assertEqual(summaries[0]["sha256"], summaries[1]["sha256"])

    def test_split_exact_coverage_lane_agreement_and_equivalence(self) -> None:
        artifacts = build_split_artifacts(
            self.ir, self.assignment, self.schedule, self.platform
        )
        validation = validate_split_artifacts(
            self.ir,
            self.assignment,
            self.schedule,
            self.platform,
            artifacts,
        )
        self.assertNotIn("semantic_contract", artifacts["manifest"])
        equivalence = simulate_partition_equivalence(
            self.ir, self.assignment, self.schedule, cycles=12
        )
        self.assertEqual(validation["status"], "pass")
        self.assertEqual(validation["instances"], 8)
        self.assertEqual(
            validation["transport_endpoints"],
            2 * validation["scheduled_hops"],
        )
        self.assertEqual(equivalence["status"], "pass")
        self.assertEqual(equivalence["mismatches"], 0)
        self.assertEqual(equivalence["cycles"], 12)

    def test_sqlite_data_plane_matches_legacy_split_exactly(self) -> None:
        old = build_split_artifacts(
            self.ir, self.assignment, self.schedule, self.platform
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.sqlite"
            assignment_path = root / "assignment.sqlite"
            split_path = root / "split.sqlite"
            write_compact_emuir(ir_path, self.ir.to_dict())
            write_phase3_assignment_index(
                assignment_path, self.ir, self.assignment
            )
            proof = write_split_netlists_sqlite(
                split_path,
                ir_path,
                assignment_path,
                self.schedule,
                expected_design=self.ir.value["design"]["name"],
                expected_platform=self.platform.name,
            )
            control = build_streaming_split_metadata(
                assignment_path,
                self.schedule,
                self.platform,
                design=self.ir.value["design"],
                clocks=self.ir.value["clocks"],
            )
            self.assertEqual(proof["status"], "pass")
            with sqlite3.connect(split_path) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0],
                    2,
                )
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertIn("source_instances", tables)
                self.assertIn("instance_copies", tables)
                self.assertIn("endpoint_segments", tables)
                self.assertNotIn("instances", tables)
                self.assertNotIn("endpoints", tables)
                copy_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(instance_copies)"
                    )
                }
                self.assertNotIn("resources_json", copy_columns)
                self.assertNotIn("parameters_json", copy_columns)
                self.assertNotIn("extras_json", copy_columns)
            self.assertEqual(control["lane_map"], old["lane_map"])
            for fpga in ("fpga0", "fpga1"):
                self.assertEqual(
                    read_split_netlist_sqlite(split_path, fpga),
                    old["netlists"][fpga],
                )
                self.assertEqual(
                    control["transports"][fpga], old["transports"][fpga]
                )
                self.assertEqual(control["anchors"][fpga], old["anchors"][fpga])

    def test_streaming_phase7_lowering_matches_legacy_exactly(self) -> None:
        """The production SQLite handoff must preserve every EmuIR record."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.sqlite"
            assignment_path = root / "assignment.sqlite"
            split_path = root / "split.sqlite"
            write_compact_emuir(ir_path, self.ir.to_dict())
            write_phase3_assignment_index(
                assignment_path, self.ir, self.assignment
            )
            write_split_netlists_sqlite(
                split_path,
                ir_path,
                assignment_path,
                self.schedule,
                expected_design=self.ir.value["design"]["name"],
                expected_platform=self.platform.name,
            )
            control = build_streaming_split_metadata(
                assignment_path,
                self.schedule,
                self.platform,
                design=self.ir.value["design"],
                clocks=self.ir.value["clocks"],
            )
            fpga = "fpga0"
            transport = control["transports"][fpga]
            transport_path = root / "transport.json"
            write_json(transport_path, transport)

            ports = []
            instances = []
            nets = []
            if transport["source_signals"]:
                ports.append({
                    "id": "source_values",
                    "name": "source_values",
                    "direction": "input",
                    "width": len(transport["source_signals"]),
                    "clock": False,
                    "reset": False,
                })
            if transport["shadow_signals"]:
                ports.append({
                    "id": "shadow_values",
                    "name": "shadow_values",
                    "direction": "output",
                    "width": len(transport["shadow_signals"]),
                    "clock": False,
                    "reset": False,
                })
            for index, _signal in enumerate(transport["source_signals"]):
                identity = f"source_sink_{index}"
                instances.append({
                    "id": identity,
                    "name": identity,
                    "type": "LUT1",
                    "resources": {"lut": 1},
                    "parameters": {"INIT": "10"},
                    "attributes": {},
                    "constant_connections": [],
                })
                nets.append({
                    "id": f"source_net_{index}",
                    "name": f"source_net_{index}",
                    "drivers": [{
                        "instance": None,
                        "port": "source_values",
                        "bit": index,
                    }],
                    "sinks": [{
                        "instance": identity,
                        "port": "I0",
                        "bit": 0,
                    }],
                    "fanout": 1,
                    "cut_class": "combinational",
                })
            for index, _signal in enumerate(transport["shadow_signals"]):
                identity = f"shadow_driver_{index}"
                instances.append({
                    "id": identity,
                    "name": identity,
                    "type": "FDRE",
                    "resources": {"ff": 1},
                    "parameters": {"INIT": "0"},
                    "attributes": {},
                    "constant_connections": [],
                })
                nets.append({
                    "id": f"shadow_net_{index}",
                    "name": f"shadow_net_{index}",
                    "drivers": [{
                        "instance": identity,
                        "port": "Q",
                        "bit": 0,
                    }],
                    "sinks": [{
                        "instance": None,
                        "port": "shadow_values",
                        "bit": index,
                    }],
                    "fanout": 1,
                    "cut_class": "register_output",
                })
            interface_ports = {}
            for endpoint in transport["endpoints"]:
                port_id = transport_endpoint_port(endpoint)
                width = endpoint["lane"] + 1
                record = interface_ports.setdefault(port_id, {
                    "id": port_id,
                    "name": port_id,
                    "direction": (
                        "output" if endpoint["kind"] == "tx" else "input"
                    ),
                    "width": width,
                    "clock": False,
                    "reset": False,
                })
                record["width"] = max(record["width"], width)
                is_tx = endpoint["kind"] == "tx"
                nets.append({
                    "id": f"interface_{endpoint['id']}",
                    "name": f"interface_{endpoint['id']}",
                    "drivers": ([] if is_tx else [{
                        "instance": None,
                        "port": port_id,
                        "bit": endpoint["lane"],
                    }]),
                    "sinks": ([{
                        "instance": None,
                        "port": port_id,
                        "bit": endpoint["lane"],
                    }] if is_tx else []),
                    "fanout": int(is_tx),
                    "cut_class": "combinational",
                })
            ports.extend(interface_ports.values())
            transport_ir = EmuIR({
                "schema": "emuflow.emuir/v1",
                "design": {
                    "name": "transport",
                    "top": "transport",
                    "source_format": "test",
                },
                "ports": ports,
                "instances": instances,
                "nets": nets,
                "clocks": [],
                "warnings": [],
            })
            transport_ir_path = root / "transport.emuir.sqlite"
            write_compact_emuir(transport_ir_path, transport_ir.to_dict())

            legacy_output = root / "legacy.emuir.sqlite"
            direct_output = root / "direct.emuir.sqlite"
            legacy_boundary = root / "legacy-boundary.json"
            direct_boundary = root / "direct-boundary.json"
            run_placement_ir_lowering_from_value(
                read_split_netlist_sqlite(split_path, fpga),
                transport_path,
                transport_ir_path,
                legacy_output,
                boundary_identity_path=legacy_boundary,
            )
            with mock.patch(
                "emuflow.lowering.read_split_netlist_sqlite",
                side_effect=AssertionError(
                    "streaming lowering must not materialize a split graph"
                ),
                create=True,
            ), mock.patch.object(
                SplitNetlistView,
                "instances",
                side_effect=AssertionError(
                    "production lowering must not iterate DUT instances"
                ),
            ), mock.patch.object(
                SplitNetlistView,
                "nets",
                side_effect=AssertionError(
                    "production lowering must not iterate DUT nets"
                ),
            ), mock.patch(
                "emuflow.lowering.EmuIR.load",
                side_effect=AssertionError(
                    "production lowering must not reconstruct transport EmuIR"
                ),
            ):
                run_placement_ir_lowering_from_split(
                    split_path,
                    fpga,
                    transport_path,
                    transport_ir_path,
                    direct_output,
                    boundary_identity_path=direct_boundary,
                )
            self.assertEqual(
                EmuIR.load(direct_output).to_dict(),
                EmuIR.load(legacy_output).to_dict(),
            )
            self.assertEqual(
                read_json(direct_boundary), read_json(legacy_boundary)
            )

    def test_legacy_assignment_migration_does_not_load_emuir_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.sqlite"
            assignment_path = root / "assignment.json"
            index_path = root / "assignment.sqlite"
            write_compact_emuir(ir_path, self.ir.to_dict())
            assignment_path.write_text(
                json.dumps(self.assignment), encoding="utf-8"
            )
            with mock.patch(
                "emuflow.phase3_assignment_index.EmuIR.load",
                side_effect=AssertionError("whole EmuIR must not load"),
            ):
                summary = migrate_phase3_assignment_index(
                    index_path, ir_path, assignment_path
                )
            self.assertEqual(summary["status"], "pass")
            self.assertEqual(summary["source_instances"], 8)

    def test_large_sqlite_phase6_never_enters_python_graph_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.sqlite"
            assignment_path = root / "assignment.sqlite"
            schedule_path = root / "schedule.json"
            output_path = root / "phase6"
            write_compact_emuir(ir_path, self.ir.to_dict())
            with sqlite3.connect(ir_path) as connection:
                connection.execute(
                    "UPDATE metadata SET value=? "
                    "WHERE key='record_counts_json'",
                    (json.dumps({
                        "cell_records": 4_000_000,
                        "net_records": 4_000_000,
                        "endpoint_records": 12_000_000,
                    }),),
                )
            write_phase3_assignment_index(
                assignment_path, self.ir, self.assignment
            )
            write_json(schedule_path, self.schedule)
            with mock.patch(
                "emuflow.phase6.EmuIR.load",
                side_effect=AssertionError(
                    "large Phase 6 must not materialize the EmuIR graph"
                ),
            ):
                report = run_phase6(
                    ir_path,
                    assignment_path,
                    schedule_path,
                    PLATFORM_PATH,
                    output_path,
                    clock_periods={"clk": 10.0},
                )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["equivalence"]["cycles"], 0)
            self.assertEqual(
                report["equivalence"]["provider"],
                "constructive-sqlite-split-certificate-v1",
            )

    def test_cycle_model_supports_generic_yosys_lut_and_dff(self) -> None:
        value = {
            "modules": {
                "generic": {
                    "attributes": {"top": "1"},
                    "ports": {
                        "clk": {"direction": "input", "bits": [2]},
                        "d": {"direction": "input", "bits": [3]},
                        "q": {"direction": "output", "bits": [4]},
                        "y": {"direction": "output", "bits": [5]},
                    },
                    "cells": {
                        "ff": {
                            "type": "$_DFF_P_",
                            "parameters": {},
                            "attributes": {},
                            "port_directions": {
                                "C": "input",
                                "D": "input",
                                "Q": "output",
                            },
                            "connections": {
                                "C": [2],
                                "D": [3],
                                "Q": [4],
                            },
                        },
                        "lut": {
                            "type": "$lut",
                            "parameters": {
                                "WIDTH": "00000000000000000000000000000010",
                                "LUT": "0110",
                            },
                            "attributes": {},
                            "port_directions": {
                                "A": "input",
                                "Y": "output",
                            },
                            "connections": {"A": [4, 3], "Y": [5]},
                        },
                    },
                    "netnames": {
                        "clk": {"bits": [2]},
                        "d": {"bits": [3]},
                        "q": {"bits": [4]},
                        "y": {"bits": [5]},
                    },
                }
            }
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "generic.json"
            source.write_text(json.dumps(value), encoding="utf-8")
            model = _MappedModel(
                import_yosys_json(source, top="generic", clocks=["clk"])
            )
            values, next_state, outputs = model.evaluate(
                model.initial_state(), cycle=0, seed=7
            )
            self.assertEqual(len(model.ff_ids), 1)
            self.assertEqual(len(model.lut_ids), 1)
            self.assertIn("ff", next_state)
            self.assertEqual(outputs["q[0]"], 0)
            self.assertIn("y[0]", outputs)
            self.assertIn("y", values)

    def test_phase6_writes_and_independently_reloads_all_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.sqlite"
            assignment_path = root / "assignment.json"
            schedule_path = root / "schedule.json"
            write_compact_emuir(ir_path, self.ir.to_dict())
            assignment_path.write_text(
                json.dumps(self.assignment), encoding="utf-8"
            )
            write_phase3_assignment_index(
                root / "assignment.sqlite", self.ir, self.assignment
            )
            write_tdm_schedule_storage(schedule_path, self.schedule)
            output = root / "phase6"
            report = run_phase6(
                ir_path,
                assignment_path,
                schedule_path,
                PLATFORM_PATH,
                output,
                equivalence_cycles=8,
            )
            self.assertEqual(report["status"], "pass")
            validation = validate_phase6(
                ir_path,
                assignment_path,
                schedule_path,
                PLATFORM_PATH,
                output / "manifest.json",
            )
            self.assertEqual(validation["status"], "pass")

            def read_without_schedule(path: Path):
                self.assertNotEqual(Path(path), schedule_path)
                return read_json(Path(path))

            with mock.patch(
                "emuflow.phase6.read_json", side_effect=read_without_schedule
            ):
                reused = validate_phase6(
                    ir_path,
                    assignment_path,
                    schedule_path,
                    PLATFORM_PATH,
                    output / "manifest.json",
                    schedule_value=self.schedule,
                )
            self.assertEqual(reused, validation)

            legacy_control = build_streaming_split_metadata(
                root / "assignment.sqlite",
                self.schedule,
                self.platform,
                design=self.ir.value["design"],
                clocks=self.ir.value["clocks"],
            )
            raw_lane_manifest = json.loads(
                (output / "lane_map.json").read_text(encoding="utf-8")
            )
            raw_transport_manifest = json.loads(
                (output / "fpga0/transport.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                raw_lane_manifest["schema"], LANE_MAP_STORAGE_SCHEMA
            )
            self.assertEqual(
                raw_transport_manifest["schema"], TRANSPORT_STORAGE_SCHEMA
            )
            self.assertLess((output / "lane_map.json").stat().st_size, 4096)
            self.assertLess(
                (output / "fpga0/transport.json").stat().st_size, 4096
            )
            self.assertEqual(
                read_json(output / "lane_map.json"),
                legacy_control["lane_map"],
            )
            self.assertEqual(
                read_json(output / "fpga0/transport.json"),
                legacy_control["transports"]["fpga0"],
            )
            self.assertEqual(
                (output / "fpga0/transport_schedule.sv").read_text(),
                transport_to_systemverilog(
                    legacy_control["transports"]["fpga0"], self.platform
                ),
            )
            self.assertEqual(
                len(LaneMapView(output / "lane_map.json")["entries"]),
                len(self.schedule["entries"]),
            )
            self.assertEqual(
                len(TransportView(output / "fpga0/transport.json")["endpoints"]),
                len(legacy_control["transports"]["fpga0"]["endpoints"]),
            )

            relocated = root / "phase6-relocated"
            shutil.copytree(output, relocated)
            self.assertEqual(
                read_json(relocated / "fpga0/transport.json"),
                legacy_control["transports"]["fpga0"],
            )
            store = relocated / "transport.sqlite"
            store.write_bytes(store.read_bytes()[:-31])
            with self.assertRaises(ValidationError):
                TransportView(relocated / "fpga0/transport.json")

            for filename in (
                "manifest.json",
                "lane_map.json",
                "phase6_report.json",
                "virtual_runtime_controller.sv",
                "split-netlists.sqlite",
                "transport.sqlite",
                "fpga0/transport.json",
                "fpga0/transport_schedule.sv",
                "fpga0/virtual_anchors.json",
                "fpga0/virtual_anchors.xdc.template",
                "fpga1/transport.json",
                "fpga1/transport_schedule.sv",
                "fpga1/virtual_anchors.json",
                "fpga1/virtual_anchors.xdc.template",
            ):
                self.assertTrue((output / filename).is_file(), filename)

    def test_phase6_binds_and_replays_transport_cost_prediction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.sqlite"
            assignment_path = root / "assignment.json"
            schedule_path = root / "schedule.json"
            cost_path = root / "transport-cost.json"
            write_compact_emuir(ir_path, self.ir.to_dict())
            assignment_path.write_text(
                json.dumps(self.assignment), encoding="utf-8"
            )
            write_phase3_assignment_index(
                root / "assignment.sqlite", self.ir, self.assignment
            )
            schedule = copy.deepcopy(self.schedule)
            schedule["metrics"]["frame_slots"] = 8
            schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
            feature_names = list(BASE_FEATURE_NAMES) + [
                f"frame_slots_{slots}" for slots in FRAME_SLOTS[1:]
            ]
            cost_path.write_text(
                json.dumps(
                    {
                        "schema": "emuflow.transport-cost/v2",
                        "platform": self.platform.name,
                        "profile": "nominal",
                        "model": OPEN_TRANSPORT_MODEL,
                        "feature_names": feature_names,
                        "resources": {
                            "lut": {name: 1.0 for name in feature_names}
                        },
                        "provenance": {
                            "class": OPEN_TRANSPORT_PROVENANCE,
                            "fit_sha256": "1" * 64,
                        },
                    }
                ),
                encoding="utf-8",
            )
            output = root / "phase6"
            report = run_phase6(
                ir_path,
                assignment_path,
                schedule_path,
                PLATFORM_PATH,
                output,
                transport_cost_path=cost_path,
            )
            prediction = report["validation"]["transport_cost_prediction"]
            self.assertEqual(prediction["status"], "pass")
            self.assertTrue((output / "transport-cost.json").is_file())
            replay = validate_phase6(
                ir_path,
                assignment_path,
                schedule_path,
                PLATFORM_PATH,
                output / "manifest.json",
            )
            self.assertEqual(replay["transport_cost_prediction"], prediction)

    def test_phase6_materializes_validated_placement_aware_pin_plan(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            schedule = copy.deepcopy(self.schedule)
            for entry in schedule["entries"]:
                entry["tdm_ratio"] = schedule["metrics"]["frame_slots"]
            executable = root / "emuflow_pin_planner"
            subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-O2",
                    "-pthread",
                    str(
                        ROOT
                        / "src/native/placement_aware_pin_planner.cpp"
                    ),
                    "-o",
                    str(executable),
                ],
                check=True,
            )
            positions = {
                "schema": SIGNAL_POSITION_HINTS_SCHEMA,
                "design": schedule["design"],
                "platform": schedule["platform"],
                "provider": "openparf-lookahead-centroid-v1",
                "region_count": 3,
                "metrics": {
                    "signals": len(schedule["entries"]),
                    "endpoint_centroid_fallbacks": 0,
                },
                "entries": [
                    {
                        "schedule_entry": entry["id"],
                        "source_y": 0.25,
                        "sink_y": 0.75,
                        "source_region": 0,
                        "sink_region": 2,
                        "source_fallback": False,
                        "sink_fallback": False,
                    }
                    for entry in schedule["entries"]
                ],
            }
            plan = build_pin_plan(
                schedule,
                self.platform,
                positions,
                executable=str(executable),
                refinement_iterations=4,
            )
            paths = {
                "ir": root / "design.emuir.sqlite",
                "assignment": root / "assignment.json",
                "schedule": root / "schedule.json",
                "positions": root / "positions.json",
                "plan": root / "plan.json",
            }
            documents = {
                "assignment": self.assignment,
                "schedule": schedule,
                "positions": positions,
                "plan": plan,
            }
            write_compact_emuir(paths["ir"], self.ir.to_dict())
            write_phase3_assignment_index(
                root / "assignment.sqlite", self.ir, self.assignment
            )
            for name, path in paths.items():
                if name == "ir":
                    continue
                path.write_text(
                    json.dumps(documents[name]), encoding="utf-8"
                )
            output = root / "phase6"
            report = run_phase6(
                paths["ir"],
                paths["assignment"],
                paths["schedule"],
                PLATFORM_PATH,
                output,
                equivalence_cycles=4,
                pin_plan_path=paths["plan"],
                position_hints_path=paths["positions"],
            )
            self.assertEqual(
                report["provider"],
                "placement-aware-pin-split-v1",
            )
            self.assertEqual(
                report["pin_plan_validation"]["status"], "pass"
            )
            self.assertEqual(report["artifacts"]["pin_plan"], "pin_plan.json")
            self.assertTrue((output / "pin_plan.json").is_file())
            self.assertTrue((output / "pin_plan.sqlite").is_file())
            self.assertEqual(
                read_json(output / "pin_plan.json")["schema"],
                PIN_PLAN_STORAGE_SCHEMA,
            )
            self.assertLess((output / "pin_plan.json").stat().st_size, 4096)
            self.assertEqual(
                read_pin_plan(output / "pin_plan.json", materialize=True),
                plan,
            )
            self.assertTrue((output / "position_hints.json").is_file())
            chimew_plan, binding = self._chimew_certificate(
                schedule, positions, plan
            )
            chimew_plan_path = root / "chimew-plan.json"
            binding_path = root / "electrical-binding.json"
            write_json(chimew_plan_path, chimew_plan)
            write_json(binding_path, binding)
            chimew_output = root / "phase6-chimew"
            with self.assertRaisesRegex(ValueError, "electrical_binding_path"):
                run_phase6(
                    paths["ir"],
                    paths["assignment"],
                    paths["schedule"],
                    PLATFORM_PATH,
                    root / "phase6-chimew-without-binding",
                    equivalence_cycles=4,
                    pin_plan_path=chimew_plan_path,
                    position_hints_path=paths["positions"],
                )
            chimew_report = run_phase6(
                paths["ir"],
                paths["assignment"],
                paths["schedule"],
                PLATFORM_PATH,
                chimew_output,
                equivalence_cycles=4,
                pin_plan_path=chimew_plan_path,
                position_hints_path=paths["positions"],
                electrical_binding_path=binding_path,
            )
            self.assertEqual(
                chimew_report["electrical_binding_validation"]["status"], "pass"
            )
            self.assertEqual(
                read_json(chimew_output / "manifest.json")["electrical_binding"],
                "electrical_binding.json",
            )
            self.assertEqual(
                validate_phase6(
                    paths["ir"],
                    paths["assignment"],
                    paths["schedule"],
                    PLATFORM_PATH,
                    chimew_output / "manifest.json",
                )["status"],
                "pass",
            )
            validation = validate_phase6(
                paths["ir"],
                paths["assignment"],
                paths["schedule"],
                PLATFORM_PATH,
                output / "manifest.json",
            )
            self.assertEqual(validation["status"], "pass")

    def test_lane_map_corruption_is_rejected(self) -> None:
        artifacts = build_split_artifacts(
            self.ir, self.assignment, self.schedule, self.platform
        )
        broken = copy.deepcopy(artifacts)
        broken["lane_map"]["entries"][0]["lane"] += 1
        with self.assertRaisesRegex(ValidationError, "lane_map"):
            validate_split_artifacts(
                self.ir,
                self.assignment,
                self.schedule,
                self.platform,
                broken,
            )

    def test_producer_linear_validation_does_not_rebuild_split(self) -> None:
        artifacts = build_split_artifacts(
            self.ir, self.assignment, self.schedule, self.platform
        )
        with mock.patch(
            "emuflow.netlist.build_split_artifacts",
            side_effect=AssertionError("split reconstruction entered hot path"),
        ):
            validation = validate_split_artifacts(
                self.ir,
                self.assignment,
                self.schedule,
                self.platform,
                artifacts,
                reconstruct=False,
            )
        self.assertEqual(validation["status"], "pass")

    def test_round_barrier_register_input_cut_is_cycle_equivalent(self) -> None:
        constraints = normalize_partition_constraints(
            None, self.ir, self.platform
        )
        clusters = build_clusters(self.ir, constraints)
        cluster_by_instance = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        cluster_assignment = {
            cluster["id"]: "fpga0" for cluster in clusters["clusters"]
        }
        cluster_assignment[cluster_by_instance["next_lut[0]"]] = "fpga1"
        assignment = build_partition_assignment(
            self.ir,
            self.platform,
            clusters,
            constraints,
            cluster_assignment,
            provider="test-dependent-cut",
            seed=0,
        )
        route_constraints = normalize_route_constraints(
            None, self.platform, frame_slots=32
        )
        routes = route_system_native(
            assignment,
            self.platform,
            route_constraints,
            executable=str(tlr_router()),
        )
        schedule = build_tdm_schedule(routes, self.platform)
        artifacts = build_split_artifacts(
            self.ir, assignment, schedule, self.platform
        )
        validation = validate_split_artifacts(
            self.ir,
            assignment,
            schedule,
            self.platform,
            artifacts,
        )
        equivalence = simulate_partition_equivalence(
            self.ir, assignment, schedule, cycles=12
        )
        cut_by_net = {
            cut["net"]: cut for cut in assignment["cut_nets"]
        }
        self.assertEqual(cut_by_net["next_q[0]"]["transport_round"], 1)
        self.assertEqual(validation["status"], "pass")
        self.assertEqual(equivalence["register_input_cuts"], 1)
        self.assertEqual(equivalence["transport_rounds"], 2)
        self.assertEqual(equivalence["round_barrier_checks"], 1)
        self.assertEqual(equivalence["mismatches"], 0)

    def test_yosys_constant_connections_are_retained(self) -> None:
        constants = [
            item
            for instance in self.ir.value["instances"]
            for item in instance["constant_connections"]
        ]
        self.assertTrue(constants)
        self.assertTrue(
            all(item["value"] in {"0", "1", "x", "z"} for item in constants)
        )

    def test_async_clear_and_preset_flops_are_modeled(self) -> None:
        for cell_type, old_port, new_port in (
            ("FDCE", "R", "CLR"),
            ("FDPE", "R", "PRE"),
        ):
            with self.subTest(cell_type=cell_type):
                value = copy.deepcopy(self.ir.to_dict())
                target = next(
                    instance
                    for instance in value["instances"]
                    if instance["type"] == "FDRE"
                )
                target["type"] = cell_type
                for connection in target["constant_connections"]:
                    if connection["port"] == old_port:
                        connection["port"] = new_port
                for net in value["nets"]:
                    for endpoint in net["sinks"]:
                        if (
                            endpoint["instance"] == target["id"]
                            and endpoint["port"] == old_port
                        ):
                            endpoint["port"] = new_port
                model = _MappedModel(EmuIR(value))
                state = model.initial_state()
                _, next_state, _ = model.evaluate(
                    state, cycle=0, seed=20260727
                )
                self.assertIn(target["id"], next_state)


if __name__ == "__main__":
    unittest.main()
