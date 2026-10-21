"""Reject invalid scientific contracts before launching the expensive pair."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("nvdla_compare", ROOT / "scripts/phase4_nvdla_compare.py")
compare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compare)


def prefix():
    return {
        "schema": "emuflow.multi-fpga-stage-run/v1", "completed_through": "phase3",
        "validation": {"status": "pass"},
        "configuration": {"clock_periods": compare.CLOCKS, "frame_slots": 1024,
                          "partition_provider": "patron", "patron_algorithm_version": 14,
                          "cut_mode": "static-exact-combinational"},
        "timing": {"sta": {"status": "pass", "path_limit_reached": False, "paths": 1500},
                   "cut_path_projection": {"status": "pass", "uncovered_cut_nets": 0,
                                           "database_paths": 1500, "projected_paths": 700,
                                           "cut_nets": 900}}}


class ContractTest(unittest.TestCase):
    def test_valid_prefix_is_explicitly_cross_partition(self):
        self.assertEqual(compare.check_prefix(prefix())["scope"], "projected-inter-fpga-paths")

    def test_truncation_missing_coverage_and_algorithm_changes_rejected(self):
        for group, field, value in [
            ("sta", "path_limit_reached", True), ("sta", "path_limit_reached", None),
            ("cut_path_projection", "uncovered_cut_nets", 1),
            ("cut_path_projection", "database_paths", 1499),
            ("cut_path_projection", "projected_paths", 0),
        ]:
            report = prefix()
            report["timing"][group][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                compare.check_prefix(report)
        for field, value in [("frame_slots", 2048), ("patron_algorithm_version", 6),
                             ("cut_mode", "sequential-only"), ("clock_periods", {"dla_core_clk": 10})]:
            report = prefix()
            report["configuration"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                compare.check_prefix(report)

    def test_platform_rejects_doubled_direction_and_capacity_change(self):
        board = compare.read(ROOT / "platforms/virtual/rapidwright_xcvu19p_4fpga_mesh.json")
        compare.check_platform(board)
        for mutate in [lambda b: b["links"][0].update(data_lanes_per_direction=720),
                       lambda b: b["links"][0].update(capacity_sharing="independent"),
                       lambda b: b["fpgas"][0].update(utilization_limit=0.5),
                       lambda b: b["fpgas"][0]["capacity"].update(lut=9999999)]:
            altered = copy.deepcopy(board)
            mutate(altered)
            with self.assertRaises(ValueError):
                compare.check_platform(altered)

    def test_changed_source_runtime_and_input_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.v"
            path.write_text("original")
            provenance = {"schema": "emuflow.nvdla-prefix-provenance/v1",
                          "source_sha256": {"phase5.py": "old"}, "runtime": {"python": "old"},
                          "files": {str(path): compare.digest(path)}, "benchmark": str(path)}
            with patch.object(compare, "code_identity", return_value={"phase5.py": "new"}):
                with self.assertRaisesRegex(ValueError, "code changed"):
                    compare.verify_provenance(provenance)
            with patch.object(compare, "code_identity", return_value=provenance["source_sha256"]), \
                 patch.object(compare, "runtime_identity", return_value={"python": "new"}):
                with self.assertRaisesRegex(ValueError, "runtime changed"):
                    compare.verify_provenance(provenance)
            path.write_text("modified")
            with patch.object(compare, "code_identity", return_value=provenance["source_sha256"]), \
                 patch.object(compare, "runtime_identity", return_value=provenance["runtime"]):
                with self.assertRaisesRegex(ValueError, "input changed"):
                    compare.verify_provenance(provenance)

    def test_stat_guard_detects_same_size_input_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "assignment"
            path.write_text("aaaa")
            before = compare.stat_identity([str(path)])
            replacement = Path(directory) / "replacement"
            replacement.write_text("bbbb")
            replacement.replace(path)
            self.assertNotEqual(before, compare.stat_identity([str(path)]))

    def test_seal_consumes_real_stage_artifact_shape_and_keeps_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common = root / "common"
            (common / "partition").mkdir(parents=True)
            board = root / "board.json"
            board.write_text(json.dumps({"platform": {"virtual_board_wiring": "wires.csv"}}))
            (root / "wires.csv").write_text("wire data")
            report = prefix()
            report["artifacts"] = {
                "platform": {"path": str(board)}, "ir": {"path": "design.sqlite"},
                "assignment": {"path": "partition/assignment.json"},
                "timing_path_database": {"path": "paths.json"},
                "timing_paths": {"path": "projected.json"}}
            for item in report["artifacts"].values():
                if item["path"] != str(board):
                    (common / item["path"]).write_text("{}")
            (common / "paths.sqlite").write_text("immutable timing payload")
            (common / "paths.json").write_text(json.dumps({"payloads": {"paths": {"path": "paths.sqlite"}}}))
            (common / "partition/assignment.sqlite").write_text("assignment mapping")
            (common / "partition/clusters.json").write_text("{}")
            (common / "unrelated-large-scratch").write_text("not a downstream input")
            (common / "partition/phase3_report.json").write_text(json.dumps({
                "validation": {"combinational_cut_nets": 5}}))
            (common / "multi-fpga-stage-report.json").write_text(json.dumps(report))
            provenance = root / "provenance.json"
            provenance.write_text("{}")
            with patch.object(compare, "verify_provenance"), patch.object(compare, "check_platform"), \
                 patch.object(compare, "check_producer"):
                sealed = compare.seal_contract(common, board, provenance)
                self.assertIn(str((common / "paths.sqlite").resolve()), sealed["files"])
                self.assertNotIn(str((common / "unrelated-large-scratch").resolve()), sealed["files"])
                self.assertEqual(sealed["coverage"]["combinational_cut_nets"], 5)
                compare.verify_contract(sealed, common, board)
                (common / "paths.sqlite").write_text("tampered timing payload")
                with self.assertRaisesRegex(ValueError, "common input changed"):
                    compare.verify_contract(sealed, common, board)
                (common / "partition/phase3_report.json").write_text(json.dumps({
                    "validation": {"combinational_cut_nets": 0}}))
                with self.assertRaisesRegex(ValueError, "naturally selected"):
                    compare.seal_contract(common, board, provenance)

    def test_stale_completed_producer_cannot_be_sealed_with_fresh_provenance(self):
        provenance = {"source_sha256": {"src/emuflow/phase3.py": "current", "script.py": "current"},
                      "runtime": {"python_version": "version", "python_sha256": "binary"}}
        report = {"producer_source_sha256": {"src/emuflow/phase3.py": "old"},
                  "producer_python": {"version": "version", "executable_sha256": "binary"}}
        with self.assertRaisesRegex(ValueError, "producer differs"):
            compare.check_producer(report, provenance)
        report["producer_source_sha256"]["src/emuflow/phase3.py"] = "current"
        compare.check_producer(report, provenance)
        report["producer_python"]["executable_sha256"] = "old"
        with self.assertRaisesRegex(ValueError, "Python differs"):
            compare.check_producer(report, provenance)


class PerClockTimingTest(unittest.TestCase):
    def test_aggregate_retains_domain_despite_identical_periods(self):
        from emuflow.tdm import reconstruct_tdm_schedule_timing
        from emuflow.errors import ValidationError
        from types import SimpleNamespace
        records = [dict(path=f"p{i}", clock_domain=clock, clock_period_ns=10,
                        slack_ns=slack, normalized_slack=slack/10, delay_ns=10-slack)
                   for i, (clock, slack) in enumerate([
                       ("dla_core_clk", -2), ("dla_core_clk", 1), ("dla_csb_clk", -7)])]
        with patch("emuflow.tdm.reconstruct_tdm_schedule_timing_paths_from_routes", return_value=records):
            result = reconstruct_tdm_schedule_timing({}, SimpleNamespace(links=[]), {})
        self.assertEqual(result["per_clock"]["dla_core_clk"], {
            "timing_paths": 2, "worst_slack_ns": -2, "total_negative_slack_ns": -2,
            "negative_slack_paths": 1})
        self.assertEqual(result["per_clock"]["dla_csb_clk"]["total_negative_slack_ns"], -7)
        self.assertEqual(sum(m["timing_paths"] for m in result["per_clock"].values()), result["timing_paths"])
        records[0].pop("clock_domain")
        with patch("emuflow.tdm.reconstruct_tdm_schedule_timing_paths_from_routes", return_value=records):
            with self.assertRaisesRegex(ValidationError, "clock domain"):
                reconstruct_tdm_schedule_timing({}, SimpleNamespace(links=[]), {})


if __name__ == "__main__":
    unittest.main()
