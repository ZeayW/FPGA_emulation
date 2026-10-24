import copy
import io
import random
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow import chimew_refinement as refinement_module
from emuflow.chimew_grouping import (
    CHIMEW_CROSSING_PROVIDER,
    CHIMEW_CROSSING_SCHEMA,
    build_chimew_initial_groups,
)
from emuflow.chimew_refinement import (
    CHIMEW_POSITION_PROVIDER,
    CHIMEW_POSITION_SCHEMA,
    CHIMEW_REFINEMENT_PROVIDER,
    _pairwise_objective,
    refine_chimew_groups,
    validate_chimew_positions,
)
from emuflow.errors import ValidationError
from tests.native_build import emuflow_native


ROOT = Path(__file__).resolve().parents[1]


class ChimewRefinementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.grouper = cls.refiner = emuflow_native()
        cls.grouper_command = [str(cls.grouper), "phase6", "group-signals"]
        cls.refiner_command = [str(cls.refiner), "phase6", "refine-groups"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def setUp(self) -> None:
        self.schedule = {
            "design": "refinement_fixture",
            "platform": "two_fpga",
            "entries": [
                {
                    "id": f"s{index}",
                    "link": "link0",
                    "from": "fpga0",
                    "to": "fpga1",
                    "tdm_ratio": 3,
                }
                for index in range(8)
            ],
        }
        encodings = [2, 2, 2, 2, 1, 1, 1, 1]
        self.crossings = {
            "schema": CHIMEW_CROSSING_SCHEMA,
            "design": "refinement_fixture",
            "platform": "two_fpga",
            "provider": CHIMEW_CROSSING_PROVIDER,
            "slls_per_fpga": 2,
            "provenance": {
                "producer": "fixture-physical-router",
                "producer_version": "1",
                "routing_sha256": "a" * 64,
            },
            "metrics": {"signals": 8, "physical_sll_crossings": 8},
            "entries": [
                {
                    "schedule_entry": f"s{index}",
                    "source_slls": [1] if encoding == 2 else [0],
                    "sink_slls": [],
                    "encoding": encoding,
                }
                for index, encoding in enumerate(encodings)
            ],
        }
        source_y = [100.0, 101.0, 0.0, 1.0, 0.0, 1.0, 100.0, 101.0]
        self.positions = {
            "schema": CHIMEW_POSITION_SCHEMA,
            "design": "refinement_fixture",
            "platform": "two_fpga",
            "provider": CHIMEW_POSITION_PROVIDER,
            "coordinate_system": "physical-site-y",
            "provenance": {
                "producer": "fixture-lookahead-placer",
                "producer_version": "1",
                "placement_sha256": "b" * 64,
            },
            "metrics": {"signals": 8},
            "entries": [
                {"schedule_entry": f"s{index}", "source_y": value}
                for index, value in enumerate(source_y)
            ],
        }
        self.initial = build_chimew_initial_groups(
            self.schedule, self.crossings, executable=str(self.grouper)
        )

    def test_native_refinement_matches_replay_and_preserves_crossings(self) -> None:
        result = refine_chimew_groups(
            self.schedule,
            self.crossings,
            self.initial,
            self.positions,
            executable=str(self.refiner),
        )
        self.assertEqual(result["provider"], CHIMEW_REFINEMENT_PROVIDER)
        self.assertEqual(result["status"], "standalone_paper_bounded_inference")
        self.assertEqual(result["integration_status"], "not-a-phase6-pin-plan")
        self.assertGreater(result["metrics"]["moved_signals"], 0)
        self.assertLessEqual(
            result["metrics"]["pairwise_source_y_after"],
            result["metrics"]["pairwise_source_y_before"],
        )
        self.assertEqual(
            result["metrics"]["group_physical_sll_crossings"],
            self.initial["metrics"]["group_physical_sll_crossings"],
        )
        self.assertEqual(result["metrics"]["oracle_disagreements"], 0)



    def test_typed_refinement_rejects_corruption(self) -> None:
        encodings = refinement_module.validate_chimew_crossings(
            self.schedule, self.crossings
        )
        positions = validate_chimew_positions(self.schedule, self.positions)
        initial = refinement_module._validate_initial_groups(
            self.schedule, self.initial, encodings
        )
        guards = refinement_module._timing_guards(self.schedule, self.initial)
        entries = sorted(self.schedule["entries"], key=lambda entry: entry["id"])
        stream = io.BytesIO()
        metadata = refinement_module._serialize_typed_refinement(
            entries, encodings, positions, initial, guards, stream
        )
        valid = stream.getvalue()
        variants = (valid[:-1], valid[:70] + bytes([valid[70] ^ 1]) + valid[71:])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, payload in enumerate(variants):
                input_path = root / f"invalid-{index}.bin"
                input_path.write_bytes(payload)
                completed = subprocess.run(
                    [
                        *self.refiner_command,
                        "--typed-input",
                        str(input_path),
                        "-",
                        metadata["logical_sha256"],
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("typed input", completed.stderr)

    def test_refinement_preserves_frozen_phase5_slot_uniqueness(self) -> None:
        schedule = copy.deepcopy(self.schedule)
        for index, entry in enumerate(schedule["entries"]):
            entry["slot"] = index % entry["tdm_ratio"]
        initial = build_chimew_initial_groups(
            schedule, self.crossings, executable=str(self.grouper)
        )
        result = refine_chimew_groups(
            schedule,
            self.crossings,
            initial,
            self.positions,
            executable=str(self.refiner),
        )
        group_by_entry = {
            record["schedule_entry"]: record["group"]
            for record in result["entries"]
        }
        slots_by_group = {}
        for entry in schedule["entries"]:
            slots = slots_by_group.setdefault(group_by_entry[entry["id"]], set())
            self.assertNotIn(entry["slot"], slots)
            slots.add(entry["slot"])

    def test_normalized_position_substitute_is_rejected(self) -> None:
        invalid = copy.deepcopy(self.positions)
        invalid["coordinate_system"] = "normalized-y"
        with self.assertRaisesRegex(ValidationError, "normalized"):
            validate_chimew_positions(self.schedule, invalid)

    def test_position_provenance_and_exact_coverage_are_checked(self) -> None:
        invalid_digest = copy.deepcopy(self.positions)
        invalid_digest["provenance"]["placement_sha256"] = "opaque"
        with self.assertRaisesRegex(ValidationError, "SHA-256"):
            validate_chimew_positions(self.schedule, invalid_digest)
        missing = copy.deepcopy(self.positions)
        missing["entries"].pop()
        missing["metrics"]["signals"] -= 1
        with self.assertRaisesRegex(ValidationError, "cover"):
            validate_chimew_positions(self.schedule, missing)

    def test_tampered_initial_group_is_rejected(self) -> None:
        invalid = copy.deepcopy(self.initial)
        invalid["entries"][0]["encoding"] ^= 1
        with self.assertRaisesRegex(ValidationError, "encoding"):
            refine_chimew_groups(
                self.schedule,
                self.crossings,
                invalid,
                self.positions,
                executable=str(self.refiner),
            )

    def test_timing_guard_must_cover_the_complete_original_lane(self) -> None:
        schedule = copy.deepcopy(self.schedule)
        for index, entry in enumerate(schedule["entries"]):
            entry["lane"] = index // 2
            entry["slot"] = index % 2
        guarded = build_chimew_initial_groups(
            schedule,
            self.crossings,
            executable=str(self.grouper),
            protected_entries={"s0"},
        )
        guarded["entries"][1].pop("timing_guard_lane")
        with self.assertRaisesRegex(ValidationError, "complete lane"):
            refine_chimew_groups(
                schedule,
                self.crossings,
                guarded,
                self.positions,
                executable=str(self.refiner),
            )

    def test_prefix_sum_pairwise_objective_matches_exhaustive(self) -> None:
        random_generator = random.Random(731)
        for size in range(1, 80):
            positions = {
                f"n{index}": random_generator.uniform(-1000.0, 1000.0)
                for index in range(size)
            }
            members = {0: set(positions)}
            ordered = sorted(positions.values())
            exhaustive = sum(
                abs(lhs - rhs)
                for index, lhs in enumerate(ordered)
                for rhs in ordered[index + 1 :]
            )
            self.assertAlmostEqual(
                _pairwise_objective(members, positions, {0}),
                exhaustive,
                places=7,
            )

    def test_large_pairwise_objective_is_linearithmic(self) -> None:
        positions = {f"n{index}": float(index % 257) for index in range(100_000)}
        members = {0: set(positions)}
        objective = _pairwise_objective(members, positions, {0})
        self.assertGreater(objective, 0.0)


if __name__ == "__main__":
    unittest.main()
