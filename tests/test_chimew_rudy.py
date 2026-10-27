import copy
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.transport.chimew import chimew_rudy as rudy_module
from emuflow.transport.chimew.chimew_rudy import (
    CHIMEW_RUDY_INPUT_PROVIDER,
    CHIMEW_RUDY_INPUT_SCHEMA,
    CHIMEW_RUDY_PROVIDER,
    evaluate_chimew_rudy,
    validate_chimew_rudy_input,
)
from emuflow.common.errors import ValidationError
from tests.native_build import emuflow_native


ROOT = Path(__file__).resolve().parents[1]


class ChimewRudyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.executable = emuflow_native()
        cls.command = [str(cls.executable), "phase6", "rudy"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def setUp(self) -> None:
        self.document = {
            "schema": CHIMEW_RUDY_INPUT_SCHEMA,
            "provider": CHIMEW_RUDY_INPUT_PROVIDER,
            "design": "rudy_fixture",
            "platform": "physical_grid",
            "coordinate_system": "physical-site-xy",
            "degenerate_bbox_policy": "reject",
            "wire_pitch_per_layer": 1.0,
            "max_utilization": 0.8,
            "provenance": {
                "producer": "fixture-lookahead-placer",
                "producer_version": "1",
                "placement_sha256": "a" * 64,
                "netlist_sha256": "b" * 64,
                "architecture_sha256": "c" * 64,
            },
            "grid": {
                "origin_x": 0.0,
                "origin_y": 0.0,
                "bin_width": 10.0,
                "bin_height": 10.0,
                "columns": 2,
                "rows": 2,
                "capacities": [40.0, 40.0, 40.0, 40.0],
            },
            "metrics": {"nets": 2, "pins": 4},
            "nets": [
                {
                    "id": "diagonal",
                    "pins": [{"x": 0.0, "y": 0.0}, {"x": 20.0, "y": 20.0}],
                },
                {
                    "id": "lower_left",
                    "pins": [{"x": 0.0, "y": 0.0}, {"x": 10.0, "y": 10.0}],
                },
            ],
        }

    def test_native_rudy_matches_independent_bin_integration(self) -> None:
        result = evaluate_chimew_rudy(
            self.document, executable=str(self.executable)
        )
        self.assertEqual(result["provider"], CHIMEW_RUDY_PROVIDER)
        self.assertEqual(result["gate_status"], "pass")
        self.assertEqual(result["integration_status"], "not-a-phase6-pin-plan")
        self.assertAlmostEqual(result["metrics"]["total_wire_area"], 60.0)
        self.assertAlmostEqual(result["metrics"]["total_bin_load"], 60.0)
        self.assertAlmostEqual(result["metrics"]["peak_utilization"], 0.75)
        self.assertEqual(
            [round(record["load"], 9) for record in result["bins"]],
            [30.0, 10.0, 10.0, 10.0],
        )
        self.assertEqual(result["metrics"]["oracle_disagreements"], 0)

    def test_file_publication_matches_stdin_stdout_stream(self) -> None:
        problem = validate_chimew_rudy_input(self.document)
        stream = io.StringIO()
        rudy_module._write_native_input(problem, stream)
        payload = stream.getvalue()
        streamed = subprocess.run([*self.command, "-", "-"], input=payload,
            text=True, capture_output=True, check=True)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "rudy.input"
            output = Path(temporary) / "rudy.output"
            source.write_text(payload)
            subprocess.run([*self.command, str(source), str(output)], check=True)
            self.assertEqual(output.read_text(), streamed.stdout)

    def test_production_uses_unified_stage_resolver_without_scratch_payloads(self) -> None:
        with mock.patch.object(
            rudy_module,
            "resolve_native_stage_command",
            return_value=[*self.command],
        ) as resolver, mock.patch.object(
            Path,
            "write_text",
            side_effect=AssertionError("RUDY scratch payload was written"),
        ):
            result = evaluate_chimew_rudy(self.document)
        resolver.assert_called_once_with(
            "phase6", "rudy", explicit=None
        )
        self.assertEqual(result["metrics"]["oracle_disagreements"], 0)

    def test_explicit_qualification_threshold_can_reject(self) -> None:
        rejected = copy.deepcopy(self.document)
        rejected["max_utilization"] = 0.7
        result = evaluate_chimew_rudy(rejected, executable=str(self.executable))
        self.assertEqual(result["gate_status"], "rejected")
        self.assertEqual(result["metrics"]["overloaded_bins"], 1)
        self.assertEqual(
            result["threshold_scope"],
            "explicit-qualification-policy-not-paper-constant",
        )

    def test_normalized_coordinates_and_opaque_provenance_are_rejected(self) -> None:
        normalized = copy.deepcopy(self.document)
        normalized["coordinate_system"] = "normalized-xy"
        with self.assertRaisesRegex(ValidationError, "normalized"):
            validate_chimew_rudy_input(normalized)
        opaque = copy.deepcopy(self.document)
        opaque["provenance"]["placement_sha256"] = "opaque"
        with self.assertRaisesRegex(ValidationError, "SHA-256"):
            validate_chimew_rudy_input(opaque)

    def test_zero_area_bbox_is_not_silently_expanded(self) -> None:
        degenerate = copy.deepcopy(self.document)
        degenerate["nets"][0]["pins"][1]["y"] = 0.0
        with self.assertRaisesRegex(ValidationError, "zero-area"):
            validate_chimew_rudy_input(degenerate)

    def test_sparse_10000_net_grid_visits_only_intersected_bins(self) -> None:
        document = copy.deepcopy(self.document)
        document["grid"].update(
            {
                "columns": 100,
                "rows": 100,
                "bin_width": 1.0,
                "bin_height": 1.0,
                "capacities": [1000.0] * 10000,
            }
        )
        document["nets"] = []
        for index in range(10000):
            column = index % 100
            row = index // 100
            document["nets"].append(
                {
                    "id": f"n{index}",
                    "pins": [
                        {"x": column + 0.1, "y": row + 0.1},
                        {"x": column + 0.9, "y": row + 0.9},
                    ],
                }
            )
        document["metrics"] = {"nets": 10000, "pins": 20000}
        result = evaluate_chimew_rudy(document, executable=str(self.executable))
        self.assertEqual(result["metrics"]["nets"], 10000)
        self.assertEqual(result["metrics"]["oracle_disagreements"], 0)
        self.assertAlmostEqual(
            result["metrics"]["total_wire_area"],
            result["metrics"]["total_bin_load"],
        )


if __name__ == "__main__":
    unittest.main()
