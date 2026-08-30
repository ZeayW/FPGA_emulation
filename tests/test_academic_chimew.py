from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.academic_chimew import (
    _bundle_shared_bidirectional_groups,
    _coalesce_timing_guard_lanes,
    _timing_weights,
    materialize_academic_chimew_inputs,
)
from emuflow.chimew_pipeline import (
    run_chimew_phase6_pipeline,
    validate_chimew_phase6_pipeline,
)
from emuflow.cli import _build_parser
from emuflow.errors import EmuFlowError, ValidationError
from emuflow.io import read_json, write_json
from emuflow.multi_fpga_flow import (
    PHASE6_AB_COMPARISON_SCHEMA,
    run_multi_fpga_flow,
    validate_phase6_ab_comparison,
)
from emuflow.phase1 import run_phase1
from emuflow.phase3 import run_phase3
from emuflow.phase4 import run_phase4
from emuflow.phase5 import run_phase5
from tests.native_build import tlr_router


ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / "platforms/virtual/academic_vtr_2fpga_p2p.json"


class AcademicChimewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        compiler = shutil.which("c++") or shutil.which("clang++")
        if compiler is None:
            raise RuntimeError("a C++17 compiler is required")
        cls.native_root = tempfile.TemporaryDirectory()
        cls.executables = {}
        for source, label in (
            ("chimew_signal_grouper.cpp", "grouper"),
            ("chimew_position_refiner.cpp", "refiner"),
            ("chimew_rudy.cpp", "rudy"),
            ("chimew_bank_channel_assigner.cpp", "assigner"),
        ):
            executable = Path(cls.native_root.name) / label
            subprocess.run(
                [
                    compiler,
                    "-std=c++17",
                    "-O2",
                    "-pthread",
                    str(ROOT / "src/native" / source),
                    "-o",
                    str(executable),
                ],
                check=True,
            )
            cls.executables[label] = str(executable)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.native_root.cleanup()

    def _upstream(self, root: Path) -> tuple[Path, Path, Path, Path]:
        phase1 = root / "phase1"
        phase3 = root / "phase3"
        phase4 = root / "phase4"
        phase5 = root / "phase5"
        run_phase1(
            ROOT / "examples/yosys/counter.json",
            PLATFORM,
            phase1,
            top="counter",
            clocks=["clk"],
        )
        run_phase3(
            phase1 / "design.emuir.json",
            PLATFORM,
            phase3,
            provider="greedy",
            cut_mode="sequential-only",
        )
        run_phase4(
            phase3 / "assignment.json",
            PLATFORM,
            phase4,
            frame_slots=32,
            router=str(tlr_router()),
        )
        run_phase5(phase4 / "routes.json", PLATFORM, phase5)
        return (
            phase1 / "design.emuir.json",
            phase3 / "assignment.json",
            phase4 / "routes.json",
            phase5 / "schedule.json",
        )

    def _physical_report(
        self, root: Path, assignment_path: Path, *, encoded_atoms: bool = False
    ) -> dict:
        assignment = read_json(assignment_path)["instance_assignment"]
        records = []
        for fpga in ("fpga0", "fpga1"):
            fpga_root = root / fpga
            fpga_root.mkdir(parents=True)
            atoms = sorted(
                instance
                for instance, destination in assignment.items()
                if destination == fpga
            )
            clusters = []
            placement_lines = []
            for index, atom in enumerate(atoms):
                cluster = f"cluster_{index}"
                clusters.append(
                    {
                        "name": cluster,
                        "atoms": [f"i{index}" if encoded_atoms else atom],
                    }
                )
                placement_lines.append(
                    f"{cluster} {index + 1} {2 * index + 1} 0 0 #{index}"
                )
            placement = fpga_root / "lookahead.place"
            placement.write_text(
                "\n".join(placement_lines) + "\n", encoding="utf-8"
            )
            packed = fpga_root / "packed.json"
            write_json(packed, {"clusters": clusters})
            placement_ir = fpga_root / "placement.emuir.json"
            write_json(
                placement_ir,
                {
                    "schema": "test",
                    "instances": [{"id": atom} for atom in atoms],
                },
            )
            write_json(fpga_root / "architecture.json", {"schema": "test"})
            boundary = fpga_root / "boundary-identities.json"
            write_json(
                boundary,
                {
                    "endpoints": [],
                },
            )
            records.append(
                {
                    "fpga": fpga,
                    "status": "pass",
                    "stages": {
                        "openparf_placement": {
                            "artifacts": {"vpr_placement": str(placement)}
                        },
                        "packed_contract": {"output": str(packed)},
                        "placement_ir": {
                            "output": str(placement_ir),
                            "boundary_identity": {"output": str(boundary)},
                        },
                    },
                }
            )
        return {"fpgas": records}

    def test_timing_guard_coalesces_the_entire_frozen_lane(self) -> None:
        schedule = {
            "entries": [
                {
                    "id": "critical-slot-0",
                    "link": "l0",
                    "from": "a",
                    "to": "b",
                    "lane": 2,
                },
                {
                    "id": "critical-slot-1",
                    "link": "l0",
                    "from": "a",
                    "to": "b",
                    "lane": 2,
                },
                {
                    "id": "unprotected",
                    "link": "l0",
                    "from": "a",
                    "to": "b",
                    "lane": 1,
                },
            ]
        }
        refined = {
            "entries": [
                {"schedule_entry": "critical-slot-0", "group": 0},
                {"schedule_entry": "critical-slot-1", "group": 1},
                # Deliberately share native group 0 with a different lane:
                # the materialization group must still isolate the guard.
                {"schedule_entry": "unprotected", "group": 0},
            ]
        }
        groups, bundles = _coalesce_timing_guard_lanes(
            schedule, refined, {"critical-slot-0"}
        )
        self.assertEqual(groups["unprotected"], 0)
        self.assertEqual(groups["critical-slot-0"], 2)
        self.assertEqual(groups["critical-slot-1"], 2)
        self.assertEqual(
            bundles,
            [
                {
                    "link": "l0",
                    "from": "a",
                    "to": "b",
                    "physical_lane": 2,
                    "schedule_entries": ["critical-slot-0", "critical-slot-1"],
                    "refined_groups": [0, 1],
                    "materialized_group": 2,
                }
            ],
        )

    def test_materialization_coalesces_split_refinement_groups_on_guarded_lane(
        self,
    ) -> None:
        """A guarded mux lane remains one electrical channel after refinement."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir, assignment, routes, schedule = self._upstream(root)
            schedule_document = read_json(schedule)
            critical_entry = schedule_document["entries"][0]
            duplicate = dict(critical_entry)
            duplicate["id"] = critical_entry["id"] + "-other-slot"
            duplicate["slot"] = 1
            duplicate["arrival_slot"] = 3
            schedule_document["entries"].append(duplicate)
            write_json(schedule, schedule_document)
            timing = root / "timing.json"
            write_json(
                timing,
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": schedule_document["design"],
                    "paths": [
                        {
                            "clock_period_ns": 10.0,
                            "slack_ns": 0.0,
                            "cut_nets": [critical_entry["net"]],
                        }
                    ],
                },
            )

            def split_refinement(
                materialized_schedule: dict, *_args: object, **_kwargs: object
            ) -> dict:
                return {
                    "entries": [
                        {"schedule_entry": entry["id"], "group": index}
                        for index, entry in enumerate(
                            materialized_schedule["entries"]
                        )
                    ]
                }

            with patch(
                "emuflow.academic_chimew.refine_chimew_groups",
                side_effect=split_refinement,
            ):
                lookahead = materialize_academic_chimew_inputs(
                    ir_path=ir,
                    schedule_path=schedule,
                    routes_path=routes,
                    platform_path=PLATFORM,
                    physical_report=self._physical_report(
                        root / "physical", assignment
                    ),
                    output_dir=root / "lookahead",
                    timing_paths_path=timing,
                    timing_path_scope="whole-net",
                    grouper=self.executables["grouper"],
                    refiner=self.executables["refiner"],
                )
            bank_input = read_json(
                Path(lookahead["artifacts"]["bank_channel_input"]["path"])
            )
            guarded_groups = [
                group
                for group in bank_input["groups"]
                if {member["id"] for member in group["members"]}
                == {critical_entry["id"], duplicate["id"]}
            ]
            self.assertEqual(len(guarded_groups), 1)
            self.assertIn("timing-guard-lane", guarded_groups[0]["domain"])
            self.assertEqual(bank_input["metrics"]["timing_guard_lane_bundles"], 1)
            self.assertEqual(
                bank_input["timing_guard_lane_bundles"][0]["refined_groups"],
                [0, 2],
            )

    def test_vtr_atom_indices_map_back_to_source_instances(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir, assignment, routes, schedule = self._upstream(root)
            lookahead = materialize_academic_chimew_inputs(
                ir_path=ir,
                schedule_path=schedule,
                routes_path=routes,
                platform_path=PLATFORM,
                physical_report=self._physical_report(
                    root / "physical", assignment, encoded_atoms=True
                ),
                output_dir=root / "lookahead",
                grouper=self.executables["grouper"],
                refiner=self.executables["refiner"],
            )
            self.assertEqual(
                lookahead["metrics"]["placement_endpoint_fallbacks"], 0
            )
            self.assertEqual(
                lookahead["metrics"]["placed_source_instances"],
                lookahead["metrics"]["source_instances"],
            )

    def test_materialized_inputs_run_complete_source_bound_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir, assignment, routes, schedule = self._upstream(root)
            lookahead = materialize_academic_chimew_inputs(
                ir_path=ir,
                schedule_path=schedule,
                routes_path=routes,
                platform_path=PLATFORM,
                physical_report=self._physical_report(root / "physical", assignment),
                output_dir=root / "lookahead",
                grouper=self.executables["grouper"],
                refiner=self.executables["refiner"],
            )
            self.assertEqual(
                lookahead["qualification"], "academic-virtual-physical-model"
            )
            self.assertEqual(
                lookahead["metrics"]["placement_endpoint_fallbacks"], 0
            )
            artifacts = lookahead["artifacts"]
            materialized_schedule = read_json(
                Path(artifacts["schedule"]["path"])
            )
            self.assertTrue(
                all(
                    "tdm_ratio" in entry
                    for entry in materialized_schedule["entries"]
                )
            )
            bank_input = read_json(Path(artifacts["bank_channel_input"]["path"]))
            electrical_map = read_json(Path(artifacts["electrical_map"]["path"]))
            directions = {group["direction"] for group in bank_input["groups"]}
            self.assertEqual(directions, {"a_to_b", "b_to_a"})
            self.assertEqual(len(bank_input["domains"]), len(directions))
            self.assertNotIn(
                "either",
                {channel["direction"] for channel in electrical_map["channels"]},
            )
            self.assertEqual(len(bank_input["domains"]), 2)
            domain_ids = {domain["id"] for domain in bank_input["domains"]}
            self.assertEqual(
                domain_ids,
                {"link_0_1:a_to_b", "link_0_1:b_to_a"},
            )
            lane_directions = {
                (channel["physical_lane"], channel["direction"])
                for channel in electrical_map["channels"]
            }
            self.assertIn((0, "a_to_b"), lane_directions)
            self.assertIn((0, "b_to_a"), lane_directions)
            report = run_chimew_phase6_pipeline(
                Path(artifacts["schedule"]["path"]),
                PLATFORM,
                Path(artifacts["crossings"]["path"]),
                Path(artifacts["positions"]["path"]),
                Path(artifacts["rudy_input"]["path"]),
                Path(artifacts["bank_channel_input"]["path"]),
                Path(artifacts["electrical_map"]["path"]),
                root / "chimew",
                source_paths={
                    label: Path(path)
                    for label, path in lookahead["sources"].items()
                },
                grouper=self.executables["grouper"],
                refiner=self.executables["refiner"],
                rudy=self.executables["rudy"],
                assigner=self.executables["assigner"],
                region_count=4,
            )
            self.assertEqual(report["qualification_scope"], "byte-bound-source-artifacts")
            self.assertEqual(
                validate_chimew_phase6_pipeline(root / "chimew")["status"],
                "pass",
            )

    def test_shared_bidirectional_link_uses_one_exclusive_lane_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir, assignment, routes, schedule = self._upstream(root)
            platform_document = read_json(PLATFORM)
            platform_document["links"][0][
                "capacity_sharing"
            ] = "shared_bidirectional"
            shared_platform = root / "shared-platform.json"
            write_json(shared_platform, platform_document)

            lookahead = materialize_academic_chimew_inputs(
                ir_path=ir,
                schedule_path=schedule,
                routes_path=routes,
                platform_path=shared_platform,
                physical_report=self._physical_report(
                    root / "physical", assignment
                ),
                output_dir=root / "lookahead",
                grouper=self.executables["grouper"],
                refiner=self.executables["refiner"],
            )
            artifacts = lookahead["artifacts"]
            bank_input = read_json(
                Path(artifacts["bank_channel_input"]["path"])
            )
            electrical_map = read_json(
                Path(artifacts["electrical_map"]["path"])
            )
            self.assertEqual(
                {group["direction"] for group in bank_input["groups"]},
                {"a_to_b", "b_to_a"},
            )
            self.assertEqual(
                [domain["id"] for domain in bank_input["domains"]],
                ["link_0_1:shared_bidirectional"],
            )
            self.assertEqual(
                {channel["direction"] for channel in electrical_map["channels"]},
                {"either"},
            )
            concrete_lanes = [
                channel["physical_lane"]
                for channel in electrical_map["channels"]
            ]
            self.assertEqual(len(concrete_lanes), len(set(concrete_lanes)))

            report = run_chimew_phase6_pipeline(
                Path(artifacts["schedule"]["path"]),
                shared_platform,
                Path(artifacts["crossings"]["path"]),
                Path(artifacts["positions"]["path"]),
                Path(artifacts["rudy_input"]["path"]),
                Path(artifacts["bank_channel_input"]["path"]),
                Path(artifacts["electrical_map"]["path"]),
                root / "chimew",
                source_paths={
                    label: Path(path)
                    for label, path in lookahead["sources"].items()
                },
                grouper=self.executables["grouper"],
                refiner=self.executables["refiner"],
                rudy=self.executables["rudy"],
                assigner=self.executables["assigner"],
                region_count=4,
            )
            binding = read_json(
                root
                / "chimew"
                / report["artifacts"]["adapter_electrical_binding"]["path"]
            )
            self.assertEqual(
                {entry["direction"] for entry in binding["entries"]},
                {"a_to_b", "b_to_a"},
            )
            self.assertEqual(
                len(binding["entries"]),
                len({entry["physical_lane"] for entry in binding["entries"]}),
            )
            self.assertEqual(
                validate_chimew_phase6_pipeline(root / "chimew")["status"],
                "pass",
            )

    def test_shared_bidirectional_bundling_is_exact_and_capacity_safe(self) -> None:
        grouped = {}
        schedule = {}
        for direction, count, slot_offset in (
            ("a_to_b", 152, 0),
            ("b_to_a", 196, 1),
        ):
            for index in range(count):
                key = ("shared", direction, index)
                entry_id = f"{direction}-{index}"
                slot = (index + slot_offset) % 4
                grouped[key] = [
                    {
                        "id": entry_id,
                        "fanout": {"x": 0.0, "y": float(index % 31)},
                        "fanins": [
                            {"x": 1.0, "y": float((index * 3) % 31)}
                        ],
                    }
                ]
                schedule[entry_id] = {
                    "id": entry_id,
                    "tdm_ratio": 4 if direction == "a_to_b" else 5,
                    "slot": slot,
                }
        first = _bundle_shared_bidirectional_groups(
            link_id="shared",
            lane_count=300,
            grouped=grouped,
            fixed_lane_by_group={},
            schedule_by_id=schedule,
        )
        second = _bundle_shared_bidirectional_groups(
            link_id="shared",
            lane_count=300,
            grouped=grouped,
            fixed_lane_by_group={},
            schedule_by_id=schedule,
        )
        self.assertEqual(first, second)
        materialized, fixed, _sources, metrics = first
        self.assertEqual(fixed, {})
        self.assertEqual(len(materialized), 300)
        self.assertEqual(metrics["required_pairs"], 48)
        self.assertEqual(metrics["selected_pairs"], 48)
        self.assertGreaterEqual(metrics["maximum_compatible_pairs"], 48)
        bundles = [
            members
            for key, members in materialized.items()
            if key[1] == "bidirectional"
        ]
        self.assertEqual(len(bundles), 48)
        for members in bundles:
            self.assertEqual({member["direction"] for member in members}, {
                "a_to_b",
                "b_to_a",
            })
            slots = {schedule[member["id"]]["slot"] for member in members}
            self.assertEqual(len(slots), len(members))

        colliding_schedule = {
            entry_id: {**entry, "slot": 0}
            for entry_id, entry in schedule.items()
        }
        with self.assertRaisesRegex(
            ValidationError, "maximum_compatible_pairs=0"
        ):
            _bundle_shared_bidirectional_groups(
                link_id="shared",
                lane_count=347,
                grouped=grouped,
                fixed_lane_by_group={},
                schedule_by_id=colliding_schedule,
            )

    def test_shared_bidirectional_matching_agrees_with_small_exact_oracle(self) -> None:
        grouped = {}
        schedule = {}
        for direction, values, slot in (
            ("a_to_b", (0.0, 10.0), 0),
            ("b_to_a", (9.0, 1.0), 1),
        ):
            for index, value in enumerate(values):
                key = ("shared", direction, index)
                entry_id = f"{direction}-{index}"
                grouped[key] = [
                    {
                        "id": entry_id,
                        "fanout": {"x": 0.0, "y": value},
                        "fanins": [{"x": 1.0, "y": value}],
                    }
                ]
                schedule[entry_id] = {
                    "id": entry_id,
                    "tdm_ratio": 2,
                    "slot": slot,
                }
        _groups, _fixed, sources, metrics = _bundle_shared_bidirectional_groups(
            link_id="shared",
            lane_count=2,
            grouped=grouped,
            fixed_lane_by_group={},
            schedule_by_id=schedule,
        )
        selected = {
            (members[0][2], members[1][2])
            for key, members in sources.items()
            if key[1] == "bidirectional"
        }
        # Exhaustive 2x2 oracle: (0->1, 1->0) has total endpoint mismatch 4;
        # the crossed alternative has total mismatch 36.
        self.assertEqual(selected, {(0, 1), (1, 0)})
        self.assertEqual(metrics["required_pairs"], 2)
        self.assertEqual(metrics["maximum_compatible_pairs"], 2)

    def test_shared_bidirectional_timing_guard_preserves_one_bundled_lane(
        self,
    ) -> None:
        """Opposite timing guards become one slot-safe static channel."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir, assignment, routes, schedule = self._upstream(root)
            platform_document = read_json(PLATFORM)
            platform_document["links"][0][
                "capacity_sharing"
            ] = "shared_bidirectional"
            shared_platform = root / "shared-platform.json"
            write_json(shared_platform, platform_document)
            schedule_document = read_json(schedule)
            forward = next(
                entry
                for entry in schedule_document["entries"]
                if (entry["from"], entry["to"]) == ("fpga0", "fpga1")
            )
            reverse = next(
                entry
                for entry in schedule_document["entries"]
                if (entry["from"], entry["to"]) == ("fpga1", "fpga0")
            )
            # Phase 5 may use the same concrete shared lane in different
            # TDM slots; the materializer must preserve it as one bundle.
            reverse["lane"] = forward["lane"]
            # Ratios are direction-qualified occupancy counts.  Opposite
            # directions may differ when concrete shared slots are disjoint.
            forward["tdm_ratio"] = 3
            reverse["tdm_ratio"] = 2
            forward["slot"] = 0
            reverse["slot"] = 1
            write_json(schedule, schedule_document)
            timing = root / "timing.json"
            write_json(
                timing,
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": schedule_document["design"],
                    "paths": [
                        {
                            "clock_period_ns": 10.0,
                            "slack_ns": 0.0,
                            "cut_nets": [forward["net"], reverse["net"]],
                        }
                    ],
                },
            )
            lookahead = materialize_academic_chimew_inputs(
                ir_path=ir,
                schedule_path=schedule,
                routes_path=routes,
                platform_path=shared_platform,
                physical_report=self._physical_report(
                    root / "physical", assignment
                ),
                output_dir=root / "lookahead",
                timing_paths_path=timing,
                timing_path_scope="whole-net",
                grouper=self.executables["grouper"],
                refiner=self.executables["refiner"],
            )
            materialized_schedule = read_json(
                Path(lookahead["artifacts"]["schedule"]["path"])
            )
            timing_guard = materialized_schedule["chimew_timing_guard"]
            self.assertEqual(
                set(timing_guard["fixed_lane_entries"]),
                {forward["id"], reverse["id"]},
            )
            self.assertEqual(
                set(timing_guard["relaxed_shared_bidirectional_entries"]),
                set(),
            )
            self.assertEqual(timing_guard["relaxed_shared_bidirectional_lanes"], [])
            bank_input = read_json(
                Path(lookahead["artifacts"]["bank_channel_input"]["path"])
            )
            electrical_map = read_json(
                Path(lookahead["artifacts"]["electrical_map"]["path"])
            )
            self.assertEqual(
                bank_input["metrics"][
                    "timing_guard_relaxed_shared_bidirectional_lanes"
                ],
                0,
            )
            bundles = [
                group
                for group in bank_input["groups"]
                if group["direction"] == "bidirectional"
            ]
            self.assertEqual(len(bundles), 1)
            self.assertEqual(
                {member["id"] for member in bundles[0]["members"]},
                {forward["id"], reverse["id"]},
            )
            concrete_lanes = [
                channel["physical_lane"] for channel in electrical_map["channels"]
            ]
            self.assertEqual(len(concrete_lanes), len(set(concrete_lanes)))

    def test_timing_driven_materialization_binds_projected_sta_weights(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir, assignment, routes, schedule = self._upstream(root)
            schedule_document = read_json(schedule)
            critical_entry = schedule_document["entries"][0]
            timing = root / "cut-timing-paths.json"
            write_json(
                timing,
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": schedule_document["design"],
                    "normalization": {
                        "positive_slack_scale_ns": 1.0,
                        "negative_slack_scale_ns": 1.0,
                        "max_clock_period_ns": 10.0,
                    },
                    "paths": [
                        {
                            "id": "critical",
                            "clock_domain": "clk",
                            "clock_period_ns": 10.0,
                            "slack_ns": 0.0,
                            "fixed_delay_ns": 10.0,
                            "cut_nets": [critical_entry["net"]],
                            "cut_signature": ["fpga0->fpga1"],
                            "normalized_slack": 0.0,
                            "compressed_path_ids": ["critical"],
                        }
                    ],
                },
            )
            lookahead = materialize_academic_chimew_inputs(
                ir_path=ir,
                schedule_path=schedule,
                routes_path=routes,
                platform_path=PLATFORM,
                physical_report=self._physical_report(
                    root / "physical", assignment
                ),
                output_dir=root / "lookahead",
                timing_paths_path=timing,
                grouper=self.executables["grouper"],
                refiner=self.executables["refiner"],
            )
            self.assertEqual(
                lookahead["timing_weighting"]["weighted_signals"], 1
            )
            self.assertEqual(
                lookahead["timing_weighting"]["maximum_weight"], 10.0
            )
            bank_input = read_json(
                Path(lookahead["artifacts"]["bank_channel_input"]["path"])
            )
            weights = {
                member["id"]: member["timing_weight"]
                for group in bank_input["groups"]
                for member in group["members"]
            }
            self.assertEqual(weights[critical_entry["id"]], 10.0)
            self.assertTrue(
                all(
                    value == 1.0
                    for entry, value in weights.items()
                    if entry != critical_entry["id"]
                )
            )
            materialized_schedule = read_json(
                Path(lookahead["artifacts"]["schedule"]["path"])
            )
            self.assertEqual(
                materialized_schedule["chimew_timing_guard"][
                    "protected_entries"
                ],
                [critical_entry["id"]],
            )
            critical_group = next(
                group
                for group in bank_input["groups"]
                if any(
                    member["id"] == critical_entry["id"]
                    for member in group["members"]
                )
            )
            critical_domain = critical_group["domain"]
            self.assertIn("timing-guard-lane", critical_domain)
            critical_channels = [
                channel
                for pair in bank_input["bank_pairs"]
                if pair["domain"] == critical_domain
                for channel in pair["channels"]
            ]
            self.assertEqual(len(critical_channels), 1)
            self.assertEqual(
                critical_channels[0]["order"], 0
            )
            electrical_map = read_json(
                Path(lookahead["artifacts"]["electrical_map"]["path"])
            )
            critical_electrical_channel = next(
                channel
                for channel in electrical_map["channels"]
                if channel["chimew_channel"] == critical_channels[0]["id"]
            )
            self.assertEqual(
                critical_electrical_channel["physical_lane"],
                critical_entry["lane"],
            )
            self.assertFalse(
                critical_electrical_channel["placement_anchor"]
            )
            self.assertTrue(
                all(
                    channel["placement_anchor"] is False
                    for channel in electrical_map["channels"]
                    if channel["chimew_channel"]
                    != critical_electrical_channel["chimew_channel"]
                )
            )
            report = run_chimew_phase6_pipeline(
                Path(lookahead["artifacts"]["schedule"]["path"]),
                PLATFORM,
                Path(lookahead["artifacts"]["crossings"]["path"]),
                Path(lookahead["artifacts"]["positions"]["path"]),
                Path(lookahead["artifacts"]["rudy_input"]["path"]),
                Path(lookahead["artifacts"]["bank_channel_input"]["path"]),
                Path(lookahead["artifacts"]["electrical_map"]["path"]),
                root / "timing-chimew",
                source_paths={
                    label: Path(path)
                    for label, path in lookahead["sources"].items()
                },
                grouper=self.executables["grouper"],
                refiner=self.executables["refiner"],
                rudy=self.executables["rudy"],
                assigner=self.executables["assigner"],
                region_count=4,
            )
            qualification = read_json(
                root / "timing-chimew" / report["artifacts"]["qualification"]["path"]
            )
            pin_plan = read_json(
                root
                / "timing-chimew"
                / report["artifacts"]["adapter_pin_plan"]["path"]
            )
            planned = {
                entry["schedule_entry"]: entry for entry in pin_plan["entries"]
            }
            self.assertEqual(
                planned[critical_entry["id"]]["physical_lane"],
                critical_entry["lane"],
            )
            electrical_binding = read_json(
                root
                / "timing-chimew"
                / report["artifacts"]["adapter_electrical_binding"]["path"]
            )
            protected_binding = next(
                binding
                for binding in electrical_binding["entries"]
                if critical_entry["id"] in binding["schedule_entries"]
            )
            self.assertFalse(protected_binding["placement_anchor"])
            self.assertEqual(
                qualification["source_binding"]["digests"]["timing_paths"],
                lookahead["timing_weighting"]["source_sha256"],
            )
            self.assertEqual(
                validate_chimew_phase6_pipeline(root / "timing-chimew")[
                    "status"
                ],
                "pass",
            )

    def test_timing_weight_covers_every_hop_of_a_multihop_net(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir, assignment, routes, schedule = self._upstream(root)
            schedule_document = read_json(schedule)
            critical_entry = schedule_document["entries"][0]
            duplicate = dict(critical_entry)
            duplicate["id"] = critical_entry["id"] + "-next-hop"
            duplicate["slot"] = max(
                entry["slot"] for entry in schedule_document["entries"]
            ) + 1
            schedule_document["entries"].append(duplicate)
            write_json(schedule, schedule_document)
            timing = root / "timing.json"
            write_json(
                timing,
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": schedule_document["design"],
                    "paths": [
                        {
                            "clock_period_ns": 10.0,
                            "slack_ns": 0.0,
                            "cut_nets": [critical_entry["net"]],
                        }
                    ],
                },
            )
            lookahead = materialize_academic_chimew_inputs(
                ir_path=ir,
                schedule_path=schedule,
                routes_path=routes,
                platform_path=PLATFORM,
                physical_report=self._physical_report(
                    root / "physical", assignment
                ),
                output_dir=root / "lookahead",
                timing_paths_path=timing,
                grouper=self.executables["grouper"],
                refiner=self.executables["refiner"],
            )
            bank_input = read_json(
                Path(lookahead["artifacts"]["bank_channel_input"]["path"])
            )
            weights = {
                member["id"]: member["timing_weight"]
                for group in bank_input["groups"]
                for member in group["members"]
            }
            self.assertEqual(weights[critical_entry["id"]], 10.0)
            self.assertEqual(weights[duplicate["id"]], 10.0)

    def test_timing_weight_selects_only_the_exact_multicast_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            timing = root / "timing.json"
            write_json(
                timing,
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "d",
                    "paths": [
                        {
                            "id": "critical",
                            "clock_period_ns": 4.0,
                            "slack_ns": -4.0,
                            "normalized_slack": -0.5,
                            "cut_nets": ["n"],
                        }
                    ],
                },
            )
            schedule = {
                "design": "d",
                "entries": [
                    {
                        "id": "side-branch",
                        "net": "n",
                        "from": "a",
                        "to": "b",
                        "link": "ab",
                    },
                    {
                        "id": "critical-hop-0",
                        "net": "n",
                        "from": "a",
                        "to": "c",
                        "link": "ac",
                    },
                    {
                        "id": "critical-hop-1",
                        "net": "n",
                        "from": "c",
                        "to": "d",
                        "link": "cd",
                    },
                ],
            }
            routes = {
                "routes": [
                    {
                        "net": "n",
                        "tree_edges": [
                            {"from": "a", "to": "b", "link": "ab"},
                            {"from": "a", "to": "c", "link": "ac"},
                            {"from": "c", "to": "d", "link": "cd"},
                        ],
                    }
                ],
                "timing": {
                    "paths": [
                        {
                            "path": "critical",
                            "cut_transitions": [
                                {"net": "n", "from": "a", "to": "d"}
                            ],
                        }
                    ]
                },
            }
            weights, source, coverage = _timing_weights(
                timing, schedule, routes
            )
            self.assertEqual(source, timing)
            self.assertEqual(weights["side-branch"], 1.0)
            self.assertEqual(weights["critical-hop-0"], 10.0)
            self.assertEqual(weights["critical-hop-1"], 10.0)
            self.assertEqual(
                coverage, {"exact_path_hops": 2, "whole_net_fallbacks": 0}
            )

    def test_negative_slack_severity_does_not_saturate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            timing = Path(temporary_directory) / "timing.json"
            write_json(
                timing,
                {
                    "schema": "emuflow.sta-paths/v1",
                    "design": "d",
                    "paths": [
                        {
                            "clock_period_ns": 4.0,
                            "slack_ns": -4.0,
                            "normalized_slack": -0.5,
                            "cut_nets": ["worst"],
                        },
                        {
                            "clock_period_ns": 4.0,
                            "slack_ns": -2.0,
                            "normalized_slack": -0.25,
                            "cut_nets": ["less-critical"],
                        },
                    ],
                },
            )
            schedule = {
                "design": "d",
                "entries": [
                    {"id": "worst", "net": "worst"},
                    {"id": "less", "net": "less-critical"},
                ],
            }
            weights, _, coverage = _timing_weights(timing, schedule, {})
            self.assertEqual(weights["worst"], 10.0)
            self.assertEqual(weights["less"], 3.25)
            self.assertEqual(coverage["whole_net_fallbacks"], 2)

    def test_timing_weight_ablation_controls_are_validated(self) -> None:
        schedule = {"design": "d", "entries": []}
        with self.assertRaisesRegex(ValidationError, "weight scale"):
            _timing_weights(None, schedule, {}, scale=-1.0)
        with self.assertRaisesRegex(ValidationError, "path scope"):
            _timing_weights(None, schedule, {}, path_scope="invalid")

    def test_comparison_validator_rejects_tampered_delta(self) -> None:
        digest = "a" * 64
        def physical(wirelength: int, critical: float, wns: float) -> dict:
            return {
                "fpgas": [
                    {
                        "critical_path_ns": critical,
                        "stages": {
                            "vpr_route": {"metrics": {"wirelength": wirelength}}
                        },
                        "physical_result": {
                            "timing": {
                                "wns_ns": wns,
                                "tns_ns": 0.0,
                                "failing_endpoints": 0,
                                "failing_endpoint_constraints": 0,
                            },
                            "closure": {
                                "unrouted_nets": 0,
                                "drc_violations": 0,
                            },
                        },
                    }
                ]
            }

        baseline_physical = physical(100, 10.0, 1.0)
        chimew_physical = physical(90, 9.0, 2.0)
        def runtime(wns: float, tns: float, failing: int) -> dict:
            clock = {
                "worst_slack_bound_ns": wns,
                "total_negative_slack_bound_ns": tns,
                "negative_slack_paths": failing,
            }
            return {
                "system_timing": {
                    "status": "pass" if wns >= 0.0 else "fail",
                    "target_clock": dict(clock),
                    "runtime_clock": dict(clock),
                }
            }

        baseline_runtime = runtime(-2.0, -3.0, 2)
        chimew_runtime = runtime(-1.0, -1.5, 1)
        report = {
            "schema": PHASE6_AB_COMPARISON_SCHEMA,
            "status": "pass",
            "selected_provider": "chimew",
            "baseline_provider": "historical-default-static-phase6",
            "qualification": "academic-virtual-physical-model",
            "frozen_upstream": {
                "emuir_sha256": digest,
                "assignment_sha256": digest,
                "routes_sha256": digest,
                "schedule_sha256": digest,
                "platform_sha256": digest,
            },
            "baseline": {
                "physical": baseline_physical,
                "runtime": baseline_runtime,
            },
            "chimew": {
                "physical": chimew_physical,
                "runtime": chimew_runtime,
            },
            "baseline_physical": {
                "total_wirelength": 100,
                "worst_critical_path_ns": 10.0,
                "worst_wns_ns": 1.0,
                "total_tns_ns": 0.0,
                "failing_endpoints": 0,
                "failing_endpoint_constraints": 0,
                "unrouted_nets": 0,
                "drc_violations": 0,
            },
            "chimew_physical": {
                "total_wirelength": 90,
                "worst_critical_path_ns": 9.0,
                "worst_wns_ns": 2.0,
                "total_tns_ns": 0.0,
                "failing_endpoints": 0,
                "failing_endpoint_constraints": 0,
                "unrouted_nets": 0,
                "drc_violations": 0,
            },
            "physical_delta": {
                "total_wirelength": -10,
                "worst_critical_path_ns": -1.0,
                "worst_wns_ns": 1.0,
                "total_tns_ns": 0.0,
                "failing_endpoints": 0,
                "failing_endpoint_constraints": 0,
            },
            "baseline_system_timing": {
                "target_clock_worst_slack_bound_ns": -2.0,
                "target_clock_total_negative_slack_bound_ns": -3.0,
                "target_clock_negative_slack_paths": 2,
                "runtime_clock_worst_slack_bound_ns": -2.0,
                "runtime_clock_total_negative_slack_bound_ns": -3.0,
                "runtime_clock_negative_slack_paths": 2,
            },
            "chimew_system_timing": {
                "target_clock_worst_slack_bound_ns": -1.0,
                "target_clock_total_negative_slack_bound_ns": -1.5,
                "target_clock_negative_slack_paths": 1,
                "runtime_clock_worst_slack_bound_ns": -1.0,
                "runtime_clock_total_negative_slack_bound_ns": -1.5,
                "runtime_clock_negative_slack_paths": 1,
            },
            "system_timing_delta": {
                "target_clock_worst_slack_bound_ns": 1.0,
                "target_clock_total_negative_slack_bound_ns": 1.5,
                "target_clock_negative_slack_paths": -1,
                "runtime_clock_worst_slack_bound_ns": 1.0,
                "runtime_clock_total_negative_slack_bound_ns": 1.5,
                "runtime_clock_negative_slack_paths": -1,
            },
            "pin_plan_metrics": {"signals": 2},
        }
        with patch(
            "emuflow.multi_fpga_flow.validate_multi_fpga_physical_report",
            return_value={"status": "pass"},
        ):
            self.assertEqual(
                validate_phase6_ab_comparison(report)["status"], "pass"
            )
            report["physical_delta"]["total_wirelength"] = -11
            with self.assertRaisesRegex(ValidationError, "deltas disagree"):
                validate_phase6_ab_comparison(report)

    def test_open_compile_defaults_to_stable_baseline(self) -> None:
        arguments = _build_parser().parse_args(
            [
                "multi-fpga",
                "compile",
                "--yosys-json",
                str(ROOT / "examples/yosys/counter.json"),
                "--platform",
                str(PLATFORM),
                "--out",
                "unused",
            ]
        )
        self.assertEqual(arguments.phase6_provider, "baseline")
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(EmuFlowError, "requires --physical"):
                run_multi_fpga_flow(
                    platform_path=PLATFORM,
                    output_dir=Path(temporary_directory) / "invalid",
                    yosys_json=ROOT / "examples/yosys/counter.json",
                    phase6_provider="chimew",
                )


if __name__ == "__main__":
    unittest.main()
