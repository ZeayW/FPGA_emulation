"""Independent Chimew lookahead versus final Vivado rank-correlation gate."""

from __future__ import annotations

import csv
import math
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .chimew_pipeline import (
    CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER,
    read_chimew_phase6_pipeline,
)
from .chimew_qualification import canonical_sha256
from .errors import ValidationError
from .io import read_json, write_json
from .vivado_board_flow import (
    VIVADO_BOARD_FLOW_SCHEMA,
    validate_vivado_board_flow_bundle,
)


CHIMEW_VIVADO_CORRELATION_INPUT_SCHEMA = (
    "emuflow.chimew-vivado-correlation-input/v1"
)
CHIMEW_VIVADO_CORRELATION_REPORT_SCHEMA = (
    "emuflow.chimew-vivado-correlation-report/v1"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SLR_COUNT_PATTERNS = (
    re.compile(r"(?i)EMUFLOW_SLR_CROSSING_COUNT\s*[\t:|]\s*([0-9]+)"),
    re.compile(
        r"(?i)(?:total|number\s+of)\s+(?:inter[- ]?)?SLR\s+"
        r"cross(?:ing|ings|ing\s+nets?)\s*[:|]\s*([0-9]+)"
    ),
)


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f"{label} must be a finite number")
    return result


def _rank(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        average = (cursor + 1 + end) / 2.0
        for position in range(cursor, end):
            ranks[order[position]] = average
        cursor = end
    return ranks


def _spearman(left: Sequence[float], right: Sequence[float]) -> Optional[float]:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_rank = _rank(left)
    right_rank = _rank(right)
    left_mean = sum(left_rank) / len(left_rank)
    right_mean = sum(right_rank) / len(right_rank)
    numerator = sum(
        (x - left_mean) * (y - right_mean)
        for x, y in zip(left_rank, right_rank)
    )
    left_norm = sum((x - left_mean) ** 2 for x in left_rank)
    right_norm = sum((y - right_mean) ** 2 for y in right_rank)
    if left_norm == 0.0 or right_norm == 0.0:
        return None
    return numerator / math.sqrt(left_norm * right_norm)


def _parse_congestion_csv(path: Path) -> Dict[str, int]:
    levels: list[int] = []
    level_column: Optional[int] = None
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.reader(stream):
            normalized = [re.sub(r"[^a-z0-9]+", "", cell.lower()) for cell in row]
            header = next(
                (
                    index
                    for index, value in enumerate(normalized)
                    if value == "congestionlevel"
                ),
                None,
            )
            if header is not None:
                level_column = header
                continue
            if level_column is None or level_column >= len(row):
                continue
            match = re.search(r"(?<![0-9])([0-9]+)(?![0-9])", row[level_column])
            if match is not None:
                levels.append(int(match.group(1)))
    if not levels:
        raise ValidationError(
            f"Vivado congestion CSV has no machine-readable congestion levels: {path}"
        )
    return {"maximum_level": max(levels), "windows": len(levels)}


def _parse_slr_crossings(path: Path) -> int:
    text = path.read_text(encoding="utf-8", errors="replace")
    values = [
        int(match.group(1))
        for pattern in _SLR_COUNT_PATTERNS
        for match in pattern.finditer(text)
    ]
    if not values:
        raise ValidationError(
            f"Vivado SLR report has no machine-readable total crossing count: {path}"
        )
    if len(set(values)) != 1:
        raise ValidationError("Vivado SLR report crossing totals disagree")
    return values[0]


def _safe_artifact(root: Path, record: Mapping[str, Any], label: str) -> Path:
    relative = record.get("path")
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        raise ValidationError(f"{label} path is invalid")
    path = (root / relative).resolve()
    if path == root or root not in path.parents or not path.is_file():
        raise ValidationError(f"{label} path is unsafe")
    return path


def _chimew_metrics_from_snapshot(
    bundle: Path,
    report: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> Dict[str, float]:
    if (
        report.get("provider") != CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER
        or validation.get("qualification_scope") != "byte-bound-source-artifacts"
    ):
        raise ValidationError("Chimew correlation requires byte-bound source bundles")
    artifacts = report.get("artifacts")
    adapter_record = (
        artifacts.get("adapter_report") if isinstance(artifacts, dict) else None
    )
    adapter_path = (
        _safe_artifact(bundle, adapter_record, "Chimew adapter report")
        if isinstance(adapter_record, dict)
        else bundle / "phase6-adapter" / "adapter_report.json"
    )
    adapter = read_json(adapter_path)
    metrics = report.get("metrics")
    pin = adapter.get("validation")
    if not isinstance(metrics, dict) or not isinstance(pin, dict):
        raise ValidationError("Chimew correlation metrics are missing")
    return {
        "rudy_peak_utilization": _finite_number(
            metrics.get("rudy_peak_utilization"),
            "Chimew RUDY peak utilization",
        ),
        "crossing_bits": _finite_number(
            pin.get("crossing_bits"), "Chimew crossing bits"
        ),
        "pin_distance": _finite_number(
            pin.get("pin_distance"), "Chimew pin distance"
        ),
    }


def _chimew_metrics(bundle: Path) -> Dict[str, float]:
    with read_chimew_phase6_pipeline(bundle) as (report, validation):
        return _chimew_metrics_from_snapshot(bundle, report, validation)


def _optional_closure_number(value: Any, label: str) -> Optional[float]:
    if value in {None, "NA", "N/A"}:
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError as error:
            raise ValidationError(f"{label} is not numeric") from error
    return _finite_number(value, label)


def _vivado_metrics(bundle: Path) -> Dict[str, Any]:
    validation = validate_vivado_board_flow_bundle(bundle)
    report = read_json(bundle / "vivado-board-flow-report.json")
    if report.get("schema") != VIVADO_BOARD_FLOW_SCHEMA:
        raise ValidationError("Chimew correlation requires Vivado board-flow v3")
    congestion_levels = []
    congestion_windows = 0
    slr_crossings = 0
    multi_slr_fpgas = 0
    critical_paths = []
    wns_values = []
    for record in report["fpgas"]:
        artifacts = record["artifacts"]
        congestion = _parse_congestion_csv(
            _safe_artifact(bundle, artifacts["congestion.csv"], "congestion CSV")
        )
        congestion_levels.append(congestion["maximum_level"])
        congestion_windows += congestion["windows"]
        if record["physical_evidence"]["slr_count"] > 1:
            multi_slr_fpgas += 1
            slr_crossings += _parse_slr_crossings(
                _safe_artifact(
                    bundle, artifacts["slr_crossing.rpt"], "SLR crossing report"
                )
            )
        closure = record["closure"]
        critical = _optional_closure_number(
            closure.get("critical_path_ns"), "Vivado critical path"
        )
        wns = _optional_closure_number(closure.get("wns_ns"), "Vivado WNS")
        if critical is not None:
            critical_paths.append(critical)
        if wns is not None:
            wns_values.append(wns)
    return {
        "congestion_maximum_level": max(congestion_levels),
        "congestion_windows": congestion_windows,
        "slr_crossings": slr_crossings if multi_slr_fpgas else None,
        "multi_slr_fpgas": multi_slr_fpgas,
        "critical_path_ns": max(critical_paths) if critical_paths else None,
        "wns_ns": min(wns_values) if wns_values else None,
        "artifacts_verified": validation["artifacts_verified"],
    }


def _correlation_record(
    candidates: Sequence[Mapping[str, Any]],
    predicted_key: str,
    actual_key: str,
    minimum: float,
) -> Dict[str, Any]:
    pairs = [
        (record["predicted"][predicted_key], record["actual"][actual_key])
        for record in candidates
        if record["actual"].get(actual_key) is not None
    ]
    rho = _spearman(
        [float(pair[0]) for pair in pairs],
        [float(pair[1]) for pair in pairs],
    )
    if len(pairs) < 3:
        status = "insufficient-candidates"
    elif rho is None:
        status = "insufficient-variation"
    elif rho >= minimum:
        status = "pass"
    else:
        status = "fail"
    return {"samples": len(pairs), "spearman_rho": rho, "status": status}


def _build_correlation_report(document: Mapping[str, Any]) -> Dict[str, Any]:
    if set(document) != {"schema", "minimum_spearman", "candidates"} or document.get(
        "schema"
    ) != CHIMEW_VIVADO_CORRELATION_INPUT_SCHEMA:
        raise ValidationError("Chimew/Vivado correlation input schema is invalid")
    minimum = _finite_number(document.get("minimum_spearman"), "minimum Spearman")
    if not 0.0 <= minimum <= 1.0:
        raise ValidationError("minimum Spearman must be in [0,1]")
    supplied = document.get("candidates")
    if not isinstance(supplied, list) or len(supplied) < 3:
        raise ValidationError("Chimew/Vivado correlation requires at least three candidates")
    records = []
    identities = set()
    design = platform = None
    for index, candidate in enumerate(supplied):
        if not isinstance(candidate, dict) or set(candidate) != {
            "id",
            "chimew_bundle",
            "vivado_bundle",
            "chimew_report_sha256",
            "vivado_report_sha256",
        }:
            raise ValidationError(f"correlation candidate {index} is invalid")
        candidate_id = candidate["id"]
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id in identities:
            raise ValidationError("correlation candidate IDs must be non-empty and unique")
        identities.add(candidate_id)
        chimew = Path(candidate["chimew_bundle"])
        vivado = Path(candidate["vivado_bundle"])
        if not chimew.is_absolute() or not vivado.is_absolute():
            raise ValidationError("correlation bundle paths must be absolute")
        chimew = chimew.resolve()
        vivado = vivado.resolve()
        chimew_report = chimew / "pipeline_report.json"
        vivado_report = vivado / "vivado-board-flow-report.json"
        with read_chimew_phase6_pipeline(
            chimew,
            expected_report_sha256=candidate["chimew_report_sha256"],
        ) as (
            chimew_document,
            chimew_validation,
        ):
            for label, path, expected in (
                ("Chimew", chimew_report, candidate["chimew_report_sha256"]),
                ("Vivado", vivado_report, candidate["vivado_report_sha256"]),
            ):
                if (
                    not path.is_file()
                    or path.is_symlink()
                    or _SHA256.fullmatch(str(expected)) is None
                    or _sha256(path) != expected
                ):
                    raise ValidationError(f"{label} correlation report hash differs")
            vivado_document = read_json(vivado_report)
            current_design = chimew_document.get("design")
            current_platform = chimew_document.get("platform")
            if (
                current_design != vivado_document.get("design")
                or current_platform != vivado_document.get("platform")
            ):
                raise ValidationError(
                    "Chimew and Vivado candidate identities disagree"
                )
            if design is None:
                design, platform = current_design, current_platform
            elif design != current_design or platform != current_platform:
                raise ValidationError(
                    "correlation candidates do not share design/platform"
                )
            records.append(
                {
                    "id": candidate_id,
                    "chimew_report_sha256": candidate["chimew_report_sha256"],
                    "vivado_report_sha256": candidate["vivado_report_sha256"],
                    "predicted": _chimew_metrics_from_snapshot(
                        chimew, chimew_document, chimew_validation
                    ),
                    "actual": _vivado_metrics(vivado),
                }
            )
            if (
                _sha256(chimew_report)
                != candidate["chimew_report_sha256"]
                or _sha256(vivado_report)
                != candidate["vivado_report_sha256"]
            ):
                raise ValidationError("correlation source changed during validation")
    records.sort(key=lambda record: record["id"])
    correlations = {
        "rudy_to_congestion": _correlation_record(
            records, "rudy_peak_utilization", "congestion_maximum_level", minimum
        ),
        "crossing_to_slr": _correlation_record(
            records, "crossing_bits", "slr_crossings", minimum
        ),
        "pin_distance_to_critical_path": _correlation_record(
            records, "pin_distance", "critical_path_ns", minimum
        ),
    }
    statuses = {record["status"] for record in correlations.values()}
    if "fail" in statuses:
        qualification = "rejected"
    elif statuses == {"pass"}:
        qualification = "qualified"
    else:
        qualification = "insufficient-evidence"
    return {
        "schema": CHIMEW_VIVADO_CORRELATION_REPORT_SCHEMA,
        "status": "pass",
        "qualification": qualification,
        "design": design,
        "platform": platform,
        "minimum_spearman": minimum,
        "manifest_sha256": canonical_sha256(document),
        "candidates": records,
        "correlations": correlations,
        "claim_boundary": (
            "rank correlation of byte-bound Chimew predictions against sealed "
            "post-route Vivado evidence; not bitstream or hardware qualification"
        ),
    }


def build_chimew_vivado_correlation(
    input_path: Path, output_path: Path
) -> Dict[str, Any]:
    report = _build_correlation_report(read_json(input_path))
    write_json(output_path, report)
    return report


def validate_chimew_vivado_correlation(
    input_path: Path, report_path: Path
) -> Dict[str, Any]:
    expected = _build_correlation_report(read_json(input_path))
    if read_json(report_path) != expected:
        raise ValidationError("Chimew/Vivado correlation report replay differs")
    return {
        "status": "pass",
        "qualification": expected["qualification"],
        "candidates": len(expected["candidates"]),
        "correlations": expected["correlations"],
    }
