import hashlib
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from emuflow.chimew_correlation import (
    CHIMEW_VIVADO_CORRELATION_INPUT_SCHEMA,
    CHIMEW_VIVADO_CORRELATION_REPORT_SCHEMA,
    build_chimew_vivado_correlation,
    validate_chimew_vivado_correlation,
)
from emuflow.chimew_pipeline import CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER
from emuflow.errors import ValidationError
from emuflow.io import read_json, write_json
from emuflow.vivado_board_flow import VIVADO_BOARD_FLOW_SCHEMA


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def _validated_chimew_snapshot(bundle: Path, **_kwargs):
    yield read_json(Path(bundle) / "pipeline_report.json"), {
        "qualification_scope": "byte-bound-source-artifacts"
    }


class ChimewVivadoCorrelationTest(unittest.TestCase):
    def _candidate(
        self,
        root: Path,
        candidate_id: str,
        *,
        predicted: float,
        actual: int,
    ) -> dict:
        chimew = root / candidate_id / "chimew"
        vivado = root / candidate_id / "vivado"
        (chimew / "phase6-adapter").mkdir(parents=True)
        (vivado / "fpga0").mkdir(parents=True)
        write_json(
            chimew / "pipeline_report.json",
            {
                "provider": CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER,
                "design": "counter",
                "platform": "two-fpga",
                "metrics": {"rudy_peak_utilization": predicted},
            },
        )
        write_json(
            chimew / "phase6-adapter" / "adapter_report.json",
            {
                "validation": {
                    "crossing_bits": predicted * 10.0,
                    "pin_distance": predicted * 20.0,
                }
            },
        )
        congestion = vivado / "fpga0" / "congestion.csv"
        congestion.write_text(
            "Direction,Congestion Level,Window\n"
            f"North,{actual},window0\n",
            encoding="utf-8",
        )
        slr = vivado / "fpga0" / "slr_crossing.rpt"
        slr.write_text(f"Total SLR crossings: {actual * 2}\n", encoding="utf-8")
        write_json(
            vivado / "vivado-board-flow-report.json",
            {
                "schema": VIVADO_BOARD_FLOW_SCHEMA,
                "design": "counter",
                "platform": "two-fpga",
                "fpgas": [
                    {
                        "fpga": "fpga0",
                        "physical_evidence": {"slr_count": 2},
                        "closure": {
                            "critical_path_ns": float(actual * 3),
                            "wns_ns": float(-actual),
                        },
                        "artifacts": {
                            "congestion.csv": {
                                "path": "fpga0/congestion.csv"
                            },
                            "slr_crossing.rpt": {
                                "path": "fpga0/slr_crossing.rpt"
                            },
                        },
                    }
                ],
            },
        )
        return {
            "id": candidate_id,
            "chimew_bundle": str(chimew.resolve()),
            "vivado_bundle": str(vivado.resolve()),
            "chimew_report_sha256": _sha256(chimew / "pipeline_report.json"),
            "vivado_report_sha256": _sha256(
                vivado / "vivado-board-flow-report.json"
            ),
        }

    def test_rank_gate_qualifies_three_monotonic_candidates_and_replays(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "input.json"
            write_json(
                manifest,
                {
                    "schema": CHIMEW_VIVADO_CORRELATION_INPUT_SCHEMA,
                    "minimum_spearman": 0.5,
                    "candidates": [
                        self._candidate(root, "candidate-a", predicted=0.1, actual=3),
                        self._candidate(root, "candidate-b", predicted=0.2, actual=4),
                        self._candidate(root, "candidate-c", predicted=0.4, actual=6),
                    ],
                },
            )
            output = root / "correlation.json"
            with (
                patch(
                    "emuflow.chimew_correlation.read_chimew_phase6_pipeline",
                    side_effect=_validated_chimew_snapshot,
                ),
                patch(
                    "emuflow.chimew_correlation.validate_vivado_board_flow_bundle",
                    return_value={"artifacts_verified": 14},
                ),
            ):
                report = build_chimew_vivado_correlation(manifest, output)
                self.assertEqual(
                    report["schema"], CHIMEW_VIVADO_CORRELATION_REPORT_SCHEMA
                )
                self.assertEqual(report["qualification"], "qualified")
                self.assertTrue(
                    all(
                        item["spearman_rho"] == 1.0
                        for item in report["correlations"].values()
                    )
                )
                validation = validate_chimew_vivado_correlation(manifest, output)
                self.assertEqual(validation["candidates"], 3)

                tampered = read_json(output)
                tampered["correlations"]["rudy_to_congestion"][
                    "spearman_rho"
                ] = 0.0
                write_json(output, tampered)
                with self.assertRaisesRegex(ValidationError, "replay differs"):
                    validate_chimew_vivado_correlation(manifest, output)

    def test_manifest_hash_prevents_cross_run_report_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidates = [
                self._candidate(root, f"candidate-{index}", predicted=index + 1, actual=index + 3)
                for index in range(3)
            ]
            manifest = root / "input.json"
            write_json(
                manifest,
                {
                    "schema": CHIMEW_VIVADO_CORRELATION_INPUT_SCHEMA,
                    "minimum_spearman": 0.5,
                    "candidates": candidates,
                },
            )
            first_report = (
                Path(candidates[0]["chimew_bundle"]) / "pipeline_report.json"
            )
            document = read_json(first_report)
            document["design"] = "cross-run"
            write_json(first_report, document)
            with self.assertRaisesRegex(ValidationError, "report hash differs"):
                build_chimew_vivado_correlation(manifest, root / "out.json")

    def test_rejects_non_machine_readable_physical_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidates = [
                self._candidate(root, f"candidate-{index}", predicted=index + 1, actual=index + 3)
                for index in range(3)
            ]
            bad = Path(candidates[0]["vivado_bundle"])
            (bad / "fpga0" / "congestion.csv").write_text(
                "not,a,congestion,table\n", encoding="utf-8"
            )
            # Keep the top-level Vivado report hash stable: the full bundle
            # validator owns artifact hashing, while this test isolates parsing.
            manifest = root / "input.json"
            write_json(
                manifest,
                {
                    "schema": CHIMEW_VIVADO_CORRELATION_INPUT_SCHEMA,
                    "minimum_spearman": 0.5,
                    "candidates": candidates,
                },
            )
            with (
                patch(
                    "emuflow.chimew_correlation.read_chimew_phase6_pipeline",
                    side_effect=_validated_chimew_snapshot,
                ),
                patch(
                    "emuflow.chimew_correlation.validate_vivado_board_flow_bundle",
                    return_value={"artifacts_verified": 14},
                ),
                self.assertRaisesRegex(ValidationError, "congestion levels"),
            ):
                build_chimew_vivado_correlation(manifest, root / "out.json")

    def test_constant_physical_metric_is_explicitly_insufficient(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "input.json"
            write_json(
                manifest,
                {
                    "schema": CHIMEW_VIVADO_CORRELATION_INPUT_SCHEMA,
                    "minimum_spearman": 0.5,
                    "candidates": [
                        self._candidate(
                            root,
                            f"candidate-{index}",
                            predicted=index + 1,
                            actual=3,
                        )
                        for index in range(3)
                    ],
                },
            )
            with (
                patch(
                    "emuflow.chimew_correlation.read_chimew_phase6_pipeline",
                    side_effect=_validated_chimew_snapshot,
                ),
                patch(
                    "emuflow.chimew_correlation.validate_vivado_board_flow_bundle",
                    return_value={"artifacts_verified": 14},
                ),
            ):
                report = build_chimew_vivado_correlation(
                    manifest, root / "out.json"
                )
            self.assertEqual(report["qualification"], "insufficient-evidence")
            self.assertTrue(
                all(
                    item["status"] == "insufficient-variation"
                    for item in report["correlations"].values()
                )
            )


if __name__ == "__main__":
    unittest.main()
