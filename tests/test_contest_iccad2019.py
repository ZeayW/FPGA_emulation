import tempfile
import unittest
from pathlib import Path

from emuflow_tools.contest_iccad2019 import evaluate_iccad2019_solution, import_iccad2019_instance, materialize_iccad2019_rtl_boarddb
from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json
from emuflow.routing.phase4 import run_phase4
from tests.timing_routing_reference import GLOBAL_CANDIDATE_PROVIDER
from tests.native_build import tlr_router


SAMPLE_INPUT = """\
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

SAMPLE_OUTPUT = """\
1
0 2
1
4 2
1
9 2
3
1 2
8 2
9 4
2
9 4
10 2
"""


class Iccad2019ContestAdapterTest(unittest.TestCase):
    def _files(self, root: Path):
        source = root / "SampleInput"
        solution = root / "SampleOutput"
        source.write_text(SAMPLE_INPUT, encoding="utf-8")
        solution.write_text(SAMPLE_OUTPUT, encoding="utf-8")
        normalized = root / "normalized"
        report = import_iccad2019_instance(source, normalized, "sample")
        return report, normalized, solution

    def test_official_sample_imports_and_evaluates_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, normalized, solution = self._files(Path(temporary))
            self.assertEqual(report["fpgas"], 8)
            self.assertEqual(report["edges"], 11)
            self.assertEqual(report["nets"], 5)
            self.assertEqual(report["net_groups"], 3)
            evaluation = evaluate_iccad2019_solution(
                normalized / "contest_instance.json", solution
            )
            self.assertEqual(evaluation["status"], "pass")
            self.assertEqual(
                evaluation["metrics"]["maximum_total_tdm_ratio"], 8
            )
            self.assertEqual(
                evaluation["metrics"]["maximum_edge_harmonic_use"], 1.0
            )

    def test_normalized_board_uses_bidirectional_shared_domains(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, normalized, _ = self._files(Path(temporary))
            board = read_json(normalized / "boarddb.json")
            constraints = read_json(normalized / "route_constraints.json")
            self.assertEqual(board["platform"]["kind"], "virtual")
            self.assertEqual(len(board["links"]), 11)
            self.assertEqual(
                constraints["shared_capacity_links"],
                [link["id"] for link in board["links"]],
            )
            self.assertEqual(constraints["tdm_ratio_quantum"], 2)
            self.assertTrue(constraints["tree_edge_sum_tdm"])

    def test_public_graph_materializes_an_rtl_capable_boarddb(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, normalized, _ = self._files(root)
            output = root / "rtl-boarddb.json"
            repository = Path(__file__).resolve().parents[1]
            report = materialize_iccad2019_rtl_boarddb(
                instance_path=normalized / "contest_instance.json",
                device_template_path=(
                    repository / "platforms/virtual/academic_vtr_4fpga_mesh.json"
                ),
                output_path=output,
                name="iccad2019_sample_academic_rtl",
                lane_scale=3,
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["fpgas"], 8)
            self.assertEqual(report["links"], 11)
            self.assertEqual(report["data_lanes"], 33)
            boarddb = read_json(output)
            self.assertEqual(
                {fpga["capacity"]["lut"] for fpga in boarddb["fpgas"]},
                {400000},
            )
            self.assertEqual(
                {link["direction"] for link in boarddb["links"]},
                {"full_duplex"},
            )
            self.assertEqual(
                {link["capacity_sharing"] for link in boarddb["links"]},
                {"shared_bidirectional"},
            )
            self.assertEqual(
                boarddb["platform"]["provenance"]["interconnect"][
                    "capacity_semantics"
                ],
                "shared-bidirectional-tdm-projection",
            )

    def test_rtl_boarddb_materializer_rejects_invalid_lane_scale(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, normalized, _ = self._files(root)
            with self.assertRaisesRegex(ValidationError, "lane_scale"):
                materialize_iccad2019_rtl_boarddb(
                    instance_path=normalized / "contest_instance.json",
                    device_template_path=(
                        Path(__file__).resolve().parents[1]
                        / "platforms/virtual/academic_vtr_4fpga_mesh.json"
                    ),
                    output_path=root / "bad.json",
                    name="bad",
                    lane_scale=0,
                )


    def test_checker_rejects_combined_bidirectional_overload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, normalized, solution = self._files(root)
            text = solution.read_text(encoding="utf-8")
            # Edge 9 carries three nets in the official sample.  Changing both
            # ratio-4 records to ratio 2 makes the exact use 3/2.
            solution.write_text(text.replace("9 4", "9 2"), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "exceeds capacity"):
                evaluate_iccad2019_solution(
                    normalized / "contest_instance.json", solution
                )

    def test_checker_rejects_disconnected_multicast_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, normalized, solution = self._files(root)
            lines = solution.read_text(encoding="utf-8").splitlines()
            # Net 3 needs F0 -> {F4,F5,F6}; removing two tree edges leaves sinks.
            lines[6:10] = ["1", "1 2"]
            solution.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "misses sinks"):
                evaluate_iccad2019_solution(
                    normalized / "contest_instance.json", solution
                )


if __name__ == "__main__":
    unittest.main()
