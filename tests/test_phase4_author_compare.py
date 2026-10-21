"""A completed control must not survive changes to its producer or evaluator."""

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "phase4_author_compare", ROOT / "scripts/phase4_author_compare.py")
compare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compare)


class BaselineReuseTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "summary.json"
        artifact = self.root / "assignment.json"
        artifact.write_text('{"assignment": {}}')
        self.case = {
            "input_sha256": {"input": "raw-hash"},
            "import": {"artifacts": {"assignment": str(artifact)}},
            "normalized_artifact_sha256": {"assignment": compare.digest(artifact)},
            "arms": {},
        }
        evaluation = {"schema": "test/v1", "status": "pass", "instance": "case1",
                      "metrics": {"score": 17}}
        outputs = {}
        for name, value in (("routes", {}), ("tdm_plan", {}), ("evaluation", evaluation)):
            path = self.root / (name + ".json")
            path.write_text(json.dumps(value))
            outputs[name] = {"path": str(path), "sha256": compare.digest(path)}
        self.case["arms"]["A"] = {
            "status": "pass", "provider": "old-provider", "evaluation": evaluation,
            "stage_seconds": {"phase4": 2}, "output_artifact_sha256": outputs,
        }
        self.baseline = {
            "python_source_sha256": {"src/emuflow/phase4.py": "route-wrapper",
                                     "src/emuflow/phase5.py": "phase5",
                                     "src/emuflow/contest_eda2023.py": "evaluator"},
            "runner_sha256": "runner", "python": "runtime",
            "python_executable_sha256": "python-binary", "tdm_settings": {"frame": 1024},
            "tools": {name: {"identity": {"source": name}, "cache_key": name, "sha256": name}
                      for name in ("tlr_router", "tdm_ratio_optimizer", "synergistic_author_router")},
            "cases": {"case1": self.case},
        }
        self.summary = copy.deepcopy(self.baseline)
        self.current = copy.deepcopy(self.case)
        self.current["arms"] = {}
        self.path.write_text(json.dumps(self.baseline))

    def reuse(self):
        compare.reuse_baseline(self.path, self.baseline, "case1", self.current,
                               self.summary, "old-provider")

    def test_fresh_worker_publishes_reusable_output_seals(self):
        output = self.root / "worker"
        manifest_path = self.root / "manifest.json"
        manifest = {"output": str(output), "provider": "old-provider",
                    "router": "router", "optimizer": "optimizer", "tdm_settings": {},
                    "artifacts": {key: str(self.root / key) for key in
                                  ("assignment", "boarddb", "route_constraints", "timing_paths", "instance")}}
        manifest_path.write_text(json.dumps(manifest))

        def route(*args, **kwargs):
            compare.write(output / "phase4/routes.json", {"routes": []})
            return {"status": "pass"}

        def tdm(*args, **kwargs):
            path = output / "tdm/tdm_plan.json"
            compare.write(path, {"schedule": []})
            return {"status": "pass", "artifacts": {"tdm_plan": str(path)}}

        evaluation = self.case["arms"]["A"]["evaluation"]
        with patch("emuflow.phase4.run_phase4", side_effect=route), \
                patch("emuflow.contest_eda2023.optimize_eda2023_tdm", side_effect=tdm), \
                patch("emuflow.contest_eda2023.evaluate_eda2023_solution", return_value=evaluation), \
                patch("builtins.print"):
            self.assertEqual(compare.worker(manifest_path), 0)
        self.case["arms"]["A"] = json.loads((output / "result.json").read_text())
        self.reuse()
        self.assertTrue(self.current["arms"]["A"]["reused"])

    def test_identical_result_and_author_native_only_change_can_reuse(self):
        self.summary["tools"]["synergistic_author_router"]["sha256"] = "new-author"
        self.summary["source_commit"] = "documentation-and-author-edit"
        self.reuse()
        arm = self.current["arms"]["A"]
        self.assertTrue(arm["reused"])
        self.assertEqual(arm["evaluation"]["metrics"]["score"], 17)
        self.assertEqual(arm["output_artifact_sha256"], self.case["arms"]["A"]["output_artifact_sha256"])
        # A second reuse must still check the original sealed outputs.
        self.baseline["cases"]["case1"] = copy.deepcopy(self.current)
        self.current["arms"] = {}
        self.reuse()

    def test_python_route_phase5_and_evaluator_edits_invalidate_metrics(self):
        for name in self.summary["python_source_sha256"]:
            with self.subTest(name=name):
                original = self.summary["python_source_sha256"][name]
                self.summary["python_source_sha256"][name] = "changed"
                with self.assertRaisesRegex(RuntimeError, "python_source_sha256"):
                    self.reuse()
                self.summary["python_source_sha256"][name] = original
        self.assertEqual(self.current["arms"], {})

    def test_runner_and_runtime_change_or_missing_identity_rejected(self):
        for key in ("runner_sha256", "python", "python_executable_sha256", "python_source_sha256"):
            with self.subTest(key=key):
                original = self.baseline.pop(key)
                with self.assertRaisesRegex(RuntimeError, key):
                    self.reuse()
                self.baseline[key] = "different"
                with self.assertRaisesRegex(RuntimeError, key):
                    self.reuse()
                self.baseline[key] = original

    def test_settings_and_native_control_changes_rejected(self):
        self.summary["tdm_settings"]["frame"] = 2048
        with self.assertRaisesRegex(RuntimeError, "TDM settings"):
            self.reuse()
        self.summary["tdm_settings"]["frame"] = 1024
        self.summary["tools"]["tdm_ratio_optimizer"]["sha256"] = "changed"
        with self.assertRaisesRegex(RuntimeError, "native tool"):
            self.reuse()

    def test_missing_normalized_seal_rejected_even_when_bytes_match(self):
        del self.case["normalized_artifact_sha256"]
        with self.assertRaisesRegex(RuntimeError, "normalized artifacts"):
            self.reuse()

    def test_missing_output_seals_rejected(self):
        del self.case["arms"]["A"]["output_artifact_sha256"]
        with self.assertRaisesRegex(RuntimeError, "output seals missing"):
            self.reuse()

    def test_mutated_route_plan_or_evaluation_rejected(self):
        for name, seal in self.case["arms"]["A"]["output_artifact_sha256"].items():
            with self.subTest(name=name):
                path = Path(seal["path"])
                original = path.read_bytes()
                path.write_text('{"changed": true}')
                with self.assertRaisesRegex(RuntimeError, "sealed output changed"):
                    self.reuse()
                path.write_bytes(original)

    def test_summary_metrics_must_match_sealed_evaluation(self):
        self.case["arms"]["A"]["evaluation"]["metrics"]["score"] = 0
        with self.assertRaisesRegex(RuntimeError, "sealed metrics"):
            self.reuse()

    def test_changed_normalized_input_rejected(self):
        self.current["normalized_artifact_sha256"]["assignment"] = "new-partition"
        with self.assertRaisesRegex(RuntimeError, "normalized model differs"):
            self.reuse()


if __name__ == "__main__":
    unittest.main()
