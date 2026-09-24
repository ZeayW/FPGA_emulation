"""Common, fail-closed capability contract for Xilinx placer candidates.

The report is deliberately independent of any placer implementation.  It lets
feasibility branches state what their pinned upstream actually supports without
turning an unverified README claim into a production backend.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping

from .errors import ValidationError


XILINX_PLACER_CAPABILITY_SCHEMA = "emuflow.xilinx-placer-capability/v1"
XILINX_PLACER_CAPABILITY_STATUSES = (
    "native_supported",
    "adapter_required",
    "core_missing",
    "unverified",
)
XILINX_PLACER_REQUIRED_STAGES = (
    "global_placement",
    "packing",
    "legalization",
    "detailed_placement",
    "physical_export",
)


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value


def _validate_capability_entry(value: object, field: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field} must be an object")
    status = value.get("status")
    if status not in XILINX_PLACER_CAPABILITY_STATUSES:
        raise ValidationError(
            f"{field}.status must be one of "
            f"{list(XILINX_PLACER_CAPABILITY_STATUSES)}"
        )
    evidence = value.get("evidence")
    if (
        not isinstance(evidence, list)
        or not evidence
        or any(not isinstance(item, str) or not item.strip() for item in evidence)
    ):
        raise ValidationError(f"{field}.evidence must contain non-empty strings")
    adapter_validation = value.get("adapter_validation")
    if status == "adapter_required":
        if adapter_validation not in {"pass", "missing"}:
            raise ValidationError(
                f"{field}.adapter_validation must be pass or missing"
            )
    elif adapter_validation is not None:
        raise ValidationError(
            f"{field}.adapter_validation is valid only for adapter_required"
        )
    return {
        "status": status,
        "evidence": list(evidence),
        **(
            {"adapter_validation": adapter_validation}
            if adapter_validation is not None
            else {}
        ),
    }


def validate_xilinx_placer_capability_report(
    report: Mapping[str, Any],
) -> Dict[str, Any]:
    """Validate a candidate's evidence without claiming production readiness."""

    if report.get("schema") != XILINX_PLACER_CAPABILITY_SCHEMA:
        raise ValidationError("Xilinx placer capability report schema is invalid")
    provider = _nonempty_string(report.get("provider"), "provider")
    revision = _nonempty_string(report.get("revision"), "revision")
    stages = report.get("stages")
    if not isinstance(stages, Mapping) or set(stages) != set(
        XILINX_PLACER_REQUIRED_STAGES
    ):
        raise ValidationError(
            "stages must contain exactly "
            f"{list(XILINX_PLACER_REQUIRED_STAGES)}"
        )
    checked_stages = {
        stage: _validate_capability_entry(stages[stage], f"stages.{stage}")
        for stage in XILINX_PLACER_REQUIRED_STAGES
    }
    checked_populations: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for population in ("primitives", "constraints"):
        values = report.get(population)
        if not isinstance(values, Mapping) or not values:
            raise ValidationError(f"{population} must be a non-empty object")
        checked_populations[population] = {
            _nonempty_string(name, f"{population} key"): _validate_capability_entry(
                entry, f"{population}.{name}"
            )
            for name, entry in values.items()
        }
    return {
        "schema": XILINX_PLACER_CAPABILITY_SCHEMA,
        "provider": provider,
        "revision": revision,
        "stages": checked_stages,
        **checked_populations,
    }


def qualify_xilinx_placer_capabilities(
    report: Mapping[str, Any],
    *,
    required_primitives: Iterable[str],
    required_constraints: Iterable[str],
) -> Dict[str, Any]:
    """Return a fail-closed readiness decision for one concrete workload.

    An upstream-native capability is ready immediately.  A capability that
    needs an EmuFlow adapter becomes ready only after the report binds a
    passing adapter validation.  Missing and unverified facts never qualify.
    """

    checked = validate_xilinx_placer_capability_report(report)
    missing_entries = []
    blocked_entries = []

    def check_entry(path: str, entry: Mapping[str, Any]) -> None:
        status = entry["status"]
        if status in {"core_missing", "unverified"}:
            blocked_entries.append(path)
        elif status == "adapter_required" and entry.get(
            "adapter_validation"
        ) != "pass":
            blocked_entries.append(path)

    for stage, entry in checked["stages"].items():
        check_entry(f"stages.{stage}", entry)
    for population, required in (
        ("primitives", required_primitives),
        ("constraints", required_constraints),
    ):
        values = checked[population]
        for name in sorted(set(required)):
            if name not in values:
                missing_entries.append(f"{population}.{name}")
                continue
            check_entry(f"{population}.{name}", values[name])
    passed = not missing_entries and not blocked_entries
    return {
        "status": "pass" if passed else "fail",
        "provider": checked["provider"],
        "revision": checked["revision"],
        "missing_entries": missing_entries,
        "blocked_entries": sorted(blocked_entries),
    }

