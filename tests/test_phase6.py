from tests.phase3_store_fixture import write_assignment_index_fixture
from tests.native_split_fixture import NativeSplitFixture
from tests.native_route_fixture import route_system_native
from tests.transport_reference import write_transport_storage as reference_transport
from tests.schedule_store_fixture import write_tdm_schedule_storage
from tests.pin_plan_fixture import make_pin_plan_fixture
from contextlib import closing
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
from emuflow.chimew_binding_storage import (
    CHIMEW_BINDING_STORAGE_SCHEMA,
    read_chimew_binding,
)
from emuflow.errors import ValidationError
from emuflow.emuir_compact import write_compact_emuir
from emuflow.ir import EmuIR
from emuflow.io import file_sha256, read_json, write_json
from emuflow.netlist import (
    transport_to_systemverilog,
)
from emuflow.partition import (
    build_partition_assignment,
    build_clusters,
    normalize_partition_constraints,
)
from tests.partition_reference import assign_clusters
from emuflow.phase6 import run_phase6, validate_phase6
from emuflow.tdm_schedule_storage import read_tdm_schedule

def _indexed_split_schedule(root,value):
    path=Path(root)/"split-schedule.json"
    write_tdm_schedule_storage(path,value)
    return read_tdm_schedule(path)

from emuflow.split_netlist_storage import (
    SplitNetlistView,
    _schedule_incoming_rows,
    build_streaming_split_metadata,
)
from emuflow.pin_planning import (
    CHIMEW_PIN_PLAN_PROVIDER,
    SIGNAL_POSITION_HINTS_SCHEMA,
    _assignment_metrics,
)
from emuflow.pin_plan_storage import (
    PIN_PLAN_STORAGE_SCHEMA,
    read_pin_plan,
    write_pin_plan_storage,
)
from emuflow.position_hints_storage import (
    POSITION_HINTS_STORAGE_SCHEMA,
    read_position_hints,
)
from emuflow.platform import Platform
from emuflow.open_transport_characterization import (
    BASE_FEATURE_NAMES,
    FRAME_SLOTS,
    OPEN_TRANSPORT_MODEL,
    OPEN_TRANSPORT_PROVENANCE,
)
from emuflow.routing import normalize_route_constraints
from tests.schedule_reference import build_tdm_schedule
from emuflow.tdm_schedule_storage import (open_tdm_schedule_view)
from emuflow.transport_storage import (
    LANE_MAP_STORAGE_SCHEMA,
    TRANSPORT_STORAGE_SCHEMA,
    LaneMapView,
    TransportView,
    write_transport_storage,
)
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
        clusters = build_clusters(self.ir, constraints, cut_mode="sequential-only")
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
        # The bounded legacy scheduling oracle still tests registered transport.
        # Persist current typed completion facts, not its retired round record.
        for route in self.schedule["routes"]:
            route.pop("transport_round", None)
        routes_by_id = {route["id"]: route for route in self.schedule["routes"]}
        for completion in self.schedule["demand_completions"]:
            completion.pop("transport_round", None)
            sinks = routes_by_id[completion["demand"]]["sinks"]
            completion["sink_arrival_slots"] = {
                entry["to"]: entry["arrival_slot"]
                for entry in self.schedule["entries"]
                if entry["demand"] == completion["demand"] and entry["to"] in sinks
            }

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
            write_assignment_index_fixture(
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
                "metrics": {"frame_slots": 8_192,"completion_slot":count},
                "domain_schedules":[],
                "demand_completions":[{"demand":f"demand_{i:06d}","net":f"net_{i:06d}","source_ready_slot":0,"completion_slot":i+1,"sink_arrival_slots":{sink:i+1}} for i in range(count)],
                "routes": [
                    {
                        "id": f"demand_{index:06d}",
                        "net": f"net_{index:06d}",
                        "source": source,"sinks":[sink],"tree_edges":[{"link":link.id,"from":source,"to":sink}],
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
                        "lane": 0,"hop":0,"capacity_key":"fixture-domain","ready_slot":0,
                    }
                    for index in range(count)
                ],
            }
            schedule=_indexed_split_schedule(root,schedule)
            summaries = []
            writer_uri_modes = []
            original_connect = sqlite3.connect

            def recording_connect(database, *args, **kwargs):
                if Path(database).name.startswith(".transport.sqlite."):
                    writer_uri_modes.append(kwargs.get("uri", False))
                return original_connect(database, *args, **kwargs)

            with (
                mock.patch(
                    "emuflow.transport_storage.sqlite3.connect",
                    side_effect=recording_connect,
                ),
                mock.patch(
                    "emuflow.store_publication.file_sha256", wraps=file_sha256
                ) as store_hash,
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
                    self.assertEqual(store_hash.call_count, run + 1)
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
            self.assertEqual(writer_uri_modes, [])  # The host opens no SQLite writer.
            self.assertEqual(summaries[0]["sha256"], summaries[1]["sha256"])

    def test_transport_store_consumes_typed_schedule_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            assignment_path = root / "assignment.sqlite"
            schedule_path = root / "schedule.json"
            write_assignment_index_fixture(
                assignment_path, self.ir, self.assignment
            )
            write_tdm_schedule_storage(schedule_path, self.schedule)

            legacy = reference_transport(
                root / "legacy",
                assignment_path,
                self.schedule,
                self.platform,
                design=self.ir.value["design"],
                clocks=self.ir.value["clocks"],
            )
            with open_tdm_schedule_view(schedule_path) as indexed:
                with mock.patch.object(
                    type(indexed),
                    "iter_entry_route_records",
                    side_effect=AssertionError(
                        "legacy schedule dictionaries were materialized"
                    ),
                ), mock.patch(
                    "emuflow.tdm_schedule_storage.ScheduleRecordSequence.__iter__",
                    side_effect=AssertionError(
                        "schedule sections were decoded in Python"
                    ),
                ):
                    typed = write_transport_storage(
                        root / "typed",
                        assignment_path,
                        indexed,
                        self.platform,
                        design=self.ir.value["design"],
                        clocks=self.ir.value["clocks"],
                    )

            self.assertEqual(
                typed["lane_map"].materialize(),
                legacy["lane_map"].materialize(),
            )
            self.assertEqual(
                {
                    fpga: value.materialize()
                    for fpga, value in typed["transports"].items()
                },
                {
                    fpga: value.materialize()
                    for fpga, value in legacy["transports"].items()
                },
            )
            self.assertEqual(
                {
                    fpga: value.materialize()
                    for fpga, value in typed["anchors"].items()
                },
                {
                    fpga: value.materialize()
                    for fpga, value in legacy["anchors"].items()
                },
            )

    def test_phase6_incoming_join_consumes_persisted_v4_static_exact(self) -> None:
        schedule = {
            "schema": "emuflow.tdm-schedule/v1",
            "design": "phase6-v4-incoming",
            "platform": "three",
            "provider": "deterministic-dependency-earliest-slot-v1",
            "qualification": "dependency-schedule-readiness-pass",
            "transport_semantics": "registered-boundary",
            "route_constraints": {"frame_slots": 8},
            "routes": [
                {
                    "id": "d0",
                    "net": "n0",
                    "source": "a",
                    "sinks": ["c"],
                    "tree_edges": [
                        {"link": "ab", "from": "a", "to": "b"},
                        {"link": "bc", "from": "b", "to": "c"},
                    ],
                }
            ],
            "entries": [
                {
                    "id": f"s{index:06d}",
                    "demand": "d0",
                    "net": "n0",
                    "hop": index,
                    "link": link,
                    "from": source,
                    "to": sink,
                    "capacity_key": f"{link}:{source}:{sink}",
                    "slot": index,
                    "lane": 0,
                    "ready_slot": index,
                    "arrival_slot": index + 1,
                }
                for index, (link, source, sink) in enumerate(
                    (("ab", "a", "b"), ("bc", "b", "c"))
                )
            ],
            "demand_completions": [
                {
                    "demand": "d0",
                    "net": "n0",
                    "source_ready_slot": 0,
                    "completion_slot": 2,
                    "sink_arrival_slots": {"c": 2},
                }
            ],
            "domain_schedules": [],
            "metrics": {"scheduled_bit_hops": 2},
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "schedule.json"
            manifest = write_tdm_schedule_storage(path, schedule)
            self.assertEqual(
                manifest["schema"], "emuflow.tdm-schedule-storage/v4"
            )
            with open_tdm_schedule_view(path) as indexed:
                rows = list(
                    _schedule_incoming_rows(
                        indexed, {"a": 0, "b": 1, "c": 2}
                    )
                )
        self.assertEqual(
            rows, [("n0", 1, "s000000"), ("n0", 2, "s000001")]
        )

    def test_transport_store_joins_indexed_pin_plan_by_entry_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            assignment_path = root / "assignment.sqlite"
            schedule_path = root / "schedule.json"
            write_assignment_index_fixture(
                assignment_path, self.ir, self.assignment
            )
            schedule = copy.deepcopy(self.schedule)
            for index, entry in enumerate(schedule["entries"]):
                entry["id"] = (
                    "s999999" if index == 0 else f"s{999999 + index}"
                )
            write_tdm_schedule_storage(schedule_path, schedule)
            pin_plan = write_pin_plan_storage(
                root / "pin-plan.json",
                {
                    "schema": "emuflow.placement-aware-pin-plan/v1",
                    "provider": CHIMEW_PIN_PLAN_PROVIDER,
                    "design": self.ir.value["design"],
                    "platform": self.platform.name,
                    "entries": [
                        {
                            "schedule_entry": entry["id"],
                            "group": index,
                            "physical_lane": entry["lane"],
                            "logical_lane": entry["lane"],
                        }
                        for index, entry in enumerate(schedule["entries"])
                    ],
                },
            )

            legacy = reference_transport(
                root / "legacy-pin",
                assignment_path,
                schedule,
                self.platform,
                design=self.ir.value["design"],
                clocks=self.ir.value["clocks"],
                pin_plan=pin_plan,
            )
            with open_tdm_schedule_view(schedule_path) as indexed:
                typed = write_transport_storage(
                    root / "typed-pin",
                    assignment_path,
                    indexed,
                    self.platform,
                    design=self.ir.value["design"],
                    clocks=self.ir.value["clocks"],
                    pin_plan=pin_plan,
                )
            self.assertEqual(
                typed["lane_map"].materialize(),
                legacy["lane_map"].materialize(),
            )

    def test_split_exact_coverage_lane_agreement_and_equivalence(self):
        with NativeSplitFixture(self.ir, self.assignment, self.schedule, self.platform) as produced:
            artifacts = produced.artifacts()
            validation = produced.validate()
            self.assertNotIn('semantic_contract', artifacts['manifest'])
            self.assertEqual(validation['status'], 'pass')
            self.assertEqual(validation['instances'], 8)
            self.assertEqual(validation['transport_endpoints'], 2 * validation['scheduled_hops'])
        equivalence = simulate_partition_equivalence(self.ir, self.assignment, self.schedule, cycles=12)
        self.assertEqual(equivalence['status'], 'pass')
        self.assertEqual(equivalence['mismatches'], 0)
        self.assertEqual(equivalence['cycles'], 12)

    def test_split_manifest_carries_phase3_resource_reservation(self):
        assignment = copy.deepcopy(self.assignment)
        reservations = {'fpga0': {'ff': 2, 'lut': 3}, 'fpga1': {'ff': 4, 'lut': 5}}
        assignment['constraints']['resource_reservations_by_fpga'] = reservations
        with NativeSplitFixture(self.ir, assignment, self.schedule, self.platform) as produced:
            self.assertEqual(produced.manifest['resource_reservations_by_fpga'], reservations)
            self.assertEqual(produced.validate()['status'], 'pass')
            broken = copy.deepcopy(produced.manifest)
            broken['resource_reservations_by_fpga']['fpga0']['lut'] += 1
            write_json(produced.manifest_path, broken)
            with self.assertRaisesRegex(ValidationError, 'resource reservations disagree'):
                produced.validate()

    def test_native_split_preserves_original_records_and_scheduled_boundaries(self):
        with NativeSplitFixture(self.ir, self.assignment, self.schedule, self.platform) as produced:
            artifacts = produced.artifacts()
            proof = produced.validate()
            self.assertEqual(proof['copy_coverage_errors'], 0)
            self.assertEqual(proof['shadow_driver_errors'], 0)
            with closing(sqlite3.connect(produced.split)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertEqual(tables, {'metadata','parts','net_segments','shadow_drivers','port_memberships','resources'})
            originals = {row['id']: row for row in self.ir.value['instances']}
            observed = []
            for fpga, netlist in artifacts['netlists'].items():
                expected = {identity for identity, owner in self.assignment['instance_assignment'].items() if owner == fpga}
                replicas = self.assignment.get('replication', {}).get('replica_instance_assignment', {})
                expected.update(identity for identity, owner in replicas.items() if owner == fpga)
                self.assertEqual({row['id'] for row in netlist['instances']}, expected)
                for instance in netlist['instances']:
                    observed.append(instance['id'])
                    if instance['id'] in originals:
                        for key, value in originals[instance['id']].items():
                            self.assertEqual(instance[key], value)
                for segment in netlist['nets']:
                    if segment['source_kind'] != 'transport_shadow':
                        continue
                    incoming = [entry for entry in self.schedule['entries'] if entry['net'] == segment['original_net'] and entry['to'] == fpga]
                    self.assertEqual(len(incoming), 1)
                    self.assertEqual(segment['drivers'], [{'instance': '__emuflow_rx_' + incoming[0]['id'], 'port': 'shadow_out', 'bit': 0}])
            self.assertEqual(len(observed), len(set(observed)))
            endpoints = [endpoint for transport in artifacts['transports'].values() for endpoint in transport['endpoints']]
            self.assertEqual({row['id'] for row in endpoints}, {'__emuflow_' + kind + '_' + entry['id'] for kind in ('tx','rx') for entry in self.schedule['entries']})
            self.assertEqual(len(endpoints), 2 * len(self.schedule['entries']))
            lanes = {row['schedule_entry']: row for row in artifacts['lane_map']['entries']}
            for entry in self.schedule['entries']:
                lane = lanes[entry['id']]
                self.assertEqual((lane['link'],lane['from'],lane['to'],lane['slot'],lane['lane']), (entry['link'],entry['from'],entry['to'],entry['slot'],entry['lane']))
            self.assertEqual(produced.audit_transport()['status'], 'pass')

    def test_large_sqlite_phase6_never_enters_python_graph_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.sqlite"
            assignment_path = root / "assignment.sqlite"
            schedule_path = root / "schedule.json"
            output_path = root / "phase6"
            value=self.ir.to_dict();assignment=copy.deepcopy(self.assignment)
            template=next(item for item in value['instances'] if item.get('resources',{}).get('lut',0)>0)
            for index in range(10001):
                cell=copy.deepcopy(template);cell['id']=f'unconnected_lut_{index}'
                value['instances'].append(cell);assignment['instance_assignment'][cell['id']]='fpga0'
            large=EmuIR(value);large.validate()
            write_compact_emuir(ir_path,large.to_dict())
            write_assignment_index_fixture(assignment_path,large,assignment)
            write_tdm_schedule_storage(schedule_path, self.schedule)
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
        reservations = {"fpga0": {"ff": 2, "lut": 3},
                        "fpga1": {"ff": 4, "lut": 5}}
        self.assignment["constraints"]["resource_reservations_by_fpga"] = reservations
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "design.emuir.sqlite"
            assignment_path = root / "assignment.json"
            schedule_path = root / "schedule.json"
            write_compact_emuir(ir_path, self.ir.to_dict())
            assignment_path.write_text(
                json.dumps(self.assignment), encoding="utf-8"
            )
            write_assignment_index_fixture(
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

            manifest = read_json(output / "manifest.json")
            self.assertEqual(manifest["resource_reservations_by_fpga"], reservations)
            for bad in ({}, {"fpga0": {"lut": 999}}):
                write_json(output / "manifest.json", {
                    **manifest, "resource_reservations_by_fpga": bad})
                with self.assertRaisesRegex(ValidationError, "resource reservations disagree"):
                    validate_phase6(ir_path, assignment_path, schedule_path,
                                    PLATFORM_PATH, output / "manifest.json")
            write_json(output / "manifest.json", manifest)

            legacy_control = build_streaming_split_metadata(
                root / "assignment.sqlite",
                self.schedule,
                self.platform,
                design=self.ir.value["design"],
                clocks=self.ir.value["clocks"],
            )
            self.assertEqual(legacy_control["manifest"]["resource_reservations_by_fpga"], reservations)
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
            write_assignment_index_fixture(
                root / "assignment.sqlite", self.ir, self.assignment
            )
            schedule = copy.deepcopy(self.schedule)
            schedule["metrics"]["frame_slots"] = 8
            write_tdm_schedule_storage(schedule_path,schedule)
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

    def test_phase6_materializes_validated_chimew_pin_contract(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            schedule = copy.deepcopy(self.schedule)
            for entry in schedule["entries"]:
                entry["tdm_ratio"] = schedule["metrics"]["frame_slots"]
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
            plan = make_pin_plan_fixture(
                schedule,
                self.platform,
                positions,
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
            write_assignment_index_fixture(
                root / "assignment.sqlite", self.ir, self.assignment
            )
            for name, path in paths.items():
                if name == "ir":
                    continue
                path.write_text(
                    json.dumps(documents[name]), encoding="utf-8"
                )
            write_tdm_schedule_storage(paths["schedule"],schedule)
            plan, binding = self._chimew_certificate(schedule, positions, plan)
            write_json(paths["plan"], plan)
            binding_path = root / "initial-electrical-binding.json"
            write_json(binding_path, binding)
            output = root / "phase6"
            report = run_phase6(
                paths["ir"], paths["assignment"], paths["schedule"], PLATFORM_PATH, output,
                equivalence_cycles=4, pin_plan_path=paths["plan"],
                position_hints_path=paths["positions"], electrical_binding_path=binding_path,
            )
            self.assertEqual(report["provider"], "chimew-electrical-pin-split-v1")
            self.assertEqual(
                report["pin_plan_validation"]["status"], "pass"
            )
            self.assertEqual(report["artifacts"]["pin_plan"], "pin_plan.json")
            self.assertTrue((output / "pin_plan.json").is_file())
            pin_selector = read_json(output / "pin_plan.json")
            self.assertTrue((output / pin_selector["artifact"]).is_file())
            self.assertEqual(pin_selector["schema"], PIN_PLAN_STORAGE_SCHEMA)
            self.assertEqual(
                report["artifacts"]["pin_plan_store"],
                pin_selector["artifact"],
            )
            self.assertLess((output / "pin_plan.json").stat().st_size, 4096)
            self.assertEqual(
                read_pin_plan(output / "pin_plan.json", materialize=True),
                plan,
            )
            self.assertTrue((output / "position_hints.json").is_file())
            position_selector = read_json(output / "position_hints.json")
            self.assertTrue(
                (output / position_selector["artifact"]).is_file()
            )
            self.assertEqual(
                position_selector["schema"], POSITION_HINTS_STORAGE_SCHEMA
            )
            self.assertEqual(
                report["artifacts"]["position_hints_store"],
                position_selector["artifact"],
            )
            self.assertLess(
                (output / "position_hints.json").stat().st_size, 4096
            )
            self.assertEqual(
                read_position_hints(
                    output / "position_hints.json", materialize=True
                ),
                positions,
            )
            chimew_plan, binding = self._chimew_certificate(
                schedule, positions, plan
            )
            chimew_plan_path = root / "chimew-plan.json"
            binding_path = root / "electrical-binding.json"
            write_json(chimew_plan_path, chimew_plan)
            write_json(binding_path, binding)
            chimew_output = root / "phase6-chimew"
            invalid_output = root / "phase6-chimew-without-binding"
            with self.assertRaisesRegex(ValueError, "electrical_binding_path"):
                run_phase6(
                    paths["ir"],
                    paths["assignment"],
                    paths["schedule"],
                    PLATFORM_PATH,
                    invalid_output,
                    equivalence_cycles=4,
                    pin_plan_path=chimew_plan_path,
                    position_hints_path=paths["positions"],
                )
            self.assertFalse((invalid_output / "pin_plan.json").exists())
            self.assertFalse(list(invalid_output.glob("pin_plan.*.sqlite")))
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
            binding_selector = read_json(
                chimew_output / chimew_report["artifacts"]["electrical_binding"]
            )
            self.assertEqual(
                binding_selector["schema"], CHIMEW_BINDING_STORAGE_SCHEMA
            )
            self.assertEqual(
                chimew_report["artifacts"]["electrical_binding_store"],
                binding_selector["artifact"],
            )
            relocated_binding = root / "relocated-binding"
            relocated_binding.mkdir()
            for role in ("electrical_binding", "electrical_binding_store"):
                source = chimew_output / chimew_report["artifacts"][role]
                shutil.copy2(source, relocated_binding / source.name)
            self.assertEqual(
                read_chimew_binding(
                    relocated_binding / "electrical_binding.json",
                    materialize=True,
                ),
                binding,
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

    def test_transport_lane_endpoint_and_collision_corruption_are_rejected(self):
        constraints = normalize_partition_constraints(None, self.ir, self.platform)
        clusters = build_clusters(self.ir, constraints, cut_mode='sequential-only')
        owners = {cluster['id']: ('fpga0' if any(identity in {'next_lut[0]', 'next_lut[2]', 'q_reg[0]', 'q_reg[2]'}
                  for identity in cluster['instances']) else 'fpga1')
                  for cluster in clusters['clusters']}
        assignment = build_partition_assignment(self.ir, self.platform, clusters,
            constraints, owners, provider='test-same-direction-collision', seed=0)
        routes = route_system_native(assignment, self.platform,
            normalize_route_constraints(None, self.platform, frame_slots=32),
            executable=str(tlr_router()))
        schedule = build_tdm_schedule(routes, self.platform)
        routes_by_id = {route['id']: route for route in schedule['routes']}
        for route in schedule['routes']:
            route.pop('transport_round', None)
        for completion in schedule['demand_completions']:
            completion.pop('transport_round', None)
            sinks = routes_by_id[completion['demand']]['sinks']
            completion['sink_arrival_slots'] = {
                entry['to']: entry['arrival_slot'] for entry in schedule['entries']
                if entry['demand'] == completion['demand'] and entry['to'] in sinks}
        for damage in ('lane', 'endpoint', 'collision'):
            with self.subTest(damage=damage), NativeSplitFixture(self.ir, assignment, schedule, self.platform) as produced:
                self.assertEqual(produced.audit_transport()['status'], 'pass')
                with closing(sqlite3.connect(produced.transport)) as db, db:
                    if damage == 'lane':
                        changed = db.execute('UPDATE hops SET lane=lane+1 WHERE entry_id=(SELECT entry_id FROM hops ORDER BY entry_id LIMIT 1)')
                    elif damage == 'endpoint':
                        changed = db.execute("UPDATE hops SET net='foreign-logical-net' WHERE entry_id=(SELECT entry_id FROM hops ORDER BY entry_id LIMIT 1)")
                    else:
                        # This link has independent full-duplex directions; a
                        # real collision must share direction as well as lane.
                        collision_count = 'SELECT count(*) FROM (SELECT 1 FROM hops GROUP BY link_id,source_fpga,sink_fpga,slot,lane HAVING count(*)>1)'
                        self.assertEqual(db.execute(collision_count).fetchone()[0], 0)
                        first = db.execute('SELECT entry_id,link_id,source_fpga,sink_fpga,slot,lane,arrival_slot FROM hops ORDER BY entry_id LIMIT 1').fetchone()
                        other = db.execute('SELECT entry_id,slot,lane,arrival_slot FROM hops WHERE entry_id!=? AND link_id=? AND source_fpga=? AND sink_fpga=? LIMIT 1', first[:4]).fetchone()
                        self.assertIsNotNone(other)
                        self.assertNotEqual(tuple(other[1:]), tuple(first[4:]))
                        changed = db.execute('UPDATE hops SET slot=?,lane=?,arrival_slot=? WHERE entry_id=?', (*first[4:],other[0]))
                        self.assertGreater(db.execute(collision_count).fetchone()[0], 0)
                    self.assertEqual(changed.rowcount, 1)
                with self.assertRaises(ValidationError):
                    produced.audit_transport()

    def test_independent_split_validation_does_not_invoke_the_producer(self):
        with NativeSplitFixture(self.ir, self.assignment, self.schedule, self.platform) as produced:
            with mock.patch('emuflow.split_overlay.build_native_split', side_effect=AssertionError('split producer replayed')):
                self.assertEqual(produced.validate_split()['status'], 'pass')

    def test_retired_round_barrier_register_input_route_is_rejected(self) -> None:
        constraints = normalize_partition_constraints(
            None, self.ir, self.platform
        )
        clusters = build_clusters(self.ir, constraints, cut_mode="sequential-only")
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
        with self.assertRaisesRegex(ValidationError, "round-barrier routing is retired"):
            route_system_native(
                assignment, self.platform, route_constraints,
                executable=str(tlr_router()),
            )

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
