from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from emuflow.ppro_calibration_cli import main


ROOT = Path(__file__).resolve().parents[1]


class PProCalibrationCliTest(unittest.TestCase):
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
                        "a" * 64,
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
                        "a" * 64,
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
                "b" * 64,
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
            self.assertEqual(json.loads(output.getvalue())["case_count"], 6)

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
