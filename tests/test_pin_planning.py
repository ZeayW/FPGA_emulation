from tests.pin_contract_fixture import validate_pin_plan
from tests.schedule_store_fixture import write_tdm_schedule_storage
from tests.pin_plan_fixture import make_pin_plan_fixture
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


from emuflow.common.errors import EmuFlowError, ValidationError
from emuflow_tools.transport.pin_plan_storage import PinPlanEntrySequence
from tests.pin_plan_storage_fixture import write_pin_plan_storage
from emuflow_tools.transport.position_hints_storage import PositionHintEntrySequence
from tests.position_hints_storage_fixture import write_position_hints_storage
from emuflow_tools.transport.pin_planning import SIGNAL_POSITION_HINTS_SCHEMA
from emuflow.platform.platform import Platform
from emuflow.scheduling.tdm_schedule_storage import (
    ScheduleRecordSequence,
    TDM_SCHEDULE_STORAGE_SCHEMA,
    TypedScheduleEntrySequence,
    read_tdm_schedule,
)


ROOT = Path(__file__).resolve().parents[1]


class ChimewPinContractTest(unittest.TestCase):

    def setUp(self) -> None:
        self.platform = Platform.from_dict(
            {
                "schema": "emuflow.boarddb/v1",
                "platform": {"name": "pin_fixture", "kind": "virtual"},
                "fpgas": [
                    {
                        "id": fpga,
                        "part": "test",
                        "utilization_limit": 1.0,
                        "capacity": {"lut": 100, "ff": 100},
                    }
                    for fpga in ("fpga0", "fpga1")
                ],
                "links": [
                    {
                        "id": "link0",
                        "endpoints": ["fpga0", "fpga1"],
                        "direction": "full_duplex",
                        "mode": "abstract",
                        "data_lanes_per_direction": 2,
                        "fabric_clock_mhz": 250,
                        "latency_cycles": 1,
                    }
                ],
            }
        )
        self.schedule = {
            "routes": [], "demand_completions": [], "domain_schedules": [],
            "schema": "emuflow.tdm-schedule/v1",
            "design": "pin_fixture",
            "platform": "pin_fixture",
            "entries": [
                {
                    "id": f"entry{index}",
                    "demand": f"demand{index}", "net": f"net{index}",
                    "hop": 0, "capacity_key": "link0:fpga0:fpga1",
                    "ready_slot": index % 3, "arrival_slot": index % 3 + 1,
                    "link": "link0",
                    "from": "fpga0",
                    "to": "fpga1",
                    "tdm_ratio": 3,
                    "slot": index % 3,
                    "lane": index % 2,
                }
                for index in range(6)
            ],
        }
        self.positions = {
            "schema": SIGNAL_POSITION_HINTS_SCHEMA,
            "design": "pin_fixture",
            "platform": "pin_fixture",
            "provider": "openparf-lookahead-centroid-v1",
            "region_count": 3,
            "metrics": {
                "signals": 6,
                "endpoint_centroid_fallbacks": 0,
            },
            "entries": [
                {
                    "schedule_entry": f"entry{index}",
                    "source_y": 0.1 if index < 3 else 0.9,
                    "sink_y": 0.15 if index < 3 else 0.85,
                    "source_region": 0 if index < 3 else 2,
                    "sink_region": 0 if index < 3 else 2,
                    "source_fallback": False,
                    "sink_fallback": False,
                }
                for index in range(6)
            ],
        }


    def test_retired_planner_entrypoints_are_rejected_before_loading_inputs(self):
        from emuflow.flow.cli import _build_parser
        parser = _build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["pin-plan", "build"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["contest", "eda2023-chimew-ab"])
    def test_retired_provider_is_rejected(self):
        plan = make_pin_plan_fixture(self.schedule, self.platform, self.positions)
        for provider in ("placement-aware-region-grouping-mcf-v1", "chimew-placement-aware-grouping-mcf-v1"):
            with self.subTest(provider=provider):
                plan["provider"] = provider
                with self.assertRaisesRegex(ValidationError, "provider"):
                    validate_pin_plan(self.schedule, self.platform, self.positions, plan)

    def test_lane_slot_collision_is_rejected(self) -> None:
        plan = make_pin_plan_fixture(
            self.schedule,
            self.platform,
            self.positions,
        )
        broken = copy.deepcopy(plan)
        by_id = {
            item["schedule_entry"]: item for item in broken["entries"]
        }
        by_id["entry3"]["physical_lane"] = by_id["entry0"][
            "physical_lane"
        ]
        with self.assertRaisesRegex(ValidationError, "collision"):
            validate_pin_plan(
                self.schedule, self.platform, self.positions, broken
            )

    def test_indexed_validation_uses_bounded_relations(self) -> None:
        plan = make_pin_plan_fixture(
            self.schedule,
            self.platform,
            self.positions,
        )
        legacy_validation = validate_pin_plan(
            self.schedule, self.platform, self.positions, plan
        )
        schedule_document = {
            "schema": "emuflow.tdm-schedule/v1",
            **self.schedule,
            "routes": [],
            "demand_completions": [],
            "domain_schedules": [],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            schedule_path = root / "schedule.json"
            plan_path = root / "pin_plan.json"
            write_tdm_schedule_storage(schedule_path, schedule_document)
            indexed_schedule = read_tdm_schedule(schedule_path)
            indexed_plan = write_pin_plan_storage(plan_path, plan)
            indexed_positions = write_position_hints_storage(
                root / "positions.json", self.positions
            )
            with mock.patch.object(
                ScheduleRecordSequence,
                "__iter__",
                side_effect=AssertionError(
                    "indexed validation scanned schedule entry objects"
                ),
            ), mock.patch.object(
                PinPlanEntrySequence,
                "__iter__",
                side_effect=AssertionError(
                    "indexed validation scanned pin-plan entry objects"
                ),
            ), mock.patch.object(
                PositionHintEntrySequence,
                "__iter__",
                side_effect=AssertionError(
                    "indexed validation scanned position-hint objects"
                ),
            ):
                validation = validate_pin_plan(
                    indexed_schedule,
                    self.platform,
                    indexed_positions,
                    indexed_plan,
                )
            self.assertEqual(validation["status"], "pass")
            self.assertEqual(validation["signals"], 6)
            self.assertEqual(validation["groups"], 2)
            self.assertEqual(set(validation), set(legacy_validation))
            for field, expected in legacy_validation.items():
                if isinstance(expected, float):
                    self.assertAlmostEqual(validation[field], expected, places=12)
                else:
                    self.assertEqual(validation[field], expected)

            broken = copy.deepcopy(plan)
            by_id = {
                item["schedule_entry"]: item
                for item in broken["entries"]
            }
            by_id["entry3"]["physical_lane"] = by_id["entry0"][
                "physical_lane"
            ]
            indexed_broken = write_pin_plan_storage(
                root / "broken.json", broken
            )
            with self.assertRaisesRegex(ValidationError, "collision"):
                validate_pin_plan(
                    indexed_schedule,
                    self.platform,
                    self.positions,
                    indexed_broken,
                )

            incomplete = copy.deepcopy(plan)
            incomplete["entries"].pop()
            indexed_incomplete = write_pin_plan_storage(
                root / "incomplete.json", incomplete
            )
            with self.assertRaisesRegex(ValidationError, "cover"):
                validate_pin_plan(
                    indexed_schedule,
                    self.platform,
                    self.positions,
                    indexed_incomplete,
                )

            wrong_metrics = copy.deepcopy(plan)
            wrong_metrics["metrics"]["objective"] += 1.0
            indexed_wrong_metrics = write_pin_plan_storage(
                root / "wrong-metrics.json", wrong_metrics
            )
            with self.assertRaisesRegex(
                ValidationError, "objective.*independently agree"
            ):
                validate_pin_plan(
                    indexed_schedule,
                    self.platform,
                    self.positions,
                    indexed_wrong_metrics,
                )

            incomplete_positions = copy.deepcopy(self.positions)
            incomplete_positions["entries"].pop()
            incomplete_positions["metrics"]["signals"] -= 1
            indexed_incomplete_positions = write_position_hints_storage(
                root / "incomplete-positions.json", incomplete_positions
            )
            with self.assertRaisesRegex(
                ValidationError, "position hints must cover"
            ):
                validate_pin_plan(
                    indexed_schedule,
                    self.platform,
                    indexed_incomplete_positions,
                    indexed_plan,
                )

            malformed_schedule_document = copy.deepcopy(schedule_document)
            malformed_schedule_document["entries"][0].pop("link")
            malformed_schedule_path = root / "malformed-schedule.json"
            with self.assertRaisesRegex(ValidationError, "typed schedule entry is malformed"):
                write_tdm_schedule_storage(
                    malformed_schedule_path, malformed_schedule_document
                )
            self.assertFalse(malformed_schedule_path.exists())

    def test_static_exact_v4_validation_reads_typed_entry_relation(self) -> None:
        plan = make_pin_plan_fixture(
            self.schedule,
            self.platform,
            self.positions,
        )
        entries = [
            {
                **entry,
                "demand": f"demand{index}",
                "net": f"net{index}",
                "hop": 0,
                "capacity_key": "link0:fpga0:fpga1",
                "ready_slot": entry["slot"],
                "arrival_slot": entry["slot"] + 1,
            }
            for index, entry in enumerate(self.schedule["entries"])
        ]
        schedule_document = {
            "schema": "emuflow.tdm-schedule/v1",
            "provider": "deterministic-dependency-earliest-slot-v1",
            "design": self.schedule["design"],
            "platform": self.schedule["platform"],
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
            indexed_plan = write_pin_plan_storage(
                root / "pin_plan.json", plan
            )
            indexed_positions = write_position_hints_storage(
                root / "positions.json", self.positions
            )
            with mock.patch.object(
                TypedScheduleEntrySequence,
                "__iter__",
                side_effect=AssertionError(
                    "v4 pin validation expanded typed schedule entries"
                ),
            ):
                validation = validate_pin_plan(
                    indexed_schedule,
                    self.platform,
                    indexed_positions,
                    indexed_plan,
                )
            self.assertEqual(validation["status"], "pass")
            self.assertEqual(validation["signals"], len(entries))


if __name__ == "__main__":
    unittest.main()
