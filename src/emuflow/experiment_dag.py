"""Content-addressed checkpoint reuse for staged validation experiments."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import EmuFlowError, ValidationError
from .experiment_identity import validate_implementation_closure
from .experiment_storage import (
    prepare_experiment_scratch,
    validate_experiment_write_path,
)
from .io import read_json, write_json
from .validation_farm import FARM_SPEC_SCHEMA


EXPERIMENT_SPEC_SCHEMA = "emuflow.experiment-dag-spec/v1"
EXPERIMENT_PLAN_SCHEMA = "emuflow.experiment-dag-plan/v1"
EXPERIMENT_CHECKPOINT_SCHEMA = "emuflow.experiment-checkpoint/v1"
EXPERIMENT_SPEC_V2_SCHEMA = "emuflow.experiment-dag-spec/v2"
EXPERIMENT_PLAN_V2_SCHEMA = "emuflow.experiment-dag-plan/v2"
EXPERIMENT_CHECKPOINT_V2_SCHEMA = "emuflow.experiment-checkpoint/v2"
EXPERIMENT_VALIDATION_SCHEMA = "emuflow.experiment-validation/v1"

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_PROVIDERS = ("baseline", "placement-aware", "chimew")
_ARTIFACT_RETENTION = {
    "consumer-checkpoint": "required",
    "evidence-critical": "required",
    "source-input": "required",
    "diagnostic": "optional",
    "failure-diagnostic": "optional",
    "regenerable-scratch": "prunable",
}
_TOKEN_RE = re.compile(
    r"\{(output_dir|artifact_root|dependency:([a-z0-9_.-]+))\}"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _portable_argv(
    arguments: list[str], bindings: Mapping[str, str]
) -> list[str]:
    """Replace byte-sealed runtime paths with stable input labels.

    The executable argv remains available for execution.  Identity argv must
    not depend on where an immutable tool or input was installed, because the
    corresponding input digest already binds its bytes.
    """

    reverse: Dict[str, str] = {}
    for label, value in bindings.items():
        if value not in reverse or label < reverse[value]:
            reverse[value] = label
    return [
        f"{{input:{reverse[argument]}}}" if argument in reverse else argument
        for argument in arguments
    ]


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"experiment {label} must be a non-empty string")
    return value


def _identifier(value: Any, label: str) -> str:
    result = _string(value, label)
    if _ID_RE.fullmatch(result) is None:
        raise ValidationError(
            f"experiment {label} may contain lowercase letters, digits, '.', '_', '-'; "
            f"got {result!r}"
        )
    return result


def _safe_relative(value: Any, label: str) -> str:
    result = _string(value, label)
    path = Path(result)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValidationError(f"experiment {label} must be a safe relative path")
    return path.as_posix()


def _safe_artifact(root: Path, relative: str) -> Path:
    root = root.resolve()
    path = root / relative
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValidationError(
                f"experiment checkpoint artifact uses a symlink: {relative}"
            )
    resolved = path.resolve()
    if root != resolved and root not in resolved.parents:
        raise ValidationError(
            f"experiment checkpoint artifact escapes its root: {relative}"
        )
    if not resolved.exists():
        raise ValidationError(
            f"experiment checkpoint artifact is missing: {relative}"
        )
    if not resolved.is_file() and not resolved.is_dir():
        raise ValidationError(
            f"experiment checkpoint artifact is not a file/directory: {relative}"
        )
    return resolved


def _artifact_digest(path: Path) -> tuple[str, str, int]:
    if path.is_file():
        return "file", _sha256(path), path.stat().st_size
    records = []
    total = 0
    for child in sorted(path.rglob("*")):
        relative = child.relative_to(path).as_posix()
        if child.is_symlink():
            raise ValidationError(
                f"experiment checkpoint directory contains symlink: {relative}"
            )
        if child.is_dir():
            records.append({"path": relative, "kind": "directory"})
        elif child.is_file():
            size = child.stat().st_size
            total += size
            records.append(
                {
                    "path": relative,
                    "kind": "file",
                    "bytes": size,
                    "sha256": _sha256(child),
                }
            )
        else:
            raise ValidationError(
                f"experiment checkpoint directory contains special file: {relative}"
            )
    return "directory", _canonical_sha256(records), total


def _validate_node(raw: Any, seen: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("experiment nodes must be objects")
    node_id = _identifier(raw.get("id"), "node id")
    stage = _identifier(raw.get("stage"), f"node {node_id} stage")
    dependencies = raw.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) and item for item in dependencies
    ):
        raise ValidationError(
            f"experiment node {node_id} dependencies must be a string list"
        )
    if len(dependencies) != len(set(dependencies)):
        raise ValidationError(f"experiment node {node_id} dependencies are duplicated")
    if any(dependency not in seen for dependency in dependencies):
        raise ValidationError(
            f"experiment node {node_id} dependencies must precede the node"
        )
    provider = raw.get("provider")
    if provider is not None:
        if provider not in _PROVIDERS:
            raise ValidationError(f"experiment node {node_id} provider is invalid")
    seed = raw.get("physical_seed")
    if seed is not None and (
        isinstance(seed, bool) or not isinstance(seed, int) or seed < 0
    ):
        raise ValidationError(
            f"experiment node {node_id} physical seed is invalid"
        )
    if stage == "shared-phase1-5" and (
        dependencies or provider is not None or seed is not None
    ):
        raise ValidationError(
            "shared Phase 1-5 checkpoint cannot have dependencies/provider/seed"
        )
    if stage == "phase6" and (provider is None or seed is not None):
        raise ValidationError(
            f"experiment Phase 6 node {node_id} requires provider and no physical seed"
        )
    if stage == "phase7":
        if provider is None or seed is None:
            raise ValidationError(
                f"experiment Phase 7 node {node_id} requires provider and physical seed"
            )
        matching_phase6 = [
            seen[dependency]
            for dependency in dependencies
            if seen[dependency]["stage"] == "phase6"
            and seen[dependency].get("provider") == provider
        ]
        if len(matching_phase6) != 1:
            raise ValidationError(
                f"experiment Phase 7 node {node_id} must depend on exactly one "
                "matching Phase 6 provider checkpoint"
            )

    inputs = raw.get("inputs", {})
    if not isinstance(inputs, dict) or not all(
        isinstance(key, str)
        and isinstance(value, str)
        and _DIGEST_RE.fullmatch(value) is not None
        for key, value in inputs.items()
    ):
        raise ValidationError(
            f"experiment node {node_id} inputs must map labels to SHA-256 values"
        )
    if not dependencies and not inputs:
        raise ValidationError(
            f"experiment root node {node_id} requires explicit input hashes"
        )
    configuration = raw.get("configuration", {})
    if not isinstance(configuration, dict):
        raise ValidationError(
            f"experiment node {node_id} configuration must be an object"
        )
    if stage == "phase7":
        backend = configuration.get("physical_backend")
        workers = configuration.get("physical_workers")
        if not isinstance(backend, str) or not backend:
            raise ValidationError(
                f"experiment Phase 7 node {node_id} must seal physical_backend"
            )
        if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
            raise ValidationError(
                f"experiment Phase 7 node {node_id} must seal physical_workers"
            )
    command = raw.get("command")
    if not isinstance(command, list) or not command or not all(
        isinstance(item, str) and item for item in command
    ):
        raise ValidationError(
            f"experiment node {node_id} command must be a non-empty argv list"
        )
    if not any("{output_dir}" in item for item in command):
        raise ValidationError(
            f"experiment node {node_id} command must reference {{output_dir}}"
        )
    for argument in command:
        stripped = _TOKEN_RE.sub("", argument)
        if "{" in stripped or "}" in stripped:
            raise ValidationError(
                f"experiment node {node_id} command contains unknown placeholder"
            )
        for _, dependency in _TOKEN_RE.findall(argument):
            if dependency and dependency not in dependencies:
                raise ValidationError(
                    f"experiment node {node_id} command references undeclared dependency"
                )
        if "{artifact_root}" in argument:
            raise ValidationError(
                f"experiment node {node_id} command cannot use {{artifact_root}}"
            )
    validator = raw.get("validator")
    if not isinstance(validator, list) or not validator or not all(
        isinstance(item, str) and item for item in validator
    ):
        raise ValidationError(
            f"experiment node {node_id} validator must be a non-empty argv list"
        )
    if not any("{artifact_root}" in item for item in validator):
        raise ValidationError(
            f"experiment node {node_id} validator must reference {{artifact_root}}"
        )
    for argument in validator:
        stripped = _TOKEN_RE.sub("", argument)
        if "{" in stripped or "}" in stripped or "{output_dir}" in argument:
            raise ValidationError(
                f"experiment node {node_id} validator contains an invalid placeholder"
            )
        for _, dependency in _TOKEN_RE.findall(argument):
            if dependency and dependency not in dependencies:
                raise ValidationError(
                    f"experiment node {node_id} validator references undeclared dependency"
                )
    artifacts = raw.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValidationError(f"experiment node {node_id} requires artifacts")
    artifact_paths = [
        _safe_relative(item, f"node {node_id} artifact") for item in artifacts
    ]
    if len(artifact_paths) != len(set(artifact_paths)):
        raise ValidationError(f"experiment node {node_id} artifacts are duplicated")
    environment = raw.get("environment", {})
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environment.items()
    ):
        raise ValidationError(
            f"experiment node {node_id} environment must map strings to strings"
        )
    result: Dict[str, Any] = {
        "id": node_id,
        "stage": stage,
        "dependencies": list(dependencies),
        "inputs": dict(sorted(inputs.items())),
        "configuration": configuration,
        "command": list(command),
        "validator": list(validator),
        "environment": dict(sorted(environment.items())),
        "artifacts": artifact_paths,
    }
    if provider is not None:
        result["provider"] = provider
    if raw.get("physical_seed") is not None:
        result["physical_seed"] = raw["physical_seed"]
    return result


def _validate_artifact_records(raw: Any, node_id: str) -> list[Dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise ValidationError(f"experiment node {node_id} requires artifacts")
    records = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValidationError(
                f"experiment node {node_id} artifact {index} must be an object"
            )
        path = _safe_relative(item.get("path"), f"node {node_id} artifact path")
        role = _string(item.get("role"), f"node {node_id} artifact role")
        if role not in _ARTIFACT_RETENTION:
            raise ValidationError(
                f"experiment node {node_id} artifact role is invalid: {role!r}"
            )
        retention = item.get("retention", _ARTIFACT_RETENTION[role])
        if retention != _ARTIFACT_RETENTION[role]:
            raise ValidationError(
                f"experiment node {node_id} artifact {path} retention disagrees "
                f"with role {role}"
            )
        records.append({"path": path, "role": role, "retention": retention})
    paths = [item["path"] for item in records]
    if len(paths) != len(set(paths)):
        raise ValidationError(f"experiment node {node_id} artifacts are duplicated")
    return records


def _validate_node_v2(
    raw: Any, seen: Mapping[str, Mapping[str, Any]]
) -> Dict[str, Any]:
    """Validate one generic v2 stage without imposing a Phase 6 study shape."""

    if not isinstance(raw, dict):
        raise ValidationError("experiment nodes must be objects")
    node_id = _identifier(raw.get("id"), "node id")
    stage = _identifier(raw.get("stage"), f"node {node_id} stage")
    dependencies = raw.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) and item for item in dependencies
    ):
        raise ValidationError(
            f"experiment node {node_id} dependencies must be a string list"
        )
    if len(dependencies) != len(set(dependencies)):
        raise ValidationError(f"experiment node {node_id} dependencies are duplicated")
    if any(dependency not in seen for dependency in dependencies):
        raise ValidationError(
            f"experiment node {node_id} dependencies must precede the node"
        )

    provider = raw.get("provider")
    if provider is not None and provider not in _PROVIDERS:
        raise ValidationError(f"experiment node {node_id} provider is invalid")
    seed = raw.get("physical_seed")
    if seed is not None and (
        isinstance(seed, bool) or not isinstance(seed, int) or seed < 0
    ):
        raise ValidationError(
            f"experiment node {node_id} physical seed is invalid"
        )

    inputs = raw.get("inputs", {})
    if not isinstance(inputs, dict) or not all(
        isinstance(key, str)
        and isinstance(value, str)
        and _DIGEST_RE.fullmatch(value) is not None
        for key, value in inputs.items()
    ):
        raise ValidationError(
            f"experiment node {node_id} inputs must map labels to SHA-256 values"
        )
    if not dependencies and not inputs:
        raise ValidationError(
            f"experiment root node {node_id} requires explicit input hashes"
        )
    configuration = raw.get("configuration", {})
    if not isinstance(configuration, dict):
        raise ValidationError(
            f"experiment node {node_id} configuration must be an object"
        )
    command = raw.get("command")
    if not isinstance(command, list) or not command or not all(
        isinstance(item, str) and item for item in command
    ):
        raise ValidationError(
            f"experiment node {node_id} command must be a non-empty argv list"
        )
    if not any("{output_dir}" in item for item in command):
        raise ValidationError(
            f"experiment node {node_id} command must reference {{output_dir}}"
        )
    validator = raw.get("validator")
    if not isinstance(validator, list) or not validator or not all(
        isinstance(item, str) and item for item in validator
    ):
        raise ValidationError(
            f"experiment node {node_id} validator must be a non-empty argv list"
        )
    if not any("{artifact_root}" in item for item in validator):
        raise ValidationError(
            f"experiment node {node_id} validator must reference {{artifact_root}}"
        )
    for label, arguments in (("command", command), ("validator", validator)):
        for argument in arguments:
            stripped = _TOKEN_RE.sub("", argument)
            if "{" in stripped or "}" in stripped:
                raise ValidationError(
                    f"experiment node {node_id} {label} contains unknown placeholder"
                )
            if label == "command" and "{artifact_root}" in argument:
                raise ValidationError(
                    f"experiment node {node_id} command cannot use {{artifact_root}}"
                )
            if label == "validator" and "{output_dir}" in argument:
                raise ValidationError(
                    f"experiment node {node_id} validator cannot use {{output_dir}}"
                )
            for _, dependency in _TOKEN_RE.findall(argument):
                if dependency and dependency not in dependencies:
                    raise ValidationError(
                        f"experiment node {node_id} {label} references undeclared dependency"
                    )

    execution_bindings = raw.get("execution_bindings", {})
    if not isinstance(execution_bindings, dict) or not all(
        isinstance(label, str)
        and label in inputs
        and isinstance(value, str)
        and value
        for label, value in execution_bindings.items()
    ):
        raise ValidationError(
            f"experiment node {node_id} execution_bindings must map input labels "
            "to non-empty runtime values"
        )
    portable_command = _portable_argv(command, execution_bindings)
    portable_validator = _portable_argv(validator, execution_bindings)
    command_identity = raw.get("command_identity")
    validator_identity = raw.get("validator_identity")
    identity_declared = (
        bool(execution_bindings)
        or command_identity is not None
        or validator_identity is not None
    )
    if identity_declared and (
        command_identity is None or validator_identity is None
    ):
        raise ValidationError(
            f"experiment node {node_id} must declare execution_bindings, "
            "command_identity, and validator_identity together"
        )
    if command_identity is not None and command_identity != portable_command:
        raise ValidationError(
            f"experiment node {node_id} command_identity disagrees with its "
            "byte-sealed execution bindings"
        )
    if validator_identity is not None and validator_identity != portable_validator:
        raise ValidationError(
            f"experiment node {node_id} validator_identity disagrees with its "
            "byte-sealed execution bindings"
        )

    environment = raw.get("environment", {})
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environment.items()
    ):
        raise ValidationError(
            f"experiment node {node_id} environment must map strings to strings"
        )
    storage_estimate = raw.get("storage_estimate")
    if not isinstance(storage_estimate, dict):
        raise ValidationError(
            f"experiment node {node_id} requires a storage_estimate"
        )
    peak_bytes = storage_estimate.get("peak_bytes")
    retained_bytes = storage_estimate.get("retained_bytes")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (peak_bytes, retained_bytes)
    ) or retained_bytes > peak_bytes:
        raise ValidationError(
            f"experiment node {node_id} storage_estimate is invalid"
        )
    result: Dict[str, Any] = {
        "id": node_id,
        "stage": stage,
        "dependencies": list(dependencies),
        "inputs": dict(sorted(inputs.items())),
        "configuration": configuration,
        "implementation": validate_implementation_closure(
            raw.get("implementation")
        ),
        "command": list(command),
        "validator_implementation": validate_implementation_closure(
            raw.get("validator_implementation")
        ),
        "validator": list(validator),
        "environment": dict(sorted(environment.items())),
        "storage_estimate": {
            "peak_bytes": peak_bytes,
            "retained_bytes": retained_bytes,
        },
        "artifacts": _validate_artifact_records(raw.get("artifacts"), node_id),
    }
    if identity_declared:
        result["execution_bindings"] = dict(sorted(execution_bindings.items()))
        result["command_identity"] = list(command_identity)
        result["validator_identity"] = list(validator_identity)
    if provider is not None:
        result["provider"] = provider
    if seed is not None:
        result["physical_seed"] = seed
    result["implementation_sha256"] = result["implementation"][
        "implementation_sha256"
    ]
    result["validator_sha256"] = result["validator_implementation"][
        "implementation_sha256"
    ]
    return result


def validate_experiment_spec(value: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") not in {
        EXPERIMENT_SPEC_SCHEMA,
        EXPERIMENT_SPEC_V2_SCHEMA,
    }:
        raise ValidationError("experiment DAG spec schema is invalid")
    schema = value["schema"]
    experiment_id = _identifier(value.get("experiment_id"), "experiment_id")
    source_commit = _string(value.get("source_commit"), "source_commit").lower()
    if _COMMIT_RE.fullmatch(source_commit) is None:
        raise ValidationError("experiment source_commit must be full 40-hex")
    nodes = value.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValidationError("experiment DAG spec requires nodes")
    normalized: Dict[str, Dict[str, Any]] = {}
    ordered = []
    for raw in nodes:
        node = (
            _validate_node_v2(raw, normalized)
            if schema == EXPERIMENT_SPEC_V2_SCHEMA
            else _validate_node(raw, normalized)
        )
        if node["id"] in normalized:
            raise ValidationError("experiment node IDs must be unique")
        normalized[node["id"]] = node
        ordered.append(node)
    if schema == EXPERIMENT_SPEC_SCHEMA:
        phase6 = [node for node in ordered if node["stage"] == "phase6"]
        phase7 = [node for node in ordered if node["stage"] == "phase7"]
        if len({node["provider"] for node in phase6}) != len(phase6):
            raise ValidationError("experiment has duplicate Phase 6 provider checkpoints")
        phase7_arms = {(node["provider"], node["physical_seed"]) for node in phase7}
        if len(phase7_arms) != len(phase7):
            raise ValidationError("experiment has duplicate Phase 7 provider/seed arms")
    return {
        "schema": schema,
        "experiment_id": experiment_id,
        "source_commit": source_commit,
        "nodes": ordered,
    }


def _node_key(
    source_commit: str,
    node: Mapping[str, Any],
    dependency_keys: Mapping[str, str],
) -> str:
    identity = {
        "schema": "emuflow.experiment-node-identity/v1",
        "source_commit": source_commit,
        "node": node,
        "dependency_keys": dict(sorted(dependency_keys.items())),
    }
    return _canonical_sha256(identity)


def _v2_execution_key(
    node: Mapping[str, Any], dependency_keys: Mapping[str, str]
) -> str:
    identity = {
        "schema": "emuflow.experiment-execution-identity/v2",
        "stage": node["stage"],
        "inputs": node["inputs"],
        "configuration": node["configuration"],
        "implementation_sha256": node["implementation_sha256"],
        "command": node.get("command_identity", node["command"]),
        "environment": node["environment"],
        "artifacts": node["artifacts"],
        "dependency_keys": dict(sorted(dependency_keys.items())),
    }
    if node.get("provider") is not None:
        identity["provider"] = node["provider"]
    if node.get("physical_seed") is not None:
        identity["physical_seed"] = node["physical_seed"]
    return _canonical_sha256(identity)


def _v2_validation_key(node: Mapping[str, Any], execution_key: str) -> str:
    return _canonical_sha256(
        {
            "schema": "emuflow.experiment-validation-identity/v1",
            "execution_key": execution_key,
            "validator_sha256": node["validator_sha256"],
            "validator": node.get("validator_identity", node["validator"]),
        }
    )


def _artifact_paths(node: Mapping[str, Any]) -> list[str]:
    artifacts = node["artifacts"]
    if artifacts and isinstance(artifacts[0], dict):
        return [item["path"] for item in artifacts]
    return list(artifacts)


def _checkpoint_manifest(cache_root: Path, key: str) -> Path:
    return cache_root / "objects" / key / "checkpoint.json"


def validate_experiment_checkpoint(
    manifest_path: Path,
    *,
    expected_key: Optional[str] = None,
    verify_artifact_content: bool = True,
) -> Dict[str, Any]:
    value = read_json(manifest_path)
    schema = value.get("schema")
    if schema not in {EXPERIMENT_CHECKPOINT_SCHEMA, EXPERIMENT_CHECKPOINT_V2_SCHEMA}:
        raise ValidationError("experiment checkpoint schema is invalid")
    key_field = "execution_key" if schema == EXPERIMENT_CHECKPOINT_V2_SCHEMA else "key"
    key = _string(value.get(key_field), "checkpoint key")
    if _DIGEST_RE.fullmatch(key) is None or (
        expected_key is not None and key != expected_key
    ):
        raise ValidationError("experiment checkpoint key is invalid")
    output_dir = Path(_string(value.get("output_dir"), "checkpoint output_dir"))
    if not output_dir.is_absolute() or output_dir.is_symlink() or not output_dir.is_dir():
        raise ValidationError("experiment checkpoint output directory is invalid")
    artifacts = value.get("artifacts")
    expected_records = value.get("expected_artifacts")
    expected = (
        [item.get("path") for item in expected_records]
        if schema == EXPERIMENT_CHECKPOINT_V2_SCHEMA
        and isinstance(expected_records, list)
        and all(isinstance(item, dict) for item in expected_records)
        else expected_records
    )
    if not isinstance(artifacts, dict) or not isinstance(expected, list):
        raise ValidationError("experiment checkpoint artifact table is invalid")
    if sorted(artifacts) != sorted(expected):
        raise ValidationError("experiment checkpoint artifact coverage is incomplete")
    for relative in sorted(expected):
        record = artifacts.get(relative)
        if not isinstance(record, dict):
            raise ValidationError("experiment checkpoint artifact record is invalid")
        path = _safe_artifact(output_dir, relative)
        if verify_artifact_content:
            kind, digest, size = _artifact_digest(path)
            if record != {"kind": kind, "sha256": digest, "bytes": size}:
                raise ValidationError(
                    f"experiment checkpoint artifact seal is broken: {relative}"
                )
        else:
            if (
                sorted(record) != ["bytes", "kind", "sha256"]
                or record.get("kind") not in {"file", "directory"}
                or not isinstance(record.get("bytes"), int)
                or record["bytes"] < 0
                or not isinstance(record.get("sha256"), str)
                or _DIGEST_RE.fullmatch(record["sha256"]) is None
                or (record["kind"] == "file" and not path.is_file())
                or (record["kind"] == "directory" and not path.is_dir())
            ):
                raise ValidationError(
                    f"experiment checkpoint artifact record is invalid: {relative}"
                )
    if value.get("status") != "pass":
        raise ValidationError("experiment checkpoint did not pass")
    if value.get("storage") == "managed" and (
        schema == EXPERIMENT_CHECKPOINT_V2_SCHEMA
        or value.get("output_immutable") is True
    ):
        if value.get("output_immutable") is not True:
            raise ValidationError("managed checkpoint lacks immutable-output seal")
        _validate_tree_immutable(output_dir)
    return value


def _validation_manifest(cache_root: Path, key: str, validation_key: str) -> Path:
    return cache_root / "objects" / key / "validations" / f"{validation_key}.json"


def _validated_certificate(
    cache_root: Path, key: str, validation_key: str
) -> Optional[Dict[str, Any]]:
    path = _validation_manifest(cache_root, key, validation_key)
    if not path.is_file():
        return None
    value = read_json(path)
    if value != {
        "schema": EXPERIMENT_VALIDATION_SCHEMA,
        "execution_key": key,
        "validation_key": validation_key,
        "status": "pass",
    }:
        raise ValidationError("experiment validation certificate is invalid")
    return value


def _cached_checkpoint(cache_root: Path, key: str) -> Optional[Dict[str, Any]]:
    manifest = _checkpoint_manifest(cache_root, key)
    if not manifest.is_file():
        return None
    value = read_json(manifest)
    managed_immutable = (
        value.get("storage") == "managed"
        and value.get("output_immutable") is True
        and manifest.stat().st_mode & 0o222 == 0
    )
    return validate_experiment_checkpoint(
        manifest,
        expected_key=key,
        verify_artifact_content=not managed_immutable,
    )


def plan_experiment(
    spec_path: Path, cache_root: Path, output_path: Path
) -> Dict[str, Any]:
    output_path = validate_experiment_write_path(output_path)
    spec_raw = read_json(spec_path)
    spec = validate_experiment_spec(spec_raw)
    cache_root = validate_experiment_write_path(cache_root)
    keys: Dict[str, str] = {}
    validation_keys: Dict[str, str] = {}
    states: Dict[str, str] = {}
    records = []
    for node in spec["nodes"]:
        dependency_keys = {dependency: keys[dependency] for dependency in node["dependencies"]}
        v2 = spec["schema"] == EXPERIMENT_SPEC_V2_SCHEMA
        key = (
            _v2_execution_key(node, dependency_keys)
            if v2
            else _node_key(spec["source_commit"], node, dependency_keys)
        )
        validation_key = _v2_validation_key(node, key) if v2 else None
        keys[node["id"]] = key
        if validation_key is not None:
            validation_keys[node["id"]] = validation_key
        cached = _cached_checkpoint(cache_root, key)
        if cached is not None:
            state = (
                "reuse"
                if not v2 or _validated_certificate(cache_root, key, validation_key) is not None
                else "revalidate"
            )
            output_dir = cached["output_dir"]
        elif all(states[dependency] == "reuse" for dependency in node["dependencies"]):
            state = "ready"
            output_dir = str((cache_root / "objects" / key / "output").resolve())
        else:
            state = "waiting"
            output_dir = str((cache_root / "objects" / key / "output").resolve())
        states[node["id"]] = state
        records.append(
            {
                **node,
                "key": key,
                **({"execution_key": key, "validation_key": validation_key} if v2 else {}),
                "dependency_keys": dependency_keys,
                **(
                    {
                        "dependency_validation_keys": {
                            dependency: validation_keys[dependency]
                            for dependency in node["dependencies"]
                        }
                    }
                    if v2
                    else {}
                ),
                "state": state,
                "output_dir": output_dir,
            }
        )
    plan = {
        "schema": (
            EXPERIMENT_PLAN_V2_SCHEMA
            if spec["schema"] == EXPERIMENT_SPEC_V2_SCHEMA
            else EXPERIMENT_PLAN_SCHEMA
        ),
        "experiment_id": spec["experiment_id"],
        "source_commit": spec["source_commit"],
        "spec_sha256": _canonical_sha256(spec_raw),
        "cache_root": str(cache_root),
        "nodes": records,
        "counts": {
            state: sum(item["state"] == state for item in records)
            for state in ("reuse", "revalidate", "ready", "waiting")
            if spec["schema"] == EXPERIMENT_SPEC_V2_SCHEMA or state != "revalidate"
        },
    }
    write_json(output_path, plan)
    return plan


def _load_plan(path: Path, expected_sha256: Optional[str] = None) -> Dict[str, Any]:
    if expected_sha256 is not None:
        if _DIGEST_RE.fullmatch(expected_sha256) is None or _sha256(path) != expected_sha256:
            raise ValidationError("experiment plan seal is broken")
    value = read_json(path)
    if value.get("schema") not in {EXPERIMENT_PLAN_SCHEMA, EXPERIMENT_PLAN_V2_SCHEMA}:
        raise ValidationError("experiment plan schema is invalid")
    cache_root = Path(_string(value.get("cache_root"), "plan cache_root"))
    if not cache_root.is_absolute():
        raise ValidationError("experiment plan cache_root must be absolute")
    nodes = value.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValidationError("experiment plan requires nodes")
    return value


def _seal_checkpoint(
    cache_root: Path,
    node: Mapping[str, Any],
    output_dir: Path,
    *,
    storage: str,
) -> Dict[str, Any]:
    object_root = cache_root / "objects" / node["key"]
    object_root.mkdir(parents=True, exist_ok=True)
    manifest = _checkpoint_value(
        node,
        output_dir,
        output_dir,
        storage=storage,
    )
    manifest_path = object_root / "checkpoint.json"
    write_json(manifest_path, manifest)
    if storage == "managed":
        manifest_path.chmod(0o444)
    return validate_experiment_checkpoint(
        manifest_path,
        expected_key=node["key"],
        verify_artifact_content=storage != "managed",
    )


def _checkpoint_value(
    node: Mapping[str, Any],
    digest_root: Path,
    declared_output_dir: Path,
    *,
    storage: str,
) -> Dict[str, Any]:
    artifacts: Dict[str, Any] = {}
    for relative in _artifact_paths(node):
        kind, digest, size = _artifact_digest(_safe_artifact(digest_root, relative))
        artifacts[relative] = {"kind": kind, "sha256": digest, "bytes": size}
    v2 = "validation_key" in node
    manifest: Dict[str, Any] = {
        "schema": (
            EXPERIMENT_CHECKPOINT_V2_SCHEMA if v2 else EXPERIMENT_CHECKPOINT_SCHEMA
        ),
        "node_id": node["id"],
        "stage": node["stage"],
        "provider": node.get("provider"),
        "physical_seed": node.get("physical_seed"),
        "dependency_keys": node["dependency_keys"],
        "storage": storage,
        "output_dir": str(declared_output_dir.resolve()),
        "output_immutable": storage == "managed",
        "expected_artifacts": node["artifacts"],
        "artifacts": artifacts,
        "status": "pass",
    }
    manifest["execution_key" if v2 else "key"] = node["key"]
    return manifest


def _make_tree_immutable(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            raise ValidationError("managed checkpoint output contains a symlink")
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def _validate_tree_immutable(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if path.stat().st_mode & 0o222:
            raise ValidationError(
                f"managed checkpoint output is writable: {path}"
            )


def _is_cache_resident_output(cache_root: Path, output_dir: Path) -> bool:
    """Return whether an imported output is an object-store-owned tree."""

    try:
        relative = output_dir.resolve().relative_to((cache_root / "objects").resolve())
    except ValueError:
        return False
    return (
        len(relative.parts) == 2
        and _DIGEST_RE.fullmatch(relative.parts[0]) is not None
        and relative.parts[1] == "output"
        and not output_dir.is_symlink()
    )


def _seal_validation(cache_root: Path, node: Mapping[str, Any]) -> Dict[str, Any]:
    value = {
        "schema": EXPERIMENT_VALIDATION_SCHEMA,
        "execution_key": node["key"],
        "validation_key": node["validation_key"],
        "status": "pass",
    }
    path = _validation_manifest(cache_root, node["key"], node["validation_key"])
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, value)
    return _validated_certificate(
        cache_root, node["key"], node["validation_key"]
    ) or value


def _dependency_outputs(
    node: Mapping[str, Any], cache_root: Path
) -> Dict[str, str]:
    result = {}
    for dependency, key in node["dependency_keys"].items():
        checkpoint = _cached_checkpoint(cache_root, key)
        if checkpoint is None:
            raise ValidationError(
                f"experiment node {node['id']} dependency {dependency} is not cached"
            )
        validation_key = node.get("dependency_validation_keys", {}).get(dependency)
        if validation_key is not None and _validated_certificate(
            cache_root, key, validation_key
        ) is None:
            raise ValidationError(
                f"experiment node {node['id']} dependency {dependency} is not validated"
            )
        result[dependency] = checkpoint["output_dir"]
    return result


def _expand_argv(
    template: list[str],
    dependency_outputs: Mapping[str, str],
    *,
    output_dir: Optional[Path] = None,
    artifact_root: Optional[Path] = None,
) -> list[str]:
    def replace(argument: str) -> str:
        def token(match: re.Match[str]) -> str:
            label = match.group(1)
            if label == "output_dir":
                if output_dir is None:
                    raise ValidationError("experiment argv lacks output_dir binding")
                return str(output_dir)
            if label == "artifact_root":
                if artifact_root is None:
                    raise ValidationError("experiment argv lacks artifact_root binding")
                return str(artifact_root)
            return dependency_outputs[match.group(2)]

        return _TOKEN_RE.sub(token, argument)

    return [replace(argument) for argument in template]


def _run_validator(
    node: Mapping[str, Any], cache_root: Path, artifact_root: Path
) -> subprocess.CompletedProcess[bytes]:
    dependencies = _dependency_outputs(node, cache_root)
    command = _expand_argv(
        node["validator"], dependencies, artifact_root=artifact_root
    )
    scratch_root = cache_root / "scratch" / (
        f"validator-{node['key']}-{os.getpid()}-{time.monotonic_ns()}"
    )
    _, scratch_environment = prepare_experiment_scratch(scratch_root)
    environment = os.environ.copy()
    environment.update(node["environment"])
    environment.update(scratch_environment)
    try:
        return subprocess.run(
            command,
            cwd=artifact_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    finally:
        shutil.rmtree(scratch_root, ignore_errors=True)


def import_experiment_checkpoint(
    plan_path: Path,
    node_id: str,
    artifact_root: Path,
    *,
    expected_plan_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    plan = _load_plan(plan_path, expected_plan_sha256)
    nodes = {item["id"]: item for item in plan["nodes"]}
    if node_id not in nodes:
        raise ValidationError(f"experiment plan has no node {node_id!r}")
    node = nodes[node_id]
    cache_root = validate_experiment_write_path(Path(plan["cache_root"]))
    existing = _cached_checkpoint(cache_root, node["key"])
    if existing is not None:
        promoted_existing = False
        if (
            existing.get("storage") == "external-validated"
            and _is_cache_resident_output(
                cache_root, Path(existing["output_dir"])
            )
        ):
            # The full-content check in _cached_checkpoint() just proved this
            # legacy alias still matches its seal.  Make the object-store-owned
            # tree immutable and publish the same digest table as a managed
            # alias so subsequent planning is metadata-only.
            output_dir = Path(existing["output_dir"])
            _make_tree_immutable(output_dir)
            promoted = {
                **existing,
                "storage": "managed",
                "output_immutable": True,
            }
            manifest = _checkpoint_manifest(cache_root, node["key"])
            manifest.chmod(0o644)
            write_json(manifest, promoted)
            manifest.chmod(0o444)
            existing = validate_experiment_checkpoint(
                manifest,
                expected_key=node["key"],
                verify_artifact_content=False,
            )
            promoted_existing = True
        if "validation_key" not in node or _validated_certificate(
            cache_root, node["key"], node["validation_key"]
        ) is not None:
            return {
                "status": "promoted" if promoted_existing else "reused",
                "checkpoint": existing,
            }
        validation = _run_validator(node, cache_root, Path(existing["output_dir"]))
        if validation.returncode != 0:
            raise ValidationError(
                f"experiment node {node_id} independent validator failed: "
                f"{validation.stderr.decode('utf-8', errors='replace')[-2048:]}"
            )
        certificate = _seal_validation(cache_root, node)
        return {
            "status": "revalidated",
            "checkpoint": existing,
            "validation": certificate,
        }
    for dependency, key in node["dependency_keys"].items():
        if _cached_checkpoint(cache_root, key) is None:
            raise ValidationError(
                f"experiment node {node_id} dependency {dependency} is not cached"
            )
    artifact_root = artifact_root.expanduser().resolve()
    validation = _run_validator(node, cache_root, artifact_root)
    if validation.returncode != 0:
        raise ValidationError(
            f"experiment node {node_id} independent validator failed: "
            f"{validation.stderr.decode('utf-8', errors='replace')[-2048:]}"
        )
    managed_alias = _is_cache_resident_output(cache_root, artifact_root)
    if managed_alias:
        _make_tree_immutable(artifact_root)
    checkpoint = _seal_checkpoint(
        cache_root,
        node,
        artifact_root,
        storage="managed" if managed_alias else "external-validated",
    )
    result = {"status": "imported", "checkpoint": checkpoint}
    if "validation_key" in node:
        result["validation"] = _seal_validation(cache_root, node)
    return result


def _expand_command(
    node: Mapping[str, Any], cache_root: Path, output_dir: Path
) -> list[str]:
    return _expand_argv(
        node["command"],
        _dependency_outputs(node, cache_root),
        output_dir=output_dir,
    )


def run_experiment_node(
    plan_path: Path,
    node_id: str,
    run_dir: Path,
    *,
    expected_plan_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    plan = _load_plan(plan_path, expected_plan_sha256)
    nodes = {item["id"]: item for item in plan["nodes"]}
    if node_id not in nodes:
        raise ValidationError(f"experiment plan has no node {node_id!r}")
    node = nodes[node_id]
    cache_root = validate_experiment_write_path(Path(plan["cache_root"]))
    run_dir = validate_experiment_write_path(run_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    (cache_root / "locks").mkdir(exist_ok=True)
    lock = (cache_root / "locks" / f"{node['key']}.lock").open("a+")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    try:
        existing = _cached_checkpoint(cache_root, node["key"])
        if existing is not None:
            if "validation_key" in node and _validated_certificate(
                cache_root, node["key"], node["validation_key"]
            ) is None:
                validation = _run_validator(
                    node, cache_root, Path(existing["output_dir"])
                )
                if validation.returncode != 0:
                    report = {
                        "status": "failed",
                        "node_id": node_id,
                        "exit_code": validation.returncode,
                        "failure_stage": "independent-revalidation",
                    }
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "validator.stdout.log").write_bytes(validation.stdout)
                    (run_dir / "validator.stderr.log").write_bytes(validation.stderr)
                    write_json(run_dir / "experiment-node-report.json", report)
                    return report
                certificate = _seal_validation(cache_root, node)
                report = {
                    "status": "revalidated",
                    "node_id": node_id,
                    "checkpoint": existing,
                    "validation": certificate,
                }
                run_dir.mkdir(parents=True, exist_ok=True)
                write_json(run_dir / "experiment-node-report.json", report)
                return report
            report = {"status": "reused", "node_id": node_id, "checkpoint": existing}
            run_dir.mkdir(parents=True, exist_ok=True)
            write_json(run_dir / "experiment-node-report.json", report)
            return report
        object_root = cache_root / "objects" / node["key"]
        if object_root.exists():
            raise EmuFlowError(
                f"experiment cache object is incomplete; preserve and inspect: {object_root}"
            )
        staging = cache_root / "staging" / f"{node['key']}.{os.getpid()}"
        staging.mkdir(parents=True, exist_ok=False)
        output_dir = staging / "output"
        output_dir.mkdir()
        run_dir.mkdir(parents=True, exist_ok=True)
        _, scratch_environment = prepare_experiment_scratch(run_dir)
        command = _expand_command(node, cache_root, output_dir)
        environment = os.environ.copy()
        environment.update(node["environment"])
        environment.update(scratch_environment)
        started = time.monotonic()
        with (run_dir / "command.stdout.log").open("wb") as stdout, (
            run_dir / "command.stderr.log"
        ).open("wb") as stderr:
            completed = subprocess.run(
                command,
                cwd=output_dir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
            )
        if completed.returncode != 0:
            failure_root = cache_root / "failures" / staging.name
            failure_root.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(failure_root)
            report = {
                "status": "failed",
                "node_id": node_id,
                "exit_code": completed.returncode,
                "elapsed_seconds": time.monotonic() - started,
                "failure_root": str(failure_root),
            }
            write_json(run_dir / "experiment-node-report.json", report)
            return report
        validation = _run_validator(node, cache_root, output_dir)
        if validation.returncode != 0:
            failure_root = cache_root / "failures" / staging.name
            failure_root.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(failure_root)
            (run_dir / "validator.stdout.log").write_bytes(validation.stdout)
            (run_dir / "validator.stderr.log").write_bytes(validation.stderr)
            report = {
                "status": "failed",
                "node_id": node_id,
                "exit_code": validation.returncode,
                "failure_stage": "independent-validator",
                "elapsed_seconds": time.monotonic() - started,
                "failure_root": str(failure_root),
            }
            write_json(run_dir / "experiment-node-report.json", report)
            return report
        final_root = cache_root / "objects" / node["key"]
        final_root.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_value = _checkpoint_value(
            node,
            output_dir,
            final_root / "output",
            storage="managed",
        )
        write_json(staging / "checkpoint.json", checkpoint_value)
        _make_tree_immutable(output_dir)
        (staging / "checkpoint.json").chmod(0o444)
        staging.rename(final_root)
        checkpoint = validate_experiment_checkpoint(
            final_root / "checkpoint.json",
            expected_key=node["key"],
            verify_artifact_content=False,
        )
        report = {
            "status": "pass",
            "node_id": node_id,
            "elapsed_seconds": time.monotonic() - started,
            "checkpoint": checkpoint,
        }
        if "validation_key" in node:
            report["validation"] = _seal_validation(cache_root, node)
        write_json(run_dir / "experiment-node-report.json", report)
        return report
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def build_experiment_farm_spec(
    plan_path: Path,
    install_dir: Path,
    nodes: list[str],
    farm_id: str,
    output_path: Path,
    experiment_nodes: Optional[list[str]] = None,
    worker_argv: Optional[list[str]] = None,
    *,
    worker_launcher: Optional[Path] = None,
) -> Dict[str, Any]:
    output_path = validate_experiment_write_path(output_path)
    plan = _load_plan(plan_path)
    ready = [
        item
        for item in plan["nodes"]
        if item["state"] in {"ready", "revalidate"}
    ]
    if not ready:
        raise EmuFlowError("experiment plan has no ready nodes; replan or finish dependencies")
    if experiment_nodes:
        if len(experiment_nodes) != len(set(experiment_nodes)):
            raise ValidationError(
                "experiment farm selected experiment nodes must be unique"
            )
        ready_by_id = {item["id"]: item for item in ready}
        unavailable = [item for item in experiment_nodes if item not in ready_by_id]
        if unavailable:
            raise ValidationError(
                "experiment farm selected nodes are not ready/revalidate: "
                + ", ".join(unavailable)
            )
        selected = set(experiment_nodes)
        ready = [item for item in ready if item["id"] in selected]
    if not nodes:
        raise ValidationError("experiment farm requires at least one HPC node")
    for node in nodes:
        _identifier(node, "HPC node")
    if len(nodes) != len(set(nodes)):
        raise ValidationError("experiment farm HPC nodes must be unique")
    if worker_launcher is not None and worker_argv is not None:
        raise ValidationError(
            "experiment farm worker launcher and worker argv are mutually exclusive"
        )
    if worker_argv is not None:
        if not isinstance(worker_argv, list) or not worker_argv:
            raise ValidationError(
                "experiment farm worker argv must be a non-empty string list"
            )
        if any(not isinstance(item, str) or not item.strip() for item in worker_argv):
            raise ValidationError(
                "experiment farm worker argv must contain non-empty strings"
            )
    plan_path = plan_path.resolve()
    plan_sha256 = _sha256(plan_path)
    launcher_binding = None
    if worker_launcher is not None:
        if not worker_launcher.is_absolute():
            raise ValidationError(
                "experiment farm worker launcher must be absolute"
            )
        if worker_launcher.is_symlink() or not worker_launcher.is_file():
            raise ValidationError(
                "experiment farm worker launcher must be a regular "
                "non-symlink file"
            )
        resolved_launcher = worker_launcher.resolve()
        launcher_binding = {
            "path": str(resolved_launcher),
            "sha256": _sha256(resolved_launcher),
        }
    spec = {
        "schema": FARM_SPEC_SCHEMA,
        "farm_id": _identifier(farm_id, "farm_id"),
        "source_commit": plan["source_commit"],
        "install_dir": str(install_dir.expanduser().resolve()),
        "nodes": nodes,
        "slots_per_node": 1,
        "tasks": [
            {
                "id": item["id"],
                "command": [
                    "{install}/bin/emuflow",
                    "experiment-cache",
                    "run-node",
                    "--plan",
                    str(plan_path),
                    "--expected-plan-sha256",
                    plan_sha256,
                    "--node",
                    item["id"],
                    "--run-dir",
                    "{run_dir}",
                ],
                "estimated_peak_bytes": item.get("storage_estimate", {}).get(
                    "peak_bytes", 0
                ),
            }
            for item in ready
        ],
    }
    if launcher_binding is not None:
        spec["worker_argv"] = [
            launcher_binding["path"],
            "{install}/bin/emuflow",
        ]
        spec["worker_launcher"] = launcher_binding
    elif worker_argv is not None:
        spec["worker_argv"] = list(worker_argv)
    write_json(output_path, spec)
    return {
        "status": "pass",
        "ready_tasks": len(ready),
        "revalidation_tasks": sum(
            item["state"] == "revalidate" for item in ready
        ),
        "reused_tasks": plan["counts"]["reuse"],
        "waiting_tasks": plan["counts"]["waiting"],
        "deferred_ready_tasks": sum(
            item["state"] in {"ready", "revalidate"} for item in plan["nodes"]
        )
        - len(ready),
        "plan_sha256": plan_sha256,
        "farm_spec": str(output_path),
    }
