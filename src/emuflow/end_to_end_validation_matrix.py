"""Validation for canonical real-RTL x contest-BoardDB full-flow cases."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Mapping

from .benchmark import BenchmarkRun
from .contest_validation_matrix import load_contest_validation_matrix
from .errors import ValidationError
from .io import read_json


END_TO_END_VALIDATION_MATRIX_SCHEMA = (
    "emuflow.end-to-end-validation-matrix/v1"
)

WORKLOAD_CONTRACT = "naturally-connected-upstream-rtl"
PLATFORM_CONTRACT = "contest-derived-boarddb"
PHASE6_PROVIDERS = ("baseline", "placement-aware", "chimew")
PHYSICAL_SEEDS = (1,)
REQUIRED_GATES = (
    "contest-fetch",
    "contest-import",
    "boarddb-materialize",
    "rtl-fetch",
    "phase1",
    "phase2",
    "phase3",
    "phase4",
    "phase5",
    "phase6",
    "phase7",
    "phase7c",
    "qor-compare",
)
PRIMARY_QOR = (
    "global_target_clock_wns_ns",
    "global_target_clock_tns_ns",
)
REQUIRED_DIAGNOSTICS = (
    "global_target_clock_failing_endpoints",
    "per_fpga_wns_ns",
    "per_fpga_tns_ns",
    "critical_path",
    "runtime_seconds",
    "unrouted_nets",
    "drc_violations",
)
FROZEN_AB_INPUTS = (
    "source_commit",
    "rtl_source_hashes",
    "target_clock_periods_ns",
    "frontend_mapping_profile",
    "boarddb_hash",
    "phase1_emuir_hash",
    "phase3_assignment_hash",
    "phase4_routes_hash",
    "phase5_schedule_hash",
    "physical_backend",
    "physical_options",
    "physical_workers",
)

_ID_RE = re.compile(r"[a-z0-9][a-z0-9.-]*(?:__[a-z0-9][a-z0-9.-]*)?")
_HEX40_RE = re.compile(r"[0-9a-f]{40}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_ROLES = {"primary-qor", "topology-replication", "final-scale"}
_STATES = {"planned", "blocked", "qualified"}
_CLAIM_SCOPE = "academic-contest-topology-device-projection"


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"end-to-end matrix {label} must be a non-empty string")
    return value


def _exact_list(value: Any, expected: tuple[Any, ...], label: str) -> None:
    if value != list(expected):
        raise ValidationError(
            f"end-to-end matrix {label} must equal {list(expected)!r}"
        )


def _repo_file(root: Path, value: Any, label: str) -> Path:
    relative = Path(_string(value, label))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError(
            f"end-to-end matrix {label} must be a safe repository-relative path"
        )
    candidate = (root / relative).resolve()
    root = root.resolve()
    if root not in candidate.parents or not candidate.is_file():
        raise ValidationError(
            f"end-to-end matrix {label} does not name a repository file"
        )
    return candidate


def _validate_policy(policy: Any) -> None:
    if not isinstance(policy, dict):
        raise ValidationError("end-to-end matrix policy must be an object")
    if policy.get("workload_contract") != WORKLOAD_CONTRACT:
        raise ValidationError("end-to-end matrix workload contract is invalid")
    if policy.get("platform_contract") != PLATFORM_CONTRACT:
        raise ValidationError("end-to-end matrix platform contract is invalid")
    _exact_list(policy.get("phase6_providers"), PHASE6_PROVIDERS, "providers")
    _exact_list(policy.get("physical_seeds"), PHYSICAL_SEEDS, "physical seeds")
    _exact_list(policy.get("required_gates"), REQUIRED_GATES, "required gates")
    _exact_list(policy.get("primary_qor"), PRIMARY_QOR, "primary QoR")
    _exact_list(
        policy.get("required_diagnostics"),
        REQUIRED_DIAGNOSTICS,
        "required diagnostics",
    )
    _exact_list(
        policy.get("frozen_ab_inputs"), FROZEN_AB_INPUTS, "frozen A/B inputs"
    )
    if policy.get("phase7_success") != {
        "drc_violations": 0,
        "unrouted_nets": 0,
    }:
        raise ValidationError("end-to-end matrix Phase 7 success policy is invalid")


def _validate_evidence(record: Mapping[str, Any], case_id: str) -> None:
    state = _string(record.get("state"), f"case {case_id} state")
    if state not in _STATES:
        raise ValidationError(f"end-to-end matrix case {case_id} state is invalid")
    evidence = record.get("evidence")
    if not isinstance(evidence, list):
        raise ValidationError(
            f"end-to-end matrix case {case_id} evidence must be a list"
        )
    blockers = record.get("blockers", [])
    if not isinstance(blockers, list) or not all(
        isinstance(item, str) and item for item in blockers
    ):
        raise ValidationError(
            f"end-to-end matrix case {case_id} blockers must be a string list"
        )
    if state == "planned" and (evidence or blockers):
        raise ValidationError(
            f"end-to-end matrix planned case {case_id} cannot claim evidence or blockers"
        )
    if state == "blocked" and (evidence or not blockers):
        raise ValidationError(
            f"end-to-end matrix blocked case {case_id} requires blockers only"
        )
    if state == "qualified":
        if blockers or not evidence:
            raise ValidationError(
                f"end-to-end matrix qualified case {case_id} requires evidence"
            )
        provider_seeds: set[tuple[str, int]] = set()
        for item in evidence:
            if not isinstance(item, dict):
                raise ValidationError(
                    f"end-to-end matrix case {case_id} evidence must be hash records"
                )
            manifest = item.get("manifest_sha256")
            commit = item.get("source_commit")
            provider = item.get("provider")
            seed = item.get("physical_seed")
            if not isinstance(manifest, str) or _HEX64_RE.fullmatch(manifest) is None:
                raise ValidationError(
                    f"end-to-end matrix case {case_id} evidence manifest is invalid"
                )
            if not isinstance(commit, str) or _HEX40_RE.fullmatch(commit) is None:
                raise ValidationError(
                    f"end-to-end matrix case {case_id} evidence commit is invalid"
                )
            if provider not in PHASE6_PROVIDERS or seed not in PHYSICAL_SEEDS:
                raise ValidationError(
                    f"end-to-end matrix case {case_id} evidence arm is invalid"
                )
            provider_seeds.add((provider, seed))
        expected = {
            (provider, seed)
            for provider in PHASE6_PROVIDERS
            for seed in PHYSICAL_SEEDS
        }
        if len(provider_seeds) != len(evidence) or provider_seeds != expected:
            raise ValidationError(
                f"end-to-end matrix qualified case {case_id} evidence must cover "
                "every provider and physical seed exactly once"
            )


def _validate_case(
    record: Any,
    root: Path,
    catalog: Mapping[str, Any],
) -> tuple[str, str, str, str]:
    if not isinstance(record, dict):
        raise ValidationError("end-to-end matrix cases must be objects")
    case_id = _string(record.get("id"), "case id")
    if _ID_RE.fullmatch(case_id) is None or "__" not in case_id:
        raise ValidationError(
            "end-to-end matrix case id must be '<workload>__<contest-case>'"
        )
    role = _string(record.get("role"), f"case {case_id} role")
    if role not in _ROLES:
        raise ValidationError(f"end-to-end matrix case {case_id} role is invalid")

    workload = record.get("workload")
    if not isinstance(workload, dict):
        raise ValidationError(
            f"end-to-end matrix case {case_id} workload must be an object"
        )
    if workload.get("contract") != WORKLOAD_CONTRACT:
        raise ValidationError(
            f"end-to-end matrix case {case_id} must use naturally connected upstream RTL"
        )
    if workload.get("platform_binding") != "replace-run-spec-platform":
        raise ValidationError(
            f"end-to-end matrix case {case_id} must replace the Phase 1 run platform"
        )
    catalog_id = _string(
        workload.get("catalog_id"), f"case {case_id} workload catalog_id"
    )
    if catalog_id not in catalog:
        raise ValidationError(
            f"end-to-end matrix case {case_id} workload is absent from rtl_catalog"
        )
    run_spec_path = _repo_file(
        root, workload.get("run_spec"), f"case {case_id} workload run_spec"
    )
    run_spec = BenchmarkRun.load(run_spec_path)
    if run_spec.value["design_id"] != catalog_id:
        raise ValidationError(
            f"end-to-end matrix case {case_id} workload catalog/run mismatch"
        )
    if "clock_periods_ns" not in run_spec.value:
        raise ValidationError(
            f"end-to-end matrix case {case_id} workload must freeze target "
            "clock_periods_ns in its run spec"
        )
    if "physical_mapping_profile" not in run_spec.value:
        raise ValidationError(
            f"end-to-end matrix case {case_id} workload must freeze its "
            "physical_mapping_profile in the run spec"
        )
    run_id = run_spec.value["id"].lower()
    if "x32" in run_id or "replicated" in run_id:
        raise ValidationError(
            f"end-to-end matrix case {case_id} uses a replicated/artificial workload"
        )

    platform = record.get("platform")
    if not isinstance(platform, dict):
        raise ValidationError(
            f"end-to-end matrix case {case_id} platform must be an object"
        )
    if platform.get("contract") != PLATFORM_CONTRACT:
        raise ValidationError(
            f"end-to-end matrix case {case_id} must use a contest-derived BoardDB"
        )
    if platform.get("claim_scope") != _CLAIM_SCOPE:
        raise ValidationError(
            f"end-to-end matrix case {case_id} platform claim scope is invalid"
        )
    contest_matrix_path = _repo_file(
        root,
        platform.get("contest_matrix"),
        f"case {case_id} contest_matrix",
    )
    contest_matrix, _ = load_contest_validation_matrix(contest_matrix_path)
    contest_case_id = _string(
        platform.get("contest_case_id"), f"case {case_id} contest_case_id"
    )
    contest_cases = {item["id"]: item for item in contest_matrix["cases"]}
    if contest_case_id not in contest_cases:
        raise ValidationError(
            f"end-to-end matrix case {case_id} contest case is not catalogued"
        )
    if "materialize-boarddb" not in contest_cases[contest_case_id]["target_gates"]:
        raise ValidationError(
            f"end-to-end matrix case {case_id} contest case cannot materialize BoardDB"
        )
    _repo_file(
        root,
        platform.get("device_template"),
        f"case {case_id} device_template",
    )
    workload_id = run_spec.value["id"].replace("_logic_only", "").replace("_", "-")
    expected_id = f"{workload_id}__{contest_case_id.replace('.', '-')}"
    if case_id != expected_id:
        raise ValidationError(
            f"end-to-end matrix case {case_id} id must equal {expected_id!r}"
        )

    _validate_evidence(record, case_id)
    return case_id, role, record["state"], catalog_id


def canonical_end_to_end_matrix_sha256(matrix: Mapping[str, Any]) -> str:
    payload = json.dumps(
        matrix, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_end_to_end_validation_matrix(
    matrix: Mapping[str, Any], repository_root: Path
) -> Dict[str, Any]:
    if not isinstance(matrix, dict):
        raise ValidationError("end-to-end validation matrix must be an object")
    if matrix.get("schema") != END_TO_END_VALIDATION_MATRIX_SCHEMA:
        raise ValidationError("end-to-end validation matrix schema is invalid")
    _validate_policy(matrix.get("policy"))

    catalog_path = _repo_file(
        repository_root, matrix.get("rtl_catalog"), "rtl_catalog"
    )
    catalog_value = read_json(catalog_path)
    if catalog_value.get("schema") != "emuflow.rtl-catalog/v1":
        raise ValidationError("end-to-end matrix RTL catalog schema is invalid")
    designs = catalog_value.get("designs")
    if not isinstance(designs, list) or not designs:
        raise ValidationError("end-to-end matrix RTL catalog has no designs")
    catalog = {
        _string(item.get("id"), "RTL catalog id"): item
        for item in designs
        if isinstance(item, dict)
    }

    cases = matrix.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValidationError("end-to-end validation matrix requires cases")
    validated = [
        _validate_case(record, repository_root, catalog) for record in cases
    ]
    case_ids = [item[0] for item in validated]
    if len(case_ids) != len(set(case_ids)):
        raise ValidationError("end-to-end matrix case IDs must be unique")
    if case_ids != sorted(case_ids):
        raise ValidationError("end-to-end matrix cases must be sorted by ID")

    def counts(index: int) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for item in validated:
            result[item[index]] = result.get(item[index], 0) + 1
        return dict(sorted(result.items()))

    return {
        "schema": END_TO_END_VALIDATION_MATRIX_SCHEMA,
        "case_count": len(cases),
        "roles": counts(1),
        "states": counts(2),
        "workloads": counts(3),
        "matrix_sha256": canonical_end_to_end_matrix_sha256(matrix),
    }


def load_end_to_end_validation_matrix(
    path: Path,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    matrix = read_json(path)
    root = path.resolve().parent.parent
    return matrix, validate_end_to_end_validation_matrix(matrix, root)
