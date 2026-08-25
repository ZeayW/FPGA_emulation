import hashlib
import fcntl
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emuflow.errors import ValidationError
from emuflow.experiment_dag import plan_experiment, run_experiment_node
from emuflow.experiment_store import (
    apply_legacy_run_retirement,
    apply_experiment_gc,
    create_experiment_evidence_bundle,
    inventory_experiment_store,
    plan_experiment_gc,
    plan_legacy_run_migration,
    plan_legacy_run_retirement,
    validate_experiment_evidence_bundle,
)
from emuflow.io import write_json


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _closure(label: str) -> dict:
    files = [{"path": f"{label}.py", "bytes": 1, "sha256": _digest(label)}]
    identity = {
        "schema": "emuflow.experiment-implementation-identity/v1",
        "files": files,
    }
    return {
        "schema": "emuflow.experiment-implementation-closure/v1",
        "status": "pass",
        "components": [f"{label}.py"],
        "files": files,
        "implementation_sha256": hashlib.sha256(
            json.dumps(identity, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest(),
    }


def _spec() -> dict:
    nodes = []
    for index in range(1, 4):
        node_id = f"phase{index}"
        artifact = f"{node_id}.json"
        dependencies = [] if index == 1 else [f"phase{index - 1}"]
        dependency_tokens = [f"{{dependency:{item}}}" for item in dependencies]
        nodes.append(
            {
                "id": node_id,
                "stage": node_id,
                "dependencies": dependencies,
                "inputs": {"source": _digest("source")} if index == 1 else {},
                "configuration": {},
                "implementation": _closure(f"impl{index}"),
                "command": [
                    sys.executable,
                    "-c",
                    "import pathlib,sys; pathlib.Path(sys.argv[1]).mkdir(parents=True,exist_ok=True); pathlib.Path(sys.argv[1],sys.argv[2]).write_text('ok')",
                    "{output_dir}",
                    artifact,
                    *dependency_tokens,
                ],
                "validator_implementation": _closure(f"validator{index}"),
                "validator": [
                    sys.executable,
                    "-c",
                    "import pathlib,sys; raise SystemExit(0 if pathlib.Path(sys.argv[1],sys.argv[2]).is_file() else 1)",
                    "{artifact_root}",
                    artifact,
                    *dependency_tokens,
                ],
                "environment": {},
                "storage_estimate": {"peak_bytes": 1024, "retained_bytes": 128},
                "artifacts": [{"path": artifact, "role": "consumer-checkpoint"}],
            }
        )
    return {
        "schema": "emuflow.experiment-dag-spec/v2",
        "experiment_id": "store-test",
        "source_commit": "1" * 40,
        "nodes": nodes,
    }


class ExperimentStoreTest(unittest.TestCase):
    def test_evidence_preserves_and_rechecks_portable_execution_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            value = _spec()
            for node in value["nodes"]:
                node["inputs"]["tool.python"] = _digest("python-runtime")
                node["execution_bindings"] = {"tool.python": sys.executable}
                node["command_identity"] = [
                    "{input:tool.python}", *node["command"][1:]
                ]
                node["validator_identity"] = [
                    "{input:tool.python}", *node["validator"][1:]
                ]
            spec_path = root / "spec.json"
            write_json(spec_path, value)
            for node_id in ("phase1", "phase2", "phase3"):
                plan_path = root / f"{node_id}.plan.json"
                plan_experiment(spec_path, cache, plan_path)
                run_experiment_node(plan_path, node_id, root / f"attempt-{node_id}")
            final_plan = root / "final.plan.json"
            plan_experiment(spec_path, cache, final_plan)
            evidence_root = root / "evidence"
            self.assertEqual(
                create_experiment_evidence_bundle(
                    final_plan, ["phase3"], evidence_root
                )["status"],
                "pass",
            )

            for path in evidence_root.rglob("*"):
                path.chmod(0o755 if path.is_dir() else 0o644)
            evidence_root.chmod(0o755)
            manifest_path = evidence_root / "evidence-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["nodes"][0]["contract"]["command"][0] = "/forged/python"
            write_json(manifest_path, manifest)
            write_json(
                evidence_root / "evidence-seal.json",
                {
                    "schema": "emuflow.experiment-evidence-seal/v1",
                    "status": "sealed",
                    "manifest_sha256": hashlib.sha256(
                        manifest_path.read_bytes()
                    ).hexdigest(),
                },
            )
            with self.assertRaisesRegex(
                ValidationError, "portable execution identity is broken"
            ):
                validate_experiment_evidence_bundle(evidence_root)

    def test_inventory_and_self_contained_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            spec_path = root / "spec.json"
            write_json(spec_path, _spec())
            for node_id in ("phase1", "phase2", "phase3"):
                plan_path = root / f"{node_id}.plan.json"
                plan_experiment(spec_path, cache, plan_path)
                run_experiment_node(plan_path, node_id, root / f"attempt-{node_id}")
            inventory = inventory_experiment_store(cache)
            self.assertEqual(inventory["counts"], {"valid": 3, "invalid": 0})
            plan_path = root / "final.plan.json"
            plan_experiment(spec_path, cache, plan_path)
            evidence_root = root / "evidence"
            evidence = create_experiment_evidence_bundle(
                plan_path, ["phase3"], evidence_root
            )
            self.assertEqual(evidence["nodes"], 3)
            self.assertEqual(evidence["retained_artifacts"], 3)
            # The bundle remains valid after removing the cache, proving that it
            # is not merely a path-based reference to checkpoint objects.
            for path in cache.rglob("*"):
                if path.is_file():
                    path.chmod(0o644)
                elif path.is_dir():
                    path.chmod(0o755)
            import shutil

            shutil.rmtree(cache)
            self.assertEqual(
                validate_experiment_evidence_bundle(evidence_root)["status"],
                "pass",
            )
            artifact = next(evidence_root.glob("checkpoints/*/phase1.json"))
            artifact.chmod(0o644)
            artifact.write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "seal is broken"):
                validate_experiment_evidence_bundle(evidence_root)

    def test_gc_requires_exact_approved_plan_and_preserves_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            spec_path = root / "spec.json"
            write_json(spec_path, _spec())
            plan_path = root / "plan.json"
            plan = plan_experiment(spec_path, cache, plan_path)
            run_experiment_node(plan_path, "phase1", root / "attempt")
            protected = cache / "objects" / plan["nodes"][0]["execution_key"]
            failure = cache / "failures/old-attempt"
            failure.mkdir(parents=True)
            (failure / "stderr.log").write_text("failed", encoding="utf-8")
            final_plan_path = root / "final-plan.json"
            plan_experiment(spec_path, cache, final_plan_path)
            gc_path = root / "gc.json"
            gc = plan_experiment_gc(
                cache, [final_plan_path], gc_path, minimum_age_seconds=0
            )
            self.assertEqual(
                [item["path"] for item in gc["candidates"]],
                ["failures/old-attempt"],
            )
            with self.assertRaisesRegex(ValidationError, "approval seal"):
                apply_experiment_gc(gc_path, "0" * 64)
            approved = hashlib.sha256(gc_path.read_bytes()).hexdigest()
            receipt = apply_experiment_gc(gc_path, approved)
            self.assertEqual(receipt["removed_bytes"], 6)
            self.assertFalse(failure.exists())
            self.assertTrue(protected.exists())

    def test_gc_preserves_cache_local_output_aliases_from_imported_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            spec_path = root / "spec.json"
            write_json(spec_path, _spec())
            initial_plan_path = root / "initial-plan.json"
            initial_plan = plan_experiment(spec_path, cache, initial_plan_path)
            run_experiment_node(initial_plan_path, "phase1", root / "attempt")
            final_plan_path = root / "final-plan.json"
            final_plan = plan_experiment(spec_path, cache, final_plan_path)

            alias_key = "a" * 64
            alias_output = cache / "objects" / alias_key / "output"
            alias_output.mkdir(parents=True)
            (alias_output / "imported.bin").write_bytes(b"imported payload")
            unreferenced_key = "b" * 64
            unreferenced = cache / "objects" / unreferenced_key
            unreferenced.mkdir(parents=True)
            (unreferenced / "old.bin").write_bytes(b"old")
            final_plan["nodes"][0]["output_dir"] = str(alias_output.resolve())
            write_json(final_plan_path, final_plan)

            gc_path = root / "gc.json"
            gc = plan_experiment_gc(
                cache, [final_plan_path], gc_path, minimum_age_seconds=0
            )
            candidates = {item["path"] for item in gc["candidates"]}
            self.assertNotIn(f"objects/{alias_key}", candidates)
            self.assertIn(f"objects/{unreferenced_key}", candidates)
            roots = gc["roots"][0]
            self.assertIn(alias_key, roots["object_keys"])
            self.assertIn(
                final_plan["nodes"][0]["execution_key"], roots["execution_keys"]
            )

    def test_gc_apply_refuses_output_alias_that_became_referenced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            spec_path = root / "spec.json"
            write_json(spec_path, _spec())
            initial_plan_path = root / "initial-plan.json"
            initial_plan = plan_experiment(spec_path, cache, initial_plan_path)
            run_experiment_node(initial_plan_path, "phase1", root / "attempt")
            final_plan_path = root / "final-plan.json"
            final_plan = plan_experiment(spec_path, cache, final_plan_path)

            alias_key = "c" * 64
            alias_output = cache / "objects" / alias_key / "output"
            alias_output.mkdir(parents=True)
            (alias_output / "imported.bin").write_bytes(b"imported payload")
            gc_path = root / "gc.json"
            gc = plan_experiment_gc(
                cache, [final_plan_path], gc_path, minimum_age_seconds=0
            )
            self.assertIn(
                f"objects/{alias_key}",
                {item["path"] for item in gc["candidates"]},
            )

            final_plan["nodes"][0]["output_dir"] = str(alias_output.resolve())
            write_json(final_plan_path, final_plan)
            approved = hashlib.sha256(gc_path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValidationError, "became referenced"):
                apply_experiment_gc(gc_path, approved)
            self.assertTrue(alias_output.is_dir())

    def test_gc_rejects_root_plan_for_another_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_cache = root / "first-cache"
            second_cache = root / "second-cache"
            spec_path = root / "spec.json"
            write_json(spec_path, _spec())
            plan_path = root / "plan.json"
            plan_experiment(spec_path, first_cache, plan_path)
            with self.assertRaisesRegex(ValidationError, "different cache"):
                plan_experiment_gc(
                    second_cache,
                    [plan_path],
                    root / "gc.json",
                    minimum_age_seconds=0,
                )

    def test_legacy_migration_inventory_is_read_only_and_counts_hardlinks_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            first = runs / "full-a"
            second = runs / "full-b"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            marker = first / "multi-fpga-flow-report.json"
            marker.write_text("{}", encoding="utf-8")
            os.link(marker, second / "multi-fpga-flow-report.json")
            output = root / "migration.json"
            report = plan_legacy_run_migration(runs, output)
            self.assertEqual(
                [item["classification"] for item in report["entries"]],
                ["full-flow-candidate", "full-flow-candidate"],
            )
            self.assertFalse(report["safety"]["mutated"])
            self.assertLess(
                report["totals"]["unique_allocated_bytes"],
                report["totals"]["allocated_bytes_before_hardlink_dedup"],
            )
            self.assertEqual(
                [item["exclusive_reclaimable_bytes"] for item in report["entries"]],
                [0, 0],
            )
            self.assertGreater(
                report["totals"]["root_reclaimable_bytes_if_all_entries_retired"],
                0,
            )
            self.assertTrue(marker.is_file())

    def test_legacy_migration_protects_top_level_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            outside = root / "outside"
            runs.mkdir()
            outside.mkdir()
            (runs / "escaped").symlink_to(outside, target_is_directory=True)
            report = plan_legacy_run_migration(runs, root / "migration.json")
            entry = report["entries"][0]
            self.assertEqual(entry["classification"], "unsafe-symlink")
            self.assertTrue(entry["retirement_protection"]["protected"])
            self.assertEqual(report["totals"]["retirement_protected_entries"], 1)

    def test_legacy_retirement_requires_exact_content_and_keeps_marker_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            retired = runs / "synthetic-old"
            retired.mkdir(parents=True)
            marker = retired / "multi-fpga-flow-report.json"
            marker.write_text('{"status":"pass"}', encoding="utf-8")
            (retired / "scratch.bin").write_bytes(b"scratch")
            migration_path = root / "migration.json"
            plan_legacy_run_migration(runs, migration_path)
            plan_path = root / "retirement.json"
            plan = plan_legacy_run_retirement(
                migration_path,
                ["synthetic-old"],
                plan_path,
                reason="retired synthetic regression",
            )
            self.assertEqual(plan["candidates"][0]["classification"], "full-flow-candidate")
            with self.assertRaisesRegex(ValidationError, "approval seal"):
                apply_legacy_run_retirement(plan_path, "0" * 64, root / "receipt")
            approved = hashlib.sha256(plan_path.read_bytes()).hexdigest()
            receipt = apply_legacy_run_retirement(
                plan_path, approved, root / "receipt"
            )
            self.assertEqual(receipt["status"], "pass")
            self.assertFalse(retired.exists())
            tombstone = (
                root
                / "receipt/marker-tombstones/synthetic-old/multi-fpga-flow-report.json"
            )
            self.assertEqual(tombstone.read_text(encoding="utf-8"), '{"status":"pass"}')

    def test_legacy_retirement_refuses_running_or_unreconciled_farm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            farm = runs / "active-farm"
            task = farm / "tasks/task-a"
            task.mkdir(parents=True)
            write_json(farm / "farm-manifest.json", {"status": "pass"})
            (farm / "launch.lock").touch()
            write_json(
                task / "state.json",
                {
                    "schema": "emuflow.validation-farm-state/v1",
                    "status": "running",
                    "lease_expires_at": "2000-01-01T00:00:00+00:00",
                },
            )
            migration_path = root / "migration.json"
            migration = plan_legacy_run_migration(runs, migration_path)
            entry = migration["entries"][0]
            self.assertTrue(entry["retirement_protection"]["protected"])
            self.assertEqual(
                entry["recommended_action"],
                "reconcile-farm-before-retention-decision",
            )
            self.assertIn(
                "nonterminal-farm-state:running:tasks/task-a/state.json",
                entry["retirement_protection"]["reasons"],
            )
            with self.assertRaisesRegex(ValidationError, "active or unreconciled"):
                plan_legacy_run_retirement(
                    migration_path,
                    ["active-farm"],
                    root / "retirement.json",
                    reason="must reconcile before retirement",
                )
            self.assertTrue(farm.is_dir())

    def test_legacy_retirement_refuses_submit_failed_farm_until_final_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            farm = runs / "retryable-farm"
            task = farm / "tasks/task-a"
            task.mkdir(parents=True)
            write_json(farm / "farm-manifest.json", {"status": "pass"})
            (farm / "launch.lock").touch()
            write_json(
                task / "state.json",
                {
                    "schema": "emuflow.validation-farm-state/v1",
                    "status": "submit_failed",
                },
            )
            migration_path = root / "migration.json"
            migration = plan_legacy_run_migration(runs, migration_path)
            self.assertTrue(
                migration["entries"][0]["retirement_protection"]["protected"]
            )
            with self.assertRaisesRegex(ValidationError, "active or unreconciled"):
                plan_legacy_run_retirement(
                    migration_path,
                    ["retryable-farm"],
                    root / "retirement.json",
                    reason="retry remains possible",
                )

    def test_completed_legacy_farm_can_be_sealed_and_retired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            farm = runs / "completed-farm"
            task = farm / "tasks/task-a"
            task.mkdir(parents=True)
            write_json(farm / "farm-manifest.json", {"status": "pass"})
            (farm / "launch.lock").touch()
            write_json(
                task / "state.json",
                {
                    "schema": "emuflow.validation-farm-state/v1",
                    "status": "pass",
                },
            )
            (farm / "diagnostic.log").write_text("complete", encoding="utf-8")
            migration_path = root / "migration.json"
            migration = plan_legacy_run_migration(runs, migration_path)
            self.assertFalse(
                migration["entries"][0]["retirement_protection"]["protected"]
            )
            plan_path = root / "retirement.json"
            plan_legacy_run_retirement(
                migration_path,
                ["completed-farm"],
                plan_path,
                reason="completed noncanonical diagnostic farm",
            )
            approved = hashlib.sha256(plan_path.read_bytes()).hexdigest()
            import shutil

            remove_tree = shutil.rmtree

            def assert_unlocked_before_removal(path: Path) -> None:
                self.assertFalse(farm.exists())
                self.assertTrue((path / "RETIREMENT_PENDING.json").is_file())
                self.assertTrue(path.name.startswith(".emuflow-retiring-completed-farm-"))
                with (path / "launch.lock").open("r+", encoding="utf-8") as stream:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                remove_tree(path)

            with mock.patch(
                "emuflow.experiment_store.shutil.rmtree",
                side_effect=assert_unlocked_before_removal,
            ):
                receipt = apply_legacy_run_retirement(
                    plan_path, approved, root / "receipt"
                )
            self.assertEqual(receipt["status"], "pass")
            self.assertFalse(farm.exists())
            self.assertEqual(receipt["quarantine"][0]["status"], "removed")

    def test_legacy_retirement_plan_refuses_concurrent_farm_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            farm = runs / "completed-farm"
            task = farm / "tasks/task-a"
            task.mkdir(parents=True)
            write_json(farm / "farm-manifest.json", {"status": "pass"})
            launch_lock = farm / "launch.lock"
            launch_lock.touch()
            write_json(
                task / "state.json",
                {
                    "schema": "emuflow.validation-farm-state/v1",
                    "status": "pass",
                },
            )
            migration_path = root / "migration.json"
            plan_legacy_run_migration(runs, migration_path)
            with launch_lock.open("r+", encoding="utf-8") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    with self.assertRaisesRegex(ValidationError, "launch is active"):
                        plan_legacy_run_retirement(
                            migration_path,
                            ["completed-farm"],
                            root / "retirement.json",
                            reason="completed noncanonical diagnostic farm",
                        )
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            self.assertTrue(farm.is_dir())

    def test_legacy_retirement_remains_quarantined_if_removal_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            farm = runs / "completed-farm"
            task = farm / "tasks/task-a"
            task.mkdir(parents=True)
            write_json(farm / "farm-manifest.json", {"status": "pass"})
            (farm / "launch.lock").touch()
            write_json(
                task / "state.json",
                {
                    "schema": "emuflow.validation-farm-state/v1",
                    "status": "pass",
                },
            )
            migration_path = root / "migration.json"
            plan_legacy_run_migration(runs, migration_path)
            plan_path = root / "retirement.json"
            plan_legacy_run_retirement(
                migration_path,
                ["completed-farm"],
                plan_path,
                reason="completed noncanonical diagnostic farm",
            )
            approved = hashlib.sha256(plan_path.read_bytes()).hexdigest()
            with mock.patch(
                "emuflow.experiment_store.shutil.rmtree",
                side_effect=OSError("injected removal failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected removal failure"):
                    apply_legacy_run_retirement(
                        plan_path, approved, root / "receipt"
                    )
            self.assertFalse(farm.exists())
            receipt = json.loads(
                (root / "receipt/retirement-receipt.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(receipt["status"], "in-progress")
            self.assertEqual(receipt["quarantine"][0]["status"], "moved")
            quarantine = Path(receipt["quarantine"][0]["path"])
            self.assertTrue(quarantine.is_dir())
            self.assertTrue((quarantine / "RETIREMENT_PENDING.json").is_file())

    def test_legacy_retirement_apply_rechecks_farm_activity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            farm = runs / "completed-farm"
            task = farm / "tasks/task-a"
            task.mkdir(parents=True)
            write_json(farm / "farm-manifest.json", {"status": "pass"})
            (farm / "launch.lock").touch()
            state_path = task / "state.json"
            write_json(
                state_path,
                {
                    "schema": "emuflow.validation-farm-state/v1",
                    "status": "pass",
                },
            )
            migration_path = root / "migration.json"
            plan_legacy_run_migration(runs, migration_path)
            plan_path = root / "retirement.json"
            plan_legacy_run_retirement(
                migration_path,
                ["completed-farm"],
                plan_path,
                reason="completed noncanonical diagnostic farm",
            )
            approved = hashlib.sha256(plan_path.read_bytes()).hexdigest()
            write_json(
                state_path,
                {
                    "schema": "emuflow.validation-farm-state/v1",
                    "status": "running",
                    "lease_expires_at": "2100-01-01T00:00:00+00:00",
                },
            )
            with self.assertRaisesRegex(ValidationError, "active or unreconciled"):
                apply_legacy_run_retirement(
                    plan_path, approved, root / "receipt"
                )
            self.assertTrue(farm.is_dir())
            self.assertFalse((root / "receipt").exists())

    def test_legacy_retirement_rejects_candidate_changed_after_planning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            candidate = runs / "partial-old"
            candidate.mkdir(parents=True)
            payload = candidate / "payload.bin"
            payload.write_bytes(b"before")
            migration_path = root / "migration.json"
            plan_legacy_run_migration(runs, migration_path)
            plan_path = root / "retirement.json"
            plan_legacy_run_retirement(
                migration_path,
                ["partial-old"],
                plan_path,
                reason="obsolete failed attempt",
            )
            approved = hashlib.sha256(plan_path.read_bytes()).hexdigest()
            payload.write_bytes(b"after")
            with self.assertRaisesRegex(ValidationError, "candidate changed"):
                apply_legacy_run_retirement(
                    plan_path, approved, root / "receipt"
                )
            self.assertTrue(candidate.is_dir())
            self.assertFalse((root / "receipt").exists())

    def test_legacy_retirement_rejects_symlink_swap_even_with_identical_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            candidate = runs / "partial-old"
            twin = runs / "identical-twin"
            candidate.mkdir(parents=True)
            twin.mkdir(parents=True)
            (candidate / "payload.bin").write_bytes(b"same")
            (twin / "payload.bin").write_bytes(b"same")
            migration_path = root / "migration.json"
            plan_legacy_run_migration(runs, migration_path)
            plan_path = root / "retirement.json"
            plan_legacy_run_retirement(
                migration_path,
                ["partial-old"],
                plan_path,
                reason="obsolete failed attempt",
            )
            approved = hashlib.sha256(plan_path.read_bytes()).hexdigest()
            import shutil

            shutil.rmtree(candidate)
            candidate.symlink_to(twin, target_is_directory=True)
            with self.assertRaisesRegex(ValidationError, "candidate changed"):
                apply_legacy_run_retirement(
                    plan_path, approved, root / "receipt"
                )
            self.assertTrue(twin.is_dir())

    def test_legacy_retirement_seals_internal_symlink_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            candidate = runs / "failed-build"
            outside = root / "outside.bin"
            candidate.mkdir(parents=True)
            outside.write_bytes(b"preserve")
            link = candidate / "generated-library.so"
            link.symlink_to(outside)
            migration_path = root / "migration.json"
            plan_legacy_run_migration(runs, migration_path)
            plan_path = root / "retirement.json"
            plan_legacy_run_retirement(
                migration_path,
                ["failed-build"],
                plan_path,
                reason="obsolete failed build",
            )
            approved = hashlib.sha256(plan_path.read_bytes()).hexdigest()
            receipt = apply_legacy_run_retirement(
                plan_path, approved, root / "receipt"
            )
            self.assertEqual(receipt["status"], "pass")
            self.assertFalse(candidate.exists())
            self.assertEqual(outside.read_bytes(), b"preserve")

    def test_legacy_retirement_rejects_internal_symlink_retarget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            candidate = runs / "failed-build"
            candidate.mkdir(parents=True)
            first = root / "first.bin"
            second = root / "second.bin"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            link = candidate / "generated-library.so"
            link.symlink_to(first)
            migration_path = root / "migration.json"
            plan_legacy_run_migration(runs, migration_path)
            plan_path = root / "retirement.json"
            plan_legacy_run_retirement(
                migration_path,
                ["failed-build"],
                plan_path,
                reason="obsolete failed build",
            )
            approved = hashlib.sha256(plan_path.read_bytes()).hexdigest()
            link.unlink()
            link.symlink_to(second)
            with self.assertRaisesRegex(ValidationError, "candidate changed"):
                apply_legacy_run_retirement(
                    plan_path, approved, root / "receipt"
                )
            self.assertTrue(candidate.is_dir())
            self.assertFalse((root / "receipt").exists())


if __name__ == "__main__":
    unittest.main()
