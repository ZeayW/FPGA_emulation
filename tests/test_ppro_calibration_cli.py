from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.ppro_calibration_cli import _campaign_status, _parser, main
from emuflow.ppro_blackbox_provenance import runner_source_bundle


ROOT = Path(__file__).resolve().parents[1]
RUNNER_REVISION = runner_source_bundle()["runner_revision"]


class PProCalibrationCliTest(unittest.TestCase):
    def test_free_partition_runtime_can_omit_logical_targets(self):
        args = _parser().parse_args(
            [
                "run-ppro-case",
                "--run-spec",
                "run-spec.json",
                "--filelist",
                "sources.f",
                "--case-dir",
                "case",
                "--install-root",
                "install",
                "--platform-reference",
                "platform.ref",
                "--documented-constraints",
                "constraints.json",
                "--fpga-alias",
                "F0=F0",
            ]
        )
        self.assertEqual(args.logical_target, [])

    def test_runner_revision_covers_complete_runtime_source_bundle(self):
        output = StringIO()
        with redirect_stdout(output):
            code = main(["runner-revision"])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "pass")
        self.assertEqual(
            result["schema"], "emuflow.ppro-runner-source-bundle/v1"
        )
        self.assertGreaterEqual(len(result["members"]), 8)
        self.assertEqual(len(result["runner_revision"]), 64)

    def test_generation_rejects_stale_runner_revision(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(
                ValidationError,
                "runner revision does not match the current runtime source bundle",
            ):
                main(
                    [
                        "generate-smoke",
                        "--out",
                        str(Path(raw) / "smoke"),
                        "--campaign-id",
                        "stale-runner-revision",
                        "--configuration-id",
                        "lx2-m1",
                        "--tool-release",
                        "2026.1",
                        "--runner-revision",
                        "0" * 64,
                    ]
                )

    def test_campaign_accepts_measured_boundaries_but_not_provider_failures(self):
        self.assertEqual(
            _campaign_status({"pass": 2, "capacity_infeasible": 2}, 4),
            "pass",
        )
        self.assertEqual(
            _campaign_status({"pass": 2, "tool_failure": 1}, 3),
            "failed",
        )

    def test_validate_public_prior(self):
        output = StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "validate-artifact",
                    str(ROOT / "calibration/ppro_blackbox/priors/lx2-public-prior-v1.json"),
                ]
            )
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["schema"], "emuflow.ppro-public-platform-prior/v1")

    def test_generate_smoke_and_validate_run_spec(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "smoke"
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "generate-smoke",
                        "--out",
                        str(root),
                        "--campaign-id",
                        "cli-smoke",
                        "--configuration-id",
                        "lx2-m1",
                        "--tool-release",
                        "2026.1",
                        "--runner-revision",
                        RUNNER_REVISION,
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue((root / "run-spec.json").is_file())
            generated = json.loads((root / "run-spec.json").read_text(encoding="utf-8"))
            self.assertEqual(
                generated["adapter"]["profile"], "ppro-2026-ordinary-reports-v1"
            )
            validation = StringIO()
            with redirect_stdout(validation):
                main(["validate-run-spec", str(root / "run-spec.json")])
            self.assertEqual(
                json.loads(validation.getvalue())["schema"],
                "emuflow.ppro-blackbox-run-spec/v1",
            )

    def test_mock_report_profile_requires_explicit_selection(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "real-smoke"
            with redirect_stdout(StringIO()):
                code = main(
                    [
                        "generate-smoke",
                        "--out",
                        str(root),
                        "--campaign-id",
                        "cli-mock-smoke",
                        "--configuration-id",
                        "lx2-m1",
                        "--tool-release",
                        "2026.1",
                        "--runner-revision",
                        RUNNER_REVISION,
                        "--adapter-profile",
                        "mock-ordinary-reports-v1",
                    ]
                )
            self.assertEqual(code, 0)
            spec = json.loads((root / "run-spec.json").read_text(encoding="utf-8"))
            self.assertEqual(
                spec["adapter"]["profile"], "mock-ordinary-reports-v1"
            )

    def test_generate_capacity_topology_and_communication_cases(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            common = [
                "--campaign-id",
                "cli-campaign",
                "--configuration-id",
                "lx2-m2",
                "--tool-release",
                "2026.1",
                "--runner-revision",
                RUNNER_REVISION,
            ]
            output = StringIO()
            with redirect_stdout(output):
                main(
                    [
                        "generate-capacity-matrix",
                        "--out",
                        str(root / "capacity"),
                        *common,
                        "--axes",
                        "lut",
                        "ff",
                        "--fit-units",
                        "8",
                        "16",
                        "--holdout-units",
                        "12",
                        "--repeats",
                        "2",
                    ]
                )
            self.assertEqual(json.loads(output.getvalue())["case_count"], 12)

            output = StringIO()
            with redirect_stdout(output):
                main(
                    [
                        "generate-topology-matrix",
                        "--out",
                        str(root / "topology"),
                        *common,
                        "--fpga-count",
                        "3",
                        "--holdout-pair",
                        "0:2",
                        "--repeats",
                        "1",
                    ]
                )
            self.assertEqual(json.loads(output.getvalue())["case_count"], 7)

            output = StringIO()
            with redirect_stdout(output):
                main(
                    [
                        "generate-communication-probe",
                        "--out",
                        str(root / "communication"),
                        *common,
                        "--kind",
                        "latency",
                        "--fpga-count",
                        "3",
                        "--source-index",
                        "0",
                        "--sink-indices",
                        "1",
                        "--width",
                        "64",
                        "--role",
                        "fit",
                    ]
                )
            result = json.loads(output.getvalue())
            self.assertTrue(result["case_id"].startswith("cli-campaign.latency"))


if __name__ == "__main__":
    unittest.main()
