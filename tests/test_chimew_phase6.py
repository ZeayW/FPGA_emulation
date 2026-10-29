from tests.chimew_bank_fixture import build_chimew_phase6_pin_plan
from tests.chimew_bank_fixture import evaluate_chimew_bank_channel_assignment
from tests.schedule_store_fixture import write_tdm_schedule_storage
from contextlib import closing
import copy
import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.native_build import chimew_bank_channel_assigner

from emuflow.transport.chimew.chimew_bank_channel import (
    CHIMEW_BANK_CHANNEL_INPUT_PROVIDER,
    CHIMEW_BANK_CHANNEL_INPUT_SCHEMA,
    validate_chimew_bank_channel_input,
)
from emuflow.transport.chimew.chimew_bank_channel_storage import (
    write_chimew_bank_channel_storage,
)
from emuflow.transport.chimew.chimew_binding_storage import (
    CHIMEW_BINDING_STORAGE_SCHEMA,
    ChimewBindingEntrySequence,
    ChimewBindingView,
    open_chimew_binding_schedule_entry_lookup,
    read_chimew_binding,
    write_chimew_binding_storage,
)
from emuflow.transport.chimew.chimew_phase6 import (
    CHIMEW_ELECTRICAL_MAP_PROVIDER,
    CHIMEW_ELECTRICAL_MAP_SCHEMA,
    CHIMEW_ELECTRICAL_MAP_SCHEMA_V1,
    CHIMEW_PHASE6_BINDING_PROVIDER,
    chimew_adapter_report_generation_path,
    run_chimew_phase6_adapter,
    validate_chimew_adapter_generation,
    validate_chimew_electrical_map,
    validate_chimew_phase6_binding,
)
from emuflow.transport.chimew.chimew_qualification import (
    CHIMEW_QUALIFICATION_PROVIDER,
    CHIMEW_QUALIFICATION_SCHEMA,
    canonical_sha256,
)
from emuflow.flow.cli import main
from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json, write_json
from emuflow.transport.pin_planning import CHIMEW_PIN_PLAN_PROVIDER, validate_pin_plan
from emuflow.transport.pin_plan_storage import (
    PinPlanEntrySequence,
    write_pin_plan_storage,
)
from emuflow.platform.platform import Platform
from emuflow.scheduling.tdm_schedule_storage import (
    ScheduleRecordSequence,
    TDM_SCHEDULE_STORAGE_SCHEMA,
    TypedScheduleEntrySequence,
    canonical_tdm_schedule_sha256,
    read_tdm_schedule,
)


ROOT = Path(__file__).resolve().parents[1]


class ChimewPhase6AdapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.executable = chimew_bank_channel_assigner()

    def setUp(self) -> None:
        self.platform_document = {
            "schema": "emuflow.boarddb/v1",
            "platform": {
                "name": "chimew_two_fpga",
                "kind": "hardware",
                "description": "electrical adapter fixture",
            },
            "fpgas": [
                {
                    "id": fpga,
                    "part": "fixture",
                    "utilization_limit": 1.0,
                    "capacity": {"lut": 100},
                }
                for fpga in ("A", "B")
            ],
            "links": [
                {
                    "id": "AB_link",
                    "endpoints": ["A", "B"],
                    "direction": "full_duplex",
                    "mode": "parallel",
                    "data_lanes_per_direction": 2,
                    "fabric_clock_mhz": 100.0,
                    "latency_cycles": 1,
                },
            ],
        }
        self.platform = Platform.from_dict(self.platform_document)
        self.schedule = {
            "schema": "emuflow.tdm-schedule/v1",
            "design": "chimew_fixture",
            "platform": self.platform.name,
            "metrics": {"frame_slots": 4},
            "entries": [
                {
                    "id": "s0",
                    "demand": "demand0", "net": "net0",
                    "hop": 0, "capacity_key": "AB_link:A:B",
                    "ready_slot": 0, "arrival_slot": 1,
                    "link": "AB_link",
                    "from": "A",
                    "to": "B",
                    "tdm_ratio": 4,
                    "lane": 0,
                    "slot": 0,
                },
                {
                    "id": "s1",
                    "demand": "demand1", "net": "net1",
                    "hop": 0, "capacity_key": "AB_link:B:A",
                    "ready_slot": 0, "arrival_slot": 1,
                    "link": "AB_link",
                    "from": "B",
                    "to": "A",
                    "tdm_ratio": 4,
                    "lane": 0,
                    "slot": 0,
                },
            ],
        }
        self.assignment_input = {
            "schema": CHIMEW_BANK_CHANNEL_INPUT_SCHEMA,
            "provider": CHIMEW_BANK_CHANNEL_INPUT_PROVIDER,
            "design": self.schedule["design"],
            "platform": self.platform.name,
            "coordinate_system": "physical-site-xy",
            "cost_quantization_per_site": 1000,
            "provenance": {
                "producer": "fixture-lookahead",
                "producer_version": "1",
                "grouping_sha256": "a" * 64,
                "placement_sha256": "b" * 64,
                "architecture_sha256": "c" * 64,
            },
            "domains": [{"id": "AB", "fpga_a": "A", "fpga_b": "B"}],
            "bank_pairs": [
                {
                    "id": "bank0",
                    "domain": "AB",
                    "bank_a": {"id": "A0", "x": 0.0, "y": 0.0},
                    "bank_b": {"id": "B0", "x": 100.0, "y": 0.0},
                    "channels": [
                        {
                            "id": f"channel{lane}",
                            "order": lane,
                            "pin_a": {"x": 0.0, "y": float(lane * 50)},
                            "pin_b": {"x": 100.0, "y": float(lane * 50)},
                        }
                        for lane in range(2)
                    ],
                }
            ],
            "groups": [
                {
                    "id": "group_ab",
                    "domain": "AB",
                    "kind": "tdm_group",
                    "direction": "a_to_b",
                    "members": [
                        {
                            "id": "s0",
                            "fanout": {"x": 0.0, "y": 10.0},
                            "fanins": [{"x": 100.0, "y": 10.0}],
                        }
                    ],
                },
                {
                    "id": "group_ba",
                    "domain": "AB",
                    "kind": "tdm_group",
                    "direction": "b_to_a",
                    "members": [
                        {
                            "id": "s1",
                            "fanout": {"x": 100.0, "y": 50.0},
                            "fanins": [{"x": 0.0, "y": 50.0}],
                        }
                    ],
                },
            ],
            "metrics": {
                "groups": 2,
                "signals": 2,
                "fanins": 2,
                "bank_pairs": 1,
                "channels": 2,
            },
        }
        self.electrical_map = {
            "schema": CHIMEW_ELECTRICAL_MAP_SCHEMA,
            "provider": CHIMEW_ELECTRICAL_MAP_PROVIDER,
            "design": self.schedule["design"],
            "platform": self.platform.name,
            "provenance": {
                "producer": "fixture-bsp",
                "producer_version": "1",
                "boarddb_sha256": "d" * 64,
                "package_pin_inventory_sha256": "e" * 64,
            },
            "fpga_y_bounds": [
                {"fpga": "A", "y_min": 0.0, "y_max": 100.0},
                {"fpga": "B", "y_min": 0.0, "y_max": 100.0},
            ],
            "channels": [
                {
                    "chimew_channel": f"channel{lane}",
                    "link": "AB_link",
                    "physical_lane": lane,
                    "direction": "either",
                    "bank_a": "A0",
                    "bank_b": "B0",
                    "package_pin_a": f"A{lane}",
                    "package_pin_b": f"B{lane}",
                    "iostandard": "LVCMOS18",
                    "supported_iostandards": ["LVCMOS18"],
                    "bank_voltage": 1.8,
                    "electrical_class": "single_ended_parallel",
                    "reserved": False,
                }
                for lane in range(2)
            ],
            "metrics": {"channels": 2, "package_pins": 4, "concrete_lanes": 2},
        }

    def test_certified_assignment_becomes_a_valid_phase6_pin_plan(self) -> None:
        result = build_chimew_phase6_pin_plan(
            self.schedule,
            self.platform,
            self.assignment_input,
            self.electrical_map,
            executable=str(self.executable),
            region_count=4,
        )
        repeated = build_chimew_phase6_pin_plan(
            self.schedule,
            self.platform,
            self.assignment_input,
            self.electrical_map,
            executable=str(self.executable),
            region_count=4,
        )
        self.assertEqual(result, repeated)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["provider"], CHIMEW_PHASE6_BINDING_PROVIDER)
        self.assertEqual(result["pin_plan"]["provider"], CHIMEW_PIN_PLAN_PROVIDER)
        self.assertEqual(
            result["electrical_binding"]["integration_status"],
            "phase6-pin-plan",
        )
        self.assertEqual(
            result["electrical_binding"]["metrics"]["package_pin_collisions"], 0
        )
        self.assertEqual(
            result["electrical_binding"]["fpga_y_bounds"],
            [
                {"fpga": "A", "y_min": 0.0, "y_max": 100.0},
                {"fpga": "B", "y_min": 0.0, "y_max": 100.0},
            ],
        )
        for entry in result["electrical_binding"]["entries"]:
            lane = entry["physical_lane"]
            self.assertEqual(
                entry["pin_a_point"], {"x": 0.0, "y": float(lane * 50)}
            )
            self.assertEqual(
                entry["pin_b_point"], {"x": 100.0, "y": float(lane * 50)}
            )

    def test_v2_binding_rejects_missing_physical_channel_coordinates(self) -> None:
        result = build_chimew_phase6_pin_plan(
            self.schedule,
            self.platform,
            self.assignment_input,
            self.electrical_map,
            executable=str(self.executable),
            region_count=4,
        )
        binding = copy.deepcopy(result["electrical_binding"])
        binding["entries"][0].pop("pin_a_point")
        with self.assertRaisesRegex(ValidationError, "physical pin point"):
            validate_chimew_phase6_binding(
                self.schedule, self.platform, result["pin_plan"], binding
            )

    def test_complete_lookahead_certificate_is_bound_into_the_plan(self) -> None:
        bank_report = evaluate_chimew_bank_channel_assignment(
            self.assignment_input, executable=str(self.executable)
        )
        certificate = {
            "schema": CHIMEW_QUALIFICATION_SCHEMA,
            "status": "pass",
            "design": self.schedule["design"],
            "platform": self.schedule["platform"],
            "provider": CHIMEW_QUALIFICATION_PROVIDER,
            "provenance": {
                "routing_sha256": "f" * 64,
                "placement_sha256": "b" * 64,
                "netlist_sha256": "d" * 64,
                "architecture_sha256": "c" * 64,
            },
            "artifacts": {
                "schedule": canonical_sha256(self.schedule),
                "refined_grouping": "a" * 64,
                "bank_channel_input": canonical_sha256(self.assignment_input),
                "bank_channel_report": canonical_sha256(bank_report),
            },
            "metrics": {
                "signals": 2,
                "groups": 2,
                "rudy_overloaded_bins": 0,
                "artifact_chain_disagreements": 0,
            },
        }
        certificate["qualification_sha256"] = canonical_sha256(certificate)
        result = build_chimew_phase6_pin_plan(
            self.schedule,
            self.platform,
            self.assignment_input,
            self.electrical_map,
            qualification_document=certificate,
            bank_channel_report_document=bank_report,
            executable=str(self.executable),
        )
        self.assertEqual(
            result["pin_plan"]["configuration"]["lookahead_qualification"],
            "complete-artifact-chain",
        )
        self.assertEqual(
            result["electrical_binding"]["provenance"]["qualification_sha256"],
            certificate["qualification_sha256"],
        )
        tampered = copy.deepcopy(certificate)
        tampered["metrics"]["signals"] = 3
        with self.assertRaisesRegex(ValidationError, "self-seal"):
            build_chimew_phase6_pin_plan(
                self.schedule,
                self.platform,
                self.assignment_input,
                self.electrical_map,
                qualification_document=tampered,
                bank_channel_report_document=bank_report,
                executable=str(self.executable),
            )
        self.assertEqual(
            validate_pin_plan(
                self.schedule,
                self.platform,
                result["position_hints"],
                result["pin_plan"],
            )["status"],
            "pass",
        )
        self.assertEqual(
            validate_chimew_phase6_binding(
                self.schedule,
                self.platform,
                result["pin_plan"],
                result["electrical_binding"],
            )["status"],
            "pass",
        )
        plan_by_id = {
            entry["schedule_entry"]: entry for entry in result["pin_plan"]["entries"]
        }
        for schedule_entry in self.schedule["entries"]:
            self.assertEqual(
                plan_by_id[schedule_entry["id"]]["logical_lane"],
                schedule_entry["lane"],
            )

    def test_indexed_binding_validation_uses_relational_join(self) -> None:
        result = build_chimew_phase6_pin_plan(
            self.schedule,
            self.platform,
            self.assignment_input,
            self.electrical_map,
            executable=str(self.executable),
            region_count=4,
        )
        schedule_document = {
            **self.schedule,
            "routes": [],
            "demand_completions": [],
            "domain_schedules": [],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            schedule_path = root / "schedule.json"
            write_tdm_schedule_storage(schedule_path, schedule_document)
            indexed_schedule = read_tdm_schedule(schedule_path)
            schedule_sha256 = canonical_tdm_schedule_sha256(indexed_schedule)
            plan_document = copy.deepcopy(result["pin_plan"])
            plan_document["configuration"]["schedule_sha256"] = (
                schedule_sha256
            )
            binding = copy.deepcopy(result["electrical_binding"])
            binding["provenance"]["schedule_sha256"] = schedule_sha256
            indexed_plan = write_pin_plan_storage(
                root / "pin_plan.json", plan_document
            )
            legacy_validation = validate_chimew_phase6_binding(
                schedule_document,
                self.platform,
                plan_document,
                binding,
            )
            with mock.patch.object(
                ScheduleRecordSequence,
                "__iter__",
                side_effect=AssertionError(
                    "Chimew indexed validation scanned schedule objects"
                ),
            ), mock.patch.object(
                PinPlanEntrySequence,
                "__iter__",
                side_effect=AssertionError(
                    "Chimew indexed validation scanned pin-plan objects"
                ),
            ):
                indexed_validation = validate_chimew_phase6_binding(
                    indexed_schedule,
                    self.platform,
                    indexed_plan,
                    binding,
                )
            self.assertEqual(indexed_validation, legacy_validation)

            incomplete = copy.deepcopy(binding)
            incomplete["entries"].pop()
            with self.assertRaisesRegex(
                ValidationError, "coverage is incomplete"
            ):
                validate_chimew_phase6_binding(
                    indexed_schedule,
                    self.platform,
                    indexed_plan,
                    incomplete,
                )

            conflicting = copy.deepcopy(binding)
            conflicting["entries"][0]["physical_lane"] = 1
            with self.assertRaisesRegex(
                ValidationError, "conflicts with pin plan"
            ):
                validate_chimew_phase6_binding(
                    indexed_schedule,
                    self.platform,
                    indexed_plan,
                    conflicting,
                )

            duplicate = copy.deepcopy(binding)
            duplicate["entries"][1]["schedule_entries"] = list(
                duplicate["entries"][0]["schedule_entries"]
            )
            with self.assertRaisesRegex(
                ValidationError, "duplicates schedule entries"
            ):
                validate_chimew_phase6_binding(
                    indexed_schedule,
                    self.platform,
                    indexed_plan,
                    duplicate,
                )

            wrong_direction = copy.deepcopy(binding)
            wrong_direction["entries"][0]["direction"] = "b_to_a"
            with self.assertRaisesRegex(
                ValidationError, "TDM bundle is invalid"
            ):
                validate_chimew_phase6_binding(
                    indexed_schedule,
                    self.platform,
                    indexed_plan,
                    wrong_direction,
                )

            original_connect = sqlite3.connect
            observed_connections = []

            class ObservedConnection(sqlite3.Connection):
                close_calls = 0

                def close(self):
                    self.close_calls += 1
                    return super().close()

            def observed_connect(*args, **kwargs):
                kwargs["factory"] = ObservedConnection
                connection = original_connect(*args, **kwargs)
                observed_connections.append(connection)
                return connection

            malformed_group = copy.deepcopy(binding)
            malformed_group["entries"][0]["group"] = None
            with mock.patch(
                "emuflow.transport.chimew.chimew_phase6.sqlite3.connect",
                side_effect=observed_connect,
            ):
                with self.assertRaisesRegex(
                    ValidationError, r"electrical_binding\[0\].group"
                ):
                    validate_chimew_phase6_binding(
                        indexed_schedule,
                        self.platform,
                        indexed_plan,
                        malformed_group,
                    )
            self.assertEqual(len(observed_connections), 1)
            self.assertEqual(observed_connections[0].close_calls, 1)

    def test_static_exact_v4_binding_validation_uses_typed_relation(self) -> None:
        result = build_chimew_phase6_pin_plan(
            self.schedule,
            self.platform,
            self.assignment_input,
            self.electrical_map,
            executable=str(self.executable),
            region_count=4,
        )
        entries = [
            {
                **entry,
                "demand": f"demand{index}",
                "net": f"net{index}",
                "hop": 0,
                "capacity_key": f"AB_link:{entry['from']}:{entry['to']}",
                "ready_slot": entry["slot"],
                "arrival_slot": entry["slot"] + 1,
            }
            for index, entry in enumerate(self.schedule["entries"])
        ]
        schedule_document = {
            **self.schedule,
            "provider": "deterministic-dependency-earliest-slot-v1",
            "routes": [
                {"id": entry["demand"], "net": entry["net"],
                 "source": entry["from"], "sinks": [entry["to"]],
                 "tree_edges": [{"link": entry["link"], "from": entry["from"], "to": entry["to"]}]}
                for entry in entries
            ],
            "entries": entries,
            "demand_completions": [],
            "domain_schedules": [],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            schedule_path = root / "schedule.json"
            manifest = write_tdm_schedule_storage(
                schedule_path, schedule_document
            )
            self.assertEqual(
                manifest["schema"], TDM_SCHEDULE_STORAGE_SCHEMA
            )
            indexed_schedule = read_tdm_schedule(schedule_path)
            schedule_sha256 = canonical_tdm_schedule_sha256(indexed_schedule)
            plan_document = copy.deepcopy(result["pin_plan"])
            plan_document["configuration"]["schedule_sha256"] = (
                schedule_sha256
            )
            indexed_plan = write_pin_plan_storage(
                root / "pin_plan.json", plan_document
            )
            binding = copy.deepcopy(result["electrical_binding"])
            binding["provenance"]["schedule_sha256"] = schedule_sha256
            indexed_binding = write_chimew_binding_storage(
                root / "electrical_binding.json", binding
            )
            with mock.patch.object(
                TypedScheduleEntrySequence,
                "__iter__",
                side_effect=AssertionError(
                    "Chimew v4 validation expanded typed schedule entries"
                ),
            ):
                with mock.patch.object(
                    ChimewBindingEntrySequence,
                    "__iter__",
                    side_effect=AssertionError(
                        "Chimew validation expanded typed binding members"
                    ),
                ):
                    validation = validate_chimew_phase6_binding(
                        indexed_schedule,
                        self.platform,
                        indexed_plan,
                        indexed_binding,
                    )
            self.assertEqual(validation["status"], "pass")
            self.assertEqual(validation["signals"], len(entries))

    def test_indexed_producer_never_materializes_typed_schedule_hops(self) -> None:
        entries = [
            {
                **entry,
                "demand": f"demand{index}",
                "net": f"net{index}",
                "hop": 0,
                "capacity_key": f"AB_link:{entry['from']}:{entry['to']}",
                "ready_slot": entry["slot"],
                "arrival_slot": entry["slot"] + 1,
            }
            for index, entry in enumerate(self.schedule["entries"])
        ]
        schedule_document = {
            **self.schedule,
            "provider": "deterministic-dependency-earliest-slot-v1",
            "routes": [
                {"id": entry["demand"], "net": entry["net"],
                 "source": entry["from"], "sinks": [entry["to"]],
                 "tree_edges": [{"link": entry["link"], "from": entry["from"], "to": entry["to"]}]}
                for entry in entries
            ],
            "entries": entries,
            "demand_completions": [],
            "domain_schedules": [],
        }
        legacy = build_chimew_phase6_pin_plan(
            schedule_document,
            self.platform,
            self.assignment_input,
            self.electrical_map,
            executable=str(self.executable),
            region_count=4,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            schedule_path = root / "schedule.json"
            write_tdm_schedule_storage(schedule_path, schedule_document)
            indexed_schedule = read_tdm_schedule(schedule_path)
            with mock.patch.object(
                TypedScheduleEntrySequence,
                "__iter__",
                side_effect=AssertionError(
                    "Chimew producer expanded typed schedule entries"
                ),
            ):
                indexed = build_chimew_phase6_pin_plan(
                    indexed_schedule,
                    self.platform,
                    self.assignment_input,
                    self.electrical_map,
                    executable=str(self.executable),
                    region_count=4,
                    position_hints_output_path=root / "position_hints.json",
                    pin_plan_output_path=root / "pin_plan.json",
                    electrical_binding_output_path=root / "electrical_binding.json",
                )
            self.assertEqual(
                indexed["position_hints"].materialize(),
                legacy["position_hints"],
            )
            self.assertEqual(indexed["pin_plan"].materialize(), legacy["pin_plan"])
            self.assertEqual(
                indexed["electrical_binding"].materialize(),
                legacy["electrical_binding"],
            )

    def test_indexed_binding_round_trip_and_tamper_rejection(self) -> None:
        result = build_chimew_phase6_pin_plan(
            self.schedule,
            self.platform,
            self.assignment_input,
            self.electrical_map,
            executable=str(self.executable),
            region_count=4,
        )
        legacy_validation = validate_chimew_phase6_binding(
            self.schedule,
            self.platform,
            result["pin_plan"],
            result["electrical_binding"],
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            selector = root / "electrical_binding.json"
            indexed = write_chimew_binding_storage(
                selector, result["electrical_binding"]
            )
            self.assertEqual(
                read_json(selector)["schema"], CHIMEW_BINDING_STORAGE_SCHEMA
            )
            self.assertEqual(
                indexed.materialize(), result["electrical_binding"]
            )
            repeated = write_chimew_binding_storage(
                root / "repeated" / "electrical_binding.json",
                result["electrical_binding"],
            )
            self.assertEqual(
                repeated.manifest["store_sha256"],
                indexed.manifest["store_sha256"],
            )
            self.assertEqual(
                repeated.store_path.read_bytes(), indexed.store_path.read_bytes()
            )
            indexed_validation = validate_chimew_phase6_binding(
                self.schedule,
                self.platform,
                result["pin_plan"],
                read_chimew_binding(selector),
            )
            self.assertEqual(indexed_validation, legacy_validation)

            class EntriesMustStayLazy:
                def __iter__(self):
                    raise AssertionError("typed Chimew members were expanded")

            indexed._entries = EntriesMustStayLazy()
            with open_chimew_binding_schedule_entry_lookup(
                indexed
            ) as lookup_binding:
                first = lookup_binding("s0")
                self.assertIsNotNone(first)
                self.assertEqual(first["group"], "group_ab")
                self.assertNotIn("schedule_entries", first)
                self.assertIsNone(lookup_binding("missing-entry"))

            manifest = read_json(selector)
            malformed = copy.deepcopy(manifest)
            malformed["members"]["records"] += 1
            write_json(selector, malformed, compact=True)
            with self.assertRaisesRegex(ValidationError, "logical seal"):
                ChimewBindingView(selector)

            write_json(selector, manifest, compact=True)
            with closing(sqlite3.connect(indexed.store_path)) as connection, connection:
                connection.execute(
                    "UPDATE members SET member='tampered' WHERE member='s0'"
                )
            with self.assertRaisesRegex(ValidationError, "store seal"):
                ChimewBindingView(selector)

        duplicate = copy.deepcopy(result["electrical_binding"])
        duplicate["entries"][1]["schedule_entries"] = ["s0"]
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(
                ValidationError, "repeats a group or schedule entry"
            ):
                write_chimew_binding_storage(
                    Path(temporary_directory) / "electrical_binding.json",
                    duplicate,
                )

    def test_duplicate_concrete_lane_is_rejected(self) -> None:
        electrical_map = copy.deepcopy(self.electrical_map)
        electrical_map["channels"][0]["direction"] = "a_to_b"
        electrical_map["channels"][1]["physical_lane"] = 0
        electrical_map["channels"][1]["direction"] = "a_to_b"
        with self.assertRaisesRegex(ValidationError, "concrete lane"):
            build_chimew_phase6_pin_plan(
                self.schedule,
                self.platform,
                self.assignment_input,
                electrical_map,
                executable=str(self.executable),
            )

    def test_legacy_map_preserves_exclusive_lane_semantics(self) -> None:
        electrical_map = copy.deepcopy(self.electrical_map)
        electrical_map["schema"] = CHIMEW_ELECTRICAL_MAP_SCHEMA_V1
        for channel in electrical_map["channels"]:
            channel.pop("direction")
        electrical_map["channels"][1]["physical_lane"] = 0
        problem = validate_chimew_bank_channel_input(self.assignment_input)
        with self.assertRaisesRegex(ValidationError, "concrete lane"):
            validate_chimew_electrical_map(
                electrical_map, self.platform, problem
            )

    def test_full_duplex_directions_may_reuse_lane_index(self) -> None:
        electrical_map = copy.deepcopy(self.electrical_map)
        electrical_map["channels"][0]["physical_lane"] = 0
        electrical_map["channels"][0]["direction"] = "either"
        electrical_map["channels"][1]["physical_lane"] = 0
        electrical_map["channels"][1]["direction"] = "either"
        problem = validate_chimew_bank_channel_input(self.assignment_input)
        validated = validate_chimew_electrical_map(
            electrical_map, self.platform, problem
        )
        self.assertEqual(validated["metrics"]["concrete_lanes"], 2)
        result = build_chimew_phase6_pin_plan(
            self.schedule,
            self.platform,
            self.assignment_input,
            electrical_map,
            executable=str(self.executable),
        )
        self.assertEqual(
            {entry["direction"] for entry in result["electrical_binding"]["entries"]},
            {"a_to_b", "b_to_a"},
        )
        self.assertEqual(
            {entry["physical_lane"] for entry in result["electrical_binding"]["entries"]},
            {0},
        )

    def test_direction_agnostic_channels_cannot_resolve_to_one_lane_direction(self) -> None:
        schedule = copy.deepcopy(self.schedule)
        schedule["entries"][1]["from"] = "A"
        schedule["entries"][1]["to"] = "B"
        schedule["entries"][1]["slot"] = 1
        assignment = copy.deepcopy(self.assignment_input)
        assignment["groups"][1]["direction"] = "a_to_b"
        assignment["groups"][1]["members"][0]["fanout"]["x"] = 0.0
        assignment["groups"][1]["members"][0]["fanins"][0]["x"] = 100.0
        electrical_map = copy.deepcopy(self.electrical_map)
        for channel in electrical_map["channels"]:
            channel["physical_lane"] = 0
            channel["direction"] = "either"
        with self.assertRaisesRegex(
            ValidationError, "physical pin|reuses a concrete lane"
        ):
            build_chimew_phase6_pin_plan(
                schedule,
                self.platform,
                assignment,
                electrical_map,
                executable=str(self.executable),
            )

    def test_shared_bidirectional_capacity_rejects_lane_index_reuse(self) -> None:
        platform_document = copy.deepcopy(self.platform_document)
        platform_document["links"][0]["capacity_sharing"] = "shared_bidirectional"
        platform = Platform.from_dict(platform_document)
        electrical_map = copy.deepcopy(self.electrical_map)
        electrical_map["channels"][0]["physical_lane"] = 0
        electrical_map["channels"][0]["direction"] = "a_to_b"
        electrical_map["channels"][1]["physical_lane"] = 0
        electrical_map["channels"][1]["direction"] = "b_to_a"
        problem = validate_chimew_bank_channel_input(self.assignment_input)
        with self.assertRaisesRegex(ValidationError, "concrete lane"):
            validate_chimew_electrical_map(electrical_map, platform, problem)

        full_duplex_map = copy.deepcopy(electrical_map)
        full_duplex_map["channels"][0]["direction"] = "either"
        full_duplex_map["channels"][1]["direction"] = "either"
        result = build_chimew_phase6_pin_plan(
            self.schedule,
            self.platform,
            self.assignment_input,
            full_duplex_map,
            executable=str(self.executable),
        )
        with self.assertRaisesRegex(ValidationError, "reuses a concrete lane"):
            validate_chimew_phase6_binding(
                self.schedule,
                platform,
                result["pin_plan"],
                result["electrical_binding"],
            )

        for channel in electrical_map["channels"][:2]:
            channel["direction"] = "either"
        with self.assertRaisesRegex(ValidationError, "concrete lane"):
            validate_chimew_electrical_map(electrical_map, platform, problem)

    def test_shared_bidirectional_disjoint_slots_form_one_strict_bundle(self) -> None:
        platform_document = copy.deepcopy(self.platform_document)
        platform_document["links"][0]["capacity_sharing"] = "shared_bidirectional"
        platform = Platform.from_dict(platform_document)
        schedule = copy.deepcopy(self.schedule)
        schedule["entries"][0]["tdm_ratio"] = 3
        schedule["entries"][1]["tdm_ratio"] = 2
        schedule["entries"][1]["slot"] = 1

        assignment = copy.deepcopy(self.assignment_input)
        assignment["groups"] = [
            {
                "id": "shared_bundle",
                "domain": "AB",
                "kind": "tdm_group",
                "direction": "bidirectional",
                "members": [
                    {
                        **assignment["groups"][0]["members"][0],
                        "direction": "a_to_b",
                    },
                    {
                        **assignment["groups"][1]["members"][0],
                        "direction": "b_to_a",
                    },
                ],
            }
        ]
        assignment["bank_pairs"][0]["channels"] = assignment["bank_pairs"][0][
            "channels"
        ][:1]
        assignment["metrics"] = {
            "groups": 1,
            "signals": 2,
            "fanins": 2,
            "bank_pairs": 1,
            "channels": 1,
            "bidirectional_bundles": 1,
        }
        electrical = copy.deepcopy(self.electrical_map)
        electrical["channels"] = electrical["channels"][:1]
        electrical["metrics"] = {
            "channels": 1,
            "package_pins": 2,
            "concrete_lanes": 1,
        }
        result = build_chimew_phase6_pin_plan(
            schedule,
            platform,
            assignment,
            electrical,
            executable=str(self.executable),
        )
        self.assertEqual(
            result["electrical_binding"]["entries"][0]["direction"],
            "bidirectional",
        )
        self.assertEqual(
            validate_chimew_phase6_binding(
                schedule,
                platform,
                result["pin_plan"],
                result["electrical_binding"],
            )["status"],
            "pass",
        )

        colliding = copy.deepcopy(schedule)
        colliding["entries"][1]["slot"] = 0
        with self.assertRaisesRegex(ValidationError, "slot collision"):
            build_chimew_phase6_pin_plan(
                colliding,
                platform,
                assignment,
                electrical,
                executable=str(self.executable),
            )

    def test_path_adapter_emits_phase6_consumable_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {
                "schedule": root / "schedule.json",
                "platform": root / "platform.json",
                "assignment": root / "assignment.json",
                "electrical": root / "electrical.json",
            }
            for key, document in (
                ("schedule", self.schedule),
                ("platform", self.platform_document),
                ("assignment", self.assignment_input),
            ):
                if key == "assignment":
                    write_chimew_bank_channel_storage(paths[key], document).close()
                else:
                    write_json(paths[key], document)
            electrical_map = copy.deepcopy(self.electrical_map)
            electrical_map["provenance"]["boarddb_sha256"] = hashlib.sha256(
                paths["platform"].read_bytes()
            ).hexdigest()
            write_json(paths["electrical"], electrical_map)
            report = run_chimew_phase6_adapter(
                paths["schedule"],
                paths["platform"],
                paths["assignment"],
                paths["electrical"],
                root / "out",
                executable=str(self.executable),
            )
            self.assertEqual(report["status"], "pass")
            selector = read_json(
                root / "out" / report["artifacts"]["electrical_binding"]
            )
            self.assertEqual(selector["schema"], CHIMEW_BINDING_STORAGE_SCHEMA)
            self.assertEqual(
                report["artifacts"]["electrical_binding_store"],
                selector["artifact"],
            )
            self.assertEqual(
                read_json(root / "out" / "adapter_report.json"), report
            )
            for name in report["artifacts"].values():
                self.assertTrue((root / "out" / name).is_file())
                self.assertIn(
                    hashlib.sha256((root / "out" / name).read_bytes()).hexdigest(),
                    name,
                )
            self.assertEqual(
                main(
                    [
                        "pin-plan",
                        "chimew-build",
                        "--schedule",
                        str(paths["schedule"]),
                        "--platform",
                        str(paths["platform"]),
                        "--assignment-input",
                        str(paths["assignment"]),
                        "--electrical-map",
                        str(paths["electrical"]),
                        "--assigner",
                        str(self.executable),
                        "--out",
                        str(root / "cli-out"),
                    ]
                ),
                0,
            )
            self.assertTrue((root / "cli-out" / "adapter_report.json").is_file())

    def test_adapter_bundle_commit_preserves_previous_generation_on_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {
                "schedule": root / "schedule.json",
                "platform": root / "platform.json",
                "assignment": root / "assignment.json",
                "electrical": root / "electrical.json",
            }
            for key, document in (
                ("schedule", self.schedule),
                ("platform", self.platform_document),
                ("assignment", self.assignment_input),
            ):
                if key == "assignment":
                    write_chimew_bank_channel_storage(paths[key], document).close()
                else:
                    write_json(paths[key], document)
            electrical_map = copy.deepcopy(self.electrical_map)
            electrical_map["provenance"]["boarddb_sha256"] = hashlib.sha256(
                paths["platform"].read_bytes()
            ).hexdigest()
            write_json(paths["electrical"], electrical_map)
            output = root / "out"
            original_report = run_chimew_phase6_adapter(
                paths["schedule"],
                paths["platform"],
                paths["assignment"],
                paths["electrical"],
                output,
                executable=str(self.executable),
            )
            self.assertFalse((output / "pin_plan.json").exists())
            original_files = {
                path.name: path.read_bytes()
                for path in output.iterdir()
                if path.is_file()
            }

            # Model a SIGKILL after a complete immutable adapter report was
            # installed but before the fixed report commit.  The preceding
            # fixed report remains authoritative, validates unchanged, and a
            # later standalone run may collect only this complete orphan group.
            orphan_inputs = root / "orphan-electrical.json"
            orphan_electrical = copy.deepcopy(electrical_map)
            orphan_electrical["provenance"]["producer_version"] = "orphan"
            write_json(orphan_inputs, orphan_electrical)
            orphan_source = root / "orphan-source"
            orphan_report = run_chimew_phase6_adapter(
                paths["schedule"],
                paths["platform"],
                paths["assignment"],
                orphan_inputs,
                orphan_source,
                executable=str(self.executable),
            )
            orphan_report_generation = chimew_adapter_report_generation_path(
                orphan_source, orphan_report
            )
            for source in (
                *(orphan_source / name for name in orphan_report["artifacts"].values()),
                orphan_report_generation,
            ):
                destination = output / source.name
                if not destination.exists():
                    shutil.copy2(source, destination)
            installed_orphan_report = output / orphan_report_generation.name
            self.assertEqual(
                read_json(output / "adapter_report.json"), original_report
            )
            validate_chimew_adapter_generation(installed_orphan_report)
            self.assertEqual(
                run_chimew_phase6_adapter(
                    paths["schedule"],
                    paths["platform"],
                    paths["assignment"],
                    paths["electrical"],
                    output,
                    executable=str(self.executable),
                ),
                original_report,
            )
            self.assertFalse(installed_orphan_report.exists())
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in output.iterdir()
                    if path.is_file()
                },
                original_files,
            )

            orphan_document = read_json(
                output / original_report["artifacts"]["bank_channel_report"]
            )
            orphan_document["orphan_generation_marker"] = True
            orphan_temporary = output / "orphan-candidate.json"
            write_json(orphan_temporary, orphan_document)
            orphan_digest = hashlib.sha256(
                orphan_temporary.read_bytes()
            ).hexdigest()
            orphan_path = (
                output / f"bank_channel_report.{orphan_digest}.json"
            )
            orphan_temporary.replace(orphan_path)
            self.assertTrue(orphan_path.is_file())
            self.assertEqual(
                run_chimew_phase6_adapter(
                    paths["schedule"],
                    paths["platform"],
                    paths["assignment"],
                    paths["electrical"],
                    output,
                    executable=str(self.executable),
                ),
                original_report,
            )
            # Bare artifacts have no immutable report root and must never be
            # treated as a collectible generation.
            self.assertTrue(orphan_path.exists())
            orphan_path.unlink()
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in output.iterdir()
                    if path.is_file()
                },
                original_files,
            )
            electrical_map["provenance"]["producer_version"] = "2"
            write_json(paths["electrical"], electrical_map)
            real_publish = __import__(
                "emuflow.transport.chimew.chimew_phase6", fromlist=["_publish_chimew_adapter_file"]
            )._publish_chimew_adapter_file

            predicates = {
                "sidecar": lambda path: path.suffix == ".sqlite"
                and path.name.startswith("pin_plan."),
                "selector": lambda path: path.suffix == ".json"
                and path.name.startswith("pin_plan."),
                "report": lambda path: path.name == "adapter_report.json",
            }
            for label, predicate in predicates.items():
                def fail_selected(source, destination, *, predicate=predicate):
                    if predicate(destination):
                        raise OSError(f"injected {label} publication failure")
                    return real_publish(source, destination)

                with self.subTest(label=label), mock.patch(
                    "emuflow.transport.chimew.chimew_phase6._publish_chimew_adapter_file",
                    side_effect=fail_selected,
                ):
                    with self.assertRaisesRegex(OSError, "injected"):
                        run_chimew_phase6_adapter(
                            paths["schedule"],
                            paths["platform"],
                            paths["assignment"],
                            paths["electrical"],
                            output,
                            executable=str(self.executable),
                        )
                self.assertEqual(
                    read_json(output / "adapter_report.json"), original_report
                )
                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in output.iterdir()
                        if path.is_file()
                    },
                    original_files,
                )
                self.assertEqual(list(root.glob(".out.adapter-*.tmp")), [])

            replacement_report = run_chimew_phase6_adapter(
                paths["schedule"],
                paths["platform"],
                paths["assignment"],
                paths["electrical"],
                output,
                executable=str(self.executable),
            )
            self.assertNotEqual(replacement_report, original_report)
            replacement_names = set(replacement_report["artifacts"].values())
            for name in replacement_names:
                self.assertTrue((output / name).is_file())
            for name in set(original_report["artifacts"].values()) - replacement_names:
                self.assertFalse((output / name).exists())

    def test_adapter_generation_requires_complete_sealed_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {
                "schedule": root / "schedule.json",
                "platform": root / "platform.json",
                "assignment": root / "assignment.json",
                "electrical": root / "electrical.json",
            }
            for key, document in (
                ("schedule", self.schedule),
                ("platform", self.platform_document),
                ("assignment", self.assignment_input),
            ):
                if key == "assignment":
                    write_chimew_bank_channel_storage(paths[key], document).close()
                else:
                    write_json(paths[key], document)
            electrical_map = copy.deepcopy(self.electrical_map)
            electrical_map["provenance"]["boarddb_sha256"] = hashlib.sha256(
                paths["platform"].read_bytes()
            ).hexdigest()
            write_json(paths["electrical"], electrical_map)
            output = root / "valid"
            report = run_chimew_phase6_adapter(
                paths["schedule"],
                paths["platform"],
                paths["assignment"],
                paths["electrical"],
                output,
                executable=str(self.executable),
            )
            immutable_report = chimew_adapter_report_generation_path(
                output, report
            )
            self.assertEqual(
                validate_chimew_adapter_generation(immutable_report),
                {
                    immutable_report.resolve(),
                    *{
                        (output / name).resolve()
                        for name in report["artifacts"].values()
                    },
                },
            )

            pragma_only = root / "pragma-only"
            shutil.copytree(output, pragma_only)
            original_store = pragma_only / report["artifacts"]["pin_plan_store"]
            with closing(sqlite3.connect(original_store)) as connection, connection:
                application_id = connection.execute(
                    "PRAGMA application_id"
                ).fetchone()[0]
                user_version = connection.execute(
                    "PRAGMA user_version"
                ).fetchone()[0]
            fake_store = pragma_only / "pin_plan.incomplete.sqlite"
            with closing(sqlite3.connect(fake_store)) as connection, connection:
                connection.execute(f"PRAGMA application_id={application_id}")
                connection.execute(f"PRAGMA user_version={user_version}")
            fake_store_digest = hashlib.sha256(fake_store.read_bytes()).hexdigest()
            sealed_fake_store = pragma_only / f"pin_plan.{fake_store_digest}.sqlite"
            fake_store.replace(sealed_fake_store)
            pin_selector = read_json(
                pragma_only / report["artifacts"]["pin_plan"]
            )
            pin_selector["artifact"] = sealed_fake_store.name
            pin_selector["store_bytes"] = sealed_fake_store.stat().st_size
            pin_selector["store_sha256"] = fake_store_digest
            rewritten_pin = pragma_only / "pin_plan.rewritten.json"
            write_json(rewritten_pin, pin_selector)
            rewritten_pin_digest = hashlib.sha256(
                rewritten_pin.read_bytes()
            ).hexdigest()
            sealed_rewritten_pin = (
                pragma_only / f"pin_plan.{rewritten_pin_digest}.json"
            )
            rewritten_pin.replace(sealed_rewritten_pin)
            pragma_report = copy.deepcopy(report)
            pragma_report["artifacts"]["pin_plan_store"] = sealed_fake_store.name
            pragma_report["artifacts"]["pin_plan"] = sealed_rewritten_pin.name
            pragma_report_path = chimew_adapter_report_generation_path(
                pragma_only, pragma_report
            )
            write_json(pragma_report_path, pragma_report)
            with self.assertRaisesRegex(
                ValidationError, "indexed generation is malformed"
            ):
                validate_chimew_adapter_generation(pragma_report_path)

            two_field = root / "two-field-json"
            shutil.copytree(output, two_field)
            original_bank = read_json(
                two_field / report["artifacts"]["bank_channel_report"]
            )
            fake_bank = two_field / "bank_channel_report.incomplete.json"
            write_json(
                fake_bank,
                {
                    "schema": original_bank["schema"],
                    "provider": original_bank["header"]["provider"],
                },
            )
            fake_bank_digest = hashlib.sha256(fake_bank.read_bytes()).hexdigest()
            sealed_fake_bank = (
                two_field / f"bank_channel_report.{fake_bank_digest}.json"
            )
            fake_bank.replace(sealed_fake_bank)
            two_field_report = copy.deepcopy(report)
            two_field_report["artifacts"][
                "bank_channel_report"
            ] = sealed_fake_bank.name
            two_field_report_path = chimew_adapter_report_generation_path(
                two_field, two_field_report
            )
            write_json(two_field_report_path, two_field_report)
            with self.assertRaisesRegex(
                ValidationError, "bank/channel report generation is malformed"
            ):
                validate_chimew_adapter_generation(two_field_report_path)

    def test_out_of_bounds_physical_placement_is_rejected(self) -> None:
        assignment = copy.deepcopy(self.assignment_input)
        assignment["groups"][0]["members"][0]["fanout"]["y"] = 101.0
        with self.assertRaisesRegex(ValidationError, "outside FPGA bounds"):
            build_chimew_phase6_pin_plan(
                self.schedule,
                self.platform,
                assignment,
                self.electrical_map,
                executable=str(self.executable),
            )

    def test_failed_pin_plan_closes_owned_report_and_active_cursor_with_live_traceback(self) -> None:
        from emuflow.transport.chimew import chimew_phase6 as adapter
        from emuflow.transport.chimew.chimew_bank_channel import evaluate_chimew_bank_channel_assignment as assign
        assignment = copy.deepcopy(self.assignment_input)
        assignment["groups"][0]["members"][0]["fanout"]["y"] = 101.0
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with write_chimew_bank_channel_storage(root / "input.json", assignment) as source:
                report = assign(source, executable=str(self.executable), output_path=root / "report.json")
                with mock.patch.object(adapter, "evaluate_chimew_bank_channel_assignment", return_value=report):
                    try:
                        adapter.build_chimew_phase6_pin_plan(
                            self.schedule, self.platform, source, self.electrical_map,
                            executable=str(self.executable), bank_channel_report_output_path=root / "report.json")
                    except ValidationError as error:
                        self.assertIn("outside FPGA bounds", str(error))
                        traceback = error.__traceback__
                    else:
                        self.fail("out-of-bounds placement was accepted")
                retained = traceback
                found = False
                while traceback is not None:
                    if traceback.tb_frame.f_code.co_name == "build_chimew_phase6_pin_plan":
                        cursor = traceback.tb_frame.f_locals["assignment_cursor"]
                        self.assertIsNone(cursor.gi_frame)
                        found = True
                    traceback = traceback.tb_next
                self.assertTrue(found)
                with self.assertRaisesRegex(ValidationError, "closed"):
                    report["assignments"][0]
                self.assertIsNotNone(retained)

    def test_bank_voltage_must_match_selected_iostandard(self) -> None:
        electrical_map = copy.deepcopy(self.electrical_map)
        electrical_map["channels"][0]["bank_voltage"] = 2.5
        with self.assertRaisesRegex(ValidationError, "voltage/IOSTANDARD"):
            build_chimew_phase6_pin_plan(
                self.schedule,
                self.platform,
                self.assignment_input,
                electrical_map,
                executable=str(self.executable),
            )

    def test_binding_checker_rejects_pin_plan_lane_tampering(self) -> None:
        result = build_chimew_phase6_pin_plan(
            self.schedule,
            self.platform,
            self.assignment_input,
            self.electrical_map,
            executable=str(self.executable),
        )
        plan = copy.deepcopy(result["pin_plan"])
        plan["entries"][0]["physical_lane"] = 1
        with self.assertRaisesRegex(ValidationError, "conflicts with pin plan"):
            validate_chimew_phase6_binding(
                self.schedule,
                self.platform,
                plan,
                result["electrical_binding"],
            )


if __name__ == "__main__":
    unittest.main()
