from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

from emuflow.chimew_bank_channel import (
    CHIMEW_BANK_CHANNEL_INPUT_PROVIDER,
    CHIMEW_BANK_CHANNEL_INPUT_SCHEMA,
)
from emuflow.chimew_grouping import (
    CHIMEW_CROSSING_PROVIDER,
    CHIMEW_CROSSING_SCHEMA,
    build_chimew_initial_groups,
)
from emuflow.chimew_phase6 import (
    CHIMEW_ELECTRICAL_MAP_PROVIDER,
    CHIMEW_ELECTRICAL_MAP_SCHEMA,
)
from emuflow.chimew_pipeline import (
    CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER,
    run_chimew_phase6_pipeline,
    validate_chimew_phase6_pipeline,
)
from emuflow.chimew_qualification import canonical_sha256
from emuflow.chimew_refinement import (
    CHIMEW_POSITION_PROVIDER,
    CHIMEW_POSITION_SCHEMA,
    refine_chimew_groups,
)
from emuflow.chimew_rudy import (
    CHIMEW_RUDY_INPUT_PROVIDER,
    CHIMEW_RUDY_INPUT_SCHEMA,
)
from emuflow.contest_iccad2019 import (
    import_iccad2019_instance,
    materialize_iccad2019_rtl_boarddb,
)
from emuflow.io import read_json, write_json
from emuflow.phase3 import run_phase3
from emuflow.phase4 import run_phase4
from emuflow.phase5 import run_phase5
from emuflow.phase6 import run_phase6, validate_phase6
from emuflow.phase7c import run_phase7c
from emuflow.platform import Platform
from emuflow.yosys import import_yosys_json
from tests.native_build import tlr_router


ROOT = Path(__file__).resolve().parents[1]

ICCAD2019_SAMPLE = """\
8 11 5 3
0 1
0 4
0 6
1 2
1 5
1 6
2 7
3 7
4 5
5 6
6 7
0 1
1 5
5 6
0 4 5 6
5 7
0 1 2
3
4
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ContestChimewCrossFlowTest(unittest.TestCase):
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

    def _chimew_inputs(
        self,
        root: Path,
        schedule: dict,
        platform_path: Path,
        routes_path: Path,
        ir_path: Path,
    ) -> tuple[dict, dict, dict, dict, dict, dict[str, Path]]:
        platform = Platform.load(platform_path)
        link_by_id = {link.id: link for link in platform.links}
        entries = schedule["entries"]
        self.assertGreater(len(entries), 0)

        placement_source = root / "lookahead-placement.json"
        write_json(
            placement_source,
            {
                "schema": "emuflow.test-lookahead-placement/v1",
                "entries": [
                    {
                        "schedule_entry": entry["id"],
                        "source_x": float(index % 17 + 1),
                        "source_y": float(index + 1),
                        "sink_x": float(index % 17 + 51),
                    }
                    for index, entry in enumerate(entries)
                ],
            },
        )
        package_source = root / "package-pin-inventory.json"
        package_records = []
        groups_per_link = defaultdict(int)

        source_paths = {
            "routing": routes_path,
            "placement": placement_source,
            "netlist": ir_path,
            "architecture": platform_path,
            "package_pins": package_source,
        }
        source_digests = {
            label: _sha256(path)
            for label, path in source_paths.items()
            if label != "package_pins"
        }

        crossings = {
            "schema": CHIMEW_CROSSING_SCHEMA,
            "provider": CHIMEW_CROSSING_PROVIDER,
            "design": schedule["design"],
            "platform": schedule["platform"],
            "slls_per_fpga": 4,
            "provenance": {
                "producer": "emuflow-cross-flow-route-adapter",
                "producer_version": "1",
                "routing_sha256": source_digests["routing"],
            },
            "metrics": {
                "signals": len(entries),
                "physical_sll_crossings": len(entries),
            },
            "entries": [
                {
                    "schedule_entry": entry["id"],
                    "source_slls": [index % 4],
                    "sink_slls": [],
                    "encoding": 1 << (index % 4),
                }
                for index, entry in enumerate(entries)
            ],
        }
        positions = {
            "schema": CHIMEW_POSITION_SCHEMA,
            "provider": CHIMEW_POSITION_PROVIDER,
            "design": schedule["design"],
            "platform": schedule["platform"],
            "coordinate_system": "physical-site-y",
            "provenance": {
                "producer": "emuflow-cross-flow-placement-adapter",
                "producer_version": "1",
                "placement_sha256": source_digests["placement"],
            },
            "metrics": {"signals": len(entries)},
            "entries": [
                {
                    "schedule_entry": entry["id"],
                    "source_y": float(index + 1),
                }
                for index, entry in enumerate(entries)
            ],
        }
        initial = build_chimew_initial_groups(
            schedule, crossings, executable=self.executables["grouper"]
        )
        refined = refine_chimew_groups(
            schedule,
            crossings,
            initial,
            positions,
            executable=self.executables["refiner"],
        )
        group_by_entry = {
            record["schedule_entry"]: record["group"]
            for record in refined["entries"]
        }

        grouped_members = defaultdict(list)
        group_direction = {}
        for index, entry in enumerate(entries):
            link = link_by_id[entry["link"]]
            endpoint_a, endpoint_b = link.endpoints
            self.assertIn(
                (entry["from"], entry["to"]),
                ((endpoint_a, endpoint_b), (endpoint_b, endpoint_a)),
            )
            direction = (
                "a_to_b"
                if (entry["from"], entry["to"]) == (endpoint_a, endpoint_b)
                else "b_to_a"
            )
            key = (entry["link"], group_by_entry[entry["id"]])
            if key in group_direction:
                self.assertEqual(group_direction[key], direction)
            group_direction[key] = direction
            grouped_members[key].append(
                {
                    "id": entry["id"],
                    "fanout": {
                        "x": 0.0 if direction == "a_to_b" else 100.0,
                        "y": float(index + 1),
                    },
                    "fanins": [
                        {
                            "x": 100.0 if direction == "a_to_b" else 0.0,
                            "y": float(index + 1),
                        }
                    ],
                }
            )

        domains = []
        bank_pairs = []
        electrical_channels = []
        bank_groups = []
        for group_index, ((link_id, group_id), members) in enumerate(
            sorted(grouped_members.items())
        ):
            link = link_by_id[link_id]
            endpoint_a, endpoint_b = link.endpoints
            if groups_per_link[link_id] == 0:
                domains.append(
                    {"id": link_id, "fpga_a": endpoint_a, "fpga_b": endpoint_b}
                )
                bank_pairs.append(
                    {
                        "id": f"bank-{link_id}",
                        "domain": link_id,
                        "bank_a": {"id": f"{endpoint_a}-bank", "x": 0.0, "y": 0.0},
                        "bank_b": {"id": f"{endpoint_b}-bank", "x": 100.0, "y": 0.0},
                        "channels": [],
                    }
                )
            physical_lane = groups_per_link[link_id]
            self.assertLess(physical_lane, link.data_lanes_per_direction)
            groups_per_link[link_id] += 1
            channel_id = f"channel-{link_id}-{physical_lane:02d}"
            bank_pair = next(item for item in bank_pairs if item["domain"] == link_id)
            bank_pair["channels"].append(
                {
                    "id": channel_id,
                    "order": physical_lane,
                    "pin_a": {"x": 0.0, "y": float(group_index + 1)},
                    "pin_b": {"x": 100.0, "y": float(group_index + 1)},
                }
            )
            pin_a = f"{endpoint_a}_{link_id}_{physical_lane}_P"
            pin_b = f"{endpoint_b}_{link_id}_{physical_lane}_P"
            package_records.extend(
                [
                    {"fpga": endpoint_a, "pin": pin_a},
                    {"fpga": endpoint_b, "pin": pin_b},
                ]
            )
            electrical_channels.append(
                {
                    "chimew_channel": channel_id,
                    "link": link_id,
                    "physical_lane": physical_lane,
                    "direction": "either",
                    "bank_a": f"{endpoint_a}-bank",
                    "bank_b": f"{endpoint_b}-bank",
                    "package_pin_a": pin_a,
                    "package_pin_b": pin_b,
                    "iostandard": "LVCMOS18",
                    "supported_iostandards": ["LVCMOS18"],
                    "bank_voltage": 1.8,
                    "electrical_class": "single_ended_parallel",
                    "reserved": False,
                }
            )
            bank_groups.append(
                {
                    "id": f"group-{link_id}-{group_id}",
                    "domain": link_id,
                    "kind": "tdm_group",
                    "direction": group_direction[(link_id, group_id)],
                    "members": members,
                }
            )

        write_json(package_source, {"pins": package_records})
        source_digests["package_pins"] = _sha256(package_source)
        rudy_input = {
            "schema": CHIMEW_RUDY_INPUT_SCHEMA,
            "provider": CHIMEW_RUDY_INPUT_PROVIDER,
            "design": schedule["design"],
            "platform": schedule["platform"],
            "coordinate_system": "physical-site-xy",
            "degenerate_bbox_policy": "reject",
            "wire_pitch_per_layer": 1.0,
            "max_utilization": 1.0,
            "provenance": {
                "producer": "emuflow-cross-flow-lookahead",
                "producer_version": "1",
                "placement_sha256": source_digests["placement"],
                "netlist_sha256": source_digests["netlist"],
                "architecture_sha256": source_digests["architecture"],
            },
            "grid": {
                "origin_x": 0.0,
                "origin_y": 0.0,
                "bin_width": 200.0,
                "bin_height": 200.0,
                "columns": 1,
                "rows": 1,
                "capacities": [1000000.0],
            },
            "metrics": {"nets": len(entries), "pins": 2 * len(entries)},
            "nets": [
                {
                    "id": f"lookahead-{entry['id']}",
                    "pins": [
                        {"x": 1.0, "y": float(index + 1)},
                        {"x": 101.0, "y": float(index + 2)},
                    ],
                }
                for index, entry in enumerate(entries)
            ],
        }
        bank_input = {
            "schema": CHIMEW_BANK_CHANNEL_INPUT_SCHEMA,
            "provider": CHIMEW_BANK_CHANNEL_INPUT_PROVIDER,
            "design": schedule["design"],
            "platform": schedule["platform"],
            "coordinate_system": "physical-site-xy",
            "cost_quantization_per_site": 1000,
            "provenance": {
                "producer": "emuflow-cross-flow-lookahead",
                "producer_version": "1",
                "grouping_sha256": canonical_sha256(refined),
                "placement_sha256": source_digests["placement"],
                "architecture_sha256": source_digests["architecture"],
            },
            "domains": domains,
            "bank_pairs": bank_pairs,
            "groups": bank_groups,
            "metrics": {
                "groups": len(bank_groups),
                "signals": len(entries),
                "fanins": len(entries),
                "bank_pairs": len(bank_pairs),
                "channels": len(electrical_channels),
            },
        }
        electrical_map = {
            "schema": CHIMEW_ELECTRICAL_MAP_SCHEMA,
            "provider": CHIMEW_ELECTRICAL_MAP_PROVIDER,
            "design": schedule["design"],
            "platform": schedule["platform"],
            "provenance": {
                "producer": "emuflow-cross-flow-academic-bsp",
                "producer_version": "1",
                "boarddb_sha256": _sha256(platform_path),
                "package_pin_inventory_sha256": source_digests["package_pins"],
            },
            "fpga_y_bounds": [
                {"fpga": fpga, "y_min": 0.0, "y_max": 200.0}
                for fpga in sorted(
                    {
                        endpoint
                        for domain in domains
                        for endpoint in (domain["fpga_a"], domain["fpga_b"])
                    }
                )
            ],
            "channels": electrical_channels,
            "metrics": {
                "channels": len(electrical_channels),
                "package_pins": len(package_records),
                "concrete_lanes": len(electrical_channels),
            },
        }
        return (
            crossings,
            positions,
            rudy_input,
            bank_input,
            electrical_map,
            source_paths,
        )

    def test_contest_boarddb_runs_through_phase7_with_source_bound_chimew(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contest_source = root / "SampleInput"
            contest_source.write_text(ICCAD2019_SAMPLE, encoding="utf-8")
            normalized = root / "contest-normalized"
            import_iccad2019_instance(contest_source, normalized, "iccad2019_sample")
            platform_path = root / "contest-rtl-boarddb.json"
            materialize_iccad2019_rtl_boarddb(
                instance_path=normalized / "contest_instance.json",
                device_template_path=(
                    ROOT / "platforms/virtual/academic_vtr_4fpga_mesh.json"
                ),
                output_path=platform_path,
                name="iccad2019_sample_academic_rtl",
                lane_scale=32,
            )

            ir = import_yosys_json(
                ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"]
            )
            ir_path = root / "counter.emuir.json"
            write_json(ir_path, ir.to_dict())
            phase3_root = root / "phase3"
            phase4_root = root / "phase4"
            phase5_root = root / "phase5"
            phase6_root = root / "phase6"
            phase7_root = root / "phase7"
            phase3 = run_phase3(
                ir_path,
                platform_path,
                phase3_root,
                provider="greedy",
                cut_mode="sequential-only",
                min_used_fpgas=2,
                balance_tolerance=1.0,
            )
            phase4 = run_phase4(
                phase3_root / "assignment.json",
                platform_path,
                phase4_root,
                router=str(tlr_router()),
            )
            phase5 = run_phase5(
                phase4_root / "routes.json",
                platform_path,
                phase5_root,
                simulation_frames=3,
            )
            schedule_path = phase5_root / "schedule.json"
            schedule = read_json(schedule_path)
            (
                crossings,
                positions,
                rudy_input,
                bank_input,
                electrical_map,
                source_paths,
            ) = self._chimew_inputs(
                root,
                schedule,
                platform_path,
                phase4_root / "routes.json",
                ir_path,
            )
            inputs = {}
            for label, document in (
                ("crossings", crossings),
                ("positions", positions),
                ("rudy", rudy_input),
                ("bank", bank_input),
                ("electrical", electrical_map),
            ):
                inputs[label] = root / f"chimew-{label}.json"
                write_json(inputs[label], document)
            pipeline_root = root / "chimew-pipeline"
            pipeline = run_chimew_phase6_pipeline(
                schedule_path,
                platform_path,
                inputs["crossings"],
                inputs["positions"],
                inputs["rudy"],
                inputs["bank"],
                inputs["electrical"],
                pipeline_root,
                source_paths=source_paths,
                **self.executables,
            )
            pipeline_validation = validate_chimew_phase6_pipeline(pipeline_root)
            repeat_root = root / "chimew-pipeline-repeat"
            repeated_pipeline = run_chimew_phase6_pipeline(
                schedule_path,
                platform_path,
                inputs["crossings"],
                inputs["positions"],
                inputs["rudy"],
                inputs["bank"],
                inputs["electrical"],
                repeat_root,
                source_paths=source_paths,
                **self.executables,
            )
            self.assertEqual(pipeline, repeated_pipeline)
            self.assertEqual(
                read_json(pipeline_root / "phase6-adapter/pin_plan.json"),
                read_json(repeat_root / "phase6-adapter/pin_plan.json"),
            )
            adapter_root = pipeline_root / "phase6-adapter"
            phase6 = run_phase6(
                ir_path,
                phase3_root / "assignment.json",
                schedule_path,
                platform_path,
                phase6_root,
                equivalence_cycles=4,
                pin_plan_path=adapter_root / "pin_plan.json",
                position_hints_path=adapter_root / "position_hints.json",
                electrical_binding_path=adapter_root / "electrical_binding.json",
            )
            phase6_validation = validate_phase6(
                ir_path,
                phase3_root / "assignment.json",
                schedule_path,
                platform_path,
                phase6_root / "manifest.json",
            )
            phase7 = run_phase7c(
                schedule_path,
                platform_path,
                phase3_root / "phase3_report.json",
                phase4_root / "phase4_report.json",
                phase5_root / "phase5_report.json",
                phase6_root / "phase6_report.json",
                phase7_root,
                simulation_frames=3,
            )

            self.assertEqual(phase3["status"], "pass")
            self.assertEqual(phase4["status"], "pass")
            self.assertEqual(phase5["status"], "pass")
            self.assertEqual(
                pipeline["provider"], CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER
            )
            self.assertEqual(
                pipeline_validation["qualification_scope"],
                "byte-bound-source-artifacts",
            )
            self.assertEqual(phase6["status"], "pass")
            self.assertEqual(phase6_validation["status"], "pass")
            self.assertEqual(phase6["equivalence"]["mismatches"], 0)
            # Phase 7C is deliberately only generated here: the cross-contract
            # test has no fabricated physical summary.  Real physical closure
            # remains a separate HPC evidence gate.
            self.assertEqual(phase7["status"], "generated")
            self.assertEqual(phase7["validation"]["status"], "pass")
            self.assertEqual(
                read_json(platform_path)["platform"]["provenance"]["interconnect"][
                    "schema"
                ],
                "emuflow.contest-iccad2019-instance/v1",
            )


if __name__ == "__main__":
    unittest.main()
