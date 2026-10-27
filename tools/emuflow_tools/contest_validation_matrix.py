"""Validation for the public contest benchmark qualification matrix."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json


CONTEST_VALIDATION_MATRIX_SCHEMA = "emuflow.contest-validation-matrix/v1"

_ID_RE = re.compile(r"[a-z0-9][a-z0-9.-]*")
_HEX_RE = re.compile(r"[0-9a-f]+")
_SUITES = {"iccad2019", "eda2023", "eda2024-repart"}
_TIERS = {"smoke", "small", "medium", "large"}
_QUALIFICATIONS = {
    "catalogued",
    "adapter-regression",
    "case-validated",
    "full-flow-validated",
}
_GATE_ORDER = {
    gate: index
    for index, gate in enumerate(
        (
            "fetch",
            "import",
            "evaluate",
            "materialize-boarddb",
            "phase3",
            "phase4",
            "phase5",
            "phase6",
            "phase7",
        )
    )
}


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"contest matrix {label} must be a non-empty string")
    return value


def _identifier(value: Any, label: str) -> str:
    result = _string(value, label)
    if _ID_RE.fullmatch(result) is None:
        raise ValidationError(
            f"contest matrix {label} may contain only lowercase letters, digits, "
            "'.', and '-'"
        )
    return result


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValidationError(f"contest matrix {label} must be a non-empty list")
    return [_string(item, label) for item in value]


def _validate_source(source: Any, case_id: str) -> None:
    if not isinstance(source, dict):
        raise ValidationError(f"contest matrix case {case_id} source must be an object")
    revision_kind = _string(
        source.get("revision_kind"), f"case {case_id} revision_kind"
    )
    revision = _string(source.get("revision"), f"case {case_id} revision").lower()
    lengths = {"git-commit": 40, "sha256": 64, "embedded-sha256": 64}
    if revision_kind not in lengths:
        raise ValidationError(
            f"contest matrix case {case_id} revision_kind is unsupported"
        )
    if len(revision) != lengths[revision_kind] or _HEX_RE.fullmatch(revision) is None:
        raise ValidationError(
            f"contest matrix case {case_id} revision does not match {revision_kind}"
        )
    if revision_kind == "embedded-sha256":
        locator = _string(source.get("locator"), f"case {case_id} source locator")
        if not locator.startswith("tests/") or not locator.endswith(".py"):
            raise ValidationError(
                f"contest matrix case {case_id} embedded source must name a test fixture"
            )
    else:
        fetcher = _string(source.get("fetcher"), f"case {case_id} source fetcher")
        if not fetcher.startswith("scripts/fetch_") or not fetcher.endswith(".py"):
            raise ValidationError(
                f"contest matrix case {case_id} source fetcher must name a fetch script"
            )


def _validate_case(record: Any) -> str:
    if not isinstance(record, dict):
        raise ValidationError("contest matrix case records must be objects")
    case_id = _identifier(record.get("id"), "case id")
    suite = _identifier(record.get("suite"), f"case {case_id} suite")
    if suite not in _SUITES:
        raise ValidationError(f"contest matrix case {case_id} suite is unsupported")
    case = _identifier(record.get("case"), f"case {case_id} case")
    if case_id != f"{suite}.{case}":
        raise ValidationError(
            f"contest matrix case {case_id} id must equal '<suite>.<case>'"
        )
    _validate_source(record.get("source"), case_id)
    input_bytes = record.get("input_bytes")
    if isinstance(input_bytes, bool) or not isinstance(input_bytes, int) or input_bytes < 1:
        raise ValidationError(
            f"contest matrix case {case_id} input_bytes must be a positive integer"
        )
    tier = _string(record.get("tier"), f"case {case_id} tier")
    if tier not in _TIERS:
        raise ValidationError(f"contest matrix case {case_id} tier is unsupported")
    qualification = _string(
        record.get("qualification"), f"case {case_id} qualification"
    )
    if qualification not in _QUALIFICATIONS:
        raise ValidationError(
            f"contest matrix case {case_id} qualification is unsupported"
        )
    gates = _strings(record.get("target_gates"), f"case {case_id} target_gates")
    if len(set(gates)) != len(gates) or any(gate not in _GATE_ORDER for gate in gates):
        raise ValidationError(
            f"contest matrix case {case_id} target_gates are invalid or duplicated"
        )
    positions = [_GATE_ORDER[gate] for gate in gates]
    if positions != sorted(positions):
        raise ValidationError(
            f"contest matrix case {case_id} target_gates must follow flow order"
        )
    evidence = record.get("evidence")
    if not isinstance(evidence, list) or not all(
        isinstance(item, str) and item for item in evidence
    ):
        raise ValidationError(
            f"contest matrix case {case_id} evidence must be a string list"
        )
    if qualification != "catalogued" and not evidence:
        raise ValidationError(
            f"contest matrix case {case_id} qualified entries require evidence"
        )
    return case_id


def canonical_matrix_sha256(matrix: Mapping[str, Any]) -> str:
    """Return a stable digest for a validated matrix document."""

    payload = json.dumps(
        matrix, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_contest_validation_matrix(matrix: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate the matrix and return deterministic coverage metadata."""

    if not isinstance(matrix, dict):
        raise ValidationError("contest validation matrix must be an object")
    if matrix.get("schema") != CONTEST_VALIDATION_MATRIX_SCHEMA:
        raise ValidationError("contest validation matrix schema is invalid")
    cases = matrix.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValidationError("contest validation matrix cases must be a non-empty list")
    case_ids = [_validate_case(record) for record in cases]
    if len(set(case_ids)) != len(case_ids):
        raise ValidationError("contest validation matrix case IDs must be unique")
    if case_ids != sorted(case_ids):
        raise ValidationError("contest validation matrix cases must be sorted by ID")

    suites: Dict[str, int] = {}
    qualifications: Dict[str, int] = {}
    total_bytes = 0
    for record in cases:
        suite = record["suite"]
        qualification = record["qualification"]
        suites[suite] = suites.get(suite, 0) + 1
        qualifications[qualification] = qualifications.get(qualification, 0) + 1
        total_bytes += record["input_bytes"]
    return {
        "schema": CONTEST_VALIDATION_MATRIX_SCHEMA,
        "case_count": len(cases),
        "input_bytes": total_bytes,
        "suites": dict(sorted(suites.items())),
        "qualifications": dict(sorted(qualifications.items())),
        "matrix_sha256": canonical_matrix_sha256(matrix),
    }


def load_contest_validation_matrix(path: Path) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Load and validate a versioned matrix from disk."""

    matrix = read_json(path)
    return matrix, validate_contest_validation_matrix(matrix)


def case_keys(matrix: Mapping[str, Any], suite: str) -> Iterable[str]:
    """Yield cases for one suite in the matrix's deterministic order."""

    return (
        record["case"]
        for record in matrix["cases"]
        if record["suite"] == suite
    )
