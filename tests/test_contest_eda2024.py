import tempfile
import unittest
from pathlib import Path

from emuflow_tools.contest_eda2024 import (
    evaluate_eda2024_solution,
    import_eda2024_case,
    materialize_eda2024_rtl_boarddb,
)
from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json


INFO = """\
F1 10 8 8 0 0 0 0 0 0
F2 10 8 8 0 0 0 0 0 0
F3 10 8 8 0 0 0 0 0 0
"""

AREA = """\
a 1 1 0 0 0 0 0 0
b 1 1 0 0 0 0 0 0
c 1 1 0 0 0 0 0 0
d 1 1 0 0 0 0 0 0
"""

NETS = """\
a 3 b c
d 2 a
"""

TOPOLOGY = """\
2
F1 F2
F2 F3
"""

PRIMARY_SOLUTION = """\
F1: a d
F2:
F3: b c
"""

REPLICATED_SOLUTION = """\
F1: a d
F2:
F3: b c a*
"""


class Eda2024ContestCheckerTest(unittest.TestCase):
    def _write_case(self, root: Path, solution: str = PRIMARY_SOLUTION):
        values = {
            "design.info": INFO,
            "design.are": AREA,
            "design.net": NETS,
            "design.topo": TOPOLOGY,
            "design.fpga.out": solution,
        }
        for name, value in values.items():
            (root / name).write_text(value, encoding="utf-8")
        return {
            "info_path": root / "design.info",
            "area_path": root / "design.are",
            "net_path": root / "design.net",
            "topology_path": root / "design.topo",
            "solution_path": root / "design.fpga.out",
        }

    def test_checker_recomputes_weighted_hop_and_communication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._write_case(root)
            report = evaluate_eda2024_solution(
                **paths, runtime_seconds=18.0, output_path=root / "report.json"
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["metrics"]["total_hop_distance"], 6)
            self.assertEqual(report["metrics"]["cut_hyperedges"], 1)
            self.assertEqual(report["metrics"]["remote_fpga_sinks"], 1)
            self.assertEqual(report["metrics"]["replica_copies"], 0)
            self.assertAlmostEqual(
                report["metrics"]["contest_score"],
                6 * (1.0 + 0.2 * 18.0 / 3600.0),
            )
            usage = {
                record["fpga"]: record["used"]
                for record in report["communication"]
            }
            self.assertEqual(usage, {"F1": 3, "F2": 0, "F3": 3})
            self.assertTrue((root / "report.json").is_file())

    def test_import_parses_all_problem_inputs_without_solution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_case(root)
            (root / "design.fpga.out").unlink()
            report = import_eda2024_case(root, root / "normalized", "fixture")
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["fpgas"], 3)
            self.assertEqual(report["nodes"], 4)
            self.assertEqual(report["nets"], 2)
            instance = read_json(root / "normalized" / "contest_instance.json")
            self.assertTrue(instance["solution_required_for_evaluation"])
            self.assertEqual(instance["topology"]["diameter"], 2)
            self.assertEqual(set(instance["source_sha256"]), {
                "design.are", "design.info", "design.net", "design.topo"
            })

    def test_import_rejects_net_node_absent_from_area(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_case(root)
            (root / "design.net").write_text("a 1 missing\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "unknown nodes"):
                import_eda2024_case(root, root / "normalized", "fixture")

    def test_source_replica_removes_outputs_but_receives_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._write_case(root, REPLICATED_SOLUTION)
            report = evaluate_eda2024_solution(**paths)
            # a* makes a->(b,c) local on F3, while d->a must now also feed F3.
            self.assertEqual(report["metrics"]["total_hop_distance"], 4)
            self.assertEqual(report["metrics"]["replicated_modules"], 1)
            self.assertEqual(report["metrics"]["replica_copies"], 1)
            resources = {
                record["fpga"]: record["used"] for record in report["resources"]
            }
            self.assertEqual(resources["F3"]["FF"], 3)
            usage = {
                record["fpga"]: record["used"]
                for record in report["communication"]
            }
            self.assertEqual(usage, {"F1": 2, "F2": 0, "F3": 2})

    def test_rtl_projection_preserves_unweighted_topology_and_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_case(root)
            output = root / "rtl-boarddb.json"
            repository = Path(__file__).resolve().parents[1]
            report = materialize_eda2024_rtl_boarddb(
                case_dir=root,
                device_template_path=(
                    repository / "platforms/virtual/academic_vtr_4fpga_mesh.json"
                ),
                output_path=output,
                name="eda2024_fixture_academic_rtl",
                lanes_per_edge=4,
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["fpgas"], 3)
            self.assertEqual(report["links"], 2)
            self.assertEqual(report["data_lanes"], 8)
            self.assertEqual(report["maximum_legal_hop_distance"], 2)
            constraints = read_json(Path(report["route_constraints"]))
            self.assertEqual(constraints["max_route_hops"], 2)

            boarddb = read_json(output)
            self.assertEqual(
                [fpga["id"] for fpga in boarddb["fpgas"]],
                ["F1", "F2", "F3"],
            )
            self.assertEqual(
                [link["endpoints"] for link in boarddb["links"]],
                [["F1", "F2"], ["F2", "F3"]],
            )
            self.assertTrue(
                all(
                    link["capacity_sharing"] == "shared_bidirectional"
                    and link["data_lanes_per_direction"] == 4
                    for link in boarddb["links"]
                )
            )
            provenance = boarddb["platform"]["provenance"]["interconnect"]
            self.assertEqual(
                provenance["capacity_semantics"], "not-specified-by-contest"
            )
            self.assertEqual(provenance["configured_lanes_per_edge"], 4)
            self.assertEqual(
                provenance["external_communication_limits"],
                {"F1": 10, "F2": 10, "F3": 10},
            )

    def test_rtl_projection_rejects_invalid_lane_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_case(root)
            with self.assertRaisesRegex(ValidationError, "lanes_per_edge"):
                materialize_eda2024_rtl_boarddb(
                    case_dir=root,
                    device_template_path=(
                        Path(__file__).resolve().parents[1]
                        / "platforms/virtual/academic_vtr_4fpga_mesh.json"
                    ),
                    output_path=root / "bad.json",
                    name="bad",
                    lanes_per_edge=0,
                )

    def test_checker_rejects_maximum_hop_violation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._write_case(root)
            (root / "design.topo").write_text(
                "1\nF1 F2\nF2 F3\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "above maximum"):
                evaluate_eda2024_solution(**paths)

    def test_checker_rejects_replica_resource_overflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._write_case(root, REPLICATED_SOLUTION)
            (root / "design.info").write_text(
                INFO.replace("F3 10 8 8", "F3 10 2 8"), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "resource overflow"):
                evaluate_eda2024_solution(**paths)

    def test_checker_rejects_external_communication_overflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._write_case(root)
            (root / "design.info").write_text(
                INFO.replace("F1 10", "F1 2"), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ValidationError, "external communication overflow"
            ):
                evaluate_eda2024_solution(**paths)

    def test_checker_rejects_solution_without_exact_area_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._write_case(
                root,
                "F1: a d ghost\nF2:\nF3: b c\n",
            )
            with self.assertRaisesRegex(ValidationError, "absent from design.are"):
                evaluate_eda2024_solution(**paths)


if __name__ == "__main__":
    unittest.main()
