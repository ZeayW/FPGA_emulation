"""Author pair selection must reach every ordinary P4/P5 orchestration path."""

from pathlib import Path
import contextlib
import gzip
import io
import json
import tempfile
import unittest

from emuflow.author_routing import AUTHOR_ROUTER_PROVIDER
from emuflow.cli import _build_parser
from emuflow.cross_stage import _run_candidate_flow
from emuflow.frame_search import run_frame_length_search
from emuflow.io import read_json, write_json
from emuflow.multi_fpga_flow import run_multi_fpga_flow, validate_multi_fpga_flow_bundle
from emuflow.phase3 import run_phase3
from emuflow.tdm import TDM_BASELINE_PROVIDER
from emuflow.tdm_ratio import TDM_TIMING_DAG_RATIO_PROVIDER
from emuflow.yosys import import_yosys_json
from tests.native_build import tdm_partition_feedback, tdm_ratio_optimizer, tdm_slot_optimizer, tdm_timing_dag_optimizer
from tests.test_author_routing import author_router
from tests.test_phase4 import _link, _platform_value


ROOT = Path(__file__).resolve().parents[1]


class AuthorFlowModesTest(unittest.TestCase):
    def test_explicit_cli_modes_leave_defaults_unchanged(self):
        parser = _build_parser()
        args = ["multi-fpga", "compile", "--out", "out"]
        self.assertIsNone(parser.parse_args(args).author_cost_policy)
        selected = parser.parse_args(args + ["--route-provider", AUTHOR_ROUTER_PROVIDER,
                                            "--author-cost-policy", "best-of-two-v1"])
        self.assertEqual(selected.author_cost_policy, "best-of-two-v1")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["phase4", "--assignment", "a", "--platform", "b", "--out", "c",
                               "--author-cost-policy", "best-of-two-v1"])

    def _inputs(self, root):
        ir = import_yosys_json(ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"])
        ir_path, platform, database = root / "ir.json", root / "platform.json", root / "database.json"
        write_json(ir_path, ir.value)
        write_json(platform, _platform_value("author-modes", ["a", "b"], [_link("ab", "a", "b", lanes=8)]))
        run_phase3(ir_path, platform, root / "partition", provider="greedy",
                   min_used_fpgas=2, balance_tolerance=1.0)
        assignment = root / "partition/assignment.json"
        cuts = read_json(assignment)["cut_nets"]
        self.assertTrue(cuts)
        timing = root / "timing.json"
        write_json(timing, {"schema": "emuflow.sta-paths/v1", "design": "counter", "paths": [{
            "id": "all-cuts", "clock_domain": "clk", "clock_period_ns": 20.0,
            "slack_ns": 10.0, "fixed_delay_ns": 0.0, "cut_nets": [cut["net"] for cut in cuts],
        }]})
        write_json(database, {
            "schema": "emuflow.sta-path-database/v1", "design": "counter",
            "source": {"provider": "mapped-fixture", "input": "counter"},
            "normalization": {"positive_slack_scale_ns": 10.0, "negative_slack_scale_ns": 1.0,
                              "max_clock_period_ns": 20.0},
            "paths": [{"id": "all-nets", "clock_domain": "clk", "clock_period_ns": 20.0,
                       "slack_ns": 10.0, "fixed_delay_ns": 0.0, "normalized_slack": 1.0,
                       "path_nets": [net["id"] for net in ir.value["nets"]]}],
        })
        return assignment, platform, timing, database

    def test_complete_compile_preserves_selected_pair_into_phase6(self):
        self._complete_compile()

    def test_complete_frame_search_preserves_selected_pair_into_phase6(self):
        self._complete_compile(optimize_frames=True)

    def test_complete_cross_stage_preserves_selected_pair_into_phase6(self):
        self._complete_compile(cross_stage=True)

    def _complete_compile(self, optimize_frames=False, cross_stage=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            timing = root / "timing.json.gz"
            timing_value = {"schema": "emuflow.sta-paths/v1", "design": "counter", "paths": [{
                "id": "cut", "clock_domain": "clk", "clock_period_ns": 20.0,
                "slack_ns": 1.0, "fixed_delay_ns": 0.0, "cut_nets": ["q[0]", "q[2]"],
            }]}
            with gzip.open(timing, "wt", encoding="utf-8") as stream:
                json.dump(timing_value, stream)
            out = root / "flow"
            result = run_multi_fpga_flow(
                platform_path=ROOT / "platforms/virtual/academic_vtr_2fpga_p2p.json", output_dir=out,
                yosys_json=ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"],
                partition_provider="greedy", timing_driven=optimize_frames or cross_stage,
                timing_paths=None if optimize_frames or cross_stage else timing,
                clock_periods={"clk": 20.0} if optimize_frames or cross_stage else None,
                opensta=str(ROOT / "tests/fixtures/fake_opensta_paths.py") if optimize_frames or cross_stage else None,
                route_provider=AUTHOR_ROUTER_PROVIDER, author_cost_policy="best-of-two-v1",
                router=author_router(), frame_slots=32, optimize_frame_slots=optimize_frames,
                tdm_provider=TDM_TIMING_DAG_RATIO_PROVIDER if cross_stage else TDM_BASELINE_PROVIDER,
                ratio_optimizer=str(tdm_ratio_optimizer()) if cross_stage else None,
                timing_dag_optimizer=str(tdm_timing_dag_optimizer()) if cross_stage else None,
                slot_optimizer=str(tdm_slot_optimizer()) if cross_stage else None,
                cross_stage_iterations=1 if cross_stage else 0,
                cross_stage_feedback_optimizer=str(tdm_partition_feedback()) if cross_stage else None,
                phase6_provider="baseline", equivalence_cycles=2, mapping_profile="vtr-hard-blocks",
            )
            self.assertEqual(result["author_cost_policy"], "best-of-two-v1")
            self.assertEqual(result["stages"]["system_route"]["provider"], AUTHOR_ROUTER_PROVIDER)
            if not optimize_frames and not cross_stage:
                self.assertEqual(read_json(out / "author-route-tdm/selection.json")["status"], "pass")
            elif optimize_frames:
                self.assertEqual(result["frame_search"]["configuration"]["author_cost_policy"], "best-of-two-v1")
            else:
                self.assertEqual(result["cross_stage"]["configuration"]["author_cost_policy"], "best-of-two-v1")
            receipt = result["artifacts"]["author_route_tdm_selection"]["path"]
            self.assertEqual(read_json(out / receipt)["status"], "pass")
            checked = validate_multi_fpga_flow_bundle(out)
            self.assertEqual(checked["status"], "pass")
            self.assertGreater(checked["cut_nets"], 0)
            self.assertEqual(checked["equivalence_mismatches"], 0)

    def test_frame_search_runs_both_author_policies_at_selected_frame(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assignment, platform, timing, _ = self._inputs(root)
            report = run_frame_length_search(
                assignment, platform, timing, root / "search", root / "routes", root / "tdm",
                max_frame_slots=16, route_provider=AUTHOR_ROUTER_PROVIDER,
                author_cost_policy="best-of-two-v1", router=author_router(),
                tdm_provider=TDM_BASELINE_PROVIDER, simulation_frames=2,
            )
            self.assertEqual(report["configuration"]["author_cost_policy"], "best-of-two-v1")
            try:
                from jsonschema import Draft202012Validator
            except ImportError:
                pass
            else:
                schema = read_json(ROOT / "schemas/frame-search-v4.schema.json")
                Draft202012Validator(schema).validate(report)
            selected = root / "search" / report["selected_candidate"]
            pair = read_json(selected / "author-route-tdm/selection.json")
            self.assertEqual([c["status"] for c in pair["candidates"]], ["pass", "pass"])
            self.assertEqual(read_json(root / "routes/routes.json")["provider"], AUTHOR_ROUTER_PROVIDER)

    def test_cross_stage_candidate_uses_same_paired_selector(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assignment, platform, _, database = self._inputs(root)
            result = _run_candidate_flow(
                root=root, iteration=0, assignment_path=assignment,
                database_path=database, platform_path=platform, route_constraints_path=None,
                frame_slots=16, optimize_frame_slots=False, route_max_iterations=None,
                router=author_router(), route_provider=AUTHOR_ROUTER_PROVIDER,
                author_cost_policy="best-of-two-v1", route_candidate_workers=1,
                simulation_frames=2, tdm_provider=TDM_TIMING_DAG_RATIO_PROVIDER,
                ratio_optimizer=str(tdm_ratio_optimizer()),
                timing_dag_optimizer=str(tdm_timing_dag_optimizer()), slot_optimizer=None,
                ratio_max_iterations=100, max_ratio=8, ratio_quantum=None,
                post_refinement_iterations=2, slot_refinement_iterations=0, ratio_convergence=1e-9,
            )
            self.assertEqual(result["status"], "pass")
            pair = read_json(root / "iteration_000/author-route-tdm/selection.json")
            self.assertEqual(pair["status"], "pass")
            self.assertEqual(len(pair["candidates"]), 2)


if __name__ == "__main__":
    unittest.main()
