import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.multi_fpga_flow import run_multi_fpga_flow
from emuflow.experiment_partition import _rebuild_timing_path_objective
from emuflow.io import read_json, write_json
from emuflow.mfspart_provider import (
    _partition_graph,
    _timing_path_groups,
    refine_mfspart_partition,
)
from emuflow.combinational_cut import STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
from emuflow.partition import (
    CUT_MODE_STATIC_EXACT,
    build_clusters,
    build_partition_assignment,
    load_partition_constraints,
    normalize_partition_constraints,
)
from emuflow.phase3 import run_phase3
from emuflow.platform import Platform
from emuflow.routing import load_route_constraints
from emuflow.yosys import import_yosys_json
from tests.native_build import tlr_router
from tests.test_combinational_cut import _chain_ir


ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / "platforms/virtual/xcvu3p_2fpga_p2p.json"
FAKE_OPENSTA = ROOT / "tests/fixtures/fake_opensta_paths.py"


class MFSPartPhase3Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        compiler = shutil.which("g++") or shutil.which("clang++")
        if compiler is None:
            raise unittest.SkipTest("a C++17 compiler is required")
        cls.temporary_directory = tempfile.TemporaryDirectory()
        native_bin = Path(cls.temporary_directory.name) / "bin"
        native_bin.mkdir()
        cls.executables = {}
        for name in (
            "coarsener",
            "initializer",
            "refiner",
            "refiner_checker",
            "legalizer",
        ):
            executable = (
                native_bin / f"emuflow_mfspart_{name}"
            )
            subprocess.run(
                [
                    compiler,
                    "-std=c++17",
                    "-O2",
                    "-Wall",
                    "-Wextra",
                    "-Wpedantic",
                    "-Werror",
                    str(ROOT / f"src/native/mfspart_{name}.cpp"),
                    "-o",
                    str(executable),
                ],
                check=True,
            )
            cls.executables[name] = str(executable)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def test_counter_runs_serial_mfspart_through_common_phase3_contract(self) -> None:
        ir = import_yosys_json(
            ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"]
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "counter.emuir.json"
            ir_path.write_text(json.dumps(ir.to_dict()), encoding="utf-8")
            report = run_phase3(
                ir_path,
                PLATFORM,
                root / "phase3",
                seed=23,
                provider="mfspart",
                cut_mode="sequential-only",
                mfspart_coarsener=self.executables["coarsener"],
                mfspart_initializer=self.executables["initializer"],
                mfspart_refiner=self.executables["refiner"],
                mfspart_refiner_checker=self.executables["refiner_checker"],
                mfspart_legalizer=self.executables["legalizer"],
            )
            assignment = json.loads(
                (root / "phase3/assignment.json").read_text(encoding="utf-8")
            )
            self.assertTrue((root / "phase3/mfspart/hierarchy.json").is_file())
            self.assertTrue(
                (root / "phase3/mfspart/initial_partition.json").is_file()
            )
            self.assertTrue(
                (root / "phase3/mfspart/uncoarsening.json").is_file()
            )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["validation"]["status"], "pass")
        self.assertEqual(report["validation"]["instances"], 8)
        self.assertEqual(report["validation"]["used_fpgas"], 2)
        self.assertEqual(
            assignment["provider"], "mfspart-serial-paper-reproduction-v1"
        )
        self.assertEqual(
            set(assignment["cluster_assignment"]),
            {f"c{index:06d}" for index in range(8)},
        )

    def test_phase3_cli_selects_mfspart_provider(self) -> None:
        ir = import_yosys_json(
            ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"]
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "counter.emuir.json"
            ir_path.write_text(json.dumps(ir.to_dict()), encoding="utf-8")
            command = [
                sys.executable,
                "-c",
                "from emuflow.cli import main; raise SystemExit(main())",
                "phase3",
                "--ir",
                str(ir_path),
                "--platform",
                str(PLATFORM),
                "--out",
                str(root / "phase3"),
                "--provider",
                "mfspart",
                "--mfspart-coarsener",
                self.executables["coarsener"],
                "--mfspart-initializer",
                self.executables["initializer"],
                "--mfspart-refiner",
                self.executables["refiner"],
                "--mfspart-refiner-checker",
                self.executables["refiner_checker"],
                "--mfspart-legalizer",
                self.executables["legalizer"],
            ]
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(ROOT / "src")
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout)
            report = json.loads(completed.stdout)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["provider"], "mfspart-serial-paper-reproduction-v1")

    def test_min_used_legalizer_handles_intentionally_loose_balance(self) -> None:
        ir = import_yosys_json(
            ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"]
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "counter.emuir.json"
            ir_path.write_text(json.dumps(ir.to_dict()), encoding="utf-8")
            report = run_phase3(
                ir_path,
                PLATFORM,
                root / "phase3",
                provider="mfspart",
                cut_mode="sequential-only",
                min_used_fpgas=2,
                balance_tolerance=10.0,
                mfspart_coarsener=self.executables["coarsener"],
                mfspart_initializer=self.executables["initializer"],
                mfspart_refiner=self.executables["refiner"],
                mfspart_refiner_checker=self.executables["refiner_checker"],
                mfspart_legalizer=self.executables["legalizer"],
            )
            assignment = json.loads(
                (root / "phase3/assignment.json").read_text(encoding="utf-8")
            )
        self.assertEqual(report["validation"]["used_fpgas"], 2)
        self.assertGreater(
            assignment["provider_metadata"]["min_used_legalization_moves"], 0
        )

    def test_directional_post_refinement_preserves_tritonpart_contract(self) -> None:
        ir = import_yosys_json(
            ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"]
        )
        platform = Platform.load(PLATFORM)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ir_path = root / "counter.emuir.json"
            ir_path.write_text(json.dumps(ir.to_dict()), encoding="utf-8")
            run_phase3(
                ir_path,
                PLATFORM,
                root / "phase3",
                seed=19,
                provider="greedy",
                cut_mode="sequential-only",
            )
            clusters = read_json(root / "phase3/clusters.json")
            initial = read_json(root / "phase3/assignment.json")
            constraints = load_partition_constraints(None, ir, platform)
            refined, report = refine_mfspart_partition(
                ir,
                platform,
                clusters,
                constraints,
                load_route_constraints(None, platform),
                initial,
                root / "post-refinement",
                early_stop=4,
                refiner=self.executables["refiner"],
                refiner_checker=self.executables["refiner_checker"],
            )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["direction_source"], "EmuIR net drivers/sinks")
        self.assertEqual(refined["provider"], initial["provider"])
        self.assertEqual(
            refined["provider_metadata"][
                "directional_mfspart_post_refinement"
            ]["initial_assignment_provider"],
            initial["provider"],
        )
        self.assertEqual(
            set(refined["cluster_assignment"]),
            set(initial["cluster_assignment"]),
        )

    def test_directional_graph_uses_emuir_driver_identity(self) -> None:
        ir = import_yosys_json(
            ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"]
        )
        platform = Platform.load(PLATFORM)
        constraints = load_partition_constraints(None, ir, platform)
        clusters = build_clusters(ir, constraints)
        _nodes, graph_nets, _dimensions = _partition_graph(
            ir, clusters, platform, {}
        )
        cluster_by_instance = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        ir_nets = {net["id"]: net for net in ir.value["nets"]}
        self.assertTrue(graph_nets)
        for graph_net in graph_nets:
            net_id = graph_net["id"].rsplit("#d", 1)[0]
            net = ir_nets[net_id]
            driver_clusters = {
                cluster_by_instance[endpoint["instance"]]
                for endpoint in net["drivers"]
                if endpoint["instance"] is not None
            }
            sink_clusters = {
                cluster_by_instance[endpoint["instance"]]
                for endpoint in net["sinks"]
                if endpoint["instance"] is not None
            }
            self.assertIn(graph_net["source"], driver_clusters)
            self.assertTrue(set(graph_net["sinks"]).issubset(sink_clusters))
            self.assertEqual(graph_net["logical_net"], net_id)
            self.assertEqual(graph_net["cut_class"], net["cut_class"])

    def test_exact_guard_replaces_a_register_cut_with_a_valuable_comb_cut(self) -> None:
        ir = _chain_ir()
        platform = Platform.load(PLATFORM)
        constraints = normalize_partition_constraints(
            {
                "schema": "emuflow.partition-constraints/v1",
                "balance_tolerance": 1.0,
                "fixed": [
                    {"instance": "q0", "fpga": "fpga0"},
                    {"instance": "q1", "fpga": "fpga1"},
                ],
            },
            ir,
            platform,
        )
        clusters = build_clusters(
            ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=8,
            static_exact_candidate_policy=STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
        )
        cluster_for = {
            instance: cluster["id"]
            for cluster in clusters["clusters"]
            for instance in cluster["instances"]
        }
        initial_targets = {
            "q0": "fpga0",
            "l0": "fpga1",
            "l1": "fpga1",
            "l2": "fpga1",
            "q1": "fpga1",
        }
        initial = build_partition_assignment(
            ir,
            platform,
            clusters,
            constraints,
            {
                cluster_for[instance]: target
                for instance, target in initial_targets.items()
            },
            provider="test",
            seed=0,
        )
        self.assertEqual(initial["metrics"]["combinational_cut_nets"], 0)
        with tempfile.TemporaryDirectory() as temporary_directory:
            refined, report = refine_mfspart_partition(
                ir,
                platform,
                clusters,
                constraints,
                load_route_constraints(None, platform, frame_slots=32),
                initial,
                Path(temporary_directory),
                net_weights={"q": 10.0},
                early_stop=3,
                refiner=self.executables["refiner"],
                refiner_checker=self.executables["refiner_checker"],
            )
        self.assertEqual(report["topology_guard"], (
            "non-combinational-net-worst-sink-distance-non-regression-v1"
        ))
        self.assertGreater(report["guarded_nets"], 0)
        self.assertGreater(refined["metrics"]["combinational_cut_nets"], 0)
        self.assertEqual(refined["instance_assignment"]["l0"], "fpga0")

    def test_timing_path_groups_follow_ordered_drivers_not_fanout_branches(self) -> None:
        ir = _chain_ir()
        platform = Platform.load(PLATFORM)
        constraints = load_partition_constraints(None, ir, platform)
        clusters = build_clusters(
            ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=3,
            static_exact_candidate_policy=STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
        )
        node_index = {
            cluster["id"]: index
            for index, cluster in enumerate(
                sorted(clusters["clusters"], key=lambda item: item["id"])
            )
        }
        path = {
            "id": "p0",
            "clock_domain": "clk",
            "clock_period_ns": 10.0,
            "slack_ns": 0.0,
            "fixed_delay_ns": 0.0,
            "path_nets": ["q", "n0", "n1", "d"],
            "normalized_slack": 0.0,
            "startpoint": {
                "object": "q0/Q",
                "instance": "q0",
                "port": "Q",
                "bit": 0,
            },
            "endpoint": {
                "object": "q1/D",
                "instance": "q1",
                "port": "D",
                "bit": 0,
            },
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "paths.json"
            write_json(
                database,
                {
                    "schema": "emuflow.sta-path-database/v1",
                    "design": ir.value["design"]["name"],
                    "paths": [path, {**path, "id": "p1"}],
                },
            )
            groups, evidence = _timing_path_groups(
                ir, clusters, node_index, database
            )
            with patch(
                "emuflow.mfspart_provider._sha256",
                side_effect=AssertionError("online path must not hash"),
            ):
                online_groups, online_evidence = _timing_path_groups(
                    ir,
                    clusters,
                    node_index,
                    database,
                    include_hash_evidence=False,
                )
            ir_path = Path(temporary_directory) / "design.emuir.json"
            clusters_path = Path(temporary_directory) / "clusters.json"
            write_json(ir_path, ir.value)
            write_json(clusters_path, clusters)
            independently_rebuilt = _rebuild_timing_path_objective(
                ir_path, clusters_path, database
            )
        self.assertEqual(evidence["eligible_paths"], 2)
        self.assertEqual(evidence["compressed_groups"], 1)
        self.assertEqual(groups[0]["weight"], 2.0)
        self.assertEqual(len(groups[0]["pins"]), 5)
        self.assertEqual(independently_rebuilt, evidence)
        self.assertEqual(online_groups, groups)
        self.assertFalse(
            any("sha256" in key for key in online_evidence)
        )

    def test_counter_runs_affected_multi_fpga_flow_with_mfspart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = {
                "EMUFLOW_NATIVE_ROOT": self.temporary_directory.name,
            }
            with patch.dict(os.environ, environment):
                report = run_multi_fpga_flow(
                    platform_path=PLATFORM,
                    output_dir=root / "flow",
                    yosys_json=ROOT / "examples/yosys/counter.json",
                    top="counter",
                    clocks=["clk"],
                    partition_provider="mfspart",
                    cut_mode="sequential-only",
                    timing_driven=False,
                    clock_periods={"clk": 10.0},
                    opensta=str(FAKE_OPENSTA),
                    router=str(tlr_router()),
                    frame_slots=32,
                    equivalence_cycles=2,
                )
            self.assertTrue((root / "flow/tdm/schedule.json").is_file())
            self.assertTrue((root / "flow/runtime/qor_report.json").is_file())
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["summary"]["used_fpgas"], 2)
        self.assertEqual(report["summary"]["equivalence_mismatches"], 0)


if __name__ == "__main__":
    unittest.main()
