import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from emuflow.archive import (
    cleanup_validation_source,
    create_validation_archive,
    validate_validation_archive,
)
from emuflow.cli import main
from emuflow.errors import EmuFlowError, ValidationError
from emuflow.io import read_json, write_json
from emuflow.multi_fpga_flow import run_multi_fpga_flow
from tests.native_build import tlr_router


ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / "platforms/virtual/academic_vtr_2fpga_p2p.json"
FAKE_OPENSTA = ROOT / "tests/fixtures/fake_opensta_paths.py"


def _run_small_flow(output: Path) -> None:
    run_multi_fpga_flow(
        platform_path=PLATFORM,
        output_dir=output,
        yosys_json=ROOT / "examples/yosys/counter.json",
        top="counter",
        clocks=["clk"],
        partition_provider="greedy",
        cut_mode="sequential-only",
        timing_driven=False,
        clock_periods={"clk": 10.0},
        opensta=str(FAKE_OPENSTA),
        router=str(tlr_router()),
        frame_slots=32,
        equivalence_cycles=2,
    )


class ValidationArchiveTest(unittest.TestCase):
    def test_create_validate_and_guarded_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            flow = root / "flow"
            archive = root / "archive"
            _run_small_flow(flow)
            nested = flow / "physical/fpga0/partition.route"
            nested.parent.mkdir(parents=True)
            nested.write_bytes(b"nested physical evidence")
            flow_report_path = flow / "multi-fpga-flow-report.json"
            flow_report = read_json(flow_report_path)
            flow_report["nested_test_evidence"] = {
                "path": str(nested),
                "sha256": hashlib.sha256(nested.read_bytes()).hexdigest(),
            }
            flow_report["pruned_test_evidence"] = {
                "path": str(flow / "physical/fpga0/rr_graph.xml"),
                "bytes": 123456,
                "sha256": "a" * 64,
                "retained": False,
            }
            write_json(flow_report_path, flow_report)

            result = create_validation_archive(
                flow,
                archive,
                run_id="counter-full-flow",
                source_commit="0123456789abcdef",
                max_copy_bytes=1,
                tool_versions={"router": "test-build"},
            )
            self.assertEqual(result["status"], "pass")
            self.assertGreater(result["hash_only_files"], 0)
            self.assertTrue(
                (archive / "files/multi-fpga-flow-report.json").is_file()
            )
            manifest = json.loads(
                (archive / "archive-manifest.json").read_text()
            )
            self.assertEqual(
                manifest["environment"]["tools"]["router"], "test-build"
            )
            nested_record = next(
                item
                for item in manifest["files"]
                if item["source_path"]
                == "physical/fpga0/partition.route"
            )
            self.assertEqual(nested_record["retention"], "hash-only")
            self.assertTrue(
                any(
                    role.startswith("reported:")
                    for role in nested_record["roles"]
                )
            )
            self.assertEqual(
                manifest["source"]["pruned_artifacts"][0]["status"],
                "intentionally-pruned",
            )
            self.assertEqual(
                validate_validation_archive(archive)["flow"]["instances"], 8
            )

            with self.assertRaisesRegex(ValidationError, "non-replayable"):
                cleanup_validation_source(archive, flow)
            self.assertTrue(flow.exists())

    def test_tampering_blocks_validation_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            flow = root / "flow"
            archive = root / "archive"
            _run_small_flow(flow)
            create_validation_archive(flow, archive, run_id="tamper-test")

            archived_report = archive / "files/multi-fpga-flow-report.json"
            archived_report.write_text(
                archived_report.read_text() + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "integrity check"):
                validate_validation_archive(archive)
            with self.assertRaises(ValidationError):
                cleanup_validation_source(archive, flow)
            self.assertTrue(flow.is_dir())

    def test_rejects_nested_archive_and_changed_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            flow = root / "flow"
            _run_small_flow(flow)
            with self.assertRaisesRegex(EmuFlowError, "non-nested"):
                create_validation_archive(
                    flow, flow / "archive", run_id="nested"
                )

            archive = root / "archive"
            create_validation_archive(flow, archive, run_id="changed")
            assignment = flow / "partition/assignment.json"
            assignment.write_text(
                assignment.read_text() + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValidationError, "changed"):
                cleanup_validation_source(archive, flow)
            self.assertTrue(flow.is_dir())

    def test_compile_can_archive_and_cleanup_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            flow = root / "automatic-flow"
            archive = root / "automatic-archive"
            status = main(
                [
                    "multi-fpga",
                    "compile",
                    "--yosys-json",
                    str(ROOT / "examples/yosys/counter.json"),
                    "--top",
                    "counter",
                    "--clock",
                    "clk",
                    "--no-timing-driven",
                    "--clock-period",
                    "clk=10",
                    "--opensta",
                    str(FAKE_OPENSTA),
                    "--platform",
                    str(PLATFORM),
                    "--partition-provider",
                    "greedy",
                    "--router",
                    str(tlr_router()),
                    "--frame-slots",
                    "32",
                    "--equivalence-cycles",
                    "2",
                    "--out",
                    str(flow),
                    "--archive-out",
                    str(archive),
                    "--archive-run-id",
                    "automatic",
                    "--archive-cleanup",
                ]
            )
            self.assertEqual(status, 0)
            self.assertFalse(flow.exists())
            self.assertEqual(
                validate_validation_archive(archive)["run_id"], "automatic"
            )


if __name__ == "__main__":
    unittest.main()
