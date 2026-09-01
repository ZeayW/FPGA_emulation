import hashlib
import json
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_RESULT_ROOT = (
    _ROOT / "benchmarks/results/patron_static_exact_four_arm_case6"
)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PublishedPatronFourArmResultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = _read_json(_RESULT_ROOT / "result.json")

    def test_every_arm_is_bound_to_checked_phase7_evidence(self):
        self.assertEqual(
            self.result["schema"],
            "emuflow.patron-static-exact-four-arm-result/v1",
        )
        self.assertEqual(self.result["status"], "pass")
        self.assertEqual(set(self.result["arms"]), {
            "static-exact-v6",
            "static-exact-v9",
            "static-exact-v6-to-v11",
            "static-exact-v9-to-v11",
        })
        for label, arm in self.result["arms"].items():
            with self.subTest(arm=label):
                evidence = arm["evidence"]
                report_path = _RESULT_ROOT / evidence["phase7_report"]
                manifest_path = _RESULT_ROOT / evidence["full_manifest"]
                self.assertEqual(
                    _sha256(report_path), evidence["phase7_report_sha256"]
                )
                self.assertEqual(
                    _sha256(manifest_path), evidence["full_manifest_sha256"]
                )
                report = _read_json(report_path)
                self.assertEqual(report["status"], "pass")
                target = report["qor_projection"]["timing"]["target_clock"]
                physical = report["qor_projection"]["physical"]
                self.assertEqual(
                    target["worst_slack_bound_ns"],
                    arm["target_clock"]["wns_ns"],
                )
                self.assertEqual(
                    target["total_negative_slack_bound_ns"],
                    arm["target_clock"]["tns_ns"],
                )
                self.assertEqual(
                    target["negative_slack_paths"],
                    arm["target_clock"]["negative_paths"],
                )
                self.assertEqual(physical["unrouted_nets"], 0)
                self.assertEqual(physical["drc_violations"], 0)
                self.assertEqual(arm["path_coverage"]["ratio"], 1.0)
                self.assertEqual(arm["path_coverage"]["fallback_paths"], 0)
                self.assertEqual(
                    arm["path_coverage"]["discontinuous_compressed_paths"],
                    0,
                )

    def test_paired_deltas_are_reconstructed_from_phase7_metrics(self):
        for child, parent, comparison in (
            (
                "static-exact-v6-to-v11",
                "static-exact-v6",
                "static-exact-v6-to-v11-minus-v6",
            ),
            (
                "static-exact-v9-to-v11",
                "static-exact-v9",
                "static-exact-v9-to-v11-minus-v9",
            ),
        ):
            candidate = self.result["arms"][child]["target_clock"]
            baseline = self.result["arms"][parent]["target_clock"]
            delta = self.result["comparisons"][comparison]
            with self.subTest(comparison=comparison):
                self.assertAlmostEqual(
                    candidate["wns_ns"] - baseline["wns_ns"],
                    delta["target_clock_wns_delta_ns"],
                    places=12,
                )
                self.assertAlmostEqual(
                    candidate["tns_ns"] - baseline["tns_ns"],
                    delta["target_clock_tns_delta_ns"],
                    places=9,
                )
                self.assertEqual(
                    candidate["negative_paths"] - baseline["negative_paths"],
                    delta["negative_path_delta"],
                )
                self.assertGreater(delta["target_clock_tns_delta_ns"], 0.0)
                self.assertLess(delta["target_clock_wns_delta_ns"], 0.0)
        self.assertEqual(
            self.result["conclusion"]["dual_metric_promotion"], "fail"
        )
        self.assertEqual(
            self.result["conclusion"]["matrix_qualification"], "not_claimed"
        )


if __name__ == "__main__":
    unittest.main()
