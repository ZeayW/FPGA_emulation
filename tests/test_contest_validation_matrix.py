import copy
import io
import json
import runpy
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from emuflow.contest_validation_matrix import (
    CONTEST_VALIDATION_MATRIX_SCHEMA,
    canonical_matrix_sha256,
    case_keys,
    load_contest_validation_matrix,
    validate_contest_validation_matrix,
)
from emuflow.cli import main
from emuflow.errors import ValidationError


REPOSITORY = Path(__file__).resolve().parents[1]
MATRIX_PATH = REPOSITORY / "benchmarks/contest_validation_matrix.json"


def _fetch_catalog(script: str):
    return runpy.run_path(str(REPOSITORY / script))


class ContestValidationMatrixTest(unittest.TestCase):
    def setUp(self):
        self.matrix, self.summary = load_contest_validation_matrix(MATRIX_PATH)

    def test_checked_in_matrix_is_valid_and_deterministic(self):
        self.assertEqual(self.summary["schema"], CONTEST_VALIDATION_MATRIX_SCHEMA)
        self.assertEqual(self.summary["case_count"], 15)
        self.assertEqual(
            self.summary["suites"],
            {
                "eda2023": 10,
                "eda2024-repart": 4,
                "iccad2019": 1,
            },
        )
        self.assertEqual(
            self.summary["matrix_sha256"], canonical_matrix_sha256(self.matrix)
        )

    def test_cli_emits_the_sealed_summary(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["contest", "matrix-validate", str(MATRIX_PATH)])
        self.assertEqual(status, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report, self.summary)

    def test_matrix_exactly_covers_hash_pinned_fetch_catalogs(self):
        suites = {
            "eda2023": "scripts/fetch_eda2023_benchmarks.py",
            "eda2024-repart": "scripts/fetch_repart_benchmarks.py",
        }
        records = {record["id"]: record for record in self.matrix["cases"]}
        for suite, script in suites.items():
            catalog = _fetch_catalog(script)
            self.assertEqual(
                set(case_keys(self.matrix, suite)), set(catalog["CASES"])
            )
            for case, files in catalog["CASES"].items():
                record = records[f"{suite}.{case}"]
                expected_bytes = (
                    sum(value[1] for value in files.values())
                    if isinstance(files, dict)
                    else sum(value[-1] for value in files)
                )
                self.assertEqual(record["input_bytes"], expected_bytes)
                self.assertTrue((REPOSITORY / record["source"]["fetcher"]).is_file())
                expected_revision = catalog["COMMIT"]
                if suite == "eda2023" and case == "case10":
                    expected_revision = catalog["OFFICIAL_ARCHIVE_SHA256"]
                self.assertEqual(record["source"]["revision"], expected_revision)

    def test_embedded_sample_has_real_evidence_and_locator(self):
        record = next(
            item for item in self.matrix["cases"] if item["suite"] == "iccad2019"
        )
        self.assertEqual(record["qualification"], "adapter-regression")
        self.assertTrue((REPOSITORY / record["source"]["locator"]).is_file())
        self.assertEqual(record["evidence"], [record["source"]["locator"]])

    def test_duplicate_or_unsorted_cases_are_rejected(self):
        duplicate = copy.deepcopy(self.matrix)
        duplicate["cases"].append(copy.deepcopy(duplicate["cases"][-1]))
        with self.assertRaisesRegex(ValidationError, "unique"):
            validate_contest_validation_matrix(duplicate)
        unsorted = copy.deepcopy(self.matrix)
        unsorted["cases"][0], unsorted["cases"][1] = (
            unsorted["cases"][1],
            unsorted["cases"][0],
        )
        with self.assertRaisesRegex(ValidationError, "sorted"):
            validate_contest_validation_matrix(unsorted)

    def test_retired_topology_contest_suite_is_rejected(self):
        invalid = copy.deepcopy(self.matrix)
        invalid["cases"][0]["suite"] = "eda2025"
        with self.assertRaisesRegex(ValidationError, "suite"):
            validate_contest_validation_matrix(invalid)

    def test_qualified_case_requires_evidence(self):
        invalid = copy.deepcopy(self.matrix)
        invalid["cases"][0]["qualification"] = "case-validated"
        with self.assertRaisesRegex(ValidationError, "require evidence"):
            validate_contest_validation_matrix(invalid)

    def test_gate_order_and_source_revision_are_strict(self):
        invalid_gate = copy.deepcopy(self.matrix)
        invalid_gate["cases"][0]["target_gates"] = ["phase4", "import"]
        with self.assertRaisesRegex(ValidationError, "flow order"):
            validate_contest_validation_matrix(invalid_gate)
        invalid_revision = copy.deepcopy(self.matrix)
        invalid_revision["cases"][0]["source"]["revision"] = "deadbeef"
        with self.assertRaisesRegex(ValidationError, "does not match"):
            validate_contest_validation_matrix(invalid_revision)


if __name__ == "__main__":
    unittest.main()
