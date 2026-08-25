import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from emuflow.cli import main
from emuflow.end_to_end_validation_matrix import (
    END_TO_END_VALIDATION_MATRIX_SCHEMA,
    canonical_end_to_end_matrix_sha256,
    load_end_to_end_validation_matrix,
    validate_end_to_end_validation_matrix,
)
from emuflow.errors import ValidationError


REPOSITORY = Path(__file__).resolve().parents[1]
MATRIX_PATH = REPOSITORY / "benchmarks/end_to_end_validation_matrix.json"


class EndToEndValidationMatrixTest(unittest.TestCase):
    def setUp(self) -> None:
        self.matrix, self.summary = load_end_to_end_validation_matrix(MATRIX_PATH)

    def test_checked_in_matrix_is_canonical_and_explicitly_planned(self) -> None:
        self.assertEqual(
            self.summary["schema"], END_TO_END_VALIDATION_MATRIX_SCHEMA
        )
        self.assertEqual(self.summary["case_count"], 3)
        self.assertEqual(
            self.summary["roles"],
            {"primary-qor": 1, "topology-replication": 2},
        )
        self.assertEqual(self.summary["states"], {"planned": 3})
        self.assertEqual(self.summary["workloads"], {"koios": 3})
        self.assertEqual(
            self.summary["matrix_sha256"],
            canonical_end_to_end_matrix_sha256(self.matrix),
        )

    def test_cli_emits_the_same_summary(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["benchmark-matrix-validate", str(MATRIX_PATH)])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), self.summary)

    def test_raw_contest_case_cannot_replace_real_rtl_workload(self) -> None:
        invalid = copy.deepcopy(self.matrix)
        invalid["cases"][0]["workload"]["contract"] = "contest-communication-graph"
        with self.assertRaisesRegex(ValidationError, "naturally connected"):
            validate_end_to_end_validation_matrix(invalid, REPOSITORY)

    def test_provider_seed_and_global_qor_policy_is_fixed(self) -> None:
        invalid = copy.deepcopy(self.matrix)
        invalid["policy"]["phase6_providers"] = ["chimew"]
        with self.assertRaisesRegex(ValidationError, "providers"):
            validate_end_to_end_validation_matrix(invalid, REPOSITORY)
        invalid = copy.deepcopy(self.matrix)
        invalid["policy"]["physical_seeds"] = [1, 2, 3]
        with self.assertRaisesRegex(ValidationError, "physical seeds"):
            validate_end_to_end_validation_matrix(invalid, REPOSITORY)
        invalid = copy.deepcopy(self.matrix)
        invalid["policy"]["primary_qor"] = ["per_fpga_wns_ns"]
        with self.assertRaisesRegex(ValidationError, "primary QoR"):
            validate_end_to_end_validation_matrix(invalid, REPOSITORY)

    def test_planned_is_not_qualified_evidence(self) -> None:
        invalid = copy.deepcopy(self.matrix)
        invalid["cases"][0]["evidence"] = [
            {"manifest_sha256": "a" * 64, "source_commit": "b" * 40}
        ]
        with self.assertRaisesRegex(ValidationError, "planned case"):
            validate_end_to_end_validation_matrix(invalid, REPOSITORY)
        invalid = copy.deepcopy(self.matrix)
        invalid["cases"][0]["state"] = "qualified"
        with self.assertRaisesRegex(ValidationError, "requires evidence"):
            validate_end_to_end_validation_matrix(invalid, REPOSITORY)
        invalid["cases"][0]["evidence"] = [
            {
                "manifest_sha256": "a" * 64,
                "source_commit": "b" * 40,
                "provider": "chimew",
                "physical_seed": 1,
            }
        ]
        with self.assertRaisesRegex(ValidationError, "every provider"):
            validate_end_to_end_validation_matrix(invalid, REPOSITORY)

    def test_paths_ids_sorting_and_boarddb_capability_are_checked(self) -> None:
        invalid = copy.deepcopy(self.matrix)
        invalid["cases"][0]["workload"]["run_spec"] = "../outside.json"
        with self.assertRaisesRegex(ValidationError, "repository-relative"):
            validate_end_to_end_validation_matrix(invalid, REPOSITORY)
        invalid = copy.deepcopy(self.matrix)
        invalid["cases"][0]["id"] = "ambiguous-case6"
        with self.assertRaisesRegex(ValidationError, "<workload>__<contest-case>"):
            validate_end_to_end_validation_matrix(invalid, REPOSITORY)
        invalid = copy.deepcopy(self.matrix)
        invalid["cases"][0], invalid["cases"][1] = (
            invalid["cases"][1],
            invalid["cases"][0],
        )
        with self.assertRaisesRegex(ValidationError, "sorted"):
            validate_end_to_end_validation_matrix(invalid, REPOSITORY)


if __name__ == "__main__":
    unittest.main()
