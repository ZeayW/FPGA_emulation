"""Translate provider-neutral probe placement into documented PPro user syntax.

Only ``assign_inst {instance} {target}``, demonstrated by the installed PPro
user example, is emitted.  Route and forced-TDM directives are not guessed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Mapping

from .errors import ValidationError
from .io import read_json


_INSTANCE = re.compile(r"^[A-Za-z_][A-Za-z0-9_./\[\]-]*$")
_LOGICAL_FPGA = re.compile(r"^F[0-9]+$")
_PHYSICAL_TARGET = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _object(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{context}: expected an object")
    return value


def _strict_keys(
    value: Mapping[str, Any], required: set[str], optional: set[str], context: str
) -> None:
    missing = required - set(value)
    unknown = set(value) - required - optional
    if missing:
        raise ValidationError(f"{context}: missing fields {sorted(missing)}")
    if unknown:
        raise ValidationError(f"{context}: unknown fields {sorted(unknown)}")


def _brace(value: str, context: str) -> str:
    if not value or any(character in value for character in "{}\n\r\x00"):
        raise ValidationError(f"{context}: unsafe PPro constraint token")
    return "{" + value + "}"


def validate_logical_targets(value: Mapping[str, str]) -> Dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise ValidationError("PPro runtime requires logical placement targets")
    result: Dict[str, str] = {}
    physical_seen: set[str] = set()
    for raw_logical, raw_physical in value.items():
        logical = str(raw_logical).strip()
        physical = str(raw_physical).strip()
        if not _LOGICAL_FPGA.fullmatch(logical):
            raise ValidationError("PPro logical placement target must use F<n>")
        if not _PHYSICAL_TARGET.fullmatch(physical):
            raise ValidationError("PPro physical placement target is invalid")
        if logical in result or physical in physical_seen:
            raise ValidationError("PPro logical placement targets must be one-to-one")
        result[logical] = physical
        physical_seen.add(physical)
    return result


def parse_logical_targets(values: list[str]) -> Dict[str, str]:
    """Parse repeatable F<n>=PHYSICAL_TARGET runtime-only CLI arguments."""

    result: Dict[str, str] = {}
    for value in values:
        logical, separator, physical = value.partition("=")
        if not separator or not logical or not physical:
            raise ValidationError("PPro logical target must use F<n>=PHYSICAL_TARGET")
        if logical in result:
            raise ValidationError("PPro logical target repeats an FPGA")
        result[logical] = physical
    return validate_logical_targets(result)


def render_ppro_prepartition_constraints(
    documented_constraints_path: Path,
    logical_targets: Mapping[str, str],
    output_path: Path,
) -> None:
    """Render only fixed instance assignments from a generated constraint JSON."""

    constraints = _object(
        read_json(documented_constraints_path.resolve()), "documented constraints"
    )
    _strict_keys(
        constraints,
        {"control_mode", "documented_actions", "seed"},
        {"assignments", "forced_tdm_ratio"},
        "documented constraints",
    )
    if constraints["control_mode"] not in {"none", "fixed_assignment"}:
        raise ValidationError(
            "PPro constraint renderer supports only none or fixed_assignment control"
        )
    seed = constraints["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValidationError("documented constraints seed must be an integer >= 0")
    forced_tdm_ratio = constraints.get("forced_tdm_ratio", 0)
    if (
        isinstance(forced_tdm_ratio, bool)
        or not isinstance(forced_tdm_ratio, int)
        or forced_tdm_ratio < 0
    ):
        raise ValidationError("documented forced TDM ratio must be an integer >= 0")
    if forced_tdm_ratio:
        raise ValidationError(
            "PPro constraint renderer does not guess route, TDM, or random-seed syntax"
        )
    actions = constraints["documented_actions"]
    if not isinstance(actions, list) or any(not isinstance(item, str) for item in actions):
        raise ValidationError("documented constraints actions must be an array of strings")
    if len(actions) != len(set(actions)):
        raise ValidationError("documented constraints actions contain duplicates")
    unsupported = set(actions) - {"partition_constraint"}
    if unsupported:
        raise ValidationError(
            "PPro constraint renderer does not guess route, TDM, or random-seed syntax"
        )
    targets = validate_logical_targets(logical_targets)
    raw_assignments = constraints.get("assignments", [])
    if not isinstance(raw_assignments, list):
        raise ValidationError("documented constraints assignments must be an array")
    if bool(raw_assignments) != ("partition_constraint" in actions):
        raise ValidationError("partition action and assignment records disagree")

    lines = ["# Generated from provider-neutral documented user constraints."]
    seen_instances: set[str] = set()
    for index, raw_assignment in enumerate(raw_assignments):
        assignment = _object(raw_assignment, f"documented assignment {index}")
        _strict_keys(
            assignment,
            {"partition", "target"},
            set(),
            f"documented assignment {index}",
        )
        instance = assignment["partition"]
        logical = assignment["target"]
        if not isinstance(instance, str) or not _INSTANCE.fullmatch(instance):
            raise ValidationError("documented assignment has an invalid instance path")
        if not isinstance(logical, str) or logical not in targets:
            raise ValidationError("documented assignment uses an unmapped logical FPGA")
        if instance in seen_instances:
            raise ValidationError("documented constraints assign one instance more than once")
        seen_instances.add(instance)
        lines.append(
            "assign_inst "
            + _brace(instance, "PPro instance")
            + " "
            + _brace(targets[logical], "PPro target")
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
