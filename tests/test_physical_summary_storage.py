import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from emuflow.errors import ValidationError
from emuflow.io import read_json, write_json
from emuflow.physical_summary_storage import (
    PHYSICAL_EVIDENCE_REFERENCE_SCHEMA,
    canonical_physical_summary_sha256,
    physical_evidence_reference,
    read_physical_summary,
)


class PhysicalSummaryStorageTest(unittest.TestCase):
    def test_evidence_is_resolved_lazily_and_sealed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "fpga0/boundary.json"
            write_json(
                evidence,
                {
                    "schema": "emuflow.boundary-identity/v1",
                    "status": "pass",
                    "endpoints": [{"id": "endpoint0"}],
                },
            )
            summary_path = root / "physical-summary.json"
            reference = physical_evidence_reference(
                evidence,
                "emuflow.boundary-identity/v1",
                relative_to=root,
            )
            write_json(
                summary_path,
                {
                    "schema": "emuflow.physical-summary/v1",
                    "boundary_identities": {"fpga0": reference},
                },
            )

            raw = read_json(summary_path)
            self.assertEqual(
                raw["boundary_identities"]["fpga0"]["schema"],
                PHYSICAL_EVIDENCE_REFERENCE_SCHEMA,
            )
            self.assertNotIn(
                "endpoints", raw["boundary_identities"]["fpga0"]
            )
            lazy = read_physical_summary(summary_path)
            self.assertEqual(
                lazy["boundary_identities"]["fpga0"]["endpoints"],
                [{"id": "endpoint0"}],
            )

            evidence.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "byte count|digest"):
                lazy["boundary_identities"]["fpga0"]

    def test_one_fpga_is_resolved_without_reading_other_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            references = {}
            inline = {}
            for fpga in ("fpga0", "fpga1"):
                document = {
                    "schema": "emuflow.boundary-identity/v1",
                    "fpga": fpga,
                    "endpoints": [{"id": f"{fpga}-endpoint"}],
                }
                evidence = root / fpga / "boundary.json"
                write_json(evidence, document)
                references[fpga] = physical_evidence_reference(
                    evidence,
                    document["schema"],
                    relative_to=root,
                )
                inline[fpga] = document
            summary_path = root / "physical-summary.json"
            bounded = {
                "schema": "emuflow.physical-summary/v1",
                "boundary_identities": references,
            }
            write_json(summary_path, bounded)
            lazy = read_physical_summary(summary_path)
            (root / "fpga1/boundary.json").write_text(
                "{}\n", encoding="utf-8"
            )

            self.assertEqual(
                lazy["boundary_identities"]["fpga0"]["fpga"], "fpga0"
            )
            with self.assertRaisesRegex(ValidationError, "byte count|digest"):
                lazy["boundary_identities"]["fpga1"]

            (root / "fpga1/boundary.json").unlink()
            write_json(
                root / "fpga1/boundary.json",
                inline["fpga1"],
            )
            expected = {
                "schema": "emuflow.physical-summary/v1",
                "boundary_identities": inline,
            }
            expected_digest = hashlib.sha256(
                json.dumps(
                    expected,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            self.assertEqual(
                canonical_physical_summary_sha256(lazy), expected_digest
            )

    def test_unsafe_reference_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(
                root / "physical-summary.json",
                {
                    "boundary_identities": {
                        "fpga0": {
                            "schema": PHYSICAL_EVIDENCE_REFERENCE_SCHEMA,
                            "logical_schema": "emuflow.boundary-identity/v1",
                            "path": "../outside.json",
                            "bytes": 1,
                            "sha256": "0" * 64,
                        }
                    }
                },
            )
            with self.assertRaisesRegex(ValidationError, "unsafe"):
                read_physical_summary(root / "physical-summary.json")

    def test_unknown_reference_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(
                root / "physical-summary.json",
                {
                    "boundary_identities": {
                        "fpga0": {
                            "schema": "emuflow.physical-evidence-reference/v2",
                            "logical_schema": "emuflow.boundary-identity/v1",
                            "path": "fpga0/boundary.json",
                            "bytes": 1,
                            "sha256": "0" * 64,
                        }
                    }
                },
            )
            with self.assertRaisesRegex(ValidationError, "unsupported"):
                read_physical_summary(root / "physical-summary.json")

    def test_mixed_inline_and_referenced_evidence_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "identity.json"
            write_json(
                evidence,
                {"schema": "emuflow.boundary-identity/v1"},
            )
            write_json(
                root / "physical-summary.json",
                {
                    "boundary_identities": {
                        "fpga0": physical_evidence_reference(
                            evidence,
                            "emuflow.boundary-identity/v1",
                            relative_to=root,
                        ),
                        "fpga1": {
                            "schema": "emuflow.boundary-identity/v1"
                        },
                    }
                },
            )
            with self.assertRaisesRegex(ValidationError, "mixes"):
                read_physical_summary(root / "physical-summary.json")


if __name__ == "__main__":
    unittest.main()
