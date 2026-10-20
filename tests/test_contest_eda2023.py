import json
import tempfile
import unittest
from pathlib import Path

from emuflow.contest_eda2023 import (
    _timing_weight_for_fpga_diameter,
    evaluate_eda2023_solution,
    import_eda2023_case,
    materialize_eda2023_rtl_boarddb,
    optimize_eda2023_tdm,
)
from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.phase4 import run_phase4
from emuflow.timing_routing import GLOBAL_CANDIDATE_PROVIDER
from tests.native_build import (
    tdm_ratio_optimizer,
    tdm_timing_dag_optimizer,
    tlr_router,
)


FPGA_DIE = """\
FPGA0:Die0 Die1
FPGA1:Die2 Die3
"""

DIE_POSITION = """\
Die0:g0
Die1:g1 g5
Die2:g2 g4
Die3:g3
"""

DIE_NETWORK = """\
0 2 0 0
2 0 2 0
0 2 0 2
0 0 2 0
"""

NETS = """\
g0 s 1
g2 l
g1 s 1
g3 l
g3 s 1
g0 l
"""


class Eda2023ContestAdapterTest(unittest.TestCase):
    def test_timing_weight_scales_with_physical_fpga_diameter(self):
        self.assertEqual(_timing_weight_for_fpga_diameter(1), 0.0)
        self.assertEqual(_timing_weight_for_fpga_diameter(2), 0.5)
        self.assertEqual(_timing_weight_for_fpga_diameter(3), 4.0)
        self.assertEqual(_timing_weight_for_fpga_diameter(5), 4.0)

    def _import(self, root: Path):
        case = root / "case"
        case.mkdir()
        for name, value in (
            ("design.fpga.die", FPGA_DIE),
            ("design.die.position", DIE_POSITION),
            ("design.die.network", DIE_NETWORK),
            ("design.net", NETS),
        ):
            (case / name).write_text(value, encoding="utf-8")
        normalized = root / "normalized"
        report = import_eda2023_case(case, normalized, "eda2023_sample")
        return report, normalized

    def test_die_route_and_direction_separated_tdm_run_end_to_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            imported, normalized = self._import(root)
            self.assertEqual(imported["physical_fpgas"], 2)
            self.assertEqual(imported["physical_fpga_diameter"], 1)
            self.assertEqual(imported["dies"], 4)
            self.assertEqual(imported["routed_nets"], 3)
            constraints = read_json(normalized / "route_constraints.json")
            instance = read_json(normalized / "contest_instance.json")
            self.assertEqual(
                instance["parameters"]["physical_fpga_diameter"], 1
            )
            self.assertEqual(constraints["tdm_min_ratio"], 4)
            self.assertEqual(constraints["lambda_load"], 68.0)
            self.assertEqual(constraints["lambda_timing"], 0.0)
            self.assertEqual(constraints["lambda_tdm"], 1.0)
            self.assertTrue(constraints["hard_sll_capacity"])
            self.assertEqual(len(constraints["sll_links"]), 2)

            routed = root / "routed"
            route_report = run_phase4(
                assignment_path=normalized / "partition_assignment.json",
                platform_path=normalized / "boarddb.json",
                output_dir=routed,
                constraints_path=normalized / "route_constraints.json",
                timing_paths_path=normalized / "contest_timing_paths.json",
                provider=GLOBAL_CANDIDATE_PROVIDER,
                router=str(tlr_router()),
            )
            self.assertEqual(route_report["status"], "pass")

            optimized = optimize_eda2023_tdm(
                normalized / "contest_instance.json",
                routed / "routes.json",
                root / "solution",
                optimizer=str(tdm_ratio_optimizer()),
                post_refinement_iterations=20,
            )
            self.assertEqual(optimized["status"], "pass")
            self.assertEqual(optimized["max_routing_weight"], 6.5)
            self.assertEqual(optimized["native_metrics"]["iterations"], 100)
            self.assertIn(
                "global_minimax_improvements", optimized["native_metrics"]
            )
            self.assertIn(
                "global_minimax_weight_exponent", optimized["native_metrics"]
            )
            evaluation = evaluate_eda2023_solution(
                normalized / "contest_instance.json",
                routed / "routes.json",
                root / "solution" / "tdm_plan.json",
            )
            self.assertEqual(evaluation["metrics"]["max_tdm_ratio"], 4)
            self.assertEqual(evaluation["metrics"]["used_wires"], 2)
            self.assertTrue((root / "solution" / "design.route.out").is_file())
            self.assertTrue((root / "solution" / "design.tdm.out").is_file())

            timing_dag = optimize_eda2023_tdm(
                normalized / "contest_instance.json",
                routed / "routes.json",
                root / "timing-dag-solution",
                optimizer=str(tdm_ratio_optimizer()),
                timing_dag_optimizer=str(tdm_timing_dag_optimizer()),
                provider="aspdac26-timing-dag-lagrangian-v1",
                post_refinement_iterations=20,
            )
            self.assertEqual(timing_dag["status"], "pass")
            self.assertLessEqual(
                timing_dag["max_routing_weight"],
                optimized["max_routing_weight"],
            )
            timing_dag_plan = read_json(
                root / "timing-dag-solution" / "tdm_plan.json"
            )
            self.assertEqual(
                timing_dag_plan["algorithm"]["validation"]["status"],
                "pass",
            )

    def test_physical_fpga_projection_preserves_wire_banks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, normalized = self._import(root)
            output = root / "rtl-boarddb.json"
            repository = Path(__file__).resolve().parents[1]
            report = materialize_eda2023_rtl_boarddb(
                instance_path=normalized / "contest_instance.json",
                device_template_path=(
                    repository / "platforms/virtual/academic_vtr_4fpga_mesh.json"
                ),
                output_path=output,
                name="eda2023_sample_academic_rtl",
                lane_scale=2,
                route_constraints_path=root / "physical-route-constraints.json",
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["physical_fpgas"], 2)
            self.assertEqual(report["dies"], 4)
            self.assertEqual(report["wire_banks"], 1)
            self.assertEqual(report["collapsed_sll_links"], 2)
            self.assertEqual(report["data_lanes"], 4)
            boarddb = read_json(output)
            self.assertEqual(
                [fpga["id"] for fpga in boarddb["fpgas"]],
                ["FPGA0", "FPGA1"],
            )
            self.assertEqual(
                boarddb["links"][0]["endpoints"], ["FPGA0", "FPGA1"]
            )
            self.assertEqual(
                boarddb["links"][0]["data_lanes_per_direction"], 4
            )
            self.assertEqual(
                boarddb["links"][0]["capacity_sharing"],
                "shared_bidirectional",
            )
            provenance = boarddb["platform"]["provenance"]["interconnect"]
            self.assertEqual(provenance["projection"], "physical-fpga-wire-bank")
            self.assertEqual(provenance["collapsed_sll_links"], 2)
            constraints = read_json(root / "physical-route-constraints.json")
            self.assertEqual(
                constraints["schema"], "emuflow.system-route-constraints/v1"
            )
            self.assertEqual(constraints["max_route_hops"], 1)
            self.assertEqual(constraints["tdm_ratio_quantum"], 4)
            self.assertEqual(
                constraints["shared_capacity_links"],
                ["eda2023_die_link_001_002"],
            )
            self.assertEqual(
                report["route_constraints"],
                str(root / "physical-route-constraints.json"),
            )

    def test_physical_fpga_projection_rejects_invalid_lane_scale(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, normalized = self._import(root)
            with self.assertRaisesRegex(ValidationError, "lane_scale"):
                materialize_eda2023_rtl_boarddb(
                    instance_path=normalized / "contest_instance.json",
                    device_template_path=(
                        Path(__file__).resolve().parents[1]
                        / "platforms/virtual/academic_vtr_4fpga_mesh.json"
                    ),
                    output_path=root / "bad.json",
                    name="bad",
                    lane_scale=0,
                )

    def test_checker_rejects_opposing_signals_on_one_wire(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, normalized = self._import(root)
            routed = root / "routed"
            run_phase4(
                assignment_path=normalized / "partition_assignment.json",
                platform_path=normalized / "boarddb.json",
                output_dir=routed,
                constraints_path=normalized / "route_constraints.json",
                timing_paths_path=normalized / "contest_timing_paths.json",
                provider=GLOBAL_CANDIDATE_PROVIDER,
                router=str(tlr_router()),
            )
            solution = root / "solution"
            optimize_eda2023_tdm(
                normalized / "contest_instance.json",
                routed / "routes.json",
                solution,
                optimizer=str(tdm_ratio_optimizer()),
                post_refinement_iterations=0,
            )
            plan = read_json(solution / "tdm_plan.json")
            for hop in plan["hops"]:
                hop["lane"] = 0
            bad = root / "bad.json"
            bad.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "mixed direction"):
                evaluate_eda2023_solution(
                    normalized / "contest_instance.json",
                    routed / "routes.json",
                    bad,
                )

    def test_import_rejects_asymmetric_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case = root / "case"
            case.mkdir()
            for name, value in (
                ("design.fpga.die", FPGA_DIE),
                ("design.die.position", DIE_POSITION),
                ("design.die.network", DIE_NETWORK.replace("2 0 2 0", "1 0 2 0", 1)),
                ("design.net", NETS),
            ):
                (case / name).write_text(value, encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "symmetric"):
                import_eda2023_case(case, root / "out", "bad")

    def test_import_rejects_duplicate_sink_endpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case = root / "case"
            case.mkdir()
            duplicate_nets = NETS.replace("g2 l\n", "g2 l\ng2 l\n", 1)
            for name, value in (
                ("design.fpga.die", FPGA_DIE),
                ("design.die.position", DIE_POSITION),
                ("design.die.network", DIE_NETWORK),
                ("design.net", duplicate_nets),
            ):
                (case / name).write_text(value, encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "duplicate net endpoint"):
                import_eda2023_case(case, root / "out", "bad")


if __name__ == "__main__":
    unittest.main()
