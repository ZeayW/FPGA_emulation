"""Version-pinned public contest fetch, import and semantic validation."""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .contest_validation_matrix import (
    canonical_matrix_sha256,
    load_contest_validation_matrix,
)
from emuflow.common.errors import EmuFlowError, ValidationError
from emuflow.common.io import read_json, write_json
from .contest_eda2023 import (
    import_eda2023_case,
    materialize_eda2023_official_outputs,
    materialize_eda2023_rtl_boarddb,
)
from .contest_eda2024 import (
    evaluate_eda2024_solution,
    import_eda2024_case,
    materialize_eda2024_rtl_boarddb,
)


PUBLIC_CONTEST_FETCH_REPORT_SCHEMA = "emuflow.public-contest-fetch-report/v1"
PUBLIC_CONTEST_IMPORT_REPORT_SCHEMA = "emuflow.public-contest-import-report/v1"
PUBLIC_CONTEST_BOARDDB_REPORT_SCHEMA = "emuflow.public-contest-boarddb-report/v1"
PUBLIC_CONTEST_EVALUATION_REPORT_SCHEMA = (
    "emuflow.public-contest-evaluation-report/v2"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SSL_CERT_SHA256_ENV = "EMUFLOW_SSL_CERT_SHA256"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_expected_sha256(
    path: Path, expected: Optional[str], *, label: str
) -> str:
    actual = _sha256_file(path)
    if expected is None:
        return actual
    if not isinstance(expected, str):
        raise ValidationError(f"{label} expected SHA256 must be full 64-hex")
    expected = expected.lower()
    if _SHA256_RE.fullmatch(expected) is None:
        raise ValidationError(f"{label} expected SHA256 must be full 64-hex")
    if actual != expected:
        raise ValidationError(f"{label} SHA256 does not match the frozen candidate")
    return actual


def _validate_bound_ssl_cert_environment() -> Optional[Dict[str, Any]]:
    """Validate a farm-bound CA bundle before a network fetch.

    Ordinary host SSL_CERT_FILE use remains supported.  The additional digest
    variable opts into a validation-farm content seal, so the referenced trust
    bundle cannot be replaced between farm planning and task execution.
    """

    expected = os.environ.get(_SSL_CERT_SHA256_ENV)
    if expected is None:
        return None
    value = os.environ.get("SSL_CERT_FILE")
    if not value:
        raise ValidationError(
            f"{_SSL_CERT_SHA256_ENV} requires SSL_CERT_FILE"
        )
    expected = expected.lower()
    if _SHA256_RE.fullmatch(expected) is None:
        raise ValidationError(
            f"{_SSL_CERT_SHA256_ENV} must be full 64-hex"
        )
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValidationError(
            "farm-bound SSL_CERT_FILE must be an absolute regular file"
        )
    actual = _sha256_file(path)
    if actual != expected:
        raise ValidationError("farm-bound SSL certificate bundle SHA256 mismatch")
    return {"provider": "farm-bound-ca-bundle", "sha256": actual}


def _paths_overlap(first: Path, second: Path) -> bool:
    return (
        first == second
        or first in second.parents
        or second in first.parents
    )


def _find_case(matrix: Mapping[str, Any], case_id: str) -> Mapping[str, Any]:
    matches = [record for record in matrix["cases"] if record["id"] == case_id]
    if len(matches) != 1:
        raise ValidationError(f"public contest case {case_id!r} is not in the matrix")
    return matches[0]


def _runtime_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_fetcher(relative: str, fetcher_root: Optional[Path]) -> Path:
    if fetcher_root is not None:
        candidate = fetcher_root.resolve() / relative
    else:
        root = _runtime_root()
        installed = root / "share" / "emuflow" / relative
        candidate = installed if installed.is_file() else root / relative
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise EmuFlowError(f"public contest fetcher is missing: {candidate}")
    return candidate


def _validate_fetch_provenance(
    case: Mapping[str, Any], source_root: Path
) -> Dict[str, Any]:
    provenance = read_json(source_root / "SOURCE.json")
    if provenance.get("schema") != "emuflow.public-benchmark-fetch/v1":
        raise ValidationError("public contest fetch provenance schema is invalid")
    if provenance.get("case") != case["case"]:
        raise ValidationError("public contest fetch provenance case does not agree")
    source = case["source"]
    revision_kind = source["revision_kind"]
    if revision_kind == "git-commit":
        actual_revision = provenance.get("commit")
    elif revision_kind == "sha256":
        actual_revision = provenance.get("archive_sha256")
    else:
        raise ValidationError("embedded contest fixtures cannot be fetched")
    if actual_revision != source["revision"]:
        raise ValidationError("public contest fetch revision does not agree")
    files = provenance.get("files")
    if not isinstance(files, list) or not files:
        raise ValidationError("public contest fetch provenance has no files")
    names = []
    total_bytes = 0
    for index, record in enumerate(files):
        if not isinstance(record, dict):
            raise ValidationError(f"public contest fetch file {index} is invalid")
        name = record.get("name")
        size = record.get("bytes")
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 1
        ):
            raise ValidationError(f"public contest fetch file {index} is invalid")
        path = source_root / name
        if not path.is_file() or path.stat().st_size != size:
            raise ValidationError(f"public contest fetched file {name!r} is missing")
        payload = path.read_bytes()
        if "git_blob_sha1" in record:
            expected = record["git_blob_sha1"]
            actual = hashlib.sha1(
                f"blob {len(payload)}\0".encode("ascii") + payload
            ).hexdigest()
        elif "sha256" in record:
            expected = record["sha256"]
            actual = hashlib.sha256(payload).hexdigest()
        else:
            raise ValidationError(
                f"public contest fetched file {name!r} has no content digest"
            )
        if expected != actual:
            raise ValidationError(
                f"public contest fetched file {name!r} digest does not agree"
            )
        names.append(name)
        total_bytes += size
    if len(set(names)) != len(names):
        raise ValidationError("public contest fetch provenance duplicates files")
    if total_bytes != case["input_bytes"]:
        raise ValidationError("public contest fetched byte count does not agree")
    return {
        "revision": actual_revision,
        "files": len(files),
        "input_bytes": total_bytes,
    }


def fetch_public_contest_case(
    matrix_path: Path,
    case_id: str,
    output_dir: Path,
    *,
    fetcher_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run a pinned fetcher and independently validate its provenance."""

    transport_security = _validate_bound_ssl_cert_environment()
    matrix, _ = load_contest_validation_matrix(matrix_path)
    case = _find_case(matrix, case_id)
    source = case["source"]
    if source["revision_kind"] == "embedded-sha256":
        raise ValidationError("embedded contest fixtures do not have a fetch gate")
    fetcher = _resolve_fetcher(source["fetcher"], fetcher_root)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise EmuFlowError(f"public contest output is not empty: {output_dir}")
    source_root = output_dir / "input"
    completed = subprocess.run(
        [
            sys.executable,
            str(fetcher),
            "--case",
            case["case"],
            "--out",
            str(source_root),
        ],
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise EmuFlowError(
            "public contest fetch failed: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    checked = _validate_fetch_provenance(case, source_root)
    report = {
        "schema": PUBLIC_CONTEST_FETCH_REPORT_SCHEMA,
        "status": "pass",
        "case_id": case_id,
        "suite": case["suite"],
        "case": case["case"],
        "tier": case["tier"],
        "gate": "fetch",
        "matrix_sha256": canonical_matrix_sha256(matrix),
        "source": {
            "revision_kind": source["revision_kind"],
            "revision": checked["revision"],
        },
        "validation": {
            "files": checked["files"],
            "input_bytes": checked["input_bytes"],
            "status": "pass",
        },
        "artifacts": {"source": "input", "provenance": "input/SOURCE.json"},
    }
    if transport_security is not None:
        report["transport_security"] = transport_security
    write_json(output_dir / "fetch_report.json", report)
    return report


def _artifact_manifest(output_dir: Path) -> list[Dict[str, Any]]:
    records = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file() or path.name == "import_report.json":
            continue
        records.append(
            {
                "path": path.relative_to(output_dir).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return records


def _sealed_artifact_manifest(
    output_dir: Path, *, report_name: str
) -> list[Dict[str, Any]]:
    records = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file() or path == output_dir / report_name:
            continue
        records.append(
            {
                "path": path.relative_to(output_dir).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return records


def _validate_sealed_manifest(
    output_dir: Path, records: Any, *, report_name: str
) -> None:
    if not isinstance(records, list) or not records:
        raise ValidationError("public contest evaluation artifact manifest is invalid")
    seen = set()
    resolved_seen = set()
    root = output_dir.resolve()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValidationError(
                f"public contest evaluation artifact {index} is invalid"
            )
        relative = record.get("path")
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or relative in seen
        ):
            raise ValidationError("public contest evaluation artifact path is invalid")
        seen.add(relative)
        path = (root / relative).resolve()
        if path == root or root not in path.parents or path in resolved_seen:
            raise ValidationError("public contest evaluation artifact path is unsafe")
        resolved_seen.add(path)
        if (
            not path.is_file()
            or path.stat().st_size != record.get("bytes")
            or hashlib.sha256(path.read_bytes()).hexdigest() != record.get("sha256")
        ):
            raise ValidationError(
                f"public contest evaluation artifact {relative!r} seal is broken"
            )
    actual = {
        path.relative_to(output_dir).as_posix()
        for path in output_dir.rglob("*")
        if path.is_file() and path != output_dir / report_name
    }
    if actual != seen:
        raise ValidationError(
            "public contest evaluation artifact coverage is not exact"
        )


def import_public_contest_case(
    matrix_path: Path,
    case_id: str,
    source_dir: Path,
    output_dir: Path,
) -> Dict[str, Any]:
    """Validate pinned fetch provenance and dispatch one semantic importer."""

    matrix, _ = load_contest_validation_matrix(matrix_path)
    case = _find_case(matrix, case_id)
    if "import" not in case["target_gates"]:
        raise ValidationError(f"public contest case {case_id!r} has no import gate")
    source_dir = source_dir.resolve()
    checked = _validate_fetch_provenance(case, source_dir)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise EmuFlowError(f"public contest import output is not empty: {output_dir}")

    name = case_id.replace(".", "-")
    if case["suite"] == "eda2023":
        adapter = import_eda2023_case(source_dir, output_dir, name)
    elif case["suite"] == "eda2024-repart":
        adapter = import_eda2024_case(source_dir, output_dir, name)
    else:
        raise ValidationError(
            f"public contest suite {case['suite']!r} has no public import adapter"
        )
    artifacts = _artifact_manifest(output_dir)
    if not artifacts:
        raise ValidationError("public contest importer produced no artifacts")
    report = {
        "schema": PUBLIC_CONTEST_IMPORT_REPORT_SCHEMA,
        "status": "pass",
        "case_id": case_id,
        "suite": case["suite"],
        "case": case["case"],
        "tier": case["tier"],
        "gate": "import",
        "matrix_sha256": canonical_matrix_sha256(matrix),
        "source": {
            "revision_kind": case["source"]["revision_kind"],
            "revision": checked["revision"],
            "files": checked["files"],
            "input_bytes": checked["input_bytes"],
        },
        "adapter": adapter,
        "artifacts": artifacts,
        "evaluation_status": "not-run",
    }
    write_json(output_dir / "import_report.json", report)
    return report


def _validate_import_artifacts(
    matrix: Mapping[str, Any], case: Mapping[str, Any], import_dir: Path
) -> Dict[str, Any]:
    report = read_json(import_dir / "import_report.json")
    if (
        report.get("schema") != PUBLIC_CONTEST_IMPORT_REPORT_SCHEMA
        or report.get("status") != "pass"
        or report.get("case_id") != case["id"]
        or report.get("matrix_sha256") != canonical_matrix_sha256(matrix)
        or report.get("evaluation_status") != "not-run"
    ):
        raise ValidationError("public contest import report is invalid")
    records = report.get("artifacts")
    if not isinstance(records, list) or not records:
        raise ValidationError("public contest import artifact manifest is invalid")
    seen = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValidationError(f"public contest import artifact {index} is invalid")
        relative = record.get("path")
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or relative in seen
        ):
            raise ValidationError("public contest import artifact path is invalid")
        seen.add(relative)
        path = import_dir / relative
        if (
            not path.is_file()
            or path.stat().st_size != record.get("bytes")
            or hashlib.sha256(path.read_bytes()).hexdigest() != record.get("sha256")
        ):
            raise ValidationError(
                f"public contest import artifact {relative!r} seal is broken"
            )
    actual = {
        path.relative_to(import_dir).as_posix()
        for path in import_dir.rglob("*")
        if path.is_file() and path.name != "import_report.json"
    }
    if actual != seen:
        raise ValidationError("public contest import artifact coverage is not exact")
    return report


def _candidate_spec_for_suite(
    suite: str,
    *,
    routes_path: Optional[Path],
    tdm_plan_path: Optional[Path],
    solution_path: Optional[Path],
) -> Dict[str, Path]:
    supplied = {
        "routes.json": routes_path,
        "tdm_plan.json": tdm_plan_path,
        "design.fpga.out": solution_path,
    }
    required = {
        "eda2023": {"routes.json", "tdm_plan.json"},
        "eda2024-repart": {"design.fpga.out"},
    }.get(suite)
    if required is None:
        raise ValidationError(
            f"public contest suite {suite!r} has no unified evaluate gate"
        )
    allowed = set(required)
    missing = sorted(name for name in required if supplied[name] is None)
    unexpected = sorted(
        name for name, path in supplied.items()
        if path is not None and name not in allowed
    )
    if missing or unexpected:
        raise ValidationError(
            "public contest candidate files do not match suite "
            f"{suite!r}: missing={missing}, unexpected={unexpected}"
        )
    resolved: Dict[str, Path] = {}
    for name, path in supplied.items():
        if path is None:
            continue
        candidate = path.resolve()
        if not candidate.is_file():
            raise ValidationError(f"public contest candidate {name!r} is missing")
        resolved[name] = candidate
    return resolved


def _candidate_summary(candidate_dir: Path) -> Dict[str, Optional[str]]:
    return {
        "routes_sha256": (
            _sha256_file(candidate_dir / "routes.json")
            if (candidate_dir / "routes.json").is_file()
            else None
        ),
        "tdm_plan_sha256": (
            _sha256_file(candidate_dir / "tdm_plan.json")
            if (candidate_dir / "tdm_plan.json").is_file()
            else None
        ),
        "solution_sha256": (
            _sha256_file(candidate_dir / "design.fpga.out")
            if (candidate_dir / "design.fpga.out").is_file()
            else None
        ),
    }


def _run_frozen_public_evaluation(
    suite: str,
    source_dir: Path,
    import_dir: Path,
    candidate_dir: Path,
    output_dir: Path,
    *,
    runtime_seconds: float,
) -> Dict[str, Any]:
    evaluation_path = output_dir / "evaluation.json"
    official_dir = output_dir / "official"
    if suite == "eda2023":
        evaluation = materialize_eda2023_official_outputs(
            import_dir / "contest_instance.json",
            candidate_dir / "routes.json",
            candidate_dir / "tdm_plan.json",
            official_dir,
        )
        write_json(evaluation_path, evaluation)
        return evaluation
    if suite == "eda2024-repart":
        evaluation = evaluate_eda2024_solution(
            source_dir / "design.info",
            source_dir / "design.are",
            source_dir / "design.net",
            source_dir / "design.topo",
            candidate_dir / "design.fpga.out",
            runtime_seconds=runtime_seconds,
            output_path=evaluation_path,
        )
        official_dir.mkdir()
        shutil.copy2(
            candidate_dir / "design.fpga.out",
            official_dir / "design.fpga.out",
        )
        return evaluation
    raise ValidationError(f"public contest evaluation suite {suite!r} is unsupported")


def evaluate_public_contest_case(
    matrix_path: Path,
    case_id: str,
    source_dir: Path,
    import_dir: Path,
    routes_path: Optional[Path],
    output_dir: Path,
    *,
    tdm_plan_path: Optional[Path] = None,
    solution_path: Optional[Path] = None,
    runtime_seconds: float = 0.0,
    expected_routes_sha256: Optional[str] = None,
    expected_tdm_plan_sha256: Optional[str] = None,
    expected_solution_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a self-contained, replayable public-contest evaluation bundle."""

    matrix, _ = load_contest_validation_matrix(matrix_path)
    case = _find_case(matrix, case_id)
    if "evaluate" not in case["target_gates"]:
        raise ValidationError(f"public contest case {case_id!r} has no evaluate gate")
    if (
        isinstance(runtime_seconds, bool)
        or not isinstance(runtime_seconds, (int, float))
        or not math.isfinite(float(runtime_seconds))
        or runtime_seconds < 0
    ):
        raise ValidationError("contest evaluation runtime must be finite and non-negative")
    source_dir = source_dir.resolve()
    import_dir = import_dir.resolve()
    source_validation = _validate_fetch_provenance(case, source_dir)
    import_report = _validate_import_artifacts(matrix, case, import_dir)
    candidates = _candidate_spec_for_suite(
        case["suite"],
        routes_path=routes_path,
        tdm_plan_path=tdm_plan_path,
        solution_path=solution_path,
    )
    expected_by_name = {
        "routes.json": expected_routes_sha256,
        "tdm_plan.json": expected_tdm_plan_sha256,
        "design.fpga.out": expected_solution_sha256,
    }
    for name, expected in expected_by_name.items():
        if name not in candidates and expected is not None:
            raise ValidationError(
                f"candidate {name!r} SHA256 was supplied without its file"
            )
    for name, path in candidates.items():
        _validate_expected_sha256(
            path, expected_by_name[name], label=f"candidate {name}"
        )

    output_dir = output_dir.resolve()
    inputs = [source_dir, import_dir, *candidates.values()]
    if any(_paths_overlap(output_dir, path) for path in inputs):
        raise ValidationError(
            "public contest evaluation output overlaps an input path"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise EmuFlowError(
            f"public contest evaluation output is not empty: {output_dir}"
        )
    frozen_source = output_dir / "source"
    frozen_import = output_dir / "import"
    frozen_candidate = output_dir / "candidate"
    frozen_source.mkdir()
    source_manifest = read_json(source_dir / "SOURCE.json")
    shutil.copy2(source_dir / "SOURCE.json", frozen_source / "SOURCE.json")
    for record in source_manifest["files"]:
        shutil.copy2(source_dir / record["name"], frozen_source / record["name"])
    shutil.copytree(import_dir, frozen_import)
    frozen_candidate.mkdir()
    for name, path in candidates.items():
        frozen = frozen_candidate / name
        shutil.copy2(path, frozen)
        _validate_expected_sha256(
            frozen,
            expected_by_name[name],
            label=f"copied candidate {name}",
        )

    evaluation_path = output_dir / "evaluation.json"
    evaluation = _run_frozen_public_evaluation(
        case["suite"],
        frozen_source,
        frozen_import,
        frozen_candidate,
        output_dir,
        runtime_seconds=runtime_seconds,
    )
    report = {
        "schema": PUBLIC_CONTEST_EVALUATION_REPORT_SCHEMA,
        "status": "pass",
        "case_id": case_id,
        "suite": case["suite"],
        "case": case["case"],
        "tier": case["tier"],
        "gate": "evaluate",
        "matrix_sha256": canonical_matrix_sha256(matrix),
        "source": {
            **source_validation,
            "provenance_sha256": hashlib.sha256(
                (frozen_source / "SOURCE.json").read_bytes()
            ).hexdigest(),
        },
        "upstream": {
            "import_report_sha256": hashlib.sha256(
                (frozen_import / "import_report.json").read_bytes()
            ).hexdigest(),
            "import_artifacts": len(import_report["artifacts"]),
        },
        "candidate": _candidate_summary(frozen_candidate),
        "evaluation_sha256": hashlib.sha256(evaluation_path.read_bytes()).hexdigest(),
        "metrics": evaluation["metrics"],
        "artifacts": _sealed_artifact_manifest(
            output_dir, report_name="evaluation_report.json"
        ),
    }
    write_json(output_dir / "evaluation_report.json", report)
    validate_public_contest_evaluation(matrix_path, output_dir)
    return report


def validate_public_contest_evaluation(
    matrix_path: Path, evaluation_dir: Path
) -> Dict[str, Any]:
    """Replay a frozen public evaluation and verify every sealed byte."""

    matrix, _ = load_contest_validation_matrix(matrix_path)
    evaluation_dir = evaluation_dir.resolve()
    report = read_json(evaluation_dir / "evaluation_report.json")
    expected_report_fields = {
        "schema",
        "status",
        "case_id",
        "suite",
        "case",
        "tier",
        "gate",
        "matrix_sha256",
        "source",
        "upstream",
        "candidate",
        "evaluation_sha256",
        "metrics",
        "artifacts",
    }
    if set(report) != expected_report_fields:
        raise ValidationError("public contest evaluation report fields are invalid")
    case_id = report.get("case_id")
    if not isinstance(case_id, str):
        raise ValidationError("public contest evaluation case id is invalid")
    case = _find_case(matrix, case_id)
    if (
        report.get("schema") != PUBLIC_CONTEST_EVALUATION_REPORT_SCHEMA
        or report.get("status") != "pass"
        or report.get("suite") != case["suite"]
        or report.get("case") != case["case"]
        or report.get("tier") != case["tier"]
        or report.get("gate") != "evaluate"
        or report.get("matrix_sha256") != canonical_matrix_sha256(matrix)
    ):
        raise ValidationError("public contest evaluation report is invalid")
    _validate_sealed_manifest(
        evaluation_dir,
        report.get("artifacts"),
        report_name="evaluation_report.json",
    )
    frozen_source = evaluation_dir / "source"
    frozen_import = evaluation_dir / "import"
    frozen_candidate = evaluation_dir / "candidate"
    source_validation = _validate_fetch_provenance(case, frozen_source)
    import_report = _validate_import_artifacts(matrix, case, frozen_import)
    _candidate_spec_for_suite(
        case["suite"],
        routes_path=(frozen_candidate / "routes.json")
        if (frozen_candidate / "routes.json").is_file()
        else None,
        tdm_plan_path=(frozen_candidate / "tdm_plan.json")
        if (frozen_candidate / "tdm_plan.json").is_file()
        else None,
        solution_path=(frozen_candidate / "design.fpga.out")
        if (frozen_candidate / "design.fpga.out").is_file()
        else None,
    )
    with tempfile.TemporaryDirectory() as temporary:
        replay_root = Path(temporary)
        replay_import = replay_root / "import"
        import_public_contest_case(
            matrix_path, case_id, frozen_source, replay_import
        )
        supplied_import_files = {
            path.relative_to(frozen_import).as_posix(): path
            for path in frozen_import.rglob("*")
            if path.is_file() and path.name != "import_report.json"
        }
        replay_import_files = {
            path.relative_to(replay_import).as_posix(): path
            for path in replay_import.rglob("*")
            if path.is_file() and path.name != "import_report.json"
        }
        if set(supplied_import_files) != set(replay_import_files) or any(
            supplied_import_files[name].read_bytes()
            != replay_import_files[name].read_bytes()
            for name in supplied_import_files
        ):
            raise ValidationError(
                "public contest semantic import replay differs"
            )
        replay = _run_frozen_public_evaluation(
            case["suite"],
            frozen_source,
            frozen_import,
            frozen_candidate,
            replay_root,
            runtime_seconds=float(report["metrics"].get("runtime_seconds", 0.0)),
        )
        stored = read_json(evaluation_dir / "evaluation.json")
        if replay != stored:
            raise ValidationError("public contest evaluation replay differs")
        supplied_official = {
            path.relative_to(evaluation_dir / "official").as_posix(): path
            for path in (evaluation_dir / "official").rglob("*")
            if path.is_file()
        }
        replay_official = {
            path.relative_to(replay_root / "official").as_posix(): path
            for path in (replay_root / "official").rglob("*")
            if path.is_file()
        }
        if set(supplied_official) != set(replay_official):
            raise ValidationError("public contest official output set differs")
        for name, path in supplied_official.items():
            if path.read_bytes() != replay_official[name].read_bytes():
                raise ValidationError(
                    f"public contest official output {name!r} differs"
                )
    expected_summary = {
        "source": {
            **source_validation,
            "provenance_sha256": hashlib.sha256(
                (frozen_source / "SOURCE.json").read_bytes()
            ).hexdigest(),
        },
        "upstream": {
            "import_report_sha256": hashlib.sha256(
                (frozen_import / "import_report.json").read_bytes()
            ).hexdigest(),
            "import_artifacts": len(import_report["artifacts"]),
        },
        "candidate": _candidate_summary(frozen_candidate),
        "evaluation_sha256": hashlib.sha256(
            (evaluation_dir / "evaluation.json").read_bytes()
        ).hexdigest(),
        "metrics": replay["metrics"],
    }
    if any(report.get(key) != value for key, value in expected_summary.items()):
        raise ValidationError("public contest evaluation summary does not agree")
    return {
        "status": "pass",
        "case_id": case_id,
        "artifacts_verified": len(report["artifacts"]),
        "metrics": replay["metrics"],
    }


def materialize_public_contest_boarddb(
    matrix_path: Path,
    case_id: str,
    source_dir: Path,
    import_dir: Path,
    device_template_path: Path,
    output_dir: Path,
    *,
    lane_scale: int = 1,
    unweighted_link_lanes: int = 1,
    fabric_clock_mhz: float = 50.0,
    latency_cycles: int = 2,
) -> Dict[str, Any]:
    """Project a passed public import onto a source-qualified FPGA template."""

    matrix, _ = load_contest_validation_matrix(matrix_path)
    case = _find_case(matrix, case_id)
    if "materialize-boarddb" not in case["target_gates"]:
        raise ValidationError(
            f"public contest case {case_id!r} has no BoardDB materialization gate"
        )
    source_dir = source_dir.resolve()
    import_dir = import_dir.resolve()
    _validate_fetch_provenance(case, source_dir)
    _validate_import_artifacts(matrix, case, import_dir)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise EmuFlowError(f"public contest BoardDB output is not empty: {output_dir}")
    output_path = output_dir / "boarddb.json"
    name = case_id.replace(".", "-") + "-rtl"
    if case["suite"] == "eda2023":
        adapter = materialize_eda2023_rtl_boarddb(
            import_dir / "contest_instance.json",
            device_template_path,
            output_path,
            name=name,
            lane_scale=lane_scale,
            fabric_clock_mhz=fabric_clock_mhz,
            latency_cycles=latency_cycles,
            route_constraints_path=output_dir / "route_constraints.json",
        )
        projection = {"lane_scale": lane_scale}
    elif case["suite"] == "eda2024-repart":
        adapter = materialize_eda2024_rtl_boarddb(
            source_dir,
            device_template_path,
            output_path,
            name=name,
            lanes_per_edge=unweighted_link_lanes,
            fabric_clock_mhz=fabric_clock_mhz,
            latency_cycles=latency_cycles,
            route_constraints_path=output_dir / "route_constraints.json",
        )
        projection = {"unweighted_link_lanes": unweighted_link_lanes}
    else:
        raise ValidationError(
            f"public contest suite {case['suite']!r} has no BoardDB adapter"
        )
    artifacts = _artifact_manifest(output_dir)
    report = {
        "schema": PUBLIC_CONTEST_BOARDDB_REPORT_SCHEMA,
        "status": "pass",
        "case_id": case_id,
        "suite": case["suite"],
        "gate": "materialize-boarddb",
        "matrix_sha256": canonical_matrix_sha256(matrix),
        "qualification": "academic-architecture-projection",
        "projection": {
            **projection,
            "fabric_clock_mhz": float(fabric_clock_mhz),
            "latency_cycles": latency_cycles,
        },
        "adapter": adapter,
        "artifacts": artifacts,
        "phase3_status": "not-run",
    }
    write_json(output_dir / "boarddb_report.json", report)
    return report
