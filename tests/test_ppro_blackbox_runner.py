from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.ppro_blackbox_runner import (
    RuntimeBinding,
    execute_blackbox_case,
    execute_blackbox_queue,
    validate_run_spec,
    validate_runtime_binding,
)
from emuflow.ppro_blackbox_provenance import runner_source_bundle


ROOT = Path(__file__).resolve().parents[1]
MOCK_PROVIDER = ROOT / "tests/fixtures/ppro_blackbox/mock_ordinary_provider.py"
RUNNER_REVISION = runner_source_bundle()["runner_revision"]


def run_spec(identifier: str = "mock-run-1"):
    return {
        "schema": "emuflow.ppro-blackbox-run-spec/v1",
        "identity": {
            "id": identifier,
            "campaign_id": "mock-stage2",
            "case_id": "latency-single-hop-w64",
            "role": "fit",
            "public_prior_id": "lx2-public-prior-v1",
            "configuration_id": "lx2-m1",
        },
        "tool": {
            "name": "PPro mock",
            "release": "mock-1",
            "runner_revision": RUNNER_REVISION,
        },
        "workload": {
            "generator_id": "ppro-blackbox-latency",
            "generator_revision": "1111111111111111111111111111111111111111111111111111111111111111",
            "rtl_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
            "parameters_sha256": "3333333333333333333333333333333333333333333333333333333333333333",
            "top_module": "ppro_blackbox_latency",
            "design_metrics": {"clock_domains": 1, "instances": 1200, "nets": 1800},
        },
        "experiment": {
            "kind": "latency",
            "control_mode": "fixed_communication",
            "documented_actions": [
                "net_route_constraint",
                "partition_constraint",
                "random_seed",
            ],
            "constraints_sha256": "4444444444444444444444444444444444444444444444444444444444444444",
        },
        "execution": {"seed": 7},
        "adapter": {
            "profile": "mock-ordinary-reports-v1",
            "expected_reports": [
                "resource_summary",
                "partition_summary",
                "route_summary",
                "system_timing",
            ],
        },
    }


def binding(root: Path, mode: str = "pass") -> RuntimeBinding:
    return RuntimeBinding(
        case_dir=root,
        command=(sys.executable, str(MOCK_PROVIDER), "--mode", mode),
        report_paths={
            "resource_summary": root / "resource_summary.csv",
            "partition_summary": root / "partition_summary.csv",
            "route_summary": root / "route_summary.csv",
            "system_timing": root / "sr0_time.rpt",
        },
        output_path=root / "observation.json",
        timeout_seconds=30,
    )


def capacity_spec(identifier: str = "capacity-run"):
    spec = copy.deepcopy(run_spec(identifier))
    spec["workload"]["generator_id"] = "ppro-blackbox-capacity-lut-v4"
    spec["workload"]["design_metrics"] = {"requested_units": 160000}
    spec["experiment"] = {
        "kind": "resource_capacity",
        "control_mode": "fixed_assignment",
        "documented_actions": ["partition_constraint", "random_seed"],
        "constraints_sha256": "4" * 64,
    }
    spec["adapter"]["expected_reports"] = [
        "partition_summary",
        "resource_summary",
    ]
    return spec


class PProBlackboxRunnerTest(unittest.TestCase):
    def test_execution_rejects_stale_runner_revision_before_launch(self):
        with tempfile.TemporaryDirectory() as raw:
            spec = run_spec("stale-runtime")
            spec["tool"]["runner_revision"] = "0" * 64
            case = Path(raw) / "case"
            with self.assertRaisesRegex(
                ValidationError,
                "runner revision does not match the active runtime source bundle",
            ):
                execute_blackbox_case(spec, binding(case))
            self.assertFalse(case.exists())

    def test_run_spec_matches_versioned_json_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed")
        schema = json.loads(
            (ROOT / "schemas/ppro-blackbox-run-spec-v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        jsonschema.Draft202012Validator(schema).validate(run_spec())

    def test_run_spec_has_no_runtime_or_vendor_database_slot(self):
        normalized = validate_run_spec(run_spec())
        text = json.dumps(normalized, sort_keys=True)
        for forbidden in ("/data/", "/research/", "boarddb", "stf", "license"):
            self.assertNotIn(forbidden, text.lower())
        self.assertNotIn("command", normalized)
        self.assertNotIn("report_paths", normalized)

    def test_real_profile_uses_runtime_only_aliases_and_nested_reports(self):
        spec = copy.deepcopy(run_spec())
        spec["adapter"]["profile"] = "ppro-2026-ordinary-reports-v1"
        self.assertEqual(
            validate_run_spec(spec)["adapter"]["profile"],
            "ppro-2026-ordinary-reports-v1",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "case"
            reports = root / "project" / "rtlpart" / "report"
            paths = {
                "resource_summary": reports / "pa0.rpt",
                "partition_summary": reports / "pa0.rpt",
                "route_summary": reports / "sr0.rpt",
                "system_timing": reports / "sr0_time.rpt",
            }
            missing_aliases = RuntimeBinding(
                case_dir=root,
                command=("true",),
                report_paths=paths,
                output_path=root / "observation.json",
            )
            with self.assertRaisesRegex(ValidationError, "requires FPGA aliases"):
                validate_runtime_binding(
                    missing_aliases, profile="ppro-2026-ordinary-reports-v1"
                )
            validate_runtime_binding(
                RuntimeBinding(
                    case_dir=root,
                    command=("true",),
                    report_paths=paths,
                    output_path=root / "observation.json",
                    fpga_aliases={"F1": "F0", "F3": "F1"},
                ),
                profile="ppro-2026-ordinary-reports-v1",
            )

    def test_mock_pass_emits_compact_observation_and_removes_raw_reports(self):
        with tempfile.TemporaryDirectory() as raw:
            case = Path(raw) / "case"
            scratch = case / "active-project"
            scratch.mkdir(parents=True)
            (scratch / "large-intermediate.bin").write_bytes(b"intermediate")
            base = binding(case)
            value = RuntimeBinding(
                case_dir=base.case_dir,
                command=base.command,
                report_paths=base.report_paths,
                output_path=base.output_path,
                timeout_seconds=base.timeout_seconds,
                cleanup_paths=(scratch,),
            )
            result = execute_blackbox_case(run_spec(), value)
            self.assertEqual(result["execution"]["outcome"], "pass")
            self.assertEqual(result["execution"]["seed"], 7)
            self.assertTrue(result["derived"]["fit_eligible"])
            self.assertEqual(
                result["metrics"]["timing"]["sr0_worst_cross_fpga_delay_ns"],
                12.5,
            )
            self.assertTrue((case / "observation.json").is_file())
            self.assertLess((case / "observation.json").stat().st_size, 8192)
            for path in binding(case).report_paths.values():
                self.assertFalse(path.exists())
            self.assertFalse((case / ".runner-stdout.log").exists())
            self.assertFalse((case / ".runner-stderr.log").exists())
            self.assertFalse(scratch.exists())

    def test_missing_report_is_not_an_evaluated_observation(self):
        with tempfile.TemporaryDirectory() as raw:
            case = Path(raw) / "case"
            result = execute_blackbox_case(run_spec(), binding(case, "missing"))
            self.assertEqual(result["execution"]["outcome"], "missing_report")
            self.assertFalse(result["derived"]["fit_eligible"])
            self.assertFalse(any(result["reports"].values()))
            self.assertEqual(result["metrics"]["timing"], {})

    def test_license_and_tool_failures_are_distinct(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            license_result = execute_blackbox_case(
                run_spec("license-run"), binding(root / "license", "license")
            )
            tool_result = execute_blackbox_case(
                run_spec("tool-run"), binding(root / "tool", "tool")
            )
            self.assertEqual(license_result["execution"]["outcome"], "license_failure")
            self.assertEqual(tool_result["execution"]["outcome"], "tool_failure")
            self.assertEqual(tool_result["execution"]["failure_code"], "tool-exit-17")

    def test_capacity_boundary_is_evaluated_and_preserves_control_coordinate(self):
        with tempfile.TemporaryDirectory() as raw:
            result = execute_blackbox_case(
                capacity_spec(), binding(Path(raw) / "capacity", "capacity")
            )
            self.assertEqual(result["execution"]["outcome"], "capacity_infeasible")
            self.assertEqual(result["execution"]["failure_code"], "capacity-boundary")
            self.assertEqual(result["metrics"]["design"]["requested_units"], 160000)
            self.assertTrue(result["derived"]["fit_eligible"])

    def test_capacity_pass_does_not_require_cross_fpga_timing_report(self):
        with tempfile.TemporaryDirectory() as raw:
            result = execute_blackbox_case(
                capacity_spec("capacity-pass"), binding(Path(raw) / "capacity", "missing")
            )
            self.assertEqual(result["execution"]["outcome"], "pass")
            self.assertTrue(result["reports"]["resource_summary"])
            self.assertTrue(result["reports"]["partition_summary"])
            self.assertFalse(result["reports"]["system_timing"])
            self.assertEqual(result["metrics"]["timing"], {})

    def test_explicit_diagnostic_mode_retains_only_bounded_failure_tails(self):
        with tempfile.TemporaryDirectory() as raw:
            case = Path(raw) / "case"
            value = binding(case)
            value = RuntimeBinding(
                case_dir=value.case_dir,
                command=(
                    sys.executable,
                    "-c",
                    "import sys; print('x'*20000); print('y'*20000,file=sys.stderr); sys.exit(9)",
                ),
                report_paths=value.report_paths,
                output_path=value.output_path,
                retain_failure_diagnostics=True,
            )
            result = execute_blackbox_case(run_spec(), value)
            self.assertEqual(result["execution"]["failure_code"], "tool-exit-9")
            for name in (".runner-stdout.log", ".runner-stderr.log"):
                path = case / name
                self.assertTrue(path.is_file())
                self.assertLessEqual(path.stat().st_size, 16384)

    def test_malformed_report_is_a_terminal_non_hardware_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            case = Path(raw) / "case"
            value = binding(case)
            value = RuntimeBinding(
                case_dir=value.case_dir,
                command=(
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    "Path('resource_summary.csv').write_text('wrong,columns\\n1,2\\n'); "
                    "Path('partition_summary.csv').write_text('x\\n1\\n'); "
                    "Path('route_summary.csv').write_text('x\\n1\\n'); "
                    "Path('sr0_time.rpt').write_text('bad')",
                ),
                report_paths=value.report_paths,
                output_path=value.output_path,
            )
            result = execute_blackbox_case(run_spec(), value)
            self.assertEqual(
                result["execution"]["outcome"], "report_parse_failure"
            )
            self.assertFalse(result["derived"]["fit_eligible"])
            self.assertEqual(result["metrics"]["resource_demand"], {})

    def test_timeout_terminates_the_provider_process_group(self):
        with tempfile.TemporaryDirectory() as raw:
            case = Path(raw) / "case"
            pid_path = case / "child.pid"
            value = binding(case)
            value = RuntimeBinding(
                case_dir=value.case_dir,
                command=(
                    sys.executable,
                    "-c",
                    "import subprocess,time,pathlib; "
                    "child=subprocess.Popen(['sleep','30']); "
                    "pathlib.Path('child.pid').write_text(str(child.pid)); "
                    "time.sleep(30)",
                ),
                report_paths=value.report_paths,
                output_path=value.output_path,
                timeout_seconds=0.2,
            )
            result = execute_blackbox_case(run_spec(), value)
            self.assertEqual(result["execution"]["failure_code"], "execution-timeout")
            child_pid = int(pid_path.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_runtime_report_paths_cannot_escape_case_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            value = binding(root / "case")
            paths = dict(value.report_paths)
            paths["system_timing"] = root / "outside.rpt"
            invalid = RuntimeBinding(
                case_dir=value.case_dir,
                command=value.command,
                report_paths=paths,
                output_path=value.output_path,
            )
            with self.assertRaisesRegex(ValidationError, "isolated case"):
                validate_runtime_binding(invalid)

    def test_runtime_cleanup_paths_are_confined_and_preserve_observation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            value = binding(root / "case")
            outside = RuntimeBinding(
                case_dir=value.case_dir,
                command=value.command,
                report_paths=value.report_paths,
                output_path=value.output_path,
                cleanup_paths=(root / "outside",),
            )
            with self.assertRaisesRegex(ValidationError, "cleanup paths"):
                validate_runtime_binding(outside)
            observation = RuntimeBinding(
                case_dir=value.case_dir,
                command=value.command,
                report_paths=value.report_paths,
                output_path=value.output_path,
                cleanup_paths=(value.output_path,),
            )
            with self.assertRaisesRegex(ValidationError, "compact observation"):
                validate_runtime_binding(observation)

    def test_queue_is_explicitly_bounded_and_result_order_is_stable(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cases = [
                (run_spec("mock-run-b"), binding(root / "b")),
                (run_spec("mock-run-a"), binding(root / "a")),
            ]
            result = execute_blackbox_queue(cases, max_workers=2)
            self.assertEqual(
                [item["identity"]["id"] for item in result],
                ["mock-run-a", "mock-run-b"],
            )
            with self.assertRaisesRegex(ValidationError, "positive integer"):
                execute_blackbox_queue(cases, max_workers=0)


if __name__ == "__main__":
    unittest.main()
