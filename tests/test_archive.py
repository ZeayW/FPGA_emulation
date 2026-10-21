import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import emuflow.archive as archive_module
from emuflow.archive import (
    cleanup_validation_source,
    create_validation_archive,
    validate_validation_archive,
)
from emuflow.cli import main
from emuflow.errors import EmuFlowError, ValidationError
from emuflow.io import read_json, write_json
from emuflow.multi_fpga_flow import run_multi_fpga_flow
from emuflow.xilinx_openparf_atomic import (
    write_xilinx_openparf_atomic_placement_certificate,
)
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
        mapping_profile="vtr-hard-blocks",
    )


class ValidationArchiveTest(unittest.TestCase):
    def test_archive_rollback_accepts_already_absent_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing = Path(temporary_directory) / "missing"
            archive_module._rollback_published_directory(missing, (1, 1))
            self.assertFalse(missing.exists())

    def test_archive_rollback_never_removes_foreign_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            archive = Path(temporary_directory) / "archive"
            archive.mkdir()
            (archive / "foreign").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(EmuFlowError, "restored a foreign"):
                archive_module._rollback_published_directory(archive, (0, 0))
            self.assertEqual(
                (archive / "foreign").read_text(encoding="utf-8"), "keep"
            )

    def test_archive_rollback_isolates_before_identity_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            archive = root / "archive"
            archive.mkdir()
            (archive / "owned").write_text("owned", encoding="utf-8")
            stat = archive.stat()
            identity = (stat.st_dev, stat.st_ino)
            foreign = root / "foreign"
            foreign.mkdir()
            (foreign / "value").write_text("foreign", encoding="utf-8")
            owned_recovery = root / "owned-recovery"
            original_replace = archive_module.os.replace
            attacked = False

            def swap_before_isolation(source: Path, target: Path) -> None:
                nonlocal attacked
                if not attacked and Path(source) == archive:
                    attacked = True
                    original_replace(archive, owned_recovery)
                    original_replace(foreign, archive)
                original_replace(source, target)

            with patch.object(
                archive_module.os,
                "replace",
                side_effect=swap_before_isolation,
            ), self.assertRaisesRegex(EmuFlowError, "restored a foreign"):
                archive_module._rollback_published_directory(archive, identity)
            self.assertTrue(attacked)
            self.assertEqual(
                (archive / "value").read_text(encoding="utf-8"), "foreign"
            )
            self.assertEqual(
                (owned_recovery / "owned").read_text(encoding="utf-8"),
                "owned",
            )

    def test_archive_rollback_preserves_two_foreign_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            archive = root / "archive"
            archive.mkdir()
            (archive / "owned").write_text("owned", encoding="utf-8")
            stat = archive.stat()
            identity = (stat.st_dev, stat.st_ino)
            isolated_foreign = root / "isolated-foreign"
            isolated_foreign.mkdir()
            (isolated_foreign / "value").write_text(
                "isolated", encoding="utf-8"
            )
            new_target = root / "new-target"
            new_target.mkdir()
            (new_target / "value").write_text("target", encoding="utf-8")
            owned_recovery = root / "owned-recovery"
            original_replace = archive_module.os.replace
            attacked = False

            def occupy_after_isolation(source: Path, target: Path) -> None:
                nonlocal attacked
                if not attacked and Path(source) == archive:
                    attacked = True
                    original_replace(archive, owned_recovery)
                    original_replace(isolated_foreign, archive)
                    original_replace(archive, target)
                    original_replace(new_target, archive)
                    return
                original_replace(source, target)

            with patch.object(
                archive_module.os,
                "replace",
                side_effect=occupy_after_isolation,
            ), self.assertRaisesRegex(
                EmuFlowError, "foreign isolated output"
            ) as raised:
                archive_module._rollback_published_directory(archive, identity)
            self.assertTrue(attacked)
            self.assertEqual(
                (archive / "value").read_text(encoding="utf-8"), "target"
            )
            quarantines = list(root.glob(".archive.rollback-*"))
            self.assertEqual(len(quarantines), 1)
            self.assertIn(str(quarantines[0]), str(raised.exception))
            self.assertEqual(
                (quarantines[0] / "value").read_text(encoding="utf-8"),
                "isolated",
            )
            self.assertEqual(
                (owned_recovery / "owned").read_text(encoding="utf-8"),
                "owned",
            )

    def test_archive_fsync_failure_keeps_old_namespace_unpublished(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            flow = root / "flow"
            archive = root / "archive"
            _run_small_flow(flow)
            with patch.object(
                archive_module,
                "_fsync_tree_directories",
                side_effect=OSError("injected fsync failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected"):
                    create_validation_archive(
                        flow, archive, run_id="fsync-failure"
                    )
            self.assertFalse(archive.exists())
            self.assertFalse(
                archive.with_name(
                    f".{archive.name}.creating-{archive_module.os.getpid()}"
                ).exists()
            )

    def test_archive_parent_fsync_failure_rolls_back_and_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            flow = root / "flow"
            archive = root / "archive"
            _run_small_flow(flow)
            original_fsync = archive_module._fsync_directory
            injected = False

            def fail_after_rename(path: Path) -> None:
                nonlocal injected
                if (
                    not injected
                    and path.resolve() == archive.parent.resolve()
                    and archive.is_dir()
                ):
                    injected = True
                    raise OSError("injected post-rename parent fsync failure")
                original_fsync(path)

            with patch.object(
                archive_module, "_fsync_directory", side_effect=fail_after_rename
            ):
                with self.assertRaisesRegex(OSError, "post-rename"):
                    create_validation_archive(
                        flow, archive, run_id="post-rename-failure"
                    )
            self.assertTrue(injected)
            self.assertFalse(archive.exists())
            self.assertFalse(any(root.glob(".archive.rollback-*")))
            result = create_validation_archive(
                flow, archive, run_id="post-rename-retry"
            )
            self.assertEqual(result["status"], "pass")

    def test_physical_summary_and_typed_logic_generation_are_one_group(
        self,
    ) -> None:
        from emuflow.logic_segment_timing_storage import (
            logic_segment_timing_reference,
            write_logic_segment_timing_storage,
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            flow = Path(temporary_directory).resolve()
            selector = flow / "physical/fpga0/logic-segment-timing.json"
            header = {
                "schema": "emuflow.logic-segment-timing/v1",
                "status": "pass", "design": "dut", "platform": "board",
                "fpga": "fpga0", "provider": "test",
                "qualification": "endpoint-chain", "coverage": {},
                "system_routes_sha256": "a" * 64,
                "tdm_schedule_sha256": "b" * 64,
            }
            record = {
                "id": "segment0", "kind": "capture",
                "system_path": "path0", "member_path": "path0",
                "cut_index": 0, "fpga": "fpga0",
                "replace_tx_endpoint": None, "start_pin": "a",
                "end_pin": "b", "delay_ns": 1.0,
            }
            view = write_logic_segment_timing_storage(
                selector, header=header, segments=(record,)
            )
            generation = view.store_path
            view.close()
            summary = flow / "physical/physical-summary.json"
            write_json(summary, {
                "schema": "emuflow.phase7b-physical-summary/v1",
                "design": "dut", "platform": "board",
                "logic_segment_system_routes_sha256": "a" * 64,
                "logic_segment_tdm_schedule_sha256": "b" * 64,
                "logic_segment_timing": {
                    "fpga0": logic_segment_timing_reference(
                        selector, relative_to=summary.parent
                    )
                },
            })
            candidates = {
                path.relative_to(flow): {"test"}
                for path in (summary, selector, generation)
            }
            groups = archive_module._atomic_retention_groups(flow, candidates)
            self.assertEqual(groups[summary.relative_to(flow)], frozenset(candidates))
            archive_module._validate_json(summary)
            generation.write_bytes(generation.read_bytes() + b"tamper")
            with self.assertRaisesRegex(ValidationError, "seal"):
                archive_module._validate_json(summary)

    def test_archive_rejects_ambiguous_sidecar_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            flow = Path(temporary_directory).resolve()
            store = flow / "certificate.deadbeef.sqlite"
            store.write_bytes(b"typed store")
            digest = hashlib.sha256(store.read_bytes()).hexdigest()
            selector = flow / "placement-certificate.json"
            selector.write_text(json.dumps({
                "artifact": store.name,
                "store_sha256": digest,
                "store_bytes": store.stat().st_size,
                "cluster_storage": {
                    "file": "decoy.sqlite",
                    "sha256": "0" * 64,
                    "bytes": 0,
                },
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "ambiguous"):
                archive_module._selector_sidecar(
                    flow, Path(selector.name)
                )

    def test_archive_keeps_placement_certificate_selector_and_store_atomic(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            flow = root / "flow"
            archive = root / "archive"
            _run_small_flow(flow)
            selector_path = flow / "physical" / "placement-certificate.json"
            certificate = {
                "schema": "emuflow.openparf-atomic-placement/v2",
                "clusters": [{
                    "cluster": "openparf:SLICE_X0Y0",
                    "site": "SLICE_X0Y0",
                    "site_type": "SLICEL",
                    "x": 0,
                    "y": 0,
                    "assignments": [{
                        "instance": "u0",
                        "cell_type": "LUT6",
                        "bel": "A6LUT",
                        "physical_site": "SLICE_X0Y0",
                        "placement_mode": "SLICEL",
                        "source_cluster": "atomic-source:u0",
                        "bram_tile_group": None,
                    }],
                }],
            }
            selector = write_xilinx_openparf_atomic_placement_certificate(
                selector_path, certificate
            )
            store_path = selector_path.with_name(
                selector["cluster_storage"]["file"]
            )
            threshold = (
                selector_path.stat().st_size + store_path.stat().st_size
            ) // 2
            create_validation_archive(
                flow, archive, run_id="atomic-placement-certificate",
                max_copy_bytes=threshold,
            )
            manifest = read_json(archive / "archive-manifest.json")
            records = {
                item["source_path"]: item for item in manifest["files"]
            }
            selector_record = records[
                "physical/placement-certificate.json"
            ]
            store_record = records[f"physical/{store_path.name}"]
            self.assertEqual(selector_record["retention"], "hash-only")
            self.assertEqual(store_record["retention"], "hash-only")

    def test_archive_keeps_selector_sidecar_retention_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            flow = root / "flow"
            archive = root / "archive"
            _run_small_flow(flow)

            selector_path = flow / "system-route/routes.json"
            selector = json.loads(selector_path.read_text(encoding="utf-8"))
            sidecar_path = selector_path.with_name(selector["artifact"])
            self.assertLess(
                selector_path.stat().st_size, sidecar_path.stat().st_size
            )
            threshold = (
                selector_path.stat().st_size + sidecar_path.stat().st_size
            ) // 2

            create_validation_archive(
                flow,
                archive,
                run_id="atomic-selector-sidecar",
                max_copy_bytes=threshold,
            )
            manifest = read_json(archive / "archive-manifest.json")
            records = {
                item["source_path"]: item for item in manifest["files"]
            }
            selector_record = records["system-route/routes.json"]
            sidecar_record = records[
                f"system-route/{sidecar_path.name}"
            ]
            self.assertEqual(selector_record["retention"], "hash-only")
            self.assertEqual(sidecar_record["retention"], "hash-only")
            self.assertIsNone(selector_record["archive_path"])
            self.assertIsNone(sidecar_record["archive_path"])
            checked_selectors = 0
            for source_path, record in records.items():
                source = flow / source_path
                if source.suffix != ".json":
                    continue
                value = json.loads(source.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or not isinstance(
                    value.get("artifact"), str
                ) or not isinstance(value.get("store_sha256"), str):
                    continue
                dependency = (
                    source.parent / value["artifact"]
                ).resolve().relative_to(flow.resolve()).as_posix()
                self.assertEqual(
                    record["retention"], records[dependency]["retention"]
                )
                checked_selectors += 1
            self.assertGreaterEqual(checked_selectors, 2)
            self.assertEqual(
                manifest["retention_policy"]["selector_sidecars"],
                "atomic-retention-groups",
            )
            self.assertEqual(
                validate_validation_archive(archive)["status"], "pass"
            )

    def test_archive_follows_schedule_manifest_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            flow = root / "flow"
            archive = root / "archive"
            _run_small_flow(flow)

            schedule_path = flow / "tdm/schedule.json"
            schedule_manifest = json.loads(schedule_path.read_text())
            original_store = schedule_path.with_name(
                schedule_manifest["artifact"]
            )
            relocated_store = schedule_path.with_name(
                f"schedule.{schedule_manifest['store_sha256']}.sqlite"
            )
            original_store.rename(relocated_store)
            schedule_manifest["artifact"] = relocated_store.name
            write_json(schedule_path, schedule_manifest, compact=True)

            flow_report_path = flow / "multi-fpga-flow-report.json"
            flow_report = read_json(flow_report_path)
            flow_report["artifacts"]["schedule"]["sha256"] = hashlib.sha256(
                schedule_path.read_bytes()
            ).hexdigest()
            flow_report["artifacts"]["schedule_store"] = {
                "path": f"tdm/{relocated_store.name}",
                "sha256": hashlib.sha256(
                    relocated_store.read_bytes()
                ).hexdigest(),
            }
            write_json(flow_report_path, flow_report)

            create_validation_archive(
                flow, archive, run_id="content-addressed-schedule"
            )
            archived = read_json(archive / "archive-manifest.json")
            schedule_store = next(
                item
                for item in archived["files"]
                if "schedule_store" in item["roles"]
            )
            self.assertEqual(
                schedule_store["source_path"],
                f"tdm/{relocated_store.name}",
            )
            self.assertEqual(
                validate_validation_archive(archive)["status"], "pass"
            )

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
                    "--mapping-profile",
                    "vtr-hard-blocks",
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
