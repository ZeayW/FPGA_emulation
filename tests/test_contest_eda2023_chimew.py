from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from emuflow.chimew_pipeline import CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER
from emuflow.contest_eda2023_chimew import (
    EDA2023_CONTEST_CHIMEW_AB_SCHEMA,
    EDA2023_CONTEST_CHIMEW_QUALIFICATION,
    materialize_eda2023_contest_chimew_inputs,
    run_eda2023_contest_chimew_ab,
)
from emuflow.errors import ValidationError
from emuflow.io import read_json, write_json


ROOT = Path(__file__).resolve().parents[1]


class Eda2023ContestChimewTest(unittest.TestCase):
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
            ("placement_aware_pin_planner.cpp", "pin_planner"),
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

    def _fixture(self, root: Path) -> tuple[Path, Path, Path]:
        imported = root / "imported"
        imported.mkdir(parents=True)
        dies = [f"Die{index}" for index in range(4)]
        links = [
            {
                "id": "die_link_000_001",
                "endpoints": ["Die0", "Die1"],
                "capacity": 16,
                "kind": "sll",
            },
            {
                "id": "die_link_001_002",
                "endpoints": ["Die1", "Die2"],
                "capacity": 16,
                "kind": "wire",
            },
            {
                "id": "die_link_002_003",
                "endpoints": ["Die2", "Die3"],
                "capacity": 16,
                "kind": "sll",
            },
        ]
        nets = [
            {
                "id": index,
                "source_node": f"g{2 * index}",
                "source_die": dies[0 if index % 4 in (0, 2) else 1],
                "sink_nodes": [f"g{2 * index + 1}"],
                "sink_dies": [dies[3 if index % 4 in (1, 2) else 2]],
            }
            for index in range(8)
        ]
        nets[0]["sink_nodes"].append("g_extra")
        nets[0]["sink_dies"].append("Die3")
        write_json(
            imported / "contest_instance.json",
            {
                "schema": "emuflow.contest-eda2023-instance/v1",
                "name": "eda2023-chimew-fixture",
                "fpgas": ["FPGA0", "FPGA1"],
                "dies": dies,
                "die_to_fpga": {
                    "Die0": "FPGA0",
                    "Die1": "FPGA0",
                    "Die2": "FPGA1",
                    "Die3": "FPGA1",
                },
                "links": links,
                "nets": nets,
                "node_positions": {
                    node: die
                    for net in nets
                    for node, die in [
                        (net["source_node"], net["source_die"]),
                        *zip(net["sink_nodes"], net["sink_dies"]),
                    ]
                },
                "parameters": {},
                "source": {},
            },
        )
        write_json(
            imported / "die_hierarchy.json",
            {
                "schema": "emuflow.die-hierarchy/v1",
                "platform": "eda2023-chimew-fixture",
                "physical_fpgas": [
                    {"id": "FPGA0", "dies": ["Die0", "Die1"]},
                    {"id": "FPGA1", "dies": ["Die2", "Die3"]},
                ],
                "links": [
                    {
                        "id": link["id"],
                        "capacity": 16,
                        "kind": link["kind"],
                    }
                    for link in links
                ],
            },
        )
        write_json(
            imported / "boarddb.json",
            {
                "schema": "emuflow.boarddb/v1",
                "platform": {
                    "name": "eda2023-chimew-fixture",
                    "kind": "virtual",
                    "description": "test contest die graph",
                },
                "fpgas": [
                    {
                        "id": die,
                        "part": "academic-contest-die",
                        "utilization_limit": 1.0,
                        "capacity": {"lut": 1},
                    }
                    for die in dies
                ],
                "links": [
                    {
                        "id": link["id"],
                        "endpoints": link["endpoints"],
                        "direction": "full_duplex",
                        "mode": "abstract",
                        "data_lanes_per_direction": 16,
                        "fabric_clock_mhz": 1000.0,
                        "latency_cycles": 0,
                    }
                    for link in links
                ],
            },
        )
        routes = root / "routes.json"
        write_json(
            routes,
            {
                "schema": "emuflow.system-routes/v1",
                "design": "eda2023-chimew-fixture",
                "platform": "eda2023-chimew-fixture",
                "constraints": {},
                "routes": [
                    {
                        "id": f"d{index:06d}",
                        "net": f"net_{index:07d}",
                        "source": net["source_die"],
                        "sinks": net["sink_dies"],
                        "width_bits": 1,
                        "tree_edges": [
                            *(
                                [
                                    {
                                        "link": links[0]["id"],
                                        "from": "Die0",
                                        "to": "Die1",
                                    }
                                ]
                                if net["source_die"] == "Die0"
                                else []
                            ),
                            {
                                "link": links[1]["id"],
                                "from": "Die1",
                                "to": "Die2",
                            },
                            *(
                                [
                                    {
                                        "link": links[2]["id"],
                                        "from": "Die2",
                                        "to": "Die3",
                                    }
                                ]
                                if "Die3" in net["sink_dies"]
                                else []
                            ),
                        ],
                    }
                    for index, net in enumerate(nets)
                ],
            },
        )
        tdm = root / "tdm_plan.json"
        write_json(
            tdm,
            {
                "schema": "emuflow.contest-eda2023-tdm/v1",
                "instance": "eda2023-chimew-fixture",
                "provider": "cpp-lagrangian-kkt-direction-separated-v1",
                "hops": [
                    {
                        "index": index,
                        "net": f"net_{index:07d}",
                        "official_net_id": index,
                        "link": links[1]["id"],
                        "from": "Die1",
                        "to": "Die2",
                        "direction": 0,
                        "lane": index % 4,
                        "ratio": 2,
                        "continuous_ratio": 2.0,
                    }
                    for index in range(8)
                ],
            },
        )
        return imported, routes, tdm

    def test_source_bound_contest_ab_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            imported, routes, tdm = self._fixture(root)
            report = run_eda2023_contest_chimew_ab(
                import_dir=imported,
                routes_path=routes,
                tdm_plan_path=tdm,
                output_dir=root / "ab",
                **self.executables,
            )
            self.assertEqual(report["schema"], EDA2023_CONTEST_CHIMEW_AB_SCHEMA)
            self.assertEqual(
                report["qualification"], EDA2023_CONTEST_CHIMEW_QUALIFICATION
            )
            self.assertEqual(report["metrics"]["signals"], 8)
            self.assertEqual(report["metrics"]["routed_sll_hops"], 9)
            self.assertLessEqual(
                report["metrics"]["chimew"]["routed_sll_crossing_bits"],
                report["metrics"]["baseline"]["routed_sll_crossing_bits"],
            )
            crossings = read_json(root / "ab/materialized/inputs/crossings.json")
            actual = [
                (entry["source_slls"], entry["sink_slls"], entry["encoding"])
                for entry in crossings["entries"]
            ]
            self.assertEqual(
                actual,
                [
                    ([0], [0], 3),
                    ([], [0], 2),
                    ([0], [0], 3),
                    ([], [], 0),
                    ([0], [], 1),
                    ([], [0], 2),
                    ([0], [0], 3),
                    ([], [], 0),
                ],
            )
            bank_input = read_json(
                root / "ab/materialized/inputs/bank_channel_input.json"
            )
            self.assertEqual(bank_input["metrics"]["fanins"], 9)
            self.assertEqual(
                read_json(root / "ab/chimew/pipeline_report.json")["provider"],
                CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER,
            )
            self.assertEqual(
                read_json(root / "ab/materialized/schedule.json")[
                    "transport_semantics"
                ],
                "registered-boundary",
            )
            self.assertIn("synthetic package pins", report["claim_boundary"])
            self.assertTrue((root / "ab/baseline_pin_plan.json").is_file())

    def test_invalid_tdm_link_is_rejected_before_native_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            imported, routes, tdm = self._fixture(root)
            document = read_json(tdm)
            document["hops"][0]["link"] = "missing"
            write_json(tdm, document)
            with self.assertRaisesRegex(ValidationError, "TDM hop is invalid"):
                materialize_eda2023_contest_chimew_inputs(
                    import_dir=imported,
                    routes_path=routes,
                    tdm_plan_path=tdm,
                    output_dir=root / "materialized",
                    grouper=self.executables["grouper"],
                    refiner=self.executables["refiner"],
                )

    def test_tdm_hop_must_match_routed_external_edge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            imported, routes, tdm = self._fixture(root)
            document = read_json(tdm)
            document["hops"][0]["from"] = "Die2"
            document["hops"][0]["to"] = "Die1"
            document["hops"][0]["direction"] = 1
            write_json(tdm, document)
            with self.assertRaisesRegex(ValidationError, "routed external edge"):
                materialize_eda2023_contest_chimew_inputs(
                    import_dir=imported,
                    routes_path=routes,
                    tdm_plan_path=tdm,
                    output_dir=root / "materialized",
                    grouper=self.executables["grouper"],
                    refiner=self.executables["refiner"],
                )


if __name__ == "__main__":
    unittest.main()
