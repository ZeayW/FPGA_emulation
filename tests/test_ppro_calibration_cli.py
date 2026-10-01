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
            validation = StringIO()
            with redirect_stdout(validation):
                main(["validate-run-spec", str(root / "run-spec.json")])
            self.assertEqual(
                json.loads(validation.getvalue())["schema"],
                "emuflow.ppro-blackbox-run-spec/v1",
            )

    def test_generate_real_report_profile(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "real-smoke"
            with redirect_stdout(StringIO()):
                code = main(
                    [
                        "generate-smoke",
                        "--out",
                        str(root),
                        "--campaign-id",
                        "cli-real-smoke",
                        "--configuration-id",
                        "lx2-m1",
                        "--tool-release",
                        "2026.1",
                        "--runner-revision",
                        "a" * 64,
                        "--adapter-profile",
                        "ppro-2026-ordinary-reports-v1",
                    ]
                )
            self.assertEqual(code, 0)
            spec = json.loads((root / "run-spec.json").read_text(encoding="utf-8"))
            self.assertEqual(
                spec["adapter"]["profile"], "ppro-2026-ordinary-reports-v1"
            )


if __name__ == "__main__":
    unittest.main()
