import copy
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from emuflow.chimew_bank_channel import (
    CHIMEW_BANK_CHANNEL_INPUT_PROVIDER,
    CHIMEW_BANK_CHANNEL_INPUT_SCHEMA,
    evaluate_chimew_bank_channel_assignment,
    validate_chimew_bank_channel_input,
)
from emuflow.chimew_phase6 import (
    CHIMEW_ELECTRICAL_MAP_PROVIDER,
    CHIMEW_ELECTRICAL_MAP_SCHEMA,
    CHIMEW_ELECTRICAL_MAP_SCHEMA_V1,
    CHIMEW_PHASE6_BINDING_PROVIDER,
    build_chimew_phase6_pin_plan,
    run_chimew_phase6_adapter,
    validate_chimew_electrical_map,
    validate_chimew_phase6_binding,
)
from emuflow.chimew_qualification import (
    CHIMEW_QUALIFICATION_PROVIDER,
    CHIMEW_QUALIFICATION_SCHEMA,
    canonical_sha256,
)
from emuflow.cli import main
from emuflow.errors import ValidationError
from emuflow.io import read_json, write_json
from emuflow.pin_planning import CHIMEW_PIN_PLAN_PROVIDER, validate_pin_plan
from emuflow.platform import Platform


ROOT = Path(__file__).resolve().parents[1]


class ChimewPhase6AdapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        compiler = shutil.which("c++") or shutil.which("clang++")
        if compiler is None:
            raise RuntimeError("a C++17 compiler is required")
        cls.executable = Path(cls.temporary_directory.name) / "chimew-assignment"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-O2",
                "-pthread",
                str(ROOT / "src/native/chimew_bank_channel_assigner.cpp"),
                "-o",
                str(cls.executable),
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

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
                    "link": "AB_link",
                    "from": "A",
                    "to": "B",
                    "tdm_ratio": 4,
                    "lane": 0,
                    "slot": 0,
                },
                {
                    "id": "s1",
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
            self.assertEqual(
                read_json(root / "out" / "adapter_report.json"), report
            )
            for name in report["artifacts"].values():
                self.assertTrue((root / "out" / name).is_file())
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
