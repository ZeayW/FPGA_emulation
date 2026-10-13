from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from emuflow.errors import ValidationError
from scripts import ppro_bank_singlehop_campaign as driver


class PProBankSinglehopCampaignTest(unittest.TestCase):
    def _arguments(self, root: Path, *extra: str):
        install = root / "install"
        (install / "bin").mkdir(parents=True)
        (install / "bin" / "rtlpart_linux").write_text("fixture\n")
        (install / "setting_rtl.sh").write_text("true\n")
        platform = root / "platform.ref"
        platform.write_text("opaque\n")
        values = [
            "--install-root", str(install), "--platform-reference", str(platform),
            "--result-root", str(root / "results"),
            "--authorized-writable-root", str(root),
            "--campaign-id", "bank-test", "--configuration-id", "lx2-m2",
            "--tool-release", "2026.1",
        ]
        for physical, logical in (("F1", "F0"), ("F3", "F1"),
                                  ("F2", "F2"), ("F4", "F3")):
            values += ["--fpga-alias", f"{physical}={logical}"]
            values += ["--logical-target", f"{logical}=MB1.{physical}"]
        return driver._parser().parse_args([*values, *extra])

    @staticmethod
    def _observation(spec, binding, outcome="pass"):
        return {
            "identity": spec["identity"],
            "execution": {"outcome": outcome, "failure_code": None,
                          "runtime_seconds": 1.0},
            "metrics": {
                "calibration_details": {
                    "bank_rows": [{"maximum_tdm_ratio": 8}],
                    "timing_paths": [],
                    "identification": {"single_hop_delay": "not_identifiable"},
                },
            },
        }

    def test_pilot_runs_two_asymmetric_cases_and_cannot_be_restarted(self):
        with tempfile.TemporaryDirectory() as raw:
            args = self._arguments(Path(raw))
            with mock.patch.object(driver, "execute_blackbox_case",
                                   side_effect=self._observation) as execute:
                with redirect_stdout(StringIO()):
                    summary = driver.run(args)
                self.assertEqual(execute.call_count, 2)
                for index, call in enumerate(execute.call_args_list):
                    spec, binding = call.args
                    metrics = spec["workload"]["design_metrics"]
                    self.assertEqual(metrics["forward_data_bits"], 32)
                    self.assertEqual(metrics["reverse_data_bits_per_sink"], [0, 16][index])
                    self.assertEqual(metrics["clock_period_ns"], 10)
                    self.assertEqual(metrics["source_fpga_index"], 0)
                    self.assertEqual(metrics["sink_fpga_index"], 2)
                    self.assertEqual(metrics["forced_tdm_ratio"], 0)
                    self.assertFalse(binding.cleanup_raw_reports)
                    self.assertTrue(binding.retain_failure_diagnostics)
                self.assertEqual(summary["status"], "completed")
                self.assertEqual(summary["calibration_status"], "observations-only-not-a-fitted-platform")
                self.assertEqual(summary["cases"][1]["observed_bank_tdm_ratios"], [8])
                self.assertTrue((args.result_root / "w32-rw0-r0" / "case-summary.json").is_file())
                with self.assertRaisesRegex(ValidationError, "already exists"):
                    driver.run(args)
                self.assertEqual(execute.call_count, 2)

    def test_matrix_uses_observed_ratios_without_forcing(self):
        with tempfile.TemporaryDirectory() as raw:
            args = self._arguments(Path(raw), "--mode", "matrix", "--widths", "32", "64",
                                   "--reverse-widths", "0", "16", "--repeats", "2",
                                   "--required-observed-tdm-ratios", "1", "8")
            with mock.patch.object(driver, "execute_blackbox_case",
                                   side_effect=self._observation) as execute:
                with redirect_stdout(StringIO()):
                    summary = driver.run(args)
            self.assertEqual(execute.call_count, 8)
            self.assertEqual(summary["status"], "incomplete-ratio-coverage")
            self.assertEqual(summary["missing_observed_tdm_ratios"], [1])
            self.assertTrue(all(call.args[0]["workload"]["design_metrics"]["forced_tdm_ratio"] == 0
                                for call in execute.call_args_list))
            self.assertTrue(all(call.args[1].cleanup_raw_reports for call in execute.call_args_list))
            self.assertEqual(summary["raw_project_policy"], "remove-after-observation")

    def test_matrix_can_explicitly_retain_raw_project(self):
        with tempfile.TemporaryDirectory() as raw:
            args = self._arguments(Path(raw), "--mode", "matrix", "--widths", "32",
                                   "--reverse-widths", "16", "--keep-raw-project")
            with mock.patch.object(driver, "execute_blackbox_case",
                                   side_effect=self._observation) as execute:
                with redirect_stdout(StringIO()):
                    summary = driver.run(args)
            self.assertFalse(execute.call_args.args[1].cleanup_raw_reports)
            self.assertEqual(summary["raw_project_policy"], "retain-for-active-inspection")

    def test_detached_checkout_does_not_require_git_executable(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("a" * 40 + "\n")
            with mock.patch.object(driver.shutil, "which", return_value=None), \
                    mock.patch.object(driver.subprocess, "check_output") as execute:
                self.assertEqual(driver._source_commit(root), "a" * 40)
                execute.assert_not_called()
                (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
                with self.assertRaisesRegex(ValidationError, "detached 40-hex HEAD"):
                    driver._source_commit(root)
                (root / ".git" / "HEAD").write_text("a" * 39 + "\n")
                with self.assertRaisesRegex(ValidationError, "detached 40-hex HEAD"):
                    driver._source_commit(root)

    def test_detached_worktree_git_pointer_resolves_head(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "metadata").mkdir()
            (root / "metadata" / "HEAD").write_text("B" * 40 + "\n")
            (root / ".git").write_text("gitdir: metadata\n")
            self.assertEqual(driver._source_commit(root), "b" * 40)

    def test_failure_stops_before_second_case(self):
        with tempfile.TemporaryDirectory() as raw:
            args = self._arguments(Path(raw))
            with mock.patch.object(driver, "execute_blackbox_case", side_effect=
                                   lambda spec, binding: self._observation(spec, binding, "tool_failure")) as execute:
                with redirect_stdout(StringIO()):
                    summary = driver.run(args)
            self.assertEqual(summary["status"], "failed")
            self.assertEqual(execute.call_count, 1)
            self.assertFalse((args.result_root / "w32-rw16-r0").exists())

    def test_preflight_rejects_storage_escape_and_noncanonical_pilot(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            args = self._arguments(root)
            args.result_root = root.parent / (root.name + "-escape")
            with self.assertRaisesRegex(ValidationError, "authorized writable root"):
                driver.run(args)
            self.assertFalse(args.result_root.exists())
            args.result_root = root / "results"
            args.widths = [64]
            with self.assertRaisesRegex(ValidationError, "pilot is fixed"):
                driver.run(args)
            self.assertFalse(args.result_root.exists())


if __name__ == "__main__":
    unittest.main()
