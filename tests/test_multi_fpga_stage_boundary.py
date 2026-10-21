"""Requested intermediate stages must never execute downstream producers."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emuflow.cli import _build_parser, _dispatch
from emuflow.errors import EmuFlowError
from emuflow.io import read_json, write_json
from emuflow.multi_fpga_flow import run_multi_fpga_flow
from emuflow.timing_routing import NATIVE_TIMING_EVALUATED_PROVIDER
from tests.native_build import tlr_router
from tests.test_multi_fpga_flow import ROOT, STATIC_EXACT_PLATFORM, FAKE_OPENSTA


class MultiFpgaStageBoundaryTest(unittest.TestCase):
    def run_boundary(self, root, boundary, require_complete=False, frame_slots=32, route_constraints=None):
        return run_multi_fpga_flow(
            platform_path=STATIC_EXACT_PLATFORM, output_dir=root,
            yosys_json=ROOT / "examples/yosys/static_exact_chain.json",
            top="static_exact_chain", clocks=["clk"],
            partition_provider="greedy", balance_tolerance=1.0,
            timing_driven=False, clock_periods={"clk": 10.0},
            opensta=str(FAKE_OPENSTA),
            router=str(tlr_router()) if boundary == "phase5" else None,
            route_provider=NATIVE_TIMING_EVALUATED_PROVIDER,
            frame_slots=frame_slots, route_constraints=route_constraints, cut_mode="static-exact-combinational",
            max_cross_fpga_dependency_depth=2,
            static_exact_candidate_policy="assignment-derived-acyclic-v2",
            mapping_profile="vtr-hard-blocks", stop_after=boundary,
            sta_require_complete=require_complete,
        )

    def test_explicit_frame_reaches_partition_model_and_actual_native_header(self):
        from emuflow.phase3 import run_phase3
        from emuflow.routing import normalize_route_constraints
        from emuflow.partition_pressure import build_partition_pressure_model, _write_patron_native_input
        from tests.test_partition_pressure import PartitionPressureTest
        fixture = PartitionPressureTest()
        fixture.setUp()
        from emuflow.partition import build_clusters, build_partition_assignment
        fixture.clusters = build_clusters(fixture.ir, fixture.constraints,
            cut_mode="static-exact-combinational", max_cross_fpga_dependency_depth=2,
            static_exact_candidate_policy="assignment-derived-acyclic-v2")
        fixture.initial = build_partition_assignment(fixture.ir, fixture.platform, fixture.clusters,
            fixture.constraints, {cluster["id"]: ("a" if cluster["instances"][0] in {"u0", "u2"} else "b")
                                  for cluster in fixture.clusters["clusters"]}, provider="fixture", seed=7)
        for configured, explicit, expected in [(None, 1024, 1024), (32, 1024, 1024), (128, None, 128)]:
            with self.subTest(configured=configured, explicit=explicit), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                constraints = None
                original = {"schema": "emuflow.system-route-constraints/v1", "frame_slots": configured,
                            "lambda_history": 2.0, "max_iterations": 7}
                if configured is not None:
                    constraints = root / "constraints.json"
                    write_json(constraints, original)
                with patch("emuflow.multi_fpga_flow.run_phase3", wraps=run_phase3) as phase3:
                    self.run_boundary(root / "run", "phase3", frame_slots=explicit, route_constraints=constraints)
                consumed = read_json(phase3.call_args.kwargs["route_constraints_path"])
                self.assertEqual(consumed["frame_slots"], expected)
                if constraints is not None:
                    self.assertEqual(read_json(constraints), original)
                    self.assertEqual(consumed["lambda_history"], 2.0)
                    self.assertEqual(consumed["max_iterations"], 7)
                routes = normalize_route_constraints(consumed, fixture.platform)
                model = build_partition_pressure_model(fixture.ir, fixture.platform, fixture.clusters,
                    fixture.constraints, fixture.timing, routes)
                native = root / "patron.in"
                _write_patron_native_input(native, fixture.platform, fixture.clusters, fixture.constraints,
                    routes, model, fixture.initial, 0, 14)
                fields = native.read_text().splitlines()[1].split()
                self.assertEqual(fields[0], "PARAM")
                self.assertEqual(int(fields[8]), expected)

    def test_phase3_boundary_includes_original_path_projection_without_routing(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch("emuflow.multi_fpga_flow.run_phase4", side_effect=AssertionError("unexpected Phase4")), \
                patch("emuflow.multi_fpga_flow.run_phase5", side_effect=AssertionError("unexpected Phase5")), \
                patch("emuflow.multi_fpga_flow.run_phase6", side_effect=AssertionError("unexpected Phase6")):
            root = Path(temp) / "run"
            report = self.run_boundary(root, "phase3")
            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["validation"], {"status": "pass", "scope": "phase3"})
            self.assertIn("cut_path_projection", report["timing"])
            for name in ("assignment", "timing_path_database", "timing_paths"):
                self.assertTrue((root / report["artifacts"][name]["path"]).is_file())
            self.assertIn("src/emuflow/multi_fpga_flow.py", report["producer_source_sha256"])
            self.assertTrue(report["producer_python"]["executable_sha256"])
            self.assertFalse((root / "multi-fpga-flow-report.json").exists())
            self.assertEqual(read_json(root / "multi-fpga-stage-report.json"), report)

    def test_phase5_boundary_runs_real_validation_without_split_or_runtime(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch("emuflow.multi_fpga_flow.run_phase6", side_effect=AssertionError("unexpected Phase6")), \
                patch("emuflow.multi_fpga_flow.run_phase7c", side_effect=AssertionError("unexpected Phase7C")), \
                patch("emuflow.multi_fpga_flow.run_multi_fpga_physical_flow", side_effect=AssertionError("unexpected physical")):
            root = Path(temp) / "run"
            report = self.run_boundary(root, "phase5")
            self.assertEqual(report["completed_through"], "phase5")
            self.assertEqual(report["stages"]["tdm"]["status"], "pass")
            self.assertTrue((root / report["artifacts"]["schedule"]["path"]).is_file())
            self.assertNotIn("split", report["stages"])
            self.assertNotIn("runtime", report)

    def test_capped_or_unknown_sta_fails_before_partition(self):
        for sta_report in ({"status": "pass", "path_limit_reached": True}, {"status": "pass"}):
            with self.subTest(sta_report=sta_report), tempfile.TemporaryDirectory() as temp, \
                    patch("emuflow.multi_fpga_flow.run_opensta_path_database", return_value=sta_report), \
                    patch("emuflow.multi_fpga_flow.run_phase3", side_effect=AssertionError("unexpected partition")):
                with self.assertRaisesRegex(EmuFlowError, "complete STA population"):
                    self.run_boundary(Path(temp) / "run", "phase3", require_complete=True)

    def test_invalid_or_incompatible_boundary_fails_before_creating_output(self):
        for kwargs in ({"stop_after": "phase2"}, {"stop_after": "phase3", "cross_stage_iterations": 1},
                       {"stop_after": "phase5", "physical": True}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "run"
                with self.assertRaises(EmuFlowError):
                    run_multi_fpga_flow(output_dir=root, **kwargs)
                self.assertFalse(root.exists())

    def test_cli_forwards_boundary_and_accepts_only_matching_partial_success(self):
        args = _build_parser().parse_args([
            "multi-fpga", "compile", "--yosys-json", "design.json", "--out", "unused",
            "--stop-after", "phase5",
        ])
        report = {"schema": "emuflow.multi-fpga-stage-run/v1", "status": "partial",
                  "completed_through": "phase5", "validation": {"status": "pass"}}
        with patch("emuflow.cli.run_multi_fpga_flow", return_value=report) as run, \
                patch("emuflow.cli._print_json"):
            self.assertEqual(_dispatch(args), 0)
            self.assertEqual(run.call_args.kwargs["stop_after"], "phase5")
            report["completed_through"] = "phase3"
            self.assertEqual(_dispatch(args), 2)
            args.archive_out = Path("archive")
            with self.assertRaisesRegex(EmuFlowError, "archive"):
                _dispatch(args)


if __name__ == "__main__":
    unittest.main()
