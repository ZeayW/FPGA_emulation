import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from emuflow.errors import EmuFlowError, ValidationError
from emuflow.experiment_dag import (
    EXPERIMENT_PLAN_SCHEMA,
    EXPERIMENT_SPEC_SCHEMA,
    EXPERIMENT_SPEC_V2_SCHEMA,
    build_experiment_farm_spec,
    import_experiment_checkpoint,
    plan_experiment,
    run_experiment_node,
    validate_experiment_checkpoint,
    validate_experiment_spec,
)
from emuflow.io import read_json, write_json


COMMIT = "1" * 40


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _writer(payload: str, artifact: str, *dependencies: str) -> list[str]:
    script = (
        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
        "p.mkdir(parents=True,exist_ok=True); "
        f"p.joinpath('{artifact}').write_text('{payload}')"
    )
    return [sys.executable, "-c", script, "{output_dir}", *dependencies]


def _validator(artifact: str, *dependencies: str) -> list[str]:
    script = (
        "import pathlib,sys; "
        f"raise SystemExit(0 if pathlib.Path(sys.argv[1]).joinpath('{artifact}').is_file() else 3)"
    )
    return [sys.executable, "-c", script, "{artifact_root}", *dependencies]


class ExperimentDagTest(unittest.TestCase):
    def _spec(self) -> dict:
        return {
            "schema": EXPERIMENT_SPEC_SCHEMA,
            "experiment_id": "koios-case6-phase6-ab",
            "source_commit": COMMIT,
            "nodes": [
                {
                    "id": "shared-phase1-5",
                    "stage": "shared-phase1-5",
                    "dependencies": [],
                    "inputs": {
                        "rtl": _digest("rtl"),
                        "boarddb": _digest("boarddb"),
                    },
                    "configuration": {"partition_seed": 0},
                    "command": _writer("shared", "phase5.json"),
                    "validator": _validator("phase5.json"),
                    "artifacts": ["phase5.json"],
                },
                {
                    "id": "phase6-baseline",
                    "stage": "phase6",
                    "provider": "baseline",
                    "dependencies": ["shared-phase1-5"],
                    "inputs": {},
                    "configuration": {"equivalence_seed": 7},
                    "command": _writer(
                        "baseline", "phase6.json", "{dependency:shared-phase1-5}"
                    ),
                    "validator": _validator(
                        "phase6.json", "{dependency:shared-phase1-5}"
                    ),
                    "artifacts": ["phase6.json"],
                },
                {
                    "id": "phase6-chimew",
                    "stage": "phase6",
                    "provider": "chimew",
                    "dependencies": ["shared-phase1-5"],
                    "inputs": {},
                    "configuration": {"regions": 4},
                    "command": _writer(
                        "chimew", "phase6.json", "{dependency:shared-phase1-5}"
                    ),
                    "validator": _validator(
                        "phase6.json", "{dependency:shared-phase1-5}"
                    ),
                    "artifacts": ["phase6.json"],
                },
                {
                    "id": "shared-lookahead",
                    "stage": "physical-lookahead",
                    "dependencies": ["shared-phase1-5", "phase6-baseline"],
                    "inputs": {},
                    "configuration": {"lookahead_seed": 11},
                    "command": _writer(
                        "lookahead",
                        "lookahead.json",
                        "{dependency:shared-phase1-5}",
                        "{dependency:phase6-baseline}",
                    ),
                    "validator": _validator(
                        "lookahead.json",
                        "{dependency:shared-phase1-5}",
                        "{dependency:phase6-baseline}",
                    ),
                    "artifacts": ["lookahead.json"],
                },
                {
                    "id": "phase7-baseline-seed1",
                    "stage": "phase7",
                    "provider": "baseline",
                    "physical_seed": 1,
                    "dependencies": ["shared-phase1-5", "phase6-baseline"],
                    "inputs": {},
                    "configuration": {
                        "physical_backend": "open",
                        "physical_workers": 8,
                    },
                    "command": _writer(
                        "baseline-seed1",
                        "physical-summary.json",
                        "{dependency:shared-phase1-5}",
                        "{dependency:phase6-baseline}",
                    ),
                    "validator": _validator(
                        "physical-summary.json",
                        "{dependency:shared-phase1-5}",
                        "{dependency:phase6-baseline}",
                    ),
                    "artifacts": ["physical-summary.json"],
                },
                {
                    "id": "phase7-chimew-seed1",
                    "stage": "phase7",
                    "provider": "chimew",
                    "physical_seed": 1,
                    "dependencies": ["shared-phase1-5", "phase6-chimew"],
                    "inputs": {},
                    "configuration": {
                        "physical_backend": "open",
                        "physical_workers": 8,
                    },
                    "command": _writer(
                        "chimew-seed1",
                        "physical-summary.json",
                        "{dependency:shared-phase1-5}",
                        "{dependency:phase6-chimew}",
                    ),
                    "validator": _validator(
                        "physical-summary.json",
                        "{dependency:shared-phase1-5}",
                        "{dependency:phase6-chimew}",
                    ),
                    "artifacts": ["physical-summary.json"],
                },
            ],
        }

    def _write_spec(self, root: Path, value: Optional[dict] = None) -> Path:
        path = root / "spec.json"
        write_json(path, value or self._spec())
        return path

    def _v2_spec(self, *, commit: str = COMMIT) -> dict:
        def closure(label: str) -> dict:
            digest = _digest(label)
            files = [{"path": f"{label}.py", "bytes": 1, "sha256": digest}]
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
                    json.dumps(
                        identity,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest(),
            }

        def node(
            node_id: str,
            stage: str,
            payload: str,
            dependencies: list[str],
            implementation: str,
            validator: str,
            *,
            inputs: Optional[dict] = None,
        ) -> dict:
            artifact = f"{stage}.json"
            return {
                "id": node_id,
                "stage": stage,
                "dependencies": dependencies,
                "inputs": inputs or {},
                "configuration": {},
                "implementation": closure(implementation),
                "command": _writer(
                    payload,
                    artifact,
                    *(f"{{dependency:{item}}}" for item in dependencies),
                ),
                "validator_implementation": closure(validator),
                "validator": _validator(
                    artifact,
                    *(f"{{dependency:{item}}}" for item in dependencies),
                ),
                "environment": {},
                "storage_estimate": {
                    "peak_bytes": 1024,
                    "retained_bytes": 128,
                },
                "artifacts": [
                    {
                        "path": artifact,
                        "role": "consumer-checkpoint",
                    }
                ],
            }

        return {
            "schema": EXPERIMENT_SPEC_V2_SCHEMA,
            "experiment_id": "generic-flow",
            "source_commit": commit,
            "nodes": [
                node(
                    "phase1",
                    "phase1",
                    "one",
                    [],
                    "phase1-implementation",
                    "phase1-validator",
                    inputs={"rtl": _digest("rtl")},
                ),
                node(
                    "phase2",
                    "phase2",
                    "two",
                    ["phase1"],
                    "phase2-implementation",
                    "phase2-validator",
                ),
                node(
                    "phase3",
                    "phase3",
                    "three",
                    ["phase2"],
                    "phase3-implementation",
                    "phase3-validator",
                ),
            ],
        }

    def test_frontiers_reuse_shared_and_provider_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            spec = self._write_spec(root)
            plan1_path = root / "plan1.json"
            plan1 = plan_experiment(spec, cache, plan1_path)
            self.assertEqual(
                plan1["counts"], {"reuse": 0, "ready": 1, "waiting": 5}
            )
            self.assertEqual(plan1["nodes"][0]["state"], "ready")

            shared_run = root / "run-shared"
            shared = run_experiment_node(
                plan1_path, "shared-phase1-5", shared_run
            )
            self.assertEqual(shared["status"], "pass")
            plan2_path = root / "plan2.json"
            plan2 = plan_experiment(spec, cache, plan2_path)
            self.assertEqual(
                plan2["counts"], {"reuse": 1, "ready": 2, "waiting": 3}
            )

            baseline6 = run_experiment_node(
                plan2_path, "phase6-baseline", root / "run-baseline6"
            )
            self.assertEqual(baseline6["status"], "pass")
            plan3_path = root / "plan3.json"
            plan3 = plan_experiment(spec, cache, plan3_path)
            by_id = {item["id"]: item["state"] for item in plan3["nodes"]}
            self.assertEqual(by_id["shared-phase1-5"], "reuse")
            self.assertEqual(by_id["phase6-baseline"], "reuse")
            self.assertEqual(by_id["phase6-chimew"], "ready")
            self.assertEqual(by_id["shared-lookahead"], "ready")
            self.assertEqual(by_id["phase7-baseline-seed1"], "ready")
            self.assertEqual(by_id["phase7-chimew-seed1"], "waiting")

            baseline7 = run_experiment_node(
                plan3_path,
                "phase7-baseline-seed1",
                root / "run-baseline7",
            )
            self.assertEqual(baseline7["status"], "pass")
            repeated = run_experiment_node(
                plan3_path,
                "phase7-baseline-seed1",
                root / "run-baseline7-repeat",
            )
            self.assertEqual(repeated["status"], "reused")

    def test_changes_invalidate_only_the_affected_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            spec = self._write_spec(root)
            plan1_path = root / "plan1.json"
            plan_experiment(spec, cache, plan1_path)
            run_experiment_node(plan1_path, "shared-phase1-5", root / "shared")
            plan2_path = root / "plan2.json"
            plan_experiment(spec, cache, plan2_path)
            run_experiment_node(plan2_path, "phase6-baseline", root / "baseline")

            changed = self._spec()
            changed["nodes"][1]["configuration"]["equivalence_seed"] = 8
            changed_path = self._write_spec(root, changed)
            changed_plan = plan_experiment(changed_path, cache, root / "changed.json")
            states = {item["id"]: item["state"] for item in changed_plan["nodes"]}
            self.assertEqual(states["shared-phase1-5"], "reuse")
            self.assertEqual(states["phase6-baseline"], "ready")
            self.assertEqual(states["phase7-baseline-seed1"], "waiting")
            self.assertEqual(states["phase6-chimew"], "ready")

            changed_source = self._spec()
            changed_source["nodes"][0]["inputs"]["rtl"] = _digest("new-rtl")
            source_path = self._write_spec(root, changed_source)
            source_plan = plan_experiment(source_path, cache, root / "source.json")
            self.assertEqual(
                source_plan["counts"], {"reuse": 0, "ready": 1, "waiting": 5}
            )

    def test_existing_external_result_can_be_imported_without_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = self._write_spec(root)
            plan_path = root / "plan.json"
            plan = plan_experiment(spec, root / "cache", plan_path)
            old = root / "old-phase1-5"
            old.mkdir()
            (old / "phase5.json").write_text("shared", encoding="utf-8")
            imported = import_experiment_checkpoint(
                plan_path, "shared-phase1-5", old
            )
            self.assertEqual(imported["status"], "imported")
            replanned = plan_experiment(spec, root / "cache", root / "next.json")
            self.assertEqual(replanned["nodes"][0]["state"], "reuse")
            (old / "phase5.json").write_text("tampered", encoding="utf-8")
            manifest = (
                root
                / "cache"
                / "objects"
                / plan["nodes"][0]["key"]
                / "checkpoint.json"
            )
            with self.assertRaisesRegex(ValidationError, "seal is broken"):
                validate_experiment_checkpoint(manifest)

    def test_cache_resident_import_becomes_an_immutable_managed_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            spec = self._write_spec(root)
            plan_path = root / "plan.json"
            plan_experiment(spec, cache, plan_path)
            source = cache / "objects" / ("f" * 64) / "output"
            source.mkdir(parents=True)
            artifact = source / "phase5.json"
            artifact.write_text("shared", encoding="utf-8")

            imported = import_experiment_checkpoint(
                plan_path, "shared-phase1-5", source
            )
            self.assertEqual(imported["status"], "imported")
            self.assertEqual(imported["checkpoint"]["storage"], "managed")
            self.assertTrue(imported["checkpoint"]["output_immutable"])
            self.assertEqual(source.stat().st_mode & 0o222, 0)
            self.assertEqual(artifact.stat().st_mode & 0o222, 0)

            with patch(
                "emuflow.experiment_dag._artifact_digest",
                side_effect=AssertionError("managed plan rehashed artifact content"),
            ):
                replanned = plan_experiment(spec, cache, root / "next.json")
            self.assertEqual(replanned["nodes"][0]["state"], "reuse")

    def test_existing_cache_alias_is_promoted_after_one_strong_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            spec = self._write_spec(root)
            plan_path = root / "plan.json"
            plan = plan_experiment(spec, cache, plan_path)
            source = cache / "objects" / ("e" * 64) / "output"
            source.mkdir(parents=True)
            artifact = source / "phase5.json"
            artifact.write_text("shared", encoding="utf-8")
            imported = import_experiment_checkpoint(
                plan_path, "shared-phase1-5", source
            )
            manifest = (
                cache
                / "objects"
                / plan["nodes"][0]["key"]
                / "checkpoint.json"
            )
            manifest.chmod(0o644)
            value = read_json(manifest)
            value["storage"] = "external-validated"
            value["output_immutable"] = False
            write_json(manifest, value)
            source.chmod(0o755)
            artifact.chmod(0o644)

            promoted = import_experiment_checkpoint(
                plan_path, "shared-phase1-5", source
            )
            self.assertEqual(promoted["status"], "promoted")
            self.assertEqual(promoted["checkpoint"]["storage"], "managed")
            self.assertEqual(manifest.stat().st_mode & 0o222, 0)
            self.assertEqual(artifact.stat().st_mode & 0o222, 0)

    def test_import_and_new_runs_require_independent_validator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            invalid = self._spec()
            invalid["nodes"][0]["validator"] = [
                sys.executable,
                "-c",
                "raise SystemExit(4)",
                "{artifact_root}",
            ]
            spec = self._write_spec(root, invalid)
            plan_path = root / "plan.json"
            plan_experiment(spec, root / "cache", plan_path)
            old = root / "old"
            old.mkdir()
            (old / "phase5.json").write_text("shared", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "validator failed"):
                import_experiment_checkpoint(plan_path, "shared-phase1-5", old)
            report = run_experiment_node(
                plan_path, "shared-phase1-5", root / "run"
            )
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["failure_stage"], "independent-validator")

    def test_farm_spec_contains_only_ready_cache_misses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "install" / COMMIT
            install.mkdir(parents=True)
            spec = self._write_spec(root)
            plan_path = root / "plan.json"
            plan_experiment(spec, root / "cache", plan_path)
            farm_path = root / "farm.json"
            report = build_experiment_farm_spec(
                plan_path, install, ["hpc1", "hpc2"], "case6-frontier", farm_path
            )
            self.assertEqual(report["ready_tasks"], 1)
            farm = read_json(farm_path)
            self.assertEqual([task["id"] for task in farm["tasks"]], ["shared-phase1-5"])
            self.assertIn("--expected-plan-sha256", farm["tasks"][0]["command"])

    def test_farm_spec_content_seals_an_outer_worker_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "install" / COMMIT
            install.mkdir(parents=True)
            launcher = root / "worker-launcher"
            launcher.write_text("#!/bin/sh\nexec \"$@\"\n", encoding="utf-8")
            launcher.chmod(0o755)
            spec = self._write_spec(root)
            plan_path = root / "plan.json"
            plan_experiment(spec, root / "cache", plan_path)
            farm_path = root / "farm.json"
            build_experiment_farm_spec(
                plan_path,
                install,
                ["hpc1"],
                "container-frontier",
                farm_path,
                worker_launcher=launcher.resolve(),
            )
            farm = read_json(farm_path)
            self.assertEqual(
                farm["worker_argv"],
                [str(launcher.resolve()), "{install}/bin/emuflow"],
            )
            self.assertEqual(
                farm["worker_launcher"]["sha256"],
                hashlib.sha256(launcher.read_bytes()).hexdigest(),
            )

            with self.assertRaisesRegex(ValidationError, "must be absolute"):
                build_experiment_farm_spec(
                    plan_path,
                    install,
                    ["hpc1"],
                    "relative-launcher",
                    root / "relative.json",
                    worker_launcher=Path("worker-launcher"),
                )

    def test_farm_spec_seals_explicit_worker_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "install" / COMMIT
            install.mkdir(parents=True)
            plan_path = root / "plan.json"
            plan_experiment(self._write_spec(root), root / "cache", plan_path)
            farm_path = root / "farm.json"
            wrapper = [
                "/usr/bin/env",
                "PYTHONPATH={install}/lib",
                "/runtime/emuflow-run",
                "{install}/bin/emuflow",
            ]
            build_experiment_farm_spec(
                plan_path,
                install,
                ["hpc1"],
                "wrapped-frontier",
                farm_path,
                worker_argv=wrapper,
            )
            self.assertEqual(read_json(farm_path)["worker_argv"], wrapper)

            with self.assertRaisesRegex(ValidationError, "non-empty string list"):
                build_experiment_farm_spec(
                    plan_path,
                    install,
                    ["hpc1"],
                    "empty-wrapper",
                    root / "empty.json",
                    worker_argv=[],
                )
            with self.assertRaisesRegex(ValidationError, "non-empty strings"):
                build_experiment_farm_spec(
                    plan_path,
                    install,
                    ["hpc1"],
                    "invalid-wrapper",
                    root / "invalid.json",
                    worker_argv=["/runtime/emuflow-run", ""],
                )
            with self.assertRaisesRegex(ValidationError, "mutually exclusive"):
                build_experiment_farm_spec(
                    plan_path,
                    install,
                    ["hpc1"],
                    "ambiguous-wrapper",
                    root / "ambiguous.json",
                    worker_argv=wrapper,
                    worker_launcher=Path("/runtime/emuflow-run"),
                )

    def test_farm_spec_can_submit_a_bounded_ready_subset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "install" / COMMIT
            install.mkdir(parents=True)
            value = self._v2_spec()
            value["nodes"][1]["dependencies"] = []
            value["nodes"][1]["inputs"] = {"fixture": "b" * 64}
            value["nodes"][1]["command"] = [
                sys.executable,
                "-c",
                "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('phase2')",
                "{output_dir}/phase2.json",
            ]
            value["nodes"][1]["validator"] = [
                sys.executable,
                "-c",
                "import pathlib,sys; assert pathlib.Path(sys.argv[1]).is_file()",
                "{artifact_root}/phase2.json",
            ]
            value["nodes"][2]["dependencies"] = ["phase1", "phase2"]
            spec = self._write_spec(root, value)
            plan_path = root / "plan.json"
            plan_experiment(spec, root / "cache", plan_path)
            farm_path = root / "farm.json"
            report = build_experiment_farm_spec(
                plan_path,
                install,
                ["hpc1", "hpc2"],
                "bounded-frontier",
                farm_path,
                ["phase2"],
            )
            self.assertEqual(report["ready_tasks"], 1)
            self.assertEqual(report["deferred_ready_tasks"], 1)
            self.assertEqual(
                [item["id"] for item in read_json(farm_path)["tasks"]],
                ["phase2"],
            )
            with self.assertRaisesRegex(ValidationError, "not ready"):
                build_experiment_farm_spec(
                    plan_path,
                    install,
                    ["hpc1"],
                    "invalid-subset",
                    root / "invalid.json",
                    ["phase3"],
                )

    def test_invalid_dependencies_provider_seed_and_placeholders_are_rejected(self) -> None:
        invalid = self._spec()
        invalid["nodes"][4]["provider"] = "chimew"
        with self.assertRaisesRegex(ValidationError, "matching Phase 6 provider"):
            validate_experiment_spec(invalid)
        invalid = self._spec()
        invalid["nodes"][1]["command"].append("{dependency:phase6-chimew}")
        with self.assertRaisesRegex(ValidationError, "undeclared dependency"):
            validate_experiment_spec(invalid)
        invalid = self._spec()
        invalid["nodes"][4]["physical_seed"] = -1
        with self.assertRaisesRegex(ValidationError, "physical seed"):
            validate_experiment_spec(invalid)
        invalid = self._spec()
        del invalid["nodes"][4]["configuration"]["physical_workers"]
        with self.assertRaisesRegex(ValidationError, "physical_workers"):
            validate_experiment_spec(invalid)
        invalid = self._spec()
        invalid["nodes"][0]["validator"] = ["check-without-artifact-root"]
        with self.assertRaisesRegex(ValidationError, "artifact_root"):
            validate_experiment_spec(invalid)

    def test_v2_commit_is_provenance_and_stage_implementation_controls_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            spec = self._write_spec(root, self._v2_spec())
            plan1_path = root / "plan1.json"
            plan_experiment(spec, cache, plan1_path)
            run_experiment_node(plan1_path, "phase1", root / "run1")

            changed_commit = self._v2_spec(commit="2" * 40)
            changed_path = self._write_spec(root, changed_commit)
            replanned = plan_experiment(changed_path, cache, root / "commit.json")
            states = {item["id"]: item["state"] for item in replanned["nodes"]}
            self.assertEqual(states, {"phase1": "reuse", "phase2": "ready", "phase3": "waiting"})

            changed_phase2 = self._v2_spec(commit="2" * 40)
            changed_phase2["nodes"][1]["implementation"] = self._v2_spec()[
                "nodes"
            ][0]["implementation"]
            changed_phase2_path = self._write_spec(root, changed_phase2)
            changed_plan = plan_experiment(
                changed_phase2_path, cache, root / "phase2-change.json"
            )
            states = {item["id"]: item["state"] for item in changed_plan["nodes"]}
            self.assertEqual(states, {"phase1": "reuse", "phase2": "ready", "phase3": "waiting"})

    def test_v2_byte_bound_runtime_paths_do_not_change_execution_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def relocated(path: str) -> dict:
                value = self._v2_spec()
                node = value["nodes"][0]
                node["inputs"]["tool.runtime"] = _digest("same-tool-bytes")
                node["command"][0] = path
                node["validator"][0] = path
                node["execution_bindings"] = {"tool.runtime": path}
                node["command_identity"] = [
                    "{input:tool.runtime}", *node["command"][1:]
                ]
                node["validator_identity"] = [
                    "{input:tool.runtime}", *node["validator"][1:]
                ]
                return value

            first_path = self._write_spec(root, relocated("/install/a/emuflow"))
            first = plan_experiment(first_path, root / "cache", root / "first.json")
            second_path = root / "relocated.json"
            write_json(second_path, relocated("/install/b/emuflow"))
            second = plan_experiment(
                second_path, root / "cache", root / "second.json"
            )
            self.assertEqual(
                first["nodes"][0]["execution_key"],
                second["nodes"][0]["execution_key"],
            )
            self.assertNotEqual(
                first["nodes"][0]["command"][0],
                second["nodes"][0]["command"][0],
            )

            changed = relocated("/install/c/emuflow")
            changed["nodes"][0]["inputs"]["tool.runtime"] = _digest(
                "changed-tool-bytes"
            )
            changed_path = root / "changed.json"
            write_json(changed_path, changed)
            changed_plan = plan_experiment(
                changed_path, root / "cache", root / "changed-plan.json"
            )
            self.assertNotEqual(
                first["nodes"][0]["execution_key"],
                changed_plan["nodes"][0]["execution_key"],
            )

            tampered = relocated("/install/d/emuflow")
            tampered["nodes"][0]["command_identity"][0] = "unsealed-tool"
            with self.assertRaisesRegex(ValidationError, "command_identity"):
                validate_experiment_spec(tampered)

            incomplete = relocated("/install/e/emuflow")
            del incomplete["nodes"][0]["validator_identity"]
            with self.assertRaisesRegex(ValidationError, "declare execution_bindings"):
                validate_experiment_spec(incomplete)

    def test_v2_validator_change_revalidates_without_rerunning_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            original = self._v2_spec()
            spec = self._write_spec(root, original)
            plan1_path = root / "plan1.json"
            plan1 = plan_experiment(spec, cache, plan1_path)
            run_experiment_node(plan1_path, "phase1", root / "run1")
            artifact = Path(plan1["nodes"][0]["output_dir"]) / "phase1.json"
            original_mtime = artifact.stat().st_mtime_ns

            changed = self._v2_spec(commit="2" * 40)
            changed["nodes"][0]["validator_implementation"] = self._v2_spec()[
                "nodes"
            ][1]["validator_implementation"]
            changed_path = self._write_spec(root, changed)
            plan2_path = root / "plan2.json"
            plan2 = plan_experiment(changed_path, cache, plan2_path)
            self.assertEqual(plan2["nodes"][0]["state"], "revalidate")
            report = run_experiment_node(plan2_path, "phase1", root / "revalidate")
            self.assertEqual(report["status"], "revalidated")
            self.assertEqual(artifact.stat().st_mtime_ns, original_mtime)
            plan3 = plan_experiment(changed_path, cache, root / "plan3.json")
            self.assertEqual(plan3["nodes"][0]["state"], "reuse")

    def test_managed_checkpoint_output_is_published_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            spec = self._write_spec(root, self._v2_spec())
            plan_path = root / "plan.json"
            plan = plan_experiment(spec, cache, plan_path)
            report = run_experiment_node(plan_path, "phase1", root / "attempt")
            self.assertEqual(report["status"], "pass")
            output = Path(plan["nodes"][0]["output_dir"])
            self.assertEqual(output.stat().st_mode & 0o222, 0)
            self.assertEqual((output / "phase1.json").stat().st_mode & 0o222, 0)
            self.assertTrue(report["checkpoint"]["output_immutable"])
            self.assertGreaterEqual(
                report["checkpoint"]["execution_elapsed_seconds"], 0.0
            )
            self.assertEqual(
                report["checkpoint"]["execution_elapsed_seconds"],
                report["elapsed_seconds"],
            )

    def test_explicit_validation_still_rehashes_managed_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache"
            spec = self._write_spec(root, self._v2_spec())
            plan_path = root / "plan.json"
            plan = plan_experiment(spec, cache, plan_path)
            run_experiment_node(plan_path, "phase1", root / "attempt")
            artifact = Path(plan["nodes"][0]["output_dir"]) / "phase1.json"
            artifact.chmod(0o644)
            artifact.write_text("tampered", encoding="utf-8")
            artifact.chmod(0o444)
            manifest = (
                cache / "objects" / plan["nodes"][0]["key"] / "checkpoint.json"
            )
            with self.assertRaisesRegex(ValidationError, "seal is broken"):
                validate_experiment_checkpoint(manifest)

    def test_v2_artifact_roles_are_validated(self) -> None:
        invalid = self._v2_spec()
        invalid["nodes"][0]["artifacts"][0]["role"] = "large-file"
        with self.assertRaisesRegex(ValidationError, "artifact role"):
            validate_experiment_spec(invalid)
        invalid = self._v2_spec()
        invalid["nodes"][0]["artifacts"][0]["retention"] = "prunable"
        with self.assertRaisesRegex(ValidationError, "retention disagrees"):
            validate_experiment_spec(invalid)
        invalid = self._v2_spec()
        invalid["nodes"][0]["storage_estimate"]["retained_bytes"] = 2048
        with self.assertRaisesRegex(ValidationError, "storage_estimate"):
            validate_experiment_spec(invalid)


if __name__ == "__main__":
    unittest.main()
