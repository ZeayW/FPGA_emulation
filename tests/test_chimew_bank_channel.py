import copy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow import chimew_bank_channel as bank_channel_module
from emuflow.chimew_bank_channel import (
    CHIMEW_BANK_CHANNEL_INPUT_PROVIDER,
    CHIMEW_BANK_CHANNEL_INPUT_SCHEMA,
    CHIMEW_BANK_CHANNEL_INPUT_SCHEMA_V1,
    CHIMEW_BANK_CHANNEL_PROVIDER,
    _verify_certificate,
    _verify_stage2_certificate,
    evaluate_chimew_bank_channel_assignment,
    validate_chimew_bank_channel_input,
)
from emuflow.errors import EmuFlowError, ValidationError


ROOT = Path(__file__).resolve().parents[1]


def member(identifier, source_y, sink_y, *, fanins=1, timing_weight=None):
    result = {
        "id": identifier,
        "fanout": {"x": 0.0, "y": source_y},
        "fanins": [
            {"x": 100.0, "y": sink_y + index} for index in range(fanins)
        ],
    }
    if timing_weight is not None:
        result["timing_weight"] = timing_weight
    return result


class ChimewBankChannelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        compiler = shutil.which("c++") or shutil.which("clang++")
        if compiler is None:
            raise RuntimeError("a C++17 compiler is required")
        cls.executable = Path(cls.temporary_directory.name) / "chimew-assignment"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-O2",
                "-pthread",
                str(ROOT / "src/native/chimew_bank_channel_assigner.cpp"),
                "-o",
                str(cls.executable),
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def setUp(self) -> None:
        self.document = {
            "schema": CHIMEW_BANK_CHANNEL_INPUT_SCHEMA,
            "provider": CHIMEW_BANK_CHANNEL_INPUT_PROVIDER,
            "design": "two_stage_fixture",
            "platform": "two_fpga_fixture",
            "coordinate_system": "physical-site-xy",
            "cost_quantization_per_site": 1000,
            "provenance": {
                "producer": "fixture-lookahead-flow",
                "producer_version": "1",
                "grouping_sha256": "a" * 64,
                "placement_sha256": "b" * 64,
                "architecture_sha256": "c" * 64,
            },
            "domains": [{"id": "AB", "fpga_a": "A", "fpga_b": "B"}],
            "bank_pairs": [
                {
                    "id": "near",
                    "domain": "AB",
                    "bank_a": {"id": "A0", "x": 0.0, "y": 0.0},
                    "bank_b": {"id": "B0", "x": 100.0, "y": 0.0},
                    "channels": [
                        {
                            "id": "near0",
                            "order": 0,
                            "pin_a": {"x": 0.0, "y": 0.0},
                            "pin_b": {"x": 100.0, "y": 0.0},
                        },
                        {
                            "id": "near1",
                            "order": 1,
                            "pin_a": {"x": 0.0, "y": 20.0},
                            "pin_b": {"x": 100.0, "y": 20.0},
                        },
                        {
                            "id": "near2",
                            "order": 2,
                            "pin_a": {"x": 0.0, "y": 40.0},
                            "pin_b": {"x": 100.0, "y": 40.0},
                        },
                    ],
                },
                {
                    "id": "far",
                    "domain": "AB",
                    "bank_a": {"id": "A1", "x": 0.0, "y": 100.0},
                    "bank_b": {"id": "B1", "x": 100.0, "y": 100.0},
                    "channels": [
                        {
                            "id": "far0",
                            "order": 0,
                            "pin_a": {"x": 0.0, "y": 100.0},
                            "pin_b": {"x": 100.0, "y": 100.0},
                        }
                    ],
                },
            ],
            "groups": [
                {
                    "id": "tdm_ab",
                    "domain": "AB",
                    "kind": "tdm_group",
                    "direction": "a_to_b",
                    "members": [member("s0", 0.0, 0.0)],
                },
                {
                    "id": "tdm_ba",
                    "domain": "AB",
                    "kind": "tdm_group",
                    "direction": "b_to_a",
                    "members": [
                        {
                            "id": "s1",
                            "fanout": {"x": 100.0, "y": 20.0},
                            "fanins": [{"x": 0.0, "y": 20.0}],
                        }
                    ],
                },
                {
                    "id": "common_near",
                    "domain": "AB",
                    "kind": "common_signal",
                    "direction": "a_to_b",
                    "members": [member("s2", 40.0, 40.0, fanins=2)],
                },
                {
                    "id": "common_far",
                    "domain": "AB",
                    "kind": "common_signal",
                    "direction": "b_to_a",
                    "members": [
                        {
                            "id": "s3",
                            "fanout": {"x": 100.0, "y": 100.0},
                            "fanins": [{"x": 0.0, "y": 100.0}],
                        }
                    ],
                },
            ],
            "metrics": {
                "groups": 4,
                "signals": 4,
                "fanins": 5,
                "bank_pairs": 2,
                "channels": 4,
            },
        }

    def test_two_stage_assignment_and_direction_priority_are_certified(self) -> None:
        result = evaluate_chimew_bank_channel_assignment(
            self.document, executable=str(self.executable)
        )
        self.assertEqual(result["provider"], CHIMEW_BANK_CHANNEL_PROVIDER)
        self.assertEqual(result["integration_status"], "not-a-phase6-pin-plan")
        self.assertEqual(result["metrics"]["certificate_disagreements"], 0)
        self.assertEqual(result["metrics"]["certified_matchings"], 5)
        assignments = {record["group"]: record for record in result["assignments"]}
        self.assertEqual(assignments["common_far"]["bank_pair"], "far")
        self.assertEqual(assignments["tdm_ab"]["channel"], "near0")
        self.assertEqual(assignments["tdm_ba"]["channel"], "near1")
        self.assertEqual(assignments["common_near"]["channel"], "near2")
        self.assertEqual(result["direction_priority"]["near"], "a_to_b")

    def test_fanin_cost_is_averaged_per_signal(self) -> None:
        result = evaluate_chimew_bank_channel_assignment(
            self.document, executable=str(self.executable)
        )
        common = next(
            record for record in result["assignments"] if record["group"] == "common_near"
        )
        self.assertEqual(common["channel_cost"], 0.5)

    def test_bidirectional_tdm_bundle_uses_member_specific_costs(self) -> None:
        document = copy.deepcopy(self.document)
        document["groups"] = [
            {
                "id": "shared_bundle",
                "domain": "AB",
                "kind": "tdm_group",
                "direction": "bidirectional",
                "members": [
                    {
                        **member("forward", 0.0, 0.0),
                        "direction": "a_to_b",
                    },
                    {
                        "id": "reverse",
                        "direction": "b_to_a",
                        "fanout": {"x": 100.0, "y": 20.0},
                        "fanins": [{"x": 0.0, "y": 20.0}],
                    },
                ],
            }
        ]
        document["metrics"] = {
            "groups": 1,
            "signals": 2,
            "fanins": 2,
            "bank_pairs": 2,
            "channels": 4,
            "bidirectional_bundles": 1,
        }
        report = evaluate_chimew_bank_channel_assignment(
            document, executable=str(self.executable)
        )
        self.assertEqual(report["metrics"]["bidirectional_bundles"], 1)
        self.assertEqual(len(report["assignments"]), 1)
        self.assertEqual(
            report,
            evaluate_chimew_bank_channel_assignment(
                document, executable=str(self.executable)
            ),
        )

        malformed = copy.deepcopy(document)
        malformed["groups"][0]["members"][1]["direction"] = "a_to_b"
        with self.assertRaisesRegex(ValidationError, "both directions"):
            validate_chimew_bank_channel_input(malformed)

    def test_legacy_v1_input_remains_supported(self) -> None:
        document = copy.deepcopy(self.document)
        document["schema"] = CHIMEW_BANK_CHANNEL_INPUT_SCHEMA_V1
        validated = validate_chimew_bank_channel_input(document)
        self.assertEqual(validated["metrics"]["bidirectional_bundles"], 0)
        self.assertEqual(
            evaluate_chimew_bank_channel_assignment(
                document, executable=str(self.executable)
            )["metrics"]["groups"],
            len(document["groups"]),
        )

    def test_dedicated_direction_bank_uses_its_full_lane_inventory(self) -> None:
        document = copy.deepcopy(self.document)
        document["bank_pairs"] = [document["bank_pairs"][0]]
        document["groups"] = [
            {
                "id": "high_y_ab",
                "domain": "AB",
                "kind": "tdm_group",
                "direction": "a_to_b",
                "members": [member("high_y", 40.0, 40.0)],
            }
        ]
        document["metrics"] = {
            "groups": 1,
            "signals": 1,
            "fanins": 1,
            "bank_pairs": 1,
            "channels": 3,
        }
        result = evaluate_chimew_bank_channel_assignment(
            document, executable=str(self.executable)
        )
        self.assertEqual(result["assignments"][0]["channel"], "near2")
        self.assertEqual(result["assignments"][0]["channel_cost"], 0.0)

    def test_unique_bank_candidate_fast_path_is_certified(self) -> None:
        document = copy.deepcopy(self.document)
        document["domains"].append(
            {"id": "CD", "fpga_a": "C", "fpga_b": "D"}
        )
        document["bank_pairs"][1]["domain"] = "CD"
        document["groups"][3]["domain"] = "CD"
        result = evaluate_chimew_bank_channel_assignment(
            document, executable=str(self.executable)
        )
        assignments = {
            record["group"]: record for record in result["assignments"]
        }
        self.assertEqual(assignments["tdm_ab"]["bank_pair"], "near")
        self.assertEqual(assignments["common_far"]["bank_pair"], "far")
        self.assertEqual(result["metrics"]["certificate_disagreements"], 0)

    def test_parallel_banks_are_deterministic_and_certified(self) -> None:
        document = copy.deepcopy(self.document)
        document["domains"].append(
            {"id": "CD", "fpga_a": "C", "fpga_b": "D"}
        )
        document["bank_pairs"][1]["domain"] = "CD"
        document["groups"][3]["domain"] = "CD"
        with mock.patch.dict(
            "os.environ", {"EMUFLOW_CHIMEW_BANK_WORKERS": "1"}
        ):
            serial = evaluate_chimew_bank_channel_assignment(
                document, executable=str(self.executable)
            )
        with mock.patch.dict(
            "os.environ", {"EMUFLOW_CHIMEW_BANK_WORKERS": "4"}
        ):
            parallel = evaluate_chimew_bank_channel_assignment(
                document, executable=str(self.executable)
            )
        self.assertEqual(parallel, serial)
        self.assertEqual(parallel["metrics"]["certificate_disagreements"], 0)

    def test_identical_channel_cost_rows_are_exactly_certified(self) -> None:
        document = copy.deepcopy(self.document)
        document["bank_pairs"] = [copy.deepcopy(document["bank_pairs"][0])]
        document["bank_pairs"][0]["channels"] = [
            {
                "id": f"near{channel}",
                "order": channel,
                "pin_a": {"x": 0.0, "y": float(channel)},
                "pin_b": {"x": 100.0, "y": float(channel)},
            }
            for channel in range(16)
        ]
        document["groups"] = [
            {
                "id": f"common{group}",
                "domain": "AB",
                "kind": "common_signal",
                "direction": "a_to_b",
                "members": [member(f"signal{group}", 8.0, 8.0)],
            }
            for group in range(16)
        ]
        document["metrics"] = {
            "groups": 16,
            "signals": 16,
            "fanins": 16,
            "bank_pairs": 1,
            "channels": 16,
        }
        with mock.patch.object(
            bank_channel_module,
            "_candidate_cost",
            wraps=bank_channel_module._candidate_cost,
        ) as candidate_cost:
            result = evaluate_chimew_bank_channel_assignment(
                document, executable=str(self.executable)
            )
        self.assertEqual(len(result["assignments"]), 16)
        self.assertEqual(
            len({record["channel"] for record in result["assignments"]}), 16
        )
        self.assertEqual(result["metrics"]["certificate_disagreements"], 0)
        self.assertEqual(result["metrics"]["certified_matchings"], 3)
        self.assertLess(candidate_cost.call_count, 100)

    def test_invalid_parallel_bank_worker_override_is_rejected(self) -> None:
        with mock.patch.dict(
            "os.environ", {"EMUFLOW_CHIMEW_BANK_WORKERS": "invalid"}
        ):
            with self.assertRaisesRegex(EmuFlowError, "must be an integer"):
                evaluate_chimew_bank_channel_assignment(
                    self.document, executable=str(self.executable)
                )

    def test_normalized_coordinates_and_bad_provenance_are_rejected(self) -> None:
        normalized = copy.deepcopy(self.document)
        normalized["coordinate_system"] = "normalized-xy"
        with self.assertRaisesRegex(ValidationError, "normalized"):
            validate_chimew_bank_channel_input(normalized)
        opaque = copy.deepcopy(self.document)
        opaque["provenance"]["architecture_sha256"] = "opaque"
        with self.assertRaisesRegex(ValidationError, "SHA-256"):
            validate_chimew_bank_channel_input(opaque)

    def test_capacity_and_common_signal_contracts_are_hard(self) -> None:
        insufficient = copy.deepcopy(self.document)
        insufficient["bank_pairs"][0]["channels"] = insufficient["bank_pairs"][0][
            "channels"
        ][:2]
        insufficient["metrics"]["channels"] = 3
        with self.assertRaisesRegex(ValidationError, "insufficient"):
            validate_chimew_bank_channel_input(insufficient)
        nonsingleton = copy.deepcopy(self.document)
        nonsingleton["groups"][2]["members"].append(member("extra", 40.0, 40.0))
        with self.assertRaisesRegex(ValidationError, "singleton"):
            validate_chimew_bank_channel_input(nonsingleton)

    def test_residual_dual_tampering_is_rejected(self) -> None:
        with self.assertRaisesRegex(EmuFlowError, "negative reduced cost"):
            _verify_certificate(
                1,
                [1],
                1,
                [(0, 0, 5)],
                {0: (0, 5)},
                [0, 0, 0, 0],
                5,
            )

    def test_compact_repeated_row_certificate_rejects_tampering(self) -> None:
        bank = {
            "channels": [
                {"pin_a": (0.0, 0.0), "pin_b": (100.0, 0.0)},
                {"pin_a": (0.0, 20.0), "pin_b": (100.0, 20.0)},
            ]
        }
        group = {
            "kind": 1,
            "direction": 0,
            "members": [
                {
                    "fanout": (0.0, 0.0),
                    "fanins": [(100.0, 0.0)],
                }
            ],
        }
        with self.assertRaisesRegex(EmuFlowError, "negative reduced cost"):
            _verify_stage2_certificate(
                bank,
                [group],
                [0],
                0,
                {0: (0, 0)},
                [0, 0, 0, 100_000, 100_000],
                0,
                1000,
            )

    def test_200_group_sparse_certificate_regression(self) -> None:
        document = copy.deepcopy(self.document)
        document["bank_pairs"] = []
        document["groups"] = []
        for bank_index in range(4):
            y = float(bank_index * 100)
            document["bank_pairs"].append(
                {
                    "id": f"bank{bank_index}",
                    "domain": "AB",
                    "bank_a": {"id": f"A{bank_index}", "x": 0.0, "y": y},
                    "bank_b": {"id": f"B{bank_index}", "x": 100.0, "y": y},
                    "channels": [
                        {
                            "id": f"bank{bank_index}_channel{channel}",
                            "order": channel,
                            "pin_a": {"x": 0.0, "y": y + channel / 100.0},
                            "pin_b": {"x": 100.0, "y": y + channel / 100.0},
                        }
                        for channel in range(50)
                    ],
                }
            )
            for signal in range(50):
                document["groups"].append(
                    {
                        "id": f"g{bank_index}_{signal}",
                        "domain": "AB",
                        "kind": "common_signal",
                        "direction": "a_to_b" if signal % 2 == 0 else "b_to_a",
                        "members": [
                            member(
                                f"s{bank_index}_{signal}",
                                y + signal / 100.0,
                                y + signal / 100.0,
                            )
                        ],
                    }
                )
        document["metrics"] = {
            "groups": 200,
            "signals": 200,
            "fanins": 200,
            "bank_pairs": 4,
            "channels": 200,
        }
        result = evaluate_chimew_bank_channel_assignment(
            document, executable=str(self.executable)
        )
        self.assertEqual(len(result["assignments"]), 200)
        self.assertEqual(len({row["channel"] for row in result["assignments"]}), 200)
        self.assertEqual(result["metrics"]["certified_matchings"], 9)

    def test_timing_weight_prioritizes_the_critical_signal(self) -> None:
        document = copy.deepcopy(self.document)
        document["bank_pairs"] = [document["bank_pairs"][0]]
        document["bank_pairs"][0]["channels"] = [
            {
                "id": "low",
                "order": 0,
                "pin_a": {"x": 0.0, "y": 0.0},
                "pin_b": {"x": 100.0, "y": 0.0},
            },
            {
                "id": "high",
                "order": 1,
                "pin_a": {"x": 0.0, "y": 100.0},
                "pin_b": {"x": 100.0, "y": 100.0},
            },
        ]
        document["groups"] = [
            {
                "id": "noncritical",
                "domain": "AB",
                "kind": "tdm_group",
                "direction": "a_to_b",
                "members": [member("n", 0.0, 0.0)],
            },
            {
                "id": "critical",
                "domain": "AB",
                "kind": "tdm_group",
                "direction": "a_to_b",
                "members": [member("c", 10.0, 10.0, timing_weight=10.0)],
            },
        ]
        document["metrics"] = {
            "groups": 2,
            "signals": 2,
            "fanins": 2,
            "bank_pairs": 1,
            "channels": 2,
        }
        result = evaluate_chimew_bank_channel_assignment(
            document, executable=str(self.executable)
        )
        assignment = {
            record["group"]: record["channel"]
            for record in result["assignments"]
        }
        self.assertEqual(assignment["critical"], "low")
        self.assertEqual(assignment["noncritical"], "high")


if __name__ == "__main__":
    unittest.main()
