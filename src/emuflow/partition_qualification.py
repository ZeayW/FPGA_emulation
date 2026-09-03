"""Compile a canonical real-RTL Phase 1--3 partition qualification DAG.

The full canonical experiment deliberately includes physical Phase 4--7 tools.
Partition-provider development needs the same real-RTL, contest-BoardDB, timing,
and content-addressed evidence contracts without pretending that the physical
stack was qualified.  This compiler emits exactly the reusable frontend,
timing, and partition checkpoints and seals every native MFSPart executable.
"""

from __future__ import annotations

import hashlib
import math
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from .canonical_experiment import _COMPONENTS, _canonical_case_contract
from .errors import ValidationError
from .experiment_dag import EXPERIMENT_SPEC_V2_SCHEMA, validate_experiment_spec
from .experiment_identity import build_implementation_closure
from .experiment_storage import validate_experiment_write_path
from .io import read_json, write_json


PARTITION_QUALIFICATION_CONFIG_SCHEMA = (
    "emuflow.partition-qualification-config/v1"
)

_MFSPART_COMPONENTS: Sequence[str] = (
    "src/emuflow/experiment_upstream.py::validate_timing_checkpoint",
    "src/emuflow/experiment_partition.py",
    "src/emuflow/experiment_stages.py::_prepare_empty_output",
    "src/emuflow/io.py",
    "src/emuflow/ir.py",
    "src/emuflow/mfspart.py",
    "src/emuflow/mfspart_initial.py",
    "src/emuflow/mfspart_legalize.py",
    "src/emuflow/mfspart_provider.py",
    "src/emuflow/mfspart_refine.py",
    "src/emuflow/native_tools.py",
    "src/emuflow/partition.py",
    "src/emuflow/partition_hops.py",
    "src/emuflow/phase3.py",
    "src/emuflow/platform.py",
    "src/emuflow/resources.py",
    "src/emuflow/routing.py",
    "src/native/hop_partition_refiner.cpp",
    "src/native/mfspart_coarsener.cpp",
    "src/native/mfspart_initializer.cpp",
    "src/native/mfspart_legalizer.cpp",
    "src/native/mfspart_refiner.cpp",
    "src/native/mfspart_refiner_checker.cpp",
)

_REQUIRED_TOOLS = (
    "emuflow",
    "hop_refiner",
    "mfspart_coarsener",
    "mfspart_initializer",
    "mfspart_legalizer",
    "mfspart_refiner",
    "mfspart_refiner_checker",
    "opensta",
    "yosys",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"partition qualification {label} must be a file path")
    supplied = Path(value).expanduser()
    if supplied.is_symlink():
        raise ValidationError(
            f"partition qualification {label} must not be a symbolic link"
        )
    path = supplied.resolve()
    if not path.is_file():
        raise ValidationError(
            f"partition qualification {label} is not a regular file"
        )
    return path


def _directory(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValidationError(
            f"partition qualification {label} must be a directory path"
        )
    supplied = Path(value).expanduser()
    if supplied.is_symlink():
        raise ValidationError(
            f"partition qualification {label} must not be a symbolic link"
        )
    path = supplied.resolve()
    if not path.is_dir():
        raise ValidationError(f"partition qualification {label} is not a directory")
    return path


def _validate_repository_commit(repository_root: Path, expected: Any) -> str:
    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{40}", expected) is None:
        raise ValidationError("partition qualification source_commit is invalid")
    try:
        actual = subprocess.run(
            ("git", "-C", str(repository_root), "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tracked_changes = subprocess.run(
            (
                "git",
                "-C",
                str(repository_root),
                "status",
                "--porcelain",
                "--untracked-files=no",
            ),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValidationError(
            "partition qualification repository_root is not a readable Git checkout"
        ) from error
    if actual != expected:
        raise ValidationError(
            "partition qualification source_commit does not match repository HEAD"
        )
    if tracked_changes:
        raise ValidationError(
            "partition qualification repository contains tracked source changes"
        )
    return expected


def _identity_argv(
    arguments: Sequence[str], bindings: Mapping[str, str]
) -> list[str]:
    reverse: Dict[str, str] = {}
    for label, value in bindings.items():
        if value not in reverse or label < reverse[value]:
            reverse[value] = label
    return [
        f"{{input:{reverse[argument]}}}" if argument in reverse else argument
        for argument in arguments
    ]


def _artifact(path: str, role: str) -> Dict[str, str]:
    return {
        "path": path,
        "role": role,
        "retention": "required",
    }


def compile_partition_qualification_spec(
    config_path: Path, repository_root: Path, output_path: Path
) -> Dict[str, Any]:
    """Compile a sealed frontend -> timing -> MFSPart qualification DAG."""

    output_path = validate_experiment_write_path(output_path)
    config = read_json(config_path)
    if config.get("schema") != PARTITION_QUALIFICATION_CONFIG_SCHEMA:
        raise ValidationError("partition qualification config schema is invalid")
    repository_root = _directory(str(repository_root), "repository_root")

    case_id = config.get("case_id")
    source_commit = config.get("source_commit")
    if not isinstance(case_id, str) or not case_id:
        raise ValidationError("partition qualification case_id is invalid")
    source_commit = _validate_repository_commit(repository_root, source_commit)
    if config.get("partition_provider", "mfspart") != "mfspart":
        raise ValidationError(
            "partition qualification partition_provider must be 'mfspart'"
        )

    rtl = _file(config.get("rtl_source"), "rtl_source")
    platform = _file(config.get("platform"), "platform")
    boarddb_report = _file(config.get("boarddb_report"), "boarddb_report")
    route_constraints = _file(
        config.get("route_constraints"), "route_constraints"
    )
    timing_model = _file(config.get("timing_model"), "timing_model")
    architecture_timing = _file(
        config.get("architecture_timing_db"), "architecture_timing_db"
    )

    tools_raw = config.get("tools")
    if not isinstance(tools_raw, dict) or set(tools_raw) != set(_REQUIRED_TOOLS):
        raise ValidationError(
            "partition qualification tools must exactly cover "
            + ", ".join(_REQUIRED_TOOLS)
        )
    tools = {
        label: _file(value, f"tool {label}")
        for label, value in tools_raw.items()
    }

    top = config.get("top")
    clocks = config.get("clocks")
    periods = config.get("clock_periods")
    if not isinstance(top, str) or not top:
        raise ValidationError("partition qualification top is invalid")
    if (
        not isinstance(clocks, list)
        or not clocks
        or not all(isinstance(item, str) and item for item in clocks)
        or len(clocks) != len(set(clocks))
        or not isinstance(periods, dict)
        or set(periods) != set(clocks)
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
            for value in periods.values()
        )
    ):
        raise ValidationError("partition qualification clocks/periods are invalid")

    partition_seed = config.get("partition_seed", 0)
    if (
        isinstance(partition_seed, bool)
        or not isinstance(partition_seed, int)
        or partition_seed < 0
    ):
        raise ValidationError("partition qualification partition_seed is invalid")
    if config.get("partition_seed_attempts", 1) != 1:
        raise ValidationError(
            "MFSPart qualification requires exactly one deterministic seed attempt"
        )
    if config.get("partition_repair_balance", False) is not False:
        raise ValidationError(
            "MFSPart qualification forbids post-provider balance repair"
        )

    contract = _canonical_case_contract(
        repository_root,
        case_id,
        rtl,
        platform,
        route_constraints,
        boarddb_report,
        top,
        clocks,
        {name: float(periods[name]) for name in clocks},
    )
    executable = str(tools["emuflow"])
    matrix_path = contract["matrix_path"]
    run_spec_path = contract["run_spec_path"]

    input_paths = {
        "rtl": rtl,
        "platform": platform,
        "boarddb_report": boarddb_report,
        "route_constraints": route_constraints,
        "end_to_end_matrix": matrix_path,
        "benchmark_run_spec": run_spec_path,
        "timing_model": timing_model,
        "architecture_timing_db": architecture_timing,
        **{f"tool.{label}": path for label, path in sorted(tools.items())},
    }
    input_hashes = {label: _sha256(path) for label, path in input_paths.items()}
    input_hashes["end_to_end_matrix"] = contract["matrix_sha256"]
    input_bindings = {label: str(path) for label, path in input_paths.items()}
    closures = {
        "frontend": build_implementation_closure(
            repository_root, _COMPONENTS["frontend"]
        ),
        "timing": build_implementation_closure(
            repository_root, _COMPONENTS["timing"]
        ),
        "partition": build_implementation_closure(
            repository_root, _MFSPART_COMPONENTS
        ),
    }
    nodes: list[Dict[str, Any]] = []

    def node(
        node_id: str,
        stage: str,
        dependencies: Sequence[str],
        command: list[str],
        validator: list[str],
        artifacts: list[Dict[str, str]],
        *,
        inputs: Sequence[str],
        configuration: Mapping[str, Any],
        peak_gib: int,
        retained_gib: int,
    ) -> None:
        runtime_values = set(command) | set(validator)
        execution_bindings = {
            label: input_bindings[label]
            for label in sorted(inputs)
            if input_bindings[label] in runtime_values
        }
        nodes.append(
            {
                "id": node_id,
                "stage": stage,
                "dependencies": list(dependencies),
                "inputs": {
                    label: input_hashes[label] for label in sorted(inputs)
                },
                "configuration": dict(configuration),
                "implementation": closures[stage],
                "command": command,
                "execution_bindings": execution_bindings,
                "command_identity": _identity_argv(command, execution_bindings),
                "validator_implementation": closures[stage],
                "validator": validator,
                "validator_identity": _identity_argv(
                    validator, execution_bindings
                ),
                "environment": {
                    "EMUFLOW_EXPERIMENT_POLICY": "canonical-real-rtl-v1"
                },
                "storage_estimate": {
                    "peak_bytes": peak_gib * 1024**3,
                    "retained_bytes": retained_gib * 1024**3,
                },
                "artifacts": artifacts,
            }
        )

    frontend_command = [
        executable,
        "experiment-stage",
        "frontend-run",
        "--platform",
        str(platform),
        "--source",
        str(rtl),
        "--top",
        top,
        "--mapping-profile",
        contract["physical_mapping_profile"],
        "--yosys",
        str(tools["yosys"]),
    ]
    for clock in clocks:
        frontend_command.extend(("--clock", clock))
    frontend_command.extend(("--out", "{output_dir}"))
    node(
        "frontend",
        "frontend",
        [],
        frontend_command,
        [
            executable,
            "experiment-stage",
            "frontend-validate",
            "{artifact_root}",
            "--platform",
            str(platform),
        ],
        [
            _artifact("sources", "source-input"),
            _artifact("phase1", "consumer-checkpoint"),
            _artifact("synthesized.json", "consumer-checkpoint"),
            _artifact("experiment-frontend-report.json", "evidence-critical"),
        ],
        inputs=(
            "rtl",
            "platform",
            "boarddb_report",
            "end_to_end_matrix",
            "benchmark_run_spec",
            "tool.emuflow",
            "tool.yosys",
        ),
        configuration={
            "case_id": case_id,
            "contest_case_id": contract["contest_case_id"],
            "top": top,
            "clocks": clocks,
            "mapping_profile": contract["physical_mapping_profile"],
            "require_no_fabric_clock": True,
        },
        peak_gib=16,
        retained_gib=4,
    )

    timing_command = [
        executable,
        "experiment-stage",
        "timing-run",
        "--frontend",
        "{dependency:frontend}",
        "--timing-model",
        str(timing_model),
        "--architecture-timing-db",
        str(architecture_timing),
        "--opensta",
        str(tools["opensta"]),
    ]
    for clock in clocks:
        timing_command.extend(
            ("--clock-period", f"{clock}={float(periods[clock]):.12g}")
        )
    timing_command.extend(("--out", "{output_dir}"))
    node(
        "timing",
        "timing",
        ["frontend"],
        timing_command,
        [
            executable,
            "experiment-stage",
            "timing-validate",
            "{artifact_root}",
            "--frontend",
            "{dependency:frontend}",
        ],
        [
            _artifact("path-database.json", "consumer-checkpoint"),
            _artifact("partition-net-weights.json", "consumer-checkpoint"),
            _artifact("experiment-timing-report.json", "evidence-critical"),
        ],
        inputs=(
            "timing_model",
            "architecture_timing_db",
            "tool.emuflow",
            "tool.opensta",
        ),
        configuration={
            "clock_periods": periods,
            "max_paths": 200000,
            "criticality_scale": 9.0,
            "criticality_exponent": 2.0,
        },
        peak_gib=16,
        retained_gib=4,
    )

    partition_command = [
        executable,
        "experiment-stage",
        "partition-run",
        "--frontend",
        "{dependency:frontend}",
        "--timing",
        "{dependency:timing}",
        "--platform",
        str(platform),
        "--provider",
        "mfspart",
        "--seed",
        str(partition_seed),
        "--seed-attempts",
        "1",
        "--cut-mode",
        "sequential-only",
        "--max-cross-fpga-dependency-depth",
        "1",
        "--minimum-combinational-cut-nets",
        "0",
        "--route-constraints",
        str(route_constraints),
        "--hop-refiner",
        str(tools["hop_refiner"]),
        "--mfspart-coarsener",
        str(tools["mfspart_coarsener"]),
        "--mfspart-initializer",
        str(tools["mfspart_initializer"]),
        "--mfspart-refiner",
        str(tools["mfspart_refiner"]),
        "--mfspart-refiner-checker",
        str(tools["mfspart_refiner_checker"]),
        "--mfspart-legalizer",
        str(tools["mfspart_legalizer"]),
        "--out",
        "{output_dir}",
    ]
    partition_validator = [
        executable,
        "experiment-stage",
        "partition-validate",
        "{artifact_root}",
        "--frontend",
        "{dependency:frontend}",
        "--timing",
        "{dependency:timing}",
        "--platform",
        str(platform),
        "--route-constraints",
        str(route_constraints),
        "--provider",
        "mfspart",
        "--seed",
        str(partition_seed),
        "--seed-attempts",
        "1",
        "--no-repair-balance",
        "--cut-mode",
        "sequential-only",
        "--max-cross-fpga-dependency-depth",
        "1",
        "--minimum-combinational-cut-nets",
        "0",
    ]
    node(
        "partition",
        "partition",
        ["frontend", "timing"],
        partition_command,
        partition_validator,
        [
            _artifact("clusters.json", "consumer-checkpoint"),
            _artifact("constraints.normalized.json", "consumer-checkpoint"),
            _artifact("assignment.json", "consumer-checkpoint"),
            _artifact("phase3_report.json", "consumer-checkpoint"),
            _artifact("experiment-partition-report.json", "evidence-critical"),
            _artifact("mfspart", "consumer-checkpoint"),
        ],
        inputs=(
            "platform",
            "route_constraints",
            "tool.emuflow",
            "tool.hop_refiner",
            "tool.mfspart_coarsener",
            "tool.mfspart_initializer",
            "tool.mfspart_legalizer",
            "tool.mfspart_refiner",
            "tool.mfspart_refiner_checker",
        ),
        configuration={
            "provider": "mfspart",
            "qualification": "paper-serial-real-rtl-phase3",
            "seed": partition_seed,
            "seed_attempts": 1,
            "repair_balance": False,
            "route_constraints": contract["route_constraints"],
            "cut_mode": "sequential-only",
            "max_cross_fpga_dependency_depth": 1,
            "minimum_combinational_cut_nets": 0,
        },
        peak_gib=24,
        retained_gib=6,
    )

    experiment_id = f"{case_id}__mfspart-phase3"
    spec = {
        "schema": EXPERIMENT_SPEC_V2_SCHEMA,
        "experiment_id": experiment_id,
        "source_commit": source_commit,
        "nodes": nodes,
    }
    validated = validate_experiment_spec(spec)
    write_json(output_path, spec)
    return {
        "status": "pass",
        "experiment_id": experiment_id,
        "provider": "mfspart",
        "nodes": len(validated["nodes"]),
        "terminal_node": "partition",
        "output": str(output_path.resolve()),
    }
